"""Synthetic durable banking identities, reconciliation and owner isolation."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from sqlalchemy import select

from hestia import db
from hestia.budget.config import BudgetConfig, ConnectionConfig
from hestia.budget.store import BudgetStore, BudgetStoreError


APP = "00000000-0000-4000-8000-000000000001"


def configuration(accounts=("synthetic-hash",), **changes):
    connection = ConnectionConfig("bank", "owner", "Synthetic Bank", "PL", "/test/session.json", accounts)
    connection = replace(connection, **changes)
    return BudgetConfig(APP, "/test/key.pem", (connection,))


def transaction(ref="ref-1", **changes):
    tx = {"entry_reference": ref, "amount": "12345678901234567890.12345", "currency": "PLN",
          "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-20",
          "value_date": None, "transaction_date": None, "remittance_information": ["Synthetic purchase"]}
    return dict(tx, **changes)


def snapshot(*transactions, account_hash="synthetic-hash", valid_until=1000000):
    account = SimpleNamespace(identification_hash=account_hash,
                              balances=({"name": "Booked", "balance_type": "CLBD",
                                         "balance_amount": {"amount": "-12.345", "currency": "PLN"}},),
                              transactions=tuple(transactions))
    return SimpleNamespace(valid_until=valid_until, accounts=(account,), errors=())


class BudgetStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "synthetic.db"
        self.engine, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin"),
                             db.User(username="viewer", password_hash="unused", role="viewer"),
                             db.User(username="disabled", password_hash="unused", role="admin", disabled=True)])
        self.now = 1000
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)

    def tearDown(self):
        db.reset_engine_cache()
        self.directory.cleanup()

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def test_owner_validation_atomic_config_and_immutable_identity(self):
        for name in ("missing", "viewer", "disabled"):
            with self.subTest(name=name), self.assertRaises(BudgetStoreError):
                self.store.configure(configuration(owner=name))
        self.assertEqual(self.store.status(), [])
        self.store.configure(configuration())
        for change in ({"owner": "other"}, {"bank_name": "Another Bank"}, {"country": "IE"}):
            with self.subTest(change=change), self.assertRaises(BudgetStoreError):
                self.store.configure(configuration(**change))
        with self.assertRaises(BudgetStoreError):
            self.store.configure(replace(configuration(), application_id="other-app"))
        self.assertTrue(self.store.status()[0]["enabled"])

    def test_selection_retirement_and_reenable_preserve_account_identity(self):
        config = configuration()
        self.store.configure(config)
        original = self.rows(db.BudgetAccount)[0].id
        self.store.configure(config)
        self.assertEqual(self.rows(db.BudgetAccount)[0].id, original)
        self.store.configure(replace(config, connections=()))
        self.assertFalse(self.rows(db.BudgetAccount)[0].enabled)
        self.assertFalse(self.store.status()[0]["enabled"])
        with self.assertRaises(BudgetStoreError):
            self.store.start_attempt("bank")
        self.store.configure(config)
        self.assertEqual(self.rows(db.BudgetAccount)[0].id, original)
        duplicate = replace(config.connections[0], id="duplicate")
        with self.assertRaises(BudgetStoreError):
            self.store.configure(replace(config, connections=(config.connections[0], duplicate)))
        self.assertEqual(len(self.store.status()), 1)
        self.store.configure(configuration(accounts=()))
        self.assertFalse(self.store.start_attempt("bank"))
        self.assertFalse(self.rows(db.BudgetAccount)[0].enabled)

    def test_durable_pre_http_quota_and_failure_restart(self):
        self.store.configure(configuration())
        with self.assertRaises(BudgetStoreError):
            self.store.start_attempt("unknown")
        self.assertTrue(self.store.start_attempt("bank"))
        self.assertFalse(self.store.start_attempt("bank"))
        db.reset_engine_cache()
        self.engine, self.sessions = db.init_db(self.path)
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.assertFalse(self.store.start_attempt("bank"))
        self.store.record_failure("bank", "rate_limit")
        state = self.store.status()[0]
        self.assertEqual(state["next_attempt"], self.now + 43200)
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state["last_error"], "rate_limit")
        self.now += 43200
        self.assertTrue(self.store.start_attempt("bank"))
        self.store.record_failure("bank", "secret body must never persist")
        self.assertEqual(self.store.status()[0]["last_error"], "invalid_data")

    def test_repeated_import_corrections_absence_and_unresolved_multiplicity(self):
        self.store.configure(configuration())
        original = transaction()
        self.store.commit_snapshot("bank", snapshot(original))
        self.store.commit_snapshot("bank", snapshot(original))
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.assertEqual(json.loads(self.rows(db.BudgetTransaction)[0].payload_json)["amount"], original["amount"])
        updated = transaction(amount="12.340001")
        ambiguous = transaction(None)
        pending = transaction("pending-ref", status="PDNG")
        undated = transaction("undated", booking_date=None)
        self.store.commit_snapshot("bank", snapshot(updated, ambiguous, ambiguous, pending, undated))
        self.assertEqual(len(self.rows(db.BudgetUnresolved)), 4)
        self.assertEqual(json.loads(self.rows(db.BudgetTransaction)[0].payload_json)["amount"], "12.340001")
        self.assertEqual(json.loads(self.rows(db.BudgetAccount)[0].balances_json)[0]["balance_amount"]["amount"], "-12.345")
        self.store.commit_snapshot("bank", snapshot(transaction("pending-ref")))
        self.assertEqual(len(self.rows(db.BudgetUnresolved)), 0)
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 2)
        self.store.commit_snapshot("bank", snapshot())
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 2)
        self.store.record_failure("bank", "network")
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 2)
        self.store.commit_snapshot("bank", snapshot())
        self.assertIsNone(self.store.status()[0]["last_error"])

    def test_commit_rechecks_expiry_owner_and_entire_selection(self):
        self.store.configure(configuration())
        for observation in (snapshot(valid_until=1000), snapshot(account_hash="different"),
                            SimpleNamespace(valid_until=100000, accounts=(), errors=())):
            with self.subTest(observation=observation), self.assertRaises(BudgetStoreError):
                self.store.commit_snapshot("bank", observation)
        with self.assertRaises(BudgetStoreError):
            self.store.commit_snapshot("unknown", snapshot())
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").enabled = False
        with self.assertRaises(BudgetStoreError):
            self.store.commit_snapshot("bank", snapshot())
        self.store.configure(configuration())
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetStoreError):
            self.store.commit_snapshot("bank", snapshot())
        self.assertEqual(self.rows(db.BudgetTransaction), [])

    def test_owner_scoped_redacted_status_and_renewal_boundary(self):
        config = configuration()
        other = replace(config.connections[0], id="other-bank", owner="other")
        self.store.configure(replace(config, connections=(config.connections[0], other)))
        self.store.commit_snapshot("bank", snapshot(transaction()))
        state = self.store.status(owner="owner")
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]["transactions"], 1)
        self.assertFalse(state[0]["renewal_due"])
        self.now = 1000000 - 604800
        self.assertTrue(self.store.status(owner="owner")[0]["renewal_due"])
        self.assertFalse(self.store.status(owner="other")[0]["renewal_due"])
        self.assertNotIn("synthetic-hash", json.dumps(state))
        self.assertNotIn("Synthetic purchase", json.dumps(state))
        self.assertNotIn("12345678901234567890", json.dumps(state))
        self.assertEqual(self.store.status(owner="missing"), [])

    def test_failed_second_account_rolls_back_first_account(self):
        self.store.configure(configuration(accounts=("synthetic-hash", "second")))
        first = snapshot(transaction()).accounts[0]
        invalid = snapshot({"missing": "required fields"}, account_hash="second").accounts[0]
        combined = SimpleNamespace(valid_until=1000000, accounts=(first, invalid), errors=())
        with self.assertRaises(KeyError):
            self.store.commit_snapshot("bank", combined)
        self.assertEqual(self.rows(db.BudgetTransaction), [])
        self.assertIsNone(self.rows(db.BudgetAccount)[0].observed_at)

    def test_partial_and_all_failed_snapshots_preserve_failed_account_data(self):
        self.store.configure(configuration(accounts=("synthetic-hash", "second")))
        good, second = snapshot(transaction()).accounts[0], snapshot(transaction(), account_hash="second").accounts[0]
        self.store.commit_snapshot("bank", SimpleNamespace(valid_until=1000000, accounts=(good, second), errors=()))
        before = self.rows(db.BudgetAccount)
        self.now += 20
        good = snapshot(transaction("new")).accounts[0]
        error = SimpleNamespace(identification_hash="second", code="provider_error")
        self.store.commit_snapshot("bank", SimpleNamespace(valid_until=1000000, accounts=(good,), errors=(error,)))
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 3)
        state = self.store.status()[0]
        self.assertEqual(state["last_success"], 1000)
        self.assertEqual(state["last_error"], "partial_data")
        failed = next(account for account in self.rows(db.BudgetAccount) if account.identification_hash == "second")
        self.assertEqual(failed.observed_at, 1000)
        self.assertEqual(failed.balances_json, before[1].balances_json)
        self.assertEqual(failed.last_error, "provider_error")
        errors = (SimpleNamespace(identification_hash="synthetic-hash", code="network"), error)
        self.store.commit_snapshot("bank", SimpleNamespace(valid_until=1000000, accounts=(), errors=errors))
        self.assertEqual(self.store.status()[0]["last_error"], "account_errors")
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 3)

    def test_conflicting_references_quarantine_without_freezing_other_records(self):
        self.store.configure(configuration())
        self.store.commit_snapshot("bank", snapshot(transaction()))
        self.store.commit_snapshot("bank", snapshot(transaction(), transaction(amount="99"),
                                                     transaction("fresh"), transaction("new-conflict"),
                                                     transaction("new-conflict", amount="98")))
        state = self.store.status()[0]
        self.assertEqual(state["transactions"], 1)
        self.assertEqual(state["unresolved"], 4)
        self.assertEqual(state["uncertain_transactions"], 1)
        self.assertTrue(next(row for row in self.rows(db.BudgetTransaction) if row.entry_reference == "ref-1").uncertain)
        self.store.commit_snapshot("bank", snapshot())
        self.assertEqual(self.store.status()[0]["unresolved"], 0)
        self.assertEqual(self.store.status()[0]["uncertain_transactions"], 1)
        self.store.commit_snapshot("bank", snapshot(transaction()))
        self.assertEqual(self.store.status()[0]["transactions"], 2)
        self.assertEqual(self.store.status()[0]["unresolved"], 0)
        self.assertEqual(self.store.status()[0]["uncertain_transactions"], 0)

    def test_pending_and_booked_same_reference_are_not_a_booked_conflict(self):
        self.store.configure(configuration())
        self.store.commit_snapshot("bank", snapshot(transaction(status="PDNG"), transaction(),
                                                     transaction(booking_date=None)))
        state = self.store.status()[0]
        self.assertEqual(state["transactions"], 1)
        self.assertEqual(state["uncertain_transactions"], 0)
        self.assertEqual(state["unresolved"], 2)
        self.store.commit_snapshot("bank", snapshot(transaction(status="PDNG")))
        self.assertEqual(self.store.status()[0]["transactions"], 1)
        self.assertEqual(self.store.status()[0]["uncertain_transactions"], 0)


if __name__ == "__main__":
    unittest.main()
