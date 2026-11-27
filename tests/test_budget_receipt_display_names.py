"""Synthetic coverage for owner-private OCR issuer display and search."""
import json
from unittest import IsolatedAsyncioTestCase, TestCase, mock
from uuid import UUID

from sqlalchemy import event

from hestia import api_contract, db
from hestia.budget import receipt_api, receipt_parser
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


def _receipt_text(name, *, payment="GOTÓWKA"):
    return (f"{name}\nNIP {_NIP}\nPARAGON FISKALNY\n2026-11-21 13:30\n"
            f"TESTOWY TOWAR 1 x 10,00 10,00 A\nSUMA PLN 10,00\n{payment}")


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


def _legacy_confirm_audit(session, receipt_id, at, *, owner="owner", after_json='{"status":"confirmed"}'):
    session.add(db.BudgetAnalyticsAudit(
        owner=owner, subject_type="receipt", subject_id=receipt_id, action="confirm",
        before_json="{}", after_json=after_json, changed_at=at,
    ))


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
            older = _receipt(session, 5, name="Older confirmation", day="2026-11-19", updated=800)
            latest = _receipt(session, 6, name=_CANONICAL, day="2026-10-10", updated=120, source_kind="photo")
            foreign = _receipt(session, 7, name="Foreign confirmation", day="2026-11-19", updated=200, owner="other")
            _receipt(session, 8, name="Unconfirmed OCR", day="2026-11-19", updated=200, status="needs_review")
            _receipt(session, 9, name="   ", day="2026-11-19", updated=200)
            _receipt(session, 10, name="Gateway", day="2026-11-19", updated=200,
                     document_kind="payment_confirmation", source_kind="email")
            _shop(session, 11, _NIP, "Registered name loses to confirmation", updated=300)
            _shop(session, 12, _REGISTERED_NIP, "Older registry name")
            _shop(session, 13, _REGISTERED_NIP, "Latest Registry Name", updated=120)
            _shop(session, 14, _REGISTERED_NIP, "Foreign registry name", owner="other", updated=200)
            _legacy_confirm_audit(session, older, 110)
            _legacy_confirm_audit(session, latest, 120)
            _legacy_confirm_audit(session, foreign, 200, owner="other")
            _legacy_confirm_audit(session, older, 300, owner="other")
            _legacy_confirm_audit(session, older, 400, after_json="{")

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
            source = _receipt(session, 4, name=_CANONICAL, day="2026-11-21", source_kind="photo")
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

    def test_confirmed_name_source_requires_an_ocr_receipt(self):
        registered_name = "Registered seller"
        with self.sessions.begin() as session:
            target = _receipt(session, 1, status="needs_review")
            for identifier, fields in enumerate((
                {"document_kind": "invoice"}, {"document_kind": "payment_confirmation"},
                {"source_kind": "live_text"}, {"source_kind": "email"}, {"source_kind": "attachment"},
            ), start=2):
                forbidden = _receipt(session, identifier, name="Unrelated buyer", **fields)
                _legacy_confirm_audit(session, forbidden, 1000)
            _shop(session, 10, _NIP, registered_name)
        page = self.receipts.list("owner", {"status": "needs_review"}, include_display_shop_name=True)
        self.assertEqual(page["receipts"][0]["display_shop_name"], registered_name)
        found = self.receipts.search("owner", {"query": "Unrelated buyer"}, include_display_shop_name=True)
        self.assertNotIn(target, {row["receipt_id"] for row in found["receipts"]})

    def test_confirmed_ocr_receipt_does_not_supply_a_name_for_its_buyer_nip(self):
        buyer_page = _receipt_text(_CANONICAL).replace(f"NIP {_NIP}", f"NABYWCA:\nKlient Testowy\nNIP {_NIP}")
        draft = self.receipts.parse_text("owner", buyer_page, source_kind="local_ocr")
        confirmed = self.receipts.confirm("owner", receipt_fixture.confirm_values(draft))["receipt"]
        self.assertEqual(confirmed["status"], "confirmed")

        target = self.receipts.parse_text("owner", _receipt_text(_RAW), source_kind="local_ocr")
        detail = self.receipts.detail("owner", target["receipt_id"], include_display_shop_name=True)["receipt"]
        self.assertNotIn("display_shop_name", detail)
        self.assertEqual(detail["raw_nip"], _NIP)
        self.assertIsNone(confirmed["raw_nip"])

    def test_group_writes_and_link_only_confirm_keep_latest_shop_name(self):
        single_ref = "synthetic-seller-name-single"
        for reference, amount in ((single_ref, "10"), ("synthetic-seller-name-group", "20")):
            self.add_transaction("a", reference, amount=amount, booking_date="2026-11-21",
                                 remittance_information=[f"NIP {_NIP}"])
        b = self.receipts.parse_text("owner", _receipt_text(_RAW, payment="KARTA PŁATNICZA"), source_kind="local_ocr")
        b_link = {"account_id": self.accounts["a"], "entry_reference": single_ref}
        b_saved = self.receipts.confirm("owner", receipt_fixture.confirm_values(b, link=b_link))["receipt"]
        self.assertTrue(b_saved["linked"])
        peer = self.receipts.parse_text("owner", _receipt_text(_RAW, payment="KARTA PŁATNICZA"), source_kind="local_ocr")
        peer_saved = self.receipts.confirm("owner", receipt_fixture.confirm_values(peer))["receipt"]
        a = self.receipts.parse_text("owner", _receipt_text("Wstępna nazwa testowa"), source_kind="local_ocr")
        a_saved = self.receipts.confirm("owner", receipt_fixture.confirm_values(a))["receipt"]
        target = self.receipts.parse_text("owner", _receipt_text(_RAW), source_kind="local_ocr")["receipt_id"]

        self.now += 1
        self.receipts.confirm("owner", receipt_fixture.confirm_values(
            a, expected_version=a_saved["version"], raw_shop_name=_CANONICAL,
        ))

        def assert_display():
            row = self.receipts.detail("owner", target, include_display_shop_name=True)["receipt"]
            self.assertEqual(row["raw_shop_name"], _RAW)
            self.assertEqual(row["display_shop_name"], _CANONICAL)

        assert_display()
        self.now += 1
        # An older client includes the original name even when only the link changes.
        b_saved = self.receipts.confirm(
            "owner", receipt_fixture.confirm_values(b, expected_version=b_saved["version"]),
        )["receipt"]
        self.assertFalse(b_saved["linked"])
        assert_display()

        self.now += 1
        candidates = self.receipts.group_candidates("owner", {"receipt_id": b["receipt_id"]})
        transaction = next(tx for tx in candidates["transactions"] if tx["amount"] == "20")
        group = self.receipts.group_link("owner", {
            "link": {key: transaction[key] for key in ("account_id", "entry_reference", "candidate_token")},
            "members": [{"receipt_id": row["receipt_id"], "expected_version": row["version"]}
                        for row in (b_saved, peer_saved)],
        })["group"]
        assert_display()

        self.now += 1
        self.receipts.group_unlink("owner", {"group_id": group["group_id"], "expected_version": group["group_version"]})
        assert_display()

        audits = sorted(
            (audit for audit in self.rows(db.BudgetAnalyticsAudit)
             if audit.subject_id == b["receipt_id"] and audit.action == "confirm"),
            key=lambda audit: audit.id,
        )
        self.assertEqual([json.loads(audit.after_json)["shop_name_confirmed"] for audit in audits], [True, False])
        for audit in audits:
            self.assertNotIn("raw_shop_name", json.loads(audit.after_json))
            self.assertNotIn("raw_nip", json.loads(audit.after_json))
            self.assertNotIn(_RAW, audit.after_json)
            self.assertNotIn(_NIP, audit.after_json)

    def test_equal_confirmation_timestamps_use_descending_audit_id(self):
        with self.sessions.begin() as session:
            target = _receipt(session, 1, status="needs_review")
            higher_uuid = _receipt(session, 3, name="Older candidate")
            lower_uuid = _receipt(session, 2, name="Newer candidate")
        for receipt_id, name in ((higher_uuid, "First correction"), (lower_uuid, "Second correction")):
            parsed = {"receipt_id": receipt_id, "version": 1, "draft": receipt_parser.parse_receipt(_receipt_text(_RAW))}
            self.receipts.confirm("owner", receipt_fixture.confirm_values(parsed, raw_shop_name=name))
        detail = self.receipts.detail("owner", target, include_display_shop_name=True)["receipt"]
        self.assertEqual(detail["display_shop_name"], "Second correction")

    def test_resumed_search_ranks_only_nips_present_after_its_cursor(self):
        with self.sessions.begin() as session:
            _receipt(session, 1, day="2026-11-24", nip=_UNKNOWN_NIP, name="Search prefix only")
            _receipt(session, 2, day="2026-11-23", name="Search prefix issuer")
            target = _receipt(session, 3, day="2026-11-22", status="needs_review")
        request = {"query": "search prefix", "limit": 1}
        page = self.receipts.search("owner", request, include_display_shop_name=True)
        self.assertIsNotNone(page["next_cursor"])
        page = self.receipts.search("owner", {**request, "cursor": page["next_cursor"]}, include_display_shop_name=True)
        self.assertIsNotNone(page["next_cursor"])
        queries = []

        def record_query(_conn, _cursor, statement, _params, *_args):
            if statement.startswith("WITH receipt_search_nips AS"):
                queries.append(statement)

        event.listen(self.engine, "before_cursor_execute", record_query)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", record_query)
        page = self.receipts.search("owner", {**request, "cursor": page["next_cursor"]}, include_display_shop_name=True)
        self.assertEqual([(row["receipt_id"], row["display_shop_name"]) for row in page["receipts"]],
                         [(target, "Search prefix issuer")])
        (statement,) = queries
        candidate_query = statement.partition("\n SELECT ")[0]
        self.assertIn("budget_receipts.receipt_date < ?", candidate_query)
        self.assertEqual(statement.count("FROM receipt_search_nips"), 2)


