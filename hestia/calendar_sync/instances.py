"""Bounded expansion of validated calendar payloads using recurring-ical-events."""
from __future__ import annotations

from bisect import insort
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from math import ceil
import re
from zoneinfo import ZoneInfo

from icalendar import Calendar, vDDDTypes, vRecur
from recurring_ical_events import of

from .events import original_start_key, to_ical


INSTANCE_LIMIT = 2000
OCCURRENCE_BOUND = 5000
REPLAY_BOUND = 200000
_MAX_INTERVAL = 1000
_UTC = timezone.utc
_MIN_UTC = datetime.min.replace(tzinfo=_UTC)
_MAX_UTC = datetime.max.replace(tzinfo=_UTC)
# Enumerated allowlist of simple Google/Outlook RRULE shapes; anything outside S1-S7 falls back to DTSTART.
_ALLOWED_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
_ALLOWED_RULE_KEYS = {
    "FREQ", "INTERVAL", "COUNT", "UNTIL", "WKST",
    "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS",
}
_BY_KEYS = {"BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS"}
_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
_BYDAY_RE = re.compile(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)\Z")
_MONTH_DAY_LIMITS = {2: 29, 4: 30, 6: 30, 9: 30, 11: 30}
_FREQUENCY_SECONDS = {
    "DAILY": 86400,
    "WEEKLY": 7 * 86400,
    "MONTHLY": 28 * 86400,
    "YEARLY": 365 * 86400,
}
_DAY_CANDIDATES = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31, "YEARLY": 366}
_DAY_SELECTORS = {"BYDAY", "BYMONTHDAY"}
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


def _rrule_parts(line: str) -> dict[str, str] | None:
    _, body = line.split(":", 1)
    parts: dict[str, str] = {}
    for item in body.upper().split(";"):
        if "=" not in item:
            return None
        key, value = item.split("=", 1)
        if key in parts:
            return None
        parts[key] = value
    return parts


def _parse_int_list(
    raw: str, minimum: int, maximum: int | None = None, *, allow_negative: bool = False
) -> list[int] | None:
    values = []
    for token in raw.split(","):
        try:
            value = int(token)
        except ValueError:
            return None
        magnitude = abs(value) if allow_negative else value
        if magnitude < minimum or (maximum is not None and magnitude > maximum):
            return None
        values.append(value)
    return values


def _start_date(payload: dict) -> date:
    start = payload["start"]
    if "date" in start:
        return date.fromisoformat(start["date"])
    zone_name = start.get("timeZone") or "UTC"
    return datetime.fromisoformat(start["dateTime"]).astimezone(ZoneInfo(zone_name)).date()


def _parse_byday(raw: str) -> tuple[set[str], int | None] | None:
    plain: set[str] = set()
    ordinals: list[int] = []
    for token in raw.split(","):
        match = _BYDAY_RE.fullmatch(token)
        if match is None:
            return None
        ordinal, weekday = match.groups()
        if ordinal is None:
            plain.add(weekday)
        else:
            value = int(ordinal)
            if value == 0:
                return None
            ordinals.append(value)
    if ordinals:
        if plain or len(ordinals) != 1:
            return None
        return set(), ordinals[0]
    return plain, None


def _shape_rejection_reason(
    frequency: str, interval: int, parts: dict[str, str], start_date: date
) -> str | None:
    months = _parse_int_list(parts["BYMONTH"], 1, 12) if "BYMONTH" in parts else None
    if "BYMONTH" in parts and months is None:
        return "uses invalid BYMONTH parameter"
    month_days = _parse_int_list(parts["BYMONTHDAY"], 1, 31, allow_negative=True) if "BYMONTHDAY" in parts else None
    if "BYMONTHDAY" in parts and month_days is None:
        return "uses invalid BYMONTHDAY parameter"
    plain_days, ordinal_day = set(), None
    if "BYDAY" in parts:
        parsed_byday = _parse_byday(parts["BYDAY"])
        if parsed_byday is None:
            return "uses unsupported BYDAY parameter"
        plain_days, ordinal_day = parsed_byday
    setpos = _parse_int_list(parts["BYSETPOS"], 1, 28, allow_negative=True) if "BYSETPOS" in parts else None
    if "BYSETPOS" in parts and setpos is None:
        return "uses out-of-range BYSETPOS parameter"

    by_keys = set(parts) & _BY_KEYS
    if not by_keys:
        # S1: no BY*; implicit DTSTART day recurs within 2 months (8 years for Feb 29) at INTERVAL=1.
        if frequency in {"MONTHLY", "YEARLY"} and interval > 1 and start_date.day >= 29:
            return "uses DTSTART day 29-31 with INTERVAL > 1"
        return None
    if frequency == "WEEKLY" and by_keys == {"BYDAY"} and ordinal_day is None:
        # S2: WEEKLY plain BYDAY; every week contains every weekday.
        return None
    if frequency == "DAILY" and by_keys == {"BYDAY"} and ordinal_day is None and interval == 1:
        # S3: DAILY plain BYDAY at INTERVAL=1; every 7 days hits every weekday.
        return None
    if frequency == "MONTHLY":
        if by_keys == {"BYMONTHDAY"} and len(month_days) == 1:
            # S4: MONTHLY single BYMONTHDAY; <=28 exists every month, 29-31 recurs within 2 months at INTERVAL=1.
            if abs(month_days[0]) >= 29 and interval > 1:
                return "uses day 29-31 selector with INTERVAL > 1"
            return None
        if by_keys == {"BYDAY"}:
            if ordinal_day is None:
                # S5(a): MONTHLY plain BYDAY; every month contains every weekday.
                return None
            # S5(b): MONTHLY single ordinal BYDAY; +-1..+-4 exist every month, +-5 recurs at INTERVAL=1.
            if abs(ordinal_day) <= (4 if interval > 1 else 5):
                return None
        if by_keys == {"BYDAY", "BYSETPOS"} and ordinal_day is None and len(setpos) == 1:
            # S6: MONTHLY plain BYDAY with single BYSETPOS; every month has >=4 of each weekday.
            if abs(setpos[0]) <= 4 * len(plain_days):
                return None
    if frequency == "YEARLY" and months is not None and len(months) == 1:
        month = months[0]
        if by_keys == {"BYMONTH", "BYMONTHDAY"} and len(month_days) == 1:
            # S7(a): YEARLY single BYMONTH+BYMONTHDAY; valid day hits yearly (Feb 29 within 8 years at INTERVAL=1).
            day = abs(month_days[0])
            if day <= _MONTH_DAY_LIMITS.get(month, 31) and not (month == 2 and day == 29 and interval > 1):
                return None
        if by_keys == {"BYMONTH", "BYDAY"}:
            if ordinal_day is None:
                # S7(c): YEARLY single BYMONTH with plain BYDAY; every visited month contains every weekday.
                return None
            # S7(b): YEARLY single BYMONTH with single ordinal BYDAY; +-1..+-4 exist yearly, +-5 recurs at INTERVAL=1.
            if abs(ordinal_day) <= (4 if interval > 1 else 5):
                return None
    return "uses unsupported recurrence shape"


