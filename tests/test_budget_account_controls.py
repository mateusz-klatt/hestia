"""Selection never follows config implicitly; confirmations retain owner identity."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import unittest
from unittest import mock

from hestia import db
from hestia.budget import runtime
from hestia.budget.store import ACCOUNT_KINDS, BudgetStore, BudgetStoreError
from tests import test_budget_store as fixtures
from tests.test_budget_runtime import AdvancingTime, SyntheticBank


class BudgetAccountControlTests(AdvancingTime, unittest.TestCase):
    setUp = fixtures.BudgetStoreTests.setUp
    tearDown = fixtures.BudgetStoreTests.tearDown
    rows = fixtures.BudgetStoreTests.rows

    def test_new_accounts_disabled_and_opt_out_survives_every_reconciliation(self):
        config = fixtures.configuration(accounts=("synthetic-hash", "second"))
        self.store.configure(config)
        accounts = self.store.accounts("owner")
        self.assertEqual(len(accounts), 2)
        self.assertTrue(all(row["configured"] and not row["selected"] and not row["active"] for row in accounts))
        self.assertEqual(self.store.selected_hashes("bank", "owner"), set())
        self.assertFalse(self.store.start_attempt("bank"))
        first_id = accounts[0]["id"]
        self.store.set_account_selected("owner", first_id, True, actor="operator:1000")
        self.store.set_account_selected("owner", first_id, False, actor="operator:1000")
        before = self.store.accounts("owner")
        for _ in range(2):
            self.store.configure(config)
        with mock.patch.object(runtime, "credentials") as credentials:
            result = runtime.run_once(config, path=self.path, clock=lambda: self.now, client_factory=mock.Mock(),
                                      monotonic=self.monotonic, sleeper=self.sleep)
        credentials.assert_not_called()
        self.assertEqual(result["results"][0]["state"], "deferred")
        self.assertEqual(self.store.accounts("owner"), before)
        self.assertEqual(len(self.rows(db.BudgetAccountAudit)), 2)

    def test_effective_selection_filters_http_and_rechecks_at_commit(self):
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()
        config = fixtures.configuration(accounts=("synthetic-hash", "second"))
        self.store.configure(config)
        target = next(row for row in self.rows(db.BudgetAccount) if row.identification_hash == "second")
        self.store.set_account_selected("owner", target.id, True, actor="operator")
        bank = SyntheticBank(config)
        # Preserve a real fake-provider response for both configured accounts,
        # while proving only the second account's endpoint is accessed.
        from tests.test_budget_runtime import SECOND_UID, ACCOUNT_UID
        with mock.patch.object(runtime, "credentials", return_value=(b"fake-key", fixtures.APP)):
            result = runtime.run_once(config, path=self.path, clock=lambda: self.now,
                                      client_factory=mock.Mock(return_value=bank),
                                      monotonic=self.monotonic, sleeper=self.sleep)
        paths = [path for path, _ in bank.calls]
        self.assertIn(f"/accounts/{SECOND_UID}/transactions", paths)
        self.assertNotIn(f"/accounts/{ACCOUNT_UID}/transactions", paths)
        self.assertEqual(result["results"][0]["state"], "updated")
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)
        self.store.set_account_selected("owner", target.id, False, actor="operator")
        with self.assertRaisesRegex(BudgetStoreError, "selection changed"):
            self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction(), account_hash="second",
                                                                 valid_until=self.now + 10000))
        self.assertEqual(len(self.rows(db.BudgetTransaction)), 1)

    def test_retirement_preserves_choice_and_requires_allowlist_for_selection(self):
        config = fixtures.configuration()
        fixtures.prepare_selected(self.store, config)
        account_id = self.store.accounts("owner")[0]["id"]
        self.store.configure(replace(config, connections=()))
        retired = self.store.accounts("owner")[0]
        self.assertTrue(retired["selected"])
        self.assertFalse(retired["active"])
        self.assertFalse(retired["configured"])
        self.assertEqual(self.store.selected_hashes("bank", "owner"), set())
        with self.assertRaises(BudgetStoreError):
            self.store.set_account_selected("owner", account_id, True, actor="operator")
        self.store.set_account_selected("owner", account_id, False, actor="operator")
        self.store.configure(config)
        restored = self.store.accounts("owner")[0]
        self.assertEqual(restored["id"], account_id)
        self.assertFalse(restored["selected"])
        self.assertTrue(restored["configured"])
        self.assertFalse(restored["active"])

    def test_kind_confirmation_does_not_change_selection_history_or_quota(self):
        config = fixtures.configuration()
        fixtures.prepare_selected(self.store, config)
        self.store.commit_snapshot("bank", fixtures.snapshot(fixtures.transaction()))
        self.store.start_attempt("bank")
        self.store.record_failure("bank", "rate_limit")
        before = self.store.status()[0]
        account_id = before["accounts"][0]["id"]
        original_payload = self.rows(db.BudgetTransaction)[0].payload_json
        for kind in sorted(ACCOUNT_KINDS):
            self.now += 10
            value = self.store.confirm_account_kind("owner", account_id, kind, actor="operator:1000")
            self.assertTrue(value["changed"])
            self.assertTrue(value["selected"])
            self.assertEqual(value["kind_confirmed_at"], self.now)
            self.assertEqual(value["kind_confirmed_by"], "operator:1000")
            self.assertEqual(value["kind_confirmation_origin"], "operator")
            again = self.store.confirm_account_kind("owner", account_id, kind, actor="second-operator")
            self.assertFalse(again["changed"])
            self.assertEqual(again["kind_confirmed_by"], "operator:1000")
        after = self.store.status()[0]
        for field in ("last_attempt", "last_success", "next_attempt", "last_error", "failures", "transactions", "valid_until"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.rows(db.BudgetTransaction)[0].payload_json, original_payload)
        changes = self.rows(db.BudgetAccountAudit)
        self.assertEqual(len(changes), 1 + len(ACCOUNT_KINDS))
        for row in changes:
            self.assertNotIn("synthetic-hash", row.old_value_json + row.new_value_json)
            self.assertNotIn("12345678901234567890", row.old_value_json + row.new_value_json)
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = BudgetStore(self.sessions, clock=lambda: self.now)
        self.assertEqual(self.store.accounts("owner")[0]["kind"], sorted(ACCOUNT_KINDS)[-1])

    def test_owner_actor_origin_kind_and_boolean_boundaries(self):
        config = fixtures.configuration()
        self.store.configure(config)
        account_id = self.store.accounts("owner")[0]["id"]
        for owner in ("missing", "viewer", "disabled"):
            with self.subTest(owner=owner), self.assertRaises(BudgetStoreError):
                self.store.accounts(owner)
        self.assertEqual(self.store.accounts("other"), [])
        for owner, identity in (("other", account_id), ("owner", "unknown-id")):
            with self.assertRaisesRegex(BudgetStoreError, "Unknown bank account"):
                self.store.set_account_selected(owner, identity, True, actor="operator")
        for actor, origin in ((None, "operator"), ("", "operator"), ("a" * 129, "operator"),
                              ("\x00", "operator"), ("\x7f", "operator"), ("actor", "web")):
            with self.subTest(actor=actor, origin=origin), self.assertRaises(BudgetStoreError):
                self.store.set_account_selected("owner", account_id, True, actor=actor, origin=origin)
        for value in (1, None, "true"):
            with self.subTest(value=value), self.assertRaises(BudgetStoreError):
                self.store.set_account_selected("owner", account_id, value, actor="operator")
        for kind in (None, [], "unknown", "foreign_currency"):
            with self.subTest(kind=kind), self.assertRaises(BudgetStoreError):
                self.store.confirm_account_kind("owner", account_id, kind, actor="operator")
        self.assertEqual(self.rows(db.BudgetAccountAudit), [])
        self.assertEqual(self.store.selected_hashes("unknown", "owner"), set())
        self.assertEqual(self.store.selected_hashes("bank", "other"), set())

    def test_unconfirmed_existing_kind_and_mismatched_parent_fail_closed(self):
        config = fixtures.configuration()
        self.store.configure(config)
        account_id = self.store.accounts("owner")[0]["id"]
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, account_id).kind = "current"
        confirmed = self.store.confirm_account_kind("owner", account_id, "current", actor="operator")
        self.assertTrue(confirmed["changed"])
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").owner = "other"
        self.assertEqual(self.store.accounts("owner"), [])
        with self.assertRaisesRegex(BudgetStoreError, "Unknown bank account"):
            self.store.set_account_selected("owner", account_id, False, actor="operator")

    def test_audit_failure_rolls_back_control_mutation(self):
        self.store.configure(fixtures.configuration())
        account_id = self.store.accounts("owner")[0]["id"]
        with mock.patch.object(self.store, "_audit", side_effect=RuntimeError("synthetic audit failure")):
            with self.assertRaises(RuntimeError):
                self.store.set_account_selected("owner", account_id, True, actor="operator")
            with self.assertRaises(RuntimeError):
                self.store.confirm_account_kind("owner", account_id, "credit_card", actor="operator")
        account = self.store.accounts("owner")[0]
        self.assertFalse(account["selected"])
        self.assertEqual(account["kind"], "unknown")
        self.assertEqual(self.rows(db.BudgetAccountAudit), [])

    def test_credit_limit_lifecycle_validation_zero_amount_audit_and_cli_omission(self):
        from hestia.budget.store import load_credit_limits, parse_credit_limit

        self.store.configure(fixtures.configuration())
        account_id = self.store.accounts("owner")[0]["id"]
        self.assertEqual(load_credit_limits(None, ()), {})
        with self.assertRaises(BudgetStoreError):
            self.store.set_account_credit_limit("owner", account_id, "10000", actor="operator")
        self.store.confirm_account_kind("owner", account_id, "credit_card", actor="operator")

        for bad in (None, "", "0", "0.00", "-10", "+10", "10000000.01", "1e5", 10000, True, "1" * 81):
            with self.subTest(bad=bad), self.assertRaises(BudgetStoreError):
                parse_credit_limit(bad)

        created = self.store.set_account_credit_limit("owner", account_id, "10000.00", actor="operator")
        self.assertEqual(created["credit_limit"], "10000")
        self.assertTrue(created["changed"])
        self.assertNotIn("credit_limit", self.store.accounts("owner")[0])

        repeated = self.store.set_account_credit_limit("owner", account_id, "10000", actor="operator")
        self.assertFalse(repeated["changed"])

        updated = self.store.set_account_credit_limit("owner", account_id, "12500.50", actor="operator")
        self.assertTrue(updated["changed"])
        self.assertEqual(updated["credit_limit"], "12500.5")

        cleared = self.store.set_account_credit_limit("owner", account_id, None, actor="operator")
        self.assertTrue(cleared["changed"])
        self.assertIsNone(cleared["credit_limit"])
        self.assertFalse(self.store.set_account_credit_limit("owner", account_id, None, actor="operator")["changed"])

        audits = [row for row in self.rows(db.BudgetAccountAudit) if row.field == "credit_limit"]
        self.assertEqual(len(audits), 3)
        for row in audits:
            self.assertNotIn("10000", row.old_value_json + row.new_value_json)
            self.assertNotIn("12500", row.old_value_json + row.new_value_json)
            self.assertEqual(set(json.loads(row.old_value_json)), {"configured"})
            self.assertEqual(set(json.loads(row.new_value_json)), {"configured"})

        # Corrupted AppMeta value fails closed.
        key = f"budget_account_credit_limit:{account_id}"
        for raw_bad in ("{bad", json.dumps({"other": "100"}), json.dumps({"amount": "0"})):
            with self.sessions.begin() as session:
                row = session.get(db.AppMeta, key)
                if row is None:
                    session.add(db.AppMeta(key=key, value=raw_bad))
                else:
                    row.value = raw_bad
            with self.assertRaises(BudgetStoreError):
                with self.sessions.begin() as session:
                    load_credit_limits(session, [account_id])


if __name__ == "__main__":
    unittest.main()
