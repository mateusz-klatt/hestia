"""Gmail message processing against synthetic metadata, MIME, and worker fixtures."""
import base64
from email.message import EmailMessage
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from hestia.budget import gmail_message
from hestia.budget.gmail_client import GmailError
from hestia.budget.gmail_invoice import ImportedInvoice
from hestia.budget.gmail_store import RFC_MESSAGE_DOMAIN, digest
from hestia.budget.gmail_worker import PDFUnsupported, WorkerRejected, WorkerUnavailable


MERCHANT = "bills@respect.example"
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
            known_merchant_domains=("respect.example",), require_known_forwarded_merchant=True,
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

    def test_mixed_attached_and_inline_depth_two_imports_the_final_original(self):
        relay = _email(MERCHANT, f"Begin forwarded message:\nFrom: {MERCHANT}\nMessage-ID: {RFC_ID}\n\n{INVOICE}")
        outer = _email(SPOUSE, "Synthetic cover note")
        outer.add_attachment(relay)
        pending = self._process(_metadata(outer), _raw(outer))
        self.assertEqual((pending.outcome, pending.forwarded_manually), ("imported", False))
        self.assertEqual(pending.invoice.invoice_number, "RE/2026/00042")
        self.assertEqual(pending.rfc_message_id_hmac, digest(SECRET, RFC_MESSAGE_DOMAIN, "Invoice-42@respect.example"))

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
                             ("imported", True, "respect.example"))
            self.assertIsNone(pending.invoice)

    def test_known_domain_with_provider_pattern_needs_review_without_exact_mailbox(self):
        for rules in ((MERCHANT,), ("@respect.example",)):
            with self.subTest(rules=rules):
                self.settings.sender_allowlist = rules
                message = _email(
                    SPOUSE, f"Begin forwarded message:\nFrom: other@respect.example\nMessage-ID: {RFC_ID}\n\n{INVOICE}")
                pending = self._process(_metadata(message), _raw(message))
                self.assertEqual((pending.outcome, pending.forwarded_manually, pending.sender_domain, pending.invoice),
                                 ("imported", True, "respect.example", None))
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
            "mx.google.com; dkim=pass header.d=respect.example; dmarc=pass header.from=respect.example"
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
                f"Authentication-Results: mx.google.com; dkim=pass header.d=respect.example; dmarc=pass header.from=respect.example\r\n"
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
