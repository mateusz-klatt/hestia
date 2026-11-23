"""Synthetic, owner-private reports: exact arithmetic, durable decisions and snapshot fences."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import unittest
from unittest import mock
import uuid

from sqlalchemy import delete, event, text

from hestia import api_contract, db
from hestia.budget import analytics_views as views
from hestia.budget import forecast
from hestia.budget import month_budget
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_views import BudgetAnalyticsViews, projection_snapshot
from hestia.budget.config import BudgetConfigError
from hestia.budget.views import BudgetAccessDenied
from tests.test_budget_analytics_core import identity
from tests.test_budget_analytics_store import AnalyticsCase


class ProjectionCase(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.store = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetTransaction))

    def output(self, reference, *, period="2026", **filters):
        return next(row for row in self.store.transactions("owner", period, **filters)["transactions"]
                    if row["entry_reference"] == reference)

    def rule(self, *, category=None, **changes):
        values = {"id": str(uuid.uuid4()), "owner": "owner", "field": "description", "operator": "contains",
                  "value": "shop", "category_id": category, "kind": "expense", "priority": 100,
                  "enabled": True, "version": 1, "created_at": self.now, "updated_at": self.now, **changes}
        with self.sessions.begin() as session:
            session.add(db.BudgetRule(**values))
        return values["id"]

    def select_account(self, account, enabled):
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts[account]).enabled = enabled

    def pair(self, *, debit_date="2026-11-30", credit_date="2026-12-01", amount="100"):
        self.add_transaction("a", "debit", amount=amount, booking_date=debit_date)
        self.add_transaction("b", "credit", amount=amount, credit_debit_indicator="CRDT", booking_date=credit_date)

    def identities(self, account, values):
        with self.sessions.begin() as session:
            session.add(db.BudgetAccountIdentity(account_id=self.accounts[account], owner="owner",
                        identities_json=json.dumps(values), source="api", observed_at=self.now, version=1))

    def assert_invalid(self, call, *, code="budget_analytics_invalid_request", status=400):
        with self.assertRaises(AnalyticsError) as error:
            call()
        self.assertEqual((error.exception.code, error.exception.status, str(error.exception)), (code, status, code))


class ReportTests(ProjectionCase):
    def test_empty_reports_and_wire_models(self):
        overview = self.store.overview("owner", "2026-02")
        self.assertEqual((overview["start"], overview["end"]), ("2026-02-01", "2026-02-28"))
        self.assertEqual(overview["currencies"], [])
        self.assertEqual(overview["categories"], [])
        self.assertEqual(overview["months"], [])
        self.assertEqual(overview["scope"]["selected_accounts"], 4)
        self.assertEqual(len(overview["scope"]["freshness"]), 4)
        transactions = self.store.transactions("owner", "2026")
        transfers = self.store.transfers("owner", "2026")
        recurring = self.store.recurring("owner")
        limits = self.store.spending_limits("owner", "2026-02")
        self.assertEqual((transactions["filtered_total"], transfers["filtered_total"]), (0, 0))
        self.assertEqual((recurring["total_hints"], recurring["truncated"], recurring["hints"]), (0, False, []))
        self.assertEqual((limits["limits"], limits["statuses"]), ([], []))
        self.assertIsNone(transactions["next_cursor"])
        api_contract.BudgetAnalyticsOverview.model_validate(overview)
        api_contract.BudgetAnalyticsTransactions.model_validate(transactions)
        api_contract.BudgetAnalyticsTransfers.model_validate(transfers)
        api_contract.BudgetAnalyticsRecurring.model_validate(recurring)
        api_contract.BudgetAnalyticsSpendingLimits.model_validate(limits)

    def test_exact_amounts_negative_net_and_separate_currencies(self):
        enormous, tiny = "9" * 80, "0." + "0" * 77 + "1"
        self.add_transaction("a", "enormous", amount=enormous)
        self.add_transaction("a", "tiny", amount=tiny)
        self.add_transaction("a", "ordinary", amount="1.20", currency="EUR")
        self.add_transaction("a", "refund", amount="3.20", currency="EUR", credit_debit_indicator="CRDT")
        self.annotation(account="a", reference="refund", kind="refund")
        self.add_transaction("a", "credit", amount="7.30", currency="EUR", credit_debit_indicator="CRDT")
        self.add_transaction("a", "income", amount="12", currency="EUR", credit_debit_indicator="CRDT")
        self.annotation(account="a", reference="income", kind="income")
        self.add_transaction("a", "excluded-d", amount="2", currency="EUR")
        self.add_transaction("a", "excluded-c", amount="4", currency="EUR", credit_debit_indicator="CRDT")
        for reference in ("excluded-d", "excluded-c"):
            self.annotation(reference=reference, kind="excluded")
        result = self.store.overview("owner", "2026")
        eur, pln = result["currencies"]
        self.assertEqual((eur["expenses"], eur["refunds"], eur["net_spending"]), ("1.2", "3.2", "-2"))
        self.assertEqual((eur["income"], eur["unclassified_credit"], eur["excluded_debits"], eur["excluded_credits"]),
                         ("12", "7.3", "2", "4"))
        self.assertEqual(pln["expenses"], enormous + "." + "0" * 77 + "1")
        self.assertEqual(result["categories"][0]["name"], None)
        self.assertEqual(self.output("credit")["effective"]["kind"], "unclassified_credit")
        self.assertEqual(self.store.overview("owner", "2026", currency="EUR")["currencies"], [eur])
        api_contract.BudgetAnalyticsOverview.model_validate(result)

    def test_selection_is_independent_of_pause_configuration_and_provider_status(self):
        self.add_transaction("a", "visible", amount="12")
        self.add_transaction("b", "excluded", amount="300")
        self.add_transaction("foreign", "private-other-owner", amount="900")
        self.select_account("b", False)
        with self.sessions.begin() as session:
            account = session.get(db.BudgetAccount, self.accounts["a"])
            account.configured = False
            connection = session.get(db.BudgetConnection, "bank")
            connection.enabled, connection.paused = False, True
            connection.valid_until, connection.next_attempt = 1, self.now + 100_000
            connection.last_error = "network"
        result = self.store.overview("owner", "2026")
        self.assertEqual(result["currencies"][0]["expenses"], "12")
        self.assertEqual((result["scope"]["selected_accounts"], result["scope"]["excluded_accounts"]), (3, 1))
        self.assertEqual(self.store.transactions("owner", "2026")["filtered_total"], 1)
        freshness = {row["account_id"]: row for row in result["scope"]["freshness"]}
        self.assertEqual(freshness[self.accounts["a"]]["transactions"]["state"], "blocked")
        self.assertNotIn(self.accounts["foreign"], freshness)

    def test_global_uncertainty_counts_and_effective_dates_do_not_enter_figures(self):
        self.add_transaction("a", "booked", amount="5", booking_date=None, value_date="2026-01-01")
        self.add_transaction("a", "pending", amount="100", status="PDNG")
        self.add_transaction("a", "undated", amount="200", booking_date=None, value_date=None)
        self.add_transaction("b", "uncertain-excluded", amount="300")
        self.change_transaction("b", "uncertain-excluded", uncertain=True)
        self.select_account("b", False)
        self.add_transaction("foreign", "foreign-uncertain")
        self.change_transaction("foreign", "foreign-uncertain", uncertain=True)
        with self.sessions.begin() as session:
            for account in ("a", "b", "foreign"):
                session.add(db.BudgetUnresolved(account_id=self.accounts[account], ordinal=0,
                            payload_json="{}", observed_at=self.now))
                batch = db.BudgetImportBatch(id="batch-" + account, fingerprint="batch-fp-" + account,
                            account_id=self.accounts[account], fetched_at=self.now, imported_at=self.now, report_json="{}")
                session.add(batch)
                session.flush()
                session.add(db.BudgetImportUnresolved(batch_id=batch.id, ordinal=0, reason="unknown_date", payload_json="{}"))
        result = self.store.overview("owner", "2026")
        self.assertEqual((result["scope"]["uncertain_count"], result["scope"]["unresolved_count"],
                          result["scope"]["import_unresolved_count"]), (1, 2, 2))
        self.assertEqual([(row["month"], row["currencies"][0]["expenses"]) for row in result["months"]], [("2026-01", "5")])
        self.assertEqual(self.store.transactions("owner", "2026")["filtered_total"], 1)
        self.assertEqual(self.store.overview("owner", "2026-11")["currencies"], [])

    def test_reads_never_mutate_and_foreign_parent_is_not_an_ownership_bridge(self):
        self.add_transaction("a", "private")
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.strip().split()[0].upper())
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            self.store.overview("owner", "2026")
            self.store.transactions("owner", "2026")
            self.store.transfers("owner", "2026")
            self.store.recurring("owner")
            self.store.spending_limits("owner", "2026-11")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertLessEqual(set(statements), {"SELECT", "BEGIN"})
        self.assertEqual(self.rows(db.Audit), [])
        self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])
        self.assertEqual(self.rows(db.BudgetAcquisition), [])
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").owner = "other"
        self.assertEqual(self.store.overview("owner", "2026")["currencies"], [])
        self.assertEqual(self.store.transactions("owner", "2026")["transactions"], [])
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(BudgetAccessDenied):
            self.store.overview("owner", "2026")


class ClassificationTests(ProjectionCase):
    def test_rule_manual_uncategorised_stale_manual_and_archived_history(self):
        for reference in ("automatic", "manual", "stale", "nonmatch"):
            self.add_transaction("a", reference, remittance_information=["Synthetic SHOP" if reference != "nonmatch" else "Else"])
        category = self.store.create_category("owner", "Food")
        disabled = self.rule(category=category["id"], enabled=False, priority=1)
        self.rule(category=category["id"], kind="income", priority=2)
        rule_id = self.rule(category=category["id"])
        self.annotation(reference="manual")
        self.annotation(reference="stale", category_id=category["id"])
        self.change_transaction("a", "stale", amount="20")
        self.store.update_category("owner", category["id"], name="Old Food", archived=True, expected_version=1)
        automatic, manual, stale = (self.output(ref) for ref in ("automatic", "manual", "stale"))
        self.assertEqual((automatic["effective"]["source"], automatic["effective"]["rule_id"]), ("rule", rule_id))
        self.assertNotEqual(automatic["effective"]["rule_id"], disabled)
        self.assertEqual((manual["effective"]["source"], manual["effective"]["category_id"]), ("manual", None))
        self.assertTrue(stale["annotation"]["stale"])
        self.assertEqual(stale["effective"]["source"], "default")
        totals = self.store.overview("owner", "2026")["categories"]
        self.assertTrue(next(row for row in totals if row["category_id"] == category["id"])["archived"])
        self.assertEqual(self.store.transactions("owner", "2026", category_id=category["id"])["filtered_total"], 1)
        self.assertEqual(self.store.transactions("owner", "2026", category_id="uncategorised")["filtered_total"], 3)
        self.assertEqual(self.store.transactions("owner", "2026", kind="income")["filtered_total"], 0)
        self.assertEqual(self.store.transactions("owner", "2026", query="else")["filtered_total"], 1)
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", category_id="foreign"),
                            code="budget_analytics_not_found", status=404)

    def test_only_current_sidecar_supplies_names_rules_search_and_identity_evidence(self):
        self.add_transaction("a", "current")
        self.add_transaction("a", "stale")
        metadata = {"creditor_name": "Synthetic Cafe", "debtor_name": None, "mcc": "5812",
                    "creditor_accounts": [identity("owned-destination")], "debtor_accounts": []}
        self.metadata("a", "current", metadata)
        self.metadata("a", "stale", metadata, fingerprint="f" * 64)
        self.identities("b", [identity("owned-destination")])
        self.rule(field="counterparty_name", value="cafe", kind="excluded")
        current, stale = self.output("current"), self.output("stale")
        self.assertEqual(current["metadata"], {"creditor_name": "Synthetic Cafe", "debtor_name": None, "mcc": "5812"})
        self.assertIsNone(stale["metadata"])
        self.assertNotEqual(current["transfer_fingerprint"], stale["transfer_fingerprint"])
        self.assertEqual(self.store.transactions("owner", "2026", query="CAFE")["filtered_total"], 1)
        self.assertEqual(current["effective"]["kind"], "excluded")
        self.assertEqual(self.store.transactions("owner", "2026", query="")["filtered_total"], 2)
        api_contract.BudgetAnalyticsTransactions.model_validate(self.store.transactions("owner", "2026"))

    def test_effective_helper_compiles_rules_and_folds_texts_when_omitted(self):
        category = self.store.create_category("owner", "Food")
        categories = {category["id"]: category}
        annotation = {"mode": "auto", "stale": False, "kind": "auto", "category_id": None}
        rules = [{"id": "r1", "enabled": True, "kind": "expense", "field": "description",
                  "operator": "contains", "value": "shop", "category_id": category["id"], "priority": 1}]
        tx = {"credit_debit_indicator": "DBIT", "remittance_information": ["Synthetic SHOP"]}
        self.assertEqual(views._effective(tx, None, annotation, categories, rules),
                         {"kind": "expense", "category_id": category["id"], "source": "rule", "rule_id": "r1"})
        miss = views._effective({"credit_debit_indicator": "DBIT", "remittance_information": ["Other"]},
                                None, annotation, categories, rules)
        self.assertEqual(miss, {"kind": "expense", "category_id": None, "source": "default", "rule_id": None})


class TransferProjectionTests(ProjectionCase):
    def test_manual_and_rule_exclusions_remove_suggestions_and_ambiguity_edges(self):
        self.pair()
        self.add_transaction("c", "rule-noise", amount="100", credit_debit_indicator="CRDT",
                             remittance_information=["Literal ignored credit"])
        self.add_transaction("d", "manual-noise", amount="100", credit_debit_indicator="CRDT")
        initial = self.store.transfers("owner", "2026")["transfers"]
        self.assertEqual(len(initial), 3)
        self.assertTrue(all(row["ambiguous"] for row in initial))
        self.annotation(account="d", reference="manual-noise", kind="excluded")
        self.rule(field="description", operator="equals", value="Literal ignored credit", kind="excluded")
        remaining = self.store.transfers("owner", "2026")["transfers"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["credit"]["entry_reference"], "credit")
        self.assertFalse(remaining[0]["ambiguous"])
        self.assertEqual(self.output("debit")["transfer"],
                         {"status": "suggested", "decision_id": None, "candidate_count": 1, "ambiguous": False})
        for reference in ("rule-noise", "manual-noise"):
            self.assertEqual(self.output(reference)["transfer"]["candidate_count"], 0)
        self.annotation(kind="excluded")
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 0)

    def test_stale_manual_exclusion_uses_current_default_and_can_suggest_again(self):
        self.pair()
        self.annotation(kind="excluded")
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 0)
        self.change_transaction("a", "debit", amount="101")
        self.change_transaction("b", "credit", amount="101")
        current = self.output("debit")
        self.assertTrue(current["annotation"]["stale"])
        self.assertEqual(current["effective"]["kind"], "expense")
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 1)

    def test_exclusions_never_change_accepted_or_review_decisions_and_reservations(self):
        self.pair()
        saved = self.decide()
        self.annotation(kind="excluded")
        self.rule(kind="excluded")
        self.assertEqual(self.store.transfers("owner", "2026", status="accepted")["filtered_total"], 1)
        self.assertEqual(self.output("debit")["effective"]["kind"], "transfer")
        self.persisted_review(saved["id"])
        self.add_transaction("c", "alternative", amount="100", credit_debit_indicator="CRDT")
        self.assertEqual(self.store.transfers("owner", "2026", status="review")["filtered_total"], 1)
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 0)
        self.assertEqual(self.output("debit")["effective"]["kind"], "excluded")
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)

    def test_global_ambiguity_and_cross_month_acceptance_with_restart(self):
        self.pair()
        self.add_transaction("c", "second", amount="100", credit_debit_indicator="CRDT", booking_date="2026-12-02")
        candidates = self.store.transfers("owner", "2026-11")["transfers"]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(row["ambiguous"] for row in candidates))
        self.assertEqual(self.output("debit")["transfer"]["candidate_count"], 2)
        self.assertEqual(self.store.overview("owner", "2026-11")["currencies"][0]["expenses"], "100")
        saved = self.decide()
        accepted = self.store.transfers("owner", "2026-11", status="accepted")["transfers"]
        self.assertEqual((len(accepted), accepted[0]["id"]), (1, saved["id"]))
        self.assertEqual(self.store.transfers("owner", "2026-12", status="accepted")["filtered_total"], 1)
        totals = self.store.overview("owner", "2026-11")["currencies"][0]
        self.assertEqual((totals["expenses"], totals["transfer_debits"]), ("0", "100"))
        self.store = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
        self.assertEqual(self.output("debit")["effective"]["kind"], "transfer")
        self.assertEqual(self.output("credit")["effective"]["kind"], "transfer")
        api_contract.BudgetAnalyticsTransfers.model_validate(self.store.transfers("owner", "2026", status="accepted"))

    def test_stale_source_uncertainty_and_boundary_never_hide_expense(self):
        self.pair()
        self.decide()
        self.change_transaction("a", "debit", remittance_information=["corrected description"])
        self.assertEqual(self.output("debit")["transfer"]["status"], "accepted")
        self.change_transaction("a", "debit", amount="101")
        review = self.store.transfers("owner", "2026", status="review")["transfers"][0]
        self.assertTrue(review["stale"])
        self.assertEqual(self.store.overview("owner", "2026-11")["currencies"][0]["expenses"], "101")
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 0, "stale accepted legs stay reserved")
        self.change_transaction("a", "debit", amount="100", uncertain=True)
        self.assertEqual(self.store.transfers("owner", "2026", status="review")["filtered_total"], 1)
        self.change_transaction("a", "debit", uncertain=False)
        self.select_account("b", False)
        boundary = self.store.transfers("owner", "2026", status="boundary")["transfers"][0]
        self.assertTrue(boundary["boundary"])
        self.assertEqual({key: boundary["credit"][key] for key in ("selected", "amount", "fingerprint", "date")},
                         {"selected": False, "amount": None, "fingerprint": None, "date": None})
        self.assertEqual(self.store.overview("owner", "2026-11")["currencies"][0]["expenses"], "100")
        self.assertEqual(self.store.transfers("owner", "2026", status="review")["filtered_total"], 0)

    def test_status_priority_and_exact_rejected_undone_tabs(self):
        self.pair()
        self.add_transaction("c", "second", amount="100", credit_debit_indicator="CRDT")
        rejected = self.decide(credit=("c", "second"), action="reject")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransferDecision, rejected["id"])
            row.id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        accepted = self.decide()
        row = self.output("debit")
        self.assertEqual((row["transfer"]["status"], row["transfer"]["decision_id"]), ("accepted", accepted["id"]))
        self.assertEqual(row["effective"]["source"], "transfer")
        self.assertEqual(self.store.transfers("owner", "2026", status="rejected")["filtered_total"], 1)
        self.assertEqual(self.store.transfers("owner", "2026", status="undone")["filtered_total"], 0)
        self.store.decide_transfer("owner", decision_id=accepted["id"], action="undo", expected_version=1)
        self.assertEqual(self.store.transfers("owner", "2026", status="undone")["filtered_total"], 1)
        self.assertEqual(self.store.transfers("owner", "2026", status="rejected")["filtered_total"], 1)
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 0)
        self.assertEqual(self.store.overview("owner", "2026")["transfer_summary"]["rejected"], 1)

    def test_identity_changes_stale_pair_without_exposing_identifications(self):
        self.pair()
        self.identities("b", [identity("synthetic-bban")])
        self.metadata("a", "debit", {"creditor_accounts": [identity("synthetic-bban")]})
        candidate = self.store.transfers("owner", "2026")["transfers"][0]
        self.assertEqual(candidate["source"], "account_identity")
        self.assertNotIn("synthetic-bban", json.dumps(candidate))
        self.decide(source="account_identity")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAccountIdentity, self.accounts["b"])
            row.identities_json = json.dumps([identity("changed-owned-identity")])
            row.version += 1
        self.assertEqual(self.store.transfers("owner", "2026", status="review")["filtered_total"], 1)

    def persisted_review(self, decision_id):
        # Ingestion persists this transition while keeping original evidence and leg reservations.
        with self.sessions.begin() as session:
            decision = session.get(db.BudgetTransferDecision, decision_id)
            decision.status, decision.version, decision.updated_at = "review", decision.version + 1, self.now

    def test_persisted_review_with_matching_evidence_never_reactivates_and_undo_releases_alternatives(self):
        self.pair()
        self.add_transaction("c", "alternative", amount="100", credit_debit_indicator="CRDT")
        saved = self.decide()
        self.persisted_review(saved["id"])
        self.store = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
        review = self.store.transfers("owner", "2026", status="review")["transfers"][0]
        current = self.pair_fps()
        with self.sessions() as session:
            decision = session.get(db.BudgetTransferDecision, saved["id"])
            self.assertEqual((decision.debit_fingerprint, decision.credit_fingerprint), tuple(current))
        self.assertEqual((review["stale"], review["version"]), (True, 2))
        self.assertEqual(self.store.transfers("owner", "2026", status="accepted")["filtered_total"], 0)
        self.assertEqual(self.store.transfers("owner", "2026")["filtered_total"], 0)
        self.assertEqual(self.store.overview("owner", "2026-11")["currencies"][0]["expenses"], "100")
        self.assertEqual(self.output("debit")["effective"]["source"], "default")
        self.assertEqual(self.output("debit")["transfer"]["status"], "review")
        self.assertEqual(len(self.rows(db.BudgetTransferLeg)), 2)
        self.store.decide_transfer("owner", decision_id=saved["id"], action="undo", expected_version=2)
        suggestions = self.store.transfers("owner", "2026")["transfers"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["credit"]["entry_reference"], "alternative")
        self.assertEqual(self.store.transfers("owner", "2026", status="undone")["filtered_total"], 1)
        self.assertEqual(self.rows(db.BudgetTransferLeg), [])

    def test_persisted_review_requires_explicit_reconfirmation_before_report_exclusion(self):
        self.pair()
        saved = self.decide()
        self.persisted_review(saved["id"])
        with self.sessions() as session:
            snapshot = projection_snapshot(session, "owner")
        self.assertTrue(all(row["effective"]["source"] != "transfer" for row in snapshot["transactions"]))
        reconfirmed = self.decide(decision_id=saved["id"], expected_version=2)
        self.assertEqual((reconfirmed["status"], reconfirmed["version"]), ("accepted", 3))
        self.assertEqual(self.store.transfers("owner", "2026", status="review")["filtered_total"], 0)
        self.assertEqual(self.store.overview("owner", "2026-11")["currencies"][0]["expenses"], "0")

    def test_graph_limit_is_explicit_and_does_not_invalidate_accepted_pairs(self):
        self.pair()
        with mock.patch.object(views, "MAX_EDGES", 0):
            result = self.store.transfers("owner", "2026")
            self.assertEqual((result["scan_limited"], result["transfers"]), (True, []))
            self.assertEqual(self.store.overview("owner", "2026-11")["currencies"][0]["expenses"], "100")
        self.decide()
        self.add_transaction("c", "more-debit", amount="15")
        self.add_transaction("d", "more-credit", amount="15", credit_debit_indicator="CRDT")
        with mock.patch.object(views, "MAX_EDGES", 0):
            result = self.store.overview("owner", "2026-11")
        self.assertTrue(result["transfer_summary"]["scan_limited"])
        self.assertEqual((result["currencies"][0]["expenses"], result["currencies"][0]["transfer_debits"]), ("15", "100"))


class PaginationTests(ProjectionCase):
    def seed_rows(self):
        for index, day in enumerate(("2026-11-01", "2026-12-31", "2026-11-30")):
            self.add_transaction("a", str(index), amount="1", booking_date=day)

    def test_whole_period_totals_and_snapshot_bound_pages_with_opaque_references(self):
        self.seed_rows()
        reference = "\x00" + "😀" * 1023
        self.add_transaction("a", reference, amount="2", booking_date="2026-12-31")
        page = self.store.transactions("owner", "2026", limit=1)
        references = []
        while True:
            references.extend(row["entry_reference"] for row in page["transactions"])
            self.assertEqual(page["filtered_total"], 4)
            if page["next_cursor"] is None:
                break
            self.assertLess(len(page["next_cursor"]), 8192)
            self.assertNotIn("amount", base64.urlsafe_b64decode(page["next_cursor"] + "=" * (-len(page["next_cursor"]) % 4)).decode("utf-8", "ignore"))
            page = self.store.transactions("owner", "2026", limit=1, cursor=page["next_cursor"])
        self.assertEqual(len(set(references)), 4)
        self.assertEqual(set(references), {"0", "1", "2", reference})
        self.assertEqual(self.store.overview("owner", "2026")["currencies"][0]["expenses"], "5")
        self.assertEqual(len(self.store.overview("owner", "2026")["months"]), 2)

    def test_cursor_rejects_owner_filter_change_expiry_corruption_and_material_updates(self):
        self.seed_rows()
        first = self.store.transactions("owner", "2026", limit=1)
        cursor = first["next_cursor"]
        for change in ({"currency": "EUR"}, {"limit": 2}, {"period": "2026-11"}):
            values = {"period": "2026", "limit": 1, "cursor": cursor, **change}
            self.assert_invalid(lambda: self.store.transactions("owner", **values))
        self.assert_invalid(lambda: self.store.transactions("other", "2026", limit=1, cursor=cursor))
        for invalid in ("!bad", "a", "x" * 8193, 1, ""):
            self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=invalid))
        self.now += 900
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=cursor))
        self.now -= 900
        self.change_transaction("a", "0", amount="2")
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=cursor),
                            code="budget_analytics_stale_cursor", status=409)

    def test_operational_updates_do_not_stale_cursor_but_selection_and_search_content_do(self):
        self.seed_rows()
        first = self.store.transactions("owner", "2026", limit=1)
        with self.sessions.begin() as session:
            account = session.get(db.BudgetAccount, self.accounts["a"])
            account.configured, account.control_version, account.kind = False, account.control_version + 1, "other"
            connection = session.get(db.BudgetConnection, "bank")
            connection.paused, connection.last_attempt, connection.next_attempt = True, self.now + 3, self.now + 100
            row = session.get(db.BudgetTransaction, (account.id, "0"))
            row.observed_at, row.api_observed_at = self.now + 4, self.now + 5
        self.assertEqual(self.store.transactions("owner", "2026", limit=1, cursor=first["next_cursor"])["snapshot_digest"],
                         first["snapshot_digest"])
        self.change_transaction("a", "0", remittance_information=["updated search text"])
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=first["next_cursor"]),
                            code="budget_analytics_stale_cursor", status=409)
        fresh = self.store.transactions("owner", "2026", limit=1)
        self.select_account("b", False)
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=fresh["next_cursor"]),
                            code="budget_analytics_stale_cursor", status=409)

    def test_transfer_pagination_is_bound_to_status_currency_and_owner(self):
        self.pair()
        self.add_transaction("c", "second", amount="100", credit_debit_indicator="CRDT")
        first = self.store.transfers("owner", "2026", limit=1)
        self.assertEqual(first["filtered_total"], 2)
        second = self.store.transfers("owner", "2026", limit=1, cursor=first["next_cursor"])
        self.assertIsNone(second["next_cursor"])
        self.assertNotEqual(first["transfers"][0]["credit"]["account_id"], second["transfers"][0]["credit"]["account_id"])
        self.assert_invalid(lambda: self.store.transfers("owner", "2026", status="accepted", limit=1, cursor=first["next_cursor"]))
        self.assertEqual(self.store.transfers("owner", "2026", currency="EUR")["filtered_total"], 0)


class ValidationTests(ProjectionCase):
    def test_pair_defence_cannot_accept_missing_ineligible_zero_or_incompatible_legs(self):
        self.pair()
        with self.sessions() as session:
            snapshot = projection_snapshot(session, "owner")
        first = next(row for row in snapshot["transactions"] if row["entry_reference"] == "debit")
        second = next(row for row in snapshot["transactions"] if row["entry_reference"] == "credit")
        self.assertTrue(views._valid_pair(first, second))
        for left, right in ((None, second), (first, None), (first | {"eligible": False}, second),
                            (first, second | {"eligible": False}), (first, first),
                            (first | {"payload": first["payload"] | {"amount": "0"}}, second),
                            (first, second | {"payload": second["payload"] | {"currency": "EUR"}}),
                            (first, second | {"payload": second["payload"] | {"booking_date": "2026-12-31"}})):
            self.assertFalse(views._valid_pair(left, right))

    def test_corrupt_canonical_reference_is_not_dropped_from_the_matching_graph(self):
        self.add_transaction("a", "x" * 1025, amount="100")
        self.add_transaction("b", "credit", amount="100", credit_debit_indicator="CRDT")
        self.assert_invalid(lambda: self.store.overview("owner", "2026"))

    def test_authenticated_cursor_outside_snapshot_and_invalid_payload_are_refused(self):
        self.add_transaction("a", "one")
        response = self.store.transactions("owner", "2026")
        filters = {"owner": "owner", "operation": "transactions", "period": "2026", "currency": None,
                   "category_id": None, "kind": None, "query": None, "limit": 50}
        context = views.core.projection_digest(filters)
        outside = self.store._cursor_encode(context, response["snapshot_digest"], 2, self.now)
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", cursor=outside))
        original = {"context": context, "snapshot": response["snapshot_digest"], "index": 0,
                    "issued": self.now, "expires": self.now + 900}
        for payload in ([], original | {"index": True}, original | {"index": -1}, original | {"extra": 1},
                        original | {"issued": "private"}, original | {"expires": float("inf")},
                        original | {"expires": self.now + 901}, original | {"issued": self.now + 1}):
            nonce = b"synthetic-iv"
            token = base64.urlsafe_b64encode(nonce + self.store._cipher.encrypt(
                nonce, json.dumps(payload).encode(), views._DOMAIN)).rstrip(b"=").decode()
            self.assert_invalid(lambda: self.store.transactions("owner", "2026", cursor=token))

    def test_invalid_filters_and_row_limits_fail_without_partial_totals(self):
        for method in (self.store.overview, self.store.transactions, self.store.transfers):
            for value in (None, "2026-13", "2026-1", "1899", True):
                self.assert_invalid(lambda: method("owner", value))
            for value in ("pln", "PLNN", 3, "€"):
                self.assert_invalid(lambda: method("owner", "2026", currency=value))
        for value in ("pln", "PLNN", 3, "€"):
            self.assert_invalid(lambda: self.store.recurring("owner", currency=value))
        for values in ({"kind": "auto"}, {"kind": []}, {"query": 1}, {"query": "x" * 257},
                       {"category_id": []}, {"category_id": ""}, {"category_id": "x" * 129}):
            self.assert_invalid(lambda: self.store.transactions("owner", "2026", **values))
        self.assert_invalid(lambda: self.store.transfers("owner", "2026", status="all"))
        for limit in (True, 0, 201, "1"):
            self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=limit))
        self.assertEqual((views.MAX_ROWS, views.MAX_EDGES), (100_000, 100_000))
        for index in range(3):
            self.add_transaction("a", str(index))
        with mock.patch.object(views, "MAX_ROWS", 2):
            self.assert_invalid(lambda: self.store.overview("owner", "2026"), code="budget_analytics_limit", status=413)

    def test_corrupt_ledger_and_metadata_never_yield_partial_figures(self):
        self.add_transaction("a", "debit")
        original = self.transaction().payload_json
        invalid = ("[]", "{", '{"amount":NaN}', '{"amount":"1","amount":"2"}',
                   json.dumps(json.loads(original) | {"remittance_information": "private"}),
                   json.dumps(json.loads(original) | {"remittance_information": [1]}),
                   json.dumps(json.loads(original) | {"booking_date": "2026-02-30"}),
                   json.dumps(json.loads(original) | {"transaction_date": 3}))
        for value in invalid:
            with self.sessions.begin() as session:
                session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = value
            self.assert_invalid(lambda: self.store.overview("owner", "2026"))
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "debit")).payload_json = original
        self.metadata("a", "debit", {})
        for value in ({"raw": "private"}, {"creditor_name": 3}, {"debtor_accounts": {}},
                      {"creditor_accounts": [{}]}, {"creditor_accounts": [{"scheme": "", "identification": "x"}]}):
            with self.sessions.begin() as session:
                session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit")).payload_json = json.dumps(value)
            self.assert_invalid(lambda: self.store.transactions("owner", "2026"))

    def test_corrupt_annotation_category_and_decision_are_fixed_errors(self):
        self.pair()
        self.annotation()
        for mode, kind in (("invalid", "auto"), ("manual", "invalid"), ("manual", "income")):
            with self.sessions.begin() as session:
                row = session.get(db.BudgetAnnotation, (self.accounts["a"], "debit"))
                row.mode, row.kind = mode, kind
            self.assert_invalid(lambda: self.store.transactions("owner", "2026"))
        foreign = self.store.create_category("other", "Private category")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAnnotation, (self.accounts["a"], "debit"))
            row.mode, row.kind, row.category_id = "manual", "auto", foreign["id"]
        self.assert_invalid(lambda: self.store.overview("owner", "2026"))
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetAnnotation, (self.accounts["a"], "debit")))
        saved = self.decide()
        for field, value in (("source", "private"), ("status", "private"), ("credit_account_id", self.accounts["foreign"])):
            with self.sessions.begin() as session:
                row = session.get(db.BudgetTransferDecision, saved["id"])
                old = getattr(row, field)
                # The last case changes the referenced tuple together to remain a valid FK.
                if field == "credit_account_id":
                    session.add(db.BudgetTransaction(account_id=value, entry_reference="credit", payload_json=self.transaction("b", "credit").payload_json,
                                observed_at=self.now, source="api", uncertain=False))
                    session.flush()
                setattr(row, field, value)
            self.assert_invalid(lambda: self.store.transfers("owner", "2026"))
            with self.sessions.begin() as session:
                setattr(session.get(db.BudgetTransferDecision, saved["id"]), field, old)


class RecurringProjectionTests(ProjectionCase):
    def test_recurring_hints_respect_currency_transfers_exclusions_selection_cache_and_bounds(self):
        # 25 monthly charges on PLN via structured counterparty_name -> evidence capped to latest 24.
        for index in range(25):
            year = 2024 + (index // 12)
            month = (index % 12) + 1
            ref = f"sub-pln-{index:02d}"
            self.add_transaction("a", ref, amount="49.90", currency="PLN",
                                 booking_date=f"{year:04d}-{month:02d}-10",
                                 remittance_information=["ignored fallback"])
            self.metadata("a", ref, {"creditor_name": "Synthetic Stream"})
        # 3 monthly charges on EUR via remittance description fallback.
        for month in ("2026-09-05", "2026-10-05", "2026-11-05"):
            self.add_transaction("a", f"sub-eur-{month}", amount="9.99", currency="EUR",
                                 booking_date=month, remittance_information=["Synthetic Cloud"])
        # 3 monthly charges that will be accepted as own transfers -> must not produce hints.
        for month in ("2026-09-12", "2026-10-12", "2026-11-12"):
            self.add_transaction("a", f"xfer-d-{month}", amount="500", booking_date=month,
                                 remittance_information=["Own move"])
            self.add_transaction("b", f"xfer-c-{month}", amount="500", credit_debit_indicator="CRDT",
                                 booking_date=month, remittance_information=["Own move"])
            self.decide(debit=("a", f"xfer-d-{month}"), credit=("b", f"xfer-c-{month}"))
        # 3 monthly charges marked excluded -> must not produce hints.
        for month in ("2026-09-18", "2026-10-18", "2026-11-18"):
            ref = f"excl-{month}"
            self.add_transaction("a", ref, amount="15", booking_date=month, remittance_information=["Excluded plan"])
            self.annotation(account="a", reference=ref, kind="excluded")
        # Zero amount (including signed -0.00) must not fail recurring or emit a hint.
        self.add_transaction("a", "zero-signed", amount="-0.00", currency="PLN",
                             booking_date="2026-11-11", remittance_information=["Zero fee"])
        # 3 monthly charges on deselected account -> must not produce hints.
        for month in ("2026-09-20", "2026-10-20", "2026-11-20"):
            self.add_transaction("c", f"desel-{month}", amount="30", booking_date=month,
                                 remittance_information=["Deselected plan"])
        self.select_account("c", False)

        result = self.store.recurring("owner")
        api_contract.BudgetAnalyticsRecurring.model_validate(result)
        self.assertEqual((result["total_hints"], result["truncated"], len(result["hints"])), (2, False, 2))
        eur_hint, pln_hint = result["hints"]
        self.assertEqual((eur_hint["currency"], eur_hint["label"], eur_hint["label_source"],
                          eur_hint["amount"], eur_hint["occurrence_count"], eur_hint["evidence_truncated"]),
                         ("EUR", "Synthetic Cloud", "description", "9.99", 3, False))
        self.assertEqual((pln_hint["currency"], pln_hint["label"], pln_hint["label_source"],
                          pln_hint["amount"], pln_hint["occurrence_count"], pln_hint["distinct_months"],
                          pln_hint["first_date"], pln_hint["last_date"], pln_hint["evidence_truncated"]),
                         ("PLN", "Synthetic Stream", "counterparty_name", "49.9", 25, 25,
                          "2024-01-10", "2026-01-10", True))
        self.assertEqual(len(pln_hint["evidence"]), views.MAX_RECURRING_EVIDENCE)
        self.assertEqual((pln_hint["evidence"][0]["date"], pln_hint["evidence"][-1]["date"]),
                         ("2024-02-10", "2026-01-10"))
        self.assertNotIn("fingerprint", json.dumps(result))

        # Second call hits cached _recurring tuple on the same committed snapshot digest.
        with mock.patch.object(views, "_recurring_hints", side_effect=AssertionError("expected cached recurring")):
            eur_only = self.store.recurring("owner", currency="EUR")
        self.assertEqual((eur_only["snapshot_digest"], eur_only["total_hints"], len(eur_only["hints"])),
                         (result["snapshot_digest"], 1, 1))
        self.assertEqual(eur_only["hints"][0]["currency"], "EUR")

        with mock.patch.object(views, "MAX_RECURRING_HINTS", 1):
            capped = self.store.recurring("owner")
        self.assertEqual((capped["total_hints"], capped["truncated"], len(capped["hints"])), (2, True, 1))

    def test_corrupt_long_creditor_name_fails_closed_in_recurring(self):
        self.add_transaction("a", "bad-meta", amount="10", booking_date="2026-11-10")
        self.metadata("a", "bad-meta", {"creditor_name": "x" * 1025})
        self.assert_invalid(lambda: self.store.recurring("owner"))


class ForecastProjectionTests(ProjectionCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 22, 0, 1, tzinfo=timezone.utc).timestamp()

    def add_hint(self, label, *, account="a", amount="15", currency="PLN", day=15):
        for month in (8, 9, 10):
            self.add_transaction(account, f"{label}-{month}", amount=amount, currency=currency,
                                 booking_date=f"2026-{month:02d}-{day:02d}", remittance_information=[label])

    def add_schedule(self, label, **overrides):
        defaults = {"label": label, "amount": "2000", "currency": "PLN", "due_day": 15,
                    "start_date": "2026-08-01", "remind_days_before": 5,
                    "destination_kind": "owner_inbox", "enabled": True, "expected_version": 0}
        return self.store.save_mortgage_schedule("owner", **(defaults | overrides))

    def test_forecast_reuses_untruncated_projection_and_reads_one_sqlite_snapshot(self):
        self.add_hint("Synthetic plan A")
        self.add_hint("Synthetic plan B")
        with mock.patch.object(views, "MAX_RECURRING_HINTS", 1):
            recurring = self.store.recurring("owner")
        self.assertEqual((recurring["total_hints"], len(recurring["hints"]), recurring["truncated"]), (2, 1, True))
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.split()[0].upper())

        def read_clock():
            self.assertIn("BEGIN", statements)
            self.assertTrue(views._read_lock.locked())
            return self.now

        self.store.clock = mock.Mock(side_effect=read_clock)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with mock.patch.object(views, "projection_snapshot", side_effect=AssertionError("projection cache missed")), \
                 mock.patch.object(views, "_recurring_hints", side_effect=AssertionError("recurring cache missed")):
                result = self.store.forecast("owner")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.store.clock.assert_called_once_with()
        self.assertEqual(statements.count("BEGIN"), 1)
        self.assertEqual(set(statements), {"BEGIN", "SELECT"})
        self.assertEqual(result["snapshot_digest"], recurring["snapshot_digest"])
        self.assertEqual((result["evaluation_date"], result["evaluation_timezone"], result["horizon_months"]),
                         ("2026-11-22", "UTC", 3))
        self.assertEqual([row["month"] for row in result["months"]], ["2026-11", "2026-12", "2027-01"])
        self.assertEqual([item["label"] for item in result["months"][0]["items"]],
                         ["Synthetic plan A", "Synthetic plan B"])
        api_contract.BudgetAnalyticsForecast.model_validate(result)

    def test_forecast_currency_scopes_money_and_warnings_but_keeps_owner_versions(self):
        scheduled = self.add_schedule("Synthetic PLN mortgage")
        disabled = self.add_schedule("Synthetic inactive PLN mortgage", enabled=False)
        self.add_schedule("Synthetic EUR mortgage", currency="EUR", start_date="2026-11-01", amount="810")
        self.add_hint("Synthetic matching mortgage", amount="2000")
        self.add_hint("Synthetic lapsed subscription", amount="20", day=7)
        self.add_hint("Synthetic stale subscription", account="b", amount="30", day=7)
        self.add_hint("Synthetic EUR subscription", amount="9.99", currency="EUR", day=27)
        self.select_account("d", False)
        with self.sessions.begin() as session:
            session.query(db.BudgetTransaction).filter_by(account_id=self.accounts["b"]).update(
                {"observed_at": self.now - 8 * 86400})
        category = self.store.create_category("owner", "Synthetic limits")
        self.store.save_spending_limit("owner", category_id=category["id"], currency="EUR", monthly_limit="5000",
                                       warning_percent=80, enabled=True, expected_version=0)

        unfiltered = self.store.forecast("owner")
        eur_only = self.store.forecast("owner", months="1", currency="EUR")
        self.assertEqual(unfiltered["summary"], {
            "selected_accounts": 3, "excluded_accounts": 1, "stale_accounts": 2,
            "overdue_mortgage_schedule_count": 1, "lapsed_recurring_count": 1,
            "deduped_recurring_count": 1, "stale_recurring_count": 1,
        })
        self.assertEqual(eur_only["summary"], {
            "selected_accounts": 3, "excluded_accounts": 1, "stale_accounts": 2,
            "overdue_mortgage_schedule_count": 0, "lapsed_recurring_count": 0,
            "deduped_recurring_count": 0, "stale_recurring_count": 0,
        })
        for field in ("stale_account_ids", "stale_account_ids_truncated", "ledger_as_of", "freshness_stale",
                      "snapshot_digest", "schedules_version"):
            self.assertEqual(unfiltered[field], eur_only[field], field)
        self.assertEqual(eur_only["schedules_version"], self.store.mortgages("owner")["schedules_version"])
        self.assertEqual(unfiltered["deduplications"], [{"schedule_id": scheduled["id"],
            "schedule_label": scheduled["label"], "recurring_label": "Synthetic matching mortgage", "amount": "2000",
            "currency": "PLN", "matched_due_date": "2026-10-15", "last_observed_date": "2026-10-15",
            "day_distance": 0, "account_stale": False}])
        self.assertEqual(eur_only["deduplications"], [])
        self.assertEqual([item["currency"] for item in eur_only["months"][0]["items"]], ["EUR", "EUR"])
        self.assertEqual(eur_only["totals"], [{"currency": "EUR", "total": "819.99",
                                             "scheduled_total": "810", "estimated_total": "9.99"}])
        self.assertEqual(eur_only["months"][0]["currencies"], eur_only["totals"])

        self.store.set_mortgage_payment("owner", schedule_id=disabled["id"], month="2027-01", paid=True,
                                       expected_version=disabled["version"])
        after = self.store.forecast("owner", months="1", currency="EUR")
        self.assertNotEqual(after["schedules_version"], eur_only["schedules_version"])
        self.assertEqual(after, eur_only | {"schedules_version": after["schedules_version"]})

    def test_forecast_account_timestamps_use_only_selected_certain_tx_and_transaction_components(self):
        self.select_account("d", False)
        for account, reference in (("a", "older"), ("b", "latest"), ("c", "stale"), ("c", "uncertain"),
                                   ("d", "excluded"), ("foreign", "foreign")):
            self.add_transaction(account, reference)
        with self.sessions.begin() as session:
            for account, reference, age in (("a", "older", 10), ("b", "latest", 6), ("c", "stale", 8),
                                            ("c", "uncertain", -1), ("d", "excluded", -1), ("foreign", "foreign", -1)):
                transaction = session.get(db.BudgetTransaction, (self.accounts[account], reference))
                transaction.observed_at = self.now - age * 86400
                transaction.uncertain = reference == "uncertain"
            for account, operation, age in (("a", "transactions", 7), ("b", "transactions", 8), ("c", "balances", -1)):
                component = session.get(db.BudgetComponent, (self.accounts[account], operation))
                component.last_success = self.now - age * 86400
        result = self.store.forecast("owner")
        self.assertEqual(result["ledger_as_of"], "2026-11-16")
        self.assertEqual(result["stale_account_ids"], [self.accounts["c"]])
        self.assertTrue(result["freshness_stale"])
        with self.sessions.begin() as session:
            component = session.get(db.BudgetComponent, (self.accounts["a"], "transactions"))
            component.last_success = self.now + 86400
        after = self.store.forecast("owner")
        self.assertEqual(after["ledger_as_of"], after["evaluation_date"])
        self.assertEqual(after["stale_account_ids"], result["stale_account_ids"])
        self.assertEqual(after["snapshot_digest"], result["snapshot_digest"])

    def test_forecast_orphan_paid_mark_returns_stored_data_error(self):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                connection.execute(db.BudgetMortgagePayment.__table__.insert(), {
                    "schedule_id": "absent", "month": "2026-11", "owner": "owner", "marked_at": self.now,
                })
                connection.commit()
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        self.assert_invalid(lambda: self.store.forecast("owner"), code="budget_analytics_data_error", status=500)
        self.assert_invalid(lambda: self.store.forecast("owner", months="13"))

    def test_forecast_malformed_component_timestamp_returns_stored_data_error(self):
        self.add_transaction("a", "valid-observation")
        with self.sessions.begin() as session:
            session.execute(text("UPDATE budget_components SET last_success = 'bad timestamp' "
                                 "WHERE account_id = :account_id AND operation = 'transactions'"),
                            {"account_id": self.accounts["a"]})
        self.assert_invalid(lambda: self.store.forecast("owner"), code="budget_analytics_data_error", status=500)

    def _power_category(self, amounts, *, currency="PLN", prefix="power", category=None):
        if category is None:
            category = self.store.create_category("owner", f"Synthetic {prefix}")
        for month, amount in amounts:
            reference = f"{prefix}-{month}"
            self.add_transaction("a", reference, amount=amount, currency=currency,
                                 booking_date=f"2026-{month:02d}-04",
                                 remittance_information=[f"Synthetic {prefix} {month}"])
            self.annotation(account="a", reference=reference, category_id=category["id"], kind="expense")
        return category

    def test_forecast_category_baseline_is_added_from_one_snapshot(self):
        category = self._power_category(tuple((month, str(amount)) for month, amount in
                                              zip(range(5, 11), (10, 20, 30, 40, 50, 60))))
        config = json.dumps({"owner": {"category_ids": [category["id"]], "history_months": 6, "estimator": "median"}})
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.split()[0].upper())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with mock.patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: config}):
                result = self.store.forecast("owner", months="1")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(statements.count("BEGIN"), 1)
        self.assertEqual(set(statements), {"BEGIN", "SELECT"})
        api_contract.BudgetAnalyticsForecast.model_validate(result)
        item = result["months"][0]["items"][0]
        self.assertEqual(item["source"], "category_baseline")
        self.assertEqual(item["amount"], "35")
        self.assertEqual(item["confidence"], "estimated")
        self.assertIsNone(item["schedule_id"])
        self.assertEqual(item["label"], "Synthetic power")
        self.assertFalse(item["account_stale"])
        self.assertEqual(result["deduplications"], [])
        self.assertEqual(result["summary"]["deduped_recurring_count"], 0)
        with self.sessions.begin() as session:
            session.query(db.BudgetTransaction).filter_by(account_id=self.accounts["a"]).update(
                {"observed_at": self.now - 60 * 86400})
            component = session.get(db.BudgetComponent, (self.accounts["a"], "transactions"))
            if component is not None:
                component.last_success = self.now - 60 * 86400
        with mock.patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: config}):
            stale = self.store.forecast("owner", months="1")
        self.assertTrue(stale["months"][0]["items"][0]["account_stale"])

    def test_forecast_category_baseline_conflict_and_estimator_and_series_cap(self):
        category = self._power_category(tuple((month, str(amount)) for month, amount in
                                              zip(range(5, 11), (10, 20, 30, 40, 50, 60))))
        self._power_category(tuple((month, "8") for month in range(5, 11)), currency="EUR", prefix="euro",
                             category=category)
        bad = json.dumps({"owner": {"category_ids": [category["id"]], "estimator": ["median"]}})
        with mock.patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: bad}):
            with self.assertRaises(BudgetConfigError) as unavailable:
                self.store.forecast("owner")
        self.assertNotIsInstance(unavailable.exception, forecast.ForecastConfigConflict)
        self.assertEqual(getattr(unavailable.exception, "code", "budget_unavailable"), "budget_unavailable")

        ready = json.dumps({"owner": {"category_ids": [category["id"]], "history_months": 6}})
        with mock.patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: ready}), \
             mock.patch.object(forecast, "MAX_BASELINE_SERIES", 1):
            self.assert_invalid(lambda: self.store.forecast("owner", months="1"),
                                code="budget_analytics_limit", status=413)
            scoped = self.store.forecast("owner", months="1", currency="EUR")
        self.assertEqual([item["source"] for item in scoped["months"][0]["items"]], ["category_baseline"])
        self.assertEqual(scoped["months"][0]["items"][0]["currency"], "EUR")

        self.add_schedule("Synthetic power mortgage", amount="10", due_day=4, start_date="2026-01-01")
        with mock.patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: ready}):
            with self.assertRaises(forecast.ForecastConfigConflict) as conflict:
                self.store.forecast("owner", months="1", currency="EUR")
        self.assertEqual(conflict.exception.code, "budget_forecast_config_conflict")
        self.assertNotIn(category["id"], str(conflict.exception))


class MonthlyBudgetProjectionTests(ProjectionCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).timestamp()

    def test_monthly_budget_reuses_projection_cache_and_reads_one_sqlite_snapshot(self):
        salary = self.store.create_category("owner", "Wynagrodzenie")
        food = self.store.create_category("owner", "Zakupy")
        self.add_transaction("a", "sal-nov", amount="10000", credit_debit_indicator="CRDT", booking_date="2026-11-01")
        self.annotation(account="a", reference="sal-nov", category_id=salary["id"], kind="income")
        self.add_transaction("a", "exp-nov", amount="200", booking_date="2026-11-05", transaction_date="2026-11-05")
        self.annotation(account="a", reference="exp-nov", category_id=food["id"], kind="expense")
        cfg = json.dumps({"owner": {"income_category_ids": [salary["id"]]}})

        self.store.overview("owner", "2026-11")
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.split()[0].upper())

        def read_clock():
            self.assertIn("BEGIN", statements)
            self.assertTrue(views._read_lock.locked())
            return self.now

        self.store.clock = mock.Mock(side_effect=read_clock)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: cfg}), \
                 mock.patch.object(views, "projection_snapshot", side_effect=AssertionError("projection cache missed")):
                result = self.store.monthly_budget("owner")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.store.clock.assert_called_once_with()
        self.assertEqual(statements.count("BEGIN"), 1)
        self.assertEqual(set(statements), {"BEGIN", "SELECT"})
        self.assertEqual(result["summary"], {"start_budget": "10000", "spent": "200", "remaining": "9800"})
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(result)

    def test_monthly_budget_income_window_from_owner_config(self):
        salary = self.store.create_category("owner", "Wynagrodzenie")
        self.add_transaction("a", "sal-oct", amount="5000", credit_debit_indicator="CRDT", booking_date="2026-10-30")
        self.annotation(account="a", reference="sal-oct", category_id=salary["id"], kind="income")
        self.add_transaction("a", "sal-nov", amount="5500", credit_debit_indicator="CRDT", booking_date="2026-11-26")
        self.annotation(account="a", reference="sal-nov", category_id=salary["id"], kind="income")
        self.now = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc).timestamp()
        cfg = json.dumps({"owner": {"income_category_ids": [salary["id"]], "income_window_start_day": 20}})

        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: cfg}):
            result = self.store.monthly_budget("owner")

        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(result)
        self.assertEqual(result["configuration"], {
            "status": "ready", "source": "bootstrap",
            "income_window": {"start": "2026-10-20", "end": "2026-11-19", "start_day": 20},
        })
        self.assertEqual(result["summary"]["start_budget"], "5000")

    def test_monthly_budget_card_fallback_requires_confirmed_account_kind(self):
        self.add_transaction("a", "card-purchase", amount="125.25", booking_date="2026-11-10",
                             transaction_date="2026-11-10")
        with self.sessions.begin() as session:
            account = session.get(db.BudgetAccount, self.accounts["a"])
            account.kind, account.kind_confirmed_at = "credit_card", None
        cfg = json.dumps({"owner": {"income_category_ids": []}})
        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: cfg}):
            unconfirmed = self.store.monthly_budget("owner")
            with self.sessions.begin() as session:
                session.get(db.BudgetAccount, self.accounts["a"]).kind_confirmed_at = self.now
            confirmed = self.store.monthly_budget("owner")

        self.assertEqual(unconfirmed["summary"]["spent"], "125.25")
        self.assertEqual(confirmed["summary"], unconfirmed["summary"])
        self.assertEqual((unconfirmed["coverage"]["pending_transfer_rows"], unconfirmed["coverage"]["provisional"]),
                         (1, True))
        self.assertEqual((confirmed["coverage"]["pending_transfer_rows"], confirmed["coverage"]["provisional"]),
                         (0, False))

    def test_ledger_as_of_offline_imports_empty_live_sync_and_stale_counts(self):
        # Empty/unresolved-only offline import leaves ledger_as_of None and all 4 selected accounts stale.
        with self.sessions.begin() as session:
            batch = db.BudgetImportBatch(
                id="b-empty", fingerprint="fp-empty", account_id=self.accounts["a"],
                fetched_at=self.now - 100, imported_at=self.now, report_json="{}",
            )
            session.add(batch)
            session.flush()
            session.add(db.BudgetImportUnresolved(batch_id="b-empty", ordinal=0, reason="non_canonical", payload_json="{}"))
        empty_res = self.store.monthly_budget("owner")
        self.assertIsNone(empty_res["coverage"]["ledger_as_of"])
        self.assertEqual(empty_res["coverage"]["stale_selected_accounts"], 4)

        # Usable offline row sets observed_at = fetched_at even when imported_at happened later.
        fetched_at = self.now - 3600
        with self.sessions.begin() as session:
            session.add(db.BudgetImportBatch(
                id="b-usable", fingerprint="fp-usable", account_id=self.accounts["a"],
                fetched_at=fetched_at, imported_at=self.now, report_json="{}",
            ))
            session.add(db.BudgetTransaction(
                account_id=self.accounts["a"], entry_reference="off-usable",
                payload_json=json.dumps({"amount": "12", "currency": "PLN", "credit_debit_indicator": "DBIT",
                                         "status": "BOOK", "booking_date": "2026-11-15", "value_date": None,
                                         "transaction_date": None, "remittance_information": ["Offline"]}),
                observed_at=fetched_at, source="offline_import", uncertain=False, api_observed_at=None,
            ))
        offline_res = self.store.monthly_budget("owner", period="2026-01", currency="EUR")
        self.assertEqual(offline_res["coverage"]["ledger_as_of"], fetched_at)
        self.assertEqual(offline_res["coverage"]["stale_selected_accounts"], 3)

        # Empty successful live fetch on account b sets component.last_success -> updates ledger_as_of and stale count.
        with self.sessions.begin() as session:
            comp = session.get(db.BudgetComponent, (self.accounts["b"], "transactions"))
            comp.last_success = self.now - 60
        live_empty = self.store.monthly_budget("owner")
        self.assertEqual(live_empty["coverage"]["ledger_as_of"], self.now - 60)
        self.assertEqual(live_empty["coverage"]["stale_selected_accounts"], 2)

    def test_end_to_end_rules_passthrough_moved_salary_category_rename_and_error_mappings(self):
        salary = self.store.create_category("owner", "Wynagrodzenie")
        zus = self.store.create_category("owner", "Świadczenia ZUS")
        rent = self.store.create_category("owner", "Najem")
        mortgage = self.store.create_category("owner", "Kredyt")
        groceries = self.store.create_category("owner", "Spożywcze")
        # Previous month salary on account a; current month moved to account b with changed amount!
        self.add_transaction("a", "sal-oct", amount="8000", credit_debit_indicator="CRDT", booking_date="2026-10-10")
        self.annotation(account="a", reference="sal-oct", category_id=salary["id"], kind="income")
        self.metadata("a", "sal-oct", {"debtor_name": "Pracodawca SA"})
        self.add_transaction("b", "sal-nov", amount="8500", credit_debit_indicator="CRDT", booking_date="2026-11-10")
        self.annotation(account="b", reference="sal-nov", category_id=salary["id"], kind="income")
        self.metadata("b", "sal-nov", {"debtor_name": "Pracodawca SA"})
        # ZUS last month (still expected on day 28).
        self.add_transaction("a", "zus-oct", amount="800", credit_debit_indicator="CRDT", booking_date="2026-10-28")
        self.annotation(account="a", reference="zus-oct", category_id=zus["id"], kind="income")
        self.metadata("a", "zus-oct", {"debtor_name": "ZUS"})
        # Mortgage + late rent + krypto excluded + stale excluded manual annotation + category rename/archive.
        self.add_transaction("a", "mort", amount="2500", booking_date="2026-11-05", transaction_date="2026-11-05")
        self.annotation(account="a", reference="mort", category_id=mortgage["id"], kind="expense")
        self.add_transaction("a", "rent", amount="2000", credit_debit_indicator="CRDT", booking_date="2026-11-20")
        self.annotation(account="a", reference="rent", category_id=rent["id"], kind="income")
        self.add_transaction("a", "krypto", amount="400", booking_date="2026-11-07", transaction_date="2026-11-07")
        self.annotation(account="a", reference="krypto", kind="excluded")
        self.add_transaction("a", "groc", amount="150", booking_date="2026-11-08", transaction_date="2026-11-08")
        self.annotation(account="a", reference="groc", category_id=groceries["id"], kind="excluded")
        self.change_transaction("a", "groc", amount="160")  # Stale manual excluded -> falls back to default and warns!
        self.add_transaction("a", "groc-2", amount="40", booking_date="2026-11-09", transaction_date="2026-11-09")
        self.annotation(account="a", reference="groc-2", category_id=groceries["id"], kind="expense")
        self.store.update_category("owner", groceries["id"], name="Jedzenie", archived=True, expected_version=1)
        self.store.save_spending_limit("owner", category_id=mortgage["id"], currency="PLN",
                                       monthly_limit="600", warning_percent=80, enabled=True, expected_version=0)

        cfg = json.dumps({
            "owner": {
                "income_category_ids": [salary["id"], zus["id"]],
                "pass_through": {"rent_category_id": rent["id"], "mortgage_category_id": mortgage["id"]},
            },
        })
        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: cfg}):
            res = self.store.monthly_budget("owner")
            api_contract.BudgetAnalyticsMonthlyBudget.model_validate(res)
            self.assertEqual(res["summary"], {"start_budget": "8500", "spent": "700", "remaining": "7800"})
            self.assertEqual(res["pass_through"]["uncovered_mortgage"], "500")
            self.assertEqual(res["expected_income"], {
                "status": "estimated", "total": "800",
                "items": [{"category_id": zus["id"], "amount": "800", "expected_date": "2026-11-28", "source": "previous_month"}],
            })
            self.assertEqual(res["coverage"]["pending_transfer_rows"], 1)
            self.assertTrue(res["coverage"]["provisional"])
            by_cat = {row["category_id"]: row for row in res["category_spend"]}
            self.assertEqual((by_cat[groceries["id"]]["name"], by_cat[groceries["id"]]["archived"],
                              by_cat[groceries["id"]]["net_spending"]), ("Jedzenie", True, "40"))
            self.assertEqual(by_cat[None]["net_spending"], "160")
            self.assertEqual(by_cat[mortgage["id"]]["limit"]["status"], "approaching")

            # Future period returns 400 even before projection.
            self.assert_invalid(lambda: self.store.monthly_budget("owner", period="2026-12"))

            # Aggregate bound violation returns 413.
            with mock.patch.object(month_budget, "MAX_AGGREGATE_CHARS", 2):
                self.assert_invalid(lambda: self.store.monthly_budget("owner"), code="budget_analytics_limit", status=413)

            # Corrupt limit amount -> 500 budget_analytics_data_error.
            with self.sessions.begin() as session:
                session.execute(text("UPDATE budget_category_limits SET monthly_limit = 'bad'"))
            self.assert_invalid(lambda: self.store.monthly_budget("owner"), code="budget_analytics_data_error", status=500)

            with self.sessions.begin() as session:
                session.execute(text("UPDATE budget_category_limits SET monthly_limit = '600'"))
                session.execute(text("UPDATE budget_components SET last_success = 'bad timestamp' "
                                     "WHERE account_id = :account_id AND operation = 'transactions'"),
                                {"account_id": self.accounts["a"]})
            self.assert_invalid(lambda: self.store.monthly_budget("owner"), code="budget_analytics_data_error", status=500)

            with mock.patch.object(self.store, "clock", return_value=float("nan")):
                self.assert_invalid(lambda: self.store.monthly_budget("owner"),
                                    code="budget_analytics_unavailable", status=503)

        # Foreign category in HESTIA_BUDGET_MONTH_CONFIG -> BudgetConfigError (503 budget_unavailable).
        foreign = self.store.create_category("other", "Foreign")
        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: json.dumps({"owner": {"income_category_ids": [foreign["id"]]}})}):
            with self.assertRaises(BudgetConfigError):
                self.store.monthly_budget("owner")

    def test_monthly_budget_charts_single_snapshot_cache_identity_and_income_window(self):
        salary = self.store.create_category("owner", "Wynagrodzenie")
        rent = self.store.create_category("owner", "Najem")
        mortgage = self.store.create_category("owner", "Kredyt")
        food = self.store.create_category("owner", "Zakupy")
        self.add_transaction("a", "sal-oct-late", amount="5000", credit_debit_indicator="CRDT", booking_date="2026-10-28")
        self.annotation(account="a", reference="sal-oct-late", category_id=salary["id"], kind="income")
        self.add_transaction("a", "sal-nov-late", amount="5500", credit_debit_indicator="CRDT", booking_date="2026-11-26")
        self.annotation(account="a", reference="sal-nov-late", category_id=salary["id"], kind="income")
        self.add_transaction("a", "mort-nov", amount="2200", booking_date="2026-11-05", transaction_date="2026-11-05")
        self.annotation(account="a", reference="mort-nov", category_id=mortgage["id"], kind="expense")
        self.add_transaction("a", "rent-nov", amount="1800", credit_debit_indicator="CRDT", booking_date="2026-11-20")
        self.annotation(account="a", reference="rent-nov", category_id=rent["id"], kind="income")
        self.add_transaction("a", "food-nov", amount="120.5", booking_date="2026-11-10", transaction_date="2026-11-10")
        self.annotation(account="a", reference="food-nov", category_id=food["id"], kind="expense")
        self.add_transaction("a", "food-sep", amount="90", booking_date="2026-09-08", transaction_date="2026-09-08")
        self.annotation(account="a", reference="food-sep", category_id=food["id"], kind="expense")
        self.now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc).timestamp()

        cfg = json.dumps({
            "owner": {
                "income_category_ids": [salary["id"]],
                "income_window_start_day": 20,
                "pass_through": {"rent_category_id": rent["id"], "mortgage_category_id": mortgage["id"]},
            },
        })
        self.store.overview("owner", "2026-11")
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.split()[0].upper())

        def read_clock():
            self.assertIn("BEGIN", statements)
            self.assertTrue(views._read_lock.locked())
            return self.now

        self.store.clock = mock.Mock(side_effect=read_clock)
        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: cfg}), \
                 mock.patch.object(views, "projection_snapshot", side_effect=AssertionError("projection cache missed")):
                charts = self.store.monthly_budget_charts("owner", history="12")
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)

        self.store.clock.assert_called_once_with()
        self.assertEqual(statements.count("BEGIN"), 1)
        self.assertEqual(set(statements), {"BEGIN", "SELECT"})
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(charts)

        budget = charts["budget"]
        series = charts["series"]
        self.assertEqual(budget["summary"], {"start_budget": "5000", "spent": "520.5", "remaining": "4479.5"})
        self.assertEqual(len(series["daily"]), 28)
        self.assertEqual(series["daily"][-1]["cumulative_spent"], "520.5")
        self.assertEqual(len(series["months"]), 12)
        nov_bucket = series["months"][-1]
        self.assertEqual(nov_bucket["period"], "2026-11")
        self.assertEqual(nov_bucket["start_budget"], "5000")
        self.assertEqual(nov_bucket["spent"], "520.5")
        self.assertEqual(month_budget.core.sum_money(c["net_spending"] for c in nov_bucket["categories"]), "520.5")
        self.assertEqual(series["months"][-3]["period"], "2026-09")
        self.assertEqual(series["months"][-3]["spent"], "90")
        self.assert_invalid(lambda: self.store.monthly_budget_charts("owner", history="3"))

    def test_monthly_budget_equals_charts_budget_on_same_snapshot_and_clock(self):
        salary = self.store.create_category("owner", "Wynagrodzenie")
        rent = self.store.create_category("owner", "Najem")
        mortgage = self.store.create_category("owner", "Kredyt")
        food = self.store.create_category("owner", "Zakupy")
        self.add_transaction("a", "sal-prior", amount="5000", credit_debit_indicator="CRDT", booking_date="2026-09-26")
        self.annotation(account="a", reference="sal-prior", category_id=salary["id"], kind="income")
        self.metadata("a", "sal-prior", {"debtor_name": "Pracodawca"})
        self.add_transaction("a", "sal-curr", amount="5200", credit_debit_indicator="CRDT", booking_date="2026-10-26")
        self.annotation(account="a", reference="sal-curr", category_id=salary["id"], kind="income")
        self.metadata("a", "sal-curr", {"debtor_name": "Pracodawca"})
        self.add_transaction("a", "mort-nov", amount="2100", booking_date="2026-11-05", transaction_date="2026-11-05")
        self.annotation(account="a", reference="mort-nov", category_id=mortgage["id"], kind="expense")
        self.add_transaction("a", "rent-nov", amount="1800", credit_debit_indicator="CRDT", booking_date="2026-11-15")
        self.annotation(account="a", reference="rent-nov", category_id=rent["id"], kind="income")
        self.add_transaction("a", "food-nov", amount="75", booking_date="2026-11-10", transaction_date="2026-11-10")
        self.annotation(account="a", reference="food-nov", category_id=food["id"], kind="expense")
        self.store.save_spending_limit("owner", category_id=food["id"], currency="PLN",
                                       monthly_limit="500", warning_percent=80, enabled=True, expected_version=0)

        self.store.clock = mock.Mock(return_value=self.now)
        unconfigured_budget = self.store.monthly_budget("owner")
        unconfigured_charts = self.store.monthly_budget_charts("owner", history="12")
        self.assertEqual(unconfigured_charts["budget"], unconfigured_budget)
        self.assertIsNone(unconfigured_charts["series"])

        cfg = json.dumps({
            "owner": {
                "income_category_ids": [salary["id"]],
                "income_window_start_day": 20,
                "pass_through": {"rent_category_id": rent["id"], "mortgage_category_id": mortgage["id"]},
            },
        })
        with mock.patch.dict(os.environ, {month_budget.MONTH_CONFIG_ENV: cfg}):
            single = self.store.monthly_budget("owner", period="2026-11", currency="PLN")
            charts = self.store.monthly_budget_charts("owner", period="2026-11", currency="PLN", history="12")
        self.assertEqual(charts["budget"], single)
        self.assertEqual(charts["series"]["daily"][-1]["cumulative_spent"], single["summary"]["spent"])


class SpendingLimitsProjectionTests(ProjectionCase):
    def test_monthly_evaluation_limits_version_cache_independence_currency_and_archived(self):
        food = self.store.create_category("owner", "Food")
        old = self.store.create_category("owner", "Archived Cat")
        self.add_transaction("a", "f1", amount="85.50", currency="PLN", booking_date="2026-11-05")
        self.add_transaction("a", "f2", amount="5.50", currency="PLN", credit_debit_indicator="CRDT",
                             booking_date="2026-11-06")
        self.add_transaction("a", "e1", amount="60", currency="EUR", booking_date="2026-11-07")
        self.annotation(account="a", reference="f1", category_id=food["id"], kind="expense")
        self.annotation(account="a", reference="f2", category_id=food["id"], kind="refund")
        self.annotation(account="a", reference="e1", category_id=food["id"], kind="expense")

        lim_pln = self.store.save_spending_limit("owner", category_id=food["id"], currency="PLN",
                                                 monthly_limit="100", warning_percent=80, enabled=True, expected_version=0)
        self.store.save_spending_limit("owner", category_id=food["id"], currency="EUR",
                                       monthly_limit="50", warning_percent=80, enabled=False, expected_version=0)
        self.store.save_spending_limit("owner", category_id=old["id"], currency="PLN",
                                       monthly_limit="200", warning_percent=80, enabled=True, expected_version=0)
        # Archiving category leaves enabled limit active and visible with archived=True even at 0 spend.
        self.store.update_category("owner", old["id"], name="Archived Cat", archived=True, expected_version=1)

        first = self.store.spending_limits("owner", "2026-11")
        api_contract.BudgetAnalyticsSpendingLimits.model_validate(first)
        self.assertEqual((len(first["limits"]), len(first["statuses"])), (3, 2))
        by_cat = {row["category_id"]: row for row in first["statuses"]}
        self.assertEqual((by_cat[food["id"]]["net_spending"], by_cat[food["id"]]["remaining"],
                          by_cat[food["id"]]["status"], by_cat[food["id"]]["archived"], by_cat[food["id"]]["name"]),
                         ("80", "20", "approaching", False, "Food"))
        self.assertEqual((by_cat[old["id"]]["net_spending"], by_cat[old["id"]]["status"],
                          by_cat[old["id"]]["archived"], by_cat[old["id"]]["name"]),
                         ("0", "within", True, "Archived Cat"))

        # Currency filter filters limits & statuses while preserving owner-wide limits_version.
        eur_only = self.store.spending_limits("owner", "2026-11", currency="EUR")
        self.assertEqual((len(eur_only["limits"]), eur_only["statuses"], eur_only["limits_version"]),
                         (1, [], first["limits_version"]))

        # Updating a limit changes limits_version without changing snapshot_digest or recomputing projection.
        self.store.save_spending_limit("owner", category_id=food["id"], currency="PLN",
                                       monthly_limit="75", warning_percent=80, enabled=True,
                                       expected_version=lim_pln["version"])
        with mock.patch.object(views, "projection_snapshot", side_effect=AssertionError("projection cache should hold")):
            second = self.store.spending_limits("owner", "2026-11")
        self.assertEqual(second["snapshot_digest"], first["snapshot_digest"])
        self.assertNotEqual(second["limits_version"], first["limits_version"])
        self.assertEqual(next(r for r in second["statuses"] if r["category_id"] == food["id"])["status"], "exceeded")

        # Strict monthly period validation (rejects YYYY) and invalid currency.
        for bad_period in ("2026", "2026-13", 202611, None):
            self.assert_invalid(lambda: self.store.spending_limits("owner", bad_period))
        self.assert_invalid(lambda: self.store.spending_limits("owner", "2026-11", currency="pln"))

    def test_corrupt_limit_amount_or_foreign_category_fails_closed(self):
        cat = self.store.create_category("owner", "Food")
        saved = self.store.save_spending_limit("owner", category_id=cat["id"], currency="PLN",
                                               monthly_limit="100", warning_percent=80, enabled=True, expected_version=0)
        with self.sessions.begin() as session:
            session.get(db.BudgetCategoryLimit, saved["id"]).monthly_limit = "not-a-decimal"
        self.assert_invalid(lambda: self.store.spending_limits("owner", "2026-11"))
        other_cat = self.store.create_category("other", "Foreign")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetCategoryLimit, saved["id"])
            row.monthly_limit = "100"
            row.category_id = other_cat["id"]
        self.assert_invalid(lambda: self.store.spending_limits("owner", "2026-11"))


if __name__ == "__main__":
    unittest.main()
