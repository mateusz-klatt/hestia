"""Durable refresh intents and the owner background opt-in; short SQLite transactions only.

Jobs are local intent records. Persisted acquisition debits, cadence floors, Retry-After
deadlines and component leases stay authoritative: a job never grants allowance.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..db import AppMeta, BudgetAccount as Account, BudgetConnection as Connection
from ..db import BudgetComponent as Component, BudgetConnectionAudit as ConnectionAudit
from ..db import BudgetJob as Job, BudgetJobRequest as JobRequest
from .scheduler import shared_state
from .store import OPERATIONS, BudgetNotFound, BudgetStore, BudgetStoreError, _json, safe_error

ACTIVE = ("queued", "running")
OUTCOMES = frozenset({"updated", "partial", "failed", "deferred", "interrupted"})
ATTENTION = frozenset({"authorization", "invalid_session", "expired_session", "session_mismatch", "configuration"})
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)
MAX_QUEUED = 32
RETENTION_SECONDS = 7 * 86400
LIST_LIMIT = 20
_DIGEST_DOMAIN = "hestia-budget-refresh-v1"


class BudgetJobError(BudgetStoreError):
    """Fixed, value-free refusal codes."""

    status = 409
    code = "budget_refresh_in_progress"

    def __init__(self):
        super().__init__(self.code)


class BudgetRefreshBusy(BudgetJobError):
    """A different target is already queued or running for this connection."""


class BudgetIdempotencyConflict(BudgetJobError):
    code = "budget_idempotency_conflict"


class BudgetConnectionPaused(BudgetJobError):
    code = "budget_connection_paused"


class BudgetQueueFull(BudgetJobError):
    status = 503
    code = "budget_refresh_unavailable"


def _digest(connection_id, account_id, operation):
    raw = json.dumps([_DIGEST_DOMAIN, connection_id, account_id, operation], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BudgetJobStore(BudgetStore):
    """Jobs bind to one owner/connection; at most one queued/running job per connection."""

    @staticmethod
    def _job_dict(job):
        return {"id": job.id, "connection_id": job.connection_id, "account_id": job.account_id,
                "operation": job.operation, "source": job.source, "status": job.status, "error": job.error,
                "next_attempt": job.next_attempt, "created_at": job.created_at,
                "started_at": job.started_at, "finished_at": job.finished_at}

    def set_connection_background(self, owner, connection_id, enabled, *, actor, origin="operator",
                                  expected_version=None):
        """Persist the owner's unattended-acquisition opt-in; the global flag never implies it."""
        self._actor(actor, origin, owner=owner)
        if type(enabled) is not bool:
            raise BudgetStoreError("Background selection must be a boolean")
        with self.transaction() as session:
            connection = self._owned_connection(session, owner, connection_id)
            changed = connection.background_enabled != enabled
            self._check_version(connection, expected_version, changed, self._connection_dict(connection), origin)
            if changed:
                session.add(ConnectionAudit(connection_id=connection.id, owner=owner, actor=actor, origin=origin,
                                             field="background", old_value_json=_json(connection.background_enabled),
                                             new_value_json=_json(enabled), changed_at=self.clock()))
                connection.background_enabled = enabled
                connection.control_version += 1
            return {**self._connection_dict(connection), "changed": changed}

    def _target(self, session, owner, connection, account_id, operation):
        if operation is not None and operation not in OPERATIONS:
            raise BudgetStoreError("Unknown refresh operation")
        if account_id is None:
            return
        account, _ = self._owned_account(session, owner, account_id)
        if account.connection_id != connection.id:
            raise BudgetNotFound()
        if not (account.enabled and account.configured):
            raise BudgetStoreError("Bank account is not selected")

    def enqueue_refresh(self, owner, connection_id, *, expected_version, idempotency_key,
                        account_id=None, operation=None):
        """Create, coalesce or replay one owner refresh intent; returns the job plus ``created``."""
        if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise BudgetStoreError("Invalid idempotency key")
        digest = _digest(connection_id, account_id, operation)
        now = self.clock()
        try:
            with self.transaction() as session:
                connection = self._owned_connection(session, owner, connection_id)
                prior = session.get(JobRequest, (owner, idempotency_key))
                if prior is not None:
                    if prior.digest != digest:
                        raise BudgetIdempotencyConflict()
                    return {**self._job_dict(session.get(Job, prior.job_id)), "created": False}
                self._target(session, owner, connection, account_id, operation)
                self._check_version(connection, expected_version, True, self._connection_dict(connection), "web")
                if not connection.enabled:
                    raise BudgetNotFound()
                if connection.paused:
                    raise BudgetConnectionPaused()
                job, created = self._active_or_new(session, connection, account_id, operation, "manual", now)
                session.add(JobRequest(owner=owner, idempotency_key=idempotency_key, digest=digest,
                                       job_id=job.id, created_at=now))
                session.flush()
                return {**self._job_dict(job), "created": created}
        except IntegrityError:
            # BEGIN IMMEDIATE serializes writers; a constraint race still maps to a clean refusal.
            raise BudgetRefreshBusy() from None

    @staticmethod
    def _active_or_new(session, connection, account_id, operation, source, now):
        active = session.scalar(select(Job).where(Job.connection_id == connection.id, Job.status.in_(ACTIVE)))
        if active is not None:
            if (active.account_id, active.operation) != (account_id, operation):
                raise BudgetRefreshBusy()
            return active, False
        if session.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")) >= MAX_QUEUED:
            raise BudgetQueueFull()
        job = Job(id=str(uuid.uuid4()), owner=connection.owner, connection_id=connection.id, source=source,
                  account_id=account_id, operation=operation, expected_version=connection.control_version,
                  status="queued", created_at=now)
        session.add(job)
        session.flush()
        return job, True

    def enqueue_background(self, connection_id):
        """Poller intent for one eligible connection; an existing active job always wins."""
        now = self.clock()
        try:
            with self.transaction() as session:
                connection = session.get(Connection, connection_id)
                if not self._eligible(session, connection):
                    return None
                if session.scalar(select(Job.id).where(Job.connection_id == connection.id,
                                                       Job.status.in_(ACTIVE))) is not None:
                    return None
                job, _ = self._active_or_new(session, connection, None, None, "poller", now)
                return self._job_dict(job)
        except (IntegrityError, BudgetQueueFull):
            return None

    def _eligible(self, session, connection):
        if connection is None or not connection.enabled or connection.paused or not connection.background_enabled:
            return False
        if connection.attention is not None:
            return False
        try:
            self._owner(session, connection.owner)
        except BudgetStoreError:
            return False
        return session.scalar(select(func.count()).select_from(Account).where(
            Account.connection_id == connection.id, Account.owner == connection.owner,
            Account.enabled, Account.configured)) > 0

    def background_candidates(self):
        """Connection IDs whose stored controls allow unattended work; due-ness is checked separately."""
        with self.sessions() as session:
            rows = session.scalars(select(Connection).where(Connection.background_enabled,
                                                            Connection.enabled).order_by(Connection.id))
            return [row.id for row in rows if self._eligible(session, row)]

    @staticmethod
    def users_authoritative(session):
        return session.get(AppMeta, "users_authoritative") is not None

    def interrupt_orphans(self):
        """Only call while holding the budget lock: no other worker can be running a job then."""
        now = self.clock()
        with self.transaction() as session:
            rows = list(session.scalars(select(Job).where(Job.status == "running")))
            for job in rows:
                job.status, job.error, job.finished_at = "interrupted", "interrupted", now
            return len(rows)

    def claim_next(self):
        now = self.clock()
        with self.transaction() as session:
            job = session.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1))
            if job is None:
                return None
            job.status, job.started_at = "running", now
            return self._job_dict(job) | {"expected_version": job.expected_version, "owner": job.owner}

    def target_hash(self, job):
        """Resolve the job's local account target to its configured primary hash, or None for all."""
        if job["account_id"] is None:
            return None
        with self.sessions() as session:
            account = session.get(Account, job["account_id"])
            if account is None or account.owner != job["owner"] or account.connection_id != job["connection_id"]:
                raise BudgetNotFound()
            return account.identification_hash

    def next_attempt(self, connection_id, target):
        """Earliest stored start time for any targeted selected component, or None without one."""
        with self.sessions() as session:
            connection = session.get(Connection, connection_id)
            floors = []
            for account in session.scalars(select(Account).where(Account.connection_id == connection_id,
                                                                 Account.enabled, Account.configured)):
                for operation in OPERATIONS:
                    if target is not None and (account.identification_hash, operation) not in target:
                        continue
                    component = session.get(Component, (account.id, operation))
                    floors.append(0 if component is None else
                                  max(component.next_attempt, component.retry_at or 0, component.backoff_until))
            if connection is None or not floors:
                return None
            return max(min(floors), shared_state(session, connection)["next_attempt"])

    def finish(self, job_id, status, error=None, *, next_attempt=None):
        if status not in OUTCOMES:
            raise BudgetStoreError("Invalid job outcome")
        now = self.clock()
        with self.transaction() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "running":
                return None
            job.status, job.finished_at, job.next_attempt = status, now, next_attempt
            job.error = None if error is None else safe_error(error)
            connection = session.get(Connection, job.connection_id)
            if job.error in ATTENTION:
                connection.attention = job.error
            elif status in ("updated", "partial"):
                connection.attention = None
            return self._job_dict(job)

    def queued_before(self, cutoff):
        with self.sessions() as session:
            return list(session.scalars(select(Job.id).where(Job.status == "queued", Job.created_at < cutoff)))

    def defer_queued(self, job_id, error):
        """Terminal deferral of a still-queued job without touching acquisition state."""
        now = self.clock()
        with self.transaction() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "queued":
                return None
            job.status, job.error, job.finished_at = "deferred", safe_error(error), now
            return self._job_dict(job)

    def prune(self):
        cutoff = self.clock() - RETENTION_SECONDS
        with self.transaction() as session:
            old = select(Job.id).where(Job.status.not_in(ACTIVE), Job.created_at < cutoff)
            session.execute(delete(JobRequest).where(JobRequest.job_id.in_(old)))
            return session.execute(delete(Job).where(Job.status.not_in(ACTIVE), Job.created_at < cutoff)).rowcount

    def jobs(self, owner, connection_id=None):
        with self.sessions() as session:
            self._owner(session, owner)
            query = select(Job).where(Job.owner == owner)
            if connection_id is not None:
                self._owned_connection(session, owner, connection_id)
                query = query.where(Job.connection_id == connection_id)
            return [self._job_dict(job) for job in
                    session.scalars(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(LIST_LIMIT))]
