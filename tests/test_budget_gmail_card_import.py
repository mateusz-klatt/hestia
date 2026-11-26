"""Real Gmail message parsing, SQLite staging and owner-card reconciliation across syncs."""
from dataclasses import replace
from unittest.mock import Mock

from hestia import db
from hestia.budget.gmail_card_import import waiting_key
from hestia.budget.gmail_client import GmailClient
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_store import (
    GMAIL_MESSAGE_DOMAIN,
    IMPORT_TTL,
    MAILBOX_DOMAIN,
    GmailStore,
    digest,
)
from hestia.budget.gmail_sync import GmailSync
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import _render, _validate_output
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_gmail_discovery import DOCUMENT, UNLISTED
from tests.test_budget_gmail_message import (
    INVOICE,
    MERCHANT,
    SECRET,
    _email,
    _metadata,
    _raw,
)
from tests.test_budget_gmail_sync import (
    OWNER,
    SETTINGS,
    FakeClock,
    FakeGmailTransport,
    page,
    response,
)


class GmailCardImportTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.now = 1795600000
        self.gmail = GmailStore(self.sessions, clock=lambda: self.now)
        self.gmail.transition(OWNER, 1, "active", digest(SECRET, MAILBOX_DOMAIN, SETTINGS.owner_mailbox))
        settings = replace(SETTINGS, hmac_secret=SECRET, sender_allowlist=(MERCHANT,),
                           payment_senders=(PaymentSender("orders@stationery.example", "Example Shop"),),
                           require_sender_authentication=False)
        worker = Mock()
        worker.parse.side_effect = lambda kind, payload: _validate_output(
            _render({"text": b"T", "html": b"H", "pdf": b"P"}[kind] + payload)
        )
        self.processor = GmailMessageProcessor(settings, worker, discover_documents=True)

    def message(self, mail_id, *, sender=UNLISTED, body=DOCUMENT, rfc_id=None, subject=None):
        message = _email(sender, body, rfc_id=rfc_id or f"<{mail_id}@example.test>")
        del message["Authentication-Results"]
        if subject:
            message["Subject"] = subject
        run = Mock()
        run.message.side_effect = [_metadata(message) | {"id": mail_id}, _raw(message) | {"id": mail_id}]
        return self.processor.process(run, mail_id, digest(SECRET, GMAIL_MESSAGE_DOMAIN, mail_id))

    def add_card(self, *, merchant="Cedar Tools", amount="7.5", day="2026-11-23"):
        self.add_transaction("a", "synthetic-card", entry_reference="synthetic-card", amount=amount,
                             booking_date=day, transaction_date=day, remittance_information=["Płatność kartą", merchant])

    def commit(self, *items):
        return self.gmail.commit(OWNER, 1, {}, items, settle_cards=True)

    def assert_linked_once(self, invoice_number):
        receipt, = self.rows(db.BudgetReceipt)
        self.assertEqual((receipt.invoice_number, receipt.payment_method, receipt.linked_account_id, receipt.linked_entry_ref),
                         (invoice_number, "card", self.accounts["a"], "synthetic-card"))
        return receipt.id

    def test_policy_catchup_replaces_old_sender_rejection_and_stages_once(self):
        item = self.message("old-sender")
        old = replace(item, card_required=False, outcome="skipped_untrusted_sender",
                      invoice=None, document_provenance=None)
        self.gmail.commit(OWNER, 1, {}, [old])
        self.now += 1
        cutoff = self.now
        for _ in range(2):
            self.gmail.commit(OWNER, 1, {}, [item], retry_skipped_before=cutoff)
        self.assertEqual(self.rows(db.BudgetGmailMessage), [])
        self.assertTrue(self.gmail.known(OWNER, item.gmail_id_hmac))
        self.add_card()
        self.gmail.commit(OWNER, 1, {"sync_phase": "idle"}, settle_cards=True)
        self.assert_linked_once("TEST/42")
        self.assertEqual([source.outcome for source in self.rows(db.BudgetGmailMessage)], ["imported"])

    def test_distinct_documents_competing_for_one_card_charge_stay_unlinked(self):
        first = self.message("first")
        second = self.message("second", body=DOCUMENT.replace("TEST/42", "TEST/43"))
        self.assertTrue(first.card_required and second.card_required)
        self.add_card()
        self.commit(first, second)
        self.gmail.retain(OWNER, 1, settle_cards=True)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertTrue(all(self.gmail.known(OWNER, doc.gmail_id_hmac) for doc in (first, second)))

    def test_duplicate_invoices_and_retries_create_one_linked_receipt(self):
        first = self.message("invoice")
        reminder = self.message("invoice-copy")
        self.add_card()
        self.commit(first, reminder)
        receipt_id = self.assert_linked_once("TEST/42")
        self.commit(reminder, first)
        self.gmail.retain(OWNER, 1, settle_cards=True)
        self.assertEqual(self.assert_linked_once("TEST/42"), receipt_id)
        self.assertEqual(len(self.rows(db.BudgetGmailMessage)), 2)

    def test_unnumbered_receipt_with_booked_card_can_be_confirmed(self):
        body = "Receipt\nMerchant: Cedar Tools\nDate: 2026-11-23\nGrand Total: PLN 7.50"
        item = self.message("plain-receipt", body=body)
        self.assertTrue(item.card_required)
        self.assertIsNone(item.invoice.invoice_number)
        self.add_card()
        self.commit(item)
        receipt_id = self.assert_linked_once(None)
        self.commit(self.message("resend", body=body, rfc_id="<plain-receipt@example.test>"))
        self.assertEqual(self.assert_linked_once(None), receipt_id)
        for title in ("Invoice", "Delivery note"):
            self.assertIsNone(self.message("other", body=body.replace("Receipt", title)).invoice)

    def test_independent_unnumbered_receipts_cannot_claim_the_same_card_charge(self):
        body = "Paragon\nSprzedawca: Cedar Tools\nData zakupu: 2026-11-23\nSuma: PLN 7,50"
        self.add_card()
        self.commit(self.message("receipt1", body=body), self.message("receipt2", body=body))
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        for header in ("Receipt #TEST-42", "Invoice #TEST-42"):
            numbered = self.message("english", body=body.replace("Paragon", header))
            self.assertEqual(numbered.invoice.invoice_number, "TEST-42")

    def test_inconsistent_totals_on_two_copies_remain_pending(self):
        original = self.message("invoice")
        inconsistent_copy = self.message("invoice-revised", body=DOCUMENT.replace("7,50", "8,50"))
        self.add_card()
        self.commit(original, inconsistent_copy)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertTrue(all(self.gmail.known(OWNER, doc.gmail_id_hmac) for doc in (original, inconsistent_copy)))

    def test_document_number_collision_respects_seller_and_total(self):
        self.add_card()
        self.commit(self.message("first"))
        different_seller = self.message("second", body=DOCUMENT.replace("Cedar Tools", "Pine Hardware"))
        different_total = self.message("third", body=DOCUMENT.replace("7,50", "7,60"))
        self.commit(different_seller, different_total)
        rows = {source.gmail_id_hmac: source.outcome for source in self.rows(db.BudgetGmailMessage)}
        self.assertNotIn(different_seller.gmail_id_hmac, rows)
        self.assertTrue(self.gmail.known(OWNER, different_seller.gmail_id_hmac))
        self.assertEqual(rows[different_total.gmail_id_hmac], "parse_failed")
        self.assert_linked_once("TEST/42")

    def test_separate_scan_chunks_do_not_promote_first_competing_document(self):
        first = self.message("first")
        second = self.message("second", body=DOCUMENT.replace("TEST/42", "TEST/43"))
        self.add_card()
        for doc in (first, second):
            self.gmail.commit(OWNER, 1, {}, [doc], settle_cards=False)
            self.gmail.retain(OWNER, 1)
            self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.gmail.commit(OWNER, 1, {"sync_phase": "idle"}, settle_cards=True)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertTrue(all(self.gmail.known(OWNER, doc.gmail_id_hmac) for doc in (first, second)))

    def test_numbered_receipt_resends_are_identified_after_linking(self):
        body = DOCUMENT.replace("Faktura VAT nr TEST/42", "Paragon nr CT/42")
        self.add_card()
        first = self.message("original", body=body)
        self.assertEqual(first.invoice.receipt_number, "CT/42")
        self.commit(first)
        receipt_id = self.assert_linked_once(None)
        for item in (self.message("copy-rfc", body=body, rfc_id="<original@example.test>"),
                     self.message("copy-new-envelope", body=body)):
            self.commit(item)
            source = next(row for row in self.rows(db.BudgetGmailMessage) if row.gmail_id_hmac == item.gmail_id_hmac)
            self.assertEqual(source.outcome, "skipped_duplicate")
            with self.sessions() as session:
                self.assertIsNone(session.get(db.AppMeta, waiting_key(OWNER, item.gmail_id_hmac)))
            self.assertEqual(self.assert_linked_once(None), receipt_id)

    def test_recognized_merchant_payment_keeps_line_items_when_linked(self):
        from tests.test_budget_gmail_message import SHOP_TABLE

        item = self.message("stationery", sender="orders@stationery.example", subject="Zamówienie opłacone", body=SHOP_TABLE)
        self.assertEqual((item.payment.fields.merchant, len(item.payment.items)), ("Example Shop", 2))
        self.add_card(merchant="Example Shop", amount="14.30", day="2026-11-25")
        self.commit(item)
        self.assert_linked_once(None)
        lines = sorted(self.rows(db.BudgetReceiptLine), key=lambda line: line.line_no)
        self.assertEqual([line.gross_line_total for line in lines], ["6.8", "7.5"])

    def test_late_card_charge_promotes_staged_unlisted_document_after_restart(self):
        item = self.message("invoice")
        self.commit(item)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.add_card()
        self.gmail = GmailStore(self.sessions, clock=lambda: self.now)
        self.gmail.retain(OWNER, 1, settle_cards=True)
        self.assert_linked_once("TEST/42")

    def test_late_bank_booking_is_imported_on_a_sync_without_new_mail(self):
        transport = FakeGmailTransport()
        clock = FakeClock()
        client = GmailClient(transport, monotonic=clock.monotonic, sleep=clock.sleep)
        service = GmailSync(self.processor.settings, self.gmail, client, self.processor, self.path)
        message = _email(UNLISTED, DOCUMENT)
        del message["Authentication-Results"]
        transport.routes[("/messages", None)] = response(page("invoice"))
        transport.routes[("/messages/invoice", None)] = lambda params: response(
            (_metadata(message) if params["format"] == ["metadata"] else _raw(message)) | {"id": "invoice"})
        self.assertEqual(service.sync(OWNER)["imported_count"], 0)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertTrue(self.gmail.known(OWNER, digest(SECRET, GMAIL_MESSAGE_DOMAIN, "invoice")))
        transport.routes[("/history", None)] = response({"history": []})
        self.now += 3600
        self.add_card()
        self.assertEqual(service.sync(OWNER, poll=True)["imported_count"], 1)
        self.assert_linked_once("TEST/42")
        self.assertEqual(len(transport.requests("/messages/invoice")), 2)

    def test_allowlisted_invoice_keeps_receipt_id_when_its_charge_arrives_later(self):
        item = self.message("utility", sender=MERCHANT, body=INVOICE)
        self.assertFalse(item.card_required)
        self.commit(item)
        receipt, = self.rows(db.BudgetReceipt)
        self.assertEqual((receipt.status, receipt.linked_entry_ref), ("imported", None))
        self.add_card(merchant="Respect Energy", amount="123.45", day="2026-11-21")
        self.gmail.retain(OWNER, 1, settle_cards=True)
        self.assertEqual(self.assert_linked_once("RE/2026/00042"), receipt.id)

    def test_revoking_grant_forgets_pending_document_before_new_card_charge(self):
        item = self.message("invoice")
        self.commit(item)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.assertTrue(self.gmail.known(OWNER, item.gmail_id_hmac))
        self.gmail.transition(OWNER, self.gmail.state(OWNER).version, "revoked")
        self.assertFalse(self.gmail.known(OWNER, item.gmail_id_hmac))
        self.gmail.transition(OWNER, self.gmail.state(OWNER).version, "active")
        self.add_card()
        self.gmail.retain(OWNER, self.gmail.state(OWNER).grant_generation, settle_cards=True)
        self.assertEqual(self.rows(db.BudgetReceipt), [])

    def test_pending_document_expires_before_late_card_charge(self):
        item = self.message("expired")
        self.commit(item)
        self.now += IMPORT_TTL
        self.add_card()
        self.gmail.retain(OWNER, 1, settle_cards=True)
        self.assertFalse(self.gmail.known(OWNER, item.gmail_id_hmac))
        self.assertEqual(self.rows(db.BudgetReceipt), [])

    def test_relaxed_sync_imports_card_document_beyond_200_pending_receipts(self):
        clock, transport = FakeClock(), FakeGmailTransport()
        client = GmailClient(transport, monotonic=clock.monotonic, sleep=clock.sleep)
        service = GmailSync(self.processor.settings, self.gmail, client, self.processor, self.path)
        with self.sessions.begin() as session:
            session.add_all(db.BudgetReceipt(id=f"pending-{n}", owner=OWNER, status="imported", source_kind="email",
                                             version=1, created_at=self.now, updated_at=self.now) for n in range(200))
        self.assertTrue(self.gmail.at_pending_cap(OWNER))
        self.add_card()
        transport.routes["/messages", None] = response(page("utility", "unpaid", token="next"))
        transport.routes["/messages", "next"] = response(page("paid"))
        unpaid = DOCUMENT.replace("7,50", "7,60").replace("TEST/42", "TEST/43")
        for mail_id, sender, body in (("utility", MERCHANT, INVOICE), ("unpaid", UNLISTED, unpaid), ("paid", UNLISTED, DOCUMENT)):
            message = _email(sender, body, rfc_id=f"<{mail_id}@example.test>")
            del message["Authentication-Results"]
            variants = {"metadata": _metadata(message) | {"id": mail_id}, "raw": _raw(message) | {"id": mail_id}}
            transport.routes[f"/messages/{mail_id}", None] = lambda params, variants=variants: response(
                variants[params["format"][0]])
        result = service.sync(OWNER)
        self.assertEqual((result["imported_count"], self.gmail.state(OWNER).history_id), (2, "100"))
        receipts = self.rows(db.BudgetReceipt)
        self.assertEqual(sum(row.status == "imported" for row in receipts), 201)
        linked, = [row for row in receipts if row.status == "confirmed"]
        self.assertEqual((linked.invoice_number, linked.linked_entry_ref), ("TEST/42", "synthetic-card"))
        self.assertTrue(self.gmail.known(OWNER, digest(SECRET, GMAIL_MESSAGE_DOMAIN, "unpaid")))
