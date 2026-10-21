"""Google Calendar v3 and authenticated CalDAV adapters.

Only explicit calendar IDs are synchronized. Transport is injectable for offline
integration tests; the production transport uses verified HTTPS, finite timeouts,
and no implicit credential-carrying redirects. Exceptions deliberately omit HTTP
bodies and request URLs, which can contain private calendar data.
"""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import http.client
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from icalendar import Calendar

from . import events as codec
from .types import CalendarInfo, ProviderError, RemoteEvent, Snapshot

DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CAL}
MAX_RESPONSE = 32 * 1024 * 1024


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes = b""
    url: str = ""

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), None)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    """Blocking transport, used exclusively off the server's asyncio loop."""

    def request(self, method: str, url: str, *, headers: dict, body: bytes | None = None) -> HTTPResponse:
        request = Request(url, data=body, headers=headers, method=method)
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=30)
        except HTTPError as exc:
            response = exc
        except (OSError, URLError, ValueError, http.client.HTTPException):
            raise ProviderError("Calendar network request failed", retryable=True) from None
        try:
            with response:
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise ProviderError("Calendar response exceeds safe size", retryable=True)
                return HTTPResponse(response.code, dict(response.headers.items()), data, url)
        except (OSError, URLError, http.client.HTTPException):
            raise ProviderError("Calendar network response interrupted", retryable=True) from None


def _error(response: HTTPResponse) -> ProviderError:
    retryable = response.status in {408, 425, 429} or response.status >= 500
    if response.status == 403:
        # Google uses HTTP 403 both for denied access and for rate limiting.
        retryable = any(reason in response.body for reason in
                        (b'"rateLimitExceeded"', b'"userRateLimitExceeded"', b'"quotaExceeded"'))
    return ProviderError(f"Calendar provider returned HTTP {response.status}",
                         status=response.status, retryable=retryable,
                         conflict=response.status in {409, 412})


def _json(response: HTTPResponse) -> dict:
    if not 200 <= response.status < 300:
        raise _error(response)
    try:
        result = json.loads(response.body)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise ProviderError("Calendar provider returned invalid JSON", retryable=True) from None


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise ProviderError("Calendar provider returned an invalid modification time") from None


def _uid(event: dict) -> str:
    return (event.get("extendedProperties", {}).get("private", {}).get("hestia_uid")
            or event.get("iCalUID") or "")


def _master_payload(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key != "exceptions"}


def _google_description(data: dict, private: dict) -> None:
    data["description"], marker = codec.encode_google_description(data.get("description", ""))
    if marker is not None:
        private["hestia_description_encoding"] = marker
    elif "hestia_description_encoding" in private:
        # An explicit empty value overrides properties retained/inherited by
        # Google's PATCH semantics; omission alone would keep the stale marker.
        private["hestia_description_encoding"] = ""


def _google_etag(master: dict, exceptions: list[dict]) -> str:
    if not master.get("etag"):
        raise ProviderError("Calendar event is missing a revision", retryable=True)
    if not master.get("recurrence"):
        return master["etag"]
    parts = {"master": master["etag"], "exceptions": {}}
    for event in exceptions:
        revision = event.get("etag")
        if not revision:
            if event.get("status") != "cancelled":
                raise ProviderError("Calendar exception is missing a revision", retryable=True)
            # Cancelled occurrences only guarantee id/parent/originalStartTime.
            revision = "cancelled:" + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        parts["exceptions"][event["id"]] = revision
    return "hestia-bundle:" + json.dumps(parts, sort_keys=True, separators=(",", ":"))


def _master_etag(etag: str | None) -> str | None:
    if etag and etag.startswith("hestia-bundle:"):
        try:
            return json.loads(etag[len("hestia-bundle:"):])["master"]
        except (ValueError, KeyError, TypeError):
            raise ProviderError("Invalid stored calendar revision") from None
    return etag


@dataclass
class _Mutation:
    attempted: bool = False


@contextmanager
def _mutation_guard():
    """Distinguish failed preflight reads from possibly completed writes."""
    mutation = _Mutation()
    try:
        yield mutation
    except ProviderError as exc:
        exc.no_mutation = not mutation.attempted
        raise


