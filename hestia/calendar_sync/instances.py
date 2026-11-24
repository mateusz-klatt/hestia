"""Bounded expansion of validated calendar payloads into concrete occurrences."""
from __future__ import annotations

from bisect import insort
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar, vRecur

from .events import default_instance, original_start_key


INSTANCE_LIMIT = 2000
WORK_LIMIT = 20000
_UTC = timezone.utc
_MIN_UTC = datetime.min.replace(tzinfo=_UTC)
_MAX_UTC = datetime.max.replace(tzinfo=_UTC)
_TIME_SELECTORS = {"BYHOUR", "BYMINUTE", "BYSECOND"}
_SEEK_STEPS = {
    "DAILY": timedelta(days=1),
    "WEEKLY": timedelta(weeks=1),
    "HOURLY": timedelta(hours=1),
    "MINUTELY": timedelta(minutes=1),
    "SECONDLY": timedelta(seconds=1),
}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Window:
    start: date
    end: date
    zone_name: str
    zone: ZoneInfo
    lower: datetime
    upper: datetime


@dataclass(frozen=True)
class _Rule:
    rule: object
    wall_start: datetime
    frequency: str
    interval: int
    count: int | None
    until: date | datetime | None


@dataclass
class _Series:
    payload: dict
    all_day: bool
    zone_name: str | None
    zone: ZoneInfo | None
    explicit: set
    exdates: set
    rules: list[_Rule]


class _Work:
    def __init__(self):
        self.remaining = WORK_LIMIT
        self.exhausted = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


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
    return _Window(start, end, zone_name, zone, _midnight_utc(start, zone, True),
                   _midnight_utc(end, zone, False))


def _subtract(value, delta):
    try:
        return value - delta
    except OverflowError:
        if isinstance(value, datetime):
            return datetime.min.replace(tzinfo=value.tzinfo)
        return date.min


def _wall(value: datetime, zone: ZoneInfo) -> datetime:
    try:
        return value.astimezone(zone).replace(tzinfo=None)
    except OverflowError:
        return datetime.min if value.year == 1 else datetime.max


def _wall_key(value: datetime, all_day: bool, zone: ZoneInfo | None):
    if all_day:
        return value.date()
    early = value.replace(tzinfo=zone, fold=0)
    late = value.replace(tzinfo=zone, fold=1)
    if early.utcoffset() < late.utcoffset():
        return None
    try:
        return early.astimezone(_UTC)
    except OverflowError:
        return None


def _property_keys(line: str, kind: str, all_day: bool) -> set:
    event = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + line + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).walk("VEVENT")[0]
    result = set()
    for item in event[kind].dts:
        value = item.dt
        result.add(value.astimezone(_UTC) if isinstance(value, datetime) else value)
    return result


def _series(payload: dict) -> _Series:
    all_day = "date" in payload["start"]
    zone_name = payload["start"].get("timeZone")
    zone = None if all_day else ZoneInfo(zone_name)
    starting = _key(payload["start"], all_day)
    wall_start = (datetime.combine(starting, time.min) if all_day
                  else starting.astimezone(zone).replace(tzinfo=None))
    explicit = {starting}
    exdates = set()
    rules = []
    for line in payload.get("recurrence", []):
        name, body = line.split(":", 1)
        kind = name.split(";", 1)[0]
        if kind in {"RDATE", "EXDATE"}:
            values = _property_keys(line, kind, all_day)
            if kind == "RDATE":
                explicit.update(values)
            else:
                exdates.update(values)
            continue
        parts = dict(part.split("=", 1) for part in body.split(";"))
        count = int(parts["COUNT"]) if "COUNT" in parts else None
        until_values = vRecur.from_ical(body).get("UNTIL")
        until = until_values[0] if until_values else None
        frequency = parts["FREQ"]
        interval = int(parts.get("INTERVAL", "1"))
        clean = [part for part in body.split(";")
                 if part.split("=", 1)[0] not in ({"COUNT", "UNTIL"} | (_TIME_SELECTORS if all_day else set()))]
        rules.append(_Rule(rrulestr(";".join(clean), dtstart=wall_start), wall_start,
                           frequency, interval, count, until))
    return _Series(payload, all_day, zone_name, zone, explicit, exdates, rules)


def _shift(rule: _Rule, periods: int) -> datetime | None:
    try:
        if rule.frequency in _SEEK_STEPS:
            return rule.wall_start + periods * rule.interval * _SEEK_STEPS[rule.frequency]
        if rule.frequency == "MONTHLY":
            months = rule.wall_start.year * 12 + rule.wall_start.month - 1 + periods * rule.interval
            year, month = divmod(months, 12)
            return rule.wall_start.replace(year=year, month=month + 1)
        if rule.frequency == "YEARLY":
            return rule.wall_start.replace(year=rule.wall_start.year + periods * rule.interval)
    except (OverflowError, ValueError):
        return None
    return None


