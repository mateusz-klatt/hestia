"""Synthetic counterparty-account rules; no real bank identifiers."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from hestia import db
from hestia.budget import analytics_views
from hestia.budget.analytics_core import (AnalyticsError, canonical_counterparty_account, compile_rules, counterparty_account_evidence,
    counterparty_account_key, counterparty_key_id, counterparty_rule_from_identification, counterparty_rule_parts,
    counterparty_rule_value, first_matching_rule, mask_counterparty_account, match_texts,
    normalize_counterparty_account, rule_matches)
from tests.test_budget_analytics_core import identity, payload
from tests.test_budget_analytics_views import ProjectionCase


SYNTHETIC_IBAN = "PL" + "0" * 22 + "1234"
OTHER_IBAN = "PL" + "0" * 22 + "5678"
POLISH_BBAN = "0" * 26
POLISH_IBAN = "PL04" + POLISH_BBAN
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
        self.assertEqual(canonical_counterparty_account(POLISH_BBAN, "BBAN"), ("IBAN", POLISH_IBAN))
        self.assertEqual(canonical_counterparty_account(POLISH_IBAN, "IBAN"), ("IBAN", POLISH_IBAN))
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
        with self.assertRaises(AnalyticsError) as missing_secret:
            rule_matches(rule, debit, debit_metadata)
        self.assertEqual((missing_secret.exception.code, missing_secret.exception.status),
                         ("budget_analytics_rule_key_unavailable", 400))

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
        formatted_bban = "-".join(POLISH_BBAN[index:index + 4] for index in range(0, len(POLISH_BBAN), 4))
        self.metadata("a", "polish-debit", {"creditor_accounts": [
            {"scheme": "BBAN", "identification": formatted_bban},
            {"scheme": "IBAN", "identification": spaced(POLISH_IBAN)},
        ]})
        body = self.store.transactions("owner", "2026")
        row = body["transactions"][0]
        self.assertEqual(row["counterparty_account"], "PL** … 0000")
        self.assertRegex(row["counterparty_account_key"], r"^[0-9a-f]{64}$")
        self.assertNotIn(POLISH_BBAN, json.dumps(body))
        candidate = self.candidate(field="counterparty_account", operator="equals", value=formatted_bban)
        preview = self.store.preview_rule("owner", candidate)
        self.assertEqual(preview["affected_count"], 1)
        saved = self.store.save_rule("owner", preview["preview_id"])
        row = self.store.transactions("owner", "2026")["transactions"][0]
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
        with self.assertRaises(AnalyticsError) as mismatched:
            self.store.transactions("owner", "2026")
        self.assertEqual((mismatched.exception.code, mismatched.exception.status),
                         ("budget_analytics_rule_key_unavailable", 400))
        self.assertEqual(saved["value"], "PL** … 1234")

    def test_validation_and_malformed_storage_fail_closed(self):
        invalid = (
            self.candidate(field="counterparty_account", operator="contains", value=SYNTHETIC_IBAN),
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
