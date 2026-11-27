"""Tests for 0.60.0 receipt↔transaction link indicators (Miesiąc & Paragony)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from sqlalchemy import event

from hestia import api_contract, authz, db, store_sql
from hestia.budget import analytics_api, device_token, receipt_api
from hestia.budget.analytics_views import BudgetAnalyticsViews, clear_projection_cache
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_api as fixture
from tests.test_budget_analytics_store import AnalyticsCase

_HEADER = "X-Hestia-Contract-Version"
_V60 = "0.60.0"
_V59 = "0.59.0"

_R_1TO1 = "30000000-0000-4000-8000-000000000001"
_R_SHOP_ONLY = "30000000-0000-4000-8000-000000000002"
_R_DOMAIN_ONLY = "30000000-0000-4000-8000-000000000003"
_R_GRP_1 = "30000000-0000-4000-8000-000000000011"
_R_GRP_2 = "30000000-0000-4000-8000-000000000012"
_R_GRP_3 = "30000000-0000-4000-8000-000000000013"
_R_GRP_EV = "30000000-0000-4000-8000-000000000014"
_R_GRP_NONAME_1 = "30000000-0000-4000-8000-000000000021"
_R_GRP_NONAME_2 = "30000000-0000-4000-8000-000000000022"
_R_GRP_SOLO = "30000000-0000-4000-8000-000000000031"
_R_UNLINKED = "30000000-0000-4000-8000-000000000099"
_GRP_APPLE = "40000000-0000-4000-8000-000000000001"
_GRP_NONAME = "40000000-0000-4000-8000-000000000002"
_GRP_SOLO = "40000000-0000-4000-8000-000000000003"
_SHOP_ID = "50000000-0000-4000-8000-000000000001"

_SECRET_CREDITOR = "PRIVATE-CREDITOR-NAME-DO-NOT-LEAK"
_SECRET_REMITTANCE = "PRIVATE-REMITTANCE-TEXT-DO-NOT-LEAK"


def _tx_payload(amount, booking_date, *, value_date=None):
    return json.dumps({
        "amount": amount,
        "currency": "PLN",
        "credit_debit_indicator": "DBIT",
        "status": "BOOK",
        "booking_date": booking_date,
        "value_date": value_date,
        "transaction_date": None,
        "remittance_information": [_SECRET_REMITTANCE],
    })


def _seed_links(session, owner, account_a, account_b, now):
    session.add(db.BudgetAccountDisplay(
        account_id=account_a, product="eKonto", currency="PLN", masked_suffix="1234",
        observed_at=now, imported_at=now, digest="disp-a",
    ))
    session.add(db.BudgetAccountDisplay(
        account_id=account_b, product="Bank B Pro", currency="PLN", masked_suffix=None,
        observed_at=now, imported_at=now, digest="disp-b",
    ))
    session.add(db.BudgetShop(
        id=_SHOP_ID, owner=owner, name="Apteka Centralna", version=1, created_at=now, updated_at=now,
    ))
    txs = (
        (account_a, "tx-1to1", "42.50", "2026-11-10", None),
        (account_b, "tx-shop", "19.99", None, "2026-11-08"),
        (account_a, "tx-domain", "25.00", "2026-11-07", None),
        (account_a, "tx-grp-apple", "60.00", "2026-11-11", None),
        (account_a, "tx-grp-noname", "30.00", "2026-11-06", None),
        (account_a, "tx-grp-solo", "12.00", "2026-11-05", None),
        (account_a, "tx-unlinked", "15.00", "2026-11-12", None),
    )
    for acct, ref, amt, bdate, vdate in txs:
        session.add(db.BudgetTransaction(
            account_id=acct, entry_reference=ref,
            payload_json=_tx_payload(amt, bdate, value_date=vdate),
            observed_at=now, uncertain=False,
        ))
    session.flush()
    for acct, ref, *_ in txs:
        session.add(db.BudgetTransactionMetadata(
            account_id=acct, entry_reference=ref, owner=owner,
            ledger_fingerprint="fp-" + ref,
            payload_json=json.dumps({"creditor_name": _SECRET_CREDITOR}),
            source="api", observed_at=now, version=1,
        ))
    session.add_all((
        db.BudgetReceipt(
            id=_R_1TO1, owner=owner, raw_shop_name="Biedronka",
            receipt_date="2026-11-10", total_amount="42.50", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email",
            linked_account_id=account_a, linked_entry_ref="tx-1to1",
            linked_fingerprint="fp1", linked_evidence="{}", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_SHOP_ONLY, owner=owner, shop_id=_SHOP_ID, raw_shop_name=None,
            receipt_date="2026-11-08", total_amount="19.99", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email",
            linked_account_id=account_b, linked_entry_ref="tx-shop",
            linked_fingerprint="fp2", linked_evidence="{}", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_DOMAIN_ONLY, owner=owner, raw_shop_name=None,
            forwarded_manually=1, forwarded_sender_domain="shop.example",
            receipt_date="2026-11-07", total_amount="25.00", currency="PLN", payment_method="card",
            status="needs_review", source_kind="email",
            linked_account_id=account_a, linked_entry_ref="tx-domain",
            linked_fingerprint="fp3", linked_evidence="{}", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_1, owner=owner, raw_shop_name="Apple",
            receipt_date="2026-11-11", total_amount="10.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_2, owner=owner, raw_shop_name="Apple",
            receipt_date="2026-11-10", total_amount="20.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_3, owner=owner, raw_shop_name="Apple",
            receipt_date="2026-11-09", total_amount="30.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_EV, owner=owner, raw_shop_name="PayU",
            receipt_date="2026-11-11", total_amount="60.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", document_kind="payment_confirmation",
            gateway="PayU", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_NONAME_1, owner=owner, raw_shop_name=None,
            receipt_date="2026-11-06", total_amount="15.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_NONAME_2, owner=owner, raw_shop_name=None,
            receipt_date="2026-11-05", total_amount="15.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_GRP_SOLO, owner=owner, raw_shop_name="SoloShop",
            receipt_date="2026-11-05", total_amount="12.00", currency="PLN", payment_method="card",
            status="confirmed", source_kind="email", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceipt(
            id=_R_UNLINKED, owner=owner, raw_shop_name="Lidl",
            receipt_date="2026-11-12", total_amount="15.00", currency="PLN", payment_method="cash",
            status="confirmed", source_kind="paste", version=1, created_at=now, updated_at=now,
        ),
    ))
    session.flush()
    session.add_all((
        db.BudgetReceiptLinkGroup(
            id=_GRP_APPLE, owner=owner, account_id=account_a, entry_reference="tx-grp-apple",
            currency="PLN", tx_amount="60.00", credit_debit_indicator="DBIT", status="confirmed",
            evidence_receipt_id=_R_GRP_EV,
            linked_fingerprint="fpg1", linked_evidence="{}", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceiptLinkGroup(
            id=_GRP_NONAME, owner=owner, account_id=account_a, entry_reference="tx-grp-noname",
            currency="PLN", tx_amount="30.00", credit_debit_indicator="DBIT", status="needs_review",
            linked_fingerprint="fpg2", linked_evidence="{}", version=1, created_at=now, updated_at=now,
        ),
        db.BudgetReceiptLinkGroup(
            id=_GRP_SOLO, owner=owner, account_id=account_a, entry_reference="tx-grp-solo",
            currency="PLN", tx_amount="12.00", credit_debit_indicator="DBIT", status="confirmed",
            linked_fingerprint="fpg3", linked_evidence="{}", version=1, created_at=now, updated_at=now,
        ),
    ))
    session.flush()
    session.add_all((
        db.BudgetReceiptLinkMember(group_id=_GRP_APPLE, receipt_id=_R_GRP_1, owner=owner),
        db.BudgetReceiptLinkMember(group_id=_GRP_APPLE, receipt_id=_R_GRP_2, owner=owner),
        db.BudgetReceiptLinkMember(group_id=_GRP_APPLE, receipt_id=_R_GRP_3, owner=owner),
        db.BudgetReceiptLinkMember(group_id=_GRP_NONAME, receipt_id=_R_GRP_NONAME_1, owner=owner),
        db.BudgetReceiptLinkMember(group_id=_GRP_NONAME, receipt_id=_R_GRP_NONAME_2, owner=owner),
        db.BudgetReceiptLinkMember(group_id=_GRP_SOLO, receipt_id=_R_GRP_SOLO, owner=owner),
    ))


class LinkIndicatorsStoreTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.views = BudgetAnalyticsViews(self.sessions, cursor_secret=b"synthetic-secret")
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-secret")
        with self.sessions.begin() as session:
            acct_a = session.get(db.BudgetAccount, self.accounts["a"])
            conn_a = session.get(db.BudgetConnection, acct_a.connection_id)
            conn_a.bank_name = "Bank A"
            session.add(db.BudgetConnection(
                id="bank-b", owner="owner", application_id="app", bank_name="Bank B",
                country="PL", enabled=True, next_attempt=0, failures=0,
            ))
            session.flush()
            session.get(db.BudgetAccount, self.accounts["b"]).connection_id = "bank-b"
            session.flush()
            _seed_links(session, "owner", self.accounts["a"], self.accounts["b"], self.now)
        clear_projection_cache()

    def test_transactions_include_receipts_1to1_group_unlinked_and_single_join(self):
        old_resp = self.views.transactions("owner", "2026-11")
        self.assertGreaterEqual(len(old_resp["transactions"]), 7)
        for row in old_resp["transactions"]:
            self.assertNotIn("receipt", row)

        queries = []

        def on_sql(_conn, _cursor, statement, _params, *_args):
            if "budget_receipts" in statement:
                queries.append(statement)

        event.listen(self.engine, "before_cursor_execute", on_sql)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", on_sql)

        new_resp = self.views.transactions("owner", "2026-11", include_receipts=True)
        api_contract.BudgetAnalyticsTransactions.model_validate(new_resp)
        self.assertEqual(len(queries), 1)
        by_ref = {row["entry_reference"]: row["receipt"] for row in new_resp["transactions"]}
        self.assertEqual(by_ref["tx-1to1"], {
            "receipt_id": _R_1TO1, "shop_name": "Biedronka", "total": "42.50",
            "currency": "PLN", "status": "confirmed",
        })
        self.assertEqual(by_ref["tx-shop"], {
            "receipt_id": _R_SHOP_ONLY, "shop_name": "Apteka Centralna", "total": "19.99",
            "currency": "PLN", "status": "confirmed",
        })
        self.assertEqual(by_ref["tx-domain"], {
            "receipt_id": _R_DOMAIN_ONLY, "shop_name": "shop.example", "total": "25.00",
            "currency": "PLN", "status": "needs_review",
        })
        self.assertEqual(by_ref["tx-grp-apple"], {
            "receipt_id": _R_GRP_1, "shop_name": "Apple +2", "total": "60.00",
            "currency": "PLN", "status": "confirmed",
        })
        self.assertEqual(by_ref["tx-grp-noname"], {
            "receipt_id": _R_GRP_NONAME_1, "shop_name": "+1", "total": "30.00",
            "currency": "PLN", "status": "needs_review",
        })
        self.assertEqual(by_ref["tx-grp-solo"], {
            "receipt_id": _R_GRP_SOLO, "shop_name": "SoloShop", "total": "12.00",
            "currency": "PLN", "status": "confirmed",
        })
        self.assertIsNone(by_ref["tx-unlinked"])

        # Empty page skips receipt join cleanly.
        empty = self.views.transactions("owner", "2026-11", currency="EUR", include_receipts=True)
        self.assertEqual(empty["transactions"], [])
        self.assertEqual(len(queries), 1)

        # transactions_by_keys honours include_receipts too.
        key = f"{self.accounts['a']}/tx-grp-apple"
        by_keys_old = self.views.transactions_by_keys("owner", row_keys=[key])
        self.assertNotIn("receipt", by_keys_old["transactions"][0])
        by_keys_new = self.views.transactions_by_keys("owner", row_keys=[key], include_receipts=True)
        self.assertEqual(by_keys_new["transactions"][0]["receipt"]["shop_name"], "Apple +2")

    def test_receipts_list_and_search_linked_transaction_1to1_group_unlinked_and_privacy(self):
        tx_queries = []

        def on_sql(_conn, _cursor, statement, _params, *_args):
            if "budget_transactions" in statement:
                tx_queries.append(statement)

        event.listen(self.engine, "before_cursor_execute", on_sql)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", on_sql)

        listed = self.receipts.list("owner", {"evidence": "include"})
        api_contract.BudgetReceiptListResult.model_validate(listed)
        self.assertEqual(len(tx_queries), 1)
        by_id = {row["receipt_id"]: row["linked_transaction"] for row in listed["receipts"]}
        self.assertEqual(by_id[_R_1TO1], {
            "account_label": "Bank A · eKonto ••1234",
            "booking_date": "2026-11-10",
            "amount": "42.50",
            "currency": "PLN",
        })
        # Product containing bank_name does not duplicate bank_name; null booking_date falls back to value_date.
        self.assertEqual(by_id[_R_SHOP_ONLY], {
            "account_label": "Bank B Pro",
            "booking_date": "2026-11-08",
            "amount": "19.99",
            "currency": "PLN",
        })
        for rid in (_R_GRP_1, _R_GRP_2, _R_GRP_3, _R_GRP_EV):
            self.assertEqual(by_id[rid], {
                "account_label": "Bank A · eKonto ••1234",
                "booking_date": "2026-11-11",
                "amount": "60.00",
                "currency": "PLN",
            })
        self.assertIsNone(by_id[_R_UNLINKED])

        # Account with no BudgetAccountDisplay falls back to bank_name alone.
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetAccountDisplay, self.accounts["b"]))
        searched = self.receipts.search("owner", {"query": "Apteka"})
        # Search matches raw_shop_name/raw_nip/lines; verify search returns linked_transaction on Biedronka.
        searched_b = self.receipts.search("owner", {"query": "Biedronka"})
        api_contract.BudgetReceiptListResult.model_validate(searched_b)
        self.assertEqual(searched_b["receipts"][0]["linked_transaction"]["account_label"], "Bank A · eKonto ••1234")
        listed_no_disp = self.receipts.list("owner", {})
        by_id2 = {row["receipt_id"]: row["linked_transaction"] for row in listed_no_disp["receipts"]}
        self.assertEqual(by_id2[_R_SHOP_ONLY]["account_label"], "Bank B")
        self.assertEqual(searched["receipts"], [])

        wire = json.dumps(listed) + json.dumps(searched_b)
        self.assertNotIn(_SECRET_CREDITOR, wire)
        self.assertNotIn(_SECRET_REMITTANCE, wire)

    def test_explain_query_plan_uses_indexes_on_both_sides(self):
        captured = {"tx": None, "rcpt": None}

        def listener(_conn, _cursor, statement, params, *_args):
            if "LEFT OUTER JOIN budget_receipts" in statement:
                captured["tx"] = (statement, params)
            elif "LEFT OUTER JOIN budget_transactions" in statement:
                captured["rcpt"] = (statement, params)

        event.listen(self.engine, "before_cursor_execute", listener)
        try:
            self.views.transactions("owner", "2026-11", include_receipts=True)
            self.receipts.list("owner", {})
        finally:
            event.remove(self.engine, "before_cursor_execute", listener)

        self.assertIsNotNone(captured["tx"])
        self.assertIsNotNone(captured["rcpt"])
        with self.engine.connect() as conn:
            tx_plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + captured["tx"][0], captured["tx"][1]).fetchall()
            rcpt_plan = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + captured["rcpt"][0], captured["rcpt"][1]).fetchall()
        tx_details = "\n".join(row[3] for row in tx_plan)
        rcpt_details = "\n".join(row[3] for row in rcpt_plan)
        self.assertIn("uq_budget_receipts_tx_link", tx_details)
        self.assertIn("ix_budget_receipt_link_members_group", tx_details)
        self.assertNotIn("SCAN budget_", tx_details)
        self.assertIn("ix_budget_receipts_owner_date", rcpt_details)
        self.assertIn("uq_budget_receipt_link_groups_evidence", rcpt_details)
        self.assertNotIn("SCAN budget_", rcpt_details)

    def test_capability_and_device_token_gating_in_stores(self):
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            session.add(db.User(username="delegate", password_hash="x", role="viewer"))
            session.flush()
            session.add(db.PermissionGrant(username="delegate", permission_id="budget.receipts.view", target_owner="owner"))
            session.add_all((
                db.BudgetReceiptDeviceToken(
                    id="dev-view-only", owner="owner", actor="owner", device_name="ViewOnly",
                    token_hmac="1" * 64, issued_permissions=json.dumps(["budget.view"]),
                    created_at=self.now, expires_at=self.now + 60, hard_expires_at=self.now + 120,
                    sliding_written_at=self.now,
                ),
                db.BudgetReceiptDeviceToken(
                    id="dev-full", owner="owner", actor="owner", device_name="Full",
                    token_hmac="2" * 64,
                    issued_permissions=json.dumps(["budget.view", "budget.receipts.view", "budget.receipts.match"]),
                    created_at=self.now, expires_at=self.now + 60, hard_expires_at=self.now + 120,
                    sliding_written_at=self.now,
                ),
                db.BudgetReceiptDeviceToken(
                    id="dev-legacy", owner="owner", actor="owner", device_name="Legacy",
                    token_hmac="3" * 64, issued_permissions=None,
                    created_at=self.now, expires_at=self.now + 60, hard_expires_at=self.now + 120,
                    sliding_written_at=self.now,
                ),
            ))

        # Device token with budget.view only -> receipt is None; with budget.receipts.view -> populated; unknown device -> None.
        for dev_id, expect_linked in (("dev-view-only", False), ("dev-missing", False), ("dev-full", True), (None, True)):
            resp = self.views.transactions("owner", "2026-11", include_receipts=True, device_id=dev_id)
            by_ref = {row["entry_reference"]: row["receipt"] for row in resp["transactions"]}
            if expect_linked:
                self.assertEqual(by_ref["tx-1to1"]["shop_name"], "Biedronka")
            else:
                self.assertIsNone(by_ref["tx-1to1"])

        # Receipt store: delegate and any device token (legacy or modern) receive linked_transaction=None.
        for actor, dev_id in (("delegate", None), ("owner", "dev-legacy"), ("owner", "dev-full")):
            store = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-secret",
                                       device_id=dev_id, actor=actor)
            rows = store.list("owner", {})["receipts"]
            self.assertTrue(rows)
            for row in rows:
                self.assertIsNone(row["linked_transaction"])

        # Owner in custom phase without budget.receipts.view -> analytics transactions returns receipt=None.
        with self.sessions.begin() as session:
            session.execute(db.PermissionGrant.__table__.delete().where(
                db.PermissionGrant.username == "owner",
                db.PermissionGrant.permission_id.in_(("budget.receipts.view", "budget.receipts.edit", "budget.receipts.match")),
            ))
        no_rcpt_perm = self.views.transactions("owner", "2026-11", include_receipts=True)
        self.assertTrue(all(row["receipt"] is None for row in no_rcpt_perm["transactions"]))

        # Owner with budget.receipts.match but WITHOUT budget.view -> receipts list returns linked_transaction=None.
        with self.sessions.begin() as session:
            session.execute(db.PermissionGrant.__table__.delete().where(
                db.PermissionGrant.username == "owner",
                db.PermissionGrant.permission_id.in_(("budget.view", "budget.edit")),
            ))
            session.add(db.PermissionGrant(username="owner", permission_id="budget.receipts.match", target_owner="owner"))
        no_view_rows = self.receipts.list("owner", {})["receipts"]
        self.assertTrue(all(row["linked_transaction"] is None for row in no_view_rows))


class LinkIndicatorsHTTPTests(unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers
    assert_private_headers = fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {
            analytics_api.FLAG: "1",
            receipt_api.FLAG: "1",
            "HESTIA_BUDGET_JOBS_ENABLED": "0",
            "HESTIA_BUDGET_POLLER_ENABLED": "0",
            "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        await fixture.BudgetAPITests.asyncSetUp(self)
        rule_key = self.path.parent / "device.key"
        rule_key.write_bytes(b"0123456789abcdef0123456789abcdef")
        rule_key.chmod(0o400)
        key_patch = mock.patch.dict(os.environ, {"HESTIA_BUDGET_RULE_KEY_FILE": str(rule_key)})
        key_patch.start()
        self.addCleanup(key_patch.stop)
        with self.sessions.begin() as session:
            conn_b = db.BudgetConnection(
                id="alice-bank-b", owner="alice", application_id="app", bank_name="Bank B",
                country="PL", enabled=True, next_attempt=0, failures=0,
            )
            session.add(conn_b)
            session.flush()
            acct_b = db.BudgetAccount(
                id="alice-acct-b", connection_id="alice-bank-b", owner="alice",
                identification_hash="ALICE-SYNTHETIC-B", enabled=True, balances_json="[]",
            )
            session.add(acct_b)
            session.flush()
            session.get(db.BudgetAccount, self.alice_id).enabled = True
            _seed_links(session, "alice", self.alice_id, "alice-acct-b", 1.0)
        clear_projection_cache()

    async def test_analytics_transactions_contract_gating_and_values(self):
        # Old or absent header omits `receipt` entirely (never null).
        for version in (None, "0.58.0", _V59):
            hdrs = self.headers() | ({} if version is None else {_HEADER: version})
            resp = await self.client.get(analytics_api.PREFIX + "/transactions",
                                         params={"period": "2026-11"}, headers=hdrs)
            self.assertEqual(resp.status, 200, await resp.text())
            body = await resp.json()
            self.assertTrue(body["transactions"])
            for row in body["transactions"]:
                self.assertNotIn("receipt", row)

            key = f"{self.alice_id}/tx-1to1"
            post_hdrs = self.headers(origin=True) | ({} if version is None else {_HEADER: version})
            post_resp = await self.client.post(analytics_api.PREFIX + "/transactions-by-keys",
                                               json={"row_keys": [key]}, headers=post_hdrs)
            self.assertEqual(post_resp.status, 200, await post_resp.text())
            self.assertNotIn("receipt", (await post_resp.json())["transactions"][0])

        # Invalid or future contract version returns 400.
        for bad in ("0.61.0", "bad"):
            resp = await self.client.get(analytics_api.PREFIX + "/transactions",
                                         params={"period": "2026-11"},
                                         headers=self.headers() | {_HEADER: bad})
            self.assertEqual(resp.status, 400)
            self.assertEqual((await resp.json())["error"], "budget_analytics_contract_version_invalid")

        # 0.60.0 returns `receipt` on GET transactions and POST transactions-by-keys.
        resp = await self.client.get(analytics_api.PREFIX + "/transactions",
                                     params={"period": "2026-11"},
                                     headers=self.headers() | {_HEADER: _V60})
        self.assertEqual(resp.status, 200, await resp.text())
        body = await resp.json()
        api_contract.BudgetAnalyticsTransactions.model_validate(body)
        by_ref = {row["entry_reference"]: row["receipt"] for row in body["transactions"]}
        self.assertEqual(by_ref["tx-1to1"]["shop_name"], "Biedronka")
        self.assertEqual(by_ref["tx-grp-apple"]["shop_name"], "Apple +2")
        self.assertIsNone(by_ref["tx-unlinked"])

        post_resp = await self.client.post(analytics_api.PREFIX + "/transactions-by-keys",
                                           json={"row_keys": [f"{self.alice_id}/tx-grp-apple"]},
                                           headers=self.headers(origin=True) | {_HEADER: _V60})
        self.assertEqual(post_resp.status, 200, await post_resp.text())
        post_body = await post_resp.json()
        api_contract.BudgetAnalyticsTransactions.model_validate(post_body)
        self.assertEqual(post_body["transactions"][0]["receipt"]["shop_name"], "Apple +2")

    async def test_receipts_list_search_detail_contract_gating_and_device_scope(self):
        # Device-token ALLOWED set is unchanged.
        expected_allowed = {
            ("POST", receipt_api.PREFIX + "/parse-text"),
            ("POST", receipt_api.PREFIX + "/confirm"),
            ("POST", receipt_api.PREFIX + "/list"),
            ("POST", receipt_api.PREFIX + "/search"),
            ("GET", receipt_api.PREFIX + "/detail"),
            ("POST", receipt_api.PREFIX + "/group/candidates"),
            ("GET", receipt_api.PREFIX + "/image"),
            ("POST", receipt_api.PREFIX + "/delete"),
            ("POST", receipt_api.PREFIX + "/device-token/revoke"),
        }
        self.assertEqual(device_token.ALLOWED, expected_allowed)

        # Old or absent header omits `linked_transaction` entirely on list, search, and detail.
        for version in (None, "0.57.0", _V59):
            extra = {} if version is None else {_HEADER: version}
            for path, payload in (("/list", {}), ("/search", {"query": "Biedronka"})):
                resp = await self.client.post(receipt_api.PREFIX + path, json=payload,
                                              headers=self.headers(origin=True) | extra)
                self.assertEqual(resp.status, 200, await resp.text())
                rows = (await resp.json())["receipts"]
                self.assertTrue(rows)
                for row in rows:
                    self.assertNotIn("linked_transaction", row)
            detail = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": _R_1TO1},
                                           headers=self.headers() | extra)
            self.assertEqual(detail.status, 200, await detail.text())
            self.assertNotIn("linked_transaction", (await detail.json())["receipt"])

        # 0.60.0 includes `linked_transaction` on list and search (and no creditor/remittance leaks).
        resp = await self.client.post(receipt_api.PREFIX + "/list", json={},
                                      headers=self.headers(origin=True) | {_HEADER: _V60})
        self.assertEqual(resp.status, 200, await resp.text())
        body = await resp.json()
        api_contract.BudgetReceiptListResult.model_validate(body)
        by_id = {row["receipt_id"]: row["linked_transaction"] for row in body["receipts"]}
        self.assertEqual(by_id[_R_1TO1]["booking_date"], "2026-11-10")
        self.assertEqual(by_id[_R_1TO1]["amount"], "42.50")
        self.assertEqual(by_id[_R_1TO1]["currency"], "PLN")
        self.assertIn("eKonto", by_id[_R_1TO1]["account_label"])
        self.assertEqual(by_id[_R_GRP_1]["booking_date"], "2026-11-11")
        self.assertIsNone(by_id[_R_UNLINKED])
        wire = json.dumps(body)
        self.assertNotIn(_SECRET_CREDITOR, wire)
        self.assertNotIn(_SECRET_REMITTANCE, wire)

        # Legacy receipts device token: blocked on analytics/transactions, and linked_transaction=None on receipts/list.
        pair = await self.client.post(receipt_api.PREFIX + "/device-token/pair-code", json={},
                                      headers=self.headers(origin=True))
        self.assertEqual(pair.status, 200, await pair.text())
        redeem = await self.client.post(device_token.REDEEM_PATH,
                                        json={"code": (await pair.json())["code"], "device_name": "Phone"})
        self.assertEqual(redeem.status, 200, await redeem.text())
        bearer = {"Authorization": "Bearer " + (await redeem.json())["token"]}

        blocked_tx = await self.client.get(analytics_api.PREFIX + "/transactions",
                                           params={"period": "2026-11"}, headers=bearer | {_HEADER: _V60})
        self.assertEqual(blocked_tx.status, 403)

        dev_list_60 = await self.client.post(receipt_api.PREFIX + "/list", json={},
                                             headers=bearer | {_HEADER: _V60})
        self.assertEqual(dev_list_60.status, 200, await dev_list_60.text())
        dev_body = await dev_list_60.json()
        api_contract.BudgetReceiptListResult.model_validate(dev_body)
        for row in dev_body["receipts"]:
            self.assertIsNone(row["linked_transaction"])
        self.assertNotIn("eKonto", json.dumps(dev_body))
        self.assertNotIn(_SECRET_CREDITOR, json.dumps(dev_body))

        dev_list_59 = await self.client.post(receipt_api.PREFIX + "/list", json={},
                                             headers=bearer | {_HEADER: _V59})
        self.assertEqual(dev_list_59.status, 200, await dev_list_59.text())
        for row in (await dev_list_59.json())["receipts"]:
            self.assertNotIn("linked_transaction", row)
