"""Exact arithmetic and material evidence use only synthetic values."""
from copy import deepcopy
from types import SimpleNamespace
import unicodedata
import unittest

from hestia.budget.analytics_core import (AnalyticsError, canonical_money, compare_money, compatible_kind,
    compile_rules, effective_date, financial_fingerprint, first_matching_rule, match_texts, negate_money,
    owned_identities, pair_fingerprint, parse_period, projection_digest, rule_matches, sum_money)


def payload(**changes):
    return {"amount": "12.30", "currency": "PLN", "credit_debit_indicator": "DBIT", "status": "BOOK",
            "booking_date": "2026-11-30", "value_date": "2026-12-01", "transaction_date": None,
            "remittance_information": ["Synthetic SHOP"], **changes}


def identity(value):
    return {"scheme": "BBAN", "identification": value}


class MoneyTests(unittest.TestCase):
    def test_canonical_exact_signed_math_preserves_large_values_and_scales(self):
        for raw, expected in (("0001.2300", "1.23"), ("-0.00", "0"), ("-001.200", "-1.2"), ("12", "12")):
            self.assertEqual(canonical_money(raw), expected)
        self.assertEqual(sum_money(["1.2", "0.003", "-0.203"]), "1")
        self.assertEqual(sum_money([]), "0")
        self.assertEqual(sum_money(["-1.20", "0.001"]), "-1.199")
        self.assertEqual(negate_money("-1.20"), "1.2")
        self.assertEqual(negate_money("0.00"), "0")
        self.assertEqual([compare_money(a, b) for a, b in (("1.0", "1"), ("0.1", "0.02"), ("-1", "0"))], [0, 1, -1])
        enormous = "9" * 80
        self.assertEqual(sum_money(enormous for _ in range(100_000)), str(int(enormous) * 100_000))
        tiny = "0." + "0" * 77 + "1"
        derived = sum_money([enormous, tiny])
        self.assertEqual(derived, enormous + "." + "0" * 77 + "1")
        self.assertEqual(sum_money([derived, negate_money("0")]), derived)
        self.assertEqual(sum_money([derived, negate_money(derived)]), "0")
        self.assertEqual(compare_money(derived, enormous), 1)

    def test_no_float_exponent_or_unbounded_projection(self):
        for value in (None, 1, True, 1.1, "", "1e2", ".1", "1.", "NaN", "1" * 257):
            with self.subTest(value=value), self.assertRaises(AnalyticsError):
                canonical_money(value)
        with self.assertRaises(AnalyticsError) as raised:
            sum_money("1" for _ in range(100_001))
        self.assertEqual((raised.exception.code, raised.exception.status), ("budget_analytics_limit", 413))


