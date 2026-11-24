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
_TMOBILE_NUMBER_LINE = "Numer faktury: 501234567890"
_TMOBILE_ISSUE_LINE = "Data i miejsce wystawienia: 09.09.2026, Warszawa"
_TMOBILE_HEADER = "Faktura nr: 501234567890 z dnia 09.09.2026"
_TMOBILE_FOOTER = (
    "Numer Klienta: 1.00000000 Numer faktury: 501234567890 "
    "Okres rozliczeniowy: 09.08.2026–08.09.2026 Data wystawienia: 09.09.2026"
)


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

    def test_tmobile_2025_layout_reads_header_and_footer(self):
        current = _fixture("tmobile_invoice.txt")
        legacy = current.replace(_TMOBILE_NUMBER_LINE + "\n", "").replace(_TMOBILE_ISSUE_LINE + "\n", "")
        for extra in (_TMOBILE_HEADER, _TMOBILE_FOOTER, f"{_TMOBILE_HEADER}\n{_TMOBILE_FOOTER}"):
            with self.subTest(extra=extra):
                self.assertEqual(extract_invoice(legacy + extra + "\n"), extract_invoice(current))

    def test_tmobile_all_number_and_issue_sources_must_agree(self):
        original = _fixture("tmobile_invoice.txt")
        all_sources = original + f"{_TMOBILE_HEADER}\n{_TMOBILE_FOOTER}\n"
        self.assertEqual(extract_invoice(all_sources), extract_invoice(original))
        for good, bad in (
            ("Numer faktury: 501234567890\n", "Numer faktury: 501234567891\n"),
            ("Faktura nr: 501234567890", "Faktura nr: 501234567891"),
            ("501234567890 Okres", "501234567891 Okres"),
            ("09.09.2026, Warszawa", "10.09.2026, Warszawa"),
            ("z dnia 09.09.2026", "z dnia 10.09.2026"),
            ("Data wystawienia: 09.09.2026", "Data wystawienia: 10.09.2026"),
            ("Data wystawienia: 09.09.2026", "Data wystawienia: 10.09.2026 Data wystawienia: 09.09.2026"),
        ):
            with self.subTest(source=good):
                self.assertIsNone(extract_invoice(all_sources.replace(good, bad)))

    def test_tmobile_requires_a_number_and_a_valid_issue_date(self):
        original = _fixture("tmobile_invoice.txt")
        for good, bad in (
            (_TMOBILE_NUMBER_LINE + "\n", ""),
            (_TMOBILE_ISSUE_LINE + "\n", ""),
            (_TMOBILE_ISSUE_LINE, _TMOBILE_ISSUE_LINE.replace("09.09.2026", "31.09.2026")),
        ):
            with self.subTest(source=good, replacement=bad):
                self.assertIsNone(extract_invoice(original.replace(good, bad)))

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
        invoices = {
            "T-Mobile": _fixture("tmobile_invoice.txt"),
            "Orange": _fixture("orange_invoice.txt"),
            "Play": _fixture("play_invoice.txt"),
            "Respect Energy": (_FIXTURES.parent / "respect-energy.txt").read_text(encoding="utf-8"),
        }
        for issuer, invoice in invoices.items():
            for marker in ("FAKTURA KORYGUJĄCA", "faktura\tkorygująca", "faktura\n korygująca", "korekta",
                           "Numer faktury\u2003\n korygującej: 509100000001"):
                with self.subTest(issuer=issuer, marker=marker):
                    self.assertIsNone(extract_invoice(invoice + "\n" + marker))

    def test_correction_detection_skips_long_lines_without_joining_words(self):
        invoice = _fixture("tmobile_invoice.txt")
        for text in ("Korekta ".ljust(513, "x"), "faktura\n" + "x" * 513 + "\nkorygująca"):
            with self.subTest(text=text[:16]):
                self.assertEqual(extract_invoice(invoice + text + "\n"), extract_invoice(invoice))
        split_marker = "faktura\n" + " " * 513 + "\nkorygująca\n"
        self.assertIsNone(extract_invoice(invoice + split_marker))

    def test_play_payment_is_primary_and_any_summary_must_agree(self):
        original = _fixture("play_invoice.txt")
        summary = "PODSUMOWANIE KWOTY DO ZAPŁATY: 150,37 zł"
        label = "PODSUMOWANIE KWOTY DO ZAPŁATY"
        for suffix in ("", ":", ": 150,38 zł", ": -150,37 zł", ": nie podano", ": 150,37 zł dopisek"):
            line = label + suffix
            with self.subTest(line=line):
                self.assertIsNone(extract_invoice(original.replace(summary, line)))
                self.assertIsNone(extract_invoice(original + line + "\n"))
        without_summary = original.replace(summary + "\n", "")
        self.assertEqual(extract_invoice(without_summary).amount, "150.37")
        equivalent_amount = original.replace(summary, f"{label}: 000150,37\u00a0ZŁ")
        self.assertEqual(extract_invoice(equivalent_amount).amount, "150.37")

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

    def test_tmobile_footer_does_not_take_numbers_from_account_lines(self):
        original = _fixture("tmobile_invoice.txt").replace(_TMOBILE_NUMBER_LINE + "\n", "")
        self.assertEqual(extract_invoice(original + _TMOBILE_FOOTER + "\n").invoice_number, "501234567890")
        for word in ("RaChUnKu", "KoNtO", "iBaN"):
            with self.subTest(word=word):
                footer = _TMOBILE_FOOTER.replace("Okres rozliczeniowy: ", f"Okres rozliczeniowy: {word} ")
                self.assertIsNone(extract_invoice(original + footer + "\n"))

    def test_pesel_and_account_like_invoice_numbers_are_rejected(self):
        cases = (
            ("tmobile_invoice.txt", "501234567890"),
            ("play_invoice.txt", "F/40000001/11/26"),
            ("orange_invoice.txt", "26110200000001"),
        )
        for name, number in cases:
            for forbidden in ("44051401458", "0" * 26):
                with self.subTest(name=name, number=forbidden):
                    self.assertIsNone(extract_invoice(_fixture(name).replace(number, forbidden)))

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
        title = "Tytuł przelewu: 26110200000001"
        for suffix in ("", ":", ": 26110200000002", ": 26110200000001 dopisek"):
            line = "Tytuł przelewu" + suffix
            with self.subTest(line=line):
                self.assertIsNone(extract_invoice(text.replace(title, line)))
                self.assertIsNone(extract_invoice(text + line + "\n"))
        self.assertEqual(extract_invoice(text + title + "\n"), extract_invoice(text))
        without_title = text.replace(title + "\n", "")
        self.assertEqual(extract_invoice(without_title).invoice_number, "26110200000001")

    def test_orange_requires_invoice_number_label_and_matching_payment_date(self):
        original = _fixture("orange_invoice.txt")
        block = "Prosimy\no wpłatę do:\n30.11.2026\n88,10 zł"
        for good, bad in (
            ("Do faktury VAT numer: 26110200000001\n", ""),
            (block, block.replace("30.11.2026", "29.11.2026")),
            (block, block.replace("30.11.2026", "31.11.2026")),
            (block, block + "\n" + block.replace("30.11.2026", "29.11.2026")),
        ):
            with self.subTest(replacement=bad):
                self.assertIsNone(extract_invoice(original.replace(good, bad)))

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
        original = _fixture("orange_invoice.txt")
        for unknown_month in ("listopad", "İ", "lİstopada"):
            with self.subTest(month=unknown_month):
                parsed = extract_invoice(original.replace("listopada", unknown_month))
                self.assertIsNotNone(parsed)
                self.assertIsNone(parsed.issue_date)
                self.assertEqual(parsed.invoice_number, "26110200000001")
        self.assertEqual(extract_invoice(original.replace("listopada", "LISTOPADA")), extract_invoice(original))

        for issue_line in ("", "Data wystawienia: 14 listopada 2026\nData wystawienia: 15 listopada 2026\n"):
            with self.subTest(issue_line=issue_line):
                parsed = extract_invoice(original.replace("Data wystawienia: 14 listopada 2026\n", issue_line))
                self.assertIsNotNone(parsed)
                self.assertIsNone(parsed.issue_date)

    def test_scoped_labels_are_anchored_to_their_lines(self):
        for name, label in (
            ("tmobile_invoice.txt", _TMOBILE_NUMBER_LINE),
            ("orange_invoice.txt", "Do faktury VAT numer: 26110200000001"),
            ("play_invoice.txt", "TERMIN PŁATNOŚCI: 23.11.2026 PROSIMY O WPŁATĘ: 150,37 zł"),
        ):
            with self.subTest(name=name):
                self.assertIsNone(extract_invoice(_fixture(name).replace(label, f"Przykład {label}")))

    def test_scoped_scans_skip_long_lines_before_matching(self):
        original = _fixture("tmobile_invoice.txt")
        conflict = "Faktura nr: 501234567891 z dnia 09.09.2026"
        bounded = conflict.ljust(512)
        self.assertIsNone(extract_invoice(original + bounded + "\n"))
        self.assertEqual(extract_invoice(original + bounded + " \n"), extract_invoice(original))

        noisy_footer = "Numer Klienta: 0 Numer faktury: 501234567891 Okres rozliczeniowy: "
        footer_end = " Data wystawienia: 09.09.2026\n"
        repeated_size = 32768 - len((original + footer_end).encode("utf-8"))
        hostile = original + (noisy_footer * 512)[:repeated_size] + footer_end
        self.assertEqual(len(hostile.encode("utf-8")), 32768)
        self.assertEqual(extract_invoice(hostile), extract_invoice(original))

    def test_play_multiline_invoice_number_cannot_bridge_a_skipped_line(self):
        text = _fixture("play_invoice.txt").replace("Numer faktury\n", "Numer faktury\n" + "x" * 513 + "\n")
        self.assertIsNone(extract_invoice(text))

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

    def test_generic_respect_fields_can_share_lines_or_span_two(self):
        text = ("Respect Energy\nNumer dokumentu:\nSYNTHETIC-EXAMPLE Kwota: 19.40 PLN "
                "Termin płatności: 14.05.2026\n")
        self.assertEqual(
            extract_invoice(text),
            ImportedInvoice("Respect Energy", None, "SYNTHETIC-EXAMPLE", "19.4", "PLN", None, "2026-05-14"),
        )
        self.assertFalse(has_provider_invoice_number(text))

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
