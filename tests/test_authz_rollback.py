"""PR 2 rollback behavior against a custom, PR 3-shaped authorization database."""

import base64
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from sqlalchemy import select

from hestia import auth, db, store_sql, web
from hestia.budget import device_token, receipt_api
from tests import test_budget_api as budget_fixture
from tests import test_budget_device_tokens as device_fixture
from tests.test_budget_receipts import receipt_text


class LegacyReceiptRollbackTests(unittest.IsolatedAsyncioTestCase):
    rows = budget_fixture.BudgetAPITests.rows
    token = budget_fixture.BudgetAPITests.token
    headers = budget_fixture.BudgetAPITests.headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {
            device_token.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
            "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        })
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(device_token.reset_limits)
        await budget_fixture.BudgetAPITests.asyncSetUp(self)
        key = device_fixture._make_rule_key(self.path.parent)
        key_patch = mock.patch.dict(os.environ, {device_fixture.RULE_KEY_FILE: str(key)})
        key_patch.start()
        self.addCleanup(key_patch.stop)
        self.password = "rollback-password"
        with self.sessions.begin() as session:
            for username in ("alice", "bob", "viewer"):
                session.get(db.User, username).password_hash = auth.hash_password(self.password)
        store_sql.activate_catalog_compat(path=self.path)
        self.store = device_token.DeviceTokenStore(self.sessions, clock=web._now)
        self.alice_device = self.store.login("alice", self.password, "Legacy Alice")
        self.bob_device = self.store.login("bob", self.password, "Legacy Bob")
        parsed = receipt_api._store(budget_fixture.SECRET).parse_text("alice", receipt_text())
        self.pair_code = self.store.issue_code("alice")
        self.receipt_id = parsed["receipt_id"]
        with self.sessions.begin() as session:
            session.get(db.BudgetReceipt, self.receipt_id).submitted_by = "viewer"
            session.get(db.AppMeta, "authz_policy_phase").value = "catalog_custom"
            session.add(db.PermissionGrant(
                username="viewer", permission_id="budget.receipts.add", target_owner="alice",
            ))
            delegate_raw = base64.urlsafe_b64encode(
                b"delegate-token".ljust(32, b"x"),
            ).rstrip(b"=").decode("ascii")
            self.delegate_raw = delegate_raw
            session.add(db.BudgetReceiptDeviceToken(
                id="delegate-device", owner="alice", actor="viewer", device_name="Delegate",
                token_hmac=device_token._mac(device_token._rule_key(), device_token._TOKEN_DOMAIN, delegate_raw),
                issued_permissions=json.dumps(["budget.receipts.add"]),
                created_at=100.0, expires_at=web._now() + 3600, hard_expires_at=web._now() + 7200,
                sliding_written_at=100.0,
            ))
            match = session.execute(select(db.PermissionGrant).where(
                db.PermissionGrant.username == "alice",
                db.PermissionGrant.permission_id == "budget.receipts.match",
                db.PermissionGrant.target_owner == "alice",
            )).scalar_one()
            session.delete(match)

    async def _post(self, path, body, headers):
        return await self.client.post(
            path, data=json.dumps(body), headers={"Content-Type": "application/json", **headers},
        )

    def _state(self):
        with self.sessions() as session:
            receipts = [
                (row.id, row.version, row.updated_at, row.submitted_by)
                for row in session.scalars(select(db.BudgetReceipt))
            ]
            audits = [(row.id, row.action) for row in session.scalars(select(db.BudgetAnalyticsAudit))]
            tokens = {
                row.id: (row.last_used_at, row.sliding_written_at, row.expires_at, row.revoked_at)
                for row in session.scalars(select(db.BudgetReceiptDeviceToken))
            }
        return receipts, audits, tokens

    async def test_custom_delegate_and_narrowed_profiles_are_side_effect_free(self):
        before = self._state()
        delegate_cookie = await self._post(
            device_token.PARSE_PATH, {"text": receipt_text()}, self.headers("viewer", origin=True),
        )
        self.assertEqual(delegate_cookie.status, 403)
        delegate_bearer = await self._post(
            device_token.PARSE_PATH, {"text": receipt_text()},
            {"Authorization": f"Bearer {self.delegate_raw}"},
        )
        self.assertEqual(delegate_bearer.status, 403)
        self.assertEqual((await delegate_bearer.json())["error"], "budget_forbidden")
        new_namespace = await self._post(
            device_token.PARSE_PATH, {"text": receipt_text()},
            {"Authorization": "Bearer v3.unsupported.device-credential"},
        )
        self.assertEqual(new_namespace.status, 401)
        self.assertEqual((await new_namespace.json())["error"], "device_token_invalid")

        narrowed_cookie = await self._post(
            device_token.PARSE_PATH, {"text": receipt_text()}, self.headers("alice", origin=True),
        )
        self.assertEqual(narrowed_cookie.status, 403)
        narrowed_bearer = await self._post(
            device_token.PARSE_PATH, {"text": receipt_text()},
            {"Authorization": f"Bearer {self.alice_device['token']}"},
        )
        self.assertEqual(narrowed_bearer.status, 403)
        with self.assertRaises(device_token.DeviceTokenError) as pair_guard:
            self.store.issue_code("alice")
        self.assertEqual(pair_guard.exception.code, "budget_forbidden")
        self.assertIsNone(self.store.redeem(self.pair_code["code"], "Narrowed"))
        with self.assertRaises(device_token.DeviceTokenError) as browser_guard:
            receipt_api._store(budget_fixture.SECRET).parse_text("alice", receipt_text())
        self.assertEqual(browser_guard.exception.code, "budget_forbidden")
        with self.assertRaises(device_token.DeviceTokenError) as bearer_guard:
            receipt_api._store(
                budget_fixture.SECRET, self.alice_device["token_id"],
            ).parse_text("alice", receipt_text())
        self.assertEqual(bearer_guard.exception.code, "budget_forbidden")
        self.assertEqual(self._state(), before)

        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptDeviceToken, self.alice_device["token_id"]).issued_permissions = json.dumps([
                "budget.receipts.add", "budget.receipts.view", "budget.receipts.edit",
            ])
            session.add(db.PermissionGrant(
                username="alice", permission_id="budget.receipts.match", target_owner="alice",
            ))
        before_mask_denial = self._state()
        narrowed_mask = await self._post(
            device_token.PARSE_PATH, {"text": receipt_text()},
            {"Authorization": f"Bearer {self.alice_device['token']}"},
        )
        self.assertEqual(narrowed_mask.status, 403)
        self.assertEqual(self._state(), before_mask_denial)

        full_profile = await self._post(
            device_token.LIST_RECEIPTS_PATH, {},
            {"Authorization": f"Bearer {self.bob_device['token']}"},
        )
        self.assertEqual(full_profile.status, 200)
        self.assertEqual((await full_profile.json())["receipts"], [])

        revoked = await self._post(
            device_token.REVOKE_PATH, {"token_id": self.alice_device["token_id"]},
            {"Authorization": f"Bearer {self.alice_device['token']}"},
        )
        self.assertEqual(revoked.status, 200)
        with self.sessions() as session:
            self.assertIsNotNone(
                session.get(db.BudgetReceiptDeviceToken, self.alice_device["token_id"]).revoked_at,
            )
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "delegate-device").revoked_at)
            self.assertEqual(session.get(db.BudgetReceipt, self.receipt_id).submitted_by, "viewer")


if __name__ == "__main__":
    unittest.main()
