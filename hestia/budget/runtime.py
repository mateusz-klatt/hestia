"""Explicit background-style acquisition; not installed as a server poller."""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .client import BudgetAPIError, EnableBankingClient
from .config import BudgetConfigError, credentials
from .normalize import BudgetDataError, fetch_snapshot
from .store import BudgetStore, BudgetStoreError


class BudgetBusyError(RuntimeError):
    """Another bank acquisition process holds this database's lock."""


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
            if not store.start_attempt(connection.id):
                results.append({"connection": connection.id, "state": "deferred"})
                continue
            try:
                key, session_id = credentials(config, connection)
                client = client_factory(config.application_id, key)
                snapshot = fetch_snapshot(client, session_id, connection,
                                          today=datetime.fromtimestamp(clock(), timezone.utc).date(), now=clock())
                store.commit_snapshot(connection.id, snapshot)
            except (BudgetAPIError, BudgetDataError, BudgetConfigError, BudgetStoreError) as exc:
                if isinstance(exc, BudgetAPIError):
                    code = "configuration" if exc.code == "invalid_configuration" else exc.code
                elif isinstance(exc, BudgetConfigError):
                    code = "configuration"
                elif isinstance(exc, BudgetDataError):
                    code = exc.code
                else:
                    code = "invalid_data"
                store.record_failure(connection.id, code)
                results.append({"connection": connection.id, "state": "failed"})
            else:
                state = ("partial" if snapshot.accounts else "failed") if snapshot.errors else "updated"
                results.append({"connection": connection.id, "state": state})
        return {"results": results, "status": store.status()}
