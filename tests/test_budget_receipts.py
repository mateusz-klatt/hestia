"""Synthetic receipts v1a parser, matching, retention, privacy, drift and HTTP tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
import random
import unicodedata
from unittest import mock

from sqlalchemy import String

from hestia import api_contract, auth, db, web
from hestia.budget import receipt_api
from hestia.budget.analytics_views import BudgetAnalyticsViews
from hestia.budget.import_file import parse_import
from hestia.budget.normalize import AccountSnapshot, Snapshot
from hestia.budget.receipt_parser import (CONFIRMED_TEXT_TTL_SECONDS, DRAFT_TTL_SECONDS, LINE_FIELDS, ReceiptError,
                                         parse_receipt, redact_digit_field, redact_pan, validate_confirm)
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


_TEXT_FIELDS = frozenset({"raw_shop_name", "raw_name", "raw_name_key", "raw_unit"})


def _nd_count(value):
    """Digit count for one field. It does not share the line scanner's separator set."""
    normalized = unicodedata.normalize("NFKC", value)
    return sum(unicodedata.category(char) == "Nd" for char in normalized)


def _assert_digit_cap(test, value, key=None):
    if isinstance(value, dict):
        for item_key, item in value.items():
            _assert_digit_cap(test, item, item_key)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_digit_cap(test, item, key)
        return
    if isinstance(value, str) and key in _TEXT_FIELDS and _nd_count(value) >= 12:
        test.assertEqual(value, "[usunięto]")


def _scan_text_columns(test, row):
    for column in row.__table__.columns:
        if isinstance(column.type, String) and column.key in _TEXT_FIELDS:
            _assert_digit_cap(test, getattr(row, column.key), column.key)


