"""Synthetic tests for pure monthly recurring-charge hint detection."""
from __future__ import annotations

import hashlib
import unittest

from hestia.budget.recurring import (
    MAX_DESCRIPTION,
    MAX_GAP_DAYS,
    MAX_TEXT,
    MIN_GAP_DAYS,
    MIN_OCCURRENCES,
    RULE_VERSION,
    RecurringEntry,
    RecurringHint,
    RecurringInputError,
    RecurringLimitError,
    RecurringOccurrence,
    detect_recurring_charges,
)


def _fp(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _entry(ref, stamp, *, owner="owner", account_id="acc-1", amount="49.99", currency="PLN",
           direction="DBIT", effective_kind="expense", counterparty_name="Synthetic Stream",
           description="", eligible=True, fingerprint=None):
    return RecurringEntry(
        owner=owner,
        account_id=account_id,
        entry_reference=ref,
        amount=amount,
        currency=currency,
        direction=direction,
        effective_kind=effective_kind,
        date=stamp,
        fingerprint=_fp(f"{account_id}:{ref}") if fingerprint is None else fingerprint,
        counterparty_name=counterparty_name,
        description=description,
        eligible=eligible,
    )


class RecurringChargeDetectionTests(unittest.TestCase):
    def test_requires_at_least_three_monthly_occurrences_and_returns_exact_evidence(self):
        two = [_entry("r1", "2026-01-15"), _entry("r2", "2026-02-15")]
        self.assertEqual(detect_recurring_charges("owner", two, {"acc-1"}), ())

        three = [
            _entry("r3", "2026-03-15", amount="49.990", counterparty_name="  SYNTHETIC   STREAM  "),
            _entry("r1", "2026-01-15", amount="0049.99", counterparty_name="Synthetic Stream"),
            _entry("r2", "2026-02-15", amount="49.9900", counterparty_name="Synthetic\tStream"),
        ]
        hints = detect_recurring_charges("owner", three, {"acc-1"})
        self.assertEqual(len(hints), 1)
        hint = hints[0]
        self.assertIsInstance(hint, RecurringHint)
        self.assertEqual(hint.rule_version, RULE_VERSION)
        self.assertEqual(hint.cadence, "monthly")
        self.assertEqual(hint.label, "SYNTHETIC STREAM")
        self.assertEqual(hint.normalized_label, "synthetic stream")
        self.assertEqual(hint.label_source, "counterparty_name")
        self.assertEqual((hint.amount, hint.currency), ("49.99", "PLN"))
        self.assertEqual((hint.occurrence_count, hint.distinct_months), (MIN_OCCURRENCES, MIN_OCCURRENCES))
        self.assertEqual((hint.first_date, hint.last_date), ("2026-01-15", "2026-03-15"))
        self.assertEqual((hint.min_gap_days, hint.max_gap_days), (28, 31))
        self.assertEqual(hint.account_ids, ("acc-1",))
        self.assertEqual(
            hint.evidence,
            (
                RecurringOccurrence("acc-1", "r1", "2026-01-15", _fp("acc-1:r1")),
                RecurringOccurrence("acc-1", "r2", "2026-02-15", _fp("acc-1:r2")),
                RecurringOccurrence("acc-1", "r3", "2026-03-15", _fp("acc-1:r3")),
            ),
        )

    def test_handles_month_end_clamping_leap_year_and_weekend_shifts_at_exact_boundaries(self):
        # Month-end clamping across leap year Feb 29 and non-leap year Feb 28.
        leap = [
            _entry("l1", "2024-01-31"),
            _entry("l2", "2024-02-29"),
            _entry("l3", "2024-03-31"),
            _entry("l4", "2024-04-30"),
        ]
        hints = detect_recurring_charges("owner", leap, {"acc-1"})
        self.assertEqual(len(hints), 1)
        self.assertEqual((hints[0].occurrence_count, hints[0].min_gap_days, hints[0].max_gap_days), (4, 29, 31))

        # Exact gap boundaries: 23 days and 38 days pass; 22 days and 39 days reject/split.
        exact_bounds = [
            _entry("b1", "2026-01-20"),
            _entry("b2", "2026-02-12"),  # +23 days
            _entry("b3", "2026-03-22"),  # +38 days
        ]
        bounded = detect_recurring_charges("owner", exact_bounds, {"acc-1"})
        self.assertEqual(len(bounded), 1)
        self.assertEqual((bounded[0].min_gap_days, bounded[0].max_gap_days), (MIN_GAP_DAYS, MAX_GAP_DAYS))

        too_close = [_entry("c1", "2026-01-20"), _entry("c2", "2026-02-11"), _entry("c3", "2026-03-15")]
        self.assertEqual(detect_recurring_charges("owner", too_close, {"acc-1"}), ())

        too_far = [_entry("f1", "2026-01-10"), _entry("f2", "2026-02-18"), _entry("f3", "2026-03-20")]
        self.assertEqual(detect_recurring_charges("owner", too_far, {"acc-1"}), ())

    def test_rejects_two_calendar_month_cluster_even_when_day_gaps_meet_minimum(self):
        # Jan 1 -> Jan 24 (+23d) -> Feb 16 (+23d) spans only 2 distinct calendar months.
        two_months = [
            _entry("m1", "2026-01-01"),
            _entry("m2", "2026-01-24"),
            _entry("m3", "2026-02-16"),
        ]
        self.assertEqual(detect_recurring_charges("owner", two_months, {"acc-1"}), ())

    def test_rejects_weekly_biweekly_quarterly_and_noisy_frequent_purchases(self):
        weekly = [_entry(f"w{i}", f"2026-01-{1 + i * 7:02d}") for i in range(4)]
        biweekly = [_entry("bw1", "2026-01-01"), _entry("bw2", "2026-01-15"),
                    _entry("bw3", "2026-01-29"), _entry("bw4", "2026-02-12")]
        quarterly = [_entry("q1", "2026-01-15"), _entry("q2", "2026-04-15"), _entry("q3", "2026-07-15")]
        noisy = [
            _entry("n1", "2026-01-02"), _entry("n2", "2026-01-05"),
            _entry("n3", "2026-02-03"), _entry("n4", "2026-03-04"),
        ]
        for case in (weekly, biweekly, quarterly, noisy):
            self.assertEqual(detect_recurring_charges("owner", case, {"acc-1"}), ())

    def test_splits_paused_and_restarted_subscription_into_separate_runs(self):
        entries = [
            _entry("a1", "2026-01-10"), _entry("a2", "2026-02-10"), _entry("a3", "2026-03-10"),
            # 3-month pause (> 38 days), then 2-month short run (ignored), then 3-month run (detected)
            _entry("b1", "2026-06-10"), _entry("b2", "2026-07-10"),
            _entry("c1", "2026-09-10"), _entry("c2", "2026-10-10"), _entry("c3", "2026-11-10"),
        ]
        hints = detect_recurring_charges("owner", entries, {"acc-1"})
        self.assertEqual(len(hints), 2)
        self.assertEqual([(h.first_date, h.last_date) for h in hints],
                         [("2026-01-10", "2026-03-10"), ("2026-09-10", "2026-11-10")])

    def test_separates_currencies_exact_large_small_amounts_and_label_sources(self):
        large = "123456789012345678901234567890.00000001"
        entries = [
            # PLN counterparty
            _entry("p1", "2026-01-05", amount=large, currency="PLN", counterparty_name="Cafe\u0301 Cloud"),
            _entry("p2", "2026-02-05", amount=large + "00", currency="PLN", counterparty_name="Caf\u00e9 Cloud"),
            _entry("p3", "2026-03-05", amount=large, currency="PLN", counterparty_name="CAF\u00c9 CLOUD"),
            # Same merchant in EUR with only 2 occurrences -> must NOT merge with PLN
            _entry("e1", "2026-01-05", amount=large, currency="EUR", counterparty_name="Caf\u00e9 Cloud"),
            _entry("e2", "2026-02-05", amount=large, currency="EUR", counterparty_name="Caf\u00e9 Cloud"),
            # Description fallback with control chars/newlines kept separate from counterparty_name
            _entry("d1", "2026-01-05", amount="12", currency="EUR", counterparty_name=None,
                   description="Line 1\nLine 2\x00"),
            _entry("d2", "2026-02-05", amount="12.00", currency="EUR", counterparty_name="   ",
                   description="  line 1  line 2 "),
            _entry("d3", "2026-03-05", amount="012.0", currency="EUR", counterparty_name=None,
                   description="LINE 1\r\nLINE 2"),
            # Only 2 description entries matching counterparty name text -> separate label_source, not merged
            _entry("s1", "2026-04-05", amount="12", currency="EUR", counterparty_name="Line 1 Line 2"),
            _entry("s2", "2026-05-05", amount="12", currency="EUR", counterparty_name="Line 1 Line 2"),
        ]
        hints = detect_recurring_charges("owner", entries, {"acc-1"})
        self.assertEqual(len(hints), 2)
        self.assertEqual((hints[0].currency, hints[0].label_source, hints[0].amount, hints[0].label),
                         ("EUR", "description", "12", "LINE 1 LINE 2"))
        self.assertEqual((hints[1].currency, hints[1].label_source, hints[1].amount, hints[1].label),
                         ("PLN", "counterparty_name", large, "CAF\u00c9 CLOUD"))

    def test_tracks_multiple_selected_accounts_and_ignores_non_expense_zero_ineligible_and_unlabelled(self):
        entries = [
            # Valid recurring across 2 selected accounts (e.g., card changed)
            _entry("ok1", "2026-01-12", account_id="acc-2"),
            _entry("ok2", "2026-02-12", account_id="acc-1"),
            _entry("ok3", "2026-03-12", account_id="acc-2"),
            # Excluded, transfer, credit/refund/income, zero amount, ineligible, empty labels -> all ignored
            _entry("ex1", "2026-01-12", effective_kind="excluded", counterparty_name="Excluded"),
            _entry("ex2", "2026-02-12", effective_kind="excluded", counterparty_name="Excluded"),
            _entry("ex3", "2026-03-12", effective_kind="excluded", counterparty_name="Excluded"),
            _entry("tr1", "2026-01-12", effective_kind="transfer", counterparty_name="Own Transfer"),
            _entry("tr2", "2026-02-12", effective_kind="transfer", counterparty_name="Own Transfer"),
            _entry("tr3", "2026-03-12", effective_kind="transfer", counterparty_name="Own Transfer"),
            _entry("cr1", "2026-01-12", direction="CRDT", effective_kind="income", counterparty_name="Salary"),
            _entry("cr2", "2026-02-12", direction="CRDT", effective_kind="income", counterparty_name="Salary"),
            _entry("cr3", "2026-03-12", direction="CRDT", effective_kind="income", counterparty_name="Salary"),
            _entry("z1", "2026-01-12", amount="0.00", counterparty_name="Zero"),
            _entry("z2", "2026-02-12", amount="0", counterparty_name="Zero"),
            _entry("z3", "2026-03-12", amount="0.0", counterparty_name="Zero"),
            _entry("in1", "2026-01-12", eligible=False, counterparty_name="Pending"),
            _entry("in2", "2026-02-12", eligible=False, counterparty_name="Pending"),
            _entry("in3", "2026-03-12", eligible=False, counterparty_name="Pending"),
            _entry("nl1", "2026-01-12", counterparty_name=None, description="   \x00\n "),
            _entry("nl2", "2026-02-12", counterparty_name="", description=""),
            _entry("nl3", "2026-03-12", counterparty_name=None, description=""),
            # Opaque entry reference with control char is accepted cleanly
            _entry("ref\x00opaque", "2026-04-12", account_id="acc-1"),
        ]
        hints = detect_recurring_charges("owner", entries, frozenset({"acc-1", "acc-2"}))
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].occurrence_count, 4)
        self.assertEqual(hints[0].account_ids, ("acc-1", "acc-2"))
        self.assertEqual(hints[0].evidence[-1].entry_reference, "ref\x00opaque")

    def test_enforces_entry_limit_without_partial_results(self):
        entries = [_entry(f"r{i}", "2026-01-15") for i in range(4)]
        with self.assertRaises(RecurringLimitError) as raised:
            detect_recurring_charges("owner", entries, {"acc-1"}, max_entries=3)
        self.assertEqual(str(raised.exception), "Recurring charge detection limit exceeded")

    def test_validation_rejects_malformed_and_out_of_scope_inputs_without_echoing_secrets(self):
        secret_merchant = "SECRET-MERCHANT-999"
        secret_amount = "777777.88"
        valid = _entry("r1", "2026-01-15", amount=secret_amount, counterparty_name=secret_merchant)
        # repr=False prevents leaking financial fields in logs/tracebacks
        for obj in (valid, RecurringOccurrence("acc-1", "r1", "2026-01-15", _fp("x"))):
            self.assertNotIn(secret_merchant, repr(obj))
            self.assertNotIn(secret_amount, repr(obj))
        hint = detect_recurring_charges(
            "owner",
            [_entry(f"h{i}", f"2026-0{i}-15", amount=secret_amount, counterparty_name=secret_merchant) for i in (1, 2, 3)],
            {"acc-1"},
        )[0]
        self.assertNotIn(secret_merchant, repr(hint))
        self.assertNotIn(secret_amount, repr(hint))

        bad_calls = [
            lambda: detect_recurring_charges("", [], {"acc-1"}),
            lambda: detect_recurring_charges("bad\x00owner", [], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [], ["acc-1"]),
            lambda: detect_recurring_charges("owner", [], {""}),
            lambda: detect_recurring_charges("owner", [], {"acc-1"}, max_entries=True),
            lambda: detect_recurring_charges("owner", [], {"acc-1"}, max_entries=-1),
            lambda: detect_recurring_charges("owner", "not-entries", {"acc-1"}),
            lambda: detect_recurring_charges("owner", b"bytes", {"acc-1"}),
            lambda: detect_recurring_charges("owner", {}, {"acc-1"}),
            lambda: detect_recurring_charges("owner", ["not-a-dataclass"], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", owner="other")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("", "2026-01-15")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("x" * (MAX_TEXT + 1), "2026-01-15")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [valid, valid], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", eligible="yes")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", account_id="unselected")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", direction="BOTH")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", effective_kind="bogus")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", currency="pln")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", fingerprint="short")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", amount="-10")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", amount="1e5")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", amount="1" * 81)], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-13-01")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "bad-date")], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", counterparty_name=123)], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", counterparty_name="x" * (MAX_TEXT + 1))], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", counterparty_name=None, description=123)], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", counterparty_name="Valid", description=123)], {"acc-1"}),
            lambda: detect_recurring_charges("owner", [_entry("r1", "2026-01-15", counterparty_name=None, description="x" * (MAX_DESCRIPTION + 1))], {"acc-1"}),
        ]
        for index, call in enumerate(bad_calls):
            with self.subTest(index=index), self.assertRaises(RecurringInputError) as raised:
                call()
            self.assertEqual(str(raised.exception), "Invalid recurring charge input")
            self.assertNotIn(secret_merchant, str(raised.exception))
            self.assertNotIn(secret_amount, str(raised.exception))

        # Direct internal _key guard coverage for non-tuple key
        from hestia.budget.recurring import _key
        with self.assertRaises(RecurringInputError):
            _key(("only-one",))


if __name__ == "__main__":
    unittest.main()
