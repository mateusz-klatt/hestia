"""Synthetic unit tests for pure private mortgage reminder schedule evaluation."""
from __future__ import annotations

import random
import unittest

from hestia.budget.mortgage_reminders import (
    DEFAULT_REMIND_DAYS,
    MAX_PAID_MONTHS,
    MAX_SCHEDULES,
    PRIVATE_DESTINATIONS,
    RULE_VERSION,
    STATUSES,
    MortgageInputError,
    MortgageLimitError,
    MortgageReminder,
    MortgageSchedule,
    evaluate_mortgage_reminders,
)


def _schedule(schedule_id="mort-1", **changes):
    values = {
        "owner": "owner",
        "schedule_id": schedule_id,
        "label": "Synthetic home loan",
        "amount": "2450.50",
        "currency": "PLN",
        "due_day": 15,
        "start_date": "2026-01-01",
        "destination_kind": "owner_inbox",
        "destination_target": "inbox:owner-private",
        "end_date": None,
        "remind_days_before": DEFAULT_REMIND_DAYS,
        "paid_months": frozenset(),
        "enabled": True,
    }
    values.update(changes)
    return MortgageSchedule(**values)


class MortgageRemindersTests(unittest.TestCase):
    def test_all_five_statuses_paid_skip_and_freshness_indicators(self):
        schedules = [
            _schedule("s-overdue", due_day=15, amount="002450.50"),
            _schedule("s-today", due_day=21, remind_days_before=0, destination_kind="owner_push"),
            _schedule("s-soon", due_day=25, remind_days_before=5, destination_kind="owner_email"),
            _schedule("s-upcoming", due_day=28, remind_days_before=5),
            _schedule("s-paid-advances", due_day=1, remind_days_before=12, paid_months=frozenset({"2026-11"})),
            _schedule("s-completed", due_day=15, end_date="2026-11-15", paid_months=frozenset({"2026-11"})),
            _schedule("s-disabled", due_day=10, enabled=False),
        ]
        fresh = evaluate_mortgage_reminders("owner", "2026-11-21", (s for s in schedules), ledger_as_of="2026-11-14")
        by_id = {item.schedule_id: item for item in fresh}
        self.assertEqual(set(item.status for item in fresh), set(STATUSES))
        self.assertNotIn("s-disabled", by_id)
        self.assertEqual([item.schedule_id for item in fresh],
                         ["s-overdue", "s-today", "s-soon", "s-upcoming", "s-paid-advances", "s-completed"])

        self.assertEqual((by_id["s-overdue"].due_date, by_id["s-overdue"].days_until_due,
                          by_id["s-overdue"].status, by_id["s-overdue"].reminder_active, by_id["s-overdue"].amount),
                         ("2026-11-15", -6, "overdue", True, "2450.5"))
        self.assertEqual((by_id["s-today"].due_date, by_id["s-today"].days_until_due,
                          by_id["s-today"].status, by_id["s-today"].reminder_active),
                         ("2026-11-21", 0, "due_today", True))
        self.assertEqual((by_id["s-soon"].due_date, by_id["s-soon"].days_until_due,
                          by_id["s-soon"].status, by_id["s-soon"].reminder_active),
                         ("2026-11-25", 4, "due_soon", True))
        self.assertEqual((by_id["s-upcoming"].due_date, by_id["s-upcoming"].days_until_due,
                          by_id["s-upcoming"].status, by_id["s-upcoming"].reminder_active),
                         ("2026-11-28", 7, "upcoming", False))
        self.assertEqual((by_id["s-paid-advances"].due_date, by_id["s-paid-advances"].days_until_due,
                          by_id["s-paid-advances"].status, by_id["s-paid-advances"].reminder_active),
                         ("2026-12-01", 10, "due_soon", True))
        self.assertEqual((by_id["s-completed"].due_date, by_id["s-completed"].days_until_due,
                          by_id["s-completed"].status, by_id["s-completed"].reminder_active,
                          by_id["s-completed"].rule_version),
                         (None, None, "completed", False, RULE_VERSION))
        self.assertTrue(all(not item.freshness_stale and item.ledger_as_of == "2026-11-14" for item in fresh))

        stale_old = evaluate_mortgage_reminders("owner", "2026-11-21", [schedules[0]], ledger_as_of="2026-11-13")
        self.assertTrue(stale_old[0].freshness_stale)
        fresh_zero = evaluate_mortgage_reminders(
            "owner", "2026-11-21", [schedules[0]], ledger_as_of="2026-11-21", stale_after_days=0
        )
        self.assertFalse(fresh_zero[0].freshness_stale)
        stale_zero = evaluate_mortgage_reminders(
            "owner", "2026-11-21", [schedules[0]], ledger_as_of="2026-11-20", stale_after_days=0
        )
        self.assertTrue(stale_zero[0].freshness_stale)
        stale_none = evaluate_mortgage_reminders("owner", "2026-11-21", [schedules[0]], ledger_as_of=None)
        self.assertTrue(stale_none[0].freshness_stale)
        self.assertIsNone(stale_none[0].ledger_as_of)

    def test_month_end_clamping_leap_year_future_start_and_year_boundaries(self):
        feb_non_leap = evaluate_mortgage_reminders("owner", "2026-02-26", [_schedule(due_day=31, remind_days_before=3)])
        self.assertEqual((feb_non_leap[0].due_date, feb_non_leap[0].days_until_due, feb_non_leap[0].status),
                         ("2026-02-28", 2, "due_soon"))

        feb_leap = evaluate_mortgage_reminders("owner", "2028-02-26", [_schedule(due_day=31, remind_days_before=3)])
        self.assertEqual((feb_leap[0].due_date, feb_leap[0].days_until_due, feb_leap[0].status),
                         ("2028-02-29", 3, "due_soon"))

        # Century and 400-year leap clamping plus 30-day month clamping and exact lead-time boundary.
        for today, start, expected_due in (
            ("2000-02-25", "2000-01-01", "2000-02-29"),
            ("2024-02-25", "2024-01-01", "2024-02-29"),
            ("2100-02-25", "2100-01-01", "2100-02-28"),
            ("2026-04-25", "2026-01-01", "2026-04-30"),
        ):
            clamped = evaluate_mortgage_reminders("owner", today, [_schedule(due_day=31, start_date=start)])
            self.assertEqual(clamped[0].due_date, expected_due)

        edge_soon = evaluate_mortgage_reminders("owner", "2026-11-10", [_schedule(due_day=15, remind_days_before=5)])
        edge_upcoming = evaluate_mortgage_reminders("owner", "2026-11-09", [_schedule(due_day=15, remind_days_before=5)])
        self.assertEqual((edge_soon[0].status, edge_soon[0].reminder_active), ("due_soon", True))
        self.assertEqual((edge_upcoming[0].status, edge_upcoming[0].reminder_active), ("upcoming", False))

        # Valid paid_months outside the schedule [start_date, end_date] window are safely ignored.
        out_of_window_paid = evaluate_mortgage_reminders(
            "owner", "2026-06-10",
            [_schedule(due_day=15, start_date="2026-06-01", end_date="2026-08-31",
                       paid_months=frozenset({"2025-12", "2026-05", "2026-09", "2030-01"}))]
        )
        self.assertEqual((out_of_window_paid[0].due_date, out_of_window_paid[0].status), ("2026-06-15", "due_soon"))

        year_roll = evaluate_mortgage_reminders(
            "owner", "2026-12-20", [_schedule(due_day=15, paid_months=frozenset({"2026-12"}))]
        )
        self.assertEqual((year_roll[0].due_date, year_roll[0].status), ("2027-01-15", "upcoming"))

        future_start = evaluate_mortgage_reminders(
            "owner", "2026-11-01", [_schedule(due_day=10, start_date="2026-11-20", remind_days_before=31)]
        )
        self.assertEqual((future_start[0].due_date, future_start[0].status), ("2026-12-10", "upcoming"))

        empty_window = evaluate_mortgage_reminders(
            "owner", "2026-11-01", [_schedule(due_day=10, start_date="2026-11-20", end_date="2026-11-25")]
        )
        self.assertEqual((empty_window[0].due_date, empty_window[0].status), (None, "completed"))

        past_end = evaluate_mortgage_reminders(
            "owner", "2026-12-01", [_schedule(due_day=15, end_date="2026-11-30")]
        )
        self.assertEqual((past_end[0].due_date, past_end[0].status), (None, "completed"))

        max_year = evaluate_mortgage_reminders(
            "owner", "9999-12-20",
            [_schedule(due_day=15, start_date="9999-12-01", paid_months=frozenset({"9999-12"}))]
        )
        self.assertEqual((max_year[0].due_date, max_year[0].status), (None, "completed"))

    def test_exact_large_tiny_amounts_currency_tiebreak_and_permutation_invariance(self):
        enormous = "9" * 77 + ".25"
        tiny = "0." + "0" * 77 + "1"
        schedules = [
            _schedule("m-pln", amount=enormous, currency="PLN", due_day=25),
            _schedule("m-eur", amount=tiny, currency="EUR", due_day=25),
            _schedule("m-eur-2", amount="100", currency="EUR", due_day=25),
        ]
        baseline = evaluate_mortgage_reminders("owner", "2026-11-21", schedules)
        self.assertEqual([(item.currency, item.schedule_id) for item in baseline],
                         [("EUR", "m-eur"), ("EUR", "m-eur-2"), ("PLN", "m-pln")])
        self.assertEqual((baseline[0].amount, baseline[2].amount), (tiny, enormous))
        rng = random.Random(20261121)
        for _ in range(25):
            perm = list(schedules)
            rng.shuffle(perm)
            self.assertEqual(evaluate_mortgage_reminders("owner", "2026-11-21", perm), baseline)

    def test_shared_calendars_and_invalid_inputs_fail_closed_with_fixed_messages(self):
        self.assertEqual(PRIVATE_DESTINATIONS, frozenset({"owner_inbox", "owner_push", "owner_email"}))
        bad_calls = [
            lambda: evaluate_mortgage_reminders(" ", "2026-11-21", []),
            lambda: evaluate_mortgage_reminders(" " * 1025, "2026-11-21", []),
            lambda: evaluate_mortgage_reminders("owner\t", "2026-11-21", []),
            lambda: evaluate_mortgage_reminders("owner", "2026-02-29", []),
            lambda: evaluate_mortgage_reminders("owner", "1899-12-31", []),
            lambda: evaluate_mortgage_reminders("owner", 20261121, []),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [], ledger_as_of="2026-11-22"),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [], stale_after_days=True),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [], stale_after_days=-1),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [], stale_after_days=366),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [], max_schedules=True),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [], max_schedules=-1),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", "not-iterable"),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", {"a": 1}),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", ["bad"]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(owner="other")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(destination_kind="shared_calendar")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21",
                                                [_schedule(destination_kind="shared_calendar", enabled=False)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(destination_kind="calendar")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(destination_target=" ")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(schedule_id="x" * 129)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(label="x" * 257)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(currency="pln")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(amount="0")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(amount="0.00")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(amount="-10")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(amount="1" * 81)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(due_day=True)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(due_day=0)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(due_day=32)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(remind_days_before=True)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(remind_days_before=-1)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(remind_days_before=32)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(enabled=1)]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21",
                                                [_schedule(start_date="2026-06-01", end_date="2026-05-31")]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(paid_months=["2026-11"])]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(paid_months=frozenset({"2026-13"}))]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(paid_months=frozenset({"2026"}))]),
            lambda: evaluate_mortgage_reminders("owner", "2026-11-21",
                                                [_schedule("dup"), _schedule("dup", enabled=False)]),
        ]
        for call in bad_calls:
            with self.assertRaises(MortgageInputError) as exc:
                call()
            self.assertEqual(str(exc.exception), "Invalid mortgage reminder input")

        with self.assertRaises(MortgageLimitError) as exc:
            evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule("a"), _schedule("b")], max_schedules=1)
        self.assertEqual(str(exc.exception), "Mortgage reminder limit exceeded")

        self.assertEqual(evaluate_mortgage_reminders("owner", "2026-11-21", [], max_schedules=0), ())
        at_cap = [_schedule(f"s-{i:03d}") for i in range(MAX_SCHEDULES)]
        self.assertEqual(len(evaluate_mortgage_reminders("owner", "2026-11-21", at_cap)), MAX_SCHEDULES)
        with self.assertRaises(MortgageLimitError):
            evaluate_mortgage_reminders("owner", "2026-11-21", [*at_cap, _schedule("s-over")])

        at_paid_cap = frozenset(f"{2000 + i // 12:04d}-{(i % 12) + 1:02d}" for i in range(MAX_PAID_MONTHS))
        self.assertEqual(len(evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(paid_months=at_paid_cap)])), 1)
        many_paid = at_paid_cap | {"2100-01"}
        with self.assertRaises(MortgageLimitError):
            evaluate_mortgage_reminders("owner", "2026-11-21", [_schedule(paid_months=many_paid)])

    def test_repr_redacts_all_private_financial_fields(self):
        sched = _schedule(
            "mort-secret-42",
            owner="alice-secret",
            label="Private Home Loan 42",
            amount="3456.78",
            destination_target="private-email-token-99",
        )
        reminder: MortgageReminder = evaluate_mortgage_reminders("alice-secret", "2026-11-21", [sched])[0]
        combined = repr(sched) + repr(reminder)
        for token in ("alice-secret", "mort-secret-42", "Private Home Loan 42", "3456.78", "private-email-token-99"):
            self.assertNotIn(token, combined)


if __name__ == "__main__":
    unittest.main()
