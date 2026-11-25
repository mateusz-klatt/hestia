"""Synthetic counterparty-account rules; no real bank identifiers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import unittest
from unittest import mock

from hestia import api_contract, db
from hestia.budget import analytics_views
from hestia.budget.analytics_core import (AnalyticsError, canonical_counterparty_account, compile_rules, counterparty_account_evidence,
    counterparty_account_key, counterparty_key_id, counterparty_rule_from_identification, counterparty_rule_parts,
    counterparty_rule_value, first_matching_rule, mask_counterparty_account, match_rule, match_texts,
    normalize_counterparty_account, rule_matches)
from hestia.budget.bank_metadata import _aliases
from tests.test_budget_analytics_core import identity, payload
from tests.test_budget_analytics_views import ProjectionCase


SYNTHETIC_IBAN = "PL" + "0" * 22 + "1234"
OTHER_IBAN = "PL" + "0" * 22 + "5678"
# 26-digit NRB whose IBAN is PL plus those digits (ISO 13616), not a second pair of check digits.
POLISH_BBAN = "04" + "0" * 24
POLISH_IBAN = "PL" + POLISH_BBAN
SECRET = b"synthetic-only-32-byte-rule-key-value"


def spaced(value):
    lowered = value.lower()
    return " ".join(lowered[index:index + 4] for index in range(0, len(lowered), 4))


def account_rule(value=SYNTHETIC_IBAN, **changes):
    return {"enabled": True, "kind": "auto", "field": "counterparty_account", "operator": "equals",
            "value": counterparty_rule_from_identification(value, SECRET, owner="owner"), **changes}


class CounterpartyAccountCoreTests(unittest.TestCase):
    def test_normalisation_mask_hmac_and_stored_display_round_trip(self):
        self.assertEqual(normalize_counterparty_account(spaced(SYNTHETIC_IBAN)), SYNTHETIC_IBAN)
        self.assertEqual(normalize_counterparty_account(spaced(SYNTHETIC_IBAN).replace(" ", "-")), SYNTHETIC_IBAN)
        self.assertEqual(mask_counterparty_account(spaced(SYNTHETIC_IBAN)), "PL** … 1234")
        key = counterparty_account_key(spaced(SYNTHETIC_IBAN), SECRET, owner="owner")
        self.assertEqual(key, counterparty_account_key(SYNTHETIC_IBAN, SECRET, owner="owner"))
        self.assertNotEqual(key, counterparty_account_key(SYNTHETIC_IBAN, b"another-synthetic-secret", owner="owner"))
        self.assertNotEqual(key, counterparty_account_key(SYNTHETIC_IBAN, SECRET, owner="other"))
        stored = counterparty_rule_from_identification(spaced(SYNTHETIC_IBAN), SECRET, owner="owner")
        key_id = counterparty_key_id(SECRET)
        self.assertEqual(counterparty_rule_parts(stored), (key_id, key, "PL** … 1234"))
        self.assertEqual(counterparty_rule_value(key_id, key, "PL** … 1234"), stored)
        self.assertLessEqual(len(stored), 256)
        self.assertNotIn(SYNTHETIC_IBAN, stored)

    def test_polish_bban_aliases_and_canonical_schemes_deduplicate_without_collisions(self):
        self.assertEqual(len(POLISH_IBAN), 28)
        self.assertEqual(_aliases({"scheme": "BBAN", "identification": POLISH_BBAN}, "PL"),
                         [{"scheme": "IBAN", "identification": POLISH_IBAN}])
        self.assertEqual(canonical_counterparty_account(POLISH_BBAN, "BBAN"), ("IBAN", POLISH_IBAN))
        self.assertEqual(canonical_counterparty_account(POLISH_BBAN), ("IBAN", POLISH_IBAN))
        self.assertEqual(canonical_counterparty_account(POLISH_IBAN, "IBAN"), ("IBAN", POLISH_IBAN))
        self.assertEqual(canonical_counterparty_account("0" * 26, "BBAN"), ("BBAN", "0" * 26))
        self.assertEqual(canonical_counterparty_account("0" * 26), ("OTHER", "0" * 26))
        self.assertEqual(_aliases({"scheme": "BBAN", "identification": "0" * 26}, "PL"), [])
        self.assertEqual(counterparty_account_key(POLISH_BBAN, SECRET, "BBAN", owner="owner"),
                         counterparty_account_key(POLISH_IBAN, SECRET, "IBAN", owner="owner"))
        self.assertNotEqual(counterparty_account_key("ABCDEF1234", SECRET, "BBAN", owner="owner"),
                            counterparty_account_key("ABCDEF1234", SECRET, "OTHER", owner="owner"))
        self.assertEqual(counterparty_account_key(SYNTHETIC_IBAN, SECRET, "OTHER", owner="owner"),
                         counterparty_account_key(SYNTHETIC_IBAN, SECRET, owner="owner"))
        with self.assertRaises(AnalyticsError):
            canonical_counterparty_account("ABCDEF1234", "not a scheme")

    def test_short_identification_masks_reveal_only_two_characters(self):
        for length in range(6, 12):
            value = "PL" + "0" * (length - 4) + "12"
            with self.subTest(length=length):
                self.assertEqual(len(value), length)
                self.assertEqual(mask_counterparty_account(value), "** … 12")
                stored = counterparty_rule_from_identification(value, SECRET, owner="owner")
                self.assertEqual(counterparty_rule_parts(stored)[2], "** … 12")
        self.assertEqual(mask_counterparty_account("PL" + "0" * 8 + "12"), "PL** … 0012")

    def test_helper_validation_is_fixed_and_value_free(self):
        for value in (None, "", "PL12", "PL!123456", "PL" + "0" * 130, 123456):
            with self.subTest(value=value), self.assertRaises(AnalyticsError) as raised:
                normalize_counterparty_account(value)
            self.assertEqual(str(raised.exception), "budget_analytics_invalid_request")
        for secret in (None, b"", "synthetic-only"):
            with self.subTest(secret=secret), self.assertRaises(AnalyticsError) as raised:
                counterparty_account_key(SYNTHETIC_IBAN, secret, owner="owner")
            self.assertEqual((raised.exception.code, raised.exception.status), ("budget_analytics_data_error", 500))
            with self.assertRaises(AnalyticsError):
                counterparty_key_id(secret)
        for owner in (None, ""):
            with self.subTest(owner=owner), self.assertRaises(AnalyticsError):
                counterparty_account_key(SYNTHETIC_IBAN, SECRET, owner=owner)
        with self.assertRaises(AnalyticsError):
            match_texts(payload(), {}, account_secret=SECRET)
        for key_id, key, masked in ((None, "a" * 64, "PL** … 1234"), ("g" * 16, "a" * 64, "PL** … 1234"),
                                    ("0" * 16, None, "PL** … 1234"), ("0" * 16, "A" * 64, "PL** … 1234"),
                                    ("0" * 16, "a" * 64, SYNTHETIC_IBAN), ("0" * 16, "a" * 64, None)):
            with self.subTest(masked=masked), self.assertRaises(AnalyticsError):
                counterparty_rule_value(key_id, key, masked)
        for stored in (None, SYNTHETIC_IBAN, "cpacct:v1:broken", "cpacct:v2:broken"):
            with self.subTest(stored=stored), self.assertRaises(AnalyticsError):
                counterparty_rule_parts(stored)

    def test_equals_matches_dbit_and_crdt_with_space_case_and_scheme_normalisation(self):
        rule = account_rule()
        debit = payload()
        credit = payload(credit_debit_indicator="CRDT")
        debit_metadata = {"creditor_accounts": [{"scheme": "OTHER", "identification": spaced(SYNTHETIC_IBAN)}],
                          "debtor_accounts": [identity(OTHER_IBAN)]}
        credit_metadata = {"debtor_accounts": [identity(SYNTHETIC_IBAN.lower())],
                           "creditor_accounts": [identity(OTHER_IBAN)]}
        self.assertTrue(rule_matches(rule, debit, debit_metadata, account_secret=SECRET, owner="owner"))
        self.assertTrue(rule_matches(rule, credit, credit_metadata, account_secret=SECRET, owner="owner"))
        self.assertFalse(rule_matches(rule, debit, {"creditor_accounts": [identity(OTHER_IBAN)]}, account_secret=SECRET, owner="owner"))
        self.assertFalse(rule_matches(rule, credit, {"creditor_accounts": [identity(SYNTHETIC_IBAN)]},
                                      account_secret=SECRET, owner="owner"))
        compiled, safe = compile_rules([rule])
        self.assertTrue(safe)
        self.assertIs(first_matching_rule(compiled, debit, match_texts(debit, debit_metadata, account_secret=SECRET, owner="owner")), rule)
        with self.assertRaises(AnalyticsError):
            rule_matches(rule | {"operator": "contains"}, debit, debit_metadata, account_secret=SECRET, owner="owner")
        self.assertFalse(rule_matches(rule, debit, debit_metadata))

    def test_unavailable_or_rotated_key_does_not_match_and_scanning_continues(self):
        account = account_rule()
        description = {"enabled": True, "kind": "expense", "field": "description", "operator": "contains",
                       "value": "shop", "id": "description"}
        debit = payload()
        metadata = {"creditor_accounts": [identity(SYNTHETIC_IBAN)]}
        compiled, safe = compile_rules([account, description])
        self.assertTrue(safe)
        missing = match_texts(debit, metadata)
        self.assertIsNone(missing["counterparty_account"])
        self.assertIs(first_matching_rule(compiled, debit, missing), description)
        self.assertFalse(rule_matches(account, debit, metadata))
        rotated_secret = b"another-synthetic-persistent-key-32b"
        rotated = match_texts(debit, metadata, account_secret=rotated_secret, owner="owner")
        self.assertIs(first_matching_rule(compiled, debit, rotated), description)
        self.assertFalse(rule_matches(account, debit, metadata, account_secret=rotated_secret, owner="owner"))
        stored_key = counterparty_rule_parts(account["value"])[1]
        forged = dict(rotated)
        forged["counterparty_account"] = ("f" * 16, frozenset({stored_key}))
        self.assertIs(first_matching_rule(compiled, debit, forged), description)
        present = match_texts(debit, metadata, account_secret=SECRET, owner="owner")
        self.assertIs(first_matching_rule(compiled, debit, present), account)

    def test_matching_uses_any_valid_account_and_ignores_non_iban_like_identifiers(self):
        metadata = {"creditor_accounts": [identity("x"), identity(OTHER_IBAN), identity(SYNTHETIC_IBAN)]}
        evidence = counterparty_account_evidence(payload(), metadata, SECRET, owner="owner")
        self.assertEqual(len(evidence), 2)
        self.assertTrue(rule_matches(account_rule(), payload(), metadata, account_secret=SECRET, owner="owner"))
        malformed = account_rule() | {"value": "cpacct:v2:broken"}
        compiled, safe = compile_rules([malformed])
        self.assertFalse(safe)
        with self.assertRaises(AnalyticsError):
            first_matching_rule(compiled, payload(), match_texts(payload(), metadata, account_secret=SECRET, owner="owner"))

    def test_expense_counterparty_account_rule_matches_crdt_debtor_side_as_refund(self):
        exp_rule = account_rule(kind="expense", id="acct-exp", priority=1)
        disabled_exp = account_rule(kind="expense", id="acct-disabled", enabled=False, priority=0)
        credit = payload(credit_debit_indicator="CRDT", remittance_information=["Refund"])
        credit_debtor = {"debtor_accounts": [identity(SYNTHETIC_IBAN.lower())], "creditor_accounts": [identity(OTHER_IBAN)]}
        credit_wrong_side = {"creditor_accounts": [identity(SYNTHETIC_IBAN)], "debtor_accounts": [identity(OTHER_IBAN)]}
        self.assertTrue(rule_matches(exp_rule, credit, credit_debtor, account_secret=SECRET, owner="owner"))
        self.assertFalse(rule_matches(exp_rule, credit, credit_wrong_side, account_secret=SECRET, owner="owner"))
        self.assertFalse(rule_matches(disabled_exp, credit, credit_debtor, account_secret=SECRET, owner="owner"))

        compiled, safe = compile_rules([disabled_exp, exp_rule])
        self.assertTrue(safe)
        texts = match_texts(credit, credit_debtor, account_secret=SECRET, owner="owner")
        self.assertEqual(match_rule(compiled, credit, texts), (exp_rule, "refund"))

        # Income rule in Pass 1 beats expense counterparty_account rule in Pass 2.
        inc_rule = {"id": "desc-inc", "enabled": True, "kind": "income", "field": "description",
                    "operator": "contains", "value": "refund", "priority": 100}
        both, _ = compile_rules([exp_rule, inc_rule])
        self.assertEqual(match_rule(both, credit, texts), (inc_rule, "income"))


class CounterpartyAccountProjectionTests(ProjectionCase):
    def metadata_for(self, account, reference, field, value):
        self.metadata(account, reference, {field: [identity(value)], "creditor_name": None, "debtor_name": None, "mcc": None})

    def test_preview_and_save_match_exactly_the_account_without_raw_identifier_output(self):
        for reference in ("debit-target", "debit-formatted", "debit-other"):
            self.add_transaction("a", reference)
        self.add_transaction("b", "credit-target", credit_debit_indicator="CRDT")
        self.add_transaction("b", "credit-wrong-side", credit_debit_indicator="CRDT")
        self.metadata_for("a", "debit-target", "creditor_accounts", SYNTHETIC_IBAN)
        self.metadata_for("a", "debit-formatted", "creditor_accounts", spaced(SYNTHETIC_IBAN))
        self.metadata_for("a", "debit-other", "creditor_accounts", OTHER_IBAN)
        self.metadata_for("b", "credit-target", "debtor_accounts", SYNTHETIC_IBAN.lower())
        self.metadata_for("b", "credit-wrong-side", "creditor_accounts", SYNTHETIC_IBAN)
        category = self.store.create_category("owner", "Synthetic Electricity")
        candidate = self.candidate(field="counterparty_account", operator="equals", value=spaced(SYNTHETIC_IBAN),
                                   category_id=category["id"], kind="auto")
        preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(preview["affected_count"], 3)
        self.assertEqual({row["entry_reference"] for row in preview["examples"]},
                         {"debit-target", "debit-formatted", "credit-target"})
        saved = self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual((saved["value"], saved["counterparty_account_key"]),
                         ("PL** … 1234", counterparty_account_key(SYNTHETIC_IBAN, SECRET, owner="owner")))
        rules = self.store.rules("owner")
        transactions = self.store.transactions("owner", "2026")
        matched = {row["entry_reference"] for row in transactions["transactions"]
                   if row["effective"]["rule_id"] == saved["id"]}
        self.assertEqual(matched, {"debit-target", "debit-formatted", "credit-target"})
        for body in (rules, preview, transactions, saved):
            serialized = json.dumps(body)
            self.assertNotIn(SYNTHETIC_IBAN, serialized)
            self.assertNotIn(spaced(SYNTHETIC_IBAN), serialized)
        target = next(row for row in transactions["transactions"] if row["entry_reference"] == "debit-target")
        self.assertEqual((target["counterparty_account_key"], target["counterparty_account"]),
                         (saved["counterparty_account_key"], "PL** … 1234"))
        stored_rows = self.rows(db.BudgetRule) + self.rows(db.BudgetRulePreview) + self.rows(db.BudgetAnalyticsAudit)
        durable = json.dumps([getattr(row, "value", None) or getattr(row, "candidate_json", None)
                              or (row.before_json + row.after_json) for row in stored_rows])
        self.assertNotIn(SYNTHETIC_IBAN, durable)
        self.assertNotIn(spaced(SYNTHETIC_IBAN), durable)

    def test_bban_and_iban_aliases_produce_one_row_key_and_match_end_to_end(self):
        self.add_transaction("a", "polish-debit")
        aliases = _aliases({"scheme": "BBAN", "identification": POLISH_BBAN}, "PL")
        self.assertEqual(aliases, [{"scheme": "IBAN", "identification": POLISH_IBAN}])
        formatted_bban = "-".join(POLISH_BBAN[index:index + 4] for index in range(0, len(POLISH_BBAN), 4))
        self.metadata("a", "polish-debit", {"creditor_accounts": [
            {"scheme": "BBAN", "identification": formatted_bban},
            aliases[0],
        ]})
        body = self.store.transactions("owner", "2026")
        row = body["transactions"][0]
        self.assertEqual(row["counterparty_account"], "PL** … 0000")
        self.assertRegex(row["counterparty_account_key"], r"^[0-9a-f]{64}$")
        with self.sessions() as session:
            evidence = self.store._snapshot(session, "owner")["transactions"][0]["_counterparty_accounts"]
        self.assertEqual([key for key, _masked in evidence], [row["counterparty_account_key"]])
        self.assertNotIn(POLISH_BBAN, json.dumps(body))
        self.assertNotIn(POLISH_IBAN, json.dumps(body))
        candidate = self.candidate(field="counterparty_account", operator="equals", value=formatted_bban)
        preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(preview["affected_count"], 1)
        saved = self.store.save_rule("owner", preview["preview_id"])
        row = self.store.transactions("owner", "2026")["transactions"][0]
        self.assertEqual(row["counterparty_account_key"], saved["counterparty_account_key"])
        self.assertEqual((row["effective"]["source"], row["effective"]["rule_id"]), ("rule", saved["id"]))

    def test_key_creation_multi_account_privacy_and_secret_rotation(self):
        self.add_transaction("a", "debit")
        self.metadata_for("a", "debit", "creditor_accounts", SYNTHETIC_IBAN)
        first = self.store.transactions("owner", "2026")["transactions"][0]
        with self.assertRaises(AnalyticsError):
            self.store.preview_rule("owner", self.candidate(field="counterparty_account", operator="equals", value="",
                                                            counterparty_account_key="0" * 64))
        candidate = self.candidate(field="counterparty_account", operator="equals",
                                   value="XX** … 9999", kind="expense",
                                   counterparty_account_key=first["counterparty_account_key"])
        preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(preview["affected_count"], 1)
        saved = self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual(saved["counterparty_account_key"], first["counterparty_account_key"])
        self.assertEqual(saved["value"], "PL** … 1234")
        replay = self.store.save_rule("owner", preview["preview_id"])
        self.assertFalse(replay["changed"])
        with mock.patch.object(self.store, "_snapshot", wraps=self.store._snapshot) as projection:
            batch = self.store.preview_rules("owner", [candidate | {"priority": 200}, candidate | {"priority": 201}])
        self.assertEqual(projection.call_count, 2, "One evidence lookup plus the final preview snapshot per batch")
        self.assertEqual(len(self.store.save_rules("owner", batch["preview_id"])["rules"]), 2)
        rotated = analytics_views.BudgetAnalyticsViews(self.sessions, clock=lambda: self.now,
                                                       cursor_secret=b"synthetic-rotated", rule_key_file=str(self.rule_key_file))
        row = rotated.transactions("owner", "2026")["transactions"][0]
        self.assertEqual((row["effective"]["source"], row["effective"]["rule_id"]), ("rule", saved["id"]))
        with self.sessions.begin() as session:
            metadata = session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit"))
            metadata.payload_json = json.dumps({"creditor_accounts": [identity(SYNTHETIC_IBAN), identity(OTHER_IBAN)]})
        row = self.store.transactions("owner", "2026")["transactions"][0]
        self.assertIsNone(row["counterparty_account_key"])
        self.assertIsNone(row["counterparty_account"])

    def test_expense_account_rule_preview_and_save_matches_crdt_debtor_as_refund(self):
        self.add_transaction("a", "debit-merchant", amount="100", booking_date="2026-11-10")
        self.add_transaction("b", "credit-merchant-refund", amount="40", credit_debit_indicator="CRDT",
                             booking_date="2026-11-10")
        self.add_transaction("c", "credit-wrong-side", amount="20", credit_debit_indicator="CRDT",
                             booking_date="2026-11-10")
        self.metadata_for("a", "debit-merchant", "creditor_accounts", SYNTHETIC_IBAN)
        self.metadata_for("b", "credit-merchant-refund", "debtor_accounts", spaced(SYNTHETIC_IBAN))
        self.metadata_for("c", "credit-wrong-side", "creditor_accounts", SYNTHETIC_IBAN)

        category = self.store.create_category("owner", "Hardware")
        candidate = self.candidate(field="counterparty_account", operator="equals", value=spaced(SYNTHETIC_IBAN),
                                   category_id=category["id"], kind="expense", priority=10)
        preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(preview["affected_count"], 2)
        self.assertEqual({row["entry_reference"] for row in preview["examples"]},
                         {"debit-merchant", "credit-merchant-refund"})
        saved = self.store.save_rule("owner", preview["preview_id"])
        by_ref = {row["entry_reference"]: row["effective"]
                  for row in self.store.transactions("owner", "2026-11")["transactions"]}
        self.assertEqual(by_ref["debit-merchant"],
                         {"kind": "expense", "category_id": category["id"], "source": "rule", "rule_id": saved["id"]})
        self.assertEqual(by_ref["credit-merchant-refund"],
                         {"kind": "refund", "category_id": category["id"], "source": "rule", "rule_id": saved["id"]})
        self.assertEqual(by_ref["credit-wrong-side"],
                         {"kind": "unclassified_credit", "category_id": None, "source": "default", "rule_id": None})

    def test_dedicated_key_is_required_and_rotation_fences_previews(self):
        self.add_transaction("a", "debit")
        self.metadata_for("a", "debit", "creditor_accounts", SYNTHETIC_IBAN)
        missing = analytics_views.BudgetAnalyticsViews(
            self.sessions, clock=lambda: self.now, cursor_secret=b"session-one",
            rule_key_file=str(self.path.parent / "missing-rule.key"))
        ordinary = missing.preview_rule("owner", self.candidate())
        self.assertEqual(ordinary["affected_count"], 1)
        with self.assertRaises(AnalyticsError) as unavailable:
            missing.preview_rule("owner", self.candidate(field="counterparty_account", operator="equals",
                                                         value=SYNTHETIC_IBAN))
        self.assertEqual((unavailable.exception.code, unavailable.exception.status),
                         ("budget_analytics_rule_key_unavailable", 400))
        insecure_path = self.path.parent / "insecure-rule.key"
        insecure_path.write_bytes(SECRET)
        insecure_path.chmod(0o644)
        insecure = analytics_views.BudgetAnalyticsViews(
            self.sessions, clock=lambda: self.now, cursor_secret=b"session-one", rule_key_file=str(insecure_path))
        with self.assertRaises(AnalyticsError) as refused:
            insecure.preview_rule("owner", self.candidate(field="counterparty_account", operator="equals",
                                                          value=SYNTHETIC_IBAN))
        self.assertEqual(refused.exception.status, 400)
        short_path = self.path.parent / "short-rule.key"
        short_path.write_bytes(b"too-short")
        short_path.chmod(0o400)
        insecure = analytics_views.BudgetAnalyticsViews(
            self.sessions, clock=lambda: self.now, cursor_secret=b"session-one", rule_key_file=str(short_path))
        with self.assertRaises(AnalyticsError) as too_short:
            insecure.preview_rule("owner", self.candidate(field="counterparty_account", operator="equals",
                                                          value=SYNTHETIC_IBAN))
        self.assertEqual(too_short.exception.status, 400)

        candidate = self.candidate(field="counterparty_account", operator="equals", value=SYNTHETIC_IBAN)
        stale = self.store.preview_rule("owner", candidate)
        saved = self.store.save_rule("owner", self.store.preview_rule("owner", candidate)["preview_id"])
        self.assertTrue(self.rows(db.BudgetRule)[0].value.startswith("cpacct:v2:" + counterparty_key_id(SECRET) + ":"))
        self.rule_key_file.chmod(0o600)
        self.rule_key_file.write_bytes(b"another-synthetic-persistent-key-32b")
        self.rule_key_file.chmod(0o400)
        with self.assertRaises(AnalyticsError) as stale_save:
            self.store.save_rule("owner", stale["preview_id"])
        self.assertEqual((stale_save.exception.code, stale_save.exception.status),
                         ("budget_analytics_stale_preview", 409))
        body = self.store.transactions("owner", "2026")
        self.assertEqual(body["transactions"][0]["effective"]["source"], "default")
        self.assertIsNone(body["transactions"][0]["effective"]["rule_id"])
        self.assertEqual(self.store.rules("owner")[0]["inactive_reason"], "rule_key_rotated")
        self.assertEqual(self.store.overview("owner", "2026")["period"], "2026")
        self.assertIn("period", self.store.monthly_budget("owner"))
        self.assertEqual(saved["value"], "PL** … 1234")

    def test_missing_or_rotated_key_degrades_only_account_rules(self):
        when = datetime(2026, 11, 30, 12, tzinfo=timezone.utc).timestamp()
        for reference, remittance in (
                ("by-description", ["marker only"]), ("by-name", ["plain text"]), ("by-mcc", ["plain text"]),
                ("by-account", ["plain text"]), ("both", ["marker transfer"])):
            self.add_transaction("a", reference, remittance_information=remittance, booking_date="2026-11-15")
        self.metadata("a", "by-name", {"creditor_name": "Synthetic Counterparty"})
        self.metadata("a", "by-mcc", {"mcc": "5411"})
        self.metadata("a", "by-account", {"creditor_accounts": [identity(SYNTHETIC_IBAN)]})
        self.metadata("a", "both", {"creditor_accounts": [identity(SYNTHETIC_IBAN)]})
        description_category = self.store.create_category("owner", "Synthetic Description")["id"]
        name_category = self.store.create_category("owner", "Synthetic Name")["id"]
        mcc_category = self.store.create_category("owner", "Synthetic Mcc")["id"]
        account_category = self.store.create_category("owner", "Synthetic Account")["id"]

        def save(**changes):
            preview = self.store.preview_rule("owner", self.candidate(**changes))
            saved = self.store.save_rule("owner", preview["preview_id"])
            self.assertNotIn("inactive_reason", saved)
            return saved

        account = save(field="counterparty_account", operator="equals", value=SYNTHETIC_IBAN,
                       category_id=account_category, kind="expense", priority=1)
        description = save(field="description", operator="contains", value="marker",
                           category_id=description_category, kind="expense", priority=20)
        name = save(field="counterparty_name", operator="equals", value="Synthetic Counterparty",
                    category_id=name_category, kind="expense", priority=30)
        mcc = save(field="mcc", operator="equals", value="5411", category_id=mcc_category, kind="expense", priority=40)
        self.assertTrue(all("inactive_reason" not in row for row in self.store.rules("owner")))
        with_key = {row["entry_reference"]: row["effective"]["rule_id"]
                    for row in self.store.transactions("owner", "2026-11")["transactions"]}
        self.assertEqual(with_key["by-account"], account["id"])
        self.assertEqual(with_key["both"], account["id"])
        self.assertEqual(with_key["by-description"], description["id"])
        self.assertEqual(with_key["by-name"], name["id"])
        self.assertEqual(with_key["by-mcc"], mcc["id"])

        def open_views(path):
            return analytics_views.BudgetAnalyticsViews(
                self.sessions, clock=lambda: when, cursor_secret=b"synthetic-only", rule_key_file=str(path))

        def assert_degraded(store, reason):
            overview = store.overview("owner", "2026-11")
            transactions = store.transactions("owner", "2026-11")
            budget = store.monthly_budget("owner", period="2026-11")
            forecast = store.forecast("owner", months="1")
            api_contract.BudgetAnalyticsOverview.model_validate(overview)
            api_contract.BudgetAnalyticsTransactions.model_validate(transactions)
            api_contract.BudgetAnalyticsMonthlyBudget.model_validate(budget)
            api_contract.BudgetAnalyticsForecast.model_validate(forecast)
            self.assertEqual((overview["period"], budget["period"], forecast["horizon_months"]), ("2026-11", "2026-11", 1))
            by_ref = {row["entry_reference"]: row["effective"] for row in transactions["transactions"]}
            self.assertEqual(by_ref["by-description"]["rule_id"], description["id"])
            self.assertEqual(by_ref["by-name"]["rule_id"], name["id"])
            self.assertEqual(by_ref["by-mcc"]["rule_id"], mcc["id"])
            self.assertEqual(by_ref["both"]["rule_id"], description["id"])
            self.assertEqual((by_ref["by-account"]["source"], by_ref["by-account"]["rule_id"]), ("default", None))
            self.assertEqual({row["category_id"] for row in overview["categories"]},
                             {description_category, name_category, mcc_category, None})
            listed = store.rules("owner")
            api_contract.BudgetAnalyticsRules.model_validate({"rules": listed})
            self.assertNotIn(SYNTHETIC_IBAN, json.dumps(listed))
            rows = {row["id"]: row for row in listed}
            self.assertEqual(rows[account["id"]]["inactive_reason"], reason)
            self.assertTrue(rows[account["id"]]["enabled"])
            for saved_rule in (description, name, mcc):
                self.assertNotIn("inactive_reason", rows[saved_rule["id"]])
            return listed

        missing = open_views(self.path.parent / "missing-rule.key")
        assert_degraded(missing, "rule_key_unavailable")
        with self.assertRaises(AnalyticsError) as unavailable:
            missing.preview_rule("owner", self.candidate(field="counterparty_account", operator="equals",
                                                         value=SYNTHETIC_IBAN, category_id=account_category,
                                                         kind="expense", priority=2))
        self.assertEqual((unavailable.exception.code, unavailable.exception.status),
                         ("budget_analytics_rule_key_unavailable", 400))
        other = missing.preview_rule("owner", self.candidate(field="description", operator="contains", value="marker",
                                                            category_id=description_category, kind="expense",
                                                            priority=80))
        self.assertIn("preview_id", other)

        rotated_path = self.path.parent / "rotated-rule.key"
        rotated_path.write_bytes(b"another-synthetic-persistent-key-32b")
        rotated_path.chmod(0o400)
        rotated = open_views(rotated_path)
        assert_degraded(rotated, "rule_key_rotated")
        preview = rotated.preview_rule("owner", self.candidate(
            field="counterparty_account", operator="equals", value=SYNTHETIC_IBAN, category_id=account_category,
            kind="expense", priority=2))
        self.assertEqual(preview["affected_count"], 2)
        created = rotated.save_rule("owner", preview["preview_id"])
        self.assertNotIn("inactive_reason", created)
        fresh = {row["entry_reference"]: row["effective"]["rule_id"]
                 for row in rotated.transactions("owner", "2026-11")["transactions"]}
        self.assertEqual(fresh["by-account"], created["id"])
        self.assertEqual(fresh["both"], created["id"])
        self.assertEqual(fresh["by-description"], description["id"])
        self.assertEqual(fresh["by-name"], name["id"])
        self.assertEqual(fresh["by-mcc"], mcc["id"])
        listed = {row["id"]: row for row in rotated.rules("owner")}
        self.assertEqual(listed[account["id"]]["inactive_reason"], "rule_key_rotated")
        self.assertNotIn("inactive_reason", listed[created["id"]])
        self.assertEqual({row["category_id"] for row in rotated.overview("owner", "2026-11")["categories"]},
                         {description_category, name_category, mcc_category, account_category})

    def test_classify_false_skips_evidence_and_fallbacks_use_dedicated_context(self):
        self.add_transaction("a", "debit")
        self.metadata_for("a", "debit", "creditor_accounts", SYNTHETIC_IBAN)
        candidate = self.candidate(field="counterparty_account", operator="equals", value=SYNTHETIC_IBAN)
        saved = self.store.save_rule("owner", self.store.preview_rule("owner", candidate)["preview_id"])
        key_id = counterparty_key_id(SECRET)
        with mock.patch.object(analytics_views.core, "counterparty_account_evidence",
                               side_effect=AssertionError("classify=False computed account evidence")):
            with self.sessions() as session:
                bare = analytics_views.projection_snapshot(session, "owner", classify=False, account_secret=SECRET,
                                                           account_key_id=key_id)
        with self.sessions() as session:
            full = analytics_views.projection_snapshot(session, "owner", account_secret=SECRET,
                                                       account_key_id=key_id)
        self.assertEqual(bare["digest"], full["digest"])
        self.assertTrue(all(not row["_counterparty_accounts"] for row in bare["transactions"]))
        stripped = [{key: value for key, value in row.items() if key != "_match_texts"}
                    for row in full["transactions"]]
        proposed = candidate | {"id": "proposed", "kind": "excluded", "value": self.rows(db.BudgetRule)[0].value}
        affected, _ = self.store._proposed(stripped, [proposed], account_secret=SECRET,
                                           account_key_id=key_id, owner="owner")
        self.assertEqual(affected, 1)
        row = full["transactions"][0]
        stored_rule = saved | {"value": self.rows(db.BudgetRule)[0].value}
        effective = analytics_views._effective(row["payload"], row["metadata"], row["annotation"], {}, [stored_rule],
                                               account_secret=SECRET, account_key_id=key_id, owner="owner")
        self.assertEqual((effective["source"], effective["rule_id"]), ("rule", saved["id"]))

    def test_validation_and_malformed_storage_fail_closed(self):
        invalid = (
            self.candidate(field="counterparty_account", operator="contains", value=SYNTHETIC_IBAN),
            self.candidate(field="counterparty_account", operator="equals", value=None),
            self.candidate(field="counterparty_account", operator="equals", value="A" * 257),
            self.candidate(field="counterparty_account", operator="equals", value="x"),
            self.candidate(field="counterparty_account", operator="equals",
                           value=counterparty_rule_from_identification(SYNTHETIC_IBAN, SECRET, owner="owner")),
            self.candidate(field="counterparty_account", operator="equals", value="PL** … 1234",
                           counterparty_account_key="A" * 64),
            self.candidate(field="counterparty_account", operator="equals", value="",
                           counterparty_account_key="0" * 64),
            self.candidate(field="counterparty_account", operator="equals",
                           value=counterparty_rule_from_identification(SYNTHETIC_IBAN, SECRET, owner="owner"),
                           counterparty_account_key="0" * 64),
            self.candidate(field="counterparty_account", operator="equals",
                           value=counterparty_rule_from_identification(SYNTHETIC_IBAN, SECRET, owner="owner"),
                           counterparty_account_key=123),
            self.candidate(field="description", counterparty_account_key="a" * 64),
        )
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(AnalyticsError) as raised:
                self.store.preview_rule("owner", candidate)
            self.assertEqual(raised.exception.status, 400)
        with self.sessions.begin() as session:
            session.add(db.BudgetRule(id="broken", owner="owner", field="counterparty_account", operator="equals",
                        value=SYNTHETIC_IBAN, category_id=None, kind="auto", priority=1, enabled=True, version=1,
                        created_at=self.now, updated_at=self.now))
        with self.assertRaises(AnalyticsError) as malformed:
            self.store.rules("owner")
        self.assertEqual(malformed.exception.status, 500)

    def test_leading_whitespace_cannot_smuggle_a_stored_account_value(self):
        missing = analytics_views.BudgetAnalyticsViews(
            self.sessions, clock=lambda: self.now, cursor_secret=b"session-one",
            rule_key_file=str(self.path.parent / "missing-rule.key"))
        forged = counterparty_rule_value("ab" * 8, "cd" * 32, "PL** … 9999")
        for prefix in (" ", "\t"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(AnalyticsError) as raised:
                    missing.preview_rule("owner", self.candidate(
                        field="counterparty_account", operator="equals", value=prefix + forged))
                self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.rows(db.BudgetRule), [])
        self.assertEqual(self.rows(db.BudgetRulePreview), [])

    def save_account_edit(self, current, *, store=None, **changes):
        store = self.store if store is None else store
        candidate = self.candidate(
            rule_id=current["id"], expected_version=current["version"], field="counterparty_account",
            operator="equals", value=current["value"], counterparty_account_key=current["counterparty_account_key"],
            category_id=current["category_id"], kind=current["kind"], priority=current["priority"],
            enabled=current["enabled"])
        candidate.update(changes)
        return store.save_rule("owner", store.preview_rule("owner", candidate)["preview_id"])

    def test_inert_account_rules_can_be_disabled_and_edited(self):
        self.add_transaction("a", "debit")
        self.metadata_for("a", "debit", "creditor_accounts", SYNTHETIC_IBAN)
        category = self.store.create_category("owner", "Synthetic Kept")["id"]

        def create(priority):
            preview = self.store.preview_rule("owner", self.candidate(
                field="counterparty_account", operator="equals", value=SYNTHETIC_IBAN, kind="expense",
                priority=priority))
            return self.store.save_rule("owner", preview["preview_id"])

        rotated_rule = create(10)
        unavailable_rule = create(11)
        stored = {row.id: row.value for row in self.rows(db.BudgetRule)}
        with self.sessions.begin() as session:
            metadata = session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit"))
            metadata.payload_json = json.dumps({"creditor_accounts": []})
        unmatched = self.save_account_edit(unavailable_rule, priority=4)
        self.assertEqual(unmatched["priority"], 4)
        self.assertEqual(unmatched["counterparty_account_key"], unavailable_rule["counterparty_account_key"])
        self.assertNotIn("inactive_reason", self.store.rules("owner")[0])

        self.rule_key_file.chmod(0o600)
        self.rule_key_file.write_bytes(b"another-synthetic-persistent-key-32b")
        self.rule_key_file.chmod(0o400)
        reprioritised = self.save_account_edit(unmatched, priority=3, category_id=category)
        self.assertEqual(reprioritised["category_id"], category)
        self.assertEqual(reprioritised["priority"], 3)
        disabled = self.save_account_edit(rotated_rule, enabled=False)
        self.assertFalse(disabled["enabled"])
        listed = {row["id"]: row for row in self.store.rules("owner")}
        self.assertEqual(listed[disabled["id"]]["inactive_reason"], "rule_key_rotated")
        self.assertFalse(listed[disabled["id"]]["enabled"])
        self.assertEqual(listed[reprioritised["id"]]["inactive_reason"], "rule_key_rotated")
        self.assertEqual({row.id: row.value for row in self.rows(db.BudgetRule)}, stored)

        missing = analytics_views.BudgetAnalyticsViews(
            self.sessions, clock=lambda: self.now, cursor_secret=b"session-one",
            rule_key_file=str(self.path.parent / "missing-rule.key"))
        unavailable = self.save_account_edit(reprioritised, store=missing, enabled=False)
        self.assertFalse(unavailable["enabled"])
        listed = {row["id"]: row for row in missing.rules("owner")}
        self.assertEqual(listed[unavailable["id"]]["inactive_reason"], "rule_key_unavailable")
        self.assertEqual(listed[disabled["id"]]["inactive_reason"], "rule_key_unavailable")
        self.assertEqual({row.id: row.value for row in self.rows(db.BudgetRule)}, stored)
        with self.assertRaises(AnalyticsError) as changed_account:
            missing.preview_rule("owner", self.candidate(
                rule_id=unavailable["id"], expected_version=unavailable["version"], field="counterparty_account",
                operator="equals", value=OTHER_IBAN, category_id=category, kind="expense", priority=3, enabled=False))
        self.assertEqual((changed_account.exception.code, changed_account.exception.status),
                         ("budget_analytics_rule_key_unavailable", 400))
        with self.assertRaises(AnalyticsError) as changed_key:
            missing.preview_rule("owner", self.candidate(
                rule_id=unavailable["id"], expected_version=unavailable["version"], field="counterparty_account",
                operator="equals", value=unavailable["value"], counterparty_account_key="ab" * 32,
                category_id=category, kind="expense", priority=3, enabled=False))
        self.assertEqual(changed_key.exception.status, 400)
        digest = unavailable["counterparty_account_key"]
        mask = unavailable["value"]
        for bad in (OTHER_IBAN, "XX** … 9999"):
            with self.subTest(value=bad), self.assertRaises(AnalyticsError) as raised:
                missing.preview_rule("owner", self.candidate(
                    rule_id=unavailable["id"], expected_version=unavailable["version"], field="counterparty_account",
                    operator="equals", value=bad, counterparty_account_key=digest, category_id=category,
                    kind="expense", priority=3, enabled=False))
            self.assertEqual(raised.exception.status, 400)
        self.assertEqual({row.id: row.value for row in self.rows(db.BudgetRule)}, stored)

        def edit(priority, **changes):
            candidate = self.candidate(
                rule_id=unavailable["id"], expected_version=changes.pop("expected_version"), field="counterparty_account",
                operator="equals", counterparty_account_key=digest, category_id=category, kind="expense",
                priority=priority, enabled=False)
            candidate.update(changes)
            return missing.save_rule("owner", missing.preview_rule("owner", candidate)["preview_id"])

        repeated = edit(2, expected_version=unavailable["version"], value=mask)
        self.assertEqual((repeated["priority"], repeated["value"], repeated["counterparty_account_key"]), (2, mask, digest))
        blank = edit(1, expected_version=repeated["version"], value="")
        self.assertEqual((blank["priority"], blank["counterparty_account_key"]), (1, digest))
        omitted = self.candidate(
            rule_id=unavailable["id"], expected_version=blank["version"], field="counterparty_account",
            operator="equals", counterparty_account_key=digest, category_id=category, kind="expense",
            priority=0, enabled=False)
        del omitted["value"]
        omitted_saved = missing.save_rule("owner", missing.preview_rule("owner", omitted)["preview_id"])
        self.assertEqual((omitted_saved["priority"], omitted_saved["value"], omitted_saved["counterparty_account_key"]),
                         (0, mask, digest))
        self.assertEqual({row.id: row.value for row in self.rows(db.BudgetRule)}, stored)
        self.assertEqual({row.id: row.enabled for row in self.rows(db.BudgetRule)},
                         {disabled["id"]: False, unavailable["id"]: False})

    def test_description_rule_can_become_an_account_rule(self):
        self.add_transaction("a", "debit")
        self.metadata_for("a", "debit", "creditor_accounts", SYNTHETIC_IBAN)
        description = self.store.save_rule("owner", self.store.preview_rule("owner", self.candidate())["preview_id"])
        account_key = self.store.transactions("owner", "2026")["transactions"][0]["counterparty_account_key"]
        saved = self.save_account_edit(description | {"value": "PL** … 1234", "counterparty_account_key": account_key,
                                                      "operator": "equals"}, field="counterparty_account")
        self.assertEqual((saved["field"], saved["counterparty_account_key"]), ("counterparty_account", account_key))

    def test_batch_of_one_hundred_previews_saves_and_replays(self):
        candidates = [self.candidate(field="counterparty_account", operator="equals", value=spaced(SYNTHETIC_IBAN),
                                     priority=index) for index in range(100)]
        preview = self.store.preview_rules("owner", candidates)
        self.assertEqual(preview["rule_count"], 100)
        saved = self.store.save_rules("owner", preview["preview_id"])
        self.assertEqual(len(saved["rules"]), 100)
        self.assertTrue(all(row["value"] == "PL** … 1234" for row in saved["rules"]))
        replay = self.store.save_rules("owner", preview["preview_id"])
        self.assertEqual(len(replay["rules"]), 100)
        self.assertFalse(replay["changed"])


if __name__ == "__main__":
    unittest.main()