class GoogleCalendarProvider:
    API = "https://www.googleapis.com/calendar/v3"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    CURSOR_PREFIX = "hestia-google:2:"

    def __init__(self, calendar_id: str, *, client_id: str, client_secret: str,
                 refresh_token: str, transport=None):
        self.calendar_id = calendar_id
        self._credentials = {"client_id": client_id, "client_secret": client_secret,
                             "refresh_token": refresh_token, "grant_type": "refresh_token"}
        self._transport = transport or HTTPTransport()
        self._access_token = ""
        self._expires = 0.0

    def allocate_id(self, uid: str) -> str:
        # Hex is a subset of Google's base32hex alphabet. Stable across retries.
        return "h" + hashlib.sha256(uid.encode()).hexdigest()

    def _token(self) -> str:
        if time.monotonic() >= self._expires:
            response = self._transport.request("POST", self.TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                body=urlencode(self._credentials).encode())
            data = _json(response)
            if not isinstance(data.get("access_token"), str) or not data["access_token"]:
                raise ProviderError("Google authorization response has no access token")
            self._access_token = data["access_token"]
            try:
                self._expires = time.monotonic() + max(1, float(data.get("expires_in", 3600)) - 60)
            except (ValueError, TypeError):
                raise ProviderError("Invalid Google authorization token lifetime") from None
        return self._access_token

    def _request(self, method: str, path: str, *, params=None, data=None, etag=None) -> dict:
        headers = {"Authorization": "Bearer " + self._token(), "Accept": "application/json"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        if etag is not None:
            headers["If-Match"] = etag
        url = self.API + path + ("?" + urlencode(params) if params else "")
        response = self._transport.request(method, url, headers=headers, body=body)
        if response.status == 401:
            self._expires = 0
            headers["Authorization"] = "Bearer " + self._token()
            response = self._transport.request(method, url, headers=headers, body=body)
        if response.status == 204:
            return {}
        result = _json(response)
        header_etag = response.header("ETag")
        if header_etag and "etag" not in result:
            result["etag"] = header_etag
        return result

    @property
    def _events_path(self) -> str:
        return "/calendars/" + quote(self.calendar_id, safe="") + "/events"

    @staticmethod
    def _calendar(data: dict) -> CalendarInfo:
        role = data.get("accessRole")
        writable = role in {"writer", "owner"} and not data.get("deleted")
        reason = None if writable else "Full read/write permission is required (private events must be visible)"
        return CalendarInfo(data["id"], data.get("summaryOverride") or data.get("summary") or data["id"],
                            writable, reason, data.get("timeZone"))

    def discover(self) -> list[CalendarInfo]:
        result = []
        page = None
        seen = set()
        while True:
            params = {"maxResults": 250, "showHidden": "true"}
            if page:
                params["pageToken"] = page
            data = self._request("GET", "/users/me/calendarList", params=params)
            try:
                result.extend(self._calendar(item) for item in data["items"])
            except (KeyError, TypeError):
                raise ProviderError("Google returned an incomplete calendar list", retryable=True) from None
            page = data.get("nextPageToken")
            if not page:
                return result
            if page in seen:
                raise ProviderError("Google calendar pagination repeated", retryable=True)
            seen.add(page)

    def _check_calendar(self) -> CalendarInfo:
        if not self.calendar_id:
            raise ProviderError("Select an explicit Google calendar ID")
        data = self._request("GET", "/users/me/calendarList/" + quote(self.calendar_id, safe=""))
        try:
            info = self._calendar(data)
        except (KeyError, TypeError):
            raise ProviderError("Google returned incomplete calendar permissions", retryable=True) from None
        if not info.writable:
            raise ProviderError("Google calendar does not grant full read/write permission", status=403)
        return info

    def _list(self, cursor: str | None) -> tuple[list[dict], str]:
        result = []
        page = None
        seen = set()
        while True:
            params = {"maxResults": 2500, "showDeleted": "true", "singleEvents": "false",
                      "showHiddenInvitations": "true"}
            if cursor:
                params["syncToken"] = cursor
            if page:
                params["pageToken"] = page
            data = self._request("GET", self._events_path, params=params)
            if data.get("accessRole") not in {"writer", "owner"}:
                raise ProviderError("Google event list lacks full calendar permissions", status=403)
            items = data.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("id") for item in items):
                raise ProviderError("Google returned an incomplete event page", retryable=True)
            result.extend(items)
            page = data.get("nextPageToken")
            if not page:
                cursor = data.get("nextSyncToken")
                if not isinstance(cursor, str) or not cursor:
                    raise ProviderError("Google omitted the final sync token", retryable=True)
                return result, cursor
            if page in seen:
                raise ProviderError("Google event pagination repeated", retryable=True)
            seen.add(page)

    @staticmethod
    def _detached_uid(event: dict) -> str:
        private = event.get("extendedProperties", {}).get("private", {})
        if private.get("hestia_detached_instance") == event["id"] and private.get("hestia_uid"):
            return private["hestia_uid"]
        # Instances can inherit the parent's hestia_uid. They need a distinct
        # identity until metadata explicitly marks this particular detached copy.
        original = event.get("iCalUID") or private.get("hestia_uid") or ""
        if not original and event.get("status") != "cancelled":
            raise ValueError
        identity = original + "\0" + event["id"]
        return "hestia-google-detached:" + hashlib.sha256(identity.encode()).hexdigest()

    def _record(self, master: dict, exceptions: list[dict] | None = None) -> RemoteEvent:
        exceptions = exceptions or []
        deleted = master.get("status") == "cancelled"
        try:
            payload = None if deleted else codec.from_google_bundle(master, exceptions)
            etag = master.get("etag") if deleted else _google_etag(master, exceptions)
            updated = max(_timestamp(event.get("updated")) for event in [master, *exceptions])
            if master.get("recurringEventId"):
                if master.get("recurrence"):
                    raise ValueError
                uid = self._detached_uid(master)
            else:
                uid = _uid(master) or ("google-deleted:" + master["id"] if deleted else "")
            if not uid:
                raise ValueError
            return RemoteEvent(master["id"], uid, payload, etag, updated, deleted)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError("Google event cannot be safely synchronized") from None

    def pull(self, cursor: str | None) -> Snapshot:
        self._check_calendar()
        # Query parameters form part of Google's cursor contract. Older stored
        # tokens used a list that omitted hidden invitations; rescan safely.
        cursor = (cursor[len(self.CURSOR_PREFIX):] or None) if cursor and cursor.startswith(self.CURSOR_PREFIX) else None
        full = cursor is None
        try:
            items, new_cursor = self._list(cursor)
        except ProviderError as exc:
            if exc.status != 410 or not cursor:
                raise
            items, new_cursor = self._list(None)
            full = True
        # A changed exception contains only its own delta. Reconstruct the entire
        # series from an unexpanded full list, never erase unreturned exceptions.
        if not full and any(e.get("recurringEventId") or e.get("recurrence") for e in items):
            items, new_cursor = self._list(None)
            full = True
        masters = {e["id"]: e for e in items if not e.get("recurringEventId")}
        grouped: dict[str, list[dict]] = {}
        detached, detached_parents = [], set()
        for event in items:
            parent = event.get("recurringEventId")
            if parent:
                if parent not in detached_parents and (parent not in masters or masters[parent].get("status") == "cancelled"):
                    try:
                        actual = self._request("GET", self._events_path + "/" + quote(parent, safe=""))
                    except ProviderError as exc:
                        if exc.status not in {404, 410}:
                            raise
                    else:
                        if actual.get("id") != parent or actual.get("status") != "cancelled":
                            raise ProviderError("Google recurring series is missing from the complete inventory", retryable=True)
                        masters[parent] = actual
                    detached_parents.add(parent)
                if parent in detached_parents:
                    # Google can retain a visible occurrence after its series
                    # disappears. Keep that occurrence as a mapped one-off; do
                    # not invent a recurrence rule or discard its content.
                    detached.append(self._record(event))
                else:
                    grouped.setdefault(parent, []).append(event)
        records = [self._record(event, grouped.get(identifier)) for identifier, event in masters.items()]
        self._check_calendar()  # Permission loss midway must never become deletions.
        return Snapshot(records + detached, self.CURSOR_PREFIX + new_cursor, complete=True, full=full)

    def _current(self, remote_id: str) -> tuple[RemoteEvent, dict, list[dict]]:
        master = self._request("GET", self._events_path + "/" + quote(remote_id, safe=""))
        exceptions = []
        if master.get("recurrence") and master.get("status") != "cancelled":
            items, _ = self._list(None)
            exceptions = [event for event in items if event.get("recurringEventId") == remote_id]
        return self._record(master, exceptions), master, exceptions

    def _instance(self, remote_id: str, original: dict, *, allow_missing=False) -> dict | None:
        value = original.get("dateTime") or original.get("date")
        data = self._request("GET", self._events_path + "/" + quote(remote_id, safe="") + "/instances",
                             params={"originalStart": value, "showDeleted": "true", "maxResults": 2500})
        matches = [event for event in data.get("items", [])
                   if codec.original_start_key(event.get("originalStartTime", {})) == codec.original_start_key(original)]
        if not matches and allow_missing:
            return None
        if len(matches) != 1:
            raise ProviderError("Google recurrence instance could not be located", retryable=True)
        instance = matches[0]
        if not instance.get("etag"):
            instance = self._request("GET", self._events_path + "/" + quote(instance["id"], safe=""))
            if not instance.get("etag"):
                raise ProviderError("Google recurrence instance is missing a revision", retryable=True)
        return instance

    @staticmethod
    def _type_restriction() -> ProviderError:
        return ProviderError("Google restricts changes to this special event type",
                             code="google_event_type_restricted")

    @staticmethod
    def _special_patch(payload: dict, uid: str, current: dict) -> dict | None:
        event_type = current.get("eventType", "default")
        if event_type not in {"birthday", "fromGmail"}:
            return None
        before, after = codec.from_google_event(current), _master_payload(payload)
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        allowed = {"summary", "start", "end"} if event_type == "birthday" else {"visibility", "status"}
        if changed - allowed or uid != _uid(current):
            raise GoogleCalendarProvider._type_restriction()
        if event_type == "birthday" and changed & {"start", "end"}:
            properties = current.get("birthdayProperties", {})
            if properties.get("contact") or properties.get("type") == "self":
                raise GoogleCalendarProvider._type_restriction()
            start, end = after["start"], after["end"]
            if "date" not in start:
                raise GoogleCalendarProvider._type_restriction()
            if date.fromisoformat(end["date"]) - date.fromisoformat(start["date"]) != timedelta(days=1):
                raise GoogleCalendarProvider._type_restriction()
        # Avoid defaults/metadata that these event types do not allow updating.
        defaults = {"summary": "", "visibility": "default", "status": "confirmed"}
        return {"eventType": event_type, **{key: after.get(key, defaults.get(key)) for key in changed}}

    @staticmethod
    def _patch_body(payload: dict, uid: str, current: dict) -> dict:
        special = GoogleCalendarProvider._special_patch(payload, uid, current)
        if special is not None:
            return special
        data = codec.to_google_event(payload)
        # PATCH only synchronized fields; retain destination attendees, alarms,
        # conferencing and unrelated provider metadata.
        for key in ("summary", "description", "location"):
            data.setdefault(key, "")
        for key, default in (("status", "confirmed"), ("transparency", "opaque"), ("visibility", "default")):
            data.setdefault(key, default)
        if current.get("recurringEventId"):
            data.pop("recurrence", None)  # Updating one detached occurrence must not change its former series.
        else:
            data.setdefault("recurrence", [])
        private = dict(current.get("extendedProperties", {}).get("private", {}))
        private["hestia_uid"] = uid
        private["hestia_end_unspecified"] = "true" if payload.get("endTimeUnspecified") else "false"
        if current.get("recurringEventId"):
            private["hestia_detached_instance"] = current["id"]
        _google_description(data, private)
        data["extendedProperties"] = {"private": private}
        return data

    def put(self, remote_id: str, payload: dict, etag: str | None, *, create: bool,
            uid: str | None = None) -> RemoteEvent:
        with _mutation_guard() as mutation:
            return self._put(remote_id, payload, etag, create=create, uid=uid, mutation=mutation)

    def _put(self, remote_id: str, payload: dict, etag: str | None, *, create: bool,
             uid: str | None, mutation: _Mutation) -> RemoteEvent:
        payload = codec.validate_payload(payload)
        self._check_calendar()
        uid = uid or remote_id
        if create:
            data = self._patch_body(payload, uid, {})
            data["id"] = remote_id
            try:
                mutation.attempted = True
                self._request("POST", self._events_path, params={"sendUpdates": "none"}, data=data)
            except ProviderError as exc:
                if exc.status != 409:
                    raise
                current, _, _ = self._current(remote_id)
                if current.uid != uid or current.deleted:
                    raise ProviderError("Google event ID is already occupied", status=409, conflict=True) from None
        current, master, exceptions = self._current(remote_id)
        if current.deleted:
            raise ProviderError("Google event was deleted", status=410, conflict=True)
        if current.payload == payload and current.uid == uid:
            return current
        if not create and (not etag or etag != current.etag):
            raise ProviderError("Google event changed before update", status=412, conflict=True)
        if (master.get("eventType") in {"birthday", "fromGmail"}
                and current.payload.get("exceptions") != payload.get("exceptions")):
            raise self._type_restriction()
        if _master_payload(current.payload) != _master_payload(payload) or current.uid != uid:
            data = self._patch_body(payload, uid, master)
            mutation.attempted = True
            master = self._request("PATCH", self._events_path + "/" + quote(remote_id, safe=""),
                params={"sendUpdates": "none"}, data=data, etag=master["etag"])
        wanted = {codec.original_start_key(e["originalStartTime"]): e for e in payload.get("exceptions", [])}
        existing = ({codec.original_start_key(e["originalStartTime"]): e for e in current.payload.get("exceptions", [])}
                    if payload.get("recurrence") else {})
        known = {codec.original_start_key(e["originalStartTime"]): e for e in exceptions}
        for key in sorted(wanted.keys() | existing.keys()):
            if wanted.get(key) == existing.get(key):
                continue
            original = (wanted.get(key) or existing[key])["originalStartTime"]
            instance = self._instance(remote_id, original, allow_missing=key not in wanted)
            if instance is None:
                continue  # Changing the recurrence rule can remove an old override.
            observed = codec.from_google_bundle(master, [instance]).get("exceptions", [])
            observed_content = observed[0] if observed else None
            if observed_content == wanted.get(key):
                continue  # A previous interrupted write already completed this part.
            previous = known.get(key)
            if previous and previous.get("etag"):
                changed = previous["etag"] != instance["etag"]
            else:
                changed = observed_content != existing.get(key)
            if changed:
                raise ProviderError("Google recurrence exception changed before update", status=412, conflict=True)
            override = wanted.get(key) or {"originalStartTime": original, **codec.default_instance(payload, original)}
            data = codec.to_google_exception(override)
            data.pop("originalStartTime", None)
            private = dict(instance.get("extendedProperties", {}).get("private", {}))
            private["hestia_end_unspecified"] = "true" if override.get("endTimeUnspecified") else "false"
            _google_description(data, private)
            data["extendedProperties"] = {"private": private}
            data.setdefault("status", "confirmed")
            for field in ("summary", "description", "location"):
                data.setdefault(field, "")
            mutation.attempted = True
            self._request("PATCH", self._events_path + "/" + quote(instance["id"], safe=""),
                          params={"sendUpdates": "none"}, data=data, etag=instance["etag"])
        result = self._current(remote_id)[0]
        if result.payload != payload:
            raise ProviderError("Google event changed while writing", status=412, conflict=True)
        return result

    def delete(self, remote_id: str, etag: str | None, *, uid: str | None = None) -> None:
        with _mutation_guard() as mutation:
            self._delete(remote_id, etag, uid=uid, mutation=mutation)

    def _delete(self, remote_id: str, etag: str | None, *, uid: str | None,
                mutation: _Mutation) -> None:
        self._check_calendar()
        if not etag:
            try:
                current, _, _ = self._current(remote_id)
            except ProviderError as exc:
                if exc.status in {404, 410}:
                    return
                raise
            if current.deleted:
                return
            if not uid or current.uid != uid:
                raise ProviderError("Unconfirmed Google event identity differs", conflict=True)
            etag = current.etag
        master_etag = _master_etag(etag)
        try:
            mutation.attempted = True
            self._request("DELETE", self._events_path + "/" + quote(remote_id, safe=""),
                          params={"sendUpdates": "none"}, etag=master_etag)
        except ProviderError as exc:
            if exc.status not in {404, 410}:
                raise