def reference_digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def confirm_values(parsed, *, link=None, **changes):
    draft = parsed["draft"]
    values = {key: draft[key] for key in (
        "raw_shop_name", "raw_nip", "receipt_kind", "receipt_date", "receipt_time", "total_amount",
        "currency", "payment_method",
    )}
    if link is not None and "candidate_token" not in link:
        presented = link.get("entry_reference")
        digested = reference_digest(presented) if isinstance(presented, str) else None
        match = next((item for item in parsed.get("candidates", [])
                      if item["account_id"] == link.get("account_id")
                      and item["entry_reference"] in {presented, digested}
                      and item.get("candidate_token")), None)
        if match is not None:
            link = {**link, "candidate_token": match["candidate_token"],
                    "entry_reference": match["entry_reference"]}
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
        self.assertNotIn("raw_ocr_text", parsed)
        redacted = redact_pan(text + PAN + "\n" + MASKED_PAN)
        for secret in (PAN, MASKED_PAN, "****5555", "5351005555555555"):
            self.assertNotIn(secret, redacted)
        self.assertIn("[usunięto]", redacted)
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
            self.assertEqual(redact_pan(sample), "[usunięto]", sep.encode("unicode_escape"))
            self.assertNotIn(digits, redact_pan("prefix " + sample + " suffix"))
        nip = "779\u00a0101\u00a013\u00a027"
        self.assertEqual(redact_pan("NIP " + nip), "NIP 779 101 13 27")
        adjacent = "NIP 7791011327\n2026-11-20 12:34\n"
        self.assertEqual(redact_pan(adjacent), adjacent)
        parsed_adjacent = parse_receipt(adjacent)
        self.assertEqual(parsed_adjacent["receipt_date"], "2026-11-20")
        self.assertEqual(parsed_adjacent["receipt_time"], "12:34")
        self.assertEqual(parsed_adjacent["raw_nip"], "7791011327")
        nbsp_pan = "\u00a0".join(groups)
        text = ("BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-20 12:34\nMLEKO " + nbsp_pan
                + "\n1 x 5,00 5,00 A\nSUMA PLN 5,00\n")
        parsed = parse_receipt(text)
        self.assertNotIn("raw_ocr_text", parsed)
        stored = json.dumps(parsed)
        self.assertNotIn(digits, stored)
        self.assertNotIn(nbsp_pan, stored)
        self.assertIn("7791011327", stored)
        self.assertEqual(parsed["raw_shop_name"], "BIEDRONKA")
        for line in parsed["lines"]:
            self.assertNotIn(digits, line["raw_name"].replace("\u00a0", ""))
            self.assertNotIn(nbsp_pan, line["raw_name"])

    def test_normalised_text_redacts_pans_hidden_by_unicode(self):
        expected = "[usunięto]\n[usunięto]"
        for sep in ("\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            raw = f"4111 1111{sep}1111 1111"
            redacted = redact_pan(raw)
            self.assertEqual(redacted, expected, sep.encode("unicode_escape"))
            parsed = parse_receipt(raw)
            self.assertNotIn("raw_ocr_text", parsed)
            self.assertNotRegex(redacted, r"[0-9]")
            self.assertNotRegex(json.dumps(parsed), r"4111")
        zwsp = "\u200b".join("4111111111111111")
        mixed = "4111\u200b1111\u200c1111\u200d\ufeff\u2060\u00ad1111"
        fullwidth = "４１１１ １１１１ １１１１ １１１１"
        nbsp = "4111\u00a01111\u00a01111\u00a01111"
        for raw in (zwsp, mixed, fullwidth, nbsp):
            redacted = redact_pan(raw)
            self.assertEqual(redacted, "[usunięto]", raw.encode("unicode_escape"))
            parsed = parse_receipt(raw)
            self.assertNotIn("raw_ocr_text", parsed)
            self.assertNotRegex(redacted, r"[0-9]")
            self.assertNotIn("4111111111111111", json.dumps(parsed))

    def test_allowlist_drops_marks_maps_indic_digits_and_keeps_polish(self):
        import unicodedata
        digits = "4111111111111111"
        marks = "\u034f\u0308\u093e\u20dd"
        raw = "".join(digit + marks[index % len(marks)] for index, digit in enumerate(digits[:-1])) + digits[-1]
        self.assertIn("\u034f", raw)
        redacted = redact_pan(raw)
        self.assertEqual(redacted, "[usunięto]")
        self.assertNotIn("raw_ocr_text", parse_receipt(raw))
        self.assertNotIn("\u034f", redacted)
        self.assertNotRegex(redacted, r"[0-9\u0300-\u036f]")
        arabic = "".join(chr(0x0660 + int(ch)) for ch in digits)
        self.assertEqual(redact_pan(arabic), "[usunięto]")
        self.assertNotIn("raw_ocr_text", parse_receipt(arabic))
        spaced = " ".join(
            "".join(chr(0x0660 + int(ch)) for ch in group)
            for group in ("4111", "1111", "1111", "1111"))
        self.assertEqual(redact_pan(spaced), "[usunięto]")
        self.assertNotIn("raw_ocr_text", parse_receipt(spaced))
        for name in ("ŻÓŁTY SER", "ŚLIWKI", "MĄKA"):
            nfc = unicodedata.normalize("NFC", name)
            self.assertEqual(redact_pan(name).encode("utf-8"), nfc.encode("utf-8"))
            self.assertEqual(redact_pan(unicodedata.normalize("NFD", name)).encode("utf-8"), nfc.encode("utf-8"))
        receipt = (
            "BIEDRONKA\nNIP 7791011327\n2026-11-30 12:34\n"
            "ŻÓŁTY SER 1 x 3,49 3,49 A\nŚLIWKI 1 x 4,99 4,99 A\nMĄKA 1 x 2,49 2,49 A\n"
            "SUMA PLN 10,97\n"
        )
        parsed = parse_receipt(receipt)
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["ŻÓŁTY SER", "ŚLIWKI", "MĄKA"])
        self.assertEqual(parsed["math_delta"], "0")
        self.assertNotIn("raw_ocr_text", parsed)

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
            self.assertNotIn(secret, saved["lines"][0]["raw_unit"] or "")
            self.assertNotIn(secret, row.raw_shop_name)
            self.assertNotIn(secret, line.raw_name)
            self.assertNotIn(secret, line.raw_name_key)
            self.assertNotIn(secret, line.raw_unit or "")
        self.assertEqual(row.raw_shop_name, "[usunięto]")
        self.assertEqual((line.raw_name, line.raw_name_key, line.raw_unit),
                         ("[usunięto]", "[usunieto]", "[usunięto]"))
        self.assertIsNone(row.raw_ocr_text)
        self.assertNotIn("raw_ocr_text", saved)
        self.assertEqual(saved["raw_shop_name"], row.raw_shop_name)
        self.assertEqual(saved["lines"][0]["raw_name"], line.raw_name)

    def test_confirm_rejects_a_line_when_name_and_unit_share_twelve_digits(self):
        parsed = self.draft()
        values = confirm_values(parsed)
        values["lines"][0]["raw_name"] = "MLEKO 12345678"
        values["lines"][0]["raw_unit"] = "9012"
        with self.assertRaises(ReceiptError) as rejected:
            self.receipts.confirm("owner", values)
        self.assertEqual((rejected.exception.code, rejected.exception.status),
                         ("budget_receipt_invalid_request", 400))
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).status, "draft")

    def test_receipt_match_friday_purchase_booked_monday(self):
        self.add_transaction("a", "monday", amount="100", booking_date="2026-11-23", transaction_date=None)
        parsed = self.draft(day="2026-11-20")
        monday = next(item for item in parsed["candidates"] if item["entry_reference"] == reference_digest("monday"))
        self.assertEqual(monday["date_distance"], 3)
        self.add_transaction("a", "friday-evidence", amount="100", booking_date="2026-11-23",
                             transaction_date="2026-11-20")
        parsed = self.draft(day="2026-11-20")
        closest = next(item for item in parsed["candidates"] if item["entry_reference"] == reference_digest("friday-evidence"))
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
        self.assertEqual(result["receipt"]["linked_entry_ref"], reference_digest("credit"))
        self.assertEqual(self.receipt_row(result["receipt"]["receipt_id"]).linked_entry_ref, "credit")

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
        self.assertNotIn("raw_ocr_text", confirmed)
        self.assertIsNone(confirmed["raw_text_expires_at"])
        self.assertEqual(confirmed["image_expires_at"], self.now + CONFIRMED_TEXT_TTL_SECONDS)
        self.assertIsNone(self.receipt_row(confirmed_draft["receipt_id"]).raw_ocr_text)
        with self.sessions.begin() as session:
            legacy = session.get(db.BudgetReceipt, confirmed_draft["receipt_id"])
            legacy.raw_ocr_text = "legacy paste"
            legacy.raw_text_expires_at = confirmed["image_expires_at"]
            legacy.image_relpath = "legacy.webp"
        expired = self.draft(total="7")
        self.now += DRAFT_TTL_SECONDS + 1
        purged = self.receipts.purge_expired("owner")
        self.assertEqual(purged["drafts_purged"], 1)
        self.assertIsNone(self.receipt_row(expired["receipt_id"]))
        self.assertEqual(self.receipt_row(confirmed_draft["receipt_id"]).raw_ocr_text, "legacy paste")
        self.now = confirmed["image_expires_at"]
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
        self.assertNotIn("raw_ocr_text", parsed["draft"])
        body = json.dumps(parsed)
        self.assertNotIn(PAN, body)
        self.assertNotIn(MASKED_PAN, body)
        self.assertNotIn("5351005555555555", body)
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
        self.assertIn(reference_digest("debit"), {item["entry_reference"] for item in parsed["candidates"]})
        result = self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))
        self.assertEqual(result["receipt"]["status"], "confirmed")

    def test_redactor_covers_repeated_separators_masks_unicode_and_continuations(self):
        doubled = "5351  0055  5555  5555"
        masked = "5351  ****  ****  5555"
        fullwidth = "５３５１００５５５５５５５５５５"
        self.assertEqual(redact_pan(doubled), "[usunięto]")
        self.assertEqual(redact_pan(masked), masked)
        self.assertEqual(redact_pan(fullwidth), "[usunięto]")
        self.assertNotIn("5351005555555555", redact_pan("pay " + doubled + " end"))
        self.assertIn("5555", redact_pan(masked))
        continued = "NR KARTY\n5351  0055\n5555  5555\nSUMA PLN 5,00\nNIP 7791011327\n2026-11-20\n"
        redacted = redact_pan(continued)
        self.assertNotIn("5351", redacted)
        self.assertNotIn("0055", redacted)
        self.assertIn("SUMA PLN 5,00", redacted)
        self.assertIn("7791011327", redacted)
        self.assertIn("2026-11-20", redacted)
        self.assertEqual(redact_pan("NIP 7791011327\n2026-11-20\n"), "NIP 7791011327\n2026-11-20\n")
        self.assertEqual(redact_pan("4111********1111"), "4111********1111")
        self.assertEqual(redact_pan("4111XXXXXXXX1111"), "4111XXXXXXXX1111")
        self.assertEqual(redact_pan("411111******1111"), "411111******1111")
        self.assertEqual(redact_pan("****1234"), "****1234")
        self.assertEqual(redact_pan("1234****"), "1234****")
        self.assertEqual(redact_pan("Ref 12345 4111 1111 1111 1111"), "[usunięto]")
        self.assertEqual(redact_pan("4111 1111 1111 1111 Ref 98765"), "[usunięto]")
        self.assertEqual(redact_pan("123456 4111-1111-1111-1111"), "[usunięto]")
        self.assertEqual(redact_pan("4111********1111 998877"), "4111********1111 998877")
        self.assertEqual(redact_pan("4111111111111111 20261120"), "[usunięto]")
        self.assertEqual(redact_pan("12,99"), "12,99")
        self.assertEqual(redact_pan("2 x 3,50"), "2 x 3,50")
        self.assertEqual(redact_pan("5351  0055  5555  5555 2026"), "[usunięto]")
        split = "NR\nKARTY ****1234\n5351 0055\n5555 5555\nSUMA PLN 5,00\nNIP 7791011327\n"
        split_redacted = redact_pan(split)
        self.assertEqual(
            split_redacted,
            "NR\n[usunięto]\n[usunięto]\n[usunięto]\nSUMA PLN 5,00\nNIP 7791011327\n",
        )
        self.assertNotIn("1234", split_redacted)
        self.assertIn("SUMA PLN 5,00", split_redacted)
        self.assertIn("7791011327", split_redacted)

    def test_pan_table_keeps_prices_and_redacts_punctuated_cards(self):
        cases = {
            ",4111 1111 1111 1111": "[usunięto]",
            "Karta:4111111111111111": "[usunięto]",
            "KARTA\n12,99": "KARTA\n12,99",
            "NR\nKARTY ,4111\n,1111 ,1111": "NR\n[usunięto]\n[usunięto]",
            "2 x 3,50": "2 x 3,50",
            "12.99 15.49": "12.99 15.49",
            "MLEKO 3,49 2 szt": "MLEKO 3,49 2 szt",
            "4111-1111-1111-1111": "[usunięto]",
            "****1234": "****1234",
            "1234****": "1234****",
            "4111 1111 1111 1111.00": "[usunięto]",
            "4111 1111 1111 1111,00": "[usunięto]",
            "NR\nKARTY ,41\n,111 ,1111": "NR\nKARTY ,41\n,111 ,1111",
            "KARTA\n1000 szt 3,49": "KARTA\n1000 szt 3,49",
            "KARTA\n12 x 3,50": "KARTA\n12 x 3,50",
            "1,2345 2,3456": "1,2345 2,3456",
            "123.4567 246.9134": "[usunięto]",
            "123.4567=246.9134": "123.4567=246.9134",
            "CARD NUMBER\n4111.11 11.11 1111.11": "CARD NUMBER\n[usunięto]",
            "CARD NUMBER\n4111.11 11.11\n1111.11": "CARD NUMBER\n[usunięto]\n[usunięto]",
            "4111.11 11.11\n1111.11": "[usunięto]\n[usunięto]",
            "KARTA\nok\nok\n12,99": "KARTA\nok\nok\n12,99",
            "KARTA 4111 1111 1111 1111": "[usunięto]",
            "4111 1111 1111 1111": "[usunięto]",
            "4111 1111\n1111 1111": "[usunięto]\n[usunięto]",
            "4111 1111 1111\n1111": "[usunięto]\n[usunięto]",
            "4111-1111\n1111-1111": "[usunięto]\n[usunięto]",
            "4111 1111\n1111\n1111": "[usunięto]\n[usunięto]\n[usunięto]",
            "4111\n1111\n1111\n1111": "[usunięto]\n[usunięto]\n[usunięto]\n[usunięto]",
            "MLEKO 3,49\nCHLEB 4,99": "MLEKO 3,49\nCHLEB 4,99",
            "41.11 1111.11 11.1111": "[usunięto]",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(redact_pan(raw), expected)
                self.assertNotIn("raw_ocr_text", parse_receipt(raw))
        wrapped = "CARD NUMBER\n4111.11 11.11\n1111.11"
        self.assertNotRegex(redact_pan(wrapped), r"[0-9]")
        self.assertNotRegex(redact_pan("4111 1111 1111\n1111"), r"[0-9]")
        split_digits = "CARD NUMBER 4 1 1\n1 1 1 1 1 1\n1 1 1 1 1 1 1"
        split_redacted = redact_pan(split_digits)
        self.assertEqual(split_redacted, "[usunięto]\n[usunięto]\n[usunięto]")
        self.assertNotRegex(split_redacted, r"[0-9]")
        self.assertEqual(redact_pan("KARTA\n****\n1234"), "KARTA\n****\n1234")

    def test_invalid_date_shape_does_not_hide_a_wrapped_pan(self):
        removed = "[usunięto]\n[usunięto]"
        for raw in (
            "4111-1111\n31-13-0000",
            "4111-1111\n00-00-0002",
            "4111-1111\n31.13.0000",
            "4111-1111\n00.00.0002",
            "4111-1111\n31.13-0000",
            "4111-1111\n00.00-0002",
            "22.11.26\n411111111111",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(redact_pan(raw), removed)
                self.assertNotIn("raw_ocr_text", parse_receipt(raw))
                self.assertNotRegex(redact_pan(raw), r"[0-9]")
        for kept in (
            "2026-11-22 14:35",
            "22.11.2026",
            "22.11.26",
            "22/11/2026",
            "29.02.2024",
            "22 11 2026",
            "KARTA\nok\nok\n2026-11-22 14:35",
            "KARTA\n2026-11-22",
            "KARTA\n22.11.2026",
        ):
            with self.subTest(kept=kept):
                self.assertEqual(redact_pan(kept), kept)
        for kept_structured in (
            "NIP 7791011327\n2026-11-22 14:35",
            "NIP 7791011327\n22.11.2026",
            "NIP 7791011327\n22.11.26",
            "5,00 PLN\n2026-11-20 12:34",
            "PLN 5,00\n2026-11-20",
            "PTU A 23,00 8,13 1,87",
        ):
            with self.subTest(kept_structured=kept_structured):
                self.assertEqual(redact_pan(kept_structured), kept_structured)
        self.assertEqual(redact_pan("5,00 PLN\n411111111111\n"), "5,00 PLN\n[usunięto]\n")
        self.assertEqual(redact_pan("PLN 5,00\n411111111111\n"), "PLN 5,00\n[usunięto]\n")
        self.assertEqual(redact_pan("PTU A 23,00 8,13\n411111111111\n"), "PTU A 23,00 8,13\n[usunięto]\n")
        self.assertEqual(redact_pan("PTU 4111111111111111"), "[usunięto]")
        self.assertEqual(redact_pan("NIP 7791011327 4111111111111111"), "[usunięto]")
        self.assertEqual(redact_pan("2026-11-20 4111111111111111"), "[usunięto]")
        self.assertEqual(redact_pan("411111111111\n2026-11-22 14:35"), "[usunięto]\n2026-11-22 14:35")
        self.assertEqual(redact_pan("22.11.2026\n411111111111"), "22.11.2026\n[usunięto]")
        self.assertEqual(redact_pan("ML12 ABCD 1234567"), "ML12 ABCD 1234567")
        labelled = "KARTA\n4 1 1 1 1 1 1 1\n31-13-0000"
        self.assertEqual(redact_pan(labelled), "KARTA\n[usunięto]\n[usunięto]")
        self.assertNotRegex(json.dumps(parse_receipt(labelled)), r"4111|31-13")

    def test_short_mask_run_split_by_a_newline_stays(self):
        cases = (
            "****\n1234",
            "1234\n****",
            "**** **** ****\n1234",
            "XXXX\n1234",
            "****",
            "2 * 3,50",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(redact_pan(raw), raw)
                self.assertNotIn("raw_ocr_text", parse_receipt(raw))

    def test_twelve_digit_run_is_redacted_without_luhn(self):
        valid = "411111111117"
        invalid = "411111111110"
        self.assertEqual(redact_pan(valid), "[usunięto]")
        self.assertNotIn("raw_ocr_text", parse_receipt(valid))
        wrapped = "411111\n111117"
        self.assertEqual(redact_pan(wrapped), "[usunięto]\n[usunięto]")
        self.assertNotRegex(redact_pan(wrapped), r"[0-9]")
        self.assertEqual(redact_pan(invalid), "[usunięto]")
        self.assertEqual(redact_pan("KARTA\n" + invalid), "KARTA\n[usunięto]")
        nip = "527-26-01-234"
        self.assertEqual(redact_pan(nip), nip)
        self.assertEqual(redact_pan("NIP " + nip), "NIP " + nip)
        parsed = parse_receipt("NIP " + nip + "\n")
        self.assertNotIn("raw_ocr_text", parsed)
        self.assertIsNone(parsed["raw_nip"])

    def test_item_three_lines_after_card_label_keeps_prices(self):
        text = (
            "BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-30 12:34\n"
            "KARTA\npin ok\nterminal\n"
            "MLEKO 1 x 123.4567=246.9134 A\nSUMA PLN 246.9134\n"
        )
        parsed = parse_receipt(text)
        self.assertNotIn("raw_ocr_text", parsed)
        self.assertEqual(parsed["lines"][0]["raw_name"], "MLEKO")
        self.assertEqual(parsed["lines"][0]["unit_price_raw"], "123.4567")
        self.assertEqual(parsed["lines"][0]["gross_line_total"], "246.9134")
        self.assertEqual(parsed["total_amount"], "246.9134")
        self.assertEqual(parsed["math_delta"], "0")
        self.assertEqual(redact_pan("KARTA\nok\n12,99"), "KARTA\nok\n12,99")

    def test_quantity_after_card_label_keeps_every_digit(self):
        self.assertEqual(redact_pan("KARTA\n1000 szt 3,49"), "KARTA\n1000 szt 3,49")
        text = (
            "BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-30 12:34\n"
            "KARTA\nplatnosc\nok\n"
            "MLEKO 1000 szt 3,49 A\nSUMA PLN 3,49\n"
        )
        parsed = parse_receipt(text)
        self.assertNotIn("raw_ocr_text", parsed)
        self.assertEqual(parsed["lines"][0]["raw_name"], "MLEKO")
        self.assertEqual(parsed["lines"][0]["quantity"], "1000")
        self.assertEqual(parsed["lines"][0]["raw_unit"], "szt")
        self.assertEqual(parsed["lines"][0]["gross_line_total"], "3.49")
        self.assertEqual(parsed["math_delta"], "0")
        priced = (
            "BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-30 12:34\n"
            "MLEKO 1 x 123.4567=246.9134 A\nSUMA PLN 246.9134\n"
        )
        parsed = parse_receipt(priced)
        self.assertEqual(parsed["lines"][0]["unit_price_raw"], "123.4567")
        self.assertEqual(parsed["lines"][0]["gross_line_total"], "246.9134")
        self.assertEqual(parsed["total_amount"], "246.9134")
        self.assertEqual(parsed["math_delta"], "0")

    def test_bare_karta_line_keeps_following_date_and_total(self):
        text = (
            "BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n"
            "KARTA\n2026-11-22 14:35\n"
            "MLEKO 1 x 12,99 12,99 A\n"
            "KARTA\nSUMA PLN 12,99\n"
        )
        redacted = redact_pan(text)
        self.assertIn("KARTA\n2026-11-22 14:35", redacted)
        self.assertIn("KARTA\nSUMA PLN 12,99", redacted)
        self.assertNotIn("[usunięto]", redacted)
        parsed = parse_receipt(text)
        self.assertEqual(parsed["receipt_date"], "2026-11-22")
        self.assertEqual(parsed["receipt_time"], "14:35")
        self.assertEqual(parsed["total_amount"], "12.99")
        self.assertEqual(parsed["payment_method"], "card")
        self.assertEqual(parsed["lines"][0]["gross_line_total"], "12.99")
        self.assertEqual(parsed["math_delta"], "0")
        draft = self.receipts.parse_text("owner", text)
        self.assertEqual(draft["draft"]["receipt_date"], "2026-11-22")
        self.assertEqual(draft["draft"]["total_amount"], "12.99")
        saved = self.receipts.confirm("owner", confirm_values(draft))["receipt"]
        self.assertEqual(saved["receipt_date"], "2026-11-22")
        self.assertEqual(saved["total_amount"], "12.99")
        row = self.receipt_row(draft["receipt_id"])
        self.assertEqual(row.receipt_date, "2026-11-22")
        self.assertEqual(row.total_amount, "12.99")

    def test_pan_scan_is_bounded_before_a_long_mask_run(self):
        import time
        started = time.perf_counter()
        with self.assertRaises(ReceiptError):
            redact_pan("*" * 50_000)
        self.assertLess(time.perf_counter() - started, 0.5)
        started = time.perf_counter()
        scanned = redact_pan("*" * (32 * 1024 - 1))
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(scanned, "")
        for sample in ("\n" * 50_000, "\n" * (32 * 1024 - 1)):
            started = time.perf_counter()
            with self.assertRaises(ReceiptError):
                redact_pan(sample)
            self.assertLess(time.perf_counter() - started, 0.5)
        many = "12,99 " * ((32 * 1024 - 1) // 6)
        started = time.perf_counter()
        scanned = redact_pan(many)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(scanned, "")

    def test_long_line_is_dropped_before_item_expressions(self):
        import time
        from hestia.budget.receipt_parser import MAX_LINE_CHARS

        price = " 1 x 2,00 2,00 A"
        kept = "MLEKO" + (" " * (MAX_LINE_CHARS - len("MLEKO") - len(price))) + price
        self.assertEqual(len(kept), MAX_LINE_CHARS)
        parsed = parse_receipt(f"SKLEP\n{kept}\nSUMA PLN 2,00\n")
        self.assertEqual(parsed["lines"][0]["raw_name"], "MLEKO")
        self.assertEqual(parsed["lines"][0]["gross_line_total"], "2")
        dropped = "M" + kept
        self.assertEqual(len(dropped), MAX_LINE_CHARS + 1)
        parsed = parse_receipt(f"SKLEP\nMLEKO 1 x 2,00 2,00 A\n{dropped}\nSUMA PLN 2,00\n")
        self.assertEqual([line["raw_name"] for line in parsed["lines"]], ["MLEKO"])
        self.assertEqual(parsed["total_amount"], "2")
        unit = parse_receipt("MLEKO 2 szt x 4,49=8,98 A\nSUMA PLN 8,98\n")
        self.assertEqual(unit["lines"][0]["raw_unit"], "szt")
        self.assertEqual(unit["lines"][0]["unit_price_raw"], "4.49")
        self.assertEqual(unit["math_delta"], "0")
        labelled = parse_receipt("KAUCJA 0,50 PLN\nSUMA PLN 0,50\n")
        self.assertEqual(labelled["lines"][0]["line_kind"], "deposit")
        self.assertEqual(labelled["lines"][0]["gross_line_total"], "0.5")
        blob = "A " * ((32 * 1024 - 1) // 2)
        self.assertLess(len(blob.encode("utf-8")), 32 * 1024)
        self.assertGreater(len(blob), MAX_LINE_CHARS)
        started = time.perf_counter()
        scanned = parse_receipt(blob)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(scanned["lines"], [])
        self.assertIsNone(scanned["total_amount"])

    def test_deposit_is_classified_before_the_name_is_redacted(self):
        hidden = "KAUCJA " + "1A" * 12
        stored = self.receipts.parse_text("owner", f"{hidden} 0,50\nSUMA PLN 0,50\n")
        line = stored["draft"]["lines"][0]
        self.assertEqual(line["line_kind"], "deposit")
        self.assertEqual(line["raw_name"], "[usunięto]")
        self.assertEqual(line["raw_name_key"], "[usunieto]")
        self.assertEqual(line["gross_line_total"], "0.5")
        row = next(item for item in self.rows(db.BudgetReceiptLine) if item.receipt_id == stored["receipt_id"])
        self.assertEqual((row.line_kind, row.raw_name), ("deposit", "[usunięto]"))

    def test_combining_marks_are_capped_before_normalisation(self):
        import time

        from hestia.budget.receipt_parser import _bound_raw_line

        mark = "\u0301"
        self.assertEqual(_bound_raw_line("e" + mark * 8), "e" + mark * 8)
        self.assertEqual(_bound_raw_line("e" + mark * 9), "e" + mark * 8)
        self.assertEqual(_bound_raw_line("x" * 600), "")
        self.assertEqual(_bound_raw_line("x" * 512), "x" * 512)
        self.assertEqual(redact_pan("e" + mark), "é")
        raw = "A" + mark * ((32 * 1024 - 1 - 1) // 2)
        self.assertEqual(len(raw.encode("utf-8")), 32 * 1024 - 1)
        started = time.perf_counter()
        redacted = redact_pan(raw)
        parsed = parse_receipt(raw)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertLessEqual(len(redacted), 512)
        self.assertNotIn(mark * 9, redacted)
        self.assertEqual(parsed["lines"], [])

    def test_money_magnitude_precision_and_folded_name_length_are_limited(self):
        def text(item):
            return f"BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-30 12:34\n{item}\nSUMA PLN 5,00\n"

        hidden = parse_receipt(text("MLEKO 1 x 4111111111111111 5,00 A"))
        self.assertNotIn("4111111111111111", json.dumps(hidden))
        self.assertNotIn("raw_ocr_text", hidden)
        for line in hidden["lines"]:
            self.assertNotIn("4111111111111111", str(line["unit_price_raw"]))
        with self.assertRaises(ReceiptError):
            parse_receipt(text("MLEKO 1 x 1,00000 5,00 A"))
        with self.assertRaises(ReceiptError):
            parse_receipt(text("MLEKO 1 x 1000000000=5,00 A"))
        with self.assertRaises(ReceiptError):
            parse_receipt(
                "BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-30 12:34\n"
                "MLEKO 1 x 5,00 5,00 A\nRABAT 999999999,99\nRABAT 999999999,99\n"
                "SUMA PLN 5,00\n")
        wide = parse_receipt(
            "BIEDRONKA\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-30 12:34\n"
            "MLEKO 1 x 999999999,99=999999999,99 A\nSUMA PLN 999999999,99\n")
        self.assertEqual(wide["lines"][0]["unit_price_raw"], "999999999.99")
        self.assertEqual(wide["total_amount"], "999999999.99")
        folded = "ß" * 128
        with self.assertRaises(ReceiptError):
            parse_receipt(text(f"{folded} 1 x 5,00 5,00 A"))
        parsed = self.draft()
        values = confirm_values(parsed)
        values["lines"][0]["unit_price_raw"] = "4111111111111111"
        with self.assertRaises(ReceiptError):
            self.receipts.confirm("owner", values)
        fraction = confirm_values(parsed)
        fraction["lines"][0]["quantity"] = "1.00000"
        with self.assertRaises(ReceiptError):
            validate_confirm(fraction)
        secret_money = confirm_values(parsed)
        secret_money["lines"][0]["quantity"] = "99999999.9999"
        with self.assertRaises(ReceiptError):
            validate_confirm(secret_money)
        long_name = confirm_values(parsed)
        long_name["lines"][0]["raw_name"] = folded
        with self.assertRaises(ReceiptError):
            validate_confirm(long_name)

    def test_text_fields_count_every_digit_between_letters(self):
        self.assertIsNone(redact_digit_field(None))
        self.assertEqual(redact_digit_field(411111111111), 411111111111)
        self.assertEqual(redact_digit_field("SKLEP 41111111111"), "SKLEP 41111111111")
        self.assertEqual(redact_digit_field("4111 abc 1111 def 1111"), "[usunięto]")
        self.assertEqual(redact_digit_field("4111 MLEKO 1111 CHLEB 1111"), "[usunięto]")
        self.assertEqual(redact_digit_field("４１１１１１１１１１１１"), "[usunięto]")
        self.assertEqual(redact_digit_field("١٢٣٤٥٦٧٨٩٠١٢"), "[usunięto]")
        text = (
            "SKLEP 4111abc1111def1111\nNIP 7791011327\nPARAGON FISKALNY\n2026-11-20 12:34\n"
            "4111 word 1111 word 1111 1 x 3,50 3,50 A\n"
            "CHLEB 1 x 4,20 4,20 A\nSUMA PLN 7,70\n"
        )
        parsed = self.receipts.parse_text("owner", text)
        self.assertEqual(parsed["draft"]["raw_shop_name"], "[usunięto]")
        milk = parsed["draft"]["lines"][0]
        self.assertEqual((milk["raw_name"], milk["raw_name_key"]), ("[usunięto]", "[usunieto]"))
        values = confirm_values(parsed)
        values["raw_shop_name"] = "BIEDRONKA 5351 xx 0055 yy 5555 zz 55"
        values["lines"][0]["raw_name"] = "SER 4111abc1111def1111"
        values["lines"][0]["raw_unit"] = "123456789012"
        confirmed = self.receipts.confirm("owner", values)["receipt"]
        self.assertEqual(confirmed["raw_shop_name"], "[usunięto]")
        self.assertEqual(confirmed["lines"][0]["raw_name"], "[usunięto]")
        self.assertEqual(confirmed["lines"][0]["raw_name_key"], "[usunieto]")
        self.assertEqual(confirmed["lines"][0]["raw_unit"], "[usunięto]")
        self.assertEqual(confirmed["total_amount"], "7.7")
        self.assertEqual(confirmed["receipt_date"], "2026-11-20")
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).raw_shop_name, "[usunięto]")
        _assert_digit_cap(self, confirmed)

    def test_confirm_omitted_or_null_lines_keeps_stored_and_redacted_lines_and_validates_totals(self):
        from sqlalchemy import delete, select

        parsed = self.receipts.parse_text(
            "owner",
            "BIEDRONKA\nNIP 7791011327\n2026-11-20\nCHLEB 1 x 4,00 4,00 A\nSER 1 x 6,00 6,00 A\nSUMA PLN 10,00\nKARTA\n",
        )
        receipt_id = parsed["receipt_id"]
        # Seed a stored line carrying a 12-digit identifier that _line_dict redacts on output.
        secret_name = "KARMA 123456789012"
        secret_unit = "987654321098"
        with self.sessions.begin() as session:
            stored = list(session.scalars(select(db.BudgetReceiptLine).where(
                db.BudgetReceiptLine.receipt_id == receipt_id).order_by(db.BudgetReceiptLine.line_no)))
            stored[0].raw_name = secret_name
            stored[0].raw_name_key = secret_name.lower()
            stored[0].raw_unit = secret_unit

        detail = self.receipts.detail("owner", receipt_id)["receipt"]
        self.assertEqual(detail["lines"][0]["raw_name"], "[usunięto]")
        self.assertEqual(detail["lines"][0]["raw_unit"], "[usunięto]")

        # Mismatched header total with omitted lines fails cleanly and keeps stored data intact.
        bad_total = confirm_values(parsed, total_amount="9.00")
        bad_total.pop("lines")
        with self.assertRaises(ReceiptError) as err:
            self.receipts.confirm("owner", bad_total)
        self.assertEqual((err.exception.code, err.exception.status), ("budget_receipt_total_mismatch", 400))

        # Omitting `lines` keeps stored lines (and unredacted DB strings) intact while updating header.
        no_lines = confirm_values(parsed, raw_shop_name="BIEDRONKA UPDATED")
        no_lines.pop("lines")
        saved = self.receipts.confirm("owner", no_lines)["receipt"]
        self.assertEqual(saved["raw_shop_name"], "BIEDRONKA UPDATED")
        self.assertEqual(saved["lines"][0]["raw_name"], "[usunięto]")
        self.assertEqual(saved["lines"][1]["raw_name"], "SER")
        with self.sessions() as session:
            kept = list(session.scalars(select(db.BudgetReceiptLine).where(
                db.BudgetReceiptLine.receipt_id == receipt_id).order_by(db.BudgetReceiptLine.line_no)))
            self.assertEqual(len(kept), 2)
            self.assertEqual((kept[0].raw_name, kept[0].raw_unit), (secret_name, secret_unit))
            self.assertEqual(kept[1].raw_name, "SER")

        # Sending explicit `"lines": None` also preserves stored lines when total matches.
        saved_v3 = self.receipts.confirm(
            "owner", confirm_values(parsed, expected_version=saved["version"], raw_shop_name="BIEDRONKA V3", lines=None),
        )["receipt"]
        self.assertEqual(saved_v3["raw_shop_name"], "BIEDRONKA V3")
        with self.sessions() as session:
            first = session.scalars(select(db.BudgetReceiptLine).where(
                db.BudgetReceiptLine.receipt_id == receipt_id, db.BudgetReceiptLine.line_no == 1)).one()
            self.assertEqual(first.raw_name, secret_name)

        # Stored line violating gross - discount == net rejects confirm when lines are omitted.
        with self.sessions.begin() as session:
            second = session.scalars(select(db.BudgetReceiptLine).where(
                db.BudgetReceiptLine.receipt_id == receipt_id, db.BudgetReceiptLine.line_no == 2)).one()
            second.gross_line_total = "99.00"
        with self.assertRaises(ReceiptError) as bad_stored:
            self.receipts.confirm("owner", confirm_values(parsed, expected_version=saved_v3["version"], lines=None))
        self.assertEqual((bad_stored.exception.code, bad_stored.exception.status), ("budget_receipt_total_mismatch", 400))

        # Zero stored lines + positive total rejects confirm when lines are omitted.
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetReceiptLine).where(db.BudgetReceiptLine.receipt_id == receipt_id))
        with self.assertRaises(ReceiptError) as empty_stored:
            self.receipts.confirm("owner", confirm_values(parsed, expected_version=saved_v3["version"], lines=None))
        self.assertEqual((empty_stored.exception.code, empty_stored.exception.status), ("budget_receipt_total_mismatch", 400))

        # Omitted-lines path must enforce per-line_kind sign rules identically to validate_confirm
        # (e.g., item totals 10 and -1 summing to 9 must be rejected).
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetReceiptLine(
                    receipt_id=receipt_id, owner="owner", line_no=1, line_kind="item", raw_name="Positive",
                    raw_name_key="positive", ptu_code="A", quantity="1", raw_unit=None, unit_price_raw=None,
                    gross_line_total="10.00", discount_amount="0", net_line_total="10.00",
                ),
                db.BudgetReceiptLine(
                    receipt_id=receipt_id, owner="owner", line_no=2, line_kind="item", raw_name="Negative Item",
                    raw_name_key="negative item", ptu_code="A", quantity="1", raw_unit=None, unit_price_raw=None,
                    gross_line_total="-1.00", discount_amount="0", net_line_total="-1.00",
                ),
            ])
        neg_item = confirm_values(parsed, expected_version=saved_v3["version"], total_amount="9.00", lines=None)
        with self.assertRaises(ReceiptError) as neg_err:
            self.receipts.confirm("owner", neg_item)
        self.assertEqual((neg_err.exception.code, neg_err.exception.status), ("budget_receipt_total_mismatch", 400))

        for bad_line in (
            dict(line_kind="deposit", gross_line_total="10.00", discount_amount="1.00", net_line_total="9.00"),
            dict(line_kind="basket_discount", gross_line_total="9.00", discount_amount="0", net_line_total="9.00"),
            dict(line_kind="other", gross_line_total="10.00", discount_amount="1.00", net_line_total="9.00"),
        ):
            with self.subTest(bad_kind=bad_line["line_kind"]):
                with self.sessions.begin() as session:
                    session.execute(delete(db.BudgetReceiptLine).where(db.BudgetReceiptLine.receipt_id == receipt_id))
                    session.add(db.BudgetReceiptLine(
                        receipt_id=receipt_id, owner="owner", line_no=1, raw_name="Bad", raw_name_key="bad",
                        ptu_code="A", quantity="1", raw_unit=None, unit_price_raw=None, **bad_line,
                    ))
                bad_sign = confirm_values(parsed, expected_version=saved_v3["version"], total_amount="9.00", lines=None)
                with self.assertRaises(ReceiptError) as err_kind:
                    self.receipts.confirm("owner", bad_sign)
                self.assertEqual((err_kind.exception.code, err_kind.exception.status),
                                 ("budget_receipt_total_mismatch", 400))

        # Omitted-lines path skips text/case-fold length validation so stored Gmail lines
        # whose folded key exceeds 128 chars (e.g. 'ß' -> 'ss') still confirm cleanly.
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetReceiptLine).where(db.BudgetReceiptLine.receipt_id == receipt_id))
            session.add(db.BudgetReceiptLine(
                receipt_id=receipt_id, owner="owner", line_no=1, line_kind="item", raw_name="ß" * 128,
                raw_name_key="ss" * 64, ptu_code="A", quantity="1", raw_unit=None, unit_price_raw="10.00",
                gross_line_total="10.00", discount_amount="0", net_line_total="10.00",
            ))
        folded_saved = self.receipts.confirm(
            "owner", confirm_values(parsed, expected_version=saved_v3["version"], total_amount="10.00", lines=None),
        )["receipt"]
        self.assertEqual(folded_saved["lines"][0]["raw_name"], "ß" * 128)

        # Omitting raw_shop_name / raw_nip preserves stored (incl. projection-redacted) header values; explicit None clears.
        stored_shop = "SKLEP 123456789012"
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetReceiptLine).where(db.BudgetReceiptLine.receipt_id == receipt_id))
            session.add(db.BudgetReceiptLine(
                receipt_id=receipt_id, owner="owner", line_no=1, line_kind="item", raw_name="SER",
                raw_name_key="ser", ptu_code="A", quantity="1", raw_unit=None, unit_price_raw="10.00",
                gross_line_total="10.00", discount_amount="0", net_line_total="10.00",
            ))
            session.get(db.BudgetReceipt, receipt_id).raw_shop_name = stored_shop
            session.get(db.BudgetReceipt, receipt_id).raw_nip = "7791011327"
        detail_redacted_shop = self.receipts.detail("owner", receipt_id)["receipt"]
        self.assertEqual(detail_redacted_shop["raw_shop_name"], "[usunięto]")
        omitted_headers = confirm_values(parsed, expected_version=folded_saved["version"])
        omitted_headers.pop("raw_shop_name")
        omitted_headers.pop("raw_nip")
        omitted_headers.pop("lines")
        kept_headers = self.receipts.confirm("owner", omitted_headers)["receipt"]
        self.assertEqual(kept_headers["raw_shop_name"], "[usunięto]")
        self.assertEqual(kept_headers["raw_nip"], "7791011327")
        self.assertEqual(self.receipt_row(receipt_id).raw_shop_name, stored_shop)
        cleared_headers = confirm_values(
            parsed, expected_version=kept_headers["version"], raw_shop_name=None, raw_nip=None, lines=None,
        )
        cleared_saved = self.receipts.confirm("owner", cleared_headers)["receipt"]
        self.assertIsNone(cleared_saved["raw_shop_name"])
        self.assertIsNone(cleared_saved["raw_nip"])

        # Legacy path sending explicit lines still replaces stored lines cleanly.
        legacy_saved = self.receipts.confirm("owner", confirm_values(parsed, expected_version=cleared_saved["version"]))["receipt"]
        self.assertEqual(len(legacy_saved["lines"]), 2)

    def test_wrapped_pan_never_survives_storage_or_responses(self):
        rng = random.Random(20261123)
        separators = list(" .,/-")
        words = ("MLEKO", "CHLEB", "SER", "MASLO", "KAWA")
        items = ("MLEKO 1 x 3,50 3,50 A", "CHLEB 1 x 4,20 4,20 A")
        for trial in range(24):
            length = rng.randint(13, 19)
            pan = "".join(str(rng.randrange(10)) for _ in range(length))
            kind = ("separators", "letters", "words")[trial % 3]
            if kind == "separators":
                pieces = rng.randint(1, 6)
                if pieces == 1:
                    groups = [pan]
                else:
                    cuts = sorted(rng.sample(range(1, length), pieces - 1))
                    bounds = [0, *cuts, length]
                    groups = [pan[start:end] for start, end in zip(bounds, bounds[1:])]
            else:
                # Each group stays under the joined-run limit. Letters or words between
                # them hide the PAN from the line scanner, so the field rule has to fire.
                while True:
                    pieces = rng.randint(2, 6)
                    cuts = sorted(rng.sample(range(1, length), pieces - 1))
                    bounds = [0, *cuts, length]
                    groups = [pan[start:end] for start, end in zip(bounds, bounds[1:])]
                    if all(len(group) < 12 for group in groups):
                        break
            header = ["BIEDRONKA", "NIP 7791011327", "PARAGON FISKALNY", "2026-11-20 12:34", "KARTA"]
            sequence = list(items)
            planted = None
            if kind == "separators":
                pan_lines = []
                for group in groups:
                    body = []
                    for offset, digit in enumerate(group):
                        if offset:
                            body.append(rng.choice(separators))
                        body.append(digit)
                    pan_lines.append("".join(body))
                pending = list(items)
                sequence = []
                for line in pan_lines:
                    if pending and rng.random() < 0.5:
                        sequence.append(pending.pop(0))
                    sequence.append(line)
                sequence.extend(pending)
            else:
                glued = []
                for index, group in enumerate(groups):
                    if index:
                        glued.append(rng.choice("abcdefghijklmn") if kind == "letters" else rng.choice(words))
                    glued.append(group)
                payload = "".join(glued) if kind == "letters" else " ".join(glued)
                if rng.random() < 0.5:
                    header[0] = f"SKLEP {payload}"
                    planted = "shop"
                else:
                    sequence[0] = f"{payload} nazwa 1 x 3,50 3,50 A"
                    planted = "item"
            text = "\n".join((*header, *sequence, "PIN OK", "SUMA PLN 7,70"))
            with self.subTest(trial=trial, kind=kind, length=length, pieces=pieces, planted=planted):
                parsed = self.receipts.parse_text("owner", text)
                confirmed = self.receipts.confirm("owner", confirm_values(parsed))
                detail = self.receipts.detail("owner", parsed["receipt_id"])
                for body in (parsed, confirmed, detail):
                    _assert_digit_cap(self, body)
                    self.assertNotIn("raw_ocr_text", json.dumps(body))
                if planted == "shop":
                    self.assertEqual(parsed["draft"]["raw_shop_name"], "[usunięto]")
                elif planted == "item":
                    self.assertIn("[usunięto]", [line["raw_name"] for line in parsed["draft"]["lines"]])
                row = self.receipt_row(parsed["receipt_id"])
                self.assertIsNone(row.raw_ocr_text)
                _scan_text_columns(self, row)
                for line in self.rows(db.BudgetReceiptLine):
                    if line.receipt_id == row.id:
                        _scan_text_columns(self, line)
                self.assertEqual(confirmed["receipt"]["total_amount"], "7.7")
                self.assertEqual(confirmed["receipt"]["receipt_date"], "2026-11-20")

    def test_confirm_is_bound_to_preview_evidence_and_reconcile_is_idempotent(self):
        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        self.add_transaction("a", "window", amount="100", booking_date="2026-11-10",
                             value_date="2026-11-10", transaction_date="2026-11-20")
        parsed = self.draft(day="2026-11-20")
        candidate = next(item for item in parsed["candidates"] if item["entry_reference"] == reference_digest("window"))
        link = {"account_id": candidate["account_id"], "entry_reference": reference_digest("window"),
                "candidate_token": candidate["candidate_token"]}
        merchant = confirm_values(parsed, link=link, raw_shop_name="INNY SKLEP")
        with self.assertRaises(ReceiptError) as stale_merchant:
            self.receipts.confirm("owner", merchant)
        self.assertEqual((stale_merchant.exception.code, stale_merchant.exception.status),
                         ("budget_receipt_stale_source", 409))
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).status, "draft")

        self.change_transaction("a", "window", transaction_date="2026-10-01")
        with self.assertRaises(ReceiptError) as stale_date:
            self.receipts.confirm("owner", confirm_values(parsed, link=link))
        self.assertEqual(stale_date.exception.code, "budget_receipt_stale_source")
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).status, "draft")

        before = self.fingerprint("a", "window")
        self.change_transaction("a", "window", transaction_date="2026-11-20")
        self.assertEqual(self.fingerprint("a", "window"), before)
        confirmed = self.receipts.confirm("owner", confirm_values(parsed, link=link))["receipt"]
        self.assertTrue(confirmed["linked"])
        self.assertIn("2026-11-20", self.receipt_row(parsed["receipt_id"]).linked_evidence)
        self.change_transaction("a", "window", transaction_date="2026-10-01")
        self.assertEqual(self.fingerprint("a", "window"), before)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 1)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 5), 0)
        self.assert_latched(parsed["receipt_id"], confirmed["version"])

    def test_spaced_and_decomposed_shop_names_match_the_candidate_token(self):
        import unicodedata

        from hestia.budget.receipt_parser import normalize_shop_name

        self.assertIsNone(normalize_shop_name(None))
        self.assertEqual(normalize_shop_name("   "), "")
        shop = unicodedata.normalize("NFD", "ŻÓŁTY") + "  SKLEP"
        parsed = self.receipts.parse_text("owner", receipt_text().replace(PRIVATE_SHOP, shop, 1))
        self.assertEqual(parsed["draft"]["raw_shop_name"], "ŻÓŁTY  SKLEP")
        draft_detail = self.receipts.detail("owner", parsed["receipt_id"])
        match = next(item for item in draft_detail["candidates"] if item["entry_reference"] == reference_digest("debit"))
        saved = self.receipts.confirm("owner", confirm_values(parsed, link={
            "account_id": match["account_id"], "entry_reference": match["entry_reference"],
            "candidate_token": match["candidate_token"],
        }))["receipt"]
        self.assertNotIn("candidates", self.receipts.detail("owner", saved["receipt_id"]))
        self.assertEqual(saved["raw_shop_name"], "ŻÓŁTY SKLEP")
        self.assertTrue(saved["linked"])
        self.assertEqual(json.loads(self.receipt_row(parsed["receipt_id"]).linked_evidence)["raw_shop_name"],
                         "ŻÓŁTY SKLEP")

    def test_needs_review_reissue_includes_own_reservation(self):
        import unicodedata

        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        parsed, confirmed = self.confirmed()
        self.change_transaction(remittance_information=["Changed after confirm"])
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 1)
        identifier = parsed["receipt_id"]
        version = self.receipt_row(identifier).version
        self.assertEqual(self.receipt_row(identifier).status, "needs_review")
        spaced = unicodedata.normalize("NFD", "ŻÓŁTY") + "  SKLEP"
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, identifier).raw_shop_name = spaced
        again = self.receipts.parse_text("owner", receipt_text())
        self.assertNotIn("debit", {item["entry_reference"] for item in again["candidates"]})
        detail = self.receipts.detail("owner", identifier)
        api_contract.BudgetReceiptDetailResult.model_validate(detail)
        match = next(item for item in detail["candidates"] if item["entry_reference"] == reference_digest("debit"))
        stale = confirm_values(parsed, link=self.link(), expected_version=version)
        with self.assertRaises(ReceiptError) as rejected:
            self.receipts.confirm("owner", stale)
        self.assertEqual(rejected.exception.code, "budget_receipt_stale_source")
        self.assertEqual(self.receipt_row(identifier).status, "needs_review")
        saved = self.receipts.confirm("owner", confirm_values(
            parsed, link={"account_id": match["account_id"], "entry_reference": match["entry_reference"],
                          "candidate_token": match["candidate_token"]},
            expected_version=version, raw_shop_name="ŻÓŁTY SKLEP",
        ))["receipt"]
        self.assertEqual((saved["status"], saved["raw_shop_name"], saved["linked"]),
                         ("confirmed", "ŻÓŁTY SKLEP", True))
        self.assertEqual(json.loads(self.receipt_row(identifier).linked_evidence)["raw_shop_name"], "ŻÓŁTY SKLEP")
        self.assertEqual(saved["version"], confirmed["version"] + 2)

    def test_confirm_token_binds_remittance_fingerprint_and_stays_compact(self):
        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        reference = "Ż" * 450
        digest = reference_digest(reference)
        self.add_transaction("a", reference, amount="100", entry_reference=reference, booking_date="2026-11-30")
        parsed = self.draft()
        candidate = next(item for item in parsed["candidates"] if item["entry_reference"] == digest)
        token = candidate["candidate_token"]
        self.assertGreater(len(token), 43)
        self.assertLessEqual(len(token), 4096)
        self.assertNotIn(".", token)
        self.assertNotIn(reference, token)
        self.assertNotIn(reference, json.dumps(candidate))
        link = {"account_id": candidate["account_id"], "entry_reference": digest, "candidate_token": token}
        saved = self.receipts.confirm("owner", confirm_values(parsed, link=link))["receipt"]
        stored = self.receipt_row(parsed["receipt_id"])
        self.assertEqual(stored.linked_entry_ref, reference)
        evidence = json.loads(stored.linked_evidence)
        self.assertEqual(evidence["entry_reference"], digest)
        self.assertNotIn(reference, stored.linked_evidence)
        self.assertEqual(saved["linked_entry_ref"], digest)
        self.assertNotIn(reference, json.dumps(saved))
        self.assertEqual(evidence["remittance_digest"], hashlib.sha256(
            json.dumps(["Synthetic SHOP"], ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest())
        self.assertNotIn("remittance_information", evidence)
        self.assertNotIn("creditor_name", evidence)
        self.assertIsNone(evidence["creditor_digest"])
        self.assertEqual(len(evidence["financial_fingerprint"]), 64)
        self.assertTrue(saved["linked"])

        preview = self.draft(item="MLEKO INNE")
        issued = next(item for item in preview["candidates"] if item["entry_reference"] == reference_digest("debit"))
        debit = {"account_id": issued["account_id"], "entry_reference": issued["entry_reference"],
                 "candidate_token": issued["candidate_token"]}
        self.change_transaction(remittance_information=["Corrected synthetic text"])
        with self.assertRaises(ReceiptError) as stale_text:
            self.receipts.confirm("owner", confirm_values(preview, link=debit))
        self.assertEqual((stale_text.exception.code, stale_text.exception.status),
                         ("budget_receipt_stale_source", 409))
        self.assertEqual(self.receipt_row(preview["receipt_id"]).status, "draft")
        self.change_transaction(remittance_information=["Synthetic SHOP"])
        self.change_transaction(amount="101")
        with self.assertRaises(ReceiptError) as stale_amount:
            self.receipts.confirm("owner", confirm_values(preview, link=debit))
        self.assertEqual(stale_amount.exception.code, "budget_receipt_stale_source")
        self.change_transaction(amount="100")

        fresh = self.draft(item="MLEKO TRZECIE")
        fresh_link = {"account_id": self.accounts["a"], "entry_reference": reference_digest("debit")}
        fresh_link["candidate_token"] = next(
            item["candidate_token"] for item in fresh["candidates"] if item["entry_reference"] == reference_digest("debit"))
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetTransaction, (self.accounts["a"], "debit")))
        with self.assertRaises(ReceiptError) as missing:
            self.receipts.confirm("owner", confirm_values(fresh, link=fresh_link))
        self.assertEqual((missing.exception.code, missing.exception.status),
                         ("budget_receipt_stale_source", 409))
        self.assertEqual(self.receipt_row(fresh["receipt_id"]).status, "draft")

        self.add_transaction("a", "debit", amount="100", booking_date="2026-11-30")
        rebound = self.draft()
        confirmed = self.receipts.confirm("owner", confirm_values(rebound, link=self.link()))["receipt"]
        self.change_transaction(remittance_information=["Changed after confirm"])
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 1)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 1), 0)
        self.assert_latched(rebound["receipt_id"], confirmed["version"])

    def test_confirm_accepts_only_the_digest_named_by_the_token(self):
        parsed = self.draft()
        account = self.accounts["a"]
        digest = reference_digest("debit")
        token = next(item["candidate_token"] for item in parsed["candidates"]
                     if item["entry_reference"] == digest)
        other = "other-account-only"
        self.add_transaction("b", other, amount="100", booking_date="2026-11-30")
        other_digest = reference_digest(other)
        from sqlalchemy import literal_column, select
        with self.sessions() as session:
            foreign = session.scalar(select(literal_column("budget_transactions.rowid")).where(
                db.BudgetTransaction.account_id == self.accounts["b"],
                db.BudgetTransaction.entry_reference == other,
            ))
            self.assertIsNotNone(foreign)
            self.assertIsNone(BudgetReceiptStore._named_transaction(session, "owner", account, foreign))

        def confirm(link, *, lookup=False):
            def run():
                with self.assertRaises(ReceiptError) as rejected:
                    self.receipts.confirm("owner", confirm_values(parsed, link=link))
                self.assertEqual((rejected.exception.code, rejected.exception.status),
                                 ("budget_receipt_invalid_request", 400))
                self.assertEqual(self.receipt_row(parsed["receipt_id"]).status, "draft")

            if lookup:
                run()
                return
            with mock.patch.object(BudgetReceiptStore, "_named_transaction", side_effect=AssertionError("lookup")):
                run()

        with self.subTest(case="wrong token"):
            confirm({"account_id": account, "entry_reference": digest, "candidate_token": "not a token!"})
        with self.subTest(case="forged token"):
            confirm({"account_id": account, "entry_reference": digest, "candidate_token": "a" * 43})
        with self.subTest(case="raw key"):
            confirm({"account_id": account, "entry_reference": "debit", "candidate_token": token}, lookup=True)
        with self.subTest(case="non-ascii digest of digest length"):
            confirm({"account_id": account, "entry_reference": "\u0105" * 64, "candidate_token": token}, lookup=True)
        with self.subTest(case="uppercase digest"):
            confirm({"account_id": account, "entry_reference": digest.upper(), "candidate_token": token}, lookup=True)
        with self.subTest(case="other account digest"):
            confirm({"account_id": account, "entry_reference": other_digest, "candidate_token": token}, lookup=True)
        with self.subTest(case="other account id"):
            confirm({"account_id": self.accounts["b"], "entry_reference": other_digest, "candidate_token": token})
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, account).enabled = False
        with self.subTest(case="disabled account"):
            confirm({"account_id": account, "entry_reference": digest, "candidate_token": token})
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, account).enabled = True
        self.assertRaises(ReceiptError, self.receipts._open_candidate_token, None)
        saved = self.receipts.confirm("owner", confirm_values(self.draft(item="MLEKO TOKEN"), link=self.link()))
        self.assertTrue(saved["receipt"]["linked"])
        self.assertEqual(saved["receipt"]["linked_entry_ref"], digest)
        self.assertEqual(self.receipt_row(saved["receipt"]["receipt_id"]).linked_entry_ref, "debit")

    def test_api_bodies_omit_the_raw_ledger_key(self):
        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        reference = "Ż" * 32
        self.add_transaction("a", reference, amount="100", booking_date="2026-11-30")
        parsed = self.draft()
        digest = reference_digest(reference)
        token = next(item["candidate_token"] for item in parsed["candidates"]
                     if item["entry_reference"] == digest)
        draft_detail = self.receipts.detail("owner", parsed["receipt_id"])
        listed = self.receipts.list("owner", {})
        found = self.receipts.search("owner", {"query": "mleko"})
        self.assertGreaterEqual(len(found["receipts"]), 1)
        confirmed = self.receipts.confirm("owner", confirm_values(parsed, link={
            "account_id": self.accounts["a"], "entry_reference": digest, "candidate_token": token,
        }))
        linked_detail = self.receipts.detail("owner", parsed["receipt_id"])
        self.change_transaction("a", reference, remittance_information=["Changed after confirm"])
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 1)
        reviewed = self.receipts.detail("owner", parsed["receipt_id"])
        self.assertIn(digest, {item["entry_reference"] for item in reviewed["candidates"]})
        bodies = {
            "parse-text": parsed, "candidates": parsed["candidates"], "detail": draft_detail,
            "list": listed, "search": found, "confirm": confirmed, "detail-linked": linked_detail,
            "list-linked": self.receipts.list("owner", {"linked": True}),
            "search-linked": self.receipts.search("owner", {"query": "mleko"}),
            "detail-review": reviewed, "candidates-review": reviewed["candidates"],
        }
        escaped = json.dumps(reference)[1:-1]
        for name, body in bodies.items():
            with self.subTest(body=name):
                self.assertNotIn(reference, json.dumps(body, ensure_ascii=False))
                self.assertNotIn(escaped, json.dumps(body))
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).linked_entry_ref, reference)
        self.assertEqual(confirmed["receipt"]["linked_entry_ref"], digest)

    def test_long_remittance_still_yields_a_candidate_and_confirms(self):
        remittance = "S" * 4096
        self.change_transaction(remittance_information=[remittance])
        parsed = self.draft()
        candidate = next(item for item in parsed["candidates"] if item["entry_reference"] == reference_digest("debit"))
        self.assertNotIn("remittance_information", candidate)
        self.assertNotIn("creditor_name", candidate)
        self.assertNotIn(remittance, json.dumps(candidate))
        saved = self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))["receipt"]
        self.assertTrue(saved["linked"])
        raw = self.receipt_row(parsed["receipt_id"]).linked_evidence
        self.assertLessEqual(len(raw), 4096)
        self.assertNotIn(remittance, raw)
        evidence = json.loads(raw)
        digest = hashlib.sha256(json.dumps(
            [remittance], ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
        self.assertEqual(evidence["remittance_digest"], digest)
        self.assertEqual(len(digest), 64)

    def test_creditor_metadata_must_match_the_ledger_fingerprint(self):
        self.metadata("a", "debit", {"creditor_name": "BIEDRONKA GROCER"})
        parsed = self.draft()
        match = next(item for item in parsed["candidates"] if item["entry_reference"] == reference_digest("debit"))
        self.assertTrue(match["merchant_token_hit"])
        self.assertNotIn("creditor_name", match)
        self.assertNotIn("BIEDRONKA GROCER", json.dumps(match))
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit")).ledger_fingerprint = "0" * 64
        hidden = self.draft(total="8", item="MLEKO INNE")
        self.assertNotIn("BIEDRONKA GROCER", json.dumps(hidden["candidates"]))
        again = self.receipts.parse_text("owner", receipt_text())
        debit = next(item for item in again["candidates"] if item["entry_reference"] == reference_digest("debit"))
        self.assertNotIn("creditor_name", debit)
        self.assertNotIn("BIEDRONKA GROCER", json.dumps(debit))
        with self.assertRaises(ReceiptError) as stale:
            self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))
        self.assertEqual(stale.exception.code, "budget_receipt_stale_source")
        self.assertEqual(self.receipt_row(parsed["receipt_id"]).status, "draft")
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "debit")).ledger_fingerprint = self.fingerprint()
        saved = self.receipts.confirm("owner", confirm_values(parsed, link=self.link()))["receipt"]
        raw = self.receipt_row(parsed["receipt_id"]).linked_evidence
        self.assertNotIn("BIEDRONKA GROCER", raw)
        self.assertNotIn("creditor_name", json.loads(raw))
        self.assertEqual(json.loads(raw)["creditor_digest"], hashlib.sha256(b"BIEDRONKA GROCER").hexdigest())
        self.assertTrue(saved["linked"])

    def test_later_confirm_keeps_the_first_deadline(self):
        parsed = self.draft()
        first = self.receipts.confirm("owner", confirm_values(parsed))["receipt"]
        self.now += 5000
        edited = self.receipts.confirm("owner", confirm_values(
            parsed, expected_version=first["version"], receipt_time="18:01"))["receipt"]
        self.assertIsNone(edited["raw_text_expires_at"])
        self.assertIsNone(first["raw_text_expires_at"])
        self.assertEqual(edited["image_expires_at"], first["image_expires_at"])
        self.assertEqual(first["image_expires_at"], self.now - 5000 + CONFIRMED_TEXT_TTL_SECONDS)
        self.assertNotIn("raw_ocr_text", edited)
        self.assertIsNone(self.receipt_row(parsed["receipt_id"]).raw_ocr_text)

    def test_retain_expired_purges_drafts_when_the_flag_is_off(self):
        expired = self.draft(total="7")
        kept = self.draft()
        confirmed = self.receipts.confirm("owner", confirm_values(kept))["receipt"]
        self.now += DRAFT_TTL_SECONDS + 1
        with mock.patch.dict(os.environ, {receipt_api.FLAG: "0"}), \
                mock.patch("hestia.budget.api._secret", side_effect=AssertionError("browser secret")):
            result = receipt_api.retain_expired(self.path, clock=lambda: self.now)
        self.assertEqual(result, {"drafts_purged": 1, "raw_texts_purged": 0})
        self.assertNotIn(expired["receipt_id"], json.dumps(result))
        self.assertIsNone(self.receipt_row(expired["receipt_id"]))
        row = self.receipt_row(kept["receipt_id"])
        self.assertIsNone(row.raw_text_expires_at)
        self.assertIsNone(confirmed["raw_text_expires_at"])
        self.assertIsNone(row.raw_ocr_text)
        self.assertEqual(row.image_expires_at, confirmed["image_expires_at"])

    def test_delete_and_draft_purge_erase_receipt_audit_rows(self):
        parsed = self.draft()
        identifier = parsed["receipt_id"]
        with self.sessions.begin() as session:
            session.add(db.BudgetAnalyticsAudit(
                owner="other", subject_type="receipt", subject_id=identifier, action="create",
                before_json="null", after_json="{}", changed_at=1))
            session.add(db.BudgetAnalyticsAudit(
                owner="owner", subject_type="category", subject_id=identifier, action="create",
                before_json="null", after_json="{}", changed_at=1))
        deleted = self.receipts.delete("owner", {"receipt_id": identifier, "expected_version": 1})
        self.assertEqual(deleted, {"ok": True, "deleted": True})
        self.assertNotIn(identifier, json.dumps(deleted))
        remaining = self.rows(db.BudgetAnalyticsAudit)
        self.assertFalse(any(row.owner == "owner" and row.subject_type == "receipt" and row.subject_id == identifier
                             for row in remaining))
        self.assertTrue(any(row.owner == "other" and row.subject_id == identifier for row in remaining))
        self.assertTrue(any(row.subject_type == "category" and row.subject_id == identifier for row in remaining))

        draft = self.draft(total="7")
        draft_id = draft["receipt_id"]
        self.assertTrue(any(row.owner == "owner" and row.subject_type == "receipt" and row.subject_id == draft_id
                            for row in self.rows(db.BudgetAnalyticsAudit)))
        self.now += DRAFT_TTL_SECONDS + 1
        purged = self.receipts.purge_expired("owner")
        self.assertNotIn(draft_id, json.dumps(purged))
        self.assertEqual(set(purged), {"drafts_purged", "raw_texts_purged"})
        self.assertFalse(any(row.owner == "owner" and row.subject_type == "receipt" and row.subject_id == draft_id
                             for row in self.rows(db.BudgetAnalyticsAudit)))

    def _seed_imported(self, *, document_kind, issue_date, due_date=None, total="100", shop="ORANGE POLSKA",
                       nip="5260250995", source_kind="email", with_line=False):
        import uuid
        identifier = str(uuid.uuid4())
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(
                id=identifier, owner="owner", raw_shop_name=shop, raw_nip=nip,
                receipt_kind="purchase", receipt_date=issue_date, due_date=due_date,
                total_amount=total, currency="PLN", payment_method=None,
                status="imported", source_kind=source_kind, document_kind=document_kind,
                import_expires_at=self.now + 86400, version=1, created_at=self.now, updated_at=self.now,
            ))
            if with_line:
                session.add(db.BudgetReceiptLine(
                    receipt_id=identifier, owner="owner", line_no=1, line_kind="other",
                    raw_name="Opłata miesięczna", raw_name_key="oplata miesieczna", quantity="1",
                    gross_line_total=total, discount_amount="0", net_line_total=total,
                ))
        return identifier

    def test_imported_invoice_due_date_window_confirm_reservation_and_reconcile(self):
        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        # Invoice issued 2026-11-01, due 2026-11-20 => window is [2026-10-31, 2026-12-04].
        # Default debit in AnalyticsCase is 2026-11-30 (100 PLN), which is inside [2026-10-31, 2026-12-04].
        # Add out-of-window transactions at issue_date-2 and due_date+15, and exact boundary transactions.
        self.add_transaction("a", "inv-before", amount="123.45", booking_date="2026-10-30", value_date=None)
        self.add_transaction("a", "inv-after", amount="123.45", booking_date="2026-12-05", value_date=None)
        self.add_transaction("a", "inv-due-plus-3", amount="123.45", booking_date="2026-11-23", value_date=None,
                             remittance_information=["ORANGE POLSKA FV"])
        invoice_id = self._seed_imported(
            document_kind="invoice", issue_date="2026-11-01", due_date="2026-11-20",
            total="123.45", source_kind="attachment",
        )
        second_invoice_id = self._seed_imported(
            document_kind="invoice", issue_date="2026-11-01", due_date="2026-11-20",
            total="123.45", source_kind="attachment",
        )
        detail = self.receipts.detail("owner", invoice_id)
        api_contract.BudgetReceiptDetailResult.model_validate(detail)
        candidates = detail["candidates"]
        self.assertEqual([item["entry_reference"] for item in candidates], [reference_digest("inv-due-plus-3")])
        self.assertTrue(candidates[0]["merchant_token_hit"])
        self.assertEqual(candidates[0]["date_distance"], 22)

        # Boundary inclusivity: issue_date - 1 and due_date + 14 are accepted.
        self.add_transaction("a", "inv-start", amount="123.45", booking_date="2026-10-31", value_date=None)
        self.add_transaction("a", "inv-end", amount="123.45", booking_date="2026-12-04", value_date=None)
        boundary_refs = {item["entry_reference"] for item in self.receipts.detail("owner", invoice_id)["candidates"]}
        self.assertEqual(boundary_refs, {
            reference_digest("inv-due-plus-3"),
            reference_digest("inv-start"),
            reference_digest("inv-end"),
        })
        self.assertNotIn(reference_digest("inv-before"), boundary_refs)
        self.assertNotIn(reference_digest("inv-after"), boundary_refs)

        # Verify _verify_link enforces the exact same invoice date window at confirm.
        cleaned_invoice = validate_confirm({
            "receipt_id": invoice_id, "expected_version": 1, "receipt_kind": "purchase",
            "receipt_date": "2026-11-01", "receipt_time": None, "total_amount": "123.45",
            "currency": "PLN", "payment_method": "unknown",
            "lines": [{
                "line_kind": "other", "raw_name": "Faktura", "ptu_code": None, "quantity": "1",
                "raw_unit": None, "unit_price_raw": None, "gross_line_total": "123.45",
                "discount_amount": "0", "net_line_total": "123.45",
            }],
            "link": None,
        })
        cleaned_invoice["document_kind"] = "invoice"
        cleaned_invoice["due_date"] = "2026-11-20"
        with self.sessions() as session:
            for bad_ref in ("inv-before", "inv-after"):
                with self.subTest(bad_ref=bad_ref), self.assertRaises(ReceiptError):
                    self.receipts._verify_link(
                        session, "owner", cleaned_invoice,
                        {"account_id": self.accounts["a"], "entry_reference": bad_ref}, invoice_id,
                    )

        # Confirming the imported invoice with the token issued while imported links & confirms it.
        summary_line = {
            "line_kind": "other", "raw_name": "Faktura Orange", "ptu_code": None, "quantity": "1",
            "raw_unit": None, "unit_price_raw": None, "gross_line_total": "123.45",
            "discount_amount": "0", "net_line_total": "123.45",
        }
        chosen = candidates[0]
        stale_cas = {
            "receipt_id": invoice_id, "expected_version": 99, "receipt_kind": "purchase",
            "receipt_date": "2026-11-01", "receipt_time": None, "total_amount": "123.45",
            "currency": "PLN", "payment_method": "unknown", "lines": [summary_line],
            "link": {
                "account_id": chosen["account_id"],
                "entry_reference": chosen["entry_reference"],
                "candidate_token": chosen["candidate_token"],
            },
        }
        second_pre_confirm = next(
            item for item in self.receipts.detail("owner", second_invoice_id)["candidates"]
            if item["entry_reference"] == chosen["entry_reference"]
        )
        with self.assertRaises(ReceiptError) as cas_err:
            self.receipts.confirm("owner", stale_cas)
        self.assertEqual((cas_err.exception.code, cas_err.exception.status), ("budget_receipt_conflict", 409))

        confirmed = self.receipts.confirm("owner", {**stale_cas, "expected_version": 1, "link": dict(stale_cas["link"])})["receipt"]
        self.assertEqual((confirmed["status"], confirmed["linked"], confirmed["version"]), ("confirmed", True, 2))
        self.assertIsNone(confirmed["import_expires_at"])
        self.assertEqual(confirmed["linked_entry_ref"], reference_digest("inv-due-plus-3"))
        self.assertNotIn("candidates", self.receipts.detail("owner", invoice_id))

        # Second imported invoice no longer sees the reserved transaction; old token fails with 409.
        second_refs = {item["entry_reference"] for item in self.receipts.detail("owner", second_invoice_id)["candidates"]}
        self.assertNotIn(reference_digest("inv-due-plus-3"), second_refs)
        with self.assertRaises(ReceiptError) as already_linked:
            self.receipts.confirm("owner", {
                **stale_cas, "receipt_id": second_invoice_id, "expected_version": 1,
                "link": {
                    "account_id": second_pre_confirm["account_id"],
                    "entry_reference": second_pre_confirm["entry_reference"],
                    "candidate_token": second_pre_confirm["candidate_token"],
                },
            })
        self.assertEqual((already_linked.exception.code, already_linked.exception.status),
                         ("budget_receipt_tx_already_linked", 409))

        # Reconcile keeps the linked invoice confirmed even though payment is >4 days after issue_date.
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 10), 0)
        self.assertEqual(self.receipt_row(invoice_id).status, "confirmed")

        # Remittance drift latches to needs_review; detail() still returns the reserved tx via invoice window.
        self.change_transaction("a", "inv-due-plus-3", remittance_information=["ORANGE POLSKA CHANGED"])
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 20), 1)
        self.assertEqual(self.receipt_row(invoice_id).status, "needs_review")
        review_detail = self.receipts.detail("owner", invoice_id)
        review_match = next(item for item in review_detail["candidates"]
                            if item["entry_reference"] == reference_digest("inv-due-plus-3"))
        reconfirmed = self.receipts.confirm("owner", {
            "receipt_id": invoice_id, "expected_version": review_detail["receipt"]["version"],
            "receipt_kind": "purchase", "receipt_date": "2026-11-01", "receipt_time": None,
            "total_amount": "123.45", "currency": "PLN", "payment_method": "unknown", "lines": None,
            "link": {
                "account_id": review_match["account_id"],
                "entry_reference": review_match["entry_reference"],
                "candidate_token": review_match["candidate_token"],
            },
        })["receipt"]
        self.assertEqual((reconfirmed["status"], reconfirmed["linked"]), ("confirmed", True))

        # Invoice without due_date or with due_date < issue_date uses [issue_date - 1, issue_date + 14].
        self.add_transaction("a", "nodue-in", amount="55.00", booking_date="2026-11-15", value_date=None)
        self.add_transaction("a", "nodue-out", amount="55.00", booking_date="2026-11-16", value_date=None)
        no_due = self._seed_imported(document_kind="invoice", issue_date="2026-11-01", due_date=None, total="55.00")
        early_due = self._seed_imported(
            document_kind="invoice", issue_date="2026-11-01", due_date="2026-10-25", total="55.00",
        )
        for identifier in (no_due, early_due):
            refs = {item["entry_reference"] for item in self.receipts.detail("owner", identifier)["candidates"]}
            self.assertEqual(refs, {reference_digest("nodue-in")})

    def test_imported_payment_confirmation_keeps_plus_minus_four_days_and_links_with_omitted_fields(self):
        self.add_transaction("a", "pc-minus-5", amount="77.00", booking_date="2026-11-05", value_date=None)
        self.add_transaction("a", "pc-minus-4", amount="77.00", booking_date="2026-11-06", value_date=None)
        self.add_transaction("a", "pc-plus-4", amount="77.00", booking_date="2026-11-14", value_date=None,
                             remittance_information=["PRZYKLADOWY SKLEP"])
        self.add_transaction("a", "pc-plus-5", amount="77.00", booking_date="2026-11-15", value_date=None)
        payment_id = self._seed_imported(
            document_kind="payment_confirmation", issue_date="2026-11-10", due_date=None,
            total="77.00", shop="PRZYKLADOWY SKLEP", nip=None, with_line=True,
        )
        detail = self.receipts.detail("owner", payment_id)
        candidates = detail["candidates"]
        self.assertEqual([item["entry_reference"] for item in candidates], [
            reference_digest("pc-plus-4"),
            reference_digest("pc-minus-4"),
        ])
        self.assertTrue(candidates[0]["merchant_token_hit"])
        self.assertFalse(candidates[1]["merchant_token_hit"])

        # Out-of-window ±5 transactions are also rejected by _verify_link.
        cleaned_pc = validate_confirm({
            "receipt_id": payment_id, "expected_version": 1, "receipt_kind": "purchase",
            "receipt_date": "2026-11-10", "receipt_time": None, "total_amount": "77.00",
            "currency": "PLN", "payment_method": "unknown", "link": None,
        })
        cleaned_pc["document_kind"] = "payment_confirmation"
        cleaned_pc["due_date"] = None
        with self.sessions() as session:
            for bad_ref in ("pc-minus-5", "pc-plus-5"):
                with self.subTest(bad_ref=bad_ref), self.assertRaises(ReceiptError):
                    self.receipts._verify_link(
                        session, "owner", cleaned_pc,
                        {"account_id": self.accounts["a"], "entry_reference": bad_ref}, payment_id,
                    )

        # Confirm omitting lines, raw_shop_name, raw_nip links cleanly using the candidate issued while imported.
        chosen = candidates[0]
        saved = self.receipts.confirm("owner", {
            "receipt_id": payment_id, "expected_version": 1, "receipt_kind": "purchase",
            "receipt_date": "2026-11-10", "receipt_time": None, "total_amount": "77.00",
            "currency": "PLN", "payment_method": "unknown",
            "link": {
                "account_id": chosen["account_id"],
                "entry_reference": chosen["entry_reference"],
                "candidate_token": chosen["candidate_token"],
            },
        })["receipt"]
        self.assertEqual((saved["status"], saved["linked"], saved["raw_shop_name"]),
                         ("confirmed", True, "PRZYKLADOWY SKLEP"))
        self.assertEqual(saved["linked_entry_ref"], reference_digest("pc-plus-4"))


