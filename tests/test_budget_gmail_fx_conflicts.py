"""FX conflicts through bank ingestion and Gmail receipt settlement."""

from dataclasses import replace
import json
import unittest

from hestia import db
from hestia.budget import gmail_card_import, normalize
from hestia.budget.analytics_core import financial_fingerprint
from hestia.budget.gmail_card_match import match_card_purchase
from hestia.budget.gmail_document import ImportedDocument
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_normalize as bank
from tests import test_budget_store as fixtures
from tests.test_budget_receipts import confirm_values, receipt_text


_REFERENCE = "synthetic-fx-purchase"
_RECEIPT_ID = "10000000-0000-4000-8000-000000000001"
_DAY = bank.TODAY.isoformat()
_DOCUMENT = ImportedDocument(
    "BIEDRONKA", None, "TEST-FX-1", "10", "EUR", _DAY, None, "invoice", True, None,
)


def _purchase(original_amount=None):
    transaction = bank.transaction(
        _REFERENCE,
        transaction_amount={"amount": "43.20", "currency": "PLN"},
        booking_date=_DAY,
        transaction_date=_DAY,
        remittance_information=["Płatność kartą BIEDRONKA", "Original amount: EUR 10.00"],
    )
    if original_amount is not None:
        transaction["exchange_rate"] = {
            "instructed_amount": {"currency": "EUR", "amount": original_amount},
        }
    return transaction


class GmailFxConflictTests(unittest.TestCase):
    tearDown = fixtures.BudgetStoreTests.tearDown

    def setUp(self):
        fixtures.BudgetStoreTests.setUp(self)
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
        self.config = fixtures.configuration(accounts=(bank.PRIMARY,))
        fixtures.prepare_selected(self.store, self.config)
        self.account_id = self.store.accounts("owner")[0]["id"]
        self.transaction_key = (self.account_id, _REFERENCE)

    def observe(self, *transactions):
        self.now += 1
        client = bank.FakeClient()
        client.pages[bank.ACCOUNT] = [{"transactions": list(transactions)}]
        snapshot = normalize.fetch_snapshot(
            client, bank.SESSION, self.config.connections[0], today=bank.TODAY, now=bank.NOW,
        )
        self.assertEqual(len(snapshot.accounts[0].transactions), 1)
        self.store.commit_snapshot("bank", snapshot)

    def fx_evidence(self):
        with self.sessions() as session:
            row = session.get(db.BudgetTransactionMetadata, self.transaction_key)
            return json.loads(row.payload_json)["exchange_rate"]

    def match(self, document, *, receipt_id=None):
        with self.sessions() as session:
            return match_card_purchase(session, "owner", document, include_receipt_id=receipt_id)

    def stage_foreign_receipt(self):
        with self.sessions.begin() as session:
            receipt = db.BudgetReceipt(
                id=_RECEIPT_ID, owner="owner", status="imported", source_kind="attachment",
                document_kind="invoice", invoice_number=_DOCUMENT.invoice_number,
                raw_shop_name=_DOCUMENT.issuer, total_amount=_DOCUMENT.amount, currency=_DOCUMENT.currency,
                receipt_date=_DOCUMENT.issue_date, version=1, created_at=self.now, updated_at=self.now,
                forwarded_manually=False,
            )
            session.add(receipt)
            session.flush()
            session.add(db.BudgetGmailMessage(
                owner="owner", gmail_id_hmac="1" * 64, internal_date=self.now, trust_path="direct_allowlist",
                outcome="imported", created_at=self.now, receipt_id=receipt.id,
            ))

    def settle(self):
        with self.sessions.begin() as session:
            gmail_card_import.settle(None, session, "owner", self.now)

    def assert_receipt_state(self, receipt_id, status, version, amount, currency, fingerprint):
        with self.sessions() as session:
            row = session.get(db.BudgetReceipt, receipt_id)
            self.assertEqual((row.status, row.version, row.total_amount, row.currency),
                             (status, version, amount, currency))
            self.assertEqual((row.linked_account_id, row.linked_entry_ref), self.transaction_key)
            self.assertEqual(row.linked_fingerprint, fingerprint)

    def test_conflicting_bank_fx_invalidates_automatic_link_and_survives_omission(self):
        self.observe(_purchase("10.00"))
        evidence = self.fx_evidence()
        self.assertEqual(evidence["instructed_amount"], {"amount": "10.00", "currency": "EUR"})
        self.observe(_purchase())
        self.assertEqual(self.fx_evidence(), evidence)

        self.stage_foreign_receipt()
        self.settle()
        fingerprint = financial_fingerprint(normalize.normalize_transaction(_purchase()))
        self.assert_receipt_state(_RECEIPT_ID, "confirmed", 2, "10", "EUR", fingerprint)

        # Only the two original amounts disagree. Posting details and the agreeing remittance remain intact.
        self.observe(_purchase("10.00"), _purchase("11.00"))
        with self.sessions() as session:
            transaction = session.get(db.BudgetTransaction, self.transaction_key)
            self.assertFalse(transaction.uncertain)
            self.assertEqual(financial_fingerprint(json.loads(transaction.payload_json)), fingerprint)
        self.assertIsNone(self.fx_evidence())
        self.assertIsNone(self.match(_DOCUMENT, receipt_id=_RECEIPT_ID))
        self.assert_receipt_state(_RECEIPT_ID, "needs_review", 3, "10", "EUR", fingerprint)

        self.observe(_purchase())
        self.assertIsNone(self.fx_evidence())
        self.assertIsNone(self.match(_DOCUMENT, receipt_id=_RECEIPT_ID))

        rate_only = _purchase()
        rate_only["exchange_rate"] = {"exchange_rate": "4.32", "unit_currency": "EUR"}
        self.observe(rate_only)
        self.assertIsNone(self.fx_evidence())
        self.assertIsNone(self.match(_DOCUMENT, receipt_id=_RECEIPT_ID))

        self.observe(_purchase("10.00"))
        self.assertEqual(self.fx_evidence(), evidence)
        self.assertEqual(self.match(_DOCUMENT, receipt_id=_RECEIPT_ID).entry_reference, _REFERENCE)
        self.settle()
        self.assert_receipt_state(_RECEIPT_ID, "needs_review", 3, "10", "EUR", fingerprint)

    def test_conflicting_bank_fx_preserves_manual_link_and_exact_pln_match(self):
        self.observe(_purchase("10.00"))
        receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"test-receipts")
        draft = receipts.parse_text("owner", receipt_text(total="43.20", day=_DAY))
        linked = receipts.confirm("owner", confirm_values(
            draft, link={"account_id": self.account_id, "entry_reference": _REFERENCE},
        ))["receipt"]
        self.assertTrue(linked["linked"])

        self.observe(_purchase("10.00"), _purchase("11.00"))
        self.assertIsNone(self.fx_evidence())
        fingerprint = financial_fingerprint(normalize.normalize_transaction(_purchase()))
        self.assert_receipt_state(linked["receipt_id"], "confirmed", linked["version"], "43.2", "PLN", fingerprint)
        same_currency = replace(_DOCUMENT, amount="43.2", currency="PLN")
        self.assertEqual(self.match(same_currency, receipt_id=linked["receipt_id"]).entry_reference, _REFERENCE)
