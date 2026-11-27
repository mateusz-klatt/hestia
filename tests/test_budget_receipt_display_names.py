"""Synthetic coverage for owner-private OCR issuer display and search."""
from unittest import IsolatedAsyncioTestCase, mock
from uuid import UUID

from sqlalchemy import event

from hestia import api_contract, db
from hestia.budget import receipt_parser
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_receipts as receipt_fixture
from tests.test_budget_analytics_store import AnalyticsCase


_HEADER = "X-Hestia-Contract-Version"
_OLD = "0.60.0"
_NEW = "0.61.0"
_NIP = "1010101016"
_REGISTERED_NIP = "1020101012"
_KNOWN_NIP = "1030101019"
_FOREIGN_NIP = "1040101015"
_UNKNOWN_NIP = "1050101011"
_INVALID_NIP = "1060101017"
_RAW = "TL Centrun Medyczne Testowe Sp. 2 O.O."
_CANONICAL = "Centrum Medyczne Łąka Testowa"


def _receipt(session, identifier, *, name=_RAW, nip=_NIP, day="2026-11-21", updated=100, **fields):
    row = db.BudgetReceipt(**({
        "id": str(UUID(int=identifier)), "owner": "owner", "raw_shop_name": name, "raw_nip": nip,
        "receipt_date": day, "total_amount": "10.00", "currency": "PLN", "payment_method": "cash",
        "status": "confirmed", "document_kind": "receipt", "source_kind": "local_ocr",
        "created_at": 100, "updated_at": updated, "version": 1,
    } | fields))
    session.add(row)
    return row.id


def _shop(session, identifier, nip, name, *, owner="owner", updated=100):
    session.add(db.BudgetShop(id=str(UUID(int=identifier)), owner=owner, name=name, nip=nip,
                              version=1, created_at=100, updated_at=updated))


class ReceiptDisplayNameStoreTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")

    def test_list_batches_distinct_page_nips_and_prefers_last_owner_confirmation(self):
        with self.sessions.begin() as session:
            ocr = _receipt(session, 1, day="2026-11-23", status="needs_review")
            photo = _receipt(session, 2, day="2026-11-22", source_kind="photo", status="needs_review")
            cloud = _receipt(session, 3, nip=_REGISTERED_NIP, source_kind="cloud_ai", status="needs_review")
            lookahead = _receipt(session, 4, nip=_UNKNOWN_NIP, day="2026-11-20", status="needs_review")
            _receipt(session, 5, name="Older confirmation", day="2026-11-19", updated=110, source_kind="live_text")
            _receipt(session, 6, name=_CANONICAL, day="2026-10-10", updated=120, source_kind="live_text")
            _receipt(session, 7, name="Foreign confirmation", day="2026-11-19", updated=200, owner="other")
            _receipt(session, 8, name="Unconfirmed OCR", day="2026-11-19", updated=200, status="needs_review")
            _receipt(session, 9, name="   ", day="2026-11-19", updated=200)
            _receipt(session, 10, name="Gateway", day="2026-11-19", updated=200,
                     document_kind="payment_confirmation", source_kind="email")
            _shop(session, 11, _NIP, "Registered name loses to confirmation", updated=300)
            _shop(session, 12, _REGISTERED_NIP, "Older registry name")
            _shop(session, 13, _REGISTERED_NIP, "Latest Registry Name", updated=120)
            _shop(session, 14, _REGISTERED_NIP, "Foreign registry name", owner="other", updated=200)

        lookups = []

        def record_lookup(_conn, _cursor, statement, parameters, *_args):
            if "row_number() OVER" in statement:
                lookups.append(parameters)

        event.listen(self.engine, "before_cursor_execute", record_lookup)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record_lookup)
        args = {"limit": 3}
        previous = self.receipts.list("owner", args)
        self.assertEqual(lookups, [])
        page = self.receipts.list("owner", args, include_display_shop_name=True)
        self.assertEqual(len(lookups), 1)
        self.assertEqual(lookups[0].count(_NIP), 2)  # Once per source in the same query.
        self.assertEqual(lookups[0].count(_REGISTERED_NIP), 2)
        self.assertNotIn(_UNKNOWN_NIP, lookups[0])
        self.assertEqual(lookups[0].count("owner"), 2)
        self.assertEqual([item["receipt_id"] for item in page["receipts"]], [ocr, photo, cloud])
        self.assertEqual([item["display_shop_name"] for item in page["receipts"]],
                         [_CANONICAL, _CANONICAL, "Latest Registry Name"])
        for old, current in zip(previous["receipts"], page["receipts"]):
            self.assertEqual(current["raw_shop_name"], _RAW)
            self.assertEqual(old, {key: value for key, value in current.items() if key != "display_shop_name"})
        second = self.receipts.list("owner", {**args, "cursor": page["next_cursor"]}, include_display_shop_name=True)
        self.assertEqual(len(lookups), 2)
        self.assertEqual(second["receipts"][0]["receipt_id"], lookahead)
        self.assertNotIn("display_shop_name", second["receipts"][0])

    def test_fallbacks_require_ocr_receipt_and_valid_exact_nip(self):
        with self.sessions.begin() as session:
            known = _receipt(session, 1, nip=_KNOWN_NIP, status="needs_review")
            registered = _receipt(session, 2, nip=_REGISTERED_NIP, status="needs_review")
            redacted = _receipt(session, 3, name="[usunięto]", nip=_KNOWN_NIP)
            secret_digits = _receipt(session, 4, name="12345 67890 12345", nip=_REGISTERED_NIP)
            _shop(session, 5, _REGISTERED_NIP, "Synthetic Registered Seller")
            _shop(session, 6, _KNOWN_NIP, "11111 22222 33333")
            _receipt(session, 7, name="Foreign only", nip=_FOREIGN_NIP, owner="other")
            _shop(session, 8, _FOREIGN_NIP, "Foreign only shop", owner="other")
            foreign = _receipt(session, 9, nip=_FOREIGN_NIP, status="needs_review")
            invalid = _receipt(session, 10, nip=_INVALID_NIP, status="needs_review")
            _receipt(session, 11, name="Checksum Mimic", nip=_INVALID_NIP)
            gmail = _receipt(session, 12, nip=_KNOWN_NIP, source_kind="attachment", status="imported")
            invoice = _receipt(session, 13, nip=_KNOWN_NIP, document_kind="invoice", status="needs_review")
            paste = _receipt(session, 14, nip=_KNOWN_NIP, source_kind="live_text", status="needs_review")
            missing = _receipt(session, 15, nip=None, status="needs_review")
            alias_only = _receipt(session, 16, nip=_UNKNOWN_NIP, name="Lidl OCR", status="needs_review")

        with mock.patch.dict(receipt_parser._CHAIN_NIPS, {_KNOWN_NIP: "lidl"}):
            page = self.receipts.list("owner", {}, include_display_shop_name=True)
            by_id = {row["receipt_id"]: row for row in page["receipts"]}
            self.assertEqual(by_id[known]["display_shop_name"], "Lidl")
            self.assertEqual(by_id[registered]["display_shop_name"], "Synthetic Registered Seller")
            for receipt_id in (foreign, invalid, gmail, invoice, paste, missing, alias_only):
                self.assertNotIn("display_shop_name", by_id[receipt_id])
            self.assertEqual(by_id[alias_only]["raw_shop_name"], "Lidl OCR")

            for receipt_id in (foreign, invalid):
                detail = self.receipts.detail("owner", receipt_id, include_display_shop_name=True)["receipt"]
                self.assertEqual(detail["raw_shop_name"], _RAW)
                self.assertNotIn("display_shop_name", detail)

            for query, expected in (("Lidl", [known, redacted, alias_only]),
                                    ("Synthetic Registered", [registered, secret_digits]),
                                    ("Foreign only", [])):
                results = self.receipts.search("owner", {"query": query}, include_display_shop_name=True)
                self.assertEqual([row["receipt_id"] for row in results["receipts"]], expected)

    def test_search_pages_canonical_matches_before_raw_matches_that_sort_later(self):
        with self.sessions.begin() as session:
            _receipt(session, 1, nip=_UNKNOWN_NIP, name="Unrelated", day="2026-11-24")
            first = _receipt(session, 2, day="2026-11-23", status="needs_review")
            second = _receipt(session, 3, day="2026-11-22", status="needs_review")
            source = _receipt(session, 4, name=_CANONICAL, day="2026-11-21", source_kind="paste")
        request = {"query": "laka testowa", "limit": 1}
        previous = self.receipts.search("owner", request)
        self.assertEqual([row["receipt_id"] for row in previous["receipts"]], [source])
        cursor = None
        for identifier in (first, second, source):
            page = self.receipts.search("owner", {**request, "cursor": cursor}, include_display_shop_name=True)
            self.assertEqual([row["receipt_id"] for row in page["receipts"]], [identifier])
            cursor = page["next_cursor"]
        self.assertIsNone(cursor)
        raw = self.receipts.search("owner", {"query": "centRun"}, include_display_shop_name=True)
        self.assertEqual([row["receipt_id"] for row in raw["receipts"]], [first, second])
        self.assertEqual([row["display_shop_name"] for row in raw["receipts"]], [_CANONICAL, _CANONICAL])


