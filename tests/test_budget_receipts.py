"""Synthetic receipts v1a parser, matching, retention, privacy, drift and HTTP tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from unittest import mock

from hestia import api_contract, db, web
from hestia.budget import receipt_api
from hestia.budget.analytics_views import BudgetAnalyticsViews
from hestia.budget.import_file import parse_import
from hestia.budget.normalize import AccountSnapshot, Snapshot
from hestia.budget.receipt_parser import (CONFIRMED_TEXT_TTL_SECONDS, DRAFT_TTL_SECONDS, LINE_FIELDS, ReceiptError,
                                         parse_receipt, redact_pan, validate_confirm)
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_api as fixture
from tests.test_budget_analytics_store import AnalyticsCase

PAN = "5351 0055 5555 5555"
MASKED_PAN = "5351********5555"
PRIVATE_SHOP = "BIEDRONKA"
PRIVATE_ITEM = "MLEKO WYPASIONE"


def receipt_text(*, total="100", day="2026-11-30", kind="purchase", payment="KARTA PŁATNICZA", item=PRIVATE_ITEM):
    marker = "\nZWROT" if kind == "refund" else ""
    return (f"{PRIVATE_SHOP}\nNIP 7791011327\nPARAGON FISKALNY{marker}\n{day} 12:34\n"
            f"{item} 1 x {total.replace('.', ',')} {total.replace('.', ',')} A\n"
            f"SUMA PLN {total.replace('.', ',')}\n{payment}\nNR KARTY {MASKED_PAN}\nAID {PAN}")


def confirm_values(parsed, *, link=None, **changes):
    draft = parsed["draft"]
    values = {key: draft[key] for key in (
        "raw_shop_name", "raw_nip", "receipt_kind", "receipt_date", "receipt_time", "total_amount",
        "currency", "payment_method",
    )}
    values.update({
        "receipt_id": parsed["receipt_id"], "expected_version": parsed["version"], "link": link,
        "lines": [{key: line[key] for key in LINE_FIELDS} for line in draft["lines"]],
    })
    values.update(changes)
    return values


class ReceiptParserTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only")

    def draft(self, **kwargs):
        return self.receipts.parse_text("owner", receipt_text(**kwargs))

    def link(self, account="a", reference="debit"):
        return {"account_id": self.accounts[account], "entry_reference": reference}

    def confirmed(self, *, account="a", reference="debit", **kwargs):
        parsed = self.draft(**kwargs)
        result = self.receipts.confirm("owner", confirm_values(parsed, link=self.link(account, reference)))
        return parsed, result["receipt"]

    def test_polish_parser_redacts_pan_and_reconciles_disjoint_lines_exactly(self):
        text = """JERONIMO MARTINS POLSKA S.A.
