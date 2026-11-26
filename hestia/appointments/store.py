"""Minimal SQLite persistence, HMAC slot-diff adapter, metadata-only alerts, generation fence, and immediate purge.

Persists ONLY:
- opaque ``watch_id`` (config key, never doctor/clinic/specialty IDs or names),
- keyed HMAC-SHA256 slot hashes + coarse ``YYYY-MM-DD`` day bucket (never exact slot times),
- ``pending_notify`` retry state for rate-limited hashes,
- metadata-only short-TTL alert rows (``watch_id``, ``slot_hash``, ``slot_date``, ``alert_kind``,
  ``created_at``, ``expires_at`` — zero human text and zero exact times at rest; inbox messages are
  rendered at read time from watch config + day bucket),
- persisted round-robin poller cursor to avoid watch starvation,
- persisted ``appointments_generation`` epoch in ``app_meta`` fencing detached workers against purges.

Disappeared slots clear their inbox alerts and read markers immediately so reappearing slots
always surface unread. Disabling ``HESTIA_APPOINTMENTS_ENABLED`` purges all appointment state,
alerts, inbox read markers, and any legacy appointment inbox audit rows immediately and bumps
``appointments_generation`` inside ``BEGIN IMMEDIATE``.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import logging
import math
from pathlib import Path
import time

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from .. import db
from ..budget import owner_inbox as inbox_mod
from .cmp_client import CmpClientError
from .config import WARSAW_TZ, PollPolicy, WatchCriteria, is_enabled
from .parser import AppointmentSlot
from .slots import diff_slots, is_better_than_booked, slot_digest

_MIN_HMAC_BYTES = 32
GENERATION_META_KEY = "appointments_generation"
DISABLED_AT_META_KEY = "appointments_disabled_at"
_APPT_TABLES = frozenset({
    "appointments_watch_state",
    "appointments_active_slots",
    "appointments_inbox_alerts",
    "appointments_poller_state",
})
log = logging.getLogger("hestia.appointments.store")


class AppointmentsStoreError(RuntimeError):
    """Fixed safe diagnostic leaving the appointments persistence boundary."""


@dataclass(frozen=True)
class WatchStateSnapshot:
    """Read-only snapshot of one persisted ``appointments_watch_state`` row."""

    owner: str
    watch_id: str
    provider: str
    last_status: str
    last_error_code: str | None
    consecutive_failures: int
    last_run_at: float | None
    next_poll_at: float | None
    cooldown_until: float | None
    attention_notified_at: float | None
    notify_window_start_at: float | None
    notify_window_count: int
    updated_at: float


@dataclass(frozen=True)
class ActiveSlotRecord:
    """Read-only snapshot of one persisted ``appointments_active_slots`` row."""

    owner: str
    watch_id: str
    slot_hash: str
    slot_date: str
    better_than_booked: bool
    pending_notify: bool
    first_seen_at: float
    last_seen_at: float
    expires_at: float
    notified_at: float | None


@dataclass(frozen=True)
class InboxAlertRecord:
    """Read-only snapshot of one metadata-only ``appointments_inbox_alerts`` row (zero text/time at rest)."""

    owner: str
    watch_id: str
    slot_hash: str
    slot_date: str
    alert_kind: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of reconciling one watch's parsed slots against persisted HMAC hashes."""

    watch_id: str
    active_count: int
    new_slots: tuple[AppointmentSlot, ...]
    better_slots: tuple[AppointmentSlot, ...]
    notifiable_slots: tuple[tuple[str, AppointmentSlot, bool], ...]
    removed_count: int
    expired_pruned: int


@dataclass(frozen=True)
class FailureResult:
    """Outcome of recording a watch poll failure with backoff / circuit-breaker state."""

    watch_id: str
    last_status: str
    last_error_code: str
    consecutive_failures: int
    cooldown_until: float
    should_notify_attention: bool
    attention_notified_at: float | None


@dataclass(frozen=True)
class PurgeResult:
    """Counts of watch-state, active-slot, alert, read-marker, and audit rows deleted."""

    watches_deleted: int
    slots_deleted: int
    alerts_deleted: int = 0
    reads_deleted: int = 0
    audits_deleted: int = 0
    generation: int | None = field(default=None, compare=False)
    fenced: bool = False


def _validate_hmac_key(hmac_key: bytes) -> bytes:
    if not isinstance(hmac_key, bytes) or len(hmac_key) < _MIN_HMAC_BYTES:
        raise AppointmentsStoreError("Missing or invalid HMAC key")
    return hmac_key