class FingerprintTests(unittest.TestCase):
    def test_financial_materiality_uses_effective_date_not_text_or_observation_time(self):
        first = payload()
        fingerprint = financial_fingerprint(first)
        self.assertEqual(len(fingerprint), 64)
        for changes in ({"amount": "12.3000"}, {"value_date": "2027-01-01"}, {"remittance_information": ["Corrected text"]},
                        {"transaction_date": "2026-11-29", "observed_at": 999}):
            self.assertEqual(financial_fingerprint(first | changes), fingerprint)
        for changes in ({"amount": "12.31"}, {"currency": "EUR"}, {"credit_debit_indicator": "CRDT"},
                        {"status": "PDNG"}, {"booking_date": "2026-12-01"}):
            self.assertNotEqual(financial_fingerprint(first | changes), fingerprint)
        self.assertEqual(effective_date(payload(booking_date=None)), "2026-12-01")
        self.assertIsNone(effective_date(payload(booking_date=None, value_date=None)))
        self.assertNotEqual(financial_fingerprint(payload(booking_date=None)), fingerprint)

    def test_invalid_financial_shape_is_fixed_and_value_free(self):
        for value in (None, payload(amount=None), payload(amount="-1"), payload(amount="1" * 81),
                      payload(currency="private"), payload(currency=None), payload(credit_debit_indicator="bad"),
                      payload(status="bad"), payload(booking_date="2026-02-30"), payload(booking_date=3)):
            with self.subTest(value=value), self.assertRaises(AnalyticsError) as raised:
                financial_fingerprint(value)
            self.assertEqual(str(raised.exception), "budget_analytics_invalid_request")

    def test_pair_binds_structured_relevant_evidence_without_names_or_timestamps(self):
        own = {"a": [identity("own-a")], "b": [identity("own-b")], "unrelated": [identity("own-x")]}
        metadata = {"creditor_accounts": [identity("own-b")], "creditor_name": "Synthetic name", "mcc": "1234"}
        first = pair_fingerprint("a", "ref", payload(), metadata, own)
        self.assertEqual(pair_fingerprint("a", "ref", payload(), metadata | {"creditor_name": "Changed", "mcc": "9999"}, own), first)
        changed = deepcopy(own)
        changed["unrelated"] = [identity("changed-unrelated")]
        self.assertEqual(pair_fingerprint("a", "ref", payload(), metadata, changed), first)
        changed["unrelated"] = [identity("own-b")]
        self.assertNotEqual(pair_fingerprint("a", "ref", payload(), metadata, changed), first)
        self.assertNotEqual(pair_fingerprint("a", "another-ref", payload(), metadata, own), first)
        self.assertNotEqual(pair_fingerprint("a", "ref", payload(), metadata | {"creditor_accounts": [identity("other")]}, own), first)
        self.assertEqual(pair_fingerprint("a", "ref", payload()), pair_fingerprint("a", "ref", payload(), {}, {}))
        # Values precomputed once per projection give exactly the per-entry result.
        self.assertEqual(pair_fingerprint("a", "ref", payload(), metadata, own, financial=financial_fingerprint(payload()),
                                          owned_ids=owned_identities(own)), first)
        with self.assertRaises(AnalyticsError):
            owned_identities([identity("own-a")])
        self.assertEqual(pair_fingerprint("a", "ref", payload(), {"creditor_accounts": [identity("own-b"), identity("own-b")]}, own),
                         pair_fingerprint("a", "ref", payload(), {"creditor_accounts": [identity("own-b")]}, own))
        for account, reference, meta, ids in ((None, "ref", {}, {}), ("a", "", {}, {}), ("a", "ref", [], {}),
                 ("a", "ref", {}, []), ("a", "ref", {"creditor_accounts": "bad"}, {}),
                 ("a", "ref", {"creditor_accounts": [None]}, {}),
                 ("a", "ref", {"creditor_accounts": [{"scheme": "BBAN"}]}, {}),
                 ("a", "ref", {"creditor_accounts": [{"scheme": "", "identification": "x"}]}, {}),
                 ("a", "ref", {"creditor_accounts": [{"scheme": "BBAN", "identification": "x" * 1025}]}, {})):
            with self.subTest(account=account, meta=meta), self.assertRaises(AnalyticsError):
                pair_fingerprint(account, reference, payload(), meta, ids)

    def test_digest_and_diagnostics_are_deterministic_and_do_not_echo_invalid_data(self):
        self.assertEqual(projection_digest({"b": 2, "a": 1}), projection_digest({"a": 1, "b": 2}))
        with self.assertRaises(AnalyticsError):
            projection_digest({"value": float("nan")})
        with self.assertRaises(AnalyticsError):
            projection_digest({"value": object()})
        error = AnalyticsError("SECRET-NOT-A-CODE", 200)
        self.assertEqual((error.code, error.status, str(error)),
                         ("budget_analytics_invalid_request", 400, "budget_analytics_invalid_request"))
        self.assertEqual(AnalyticsError("budget_analytics_stale_cursor", 409).status, 409)


