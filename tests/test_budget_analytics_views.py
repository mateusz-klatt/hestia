"""Synthetic, owner-private reports: exact arithmetic, durable decisions and snapshot fences."""
from __future__ import annotations

import base64
import json
import unittest
from unittest import mock
import uuid

from sqlalchemy import delete, event

from hestia import api_contract, db
from hestia.budget import analytics_views as views
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_views import BudgetAnalyticsViews, projection_snapshot
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
        self.assertEqual((transactions["filtered_total"], transfers["filtered_total"]), (0, 0))
        self.assertIsNone(transactions["next_cursor"])
        api_contract.BudgetAnalyticsOverview.model_validate(overview)
        api_contract.BudgetAnalyticsTransactions.model_validate(transactions)
        api_contract.BudgetAnalyticsTransfers.model_validate(transfers)

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


class TransferProjectionTests(ProjectionCase):
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


if __name__ == "__main__":
    unittest.main()
