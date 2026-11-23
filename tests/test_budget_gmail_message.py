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

    def test_direct_sender_never_unwraps_nested_mail_and_unknown_forwards_are_not_imported(self):
        direct = _email(MERCHANT, "")
        inner = _email("attacker@evil.example")
        inner.add_attachment(b"%PDF-fixture", maintype="application", subtype="pdf")
        direct.add_attachment(inner)
        unknown_forward = _email(SPOUSE, f"Begin forwarded message:\nFrom: attacker@evil.example\n\n{INVOICE}")
        for message in (direct, unknown_forward):
            with self.subTest(sender=message["From"]):
                self.assertEqual(self._process(_metadata(message), _raw(message)).outcome, "skipped_non_receipt")
        self.worker.parse.assert_called_once_with("text", b"\n")

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