def _validate_timestamp(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise AppointmentsStoreError("Invalid appointments clock")
    return float(value)


def _today_warsaw(now: float) -> date:
    return datetime.fromtimestamp(now, timezone.utc).astimezone(WARSAW_TZ).date()


def diff_with_stored_hashes(
    stored_hashes: Collection[str],
    current_slots: Iterable[AppointmentSlot],
    criteria: WatchCriteria,
    *,
    hmac_key: bytes,
    today_warsaw: date | None = None,
) -> tuple[
    dict[str, AppointmentSlot],
    tuple[AppointmentSlot, ...],
    tuple[AppointmentSlot, ...],
    tuple[tuple[str, AppointmentSlot, bool], ...],
    frozenset[str],
]:
    """Bridge persisted HMAC slot digests and pure ``slots.diff_slots`` at the store boundary."""
    key = _validate_hmac_key(hmac_key)
    candidate = diff_slots(previous=(), current=current_slots, criteria=criteria)
    current_by_hash: dict[str, AppointmentSlot] = {}
    for slot in candidate.active_slots:
        if today_warsaw is not None and slot.start.astimezone(WARSAW_TZ).date() < today_warsaw:
            continue
        digest = slot_digest(criteria.watch_id, slot, hmac_key=key)
        current_by_hash[digest] = slot

    stored = frozenset(stored_hashes)
    new_hashes = current_by_hash.keys() - stored
    removed_hashes = frozenset(stored - current_by_hash.keys())

    new_slots: list[AppointmentSlot] = []
    better_slots: list[AppointmentSlot] = []
    notifiable: list[tuple[str, AppointmentSlot, bool]] = []
    for digest, slot in current_by_hash.items():
        if digest not in new_hashes:
            continue
        new_slots.append(slot)
        is_better = (
            criteria.booked_slot is not None
            and is_better_than_booked(slot, criteria.booked_slot)
        )
        if is_better:
            better_slots.append(slot)
        if not criteria.only_better_than_booked or is_better:
            notifiable.append((digest, slot, is_better))

    return (
        current_by_hash,
        tuple(new_slots),
        tuple(better_slots),
        tuple(notifiable),
        removed_hashes,
    )


def _snapshot_watch(row: db.AppointmentsWatchState) -> WatchStateSnapshot:
    return WatchStateSnapshot(
        owner=row.owner,
        watch_id=row.watch_id,
        provider=row.provider,
        last_status=row.last_status,
        last_error_code=row.last_error_code,
        consecutive_failures=row.consecutive_failures,
        last_run_at=row.last_run_at,
        next_poll_at=row.next_poll_at,
        cooldown_until=row.cooldown_until,
        attention_notified_at=row.attention_notified_at,
        notify_window_start_at=row.notify_window_start_at,
        notify_window_count=row.notify_window_count,
        updated_at=row.updated_at,
    )


def _snapshot_slot(row: db.AppointmentsActiveSlot) -> ActiveSlotRecord:
    return ActiveSlotRecord(
        owner=row.owner,
        watch_id=row.watch_id,
        slot_hash=row.slot_hash,
        slot_date=row.slot_date,
        better_than_booked=bool(row.better_than_booked),
        pending_notify=bool(row.pending_notify),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        notified_at=row.notified_at,
    )


def _snapshot_alert(row: db.AppointmentsInboxAlert) -> InboxAlertRecord:
    return InboxAlertRecord(
        owner=row.owner,
        watch_id=row.watch_id,
        slot_hash=row.slot_hash,
        slot_date=row.slot_date,
        alert_kind=row.alert_kind,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _read_generation(session: Session) -> int:
    row = session.get(db.AppMeta, GENERATION_META_KEY)
    return 0 if row is None else int(row.value)


def _bump_generation(session: Session) -> int:
    row = session.get(db.AppMeta, GENERATION_META_KEY)
    if row is None:
        session.add(db.AppMeta(key=GENERATION_META_KEY, value="1"))
        return 1
    next_gen = int(row.value) + 1
    row.value = str(next_gen)
    return next_gen


def _read_disabled_at(session: Session) -> float | None:
    row = session.get(db.AppMeta, DISABLED_AT_META_KEY)
    return None if row is None else float(row.value)


def _set_disabled_at(session: Session, now: float) -> None:
    row = session.get(db.AppMeta, DISABLED_AT_META_KEY)
    if row is None:
        session.add(db.AppMeta(key=DISABLED_AT_META_KEY, value=repr(now)))
    else:
        row.value = repr(now)


def _delete_all_appointment_state(session: Session, now: float) -> PurgeResult:
    alerts_deleted = session.execute(delete(db.AppointmentsInboxAlert)).rowcount
    slots_deleted = session.execute(delete(db.AppointmentsActiveSlot)).rowcount
    watches_deleted = session.execute(delete(db.AppointmentsWatchState)).rowcount
    session.execute(delete(db.AppointmentsPollerState))
    reads_deleted, audits_deleted = _purge_all_appointment_reads_and_audits(
        session, owner=None, now=now
    )
    return PurgeResult(
        watches_deleted=watches_deleted,
        slots_deleted=slots_deleted,
        alerts_deleted=alerts_deleted,
        reads_deleted=reads_deleted,
        audits_deleted=audits_deleted,
    )


def _bump_inbox_revisions(session: Session, owners: Collection[str], now: float) -> None:
    for owner in sorted(set(owners)):
        state = session.get(db.BudgetOwnerInboxState, owner)
        if state is None:
            session.add(db.BudgetOwnerInboxState(owner=owner, reads_revision=1, updated_at=now))
        else:
            state.reads_revision += 1
            state.updated_at = now


def _delete_slot_read_markers(
    session: Session,
    slot_keys: Collection[tuple[str, str, str]],
    now: float,
) -> int:
    by_owner: dict[str, list[str]] = {}
    for owner, watch_id, slot_hash in slot_keys:
        subject_id = inbox_mod.opaque_subject_id(
            owner,
            inbox_mod.KIND_APPOINTMENT_SLOT,
            f"{watch_id}\0{slot_hash}",
        )
        by_owner.setdefault(owner, []).append(subject_id)
    deleted = 0
    touched_owners: set[str] = set()
    for owner, subject_ids in by_owner.items():
        count = session.execute(
            delete(db.BudgetOwnerInboxRead).where(
                db.BudgetOwnerInboxRead.owner == owner,
                db.BudgetOwnerInboxRead.kind == inbox_mod.KIND_APPOINTMENT_SLOT,
                db.BudgetOwnerInboxRead.subject_id.in_(subject_ids),
            )
        ).rowcount
        if count:
            deleted += count
            touched_owners.add(owner)
    _bump_inbox_revisions(session, touched_owners, now)
    return deleted


def _delete_attention_read_markers(
    session: Session,
    owner: str,
    watch_ids: Collection[str],
    now: float,
) -> int:
    subject_ids = [
        inbox_mod.opaque_subject_id(owner, inbox_mod.KIND_APPOINTMENT_ATTENTION, watch_id)
        for watch_id in watch_ids
    ]
    count = session.execute(
        delete(db.BudgetOwnerInboxRead).where(
            db.BudgetOwnerInboxRead.owner == owner,
            db.BudgetOwnerInboxRead.kind == inbox_mod.KIND_APPOINTMENT_ATTENTION,
            db.BudgetOwnerInboxRead.subject_id.in_(subject_ids),
        )
    ).rowcount
    if count:
        _bump_inbox_revisions(session, (owner,), now)
    return count


def _is_appointment_inbox_audit(row: db.BudgetAnalyticsAudit) -> bool:
    for raw in (row.after_json, row.before_json):
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("kind") in inbox_mod.APPOINTMENT_INBOX_KINDS:
            return True
    return False


def _purge_all_appointment_reads_and_audits(
    session: Session,
    *,
    owner: str | None,
    now: float,
) -> tuple[int, int]:
    kinds = tuple(inbox_mod.APPOINTMENT_INBOX_KINDS)
    owner_stmt = select(db.BudgetOwnerInboxRead.owner).where(
        db.BudgetOwnerInboxRead.kind.in_(kinds)
    ).distinct()
    if owner is not None:
        owner_stmt = owner_stmt.where(db.BudgetOwnerInboxRead.owner == owner)
    affected_owners = list(session.scalars(owner_stmt).all())
    reads_stmt = delete(db.BudgetOwnerInboxRead).where(db.BudgetOwnerInboxRead.kind.in_(kinds))
    if owner is not None:
        reads_stmt = reads_stmt.where(db.BudgetOwnerInboxRead.owner == owner)
    reads_deleted = session.execute(reads_stmt).rowcount
    if affected_owners:
        _bump_inbox_revisions(session, affected_owners, now)

    audit_stmt = select(db.BudgetAnalyticsAudit).where(
        db.BudgetAnalyticsAudit.subject_type == "owner_inbox_read"
    )
    if owner is not None:
        audit_stmt = audit_stmt.where(db.BudgetAnalyticsAudit.owner == owner)
    audit_ids = [row.id for row in session.scalars(audit_stmt).all() if _is_appointment_inbox_audit(row)]
    audits_deleted = 0
    if audit_ids:
        audits_deleted = session.execute(
            delete(db.BudgetAnalyticsAudit).where(db.BudgetAnalyticsAudit.id.in_(audit_ids))
        ).rowcount
    return reads_deleted, audits_deleted


class AppointmentsStore:
    """Minimal transactional SQLite store for watches, HMAC slot hashes, metadata-only alerts, and purge fencing."""

    def __init__(self, session_factory: sessionmaker[Session], *, clock=time.time) -> None:
        self.sessions = session_factory
        self.clock = clock

    def _now(self) -> float:
        return _validate_timestamp(self.clock())

    @contextmanager
    def _immediate_scope(self) -> Iterator[Session]:
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session

    @staticmethod
    def _is_fenced(
        session: Session,
        *,
        expected_generation: int | None,
        stop_check: Callable[[], bool] | None,
    ) -> bool:
        if stop_check is not None and stop_check():
            return True
        if _read_disabled_at(session) is not None:
            return True
        return expected_generation is not None and _read_generation(session) != expected_generation

    @staticmethod
    def _require_owner(session: Session, owner: str) -> None:
        if session.get(db.User, owner) is None:
            raise AppointmentsStoreError("Unknown appointments owner")

    @staticmethod
    def _upsert_watch_row(
        session: Session,
        owner: str,
        watch: WatchCriteria,
        now: float,
    ) -> db.AppointmentsWatchState:
        row = session.get(db.AppointmentsWatchState, (owner, watch.watch_id))
        if row is None:
            row = db.AppointmentsWatchState(
                owner=owner,
                watch_id=watch.watch_id,
                provider=watch.provider,
                last_status="ok",
                last_error_code=None,
                consecutive_failures=0,
                last_run_at=None,
                next_poll_at=None,
                cooldown_until=None,
                attention_notified_at=None,
                notify_window_start_at=None,
                notify_window_count=0,
                updated_at=now,
            )
            session.add(row)
        else:
            row.provider = watch.provider
            row.updated_at = now
        return row

    def get_generation(self) -> int:
        """Return the current persisted purge generation counter (0 before any purge)."""
        with self.sessions() as session:
            return _read_generation(session)

    def get_disabled_at(self) -> float | None:
        """Return the persisted disable-purge timestamp marker, or None when enabled."""
        with self.sessions() as session:
            return _read_disabled_at(session)

    def is_fenced(self, *, expected_generation: int) -> bool:
        """Read-only fence check: True when disabled marker is set or generation changed."""
        with self.sessions() as session:
            return _read_disabled_at(session) is not None or _read_generation(session) != expected_generation

    def mark_enabled(self) -> int:
        """Atomically clear ``appointments_disabled_at``, bump generation, and return the unique new epoch."""
        with self._immediate_scope() as session:
            session.execute(delete(db.AppMeta).where(db.AppMeta.key == DISABLED_AT_META_KEY))
            return _bump_generation(session)

    def mark_disabled(self) -> int:
        """Atomically purge all appointment state, bump generation, set disabled_at, and return the bound epoch."""
        result = self.prune_expired(enabled=False)
        assert result.generation is not None
        return result.generation

    def get_poll_cursor(self, owner: str) -> int:
        with self.sessions() as session:
            row = session.get(db.AppointmentsPollerState, owner)
            return 0 if row is None else row.next_watch_index

    def set_poll_cursor(
        self,
        owner: str,
        next_watch_index: int,
        *,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> bool:
        if type(next_watch_index) is not int or next_watch_index < 0:
            raise AppointmentsStoreError("Invalid poll cursor")
        now = self._now()
        with self._immediate_scope() as session:
            if self._is_fenced(session, expected_generation=expected_generation, stop_check=stop_check):
                return False
            self._require_owner(session, owner)
            row = session.get(db.AppointmentsPollerState, owner)
            if row is None:
                session.add(
                    db.AppointmentsPollerState(
                        owner=owner,
                        next_watch_index=next_watch_index,
                        updated_at=now,
                    )
                )
            else:
                row.next_watch_index = next_watch_index
                row.updated_at = now
        return True

    def get_watch_state(self, owner: str, watch_id: str) -> WatchStateSnapshot | None:
        with self.sessions() as session:
            row = session.get(db.AppointmentsWatchState, (owner, watch_id))
            return None if row is None else _snapshot_watch(row)

    def list_watch_states(self, owner: str) -> tuple[WatchStateSnapshot, ...]:
        with self.sessions() as session:
            rows = session.scalars(
                select(db.AppointmentsWatchState)
                .where(db.AppointmentsWatchState.owner == owner)
                .order_by(db.AppointmentsWatchState.watch_id)
            ).all()
            return tuple(_snapshot_watch(row) for row in rows)

    def load_slot_hashes(self, owner: str, watch_id: str) -> frozenset[str]:
        with self.sessions() as session:
            hashes = session.scalars(
                select(db.AppointmentsActiveSlot.slot_hash).where(
                    db.AppointmentsActiveSlot.owner == owner,
                    db.AppointmentsActiveSlot.watch_id == watch_id,
                )
            ).all()
            return frozenset(hashes)

    def list_active_slots(self, owner: str, watch_id: str | None = None) -> tuple[ActiveSlotRecord, ...]:
        with self.sessions() as session:
            stmt = select(db.AppointmentsActiveSlot).where(db.AppointmentsActiveSlot.owner == owner)
            if watch_id is not None:
                stmt = stmt.where(db.AppointmentsActiveSlot.watch_id == watch_id)
            stmt = stmt.order_by(
                db.AppointmentsActiveSlot.watch_id,
                db.AppointmentsActiveSlot.slot_date,
                db.AppointmentsActiveSlot.slot_hash,
            )
            return tuple(_snapshot_slot(row) for row in session.scalars(stmt).all())

    def prune_expired_alerts(self, owner: str) -> int:
        """Delete expired metadata-only alert rows for ``owner`` only when a read-only check finds any."""
        now = self._now()
        with self.sessions() as session:
            expired_exists = session.scalar(
                select(db.AppointmentsInboxAlert.slot_hash)
                .where(
                    db.AppointmentsInboxAlert.owner == owner,
                    db.AppointmentsInboxAlert.expires_at <= now,
                )
                .limit(1)
            )
        if expired_exists is None:
            return 0
        with self._immediate_scope() as session:
            return session.execute(
                delete(db.AppointmentsInboxAlert).where(
                    db.AppointmentsInboxAlert.owner == owner,
                    db.AppointmentsInboxAlert.expires_at <= now,
                )
            ).rowcount

    def list_inbox_alerts(self, owner: str, watch_id: str | None = None) -> tuple[InboxAlertRecord, ...]:
        """Prune expired alerts only when present, then return remaining metadata-only alert rows."""
        self.prune_expired_alerts(owner)
        with self.sessions() as session:
            stmt = select(db.AppointmentsInboxAlert).where(db.AppointmentsInboxAlert.owner == owner)
            if watch_id is not None:
                stmt = stmt.where(db.AppointmentsInboxAlert.watch_id == watch_id)
            stmt = stmt.order_by(
                db.AppointmentsInboxAlert.watch_id,
                db.AppointmentsInboxAlert.created_at,
                db.AppointmentsInboxAlert.slot_hash,
            )
            return tuple(_snapshot_alert(row) for row in session.scalars(stmt).all())

    def reconcile_watch(
        self,
        owner: str,
        watch: WatchCriteria,
        current_slots: Iterable[AppointmentSlot],
        *,
        hmac_key: bytes,
        policy: PollPolicy,
        next_poll_at: float | None = None,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> ReconcileResult | None:
        """Persist current active HMAC slot hashes, retry pending_notify slots, and clear gone markers."""
        key = _validate_hmac_key(hmac_key)
        now = self._now()
        today = _today_warsaw(now)
        today_iso = today.isoformat()
        ttl_cutoff = now - policy.slot_ttl_seconds
        next_poll = None if next_poll_at is None else _validate_timestamp(next_poll_at)

        with self._immediate_scope() as session:
            if self._is_fenced(session, expected_generation=expected_generation, stop_check=stop_check):
                return None
            self._require_owner(session, owner)
            state = self._upsert_watch_row(session, owner, watch, now)
            had_attention = state.attention_notified_at is not None
            state.last_status = "ok"
            state.last_error_code = None
            state.consecutive_failures = 0
            state.last_run_at = now
            state.next_poll_at = next_poll
            state.cooldown_until = None
            state.attention_notified_at = None
            state.updated_at = now
            if had_attention:
                _delete_attention_read_markers(session, owner, (watch.watch_id,), now)

            existing_rows = list(
                session.scalars(
                    select(db.AppointmentsActiveSlot).where(
                        db.AppointmentsActiveSlot.owner == owner,
                        db.AppointmentsActiveSlot.watch_id == watch.watch_id,
                    )
                ).all()
            )
            surviving_by_hash: dict[str, db.AppointmentsActiveSlot] = {}
            gone_slot_keys: list[tuple[str, str, str]] = []
            expired_pruned = 0
            for existing in existing_rows:
                if (
                    existing.slot_date < today_iso
                    or existing.expires_at <= now
                    or existing.last_seen_at <= ttl_cutoff
                ):
                    gone_slot_keys.append((owner, watch.watch_id, existing.slot_hash))
                    session.delete(existing)
                    expired_pruned += 1
                else:
                    surviving_by_hash[existing.slot_hash] = existing

            current_by_hash, new_slots, better_slots, new_notifiable, removed_hashes = diff_with_stored_hashes(
                surviving_by_hash.keys(),
                current_slots,
                watch,
                hmac_key=key,
                today_warsaw=today,
            )

            for removed_hash in removed_hashes:
                gone_slot_keys.append((owner, watch.watch_id, removed_hash))
                session.delete(surviving_by_hash[removed_hash])

            if gone_slot_keys:
                gone_hashes = [slot_hash for _, _, slot_hash in gone_slot_keys]
                session.execute(
                    delete(db.AppointmentsInboxAlert).where(
                        db.AppointmentsInboxAlert.owner == owner,
                        db.AppointmentsInboxAlert.watch_id == watch.watch_id,
                        db.AppointmentsInboxAlert.slot_hash.in_(gone_hashes),
                    )
                )
                _delete_slot_read_markers(session, gone_slot_keys, now)

            new_notifiable_hashes = {digest for digest, _, _ in new_notifiable}
            combined_notifiable: list[tuple[str, AppointmentSlot, bool]] = []
            expires_at = now + policy.slot_ttl_seconds
            for digest, slot in current_by_hash.items():
                slot_date = slot.start.astimezone(WARSAW_TZ).date().isoformat()
                is_better = (
                    watch.booked_slot is not None
                    and is_better_than_booked(slot, watch.booked_slot)
                )
                eligible = (not watch.only_better_than_booked) or is_better
                row = surviving_by_hash.get(digest)
                if row is None:
                    is_pending = digest in new_notifiable_hashes
                    session.add(
                        db.AppointmentsActiveSlot(
                            owner=owner,
                            watch_id=watch.watch_id,
                            slot_hash=digest,
                            slot_date=slot_date,
                            better_than_booked=is_better,
                            pending_notify=is_pending,
                            first_seen_at=now,
                            last_seen_at=now,
                            expires_at=expires_at,
                            notified_at=None,
                        )
                    )
                    if is_pending:
                        combined_notifiable.append((digest, slot, is_better))
                else:
                    row.slot_date = slot_date
                    row.better_than_booked = is_better
                    row.last_seen_at = now
                    row.expires_at = expires_at
                    if row.pending_notify:
                        if eligible:
                            combined_notifiable.append((digest, slot, is_better))
                        else:
                            row.pending_notify = False

        return ReconcileResult(
            watch_id=watch.watch_id,
            active_count=len(current_by_hash),
            new_slots=new_slots,
            better_slots=better_slots,
            notifiable_slots=tuple(combined_notifiable),
            removed_count=len(removed_hashes),
            expired_pruned=expired_pruned,
        )

    def claim_slot_notifications(
        self,
        owner: str,
        watch_id: str,
        candidates: Iterable[tuple[str, str, bool]],
        *,
        policy: PollPolicy,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> tuple[tuple[str, ...], int]:
        """Atomically dedupe, rate-limit, persist metadata-only alerts, and retain pending_notify on limit."""
        now = self._now()
        approved: list[str] = []
        rate_limited = 0
        with self._immediate_scope() as session:
            if self._is_fenced(session, expected_generation=expected_generation, stop_check=stop_check):
                return (), 0
            state = session.get(db.AppointmentsWatchState, (owner, watch_id))
            if state is None:
                return (), 0
            if (
                state.notify_window_start_at is None
                or now - state.notify_window_start_at >= policy.notify_window_seconds
            ):
                state.notify_window_start_at = now
                state.notify_window_count = 0

            seen_in_call: set[str] = set()
            alert_expires = now + policy.alert_ttl_seconds
            for digest, slot_date, is_better in candidates:
                if digest in seen_in_call:
                    continue
                seen_in_call.add(digest)
                slot_row = session.get(db.AppointmentsActiveSlot, (owner, watch_id, digest))
                if slot_row is None or slot_row.notified_at is not None:
                    continue
                if state.notify_window_count >= policy.notify_max_per_window:
                    slot_row.pending_notify = True
                    rate_limited += 1
                    continue
                slot_row.notified_at = now
                slot_row.pending_notify = False
                state.notify_window_count += 1
                alert_kind = "better_slot" if is_better else "new_slot"
                alert_row = session.get(db.AppointmentsInboxAlert, (owner, watch_id, digest))
                if alert_row is None:
                    session.add(
                        db.AppointmentsInboxAlert(
                            owner=owner,
                            watch_id=watch_id,
                            slot_hash=digest,
                            slot_date=slot_date,
                            alert_kind=alert_kind,
                            created_at=now,
                            expires_at=alert_expires,
                        )
                    )
                else:
                    alert_row.slot_date = slot_date
                    alert_row.alert_kind = alert_kind
                    alert_row.created_at = now
                    alert_row.expires_at = alert_expires
                approved.append(digest)
        return tuple(approved), rate_limited

    def record_failure(
        self,
        owner: str,
        watch: WatchCriteria,
        *,
        error_code: str,
        policy: PollPolicy,
        retry_after: float | None = None,
        attention: str | None = None,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> FailureResult | None:
        """Record a failed watch poll, update exponential backoff, and detect circuit-break."""
        if error_code not in CmpClientError.CODES:
            raise AppointmentsStoreError("Invalid error code")
        now = self._now()
        with self._immediate_scope() as session:
            if self._is_fenced(session, expected_generation=expected_generation, stop_check=stop_check):
                return None
            self._require_owner(session, owner)
            state = self._upsert_watch_row(session, owner, watch, now)
            state.consecutive_failures += 1
            state.last_run_at = now
            state.last_error_code = error_code
            state.updated_at = now

            if attention == "authorization" or error_code == "authorization":
                state.last_status = "auth_attention"
                delay = float(policy.failure_backoff_max_seconds)
                should_notify = state.attention_notified_at is None
                if should_notify:
                    state.attention_notified_at = now
            elif state.consecutive_failures >= policy.circuit_breaker_threshold:
                state.last_status = "circuit_open"
                delay = policy.failure_backoff_delay(
                    state.consecutive_failures,
                    retry_after=retry_after,
                )
                should_notify = state.attention_notified_at is None
                if should_notify:
                    state.attention_notified_at = now
            else:
                state.last_status = "backoff"
                delay = policy.failure_backoff_delay(
                    state.consecutive_failures,
                    retry_after=retry_after,
                )
                should_notify = False

            state.cooldown_until = now + delay
            state.next_poll_at = state.cooldown_until
            result = FailureResult(
                watch_id=watch.watch_id,
                last_status=state.last_status,
                last_error_code=error_code,
                consecutive_failures=state.consecutive_failures,
                cooldown_until=state.cooldown_until,
                should_notify_attention=should_notify,
                attention_notified_at=state.attention_notified_at,
            )
        return result

    def prune_removed_watches(
        self,
        owner: str,
        active_watch_ids: Collection[str],
        *,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> PurgeResult:
        """Hard-delete persisted watches removed from config, along with slots, alerts, and read markers."""
        keep = set(active_watch_ids)
        now = self._now()
        with self._immediate_scope() as session:
            if self._is_fenced(session, expected_generation=expected_generation, stop_check=stop_check):
                return PurgeResult(0, 0)
            rows = list(
                session.scalars(
                    select(db.AppointmentsWatchState).where(db.AppointmentsWatchState.owner == owner)
                ).all()
            )
            stale_ids = [row.watch_id for row in rows if row.watch_id not in keep]
            if not stale_ids:
                return PurgeResult(0, 0)
            stale_slot_keys = list(
                session.execute(
                    select(
                        db.AppointmentsActiveSlot.owner,
                        db.AppointmentsActiveSlot.watch_id,
                        db.AppointmentsActiveSlot.slot_hash,
                    ).where(
                        db.AppointmentsActiveSlot.owner == owner,
                        db.AppointmentsActiveSlot.watch_id.in_(stale_ids),
                    )
                ).all()
            )
            alerts_deleted = session.execute(
                delete(db.AppointmentsInboxAlert).where(
                    db.AppointmentsInboxAlert.owner == owner,
                    db.AppointmentsInboxAlert.watch_id.in_(stale_ids),
                )
            ).rowcount
            slots_deleted = session.execute(
                delete(db.AppointmentsActiveSlot).where(
                    db.AppointmentsActiveSlot.owner == owner,
                    db.AppointmentsActiveSlot.watch_id.in_(stale_ids),
                )
            ).rowcount
            watches_deleted = session.execute(
                delete(db.AppointmentsWatchState).where(
                    db.AppointmentsWatchState.owner == owner,
                    db.AppointmentsWatchState.watch_id.in_(stale_ids),
                )
            ).rowcount
            reads_deleted = _delete_slot_read_markers(
                session, [(r[0], r[1], r[2]) for r in stale_slot_keys], now
            )
            reads_deleted += _delete_attention_read_markers(session, owner, stale_ids, now)
        return PurgeResult(
            watches_deleted=watches_deleted,
            slots_deleted=slots_deleted,
            alerts_deleted=alerts_deleted,
            reads_deleted=reads_deleted,
        )

    def purge_owner(self, owner: str) -> PurgeResult:
        """Immediately hard-delete all appointment data for one owner and bump the generation fence."""
        now = self._now()
        with self._immediate_scope() as session:
            alerts_deleted = session.execute(
                delete(db.AppointmentsInboxAlert).where(db.AppointmentsInboxAlert.owner == owner)
            ).rowcount
            slots_deleted = session.execute(
                delete(db.AppointmentsActiveSlot).where(db.AppointmentsActiveSlot.owner == owner)
            ).rowcount
            watches_deleted = session.execute(
                delete(db.AppointmentsWatchState).where(db.AppointmentsWatchState.owner == owner)
            ).rowcount
            session.execute(
                delete(db.AppointmentsPollerState).where(db.AppointmentsPollerState.owner == owner)
            )
            reads_deleted, audits_deleted = _purge_all_appointment_reads_and_audits(
                session, owner=owner, now=now
            )
            _bump_generation(session)
        return PurgeResult(
            watches_deleted=watches_deleted,
            slots_deleted=slots_deleted,
            alerts_deleted=alerts_deleted,
            reads_deleted=reads_deleted,
            audits_deleted=audits_deleted,
        )

    def prune_expired(
        self,
        *,
        enabled: bool,
        expected_generation: int | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> PurgeResult:
        """Purge on disable (fenced when bound to a generation); when enabled, enforce alert & slot TTLs."""
        now = self._now()
        today_iso = _today_warsaw(now).isoformat()
        with self._immediate_scope() as session:
            if not enabled:
                if expected_generation is not None:
                    if _read_disabled_at(session) is None or _read_generation(session) != expected_generation:
                        return PurgeResult(0, 0, fenced=True)
                    purged = _delete_all_appointment_state(session, now)
                    return PurgeResult(
                        watches_deleted=purged.watches_deleted,
                        slots_deleted=purged.slots_deleted,
                        alerts_deleted=purged.alerts_deleted,
                        reads_deleted=purged.reads_deleted,
                        audits_deleted=purged.audits_deleted,
                        generation=expected_generation,
                    )
                purged = _delete_all_appointment_state(session, now)
                gen = _bump_generation(session)
                _set_disabled_at(session, now)
                return PurgeResult(
                    watches_deleted=purged.watches_deleted,
                    slots_deleted=purged.slots_deleted,
                    alerts_deleted=purged.alerts_deleted,
                    reads_deleted=purged.reads_deleted,
                    audits_deleted=purged.audits_deleted,
                    generation=gen,
                )

            if self._is_fenced(session, expected_generation=expected_generation, stop_check=stop_check):
                return PurgeResult(0, 0, fenced=True)
            alerts_deleted = session.execute(
                delete(db.AppointmentsInboxAlert).where(db.AppointmentsInboxAlert.expires_at <= now)
            ).rowcount
            expired_slots = list(
                session.execute(
                    select(
                        db.AppointmentsActiveSlot.owner,
                        db.AppointmentsActiveSlot.watch_id,
                        db.AppointmentsActiveSlot.slot_hash,
                    ).where(
                        (db.AppointmentsActiveSlot.slot_date < today_iso)
                        | (db.AppointmentsActiveSlot.expires_at <= now)
                    )
                ).all()
            )
            slots_deleted = 0
            reads_deleted = 0
            if expired_slots:
                for slot_owner, slot_watch_id, slot_hash in expired_slots:
                    alerts_deleted += session.execute(
                        delete(db.AppointmentsInboxAlert).where(
                            db.AppointmentsInboxAlert.owner == slot_owner,
                            db.AppointmentsInboxAlert.watch_id == slot_watch_id,
                            db.AppointmentsInboxAlert.slot_hash == slot_hash,
                        )
                    ).rowcount
                    slots_deleted += session.execute(
                        delete(db.AppointmentsActiveSlot).where(
                            db.AppointmentsActiveSlot.owner == slot_owner,
                            db.AppointmentsActiveSlot.watch_id == slot_watch_id,
                            db.AppointmentsActiveSlot.slot_hash == slot_hash,
                        )
                    ).rowcount
                reads_deleted = _delete_slot_read_markers(
                    session, [(r[0], r[1], r[2]) for r in expired_slots], now
                )
        return PurgeResult(
            watches_deleted=0,
            slots_deleted=slots_deleted,
            alerts_deleted=alerts_deleted,
            reads_deleted=reads_deleted,
            audits_deleted=0,
        )


def prune_expired_appointments(
    path: str | Path | None = None,
    *,
    now: float | None = None,
    enabled: bool | None = None,
    session_factory: sessionmaker[Session] | None = None,
    expected_generation: int | None = None,
) -> PurgeResult:
    """Always-on idempotent purge hook for server startup and daily housekeeping.

    Never gated on ``HESTIA_APPOINTMENTS_ENABLED``: purges all appointment state, alerts,
    read markers, and legacy audit rows immediately (bumping generation and persisting
    ``appointments_disabled_at``) whenever the flag is OFF, and enforces short alert TTL +
    active slot TTL whenever the flag is ON (fenced by ``expected_generation`` and
    ``appointments_disabled_at``). Never clears ``appointments_disabled_at``.
    """
    flag_on = is_enabled() if enabled is None else enabled
    clock = time.time if now is None else (lambda: now)

    if session_factory is None:
        target = db.db_path() if path is None else Path(path)
        if not target.exists():
            return PurgeResult(0, 0)
        engine, session_factory = db.init_db(target)
    else:
        engine = session_factory.kw["bind"]

    tables = set(inspect(engine).get_table_names())
    if not _APPT_TABLES <= tables:
        return PurgeResult(0, 0)

    store = AppointmentsStore(session_factory, clock=clock)
    result = store.prune_expired(enabled=flag_on, expected_generation=expected_generation)
    if any((
        result.watches_deleted,
        result.slots_deleted,
        result.alerts_deleted,
        result.reads_deleted,
        result.audits_deleted,
    )):
        log.info(
            "Appointments purge completed watches_deleted=%d slots_deleted=%d "
            "alerts_deleted=%d reads_deleted=%d audits_deleted=%d",
            result.watches_deleted,
            result.slots_deleted,
            result.alerts_deleted,
            result.reads_deleted,
            result.audits_deleted,
        )
    return result
