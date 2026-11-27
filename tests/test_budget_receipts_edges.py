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
        self.assertEqual(redact_pan("NL91 ABNA 0417 1643 00EXTRA"), "[usunięto]EXTRA")
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

    def test_iban_matching_and_redaction_share_the_oman_country_length(self):
        iban = "OM65001ABCDEFGHIJKLMNOP"
        self.assertEqual(redact_pan(iban), "[usunięto]")
        self.assertEqual(receipt_store._mask_iban_sequences(iban), " " * len(iban))

        # Matching still excludes IBAN-shaped evidence even if the check digits are wrong.
        # This exercises the registry length instead of the store's existing checksum fallback.
        invalid = "OM00" + iban[4:]
        self.assertEqual(redact_pan(invalid), invalid)
        self.assertEqual(receipt_store._mask_iban_sequences(invalid), " " * len(invalid))

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

    def test_parse_selects_the_seller_nip_outside_buyer_sections(self):
        seller, buyer = "1010101016", "1020101012"
        with self.subTest("synthetic checksums"):
            self.assertTrue(valid_nip(seller))
            self.assertTrue(valid_nip(buyer))
        cases = (
            (f"NIP: {seller}\nNIP nabywcy: {buyer}", seller),
            (f"Nabywca:\nKlient Testowy\nNIP: {buyer}\nSprzedawca: Sklep Testowy\nNIP: {seller}", seller),
            (f"Nabywca:\nNIP: {buyer}\nSprzedawca\nSklep Testowy\nNIP: {seller}", seller),
            (f"Nabywca:\nNIP: {buyer}\nSprzedawca NIP: {seller}", seller),
            (f"Nabywca może zwrócić towar\nNIP: {seller}", seller),
            (f"Zwrot zgłasza nabywca, podając NIP: {seller}", seller),
            (f"NIP: {seller} Nabywca: NIP: {buyer}", seller),
            (f"Nabywca: NIP: {buyer} Sprzedawca: NIP: {seller}", seller),
            (f"NIP odbiorcy: {buyer}\nNIP sprzedawcy: {seller}", seller),
            (f"NIP: 1060101017 Nabywca: NIP: {buyer}", None),
            (f"Nabywca NIP: {buyer}", None),
        )
        for party_lines, expected in cases:
            with self.subTest(party_lines=party_lines):
                self.assertEqual(parse_receipt(f"Sklep Testowy\n{party_lines}")["raw_nip"], expected)

        for marker in ("NIP nabywcy:", "NIP odbiorcy", "Nabywca:", "NABYWCA", "Dane nabywcy:", "Kupujący", "Odbiorca:"):
            with self.subTest(marker=marker):
                text = f"Sklep Testowy\n{marker}\nKlient Testowy\nNIP: {buyer}"
                self.assertIsNone(parse_receipt(text)["raw_nip"])
                self.assertEqual(redact_pan(text), text)
                for boundary in ("", "PARAGON FISKALNY", "-------"):
                    self.assertEqual(parse_receipt(f"{text}\n{boundary}\nNIP: {seller}")["raw_nip"], seller)

    def test_gap_under_buyer_heading_keeps_its_nip_in_the_buyer_block(self):
        buyer_block = "Nabywca:\n\nKlient Testowy\nul. Testowa 1\nNIP: 1020101012"
        seller_block = "Sprzedawca:\n\nSklep Testowy\nNIP: 1010101016"
        self.assertEqual(parse_receipt(f"{buyer_block}\n\n{seller_block}")["raw_nip"], "1010101016")
        self.assertIsNone(parse_receipt(buyer_block)["raw_nip"])

    def test_headings_and_seller_prose_keep_unread_buyer_nip_in_its_block(self):
        for line in ("Faktura VAT", "PARAGON FISKALNY", "Pozycje", "Podsumowanie", "Suma PLN",
                     "Sprzedawca udziela gwarancji na towar"):
            with self.subTest(line=line):
                text = f"Nabywca:\nKlient Testowy\n{line}\nNIP: 1020101012"
                self.assertIsNone(parse_receipt(text)["raw_nip"])
                self.assertEqual(parse_receipt(f"{text}\n\nNIP: 1010101016")["raw_nip"], "1010101016")

    def test_explicit_boundaries_end_a_buyer_block_before_its_nip(self):
        for boundary in ("-------", "Sprzedawca:"):
            with self.subTest(boundary=boundary):
                text = f"Nabywca:\nKlient Testowy\n{boundary}\nNIP: 1010101016"
                self.assertEqual(parse_receipt(text)["raw_nip"], "1010101016")

    def test_invalid_nip_does_not_allow_a_blank_to_end_the_buyer_block(self):
        text = "Nabywca:\nNIP: 1060101017\n\nNIP: 1020101012"
        self.assertIsNone(parse_receipt(text)["raw_nip"])

    def test_buyer_nip_with_an_inline_qualifier_allows_a_later_blank_to_end_the_block(self):
        for label in ("NIP nabywcy:", "NIP odbiorcy"):
            with self.subTest(label=label):
                text = f"{label} 1020101012\n\nNIP: 1010101016"
                self.assertEqual(parse_receipt(text)["raw_nip"], "1010101016")

    def test_paired_empty_seller_and_buyer_headings_trust_the_seller_column_first(self):
        text = "Sprzedawca: Nabywca:\nSklep Testowy\nNIP: 1010101016\nKlient Testowy\nNIP: 1020101012"
        self.assertEqual(parse_receipt(text)["raw_nip"], "1010101016")

    def test_redacting_a_buyer_heading_preserves_its_nip_context(self):
        text = "Sklep Testowy\nNabywca: Klient Testowy, karta 5351 0055 5555 5555\nNIP: 1020101012"
        self.assertEqual(redact_pan(text), "Sklep Testowy\n[usunięto]\nNIP: 1020101012")
        self.assertIsNone(parse_receipt(text)["raw_nip"])

    def test_parse_validation_and_defensive_text_checks(self):
        from hestia.budget.receipt_parser import chain_for, merchant_match_groups, merchant_score_groups

        self.assertEqual(merchant_tokens(None, None), frozenset())
        self.assertEqual(merchant_match_groups("LIDLBERG", None), (frozenset({"lidlberg"}),))
        self.assertEqual(
            merchant_match_groups("BIEDRONKA", None),
            (frozenset({"biedronka"}), frozenset({"jeronimo", "martins"})),
        )
        self.assertEqual(
            merchant_score_groups("BIEDRONKA", None),
            (((frozenset({"biedronka"}), frozenset({"jeronimo", "martins"})),), ()),
        )
        self.assertEqual(
            merchant_score_groups("BIEDRONKA EXPRESS", None),
            (((frozenset({"biedronka"}), frozenset({"jeronimo", "martins"})), (frozenset({"express"}),)), ()),
        )
        self.assertEqual(merchant_score_groups("ACME GROCERY", None), (((frozenset({"acme"}),),), ("grocery",)))
        self.assertEqual(merchant_score_groups("GROCERY", None), ((), ("grocery",)))
        self.assertEqual(merchant_score_groups("POLAND ENERGY GROUP", None), ((), ("energy", "group", "poland")))
        self.assertIsNone(chain_for("LIDLBERG", None))
        self.assertIsNone(chain_for("MARTINS AUTO", None))
        self.assertEqual(chain_for("JERONIMO MARTINS POLSKA", None), "biedronka")
        self.assertEqual(merchant_tokens("LIDLBERG", None), frozenset({"lidlberg"}))
        self.assertEqual(merchant_tokens("MARTINS AUTO", None), frozenset({"martins", "auto"}))
        self.assertEqual(merchant_tokens("BIEDRONKA", None), frozenset({"biedronka", "jeronimo", "martins"}))
        self.assertEqual(merchant_tokens("ab polska market piernik", None), frozenset({"piernik"}))
        for generic in ("grocery", "groceries", "grocer", "energy", "energia", "polska", "poland", "services",
                        "service", "group", "store", "shop", "sklep", "market", "online", "pay", "payment",
                        "oplata", "opłata", "sp", "z", "o", "oo", "sa", "spółka", "zoo", "com", "pl", "www",
                        "net", "eu", "p24", "przelewy24", "paypro", "payu", "autopay", "tpay", "paynow", "dotpay",
                        "blik", "bluemedia", "stripe", "paypal", "adyen", "klarna", "eservice", "elavon",
                        "fiserv", "polcard", "ecard", "cashbill", "hotpay", "imoje",
                        "krakow", "kraków", "warszawa", "wrocław", "łódź", "poznań", "gdańsk",
                        "ul", "ulica", "al", "aleja", "plac", "centrum", "galeria",
                        "spółka z ograniczoną odpowiedzialnością", "z ograniczoną odpowiedzialnością",
                        "spółka akcyjna", "spółka komandytowa", "spółka jawna",
                        "sp. z o.o.", "sp.k.", "s.a.", "s.k.a.", "gmbh", "ltd", "limited", "inc", "llc",
                        "bv", "b.v.", "sarl", "srl", "ag", "plc", "oy", "ab", "as", "kft", "s.r.o.", "abc"):
            self.assertEqual(merchant_tokens(generic, None), frozenset(), generic)
        self.assertNotIn("polska", merchant_tokens("ab polska market piernik", None))
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
        with mock.patch.object(receipt_parser, "_redact_normalized", return_value="\n" * MAX_TEXT_LINES):
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
        check(extra_field="unexpected")
        for missing_key in ("expected_version", "receipt_id", "link"):
            with self.subTest(missing_key=missing_key), self.assertRaises(ReceiptError):
                validate_confirm({key: value for key, value in base.items() if key != missing_key})
        for omitted in ({key: value for key, value in base.items() if key != "lines"}, {**base, "lines": None}):
            cleaned = validate_confirm(omitted)
            self.assertIsNone(cleaned["lines"])
        omitted_headers = validate_confirm(
            {key: value for key, value in base.items() if key not in ("raw_shop_name", "raw_nip")},
        )
        self.assertNotIn("raw_shop_name", omitted_headers)
        self.assertNotIn("raw_nip", omitted_headers)
        for bad in ({}, [], None):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(ReceiptError):
                validate_confirm(bad)
        other = validate_confirm({**base, "lines": [_line(line_kind="other", unit_price_raw=None, ptu_code=None)]})
        self.assertEqual(other["lines"][0]["line_kind"], "other")
        self.assertIsNone(other["lines"][0]["unit_price_raw"])
        blank = validate_confirm({**base, "raw_shop_name": "", "raw_nip": "", "receipt_time": ""})
        self.assertEqual((blank["raw_shop_name"], blank["raw_nip"], blank["receipt_time"]), (None, None, None))
        # Legacy error precedence: impossible date + total mismatch still raises total_mismatch first.
        with self.assertRaises(ReceiptError) as precedence:
            validate_confirm({**base, "receipt_date": "2026-02-30", "total_amount": "99.00"})
        self.assertEqual((precedence.exception.code, precedence.exception.status),
                         ("budget_receipt_total_mismatch", 400))
        with self.assertRaises(ReceiptError) as date_only:
            validate_confirm({**base, "receipt_date": "2026-02-30"})
        self.assertEqual((date_only.exception.code, date_only.exception.status),
                         ("budget_receipt_invalid_request", 400))


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

    def test_gateway_domain_and_receipt_helpers_cover_all_branches(self):
        from types import SimpleNamespace
        from hestia.budget.gmail_store import _payment_payee_key
        from hestia.budget.receipt_store import _is_gateway_domain, _is_gateway_receipt
        from hestia.db import AppMeta

        for non_gateway in (None, 123, "", "localhost", "sklep.example.com", "merchant.payu"):
            self.assertFalse(_is_gateway_domain(non_gateway), repr(non_gateway))
        for gateway_dom in (
            "przelewy24.pl", "mail.przelewy24.pl", "p24.pl", "paypro.pl",
            "autopay.pl", "sub.autopay.pl", "tpay.com", "paynow.pl", "dotpay.pl",
            "payu.pl", "secure.payu.com", "pay.bluemedia.pl",
        ):
            self.assertTrue(_is_gateway_domain(gateway_dom), gateway_dom)

        with self.sessions.begin() as session:
            self.assertFalse(_is_gateway_receipt(session, SimpleNamespace(
                id=None, raw_shop_name="Biedronka", forwarded_sender_domain=None, sender_domain="biedronka.pl",
            )))
            self.assertTrue(_is_gateway_receipt(session, SimpleNamespace(
                id=None, raw_shop_name="Sklep PAYPRO", forwarded_sender_domain=None, sender_domain=None,
            )))
            self.assertTrue(_is_gateway_receipt(session, SimpleNamespace(
                id=None, raw_shop_name="Biedronka", forwarded_sender_domain="mail.payu.pl", sender_domain=None,
            )))
            self.assertTrue(_is_gateway_receipt(session, SimpleNamespace(
                id=None, raw_shop_name="Biedronka", forwarded_sender_domain=None, sender_domain="autopay.pl",
            )))
            session.add(AppMeta(key=_payment_payee_key("gw-rec-1"), value="hashed-payee"))
            session.flush()
            self.assertFalse(_is_gateway_receipt(session, SimpleNamespace(
                id="other-rec-1", raw_shop_name="Lidl", forwarded_sender_domain=None, sender_domain="lidl.pl",
            )))
            self.assertTrue(_is_gateway_receipt(session, SimpleNamespace(
                id="gw-rec-1", raw_shop_name="Lidl", forwarded_sender_domain=None, sender_domain="lidl.pl",
            )))
            self.assertTrue(_is_gateway_receipt(session, SimpleNamespace(
                id=None, raw_shop_name="SKLEP / BLUE MEDIA", forwarded_sender_domain=None, sender_domain=None,
            )))
            self.assertTrue(_is_gateway_receipt(session, SimpleNamespace(
                id=None, raw_shop_name="SKLEP REVOLUT PAY", forwarded_sender_domain=None, sender_domain=None,
            )))

    def test_iban_masking_nip_proximity_party_names_and_gateway_phrases_cover_branches(self):
        from hestia.budget.receipt_parser import contains_gateway_marker, strip_legal_forms
        from hestia.budget.receipt_reconcile import _payload_party_name, resolved_party_names, transaction_party_names
        from hestia.budget.receipt_store import _mask_iban_sequences, _nip_in_text, _nip_match, _payload_party_texts

        # Legal-form stripping (with/without diacritics, non-string guard, word boundaries).
        self.assertEqual(strip_legal_forms(None), "")
        for suffix in (
            "spółka z ograniczoną odpowiedzialnością", "spolka z ograniczona odpowiedzialnoscia",
            "z ograniczoną odpowiedzialnością", "z ograniczona odpowiedzialnoscia",
            "spółka akcyjna", "spolka akcyjna", "spółka komandytowa", "spolka komandytowa",
            "spółka jawna", "spolka jawna", "sp. z o.o.", "sp.k.", "s.a.", "s.k.a.",
            "gmbh", "ltd", "limited", "inc", "llc", "bv", "b.v.", "sarl", "srl", "ag",
            "plc", "oy", "ab", "as", "kft", "s.r.o.",
        ):
            self.assertEqual(merchant_tokens(f"Alfa {suffix}", None), frozenset({"alfa"}), suffix)
            self.assertEqual(merchant_tokens(f"Alfa {suffix.upper()}", None), frozenset({"alfa"}), suffix)

        # Gateway marker edge branches: empty, non-gateway pairs vs all phrase/token variants.
        self.assertFalse(contains_gateway_marker(None))
        self.assertFalse(contains_gateway_marker("MEDIA MARKT"))
        self.assertFalse(contains_gateway_marker("REVOLUT TRANSFER"))
        for marker in (
            "BLUE MEDIA", "Blue-Media", "BLUEMEDIA", "REVOLUT PAY", "Stripe", "PayPal", "Adyen", "Klarna",
            "Krajowy Integrator Płatności S.A.", "Krajowy Integrator Platnosci", "Autopay S.A.",
            "PayU S.A.", "Pay Pro S.A.", "PayPal (Europe)", "mBank SA PayNow", "eService",
            "Elavon", "First Data", "Fiserv", "Polcard", "eCard", "CashBill", "HotPay", "imoje",
            "ING Bank Śląski imoje",
        ):
            self.assertTrue(contains_gateway_marker(marker), marker)

        # IBAN masking + strict NIP/VAT/TAX ID vs excluded NR/FAKTURA/FV/INVOICE/NO branches.
        self.assertEqual(_mask_iban_sequences(""), "")
        self.assertEqual(_mask_iban_sequences("..."), "...")
        self.assertIn("5260 2509 95", _mask_iban_sequences("ref_nl92 abna 5260 2509 95"))
        self.assertIn("5260250995", _mask_iban_sequences("ab12 sklepy 5260250995"))
        over_34 = "pl61 1090 1014 0000 0712 1981 2874 1234 5260250995"
        self.assertIn("5260250995", _mask_iban_sequences(over_34))
        self.assertEqual(_nip_match("5260250995", "nl92 abna 5260 2509 95"), (False, False))
        self.assertEqual(_nip_match("5260250995", "nl92abna5260250995"), (False, False))
        self.assertEqual(_nip_match("5260250995", "nl92 abna 5260 2509 95 nip 5260250995"), (True, True))
        self.assertEqual(_nip_match("5260250995", "vat pl 526-025-09-95"), (True, True))
        self.assertEqual(_nip_match("5260250995", "vat id: 5260250995"), (True, True))
        self.assertEqual(_nip_match("5260250995", "nip pl 5260250995"), (True, True))
        self.assertEqual(_nip_match("5260250995", "/nip/5260250995"), (True, True))
        self.assertEqual(_nip_match("5260250995", "tax id: 5260250995"), (True, True))
        self.assertEqual(_nip_match("5260250995", "pl5260250995"), (True, True))
        self.assertEqual(_nip_match("5260250995", "faktura vat 5260250995"), (True, False))
        self.assertEqual(_nip_match("5260250995", "faktura vat nr 5260250995"), (False, False))
        self.assertEqual(_nip_match("5260250995", "fv/5260250995"), (False, False))
        self.assertEqual(_nip_match("5260250995", "invoice 5260250995"), (False, False))
        self.assertEqual(_nip_match("5260250995", "no. pl5260250995"), (False, False))
        self.assertTrue(_nip_in_text("5260250995", "pl5260250995"))

        # Payload party extraction and metadata debtor/creditor branches.
        self.assertIsNone(_payload_party_name("bad", "creditor"))
        self.assertIsNone(_payload_party_name({"creditor": "not-dict"}, "creditor"))
        self.assertEqual(_payload_party_name({"creditor_name": "BLUE MEDIA"}, "creditor"), "BLUE MEDIA")
        self.assertEqual(_payload_party_name({"debtor": {"name": "PayU"}}, "debtor"), "PayU")
        with self.sessions.begin() as session:
            self.assertEqual(
                resolved_party_names(session, "owner", self.accounts["a"], "missing-tx", {"debtor": {"name": "PayU"}}, None),
                (None, "PayU"),
            )
        self.assertEqual(
            _payload_party_texts({
                "creditor_name": "BLUE MEDIA", "debtor_name": "",
                "creditor": "not-dict", "debtor": {"name": "PayU"},
            }),
            ["BLUE MEDIA", "PayU"],
        )
        self.add_transaction("a", "meta-bad-json", amount="12.30", booking_date="2026-11-30")
        self.metadata("a", "meta-bad-json", {"creditor_name": "ACME", "debtor_name": "PAYU"})
        with self.sessions.begin() as session:
            self.assertEqual(transaction_party_names(session, "owner", self.accounts["a"], "meta-bad-json", None),
                             (None, None))
            meta = session.get(db.BudgetTransactionMetadata, (self.accounts["a"], "meta-bad-json"))
            self.assertEqual(
                transaction_party_names(session, "owner", self.accounts["a"], "meta-bad-json", meta.ledger_fingerprint),
                ("ACME", "PAYU"),
            )
            self.assertEqual(
                transaction_party_names(session, "other", self.accounts["a"], "meta-bad-json", meta.ledger_fingerprint),
                (None, None),
            )
            meta.payload_json = "{bad"
            session.flush()
            self.assertEqual(
                transaction_party_names(session, "owner", self.accounts["a"], "meta-bad-json", meta.ledger_fingerprint),
                (None, None),
            )

    def test_round3_token_letter_floor_strict_iban_and_corrupt_evidence_edges(self):
        import uuid
        from hestia.budget.analytics_core import financial_fingerprint
        from hestia.budget.receipt_parser import merchant_score_groups
        from hestia.budget.receipt_reconcile import reconcile_receipt_links
        from hestia.budget.receipt_store import _mask_iban_sequences, _nip_match

        # Numeric and <3-letter tokens never qualify as distinctive; >=3 letters with digits (>=4 chars) do.
        self.assertEqual(merchant_score_groups("SKLEP 1234 A123 AB12", None), ((), ("sklep",)))
        self.assertEqual(merchant_tokens("SKLEP 1234 A123 AB12 ABC1", None), frozenset({"abc1"}))

        # PL+10 digits VAT ID is never eaten by IBAN masker; country length / mod-97 IBANs ARE masked.
        self.assertIn("PL5260250995", _mask_iban_sequences("NIP PL5260250995 FV2026"))
        self.assertIn("2026", _mask_iban_sequences("NL92 ABNA 5260 2509 95 2026"))
        self.assertNotIn("5260250995", _mask_iban_sequences("NL92ABNA5260250995"))
        self.assertNotIn("5260", _mask_iban_sequences("PL61 1090 1014 0000 5260 2509 9500"))
        self.assertNotIn("8601", _mask_iban_sequences("NO93 8601 1117 947"))
        self.assertIn("5260 2509 95", _mask_iban_sequences("DE89 1234 5260 2509 95"))
        self.assertNotIn("5260", _mask_iban_sequences("XX25 1234 5260 2509 95"))
        self.assertIn("5260 2509 95", _mask_iban_sequences("XX00 1234 5260 2509 95"))
        self.assertIn("EFGH IJKL", _mask_iban_sequences("BE92 ABCD EFGH IJKL"))
        long_single = "PL61" + "0" * 34
        self.assertEqual(_mask_iban_sequences(long_single), long_single)

        self.assertEqual(_nip_match("5260250995", "NIP PL5260250995 FV2026"), (True, True))
        self.assertEqual(_nip_match("5260250995", "NL92 ABNA 5260 2509 95 2026"), (False, False))
        self.assertEqual(_nip_match("5260250995", "a5260250995 5260250995b nr 5260250995"), (False, False))

        # Invalid JSON and non-dict JSON on linked_evidence cleanly fall through to needs_review.
        for idx, bad_ev in enumerate(("not-json", "[]")):
            tx_ref = f"r3-bad-ev-{idx}"
            payload = self.add_transaction("a", tx_ref, amount="12.00", booking_date="2026-11-30")
            fp = financial_fingerprint(payload)
            rid = str(uuid.uuid4())
            with self.sessions.begin() as session:
                session.add(db.BudgetReceipt(
                    id=rid, owner="owner", raw_shop_name="BIEDRONKA", raw_nip="7791011327",
                    receipt_kind="purchase", receipt_date="2026-11-30", total_amount="12.00", currency="PLN",
                    payment_method="card", status="confirmed", source_kind="local_ocr", document_kind="receipt",
                    linked_account_id=self.accounts["a"], linked_entry_ref=tx_ref,
                    linked_fingerprint=fp, linked_evidence=bad_ev,
                    version=1, created_at=self.now + idx, updated_at=self.now + idx,
                ))
                self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 50 + idx), 1)

        # Shop-category nouns (PL/EN with diacritics & inflections) never enter distinctive merchant tokens.
        for category_shop in (
            "APTEKA CENTRUM", "APTEKI CENTRUM", "SKLEP SPOŻYWCZY", "PIEKARNIA CUKIERNIA",
            "PIEKARNI CUKIERNI", "RESTAURACJA KAWIARNIA", "BAR PIZZERIA BISTRO", "STACJA PALIWA",
            "HOTEL HOSTEL KINO", "SALON STUDIO DROGERIA", "KSIĘGARNIA KSIEGARNI KWIACIARNIA",
            "OPTYK FRYZJERA KEBAB SUSHI", "DELIKATESY MARKET SUPERMARKET HIPERMARKET",
            "PARKING MYJNIA WARSZTAT SERWIS", "PRZYCHODNIA GABINET KLINIKA",
            "PHARMACY BAKERY CAFE CAFÉ COFFEE RESTAURANT STATION FUEL CINEMA CLINIC",
        ):
            self.assertEqual(merchant_tokens(category_shop, None), frozenset(), category_shop)
        self.assertEqual(merchant_score_groups("Księgarnia Centrum", None), ((), ("centrum", "ksiegarnia")))

        # E-commerce and billing words/inflections never qualify as distinctive tokens.
        for ecom_shop in (
            "SKLEP INTERNETOWY INTERNETOWA INTERNETOWE INTERNETOWEJ",
            "ZAKUP ZAKUPY ZAKUPÓW ZAMÓWIENIE ZAMOWIENIA ORDER ORDERS",
            "PŁATNOŚĆ PLATNOSCI PAYMENT PAYMENTS USŁUGA USLUGI USŁUG",
            "ABONAMENT ABONAMENTU OPŁATA OPŁATY FAKTURA RACHUNEK TRANSAKCJA",
            "KARTA KARTY PRZELEW PRZELEWU TRANSFER POLECENIE ZAPŁATA ZAPLATA",
        ):
            self.assertEqual(merchant_tokens(ecom_shop, None), frozenset(), ecom_shop)
        self.assertEqual(
            merchant_score_groups("Sklep Internetowy Zamówienie", None),
            ((), ("internetowy", "sklep", "zamowienie")),
        )

        # Generic-only business/corporate names never fall back to distinctive tokens.
        for corporate_generic in (
            "FIRMA HANDLOWA",
            "FIRMA HANDLOWO-USŁUGOWA",
            "COMPANY",
            "SKLEP OGÓLNOSPOŻYWCZY",
            "PRZEDSIĘBIORSTWO HANDLOWE USŁUGOWE PRODUKCYJNE HURTOWNIA CENTRUM",
            "GRUPA HOLDING INTERNATIONAL TRADING SOLUTIONS SYSTEMS CONSULTING POLAND POLSKA PL",
        ):
            self.assertEqual(merchant_tokens(corporate_generic, None), frozenset(), corporate_generic)
            groups, _generic = merchant_score_groups(corporate_generic, None)
            self.assertEqual(groups, (), corporate_generic)
        self.assertEqual(merchant_score_groups("FIRMA HANDLOWA ALFA", None)[0], ((frozenset({"alfa"}),),))


class ReceiptApiEdgeTests(unittest.TestCase):
    def test_store_refuses_a_non_authoritative_backend(self):
        sessions = mock.MagicMock()
        sessions.return_value.__enter__.return_value.get.return_value = None
        with mock.patch("hestia.budget.receipt_api.init_db", return_value=(mock.Mock(), sessions)), \
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
