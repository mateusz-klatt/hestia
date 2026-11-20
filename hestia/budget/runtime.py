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

from .client import BudgetAPIError, EnableBankingClient
from .config import BudgetConfigError, credentials
from .normalize import BudgetDataError, fetch_snapshot
from .store import BudgetStore, BudgetStoreError
from .scheduler import BudgetScheduler, BudgetDeferredError

ACQUISITION_SECONDS = 900


class BudgetBusyError(RuntimeError):
    """Another bank acquisition process holds this database's lock."""


def _error_code(exc):
    if isinstance(exc, BudgetAPIError):
        return "configuration" if exc.code == "invalid_configuration" else exc.code
    if isinstance(exc, BudgetConfigError):
        return "configuration"
    if isinstance(exc, BudgetDataError):
        return exc.code
    return "invalid_data" if isinstance(exc, BudgetStoreError) else "internal_error"


def _snapshot_state(snapshot):
    complete = sum(getattr(account, op) is not None for account in snapshot.accounts
                   for op in ("transactions", "balances"))
    errors = len(snapshot.errors) + sum(getattr(account, op + "_error", None) is not None
                                       for account in snapshot.accounts for op in ("transactions", "balances"))
    if errors:
        return "partial" if complete else "failed"
    if not complete:
        return "deferred"
    return "updated" if complete == len(snapshot.accounts) * 2 else "partial"


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

    def __init__(self, client, scheduler, connection, attempt_id, *, monotonic, sleeper):
        self.client, self.scheduler, self.connection = client, scheduler, connection
        self.attempt_id, self.monotonic, self.sleeper = attempt_id, monotonic, sleeper
        self.deadline = _monotonic_time(monotonic) + ACQUISITION_SECONDS
        self.valid_until = None
        self.primary = self.operation = None

    def session_validated(self, valid_until):
        self.valid_until = valid_until

    def begin_component(self, primary, operation):
        self.primary, self.operation = primary, operation
        if _monotonic_time(self.monotonic) >= self.deadline:
            return False
        return self.scheduler.reserve_component(self.connection, primary, operation,
                                                attempt_id=self.attempt_id, valid_until=self.valid_until)

    def _request_slot(self):
        while True:
            remaining = self.deadline - _monotonic_time(self.monotonic)
            if remaining <= 0:
                raise BudgetAPIError(code="acquisition_timeout", retryable=True)
            delay = self.scheduler.request_wait(self.connection, primary=self.primary, operation=self.operation,
                                                attempt_id=self.attempt_id, valid_until=self.valid_until)
            if not delay:
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
            raise
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


def _run_connection(config, connection, store, scheduler, client_factory, clock, monotonic, sleeper):
    attempt_id = str(uuid.uuid4())
    try:
        if not scheduler.can_start(connection):
            return {"connection": connection.id, "state": "deferred"}
        chosen = store.selected_hashes(connection.id, connection.owner)
        selected = replace(connection, accounts=tuple(h for h in connection.accounts if h in chosen))
        key, session_id = credentials(config, connection)
        client = AcquisitionClient(client_factory(config.application_id, key), scheduler, connection, attempt_id,
                                   monotonic=monotonic, sleeper=sleeper)
        snapshot = fetch_snapshot(client, session_id, selected,
                                  today=datetime.fromtimestamp(clock(), timezone.utc).date(), now=clock(),
                                  before_component=client.begin_component, on_session=client.session_validated)
        store.commit_snapshot(connection.id, snapshot, attempt_id=attempt_id)
    except BudgetDeferredError:
        state = "deferred"
    except Exception as exc:
        # Storage failure here still escapes to the safe CLI; bank/programming
        # failures must not starve independent connections.
        store.record_failure(connection.id, _error_code(exc), attempt_id=attempt_id)
        state = "failed"
    else:
        state = _snapshot_state(snapshot)
    return {"connection": connection.id, "state": state}


def run_once(config, *, path=None, clock=time.time, client_factory=EnableBankingClient,
             monotonic=time.monotonic, sleeper=time.sleep):
    from ..db import db_path, init_db

    target = db_path() if path is None else path
    with budget_lock(target):
        _, sessions = init_db(target)
        store = BudgetStore(sessions, clock=clock)
        store.configure(config)
        scheduler = BudgetScheduler(store, application_id=config.application_id)
        results = [_run_connection(config, connection, store, scheduler, client_factory, clock, monotonic, sleeper)
                   for connection in config.connections]
        return {"results": results, "status": store.status()}
