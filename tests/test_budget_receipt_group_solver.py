"""Exact, deterministic and bounded receipt group search over synthetic amounts."""
import unittest

from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.receipt_group_solver import (
    MAX_GROUP_POOL,
    MAX_GROUP_PROPOSALS,
    MAX_GROUP_SIZE,
    MAX_SOLVER_STEPS,
    find_receipt_groups,
)


class ReceiptGroupSolverTests(unittest.TestCase):
    def test_exact_fractional_amounts_beyond_decimal_context_precision(self):
        anchor = ("anchor", "12345678901234567890123456789.999999999999999999")
        peers = [("a", "0.000000000000000001"), ("b", "0.000000000000000002")]
        groups, _ = find_receipt_groups(anchor, peers, "12345678901234567890123456790.000000000000000002")
        self.assertEqual(groups, [("anchor", "a", "b")])
        groups, _ = find_receipt_groups(anchor, peers, "12345678901234567890123456790.000000000000000003")
        self.assertEqual(groups, [])
        groups, _ = find_receipt_groups(("anchor", "0.1"), [("a", "0.20")], "0.300")
        self.assertEqual(groups, [("anchor", "a")])

    def test_full_bucket_then_pairs_then_dfs_are_stable_and_deduplicated(self):
        anchor = ("anchor", "1")
        peers = [("y", "2"), ("c", "1"), ("b", "1"), ("x", "2"), ("a", "1"), ("a", "1.0")]
        full_bucket = {"anchor", "a", "b"}
        expected = [
            ("anchor", "a", "b"),
            ("anchor", "x"),
            ("anchor", "y"),
            ("anchor", "a", "c"),
            ("anchor", "b", "c"),
        ]
        groups, steps = find_receipt_groups(anchor, peers, "3", same_day_merchant_ids=full_bucket)
        self.assertEqual(groups, expected)
        self.assertEqual(find_receipt_groups(anchor, reversed(peers), "3", same_day_merchant_ids=full_bucket),
                         (expected, steps))
        self.assertEqual(
            find_receipt_groups(anchor, [("a", "1"), ("b", "2")], "3", same_day_merchant_ids={"a"}),
            ([("anchor", "b")], 2),
        )

    def test_ineligible_exact_groups_are_checked_only_once(self):
        inspected = []

        def eligible(group):
            inspected.append(group)
            return "b" not in group

        groups, _ = find_receipt_groups(
            ("anchor", "1"),
            [("a", "1"), ("b", "1"), ("c", "1"), ("x", "2")],
            "3", same_day_merchant_ids={"a", "b"}, eligible=eligible,
        )
        self.assertEqual(groups, [("anchor", "x"), ("anchor", "a", "c")])
        self.assertEqual(inspected.count(("anchor", "a", "b")), 1)

    def test_group_size_and_peer_pool_caps(self):
        anchor = ("anchor", "1")
        peers = [(f"p{index:02}", "1") for index in range(MAX_GROUP_SIZE)]
        groups, _ = find_receipt_groups(anchor, peers, str(MAX_GROUP_SIZE))
        self.assertTrue(groups)
        self.assertTrue(all(len(group) == MAX_GROUP_SIZE for group in groups))
        groups, _ = find_receipt_groups(anchor, peers, str(MAX_GROUP_SIZE + 1), same_day_merchant_ids=dict(peers))
        self.assertEqual(groups, [])

        def ranked_peers():
            for index in range(MAX_GROUP_POOL):
                yield f"p{index:02}", "9"
            self.fail("read beyond the ranked peer cap")

        self.assertEqual(find_receipt_groups(anchor, ranked_peers(), "8"), ([], 0))

    def test_node_budget_and_global_proposal_cap(self):
        anchor = ("anchor", "1")
        peers = [(f"p{index:02}", "2") for index in range(MAX_GROUP_POOL)]
        # The odd remainder can never be made from even peers.
        self.assertEqual(find_receipt_groups(anchor, peers, "12", max_steps=17), ([], 17))
        self.assertEqual(find_receipt_groups(anchor, peers, "12", max_steps=10_000), ([], MAX_SOLVER_STEPS))
        self.assertEqual(find_receipt_groups(anchor, peers, "3", max_steps=0), ([], 0))
        self.assertEqual(find_receipt_groups(anchor, peers, "3", max_proposals=0), ([], 0))

        groups, steps = find_receipt_groups(anchor, peers, "3", max_proposals=99)
        self.assertEqual(groups, [("anchor", f"p{index:02}") for index in range(MAX_GROUP_PROPOSALS)])
        self.assertEqual(steps, MAX_GROUP_PROPOSALS)
        self.assertEqual(find_receipt_groups(anchor, peers, "3", max_proposals=2), (groups[:2], 2))
        self.assertEqual(find_receipt_groups(anchor, peers, "3", max_steps=1), (groups[:1], 1))

    def test_rejects_nonpositive_money_and_conflicting_amounts_for_an_id(self):
        with self.assertRaises(AnalyticsError):
            find_receipt_groups(("anchor", "1"), [("a", "1")], "0")
        with self.assertRaises(AnalyticsError):
            find_receipt_groups(("anchor", "1"), [("a", "-1")], "2")
        with self.assertRaises(AnalyticsError):
            find_receipt_groups(("anchor", "1"), [("a", "1"), ("a", "2")], "3")
        self.assertEqual(find_receipt_groups(("anchor", "1"), [("anchor", "1.0")], "2"), ([], 0))
        self.assertEqual(find_receipt_groups(("anchor", "1"), [("a", "1")], "1"), ([], 0))
