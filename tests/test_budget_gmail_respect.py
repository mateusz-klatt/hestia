"""Synthetic Respect Energy settlement itemization boundaries."""
from dataclasses import replace
import unittest
from unittest.mock import Mock

from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

from hestia.budget.gmail_document import extract_document
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_worker import _render, _validate_output
from tests.test_budget_gmail_message import GMAIL_HASH, MERCHANT, MESSAGE_ID, SECRET, _email, _metadata, _raw
from tests.test_budget_gmail_sync import SETTINGS
from tests.test_budget_gmail_worker import _pdf_text, _pdf_writer, _write_pdf

_POLISH_GLYPHS = (
    ("ą", "aogonek"), ("ć", "cacute"), ("ę", "eogonek"), ("ł", "lslash"),
    ("ń", "nacute"), ("ó", "oacute"), ("ś", "sacute"), ("ź", "zacute"), ("ż", "zdotaccent"),
)
_POLISH_BYTES = {char: bytes([128 + index]) for index, (char, _) in enumerate(_POLISH_GLYPHS)}


def _polish_pdf(text: str) -> bytes:
    encoded = []
    for line in text.splitlines():
        raw = bytearray()
        for char in line:
            if char in _POLISH_BYTES:
                raw.extend(_POLISH_BYTES[char])
            elif char in ("(", ")", "\\"):
                raw.extend(b"\\" + char.encode("ascii"))
            else:
                raw.extend(char.encode("ascii"))
        encoded.append(bytes(raw))
    writer = _pdf_writer(_pdf_text(*encoded))
    writer.pages[0]["/Resources"]["/Font"]["/F1"][NameObject("/Encoding")] = DictionaryObject({
        NameObject("/Type"): NameObject("/Encoding"),
        NameObject("/Differences"): ArrayObject([
            NumberObject(128), *(NameObject("/" + glyph) for _, glyph in _POLISH_GLYPHS),
        ]),
    })
    return _write_pdf(writer)


def _settlement_text(*, number="8800000042", energy_net="60,00", distribution_net="40,00",
                     printed_net="100,00", vat="23,00", gross="123,00", kwh="75,5"):
    return (
        "Twoja faktura rozliczeniowa\n"
        f"nr. {number} z dnia: 23.11.2026\n"
        "Sprzedawca: Respect Energy\n"
        f"Numer faktury: {number}\n"
        "Data wystawienia: 23.11.2026\n"
        "Termin płatności: 10.12.2026\n"
        f"Do zapłaty: {gross} zł\n"
        f"Rozliczenie {printed_net} 23 {vat} {gross}\n"
        f"a) Sprzedaż energii elektrycznej {energy_net} 23\n"
        f"b) Dystrybucja energii elektrycznej {distribution_net} 23\n"
        f"Energia czynna - Całodobowa 01.10.2026 - 31.10.2026 kWh {kwh}\n"
        f"Tytuł wpłaty: faktura {number}\n"
        f"Kwota: {gross} zł\n"
    )


class RespectSettlementTests(unittest.TestCase):
    def _process_pdf(self, pdf_bytes: bytes):
        settings = replace(SETTINGS, sender_allowlist=(MERCHANT,), hmac_secret=SECRET)
        worker = Mock()
        worker.parse.side_effect = lambda kind, payload: _validate_output(
            _render({"text": b"T", "html": b"H", "pdf": b"P"}[kind] + payload)
        )
        processor = GmailMessageProcessor(settings, worker)
        message = _email(MERCHANT, "Faktura rozliczeniowa w załączniku.")
        message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename="settlement.pdf")
        run = Mock()
        run.message.side_effect = [_metadata(message), _raw(message)]
        return processor.process(run, MESSAGE_ID, GMAIL_HASH)

    def test_valid_settlement_and_four_fractional_digit_quantity_itemize(self):
        text = _settlement_text(kwh="75,1234")
        document = extract_document(text, merchant="Respect Energy", pdf=True)
        self.assertEqual(
            [(item["raw_name"], item["quantity"], item["raw_unit"], item["net_line_total"])
             for item in document.items],
            [
                ("Energia elektryczna (netto)", "75.1234", "kWh", "60"),
                ("Dystrybucja energii elektrycznej (netto)", "1", None, "40"),
                ("VAT 23%", "1", None, "23"),
            ],
        )
        pending = self._process_pdf(_polish_pdf(text))
        self.assertEqual((pending.outcome, pending.source_kind, pending.invoice.amount), ("imported", "attachment", "123"))
        self.assertEqual(pending.invoice.items[0]["quantity"], "75.1234")

    def test_rejects_positions_when_energy_row_is_first_or_last_line(self):
        base = _settlement_text()
        energy_row = "a) Sprzedaż energii elektrycznej 60,00 23\n"
        without_energy = base.replace(energy_row, "")
        for text in (energy_row + without_energy, without_energy + energy_row):
            with self.subTest(first=text.startswith(energy_row)):
                document = extract_document(text, merchant="Respect Energy", pdf=True)
                self.assertEqual((document.invoice_number, document.amount, document.items),
                                 ("8800000042", "123", ()))

    def test_rejects_positions_for_arithmetically_consistent_non_23_vat_rate(self):
        for vat, gross, expected_amount in (("8,00", "108,00", "108"), ("5,00", "105,00", "105")):
            text = _settlement_text(vat=vat, gross=gross)
            with self.subTest(vat=vat):
                document = extract_document(text, merchant="Respect Energy", pdf=True)
                self.assertEqual((document.amount, document.items), (expected_amount, ()))
                pending = self._process_pdf(_polish_pdf(text))
                self.assertEqual((pending.outcome, pending.invoice.amount, pending.invoice.items),
                                 ("imported", expected_amount, ()))

    def test_rejects_positions_when_provided_quantity_has_five_fractional_digits(self):
        text = _settlement_text(kwh="75,12345")
        document = extract_document(text, merchant="Respect Energy", pdf=True)
        self.assertEqual((document.invoice_number, document.amount, document.items), ("8800000042", "123", ()))
        pending = self._process_pdf(_polish_pdf(text))
        self.assertEqual((pending.outcome, pending.source_kind, pending.invoice.amount, pending.invoice.items),
                         ("imported", "attachment", "123", ()))
