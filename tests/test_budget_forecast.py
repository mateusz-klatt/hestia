"""Forecast calendar, exact amounts, limits and instalment-level matching."""

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import random
import unittest
from unittest.mock import patch

from hestia.budget import forecast
from hestia.budget.mortgage_reminders import MortgageSchedule
from hestia.budget.recurring import RecurringEntry, RecurringHint, detect_recurring_charges


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


def _forecast(**overrides):
    evaluation = overrides.get("evaluation_date", "2026-11-22")
    defaults = dict(owner="test-owner", evaluation_date=evaluation, months=3, currency=None,
                    account_timestamps={"source": (_stamp(evaluation), None)}, excluded_accounts=0,
                    schedules=(), hints=(), snapshot_digest="d" * 64, schedules_version="a" * 64)
    return forecast.evaluate_forecast(**(defaults | overrides))


class BudgetForecastTests(unittest.TestCase):
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
            "deduped_recurring_count": 1, "stale_recurring_count": 2,
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
