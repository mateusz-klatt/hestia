"""Forecast calendar, exact amounts, limits and instalment-level matching."""

from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
import random
import unittest
from unittest.mock import patch

from hestia.budget import forecast
from hestia.budget.config import BudgetConfigError
from hestia.budget.mortgage_reminders import MortgageSchedule
from hestia.budget.recurring import RecurringEntry, RecurringHint, RecurringOccurrence, detect_recurring_charges


def _stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def _schedule(schedule_id, **fields):
    schedule = MortgageSchedule(owner="test-owner", schedule_id=schedule_id, label=schedule_id,
                                amount="100", currency="PLN", due_day=15, start_date="2026-10-01",
                                destination_kind="owner_inbox")
    return replace(schedule, **fields)


def _hint(label, **fields):
    hint = RecurringHint(label=label, normalized_label=label.casefold(), label_source="counterparty_name",
                         amount="100", currency="PLN", cadence="monthly", occurrence_count=3,
                         distinct_months=3, first_date="2026-08-15", last_date="2026-10-15",
                         min_gap_days=30, max_gap_days=31, account_ids=("source",), evidence=())
    return replace(hint, **fields)


def _observed_hints(days, *, label=None, description="", amount="100"):
    entries = [RecurringEntry("test-owner", "source", str(index), amount, "PLN", "DBIT", "expense", day,
                              "f" * 64, counterparty_name=label, description=description)
               for index, day in enumerate(days)]
    return detect_recurring_charges("test-owner", entries, {"source"})


def _match_reference(token):
    return {"hash": sha256(token.encode("ascii")).hexdigest(), "last4": token[-4:]}


def _forecast(**overrides):
    evaluation = overrides.get("evaluation_date", "2026-11-22")
    defaults = dict(owner="test-owner", evaluation_date=evaluation, months=3, currency=None,
                    account_timestamps={"source": (_stamp(evaluation), None)}, excluded_accounts=0,
                    schedules=(), hints=(), snapshot_digest="d" * 64, schedules_version="a" * 64)
    return forecast.evaluate_forecast(**(defaults | overrides))


