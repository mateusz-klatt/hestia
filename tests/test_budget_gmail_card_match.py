"""A direct document requires independent, unique evidence in the owner's card ledger."""

import json
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4

from hestia import db
from hestia.budget import analytics_views, bank_metadata
from hestia.budget.analytics_core import AnalyticsError, financial_fingerprint
from hestia.budget.gmail_card_match import card_purchase_candidates, match_card_purchase
from hestia.budget.gmail_config import CARD_ACCOUNTS_ENV
from hestia.budget.gmail_document import ImportedDocument
from hestia.budget.receipt_reconcile import evidence_json, reconcile_receipt_links
from hestia.budget.receipt_store import BudgetReceiptStore
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

    def group_purchase(self):
        self.ledger("group-charge")
        anchor = str(uuid4())
        members = {anchor: "20", str(uuid4()): "23.2"}
        with self.sessions.begin() as session:
            for receipt_id, amount in members.items():
                session.add(db.BudgetReceipt(
                    id=receipt_id, owner="owner", status="confirmed", source_kind="attachment",
                    document_kind="invoice", receipt_kind="purchase", raw_shop_name=DOCUMENT.issuer,
                    receipt_date=DOCUMENT.issue_date, total_amount=amount, currency=DOCUMENT.currency,
                    payment_method="card", version=1, created_at=self.now, updated_at=self.now,
                ))
        store = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"card-group-test")
        proposal, = store.group_candidates("owner", {"receipt_id": anchor})["proposals"]
        linked = store.group_link("owner", {
            "link": {key: proposal[key] for key in ("account_id", "entry_reference", "candidate_token")},
            "members": [{"receipt_id": receipt_id, "expected_version": 1} for receipt_id in members],
        })
        return linked["group"]["group_id"], anchor

    def test_group_reservation_blocks_card_matching_and_preserves_ambiguity(self):
        group_id, _ = self.group_purchase()
        self.assertIsNone(self.match())
        self.ledger("another-charge", account="d")
        self.assertIsNone(self.match())
        with self.sessions() as session:
            candidates = card_purchase_candidates(session, "owner", DOCUMENT)
        purchases = {candidate.transaction_key[1]: candidate.available_purchase for candidate in candidates}
        self.assertIsNone(purchases["group-charge"])
        self.assertEqual(purchases["another-charge"].entry_reference, "another-charge")

        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptLinkGroup, group_id).status = "needs_review"
        self.assertIsNone(self.match())

    def test_group_member_cannot_match_a_separate_card_debit(self):
        _, anchor = self.group_purchase()
        self.ledger("separate-charge", amount="20")
        member_document = replace(DOCUMENT, amount="20")
        self.assertEqual(self.match(member_document).entry_reference, "separate-charge")
        self.assertIsNone(self.match(member_document, include_receipt_id=anchor))

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
        with self.sessions.begin() as session:
            for index in range(6):
                session.delete(session.get(db.BudgetTransaction, (self.accounts["a"], f"unrelated-{index}")))
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

    def test_google_legal_name_is_insufficient_and_different_products_are_ambiguous(self):
        google = replace(DOCUMENT, issuer="Google Ireland Limited", nip="5250007738")
        self.ledger("legal-name", lines=["Card purchase", "Google Ireland Limited", "NIP: 5250007738"])
        self.assertIsNone(self.match(google))
        self.ledger("workspace", account="d", lines=["Card purchase", "GOOGLE *WORKSPACE"])
        self.assertIsNone(self.match(google))
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetTransaction, (self.accounts["a"], "legal-name")))
        self.assertEqual(self.match(google).entry_reference, "workspace")
        self.ledger("play", lines=["Card purchase", "GOOGLE *PLAY"], amount="9.99")
        self.assertIsNone(self.match(google))

    def test_google_issuer_variants_do_not_match_an_unrelated_youtube_debit(self):
        self.ledger("youtube", lines=["Card purchase", "GOOGLE*YouTube"], amount="9.99")
        for issuer in ("Google Poland", "Google Poland Sp. z o.o.", "Google Pay", "Google One"):
            with self.subTest(issuer=issuer):
                self.assertIsNone(self.match(replace(DOCUMENT, issuer=issuer, amount="9.99")))

    def test_google_one_requires_the_same_billing_product_and_no_other_google_debit(self):
        document = replace(DOCUMENT, issuer="Google One", amount="9.99")
        self.ledger("one", lines=["Card purchase", "GOOGLE*Google One"], amount="9.99")
        self.assertEqual(self.match(document).entry_reference, "one")
        self.ledger("youtube", lines=["Card purchase", "GOOGLE*YouTube"], amount="14.99")
        self.assertIsNone(self.match(document))
        with self.sessions() as session:
            candidates = card_purchase_candidates(session, "owner", document)
        purchases = {candidate.transaction_key[1]: candidate.available_purchase for candidate in candidates}
        self.assertEqual(purchases["one"].entry_reference, "one")
        self.assertIsNone(purchases["youtube"])

    def test_card_suffix_limits_eligible_accounts_and_overrides_no_bank_suffix(self):
        document = replace(DOCUMENT, card_last4="0123")
        self.ledger("unknown-suffix", lines=["Card purchase Arvid Books"])
        self.ledger("wrong-suffix", lines=["Card purchase Arvid Books", "Visa ending in 4567"])
        self.ledger("other-account", account="d", lines=["Card purchase Arvid Books", "Visa ending in 0123"])
        with patch.dict("os.environ", {CARD_ACCOUNTS_ENV: ""}):
            self.assertEqual(self.match(document).entry_reference, "other-account")
            self.assertIsNone(self.match(replace(document, card_last4="9999")))
        with patch.dict("os.environ", {CARD_ACCOUNTS_ENV: json.dumps({"0123": [self.accounts["a"]]})}):
            self.assertEqual(self.match(document).entry_reference, "unknown-suffix")
            with self.sessions.begin() as session:
                session.delete(session.get(db.BudgetTransaction, (self.accounts["a"], "unknown-suffix")))
            self.assertIsNone(self.match(document))
        with patch.dict("os.environ", {CARD_ACCOUNTS_ENV: "invalid"}):
            self.assertIsNone(self.match(document))

    def test_blik_document_requires_a_blik_purchase_without_card_evidence(self):
        self.ledger("card")
        document = replace(DOCUMENT, payment_method="blik")
        self.assertIsNone(self.match(document))
        self.assertIsNone(self.match(replace(document, payment_method="mixed")))
        self.ledger("card-and-blik", lines=["Card purchase", "Płatność BLIK Arvid Books"])
        self.ledger("masked-card-and-blik", lines=["Płatność BLIK Arvid Books", "Visa ending in 0123"])
        self.set_kind("d", "credit_card")
        self.ledger("credit-account", account="d", lines=["Płatność BLIK Arvid Books"])
        self.assertIsNone(self.match(document))
        self.ledger("blik", lines=["Płatność BLIK Arvid Books"])
        self.assertEqual(self.match(document).entry_reference, "blik")

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

    def test_due_date_extends_card_match_window_by_at_most_45_days(self):
        issue_date = date.fromisoformat(DOCUMENT.issue_date)
        for days, amount, expected in ((40, "10", "day-40"), (45, "11", "day-45"),
                                        (46, "12", None), (90, "13", None)):
            with self.subTest(days=days):
                due_date = issue_date + timedelta(days=days)
                self.ledger(f"day-{days}", amount=amount, value_date=None,
                            booking_date=(due_date + timedelta(days=4)).isoformat())
                match = self.match(replace(DOCUMENT, due_date=due_date.isoformat(), amount=amount))
                self.assertEqual(None if match is None else match.entry_reference, expected)

        self.ledger("near-issue", booking_date=issue_date.isoformat())
        document = replace(DOCUMENT, due_date=(issue_date + timedelta(days=90)).isoformat())
        self.assertEqual(self.match(document).entry_reference, "near-issue")

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

    def test_candidate_enumeration_exposes_occupied_and_fx_filtered_candidates(self):
        first = self.ledger("t1")
        second = self.ledger("t2", account="d")
        with self.sessions() as session:
            candidates = card_purchase_candidates(session, "owner", DOCUMENT)
        self.assertEqual(
            sorted((item.transaction_key[1], item.available_purchase is None) for item in candidates),
            [("t1", False), ("t2", False)],
        )
        self.assertIsNone(self.match())

        self.confirm(next(item.available_purchase for item in candidates
                          if item.transaction_key == (self.accounts["a"], "t1")))
        with self.sessions() as session:
            candidates_after = card_purchase_candidates(session, "owner", DOCUMENT)
            included = card_purchase_candidates(session, "owner", DOCUMENT, include_receipt_id="known")
        self.assertEqual(
            sorted((item.transaction_key[1], item.available_purchase is None) for item in candidates_after),
            [("t1", True), ("t2", False)],
        )
        self.assertEqual(
            sorted((item.transaction_key[1], item.available_purchase is None) for item in included),
            [("t1", False), ("t2", False)],
        )
        self.assertIsNone(self.match())

        fx_doc = replace(DOCUMENT, currency="EUR", amount="10")
        with self.sessions.begin() as session:
            session.add(db.BudgetTransactionMetadata(
                account_id=self.accounts["a"],
                entry_reference="t1",
                owner="owner",
                ledger_fingerprint=financial_fingerprint(first),
                source="api",
                observed_at=self.now,
                version=1,
                payload_json=json.dumps({"exchange_rate": None}),
            ))
            session.add(db.BudgetTransactionMetadata(
                account_id=self.accounts["d"],
                entry_reference="t2",
                owner="owner",
                ledger_fingerprint=financial_fingerprint(second),
                source="api",
                observed_at=self.now,
                version=1,
                payload_json=json.dumps(
                    {"exchange_rate": {"instructed_amount": {"amount": "10.00", "currency": "EUR"}}}
                ),
            ))
        with self.sessions() as session:
            fx_candidates = card_purchase_candidates(session, "owner", fx_doc)
        candidate, = fx_candidates
        self.assertEqual(candidate.transaction_key, (self.accounts["d"], "t2"))
        self.assertEqual(candidate.available_purchase.entry_reference, "t2")
        self.assertEqual(self.match(fx_doc).entry_reference, "t2")
