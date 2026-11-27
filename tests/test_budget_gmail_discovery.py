"""Discover structured, previously unknown documents from direct and forwarded MIME."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock

from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_store import DOCUMENT_NUMBER_DOMAIN, RFC_MESSAGE_DOMAIN, digest
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import WorkerUnavailable, _render, _validate_output
from tests.test_budget_gmail_message import (
    GMAIL_HASH,
    INVOICE,
    MESSAGE_ID,
    RFC_ID,
    SECRET,
    SPOUSE,
    _email,
    _metadata,
    _raw,
)
from tests.test_budget_gmail_respect import _polish_pdf, _settlement_text
from tests.test_budget_gmail_sync import SETTINGS
from tests.test_budget_gmail_worker import _pdf_text, _pdf_writer, _write_pdf

UNLISTED = "documents@cedar.example"
DOCUMENT = ("Faktura VAT nr TEST/42\nSprzedawca: Cedar Tools\nData wystawienia: 23.11.2026\n"
            "Suma: 7,50 PLN\n")
DOCUMENT_PDF = _write_pdf(_pdf_writer(_pdf_text(*DOCUMENT.encode("ascii").splitlines())))


class GmailDocumentDiscoveryTests(TestCase):
    def setUp(self):
        self.settings = replace(SETTINGS, hmac_secret=SECRET, spouse_forward_senders=(SPOUSE,),
                                require_sender_authentication=False)
        self.worker = Mock()
        self.worker.parse.side_effect = lambda kind, payload: _validate_output(
            _render({"text": b"T", "html": b"H", "pdf": b"P"}[kind] + payload)
        )
        self.processor = GmailMessageProcessor(self.settings, self.worker, discover_documents=True)

    def process(self, message):
        run = Mock()
        run.message.side_effect = [_metadata(message), _raw(message)]
        return self.processor.process(run, MESSAGE_ID, GMAIL_HASH)

    def test_same_document_discovered_from_plain_html_and_pdf(self):
        plain = _email(UNLISTED, DOCUMENT)
        html = _email(UNLISTED, "".join(f"<p>{line}</p>" for line in DOCUMENT.splitlines()), subtype="html")
        pdf = _email(UNLISTED, DOCUMENT)
        pdf.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf")
        for message, kind in ((plain, "email"), (html, "email"), (pdf, "attachment")):
            with self.subTest(kind=message.get_content_type()):
                pending = self.process(message)
                self.assertTrue(pending.card_required)
                self.assertEqual((pending.outcome, pending.source_kind), ("imported", kind))
                self.assertEqual((pending.invoice.issuer, pending.invoice.amount, pending.invoice.issue_date),
                                 ("Cedar Tools", "7.5", "2026-11-23"))
                self.assertEqual(pending.document_provenance["number_hmac"],
                                 digest(SECRET, DOCUMENT_NUMBER_DOMAIN, "test/42"))
                self.assertEqual(pending.rfc_message_id_hmac,
                                 digest(SECRET, RFC_MESSAGE_DOMAIN, "Invoice-42@respect.example"))

    def test_forwarded_origin_and_inline_attachment_share_provenance(self):
        original = _email(UNLISTED, DOCUMENT)
        attached = _email(SPOUSE, "Cover message", rfc_id="<forward@spouse.example>")
        attached.add_attachment(original)
        inline = _email(SPOUSE, f"Forwarded message:\nFrom: {UNLISTED}\nMessage-ID: {RFC_ID}\n\nSee the PDF")
        inline.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf")
        for message in (attached, inline):
            with self.subTest(subject=message.get_body().get_content()):
                pending = self.process(message)
                self.assertEqual((pending.trust_path, pending.card_required, pending.forwarded_manually),
                                 ("spouse_forward", True, False))
                self.assertEqual(pending.invoice.issuer, "Cedar Tools")
                self.assertEqual(pending.rfc_message_id_hmac,
                                 digest(SECRET, RFC_MESSAGE_DOMAIN, "Invoice-42@respect.example"))
        missing = _email(SPOUSE, f"Forwarded message:\nFrom: {UNLISTED}\n\nHere's your document link.")
        pending = self.process(missing)
        self.assertEqual((pending.forwarded_manually, pending.card_required, pending.invoice), (True, False, None))

    def test_spouse_forward_with_only_the_pdf_and_no_original_headers(self):
        for cover in ("Przesyłam nasz rachunek.", "----- Forwarded message -----\nW załączniku."):
            outer = _email(SPOUSE, cover, rfc_id="<forwarding-cover@example.test>")
            outer["Subject"] = "Fwd: Faktura"
            outer.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf", filename="Faktura.pdf")
            with self.subTest(cover=cover):
                pending = self.process(outer)
                self.assertEqual((pending.trust_path, pending.card_required, pending.invoice.issuer, pending.source_kind),
                                 ("spouse_forward", True, "Cedar Tools", "attachment"))
                self.assertEqual(pending.rfc_message_id_hmac,
                                 digest(SECRET, RFC_MESSAGE_DOMAIN, "forwarding-cover@example.test"))
        self.assertIsNone(self.process(_email(SPOUSE, DOCUMENT)).invoice)
        outer.replace_header("Subject", "Potwierdzenie anulowania zamówienia")
        self.assertIsNone(self.process(outer).invoice)

    def test_known_document_and_payment_senders_fall_back_to_structured_discovery(self):
        sender = PaymentSender(UNLISTED, "Configured Billing Name")
        for field in ("document_senders", "payment_senders"):
            settings = replace(self.settings, **{field: (sender,)})
            self.processor = GmailMessageProcessor(settings, self.worker, discover_documents=True)
            if field == "document_senders":
                message = _email(UNLISTED, DOCUMENT)
            else:
                message = _email(UNLISTED, "Your digital invoice is attached.")
                message.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf")
            with self.subTest(field=field):
                pending = self.process(message)
                self.assertEqual((pending.trust_path, pending.card_required, pending.invoice.issuer),
                                 ("direct_allowlist", True, "Cedar Tools"))

    def test_conflicting_document_evidence_and_status_notices_cannot_create_a_candidate(self):
        conflict = _email(UNLISTED, DOCUMENT.replace("7,50", "8,00"))
        conflict.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf")
        notice = _email(UNLISTED, DOCUMENT)
        notice["Subject"] = "Potwierdzenie anulowania zamówienia"
        refund = _email(UNLISTED, DOCUMENT)
        refund["Subject"] = "Refund issued"
        quote = _email(UNLISTED, DOCUMENT)
        quote["Subject"] = "Pro forma invoice"
        body_notice = _email(UNLISTED, "Payment failed\n" + DOCUMENT)
        for message in (conflict, notice, refund, quote, body_notice, _email(UNLISTED, "A meeting tomorrow"),
                        _email(UNLISTED, DOCUMENT.replace("Sprzedawca: Cedar Tools\n", ""))):
            with self.subTest(subject=message["Subject"]):
                result = self.process(message)
                self.assertFalse(result.card_required)
                self.assertIsNone(result.invoice)

    def test_known_issuer_template_still_needs_a_date_and_bank_corroboration(self):
        pending = self.process(_email(UNLISTED, INVOICE))
        self.assertTrue(pending.card_required)
        self.assertEqual(pending.invoice.issuer, "Respect Energy")
        self.assertIsNone(self.process(_email(UNLISTED, INVOICE.replace("Data wystawienia: 21.11.2026\n", ""))).invoice)
        self.assertIsNone(self.process(_email(UNLISTED, INVOICE.replace("RE/2026/00042", "TEST/42"))).invoice)

    def test_reversal_notices_in_subject_or_body_cannot_create_purchases(self):
        for notice in (
            "Potwierdzenie anulowania zamówienia", "Anulowanie zamówienia TEST/42",
            "Informujemy, że zamówienie zostało anulowane.", "Anulowane: TEST/42",
            "Zwrot środków: TEST/42", "Zwrot: TEST/42", "Otrzymaliśmy zwrot towaru.",
            "Dokonano zwrotu",
            "Korekta: TEST/42", "Faktura korygująca TEST/42",
            "Your order has been cancelled", "Order canceled", "Cancellation confirmation",
            "Refund", "We processed your refund", "Your return has been approved",
            "Return #TEST/42", "Credit note TEST/42", "Corrected invoice TEST/42",
        ):
            for field in ("subject", "body"):
                with self.subTest(notice=notice, field=field):
                    message = _email(UNLISTED, DOCUMENT + ("\n" + notice if field == "body" else ""))
                    message["Subject"] = notice if field == "subject" else "Faktura TEST/42"
                    result = self.process(message)
                    self.assertEqual((result.outcome, result.card_required, result.invoice),
                                     ("skipped_non_receipt", False, None))

    def test_amount_bearing_refund_notice_in_body_cannot_create_a_purchase(self):
        for notice in (
            "Zwrot w kwocie 43,20 PLN",
            "Zwrot środków w kwocie 43,20 PLN",
            "Refund of 43.20 PLN",
            "Refund amount: PLN 43.20",
            "Your refund of 43.20 PLN",
            "Your refund of 43.20 PLN has been processed",
            "Refund of 43.20 PLN has been processed",
            "Refund of 43.20 PLN has been issued",
            "Dokonano zwrotu w kwocie 43,20 PLN",
            "Dokonano zwrotu środków w kwocie 43,20 PLN",
            "Zwrot w kwocie 43,20 PLN został zlecony",
            "Zwrot środków w kwocie 43,20 PLN został wykonany",
            "Zwrot w kwocie 43,20 PLN został zrealizowany",
        ):
            with self.subTest(notice=notice):
                result = self.process(_email(UNLISTED, DOCUMENT + "\n" + notice))
                self.assertEqual((result.outcome, result.card_required, result.invoice),
                                 ("skipped_non_receipt", False, None))

    def test_purchase_with_optional_cancellation_or_return_instructions_is_discovered(self):
        footer = ("\nMożesz anulować zamówienie i otrzymać zwrot środków.\n"
                  "Jeśli zamówienie zostało anulowane, sprawdź status w swoim koncie.\n"
                  "Cancellation policy: You can cancel your order within 24 hours.\n"
                  "Return policy: You can return unopened items within 14 days.\n"
                  "You can request a refund of 7.50 PLN if the order is canceled.\n"
                  "If your refund of 7.50 PLN has been processed, allow three days.\n"
                  "Jeśli dokonano zwrotu środków w kwocie 7,50 PLN, sprawdź konto.\n"
                  "Jeśli zwrot w kwocie 7,50 PLN jest potrzebny, skontaktuj się z nami.\n")
        result = self.process(_email(UNLISTED, DOCUMENT + footer))
        self.assertEqual((result.outcome, result.card_required, result.invoice.amount), ("imported", True, "7.5"))

    def test_conflicting_payment_labels_do_not_lose_their_scope(self):
        for conflict in ("Visa ending in 9876", "Metoda płatności: BLIK"):
            with self.subTest(conflict=conflict):
                body = DOCUMENT + "\nVisa ending in 0123\n" + conflict
                pending = self.process(_email(UNLISTED, body))
                self.assertTrue(pending.card_required)
                self.assertEqual((pending.invoice.card_last4, pending.invoice.payment_method), (None, "mixed"))

    def test_explicit_seller_takes_precedence_over_issuer_name_in_buyer_field(self):
        body = DOCUMENT + ("Numer faktury: TEST/42\nKwota brutto: 7,50 PLN\n"
                           "Termin płatności: 29.11.2026\nNabywca: Respect Energy\n")
        self.assertEqual(self.process(_email(UNLISTED, body)).invoice.issuer, "Cedar Tools")

    def test_unlisted_direct_sender_waits_for_card_with_or_without_dkim(self):
        signed = _email(UNLISTED, DOCUMENT)
        unsigned = _email(UNLISTED, DOCUMENT)
        del unsigned["Authentication-Results"]
        for message in (signed, unsigned):
            with self.subTest(signed="Authentication-Results" in message):
                pending = self.process(message)
                self.assertEqual((pending.card_required, pending.invoice.issuer), (True, "Cedar Tools"))
        strict = replace(self.settings, require_sender_authentication=True)
        self.processor = GmailMessageProcessor(strict, self.worker, discover_documents=True)
        self.assertEqual(self.process(signed).outcome, "skipped_untrusted_sender")

    def test_invalid_or_unreadable_documents_cannot_hide_second_invoice(self):
        for attachment in (b"%PDF-damaged", _write_pdf(_pdf_writer(None))):
            message = _email(UNLISTED, DOCUMENT)
            message.add_attachment(attachment, maintype="application", subtype="pdf")
            with self.subTest(size=len(attachment)):
                self.assertFalse(self.process(message).card_required)
        dense = _email(UNLISTED, DOCUMENT)
        for _ in range(8):
            dense.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf")
        self.worker.reset_mock()
        self.assertIsNone(self.process(dense).invoice)
        self.worker.parse.assert_not_called()

    def test_empty_body_is_skipped_and_worker_outage_is_retryable(self):
        from hestia.budget.gmail_client import GmailError

        message = _email(UNLISTED)
        message.set_content(b"", maintype="text", subtype="plain")
        message.add_attachment(DOCUMENT_PDF, maintype="application", subtype="pdf")
        self.assertTrue(self.process(message).card_required)
        self.worker.parse.side_effect = WorkerUnavailable()
        with self.assertRaisesRegex(GmailError, "parser_unavailable"):
            self.process(message)

    def test_two_pdfs_of_same_document_keep_matching_and_drop_differing_positions(self):
        base_text = _settlement_text()
        base_pdf = _polish_pdf(base_text)
        alt_split_pdf = _polish_pdf(_settlement_text(energy_net="59,00", distribution_net="41,00"))
        alt_kwh_pdf = _polish_pdf(_settlement_text(kwh="76,0"))
        summary_text = "\n".join(base_text.splitlines()[:7]) + "\n"
        summary_pdf = _polish_pdf(summary_text)

        matching = _email(UNLISTED, "")
        for attachment in (base_pdf, base_pdf):
            matching.add_attachment(attachment, maintype="application", subtype="pdf")
        kept = self.process(matching)
        self.assertEqual((kept.outcome, kept.card_required, kept.source_kind), ("imported", True, "attachment"))
        self.assertEqual(
            [(item["raw_name"], item["quantity"], item["raw_unit"], item["net_line_total"])
             for item in kept.invoice.items],
            [
                ("Energia elektryczna (netto)", "75.5", "kWh", "60"),
                ("Dystrybucja energii elektrycznej (netto)", "1", None, "40"),
                ("VAT 23%", "1", None, "23"),
            ],
        )

        for label, attachments, body in (
            ("different_split", (base_pdf, alt_split_pdf), ""),
            ("different_kwh", (base_pdf, alt_kwh_pdf), ""),
            ("itemized_plus_summary", (base_pdf, summary_pdf), ""),
            ("summary_plus_itemized", (summary_pdf, base_pdf), ""),
            ("body_plus_differing_pdfs", (base_pdf, alt_split_pdf), summary_text),
        ):
            with self.subTest(label=label):
                message = _email(UNLISTED, body)
                for attachment in attachments:
                    message.add_attachment(attachment, maintype="application", subtype="pdf")
                pending = self.process(message)
                self.assertEqual((pending.outcome, pending.card_required, pending.source_kind),
                                 ("imported", True, "attachment"))
                self.assertEqual((pending.invoice.issuer, pending.invoice.invoice_number, pending.invoice.amount),
                                 ("Respect Energy", "8800000042", "123"))
                self.assertEqual(pending.invoice.items, ())
