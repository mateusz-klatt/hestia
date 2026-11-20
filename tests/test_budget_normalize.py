"""Synthetic banking snapshots: identity, exact money, complete paging and limits."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import json
import unittest
from unittest import mock

from hestia.budget import normalize
from hestia.budget.client import BudgetAPIError
from hestia.budget.config import ConnectionConfig
from hestia.budget.normalize import (AccountFailure, AccountSnapshot, BudgetDataError, Snapshot, fetch_snapshot,
                                     normalize_balances, normalize_transaction)

SESSION = "10000000-0000-0000-0000-000000000001"
ACCOUNT = "20000000-0000-0000-0000-000000000001"
OTHER = "20000000-0000-0000-0000-000000000002"
PRIMARY = "synthetic-primary-hash"
TODAY = date(2026, 11, 20)
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()


def transaction(reference="entry-1", **changes):
    row = {"entry_reference": reference, "transaction_amount": {"amount": "000123.4500", "currency": "PLN"},
           "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-20",
           "value_date": "2026-11-19", "transaction_date": "2026-11-18",
           "remittance_information": ["Synthetic purchase"], "transaction_id": "transient-detail-id",
           "arbitrary_private_field": "do-not-retain"}
    return {**row, **changes}


def balance(**changes):
    return {"name": "Synthetic balance", "balance_type": "CLBD", "reference_date": "2026-11-20",
            "balance_amount": {"amount": "-00100.1200", "currency": "PLN"},
            "last_committed_transaction": "not-retained", **changes}


def session():
    return {"status": "AUTHORIZED", "aspsp": {"name": "Synthetic Bank", "country": "PL"},
            "access": {"valid_until": "2027-01-01T00:00:00+00:00"},
            "accounts_data": [{"identification_hash": PRIMARY, "uid": ACCOUNT},
                              {"identification_hash": "unselected", "uid": OTHER,
                               "identification_hashes": [PRIMARY]}]}


class FakeClient:
    def __init__(self):
        self.session = session()
        self.calls = []
        self.balances = {ACCOUNT: {"balances": [balance()]}, OTHER: {"balances": []}}
        self.pages = {ACCOUNT: [{"transactions": [transaction()]}], OTHER: [{"transactions": []}]}
        self.positions = {ACCOUNT: 0, OTHER: 0}

    def get(self, path, query=None):
        self.calls.append((path, deepcopy(query)))
        if path == "/sessions/" + SESSION:
            return deepcopy(self.session)
        _, _, uid, kind = path.split("/")
        if kind == "balances":
            if isinstance(self.balances[uid], Exception):
                raise self.balances[uid]
            return deepcopy(self.balances[uid])
        position = self.positions[uid]
        self.positions[uid] += 1
        result = self.pages[uid][position]
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class NormalizeTransactionTests(unittest.TestCase):
    def test_portable_allowlist_canonical_money_and_all_statuses(self):
        for status in normalize.TRANSACTION_STATUSES:
            result = normalize_transaction(transaction(status=status))
            self.assertEqual(result["status"], status)
            self.assertEqual(result["amount"], "123.45")
            self.assertEqual(set(result), {"entry_reference", "amount", "currency", "credit_debit_indicator",
                "status", "booking_date", "value_date", "transaction_date", "remittance_information"})
        for amount, expected in (("000", "0"), ("0.0000", "0"), ("10", "10"), ("001.00100", "1.001"),
                                 ("123456789012345678901234567890.123456789", "123456789012345678901234567890.123456789")):
            self.assertEqual(normalize_transaction(transaction(transaction_amount={"amount": amount, "currency": "XXX"}))["amount"], expected)
        self.assertEqual(normalize_transaction(transaction(credit_debit_indicator="CRDT"))["credit_debit_indicator"], "CRDT")

    def test_missing_and_empty_optional_fields_remain_explicit(self):
        for value in (None, ""):
            result = normalize_transaction(transaction(value, booking_date=value, value_date=value,
                                                       transaction_date=value, remittance_information=None))
            for field in ("entry_reference", "booking_date", "value_date", "transaction_date"):
                self.assertIsNone(result[field])
            self.assertEqual(result["remittance_information"], [])
        original = transaction()
        copied = normalize_transaction(original)
        original["remittance_information"].append("later mutation")
        self.assertEqual(copied["remittance_information"], ["Synthetic purchase"])

    def test_invalid_money_is_rejected_without_rounding_or_sign_inversion(self):
        for amount in (1, 1.2, True, None, "", "NaN", "Infinity", "-Infinity", "1e99999999", "+1", "1.", ".5", "1" * 81, "-1", "-0.00", "1,25"):
            with self.subTest(amount=amount), self.assertRaises(BudgetDataError) as caught:
                normalize_transaction(transaction(transaction_amount={"amount": amount, "currency": "PLN"}))
            self.assertEqual(caught.exception.code, "unsupported_amount")
        for currency in (None, 1, "pln", "EURO", ""):
            with self.subTest(currency=currency), self.assertRaises(BudgetDataError):
                normalize_transaction(transaction(transaction_amount={"amount": "1", "currency": currency}))

    def test_malformed_required_optional_and_date_values_fail(self):
        for value in (None, [], 4):
            with self.subTest(value=value), self.assertRaises(BudgetDataError):
                normalize_transaction(value)
        changes = [{"transaction_amount": []}, {"credit_debit_indicator": None}, {"credit_debit_indicator": []},
                   {"status": []}, {"status": "UNKNOWN"}, {"entry_reference": 1}, {"entry_reference": " "},
                   {"entry_reference": "x" * 1025}, {"remittance_information": "notes"},
                   {"remittance_information": [1]}, {"remittance_information": ["x" * 4097]},
                   {"remittance_information": [""] * 101}]
        changes += [{field: value} for field in ("booking_date", "value_date", "transaction_date")
                    for value in (1, "2026-02-30", "2026-1-01", "2026-W01-1")]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(BudgetDataError):
                normalize_transaction(transaction(**change))

    def test_error_text_is_safe_for_unknown_or_malformed_codes(self):
        for code in ("private-bank-text", [], None):
            error = BudgetDataError(code)
            self.assertEqual(error.code, "invalid_data")
            self.assertEqual(str(error), "Invalid budget snapshot: invalid_data")


class NormalizeBalancesTests(unittest.TestCase):
    def test_preserves_types_currencies_negative_balances_without_inference(self):
        rows = [balance(balance_type=kind) for kind in normalize.BALANCE_TYPES]
        rows.append(balance(balance_amount={"amount": "-000.000", "currency": "EUR"}))
        result = normalize_balances({"balances": rows})
        self.assertEqual(len(result), len(rows))
        self.assertEqual(result[0]["balance_amount"], {"amount": "-100.12", "currency": "PLN"})
        self.assertEqual(result[-1]["balance_amount"], {"amount": "0", "currency": "EUR"})
        self.assertNotIn("last_committed_transaction", result[0])
        self.assertEqual(normalize_balances({"balances": []}), ())
        self.assertIsNone(normalize_balances({"balances": [balance(reference_date=None)]})[0]["reference_date"])

    def test_rejects_invalid_lists_rows_names_and_types(self):
        invalid = [None, {}, {"balances": None}, {"balances": {}}, {"balances": [None]}]
        invalid += [{"balances": [balance(**change)]} for change in (
            {"name": None}, {"name": " "}, {"balance_type": []}, {"balance_type": "unsupported"},
            {"balance_amount": {"amount": 1.2, "currency": "PLN"}}, {"reference_date": "yesterday"})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(BudgetDataError):
                normalize_balances(value)


class FetchSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.connection = ConnectionConfig("home", "operator", "Synthetic Bank", "PL", "/not-read/session.json", (PRIMARY,))

    def fetch(self, **kwargs):
        return fetch_snapshot(self.client, SESSION, self.connection, today=TODAY, now=NOW, **kwargs)

    def assert_failed_account(self, code=None):
        snapshot = self.fetch()
        self.assertEqual(snapshot.accounts, ())
        self.assertEqual(len(snapshot.errors), 1)
        self.assertEqual(snapshot.errors[0].identification_hash, PRIMARY)
        if code is not None:
            self.assertEqual(snapshot.errors[0].code, code)
        return snapshot

    def test_fetches_only_selected_primary_hash_and_preserves_frozen_contract(self):
        result = self.fetch()
        self.assertIsInstance(result, Snapshot)
        self.assertIsInstance(result.accounts[0], AccountSnapshot)
        self.assertEqual(result.accounts[0].identification_hash, PRIMARY)
        self.assertEqual(result.date_from, (TODAY - timedelta(days=364)).isoformat())
        self.assertEqual(result.date_to, TODAY.isoformat())
        self.assertEqual(result.valid_until, datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(result.errors, ())
        self.assertEqual(self.client.calls, [("/sessions/" + SESSION, None),
            ("/accounts/" + ACCOUNT + "/transactions", {"date_from": result.date_from, "date_to": result.date_to}),
            ("/accounts/" + ACCOUNT + "/balances", None)])
        with self.assertRaises(FrozenInstanceError):
            result.valid_until = 0
        with self.assertRaises(FrozenInstanceError):
            result.accounts[0].identification_hash = "changed"
        with self.assertRaises(FrozenInstanceError):
            AccountFailure(PRIMARY, "invalid_data").code = "changed"
        self.assertEqual(Snapshot(result.valid_until, (), result.date_from, result.date_to).errors, ())

    def test_empty_selection_still_checks_session_but_never_fetches_accounts(self):
        self.connection = replace(self.connection, accounts=())
        self.assertEqual(self.fetch().accounts, ())
        self.assertEqual(self.client.calls, [("/sessions/" + SESSION, None)])

    def test_reauthorization_uid_is_session_specific_and_case_normalized(self):
        self.client.session["accounts_data"][0]["uid"] = "ABCDEF00-0000-0000-0000-000000000001"
        uid = "abcdef00-0000-0000-0000-000000000001"
        self.client.balances[uid], self.client.pages[uid], self.client.positions[uid] = {"balances": []}, [{"transactions": []}], 0
        result = self.fetch()
        self.assertEqual(result.accounts[0].identification_hash, PRIMARY)
        self.assertEqual(self.client.calls[-2][0], "/accounts/" + uid + "/transactions")

    def test_expired_invalid_or_other_bank_session_fails_before_account_http(self):
        variants = [{"status": "EXPIRED"}, {"status": None}, {"aspsp": {"name": "Other Bank", "country": "PL"}},
                    {"aspsp": {"name": "Synthetic Bank", "country": "DE"}}, {"aspsp": None}, {"access": None}]
        variants += [{"access": {"valid_until": value}} for value in (
            None, 1, "2027-01-01", "2027-01-01T00:00:00", "2027-01-01Tinvalid+00:00", "2027-01-01T12:00:00+25:00",
            "2027-01-01T" + "0" * 65, "2026-11-20T11:59:59Z", "2026-11-20T12:00:00+00:00")]
        for change in variants:
            with self.subTest(change=change):
                self.client = FakeClient()
                self.client.session.update(change)
                with self.assertRaises(BudgetDataError):
                    self.fetch()
                self.assertEqual(len(self.client.calls), 1)
        self.client = FakeClient()
        self.client.session = []
        with self.assertRaises(BudgetDataError):
            self.fetch()

    def test_expiry_offsets_default_clock_and_malformed_clock_inputs(self):
        self.client.session["access"]["valid_until"] = "2027-01-01T02:00:00+02:00"
        with mock.patch.object(normalize.time, "time", return_value=NOW):
            result = fetch_snapshot(self.client, SESSION, self.connection, today=TODAY)
        self.assertEqual(result.valid_until, datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp())
        for now in (True, "now", float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(now_type=type(now).__name__), self.assertRaises(BudgetDataError):
                fetch_snapshot(self.client, SESSION, self.connection, today=TODAY, now=now)

    def test_invalid_today_or_session_identifier_never_calls_provider(self):
        for today in ("2026-11-20", datetime(2026, 11, 20), date(1, 1, 1)):
            with self.subTest(today=today), self.assertRaises(BudgetDataError):
                fetch_snapshot(self.client, SESSION, self.connection, today=today, now=NOW)
        for value in (None, [], "../session", "not-a-uuid"):
            with self.subTest(session=value), self.assertRaises(BudgetDataError):
                fetch_snapshot(self.client, value, self.connection, today=TODAY, now=NOW)
        self.assertEqual(self.client.calls, [])

    def test_missing_primary_duplicate_hash_or_uid_is_rejected(self):
        variants = [None, {}, [], [None], [{"uid": ACCOUNT}], [{"identification_hash": PRIMARY}],
            [{"identification_hash": "another-primary", "identification_hashes": [PRIMARY], "uid": ACCOUNT}],
            [{"identification_hash": PRIMARY, "uid": ACCOUNT}, {"identification_hash": PRIMARY, "uid": OTHER}],
            [{"identification_hash": PRIMARY, "uid": ACCOUNT}, {"identification_hash": "other", "uid": ACCOUNT.upper()}]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.client = FakeClient()
                self.client.session["accounts_data"] = rows
                with self.assertRaises(BudgetDataError):
                    self.fetch()
                self.assertEqual(len(self.client.calls), 1)

    def test_all_pages_including_empty_middle_keep_query_and_dedup_exact_references(self):
        self.client.pages[ACCOUNT] = [
            {"transactions": [transaction()], "continuation_key": "page-2"},
            {"transactions": [], "continuation_key": "page-3"},
            {"transactions": [transaction(), transaction(None), transaction(None)], "continuation_key": None}]
        result = self.fetch()
        self.assertEqual(len(result.accounts[0].transactions), 3)
        queries = [query for path, query in self.client.calls if path.endswith("/transactions")]
        for index, query in enumerate(queries):
            self.assertEqual(query["date_from"], result.date_from)
            self.assertEqual(query["date_to"], result.date_to)
            self.assertEqual(query.get("continuation_key"), (None, "page-2", "page-3")[index])

    def test_conflicting_reference_including_pending_to_booked_keeps_both_for_quarantine(self):
        for change in ({"status": "PDNG"}, {"remittance_information": ["Changed"]}, {"booking_date": "2026-11-19"}):
            with self.subTest(change=change):
                self.client = FakeClient()
                self.client.pages[ACCOUNT] = [{"transactions": [transaction()], "continuation_key": "more"},
                    {"transactions": [transaction(**change), transaction(), transaction(**change)]}]
                snapshot = self.fetch()
                self.assertEqual(snapshot.errors, ())
                self.assertEqual(snapshot.accounts[0].transactions,
                                 (normalize_transaction(transaction()), normalize_transaction(transaction(**change))))

    def test_locally_filters_booked_or_value_date_but_retains_dateless_observations(self):
        start = (TODAY - timedelta(days=364)).isoformat()
        rows = [transaction("at-start", booking_date=start), transaction("at-end"),
                transaction("before", booking_date="2025-01-01"), transaction("after", booking_date="2027-01-01"),
                transaction("value", booking_date=None), transaction("value-out", booking_date=None, value_date="2020-01-01"),
                transaction("dateless", booking_date=None, value_date=None, transaction_date="2020-01-01")]
        self.client.pages[ACCOUNT] = [{"transactions": rows}]
        self.assertEqual([row["entry_reference"] for row in self.fetch().accounts[0].transactions],
                         ["at-start", "at-end", "value", "dateless"])

    def test_invalid_records_outside_the_window_and_nonobject_rows_are_not_dropped(self):
        for row in (None, transaction(booking_date="2020-01-01", transaction_amount={"amount": "NaN", "currency": "PLN"})):
            self.client = FakeClient()
            self.client.pages[ACCOUNT] = [{"transactions": [row]}]
            self.assert_failed_account()

    def test_window_keeps_outside_conflict_versions_when_any_version_is_relevant_or_dateless(self):
        rows = [transaction("relevant", booking_date="2020-01-01"), transaction("relevant"),
                transaction("undated", booking_date="2020-01-01"), transaction("undated", booking_date=None, value_date=None),
                transaction("outside", booking_date="2020-01-01"), transaction("outside", booking_date="2021-01-01"),
                transaction(None, booking_date="2020-01-01"), transaction(None, booking_date=None, value_date=None)]
        self.client.pages[ACCOUNT] = [{"transactions": rows}]
        kept = self.fetch().accounts[0].transactions
        self.assertEqual([row["entry_reference"] for row in kept], ["relevant", "relevant", "undated", "undated", None])

    def test_bad_page_shapes_keys_and_repeat_are_rejected(self):
        for page in (None, {}, {"transactions": {}}, {"transactions": None}):
            self.client = FakeClient()
            self.client.pages[ACCOUNT] = [page]
            self.assert_failed_account()
        for key in ("", False, [], "x" * 8193):
            self.client = FakeClient()
            self.client.pages[ACCOUNT] = [{"transactions": [], "continuation_key": key}]
            self.assert_failed_account("invalid_pagination")
        self.client = FakeClient()
        self.client.pages[ACCOUNT] = [{"transactions": [], "continuation_key": "repeat"}] * 2
        self.assert_failed_account("invalid_pagination")

    def test_page_limit_and_total_raw_record_limit_cover_all_selected_accounts(self):
        self.assertEqual(normalize.MAX_PAGES, 1000)
        self.assertEqual(normalize.MAX_TRANSACTIONS, 100_000)
        self.client.pages[ACCOUNT] = [{"transactions": [], "continuation_key": "1"},
                                     {"transactions": [], "continuation_key": "2"}]
        with mock.patch.object(normalize, "MAX_PAGES", 2):
            self.assert_failed_account("limit_exceeded")
        self.client = FakeClient()
        self.client.pages[ACCOUNT] = [{"transactions": [transaction()] * 3}]
        with mock.patch.object(normalize, "MAX_TRANSACTIONS", 2):
            self.assert_failed_account("limit_exceeded")
        self.client = FakeClient()
        self.connection = replace(self.connection, accounts=(PRIMARY, "unselected"))
        self.client.pages[OTHER] = [{"transactions": [transaction("second-account")]}]
        with mock.patch.object(normalize, "MAX_TRANSACTIONS", 1):
            snapshot = self.fetch()
        self.assertEqual(len(snapshot.accounts), 1)
        self.assertEqual(snapshot.errors, (AccountFailure("unselected", "limit_exceeded"),))
        self.client = FakeClient()
        result = self.fetch()
        self.assertEqual(len(result.accounts), 2)

    def test_second_page_provider_failure_discards_partial_account_data(self):
        failure = BudgetAPIError(429, "rate_limit", True)
        self.client.pages[ACCOUNT] = [{"transactions": [transaction()], "continuation_key": "more"}, failure]
        self.assert_failed_account("rate_limit")

    def test_failed_account_does_not_block_healthy_account_and_diagnostics_stay_safe(self):
        self.connection = replace(self.connection, accounts=(PRIMARY, "unselected"))
        self.client.balances[ACCOUNT] = BudgetAPIError(400, "private-bank-error-body")
        snapshot = self.fetch()
        self.assertEqual(snapshot.errors, (AccountFailure(PRIMARY, "provider_error"),))
        self.assertEqual([account.identification_hash for account in snapshot.accounts], ["unselected"])
        self.assertEqual(self.client.calls[1][0], "/accounts/" + ACCOUNT + "/transactions")
        self.assertNotIn("private-bank", repr(snapshot))

    def test_401_on_balance_or_later_page_invalidates_connection_before_other_accounts(self):
        self.connection = replace(self.connection, accounts=(PRIMARY, "unselected"))
        for location in ("balance", "page"):
            self.client = FakeClient()
            failure = BudgetAPIError(401, "authorization")
            if location == "balance":
                self.client.balances[ACCOUNT] = failure
            else:
                self.client.pages[ACCOUNT] = [{"transactions": [transaction()], "continuation_key": "more"}, failure]
            with self.subTest(location=location), self.assertRaises(BudgetAPIError) as caught:
                self.fetch()
            self.assertIs(caught.exception, failure)
            self.assertFalse(any(OTHER in path for path, _ in self.client.calls))

    def test_global_record_limit_stops_later_accounts_without_more_requests(self):
        self.connection = replace(self.connection, accounts=(PRIMARY, "unselected"))
        self.client.pages[ACCOUNT] = [{"transactions": [transaction(), transaction("second")]}]
        with mock.patch.object(normalize, "MAX_TRANSACTIONS", 1):
            snapshot = self.fetch()
        self.assertEqual(snapshot.accounts, ())
        self.assertEqual(snapshot.errors, (AccountFailure(PRIMARY, "limit_exceeded"),
                                           AccountFailure("unselected", "limit_exceeded")))
        self.assertFalse(any(OTHER in path for path, _ in self.client.calls))

    def test_normalized_byte_limit_includes_balances_and_stops_remaining_accounts(self):
        self.assertEqual(normalize.MAX_NORMALIZED_BYTES, 32 * 1024 * 1024)
        tx_bytes = len(json.dumps(normalize_transaction(transaction()), separators=(",", ":")).encode("utf-8"))
        balance_bytes = len(json.dumps(normalize_balances({"balances": [balance()]})[0], separators=(",", ":")).encode("utf-8"))
        with mock.patch.object(normalize, "MAX_NORMALIZED_BYTES", tx_bytes + balance_bytes):
            self.assertEqual(len(self.fetch().accounts), 1)
        for limit in (tx_bytes - 1, tx_bytes + balance_bytes - 1):
            with self.subTest(limit=limit):
                self.client = FakeClient()
                self.connection = replace(self.connection, accounts=(PRIMARY, "unselected"))
                with mock.patch.object(normalize, "MAX_NORMALIZED_BYTES", limit):
                    result = self.fetch()
                self.assertEqual(result.accounts, ())
                self.assertEqual(result.errors, (AccountFailure(PRIMARY, "limit_exceeded"),
                                                 AccountFailure("unselected", "limit_exceeded")))
                self.assertFalse(any(OTHER in path for path, _ in self.client.calls))