def _rrule_rejection_reason(parts: dict[str, str] | None, start_date: date) -> str | None:
    if parts is None:
        return "has malformed recurrence parameters"
    frequency = parts.get("FREQ")
    if frequency not in _ALLOWED_FREQUENCIES:
        return f"uses unsupported recurrence frequency {frequency or 'missing'}"
    disallowed = set(parts) - _ALLOWED_RULE_KEYS
    if disallowed:
        return f"uses unsupported recurrence parameters ({', '.join(sorted(disallowed))})"
    if "COUNT" in parts and "UNTIL" in parts:
        return "uses both COUNT and UNTIL"
    interval = 1
    if "INTERVAL" in parts:
        parsed = _parse_int_list(parts["INTERVAL"], 1, _MAX_INTERVAL)
        if parsed is None or len(parsed) != 1:
            return "uses invalid or oversized INTERVAL parameter"
        interval = parsed[0]
    if "COUNT" in parts:
        parsed = _parse_int_list(parts["COUNT"], 1)
        if parsed is None or len(parsed) != 1:
            return "uses invalid COUNT parameter"
    if "WKST" in parts and parts["WKST"] not in _WEEKDAYS:
        return "uses invalid WKST parameter"
    return _shape_rejection_reason(frequency, interval, parts, start_date)


def _unsupported_rule_reason(payload: dict) -> str | None:
    start_date = _start_date(payload)
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        reason = _rrule_rejection_reason(_rrule_parts(line), start_date)
        if reason is not None:
            return reason
    return None


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


def _shift_date(day: date, days: int) -> date:
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return date.min if days < 0 else date.max


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
        parts = _rrule_parts(line)
        interval = int(parts.get("INTERVAL", "1"))
        estimated = ceil(seconds / (_FREQUENCY_SECONDS[parts["FREQ"]] * interval)) + 1
        bound += min(int(parts["COUNT"]), estimated) if "COUNT" in parts else estimated
    return bound


def _replay_upper_bound(payload: dict, end: datetime) -> int:
    all_day = "date" in payload["start"]
    starting = _key(payload["start"], all_day)
    if all_day:
        starting = datetime.combine(starting, time.min, tzinfo=_UTC)
    seconds = max(0, (end - starting).total_seconds())
    bound = 0
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        parts = _rrule_parts(line)
        frequency = parts["FREQ"]
        periods = ceil(seconds / (_FREQUENCY_SECONDS[frequency] * int(parts.get("INTERVAL", "1")))) + 1
        per_period = _DAY_CANDIDATES[frequency] if _DAY_SELECTORS.intersection(parts) else 1
        bound += periods * per_period
    return bound


def _expand_event(event: dict, window: _Window) -> tuple[list[dict], bool]:
    payload = event["payload"]
    if not payload.get("recurrence"):
        return ([_instance(event, payload, None)] if _overlaps(payload, window) else []), False
    reason = _unsupported_rule_reason(payload)
    if reason is not None:
        _LOG.warning("Calendar event %s %s; using DTSTART only", event["id"], reason)
        first = _first_instance(event, window)
        return ([first] if first is not None else []), True
    all_day = "date" in payload["start"]
    if all_day:
        first, last = _shift_date(window.start, -1), _shift_date(window.end, 1)
        query_start = datetime.combine(first, time.min, tzinfo=_UTC)
        try:
            query_end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=_UTC)
        except OverflowError:
            query_end = _MAX_UTC
    else:
        query_start, query_end = _query_bounds(window)
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
            near = first <= original <= last if all_day else query_start <= original <= query_end
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
