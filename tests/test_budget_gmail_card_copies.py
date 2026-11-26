"""Regressions for staged Gmail card copy conflicts and shared RFC message IDs."""
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from sqlalchemy import delete

from hestia import db
from hestia.budget.gmail_card_match import still_matches_card
from hestia.budget.gmail_document import ImportedDocument
from hestia.budget.gmail_message import GmailMessageProcessor
from hestia.budget.gmail_payment import _payee_key
from hestia.budget.gmail_store import (
    DOCUMENT_NUMBER_DOMAIN,
    GMAIL_MESSAGE_DOMAIN,
    MAILBOX_DOMAIN,
    RFC_MESSAGE_DOMAIN,
    GmailStore,
    PendingMessage,
    digest,
)
from hestia.budget.gmail_worker import _render, _validate_output
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_gmail_discovery import DOCUMENT, UNLISTED
from tests.test_budget_gmail_message import MERCHANT, SECRET, _email, _metadata, _raw
from tests.test_budget_gmail_sync import OWNER, SETTINGS

_WARSAW = ZoneInfo("Europe/Warsaw")


def _ordered_mail_ids():
    pair = ("copy-alpha", "copy-beta")
    if digest(SECRET, GMAIL_MESSAGE_DOMAIN, pair[0]) < digest(SECRET, GMAIL_MESSAGE_DOMAIN, pair[1]):
        return pair
    return pair[1], pair[0]


class GmailCardCopySettlementTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.now = 1795600000
        self.gmail = GmailStore(self.sessions, clock=lambda: self.now)
        self.gmail.transition(OWNER, 1, "active", digest(SECRET, MAILBOX_DOMAIN, SETTINGS.owner_mailbox))
        settings = replace(SETTINGS, hmac_secret=SECRET, sender_allowlist=(MERCHANT,),
                           require_sender_authentication=False)
        worker = Mock()
        worker.parse.side_effect = lambda kind, payload: _validate_output(
            _render({"text": b"T", "html": b"H", "pdf": b"P"}[kind] + payload)
        )
        self.processor = GmailMessageProcessor(settings, worker, discover_documents=True)

    def _reset_case(self):
        with self.sessions.begin() as session:
            session.execute(delete(db.BudgetReceiptLine))
            session.execute(delete(db.BudgetGmailMessage))
            session.execute(delete(db.BudgetReceipt))
            session.execute(delete(db.BudgetAnalyticsAudit))
            session.execute(delete(db.BudgetTransaction))
            session.execute(delete(db.AppMeta).where(db.AppMeta.key.startswith("budget_gmail_card_wait:")))
            session.execute(delete(db.AppMeta).where(db.AppMeta.key.startswith("budget_gmail_document_number:")))
        self.now = 1795600000

    def _message(self, mail_id, *, body=DOCUMENT, rfc_id=None, internal_date=None):
        message = _email(UNLISTED, body, rfc_id=rfc_id)
        del message["Authentication-Results"]
        run = Mock()
        meta = _metadata(message) | {"id": mail_id}
        if internal_date is not None:
            meta["internalDate"] = str(int(internal_date * 1000))
        run.message.side_effect = [meta, _raw(message) | {"id": mail_id}]
        return self.processor.process(run, mail_id, digest(SECRET, GMAIL_MESSAGE_DOMAIN, mail_id))

    def _add_card(self, reference, *, merchant="Cedar Tools", amount="7.5", day="2026-11-23", lines=None):
        remittance = ["Płatność kartą", merchant] if lines is None else lines
        self.add_transaction("a", reference, entry_reference=reference, amount=amount,
                             booking_date=day, transaction_date=day, remittance_information=remittance)

    def _staged_pending(self, mail_id, invoice, *, received_day="2026-11-23", rfc_id=None):
        stamp = datetime.fromisoformat(f"{received_day}T12:00:00").replace(tzinfo=_WARSAW).timestamp()
        number_hmac = (digest(SECRET, DOCUMENT_NUMBER_DOMAIN, invoice.invoice_number.casefold())
                       if invoice.invoice_number else None)
        return PendingMessage(
            digest(SECRET, GMAIL_MESSAGE_DOMAIN, mail_id),
            stamp,
            "untrusted",
            "imported",
            rfc_message_id_hmac=digest(SECRET, RFC_MESSAGE_DOMAIN, rfc_id) if rfc_id else None,
            invoice=invoice,
            source_kind="email",
            document_provenance={
                "number_hmac": number_hmac,
                "seller_key": _payee_key(invoice.issuer),
                "paid": invoice.paid,
                "receipt_number_hmac": None,
            },
            card_required=True,
        )

    def _commit(self, *items, settle_cards=True):
        return self.gmail.commit(OWNER, 1, {}, items, settle_cards=settle_cards)

    def test_shared_rfc_message_id_does_not_confirm_distinct_staged_documents(self):
        low_id, high_id = _ordered_mail_ids()
        shared_rfc = "<shared-envelope@example.test>"
        doc_a = DOCUMENT
        doc_b = DOCUMENT.replace("TEST/42", "TEST/43").replace("7,50", "8,50")

        for case, pairs in (
            ("same_seller_distinct_numbers_low_first", ((low_id, doc_a), (high_id, doc_b))),
            ("same_seller_distinct_numbers_high_first", ((high_id, doc_a), (low_id, doc_b))),
            ("distinct_sellers", ((low_id, doc_a),
                                  (high_id, doc_b.replace("Cedar Tools", "Pine Hardware")))),
            ("numbered_and_unnumbered", ((low_id, doc_a),
                                         (high_id, doc_b.replace("Faktura VAT nr TEST/43", "Paragon")))),
        ):
            with self.subTest(case=case):
                self._reset_case()
                self._add_card("debit-a", merchant="Cedar Tools", amount="7.5")
                second_merchant = "Pine Hardware" if case == "distinct_sellers" else "Cedar Tools"
                self._add_card("debit-b", merchant=second_merchant, amount="8.5")
                first = self._message(pairs[0][0], body=pairs[0][1], rfc_id=shared_rfc)
                second = self._message(pairs[1][0], body=pairs[1][1], rfc_id=shared_rfc)
                self._commit(first, second, settle_cards=True)
                self.gmail.retain(OWNER, 1, settle_cards=True)
                self.assertEqual(self.rows(db.BudgetReceipt), [])
                self.assertEqual(self.rows(db.BudgetGmailMessage), [])
                self.assertTrue(all(self.gmail.known(OWNER, item.gmail_id_hmac) for item in (first, second)))

        for copy_order in ((low_id, high_id), (high_id, low_id)):
            with self.subTest(case="multi_copy_group_sharing_rfc", copy_order=copy_order):
                self._reset_case()
                self._add_card("debit-a", merchant="Cedar Tools", amount="7.5")
                self._add_card("debit-b", merchant="Cedar Tools", amount="8.5")
                copy_unique = self._message(copy_order[0], body=doc_a, rfc_id="<unique-copy@example.test>")
                copy_shared = self._message(copy_order[1], body=doc_a, rfc_id=shared_rfc)
                other_doc = self._message("copy-gamma", body=doc_b, rfc_id=shared_rfc)
                for item in (copy_unique, copy_shared, other_doc):
                    self._commit(item, settle_cards=False)
                self.gmail.retain(OWNER, 1, settle_cards=True)
                self.assertEqual(self.rows(db.BudgetReceipt), [])
                self.assertEqual(self.rows(db.BudgetGmailMessage), [])
                self.assertTrue(
                    all(self.gmail.known(OWNER, item.gmail_id_hmac) for item in (copy_unique, copy_shared, other_doc))
                )

        with self.subTest(case="identical_copies_with_shared_rfc_still_confirm_once"):
            self._reset_case()
            self._add_card("debit-a", merchant="Cedar Tools", amount="7.5")
            first = self._message(low_id, body=doc_a, rfc_id=shared_rfc)
            duplicate = self._message(high_id, body=doc_a, rfc_id=shared_rfc)
            self._commit(first, duplicate, settle_cards=True)
            receipt, = self.rows(db.BudgetReceipt)
            self.assertEqual((receipt.invoice_number, receipt.linked_entry_ref), ("TEST/42", "debit-a"))
            outcomes = sorted(row.outcome for row in self.rows(db.BudgetGmailMessage))
            self.assertEqual(outcomes, ["imported", "skipped_duplicate"])

        with self.subTest(case="missing_rfc_ids_do_not_block_distinct_documents"):
            self._reset_case()
            self._add_card("debit-a", merchant="Cedar Tools", amount="7.5")
            self._add_card("debit-b", merchant="Cedar Tools", amount="8.5")
            first = self._message(low_id, body=doc_a, rfc_id=None)
            second = self._message(high_id, body=doc_b, rfc_id=None)
            self._commit(first, second, settle_cards=True)
            confirmed = {(row.invoice_number, row.linked_entry_ref) for row in self.rows(db.BudgetReceipt)}
            self.assertEqual(confirmed, {("TEST/42", "debit-a"), ("TEST/43", "debit-b")})

        with self.subTest(case="sequential_shared_rfc_does_not_confirm_second_document"):
            self._reset_case()
            self._add_card("debit-a", merchant="Cedar Tools", amount="7.5")
            self._add_card("debit-b", merchant="Cedar Tools", amount="8.5")
            first = self._message(low_id, body=doc_a, rfc_id=shared_rfc)
            second = self._message(high_id, body=doc_b, rfc_id=shared_rfc)
            self._commit(first, settle_cards=True)
            self._commit(second, settle_cards=True)
            receipt, = self.rows(db.BudgetReceipt)
            self.assertEqual((receipt.invoice_number, receipt.linked_entry_ref), ("TEST/42", "debit-a"))
            outcomes = {row.gmail_id_hmac: row.outcome for row in self.rows(db.BudgetGmailMessage)}
            self.assertEqual(outcomes, {first.gmail_id_hmac: "imported", second.gmail_id_hmac: "skipped_duplicate"})

    def test_conflicting_matching_discriminators_are_independent_of_gmail_hmac(self):
        low_id, high_id = _ordered_mail_ids()
        base = ImportedDocument("Cedar Tools", None, "TEST/42", "7.5", "PLN",
                                "2026-11-23", None, "invoice", False, None)

        conflict_cases = (
            ("due_date_widened_window",
             self._message(low_id, body=DOCUMENT + "Termin płatności: 25.11.2026\n", rfc_id="<due-1@example.test>"),
             self._message(high_id, body=DOCUMENT + "Termin płatności: 15.12.2026\n", rfc_id="<due-2@example.test>"),
             {"day": "2026-12-16"}),
            ("due_date_same_window",
             self._message(low_id, body=DOCUMENT, rfc_id="<due-none@example.test>"),
             self._message(high_id, body=DOCUMENT + "Termin płatności: 25.11.2026\n", rfc_id="<due-set@example.test>"),
             {"day": "2026-11-23"}),
            ("nip_discriminator",
             self._message(low_id, body=DOCUMENT + "Termin płatności: 15.12.2026\n", rfc_id="<nip-none@example.test>"),
             self._message(high_id, body=DOCUMENT + "NIP: 5250007738\nTermin płatności: 15.12.2026\n",
                           rfc_id="<nip-set@example.test>"),
             {"day": "2026-12-16", "lines": ["Płatność kartą", "Inna nazwa sklepu", "NIP: 5250007738"]}),
            ("document_kind_invoice_vs_receipt",
             self._message(low_id, body=DOCUMENT.replace("Faktura VAT nr TEST/42", "Invoice #TEST-42"),
                           rfc_id="<kind-inv@example.test>"),
             self._message(high_id, body=DOCUMENT.replace("Faktura VAT nr TEST/42", "Receipt #TEST-42"),
                           rfc_id="<kind-rcpt@example.test>"),
             {"day": "2026-11-23"}),
            ("document_kind_credit_note_vs_invoice",
             self._staged_pending(low_id, replace(base, document_kind="credit_note"), rfc_id="kind-cn@example.test"),
             self._staged_pending(high_id, base, rfc_id="kind-inv@example.test"),
             {"day": "2026-11-23"}),
            ("received_date_when_issue_date_missing",
             self._staged_pending(low_id, replace(base, issue_date=None),
                                  received_day="2026-11-23", rfc_id="undated-1@example.test"),
             self._staged_pending(high_id, replace(base, issue_date=None),
                                  received_day="2026-12-10", rfc_id="undated-2@example.test"),
             {"day": "2026-11-23"}),
            ("issuer_word_tokens_with_same_payee_key",
             self._staged_pending(low_id, replace(base, issuer="Google Commerce Limited"), rfc_id="tok-1@example.test"),
             self._staged_pending(high_id, replace(base, issuer="Google Commerce PBC"), rfc_id="tok-2@example.test"),
             {"day": "2026-11-23", "lines": ["Płatność kartą", "GOOGLE*Google Storage"]}),
        )

        low_hmac = digest(SECRET, GMAIL_MESSAGE_DOMAIN, low_id)
        high_hmac = digest(SECRET, GMAIL_MESSAGE_DOMAIN, high_id)
        for label, first_item, second_item, card_kwargs in conflict_cases:
            for order, (left, right) in (
                ("low_then_high", (first_item, second_item)),
                ("high_then_low", (replace(second_item, gmail_id_hmac=low_hmac),
                                   replace(first_item, gmail_id_hmac=high_hmac))),
            ):
                with self.subTest(case=label, order=order):
                    self._reset_case()
                    self._add_card("debit", **card_kwargs)
                    self._commit(left, right, settle_cards=True)
                    self.gmail.retain(OWNER, 1, settle_cards=True)
                    self.assertEqual(self.rows(db.BudgetReceipt), [])
                    self.assertEqual(self.rows(db.BudgetGmailMessage), [])
                    self.assertTrue(self.gmail.known(OWNER, low_hmac))
                    self.assertTrue(self.gmail.known(OWNER, high_hmac))

        agreeing_a = self._message(
            low_id,
            body=DOCUMENT + "NIP: 5250007738\nTermin płatności: 15.12.2026\n",
            rfc_id="<agree-1@example.test>",
            internal_date=datetime(2026, 11, 23, 12, tzinfo=_WARSAW).timestamp(),
        )
        agreeing_b = self._message(
            high_id,
            body=(DOCUMENT.replace("Cedar Tools", "Cedar Tools Sp. z o.o.")
                  + "NIP: 5250007738\nTermin płatności: 15.12.2026\nZapłacono: 7,50 PLN\n"),
            rfc_id="<agree-2@example.test>",
            internal_date=datetime(2026, 11, 26, 12, tzinfo=_WARSAW).timestamp(),
        )
        for order, (left, right) in (
            ("low_then_high", (agreeing_a, agreeing_b)),
            ("high_then_low", (replace(agreeing_b, gmail_id_hmac=low_hmac),
                               replace(agreeing_a, gmail_id_hmac=high_hmac))),
        ):
            with self.subTest(case="agreeing_copies_still_confirm_once", order=order):
                self._reset_case()
                self._add_card("debit", day="2026-12-16",
                               lines=["Płatność kartą", "Inna nazwa sklepu", "NIP: 5250007738"])
                self._commit(left, right, settle_cards=True)
                receipt, = self.rows(db.BudgetReceipt)
                self.assertEqual((receipt.invoice_number, receipt.due_date, receipt.raw_nip, receipt.linked_entry_ref),
                                 ("TEST/42", "2026-12-15", "5250007738", "debit"))
                self.assertEqual(sorted(row.outcome for row in self.rows(db.BudgetGmailMessage)),
                                 ["imported", "skipped_duplicate"])
                with self.sessions() as session:
                    self.assertTrue(still_matches_card(session, session.get(db.BudgetReceipt, receipt.id)))
