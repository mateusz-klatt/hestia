import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import json
import os
import unittest
import uuid
from unittest import mock

from sqlalchemy import select

from hestia import authz, db, store_sql, web
from hestia.budget import device_token, receipt_api
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_api as fixture
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_receipts import confirm_values, receipt_text


class DelegatedReceiptStoreTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.add(db.User(username="delegate", password_hash="unused", role="viewer"))
            session.add(db.User(username="delegate2", password_hash="unused", role="viewer"))
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        self.receipts = BudgetReceiptStore(self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only")

    def grant(self, *permissions, actor="delegate"):
        with self.sessions.begin() as session:
            for permission in permissions:
                session.add(db.PermissionGrant(username=actor, permission_id=permission, target_owner="owner"))

    def delegated(self, actor="delegate"):
        return BudgetReceiptStore(
            self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only", actor=actor,
        )

    def test_add_only_attribution_cap_finalize_repeat_and_discard(self):
        with self.assertRaises(device_token.DeviceTokenError):
            self.delegated().list("delegate", {})
        self.grant("budget.receipts.add")
        delegate = self.delegated()
        with mock.patch.object(BudgetReceiptStore, "_candidates", side_effect=AssertionError("bank read")):
            drafts = [delegate.parse_text("delegate", receipt_text()) for _ in range(3)]
        self.assertTrue(all(item["candidates"] == [] for item in drafts))
        with self.assertRaises(ReceiptError) as capped:
            delegate.parse_text("delegate", receipt_text())
        self.assertEqual((capped.exception.status, capped.exception.code), (413, "budget_receipt_quota_exceeded"))
        with self.sessions() as session:
            rows = [session.get(db.BudgetReceipt, item["receipt_id"]) for item in drafts]
            self.assertEqual({row.submitted_by for row in rows}, {"delegate"})
            audit = session.scalar(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_id == drafts[0]["receipt_id"],
                db.BudgetAnalyticsAudit.action == "create",
            ))
            self.assertEqual(json.loads(audit.after_json)["actor"], "delegate")
        confirmed = delegate.confirm("delegate", confirm_values(drafts[0]))
        self.assertEqual(confirmed["receipt"]["status"], "confirmed")
        repeat = confirm_values({**drafts[0], "draft": confirmed["receipt"]})
        with self.assertRaises(device_token.DeviceTokenError) as repeated:
            delegate.confirm("delegate", repeat)
        self.assertEqual((repeated.exception.status, repeated.exception.code), (409, "already_submitted"))
        with self.assertRaises(device_token.DeviceTokenError):
            delegate.delete("delegate", {"receipt_id": drafts[0]["receipt_id"], "expected_version": 2})
        removed = delegate.delete("delegate", {"receipt_id": drafts[1]["receipt_id"], "expected_version": 1})
        self.assertTrue(removed["deleted"])
        owner_draft = self.receipts.parse_text("owner", receipt_text())
        with self.assertRaises(device_token.DeviceTokenError):
            delegate.confirm("delegate", confirm_values(owner_draft))
        with self.assertRaises(device_token.DeviceTokenError):
            delegate.list("delegate", {})
        with self.sessions() as session:
            self.assertEqual(authz.delegated_receipt_permissions(session, "owner"), frozenset())

    def test_owner_reserve_and_delegate_cursor_binding(self):
        self.grant("budget.receipts.add", "budget.receipts.view")
        self.grant("budget.receipts.view", actor="delegate2")
        with self.sessions.begin() as session:
            for index in range(15):
                session.add(db.BudgetReceipt(
                    id=str(uuid.uuid4()), owner="owner", submitted_by="owner", status="draft",
                    source_kind="live_text", version=1, created_at=self.now, updated_at=self.now,
                ))
        with self.assertRaises(ReceiptError) as reserved:
            self.delegated().parse_text("delegate", receipt_text())
        self.assertEqual(reserved.exception.code, "budget_receipt_quota_exceeded")
        owner = self.receipts.parse_text("owner", receipt_text())
        self.assertEqual(owner["version"], 1)
        first = self.receipts.parse_text("owner", receipt_text())
        second = self.receipts.parse_text("owner", receipt_text())
        del first, second
        delegate = self.delegated()
        with mock.patch("hestia.budget.receipt_store.os.urandom", return_value=bytes(12)):
            page = delegate.list("delegate", {"limit": 1})
        self.assertIsNotNone(page["next_cursor"])
        encoded_cursor = page["next_cursor"].split(".")[0]
        protected = base64.urlsafe_b64decode(encoded_cursor + "=" * (-len(encoded_cursor) % 4))
        self.assertEqual(protected[0], 1)
        self.assertNotIn(b"owner", protected)
        with self.assertRaises(ValueError):
            json.loads(protected)
        following = delegate.list("delegate", {"limit": 1, "cursor": page["next_cursor"]})
        self.assertEqual(len(following["receipts"]), 1)
        for actor, store in (("owner", self.receipts), ("delegate2", self.delegated("delegate2"))):
            with self.subTest(actor=actor), self.assertRaises(ReceiptError):
                store.list(actor, {"limit": 1, "cursor": page["next_cursor"]})

        last = page["receipts"][0]
        raw = json.dumps({
            "owner": "owner", "actor": "delegate", "context": delegate._filters({"limit": 1}, search=False)[-1],
            "last_date": last["receipt_date"], "last_id": last["receipt_id"], "expires_at": self.now + 60,
        }).encode("ascii")
        mac = hmac.digest(delegate._receipt_cursor_key, raw, "sha256")
        legacy_cursor = ".".join(base64.urlsafe_b64encode(part).rstrip(b"=").decode("ascii") for part in (raw, mac))
        with self.assertRaises(ReceiptError):
            delegate.list("delegate", {"limit": 1, "cursor": legacy_cursor})

    def test_link_redaction_and_sensitive_edit_preserve_reservation_without_bank_read(self):
        parsed = self.receipts.parse_text("owner", receipt_text())
        link = {"account_id": self.accounts["a"], "entry_reference": "debit"}
        owner_values = confirm_values(parsed, link=link)
        confirmed = self.receipts.confirm("owner", owner_values)["receipt"]
        sealed_link = owner_values["link"]
        self.grant("budget.receipts.edit")
        delegate = self.delegated()
        detail = delegate.detail("delegate", parsed["receipt_id"])["receipt"]
        self.assertTrue(detail["linked"])
        self.assertIsNone(detail["linked_account_id"])
        self.assertIsNone(detail["linked_entry_ref"])
        editable = {"receipt_id": parsed["receipt_id"], "draft": detail, "version": detail["version"]}
        with mock.patch.object(BudgetReceiptStore, "_confirmed_link", side_effect=AssertionError("bank read")):
            changed = delegate.confirm("delegate", confirm_values(editable, raw_shop_name="EDITED SHOP"))
        self.assertEqual(changed["receipt"]["status"], "needs_review")
        self.assertIsNone(changed["receipt"]["linked_account_id"])
        with self.sessions() as session:
            row = session.get(db.BudgetReceipt, parsed["receipt_id"])
            self.assertEqual((row.status, row.linked_account_id, row.linked_entry_ref),
                             ("needs_review", self.accounts["a"], "debit"))
            self.assertEqual(row.submitted_by, "owner")
        reviewed = delegate.detail("delegate", parsed["receipt_id"])
        self.assertNotIn("candidates", reviewed)
        with self.assertRaises(device_token.DeviceTokenError):
            delegate.delete("delegate", {
                "receipt_id": parsed["receipt_id"], "expected_version": changed["receipt"]["version"],
            })
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, parsed["receipt_id"]).linked_entry_ref, "debit")
        editable = {"receipt_id": parsed["receipt_id"], "draft": changed["receipt"],
                    "version": changed["receipt"]["version"]}
        with self.assertRaises(device_token.DeviceTokenError):
            delegate.confirm("delegate", confirm_values(editable, link=sealed_link))
        owner_detail = self.receipts.detail("owner", parsed["receipt_id"])
        self.assertEqual(owner_detail["receipt"]["linked_account_id"], self.accounts["a"])
        self.assertTrue(owner_detail["candidates"])

    def test_match_withdrawal_is_rechecked_inside_parse_candidate_snapshot(self):
        real_read = BudgetReceiptStore._read

        @contextmanager
        def withdraw_before_read(store, owner, permission="budget.view"):
            if permission is None:
                with store.sessions.begin() as session:
                    session.execute(db.PermissionGrant.__table__.delete().where(
                        db.PermissionGrant.username == "owner", db.PermissionGrant.target_owner == "owner",
                        db.PermissionGrant.permission_id == "budget.receipts.match",
                    ))
            with real_read(store, owner, permission=permission) as session:
                yield session

        with mock.patch.object(BudgetReceiptStore, "_read", withdraw_before_read), \
             mock.patch.object(BudgetReceiptStore, "_candidates", side_effect=AssertionError("stale bank read")):
            parsed = self.receipts.parse_text("owner", receipt_text())
        self.assertEqual(parsed["candidates"], [])

    def test_match_withdrawal_before_detail_redacts_and_skips_candidates(self):
        parsed = self.receipts.parse_text("owner", receipt_text())
        link = {"account_id": self.accounts["a"], "entry_reference": "debit"}
        confirmed = self.receipts.confirm("owner", confirm_values(parsed, link=link))["receipt"]
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, parsed["receipt_id"]).status = "needs_review"
        real_read = BudgetReceiptStore._read

        @contextmanager
        def withdraw_before_read(store, owner, permission="budget.view"):
            if permission is None:
                with store.sessions.begin() as session:
                    session.execute(db.PermissionGrant.__table__.delete().where(
                        db.PermissionGrant.username == "owner", db.PermissionGrant.target_owner == "owner",
                        db.PermissionGrant.permission_id == "budget.receipts.match",
                    ))
            with real_read(store, owner, permission=permission) as session:
                yield session

        with mock.patch.object(BudgetReceiptStore, "_read", withdraw_before_read), \
             mock.patch.object(BudgetReceiptStore, "_candidates", side_effect=AssertionError("stale bank read")):
            detail = self.receipts.detail("owner", parsed["receipt_id"])
        self.assertNotIn("candidates", detail)
        self.assertIsNone(detail["receipt"]["linked_account_id"])
        self.assertIsNone(detail["receipt"]["linked_entry_ref"])
        self.assertEqual(confirmed["status"], "confirmed")

    def test_delegate_target_change_before_parse_write_rejects_the_draft(self):
        self.grant("budget.receipts.add")
        delegate = self.delegated()
        read_candidates = delegate._read_candidates

        def retarget_after_read(owner, parsed):
            candidates = read_candidates(owner, parsed)
            with self.sessions.begin() as session:
                session.execute(db.PermissionGrant.__table__.update().where(
                    db.PermissionGrant.username == "delegate",
                ).values(target_owner="other"))
            return candidates

        with mock.patch.object(delegate, "_read_candidates", side_effect=retarget_after_read), \
             self.assertRaises(device_token.DeviceTokenError) as denied:
            delegate.parse_text("delegate", receipt_text())
        self.assertEqual((denied.exception.status, denied.exception.code), (403, "budget_forbidden"))
        self.assertEqual(self.rows(db.BudgetReceipt), [])

    def test_target_change_before_detail_candidate_snapshot_rejects_former_ledger(self):
        parsed = self.receipts.parse_text("owner", receipt_text())
        read = BudgetReceiptStore._read

        @contextmanager
        def retarget_before_read(store, owner, permission="budget.view"):
            with self.sessions.begin() as session:
                session.get(db.User, "owner").budget_owner = False
                session.add(db.PermissionGrant(username="owner", permission_id="budget.receipts.view", target_owner="other"))
            with read(store, owner, permission=permission) as session:
                yield session

        with mock.patch.object(BudgetReceiptStore, "_read", retarget_before_read), \
             self.assertRaises(device_token.DeviceTokenError) as denied:
            self.receipts.detail("owner", parsed["receipt_id"])
        self.assertEqual((denied.exception.status, denied.exception.code), (403, "budget_forbidden"))

    def test_delegate_qr_alert_uses_actor_and_redemption_date(self):
        from hestia.budget.analytics_views import BudgetAnalyticsViews

        code_id = str(uuid.uuid4())
        issued = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc).timestamp()
        redeemed = datetime(2026, 12, 1, 0, 1, tzinfo=timezone.utc).timestamp()
        with self.sessions.begin() as session:
            session.add(db.NativeLoginCode(
                id=code_id, username="delegate", code_hmac="a" * 64, generation=0,
                created_at=issued, expires_at=issued + 300, consumed_at=redeemed,
            ))
            device_token.write_device_audit(
                session, "owner", code_id, device_token.NEW_DEVICE_LOGIN, redeemed,
                actor="delegate", device_name="Vesta QR",
            )
        with self.sessions() as session:
            entries = BudgetAnalyticsViews._inbox_device_entries(session, "owner")
        self.assertEqual(entries[0]["params"]["actor"], "delegate")
        self.assertEqual(entries[0]["params"]["date"], "2026-12-01")

    def test_modern_device_uses_actor_owner_and_live_grants(self):
        self.grant("budget.receipts.add", "budget.receipts.view")
        store_sql.activate_sessions_v2(path=self.path)
        devices = device_token.DeviceTokenStore(
            self.sessions, clock=lambda: self.now, session_secret=b"device-secret",
        )
        device = devices.exchange("delegate", "Vesta", generation=0)
        self.assertEqual((device["actor"], device["owner"], device["scope"]), ("delegate", "owner", "receipts"))
        store = BudgetReceiptStore(
            self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only", device_id=device["token_id"],
        )
        parsed = store.parse_text("owner", receipt_text())
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceipt, parsed["receipt_id"]).submitted_by, "delegate")
        with self.sessions.begin() as session:
            session.execute(db.PermissionGrant.__table__.delete().where(
                db.PermissionGrant.username == "delegate",
                db.PermissionGrant.permission_id == "budget.receipts.view",
            ))
        store.parse_text("owner", receipt_text())
        with self.sessions() as session:
            self.assertEqual(json.loads(session.get(
                db.BudgetReceiptDeviceToken, device["token_id"],
            ).issued_permissions), ["budget.receipts.add"])
        with self.sessions.begin() as session:
            session.execute(db.PermissionGrant.__table__.delete().where(
                db.PermissionGrant.username == "delegate",
            ))
        with self.assertRaises(device_token.DeviceTokenError):
            store.parse_text("owner", receipt_text())


