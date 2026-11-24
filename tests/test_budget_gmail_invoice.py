"""Issuer-specific invoice layouts and parser isolation boundaries."""
from __future__ import annotations

from pathlib import Path
import unittest

from hestia.budget.gmail_invoice import (
    ImportedInvoice,
    _parse_pln_amount,
    _safe_field,
    extract_invoice,
    has_provider_invoice_number,
)


_FIXTURES = Path(__file__).parent / "fixtures" / "gmail" / "pdf_text"


def _fixture(name):
    return (_FIXTURES / name).read_text(encoding="utf-8")


class IssuerInvoiceFixtureTests(unittest.TestCase):
    def test_tmobile_layout(self):
        text = _fixture("tmobile_invoice.txt")
        self.assertEqual(
            extract_invoice(text),
            ImportedInvoice("T-Mobile", "5261040567", "501234567890", "59.99", "PLN", "2026-09-09", "2026-09-23"),
        )
        self.assertTrue(has_provider_invoice_number(text))

    def test_play_layout(self):
        text = _fixture("play_invoice.txt")
        self.assertEqual(
            extract_invoice(text),
            ImportedInvoice("Play", "9512120077", "F/40000001/11/26", "150.37", "PLN", "2026-11-08", "2026-11-23"),
        )
        self.assertTrue(has_provider_invoice_number(text))

    def test_orange_layout(self):
        text = _fixture("orange_invoice.txt")
        self.assertEqual(
            extract_invoice(text),
            ImportedInvoice("Orange", "5260250995", "26110200000001", "88.1", "PLN", "2026-11-14", "2026-11-30"),
        )
        self.assertTrue(has_provider_invoice_number(text))

    def test_correction_and_credit_note_are_not_payable_invoices(self):
        self.assertIsNone(extract_invoice(_fixture("tmobile_correction.txt")))
        invoice = _fixture("tmobile_invoice.txt")
        for marker in ("FAKTURA KORYGUJĄCA", "Numer faktury korygującej: 509100000001"):
            with self.subTest(marker=marker):
                self.assertIsNone(extract_invoice(invoice + "\n" + marker))

    def test_play_payment_is_primary_and_any_summary_must_agree(self):
        original = _fixture("play_invoice.txt")
        disagreeing = original.replace(
            "PODSUMOWANIE KWOTY DO ZAPŁATY: 150,37 zł", "PODSUMOWANIE KWOTY DO ZAPŁATY: 150,38 zł"
        )
        self.assertIsNone(extract_invoice(disagreeing))
        without_summary = original.replace("PODSUMOWANIE KWOTY DO ZAPŁATY: 150,37 zł\n", "")
        self.assertEqual(extract_invoice(without_summary).amount, "150.37")

    def test_missing_due_date_is_rejected_for_each_real_layout(self):
        cases = (
            ("tmobile_invoice.txt", "Termin płatności: 23.09.2026", "Termin płatności:"),
            ("play_invoice.txt", "23.11.2026", "23.11.202"),
            ("orange_invoice.txt", "Zapłać do: 30.11.2026", "Zapłać do:"),
        )
        for name, old, new in cases:
            with self.subTest(name=name):
                self.assertIsNone(extract_invoice(_fixture(name).replace(old, new)))

    def test_more_than_one_brand_is_rejected(self):
        text = _fixture("tmobile_invoice.txt") + "\nOrange Polska S.A.\n"
        self.assertIsNone(extract_invoice(text))

    def test_bank_account_lines_never_supply_invoice_numbers(self):
        cases = (
            ("tmobile_invoice.txt", (("Numer faktury: 501234567890\n", ""),)),
            ("play_invoice.txt", (("Numer faktury\nF/40000001/11/26\n", ""),)),
            ("orange_invoice.txt", (
                ("Tytuł przelewu: 26110200000001\n", ""),
                ("Do faktury VAT numer: 26110200000001\n", ""),
            )),
        )
        for name, removals in cases:
            with self.subTest(name=name):
                text = _fixture(name)
                for old, new in removals:
                    text = text.replace(old, new)
                self.assertIsNone(extract_invoice(text))

    def test_pesel_like_invoice_numbers_are_rejected(self):
        cases = (
            ("tmobile_invoice.txt", "501234567890"),
            ("play_invoice.txt", "F/40000001/11/26"),
            ("orange_invoice.txt", "26110200000001"),
        )
        for name, number in cases:
            with self.subTest(name=name):
                self.assertIsNone(extract_invoice(_fixture(name).replace(number, "44051401458")))

    def test_forwarded_number_formats_replace_the_assumed_formats(self):
        cases = (
            ("tmobile_invoice.txt", "501234567890", "TM-2026-120"),
            ("play_invoice.txt", "F/40000001/11/26", "P4/2026/002"),
            ("orange_invoice.txt", "26110200000001", "FV/2026/10/12345"),
        )
        for name, number, old_format in cases:
            with self.subTest(name=name):
                self.assertFalse(has_provider_invoice_number(_fixture(name).replace(number, old_format)))

    def test_orange_transfer_title_must_agree_when_present(self):
        text = _fixture("orange_invoice.txt")
        disagreeing = text.replace("Tytuł przelewu: 26110200000001", "Tytuł przelewu: 26110200000002")
        self.assertIsNone(extract_invoice(disagreeing))
        without_title = text.replace("Tytuł przelewu: 26110200000001\n", "")
        self.assertEqual(extract_invoice(without_title).invoice_number, "26110200000001")

    def test_missing_or_invalid_amount_is_rejected(self):
        tmobile = _fixture("tmobile_invoice.txt").replace(
            "A+C Prosimy o wpłatę 59,99 zł brutto i", "A+C Prosimy o wpłatę -59,99 zł brutto i"
        )
        play = _fixture("play_invoice.txt").replace("PROSIMY O WPŁATĘ: 150,37 zł", "PROSIMY O WPŁATĘ:")
        for name, text in (("T-Mobile", tmobile), ("Play", play)):
            with self.subTest(issuer=name):
                self.assertIsNone(extract_invoice(text))

    def test_issue_date_cannot_follow_due_date(self):
        text = _fixture("tmobile_invoice.txt").replace(
            "Data i miejsce wystawienia: 09.09.2026", "Data i miejsce wystawienia: 24.09.2026"
        )
        self.assertIsNone(extract_invoice(text))

    def test_orange_issue_date_is_optional_but_other_fields_remain_structured(self):
        text = _fixture("orange_invoice.txt").replace("14 listopada 2026", "14 listopad 2026")
        parsed = extract_invoice(text)
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.issue_date)
        self.assertEqual(parsed.invoice_number, "26110200000001")

    def test_real_layouts_accept_normalized_crlf_input(self):
        for name in ("tmobile_invoice.txt", "play_invoice.txt", "orange_invoice.txt"):
            with self.subTest(name=name):
                text = _fixture(name)
                self.assertEqual(extract_invoice(text.replace("\n", "\r\n")), extract_invoice(text))


