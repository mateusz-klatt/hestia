"""Bounded expansion of validated calendar payloads using recurring-ical-events."""
from __future__ import annotations

from bisect import insort
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from math import ceil
from zoneinfo import ZoneInfo

from icalendar import Calendar, vDDDTypes, vRecur
from recurring_ical_events import of

from .events import original_start_key, to_ical


INSTANCE_LIMIT = 2000
OCCURRENCE_BOUND = 5000
REPLAY_BOUND = 200000
_UTC = timezone.utc
_MIN_UTC = datetime.min.replace(tzinfo=_UTC)
_MAX_UTC = datetime.max.replace(tzinfo=_UTC)
_TIME_SELECTORS = {"BYHOUR", "BYMINUTE", "BYSECOND"}
_HIGH_FREQUENCIES = {"SECONDLY", "MINUTELY", "HOURLY"}
_FREQUENCY_SECONDS = {
    "HOURLY": 3600,
    "DAILY": 86400,
    "WEEKLY": 7 * 86400,
    "MONTHLY": 28 * 86400,
    "YEARLY": 365 * 86400,
}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Window:
    start: date
    end: date
    zone: ZoneInfo
    lower: datetime
    upper: datetime


class _Collector:
    def __init__(self, window: _Window):
        self.window = window
        self.entries = []
        self.overflow = False

    def add(self, instance: dict):
        entry = (_sort_key(instance, self.window), instance["instance_id"], instance)
        insort(self.entries, entry)
        if len(self.entries) > INSTANCE_LIMIT + 1:
            self.entries.pop()
            self.overflow = True

    def values(self) -> list[dict]:
        return [entry[2] for entry in self.entries]


def _key(value: dict, all_day: bool):
    if all_day:
        return date.fromisoformat(value["date"])
    return datetime.fromisoformat(value["dateTime"]).astimezone(_UTC)


def _midnight_utc(day: date, zone: ZoneInfo, lower: bool) -> datetime:
    try:
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(_UTC)
    except OverflowError:
        return _MIN_UTC if lower else _MAX_UTC


def _make_window(start: date, end: date, zone_name: str) -> _Window:
    zone = ZoneInfo(zone_name)
    return _Window(start, end, zone, _midnight_utc(start, zone, True), _midnight_utc(end, zone, False))


def _property_keys(line: str, kind: str) -> set:
    event = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + line + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).walk("VEVENT")[0]
    return {item.dt.astimezone(_UTC) if isinstance(item.dt, datetime) else item.dt for item in event[kind].dts}


def _kind(line: str) -> str:
    return line.split(":", 1)[0].split(";", 1)[0]


def _library_payload(payload: dict) -> dict:
    content = deepcopy(payload)
    content["exceptions"] = []
    content["recurrence"] = [line for line in content.get("recurrence", []) if _kind(line) != "EXDATE"]
    if "date" not in content["start"]:
        return content
    recurrence = []
    for line in content["recurrence"]:
        name, body = line.split(":", 1)
        if _kind(line) == "RRULE":
            body = ";".join(part for part in body.split(";") if part.split("=", 1)[0] not in _TIME_SELECTORS)
        recurrence.append(name + ":" + body)
    content["recurrence"] = recurrence
    return content


def _calendar(payload: dict, uid: str) -> Calendar:
    content = _library_payload(payload)
    calendar = Calendar.from_ical(to_ical(content, uid, explicit_end=True))
    components = calendar.walk("VEVENT")
    if "date" not in content["start"]:
        components[0]["DTEND"] = vDDDTypes(_key(content["end"], False))
    return calendar


def _timed_queries(payload: dict) -> list[tuple[dict, datetime | None]]:
    content = _library_payload(payload)
    rdates = [line for line in content.get("recurrence", []) if _kind(line) == "RDATE"]
    rrules = [line for line in content.get("recurrence", []) if _kind(line) == "RRULE"]
    if not rrules:
        return [(content, None)]
    queries = []
    for line in rrules:
        name, body = line.split(":", 1)
        until_values = vRecur.from_ical(body).get("UNTIL")
        until = until_values[0].astimezone(_UTC) if until_values else None
        body = ";".join(part for part in body.split(";") if part.split("=", 1)[0] != "UNTIL")
        item = deepcopy(content)
        item["recurrence"] = [name + ":" + body, *rdates]
        queries.append((item, until))
    return queries


def _explicit_keys(payload: dict, all_day: bool) -> set:
    result = {_key(payload["start"], all_day)}
    for line in payload.get("recurrence", []):
        if _kind(line) == "RDATE":
            result.update(_property_keys(line, "RDATE"))
    return result


