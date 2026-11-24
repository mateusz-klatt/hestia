"""Bounded expansion of validated calendar payloads into concrete occurrences."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from dateutil.rrule import rruleset, rrulestr
from icalendar import Calendar

from .events import default_instance, original_start_key


INSTANCE_LIMIT = 2000
_UTC = timezone.utc


def _rule_time(value: dict, zone_name: str | None) -> datetime:
    if "date" in value:
        return datetime.combine(date.fromisoformat(value["date"]), time.min)
    return datetime.fromisoformat(value["dateTime"]).astimezone(ZoneInfo(zone_name))


def _calendar_time(value: datetime, all_day: bool, zone_name: str | None) -> dict:
    if all_day:
        return {"date": value.date().isoformat()}
    return {"dateTime": value.isoformat(), "timeZone": zone_name}


def _rules(payload: dict) -> rruleset:
    zone_name = payload["start"].get("timeZone")
    starting = _rule_time(payload["start"], zone_name)
    rules = rruleset()
    rules.rdate(starting)
    for line in payload.get("recurrence", []):
        name, body = line.split(":", 1)
        kind = name.split(";", 1)[0]
        if kind == "RRULE":
            rules.rrule(rrulestr(body, dtstart=starting))
            continue
        event = Calendar.from_ical(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + line + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).walk("VEVENT")[0]
        for item in event[kind].dts:
            value = item.dt
            if isinstance(value, datetime):
                value = value.astimezone(ZoneInfo(zone_name))
            else:
                value = datetime.combine(value, time.min)
            if kind == "RDATE":
                rules.rdate(value)
            else:
                rules.exdate(value)
    return rules


def _contains(rules: rruleset, value: datetime) -> bool:
    return next(rules.xafter(value, count=1, inc=True), None) == value


def _overlaps(content: dict, window_start: date, window_end: date) -> bool:
    if "date" in content["start"]:
        starting = date.fromisoformat(content["start"]["date"])
        ending = date.fromisoformat(content["end"]["date"])
        return starting < window_end and ending > window_start
    starting = datetime.fromisoformat(content["start"]["dateTime"]).astimezone(_UTC)
    ending = datetime.fromisoformat(content["end"]["dateTime"]).astimezone(_UTC)
    lower = datetime.combine(window_start, time.min, tzinfo=_UTC)
    upper = datetime.combine(window_end, time.min, tzinfo=_UTC)
    return starting < upper and (ending > lower or starting == ending and starting >= lower)


def _instance(event: dict, content: dict, recurrence_id: dict | None) -> dict:
    return {
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


def _candidate_bounds(payload: dict, window_start: date, window_end: date) -> tuple[datetime, datetime]:
    all_day = "date" in payload["start"]
    if all_day:
        duration = date.fromisoformat(payload["end"]["date"]) - date.fromisoformat(payload["start"]["date"])
        return (datetime.combine(window_start - duration, time.min),
                datetime.combine(window_end, time.min))
    starting = datetime.fromisoformat(payload["start"]["dateTime"]).astimezone(_UTC)
    ending = datetime.fromisoformat(payload["end"]["dateTime"]).astimezone(_UTC)
    return (datetime.combine(window_start, time.min, tzinfo=_UTC) - (ending - starting),
            datetime.combine(window_end, time.min, tzinfo=_UTC))


def _expand_event(event: dict, window_start: date, window_end: date) -> list[dict]:
    payload = event["payload"]
    if not payload.get("recurrence"):
        content = payload
        return [_instance(event, content, None)] if _overlaps(content, window_start, window_end) else []

    all_day = "date" in payload["start"]
    zone_name = payload["start"].get("timeZone")
    rules = _rules(payload)
    exceptions = {original_start_key(item["originalStartTime"]): item for item in payload.get("exceptions", [])}
    lower, upper = _candidate_bounds(payload, window_start, window_end)
    instances = []
    for original in rules.xafter(lower, count=None, inc=True):
        if original > upper:
            break
        content = default_instance(payload, _calendar_time(original, all_day, zone_name))
        recurrence_id = content["start"]
        if original_start_key(recurrence_id) in exceptions:
            continue
        if _overlaps(content, window_start, window_end):
            instances.append(_instance(event, content, recurrence_id))
            if len(instances) > INSTANCE_LIMIT:
                break

    for exception in exceptions.values():
        original = default_instance(payload, exception["originalStartTime"])
        recurrence_id = original["start"]
        if not _contains(rules, _rule_time(recurrence_id, zone_name)):
            continue
        if exception.get("status") == "cancelled":
            continue
        if _overlaps(exception, window_start, window_end):
            instances.append(_instance(event, exception, recurrence_id))
    instances.sort(key=_sort_key)
    return instances[:INSTANCE_LIMIT + 1]


def _sort_key(instance: dict) -> tuple:
    value = instance["start"]
    if "date" in value:
        starting = datetime.combine(date.fromisoformat(value["date"]), time.min, tzinfo=_UTC)
    else:
        starting = datetime.fromisoformat(value["dateTime"]).astimezone(_UTC)
    recurrence_id = instance["recurrence_id"]
    identity = original_start_key(recurrence_id) if recurrence_id is not None else ""
    return starting, instance["event_id"], identity


def expand_events(events: list[dict], window_start: date, window_end: date) -> dict:
    """Return the earliest instances in the half-open date window, capped at 2000.

    Timed values sort by instant; an all-day date sorts as midnight UTC because a
    floating calendar date has no time zone of its own. The window end is exclusive.
    """
    instances = []
    truncated = False
    for event in events:
        instances.extend(_expand_event(event, window_start, window_end))
        if len(instances) > INSTANCE_LIMIT:
            truncated = True
            instances.sort(key=_sort_key)
            del instances[INSTANCE_LIMIT:]
    instances.sort(key=_sort_key)
    return {"instances": instances, "truncated": truncated}
