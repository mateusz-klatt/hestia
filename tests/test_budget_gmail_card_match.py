"""A direct document requires independent, unique evidence in the owner's card ledger."""

import json
from dataclasses import replace
from datetime import date

from hestia import db
from hestia.budget import analytics_views, bank_metadata
from hestia.budget.analytics_core import AnalyticsError, financial_fingerprint
from hestia.budget.gmail_card_match import match_card_purchase
from hestia.budget.gmail_document import ImportedDocument
from hestia.budget.receipt_reconcile import evidence_json, reconcile_receipt_links
from tests.test_budget_analytics_store import AnalyticsCase

DOCUMENT = ImportedDocument(
    "Arvid Books", None, "INV-77", "43.20", "PLN", "2026-11-30", None, "invoice", True, None
)


class CardPurchaseTests(AnalyticsCase):
    def match(self, document=DOCUMENT, **kwargs):
        with self.sessions() as session:
            return match_card_purchase(session, "owner", document, **kwargs)

    def ledger(self, reference="sale", *, account="a", lines=None, **kwargs):
        description = ["Płatność kartą", "Arvid Books"] if lines is None else lines
        return self.add_transaction(
            account,
            reference,
            amount=kwargs.pop("amount", "43.2"),
            remittance_information=description,
            **kwargs,
        )

    def set_kind(self, account, kind):
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts[account]).kind = kind

    def confirm(self, match, document=DOCUMENT, *, identifier="known", manual=False):
        evidence = dict(match.evidence)
        if manual:
            evidence.pop("gmail_card")
        with self.sessions.begin() as session:
            session.add(
                db.BudgetReceipt(
                    id=identifier,
                    owner="owner",
                    status="confirmed",
                    source_kind="attachment",
                    document_kind="invoice",
                    raw_shop_name=document.issuer,
                    total_amount=document.amount,
                    currency=document.currency,
                    payment_method="card",
                    version=1,
                    created_at=1,
                    updated_at=1,
                    receipt_date=document.issue_date,
                    linked_account_id=match.account_id,
                    linked_entry_ref=match.entry_reference,
                    linked_fingerprint=match.fingerprint,
                    linked_evidence=evidence_json(evidence),
                )
            )

    def test_match_is_owner_scoped_unique_and_reconcilable(self):
        self.ledger(account="foreign")
        self.assertIsNone(self.match())
        p = self.ledger()
        match = self.match()
        self.assertEqual(
            (match.account_id, match.entry_reference, match.fingerprint, match.receipt_date),
            (self.accounts["a"], "sale", financial_fingerprint(p), DOCUMENT.issue_date),
        )
        self.confirm(match)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 0)
        self.assertIsNone(self.match())
        self.assertEqual(self.match(include_receipt_id="known").entry_reference, "sale")
        self.ledger(reference="another", account="d")
        self.assertIsNone(self.match())
        self.assertIsNone(self.match(include_receipt_id="known"))
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetReceipt, "known"))
        self.assertIsNone(self.match())

    def test_cash_transfer_pending_credit_uncertain_disabled_and_gateway_do_not_qualify(self):
        self.ledger("transfer", lines=["przelew do Arvid Books"])
        self.ledger("pos", lines=["POS Arvid Books"])
        self.ledger("pending", status="PDNG")
        self.ledger("refunded", credit_debit_indicator="CRDT")
        self.ledger("uncertain")
        self.ledger("disabled", account="d")
        self.ledger("other-merchant", lines=["Płatność kartą Arvid Woda"])
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "uncertain")).uncertain = True
            session.get(db.BudgetAccount, self.accounts["d"]).enabled = False
        self.assertIsNone(self.match())
        self.assertIsNone(self.match(replace(DOCUMENT, issuer="PayU")))
        self.set_kind("a", "credit_card")
        self.assertEqual(
            self.match().entry_reference, "pos"
        )  # the explicitly labelled transfer is ineligible
        self.ledger("repayment", lines=["Spłata karty Arvid Books"])
        self.ledger("withdrawal", lines=["Cash withdrawal Arvid Books"])
        self.ledger("blik", lines=["Płatność BLIK Arvid Books"])
        self.assertEqual(self.match().entry_reference, "pos")

    def test_legal_billing_names_require_independent_first_party_card_descriptions(self):
        google = replace(DOCUMENT, issuer="Google Commerce Limited")
        apple = replace(DOCUMENT, issuer="Apple Distribution International Ltd.")
        for index, description in enumerate((
            "GOOGLE*Indie Developer", "GOOGLE *TEMPORARY HOLD", "Bookshop GOOGLE*Google Storage",
            "GOOGLE*Google Storage Reseller", "APPLE.COM/BILL.EXAMPLE", "Bookshop Apple.com/bill",
        )):
            self.ledger(f"unrelated-{index}", lines=["Card purchase", description])
        self.assertIsNone(self.match(google))
        self.assertIsNone(self.match(apple))
        self.ledger("google-sale", lines=[
            "Płatność kartą: GOOGLE*Google Storage g.co/helppay# IE", "Original amount: EUR 9.99",
        ])
        self.ledger("apple-sale", lines=["Card payment", "APPLE.COM/BILL CORK IE"])
        self.assertEqual(self.match(google).entry_reference, "google-sale")
        self.assertEqual(self.match(replace(google, amount="9.99", currency="EUR")).entry_reference, "google-sale")
        self.assertIsNone(self.match(replace(google, amount="10.00", currency="EUR")))
        self.assertEqual(self.match(replace(google, issuer="Google Ireland Ltd.")).entry_reference, "google-sale")
        self.assertEqual(self.match(apple).entry_reference, "apple-sale")
        self.assertIsNone(self.match(replace(google, issuer="Google Commerce Stationery Limited")))
        self.ledger("second", lines=["Card purchase", "GOOGLE*YouTube Premium"])
        self.assertIsNone(self.match(google))

    def test_foreign_amount_requires_unique_explicit_original(self):
        invoice = replace(DOCUMENT, currency="EUR", amount="10")
        self.ledger(lines=["Card purchase Arvid Books", "EUR rate 4.32"])
        self.assertIsNone(self.match(invoice))
        self.ledger("explicit", lines=["Card purchase Arvid Books", "Kwota oryginalna: 10,00 EUR"])
        self.assertEqual(self.match(invoice).entry_reference, "explicit")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (self.accounts["a"], "explicit"))
            payload = json.loads(row.payload_json)
            payload["remittance_information"].append("Original amount: EUR 9.50")
            row.payload_json = json.dumps(payload)
        self.assertIsNone(self.match(invoice))

    def test_budget_metadata_fx_roundtrip_and_stale_or_different_owner_records(self):
        invoice = replace(DOCUMENT, currency="EUR", amount="10")
        payload = self.ledger(lines=["Card purchase"])
        raw = {
            "creditor": {"name": DOCUMENT.issuer},
            "exchange_rate": {
                "instructed_amount": {"amount": "10", "currency": "EUR"},
                "exchange_rate": "4.32",
                "unit_currency": "EUR",
                "contract_identification": "unused",
            },
        }
        patch = bank_metadata.normalize_transaction_metadata(raw)
        self.assertEqual(
            patch["exchange_rate"],
            {
                "instructed_amount": {"amount": "10", "currency": "EUR"},
                "exchange_rate": "4.32",
                "unit_currency": "EUR",
            },
        )
        self.assertEqual(analytics_views._metadata(json.dumps(patch)), {"creditor_name": DOCUMENT.issuer})
        self.assertEqual(
            analytics_views._metadata(json.dumps({**patch, "exchange_rate": None})),
            {"creditor_name": DOCUMENT.issuer},
        )
        self.assertEqual(bank_metadata._payload_patch(patch), patch)
        for bad in ({"exchange_rate": []}, {"exchange_rate": {}}, {"exchange_rate": {"extra": "ignored"}}):
            self.assertIsNone(bank_metadata._payload_patch(bad))
        with self.assertRaises(AnalyticsError):
            analytics_views._metadata('{"exchange_rate":{"wrong":true}}')
        key = self.accounts["a"], "sale"
        fp = financial_fingerprint(payload)
        with self.sessions.begin() as session:
            session.add(
                db.BudgetTransactionMetadata(
                    account_id=key[0],
                    entry_reference=key[1],
                    owner="other",
                    ledger_fingerprint=fp,
                    payload_json=json.dumps(patch),
                    source="api",
                    observed_at=self.now,
                    version=1,
                )
            )
        self.assertIsNone(self.match(invoice))
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransactionMetadata, key)
            row.owner, row.ledger_fingerprint = "owner", "a" * 64
        self.assertIsNone(self.match(invoice))
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, key).ledger_fingerprint = fp
        self.assertEqual(self.match(invoice).entry_reference, "sale")
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, key).payload_json = "[]"
        self.assertIsNone(self.match(invoice))

    def test_document_window_and_labelled_nip(self):
        self.ledger(
            "other",
            booking_date="2026-12-16",
            value_date=None,
            lines=["Card payment another legal name", "NIP: 5250007738"],
        )
        self.assertIsNone(self.match(replace(DOCUMENT, nip="5250007738")))
        invoice = replace(DOCUMENT, nip="5250007738", due_date="2026-12-15")
        self.assertEqual(self.match(invoice).entry_reference, "other")
        self.assertEqual(
            self.match(replace(invoice, issue_date=None), received_date=date(2026, 11, 30)).receipt_date,
            DOCUMENT.issue_date,
        )
        for doc in (
            replace(invoice, amount="0"),
            replace(invoice, amount="-43.2"),
            replace(invoice, amount="inf"),
            replace(invoice, document_kind="credit_note"),
            replace(invoice, issue_date="bad"),
        ):
            self.assertIsNone(self.match(doc))

    def test_malformed_rows_and_unlabelled_fx_do_not_match(self):
        base = self.ledger("bad1")
        variants = (
            {"booking_date": "2026-11-31", "value_date": None},
            {"booking_date": "2026-12-29", "value_date": None, "transaction_date": "2026-11-31"},
            {"amount": "0"},
            {"amount": "zero"},
            {"remittance_information": [2]},
            {"remittance_information": ["Card purchase Arvid Books", "Original amount: 10.00"]},
        )
        with self.sessions.begin() as session:
            for i, override in enumerate(variants):
                session.add(
                    db.BudgetTransaction(
                        account_id=self.accounts["a"],
                        entry_reference=f"invalid-{i}",
                        payload_json=json.dumps(dict(base, **override)),
                        observed_at=self.now,
                        uncertain=False,
                    )
                )
            session.get(db.BudgetTransaction, (self.accounts["a"], "bad1")).payload_json = "invalid"
        self.assertIsNone(self.match(replace(DOCUMENT, currency="EUR", amount="10")))

    def test_changed_fx_metadata_marks_only_previously_automatic_link_for_review(self):
        invoice = replace(DOCUMENT, currency="EUR", amount="10")
        payload = self.ledger()
        key = self.accounts["a"], "sale"
        with self.sessions.begin() as session:
            session.add(
                db.BudgetTransactionMetadata(
                    account_id=key[0],
                    entry_reference=key[1],
                    owner="owner",
                    ledger_fingerprint=financial_fingerprint(payload),
                    source="api",
                    observed_at=self.now,
                    version=1,
                    payload_json=json.dumps(
                        {"exchange_rate": {"instructed_amount": {"amount": "10.00", "currency": "EUR"}}}
                    ),
                )
            )
        match = self.match(invoice)
        self.confirm(match, invoice)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 0)
            session.get(db.BudgetTransactionMetadata, key).payload_json = json.dumps(
                {"exchange_rate": {"instructed_amount": {"amount": "19.00", "currency": "EUR"}}}
            )
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 1), 1)
            receipt = session.get(db.BudgetReceipt, "known")
            self.assertEqual(
                (receipt.status, receipt.version, receipt.total_amount), ("needs_review", 2, "10")
            )
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 2), 0)
            # The same normal user-chosen link is independent of automatic purchase matching.
            receipt.status = "confirmed"
            manual = dict(match.evidence)
            del manual["gmail_card"]
            receipt.linked_evidence = evidence_json(manual)
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 3), 0)

    def test_second_qualifying_charge_on_later_bank_import_marks_automatic_link_for_review(self):
        self.ledger()
        self.confirm(self.match())
        self.ledger(reference="later", account="d")
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 1)
            row = session.get(db.BudgetReceipt, "known")
            self.assertEqual((row.status, row.linked_entry_ref), ("needs_review", "sale"))

    def test_matching_transaction_changing_identity_does_not_relink_automatically(self):
        self.ledger()
        self.set_kind("a", "credit_card")
        self.confirm(self.match())
        self.ledger(reference="replacement", account="d")
        with self.sessions.begin() as session:
            # Incorrectly attributed card purchase now moves to a unique, different account.
            row = session.get(db.BudgetTransaction, (self.accounts["a"], "sale"))
            row.uncertain = True
            from hestia.budget.gmail_card_match import still_matches_card

            self.assertFalse(still_matches_card(session, session.get(db.BudgetReceipt, "known")))