GoogleProvider = GoogleCalendarProvider


def _xml(body: bytes) -> ET.Element:
    try:
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError
        root = ET.fromstring(body)
        if root.tag != f"{{{DAV}}}multistatus" or root.find(".//d:error", NS) is not None:
            raise ValueError
        return root
    except (ET.ParseError, ValueError):
        raise ProviderError("CalDAV returned invalid or incomplete XML", retryable=True) from None


def _status(element: ET.Element, path="d:status") -> int:
    try:
        return int(element.findtext(path, namespaces=NS).split()[1])
    except (AttributeError, IndexError, ValueError):
        raise ProviderError("CalDAV omitted a response status", retryable=True) from None


def _properties(response: ET.Element, *, strict=False) -> dict[str, ET.Element]:
    direct = response.find("d:status", NS)
    if direct is not None and _status(response) != 200:
        raise ProviderError("CalDAV returned an incomplete response", retryable=True)
    properties = {}
    for part in response.findall("d:propstat", NS):
        if _status(part) != 200:
            if strict:
                raise ProviderError("CalDAV returned incomplete event properties", retryable=True)
            continue
        node = part.find("d:prop", NS)
        if node is None:
            raise ProviderError("CalDAV omitted response properties", retryable=True)
        properties.update({element.tag: element for element in node})
    return properties


