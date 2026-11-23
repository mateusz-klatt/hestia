"""Branches the receipt modules take on bad input, quotas and defensive checks."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest import mock
import unittest

from hestia import db
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget import receipt_api, receipt_parser, receipt_reconcile, receipt_store
from hestia.budget.receipt_parser import (MAX_RECEIPT_LINES, MAX_TEXT_LINES, ReceiptError, merchant_tokens,
                                         parse_receipt, redact_pan, valid_nip, validate_confirm)
from hestia.budget.receipt_store import MAX_DRAFTS, BudgetReceiptStore
from hestia.budget.views import BudgetUsersBackendRequired
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_receipts import confirm_values, receipt_text


def _line(**changes):
    values = {
        "line_kind": "item", "raw_name": "MLEKO", "ptu_code": "A", "quantity": "1", "raw_unit": None,
        "unit_price_raw": "100", "gross_line_total": "100", "discount_amount": "0", "net_line_total": "100",
    }
    values.update(changes)
    return values


class ParserEdgeTests(unittest.TestCase):
    def test_pesel_iban_and_normaliser_edges(self):
        self.assertFalse(receipt_parser._pesel(""))
        self.assertFalse(receipt_parser._pesel("4405140135"))
        self.assertFalse(receipt_parser._pesel("44051401358"))
        self.assertTrue(receipt_parser._pesel("44051401359"))
        self.assertFalse(receipt_parser._iban_shape("PL6110901014"))
        self.assertFalse(receipt_parser._iban_shape("GB" + "29" + "A" * 40))
        self.assertFalse(receipt_parser._iban_shape("1B29NWBK60161331926819"))
        self.assertFalse(receipt_parser._iban_shape("G129NWBK60161331926819"))
        self.assertFalse(receipt_parser._iban_shape("GBX9NWBK60161331926819"))
        self.assertFalse(receipt_parser._iban_shape("GB2XNWBK60161331926819"))
        self.assertFalse(receipt_parser._iban_shape("GB29NWBK6016133192681!"))
        self.assertTrue(receipt_parser._iban_shape("PL61109010140000071219812874"))
        self.assertTrue(receipt_parser._iban_shape("PL" + "0" * 26))
        self.assertTrue(receipt_parser._iban_shape("GB29NWBK60161331926819"))
        self.assertTrue(receipt_parser._iban_checksum("GB29NWBK60161331926819"))
        self.assertFalse(receipt_parser._iban_shape("ML12ABCD1234567"))
        self.assertFalse(receipt_parser._iban_checksum("ML12ABCD1234567"))
        for error in (ValueError, TypeError):
            with mock.patch("hestia.budget.receipt_parser.unicodedata.digit", side_effect=error):
                self.assertEqual(receipt_parser._normalize_receipt_text("١"), "")
        self.assertEqual(redact_pan("44051401359"), "[usunięto]")
        self.assertEqual(redact_pan("44051401358"), "44051401358")
        self.assertEqual(redact_pan("440514\n01359"), "[usunięto]\n[usunięto]")
        self.assertEqual(redact_pan("PESEL 44051401359"), "[usunięto]")
        self.assertEqual(redact_pan("PL61109010140000071219812874"), "[usunięto]")
        self.assertEqual(redact_pan("PL61 1090 1014 0000 0712 1981 2874"), "[usunięto]")
        self.assertEqual(redact_pan("PL61109010140000071219812874!"), "[usunięto]")
        self.assertEqual(redact_pan("GB29 NWBK 6016 1331 9268 19"), "[usunięto]")
        self.assertEqual(redact_pan("gb29 nwbk 6016 1331 9268 19"), "[usunięto]")
        self.assertEqual(redact_pan("GB29 NWBK\n6016 1331 9268 19"), "[usunięto]\n[usunięto]")
        self.assertEqual(redact_pan("NL91 ABNA 0417 1643 00"), "[usunięto]")
        self.assertEqual(redact_pan("NL91 ABNA 0417 1643 00EXTRA"), "NL91 ABNA 0417 1643 00EXTRA")
        self.assertEqual(redact_pan("NL91 ABNA 0417 1643 00\n"), "[usunięto]\n")
        self.assertEqual(redact_pan("PL61!123"), "PL61!123")
        self.assertEqual(redact_pan("PL6110901014"), "PL6110901014")
        glued = "GB29NWBK6016X1331X9268X19EXTRAEXTRAEXTRA"
        self.assertEqual(redact_pan(glued), glued)
        self.assertEqual(redact_pan("A12345"), "A12345")
        self.assertEqual(redact_pan("A1234567890123456"), "[usunięto]")
        self.assertEqual(redact_pan("ABXX12345678901"), "ABXX12345678901")
        self.assertEqual(redact_pan("ABCX12345678901"), "ABCX12345678901")
        self.assertEqual(redact_pan("***1****"), "***1****")
        self.assertEqual(redact_pan("1GB29NWBK6016X1331"), "1GB29NWBK6016X1331")
        with mock.patch.object(receipt_parser, "redact_pan", return_value=""):
            with self.assertRaises(ReceiptError):
                receipt_parser._clean_text("SKLEP", limit=16)
        with mock.patch.object(receipt_parser, "redact_pan", return_value="A" * 20):
            with self.assertRaises(ReceiptError):
                receipt_parser._clean_text("SKLEP", limit=16)
        with self.assertRaises(ReceiptError):
            receipt_parser._bounded_money("99999999.9999")
        self.assertEqual(receipt_parser._bounded_money("999999999.99"), "999999999.99")

    def test_nip_scan_matches_the_bounded_span(self):
        scan = receipt_parser._nip_candidate
        self.assertEqual(scan("NIP: 7791011327"), "7791011327")
        self.assertEqual(scan("NIP:7791011327"), "7791011327")
        self.assertEqual(scan("NIP:\t779-101-13-27 tail"), "7791011327")
        self.assertEqual(scan("NIP 7791011327 done"), "7791011327")
        self.assertIsNone(scan("NIP"))
        self.assertIsNone(scan("NIP:"))
        self.assertIsNone(scan("NIP abc"))
        self.assertIsNone(scan("NIP 7791011327X"))
        self.assertIsNone(scan("NIP 7791011327_"))
        self.assertEqual(scan("NIP 7791011327ł"), "7791011327")
        self.assertIsNone(scan("NIP " + "1" * 20))
        self.assertFalse(receipt_parser._ascii_word(" "))
        self.assertTrue(receipt_parser._ascii_word("A"))
        self.assertTrue(receipt_parser._ascii_word("_"))
        self.assertFalse(receipt_parser._ascii_word("ł"))

    def test_parse_validation_and_defensive_text_checks(self):
        self.assertIn("market", merchant_tokens("ab polska market", None))
        self.assertNotIn("polska", merchant_tokens("ab polska market", None))
        self.assertFalse(valid_nip(None))
        self.assertFalse(valid_nip("123"))
        bare = parse_receipt("2 szt 5,00 A\nSUMA PLN 5,00\n")
        self.assertEqual(bare["lines"][0]["raw_name"], "szt")
        self.assertEqual(bare["lines"][0]["raw_unit"], "szt")
        calculated = parse_receipt("MLEKO\n2 x 3,50=7,00 A\nSUMA PLN 7,00\n")
        self.assertEqual(calculated["lines"][0]["raw_name"], "MLEKO")
        self.assertEqual(calculated["lines"][0]["gross_line_total"], "7")
        undated = parse_receipt("SKLEP\n2026-02-30\nSUMA PLN\nKONIEC\n")
        self.assertIsNone(undated["receipt_date"])
        self.assertIsNone(undated["total_amount"])
        items = "\n".join(f"TOWAR{index} 1 x 1,00 1,00 A" for index in range(MAX_RECEIPT_LINES + 1))
        with self.assertRaises(ReceiptError):
            parse_receipt("SKLEP\n" + items + "\n")
        with mock.patch.object(receipt_parser, "redact_pan", return_value="\n" * MAX_TEXT_LINES):
            with self.assertRaises(ReceiptError):
                parse_receipt("ok")
        for value in (None, b"text", "\ud800"):
            with self.subTest(value=type(value).__name__), self.assertRaises(ReceiptError):
                parse_receipt(value)
        with mock.patch.object(receipt_parser, "_normalize_receipt_text", return_value="\ud800"):
            with self.assertRaises(ReceiptError):
                receipt_parser._receipt_text("ok")
        with self.assertRaises(ReceiptError):
            receipt_parser._bounded_money(1)
        with self.assertRaises(ReceiptError):
            receipt_parser._bounded_money("not-money")
        with self.assertRaises(ReceiptError) as mismatch:
            receipt_parser._bounded_money("not-money", mismatch=True)
        self.assertEqual(mismatch.exception.code, "budget_receipt_total_mismatch")
        self.assertIsNone(receipt_parser._clean_text("", limit=16, optional=True))
        self.assertIsNone(receipt_parser._clean_text(None, limit=16, optional=True))
        with self.assertRaises(ReceiptError):
            receipt_parser._clean_text(None, limit=16)
        with self.assertRaises(ReceiptError):
            receipt_parser._confirm_date("20261130")

    def test_confirm_rejects_each_invalid_field(self):
        base = {
            "receipt_id": str(uuid.uuid4()), "expected_version": 1, "raw_shop_name": "SKLEP",
            "raw_nip": "7791011327", "receipt_kind": "purchase", "receipt_date": "2026-11-30",
            "receipt_time": "12:34", "total_amount": "100", "currency": "PLN", "payment_method": "cash",
            "lines": [_line()], "link": None,
        }

        def check(**changes):
            values = {**base, "lines": [_line(**(changes.pop("line", {})))], **changes}
            with self.assertRaises(ReceiptError):
                validate_confirm(values)

        check(receipt_id="not-a-uuid")
        check(expected_version=0)
        check(receipt_kind="other")
        check(payment_method="wire")
        check(currency="pln")
        check(raw_nip="1234567890")
        check(receipt_time="25:99")
        check(receipt_time=1)
        check(receipt_date="2026-02-30")
        check(receipt_date=20261130)
        check(raw_shop_name="SKLEP\x00")
        check(raw_shop_name="\ufeffSKLEP")
        check(raw_shop_name=1)
        check(raw_shop_name="   ")
        check(raw_shop_name="A" * 129)
        check(lines="nope")
        check(lines=["nope"])
        check(lines=[{key: _line()[key] for key in _line() if key != "quantity"}])
        check(lines=[_line() for _ in range(MAX_RECEIPT_LINES + 1)])
        check(line={"line_kind": "fee"})
        check(line={"ptu_code": "Z"})
        check(line={"raw_name": 1})
        for bad in ({}, [], None):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(ReceiptError):
                validate_confirm(bad)
        other = validate_confirm({**base, "lines": [_line(line_kind="other", unit_price_raw=None, ptu_code=None)]})
        self.assertEqual(other["lines"][0]["line_kind"], "other")
        self.assertIsNone(other["lines"][0]["unit_price_raw"])
        blank = validate_confirm({**base, "raw_shop_name": "", "raw_nip": "", "receipt_time": ""})
        self.assertEqual((blank["raw_shop_name"], blank["raw_nip"], blank["receipt_time"]), (None, None, None))


class ReconcileEdgeTests(AnalyticsCase):
    def test_json_dates_digests_and_evidence_reject_bad_input(self):
        with self.assertRaises(ValueError):
            receipt_reconcile._json('{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            receipt_reconcile._json('{"a": NaN}')
        self.assertIsNone(receipt_reconcile._bound_date("2026-02-30"))
        self.assertIsNone(receipt_reconcile._bound_date("20261130"))
        with mock.patch("hestia.budget.receipt_reconcile.json.dumps", side_effect=ValueError):
            with self.assertRaises(AnalyticsError):
                receipt_reconcile._remittance_digest({"remittance_information": ["a"]})
        with self.assertRaises(AnalyticsError):
            receipt_reconcile.link_evidence("owner", "a", "ref", {}, "shop", None, None, "zz")
        with mock.patch.object(receipt_reconcile, "_EVIDENCE_KEYS", receipt_reconcile._EVIDENCE_KEYS + ("extra",)):
            with self.assertRaises(AnalyticsError):
                receipt_reconcile.link_evidence("owner", "a", "ref", {}, "shop", None, None, None)
        with self.assertRaises(AnalyticsError):
            receipt_reconcile.evidence_json({"amount": float("nan")})
        with self.assertRaises(AnalyticsError):
            receipt_reconcile.evidence_json({"note": "x" * 5000})
        with mock.patch("hestia.budget.receipt_reconcile.json.dumps", return_value="żółć"):
            with self.assertRaises(AnalyticsError):
                receipt_reconcile.evidence_json({"note": "ok"})
        pending = SimpleNamespace(uncertain=False, payload_json='{"status": "PDNG"}')
        self.assertIsNone(receipt_reconcile._fingerprint(pending))
        broken = SimpleNamespace(uncertain=False, payload_json="{")
        self.assertIsNone(receipt_reconcile._fingerprint(broken))
        with self.sessions.begin() as session:
            for bad in (float("nan"), float("inf"), -1, "0", 10**400):
                with self.subTest(bad=bad), self.assertRaises(AnalyticsError):
                    receipt_reconcile.reconcile_receipt_links(session, "owner", bad)

    def test_creditor_name_ignores_bad_metadata_and_reconcile_survives_evidence_errors(self):
        account = self.accounts["a"]
        fingerprint = self.fingerprint()
        with self.sessions.begin() as session:
            session.add(db.BudgetTransactionMetadata(
                account_id=account, entry_reference="debit", owner="owner", ledger_fingerprint=fingerprint,
                payload_json="{bad", source="api", observed_at=self.now, version=1))
            self.assertIsNone(receipt_reconcile.creditor_name(session, "owner", account, "debit", fingerprint))
        self.metadata("b", "credit", {"creditor_name": ""})
        with self.sessions() as session:
            row = session.get(db.BudgetTransactionMetadata, (self.accounts["b"], "credit"))
            fingerprint = self.fingerprint("b", "credit")
            for raw in ('{"creditor_name": ""}', '{"creditor_name": 1}', '{"creditor_name": "' + ("x" * 257) + '"}', "[]"):
                row.payload_json = raw
                session.flush()
                self.assertIsNone(receipt_reconcile.creditor_name(
                    session, "owner", self.accounts["b"], "credit", fingerprint))
        receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only")
        parsed = receipts.parse_text("owner", receipt_text())
        receipts.confirm("owner", confirm_values(parsed, link={
            "account_id": self.accounts["a"], "entry_reference": "debit"}))
        with mock.patch("hestia.budget.receipt_reconcile.evidence_json", side_effect=AnalyticsError()):
            with self.sessions.begin() as session:
                changed = receipt_reconcile.reconcile_receipt_links(session, "owner", self.now)
        self.assertEqual(changed, 1)


class StoreEdgeTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only")

    def draft(self, **kwargs):
        return self.receipts.parse_text("owner", receipt_text(**kwargs))

    def add_raw(self, reference, raw):
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(
                account_id=self.accounts["a"], entry_reference=reference, payload_json=raw,
                observed_at=self.now, source="api", api_observed_at=self.now, uncertain=False))

    def test_shop_resolution_null_total_quota_and_clock(self):
        shop_id = str(uuid.uuid4())
        with self.sessions.begin() as session:
            session.add(db.BudgetShop(
                id=shop_id, owner="owner", name="BIEDRONKA", chain_key="biedronka", nip="7791011327",
                merchant_tokens="[]", version=1, created_at=self.now, updated_at=self.now))
        parsed = self.draft()
        self.assertEqual(parsed["draft"]["shop_id"], shop_id)
        chain_id = str(uuid.uuid4())
        with self.sessions.begin() as session:
            session.add(db.BudgetShop(
                id=chain_id, owner="other", name="LIDL", chain_key="lidl", nip=None,
                merchant_tokens="[]", version=1, created_at=self.now, updated_at=self.now))
        chain_text = "LIDL\n2026-11-30 12:34\nMLEKO 1 x 5,00 5,00 A\nSUMA PLN 5,00\n"
        chain = self.receipts.parse_text("other", chain_text)
        self.assertEqual(chain["draft"]["shop_id"], chain_id)
        blank = self.receipts.parse_text("owner", "SKLEP OSOBNY\nMLEKO 1 x 5,00 5,00 A\n")
        self.assertIsNone(self.receipts.detail("owner", blank["receipt_id"])["receipt"]["math_delta"])
        with self.assertRaises(ReceiptError) as missing:
            self.receipts.detail("owner", "not-a-uuid")
        self.assertEqual(missing.exception.status, 404)
        for _ in range(MAX_DRAFTS - 2):
            self.draft(total="7")
        with self.assertRaises(ReceiptError) as quota:
            self.draft(total="7")
        self.assertEqual((quota.exception.code, quota.exception.status), ("budget_receipt_quota_exceeded", 413))
        self.receipts.clock = lambda: float("nan")
        with self.assertRaises(ReceiptError) as clock:
            self.receipts.detail("owner", parsed["receipt_id"])
        self.assertEqual(clock.exception.status, 503)

    def test_candidate_scan_skips_bad_payloads_and_non_list_remittance(self):
        with self.assertRaises(ValueError):
            receipt_store._json('{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            receipt_store._json('{"a": NaN}')
        self.assertFalse(receipt_store._valid_date("2026-02-30"))
        self.assertIsNone(receipt_store._date_distance({}, "2026-02-30"))
        self.add_raw("duplicate", '{"status": "BOOK", "status": "BOOK"}')
        self.add_raw("nan", '{"status": NaN}')
        self.add_transaction("a", "bad-amount", amount=None, booking_date="2026-11-30")
        self.add_transaction("a", "bad-book", amount="100", booking_date="2026-02-30",
                             value_date="2026-11-30", transaction_date="2026-11-30")
        self.add_transaction("a", "text-remit", amount="100", booking_date="2026-11-30",
                             remittance_information="Synthetic")
        preview = SimpleNamespace(
            total_amount="not-money", currency="PLN", receipt_date="2026-11-30", payment_method="card",
            receipt_kind="purchase", raw_shop_name="SKLEP", raw_nip=None)
        with self.sessions() as session:
            self.assertEqual(self.receipts._candidates(session, "owner", preview, {}), [])
        parsed = self.draft()
        text_remit = next(item for item in parsed["candidates"] if item["entry_reference"] == hashlib.sha256(b"text-remit").hexdigest())
        self.assertNotIn("remittance_information", text_remit)
        self.assertNotIn("Synthetic", json.dumps(text_remit))
        with mock.patch("hestia.budget.receipt_store.link_evidence", side_effect=AnalyticsError()):
            self.assertEqual(self.draft()["candidates"], [])
        with mock.patch("hestia.budget.receipt_store.base64.urlsafe_b64encode", return_value=b"***"):
            with self.assertRaises(ReceiptError):
                self.receipts._candidate_token({"owner": "owner"}, "account", 1)
        from hestia.budget.receipt_reconcile import link_evidence
        evidence = link_evidence("owner", "account", "ref", {}, None, None, None, None)
        with self.assertRaises(AnalyticsError):
            self.receipts._candidate_token(evidence, "account", "not-a-row")
        with mock.patch("hestia.budget.receipt_store.base64.urlsafe_b64encode", return_value=b"A" * 4097):
            with self.assertRaises(AnalyticsError):
                self.receipts._candidate_token(evidence, "account", 1)

    def test_maximum_unicode_reference_is_a_candidate_and_confirms(self):
        reference = "Ż" * 1024
        self.add_transaction("a", reference, amount="100", booking_date="2026-11-30")
        parsed = self.draft()
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()
        candidate = next(item for item in parsed["candidates"] if item["entry_reference"] == digest)
        self.assertLessEqual(len(candidate["candidate_token"]), 4096)
        self.assertNotIn(reference, candidate["candidate_token"])
        saved = self.receipts.confirm("owner", confirm_values(parsed, link={
            "account_id": candidate["account_id"], "entry_reference": digest,
            "candidate_token": candidate["candidate_token"],
        }))["receipt"]
        self.assertTrue(saved["linked"])
        self.assertEqual(saved["linked_entry_ref"], digest)
        self.assertNotIn(reference, json.dumps(saved, ensure_ascii=False))
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, parsed["receipt_id"]).linked_entry_ref, reference)

    def test_sealed_token_rejects_a_non_string_pair_and_a_broken_evidence_rebuild(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        payload = json.dumps({"a": 1, "i": 1, "m": "ab" * 32}, separators=(",", ":")).encode("ascii")
        key = self.receipts._token_key
        nonce = b"0123456789ab"
        blob = nonce + AESGCM(key).encrypt(nonce, payload, None)
        token = base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")
        self.assertRaises(ReceiptError, self.receipts._open_candidate_token, token)

        parsed = self.draft()
        digest = hashlib.sha256(b"debit").hexdigest()
        candidate = next(item for item in parsed["candidates"] if item["entry_reference"] == digest)
        link = {"account_id": candidate["account_id"], "entry_reference": digest,
                "candidate_token": candidate["candidate_token"]}
        with mock.patch("hestia.budget.receipt_store.link_evidence", side_effect=AnalyticsError()):
            with self.assertRaises(ReceiptError) as stale:
                self.receipts.confirm("owner", confirm_values(parsed, link=link))
        self.assertEqual((stale.exception.code, stale.exception.status), ("budget_receipt_stale_source", 409))
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, parsed["receipt_id"]).status, "draft")

    def test_verify_and_confirm_reject_bad_links(self):
        parsed = self.draft()
        cleaned = validate_confirm(confirm_values(parsed))
        account = self.accounts["a"]

        def verify(reference):
            with self.sessions() as session:
                self.receipts._verify_link(session, "owner", cleaned, {
                    "account_id": account, "entry_reference": reference,
                }, parsed["receipt_id"])

        with self.assertRaises(ReceiptError):
            verify("missing-ref")
        self.add_raw("bad-json", "{")
        with self.assertRaises(ReceiptError):
            verify("bad-json")
        self.add_transaction("a", "far", amount="100", booking_date="2020-01-01",
                             value_date="2020-01-01", transaction_date="2020-01-01")
        with self.assertRaises(ReceiptError):
            verify("far")
        self.add_transaction("a", "unsure", amount="100", booking_date="2026-11-30")
        self.change_transaction("a", "unsure", uncertain=True)
        with self.assertRaises(ReceiptError):
            verify("unsure")
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, account).enabled = False
        with self.assertRaises(ReceiptError):
            verify("debit")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAccount, account)
            row.enabled = True
            session.get(db.BudgetConnection, row.connection_id).owner = "other"
        with self.assertRaises(ReceiptError):
            verify("debit")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAccount, account)
            session.get(db.BudgetConnection, row.connection_id).owner = "owner"
        self.decide()
        with self.assertRaises(ReceiptError):
            verify("debit")
        short = confirm_values(parsed, link={"account_id": account, "entry_reference": "debit",
                                              "candidate_token": "short"})
        with self.assertRaises(ReceiptError):
            self.receipts.confirm("owner", short)
        forged = confirm_values(parsed, link={"account_id": account, "entry_reference": "debit",
                                               "candidate_token": "a" * 43})
        with mock.patch.object(BudgetReceiptStore, "_named_transaction", side_effect=AssertionError("lookup")):
            with self.assertRaises(ReceiptError) as forged_error:
                self.receipts.confirm("owner", forged)
        self.assertEqual(forged_error.exception.status, 400)

    def test_confirm_without_raw_text_still_sets_the_image_deadline(self):
        parsed = self.draft(payment="GOTOWKA")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceipt, parsed["receipt_id"])
            row.raw_ocr_text = None
            row.raw_text_expires_at = None
        saved = self.receipts.confirm("owner", confirm_values(parsed))["receipt"]
        self.assertIsNone(saved["raw_text_expires_at"])
        self.assertIsNotNone(saved["image_expires_at"])

    def test_cursors_filters_search_and_delete(self):
        parsed = self.draft()
        identifier = parsed["receipt_id"]

        def sign(payload):
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")
            signature = hmac.new(self.receipts._receipt_cursor_key, raw, hashlib.sha256).digest()
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            signed = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
            return encoded + "." + signed

        context = "ctx"
        future = self.now + 100
        valid_id = str(uuid.uuid4())
        bodies = [
            [],
            {"owner": "owner"},
            {"owner": "other", "context": context, "last_date": None, "last_id": valid_id, "expires_at": future},
            {"owner": "owner", "context": "nope", "last_date": None, "last_id": valid_id, "expires_at": future},
            {"owner": "owner", "context": context, "last_date": None, "last_id": valid_id, "expires_at": True},
            {"owner": "owner", "context": context, "last_date": None, "last_id": valid_id, "expires_at": 1e309},
            {"owner": "owner", "context": context, "last_date": None, "last_id": valid_id, "expires_at": 1},
            {"owner": "owner", "context": context, "last_date": None, "last_id": "nope", "expires_at": future},
            {"owner": "owner", "context": context, "last_date": "2026-02-30", "last_id": valid_id, "expires_at": future},
        ]
        for payload in bodies:
            raw = json.dumps(payload, separators=(",", ":"), allow_nan=True)
            if isinstance(payload, dict) and payload.get("expires_at") == 1e309:
                raw = raw.replace("Infinity", "1e309")
            encoded = base64.urlsafe_b64encode(raw.encode("ascii")).rstrip(b"=").decode("ascii")
            signature = hmac.new(self.receipts._receipt_cursor_key, raw.encode("ascii"), hashlib.sha256).digest()
            signed = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
            with self.subTest(payload=payload), self.assertRaises(ReceiptError):
                self.receipts._decode_cursor("owner", context, encoded + "." + signed)
        good = {"owner": "owner", "context": context, "last_date": "2026-11-30", "last_id": valid_id,
                "expires_at": future}
        self.assertEqual(self.receipts._decode_cursor("owner", context, sign(good)), ("2026-11-30", valid_id))
        signed = sign(good)
        flipped = ("A" if signed[0] != "A" else "B") + signed[1:]
        with self.assertRaises(ReceiptError):
            self.receipts._decode_cursor("owner", context, flipped)
        for cursor in (1, "", "a" * 9000):
            with self.subTest(cursor=type(cursor).__name__), self.assertRaises(ReceiptError):
                self.receipts.list("owner", {"cursor": cursor})
        for values in ({"extra": 1}, {"limit": 0}, {"limit": True}, {"month": "2026-13"}, {"month": "bad"},
                       {"status": "nope"}, {"linked": "yes"}):
            with self.subTest(values=values), self.assertRaises(ReceiptError):
                self.receipts.list("owner", values)
        for values in ({}, {"query": "   "}, {"query": "x" * 129}):
            with self.subTest(values=values), self.assertRaises(ReceiptError):
                self.receipts.search("owner", values)
        self.assertTrue(self.receipts.list("owner", {"status": "draft"})["receipts"])
        self.assertEqual(self.receipts.search("owner", {"query": "zz%_\\nope"})["receipts"], [])
        self.assertEqual(self.receipts.search("owner", {"query": "paragon"})["receipts"], [])
        self.assertEqual(len(self.receipts.search("owner", {"query": "biedronka"})["receipts"]), 1)
        for values in ({"receipt_id": identifier}, {"receipt_id": identifier, "expected_version": 0},
                       {"receipt_id": identifier, "expected_version": True}):
            with self.subTest(values=values), self.assertRaises(ReceiptError):
                self.receipts.delete("owner", values)
        with self.assertRaises(ReceiptError) as conflict:
            self.receipts.delete("owner", {"receipt_id": identifier, "expected_version": 9})
        self.assertEqual(conflict.exception.code, "budget_receipt_conflict")


class ReceiptApiEdgeTests(unittest.TestCase):
    def test_store_refuses_a_non_authoritative_backend(self):
        with mock.patch("hestia.budget.receipt_api.init_db", return_value=(mock.Mock(), mock.Mock())), \
                mock.patch("hestia.budget.receipt_api.store_sql.is_users_db_authoritative", return_value=False):
            with self.assertRaises(BudgetUsersBackendRequired):
                receipt_api._store(b"secret")
            self.assertEqual(receipt_api.retain_expired(), {"drafts_purged": 0, "raw_texts_purged": 0})

    def test_receipt_id_requires_one_query_value(self):
        from multidict import MultiDict

        request = mock.Mock()
        request.query = MultiDict([("other", "1")])
        with self.assertRaises(ReceiptError):
            receipt_api._receipt_id(request)
        request.query = MultiDict([("receipt_id", "a"), ("receipt_id", "b")])
        with self.assertRaises(ReceiptError):
            receipt_api._receipt_id(request)

    def test_image_read_stays_not_found_when_the_store_returns(self):
        store = mock.Mock()
        store.image.return_value = {"ignored": True}
        with mock.patch.object(receipt_api, "_store", return_value=store):
            with self.assertRaises(ReceiptError) as missing:
                receipt_api._run_read("owner", "image", "identifier", b"secret")
        self.assertEqual(missing.exception.code, "budget_receipt_not_found")
        store.image.assert_called_once_with("owner", "identifier")