def _seek_rule(rule: _Rule, lower, all_day: bool, zone: ZoneInfo | None):
    if rule.count is not None:
        return rule.rule
    lower_wall = datetime.combine(lower, time.min) if all_day else _wall(lower, zone)
    if lower_wall <= rule.wall_start:
        return rule.rule
    if rule.frequency in _SEEK_STEPS:
        step = rule.interval * _SEEK_STEPS[rule.frequency]
        periods = (lower_wall - rule.wall_start) // step - 1
    elif rule.frequency == "MONTHLY":
        months = (lower_wall.year - rule.wall_start.year) * 12 + lower_wall.month - rule.wall_start.month
        periods = months // rule.interval - 1
    elif rule.frequency == "YEARLY":
        periods = (lower_wall.year - rule.wall_start.year) // rule.interval - 1
    else:
        return rule.rule
    while periods > 0:
        candidate = _shift(rule, periods)
        if candidate is not None and rule.wall_start <= candidate < lower_wall:
            return rule.rule.replace(dtstart=candidate)
        periods -= 1
    return rule.rule


def _rule_slots(rule: _Rule, series: _Series, lower, upper, work: _Work):
    lower_wall = datetime.combine(lower, time.min) if series.all_day else _wall(lower, series.zone)
    upper_wall = datetime.combine(upper, time.min) if series.all_day else _wall(upper, series.zone)
    until_wall = None
    if rule.until is not None:
        until_wall = (datetime.combine(rule.until, time.min) if series.all_day
                      else _wall(rule.until.astimezone(_UTC), series.zone))
    generated = _seek_rule(rule, lower, series.all_day, series.zone)
    produced = 0
    seen_dates = set()
    for wall in generated:
        if not work.take():
            return
        key = _wall_key(wall, series.all_day, series.zone)
        if wall > upper_wall or until_wall is not None and wall > until_wall:
            return
        if key is None or rule.until is not None and not series.all_day and key > rule.until:
            continue
        if series.all_day and key in seen_dates:
            continue
        seen_dates.add(key)
        produced += 1
        if lower <= key <= upper:
            yield key
        if rule.count is not None and produced >= rule.count:
            return


def _contains(series: _Series, key, known: set, work: _Work) -> bool:
    if key in series.exdates:
        return False
    if key in series.explicit or key in known:
        return True
    for rule in series.rules:
        for candidate in _rule_slots(rule, series, key, key, work):
            if candidate == key:
                return True
        if work.exhausted:
            return False
    return False


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


def _content_at(series: _Series, key) -> dict | None:
    try:
        if series.all_day:
            value = {"date": key.isoformat()}
        else:
            value = {"dateTime": key.astimezone(series.zone).isoformat(), "timeZone": series.zone_name}
        return default_instance(series.payload, value)
    except OverflowError:
        return None


def _candidate_bounds(series: _Series, window: _Window):
    if series.all_day:
        duration = date.fromisoformat(series.payload["end"]["date"]) - date.fromisoformat(series.payload["start"]["date"])
        return _subtract(window.start, duration), window.end
    starting = datetime.fromisoformat(series.payload["start"]["dateTime"]).astimezone(_UTC)
    ending = datetime.fromisoformat(series.payload["end"]["dateTime"]).astimezone(_UTC)
    return _subtract(window.lower, ending - starting), window.upper


def _expand_event(event: dict, window: _Window) -> tuple[list[dict], bool]:
    payload = event["payload"]
    if payload.get("status") == "cancelled":
        return [], False
    if not payload.get("recurrence"):
        return ([_instance(event, payload, None)] if _overlaps(payload, window) else []), False

    series = _series(payload)
    exceptions = {_key(item["originalStartTime"], series.all_day): item for item in payload.get("exceptions", [])}
    lower, upper = _candidate_bounds(series, window)
    collector = _Collector(window)
    work = _Work()
    known = set()
    seen = set()

    def process(key) -> bool:
        if key in seen:
            return False
        seen.add(key)
        if key in series.exdates:
            return False
        known.add(key)
        if key in exceptions:
            return False
        content = _content_at(series, key)
        if content is None or not _overlaps(content, window):
            return False
        collector.add(_instance(event, content, content["start"]))
        return True

    for key in sorted(series.explicit):
        if lower <= key <= upper:
            process(key)
    for rule in series.rules:
        matches = 0
        for key in _rule_slots(rule, series, lower, upper, work):
            matches += process(key)
            if matches > INSTANCE_LIMIT:
                break
        if work.exhausted:
            break

    for key, exception in exceptions.items():
        if exception.get("status") == "cancelled" or not _overlaps(exception, window):
            continue
        if not _contains(series, key, known, work):
            continue
        original = _content_at(series, key)
        if original is not None:
            collector.add(_instance(event, exception, original["start"]))
    if work.exhausted:
        _LOG.warning("Calendar event %s reached the instance expansion work budget", event.get("id", "unknown"))
    return collector.values(), collector.overflow or work.exhausted


def _first_instance(event: dict, window: _Window) -> dict | None:
    payload = event["payload"]
    if payload.get("status") == "cancelled":
        return None
    all_day = "date" in payload["start"]
    starting = _key(payload["start"], all_day)
    for line in payload.get("recurrence", []):
        name = line.split(":", 1)[0].split(";", 1)[0]
        if name == "EXDATE" and starting in _property_keys(line, name, all_day):
            return None
    content = payload
    for exception in payload.get("exceptions", []):
        if _key(exception["originalStartTime"], all_day) == starting:
            if exception.get("status") == "cancelled":
                return None
            content = exception
            break
    if not _overlaps(content, window):
        return None
    recurrence_id = payload["start"] if payload.get("recurrence") else None
    return _instance(event, content, recurrence_id)


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