def _exclusion_keys(payload: dict) -> set:
    result = set()
    for line in payload.get("recurrence", []):
        if _kind(line) == "EXDATE":
            result.update(_property_keys(line, "EXDATE"))
    return result


def _time_dict(prop, default_zone: str | None, force_zone: str | None = None) -> dict:
    value = prop.dt
    if not isinstance(value, datetime):
        return {"date": value.isoformat()}
    zone_name = force_zone or default_zone or "UTC"
    normalized = value.astimezone(_UTC).astimezone(ZoneInfo(zone_name))
    return {"dateTime": normalized.isoformat(timespec="seconds"), "timeZone": zone_name}


def _overlaps(content: dict, window: _Window) -> bool:
    if content.get("status") == "cancelled":
        return False
    if "date" in content["start"]:
        starting = date.fromisoformat(content["start"]["date"])
        ending = date.fromisoformat(content["end"]["date"])
        return starting < window.end and ending > window.start
    starting = datetime.fromisoformat(content["start"]["dateTime"]).astimezone(_UTC)
    ending = datetime.fromisoformat(content["end"]["dateTime"]).astimezone(_UTC)
    return starting < window.upper and (ending > window.lower or starting == ending and starting >= window.lower)


def _instance(event: dict, content: dict, recurrence_id: dict | None) -> dict:
    instance_id = event["id"] if recurrence_id is None else event["id"] + "/" + original_start_key(recurrence_id)
    return {
        "instance_id": instance_id,
        "event_id": event["id"],
        "revision": event["revision"],
        "group": event["group_id"],
        "summary": content.get("summary", ""),
        "location": content.get("location", ""),
        "start": content["start"],
        "end": content["end"],
        "all_day": "date" in content["start"],
        "status": content.get("status", "confirmed"),
        "recurrence_id": recurrence_id,
    }


def _base_end(payload: dict, start: dict) -> dict | None:
    try:
        if "date" in start:
            duration = date.fromisoformat(payload["end"]["date"]) - date.fromisoformat(payload["start"]["date"])
            return {"date": (date.fromisoformat(start["date"]) + duration).isoformat()}
        duration = _key(payload["end"], False) - _key(payload["start"], False)
        ending = _key(start, False) + duration
        zone_name = payload["end"]["timeZone"]
        return {"dateTime": ending.astimezone(ZoneInfo(zone_name)).isoformat(timespec="seconds"),
                "timeZone": zone_name}
    except OverflowError:
        return None


def _component_instance(event: dict, component, window: _Window) -> dict | None:
    payload = event["payload"]
    default_zone = payload["start"].get("timeZone")
    content = {
        "summary": str(component.get("SUMMARY", "")),
        "location": str(component.get("LOCATION", "")),
        "start": _time_dict(component["DTSTART"], default_zone, default_zone),
        "status": str(component.get("STATUS", "CONFIRMED")).lower(),
    }
    content["end"] = _base_end(payload, content["start"])
    if content["end"] is None or not _overlaps(content, window):
        return None
    recurrence_id = (_time_dict(component["RECURRENCE-ID"], default_zone, default_zone)
                     if payload.get("recurrence") else None)
    return _instance(event, content, recurrence_id)


def _stored_override(event: dict, exception: dict, original) -> dict | None:
    if not isinstance(original, datetime):
        recurrence_id = {"date": original.isoformat()}
    else:
        zone_name = event["payload"]["start"]["timeZone"]
        recurrence_id = {"dateTime": original.astimezone(ZoneInfo(zone_name)).isoformat(timespec="seconds"),
                         "timeZone": zone_name}
    return _instance(event, exception, recurrence_id)


def _high_frequency(payload: dict) -> bool:
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        _, body = line.split(":", 1)
        parts = dict(part.split("=", 1) for part in body.split(";"))
        if parts.get("FREQ") in _HIGH_FREQUENCIES:
            return True
    return False


def _first_instance(event: dict, window: _Window) -> dict | None:
    payload = event["payload"]
    all_day = "date" in payload["start"]
    starting = _key(payload["start"], all_day)
    for line in payload.get("recurrence", []):
        if _kind(line) == "EXDATE" and starting in _property_keys(line, "EXDATE"):
            return None
    content = payload
    for exception in payload.get("exceptions", []):
        if _key(exception["originalStartTime"], all_day) == starting:
            if exception.get("status") == "cancelled":
                return None
            content = exception
            break
    else:
        if payload.get("status") == "cancelled":
            return None
    if not _overlaps(content, window):
        return None
    recurrence_id = payload["start"] if payload.get("recurrence") else None
    return _instance(event, content, recurrence_id)


