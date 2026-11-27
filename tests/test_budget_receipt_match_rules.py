"""Late document IDs and card round-ups against a synthetic ledger."""
from __future__ import annotations

import json
import unittest
from unittest import mock
from uuid import uuid4

from hestia import api_contract, db
from hestia.budget import receipt_api
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.gmail_store import GATEWAY_REF_DOMAIN, digest
from hestia.budget.receipt_match_rules import (
    invoice_number_pattern, remittance_has_invoice_number, rounding_delta, stored_rounding_delta,
)
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_reconcile import reconcile_receipt_links
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_receipts as fixtures
from tests.test_budget_analytics_core import payload

_SHOP = "Synthetica Transit"
_REFERENCE_KEY = b"synthetic-reference-only"
_INVOICE_NUMBER = "Sy/482-A"
_INVOICE_DATE = "2026-08-04"
_DUE_DATE = "2026-08-20"
_LATE_DATE = "2026-09-20"
_CARD_DATE = "2026-11-19"


class RuleTests(unittest.TestCase):
    def test_invoice_number_uses_alphanumeric_boundaries(self):
        self.assertIsNone(invoice_number_pattern(None))
        self.assertIsNone(invoice_number_pattern("48-21"))
        pattern = invoice_number_pattern(_INVOICE_NUMBER)
        for remittance, expected in (
            (["SY482A"], True),
            (["Payment: s y.482 / a"], True),
            (["XSY482A9"], False),
            (["SY482A9"], False),
            ([""], False),
            ([None], False),
            ("SY482A", False),
        ):
            with self.subTest(remittance=remittance):
                self.assertEqual(remittance_has_invoice_number(pattern, {
                    "remittance_information": remittance,
                }), expected)

    def test_decimal_rounding_bounds(self):
        for total, bank, expected in (
            ("42.91", "43", "0.09"), ("42.01", "43", "0.99"), ("42.009", "43", None),
            ("43", "43", None), ("42", "43", None), ("43.03", "43", None),
            ("42.91", "43.01", None), ("0", "1", None),
        ):
            with self.subTest(total=total, bank=bank):
                self.assertEqual(rounding_delta(total, bank), expected)

    def test_stored_rounding_delta_is_optional_and_requires_valid_evidence(self):
        for evidence in (None, "{", "[]", "{}", '{"rounding_delta": 0.09}'):
            with self.subTest(evidence=evidence):
                self.assertIsNone(stored_rounding_delta(evidence))
        self.assertEqual(stored_rounding_delta('{"rounding_delta": "0.09"}'), "0.09")

    def test_old_confirm_contract_omits_rounding_delta(self):
        response = receipt_api._response("confirm", {
            "receipt": {"rounding_delta": "0.09", "total_amount": "42.91"},
        }, (0, 67, 0))
        self.assertEqual(json.loads(response.body), {"receipt": {"total_amount": "42.91"}})


