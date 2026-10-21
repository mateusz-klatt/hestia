"""Provider protocol tests: no network and no access to personal calendars."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from icalendar import Calendar

from hestia.calendar_sync import events as codec
from hestia.calendar_sync import providers as p
from hestia.calendar_sync.types import ProviderError


PAYLOAD = codec.validate_payload({"summary": "Family", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}})
CALENDAR = "https://caldav.icloud.com/1/calendars/family/"
RESOURCE = CALENDAR + "one.ics"


def response(data=None, *, status=200, headers=None):
    body = data if isinstance(data, bytes) else json.dumps(data or {}).encode()
    return p.HTTPResponse(status, headers or {}, body)


def google_event(identifier="event", payload=None, etag='"v1"', **extra):
    return {"id": identifier, "etag": etag, "iCalUID": "family-uid", "updated": "2026-10-21T10:00:00Z",
            **(payload or PAYLOAD), **extra}


def google_page(items=(), token="cursor", **extra):
    return response({"items": list(items), "accessRole": "owner", "nextSyncToken": token, **extra})


def google_acl(role="owner", **extra):
    return response({"id": "family", "summary": "Family", "accessRole": role, **extra})


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def google(*responses):
    transport = QueueTransport(*responses)
    provider = p.GoogleProvider("family", client_id="id", client_secret="secret", refresh_token="refresh", transport=transport)
    provider._access_token = "access"
    provider._expires = float("inf")
    return provider, transport


def dav_response(content, *, href=CALENDAR, status=200, propstatus=200):
    props = f'<d:propstat><d:prop>{content}</d:prop><d:status>HTTP/1.1 {propstatus} Status</d:status></d:propstat>'
    return f'<d:response><d:href>{escape(href)}</d:href>{props}</d:response>'


def multistatus(*responses):
    return response(('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + ''.join(responses) + '</d:multistatus>').encode(), status=207)


def dav_acl(privileges=("read", "write"), *, token="one", href=CALENDAR, extra=""):
    value = ('<d:resourcetype><c:calendar/></d:resourcetype><d:displayname>Family</d:displayname>'
             '<d:current-user-privilege-set>' + ''.join(f'<d:privilege><d:{name}/></d:privilege>' for name in privileges) + '</d:current-user-privilege-set>'
             + (f'<d:sync-token>{token}</d:sync-token>' if token is not None else '') + extra)
    return multistatus(dav_response(value, href=href))


def dav_item(payload=None, *, uid="family-uid", href=RESOURCE, etag='"v1"'):
    data = codec.to_ical(payload or PAYLOAD, uid)
    return dav_response('<d:getetag>' + escape(etag) + '</d:getetag><c:calendar-data>' + escape(data.decode()) + '</c:calendar-data>', href=href)


def dav_get(payload=None, etag='"v1"', uid="family-uid"):
    return response(codec.to_ical(payload or PAYLOAD, uid), headers={"ETag": etag})


def dav(*responses, calendar_id=CALENDAR, floating_time_zone=None):
    transport = QueueTransport(*responses)
    return p.CalDAVProvider(calendar_id, username="user", password="secret", floating_time_zone=floating_time_zone, transport=transport), transport


class GoogleReadTests(unittest.TestCase):
    def test_paginated_incremental_and_stable_cursor(self):
        provider, transport = google(google_acl(), google_page([google_event()], nextPageToken="two"),
                                     google_page([google_event("deleted", status="cancelled")], token="next"), google_acl())
        snapshot = provider.pull(p.GoogleProvider.CURSOR_PREFIX + "old")
        self.assertTrue(snapshot.complete)
        self.assertFalse(snapshot.full)
        self.assertEqual(snapshot.cursor, p.GoogleProvider.CURSOR_PREFIX + "next")
        self.assertEqual([e.deleted for e in snapshot.events], [False, True])
        self.assertIn("syncToken=old", transport.calls[1][1])
        self.assertIn("pageToken=two", transport.calls[2][1])
        self.assertEqual(snapshot.events[0].uid, "family-uid")

    def test_expired_token_refetches_full_inventory(self):
        provider, _ = google(google_acl(), response(status=410), google_page([google_event()]), google_acl())
        self.assertTrue(provider.pull(p.GoogleProvider.CURSOR_PREFIX + "expired").full)

    def test_no_partial_result_on_later_page_failure_or_acl_loss(self):
        for tail in (response(status=503), google_page([], accessRole="reader"), google_page([], token="")):
            with self.subTest(tail=tail):
                provider, _ = google(google_acl(), google_page([google_event()], nextPageToken="two"), tail)
                with self.assertRaises(ProviderError):
                    provider.pull(None)
        provider, _ = google(google_acl(), google_page([google_event()]), google_acl("reader"))
        with self.assertRaises(ProviderError):
            provider.pull(None)

    def test_recurring_delta_is_reconstructed_from_full_list(self):
        master = google_event(payload={**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]})
        exception = google_event("exception", {**PAYLOAD, "summary": "Moved", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}},
                                recurringEventId="event", originalStartTime={"date": "2026-11-02"})
        provider, _ = google(google_acl(), google_page([exception]), google_page([master, exception]), google_acl())
        result = provider.pull(p.GoogleProvider.CURSOR_PREFIX + "old")
        self.assertTrue(result.full)
        self.assertEqual(result.events[0].payload["exceptions"][0]["summary"], "Moved")
        self.assertIn("hestia-bundle:", result.events[0].etag)
        self.assertEqual(p._master_etag(result.events[0].etag), '"v1"')

    def test_read_failures_are_sanitized(self):
        cases = [google_page([{"id": "bad"}]), google_page([{"summary": "missing id"}]),
                 google_page([google_event(etag=None)]), google_page([google_event(updated="nonsense")])]
        for bad in cases:
            provider, _ = google(google_acl(), bad)
            with self.subTest(bad=bad), self.assertRaises(ProviderError) as error:
                provider.pull(None)
            self.assertNotIn("secret", str(error.exception))
        provider, _ = google(google_acl(), response(status=410))
        with self.assertRaises(ProviderError):
            provider.pull(None)

    def test_incomplete_or_repeated_pages_rejected(self):
        for page in (google_page([], token=""), response({"accessRole": "owner", "items": {}})):
            provider, _ = google(page)
            with self.assertRaises(ProviderError):
                provider._list(None)
        provider, _ = google(google_page([], nextPageToken="again"), google_page([], nextPageToken="again"))
        with self.assertRaises(ProviderError):
            provider._list(None)

    def test_discovery_and_actual_roles(self):
        first = {"id": "a", "summary": "A", "accessRole": "writer", "timeZone": "Europe/Warsaw"}
        second = {"id": "b", "summaryOverride": "Private limited", "accessRole": "writerWithoutPrivateAccess"}
        provider, _ = google(response({"items": [first], "nextPageToken": "two"}), response({"items": [second]}))
        self.assertEqual([i.writable for i in provider.discover()], [True, False])
        for role in ("reader", "freeBusyReader", "writerWithoutPrivateAccess"):
            provider, _ = google(google_acl(role))
            with self.assertRaises(ProviderError) as error:
                provider._check_calendar()
            self.assertEqual(error.exception.status, 403)
        for invalid in ({}, {"items": None}):
            provider, _ = google(response(invalid))
            with self.assertRaises(ProviderError):
                provider.discover()
        provider, _ = google(response({"items": [], "nextPageToken": "two"}), response({"items": [], "nextPageToken": "two"}))
        with self.assertRaises(ProviderError):
            provider.discover()
        provider, _ = google()
        provider.calendar_id = ""
        with self.assertRaises(ProviderError):
            provider._check_calendar()
        provider, _ = google(response({}))
        with self.assertRaises(ProviderError):
            provider._check_calendar()

    def test_token_refresh_401_and_errors_do_not_include_credentials(self):
        provider, transport = google(response(status=401), response({"access_token": "new", "expires_in": 3600}), google_acl())
        self.assertTrue(provider._check_calendar().writable)
        self.assertEqual(transport.calls[2][2]["headers"]["Authorization"], "Bearer new")
        for data in ({}, {"access_token": ""}, {"access_token": "x", "expires_in": "bad"}):
            provider, _ = google(response(data))
            provider._expires = 0
            with self.assertRaises(ProviderError):
                provider._token()
        provider, transport = google(response({"access_token": "x"}))
        provider._expires = 0
        self.assertEqual(provider._token(), "x")
        self.assertIn(b"refresh_token=refresh", transport.calls[0][2]["body"])


class GoogleWriteTests(unittest.TestCase):
    def test_create_update_delete_and_crash_replay(self):
        created = google_event(extendedProperties={"private": {"hestia_uid": "uid"}})
        provider, transport = google(google_acl(), response(created), response(created))
        self.assertEqual(provider.put("event", PAYLOAD, None, create=True, uid="uid").uid, "uid")
        request = json.loads(transport.calls[1][2]["body"])
        self.assertEqual(request["id"], "event")
        self.assertNotIn("iCalUID", request)
        provider, _ = google(google_acl(), response(status=409), response(created), response(created))
        self.assertEqual(provider.put("event", PAYLOAD, None, create=True, uid="uid").payload, PAYLOAD)
        wanted = {**PAYLOAD, "summary": "New"}
        changed = google_event(payload=wanted, etag='"v2"', extendedProperties={"private": {"hestia_uid": "uid"}})
        provider, transport = google(google_acl(), response(created), response(changed), response(changed))
        self.assertEqual(provider.put("event", wanted, '"v1"', create=False, uid="uid").payload["summary"], "New")
        self.assertEqual(transport.calls[2][2]["headers"]["If-Match"], '"v1"')
        self.assertEqual(transport.calls[2][0], "PATCH")
        provider, _ = google(google_acl(), response(changed))
        self.assertEqual(provider.put("event", wanted, '"v1"', create=False, uid="uid").etag, '"v2"')
        provider, transport = google(google_acl(), response(status=204))
        provider.delete("event", '"v2"')
        self.assertEqual(transport.calls[1][2]["headers"]["If-Match"], '"v2"')
        expected_id = "h17b788a70eeccbdc2fcb2d2d3db216c02fa88ac668beeb164bb2328c864bf3f4"
        restarted, restarted_transport = google()
        self.assertEqual(provider.allocate_id("uid"), expected_id)
        self.assertEqual(restarted.allocate_id("uid"), expected_id)
        self.assertEqual(restarted_transport.calls, [])
        self.assertRegex(provider.allocate_id("uid"), "^[0-9a-v]{5,1024}$")

    def test_conflict_permission_errors_and_missing_deletion(self):
        wanted = {**PAYLOAD, "summary": "New"}
        for revision in (None, '"stale"'):
            provider, _ = google(google_acl(), response(google_event()))
            with self.assertRaises(ProviderError) as error:
                provider.put("event", wanted, revision, create=False)
            self.assertTrue(error.exception.conflict)
        for existing in (google_event(), google_event(status="cancelled", extendedProperties={"private": {"hestia_uid": "uid"}})):
            provider, _ = google(google_acl(), response(status=409), response(existing))
            with self.assertRaises(ProviderError):
                provider.put("event", PAYLOAD, None, create=True, uid="uid")
        provider, _ = google(google_acl(), response(status=503))
        with self.assertRaises(ProviderError) as error:
            provider.put("event", PAYLOAD, None, create=True)
        self.assertTrue(error.exception.retryable)
        provider, _ = google(google_acl(), response(google_event(status="cancelled")))
        with self.assertRaises(ProviderError):
            provider.put("event", PAYLOAD, '"v1"', create=False)
        for status in (404, 410):
            provider, _ = google(google_acl(), response(status=status))
            provider.delete("event", '"v1"')
        provider, _ = google(google_acl(), response(status=412))
        with self.assertRaises(ProviderError):
            provider.delete("event", '"v1"')
        provider, _ = google(google_acl(), response(google_event()))
        with self.assertRaises(ProviderError):
            provider.delete("event", None)

    def test_recurrence_override_cancel_and_reset(self):
        master_payload = codec.validate_payload({**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=4"]})
        master = google_event(payload=master_payload, extendedProperties={"private": {"hestia_uid": "uid", "other": "keep"}})
        original = {"date": "2026-11-02"}
        override = {"originalStartTime": original, **codec.default_instance(master_payload, original), "summary": "Override"}
        wanted = codec.validate_payload({**master_payload, "exceptions": [override]})
        instance = google_event("instance", payload=codec.default_instance(master_payload, original), originalStartTime=original, recurringEventId="event")
        modified = {**instance, **override, "etag": '"ex2"'}
        baseline = p._google_etag(master, [])
        provider, transport = google(google_acl(), response(master), google_page([master]), response({"items": [instance]}), response(modified), response(master), google_page([master, modified]))
        result = provider.put("event", wanted, baseline, create=False, uid="uid")
        self.assertEqual(result.payload, wanted)
        self.assertEqual(transport.calls[4][2]["headers"]["If-Match"], '"v1"')
        reset = {**instance, "etag": '"ex3"'}
        provider, transport = google(google_acl(), response(master), google_page([master, modified]), response({"items": [modified]}), response(reset), response(master), google_page([master, reset]))
        result = provider.put("event", master_payload, result.etag, create=False, uid="uid")
        self.assertEqual(result.payload, master_payload)
        self.assertEqual(json.loads(transport.calls[4][2]["body"])["summary"], "Family")
        cancelled = {"originalStartTime": original, "status": "cancelled"}
        wanted = codec.validate_payload({**master_payload, "exceptions": [cancelled]})
        current = {**instance, "status": "cancelled"}
        provider, _ = google(google_acl(), response(master), google_page([master]), response({"items": [instance]}), response(current), response(master), google_page([master, current]))
        self.assertEqual(provider.put("event", wanted, baseline, create=False, uid="uid").payload, wanted)
        for items in ([], [dict(instance, etag=None)]):
            provider, _ = google(response({"items": items}), response(dict(instance, etag=None)))
            with self.assertRaises(ProviderError):
                provider._instance("event", original)


class CalDAVReadTests(unittest.TestCase):
    def test_full_inventory_with_etags_and_cancellation(self):
        cancelled = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:deleted\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        item = dav_response('<d:getetag>"v2"</d:getetag><c:calendar-data>' + escape(cancelled.decode()) + '</c:calendar-data>', href=CALENDAR + 'deleted.ics')
        provider, transport = dav(dav_acl(), multistatus(dav_item(), item), dav_acl())
        result = provider.pull("old-token")
        self.assertTrue(result.full and result.complete)
        self.assertEqual(result.cursor, "one")
        self.assertEqual([e.deleted for e in result.events], [False, True])
        self.assertEqual(transport.calls[1][0], "REPORT")
        self.assertNotIn(b"time-range", transport.calls[1][2]["body"])

    def test_partial_listing_limits_and_changed_collection_are_never_deletions(self):
        bad = [multistatus(dav_response('<d:getetag>"v1"</d:getetag>', href=RESOURCE)),
               multistatus(dav_item(), dav_item()), multistatus(dav_response('', href=RESOURCE, propstatus=403)),
               multistatus('<d:response><d:href>' + RESOURCE + '</d:href><d:status>HTTP/1.1 507 Limited</d:status></d:response>'),
               response(b'<d:multistatus xmlns:d="DAV:"><d:error/></d:multistatus>', status=207),
               response(b'<notxml', status=207), multistatus('<d:response/>'),
               response(status=403), response(status=503)]
        for page in bad:
            provider, _ = dav(dav_acl(), page)
            with self.subTest(page=page), self.assertRaises(ProviderError):
                provider.pull(None)
        provider, _ = dav(dav_acl(), multistatus(), dav_acl(token="two"))
        with self.assertRaises(ProviderError) as error:
            provider.pull(None)
        self.assertTrue(error.exception.retryable)
        provider, _ = dav(dav_acl(), multistatus(), dav_acl(("read",)))
        with self.assertRaises(ProviderError):
            provider.pull(None)

    def test_account_discovery_and_shared_permissions(self):
        principal = multistatus(dav_response('<d:current-user-principal><d:href>/principal/</d:href></d:current-user-principal>', href="/"))
        homes = multistatus(dav_response('<c:calendar-home-set><d:href>/1/calendars/</d:href></c:calendar-home-set>'))
        writable = dav_acl().body.decode().split('<d:response>', 1)[1].split('</d:response>', 1)[0]
        readonly = dav_acl(("read",), href=CALENDAR + "shared/").body.decode().split('<d:response>', 1)[1].split('</d:response>', 1)[0]
        provider, _ = dav(principal, homes, multistatus('<d:response>' + writable + '</d:response>', '<d:response>' + readonly + '</d:response>', dav_response('<d:resourcetype/>', href='/1/calendars/')), calendar_id="")
        self.assertEqual([info.writable for info in provider.discover()], [True, False])
        provider, _ = dav(multistatus(), calendar_id="")
        with self.assertRaises(ProviderError):
            provider.discover()
        provider, _ = dav(principal, multistatus(), calendar_id="")
        with self.assertRaises(ProviderError):
            provider.discover()
        provider, _ = dav(principal, homes, multistatus('<d:response/>'), calendar_id="")
        with self.assertRaises(ProviderError):
            provider.discover()

    def test_permissions_explicit_selection_and_component_support(self):
        for privileges in (("read",), ("write",), ()):
            provider, _ = dav(dav_acl(privileges))
            with self.assertRaises(ProviderError):
                provider._check_calendar()
        for privileges in (("all",), ("read", "write-content", "bind", "unbind")):
            provider, _ = dav(dav_acl(privileges, token=None))
            self.assertIsNone(provider._check_calendar())
        provider, _ = dav(dav_acl(extra='<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>'))
        with self.assertRaises(ProviderError):
            provider._check_calendar()
        provider, _ = dav(dav_acl(extra='<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'))
        self.assertEqual(provider._check_calendar(), "one")
        for inventory in (multistatus(), dav_acl(href=CALENDAR + "other/"), multistatus(dav_response('<d:resourcetype/>'))):
            provider, _ = dav(inventory)
            with self.assertRaises(ProviderError):
                provider._check_calendar()
        provider, _ = dav(calendar_id="")
        with self.assertRaises(ProviderError):
            provider._check_calendar()
        with self.assertRaises(ProviderError):
            provider.allocate_id("uid")

    def test_redirects_only_to_trusted_https_and_never_rebind_collection(self):
        provider, transport = dav(response(status=301, headers={"Location": "https://p01-caldav.icloud.com/"}), multistatus())
        self.assertEqual(provider._request("PROPFIND", provider.url).url, "https://p01-caldav.icloud.com/")
        for location in ("https://evil.example/", "http://caldav.icloud.com/", "https://icloud.com.evil.example/"):
            provider, _ = dav(response(status=302, headers={"Location": location}))
            with self.assertRaises(ProviderError):
                provider._request("PROPFIND", provider.url)
        provider, _ = dav(response(status=302))
        with self.assertRaises(ProviderError):
            provider._request("PROPFIND", provider.url)
        provider, _ = dav(*[response(status=307, headers={"Location": "https://caldav.icloud.com/"}) for _ in range(5)])
        with self.assertRaises(ProviderError):
            provider._request("PROPFIND", provider.url)
        provider, _ = dav(response(status=308, headers={"Location": "https://p01-caldav.icloud.com/1/calendars/family/"}), dav_acl())
        with self.assertRaises(ProviderError):
            provider._check_calendar()


class CalDAVWriteTests(unittest.TestCase):
    def test_create_update_delete_idempotent_and_preserve_destination_metadata(self):
        provider, transport = dav(dav_acl(), response(status=201), dav_get(uid="uid"))
        result = provider.put(RESOURCE, PAYLOAD, None, create=True, uid="uid")
        self.assertEqual(result.uid, "uid")
        self.assertEqual(transport.calls[1][2]["headers"]["If-None-Match"], "*")
        for status in (409, 412):
            provider, _ = dav(dav_acl(), response(status=status), dav_get(uid="uid"))
            self.assertEqual(provider.put(RESOURCE, PAYLOAD, None, create=True, uid="uid").payload, PAYLOAD)
        original = codec.to_ical(PAYLOAD, "uid").replace(b'END:VEVENT', b'ATTENDEE:mailto:guest@example.com\r\nX-APPLE-TRAVEL-DURATION:PT15M\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nDESCRIPTION:Reminder\r\nEND:VALARM\r\nEND:VEVENT')
        wanted = {**PAYLOAD, "summary": "Edited"}
        provider, transport = dav(dav_acl(), response(original, headers={"ETag": '"v1"'}), response(status=204), dav_get(wanted, '"v2"', uid="uid"))
        self.assertEqual(provider.put(RESOURCE, wanted, '"v1"', create=False, uid="uid").payload, wanted)
        body = transport.calls[2][2]["body"]
        self.assertIn(b"ATTENDEE", body)
        self.assertIn(b"VALARM", body)
        self.assertIn(b"X-APPLE-TRAVEL-DURATION", body)
        self.assertEqual(transport.calls[2][2]["headers"]["If-Match"], '"v1"')
        provider, _ = dav(dav_acl(), dav_get(wanted, '"v2"', uid="uid"))
        self.assertEqual(provider.put(RESOURCE, wanted, '"old"', create=False, uid="uid").etag, '"v2"')
        for status in (204, 404, 410):
            provider, _ = dav(dav_acl(), response(status=status))
            provider.delete(RESOURCE, '"v2"')
        expected_id = CALENDAR + "h17b788a70eeccbdc2fcb2d2d3db216c02fa88ac668beeb164bb2328c864bf3f4.ics"
        restarted, restarted_transport = dav()
        self.assertEqual(provider.allocate_id("uid"), expected_id)
        self.assertEqual(restarted.allocate_id("uid"), expected_id)
        self.assertEqual(restarted_transport.calls, [])
        self.assertTrue(provider.allocate_id("uid").startswith(CALENDAR))

    def test_failed_writes_permissions_and_conflicts(self):
        wanted = {**PAYLOAD, "summary": "Changed"}
        for revision in (None, '"stale"'):
            provider, _ = dav(dav_acl(), dav_get())
            with self.assertRaises(ProviderError) as error:
                provider.put(RESOURCE, wanted, revision, create=False)
            self.assertTrue(error.exception.conflict)
        for result in (dav_get(uid="other"), dav_get(wanted, uid="uid")):
            provider, _ = dav(dav_acl(), response(status=412), result)
            with self.assertRaises(ProviderError):
                provider.put(RESOURCE, PAYLOAD, None, create=True, uid="uid")
        for create in (True, False):
            responses = [dav_acl(), response(status=503)] if create else [dav_acl(), dav_get(), response(status=412)]
            provider, _ = dav(*responses)
            with self.assertRaises(ProviderError):
                provider.put(RESOURCE, wanted, '"v1"', create=create)
        provider, _ = dav(dav_acl(), dav_get())
        with self.assertRaises(ProviderError):
            provider.delete(RESOURCE, None)
        provider, _ = dav(dav_acl(), response(status=412))
        with self.assertRaises(ProviderError):
            provider.delete(RESOURCE, '"v1"')
        cancelled = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:uid\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        provider, _ = dav(dav_acl(), response(cancelled, headers={"ETag": '"v1"'}))
        with self.assertRaises(ProviderError):
            provider.put(RESOURCE, PAYLOAD, '"v1"', create=False, uid="uid")


class DefensiveTransportTests(unittest.TestCase):
    def test_json_errors_and_google_rate_limit_classification(self):
        for raw in (b'bad json', b'[]', b'"value"'):
            with self.assertRaises(ProviderError):
                p._json(response(raw))
        for status, body, retry in ((503, b'', True), (429, b'', True), (403, b'"rateLimitExceeded"', True), (403, b'permission denied secret', False), (401, b'', False)):
            error = p._error(response(body, status=status))
            self.assertEqual(error.retryable, retry)
            self.assertNotIn("secret", str(error))
        self.assertEqual(p._timestamp(None), 0)
        for stamp in ("no", "2026-01-01T10:00:00", 12):
            with self.assertRaises(ProviderError):
                p._timestamp(stamp)
        with self.assertRaises(ProviderError):
            p._master_etag("hestia-bundle:invalid")
        with self.assertRaises(ProviderError):
            p._google_etag(google_event(payload={**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY"]}), [{"id": "ex"}])

    def test_dav_xml_and_url_validation(self):
        for url in ("http://caldav.icloud.com/", "https://user:secret@caldav.icloud.com/", "https://host/x/../y/", "https://host/?token=secret", "https://host:abc/", "/relative"):
            with self.assertRaises(ProviderError):
                p._secure_url(url)
        with self.assertRaises(ProviderError):
            p.CalDAVProvider(CALENDAR.rstrip('/'), username="u", password="p")
        provider, _ = dav()
        for resource in (CALENDAR, "https://caldav.icloud.com/other/event.ics"):
            with self.assertRaises(ProviderError):
                provider._resource(resource)
        for raw in (b'<!DOCTYPE x><x/>', b'<x/>', b'<invalid'):
            with self.assertRaises(ProviderError):
                p._xml(raw)
        for raw in ('<d:response xmlns:d="DAV:"/>', '<d:response xmlns:d="DAV:"><d:status>invalid</d:status></d:response>'):
            with self.assertRaises(ProviderError):
                p._status(ET.fromstring(raw))
        with self.assertRaises(ProviderError):
            p._properties(ET.fromstring('<d:response xmlns:d="DAV:"><d:propstat><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'))
        self.assertEqual(p._properties(ET.fromstring('<d:response xmlns:d="DAV:"><d:propstat><d:status>HTTP/1.1 404 No</d:status></d:propstat></d:response>')), {})
        missing_privileges = {f'{{{p.DAV}}}resourcetype': ET.fromstring('<d:resourcetype xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><c:calendar/></d:resourcetype>')}
        self.assertFalse(p.CalDAVProvider._info(CALENDAR, missing_privileges).writable)

    def test_invalid_calendar_data_is_not_an_empty_inventory(self):
        for raw in (b'bad', b'BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n', b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:No UID\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n', codec.to_ical(PAYLOAD, 'uid').replace(b'END:VEVENT', b'DTSTAMP:20260101\r\nEND:VEVENT')):
            with self.assertRaises(ProviderError):
                p._ical_record(RESOURCE, raw, '"v1"')
        with self.assertRaises(ProviderError):
            p._ical_record(RESOURCE, codec.to_ical(PAYLOAD, 'uid'), None)

    def test_production_transport_without_network(self):
        raw = BytesIO(b'data')
        raw.code = 200
        raw.headers = {"ETag": '"v1"'}
        opener = Mock()
        opener.open.return_value = raw
        with patch.object(p, 'build_opener', return_value=opener):
            result = p.HTTPTransport().request("GET", "https://host/", headers={})
        self.assertEqual(result.body, b'data')
        self.assertEqual(result.header('etag'), '"v1"')
        self.assertIsNone(result.header('missing'))
        self.assertIsNone(p._NoRedirect().redirect_request(None, None, 301, '', {}, 'https://host/'))
        for exc in (URLError('secret'), OSError('secret')):
            opener.open.side_effect = exc
            with patch.object(p, 'build_opener', return_value=opener), self.assertRaises(ProviderError) as error:
                p.HTTPTransport().request("GET", "https://host/", headers={})
            self.assertNotIn('secret', str(error.exception))
        error_response = HTTPError('https://host/', 403, 'secret', {}, BytesIO(b'denied'))
        opener.open.side_effect = error_response
        with patch.object(p, 'build_opener', return_value=opener):
            self.assertEqual(p.HTTPTransport().request("GET", "https://host/", headers={}).status, 403)
        raw = Mock()
        raw.__enter__ = Mock(return_value=raw)
        raw.__exit__ = Mock(return_value=False)
        raw.read.side_effect = p.http.client.IncompleteRead(b'partial')
        opener.open.side_effect = None
        opener.open.return_value = raw
        with patch.object(p, 'build_opener', return_value=opener), self.assertRaises(ProviderError):
            p.HTTPTransport().request("GET", "https://host/", headers={})
        raw.read.side_effect = None
        raw.read.return_value = b'x' * (p.MAX_RESPONSE + 1)
        with patch.object(p, 'build_opener', return_value=opener), self.assertRaises(ProviderError):
            p.HTTPTransport().request("GET", "https://host/", headers={})


class RecoveryAndRaceTests(unittest.TestCase):
    def test_unconfirmed_create_delete_recovers_by_identity(self):
        for status in (404, 410):
            provider, _ = google(google_acl(), response(status=status))
            provider.delete("event", None, uid="uid")
            provider, _ = dav(dav_acl(), response(status=status))
            provider.delete(RESOURCE, None, uid="uid")
        for factory, acl, identifier, current in (
                (google, google_acl, "event", response(google_event(extendedProperties={"private": {"hestia_uid": "uid"}}))),
                (dav, dav_acl, RESOURCE, dav_get(uid="uid"))):
            provider, transport = factory(acl(), current, response(status=204))
            provider.delete(identifier, None, uid="uid")
            self.assertEqual(transport.calls[-1][2]["headers"]["If-Match"], '\"v1\"')
            provider, _ = factory(acl(), current)
            with self.assertRaises(ProviderError):
                provider.delete(identifier, None, uid="different")
            provider, _ = factory(acl(), response(status=503))
            with self.assertRaises(ProviderError) as error:
                provider.delete(identifier, None, uid="uid")
            self.assertTrue(error.exception.retryable)
        provider, _ = google(google_acl(), response({"id": "event", "status": "cancelled"}))
        provider.delete("event", None, uid="uid")
        cancelled = b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:uid\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
        provider, _ = dav(dav_acl(), response(cancelled, headers={"ETag": '\"v1\"'}))
        provider.delete(RESOURCE, None, uid="uid")

    def test_documented_minimal_google_tombstones_and_header_revisions(self):
        master = google_event(payload={**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]})
        exception = {"id": "occurrence", "recurringEventId": "event", "originalStartTime": {"date": "2026-11-02"}, "status": "cancelled"}
        provider, _ = google(google_acl(), google_page([master, exception, {"id": "gone", "status": "cancelled"}]), google_acl())
        result = provider.pull(None)
        self.assertTrue(result.events[1].deleted)
        self.assertEqual(result.events[1].uid, "google-deleted:gone")
        self.assertIn("cancelled:", result.events[0].etag)
        provider, _ = google(response({"items": [exception]}), response(exception, headers={"ETag": '\"instance-etag\"'}))
        self.assertEqual(provider._instance("event", exception["originalStartTime"])["etag"], '\"instance-etag\"')
        provider, _ = google(google_acl(), google_page([google_event(iCalUID=None)]))
        with self.assertRaises(ProviderError):
            provider.pull(None)

    def test_master_edit_keeps_unchanged_exception_and_unrelated_properties(self):
        base = codec.validate_payload({**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]})
        original = {"date": "2026-11-02"}
        override = {"originalStartTime": original, **codec.default_instance(base, original), "summary": "Override"}
        before = codec.validate_payload({**base, "exceptions": [override]})
        wanted = codec.validate_payload({**before, "summary": "Edited master"})
        master = google_event(payload=base, extendedProperties={"private": {"other": "keep", "hestia_uid": "uid"}})
        exception = google_event("exception", override, recurringEventId="event")
        changed = google_event(payload={**base, "summary": "Edited master"}, etag='\"v2\"', extendedProperties={"private": {"other": "keep", "hestia_uid": "uid"}})
        provider, transport = google(google_acl(), response(master), google_page([master, exception]), response(changed), response(changed), google_page([changed, exception]))
        result = provider.put("event", wanted, p._google_etag(master, [exception]), create=False, uid="uid")
        self.assertEqual(result.payload, wanted)
        self.assertEqual(json.loads(transport.calls[3][2]["body"])["extendedProperties"]["private"]["other"], "keep")

    def test_write_race_does_not_acknowledge_wrong_content(self):
        before = google_event(extendedProperties={"private": {"hestia_uid": "uid"}})
        wanted = {**PAYLOAD, "summary": "Intended"}
        provider, _ = google(google_acl(), response(before), response(before), response(before))
        with self.assertRaises(ProviderError) as error:
            provider.put("event", wanted, '\"v1\"', create=False, uid="uid")
        self.assertTrue(error.exception.conflict)
        provider, _ = dav(dav_acl(), response(status=201), dav_get(uid="uid"))
        with self.assertRaises(ProviderError):
            provider.put(RESOURCE, wanted, None, create=True, uid="uid")

    def test_naive_modification_time_and_new_ical_exception_metadata(self):
        import re
        raw = re.sub(b'DTSTAMP:[^\r\n]+', b'DTSTAMP:20260101T100000', codec.to_ical(PAYLOAD, "uid"))
        with self.assertRaises(ProviderError):
            p._ical_record(RESOURCE, raw, '\"v1\"')
        base = {**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]}
        original = {"date": "2026-11-02"}
        override = {"originalStartTime": original, **codec.default_instance(codec.validate_payload(base), original), "summary": "Override"}
        merged = p._merge_ical(codec.to_ical(base, "uid"), codec.to_ical({**base, "exceptions": [override]}, "uid"))
        self.assertEqual(codec.from_ical(merged)[1]["exceptions"][0]["summary"], "Override")
        timed = codec.validate_payload({"summary": "Timed", "start": {"dateTime": "2026-11-01T09:00:00+01:00", "timeZone": "Europe/Warsaw"}, "end": {"dateTime": "2026-11-01T10:00:00+01:00", "timeZone": "Europe/Warsaw"}, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]})
        original = {"dateTime": "2026-11-02T09:00:00+01:00", "timeZone": "Europe/Warsaw"}
        override = {"originalStartTime": original, **codec.default_instance(timed, original), "summary": "Override"}
        generated = codec.to_ical({**timed, "exceptions": [override]}, "uid")
        old = generated.replace(b'RECURRENCE-ID;TZID=Europe/Warsaw:20261102T090000', b'RECURRENCE-ID:20261102T080000Z')
        old = old.replace(b'SUMMARY:Override', b'SUMMARY:Override\r\nX-DESTINATION:preserved')
        self.assertIn(b'X-DESTINATION:preserved', p._merge_ical(old, generated))


class RecurrenceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.base = codec.validate_payload({**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=4"]})
        self.master = google_event(payload=self.base, extendedProperties={"private": {"hestia_uid": "uid"}})
        self.original = {"date": "2026-11-02"}
        self.override = {"originalStartTime": self.original, **codec.default_instance(self.base, self.original), "summary": "Override"}
        self.raw = google_event("exception", self.override, recurringEventId="event")
        self.baseline = p._google_etag(self.master, [self.raw])

    def test_convert_series_to_one_off_does_not_reset_removed_occurrences(self):
        single = google_event(payload=PAYLOAD, etag='\"v2\"', extendedProperties={"private": {"hestia_uid": "uid"}})
        provider, transport = google(google_acl(), response(self.master), google_page([self.master, self.raw]), response(single), response(single))
        self.assertEqual(provider.put("event", PAYLOAD, self.baseline, create=False, uid="uid").payload, PAYLOAD)
        self.assertEqual([call[0] for call in transport.calls], ["GET", "GET", "GET", "PATCH", "GET"])

    def test_shorter_rule_can_remove_an_existing_override(self):
        wanted = codec.validate_payload({**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"]})
        updated = google_event(payload=wanted, etag='\"v2\"', extendedProperties={"private": {"hestia_uid": "uid"}})
        provider, _ = google(google_acl(), response(self.master), google_page([self.master, self.raw]), response(updated), response({"items": []}), response(updated), google_page([updated]))
        self.assertEqual(provider.put("event", wanted, self.baseline, create=False, uid="uid").payload, wanted)

    def test_concurrent_override_edit_is_not_overwritten(self):
        wanted = codec.validate_payload({**self.base, "exceptions": [{**self.override, "summary": "Intended"}]})
        concurrent = {**self.raw, "summary": "Concurrent", "etag": '\"v2\"'}
        # Covers both editing an existing override and a user creating one after
        # the complete inventory was fetched.
        for existing in ([self.raw], []):
            provider, transport = google(google_acl(), response(self.master), google_page([self.master, *existing]), response({"items": [concurrent]}))
            with self.assertRaises(ProviderError) as error:
                provider.put("event", wanted, p._google_etag(self.master, existing), create=False, uid="uid")
            self.assertTrue(error.exception.conflict)
            self.assertFalse(any(call[0] == "PATCH" for call in transport.calls))

    def test_already_applied_override_is_safe_to_resume(self):
        wanted = codec.validate_payload({**self.base, "exceptions": [self.override]})
        provider, transport = google(google_acl(), response(self.master), google_page([self.master]), response({"items": [self.raw]}), response(self.master), google_page([self.master, self.raw]))
        self.assertEqual(provider.put("event", wanted, p._google_etag(self.master, []), create=False, uid="uid").payload, wanted)
        self.assertFalse(any(call[0] == "PATCH" for call in transport.calls))

    def test_cancelled_override_without_etag_can_be_reset_after_exact_get(self):
        cancelled = {"id": "exception", "recurringEventId": "event", "originalStartTime": self.original, "status": "cancelled"}
        default = google_event("exception", {"originalStartTime": self.original, **codec.default_instance(self.base, self.original)}, recurringEventId="event")
        provider, _ = google(google_acl(), response(self.master), google_page([self.master, cancelled]), response({"items": [cancelled]}), response(cancelled, headers={"ETag": '\"actual\"'}), response(default), response(self.master), google_page([self.master, default]))
        self.assertEqual(provider.put("event", self.base, p._google_etag(self.master, [cancelled]), create=False, uid="uid").payload, self.base)

    def test_malformed_cancelled_resource_with_mixed_uids_is_not_a_tombstone(self):
        raw = (b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:one\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\n'
               b'BEGIN:VEVENT\r\nUID:two\r\nRECURRENCE-ID;VALUE=DATE:20261102\r\nSTATUS:CANCELLED\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')
        with self.assertRaises(ProviderError):
            p._ical_record(RESOURCE, raw, '\"v1\"')


class DetachedGoogleOccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.child = google_event("detached", recurringEventId="missing-series", originalStartTime={"date": "2026-11-01"})

    def test_live_orphans_are_preserved_as_distinct_mapped_one_offs(self):
        sibling = {**self.child, "id": "other-detached"}
        cancelled = {"id": "deleted-detached", "status": "cancelled", "recurringEventId": "missing-series", "originalStartTime": {"date": "2026-11-03"}}
        regular = google_event("regular")  # Same original UID must not collide.
        provider, transport = google(google_acl(), google_page([regular, self.child, sibling, cancelled]), response(status=404), google_acl())
        result = provider.pull(None)
        self.assertTrue(result.complete and result.full)
        self.assertEqual(len(result.events), 4)
        self.assertEqual(len({event.uid for event in result.events}), 4)
        detached = result.events[1]
        self.assertEqual(detached.remote_id, "detached")
        self.assertEqual(detached.payload, PAYLOAD)
        self.assertTrue(detached.uid.startswith("hestia-google-detached:"))
        self.assertNotIn("recurrence", detached.payload)
        self.assertTrue(result.events[-1].deleted)
        self.assertEqual(sum("missing-series" in call[1] for call in transport.calls), 1)
        self.assertIn("showHiddenInvitations=true", transport.calls[1][1])
        provider, _ = google(response(self.child))
        self.assertEqual(provider._current("detached")[0].uid, detached.uid)

    def test_missing_or_cancelled_parent_is_verified_without_inventing_series(self):
        for parent_response in (response(status=404), response(status=410), response({"id": "missing-series", "status": "cancelled"})):
            provider, _ = google(google_acl(), google_page([self.child]), parent_response, google_acl())
            result = provider.pull(None)
            self.assertEqual(result.events[-1].payload, PAYLOAD)
            self.assertFalse(result.events[-1].deleted)
            if parent_response.status == 200:
                self.assertTrue(result.events[0].deleted)
            else:
                self.assertEqual(len(result.events), 1)  # No fabricated parent tombstone.
        parent = google_event("missing-series", status="cancelled")
        provider, _ = google(google_acl(), google_page([parent, self.child]), response(parent), google_acl())
        result = provider.pull(None)
        self.assertTrue(result.events[0].deleted)
        self.assertFalse(result.events[1].deleted)
        self.assertNotEqual(result.events[0].uid, result.events[1].uid)

    def test_live_missing_parent_network_or_permission_errors_abort_inventory(self):
        for parent_response in (response(google_event("missing-series", payload={**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY"]})),
                                response({}), response({"id": "wrong", "status": "cancelled"}),
                                response(status=403), response(status=503), ProviderError("Network failed", retryable=True)):
            provider, _ = google(google_acl(), google_page([self.child]), parent_response)
            with self.subTest(status=getattr(parent_response, "status", None)), self.assertRaises(ProviderError):
                provider.pull(None)

    def test_detached_update_keeps_identity_and_targets_only_original_instance(self):
        before = {**self.child, "extendedProperties": {"private": {"hestia_uid": "parent-logical-uid", "unrelated": "keep"}}}
        provider, _ = google()
        uid = provider._record(before).uid
        self.assertNotEqual(uid, "parent-logical-uid")
        wanted = {**PAYLOAD, "summary": "Edited detached occurrence"}
        after = {**before, **wanted, "etag": '\"v2\"', "extendedProperties": {"private": {"hestia_uid": uid, "hestia_detached_instance": "detached", "unrelated": "keep"}}}
        provider, transport = google(google_acl(), response(before), response(after), response(after))
        result = provider.put("detached", wanted, '\"v1\"', create=False, uid=uid)
        self.assertEqual(result.uid, uid)
        self.assertEqual(result.payload, wanted)
        body = json.loads(transport.calls[2][2]["body"])
        self.assertNotIn("recurrence", body)
        self.assertNotIn("recurringEventId", body)
        self.assertEqual(body["extendedProperties"]["private"]["hestia_detached_instance"], "detached")
        self.assertEqual(body["extendedProperties"]["private"]["unrelated"], "keep")
        self.assertTrue(transport.calls[2][1].split("?")[0].endswith("/events/detached"))
        provider, transport = google(google_acl(), response(after), response(status=204))
        provider.delete("detached", None, uid=uid)
        self.assertTrue(transport.calls[-1][1].split("?")[0].endswith("/events/detached"))
        self.assertEqual(transport.calls[-1][2]["headers"]["If-Match"], '\"v2\"')

    def test_invalid_detached_content_never_becomes_complete_snapshot(self):
        for child in (dict(self.child, iCalUID=None), dict(self.child, recurrence=["RRULE:FREQ=DAILY"])):
            provider, _ = google(google_acl(), google_page([child]), response(status=404))
            with self.assertRaises(ProviderError):
                provider.pull(None)

    def test_old_query_cursors_trigger_safe_full_rescan(self):
        for old_cursor in ("old-unversioned-token", p.GoogleProvider.CURSOR_PREFIX):
            provider, transport = google(google_acl(), google_page([]), google_acl())
            result = provider.pull(old_cursor)
            self.assertTrue(result.full)
            self.assertNotIn("syncToken=", transport.calls[1][1])
            self.assertEqual(result.cursor, p.GoogleProvider.CURSOR_PREFIX + "cursor")


class GoogleSpecialEventTests(unittest.TestCase):
    def setUp(self):
        self.birthday_payload = codec.validate_payload({**PAYLOAD, "recurrence": ["RRULE:FREQ=YEARLY"], "visibility": "private", "transparency": "transparent"})
        self.birthday = google_event(payload=self.birthday_payload, eventType="birthday", birthdayProperties={"type": "birthday"})

    def test_birthday_summary_and_independent_date_are_patched_without_other_fields(self):
        for changes in ({"summary": "Edited birthday"}, {"start": {"date": "2026-11-02"}, "end": {"date": "2026-11-03"}}):
            wanted = codec.validate_payload({**self.birthday_payload, **changes})
            after = {**self.birthday, **wanted, "etag": '\"v2\"'}
            provider, transport = google(google_acl(), response(self.birthday), google_page([self.birthday]), response(after), response(after), google_page([after]))
            self.assertEqual(provider.put("event", wanted, p._google_etag(self.birthday, []), create=False, uid="family-uid").payload, wanted)
            self.assertEqual(json.loads(transport.calls[3][2]["body"]), {"eventType": "birthday", **changes})
            self.assertEqual(transport.calls[3][2]["headers"]["If-Match"], '\"v1\"')

    def test_from_gmail_only_writes_changed_allowed_metadata(self):
        for before_payload, wanted, expected in (
                (PAYLOAD, {**PAYLOAD, "visibility": "private"}, {"visibility": "private"}),
                ({**PAYLOAD, "visibility": "private", "status": "tentative"}, PAYLOAD, {"visibility": "default", "status": "confirmed"})):
            before = google_event(payload=before_payload, eventType="fromGmail")
            after = google_event(payload=wanted, eventType="fromGmail", etag='\"v2\"')
            provider, transport = google(google_acl(), response(before), response(after), response(after))
            result = provider.put("event", codec.validate_payload(wanted), '\"v1\"', create=False, uid="family-uid")
            self.assertEqual(result.payload, codec.validate_payload(wanted))
            self.assertEqual(json.loads(transport.calls[2][2]["body"]), {"eventType": "fromGmail", **expected})

    def test_forbidden_special_event_edits_fail_before_any_remote_write(self):
        cases = []
        for change in ({"description": "Changed"}, {"visibility": "public"}, {"recurrence": ["RRULE:FREQ=YEARLY;COUNT=4"]},
                       {"exceptions": [{"originalStartTime": {"date": "2027-11-01"}, "status": "cancelled"}]}):
            cases.append((self.birthday, codec.validate_payload({**self.birthday_payload, **change}), "family-uid"))
        for properties in ({"contact": "people/contact"}, {"type": "self"}, {}):
            birthday = {**self.birthday, "birthdayProperties": properties}
            change = {"start": {"date": "2026-11-02"}, "end": {"date": "2026-11-03" if properties else "2026-11-04"}}
            cases.append((birthday, codec.validate_payload({**self.birthday_payload, **change}), "family-uid"))
        timed = {"start": {"dateTime": "2026-11-01T09:00:00+00:00", "timeZone": "UTC"}, "end": {"dateTime": "2026-11-01T10:00:00+00:00", "timeZone": "UTC"}}
        cases.append((self.birthday, codec.validate_payload({**self.birthday_payload, **timed}), "family-uid"))
        gmail = google_event(eventType="fromGmail")
        cases.extend([(gmail, {**PAYLOAD, "summary": "Cannot edit Gmail content"}, "family-uid"), (gmail, PAYLOAD, "changed-uid")])
        for before, wanted, uid in cases:
            extra = [google_page([before])] if before.get("recurrence") else []
            provider, transport = google(google_acl(), response(before), *extra)
            with self.subTest(event_type=before["eventType"], wanted=wanted), self.assertRaises(ProviderError) as error:
                provider.put("event", wanted, p._google_etag(before, []), create=False, uid=uid)
            self.assertEqual(error.exception.code, "google_event_type_restricted")
            self.assertFalse(any(call[0] in {"POST", "PATCH", "DELETE"} for call in transport.calls))


class UnknownEndProviderTests(unittest.TestCase):
    def setUp(self):
        self.start = {"dateTime": "2026-11-01T09:00:00+00:00", "timeZone": "UTC"}
        self.unknown = codec.validate_payload({"summary": "Unknown end", "start": self.start, "end": self.start, "endTimeUnspecified": True})
        self.known = codec.validate_payload({"summary": "Unknown end", "start": self.start, "end": {"dateTime": "2026-11-01T10:00:00+00:00", "timeZone": "UTC"}})

    def test_readonly_unknown_end_uses_google_private_marker_and_can_be_cleared(self):
        copied = google_event(payload={key: value for key, value in self.unknown.items() if key != "endTimeUnspecified"}, extendedProperties={"private": {"hestia_uid": "uid", "hestia_end_unspecified": "true"}})
        provider, transport = google(google_acl(), response(copied), response(copied))
        self.assertEqual(provider.put("event", self.unknown, None, create=True, uid="uid").payload, self.unknown)
        body = json.loads(transport.calls[1][2]["body"])
        self.assertNotIn("endTimeUnspecified", body)
        self.assertEqual(body["extendedProperties"]["private"]["hestia_end_unspecified"], "true")
        changed = google_event(payload=self.known, etag='\"v2\"', extendedProperties={"private": {"hestia_uid": "uid", "hestia_end_unspecified": "false"}})
        provider, transport = google(google_acl(), response(copied), response(changed), response(changed))
        self.assertEqual(provider.put("event", self.known, '\"v1\"', create=False, uid="uid").payload, self.known)
        self.assertEqual(json.loads(transport.calls[2][2]["body"])["extendedProperties"]["private"]["hestia_end_unspecified"], "false")
        externally_edited = {**changed, "extendedProperties": copied["extendedProperties"]}
        self.assertEqual(provider._record(externally_edited).payload, self.known)

    def test_ical_explicit_end_removes_old_unknown_marker(self):
        old = codec.to_ical(self.unknown, "uid")
        self.assertIn(b"X-HESTIA-END-UNSPECIFIED:TRUE", old)
        generated = p._merge_ical(old, codec.to_ical(self.known, "uid"))
        self.assertNotIn(b"X-HESTIA-END-UNSPECIFIED", generated)
        self.assertEqual(codec.from_ical(generated)[1], self.known)

    def test_unknown_end_exception_uses_marker_without_readonly_field(self):
        base = codec.validate_payload({**self.known, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]})
        original = {"dateTime": "2026-11-02T09:00:00+00:00", "timeZone": "UTC"}
        override = {"originalStartTime": original, "summary": "Unknown end", "start": original, "end": original, "endTimeUnspecified": True}
        wanted = codec.validate_payload({**base, "exceptions": [override]})
        master = google_event(payload=base, extendedProperties={"private": {"hestia_uid": "uid"}})
        instance = google_event("instance", codec.default_instance(base, original), originalStartTime=original, recurringEventId="event")
        changed = {**instance, **{key: value for key, value in override.items() if key != "endTimeUnspecified"}, "etag": '\"v2\"', "extendedProperties": {"private": {"hestia_end_unspecified": "true"}}}
        provider, transport = google(google_acl(), response(master), google_page([master]), response({"items": [instance]}), response(changed), response(master), google_page([master, changed]))
        self.assertEqual(provider.put("event", wanted, p._google_etag(master, []), create=False, uid="uid").payload, wanted)
        body = json.loads(transport.calls[4][2]["body"])
        self.assertNotIn("endTimeUnspecified", body)
        self.assertEqual(body["extendedProperties"]["private"]["hestia_end_unspecified"], "true")


class CalDAVDefaultPortIdentityTests(unittest.TestCase):
    def test_report_default_port_aliases_keep_configured_origin_and_stable_ids(self):
        explicit = CALENDAR.replace("caldav.icloud.com/", "caldav.icloud.com:443/")
        for selected, alias in ((explicit, CALENDAR), (CALENDAR, explicit)):
            with self.subTest(selected=selected):
                provider, transport = dav(dav_acl(href=alias), multistatus(dav_item(href=alias + "one.ics")), dav_acl(href=alias),
                                          dav_acl(href=selected), multistatus(dav_item(href=selected + "one.ics")), dav_acl(href=selected),
                                          calendar_id=selected)
                first = provider.pull(None)
                second = provider.pull(first.cursor)
                expected_id = selected + "one.ics"
                self.assertEqual(first.events[0].remote_id, expected_id)
                self.assertEqual(second.events[0].remote_id, expected_id)
                self.assertEqual(first.events[0].uid, second.events[0].uid)
                self.assertEqual(first.events[0].payload, PAYLOAD)
                self.assertEqual(provider.calendar_id, selected)
                self.assertTrue(all(call[1] == selected for call in transport.calls))
                self.assertTrue(provider.allocate_id("uid").startswith(selected))

    def test_default_port_only_redirect_is_accepted_but_collection_moves_are_rejected(self):
        explicit = CALENDAR.replace("caldav.icloud.com/", "caldav.icloud.com:443/")
        for selected, alias in ((explicit, CALENDAR), (CALENDAR, explicit)):
            provider, _ = dav(response(status=301, headers={"Location": alias}), dav_acl(href=alias), calendar_id=selected)
            self.assertEqual(provider._check_calendar(), "one")
        for changed in (CALENDAR.replace("caldav.icloud.com/", "p01-caldav.icloud.com/"), CALENDAR + "other/"):
            provider, _ = dav(response(status=301, headers={"Location": changed}), dav_acl(href=changed))
            with self.assertRaises(ProviderError):
                provider._check_calendar()
        provider, _ = dav(multistatus(dav_response('<d:resourcetype><c:calendar/></d:resourcetype>', href=CALENDAR + "other/")))
        with self.assertRaises(ProviderError):
            provider._check_calendar()
        provider, _ = dav(multistatus('<d:response><d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'))
        with self.assertRaises(ProviderError):
            provider._check_calendar()

    def test_equivalent_origin_never_relaxes_collection_path_or_credentials_boundary(self):
        explicit = CALENDAR.replace("caldav.icloud.com/", "caldav.icloud.com:443/")
        provider, _ = dav(calendar_id=explicit)
        self.assertEqual(provider._resource(CALENDAR + "one.ics"), explicit + "one.ics")
        for remote_id in (CALENDAR, CALENDAR.replace("family/", "family-other/") + "one.ics",
                          CALENDAR.replace("caldav.icloud.com/", "p01-caldav.icloud.com/") + "one.ics",
                          CALENDAR.replace("caldav.icloud.com/", "caldav.icloud.com:444/") + "one.ics",
                          CALENDAR + "%2e%2e/other/one.ics", CALENDAR + "one.ics?secret=value",
                          CALENDAR.replace("https://", "https://user:secret@") + "one.ics"):
            with self.subTest(remote_id=remote_id), self.assertRaises(ProviderError):
                provider._resource(remote_id)

    def test_existing_get_mapping_identity_is_not_rewritten(self):
        explicit = CALENDAR.replace("caldav.icloud.com/", "caldav.icloud.com:443/")
        provider, transport = dav(dav_get(), calendar_id=explicit)
        event, _ = provider._get(RESOURCE)
        self.assertEqual(event.remote_id, RESOURCE)
        self.assertEqual(transport.calls[0][1], explicit + "one.ics")
        self.assertEqual(provider.calendar_id, explicit)


class CalDAVCollectionEchoTests(unittest.TestCase):
    @staticmethod
    def echo(*, good_status=200, missing_status=404, good_prop='<d:getetag>"collection-v1"</d:getetag>',
             missing_prop='<c:calendar-data/>', direct="", href="/1/calendars/family/"):
        return (f'<d:response><d:href>{escape(href)}</d:href>{direct}'
                f'<d:propstat><d:prop>{good_prop}</d:prop><d:status>HTTP/1.1 {good_status} Status</d:status></d:propstat>'
                f'<d:propstat><d:prop>{missing_prop}</d:prop><d:status>HTTP/1.1 {missing_status} Status</d:status></d:propstat>'
                '</d:response>')

    def test_actual_icloud_collection_metadata_echo_is_not_an_event_or_deletion(self):
        explicit = CALENDAR.replace("caldav.icloud.com/", "caldav.icloud.com:443/")
        provider, transport = dav(dav_acl(href=explicit), multistatus(self.echo(), dav_item(href=explicit + "one.ics")),
                                  dav_acl(href=explicit), calendar_id=explicit)
        snapshot = provider.pull(None)
        self.assertTrue(snapshot.complete and snapshot.full)
        self.assertEqual(len(snapshot.events), 1)
        self.assertEqual(snapshot.events[0].remote_id, explicit + "one.ics")
        self.assertFalse(snapshot.events[0].deleted)
        self.assertEqual(snapshot.events[0].payload, PAYLOAD)
        self.assertEqual(len(transport.calls), 3)  # Both collection checks remain mandatory.

    def test_collection_errors_or_unexpected_shape_abort_full_inventory(self):
        bad_entries = [self.echo(good_status=403), self.echo(good_status=500), self.echo(missing_status=403),
                       self.echo(missing_status=200), self.echo(good_prop='<d:displayname>Unexpected</d:displayname>'),
                       self.echo(missing_prop='<d:getetag/>'), self.echo(good_prop='<d:getetag/>'),
                       self.echo(missing_prop='<c:calendar-data>BEGIN:VCALENDAR</c:calendar-data>'),
                       self.echo(good_prop='<d:getetag><d:extra/></d:getetag>'),
                       self.echo(missing_prop='<c:calendar-data><c:extra/></c:calendar-data>'),
                       '<d:response><d:href>/1/calendars/family/</d:href></d:response>',
                       '<d:response><d:href>/1/calendars/family/</d:href><d:propstat><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                       '<d:propstat><d:prop/><d:status>HTTP/1.1 404 Missing</d:status></d:propstat></d:response>']
        bad_entries.extend(self.echo(direct=f'<d:status>HTTP/1.1 {status} Status</d:status>') for status in (200, 401, 403, 507))
        for entry in bad_entries:
            provider, transport = dav(dav_acl(), multistatus(dav_item(), entry))
            with self.subTest(entry=entry), self.assertRaises(ProviderError):
                provider.pull(None)
            self.assertEqual(len(transport.calls), 2)  # No completed inventory/cursor is returned.

    def test_child_calendar_data_404_is_not_ignored_with_benign_self_echo(self):
        missing_child = dav_response('<c:calendar-data/>', href=CALENDAR + "missing.ics", propstatus=404)
        provider, _ = dav(dav_acl(), multistatus(self.echo(), dav_item(), missing_child))
        with self.assertRaises(ProviderError):
            provider.pull(None)
        # The metadata shape is permitted only for the exact verified collection,
        # never for a child event, a sibling collection, or another origin.
        for href in (RESOURCE, CALENDAR + "sibling/", CALENDAR.replace("caldav.icloud.com", "p01-caldav.icloud.com")):
            provider, _ = dav(dav_acl(), multistatus(self.echo(href=href)))
            with self.subTest(href=href), self.assertRaises(ProviderError):
                provider.pull(None)


class CalDAVFloatingTimePolicyTests(unittest.TestCase):
    @staticmethod
    def floating_ical(*, start="20260115T120000", end="20260115T130000", summary="Synthetic floating", tzid=None):
        parameter = ";TZID=" + tzid if tzid else ""
        return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:floating@synthetic.test\r\n'
                'DTSTAMP:20260101T000000Z\r\nSUMMARY:' + summary + '\r\nDTSTART' + parameter + ':' + start
                + '\r\nDTEND' + parameter + ':' + end + '\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n').encode()

    @staticmethod
    def expected(summary="Synthetic floating"):
        return codec.validate_payload({"summary": summary,
            "start": {"dateTime": "2026-01-15T12:00:00+01:00", "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": "2026-01-15T13:00:00+01:00", "timeZone": "Europe/Warsaw"}})

    def test_explicit_policy_is_used_for_full_pull_and_get_without_changing_identity(self):
        raw = self.floating_ical()
        item = dav_response('<d:getetag>"floating-v1"</d:getetag><c:calendar-data>' + escape(raw.decode()) + '</c:calendar-data>', href=RESOURCE)
        provider, _ = dav(dav_acl(), multistatus(item), dav_acl(), response(raw, headers={"ETag": '\"floating-v1\"'}),
                          floating_time_zone="Europe/Warsaw")
        snapshot = provider.pull(None)
        current, _ = provider._get(RESOURCE)
        self.assertEqual(snapshot.events[0], current)
        self.assertEqual(current.payload, self.expected())
        self.assertEqual((current.remote_id, current.uid), (RESOURCE, "floating@synthetic.test"))
        self.assertTrue(snapshot.full and snapshot.complete)
        self.assertEqual(provider.floating_time_zone, "Europe/Warsaw")

    def test_default_policy_and_invalid_or_ambiguous_wall_times_stay_rejected(self):
        cases = [(self.floating_ical(), None), (self.floating_ical(), "No/Such_Zone"),
                 (self.floating_ical(start="20261025T023000", end="20261025T033000"), "Europe/Warsaw"),
                 (self.floating_ical(start="20260329T023000", end="20260329T033000"), "Europe/Warsaw"),
                 (self.floating_ical(tzid="No/Such_Zone"), "Europe/Warsaw")]
        for raw, policy in cases:
            provider, _ = dav(response(raw, headers={"ETag": '\"floating-v1\"'}), floating_time_zone=policy)
            with self.subTest(policy=policy, raw=raw), self.assertRaises(ProviderError):
                provider._get(RESOURCE)

    def test_create_and_update_verify_returned_floating_times_with_the_same_policy(self):
        original = self.floating_ical()
        provider, transport = dav(dav_acl(), response(status=201), response(original, headers={"ETag": '\"floating-v1\"'}),
                                  floating_time_zone="Europe/Warsaw")
        created = provider.put(RESOURCE, self.expected(), None, create=True, uid="floating@synthetic.test")
        self.assertEqual(created.payload, self.expected())
        self.assertIn(b"DTSTART;TZID=Europe/Warsaw:20260115T120000", transport.calls[1][2]["body"])
        old = original.replace(b'END:VEVENT', b'X-DESTINATION:preserved\r\nEND:VEVENT')
        changed = self.floating_ical(summary="Synthetic changed")
        provider, transport = dav(dav_acl(), response(old, headers={"ETag": '\"floating-v1\"'}), response(status=204),
                                  response(changed, headers={"ETag": '\"floating-v2\"'}), floating_time_zone="Europe/Warsaw")
        updated = provider.put(RESOURCE, self.expected("Synthetic changed"), '\"floating-v1\"', create=False, uid="floating@synthetic.test")
        self.assertEqual(updated.payload, self.expected("Synthetic changed"))
        self.assertIn(b"X-DESTINATION:preserved", transport.calls[2][2]["body"])
        self.assertEqual(transport.calls[2][2]["headers"]["If-Match"], '\"floating-v1\"')

    def test_aware_and_all_day_values_ignore_floating_fallback(self):
        for content in (PAYLOAD, codec.validate_payload({"summary": "Aware UTC", "start": {"dateTime": "2026-01-15T12:00:00+00:00", "timeZone": "UTC"},
                                                        "end": {"dateTime": "2026-01-15T13:00:00+00:00", "timeZone": "UTC"}})):
            provider, _ = dav(dav_get(content), floating_time_zone="Europe/Warsaw")
            self.assertEqual(provider._get(RESOURCE)[0].payload, content)

    def test_floating_recurrence_id_metadata_matches_its_anchored_equivalent(self):
        import re
        base = codec.validate_payload({**self.expected(), "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        original = {"dateTime": "2026-01-16T12:00:00+01:00", "timeZone": "Europe/Warsaw"}
        override = {"originalStartTime": original, **codec.default_instance(base, original), "summary": "Synthetic override"}
        before = codec.validate_payload({**base, "exceptions": [override]})
        raw = codec.to_ical(before, "floating@synthetic.test")
        raw = re.sub(b'BEGIN:VTIMEZONE\r\n.*?END:VTIMEZONE\r\n', b'', raw, flags=re.DOTALL)
        raw = raw.replace(b';TZID=Europe/Warsaw:', b':')
        raw = raw.replace(b'SUMMARY:Synthetic override', b'SUMMARY:Synthetic override\r\nX-DESTINATION:override-metadata')
        self.assertEqual(codec.from_ical(raw, floating_time_zone="Europe/Warsaw")[1], before)
        wanted = codec.validate_payload({**before, "summary": "Changed master"})
        provider, transport = dav(dav_acl(), response(raw, headers={"ETag": '\"floating-v1\"'}), response(status=204),
                                  dav_get(wanted, etag='\"floating-v2\"', uid="floating@synthetic.test"), floating_time_zone="Europe/Warsaw")
        self.assertEqual(provider.put(RESOURCE, wanted, '\"floating-v1\"', create=False, uid="floating@synthetic.test").payload, wanted)
        self.assertIn(b"X-DESTINATION:override-metadata", transport.calls[2][2]["body"])


class CalDAVExplicitEndCompatibilityTests(unittest.TestCase):
    @staticmethod
    def contents():
        start = {"dateTime": "2026-11-01T09:00:00+00:00", "timeZone": "UTC"}
        zero = codec.validate_payload({"summary": "Synthetic point", "start": start, "end": start})
        return [zero, codec.validate_payload({**zero, "endTimeUnspecified": True}),
                codec.validate_payload({**PAYLOAD, "endTimeUnspecified": True}), PAYLOAD,
                codec.validate_payload({"summary": "Synthetic fixed offset",
                    "start": {"dateTime": "2026-11-01T09:00:00+03:30", "timeZone": "GMT+03:30"},
                    "end": {"dateTime": "2026-11-01T10:00:00+03:30", "timeZone": "GMT+03:30"}})]

    def test_icloud_hosts_emit_explicit_end_and_preserve_unknown_zero_and_dates(self):
        for host in ("caldav.icloud.com", "p116-caldav.icloud.com"):
            selected = CALENDAR.replace("caldav.icloud.com", host)
            for content in self.contents():
                with self.subTest(host=host, content=content):
                    accepted = codec.to_ical(content, "compatibility@synthetic.test", explicit_end=True)
                    provider, transport = dav(dav_acl(href=selected), response(status=201),
                                              response(accepted, headers={"ETag": '\"compat-v1\"'}), calendar_id=selected)
                    result = provider.put(selected + "one.ics", content, None, create=True, uid="compatibility@synthetic.test")
                    body = transport.calls[1][2]["body"]
                    event = Calendar.from_ical(body).walk("VEVENT")[0]
                    self.assertIn("DTEND", event)
                    self.assertNotIn("DURATION", event)
                    self.assertEqual(result.payload, content)
                    self.assertEqual(codec.from_ical(body)[1], content)
                    self.assertEqual("X-HESTIA-END-UNSPECIFIED" in event, bool(content.get("endTimeUnspecified")))
                    if "dateTime" in content["start"] and content["end"] == content["start"]:
                        self.assertEqual(event["DTEND"].dt, event["DTSTART"].dt)

    def test_other_caldav_hosts_keep_rfc_omitted_end_without_inventing_duration(self):
        for host in ("caldav.example.test", "calendar.icloud.com", "p116-caldav.icloud.com.evil.test"):
            selected = "https://" + host + "/1/calendars/family/"
            for content in self.contents()[:3]:
                with self.subTest(host=host, content=content):
                    transport = QueueTransport(dav_acl(href=selected), response(status=201),
                        response(codec.to_ical(content, "compatibility@synthetic.test"), headers={"ETag": '\"compat-v1\"'}))
                    provider = p.CalDAVProvider(selected, username="user", password="secret", url="https://" + host + "/", transport=transport)
                    result = provider.put(selected + "one.ics", content, None, create=True, uid="compatibility@synthetic.test")
                    event = Calendar.from_ical(transport.calls[1][2]["body"]).walk("VEVENT")[0]
                    self.assertNotIn("DTEND", event)
                    self.assertNotIn("DURATION", event)
                    self.assertEqual(result.payload, content)

    def test_unknown_end_replay_matches_iclouds_explicit_end_copy(self):
        content = self.contents()[1]
        accepted = codec.to_ical(content, "compatibility@synthetic.test", explicit_end=True)
        provider, transport = dav(dav_acl(), response(status=412), response(accepted, headers={"ETag": '\"compat-v1\"'}))
        result = provider.put(RESOURCE, content, None, create=True, uid="compatibility@synthetic.test")
        self.assertEqual(result.payload, content)
        self.assertEqual(result.remote_id, RESOURCE)
        self.assertEqual(transport.calls[1][2]["headers"]["If-None-Match"], "*")


if __name__ == '__main__':
    unittest.main()
