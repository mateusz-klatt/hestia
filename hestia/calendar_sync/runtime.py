"""Opt-in background polling, shared by the standalone server and proxy."""
from __future__ import annotations

import asyncio
import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, load_config, make_providers

log = logging.getLogger("hestia.calendar_sync")


class SyncBusyError(RuntimeError):
    """Another process is currently reconciling the same database."""


@contextmanager
def sync_lock(path):
    """Serialize configure/pull/push across CLI and server processes on this DB."""
    lock_path = Path(str(Path(path).resolve()) + ".calendar-sync.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncBusyError("Calendar synchronization is already running") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_once(config, *, path=None):
    from ..db import db_path, init_db
    from .engine import SyncEngine
    from .store import SyncStore

    target = db_path() if path is None else path
    with sync_lock(target):
        _, sessions = init_db(target)
        store = SyncStore(sessions)
        store.configure(config.store_groups())
        return SyncEngine(store, make_providers(config)).run_once()


async def _settled_thread(operation):
    """Cancellation waits for the blocking round to finish before cleanup releases locks."""
    worker = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.gather(worker, return_exceptions=True)
        raise


async def poll_calendars() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Calendar synchronization disabled: %s", exc)
        return
    if config is None:
        return
    log.info("Calendar synchronization enabled for %d explicit groups", len(config.groups))
    while True:
        try:
            await _settled_thread(lambda: run_once(config))
        except SyncBusyError:
            log.info("Calendar synchronization round skipped: another worker is active")
        except ConfigError as exc:
            log.error("Calendar synchronization configuration error: %s", exc)
        except Exception as exc:
            # Provider/HTTP exception strings may include Authorization headers or URLs.
            log.warning("Calendar synchronization round failed (%s); pending changes retained",
                        type(exc).__name__)
        await asyncio.sleep(config.poll_seconds)
