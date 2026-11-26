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
