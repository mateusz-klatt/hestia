"""Pure CMP ``/GetScheduler`` response parser.

Normalizes all slot timestamps into ``Europe/Warsaw`` (DST-safe) and rejects malformed
or unavailable entries without exposing raw response bodies in errors.
Wire field names reflect Slice 1 synthetic assumptions documented in ``docs/APPOINTMENTS.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re

from .config import WARSAW_TZ, parse_strict_json, parse_warsaw_datetime

MAX_SLOTS = 5000
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_WALL_TIME_RE = re.compile(r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?\Z", re.ASCII)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z", re.ASCII)


class CmpParseError(ValueError):
    """Fixed safe diagnostic for structurally invalid CMP responses."""


@dataclass(frozen=True)
class AppointmentSlot:
    """One normalized available appointment slot in ``Europe/Warsaw``."""

    provider: str
    start: datetime
    end: datetime | None = None
    doctor_id: str = ""
    location_id: str = ""
    specialty_id: str = ""
    slot_id: str = ""
    doctor_label: str | None = None
    location_label: str | None = None
    telemedicine: bool = False


@dataclass(frozen=True)
class ParseResult:
    """Parsed available slots plus counts of empty days and skipped entries."""

    slots: tuple[AppointmentSlot, ...] = ()
    skipped: int = 0
    empty_days: int = 0


def _coerce_id(value: object) -> str | None:
    if type(value) is int and value >= 0:
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    return text if _ID_RE.fullmatch(text) else None


def _opt_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return None
    return cleaned


def _parse_day_date(value: object) -> date | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_slot_time(value: object, day_date: date | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _WALL_TIME_RE.fullmatch(text):
        if day_date is None:
            return None
        candidate = f"{day_date.isoformat()}T{text}"
    else:
        candidate = text
    try:
        return parse_warsaw_datetime(candidate)
    except ValueError:
        return None


def _parse_slot(
    item: object,
    *,
    day_date: date | None,
    fallback_specialty_id: str | None,
) -> AppointmentSlot | None:
    if not isinstance(item, dict):
        return None
    available = item.get("available", True)
    if type(available) is not bool or not available:
        return None
    telemedicine = item.get("telemedicine", False)
    if type(telemedicine) is not bool:
        return None
    raw_start = item.get("startTime") if "startTime" in item else item.get("time")
    start = _parse_slot_time(raw_start, day_date)
    if start is None:
        return None
    end: datetime | None = None
    if item.get("endTime") is not None:
        end = _parse_slot_time(item["endTime"], day_date or start.astimezone(WARSAW_TZ).date())
        if end is None or end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
            return None
    doctor_id = _coerce_id(item.get("doctorId"))
    location_id = _coerce_id(item.get("locationId"))
    specialty_id = _coerce_id(item.get("specialtyId")) or fallback_specialty_id
    if not doctor_id or not location_id or not specialty_id:
        return None
    return AppointmentSlot(
        provider="cmp",
        start=start,
        end=end,
        doctor_id=doctor_id,
        location_id=location_id,
        specialty_id=specialty_id,
        slot_id=_coerce_id(item.get("slotId")) or "",
        doctor_label=_opt_label(item.get("doctorName")),
        location_label=_opt_label(item.get("locationName")),
        telemedicine=telemedicine,
    )


def parse_cmp_scheduler(
    payload: object,
    *,
    default_specialty_id: str | None = None,
    max_slots: int = MAX_SLOTS,
) -> ParseResult:
    """Parse a decoded CMP ``/GetScheduler`` JSON object into normalized slots."""
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
        raise CmpParseError("Invalid CMP scheduler response")
    top_specialty = _coerce_id(payload.get("specialtyId")) or _coerce_id(default_specialty_id)
    slots: list[AppointmentSlot] = []
    skipped = 0
    empty_days = 0
    seen_entries = 0
    for day in payload["days"]:
        if not isinstance(day, dict):
            skipped += 1
            continue
        day_date = _parse_day_date(day.get("date"))
        raw_slots = day.get("slots")
        if raw_slots is None or raw_slots == []:
            empty_days += 1
            continue
        if not isinstance(raw_slots, list):
            skipped += 1
            continue
        for item in raw_slots:
            seen_entries += 1
            if seen_entries > max_slots:
                raise CmpParseError("CMP scheduler response exceeds slot limit")
            slot = _parse_slot(item, day_date=day_date, fallback_specialty_id=top_specialty)
            if slot is None:
                skipped += 1
            else:
                slots.append(slot)
    return ParseResult(slots=tuple(slots), skipped=skipped, empty_days=empty_days)


def parse_cmp_scheduler_bytes(
    raw: bytes,
    *,
    default_specialty_id: str | None = None,
    max_slots: int = MAX_SLOTS,
) -> ParseResult:
    """Strictly decode raw UTF-8 JSON bytes and parse a CMP ``/GetScheduler`` response."""
    try:
        payload = parse_strict_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CmpParseError("Invalid CMP scheduler response") from None
    return parse_cmp_scheduler(payload, default_specialty_id=default_specialty_id, max_slots=max_slots)
