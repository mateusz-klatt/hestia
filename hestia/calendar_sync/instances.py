"""Bounded expansion of validated calendar payloads using recurring-ical-events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from heapq import nsmallest
import logging
from math import ceil
import re
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event as ICalEvent, vRecur
from recurring_ical_events import of

from .events import original_start_key


INSTANCE_LIMIT = 2000
MAX_REQUEST_EVENTS = 5000
OCCURRENCE_BOUND = 5000
REQUEST_INSTANCE_BUDGET = 50_000
# Shared request replay budget in dateutil day-check units across all events in a query.
# Sized at 600,000 so a normal household calendar expands without truncation (e.g. ~12
# yearly series from 1900 at ~46.8k each, ~63 daily series from 2000 at ~9.5k each,
# or a full 500-RDATE/500-EXDATE series) while keeping worst-case daily replay under ~1.3s.
REQUEST_REPLAY_BUDGET = 600_000
MAX_EXPLICIT_DATES = 500
MAX_RECURRING_DURATION = timedelta(days=31)
_MAX_INTERVAL = 1000
_UTC = timezone.utc
_MIN_UTC = datetime.min.replace(tzinfo=_UTC)
_MAX_UTC = datetime.max.replace(tzinfo=_UTC)
_MAX_ICAL_UTC = datetime(9999, 12, 31, 23, 59, 59, tzinfo=_UTC)
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
_DAYS_PER_PERIOD = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 31, "YEARLY": 366}
_CANDIDATES_PER_DAY = 1
_EXPLICIT_DATE_COST = 300  # Explicit RDATE/EXDATE property handling costs ~300x a day-check.
_OVERRIDE_COST = 100  # Indexing stored overrides/exceptions costs ~100x a day-check.
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Window:
    start: date
    end: date
    zone: ZoneInfo
    lower: datetime
    upper: datetime


@dataclass
class _ReplayBudget:
    remaining: int = REQUEST_REPLAY_BUDGET
    instances_remaining: int = REQUEST_INSTANCE_BUDGET


def _entry_order(entry: tuple) -> tuple:
    return entry[0], entry[1]


class _Collector:
    def __init__(self, window: _Window):
        self.window = window
        self.entries = []
        self.overflow = False

    def add(self, instance: dict):
        self.entries.append((_sort_key(instance, self.window), instance["instance_id"], instance))
        if len(self.entries) >= 2 * (INSTANCE_LIMIT + 1):
            self.entries = nsmallest(INSTANCE_LIMIT + 1, self.entries, key=_entry_order)
            self.overflow = True

    def values(self) -> list[dict]:
        if len(self.entries) > INSTANCE_LIMIT + 1:
            self.overflow = True
        return [entry[2] for entry in nsmallest(INSTANCE_LIMIT + 1, self.entries, key=_entry_order)]


def _key(value: dict, all_day: bool):
    if all_day:
        return date.fromisoformat(value["date"])
    return datetime.fromisoformat(value["dateTime"]).astimezone(_UTC)


def _midnight_utc(day: date, zone: ZoneInfo, lower: bool) -> datetime:
    try:
        return datetime.combine(day, time.min, tzinfo=zone).astimezone(_UTC)
    except OverflowError:
        return _MIN_UTC if lower else _MAX_UTC


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _make_window(start: date, end: date, zone_name: str) -> _Window:
    zone = _zone(zone_name)
    return _Window(start, end, zone, _midnight_utc(start, zone, True), _midnight_utc(end, zone, False))


def _parse_utc_property_line(line: str, kind: str) -> list | None:
    header, body = line.split(":", 1)
    if ";" in header and header.upper() != kind + ";VALUE=DATE":
        return None
    parsed = []
    try:
        for token in body.split(","):
            if "T" in token:
                if not token.endswith("Z"):
                    return None
                parsed.append(datetime.fromisoformat(token).astimezone(_UTC))
            else:
                parsed.append(date.fromisoformat(token))
    except ValueError:
        return None
    return parsed


def _property_values(payload: dict, kind: str) -> list:
    lines = [line for line in payload.get("recurrence", []) if _kind(line) == kind]
    if not lines:
        return []
    fast_values = []
    for line in lines:
        parsed = _parse_utc_property_line(line, kind)
        if parsed is None:
            break
        fast_values.extend(parsed)
    else:
        return fast_values
    event = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + "\r\n".join(lines) + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).walk("VEVENT")[0]
    props = event[kind]
    return [
        item.dt.astimezone(_UTC) if isinstance(item.dt, datetime) else item.dt
        for prop in (props if isinstance(props, list) else [props])
        for item in prop.dts
    ]


def _explicit_date_counts(payload: dict) -> tuple[int, int]:
    rdate_count = 0
    exdate_count = 0
    for line in payload.get("recurrence", []):
        kind = _kind(line)
        if kind == "RDATE":
            rdate_count += line.split(":", 1)[1].count(",") + 1
        elif kind == "EXDATE":
            exdate_count += line.split(":", 1)[1].count(",") + 1
    return rdate_count, exdate_count


def _kind(line: str) -> str:
    return line.split(":", 1)[0].split(";", 1)[0]


def _build_calendar(payload: dict, uid: str, rdates: list, all_day: bool) -> tuple[Calendar, datetime | None]:
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", "-//Hestia//Calendar Sync//EN")
    event = ICalEvent()
    event.add("uid", uid)
    event.add("summary", payload.get("summary", ""))
    event.add("location", payload.get("location", ""))
    event.add("status", payload.get("status", "confirmed").upper())
    if all_day:
        event.add("dtstart", date.fromisoformat(payload["start"]["date"]))
        event.add("dtend", date.fromisoformat(payload["end"]["date"]))
    else:
        start = payload["start"]
        zone = _zone(start.get("timeZone") or "UTC")
        event.add("dtstart", datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00")).astimezone(zone))
        event.add("dtend", _key(payload["end"], False))
    until = None
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        rule = vRecur.from_ical(line.split(":", 1)[1])
        if not all_day and "UNTIL" in rule:
            until = rule["UNTIL"][0].astimezone(_UTC)
            # Keep UNTIL + 1 day padding so dateutil stops at series end while DST gap shifts still yield.
            rule["UNTIL"] = [min(_shift_datetime(until, timedelta(days=1)), _MAX_ICAL_UTC)]
        event.add("rrule", rule)
    if rdates:
        event.add("rdate", rdates)
    calendar.add_component(event)
    return calendar, until


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


def _event_duration(payload: dict) -> timedelta:
    if "date" in payload["start"]:
        return date.fromisoformat(payload["end"]["date"]) - date.fromisoformat(payload["start"]["date"])
    return _key(payload["end"], False) - _key(payload["start"], False)


def _occurrence_instance(
    event: dict, original, duration: timedelta, window: _Window,
    start_zone: ZoneInfo | None, end_zone: ZoneInfo | None,
) -> dict | None:
    payload = event["payload"]
    if payload.get("status") == "cancelled":
        return None
    try:
        ending = original + duration
        if start_zone is None:
            if not (original < window.end and ending > window.start):
                return None
            start_dict = {"date": original.isoformat()}
            end_dict = {"date": ending.isoformat()}
        else:
            if not (original < window.upper and (ending > window.lower or original == ending and original >= window.lower)):
                return None
            start_zone_name = payload["start"].get("timeZone") or "UTC"
            end_zone_name = payload["end"]["timeZone"]
            start_dict = {"dateTime": original.astimezone(start_zone).isoformat(timespec="seconds"), "timeZone": start_zone_name}
            end_dict = {"dateTime": ending.astimezone(end_zone).isoformat(timespec="seconds"), "timeZone": end_zone_name}
    except OverflowError:
        return None
    return {
        "instance_id": event["id"] + "/" + original.isoformat(),
        "event_id": event["id"],
        "revision": event["revision"],
        "group": event["group_id"],
        "summary": payload.get("summary", ""),
        "location": payload.get("location", ""),
        "start": start_dict,
        "end": end_dict,
        "all_day": start_zone is None,
        "status": payload.get("status", "confirmed"),
        "recurrence_id": start_dict,
    }


def _stored_override(event: dict, exception: dict, original) -> dict | None:
    if not isinstance(original, datetime):
        recurrence_id = {"date": original.isoformat()}
    else:
        zone_name = event["payload"]["start"]["timeZone"]
        recurrence_id = {"dateTime": original.astimezone(_zone(zone_name)).isoformat(timespec="seconds"),
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
) -> tuple[int, ...] | None:
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
    return tuple(values)


def _start_date(payload: dict) -> date:
    start = payload["start"]
    if "date" in start:
        return date.fromisoformat(start["date"])
    zone_name = start.get("timeZone") or "UTC"
    return datetime.fromisoformat(start["dateTime"]).astimezone(_zone(zone_name)).date()


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
    months: tuple[int, ...] = ()
    if "BYMONTH" in parts:
        parsed_months = _parse_int_list(parts["BYMONTH"], 1, 12)
        if parsed_months is None:
            return "uses invalid BYMONTH parameter"
        months = parsed_months
    month_days: tuple[int, ...] = ()
    if "BYMONTHDAY" in parts:
        parsed_month_days = _parse_int_list(parts["BYMONTHDAY"], 1, 31, allow_negative=True)
        if parsed_month_days is None:
            return "uses invalid BYMONTHDAY parameter"
        month_days = parsed_month_days
    plain_days: set[str] = set()
    ordinal_day: int | None = None
    if "BYDAY" in parts:
        parsed_byday = _parse_byday(parts["BYDAY"])
        if parsed_byday is None:
            return "uses unsupported BYDAY parameter"
        plain_days, ordinal_day = parsed_byday
    setpos: tuple[int, ...] = ()
    if "BYSETPOS" in parts:
        parsed_setpos = _parse_int_list(parts["BYSETPOS"], 1, 28, allow_negative=True)
        if parsed_setpos is None:
            return "uses out-of-range BYSETPOS parameter"
        setpos = parsed_setpos

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
    if frequency == "YEARLY" and len(months) == 1:
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
    if starting in set(_property_values(payload, "EXDATE")):
        return None
    recurrence_id = payload["start"] if payload.get("recurrence") else None
    return _instance(event, content, recurrence_id)


def _second_fold_start(payload: dict) -> datetime | None:
    if "date" in payload["start"] or not payload.get("recurrence"):
        return None
    zone = _zone(payload["start"]["timeZone"])
    local = datetime.fromisoformat(payload["start"]["dateTime"]).astimezone(zone)
    if local.fold == 1 and local.replace(fold=0).utcoffset() != local.utcoffset():
        return local.astimezone(_UTC)
    return None


def _shift_date(day: date, days: int) -> date:
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return date.min if days < 0 else date.max


def _shift_datetime(moment: datetime, delta: timedelta) -> datetime:
    try:
        return moment + delta
    except OverflowError:
        return _MIN_UTC if delta < timedelta(0) else _MAX_UTC


def _query_bounds(window: _Window) -> tuple[datetime, datetime]:
    return _shift_datetime(window.lower, -timedelta(days=1)), _shift_datetime(window.upper, timedelta(days=1))


def _until_bound(raw: str, end: datetime) -> datetime:
    until_dt = (
        datetime.fromisoformat(raw).astimezone(_UTC)
        if "T" in raw else datetime.combine(date.fromisoformat(raw), time.min, tzinfo=_UTC)
    )
    return min(end, _shift_datetime(until_dt, timedelta(days=1)))


def _occurrence_upper_bound(payload: dict, start: datetime, end: datetime, rdate_count: int) -> int:
    bound = 1 + rdate_count + len(payload.get("exceptions", []))
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        parts = _rrule_parts(line)
        rule_end = _until_bound(parts["UNTIL"], end) if "UNTIL" in parts else end
        seconds = max(0, (rule_end - start).total_seconds())
        interval = int(parts.get("INTERVAL", "1"))
        estimated = ceil(seconds / (_FREQUENCY_SECONDS[parts["FREQ"]] * interval)) + 1
        bound += min(int(parts["COUNT"]), estimated) if "COUNT" in parts else estimated
    return bound


def _replay_upper_bound(payload: dict, end: datetime, query_start: datetime | None = None) -> int:
    # Keep age-proportional cost from min(DTSTART, lookback): re-anchoring DTSTART near the
    # window changes COUNT, INTERVAL, and phase alignment, so it is not equivalent.
    all_day = "date" in payload["start"]
    starting = _key(payload["start"], all_day)
    if all_day:
        starting = datetime.combine(starting, time.min, tzinfo=_UTC)
    anchor = starting
    if query_start is not None:
        lookback = _shift_datetime(query_start, -_event_duration(payload))
        if lookback < anchor:
            anchor = lookback
    rdate_count, exdate_count = _explicit_date_counts(payload)
    exception_count = len(payload.get("exceptions", []))
    bound = (rdate_count + exdate_count) * _EXPLICIT_DATE_COST + exception_count * _OVERRIDE_COST
    for line in payload.get("recurrence", []):
        if _kind(line) != "RRULE":
            continue
        parts = _rrule_parts(line)
        frequency = parts["FREQ"]
        rule_end = _until_bound(parts["UNTIL"], end) if "UNTIL" in parts else end
        seconds = max(0, (rule_end - anchor).total_seconds())
        periods = ceil(seconds / (_FREQUENCY_SECONDS[frequency] * int(parts.get("INTERVAL", "1")))) + 1
        bound += periods * _DAYS_PER_PERIOD[frequency] * _CANDIDATES_PER_DAY
    return bound


def _expand_event(event: dict, window: _Window, budget: _ReplayBudget | None = None) -> tuple[list[dict], bool]:
    payload = event["payload"]
    if budget is None:
        budget = _ReplayBudget()
    if not payload.get("recurrence"):
        item = _instance(event, payload, None) if _overlaps(payload, window) else None
        if item is not None:
            budget.instances_remaining -= 1
            return [item], False
        return [], False

    rdate_count, exdate_count = _explicit_date_counts(payload)
    exception_count = len(payload.get("exceptions", []))
    pre_map_cost = (rdate_count + exdate_count) * _EXPLICIT_DATE_COST + exception_count * _OVERRIDE_COST
    if pre_map_cost > budget.remaining:
        _LOG.warning("Calendar event %s exceeds remaining explicit-date/override budget; omitting event", event["id"])
        return [], True

    def dtstart_only() -> tuple[list[dict], bool]:
        budget.remaining -= exdate_count * _EXPLICIT_DATE_COST + exception_count * _OVERRIDE_COST
        item = _first_instance(event, window)
        if item is not None:
            budget.instances_remaining -= 1
            return [item], True
        return [], True

    rrules = [line for line in payload.get("recurrence", []) if _kind(line) == "RRULE"]
    if len(rrules) > 1:
        _LOG.warning("Calendar event %s has multiple RRULEs; using DTSTART only", event["id"])
        return dtstart_only()
    duration = _event_duration(payload)
    if duration > MAX_RECURRING_DURATION:
        _LOG.warning("Calendar event %s exceeds the 31-day recurring duration limit; using DTSTART only", event["id"])
        return dtstart_only()
    reason = _unsupported_rule_reason(payload)
    if reason is not None:
        _LOG.warning("Calendar event %s %s; using DTSTART only", event["id"], reason)
        return dtstart_only()
    if rdate_count > MAX_EXPLICIT_DATES or exdate_count > MAX_EXPLICIT_DATES:
        _LOG.warning("Calendar event %s exceeds the 500 explicit recurrence date limit; using DTSTART only", event["id"])
        return dtstart_only()
    all_day = "date" in payload["start"]
    if all_day:
        first, last = _shift_date(window.start, -1), _shift_date(window.end, 1)
        query_start = datetime.combine(first, time.min, tzinfo=_UTC)
        try:
            query_end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=_UTC)
        except OverflowError:
            query_end = _MAX_UTC
        lookback_day = _shift_date(first, -duration.days)
    else:
        query_start, query_end = _query_bounds(window)
    lookback_start = _shift_datetime(query_start, -duration)
    if _occurrence_upper_bound(payload, lookback_start, query_end, rdate_count) > OCCURRENCE_BOUND:
        _LOG.warning("Calendar event %s exceeds the instance occurrence bound; using DTSTART only", event["id"])
        return dtstart_only()
    replay_bound = _replay_upper_bound(payload, query_end, query_start)
    if replay_bound > budget.remaining:
        _LOG.warning("Calendar event %s exceeds the remaining request replay bound; using DTSTART only", event["id"])
        return dtstart_only()
    budget.remaining -= replay_bound

    exceptions = {}
    pending = {}
    for item in payload.get("exceptions", []):
        original = _key(item["originalStartTime"], all_day)
        if _overlaps(item, window):
            pending[original] = item
            exceptions[original] = item
        elif (lookback_day <= original <= last) if all_day else (lookback_start <= original < query_end):
            exceptions[original] = item
    rdates = _property_values(payload, "RDATE")
    exdates = set(_property_values(payload, "EXDATE"))

    starting = _key(payload["start"], all_day)
    explicit = {starting, *rdates}
    seen_overrides = set()
    series_slots = set()
    second_fold = _second_fold_start(payload)
    matches = []
    identifiers = set()
    start_zone = None if all_day else _zone(payload["start"].get("timeZone") or "UTC")
    end_zone = None if all_day else _zone(payload["end"]["timeZone"])

    def record(instance):
        if instance is not None and instance["instance_id"] not in identifiers:
            identifiers.add(instance["instance_id"])
            matches.append(instance)

    def budget_exhausted():
        if budget.instances_remaining > 0:
            return False
        _LOG.warning("Calendar request exceeded %d constructed instance limit; truncating response",
                     REQUEST_INSTANCE_BUDGET)
        return True

    def sort_key(instance):
        return _sort_key(instance, window)

    calendar, until = _build_calendar(payload, event["uid"], rdates, all_day)
    for series in of(calendar).series:
        for occurrence in series.between(query_start, query_end):
            if budget_exhausted():
                return nsmallest(INSTANCE_LIMIT + 1, matches, key=sort_key), True
            # Charge every candidate yielded by the library (including discarded ones).
            budget.instances_remaining -= 1
            original = occurrence.start if all_day else occurrence.start.astimezone(_UTC)
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
                instance = _occurrence_instance(event, original, duration, window, start_zone, end_zone)
            if instance is not None and second_fold is not None and original not in explicit:
                zone = start_zone
                original_wall = datetime.fromisoformat(payload["start"]["dateTime"]).astimezone(zone).replace(tzinfo=None)
                candidate_wall = original.astimezone(zone).replace(tzinfo=None)
                if original != second_fold and candidate_wall == original_wall:
                    continue
            record(instance)
    # RFC 5545 treats DTSTART as an unconditional occurrence even when UNTIL precedes DTSTART or DST fold skips it.
    extra_instances = []
    if (
        starting not in series_slots
        and starting not in exdates
        and starting not in exceptions
        and payload.get("status") != "cancelled"
        and _overlaps(payload, window)
    ):
        extra_instances.append(_instance(event, payload, payload["start"]))
    for original, exception in pending.items():
        if original in seen_overrides or original in exdates:
            continue
        if original not in explicit and original not in series_slots:
            near = first <= original <= last if all_day else query_start <= original < query_end
            if near:
                _LOG.debug("Skipping orphan override for calendar event %s at %s", event["id"], original.isoformat())
                continue
        extra_instances.append(_stored_override(event, exception, original))
    for item in extra_instances:
        if budget_exhausted():
            return nsmallest(INSTANCE_LIMIT + 1, matches, key=sort_key), True
        record(item)
        budget.instances_remaining -= 1
    return nsmallest(INSTANCE_LIMIT + 1, matches, key=sort_key), False


def _sort_key(instance: dict, window: _Window) -> tuple:
    value = instance["start"]
    if "date" in value:
        starting = _midnight_utc(date.fromisoformat(value["date"]), window.zone, True)
    else:
        starting = datetime.fromisoformat(value["dateTime"]).astimezone(_UTC)
    prefix = instance["event_id"]
    identity = instance["instance_id"][len(prefix) + 1:] if instance["recurrence_id"] is not None else ""
    return starting, prefix, identity


def expand_events(events: list[dict], window_start: date, window_end: date, zone_name: str = "UTC") -> dict:
    """Return the earliest instances in the half-open local-date window, capped at 2000."""
    if window_start >= window_end:
        return {"instances": [], "truncated": False}
    window = _make_window(window_start, window_end, zone_name)
    empty_timed_window = window.lower >= window.upper
    collector = _Collector(window)
    budget = _ReplayBudget(REQUEST_REPLAY_BUDGET)
    incomplete = False
    for processed, event in enumerate(events):
        if processed >= MAX_REQUEST_EVENTS or budget.instances_remaining <= 0:
            _LOG.warning("Calendar request exceeded processing limit; truncating response")
            incomplete = True
            break
        if empty_timed_window and "date" not in event["payload"]["start"]:
            continue
        try:
            expanded, event_incomplete = _expand_event(event, window, budget)
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