class RespectCompatibilityTests(unittest.TestCase):
    def test_existing_respect_fixture_is_unchanged(self):
        text = (Path(__file__).parent / "fixtures" / "gmail" / "respect-energy.txt").read_text(encoding="utf-8")
        self.assertEqual(
            extract_invoice(text),
            ImportedInvoice("Respect Energy", "7791011327", "RE/09/2026/042", "231.8", "PLN",
                            "2026-09-04", "2026-09-25"),
        )
        self.assertTrue(has_provider_invoice_number(text))

    def test_every_field_rejects_pan_iban_and_pesel(self):
        fields = {
            "issuer": "Respect Energy", "nip": "7791011327", "invoice_number": "RE/2026/00042", "amount": "50,23",
            "currency": "PLN", "issue_date": "2026-07-01", "due_date": "2026-07-29",
        }
        template = ("{issuer}\nNIP sprzedawcy: {nip}\nNumer dokumentu: {invoice_number}\nKwota: {amount} {currency}\n"
                    "Data wystawienia: {issue_date}\nTermin płatności: {due_date}")
        self.assertTrue(extract_invoice(template.format(**fields)))
        for secret in ("4111-1111-1111-1111", "PL 61 1090 1014 0000 0712 1981 2874", "44051401458"):
            for name in fields:
                with self.subTest(name=name, secret=secret):
                    modified = fields | {name: f"Respect Energy {secret}" if name == "issuer" else secret}
                    parsed = extract_invoice(template.format(**modified))
                    if name in ("nip", "issue_date"):
                        self.assertIsNotNone(parsed)
                        self.assertIsNone(getattr(parsed, name))
                        self.assertNotIn(secret, repr(parsed))
                    else:
                        self.assertIsNone(parsed)

    def test_text_normalization_and_ambiguous_or_missing_fields(self):
        invoice = ("Respect Energy\nNumer dokumentu: RE/2026/042\nKwota: 12,19 EUR\n"
                   "Termin płatności: 05.02.2026\n")
        cases = (
            "", "Unknown supplier\nNumer dokumentu: RE/2026/042\n", invoice + "\nP4 Sp. z o.o.",
            invoice + "Kwota: 44 PLN", invoice.replace("RE/2026/042", "AA12BB34CC56DD78EE90FF12"),
            invoice.replace("RE/2026/042", "4405FT1401458"), invoice.replace("12,19 EUR", "440514014,58 PLN"),
            invoice.replace("05.02.2026", "30.02.2026"), invoice + "Data dokumentu: 06.02.2026",
            invoice.replace("RE/2026/042", "near@example.test"), invoice.replace("Kwota: 12,19 EUR", "Kwota:"),
            invoice.replace("RE/2026/042", "\n" + "x" * 513 + "\nunrelated"), "Respect Energy",
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(extract_invoice(case))
        safe = extract_invoice(
            invoice.replace("Respect Energy", "Respect Energy invoices@example.test") + "NIP wystawcy: PL7791011327"
        )
        self.assertEqual(
            safe, ImportedInvoice("Respect Energy", "7791011327", "RE/2026/042", "12.19", "EUR", None, "2026-02-05")
        )

    def test_generic_nip_and_zloty_currency_remain_supported_for_respect(self):
        text = ("Respect Energy\nNumer dokumentu: RE/2026/00042\nKwota brutto: 1 234,00 zł\n"
                "Data wystawienia: 02.11.2026\nPłatne do: 15.11.2026\nNIP: 781 189 73 58")
        self.assertEqual(
            extract_invoice(text),
            ImportedInvoice("Respect Energy", "7811897358", "RE/2026/00042", "1234", "PLN",
                            "2026-11-02", "2026-11-15"),
        )

    def test_safe_field_and_pln_amount_boundaries(self):
        self.assertIsNone(_safe_field(""))
        self.assertIsNone(_safe_field("billing@example.test"))
        self.assertIsNone(_safe_field("44051401458"))
        self.assertEqual(_safe_field("12345678901"), "12345678901")
        self.assertIsNone(_parse_pln_amount(""))
        self.assertIsNone(_parse_pln_amount("12,19 PLN"))
        self.assertEqual(_parse_pln_amount("1 234,00 zł"), "1234")


if __name__ == "__main__":
    unittest.main()
