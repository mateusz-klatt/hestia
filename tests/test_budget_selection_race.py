"""Mid-round account-selection changes fence acquisition as deferred/control_changed.

Selection is fenced explicitly without bumping connection.control_version, so
toggling an account never causes a 409 version conflict on the next UI
connection action or invalidates an open consent flow. Already-consumed debits
and floors stay durable; bogus snapshots without a real selection change still
fail closed as invalid_data.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select

from hestia import db
from hestia.budget import runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.consent_store import BudgetConsentStore
from hestia.budget.job_store import BudgetJobStore
from hestia.budget.jobs import BudgetJobRunner
from hestia.budget.rate_limit import RateScope
from hestia.budget.scheduler import BudgetScheduler
from hestia.budget.store import BudgetPausedError, BudgetStoreError
from tests.test_budget_runtime import ACCOUNT_UID, AdvancingTime, FAKE_KEY, SECOND_UID, SyntheticBank, reserved_snapshots
from tests.test_budget_store import APP, configuration, snapshot, transaction


def _hash(label):
    return hashlib.sha256(label.encode("ascii")).hexdigest()


class BudgetSelectionRaceTests(AdvancingTime, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "selection-race.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add(db.User(username="owner", password_hash="unused", role="admin"))
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.config = configuration(accounts=("synthetic-hash", "second"))
        self.store = BudgetJobStore(self.sessions, clock=lambda: self.now)
        self.store.configure(self.config)
        self.accounts = {row.identification_hash: row.id for row in self.rows(db.BudgetAccount)}
        self.store.set_account_selected("owner", self.accounts["synthetic-hash"], True, actor="operator")

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def select_account(self, primary, selected, *, origin="web"):
        current = next(row for row in self.store.accounts("owner") if row["id"] == self.accounts[primary])
        return self.store.set_account_selected(
            "owner", self.accounts[primary], selected,
            actor="owner" if origin == "web" else "operator",
            origin=origin,
            expected_version=current["control_version"],
        )

    def seed_first(self):
        prior = snapshot(transaction("seed-only"), valid_until=self.now + 31536000)
        prior.accounts[0].balances = ({"name": "Prior", "balance_type": "CLBD",
                                       "balance_amount": {"amount": "777", "currency": "PLN"}},)
        self.store.commit_snapshot("bank", prior)
        self.seed_time = self.now
        self.now += 86400

    def runner(self, bank):
        return BudgetJobRunner(
            self.path,
            clock=lambda: self.now,
            monotonic=self.monotonic,
            sleeper=self.sleep,
            client_factory=lambda *args: bank,
            config_loader=lambda: self.config,
        )

    def run_job(self, bank, key="selection-race-key-0001"):
        version = self.store.status(owner="owner")[0]["control_version"]
        self.store.enqueue_refresh("owner", "bank", expected_version=version, idempotency_key=key)
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            return self.runner(bank).step()

    def mutating_bank(self, suffix, mutation, *, failure=None):
        class MutatingBank(SyntheticBank):
            triggered = False

            def get(self, path, query=None):
                result = super().get(path, query=query)
                if path.endswith(suffix) and not self.triggered:
                    self.triggered = True
                    mutation()
                    if failure is not None:
                        raise failure
                return result

        return MutatingBank(self.config)

    def assert_clean_deferral(self, job, debit_count, *, connection_floor=0):
        self.assertEqual((job["status"], job["error"]), ("deferred", "control_changed"))
        connection = self.rows(db.BudgetConnection)[0]
        self.assertEqual((connection.next_attempt, connection.failures, connection.last_error, connection.attention),
                         (connection_floor, 0, None, None))
        first = next(row for row in self.rows(db.BudgetAccount) if row.identification_hash == "synthetic-hash")
        self.assertEqual(first.observed_at, self.seed_time)
        self.assertEqual(json.loads(first.balances_json)[0]["balance_amount"]["amount"], "777")
        self.assertEqual([row.entry_reference for row in self.rows(db.BudgetTransaction)], ["seed-only"])
        debits = self.rows(db.BudgetAcquisition)
        self.assertEqual(len(debits), debit_count)
        debited_keys = {(d.account_id, d.operation) for d in debits}
        for component in self.rows(db.BudgetComponent):
            self.assertEqual(component.failures, 0)
            self.assertIsNone(component.last_error)
            self.assertIsNone(component.attempt_id)
            self.assertIsNone(component.lease_until)
            if (component.account_id, component.operation) in debited_keys:
                self.assertGreaterEqual(component.next_attempt, component.last_attempt + 43200)

    def test_adding_account_during_transactions_defers_with_control_changed_and_subsequent_job_succeeds(self):
        self.seed_first()
        before_version = self.rows(db.BudgetConnection)[0].control_version
        bank = self.mutating_bank("/transactions", lambda: self.select_account("second", True))
        job = self.run_job(bank, key="selection-race-key-0001")
        self.assert_clean_deferral(job, debit_count=1)
        self.assertEqual(self.rows(db.BudgetConnection)[0].control_version, before_version)
        self.assertFalse(any(path.endswith("/balances") for path, _ in bank.calls))

        # A follow-up refresh runs on the new 2-account selection without refunding the prior debit.
        next_bank = SyntheticBank(self.config)
        resumed = self.run_job(next_bank, key="selection-race-key-0002")
        self.assertEqual((resumed["status"], resumed["error"]), ("partial", None))
        paths = [path for path, _ in next_bank.calls]
        self.assertNotIn(f"/accounts/{ACCOUNT_UID}/transactions", paths)
        self.assertIn(f"/accounts/{ACCOUNT_UID}/balances", paths)
        self.assertIn(f"/accounts/{SECOND_UID}/transactions", paths)
        self.assertIn(f"/accounts/{SECOND_UID}/balances", paths)
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 4)
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)}, {"seed-only", "second-ref"})

    def test_deselecting_account_during_balances_defers_and_keeps_both_debits(self):
        self.seed_first()
        bank = self.mutating_bank("/balances", lambda: self.select_account("synthetic-hash", False))
        job = self.run_job(bank, key="selection-race-key-0003")
        self.assert_clean_deferral(job, debit_count=2)
        self.assertEqual(len(bank.calls), 3)

    def test_selection_change_during_session_get_stops_before_any_component_debit(self):
        self.seed_first()
        bank = self.mutating_bank(f"/sessions/{APP}", lambda: self.select_account("second", True))
        job = self.run_job(bank, key="selection-race-key-0004")
        self.assert_clean_deferral(job, debit_count=0)
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}"])

    def test_selection_change_after_fetch_snapshot_before_commit_defers_cleanly(self):
        self.seed_first()
        original_fetch = runtime.fetch_snapshot

        def fetch_then_select(*args, **kwargs):
            result = original_fetch(*args, **kwargs)
            self.select_account("second", True)
            return result

        bank = SyntheticBank(self.config)
        with mock.patch.object(runtime, "fetch_snapshot", side_effect=fetch_then_select):
            job = self.run_job(bank, key="selection-race-key-0005")
        self.assert_clean_deferral(job, debit_count=2)
        self.assertEqual(len(bank.calls), 3)

    def test_selection_change_after_fetch_wins_over_expired_snapshot_at_commit(self):
        self.seed_first()
        original_fetch = runtime.fetch_snapshot

        def fetch_then_select_and_expire(*args, **kwargs):
            result = original_fetch(*args, **kwargs)
            self.select_account("second", True)
            self.now = result.valid_until
            return result

        bank = SyntheticBank(self.config)
        with mock.patch.object(runtime, "fetch_snapshot", side_effect=fetch_then_select_and_expire):
            job = self.run_job(bank, key="selection-race-key-0010")
        self.assert_clean_deferral(job, debit_count=2)
        self.assertEqual(len(bank.calls), 3)

    def test_aba_selection_toggle_returning_to_same_set_commits_cleanly(self):
        """Set-based fencing intentionally lets A -> B -> A commit the unchanged final selection."""
        self.seed_first()

        def toggle_on_then_off():
            self.select_account("second", True)
            self.select_account("second", False)

        bank = self.mutating_bank("/transactions", toggle_on_then_off)
        job = self.run_job(bank, key="selection-race-key-0011")
        self.assertEqual((job["status"], job["error"]), ("updated", None))
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)}, {"seed-only", "ref-1"})

    def test_deselecting_all_accounts_between_can_start_and_selected_hashes_defers_before_credentials(self):
        self.seed_first()
        scheduler = BudgetScheduler(self.store, application_id=APP)
        original_can_start = scheduler.can_start

        def can_start_then_deselect(*args, **kwargs):
            allowed = original_can_start(*args, **kwargs)
            self.select_account("synthetic-hash", False)
            return allowed

        bank = SyntheticBank(self.config)
        with mock.patch.object(scheduler, "can_start", side_effect=can_start_then_deselect), \
             mock.patch.object(runtime, "credentials") as credentials:
            state, error = runtime.acquire(
                self.config, self.config.connections[0], self.store, scheduler,
                lambda *args: bank, lambda: self.now, self.monotonic, self.sleep,
            )
        self.assertEqual((state, error), ("deferred", "control_changed"))
        credentials.assert_not_called()
        self.assertEqual(bank.calls, [])
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.rows(db.BudgetConnection)[0].failures, 0)

    def test_toctou_selection_change_inside_reserve_component_and_request_wait_fences_atomically(self):
        scheduler = BudgetScheduler(self.store, application_id=APP)
        connection = self.config.connections[0]
        expected = frozenset({"synthetic-hash"})
        self.select_account("second", True)
        with self.assertRaisesRegex(BudgetPausedError, "selection changed"):
            scheduler.reserve_component(
                connection, "synthetic-hash", "transactions",
                attempt_id="round-1", valid_until=self.now + 31536000, expected_selection=expected,
            )
        with self.assertRaisesRegex(BudgetPausedError, "selection changed"):
            scheduler.request_wait(connection, expected_selection=expected)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])

        # Also fence when the targeted account itself is deselected right before reservation.
        self.select_account("synthetic-hash", False)
        self.select_account("second", False)
        with self.assertRaisesRegex(BudgetPausedError, "selection changed"):
            scheduler.reserve_component(
                connection, "synthetic-hash", "transactions",
                attempt_id="round-2", valid_until=self.now + 31536000, expected_selection=expected,
            )
        with self.assertRaisesRegex(BudgetPausedError, "selection changed"):
            scheduler.request_wait(connection, expected_selection=expected)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.rows(db.BudgetRequestLimit), [])

    def test_late_provider_errors_after_selection_change_are_fenced_while_preserving_real_rate_limit(self):
        self.seed_first()
        bank_401 = self.mutating_bank(
            "/transactions", lambda: self.select_account("second", True),
            failure=BudgetAPIError(401, "authorization"),
        )
        job_401 = self.run_job(bank_401, key="selection-race-key-0006")
        self.assert_clean_deferral(job_401, debit_count=1)
        self.assertFalse(any(path.endswith("/balances") for path, _ in bank_401.calls))

        # Advance 12h so synthetic-hash/transactions is due again, then trigger a 429 + selection toggle.
        self.now += 43200
        retry = self.now + 864000
        bank_429 = self.mutating_bank(
            "/transactions", lambda: self.select_account("second", False),
            failure=BudgetAPIError(
                429, "rate_limit", True,
                provider_code="ASPSP_RATE_LIMIT_EXCEEDED",
                retry_at=retry, rate_scope=RateScope.ACCOUNT_ENDPOINT,
            ),
        )
        job_429 = self.run_job(bank_429, key="selection-race-key-0007")
        self.assert_clean_deferral(job_429, debit_count=2)
        first_id = self.accounts["synthetic-hash"]
        components = {row.operation: row for row in self.rows(db.BudgetComponent) if row.account_id == first_id}
        self.assertEqual(components["transactions"].retry_at, retry)
        self.assertTrue(components["transactions"].suspended)
        self.assertIsNone(components["balances"].retry_at)
        self.assertFalse(components["balances"].suspended)

    def test_connection_control_version_and_open_consent_flow_survive_account_selection_toggle(self):
        consent_store = BudgetConsentStore(self.sessions, clock=lambda: self.now)
        version = self.store.status(owner="owner")[0]["control_version"]
        flow = consent_store.create_flow(
            "owner", "bank", intent="renew", expected_version=version,
            state_hash=_hash("state"), nonce_hash=_hash("nonce"), auth_fingerprint=_hash("session"),
        )
        # Toggle account selection via web path (simulating UI checkbox click).
        self.select_account("second", True, origin="web")
        # Connection control_version stays unchanged: next UI refresh with the same version succeeds without 409.
        queued = self.store.enqueue_refresh(
            "owner", "bank", expected_version=version, idempotency_key="selection-race-key-0008",
        )
        self.assertEqual(queued["status"], "queued")
        # Open consent flow is not invalidated by consent_store._fence().
        with self.sessions() as session:
            flow_row = session.get(db.BudgetConsentFlow, flow["id"])
            connection_row = session.get(db.BudgetConnection, "bank")
            consent_store._fence(flow_row, connection_row)

    def test_confirm_account_kind_during_acquisition_does_not_abort_round(self):
        self.seed_first()

        def confirm_kind():
            current = next(row for row in self.store.accounts("owner") if row["id"] == self.accounts["synthetic-hash"])
            self.store.confirm_account_kind(
                "owner", self.accounts["synthetic-hash"], "current",
                actor="owner", origin="web", expected_version=current["control_version"],
            )

        bank = self.mutating_bank("/transactions", confirm_kind)
        job = self.run_job(bank, key="selection-race-key-0009")
        self.assertEqual((job["status"], job["error"]), ("updated", None))
        self.assertEqual(len(self.rows(db.BudgetAcquisition)), 2)

    def test_mismatched_snapshot_without_selection_change_still_fails_with_invalid_data(self):
        self.seed_first()
        bogus = snapshot(transaction("rogue"), account_hash="not-selected", valid_until=self.now + 31536000)
        scheduler = BudgetScheduler(self.store, application_id=APP)
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)), \
             mock.patch.object(runtime, "fetch_snapshot", side_effect=reserved_snapshots(bogus)):
            state, error = runtime.acquire(
                self.config, self.config.connections[0], self.store, scheduler,
                mock.Mock(), lambda: self.now, self.monotonic, self.sleep,
            )
        self.assertEqual((state, error), ("failed", "invalid_data"))
        self.assertEqual(self.rows(db.BudgetConnection)[0].failures, 1)
        self.assertEqual(self.rows(db.BudgetConnection)[0].last_error, "invalid_data")

        # Direct commit_snapshot without expected_selection keeps raising non-paused BudgetStoreError on mismatch.
        with self.assertRaises(BudgetStoreError) as raised:
            self.store.commit_snapshot("bank", bogus)
        self.assertNotIsInstance(raised.exception, BudgetPausedError)


if __name__ == "__main__":
    unittest.main()