def _second_fold_start(payload: dict) -> datetime | None:
    if "date" in payload["start"] or not payload.get("recurrence"):
        return None
    zone = ZoneInfo(payload["start"]["timeZone"])
    local = datetime.fromisoformat(payload["start"]["dateTime"]).astimezone(zone)
    if local.fold == 1 and local.replace(fold=0).utcoffset() != local.utcoffset():
        return local.astimezone(_UTC)
    return None


def _query_bounds(window: _Window) -> tuple[datetime, datetime]:
    try:
        lower = window.lower - timedelta(days=1)
    except OverflowError:
        lower = _MIN_UTC
    try:
        upper = window.upper + timedelta(days=1)
    except OverflowError:
        upper = _MAX_UTC
    return lower, upper


def _occurrence_upper_bound(payload: dict, start: datetime, end: datetime) -> int:
    seconds = max(0, (end - start).total_seconds())
    bound = 1 + len(payload.get("exceptions", []))
    for line in payload.get("recurrence", []):
        kind = _kind(line)
        if kind == "RDATE":
            bound += len(_property_keys(line, kind))
            continue
        if kind != "RRULE":
            continue
        _, body = line.split(":", 1)
        parts = dict(part.split("=", 1) for part in body.split(";"))
        interval = int(parts.get("INTERVAL", "1"))
        estimated = ceil(seconds / (_FREQUENCY_SECONDS[parts["FREQ"]] * interval)) + 1
        bound += min(int(parts["COUNT"]), estimated) if "COUNT" in parts else estimated
    return bound


def _replay_upper_bound(payload: dict, end: datetime) -> int:
    content = _library_payload(payload)
    all_day = "date" in content["start"]
    starting = _key(content["start"], all_day)
    if all_day:
        starting = datetime.combine(starting, time.min, tzinfo=_UTC)
    seconds = max(0, (end - starting).total_seconds())
    bound = 0
    for line in content.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        _, body = line.split(":", 1)
        parts = dict(part.split("=", 1) for part in body.split(";"))
        frequency = parts["FREQ"]
        estimated = ceil(seconds / (_FREQUENCY_SECONDS[frequency] * int(parts.get("INTERVAL", "1")))) + 1
        if "BYDAY" in parts and not {"BYMONTHDAY", "BYYEARDAY", "BYWEEKNO"}.intersection(parts):
            if frequency == "YEARLY":
                estimated *= 53
            elif frequency == "MONTHLY":
                estimated *= 5
        for selector, values in parts.items():
            if selector.startswith("BY"):
                estimated *= len(values.split(","))
        bound += estimated
    return bound


def _impossible_rule(payload: dict) -> bool:
    limits = {2: 29, 4: 30, 6: 30, 9: 30, 11: 30}
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        _, body = line.split(":", 1)
        parts = dict(part.split("=", 1) for part in body.split(";"))
        if "BYMONTH" not in parts or "BYMONTHDAY" not in parts:
            continue
        days = [int(value) for value in parts["BYMONTHDAY"].split(",")]
        possible = any(1 <= abs(day) <= limits.get(int(month), 31)
                       for month in parts["BYMONTH"].split(",") for day in days)
        if not possible:
            return True
    return False


