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

from hestia import api_contract, authz, db, store_sql, web
from hestia.budget import device_token, receipt_api
from hestia.budget.receipt_parser import ReceiptError
from hestia.budget.receipt_store import BudgetReceiptStore
from tests import test_budget_api as fixture
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_receipts import confirm_values, receipt_text


class DelegatedReceiptStoreTests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.change_transaction(remittance_information=["BIEDRONKA"])
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

    def test_device_token_match_strength_and_delegated_confirm_guards(self):
        self.add_transaction(
            "a", "del-match-tx", amount="123.45", booking_date="2026-11-30", value_date=None,
            remittance_information=["BIEDRONKA NIP 7791011327"],
        )
        store_sql.activate_sessions_v2(path=self.path)
        devices = device_token.DeviceTokenStore(
            self.sessions, clock=lambda: self.now, session_secret=b"device-secret",
        )
        owner_device = devices.exchange("owner", "Vesta Owner", generation=0)
        owner_dev_store = BudgetReceiptStore(
            self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only", device_id=owner_device["token_id"],
        )
        parsed = owner_dev_store.parse_text("owner", receipt_text(total="123.45"))
        self.assertEqual(len(parsed["candidates"]), 1)
        self.assertEqual(parsed["candidates"][0]["match_strength"], "nip")
        detail = owner_dev_store.detail("owner", parsed["receipt_id"])
        self.assertEqual(detail["candidates"][0]["match_strength"], "nip")

        # Stale candidate token submitted by device token is rejected inside BEGIN IMMEDIATE.
        stale_cand = detail["candidates"][0]
        self.change_transaction("a", "del-match-tx", remittance_information=["BIEDRONKA NIP 7791011327 CHANGED"])
        with self.assertRaises(ReceiptError) as stale_err:
            owner_dev_store.confirm("owner", confirm_values(parsed, link={
                "account_id": stale_cand["account_id"],
                "entry_reference": stale_cand["entry_reference"],
                "candidate_token": stale_cand["candidate_token"],
            }))
        self.assertEqual((stale_err.exception.status, stale_err.exception.code),
                         (409, "budget_receipt_stale_source"))

        # Grant delegate add+view+edit: delegate confirms on an evidence row (both browser & device) fail with 409,
        # and delegate passing any link fails closed with 403.
        self.grant("budget.receipts.add", "budget.receipts.view", "budget.receipts.edit")
        doc_id, ev_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.sessions.begin() as session:
            session.add_all((
                db.BudgetReceipt(
                    id=doc_id, owner="owner", raw_shop_name="BIEDRONKA", raw_nip="7791011327",
                    receipt_kind="purchase", receipt_date="2026-11-30", total_amount="100",
                    currency="PLN", status="imported", source_kind="email", document_kind="invoice",
                    import_expires_at=self.now + 86400, version=1, created_at=self.now, updated_at=self.now,
                ),
                db.BudgetReceipt(
                    id=ev_id, owner="owner", raw_shop_name="BIEDRONKA", raw_nip="7791011327",
                    receipt_kind="purchase", receipt_date="2026-11-30", total_amount="100",
                    currency="PLN", status="imported", source_kind="email", document_kind="payment_confirmation",
                    gateway="PayU", evidence_for_receipt_id=doc_id,
                    import_expires_at=self.now + 86400, version=1, created_at=self.now, updated_at=self.now,
                ),
            ))
            session.add(db.BudgetReceiptLine(
                receipt_id=ev_id, owner="owner", line_no=1, line_kind="other",
                raw_name="Pozycja", raw_name_key="pozycja", quantity="1",
                gross_line_total="100", discount_amount="0", net_line_total="100",
            ))

        del_device = devices.exchange("delegate", "Vesta Delegate", generation=0)
        del_dev_store = BudgetReceiptStore(
            self.sessions, clock=lambda: self.now, cursor_secret=b"receipts-only", device_id=del_device["token_id"],
        )
        ev_payload = confirm_values({
            "receipt_id": ev_id, "version": 1,
            "draft": {
                "raw_shop_name": "BIEDRONKA", "raw_nip": "7791011327", "receipt_kind": "purchase",
                "receipt_date": "2026-11-30", "receipt_time": None, "total_amount": "100",
                "currency": "PLN", "payment_method": "card",
                "lines": [{
                    "line_kind": "other", "raw_name": "Pozycja", "ptu_code": None, "quantity": "1",
                    "raw_unit": None, "unit_price_raw": None, "gross_line_total": "100",
                    "discount_amount": "0", "net_line_total": "100",
                }],
            },
        })
        for store_actor, target_store in (("delegate", self.delegated()), ("delegate", del_dev_store)):
            with self.subTest(actor=target_store._actor):
                with self.assertRaises(ReceiptError) as ev_err:
                    target_store.confirm(store_actor if target_store._device_id is None else "owner", ev_payload)
                self.assertEqual((ev_err.exception.status, ev_err.exception.code),
                                 (409, "budget_receipt_stale_source"))
                with self.assertRaises(ReceiptError) as del_err:
                    target_store.delete(
                        store_actor if target_store._device_id is None else "owner",
                        {"receipt_id": ev_id, "expected_version": 1},
                    )
                self.assertEqual((del_err.exception.status, del_err.exception.code),
                                 (409, "budget_receipt_stale_source"))

        # Even if a delegate holds match permission, candidates preserve match_strength and stale tokens still 409.
        real_effective = authz.effective_permissions

        def with_match(session, actor, *, owner=None):
            perms = real_effective(session, actor, owner=owner)
            if actor == "delegate" and owner == "owner":
                return perms | {"budget.receipts.match"}
            return perms

        with mock.patch("hestia.authz.effective_permissions", side_effect=with_match):
            match_delegate = self.delegated()
            del_parsed = match_delegate.parse_text("delegate", receipt_text(total="123.45"))
            self.assertEqual(del_parsed["candidates"][0]["match_strength"], "nip")
            del_detail = match_delegate.detail("delegate", del_parsed["receipt_id"])
            self.assertEqual(del_detail["candidates"][0]["match_strength"], "nip")
            del_cand = del_detail["candidates"][0]
            self.change_transaction("a", "del-match-tx", remittance_information=["BIEDRONKA NIP 7791011327 AGAIN"])
            with self.assertRaises(ReceiptError) as del_stale:
                match_delegate.confirm("delegate", confirm_values(del_parsed, link={
                    "account_id": del_cand["account_id"],
                    "entry_reference": del_cand["entry_reference"],
                    "candidate_token": del_cand["candidate_token"],
                }))
            self.assertEqual((del_stale.exception.status, del_stale.exception.code),
                             (409, "budget_receipt_stale_source"))


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

    async def test_delegated_http_evidence_row_confirm_and_stale_token_rejected(self):
        doc_id, ev_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = web._now()
        with self.sessions.begin() as session:
            for perm in ("budget.receipts.add", "budget.receipts.view", "budget.receipts.edit"):
                session.add(db.PermissionGrant(username="operator", permission_id=perm, target_owner="alice"))
            session.add(db.BudgetConnection(
                id="conn-del-http", owner="alice", application_id="app", bank_name="Bank", country="PL",
                enabled=True, next_attempt=0, failures=0,
            ))
            session.flush()
            session.add(db.BudgetAccount(
                id="acc-del-http", connection_id="conn-del-http", owner="alice", identification_hash="acc",
                enabled=True, balances_json="{}",
            ))
            session.flush()
            session.add(db.BudgetTransaction(
                account_id="acc-del-http", entry_reference="tx-del-http",
                payload_json=json.dumps({
                    "entry_reference": "tx-del-http", "amount": "100", "currency": "PLN",
                    "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-30",
                    "value_date": None, "transaction_date": None,
                    "remittance_information": ["BIEDRONKA NIP 7791011327"],
                }),
                observed_at=now, source="api", api_observed_at=now, uncertain=False,
            ))
            session.add_all((
                db.BudgetReceipt(
                    id=doc_id, owner="alice", raw_shop_name="BIEDRONKA", raw_nip="7791011327",
                    receipt_kind="purchase", receipt_date="2026-11-30", total_amount="100",
                    currency="PLN", status="imported", source_kind="email", document_kind="invoice",
                    import_expires_at=now + 86400, version=1, created_at=now, updated_at=now,
                ),
                db.BudgetReceipt(
                    id=ev_id, owner="alice", raw_shop_name="BIEDRONKA", raw_nip="7791011327",
                    receipt_kind="purchase", receipt_date="2026-11-30", total_amount="100",
                    currency="PLN", status="imported", source_kind="email", document_kind="payment_confirmation",
                    gateway="PayU", evidence_for_receipt_id=doc_id,
                    import_expires_at=now + 86400, version=1, created_at=now, updated_at=now,
                ),
            ))
            session.add(db.BudgetReceiptLine(
                receipt_id=ev_id, owner="alice", line_no=1, line_kind="other",
                raw_name="Pozycja", raw_name_key="pozycja", quantity="1",
                gross_line_total="100", discount_amount="0", net_line_total="100",
            ))

        del_headers = {
            **self.headers("operator", origin=True),
            "Content-Type": "application/json",
            "X-Hestia-Contract-Version": api_contract.CONTRACT_VERSION,
        }
        del_detail = await self.client.get(
            receipt_api.PREFIX + "/detail", params={"receipt_id": doc_id}, headers=del_headers,
        )
        self.assertEqual(del_detail.status, 200, await del_detail.text())
        self.assertNotIn("candidates", await del_detail.json())

        confirm_body = {
            "receipt_id": ev_id, "expected_version": 1, "raw_shop_name": "BIEDRONKA",
            "raw_nip": "7791011327", "receipt_kind": "purchase", "receipt_date": "2026-11-30",
            "receipt_time": None, "total_amount": "100", "currency": "PLN", "payment_method": "card",
            "lines": [{
                "line_kind": "other", "raw_name": "Pozycja", "ptu_code": None, "quantity": "1",
                "raw_unit": None, "unit_price_raw": None, "gross_line_total": "100",
                "discount_amount": "0", "net_line_total": "100",
            }],
            "link": None,
        }
        ev_res = await self.client.post(
            receipt_api.PREFIX + "/confirm", data=json.dumps(confirm_body), headers=del_headers,
        )
        self.assertEqual(ev_res.status, 409, await ev_res.text())
        self.assertEqual((await ev_res.json())["error"], "budget_receipt_stale_source")

        link_res = await self.client.post(
            receipt_api.PREFIX + "/confirm",
            data=json.dumps({
                **confirm_body, "receipt_id": doc_id,
                "link": {"account_id": "acc-del-http", "entry_reference": "a" * 64, "candidate_token": "tok"},
            }),
            headers=del_headers,
        )
        self.assertEqual(link_res.status, 403, await link_res.text())
        self.assertEqual((await link_res.json())["error"], "budget_forbidden")


if __name__ == "__main__":
    unittest.main()