def _verify_collection_echo(response: ET.Element) -> None:
    """Accept only iCloud's collection ETag plus absent calendar-data echo.

    A collection has no VEVENT body. This is not a missing child resource;
    every child and every unexpected collection error must still abort the read.
    """
    parts = response.findall("d:propstat", NS)
    if response.find("d:status", NS) is not None or len(parts) != 2:
        raise ProviderError("CalDAV returned unexpected collection inventory metadata", retryable=True)
    by_status = {}
    for part in parts:
        status, prop = _status(part), part.find("d:prop", NS)
        if prop is None or status in by_status:
            raise ProviderError("CalDAV returned incomplete collection inventory metadata", retryable=True)
        by_status[status] = prop
    success, missing = by_status.get(200), by_status.get(404)
    if (success is None or missing is None
            or [node.tag for node in success] != [f"{{{DAV}}}getetag"]
            or [node.tag for node in missing] != [f"{{{CAL}}}calendar-data"]):
        raise ProviderError("CalDAV returned unexpected collection inventory properties", retryable=True)
    etag, data = success[0], missing[0]
    if not (etag.text or "").strip() or len(etag) or len(data) or (data.text or "").strip():
        raise ProviderError("CalDAV collection inventory metadata contains unexpected content", retryable=True)


def _secure_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))):
            raise ValueError
        parsed.port  # Validate malformed ports before handing the URL to urllib.
    except ValueError:
        raise ProviderError("CalDAV requires an absolute HTTPS URL without credentials") from None
    return url


