"""Pure, DB-free appointment slot filtering, deduplication, and diff engine.

Evaluates previous and current slot snapshots against ``WatchCriteria`` (including
an optional "better than currently booked" rule) and produces deterministic, deduped
results plus stable SHA-256 / HMAC-SHA256 digests for minimal retention.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac

from .config import BookedAppointment, WARSAW_TZ, WatchCriteria
from .parser import AppointmentSlot

_DIGEST_DOMAIN = "hestia/appointments/slot/v1"


@dataclass(frozen=True)
class SlotDiff:
    """Deterministic diff between previous and current matching appointment slots."""

    watch_id: str
    active_slots: tuple[AppointmentSlot, ...]
    new_slots: tuple[AppointmentSlot, ...]
    removed_slots: tuple[AppointmentSlot, ...]
    better_slots: tuple[AppointmentSlot, ...]
    notifiable_slots: tuple[AppointmentSlot, ...]


def slot_identity_key(slot: AppointmentSlot) -> tuple[str, datetime, str, str, str]:
    """Canonical dedupe identity using the exact UTC instant (DST-fold safe)."""
    return (
        slot.provider,
        slot.start.astimezone(timezone.utc),
        slot.doctor_id,
        slot.location_id,
        slot.specialty_id,
    )


def slot_sort_key(slot: AppointmentSlot) -> tuple[datetime, str, str, str, str, str]:
    """Deterministic total ordering independent of input order or timezone representation."""
    return (
        slot.start.astimezone(timezone.utc),
        slot.doctor_id,
        slot.location_id,
        slot.specialty_id,
        slot.slot_id,
        slot.provider,
    )


def slot_digest(watch_id: str, slot: AppointmentSlot, *, hmac_key: bytes | None = None) -> str:
    """Stable opaque hex digest of one slot for minimal-retention storage and deduping."""
    start_utc = slot.start.astimezone(timezone.utc).isoformat()
    payload = "\0".join((
        _DIGEST_DOMAIN,
        watch_id,
        slot.provider,
        start_utc,
        slot.doctor_id,
        slot.location_id,
        slot.specialty_id,
    )).encode("utf-8")
    if hmac_key is not None:
        return hmac.new(hmac_key, payload, hashlib.sha256).hexdigest()
    return hashlib.sha256(payload).hexdigest()


def matches_criteria(slot: AppointmentSlot, criteria: WatchCriteria) -> bool:
    """Return True iff ``slot`` satisfies provider, ID, date, and Warsaw time-of-day criteria."""
    if slot.provider != criteria.provider:
        return False
    if criteria.specialty_id is not None and slot.specialty_id != criteria.specialty_id:
        return False
    if criteria.doctor_ids and slot.doctor_id not in criteria.doctor_ids:
        return False
    if criteria.location_ids and slot.location_id not in criteria.location_ids:
        return False
    local = slot.start.astimezone(WARSAW_TZ)
    if not criteria.date_from <= local.date() <= criteria.date_to:
        return False
    local_time = local.time()
    if criteria.time_from is not None and local_time < criteria.time_from:
        return False
    if criteria.time_to is not None and local_time > criteria.time_to:
        return False
    return True


def is_better_than_booked(slot: AppointmentSlot, booked: BookedAppointment) -> bool:
    """Return True iff ``slot`` improves on ``booked`` under the configured rule."""
    if booked.require_same_doctor and slot.doctor_id != booked.doctor_id:
        return False
    if booked.require_same_location and slot.location_id != booked.location_id:
        return False
    slot_utc = slot.start.astimezone(timezone.utc)
    booked_utc = booked.start.astimezone(timezone.utc)
    if booked.min_improvement_minutes == 0:
        return slot_utc < booked_utc
    return (booked_utc - slot_utc) >= timedelta(minutes=booked.min_improvement_minutes)


def dedupe_and_sort(slots: Iterable[AppointmentSlot]) -> tuple[AppointmentSlot, ...]:
    """Deduplicate by canonical identity and sort deterministically regardless of input order."""
    ordered = sorted(slots, key=slot_sort_key)
    seen: set[tuple[str, datetime, str, str, str]] = set()
    unique: list[AppointmentSlot] = []
    for slot in ordered:
        key = slot_identity_key(slot)
        if key not in seen:
            seen.add(key)
            unique.append(slot)
    return tuple(unique)


def diff_slots(
    previous: Iterable[AppointmentSlot],
    current: Iterable[AppointmentSlot],
    criteria: WatchCriteria,
) -> SlotDiff:
    """Compute a pure, deduped, stably ordered slot diff for one watch."""
    prev_matching = dedupe_and_sort(slot for slot in previous if matches_criteria(slot, criteria))
    curr_matching = dedupe_and_sort(slot for slot in current if matches_criteria(slot, criteria))
    prev_keys = {slot_identity_key(slot) for slot in prev_matching}
    curr_keys = {slot_identity_key(slot) for slot in curr_matching}
    new_slots = tuple(slot for slot in curr_matching if slot_identity_key(slot) not in prev_keys)
    removed_slots = tuple(slot for slot in prev_matching if slot_identity_key(slot) not in curr_keys)
    if criteria.booked_slot is not None:
        better_slots = tuple(
            slot for slot in new_slots if is_better_than_booked(slot, criteria.booked_slot)
        )
    else:
        better_slots = ()
    notifiable_slots = (
        better_slots
        if criteria.booked_slot is not None and criteria.only_better_than_booked
        else new_slots
    )
    return SlotDiff(
        watch_id=criteria.watch_id,
        active_slots=curr_matching,
        new_slots=new_slots,
        removed_slots=removed_slots,
        better_slots=better_slots,
        notifiable_slots=notifiable_slots,
    )
