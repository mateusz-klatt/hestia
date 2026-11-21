"""Default-off durable refresh executor and opt-in unattended poller (budget phase 2B.jobs).

One worker thread runs blocking acquisition off the event loop. ``BudgetJobRunner`` is the
synchronous core (``poll``/``step``) with injectable clocks and client factory, so tests and
offline pilots drive it on a simulated clock without sleeping. ``BudgetJobService`` only
schedules it from an aiohttp cleanup context. Neither grants quota: persisted debits, cadence
floors, Retry-After deadlines and component leases from 2A remain the only allowance.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import random
import threading
import time

from .client import EnableBankingClient
from .config import BudgetConfigError, load_config
from .job_store import BudgetJobStore
from .runtime import BudgetBusyError, acquire, budget_lock
from .scheduler import BudgetScheduler
from .store import OPERATIONS, BudgetNotFound, BudgetStoreError

JOBS_FLAG = "HESTIA_BUDGET_JOBS_ENABLED"
POLLER_FLAG = "HESTIA_BUDGET_POLLER_ENABLED"
POLL_SECONDS = 60
JITTER_SECONDS = 10
BUSY_DEFER_SECONDS = 900
SHUTDOWN_GRACE_SECONDS = 5
log = logging.getLogger("hestia.budget.jobs")


class BudgetJobsUnavailable(RuntimeError):
    """A prerequisite is missing; the executor stays disabled and refresh is unavailable."""


def _target(connection, primary, operation):
    """Canonical component set for a job; None means every selected component of the connection."""
    if primary is None and operation is None:
        return None
    hashes = connection.accounts if primary is None else (primary,)
    operations = OPERATIONS if operation is None else (operation,)
    return frozenset((item, op) for item in hashes for op in operations)


class BudgetJobRunner:
    """Synchronous job core; every call holds the per-database budget lock only for its own work."""

    def __init__(self, path, *, clock=time.time, monotonic=time.monotonic, sleeper=time.sleep,
                 client_factory=EnableBankingClient, config_loader=load_config, poller=False):
        self.path, self.clock, self.monotonic, self.sleeper = path, clock, monotonic, sleeper
        self.client_factory, self.config_loader = client_factory, config_loader
        # Poller intents only ever run while unattended acquisition is enabled in this process.
        self.poller = poller
        self.stopping = threading.Event()

    def _config(self):
        config = self.config_loader()
        if config is None:
            raise BudgetConfigError("Budget configuration is required")
        return config

    def _store(self):
        from ..db import init_db

        _, sessions = init_db(self.path)
        store = BudgetJobStore(sessions, clock=self.clock)
        with sessions() as session:
            if not store.users_authoritative(session):
                raise BudgetJobsUnavailable("Budget jobs require authoritative SQLite users")
        return store

    def check(self):
        """Startup prerequisites: readable configuration and authoritative SQLite users; no secrets."""
        self._config()
        self._store()

    def enqueue_refresh(self, owner, connection_id, **target):
        return self._store().enqueue_refresh(owner, connection_id, **target)

    def poll(self):
        """Enqueue at most one intent per eligible, due connection; never reads secrets or calls a bank."""
        if not self.poller:
            return 0
        config = self._config()
        try:
            with budget_lock(self.path):
                store = self._store()
                store.configure(config)
                candidates = set(store.background_candidates())
                scheduler = BudgetScheduler(store, application_id=config.application_id)
                added = 0
                for connection in config.connections:
                    if connection.id in candidates and scheduler.can_start(connection):
                        added += store.enqueue_background(connection.id) is not None
                return added
        except BudgetBusyError:
            return 0

    def step(self):
        """Run at most one queued job; None when idle or while another acquisition holds the lock."""
        config = self._config()
        try:
            with budget_lock(self.path):
                store = self._store()
                store.interrupt_orphans()
                store.prune()
                job = None if self.stopping.is_set() else store.claim_next()
                if job is None:
                    return None
                return self._execute(store, config, job)
        except BudgetBusyError:
            self._defer_stale()
            return None

    def _defer_stale(self):
        """A lock held elsewhere for too long must not keep a queued job's slot indefinitely."""
        store = self._store()
        cutoff = self.clock() - BUSY_DEFER_SECONDS
        for job in store.queued_before(cutoff):
            store.defer_queued(job, "busy")

    def _execute(self, store, config, job):
        connection = next((item for item in config.connections if item.id == job["connection_id"]), None)
        if connection is None:
            return store.finish(job["id"], "deferred", "configuration")
        if job["source"] == "poller" and not (self.poller and store.background_eligible(connection.id)):
            # A queued automatic intent never outlives disabling the poller, an opt-out or attention.
            return store.finish(job["id"], "deferred", "background_disabled")
        try:
            store.configure(config)
            primary = store.target_hash(job)
        except BudgetNotFound:
            return store.finish(job["id"], "deferred", "account_selection")
        except BudgetStoreError:
            return store.finish(job["id"], "failed", "configuration")
        target = _target(connection, primary, job["operation"])
        scheduler = BudgetScheduler(store, application_id=config.application_id)
        state, error = acquire(config, connection, store, scheduler, self.client_factory, self.clock,
                               self.monotonic, self.sleeper, target=target,
                               expected_version=job["expected_version"], stop=self.stopping.is_set)
        return store.finish(job["id"], state, error, next_attempt=store.next_attempt(connection.id, target))


