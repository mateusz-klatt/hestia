"""Durable A→B→A review latches on real synthetic API/import transactions."""
from dataclasses import replace
import json
import unittest
from unittest import mock

from hestia import db
from hestia.budget import analytics_reconcile as reconcile, store as store_module
from hestia.budget.analytics_core import AnalyticsError, financial_fingerprint, pair_fingerprint
from hestia.budget.bank_metadata import BankMetadata, TransactionMetadata
from hestia.budget.import_file import ImportSnapshot
from hestia.budget.normalize import AccountSnapshot, Snapshot
from tests import test_budget_analytics_store as fixtures
from tests.test_budget_import_store import quota_state
from tests.test_budget_store import prepare_selected


class AnalyticsReconcileTests(fixtures.AnalyticsCase):
    def decision(self):
        return next(row for row in self.rows(db.BudgetTransferDecision) if row.owner == "owner")

    def latched_audits(self):
        return [row for row in self.rows(db.BudgetAnalyticsAudit) if row.action == "source_changed"]

    def commit(self, *, transactions=None, bank=None, error=None):
        observations = tuple(AccountSnapshot(primary, None, transactions if primary == "a" else None,
                transactions_error=error if primary == "a" else None,
                bank_metadata=bank if primary == "a" else None) for primary in ("a", "b", "c", "d"))
        self.store.commit_snapshot("bank", Snapshot(self.now + 10000, observations, "2026-01-01", "2026-12-31"))

    def test_real_api_financial_cycle_stays_review_reserved_until_explicit_reconfirmation(self):
        saved = self.decide()
        old_fps = saved["debit_fingerprint"], saved["credit_fingerprint"]
        original = json.loads(self.transaction().payload_json)
        self.now += 1
        self.commit(transactions=({**original, "entry_reference": "debit", "amount": "200"},))
        row = self.decision()
        self.assertEqual((row.status, row.version, row.updated_at), ("review", 2, self.now))
        self.assertEqual((row.debit_fingerprint, row.credit_fingerprint), old_fps)
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        self.now += 1
        self.commit(transactions=({**original, "entry_reference": "debit"},))
        self.assertEqual((self.decision().status, self.decision().version), ("review", 2))
        self.assertEqual(len(self.latched_audits()), 1)
        reconfirmed = self.decide(decision_id=saved["id"], expected_version=2, source="manual")
        self.assertEqual((reconfirmed["status"], reconfirmed["version"]), ("accepted", 3))
        self.commit(transactions=({**original, "entry_reference": "debit"},))
        self.assertEqual((self.decision().status, self.decision().version), ("accepted", 3))
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.rows(db.Audit), [])

    def test_real_offline_import_cycle_and_duplicate_enrichment_keep_latch_and_quota(self):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (self.accounts["a"], "debit"))
            row.source, row.api_observed_at = "offline_import", None
        saved = self.decide()
        original = json.loads(self.transaction().payload_json)
        quota = quota_state(self.rows)
        for ordinal, amount in enumerate(("200", "100"), 1):
            self.now += 1
            snapshot = ImportSnapshot("bank", "a", self.now,
                         ({**original, "entry_reference": "debit", "amount": amount},), None, str(ordinal) * 64)
            self.store.import_snapshot(self.config, snapshot)
            self.assertEqual((self.decision().status, self.decision().version), ("review", 2))
        # Replaying the last core batch still runs reconciliation and cannot
        # restore the original exclusion or consume another acquisition.
        self.assertTrue(self.store.import_snapshot(self.config, snapshot)["duplicate"])
        self.assertEqual((self.decision().status, self.decision().version), ("review", 2))
        self.assertEqual(quota_state(self.rows), quota)
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        self.assertEqual(len(self.latched_audits()), 1)
        self.assertEqual(self.decide(decision_id=saved["id"], expected_version=2, source="manual")["status"], "accepted")
        # Sidecar-only changes on an already imported batch are material too.
        self.now += 1
        ids = ({"scheme": "BBAN", "identification": "synthetic-own"},)
        enriched = replace(snapshot, bank_metadata=BankMetadata(ids))
        self.assertTrue(self.store.import_snapshot(self.config, enriched)["duplicate"])
        self.assertEqual((self.decision().status, self.decision().version), ("review", 4))

    def test_identity_and_transaction_evidence_cycles_never_auto_reaccept(self):
        first = ({"scheme": "BBAN", "identification": "synthetic-a"},)
        second = ({"scheme": "BBAN", "identification": "synthetic-b"},)
        self.commit(bank=BankMetadata(first))
        saved = self.decide()
        self.now += 1
        self.commit(bank=BankMetadata(second))
        self.assertEqual((self.decision().status, self.decision().version), ("review", 2))
        self.now += 1
        self.commit(bank=BankMetadata(first))
        self.assertEqual((self.decision().status, self.decision().version), ("review", 2))
        self.decide(decision_id=saved["id"], expected_version=2, source="manual")
        fp = self.fingerprint()
        patch = lambda ids: BankMetadata(transactions=(TransactionMetadata("debit", fp, {"creditor_accounts": list(ids)}),))
        self.now += 1
        self.commit(bank=patch(second))
        self.assertEqual((self.decision().status, self.decision().version), ("review", 4))
        self.now += 1
        self.commit(bank=patch(first))
        self.now += 1
        self.commit(bank=patch(second))
        self.assertEqual((self.decision().status, self.decision().version), ("review", 4))
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)

    def test_uncertain_placeholder_and_failed_partial_round_preserve_core_and_reservations(self):
        self.decide()
        original = json.loads(self.transaction().payload_json)
        self.commit(error="unavailable")
        self.assertEqual(self.decision().status, "accepted")
        self.assertEqual(json.loads(self.transaction().payload_json), original)
        self.assertEqual(self.latched_audits(), [])
        self.now += 1
        self.commit(transactions=({**original, "entry_reference": "debit"},
                                 {**original, "entry_reference": "debit", "amount": "200"}))
        self.assertTrue(self.transaction().uncertain)
        self.assertEqual((self.decision().status, self.decision().version), ("review", 2))
        self.now += 1
        self.commit(transactions=({**original, "entry_reference": "debit"},))
        self.assertFalse(self.transaction().uncertain)
        self.assertEqual(self.decision().status, "review")
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)

    def test_atomic_source_commit_rolls_back_latch_and_core_if_any_following_work_fails(self):
        self.decide()
        original = self.transaction().payload_json
        helper = store_module.reconcile_transfers
        def fail(session, owner, observed_at):
            helper(session, owner, observed_at)
            raise RuntimeError("synthetic transaction rollback")
        with mock.patch.object(store_module, "reconcile_transfers", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.commit(transactions=({**json.loads(original), "entry_reference": "debit", "amount": "200"},))
        self.assertEqual((self.decision().status, self.decision().version), ("accepted", 1))
        self.assertEqual(self.transaction().payload_json, original)
        self.assertEqual(self.latched_audits(), [])

    def test_current_metadata_only_and_batched_lookups_ignore_other_owner(self):
        saved = self.decide()
        self.metadata("a", "debit", {"creditor_accounts": [{"scheme": "BBAN", "identification": "synthetic"}]},
                      fingerprint="0" * 64)
        quota = quota_state(self.rows)
        with self.sessions.begin() as session, mock.patch.object(reconcile, "_CHUNK", 1):
            self.assertEqual(reconcile.reconcile_transfers(session, "owner", self.now), 0)
        other_config = replace(self.config.connections[1], accounts=("foreign", "foreign-credit"))
        self.config = replace(self.config, connections=(self.config.connections[0], other_config))
        prepare_selected(self.store, self.config)
        self.accounts.update({row.identification_hash: row.id for row in self.rows(db.BudgetAccount)})
        self.add_transaction("foreign-credit", "other-credit", amount="100", credit_debit_indicator="CRDT", booking_date="2026-11-30")
        debit = self.transaction("foreign", "foreign")
        credit = self.transaction("foreign-credit", "other-credit")
        fps = [pair_fingerprint(row.account_id, row.entry_reference, json.loads(row.payload_json), own_ids={row.account_id: []})
               for row in (debit, credit)]
        other = self.store.decide_transfer("other", debit={"account_id": debit.account_id, "entry_reference": debit.entry_reference},
                  credit={"account_id": credit.account_id, "entry_reference": credit.entry_reference}, action="accept", expected_version=0,
                  expected_debit_fingerprint=fps[0], expected_credit_fingerprint=fps[1])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit"))
            row.ledger_fingerprint = self.fingerprint()
            self.assertEqual(reconcile.reconcile_transfers(session, "owner", self.now), 1)
        self.assertEqual((self.decision().id, self.decision().status), (saved["id"], "review"))
        unchanged = next(row for row in self.rows(db.BudgetTransferDecision) if row.id == other["id"])
        self.assertEqual((unchanged.status, unchanged.version), ("accepted", 1))
        # The source reconciliation itself never debits a request or modifies a floor.
        with self.sessions.begin() as session:
            self.assertEqual(reconcile.reconcile_transfers(session, "owner", self.now + 1), 0)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertTrue(quota)

    def test_corrupt_or_ineligible_current_sources_fail_closed_without_raw_diagnostics(self):
        valid = json.loads(self.transaction().payload_json)
        duplicate = '{"amount":"999",' + json.dumps(valid)[1:]
        nonfinite = json.dumps(valid)[:-1] + ',"unexpected":NaN}'
        for value in ("not json", "[]", '{"status":"PDNG"}', '{"status":"BOOK"}',
                      duplicate, nonfinite,
                      json.dumps({"amount": "bad", "currency": "PLN", "status": "BOOK", "booking_date": "2026-11-30"})):
            with self.subTest(value=value):
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetTransaction, (self.accounts["a"], "debit"))
                    row.payload_json = value
                    self.assertIsNone(reconcile._current_fingerprint((row.account_id, row.entry_reference),
                                                                     {(row.account_id, row.entry_reference): row}, {}, {}))
        # Restore and accept before testing corrupted identity JSON and metadata JSON.
        self.change_transaction(amount="100", currency="PLN", status="BOOK", booking_date="2026-11-30",
                                credit_debit_indicator="DBIT", value_date=None)
        self.decide()
        with self.sessions.begin() as session:
            session.add(db.BudgetAccountIdentity(account_id=self.accounts["a"], owner="owner", identities_json="bad json",
                                                source="api", observed_at=self.now, version=1))
            self.assertEqual(reconcile.reconcile_transfers(session, "owner", self.now), 1)
        self.assertEqual(self.decision().status, "review")
        for bad in (None, True, -1, float("nan"), 10**1000):
            with self.subTest(bad=bad), self.sessions.begin() as session, self.assertRaises(AnalyticsError):
                reconcile.reconcile_transfers(session, "owner", bad)


if __name__ == "__main__":
    unittest.main()
