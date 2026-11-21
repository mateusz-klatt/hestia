"""Executor/poller core on a synthetic bank and simulated clock; asyncio supervisor lifecycle."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from hestia import db
from hestia.budget import jobs, runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.config import BudgetConfigError
from hestia.budget.job_store import BudgetJobStore
from hestia.budget.jobs import BudgetJobRunner, BudgetJobService, BudgetJobsUnavailable
from hestia.budget.store import BudgetNotFound
from tests.test_budget_runtime import ACCOUNT_UID, FAKE_KEY, SECOND_UID, AdvancingTime, SyntheticBank
from tests.test_budget_store import APP, configuration, prepare_selected

KEY = "synthetic-runner-key-0001"
DAY = 86400


class RunnerCase(AdvancingTime, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "runner.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add(db.User(username="owner", password_hash="unused", role="admin"))
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        self.config = configuration(accounts=("hash-a", "hash-b"))
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.store = BudgetJobStore(self.sessions, clock=lambda: self.now)
        prepare_selected(self.store, self.config)
        self.accounts = {row.identification_hash: row.id for row in self.rows(db.BudgetAccount)}
        self.bank = SyntheticBank(self.config)
        self.factory = mock.Mock(return_value=self.bank)
        self.loaded = self.config
        self.runner = BudgetJobRunner(self.path, clock=lambda: self.now, monotonic=self.monotonic,
                                      sleeper=self.sleep, client_factory=self.factory,
                                      config_loader=lambda: self.loaded)
        patcher = mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP))
        self.credentials = patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def job(self, job_id):
        with self.sessions() as session:
            return session.get(db.BudgetJob, job_id)

    def connection(self):
        with self.sessions() as session:
            return session.get(db.BudgetConnection, "bank")

    def enqueue(self, key=KEY, **target):
        return self.runner.enqueue_refresh("owner", "bank", expected_version=self.connection().control_version,
                                           idempotency_key=key, **target)

    def paths(self):
        return [path for path, _ in self.bank.calls]

    def opt_in(self):
        self.store.set_connection_background("owner", "bank", True, actor="owner", origin="web",
                                             expected_version=self.connection().control_version)

    def debits(self):
        names = {value: key for key, value in self.accounts.items()}
        return sorted((names[row.account_id], row.operation) for row in self.rows(db.BudgetAcquisition))


class ManualJobTests(RunnerCase):
    def test_whole_connection_job_runs_each_selected_component_once(self):
        job = self.enqueue()
        done = self.runner.step()
        self.assertEqual((done["id"], done["status"], done["error"], done["source"]),
                         (job["id"], "updated", None, "manual"))
        self.assertGreater(done["next_attempt"], self.now)
        self.assertIsNotNone(done["started_at"])
        self.assertEqual(self.paths(), [f"/sessions/{APP}",
                                        f"/accounts/{ACCOUNT_UID}/transactions", f"/accounts/{ACCOUNT_UID}/balances",
                                        f"/accounts/{SECOND_UID}/transactions", f"/accounts/{SECOND_UID}/balances"])
        self.assertIsNone(self.runner.step())
        self.assertEqual(len(self.debits()), 4)
        self.factory.assert_called_once_with(APP, FAKE_KEY)

    def test_targets_reserve_only_requested_components_and_defer_without_http_when_not_due(self):
        single = self.enqueue(account_id=self.accounts["hash-a"], operation="balances")
        self.assertEqual(self.runner.step()["status"], "updated")
        self.assertEqual(self.paths(), [f"/sessions/{APP}", f"/accounts/{ACCOUNT_UID}/balances"])
        self.assertEqual(self.debits(), [("hash-a", "balances")])
        self.assertEqual(self.job(single["id"]).status, "updated")
        self.enqueue("synthetic-runner-key-0002", operation="transactions")
        self.assertEqual(self.runner.step()["status"], "updated")
        self.assertEqual(self.debits(), [("hash-a", "balances"), ("hash-a", "transactions"), ("hash-b", "transactions")])
        calls, reads = len(self.bank.calls), self.credentials.call_count
        whole = self.enqueue("synthetic-runner-key-0003", account_id=self.accounts["hash-a"])
        partial = self.runner.step()
        self.assertEqual((partial["id"], partial["status"]), (whole["id"], "deferred"))
        self.assertGreater(partial["next_attempt"], self.now)
        self.assertEqual((len(self.bank.calls), self.credentials.call_count), (calls, reads))
        self.enqueue("synthetic-runner-key-0004")
        remaining = self.runner.step()
        self.assertEqual((remaining["status"], remaining["error"]), ("partial", None),
                         "only hash-b balances was still due; cadence skips are not failures")
        self.assertEqual(self.paths()[-1], f"/accounts/{SECOND_UID}/balances")

    def test_component_failure_reports_partial_with_safe_code(self):
        self.bank.failures = {"hash-b"}
        self.enqueue()
        done = self.runner.step()
        self.assertEqual((done["status"], done["error"]), ("partial", "provider_error"))
        self.assertIsNone(self.connection().attention)

    def test_lock_contention_keeps_job_queued_then_defers_a_stale_one_without_debits(self):
        job = self.enqueue()
        with runtime.budget_lock(self.path):
            self.assertIsNone(self.runner.step())
            self.assertEqual(self.job(job["id"]).status, "queued")
            self.assertEqual(self.runner.poll(), 0)
            self.now += jobs.BUSY_DEFER_SECONDS + 1
            self.assertIsNone(self.runner.step())
        stale = self.job(job["id"])
        self.assertEqual((stale.status, stale.error), ("deferred", "busy"))
        self.assertEqual((self.bank.calls, self.debits()), ([], []))

    def test_crash_mid_round_is_reported_interrupted_and_never_replayed(self):
        get = self.bank.get

        def crash(path, query=None):
            if path.endswith("/balances"):
                raise SystemExit("simulated process death")
            return get(path, query)
        self.bank.get = crash
        job = self.enqueue()
        with self.assertRaises(SystemExit):
            self.runner.step()
        self.assertEqual(self.job(job["id"]).status, "running")
        self.bank.get = get
        calls = len(self.bank.calls)
        self.assertIsNone(self.runner.step())
        interrupted = self.job(job["id"])
        self.assertEqual((interrupted.status, interrupted.error), ("interrupted", "interrupted"))
        self.assertEqual(len(self.bank.calls), calls)
        self.assertEqual(self.debits(), [("hash-a", "balances"), ("hash-a", "transactions")])
        with self.sessions() as session:
            lease = session.get(db.BudgetComponent, (self.accounts["hash-a"], "balances")).lease_until
        self.assertGreater(lease, self.now, "the uncertain request keeps its lease")

    def test_shutdown_stops_at_a_request_boundary_and_leaves_other_intents_queued(self):
        get = self.bank.get

        def stop_after_session(path, query=None):
            result = get(path, query)
            self.runner.stopping.set()
            return result
        self.bank.get = stop_after_session
        job = self.enqueue()
        done = self.runner.step()
        self.assertEqual((done["id"], done["status"], done["error"]), (job["id"], "interrupted", "interrupted"))
        self.assertEqual(self.paths(), [f"/sessions/{APP}"])
        self.assertEqual(self.debits(), [])
        later = self.enqueue("synthetic-runner-key-0002")
        self.assertIsNone(self.runner.step())
        self.assertEqual(self.job(later["id"]).status, "queued")

    def test_configuration_selection_owner_and_version_changes_fail_closed(self):
        job = self.enqueue()
        self.loaded = replace(self.config, connections=())
        self.assertEqual((self.runner.step()["status"], self.job(job["id"]).error), ("deferred", "configuration"))
        self.assertEqual(self.connection().attention, "configuration")
        self.loaded = self.config
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").attention = None
        self.enqueue("synthetic-runner-key-0002", account_id=self.accounts["hash-a"])
        with mock.patch.object(BudgetJobStore, "target_hash", side_effect=BudgetNotFound()):
            self.assertEqual(self.runner.step()["error"], "account_selection")
        self.enqueue("synthetic-runner-key-0003")
        with self.sessions.begin() as session:
            session.get(db.User, "owner").role = "viewer"
        self.assertEqual(self.runner.step()["error"], "configuration")
        with self.sessions.begin() as session:
            session.get(db.User, "owner").role = "admin"
            session.get(db.BudgetConnection, "bank").attention = None
        self.enqueue("synthetic-runner-key-0004")
        for paused in (True, False):
            self.store.set_connection_paused("owner", "bank", paused, actor="owner", origin="web",
                                             expected_version=self.connection().control_version)
        changed = self.runner.step()
        self.assertEqual((changed["status"], changed["error"]), ("deferred", "control_changed"))
        self.assertEqual((self.bank.calls, self.credentials.call_count), ([], 0))

    def test_prerequisites_raise_before_any_work(self):
        self.loaded = None
        for operation in (self.runner.poll, self.runner.step, self.runner.check):
            with self.subTest(operation=operation.__name__), self.assertRaises(BudgetConfigError):
                operation()
        self.loaded = self.config
        self.runner.check()
        with self.sessions.begin() as session:
            session.delete(session.get(db.AppMeta, "users_authoritative"))
        with self.assertRaises(BudgetJobsUnavailable):
            self.runner.check()
        with self.assertRaises(BudgetJobsUnavailable):
            self.enqueue()


class PollerTests(RunnerCase):
    def test_opt_in_due_state_and_no_secret_or_http_reads_while_polling(self):
        self.assertEqual(self.runner.poll(), 0)
        self.opt_in()
        self.assertEqual(self.runner.poll(), 1)
        self.assertEqual(self.runner.poll(), 0, "one intent per connection")
        self.assertEqual((self.bank.calls, self.credentials.call_count), ([], 0))
        done = self.runner.step()
        self.assertEqual((done["source"], done["status"]), ("poller", "updated"))
        self.now += 3600
        self.assertEqual(self.runner.poll(), 0, "not due: no intent, no HTTP")
        self.assertEqual(self.credentials.call_count, 1)
        self.now += 43200
        self.assertEqual(self.runner.poll(), 1)
        self.runner.step()
        self.now += 30 * DAY
        self.assertEqual(self.runner.poll(), 1, "no catch-up burst after downtime")
        self.assertEqual(self.runner.poll(), 0)

    def test_refresh_coalesces_with_a_queued_poller_intent(self):
        self.opt_in()
        self.runner.poll()
        queued = self.rows(db.BudgetJob)[0]
        manual = self.enqueue()
        self.assertEqual((manual["id"], manual["created"], manual["source"]), (queued.id, False, "poller"))
        with self.assertRaises(Exception):
            self.enqueue("synthetic-runner-key-0002", operation="balances")
        self.runner.step()
        self.assertEqual(len(self.rows(db.BudgetJob)), 1)

    def test_authorization_failure_stops_unattended_work_until_an_owner_refresh_succeeds(self):
        get = self.bank.get

        def expired(path, query=None):
            raise BudgetAPIError(401, "authorization")
        self.bank.get = expired
        self.opt_in()
        self.runner.poll()
        failed = self.runner.step()
        self.assertEqual((failed["status"], failed["error"]), ("failed", "authorization"))
        self.assertEqual(self.connection().attention, "authorization")
        self.assertEqual(self.debits(), [])
        self.now += 7 * DAY
        self.assertEqual(self.runner.poll(), 0)
        self.bank.get = get
        self.enqueue()
        self.assertEqual(self.runner.step()["status"], "updated")
        self.assertIsNone(self.connection().attention)
        self.now += 43201
        self.assertEqual(self.runner.poll(), 1)

    def test_pause_and_opt_out_between_poll_and_execution(self):
        self.opt_in()
        for change in ("pause", "opt-out"):
            with self.subTest(change):
                self.assertEqual(self.runner.poll(), 1)
                version = self.connection().control_version
                if change == "pause":
                    self.store.set_connection_paused("owner", "bank", True, actor="owner", origin="web",
                                                     expected_version=version)
                else:
                    self.store.set_connection_background("owner", "bank", False, actor="owner", origin="web",
                                                         expected_version=version)
                self.assertEqual(self.runner.step()["error"], "control_changed")
                self.assertEqual(self.runner.poll(), 0)
                if change == "pause":
                    self.store.set_connection_paused("owner", "bank", False, actor="owner", origin="web",
                                                     expected_version=self.connection().control_version)
        self.assertEqual(self.bank.calls, [])

    def test_seven_simulated_days_keep_cadence_and_rolling_limits(self):
        self.opt_in()
        end = self.now + 7 * DAY
        while self.now < end:
            self.now += 1800
            self.runner.poll()
            while self.runner.step() is not None:
                pass
        starts = {}
        for row in self.rows(db.BudgetAcquisition):
            starts.setdefault((row.account_id, row.operation), []).append(row.started_at)
        self.assertEqual(len(starts), 4)
        for stamps in starts.values():
            stamps.sort()
            self.assertGreaterEqual(len(stamps), 13)
            self.assertTrue(all(later - earlier >= 43200 for earlier, later in zip(stamps, stamps[1:])))
            self.assertTrue(all(sum(stamp <= value < stamp + DAY for value in stamps) <= 2 for stamp in stamps))
        self.assertEqual({row.status for row in self.rows(db.BudgetJob)}, {"updated"})


class FakeRunner:
    def __init__(self):
        self.stopping = threading.Event()
        self.queue, self.polls, self.threads = [], 0, set()
        self.failure = None
        self.block = None

    def poll(self):
        self.polls += 1
        return 0

    def step(self):
        self.threads.add(threading.current_thread().name)
        if self.block is not None:
            started, release = self.block
            self.block = None
            started.set()
            release.wait(5)
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        return self.queue.pop(0) if self.queue else None


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_drains_queue_on_the_worker_thread_and_stop_refuses_work(self):
        runner = FakeRunner()
        service = BudgetJobService(runner, poll_seconds=3600)
        self.assertFalse(service.accepting)
        service.start()
        await until(lambda: runner.threads)
        self.assertTrue(service.accepting)
        runner.queue = [{"id": "one"}, {"id": "two"}]
        service.wake()
        await until(lambda: not runner.queue)
        self.assertEqual(runner.polls, 0)
        self.assertTrue(all(name.startswith("hestia-budget-jobs") for name in runner.threads))
        await service.stop()
        self.assertFalse(service.accepting)
        self.assertTrue(runner.stopping.is_set())

    async def test_stopping_a_service_that_never_started_is_safe(self):
        service = BudgetJobService(FakeRunner())
        await service.stop()
        self.assertFalse(service.accepting)

    async def test_poller_cadence_uses_bounded_jitter(self):
        runner = FakeRunner()
        jitter = mock.Mock(return_value=0.5)
        service = BudgetJobService(runner, poller=True, poll_seconds=0.02, jitter_seconds=0.02, jitter=jitter)
        service.start()
        await until(lambda: runner.polls >= 3)
        await service.stop()
        self.assertGreaterEqual(jitter.call_count, 2)

    async def test_round_failure_logs_only_the_exception_class_and_keeps_running(self):
        runner = FakeRunner()
        runner.failure = RuntimeError("PRIVATE provider URL and path")
        service = BudgetJobService(runner, poller=True, poll_seconds=0.02, jitter_seconds=0, jitter=lambda: 0)
        with self.assertLogs("hestia.budget.jobs", logging.WARNING) as logs:
            service.start()
            await until(lambda: runner.failure is None and runner.polls >= 2)
        await service.stop()
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("PRIVATE", "".join(logs.output))

    async def test_stop_waits_a_bounded_grace_without_cancelling_blocking_work(self):
        runner = FakeRunner()
        started, release = threading.Event(), threading.Event()
        runner.block = (started, release)
        service = BudgetJobService(runner, poll_seconds=3600, grace_seconds=0.05)
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        service.start()
        await until(started.is_set)
        begun = time.monotonic()
        await service.stop()
        self.assertLess(time.monotonic() - begun, 2)
        self.assertFalse(service._inflight.done(), "the blocking step is still running, not cancelled")
        runner.failure = RuntimeError("late failure")
        release.set()
        await until(service._inflight.done)
        await asyncio.sleep(0.01)
        self.assertEqual(unhandled, [])
        await service.stop()


class BuildAndRegisterTests(unittest.IsolatedAsyncioTestCase):
    def build(self, environ, *, auth=True, check=None):
        with mock.patch.dict(os.environ, environ, clear=False), \
                mock.patch.object(BudgetJobRunner, "check", side_effect=check) as checked:
            for name in (jobs.JOBS_FLAG, jobs.POLLER_FLAG, "HESTIA_PERSIST"):
                if name not in environ:
                    os.environ.pop(name, None)
            return jobs.build_service(auth_enabled=lambda: auth, path="/synthetic/unused.db"), checked

    def test_flags_and_prerequisites_matrix(self):
        sqlite = {"HESTIA_PERSIST": "sqlite"}
        self.assertIsNone(self.build(sqlite)[0])
        with self.assertLogs("hestia.budget.jobs", logging.ERROR):
            self.assertIsNone(self.build({**sqlite, jobs.POLLER_FLAG: "1"})[0])
        enabled = {**sqlite, jobs.JOBS_FLAG: "1"}
        for environ, auth in (({jobs.JOBS_FLAG: "1"}, True), (enabled, False)):
            with self.subTest(environ=environ, auth=auth), self.assertLogs("hestia.budget.jobs", logging.ERROR):
                service, checked = self.build(environ, auth=auth)
                self.assertIsNone(service)
                checked.assert_not_called()
        for error in (BudgetConfigError("PRIVATE path"), BudgetJobsUnavailable("users"), RuntimeError("PRIVATE db")):
            with self.subTest(error=type(error).__name__), self.assertLogs("hestia.budget.jobs", logging.ERROR) as logs:
                self.assertIsNone(self.build(enabled, check=error)[0])
            self.assertNotIn("PRIVATE", "".join(logs.output))
        service, _ = self.build(enabled)
        self.assertFalse(service.poller)
        service, _ = self.build({**enabled, jobs.POLLER_FLAG: "1"})
        self.assertTrue(service.poller)
        with mock.patch.dict(os.environ, {"HESTIA_DB": "/synthetic/default.db", **enabled}), \
                mock.patch.object(BudgetJobRunner, "check"):
            self.assertEqual(jobs.build_service(auth_enabled=lambda: True).runner.path, Path("/synthetic/default.db"))

    async def test_lifecycle_fills_and_clears_the_slot(self):
        key = aioweb.AppKey("synthetic_jobs", object)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                service = mock.Mock(poller=enabled)
                service.stop = mock.AsyncMock()
                app = aioweb.Application()
                jobs.register(app, key, auth_enabled=lambda: True)
                with mock.patch.object(jobs, "build_service", return_value=service if enabled else None):
                    client = TestClient(TestServer(app))
                    await client.start_server()
                    self.assertIs(app[key].service, service if enabled else None)
                    await client.close()
                self.assertIsNone(app[key].service)
                self.assertEqual(service.start.call_count, int(enabled))
                self.assertEqual(service.stop.await_count, int(enabled))


if __name__ == "__main__":
    unittest.main()
