"""Pure monthly budget evaluation tests covering P1 rules, bounds, and DST edges."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import unittest
from unittest import mock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hestia import api_contract
from hestia.budget import month_budget
from hestia.budget.analytics_core import financial_fingerprint, pair_fingerprint
from hestia.budget.config import BudgetConfigError


def _utc_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def _cat(cid: str, name: str, *, archived: bool = False) -> dict:
    return {"id": cid, "name": name, "archived": archived, "version": 1, "created_at": 1.0, "updated_at": 1.0}


def _tx(
    account_id: str,
    ref: str,
    *,
    amount: str = "100",
    currency: str = "PLN",
    direction: str = "DBIT",
    booking_date: str | None = "2026-11-10",
    value_date: str | None = None,
    transaction_date: str | None = None,
    kind: str | None = None,
    category_id: str | None = None,
    source: str = "default",
    rule_id: str | None = None,
    annotation: dict | None = None,
    metadata: dict | None = None,
    transfer_status: str = "none",
    observed_at: float = 1_795_000_000.0,
) -> dict:
    payload = {
        "amount": amount,
        "currency": currency,
        "credit_debit_indicator": direction,
        "status": "BOOK",
        "booking_date": booking_date,
        "value_date": value_date,
        "transaction_date": transaction_date,
        "remittance_information": ["Synthetic"],
    }
    if kind is None:
        kind = "expense" if direction == "DBIT" else "unclassified_credit"
    eff = {"kind": kind, "category_id": category_id, "source": source, "rule_id": rule_id}
    ann = annotation or {"version": 0, "mode": "auto", "category_id": None, "kind": "auto", "stale": False}
    fin_fp = financial_fingerprint(payload)
    xfer_fp = pair_fingerprint(account_id, ref, payload, metadata, {})
    return {
        "account_id": account_id,
        "entry_reference": ref,
        "payload": payload,
        "financial_fp": fin_fp,
        "transfer_fp": xfer_fp,
        "metadata": metadata,
        "annotation": ann,
        "effective": eff,
        "base_effective": dict(eff),
        "source": "api",
        "observed_at": observed_at,
        "api_observed_at": observed_at,
        "uncertain": False,
        "eligible": True,
        "transfer": {"status": transfer_status, "decision_id": None, "candidate_count": 0, "ambiguous": False},
    }


def _eval(
    *,
    period: str | None = "2026-11",
    currency: str | None = "PLN",
    evaluated_at: float | None = None,
    transactions: list[dict] | tuple[dict, ...] = (),
    transfers: list[dict] | tuple[dict, ...] = (),
    scan_limited: bool = False,
    categories: list[dict] | tuple[dict, ...] | None = None,
    rules: list[dict] | tuple[dict, ...] = (),
    identities: dict | None = None,
    limits: list[dict] | tuple[dict, ...] = (),
    account_component_timestamps: dict | None = None,
    account_kinds: dict | None = None,
    settings: month_budget.MonthBudgetSettings | None = None,
) -> dict:
    if evaluated_at is None:
        evaluated_at = _utc_ts("2026-11-22T12:00:00Z")
    if categories is None:
        categories = [
            _cat("cat-salary", "Wynagrodzenie"),
            _cat("cat-zus", "Świadczenia ZUS"),
            _cat("cat-rent", "Najem"),
            _cat("cat-mortgage", "Hipoteka"),
            _cat("cat-food", "Zakupy"),
            _cat("cat-other-inc", "Inne"),
        ]
    if account_component_timestamps is None:
        account_component_timestamps = {"acc-a": evaluated_at, "acc-b": evaluated_at}
    if identities is None:
        identities = {acct: [] for acct in account_component_timestamps}
    return month_budget.evaluate_monthly_budget(
        owner="owner",
        period=period,
        currency=currency,
        evaluated_at=evaluated_at,
        transactions=transactions,
        transfers=transfers,
        scan_limited=scan_limited,
        categories=categories,
        rules=rules,
        identities=identities,
        limits=limits,
        account_component_timestamps=account_component_timestamps,
        account_kinds={} if account_kinds is None else account_kinds,
        settings=settings,
    )


DEFAULT_SETTINGS = month_budget.MonthBudgetSettings(
    income_category_ids=("cat-salary", "cat-zus"),
    pass_through=month_budget.PassThroughSettings(rent_category_id="cat-rent", mortgage_category_id="cat-mortgage"),
)


class MonthlyBudgetConfigAndClockTests(unittest.TestCase):
    def test_query_validation_defaults_and_rejections(self):
        self.assertEqual(month_budget.validate_monthly_budget_query(None, None), (None, "PLN"))
        self.assertEqual(month_budget.validate_monthly_budget_query("2026-02", "EUR"), ("2026-02", "EUR"))
        for bad_period in ("", "2026", "2026-1", "2026-00", "2026-13", "1899-12", "10000-01", "２０２６-１１", 202611):
            with self.subTest(period=bad_period), self.assertRaises(month_budget.MonthInputError):
                month_budget.validate_monthly_budget_query(bad_period, None)
        for bad_currency in ("", "pln", "PLNN", "PL1", "€UR", 123):
            with self.subTest(currency=bad_currency), self.assertRaises(month_budget.MonthInputError):
                month_budget.validate_monthly_budget_query(None, bad_currency)

    def test_warsaw_midnight_winter_summer_and_both_dst_transitions(self):
        cases = [
            # Winter (CET, UTC+1): 22:59:59Z stays Nov 22; 23:00:00Z rolls to Nov 23.
            ("2026-11-22T22:59:59Z", "2026-11-22", "2026-11"),
            ("2026-11-22T23:00:00Z", "2026-11-23", "2026-11"),
            ("2026-11-30T22:59:59Z", "2026-11-30", "2026-11"),
            ("2026-11-30T23:00:00Z", "2026-12-01", "2026-12"),
            # Summer (CEST, UTC+2): 21:59:59Z stays Jul 31; 22:00:00Z rolls to Aug 1.
            ("2026-07-31T21:59:59Z", "2026-07-31", "2026-07"),
            ("2026-07-31T22:00:00Z", "2026-08-01", "2026-08"),
            # Spring-forward DST (2026-03-29 01:00Z = 02:00 CET -> 03:00 CEST).
            ("2026-03-28T22:59:59Z", "2026-03-28", "2026-03"),
            ("2026-03-28T23:00:00Z", "2026-03-29", "2026-03"),
            ("2026-03-29T00:59:59Z", "2026-03-29", "2026-03"),
            ("2026-03-29T01:00:00Z", "2026-03-29", "2026-03"),
            ("2026-03-29T21:59:59Z", "2026-03-29", "2026-03"),
            ("2026-03-29T22:00:00Z", "2026-03-30", "2026-03"),
            # Fall-back DST (2026-10-25 00:00Z = 02:00 CEST, 01:00Z = 02:00 CET).
            ("2026-10-24T21:59:59Z", "2026-10-24", "2026-10"),
            ("2026-10-24T22:00:00Z", "2026-10-25", "2026-10"),
            ("2026-10-25T00:59:59Z", "2026-10-25", "2026-10"),
            ("2026-10-25T01:00:00Z", "2026-10-25", "2026-10"),
            ("2026-10-25T22:59:59Z", "2026-10-25", "2026-10"),
            ("2026-10-25T23:00:00Z", "2026-10-26", "2026-10"),
        ]
        for instant, expected_date, expected_month in cases:
            with self.subTest(instant=instant):
                result = _eval(period=None, evaluated_at=_utc_ts(instant), settings=DEFAULT_SETTINGS)
                self.assertEqual((result["evaluation_date"], result["current_month"], result["period"]),
                                 (expected_date, expected_month, expected_month))
                self.assertEqual(result["evaluation_timezone"], "Europe/Warsaw")
                self.assertEqual(result["cutoff"], expected_date)
                api_contract.BudgetAnalyticsMonthlyBudget.model_validate(result)

        with mock.patch.object(month_budget, "ZoneInfo", side_effect=ZoneInfoNotFoundError("Europe/Warsaw")):
            with self.assertRaises(BudgetConfigError):
                month_budget.warsaw_date(_utc_ts("2026-11-22T12:00:00Z"))
        for bad_ts in (-1, float("nan"), float("inf"), True, 1e16, _utc_ts("1899-12-31T12:00:00Z")):
            with self.subTest(bad_ts=bad_ts), self.assertRaises(month_budget.MonthDataError):
                month_budget.warsaw_date(bad_ts)

    def test_month_config_parsing_bounds_and_validation(self):
        self.assertIsNone(month_budget.load_month_config({}))
        self.assertIsNone(month_budget.load_month_config({month_budget.MONTH_CONFIG_ENV: ""}))

        valid = json.dumps({
            "owner": {
                "income_category_ids": ["cat-salary", "cat-zus"],
                "pass_through": {"rent_category_id": "cat-rent", "mortgage_category_id": "cat-mortgage"},
            },
            "empty_owner": {"income_category_ids": []},
        })
        parsed = month_budget.load_month_config({month_budget.MONTH_CONFIG_ENV: valid})
        self.assertEqual(parsed["owner"].income_category_ids, ("cat-salary", "cat-zus"))
        self.assertEqual(parsed["owner"].pass_through.rent_category_id, "cat-rent")
        self.assertEqual(parsed["empty_owner"], month_budget.MonthBudgetSettings(income_category_ids=(), pass_through=None))

        invalid_payloads = [
            "not-json",
            "[]",
            "\ud800",
            "x" * (month_budget.MAX_CONFIG_BYTES + 1),
            '{"owner":{"income_category_ids":[]},"owner":{"income_category_ids":[]}}',
            '{"owner":{"income_category_ids":[NaN]}}',
            '{"":{"income_category_ids":[]}}',
            '{"owner":[]}',
            '{"owner":{}}',
            '{"owner":{"income_category_ids":[],"extra":1}}',
            '{"owner":{"income_category_ids":"cat"}}',
            json.dumps({"owner": {"income_category_ids": [f"c{i}" for i in range(101)]}}),
            '{"owner":{"income_category_ids":["uncategorised"]}}',
            '{"owner":{"income_category_ids":["c1","c1"]}}',
            '{"owner":{"income_category_ids":[],"pass_through":[]}}',
            '{"owner":{"income_category_ids":[],"pass_through":{"rent_category_id":"r"}}}',
            '{"owner":{"income_category_ids":[],"pass_through":{"rent_category_id":"same","mortgage_category_id":"same"}}}',
            '{"owner":{"income_category_ids":["c1"],"pass_through":{"rent_category_id":"c1","mortgage_category_id":"m"}}}',
            '{"owner":{"income_category_ids":["c1"],"pass_through":{"rent_category_id":"r","mortgage_category_id":"c1"}}}',
        ]
        for raw in invalid_payloads:
            with self.subTest(raw=raw[:40]), self.assertRaises(BudgetConfigError):
                month_budget.load_month_config({month_budget.MONTH_CONFIG_ENV: raw})

    def test_unconfigured_dashboard_and_snapshot_category_reference_checks(self):
        txs = [
            _tx("acc-a", "exp1", amount="50", booking_date="2026-11-05", kind="expense", category_id="cat-food", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
            _tx("acc-a", "excl1", amount="10", booking_date="2026-11-06", kind="excluded"),
        ]
        result = _eval(transactions=txs, settings=None)
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(result)
        self.assertEqual(result["configuration"], {"status": "unconfigured", "source": "none"})
        self.assertIsNone(result["summary"])
        self.assertIsNone(result["expected_income"])
        self.assertIsNone(result["pace"])
        self.assertIsNone(result["pass_through"])
        self.assertEqual(result["category_spend"], [])
        self.assertEqual(result["card_exposure"], {"status": "unavailable", "amount": None, "as_of_date": None})
        self.assertTrue(result["coverage"]["has_activity"])
        self.assertEqual(result["coverage"]["purchase_date_fallback_count"], 1)

        with self.assertRaises(month_budget.MonthInputError):
            _eval(period="2026-12", evaluated_at=_utc_ts("2026-11-22T12:00:00Z"), settings=DEFAULT_SETTINGS)

        for bad_settings in (
            month_budget.MonthBudgetSettings(income_category_ids=("missing-cat",)),
            month_budget.MonthBudgetSettings(
                income_category_ids=("cat-salary",),
                pass_through=month_budget.PassThroughSettings("missing-rent", "cat-mortgage"),
            ),
            month_budget.MonthBudgetSettings(
                income_category_ids=("cat-salary",),
                pass_through=month_budget.PassThroughSettings("cat-rent", "missing-mort"),
            ),
        ):
            with self.subTest(bad_settings=bad_settings), self.assertRaises(BudgetConfigError):
                _eval(settings=bad_settings)


class MonthlyBudgetTransferAndProvisionalTests(unittest.TestCase):
    def test_repayments_one_sided_split_multicurrency_delayed_deselected_and_verified_exclusions(self):
        deselected_id = [{"scheme": "IBAN", "identification": "PL11111111111111111111111111"}]
        # One-sided repayment, split repayments (600 + 400), multi-currency (PLN vs EUR),
        # delayed (> 3 days), deselected counterparty identity, auto-with-category (implicit),
        # and explicit expense with stale manual excluded annotation.
        rules = [
            {"id": "rule-auto", "kind": "auto", "category_id": "cat-food"},
            {"id": "rule-explicit", "kind": "expense", "category_id": "cat-food"},
        ]
        stale_unrelated_tx = _tx(
            "acc-a", "stale-excl-unrelated", amount="60", booking_date="2026-11-08", transaction_date="2026-11-08",
            kind="expense", category_id="cat-food", source="rule", rule_id="rule-explicit",
            annotation={"version": 1, "mode": "manual", "category_id": "cat-other-inc", "kind": "excluded", "stale": True},
        )
        raw_txs = [
            _tx("acc-a", "one-sided", amount="1000", booking_date="2026-11-02", transaction_date="2026-11-02"),
            _tx("acc-a", "split-1", amount="600", booking_date="2026-11-03", transaction_date="2026-11-03"),
            _tx("acc-a", "split-2", amount="400", booking_date="2026-11-03", transaction_date="2026-11-03"),
            _tx("acc-a", "fx-debit", amount="500", currency="PLN", booking_date="2026-11-04", transaction_date="2026-11-04"),
            _tx("acc-b", "fx-credit", amount="120", currency="EUR", direction="CRDT", booking_date="2026-11-04"),
            _tx("acc-a", "delayed-d", amount="700", booking_date="2026-11-01", transaction_date="2026-11-01"),
            _tx("acc-b", "delayed-c", amount="700", direction="CRDT", booking_date="2026-11-06"),
            # Explicit expense targeting deselected owned account -> STILL warns via creditor_accounts identity!
            _tx("acc-a", "desel-repay", amount="300", booking_date="2026-11-05", transaction_date="2026-11-05",
                kind="expense", category_id="cat-food", source="rule", rule_id="rule-explicit",
                metadata={"creditor_accounts": deselected_id}),
            # Rule with kind="auto" + category -> STILL implicit expense and warns!
            _tx("acc-a", "auto-cat", amount="50", booking_date="2026-11-07", transaction_date="2026-11-07",
                kind="expense", category_id="cat-food", source="rule", rule_id="rule-auto"),
            # Explicit expense with stale manual excluded annotation -> STILL warns!
            _tx("acc-a", "stale-excl", amount="80", booking_date="2026-11-08", transaction_date="2026-11-08",
                kind="expense", category_id="cat-food", source="rule", rule_id="rule-explicit",
                annotation={"version": 1, "mode": "manual", "category_id": None, "kind": "excluded", "stale": True}),
            stale_unrelated_tx,
            # Manual auto annotation with category -> implicit expense, warns!
            _tx("acc-a", "manual-auto", amount="20", booking_date="2026-11-09", transaction_date="2026-11-09",
                kind="expense", category_id="cat-food", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "auto", "stale": False}),
            # Clean explicit manual expense with rejected transfer status and NO other evidence -> does NOT warn!
            _tx("acc-a", "clean-explicit", amount="25", booking_date="2026-11-10", transaction_date="2026-11-10",
                kind="expense", category_id="cat-food", source="manual", transfer_status="rejected",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
            # Clean explicit rule expense -> does NOT warn!
            _tx("acc-a", "clean-rule", amount="15", booking_date="2026-11-11", transaction_date="2026-11-11",
                kind="expense", category_id="cat-food", source="rule", rule_id="rule-explicit"),
        ]
        before = _eval(
            transactions=raw_txs,
            rules=rules,
            identities={"acc-a": [], "acc-b": [], "acc-deselected": deselected_id},
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(before["summary"]["spent"], "3750")
        self.assertEqual(before["coverage"]["pending_transfer_rows"], 10)
        self.assertTrue(before["coverage"]["provisional"])
        stale_unrelated_res = _eval(transactions=[stale_unrelated_tx], rules=rules, settings=DEFAULT_SETTINGS)
        self.assertEqual(stale_unrelated_res["summary"]["spent"], "60")
        self.assertEqual(stale_unrelated_res["coverage"]["pending_transfer_rows"], 1)
        self.assertTrue(stale_unrelated_res["coverage"]["provisional"])

        # After verified independent exclusions on all repayment legs + making auto/stale explicit clean:
        verified_txs = [
            _tx("acc-a", "one-sided", amount="1000", booking_date="2026-11-02", transaction_date="2026-11-02", kind="excluded", source="manual"),
            _tx("acc-a", "split-1", amount="600", booking_date="2026-11-03", transaction_date="2026-11-03", kind="excluded", source="manual"),
            _tx("acc-a", "split-2", amount="400", booking_date="2026-11-03", transaction_date="2026-11-03", kind="excluded", source="manual"),
            _tx("acc-a", "fx-debit", amount="500", currency="PLN", booking_date="2026-11-04", transaction_date="2026-11-04", kind="excluded", source="rule"),
            _tx("acc-b", "fx-credit", amount="120", currency="EUR", direction="CRDT", booking_date="2026-11-04", kind="excluded", source="manual"),
            _tx("acc-a", "delayed-d", amount="700", booking_date="2026-11-01", transaction_date="2026-11-01", kind="excluded", source="manual"),
            _tx("acc-b", "delayed-c", amount="700", direction="CRDT", booking_date="2026-11-06", kind="excluded", source="manual"),
            _tx("acc-a", "desel-repay", amount="300", booking_date="2026-11-05", transaction_date="2026-11-05", kind="excluded", source="manual"),
            _tx("acc-a", "clean-explicit", amount="25", booking_date="2026-11-10", transaction_date="2026-11-10",
                kind="expense", category_id="cat-food", source="manual", transfer_status="rejected",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
            _tx("acc-a", "clean-rule", amount="15", booking_date="2026-11-11", transaction_date="2026-11-11",
                kind="expense", category_id="cat-food", source="rule", rule_id="rule-explicit"),
        ]
        after = _eval(
            transactions=verified_txs,
            rules=rules,
            identities={"acc-a": [], "acc-b": [], "acc-deselected": deselected_id},
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(after["summary"]["spent"], "40")
        self.assertEqual(after["coverage"]["pending_transfer_rows"], 0)
        self.assertFalse(after["coverage"]["provisional"])
        # Also verify non-matching counterparty identity, non-accepted transfer view,
        # zero-amount expense/refund/income/rent rows, and descending observed_at ordering.
        unrelated_id = [{"scheme": "IBAN", "identification": "PL99999999999999999999999999"}]
        non_accepted_view = {
            "id": None, "version": 0, "status": "suggested", "source": "heuristic",
            "debit": {"account_id": "acc-a", "entry_reference": "x", "selected": True,
                      "fingerprint": "f" * 64, "amount": "1", "currency": "PLN", "date": "2026-11-01"},
            "credit": {"account_id": "acc-b", "entry_reference": "y", "selected": True,
                       "fingerprint": "e" * 64, "amount": "1", "currency": "PLN", "date": "2026-11-01"},
            "date_distance": 0, "ambiguous": False, "stale": False, "boundary": False,
        }
        now_ts = _utc_ts("2026-11-22T12:00:00Z")
        extra_txs = [
            _tx("acc-a", "clean-unrelated-meta", amount="10", booking_date="2026-11-12", transaction_date="2026-11-12",
                kind="expense", category_id="cat-food", source="manual", observed_at=now_ts - 10,
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False},
                metadata={"creditor_accounts": unrelated_id}),
            _tx("acc-a", "zero-exp", amount="0", booking_date="2026-11-12", transaction_date="2026-11-12",
                kind="expense", category_id="cat-food", source="manual", observed_at=now_ts - 20,
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
            _tx("acc-a", "zero-ref", amount="0", direction="CRDT", booking_date="2026-11-12",
                kind="refund", category_id="cat-food", source="manual"),
            _tx("acc-a", "zero-inc", amount="0", direction="CRDT", booking_date="2026-11-12",
                kind="income", category_id="cat-salary", source="manual"),
            _tx("acc-a", "zero-rent", amount="0", direction="CRDT", booking_date="2026-11-12",
                kind="income", category_id="cat-rent", source="manual", observed_at=now_ts - 5),
        ]
        clean_extra = _eval(
            evaluated_at=now_ts,
            transactions=extra_txs,
            transfers=[non_accepted_view],
            identities={"acc-a": [], "acc-b": deselected_id},
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(clean_extra["summary"]["spent"], "10")
        self.assertFalse(clean_extra["coverage"]["provisional"])
        self.assertEqual(clean_extra["coverage"]["ledger_as_of"], now_ts)

    def test_implicit_purchase_fallback_by_account_kind(self):
        purchase = _tx("acc-a", "purchase", amount="125.25", booking_date="2026-11-10", transaction_date="2026-11-10")
        for kind in ("credit_card", "current", "savings", "other", "unknown"):
            with self.subTest(kind=kind):
                result = _eval(
                    transactions=[purchase], account_kinds={"acc-a": kind}, settings=DEFAULT_SETTINGS,
                )
                self.assertEqual(result["summary"]["spent"], "125.25")
                self.assertEqual(result["coverage"]["pending_transfer_rows"], 0 if kind == "credit_card" else 1)
                self.assertEqual(result["coverage"]["provisional"], kind != "credit_card")

    def test_card_debits_with_unresolved_transfer_evidence_remain_pending(self):
        account_kinds = {"acc-a": "credit_card", "acc-b": "current"}

        def assert_pending(result, spent):
            self.assertEqual(result["summary"]["spent"], spent)
            self.assertEqual(result["coverage"]["pending_transfer_rows"], 1)
            self.assertTrue(result["coverage"]["provisional"])

        for status in ("suggested", "review", "boundary"):
            with self.subTest(status=status):
                row = _tx("acc-a", f"card-{status}", amount="20", transaction_date="2026-11-10",
                          transfer_status=status)
                assert_pending(_eval(transactions=[row], account_kinds=account_kinds, settings=DEFAULT_SETTINGS), "20")

        own_b_id = [{"scheme": "IBAN", "identification": "PL22222222222222222222222222"}]
        identified = _tx("acc-a", "card-owned-identity", amount="30", transaction_date="2026-11-10",
                         metadata={"creditor_accounts": own_b_id})
        assert_pending(_eval(
            transactions=[identified], account_kinds=account_kinds,
            identities={"acc-a": [], "acc-b": own_b_id}, settings=DEFAULT_SETTINGS,
        ), "30")

        stale = _tx("acc-a", "card-stale-excluded", amount="40", transaction_date="2026-11-10",
                    kind="expense", category_id="cat-food", source="rule", rule_id="rule-explicit",
                    annotation={"version": 1, "mode": "manual", "category_id": "cat-other-inc",
                                "kind": "excluded", "stale": True})
        assert_pending(_eval(
            transactions=[stale], account_kinds=account_kinds,
            rules=[{"id": "rule-explicit", "kind": "expense", "category_id": "cat-food"}],
            settings=DEFAULT_SETTINGS,
        ), "40")

        card_debit = _tx("acc-a", "card-outside", amount="50", booking_date="2026-11-30",
                         transaction_date="2026-11-30", kind="transfer", source="transfer", transfer_status="accepted")
        card_debit["base_effective"] = {"kind": "expense", "category_id": None, "source": "default", "rule_id": None}
        card_credit = _tx("acc-b", "card-outside-credit", amount="50", direction="CRDT", booking_date="2026-12-01",
                          kind="transfer", source="transfer", transfer_status="accepted")
        accepted = {
            "status": "accepted",
            "debit": {"account_id": "acc-a", "entry_reference": "card-outside", "date": "2026-11-30"},
            "credit": {"account_id": "acc-b", "entry_reference": "card-outside-credit", "date": "2026-12-01"},
        }
        assert_pending(_eval(
            evaluated_at=_utc_ts("2026-11-30T12:00:00Z"), transactions=[card_debit, card_credit], transfers=[accepted],
            account_kinds=account_kinds, settings=DEFAULT_SETTINGS,
        ), "50")

    def test_cross_month_accepted_pair_horizon_purchase_date_warnings_and_credit_warnings(self):
        rules = [{"id": "rule-exp", "kind": "expense", "category_id": "cat-food"}]
        own_b_id = [{"scheme": "IBAN", "identification": "PL22222222222222222222222222"}]
        # Accepted pair across month boundary: debit 2026-11-30, credit 2026-12-01.
        accepted_transfer = {
            "id": "dec-1", "version": 1, "status": "accepted", "source": "manual",
            "debit": {"account_id": "acc-a", "entry_reference": "xfer-d", "selected": True,
                      "fingerprint": "f" * 64, "amount": "200", "currency": "PLN", "date": "2026-11-30"},
            "credit": {"account_id": "acc-b", "entry_reference": "xfer-c", "selected": True,
                       "fingerprint": "e" * 64, "amount": "200", "currency": "PLN", "date": "2026-12-01"},
            "date_distance": 1, "ambiguous": False, "stale": False, "boundary": False,
        }
        xfer_d = _tx("acc-a", "xfer-d", amount="200", booking_date="2026-11-30", transaction_date="2026-11-30",
                     kind="transfer", source="transfer", transfer_status="accepted")
        xfer_d["base_effective"] = {"kind": "expense", "category_id": "cat-food", "source": "rule", "rule_id": "rule-exp"}
        xfer_c = _tx("acc-b", "xfer-c", amount="200", direction="CRDT", booking_date="2026-12-01",
                     kind="transfer", source="transfer", transfer_status="accepted")
        xfer_c["base_effective"] = {"kind": "unclassified_credit", "category_id": None, "source": "default", "rule_id": None}

        # Card purchase on 2026-11-30 booked on 2026-12-02 (implicit expense).
        late_booked = _tx("acc-a", "late-card", amount="45", booking_date="2026-12-02", transaction_date="2026-11-30")
        # Credits in Nov: refund with suggested pair warns; income in configured cat with boundary warns;
        # rent income with own-account identity warns; unclassified credit with suggested does NOT warn.
        ref_warn = _tx("acc-a", "ref-w", amount="10", direction="CRDT", booking_date="2026-11-15",
                       kind="refund", category_id="cat-food", source="manual", transfer_status="suggested")
        inc_warn = _tx("acc-a", "inc-w", amount="500", direction="CRDT", booking_date="2026-11-16",
                       kind="income", category_id="cat-salary", source="manual", transfer_status="boundary")
        rent_warn = _tx("acc-a", "rent-w", amount="100", direction="CRDT", booking_date="2026-11-17",
                        kind="income", category_id="cat-rent", source="manual",
                        metadata={"debtor_accounts": own_b_id})
        unclass = _tx("acc-a", "unclass", amount="90", direction="CRDT", booking_date="2026-11-18",
                      kind="unclassified_credit", transfer_status="suggested")
        other_inc = _tx("acc-a", "other-inc", amount="90", direction="CRDT", booking_date="2026-11-18",
                        kind="income", category_id="cat-other-inc", source="manual", transfer_status="suggested")

        txs = [xfer_d, xfer_c, late_booked, ref_warn, inc_warn, rent_warn, unclass, other_inc]

        # Evaluated at 2026-11-30: xfer-c (Dec 1) is outside horizon so xfer-d reverts to base expense (200) and warns!
        # late-card (booked Dec 2) is not yet visible at Nov 30!
        at_nov30 = _eval(
            period="2026-11",
            evaluated_at=_utc_ts("2026-11-30T12:00:00Z"),
            transactions=txs,
            transfers=[accepted_transfer],
            rules=rules,
            identities={"acc-a": [], "acc-b": own_b_id},
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(at_nov30["summary"]["spent"], "190")  # 200 - 10 refund
        self.assertEqual(at_nov30["coverage"]["pending_transfer_rows"], 4)  # xfer_d, ref_warn, inc_warn, rent_warn
        self.assertTrue(at_nov30["coverage"]["provisional"])

        # Evaluated at 2026-12-02: xfer_d+xfer_c now both visible & excluded (0 warning);
        # late-card now observed & attributed to purchase month 2026-11!
        nov_at_dec2 = _eval(
            period="2026-11",
            evaluated_at=_utc_ts("2026-12-02T12:00:00Z"),
            transactions=txs,
            transfers=[accepted_transfer],
            rules=rules,
            identities={"acc-a": [], "acc-b": own_b_id},
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(nov_at_dec2["summary"]["spent"], "35")  # 45 - 10 refund
        self.assertEqual(nov_at_dec2["coverage"]["pending_transfer_rows"], 4)  # late_booked, ref_warn, inc_warn, rent_warn

        dec_at_dec2 = _eval(
            period="2026-12",
            evaluated_at=_utc_ts("2026-12-02T12:00:00Z"),
            transactions=txs,
            transfers=[accepted_transfer],
            rules=rules,
            identities={"acc-a": [], "acc-b": own_b_id},
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(dec_at_dec2["summary"]["spent"], "0")
        self.assertEqual(dec_at_dec2["coverage"]["pending_transfer_rows"], 0)
        self.assertTrue(dec_at_dec2["coverage"]["has_activity"])
        self.assertFalse(dec_at_dec2["coverage"]["provisional"])

    def test_scan_limited_only_triggers_when_period_has_affecting_unexcluded_rows(self):
        excluded_only = [_tx("acc-a", "excl", amount="50", kind="excluded", source="manual")]
        res_empty = _eval(scan_limited=True, transactions=excluded_only, settings=DEFAULT_SETTINGS)
        self.assertFalse(res_empty["coverage"]["transfer_scan_limited"])
        self.assertFalse(res_empty["coverage"]["provisional"])
        self.assertTrue(res_empty["coverage"]["has_activity"])

        explicit = _tx("acc-a", "exp", amount="50", kind="expense", category_id="cat-food", source="manual",
                       transaction_date="2026-11-10",
                       annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False})
        res_affecting = _eval(scan_limited=True, transactions=[explicit], settings=DEFAULT_SETTINGS)
        self.assertTrue(res_affecting["coverage"]["transfer_scan_limited"])
        self.assertTrue(res_affecting["coverage"]["provisional"])


class MonthlyBudgetPassThroughAndLimitsTests(unittest.TestCase):
    def test_late_rent_overpayment_unused_mortgage_refund_and_no_carry(self):
        # Mortgage debits on Nov 5 (2000) and Nov 10 (500 overpayment) = 2500 PLN; late rent on Nov 25 = 2000 PLN.
        # Plus EUR rent (999 EUR) and Oct rent excess (5000 PLN) that must NOT carry into Nov PLN.
        txs = [
            _tx("acc-a", "sal", amount="10000", direction="CRDT", booking_date="2026-11-01",
                kind="income", category_id="cat-salary", source="manual"),
            _tx("acc-a", "m1", amount="2000", booking_date="2026-11-05", transaction_date="2026-11-05",
                kind="expense", category_id="cat-mortgage", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-mortgage", "kind": "expense", "stale": False}),
            _tx("acc-a", "m2", amount="500", booking_date="2026-11-10", transaction_date="2026-11-10",
                kind="expense", category_id="cat-mortgage", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-mortgage", "kind": "expense", "stale": False}),
            _tx("acc-a", "rent-nov", amount="2000", direction="CRDT", booking_date="2026-11-25",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "rent-eur", amount="999", currency="EUR", direction="CRDT", booking_date="2026-11-05",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "rent-oct", amount="5000", direction="CRDT", booking_date="2026-10-15",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "food", amount="300", booking_date="2026-11-12", transaction_date="2026-11-12",
                kind="expense", category_id="cat-food", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
        ]
        limits = [
            {"category_id": "cat-mortgage", "currency": "PLN", "monthly_limit": "400", "warning_percent": 80, "enabled": True},
        ]
        # Before late rent arrives (evaluated Nov 20): full 2500 mortgage uncovered.
        before_rent = _eval(evaluated_at=_utc_ts("2026-11-20T12:00:00Z"), transactions=txs, limits=limits, settings=DEFAULT_SETTINGS)
        self.assertEqual(before_rent["pass_through"]["matched"], "0")
        self.assertEqual(before_rent["pass_through"]["uncovered_mortgage"], "2500")
        self.assertEqual(before_rent["summary"]["spent"], "2800")

        # After late rent arrives (evaluated Nov 28): matched=2000, uncovered=500, spent=800, limit exceeded (500 > 400).
        after_rent = _eval(evaluated_at=_utc_ts("2026-11-28T12:00:00Z"), transactions=txs, limits=limits, settings=DEFAULT_SETTINGS)
        self.assertEqual(after_rent["pass_through"], {
            "rent_category_id": "cat-rent",
            "mortgage_category_id": "cat-mortgage",
            "rent_received": "2000",
            "mortgage_debits": "2500",
            "mortgage_refunds": "0",
            "unapplied_mortgage_refunds": "0",
            "matched": "2000",
            "unapplied_rent": "0",
            "uncovered_mortgage": "500",
        })
        self.assertEqual(after_rent["summary"], {"start_budget": "10000", "spent": "800", "remaining": "9200"})
        by_cat = {row["category_id"]: row for row in after_rent["category_spend"]}
        self.assertEqual(by_cat["cat-mortgage"]["net_spending"], "500")
        self.assertEqual(by_cat["cat-mortgage"]["limit"]["status"], "exceeded")
        self.assertEqual(by_cat["cat-mortgage"]["limit"]["over_by"], "100")
        self.assertEqual(by_cat["cat-mortgage"]["limit"]["percent_used_floor"], "125")
        self.assertEqual(after_rent["available_currencies"], ["PLN", "EUR"])

        # Unused mortgage refund alongside ordinary spending: F=700 (split across 2 refund legs: 400 + 300) > M=400, R=1000, food=300.
        refund_txs = [
            _tx("acc-a", "m", amount="400", booking_date="2026-11-05", transaction_date="2026-11-05",
                kind="expense", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "m-ref-1", amount="400", direction="CRDT", booking_date="2026-11-06",
                kind="refund", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "m-ref-2", amount="300", direction="CRDT", booking_date="2026-11-07",
                kind="refund", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "rent", amount="1000", direction="CRDT", booking_date="2026-11-07",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "food", amount="300", booking_date="2026-11-08", transaction_date="2026-11-08",
                kind="expense", category_id="cat-food", source="manual"),
        ]
        refund_res = _eval(transactions=refund_txs, settings=DEFAULT_SETTINGS)
        self.assertEqual(refund_res["pass_through"], {
            "rent_category_id": "cat-rent",
            "mortgage_category_id": "cat-mortgage",
            "rent_received": "1000",
            "mortgage_debits": "400",
            "mortgage_refunds": "700",
            "unapplied_mortgage_refunds": "300",
            "matched": "0",
            "unapplied_rent": "1000",
            "uncovered_mortgage": "0",
        })
        self.assertEqual(refund_res["summary"]["spent"], "300")
        by_cat = {row["category_id"]: row for row in refund_res["category_spend"]}
        self.assertEqual((by_cat["cat-mortgage"]["expenses"], by_cat["cat-mortgage"]["refunds"],
                          by_cat["cat-mortgage"]["net_spending"]), ("400", "400", "0"))
        self.assertEqual(sum(float(row["net_spending"]) for row in refund_res["category_spend"]), 300.0)

        # Fully rent-covered mortgage still appears in category_spend with zero net spending.
        covered = _eval(
            transactions=[
                _tx("acc-a", "m", amount="1500", booking_date="2026-11-05", transaction_date="2026-11-05",
                    kind="expense", category_id="cat-mortgage", source="manual"),
                _tx("acc-a", "r", amount="2200", direction="CRDT", booking_date="2026-11-06",
                    kind="income", category_id="cat-rent", source="manual"),
            ],
            settings=DEFAULT_SETTINGS,
        )
        self.assertEqual(covered["pass_through"]["unapplied_rent"], "700")
        self.assertEqual(covered["pass_through"]["matched"], "1500")
        self.assertEqual(covered["category_spend"][0]["category_id"], "cat-mortgage")
        self.assertEqual(covered["category_spend"][0]["net_spending"], "0")
        self.assertEqual(covered["summary"]["spent"], "0")

    def test_huge_80_digit_totals_huge_percentage_strings_and_192_char_cap(self):
        huge = "9" * 80
        tiny = "0." + "0" * 77 + "1"
        txs = [
            _tx("acc-a", "h1", amount=huge, booking_date="2026-11-01", transaction_date="2026-11-01",
                kind="expense", category_id="cat-food", source="manual"),
            _tx("acc-a", "h2", amount=huge, booking_date="2026-11-02", transaction_date="2026-11-02",
                kind="expense", category_id="cat-food", source="manual"),
            _tx("acc-a", "t1", amount=tiny, booking_date="2026-11-03", transaction_date="2026-11-03",
                kind="expense", category_id="cat-food", source="manual"),
            _tx("acc-a", "neg", amount="10", direction="CRDT", booking_date="2026-11-04",
                kind="refund", category_id="cat-other-inc", source="manual"),
        ]
        limits = [
            {"category_id": "cat-food", "currency": "PLN", "monthly_limit": tiny, "warning_percent": 80, "enabled": True},
            {"category_id": "cat-other-inc", "currency": "PLN", "monthly_limit": "50", "warning_percent": 80, "enabled": True},
            {"category_id": "cat-zus", "currency": "PLN", "monthly_limit": "100", "warning_percent": 80, "enabled": True},
            {"category_id": "cat-rent", "currency": "EUR", "monthly_limit": "100", "warning_percent": 80, "enabled": False},
        ]
        # Exact at_limit case (net_spending == monthly_limit)
        at_limit_res = _eval(
            transactions=[_tx("acc-a", "exact", amount="100", booking_date="2026-11-02", kind="expense", category_id="cat-zus", source="manual")],
            limits=[{"category_id": "cat-zus", "currency": "PLN", "monthly_limit": "100", "warning_percent": 80, "enabled": True}],
            settings=month_budget.MonthBudgetSettings(income_category_ids=("cat-salary",)),
        )
        self.assertEqual(at_limit_res["category_spend"][0]["limit"], {
            "monthly_limit": "100", "remaining": "0", "over_by": "0",
            "percent_used_floor": "100", "warning_percent": 80, "status": "at_limit",
        })
        no_pt_settings = month_budget.MonthBudgetSettings(income_category_ids=("cat-salary",))
        res = _eval(transactions=txs, limits=limits, settings=no_pt_settings)
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(res)
        expected_net = str(2 * (10 ** 80 - 1)) + "." + "0" * 77 + "1"
        by_cat = {row["category_id"]: row for row in res["category_spend"]}
        self.assertEqual(by_cat["cat-food"]["net_spending"], expected_net)
        self.assertEqual(by_cat["cat-food"]["limit"]["percent_used_floor"],
                         str((2 * (10 ** 80 - 1) * 10 ** 78 + 1) * 100))
        self.assertEqual(by_cat["cat-food"]["limit"]["status"], "exceeded")
        self.assertEqual(by_cat["cat-other-inc"]["net_spending"], "-10")
        self.assertEqual(by_cat["cat-other-inc"]["limit"]["percent_used_floor"], "0")
        self.assertEqual(by_cat["cat-other-inc"]["limit"]["remaining"], "60")
        self.assertEqual(by_cat["cat-zus"]["limit"]["status"], "within")
        self.assertIsNone(res["pass_through"])

        # > 192 char aggregate bound -> MonthLimitError
        with mock.patch.object(month_budget, "MAX_AGGREGATE_CHARS", 10):
            with self.assertRaises(month_budget.MonthLimitError):
                _eval(transactions=[_tx("acc-a", "big", amount="12345678901")], settings=no_pt_settings)

        # > MAX_LIMITS -> MonthLimitError; > MAX_ROWS in sum_money -> MonthLimitError
        with mock.patch.object(month_budget, "MAX_LIMITS", 0):
            with self.assertRaises(month_budget.MonthLimitError):
                _eval(limits=limits[:1], settings=no_pt_settings)
        with mock.patch.object(month_budget.core, "MAX_ROWS", 1):
            with self.assertRaises(month_budget.MonthLimitError):
                _eval(transactions=txs[:2], settings=no_pt_settings)


class MonthlyBudgetExpectedIncomeAndPaceTests(unittest.TestCase):
    def test_expected_income_moved_salary_precedence_common_payer_local_multiplicity_and_partial(self):
        # 1. Late salary on day 28 + Jan 31 -> Feb 28 clamping + January -> December previous_month rollover
        dec_prior = _tx("acc-a", "d31", amount="4500", direction="CRDT", booking_date="2025-12-15",
                        kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Acme Corp"})
        jan_res = _eval(period="2026-01", evaluated_at=_utc_ts("2026-01-20T12:00:00Z"),
                        transactions=[dec_prior], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(jan_res["expected_income"], {
            "status": "estimated", "total": "4500",
            "items": [{"category_id": "cat-salary", "amount": "4500", "expected_date": "2026-01-15", "source": "previous_month"}],
        })
        jan31 = _tx("acc-a", "p1", amount="5000", direction="CRDT", booking_date="2026-01-31",
                    kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Acme Corp"})
        feb = _eval(period="2026-02", evaluated_at=_utc_ts("2026-02-28T12:00:00Z"),
                    transactions=[jan31], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(feb["expected_income"], {
            "status": "estimated", "total": "5000",
            "items": [{"category_id": "cat-salary", "amount": "5000", "expected_date": "2026-02-28", "source": "previous_month"}],
        })

        # Delayed salary on day 15 evaluated on Nov 22 remains expected on 2026-11-15
        oct15 = _tx("acc-a", "p15", amount="4000", direction="CRDT", booking_date="2026-10-15",
                    kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Acme Corp"})
        nov = _eval(transactions=[oct15], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(nov["expected_income"]["items"][0]["expected_date"], "2026-11-15")
        self.assertEqual(nov["pace"]["basis"], "received_plus_expected")
        self.assertEqual(nov["pace"]["budget"], "4000")

        # 2. Moved salary with changed amount (acc-a -> acc-b) + Unicode/whitespace/casefold normalization
        moved_prior = _tx("acc-a", "p-m", amount="6000", direction="CRDT", booking_date="2026-10-10",
                          kind="income", category_id="cat-salary", source="manual")
        moved_curr = _tx("acc-b", "c-m", amount="6500", direction="CRDT", booking_date="2026-11-10",
                         kind="income", category_id="cat-salary", source="manual")
        moved_prior["metadata"] = {"debtor_name": "Spo\u0301\u0142ka   ABC"}
        moved_curr["metadata"] = {"debtor_name": "sp\u00f3\u0142ka abc"}
        moved_res = _eval(transactions=[moved_prior, moved_curr], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(moved_res["expected_income"], {"status": "none", "total": "0", "items": []})
        self.assertEqual(moved_res["summary"]["start_budget"], "6500")

        # 3. Account-local precedence: prior on A and B for Common Payer; current on A -> matches A locally, B remains expected!
        p_a = _tx("acc-a", "pa", amount="3000", direction="CRDT", booking_date="2026-10-10",
                  kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Common Payer"})
        p_b = _tx("acc-b", "pb", amount="3500", direction="CRDT", booking_date="2026-10-12",
                  kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Common Payer"})
        c_a = _tx("acc-a", "ca", amount="3000", direction="CRDT", booking_date="2026-11-10",
                  kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Common Payer"})
        local_prec = _eval(transactions=[p_a, p_b, c_a], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(local_prec["expected_income"]["status"], "estimated")
        self.assertEqual(local_prec["expected_income"]["total"], "3500")

        # 4. Common payer with two possible prior accounts (A, B) and current on C -> suppressed & partial, retains ZUS AND other employer in same category!
        c_c = _tx("acc-c", "cc", amount="3200", direction="CRDT", booking_date="2026-11-11",
                  kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Common Payer"})
        other_emp = _tx("acc-a", "oe", amount="2100", direction="CRDT", booking_date="2026-10-18",
                        kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Distinct Corp"})
        zus_p = _tx("acc-a", "zp", amount="800", direction="CRDT", booking_date="2026-10-20",
                    kind="income", category_id="cat-zus", source="manual", metadata={"debtor_name": "ZUS"})
        common_ambig = _eval(
            transactions=[p_a, p_b, c_c, other_emp, zus_p],
            account_component_timestamps={"acc-a": _utc_ts("2026-11-22T12:00:00Z"),
                                          "acc-b": _utc_ts("2026-11-22T12:00:00Z"),
                                          "acc-c": _utc_ts("2026-11-22T12:00:00Z")},
            settings=month_budget.MonthBudgetSettings(("cat-salary", "cat-zus")),
        )
        self.assertEqual(common_ambig["expected_income"], {
            "status": "partial", "total": "2900",
            "items": [
                {"category_id": "cat-salary", "amount": "2100", "expected_date": "2026-11-18", "source": "previous_month"},
                {"category_id": "cat-zus", "amount": "800", "expected_date": "2026-11-20", "source": "previous_month"},
            ],
        })
        self.assertEqual(common_ambig["pace"]["basis"], "received_plus_expected")
        self.assertEqual(common_ambig["pace"]["budget"], "6100")  # 3200 + 2900

        # 5. Local multiplicity blocks cross-account matching (2 prior on A, 1 current on A, 1 current on B -> partial, empty items -> total=None).
        p_a2 = _tx("acc-a", "pa2", amount="1000", direction="CRDT", booking_date="2026-10-15",
                   kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Common Payer"})
        c_b = _tx("acc-b", "cb", amount="3000", direction="CRDT", booking_date="2026-11-11",
                  kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Common Payer"})
        local_mult = _eval(transactions=[p_a, p_a2, c_a, c_b], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(local_mult["expected_income"], {"status": "partial", "total": None, "items": []})
        self.assertEqual(local_mult["pace"]["basis"], "received")
        self.assertEqual(local_mult["pace"]["budget"], "6000")

        # 6. Unnamed prior with 0 current credits -> combined subtotal with expected_date=None & partial.
        unnamed_1 = _tx("acc-a", "u1", amount="100", direction="CRDT", booking_date="2026-10-05",
                        kind="income", category_id="cat-salary", source="manual", metadata=None)
        unnamed_2 = _tx("acc-a", "u2", amount="200", direction="CRDT", booking_date="2026-10-06",
                        kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "   "})
        subtotal_res = _eval(transactions=[unnamed_1, unnamed_2], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(subtotal_res["expected_income"], {
            "status": "partial", "total": "300",
            "items": [{"category_id": "cat-salary", "amount": "300", "expected_date": None, "source": "previous_month"}],
        })

        # 7. Unnamed current credit suppresses remaining named prior in same category.
        unnamed_curr = _tx("acc-a", "uc", amount="50", direction="CRDT", booking_date="2026-11-05",
                           kind="income", category_id="cat-salary", source="manual")
        unattrib = _eval(transactions=[oct15, unnamed_curr], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(unattrib["expected_income"], {"status": "partial", "total": None, "items": []})

        # 8. Missing prior evidence -> unavailable; empty configured set -> none; closed month -> historical.
        unavail = _eval(transactions=[], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(unavail["expected_income"], {"status": "unavailable", "total": None, "items": []})
        empty_cfg = _eval(transactions=[], settings=month_budget.MonthBudgetSettings(()))
        self.assertEqual(empty_cfg["expected_income"], {"status": "none", "total": "0", "items": []})
        closed = _eval(period="2026-10", transactions=[oct15], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(closed["expected_income"], {"status": "historical", "total": None, "items": []})
        self.assertEqual(closed["pace"], {
            "basis": "received", "budget": "4000", "status": "historical",
            "daily_rate": None, "projected_month_spend": None, "exhaustion_day": None,
        })

        # 9. One configured category missing prior evidence + another category matched/has item -> partial.
        partial_missing = _eval(transactions=[oct15], settings=month_budget.MonthBudgetSettings(("cat-salary", "cat-zus")))
        self.assertEqual(partial_missing["expected_income"]["status"], "partial")
        self.assertEqual(partial_missing["expected_income"]["total"], "4000")

        # 10. Current matched + unnamed prior in same category -> unnamed prior suppressed when current_rows > 0;
        # plus zero prior credit and debtor_name=None metadata on prior credit.
        zero_oct = _tx("acc-a", "zo", amount="0", direction="CRDT", booking_date="2026-10-05",
                       kind="income", category_id="cat-salary", source="manual", metadata={"creditor_name": "Ignored"})
        curr_acme = _tx("acc-a", "c15", amount="4000", direction="CRDT", booking_date="2026-11-15",
                        kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Acme Corp"})
        suppr_unnamed = _eval(transactions=[oct15, unnamed_1, zero_oct, curr_acme],
                              settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(suppr_unnamed["expected_income"], {"status": "partial", "total": None, "items": []})

        # 11. Prior credit with debtor_name=None in metadata and 0 current rows -> subtotal fallback.
        no_debtor_prior = _tx("acc-a", "nd", amount="250", direction="CRDT", booking_date="2026-10-10",
                              kind="income", category_id="cat-salary", source="manual", metadata={"creditor_name": "X"})
        nd_res = _eval(transactions=[no_debtor_prior], settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        self.assertEqual(nd_res["expected_income"]["status"], "partial")
        self.assertIsNone(nd_res["expected_income"]["items"][0]["expected_date"])

    def test_pace_all_statuses_and_exact_ratio_boundaries(self):
        cfg = month_budget.MonthBudgetSettings(("cat-salary",))
        ts = _utc_ts("2026-11-10T12:00:00Z")  # d = 10, D = 30

        # no_budget with S == 0
        no_b_zero = _eval(period="2026-11", evaluated_at=ts, settings=cfg)
        self.assertEqual(no_b_zero["pace"], {
            "basis": "received", "budget": "0", "status": "no_budget",
            "daily_rate": None, "projected_month_spend": None, "exhaustion_day": None,
        })
        # no_budget with S > 0
        no_b_pos = _eval(period="2026-11", evaluated_at=ts,
                         transactions=[_tx("acc-a", "e", amount="50", booking_date="2026-11-05")], settings=cfg)
        self.assertEqual(no_b_pos["pace"], {
            "basis": "received", "budget": "0", "status": "no_budget",
            "daily_rate": {"numerator": "50", "denominator": 10},
            "projected_month_spend": {"numerator": "1500", "denominator": 10},
            "exhaustion_day": None,
        })

        # Positive budget B = 300
        inc = _tx("acc-a", "inc", amount="300", direction="CRDT", booking_date="2026-11-01",
                  kind="income", category_id="cat-salary", source="manual")
        # no_positive_pace when S <= 0 (negative net from refund)
        neg_pace = _eval(period="2026-11", evaluated_at=ts, transactions=[
            inc, _tx("acc-a", "r", amount="20", direction="CRDT", booking_date="2026-11-02",
                     kind="refund", category_id="cat-food", source="manual"),
        ], settings=cfg)
        self.assertEqual(neg_pace["pace"]["status"], "no_positive_pace")
        self.assertIsNone(neg_pace["pace"]["daily_rate"])

        # already_reached when S >= B (exact equality)
        reached = _eval(period="2026-11", evaluated_at=ts, transactions=[
            inc, _tx("acc-a", "e", amount="300", booking_date="2026-11-02"),
        ], settings=cfg)
        self.assertEqual(reached["pace"]["status"], "already_reached")
        self.assertEqual(reached["pace"]["exhaustion_day"], 10)

        # runs_out on exact day 30: S=100, d=10, B=300 => S*30 == B*10
        runs_out = _eval(period="2026-11", evaluated_at=ts, transactions=[
            inc, _tx("acc-a", "e", amount="100", booking_date="2026-11-02"),
        ], settings=cfg)
        self.assertEqual(runs_out["pace"]["status"], "runs_out")
        self.assertEqual(runs_out["pace"]["exhaustion_day"], 30)

        # within_budget: S=99.99, d=10, B=300 => S*30 < B*10
        within = _eval(period="2026-11", evaluated_at=ts, transactions=[
            inc, _tx("acc-a", "e", amount="99.99", booking_date="2026-11-02"),
        ], settings=cfg)
        self.assertEqual(within["pace"]["status"], "within_budget")
        self.assertIsNone(within["pace"]["exhaustion_day"])


class MonthlyBudgetLedgerFreshnessTests(unittest.TestCase):
    def test_ledger_as_of_offline_live_empty_future_clamp_and_stale_threshold(self):
        now = _utc_ts("2026-11-22T12:00:00Z")
        stale_cutoff = month_budget.STALE_AFTER_SECONDS  # 129600
        # Account a: offline-only usable tx with fetched_at = now - 129599 (fresh!).
        # Account b: offline-only empty / no component -> None (stale!).
        # Account c: empty live sync with last_success = now + 500 -> clamped to now (fresh!).
        # Account d: component last_success = now - 129600 (stale!).
        # Account e: only future posting_date tx (2026-11-23), no component -> ignored -> None (stale!).
        txs = [
            _tx("acc-a", "off-1", booking_date="2026-11-20", observed_at=now - (stale_cutoff - 1)),
            _tx("acc-e", "fut-1", booking_date="2026-11-23", observed_at=now),
        ]
        res = _eval(
            evaluated_at=now,
            transactions=txs,
            account_component_timestamps={
                "acc-a": None,
                "acc-b": None,
                "acc-c": now + 500,
                "acc-d": now - stale_cutoff,
                "acc-e": None,
            },
            settings=None,
        )
        self.assertEqual(res["coverage"]["ledger_as_of"], now)
        self.assertEqual(res["coverage"]["stale_selected_accounts"], 3)  # b, d, e

        # Only offline-only empty / unresolved -> ledger_as_of is None.
        empty = _eval(evaluated_at=now, transactions=[], account_component_timestamps={"acc-a": None}, settings=None)
        self.assertIsNone(empty["coverage"]["ledger_as_of"])
        self.assertEqual(empty["coverage"]["stale_selected_accounts"], 1)

        # Offline-only usable row sets ledger_as_of to its observed_at (fetched_at) independent of period/currency.
        offline_only = _eval(
            period="2026-01",
            currency="EUR",
            evaluated_at=now,
            transactions=[_tx("acc-a", "off-pln", currency="PLN", booking_date="2026-11-10", observed_at=now - 1000)],
            account_component_timestamps={"acc-a": None},
            settings=None,
        )
        self.assertEqual(offline_only["coverage"]["ledger_as_of"], now - 1000)
        self.assertEqual(offline_only["coverage"]["stale_selected_accounts"], 0)

    def test_corrupt_stored_inputs_fail_with_month_data_error(self):
        base_limit = {"category_id": "cat-food", "currency": "PLN", "monthly_limit": "100", "warning_percent": 80, "enabled": True}
        bad_limits = [
            [base_limit | {"category_id": "missing"}],
            [base_limit | {"currency": "pln"}],
            [base_limit | {"warning_percent": 100}],
            [base_limit | {"enabled": 1}],
            [base_limit, base_limit],
            [base_limit | {"monthly_limit": "0"}],
        ]
        for limits in bad_limits:
            with self.subTest(limits=limits), self.assertRaises(month_budget.MonthDataError):
                _eval(limits=limits)
        bad_tx = _tx("acc-a", "bad-amt")
        bad_tx["payload"]["amount"] = "bad-money"
        with self.assertRaises(month_budget.MonthDataError):
            _eval(transactions=[bad_tx])
        with self.assertRaises(month_budget.MonthDataError):
            _eval(transactions=[_tx("acc-a", "bad-obs", observed_at="bad-ts")])
        with self.assertRaises(month_budget.MonthDataError):
            _eval(account_component_timestamps={"acc-a": "bad-ts"})
        with self.assertRaises(month_budget.MonthDataError):
            _eval(
                transactions=[_tx("acc-a", "bad-debtor", direction="CRDT", booking_date="2026-11-10",
                                  kind="income", category_id="cat-salary", source="manual",
                                  metadata={"debtor_name": 123})],
                settings=month_budget.MonthBudgetSettings(("cat-salary",)),
            )
