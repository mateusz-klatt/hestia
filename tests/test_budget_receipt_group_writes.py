"""Store-level M:1 receipt group matching, concurrency, reconciliation, transfer exclusion and dissolve tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hestia import authz, db, store_sql
from hestia.budget import device_token
from hestia.budget.analytics_core import AnalyticsError, canonical_money, financial_fingerprint, negate_money, sum_money
from hestia.budget.analytics_store import BudgetAnalyticsStore
from hestia.budget.gmail_store import GATEWAY_REF_DOMAIN, digest
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_reconcile import reconcile_receipt_links
from hestia.budget.receipt_store import (
    MAX_GROUP_POOL,
    MAX_GROUP_PROPOSALS,
    MAX_GROUP_TX_CANDIDATES,
    BudgetReceiptStore,
    ReceiptGroupError,
)
from hestia.budget.store import BudgetStore
from tests import test_budget_api as api_fixture
from tests.test_budget_analytics_store import AnalyticsCase


def _uuid():
    return str(uuid.uuid4())


class ReceiptGroupWriteTests(AnalyticsCase):
    _HMAC_SECRET = b"receipt-group-ref-hmac-secret-32"

    def setUp(self):
        super().setUp()
        self.acc_a = self.accounts["a"]
        self.acc_2 = self.accounts["b"]
        self.acc_a_hash = "a"
        with self.sessions() as session:
            other_acc = session.scalar(select(db.BudgetAccount).where(db.BudgetAccount.owner != "owner"))
            self.acc_b = other_acc.id
        self.now = 2_000_000.0
        self.receipts = self._make_receipt_store()

    def _make_receipt_store(self, *, sessions=None, card_accounts=None, actor=None, dev_id=None):
        return BudgetReceiptStore(
            sessions or self.sessions,
            clock=lambda: self.now,
            cursor_secret=b"receipts-only",
            device_id=dev_id,
            actor=actor,
            ref_hmac_secret=self._HMAC_SECRET,
            card_accounts=card_accounts,
        )

    def _add_tx(self, ref, *, account_id=None, amount="100.00", currency="PLN", direction="DBIT",
                status="BOOK", booking_date="2026-11-20", transaction_date=None, value_date=None,
                remittance=None, creditor=None, debtor=None, uncertain=False):
        acc = account_id or self.acc_a
        payload = api_fixture.transaction(
            ref, amount=amount, currency=currency, credit_debit_indicator=direction,
            status=status, booking_date=booking_date, transaction_date=transaction_date,
            value_date=value_date,
            remittance_information=["Synthetic purchase"] if remittance is None else remittance,
        )
        if creditor is not None:
            payload["creditor"] = {"name": creditor}
        if debtor is not None:
            payload["debtor"] = {"name": debtor}
        with self.sessions.begin() as session:
            existing = session.get(db.BudgetTransaction, (acc, ref))
            if existing is not None:
                existing.payload_json = json.dumps(payload)
                existing.uncertain = uncertain
            else:
                session.add(db.BudgetTransaction(
                    account_id=acc, entry_reference=ref, payload_json=json.dumps(payload),
                    observed_at=self.now, uncertain=uncertain,
                ))

    def _add_receipt(self, *, owner="owner", status="confirmed", shop_name="Shop A", shop_id=None,
                     nip=None, kind="purchase", receipt_date="2026-11-20", total_amount="50.00",
                     currency="PLN", payment_method="card", document_kind="receipt", due_date=None,
                     gateway=None, gateway_status=None, fee_amount=None, payee_host=None,
                     forwarded_sender_domain=None, gateway_ref=None, blik_ref=None,
                     evidence_for_receipt_id=None, card_last4=None, version=1, source_kind="live_text"):
        rid = _uuid()
        with self.sessions.begin() as session:
            row = db.BudgetReceipt(
                id=rid, owner=owner, submitted_by=owner, shop_id=shop_id,
                raw_shop_name=shop_name, raw_nip=nip, receipt_kind=kind,
                receipt_date=receipt_date, receipt_time="12:00", total_amount=total_amount,
                currency=currency, payment_method=payment_method, status=status,
                source_kind=source_kind, document_kind=document_kind, due_date=due_date,
                gateway=gateway, gateway_status=gateway_status, fee_amount=fee_amount,
                payee_host=payee_host, forwarded_sender_domain=forwarded_sender_domain,
                gateway_ref_hmac=None if gateway_ref is None else digest(self._HMAC_SECRET, GATEWAY_REF_DOMAIN, gateway_ref),
                blik_ref_hmac=None if blik_ref is None else digest(self._HMAC_SECRET, GATEWAY_REF_DOMAIN, blik_ref),
                evidence_for_receipt_id=evidence_for_receipt_id, card_last4=card_last4,
                version=version, created_at=self.now, updated_at=self.now,
            )
            session.add(row)
            if status in ("confirmed", "draft"):
                session.flush()
                session.add(db.BudgetReceiptLine(
                    receipt_id=rid, owner=owner, line_no=1, line_kind="item",
                    raw_name="Synthetic Item", raw_name_key="synthetic item", ptu_code="A",
                    quantity="1", raw_unit="szt", unit_price_raw=total_amount,
                    gross_line_total=total_amount, discount_amount="0", net_line_total=total_amount,
                ))
        return rid

    def _add_shop(self, *, owner="owner", name="Apple", chain_key="apple", nip=None):
        sid = _uuid()
        with self.sessions.begin() as session:
            session.add(db.BudgetShop(
                id=sid, owner=owner, name=name, chain_key=chain_key, nip=nip,
                version=1, created_at=self.now, updated_at=self.now,
            ))
        return sid

    def _link_from_candidate(self, candidate):
        return {
            "account_id": candidate["account_id"],
            "entry_reference": candidate["entry_reference"],
            "candidate_token": candidate["candidate_token"],
        }

    def test_apple_consolidated_debit_exclusion_and_atomic_group_link(self):
        shop_id = self._add_shop(name="Apple Distribution", chain_key="apple")
        self._add_tx(
            "apple-debit", amount="100.00", booking_date="2026-11-20",
            remittance=["APPLE.COM/BILL 445511"], creditor="Apple Distribution",
        )
        r1 = self._add_receipt(shop_name="Apple Services", shop_id=shop_id, receipt_date="2026-11-18", total_amount="19.99",
                               forwarded_sender_domain="email.apple.com", payee_host="apple.com")
        r2 = self._add_receipt(shop_name="Apple Services", shop_id=shop_id, receipt_date="2026-11-19", total_amount="29.99",
                               forwarded_sender_domain="email.apple.com", payee_host="apple.com")
        r3 = self._add_receipt(shop_name="Apple Services", shop_id=shop_id, receipt_date="2026-11-20", total_amount="50.02",
                               forwarded_sender_domain="email.apple.com", payee_host="apple.com")
        # Unrelated shops summing to 100.00 must never join Apple's candidate pool.
        unrelated1 = self._add_receipt(shop_name="Biedronka", receipt_date="2026-11-20", total_amount="80.01")
        unrelated2 = self._add_receipt(shop_name="Zabka", receipt_date="2026-11-20", total_amount="19.99")

        detail = self.receipts.detail("owner", r1)
        self.assertNotIn("candidates", detail)
        self.assertEqual(len(detail["group_candidates"]), 1)
        prop = detail["group_candidates"][0]
        self.assertEqual(set(prop["member_receipt_ids"]), {r1, r2, r3})
        self.assertEqual(prop["members_sum"], "100")
        self.assertIsNone(prop["fee_amount"])
        self.assertIsNone(prop["evidence_receipt_id"])

        cand_res = self.receipts.group_candidates("owner", {"receipt_id": r1})
        self.assertFalse(cand_res["no_bank_data"])
        eligible_ids = {r["receipt_id"] for r in cand_res["eligible_receipts"]}
        self.assertEqual(eligible_ids, {r1, r2, r3})
        self.assertNotIn(unrelated1, eligible_ids)
        self.assertNotIn(unrelated2, eligible_ids)

        linked = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(prop),
            "members": [{"receipt_id": rid, "expected_version": 1} for rid in (r1, r2, r3)],
        })
        self.assertTrue(linked["ok"])
        group_id = linked["group"]["group_id"]
        self.assertEqual(linked["group"]["group_version"], 1)
        self.assertEqual(linked["group"]["group_status"], "confirmed")
        self.assertEqual(linked["group"]["member_count"], 3)
        self.assertTrue(all(m["linked"] and m["version"] == 2 for m in linked["members"]))
        self.assertEqual(linked["linked_account_id"], self.acc_a)
        self.assertIsNone(linked["evidence"])

        # Grouped receipt exposes link_group and hides group_candidates; 1:1 confirm link and sensitive edits fail.
        post_detail = self.receipts.detail("owner", r1)
        self.assertEqual(post_detail["receipt"]["link_group"]["group_id"], group_id)
        self.assertNotIn("group_candidates", post_detail)

        # Re-using the same tx or member fails with 409; non-sensitive line/time edit preserves group link.
        with self.assertRaises(ReceiptError) as dup_tx:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(cand_res["transactions"][0]),
                "members": [{"receipt_id": unrelated1, "expected_version": 1},
                            {"receipt_id": unrelated2, "expected_version": 1}],
            })
        self.assertEqual((dup_tx.exception.status, dup_tx.exception.code), (409, "budget_receipt_tx_already_linked"))

        with self.assertRaises(ReceiptError) as dup_mem:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(prop),
                "members": [{"receipt_id": r1, "expected_version": 2},
                            {"receipt_id": r2, "expected_version": 2}],
            })
        self.assertEqual(dup_mem.exception.status, 409)

        with self.assertRaises(ReceiptError) as edit_block:
            self.receipts.confirm("owner", {
                "receipt_id": r1, "expected_version": 2, "raw_shop_name": "Changed", "raw_nip": None,
                "receipt_kind": "purchase", "receipt_date": "2026-11-18", "receipt_time": "12:00",
                "total_amount": "19.99", "currency": "PLN", "payment_method": "card", "lines": None, "link": None,
            })
        self.assertEqual((edit_block.exception.status, edit_block.exception.code), (409, "budget_receipt_in_group"))

        non_sensitive = self.receipts.confirm("owner", {
            "receipt_id": r1, "expected_version": 2, "raw_shop_name": "Apple Services", "raw_nip": None,
            "receipt_kind": "purchase", "receipt_date": "2026-11-18", "receipt_time": "12:05",
            "total_amount": "19.99", "currency": "PLN", "payment_method": "card", "lines": None, "link": None,
        })
        self.assertTrue(non_sensitive["receipt"]["linked"])
        self.assertEqual(non_sensitive["receipt"]["version"], 3)

    def test_allegro_three_sellers_payu_id_pin_fee_bridge_v1f_and_refund(self):
        self._add_tx(
            "payu-100", amount="100.00", booking_date="2026-11-20",
            remittance=["PAYU SA ALLEGRO ID 9988776611"], creditor="PayU S.A.",
        )
        s1 = self._add_receipt(shop_name="Sklep Alfa", receipt_date="2026-11-19", total_amount="30.00",
                               forwarded_sender_domain="allegro.pl", document_kind="receipt")
        s2 = self._add_receipt(shop_name="Sklep Beta", receipt_date="2026-11-20", total_amount="45.00",
                               forwarded_sender_domain="allegro.pl")
        s3 = self._add_receipt(shop_name="Sklep Gamma", receipt_date="2026-11-20", total_amount="22.50")
        # v1f gateway evidence attached to s1 does not block s1 from R, and stays excluded from R.
        attached_g2 = self._add_receipt(
            shop_name="Przelewy24", receipt_date="2026-11-19", total_amount="30.00",
            gateway="przelewy24", gateway_status="paid", evidence_for_receipt_id=s1, source_kind="email",
        )
        payu_ev = self._add_receipt(
            shop_name="PayU", receipt_date="2026-11-20", total_amount="100.00",
            gateway="payu", gateway_status="paid", fee_amount="2.50",
            gateway_ref="9988776611", source_kind="email",
        )

        # Imported anchor s1 also gets group_candidates while still imported, without appearing in 1:1 candidates[].
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, s1).status = "imported"
        imp_detail = self.receipts.detail("owner", s1)
        self.assertEqual(imp_detail["candidates"], [])
        self.assertTrue(any(
            item["match_strength"] == "id"
            and item["fee_amount"] == "2.5"
            and item["evidence_receipt_id"] == payu_ev
            and set(item["member_receipt_ids"]) == {s1, s2, s3}
            for item in imp_detail["group_candidates"]
        ))
        # Confirming s1 must not silently promote other members; explicit imported confirm is still required.
        with self.assertRaises(ReceiptError) as imp_link_err:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(imp_detail["group_candidates"][0]),
                "members": [{"receipt_id": rid, "expected_version": 1} for rid in (s1, s2, s3)],
                "evidence_receipt_id": payu_ev,
                "evidence_expected_version": 1,
                "fee_amount": "2.50",
            })
        self.assertEqual((imp_link_err.exception.status, imp_link_err.exception.code),
                         (400, "budget_receipt_invalid_request"))

        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, s1).status = "confirmed"
        prop = next(
            p for p in self.receipts.group_candidates("owner", {"receipt_id": s1})["proposals"]
            if p["evidence_receipt_id"] == payu_ev
        )
        self.assertEqual(prop["members_sum"], "97.5")
        linked = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(prop),
            "members": [{"receipt_id": rid, "expected_version": 1} for rid in (s1, s2, s3)],
            "evidence_receipt_id": payu_ev,
            "evidence_expected_version": 1,
            "fee_amount": "2.50",
        })
        self.assertEqual(linked["group"]["fee_amount"], "2.5")
        self.assertEqual(linked["evidence"]["receipt_id"], payu_ev)
        self.assertTrue(linked["evidence"]["linked"])
        self.assertNotIn(attached_g2, [m["receipt_id"] for m in linked["members"]])

        excl_ids = {r["receipt_id"] for r in self.receipts.list("owner", {"evidence": "exclude"})["receipts"]}
        only_ids = {r["receipt_id"] for r in self.receipts.list("owner", {"evidence": "only"})["receipts"]}
        self.assertNotIn(payu_ev, excl_ids)
        self.assertIn(payu_ev, only_ids)

        # Refund M:1 group (CRDT) and zero-fee ID-pinned standalone evidence.
        self._add_tx("crdt-ref", amount="60.00", direction="CRDT", booking_date="2026-11-21",
                     remittance=["ZWROT nr transakcji blik 5544332211 PAYU SA ID 5544332211"], debtor="Sklep Zwrot")
        ref1 = self._add_receipt(shop_name="Sklep Zwrot", kind="refund", receipt_date="2026-11-21",
                                 total_amount="25.00", blik_ref="5544332211")
        ref2 = self._add_receipt(shop_name="Sklep Zwrot", kind="refund", receipt_date="2026-11-21",
                                 total_amount="35.00")
        ev_zero = self._add_receipt(shop_name="PayU", kind="refund", receipt_date="2026-11-21",
                                    total_amount="60.00", gateway="payu", gateway_status="paid",
                                    fee_amount="0.00", gateway_ref="5544332211")
        ref_props = self.receipts.group_candidates("owner", {"receipt_id": ref1})["proposals"]
        self.assertEqual(ref_props[0]["match_strength"], "id")
        self.assertIsNone(ref_props[0]["fee_amount"])
        self.assertEqual(ref_props[0]["evidence_receipt_id"], ev_zero)
        ref_link = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(ref_props[0]),
            "members": [{"receipt_id": ref1, "expected_version": 1}, {"receipt_id": ref2, "expected_version": 1}],
            "evidence_receipt_id": ev_zero,
            "evidence_expected_version": 1,
            "fee_amount": "0.00",
        })
        self.assertIsNone(ref_link["group"]["fee_amount"])

    def test_card_scoping_domain_fallback_solver_caps_and_rejection_matrix(self):
        store_scoped = self._make_receipt_store(card_accounts={"1111": (self.acc_a,), "2222": (self.acc_2,)})
        self._add_tx("scoped-tx", account_id=self.acc_a, amount="50.00", booking_date="2026-11-18",
                     remittance=["SPLIT SHOP"])
        self._add_tx("scoped-tx-2", account_id=self.acc_2, amount="50.00", booking_date="2026-11-18",
                     remittance=["SPLIT SHOP"])
        c1 = self._add_receipt(shop_name="Split Shop", receipt_date="2026-11-18", total_amount="20.00", card_last4="1111")
        c2 = self._add_receipt(shop_name="Split Shop", receipt_date="2026-11-18", total_amount="30.00", card_last4="2222")
        c3 = self._add_receipt(shop_name="Split Shop", receipt_date="2026-11-18", total_amount="30.00", card_last4="1111")
        res_c1 = store_scoped.group_candidates("owner", {"receipt_id": c1})
        self.assertEqual(len(res_c1["proposals"]), 1)
        self.assertEqual(set(res_c1["proposals"][0]["member_receipt_ids"]), {c1, c3})
        # Trying to force c2 (card 2222) onto acc_a transaction is rejected even with valid bank token.
        with self.assertRaises(ReceiptError) as card_err:
            store_scoped.group_link("owner", {
                "link": self._link_from_candidate(res_c1["transactions"][0]),
                "members": [{"receipt_id": c1, "expected_version": 1}, {"receipt_id": c2, "expected_version": 1}],
            })
        self.assertEqual((card_err.exception.status, card_err.exception.code), (409, "budget_receipt_stale_source"))

        # Disjoint card store where anchor card_9999 maps to empty intersection.
        c9999 = self._add_receipt(shop_name="Split Shop", receipt_date="2026-11-18", total_amount="20.00", card_last4="9999")
        empty_scoped = self._make_receipt_store(card_accounts={"9999": ("nonexistent",)})
        self.assertEqual(empty_scoped.group_candidates("owner", {"receipt_id": c9999})["transactions"], [])

        # Ineligible anchor (cash) returns empty candidates safely.
        cash_r = self._add_receipt(shop_name="Cash Shop", payment_method="cash", total_amount="15.00")
        self.assertEqual(self.receipts.group_candidates("owner", {"receipt_id": cash_r})["proposals"], [])
        self.assertFalse(self.receipts.group_candidates("owner", {"receipt_id": cash_r})["no_bank_data"])

        # Span > 14 days (via invoice due_date extension reaching bank date while span between members is 15 days).
        self._add_tx("span-tx", account_id=self.acc_a, amount="40.00", booking_date="2026-11-25")
        inv_old = self._add_receipt(shop_name="Invoice Co", document_kind="invoice", receipt_date="2026-11-01",
                                    due_date="2026-11-24", total_amount="20.00")
        inv_new = self._add_receipt(shop_name="Invoice Co", document_kind="invoice", receipt_date="2026-11-22",
                                    due_date="2026-11-24", total_amount="20.00")
        span_cands = self.receipts.group_candidates("owner", {"receipt_id": inv_new})
        self.assertEqual(span_cands["proposals"], [])
        self.assertGreaterEqual(len(span_cands["transactions"]), 1)
        with self.assertRaises(ReceiptError) as span_err:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(span_cands["transactions"][0]),
                "members": [{"receipt_id": inv_old, "expected_version": 1},
                            {"receipt_id": inv_new, "expected_version": 1}],
            })
        self.assertEqual(span_err.exception.status, 400)

        # Caps: 12 candidate txs -> <= MAX_GROUP_TX_CANDIDATES (10); 26 peers -> pool capped at 24 & <= 5 proposals.
        cap_shop = self._add_shop(name="Cap Shop", chain_key="capshop")
        for idx in range(12):
            self._add_tx(f"cap-tx-{idx}", amount=f"{100 + idx}.00", booking_date="2026-11-10")
        cap_anchor = self._add_receipt(shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10", total_amount="10.00")
        for idx in range(26):
            self._add_receipt(shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10", total_amount=f"{10 + idx}.00")
        # Include invalid zero-date/zero-amount peer in DB to ensure pool builder skips non-positive/missing-date safely.
        self._add_receipt(shop_name="Cap Shop", shop_id=cap_shop, receipt_date=None, total_amount="10.00", status="imported")
        cap_res = self.receipts.group_candidates("owner", {"receipt_id": cap_anchor})
        self.assertLessEqual(len(cap_res["transactions"]), MAX_GROUP_TX_CANDIDATES)
        self.assertLessEqual(len(cap_res["proposals"]), MAX_GROUP_PROPOSALS)
        self.assertLessEqual(len(cap_res["eligible_receipts"]), MAX_GROUP_TX_CANDIDATES * MAX_GROUP_POOL + 1)

        # Rejection matrix for group_link and group_unlink / ReceiptGroupError / validation helpers.
        tx_cand = cap_res["transactions"][0]
        bad_err = ReceiptGroupError("unknown_code", 400)
        self.assertEqual(bad_err.code, "budget_receipt_invalid_request")
        gw_mem = self._add_receipt(shop_name="PayU", receipt_date="2026-11-10", total_amount="90.00", gateway="payu")
        ev_missing_fee = self._add_receipt(shop_name="PayU", receipt_date="2026-11-10", total_amount=tx_cand["amount"],
                                           gateway="payu", fee_amount="1.00")
        ev_bad_amount = self._add_receipt(shop_name="PayU", receipt_date="2026-11-10", total_amount="999.00", gateway="payu")
        non_gw_ev = self._add_receipt(shop_name="NotGW", receipt_date="2026-11-10", total_amount=tx_cand["amount"])
        needs_rev = self._add_receipt(shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10",
                                      total_amount="5.00", status="needs_review")
        fx_rec = self._add_receipt(shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10",
                                   total_amount="90.00", currency="EUR")
        wrong_kind = self._add_receipt(shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10",
                                       total_amount="90.00", kind="refund")
        matching_peer = self._add_receipt(
            shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10",
            total_amount=canonical_money(sum_money((tx_cand["amount"], negate_money("10.00")))),
        )

        for payload_bad, expected_status in (
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": gw_mem, "expected_version": 1}]}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": fx_rec, "expected_version": 1}]}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": wrong_kind, "expected_version": 1}]}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": needs_rev, "expected_version": 1}]}, 409),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": matching_peer, "expected_version": 1}],
              "fee_amount": "1.00"}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": matching_peer, "expected_version": 1}],
              "evidence_receipt_id": non_gw_ev, "evidence_expected_version": 1}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": matching_peer, "expected_version": 1}],
              "evidence_receipt_id": ev_bad_amount, "evidence_expected_version": 1}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": matching_peer, "expected_version": 1}],
              "evidence_receipt_id": ev_missing_fee, "evidence_expected_version": 1}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": cap_anchor, "expected_version": 1}]}, 400),
            ({"link": {**self._link_from_candidate(tx_cand), "account_id": self.acc_b},
              "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                          {"receipt_id": matching_peer, "expected_version": 1}]}, 400),
            ({"link": {**self._link_from_candidate(tx_cand), "entry_reference": "0" * 64},
              "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                          {"receipt_id": matching_peer, "expected_version": 1}]}, 400),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 99},
                                                                       {"receipt_id": matching_peer, "expected_version": 1}]}, 409),
            ({"link": self._link_from_candidate(tx_cand), "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                                                                       {"receipt_id": matching_peer, "expected_version": 1}],
              "evidence_receipt_id": ev_missing_fee, "evidence_expected_version": 99, "fee_amount": "1.00"}, 409),
        ):
            with self.subTest(payload=payload_bad), self.assertRaises(ReceiptError) as err:
                self.receipts.group_link("owner", payload_bad)
            self.assertEqual(err.exception.status, expected_status)

        wrong_sum_peer = self._add_receipt(
            shop_name="Cap Shop", shop_id=cap_shop, receipt_date="2026-11-10", total_amount="15.00",
        )
        with self.assertRaises(ReceiptGroupError) as sum_err:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(tx_cand),
                "members": [{"receipt_id": cap_anchor, "expected_version": 1},
                            {"receipt_id": wrong_sum_peer, "expected_version": 1}],
            })
        self.assertEqual(sum_err.exception.code, "budget_receipt_group_sum_mismatch")

    def test_eight_non_ascii_128_char_shops_and_reconcile_with_zero_1to1_links(self):
        # Owner has ONLY M:1 groups and zero 1:1 linked rows.
        self._add_tx("eight-128", amount="80.00", booking_date="2026-11-20",
                     remittance=["ZBIORCZY 8X128"], creditor="Sklep Zbiorczy")
        members = []
        for idx in range(8):
            base = f"ŻółćGęśląJaźńŁódźŚwiętochłowiceSklepNumer{idx}-"
            name = (base + ("ĄĆĘŁŃÓŚŹŻ" * 16))[:128]
            self.assertEqual(len(name), 128)
            rid = self._add_receipt(shop_name=name, receipt_date="2026-11-20", total_amount="10.00")
            members.append(rid)

        ev = self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="80.00",
                               gateway="payu", gateway_status="paid", source_kind="email")
        # Generate group candidate token on the 80.00 transaction.
        tx_cand = next(
            tx for tx in self.receipts.group_candidates("owner", {"receipt_id": members[0]})["transactions"]
            if tx["amount"] == "80"
        )
        linked = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(tx_cand),
            "members": [{"receipt_id": rid, "expected_version": 1} for rid in members],
            "evidence_receipt_id": ev,
            "evidence_expected_version": 1,
        })
        group_id = linked["group"]["group_id"]
        with self.sessions() as session:
            one_to_one = session.scalar(select(func.count()).select_from(db.BudgetReceipt).where(
                db.BudgetReceipt.owner == "owner", db.BudgetReceipt.linked_account_id.is_not(None),
            ))
            group_row = session.get(db.BudgetReceiptLinkGroup, group_id)
            self.assertEqual(one_to_one, 0)
            self.assertLessEqual(len(group_row.linked_evidence.encode("utf-8")), 4096)
            self.assertIn("members_digest", group_row.linked_evidence)

            # Reconcile is idempotent when tx is unchanged.
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 5), 0)

        # Drift bank tx via BudgetStore.commit_snapshot AND verify import_snapshot with zero 1:1 links.
        budget_store = BudgetStore(self.sessions, clock=lambda: self.now + 10)
        with self.sessions() as session:
            conn_id = self.config.connections[0].id
            conn_hashes = list(session.scalars(select(db.BudgetAccount.identification_hash).where(
                db.BudgetAccount.connection_id == conn_id, db.BudgetAccount.owner == "owner",
                db.BudgetAccount.enabled.is_(True), db.BudgetAccount.configured.is_(True),
            )))
        snap_accounts = tuple(
            SimpleNamespace(
                identification_hash=acc_hash,
                balances=(),
                transactions=(
                    api_fixture.transaction(
                        "eight-128", amount="80.01", booking_date="2026-11-20",
                        remittance_information=["ZBIORCZY CHANGED"],
                    ),
                ) if acc_hash == self.acc_a_hash else (),
            )
            for acc_hash in conn_hashes
        )
        budget_store.commit_snapshot(
            conn_id,
            SimpleNamespace(valid_until=self.now + 31536000, errors=(), accounts=snap_accounts),
        )
        with self.sessions() as session:
            group_row = session.get(db.BudgetReceiptLinkGroup, group_id)
            self.assertEqual((group_row.status, group_row.version), ("needs_review", 2))
            for rid in (*members, ev):
                rec = session.get(db.BudgetReceipt, rid)
                self.assertEqual((rec.status, rec.version), ("needs_review", 3))
            audits = list(session.scalars(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_type == "receipt_group",
                db.BudgetAnalyticsAudit.subject_id == group_id,
                db.BudgetAnalyticsAudit.action == "source_changed",
            )))
            self.assertEqual(len(audits), 1)
            self.assertNotIn("Żółć", audits[0].after_json)
            self.assertNotIn("eight-128", audits[0].after_json)
            # Replaying reconcile must be idempotent.
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 20), 0)
            self.assertEqual(session.get(db.BudgetReceiptLinkGroup, group_id).version, 2)

        # Stale CAS unlink fails cleanly with 409 budget_receipt_conflict.
        with self.assertRaises(ReceiptError) as stale_unlink:
            self.receipts.group_unlink("owner", {"group_id": group_id, "expected_version": 1})
        self.assertEqual((stale_unlink.exception.status, stale_unlink.exception.code), (409, "budget_receipt_conflict"))

        # Valid CAS unlink restores all 8 members + evidence to confirmed unlinked.
        unlinked = self.receipts.group_unlink("owner", {"group_id": group_id, "expected_version": 2})
        self.assertTrue(unlinked["ok"] and unlinked["unlinked"])
        self.assertEqual(len(unlinked["members"]), 8)
        self.assertTrue(all(m["status"] == "confirmed" and not m["linked"] and m["version"] == 4 for m in unlinked["members"]))
        self.assertEqual((unlinked["evidence"]["status"], unlinked["evidence"]["linked"], unlinked["evidence"]["version"]),
                         ("confirmed", False, 4))

        # Re-link and test disabled account + import_snapshot reconcile + AnalyticsError on invalid clock.
        budget_store.import_snapshot(
            self.config,
            SimpleNamespace(
                connection_id=self.config.connections[0].id,
                identification_hash=self.acc_a_hash,
                metadata=None,
                digest="imp-batch-1",
                fetched_at=self.now + 25,
                balances=None,
                transactions=(
                    api_fixture.transaction(
                        "imp-group-tx", amount="80.00", booking_date="2026-11-22",
                        remittance_information=["ZBIORCZY 8X128"],
                    ),
                ),
            ),
        )
        tx_cand2 = next(
            tx for tx in self.receipts.group_candidates("owner", {"receipt_id": members[0]})["transactions"]
            if tx["amount"] == "80"
        )
        relinked = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(tx_cand2),
            "members": [{"receipt_id": rid, "expected_version": 4} for rid in members],
        })
        with self.sessions.begin() as session:
            with self.assertRaises(AnalyticsError):
                reconcile_receipt_links(session, "owner", float("nan"))
            session.get(db.BudgetAccount, self.acc_a).enabled = False
            session.flush()
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 30), 1)
            self.assertEqual(session.get(db.BudgetReceiptLinkGroup, relinked["group"]["group_id"]).status, "needs_review")

    def test_transfer_exclusions_and_delete_dissolve_on_member_and_evidence(self):
        self._add_tx("tx-xfer-left", account_id=self.acc_a, amount="90.00", direction="DBIT", booking_date="2026-11-20")
        self._add_tx("tx-xfer-right", account_id=self.acc_2, amount="90.00", direction="CRDT", booking_date="2026-11-20")
        m1 = self._add_receipt(shop_name="Shop X", receipt_date="2026-11-20", total_amount="40.00")
        m2 = self._add_receipt(shop_name="Shop X", receipt_date="2026-11-20", total_amount="50.00")
        ev1 = self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="90.00", gateway="payu")
        tx_cand = next(tx for tx in self.receipts.group_candidates("owner", {"receipt_id": m1})["transactions"]
                       if tx["entry_reference"] != "0" * 64 and tx["amount"] == "90")
        linked = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(tx_cand),
            "members": [{"receipt_id": m1, "expected_version": 1}, {"receipt_id": m2, "expected_version": 1}],
            "evidence_receipt_id": ev1,
            "evidence_expected_version": 1,
        })
        group_id = linked["group"]["group_id"]

        debit_fp, credit_fp = self.pair_fps(debit=("a", "tx-xfer-left"), credit=("b", "tx-xfer-right"))

        # Confirmed group blocks transfer decision (409), with zero partial transfer rows.
        with self.assertRaises(AnalyticsError) as xfer_err1:
            self.store.decide_transfer(
                "owner",
                debit={"account_id": self.acc_a, "entry_reference": "tx-xfer-left"},
                credit={"account_id": self.acc_2, "entry_reference": "tx-xfer-right"},
                action="accept",
                expected_version=0,
                expected_debit_fingerprint=debit_fp,
                expected_credit_fingerprint=credit_fp,
            )
        self.assertEqual((xfer_err1.exception.status, xfer_err1.exception.code), (409, "budget_analytics_leg_reserved"))
        with self.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(db.BudgetTransferLeg)), 0)

        # Needs-review group ALSO blocks transfer decision (409).
        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptLinkGroup, group_id).status = "needs_review"
            session.get(db.BudgetReceiptLinkGroup, group_id).version = 2
        with self.assertRaises(AnalyticsError) as xfer_err2:
            self.store.decide_transfer(
                "owner",
                debit={"account_id": self.acc_a, "entry_reference": "tx-xfer-left"},
                credit={"account_id": self.acc_2, "entry_reference": "tx-xfer-right"},
                action="accept",
                expected_version=0,
                expected_debit_fingerprint=debit_fp,
                expected_credit_fingerprint=credit_fp,
            )
        self.assertEqual((xfer_err2.exception.status, xfer_err2.exception.code), (409, "budget_analytics_leg_reserved"))

        # Plain delete returns 409 budget_receipt_in_group; wrong group version returns 409 budget_receipt_conflict.
        with self.assertRaises(ReceiptError) as del_plain:
            self.receipts.delete("owner", {"receipt_id": m1, "expected_version": 2})
        self.assertEqual((del_plain.exception.status, del_plain.exception.code), (409, "budget_receipt_in_group"))
        with self.assertRaises(ReceiptError) as del_stale_grp:
            self.receipts.delete("owner", {
                "receipt_id": m1, "expected_version": 2, "dissolve_group": True, "expected_group_version": 1,
            })
        self.assertEqual((del_stale_grp.exception.status, del_stale_grp.exception.code), (409, "budget_receipt_conflict"))

        # Dissolve via member delete restores m2 + ev1 and removes m1 + group atomically.
        deleted = self.receipts.delete("owner", {
            "receipt_id": m1, "expected_version": 2, "dissolve_group": True, "expected_group_version": 2,
        })
        self.assertEqual(deleted, {"ok": True, "deleted": True})
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetReceipt, m1))
            self.assertIsNone(session.get(db.BudgetReceiptLinkGroup, group_id))
            self.assertEqual((session.get(db.BudgetReceipt, m2).status, session.get(db.BudgetReceipt, m2).version),
                             ("confirmed", 3))
            self.assertEqual((session.get(db.BudgetReceipt, ev1).status, session.get(db.BudgetReceipt, ev1).version),
                             ("confirmed", 3))

        # After dissolve, accepting transfer succeeds!
        xfer_ok = self.store.decide_transfer(
            "owner",
            debit={"account_id": self.acc_a, "entry_reference": "tx-xfer-left"},
            credit={"account_id": self.acc_2, "entry_reference": "tx-xfer-right"},
            action="accept",
            expected_version=0,
            expected_debit_fingerprint=debit_fp,
            expected_credit_fingerprint=credit_fp,
        )
        self.assertEqual(xfer_ok["status"], "accepted")

        # Attempting group_link on the transfer leg fails with 409 budget_receipt_tx_already_linked.
        self._add_tx("tx-after", account_id=self.acc_a, amount="90.00", booking_date="2026-11-21")
        ev2 = self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="90.00", gateway="payu")
        m3 = self._add_receipt(shop_name="Shop X", receipt_date="2026-11-20", total_amount="40.00")
        # Grab token before leg or reuse saved tx_cand against tx-xfer-left:
        with self.assertRaises(ReceiptError) as leg_err:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(tx_cand),
                "members": [{"receipt_id": m2, "expected_version": 3}, {"receipt_id": m3, "expected_version": 1}],
            })
        self.assertEqual((leg_err.exception.status, leg_err.exception.code), (409, "budget_receipt_tx_already_linked"))

        # Now test delete(dissolve_group=True) on evidence_receipt_id itself!
        tx2_cand = next(tx for tx in self.receipts.group_candidates("owner", {"receipt_id": m3})["transactions"]
                        if tx["amount"] == "90")
        linked2 = self.receipts.group_link("owner", {
            "link": self._link_from_candidate(tx2_cand),
            "members": [{"receipt_id": m2, "expected_version": 3}, {"receipt_id": m3, "expected_version": 1}],
            "evidence_receipt_id": ev2,
            "evidence_expected_version": 1,
        })
        with self.assertRaises(ReceiptError) as ev_del_plain:
            self.receipts.delete("owner", {"receipt_id": ev2, "expected_version": 2})
        self.assertEqual(ev_del_plain.exception.code, "budget_receipt_in_group")
        self.receipts.delete("owner", {
            "receipt_id": ev2, "expected_version": 2, "dissolve_group": True, "expected_group_version": 1,
        })
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetReceipt, ev2))
            self.assertIsNone(session.get(db.BudgetReceiptLinkGroup, linked2["group"]["group_id"]))
            self.assertEqual((session.get(db.BudgetReceipt, m2).status, session.get(db.BudgetReceipt, m2).version),
                             ("confirmed", 5))
            self.assertEqual((session.get(db.BudgetReceipt, m3).status, session.get(db.BudgetReceipt, m3).version),
                             ("confirmed", 3))

        # dissolve_group=True on an ungrouped receipt raises 409 budget_receipt_conflict.
        with self.assertRaises(ReceiptError) as not_grouped_dissolve:
            self.receipts.delete("owner", {
                "receipt_id": m3, "expected_version": 3, "dissolve_group": True, "expected_group_version": 1,
            })
        self.assertEqual((not_grouped_dissolve.exception.status, not_grouped_dissolve.exception.code),
                         (409, "budget_receipt_conflict"))

    def test_concurrent_sqlite_races_between_independent_sessions_and_previews(self):
        # Set up independent sessionmaker backed by the same SQLite DB file.
        _e2, sessions_b = db.init_db(self.path)
        store_a = self._make_receipt_store(sessions=self.sessions)
        store_b = self._make_receipt_store(sessions=sessions_b)
        analytics_b = BudgetAnalyticsStore(sessions_b, clock=lambda: self.now, cursor_secret=b"synthetic-only")

        def run_race(fn_a, fn_b):
            barrier = threading.Barrier(2)

            def call(fn):
                barrier.wait()
                try:
                    return True, fn()
                except Exception as exc:
                    return False, exc

            with ThreadPoolExecutor(max_workers=2) as pool:
                fut_a = pool.submit(call, fn_a)
                fut_b = pool.submit(call, fn_b)
                return fut_a.result(), fut_b.result()

        # 1. Group vs Group colliding on shared member across two different transactions & previews.
        self._add_tx("race-tx-1", amount="50.00", booking_date="2026-11-20")
        self._add_tx("race-tx-2", account_id=self.acc_2, amount="50.00", booking_date="2026-11-20")
        shared_m = self._add_receipt(shop_name="Race Shop", receipt_date="2026-11-20", total_amount="20.00")
        peer_a = self._add_receipt(shop_name="Race Shop", receipt_date="2026-11-20", total_amount="30.00")
        peer_b = self._add_receipt(shop_name="Race Shop", receipt_date="2026-11-20", total_amount="30.00")
        preview_1 = [tx for tx in store_a.group_candidates("owner", {"receipt_id": shared_m})["transactions"]
                     if tx["amount"] == "50"]
        preview_2 = [tx for tx in store_b.group_candidates("owner", {"receipt_id": shared_m})["transactions"]
                     if tx["amount"] == "50"]
        tx1 = next(tx for tx in preview_1 if tx["account_id"] == self.acc_a)
        tx2 = next(tx for tx in preview_2 if tx["account_id"] == self.acc_2)

        res1, res2 = run_race(
            lambda: store_a.group_link("owner", {
                "link": self._link_from_candidate(tx1),
                "members": [{"receipt_id": shared_m, "expected_version": 1}, {"receipt_id": peer_a, "expected_version": 1}],
            }),
            lambda: store_b.group_link("owner", {
                "link": self._link_from_candidate(tx2),
                "members": [{"receipt_id": shared_m, "expected_version": 1}, {"receipt_id": peer_b, "expected_version": 1}],
            }),
        )
        outcomes = [res1, res2]
        winners = [out for ok, out in outcomes if ok]
        losers = [out for ok, out in outcomes if not ok]
        self.assertEqual((len(winners), len(losers)), (1, 1))
        self.assertIsInstance(losers[0], ReceiptError)
        self.assertEqual(losers[0].status, 409)
        loser_peer = peer_b if res1[0] else peer_a
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, loser_peer).version, 1)
            loser_audits = session.scalar(select(func.count()).select_from(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_type == "receipt", db.BudgetAnalyticsAudit.subject_id == loser_peer,
            ))
            self.assertEqual(loser_audits, 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(db.BudgetReceiptLinkGroup)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(db.BudgetReceiptLinkMember)), 2)

        # 2. Group vs Group colliding on same bank transaction with completely disjoint members.
        self._add_tx("race-same-tx", amount="70.00", booking_date="2026-11-20")
        m_a1 = self._add_receipt(shop_name="Race Disjoint", receipt_date="2026-11-20", total_amount="30.00")
        m_a2 = self._add_receipt(shop_name="Race Disjoint", receipt_date="2026-11-20", total_amount="40.00")
        m_b1 = self._add_receipt(shop_name="Race Disjoint", receipt_date="2026-11-20", total_amount="35.00")
        m_b2 = self._add_receipt(shop_name="Race Disjoint", receipt_date="2026-11-20", total_amount="35.00")
        tok_a = next(tx for tx in store_a.group_candidates("owner", {"receipt_id": m_a1})["transactions"]
                     if tx["amount"] == "70")
        tok_b = next(tx for tx in store_b.group_candidates("owner", {"receipt_id": m_b1})["transactions"]
                     if tx["amount"] == "70")
        r_a, r_b = run_race(
            lambda: store_a.group_link("owner", {
                "link": self._link_from_candidate(tok_a),
                "members": [{"receipt_id": m_a1, "expected_version": 1}, {"receipt_id": m_a2, "expected_version": 1}],
            }),
            lambda: store_b.group_link("owner", {
                "link": self._link_from_candidate(tok_b),
                "members": [{"receipt_id": m_b1, "expected_version": 1}, {"receipt_id": m_b2, "expected_version": 1}],
            }),
        )
        self.assertEqual(sum(1 for ok, _ in (r_a, r_b) if ok), 1)
        loser_pair = (m_b1, m_b2) if r_a[0] else (m_a1, m_a2)
        with self.sessions() as session:
            for rid in loser_pair:
                self.assertEqual(session.get(db.BudgetReceipt, rid).version, 1)

        # 3. Group vs Group colliding on shared evidence_receipt_id across two disjoint transactions.
        self._add_tx("race-ev-tx1", amount="60.00", booking_date="2026-11-20")
        self._add_tx("race-ev-tx2", account_id=self.acc_2, amount="60.00", booking_date="2026-11-20")
        ev_shared = self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="60.00", gateway="payu")
        ev_m1 = self._add_receipt(shop_name="EvRace", receipt_date="2026-11-20", total_amount="25.00")
        ev_m2 = self._add_receipt(shop_name="EvRace", receipt_date="2026-11-20", total_amount="35.00")
        ev_n1 = self._add_receipt(shop_name="EvRace", receipt_date="2026-11-20", total_amount="20.00")
        ev_n2 = self._add_receipt(shop_name="EvRace", receipt_date="2026-11-20", total_amount="40.00")
        etx1 = next(tx for tx in store_a.group_candidates("owner", {"receipt_id": ev_m1})["transactions"]
                    if tx["amount"] == "60" and tx["account_id"] == self.acc_a)
        etx2 = next(tx for tx in store_b.group_candidates("owner", {"receipt_id": ev_n1})["transactions"]
                    if tx["amount"] == "60" and tx["account_id"] == self.acc_2)
        er1, er2 = run_race(
            lambda: store_a.group_link("owner", {
                "link": self._link_from_candidate(etx1),
                "members": [{"receipt_id": ev_m1, "expected_version": 1}, {"receipt_id": ev_m2, "expected_version": 1}],
                "evidence_receipt_id": ev_shared, "evidence_expected_version": 1,
            }),
            lambda: store_b.group_link("owner", {
                "link": self._link_from_candidate(etx2),
                "members": [{"receipt_id": ev_n1, "expected_version": 1}, {"receipt_id": ev_n2, "expected_version": 1}],
                "evidence_receipt_id": ev_shared, "evidence_expected_version": 1,
            }),
        )
        self.assertEqual(sum(1 for ok, _ in (er1, er2) if ok), 1)
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, ev_shared).version, 2)

        # 4. Group vs Legacy 1:1 confirm colliding on the same bank transaction.
        self._add_tx("race-1to1-vs-group", amount="45.00", booking_date="2026-11-20")
        solo = self._add_receipt(shop_name="Solo", receipt_date="2026-11-20", total_amount="45.00", status="draft")
        gm1 = self._add_receipt(shop_name="Grp", receipt_date="2026-11-20", total_amount="20.00")
        gm2 = self._add_receipt(shop_name="Grp", receipt_date="2026-11-20", total_amount="25.00")
        c1_tok = next(tx for tx in store_a.detail("owner", solo)["candidates"] if tx["amount"] == "45")
        cg_tok = next(tx for tx in store_b.group_candidates("owner", {"receipt_id": gm1})["transactions"]
                      if tx["amount"] == "45")
        c1_res, cg_res = run_race(
            lambda: store_a.confirm("owner", {
                "receipt_id": solo, "expected_version": 1, "raw_shop_name": "Solo", "raw_nip": None,
                "receipt_kind": "purchase", "receipt_date": "2026-11-20", "receipt_time": "12:00",
                "total_amount": "45.00", "currency": "PLN", "payment_method": "card", "lines": None,
                "link": {
                    "account_id": c1_tok["account_id"], "entry_reference": c1_tok["entry_reference"],
                    "candidate_token": c1_tok["candidate_token"],
                },
            }),
            lambda: store_b.group_link("owner", {
                "link": self._link_from_candidate(cg_tok),
                "members": [{"receipt_id": gm1, "expected_version": 1}, {"receipt_id": gm2, "expected_version": 1}],
            }),
        )
        self.assertEqual(sum(1 for ok, _ in (c1_res, cg_res) if ok), 1)
        loser_exc = cg_res[1] if c1_res[0] else c1_res[1]
        self.assertEqual(loser_exc.status, 409)

        # 5. Group vs Transfer decide colliding on the same bank transaction.
        self._add_tx("race-xfer-l", account_id=self.acc_a, amount="55.00", direction="DBIT", booking_date="2026-11-20")
        self._add_tx("race-xfer-r", account_id=self.acc_2, amount="55.00", direction="CRDT", booking_date="2026-11-20")
        xm1 = self._add_receipt(shop_name="XferShop", receipt_date="2026-11-20", total_amount="25.00")
        xm2 = self._add_receipt(shop_name="XferShop", receipt_date="2026-11-20", total_amount="30.00")
        xtok = next(tx for tx in store_a.group_candidates("owner", {"receipt_id": xm1})["transactions"]
                    if tx["amount"] == "55")
        race_debit_fp, race_credit_fp = self.pair_fps(debit=("a", "race-xfer-l"), credit=("b", "race-xfer-r"))
        gx_res, tx_res = run_race(
            lambda: store_a.group_link("owner", {
                "link": self._link_from_candidate(xtok),
                "members": [{"receipt_id": xm1, "expected_version": 1}, {"receipt_id": xm2, "expected_version": 1}],
            }),
            lambda: analytics_b.decide_transfer(
                "owner",
                debit={"account_id": self.acc_a, "entry_reference": "race-xfer-l"},
                credit={"account_id": self.acc_2, "entry_reference": "race-xfer-r"},
                action="accept",
                expected_version=0,
                expected_debit_fingerprint=race_debit_fp,
                expected_credit_fingerprint=race_credit_fp,
            ),
        )
        self.assertEqual(sum(1 for ok, _ in (gx_res, tx_res) if ok), 1)

    def test_cross_purpose_tokens_device_token_blocks_and_edge_branches(self):
        # Cross-purpose tokens: 1:1 token rejected by group_link, group token rejected by 1:1 confirm.
        self._add_tx("purpose-tx", amount="50.00", booking_date="2026-11-20")
        draft_1 = self._add_receipt(shop_name="Purpose Shop", receipt_date="2026-11-20", total_amount="50.00", status="draft")
        m1 = self._add_receipt(shop_name="Purpose Shop", receipt_date="2026-11-20", total_amount="20.00")
        m2 = self._add_receipt(shop_name="Purpose Shop", receipt_date="2026-11-20", total_amount="30.00")
        one_cand = next(tx for tx in self.receipts.detail("owner", draft_1)["candidates"] if tx["amount"] == "50")
        grp_cand = next(tx for tx in self.receipts.group_candidates("owner", {"receipt_id": m1})["transactions"]
                        if tx["amount"] == "50")
        with self.assertRaises(ReceiptError) as err1:
            self.receipts.group_link("owner", {
                "link": {
                    "account_id": one_cand["account_id"], "entry_reference": one_cand["entry_reference"],
                    "candidate_token": one_cand["candidate_token"],
                },
                "members": [{"receipt_id": m1, "expected_version": 1}, {"receipt_id": m2, "expected_version": 1}],
            })
        self.assertEqual(err1.exception.status, 400)
        with self.assertRaises(ReceiptError) as err2:
            self.receipts.confirm("owner", {
                "receipt_id": draft_1, "expected_version": 1, "raw_shop_name": "Purpose Shop", "raw_nip": None,
                "receipt_kind": "purchase", "receipt_date": "2026-11-20", "receipt_time": "12:00",
                "total_amount": "50.00", "currency": "PLN", "payment_method": "card", "lines": None,
                "link": self._link_from_candidate(grp_cand),
            })
        self.assertEqual(err2.exception.status, 400)

        # Device token store cannot invoke group_link or group_unlink directly.
        dev_store = self._make_receipt_store(dev_id="dev-1")
        with self.assertRaises(device_token.DeviceTokenError) as dev_link:
            dev_store.group_link("owner", {})
        self.assertEqual(dev_link.exception.status, 403)
        with self.assertRaises(device_token.DeviceTokenError) as dev_unlink:
            dev_store.group_unlink("owner", {})
        self.assertEqual(dev_unlink.exception.status, 403)

        # Simulated DB IntegrityError on group flush maps to 409 budget_receipt_tx_already_linked.
        real_flush = Session.flush

        def fail_on_group_flush(session, *args, **kwargs):
            if any(isinstance(obj, db.BudgetReceiptLinkGroup) for obj in session.new):
                raise IntegrityError("stmt", "params", "orig")
            return real_flush(session, *args, **kwargs)

        with mock.patch.object(Session, "flush", autospec=True, side_effect=fail_on_group_flush):
            with self.assertRaises(ReceiptError) as db_race:
                self.receipts.group_link("owner", {
                    "link": self._link_from_candidate(grp_cand),
                    "members": [{"receipt_id": m1, "expected_version": 1}, {"receipt_id": m2, "expected_version": 1}],
                })
        self.assertEqual((db_race.exception.status, db_race.exception.code), (409, "budget_receipt_tx_already_linked"))

    def test_coverage_branches_for_validation_scoring_reconcile_and_permissions(self):
        # 1. Corrupt/non-positive/invalid amounts, corrupt tx JSON, non-list remittance, NIP/generic scoring.
        self._add_tx("nip-tx", amount="70.00", booking_date="2026-11-20",
                     remittance=["NIP 5260250995 ZAKUP"], creditor="Sklep NIP")
        self._add_tx("generic-tx", amount="5.00", booking_date="2026-11-20",
                     remittance=["SKLEP WARSZAWA"], creditor="Warszawa")
        self._add_tx("generic-ok-tx", amount="70.00", booking_date="2026-11-20",
                     remittance=["WARSZAWA"], creditor="Warszawa")
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(
                account_id=self.acc_a, entry_reference="corrupt-tx", payload_json="{not-json",
                observed_at=self.now, uncertain=False,
            ))
            session.add(db.BudgetTransaction(
                account_id=self.acc_a, entry_reference="bad-remit-tx",
                payload_json=json.dumps({
                    "entry_reference": "bad-remit-tx", "amount": "70.00", "currency": "PLN",
                    "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-20",
                    "remittance_information": "not-a-list",
                }),
                observed_at=self.now, uncertain=False,
            ))

        nip_r1 = self._add_receipt(shop_name="Mega Firma", nip="5260250995", total_amount="30.00")
        nip_r2 = self._add_receipt(shop_name="Mega Firma", nip="5260250995", total_amount="40.00")
        gen_r1 = self._add_receipt(shop_name="Warszawa", total_amount="30.00")
        gen_r2 = self._add_receipt(shop_name="Warszawa", total_amount="40.00")
        # Peers hitting edge branches: non-positive, invalid money, evidence scoped/window/fee edge cases.
        bad_money_r = self._add_receipt(shop_name="Mega Firma", nip="5260250995", total_amount="bad-num", status="imported")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, bad_money_r).total_amount = "0.00"
        inv_money_r = self._add_receipt(shop_name="Mega Firma", nip="5260250995", total_amount="1.00", status="imported")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, inv_money_r).total_amount = "not-money"
        # Tx-hit peer with different shop domain triggers rank 1 tx_hit path in _group_peer_rank.
        hit_peer = self._add_receipt(shop_name="Mega Firma", nip=None, total_amount="40.00")
        # Invoice span>14 + ev card/date edge cases + ev fee reducing target <= anchor_total.
        self._add_receipt(shop_name="PayU", receipt_date="2026-11-10", total_amount="70.00", gateway="payu")
        self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="70.00", gateway="payu",
                          card_last4="2222")
        self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="70.00", gateway="payu",
                          fee_amount="50.00")
        scoped = self._make_receipt_store(card_accounts={"2222": (self.acc_2,)})
        nip_cands = scoped.group_candidates("owner", {"receipt_id": nip_r1})
        self.assertTrue(any(t["match_strength"] == "nip" for t in nip_cands["transactions"]))
        gen_cands = scoped.group_candidates("owner", {"receipt_id": gen_r1})
        self.assertTrue(any(t["match_strength"] == "generic" for t in gen_cands["transactions"]))
        # Trigger invoice + ev span > 14 in solver is_group_valid across three dates (Nov 01, Nov 10, Nov 20).
        self._add_tx("ev-edge-tx", amount="70.00", booking_date="2026-11-18")
        inv_anchor = self._add_receipt(shop_name="SpanInv", document_kind="invoice", receipt_date="2026-11-10",
                                       due_date="2026-11-25", total_amount="30.00")
        self._add_receipt(shop_name="SpanInv", document_kind="invoice", receipt_date="2026-11-01",
                          due_date="2026-11-25", total_amount="20.00")
        self._add_receipt(shop_name="SpanInv", document_kind="invoice", receipt_date="2026-11-20",
                          due_date="2026-11-25", total_amount="20.00")
        self._add_receipt(shop_name="PayU", receipt_date="2026-11-18", total_amount="70.00",
                          gateway="payu", fee_amount="2.00")
        self.assertEqual(scoped.group_candidates("owner", {"receipt_id": inv_anchor})["proposals"], [])
        self.assertIn(hit_peer, {r["receipt_id"] for r in nip_cands["eligible_receipts"]} | {hit_peer})

        # Unrelated domain/shop anchor+peer both hitting bank remittance directly (line 1613) + null total_amount (_positive_money line 364).
        self._add_tx("multi-hit-tx", amount="60.00", booking_date="2026-11-20",
                     remittance=["OMEGAA OMEGAB PAYMENT"], creditor="Omegaa Omegab")
        none_amt = self._add_receipt(shop_name="Omegaa", total_amount=None, status="imported")
        self.assertEqual(self.receipts.group_candidates("owner", {"receipt_id": none_amt})["proposals"], [])
        mh1 = self._add_receipt(shop_name="Omegaa", total_amount="25.00")
        mh2 = self._add_receipt(shop_name="Omegab", total_amount="35.00")
        mh_res = self.receipts.group_candidates("owner", {"receipt_id": mh1})
        self.assertTrue(any(set(p["member_receipt_ids"]) == {mh1, mh2} for p in mh_res["proposals"]))

        # 2. Direct validation helpers in group_candidates / group_link / group_unlink / delete.
        tok = next(t for t in nip_cands["transactions"] if t["amount"] == "70")
        for bad_call in (
            lambda: self.receipts.group_candidates("owner", "not-dict"),
            lambda: self.receipts.group_candidates("owner", {"receipt_id": 123}),
            lambda: self.receipts.group_link("owner", "bad"),
            lambda: self.receipts.group_link("owner", {"link": {}, "members": [], "extra": 1}),
            lambda: self.receipts.group_link("owner", {"link": "bad", "members": [{}, {}]}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [{}]}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [
                {"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 0},
            ]}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [
                {"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 1},
            ], "evidence_receipt_id": nip_r1, "evidence_expected_version": 1}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [
                {"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 1},
            ], "evidence_receipt_id": "x"}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [
                {"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 1},
            ], "fee_amount": "not-money"}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [
                {"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 1},
            ], "fee_amount": "-1"}),
            lambda: self.receipts.group_link("owner", {"link": self._link_from_candidate(tok), "members": [
                {"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 1},
            ], "fee_amount": "x" * 81}),
            lambda: self.receipts.group_unlink("owner", {"group_id": "bad", "expected_version": 0}),
            lambda: self.receipts.group_unlink("owner", {"group_id": "not-uuid", "expected_version": 1}),
            lambda: self.receipts.group_unlink("owner", {"group_id": _uuid(), "expected_version": 1}),
            lambda: self.receipts.delete("owner", {"receipt_id": nip_r1, "expected_version": 0}),
            lambda: self.receipts.delete("owner", {
                "receipt_id": nip_r1, "expected_version": 1, "dissolve_group": "yes",
            }),
            lambda: self.receipts.delete("owner", {
                "receipt_id": nip_r1, "expected_version": 1, "dissolve_group": True, "expected_group_version": 0,
            }),
            lambda: self.receipts.delete("owner", {
                "receipt_id": nip_r1, "expected_version": 1, "dissolve_group": False, "expected_group_version": 1,
            }),
        ):
            with self.assertRaises(ReceiptError):
                bad_call()

        # Unknown receipt in delete -> 404, bad group_id in group_detail -> 404, 1:1 linked receipt in group_link -> 409.
        with self.assertRaises(ReceiptError):
            self.receipts.group_detail("owner", "not-uuid")
        with self.sessions.begin() as session:
            rec_1to1 = session.get(db.BudgetReceipt, nip_r1)
            rec_1to1.linked_account_id = self.acc_a
            rec_1to1.linked_entry_ref = "generic-tx"
            rec_1to1.linked_fingerprint = "a" * 64
            rec_1to1.linked_evidence = "{}"
        with self.assertRaises(ReceiptError) as already_linked_mem:
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(tok),
                "members": [{"receipt_id": nip_r1, "expected_version": 1}, {"receipt_id": nip_r2, "expected_version": 1}],
            })
        self.assertEqual(already_linked_mem.exception.code, "budget_receipt_tx_already_linked")

        # 3. Corrupted DB tx JSON & already needs_review member during group reconciliation + actor audit + ev negative fee.
        ev_neg_fee = self._add_receipt(shop_name="PayU", receipt_date="2026-11-20", total_amount="70.00", gateway="payu")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, ev_neg_fee).fee_amount = "not-money"
        with self.assertRaises(ReceiptGroupError):
            self.receipts.group_link("owner", {
                "link": self._link_from_candidate(tok),
                "members": [{"receipt_id": gen_r1, "expected_version": 1}, {"receipt_id": gen_r2, "expected_version": 1}],
                "evidence_receipt_id": ev_neg_fee, "evidence_expected_version": 1,
            })

        actor_store = self._make_receipt_store(actor="delegate-admin")
        linked = actor_store.group_link("owner", {
            "link": self._link_from_candidate(tok),
            "members": [{"receipt_id": gen_r1, "expected_version": 1}, {"receipt_id": gen_r2, "expected_version": 1}],
        })
        gid = linked["group"]["group_id"]
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, gen_r1).status = "needs_review"
            session.get(db.BudgetReceipt, gen_r1).total_amount = "not-money"
            self.assertEqual(reconcile_receipt_links(session, "owner", self.now + 50), 2)
            self.assertEqual(session.get(db.BudgetReceiptLinkGroup, gid).status, "needs_review")
            session.get(db.BudgetReceipt, gen_r1).total_amount = "30.00"
        actor_store.group_unlink("owner", {"group_id": gid, "expected_version": 2})

        # 4. Mid-detail permission loss + group_unlink permission check (requires edit without match).
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            session.query(db.PermissionGrant).filter(
                db.PermissionGrant.username == "owner",
                db.PermissionGrant.permission_id == "budget.receipts.match",
            ).delete()
        with self.assertRaises(device_token.DeviceTokenError):
            self.receipts.group_unlink("owner", {"group_id": _uuid(), "expected_version": 1})
        # Simulate match permission allowed in first tx of detail() and withdrawn before second _read tx.
        calls = {"count": 0}
        orig_owner = self.receipts._owner
        def flip_match(sess, owner_arg, perm=None, **kw):
            res = orig_owner(sess, owner_arg, perm, **kw)
            calls["count"] += 1
            if calls["count"] == 1:
                self.receipts._match_allowed = True
            else:
                self.receipts._match_allowed = False
            return res
        with mock.patch.object(self.receipts, "_owner", side_effect=flip_match):
            det_flipped = self.receipts.detail("owner", gen_r1)
            self.assertNotIn("group_candidates", det_flipped)


if __name__ == "__main__":
    unittest.main()