def _same_https_location(left: str, right: str) -> bool:
    """Compare an exact path while allowing the equivalent HTTPS :443 origin."""
    first, second = urlsplit(_secure_url(left)), urlsplit(_secure_url(right))
    return (first.hostname, first.port or 443, first.path) == (second.hostname, second.port or 443, second.path)


def _trusted_url(url: str, base: str) -> str:
    _secure_url(url)
    current, target = urlsplit(base), urlsplit(url)
    same_origin = (current.hostname, current.port or 443) == (target.hostname, target.port or 443)
    icloud_shard = (current.hostname.endswith(".icloud.com") and target.hostname.endswith(".icloud.com")
                    and (current.port or 443) == (target.port or 443) == 443)
    if not (same_origin or icloud_shard):
        raise ProviderError("CalDAV attempted to redirect credentials to an untrusted server")
    return url


def _ical_record(remote_id: str, body: bytes, etag: str | None, *, floating_time_zone: str | None = None) -> RemoteEvent:
    if not etag:
        raise ProviderError("CalDAV event is missing a revision", retryable=True)
    try:
        calendar = Calendar.from_ical(body)
        entries = calendar.walk("VEVENT")
        masters = [entry for entry in entries if "RECURRENCE-ID" not in entry]
        if len(masters) != 1 or any(entry.errors for entry in entries):
            raise ValueError
        master = masters[0]
        uid = str(master.get("UID", ""))
        if not uid or any(str(entry.get("UID", "")) != uid for entry in entries):
            raise ValueError
        stamps = [value.dt for entry in entries
                  for name in ("LAST-MODIFIED", "DTSTAMP")
                  if (value := entry.get(name)) is not None]
        if any(not isinstance(value, datetime) or value.tzinfo is None for value in stamps):
            raise ValueError
        updated = max((value.timestamp() for value in stamps), default=0.0)
        deleted = str(master.get("STATUS", "")).upper() == "CANCELLED"
        payload = None if deleted else codec.from_ical(body, floating_time_zone=floating_time_zone)[1]
        return RemoteEvent(remote_id, uid, payload, etag, updated, deleted)
    except (ValueError, TypeError, AttributeError, KeyError):
        raise ProviderError("CalDAV event cannot be safely synchronized") from None


