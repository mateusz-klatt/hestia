"""Owner-private receipt evidence suggestions and transactional attachment."""
import json
import unittest
from unittest import mock
from uuid import uuid4

from hestia import api_contract, authz, db, store_sql
from hestia.budget import device_token, receipt_api, receipt_store
from hestia.budget.analytics_core import financial_fingerprint
from hestia.budget.gmail_store import (
    ORDER_REFERENCE_DOMAIN, PAYMENT_PAYEE_DOMAIN, _payment_order_key, _payment_payee_key, digest,
)
from tests import test_budget_api as api_fixture
from tests import test_budget_receipt_group_api as group_fixture


class ReceiptEvidenceTests(unittest.IsolatedAsyncioTestCase):
    _HMAC_SECRET = group_fixture.ReceiptGroupAPITests._HMAC_SECRET
    asyncSetUp = group_fixture.ReceiptGroupAPITests.asyncSetUp
    rows = api_fixture.BudgetAPITests.rows
    token = api_fixture.BudgetAPITests.token
    headers = api_fixture.BudgetAPITests.headers
    _add_receipt = group_fixture.ReceiptGroupAPITests._add_receipt
    _receipt_headers = group_fixture.ReceiptGroupAPITests._receipt_headers
    _post_receipt = group_fixture.ReceiptGroupAPITests._post_receipt

    def source(self, *, shop_name="Fikcjusz Bistro", **overrides):
        receipt_id = str(uuid4())
        # The application's startup retention sweep may run while the fixtures are inserted.
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(**({
                "id": receipt_id, "owner": "alice", "submitted_by": "alice", "raw_shop_name": shop_name,
                "receipt_date": "2026-04-15", "total_amount": "37.4200", "currency": "PLN",
                "status": "imported", "source_kind": "email", "payment_method": None,
                "import_expires_at": 2_900_000_000, "version": 1, "created_at": 2_000_000.0,
                "updated_at": 2_000_000.0,
            } | overrides)))
        return receipt_id

    def bank(self, ref, owner, **overrides):
        account = self.alice_id if owner == "alice" else self.bob_id
        payload = api_fixture.transaction(ref, **({
            "amount": "37.42", "booking_date": "2026-04-15",
            "remittance_information": ["Synthetic charge"],
        } | overrides))
        with self.sessions.begin() as session:
            session.add(db.BudgetTransaction(
                account_id=account, entry_reference=ref, payload_json=json.dumps(payload),
                observed_at=2_000_000.0, uncertain=False,
            ))
        return account, payload

    def target(self, ref, *, owner="alice", tx=None, **overrides):
        account, payload = self.bank(ref, owner, **(tx or {}))
        target = self._add_receipt(**({
            "owner": owner, "shop_name": "Fikcjusz", "receipt_date": "2026-04-15",
            "total_amount": "37.42", "forwarded_sender_domain": None,
        } | overrides))
        self.change(target, linked_account_id=account, linked_entry_ref=ref,
                    linked_fingerprint=financial_fingerprint(payload), linked_evidence="{}")
        return target

    def change(self, receipt_id, **updates):
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceipt, receipt_id)
            for name, value in updates.items():
                setattr(row, name, value)

    async def detail(self, receipt_id, *, user="alice", version="0.69.0", device_token_secret=None):
        response = await self.client.get(
            receipt_api.PREFIX + "/detail", params={"receipt_id": receipt_id},
            headers=self._receipt_headers(user, version=version, device_token_secret=device_token_secret),
        )
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def attach(self, source, target, *, source_version=1, target_version=1, version="0.69.0",
                     user="alice", device_token_secret=None):
        return await self._post_receipt("attach-evidence", {
            "receipt_id": source, "expected_version": source_version,
            "target_receipt_id": target, "target_expected_version": target_version,
        }, version=version, user=user, device_token_secret=device_token_secret)

    async def test_distinctive_merchant_preview_and_explicit_attach_with_legacy_omission(self):
        source = self.source()
        target = self.target("distinctive", receipt_date="2026-04-13", total_amount="36.42",
                             tx={"booking_date": "2026-04-18", "transaction_date": "2026-04-17"})
        for version in (None, "0.66.0", "0.68.0"):
            with self.subTest(version=version):
                self.assertNotIn("evidence_target", await self.detail(source, version=version))
                self.assertEqual((await self.attach(source, target, version=version)).status, 404)

        preview = await self.detail(source)
        api_contract.BudgetReceiptDetailResult.model_validate(preview)
        self.assertEqual(preview["candidates"], [])
        self.assertEqual(preview["evidence_target"], {
            "receipt_id": target, "version": 1, "raw_shop_name": "Fikcjusz", "receipt_date": "2026-04-13",
        })
        self.assertIsNone(preview["receipt"]["evidence_for_receipt_id"])

        response = await self.attach(source, target)
        self.assertEqual(response.status, 200, await response.text())
        receipt = api_contract.BudgetReceiptConfirmResult.model_validate(await response.json()).receipt
        self.assertEqual((receipt.evidence_for_receipt_id, receipt.version, receipt.status, receipt.payment_method),
                         (target, 2, "confirmed", "unknown"))
        self.assertFalse(receipt.linked)
        self.assertIsNone(receipt.import_expires_at)
        target_detail = (await self.detail(target))["receipt"]
        self.assertTrue(target_detail["linked"])
        self.assertEqual(target_detail["total_amount"], "36.42")
        self.assertEqual([row["receipt_id"] for row in target_detail["payment_evidence"]], [source])
        audit = next(row for row in self.rows(db.BudgetAnalyticsAudit) if row.subject_id == source)
        self.assertEqual(audit.action, "attach_evidence")
        self.assertNotIn("Fikcjusz", audit.after_json)
        for version in (1, 2):
            self.assertEqual((await self.attach(source, target, source_version=version)).status, 409)

    async def test_gateway_and_platform_relations_from_payment_evidence(self):
        with (
            mock.patch.object(receipt_store, "GATEWAY_TOKENS", {"fauxgateway", "othergateway"}),
            mock.patch.object(receipt_store, "_EVIDENCE_PLATFORMS", (("test", "bazaar"),)),
        ):
            source = self.source(shop_name="FauxGateway")
            target = self.target("gateway", tx={"remittance_information": ["FauxGateway Fikcjusz"]})
            self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)

            source = self.source(shop_name="Fikcjusz", receipt_date="2026-05-15")
            target = self.target("platform", shop_name="Test Bazaar", receipt_date="2026-05-15", tx={
                "amount": "38.42", "booking_date": "2026-05-15", "creditor": {"name": "Fikcjusz"},
            })
            self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)

            source = self.source(shop_name="FauxGateway", receipt_date="2026-06-15")
            target = self.target("payee-hmac", receipt_date="2026-06-15", tx={
                "booking_date": "2026-06-15", "remittance_information": ["OtherGateway Fikcjusz"],
            })
            self.assertNotIn("evidence_target", await self.detail(source))
            with self.sessions.begin() as session:
                session.add(db.AppMeta(
                    key=_payment_payee_key(source), value=digest(self._HMAC_SECRET, PAYMENT_PAYEE_DOMAIN, "fikcjusz"),
                ))
            self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)

    async def test_shared_order_reference_or_invoice_number(self):
        source = self.source(shop_name="Mistberry")
        target = self.target("order-hmac")
        order = digest(self._HMAC_SECRET, ORDER_REFERENCE_DOMAIN, "ORDER-DEMO-412")
        with self.sessions.begin() as session:
            session.add_all(db.AppMeta(key=_payment_order_key(rid), value=order) for rid in (source, target))
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)

        target = self.target("invoice-number", receipt_date="2026-05-15", tx={"booking_date": "2026-05-15"})
        source = self.source(shop_name="Mistberry", receipt_date="2026-05-15")
        self.change(source, invoice_number="TEST/DEMO/16")
        self.change(target, invoice_number="test/demo/16")
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)

    async def test_ineligible_targets(self):
        source = self.source(shop_name="Fikcjusz sp. z o.o. PL Shop")
        for ref, receipt_options, bank_options in (
            ("generic-only", {"shop_name": "Mistberry sp. z o.o. PL Shop"}, {}),
            ("unconfirmed", {"status": "needs_review"}, {}),
            ("distant-receipt", {"receipt_date": "2026-04-12"}, {}),
            ("distant-bank-debit", {}, {"booking_date": "2026-04-18"}),
            ("different-currency", {}, {"currency": "EUR"}),
            ("different-amount", {"total_amount": "37.41"}, {"amount": "37.43"}),
            ("credit", {}, {"credit_debit_indicator": "CRDT"}),
        ):
            with self.subTest(ref=ref):
                target = self.target(ref, tx=bank_options, **receipt_options)
                self.assertNotIn("evidence_target", await self.detail(source))
                self.assertEqual((await self.attach(source, target)).status, 409)

        self.target("malformed-json")
        with self.sessions.begin() as session:
            tx = session.get(db.BudgetTransaction, (self.alice_id, "malformed-json"))
            # SQLite accepts duplicate JSON keys. Receipt matching rejects them.
            tx.payload_json = tx.payload_json[:-1] + ', "status": "BOOK"}'
        self.assertNotIn("evidence_target", await self.detail(source))
        self.target("missing-debit-amount")
        with self.sessions.begin() as session:
            tx = session.get(db.BudgetTransaction, (self.alice_id, "missing-debit-amount"))
            payload = json.loads(tx.payload_json)
            payload.pop("amount")
            tx.payload_json = json.dumps(payload)
        self.assertNotIn("evidence_target", await self.detail(source))

    async def test_evidence_can_attach_to_confirmed_group_member(self):
        source = self.source(payment_method="card")
        target = self._add_receipt(shop_name="Fikcjusz", receipt_date="2026-04-15", total_amount="37.42",
                                   forwarded_sender_domain=None)
        other = self._add_receipt(shop_name="Mistberry", receipt_date="2026-04-15", total_amount="18.23",
                                  forwarded_sender_domain=None)
        group_id = str(uuid4())
        account, payload = self.bank("group-debit", "alice", amount="55.65")
        with self.sessions.begin() as session:
            session.add(db.BudgetReceiptLinkGroup(
                id=group_id, owner="alice", account_id=account, entry_reference="group-debit",
                currency="PLN", tx_amount="55.65", credit_debit_indicator="DBIT", status="confirmed", version=1,
                linked_fingerprint=financial_fingerprint(payload), linked_evidence="{}",
                created_at=2_000_000.0, updated_at=2_000_000.0,
            ))
            session.flush()
            session.add_all(db.BudgetReceiptLinkMember(group_id=group_id, receipt_id=member, owner="alice")
                            for member in (target, other))
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)
        response = await self.attach(source, target)
        self.assertEqual(response.status, 200, await response.text())
        detail = (await self.detail(target))["receipt"]
        self.assertEqual(detail["payment_evidence"][0]["receipt_id"], source)
        self.assertEqual(detail["link_group"]["member_count"], 2)

    async def test_ambiguity_and_rechecked_confirmation_and_link(self):
        source = self.source()
        target = self.target("first")
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)
        other = self.target("second")
        self.assertNotIn("evidence_target", await self.detail(source))
        self.assertEqual((await self.attach(source, target)).status, 409)

        self.change(other, status="needs_review")
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)
        self.change(target, status="needs_review")
        self.assertEqual((await self.attach(source, target)).status, 409)

        self.change(target, status="confirmed", version=2)
        self.assertEqual((await self.attach(source, target)).status, 409)
        self.change(target, linked_account_id=None, linked_entry_ref=None, linked_fingerprint=None, linked_evidence=None)
        self.assertEqual((await self.attach(source, target, target_version=2)).status, 409)
        self.assertIsNone((await self.detail(source))["receipt"]["evidence_for_receipt_id"])

    async def test_owner_isolation_and_write_privilege(self):
        source = self.source()
        target = self.target("foreign", owner="bob")
        self.assertNotIn("evidence_target", await self.detail(source))
        self.assertEqual((await self.attach(source, target)).status, 409)
        self.assertEqual((await self.attach(source, target, user="bob")).status, 409)

        local = self.target("only-owner")
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        paired = await self.client.post(device_token.PAIR_PATH, json={}, headers=self.headers("alice", origin=True))
        redeem = await self.client.post(
            device_token.REDEEM_PATH, json={"code": (await paired.json())["code"], "device_name": "Test device"},
        )
        token = (await redeem.json())["token"]
        self.assertNotIn("evidence_target", await self.detail(source, device_token_secret=token))
        self.assertEqual((await self.attach(source, local, device_token_secret=token)).status, 403)
        with self.sessions.begin() as session:
            session.add_all(db.PermissionGrant(username="operator", permission_id=permission, target_owner="alice")
                            for permission in (
                                "budget.receipts.add", "budget.receipts.view",
                                "budget.receipts.edit", "budget.receipts.match",
                            ))
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], local)
        self.assertNotIn("evidence_target", await self.detail(source, user="operator"))
        self.assertEqual((await self.attach(source, local, user="operator")).status, 403)
        with self.sessions.begin() as session:
            session.query(db.PermissionGrant).filter(
                db.PermissionGrant.username == "alice", db.PermissionGrant.permission_id == "budget.receipts.match",
            ).delete()
        self.assertEqual((await self.attach(source, local)).status, 403)

    async def test_stale_attach_after_source_is_bank_linked(self):
        source = self.source()
        target = self.target("original-target")
        self.assertEqual((await self.detail(source))["evidence_target"]["receipt_id"], target)
        account, payload = self.bank("new-source-link", "alice")
        self.change(source, status="confirmed", payment_method="card", version=2, linked_account_id=account,
                    linked_entry_ref="new-source-link", linked_fingerprint=financial_fingerprint(payload),
                    linked_evidence="{}")
        self.assertEqual((await self.attach(source, target, source_version=2)).status, 409)
        actual = (await self.detail(source))["receipt"]
        self.assertIsNone(actual["evidence_for_receipt_id"])
        self.assertTrue(actual["linked"])

    async def test_free_bank_candidate_takes_precedence(self):
        source = self.source(payment_method="card")
        self.target("occupied")
        self.bank("free", "alice", remittance_information=["Fikcjusz Bistro"])
        preview = await self.detail(source)
        self.assertEqual(len(preview["candidates"]), 1)
        self.assertNotIn("evidence_target", preview)
