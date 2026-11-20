"""A control-plane pause fences staged data without a spurious bank penalty."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select

from hestia import db
from hestia.budget import runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.rate_limit import RateScope
from hestia.budget.store import BudgetStore
from tests.test_budget_runtime import AdvancingTime, FAKE_KEY, SyntheticBank
from tests.test_budget_store import APP, configuration, prepare_selected, snapshot, transaction


class BudgetPauseTests(AdvancingTime, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "pause.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add(db.User(username="owner", password_hash="unused", role="admin"))
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.config = configuration()
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        prepare_selected(self.store, self.config)
        self.account_id = self.store.accounts("owner")[0]["id"]

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def set_paused(self, paused):
        return self.store.set_connection_paused("owner", "bank", paused, actor="operator")

    def acquire(self, bank):
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)) as credentials:
            factory = mock.Mock(return_value=bank)
            result = runtime.run_once(self.config, path=self.path, clock=lambda: self.now,
                                      monotonic=self.monotonic, sleeper=self.sleep, client_factory=factory)
        return result, credentials, factory

    def seed(self):
        prior = snapshot(transaction("seed-only"), valid_until=self.now + 31536000)
        prior.accounts[0].balances = ({"name": "Prior", "balance_type": "CLBD",
                                       "balance_amount": {"amount": "777", "currency": "PLN"}},)
        self.store.commit_snapshot("bank", prior)
        self.seed_time = self.now
        self.now += 86400

    def pausing_bank(self, suffix, *, resume=False, failure=None):
        test = self

        class PausingBank(SyntheticBank):
            triggered = False

            def get(self, path, query=None):
                result = super().get(path, query=query)
                if path.endswith(suffix) and not self.triggered:
                    self.triggered = True
                    test.set_paused(True)
                    if resume:
                        test.set_paused(False)
                    if failure is not None:
                        raise failure
                return result

        return PausingBank(self.config)

    def assert_preserved_and_released(self, result, debit_count, *, paused=True, connection_floor=0):
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        connection = self.rows(db.BudgetConnection)[0]
        self.assertEqual(connection.paused, paused)
        self.assertEqual((connection.next_attempt, connection.failures, connection.last_error), (connection_floor, 0, None))
        account = self.rows(db.BudgetAccount)[0]
        self.assertEqual(account.observed_at, self.seed_time)
        self.assertEqual(json.loads(account.balances_json)[0]["balance_amount"]["amount"], "777")
        self.assertEqual([row.entry_reference for row in self.rows(db.BudgetTransaction)], ["seed-only"])
        debits = self.rows(db.BudgetAcquisition)
        self.assertEqual(len(debits), debit_count)
        for component in self.rows(db.BudgetComponent):
            self.assertEqual(component.last_success, self.seed_time)
            self.assertEqual(component.failures, 0)
            self.assertIsNone(component.last_error)
            self.assertIsNone(component.attempt_id)
            self.assertIsNone(component.lease_until)
            if component.operation in {debit.operation for debit in debits}:
                self.assertGreaterEqual(component.next_attempt, component.last_attempt + 43200)

    def assert_late_failure_is_fenced(self, error, *, connection_floor=0):
        bank = self.pausing_bank("/transactions", resume=True, failure=error)
        result, _, _ = self.acquire(bank)
        self.assert_preserved_and_released(result, 1, paused=False, connection_floor=connection_floor)
        self.assertFalse(any(path.endswith("/balances") for path, _ in bank.calls))

    def test_late_authorization_after_pause_resume_does_not_mark_new_generation_unauthorized(self):
        self.seed()
        self.assert_late_failure_is_fenced(BudgetAPIError(401, "authorization"))

    def test_late_network_error_after_pause_resume_does_not_add_global_failure_backoff(self):
        self.seed()
        self.assert_late_failure_is_fenced(BudgetAPIError(code="network", retryable=True))

    def test_late_component_rate_limit_after_pause_resume_keeps_server_deadline_and_suspension(self):
        self.seed()
        retry = self.now + 864000
        self.assert_late_failure_is_fenced(BudgetAPIError(429, "rate_limit", True,
                                                        provider_code="ASPSP_RATE_LIMIT_EXCEEDED",
                                                        retry_at=retry, rate_scope=RateScope.ACCOUNT_ENDPOINT))
        components = {row.operation: row for row in self.rows(db.BudgetComponent)}
        self.assertEqual(components["transactions"].retry_at, retry)
        self.assertEqual(components["transactions"].backoff_until, retry)
        self.assertEqual(components["transactions"].next_attempt, retry)
        self.assertTrue(components["transactions"].suspended)
        self.assertIsNone(components["balances"].retry_at)
        self.assertFalse(components["balances"].suspended)

    def test_late_shared_rate_limit_after_pause_resume_keeps_real_connection_floor(self):
        self.seed()
        retry = self.now + 86400
        self.assert_late_failure_is_fenced(BudgetAPIError(429, "rate_limit", True, retry_at=retry),
                                          connection_floor=retry)
        limits = [row for row in self.rows(db.BudgetRequestLimit) if row.scope == "connection"]
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0].retry_at, retry)
        self.assertEqual(limits[0].next_attempt, retry)

    def test_pause_resume_during_transactions_fences_old_round_without_fetching_balances(self):
        self.seed()
        version = self.store.acquisition_version("bank", "owner")
        bank = self.pausing_bank("/transactions", resume=True)
        result, _, _ = self.acquire(bank)
        self.assert_preserved_and_released(result, 1, paused=False)
        self.assertEqual(self.rows(db.BudgetConnection)[0].control_version, version + 2)
        self.assertFalse(any(path.endswith("/balances") for path, _ in bank.calls))
        self.assertEqual(len(self.rows(db.BudgetConnectionAudit)), 2)
        resumed, _, _ = self.acquire(bank)
        self.assertEqual(resumed["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual(sum(path.endswith("/transactions") for path, _ in bank.calls), 1)
        self.assertEqual(sum(path.endswith("/balances") for path, _ in bank.calls), 1)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)
        self.assertEqual([row.entry_reference for row in self.rows(db.BudgetTransaction)], ["seed-only"])

    def test_pause_resume_during_balances_keeps_both_debits_and_does_not_publish(self):
        self.seed()
        bank = self.pausing_bank("/balances", resume=True)
        result, _, _ = self.acquire(bank)
        self.assert_preserved_and_released(result, 2, paused=False)
        self.assertEqual(len(bank.calls), 3)
        resumed, credentials, factory = self.acquire(bank)
        self.assertEqual(resumed["results"], [{"connection": "bank", "state": "deferred"}])
        credentials.assert_not_called()
        factory.assert_not_called()
        self.assertEqual(len(bank.calls), 3)

    def test_final_commit_rechecks_generation_after_complete_snapshot(self):
        self.seed()
        fetch = runtime.fetch_snapshot

        def complete_then_pause_resume(*args, **kwargs):
            result = fetch(*args, **kwargs)
            self.set_paused(True)
            self.set_paused(False)
            return result

        with mock.patch.object(runtime, "fetch_snapshot", side_effect=complete_then_pause_resume):
            result, _, _ = self.acquire(SyntheticBank(self.config))
        self.assert_preserved_and_released(result, 2, paused=False)

    def test_pause_resume_before_first_http_stops_stale_round_without_debits(self):
        self.seed()

        def credentials(*args):
            self.set_paused(True)
            self.set_paused(False)
            return FAKE_KEY, APP

        bank = SyntheticBank(self.config)
        with mock.patch.object(runtime, "credentials", side_effect=credentials):
            result = runtime.run_once(self.config, path=self.path, clock=lambda: self.now,
                                      monotonic=self.monotonic, sleeper=self.sleep,
                                      client_factory=lambda *args: bank)
        self.assert_preserved_and_released(result, 0, paused=False)
        self.assertEqual(bank.calls, [])

    def test_pause_after_transactions_preserves_data_and_unfetched_balance_is_eligible_on_resume(self):
        self.seed()
        bank = self.pausing_bank("/transactions")
        result, _, _ = self.acquire(bank)
        self.assert_preserved_and_released(result, 1)
        self.assertFalse(any(path.endswith("/balances") for path, _ in bank.calls))
        self.set_paused(False)
        resumed, _, _ = self.acquire(bank)
        self.assertEqual(resumed["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual(sum(path.endswith("/transactions") for path, _ in bank.calls), 1)
        self.assertEqual(sum(path.endswith("/balances") for path, _ in bank.calls), 1)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)
        self.assertEqual(self.rows(db.BudgetConnection)[0].next_attempt, 0)
        self.assertEqual([row.entry_reference for row in self.rows(db.BudgetTransaction)], ["seed-only"])
        self.assertEqual(json.loads(self.rows(db.BudgetAccount)[0].balances_json)[0]["balance_amount"]["amount"], "-12.345")

    def test_pause_after_both_responses_releases_markers_and_keeps_both_consumed_debits(self):
        self.seed()
        result, _, _ = self.acquire(self.pausing_bank("/balances"))
        self.assert_preserved_and_released(result, 2)
        self.set_paused(False)
        result, credentials, factory = self.acquire(SyntheticBank(self.config))
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        credentials.assert_not_called()
        factory.assert_not_called()
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)

    def test_pause_survives_reconciliation_and_defers_before_credentials(self):
        self.set_paused(True)
        result, credentials, factory = self.acquire(SyntheticBank(self.config))
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        credentials.assert_not_called()
        factory.assert_not_called()
        self.assertTrue(self.rows(db.BudgetConnection)[0].paused)
        self.assertIsNone(self.rows(db.BudgetConnection)[0].last_attempt)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertTrue(self.rows(db.BudgetAccount)[0].enabled)
