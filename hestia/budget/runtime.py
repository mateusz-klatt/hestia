"""Explicit background-style acquisition; not installed as a server poller."""
from __future__ import annotations

import fcntl
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


def run_once(config, *, path=None, clock=time.time, client_factory=EnableBankingClient):
    from ..db import db_path, init_db

    target = db_path() if path is None else path
    with budget_lock(target):
        _, sessions = init_db(target)
        store = BudgetStore(sessions, clock=clock)
        store.configure(config)
        results = []
        for connection in config.connections:
            chosen = store.selected_hashes(connection.id, connection.owner)
            selected_connection = replace(connection, accounts=tuple(h for h in connection.accounts if h in chosen))
            if not store.start_attempt(connection.id):
                results.append({"connection": connection.id, "state": "deferred"})
                continue
            attempt_id = str(uuid.uuid4())
            try:
                key, session_id = credentials(config, connection)
                client = client_factory(config.application_id, key)
                snapshot = fetch_snapshot(client, session_id, selected_connection,
                                          today=datetime.fromtimestamp(clock(), timezone.utc).date(), now=clock(),
                                          before_component=lambda primary, op: store.start_component_attempt(
                                              connection.id, primary, op, attempt_id=attempt_id))
                store.commit_snapshot(connection.id, snapshot, attempt_id=attempt_id)
            except Exception as exc:
                # One provider/programming failure must not starve independent
                # connections. Storage failure here still escapes to the safe CLI.
                store.record_failure(connection.id, _error_code(exc), attempt_id=attempt_id)
                results.append({"connection": connection.id, "state": "failed"})
            else:
                state = _snapshot_state(snapshot)
                results.append({"connection": connection.id, "state": state})
        return {"results": results, "status": store.status()}
