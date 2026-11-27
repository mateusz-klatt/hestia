"""Contract-gated receipt status filters using synthetic ledger rows."""
import time
from unittest import IsolatedAsyncioTestCase
from uuid import UUID

from sqlalchemy import event

from hestia import db
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_receipts as receipt_fixture
from tests.test_budget_analytics_store import AnalyticsCase


_HEADER = "X-Hestia-Contract-Version"
_STATUSES = ["imported", "needs_review", "draft"]


def _add_receipt(session, identifier, status, date, *, owner, now):
    receipt_id = str(UUID(int=identifier))
    session.add(db.BudgetReceipt(
        id=receipt_id, owner=owner, raw_shop_name="Synthetic seller", receipt_date=date,
        total_amount="10.00", currency="PLN", payment_method="cash", status=status, source_kind="live_text",
        created_at=now, updated_at=now, version=1,
    ))
    return receipt_id


class ReceiptStatusStoreTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")

    def test_multiple_statuses_page_in_keyset_order_using_the_existing_index(self):
        with self.sessions.begin() as session:
            identifiers = [
                _add_receipt(session, i, status, day, owner="owner", now=self.now)
                for i, (status, day) in enumerate((
                    ("confirmed", "2026-11-24"),
                    ("imported", "2026-11-23"),
                    ("confirmed", "2026-11-23"),
                    ("needs_review", "2026-11-23"),
                    ("draft", "2026-11-22"),
                    ("confirmed", "2026-11-22"),
                    ("imported", None),
                    ("draft", None),
                ), 1)
            ]
            _add_receipt(session, 9, "imported", "2026-11-23", owner="other", now=self.now)

        queries = []

        def capture(_conn, _cursor, statement, params, *_args):
            if statement.startswith("SELECT") and "FROM budget_receipts" in statement and "LIMIT ?" in statement:
                queries.append((statement, params))

        event.listen(self.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", capture)

        cursor = None
        for indices, statuses in zip(((1, 3), (4, 6), (7,)), (_STATUSES, ["draft", *_STATUSES], _STATUSES)):
            page = self.receipts.list("owner", {"statuses": statuses, "limit": 2, "cursor": cursor})
            self.assertEqual([row["receipt_id"] for row in page["receipts"]], [identifiers[i] for i in indices])
            cursor = page["next_cursor"]
            self.assertEqual(cursor is None, indices == (7,))
            if cursor is not None:
                with self.assertRaises(ReceiptError):
                    self.receipts.list("owner", {"statuses": ["draft"], "limit": 2, "cursor": cursor})

        self.assertEqual(len(queries), 3)
        with self.engine.connect() as conn:
            for statement, params in queries:
                self.assertIn("budget_receipts.status IN (?, ?, ?)", statement)
                plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, params).fetchall()
                details = "\n".join(row[3] for row in plan)
                self.assertIn("ix_budget_receipts_owner_date", details)
                self.assertNotIn("SCAN budget_", details)
                self.assertNotIn("USE TEMP B-TREE", details)

    def test_store_rejects_invalid_status_filters(self):
        for filters in (
            {"statuses": None}, {"statuses": "draft"}, {"statuses": []}, {"statuses": ["other"]},
            {"status": "draft", "statuses": _STATUSES},
        ):
            with self.subTest(filters=filters), self.assertRaises(ReceiptError):
                self.receipts.list("owner", filters)


class ReceiptStatusHTTPTests(IsolatedAsyncioTestCase):
    asyncSetUp = receipt_fixture.ReceiptHTTPTests.asyncSetUp
    rows = receipt_fixture.ReceiptHTTPTests.rows
    token = receipt_fixture.ReceiptHTTPTests.token
    headers = receipt_fixture.ReceiptHTTPTests.headers
    post = receipt_fixture.ReceiptHTTPTests.post

    async def test_negotiated_list_accepts_statuses_and_preserves_legacy_single_status(self):
        now = time.time()
        with self.sessions.begin() as session:
            for identifier, status in enumerate(["confirmed", *_STATUSES], 1):
                _add_receipt(session, identifier, status, "2026-11-23", owner="alice", now=now)

        browser = self.headers(origin=True)
        current = browser | {_HEADER: "0.65.0"}
        legacy = browser | {_HEADER: "0.64.99"}
        for headers, filters, expected in (
            (current, {"statuses": _STATUSES}, _STATUSES),
            (current, {"status": "confirmed"}, ["confirmed"]),
            (legacy, {"status": "confirmed"}, ["confirmed"]),
            (browser, {}, ["confirmed", "draft", "needs_review", "draft"]),
        ):
            with self.subTest(version=headers.get(_HEADER), filters=filters):
                response = await self.post("list", filters, headers=headers)
                self.assertEqual(response.status, 200, await response.text())
                self.assertEqual([item["status"] for item in (await response.json())["receipts"]], expected)

    async def test_unavailable_and_conflicting_fields_return_the_unknown_field_error(self):
        browser = self.headers(origin=True)
        current = browser | {_HEADER: "0.65.0"}
        legacy = browser | {_HEADER: "0.64.99"}
        unknown = await self.post("list", {"unknown_field": []}, headers=legacy)
        self.assertEqual(unknown.status, 400)
        unknown_body = await unknown.json()
        self.assertEqual(unknown_body["error"], "budget_receipt_invalid_request")
        old_versions = ("0.62.0", "0.63.0", "0.64.0", "0.64.99")
        for headers, filters in (
            *((browser | {_HEADER: version}, {"statuses": _STATUSES}) for version in old_versions),
            (browser, {"statuses": _STATUSES}),
            (current, {"status": "draft", "statuses": _STATUSES}),
            (current, {"status": None, "statuses": _STATUSES}),
            (current, {"statuses": []}),
            (current, {"statuses": None}),
            (current, {"statuses": ["bad-status"]}),
        ):
            with self.subTest(version=headers.get(_HEADER), filters=filters):
                response = await self.post("list", filters, headers=headers)
                self.assertEqual(response.status, 400, await response.text())
                self.assertEqual(await response.json(), unknown_body)
