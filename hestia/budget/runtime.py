"""Explicit background-style acquisition; not installed as a server poller."""
from __future__ import annotations

import fcntl
import math
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

from .client import BudgetAPIError, EnableBankingClient, ProviderDiagnostic
from .config import BudgetConfigError, credentials
from .normalize import BudgetDataError, fetch_snapshot
from .store import ATTENTION, OPERATIONS, BudgetStore, BudgetStoreError, BudgetPausedError, safe_error
from .scheduler import BudgetScheduler, BudgetDeferredError

ACQUISITION_SECONDS = 900


class BudgetBusyError(RuntimeError):
    """Another bank acquisition process holds this database's lock."""


class BudgetStopRequested(BudgetDeferredError):
    """Process shutdown observed between requests; no request is in flight at that point."""


def _error_code(exc):
    if getattr(exc, "configuration", False):
        return "configuration"
    if isinstance(exc, BudgetAPIError):
        return "configuration" if exc.code == "invalid_configuration" else exc.code
    if isinstance(exc, BudgetConfigError):
        return "configuration"
    if isinstance(exc, BudgetDataError):
        return exc.code
    return "invalid_data" if isinstance(exc, BudgetStoreError) else "internal_error"


def _wanted(target, primary, operation):
    return target is None or (primary, operation) in target


def _snapshot_errors(snapshot, target):
    errors = [error.code for error in snapshot.errors
              if any(_wanted(target, error.identification_hash, op) for op in OPERATIONS)]
    return errors + [getattr(account, op + "_error") for account in snapshot.accounts for op in OPERATIONS
                     if _wanted(target, account.identification_hash, op)
                     and getattr(account, op + "_error", None) is not None]


def _snapshot_state(snapshot, target=None):
    """Outcome relative to the requested components; untargeted ones are not failures."""
    wanted = [(account, op) for account in snapshot.accounts for op in OPERATIONS
              if _wanted(target, account.identification_hash, op)]
    complete = sum(getattr(account, op) is not None for account, op in wanted)
    if _snapshot_errors(snapshot, target):
        return "partial" if complete else "failed"
    if not complete:
        return "deferred"
    return "updated" if complete == len(wanted) else "partial"


def _monotonic_time(clock):
    try:
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise BudgetAPIError(code="invalid_configuration") from None
    return now


class AcquisitionClient:
    """One bounded round; every GET rechecks durable limits outside network I/O."""

    def __init__(self, client, scheduler, connection, attempt_id, *, monotonic, sleeper, guard=None, target=None):
        self.client, self.scheduler, self.connection = client, scheduler, connection
        self.attempt_id, self.monotonic, self.sleeper = attempt_id, monotonic, sleeper
        self.guard = guard if guard is not None else lambda: None
        self.target = target
        self.deadline = _monotonic_time(monotonic) + ACQUISITION_SECONDS
        self.valid_until = None
        self.primary = self.operation = None

    def session_validated(self, valid_until):
        self.valid_until = valid_until

    def begin_component(self, primary, operation):
        if not _wanted(self.target, primary, operation):
            return False
        self.guard()
        self.primary, self.operation = primary, operation
        if _monotonic_time(self.monotonic) >= self.deadline:
            return False
        return self.scheduler.reserve_component(self.connection, primary, operation,
                                                attempt_id=self.attempt_id, valid_until=self.valid_until)

    def _request_slot(self):
        while True:
            self.guard()
            remaining = self.deadline - _monotonic_time(self.monotonic)
            if remaining <= 0:
                raise BudgetAPIError(code="acquisition_timeout", retryable=True)
            delay = self.scheduler.request_wait(self.connection, primary=self.primary, operation=self.operation,
                                                attempt_id=self.attempt_id, valid_until=self.valid_until,
                                                target=self.target)
            if not delay:
                self.guard()
                return
            if delay >= remaining:
                raise BudgetDeferredError()
            self.sleeper(delay)

    def get(self, path, query=None):
        self._request_slot()
        try:
            if isinstance(self.client, EnableBankingClient):
                result = self.client.get(path, query=query, deadline=self.deadline)
            else:
                result = self.client.get(path, query=query)
        except BudgetAPIError as exc:
            self.scheduler.record_rate_limit(self.connection, primary=self.primary, operation=self.operation,
                                             error=exc, valid_until=self.valid_until)
            self.guard()
            raise
        self.guard()
        if _monotonic_time(self.monotonic) >= self.deadline:
            raise BudgetAPIError(code="acquisition_timeout", retryable=True)
        return result