def _merge_ical(original: bytes, generated: bytes, *, floating_time_zone: str | None = None) -> bytes:
    """Keep destination-only metadata (including alarms and invitations) on edit."""
    old, new = Calendar.from_ical(original), Calendar.from_ical(generated)
    controlled = {"UID", "DTSTAMP", "LAST-MODIFIED", "CREATED", "SEQUENCE", "SUMMARY", "DESCRIPTION",
                  "LOCATION", "DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXDATE", "STATUS",
                  "RECURRENCE-ID", "TRANSP", "CLASS", "X-HESTIA-END-UNSPECIFIED"}
    def recurrence_key(event):
        prop = event.get("RECURRENCE-ID")
        if prop is None:
            return ""
        value = prop.dt
        temporal = {"dateTime" if isinstance(value, datetime) else "date": value.isoformat()}
        if isinstance(value, datetime) and value.tzinfo is None and floating_time_zone:
            temporal["timeZone"] = floating_time_zone
        return codec.original_start_key(temporal)

    previous = {recurrence_key(e): e for e in old.walk("VEVENT")}
    for event in new.walk("VEVENT"):
        source = previous.get(recurrence_key(event))
        if source is not None:
            for key, value in source.items():
                if key not in controlled:
                    event[key] = copy.deepcopy(value)
            event.subcomponents.extend(copy.deepcopy(source.subcomponents))
    return new.to_ical()