NIP 7791011327
PARAGON FISKALNY
20-11-2026 12:34
ML.UHT 3,2% 1L MLEKOVITA 2 * 4,49 = 8,98 A
Rabat -2,00
ZIEMNIAKI JAD.LUZ 1,420 kg x 3,99 5,67 B
KAUCJA 0,50
KUPON CAŁY KOSZYK -1,00
SUMA PLN 12,15
KARTA PŁATNICZA
NR KARTY ****5555
"""
        parsed = parse_receipt(text)
        self.assertEqual((parsed["receipt_date"], parsed["receipt_time"], parsed["total_amount"]),
                         ("2026-11-20", "12:34", "12.15"))
        self.assertEqual(parsed["math_delta"], "0")
        self.assertEqual([line["line_kind"] for line in parsed["lines"]],
                         ["item", "item", "deposit", "basket_discount"])
        milk, potatoes, deposit, coupon = parsed["lines"]
        self.assertEqual((milk["gross_line_total"], milk["discount_amount"], milk["net_line_total"]),
                         ("8.98", "2", "6.98"))
        self.assertEqual((potatoes["quantity"], potatoes["raw_unit"], potatoes["unit_price_raw"]),
                         ("1.42", "kg", "3.99"))
        self.assertEqual((deposit["net_line_total"], coupon["net_line_total"]), ("0.5", "-1"))
        self.assertEqual(parsed["raw_nip"], "7791011327")
        self.assertEqual(parsed["chain_key"], "biedronka")
        redacted = redact_pan(text + PAN + "\n" + MASKED_PAN)
        for secret in (PAN, MASKED_PAN, "****5555"):
            self.assertNotIn(secret, redacted)
        self.assertIn("7791011327", redacted)
        for invalid in ("", "x" * 32769, "line\n" * 501):
            with self.subTest(invalid_size=len(invalid)), self.assertRaises(ReceiptError):
                parse_receipt(invalid)

    def test_redact_pan_accepts_unicode_separators(self):
        groups = ("5351", "0055", "5555", "5555")
        digits = "".join(groups)
        for sep in ("\u00a0", "\u2007", "\u202f", "\u2009", "\u200a", "\u2000", "\u3000",
                    "\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", " ", "-", "\t"):
            sample = sep.join(groups)
            self.assertEqual(redact_pan(sample), "[REDACTED]", sep.encode("unicode_escape"))
            self.assertNotIn(digits, redact_pan("prefix " + sample + " suffix"))
        nip = "779\u00a0101\u00a013\u00a027"
        self.assertEqual(redact_pan("NIP " + nip), "NIP " + nip)
        adjacent = "NIP 7791011327\n2026-11-20 12:34\n"
        self.assertEqual(redact_pan(adjacent), adjacent)
        nbsp_pan = "\u00a0".join(groups)
        text = ("BIEDRONKA\nNIP 7791011327\n2026-11-20 12:34\nMLEKO " + nbsp_pan
                + "\n1 x 5,00 5,00 A\nSUMA PLN 5,00\n")
        parsed = parse_receipt(text)
        self.assertIn("[REDACTED]", parsed["raw_ocr_text"])
        self.assertNotIn(digits, parsed["raw_ocr_text"].replace("\u00a0", ""))
        self.assertNotIn(nbsp_pan, parsed["raw_ocr_text"])
        self.assertIn("7791011327", parsed["raw_ocr_text"])
        for line in parsed["lines"]:
            self.assertNotIn(digits, line["raw_name"].replace("\u00a0", ""))
            self.assertNotIn(nbsp_pan, line["raw_name"])

    def test_confirm_redacts_pan_in_shop_name_and_line_name(self):
        parsed = self.draft()
        nbsp_pan = "5351\u00a00055\u00a05555\u00a05555"
        dash_pan = "5351\u20130055\u20135555\u20135555"
        unit_pan = "411111111111"
        values = confirm_values(parsed, raw_shop_name=f"BIEDRONKA {nbsp_pan}")
        values["lines"][0]["raw_name"] = f"MLEKO {dash_pan}"
        values["lines"][0]["raw_unit"] = unit_pan
        saved = self.receipts.confirm("owner", values)["receipt"]
        row = self.receipt_row(parsed["receipt_id"])
        line = next(item for item in self.rows(db.BudgetReceiptLine) if item.receipt_id == row.id)
        for secret in (nbsp_pan, dash_pan, "5351005555555555", unit_pan):
            self.assertNotIn(secret, saved["raw_shop_name"])
            self.assertNotIn(secret, saved["lines"][0]["raw_name"])
            self.assertNotIn(secret, saved["lines"][0]["raw_name_key"])
            self.assertNotIn(secret, saved["lines"][0]["raw_unit"])
            self.assertNotIn(secret, row.raw_shop_name)
            self.assertNotIn(secret, line.raw_name)
            self.assertNotIn(secret, line.raw_name_key)
            self.assertNotIn(secret, line.raw_unit)
        self.assertEqual(row.raw_shop_name, "BIEDRONKA [REDACTED]")
        self.assertEqual((line.raw_name, line.raw_name_key, line.raw_unit),
                         ("MLEKO [REDACTED]", "mleko [redacted]", "[REDACTED]"))
        self.assertEqual(saved["raw_shop_name"], row.raw_shop_name)
        self.assertEqual(saved["lines"][0]["raw_name"], line.raw_name)

    def test_receipt_match_friday_purchase_booked_monday(self):
        self.add_transaction("a", "monday", amount="100", booking_date="2026-11-23", transaction_date=None)
        parsed = self.draft(day="2026-11-20")
        monday = next(item for item in parsed["candidates"] if item["entry_reference"] == "monday")
        self.assertEqual(monday["date_distance"], 3)
        self.add_transaction("a", "friday-evidence", amount="100", booking_date="2026-11-23",
                             transaction_date="2026-11-20")
        parsed = self.draft(day="2026-11-20")
        closest = next(item for item in parsed["candidates"] if item["entry_reference"] == "friday-evidence")
        self.assertEqual(closest["date_distance"], 0)
        confirmed = self.receipts.confirm("owner", confirm_values(parsed, link=self.link(reference="monday")))
        self.assertTrue(confirmed["receipt"]["linked"])

    def test_receipt_refund_matches_only_crdt(self):
        parsed = self.draft(kind="refund")
        self.assertTrue(parsed["candidates"])
        self.assertEqual({item["credit_debit_indicator"] for item in parsed["candidates"]}, {"CRDT"})
        self.assertNotIn("debit", {item["entry_reference"] for item in parsed["candidates"]})
        self.assertEqual(parsed["draft"]["total_amount"], "100")
        with self.assertRaises(ReceiptError) as invalid:
            self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))
        self.assertEqual(invalid.exception.status, 400)
        result = self.receipts.confirm("owner", confirm_values(parsed, link=self.link("b", "credit")))
        self.assertEqual(result["receipt"]["linked_entry_ref"], "credit")

    def test_receipt_match_excludes_transfer_legs(self):
        self.decide()
        parsed = self.draft()
        self.assertNotIn("debit", {item["entry_reference"] for item in parsed["candidates"]})
        with self.assertRaises(ReceiptError) as invalid:
            self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))
        self.assertEqual(invalid.exception.status, 400)
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).status, "draft")

    def receipt_row(self, identifier):
        with self.sessions() as session:
            return session.get(db.BudgetReceipt, identifier)

    def test_receipt_concurrent_confirm_returns_409(self):
        first, second = self.draft(), self.draft()
        result = self.receipts.confirm("owner", confirm_values(first, link=self.link()))
        self.assertEqual(result["receipt"]["status"], "confirmed")
        with self.assertRaises(ReceiptError) as duplicate:
            self.receipts.confirm("owner", confirm_values(second, link=self.link()))
        self.assertEqual((duplicate.exception.code, duplicate.exception.status),
                         ("budget_receipt_tx_already_linked", 409))
        with self.assertRaises(ReceiptError) as stale:
            self.receipts.confirm("owner", confirm_values(first, expected_version=99))
        self.assertEqual((stale.exception.code, stale.exception.status), ("budget_receipt_conflict", 409))
        with mock.patch.object(BudgetReceiptStore, "_verify_link", return_value="fingerprint"):
            with self.assertRaises(ReceiptError) as raced:
                self.receipts.confirm("owner", confirm_values(second, link=self.link()))
        self.assertEqual((raced.exception.code, raced.exception.status),
                         ("budget_receipt_tx_already_linked", 409))
        raced_row = self.receipt_row(second["receipt_id"])
        self.assertEqual((raced_row.status, raced_row.linked_account_id, raced_row.linked_entry_ref,
                          raced_row.version), ("draft", None, None, 1))
        self.assertEqual(self.receipt_row(first["receipt_id"]).linked_entry_ref, "debit")

    def assert_latched(self, identifier, previous_version):
        row = self.receipt_row(identifier)
        self.assertEqual((row.status, row.version), ("needs_review", previous_version + 1))
        self.receipts.parse_text("owner", receipt_text(total="7", day="2026-11-30"))
        self.assertEqual(self.receipt_row(identifier).version, previous_version + 1)

    def offline(self, transactions, *, fetched_at):
        raw = []
        for value in transactions:
            value = dict(value)
            money = {key: value.pop(key) for key in ("amount", "currency")}
            raw.append({**value, "transaction_amount": money})
        item = self.config.connections[0]
        document = {"version": 1, "connection_id": item.id,
                    "aspsp": {"name": item.bank_name, "country": item.country},
                    "identification_hash": "a", "fetched_at": datetime.fromtimestamp(fetched_at, timezone.utc).isoformat(),
                    "complete": True, "transactions": {"transactions": raw}}
        return parse_import(document, self.config, now=fetched_at + 1000)

    def commit(self, transactions):
        observations = (AccountSnapshot("a", (), tuple(transactions)),
                        *(AccountSnapshot(account, None, None) for account in ("b", "c", "d")))
        self.store.commit_snapshot("bank", Snapshot(self.now + 10000, observations,
                                                   "2026-01-01", "2026-12-31"))

    def make_offline(self, reference):
        from tests.test_budget_analytics_core import payload
        self.add_transaction("a", reference, amount="100", booking_date="2026-11-30")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (self.accounts["a"], reference))
            row.source, row.api_observed_at = "offline_import", None
        return payload(entry_reference=reference, amount="100", booking_date="2026-11-30")

    def test_receipt_fingerprint_change_and_transfer_latch_needs_review(self):
        from tests.test_budget_analytics_core import payload

        parsed, confirmed = self.confirmed(reference="debit")
        changed = payload(entry_reference="debit", amount="101", booking_date="2026-11-30")
        self.commit((changed,))
        self.assert_latched(parsed["receipt_id"], confirmed["version"])

        self.add_transaction("a", "commit-uncertain", amount="100", booking_date="2026-11-30")
        parsed, confirmed = self.confirmed(reference="commit-uncertain")
        conflicting = (payload(entry_reference="commit-uncertain", amount="100", booking_date="2026-11-30"),
                       payload(entry_reference="commit-uncertain", amount="102", booking_date="2026-11-30"))
        self.commit(conflicting)
        self.assert_latched(parsed["receipt_id"], confirmed["version"])

        original = self.make_offline("offline-update")
        parsed, confirmed = self.confirmed(reference="offline-update")
        changed = payload(entry_reference="offline-update", amount="103", booking_date="2026-11-30")
        self.store.import_snapshot(self.config, self.offline((changed,), fetched_at=self.now + 100))
        self.assert_latched(parsed["receipt_id"], confirmed["version"])
        self.assertIsNotNone(original)

        self.make_offline("offline-conflict")
        parsed, confirmed = self.confirmed(reference="offline-conflict")
        conflicting = (payload(entry_reference="offline-conflict", amount="100", booking_date="2026-11-30"),
                       payload(entry_reference="offline-conflict", amount="104", booking_date="2026-11-30"))
        self.store.import_snapshot(self.config, self.offline(conflicting, fetched_at=self.now + 200))
        self.assert_latched(parsed["receipt_id"], confirmed["version"])

        self.make_offline("offline-duplicate")
        parsed, confirmed = self.confirmed(reference="offline-duplicate")
        batch = self.offline((payload(entry_reference="unrelated", amount="8", booking_date="2026-11-30"),),
                             fetched_at=self.now + 300)
        self.store.import_snapshot(self.config, batch)
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "offline-duplicate")).uncertain = True
        self.store.import_snapshot(self.config, batch)
        self.assert_latched(parsed["receipt_id"], confirmed["version"])

        parsed, confirmed = self.confirmed(account="d", reference="debit-2")
        self.decide(debit=("d", "debit-2"), credit=("b", "credit"))
        self.assert_latched(parsed["receipt_id"], confirmed["version"])

    def test_receipt_retention_purge_48h_draft_and_30d_confirmed(self):
        confirmed_draft = self.draft()
        confirmed = self.receipts.confirm("owner", confirm_values(confirmed_draft))["receipt"]
        self.assertIsNotNone(confirmed["raw_ocr_text"])
        self.assertEqual(confirmed["raw_text_expires_at"], self.now + CONFIRMED_TEXT_TTL_SECONDS)
        expired = self.draft(total="7")
        self.now += DRAFT_TTL_SECONDS + 1
        purged = self.receipts.purge_expired("owner")
        self.assertEqual(purged["drafts_purged"], 1)
        self.assertIsNone(self.receipt_row(expired["receipt_id"]))
        self.assertIsNotNone(self.receipt_row(confirmed_draft["receipt_id"]).raw_ocr_text)
        self.now = confirmed["raw_text_expires_at"]
        purged = self.receipts.purge_expired("owner")
        self.assertEqual(purged["raw_texts_purged"], 1)
        row = self.receipt_row(confirmed_draft["receipt_id"])
        self.assertEqual(row.status, "confirmed")
        self.assertIsNone(row.raw_ocr_text)
        self.assertIsNone(row.raw_text_expires_at)
        self.assertIsNone(row.image_relpath)
        self.assertIsNone(row.image_expires_at)
        detail = self.receipts.detail("owner", row.id)["receipt"]
        self.assertEqual(len(detail["lines"]), 1)

    def test_receipt_confirm_leaves_overview_totals_unchanged(self):
        views = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only")
        before = views.overview("owner", "2026-11")
        parsed, confirmed = self.confirmed()
        self.receipts.delete("owner", {"receipt_id": confirmed["receipt_id"], "expected_version": confirmed["version"]})
        self.assertIsNone(self.receipt_row(parsed["receipt_id"]))
        self.assertEqual(views.overview("owner", "2026-11"), before)

    def test_receipt_logs_contain_no_nip_pan_or_amounts(self):
        parsed = self.receipts.parse_text("owner", receipt_text() + "\n" + PAN)
        secret_values = (PRIVATE_SHOP, PRIVATE_ITEM, "7791011327", PAN, MASKED_PAN, "100")
        self.assertNotIn(PAN, parsed["draft"]["raw_ocr_text"])
        self.assertNotIn(MASKED_PAN, parsed["draft"]["raw_ocr_text"])
        with mock.patch("hestia.budget.api.log.error") as log:
            self.receipts.confirm("owner", confirm_values(parsed))
        log.assert_not_called()
        receipt_audits = [row for row in self.rows(db.BudgetAnalyticsAudit) if row.subject_type == "receipt"]
        serialized = json.dumps([(row.before_json, row.after_json) for row in receipt_audits])
        for secret in secret_values:
            self.assertNotIn(secret, serialized)
        for row in receipt_audits:
            for raw in (row.before_json, row.after_json):
                if raw != "null":
                    self.assertEqual(set(json.loads(raw)),
                                     {"receipt_id", "action", "status", "version", "linked", "source_kind"})
        self.assertEqual(self.rows(db.Audit), [])

    def test_receipt_confirm_rejects_mismatched_total_and_wrong_line_net(self):
        parsed = self.draft()
        valid = confirm_values(parsed)
        cases = []
        cases.append(confirm_values(parsed, total_amount="99"))
        wrong_net = confirm_values(parsed)
        wrong_net["lines"][0]["net_line_total"] = "99"
        cases.append(wrong_net)
        deposit = confirm_values(parsed)
        deposit["lines"][0].update(line_kind="deposit", discount_amount="1", net_line_total="99")
        cases.append(deposit)
        basket = confirm_values(parsed)
        basket["lines"][0].update(line_kind="basket_discount", discount_amount="0", net_line_total="100")
        cases.append(basket)
        zero = confirm_values(parsed, total_amount="0")
        zero["lines"][0].update(gross_line_total="0", net_line_total="0", unit_price_raw="0")
        cases.append(zero)
        for values in cases:
            with self.subTest(values=(values["total_amount"], values["lines"][0]["line_kind"])):
                with self.assertRaises(ReceiptError) as invalid:
                    validate_confirm(values)
                self.assertEqual((invalid.exception.code, invalid.exception.status),
                                 ("budget_receipt_total_mismatch", 400))
        self.assertEqual(validate_confirm(valid)["total_amount"], "100")

    def test_parse_text_resolves_candidates_outside_the_write_transaction(self):
        from contextlib import contextmanager
        in_write = False
        real_tx = BudgetReceiptStore.transaction
        real_candidates = BudgetReceiptStore._candidates
        seen = {"called": False}

        @contextmanager
        def tracking(store):
            nonlocal in_write
            with real_tx(store) as session:
                in_write = True
                try:
                    yield session
                finally:
                    in_write = False

        def spy(store, session, owner, row, parsed):
            seen["called"] = True
            self.assertFalse(in_write)
            return real_candidates(store, session, owner, row, parsed)

        with mock.patch.object(BudgetReceiptStore, "transaction", tracking), \
                mock.patch.object(BudgetReceiptStore, "_candidates", spy):
            parsed = self.draft()
        self.assertTrue(seen["called"])
        self.assertTrue(parsed["candidates"])

    def test_receipt_list_and_search_page_mixed_null_dates(self):
        from sqlalchemy import select
        dated = [self.draft(day=day, item="MLEKO WSPOLNY")
                 for day in ("2026-11-30", "2026-11-20", "2026-11-01")]
        undated_text = "SKLEP WSPOLNY\nMLEKO WSPOLNY 1 x 5,00 5,00 A\nSUMA PLN 5,00\nGOTOWKA\n"
        undated = [self.receipts.parse_text("owner", undated_text) for _ in range(4)]
        dates = {item["receipt_id"]: item["draft"]["receipt_date"] for item in dated + undated}
        self.assertEqual(sum(value is None for value in dates.values()), 4)
        with self.sessions() as session:
            expected = [row.id for row in session.scalars(select(db.BudgetReceipt).where(
                db.BudgetReceipt.owner == "owner").order_by(
                    db.BudgetReceipt.receipt_date.desc(), db.BudgetReceipt.id))]
        self.assertEqual(len(expected), 7)
        self.assertIsNotNone(dates[expected[2]])
        self.assertIsNone(dates[expected[3]])

        def collect(search):
            seen, cursor, guard = [], None, 0
            while True:
                body = {"limit": 2, "query": "wspolny"} if search else {"limit": 2}
                if cursor is not None:
                    body["cursor"] = cursor
                page = (self.receipts.search if search else self.receipts.list)("owner", body)
                identifiers = [item["receipt_id"] for item in page["receipts"]]
                self.assertLessEqual(len(identifiers), 2)
                seen.extend(identifiers)
                cursor = page["next_cursor"]
                guard += 1
                self.assertLess(guard, 8)
                if cursor is None:
                    break
                self.assertEqual(len(identifiers), 2)
            return seen

        listed = collect(False)
        self.assertEqual(listed, expected)
        self.assertEqual(len(listed), len(set(listed)))
        seen_null = False
        for identifier in listed:
            if dates[identifier] is None:
                seen_null = True
            else:
                self.assertFalse(seen_null)
        self.assertEqual(collect(True), expected)

    def test_receipt_match_allows_excluded_category_purchase(self):
        self.annotation(kind="excluded")
        parsed = self.draft()
        self.assertIn("debit", {item["entry_reference"] for item in parsed["candidates"]})
        result = self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))
        self.assertEqual(result["receipt"]["status"], "confirmed")


class ReceiptHTTPTests(fixture.unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers
    assert_private_headers = fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {receipt_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
                               "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0"})
        patch.start()
        self.addCleanup(patch.stop)
        await fixture.BudgetAPITests.asyncSetUp(self)

    async def post(self, operation, body, *, user="alice", headers=None):
        headers = self.headers(user, origin=True) if headers is None else headers
        return await self.client.post(receipt_api.PREFIX + "/" + operation, data=json.dumps(body),
                                     headers={**headers, "Content-Type": "application/json"})

    async def get(self, operation, receipt_id, *, user="alice", headers=None):
        return await self.client.get(receipt_api.PREFIX + "/" + operation, params={"receipt_id": receipt_id},
                                     headers=self.headers(user) if headers is None else headers)

    async def test_receipt_owner_isolation_foreign_id(self):
        response = await self.post("parse-text", {"text": receipt_text()})
        self.assertEqual(response.status, 200, await response.text())
        parsed = await response.json()
        identifier = parsed["receipt_id"]
        for operation in ("detail", "image"):
            foreign = await self.get(operation, identifier, user="bob")
            self.assertEqual(foreign.status, 404)
        for operation, body in (("confirm", confirm_values(parsed)),
                                ("delete", {"receipt_id": identifier, "expected_version": 1})):
            foreign = await self.post(operation, body, user="bob")
            self.assertEqual(foreign.status, 404)
        self.assertEqual((await (await self.post("list", {}, user="bob")).json())["receipts"], [])
        self.assertEqual((await (await self.post("search", {"query": "mleko"}, user="bob")).json())["receipts"], [])
        for user in ("viewer", "operator"):
            self.assertEqual((await self.get("detail", identifier, user=user)).status, 403)
            self.assertEqual((await self.post("list", {}, user=user)).status, 403)
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            self.assertEqual((await self.get("detail", identifier)).status, 401)
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertEqual((await self.get("detail", identifier)).status, 409)
        with mock.patch.dict(os.environ, {receipt_api.FLAG: "0"}):
            self.assertEqual((await self.get("detail", identifier)).status, 404)
        self.assertEqual((await self.get("image", identifier)).status, 404)
        self.assertEqual((await self.client.head(receipt_api.PREFIX + "/detail", headers=self.headers())).status, 405)

    async def test_receipt_http_review_confirm_search_and_strict_body_limits(self):
        response = await self.post("parse-text", {"text": receipt_text()})
        self.assertEqual(response.status, 200, await response.text())
        parsed = await response.json()
        api_contract.BudgetReceiptParseResult.model_validate(parsed)
        self.assert_private_headers(response)
        detail = await self.get("detail", parsed["receipt_id"])
        self.assertEqual(detail.status, 200)
        api_contract.BudgetReceiptDetailResult.model_validate(await detail.json())
        listed = await self.post("list", {"month": "2026-11", "linked": False})
        self.assertEqual(listed.status, 200)
        api_contract.BudgetReceiptListResult.model_validate(await listed.json())
        found = await self.post("search", {"query": "wypasione"})
        self.assertEqual(len((await found.json())["receipts"]), 1)
        invalid = confirm_values(parsed, total_amount="99")
        mismatch = await self.post("confirm", invalid)
        self.assertEqual(mismatch.status, 400)
        self.assertEqual((await mismatch.json())["error"], "budget_receipt_total_mismatch")
        confirmed = await self.post("confirm", confirm_values(parsed))
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        saved = (await confirmed.json())["receipt"]
        api_contract.BudgetReceiptConfirmResult.model_validate({"ok": True, "receipt": saved})
        self.assertIsNotNone(saved["raw_ocr_text"])
        deleted = await self.post("delete", {"receipt_id": saved["receipt_id"],
                                              "expected_version": saved["version"]})
        self.assertEqual(deleted.status, 200)
        self.assertEqual((await self.get("detail", saved["receipt_id"])).status, 404)
        for operation, size in (("parse-text", 49152), ("confirm", 131072)):
            oversized = await self.client.post(receipt_api.PREFIX + "/" + operation, data=b" " * (size + 1),
                                               headers={**self.headers(origin=True), "Content-Type": "application/json"})
            self.assertEqual(oversized.status, 413)
            self.assertIn(str(size), (await oversized.json())["error"])
        # 128 KiB is above the 69632 application cap and still reaches confirm's parser.
        at_confirm_cap = await self.client.post(
            receipt_api.PREFIX + "/confirm", data=b" " * 131072,
            headers={**self.headers(origin=True), "Content-Type": "application/json"})
        self.assertEqual(at_confirm_cap.status, 400)
        self.assertEqual((await at_confirm_cap.json())["error"], "invalid JSON")
        missing_origin = await self.post("parse-text", {"text": "private"}, headers=self.headers())
        self.assertEqual(missing_origin.status, 403)
        bearer = await self.post("parse-text", {"text": "private"},
                                 headers=self.headers(bearer=True, origin=True))
        self.assertEqual(bearer.status, 403)


if __name__ == "__main__":
    import unittest
    unittest.main()
