"""Branches the household happy path does not take.

These are fixed denials and redaction edges. They do not add a spouse permission.
"""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.exc import OperationalError

from hestia import db, store_sql
from hestia.budget import household, household_api, household_views, month_budget
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.household import HouseholdAuthError, HouseholdCommandError, entity_alias
from hestia.budget.household_views import AliasBook, HouseholdReader
from hestia.budget.views import BudgetUsersBackendRequired

SECRET = b"edge-test-secret"
GRANT = "ab" * 32
PERIOD = "2026-11"


def _book():
    categories = [
        {"id": "cat-salary", "name": "salary", "archived": False},
        {"id": "cat-other", "name": "Groceries", "archived": True},
    ]
    return AliasBook(SECRET, GRANT, ["acc-1"], categories)


def _tx(reference, *, account="acc-1", kind="expense", category="cat-salary", direction="DBIT", amount="10.00"):
    return {
        "account_id": account,
        "entry_reference": reference,
        "payload": {
            "booking_date": "2026-11-02",
            "currency": "PLN",
            "amount": amount,
            "credit_debit_indicator": direction,
        },
        "effective": {"kind": kind, "category_id": category},
    }


def _leg(account="acc-1", *, selected=True, date="2026-11-02", amount="5.00", currency="PLN", reference="ref"):
    return {
        "selected": selected, "date": date, "amount": amount, "currency": currency,
        "account_id": account, "entry_reference": reference,
    }


class _Query:
    def __init__(self, items):
        from multidict import MultiDict
        self.query = MultiDict(items)


class HouseholdEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.path = Path(cls.directory.name) / "edges.db"
        cls.engine, cls.sessions = db.init_db(cls.path)
        store_sql.cutover_users(cls.engine, {"alice": "unused"})
        with cls.sessions.begin() as session:
            session.add_all([
                db.User(username="viewer", password_hash="unused", role="viewer"),
                db.User(username="carol", password_hash="unused", role="viewer"),
                db.BudgetCategory(id="cat-salary", owner="alice", name="salary", name_key="salary",
                                  archived=False, version=1, created_at=1, updated_at=1),
                db.BudgetCategoryLimit(
                    id="lim-1", owner="alice", category_id="cat-salary", currency="PLN",
                    monthly_limit="100.00", warning_percent=80, enabled=True, version=1,
                    created_at=1, updated_at=1),
            ])

    @classmethod
    def tearDownClass(cls):
        db.reset_engine_cache()
        cls.directory.cleanup()

    def test_alias_and_grant_defensive_branches(self):
        book = _book()
        with self.assertRaises(AnalyticsError):
            book.account("missing")
        self.assertIsNone(book.label(None))
        with self.assertRaises(AnalyticsError):
            book.label("missing")
        with self.assertRaises(AnalyticsError):
            book.resolve_account("missing")
        with self.assertRaises(AnalyticsError):
            book.resolve_category("missing")
        for args in (
            (None, GRANT, "account", "acc-1"),
            (b"", GRANT, "account", "acc-1"),
            (SECRET, None, "account", "acc-1"),
            (SECRET, "", "account", "acc-1"),
            (SECRET, GRANT, None, "acc-1"),
            (SECRET, GRANT, "", "acc-1"),
            (SECRET, GRANT, "account", None),
            (SECRET, GRANT, "account", ""),
        ):
            with self.assertRaises(HouseholdAuthError):
                entity_alias(*args)

        class Empty:
            def get(self, _model, _key):
                return None

        with self.assertRaises(BudgetUsersBackendRequired):
            household.authorize_household(Empty(), "viewer", header=None, require_header=False)
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "0"}, clear=False):
            with self.assertRaises(HouseholdAuthError) as caught:
                household.require_household_data_grant(Empty(), "viewer", header=None, require_header=False)
        self.assertEqual(caught.exception.code, "budget_disabled")
        self.assertIsNone(household._active(Empty(), "", "viewer"))
        self.assertIsNone(household._active(Empty(), None, "viewer"))

        class Broken:
            def execute(self, _statement):
                raise OperationalError("select", {}, Exception("missing"))

        with self.assertRaises(HouseholdCommandError) as caught:
            household._require_lineage(Broken())
        self.assertEqual(caught.exception.code, "household_schema_required")

        class HeadWithoutTable:
            def __init__(self):
                self.calls = 0

            def execute(self, _statement):
                self.calls += 1
                return self

            def scalars(self):
                return self

            def all(self):
                return ["0022"]

            def first(self):
                return None

        with self.assertRaises(HouseholdCommandError) as caught:
            household._require_lineage(HeadWithoutTable())
        self.assertEqual(caught.exception.code, "household_schema_required")

    def test_grant_uses_env_path_and_rejects_a_duplicate_grant_id(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path)}):
            granted = household.grant_member("viewer", "alice")
        self.assertEqual(granted["status"], "granted")
        with mock.patch.object(household.secrets, "token_hex", return_value=granted["grant_id"]):
            with self.assertRaises(HouseholdCommandError) as caught:
                household.grant_member("carol", "alice", path=str(self.path))
        self.assertEqual(caught.exception.code, "household_grant_conflict")
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetHouseholdMember, "carol"))

    def test_cli_main_dispatches_household_without_bank_io(self):
        from hestia.budget.__main__ import main
        with self.sessions.begin() as session:
            session.add(db.User(username="dave", password_hash="unused", role="viewer"))
        stdout = io.StringIO()
        with redirect_stdout(stdout), \
             mock.patch("hestia.budget.__main__.execute", side_effect=AssertionError("execute")), \
             mock.patch("hestia.budget.__main__.load_config", side_effect=AssertionError("config")), \
             mock.patch("hestia.budget.__main__.budget_lock", side_effect=AssertionError("lock")):
            code = main(["--db", str(self.path), "household", "grant", "--member", "dave", "--owner", "alice"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "granted")
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.BudgetHouseholdMember, "dave"))

    def test_query_cursors_transfers_and_redaction(self):
        normalize = household_views._normalize_query
        with self.assertRaises(AnalyticsError):
            normalize("nope", {})
        with self.assertRaises(AnalyticsError):
            normalize("overview", ["period"])
        with self.assertRaises(AnalyticsError):
            normalize("overview", {"nope": "1"})
        with self.assertRaises(AnalyticsError):
            normalize("overview", {})
        with self.assertRaises(AnalyticsError):
            normalize("monthly-budget", {"period": "2026-13"})
        with self.assertRaises(AnalyticsError):
            normalize("monthly-budget", {"currency": "zz"})
        self.assertEqual(normalize("monthly-budget", {})["currency"], "PLN")
        with self.assertRaises(AnalyticsError):
            normalize("spending-limits", {"period": "bad"})
        self.assertEqual(normalize("spending-limits", {"period": PERIOD, "currency": "PLN"})["currency"], "PLN")
        with self.assertRaises(AnalyticsError):
            normalize("transactions", {"period": PERIOD, "limit": "x"})
        with self.assertRaises(AnalyticsError):
            normalize("transactions", {"period": PERIOD, "limit": "0"})
        with self.assertRaises(AnalyticsError):
            normalize("transactions", {"period": PERIOD, "limit": 0})
        with self.assertRaises(AnalyticsError):
            normalize("transactions", {"period": PERIOD, "kind": "nope"})
        with self.assertRaises(AnalyticsError):
            normalize("transactions", {"period": PERIOD, "account_id": ""})
        self.assertEqual(normalize("transactions", {"period": PERIOD, "limit": 2})["limit"], 2)
        with self.assertRaises(AnalyticsError):
            normalize("transfers", {"period": PERIOD, "status": "nope"})
        with self.assertRaises(AnalyticsError):
            normalize("transfers", {"period": PERIOD, "cursor": ""})

        with self.assertRaises(AnalyticsError):
            household_api._query(_Query([]), "nope")
        with self.assertRaises(AnalyticsError):
            household_api._query(_Query([("period", "")]), "overview")
        with mock.patch("hestia.budget.household_api.init_db", return_value=(object(), object())), \
             mock.patch("hestia.budget.household_api.store_sql.is_users_db_authoritative", return_value=False):
            with self.assertRaises(BudgetUsersBackendRequired):
                household_api._reader(SECRET)

        with self.assertRaises(AnalyticsError):
            HouseholdReader(object(), cursor_secret=b"")
        reader = HouseholdReader(object(), cursor_secret=SECRET)
        now = time.time()
        context = household_views._cursor_context("viewer", "alice", GRANT, "transfers", {"period": PERIOD})
        with mock.patch.object(household_views, "_MAX_CURSOR", 1):
            with self.assertRaises(AnalyticsError) as caught:
                reader._cursor_encode(context, "digest", 0, now)
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(AnalyticsError):
            reader._page([], reader._cursor_encode(context, "digest", 5, now), context, "digest", 50)
        for cursor in (1, "", "!!!!", self._sealed(reader, ["nope"], context), self._sealed(reader, {
            "context": context, "digest": "digest", "index": 0, "issued": now, "expires": now + 10,
        }, context), self._sealed(reader, {
            "digest": "digest", "index": -1, "issued": now, "expires": now + 10,
        }, context), self._sealed(reader, {
            "digest": "digest", "index": "0", "issued": now, "expires": now + 10,
        }, context), self._sealed(reader, {
            "digest": "digest", "index": 0, "issued": "now", "expires": now + 10,
        }, context)):
            with self.assertRaises(AnalyticsError):
                reader._cursor_index(cursor, context, "digest", now)

        book = _book()
        account_alias = book.alias("account", "acc-1")
        category_alias = book.alias("category", "cat-salary")
        values = normalize("transactions", {
            "period": PERIOD, "account_id": account_alias, "kind": "expense", "category_id": category_alias,
        })
        snapshot = {"transactions": [
            _tx("other-account", account="acc-2"),
            _tx("other-kind", kind="income"),
            _tx("other-category", category="cat-other"),
            _tx("kept"),
        ]}
        page = reader._transactions(snapshot, "viewer", "alice", values, book, GRANT)
        self.assertEqual([row["id"] for row in page["transactions"]], [
            book.alias("transaction", "acc-1\0kept"),
        ])
        with self.assertRaises(AnalyticsError):
            household_views._safe_transaction(_tx("bad", kind="nope"), book)
        with self.assertRaises(AnalyticsError):
            household_views._safe_transaction(_tx("bad-direction", direction="XXXX"), book)

        transfer_values = normalize("transfers", {"period": PERIOD, "status": "suggested"})
        transfers = {"_transfers": [
            {"status": "accepted", "debit": _leg(), "credit": _leg()},
            {"status": "suggested", "debit": _leg(selected=False), "credit": _leg()},
            {"status": "suggested", "debit": _leg(account="acc-2"), "credit": _leg()},
            {"status": "suggested", "debit": _leg(), "credit": _leg()},
        ]}
        body = reader._transfers(transfers, "viewer", "alice", transfer_values, book, GRANT)
        self.assertEqual(body["filtered_total"], 1)
        self.assertIsNone(household_views._safe_leg(_leg(selected=False), book))
        self.assertIsNone(household_views._safe_leg(_leg(account="missing"), book))

        rows = [
            _tx("expense"),
            _tx("income-same", kind="income", amount="3.00"),
            _tx("income-only", kind="income", category="cat-other", amount="4.00"),
            _tx("income-none", kind="income", category=None, amount="5.00"),
        ]
        categories = household_views._category_rows(rows, book)
        labels = {row["label"] for row in categories}
        self.assertIn("salary", labels)
        self.assertTrue(any(row["category_id"] is None and row["income"] != "0" for row in categories))
        self.assertNotIn("Groceries", json.dumps(categories))

        evidence = (SimpleNamespace(date="2026-11-01"),)
        hints = (
            SimpleNamespace(currency="PLN", cadence="monthly", amount="1.00", first_date="2026-01-01",
                            last_date="2026-11-01", occurrence_count=2, evidence=evidence),
            SimpleNamespace(currency="EUR", cadence="monthly", amount="2.00", first_date="2026-01-01",
                            last_date="2026-11-01", occurrence_count=1, evidence=evidence),
        )
        with mock.patch.object(household_views, "_snapshot_recurring", return_value=hints):
            recurring = household_views._recurring({}, {"currency": "PLN"}, GRANT)
        self.assertEqual([hint["currency"] for hint in recurring["hints"]], ["PLN"])

        self.assertEqual(household_views._month_span("1900-01"), ["1900-01"])
        self.assertEqual(household_views._month_span("1899-12"), [])

    def test_rules_limits_mortgages_inbox_and_month_edges(self):
        book = _book()

        class Rows:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, _statement):
                return self.rows

        with self.assertRaises(AnalyticsError):
            household_views._rules(Rows([
                ("rule-1", "iban", "contains", "cat-salary", "expense", 1, True),
            ]), "alice", book, GRANT)
        coerced = household_views._rules(Rows([
            ("rule-1", "description", "contains", "cat-salary", "expense", 1, 0),
        ]), "alice", book, GRANT)
        self.assertFalse(coerced["rules"][0]["enabled"])
        with self.assertRaises(AnalyticsError):
            household_views._rules(Rows([
                ("rule-1", "description", "contains", "cat-salary", "expense", "x", True),
            ]), "alice", book, GRANT)

        with self.sessions() as session:
            limited = household_views._limits(
                session, {"transactions": [], "categories": [
                    {"id": "cat-salary", "name": "salary", "archived": False}]},
                "alice", {"period": PERIOD, "currency": "PLN"}, book, GRANT)
            self.assertEqual(limited["limits"][0]["currency"], "PLN")
            self.assertNotIn("salary", json.dumps(limited["limits"][0]["category_id"]))
            with self.assertRaises(AnalyticsError):
                household_views._limits(
                    session, {"transactions": [], "categories": []},
                    "alice", {"period": PERIOD, "currency": None}, book, GRANT)

        reader = HouseholdReader(self.sessions, cursor_secret=SECRET)
        schedule = {
            "id": "sched-1", "currency": "PLN", "amount": "10.00", "enabled": 1, "due_day": 15,
            "start_date": "2026-01-01", "end_date": None, "paid_months": ["2026-10"],
        }
        good = SimpleNamespace(schedule_id="sched-1", due_date="2026-11-15", status="due_soon")
        with mock.patch.object(reader.views, "_mortgage_reminders_in_session",
                                return_value=([schedule], [], [good])):
            mortgages = reader._mortgages(None, "alice", book, GRANT)
        self.assertEqual(mortgages["schedules"][0]["ordinal"], 1)
        self.assertEqual(len(mortgages["reminders"]), 1)
        orphan = SimpleNamespace(schedule_id="missing", due_date="2026-11-15", status="due_soon")
        with mock.patch.object(reader.views, "_mortgage_reminders_in_session",
                                return_value=([schedule], [], [orphan])):
            with self.assertRaises(AnalyticsError):
                reader._mortgages(None, "alice", book, GRANT)

        mortgage_item = {
            "kind": "mortgage_due", "subject_id": "subject-m", "milestone": "2026-11:due_soon", "read": False,
            "params": {"status": "due_soon", "due_date": "2026-11-15"},
            "ref": {"schedule_id": "sched-1"},
        }
        spending_item = {
            "kind": "spending_limit", "subject_id": "subject-s", "milestone": "2026-11:exceeded", "read": True,
            "params": {"status": "exceeded", "currency": "PLN", "period": PERIOD},
            "ref": {"category_id": "cat-salary"},
        }
        other_item = {"kind": "consent_renewal", "subject_id": "subject-c", "milestone": "x", "read": False,
                      "params": {}, "ref": {}}
        self._patch_inbox(reader, [other_item, mortgage_item, spending_item])
        inbox = reader._inbox(None, "alice", book, GRANT)
        self.assertEqual(inbox["unread_count"], 1)
        self.assertEqual({item["kind"] for item in inbox["items"]}, {"mortgage_due", "spending_limit"})
        text = json.dumps(inbox)
        self.assertNotIn("sched-1", text)
        self.assertNotIn("cat-salary", text)
        with mock.patch.object(household_views.inbox_mod, "build_inbox_items",
                                side_effect=household_views.inbox_mod.OwnerInboxDataError()):
            self._patch_inbox(reader, [])
            with self.assertRaises(AnalyticsError):
                reader._inbox(None, "alice", book, GRANT)

        snapshot = {"accounts": [], "transactions": [], "_transfers": [], "categories": [],
                    "rules": [], "identities": [], "_scan_limited": False}
        with self.sessions() as session:
            with self.assertRaises(AnalyticsError):
                reader._evaluate(session, snapshot, "alice", "2999-01", "PLN", include_series=False)
            for error, status in (
                (month_budget.MonthInputError(), 400),
                (month_budget.MonthLimitError(), 413),
                (month_budget.MonthDataError(), 500),
            ):
                with mock.patch.object(household_views.month_budget_mod, "evaluate_monthly_budget",
                                        side_effect=error):
                    with self.assertRaises(AnalyticsError) as caught:
                        reader._evaluate(session, snapshot, "alice", PERIOD, "PLN", include_series=True)
                self.assertEqual(caught.exception.status, status)

        def budget(*, include_series):
            body = {key: None for key in household_views._BUDGET_KEYS}
            body.update({
                "period": PERIOD,
                "current_month": PERIOD,
                "coverage": {"has_activity": True, "provisional": False, "pending_row_keys": ["acc-1/secret"]},
                "summary": {"start_budget": "0.00", "spent": "10.00"},
                "expected_income": None,
                "pass_through": None,
                "category_spend": [
                    {"category_id": "cat-salary", "archived": False, "expenses": "4.00", "refunds": "1.00",
                     "net_spending": "3.00", "limit": "100.00", "one_off": False},
                    {"category_id": "cat-salary", "archived": True, "expenses": "6.00", "refunds": "0.50",
                     "net_spending": "5.50", "limit": None, "one_off": True, "name": "Remont lazienki"},
                    {"category_id": "cat-other", "archived": False, "expenses": "1.00", "refunds": "0.00",
                     "net_spending": "1.00", "limit": None, "one_off": True, "name": "Prywatna etykieta"},
                    {"category_id": "cat-other", "archived": True, "expenses": "2.00", "refunds": "1.00",
                     "net_spending": "1.00", "limit": "9.00", "one_off": False},
                ],
            })
            if include_series:
                body["_series_daily"] = [{"date": "2026-11-01", "cumulative_spent": "10.00"}]
            return body

        reader._evaluate = lambda session, snapshot, owner, period, currency, *, include_series: budget(
            include_series=include_series)
        monthly = reader._monthly(None, None, "alice", {"period": PERIOD, "currency": "PLN"}, book, GRANT)
        encoded = json.dumps(monthly)
        self.assertNotIn("Remont lazienki", encoded)
        self.assertNotIn("Prywatna etykieta", encoded)
        self.assertNotIn("Groceries", encoded)
        self.assertNotIn("acc-1/secret", encoded)
        self.assertNotIn("one_off", encoded)
        self.assertEqual(monthly["budget"]["coverage"]["pending_row_keys"], [])
        spend = {row["name"]: row for row in monthly["budget"]["category_spend"]}
        self.assertEqual(set(spend), {"salary", "1"})
        self.assertEqual(spend["salary"]["expenses"], "10")
        self.assertEqual(spend["salary"]["refunds"], "1.5")
        self.assertEqual(spend["salary"]["net_spending"], "8.5")
        self.assertEqual(spend["salary"]["limit"], "100.00")
        self.assertIs(spend["salary"]["archived"], False)
        self.assertEqual(spend["1"]["expenses"], "3")
        self.assertEqual(spend["1"]["refunds"], "1")
        self.assertEqual(spend["1"]["net_spending"], "2")
        self.assertEqual(spend["1"]["limit"], "9.00")
        self.assertIs(spend["1"]["archived"], True)
        selected = next(item for item in monthly["series"]["months"] if item["period"] == PERIOD)
        self.assertEqual(len(selected["categories"]), 2)
        self.assertEqual(sorted(row["net_spending"] for row in selected["categories"]), ["2", "8.5"])
        self.assertEqual([row["name"] for row in monthly["series"]["category_labels"]], ["1", "salary"])
        self.assertIs(monthly["series"]["category_labels"][0]["archived"], True)
        self.assertIs(monthly["series"]["category_labels"][1]["archived"], False)
        reader._evaluate = lambda *args, **kwargs: {"current_month": PERIOD, "period": PERIOD, "category_spend": []}
        with self.assertRaises(AnalyticsError):
            reader._monthly(None, None, "alice", {"period": PERIOD, "currency": "PLN"}, book, GRANT)

        reader._book = lambda *args: book
        reader.views._projection = lambda *args: {}
        with self.assertRaises(AnalyticsError):
            reader._build(None, "viewer", "alice", GRANT, "nope", {})

    def _patch_inbox(self, reader, items):
        reader.views._inbox_read_state = lambda *args: (None, None, None, [], None)
        reader.views._evaluate_active_inbox = lambda *args, **kwargs: []
        if items:
            mock.patch.object(household_views.inbox_mod, "build_inbox_items", return_value=items).start()
            self.addCleanup(mock.patch.stopall)

    def test_cursor_length_does_not_depend_on_the_owner_name(self):
        reader = HouseholdReader(object(), cursor_secret=SECRET)
        now = 1_700_000_000
        filters = {"period": PERIOD, "currency": None, "kind": None,
                   "account_id": None, "category_id": None, "limit": 1}
        short = household_views._cursor_context("viewer", "a", GRANT, "transactions", filters)
        long = household_views._cursor_context("viewer", "ą" * 80, GRANT, "transactions", filters)
        nonce = b"0123456789ab"
        with mock.patch.object(household_views.os, "urandom", return_value=nonce):
            first = reader._cursor_encode(short, "ab" * 32, 3, now)
            second = reader._cursor_encode(long, "ab" * 32, 3, now)
        self.assertEqual(len(first), len(second))
        self.assertNotEqual(first, second)
        self.assertEqual(reader._cursor_index(first, short, "ab" * 32, now), 3)
        with self.assertRaises(AnalyticsError):
            reader._cursor_index(first, long, "ab" * 32, now)
        with self.assertRaises(AnalyticsError) as caught:
            reader._cursor_index(first, short, "cd" * 32, now)
        self.assertEqual(caught.exception.status, 409)

    def _sealed(self, reader, payload, context):
        nonce = b"0123456789ab"
        data = json.dumps(payload).encode()
        token = nonce + reader._cipher.encrypt(nonce, data, household_views._cursor_aad(context))
        return base64.urlsafe_b64encode(token).rstrip(b"=").decode("ascii")
