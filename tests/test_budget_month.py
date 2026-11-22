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


def _eval_charts(*, history: int = 6, **kwargs) -> dict:
    evaluated_at = kwargs.get("evaluated_at") or _utc_ts("2026-11-22T12:00:00Z")
    categories = kwargs.get("categories") or [
        _cat("cat-salary", "Wynagrodzenie"),
        _cat("cat-zus", "Świadczenia ZUS"),
        _cat("cat-rent", "Najem"),
        _cat("cat-mortgage", "Hipoteka"),
        _cat("cat-food", "Zakupy"),
        _cat("cat-other-inc", "Inne"),
    ]
    timestamps = kwargs.get("account_component_timestamps") or {"acc-a": evaluated_at, "acc-b": evaluated_at}
    identities = kwargs.get("identities") or {acct: [] for acct in timestamps}
    return month_budget.evaluate_monthly_budget_charts(
        owner="owner",
        period=kwargs.get("period", "2026-11"),
        currency=kwargs.get("currency", "PLN"),
        history=history,
        evaluated_at=evaluated_at,
        transactions=kwargs.get("transactions", ()),
        transfers=kwargs.get("transfers", ()),
        scan_limited=kwargs.get("scan_limited", False),
        categories=categories,
        rules=kwargs.get("rules", ()),
        identities=identities,
        limits=kwargs.get("limits", ()),
        account_component_timestamps=timestamps,
        account_kinds=kwargs.get("account_kinds") or {},
        settings=kwargs.get("settings"),
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
                "income_window_start_day": 20,
                "pass_through": {"rent_category_id": "cat-rent", "mortgage_category_id": "cat-mortgage"},
            },
            "empty_owner": {"income_category_ids": []},
        })
        parsed = month_budget.load_month_config({month_budget.MONTH_CONFIG_ENV: valid})
        self.assertEqual(parsed["owner"].income_category_ids, ("cat-salary", "cat-zus"))
        self.assertEqual(parsed["owner"].income_window_start_day, 20)
        self.assertEqual(parsed["owner"].pass_through.rent_category_id, "cat-rent")
        self.assertEqual(parsed["empty_owner"], month_budget.MonthBudgetSettings(income_category_ids=(), pass_through=None))
        self.assertEqual(parsed["empty_owner"].income_window_start_day, 1)

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

        for bad_start_day in (0, 29, "20", 1.0, True, False, None):
            raw = json.dumps({"owner": {"income_category_ids": [], "income_window_start_day": bad_start_day}})
            with self.subTest(start_day=bad_start_day), self.assertRaises(BudgetConfigError):
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

    def test_pending_row_keys_match_pending_transfer_rows_and_cap_at_500(self):
        cfg = month_budget.MonthBudgetSettings(
            ("cat-salary",),
            pass_through=month_budget.PassThroughSettings("cat-rent", "cat-mortgage"),
            income_window_start_day=1,
        )
        rules = [
            {"id": "rule-auto", "kind": "auto", "category_id": "cat-food"},
            {"id": "rule-exp", "kind": "expense", "category_id": "cat-food"},
        ]
        txs = [
            # Counted pending rows across all 4 coverage paths:
            _tx("acc-a", "imp-dbit", amount="40"),
            _tx("acc-a", "rule-auto-dbit", amount="50", kind="expense", category_id="cat-food",
                source="rule", rule_id="rule-auto"),
            _tx("acc-card", "card-stale-excl", amount="60", kind="expense", category_id="cat-food",
                source="rule", rule_id="rule-exp",
                annotation={"version": 1, "mode": "manual", "category_id": None, "kind": "excluded", "stale": True}),
            _tx("acc-a", "ref-sug", amount="15", direction="CRDT", kind="refund", category_id="cat-food",
                source="manual", transfer_status="suggested"),
            _tx("acc-a", "rent-rev", amount="800", direction="CRDT", kind="income", category_id="cat-rent",
                source="manual", transfer_status="review"),
            _tx("acc-a", "sal-bnd", amount="5000", direction="CRDT", kind="income", category_id="cat-salary",
                source="manual", transfer_status="boundary"),
            # Non-pending rows that must NOT appear in pending_row_keys:
            _tx("acc-card", "card-clean", amount="70"),
            _tx("acc-a", "rule-exp-clean", amount="80", kind="expense", category_id="cat-food",
                source="rule", rule_id="rule-exp"),
            _tx("acc-a", "manual-exp-clean", amount="90", kind="expense", category_id="cat-food", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
            _tx("acc-a", "unclass-sug", amount="25", direction="CRDT", transfer_status="suggested"),
            _tx("acc-a", "zero-amt", amount="0"),
            _tx("acc-a", "other-month", amount="33", booking_date="2026-10-15"),
        ]
        res = _eval(
            transactions=txs,
            rules=rules,
            account_kinds={"acc-a": "current", "acc-card": "credit_card"},
            settings=cfg,
        )
        expected_keys = [
            "acc-a/imp-dbit",
            "acc-a/rule-auto-dbit",
            "acc-card/card-stale-excl",
            "acc-a/ref-sug",
            "acc-a/rent-rev",
            "acc-a/sal-bnd",
        ]
        self.assertEqual(res["coverage"]["pending_transfer_rows"], len(expected_keys))
        self.assertEqual(res["coverage"]["pending_row_keys"], expected_keys)
        self.assertFalse(res["coverage"]["pending_row_keys_truncated"])
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(res)

        charts = _eval_charts(
            transactions=txs,
            rules=rules,
            account_kinds={"acc-a": "current", "acc-card": "credit_card"},
            settings=cfg,
        )
        self.assertEqual(charts["budget"]["coverage"]["pending_row_keys"], expected_keys)
        self.assertFalse(charts["budget"]["coverage"]["pending_row_keys_truncated"])
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(charts)

        clean_only = _eval(
            transactions=[_tx("acc-card", "card-clean", amount="70")],
            account_kinds={"acc-card": "credit_card"},
            settings=cfg,
        )
        self.assertEqual(clean_only["coverage"]["pending_transfer_rows"], 0)
        self.assertEqual(clean_only["coverage"]["pending_row_keys"], [])
        self.assertFalse(clean_only["coverage"]["pending_row_keys_truncated"])
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(clean_only)

        exact_500 = _eval(
            transactions=[_tx("acc-a", f"eq-{idx:03d}", amount="1") for idx in range(500)],
            settings=cfg,
        )
        self.assertEqual(exact_500["coverage"]["pending_transfer_rows"], 500)
        self.assertEqual(len(exact_500["coverage"]["pending_row_keys"]), 500)
        self.assertFalse(exact_500["coverage"]["pending_row_keys_truncated"])

        many_txs = [_tx("acc-a", f"p-{idx:03d}", amount="1") for idx in range(505)]
        capped = _eval(transactions=many_txs, settings=cfg)
        self.assertEqual(capped["coverage"]["pending_transfer_rows"], 505)
        self.assertEqual(len(capped["coverage"]["pending_row_keys"]), month_budget.MAX_PENDING_ROW_KEYS)
        self.assertEqual(capped["coverage"]["pending_row_keys"], [f"acc-a/p-{idx:03d}" for idx in range(500)])
        self.assertTrue(capped["coverage"]["pending_row_keys_truncated"])
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(capped)


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


class MonthlyBudgetIncomeWindowTests(unittest.TestCase):
    def test_salary_zus_weekend_expected_income_and_closed_month_window(self):
        cfg = month_budget.MonthBudgetSettings(
            ("cat-salary", "cat-zus"), income_window_start_day=20,
        )

        def credit(ref, booking_date, amount, name, category_id="cat-salary"):
            return _tx(
                "acc-a", ref, amount=amount, direction="CRDT", booking_date=booking_date,
                kind="income", category_id=category_id, source="manual", metadata={"debtor_name": name},
            )

        transactions = [
            credit("sep-a", "2026-09-26", "5000", "Employer A"),
            credit("oct-a", "2026-10-26", "5000", "Employer A"),
            credit("nov-a", "2026-11-26", "5500", "Employer A"),
            credit("sep-b", "2026-09-30", "6000", "Employer B"),
            credit("oct-b", "2026-10-30", "6000", "Employer B"),
            credit("nov-b", "2026-11-30", "6500", "Employer B"),
            credit("sep-c", "2026-09-30", "7000", "Employer C"),
            credit("weekend-c", "2026-11-01", "7000", "Employer C"),
            credit("oct-zus", "2026-10-10", "800", "ZUS", "cat-zus"),
            credit("nov-zus", "2026-11-10", "800", "ZUS", "cat-zus"),
        ]
        early = _eval(
            period="2026-11", evaluated_at=_utc_ts("2026-11-05T12:00:00Z"),
            transactions=transactions, settings=cfg,
        )
        self.assertEqual(early["summary"]["start_budget"], "18000")
        self.assertEqual(early["expected_income"], {
            "status": "estimated", "total": "800",
            "items": [{"category_id": "cat-zus", "amount": "800", "expected_date": "2026-11-10",
                       "source": "previous_month"}],
        })
        self.assertEqual(early["pace"]["budget"], "18800")
        self.assertEqual(early["pace"]["basis"], "received_plus_expected")

        november = _eval(
            period="2026-11", evaluated_at=_utc_ts("2026-11-30T12:00:00Z"),
            transactions=transactions, settings=cfg,
        )
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(november)
        self.assertEqual(november["configuration"], {
            "status": "ready", "source": "bootstrap",
            "income_window": {"start": "2026-10-20", "end": "2026-11-19", "start_day": 20},
        })
        self.assertEqual(november["summary"], {"start_budget": "18800", "spent": "0", "remaining": "18800"})
        self.assertEqual(november["expected_income"], {"status": "none", "total": "0", "items": []})
        self.assertEqual(november["pace"]["budget"], "18800")
        self.assertEqual(november["pace"]["basis"], "received")

        december = _eval(
            period="2026-12", evaluated_at=_utc_ts("2027-01-02T12:00:00Z"),
            transactions=transactions, settings=cfg,
        )
        self.assertEqual(december["configuration"]["income_window"], {
            "start": "2026-11-20", "end": "2026-12-19", "start_day": 20,
        })
        self.assertEqual(december["summary"]["start_budget"], "12000")
        self.assertEqual(december["expected_income"], {"status": "historical", "total": None, "items": []})
        self.assertEqual(december["pace"]["basis"], "received")
        self.assertEqual(december["pace"]["budget"], "12000")

    def test_window_edges_at_warsaw_midnight_in_winter_and_summer(self):
        cfg = month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=20)
        cases = (
            ("winter", "2026-12", "2026-11-19", "2026-11-20", "2026-12-19", "2026-12-20",
             (("2026-12-19T22:59:59Z", "2026-12-19"), ("2026-12-19T23:00:00Z", "2026-12-20"))),
            ("summer", "2026-07", "2026-06-19", "2026-06-20", "2026-07-19", "2026-07-20",
             (("2026-07-19T21:59:59Z", "2026-07-19"), ("2026-07-19T22:00:00Z", "2026-07-20"))),
        )
        for label, period, before, start, end, after, instants in cases:
            transactions = [
                _tx("acc-a", label + booking, amount=amount, direction="CRDT", booking_date=booking,
                    kind="income", category_id="cat-salary", source="manual")
                for booking, amount in ((before, "10"), (start, "100"), (end, "200"), (after, "400"))
            ]
            for instant, evaluation_date in instants:
                with self.subTest(season=label, instant=instant):
                    result = _eval(
                        period=period, evaluated_at=_utc_ts(instant), transactions=transactions, settings=cfg,
                    )
                    self.assertEqual(result["evaluation_date"], evaluation_date)
                    self.assertEqual(result["cutoff"], evaluation_date)
                    self.assertEqual(result["configuration"]["income_window"],
                                     {"start": start, "end": end, "start_day": 20})
                    self.assertEqual(result["summary"]["start_budget"], "300")
                    self.assertEqual(result["pace"]["budget"], "300")

    def test_unmatched_expected_income_clamps_to_current_window(self):
        cfg = month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=20)
        prior = _tx(
            "acc-a", "prior-late", amount="5000", direction="CRDT", booking_date="2026-09-26",
            kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer A"},
        )
        result = _eval(transactions=[prior], settings=cfg)
        self.assertEqual(result["configuration"]["income_window"], {
            "start": "2026-10-20", "end": "2026-11-19", "start_day": 20,
        })
        self.assertEqual(result["expected_income"], {
            "status": "estimated", "total": "5000",
            "items": [{"category_id": "cat-salary", "amount": "5000", "expected_date": "2026-11-19",
                       "source": "previous_month"}],
        })

        prior_other = _tx(
            "acc-a", "prior-other", amount="6000", direction="CRDT", booking_date="2026-10-10",
            kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer B"},
        )
        current_other = _tx(
            "acc-a", "current-other", amount="6500", direction="CRDT", booking_date="2026-11-10",
            kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer B"},
        )
        with_current = _eval(transactions=[prior, prior_other, current_other], settings=cfg)
        self.assertEqual(with_current["expected_income"], result["expected_income"])

    def test_year_boundary_and_february_day_28_windows(self):
        cases = (
            ("january", "2027-01", 20, "2026-12-20", "2027-01-19", "2026-12-19", "2027-01-20",
             "2027-01-20T12:00:00Z", "2026-11-26", "2027-01-19"),
            ("non_leap_february", "2027-02", 28, "2027-01-28", "2027-02-27", "2027-01-27", "2027-02-28",
             "2027-02-28T12:00:00Z", "2026-12-28", "2027-02-27"),
            ("leap_february", "2028-02", 28, "2028-01-28", "2028-02-27", "2028-01-27", "2028-02-28",
             "2028-02-29T12:00:00Z", "2027-12-28", "2028-02-27"),
        )
        for (label, period, start_day, start, end, before, after, instant,
             prior_booking, expected_date) in cases:
            with self.subTest(case=label):
                cfg = month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=start_day)
                transactions = [
                    _tx("acc-a", f"{label}-{booking}", amount=amount, direction="CRDT", booking_date=booking,
                        kind="income", category_id="cat-salary", source="manual")
                    for booking, amount in ((before, "10"), (start, "100"), (end, "200"), (after, "400"))
                ]
                result = _eval(
                    period=period, evaluated_at=_utc_ts(instant), transactions=transactions, settings=cfg,
                )
                self.assertEqual(result["configuration"]["income_window"], {
                    "start": start, "end": end, "start_day": start_day,
                })
                self.assertEqual(result["summary"]["start_budget"], "300")

                prior = _tx(
                    "acc-a", f"{label}-prior", amount="5000.25", direction="CRDT", booking_date=prior_booking,
                    kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"},
                )
                expected = _eval(period=period, evaluated_at=_utc_ts(instant), transactions=[prior], settings=cfg)
                self.assertEqual(expected["expected_income"], {
                    "status": "estimated", "total": "5000.25",
                    "items": [{"category_id": "cat-salary", "amount": "5000.25", "expected_date": expected_date,
                               "source": "previous_month"}],
                })

    def test_budget_start_income_before_calendar_month_marks_activity(self):
        income = _tx(
            "acc-a", "early-income", amount="5000", direction="CRDT", booking_date="2026-10-26",
            kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"},
        )
        result = _eval(
            period="2026-11", evaluated_at=_utc_ts("2026-11-05T12:00:00Z"), transactions=[income],
            settings=month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=20),
        )
        self.assertEqual(result["summary"], {"start_budget": "5000", "spent": "0", "remaining": "5000"})
        self.assertEqual(result["category_spend"], [])
        self.assertTrue(result["coverage"]["has_activity"])

        calendar_month = _eval(
            period="2026-11", evaluated_at=_utc_ts("2026-11-05T12:00:00Z"), transactions=[income],
            settings=month_budget.MonthBudgetSettings(("cat-salary",)),
        )
        self.assertEqual(calendar_month["summary"]["start_budget"], "0")
        self.assertFalse(calendar_month["coverage"]["has_activity"])

    def test_start_day_one_is_byte_for_byte_calendar_month_regression(self):
        transactions = [
            _tx("acc-a", "oct", amount="5000", direction="CRDT", booking_date="2026-10-26",
                kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"}),
            _tx("acc-a", "nov", amount="5500", direction="CRDT", booking_date="2026-11-26",
                kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"}),
        ]
        kwargs = {
            "period": "2026-11", "evaluated_at": _utc_ts("2026-11-30T12:00:00Z"),
            "transactions": transactions,
        }
        implicit = _eval(**kwargs, settings=month_budget.MonthBudgetSettings(("cat-salary",)))
        explicit = _eval(
            **kwargs,
            settings=month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=1),
        )
        self.assertEqual(explicit, implicit)
        self.assertEqual(implicit["configuration"], {"status": "ready", "source": "bootstrap"})
        self.assertEqual(implicit["summary"]["start_budget"], "5500")

    def test_spending_refunds_and_rent_remain_on_calendar_month(self):
        cfg = month_budget.MonthBudgetSettings(
            ("cat-salary",),
            pass_through=month_budget.PassThroughSettings("cat-rent", "cat-mortgage"),
            income_window_start_day=20,
        )
        transactions = [
            _tx("acc-a", "old-expense", amount="100", booking_date="2026-10-25",
                kind="expense", category_id="cat-food", source="manual"),
            _tx("acc-a", "new-expense", amount="40", booking_date="2026-11-05",
                kind="expense", category_id="cat-food", source="manual"),
            _tx("acc-a", "old-refund", amount="10", direction="CRDT", booking_date="2026-10-25",
                kind="refund", category_id="cat-food", source="manual"),
            _tx("acc-a", "new-refund", amount="5", direction="CRDT", booking_date="2026-11-06",
                kind="refund", category_id="cat-food", source="manual"),
            _tx("acc-a", "old-rent", amount="2000", direction="CRDT", booking_date="2026-10-25",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "new-rent", amount="1000", direction="CRDT", booking_date="2026-11-07",
                kind="income", category_id="cat-rent", source="manual"),
        ]
        result = _eval(transactions=transactions, settings=cfg)
        self.assertEqual(result["summary"], {"start_budget": "0", "spent": "35", "remaining": "-35"})
        self.assertEqual(result["category_spend"][0]["expenses"], "40")
        self.assertEqual(result["category_spend"][0]["refunds"], "5")
        self.assertEqual(result["pass_through"]["rent_received"], "1000")


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


class MonthlyBudgetChartsTests(unittest.TestCase):
    def _assert_spending_identity(self, charts: dict) -> None:
        budget = charts["budget"]
        series = charts["series"]
        self.assertIsNotNone(series)
        spent = budget["summary"]["spent"]
        self.assertEqual(series["daily"][-1]["cumulative_spent"], spent)
        self.assertEqual(series["months"][-1]["period"], budget["period"])
        self.assertEqual(series["months"][-1]["spent"], spent)
        self.assertEqual(
            month_budget.core.sum_money(row["net_spending"] for row in budget["category_spend"]),
            spent,
        )
        self.assertEqual(
            month_budget.core.sum_money(row["net_spending"] for row in series["months"][-1]["categories"]),
            spent,
        )

    def test_charts_query_validation_and_unconfigured_series_null(self):
        self.assertEqual(month_budget.validate_monthly_budget_charts_query(None, None, None), (None, "PLN", 6))
        self.assertEqual(month_budget.validate_monthly_budget_charts_query("2026-10", "EUR", "6"), ("2026-10", "EUR", 6))
        self.assertEqual(month_budget.validate_monthly_budget_charts_query(None, "PLN", "12"), (None, "PLN", 12))
        for bad_history in ("", "0", "1", "3", "06", " 6", "6\n", "6.0", "13", "twelve", 6, True):
            with self.subTest(bad_history=bad_history), self.assertRaises(month_budget.MonthInputError):
                month_budget.validate_monthly_budget_charts_query(None, None, bad_history)
        for bad_eval_history in (0, 3, 24, True, "6", None):
            with self.subTest(bad_eval_history=bad_eval_history), self.assertRaises(month_budget.MonthInputError):
                _eval_charts(history=bad_eval_history, settings=DEFAULT_SETTINGS)

        unconfigured = _eval_charts(
            transactions=[_tx("acc-a", "exp", amount="50", booking_date="2026-11-05")],
            settings=None,
        )
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(unconfigured)
        self.assertEqual(unconfigured["budget"]["configuration"], {"status": "unconfigured", "source": "none"})
        self.assertIsNone(unconfigured["series"])

    def test_identity_late_rent_unused_mortgage_refund_negative_refunds_and_uncategorised(self):
        txs = [
            _tx("acc-a", "sal", amount="10000", direction="CRDT", booking_date="2026-11-01",
                kind="income", category_id="cat-salary", source="manual"),
            # Day 3: prior-month food refund -> daily cumulative dips negative (-40)!
            _tx("acc-a", "food-ref", amount="40", direction="CRDT", booking_date="2026-11-03",
                kind="refund", category_id="cat-food", source="manual"),
            # Day 5: mortgage debit 2500 (covered 2000 by late rent on day 20 -> adjusted +500 on Nov 5!)
            _tx("acc-a", "mort-1", amount="2000", booking_date="2026-11-05", transaction_date="2026-11-05",
                kind="expense", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "mort-2", amount="500", booking_date="2026-11-10", transaction_date="2026-11-10",
                kind="expense", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "rent-late", amount="2000", direction="CRDT", booking_date="2026-11-20",
                kind="income", category_id="cat-rent", source="manual"),
            # Day 12: uncategorised expense (120) + day 15: food expense (25)
            _tx("acc-a", "uncat", amount="120", booking_date="2026-11-12", transaction_date="2026-11-12",
                kind="expense", category_id=None, source="manual"),
            _tx("acc-a", "food-exp", amount="25", booking_date="2026-11-15", transaction_date="2026-11-15",
                kind="expense", category_id="cat-food", source="manual"),
        ]
        # Include an enabled limit with zero spending (cat-other-inc) to verify category_spend includes it while
        # series identity holds identically.
        limits = [
            {"category_id": "cat-other-inc", "currency": "PLN", "monthly_limit": "300", "warning_percent": 80, "enabled": True},
        ]
        charts = _eval_charts(transactions=txs, limits=limits, settings=DEFAULT_SETTINGS)
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(charts)
        self._assert_spending_identity(charts)
        self.assertEqual(charts["budget"]["summary"]["spent"], "605")  # -40 + 500 + 120 + 25

        daily = {row["date"]: row["cumulative_spent"] for row in charts["series"]["daily"]}
        self.assertEqual(len(daily), 22)  # 2026-11-01..2026-11-22 including zero-activity days
        self.assertEqual(daily["2026-11-02"], "0")
        self.assertEqual(daily["2026-11-03"], "-40")
        self.assertEqual(daily["2026-11-05"], "-40")  # mort-1 (2000) completely covered by late rent!
        self.assertEqual(daily["2026-11-10"], "460")  # mort-2 (500) uncovered!
        self.assertEqual(daily["2026-11-12"], "580")
        self.assertEqual(daily["2026-11-15"], "605")
        self.assertEqual(daily["2026-11-22"], "605")

        # Unused mortgage refund case: F=700 > M=400, food=100, uncategorised refund=-200 -> net spent=-100.
        refund_txs = [
            _tx("acc-a", "m", amount="400", booking_date="2026-11-05", transaction_date="2026-11-05",
                kind="expense", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "m-r1", amount="400", direction="CRDT", booking_date="2026-11-06",
                kind="refund", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "m-r2", amount="300", direction="CRDT", booking_date="2026-11-07",
                kind="refund", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "rent", amount="1000", direction="CRDT", booking_date="2026-11-08",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "food", amount="100", booking_date="2026-11-09", transaction_date="2026-11-09",
                kind="expense", category_id="cat-food", source="manual"),
            _tx("acc-a", "uncat-ref", amount="200", direction="CRDT", booking_date="2026-11-11",
                kind="refund", category_id=None, source="manual"),
        ]
        refund_charts = _eval_charts(transactions=refund_txs, settings=DEFAULT_SETTINGS)
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(refund_charts)
        self._assert_spending_identity(refund_charts)
        r_daily = {row["date"]: row["cumulative_spent"] for row in refund_charts["series"]["daily"]}
        self.assertEqual(r_daily["2026-11-05"], "400")
        self.assertEqual(r_daily["2026-11-06"], "0")
        self.assertEqual(r_daily["2026-11-07"], "0")  # unused mortgage refund stays out!
        self.assertEqual(r_daily["2026-11-09"], "100")
        self.assertEqual(r_daily["2026-11-11"], "-100")
        self.assertEqual(refund_charts["budget"]["summary"]["spent"], "-100")
        labels = refund_charts["series"]["category_labels"]
        self.assertEqual([item["category_id"] for item in labels], ["cat-food", "cat-mortgage"])

    def test_history_6_12_earliest_1900_closed_vs_current_provisional_and_income_window(self):
        cfg = month_budget.MonthBudgetSettings(
            ("cat-salary", "cat-zus"),
            pass_through=month_budget.PassThroughSettings("cat-rent", "cat-mortgage"),
            income_window_start_day=20,
        )
        cats = [
            _cat("cat-salary", "Wynagrodzenie"),
            _cat("cat-zus", "Świadczenia ZUS"),
            _cat("cat-rent", "Najem"),
            _cat("cat-mortgage", "Hipoteka"),
            _cat("cat-food", "Zakupy", archived=True),
        ]
        txs = [
            # Income in 2025-12 window [2025-11-20..2025-12-19]
            _tx("acc-a", "inc-dec25", amount="4000", direction="CRDT", booking_date="2025-11-25",
                kind="income", category_id="cat-salary", source="manual"),
            # Income in 2026-10 window [2026-09-20..2026-10-19]
            _tx("acc-a", "inc-oct", amount="5000", direction="CRDT", booking_date="2026-09-26",
                kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Acme"}),
            # Income in 2026-11 window [2026-10-20..2026-11-19] -> salary 2026-10-30 + weekend 2026-11-01 + ZUS 2026-11-10
            _tx("acc-a", "inc-nov-1", amount="6000", direction="CRDT", booking_date="2026-10-30",
                kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Acme"}),
            _tx("acc-a", "inc-nov-zus", amount="800", direction="CRDT", booking_date="2026-11-10",
                kind="income", category_id="cat-zus", source="manual", metadata={"debtor_name": "ZUS"}),
            # Income in 2026-12 window [2026-11-20..2026-12-19] -> must NOT enter Nov!
            _tx("acc-a", "inc-dec26", amount="7000", direction="CRDT", booking_date="2026-11-26",
                kind="income", category_id="cat-salary", source="manual"),
            # Closed month 2026-09: implicit debit -> provisional=True on 2026-09 bucket only!
            _tx("acc-a", "sep-imp", amount="120", booking_date="2026-09-10", transaction_date="2026-09-10"),
            # Closed month 2026-08: excluded only -> has_activity=True, provisional=False even with scan_limited=True!
            _tx("acc-a", "aug-excl", amount="50", booking_date="2026-08-10", kind="excluded", source="manual"),
            # Closed month 2026-07: refund -> negative historical bucket spent!
            _tx("acc-a", "jul-ref", amount="35", direction="CRDT", booking_date="2026-07-14",
                kind="refund", category_id="cat-food", source="manual"),
            # Current month 2026-11: clean explicit expense on card/manual -> provisional=False when scan_limited=False
            _tx("acc-a", "nov-exp", amount="250", booking_date="2026-11-04", transaction_date="2026-11-04",
                kind="expense", category_id="cat-food", source="manual",
                annotation={"version": 1, "mode": "manual", "category_id": "cat-food", "kind": "expense", "stale": False}),
            # Missing ZUS in Oct -> partial expected income in Nov!
        ]
        # Evaluated at 2026-11-28 (current month): 12 months
        current_12 = _eval_charts(
            period="2026-11",
            history=12,
            evaluated_at=_utc_ts("2026-11-28T12:00:00Z"),
            transactions=txs,
            categories=cats,
            scan_limited=True,
            settings=cfg,
        )
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(current_12)
        self._assert_spending_identity(current_12)
        months_12 = current_12["series"]["months"]
        self.assertEqual([m["period"] for m in months_12], [
            "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
            "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11",
        ])
        by_m = {m["period"]: m for m in months_12}
        self.assertEqual(by_m["2025-12"]["start_budget"], "4000")
        self.assertEqual(by_m["2026-07"]["spent"], "-35")
        self.assertEqual(by_m["2026-08"]["has_activity"], True)
        self.assertFalse(by_m["2026-08"]["provisional"])  # excluded-only ignores scan_limited!
        self.assertTrue(by_m["2026-09"]["provisional"])
        self.assertEqual(by_m["2026-09"]["spent"], "120")
        self.assertEqual(by_m["2026-10"]["start_budget"], "5000")
        self.assertFalse(by_m["2026-10"]["is_current_month"])
        self.assertFalse(by_m["2026-10"]["income_estimate_partial"])
        self.assertEqual(by_m["2026-11"]["start_budget"], "6800")
        self.assertTrue(by_m["2026-11"]["is_current_month"])
        self.assertTrue(by_m["2026-11"]["income_estimate_partial"])
        self.assertTrue(by_m["2026-11"]["provisional"])  # scan_limited + affecting unexcluded rows
        self.assertEqual(len(current_12["series"]["daily"]), 28)
        self.assertEqual(current_12["series"]["category_labels"], [
            {"category_id": "cat-food", "name": "Zakupy", "archived": True},
        ])

        # Closed month 2026-10 evaluated at 2026-11-28: full 31 daily points, all buckets is_current_month=False & partial=False
        closed_6 = _eval_charts(
            period="2026-10",
            history=6,
            evaluated_at=_utc_ts("2026-11-28T12:00:00Z"),
            transactions=txs,
            categories=cats,
            settings=cfg,
        )
        api_contract.BudgetAnalyticsMonthlyBudgetCharts.model_validate(closed_6)
        self._assert_spending_identity(closed_6)
        self.assertEqual(len(closed_6["series"]["daily"]), 31)
        self.assertEqual([m["period"] for m in closed_6["series"]["months"]],
                         ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"])
        self.assertFalse(any(m["is_current_month"] for m in closed_6["series"]["months"]))
        self.assertFalse(any(m["income_estimate_partial"] for m in closed_6["series"]["months"]))

        # Earliest supported period clamping (1900-03 -> 3 buckets, 1900-01 -> 1 bucket + 1899-12 income window)
        early_cfg = month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=20)
        early_txs = [
            _tx("acc-a", "old-inc", amount="300", direction="CRDT", booking_date="1899-12-25",
                kind="income", category_id="cat-salary", source="manual"),
        ]
        early_1900 = _eval_charts(
            period="1900-03", history=6, evaluated_at=_utc_ts("2026-11-22T12:00:00Z"),
            transactions=early_txs, categories=cats, settings=early_cfg,
        )
        self.assertEqual([m["period"] for m in early_1900["series"]["months"]], ["1900-01", "1900-02", "1900-03"])
        self.assertEqual(early_1900["series"]["months"][0]["start_budget"], "300")

        # 192-char aggregate bound enforced on daily & monthly buckets
        with mock.patch.object(month_budget, "MAX_AGGREGATE_CHARS", 4):
            with self.assertRaises(month_budget.MonthLimitError):
                _eval_charts(
                    period="2026-11",
                    evaluated_at=_utc_ts("2026-11-22T12:00:00Z"),
                    transactions=[_tx("acc-a", "sep-big", amount="12345", booking_date="2026-09-10")],
                    categories=cats,
                    settings=cfg,
                )

    def test_evaluate_monthly_budget_matches_charts_budget(self):
        cfg = month_budget.MonthBudgetSettings(
            ("cat-salary", "cat-zus"),
            pass_through=month_budget.PassThroughSettings("cat-rent", "cat-mortgage"),
            income_window_start_day=20,
        )
        txs = [
            _tx("acc-a", "oct-sal", amount="5000", direction="CRDT", booking_date="2026-09-26",
                kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"}),
            _tx("acc-a", "nov-sal", amount="5500", direction="CRDT", booking_date="2026-10-26",
                kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"}),
            _tx("acc-a", "oct-zus", amount="800", direction="CRDT", booking_date="2026-10-10",
                kind="income", category_id="cat-zus", source="manual", metadata={"debtor_name": "ZUS"}),
            _tx("acc-a", "mort", amount="2200", booking_date="2026-11-05", transaction_date="2026-11-05",
                kind="expense", category_id="cat-mortgage", source="manual"),
            _tx("acc-a", "rent", amount="1800", direction="CRDT", booking_date="2026-11-12",
                kind="income", category_id="cat-rent", source="manual"),
            _tx("acc-a", "food-ref", amount="30", direction="CRDT", booking_date="2026-11-07",
                kind="refund", category_id="cat-food", source="manual"),
            _tx("acc-a", "imp", amount="45", booking_date="2026-11-09"),
        ]
        limits = [{"category_id": "cat-food", "currency": "PLN", "monthly_limit": "100", "warning_percent": 80, "enabled": True}]
        ts = _utc_ts("2026-11-22T12:00:00Z")

        for settings, period in ((cfg, "2026-11"), (cfg, "2026-10"), (None, "2026-11")):
            with self.subTest(configured=settings is not None, period=period):
                budget = _eval(period=period, evaluated_at=ts, transactions=txs, limits=limits, settings=settings)
                charts = _eval_charts(period=period, history=12, evaluated_at=ts, transactions=txs, limits=limits, settings=settings)
                self.assertEqual(charts["budget"], budget)

        # Window income before calendar month marks has_activity on both budget and matching series bucket.
        early_income = _tx(
            "acc-a", "early-inc", amount="5000", direction="CRDT", booking_date="2026-10-26",
            kind="income", category_id="cat-salary", source="manual", metadata={"debtor_name": "Employer"},
        )
        early_cfg = month_budget.MonthBudgetSettings(("cat-salary",), income_window_start_day=20)
        early_ts = _utc_ts("2026-11-05T12:00:00Z")
        early_budget = _eval(period="2026-11", evaluated_at=early_ts, transactions=[early_income], settings=early_cfg)
        early_charts = _eval_charts(period="2026-11", evaluated_at=early_ts, transactions=[early_income], settings=early_cfg)
        self.assertEqual(early_charts["budget"], early_budget)
        self.assertTrue(early_charts["budget"]["coverage"]["has_activity"])
        self.assertTrue(early_charts["series"]["months"][-1]["has_activity"])
