"""Gmail message processing against synthetic metadata, MIME, and worker fixtures."""
import base64
from datetime import UTC, date, datetime
from email.message import EmailMessage
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from hestia.budget import gmail_message
from hestia.budget.gmail_client import GmailError
from hestia.budget.gmail_invoice import ImportedInvoice
from hestia.budget.gmail_store import (ORDER_REFERENCE_DOMAIN, PAYMENT_PAYEE_DOMAIN, PAYMENT_SENDER_DOMAIN,
                                      RFC_MESSAGE_DOMAIN, digest)
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import PDFUnsupported, WorkerRejected, WorkerUnavailable, _render, _validate_output


MERCHANT = "synthetic-billing@respectenergy.pl"
SPOUSE = "spouse@gmail.com"
OWNER = "owner@gmail.com"
MESSAGE_ID = "syntheticGmailId"
GMAIL_HASH = "a" * 64
SECRET = b"synthetic-key-for-rfc-message-dedupe-only"
RFC_ID = "<Invoice-42@RESPECT.EXAMPLE>"
INVOICE = (
    "Wystawca: Respect Energy\n"
    "Numer dokumentu: RE/2026/00042\n"
    "Kwota: 123,45 PLN\n"
    "Data wystawienia: 21.11.2026\n"
    "Termin płatności: 2026-12-05\n"
)
PDF_TEXT = Path(__file__).parent / "fixtures" / "gmail" / "pdf_text"


SHOP_TABLE = """Zamówione produkty
Kolorowe kredki (8 szt.)
Kod produktu: 31415
Ilość: 2 x
Cena za sztukę: 3,40 zł
2x
6,80 zł
Blok rysunkowy (1 szt.)
Kod produktu: 31416
Ilość: 1 x
Cena za sztukę: 7,50 zł
1x
7,50 zł
Wartość zamówienia
14,30 zł
Łącznie
14,30 zł
"""

def _email(sender=MERCHANT, text=INVOICE, *, rfc_id=RFC_ID, **body_options):
    domain = sender.rpartition("@")[2]
    message = EmailMessage()
    message["From"] = sender
    message["Received"] = "from fixture.example"
    message["Authentication-Results"] = (
        f"mx.google.com; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
    )
    if rfc_id is not None:
        message["Message-ID"] = rfc_id
    message.set_content(text, **body_options)
    return message


def _metadata(message=None, *, size=200):
    message = message if message is not None else _email()
    return {"id": MESSAGE_ID, "internalDate": "1795566945123", "sizeEstimate": size,
            "payload": {"headers": [
                {"name": name, "value": value} for name, value in message.raw_items()
                if name in {"From", "Sender", "Authentication-Results", "DKIM-Signature"}
            ]}}


def _raw(message):
    raw = message if isinstance(message, bytes) else message.as_bytes()
    return {"id": MESSAGE_ID, "raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")}


class GmailMessageProcessorTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            sender_allowlist=(MERCHANT,), spouse_forward_senders=(SPOUSE,), hmac_secret=SECRET,
            known_merchant_domains=("respectenergy.pl",), require_known_forwarded_merchant=True,
            owner_mailbox=OWNER,
        )
        self.worker = Mock()
        self.worker.ready.return_value = True
        self.worker.parse.side_effect = lambda _kind, source: source.decode("utf-8")
        self.processor = gmail_message.GmailMessageProcessor(self.settings, self.worker)

    def _run(self, *responses):
        run = Mock()
        run.message.side_effect = responses
        return run

    def _process(self, *responses):
        return self.processor.process(self._run(*responses), MESSAGE_ID, GMAIL_HASH)

    def test_imports_only_structured_fields_and_rfc_hmac_from_a_direct_invoice(self):
        message = _email(charset="iso-8859-2", cte="base64")
        run = self._run(_metadata(message), _raw(message))
        self.assertTrue(self.processor.ready())
        pending = self.processor.process(run, MESSAGE_ID, GMAIL_HASH)
        self.assertEqual(run.message.call_args_list, [call(MESSAGE_ID), call(MESSAGE_ID, raw=True)])
        self.worker.parse.assert_called_once_with("text", INVOICE.encode("utf-8"))
        self.assertEqual(pending.trust_path, "direct_allowlist")
        self.assertEqual(pending.outcome, "imported")
        self.assertEqual(pending.gmail_id_hmac, GMAIL_HASH)
        self.assertEqual(pending.rfc_message_id_hmac, digest(SECRET, RFC_MESSAGE_DOMAIN, "Invoice-42@respect.example"))
        self.assertEqual(pending.internal_date, 1795566945.123)
        self.assertEqual(pending.source_kind, "email")
        self.assertEqual(pending.invoice, ImportedInvoice("Respect Energy", None, "RE/2026/00042", "123.45",
                                                          "PLN", "2026-11-21", "2026-12-05"))

    def _payment_email(self, text, **body_options):
        self.settings.payment_senders = (
            PaymentSender("@shop.example.test", "Example Shop"),
            PaymentSender("@gateway.example.test", "Example Gateway", gateway=True),
            PaymentSender("@market.example.test", "Example Market"),
        )
        return _email("orders@billing.shop.example.test", text, **body_options)

    def test_cancellation_notice_skips_new_purchase_for_direct_and_forwarded_original(self):
        notice = "Potwierdzenie anulowania zamówienia we Example"
        inner = self._payment_email("Wartość zamówienia: 21,90 PLN")
        self.settings.document_senders = self.settings.payment_senders
        inner["Subject"] = notice
        inner.add_attachment(b"%PDF-synthetic", maintype="application", subtype="pdf")
        attached = _email(OWNER, "Notatka do zamówienia.")
        attached.add_attachment(inner)
        encoded = notice.replace("ó", "&amp;oacute;")
        inline = _email(SPOUSE, "Notatka.\n---------- Forwarded message ---------\n"
                        f"From: {inner['From']}\nSubject: {encoded}\n\nWartość zamówienia: 21,90 PLN")
        for message in (inner, attached, inline):
            with self.subTest(sender=message["From"]):
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.invoice, pending.payment, pending.forwarded_manually),
                                 ("skipped_non_receipt", None, None, False))
        self.worker.parse.assert_not_called()

        purchase = self._payment_email("Wartość zamówienia: 21,90 PLN")
        purchase["Subject"] = "Zamówienie opłacone"
        cover = _email(OWNER, "Proszę sprawdzić.")
        cover["Subject"] = notice
        cover.add_attachment(purchase)
        self.assertEqual(self._process(_metadata(cover), _raw(cover)).payment.fields.total_amount, "21.9")

    def test_order_cancellation_preserves_gateway_terminal_failure(self):
        self.settings.payment_senders = (PaymentSender("@gateway.example.test", "PayNow", gateway=True,
                                                      provider="paynow"),)
        body = ("Twoja transakcja AB12-CDE-FGH-IJK straciła ważność.\n"
                "Kwota transakcji: 21,90 PLN")
        inner = _email("alert@gateway.example.test", body)
        inner["Subject"] = "Potwierdzenie anulowania zamówienia"
        outer = _email(OWNER, "Proszę sprawdzić.")
        outer.add_attachment(inner)
        for message in (inner, outer):
            pending = self._process(_metadata(message), _raw(message))
            self.assertEqual((pending.outcome, pending.payment, pending.failed_gateway[0]),
                             ("skipped_non_receipt", None, "PayNow"))

    def test_direct_order_confirmation_prefers_amount_due_over_order_subtotal_via_worker(self):
        self.worker.parse.side_effect = lambda kind, source: _validate_output(_render(
            {"text": b"T", "html": b"H"}[kind] + source))
        plain = (
            "Dziękujemy za zamówienie nr 990100200.\n"
            "Wartość zamówienia\n"
            "40,00 PLN\n"
            "Koszt dostawy\n"
            "4,00 PLN\n"
            "Kaucja\n"
            "1,00 PLN\n"
            "Kwota do zapłaty\n"
            "45,00 PLN\n"
        )
        html = (
            "<html><body>"
            "<p>Dziękujemy za zamówienie nr 990100200.</p>"
            "<table>"
            "<tr><td>Wartość zamówienia</td><td>40,00&nbsp;zł</td></tr>"
            "<tr><td>Koszt dostawy</td><td>4,00&nbsp;zł</td></tr>"
            "<tr><td>Kaucja</td><td>1,00&nbsp;zł</td></tr>"
            "<tr><td><strong>Kwota do zapłaty</strong></td><td><strong>45,00&nbsp;zł</strong></td></tr>"
            "</table>"
            "</body></html>"
        )
        for subtype, body in (("plain", plain), ("html", html)):
            with self.subTest(subtype=subtype):
                message = self._payment_email(body, subtype=subtype)
                message["Subject"] = "Potwierdzenie zamówienia nr 990100200"
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome, pending.source_kind),
                                 ("direct_allowlist", "imported", "email"))
                self.assertFalse(pending.payment.gateway)
                self.assertEqual((pending.payment.fields.merchant, pending.payment.fields.total_amount,
                                  pending.payment.fields.currency),
                                 ("Example Shop", "45", "PLN"))
                self.assertEqual(pending.payment.order_reference_hmac,
                                 digest(SECRET, ORDER_REFERENCE_DOMAIN, "990100200"))

        cancelled = self._payment_email(html, subtype="html")
        cancelled["Subject"] = "Potwierdzenie anulowania zamówienia nr 990100200"
        skipped = self._process(_metadata(cancelled), _raw(cancelled))
        self.assertEqual((skipped.outcome, skipped.payment), ("skipped_non_receipt", None))

    def test_itemized_shop_table_in_direct_and_forwarded_mail(self):
        inner = self._payment_email(SHOP_TABLE)
        outer = _email(OWNER, "Notatka właściciela")
        outer.add_attachment(inner)
        for message, trust_path in ((inner, "direct_allowlist"), (outer, "owner_forward")):
            with self.subTest(trust_path=trust_path):
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.trust_path), ("imported", trust_path))
                self.assertEqual((pending.payment.fields.total_amount, pending.payment.fields.currency),
                                 ("14.3", "PLN"))
                self.assertEqual([(item["raw_name"], item["quantity"], item["unit_price_raw"], item["net_line_total"])
                                  for item in pending.payment.items],
                                 [("Kolorowe kredki (8 szt.)", "2", "3.4", "6.8"),
                                  ("Blok rysunkowy (1 szt.)", "1", "7.5", "7.5")])

    def test_shop_alternative_checks_item_identity_and_keeps_summary_fast_path(self):
        self.worker.parse.side_effect = lambda kind, source: _validate_output(_render(
            {"text": b"T", "html": b"H"}[kind] + source))
        for plain, html, item_count, renders in (
                (SHOP_TABLE, SHOP_TABLE, 2, 2),
                (SHOP_TABLE, SHOP_TABLE.replace("Blok rysunkowy", "Szkicownik"), 0, 2),
                (SHOP_TABLE, SHOP_TABLE.replace("3,40 zł", "3,39 zł"), 0, 2),
                (SHOP_TABLE.replace("3,40 zł", "3,39 zł"), SHOP_TABLE, 0, 1),
                (SHOP_TABLE, "Łącznie: 14,30 zł", 2, 2),
                ("Łącznie: 14,30 zł", SHOP_TABLE, 0, 1)):
            with self.subTest(item_count=item_count, renders=renders):
                message = self._payment_email(plain)
                cells = "".join(f"<tr><td>{line}</td></tr>" for line in html.splitlines())
                message.add_alternative(f"<table>{cells}</table>", subtype="html")
                self.worker.parse.reset_mock()
                payment = self._process(_metadata(message), _raw(message)).payment
                self.assertEqual((payment.fields.total_amount, len(payment.items)), ("14.3", item_count))
                self.assertEqual(self.worker.parse.call_count, renders)

    def test_payment_gateway_cannot_supply_shop_items(self):
        self.settings.payment_senders = (PaymentSender("@gateway.example.test", "Example Gateway", gateway=True),)
        message = _email("shop@gateway.example.test", SHOP_TABLE)
        with patch.object(gmail_message, "inspect_shop_items", side_effect=AssertionError("Unexpected shop parser")):
            pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.outcome, pending.payment.fields.total_amount, pending.payment.items),
                         ("imported", "14.3", ()))

    def test_payment_uses_warsaw_internal_date_and_decoded_subject(self):
        message = self._payment_email("Amount paid: 123,45 PLN\nDate paid: 2026-11-22")
        message["Subject"] = "=?utf-8?q?Op=C5=82ata_za_us=C5=82ug=C4=99?="
        metadata = _metadata(message)
        metadata["internalDate"] = str(int(datetime(2026, 11, 24, 23, 30, tzinfo=UTC).timestamp() * 1000))
        pending = self._process(metadata, _raw(message))
        self.assertEqual((pending.trust_path, pending.outcome, pending.source_kind, pending.invoice),
                         ("direct_allowlist", "imported", "email", None))
        self.assertEqual(pending.payment.receipt_date, date(2026, 11, 25))
        self.assertEqual((pending.payment.fields.merchant, pending.payment.fields.description,
                          pending.payment.fields.currency, pending.payment.fields.total_amount),
                         ("Example Shop", "Opłata za usługę", "PLN", "123.45"))
        self.assertEqual(pending.payment.sender_hmac, digest(SECRET, PAYMENT_SENDER_DOMAIN, "@shop.example.test"))
        self.assertIsNone(pending.payment.order_reference_hmac)
        self.assertIsNone(pending.payment.payee_hmac)

    def test_long_gateway_summary_uses_card_charge_and_cross_checks_paid_heading_date(self):
        body = (Path(__file__).parent / "fixtures" / "gmail" / "payment" / "saas_gateway_long_url.txt").read_text(
            encoding="utf-8",
        )
        self.assertGreater(len(body.splitlines()[1]), 512)
        self.settings.payment_senders = (PaymentSender("@gateway.example.test", "Example Gateway", gateway=True),)
        message = _email("receipts@gateway.example.test", body)
        message["Subject"] = "Receipt #3333-4444"

        def process_on(day):
            internal_date = datetime(2026, 12, day, tzinfo=UTC)
            metadata = {**_metadata(message), "internalDate": str(int(internal_date.timestamp() * 1000))}
            return self._process(metadata, _raw(message))

        pending = process_on(11)
        self.assertEqual(pending.outcome, "imported")
        self.assertEqual(pending.payment.receipt_date, date(2026, 12, 11))
        fields = pending.payment.fields
        self.assertEqual((fields.merchant, fields.total_amount, fields.currency, fields.body_dates),
                         ("Example Render Studio", "66.19", "PLN", (date(2026, 12, 8),)))
        self.assertEqual(pending.payment.payee_hmac, digest(SECRET, PAYMENT_PAYEE_DOMAIN, "example render studio"))
        pending = process_on(12)
        self.assertEqual((pending.outcome, pending.payment), ("skipped_non_receipt", None))

    def test_payment_gateway_marks_actual_config_fallback(self):
        self.settings.payment_senders = (PaymentSender("@gateway.example.test", "Example Gateway", gateway=True),)
        for payee_line, fell_back in (("", True), ("\nOdbiorca: Example Gateway", False)):
            with self.subTest(fell_back=fell_back):
                message = _email("receipts@gateway.example.test", f"Total: 12 PLN{payee_line}")
                payment = self._process(_metadata(message), _raw(message)).payment
                self.assertEqual(payment.fields.merchant, "Example Gateway")
                self.assertIs(payment.fields.payee_from_config, fell_back)
                self.assertEqual(payment.payee_hmac, None if fell_back else digest(
                    SECRET, PAYMENT_PAYEE_DOMAIN, "example gateway",
                ))

    def test_payment_order_reference_crosses_processing_boundary_only_as_hmac(self):
        message = self._payment_email("Amount paid: 123 PLN\nTytuł: Example purchase")
        message["Subject"] = "Potwierdzenie zamówienia nr 00000000001"
        pending = self._process(_metadata(message), _raw(message))
        self.assertEqual(pending.outcome, "imported")
        self.assertEqual(pending.payment.order_reference_hmac, digest(SECRET, ORDER_REFERENCE_DOMAIN, "00000000001"))
        self.assertIsNone(pending.payment.fields.order_reference)
        self.assertEqual(pending.payment.fields.description, "Example purchase")

    def test_payment_checks_all_decoded_subject_headers_for_rejection(self):
        message = self._payment_email("Amount paid: 123 PLN")
        message["Subject"] = "Example payment"
        raw = message.as_bytes().replace(
            b"Subject: Example payment\n", b"Subject: Example payment\nsUbJeCt: =?utf-8?q?Ref=00und?=\n",
        )
        pending = self._process(_metadata(message), _raw(raw))
        self.assertEqual((pending.outcome, pending.payment), ("skipped_non_receipt", None))

    def test_payment_rejects_distant_dates_and_unrepresentable_internal_date(self):
        for header, internal_date in (("Data: 2026-11-21", "1795566945123"),
                                      ("Data: 2026-11-29", "1795566945123"),
                                      ("Data: 2026-11-25", "9999999999999999999")):
            with self.subTest(header=header, internal_date=internal_date):
                message = self._payment_email(f"Amount paid: 1 PLN\n{header}")
                pending = self._process({**_metadata(message), "internalDate": internal_date}, _raw(message))
                self.assertEqual((pending.outcome, pending.payment), ("skipped_non_receipt", None))

    def test_explicit_plain_payment_rejection_takes_precedence_over_html(self):
        for body in ("Payment failed", "Total: -5 PLN", "Total: 1 PLN\nDo zapłaty: 2 PLN", "Amount paid: 0,00 € EUR"):
            with self.subTest(body=body):
                self.worker.parse.reset_mock()
                message = self._payment_email(body)
                message.add_alternative("Amount paid: 123 PLN", subtype="html")
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.payment), ("skipped_non_receipt", None))
                self.worker.parse.assert_called_once_with("text", (body + "\n").encode("utf-8"))

    def test_payment_uses_valid_plain_total_before_html(self):
        message = self._payment_email("Amount paid: 25 PLN")
        message.add_alternative("Amount paid: 123 PLN", subtype="html")
        pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.outcome, pending.payment.fields.total_amount), ("imported", "25"))
        self.worker.parse.assert_called_once_with("text", b"Amount paid: 25 PLN\n")

    def test_payment_uses_html_for_missing_total_or_unreadable_plain_body(self):
        for plain in ("missing payload", "empty render", "bad charset", "no total"):
            with self.subTest(plain=plain):
                self.worker.parse.reset_mock()
                body = "Example receipt stub\n" if plain == "no total" else " \n"
                message = self._payment_email(body)
                message.add_alternative("Amount paid: 123 PLN", subtype="html")
                part = message.get_body(("plain",))
                if plain == "missing payload":
                    part.set_payload("")
                elif plain == "bad charset":
                    part.set_param("charset", "unknown-encoding")
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.payment.fields.total_amount), ("imported", "123"))
                worker_calls = [call("html", b"Amount paid: 123 PLN\n")]
                if plain in ("empty render", "no total"):
                    worker_calls.insert(0, call("text", body.encode("utf-8")))
                self.assertEqual(self.worker.parse.call_args_list, worker_calls)

    def test_payment_oversized_and_rejected_body_use_existing_fixed_outcomes(self):
        message = self._payment_email("synthetic private payment message")
        with patch.object(gmail_message, "MAX_HTML_BYTES", 8):
            pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("skipped_oversized", None))
        self.worker.parse.assert_not_called()

        self.worker.parse.side_effect = WorkerRejected("synthetic private reason")
        with self.assertLogs(gmail_message.log, level="WARNING") as logs:
            pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("parse_failed", "worker_rejected"))
        self.assertEqual(logs.output, [
            "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=worker_rejected",
        ])

    def test_payment_sender_does_not_pass_pdfs_to_worker_or_use_invoice_parser(self):
        message = self._payment_email(INVOICE)
        message.set_content(b"%PDF-1.4 synthetic", maintype="application", subtype="pdf")
        self.assertEqual(self._process(_metadata(message), _raw(message)).outcome, "skipped_non_receipt")
        self.worker.parse.assert_not_called()

        message = self._payment_email(INVOICE)
        with patch.object(gmail_message, "extract_invoice") as invoice_parser:
            pending = self._process(_metadata(message), _raw(message))
        invoice_parser.assert_not_called()
        self.assertEqual((pending.outcome, pending.invoice), ("imported", None))

    def test_forwarded_payment_parses_inner_message_and_falls_back_to_manual_on_reject_or_missing_total(self):
        inner_id = "<inner-shop-42@shop.example.test>"
        outer_id = "<outer-forward-99@gmail.com>"
        original = self._payment_email("Amount paid: 123 PLN", rfc_id=inner_id)
        original["Subject"] = "Order #800100200"
        for forwarder, expected_trust in ((SPOUSE, "spouse_forward"), (OWNER, "owner_forward")):
            with self.subTest(trust_path=expected_trust):
                message = _email(forwarder, "Synthetic cover note", rfc_id=outer_id)
                message.add_attachment(original)
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                                  pending.source_kind, pending.invoice),
                                 (expected_trust, "imported", False, "email", None))
                self.assertEqual((pending.payment.fields.merchant, pending.payment.fields.total_amount),
                                 ("Example Shop", "123"))
                self.assertEqual(pending.rfc_message_id_hmac,
                                 digest(SECRET, RFC_MESSAGE_DOMAIN, "inner-shop-42@shop.example.test"))

        # Missing total or explicit rejection on a matched payment sender falls back to manual review.
        for body in ("No amount in this message", "Payment failed\nAmount paid: 123 PLN"):
            with self.subTest(fallback_body=body):
                inner = self._payment_email(body, rfc_id=inner_id)
                message = _email(SPOUSE, "Synthetic cover note", rfc_id=outer_id)
                message.add_attachment(inner)
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                                  pending.sender_domain, pending.payment, pending.rfc_message_id_hmac),
                                 ("spouse_forward", "imported", True, "billing.shop.example.test", None, None))

        # Attached message/rfc822 forwards never fall back to the outer cover note's HTML part.
        attached_no_total = self._payment_email("No amount in this message", rfc_id=inner_id)
        outer_with_html = _email(SPOUSE, "Cover note", rfc_id=outer_id)
        outer_with_html.add_alternative("<p>Amount paid: 999 PLN</p>", subtype="html")
        outer_with_html.add_attachment(attached_no_total)
        pending = self._process(_metadata(outer_with_html), _raw(outer_with_html))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.payment), ("imported", True, None))

    def test_inline_forwarded_payment_falls_back_to_outer_html_only_on_missing_total(self):
        self.settings.payment_senders = (PaymentSender("@shop.example.test", "Example Shop"),)
        inner_id = "<inline-shop-42@shop.example.test>"
        plain = (
            "Note: 999 PLN\n"
            "---------- Forwarded message ---------\n"
            "From: Example Shop <orders@shop.example.test>\n"
            "Subject: Order #800100200\n"
            f"Message-ID: {inner_id}\n\n"
            "Order confirmed without table."
        )
        html = (
            "<div>Note: Amount paid: 999 PLN</div>"
            "<div>---------- Forwarded message ---------<br>"
            "From: Example Shop &lt;orders@shop.example.test&gt;<br>"
            "Subject: Order #800100200<br>"
            f"Message-ID: {inner_id}</div>"
            "<p>Razem: 42,50 PLN</p>"
        )
        message = _email(OWNER, plain, rfc_id="<outer-inline@gmail.com>")
        message.add_alternative(html, subtype="html")
        self.worker.parse.side_effect = lambda kind, source: (
            source.decode("utf-8").replace("<br>", "\n").replace("<div>", "\n").replace("</div>", "\n")
            .replace("<p>", "\n").replace("</p>", "\n").replace("&lt;", "<").replace("&gt;", ">")
            if kind == "html" else source.decode("utf-8")
        )
        pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                          pending.payment.fields.merchant, pending.payment.fields.total_amount),
                         ("owner_forward", "imported", False, "Example Shop", "42.5"))
        self.assertEqual(pending.rfc_message_id_hmac,
                         digest(SECRET, RFC_MESSAGE_DOMAIN, "inline-shop-42@shop.example.test"))

        # Zero or multiple markers in the rendered outer HTML skip the fallback and require manual review.
        no_marker_html_msg = _email(OWNER, plain, rfc_id="<outer-inline-nomarker@gmail.com>")
        no_marker_html_msg.add_alternative("<p>Razem: 51,00 PLN</p>", subtype="html")
        pending = self._process(_metadata(no_marker_html_msg), _raw(no_marker_html_msg))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.payment),
                         ("imported", True, None))

        # An additional quoted substring is not a second whole-line marker when a valid cut exists.
        quoted_marker_html = html.replace(
            "<p>Razem: 42,50 PLN</p>",
            "<p>Quoted ---------- Wiadomość przekazana dalej ----------</p><p>Razem: 42,50 PLN</p>",
        )
        quoted_marker_msg = _email(OWNER, plain, rfc_id="<outer-inline-quoted-marker@gmail.com>")
        quoted_marker_msg.add_alternative(quoted_marker_html, subtype="html")
        pending = self._process(_metadata(quoted_marker_msg), _raw(quoted_marker_msg))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.payment.fields.total_amount),
                         ("imported", False, "42.5"))

        # Explicit plain rejection does NOT fall back to outer HTML; neither does plain-only missing total or HTML-only.
        rejected_plain = plain.replace("Order confirmed without table.", "Payment failed")
        rejected_msg = _email(OWNER, rejected_plain, rfc_id="<outer-inline-rej@gmail.com>")
        rejected_msg.add_alternative(html, subtype="html")
        pending = self._process(_metadata(rejected_msg), _raw(rejected_msg))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.payment), ("imported", True, None))

        plain_only = _email(OWNER, plain, rfc_id="<outer-plain-only@gmail.com>")
        pending = self._process(_metadata(plain_only), _raw(plain_only))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.payment), ("imported", True, None))

        html_only = _email(OWNER, html.replace("<p>Razem: 42,50 PLN</p>", "<p>No amount</p>"), subtype="html",
                           rfc_id="<outer-html-only@gmail.com>")
        pending = self._process(_metadata(html_only), _raw(html_only))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.payment), ("imported", True, None))

    def test_direct_invoice_accepts_its_provider_domains(self):
        for name, issuer, domains in (
            ("orange_invoice.txt", "Orange", ("pl.orange.com", "orange.com", "orange.pl")),
            ("tmobile_invoice.txt", "T-Mobile", ("synthetic.t-mobile.pl",)),
            ("play_invoice.txt", "Play", ("play.pl", "mojefinanseplay.pl")),
        ):
            text = (PDF_TEXT / name).read_text(encoding="utf-8")
            for domain in domains:
                with self.subTest(domain=domain):
                    sender = f"synthetic-billing@{domain}"
                    self.settings.sender_allowlist = (sender,)
                    message = _email(sender, text)
                    pending = self._process(_metadata(message), _raw(message))
                    self.assertEqual((pending.trust_path, pending.outcome, pending.invoice.issuer),
                                     ("direct_allowlist", "imported", issuer))

    def test_direct_orange_invoice_from_other_domains_is_skipped(self):
        text = (PDF_TEXT / "orange_invoice.txt").read_text(encoding="utf-8")
        for sender in ("synthetic-billing@notorange.com", "synthetic-billing@orange.com.example.test"):
            with self.subTest(sender=sender):
                self.settings.sender_allowlist = (sender,)
                no_invoice = _email(sender, "Synthetic body without an invoice")
                invoice = _email(sender, text)
                pending = self._process(_metadata(invoice), _raw(invoice))
                self.assertEqual(pending, self._process(_metadata(no_invoice), _raw(no_invoice)))
                self.assertEqual((pending.trust_path, pending.outcome, pending.invoice),
                                 ("direct_allowlist", "skipped_non_receipt", None))

    def test_invoices_from_each_other_provider_are_rejected(self):
        bodies_by_domain = {
            "t-mobile.pl": (PDF_TEXT / "tmobile_invoice.txt").read_text(encoding="utf-8"),
            "orange.pl": (PDF_TEXT / "orange_invoice.txt").read_text(encoding="utf-8"),
            "play.pl": (PDF_TEXT / "play_invoice.txt").read_text(encoding="utf-8"),
            "respectenergy.pl": INVOICE,
        }
        for invoice_domain, sender_domain in permutations(bodies_by_domain, 2):
            with self.subTest(invoice_domain=invoice_domain, sender_domain=sender_domain):
                sender = f"synthetic-billing@{sender_domain}"
                self.settings.sender_allowlist = (sender,)
                invoice = _email(sender, bodies_by_domain[invoice_domain])
                direct = self._process(_metadata(invoice), _raw(invoice))
                self.assertEqual((direct.trust_path, direct.outcome, direct.invoice),
                                 ("direct_allowlist", "skipped_non_receipt", None))

                forwarded = _email(SPOUSE, "Synthetic cover note")
                forwarded.add_attachment(invoice)
                pending = self._process(_metadata(forwarded), _raw(forwarded))
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                                  pending.sender_domain, pending.invoice),
                                 ("spouse_forward", "imported", True, sender_domain, None))

    def test_display_name_and_reply_to_do_not_supply_the_issuer_domain(self):
        sender = "x@evil.example"
        self.settings.sender_allowlist = (sender,)
        text = (PDF_TEXT / "tmobile_invoice.txt").read_text(encoding="utf-8")
        display_name = _email(sender, text)
        display_name.replace_header("From", f"T-Mobile <{sender}>")
        reply_to = _email(sender, text)
        reply_to["Reply-To"] = "synthetic-billing@t-mobile.pl"
        for header, message in (("From", display_name), ("Reply-To", reply_to)):
            with self.subTest(header=header):
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome, pending.invoice),
                                 ("direct_allowlist", "skipped_non_receipt", None))

    def test_mismatched_issuer_in_trusted_forward_needs_review_without_invoice(self):
        text = (PDF_TEXT / "orange_invoice.txt").read_text(encoding="utf-8")
        for body in (text, "Synthetic body without an invoice"):
            with self.subTest(body_has_invoice=body == text):
                forwarded = _email(SPOUSE, "Synthetic cover note")
                forwarded.add_attachment(_email(MERCHANT, body))
                pending = self._process(_metadata(forwarded), _raw(forwarded))
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                                  pending.sender_domain, pending.invoice, pending.rfc_message_id_hmac),
                                 ("spouse_forward", "imported", True, "respectenergy.pl", None, None))

    def test_untrusted_or_oversize_metadata_never_fetches_raw(self):
        spoof = _email("malicious@evil.example")
        spoof.replace_header("From", MERCHANT)
        for metadata, outcome in ((_metadata(spoof), "skipped_untrusted_sender"),
                                  (_metadata(size=gmail_message.MAX_MESSAGE_BYTES + 1), "skipped_oversized"),
                                  ({**_metadata(), "payload": {"headers": []}}, "skipped_untrusted_sender")):
            with self.subTest(outcome=outcome):
                run = self._run(metadata)
                pending = self.processor.process(run, MESSAGE_ID, GMAIL_HASH)
                self.assertEqual(pending.outcome, outcome)
                self.assertEqual(pending.trust_path, "untrusted")
                self.assertIsNone(pending.rfc_message_id_hmac)
                run.message.assert_called_once_with(MESSAGE_ID)
        self.worker.parse.assert_not_called()

    def test_raw_sender_is_checked_again_before_any_worker_call(self):
        spoof = _email("malicious@evil.example")
        spoof.replace_header("From", MERCHANT)
        for message in (spoof, _email(SPOUSE)):
            with self.subTest(sender=message["From"]):
                pending = self._process(_metadata(), _raw(message))
                self.assertEqual(pending.outcome, "skipped_untrusted_sender")
                self.assertEqual(pending.trust_path, "untrusted")
        self.worker.parse.assert_not_called()

    def test_incomplete_or_invalid_metadata_leaves_message_uncommitted(self):
        meta = _metadata()
        for changes in (
            {"id": "otherGmailId"}, {"internalDate": None}, {"internalDate": "-2"},
            {"sizeEstimate": True}, {"sizeEstimate": -1}, {"payload": None}, {"payload": {}},
            {"payload": {"headers": [None]}}, {"payload": {"headers": [{"name": False, "value": "ignored"}]}},
            {"payload": {"headers": [{"name": "From", "value": 42}]}},
        ):
            with self.subTest(changes=changes):
                run = self._run({**meta, **changes})
                with self.assertRaisesRegex(GmailError, "^invalid_response$"):
                    self.processor.process(run, MESSAGE_ID, GMAIL_HASH)
                run.message.assert_called_once_with(MESSAGE_ID)
        self.worker.parse.assert_not_called()

    def test_raw_size_and_encoding_are_checked_before_parsing_mime(self):
        meta = _metadata(size=0)
        with patch.object(gmail_message, "_MAX_BASE64_CHARS", 3):
            self.assertEqual(self._process(meta, _raw(b"hello")).outcome, "skipped_oversized")
        with patch.object(gmail_message, "MAX_MESSAGE_BYTES", 3):
            self.assertEqual(self._process(meta, _raw(b"hello")).outcome, "skipped_oversized")
        for raw in ({"id": "otherGmailId", "raw": _raw(_email())["raw"]}, {"id": MESSAGE_ID},
                    {"id": MESSAGE_ID, "raw": []}, {"id": MESSAGE_ID, "raw": ""},
                    {"id": MESSAGE_ID, "raw": "?not-base64?"}):
            with self.subTest(raw=raw), self.assertRaisesRegex(GmailError, "^invalid_response$"):
                self._process(meta, raw)
        self.worker.parse.assert_not_called()

    def test_message_removed_from_gmail_during_a_request(self):
        gone = GmailError("provider_http", http_status=404)
        self.assertIsNone(self._process(gone))
        pending = self._process(_metadata(), gone)
        self.assertEqual((pending.outcome, pending.trust_path), ("deleted", "direct_allowlist"))
        self.assertEqual(pending.internal_date, 1795566945.123)
        self.assertIsNone(pending.rfc_message_id_hmac)
        quota = GmailError("quota", 429, http_status=429)
        for responses in ((quota,), (_metadata(), quota)):
            with self.subTest(responses=responses), self.assertRaises(GmailError) as raised:
                self._process(*responses)
            self.assertIs(raised.exception, quota)
        self.worker.parse.assert_not_called()

    def test_mime_and_inline_spouse_forwards_keep_the_original_rfc_id(self):
        original = _email()
        outer = _email(SPOUSE, "Forwarding an invoice", rfc_id="<unrelated@gmail.com>")
        outer.add_attachment(original)
        inline = _email(SPOUSE, f"Przekazana wiadomość:\nOd: {MERCHANT}\nMessage-ID: {RFC_ID}\n\n{INVOICE}")
        for message in (outer, inline):
            with self.subTest(content_type=message.get_content_type()):
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome), ("spouse_forward", "imported"))
                self.assertEqual(pending.rfc_message_id_hmac,
                                 digest(SECRET, RFC_MESSAGE_DOMAIN, "Invoice-42@respect.example"))

    def test_html_inline_forward_goes_through_worker_before_unwrap(self):
        html = "<html>Forwarded message fixture</html>\n"
        message = _email(SPOUSE, html, subtype="html")
        rendered = f"Begin forwarded message:\nFrom: {MERCHANT}\n\n{INVOICE}"
        self.worker.parse.side_effect = [rendered, INVOICE]
        pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.trust_path, pending.outcome), ("spouse_forward", "imported"))
        self.assertEqual(self.worker.parse.call_args_list, [call("html", html.encode()),
                                                            call("text", b"\n" + INVOICE.encode())])

    def test_mixed_attached_and_inline_depth_two_requires_manual_review(self):
        relay = _email(MERCHANT, f"Begin forwarded message:\nFrom: {MERCHANT}\nMessage-ID: {RFC_ID}\n\n{INVOICE}")
        outer = _email(SPOUSE, "Synthetic cover note")
        outer.add_attachment(relay)
        pending = self._process(_metadata(outer), _raw(outer))
        self.assertEqual((pending.outcome, pending.forwarded_manually), ("imported", True))
        self.assertIsNone(pending.invoice)
        self.assertIsNone(pending.rfc_message_id_hmac)

    def test_mixed_depth_three_and_ambiguous_or_malformed_inner_forwards_are_rejected(self):
        inline = f"Begin forwarded message:\nFrom: {MERCHANT}\n\n{INVOICE}"
        attached_relay = _email(MERCHANT, "Synthetic cover note")
        attached_relay.add_attachment(_email(MERCHANT, inline))
        for relay in (
            attached_relay,
            _email(MERCHANT, f"Begin forwarded message:\nFrom: {MERCHANT}\n\n{inline}"),
            _email(MERCHANT, f"{INVOICE}\nBegin forwarded message:\nFrom:"),
        ):
            with self.subTest(relay=relay.get_content_type()):
                outer = _email(SPOUSE, "Synthetic cover note")
                outer.add_attachment(relay)
                pending = self._process(_metadata(outer), _raw(outer))
                self.assertEqual((pending.outcome, pending.forwarded_manually, pending.invoice),
                                 ("skipped_non_receipt", False, None))
        self.worker.parse.assert_not_called()

    def test_inline_merchant_without_provider_pattern_needs_review(self):
        for text in ("Please pay this bill", INVOICE.replace("RE/2026/00042", "FV/2026/11/42")):
            message = _email(SPOUSE, f"Begin forwarded message:\nFrom: {MERCHANT}\n\n{text}")
            pending = self._process(_metadata(message), _raw(message))
            self.assertEqual((pending.outcome, pending.forwarded_manually, pending.sender_domain),
                             ("imported", True, "respectenergy.pl"))
            self.assertIsNone(pending.invoice)

    def test_known_domain_with_provider_pattern_needs_review_without_exact_mailbox(self):
        for rules in ((MERCHANT,), ("@respectenergy.pl",)):
            with self.subTest(rules=rules):
                self.settings.sender_allowlist = rules
                message = _email(
                    SPOUSE, f"Begin forwarded message:\nFrom: synthetic-other@respectenergy.pl\nMessage-ID: {RFC_ID}\n\n{INVOICE}")
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.forwarded_manually, pending.sender_domain, pending.invoice),
                                 ("imported", True, "respectenergy.pl", None))
                self.assertIsNone(pending.rfc_message_id_hmac)
        self.worker.parse.assert_not_called()

    def test_direct_sender_never_unwraps_nested_mail_and_unknown_forwards_are_not_imported(self):
        direct = _email(MERCHANT, "")
        inner = _email("attacker@evil.example")
        inner.add_attachment(b"%PDF-fixture", maintype="application", subtype="pdf")
        direct.add_attachment(inner)
        self.assertEqual(self._process(_metadata(direct), _raw(direct)).outcome, "skipped_non_receipt")
        self.worker.parse.assert_called_once_with("text", b"\n")
        unknown_forward = _email(SPOUSE, f"Begin forwarded message:\nFrom: attacker@evil.example\n\n{INVOICE}")
        pending = self._process(_metadata(unknown_forward), _raw(unknown_forward))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.sender_domain, pending.invoice),
                         ("imported", True, "evil.example", None))

    def test_invalid_or_missing_rfc_message_ids_are_not_hashed(self):
        for rfc_id in (None, "no-brackets@respect.example", "<Invoice-42@local>", "< Invoice@respect.example>"):
            with self.subTest(rfc_id=rfc_id):
                message = _email(rfc_id=rfc_id)
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual(pending.outcome, "imported")
                self.assertIsNone(pending.rfc_message_id_hmac)
        duplicate = b"Message-ID: <another@respect.example>\n" + _email().as_bytes()
        self.assertIsNone(self._process(_metadata(), _raw(duplicate)).rfc_message_id_hmac)

    def test_html_alternative_can_import_even_with_pdf_present(self):
        message = _email(text="Open the formatted invoice")
        message.add_alternative("<strong>Synthetic invoice</strong>", subtype="html")
        message.add_attachment(b"%PDF-fixture", maintype="application", subtype="pdf")
        self.worker.parse.side_effect = [WorkerRejected(), INVOICE]
        pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.outcome, pending.source_kind), ("imported", "email"))
        self.assertEqual([args[0] for args, _ in self.worker.parse.call_args_list], ["text", "html"])

    def test_pdf_stub_fails_safely_for_pdf_only_and_future_results_are_structured(self):
        message = _email()
        message.set_content(b"%PDF-fixture", maintype="application", subtype="pdf")
        self.worker.parse.side_effect = PDFUnsupported()
        pending = self._process(_metadata(message), _raw(message))
        self.worker.parse.assert_called_once_with("pdf", b"%PDF-fixture")
        self.assertEqual(pending.outcome, "parse_failed")
        self.assertIsNone(pending.invoice)
        self.worker.parse.side_effect = [INVOICE]
        pending = self._process(_metadata(message), _raw(message))
        self.assertEqual((pending.outcome, pending.source_kind), ("imported", "attachment"))

    def test_body_bounds_and_invalid_mime_never_reach_the_worker(self):
        plain = _email()
        compact = _email(text="ęęę", charset="iso-8859-2", cte="base64")
        with patch.object(gmail_message, "MAX_HTML_BYTES", 4):
            for message in (plain, compact):
                with self.subTest(charset=message.get_content_charset()):
                    self.assertEqual(self._process(_metadata(message), _raw(message)).outcome, "skipped_oversized")
        for invalid in (
            b"Content-Type: multipart/mixed; boundary=missing\n",  # Broken top-level multipart.
            b"Content-Transfer-Encoding: base64\n",                # Invalid transfer encoding of the body.
        ):
            raw = invalid + plain.as_bytes().replace(b"Content-Transfer-Encoding: 8bit\n", b"")
            self.assertEqual(self._process(_metadata(), _raw(raw)).outcome, "parse_failed")
        plain.set_param("charset", "not-a-real-charset")
        self.assertEqual(self._process(_metadata(plain), _raw(plain)).outcome, "parse_failed")
        # A body with no bytes is conclusively non-receipt content.
        empty = _email(text="")
        self.assertEqual(self._process(_metadata(empty), _raw(empty.as_bytes().rstrip(b"\n") + b"\n\n")).outcome,
                         "skipped_non_receipt")
        with patch.object(gmail_message.BytesParser, "parsebytes", side_effect=RecursionError):
            self.assertEqual(self._process(_metadata(), _raw(_email())).outcome, "parse_failed")
        self.worker.parse.assert_not_called()

    def test_worker_unavailable_is_retryable_and_worker_rejection_is_terminal(self):
        self.worker.ready.return_value = False
        self.assertFalse(self.processor.ready())
        self.worker.parse.side_effect = WorkerUnavailable("synthetic private detail")
        with self.assertRaisesRegex(GmailError, "^parser_unavailable$"):
            self._process(_metadata(), _raw(_email()))
        html_forward = _email(SPOUSE, "<body>forward fixture</body>", subtype="html")
        self.worker.parse.side_effect = WorkerRejected("synthetic parse detail")
        self.assertEqual(self._process(_metadata(html_forward), _raw(html_forward)).outcome, "parse_failed")

    def test_owner_self_forward_imports_inline_and_attachment(self):
        original = _email()
        attached = _email(OWNER, "Forwarding my invoice", rfc_id="<owner-cover@gmail.com>")
        attached.add_attachment(original)
        inline = _email(OWNER, f"Przekazana wiadomość:\nOd: {MERCHANT}\nMessage-ID: {RFC_ID}\n\n{INVOICE}")
        for message in (attached, inline):
            with self.subTest(content_type=message.get_content_type()):
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually),
                                 ("owner_forward", "imported", False))
                self.assertEqual(pending.invoice.invoice_number, "RE/2026/00042")
                self.assertIsNone(pending.sender_domain)

    def test_spouse_forward_of_unknown_shop_needs_review(self):
        shop = "billing@corner-shop.example"
        inline = _email(SPOUSE, f"Begin forwarded message:\nFrom: {shop}\n\nPlease pay this shop bill")
        original = _email(shop, "Please pay this shop bill", rfc_id="<shop-1@corner-shop.example>")
        attached = _email(SPOUSE, "Forwarding a shop mail", rfc_id="<spouse-cover@gmail.com>")
        attached.add_attachment(original)
        for message, source in ((inline, "email"), (attached, "attachment")):
            with self.subTest(source=source):
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually,
                                  pending.sender_domain, pending.source_kind, pending.invoice),
                                 ("spouse_forward", "imported", True, "corner-shop.example", source, None))

    def test_untrusted_forwarder_is_rejected(self):
        message = _email("intruder@evil.example", f"Begin forwarded message:\nFrom: {MERCHANT}\n\n{INVOICE}")
        pending = self._process(_metadata(message))
        self.assertEqual((pending.outcome, pending.trust_path), ("skipped_untrusted_sender", "untrusted"))
        self.worker.parse.assert_not_called()

    def test_forged_inner_authentication_results_are_ignored(self):
        original = _email("attacker@evil.example", "Please pay this shop bill")
        original["Authentication-Results"] = (
            "mx.google.com; dkim=pass header.d=respectenergy.pl; dmarc=pass header.from=respectenergy.pl"
        )
        outer = _email(SPOUSE, "Forwarding", rfc_id="<spouse-cover@gmail.com>")
        outer.add_attachment(original)
        pending = self._process(_metadata(outer), _raw(outer))
        self.assertEqual((pending.trust_path, pending.forwarded_manually, pending.sender_domain, pending.invoice),
                         ("spouse_forward", True, "evil.example", None))

    def test_depth_three_forward_is_rejected(self):
        inner = _email(MERCHANT)
        middle = _email("relay@example.test", "middle")
        middle.add_attachment(inner)
        outer_middle = _email("other-relay@example.test", "outer middle")
        outer_middle.add_attachment(middle)
        outer = _email(SPOUSE, "outer")
        outer.add_attachment(outer_middle)
        pending = self._process(_metadata(outer), _raw(outer))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.invoice),
                         ("skipped_non_receipt", False, None))

    def test_parse_failed_logs_fixed_reason_codes_without_message_material(self):
        def make_part_defect():
            return (
                f"From: {MERCHANT}\r\n"
                f"Authentication-Results: mx.google.com; dkim=pass header.d=respectenergy.pl; dmarc=pass header.from=respectenergy.pl\r\n"
                "Subject: Sensitive Subject\r\n"
                "Content-Type: multipart/mixed; boundary=\"bnd\"\r\n\r\n"
                "--bnd\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!\r\n--bnd--\r\n"
            ).encode("ascii")

        def make_message_defect():
            return b"Content-Type: multipart/mixed; boundary=missing\n" + _email().as_bytes()

        def make_bad_charset():
            message = _email()
            message.set_param("charset", "not-a-real-charset")
            return message

        pdf_mail = _email()
        pdf_mail.set_content(b"%PDF-fixture", maintype="application", subtype="pdf")
        html_forward = _email(SPOUSE, "<body>Sensitive Forward Body</body>", subtype="html")

        from email.errors import MessageError

        cases = [
            ("message_defects", make_message_defect(), None, None),
            ("part_defects", make_part_defect(), None, None),
            ("charset_error", make_bad_charset(), None, None),
            ("pdf_unsupported", pdf_mail, PDFUnsupported("sensitive pdf detail"), None),
            ("worker_rejected", _email(), WorkerRejected("sensitive worker detail"), None),
            ("worker_rejected", html_forward, WorkerRejected("sensitive forward detail"), None),
            ("mime_error", _email(), None, MessageError("sensitive mime detail")),
            ("recursion", _email(), None, RecursionError("sensitive recursion detail")),
            ("unicode_error", _email(), None, UnicodeError("sensitive unicode detail")),
        ]
        for expected_reason, message, worker_error, parser_error in cases:
            with self.subTest(reason=expected_reason, message_type=type(message).__name__):
                self.worker.parse.side_effect = worker_error or (lambda _kind, source: source.decode("utf-8"))
                raw_response = _raw(message)
                meta = _metadata() if isinstance(message, bytes) else _metadata(message)
                with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
                    if parser_error is not None:
                        with patch.object(gmail_message.BytesParser, "parsebytes", side_effect=parser_error):
                            pending = self._process(meta, raw_response)
                    else:
                        pending = self._process(meta, raw_response)
                self.assertEqual((pending.outcome, pending.parse_failed_reason), ("parse_failed", expected_reason))
                self.assertEqual(logs.output, [f"WARNING:hestia.budget.gmail_message:gmail parse_failed reason={expected_reason}"])
                for forbidden in (MESSAGE_ID, MERCHANT, SPOUSE, "Sensitive", "sensitive", "RE/2026/00042"):
                    self.assertNotIn(forbidden, logs.output[0])

    def test_invoice_and_forward_precedence_records_only_deciding_final_reason(self):
        # Earlier WorkerRejected overwritten by a later imported HTML part logs nothing.
        imported_alt = _email(text="Open invoice")
        imported_alt.add_alternative("<strong>Synthetic invoice</strong>", subtype="html")
        self.worker.parse.side_effect = [WorkerRejected(), INVOICE]
        with self.assertNoLogs("hestia.budget.gmail_message", level="INFO"):
            pending = self._process(_metadata(imported_alt), _raw(imported_alt))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("imported", None))

        # Earlier WorkerRejected overwritten by later oversized part -> skipped_oversized, no reason/log.
        oversized_alt = _email(text="First part fails")
        oversized_alt.add_attachment(b"%PDF-" + b"x" * 16, maintype="application", subtype="pdf")
        self.worker.parse.side_effect = [WorkerRejected()]
        with patch.object(gmail_message, "MAX_PDF_BYTES", 8), \
                self.assertNoLogs("hestia.budget.gmail_message", level="INFO"):
            pending = self._process(_metadata(oversized_alt), _raw(oversized_alt))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("skipped_oversized", None))

        # Earlier WorkerRejected overwritten by later PDFUnsupported -> logs pdf_unsupported once.
        two_failures = _email(text="First part fails")
        two_failures.add_attachment(b"%PDF-fixture", maintype="application", subtype="pdf")
        self.worker.parse.side_effect = [WorkerRejected(), PDFUnsupported()]
        with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
            pending = self._process(_metadata(two_failures), _raw(two_failures))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("parse_failed", "pdf_unsupported"))
        self.assertEqual(logs.output, ["WARNING:hestia.budget.gmail_message:gmail parse_failed reason=pdf_unsupported"])

        # Earlier PDFUnsupported followed by non-invoice HTML stays parse_failed with pdf_unsupported.
        pdf_then_non_invoice = _email(text="Cover note without invoice fields")
        pdf_then_non_invoice.add_attachment(b"%PDF-fixture", maintype="application", subtype="pdf")
        self.worker.parse.side_effect = ["Cover note without invoice fields", PDFUnsupported()]
        with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
            pending = self._process(_metadata(pdf_then_non_invoice), _raw(pdf_then_non_invoice))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("parse_failed", "pdf_unsupported"))
        self.assertEqual(logs.output, ["WARNING:hestia.budget.gmail_message:gmail parse_failed reason=pdf_unsupported"])

        # Spouse forward falling back from failed _invoice to _manual_forward is imported, not parse_failed.
        inner = _email(MERCHANT)
        inner.set_content(b"%PDF-fixture", maintype="application", subtype="pdf")
        outer = _email(SPOUSE, "Forwarding PDF", rfc_id="<spouse-pdf@gmail.com>")
        outer.add_attachment(inner)
        self.worker.parse.side_effect = PDFUnsupported()
        with self.assertNoLogs("hestia.budget.gmail_message", level="INFO"):
            pending = self._process(_metadata(outer), _raw(outer))
        self.assertEqual((pending.outcome, pending.forwarded_manually, pending.parse_failed_reason),
                         ("imported", True, None))

    def test_octet_stream_pdf_detection_requires_pdf_extension_and_header(self):
        for filename in ("invoice.pdf", "Faktura.PDF", "bill.Pdf"):
            with self.subTest(filename=filename):
                message = _email()
                message.set_content(b"%PDF-1.4 fixture", maintype="application", subtype="octet-stream",
                                    filename=filename)
                self.worker.parse.side_effect = [INVOICE]
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.source_kind, pending.parse_failed_reason),
                                 ("imported", "attachment", None))
                self.worker.parse.assert_called_once_with("pdf", b"%PDF-1.4 fixture")
                self.worker.parse.reset_mock()

                self.worker.parse.side_effect = [PDFUnsupported()]
                with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
                    failed = self._process(_metadata(message), _raw(message))
                self.assertEqual((failed.outcome, failed.parse_failed_reason), ("parse_failed", "pdf_unsupported"))
                self.assertEqual(logs.output, ["WARNING:hestia.budget.gmail_message:gmail parse_failed reason=pdf_unsupported"])
                self.worker.parse.reset_mock()

        # Multipart message with a non-invoice cover body also imports the octet-stream PDF attachment.
        multipart = _email(text="See attached invoice")
        multipart.add_attachment(b"%PDF-1.4 fixture", maintype="application", subtype="octet-stream",
                                 filename="Faktura.PDF")
        self.worker.parse.side_effect = ["See attached invoice", INVOICE]
        pending = self._process(_metadata(multipart), _raw(multipart))
        self.assertEqual((pending.outcome, pending.source_kind), ("imported", "attachment"))
        self.assertEqual(self.worker.parse.call_args_list,
                         [call("text", b"See attached invoice\n"), call("pdf", b"%PDF-1.4 fixture")])
        self.worker.parse.reset_mock()

        # Filename in Content-Type name parameter also matches.
        name_only = _email()
        name_only.set_content(b"%PDF-1.4 fixture", maintype="application", subtype="octet-stream")
        name_only.set_param("name", "faktura.pdf")
        self.worker.parse.side_effect = [INVOICE]
        self.assertEqual(self._process(_metadata(name_only), _raw(name_only)).outcome, "imported")
        self.worker.parse.assert_called_once_with("pdf", b"%PDF-1.4 fixture")
        self.worker.parse.reset_mock()

        # Missing filename, wrong extension, or missing %PDF- header must NOT be sent as PDF.
        for kwargs, body in (
            ({}, b"%PDF-1.4 fixture"),
            ({"filename": "invoice.bin"}, b"%PDF-1.4 fixture"),
            ({"filename": "invoice.pdf"}, b"Not a PDF payload"),
        ):
            with self.subTest(kwargs=kwargs, body=body):
                message = _email()
                message.set_content(body, maintype="application", subtype="octet-stream", **kwargs)
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual(pending.outcome, "skipped_non_receipt")
                self.worker.parse.assert_not_called()

        oversized = _email()
        oversized.set_content(b"%PDF-" + b"x" * 16, maintype="application", subtype="octet-stream",
                              filename="big.PDF")
        with patch.object(gmail_message, "MAX_PDF_BYTES", 8):
            pending = self._process(_metadata(oversized), _raw(oversized))
        self.assertEqual(pending.outcome, "skipped_oversized")
        self.worker.parse.assert_not_called()

    def test_unknown_rejected_reason_normalizes_to_other_in_logs_and_result(self):
        secret = "raw-sensitive-error"
        self.worker.parse.side_effect = gmail_message._Rejected(secret)
        with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
            pending = self._process(_metadata(), _raw(_email()))
        self.assertEqual((pending.outcome, pending.parse_failed_reason), ("parse_failed", "other"))
        self.assertEqual(logs.output, ["WARNING:hestia.budget.gmail_message:gmail parse_failed reason=other"])
        self.assertNotIn(secret, logs.output[0])