class BudgetForecastTests(unittest.TestCase):
    def test_changed_and_variable_monthly_amounts_project_only_the_latest_amount(self):
        for amounts in (("93", "93", "93", "107", "107", "107"),
                        ("100.01", "127.19", "99.07", "103.21", "120", "89.9900")):
            with self.subTest(amounts=amounts):
                entries = [RecurringEntry("test-owner", "source", str(month), amount, "PLN", "DBIT", "expense",
                                          f"2026-{month:02d}-10", "f" * 64, counterparty_name="Synthetic Monthly Bill")
                           for month, amount in enumerate(amounts, 5)]
                hints = detect_recurring_charges("test-owner", entries, {"source"})
                self.assertEqual(len(hints), 1)
                expected_amount = "107" if amounts[0] == "93" else "89.99"
                # Stale accounts also retain the single history instead of
                # projecting both old and new price bands independently.
                result = _forecast(evaluation_date="2026-11-01", months=2, hints=hints,
                                   account_timestamps={"source": (None, None)})
                self.assertEqual([len(month["items"]) for month in result["months"]], [1, 1])
                self.assertEqual([month["items"][0]["amount"] for month in result["months"]],
                                 [expected_amount, expected_amount])

    def test_parallel_same_merchant_lanes_and_alternating_accounts_do_not_doublecount_or_fabricate_cycles(self):
        # Report (1): same-name lane day 10 changes 93->107, separate 50 PLN lane day 20 -> stale projects 157, not 250.
        for same_account in (True, False):
            with self.subTest(same_account=same_account):
                entries = []
                second_account = "source" if same_account else "other"
                accounts = {"source"} if same_account else {"source", "other"}
                for month in range(1, 7):
                    entries.append(RecurringEntry(
                        "test-owner", "source", f"d10-{month}", "93" if month <= 3 else "107",
                        "PLN", "DBIT", "expense", f"2026-{month:02d}-10", "f" * 64,
                        counterparty_name="Synthetic Stream",
                    ))
                    entries.append(RecurringEntry(
                        "test-owner", second_account, f"d20-{month}", "50",
                        "PLN", "DBIT", "expense", f"2026-{month:02d}-20", "f" * 64,
                        counterparty_name="Synthetic Stream",
                    ))
                hints = detect_recurring_charges("test-owner", entries, accounts)
                self.assertEqual([(h.amount, h.occurrence_count) for h in hints], [("107", 6), ("50", 6)])
                timestamps = {account: (None, None) for account in accounts}
                result = _forecast(evaluation_date="2026-07-01", months=2, hints=hints, account_timestamps=timestamps)
                self.assertEqual(result["totals"], [
                    {"currency": "PLN", "total": "314", "scheduled_total": "0", "estimated_total": "314"},
                ])
                self.assertEqual([(item["amount"], item["expected_date"], item["confidence"])
                                  for item in result["months"][0]["items"]], [
                    ("107", "2026-07-10", "high"),
                    ("50", "2026-07-20", "high"),
                ])

        # Variable utility bill on day 10 + 50 PLN lane on day 20 on stale account preserves latest actual amount.
        utility_entries = [
            RecurringEntry("test-owner", "source", "u1", "773.42", "PLN", "DBIT", "expense", "2026-01-10", "f" * 64,
                           counterparty_name="Synthetic Energy"),
            RecurringEntry("test-owner", "source", "u2", "829.68", "PLN", "DBIT", "expense", "2026-02-10", "f" * 64,
                           counterparty_name="Synthetic Energy"),
            RecurringEntry("test-owner", "source", "u3", "815.00", "PLN", "DBIT", "expense", "2026-03-10", "f" * 64,
                           counterparty_name="Synthetic Energy"),
            RecurringEntry("test-owner", "source", "f1", "50", "PLN", "DBIT", "expense", "2026-01-20", "f" * 64,
                           counterparty_name="Synthetic Energy"),
            RecurringEntry("test-owner", "source", "f2", "50", "PLN", "DBIT", "expense", "2026-02-20", "f" * 64,
                           counterparty_name="Synthetic Energy"),
            RecurringEntry("test-owner", "source", "f3", "50", "PLN", "DBIT", "expense", "2026-03-20", "f" * 64,
                           counterparty_name="Synthetic Energy"),
        ]
        utility_hints = detect_recurring_charges("test-owner", utility_entries, {"source"})
        utility_result = _forecast(evaluation_date="2026-04-01", months=1, hints=utility_hints,
                                   account_timestamps={"source": (None, None)})
        self.assertEqual(utility_result["totals"], [
            {"currency": "PLN", "total": "865", "scheduled_total": "0", "estimated_total": "865"},
        ])

        # Report (2): alternating months across two accounts must not fabricate a monthly high-confidence hint.
        for amounts in (("93", "93", "107", "107", "107", "107"), ("107",) * 6):
            with self.subTest(alternating=amounts):
                alt_entries = [
                    RecurringEntry("test-owner", "acc-1" if month % 2 == 1 else "acc-2", f"alt-{month}",
                                   amount, "PLN", "DBIT", "expense", f"2026-{month:02d}-10", "f" * 64,
                                   counterparty_name="Synthetic Stream")
                    for month, amount in enumerate(amounts, 1)
                ]
                alt_hints = detect_recurring_charges("test-owner", alt_entries, {"acc-1", "acc-2"})
                self.assertEqual(alt_hints, ())
                alt_result = _forecast(evaluation_date="2026-07-01", months=1, hints=alt_hints,
                                       account_timestamps={"acc-1": (_stamp("2026-07-01"), None),
                                                           "acc-2": (_stamp("2026-07-01"), None)})
                self.assertEqual(alt_result["totals"], [])
                self.assertEqual(alt_result["months"][0]["items"], [])

    def test_identical_hints_on_distinct_accounts_stay_order_invariant_and_dedup_independently(self):
        entries = [
            RecurringEntry("test-owner", account, f"{account}-{month}", "107", "PLN", "DBIT", "expense",
                           f"2026-{month:02d}-10", "f" * 64, counterparty_name="Synthetic Stream")
            for account in ("acc-1", "acc-2") for month in range(1, 7)
        ]
        hints = detect_recurring_charges("test-owner", entries, {"acc-1", "acc-2"})
        self.assertEqual(len(hints), 2)
        timestamps = {"acc-1": (None, None), "acc-2": (_stamp("2026-07-01"), None)}

        both = _forecast(evaluation_date="2026-07-01", months=1, hints=hints, account_timestamps=timestamps)
        self.assertEqual(both, _forecast(evaluation_date="2026-07-01", months=1, hints=hints[::-1], account_timestamps=timestamps))
        self.assertEqual([(item["amount"], item["account_stale"], item["confidence"])
                          for item in both["months"][0]["items"]], [
            ("107", True, "high"),
            ("107", False, "high"),
        ])
        self.assertEqual(both["totals"], [
            {"currency": "PLN", "total": "214", "scheduled_total": "0", "estimated_total": "214"},
        ])

        # One matching mortgage schedule suppresses only one account's hint, preserving the other.
        schedule = _schedule("loan", amount="107", due_day=10, start_date="2026-01-01")
        deduped = _forecast(evaluation_date="2026-07-01", months=1, schedules=[schedule],
                            hints=hints, account_timestamps=timestamps)
        self.assertEqual(deduped, _forecast(evaluation_date="2026-07-01", months=1, schedules=[schedule],
                                            hints=hints[::-1], account_timestamps=timestamps))
        self.assertEqual(deduped["summary"]["deduped_recurring_count"], 1)
        self.assertEqual([(item["source"], item["amount"], item["account_stale"])
                          for item in deduped["months"][0]["items"]], [
            ("mortgage", "107", False),
            ("recurring", "107", False),
        ])
        self.assertEqual(deduped["totals"], [
            {"currency": "PLN", "total": "214", "scheduled_total": "107", "estimated_total": "107"},
        ])

        # Category conflict assignment keeps identical hints on distinct accounts separate.
        evidence = {
            ("acc-1", "acc-1-6"): "cat-a",
            ("acc-2", "acc-2-6"): "cat-b",
        }
        self.assertEqual(
            forecast._hint_categories(hints, evidence, {"cat-a", "cat-b"}),
            {
                forecast._hint_identity(hints[0]): frozenset({"cat-a"}),
                forecast._hint_identity(hints[1]): frozenset({"cat-b"}),
            },
        )

    def test_query_defaults_and_exact_syntax(self):
        self.assertEqual(forecast.validate_forecast_query(None, None), (3, None))
        self.assertEqual(forecast.validate_forecast_query("01", "PLN"), (1, "PLN"))
        self.assertEqual(forecast.validate_forecast_query("12", "EUR"), (12, "EUR"))
        for value in ("", "0", "13", "001", "+1", "1\n", "1.0", "１２", 3, True):
            with self.subTest(months=value), self.assertRaisesRegex(forecast.ForecastInputError, "^Invalid forecast query$"):
                forecast.validate_forecast_query(value, None)
        for value in ("", "pln", "EUR\n", "ÉUR", 3):
            with self.subTest(currency=value), self.assertRaisesRegex(forecast.ForecastInputError, "^Invalid forecast query$"):
                forecast.validate_forecast_query(None, value)

    def test_month_end_clamping_and_uncapped_exact_decimal_arithmetic_per_currency(self):
        principal = "9" * 70 + ".1234567"
        for year, feb_end in ((2026, 28), (2028, 29), (2100, 28)):
            with self.subTest(year=year):
                evaluation = f"{year}-01-31"
                schedules = [_schedule("pln", amount=principal + "0", due_day=31, start_date=f"{year}-01-01"),
                             _schedule("eur", amount="01.20", currency="EUR", due_day=31, start_date=f"{year}-01-01")]
                hint = _hint("month end", amount="0.87654330", last_date=evaluation, occurrence_count=6,
                             min_gap_days=24, max_gap_days=31)
                result = _forecast(evaluation_date=evaluation, months=4, schedules=schedules, hints=[hint])
                self.assertEqual(result["horizon_months"], 4)
                self.assertEqual((result["start_month"], result["end_month"]), (f"{year}-01", f"{year}-04"))
                self.assertEqual(result["evaluation_timezone"], "UTC")
                self.assertEqual([len(month["items"]) for month in result["months"]], [2, 3, 3, 3])
                for month, days in zip(result["months"], (31, feb_end, 31, 30)):
                    self.assertEqual({item["expected_date"] for item in month["items"]}, {f"{month['month']}-{days}"})
                self.assertEqual(result["months"][1]["currencies"], [
                    {"currency": "EUR", "total": "1.2", "scheduled_total": "1.2", "estimated_total": "0"},
                    {"currency": "PLN", "total": "1" + "0" * 70,
                     "scheduled_total": principal, "estimated_total": "0.8765433"},
                ])
                self.assertEqual(result["totals"], [
                    {"currency": "EUR", "total": "4.8", "scheduled_total": "4.8", "estimated_total": "0"},
                    {"currency": "PLN", "total": str(4 * 10 ** 70 - 1) + ".1234567",
                     "scheduled_total": str(4 * 10 ** 70 - 4) + ".4938268", "estimated_total": "2.6296299"},
                ])
                self.assertEqual(result["months"][1]["items"][-1]["confidence"], "high")
                self.assertFalse(result["months"][0]["items"][0]["past_due"])

    def test_paid_ended_disabled_and_unmarked_schedules(self):
        schedules = [
            _schedule("full", due_day=10, paid_months=frozenset({"2026-10", "2026-11", "2026-12", "2027-01"})),
            _schedule("mixed", amount="0400.00", paid_months=frozenset({"2026-10", "2026-12"})),
            _schedule("ended", amount="200", due_day=31, end_date="2026-10-31", paid_months=frozenset({"2026-10"})),
            _schedule("disabled", amount="300", due_day=20, enabled=False),
            _schedule("m0-only", amount="500", start_date="2026-11-01", end_date="2026-11-30"),
        ]
        hints = [_hint("full debit", last_date="2026-10-10"),
                 _hint("mixed debit", amount="400"),
                 _hint("ended debit", amount="200", last_date="2026-11-04"),
                 _hint("disabled debit", amount="300", last_date="2026-10-20")]
        result = _forecast(schedules=schedules, hints=hints, account_timestamps={"source": (None, None)})
        self.assertEqual(result["summary"]["overdue_mortgage_schedule_count"], 0)
        self.assertEqual(result["summary"]["deduped_recurring_count"], 3)
        self.assertEqual([row["schedule_id"] for row in result["deduplications"]], ["full", "mixed", "ended"])
        self.assertEqual(result["deduplications"][-1], {
            "schedule_id": "ended", "schedule_label": "ended", "recurring_label": "ended debit", "amount": "200",
            "currency": "PLN", "matched_due_date": "2026-10-31", "last_observed_date": "2026-11-04",
            "day_distance": 4, "account_stale": True,
        })
        self.assertEqual([month["month"] for month in result["months"]], ["2026-11", "2026-12", "2027-01"])
        self.assertEqual([[item["label"] for item in month["items"]] for month in result["months"]],
                         [["m0-only", "mixed", "disabled debit"], ["disabled debit"], ["mixed", "disabled debit"]])
        november = result["months"][0]
        self.assertEqual(november["currencies"], [
            {"currency": "PLN", "total": "1200", "scheduled_total": "900", "estimated_total": "300"},
        ])
        self.assertEqual(november["items"][1], {
            "source": "mortgage", "schedule_id": "mixed", "label": "mixed", "amount": "400", "currency": "PLN",
            "expected_date": "2026-11-15", "confidence": "scheduled", "past_due": True, "account_stale": False,
        })
        self.assertEqual(november["items"][2], {
            "source": "recurring", "schedule_id": None, "label": "disabled debit", "amount": "300", "currency": "PLN",
            "expected_date": "2026-11-20", "confidence": "medium", "past_due": False, "account_stale": True,
        })

    def test_prior_overdue_instalments_count_once_per_schedule(self):
        schedules = [_schedule("prior", start_date="2026-08-01"), _schedule("m0", start_date="2026-11-01")]
        result = _forecast(schedules=schedules, months=1)
        self.assertEqual(result["summary"]["overdue_mortgage_schedule_count"], 1)
        self.assertEqual(result["totals"][0]["scheduled_total"], "200")
        self.assertEqual([row["past_due"] for row in result["months"][0]["items"]], [True, True])

    def test_freshness_uses_per_account_max_utc_boundaries_and_skew_clamp(self):
        before_boundary = _stamp("2026-11-14T23:59:59.9")
        boundary = _stamp("2026-11-15T00:00:00")
        timestamps = {"tx-wins": (before_boundary, boundary), "component-wins": (boundary, before_boundary),
                      "skew": (None, _stamp("2026-11-24")), "old": (before_boundary, None), "never": (None, None)}
        result = _forecast(account_timestamps=timestamps, excluded_accounts=2)
        self.assertEqual(result["ledger_as_of"], "2026-11-22")
        self.assertTrue(result["freshness_stale"])
        self.assertEqual(result["stale_account_ids"], ["never", "old"])
        self.assertFalse(result["stale_account_ids_truncated"])
        self.assertEqual(result["summary"]["selected_accounts"], 5)
        self.assertEqual(result["summary"]["excluded_accounts"], 2)
        self.assertEqual(result["summary"]["stale_accounts"], 2)
        result = _forecast(account_timestamps={"fresh": (boundary, None)})
        self.assertEqual(result["ledger_as_of"], "2026-11-15")
        self.assertFalse(result["freshness_stale"])

    def test_no_freshness_when_never_synced_or_no_selected_accounts(self):
        for timestamps in ({}, {"never": (None, None)}):
            with self.subTest(timestamps=timestamps):
                result = _forecast(account_timestamps=timestamps)
                self.assertIsNone(result["ledger_as_of"])
                self.assertTrue(result["freshness_stale"])
                self.assertEqual(result["stale_account_ids"], sorted(timestamps))
                self.assertEqual(result["totals"], [])
                self.assertEqual(result["deduplications"], [])
                self.assertEqual(result["months"], [{"month": month, "currencies": [], "items": []}
                                                    for month in ("2026-11", "2026-12", "2027-01")])

    def test_fresh_and_stale_hint_lapse_at_45_day_boundary_and_currency_scoping(self):
        schedules = [_schedule("pln", due_day=7), _schedule("eur", currency="EUR", due_day=7)]
        hints = [_hint("age 45", amount="7", last_date="2026-10-08", occurrence_count=6,
                       min_gap_days=23, max_gap_days=31),
                 _hint("fresh age 46", amount="8", last_date="2026-10-07"),
                 _hint("stale age 46", amount="9", last_date="2026-10-07", account_ids=("stale",)),
                 _hint("mixed age 46", last_date="2026-10-07", account_ids=("source", "stale")),
                 _hint("mixed EUR age 46", last_date="2026-10-07", currency="EUR", account_ids=("source", "stale")),
                 _hint("fresh EUR age 46", amount="8", last_date="2026-10-07", currency="EUR"),
                 _hint("current month", amount="10", last_date="2026-11-01")]
        timestamps = {"source": (_stamp("2026-11-22"), None), "stale": (None, None)}
        result = _forecast(schedules=schedules, hints=hints, account_timestamps=timestamps, currency="PLN")
        self.assertEqual(result["summary"], {
            "selected_accounts": 2, "excluded_accounts": 0, "stale_accounts": 1,
            "overdue_mortgage_schedule_count": 1, "lapsed_recurring_count": 1,
            "deduped_recurring_count": 1, "stale_recurring_count": 2, "baseline_conflicts": [],
        })
        self.assertEqual(result["stale_account_ids"], ["stale"])
        self.assertTrue(result["deduplications"][0]["account_stale"])
        self.assertEqual(result["deduplications"][0]["recurring_label"], "mixed age 46")
        current_items = {item["label"]: item for item in result["months"][0]["items"]}
        self.assertEqual(set(current_items), {"pln", "age 45", "stale age 46"})
        self.assertFalse(current_items["pln"]["account_stale"])
        self.assertTrue(current_items["pln"]["past_due"])
        self.assertTrue(current_items["stale age 46"]["account_stale"])
        self.assertFalse(current_items["stale age 46"]["past_due"])
        self.assertEqual(current_items["age 45"]["confidence"], "medium")
        self.assertFalse(current_items["age 45"]["account_stale"])
        self.assertIn("current month", [item["label"] for item in result["months"][1]["items"]])
        self.assertEqual([row["currency"] for row in result["totals"]], ["PLN"])

    def test_stale_id_cap_keeps_last_account_active_internally(self):
        timestamps = {f"acct-{index:03d}": (None, None) for index in reversed(range(201))}
        hint = _hint("last stale", last_date="2026-10-07", account_ids=("acct-200",))
        result = _forecast(account_timestamps=timestamps, hints=[hint], months=1)
        self.assertEqual(result["stale_account_ids"], sorted(timestamps)[:200])
        self.assertTrue(result["stale_account_ids_truncated"])
        self.assertEqual(result["summary"]["stale_accounts"], 201)
        self.assertEqual(result["summary"]["selected_accounts"], 201)
        self.assertEqual(result["summary"]["lapsed_recurring_count"], 0)
        self.assertEqual(result["summary"]["stale_recurring_count"], 1)
        self.assertEqual([item["account_stale"] for item in result["months"][0]["items"]], [True])

    def test_two_detected_loan_clusters_match_distinct_historical_instalments(self):
        hints = _observed_hints([f"2026-{month:02d}-15" for month in (1, 2, 3, 6, 7, 8)], label="loan debit")
        self.assertEqual(len(hints), 2)
        result = _forecast(evaluation_date="2026-10-15", account_timestamps={"source": (None, None)}, hints=hints,
                           schedules=[_schedule("loan", start_date="2026-01-01", end_date="2026-08-31")])
        self.assertEqual(result["summary"]["stale_recurring_count"], 2)
        self.assertEqual(result["summary"]["deduped_recurring_count"], 2)
        self.assertEqual([(row["schedule_id"], row["matched_due_date"], row["account_stale"])
                          for row in result["deduplications"]],
                         [("loan", "2026-03-15", True), ("loan", "2026-08-15", True)])
        self.assertEqual(result["totals"], [])
        self.assertTrue(all(not month["items"] for month in result["months"]))

    def test_greedy_hint_that_loses_one_edge_can_still_match_another(self):
        shared = _schedule("shared", due_day=16)
        alternate = _schedule("alternate", due_day=19)
        hints = [_hint("same label", last_date="2026-10-15", label_source="counterparty_name"),
                 _hint("same label", last_date="2026-10-17", label_source="description")]
        single = _forecast(schedules=[shared], hints=hints, months=1)
        self.assertEqual([(row["schedule_id"], row["matched_due_date"], row["last_observed_date"], row["day_distance"])
                          for row in single["deduplications"]], [("shared", "2026-10-16", "2026-10-15", 1)])
        self.assertEqual([(row["source"], row["expected_date"]) for row in single["months"][0]["items"]],
                         [("mortgage", "2026-11-16"), ("recurring", "2026-11-17")])

        schedules = [shared, alternate]
        result = _forecast(schedules=schedules, hints=hints, months=1)
        self.assertEqual([(row["schedule_id"], row["matched_due_date"], row["last_observed_date"], row["day_distance"])
                          for row in result["deduplications"]],
                         [("shared", "2026-10-16", "2026-10-15", 1), ("alternate", "2026-10-19", "2026-10-17", 2)])
        self.assertEqual([row["source"] for row in result["months"][0]["items"]], ["mortgage", "mortgage"])
        self.assertEqual(_forecast(schedules=schedules[::-1], hints=hints[::-1], months=1), result)

    def test_one_hint_matches_only_one_of_two_equal_schedules(self):
        result = _forecast(hints=[_hint("tie")], schedules=[_schedule("b"), _schedule("a")], months=1)
        self.assertEqual([row["schedule_id"] for row in result["deduplications"]], ["a"])
        self.assertEqual([row["schedule_id"] for row in result["months"][0]["items"]], ["a", "b"])

    def test_linked_variable_rate_history_uses_schedule_amount_each_month(self):
        reference = "TESTLN1234"
        schedule = _schedule("loan", start_date="2026-01-01", due_day=5, amount="200",
                             match_references=[_match_reference(reference)])
        history = []
        for month, amount, delta in ((7, "200", "0"), (8, "199", "-1"), (9, "201", "1"),
                                     (10, "199", "-1"), (11, "201", "1")):
            history.append(RecurringEntry("test-owner", "source", f"txn-{month}", amount, "PLN", "DBIT", "expense",
                                          f"2026-{month:02d}-05", "f" * 64, description=f"RATA KREDYTU NR {reference}"))
            if len(history) < 3:
                continue
            with self.subTest(month=month):
                hints = detect_recurring_charges("test-owner", history, {"source"})
                result = _forecast(evaluation_date=f"2026-{month:02d}-07", schedules=[schedule], hints=hints, months=2)
                self.assertEqual([(row["match_type"], row["amount"], row["amount_delta"]) for row in result["deduplications"]],
                                 [("linked", amount, delta)])
                self.assertEqual([[(item["source"], item["amount"]) for item in row["items"]] for row in result["months"]],
                                 [[("mortgage", "200")], [("mortgage", "200")]])
                self.assertNotIn(reference, json.dumps(result))
                self.assertIn("1234", result["deduplications"][0]["recurring_label"])

    def test_links_keep_same_day_loans_separate_and_also_cover_insurance(self):
        first, second, insurance = "TESTLN1234", "TESTLN5678", "TESTINS3456"
        schedules = [
            _schedule("loan-one", due_day=5, match_references=[_match_reference(first), _match_reference(insurance)]),
            _schedule("loan-two", due_day=5, match_references=[_match_reference(second)]),
            _schedule("unlinked", due_day=5),
        ]
        hints = [_hint(f"RATA KREDYTU NR {first}", last_date="2026-10-05", amount="99.5"),
                 _hint(f"RATA KREDYTU NR {second}", last_date="2026-10-05", amount="100.5"),
                 _hint(f"UBEZPIECZENIE NR {insurance}", last_date="2026-10-10", amount="25")]
        result = _forecast(evaluation_date="2026-11-01", schedules=schedules, hints=hints, months=1)
        self.assertEqual(result, _forecast(evaluation_date="2026-11-01", schedules=schedules[::-1], hints=hints[::-1], months=1))
        self.assertEqual([(row["schedule_id"], row["match_type"], row["amount_delta"], row["day_distance"])
                          for row in result["deduplications"]],
                         [("loan-one", "linked", "-0.5", 0), ("loan-one", "linked", "-75", 5),
                          ("loan-two", "linked", "0.5", 0)])
        self.assertEqual(result["totals"], [
            {"currency": "PLN", "total": "300", "scheduled_total": "300", "estimated_total": "0"},
        ])
        for reference in (first, second, insurance):
            self.assertNotIn(reference, json.dumps(result))

    def test_link_checks_reference_currency_and_due_window(self):
        reference = "TESTLN1234"
        label = f"RATA KREDYTU NR {reference}"
        schedule = _schedule("linked", due_day=5, match_references=[_match_reference(reference)])
        for hint in (_hint(label, last_date="2026-10-11"),
                     _hint(label, last_date="2026-10-05", currency="EUR"),
                     _hint(f"RATA KREDYTU NR {reference}5", last_date="2026-10-05"),
                     _hint("unrelated monthly debit", last_date="2026-10-05")):
            with self.subTest(label=hint.label, date=hint.last_date, currency=hint.currency):
                result = _forecast(evaluation_date="2026-11-01", schedules=[schedule], hints=[hint], months=1)
                self.assertEqual(result["deduplications"], [])
                self.assertEqual({item["source"] for item in result["months"][0]["items"]}, {"mortgage", "recurring"})
                self.assertNotIn(reference, json.dumps(result))

    def test_inferred_matching_requires_loan_label_and_inclusive_amount_and_day_limits(self):
        schedule = _schedule("unlinked", due_day=5)
        cases = (
            ("rAtA   KrEdYtU", "90", "2026-10-02", "-10"),
            ("kredyt hipoteczny", "110", "2026-10-08", "10"),
            ("POZ\u0307YCZKA", "99.5", "2026-10-05", "-0.5"),
            ("Leasing", "100", "2026-10-05", "0"),
            ("kredyt", "89.99999", "2026-10-05", None),
            ("kredyt", "110.00001", "2026-10-05", None),
            ("kredyt", "100", "2026-10-09", None),
            ("przedkredyt kredytowy", "99.5", "2026-10-05", None),
        )
        for label, amount, observed, delta in cases:
            with self.subTest(label=label, amount=amount, observed=observed):
                hint = _hint(label, amount=amount, last_date=observed)
                result = _forecast(evaluation_date="2026-11-01", schedules=[schedule], hints=[hint], months=1)
                self.assertEqual([(row["match_type"], row["amount_delta"]) for row in result["deduplications"]],
                                 [] if delta is None else [("inferred", delta)])
                expected = {"mortgage", "recurring"} if delta is None else {"mortgage"}
                self.assertEqual({item["source"] for item in result["months"][0]["items"]}, expected)

    def test_ambiguous_inference_keeps_all_hints_visible(self):
        first = _schedule("loan-one", due_day=5)
        second = _schedule("loan-two", due_day=5, amount="105")
        installment = _hint("RATA KREDYTU", last_date="2026-10-05")
        leasing = _hint("LEASING", last_date="2026-10-06", amount="97")
        for schedules, hints in (([first], [installment, leasing]), ([first, second], [installment])):
            with self.subTest(schedules=len(schedules), hints=len(hints)):
                result = _forecast(evaluation_date="2026-11-01", schedules=schedules, hints=hints, months=1)
                self.assertEqual(result["deduplications"], [])
                self.assertEqual([item["label"] for item in result["months"][0]["items"] if item["source"] == "recurring"],
                                 [hint.label for hint in hints])

    def test_conflicting_links_keep_hint_visible_and_do_not_fall_back_to_inference(self):
        reference = "TESTLN1234"
        schedules = [_schedule(name, due_day=5, match_references=[_match_reference(reference)])
                     for name in ("loan-one", "loan-two")]
        schedules.append(_schedule("unlinked", due_day=5))
        hint = _hint(f"RATA KREDYTU NR {reference}", last_date="2026-10-05")
        result = _forecast(evaluation_date="2026-11-01", schedules=schedules, hints=[hint], months=1)
        self.assertEqual(result["deduplications"], [])
        self.assertEqual(len(result["months"][0]["items"]), 4)

    def test_permutations_preserve_total_order_even_when_displayed_fields_tie(self):
        schedules = [_schedule("b"), _schedule("a"), _schedule("eur", currency="EUR")]
        hints = [_hint("duplicate"),
                 _hint("same item", amount="20", label_source="description"),
                 _hint("same item", amount="20", occurrence_count=6),
                 _hint("equal distance", amount="99", last_date="2026-10-14")]
        result = _forecast(hints=hints, schedules=schedules)
        self.assertEqual([item["confidence"] for item in result["months"][0]["items"] if item["label"] == "same item"],
                         ["high", "medium"])
        rng = random.Random(191)
        for _ in range(15):
            rng.shuffle(schedules)
            rng.shuffle(hints)
            self.assertEqual(_forecast(hints=hints, schedules=schedules), result)

    def test_dedup_adjacent_months_and_supported_date_bounds(self):
        result = _forecast(evaluation_date="2026-10-22", schedules=[_schedule("next due month", due_day=1)],
                           hints=[_hint("early settlement", last_date="2026-09-26"),
                                  _hint("outside tolerance", last_date="2026-09-25")])
        self.assertEqual(len(result["deduplications"]), 1)
        self.assertEqual(result["deduplications"][0]["matched_due_date"], "2026-10-01")
        self.assertEqual(result["deduplications"][0]["day_distance"], 5)
        for month in ("1900-01", "9999-12"):
            with self.subTest(month=month):
                result = _forecast(evaluation_date=f"{month}-22", months=1,
                                   schedules=[_schedule("bounded", start_date=f"{month}-01")],
                                   hints=[_hint("debit", last_date=f"{month}-16")])
                self.assertEqual(result["end_month"], month)
                self.assertEqual(result["deduplications"][0]["matched_due_date"], f"{month}-15")

    def test_dedup_crosses_year_boundary_with_five_day_tolerance(self):
        for due_date, five_days_away, six_days_away in (
            ("2027-01-01", "2026-12-27", "2026-12-26"),
            ("2026-12-31", "2027-01-05", "2027-01-06"),
        ):
            schedule = _schedule("loan", due_day=int(due_date[-2:]), start_date=due_date, end_date=due_date)
            for last_date, matches in ((five_days_away, True), (six_days_away, False)):
                with self.subTest(due_date=due_date, last_date=last_date):
                    hint = _hint("monthly debit", last_date=last_date)
                    result = _forecast(evaluation_date="2027-02-01", schedules=[schedule], hints=[hint], months=1)
                    self.assertEqual(result["summary"]["deduped_recurring_count"], int(matches))
                    audits = [(row["matched_due_date"], row["last_observed_date"], row["day_distance"])
                              for row in result["deduplications"]]
                    self.assertEqual(audits, [(due_date, last_date, 5)] if matches else [])
                    labels = [row["label"] for row in result["months"][0]["items"]]
                    self.assertEqual(labels, [] if matches else ["monthly debit"])

    def test_filter_then_partition_then_hint_cap_before_dedup(self):
        hints = [_hint(f"debit {index}") for index in range(201)]
        with self.assertRaisesRegex(forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            _forecast(hints=hints, schedules=[_schedule("would-dedup")])
        self.assertEqual(_forecast(hints=hints, currency="EUR")["totals"], [])
        lapsed = [_hint(f"old {index}", last_date="2026-10-07") for index in range(201)]
        result = _forecast(hints=lapsed)
        self.assertEqual(result["summary"]["lapsed_recurring_count"], 201)
        self.assertEqual(result["totals"], [])

    def test_maximum_short_item_payload_and_injected_item_limit(self):
        schedules = [_schedule(f"schedule-{index}") for index in range(100)]
        hints = [_hint(f"hint-{index}", currency="EUR") for index in range(200)]
        result = _forecast(schedules=schedules, hints=hints, months=12)
        self.assertEqual(sum(len(month["items"]) for month in result["months"]), 3_600)
        self.assertLess(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 4 * 1024 ** 2)
        with patch.object(forecast, "MAX_FORECAST_ITEMS", 1):
            with self.assertRaisesRegex(forecast.ForecastLimitError, "^Forecast limit exceeded$"):
                _forecast(schedules=schedules[:2], months=1)

    def test_labels_trim_unicode_by_code_point_for_items_and_audits(self):
        long_label = "é" * 255 + " " + "x" * 409_744
        hints = _observed_hints(["2026-08-15", "2026-09-15", "2026-10-15"], description=long_label)
        self.assertEqual(len(hints[0].label), 410_000)
        result = _forecast(hints=hints, months=1)
        self.assertEqual(result["months"][0]["items"][0]["label"], "é" * 255 + "…")
        self.assertEqual(len(result["months"][0]["items"][0]["label"]), 256)
        spaced_prefix = "é" * 254 + " " + "x" * 409_745
        hints = _observed_hints(["2026-08-15", "2026-09-15", "2026-10-15"], description=spaced_prefix)
        result = _forecast(hints=hints, schedules=[_schedule("loan", label="漢" * 256)], months=1)
        self.assertEqual(result["deduplications"][0]["recurring_label"], "é" * 254 + "…")
        self.assertEqual(result["deduplications"][0]["schedule_label"], "漢" * 256)

    def test_iban_like_sequences_are_redacted_in_every_displayed_label(self):
        plain = "Principal repayment for November (reference 2026-11)"
        for original, redacted in (
            ("12345678901234567890123456", "••3456"),
            ("12 3456 7890 1234 5678 9012 3456", "••3456"),
            ("PL12345678901234567890123456", "PL••3456"),
            ("PL12 3456 7890 1234 5678 9012 3456", "PL••3456"),
            ("PL 12 3456 7890 1234 5678 9012 3456", "PL••3456"),
            ("GB82QWER12345678901234", "GB••1234"),
            ("GB82 QWER 1234 5678 9012 34", "GB••1234"),
            ("GB82QWER12345678901234 monthly fee", "GB••1234 monthly fee"),
            ("GB82 QWER 1234 5678 9012 34 monthly fee", "GB••1234 monthly fee"),
            ("GB82QWER12345678 901234", "GB••1234"),
            ("GB82QWER12345678901234 GB82 QWER 1234 5678 9012 34", "GB••1234 GB••1234"),
            (plain, plain),
        ):
            with self.subTest(label=original):
                original += " - monthly fee"
                redacted += " - monthly fee"
                hints = _observed_hints(["2026-08-15", "2026-09-15", "2026-10-15"], description=original)
                self.assertEqual(hints[0].label_source, "description")
                self.assertEqual(hints[0].label, original)
                projected = _forecast(hints=hints, schedules=[_schedule("loan", amount="200", label=original)], months=1)
                self.assertEqual({item["source"]: item["label"] for item in projected["months"][0]["items"]},
                                 {"mortgage": redacted, "recurring": redacted})
                matched = _forecast(hints=hints, schedules=[_schedule("loan", label=original)], months=1)
                self.assertEqual(len(matched["deduplications"]), 1)
                audit = matched["deduplications"][0]
                self.assertEqual((audit["schedule_label"], audit["recurring_label"]), (redacted, redacted))

    def test_iban_redaction_precedes_label_truncation(self):
        prefix = "x" * 244 + " "
        hints = _observed_hints(["2026-08-15", "2026-09-15", "2026-10-15"],
                                description=prefix + "PL12345678901234567890123456")
        result = _forecast(hints=hints, months=1)
        self.assertEqual(result["months"][0]["items"][0]["label"], prefix + "PL••3456")
        result = _forecast(hints=hints, schedules=[_schedule("loan")], months=1)
        self.assertEqual(result["deduplications"][0]["recurring_label"], prefix + "PL••3456")

    def test_hints_with_identical_masked_labels_keep_separate_dedup_identities(self):
        dates = ["2026-08-15", "2026-09-15", "2026-10-15"]
        first = _observed_hints(dates, description="PL12345678901234567890123456")
        second = _observed_hints(dates, description="PL34345678901234567890123456")
        schedules = [_schedule("a"), _schedule("b")]
        result = _forecast(hints=first + second, schedules=schedules, months=1)
        self.assertEqual(result["summary"]["deduped_recurring_count"], 2)
        self.assertEqual([(audit["schedule_id"], audit["recurring_label"]) for audit in result["deduplications"]],
                         [("a", "PL••3456"), ("b", "PL••3456")])
        self.assertEqual([item["source"] for item in result["months"][0]["items"]], ["mortgage", "mortgage"])

    def test_validation_errors_are_fixed_before_currency_filter_and_redact_reprs(self):
        confidential = _hint("private label", amount="123456789.987654321")
        active, _, _ = forecast._partition_hints([confidential], date(2026, 11, 22), set())
        for private_value in ("private label", "123456789.987654321"):
            self.assertNotIn(private_value, repr(active[0]))
        corrupt = _schedule("corrupt", amount="private amount", enabled=False, currency="EUR")
        for overrides in (
            {"schedules": [corrupt], "currency": "PLN"},
            {"account_timestamps": {"source": ("private corrupted timestamp", _stamp("2026-11-22"))}},
            {"hints": [replace(confidential, last_date="private corrupted date")]},
            {"evaluation_date": "9999-12-01", "months": 2},
        ):
            with self.subTest(case=list(overrides)), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                _forecast(**overrides)

        schedules = [_schedule(f"private {index}", enabled=False, currency="EUR") for index in range(101)]
        with self.assertRaisesRegex(forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            _forecast(schedules=schedules, currency="PLN")
        paid_months = frozenset(f"{1900 + index // 12}-{1 + index % 12:02d}" for index in range(1_201))
        with self.assertRaisesRegex(forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            _forecast(schedules=[replace(schedules[0], paid_months=paid_months)], currency="PLN")


def _baseline(**fields):
    baseline = forecast.CategoryBaseline(
        category_id="cat-power", name="Electricity", currency="PLN",
        monthly_amounts=("10", "20", "30", "40", "80", "100"), typical_day=28, current_spend="0",
        account_ids=frozenset(), last_observed_date="2026-10-12")
    return replace(baseline, **fields)


def _occurrence(reference, day="2026-10-15", account="source"):
    return RecurringOccurrence(account, reference, day, "f" * 64)


def _txn(category, amount, day, **fields):
    payload = {"credit_debit_indicator": fields.get("indicator", "DBIT"), "currency": fields.get("currency", "PLN"),
               "amount": amount}
    if day is not None:
        payload["booking_date"] = day
    payload.update(fields.get("payload", {}))
    return {"account_id": fields.get("account", "source"), "entry_reference": fields.get("reference", f"{day}:{amount}"),
            "payload": payload, "effective": {"kind": fields.get("kind", "expense"), "category_id": category}}


def _config(body):
    raw = body if isinstance(body, str) else json.dumps(body)
    return forecast.load_forecast_config({forecast.FORECAST_CONFIG_ENV: raw})


class CategoryBaselineTests(unittest.TestCase):
    def test_mean_median_and_trimmed_mean(self):
        self.assertEqual(forecast._mean_money(("1", "2")), "1.5")
        self.assertEqual(forecast._mean_money(("1", "2.5")), "1.75")
        self.assertEqual(forecast._mean_money(("1", "1", "2")), "1.3333333")
        self.assertEqual(forecast._mean_money(("1", "2", "2")), "1.6666667")
        self.assertEqual(forecast._median_money(("30", "10", "20")), "20")
        self.assertEqual(forecast._median_money(("40", "10", "30", "20")), "25")
        self.assertEqual(forecast._trimmed_mean_money(("100", "10", "80", "20", "40", "30")), "42.5")
        self.assertEqual(forecast._trimmed_mean_money(("80", "10", "30")), "30")
        self.assertEqual(forecast._mean_money(("-1",)), "-1")
        self.assertEqual(forecast._mean_money(("1", "-1")), "0")
        self.assertEqual(forecast._mean_money(("-1", "-2")), "-1.5")
        self.assertEqual(forecast._mean_money(("1", "1", "-1")), "0.3333333")
        self.assertEqual(forecast._mean_money(("-1", "-1", "1")), "-0.3333333")
        self.assertEqual(forecast._sub_money("10", "3"), ("7", "3"))
        self.assertEqual(forecast._sub_money("3", "10"), ("0", "3"))
        self.assertEqual(forecast._sub_money("5", "5"), ("0", "5"))
        self.assertEqual(forecast._sub_money("0", "4"), ("0", "0"))
        self.assertEqual(forecast._previous_months(date(2026, 11, 22), 6),
                         ("2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"))
        self.assertEqual(forecast._previous_months(date(1900, 3, 1), 6), ("1900-01", "1900-02"))
        for values in ((), ("nope",)):
            with self.subTest(values=values), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast._mean_money(values)
        for args in (("-1", "1"), ("1", "-1"), ("1", "nope"), ("nope", "1")):
            with self.subTest(args=args), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast._sub_money(*args)
        with self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
            forecast._baseline_estimate(("1", "2", "3"), "mode")
        self.assertIsNone(forecast._baseline_estimate(("1", "2"), "median"))
        for value in (None, "2026-02-31", "2026-2-01", "20261122"):
            with self.subTest(value=value), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast._iso_day(value)

    def test_config_defaults_and_explicit_estimator(self):
        self.assertIsNone(forecast.load_forecast_config({}))
        self.assertIsNone(_config(""))
        loaded = _config({"owner": {"category_ids": ["cat-power", "cat-mobile"]}})
        self.assertEqual(loaded["owner"], forecast.ForecastBaselineSettings(("cat-power", "cat-mobile"), 6, "median"))
        explicit = _config({"owner": {"category_ids": ["cat-power"], "history_months": 3, "estimator": "trimmed_mean"}})
        self.assertEqual(explicit["owner"].history_months, 3)
        self.assertEqual(explicit["owner"].estimator, "trimmed_mean")
        self.assertEqual(_config({"owner": {"category_ids": ["cat-power"], "history_months": 24}} )["owner"].history_months, 24)
        raw = json.dumps({"owner": {"category_ids": ["cat-power"]}})
        with patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: raw}):
            self.assertEqual(forecast.load_forecast_config()["owner"].category_ids, ("cat-power",))
        with patch.dict(os.environ, {forecast.FORECAST_CONFIG_ENV: ""}):
            self.assertIsNone(forecast.load_forecast_config())

    def test_config_rejects_invalid_documents(self):
        def reject(body):
            raw = body if isinstance(body, str) else json.dumps(body)
            with self.assertRaisesRegex(BudgetConfigError, "^Invalid forecast configuration$") as caught:
                forecast.load_forecast_config({forecast.FORECAST_CONFIG_ENV: raw})
            self.assertNotIn("secret-cat", str(caught.exception))
            self.assertNotIn("private-owner", str(caught.exception))

        reject("   ")
        reject([])
        reject("hello")
        reject({"private-owner": []})
        reject({"private-owner": {"history_months": 6}})
        reject({"private-owner": {"category_ids": ["secret-cat"], "note": True}})
        reject({"private-owner": {"category_ids": []}})
        reject({"private-owner": {"category_ids": ["secret-cat"] * 2}})
        reject({"private-owner": {"category_ids": ["secret-cat"] + [f"c{index}" for index in range(32)]}})
        reject({"private-owner": {"category_ids": ["uncategorised"]}})
        reject({"private-owner": {"category_ids": [" "]}})
        reject({"private-owner": {"category_ids": [""]}})
        reject({"private-owner": {"category_ids": ["a" * 129]}})
        reject({"private-owner": {"category_ids": ["bad\nid"]}})
        reject({"private-owner": {"category_ids": [1]}})
        reject({"private-owner": {"category_ids": "secret-cat"}})
        reject({"uncategorised": {"category_ids": ["secret-cat"]}})
        reject({"": {"category_ids": ["secret-cat"]}})
        reject({"a" * 129: {"category_ids": ["secret-cat"]}})
        reject({"private-owner\n": {"category_ids": ["secret-cat"]}})
        for history in (True, False, 6.0, "6", 2, 25, -1):
            with self.subTest(history=history):
                reject({"private-owner": {"category_ids": ["secret-cat"], "history_months": history}})
        reject({"private-owner": {"category_ids": ["secret-cat"], "estimator": "mean"}})
        reject({"private-owner": {"category_ids": ["secret-cat"], "estimator": ["median"]}})
        reject({"private-owner": {"category_ids": ["secret-cat"], "estimator": {"kind": "median"}}})
        reject({"private-owner": {"category_ids": ["secret-cat"], "estimator": 1}})
        reject('{"private-owner": {"category_ids": ["secret-cat"]}, "private-owner": {"category_ids": ["other"]}}')
        reject('{"private-owner": {"category_ids": ["secret-cat"], "category_ids": ["other"]}}')
        reject('{"private-owner": {"category_ids": ["secret-cat"], "history_months": NaN}}')
        reject('{"private-owner": {"category_ids": ["secret-cat"], "history_months": Infinity}}')
        reject("\ud800")
        reject("x" * forecast.MAX_FORECAST_CONFIG_BYTES)
        reject("{" + ("x" * forecast.MAX_FORECAST_CONFIG_BYTES))
        with patch.object(forecast.json, "loads", side_effect=RecursionError):
            reject('{"recurse": 1}')

    def test_fewer_than_three_covered_months_emits_nothing(self):
        names = {"cat-power": "Electricity"}
        short = [("cat-power", "PLN", "2026-09-02", "10"), ("cat-power", "PLN", "2026-10-03", "20")]
        self.assertEqual(forecast.build_category_baselines(
            category_ids=["cat-power"], category_names=names, history_months=6, evaluation_date="2026-11-22",
            expenses=short), ())
        zeros = short + [("cat-power", "PLN", "2026-08-04", "0"), ("cat-power", "PLN", "2026-07-04", "-5")]
        self.assertEqual(forecast.build_category_baselines(
            category_ids=("cat-power",), category_names=names, history_months=6, evaluation_date="2026-11-22",
            expenses=zeros), ())
        result = _forecast(baselines=[_baseline(monthly_amounts=("10", "20"))], months=1)
        self.assertEqual(result["months"][0]["items"], [])
        self.assertEqual(result["deduplications"], [])
        self.assertEqual(result["rule_version"], 2)

    def test_build_sorts_amounts_and_keeps_current_spend_apart(self):
        expenses = [
            ("cat-power", "PLN", "2026-05-12", "10"),
            ("cat-power", "PLN", "2026-06-01", "100"),
            ("cat-power", "PLN", "2026-06-20", "0"),
            ("cat-power", "PLN", "2026-07-03", "20"),
            ("cat-power", "PLN", "2026-08-28", "80"),
            ("cat-power", "PLN", "2026-09-04", "30"),
            ("cat-power", "PLN", "2026-10-15", "40"),
            ("cat-power", "PLN", "2026-04-01", "999"),
            ("cat-power", "PLN", "2026-11-09", "12"),
            ("cat-power", "PLN", "2026-11-11", "3"),
            ("cat-power", "EUR", "2026-11-11", "7"),
            ("cat-rent", "PLN", "2026-08-01", "500"),
            ("cat-power", "PLN", "2026-12-01", "500"),
            ("cat-power", "EUR", "2026-08-01", "1"),
            ("cat-power", "EUR", "2026-09-01", "1"),
        ]
        early = [
            ("cat-power", "PLN", "1899-12-01", "999"),
            ("cat-power", "PLN", "1900-01-10", "10"),
            ("cat-power", "PLN", "1900-02-10", "20"),
            ("cat-power", "PLN", "1900-03-10", "30"),
        ]
        built = forecast.build_category_baselines(
            category_ids=["cat-power"], category_names={"cat-power": "Electricity", "cat-rent": "Rent"},
            history_months=6, evaluation_date="2026-11-22", expenses=expenses)
        eur, pln = built
        self.assertEqual(pln.monthly_amounts, ("10", "100", "20", "80", "30", "40"))
        self.assertEqual(pln.typical_day, 4)
        self.assertEqual(pln.current_spend, "15")
        self.assertEqual(pln.last_observed_date, "2026-10-15")
        self.assertEqual(pln.account_ids, frozenset({""}))
        # The shared account's first row is April, so the EUR series includes the empty months around it.
        self.assertEqual(eur.currency, "EUR")
        self.assertEqual(eur.monthly_amounts, ("0", "0", "0", "1", "1", "0"))
        self.assertEqual(eur.current_spend, "7")
        self.assertEqual(eur.typical_day, 1)
        clamped = forecast.build_category_baselines(
            category_ids=["cat-power"], category_names={"cat-power": " Archived power "}, history_months=6,
            evaluation_date="1900-04-15", expenses=early)
        self.assertEqual(clamped[0].monthly_amounts, ("10", "20", "30"))
        self.assertEqual(clamped[0].name, " Archived power ")

    def test_any_overlap_inside_the_match_window_assigns_the_category(self):
        allowed = {"cat-power"}
        near = [("cat-power", "PLN", "2026-10-15", "100", 15)]
        loan = _schedule("loan", amount="100", due_day=15, start_date="2026-01-01")
        self.assertEqual(forecast._schedule_categories([loan], near, allowed),
                         {"loan": (frozenset({"cat-power"}), "PLN")})
        self.assertEqual(forecast._schedule_categories([replace(loan, enabled=False)], near, allowed), {})
        self.assertEqual(forecast._schedule_categories([loan], [("cat-power", "PLN", "2026-10-01", "100", 1)], allowed), {})
        self.assertEqual(forecast._schedule_categories([loan], [("cat-power", "PLN", "2026-10-15", "90", 15)], allowed), {})
        self.assertEqual(forecast._schedule_categories(
            [loan], near + [("cat-rent", "PLN", "2026-10-16", "100", 16)], allowed),
            {"loan": (frozenset({"cat-power"}), "PLN")})
        early = _schedule("early", amount="100", due_day=1, start_date="2026-01-01")
        self.assertEqual(forecast._schedule_categories(
            [early], [("cat-power", "PLN", "2026-10-30", "100", 30)], allowed),
            {"early": (frozenset({"cat-power"}), "PLN")})
        self.assertEqual(forecast._schedule_categories([loan], [("cat-rent", "PLN", "2026-10-15", "100", 15)], allowed), {})
        self.assertEqual(forecast._schedule_categories(
            [replace(loan, start_date="2026-11-01")], [("cat-power", "PLN", "2026-10-15", "100", 15)], allowed), {})
        phone = _hint("Orange", evidence=(_occurrence("a"), _occurrence("b")))
        evidence = {("source", "a"): "cat-power", ("source", "b"): "cat-power"}
        self.assertEqual(forecast._hint_categories([phone], evidence, allowed),
                         {forecast._hint_identity(phone): frozenset({"cat-power"})})
        self.assertEqual(forecast._hint_categories(
            [phone], {("source", "a"): "cat-power", ("source", "b"): "cat-rent"}, allowed),
            {forecast._hint_identity(phone): frozenset({"cat-power"})})
        self.assertEqual(forecast._hint_categories([phone], {("source", "a"): "cat-power"}, allowed),
                         {forecast._hint_identity(phone): frozenset({"cat-power"})})
        self.assertEqual(forecast._hint_categories([_hint("Orange")], evidence, allowed), {})
        self.assertEqual(forecast._hint_categories([phone], {("source", "a"): "cat-rent", ("source", "b"): "cat-rent"}, allowed), {})
        self.assertEqual(forecast._hint_categories(
            [phone], {("source", "a"): None, ("source", "b"): None}, allowed), {})
        other_currency = forecast.build_category_baselines(
            category_ids=["cat-power"], category_names={"cat-power": "Electricity"}, history_months=6,
            evaluation_date="2026-11-22",
            expenses=[("cat-power", "EUR", f"2026-{month:02d}-10", "10", "eur") for month in range(5, 11)])
        self.assertEqual(other_currency[0].account_ids, frozenset({"eur"}))
        self.assertEqual(other_currency[0].monthly_amounts, ("10", "10", "10", "10", "10", "10"))

    def test_expenses_use_the_effective_category(self):
        rows = [
            _txn("cat-power", "10.00", "2026-08-02", reference="kept"),
            _txn("cat-power", "5", "2026-08-03", kind="transfer", reference="transfer"),
            _txn("cat-power", "5", "2026-08-04", kind="income", indicator="CRDT", reference="income"),
            _txn("cat-power", "5", "2026-08-05", kind="refund", reference="refund"),
            _txn("cat-power", "4.00", "2026-08-09", kind="refund", indicator="CRDT", reference="credit-refund"),
            _txn(None, "5", "2026-08-06", reference="none"),
            _txn("cat-power", "5", None, reference="undated"),
        ]
        expenses, refunds, evidence, first_dates = forecast.expenses_from_transactions(rows)
        self.assertEqual(expenses, (("cat-power", "PLN", "2026-08-02", "10.00", "source"),))
        self.assertEqual(refunds, (("cat-power", "PLN", "2026-08-09", "4.00", "source"),))
        self.assertEqual(evidence[("source", "kept")], "cat-power")
        self.assertEqual(evidence[("source", "transfer")], "cat-power")
        self.assertEqual(evidence[("source", "none")], None)
        self.assertIn(("source", "undated"), evidence)
        self.assertEqual(first_dates["source"], "2026-08-02")
        built = forecast.build_category_baselines(
            category_ids=["cat-power"], category_names={"cat-power": "Electricity"}, history_months=6,
            evaluation_date="2026-11-22",
            expenses=expenses + (("cat-power", "PLN", "2026-09-02", "0.00", "source"),
                                 ("cat-power", "PLN", "2026-10-02", "-1", "source"),
                                 ("cat-power", "PLN", "2026-09-03", "20", "source"),
                                 ("cat-power", "PLN", "2026-10-04", "30", "source")),
            refunds=refunds)
        self.assertEqual(built[0].monthly_amounts, ("6", "20", "30"))

    def test_snapshot_uses_config_and_skips_other_owners(self):
        env = {forecast.FORECAST_CONFIG_ENV: json.dumps(
            {"test-owner": {"category_ids": ["cat-power"], "history_months": 6, "estimator": "trimmed_mean"}})}
        rows = [_txn("cat-power", str(amount), f"2026-{month:02d}-10", reference=f"m{month}")
                for month, amount in zip(range(5, 11), (10, 100, 20, 80, 30, 40))]
        rows.append(_txn("cat-power", "12", "2026-11-04", reference="now"))
        snapshot = {"owner": "test-owner", "categories": [{"id": "cat-power", "name": "Electricity"}],
                    "transactions": rows}
        fresh = {"source": (_stamp("2026-11-22"), None)}
        baselines, estimator, conflicts = forecast.baselines_for_snapshot(
            snapshot, "2026-11-22", (), (), account_timestamps=fresh, environ=env)
        self.assertEqual(estimator, "trimmed_mean")
        self.assertEqual(conflicts, ())
        self.assertEqual(baselines[0].monthly_amounts, ("10", "100", "20", "80", "30", "40"))
        self.assertEqual(baselines[0].current_spend, "12")
        self.assertEqual(forecast.baselines_for_snapshot("not-a-snapshot", "2026-11-22", (), (), environ={}),
                         ((), "median", ()))
        self.assertEqual(forecast.baselines_for_snapshot(
            {"owner": "other", "categories": "corrupt"}, "2026-11-22", (), (), environ=env), ((), "median", ()))
        self.assertEqual(forecast.baselines_for_snapshot({"owner": 1}, "2026-11-22", (), (), environ=env),
                         ((), "median", ()))
        for corrupt in (
            "not-a-snapshot",
            {"owner": "test-owner", "categories": "nope"},
            {"owner": "test-owner", "categories": [{"name": "Electricity"}]},
            {"owner": "test-owner", "categories": ["Electricity"]},
            {"owner": "test-owner", "categories": [{"id": 1, "name": "Electricity"}]},
            {"owner": "test-owner", "categories": [{"id": "cat-power", "name": "Electricity"}], "transactions": {}},
        ):
            with self.subTest(corrupt=type(corrupt).__name__), self.assertRaisesRegex(
                    forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast.baselines_for_snapshot(corrupt, "2026-11-22", (), (), account_timestamps=fresh, environ=env)
        with self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
            forecast.baselines_for_snapshot(snapshot, "2026-11-22", (), (), environ=env)
        with self.assertRaises(BudgetConfigError) as missing_category:
            forecast.baselines_for_snapshot(
                {"owner": "test-owner", "categories": [{"id": "other", "name": "Other"}], "transactions": []},
                "2026-11-22", (), (), account_timestamps=fresh, environ=env)
        self.assertEqual(type(missing_category.exception), BudgetConfigError)
        self.assertNotIn("cat-power", str(missing_category.exception))

    def test_projected_baseline_uses_typical_day_estimate_and_masks_labels(self):
        result = _forecast(baselines=[_baseline(), _baseline(currency="EUR", category_id="cat-eur", name="Power EU")],
                           currency="PLN", months=1)
        item = result["months"][0]["items"][0]
        self.assertEqual(item, {
            "source": "category_baseline", "schedule_id": None, "label": "Electricity", "amount": "35",
            "currency": "PLN", "expected_date": "2026-11-28", "confidence": "estimated", "past_due": False,
            "account_stale": False,
        })
        self.assertEqual(result["totals"], [
            {"currency": "PLN", "total": "35", "scheduled_total": "0", "estimated_total": "35"},
        ])
        trimmed = _forecast(baselines=[_baseline()], baseline_estimator="trimmed_mean", months=1)
        self.assertEqual(trimmed["months"][0]["items"][0]["amount"], "42.5")
        february = _forecast(evaluation_date="2026-02-10", baselines=[_baseline(typical_day=31)], months=1)
        self.assertEqual(february["months"][0]["items"][0]["expected_date"], "2026-02-28")
        self.assertFalse(february["months"][0]["items"][0]["past_due"])
        leap = _forecast(evaluation_date="2028-02-10", baselines=[_baseline(typical_day=31)], months=1)
        self.assertEqual(leap["months"][0]["items"][0]["expected_date"], "2028-02-29")
        early = _forecast(baselines=[_baseline(typical_day=5)], months=1)
        self.assertEqual(early["months"][0]["items"][0]["expected_date"], "2026-11-05")
        self.assertFalse(early["months"][0]["items"][0]["past_due"])
        masked = _forecast(baselines=[
            _baseline(name="Grid PL12345678901234567890123456", monthly_amounts=("10", "10", "10")),
            _baseline(category_id="cat-long", name="é" * 300, monthly_amounts=("10", "10", "10")),
        ], months=1)
        labels = {item["label"] for item in masked["months"][0]["items"]}
        self.assertIn("Grid PL••3456", labels)
        self.assertIn("é" * 255 + "…", labels)

    def test_unconfigured_forecast_keeps_mortgage_dedup_and_omits_baselines(self):
        unchanged = _forecast(schedules=[_schedule("loan")], hints=[_hint("rent")])
        self.assertEqual(unchanged["rule_version"], forecast.RULE_VERSION)
        self.assertEqual(unchanged["summary"]["deduped_recurring_count"], 1)
        self.assertEqual(set(unchanged["deduplications"][0]), {
            "schedule_id", "schedule_label", "recurring_label", "amount", "currency", "matched_due_date",
            "last_observed_date", "day_distance", "account_stale",
        })
        self.assertFalse(any(item["source"] == "category_baseline" for month in unchanged["months"] for item in month["items"]))
        alongside = _forecast(schedules=[_schedule("loan", amount="40", due_day=28)],
                              hints=[_hint("Orange", amount="25", first_date="2026-08-28", last_date="2026-10-28")],
                              baselines=[_baseline(monthly_amounts=("100", "100", "100"), current_spend="10",
                                                   account_ids=frozenset({"source"}))],
                              months=1, account_timestamps={"source": (None, None)})
        self.assertEqual([(item["source"], item["amount"], item["account_stale"])
                          for item in alongside["months"][0]["items"]], [
            ("category_baseline", "90", True),
            ("mortgage", "40", False),
            ("recurring", "25", True),
        ])
        self.assertEqual(alongside["deduplications"], [])
        self.assertEqual(alongside["summary"]["deduped_recurring_count"], 0)

    def test_zero_months_inside_coverage_and_refunds_set_the_net(self):
        expenses = [
            ("cat-power", "PLN", "2026-05-02", "100", "home"),
            ("cat-power", "PLN", "2026-07-02", "100", "home"),
            ("cat-power", "PLN", "2026-09-02", "100", "home"),
        ]
        refunds = [("cat-power", "PLN", "2026-09-03", "40", "home")]
        built = forecast.build_category_baselines(
            category_ids=["cat-power"], category_names={"cat-power": "Electricity"}, history_months=6,
            evaluation_date="2026-11-22", expenses=expenses, refunds=refunds,
            account_first_dates={"home": "2026-05-01"})
        self.assertEqual(built[0].monthly_amounts, ("100", "0", "100", "0", "60", "0"))
        self.assertEqual(forecast._median_money(built[0].monthly_amounts), "30")
        self.assertEqual(built[0].account_ids, frozenset({"home"}))
        refund_only = [("cat-power", "PLN", f"2026-{month:02d}-04", "15", "home") for month in (8, 9, 10)]
        self.assertEqual(forecast.build_category_baselines(
            category_ids=["cat-power"], category_names={"cat-power": "Electricity"}, history_months=6,
            evaluation_date="2026-11-22", expenses=[], refunds=refund_only,
            account_first_dates={"home": "2026-08-01"}), ())

    def test_configured_category_conflicts_with_an_active_hint_or_enabled_mortgage(self):
        env = {forecast.FORECAST_CONFIG_ENV: json.dumps({"test-owner": {"category_ids": ["cat-power"]}})}
        fresh = {"source": (_stamp("2026-11-22"), None)}
        rows = [_txn("cat-power", "40", f"2026-{month:02d}-15", reference=f"m{month}") for month in range(5, 11)]
        snapshot = {"owner": "test-owner", "categories": [{"id": "cat-power", "name": "Electricity"},
                                                          {"id": "cat-rent", "name": "Rent"}],
                    "transactions": rows}

        def load(extra_rows=(), hints=(), schedules=(), timestamps=None):
            body = dict(snapshot)
            body["transactions"] = rows + list(extra_rows)
            return forecast.baselines_for_snapshot(
                body, "2026-11-22", hints, schedules, account_timestamps=fresh if timestamps is None else timestamps,
                environ=env)

        phone = _hint("Orange", amount="25", first_date="2026-08-20", last_date="2026-10-20",
                      evidence=(_occurrence("orange", "2026-10-20"),))
        hinted, _estimator, hinted_conflicts = load(
            [_txn("cat-power", "25", "2026-10-20", reference="orange")], hints=[phone])
        self.assertEqual(hinted, ())
        self.assertEqual(hinted_conflicts, ({"category_id": "cat-power", "reason": "recurring"},))
        self.assertNotIn("amount", hinted_conflicts[0])
        self.assertNotIn("Orange", str(hinted_conflicts))

        loan = _schedule("loan", amount="40", due_day=15, start_date="2026-01-01")
        scheduled, _estimator, scheduled_conflicts = load(schedules=[loan])
        self.assertEqual(scheduled, ())
        self.assertEqual(scheduled_conflicts, ({"category_id": "cat-power", "reason": "mortgage"},))
        self.assertNotIn("loan", str(scheduled_conflicts))
        self.assertNotIn("40", str(scheduled_conflicts))

        lapsed = replace(phone, last_date="2026-09-01", first_date="2026-07-01")
        orange = [_txn("cat-power", "25", "2026-10-20", reference="orange")]
        kept, _estimator, kept_conflicts = load(orange, hints=[lapsed])
        self.assertTrue(kept)
        self.assertEqual(kept_conflicts, ())
        stale_baselines, _estimator, stale_conflicts = load(orange, hints=[lapsed], timestamps={"source": (None, None)})
        self.assertEqual(stale_baselines, ())
        self.assertEqual(stale_conflicts, ({"category_id": "cat-power", "reason": "recurring"},))
        mixed = _hint("Orange", evidence=(_occurrence("a"), _occurrence("b")))
        partial, _estimator, partial_conflicts = load([
            _txn("cat-power", "25", "2026-10-20", reference="a"),
            _txn("cat-rent", "25", "2026-10-21", reference="b"),
        ], hints=[mixed])
        self.assertEqual(partial, ())
        self.assertEqual(partial_conflicts, ({"category_id": "cat-power", "reason": "recurring"},))
        disabled, _estimator, disabled_conflicts = load(schedules=[replace(loan, enabled=False)])
        self.assertTrue(disabled)
        self.assertEqual(disabled_conflicts, ())
        overlapped, _estimator, overlapped_conflicts = load(
            schedules=[loan], extra_rows=[_txn("cat-rent", "40", "2026-10-16", reference="rent")])
        self.assertEqual(overlapped, ())
        self.assertEqual(overlapped_conflicts, ({"category_id": "cat-power", "reason": "mortgage"},))
        other_currency, _estimator, other_conflicts = load(schedules=[replace(loan, currency="EUR")])
        self.assertTrue(other_currency)
        self.assertEqual(other_conflicts, ())
        # The conflict does not depend on a later currency filter: a PLN mortgage still skips the category.
        filtered, _estimator, filtered_conflicts = load(schedules=[loan])
        self.assertEqual(filtered, ())
        self.assertEqual(filtered_conflicts, ({"category_id": "cat-power", "reason": "mortgage"},))

    def test_partial_cross_month_and_direct_baselines_conflict(self):
        baseline = _baseline(monthly_amounts=("10", "10", "10"))
        early = _schedule("early", amount="40", due_day=1, start_date="2026-01-01")
        cross_month = _forecast(baselines=[baseline], schedules=[early], currency="EUR",
                                baseline_expenses=[("cat-power", "PLN", "2026-10-30", "40")], months=1)
        self.assertEqual(cross_month["months"][0]["items"], [])
        self.assertEqual(cross_month["summary"]["baseline_conflicts"],
                         [{"category_id": "cat-power", "reason": "mortgage"}])
        occurrences = tuple(_occurrence(f"e{index}", f"2026-{month:02d}-15") for index, month in enumerate(range(5, 11)))
        partial = _hint("Orange", evidence=occurrences, first_date="2026-05-15", last_date="2026-10-15")
        evidence = {("source", f"e{index}"): "cat-power" if index < 5 else "cat-rent" for index in range(6)}
        hinted = _forecast(baselines=[baseline], hints=[partial], baseline_evidence=evidence, months=1)
        self.assertFalse(any(item["source"] == "category_baseline" for item in hinted["months"][0]["items"]))
        self.assertEqual(hinted["summary"]["baseline_conflicts"], [{"category_id": "cat-power", "reason": "recurring"}])
        self.assertTrue(any(item["source"] == "recurring" for item in hinted["months"][0]["items"]))
        split = _schedule("split", amount="40", due_day=15, start_date="2026-01-01")
        rent = _baseline(category_id="cat-rent", name="Rent", monthly_amounts=("10", "10", "10"))
        both = _forecast(baselines=[baseline, rent], schedules=[split], baseline_expenses=[
            ("cat-power", "PLN", "2026-10-15", "40"),
            ("cat-rent", "PLN", "2026-10-16", "40"),
        ], months=1)
        self.assertEqual([item["source"] for item in both["months"][0]["items"]], ["mortgage"])
        self.assertEqual(both["summary"]["baseline_conflicts"], [
            {"category_id": "cat-power", "reason": "mortgage"},
            {"category_id": "cat-rent", "reason": "mortgage"},
        ])
        clean = _forecast(baselines=[baseline], months=1)
        self.assertEqual(clean["months"][0]["items"][0]["source"], "category_baseline")
        self.assertEqual(clean["summary"]["baseline_conflicts"], [])

    def test_conflict_skips_only_that_category_and_caps_the_note(self):
        fresh = {"source": (_stamp("2026-11-22"), None)}
        rows = []
        for month in range(5, 11):
            rows.append(_txn("cat-power", "40", f"2026-{month:02d}-15", reference=f"p{month}"))
            rows.append(_txn("cat-rent", "15", f"2026-{month:02d}-08", reference=f"r{month}"))
        phone = _hint("Orange", amount="25", first_date="2026-08-20", last_date="2026-10-20",
                      evidence=(_occurrence("orange", "2026-10-20"),))
        rows.append(_txn("cat-power", "25", "2026-10-20", reference="orange"))
        snapshot = {"owner": "test-owner", "categories": [{"id": "cat-power", "name": "Electricity"},
                                                          {"id": "cat-rent", "name": "Rent"}],
                    "transactions": rows}
        both = {forecast.FORECAST_CONFIG_ENV: json.dumps(
            {"test-owner": {"category_ids": ["cat-power", "cat-rent"]}})}
        baselines, estimator, conflicts = forecast.baselines_for_snapshot(
            snapshot, "2026-11-22", [phone], (), account_timestamps=fresh, environ=both)
        self.assertEqual(estimator, "median")
        self.assertEqual([item.category_id for item in baselines], ["cat-rent"])
        self.assertEqual(conflicts, ({"category_id": "cat-power", "reason": "recurring"},))
        loan = _schedule("loan", amount="40", due_day=15, start_date="2026-01-01")
        projected = _forecast(baselines=list(baselines), baseline_estimator=estimator, baseline_conflicts=conflicts,
                              hints=[phone], schedules=[loan], months=1)
        kinds = {(item["source"], item["label"]) for item in projected["months"][0]["items"]}
        self.assertIn(("category_baseline", "Rent"), kinds)
        self.assertNotIn(("category_baseline", "Electricity"), kinds)
        self.assertIn(("mortgage", "loan"), kinds)
        self.assertIn(("recurring", "Orange"), kinds)
        self.assertEqual(projected["summary"]["baseline_conflicts"],
                         [{"category_id": "cat-power", "reason": "recurring"}])
        self.assertNotIn("amount", projected["summary"]["baseline_conflicts"][0])

        only_power = {forecast.FORECAST_CONFIG_ENV: json.dumps({"test-owner": {"category_ids": ["cat-power"]}})}
        empty, _estimator, alone = forecast.baselines_for_snapshot(
            snapshot, "2026-11-22", [phone], [loan], account_timestamps=fresh, environ=only_power)
        self.assertEqual(empty, ())
        self.assertEqual(alone, (
            {"category_id": "cat-power", "reason": "mortgage"},
            {"category_id": "cat-power", "reason": "recurring"},
        ))
        still = _forecast(baselines=empty, baseline_conflicts=alone, hints=[phone], schedules=[loan], months=1)
        self.assertEqual({item["source"] for item in still["months"][0]["items"]}, {"mortgage", "recurring"})
        self.assertFalse(any(item["source"] == "category_baseline" for item in still["months"][0]["items"]))

        dropped = _forecast(baselines=[_baseline(monthly_amounts=("10", "10", "10"))],
                            baseline_conflicts=[{"category_id": "cat-power", "reason": "mortgage"},
                                                {"category_id": "cat-power", "reason": "mortgage"}],
                            months=1)
        self.assertEqual(dropped["months"][0]["items"], [])
        self.assertEqual(dropped["summary"]["baseline_conflicts"],
                         [{"category_id": "cat-power", "reason": "mortgage"}])
        power = _baseline(category_id="cat-power", name="Electricity", monthly_amounts=("10", "10", "10"))
        rent = _baseline(category_id="cat-rent", name="Rent", monthly_amounts=("10", "10", "10"))
        with patch.object(forecast, "MAX_BASELINE_CONFLICTS", 1):
            capped = _forecast(baselines=[power, rent], baseline_conflicts=[
                {"category_id": "cat-rent", "reason": "recurring"},
                {"category_id": "cat-power", "reason": "mortgage"},
            ], months=1)
        self.assertEqual(capped["months"][0]["items"], [])
        self.assertEqual(capped["summary"]["baseline_conflicts"],
                         [{"category_id": "cat-power", "reason": "mortgage"}])
        for bad in (
            "nope", b"nope", 1, ["x"],
            [{"category_id": "cat-power", "reason": "yearly"}],
            [{"category_id": "cat-power", "reason": "recurring", "amount": "10"}],
            [{"category_id": "", "reason": "recurring"}],
            [{"category_id": "a" * 129, "reason": "mortgage"}],
            [{"category_id": "bad\nid", "reason": "recurring"}],
            [{"category_id": "uncategorised", "reason": "recurring"}],
            [{"category_id": 1, "reason": "recurring"}],
            [{"reason": "recurring"}],
            [{"category_id": "cat-power"}],
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                _forecast(baseline_conflicts=bad, months=1)

    def test_current_month_remaining_floors_at_zero(self):
        for spend, november in (("40", "60"), ("100", None), ("150", None)):
            with self.subTest(spend=spend):
                result = _forecast(baselines=[_baseline(monthly_amounts=("100", "100", "100"), current_spend=spend)],
                                   months=2)
                amounts = [item["amount"] for item in result["months"][0]["items"]]
                self.assertEqual(amounts, [] if november is None else [november])
                self.assertEqual(result["months"][1]["items"][0]["amount"], "100")
                self.assertEqual(result["deduplications"], [])
        zero = _forecast(schedules=[_schedule("loan", amount="40")],
                         baselines=[_baseline(monthly_amounts=("0", "0", "0"))], months=1)
        self.assertEqual([item["source"] for item in zero["months"][0]["items"]], ["mortgage"])
        self.assertEqual(zero["deduplications"], [])
        negative = _forecast(baselines=[_baseline(monthly_amounts=("100", "100", "100"), current_spend="-5")], months=1)
        self.assertEqual(negative["months"][0]["items"][0]["amount"], "100")
        stale = _forecast(baselines=[_baseline(monthly_amounts=("10", "10", "10"), account_ids=frozenset({"source"}))],
                          account_timestamps={"source": (None, None)}, months=1)
        self.assertTrue(stale["months"][0]["items"][0]["account_stale"])
        fresh = _forecast(baselines=[_baseline(monthly_amounts=("10", "10", "10"), account_ids=frozenset({"source"}))],
                          months=1)
        self.assertFalse(fresh["months"][0]["items"][0]["account_stale"])

    def test_baseline_inputs_and_limits_fail_closed(self):
        names = {"cat-power": "Electricity"}
        base = dict(category_ids=["cat-power"], category_names=names, history_months=6,
                    evaluation_date="2026-11-22", expenses=[])
        for overrides in (
            {"category_ids": "cat-power"},
            {"category_names": []},
            {"history_months": True},
            {"history_months": 2},
            {"history_months": 25},
            {"category_ids": ["cat-power", "cat-power"]},
            {"category_names": {"cat-power": "  "}},
            {"category_names": {"cat-power": "bad\nname"}},
            {"category_names": {"cat-power": None}},
            {"expenses": "nope"},
            {"expenses": [("cat-power", "PLN", "2026-08-01")]},
            {"expenses": [["cat-power", "PLN", "2026-08-01", "10"]]},
            {"expenses": [(1, "PLN", "2026-08-01", "10")]},
            {"expenses": [("", "PLN", "2026-08-01", "10")]},
            {"expenses": [("a" * 129, "PLN", "2026-08-01", "10")]},
            {"expenses": [("bad\nid", "PLN", "2026-08-01", "10")]},
            {"expenses": [("cat-power", "pln", "2026-08-01", "10")]},
            {"expenses": [("cat-power", "PLN", "2026-02-31", "10")]},
            {"expenses": [("cat-power", "PLN", "2026-08-01", "nope")]},
            {"expenses": [("cat-power", "PLN", "2026-08-01", "10", 1)]},
            {"expenses": [("cat-power", "PLN", "2026-08-01", "10", "a" * 129)]},
            {"expenses": [("cat-power", "PLN", "2026-08-01", "10", "bad\nid")]},
            {"refunds": "nope"},
            {"refunds": [("cat-power", "PLN", "2026-08-01")]},
            {"account_first_dates": []},
            {"account_first_dates": {1: "2026-08-01"}},
            {"account_first_dates": {"source": "2026-02-31"}},
            {"evaluation_date": "2026-11-22T00:00:00"},
        ):
            with self.subTest(overrides=list(overrides)), self.assertRaisesRegex(
                    forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast.build_category_baselines(**(base | overrides))
        for missing in (["missing"], [1]):
            with self.subTest(missing=missing), self.assertRaisesRegex(BudgetConfigError, "^Invalid forecast configuration$"):
                forecast.build_category_baselines(**(base | {"category_ids": missing}))
        phone = _hint("Orange", amount="nope", evidence=(_occurrence("a"),))
        with self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
            forecast._hint_categories([phone], {("source", "a"): "cat-power"}, {"cat-power"})
        with self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
            forecast._hint_categories([_hint("Orange", evidence=("nope",))], {("source", "nope"): "cat-power"}, {"cat-power"})
        for label, hints, evidence in (
            ("string", "nope", {}),
            ("bytes", b"nope", {}),
            ("evidence", [], []),
            ("hint", [object()], {}),
        ):
            with self.subTest(hints=label), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast._hint_categories(hints, evidence, set())
        with self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
            forecast._schedule_categories([_schedule("loan", due_day=0)], [("cat-power", "PLN", "2026-10-15", "100", 15)],
                                          {"cat-power"})
        fresh = {"source": (_stamp("2026-11-22"), None)}
        conflict = dict(category_ids=["cat-power"], hints=(), schedules=(), expenses=[], evidence_categories={},
                        evaluation_date="2026-11-22", account_timestamps=fresh)
        for overrides in (
            {"hints": "nope"},
            {"hints": [object()]},
            {"hints": [_hint("Orange", last_date="not-a-date")]},
            {"hints": [replace(_hint("Orange"), account_ids=None)]},
            {"evidence_categories": []},
            {"expenses": "nope"},
            {"schedules": "nope"},
            {"schedules": [object()]},
            {"schedules": [_schedule("loan", amount="nope", due_day=0)]},
            {"account_timestamps": []},
            {"account_timestamps": {"source": ("bad", None)}},
            {"evaluation_date": "bad"},
        ):
            with self.subTest(conflict=list(overrides)), self.assertRaisesRegex(
                    forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast._baseline_conflicts(**(conflict | overrides))
        self.assertEqual(forecast._baseline_conflicts(**(conflict | {
            "expenses": [("cat-power", "PLN", "2026-10-15", "0"), ("cat-power", "PLN", "2026-10-15", "-3")],
        })), ([], frozenset()))
        matched, blocked = forecast._baseline_conflicts(**(conflict | {
            "schedules": [_schedule("loan", amount="40", due_day=15, start_date="2026-01-01")],
            "expenses": [("cat-power", "PLN", "2026-10-15", "0"), ("cat-power", "PLN", "2026-10-15", "40")],
        }))
        self.assertEqual(matched, [{"category_id": "cat-power", "reason": "mortgage"}])
        self.assertEqual(blocked, frozenset({"cat-power"}))
        three = [("cat-power", "PLN", f"2026-{month:02d}-01", "10") for month in (8, 9, 10)]
        pln = _baseline(monthly_amounts=("10", "10", "10"))
        eur = _baseline(currency="EUR", category_id="cat-eur", name="Power EU", monthly_amounts=("10", "10", "10"))
        with patch.object(forecast, "MAX_BASELINE_SERIES", 1), self.assertRaisesRegex(
                forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            _forecast(baselines=[pln, eur], months=1)
        filtered = _forecast(baselines=[pln, eur], currency="EUR", months=1)
        self.assertEqual([item["currency"] for item in filtered["months"][0]["items"]], ["EUR"])
        with patch.object(forecast, "MAX_ROWS", 1), self.assertRaisesRegex(
                forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            forecast.build_category_baselines(**(base | {"expenses": three[:2]}))
        with patch.object(forecast, "MAX_ROWS", 1), self.assertRaisesRegex(
                forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            forecast.expenses_from_transactions([_txn("cat-power", "1", "2026-08-01"), _txn("cat-power", "2", "2026-09-01")])
        for corrupt in ("rows", b"rows", [None], [{}], [{"payload": [], "effective": {}, "account_id": "a", "entry_reference": "e"}],
                        [{"payload": {}, "effective": [], "account_id": "a", "entry_reference": "e"}],
                        [{"payload": {}, "effective": {}, "account_id": 1, "entry_reference": "e"}],
                        [{"payload": {}, "effective": {}, "account_id": "a", "entry_reference": 1}],
                        [{"payload": {}, "effective": {"category_id": 1}, "account_id": "a", "entry_reference": "e"}],
                        [_txn("cat-power", "1", "not-a-date")]):
            with self.subTest(corrupt=corrupt), self.assertRaisesRegex(forecast.ForecastDataError, "^Invalid forecast data$"):
                forecast.expenses_from_transactions(corrupt)
        for overrides in (
            {"baselines": "nope"},
            {"baselines": [object()]},
            {"baselines": [_baseline()], "baseline_estimator": "mode"},
            {"baselines": [_baseline()], "baseline_estimator": ["median"]},
            {"baselines": [_baseline(name=None)]},
            {"baselines": [_baseline(currency="pln")]},
            {"baselines": [_baseline(typical_day=0)]},
            {"baselines": [_baseline(typical_day=True)]},
            {"baselines": [_baseline(typical_day=32)]},
            {"baselines": [_baseline(account_ids={"source"})]},
            {"baselines": [_baseline(current_spend="nope")]},
        ):
            with self.subTest(overrides=list(overrides)), self.assertRaisesRegex(
                    forecast.ForecastDataError, "^Invalid forecast data$"):
                _forecast(**overrides)
        self.assertEqual(_forecast(baseline_estimator="mode")["months"][0]["items"], [])
        self.assertEqual(_forecast(baseline_estimator=["median"])["months"][0]["items"], [])
        with patch.object(forecast, "MAX_FORECAST_ITEMS", 1), self.assertRaisesRegex(
                forecast.ForecastLimitError, "^Forecast limit exceeded$"):
            _forecast(schedules=[_schedule("loan")], baselines=[_baseline(monthly_amounts=("10", "10", "10"))], months=1)
