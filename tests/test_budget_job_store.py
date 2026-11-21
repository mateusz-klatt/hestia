"""Durable refresh intents: idempotency, coalescing, owner scope, opt-in and retention (synthetic)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hestia import db
from hestia.budget import job_store
from hestia.budget.job_store import (BudgetConnectionPaused, BudgetIdempotencyConflict, BudgetJobStore,
                                     BudgetQueueFull, BudgetRefreshBusy)
from hestia.budget.store import BudgetControlConflict, BudgetNotFound, BudgetStoreError
from tests.test_budget_store import configuration, prepare_selected

KEY = "synthetic-refresh-key-0001"
OTHER_KEY = "synthetic-refresh-key-0002"


class JobStoreCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "jobs.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin")])
        self.now = 1_000_000.0
        self.store = BudgetJobStore(self.sessions, clock=lambda: self.now)
        first = configuration(accounts=("hash-a", "hash-b"))
        other = replace(first.connections[0], id="other-bank", owner="other", accounts=("hash-c",))
        self.config = replace(first, connections=(*first.connections, other))
        prepare_selected(self.store, self.config)
        self.accounts = {row.identification_hash: row.id for row in self.rows(db.BudgetAccount)}

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def connection(self, connection_id="bank"):
        with self.sessions() as session:
            return session.get(db.BudgetConnection, connection_id)

    def version(self, connection_id="bank"):
        return self.connection(connection_id).control_version

    def enqueue(self, key=KEY, owner="owner", connection_id="bank", **target):
        target.setdefault("expected_version", self.version(connection_id))
        return self.store.enqueue_refresh(owner, connection_id, idempotency_key=key, **target)

    def opt_in(self, connection_id="bank", owner="owner"):
        return self.store.set_connection_background(owner, connection_id, True, actor=owner, origin="web",
                                                    expected_version=self.version(connection_id))


class EnqueueTests(JobStoreCase):
    def test_new_job_replay_and_conflicting_key(self):
        job = self.enqueue()
        self.assertTrue(job["created"])
        self.assertEqual((job["status"], job["source"], job["account_id"], job["operation"]),
                         ("queued", "manual", None, None))
        self.assertEqual(set(job), {"id", "connection_id", "account_id", "operation", "source", "status", "error",
                                    "next_attempt", "created_at", "started_at", "finished_at", "created"})
        self.now += 5
        replay = self.enqueue(expected_version=99)
        self.assertEqual((replay["id"], replay["created"]), (job["id"], False))
        with self.assertRaises(BudgetIdempotencyConflict) as raised:
            self.enqueue(operation="balances")
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "budget_idempotency_conflict"))
        self.assertEqual(len(self.rows(db.BudgetJob)), 1)
        self.assertEqual(len(self.rows(db.BudgetJobRequest)), 1)

    def test_same_target_coalesces_and_records_second_key(self):
        first = self.enqueue(account_id=self.accounts["hash-a"], operation="transactions")
        second = self.enqueue(OTHER_KEY, account_id=self.accounts["hash-a"], operation="transactions")
        self.assertEqual((second["id"], second["created"]), (first["id"], False))
        self.assertEqual({row.idempotency_key for row in self.rows(db.BudgetJobRequest)}, {KEY, OTHER_KEY})
        self.assertEqual(self.enqueue(OTHER_KEY, account_id=self.accounts["hash-a"],
                                      operation="transactions")["id"], first["id"])

    def test_different_target_while_active_is_busy_and_records_nothing(self):
        self.enqueue()
        for target in ({"operation": "balances"}, {"account_id": self.accounts["hash-a"]}):
            with self.subTest(target=target), self.assertRaises(BudgetRefreshBusy) as raised:
                self.enqueue(OTHER_KEY, **target)
            self.assertEqual(raised.exception.code, "budget_refresh_in_progress")
        self.assertEqual(len(self.rows(db.BudgetJobRequest)), 1)
        with self.subTest("running job also blocks"):
            self.store.claim_next()
            with self.assertRaises(BudgetRefreshBusy):
                self.enqueue(OTHER_KEY, operation="balances")

    def test_versions_pause_and_validation(self):
        with self.assertRaises(BudgetControlConflict) as raised:
            self.enqueue(expected_version=self.version() + 1)
        self.assertEqual(raised.exception.current["id"], "bank")
        with self.assertRaises(BudgetControlConflict):
            self.enqueue(expected_version=None)
        for key in (None, 7, "short", "x" * 129, "bad key with spaces!!", "ä" * 20):
            with self.subTest(key=key), self.assertRaises(BudgetStoreError):
                self.store.enqueue_refresh("owner", "bank", expected_version=1, idempotency_key=key)
        with self.assertRaises(BudgetStoreError):
            self.enqueue(operation="payments")
        self.store.set_connection_paused("owner", "bank", True, actor="owner", origin="web",
                                         expected_version=self.version())
        with self.assertRaises(BudgetConnectionPaused) as paused:
            self.enqueue()
        self.assertEqual(paused.exception.code, "budget_connection_paused")
        self.assertEqual(self.rows(db.BudgetJob), [])

    def test_foreign_unknown_disabled_and_unselected_targets(self):
        for owner, connection_id in (("other", "bank"), ("owner", "other-bank"), ("owner", "missing"), ("owner", "")):
            with self.subTest(owner=owner, connection=connection_id), self.assertRaises(BudgetNotFound):
                self.store.enqueue_refresh(owner, connection_id, expected_version=1, idempotency_key=KEY)
        for account in (self.accounts["hash-c"], "missing-account"):
            with self.subTest(account=account), self.assertRaises(BudgetNotFound):
                self.enqueue(account_id=account)
        prepare_selected(self.store, self.config)
        with self.sessions.begin() as session:
            other_owned = db.BudgetConnection(id="second", owner="owner", application_id=self.config.application_id,
                                              bank_name="Synthetic Bank", country="PL", enabled=True,
                                              next_attempt=0, failures=0)
            session.add(other_owned)
        with self.assertRaises(BudgetNotFound):
            self.enqueue(connection_id="second", account_id=self.accounts["hash-a"])
        self.store.set_account_selected("owner", self.accounts["hash-b"], False, actor="owner")
        with self.assertRaisesRegex(BudgetStoreError, "not selected"):
            self.enqueue(account_id=self.accounts["hash-b"])
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "second").enabled = False
        with self.assertRaises(BudgetNotFound):
            self.enqueue(connection_id="second")
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.enqueue()

    def test_queue_bound_and_integrity_race_map_to_clean_refusals(self):
        with mock.patch.object(job_store, "MAX_QUEUED", 0), self.assertRaises(BudgetQueueFull) as full:
            self.enqueue()
        self.assertEqual((full.exception.status, full.exception.code), (503, "budget_refresh_unavailable"))
        original = BudgetJobStore._active_or_new

        def racing(session, *args):
            raise IntegrityError("synthetic", {}, Exception("unique"))
        with mock.patch.object(BudgetJobStore, "_active_or_new", staticmethod(racing)), \
                self.assertRaises(BudgetRefreshBusy):
            self.enqueue()
        self.assertIs(BudgetJobStore._active_or_new, original)

    def test_parallel_enqueue_creates_one_job_and_refuses_the_other_target_cleanly(self):
        barrier, results = threading.Barrier(2), []

        def worker(key, operation):
            barrier.wait()
            try:
                results.append(self.enqueue(key, operation=operation)["id"])
            except BudgetRefreshBusy as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=worker, args=(KEY, "transactions")),
                   threading.Thread(target=worker, args=(OTHER_KEY, "balances"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(len(self.rows(db.BudgetJob)), 1)
        self.assertEqual(sorted(result == "budget_refresh_in_progress" for result in results), [False, True])

    def test_partial_unique_index_rejects_a_second_active_job(self):
        self.enqueue()
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as session:
                session.add(db.BudgetJob(id="duplicate", owner="owner", connection_id="bank", source="manual",
                                         expected_version=1, status="running", created_at=self.now))


class BackgroundTests(JobStoreCase):
    def test_opt_in_is_versioned_audited_and_noop_safe(self):
        before = self.version()
        saved = self.opt_in()
        self.assertTrue(saved["changed"] and saved["background_enabled"])
        self.assertEqual(saved["control_version"], before + 1)
        repeat = self.store.set_connection_background("owner", "bank", True, actor="owner", origin="web",
                                                      expected_version=before)
        self.assertFalse(repeat["changed"])
        audit = self.rows(db.BudgetConnectionAudit)
        self.assertEqual([(row.field, row.old_value_json, row.new_value_json, row.origin) for row in audit],
                         [("background", "false", "true", "web")])
        with self.assertRaises(BudgetControlConflict):
            self.store.set_connection_background("owner", "bank", False, actor="owner", origin="web",
                                                 expected_version=before)
        with self.assertRaises(BudgetControlConflict):
            self.store.set_connection_background("owner", "bank", False, actor="owner", origin="web")
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaises(BudgetStoreError):
                self.store.set_connection_background("owner", "bank", value, actor="owner", origin="web",
                                                     expected_version=self.version())
        with self.assertRaises(BudgetNotFound):
            self.store.set_connection_background("other", "bank", False, actor="other", origin="web",
                                                 expected_version=self.version())
        with self.assertRaises(BudgetStoreError):
            self.store.set_connection_background("owner", "bank", False, actor="mallory", origin="web",
                                                 expected_version=self.version())
        operator = self.store.set_connection_background("owner", "bank", False, actor="operator:1000")
        self.assertFalse(operator["background_enabled"])

    def test_reconfiguration_never_restores_an_opt_out(self):
        self.opt_in()
        self.store.set_connection_background("owner", "bank", False, actor="owner", origin="web",
                                             expected_version=self.version())
        self.store.configure(self.config)
        self.assertFalse(self.connection().background_enabled)

    def test_background_eligibility_matrix(self):
        self.assertEqual(self.store.background_candidates(), [])
        self.assertIsNone(self.store.enqueue_background("bank"))
        self.opt_in()
        self.assertEqual(self.store.background_candidates(), ["bank"])
        job = self.store.enqueue_background("bank")
        self.assertEqual((job["source"], job["status"], job["account_id"]), ("poller", "queued", None))
        self.assertIsNone(self.store.enqueue_background("bank"), "an active job always wins")
        self.assertIsNone(self.store.enqueue_background("missing"))
        self.store.claim_next()
        self.store.finish(job["id"], "failed", "authorization")
        self.assertEqual(self.connection().attention, "authorization")
        self.assertEqual(self.store.background_candidates(), [])
        cases = {"paused": lambda s: setattr(s.get(db.BudgetConnection, "bank"), "paused", True),
                 "disabled": lambda s: setattr(s.get(db.BudgetConnection, "bank"), "enabled", False),
                 "owner": lambda s: setattr(s.get(db.User, "owner"), "role", "viewer"),
                 "unselected": lambda s: [setattr(row, "enabled", False) for row in s.scalars(select(db.BudgetAccount))]}
        for name, change in cases.items():
            with self.subTest(name):
                with self.sessions.begin() as session:
                    session.get(db.BudgetConnection, "bank").attention = None
                    change(session)
                self.assertIsNone(self.store.enqueue_background("bank"))
                with self.sessions.begin() as session:
                    connection = session.get(db.BudgetConnection, "bank")
                    connection.paused, connection.enabled = False, True
                    session.get(db.User, "owner").role = "admin"
                    for row in session.scalars(select(db.BudgetAccount)):
                        row.enabled = True
        with mock.patch.object(job_store, "MAX_QUEUED", 0):
            self.assertIsNone(self.store.enqueue_background("bank"))

        def racing(session, *args):
            raise IntegrityError("synthetic", {}, Exception("unique"))
        with mock.patch.object(BudgetJobStore, "_active_or_new", staticmethod(racing)):
            self.assertIsNone(self.store.enqueue_background("bank"))
        self.assertIsNotNone(self.store.enqueue_background("bank"))


class LifecycleTests(JobStoreCase):
    def test_claim_order_finish_attention_and_interruption(self):
        first = self.enqueue()
        self.now += 1
        second = self.enqueue(OTHER_KEY, owner="other", connection_id="other-bank")
        claimed = self.store.claim_next()
        self.assertEqual((claimed["id"], claimed["status"], claimed["owner"]), (first["id"], "running", "owner"))
        self.assertEqual(claimed["expected_version"], self.version())
        self.assertEqual(self.store.claim_next()["id"], second["id"])
        self.assertIsNone(self.store.claim_next())
        self.assertEqual(self.store.interrupt_orphans(), 2)
        self.assertEqual({row.status for row in self.rows(db.BudgetJob)}, {"interrupted"})
        self.assertIsNone(self.store.finish(first["id"], "updated"), "terminal jobs never change again")
        with self.assertRaises(BudgetStoreError):
            self.store.finish(first["id"], "queued")
        third = self.enqueue("synthetic-refresh-key-0003")
        self.store.claim_next()
        done = self.store.finish(third["id"], "failed", "expired_session", next_attempt=5.0)
        self.assertEqual((done["status"], done["error"], done["next_attempt"]), ("failed", "expired_session", 5.0))
        self.assertEqual(self.connection().attention, "expired_session")
        fourth = self.enqueue("synthetic-refresh-key-0004")
        self.store.claim_next()
        self.store.finish(fourth["id"], "deferred")
        self.assertEqual(self.connection().attention, "expired_session", "a deferral is not a verification")
        fifth = self.enqueue("synthetic-refresh-key-0005")
        self.store.claim_next()
        self.assertEqual(self.store.finish(fifth["id"], "partial", "PRIVATE provider text")["error"], "invalid_data")
        self.assertIsNone(self.connection().attention)

    def test_queued_deferral_retention_and_listing(self):
        job = self.enqueue()
        self.assertEqual(self.store.queued_before(self.now + 1), [job["id"]])
        self.assertEqual(self.store.queued_before(self.now), [])
        deferred = self.store.defer_queued(job["id"], "busy")
        self.assertEqual((deferred["status"], deferred["error"]), ("deferred", "busy"))
        self.assertIsNone(self.store.defer_queued(job["id"], "busy"))
        self.assertIsNone(self.store.defer_queued("missing", "busy"))
        for index in range(25):
            self.now += 1
            self.store.claim_next()
            created = self.enqueue(f"synthetic-refresh-key-{index:04d}x")
            self.store.claim_next()
            self.store.finish(created["id"], "updated")
        self.now += 1
        active = self.enqueue("synthetic-refresh-key-active")
        listed = self.store.jobs("owner")
        self.assertEqual(len(listed), job_store.LIST_LIMIT)
        self.assertEqual(listed[0]["id"], active["id"])
        self.assertNotIn("created", listed[0])
        self.assertEqual(self.store.jobs("owner", "bank"), listed)
        self.assertEqual(self.store.jobs("other"), [])
        with self.assertRaises(BudgetNotFound):
            self.store.jobs("other", "bank")
        self.now += job_store.RETENTION_SECONDS + 100
        self.assertEqual(self.store.prune(), 26)
        self.assertEqual([row.id for row in self.rows(db.BudgetJob)], [active["id"]])
        self.assertEqual([row.job_id for row in self.rows(db.BudgetJobRequest)], [active["id"]])

    def test_target_hash_and_next_attempt(self):
        whole = {"account_id": None, "owner": "owner", "connection_id": "bank"}
        self.assertIsNone(self.store.target_hash(whole))
        self.assertEqual(self.store.target_hash({**whole, "account_id": self.accounts["hash-a"]}), "hash-a")
        for job in ({**whole, "account_id": "missing"}, {**whole, "account_id": self.accounts["hash-c"]},
                    {**whole, "account_id": self.accounts["hash-a"], "connection_id": "other-bank"}):
            with self.subTest(job=job), self.assertRaises(BudgetNotFound):
                self.store.target_hash(job)
        self.assertEqual(self.store.next_attempt("bank", None), 0)
        with self.store.transaction() as session:
            first = self.store._component(session, self.accounts["hash-a"], "transactions")
            first.next_attempt, first.retry_at, first.backoff_until = 500.0, 900.0, 0
            second = self.store._component(session, self.accounts["hash-b"], "balances")
            second.next_attempt, second.backoff_until = 0, 700.0
        target = frozenset({("hash-a", "transactions"), ("hash-b", "balances")})
        self.assertEqual(self.store.next_attempt("bank", target), 700.0)
        self.assertEqual(self.store.next_attempt("bank", frozenset({("hash-a", "transactions")})), 900.0)
        self.assertIsNone(self.store.next_attempt("bank", frozenset({("hash-c", "balances")})))
        self.assertIsNone(self.store.next_attempt("missing", None))
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").next_attempt = 2000.0
        self.assertEqual(self.store.next_attempt("bank", target), 2000.0)

    def test_users_authoritative_marker(self):
        with self.sessions() as session:
            self.assertFalse(BudgetJobStore.users_authoritative(session))
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        with self.sessions() as session:
            self.assertTrue(BudgetJobStore.users_authoritative(session))


class MigrationTests(JobStoreCase):
    def test_0013_roundtrip_preserves_existing_rows_and_defaults_opt_in_off(self):
        self.opt_in()
        self.enqueue()
        db.reset_engine_cache()
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0012")
            tables = [name for name, in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
                      if name != "alembic_version"]
            self.assertFalse({"budget_jobs", "budget_job_requests"} & set(tables))
            columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(budget_connections)")}
            self.assertFalse({"background_enabled", "attention"} & columns)
            before = {name: [tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}"')] for name in tables}
            command.upgrade(db._alembic_config(connection), "0013")
            self.assertEqual(set(connection.exec_driver_sql(
                "SELECT background_enabled, attention FROM budget_connections").all()), {(0, None)})
            index = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='uq_budget_jobs_active'").scalar_one()
            self.assertIn("WHERE status IN ('queued', 'running')", index)
            self.assertEqual(connection.exec_driver_sql("SELECT COUNT(*) FROM budget_jobs").scalar_one(), 0)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            command.downgrade(db._alembic_config(connection), "0012")
            for name in tables:
                self.assertEqual([tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}"')],
                                 before[name])
            command.upgrade(db._alembic_config(connection), "head")


if __name__ == "__main__":
    unittest.main()
