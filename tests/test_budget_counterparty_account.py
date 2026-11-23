"""Synthetic counterparty-account rules; no real bank identifiers."""
from __future__ import annotations

import json
import unittest

from hestia import db
from hestia.budget import analytics_views
from hestia.budget.analytics_core import (AnalyticsError, compile_rules, counterparty_account_evidence,
    counterparty_account_key, counterparty_rule_from_identification, counterparty_rule_parts,
    counterparty_rule_value, first_matching_rule, mask_counterparty_account, match_texts,
    normalize_counterparty_account, rule_matches)
from tests.test_budget_analytics_core import identity, payload
from tests.test_budget_analytics_views import ProjectionCase


SYNTHETIC_IBAN = "PL" + "0" * 22 + "1234"
OTHER_IBAN = "PL" + "0" * 22 + "5678"
SECRET = b"synthetic-only"


def spaced(value):
    lowered = value.lower()
    return " ".join(lowered[index:index + 4] for index in range(0, len(lowered), 4))


def account_rule(value=SYNTHETIC_IBAN, **changes):
    return {"enabled": True, "kind": "auto", "field": "counterparty_account", "operator": "equals",
            "value": counterparty_rule_from_identification(value, SECRET), **changes}


class CounterpartyAccountCoreTests(unittest.TestCase):
    def test_normalisation_mask_hmac_and_stored_display_round_trip(self):
        self.assertEqual(normalize_counterparty_account(spaced(SYNTHETIC_IBAN)), SYNTHETIC_IBAN)
        self.assertEqual(mask_counterparty_account(spaced(SYNTHETIC_IBAN)), "PL** … 1234")
        key = counterparty_account_key(spaced(SYNTHETIC_IBAN), SECRET)
        self.assertEqual(key, counterparty_account_key(SYNTHETIC_IBAN, SECRET))
        self.assertNotEqual(key, counterparty_account_key(SYNTHETIC_IBAN, b"another-synthetic-secret"))
        stored = counterparty_rule_from_identification(spaced(SYNTHETIC_IBAN), SECRET)
        self.assertEqual(counterparty_rule_parts(stored), (key, "PL** … 1234"))
        self.assertEqual(counterparty_rule_value(key, "PL** … 1234"), stored)
        self.assertLessEqual(len(stored), 256)
        self.assertNotIn(SYNTHETIC_IBAN, stored)

    def test_helper_validation_is_fixed_and_value_free(self):
        for value in (None, "", "PL12", "PL-123456", "PL" + "0" * 130, 123456):
            with self.subTest(value=value), self.assertRaises(AnalyticsError) as raised:
                normalize_counterparty_account(value)
            self.assertEqual(str(raised.exception), "budget_analytics_invalid_request")
        for secret in (None, b"", "synthetic-only"):
            with self.subTest(secret=secret), self.assertRaises(AnalyticsError) as raised:
                counterparty_account_key(SYNTHETIC_IBAN, secret)
            self.assertEqual((raised.exception.code, raised.exception.status), ("budget_analytics_data_error", 500))
        for key, masked in ((None, "PL** … 1234"), ("A" * 64, "PL** … 1234"),
                            ("a" * 64, SYNTHETIC_IBAN), ("a" * 64, None)):
            with self.subTest(masked=masked), self.assertRaises(AnalyticsError):
                counterparty_rule_value(key, masked)
        for stored in (None, SYNTHETIC_IBAN, "cpacct:v1:broken"):
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
        self.assertTrue(rule_matches(rule, debit, debit_metadata, account_secret=SECRET))
        self.assertTrue(rule_matches(rule, credit, credit_metadata, account_secret=SECRET))
        self.assertFalse(rule_matches(rule, debit, {"creditor_accounts": [identity(OTHER_IBAN)]}, account_secret=SECRET))
        self.assertFalse(rule_matches(rule, credit, {"creditor_accounts": [identity(SYNTHETIC_IBAN)]},
                                      account_secret=SECRET))
        compiled, safe = compile_rules([rule])
        self.assertTrue(safe)
        self.assertIs(first_matching_rule(compiled, debit, match_texts(debit, debit_metadata, account_secret=SECRET)), rule)
        with self.assertRaises(AnalyticsError):
            rule_matches(rule | {"operator": "contains"}, debit, debit_metadata, account_secret=SECRET)
        with self.assertRaises(AnalyticsError) as missing_secret:
            rule_matches(rule, debit, debit_metadata)
        self.assertEqual(missing_secret.exception.status, 500)

    def test_matching_uses_any_valid_account_and_ignores_non_iban_like_identifiers(self):
        metadata = {"creditor_accounts": [identity("not-an-iban"), identity(OTHER_IBAN), identity(SYNTHETIC_IBAN)]}
        evidence = counterparty_account_evidence(payload(), metadata, SECRET)
        self.assertEqual(len(evidence), 2)
        self.assertTrue(rule_matches(account_rule(), payload(), metadata, account_secret=SECRET))
        malformed = account_rule() | {"value": "cpacct:v1:broken"}
        compiled, safe = compile_rules([malformed])
        self.assertFalse(safe)
        with self.assertRaises(AnalyticsError):
            first_matching_rule(compiled, payload(), match_texts(payload(), metadata, account_secret=SECRET))


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
                         ("PL** … 1234", counterparty_account_key(SYNTHETIC_IBAN, SECRET)))
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

    def test_key_creation_multi_account_privacy_and_secret_rotation(self):
        self.add_transaction("a", "debit")
        self.metadata_for("a", "debit", "creditor_accounts", SYNTHETIC_IBAN)
        first = self.store.transactions("owner", "2026")["transactions"][0]
        candidate = self.candidate(field="counterparty_account", operator="equals",
                                   value=first["counterparty_account"], kind="expense",
                                   counterparty_account_key=first["counterparty_account_key"])
        preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(preview["affected_count"], 1)
        saved = self.store.save_rule("owner", preview["preview_id"])
        self.assertEqual(saved["counterparty_account_key"], first["counterparty_account_key"])
        replay = self.store.save_rule("owner", preview["preview_id"])
        self.assertFalse(replay["changed"])
        rotated = analytics_views.BudgetAnalyticsViews(self.sessions, clock=lambda: self.now,
                                                       cursor_secret=b"synthetic-rotated")
        self.assertEqual(rotated.transactions("owner", "2026")["transactions"][0]["effective"]["source"], "default")
        with self.sessions.begin() as session:
            metadata = session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit"))
            metadata.payload_json = json.dumps({"creditor_accounts": [identity(SYNTHETIC_IBAN), identity(OTHER_IBAN)]})
        row = self.store.transactions("owner", "2026")["transactions"][0]
        self.assertIsNone(row["counterparty_account_key"])
        self.assertIsNone(row["counterparty_account"])

    def test_validation_and_malformed_storage_fail_closed(self):
        invalid = (
            self.candidate(field="counterparty_account", operator="contains", value=SYNTHETIC_IBAN),
            self.candidate(field="counterparty_account", operator="equals", value="not-an-iban"),
            self.candidate(field="counterparty_account", operator="equals", value="PL** … 1234",
                           counterparty_account_key="A" * 64),
            self.candidate(field="counterparty_account", operator="equals",
                           value=counterparty_rule_from_identification(SYNTHETIC_IBAN, SECRET),
                           counterparty_account_key="0" * 64),
            self.candidate(field="counterparty_account", operator="equals",
                           value=counterparty_rule_from_identification(SYNTHETIC_IBAN, SECRET),
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