class DelegatedReceiptHTTPTests(fixture.unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {
            receipt_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
            "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        await fixture.BudgetAPITests.asyncSetUp(self)
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            session.add(db.PermissionGrant(
                username="viewer", permission_id="budget.receipts.add", target_owner="alice",
            ))

    async def test_delegate_parse_and_capabilities_hide_owner(self):
        response = await self.client.post(
            device_token.PARSE_PATH, data=json.dumps({"text": receipt_text()}),
            headers={**self.headers("viewer", origin=True), "Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["candidates"], [])
        capabilities = await self.client.get("/api/me/capabilities", headers=self.headers("viewer"))
        payload = await capabilities.json()
        self.assertEqual(payload["delegated_receipts"], {
            "permissions": ["budget.receipts.add"], "can_use": True,
        })
        self.assertNotIn("alice", json.dumps(payload))

    async def test_view_only_delegate_sees_versioned_evidence_for_the_authorized_ledger(self):
        document_id, evidence_id, foreign_document_id, foreign_evidence_id = (str(uuid.uuid4()) for _ in range(4))
        now = web._now()
        shared = {"source_kind": "email", "version": 1, "created_at": now, "updated_at": now,
                  "currency": "PLN", "total_amount": "85", "payment_method": "card"}
        with self.sessions.begin() as session:
            session.add(db.PermissionGrant(username="operator", permission_id="budget.receipts.view",
                                           target_owner="alice"))
            session.add_all((
                db.BudgetReceipt(
                    id=document_id, owner="alice", status="needs_review", document_kind="invoice",
                    receipt_date="2026-11-10", linked_account_id=self.alice_id, linked_entry_ref="alice-entry",
                    linked_fingerprint="a" * 64, linked_evidence="{}", **shared,
                ),
                db.BudgetReceipt(id=foreign_document_id, owner="bob", status="imported", document_kind="invoice",
                                 receipt_date="2026-11-11", **shared),
            ))
            session.flush()
            for owner, payment_id, target_id in (("alice", evidence_id, document_id),
                                                 ("bob", foreign_evidence_id, foreign_document_id)):
                session.add(db.BudgetReceipt(
                    id=payment_id, owner=owner, status="imported", document_kind="payment_confirmation",
                    receipt_date="2026-11-12", gateway="Example Pay", gateway_status="paid", fee_amount="1",
                    evidence_for_receipt_id=target_id, **shared,
                ))

        def headers(user="operator", version="0.52.0"):
            return {**self.headers(user, origin=True), "X-Hestia-Contract-Version": version}

        async def listing(values, *, user="operator", version="0.52.0"):
            return await self.client.post(receipt_api.PREFIX + "/list", json=values, headers=headers(user, version))

        async def detail(receipt_id):
            return await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": receipt_id},
                                         headers=headers())

        for values, expected in (({}, [document_id]), ({"evidence": "only"}, [evidence_id]),
                                 ({"evidence": "include"}, [evidence_id, document_id])):
            with self.subTest(values=values):
                response = await listing(values)
                self.assertEqual(response.status, 200, await response.text())
                self.assertEqual([receipt["receipt_id"] for receipt in (await response.json())["receipts"]], expected)

        for version, expected in (("0.51.0", {}), ("0.52.0", {"evidence_for_receipt_id": document_id})):
            response = await listing({"evidence": "only"}, version=version)
            self.assertEqual(response.status, 200, await response.text())
            (payment,) = (await response.json())["receipts"]
            self.assertEqual({key: payment[key] for key in payment.keys() & {"evidence_for_receipt_id"}}, expected)

        response = await detail(document_id)
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertEqual([item["receipt_id"] for item in body["receipt"]["payment_evidence"]], [evidence_id])
        self.assertTrue(body["receipt"]["linked"])
        self.assertIsNone(body["receipt"]["linked_account_id"])
        self.assertIsNone(body["receipt"]["linked_entry_ref"])
        self.assertNotIn("candidates", body)
        self.assertEqual((await detail(foreign_evidence_id)).status, 404)

        first = await listing({"evidence": "include", "limit": 1})
        self.assertEqual(first.status, 200, await first.text())
        cursor = (await first.json())["next_cursor"]
        self.assertIsNotNone(cursor)
        following = await listing({"evidence": "include", "limit": 1, "cursor": cursor})
        self.assertEqual(following.status, 200, await following.text())
        self.assertEqual([receipt["receipt_id"] for receipt in (await following.json())["receipts"]], [document_id])
        self.assertEqual((await listing({"evidence": "only", "cursor": cursor})).status, 400)
        self.assertEqual((await listing({"evidence": "include", "cursor": cursor}, user="alice")).status, 400)


if __name__ == "__main__":
    unittest.main()
