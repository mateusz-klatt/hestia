"""Synthetic unit tests for pure monthly category spending-limit evaluation."""
from __future__ import annotations

import random
import unittest

from hestia.budget.analytics_core import compare_money, negate_money, sum_money
from hestia.budget.spending_limits import (
    DEFAULT_WARNING_PERCENT,
    MAX_LIMITS,
    MAX_SPENDING,
    RULE_VERSION,
    STATUSES,
    CategoryLimit,
    CategoryLimitStatus,
    CategorySpending,
    SpendingLimitCapError,
    SpendingLimitInputError,
    evaluate_spending_limits,
)


def _limit(category_id="cat-groceries", monthly_limit="100.00", **changes):
    values = {"owner": "owner", "category_id": category_id, "currency": "PLN",
              "monthly_limit": monthly_limit, "enabled": True, "warning_percent": DEFAULT_WARNING_PERCENT, **changes}
    return CategoryLimit(**values)


def _spending(category_id="cat-groceries", expenses="0", refunds="0", **changes):
    values = {"owner": "owner", "category_id": category_id, "currency": "PLN",
              "expenses": expenses, "refunds": refunds, "archived": False, **changes}
    return CategorySpending(**values)


class SpendingLimitsTests(unittest.TestCase):
    def assert_balance_invariant(self, result: CategoryLimitStatus):
        reconstructed = sum_money([result.net_spending, result.remaining, negate_money(result.over_by)])
        self.assertEqual(compare_money(reconstructed, result.monthly_limit), 0)

    def test_all_four_statuses_exact_boundaries_and_balance_invariant(self):
        limits = [
            _limit("cat-a-within", "100.00"),
            _limit("cat-b-approach-edge", "100.00"),
            _limit("cat-c-exact", "100.00"),
            _limit("cat-d-exceeded", "100.00"),
            _limit("cat-e-no-spend", "250.50"),
        ]
        spending = [
            _spending("cat-a-within", "79.999999999999", "0"),
            _spending("cat-b-approach-edge", "80.00", "0"),
            _spending("cat-c-exact", "125.40", "25.40"),
            _spending("cat-d-exceeded", "100.0001", "0"),
        ]
        results = evaluate_spending_limits("owner", "2026-11", limits, spending)
        self.assertEqual([r.category_id for r in results],
                         ["cat-a-within", "cat-b-approach-edge", "cat-c-exact", "cat-d-exceeded", "cat-e-no-spend"])
        self.assertEqual(set(r.status for r in results), set(STATUSES))

        within, approaching, exact, exceeded, unused = results
        self.assertEqual((within.status, within.percent_used_floor, within.over_by), ("within", 79, "0"))
        self.assertEqual(within.remaining, "20.000000000001")
        self.assertEqual((approaching.status, approaching.percent_used_floor, approaching.remaining, approaching.over_by),
                         ("approaching", 80, "20", "0"))
        self.assertEqual((exact.status, exact.net_spending, exact.remaining, exact.over_by, exact.percent_used_floor),
                         ("at_limit", "100", "0", "0", 100))
        self.assertEqual((exceeded.status, exceeded.remaining, exceeded.over_by, exceeded.percent_used_floor),
                         ("exceeded", "0", "0.0001", 100))
        self.assertEqual((unused.status, unused.expenses, unused.refunds, unused.net_spending, unused.remaining,
                          unused.over_by, unused.percent_used_floor, unused.archived, unused.rule_version),
                         ("within", "0", "0", "0", "250.5", "0", 0, False, RULE_VERSION))
        for item in results:
            self.assertEqual(item.period, "2026-11")
            self.assert_balance_invariant(item)

    def test_exact_large_tiny_negative_net_and_repeating_decimal_thresholds(self):
        enormous = "9" * 78 + ".5"
        tiny = "0." + "0" * 77 + "1"
        limits = [
            _limit("cat-enormous", enormous),
            _limit("cat-tiny", tiny),
            _limit("cat-negative-net", "100"),
            _limit("cat-sub-unit-neg", "1.00"),
            _limit("cat-zero-net", "100", warning_percent=1),
            _limit("cat-low-warn-hit", "100", warning_percent=1),
            _limit("cat-high-warn", "100", warning_percent=99),
            _limit("cat-high-warn-hit", "100", warning_percent=99),
            _limit("cat-third-33", "3", warning_percent=33),
            _limit("cat-third-34", "3", warning_percent=34),
            _limit("cat-archived", "50"),
        ]
        spending = [
            _spending("cat-enormous", enormous, "0"),
            _spending("cat-tiny", "0." + "0" * 77 + "2", "0"),
            _spending("cat-negative-net", "10.25", "35.50"),
            _spending("cat-sub-unit-neg", "0.50", "1.00"),
            _spending("cat-zero-net", "12.50", "12.50"),
            _spending("cat-low-warn-hit", "1.00", "0"),
            _spending("cat-high-warn", "98.99", "0"),
            _spending("cat-high-warn-hit", "99.00", "0"),
            _spending("cat-third-33", "1", "0"),
            _spending("cat-third-34", "1", "0"),
            _spending("cat-archived", "12.50", "0", archived=True),
            _spending(None, "9999.99", "0"),
        ]
        by_cat = {item.category_id: item for item in evaluate_spending_limits(
            "owner", "2026-02", (x for x in limits), (x for x in spending))}
        self.assertEqual((by_cat["cat-enormous"].status, by_cat["cat-enormous"].remaining), ("at_limit", "0"))
        self.assertEqual((by_cat["cat-tiny"].status, by_cat["cat-tiny"].over_by, by_cat["cat-tiny"].percent_used_floor),
                         ("exceeded", tiny, 200))
        neg = by_cat["cat-negative-net"]
        self.assertEqual((neg.status, neg.net_spending, neg.remaining, neg.over_by, neg.percent_used_floor),
                         ("within", "-25.25", "125.25", "0", 0))
        sub_neg = by_cat["cat-sub-unit-neg"]
        self.assertEqual((sub_neg.status, sub_neg.net_spending, sub_neg.remaining, sub_neg.percent_used_floor),
                         ("within", "-0.5", "1.5", 0))
        zero_net = by_cat["cat-zero-net"]
        self.assertEqual((zero_net.status, zero_net.net_spending, zero_net.remaining, zero_net.percent_used_floor),
                         ("within", "0", "100", 0))
        self.assertEqual((by_cat["cat-low-warn-hit"].status, by_cat["cat-low-warn-hit"].percent_used_floor),
                         ("approaching", 1))
        self.assertEqual((by_cat["cat-high-warn"].status, by_cat["cat-high-warn"].percent_used_floor), ("within", 98))
        self.assertEqual((by_cat["cat-high-warn-hit"].status, by_cat["cat-high-warn-hit"].percent_used_floor),
                         ("approaching", 99))
        self.assertEqual((by_cat["cat-third-33"].status, by_cat["cat-third-33"].percent_used_floor), ("approaching", 33))
        self.assertEqual((by_cat["cat-third-34"].status, by_cat["cat-third-34"].percent_used_floor), ("within", 33))
        self.assertTrue(by_cat["cat-archived"].archived)
        for item in by_cat.values():
            self.assert_balance_invariant(item)
        self.assertEqual(evaluate_spending_limits("owner", "1900-01", [], [], max_limits=0, max_spending=0), ())
        self.assertEqual(evaluate_spending_limits("owner", "9999-12", [], []), ())

    def test_currency_isolation_disabled_limits_and_permutation_invariance(self):
        limits = [
            _limit("cat-food", "200", currency="PLN"),
            _limit("cat-food", "50", currency="EUR"),
            _limit("cat-disabled", "10", currency="PLN", enabled=False),
            _limit("cat-travel", "300", currency="USD"),
        ]
        spending = [
            _spending("cat-food", "190", "0", currency="PLN"),
            _spending("cat-food", "55", "0", currency="EUR"),
            _spending("cat-disabled", "999", "0", currency="PLN"),
            _spending("cat-unlisted", "42", "0", currency="PLN"),
        ]
        baseline = evaluate_spending_limits("owner", "2026-11", limits, spending)
        self.assertEqual([(item.currency, item.category_id, item.status) for item in baseline], [
            ("EUR", "cat-food", "exceeded"),
            ("PLN", "cat-food", "approaching"),
            ("USD", "cat-travel", "within"),
        ])
        rng = random.Random(20261121)
        for _ in range(25):
            perm_limits = list(limits)
            perm_spending = list(spending)
            rng.shuffle(perm_limits)
            rng.shuffle(perm_spending)
            self.assertEqual(evaluate_spending_limits("owner", "2026-11", perm_limits, perm_spending), baseline)

    def test_validation_caps_and_fixed_error_messages(self):
        bad_calls = [
            lambda: evaluate_spending_limits(" ", "2026-11", [], []),
            lambda: evaluate_spending_limits(" " * 1025, "2026-11", [], []),
            lambda: evaluate_spending_limits("owner\n", "2026-11", [], []),
            lambda: evaluate_spending_limits("owner", "2026", [], []),
            lambda: evaluate_spending_limits("owner", "2026-00", [], []),
            lambda: evaluate_spending_limits("owner", "2026-13", [], []),
            lambda: evaluate_spending_limits("owner", "1899-12", [], []),
            lambda: evaluate_spending_limits("owner", 202611, [], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [], max_limits=True),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [], max_limits=-1),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [], max_spending=True),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [], max_spending=-1),
            lambda: evaluate_spending_limits("owner", "2026-11", "not-a-list", []),
            lambda: evaluate_spending_limits("owner", "2026-11", [], {"a": 1}),
            lambda: evaluate_spending_limits("owner", "2026-11", ["bad-item"], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [], ["bad-item"]),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(owner="other")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending(owner="other")]),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(category_id="uncategorised")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(category_id="x" * 129)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(currency="pln")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="0")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="0.00")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="-5")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="+5")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="1e2")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="NaN")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="10.")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit=".5")], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit="1" * 81)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(monthly_limit=100)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(enabled=1)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(warning_percent=True)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(warning_percent=0)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit(warning_percent=100)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [_limit("c1"), _limit("c1", enabled=False)], []),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending("c1"), _spending("c1")]),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending(None), _spending(None)]),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending(expenses="-1")]),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending(refunds="-1")]),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending(archived=1)]),
            lambda: evaluate_spending_limits("owner", "2026-11", [], [_spending(category_id="uncategorised")]),
        ]
        for call in bad_calls:
            with self.assertRaises(SpendingLimitInputError) as exc:
                call()
            self.assertEqual(str(exc.exception), "Invalid spending limit input")

        with self.assertRaises(SpendingLimitCapError) as exc:
            evaluate_spending_limits("owner", "2026-11", [_limit("c1"), _limit("c2")], [], max_limits=1)
        self.assertEqual(str(exc.exception), "Spending limit evaluation cap exceeded")

        with self.assertRaises(SpendingLimitCapError) as exc:
            evaluate_spending_limits("owner", "2026-11", [], [_spending("c1"), _spending("c2")], max_spending=1)
        self.assertEqual(str(exc.exception), "Spending limit evaluation cap exceeded")

        at_cap_limits = [_limit(f"cat-{i:04d}", "10") for i in range(MAX_LIMITS)]
        self.assertEqual(len(evaluate_spending_limits("owner", "2026-11", at_cap_limits, [])), MAX_LIMITS)
        with self.assertRaises(SpendingLimitCapError):
            evaluate_spending_limits("owner", "2026-11", [*at_cap_limits, _limit("cat-over", "10")], [])

        at_cap_spending = [_spending(f"cat-{i:05d}", "1") for i in range(MAX_SPENDING)]
        self.assertEqual(evaluate_spending_limits("owner", "2026-11", [], at_cap_spending), ())
        with self.assertRaises(SpendingLimitCapError):
            evaluate_spending_limits("owner", "2026-11", [], [*at_cap_spending, _spending("cat-over", "1")])

    def test_parent_limit_counts_the_subtree_in_the_same_currency_only(self):
        parents = {"transport": None, "car": "transport", "fuel": "car", "taxi": "transport", "wash": "car",
                   "parking": None, "meter": "parking", "food": None, "rebate": "food",
                   "solo": None, "zero-child": "solo"}
        limits = [
            _limit("transport", "30"), _limit("car", "10"), _limit("fuel", "10"),
            _limit("parking", "5"), _limit("food", "50"), _limit("solo", "10.00"),
            _limit("parking", "100", currency="EUR"), _limit("transport", "100", currency="EUR"),
        ]
        spending = [
            _spending("transport", "1", archived=True),
            _spending("car", "5"),
            _spending("fuel", "8", "1"),
            _spending("taxi", "20"),
            _spending("wash", "0", "0"),
            _spending("meter", "6"),
            _spending("food", "40"),
            _spending("rebate", "0", "2"),
            _spending(None, "100"),
            _spending("solo", "10.00"),
            _spending("zero-child", "0", "0"),
            _spending("meter", "9", currency="EUR"),
            _spending("fuel", "3", currency="EUR"),
        ]
        results = evaluate_spending_limits("owner", "2026-11", limits, spending, parents=parents)
        by_key = {(item.currency, item.category_id): item for item in results}
        transport = by_key[("PLN", "transport")]
        self.assertEqual((transport.expenses, transport.refunds, transport.net_spending, transport.status,
                          transport.over_by, transport.percent_used_floor, transport.archived),
                         ("34", "1", "33", "exceeded", "3", 110, True))
        self.assertEqual(by_key[("PLN", "car")].net_spending, "12")
        self.assertEqual((by_key[("PLN", "fuel")].net_spending, by_key[("PLN", "fuel")].status,
                          by_key[("PLN", "fuel")].archived), ("7", "within", False))
        self.assertEqual((by_key[("PLN", "parking")].net_spending, by_key[("PLN", "parking")].archived,
                          by_key[("PLN", "parking")].status), ("6", False, "exceeded"))
        self.assertEqual((by_key[("PLN", "food")].expenses, by_key[("PLN", "food")].refunds,
                          by_key[("PLN", "food")].net_spending, by_key[("PLN", "food")].status),
                         ("40", "2", "38", "within"))
        self.assertEqual(by_key[("EUR", "parking")].net_spending, "9")
        self.assertEqual(by_key[("EUR", "transport")].net_spending, "3")
        flat = evaluate_spending_limits("owner", "2026-11", limits, spending)
        self.assertEqual(next(item.net_spending for item in flat if item.category_id == "transport"
                              and item.currency == "PLN"), "1")
        roots = {"solo": None, "zero-child": None}
        solo_limits = [_limit("solo", "10.00")]
        solo_spending = [_spending("solo", "10.00"), _spending("zero-child", "0", "0")]
        self.assertEqual(
            evaluate_spending_limits("owner", "2026-11", solo_limits, solo_spending, parents=roots),
            evaluate_spending_limits("owner", "2026-11", solo_limits, solo_spending))
        self.assertEqual(
            evaluate_spending_limits("owner", "2026-11", solo_limits, solo_spending,
                                     parents={"solo": None, "zero-child": "solo"}),
            evaluate_spending_limits("owner", "2026-11", solo_limits, solo_spending))

    def test_parent_map_rejects_cycles_self_parents_bad_shapes_and_oversized_maps(self):
        spending = [_spending("a", "1"), _spending("b", "1")]
        limits = [_limit("a", "10")]
        for parents in ({"a": "a"}, {"a": "uncategorised"}, {1: None}, {"a": 2}, ["a"], {"a": ""}):
            with self.subTest(parents=parents), self.assertRaises(SpendingLimitInputError):
                evaluate_spending_limits("owner", "2026-11", limits, spending, parents=parents)
        with self.assertRaises(SpendingLimitInputError):
            evaluate_spending_limits("owner", "2026-11", limits, spending, parents={"a": "b", "b": "a"})
        with self.assertRaises(SpendingLimitCapError):
            evaluate_spending_limits("owner", "2026-11", [], [], parents={"a": None}, max_spending=0)

    def test_repr_redacts_all_private_financial_fields(self):
        limit = _limit("cat-secret-99", "4321.99", owner="alice-secret")
        spend = _spending("cat-secret-99", "1234.56", "78.90", owner="alice-secret")
        status = evaluate_spending_limits("alice-secret", "2026-11", [limit], [spend])[0]
        combined = repr(limit) + repr(spend) + repr(status)
        for token in ("alice-secret", "cat-secret-99", "4321.99", "1234.56", "78.90", "1155.66", "3166.33"):
            self.assertNotIn(token, combined)


if __name__ == "__main__":
    unittest.main()
