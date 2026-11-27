"""Real Gmail message parsing, SQLite staging and owner-card reconciliation across syncs."""
import json
from dataclasses import replace
from unittest.mock import Mock, patch

from sqlalchemy import delete, select

from hestia import authz, db
from hestia.budget.gmail_card_import import stage, waiting_key
from hestia.budget.gmail_client import GmailClient
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_store import (
    GMAIL_MESSAGE_DOMAIN,
    IMPORT_TTL,
    MAILBOX_DOMAIN,
    GmailStore,
    _document_number_key,
    digest,
    retain_imports,
)
from hestia.budget.gmail_sync import GmailSync
from hestia.budget.gmail_trust import PaymentSender
from hestia.budget.gmail_worker import _render, _validate_output
from hestia.budget.receipt_reconcile import evidence_json, reconcile_receipt_links
from hestia.budget.receipt_store import BudgetReceiptStore
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
from tests.test_budget_gmail_respect import _polish_pdf
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
        confirm_audit, = (row for row in self.rows(db.BudgetAnalyticsAudit)
                          if row.subject_id == receipt_id and row.action == "confirm")
        self.assertIs(json.loads(confirm_audit.after_json)["shop_name_confirmed"], False)
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

    def test_card_suffix_survives_staging_and_link_revalidation(self):
        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        body = DOCUMENT + "\nKarta płatnicza: **** 0123\n"
        item = self.message("scoped-card", body=body)
        self.assertEqual(item.invoice.card_last4, "0123")
        for account, ending in (("d", "9876"), ("a", "0123")):
            self.add_transaction(account, f"card-{ending}", amount="7.5", booking_date="2026-11-23",
                                 remittance_information=["Płatność kartą", "Cedar Tools", f"Visa ending in {ending}"])
            if account == "d":
                self.commit(item)
                self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.gmail = GmailStore(self.sessions, clock=lambda: self.now)
        self.gmail.retain(OWNER, 1, settle_cards=True)
        receipt, = self.rows(db.BudgetReceipt)
        self.assertEqual((receipt.status, receipt.card_last4, receipt.linked_account_id, receipt.linked_entry_ref),
                         ("confirmed", "0123", self.accounts["a"], "card-0123"))
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, OWNER, self.now), 0)

    def test_blik_document_waits_for_blik_debit_and_keeps_its_payment_method(self):
        from hestia.budget.receipt_reconcile import reconcile_receipt_links

        self.add_card()
        item = self.message("blik", body=DOCUMENT + "\nMetoda płatności: BLIK\n")
        self.assertEqual(item.invoice.payment_method, "blik")
        self.commit(item)
        self.assertEqual(self.rows(db.BudgetReceipt), [])
        self.add_transaction("d", "booked-blik", amount="7.5", booking_date="2026-11-23",
                             remittance_information=["Płatność BLIK", "Cedar Tools"])
        self.gmail.retain(OWNER, 1, settle_cards=True)
        receipt, = self.rows(db.BudgetReceipt)
        self.assertEqual((receipt.status, receipt.payment_method, receipt.linked_account_id, receipt.linked_entry_ref),
                         ("confirmed", "blik", self.accounts["d"], "booked-blik"))
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, OWNER, self.now), 0)

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
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, _document_number_key(receipt.id)))
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

    def _reset_state(self):
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetReceiptLine))
            session.execute(delete(db.BudgetGmailMessage))
            session.execute(delete(db.BudgetReceipt))
            session.execute(delete(db.BudgetAnalyticsAudit))
            session.execute(delete(db.BudgetTransactionMetadata))
            session.execute(delete(db.BudgetTransaction))
            session.execute(delete(db.AppMeta).where(db.AppMeta.key.startswith("budget_gmail_card_wait:")))
            session.execute(delete(db.AppMeta).where(db.AppMeta.key.startswith("budget_gmail_document_number:")))

    def _add_debit(self, reference, *, account="a", merchant="Cedar Tools", amount="7.5", day="2026-11-23", lines=None):
        remittance = ["Płatność kartą", merchant] if lines is None else lines
        return self.add_transaction(
            account, reference, entry_reference=reference, amount=amount,
            booking_date=day, transaction_date=day, remittance_information=remittance,
        )

    def _ordered_pair(self, first="pair-alpha", second="pair-beta"):
        if digest(SECRET, GMAIL_MESSAGE_DOMAIN, first) < digest(SECRET, GMAIL_MESSAGE_DOMAIN, second):
            return first, second
        return second, first

    def _trusted_pdf_message(self, mail_id, body, *, rfc_id=None):
        settings = replace(self.processor.settings, document_senders=(PaymentSender(MERCHANT, "Cedar Tools"),))
        processor = GmailMessageProcessor(settings, self.processor.worker, discover_documents=True)
        message = _email(MERCHANT, "Document attached", rfc_id=rfc_id or f"<{mail_id}@example.test>")
        del message["Authentication-Results"]
        message.add_attachment(_polish_pdf(body), maintype="application", subtype="pdf", filename="doc.pdf")
        run = Mock()
        run.message.side_effect = [_metadata(message) | {"id": mail_id}, _raw(message) | {"id": mail_id}]
        pending = processor.process(run, mail_id, digest(SECRET, GMAIL_MESSAGE_DOMAIN, mail_id))
        self.assertEqual((pending.outcome, pending.source_kind, pending.card_required),
                         ("imported", "attachment", False))
        return pending

    def test_ambiguous_candidate_set_blocks_overlapping_single_match_across_pending_and_staged(self):
        wide_doc = DOCUMENT + "Termin płatności: 10.12.2026\n"
        narrow_doc = DOCUMENT.replace("TEST/42", "TEST/43")
        unrelated_doc = DOCUMENT.replace("TEST/42", "TEST/99").replace("Cedar Tools", "Pine Hardware").replace("7,50", "19,00")

        for mode in ("staged_staged", "pending_staged", "staged_pending", "pending_pending"):
            for reverse_order in (False, True):
                with self.subTest(mode=mode, reverse_order=reverse_order):
                    self._reset_state()
                    self._add_debit("t1", account="a", day="2026-11-23")
                    self._add_debit("t2", account="d", day="2026-12-09")
                    self._add_debit("t3", account="a", merchant="Pine Hardware", amount="19.0", day="2026-11-23")

                    a_pending = mode in ("pending_staged", "pending_pending")
                    b_pending = mode in ("staged_pending", "pending_pending")
                    low_id, high_id = self._ordered_pair("wide-doc", "narrow-doc")
                    a_id, b_id = (high_id, low_id) if reverse_order else (low_id, high_id)
                    doc_a = self._trusted_pdf_message(a_id, wide_doc) if a_pending else self.message(a_id, body=wide_doc)
                    doc_b = self._trusted_pdf_message(b_id, narrow_doc) if b_pending else self.message(b_id, body=narrow_doc)
                    doc_c = self.message("unrelated-doc", sender=UNLISTED, body=unrelated_doc)

                    items = (doc_b, doc_a, doc_c) if reverse_order else (doc_a, doc_b, doc_c)
                    self.gmail.commit(OWNER, 1, {}, items, settle_cards=False)
                    self.gmail.retain(OWNER, 1, settle_cards=True)

                    confirmed = {(row.invoice_number, row.linked_entry_ref)
                                 for row in self.rows(db.BudgetReceipt) if row.status == "confirmed"}
                    self.assertEqual(confirmed, {("TEST/99", "t3")})
                    imported_pending = [row for row in self.rows(db.BudgetReceipt) if row.status == "imported"]
                    self.assertEqual(len(imported_pending), int(a_pending) + int(b_pending))

    def test_occupied_transaction_keeps_ambiguous_document_claiming_other_free_charge(self):
        for occupied_status in ("confirmed", "needs_review"):
            with self.subTest(occupied_status=occupied_status):
                self._reset_state()
                t1_payload = self._add_debit("t1-occupied", account="a", day="2026-11-23")
                self._add_debit("t2-free", account="d", day="2026-12-09")
                self._add_debit("t3-free", account="a", merchant="Pine Hardware", amount="19.0", day="2026-11-23")

                from hestia.budget.analytics_core import financial_fingerprint
                fp = financial_fingerprint(t1_payload)
                with self.sessions.begin() as session:
                    session.add(db.BudgetReceipt(
                        id="owner-linked", owner=OWNER, status=occupied_status, source_kind="live_text",
                        document_kind="receipt", raw_shop_name="Cedar Tools", total_amount="7.5", currency="PLN",
                        payment_method="card", receipt_date="2026-11-23", version=1, created_at=self.now,
                        updated_at=self.now, linked_account_id=self.accounts["a"], linked_entry_ref="t1-occupied",
                        linked_fingerprint=fp,
                        linked_evidence=evidence_json({
                            "owner": OWNER, "account_id": self.accounts["a"], "entry_reference": "0" * 64,
                            "transaction_date": "2026-11-23", "booking_date": "2026-11-23", "value_date": None,
                            "raw_shop_name": "Cedar Tools", "raw_nip": None, "creditor_digest": None,
                            "debtor_digest": None, "remittance_digest": "0" * 64, "financial_fingerprint": fp,
                        }),
                    ))

                wide_a = self.message("wide-a", body=DOCUMENT + "Termin płatności: 10.12.2026\n")
                narrow_b = self.message("narrow-b", body=DOCUMENT.replace("TEST/42", "TEST/43").replace("2026-11-23", "2026-12-09"))
                only_occupied = self.message("only-occ", body=DOCUMENT.replace("TEST/42", "TEST/44"))
                unrelated = self.message(
                    "unrelated", body=DOCUMENT.replace("TEST/42", "TEST/99").replace("Cedar Tools", "Pine Hardware").replace("7,50", "19,00")
                )
                self.commit(wide_a, narrow_b, only_occupied, unrelated)

                with self.sessions() as session:
                    owner_row = session.get(db.BudgetReceipt, "owner-linked")
                    self.assertEqual((owner_row.status, owner_row.linked_entry_ref), (occupied_status, "t1-occupied"))
                settled = {(row.invoice_number, row.linked_entry_ref)
                           for row in self.rows(db.BudgetReceipt) if row.id != "owner-linked"}
                self.assertEqual(settled, {("TEST/99", "t3-free")})

    def test_contested_copy_groups_and_shared_rfc_groups_block_competing_single_candidates(self):
        low_id, high_id = self._ordered_pair("copy-1", "copy-2")

        with self.subTest(case="conflicting_copy_group_blocks_competing_t1_and_t2"):
            for reverse in (False, True):
                self._reset_state()
                self._add_debit("t1", account="a", amount="7.5", day="2026-11-23")
                self._add_debit("t2", account="d", amount="8.5", day="2026-11-23")
                self._add_debit("t3", account="a", merchant="Pine Hardware", amount="19.0", day="2026-11-23")

                c1 = self.message(high_id if reverse else low_id, body=DOCUMENT)
                c2 = self.message(low_id if reverse else high_id, body=DOCUMENT.replace("7,50", "8,50"))
                competing_b = self.message("b-t1", body=DOCUMENT.replace("TEST/42", "TEST/43"))
                unrelated_c = self.message(
                    "c-t3", body=DOCUMENT.replace("TEST/42", "TEST/99").replace("Cedar Tools", "Pine Hardware").replace("7,50", "19,00")
                )
                self.commit(c1, c2, competing_b, unrelated_c)
                confirmed = {(row.invoice_number, row.linked_entry_ref) for row in self.rows(db.BudgetReceipt)}
                self.assertEqual(confirmed, {("TEST/99", "t3")})

        with self.subTest(case="shared_rfc_group_blocks_competing_unique_rfc_document"):
            self._reset_state()
            self._add_debit("t1", account="a", amount="7.5", day="2026-11-23")
            self._add_debit("t2", account="d", amount="8.5", day="2026-11-23")
            self._add_debit("t3", account="a", merchant="Pine Hardware", amount="19.0", day="2026-11-23")

            shared = "<shared-msg@example.test>"
            g1 = self.message("g1", body=DOCUMENT, rfc_id=shared)
            g2 = self.message("g2", body=DOCUMENT.replace("TEST/42", "TEST/43").replace("7,50", "8,50"), rfc_id=shared)
            competing = self.message("competing", body=DOCUMENT.replace("TEST/42", "TEST/44"))
            unrelated = self.message(
                "unrelated", body=DOCUMENT.replace("TEST/42", "TEST/99").replace("Cedar Tools", "Pine Hardware").replace("7,50", "19,00")
            )
            self.commit(g1, g2, competing, unrelated)
            confirmed = {(row.invoice_number, row.linked_entry_ref) for row in self.rows(db.BudgetReceipt)}
            self.assertEqual(confirmed, {("TEST/99", "t3")})

    def test_fx_conflict_invalidates_eur_claim_without_blocking_pln_or_valid_fx_match(self):
        from hestia.budget.analytics_core import financial_fingerprint

        with self.subTest(case="fx_conflict_and_distinct_pln_amounts_both_settle"):
            self._reset_state()
            t1 = self._add_debit("t1-fx-conflict", account="a", amount="43.20",
                                 lines=["Płatność kartą Cedar Tools", "Original amount: EUR 10.00"])
            t2 = self._add_debit("t2-fx-valid", account="d", amount="44.10",
                                 lines=["Płatność kartą Cedar Tools", "Original amount: EUR 10.00"])
            with self.sessions.begin() as session:
                session.add(db.BudgetTransactionMetadata(
                    account_id=self.accounts["a"], entry_reference="t1-fx-conflict", owner=OWNER,
                    ledger_fingerprint=financial_fingerprint(t1), source="api", observed_at=self.now,
                    version=1, payload_json=json.dumps({"exchange_rate": None}),
                ))
                session.add(db.BudgetTransactionMetadata(
                    account_id=self.accounts["d"], entry_reference="t2-fx-valid", owner=OWNER,
                    ledger_fingerprint=financial_fingerprint(t2), source="api", observed_at=self.now,
                    version=1, payload_json=json.dumps({"exchange_rate": {"instructed_amount": {"currency": "EUR", "amount": "10.00"}}}),
                ))

            eur_doc = self.message("eur-doc", body=DOCUMENT.replace("7,50 PLN", "10,00 EUR"))
            pln_doc = self.message("pln-doc", body=DOCUMENT.replace("TEST/42", "TEST/43").replace("7,50", "43,20"))
            self.commit(eur_doc, pln_doc)
            confirmed = {(row.invoice_number, row.linked_entry_ref) for row in self.rows(db.BudgetReceipt)}
            self.assertEqual(confirmed, {("TEST/42", "t2-fx-valid"), ("TEST/43", "t1-fx-conflict")})

        with self.subTest(case="same_pln_amount_across_t1_and_t2_blocks_competing_eur_settlement"):
            self._reset_state()
            t1 = self._add_debit("t1-fx-conflict", account="a", amount="43.20",
                                 lines=["Płatność kartą Cedar Tools", "Original amount: EUR 10.00"])
            t2 = self._add_debit("t2-fx-valid", account="d", amount="43.20",
                                 lines=["Płatność kartą Cedar Tools", "Original amount: EUR 10.00"])
            with self.sessions.begin() as session:
                session.add(db.BudgetTransactionMetadata(
                    account_id=self.accounts["a"], entry_reference="t1-fx-conflict", owner=OWNER,
                    ledger_fingerprint=financial_fingerprint(t1), source="api", observed_at=self.now,
                    version=1, payload_json=json.dumps({"exchange_rate": None}),
                ))
                session.add(db.BudgetTransactionMetadata(
                    account_id=self.accounts["d"], entry_reference="t2-fx-valid", owner=OWNER,
                    ledger_fingerprint=financial_fingerprint(t2), source="api", observed_at=self.now,
                    version=1, payload_json=json.dumps({"exchange_rate": {"instructed_amount": {"currency": "EUR", "amount": "10.00"}}}),
                ))

            eur_doc = self.message("eur-doc", body=DOCUMENT.replace("7,50 PLN", "10,00 EUR"))
            pln_doc = self.message("pln-doc", body=DOCUMENT.replace("TEST/42", "TEST/43").replace("7,50", "43,20"))
            self.commit(eur_doc, pln_doc)
            self.assertEqual(self.rows(db.BudgetReceipt), [])

    def test_case4_auto_promoted_receipt_to_invoice_settles_and_participates_in_conflicts(self):
        receipt_body = (
            "Paragon nr CT/42\n"
            "Sprzedawca: Cedar Tools\n"
            "Data zakupu: 2026-11-23\n"
            "Kwota do zapłaty: 7,50 PLN\n"
        )
        invoice_body = (
            "Faktura VAT nr CT/42\n"
            "Sprzedawca: Cedar Tools\n"
            "NIP: 5250007738\n"
            "Data wystawienia: 2026-11-23\n"
            "Termin płatności: 15.12.2026\n"
            "Do zapłaty: 7,50 PLN\n"
        )

        with self.subTest(case="unmodified_auto_promoted_receipt_settles_later"):
            self._reset_state()
            rcpt_msg = self._trusted_pdf_message("trusted-rcpt", receipt_body)
            inv_msg = self._trusted_pdf_message("trusted-inv", invoice_body)
            self.assertFalse(rcpt_msg.card_required or inv_msg.card_required)
            self.commit(rcpt_msg)
            self.commit(inv_msg)
            row, = self.rows(db.BudgetReceipt)
            self.assertEqual((row.status, row.document_kind, row.version), ("imported", "invoice", 2))

            self._add_debit("promoted-card", day="2026-12-16",
                            lines=["Płatność kartą", "Inna nazwa sklepu", "NIP: 5250007738"])
            self.gmail.retain(OWNER, 1, settle_cards=True)
            settled, = self.rows(db.BudgetReceipt)
            self.assertEqual((settled.id, settled.status, settled.version, settled.linked_entry_ref),
                             (row.id, "confirmed", 3, "promoted-card"))
            with self.sessions() as session:
                self.assertEqual(reconcile_receipt_links(session, OWNER, self.now + 1), 0)

        with self.subTest(case="promoted_receipt_participates_in_competing_claims"):
            self._reset_state()
            rcpt_msg = self._trusted_pdf_message("trusted-rcpt", receipt_body)
            inv_msg = self._trusted_pdf_message("trusted-inv", invoice_body)
            self.commit(rcpt_msg)
            self.commit(inv_msg)
            self._add_debit("t1-wide", account="a", day="2026-12-16",
                            lines=["Płatność kartą", "Cedar Tools", "NIP: 5250007738"])
            self._add_debit("t2-wide", account="d", day="2026-11-23",
                            lines=["Płatność kartą", "Cedar Tools", "NIP: 5250007738"])
            competing = self.message("staged-competing", body=DOCUMENT.replace("TEST/42", "TEST/98"))
            self.commit(competing)
            self.assertEqual([r.status for r in self.rows(db.BudgetReceipt)], ["imported"])

        with self.subTest(case="owner_or_external_version_bump_after_promotion_stays_excluded"):
            self._reset_state()
            rcpt_msg = self._trusted_pdf_message("trusted-rcpt", receipt_body)
            inv_msg = self._trusted_pdf_message("trusted-inv", invoice_body)
            self.commit(rcpt_msg)
            self.commit(inv_msg)
            with self.sessions.begin() as session:
                row = session.scalar(select(db.BudgetReceipt))
                row.version = 3
            self._add_debit("promoted-card", day="2026-12-16",
                            lines=["Płatność kartą", "Cedar Tools", "NIP: 5250007738"])
            self.gmail.retain(OWNER, 1, settle_cards=True)
            row, = self.rows(db.BudgetReceipt)
            self.assertEqual((row.status, row.version, row.linked_entry_ref), ("imported", 3, None))

        with self.subTest(case="unmarked_version2_imported_receipt_stays_excluded"):
            self._reset_state()
            rcpt_msg = self._trusted_pdf_message("trusted-rcpt", receipt_body)
            self.commit(rcpt_msg)
            with self.sessions.begin() as session:
                row = session.scalar(select(db.BudgetReceipt))
                row.version = 2
            self._add_debit("card", day="2026-11-23")
            self.gmail.retain(OWNER, 1, settle_cards=True)
            row, = self.rows(db.BudgetReceipt)
            self.assertEqual((row.status, row.version, row.linked_entry_ref), ("imported", 2, None))

    def test_shared_rfc_mutation_and_existing_document_pruning_are_hmac_order_independent(self):
        low_id, high_id = self._ordered_pair("copy-a", "copy-b")

        with self.subTest(case="settled_duplicate_copy_rfc_blocks_later_distinct_document"):
            for copy_shared_first in (False, True):
                self._reset_state()
                self._add_debit("debit-a", account="a", amount="7.5")
                self._add_debit("debit-b", account="d", amount="8.5")
                shared_rfc = "<copy-shared@example.test>"
                unique_rfc = "<copy-unique@example.test>"
                id_shared, id_unique = (low_id, high_id) if copy_shared_first else (high_id, low_id)

                copy_shared = self.message(id_shared, body=DOCUMENT, rfc_id=shared_rfc)
                copy_unique = self.message(id_unique, body=DOCUMENT, rfc_id=unique_rfc)
                self.commit(copy_shared, copy_unique)
                self.assertEqual(len(self.rows(db.BudgetReceipt)), 1)

                later_distinct = self.message(
                    "later-distinct",
                    body=DOCUMENT.replace("TEST/42", "TEST/43").replace("7,50", "8,50"),
                    rfc_id=shared_rfc,
                )
                self.commit(later_distinct)
                self.assertEqual(len(self.rows(db.BudgetReceipt)), 1)
                later_row = next(r for r in self.rows(db.BudgetGmailMessage)
                                 if r.gmail_id_hmac == later_distinct.gmail_id_hmac)
                self.assertEqual(later_row.outcome, "skipped_duplicate")

        with self.subTest(case="existing_document_pruning_does_not_unblock_colliding_staged_rfc"):
            for existing_low in (True, False):
                self._reset_state()
                self._add_debit("debit-a", account="a", amount="7.5")
                self._add_debit("debit-b", account="d", amount="8.5")
                existing_allow = self._trusted_pdf_message("allow-a", DOCUMENT, rfc_id="<allow-a@example.test>")
                self.commit(existing_allow)

                shared_rfc = "<pruned-shared@example.test>"
                id_dup, id_other = (low_id, high_id) if existing_low else (high_id, low_id)
                staged_dup = self.message(id_dup, body=DOCUMENT, rfc_id=shared_rfc)
                staged_other = self.message(
                    id_other,
                    body=DOCUMENT.replace("TEST/42", "TEST/43").replace("7,50", "8,50"),
                    rfc_id=shared_rfc,
                )
                self.commit(staged_dup, staged_other)
                self.gmail.retain(OWNER, 1, settle_cards=True)
                self.assertEqual([(r.invoice_number, r.status) for r in self.rows(db.BudgetReceipt)],
                                 [("TEST/42", "confirmed")])


    def test_pruning_cannot_make_a_conflicting_copy_eligible(self):
        self._add_debit("paid-original")
        shared_rfc = "<already-settled@example.test>"
        self.commit(self.message("earlier", rfc_id=shared_rfc))
        self._add_debit("unrelated-charge", amount="8.5")
        other_doc = DOCUMENT.replace("TEST/42", "TEST/43")
        copy_with_settled_rfc = self.message("copy-with-settled-rfc", body=other_doc, rfc_id=shared_rfc)
        conflicting_copy = self.message("conflicting-copy", body=other_doc.replace("7,50", "8,50"))
        self.commit(copy_with_settled_rfc, conflicting_copy)
        self.gmail.retain(OWNER, 1, settle_cards=True)

        row, = self.rows(db.BudgetReceipt)
        self.assertEqual((row.invoice_number, row.linked_entry_ref), ("TEST/42", "paid-original"))
        with self.sessions() as session:
            for item in (copy_with_settled_rfc, conflicting_copy):
                self.assertIsNotNone(session.get(db.AppMeta, waiting_key(OWNER, item.gmail_id_hmac)))

    def test_promoted_invoice_keeps_card_scope_and_rejects_conflicting_payment_details(self):
        receipt = DOCUMENT.replace("Faktura VAT nr TEST/42", "Paragon nr TEST/42")
        invoice = DOCUMENT + "Karta płatnicza: **** 0123\n"
        for payment_details, expected in (
            ("", ("confirmed", "right-card", "0123")),
            ("Karta płatnicza: **** 9876\n", ("imported", None, "9876")),
            ("Płatność BLIK\n", ("imported", None, None)),
        ):
            with self.subTest(payment_details=payment_details):
                self._reset_state()
                self.commit(self._trusted_pdf_message("receipt", receipt + payment_details))
                self.commit(self._trusted_pdf_message("invoice", invoice))
                for reference, account, suffix in (("wrong-card", "a", "9876"), ("right-card", "d", "0123")):
                    self._add_debit(reference, account=account,
                                    lines=["Płatność kartą", "Cedar Tools", f"Visa ending in {suffix}"])
                self._add_debit("blik", lines=["Płatność BLIK Cedar Tools"])
                self.gmail.retain(OWNER, 1, settle_cards=True)
                row, = self.rows(db.BudgetReceipt)
                self.assertEqual(row.document_kind, "invoice")
                self.assertEqual((row.status, row.linked_entry_ref, row.card_last4), expected)

    def test_two_gmail_sources_for_one_undated_receipt_keep_both_debits_unlinked(self):
        item = self.message("undated-invoice", sender=MERCHANT, body=INVOICE)
        self.commit(replace(item, invoice=replace(item.invoice, issue_date=None, due_date=None)))
        row, = self.rows(db.BudgetReceipt)
        with self.sessions.begin() as session:
            session.add(db.BudgetGmailMessage(
                owner=OWNER, gmail_id_hmac=digest(SECRET, GMAIL_MESSAGE_DOMAIN, "other-source"),
                internal_date=item.internal_date + 14 * 86400, created_at=self.now,
                trust_path=item.trust_path, outcome="imported", receipt_id=row.id,
            ))

        for reference, day in (("early", "2026-11-25"), ("late", "2026-12-09")):
            self._add_debit(reference, merchant="Respect Energy", amount="123.45", day=day)
        self.gmail.retain(OWNER, 1, settle_cards=True)
        pending, = self.rows(db.BudgetReceipt)
        self.assertEqual((pending.id, pending.status, pending.linked_entry_ref), (row.id, "imported", None))

    def test_malformed_staged_copy_without_rfc_cannot_settle_a_conflicting_copy(self):
        valid = self.message("valid-copy")
        malformed = self.message("malformed-copy")
        malformed = replace(malformed, rfc_message_id_hmac=None,
                            invoice=replace(malformed.invoice, amount="invalid"))
        self.add_card()
        self.commit(valid, malformed)
        self.assertEqual(self.rows(db.BudgetReceipt), [])


    def test_staggered_expiry_keeps_conflicting_copies_until_both_expire(self):
        for conflict in ("document_number", "shared_rfc"):
            with self.subTest(conflict=conflict):
                self._reset_state()
                first = self.message("early-copy")
                body = DOCUMENT.replace("7,50", "8,50")
                if conflict == "shared_rfc":
                    body = body.replace("TEST/42", "TEST/43")
                later = self.message("later-copy", body=body, rfc_id=(
                    "<early-copy@example.test>" if conflict == "shared_rfc" else None
                ))
                self.commit(first)
                self.now += 60
                self.commit(later)
                self.now += IMPORT_TTL - 59
                self._add_debit("only-survivor-could-match", amount="8.5")
                self.gmail.retain(OWNER, 1)
                self.gmail.retain(OWNER, 1, settle_cards=True)
                self.assertEqual(self.rows(db.BudgetReceipt), [])
                self.assertTrue(self.gmail.known(OWNER, first.gmail_id_hmac))
                self.assertTrue(self.gmail.known(OWNER, later.gmail_id_hmac))

                self.now += 59
                self.gmail.retain(OWNER, 1, settle_cards=True)
                self.assertEqual(self.rows(db.BudgetReceipt), [])
                self.assertFalse(self.gmail.known(OWNER, first.gmail_id_hmac))
                self.assertFalse(self.gmail.known(OWNER, later.gmail_id_hmac))

    def test_global_expiry_follows_transitive_copy_edges_within_one_owner(self):
        shared = "<shared@example.test>"
        bridge = "<bridge@example.test>"
        first = self.message("first", rfc_id=shared)
        middle = DOCUMENT.replace("TEST/42", "TEST/43")
        left = self.message("left", body=middle, rfc_id=shared)
        right = self.message("right", body=middle, rfc_id=bridge)
        last = self.message("last", body=DOCUMENT.replace("TEST/42", "TEST/44"), rfc_id=bridge)
        with self.sessions.begin() as session:
            for item in (first, left, right):
                stage(session, OWNER, item, self.now)
            stage(session, OWNER, last, self.now + 1)
            stage(session, "other", first, self.now + 2)
            retain_imports(session, None, self.now)
        for item in (first, left, right, last):
            self.assertTrue(self.gmail.known(OWNER, item.gmail_id_hmac))

        with self.sessions.begin() as session:
            retain_imports(session, None, self.now + 1)
            self.assertIsNotNone(session.get(db.AppMeta, waiting_key("other", first.gmail_id_hmac)))
        for item in (first, left, right, last):
            self.assertFalse(self.gmail.known(OWNER, item.gmail_id_hmac))

    def test_later_distinct_document_reopens_and_unlinks_an_automatic_confirmation(self):
        for trusted in (False, True):
            with self.subTest(trusted=trusted):
                self._reset_state()
                self.add_card()
                self.commit(self.message("first"))
                original_id = self.assert_linked_once("TEST/42")

                self.now += 60
                body = DOCUMENT.replace("TEST/42", "TEST/43")
                challenger = (self._trusted_pdf_message("later", body) if trusted else self.message(
                    "later", body="Receipt\nMerchant: Cedar Tools\nDate: 2026-11-23\nGrand Total: PLN 7.50",
                ))
                self.commit(challenger)

                with self.sessions.begin() as session:
                    row = session.get(db.BudgetReceipt, original_id)
                    self.assertEqual((row.status, row.version), ("needs_review", 3))
                    self.assertEqual((row.linked_account_id, row.linked_entry_ref, row.linked_fingerprint,
                                      row.linked_evidence), (None, None, None, None))
                    audit, = session.scalars(select(db.BudgetAnalyticsAudit).where(
                        db.BudgetAnalyticsAudit.subject_id == original_id,
                        db.BudgetAnalyticsAudit.action == "source_changed",
                    ))
                    self.assertTrue(json.loads(audit.before_json)["linked"])
                    after = json.loads(audit.after_json)
                    self.assertEqual((after["actor"], after["status"], after["linked"]),
                                     ("gmail_import", "needs_review", False))
                    # Receipt review can outlive the Gmail provenance ledger.
                    session.execute(delete(db.BudgetGmailMessage).where(db.BudgetGmailMessage.receipt_id == original_id))

                self.gmail.retain(OWNER, 1, settle_cards=True)
                self.assertEqual({r.invoice_number: (r.status, r.linked_entry_ref) for r in self.rows(db.BudgetReceipt)},
                                 {"TEST/42": ("needs_review", None), "TEST/43": ("imported", None)}
                                 if trusted else {"TEST/42": ("needs_review", None)})
                self.assertTrue(self.gmail.known(OWNER, challenger.gmail_id_hmac))

    def test_later_distinct_document_preserves_an_owner_confirmation(self):
        for preserve_auto_link in (False, True):
            with self.subTest(preserve_auto_link=preserve_auto_link):
                self._reset_state()
                self.add_card()
                first = self._trusted_pdf_message("owner-first", DOCUMENT)
                self.gmail.commit(OWNER, 1, {}, [first], settle_cards=preserve_auto_link)
                original, = self.rows(db.BudgetReceipt)
                store = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"card-review-test")
                permissions = authz.LEGACY_RECEIPT_PERMISSIONS
                link = None
                if preserve_auto_link:
                    permissions = permissions - {"budget.receipts.match"}
                else:
                    match, = store.detail(OWNER, original.id)["candidates"]
                    link = {key: match[key] for key in ("account_id", "entry_reference", "candidate_token")}
                with patch.object(authz, "LEGACY_RECEIPT_PERMISSIONS", permissions):
                    store.confirm(OWNER, {
                        "receipt_id": original.id, "expected_version": original.version,
                        "receipt_kind": "purchase", "receipt_date": original.receipt_date, "receipt_time": None,
                        "total_amount": original.total_amount, "currency": original.currency, "payment_method": "card",
                        "link": link,
                    })

                self.now += 60
                challenger = self.message("later", body=DOCUMENT.replace("TEST/42", "TEST/43"))
                self.commit(challenger)
                self.gmail.retain(OWNER, 1, settle_cards=True)

                self.assertEqual(self.assert_linked_once("TEST/42"), original.id)
                row, = self.rows(db.BudgetReceipt)
                self.assertEqual((row.status, row.version), ("confirmed", original.version + 1))
                self.assertEqual(json.loads(row.linked_evidence).get("gmail_card"), True if preserve_auto_link else None)
                self.assertTrue(self.gmail.known(OWNER, challenger.gmail_id_hmac))
                actions = [audit.action for audit in self.rows(db.BudgetAnalyticsAudit) if audit.subject_id == row.id]
                self.assertEqual(actions, ["confirm"] * (2 if preserve_auto_link else 1))

    def test_conflicting_copies_of_the_same_document_do_not_reopen_the_link(self):
        self.add_card()
        self.commit(self.message("first"))
        original_id = self.assert_linked_once("TEST/42")
        self.commit(self.message("copy-a"), self.message("copy-b", body=DOCUMENT.replace("7,50", "8,50")))
        self.assertEqual(self.assert_linked_once("TEST/42"), original_id)
        row, = self.rows(db.BudgetReceipt)
        self.assertEqual((row.status, row.version), ("confirmed", 2))

    def test_review_claim_cannot_reopen_another_automatic_confirmation(self):
        self.add_card()
        wide = DOCUMENT + "Termin płatności: 10.12.2026\n"
        self.commit(self.message("wide", body=wide))
        self._add_debit("later-debit", day="2026-12-09")
        later = DOCUMENT.replace("TEST/42", "TEST/43").replace("23.11.2026", "09.12.2026")
        self.commit(self.message("later-charge", body=later))
        competitor = self.message("overlap", body=DOCUMENT.replace("TEST/42", "TEST/44"))
        self.commit(competitor)
        self.gmail.retain(OWNER, 1, settle_cards=True)
        self.assertEqual({r.invoice_number: (r.status, r.linked_entry_ref) for r in self.rows(db.BudgetReceipt)},
                         {"TEST/42": ("needs_review", None), "TEST/43": ("confirmed", "later-debit")})

    def test_unmarked_version_two_invoice_claims_a_debit_without_ever_confirming(self):
        invoice = self._trusted_pdf_message("older-invoice", DOCUMENT)
        self.commit(invoice)
        with self.sessions.begin() as session:
            row = session.scalar(select(db.BudgetReceipt))
            row.version = 2
            original_id = row.id

        self.add_card()
        self.gmail.retain(OWNER, 1, settle_cards=True)
        challenger = self.message("later", body=DOCUMENT.replace("TEST/42", "TEST/43"))
        self.commit(challenger)
        self.gmail.retain(OWNER, 1, settle_cards=True)

        row, = self.rows(db.BudgetReceipt)
        self.assertEqual((row.id, row.status, row.version, row.linked_entry_ref),
                         (original_id, "imported", 2, None))
        self.assertTrue(self.gmail.known(OWNER, challenger.gmail_id_hmac))

    def _google_invoice(self, mail_id, number, amount="7,50"):
        body = (DOCUMENT.replace("TEST/42", number).replace("Cedar Tools", "Google Ireland Limited")
                .replace("7,50", amount))
        return self.message(mail_id, body=body)

    def _confirm_google_workspace(self):
        self._add_debit("synthetic-card", lines=["Card purchase", "GOOGLE*Workspace"])
        self.commit(self._google_invoice("workspace", "WS/100"))
        return self.assert_linked_once("WS/100")

    def test_uncorroborated_google_debit_cannot_reopen_workspace_or_confirm_youtube(self):
        original_id = self._confirm_google_workspace()
        self._add_debit("youtube-charge", amount="19", day="2026-11-24",
                        lines=["Card purchase", "GOOGLE*YouTube"])
        youtube = self._google_invoice("youtube", "YT/100", "19,00")
        self.commit(youtube)
        self.gmail.retain(OWNER, 1, settle_cards=True)

        row, = self.rows(db.BudgetReceipt)
        self.assertEqual((row.id, row.status, row.version, row.linked_entry_ref),
                         (original_id, "confirmed", 2, "synthetic-card"))
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.AppMeta, waiting_key(OWNER, youtube.gmail_id_hmac)))

    def test_positive_google_claim_still_reopens_workspace(self):
        original_id = self._confirm_google_workspace()
        competitor = self._google_invoice("competing-workspace", "WS/101")
        self.commit(competitor)
        self.gmail.retain(OWNER, 1, settle_cards=True)

        row, = self.rows(db.BudgetReceipt)
        self.assertEqual((row.id, row.status, row.version, row.linked_entry_ref),
                         (original_id, "needs_review", 3, None))
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.AppMeta, waiting_key(OWNER, competitor.gmail_id_hmac)))

    def test_numberless_forward_can_reopen_workspace_only_on_a_positive_match(self):
        for amount, expected in (
            ("19.00", ("confirmed", 2, "synthetic-card")),
            ("7.50", ("needs_review", 3, None)),
        ):
            with self.subTest(amount=amount):
                self._reset_state()
                original_id = self._confirm_google_workspace()
                body = f"Receipt\nMerchant: Google Ireland Limited\nDate: 2026-11-23\nGrand Total: PLN {amount}"
                forward = _email(self.processor.settings.spouse_forward_senders[0], "Forwarded receipt", rfc_id=None)
                forward.add_attachment(_email(UNLISTED, body, rfc_id=None))
                mail_id = "numberless-forward"
                run = Mock()
                run.message.side_effect = [_metadata(forward) | {"id": mail_id}, _raw(forward) | {"id": mail_id}]
                pending = self.processor.process(run, mail_id, digest(SECRET, GMAIL_MESSAGE_DOMAIN, mail_id))
                self.assertTrue(pending.card_required)
                self.assertEqual(pending.trust_path, "spouse_forward")
                self.assertEqual(
                    (pending.rfc_message_id_hmac, pending.invoice.invoice_number,
                     pending.document_provenance["number_hmac"]),
                    (None, None, None),
                )
                self.commit(pending)
                self.gmail.retain(OWNER, 1, settle_cards=True)

                row, = self.rows(db.BudgetReceipt)
                self.assertEqual(row.id, original_id)
                self.assertEqual((row.status, row.version, row.linked_entry_ref), expected)
                with self.sessions() as session:
                    self.assertIsNotNone(session.get(db.AppMeta, waiting_key(OWNER, pending.gmail_id_hmac)))
