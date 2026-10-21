"""Portable event content, deliberately separate from remote IDs and revision stamps.

Dates use an exclusive end. Recurring events remain series with detached exceptions;
they are never expanded into a finite list of appointments. Invitations, alarms and
provider-specific presentation fields are outside the copied content contract.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar, Event, Timezone, vRecur


class EventValidationError(ValueError):
    """Content cannot be copied without changing its calendar semantics."""


_TEXT = ("summary", "description", "location")
_CONTENT = {*_TEXT, "start", "end", "recurrence", "exceptions", "status", "transparency", "visibility", "endTimeUnspecified"}
_FIXED_ZONE = re.compile(r"GMT([+-])(\d{2}):(\d{2})\Z")
_OPTIONAL = {"status": ("confirmed", "tentative", "cancelled"),
             "transparency": ("opaque", "transparent"),
             "visibility": ("default", "public", "private", "confidential")}
_UTC = timezone.utc


def _fail(message: str) -> Any:
    raise EventValidationError(message)


def _temporal(value: Any, default_zone: str | None = None) -> dict:
    if not isinstance(value, dict) or set(value) - {"date", "dateTime", "timeZone"}:
        _fail("An event time must contain date or dateTime and an optional IANA timeZone")
    if ("date" in value) == ("dateTime" in value):
        _fail("Exactly one of date and dateTime is required")
    if "date" in value:
        raw = value["date"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            _fail("All-day dates must use YYYY-MM-DD")
        try:
            date.fromisoformat(raw)
        except ValueError:
            _fail("Invalid all-day date")
        return {"date": raw}
    try:
        raw = value["dateTime"]
        if not isinstance(raw, str) or "T" not in raw:
            _fail("dateTime must use RFC3339")
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        zone_name = value.get("timeZone", default_zone)
        fixed = _FIXED_ZONE.fullmatch(zone_name) if isinstance(zone_name, str) else None
        if fixed:
            if int(fixed[3]) > 59:
                _fail("Invalid fixed-offset timeZone")
            minutes = (int(fixed[2]) * 60 + int(fixed[3])) * (-1 if fixed[1] == "-" else 1)
            zone = timezone(timedelta(minutes=minutes))
        else:
            zone = ZoneInfo(zone_name) if zone_name else _UTC
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        _fail("Invalid dateTime or unknown IANA timeZone")
    if parsed.microsecond:
        _fail("Subsecond event times cannot be represented by iCalendar")
    if parsed.tzinfo is None:
        if not zone_name:
            _fail("Floating times require an explicit IANA timeZone")
        early, late = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
        if early.utcoffset() != late.utcoffset():
            _fail("Ambiguous or nonexistent local time needs an explicit UTC offset")
        parsed = early
    normalized = parsed.astimezone(_UTC if fixed else zone)
    return {"dateTime": normalized.isoformat(timespec="seconds"), "timeZone": "UTC" if fixed else zone_name or "UTC"}


def _dt(value: dict) -> date | datetime:
    if "date" in value:
        return date.fromisoformat(value["date"])
    return datetime.fromisoformat(value["dateTime"]).astimezone(ZoneInfo(value["timeZone"]))


def _time_key(value: dict) -> str:
    if "date" in value:
        return value["date"]
    return _dt(value).astimezone(_UTC).isoformat()


def _unspecified_end(start: dict) -> dict:
    return ({"date": (_dt(start) + timedelta(days=1)).isoformat()}
            if "date" in start else deepcopy(start))


def original_start_key(value: dict) -> str:
    """Stable instance identity across IANA-zone and UTC API representations."""
    return _time_key(_temporal(value))


def _recurrence(lines: Any, start: dict) -> list[str]:
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        _fail("recurrence must be a list of RFC5545 content lines")
    result = set()
    for line in lines:
        if "\r" in line or "\n" in line or ":" not in line:
            _fail("Invalid recurrence content line")
        name, body = line.split(":", 1)
        kind = name.split(";", 1)[0].upper()
        if kind not in {"RRULE", "RDATE", "EXDATE"}:
            _fail("Only RRULE, RDATE and EXDATE recurrence properties are supported")
        try:
            event = Calendar.from_ical("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n" + line + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").walk("VEVENT")[0]
            if event.errors:
                _fail("Invalid recurrence property")
            prop = event[kind]
            if kind == "RRULE":
                rule = vRecur.from_ical(body.upper())
                if set(rule) - {"FREQ", "UNTIL", "COUNT", "INTERVAL", "BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH", "BYSETPOS", "WKST"}:
                    _fail("Unsupported RRULE extension")
                if rule.get("FREQ", [None])[0] not in {"SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
                    _fail("RRULE requires a valid FREQ")
                if "COUNT" in rule and "UNTIL" in rule:
                    _fail("RRULE cannot contain both COUNT and UNTIL")
                if any(int(rule.get(key, [1])[0]) <= 0 for key in ("COUNT", "INTERVAL")):
                    _fail("RRULE COUNT and INTERVAL must be positive")
                if "UNTIL" in rule:
                    until = rule["UNTIL"][0]
                    if ("date" in start) == isinstance(until, datetime):
                        _fail("RRULE UNTIL must match the event date type")
                    if isinstance(until, datetime) and until.utcoffset() != timedelta(0):
                        _fail("Timed RRULE UNTIL must be UTC")
                result.add("RRULE:" + ";".join(key + "=" + ",".join(sorted(part.split("=", 1)[1].split(",")))
                                             for part in rule.to_ical().decode().split(";")
                                             for key in [part.split("=", 1)[0]]))
            else:
                for item in prop.dts:
                    value = item.dt
                    if isinstance(value, tuple):
                        _fail("RDATE periods are not supported")
                    if ("date" in start) == isinstance(value, datetime):
                        _fail("Recurrence dates must match the event date type")
                    if isinstance(value, datetime):
                        if value.tzinfo is None:
                            _fail("Floating recurrence dates are not supported")
                        result.add(kind + ":" + value.astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ"))
                    else:
                        result.add(kind + ";VALUE=DATE:" + value.strftime("%Y%m%d"))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            if isinstance(exc, EventValidationError):
                raise
            _fail("Invalid recurrence property")
    return sorted(result)


def _content(payload: Any, *, exception: bool = False, parent: dict | None = None) -> dict:
    allowed = _CONTENT | ({"originalStartTime"} if exception else set())
    if not isinstance(payload, dict) or set(payload) - allowed:
        _fail("Unknown event content fields")
    raw_start = payload.get("start", {})
    if payload.get("recurrence") and isinstance(raw_start, dict) and _FIXED_ZONE.fullmatch(str(raw_start.get("timeZone", ""))):
        _fail("A recurring fixed-offset calendar needs an equivalent IANA timeZone")
    result = {}
    if exception:
        original = payload.get("originalStartTime")
        if isinstance(original, dict) and "dateTime" in original:
            original = {**original, "timeZone": parent["start"].get("timeZone", "UTC")}
        result["originalStartTime"] = _temporal(original)
        if ("date" in result["originalStartTime"]) != ("date" in parent["start"]):
            _fail("An exception must match its series date type")
        if payload.get("status") == "cancelled":
            return {**result, "status": "cancelled"}
        if "recurrence" in payload or "exceptions" in payload:
            _fail("An exception cannot contain its own recurrence")
    for field in _TEXT:
        value = payload.get(field, parent.get(field, "") if parent else "")
        if not isinstance(value, str):
            _fail("summary, description and location must be strings")
        result[field] = value.replace("\r\n", "\n").replace("\r", "\n")
    for field in ("start", "end"):
        result[field] = _temporal(payload.get(field))
    if ("date" in result["start"]) != ("date" in result["end"]):
        _fail("Event start and end must have the same date type")
    unknown_end = payload.get("endTimeUnspecified", parent.get("endTimeUnspecified", False) if parent else False)
    if type(unknown_end) is not bool:
        _fail("endTimeUnspecified must be boolean")
    if unknown_end:
        result["end"] = _unspecified_end(result["start"])
    if unknown_end or (exception and parent.get("endTimeUnspecified", False)):
        result["endTimeUnspecified"] = unknown_end
    start_key, end_key = _time_key(result["start"]), _time_key(result["end"])
    if end_key < start_key or ("date" in result["start"] and end_key == start_key):
        _fail("Event end cannot precede its start; all-day end is exclusive")
    for field, choices in _OPTIONAL.items():
        value = payload.get(field, parent.get(field, choices[0]) if parent else choices[0])
        if value not in choices:
            _fail("Invalid event " + field)
        if value != choices[0] or (exception and parent.get(field, choices[0]) != value):
            result[field] = value
    if not exception:
        recurrence = _recurrence(payload.get("recurrence", []), result["start"])
        if recurrence:
            result["recurrence"] = recurrence
        exceptions = payload.get("exceptions", [])
        if not isinstance(exceptions, list) or (exceptions and not recurrence):
            _fail("Exceptions require a recurring series")
        by_start = {}
        seen = set()
        for item in exceptions:
            normalized = _content(item, exception=True, parent=result)
            key = _time_key(normalized["originalStartTime"])
            if key in seen:
                _fail("Duplicate recurrence exception")
            seen.add(key)
            if normalized != {"originalStartTime": normalized["originalStartTime"],
                              **default_instance(result, normalized["originalStartTime"])}:
                by_start[key] = normalized
        if by_start:
            result["exceptions"] = [by_start[key] for key in sorted(by_start)]
    return result


def validate_payload(payload: Any) -> dict:
    """Return detached, normalized portable content or raise EventValidationError."""
    return _content(payload)


def fingerprint(payload: dict) -> str:
    """Hash content, excluding server IDs, etags, modification stamps and ordering."""
    wire = json.dumps(validate_payload(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def default_instance(payload: dict, original_start_time: dict) -> dict:
    """Default occurrence content, preserving DTSTART/DTEND exact duration.

    Used to reset removed Google overrides. The caller supplies validated master
    content and an instance's originalStartTime, not a relocated override start.
    """
    result = {key: deepcopy(value) for key, value in payload.items()
              if key not in {"recurrence", "exceptions", "start", "end"}}
    start = _temporal(original_start_time, payload["start"].get("timeZone"))
    if "date" in payload["start"]:
        result["start"] = start
        result["end"] = {"date": (_dt(start) + (_dt(payload["end"]) - _dt(payload["start"]))).isoformat()}
    else:
        zone_name = payload["start"]["timeZone"]
        occurrence = _dt(start).astimezone(ZoneInfo(zone_name))
        # RFC5545 3.8.5.3: a DTEND duration is elapsed time, including across DST.
        duration = _dt(payload["end"]).astimezone(_UTC) - _dt(payload["start"]).astimezone(_UTC)
        result["start"] = _temporal({"dateTime": occurrence.isoformat(), "timeZone": zone_name})
        result["end"] = _temporal({"dateTime": (occurrence.astimezone(_UTC) + duration).isoformat(), "timeZone": payload["end"]["timeZone"]})
    return result


def from_google_event(event: dict) -> dict:
    """Normalize a Google master; remote metadata is intentionally not content."""
    if event.get("eventType", "default") not in ("default", "fromGmail", "birthday"):
        _fail("This Google event type cannot be copied as ordinary content")
    return validate_payload({key: value for key, value in _google_content(event).items() if key in _CONTENT})


def _google_content(event: dict) -> dict:
    result = {key: deepcopy(value) for key, value in event.items() if key in (_CONTENT | {"originalStartTime"})}
    native = event.get("endTimeUnspecified", False)
    if type(native) is not bool:
        _fail("endTimeUnspecified must be boolean")
    result["endTimeUnspecified"] = native
    marker = event.get("extendedProperties", {}).get("private", {}).get("hestia_end_unspecified")
    if marker in ("true", "false"):
        unknown_end = marker == "true"
        if unknown_end and not native and event.get("status") != "cancelled":
            # A device adding a concrete duration supersedes a retained metadata
            # marker on a copied ordinary Google event.
            start, end = _temporal(event.get("start")), _temporal(event.get("end"))
            unknown_end = _time_key(end) == _time_key(_unspecified_end(start))
        result["endTimeUnspecified"] = unknown_end
    return result


def from_google_bundle(master: dict, exceptions: list[dict]) -> dict:
    payload = from_google_event(master)
    payload["exceptions"] = [_google_content(item) for item in exceptions]
    return validate_payload(payload)


def to_google_event(payload: dict, uid: str | None = None) -> dict:
    result = validate_payload(payload)
    result.pop("exceptions", None)
    result.pop("endTimeUnspecified", None)  # Google exposes this field as read-only.
    if uid:
        result["iCalUID"] = uid
    return result


def to_google_exception(exception: dict) -> dict:
    """The bundle is validated before provider writes; keep the original instance key."""
    result = deepcopy(exception)
    result.pop("endTimeUnspecified", None)
    return result


def _ical_time(prop: Any) -> dict:
    value = prop.dt
    if isinstance(value, datetime):
        name = prop.params.get("TZID") or getattr(value.tzinfo, "key", None)
        return _temporal({"dateTime": value.isoformat(), **({"timeZone": str(name)} if name else {})})
    return _temporal({"date": value.isoformat()})


def _ical_content(event: Event, parent: dict | None = None) -> dict:
    if event.errors:
        _fail("Malformed iCalendar event")
    if "RECURRENCE-ID" in event and event["RECURRENCE-ID"].params.get("RANGE"):
        _fail("RECURRENCE-ID RANGE changes are not supported")
    result = {key: str(event.get(key, parent.get(key, "") if parent else "")) for key in _TEXT}
    if "RECURRENCE-ID" in event:
        result["originalStartTime"] = _ical_time(event["RECURRENCE-ID"])
    for field in _OPTIONAL:
        ical_name = {"transparency": "TRANSP", "visibility": "CLASS"}.get(field, field)
        if ical_name.upper() in event:
            result[field] = str(event[ical_name]).lower()
    if result.get("status") == "cancelled" and parent:
        return {"originalStartTime": result["originalStartTime"], "status": "cancelled"}
    if "DTSTART" not in event:
        _fail("iCalendar event is missing DTSTART")
    result["start"] = _ical_time(event["DTSTART"])
    fixed = _FIXED_ZONE.fullmatch(str(event["DTSTART"].params.get("TZID", "")))
    if fixed and any(name in event for name in ("RRULE", "RDATE", "EXDATE", "EXRULE")):
        _fail("A recurring fixed-offset calendar needs an equivalent IANA timeZone")
    if "DTEND" in event and "DURATION" in event:
        _fail("iCalendar event cannot contain both DTEND and DURATION")
    if "DTEND" in event:
        result["end"] = _ical_time(event["DTEND"])
    else:
        duration = event.decoded("DURATION", timedelta(days=1) if "date" in result["start"] else timedelta(0))
        starting = _dt(result["start"])
        if isinstance(starting, datetime):
            if duration.days:
                _fail("Timed DURATION with nominal days cannot be copied; use DTEND")
            starting = starting.astimezone(_UTC)
        ending = starting + duration
        result["end"] = _temporal({"dateTime": ending.isoformat(), "timeZone": result["start"]["timeZone"]} if isinstance(ending, datetime) else {"date": ending.isoformat()})
    if str(event.get("X-HESTIA-END-UNSPECIFIED", "")).upper() == "TRUE" and "DTEND" not in event and "DURATION" not in event:
        result["endTimeUnspecified"] = True
    elif parent and parent.get("endTimeUnspecified"):
        result["endTimeUnspecified"] = False
    lines = []
    for name in ("RRULE", "RDATE", "EXDATE", "EXRULE"):
        props = event.get(name, [])
        for prop in props if isinstance(props, list) else [props]:
            params = prop.params.to_ical().decode()
            lines.append(name + (";" + params if params else "") + ":" + prop.to_ical().decode())
    if lines:
        result["recurrence"] = lines
    return result


def from_ical(data: str | bytes) -> tuple[str, dict]:
    """Read one CalDAV resource (one UID, one series master, optional overrides)."""
    try:
        calendar = Calendar.from_ical(data)
        events = calendar.walk("VEVENT")
        masters = [event for event in events if "RECURRENCE-ID" not in event]
        if len(masters) != 1:
            _fail("A CalDAV resource must contain exactly one series master")
        uid = str(masters[0].get("UID", ""))
        if not uid or any(str(event.get("UID", "")) != uid for event in events):
            _fail("A CalDAV resource must have one nonempty UID")
        result = _ical_content(masters[0])
        result["exceptions"] = [_ical_content(event, result) for event in events if "RECURRENCE-ID" in event]
        return uid, validate_payload(result)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        if isinstance(exc, EventValidationError):
            raise
        _fail("Invalid iCalendar resource")


def to_ical(payload: dict, uid: str) -> bytes:
    content = validate_payload(payload)
    if not isinstance(uid, str) or not uid or "\r" in uid or "\n" in uid:
        _fail("A nonempty single-line UID is required")
    calendar = Calendar()
    calendar.add("prodid", "-//Hestia//Calendar Sync//EN")
    calendar.add("version", "2.0")
    zones = set()
    for item in [content, *content.get("exceptions", [])]:
        event = Event()
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(_UTC).replace(microsecond=0))
        for field in _TEXT:
            if item.get(field):
                event.add(field, item[field])
        for field, ical_name in (("start", "dtstart"), ("end", "dtend"), ("originalStartTime", "recurrence-id")):
            if field in item:
                if field == "end" and (item.get("endTimeUnspecified") or _time_key(item["end"]) == _time_key(item["start"])):
                    continue
                event.add(ical_name, _dt(item[field]))
                if item[field].get("timeZone", "UTC") != "UTC":
                    zones.add(item[field]["timeZone"])
        if item.get("endTimeUnspecified"):
            event.add("X-HESTIA-END-UNSPECIFIED", "TRUE")
        for field in _OPTIONAL:
            if field in item:
                event.add({"transparency": "transp", "visibility": "class"}.get(field, field), item[field].upper())
        for line in item.get("recurrence", []):
            parsed = Calendar.from_ical("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + line + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").walk("VEVENT")[0]
            name = line.split(":", 1)[0].split(";", 1)[0]
            event.add(name, parsed[name], encode=False)
        calendar.add_component(event)
    for name in sorted(zones):
        calendar.add_component(Timezone.from_tzinfo(ZoneInfo(name), first_date=date(1970, 1, 1), last_date=date(2100, 1, 1)))
    return calendar.to_ical()