class ReceiptRetentionSweepTests(fixture.unittest.IsolatedAsyncioTestCase):
    async def test_sweep_calls_retain_expired_when_flag_is_off(self):
        from aiohttp import web as aio_web

        started = asyncio.Event()

        async def settled(function):
            self.assertIs(function, receipt_api.retain_expired)
            started.set()
            return {"drafts_purged": 0, "raw_texts_purged": 0}

        app = aio_web.Application()
        environ = os.environ.copy()
        environ.pop(receipt_api.FLAG, None)
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch("hestia.budget.api._secret", side_effect=AssertionError("browser secret")), \
                mock.patch("hestia.budget.api._settled_thread", settled):
            receipt_api.register(app, lambda *_args, **_kwargs: None, auth_enabled=True, session_secret=None,
                                 login_limiter=auth.LoginLimiter())
            context = app.cleanup_ctx[-1](app).__aiter__()
            await context.__anext__()
            await asyncio.wait_for(started.wait(), 2)
            with self.assertRaises(StopAsyncIteration):
                await context.__anext__()

    async def test_sweep_reraises_when_the_task_is_cancelling(self):
        from aiohttp import web as aio_web

        async def settled(_function):
            asyncio.current_task().cancel()
            raise asyncio.CancelledError()

        app = aio_web.Application()
        with mock.patch("hestia.budget.api._settled_thread", settled):
            receipt_api.register(app, lambda *_args, **_kwargs: None, auth_enabled=True, session_secret=None,
                                 login_limiter=auth.LoginLimiter())
            context = app.cleanup_ctx[-1](app).__aiter__()
            await context.__anext__()
            await asyncio.sleep(0)
            with self.assertRaises(StopAsyncIteration):
                await context.__anext__()

    async def test_sweep_continues_after_a_failed_pass(self):
        from aiohttp import web as aio_web

        started = asyncio.Event()
        calls = {"n": 0}

        async def settled(_function):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("sweep failed")
            started.set()
            await asyncio.Event().wait()

        async def quick_sleep(_seconds):
            return None

        app = aio_web.Application()
        with mock.patch("hestia.budget.receipt_api.asyncio.sleep", quick_sleep), \
                mock.patch("hestia.budget.api._settled_thread", settled):
            receipt_api.register(app, lambda *_args, **_kwargs: None, auth_enabled=True, session_secret=None,
                                 login_limiter=auth.LoginLimiter())
            context = app.cleanup_ctx[-1](app).__aiter__()
            await context.__anext__()
            await asyncio.wait_for(started.wait(), 2)
            with self.assertRaises(StopAsyncIteration):
                await context.__anext__()
        self.assertGreaterEqual(calls["n"], 2)


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
        # HTTP confirm omitting `lines` and header fields preserves stored values cleanly.
        omit_body = confirm_values(parsed, expected_version=saved["version"], raw_shop_name="BIEDRONKA HTTP 2")
        omit_body.pop("lines")
        http_omitted = await self.post("confirm", omit_body)
        self.assertEqual(http_omitted.status, 200, await http_omitted.text())
        saved = (await http_omitted.json())["receipt"]
        self.assertEqual(saved["raw_shop_name"], "BIEDRONKA HTTP 2")
        self.assertEqual(len(saved["lines"]), 1)
        self.assertEqual(saved["lines"][0]["raw_name"], PRIVATE_ITEM)
        keep_shop_body = confirm_values(parsed, expected_version=saved["version"])
        keep_shop_body.pop("raw_shop_name")
        keep_shop_body.pop("raw_nip")
        keep_shop_body.pop("lines")
        http_kept_shop = await self.post("confirm", keep_shop_body)
        self.assertEqual(http_kept_shop.status, 200, await http_kept_shop.text())
        saved = (await http_kept_shop.json())["receipt"]
        self.assertEqual(saved["raw_shop_name"], "BIEDRONKA HTTP 2")
        http_null = await self.post("confirm", confirm_values(
            parsed, expected_version=saved["version"], raw_shop_name="BIEDRONKA HTTP 3", lines=None))
        self.assertEqual(http_null.status, 200, await http_null.text())
        saved = (await http_null.json())["receipt"]
        self.assertEqual(saved["lines"][0]["raw_name"], PRIVATE_ITEM)
        self.assertNotIn("raw_ocr_text", saved)
        self.assertIsNone(saved["raw_text_expires_at"])
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
        invalid_limit = await self.post("list", {"limit": 0})
        self.assertEqual(invalid_limit.status, 400)
        self.assertEqual((await invalid_limit.json())["error"], "budget_receipt_invalid_request")
        seen = {"n": 0}

        def enabled():
            seen["n"] += 1
            return seen["n"] == 1

        with mock.patch.object(receipt_api, "enabled", enabled):
            disabled = await self.post("list", {})
        self.assertEqual(disabled.status, 404)
        self.assertEqual((await disabled.json())["error"], "budget_receipts_disabled")
        self.assertGreaterEqual(seen["n"], 2)
        for params in ([("receipt_id", "a"), ("extra", "1")], [("receipt_id", "a"), ("receipt_id", "b")]):
            rejected = await self.client.get(receipt_api.PREFIX + "/detail", params=params, headers=self.headers())
            self.assertEqual(rejected.status, 400)
            self.assertEqual((await rejected.json())["error"], "budget_receipt_invalid_request")

    async def test_http_imported_invoice_candidates_and_confirm_link(self):
        import uuid
        invoice_id = str(uuid.uuid4())
        now = web._now()
        with self.sessions.begin() as session:
            session.add(db.BudgetConnection(
                id="conn-http", owner="alice", application_id="app", bank_name="Bank", country="PL",
                enabled=True, next_attempt=0, failures=0,
            ))
            session.flush()
            session.add(db.BudgetAccount(
                id="acc-http", connection_id="conn-http", owner="alice", identification_hash="acc",
                enabled=True, balances_json="{}",
            ))
            session.flush()
            session.add(db.BudgetTransaction(
                account_id="acc-http", entry_reference="tx-due-3",
                payload_json=json.dumps({
                    "entry_reference": "tx-due-3", "amount": "150", "currency": "PLN",
                    "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-23",
                    "value_date": None, "transaction_date": None,
                    "remittance_information": ["RESPECT ENERGY FV"],
                }),
                observed_at=now, source="api", api_observed_at=now, uncertain=False,
            ))
            session.add(db.BudgetReceipt(
                id=invoice_id, owner="alice", raw_shop_name="Respect Energy", raw_nip="7791011327",
                receipt_kind="purchase", receipt_date="2026-11-01", due_date="2026-11-20",
                total_amount="150", currency="PLN", payment_method=None, status="imported",
                source_kind="email", document_kind="invoice", import_expires_at=now + 86400,
                version=1, created_at=now, updated_at=now,
            ))
        headers = {**self.headers(origin=True), "X-Hestia-Contract-Version": "0.40.0"}
        detail_res = await self.get("detail", invoice_id, headers=headers)
        self.assertEqual(detail_res.status, 200, await detail_res.text())
        detail_body = await detail_res.json()
        api_contract.BudgetReceiptDetailResult.model_validate(detail_body)
        self.assertEqual(detail_body["receipt"]["status"], "imported")
        self.assertEqual(len(detail_body["candidates"]), 1)
        candidate = detail_body["candidates"][0]
        self.assertTrue(candidate["merchant_token_hit"])
        confirm_res = await self.post("confirm", {
            "receipt_id": invoice_id, "expected_version": 1, "receipt_kind": "purchase",
            "receipt_date": "2026-11-01", "receipt_time": None, "total_amount": "150",
            "currency": "PLN", "payment_method": "unknown",
            "lines": [{
                "line_kind": "other", "raw_name": "Respect Energy", "ptu_code": None, "quantity": "1",
                "raw_unit": None, "unit_price_raw": None, "gross_line_total": "150",
                "discount_amount": "0", "net_line_total": "150",
            }],
            "link": {
                "account_id": candidate["account_id"],
                "entry_reference": candidate["entry_reference"],
                "candidate_token": candidate["candidate_token"],
            },
        }, headers=headers)
        self.assertEqual(confirm_res.status, 200, await confirm_res.text())
        confirmed = await confirm_res.json()
        api_contract.BudgetReceiptConfirmResult.model_validate(confirmed)
        self.assertEqual((confirmed["receipt"]["status"], confirmed["receipt"]["linked"]), ("confirmed", True))


if __name__ == "__main__":
    import unittest
    unittest.main()