class ReceiptDisplayNameHTTPTests(IsolatedAsyncioTestCase):
    asyncSetUp = receipt_fixture.ReceiptHTTPTests.asyncSetUp
    rows = receipt_fixture.ReceiptHTTPTests.rows
    token = receipt_fixture.ReceiptHTTPTests.token
    headers = receipt_fixture.ReceiptHTTPTests.headers
    post = receipt_fixture.ReceiptHTTPTests.post
    get = receipt_fixture.ReceiptHTTPTests.get

    async def test_negotiated_list_search_and_detail_preserve_raw_name(self):
        def receipt_text(name):
            return (f"{name}\nNIP {_NIP}\nPARAGON FISKALNY\n2026-11-21 13:30\n"
                    "TESTOWY TOWAR 1 x 10,00 10,00 A\nSUMA PLN 10,00\nGOTÓWKA")

        source = await self.post("parse-text", {"text": receipt_text("Wstępna nazwa testowa")})
        self.assertEqual(source.status, 200, await source.text())
        saved = await self.post("confirm", receipt_fixture.confirm_values(
            await source.json(), raw_shop_name=_CANONICAL,
        ))
        self.assertEqual(saved.status, 200, await saved.text())
        draft = await self.post("parse-text", {"text": receipt_text(_RAW), "source_kind": "local_ocr"})
        self.assertEqual(draft.status, 200, await draft.text())
        identifier = (await draft.json())["receipt_id"]

        for operation, body in (("detail", None), ("list", {"status": "draft"}), ("search", {"query": "centrun"})):
            async def read(version):
                headers = self.headers(origin=body is not None) | {_HEADER: version}
                if body is None:
                    response = await self.get(operation, identifier, headers=headers)
                else:
                    response = await self.post(operation, body, headers=headers)
                self.assertEqual(response.status, 200, await response.text())
                return await response.json()

            old = await read(_OLD)
            current = await read(_NEW)
            schema = api_contract.BudgetReceiptDetailResult if body is None else api_contract.BudgetReceiptListResult
            schema.model_validate(current)
            item = current["receipt"] if body is None else current["receipts"][0]
            self.assertEqual(item["receipt_id"], identifier)
            self.assertEqual(item["raw_shop_name"], _RAW)
            self.assertEqual(item.pop("display_shop_name"), _CANONICAL)
            self.assertEqual(old, current)

        for version in (_OLD, _NEW):
            match = await self.post("search", {"query": "laka testowa"},
                                    headers=self.headers(origin=True) | {_HEADER: version})
            self.assertEqual(match.status, 200, await match.text())
            ids = {row["receipt_id"] for row in (await match.json())["receipts"]}
            self.assertEqual(identifier in ids, version == _NEW)
        stored = next(row for row in self.rows(db.BudgetReceipt) if row.id == identifier)
        self.assertEqual(stored.raw_shop_name, _RAW)
        self.assertEqual(stored.version, 1)