def _response_result(key, value):
    return {"receipts": [value, {}]} if key == "receipts" else {key: value}


class ReceiptDisplayNameResponseTests(TestCase):
    def test_response_strips_an_accidentally_populated_new_field_for_older_clients(self):
        for operation, key in (("detail", "receipt"), ("confirm", "receipt"), ("parse-text", "draft"),
                               ("list", "receipts"), ("search", "receipts")):
            for version, exposed in (((0, 60, 0), False), ((0, 61, 0), True)):
                with self.subTest(operation=operation, version=version):
                    receipt = {"raw_shop_name": _RAW, "display_shop_name": _CANONICAL}
                    response = receipt_api._response(operation, _response_result(key, receipt), version)
                    expected = {"raw_shop_name": _RAW}
                    if exposed:
                        expected["display_shop_name"] = _CANONICAL
                    self.assertEqual(json.loads(response.body), _response_result(key, expected))


class ReceiptDisplayNameHTTPTests(IsolatedAsyncioTestCase):
    asyncSetUp = receipt_fixture.ReceiptHTTPTests.asyncSetUp
    rows = receipt_fixture.ReceiptHTTPTests.rows
    token = receipt_fixture.ReceiptHTTPTests.token
    headers = receipt_fixture.ReceiptHTTPTests.headers
    post = receipt_fixture.ReceiptHTTPTests.post
    get = receipt_fixture.ReceiptHTTPTests.get

    async def test_negotiated_list_search_and_detail_preserve_raw_name(self):
        source = await self.post("parse-text", {
            "text": _receipt_text("Wstępna nazwa testowa"), "source_kind": "local_ocr",
        })
        self.assertEqual(source.status, 200, await source.text())
        saved = await self.post("confirm", receipt_fixture.confirm_values(
            await source.json(), raw_shop_name=_CANONICAL,
        ))
        self.assertEqual(saved.status, 200, await saved.text())
        draft = await self.post("parse-text", {"text": _receipt_text(_RAW), "source_kind": "local_ocr"})
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
