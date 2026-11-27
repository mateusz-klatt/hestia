"""Cross-currency receipt candidates, explicit acceptance and public quote prefetch."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
import unittest
from unittest import mock
import uuid

from hestia import api_contract, db
from hestia.budget import receipt_api, receipt_fx, receipt_fx_rates as rates
from hestia.budget.analytics_core import financial_fingerprint
from hestia.budget.bank_metadata import MetadataCollector, apply_bank_metadata
from hestia.budget.gmail_card_import import _matching_variants
from hestia.budget.gmail_card_match import _card_amount_matches
from hestia.budget.gmail_document import ImportedDocument
from hestia.budget.gmail_store import GmailStore, PendingMessage, retain_imports
from hestia.budget.normalize import normalize_transaction
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_reconcile import reconcile_receipt_links
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_api as http_fixture
from tests import test_budget_receipts as fixture


_DAY = "2026-11-21"
_PUBLISHED = "2026-11-20"
_TABLE = "226/A/NBP/2026"
_MERCHANT = "Example Hosting"
_NOW = datetime(2026, 11, 23, tzinfo=timezone.utc).timestamp()


def _quote(mid="4.0000", day=_PUBLISHED):
    return {"effectiveDate": day, "no": _TABLE, "mid": mid}


def _cache(session, currency="EUR", day=_DAY, quote=None):
    key = rates._key(currency, date.fromisoformat(day))
    session.merge(db.AppMeta(key=key, value=json.dumps({"fetched_at": _NOW, "quote": quote or _quote()})))


def _receipt(session, *, owner="owner", currency="EUR", amount="100", kind="purchase", day=_DAY,
             merchant=_MERCHANT, payment="card", card_last4=None, fee=None):
    receipt_id = str(uuid.uuid4())
    session.add(db.BudgetReceipt(
        id=receipt_id, owner=owner, submitted_by=owner, raw_shop_name=merchant, total_amount=amount,
        currency=currency, receipt_date=day, receipt_kind=kind, payment_method=payment, card_last4=card_last4,
        status="imported", source_kind="email", document_kind="receipt", fee_amount=fee,
        version=1, created_at=_NOW, updated_at=_NOW,
    ))
    session.flush()
    session.add(db.BudgetReceiptLine(receipt_id=receipt_id, owner=owner, line_no=1, line_kind="other",
                                     raw_name="Subscription", raw_name_key="subscription", quantity="1",
                                     gross_line_total=amount, net_line_total=amount))
    return receipt_id


def _values(detail, candidate):
    receipt = detail["receipt"]
    values = {name: receipt[name] for name in ("receipt_id", "receipt_kind", "receipt_date", "receipt_time",
                                              "total_amount", "currency", "payment_method")}
    if values["payment_method"] is None:
        values["payment_method"] = "unknown"
    return {**values, "expected_version": receipt["version"], "link": {
        field: candidate[field] for field in ("account_id", "entry_reference", "candidate_token")
    }}


class FXReceiptTests(fixture.AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.now = _NOW
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"fx-receipts",
                                           card_accounts={"1234": [self.accounts["b"]]})
        transport = mock.patch.object(rates, "HTTPSConnection", side_effect=AssertionError("unexpected network"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)

    def receipt(self, **fields):
        with self.sessions.begin() as session:
            return _receipt(session, **fields)

    def bank(self, reference="fx", *, account="a", amount="408", currency="PLN", direction="DBIT",
             original=None, unit=None, rate=None, day=_DAY, remittance=None, status="BOOK"):
        raw = {
            "entry_reference": reference, "transaction_amount": {"amount": amount, "currency": currency},
            "credit_debit_indicator": direction, "status": status, "booking_date": day,
            "remittance_information": [_MERCHANT] if remittance is None else remittance,
        }
        exchange = {}
        if original:
            exchange["instructed_amount"] = {"currency": original[0], "amount": original[1]}
        if rate is not None:
            exchange.update(unit_currency=unit, exchange_rate=rate, contract_identification="discarded")
        if exchange:
            raw["exchange_rate"] = exchange
        tx = normalize_transaction(raw)
        collector = MetadataCollector()
        collector.add(raw, tx)
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(account_id=self.accounts[account], entry_reference=reference,
                                             payload_json=json.dumps(tx), observed_at=self.now, uncertain=False))
            session.flush()
            apply_bank_metadata(session, self.accounts[account], collector.finish(), source="api", observed_at=self.now)
        self.assertNotIn("exchange_rate", tx)
        return financial_fingerprint(tx)

    def detail(self, receipt_id, *, include_fx=True):
        return self.receipts.detail("owner", receipt_id, include_fx=include_fx)

    def test_normalized_original_is_strong_and_explicit_link_preserves_posted_money(self):
        fingerprint = self.bank(original=("EUR", "100.00"), unit="EUR", rate="4.0800",
                                remittance=[_MERCHANT, "Original amount: 100 EUR", "FX fee: 2.00 PLN"])
        receipt_id = self.receipt(payment=None)
        detail = self.detail(receipt_id)
        exact, = detail["candidates"]
        self.assertEqual(exact["match_strength"], "id")
        fx = exact["fx"]
        self.assertEqual(fx["source"], "bank_original")
        self.assertEqual(fx["implied_rate"], "4.08")
        self.assertEqual(fx["stated_rate"], {"rate": "4.0800", "source": "bank_original",
                                            "unit_currency": "EUR", "counter_currency": "PLN"})
        self.assertEqual(fx["explicit_fee"], {"currency": "PLN", "amount": "2.00"})
        self.assertIsNone(fx["quote"])
        self.assertFalse(detail["receipt"]["linked"])
        self.assertEqual(self.detail(receipt_id, include_fx=False)["candidates"], [])
        result = self.receipts.confirm("owner", _values(detail, exact), include_fx=True)["receipt"]
        self.assertEqual((result["total_amount"], result["currency"], result["status"]), ("100", "EUR", "confirmed"))
        self.assertEqual(result["fx_link"], fx)
        self.assertEqual(self.detail(receipt_id)["receipt"]["fx_link"], fx)
        self.assertNotIn("fx_link", self.detail(receipt_id, include_fx=False)["receipt"])
        self.assertEqual(self.fingerprint(reference="fx"), fingerprint)
        self.assertEqual(json.loads(self.transaction(reference="fx").payload_json)["amount"], "408")
        self.transport.assert_not_called()

    def test_market_suggestion_is_medium_and_requires_explicit_link(self):
        self.bank()
        self.bank("out-of-tolerance", amount="412.01")
        self.bank("purchase-not-refund", amount="408", direction="CRDT")
        self.bank("pending", status="PDNG")
        self.bank("other-owner", account="foreign", original=("EUR", "100"))
        receipt_id = self.receipt()
        self.assertEqual(self.detail(receipt_id)["candidates"], [])
        with self.sessions.begin() as session:
            _cache(session)
        detail = self.detail(receipt_id)
        match, = detail["candidates"]
        self.assertEqual(match["match_strength"], "medium")
        self.assertEqual(match["fx"]["quote"], {"rate": "4.0000", "unit_currency": "EUR", "publication_date": _PUBLISHED,
                                                "table_number": _TABLE, "estimated_amount": "400", "difference": "8"})
        self.assertIsNone(match["fx"]["explicit_fee"])
        self.assertEqual(detail["receipt"]["status"], "imported")
        self.assertFalse(detail["receipt"]["linked"])
        result = self.receipts.confirm("owner", _values(detail, match), include_fx=True)["receipt"]
        self.assertTrue(result["linked"])
        self.assertEqual(result["fx_link"], match["fx"])
        self.transport.assert_not_called()

    def test_market_estimates_normalize_published_quote_units(self):
        cases = (
            ("CLP", "0.40", "10000", "40", "4000", "0.004"),
            ("HUF", "1.15", "10000", "115", "11500", "0.0115"),
            ("IDR", "2.25", "1000000", "225", "2250000", "0.000225"),
            ("INR", "4.40", "10000", "440", "44000", "0.044"),
            ("ISK", "3.25", "10000", "325", "32500", "0.0325"),
            ("JPY", "2.50", "10000", "250", "25000", "0.025"),
            ("KRW", "0.30", "10000", "30", "3000", "0.003"),
            ("EUR", "4.0000", "100", "400", "4", "4.0000"),
        )
        for index, (currency, mid, total, correct, wrong, per_unit) in enumerate(cases):
            with self.subTest(currency=currency):
                day = f"2026-11-{10 + index:02d}"
                self.bank(f"{currency}-correct", amount=correct, day=day)
                self.bank(f"{currency}-wrong", amount=wrong, day=day)
                receipt = self.receipt(currency=currency, amount=total, day=day)
                with self.sessions.begin() as session:
                    _cache(session, currency=currency, day=day, quote=_quote(mid, day))
                match, = self.detail(receipt)["candidates"]
                self.assertEqual((match["amount"], match["match_strength"]), (correct, "medium"))
                fx = match["fx"]
                self.assertEqual(Decimal(fx["implied_rate"]), Decimal(per_unit))
                self.assertEqual(fx["quote"], {
                    "rate": per_unit, "unit_currency": currency, "publication_date": day,
                    "table_number": _TABLE, "estimated_amount": correct, "difference": "0",
                })
        self.transport.assert_not_called()

    def test_inverse_market_normalizes_per_hundred_quote(self):
        self.bank("correct", currency="HUF", amount="10000")
        self.bank("wrong", currency="HUF", amount="100")
        receipt = self.receipt(currency="PLN", amount="115")
        with self.sessions.begin() as session:
            _cache(session, currency="HUF", quote=_quote("1.15"))
        match, = self.detail(receipt)["candidates"]
        self.assertEqual((match["amount"], match["match_strength"]), ("10000", "medium"))
        self.assertEqual(match["fx"]["implied_rate"], "86.95652174")
        self.assertEqual(match["fx"]["quote"], {
            "rate": "0.0115", "unit_currency": "HUF", "publication_date": _PUBLISHED,
            "table_number": _TABLE, "estimated_amount": "10000", "difference": "0",
        })

    def test_inverse_market_for_refund_and_known_account(self):
        self.bank("wrong-account", account="a", amount="100", currency="EUR", direction="CRDT")
        self.bank("refund", account="b", amount="98", currency="EUR", direction="CRDT")
        self.bank("wrong-direction", account="b", amount="100", currency="EUR")
        receipt_id = self.receipt(currency="PLN", amount="400", kind="refund", card_last4="1234")
        with self.sessions.begin() as session:
            _cache(session)
        match, = self.detail(receipt_id)["candidates"]
        self.assertEqual(match["account_id"], self.accounts["b"])
        self.assertEqual(match["credit_debit_indicator"], "CRDT")
        self.assertEqual(match["fx"]["implied_rate"], "0.245")
        self.assertEqual(match["fx"]["quote"]["estimated_amount"], "100")
        self.assertEqual(match["fx"]["quote"]["difference"], "-2")

    def test_repeated_bookings_and_competing_fx_receipts_are_ambiguous(self):
        self.bank(original=("EUR", "100"))
        self.bank("duplicate", original=("EUR", "100"), amount="407")
        receipt_id = self.receipt()
        self.assertEqual([item["match_strength"] for item in self.detail(receipt_id)["candidates"]],
                         ["ambiguous", "ambiguous"])
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["a"], "duplicate")).uncertain = True
            _receipt(session)
        match, = self.detail(receipt_id)["candidates"]
        self.assertEqual(match["match_strength"], "ambiguous")

    def test_multiple_currencies_compete_but_do_not_form_a_group(self):
        self.bank(amount="400")
        receipt_id = self.receipt()
        with self.sessions.begin() as session:
            _cache(session)
            _cache(session, currency="USD", quote=_quote("3.8"))
            second = _receipt(session, currency="USD", amount="105")
        for identifier in (receipt_id, second):
            detail = self.detail(identifier)
            match, = detail["candidates"]
            self.assertEqual(match["match_strength"], "ambiguous")
            self.assertFalse(detail.get("group_candidates"))

    def test_explicit_conflict_never_falls_through_to_the_market_rate(self):
        self.bank(original=("EUR", "100"), remittance=[_MERCHANT, "Original amount: 99 EUR"])
        identifier = self.receipt()
        with self.sessions.begin() as session:
            _cache(session)
        detail = self.detail(identifier)
        match, = detail["candidates"]
        self.assertEqual(match["match_strength"], "ambiguous")
        self.assertTrue(match["fx"]["conflicting"])
        self.assertIsNone(match["fx"]["quote"])
        with self.assertRaisesRegex(ReceiptError, "budget_receipt_stale_source"):
            self.receipts.confirm("owner", _values(detail, match), include_fx=True)

    def test_remittance_and_explicit_charged_amount_can_match(self):
        self.bank(remittance=[_MERCHANT, "Kwota w walucie oryginalnej: EUR 100,00"])
        identifier = self.receipt()
        candidate, = self.detail(identifier)["candidates"]
        self.assertEqual((candidate["match_strength"], candidate["fx"]["source"]), ("id", "bank_remittance"))
        self.change_transaction(reference="fx", remittance_information=[_MERCHANT, "Comment: 100 EUR"])
        with self.sessions.begin() as session:
            receipt_fx.write_receipt_conversion(session, "owner", identifier, receipt_fx.receipt_conversion_fields(
                "Amount charged: PLN 408.00\n1 EUR = 4.0800 PLN\nConversion fee: 1.25 PLN",
            ))
        candidate, = self.detail(identifier)["candidates"]
        self.assertEqual(candidate["fx"]["source"], "receipt_charged")
        self.assertEqual(candidate["fx"]["stated_rate"]["rate"], "4.0800")
        self.assertEqual(candidate["fx"]["explicit_fee"], {"amount": "1.25", "currency": "PLN"})

    def test_token_revalidation_and_bank_sidecar_drift_latch_review(self):
        self.bank(original=("EUR", "100"))
        identifier = self.receipt()
        stale = self.detail(identifier)
        transaction_key = (self.accounts["a"], "fx")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransactionMetadata, transaction_key)
            row.payload_json = json.dumps({"exchange_rate": {"instructed_amount": {"amount": "101", "currency": "EUR"}}})
        with self.assertRaisesRegex(ReceiptError, "budget_receipt_stale_source"):
            self.receipts.confirm("owner", _values(stale, stale["candidates"][0]), include_fx=True)
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransactionMetadata, transaction_key)
            row.payload_json = json.dumps({"exchange_rate": {"instructed_amount": {"amount": "100", "currency": "EUR"}}})
        current = self.detail(identifier)
        self.receipts.confirm("owner", _values(current, current["candidates"][0]), include_fx=True)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 1), 0)
            row = session.get(db.BudgetTransactionMetadata, transaction_key)
            row.payload_json = json.dumps({"exchange_rate": None})
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 2), 1)
        self.assertEqual(self.detail(identifier)["receipt"]["status"], "needs_review")

    def test_market_link_survives_cache_eviction_but_latches_on_changed_evidence(self):
        self.bank()
        identifier = self.receipt()
        with self.sessions.begin() as session:
            _cache(session)
        detail = self.detail(identifier)
        self.receipts.confirm("owner", _values(detail, detail["candidates"][0]), include_fx=True)
        with self.sessions.begin() as session:
            session.delete(session.get(db.AppMeta, rates._key("EUR", date.fromisoformat(_DAY))))
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 1), 0)
            _cache(session, quote=_quote("4.01"))
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 2), 1)

    def test_scaled_market_link_survives_cache_eviction_and_refetch(self):
        self.bank(amount="115")
        receipt = self.receipt(currency="HUF", amount="10000")
        with mock.patch.object(rates, "fetch_nbp_quote", return_value=_quote("1.15")) as fetch:
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW), 1)
        fetch.assert_called_once_with("HUF", date.fromisoformat(_DAY))
        detail = self.detail(receipt)
        match, = detail["candidates"]
        self.assertEqual(match["fx"]["quote"]["rate"], "0.0115")
        self.assertEqual(self.receipts.confirm("owner", _values(detail, match), include_fx=True)["receipt"]["fx_link"],
                         match["fx"])
        with self.sessions.begin() as session:
            cache_key = rates._key("HUF", date.fromisoformat(_DAY))
            cached = session.get(db.AppMeta, cache_key)
            self.assertEqual(json.loads(cached.value)["quote"]["mid"], "1.15")
            session.delete(cached)
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 1), 0)
        with mock.patch.object(rates, "fetch_nbp_quote", return_value=_quote("1.15")):
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW + 2), 1)
        self.assertEqual(self.detail(receipt)["receipt"]["status"], "confirmed")
        self.transport.assert_not_called()

    def test_legacy_scaled_market_link_shows_corrected_estimate_and_requires_review(self):
        self.bank(amount="11500")
        receipt = self.receipt(currency="HUF", amount="10000")
        with self.sessions.begin() as session:
            _cache(session, currency="HUF", quote=_quote("1.15"))
        with mock.patch.object(receipt_fx, "NBP_QUOTE_UNITS", {}):
            detail = self.detail(receipt)
            match, = detail["candidates"]
            self.receipts.confirm("owner", _values(detail, match), include_fx=True)
        with self.sessions.begin() as session:
            session.delete(session.get(db.AppMeta, rates._key("HUF", date.fromisoformat(_DAY))))
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 1), 1)
        detail = self.detail(receipt)
        self.assertEqual(detail["candidates"], [])
        self.assertEqual(detail["receipt"]["status"], "needs_review")
        self.assertEqual(detail["receipt"]["fx_link"]["quote"], {
            "rate": "0.0115", "unit_currency": "HUF", "publication_date": _PUBLISHED,
            "table_number": _TABLE, "estimated_amount": "115", "difference": "11385",
        })
        self.assertEqual(detail["receipt"]["fx_link"]["booked_amount"], "11500")

    def test_prefetch_revalidates_accepted_links_when_quote_is_revised(self):
        self.bank()
        identifier = self.receipt()
        with self.sessions.begin() as session:
            _cache(session)
        detail = self.detail(identifier)
        self.receipts.confirm("owner", _values(detail, detail["candidates"][0]), include_fx=True)
        with mock.patch.object(rates, "fetch_nbp_quote", side_effect=[None, _quote("4.01")]) as fetch:
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW + rates.RETRY_SECONDS), 1)
            self.assertEqual(self.detail(identifier)["receipt"]["status"], "confirmed")
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW + 2 * rates.RETRY_SECONDS), 1)
            self.assertEqual(fetch.call_count, 2)
        refreshed = self.detail(identifier)
        self.assertEqual(refreshed["receipt"]["status"], "needs_review")
        self.assertEqual(refreshed["receipt"]["fx_link"]["quote"]["rate"], "4.0000")
        self.assertEqual(refreshed["candidates"][0]["fx"]["quote"]["rate"], "4.01")

    def test_paste_keeps_only_explicit_conversion_fields_and_can_confirm_the_preview(self):
        self.bank(amount="25", currency="EUR")
        text = fixture.receipt_text(day=_DAY)
        text += "\nCharged amount: EUR 25.00\nFX fee: EUR 0.25"
        draft = self.receipts.parse_text("owner", text, include_fx=True)
        match, = draft["candidates"]
        self.assertEqual(match["fx"]["source"], "receipt_charged")
        self.assertEqual(match["fx"]["explicit_fee"], {"currency": "EUR", "amount": "0.25"})
        link = {key: match[key] for key in ("account_id", "entry_reference", "candidate_token")}
        receipt = self.receipts.confirm("owner", fixture.confirm_values(draft, link=link), include_fx=True)["receipt"]
        self.assertEqual((receipt["fx_link"]["receipt_currency"], receipt["total_amount"]), ("PLN", "100"))
        meta_key = receipt_fx.receipt_conversion_key(receipt["receipt_id"])
        with self.sessions() as session:
            self.assertNotIn(fixture.PAN, session.get(db.AppMeta, meta_key).value)
            self.assertEqual(receipt_fx.read_receipt_conversion(session, "other", receipt["receipt_id"]), {})
        self.receipts.delete("owner", {"receipt_id": receipt["receipt_id"], "expected_version": receipt["version"]})
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, meta_key))

    def test_gmail_structured_conversion_survives_import_and_is_purged_with_expired_document(self):
        self.bank()
        text = "Amount charged: 408.00 PLN\n1 PLN = 0.245098 EUR\n1 EUR = 4.0800 PLN"
        fields = receipt_fx.receipt_conversion_fields(text)
        invoice = ImportedDocument(_MERCHANT, None, "FX-TEST", "100", "EUR", _DAY, None, "receipt",
                                   True, None, fx_fields=fields)
        item = PendingMessage("synthetic", self.now, "direct", "imported", invoice=invoice, source_kind="attachment")
        with self.sessions.begin() as session:
            receipt_id = GmailStore._invoice(session, "owner", item, self.now)
        candidate, = self.detail(receipt_id)["candidates"]
        self.assertEqual(candidate["fx"]["source"], "receipt_charged")
        self.assertFalse(candidate["fx"]["conflicting"])
        with self.sessions.begin() as session:
            receipt = session.get(db.BudgetReceipt, receipt_id)
            retain_imports(session, "owner", receipt.import_expires_at + 1)
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, receipt_fx.receipt_conversion_key(receipt_id)))

    def test_conflicting_copy_of_an_accepted_gmail_receipt_surfaces_for_review(self):
        self.bank(original=("EUR", "100"))
        invoice = ImportedDocument(_MERCHANT, None, "FX-COPY", "100", "EUR", _DAY, None, "receipt",
                                   True, None, fx_fields=receipt_fx.receipt_conversion_fields("Charged amount: 408 PLN"))
        item = PendingMessage(
            "synthetic-copy", self.now, "direct", "imported", invoice=invoice, source_kind="attachment",
            document_provenance={"number_hmac": "synthetic-number", "seller_key": "example",
                                 "paid": True, "receipt_number_hmac": None},
        )
        gmail = GmailStore(self.sessions, clock=lambda: self.now)
        with self.sessions.begin() as session:
            identifier = gmail._invoice(session, "owner", item, self.now)
            self.assertEqual(gmail._duplicate_document(session, "owner", item, self.now), "skipped_duplicate")
            fee_invoice = replace(invoice, fx_fields=receipt_fx.receipt_conversion_fields("FX fee: 1.25 PLN"))
            self.assertEqual(gmail._duplicate_document(session, "owner", replace(item, invoice=fee_invoice), self.now),
                             "skipped_duplicate")
        detail = self.detail(identifier)
        self.assertEqual(detail["candidates"][0]["fx"]["explicit_fee"], {"amount": "1.25", "currency": "PLN"})
        self.receipts.confirm("owner", _values(detail, detail["candidates"][0]), include_fx=True)
        conflicting = replace(invoice, fx_fields=receipt_fx.receipt_conversion_fields("Charged amount: 407 PLN"))
        with self.sessions.begin() as session:
            self.assertEqual(gmail._duplicate_document(session, "owner", replace(item, invoice=conflicting), self.now),
                             "skipped_duplicate")
        detail = self.detail(identifier)
        self.assertEqual(detail["receipt"]["status"], "needs_review")
        self.assertTrue(detail["candidates"][0]["fx"]["conflicting"])

    def test_gmail_auto_match_preserves_exact_bank_rule_and_rejects_conflicting_copies(self):
        data = {"received": _NOW}
        base = dict(issuer=_MERCHANT, nip=None, invoice_number="EXAMPLE", amount="100", currency="EUR",
                    issue_date=_DAY, due_date=None, document_kind="receipt", paid=True, receipt_number=None)
        first = ImportedDocument(**base, fx_fields=receipt_fx.receipt_conversion_fields("Amount charged: 408 PLN"))
        second = ImportedDocument(**base, fx_fields=receipt_fx.receipt_conversion_fields("Amount charged: 406 PLN"))
        (_, merged), = _matching_variants([(None, data, first), (None, data, second)]).values()
        bank = {"currency": "PLN", "amount": "408"}
        metadata = {"exchange_rate": {"instructed_amount": {"currency": "EUR", "amount": "100"}}}
        self.assertTrue(_card_amount_matches(bank, "EUR", "100", (), metadata, first.fx_fields))
        self.assertFalse(_card_amount_matches(bank, "EUR", "100", (), metadata, merged.fx_fields))
        self.assertFalse(_card_amount_matches(bank, "EUR", "100", (), {}, first.fx_fields))

    def test_explicit_stated_rates_and_labelled_amounts_disagree(self):
        self.bank(original=("EUR", "100"), unit="EUR", rate="4.0800",
                  remittance=["Card purchase", "Original transaction amount EUR 100", "1 PLN = 0.1 EUR"])
        receipt_id = self.receipt()
        candidate, = self.detail(receipt_id)["candidates"]
        self.assertEqual(candidate["match_strength"], "ambiguous")
        self.assertTrue(candidate["fx"]["conflicting"])
        self.assertEqual(receipt_fx.remittance_original_amounts(["Original amount: EUR 100 PLN", "Total 100 EUR"]), [])

    def test_other_owner_or_stale_bank_sidecar_does_not_supply_evidence(self):
        self.bank(original=("EUR", "100"))
        receipt_id = self.receipt()
        key = (self.accounts["a"], "fx")
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, key).owner = "other"
        self.assertEqual(self.detail(receipt_id)["candidates"], [])
        with self.sessions.begin() as session:
            record = session.get(db.BudgetTransactionMetadata, key)
            record.owner, record.ledger_fingerprint = "owner", "0" * 64
        self.assertEqual(self.detail(receipt_id)["candidates"], [])

    def test_rate_only_and_malformed_or_zero_transactions_fail_closed(self):
        self.bank("rate-only", unit="EUR", rate="4.08")
        self.bank("zero", amount="0")
        self.bank("malformed")
        self.change_transaction(reference="malformed", remittance_information="unstructured")
        receipt_id = self.receipt()
        with self.sessions.begin() as session:
            _cache(session)
        self.assertEqual(self.detail(receipt_id)["candidates"], [])

    def test_stated_fee_from_structured_receipt_and_unrelated_rate_pair(self):
        self.bank(original=("EUR", "100"), unit="GBP", rate="4.08")
        match, = self.detail(self.receipt(fee="1.20"))["candidates"]
        self.assertEqual(match["match_strength"], "ambiguous")
        self.assertEqual(match["fx"]["explicit_fee"], {"currency": "EUR", "amount": "1.20"})

    def test_untrusted_optional_conversion_fields_fail_closed(self):
        self.bank(original=("EUR", "100"))
        receipt_id = self.receipt()
        key = (self.accounts["a"], "fx")
        meta_key = receipt_fx.receipt_conversion_key(receipt_id)
        with self.sessions.begin() as session:
            session.get(db.BudgetTransactionMetadata, key).payload_json = "[]"
            session.add(db.AppMeta(key=meta_key, value="{"))
        self.assertEqual(self.detail(receipt_id)["candidates"], [])
        self.assertEqual(receipt_fx.receipt_conversion_fields(
            "FX fee: 0 PLN\nAmount charged: pending\n1 EUR = 1 EUR\n1 EUR = 0 PLN",
        ), {})

    def test_prefetch_refresh_keeps_the_latest_quote_during_races_and_retries(self):
        self.bank()
        self.receipt()
        day = date.fromisoformat(_DAY)
        key = rates._key("EUR", day)

        def another_fetch_completed(_currency, _day):
            with self.sessions.begin() as session:
                session.merge(db.AppMeta(key=key, value=json.dumps({"fetched_at": _NOW + 1, "quote": _quote()})))
            return _quote("9.0")

        with mock.patch.object(rates, "fetch_nbp_quote", side_effect=another_fetch_completed):
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW), 1)
        with mock.patch.object(rates, "fetch_nbp_quote", side_effect=[_quote("3.98", "2026-11-19"), _quote()]):
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW + 3601), 1)
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW + 7201), 1)
        with self.sessions() as session:
            self.assertEqual(rates.cached_quote(session, "EUR", day), _quote())

    def test_nbp_does_not_triangulate_two_foreign_currencies(self):
        self.bank(amount="100", currency="USD")
        identifier = self.receipt(currency="EUR")
        with self.sessions.begin() as session:
            _cache(session)
            _cache(session, currency="USD")
        self.assertEqual(self.detail(identifier)["candidates"], [])

    def test_merchant_evidence_outranks_a_closer_market_amount(self):
        self.bank("merchant", amount="408")
        identifier = self.receipt()
        with self.sessions.begin() as session:
            _cache(session)
            _receipt(session, currency="PLN", amount="408")
            _receipt(session, card_last4="1234")
            _receipt(session, day="2026-11-30")
            _receipt(session, merchant="Unrelated seller", amount="200")
        sole, = self.detail(identifier)["candidates"]
        self.assertEqual(sole["match_strength"], "medium")
        self.bank("look-alike", amount="400", remittance=["Another provider"])
        candidates = self.detail(identifier)["candidates"]
        self.assertEqual([item["amount"] for item in candidates], ["408", "400"])
        self.assertEqual([item["merchant_token_hit"] for item in candidates], [True, False])
        self.assertEqual([item["match_strength"] for item in candidates], ["ambiguous", "ambiguous"])

    def test_peer_pool_limit_stays_ambiguous(self):
        self.bank(original=("EUR", "100"))
        first = self.receipt()
        self.receipt()
        self.receipt()
        with mock.patch("hestia.budget.receipt_store.MAX_CANDIDATES", 1):
            candidate, = self.detail(first)["candidates"]
        self.assertEqual(candidate["match_strength"], "ambiguous")

    def test_same_currency_candidate_keeps_priority_and_existing_link_shape(self):
        self.bank("same-currency", amount="100", currency="EUR")
        self.bank("cross-currency", original=("EUR", "100"))
        identifier = self.receipt()
        with mock.patch("hestia.budget.receipt_store.MAX_CANDIDATES", 1):
            detail = self.detail(identifier)
        match, = detail["candidates"]
        self.assertEqual(match["currency"], "EUR")
        self.assertNotIn("fx", match)
        self.receipts.confirm("owner", _values(detail, match), include_fx=True)
        self.assertNotIn("fx_link", self.detail(identifier)["receipt"])

    def test_accepted_fx_link_detects_uncertain_ledger_entry(self):
        self.bank(original=("EUR", "100"))
        identifier = self.receipt()
        detail = self.detail(identifier)
        self.receipts.confirm("owner", _values(detail, detail["candidates"][0]), include_fx=True)
        self.change_transaction(reference="fx", uncertain=True)
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 1), 1)
        self.assertEqual(self.detail(identifier)["receipt"]["status"], "needs_review")

    def test_confirmation_rechecks_current_card_account_scope(self):
        self.bank(account="b", original=("EUR", "100"))
        identifier = self.receipt(card_last4="1234")
        detail = self.detail(identifier)
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"fx-receipts",
                                           card_accounts={"1234": [self.accounts["a"]]})
        with self.assertRaisesRegex(ReceiptError, "budget_receipt_stale_source"):
            self.receipts.confirm("owner", _values(detail, detail["candidates"][0]), include_fx=True)
        self.assertFalse(self.detail(identifier)["receipt"]["linked"])

    def test_disabled_account_invalidates_accepted_fx_link(self):
        self.bank(original=("EUR", "100"))
        identifier = self.receipt()
        detail = self.detail(identifier)
        self.receipts.confirm("owner", _values(detail, detail["candidates"][0]), include_fx=True)
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.accounts["a"]).enabled = False
            self.assertEqual(reconcile_receipt_links(session, "owner", _NOW + 1), 1)
        refreshed = self.detail(identifier)
        self.assertEqual(refreshed["receipt"]["status"], "needs_review")
        self.assertEqual(refreshed["candidates"], [])

    def test_cache_miss_validation_respects_date_and_payload_bounds(self):
        day = date.fromisoformat(_DAY)
        key = rates._key("EUR", day)
        for malformed in ("{", "[]", "x" * 513, json.dumps({"quote": _quote("4.0", "2026-11-22")})):
            with self.subTest(prefix=malformed[:10]), self.sessions.begin() as session:
                session.merge(db.AppMeta(key=key, value=malformed))
                session.flush()
                self.assertIsNone(rates.cached_quote(session, "EUR", day))
        self.assertIsNone(rates.reference_day("yesterday"))
        self.assertIsNone(rates.validate_quote(_quote(), None))

    def test_prefetch_scopes_owner_pairs_and_bounds_cache(self):
        self.bank()
        self.bank("foreign-eur", account="foreign", amount="100", currency="EUR")
        self.receipt()
        self.receipt(currency="PLN", amount="400")  # No owner EUR booking, so no separate inverse demand.
        self.receipt(currency="USD", day="2026-11-24")  # Future reference date.
        self.receipt(currency="CAD", payment="cash")
        with self.sessions.begin() as session:
            _cache(session, currency="CZK", day="2025-11-20")
        with mock.patch.object(rates, "MAX_CACHE_ROWS", 1), mock.patch.object(rates, "MAX_FETCHES", 1), \
                mock.patch.object(rates, "fetch_nbp_quote", return_value=_quote()) as fetch:
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW), 1)
            fetch.assert_called_once_with("EUR", date.fromisoformat(_DAY))
            self.assertEqual(rates.prefetch_nbp_quotes(self.sessions, _NOW + 1), 0)
        with self.sessions() as session:
            self.assertIsNone(rates.cached_quote(session, "CZK", date(2025, 11, 20)))
            self.assertEqual(rates.cached_quote(session, "EUR", date.fromisoformat(_DAY)), _quote())


class NBPEndpointTests(unittest.TestCase):
    def _response(self, *, status=200, body=None):
        patch = mock.patch.object(rates, "HTTPSConnection")
        constructor = patch.start()
        self.addCleanup(patch.stop)
        connection = constructor.return_value
        connection.getresponse.return_value.status = status
        connection.getresponse.return_value.read.return_value = body
        return constructor, connection

    def test_allowlisted_https_range_timeout_and_previous_publication(self):
        raw = {"code": "EUR", "table": "A", "rates": [
            {"effectiveDate": "2026-11-09", "no": _TABLE, "mid": 4.1},
            {"effectiveDate": "2026-11-12", "no": _TABLE, "mid": 8},
            {"effectiveDate": "2026-11-10", "no": _TABLE, "mid": 4.2},
        ]}
        constructor, connection = self._response(body=json.dumps(raw).encode())
        self.assertEqual(rates.fetch_nbp_quote("EUR", date(2026, 11, 11)), _quote("4.2", "2026-11-10"))
        constructor.assert_called_once_with("api.nbp.pl", timeout=3)
        connection.request.assert_called_once_with(
            "GET", "/api/exchangerates/rates/a/EUR/2026-11-01/2026-11-11/?format=json",
            headers={"Accept": "application/json"},
        )
        connection.close.assert_called_once()
        self.assertIsNone(rates.fetch_nbp_quote("https://example.invalid", date(2026, 11, 11)))
        self.assertEqual(constructor.call_count, 1)

    def test_invalid_and_conflicting_publications_fail_closed(self):
        constructor, connection = self._response()
        response = connection.getresponse.return_value
        for value in (b"x" * (rates._MAX_RESPONSE + 1), b"?", b"[]",
                      b'{"code":"EUR","table":"A","rates":{}}',
                      json.dumps({"code": "EUR", "table": "A", "rates": [
                          _quote("4.0"), _quote("4.1"), _quote("0"), {"effectiveDate": "2026-33-01"},
                      ]}).encode()):
            response.read.return_value = value
            self.assertIsNone(rates.fetch_nbp_quote("EUR", date.fromisoformat(_DAY)))
        constructor.side_effect = TimeoutError
        self.assertIsNone(rates.fetch_nbp_quote("EUR", date.fromisoformat(_DAY)))
        self.assertIsNone(rates.fetch_nbp_quote("EUR", date(2001, 1, 1)))

    def test_future_only_and_redirect_fail_closed(self):
        raw = {"code": "EUR", "table": "A", "rates": [{"effectiveDate": "2026-11-23", "no": _TABLE, "mid": 4}]}
        _, connection = self._response(body=json.dumps(raw).encode())
        self.assertIsNone(rates.fetch_nbp_quote("EUR", date.fromisoformat(_DAY)))
        connection.getresponse.return_value.status = 302
        self.assertIsNone(rates.fetch_nbp_quote("EUR", date.fromisoformat(_DAY)))
        self.assertEqual(connection.request.call_count, 2)
        self.assertEqual(connection.getresponse.return_value.read.call_count, 1)


class ReceiptFXHTTPTests(unittest.IsolatedAsyncioTestCase):
    rows = http_fixture.BudgetAPITests.rows
    headers = http_fixture.BudgetAPITests.headers
    token = http_fixture.BudgetAPITests.token

    async def asyncSetUp(self):
        environment = mock.patch.dict(os.environ, {receipt_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
                                                     "HESTIA_BUDGET_POLLER_ENABLED": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.enterContext(mock.patch.object(rates, "HTTPSConnection", side_effect=AssertionError("unexpected network")))
        await http_fixture.BudgetAPITests.asyncSetUp(self)

    async def test_prefetch_skips_an_unauthoritative_database(self):
        with mock.patch.object(receipt_api.store_sql, "is_users_db_authoritative", return_value=False), \
                mock.patch.object(receipt_api, "prefetch_nbp_quotes") as prefetch:
            receipt_api._prefetch_rates()
        prefetch.assert_not_called()

    async def test_background_prefetch_recovers_from_failed_pass_and_runs_off_request_path(self):
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        attempts = []

        def prefetch(sessions, now):
            attempts.append(now)
            with sessions() as session:
                self.assertIsNotNone(session.get(db.User, "alice"))
            if len(attempts) == 1:
                raise RuntimeError("synthetic cache failure")
            loop.call_soon_threadsafe(finished.set)

        app = self.client.app
        worker = next(context for context in app.cleanup_ctx if context.__name__ == "fx_prefetch")
        with mock.patch.object(receipt_api, "_FX_PREFETCH_INITIAL_SECONDS", 0), mock.patch.object(
            receipt_api, "_FX_PREFETCH_SECONDS", 0.01,
        ), mock.patch.object(receipt_api, "prefetch_nbp_quotes", side_effect=prefetch), self.assertLogs(
            receipt_api.__name__, level="WARNING",
        ) as log:
            lifecycle = worker(app)
            await anext(lifecycle)
            try:
                await asyncio.wait_for(finished.wait(), timeout=5)
            finally:
                with self.assertRaises(StopAsyncIteration):
                    await anext(lifecycle)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertIn("Receipt exchange-rate prefetch failed", log.output[0])

    async def test_legacy_responses_strip_fx_even_if_the_store_returns_it(self):
        fx = {"source": "nbp"}
        receipt_id = str(uuid.uuid4())
        result = {
            "receipt": {"receipt_id": receipt_id, "fx_link": fx},
            "candidates": [
                {"match_strength": "medium", "fx": fx},
                {"match_strength": "ambiguous"},
                {"match_strength": "id", "fx": {"source": "bank_original"}},
                {"match_strength": "distinctive", "fx": None},
                {"match_strength": "nip"},
            ],
        }
        with mock.patch.object(receipt_api, "_run_read", side_effect=lambda *_a, **_kw: deepcopy(result)):
            for version in ("0.64.0", "0.65.0", "0.68.0"):
                with self.subTest(version=version):
                    headers = {**self.headers("alice", origin=True), "X-Hestia-Contract-Version": version}
                    response = await self.client.get(receipt_api.PREFIX + "/detail",
                                                     params={"receipt_id": receipt_id}, headers=headers)
                    self.assertEqual(response.status, 200)
                    if version == "0.68.0":
                        self.assertEqual(await response.json(), result)
                    else:
                        self.assertEqual(await response.json(), {
                            "receipt": {"receipt_id": receipt_id},
                            "candidates": [{"match_strength": "distinctive"}, {"match_strength": "nip"}],
                        })

    async def test_opt_in_gates_candidates_and_link_details(self):
        tx = http_fixture.transaction("synthetic-fx", amount="408", booking_date=_DAY,
                                     remittance_information=[_MERCHANT])
        with self.sessions.begin() as session:
            receipt_id = _receipt(session, owner="alice")
            _cache(session)
            session.add(db.BudgetTransaction(account_id=self.alice_id, entry_reference="synthetic-fx",
                                             payload_json=json.dumps(tx), observed_at=_NOW, uncertain=False))
        path = receipt_api.PREFIX + "/detail"
        with mock.patch.object(rates, "HTTPSConnection", side_effect=AssertionError("review called network")) as transport:
            versions = {}
            for version in (None, "0.64.0", "0.65.0", "0.67.0", "0.68.0"):
                headers = self.headers("alice", origin=True)
                if version is not None:
                    headers["X-Hestia-Contract-Version"] = version
                response = await self.client.get(path, params={"receipt_id": receipt_id}, headers=headers)
                self.assertEqual(response.status, 200)
                versions[version] = await response.json()
            self.assertEqual(versions["0.64.0"], versions["0.67.0"])
            self.assertEqual(versions[None]["candidates"], [])
            self.assertEqual(versions["0.64.0"]["candidates"], [])
            current = versions["0.68.0"]
            api_contract.BudgetReceiptDetailResult.model_validate(current)
            match, = current["candidates"]
            self.assertEqual(match["match_strength"], "medium")
            self.assertFalse(current["receipt"]["linked"])
            body = _values(current, match)
            legacy_headers = {**headers, "X-Hestia-Contract-Version": "0.64.0"}
            legacy = await self.client.post(receipt_api.PREFIX + "/confirm", json=body, headers=legacy_headers)
            self.assertEqual(legacy.status, 400)
            response = await self.client.post(receipt_api.PREFIX + "/confirm", json=body, headers=headers)
            self.assertEqual(response.status, 200)
            linked = await response.json()
            api_contract.BudgetReceiptConfirmResult.model_validate(linked)
            self.assertEqual(linked["receipt"]["fx_link"], match["fx"])
            headers["X-Hestia-Contract-Version"] = "0.64.0"
            response = await self.client.get(path, params={"receipt_id": receipt_id}, headers=headers)
            self.assertNotIn("fx_link", (await response.json())["receipt"])
            transport.assert_not_called()