@contextmanager
def budget_lock(path):
    target = Path(str(Path(path).resolve()) + ".budget-sync.lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BudgetBusyError("Bank acquisition is already running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _guard(store, connection, version, stop):
    if stop is not None and stop():
        raise BudgetStopRequested()
    store.acquisition_version(connection.id, connection.owner, expected_version=version)


def acquire(config, connection, store, scheduler, client_factory, clock, monotonic, sleeper, *,
            target=None, expected_version=None, stop=None):
    """One bounded round for a full-config connection; returns (state, safe error code or None).

    ``target`` narrows which (primary hash, operation) components may be reserved. Untargeted
    components are skipped before any reservation, so they consume no debit and record no error.
    """
    attempt_id = str(uuid.uuid4())
    try:
        version = store.acquisition_version(connection.id, connection.owner, expected_version=expected_version)
        if not scheduler.can_start(connection, target=target):
            return "deferred", None
        chosen = store.selected_hashes(connection.id, connection.owner)
        selected = replace(connection, accounts=tuple(h for h in connection.accounts if h in chosen))
        key, session_id = credentials(config, connection)
        client = AcquisitionClient(client_factory(config.application_id, key), scheduler, connection, attempt_id,
                                   monotonic=monotonic, sleeper=sleeper, target=target,
                                   guard=lambda: _guard(store, connection, version, stop))
        snapshot = fetch_snapshot(client, session_id, selected,
                                  today=datetime.fromtimestamp(clock(), timezone.utc).date(), now=clock(),
                                  before_component=client.begin_component, on_session=client.session_validated,
                                  wanted=None if target is None else lambda primary, op: _wanted(target, primary, op))
        store.commit_snapshot(connection.id, snapshot, attempt_id=attempt_id, expected_version=version)
    except BudgetStopRequested:
        store.release_round(connection.id, attempt_id)
        return "interrupted", "interrupted"
    except BudgetDeferredError:
        store.release_round(connection.id, attempt_id)
        return "deferred", None
    except BudgetPausedError:
        store.release_round(connection.id, attempt_id)
        return "deferred", "control_changed"
    except Exception as exc:
        # Storage failure here still escapes to the safe CLI; bank/programming
        # failures must not starve independent connections.
        code = _error_code(exc)
        store.record_failure(connection.id, code, attempt_id=attempt_id, diagnostic=ProviderDiagnostic.of(exc))
        return "failed", safe_error(code)
    errors = [safe_error(code) for code in _snapshot_errors(snapshot, target)]
    # A session/authorization failure must win over an unrelated earlier component error.
    error = next((code for code in errors if code in ATTENTION), errors[0] if errors else None)
    return _snapshot_state(snapshot, target), error


def _run_connection(config, connection, store, scheduler, client_factory, clock, monotonic, sleeper):
    state, _ = acquire(config, connection, store, scheduler, client_factory, clock, monotonic, sleeper)
    return {"connection": connection.id, "state": state}


def run_once(config, *, path=None, clock=time.time, client_factory=EnableBankingClient,
             monotonic=time.monotonic, sleeper=time.sleep):
    from ..db import db_path, init_db

    target = db_path() if path is None else path
    with budget_lock(target):
        _, sessions = init_db(target)
        store = BudgetStore(sessions, clock=clock)
        store.configure(config)
        from .consent_runtime import resolve_config

        config = resolve_config(config, path=target, clock=clock)
        scheduler = BudgetScheduler(store, application_id=config.application_id)
        results = [_run_connection(config, connection, store, scheduler, client_factory, clock, monotonic, sleeper)
                   for connection in config.connections]
        return {"results": results, "status": store.status()}
