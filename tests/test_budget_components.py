"""Independent component publication, durable reservations and virtual freshness."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import delete, select

from hestia import db
from hestia.budget import normalize, runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.normalize import AccountFailure, AccountSnapshot, Snapshot
from hestia.budget.scheduler import BudgetScheduler
from hestia.budget.store import BudgetStore, BudgetStoreError, OPERATIONS, STALE_AFTER
from tests.test_budget_store import APP, configuration, prepare_selected, snapshot, transaction
from tests.test_budget_runtime import ACCOUNT_UID, SECOND_UID, FAKE_KEY, AdvancingTime, SyntheticBank


class ComponentCase(AdvancingTime, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "components.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin")])
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.config = configuration()
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        prepare_selected(self.store, self.config)

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def account(self, primary="synthetic-hash"):
        return next(row for row in self.rows(db.BudgetAccount) if row.identification_hash == primary)

    def components(self, primary="synthetic-hash"):
        account_id = self.account(primary).id
        fields = ("last_attempt", "last_success", "last_error", "failures", "next_attempt", "attempt_id", "lease_until",
                  "retry_at", "suspended")
        return {row.operation: {field: getattr(row, field) for field in fields}
                for row in self.rows(db.BudgetComponent) if row.account_id == account_id}

    def state(self):
        return next(row for row in self.store.status() if row["connection"] == "bank")

    def observation(self, *, primary="synthetic-hash", transactions=None, balances=None,
                    transactions_error=None, balances_error=None):
        return AccountSnapshot(primary, balances, transactions, balances_error, transactions_error)

    def batch(self, *observations, errors=()):
        return Snapshot(self.now + 31536000, tuple(observations), "2025-11-21", "2026-11-20", tuple(errors))

    def seed(self, *transactions):
        self.store.commit_snapshot("bank", snapshot(*transactions, valid_until=self.now + 31536000))

    def reserve(self, *operations, attempt_id="round"):
        for operation in operations:
            self.assertTrue(self.store.start_component_attempt("bank", "synthetic-hash", operation,
                                                               attempt_id=attempt_id))

    def acquire(self, bank, *, config=None):
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            return runtime.run_once(self.config if config is None else config, path=self.path,
                                    clock=lambda: self.now, client_factory=mock.Mock(return_value=bank),
                                    monotonic=self.monotonic, sleeper=self.sleep)


class ComponentPublicationTests(ComponentCase):
    def test_transactions_and_balances_publish_independently_across_restart(self):
        self.seed(transaction("seed"), transaction(None))
        seed_time = self.now
        old_balances = self.account().balances_json
        self.now += 60
        self.reserve(*OPERATIONS)
        tx = transaction("fresh", amount="7.5")
        self.store.commit_snapshot("bank", self.batch(self.observation(
            transactions=(tx, transaction(None, amount="9")), balances_error="network")), attempt_id="round")
        parts = self.components()
        self.assertEqual(parts["transactions"]["last_success"], self.now)
        self.assertEqual(parts["balances"]["last_success"], seed_time)
        self.assertEqual(parts["balances"]["last_error"], "network")
        self.assertEqual(parts["balances"]["failures"], 1)
        self.assertTrue(all(row["attempt_id"] is None for row in parts.values()))
        self.assertEqual(self.account().balances_json, old_balances)
        self.assertEqual(self.account().observed_at, seed_time)
        self.assertEqual(self.account().last_error, "balances_network")
        self.assertEqual(self.state()["last_error"], "partial_data")
        self.assertEqual(self.state()["last_success"], seed_time)
        quarantine = [row.payload_json for row in self.rows(db.BudgetUnresolved)]
        ledger = {row.entry_reference: row.payload_json for row in self.rows(db.BudgetTransaction)}
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.now += 60
        self.reserve(*OPERATIONS, attempt_id="next-round")
        self.store.commit_snapshot("bank", self.batch(self.observation(
            balances=(), transactions_error="rate_limit")), attempt_id="next-round")
        self.assertEqual(self.account().balances_json, "[]")
        self.assertEqual({row.entry_reference: row.payload_json for row in self.rows(db.BudgetTransaction)}, ledger)
        self.assertEqual([row.payload_json for row in self.rows(db.BudgetUnresolved)], quarantine)
        parts = self.components()
        self.assertEqual(parts["transactions"]["last_success"], self.now - 60)
        self.assertEqual(parts["transactions"]["last_error"], "rate_limit")
        self.assertEqual(parts["balances"]["last_success"], self.now)
        self.assertEqual(parts["balances"]["failures"], 0)
        self.assertIsNone(parts["balances"]["last_error"])
        self.assertEqual(self.account().observed_at, seed_time)
        self.assertEqual(self.account().last_error, "transactions_rate_limit")

    def test_skipped_component_is_untouched_and_retains_prior_error_in_account_summary(self):
        self.seed(transaction())
        self.now += 60
        self.store.commit_snapshot("bank", self.batch(self.observation(
            balances=(), transactions_error="network")))
        prior_tx = self.components()["transactions"]
        self.now += 60
        self.reserve("balances")
        self.store.commit_snapshot("bank", self.batch(self.observation(balances=())), attempt_id="round")
        self.assertEqual(self.components()["transactions"], prior_tx)
        self.assertEqual(self.account().last_error, "transactions_network")
        untouched_parts, untouched_account = self.components(), self.account()
        self.now += 60
        self.store.commit_snapshot("bank", self.batch(self.observation()), attempt_id="empty-round")
        self.assertEqual(self.components(), untouched_parts)
        self.assertEqual(self.account().last_attempt, untouched_account.last_attempt)
        self.assertEqual(self.account().last_error, untouched_account.last_error)
        self.assertEqual(self.account().observed_at, untouched_account.observed_at)

    def test_two_component_errors_are_sanitized_and_recovery_clears_only_successful_component(self):
        self.seed(transaction())
        self.now += 60
        self.reserve(*OPERATIONS)
        self.store.commit_snapshot("bank", self.batch(self.observation(
            transactions_error="SECRET arbitrary provider body", balances_error="provider_error")), attempt_id="round")
        self.assertEqual(self.account().last_error, "component_errors")
        self.assertEqual(self.components()["transactions"]["last_error"], "invalid_data")
        self.assertEqual(self.state()["last_error"], "account_errors")
        self.assertNotIn("SECRET", json.dumps(self.store.status()))
        self.now += 60
        self.reserve("transactions", attempt_id="recovery")
        self.store.commit_snapshot("bank", self.batch(self.observation(transactions=())), attempt_id="recovery")
        self.assertEqual(self.account().last_error, "balances_provider_error")
        self.assertEqual(self.components()["transactions"]["failures"], 0)
        self.assertEqual(self.components()["balances"]["failures"], 1)

    def test_ambiguous_result_rolls_back_an_already_staged_other_component(self):
        self.seed(transaction())
        self.now += 60
        self.reserve(*OPERATIONS)
        before = self.components()
        with self.assertRaisesRegex(BudgetStoreError, "Ambiguous component result"):
            self.store.commit_snapshot("bank", self.batch(self.observation(
                transactions=(transaction("must-not-publish"),), balances=(), balances_error="network")),
                attempt_id="round")
        self.assertEqual(self.components(), before)
        self.assertEqual([row.entry_reference for row in self.rows(db.BudgetTransaction)], ["ref-1"])
        self.assertEqual(self.state()["failures"], 0)

    def test_strict_result_requires_its_marker_and_cannot_steal_another_round(self):
        for observation in (self.observation(transactions=()), self.observation(transactions_error="network")):
            with self.subTest(observation=observation), self.assertRaisesRegex(BudgetStoreError, "reservation changed"):
                self.store.commit_snapshot("bank", self.batch(observation), attempt_id="missing-round")
        self.assertIsNone(self.components()["transactions"]["last_attempt"])
        self.reserve("transactions", attempt_id="original")
        before = self.components()
        for observation in (self.observation(transactions=()), self.observation(transactions_error="limit_exceeded")):
            with self.subTest(observation=observation), self.assertRaisesRegex(BudgetStoreError, "reservation changed"):
                self.store.commit_snapshot("bank", self.batch(observation), attempt_id="different")
        self.assertEqual(self.components(), before)
        with self.sessions.begin() as session:
            session.get(db.BudgetComponent, (self.account().id, "transactions")).lease_until = None
        with self.assertRaisesRegex(BudgetStoreError, "reservation changed"):
            self.store.commit_snapshot("bank", self.batch(self.observation(transactions_error="limit_exceeded")),
                                       attempt_id="different")

    def test_local_resource_error_retires_expired_orphan_without_discarding_healthy_sibling(self):
        self.seed(transaction("seed"))
        self.reserve("balances", attempt_id="interrupted-previous-round")
        old_balances = self.components()["balances"]
        self.now += 901
        self.reserve("transactions", attempt_id="new-round")
        self.store.commit_snapshot("bank", self.batch(self.observation(
            transactions=(transaction("healthy-new-observation"),), balances_error="limit_exceeded")),
            attempt_id="new-round")
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)},
                         {"seed", "healthy-new-observation"})
        parts = self.components()
        self.assertEqual(parts["transactions"]["last_success"], self.now)
        self.assertEqual(parts["balances"]["last_attempt"], old_balances["last_attempt"])
        self.assertEqual(parts["balances"]["last_success"], old_balances["last_success"])
        self.assertEqual(parts["balances"]["last_error"], "limit_exceeded")
        self.assertIsNone(parts["balances"]["attempt_id"])
        self.assertIsNone(parts["balances"]["lease_until"])
        self.assertEqual(self.state()["last_error"], "partial_data")

    def test_global_resource_skip_has_no_fake_http_attempt_and_other_global_failures_are_rejected(self):
        original_parts = self.components()
        self.store.commit_snapshot("bank", self.batch(errors=(AccountFailure("synthetic-hash", "limit_exceeded"),)),
                                   attempt_id="round-with-no-http")
        self.assertEqual(self.components(), original_parts)
        self.assertIsNone(self.account().last_attempt)
        self.assertEqual(self.account().last_error, "limit_exceeded")
        self.assertEqual(self.state()["last_error"], "account_errors")
        for attempt_id in (None, "strict-round"):
            with self.subTest(attempt_id=attempt_id), self.assertRaisesRegex(BudgetStoreError, "global acquisition failure"):
                self.store.commit_snapshot("bank", self.batch(errors=(AccountFailure("synthetic-hash", "network"),)),
                                           attempt_id=attempt_id)
        self.assertEqual(self.components(), original_parts)

    def test_local_budget_component_error_is_allowed_without_reserving_http(self):
        self.store.commit_snapshot("bank", self.batch(self.observation(
            transactions_error="limit_exceeded", balances_error="limit_exceeded")), attempt_id="no-http")
        for part in self.components().values():
            self.assertIsNone(part["last_attempt"])
            self.assertIsNone(part["last_success"])
            self.assertIsNone(part["attempt_id"])
            self.assertEqual(part["last_error"], "limit_exceeded")
            self.assertEqual(part["failures"], 1)
        self.assertIsNone(self.account().last_attempt)


class ComponentLeaseTests(ComponentCase):
    def test_live_lease_cannot_be_replaced_but_expired_lease_can_be_reclaimed(self):
        self.assertTrue(self.store.start_attempt("bank"))
        self.reserve("transactions", attempt_id="original")
        first = self.components()["transactions"]
        self.assertEqual(first["lease_until"], self.now + 900)
        self.assertEqual(first["next_attempt"], self.now + 43200)
        self.assertFalse(self.store.start_component_attempt("bank", "synthetic-hash", "transactions", attempt_id="other"))
        self.assertEqual(self.components()["transactions"], first)
        self.now += 900
        self.reserve("transactions", attempt_id="replacement")
        self.assertEqual(self.components()["transactions"]["attempt_id"], "replacement")
        with self.assertRaises(BudgetStoreError):
            self.store.commit_snapshot("bank", self.batch(self.observation(transactions=())), attempt_id="original")
        self.store.commit_snapshot("bank", self.batch(self.observation(transactions=())), attempt_id="replacement")
        self.assertEqual(self.components()["transactions"]["last_success"], self.now)
        self.assertIsNone(self.components()["transactions"]["attempt_id"])

    def test_component_reservation_validates_operation_connection_owner_and_selection(self):
        for operation, attempt_id in (("details", "round"), (None, "round"), ("transactions", None),
                                      ("transactions", ""), ("balances", 7)):
            with self.subTest(operation=operation, attempt_id=attempt_id), self.assertRaises(BudgetStoreError):
                self.store.start_component_attempt("bank", "synthetic-hash", operation, attempt_id=attempt_id)
        with self.assertRaises(BudgetStoreError):
            self.store.start_component_attempt("missing", "synthetic-hash", "transactions", attempt_id="round")
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = False
        with self.assertRaises(BudgetStoreError):
            self.store.start_component_attempt("bank", "synthetic-hash", "transactions", attempt_id="round")
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = True
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.store.start_component_attempt("bank", "synthetic-hash", "transactions", attempt_id="round")
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
        for primary in ("unselected", "synthetic-hash"):
            if primary == "synthetic-hash":
                self.store.set_account_selected("owner", self.account().id, False, actor="owner")
            with self.subTest(primary=primary), self.assertRaises(BudgetStoreError):
                self.store.start_component_attempt("bank", primary, "transactions", attempt_id="round")
        self.assertTrue(all(part["last_attempt"] is None for part in self.components().values()))

    def test_failure_closes_only_this_connections_current_round_markers(self):
        second = replace(self.config.connections[0], id="other-bank", owner="other", accounts=("other-hash",))
        self.config = replace(self.config, connections=(*self.config.connections, second))
        prepare_selected(self.store, self.config)
        self.reserve("transactions", attempt_id="old-round")
        self.reserve("balances", attempt_id="current-round")
        self.assertTrue(self.store.start_component_attempt("other-bank", "other-hash", "transactions",
                                                          attempt_id="current-round"))
        old_tx = self.components()["transactions"]
        other_tx = self.components("other-hash")["transactions"]
        self.store.record_failure("bank", "network", attempt_id="current-round")
        self.assertEqual(self.components()["transactions"], old_tx)
        self.assertEqual(self.components("other-hash")["transactions"], other_tx)
        balances = self.components()["balances"]
        self.assertIsNone(balances["attempt_id"])
        self.assertIsNone(balances["lease_until"])
        self.assertEqual(balances["last_error"], "network")
        self.assertEqual(balances["failures"], 1)
        self.store.record_failure("bank", "SECRET", attempt_id="unknown-round")
        self.store.record_failure("bank", "network")
        self.assertEqual(self.components()["transactions"], old_tx)
        self.assertEqual(self.components()["balances"], balances)


class ComponentStatusTests(ComponentCase):
    def test_virtual_components_show_never_deferred_and_blocked_without_creating_rows(self):
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetComponent))
        for component in self.state()["accounts"][0]["components"].values():
            self.assertEqual(component["state"], "never")
            self.assertTrue(component["stale"])
            self.assertIsNone(component["age_seconds"])
            self.assertIsNone(component["last_attempt"])
            self.assertEqual(component["failures"], 0)
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").next_attempt = self.now + 10
        self.assertEqual(self.state()["accounts"][0]["components"]["transactions"]["state"], "deferred")
        for target, field, value in ((db.BudgetAccount, "enabled", False), (db.BudgetAccount, "configured", False),
                                     (db.BudgetConnection, "enabled", False),
                                     (db.BudgetConnection, "valid_until", self.now),
                                     *((db.BudgetConnection, "last_error", error) for error in
                                       ("authorization", "invalid_session", "expired_session", "session_mismatch"))):
            key = self.account().id if target is db.BudgetAccount else "bank"
            with self.sessions.begin() as session:
                row = session.get(target, key)
                old = getattr(row, field)
                setattr(row, field, value)
            with self.subTest(field=field, value=value):
                self.assertTrue(all(p["state"] == "blocked" for p in self.state()["accounts"][0]["components"].values()))
            with self.sessions.begin() as session:
                setattr(session.get(target, key), field, old)
        self.assertEqual(self.rows(db.BudgetComponent), [])

    def test_component_states_have_independent_staleness_and_error_running_priority(self):
        self.seed()
        for operation in OPERATIONS:
            threshold = STALE_AFTER[operation]
            for age, expected in ((-10, "ok"), (0, "ok"), (threshold - 1, "ok"), (threshold, "ready")):
                with self.sessions.begin() as session:
                    session.get(db.BudgetComponent, (self.account().id, operation)).last_success = self.now - age
                part = self.state()["accounts"][0]["components"][operation]
                self.assertEqual(part["state"], expected)
                self.assertEqual(part["age_seconds"], max(0, age))
                self.assertEqual(part["stale"], age >= threshold)
                self.assertEqual(part["stale_after_seconds"], threshold)
        key = (self.account().id, "transactions")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetComponent, key)
            row.last_success, row.last_error, row.next_attempt = self.now, "network", self.now + 30
        part = self.state()["accounts"][0]["components"]["transactions"]
        self.assertEqual(part["state"], "error")
        self.assertFalse(part["stale"])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetComponent, key)
            row.attempt_id, row.lease_until = "interrupted-round", self.now + 10
        self.assertEqual(self.state()["accounts"][0]["components"]["transactions"]["state"], "running")
        for lease in (self.now, None):
            with self.sessions.begin() as session:
                session.get(db.BudgetComponent, key).lease_until = lease
            part = self.state()["accounts"][0]["components"]["transactions"]
            self.assertEqual((part["state"], part["last_error"]), ("error", "interrupted"))
            self.assertEqual(self.components()["transactions"]["attempt_id"], "interrupted-round")


class ComponentRuntimeTests(ComponentCase):
    def reserve(self, *operations, attempt_id="round"):
        scheduler = BudgetScheduler(self.store)
        for operation in operations:
            self.assertTrue(scheduler.reserve_component(self.config.connections[0], "synthetic-hash", operation,
                                                        attempt_id=attempt_id, valid_until=self.now + 31536000))

    def test_401_discards_all_staged_accounts_and_does_not_invent_unvisited_attempts(self):
        self.config = configuration(accounts=("synthetic-hash", "second-hash"))
        prepare_selected(self.store, self.config)
        self.store.commit_snapshot("bank", self.batch(
            self.observation(transactions=(transaction("first-seed"),), balances=()),
            self.observation(primary="second-hash", transactions=(transaction("second-seed"),), balances=())))
        old_rows = [(row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)]
        previous_success = self.now
        self.now += 43200
        started = self.now
        bank = SyntheticBank(self.config)
        get = bank.get
        def fail_second(path, query=None):
            if path == f"/accounts/{SECOND_UID}/transactions":
                bank.calls.append((path, query))
                raise BudgetAPIError(401, "authorization")
            return get(path, query)
        bank.get = fail_second
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual([(row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)], old_rows)
        self.assertEqual(self.state()["last_success"], previous_success)
        self.assertEqual(self.state()["last_error"], "authorization")
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}",
                         f"/accounts/{ACCOUNT_UID}/transactions", f"/accounts/{ACCOUNT_UID}/balances",
                         f"/accounts/{SECOND_UID}/transactions"])
        attempted_at = {("synthetic-hash", "transactions"): started,
                        ("synthetic-hash", "balances"): started + 1,
                        ("second-hash", "transactions"): started + 2}
        for primary, operations in (("synthetic-hash", OPERATIONS), ("second-hash", ("transactions",))):
            for operation in operations:
                part = self.components(primary)[operation]
                self.assertEqual(part["last_attempt"], attempted_at[(primary, operation)])
                self.assertEqual(part["last_success"], previous_success)
                self.assertEqual(part["last_error"], "authorization")
                self.assertIsNone(part["attempt_id"])
        unvisited = self.components("second-hash")["balances"]
        self.assertEqual(unvisited["last_attempt"], previous_success)
        self.assertIsNone(unvisited["last_error"])

    def _deny_after_session(self, *, disable_owner):
        bank = SyntheticBank(self.config)
        get = bank.get
        def change_access(path, query=None):
            result = get(path, query)
            if path == f"/sessions/{APP}":
                with self.sessions.begin() as session:
                    if disable_owner:
                        session.get(db.User, "owner").disabled = True
                    else:
                        session.get(db.BudgetAccount, self.account().id).enabled = False
            return result
        bank.get = change_access
        result = self.acquire(bank)
        expected_state = "failed" if disable_owner else "deferred"
        self.assertEqual(result["results"], [{"connection": "bank", "state": expected_state}])
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}"])
        self.assertTrue(all(part["last_attempt"] is None for part in self.components().values()))
        self.assertTrue(all(part["attempt_id"] is None for part in self.components().values()))
        if not disable_owner:
            self.assertEqual(self.state()["failures"], 0)
            self.assertIsNone(self.state()["last_error"])

    def test_component_callback_rechecks_owner_before_any_endpoint_http(self):
        self._deny_after_session(disable_owner=True)

    def test_component_callback_rechecks_selection_before_any_endpoint_http(self):
        self._deny_after_session(disable_owner=False)

    def test_live_component_reservations_skip_http_without_faking_success_or_failure(self):
        self.reserve(*OPERATIONS, attempt_id="other-worker")
        before = self.components()
        bank = SyntheticBank(self.config)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual(bank.calls, [])
        self.assertEqual(self.components(), before)
        self.assertEqual(self.state()["failures"], 0)
        self.assertIsNone(self.state()["last_success"])

    def test_one_live_component_skips_only_its_endpoint_and_the_other_can_publish(self):
        self.reserve("transactions", attempt_id="other-worker")
        transaction_state = self.components()["transactions"]
        bank = SyntheticBank(self.config)
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}", f"/accounts/{ACCOUNT_UID}/balances"])
        self.assertEqual(self.components()["transactions"], transaction_state)
        self.assertEqual(self.components()["balances"]["last_success"], self.now)
        self.assertIsNone(self.state()["last_success"])

    def test_resource_cap_marks_only_the_endpoints_that_really_attempted_http(self):
        self.config = configuration(accounts=("synthetic-hash", "second-hash"))
        prepare_selected(self.store, self.config)
        bank = SyntheticBank(self.config)
        started = self.now
        with mock.patch.object(normalize, "MAX_TRANSACTIONS", 1):
            result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}", f"/accounts/{ACCOUNT_UID}/transactions"])
        parts = self.components()
        self.assertEqual(parts["transactions"]["last_attempt"], started)
        self.assertEqual(parts["transactions"]["last_success"], self.now)
        self.assertIsNone(parts["balances"]["last_attempt"])
        self.assertEqual(parts["balances"]["last_error"], "limit_exceeded")
        self.assertIsNone(self.account("second-hash").last_attempt)
        self.assertTrue(all(part["last_attempt"] is None for part in self.components("second-hash").values()))

    def test_pre_http_validation_failure_does_not_create_endpoint_attempts(self):
        bank = SyntheticBank(self.config)
        get = bank.get
        def invalid_session(path, query=None):
            result = get(path, query)
            result["status"] = "EXPIRED"
            return result
        bank.get = invalid_session
        result = self.acquire(bank)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual(self.state()["last_error"], "invalid_session")
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}"])
        self.assertTrue(all(part["last_attempt"] is None for part in self.components().values()))

    def test_crash_leaves_durable_markers_then_restart_observes_lease_and_quota(self):
        bank = SyntheticBank(self.config)
        started = self.now
        get = bank.get
        def interrupt(path, query=None):
            if path.startswith("/accounts/"):
                operation = path.rsplit("/", 1)[1]
                part = self.components()[operation]
                self.assertIsNotNone(part["attempt_id"])
                self.assertEqual(part["last_attempt"], started if operation == "transactions" else started + 1)
                self.assertEqual(part["lease_until"], part["last_attempt"] + 900)
            if path.endswith("/balances"):
                raise SystemExit("synthetic interruption")
            return get(path, query)
        bank.get = interrupt
        with self.assertRaisesRegex(SystemExit, "synthetic interruption"):
            self.acquire(bank)
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        before = self.components()
        self.assertEqual(len({part["attempt_id"] for part in before.values()}), 1)
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.now += 901
        with mock.patch.object(runtime, "credentials") as credentials:
            result = runtime.run_once(self.config, path=self.path, clock=lambda: self.now,
                                      monotonic=self.monotonic, sleeper=self.sleep)
        credentials.assert_not_called()
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual(self.components(), before)
        self.assertTrue(all(part["last_error"] == "interrupted" for part in
                            self.state()["accounts"][0]["components"].values()))
        self.now += 43200
        result = self.acquire(SyntheticBank(self.config))
        self.assertEqual(result["results"], [{"connection": "bank", "state": "updated"}])
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.assertTrue(all(part["attempt_id"] is None for part in self.components().values()))
        self.assertTrue(all(part["last_success"] == self.now for part in self.components().values()))
