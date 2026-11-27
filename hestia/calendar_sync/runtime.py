"""Opt-in background polling, shared by the standalone server and proxy."""
from __future__ import annotations

import asyncio
import concurrent.futures
import fcntl
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, load_config, make_providers

log = logging.getLogger("hestia.calendar_sync")
_EXECUTOR = None
_EXECUTOR_LOCK = threading.Lock()


def configure_debug_logging(environ=None) -> bool:
    """Bump only the calendar-sync logger to DEBUG when explicitly opted in."""
    env = os.environ if environ is None else environ
    if env.get("HESTIA_CALENDAR_SYNC_DEBUG") == "1":
        log.setLevel(logging.DEBUG)
        return True
    return False


def _background_priority():
    """Pin this worker at nice 10 or worse, and leave every other thread alone.

    ``os.nice`` adds a delta to the whole process on non-Linux POSIX and would stack
    on a process that is already niced. ``setpriority`` on this thread's id is absolute.
    """
    if not (hasattr(os, "getpriority") and hasattr(os, "setpriority") and hasattr(threading, "get_native_id")):
        return
    try:
        tid = threading.get_native_id()
        current = os.getpriority(os.PRIO_PROCESS, tid)
        os.setpriority(os.PRIO_PROCESS, tid, max(current, 10))
    except OSError:
        return


def _calendar_executor():
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="calendar-sync", initializer=_background_priority)
        return _EXECUTOR


def close_executor():
    """Wait for the background worker to finish, then allow a later poll to start a new one."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=True)


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
    """Cancellation waits for the blocking round to finish before cleanup releases locks.

    The round runs on a single low-priority thread so a full calendar pull (every replica,
    every restart) does not share the default executor with budget analytics or outrank
    other services on a small host.
    """
    loop = asyncio.get_running_loop()
    worker = asyncio.ensure_future(loop.run_in_executor(_calendar_executor(), operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await asyncio.gather(worker, return_exceptions=True)
        raise


async def poll_calendars() -> None:
    try:
        try:
            config = load_config()
        except ConfigError as exc:
            log.error("Calendar synchronization disabled: %s", exc)
            return
        if config is None:
            return
        configure_debug_logging()
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
    finally:
        close_executor()