class PeriodAndRuleTests(unittest.TestCase):
    def test_inclusive_month_and_year_bounds_handle_leap_year_and_last_supported_year(self):
        self.assertEqual(parse_period("2024-02"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(parse_period("1900-02"), ("1900-02-01", "1900-02-28"))
        self.assertEqual(parse_period("9999-12"), ("9999-12-01", "9999-12-31"))
        self.assertEqual(parse_period("2026"), ("2026-01-01", "2026-12-31"))
        for value in (None, 2026, "1899", "10000", "2026-00", "2026-13", "2026-1", "2026-11-01"):
            with self.subTest(value=value), self.assertRaises(AnalyticsError):
                parse_period(value)

    def test_literal_unicode_rules_and_direction_constraints(self):
        rule = {"enabled": True, "kind": "auto", "field": "description", "operator": "contains", "value": "shop"}
        self.assertTrue(rule_matches(rule, payload()))
        self.assertFalse(rule_matches(rule | {"value": ".*"}, payload()))
        self.assertFalse(rule_matches(rule | {"enabled": False}, payload()))
        self.assertFalse(rule_matches(rule | {"kind": "refund"}, payload()))
        self.assertTrue(rule_matches(rule | {"field": "counterparty_name", "operator": "equals", "value": "ŻÓŁĆ"},
                                     payload(), {"creditor_name": "żółć"}))
        self.assertTrue(rule_matches(SimpleNamespace(**(rule | {"field": "counterparty_name", "value": "sender"})),
                                     payload(credit_debit_indicator="CRDT"), {"debtor_name": "Synthetic SENDER"}))
        self.assertTrue(rule_matches(rule | {"field": "mcc", "operator": "equals", "value": "1234"}, payload(), {"mcc": "1234"}))
        self.assertFalse(rule_matches(rule | {"field": "mcc", "operator": "equals", "value": "1234"}, payload()))
        for changes in ({"field": "execute"}, {"operator": "regex"}):
            with self.assertRaises(AnalyticsError):
                rule_matches(rule | changes, payload())
        for kind, direction, expected in (("auto", "DBIT", True), ("excluded", "CRDT", True),
                     ("expense", "DBIT", True), ("income", "CRDT", True), ("refund", "CRDT", True),
                     ("income", "DBIT", False), ("expense", "CRDT", False), ("bad", "DBIT", False)):
            self.assertEqual(compatible_kind(kind, payload(credit_debit_indicator=direction)), expected)

    def test_compiled_rules_match_the_same_first_rule_without_refolding(self):
        rules = [
            {"id": "later", "enabled": True, "kind": "expense", "field": "description", "operator": "contains",
             "value": "shop", "priority": 2},
            {"id": "first", "enabled": True, "kind": "auto", "field": "description", "operator": "equals",
             "value": "Synthetic SHOP", "priority": 1},
            {"id": "off", "enabled": False, "kind": "expense", "field": "execute", "operator": "regex",
             "value": "shop", "priority": 0},
        ]
        ordered = sorted(rules, key=lambda row: (row["priority"], row["id"]))
        compiled, safe = compile_rules(ordered)
        self.assertTrue(safe)
        texts = match_texts(payload(), None)
        found = first_matching_rule(compiled, payload(), texts)
        self.assertEqual(found["id"], "first")
        self.assertTrue(rule_matches(ordered[1], payload()))
        # Sort order is (priority, id), so the disabled rule is ordered[0], not the last input.
        self.assertEqual(ordered[0]["id"], "off")
        disabled, disabled_safe = compile_rules([ordered[0]])
        self.assertTrue(disabled_safe)
        self.assertIsNone(first_matching_rule(disabled, payload(), texts))
        self.assertFalse(rule_matches(ordered[0], payload()))
        broken, broken_safe = compile_rules([ordered[1] | {"field": "execute"}])
        self.assertFalse(broken_safe)
        with self.assertRaises(AnalyticsError):
            first_matching_rule(broken, payload(), texts)

    def test_compiled_matcher_agrees_with_rule_matches(self):
        base = {"enabled": True, "kind": "auto", "field": "description", "operator": "contains", "priority": 1}
        literal = ".*+?[]()"
        nfc, nfd = "ŻÓŁĆ", unicodedata.normalize("NFD", "ŻÓŁĆ")
        self.assertNotEqual(nfc, nfd)
        text = {**base, "id": "u", "field": "counterparty_name", "operator": "equals"}
        expense = {"id": "later", "enabled": True, "kind": "expense", "field": "description",
                   "operator": "contains", "value": "shop", "priority": 2}
        cases = (
            ("empty contains", [{**base, "id": "e", "value": ""}], payload(), None),
            ("empty equals miss", [{**base, "id": "e", "operator": "equals", "value": ""}], payload(), None),
            ("empty equals hit", [{**base, "id": "e", "operator": "equals", "value": ""}],
             payload(remittance_information=[""]), None),
            ("regex literal miss", [{**base, "id": "r", "value": literal}], payload(), None),
            ("regex literal hit", [{**base, "id": "r", "value": literal}],
             payload(remittance_information=[f"before {literal} after"]), None),
            ("nfc needle nfd text", [{**text, "value": nfc}], payload(), {"creditor_name": nfd}),
            ("nfd needle nfc text", [{**text, "value": nfd}], payload(), {"creditor_name": nfc}),
            ("sharp s", [{**text, "value": "ß"}], payload(), {"creditor_name": "SS"}),
            ("sharp s lower", [{**text, "value": "ß"}], payload(), {"creditor_name": "ss"}),
            ("priority tie", [{**base, "id": "b", "priority": 4, "value": "shop"},
                              {**base, "id": "a", "priority": 4, "value": "shop"}], payload(), None),
            ("disabled isolation", [{"id": "off", "enabled": False, "kind": "expense", "field": "execute",
                                     "operator": "regex", "value": "shop", "priority": 0}], payload(), None),
            ("expense debit", [expense], payload(), None),
            ("expense credit", [expense], payload(credit_debit_indicator="CRDT"), None),
        )
        for name, rules, body, metadata in cases:
            with self.subTest(name=name):
                ordered = sorted(rules, key=lambda row: (row["priority"], row["id"]))
                compiled, safe = compile_rules(ordered)
                self.assertTrue(safe)
                found = first_matching_rule(compiled, body, match_texts(body, metadata))
                expected = next((rule for rule in ordered if rule_matches(rule, body, metadata)), None)
                self.assertEqual(found["id"] if found is not None else None,
                                 expected["id"] if expected is not None else None)
                if name == "priority tie":
                    self.assertEqual(found["id"], "a")
                if name == "disabled isolation":
                    self.assertIsNone(found)
                    self.assertFalse(rule_matches(ordered[0], body, metadata))
