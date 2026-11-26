"""Exercise the optional sender checks at both Gmail reads and the forwarded-message boundary."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock

from hestia.budget.gmail_client import GmailCredentials
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_sync import GmailSettings
from hestia.budget.gmail_trust import PaymentSender
from tests.test_budget_gmail_message import (
    GMAIL_HASH, INVOICE, MERCHANT, MESSAGE_ID, OWNER, SECRET, SPOUSE, _email, _metadata, _raw,
)


def _without_dkim(message):
    domain = str(message["From"]).rpartition("@")[2]
    message.replace_header(
        "Authentication-Results",
        f"mx.google.com; spf=pass smtp.mailfrom={domain}; dmarc=pass header.from={domain}",
    )
    return message


class GmailSenderAuthPolicyTests(TestCase):
    def setUp(self):
        self.settings = GmailSettings(
            "test-owner", OWNER, GmailCredentials("client", "secret", "refresh"), SECRET, (MERCHANT,), (SPOUSE,),
        )
        self.worker = Mock()
        self.worker.parse.side_effect = lambda kind, data: data.decode("utf-8")
        self.run = Mock()

    def _process(self, message, *, settings=None, metadata=None):
        self.run.reset_mock()
        source_metadata = _metadata(message) if metadata is None else metadata
        self.run.message.side_effect = [source_metadata, _raw(message)]
        processor = GmailMessageProcessor(settings or self.settings, self.worker)
        return processor.process(self.run, MESSAGE_ID, GMAIL_HASH)

    def test_direct_respect_invoice_with_spf_and_dmarc_only(self):
        self.assertFalse(self.settings.require_sender_authentication)
        invoice = _without_dkim(_email())
        strict = replace(self.settings, require_sender_authentication=True)
        rejected = self._process(invoice, settings=strict)
        self.assertEqual(rejected.outcome, "skipped_untrusted_sender")
        self.assertEqual(self.run.message.call_count, 1)
        self.worker.parse.assert_not_called()

        for message, settings in ((invoice, self.settings), (_email(), strict)):
            with self.subTest(strict=settings.require_sender_authentication):
                pending = self._process(message, settings=settings)
                self.assertEqual((pending.trust_path, pending.outcome), ("direct_allowlist", "imported"))
                self.assertEqual(pending.invoice.invoice_number, "RE/2026/00042")

    def test_undated_respect_pdf_uses_the_configured_direct_sender_policy(self):
        settings = replace(self.settings, document_senders=(PaymentSender(MERCHANT, "Respect Energy"),))
        message = _without_dkim(_email(text="The invoice is attached"))
        message.add_attachment(b"%PDF-fixture", maintype="application", subtype="pdf")
        self.worker.parse.side_effect = None
        self.worker.parse.return_value = INVOICE.replace("Data wystawienia: 21.11.2026\n", "")
        pending = self._process(message, settings=settings)
        self.assertEqual((pending.outcome, pending.source_kind), ("imported", "attachment"))
        self.assertEqual((pending.invoice.invoice_number, pending.invoice.issue_date), ("RE/2026/00042", None))
        self.worker.parse.assert_called_once_with("pdf", b"%PDF-fixture")

    def test_spouse_attachment_and_owner_inline_forward_without_auth_results(self):
        attached = _email(SPOUSE, "Forwarding the invoice")
        inner = _email()
        del inner["Authentication-Results"]
        attached.add_attachment(inner)
        inline = _email(OWNER, f"Przekazana wiadomość:\nOd: {MERCHANT}\n\n{INVOICE}")
        for message, path in ((attached, "spouse_forward"), (inline, "owner_forward")):
            with self.subTest(path=path):
                del message["Authentication-Results"]
                pending = self._process(message)
                self.assertEqual((pending.trust_path, pending.outcome, pending.forwarded_manually),
                                 (path, "imported", False))
                self.assertEqual(pending.invoice.amount, "123.45")

    def test_strict_mode_checks_the_forwarder_and_leaves_unknown_original_for_review(self):
        strict = replace(self.settings, require_sender_authentication=True)
        original = _without_dkim(_email())
        cover = _email(SPOUSE, "Forwarding")
        cover.add_attachment(original)
        pending = self._process(cover, settings=strict)
        self.assertEqual((pending.trust_path, pending.invoice.invoice_number, pending.forwarded_manually),
                         ("spouse_forward", "RE/2026/00042", False))
        rejected = self._process(_without_dkim(cover), settings=strict)
        self.assertEqual(rejected.outcome, "skipped_untrusted_sender")
        self.assertEqual(self.run.message.call_count, 1)

        unknown = _email(SPOUSE, f"Begin forwarded message:\nFrom: shop@unlisted.example\n\n{INVOICE}")
        for settings in (self.settings, strict):
            with self.subTest(strict=settings.require_sender_authentication):
                pending = self._process(unknown, settings=settings)
                self.assertEqual((pending.trust_path, pending.forwarded_manually, pending.sender_domain,
                                  pending.invoice), ("spouse_forward", True, "unlisted.example", None))

    def test_configured_senders_and_unambiguous_metadata_are_still_required(self):
        wrong_sender = _email()
        wrong_sender["Sender"] = "elsewhere@unlisted.example"
        duplicated = _metadata()
        duplicated["payload"]["headers"].append({"name": "From", "value": MERCHANT})
        for message, metadata in ((_email("shop@unlisted.example"), None), (wrong_sender, None), (_email(), duplicated)):
            with self.subTest(sender=message["From"], duplicated=metadata is not None):
                rejected = self._process(message, metadata=metadata)
                self.assertEqual((rejected.trust_path, rejected.outcome), ("untrusted", "skipped_untrusted_sender"))
                self.assertEqual(self.run.message.call_count, 1)
        self.worker.parse.assert_not_called()

    def test_raw_author_must_match_metadata_even_under_the_same_allowlisted_domain(self):
        settings = replace(self.settings, sender_allowlist=("@respectenergy.pl",))
        changed = _without_dkim(_email("other@respectenergy.pl"))
        rejected = self._process(changed, settings=settings, metadata=_metadata(_without_dkim(_email())))
        self.assertEqual((rejected.trust_path, rejected.outcome), ("untrusted", "skipped_untrusted_sender"))
        self.assertEqual(self.run.message.call_count, 2)
        self.worker.parse.assert_not_called()
