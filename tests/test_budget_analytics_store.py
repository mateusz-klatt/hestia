"""Synthetic owner-private annotations, previews and durable transfer reservations."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select

from hestia import db
from hestia.budget.analytics_core import AnalyticsError, financial_fingerprint
from hestia.budget import analytics_core, analytics_store
from hestia.budget.analytics_store import BudgetAnalyticsStore
from hestia.budget.views import BudgetAccessDenied, BudgetUsersBackendRequired
from hestia.budget.store import BudgetNotFound
from tests.test_budget_analytics_core import identity, payload
from tests.test_budget_store import configuration, prepare_selected


class AnalyticsCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "synthetic.db"
        self.engine, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add_all([db.User(username="owner", password_hash="unused", role="admin"),
                             db.User(username="other", password_hash="unused", role="admin"),
                             db.AppMeta(key="users_authoritative", value="1")])
        self.now = 1_000_000.0
        self.rule_key_file = self.path.parent / "counterparty-rule.key"
        self.rule_key_file.write_bytes(b"synthetic-only-32-byte-rule-key-value")
        self.rule_key_file.chmod(0o400)
        self.store = BudgetAnalyticsStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only",
                                          rule_key_file=str(self.rule_key_file))
        config = configuration(accounts=("a", "b", "c", "d"))
        other = replace(config.connections[0], id="other-bank", owner="other", accounts=("foreign",))
        self.config = replace(config, connections=(*config.connections, other))
        prepare_selected(self.store, self.config)
        self.accounts = {row.identification_hash: row.id for row in self.rows(db.BudgetAccount)}
        self.add_transaction("a", "debit", amount="100", booking_date="2026-11-30")
        self.add_transaction("b", "credit", amount="100.00", credit_debit_indicator="CRDT", booking_date="2026-12-01")
        self.add_transaction("c", "credit-2", amount="100", credit_debit_indicator="CRDT", booking_date="2026-12-02")
        self.add_transaction("d", "debit-2", amount="100", booking_date="2026-11-29")
        self.add_transaction("foreign", "foreign", amount="100", booking_date="2026-11-30")

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def add_transaction(self, account, reference, **changes):
        value = payload(**changes)
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(account_id=self.accounts[account], entry_reference=reference,
                        payload_json=json.dumps(value), observed_at=self.now, source="api", api_observed_at=self.now,
                        uncertain=False))
        return value

    def transaction(self, account="a", reference="debit"):
        with self.sessions() as session:
            return session.get(db.BudgetTransaction, (self.accounts[account], reference))

    def change_transaction(self, account="a", reference="debit", *, uncertain=None, **changes):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (self.accounts[account], reference))
            value = json.loads(row.payload_json) | changes
            row.payload_json = json.dumps(value)
            if uncertain is not None:
                row.uncertain = uncertain
        return value

    def fingerprint(self, account="a", reference="debit"):
        return financial_fingerprint(json.loads(self.transaction(account, reference).payload_json))

    def annotation(self, *, account="a", reference="debit", **changes):
        defaults = {"mode": "manual", "category_id": None, "kind": "auto", "expected_version": 0,
                    "expected_fingerprint": self.fingerprint(account, reference)}
        return self.store.annotate("owner", self.accounts[account], reference, **(defaults | changes))

    def pair_fps(self, debit=("a", "debit"), credit=("b", "credit")):
        result = []
        with self.sessions() as session:
            for account, reference in (debit, credit):
                row = session.get(db.BudgetTransaction, (self.accounts[account], reference))
                value = json.loads(row.payload_json)
                result.append(self.store._evidence(session, "owner", row, value, financial_fingerprint(value))[0])
        return result

    def decide(self, *, debit=("a", "debit"), credit=("b", "credit"), **changes):
        first, second = self.pair_fps(debit if isinstance(debit, tuple) else ("a", "debit"),
                                      credit if isinstance(credit, tuple) else ("b", "credit"))
        defaults = {"debit": {"account_id": self.accounts[debit[0]], "entry_reference": debit[1]} if isinstance(debit, tuple) else debit,
                    "credit": {"account_id": self.accounts[credit[0]], "entry_reference": credit[1]} if isinstance(credit, tuple) else credit,
                    "action": "accept", "expected_version": 0,
                    "expected_debit_fingerprint": first, "expected_credit_fingerprint": second}
        return self.store.decide_transfer("owner", **(defaults | changes))

    @staticmethod
    def candidate(**changes):
        return {"rule_id": None, "expected_version": 0, "field": "description", "operator": "contains",
                "value": "shop", "category_id": None, "kind": "expense", "priority": 100, "enabled": True, **changes}

    def metadata(self, account, reference, value, *, fingerprint=None):
        with self.sessions.begin() as session:
            session.add(db.BudgetTransactionMetadata(account_id=self.accounts[account], entry_reference=reference,
                        owner="owner", ledger_fingerprint=fingerprint or self.fingerprint(account, reference),
                        payload_json=json.dumps(value), source="api", observed_at=self.now, version=1))


class CategoryTests(AnalyticsCase):
    def test_owner_uniqueness_exact_retry_versioned_archive_and_private_audit(self):
        first = self.store.create_category("owner", "  Synthetic  Food  ")
        self.assertEqual((first["name"], first["version"], first["changed"]), ("Synthetic Food", 1, True))
        self.assertFalse(self.store.create_category("owner", "Synthetic Food")["changed"])
        other = self.store.create_category("other", "Synthetic Food")
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(len(self.store.categories("owner")), 1)
        with self.assertRaises(AnalyticsError) as conflict:
            self.store.create_category("owner", "synthetic food")
        self.assertEqual(conflict.exception.status, 409)
        unchanged = self.store.update_category("owner", first["id"], name=first["name"], archived=False, expected_version=1)
        self.assertFalse(unchanged["changed"])
        self.now += 1
        renamed = self.store.update_category("owner", first["id"], name="Synthetic Groceries", archived=True, expected_version=1)
        self.assertEqual((renamed["version"], renamed["archived"]), (2, True))
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", first["id"], name="stale", archived=False, expected_version=1)
        self.assertEqual([(row.subject_type, row.action) for row in self.rows(db.BudgetAnalyticsAudit)],
                         [("category", "create"), ("category", "create"), ("category", "update")])
        self.assertEqual(self.rows(db.Audit), [])

    def test_validation_owned_resources_category_cap_and_clock_are_safe(self):
        for value in (None, "", " ", "a" * 81, "a\x00b", "a\u200bb", "\ud800"):
            with self.subTest(value=value), self.assertRaises(AnalyticsError):
                self.store.create_category("owner", value)
        first = self.store.create_category("owner", "One")
        second = self.store.create_category("owner", "Two")
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", second["id"], name="One", archived=False, expected_version=1)
        for target in ("unknown", first["id"]):
            with self.assertRaises(AnalyticsError):
                self.store.update_category("other", target, name="new", archived=False, expected_version=1)
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", "", name="new", archived=False, expected_version=1)
        for values in ({"archived": 1}, {"expected_version": True}, {"expected_version": -1}, {"expected_version": None}):
            with self.subTest(values=values), self.assertRaises(AnalyticsError):
                self.store.update_category("owner", first["id"], **({"name": "One", "archived": False, "expected_version": 1} | values))
        with self.sessions.begin() as session:
            for index in range(98):
                session.add(db.BudgetCategory(id=str(uuid.uuid4()), owner="owner", name=f"Name{index}", name_key=f"name{index}",
                            archived=False, version=1, created_at=self.now, updated_at=self.now))
        with self.assertRaises(AnalyticsError) as cap:
            self.store.create_category("owner", "Too many")
        self.assertEqual((cap.exception.code, cap.exception.status), ("budget_analytics_limit", 413))
        self.assertFalse(self.store.create_category("owner", "One")["changed"])
        self.now = float("nan")
        with self.assertRaises(AnalyticsError) as clock:
            self.store.create_category("other", "Bad clock")
        self.assertEqual(clock.exception.status, 503)

    def test_role_and_authority_changes_refuse_all_analytics_access(self):
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetAccessDenied):
            self.store.create_category("owner", "Blocked")
        with self.sessions.begin() as session:
            row = session.get(db.User, "owner")
            row.disabled, row.role = False, "viewer"
        with self.assertRaises(BudgetAccessDenied):
            self.store.categories("owner")
        with self.sessions.begin() as session:
            session.delete(session.get(db.AppMeta, "users_authoritative"))
        with self.assertRaises(BudgetUsersBackendRequired):
            self.store.create_category("other", "Blocked")


class AnnotationTests(AnalyticsCase):
    def test_kind_edit_retains_current_manual_archived_category_even_under_accepted_transfer(self):
        category = self.store.create_category("owner", "Archived household category")
        self.annotation(category_id=category["id"], kind="expense")
        self.store.update_category("owner", category["id"], name=category["name"], archived=True, expected_version=1)
        retained = self.annotation(category_id=category["id"], kind="excluded", expected_version=1)
        self.assertEqual((retained["category_id"], retained["kind"], retained["version"]), (category["id"], "excluded", 2))
        self.decide()
        with self.store._read("owner") as session:
            current = next(row for row in self.store._snapshot(session, "owner")["transactions"]
                           if (row["account_id"], row["entry_reference"]) == (self.accounts["a"], "debit"))
        self.assertEqual(current["effective"]["source"], "transfer")
        self.assertEqual(current["base_effective"]["category_id"], category["id"])
        retained = self.annotation(category_id=category["id"], kind="expense", expected_version=2)
        self.assertEqual((retained["category_id"], retained["kind"], retained["version"]), (category["id"], "expense", 3))
        self.assertTrue(self.store.categories("owner")[0]["archived"])

    def test_kind_override_retains_current_rule_category_after_archival_without_new_assignments(self):
        category = self.store.create_category("owner", "Rule category")
        preview = self.store.preview_rule("owner", self.candidate(category_id=category["id"], kind="auto"))
        self.store.save_rule("owner", preview["preview_id"])
        self.store.update_category("owner", category["id"], name=category["name"], archived=True, expected_version=1)
        retained = self.annotation(category_id=category["id"], kind="excluded")
        self.assertEqual((retained["category_id"], retained["kind"], retained["version"]), (category["id"], "excluded", 1))
        other = self.store.create_category("owner", "Different archived category")
        self.store.update_category("owner", other["id"], name=other["name"], archived=True, expected_version=1)
        with self.assertRaises(AnalyticsError):
            self.annotation(category_id=other["id"], expected_version=1)
        foreign = self.store.create_category("other", "Foreign archived category")
        self.store.update_category("other", foreign["id"], name=foreign["name"], archived=True, expected_version=1)
        with self.assertRaises(AnalyticsError) as denied:
            self.annotation(category_id=foreign["id"], expected_version=1)
        self.assertEqual(denied.exception.status, 404)
        with self.assertRaises(AnalyticsError):
            self.store.preview_rule("owner", self.candidate(category_id=category["id"]))

    def test_archived_retention_cannot_bypass_source_version_or_stale_manual_review(self):
        category = self.store.create_category("owner", "Archived category")
        original = self.annotation(category_id=category["id"], kind="expense")
        self.store.update_category("owner", category["id"], name=category["name"], archived=True, expected_version=1)
        with self.assertRaises(AnalyticsError) as stale_version:
            self.annotation(category_id=category["id"], kind="excluded", expected_version=0)
        self.assertEqual((stale_version.exception.code, stale_version.exception.status), ("budget_analytics_conflict", 409))
        self.change_transaction(amount="101")
        with self.assertRaises(AnalyticsError) as stale_source:
            self.annotation(category_id=category["id"], kind="excluded", expected_version=1,
                            expected_fingerprint=original["ledger_fingerprint"])
        self.assertEqual((stale_source.exception.code, stale_source.exception.status), ("budget_analytics_source_changed", 409))
        # The old stored category is stale; the current fallback classification is uncategorised.
        with self.assertRaises(AnalyticsError):
            self.annotation(category_id=category["id"], kind="excluded", expected_version=1)
        row = self.rows(db.BudgetAnnotation)[0]
        self.assertEqual((row.category_id, row.kind, row.version), (category["id"], "expense", 1))

    def test_explicit_uncategorized_is_not_auto_and_bank_payload_control_quota_unchanged(self):
        category = self.store.create_category("owner", "Food")
        before = self.transaction().payload_json
        self.assertFalse(self.annotation(mode="auto")["changed"])
        self.assertEqual(self.rows(db.BudgetAnnotation), [])
        first = self.annotation(category_id=category["id"], kind="expense")
        self.assertEqual((first["mode"], first["version"], first["changed"]), ("manual", 1, True))
        self.assertFalse(self.annotation(category_id=category["id"], kind="expense", expected_version=1)["changed"])
        uncategorized = self.annotation(expected_version=1)
        self.assertEqual((uncategorized["category_id"], uncategorized["mode"], uncategorized["version"]), (None, "manual", 2))
        reset = self.annotation(mode="auto", expected_version=2)
        self.assertEqual((reset["mode"], reset["category_id"], reset["kind"], reset["version"]), ("auto", None, "auto", 3))
        self.assertEqual(self.transaction().payload_json, before)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 4)

    def test_virtual_and_existing_annotation_require_current_financial_fingerprint(self):
        old = self.fingerprint()
        self.change_transaction(amount="101")
        with self.assertRaises(AnalyticsError) as stale:
            self.annotation(expected_fingerprint=old)
        self.assertEqual(stale.exception.code, "budget_analytics_source_changed")
        self.assertEqual(self.rows(db.BudgetAnnotation), [])
        first = self.annotation()
        current = self.fingerprint()
        self.change_transaction(remittance_information=["Corrected free text"])
        self.assertFalse(self.annotation(expected_version=1, expected_fingerprint=current)["changed"])
        self.change_transaction(currency="EUR")
        with self.assertRaises(AnalyticsError):
            self.annotation(expected_version=1, expected_fingerprint=current)
        updated = self.annotation(expected_version=first["version"])
        self.assertEqual(updated["version"], 2)
        with self.assertRaises(AnalyticsError):
            self.annotation(expected_version=1)
        for invalid in (None, "bad", True):
            with self.assertRaises(AnalyticsError):
                self.annotation(expected_version=2, expected_fingerprint=invalid)

    def test_selected_history_is_writable_when_acquisition_paused_or_unconfigured(self):
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts["a"]).configured = False
            row = session.get(db.BudgetConnection, "bank")
            row.paused, row.enabled = True, False
        self.assertEqual(self.annotation()["version"], 1)
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts["a"]).enabled = False
        with self.assertRaises(AnalyticsError):
            self.annotation(expected_version=1)

    def test_invalid_direction_ownership_archived_category_and_unsafe_source_refused(self):
        category = self.store.create_category("other", "Foreign")
        with self.assertRaises(AnalyticsError) as foreign:
            self.annotation(category_id=category["id"])
        self.assertEqual(foreign.exception.status, 404)
        own = self.store.create_category("owner", "Archived")
        self.store.update_category("owner", own["id"], name=own["name"], archived=True, expected_version=1)
        for change in ({"mode": "invalid"}, {"mode": "auto", "category_id": own["id"]},
                       {"mode": "auto", "kind": "expense"}, {"kind": "refund"}, {"kind": []},
                       {"category_id": own["id"]}):
            with self.subTest(change=change), self.assertRaises(AnalyticsError):
                self.annotation(**change)
        for reference in ("missing", "", "x" * 1025, None):
            with self.assertRaises(AnalyticsError):
                self.store.annotate("owner", self.accounts["a"], reference, mode="manual", category_id=None,
                                    kind="auto", expected_version=0, expected_fingerprint=self.fingerprint())
        self.change_transaction(uncertain=True)
        with self.assertRaises(AnalyticsError):
            self.annotation()
        self.change_transaction(uncertain=False, status="PDNG")
        with self.assertRaises(AnalyticsError):
            self.annotation()
        self.change_transaction(status="BOOK", booking_date=None, value_date=None)
        with self.assertRaises(AnalyticsError):
            self.annotation()
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = "not-json"
        with self.assertRaises(AnalyticsError):
            self.store.annotate("owner", self.accounts["a"], "debit", mode="manual", category_id=None, kind="auto",
                                expected_version=0, expected_fingerprint="0" * 64)

    def test_corrupt_canonical_json_duplicate_keys_and_nonfinite_values_never_create_annotations(self):
        valid = self.transaction().payload_json
        expected = self.fingerprint()
        corrupt = [valid[:-1] + ', "amount": "100"}', valid[:-1] + ', "unused": NaN}']
        for value in corrupt:
            with self.subTest(payload=value), self.sessions.begin() as session:
                session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = value
            with self.assertRaises(AnalyticsError) as invalid:
                self.store.annotate("owner", self.accounts["a"], "debit", mode="manual", category_id=None,
                                    kind="expense", expected_version=0, expected_fingerprint=expected)
            self.assertEqual((invalid.exception.code, invalid.exception.status),
                             ("budget_analytics_invalid_request", 400))
            self.assertEqual(self.rows(db.BudgetAnnotation), [])
            self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])
            self.assertEqual(self.transaction().payload_json, value)

    def test_opaque_reference_and_annotation_owner_cannot_be_reassigned(self):
        self.add_transaction("a", "opaque\x00ref")
        result = self.annotation(reference="opaque\x00ref")
        self.assertEqual(result["entry_reference"], "opaque\x00ref")
        with self.sessions.begin() as session:
            session.get(db.BudgetAnnotation, (self.accounts["a"], "opaque\x00ref")).owner = "other"
        with self.assertRaises(AnalyticsError) as foreign:
            self.annotation(reference="opaque\x00ref", expected_version=1)
        self.assertEqual(foreign.exception.status, 404)


class TransferTests(AnalyticsCase):
    def test_ingested_a_to_b_to_a_stays_review_until_current_version_explicitly_reconfirmed(self):
        from hestia.budget.analytics_reconcile import reconcile_transfers
        accepted = self.decide(source="heuristic")
        original = json.loads(self.transaction().payload_json)
        self.now += 1
        with self.store.transaction() as session:
            debit = session.get(db.BudgetTransaction, (self.accounts["a"], "debit"))
            debit.payload_json = json.dumps(original | {"amount": "101"})
            self.assertEqual(reconcile_transfers(session, "owner", self.now), 1)
        review = self.rows(db.BudgetTransferDecision)[0]
        self.assertEqual((review.status, review.version, review.updated_at), ("review", 2, self.now))
        self.assertEqual((review.debit_fingerprint, review.credit_fingerprint),
                         (accepted["debit_fingerprint"], accepted["credit_fingerprint"]))
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        self.now += 1
        with self.store.transaction() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = json.dumps(original)
            self.assertEqual(reconcile_transfers(session, "owner", self.now), 0)
        self.assertEqual(self.pair_fps(), [accepted["debit_fingerprint"], accepted["credit_fingerprint"]])
        # A new reader sees a persisted latch even though the original fingerprints match again.
        restarted = BudgetAnalyticsStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-restart")
        with restarted._read("owner") as session:
            snapshot = restarted._snapshot(session, "owner")
            decision = next(item for item in snapshot["decisions"] if item["id"] == accepted["id"])
            debit = next(item for item in snapshot["transactions"] if item["entry_reference"] == "debit")
        self.assertEqual((decision["status"], decision["version"]), ("review", 2))
        self.assertEqual(debit["effective"]["kind"], "expense")
        with self.assertRaises(AnalyticsError) as stale:
            self.decide(decision_id=accepted["id"], expected_version=1, source="heuristic")
        self.assertEqual((stale.exception.code, stale.exception.status), ("budget_analytics_conflict", 409))
        confirmed = self.decide(decision_id=accepted["id"], expected_version=2, source="heuristic")
        self.assertEqual((confirmed["status"], confirmed["version"], confirmed["changed"]), ("accepted", 3, True))
        self.assertEqual({row.decision_id for row in self.rows(db.BudgetTransferLeg)}, {accepted["id"]})
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        with restarted._read("owner") as session:
            debit = next(item for item in restarted._snapshot(session, "owner")["transactions"]
                         if item["entry_reference"] == "debit")
        self.assertEqual(debit["effective"]["kind"], "transfer")
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_reject_and_undo_of_persisted_review_release_legs_without_implicit_reacceptance(self):
        rejected_pair = self.decide()
        undone_pair = self.decide(debit=("d", "debit-2"), credit=("c", "credit-2"))
        with self.sessions.begin() as session:
            for identifier in (rejected_pair["id"], undone_pair["id"]):
                row = session.get(db.BudgetTransferDecision, identifier)
                row.status, row.version = "review", 2
        rejected = self.decide(decision_id=rejected_pair["id"], action="reject", expected_version=2)
        self.assertEqual((rejected["status"], rejected["version"]), ("rejected", 3))
        self.assertEqual({row.decision_id for row in self.rows(db.BudgetTransferLeg)}, {undone_pair["id"]})
        self.store.set_account_selected("owner", self.accounts["c"], False, actor="owner")
        undone = self.store.decide_transfer("owner", decision_id=undone_pair["id"], action="undo", expected_version=2)
        self.assertEqual((undone["status"], undone["version"]), ("undone", 3))
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])
        self.assertEqual({row.status for row in self.rows(db.BudgetTransferDecision)}, {"rejected", "undone"})

    def test_accept_reject_undo_reaccept_reserves_both_legs_and_audits_without_ledger_changes(self):
        before = [(row.account_id, row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)]
        accepted = self.decide()
        self.assertEqual((accepted["status"], accepted["version"], accepted["source"]), ("accepted", 1, "manual"))
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        unchanged = self.decide(decision_id=accepted["id"], expected_version=1)
        self.assertFalse(unchanged["changed"])
        rejected = self.decide(decision_id=accepted["id"], action="reject", expected_version=1)
        self.assertEqual((rejected["status"], rejected["version"]), ("rejected", 2))
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])
        accepted = self.decide(decision_id=accepted["id"], expected_version=2)
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts["b"]).enabled = False
        undone = self.store.decide_transfer("owner", decision_id=accepted["id"], action="undo", expected_version=3)
        self.assertEqual((undone["status"], undone["version"]), ("undone", 4))
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])
        self.assertFalse(self.store.decide_transfer("owner", decision_id=accepted["id"], action="undo", expected_version=4)["changed"])
        self.assertEqual([(row.account_id, row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)], before)
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 4)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        self.assertEqual(self.rows(db.Audit), [])

    def test_stale_financial_or_identity_evidence_refuses_confirmation_until_reviewed(self):
        debit_fp, credit_fp = self.pair_fps()
        self.change_transaction(amount="99")
        with self.assertRaises(AnalyticsError) as stale:
            self.decide(expected_debit_fingerprint=debit_fp, expected_credit_fingerprint=credit_fp)
        self.assertEqual(stale.exception.code, "budget_analytics_source_changed")
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])
        self.change_transaction("b", "credit", amount="99")
        debit_fp, credit_fp = self.pair_fps()
        self.metadata("a", "debit", {"creditor_accounts": [identity("own-b")]})
        with self.assertRaises(AnalyticsError) as stale:
            self.decide(expected_debit_fingerprint=debit_fp, expected_credit_fingerprint=credit_fp)
        self.assertEqual(stale.exception.code, "budget_analytics_source_changed")
        accepted = self.decide()
        self.change_transaction(amount="101")
        self.change_transaction("b", "credit", amount="101")
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2, "A stale accepted pair stays reserved until reviewed")
        reconfirmed = self.decide(decision_id=accepted["id"], expected_version=1)
        self.assertEqual(reconfirmed["version"], 2)

    def test_source_is_checked_but_explicit_manual_pair_can_override_identity_conflict(self):
        heuristic = self.decide(source="heuristic")
        self.assertEqual(heuristic["source"], "heuristic")
        self.store.decide_transfer("owner", decision_id=heuristic["id"], action="undo", expected_version=1)
        for account, ident in (("a", "own-a"), ("b", "own-b")):
            with self.sessions.begin() as session:
                session.add(db.BudgetAccountIdentity(account_id=self.accounts[account], owner="owner", identities_json=json.dumps([identity(ident)]),
                            source="api", observed_at=self.now, version=1))
        self.metadata("a", "debit", {"creditor_accounts": [identity("own-b")]})
        confirmed = self.decide(decision_id=heuristic["id"], expected_version=2, source="account_identity")
        self.assertEqual(confirmed["source"], "account_identity")
        self.store.decide_transfer("owner", decision_id=confirmed["id"], action="undo", expected_version=3)
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit")).payload_json = json.dumps({"creditor_accounts": [identity("different-b")]})
        for source in ("account_identity", "heuristic"):
            with self.assertRaises(AnalyticsError):
                self.decide(decision_id=confirmed["id"], expected_version=4, source=source)
        self.assertEqual(self.decide(decision_id=confirmed["id"], expected_version=4)["status"], "accepted")

    def test_invalid_pair_math_dates_direction_owner_and_version_never_reserve(self):
        for change in ({"amount": "0"}, {"currency": "EUR"}, {"booking_date": "2026-11-01"},
                       {"credit_debit_indicator": "CRDT"}):
            previous = json.loads(self.transaction().payload_json)
            self.change_transaction(**change)
            with self.assertRaises(AnalyticsError):
                self.decide()
            with self.sessions.begin() as session:
                session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = json.dumps(previous)
        self.change_transaction("b", "credit", amount="99")
        with self.assertRaises(AnalyticsError):
            self.decide()
        self.change_transaction("b", "credit", amount="100", credit_debit_indicator="DBIT")
        with self.assertRaises(AnalyticsError):
            self.decide()
        self.change_transaction("b", "credit", credit_debit_indicator="CRDT")
        for changes in ({"source": "automatic"}, {"action": "delete"}, {"debit": {}}, {"credit": None},
                        {"expected_version": True}, {"action": "undo"}):
            with self.subTest(changes=changes), self.assertRaises(AnalyticsError):
                self.decide(**changes)
        with self.assertRaises(AnalyticsError):
            self.decide(credit=("a", "debit"))
        with self.assertRaises(BudgetNotFound):
            self.decide(credit=("foreign", "foreign"))
        accepted = self.decide()
        with self.assertRaises(AnalyticsError):
            self.decide(decision_id=accepted["id"], expected_version=0)
        with self.assertRaises(AnalyticsError):
            self.decide(decision_id=accepted["id"], expected_version=1,
                        debit={"account_id": self.accounts["a"], "entry_reference": "different"})
        with self.assertRaises(AnalyticsError):
            self.decide(decision_id=accepted["id"], expected_version=1,
                        credit={"account_id": self.accounts["c"], "entry_reference": "credit-2"})
        with self.assertRaises(AnalyticsError):
            self.store.decide_transfer("other", decision_id=accepted["id"], action="undo", expected_version=1)

    def test_one_leg_cannot_be_reserved_twice_and_partial_failure_rolls_back_both_legs(self):
        accepted = self.decide()
        with self.assertRaises(AnalyticsError) as busy:
            self.decide(credit=("c", "credit-2"))
        self.assertEqual(busy.exception.code, "budget_analytics_leg_reserved")
        self.assertEqual(len(self.rows(db.BudgetTransferDecision)), 1)
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        with self.assertRaises(AnalyticsError):
            self.decide(debit=("d", "debit-2"))
        self.assertEqual({row.decision_id for row in self.rows(db.BudgetTransferLeg)}, {accepted["id"]})
        with mock.patch.object(self.store, "_analytics_audit", side_effect=RuntimeError("synthetic-disk-error")):
            with self.assertRaises(RuntimeError):
                self.store.decide_transfer("owner", decision_id=accepted["id"], action="undo", expected_version=1)
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        self.assertEqual(self.rows(db.BudgetTransferDecision)[0].status, "accepted")

    def test_two_simultaneous_accepts_have_only_one_atomic_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        barrier = threading.Barrier(2)

        def accept(credit):
            barrier.wait()
            try:
                return self.decide(credit=credit)["status"]
            except AnalyticsError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(accept, credit) for credit in (("b", "credit"), ("c", "credit-2"))]
            outcomes = [future.result() for future in futures]
        self.assertCountEqual(outcomes, ["accepted", "budget_analytics_leg_reserved"])
        self.assertEqual(len(self.rows(db.BudgetTransferDecision)), 1)
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)

    def test_stale_sidecar_is_not_reused_as_current_identity_evidence(self):
        baseline = self.pair_fps()
        self.metadata("a", "debit", {"creditor_accounts": [identity("wrong")]}, fingerprint="0" * 64)
        self.assertEqual(self.pair_fps(), baseline)
        self.assertEqual(self.decide(source="heuristic")["status"], "accepted")

    def test_corrupt_structured_evidence_returns_fixed_error_instead_of_trusting_source_label(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetAccountIdentity(account_id=self.accounts["a"], owner="owner",
                        identities_json=json.dumps([identity("synthetic-secret-identifier" * 8)]),
                        source="api", observed_at=self.now, version=1))
        with self.assertRaises(AnalyticsError) as malformed:
            self.decide(source="heuristic")
        self.assertEqual(str(malformed.exception), "budget_analytics_invalid_request")
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])


class SchemaTests(AnalyticsCase):
    def test_0014_upgrade_drift_and_roundtrip_preserve_existing_budget_controls_and_history(self):
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), db.metadata), [])
        before = [(row.account_id, row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)]
        with self.engine.begin() as connection:
            command.downgrade(db._alembic_config(connection), "0014")
            command.upgrade(db._alembic_config(connection), "head")
        self.assertEqual([(row.account_id, row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)], before)
        self.assertEqual(len(self.rows(db.BudgetAccount)), 5)
        self.assertEqual(self.rows(db.BudgetCategory), [])


class RuleTests(AnalyticsCase):
    def test_rule_can_be_disabled_with_its_archived_category_but_cannot_enable_or_reassign_it(self):
        category = self.store.create_category("owner", "Archived rule category")
        preview = self.store.preview_rule("owner", self.candidate(category_id=category["id"]))
        rule = self.store.save_rule("owner", preview["preview_id"])
        before = [(row.account_id, row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)]
        self.store.update_category("owner", category["id"], name=category["name"], archived=True, expected_version=1)
        candidate = self.candidate(rule_id=rule["id"], expected_version=1, category_id=category["id"], enabled=False)
        disabled_preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(disabled_preview["affected_count"], 4)
        disabled = self.store.save_rule("owner", disabled_preview["preview_id"])
        self.assertEqual((disabled["enabled"], disabled["category_id"], disabled["version"]), (False, category["id"], 2))
        edited_preview = self.store.preview_rule("owner", candidate | {"expected_version": 2, "value": "different literal"})
        self.assertEqual(edited_preview["affected_count"], 0)
        edited = self.store.save_rule("owner", edited_preview["preview_id"])
        self.assertEqual((edited["enabled"], edited["category_id"], edited["version"]), (False, category["id"], 3))
        for invalid in (self.candidate(category_id=category["id"], enabled=False),
                        candidate | {"expected_version": 3, "enabled": True}):
            with self.subTest(candidate=invalid), self.assertRaises(AnalyticsError):
                self.store.preview_rule("owner", invalid)
        different = self.store.create_category("owner", "Other archived category")
        self.store.update_category("owner", different["id"], name=different["name"], archived=True, expected_version=1)
        with self.assertRaises(AnalyticsError):
            self.store.preview_rule("owner", candidate | {"expected_version": 3, "category_id": different["id"]})
        self.assertEqual([(row.account_id, row.entry_reference, row.payload_json) for row in self.rows(db.BudgetTransaction)], before)
        self.assertEqual(self.rows(db.BudgetAcquisition), [])

    def test_simultaneous_save_and_restart_replay_create_one_rule_and_one_audit(self):
        from concurrent.futures import ThreadPoolExecutor
        preview = self.store.preview_rule("owner", self.candidate())
        barrier = threading.Barrier(2)

        def save():
            barrier.wait()
            return self.store.save_rule("owner", preview["preview_id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [future.result() for future in (executor.submit(save), executor.submit(save))]
        self.assertEqual({result["id"] for result in results}, {self.rows(db.BudgetRule)[0].id})
        self.assertCountEqual([result["changed"] for result in results], [True, False])
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 1)
        restarted = BudgetAnalyticsStore(self.sessions, clock=lambda: self.now + 901, cursor_secret=b"synthetic-restart")
        self.assertFalse(restarted.save_rule("owner", preview["preview_id"])["changed"])
        self.assertEqual(len(self.rows(db.BudgetRule)), 1)

    def test_oversized_scope_refuses_preview_without_a_partial_result_or_durable_draft(self):
        from hestia.budget import analytics_views
        with mock.patch.object(analytics_views, "MAX_ROWS", 1), self.assertRaises(AnalyticsError) as large:
            self.store.preview_rule("owner", self.candidate())
        self.assertEqual((large.exception.code, large.exception.status), ("budget_analytics_limit", 413))
        self.assertEqual(self.rows(db.BudgetRulePreview), [])

    def test_real_preview_save_replay_and_manual_annotations_are_preserved(self):
        category = self.store.create_category("owner", "Food")
        self.annotation()
        self.change_transaction(amount="101")  # stale manual still must not be overwritten by a rule.
        preview = self.store.preview_rule("owner", self.candidate(category_id=category["id"]))
        self.assertEqual((preview["affected_count"], len(preview["examples"])), (3, 3))
        self.assertIn({"account_id": self.accounts["d"], "entry_reference": "debit-2"}, preview["examples"])
        self.assertNotIn({"account_id": self.accounts["a"], "entry_reference": "debit"}, preview["examples"])
        self.assertEqual(self.store.rules("owner"), [])
        saved = self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual((saved["version"], saved["changed"], saved["category_id"]), (1, True, category["id"]))
        self.assertEqual(len(self.store.rules("owner")), 1)
        # A saved preview remains an idempotency key even after expiry or source changes.
        self.now += 1000
        self.change_transaction(remittance_information=["New bank text"])
        repeated = self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual((repeated["id"], repeated["version"], repeated["changed"]), (saved["id"], 1, False))
        self.assertEqual([row.action for row in self.rows(db.BudgetAnalyticsAudit)], ["create", "set", "save"])
        self.assertEqual(self.rows(db.BudgetAnnotation)[0].mode, "manual")

    def test_edit_requires_new_preview_and_noop_does_not_create_audit_or_increment_version(self):
        first = self.store.preview_rule("owner", self.candidate())
        saved = self.store.save_rule("owner", first["preview_id"])
        candidate = self.candidate(rule_id=saved["id"], expected_version=1)
        noop = self.store.preview_rule("owner", candidate)
        self.assertEqual(noop["affected_count"], 0)
        same = self.store.save_rule("owner", noop["preview_id"])
        self.assertFalse(same["changed"])
        changed = self.store.preview_rule("owner", candidate | {"priority": 1, "enabled": False})
        saved = self.store.save_rule("owner", changed["preview_id"])
        self.assertEqual((saved["version"], saved["enabled"]), (2, False))
        with self.assertRaises(AnalyticsError):
            self.store.preview_rule("owner", candidate)
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 2)
        self.assertEqual(self.store.rules("other"), [])

    def test_preview_cas_rejects_financial_text_metadata_selection_category_rule_and_annotation_changes(self):
        mutations = (
            lambda: self.change_transaction(amount="99"),
            lambda: self.change_transaction(remittance_information=["Bank text corrected"]),
            lambda: self.metadata("a", "debit", {"creditor_name": "Changed counterparty"}),
            lambda: self.store.set_account_selected("owner", self.accounts["c"], False, actor="owner"),
            lambda: self.store.create_category("owner", "New category"),
            lambda: self.annotation(),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                preview = self.store.preview_rule("owner", self.candidate())
                mutation()
                with self.assertRaises(AnalyticsError) as stale:
                    self.store.save_rule("owner", preview["preview_id"])
                self.assertEqual(stale.exception.code, "budget_analytics_stale_preview")
        prior = self.store.preview_rule("owner", self.candidate())
        intervening = self.store.preview_rule("owner", self.candidate(value="corrected"))
        self.store.save_rule("owner", intervening["preview_id"])
        with self.assertRaises(AnalyticsError) as stale:
            self.store.save_rule("owner", prior["preview_id"])
        self.assertEqual(stale.exception.code, "budget_analytics_stale_preview")

    def test_pause_availability_and_quota_do_not_change_rule_scope_or_preview_digest(self):
        preview = self.store.preview_rule("owner", self.candidate())
        with self.sessions.begin() as session:
            connection = session.get(db.BudgetConnection, "bank")
            connection.paused, connection.enabled, connection.next_attempt = True, False, self.now + 43200
            account = session.get(db.BudgetAccount, self.accounts["a"])
            account.configured = False
            component = session.get(db.BudgetComponent, (account.id, "transactions"))
            component.next_attempt, component.suspended = self.now + 43200, True
        saved = self.store.save_rule("owner", preview["preview_id"])
        self.assertTrue(saved["changed"])
        self.assertEqual(saved["version"], 1)

    def test_preview_excludes_valid_accepted_transfer_legs_but_stale_or_boundary_remain_spending(self):
        accepted = self.decide()
        preview = self.store.preview_rule("owner", self.candidate(kind="auto"))
        self.assertEqual(preview["affected_count"], 2)
        keys = {(item["account_id"], item["entry_reference"]) for item in preview["examples"]}
        self.assertNotIn((self.accounts["a"], "debit"), keys)
        self.assertNotIn((self.accounts["b"], "credit"), keys)
        self.change_transaction(amount="101")
        stale = self.store.preview_rule("owner", self.candidate(kind="auto"))
        self.assertEqual(stale["affected_count"], 4)
        self.store.decide_transfer("owner", decision_id=accepted["id"], action="undo", expected_version=1)
        self.store.set_account_selected("owner", self.accounts["b"], False, actor="owner")
        boundary = self.store.preview_rule("owner", self.candidate(kind="auto"))
        self.assertEqual(boundary["affected_count"], 3)

    def test_validation_expiry_preview_bounds_and_ownership_are_enforced(self):
        bad = (None, {}, self.candidate(extra="bad"), self.candidate(field="execute"), self.candidate(operator="regex"),
               self.candidate(kind=[]), self.candidate(kind="bad"), self.candidate(priority=True), self.candidate(priority=-1),
               self.candidate(priority=10001), self.candidate(enabled=1), self.candidate(value=""),
               self.candidate(value="x" * 257), self.candidate(expected_version=1))
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises(AnalyticsError):
                self.store.preview_rule("owner", candidate)
        preview = self.store.preview_rule("owner", self.candidate())
        with self.assertRaises(AnalyticsError) as foreign:
            self.store.save_rule("other", preview["preview_id"])
        self.assertEqual(foreign.exception.status, 404)
        self.now += 900
        with self.assertRaises(AnalyticsError) as expired:
            self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual(expired.exception.code, "budget_analytics_preview_expired")
        fresh = self.store.preview_rule("owner", self.candidate())
        self.assertEqual(len(self.rows(db.BudgetRulePreview)), 1, "Expired unsaved previews are pruned")
        with self.sessions.begin() as session:
            for index in range(99):
                session.add(db.BudgetRulePreview(id=str(uuid.uuid4()), owner="owner", candidate_json="{}", snapshot_digest="x",
                            created_at=self.now, expires_at=self.now + 900))
        with self.assertRaises(AnalyticsError) as cap:
            self.store.preview_rule("owner", self.candidate())
        self.assertEqual(cap.exception.status, 413)
        self.assertEqual(len(self.rows(db.BudgetRulePreview)), 100)
        self.assertNotEqual(fresh["preview_id"], preview["preview_id"])

    def test_rule_limit_is_checked_at_save_and_examples_are_bounded(self):
        with self.sessions.begin() as session:
            for index in range(analytics_store.MAX_RULES):
                session.add(db.BudgetRule(id=str(uuid.uuid4()), owner="owner", field="description", operator="contains", value="never",
                            category_id=None, kind="auto", priority=index % 10001, enabled=False, version=1,
                            created_at=self.now, updated_at=self.now))
        preview = self.store.preview_rule("owner", self.candidate())
        with self.assertRaises(AnalyticsError) as cap:
            self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual(cap.exception.status, 413)
        for index in range(25):
            self.add_transaction("a", f"more-{index}")
        preview = self.store.preview_rule("owner", self.candidate())
        self.assertEqual((preview["affected_count"], len(preview["examples"])), (29, 20))

    def test_transaction_rollback_does_not_leave_a_rule_or_consume_its_preview(self):
        preview = self.store.preview_rule("owner", self.candidate())
        with mock.patch.object(self.store, "_analytics_audit", side_effect=RuntimeError("synthetic-disk-error")):
            with self.assertRaises(RuntimeError):
                self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual(self.store.rules("owner"), [])
        self.assertIsNone(self.rows(db.BudgetRulePreview)[0].saved_rule_id)
        self.assertTrue(self.store.save_rule("owner", preview["preview_id"])["changed"])


class RuleBatchTests(AnalyticsCase):
    def test_one_batch_matches_single_preview_and_one_digest_fences_every_rule(self):
        from hestia.budget.analytics_views import projection_snapshot

        category = self.store.create_category("owner", "Batch Groceries")
        single = self.store.preview_rule("owner", self.candidate(category_id=category["id"]))
        batch = self.store.preview_rules("owner", [self.candidate(category_id=category["id"]),
                                                   self.candidate(value="missing-literal", priority=4, kind="excluded")])
        self.assertEqual((batch["affected_count"], batch["examples"], batch["rule_count"]),
                         (single["affected_count"], single["examples"], 2))
        self.assertGreater(batch["affected_count"], 0)
        with self.sessions.begin() as session:
            full = projection_snapshot(session, "owner")
            bare = projection_snapshot(session, "owner", classify=False)
        self.assertEqual(bare["digest"], full["digest"])
        saved = self.store.save_rules("owner", batch["preview_id"])
        self.assertEqual(len(saved["rules"]), 2)
        self.assertTrue(all(row["changed"] for row in saved["rules"]))
        self.assertTrue(saved["changed"])
        replay = self.store.save_rules("owner", batch["preview_id"])
        self.assertFalse(replay["changed"])
        self.assertEqual([row["id"] for row in replay["rules"]], [row["id"] for row in saved["rules"]])
        self.assertEqual(len(self.store.rules("owner")), 2)
        with self.assertRaises(AnalyticsError):
            self.store.save_rule("owner", batch["preview_id"])
        with self.assertRaises(AnalyticsError):
            self.store.save_rules("owner", single["preview_id"])

    def test_batch_is_all_or_nothing_on_validation_version_digest_and_cap(self):
        first = self.store.save_rule("owner", self.store.preview_rule("owner", self.candidate())["preview_id"])
        stale = self.store.preview_rules("owner", [self.candidate(value="one", priority=1),
                                                   self.candidate(value="two", priority=2)])
        self.add_transaction("a", "extra", remittance_information=["other"])
        with self.assertRaises(AnalyticsError) as conflict:
            self.store.save_rules("owner", stale["preview_id"])
        self.assertEqual(conflict.exception.code, "budget_analytics_stale_preview")
        self.assertEqual(len(self.store.rules("owner")), 1)
        with self.assertRaises(AnalyticsError):
            self.store.preview_rules("owner", [self.candidate(value="ok"), self.candidate(field="execute")])
        self.assertEqual(len(self.rows(db.BudgetRulePreview)), 2)
        with self.assertRaises(AnalyticsError) as duplicate:
            self.store.preview_rules("owner", [
                self.candidate(rule_id=first["id"], expected_version=1, value="renamed"),
                self.candidate(rule_id=first["id"], expected_version=1, value="again", priority=3)])
        self.assertEqual(duplicate.exception.status, 400)
        self.assertEqual(self.store.rules("owner")[0]["value"], first["value"])
        preview = self.store.preview_rules("owner", [
            self.candidate(rule_id=first["id"], expected_version=1, value="renamed"),
            self.candidate(value="brand-new", priority=9)])
        with self.sessions.begin() as session:
            session.get(db.BudgetRule, first["id"]).version = 2
        with self.assertRaises(AnalyticsError) as mismatch:
            self.store.save_rules("owner", preview["preview_id"])
        self.assertEqual(mismatch.exception.status, 409)
        self.assertEqual(self.store.rules("owner")[0]["value"], first["value"])
        with self.sessions.begin() as session:
            for index in range(analytics_store.MAX_RULES - 1):
                session.add(db.BudgetRule(id=str(uuid.uuid4()), owner="owner", field="description", operator="contains",
                            value=f"filled-{index}", category_id=None, kind="auto", priority=index % 10001,
                            enabled=False, version=1, created_at=self.now, updated_at=self.now))
        capped = self.store.preview_rules("owner", [self.candidate(value="cap-a", priority=1),
                                                    self.candidate(value="cap-b", priority=2)])
        with self.assertRaises(AnalyticsError) as cap:
            self.store.save_rules("owner", capped["preview_id"])
        self.assertEqual(cap.exception.status, 413)
        self.assertEqual(len(self.store.rules("owner")), analytics_store.MAX_RULES)
        self.assertIsNone(next(row for row in self.rows(db.BudgetRulePreview) if row.id == capped["preview_id"]).saved_rule_id)

    def test_batch_rejects_empty_and_oversize_inputs(self):
        with self.assertRaises(AnalyticsError):
            self.store.preview_rules("owner", [])
        with self.assertRaises(AnalyticsError):
            self.store.preview_rules("owner", [self.candidate() for _ in range(analytics_store.MAX_BATCH_RULES + 1)])
        self.assertEqual(self.store.rules("owner"), [])

    def test_mid_batch_failure_rolls_back_every_rule_and_keeps_the_preview(self):
        preview = self.store.preview_rules("owner", [self.candidate(value="one", priority=1),
                                                     self.candidate(value="two", priority=2)])
        calls = {"n": 0}
        real = self.store._analytics_audit

        def audit(session, owner, subject_type, subject_id, action, before, after):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("synthetic-disk-error")
            return real(session, owner, subject_type, subject_id, action, before, after)

        with mock.patch.object(self.store, "_analytics_audit", audit):
            with self.assertRaises(RuntimeError):
                self.store.save_rules("owner", preview["preview_id"])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.store.rules("owner"), [])
        stored = next(row for row in self.rows(db.BudgetRulePreview) if row.id == preview["preview_id"])
        self.assertIsNone(stored.saved_rule_id)
        saved = self.store.save_rules("owner", preview["preview_id"])
        self.assertEqual(len(saved["rules"]), 2)
        self.assertTrue(all(row["changed"] for row in saved["rules"]))

    def test_overlapping_rules_apply_once_in_priority_order(self):
        from hestia.budget.analytics_views import projection_snapshot

        expense = self.store.preview_rule("owner", self.candidate(priority=1, kind="expense"))
        income = self.store.preview_rule("owner", self.candidate(priority=2, kind="income"))
        excluded = self.store.preview_rule("owner", self.candidate(priority=2, kind="excluded"))
        batch = self.store.preview_rules("owner", [self.candidate(priority=1, kind="expense"),
                                                   self.candidate(priority=2, kind="excluded")])
        self.assertEqual(expense["affected_count"], 4)
        self.assertEqual(income["affected_count"], 2)
        self.assertGreater(excluded["affected_count"], income["affected_count"])
        self.assertEqual(batch["affected_count"], excluded["affected_count"])
        self.assertLess(batch["affected_count"], expense["affected_count"] + excluded["affected_count"])
        saved = self.store.save_rules("owner", batch["preview_id"])
        by_priority = {row["priority"]: row["id"] for row in saved["rules"]}
        with self.sessions.begin() as session:
            snapshot = projection_snapshot(session, "owner")
        matched = [row for row in snapshot["transactions"] if row["effective"]["rule_id"] is not None]
        self.assertEqual(len(matched), batch["affected_count"])
        for row in matched:
            expected = by_priority[1] if row["payload"]["credit_debit_indicator"] == "DBIT" else by_priority[2]
            self.assertEqual(row["effective"]["rule_id"], expected)

    def test_preview_and_watch_reclassification_stay_consistent_across_expense_fallback_and_income_override(self):
        from hestia.budget.analytics_views import projection_snapshot

        ai = self.store.create_category("owner", "Subskrypcje AI")
        other_income = self.store.create_category("owner", "Zwroty inne")
        # Manual annotation on a CRDT cannot use kind="expense" even though rules can fall back to refund.
        with self.assertRaises(AnalyticsError):
            self.annotation(account="b", reference="credit", kind="expense")

        # Manual annotation on c/credit-2 must stay untouched by expense fallback.
        self.annotation(account="c", reference="credit-2", category_id=other_income["id"], kind="income")

        # 1. Preview & save an expense rule: matches a/debit, d/debit-2 (DBIT->expense) and b/credit (CRDT->refund).
        exp_preview = self.store.preview_rule("owner", self.candidate(category_id=ai["id"], priority=1, kind="expense"))
        self.assertEqual(exp_preview["affected_count"], 3)
        self.assertEqual(
            {(item["account_id"], item["entry_reference"]) for item in exp_preview["examples"]},
            {(self.accounts["a"], "debit"), (self.accounts["d"], "debit-2"), (self.accounts["b"], "credit")},
        )
        exp_rule = self.store.save_rule("owner", exp_preview["preview_id"])
        with self.sessions() as session:
            by_ref = {row["entry_reference"]: row["effective"] for row in projection_snapshot(session, "owner")["transactions"]}
        self.assertEqual(by_ref["credit"], {"kind": "refund", "category_id": ai["id"], "source": "rule", "rule_id": exp_rule["id"]})
        self.assertEqual(by_ref["credit-2"]["source"], "manual")

        # 2. Preview & save a lower-priority income rule: beats expense fallback on b/credit only (watch filter).
        inc_candidate = self.candidate(category_id=other_income["id"], priority=100, kind="income")
        inc_preview = self.store.preview_rule("owner", inc_candidate)
        self.assertEqual(inc_preview["affected_count"], 1)
        self.assertEqual(inc_preview["examples"], [{"account_id": self.accounts["b"], "entry_reference": "credit"}])
        inc_rule = self.store.save_rule("owner", inc_preview["preview_id"])

        # 3. Previewing a second lower-priority expense rule when income already matches b/credit affects 0 rows.
        shadowed = self.store.preview_rule("owner", self.candidate(category_id=ai["id"], priority=50, kind="expense"))
        self.assertEqual(shadowed["affected_count"], 0)

        # 4. Disabling the income rule reverts b/credit back to the expense rule as kind="refund".
        disable_preview = self.store.preview_rule(
            "owner",
            inc_candidate | {"rule_id": inc_rule["id"], "expected_version": inc_rule["version"], "enabled": False},
        )
        self.assertEqual(disable_preview["affected_count"], 1)
        self.assertEqual(disable_preview["examples"], [{"account_id": self.accounts["b"], "entry_reference": "credit"}])
        self.store.save_rule("owner", disable_preview["preview_id"])
        with self.sessions() as session:
            reverted = {row["entry_reference"]: row["effective"] for row in projection_snapshot(session, "owner")["transactions"]}
        self.assertEqual(reverted["credit"], {"kind": "refund", "category_id": ai["id"], "source": "rule", "rule_id": exp_rule["id"]})

    def test_old_classification_version_preview_is_rejected_with_stale_409(self):
        ai = self.store.create_category("owner", "Subskrypcje AI")
        candidate = self.candidate(field="description", operator="contains", value="shop",
                                   category_id=ai["id"], kind="expense", priority=10)
        with mock.patch.object(analytics_core, "CLASSIFICATION_VERSION", 1):
            old_single = self.store.preview_rule("owner", candidate)
            old_batch = self.store.preview_rules("owner", [candidate])

        for action in (lambda: self.store.save_rule("owner", old_single["preview_id"]),
                       lambda: self.store.save_rules("owner", old_batch["preview_id"])):
            with self.assertRaises(AnalyticsError) as stale:
                action()
            self.assertEqual((stale.exception.code, stale.exception.status), ("budget_analytics_stale_preview", 409))
        self.assertEqual(self.store.rules("owner"), [])

        # A legacy snapshot_digest minted before classification_version existed is also rejected.
        legacy = self.store.preview_rule("owner", candidate)
        with self.sessions.begin() as session:
            session.get(db.BudgetRulePreview, legacy["preview_id"]).snapshot_digest = "0" * 64
        with self.assertRaises(AnalyticsError) as stale:
            self.store.save_rule("owner", legacy["preview_id"])
        self.assertEqual((stale.exception.code, stale.exception.status), ("budget_analytics_stale_preview", 409))

        fresh = self.store.preview_rule("owner", candidate)
        self.assertEqual(fresh["affected_count"], 4)
        self.assertTrue(self.store.save_rule("owner", fresh["preview_id"])["changed"])

    def test_saved_batch_is_visible_through_the_projection_cache(self):
        from hestia.budget.analytics_views import BudgetAnalyticsViews, clear_projection_cache

        clear_projection_cache()
        self.addCleanup(clear_projection_cache)
        views = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
        before = views.transactions("owner", "2026")
        debit = next(row for row in before["transactions"] if row["entry_reference"] == "debit")
        self.assertIsNone(debit["effective"]["rule_id"])
        preview = views.preview_rules("owner", [self.candidate(kind="excluded")])
        saved = views.save_rules("owner", preview["preview_id"])
        after = views.transactions("owner", "2026")
        debit = next(row for row in after["transactions"] if row["entry_reference"] == "debit")
        self.assertEqual(debit["effective"]["rule_id"], saved["rules"][0]["id"])
        self.assertEqual(debit["effective"]["kind"], "excluded")
        self.assertNotEqual(before["snapshot_digest"], after["snapshot_digest"])

    def test_tampered_preview_without_proposed_id_is_a_client_error(self):
        preview = self.store.preview_rule("owner", self.candidate())
        with self.sessions.begin() as session:
            row = session.get(db.BudgetRulePreview, preview["preview_id"])
            candidate = json.loads(row.candidate_json)
            del candidate["_proposed_id"]
            row.candidate_json = json.dumps(candidate)
        with self.assertRaises(AnalyticsError) as error:
            self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.store.rules("owner"), [])
        batch = self.store.preview_rules("owner", [self.candidate(value="one", priority=1),
                                                   self.candidate(value="two", priority=2)])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetRulePreview, batch["preview_id"])
            payload = json.loads(row.candidate_json)
            del payload["candidates"][1]["_proposed_id"]
            row.candidate_json = json.dumps(payload)
        with self.assertRaises(AnalyticsError) as error:
            self.store.save_rules("owner", batch["preview_id"])
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.store.rules("owner"), [])
        stored = next(row for row in self.rows(db.BudgetRulePreview) if row.id == batch["preview_id"])
        self.assertIsNone(stored.saved_rule_id)

    def test_proposed_handles_unwatched_and_unsafe_rule_fallbacks_and_unfolded_rows(self):
        from hestia.budget.analytics_views import projection_snapshot

        with self.sessions.begin() as session:
            snapshot = projection_snapshot(session, "owner")
        rule = {**self.candidate(kind="excluded"), "id": "proposed-1"}
        stripped = [{key: value for key, value in row.items() if key not in ("_match_texts", "_default_kind")}
                    for row in snapshot["transactions"]]
        expected = BudgetAnalyticsStore._proposed(snapshot["transactions"], [rule], watch=[rule])
        self.assertGreater(expected[0], 0)
        self.assertEqual(BudgetAnalyticsStore._proposed(stripped, [rule]), expected)
        self.assertEqual(BudgetAnalyticsStore._proposed(snapshot["transactions"], [rule],
                                                         watch=[rule | {"field": "execute"}]), expected)

    def test_snapshot_digest_falls_back_when_stored_rule_is_unsafe(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetRule(id=str(uuid.uuid4()), owner="owner", field="execute", operator="contains",
                        value="x", category_id=None, kind="bad", priority=1, enabled=True, version=1,
                        created_at=self.now, updated_at=self.now))
        preview = self.store.preview_rules("owner", [self.candidate()])
        saved = self.store.save_rules("owner", preview["preview_id"])
        self.assertTrue(saved["changed"])
        self.assertEqual(len(saved["rules"]), 1)

    def test_batch_expiry_unsaved_cross_save_and_corrupt_or_tampered_replays(self):
        expired = self.store.preview_rules("owner", [self.candidate(value="exp")])
        self.now += 900
        with self.assertRaises(AnalyticsError) as exc:
            self.store.save_rules("owner", expired["preview_id"])
        self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_preview_expired", 409))

        single = self.store.preview_rule("owner", self.candidate(value="single-ok"))
        saved_single = self.store.save_rule("owner", single["preview_id"])
        with self.sessions.begin() as session:
            session.get(db.BudgetRulePreview, single["preview_id"]).candidate_json = "{corrupt-json"
        replayed_single = self.store.save_rule("owner", single["preview_id"])
        self.assertEqual((replayed_single["id"], replayed_single["changed"]), (saved_single["id"], False))

        unsaved_batch = self.store.preview_rules("owner", [self.candidate(value="shop")])
        with self.assertRaises(AnalyticsError) as cross:
            self.store.save_rule("owner", unsaved_batch["preview_id"])
        self.assertEqual(cross.exception.status, 400)

        saved_batch = self.store.save_rules("owner", unsaved_batch["preview_id"])
        self.assertTrue(saved_batch["changed"])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetRulePreview, unsaved_batch["preview_id"])
            payload = json.loads(row.candidate_json)
            del payload["candidates"][0]["_proposed_id"]
            row.candidate_json = json.dumps(payload)
        with self.assertRaises(AnalyticsError) as tampered:
            self.store.save_rules("owner", unsaved_batch["preview_id"])
        self.assertEqual(tampered.exception.status, 400)


class SpendingLimitStoreTests(AnalyticsCase):
    def save(self, category_id, **changes):
        values = {"currency": "PLN", "monthly_limit": "500.00", "warning_percent": 80,
                  "enabled": True, "expected_version": 0, **changes}
        return self.store.save_spending_limit("owner", category_id=category_id, **values)

    def test_create_cas_noop_update_archived_and_audit(self):
        cat = self.store.create_category("owner", "Groceries")
        foreign = self.store.create_category("other", "Other Groceries")
        created = self.save(cat["id"], monthly_limit="0500.50")
        self.assertEqual((created["monthly_limit"], created["version"], created["changed"]), ("500.5", 1, True))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 3)  # 2 categories + 1 limit
        self.assertEqual(self.rows(db.Audit), [])

        # Strict CAS: expected_version=0 on existing row fails with 409 (no silent upsert),
        # and wrong version fails BEFORE no-op detection.
        for bad_version in (0, 2):
            with self.assertRaises(AnalyticsError) as conflict:
                self.save(cat["id"], monthly_limit="500.5", expected_version=bad_version)
            self.assertEqual((conflict.exception.code, conflict.exception.status), ("budget_analytics_conflict", 409))
        with self.assertRaises(AnalyticsError) as missing_cas:
            self.save(cat["id"], currency="EUR", expected_version=1)
        self.assertEqual((missing_cas.exception.code, missing_cas.exception.status), ("budget_analytics_conflict", 409))

        # Exact no-op (after canonicalization) keeps version, updated_at and audit unchanged.
        self.now += 50
        noop = self.save(cat["id"], monthly_limit="0500.500", expected_version=1)
        self.assertEqual((noop["changed"], noop["version"], noop["updated_at"]), (False, 1, created["updated_at"]))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 3)

        # Update increments version and writes audit.
        updated = self.save(cat["id"], monthly_limit="600", warning_percent=75, expected_version=1)
        self.assertEqual((updated["changed"], updated["version"], updated["monthly_limit"], updated["warning_percent"]),
                         (True, 2, "600", 75))
        self.assertEqual(updated["updated_at"], self.now)
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), 4)

        # Foreign or unknown category -> 404.
        for bad_cat in (foreign["id"], "unknown-category"):
            with self.assertRaises(AnalyticsError) as nf:
                self.save(bad_cat)
            self.assertEqual((nf.exception.code, nf.exception.status), ("budget_analytics_not_found", 404))

        # Archiving category keeps existing enabled limit intact; disabling is allowed,
        # re-enabling or creating new limits on archived category is refused.
        self.store.update_category("owner", cat["id"], name="Groceries", archived=True, expected_version=1)
        with self.assertRaises(AnalyticsError) as archived_new:
            self.save(cat["id"], currency="EUR", expected_version=0)
        self.assertEqual(archived_new.exception.status, 400)
        disabled = self.save(cat["id"], monthly_limit="600", warning_percent=75, enabled=False, expected_version=2)
        self.assertEqual((disabled["enabled"], disabled["version"], disabled["changed"]), (False, 3, True))
        with self.assertRaises(AnalyticsError) as archived_reenable:
            self.save(cat["id"], monthly_limit="600", warning_percent=75, enabled=True, expected_version=3)
        self.assertEqual(archived_reenable.exception.status, 400)

    def test_cap_validation_and_rollback(self):
        cat = self.store.create_category("owner", "Dining")
        bad_inputs = (
            {"category_id": ""}, {"category_id": "uncategorised"}, {"category_id": "x" * 129},
            {"currency": "pln"}, {"currency": "PLNN"}, {"monthly_limit": "0"}, {"monthly_limit": "0.00"},
            {"monthly_limit": "-5"}, {"monthly_limit": "1e2"}, {"monthly_limit": "1" * 81},
            {"monthly_limit": 100}, {"warning_percent": True}, {"warning_percent": 0},
            {"warning_percent": 100}, {"enabled": 1}, {"expected_version": True}, {"expected_version": -1},
        )
        for bad in bad_inputs:
            values = {"category_id": cat["id"], "currency": "PLN", "monthly_limit": "100",
                      "warning_percent": 80, "enabled": True, "expected_version": 0, **bad}
            with self.subTest(bad=bad), self.assertRaises(AnalyticsError) as exc:
                self.store.save_spending_limit("owner", **values)
            self.assertEqual(exc.exception.status, 400)

        # Rollback on audit failure leaves no saved row.
        with mock.patch.object(self.store, "_analytics_audit", side_effect=RuntimeError("synthetic-disk-error")):
            with self.assertRaises(RuntimeError):
                self.save(cat["id"])
        self.assertEqual(self.rows(db.BudgetCategoryLimit), [])

        # Cap 200 counts all owner limits (including disabled) on create only;
        # update, disable and no-op still succeed at 200 rows.
        first = self.save(cat["id"], currency="AAA")
        with self.sessions.begin() as session:
            for index in range(199):
                c1 = chr(66 + (index // 26))
                c2 = chr(65 + (index % 26))
                session.add(db.BudgetCategoryLimit(
                    id=str(uuid.uuid4()), owner="owner", category_id=cat["id"], currency=f"Z{c1}{c2}",
                    monthly_limit="100", warning_percent=80, enabled=bool(index % 2), version=1,
                    created_at=self.now, updated_at=self.now))
        with self.assertRaises(AnalyticsError) as cap:
            self.save(cat["id"], currency="PLN")
        self.assertEqual((cap.exception.code, cap.exception.status), ("budget_analytics_limit", 413))
        self.assertFalse(self.save(cat["id"], currency="AAA", monthly_limit="500", expected_version=1)["changed"])
        disabled = self.save(cat["id"], currency="AAA", monthly_limit="500", enabled=False, expected_version=1)
        self.assertEqual((disabled["enabled"], disabled["version"], disabled["id"]), (False, 2, first["id"]))

    def test_concurrent_same_version_cas_writers_serialize_without_lost_update(self):
        cat = self.store.create_category("owner", "Utilities")

        def run_race(expected_version, amounts):
            barrier = threading.Barrier(2)
            outcomes = []
            lock = threading.Lock()

            def worker(amount):
                writer = BudgetAnalyticsStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
                barrier.wait(timeout=5)
                try:
                    saved = writer.save_spending_limit(
                        "owner", category_id=cat["id"], currency="PLN",
                        monthly_limit=amount, warning_percent=80, enabled=True,
                        expected_version=expected_version,
                    )
                    outcome = ("ok", saved)
                except AnalyticsError as exc:
                    outcome = ("err", exc.code, exc.status)
                with lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=worker, args=(amount,)) for amount in amounts]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            return outcomes

        # Concurrent create (expected_version=0): exactly one succeeds, one gets 409 conflict.
        create_outcomes = run_race(0, ("400", "450"))
        winners = [item[1] for item in create_outcomes if item[0] == "ok"]
        conflicts = [item for item in create_outcomes if item[0] == "err"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(conflicts, [("err", "budget_analytics_conflict", 409)])
        self.assertEqual(winners[0]["version"], 1)
        self.assertEqual(len(self.rows(db.BudgetCategoryLimit)), 1)
        self.assertEqual(self.rows(db.BudgetCategoryLimit)[0].monthly_limit, winners[0]["monthly_limit"])

        # Concurrent update at expected_version=1: exactly one advances to version 2, one gets 409 conflict.
        audit_before = len(self.rows(db.BudgetAnalyticsAudit))
        update_outcomes = run_race(1, ("700", "900"))
        update_winners = [item[1] for item in update_outcomes if item[0] == "ok"]
        update_conflicts = [item for item in update_outcomes if item[0] == "err"]
        self.assertEqual(len(update_winners), 1)
        self.assertEqual(update_conflicts, [("err", "budget_analytics_conflict", 409)])
        self.assertEqual(update_winners[0]["version"], 2)
        saved_row = self.rows(db.BudgetCategoryLimit)[0]
        self.assertEqual((saved_row.version, saved_row.monthly_limit), (2, update_winners[0]["monthly_limit"]))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), audit_before + 1)

    def test_rule_preview_and_save_override_bank_evidenced_card_repayment_and_revert_on_disable(self):
        from hestia.budget.analytics_views import BudgetAnalyticsViews

        cat = self.store.create_category("owner", "Manual Card Bill")
        with self.sessions.begin() as session:
            session.query(db.BudgetTransaction).delete()
            session.get(db.BudgetAccount, self.accounts["a"]).kind = "current"
            session.get(db.BudgetAccount, self.accounts["a"]).kind_confirmed_at = self.now
            session.get(db.BudgetAccount, self.accounts["b"]).kind = "savings"
            session.get(db.BudgetAccount, self.accounts["b"]).kind_confirmed_at = self.now
            session.get(db.BudgetAccount, self.accounts["c"]).enabled = False
            session.get(db.BudgetAccount, self.accounts["c"]).kind = "credit_card"
            session.get(db.BudgetAccount, self.accounts["c"]).kind_confirmed_at = self.now
        self.add_transaction("b", "card-shop", amount="1000", booking_date="2026-11-05",
                             remittance_information=["Zakup kartą - Sklep"])
        self.add_transaction("a", "card-repay", amount="1000", booking_date="2026-11-10",
                             remittance_information=["Spłata karty kredytowej"])

        views = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only",
                                     rule_key_file=str(self.rule_key_file))
        # While card "c" is deselected and "a"/"b" are current/savings, repayment is NOT silently excluded.
        before_card = views.transactions("owner", "2026-11")["transactions"]
        self.assertEqual(next(r for r in before_card if r["entry_reference"] == "card-repay")["effective"]["kind"], "expense")
        self.assertEqual(views.overview("owner", "2026-11")["currencies"][0]["expenses"], "2000")

        # Confirming selected counterpart "b" as the single credit card enables auto-transfer and rule override/disable.
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts["c"]).kind = "other"
            session.get(db.BudgetAccount, self.accounts["b"]).kind = "credit_card"
        initial = views.transactions("owner", "2026-11")["transactions"]
        repay = next(row for row in initial if row["entry_reference"] == "card-repay")
        self.assertEqual(repay["effective"], {"kind": "transfer", "category_id": None, "source": "default", "rule_id": None})
        self.assertEqual(views.overview("owner", "2026-11")["currencies"][0]["expenses"], "1000")

        # Post-hoc rule override counts and applies cleanly on top of auto-transfer default.
        preview_exp = self.store.preview_rule("owner", {
            "rule_id": None, "expected_version": 0, "field": "description", "operator": "contains",
            "value": "Spłata karty kredytowej", "category_id": cat["id"], "kind": "expense", "priority": 1, "enabled": True,
        })
        self.assertEqual((preview_exp["affected_count"], len(preview_exp["examples"])), (1, 1))
        self.assertEqual(preview_exp["examples"][0]["entry_reference"], "card-repay")
        saved_exp = self.store.save_rule("owner", preview_exp["preview_id"])
        rule_id = saved_exp["id"]

        overridden = next(row for row in views.transactions("owner", "2026-11")["transactions"]
                          if row["entry_reference"] == "card-repay")
        self.assertEqual(overridden["effective"], {"kind": "expense", "category_id": cat["id"], "source": "rule", "rule_id": rule_id})
        self.assertEqual(views.overview("owner", "2026-11")["currencies"][0]["expenses"], "2000")

        # Disabling the override reverts preview & projection back to transfer rather than double-counting.
        preview_off = self.store.preview_rule("owner", {
            "rule_id": rule_id, "expected_version": 1, "field": "description", "operator": "contains",
            "value": "Spłata karty kredytowej", "category_id": cat["id"], "kind": "expense", "priority": 1, "enabled": False,
        })
        self.assertEqual(preview_off["affected_count"], 1)
        self.store.save_rule("owner", preview_off["preview_id"])
        reverted = next(row for row in views.transactions("owner", "2026-11")["transactions"]
                        if row["entry_reference"] == "card-repay")
        self.assertEqual(reverted["effective"], {"kind": "transfer", "category_id": None, "source": "default", "rule_id": None})
        self.assertEqual(views.overview("owner", "2026-11")["currencies"][0]["expenses"], "1000")
