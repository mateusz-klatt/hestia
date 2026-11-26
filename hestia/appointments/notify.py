"""Owner-private appointment notification bridge for Hestia's ``owner_inbox``.

Stores NO human text and NO exact slot time at rest: ``appointments_inbox_alerts`` holds only
``(watch_id, slot_hash, slot_date, alert_kind, created_at, expires_at)``. Inbox messages are
rendered strictly at READ time from operator-configured watch labels (with generic fallbacks,
never IDs or portal payloads) plus the coarse day bucket. Precise slot times exist only in
memory during a live notify cycle and are never persisted.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import db
from ..budget import owner_inbox as inbox_mod
from .config import (
    WARSAW_TZ,
    AppointmentsConfigError,
    AppointmentsSettings,
    PollPolicy,
    WatchCriteria,
    load_settings,
)
from .store import AppointmentsStore, FailureResult, ReconcileResult

log = logging.getLogger("hestia.appointments.notify")

DEFAULT_SPECIALTY_LABEL = "Configured specialty"
DEFAULT_CLINIC_LABEL = "Configured clinic"

_ATTENTION_STATUS_MAP = {
    "circuit_open": "circuit_open",
    "auth_attention": "authorization",
}


@dataclass(frozen=True)
class SlotNotification:
    """One owner-private appointment slot notification ready for ``owner_inbox``."""

    owner: str
    watch_id: str
    slot_hash: str
    slot_date: str
    slot_time: str
    specialty_label: str
    clinic_label: str
    better_than_booked: bool
    message: str
    inbox_entry: dict


@dataclass(frozen=True)
class AttentionNotification:
    """One owner-private circuit-break or auth-renewal alert emitted once per episode."""

    owner: str
    watch_id: str
    status: str
    error_code: str
    consecutive_failures: int
    notified_at: float
    inbox_entry: dict


@dataclass(frozen=True)
class NotifyBatchResult:
    """Outcome of dispatching notifiable slots through dedupe + rate-limit gates."""

    notifications: tuple[SlotNotification, ...] = ()
    rate_limited: int = 0


def resolve_config_labels(watch: WatchCriteria) -> tuple[str, str]:
    """Derive ``(specialty_label, clinic_label)`` from config labels only (never IDs or portal payloads)."""
    specialty_label = watch.specialty_label or watch.label or DEFAULT_SPECIALTY_LABEL
    clinic_label = watch.clinic_label or DEFAULT_CLINIC_LABEL
    return specialty_label, clinic_label


def format_slot_message(
    *,
    slot_date: str,
    specialty_label: str,
    clinic_label: str,
    better_than_booked: bool,
    slot_time: str | None = None,
) -> str:
    """Build a concise message from config labels and coarse day (precise time in-memory only)."""
    prefix = "Earlier appointment slot" if better_than_booked else "New appointment slot"
    when = f"{slot_date} {slot_time}" if slot_time else slot_date
    return f"{prefix}: {specialty_label} ({clinic_label}), {when}"


class AppointmentNotifier:
    """Dispatches rate-limited, slot-hash-deduped slot and circuit-break alerts to ``owner_inbox``."""

    def __init__(
        self,
        store: AppointmentsStore,
        *,
        sink: Callable[[dict], None] | None = None,
    ) -> None:
        self.store = store
        self.sink = sink

    def notify_slots(
        self,
        owner: str,
        watch: WatchCriteria,
        reconcile: ReconcileResult,
        *,
        policy: PollPolicy,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> NotifyBatchResult:
        """Persist metadata-only alerts, enforce rate limits, and emit in-memory ``owner_inbox`` entries."""
        if not reconcile.notifiable_slots:
            return NotifyBatchResult()

        specialty_label, clinic_label = resolve_config_labels(watch)
        prepared: dict[str, tuple[str, str, bool, str]] = {}
        claim_input: list[tuple[str, str, bool]] = []
        for slot_hash, slot, is_better in reconcile.notifiable_slots:
            local = slot.start.astimezone(WARSAW_TZ)
            slot_date = local.date().isoformat()
            slot_time = local.strftime("%H:%M")
            message = format_slot_message(
                slot_date=slot_date,
                slot_time=slot_time,
                specialty_label=specialty_label,
                clinic_label=clinic_label,
                better_than_booked=is_better,
            )
            prepared[slot_hash] = (slot_date, slot_time, is_better, message)
            claim_input.append((slot_hash, slot_date, is_better))

        approved_hashes, rate_limited = self.store.claim_slot_notifications(
            owner,
            watch.watch_id,
            claim_input,
            policy=policy,
            expected_generation=expected_generation,
            stop_check=stop_check,
        )
        emitted: list[SlotNotification] = []
        for slot_hash in approved_hashes:
            slot_date, slot_time, is_better, message = prepared[slot_hash]
            entry = inbox_mod.appointment_slot_entry(
                owner,
                watch.watch_id,
                slot_hash,
                slot_date=slot_date,
                message=message,
                better_than_booked=is_better,
            )
            if self.sink is not None:
                self.sink(entry)
            emitted.append(
                SlotNotification(
                    owner=owner,
                    watch_id=watch.watch_id,
                    slot_hash=slot_hash,
                    slot_date=slot_date,
                    slot_time=slot_time,
                    specialty_label=specialty_label,
                    clinic_label=clinic_label,
                    better_than_booked=is_better,
                    message=message,
                    inbox_entry=entry,
                )
            )
        if emitted or rate_limited:
            log.info(
                "Appointments notifications processed notified=%d rate_limited=%d",
                len(emitted),
                rate_limited,
            )
        return NotifyBatchResult(notifications=tuple(emitted), rate_limited=rate_limited)

    def notify_attention(
        self,
        owner: str,
        watch: WatchCriteria,
        failure: FailureResult,
    ) -> AttentionNotification | None:
        """Emit one owner-private attention alert when a watch breaks circuit or needs auth."""
        if not failure.should_notify_attention or failure.attention_notified_at is None:
            return None
        inbox_status = _ATTENTION_STATUS_MAP.get(failure.last_status)
        if inbox_status is None:
            return None
        entry = inbox_mod.appointment_attention_entry(
            owner,
            watch.watch_id,
            status=inbox_status,
            error_code=failure.last_error_code,
            consecutive_failures=failure.consecutive_failures,
            notified_at=failure.attention_notified_at,
        )
        if self.sink is not None:
            self.sink(entry)
        log.warning(
            "Appointments attention alert emitted status=%s code=%s failures=%d",
            inbox_status,
            failure.last_error_code,
            failure.consecutive_failures,
        )
        return AttentionNotification(
            owner=owner,
            watch_id=watch.watch_id,
            status=inbox_status,
            error_code=failure.last_error_code,
            consecutive_failures=failure.consecutive_failures,
            notified_at=failure.attention_notified_at,
            inbox_entry=entry,
        )


def active_inbox_entries(
    session: Session,
    owner: str,
    *,
    now: float,
    settings: AppointmentsSettings | None = None,
    environ: Mapping[str, str] | None = None,
    session_secret: bytes = b"",
    prune_expired: bool = True,
) -> list[dict]:
    """Project live owner-private appointment alerts and render text at read time from watch config.

    Returns ``[]`` with zero file or DB reads when ``HESTIA_APPOINTMENTS_ENABLED`` is OFF.
    Deletes expired alert rows on writable sessions (``prune_expired=True``) and renders
    human-readable messages at read time from watch config labels + coarse day bucket only.
    """
    resolved = settings
    if resolved is None:
        try:
            resolved = load_settings(environ=environ, session_secret=session_secret)
        except AppointmentsConfigError:
            return []
    if resolved is None or resolved.owner != owner:
        return []

    if prune_expired:
        session.execute(
            delete(db.AppointmentsInboxAlert).where(
                db.AppointmentsInboxAlert.owner == owner,
                db.AppointmentsInboxAlert.expires_at <= now,
            )
        )

    watches_by_id = {watch.watch_id: watch for watch in resolved.watches}
    today_iso = datetime.fromtimestamp(now, timezone.utc).astimezone(WARSAW_TZ).date().isoformat()
    entries: list[dict] = []

    alert_rows = session.execute(
        select(
            db.AppointmentsInboxAlert.watch_id,
            db.AppointmentsInboxAlert.slot_hash,
            db.AppointmentsInboxAlert.slot_date,
            db.AppointmentsInboxAlert.alert_kind,
        )
        .join(
            db.AppointmentsActiveSlot,
            (db.AppointmentsActiveSlot.owner == db.AppointmentsInboxAlert.owner)
            & (db.AppointmentsActiveSlot.watch_id == db.AppointmentsInboxAlert.watch_id)
            & (db.AppointmentsActiveSlot.slot_hash == db.AppointmentsInboxAlert.slot_hash),
        )
        .where(
            db.AppointmentsInboxAlert.owner == owner,
            db.AppointmentsInboxAlert.expires_at > now,
            db.AppointmentsActiveSlot.expires_at > now,
            db.AppointmentsActiveSlot.slot_date >= today_iso,
        )
        .order_by(
            db.AppointmentsInboxAlert.watch_id,
            db.AppointmentsInboxAlert.slot_date,
            db.AppointmentsInboxAlert.created_at,
            db.AppointmentsInboxAlert.slot_hash,
        )
    ).all()
    for watch_id, slot_hash, slot_date, alert_kind in alert_rows:
        watch = watches_by_id.get(watch_id)
        if watch is None:
            continue
        specialty_label, clinic_label = resolve_config_labels(watch)
        better = alert_kind == "better_slot"
        message = format_slot_message(
            slot_date=slot_date,
            specialty_label=specialty_label,
            clinic_label=clinic_label,
            better_than_booked=better,
        )
        entries.append(
            inbox_mod.appointment_slot_entry(
                owner,
                watch_id,
                slot_hash,
                slot_date=slot_date,
                message=message,
                better_than_booked=better,
            )
        )

    watch_rows = session.scalars(
        select(db.AppointmentsWatchState)
        .where(
            db.AppointmentsWatchState.owner == owner,
            db.AppointmentsWatchState.last_status.in_(tuple(_ATTENTION_STATUS_MAP)),
            db.AppointmentsWatchState.attention_notified_at.is_not(None),
        )
        .order_by(db.AppointmentsWatchState.watch_id)
    ).all()
    for row in watch_rows:
        if row.watch_id not in watches_by_id:
            continue
        entries.append(
            inbox_mod.appointment_attention_entry(
                owner,
                row.watch_id,
                status=_ATTENTION_STATUS_MAP[row.last_status],
                error_code=row.last_error_code or "provider_http",
                consecutive_failures=max(1, row.consecutive_failures),
                notified_at=row.attention_notified_at,
            )
        )
    return entries