class CalDAVProvider:
    """Authenticated writable iCloud/CalDAV collection, never a public ICS feed.

    Full, unbounded VEVENT REPORTs deliberately avoid depending on optional
    server sync-token extensions. ETags detect changes; the engine persists
    successful inventories and deletion tombstones. A partial REPORT is rejected.
    """

    def __init__(self, calendar_id: str, *, username: str, password: str,
                 url: str = "https://caldav.icloud.com", floating_time_zone: str | None = None, transport=None):
        self.url = _secure_url(url)
        self.floating_time_zone = floating_time_zone
        self.calendar_id = _trusted_url(calendar_id, self.url) if calendar_id else ""
        if self.calendar_id and not self.calendar_id.endswith("/"):
            raise ProviderError("CalDAV calendar collection URL must end with a slash")
        host = urlsplit(self.calendar_id).hostname or ""
        # iCloud rejects VEVENTs with neither DTEND nor DURATION, even though
        # RFC5545 permits an omitted end for a point/unknown-end event.
        self._explicit_end = host == "caldav.icloud.com" or host.endswith("-caldav.icloud.com")
        self._auth = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        self._transport = transport or HTTPTransport()

    def allocate_id(self, uid: str) -> str:
        return self._resource(self.calendar_id + "h" + hashlib.sha256(uid.encode()).hexdigest() + ".ics")

    def _resource(self, remote_id: str) -> str:
        if not self.calendar_id:
            raise ProviderError("Select an explicit CalDAV calendar collection")
        target, selected = urlsplit(_secure_url(remote_id)), urlsplit(self.calendar_id)
        if ((target.hostname, target.port or 443) != (selected.hostname, selected.port or 443)
                or not target.path.startswith(selected.path) or target.path == selected.path):
            raise ProviderError("CalDAV event URL is outside the selected calendar")
        # The server can omit :443 in REPORT hrefs. Keep one stable identity in
        # the exact configured origin style; do not rewrite configuration or DB.
        return urlunsplit((selected.scheme, selected.netloc, target.path, "", ""))

    def _request(self, method: str, url: str, *, body=None, headers=None) -> HTTPResponse:
        values = {"Authorization": self._auth, **(headers or {})}
        if body is not None:
            values.setdefault("Content-Type", "application/xml; charset=utf-8")
        for _ in range(5):
            _trusted_url(url, self.url)
            response = self._transport.request(method, url, headers=values, body=body)
            if response.status not in {301, 302, 307, 308}:
                if not 200 <= response.status < 300:
                    raise _error(response)
                return HTTPResponse(response.status, response.headers, response.body, url)
            target = response.header("Location")
            if not target:
                raise ProviderError("CalDAV redirect omitted its location", retryable=True)
            url = _trusted_url(urljoin(url, target), self.url)
        raise ProviderError("CalDAV redirected too many times", retryable=True)

    def _propfind(self, url: str, names: str, *, depth="0") -> tuple[str, list[ET.Element]]:
        body = (f'<d:propfind xmlns:d="DAV:" xmlns:c="{CAL}"><d:prop>{names}</d:prop></d:propfind>').encode()
        response = self._request("PROPFIND", url, body=body, headers={"Depth": depth})
        root = _xml(response.body)
        return response.url, root.findall("d:response", NS)

    @staticmethod
    def _info(identifier: str, properties: dict[str, ET.Element]) -> CalendarInfo | None:
        resource_type = properties.get(f"{{{DAV}}}resourcetype")
        if resource_type is None or resource_type.find("c:calendar", NS) is None:
            return None
        privileges = properties.get(f"{{{DAV}}}current-user-privilege-set")
        granted = {node.tag for node in privileges.findall("d:privilege/*", NS)} if privileges is not None else set()
        all_access = f"{{{DAV}}}all" in granted
        writable = all_access or (f"{{{DAV}}}read" in granted and
                   (f"{{{DAV}}}write" in granted or
                    {f"{{{DAV}}}write-content", f"{{{DAV}}}bind", f"{{{DAV}}}unbind"} <= granted))
        supported = properties.get(f"{{{CAL}}}supported-calendar-component-set")
        if supported is not None and not any(node.get("name") == "VEVENT" for node in supported):
            writable = False
        display = properties.get(f"{{{DAV}}}displayname")
        return CalendarInfo(identifier, display.text if display is not None and display.text else identifier,
                            writable, None if writable else "CalDAV must grant read, create, edit and delete access to VEVENTs")

    _CALENDAR_PROPS = ('<d:resourcetype/><d:displayname/><d:current-user-privilege-set/>'
                       '<c:supported-calendar-component-set/><d:sync-token/>')

    def discover(self) -> list[CalendarInfo]:
        base, responses = self._propfind(self.url, "<d:current-user-principal/>")
        principal = next((node.find("d:href", NS).text for response in responses
                          for node in _properties(response).values()
                          if node.tag == f"{{{DAV}}}current-user-principal" and node.find("d:href", NS) is not None), None)
        if not principal:
            raise ProviderError("CalDAV did not advertise the account principal")
        principal_url = _trusted_url(urljoin(base, principal), self.url)
        base, responses = self._propfind(principal_url, "<c:calendar-home-set/>")
        homes = [node.text for response in responses
                 for prop in _properties(response).values() if prop.tag == f"{{{CAL}}}calendar-home-set"
                 for node in prop.findall("d:href", NS) if node.text]
        if not homes:
            raise ProviderError("CalDAV did not advertise calendar homes")
        result = {}
        for home in homes:
            home_url = _trusted_url(urljoin(base, home), self.url)
            listing_url, responses = self._propfind(home_url, self._CALENDAR_PROPS, depth="1")
            for response in responses:
                href = response.findtext("d:href", namespaces=NS)
                if not href:
                    raise ProviderError("CalDAV discovery omitted a collection URL", retryable=True)
                identifier = _trusted_url(urljoin(listing_url, href), self.url)
                info = self._info(identifier, _properties(response))
                if info is not None:
                    result[identifier] = info
        return list(result.values())

    def _check_calendar(self) -> str | None:
        if not self.calendar_id:
            raise ProviderError("Select an explicit CalDAV calendar collection")
        base, responses = self._propfind(self.calendar_id, self._CALENDAR_PROPS)
        if not _same_https_location(base, self.calendar_id):
            # A moved collection must be rediscovered and deliberately selected;
            # silently rebinding IDs could turn all mapped events into deletions.
            raise ProviderError("CalDAV calendar URL changed; rediscover and update configuration")
        if len(responses) != 1:
            raise ProviderError("CalDAV did not verify the selected calendar", retryable=True)
        href = responses[0].findtext("d:href", namespaces=NS)
        if not href or not _same_https_location(urljoin(base, href), self.calendar_id):
            raise ProviderError("CalDAV did not verify the selected calendar", retryable=True)
        properties = _properties(responses[0])
        info = self._info(self.calendar_id, properties)
        if info is None or not info.writable:
            raise ProviderError("CalDAV calendar does not grant complete read/write permission", status=403)
        token = properties.get(f"{{{DAV}}}sync-token")
        return token.text if token is not None else None

    def pull(self, cursor: str | None) -> Snapshot:
        before = self._check_calendar()
        body = (f'<c:calendar-query xmlns:d="DAV:" xmlns:c="{CAL}">'
                '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
                '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/>'
                '</c:comp-filter></c:filter></c:calendar-query>').encode()
        response = self._request("REPORT", self.calendar_id, body=body, headers={"Depth": "1"})
        records = []
        seen = set()
        for item in _xml(response.body).findall("d:response", NS):
            href = item.findtext("d:href", namespaces=NS)
            if not href:
                raise ProviderError("CalDAV event listing omitted a resource URL", retryable=True)
            target = urljoin(self.calendar_id, href)
            if _same_https_location(target, self.calendar_id):
                _verify_collection_echo(item)
                continue
            remote_id = self._resource(target)
            properties = _properties(item, strict=True)
            data, etag = properties.get(f"{{{CAL}}}calendar-data"), properties.get(f"{{{DAV}}}getetag")
            if data is None or not data.text or etag is None or remote_id in seen:
                raise ProviderError("CalDAV returned an incomplete event inventory", retryable=True)
            seen.add(remote_id)
            records.append(_ical_record(remote_id, data.text.encode(), etag.text, floating_time_zone=self.floating_time_zone))
        after = self._check_calendar()
        if before != after:
            raise ProviderError("CalDAV calendar changed during inventory; retry required", retryable=True)
        return Snapshot(records, after, complete=True, full=True)

    def _get(self, remote_id: str) -> tuple[RemoteEvent, bytes]:
        response = self._request("GET", self._resource(remote_id), headers={"Accept": "text/calendar"})
        return _ical_record(remote_id, response.body, response.header("ETag"), floating_time_zone=self.floating_time_zone), response.body

    def put(self, remote_id: str, payload: dict, etag: str | None, *, create: bool,
            uid: str | None = None) -> RemoteEvent:
        with _mutation_guard() as mutation:
            return self._put(remote_id, payload, etag, create=create, uid=uid, mutation=mutation)

    def _put(self, remote_id: str, payload: dict, etag: str | None, *, create: bool,
             uid: str | None, mutation: _Mutation) -> RemoteEvent:
        payload = codec.validate_payload(payload)
        self._resource(remote_id)
        self._check_calendar()
        uid = uid or remote_id
        generated = codec.to_ical(payload, uid, explicit_end=self._explicit_end)
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        else:
            current, body = self._get(remote_id)
            if current.deleted:
                raise ProviderError("CalDAV event was deleted", status=410, conflict=True)
            if current.payload == payload and current.uid == uid:
                return current
            if not etag or current.etag != etag:
                raise ProviderError("CalDAV event changed before update", status=412, conflict=True)
            headers["If-Match"] = etag
            generated = _merge_ical(body, generated, floating_time_zone=self.floating_time_zone)
        try:
            mutation.attempted = True
            self._request("PUT", remote_id, body=generated, headers=headers)
        except ProviderError as exc:
            if not create or exc.status not in {409, 412}:
                raise
            current, _ = self._get(remote_id)
            if current.uid != uid or current.deleted:
                raise ProviderError("CalDAV event URL is already occupied", status=409, conflict=True) from None
            # A prior create may have succeeded before its response was lost.
            if current.payload != payload:
                raise ProviderError("CalDAV created event changed before retry", status=412, conflict=True) from None
            return current
        result = self._get(remote_id)[0]
        if result.payload != payload:
            raise ProviderError("CalDAV event changed while writing", status=412, conflict=True)
        return result

    def delete(self, remote_id: str, etag: str | None, *, uid: str | None = None) -> None:
        with _mutation_guard() as mutation:
            self._delete(remote_id, etag, uid=uid, mutation=mutation)

    def _delete(self, remote_id: str, etag: str | None, *, uid: str | None,
                mutation: _Mutation) -> None:
        self._resource(remote_id)
        self._check_calendar()
        if not etag:
            try:
                current, _ = self._get(remote_id)
            except ProviderError as exc:
                if exc.status in {404, 410}:
                    return
                raise
            if current.deleted:
                return
            if not uid or current.uid != uid:
                raise ProviderError("Unconfirmed CalDAV event identity differs", conflict=True)
            etag = current.etag
        try:
            mutation.attempted = True
            self._request("DELETE", remote_id, headers={"If-Match": etag})
        except ProviderError as exc:
            if exc.status not in {404, 410}:
                raise