class ReceiptMatchTests(fixtures.AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only",
                                           ref_hmac_secret=_REFERENCE_KEY)

    def add_receipt(self, **overrides):
        identifier = str(uuid4())
        values = dict(
            id=identifier, owner="owner", raw_shop_name=_SHOP, document_kind="receipt", receipt_kind="purchase",
            receipt_date=_CARD_DATE, total_amount="42.91", currency="PLN", payment_method="card", status="imported",
            source_kind="email", version=1, created_at=self.now, updated_at=self.now,
        )
        values.update(overrides)
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(**values))
            session.flush()
            session.add(db.BudgetReceiptLine(
                receipt_id=identifier, owner="owner", line_no=1, line_kind="other", raw_name="Synthetic document",
                raw_name_key="synthetic document", quantity="1", gross_line_total=values["total_amount"],
                net_line_total=values["total_amount"],
            ))
        return identifier

    def add_card(self, reference="card-charge", *, day="2026-11-20", account="a", title=None, **changes):
        self.add_transaction(account, reference, **dict(
            amount="43.00", transaction_date=day, booking_date=day, value_date=day,
            remittance_information=[title or f"Płatność kartą {_SHOP}"],
        ) | changes)

    def candidates(self, identifier):
        return self.receipts.detail("owner", identifier)["candidates"]

    def confirmation(self, identifier, candidate):
        detail = self.receipts.detail("owner", identifier)["receipt"]
        link = {key: candidate[key] for key in ("account_id", "entry_reference", "candidate_token")}
        return fixtures.confirm_values({"draft": detail, "receipt_id": identifier, "version": detail["version"]}, link=link)

    def test_late_invoice_id_requires_exact_amount(self):
        identifier = self.add_receipt(document_kind="invoice", payment_method="unknown", invoice_number=_INVOICE_NUMBER,
                                      receipt_date=_INVOICE_DATE, due_date=_DUE_DATE, total_amount="156.82")
        self.add_transaction("a", "late-transfer", amount="156.82", booking_date=_LATE_DATE, value_date=_LATE_DATE,
                             remittance_information=["sY 482.a"])
        candidate, = self.candidates(identifier)
        self.assertEqual(candidate["match_strength"], "id")
        self.assertEqual(candidate["entry_reference"], fixtures.reference_digest("late-transfer"))
        old_store = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"legacy-synthetic",
                                       extended_matching=False)
        self.assertEqual(old_store.detail("owner", identifier)["candidates"], [])
        request = self.confirmation(identifier, candidate)

        for remit, amount in ((["Synthetic transfer"], "156.82"), (["SY482A"], "156.81")):
            with self.subTest(remit=remit, amount=amount):
                self.change_transaction("a", "late-transfer", remittance_information=remit, amount=amount)
                self.assertEqual(self.candidates(identifier), [])

        self.change_transaction("a", "late-transfer", remittance_information=["4821"], amount="156.82")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, identifier).invoice_number = "4821"
        self.assertEqual(self.candidates(identifier), [])
        self.change_transaction("a", "late-transfer", remittance_information=["sY 482.a"])
        with self.assertRaises(ReceiptError) as stale:
            self.receipts.confirm("owner", request)
        self.assertEqual(stale.exception.code, "budget_receipt_stale_source")

        self.change_transaction("a", "late-transfer", remittance_information=["SY482A"])
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, identifier).invoice_number = _INVOICE_NUMBER
        candidate, = self.candidates(identifier)
        saved = self.receipts.confirm("owner", self.confirmation(identifier, candidate))["receipt"]
        self.assertTrue(saved["linked"])
        self.assertEqual(saved["total_amount"], "156.82")

    def test_invoice_id_window_boundaries(self):
        identifier = self.add_receipt(document_kind="invoice", invoice_number=_INVOICE_NUMBER,
                                      receipt_date=_INVOICE_DATE, due_date=_DUE_DATE)
        self.add_transaction("a", "receipt-ref", amount="42.91", remittance_information=[_INVOICE_NUMBER])
        for day, present in (("2026-08-01", True), ("2026-07-31", False),
                             ("2026-12-18", True), ("2026-12-19", False)):
            with self.subTest(day=day):
                self.change_transaction("a", "receipt-ref", booking_date=day, value_date=day)
                self.assertEqual([c["match_strength"] for c in self.candidates(identifier)], ["id"] if present else [])

    def test_late_existing_gateway_reference(self):
        provider_ref = "P24-SYN-654-321"
        identifier = self.add_receipt(
            document_kind="invoice", receipt_date=_INVOICE_DATE, due_date=_DUE_DATE, gateway="p24",
            gateway_ref_hmac=digest(_REFERENCE_KEY, GATEWAY_REF_DOMAIN, provider_ref), fee_amount="1",
        )
        self.add_transaction("a", "late-gateway", amount="41.91", booking_date=_LATE_DATE, value_date=_LATE_DATE,
                             remittance_information=[provider_ref])
        self.assertEqual(self.candidates(identifier), [])
        self.change_transaction("a", "late-gateway", amount="42.91")
        candidate, = self.candidates(identifier)
        self.assertEqual(candidate["match_strength"], "id")
        self.assertTrue(self.receipts.confirm("owner", self.confirmation(identifier, candidate))["receipt"]["linked"])

    def test_short_gateway_reference_does_not_widen_window(self):
        short_ref = "54321"
        identifier = self.add_receipt(
            document_kind="invoice", receipt_date=_INVOICE_DATE, due_date=_DUE_DATE, gateway="payu",
            gateway_ref_hmac=digest(_REFERENCE_KEY, GATEWAY_REF_DOMAIN, short_ref),
        )
        self.add_transaction("a", "short-gateway", amount="42.91", booking_date=_LATE_DATE, value_date=_LATE_DATE,
                             remittance_information=[f"PayU {short_ref}"])
        self.assertEqual(self.candidates(identifier), [])
        self.change_transaction("a", "short-gateway", booking_date=_DUE_DATE, value_date=_DUE_DATE)
        candidate, = self.candidates(identifier)
        self.assertEqual(candidate["match_strength"], "id")

    def test_round_up_strength_confirm_and_reconciliation(self):
        identifier = self.add_receipt()
        self.add_card()
        candidate, = self.candidates(identifier)
        self.assertEqual((candidate["match_rule"], candidate["match_strength"], candidate["rounding_delta"]),
                         ("rounded_up", "medium", "0.09"))
        self.change_transaction("a", "card-charge", transaction_date=_CARD_DATE)
        candidate, = self.candidates(identifier)
        self.assertEqual((candidate["match_strength"], candidate["date_distance"]), ("strong", 0))

        saved = self.receipts.confirm("owner", self.confirmation(identifier, candidate))["receipt"]
        self.assertEqual((saved["total_amount"], saved["rounding_delta"], saved["linked_entry_ref"]),
                         ("42.91", "0.09", fixtures.reference_digest("card-charge")))
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 0)
        self.assertEqual(self.receipts.detail("owner", identifier)["receipt"]["rounding_delta"], "0.09")
        self.change_transaction("a", "card-charge", amount="44")
        with self.sessions.begin() as session:
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now), 1)
        self.assertNotIn("rounding_delta", self.receipts.detail("owner", identifier)["receipt"])

    def test_exact_candidate_beats_round_up_and_amounts_must_meet_bounds(self):
        identifier = self.add_receipt()
        self.add_card()
        self.add_card("exact-charge", amount="42.9100")
        exact, = self.candidates(identifier)
        self.assertEqual(exact["entry_reference"], fixtures.reference_digest("exact-charge"))
        self.assertNotIn("rounding_delta", exact)
        self.change_transaction("a", "exact-charge", amount="43.05")
        self.change_transaction("a", "card-charge", amount="43.01")
        self.assertEqual(self.candidates(identifier), [])
        self.change_transaction("a", "card-charge", amount="44")
        integer = self.add_receipt(total_amount="43")
        self.assertEqual(self.candidates(integer), [])
        self.change_transaction("a", "card-charge", amount="43.00")
        exact, = self.candidates(integer)
        self.assertNotIn("match_rule", exact)

    def test_round_up_requires_merchant_and_card_and_date_and_currency(self):
        identifier = self.add_receipt()
        self.add_card()
        for changes in (
            {"remittance_information": ["Płatność kartą Fikcyjny Targ"]},
            {"remittance_information": [f"Bank transfer {_SHOP}"]},
            {"transaction_date": "2026-11-22"},
            {"currency": "EUR"},
            {"remittance_information": ["Płatność kartą SP. Z O.O."]},
        ):
            with self.subTest(changes=changes):
                defaults = {
                    "transaction_date": "2026-11-20", "currency": "PLN",
                    "remittance_information": [f"Card purchase {_SHOP}"],
                }
                self.change_transaction("a", "card-charge", **(defaults | changes))
                self.assertEqual(self.candidates(identifier), [])
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, identifier).raw_shop_name = "SP. Z O.O."
        self.assertEqual(self.candidates(identifier), [])

    def test_round_up_rejects_both_directions_of_ambiguity_even_at_confirm(self):
        identifier = self.add_receipt()
        self.add_card()
        candidate, = self.candidates(identifier)
        request = self.confirmation(identifier, candidate)
        self.add_card("additional-charge", account="b")
        self.assertEqual(self.candidates(identifier), [])
        with self.assertRaises(ReceiptError) as error:
            self.receipts.confirm("owner", request)
        self.assertEqual(error.exception.code, "budget_receipt_stale_source")
        self.change_transaction("b", "additional-charge", status="PDNG")
        peer = self.add_receipt(total_amount="42.51")
        self.assertEqual(self.candidates(identifier), [])
        self.assertEqual(self.candidates(peer), [])

    def test_exact_candidate_for_another_receipt_beats_round_up(self):
        rounded_receipt = self.add_receipt()
        exact_receipt = self.add_receipt(total_amount="43")
        self.add_card()
        self.assertEqual(self.candidates(rounded_receipt), [])
        exact, = self.candidates(exact_receipt)
        self.assertNotIn("match_rule", exact)

    def test_round_up_link_rejects_an_edited_receipt_date(self):
        identifier = self.add_receipt()
        self.add_card()
        candidate, = self.candidates(identifier)
        request = self.confirmation(identifier, candidate)
        with self.assertRaises(ReceiptError) as error:
            self.receipts.confirm("owner", {**request, "receipt_date": "2026-11-20"})
        self.assertEqual(error.exception.code, "budget_receipt_stale_source")

    def test_round_up_link_rechecks_exact_priority_at_confirm(self):
        identifier = self.add_receipt()
        self.add_card()
        candidate, = self.candidates(identifier)
        request = self.confirmation(identifier, candidate)
        self.add_card("exact-charge", amount="42.91")
        with self.assertRaises(ReceiptError) as error:
            self.receipts.confirm("owner", request)
        self.assertEqual(error.exception.code, "budget_receipt_stale_source")

    def test_an_occupied_card_charge_counts_toward_round_up_ambiguity(self):
        rounded_receipt = self.add_receipt()
        exact_receipt = self.add_receipt(total_amount="43")
        self.add_card()
        candidate, = self.candidates(exact_receipt)
        self.receipts.confirm("owner", self.confirmation(exact_receipt, candidate))
        self.add_card("second-charge", day=_CARD_DATE)
        self.assertEqual(self.candidates(rounded_receipt), [])

    def test_round_up_skips_a_corrupt_competing_receipt_amount(self):
        identifier = self.add_receipt()
        peer = self.add_receipt()
        self.add_receipt(total_amount="41")
        self.add_receipt(raw_shop_name="Fikcyjny Targ")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, peer).total_amount = "invalid"
        self.add_card()
        candidate, = self.candidates(identifier)
        self.assertEqual(candidate["match_rule"], "rounded_up")

    def test_round_up_rechecks_draft_parse_after_concurrent_receipt(self):
        self.add_card()
        initial_read = self.receipts._read_candidates

        def insert_competitor(owner, parsed):
            candidates, no_bank_data = initial_read(owner, parsed)
            self.assertEqual([c["match_rule"] for c in candidates], ["rounded_up"])
            self.add_receipt()
            return candidates, no_bank_data

        with mock.patch.object(self.receipts, "_read_candidates", side_effect=insert_competitor):
            draft = self.receipts.parse_text("owner", self.draft_text())
        self.assertEqual(draft["candidates"], [])

    @staticmethod
    def draft_text():
        return f"{_SHOP}\nPARAGON FISKALNY\n{_CARD_DATE}\nPrzejazd 1 x 42,91 42,91 A\nSUMA PLN 42,91\nKARTA PŁATNICZA"

    def test_round_up_skips_bad_candidate_evidence_and_token_failures(self):
        identifier = self.add_receipt()
        self.add_card()
        for changes in (
            {"amount": "bogus"},
            {"transaction_date": "2026-11-20T12", "booking_date": None, "value_date": None},
            {"remittance_information": "synthetic card"},
            {"remittance_information": [None]},
        ):
            with self.subTest(changes=changes):
                defaults = {
                    "amount": "43", "transaction_date": _CARD_DATE, "booking_date": _CARD_DATE, "value_date": _CARD_DATE,
                    "remittance_information": [f"Płatność kartą {_SHOP}"],
                }
                self.change_transaction("a", "card-charge", **(defaults | changes))
                self.assertEqual(self.candidates(identifier), [])
        self.change_transaction("a", "card-charge", remittance_information=[f"Płatność kartą {_SHOP}"])
        with mock.patch.object(self.receipts, "_candidate_token", side_effect=AnalyticsError):
            self.assertEqual(self.candidates(identifier), [])


class ReceiptMatchContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ReceiptHTTPTests.asyncSetUp
    rows = fixtures.ReceiptHTTPTests.rows
    token = fixtures.ReceiptHTTPTests.token
    headers = fixtures.ReceiptHTTPTests.headers
    post = fixtures.ReceiptHTTPTests.post
    get = fixtures.ReceiptHTTPTests.get

    async def test_version_gates_parse_detail_and_confirm_fields(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(
                account_id=self.alice_id, entry_reference="synthetic-card", observed_at=123, uncertain=False,
                payload_json=json.dumps(payload(
                    amount="43", booking_date=_CARD_DATE, transaction_date=_CARD_DATE,
                    remittance_information=[f"Card purchase {_SHOP}"],
                )),
            ))
        text = f"{_SHOP}\nPARAGON FISKALNY\n{_CARD_DATE}\nPrzejazd 1 x 42,91 42,91 A\nSUMA PLN 42,91\nKARTA PŁATNICZA"
        version = "X-Hestia-Contract-Version"
        for minor, expected in (("0.67.0", []), ("0.68.0", ["rounded_up"])):
            response = await self.post("parse-text", {"text": text}, headers={**self.headers(origin=True), version: minor})
            self.assertEqual(response.status, 200, await response.text())
            draft = await response.json()
            self.assertEqual([c["match_rule"] for c in draft["candidates"]], expected)
            if not expected:
                await self.post("delete", {"receipt_id": draft["receipt_id"], "expected_version": 1})
        api_contract.BudgetReceiptParseResult.model_validate(draft)
        identifier = draft["receipt_id"]
        for minor, expected in (("0.67.0", []), ("0.68.0", ["strong"])):
            response = await self.get("detail", identifier, headers={**self.headers(), version: minor})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual([c["match_strength"] for c in (await response.json())["candidates"]], expected)
        candidate, = draft["candidates"]
        link = {key: candidate[key] for key in ("account_id", "entry_reference", "candidate_token")}
        values = fixtures.confirm_values(draft, link=link)
        confirmed = await self.post("confirm", values, headers={**self.headers(origin=True), version: "0.68.0"})
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        saved = (await confirmed.json())["receipt"]
        self.assertEqual(saved["rounding_delta"], "0.09")
        older = await self.get("detail", identifier, headers={**self.headers(), version: "0.67.0"})
        self.assertNotIn("rounding_delta", (await older.json())["receipt"])
