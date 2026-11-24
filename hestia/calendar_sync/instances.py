"""Bounded expansion of validated calendar payloads using recurring-ical-events."""
from __future__ import annotations

from bisect import insort
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from icalendar import Calendar, vDDDTypes
from recurring_ical_events import of

from .events import default_instance, original_start_key, to_ical


INSTANCE_LIMIT = 2000
WORK_LIMIT = INSTANCE_LIMIT + 1
_UTC = timezone.utc
_MIN_UTC = datetime.min.replace(tzinfo=_UTC)
_MAX_UTC = datetime.max.replace(tzinfo=_UTC)
_TIME_SELECTORS = {"BYHOUR", "BYMINUTE", "BYSECOND"}
_HIGH_FREQUENCIES = {"SECONDLY", "MINUTELY"}
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
        self.ids = set()
        self.overflow = False

    def add(self, instance: dict):
        identifier = instance["instance_id"]
        if identifier in self.ids:
            return
        entry = (_sort_key(instance, self.window), identifier, instance)
        insort(self.entries, entry)
        self.ids.add(identifier)
        if len(self.entries) > INSTANCE_LIMIT + 1:
            removed = self.entries.pop()[2]["instance_id"]
            self.ids.remove(removed)
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


def _dt_value(value: dict):
    if "date" in value:
        return date.fromisoformat(value["date"])
    return datetime.fromisoformat(value["dateTime"]).astimezone(ZoneInfo(value["timeZone"]))


def _property_keys(line: str, kind: str) -> set:
    event = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + line + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).walk("VEVENT")[0]
    return {item.dt.astimezone(_UTC) if isinstance(item.dt, datetime) else item.dt for item in event[kind].dts}


def _library_payload(payload: dict) -> dict:
    content = deepcopy(payload)
    if "date" not in content["start"]:
        return content
    recurrence = []
    for line in content.get("recurrence", []):
        name, body = line.split(":", 1)
        if name.split(";", 1)[0] == "RRULE":
            body = ";".join(part for part in body.split(";") if part.split("=", 1)[0] not in _TIME_SELECTORS)
        recurrence.append(name + ":" + body)
    content["recurrence"] = recurrence
    return content


def _calendar(payload: dict, uid: str) -> Calendar:
    content = _library_payload(payload)
    calendar = Calendar.from_ical(to_ical(content, uid, explicit_end=True))
    components = calendar.walk("VEVENT")
    for component, exception in zip(components[1:], content.get("exceptions", [])):
        if "DTSTART" not in component:
            default = default_instance(content, exception["originalStartTime"])
            component.add("dtstart", _dt_value(default["start"]))
            component.add("dtend", _dt_value(default["end"]))
        if "dateTime" in exception["originalStartTime"]:
            original = datetime.fromisoformat(exception["originalStartTime"]["dateTime"]).astimezone(_UTC)
            component["RECURRENCE-ID"] = vDDDTypes(original)
    return calendar


def _time_dict(prop, default_zone: str | None, force_zone: str | None = None) -> dict:
    value = prop.dt
    if not isinstance(value, datetime):
        return {"date": value.isoformat()}
    if force_zone:
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(default_zone or "UTC"))
        zone_name = force_zone
        normalized = value.astimezone(_UTC).astimezone(ZoneInfo(force_zone))
        return {"dateTime": normalized.isoformat(timespec="seconds"), "timeZone": zone_name}
    zone_name = prop.params.get("TZID") or getattr(value.tzinfo, "key", None)
    if zone_name:
        zone = ZoneInfo(str(zone_name))
        zone_name = str(zone_name)
    elif value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(None):
        zone = _UTC
        zone_name = "UTC"
    else:
        zone_name = default_zone or "UTC"
        zone = ZoneInfo(zone_name)
        if value.tzinfo is None:
            value = value.replace(tzinfo=zone)
    return {"dateTime": value.astimezone(zone).isoformat(timespec="seconds"), "timeZone": zone_name}


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


def _component_instance(event: dict, component, window: _Window, override: bool) -> dict | None:
    default_zone = event["payload"]["start"].get("timeZone")
    start_zone = None if override else event["payload"]["start"].get("timeZone")
    end_zone = None if override else event["payload"]["end"].get("timeZone")
    content = {
        "summary": str(component.get("SUMMARY", "")),
        "location": str(component.get("LOCATION", "")),
        "start": _time_dict(component["DTSTART"], default_zone, start_zone),
        "end": _time_dict(component["DTEND"], default_zone, end_zone),
        "status": str(component.get("STATUS", "CONFIRMED")).lower(),
    }
    if not _overlaps(content, window):
        return None
    recurrence_id = (_time_dict(component["RECURRENCE-ID"], default_zone, default_zone)
                     if event["payload"].get("recurrence") else None)
    return _instance(event, content, recurrence_id)


def _high_frequency(payload: dict) -> bool:
    for line in payload.get("recurrence", []):
        name, body = line.split(":", 1)
        if name.split(";", 1)[0] != "RRULE":
            continue
        parts = dict(part.split("=", 1) for part in body.split(";"))
        if parts.get("FREQ") in _HIGH_FREQUENCIES:
            return True
    return False


def _first_instance(event: dict, window: _Window) -> dict | None:
    payload = event["payload"]
    all_day = "date" in payload["start"]
    starting = _key(payload["start"], all_day)
    for line in payload.get("recurrence", []):
        name = line.split(":", 1)[0].split(";", 1)[0]
        if name == "EXDATE" and starting in _property_keys(line, name):
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


def _expand_event(event: dict, window: _Window) -> tuple[list[dict], bool]:
    payload = event["payload"]
    if not payload.get("recurrence"):
        return ([_instance(event, payload, None)] if _overlaps(payload, window) else []), False
    if _high_frequency(payload):
        _LOG.warning("Calendar event %s uses SECONDLY or MINUTELY recurrence; using DTSTART only", event["id"])
        first = _first_instance(event, window)
        return ([first] if first is not None else []), True

    collector = _Collector(window)
    second_fold = _second_fold_start(payload)
    iterations = 0
    incomplete = False
    query_start, query_end = _query_bounds(window)
    for series in of(_calendar(payload, event["uid"])).series:
        for occurrence in series.between(query_start, query_end):
            component = occurrence.as_component(False)
            instance = _component_instance(event, component, window, bool(occurrence.recurrence_ids))
            if instance is None and payload.get("status") == "cancelled":
                continue
            iterations += 1
            if instance is not None:
                if second_fold is not None:
                    recurrence_id = _key(instance["recurrence_id"], False)
                    zone = ZoneInfo(payload["start"]["timeZone"])
                    original_wall = datetime.fromisoformat(payload["start"]["dateTime"]).astimezone(zone).replace(tzinfo=None)
                    candidate_wall = recurrence_id.astimezone(zone).replace(tzinfo=None)
                    if recurrence_id != second_fold and candidate_wall == original_wall:
                        continue
                collector.add(instance)
            if iterations >= WORK_LIMIT:
                incomplete = True
                break
        if incomplete:
            break
    if second_fold is not None:
        first = _first_instance(event, window)
        if first is not None:
            collector.add(first)
    if incomplete:
        _LOG.warning("Calendar event %s reached the instance expansion work budget", event["id"])
    return collector.values(), collector.overflow or incomplete


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
    if window.lower >= window.upper:
        return {"instances": [], "truncated": False}
    collector = _Collector(window)
    incomplete = False
    for event in events:
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