class BudgetJobService:
    """Asyncio supervisor: wake on enqueue, poll on a jittered cadence, drain off-loop."""

    def __init__(self, runner, *, poller=False, poll_seconds=POLL_SECONDS, jitter_seconds=JITTER_SECONDS,
                 jitter=random.random, grace_seconds=SHUTDOWN_GRACE_SECONDS):
        self.runner, self.poller = runner, poller
        self.poll_seconds, self.jitter_seconds, self.jitter = poll_seconds, jitter_seconds, jitter
        self.grace_seconds = grace_seconds
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hestia-budget-jobs")
        self._wake = asyncio.Event()
        self._task = None
        self._inflight = None

    @property
    def accepting(self):
        return self._task is not None and not self._task.done() and not self.runner.stopping.is_set()

    def wake(self):
        self._wake.set()

    async def _call(self, operation):
        future = asyncio.get_running_loop().run_in_executor(self._pool, operation)
        # Retrieve a late result even when the awaiting supervisor was cancelled at shutdown.
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        self._inflight = future
        # asyncio.wait never cancels the executor future when the supervisor itself is cancelled.
        await asyncio.wait({future})
        return future.result()

    async def _round(self, loop, next_poll):
        if self.poller and loop.time() >= next_poll:
            await self._call(self.runner.poll)
            next_poll = loop.time() + self.poll_seconds + self.jitter() * self.jitter_seconds
        while not self.runner.stopping.is_set() and await self._call(self.runner.step) is not None:
            pass
        return next_poll

    async def _loop(self):
        loop = asyncio.get_running_loop()
        next_poll = loop.time()
        while not self.runner.stopping.is_set():
            self._wake.clear()
            try:
                next_poll = await self._round(loop, next_poll)
            except Exception as exc:
                # Exception text may carry paths or provider details; log the class only.
                log.warning("Budget job round failed (%s); queued intents are retained", type(exc).__name__)
                next_poll = max(next_poll, loop.time() + self.poll_seconds)
            delay = max(0.0, next_poll - loop.time()) if self.poller else self.poll_seconds
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self):
        """Refuse new work, then wait a bounded grace for in-flight work to stop at a request boundary.

        The blocking request is never cancelled or reported as cancelled: if it outlives the grace,
        its lease and debit stay in force and the next start reports the job as interrupted.
        """
        self.runner.stopping.set()
        self._wake.set()
        if self._task is not None:
            # An idle supervisor exits at once; a busy one after its step reaches a request boundary.
            await asyncio.wait({self._task}, timeout=self.grace_seconds)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._pool.shutdown(wait=False)


def _flag(name):
    return os.environ.get(name) == "1"


def build_service(*, auth_enabled, path=None):
    """Return a started-ready service, or None when disabled or a prerequisite is missing."""
    from ..db import db_path

    if not _flag(JOBS_FLAG):
        if _flag(POLLER_FLAG):
            log.error("Budget poller requires %s=1; unattended acquisition stays disabled", JOBS_FLAG)
        return None
    if not auth_enabled() or os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        log.error("Budget jobs require authentication and SQLite users; executor stays disabled")
        return None
    runner = BudgetJobRunner(db_path() if path is None else path, poller=_flag(POLLER_FLAG))
    try:
        runner.check()
    except Exception as exc:
        # Never let an optional financial feature abort the home-automation web server's startup.
        log.error("Budget jobs disabled (%s)", type(exc).__name__)
        return None
    return BudgetJobService(runner, poller=runner.poller)


class ServiceSlot:
    """Set before startup so the lifecycle never mutates a frozen aiohttp application."""

    service: BudgetJobService | None = None


def register(app, key, *, auth_enabled):
    """Install the executor lifecycle; the default-off flags leave the slot empty."""
    slot = ServiceSlot()
    app[key] = slot

    async def lifecycle(_app):
        service = await asyncio.to_thread(build_service, auth_enabled=auth_enabled)
        slot.service = service
        if service is not None:
            service.start()
            log.info("Budget job executor enabled (poller %s)", "enabled" if service.poller else "disabled")
        try:
            yield
        finally:
            slot.service = None
            if service is not None:
                await service.stop()

    app.cleanup_ctx.append(lifecycle)
