"""Default-off off-loop appointments poller supervisor and always-on purge housekeeping.

Hooked into Hestia's aiohttp ``app.cleanup_ctx`` (mirroring ``hestia/budget/jobs.py`` and
``hestia/budget/receipt_api.py``):
- Poller activates ONLY when ``HESTIA_APPOINTMENTS_ENABLED=1`` AND strict ``0400`` secret
  files exist and load cleanly.
- Runs jittered 10-15 min cycles strictly inside the configured ``Europe/Warsaw`` active
  window (default 06:00-23:00), rotating the persisted watch start cursor across cycles so
  every watch runs even under tight per-run request caps.
- Exponential failure backoff with circuit-break owner notification emitted once per episode.
- Always runs idempotent purge-on-disable (immediate purge whenever the flag is OFF) and
  short-TTL / slot-TTL expiry at startup and every 24h.
- Logs counts and fixed safe codes only (never credentials, labels, or patient data).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path
import random
import threading
import time

from aiohttp import web
from sqlalchemy.orm import Session, sessionmaker

from .. import db
from .cmp_client import CmpClient, CmpClientError, CmpRunLimitError, CmpSchedulerQuery, Transport
from .config import (
    AppointmentsConfigError,
    AppointmentsSettings,
    is_enabled,
    load_runtime_settings,
)
from .notify import AppointmentNotifier, AttentionNotification, SlotNotification
from .store import AppointmentsStore, AppointmentsStoreError, PurgeResult, prune_expired_appointments

APPOINTMENTS_KEY = web.AppKey("appointments_poller", object)
HOUSEKEEPING_INTERVAL_SECONDS = 86400.0
SHUTDOWN_GRACE_SECONDS = 5.0
log = logging.getLogger("hestia.appointments.poller")


@dataclass(frozen=True)
class PollCycleResult:
    """Summary of one synchronous poll cycle (counts & emitted notifications)."""

    active_window: bool
    polled: int = 0
    skipped_cooldown: int = 0
    deferred_run_limit: int = 0
    notified: int = 0
    rate_limited: int = 0
    attention_alerts: int = 0
    errors: int = 0
    next_delay_seconds: float = 0.0
    notifications: tuple[SlotNotification, ...] = ()
    attention_notifications: tuple[AttentionNotification, ...] = ()


class AppointmentsRunner:
    """Synchronous appointments poll core with injectable clocks, transport, and store."""

    def __init__(
        self,
        settings: AppointmentsSettings,
        *,
        path: str | Path | None = None,
        session_factory: sessionmaker[Session] | None = None,
        transport: Transport | None = None,
        client_factory: Callable[..., CmpClient] = CmpClient,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        sink: Callable[[dict], None] | None = None,
        generation: int | None = None,
    ) -> None:
        self.settings = settings
        self.path = path
        self._session_factory = session_factory
        self.transport = transport
        self.client_factory = client_factory
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.jitter = jitter
        self.sink = sink
        self.stopping = threading.Event()
        self._generation: int | None = generation

    @property
    def generation(self) -> int | None:
        return self._generation

    def _sessions(self) -> sessionmaker[Session]:
        if self._session_factory is not None:
            return self._session_factory
        _, sessions = db.init_db(self.path)
        return sessions

    def _trip_fence(self) -> None:
        self.stopping.set()
        log.warning("Appointments poller stopped reason=generation_fenced")

    def check(self) -> int:
        """Verify HMAC secret + owner, atomically clear disabled_at + bump generation, and bind this runner."""
        if not isinstance(self.settings.hmac_secret, bytes) or len(self.settings.hmac_secret) < 32:
            raise AppointmentsConfigError("Missing appointments persistence secret")
        sessions = self._sessions()
        with sessions() as session:
            if session.get(db.User, self.settings.owner) is None:
                raise AppointmentsStoreError("Unknown appointments owner")
        self._generation = AppointmentsStore(sessions, clock=self.clock).mark_enabled()
        return self._generation

    def next_delay(self) -> float:
        """Return seconds until next active poll (quiet-hours wait or jittered cadence)."""
        policy = self.settings.poll_policy
        now = self.clock()
        if not policy.is_active_window(now):
            return policy.seconds_until_active(now)
        return policy.next_poll_delay(jitter=self.jitter)

    def poll_once(self) -> PollCycleResult:
        """Run one bounded poll cycle across rotated watches within the active window."""
        if self.stopping.is_set():
            return PollCycleResult(active_window=True, next_delay_seconds=0.0)
        if self._generation is None:
            self._trip_fence()
            return PollCycleResult(active_window=True, next_delay_seconds=0.0)

        assert self.settings.hmac_secret is not None
        store = AppointmentsStore(self._sessions(), clock=self.clock)
        generation = self._generation
        if store.is_fenced(expected_generation=generation):
            self._trip_fence()
            return PollCycleResult(active_window=True, next_delay_seconds=0.0)

        policy = self.settings.poll_policy
        now = self.clock()
        if not policy.is_active_window(now):
            wait = policy.seconds_until_active(now)
            log.info("Appointments poll skipped reason=quiet_hours wait_seconds=%d", int(wait))
            return PollCycleResult(active_window=False, next_delay_seconds=wait)

        notifier = AppointmentNotifier(store, sink=self.sink)
        owner = self.settings.owner
        stop_check = self.stopping.is_set
        store.prune_removed_watches(
            owner,
            [watch.watch_id for watch in self.settings.watches],
            expected_generation=generation,
            stop_check=stop_check,
        )
        store.prune_expired(
            enabled=True,
            expected_generation=generation,
            stop_check=stop_check,
        )

        client = self.client_factory(
            transport=self.transport,
            base_url=self.settings.cmp_base_url,
            credentials=self.settings.cmp_credentials,
            policy=policy,
            monotonic=self.monotonic,
            wall_clock=self.clock,
            sleep=self.sleeper,
            jitter=self.jitter,
        )
        run = client.new_run()

        polled = 0
        skipped_cooldown = 0
        deferred_run_limit = 0
        rate_limited = 0
        errors = 0
        fenced = False
        notifications: list[SlotNotification] = []
        attention_notifications: list[AttentionNotification] = []

        watches = self.settings.watches
        count = len(watches)
        start_idx = store.get_poll_cursor(owner) % count
        next_cursor = start_idx

        def record_watch_failure(
            target_watch,
            *,
            error_code: str,
            retry_after: float | None = None,
            attention: str | None = None,
        ) -> bool:
            nonlocal errors, fenced
            failure = store.record_failure(
                owner,
                target_watch,
                error_code=error_code,
                policy=policy,
                retry_after=retry_after,
                attention=attention,
                expected_generation=generation,
                stop_check=stop_check,
            )
            if failure is None:
                fenced = True
                self._trip_fence()
                return False
            errors += 1
            log.warning(
                "Appointments watch poll failed code=%s failures=%d status=%s",
                failure.last_error_code,
                failure.consecutive_failures,
                failure.last_status,
            )
            att = notifier.notify_attention(owner, target_watch, failure)
            if att is not None:
                attention_notifications.append(att)
            return True

        for step in range(count):
            if self.stopping.is_set():
                fenced = True
                break
            watch_idx = (start_idx + step) % count
            watch = watches[watch_idx]
            state = store.get_watch_state(owner, watch.watch_id)
            if state is not None and state.cooldown_until is not None and now < state.cooldown_until:
                skipped_cooldown += 1
                next_cursor = (watch_idx + 1) % count
                continue

            query = CmpSchedulerQuery.from_criteria(watch)
            try:
                parsed = client.get_scheduler(query, run=run)
            except CmpRunLimitError as exc:
                if exc.attempted:
                    next_cursor = (watch_idx + 1) % count
                    if not record_watch_failure(
                        watch,
                        error_code=exc.cause_code,
                        retry_after=exc.retry_after,
                    ):
                        break
                    deferred_run_limit = count - step - 1
                else:
                    next_cursor = watch_idx
                    deferred_run_limit = count - step
                log.info("Appointments poll deferred reason=run_limit remaining=%d", deferred_run_limit)
                break
            except CmpClientError as exc:
                next_cursor = (watch_idx + 1) % count
                if not record_watch_failure(
                    watch,
                    error_code=exc.code,
                    retry_after=exc.retry_after,
                    attention=exc.attention,
                ):
                    break
                continue

            if self.stopping.is_set():
                fenced = True
                break
            watch_next = self.clock() + policy.next_poll_delay(jitter=self.jitter)
            reconcile = store.reconcile_watch(
                owner,
                watch,
                parsed.slots,
                hmac_key=self.settings.hmac_secret,
                policy=policy,
                next_poll_at=watch_next,
                expected_generation=generation,
                stop_check=stop_check,
            )
            if reconcile is None:
                fenced = True
                self._trip_fence()
                break
            polled += 1
            next_cursor = (watch_idx + 1) % count
            batch = notifier.notify_slots(
                owner,
                watch,
                reconcile,
                policy=policy,
                expected_generation=generation,
                stop_check=stop_check,
            )
            notifications.extend(batch.notifications)
            rate_limited += batch.rate_limited

        if not fenced:
            store.set_poll_cursor(
                owner,
                next_cursor,
                expected_generation=generation,
                stop_check=stop_check,
            )
        next_delay = self.next_delay()
        log.info(
            "Appointments poll cycle completed polled=%d skipped=%d deferred=%d notified=%d rate_limited=%d errors=%d",
            polled,
            skipped_cooldown,
            deferred_run_limit,
            len(notifications),
            rate_limited,
            errors,
        )
        return PollCycleResult(
            active_window=True,
            polled=polled,
            skipped_cooldown=skipped_cooldown,
            deferred_run_limit=deferred_run_limit,
            notified=len(notifications),
            rate_limited=rate_limited,
            attention_alerts=len(attention_notifications),
            errors=errors,
            next_delay_seconds=next_delay,
            notifications=tuple(notifications),
            attention_notifications=tuple(attention_notifications),
        )


class AppointmentsPollerService:
    """Asyncio supervisor running ``AppointmentsRunner`` off the event loop."""

    def __init__(
        self,
        runner: AppointmentsRunner,
        *,
        grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.runner = runner
        self.grace_seconds = grace_seconds
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hestia-appointments")
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done() and not self.runner.stopping.is_set()

    def wake(self) -> None:
        self._wake.set()

    async def _call(self, operation: Callable[[], PollCycleResult]) -> PollCycleResult:
        future = asyncio.get_running_loop().run_in_executor(self._pool, operation)
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        await asyncio.wait({future})
        return future.result()

    async def _loop(self) -> None:
        while not self.runner.stopping.is_set():
            self._wake.clear()
            try:
                result = await self._call(self.runner.poll_once)
                delay = max(0.0, result.next_delay_seconds)
            except Exception as exc:
                log.warning("Appointments poll loop failed error_type=%s", type(exc).__name__)
                delay = float(self.runner.settings.poll_policy.min_interval_seconds)
            if self.runner.stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self.runner.stopping.set()
        self._wake.set()
        if self._task is not None:
            await asyncio.wait({self._task}, timeout=self.grace_seconds)
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._pool.shutdown(wait=False)


def _resolve_secret(session_secret: Callable[[], bytes] | bytes) -> bytes:
    value = session_secret() if callable(session_secret) else session_secret
    return value if isinstance(value, bytes) else b""


def build_service(
    *,
    session_secret: Callable[[], bytes] | bytes = b"",
    path: str | Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    environ: Mapping[str, str] | None = None,
    secrets_dir: str | Path | None = None,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    sink: Callable[[dict], None] | None = None,
) -> AppointmentsPollerService | None:
    """Return a ready poller service, or None when flag is OFF or 0400 secrets/DB checks fail."""
    if not is_enabled(environ=environ):
        return None
    try:
        settings = load_runtime_settings(
            environ=environ,
            session_secret=_resolve_secret(session_secret),
            secrets_dir=secrets_dir,
        )
    except AppointmentsConfigError:
        log.warning("Appointments poller disabled reason=config_invalid")
        return None
    if settings is None:
        log.info("Appointments poller disabled reason=unconfigured")
        return None

    runner = AppointmentsRunner(
        settings,
        path=path,
        session_factory=session_factory,
        transport=transport,
        clock=clock,
        monotonic=monotonic,
        sleeper=sleeper,
        jitter=jitter,
        sink=sink,
    )
    try:
        runner.check()
    except (AppointmentsConfigError, AppointmentsStoreError):
        log.warning("Appointments poller disabled reason=prerequisites_unmet")
        return None
    except Exception as exc:
        log.warning("Appointments poller disabled reason=init_failed error_type=%s", type(exc).__name__)
        return None
    return AppointmentsPollerService(runner)


def _run_housekeeping_purge(
    *,
    path: str | Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.time,
    expected_generation: int | None = None,
) -> PurgeResult:
    return prune_expired_appointments(
        path=path,
        now=clock(),
        enabled=is_enabled(environ=environ),
        session_factory=session_factory,
        expected_generation=expected_generation,
    )


class ServiceSlot:
    """Set before startup so lifecycle hooks never mutate a frozen aiohttp application."""

    service: AppointmentsPollerService | None = None
    housekeeping_task: asyncio.Task[None] | None = None


def register(
    app: web.Application,
    key: web.AppKey[object] = APPOINTMENTS_KEY,
    *,
    session_secret: Callable[[], bytes] | bytes = b"",
    path: str | Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    environ: Mapping[str, str] | None = None,
    secrets_dir: str | Path | None = None,
    transport: Transport | None = None,
    clock: Callable[[], float] = time.time,
    housekeeping_interval: float = HOUSEKEEPING_INTERVAL_SECONDS,
    sink: Callable[[dict], None] | None = None,
) -> None:
    """Install always-on startup/daily purge housekeeping and flag-gated appointments polling."""
    slot = ServiceSlot()
    app[key] = slot

    async def lifecycle(_app: web.Application):
        service = await asyncio.to_thread(
            build_service,
            session_secret=session_secret,
            path=path,
            session_factory=session_factory,
            environ=environ,
            secrets_dir=secrets_dir,
            transport=transport,
            clock=clock,
            sink=sink,
        )
        bound_generation = None if service is None else service.runner.generation
        try:
            startup_purge = await asyncio.to_thread(
                _run_housekeeping_purge,
                path=path,
                session_factory=session_factory,
                environ=environ,
                clock=clock,
                expected_generation=bound_generation,
            )
            if bound_generation is None:
                bound_generation = startup_purge.generation
        except Exception as exc:
            log.warning("Appointments startup purge failed error_type=%s", type(exc).__name__)

        async def housekeeping() -> None:
            while True:
                await asyncio.sleep(housekeeping_interval)
                try:
                    sweep = await asyncio.to_thread(
                        _run_housekeeping_purge,
                        path=path,
                        session_factory=session_factory,
                        environ=environ,
                        clock=clock,
                        expected_generation=bound_generation,
                    )
                except Exception as exc:
                    log.warning("Appointments housekeeping purge failed error_type=%s", type(exc).__name__)
                    continue
                if sweep.fenced:
                    log.warning("Appointments housekeeping stopped reason=generation_fenced")
                    break

        housekeeping_task = asyncio.create_task(housekeeping())
        slot.service = service
        slot.housekeeping_task = housekeeping_task
        if service is not None:
            service.start()
            log.info("Appointments poller enabled watches=%d", len(service.runner.settings.watches))
        try:
            yield
        finally:
            slot.service = None
            slot.housekeeping_task = None
            housekeeping_task.cancel()
            await asyncio.gather(housekeeping_task, return_exceptions=True)
            if service is not None:
                await service.stop()

    app.cleanup_ctx.append(lifecycle)
