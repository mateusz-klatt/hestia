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
_RESPECT_FIXTURE = _FIXTURES.parent / "respect-energy.txt"
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
            "Respect Energy": _RESPECT_FIXTURE.read_text(encoding="utf-8"),
        }
        for issuer, invoice in invoices.items():
            for marker in (
                "FAKTURA KORYGUJĄCA", "faktura\tkorygująca", "faktura\n korygująca", "korekta",
                "Numer faktury\u2003\n korygującej: 509100000001", "FAKTURA VAT KORYGUJĄCA",
                "Numer faktury\tVAT\n korygującej: 509100000001", "Dokument: KOREKTA DO\nFAKTURY",
                "KOREKTA VAT", "KOREKTA nr 1", "KOREKTA FAKTURY nr 1", "STRONA 1 KOREKTA",
                "Korekta\u00a0faktury", "kore\u200dkta\u00a0faktury", "faktura koryguja\u200b\u0328ca",
                "faktura\n\x00\nkorygująca",
            ):
                with self.subTest(issuer=issuer, marker=marker):
                    self.assertIsNone(extract_invoice(invoice + "\n" + marker))
            item = "Usługi telekomunikacyjne - korekta 14 lis 2026 1 23% -7,11 -8,75"
            with self.subTest(issuer=issuer, item=item):
                self.assertEqual(extract_invoice(invoice + "\n" + item), extract_invoice(invoice))

    def test_inline_and_wrapped_adjustments_are_payable_orange_invoices(self):
        invoice = _fixture("orange_invoice.txt")
        for adjustment in (
            "Usługi telekomunikacyjne - korekta 14 lis 2026 - 13 gru 2026 1 23% -7,11 -8,75",
            "Usługi telekomunikacyjne -\nkorekta 14 lis 2026 - 13 gru 2026 1 23% -7,11 -8,75",
            "Usługi telekomunikacyjne -\nkorekta",
            "Usługi telekomunikacyjne / \nkorekta",
        ):
            with self.subTest(adjustment=adjustment):
                self.assertEqual(extract_invoice(invoice + "\n" + adjustment), extract_invoice(invoice))

    def test_correction_detection_skips_long_lines_without_joining_words(self):
        invoice = _fixture("tmobile_invoice.txt")
        for text in (
            "Korekta ".ljust(513, "x"),
            "faktura\n" + "x" * 513 + "\nkorygująca",
            "STRONA " + "\ufb03" * 171 + " KOREKTA",  # NFKC expands each ligature to three characters.
        ):
            with self.subTest(text=text[:16]):
                self.assertEqual(extract_invoice(invoice + text + "\n"), extract_invoice(invoice))
        split_marker = "faktura\n" + " " * 513 + "\nkorygująca\n"
        self.assertIsNone(extract_invoice(invoice + split_marker))

    def test_label_words_in_prose_do_not_take_part_in_agreement(self):
        prose = {
            "tmobile_invoice.txt": "Termin płatności\nTermin płatności do zapłaty sprawdzisz w aplikacji\n",
            "play_invoice.txt": "PROSIMY O WPŁATĘ\nPODSUMOWANIE KWOTY DO ZAPŁATY\n",
            "orange_invoice.txt": "Zapłać do\n",
        }
        for name, extra in prose.items():
            with self.subTest(name=name):
                text = _fixture(name)
                self.assertIsNotNone(extract_invoice(text))
                self.assertEqual(extract_invoice(text + extra), extract_invoice(text))

    def test_play_payment_is_primary_and_any_summary_must_agree(self):
        original = _fixture("play_invoice.txt")
        summary = "PODSUMOWANIE KWOTY DO ZAPŁATY: 150,37 zł"
        label = "PODSUMOWANIE KWOTY DO ZAPŁATY"
        for suffix in ("", ":"):
            with self.subTest(heading=label + suffix):
                self.assertEqual(extract_invoice(original + label + suffix + "\n"), extract_invoice(original))
        for suffix in (": 150,38 zł", ": -150,37 zł", ": nie podano", ": 150,37 zł dopisek"):
            line = label + suffix
            with self.subTest(line=line):
                self.assertIsNone(extract_invoice(original.replace(summary, line)))
                self.assertIsNone(extract_invoice(original + line + "\n"))
        without_summary = original.replace(summary + "\n", "")
        self.assertEqual(extract_invoice(without_summary).amount, "150.37")
        equivalent_amount = original.replace(summary, f"{label}: 000150,37\u00a0ZŁ")
        self.assertEqual(extract_invoice(equivalent_amount).amount, "150.37")

    def test_all_layouts_accept_nbsp_next_to_the_currency(self):
        for path, old, new in (
            (_FIXTURES / "tmobile_invoice.txt", "59,99 zł", "59,99\u00a0zł"),
            (_FIXTURES / "play_invoice.txt", "150,37 zł", "150,37\u00a0zł"),
            (_FIXTURES / "orange_invoice.txt", "88,10 zł", "88,10\u00a0zł"),
            (_RESPECT_FIXTURE, "231,80 PLN", "231,80\u00a0PLN"),
        ):
            with self.subTest(name=path.name):
                original = path.read_text(encoding="utf-8")
                self.assertIn(old, original)
                self.assertEqual(extract_invoice(original.replace(old, new)), extract_invoice(original))

    def test_scoped_fields_use_normalized_text_before_redaction(self):
        tmobile = _fixture("tmobile_invoice.txt")
        normalized_number = "Nu\u200bmer faktury:\u00a0５０１２３４５６７８９０"
        self.assertEqual(
            extract_invoice(tmobile.replace(_TMOBILE_NUMBER_LINE, normalized_number)), extract_invoice(tmobile),
        )
        footer = _TMOBILE_FOOTER.replace("Okres rozliczeniowy: ", "Okres rozliczeniowy: I\u200bBAN ")
        self.assertIsNone(extract_invoice(tmobile + footer + "\n"))
        orange = _fixture("orange_invoice.txt")
        self.assertIsNone(extract_invoice(orange + "Tytuł\u00a0przelewu: 26110200000002\n"))
        play = _fixture("play_invoice.txt")
        self.assertIsNone(extract_invoice(play + "PODSUMOWANIE\u00a0KWOTY DO ZAPŁATY: -10,00 zł\n"))

    def test_duplicate_amount_labels_must_have_matching_valid_amounts(self):
        for name, equivalent, invalid in (
            (
                "tmobile_invoice.txt",
                ("A+C Prosimy o wpłatę 059,99 zł brutto i", "A+C Prosimy o wpłatę 59,99 zł",
                 "A+C Prosimy o wpłatę"),
                ("A+C Prosimy o wpłatę 60,00 zł brutto i", "A+C Prosimy o wpłatę -59,99 zł brutto i",
                 "A+C Prosimy o wpłatę nie podano"),
            ),
            (
                "play_invoice.txt",
                ("PROSIMY O WPŁATĘ: 000150,37 zł", "PROSIMY O WPŁATĘ:", "PROSIMY O WPŁATĘ"),
                ("PROSIMY O WPŁATĘ: 150,38 zł", "PROSIMY O WPŁATĘ: -10,00 zł",
                 "PROSIMY O WPŁATĘ: nie podano", "PROSIMY O WPŁATĘ: 0000 0000 0000 0000",
                 "TERMIN PŁATNOŚCI: 23.11.2026 PROSIMY O WPŁATĘ: -10,00 zł",
                 "TERMIN PŁATNOŚCI: nie podano PROSIMY O WPŁATĘ: 150,37 zł"),
            ),
        ):
            original = _fixture(name)
            # Bare label lines are table headings in the real layouts.
            for duplicate in equivalent:
                with self.subTest(name=name, duplicate=duplicate):
                    self.assertEqual(extract_invoice(original + duplicate + "\n"), extract_invoice(original))
            for duplicate in invalid:
                with self.subTest(name=name, duplicate=duplicate):
                    self.assertIsNone(extract_invoice(original + duplicate + "\n"))

    def test_duplicate_due_date_labels_must_have_matching_valid_dates(self):
        for name, label, date, other_date in (
            ("tmobile_invoice.txt", "Termin płatności:", "23.09.2026", "24.09.2026"),
            ("orange_invoice.txt", "Zapłać do:", "30.11.2026", "29.11.2026"),
        ):
            original = _fixture(name)
            self.assertEqual(extract_invoice(original + f"{label} {date}\n"), extract_invoice(original))
            self.assertEqual(extract_invoice(original + label + "\n"), extract_invoice(original))
            for duplicate_date in (other_date, "31.11.2026", "nie podano", f"{date} dopisek"):
                with self.subTest(name=name, duplicate_date=duplicate_date):
                    self.assertIsNone(extract_invoice(original + f"{label} {duplicate_date}\n"))

    def test_duplicate_issue_date_labels_must_have_matching_valid_dates(self):
        for name, equivalent, invalid in (
            (
                "play_invoice.txt", "Data wystawienia\n08.11.2026",
                ("Data wystawienia\n09.11.2026", "Data wystawienia\n31.11.2026",
                 "Data wystawienia\nnie podano", "Data wystawienia"),
            ),
            (
                "orange_invoice.txt", "Data wystawienia: 14 LISTOPADA 2026",
                ("Data wystawienia: 15 listopada 2026", "Data wystawienia: 31 listopada 2026",
                 "Data wystawienia: 14 lİstopada 2026", "Data wystawienia: 14 listopada 2026 dopisek",
                 "Data wystawienia:"),
            ),
        ):
            original = _fixture(name)
            with self.subTest(name=name, duplicate=equivalent):
                self.assertEqual(extract_invoice(original + equivalent + "\n"), extract_invoice(original))
            for duplicate in invalid:
                with self.subTest(name=name, duplicate=duplicate):
                    self.assertIsNone(extract_invoice(original + duplicate + "\n"))

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

    def test_orange_issue_date_is_optional_but_must_parse_if_present(self):
        original = _fixture("orange_invoice.txt")
        for unknown_month in ("listopad", "İ", "lİstopada"):
            with self.subTest(month=unknown_month):
                self.assertIsNone(extract_invoice(original.replace("listopada", unknown_month)))
        self.assertEqual(extract_invoice(original.replace("listopada", "LISTOPADA")), extract_invoice(original))

        parsed = extract_invoice(original.replace("Data wystawienia: 14 listopada 2026\n", ""))
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.issue_date)
        self.assertEqual(parsed.invoice_number, "26110200000001")

    def test_play_issue_date_is_optional_but_must_parse_if_present(self):
        original = _fixture("play_invoice.txt")
        issue_line = "Data wystawienia\n08.11.2026\n"
        parsed = extract_invoice(original.replace(issue_line, ""))
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.issue_date)
        self.assertIsNone(extract_invoice(original.replace(issue_line, "Data wystawienia\nnie podano\n")))

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
        self.assertEqual(extract_invoice(original + conflict.ljust(513, "\u200b") + "\n"), extract_invoice(original))

        noisy_footer = "Numer Klienta: 0 Numer faktury: 501234567891 Okres rozliczeniowy: "
        footer_end = " Data wystawienia: 09.09.2026\n"
        repeated_size = 32768 - len((original + footer_end).encode("utf-8"))
        hostile = original + (noisy_footer * 512)[:repeated_size] + footer_end
        self.assertEqual(len(hostile.encode("utf-8")), 32768)
        self.assertEqual(extract_invoice(hostile), extract_invoice(original))

    def test_text_byte_limit_is_enforced_before_normalization(self):
        original = _fixture("tmobile_invoice.txt")
        for filler in ("x" * 32768, "\u00a0" * 16384):
            with self.subTest(filler_chars=len(filler)):
                self.assertIsNone(extract_invoice(original + filler))

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
        text = _RESPECT_FIXTURE.read_text(encoding="utf-8")
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