def _expand_event(event: dict, window: _Window) -> tuple[list[dict], bool]:
    payload = event["payload"]
    if not payload.get("recurrence"):
        return ([_instance(event, payload, None)] if _overlaps(payload, window) else []), False
    if _high_frequency(payload):
        _LOG.warning("Calendar event %s uses SECONDLY, MINUTELY or HOURLY recurrence; using DTSTART only", event["id"])
        first = _first_instance(event, window)
        return ([first] if first is not None else []), True
    if _impossible_rule(payload):
        _LOG.warning("Calendar event %s has impossible month/day recurrence selectors; using DTSTART only", event["id"])
        first = _first_instance(event, window)
        return ([first] if first is not None else []), True
    query_start, query_end = _query_bounds(window)
    all_day = "date" in payload["start"]
    exceptions = {_key(item["originalStartTime"], all_day): item for item in payload.get("exceptions", [])}
    pending = {original: item for original, item in exceptions.items() if _overlaps(item, window)}
    if _occurrence_upper_bound(payload, query_start, query_end) > OCCURRENCE_BOUND:
        _LOG.warning("Calendar event %s exceeds the instance occurrence bound; using DTSTART only", event["id"])
        first = _first_instance(event, window)
        return ([first] if first is not None else []), True
    if _replay_upper_bound(payload, query_end) > REPLAY_BOUND:
        _LOG.warning("Calendar event %s exceeds the recurrence replay bound; using DTSTART only", event["id"])
        first = _first_instance(event, window)
        return ([first] if first is not None else []), True

    explicit = _explicit_keys(payload, all_day)
    exdates = _exclusion_keys(payload)
    seen_overrides = set()
    series_slots = set()
    second_fold = _second_fold_start(payload)
    matches = []
    identifiers = set()

    def add(instance):
        if instance is not None and instance["instance_id"] not in identifiers:
            identifiers.add(instance["instance_id"])
            matches.append(instance)

    queries = [(payload, None)] if all_day else _timed_queries(payload)
    for content, until in queries:
        for series in of(_calendar(content, event["uid"])).series:
            for occurrence in series.between(query_start, query_end):
                component = occurrence.as_component(False)
                if all_day:
                    original_value = component["RECURRENCE-ID"].dt
                    original = original_value.date() if isinstance(original_value, datetime) else original_value
                else:
                    recurrence_id = _time_dict(component["RECURRENCE-ID"], payload["start"]["timeZone"],
                                               payload["start"]["timeZone"])
                    original = _key(recurrence_id, False)
                if until is not None and original not in explicit and original > until:
                    continue
                series_slots.add(original)
                if original in exdates:
                    continue
                if original in exceptions:
                    seen_overrides.add(original)
                    instance = (_stored_override(event, exceptions[original], original)
                                if original in pending else None)
                else:
                    instance = _component_instance(event, component, window)
                if instance is not None and second_fold is not None and original not in explicit:
                    zone = ZoneInfo(payload["start"]["timeZone"])
                    original_wall = datetime.fromisoformat(payload["start"]["dateTime"]).astimezone(zone).replace(tzinfo=None)
                    candidate_wall = original.astimezone(zone).replace(tzinfo=None)
                    if original != second_fold and candidate_wall == original_wall:
                        continue
                add(instance)
    if second_fold is not None:
        add(_first_instance(event, window))
    for original, exception in pending.items():
        if original in seen_overrides or original in exdates:
            continue
        if original not in explicit and original not in series_slots:
            if all_day:
                first = query_start.astimezone(window.zone).date()
                last = query_end.astimezone(window.zone).date()
                near = first <= original <= last
            else:
                near = query_start <= original <= query_end
            if near:
                _LOG.debug("Skipping orphan override for calendar event %s at %s", event["id"], original.isoformat())
                continue
        add(_stored_override(event, exception, original))
    matches.sort(key=lambda instance: _sort_key(instance, window))
    return matches[:INSTANCE_LIMIT + 1], False


def _sort_key(instance: dict, window: _Window) -> tuple:
    value = instance["start"]
    if "date" in value:
        starting = _midnight_utc(date.fromisoformat(value["date"]), window.zone, True)
    else:
        starting = datetime.fromisoformat(value["dateTime"]).astimezone(_UTC)
    recurrence_id = instance["recurrence_id"]
    identity = original_start_key(recurrence_id) if recurrence_id is not None else ""
    return starting, instance["event_id"], identity


def expand_events(events: list[dict], window_start: date, window_end: date, zone_name: str = "UTC") -> dict:
    """Return the earliest instances in the half-open local-date window, capped at 2000."""
    if window_start >= window_end:
        return {"instances": [], "truncated": False}
    window = _make_window(window_start, window_end, zone_name)
    empty_timed_window = window.lower >= window.upper
    collector = _Collector(window)
    incomplete = False
    for event in events:
        if empty_timed_window and "date" not in event["payload"]["start"]:
            continue
        try:
            expanded, event_incomplete = _expand_event(event, window)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
            try:
                first = _first_instance(event, window)
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
                first = None
            if first is None:
                _LOG.warning("Skipping incompatible calendar event %s", event.get("id", "unknown"))
            else:
                _LOG.warning("Calendar event %s has incompatible recurrence; using DTSTART only", event.get("id", "unknown"))
            expanded = [first] if first is not None else []
            event_incomplete = True
        incomplete |= event_incomplete
        for instance in expanded:
            collector.add(instance)
    instances = collector.values()
    truncated = collector.overflow or incomplete or len(instances) > INSTANCE_LIMIT
    return {"instances": instances[:INSTANCE_LIMIT], "truncated": truncated}
