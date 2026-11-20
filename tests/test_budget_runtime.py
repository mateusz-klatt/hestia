"""Offline acquisition, durable scheduling, locks and redacted CLI boundaries."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from sqlalchemy import select

from hestia import db
from hestia.budget import __main__ as cli
from hestia.budget import runtime
from hestia.budget.client import BudgetAPIError
from hestia.budget.config import BudgetConfigError
from hestia.budget.normalize import BudgetDataError
from hestia.budget.store import BudgetStore, BudgetStoreError
from tests.test_budget_store import APP, configuration, snapshot, transaction


ACCOUNT_UID = "00000000-0000-4000-8000-000000000002"
UNSELECTED_UID = "00000000-0000-4000-8000-000000000003"
SECOND_UID = "00000000-0000-4000-8000-000000000004"
FAKE_KEY = b"synthetic key; never sent to a bank"


class SyntheticBank:
    """Small API fixture; real normalization and SQLite commits remain in use."""

    def __init__(self, config, *, failures=()):
        self.config = config
        self.calls = []
        self.failures = set(failures)
        self.accounts = dict(zip((ACCOUNT_UID, SECOND_UID), config.connections[0].accounts))

    def get(self, path, query=None):
        self.calls.append((path, query))
        if path == f"/sessions/{APP}":
            connection = self.config.connections[0]
            return {"status": "AUTHORIZED", "aspsp": {"name": connection.bank_name, "country": connection.country},
                    "access": {"valid_until": "2027-11-20T00:00:00+00:00"},
                    "accounts_data": [{"uid": uid, "identification_hash": primary}
                                      for uid, primary in self.accounts.items()]
                                     + [{"uid": UNSELECTED_UID, "identification_hash": "unselected-private-account"}]}
        for uid, primary in self.accounts.items():
            if path == f"/accounts/{uid}/balances":
                return {"balances": [{"name": "Booked", "balance_type": "CLBD",
                                      "balance_amount": {"amount": "-12.34500", "currency": "PLN"}}]}
            if path == f"/accounts/{uid}/transactions":
                if primary in self.failures:
                    raise BudgetAPIError(400, "provider_error")
                tx = transaction("ref-1" if uid == ACCOUNT_UID else "second-ref",
                                 remittance_information=["SECRET synthetic financial description"])
                money = {"amount": tx.pop("amount"), "currency": tx.pop("currency")}
                return {"transactions": [{**tx, "transaction_amount": money}]}
        raise AssertionError("Unexpected synthetic API route")


class BudgetRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "synthetic.db"
        _, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin")])
        self.config = configuration()
        self.factory = mock.Mock()
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def acquisition(self, config=None, **kwargs):
        return runtime.run_once(self.config if config is None else config,
                                clock=lambda: self.now, **kwargs)

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def cli_acquisition(self, config, *, path):
        return self.acquisition(config, path=path, client_factory=self.factory)

    def test_synthetic_api_normalization_commit_and_repeat_are_durable_and_selected(self):
        bank = SyntheticBank(self.config)
        factory = mock.Mock(return_value=bank)
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)) as credentials:
            result = self.acquisition(path=self.path, client_factory=factory)
            self.assertEqual(result["results"], [{"connection": "bank", "state": "updated"}])
            repeated = self.acquisition(path=self.path, client_factory=factory)
        credentials.assert_called_once_with(self.config, self.config.connections[0])
        factory.assert_called_once_with(APP, FAKE_KEY)
        self.assertEqual(repeated["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual([path for path, _ in bank.calls], [f"/sessions/{APP}",
                         f"/accounts/{ACCOUNT_UID}/transactions", f"/accounts/{ACCOUNT_UID}/balances"])
        self.assertEqual(bank.calls[1][1], {"date_from": "2025-11-21", "date_to": "2026-11-20"})
        account = self.rows(db.BudgetAccount)[0]
        self.assertEqual(json.loads(account.balances_json)[0]["balance_amount"]["amount"], "-12.345")
        self.assertEqual(json.loads(self.rows(db.BudgetTransaction)[0].payload_json)["amount"],
                         "12345678901234567890.12345")
        self.assertEqual(result["status"][0]["next_attempt"], self.now + 43200)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("synthetic-hash", json.dumps(result))

    def test_crash_reserves_before_http_and_restart_defers_without_reading_credentials(self):
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)), \
             self.assertRaisesRegex(SystemExit, "simulated interruption"):
            self.acquisition(path=self.path, client_factory=mock.Mock(side_effect=SystemExit("simulated interruption")))
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        with mock.patch.object(runtime, "credentials") as credentials:
            result = self.acquisition(path=self.path, client_factory=mock.Mock())
        credentials.assert_not_called()
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual(result["status"][0]["next_attempt"], self.now + 43200)
        self.assertEqual(result["status"][0]["failures"], 0)
        self.now += 43200
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            result = self.acquisition(path=self.path, client_factory=mock.Mock(return_value=SyntheticBank(self.config)))
        self.assertEqual(result["results"][0]["state"], "updated")

    def test_expected_acquisition_failures_keep_data_quota_and_safe_cli_exit_one(self):
        self.store.configure(self.config)
        self.store.commit_snapshot("bank", snapshot(transaction(), valid_until=self.now + 31536000))
        original = self.rows(db.BudgetTransaction)[0].payload_json
        cases = [(BudgetAPIError(403, "authorization"), "authorization"),
                 (BudgetAPIError(code="network", retryable=True), "network"),
                 (BudgetAPIError(429, "rate_limit", True), "rate_limit"),
                 (BudgetAPIError(code="invalid_configuration"), "configuration"),
                 (BudgetDataError("invalid_session"), "invalid_session"),
                 (ValueError("SECRET provider or programming error"), "internal_error"),
                 (BudgetConfigError("Cannot safely read budget credential file"), "configuration"),
                 (BudgetStoreError("Bank account selection changed during acquisition"), "invalid_data")]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, code=expected):
                self.now += 43200
                self.factory = mock.Mock()
                with mock.patch.object(cli, "load_config", return_value=self.config), \
                     mock.patch.object(cli, "run_once", side_effect=self.cli_acquisition), \
                     mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)), \
                     mock.patch.object(runtime, "fetch_snapshot", side_effect=error):
                    code, output, diagnostic = self.invoke(["--db", str(self.path), "once"])
                self.assertEqual(code, 1)
                self.assertEqual(diagnostic, "")
                result = json.loads(output)
                self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
                self.assertEqual(result["status"][0]["last_error"], expected)
                self.assertEqual(result["status"][0]["next_attempt"], self.now + 43200)
                self.assertEqual(self.rows(db.BudgetTransaction)[0].payload_json, original)
                self.assertNotIn("Synthetic purchase", output)
                self.assertNotIn("synthetic-hash", output)
                self.assertNotIn("12345678901234567890", output)

    def test_partial_account_error_commits_good_account_and_preserves_failed_account_data(self):
        self.config = configuration(accounts=("synthetic-hash", "second-hash"))
        self.store.configure(self.config)
        first = snapshot(transaction("seed-first"), valid_until=self.now + 31536000).accounts[0]
        second = snapshot(transaction("seed-second"), account_hash="second-hash", valid_until=self.now + 31536000).accounts[0]
        second.balances = ({"name": "Old balance", "balance_type": "CLBD",
                            "balance_amount": {"amount": "999.01", "currency": "PLN"}},)
        self.store.commit_snapshot("bank", SimpleNamespace(valid_until=self.now + 31536000,
                                                           accounts=(first, second), errors=()))
        old_success = self.now
        second_before = next(row for row in self.rows(db.BudgetAccount) if row.identification_hash == "second-hash")
        self.now += 3600
        self.factory = mock.Mock(return_value=SyntheticBank(self.config, failures=("second-hash",)))
        with mock.patch.object(cli, "load_config", return_value=self.config), \
             mock.patch.object(cli, "run_once", side_effect=self.cli_acquisition), \
             mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            code, output, diagnostic = self.invoke(["--db", str(self.path), "once"])
        self.assertEqual((code, diagnostic), (1, ""))
        result = json.loads(output)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "partial"}])
        self.assertEqual(result["status"][0]["last_error"], "partial_data")
        self.assertEqual(result["status"][0]["last_success"], old_success)
        accounts = {row.identification_hash: row for row in self.rows(db.BudgetAccount)}
        self.assertEqual(accounts["second-hash"].balances_json, second_before.balances_json)
        self.assertEqual(accounts["second-hash"].observed_at, old_success)
        self.assertEqual(accounts["second-hash"].last_error, "provider_error")
        self.assertEqual(accounts["synthetic-hash"].observed_at, self.now)
        self.assertIsNone(accounts["synthetic-hash"].last_error)
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)},
                         {"seed-first", "seed-second", "ref-1"})
        self.assertEqual(result["status"][0]["next_attempt"], self.now + 43200)
        self.assertNotIn("second-hash", output)
        self.assertNotIn("SECRET", output)
        self.now += 43200
        self.factory = mock.Mock(return_value=SyntheticBank(self.config, failures=self.config.connections[0].accounts))
        with mock.patch.object(cli, "load_config", return_value=self.config), \
             mock.patch.object(cli, "run_once", side_effect=self.cli_acquisition), \
             mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            code, output, diagnostic = self.invoke(["--db", str(self.path), "once"])
        self.assertEqual((code, diagnostic), (1, ""))
        result = json.loads(output)
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"}])
        self.assertEqual(result["status"][0]["last_error"], "account_errors")
        self.assertEqual(result["status"][0]["last_success"], old_success)
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 3)

    def test_invalid_commit_is_atomic_and_other_connections_continue(self):
        first = self.config.connections[0]
        second = replace(first, id="other-bank", owner="other", accounts=("second-hash",))
        config = replace(self.config, connections=(first, second))
        self.store.configure(config)
        self.store.commit_snapshot("bank", snapshot(transaction(), valid_until=self.now + 31536000))
        invalid = snapshot(transaction("new-but-unselected"), account_hash="not-selected", valid_until=self.now + 31536000)
        valid = snapshot(transaction("other-ref"), account_hash="second-hash", valid_until=self.now + 31536000)
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)), \
             mock.patch.object(runtime, "fetch_snapshot", side_effect=(invalid, valid)):
            result = self.acquisition(config, path=self.path, client_factory=mock.Mock())
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"},
                                            {"connection": "other-bank", "state": "updated"}])
        self.assertEqual({row.entry_reference for row in self.rows(db.BudgetTransaction)}, {"ref-1", "other-ref"})
        self.assertEqual(result["status"][0]["last_error"], "invalid_data")
        self.assertIsNone(result["status"][1]["last_error"])
        self.now += 43200
        with mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)), \
             mock.patch.object(runtime, "fetch_snapshot", side_effect=(ValueError("SECRET failure"), valid)):
            result = self.acquisition(config, path=self.path, client_factory=mock.Mock())
        self.assertEqual(result["results"], [{"connection": "bank", "state": "failed"},
                                            {"connection": "other-bank", "state": "updated"}])
        self.assertEqual(result["status"][0]["last_error"], "internal_error")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_discovery_only_connection_is_deferred_without_any_secret_or_http_access(self):
        with mock.patch.object(runtime, "credentials") as credentials:
            factory = mock.Mock()
            result = self.acquisition(configuration(accounts=()), path=self.path, client_factory=factory)
        credentials.assert_not_called()
        factory.assert_not_called()
        self.assertEqual(result["results"], [{"connection": "bank", "state": "deferred"}])
        self.assertEqual(result["status"][0]["selected_accounts"], 0)

    def test_environment_database_and_empty_configuration_are_explicit(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path)}), \
             mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            result = self.acquisition(client_factory=mock.Mock(return_value=SyntheticBank(self.config)))
        self.assertEqual(result["results"][0]["state"], "updated")
        empty = self.acquisition(replace(self.config, connections=()), path=self.path, client_factory=mock.Mock())
        self.assertEqual(empty["results"], [])
        self.assertFalse(empty["status"][0]["enabled"])

    def test_lock_rejects_overlap_and_releases_after_exception(self):
        nested = self.path.parent / "new-parent" / "nested.db"
        with runtime.budget_lock(nested):
            with self.assertRaisesRegex(runtime.BudgetBusyError, "already running"):
                with runtime.budget_lock(nested):
                    self.fail("overlapping acquisition entered the lock")
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with runtime.budget_lock(nested):
                raise RuntimeError("synthetic failure")
        with runtime.budget_lock(nested):
            self.assertTrue(Path(str(nested) + ".budget-sync.lock").is_file())

    def test_cli_status_reads_no_secrets_with_explicit_and_environment_database(self):
        with mock.patch.object(cli, "load_config", return_value=self.config), \
             mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch.object(runtime, "EnableBankingClient") as client:
            code, output, diagnostic = self.invoke(["--config", "/synthetic/config.json", "--db", str(self.path), "status"])
            self.assertEqual(code, 0)
            self.assertEqual(diagnostic, "")
            self.assertEqual(json.loads(output)["status"][0]["selected_accounts"], 1)
            with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path)}):
                code, output, diagnostic = self.invoke(["status"])
        credentials.assert_not_called()
        client.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(diagnostic, "")
        self.assertEqual(json.loads(output)["status"][0]["owner"], "owner")
        self.assertNotIn("synthetic-hash", output)

    def test_cli_missing_config_busy_and_invalid_owner_have_safe_exit_two(self):
        with mock.patch.object(cli, "load_config", return_value=None):
            code, output, diagnostic = self.invoke(["once"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("HESTIA_BUDGET_CONFIG", diagnostic)
        with mock.patch.object(cli, "load_config", return_value=self.config), runtime.budget_lock(self.path):
            code, output, diagnostic = self.invoke(["--db", str(self.path), "status"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("already running", diagnostic)
        with mock.patch.object(cli, "load_config", return_value=configuration(owner="missing")):
            code, output, diagnostic = self.invoke(["--db", str(self.path), "status"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("existing enabled admin owner", diagnostic)

    def test_cli_unexpected_error_never_exposes_exception_and_durable_data_survives(self):
        self.store.configure(self.config)
        self.store.commit_snapshot("bank", snapshot(transaction(), valid_until=self.now + 31536000))
        with mock.patch.object(cli, "load_config", return_value=self.config), \
             mock.patch.object(cli, "run_once", side_effect=self.cli_acquisition), \
             mock.patch.object(runtime, "credentials", side_effect=RuntimeError("SECRET key path and bank payload")):
            code, output, diagnostic = self.invoke(["--db", str(self.path), "once"])
        self.assertEqual((code, diagnostic), (1, ""))
        self.assertEqual(json.loads(output)["status"][0]["last_error"], "internal_error")
        self.assertNotIn("SECRET", output)
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.assertEqual(self.store.status()[0]["next_attempt"], self.now + 43200)

    def test_cli_unexpected_database_failure_is_redacted(self):
        with mock.patch.object(cli, "execute", side_effect=RuntimeError("SECRET database detail")):
            code, output, diagnostic = self.invoke(["status"])
        self.assertEqual((code, output), (1, ""))
        self.assertEqual(diagnostic, "Bank acquisition failed; previously committed data is retained\n")

    def test_cli_success_and_deferred_result_exit_zero(self):
        self.factory = mock.Mock(return_value=SyntheticBank(self.config))
        with mock.patch.object(cli, "load_config", return_value=self.config), \
             mock.patch.object(cli, "run_once", side_effect=self.cli_acquisition), \
             mock.patch.object(runtime, "credentials", return_value=(FAKE_KEY, APP)):
            for expected in ("updated", "deferred"):
                code, output, diagnostic = self.invoke(["--db", str(self.path), "once"])
                self.assertEqual((code, diagnostic), (0, ""))
                self.assertEqual(json.loads(output)["results"][0]["state"], expected)
                self.assertNotIn("SECRET", output)
                self.assertNotIn("synthetic-hash", output)


if __name__ == "__main__":
    unittest.main()
