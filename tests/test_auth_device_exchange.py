import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from hestia import auth, authz, db, store_sql, web
from hestia.budget import device_token


class DeviceExchangeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.temp.name) / "exchange.db"
        self.secret = b"native-device-exchange-secret"
        self.key_path = Path(self.temp.name) / "rule.key"
        self.key_path.write_bytes(bytes(range(32)))
        self.key_path.chmod(0o600)
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path),
            "HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_BUDGET_RECEIPTS_ENABLED": "1",
            device_token._RULE_KEY_FILE: str(self.key_path),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"owner": "h", "other": "h2"})
        store_sql.activate_catalog_compat(path=self.path)
        store_sql.activate_sessions_v2(path=self.path)
        self.moment = 1_700_000_000.0
        self.store = device_token.DeviceTokenStore(
            self.sessions, clock=lambda: self.moment, session_secret=self.secret,
        )

    def exchange(self, actor="owner", name="Vesta", prior=None):
        return self.store.exchange(actor, name, prior, generation=0)

    def test_self_exchange_native_read_and_persistent_narrowing(self):
        issued = self.exchange()
        self.assertEqual(set(issued), {
            "token", "token_id", "scope", "actor", "owner", "owner_key", "issued_permissions", "expires_at",
        })
        self.assertEqual((issued["actor"], issued["owner"], issued["scope"]), ("owner", "owner", "device"))
        self.assertTrue(issued["token"].startswith(device_token.DEVICE_TOKEN_PREFIX))
        self.assertNotIn("owner", issued["owner_key"])
        self.assertEqual(len(issued["owner_key"]), 43)
        self.assertEqual(issued["owner_key"], device_token.owner_key(self.secret, "owner"))
        self.assertIn("budget.view", issued["issued_permissions"])
        found = self.store.identify(issued["token"], slide=False)
        self.assertEqual(found["status"], "ok")
        self.assertEqual(set(found["permissions"]), set(issued["issued_permissions"]))
        with self.sessions.begin() as session:
            row = session.scalar(select(db.PermissionGrant).where(
                db.PermissionGrant.username == "owner",
                db.PermissionGrant.permission_id == "budget.receipts.edit",
            ))
            session.delete(row)
        narrowed = self.store.identify(issued["token"], slide=False)
        self.assertNotIn("budget.receipts.edit", narrowed["permissions"])
        with self.sessions() as session:
            row = session.get(db.BudgetReceiptDeviceToken, issued["token_id"])
            self.assertNotIn("budget.receipts.edit", json.loads(row.issued_permissions))
            alerts = session.scalar(select(func.count()).select_from(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.action == device_token.NEW_DEVICE_LOGIN,
            ))
            self.assertEqual(alerts, 1)
            alert = session.scalar(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_id == issued["token_id"],
                db.BudgetAnalyticsAudit.action == device_token.NEW_DEVICE_LOGIN,
            ))
            self.assertEqual(json.loads(alert.after_json), {
                "actor": "owner", "device_name": "Vesta", "token_id": issued["token_id"],
            })
        with self.sessions.begin() as session:
            session.add(db.PermissionGrant(
                username="owner", permission_id="budget.receipts.edit", target_owner="owner",
            ))
        rotated = self.exchange(name="Renamed", prior=issued["token"])
        self.assertEqual(rotated["token_id"], issued["token_id"])
        self.assertNotIn("budget.receipts.edit", rotated["issued_permissions"])
        self.assertNotEqual(rotated["token"], issued["token"])
        self.assertEqual(self.store.identify(issued["token"], slide=False)["status"], "unknown")
        self.assertEqual(self.store.identify(rotated["token"], slide=False)["status"], "ok")
        with self.sessions() as session:
            alerts = session.scalar(select(func.count()).select_from(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.action == device_token.NEW_DEVICE_LOGIN,
            ))
            self.assertEqual(alerts, 1)

    def test_slots_invalid_proofs_and_empty_intersection(self):
        for index in range(5):
            self.exchange(name=f"Vesta {index}")
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.exchange(name="Sixth")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "device_token_limit_reached"))
        other = self.exchange("other", "Other")
        for proof in (None, "bad", "A" * 43, other["token"]):
            if proof is None:
                continue
            with self.subTest(proof=proof), self.assertRaises(device_token.DeviceTokenError) as caught:
                self.exchange("owner", "Proof", proof)
            self.assertEqual(caught.exception.code, "device_token_prior_invalid")
        self.assertEqual(self.store.identify(other["token"], slide=False)["status"], "ok")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptDeviceToken, other["token_id"]).revoked_at = self.moment
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.exchange("other", "Revoked", other["token"])
        self.assertEqual(caught.exception.code, "device_token_prior_invalid")
        fresh = self.store.exchange("other", "Fresh", generation=0)
        self.moment = fresh_time = self.moment + device_token.HARD_TTL
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.store.exchange("other", "Expired", fresh["token"], generation=0)
        self.assertEqual(caught.exception.code, "device_token_prior_invalid")
        self.moment = fresh_time - device_token.HARD_TTL
        depleted = self.exchange("other", "Depleted")
        with self.sessions.begin() as session:
            session.execute(db.PermissionGrant.__table__.delete().where(
                db.PermissionGrant.username == "other",
                db.PermissionGrant.target_owner == "other",
            ))
        denied = self.store.identify(depleted["token"], slide=False)
        self.assertEqual(denied["status"], "forbidden")
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.BudgetReceiptDeviceToken, depleted["token_id"]).revoked_at)
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.exchange("other", "No grants")
        self.assertEqual(caught.exception.code, "budget_forbidden")

    def test_legacy_migration_and_read_allowlist(self):
        legacy = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        with self.sessions.begin() as session:
            session.add(db.BudgetReceiptDeviceToken(
                id="11111111-1111-4111-8111-111111111111", owner="owner", actor=None,
                device_name="Legacy", token_hmac=device_token._mac(bytes(range(32)), device_token._TOKEN_DOMAIN, legacy),
                created_at=self.moment, expires_at=self.moment + 1000, hard_expires_at=self.moment + 2000,
                sliding_written_at=self.moment,
            ))
        migrated = self.exchange(prior=legacy)
        self.assertEqual(migrated["token_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(migrated["scope"], "receipts")
        self.assertNotIn("budget.view", migrated["issued_permissions"])
        self.assertEqual(self.store.identify(legacy, slide=False)["status"], "unknown")
        with mock.patch.object(device_token, "_rule_key", side_effect=device_token.BudgetConfigError("missing")):
            with self.assertRaises(device_token.DeviceTokenError) as caught:
                self.exchange(prior=legacy)
            self.assertEqual(caught.exception.code, "device_token_prior_invalid")
        native = self.exchange(name="Native")
        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptDeviceToken, native["token_id"]).issued_permissions = "[]"
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.exchange(prior=native["token"])
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "budget_forbidden"))
        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptDeviceToken, native["token_id"]).issued_permissions = json.dumps(
                native["issued_permissions"],
            )
        with mock.patch.dict(os.environ, {"HESTIA_SESSION_SECRET": self.secret.decode()}):
            for route in sorted(device_token.NATIVE_READ_ALLOWED):
                with self.subTest(route=route):
                    allowed = device_token.decide(*route, {"Authorization": "Bearer " + native["token"]},
                        {}, self.secret, self.moment)
                    self.assertEqual(allowed["action"], "accept")
            denied = device_token.decide("GET", "/api/budget/status", {"Authorization": "Bearer " + migrated["token"]},
                {}, self.secret, self.moment)
            self.assertEqual((denied["action"], denied["code"]), ("response", "budget_forbidden"))
            forbidden = device_token.decide("POST", "/api/budget/refresh", {"Authorization": "Bearer " + migrated["token"]},
                {}, self.secret, self.moment)
            self.assertEqual(forbidden["status"], 403)

    def test_exchange_validation_actor_state_and_session_generation(self):
        for name in (None, "", "x" * 65, "bad\n"):
            with self.subTest(name=name), self.assertRaises(device_token.DeviceTokenError) as caught:
                self.exchange(name=name)
            self.assertEqual(caught.exception.code, "budget_invalid_request")
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.store.exchange("ghost", "Vesta", generation=0)
        self.assertEqual(caught.exception.code, "budget_forbidden")
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(device_token.DeviceTokenError):
            self.exchange()
        with self.sessions.begin() as session:
            user = session.get(db.User, "owner")
            user.disabled = False
            user.credential_generation = 1
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.exchange()
        self.assertEqual(caught.exception.status, 401)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, store_sql._SESSION_PHASE_KEY).value = store_sql.SESSION_PHASE_LEGACY
        with self.assertRaises(device_token.DeviceTokenError) as caught:
            self.store.exchange("owner", "Vesta", generation=1)
        self.assertEqual(caught.exception.status, 401)
        empty_secret = device_token.DeviceTokenStore(self.sessions, clock=lambda: self.moment)
        with self.assertRaises(device_token.DeviceTokenError):
            empty_secret.exchange("owner", "Vesta", generation=1)


class DeviceExchangeWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.temp.name) / "exchange-web.db"
        self.secret = b"exchange-web-secret"
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path),
            "HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_BUDGET_RECEIPTS_ENABLED": "1",
            "HESTIA_BUDGET_ANALYTICS_ENABLED": "1",
            "HESTIA_PUBLIC_ORIGIN": "https://hestia.example",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for patch in (mock.patch.object(web, "_AUTH_ENABLED", True),
                      mock.patch.object(web, "_SESSION_SECRET", self.secret)):
            patch.start()
            self.addCleanup(patch.stop)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"owner": auth.hash_password("owner password")})
        store_sql.add_user("viewer", auth.hash_password("viewer password"), "viewer", path=self.path)
        store_sql.activate_catalog_compat(path=self.path)
        store_sql.activate_sessions_v2(path=self.path)
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace(audit_engine=None))))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        login = await self.client.post("/api/login", json={
            "user": "owner", "password": "owner password", "bearer": True,
        })
        self.token = (await login.json())["token"]
        self.client.session.cookie_jar.clear()

    async def test_native_http_exchange_features_and_budget_read(self):
        response = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
            headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertEqual(result["scope"], "device")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        features = await (await self.client.get("/api/auth/features")).json()
        self.assertIn("device_exchange", features["features"])
        self.assertIn("budget_native_read", features["features"])
        for method, path in sorted(device_token.NATIVE_READ_ALLOWED):
            params = ({"period": "2026-11", "currency": "PLN"}
                      if path.startswith("/api/budget/analytics/") and not path.endswith("/categories") else None)
            read = await self.client.request(method, path, params=params,
                headers={"Authorization": "Bearer " + result["token"]})
            self.assertEqual(read.status, 200, f"{path}: {await read.text()}")
        forbidden = await self.client.post("/api/budget/refresh", json={},
            headers={"Authorization": "Bearer " + result["token"]})
        self.assertEqual(forbidden.status, 403)
        ambiguous = await self.client.get("/api/budget/status", headers={
            "Authorization": "Bearer " + result["token"], "Cookie": "hestia_session=invalid",
        })
        self.assertEqual(ambiguous.status, 400)

    async def test_native_requirement_input_flags_and_forbidden_actor(self):
        browser = await self.client.post("/api/login", json={"user": "owner", "password": "owner password"})
        cookie = browser.cookies["hestia_session"].value
        self.client.session.cookie_jar.clear()
        for headers in ({"Cookie": f"hestia_session={cookie}"}, {
            "Authorization": "Bearer " + auth.make_session("owner", now=web._now(), secret=self.secret),
        }):
            response = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"}, headers=headers)
            self.assertEqual(response.status, 401)
            self.assertEqual(await response.json(), {"error": {"code": "native_session_required"}})
        for body in ({}, {"device_name": "Vesta", "extra": True}, {"device_name": "Vesta", "prior_token": 1}):
            response = await self.client.post(device_token.EXCHANGE_PATH, json=body,
                headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(response.status, 400)
        malformed = await self.client.post(device_token.EXCHANGE_PATH, data="{", headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
        })
        self.assertEqual(malformed.status, 400)
        from hestia.budget.views import BudgetUsersBackendRequired
        with mock.patch.object(device_token, "open_store", side_effect=BudgetUsersBackendRequired):
            unavailable = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
                headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(unavailable.status, 403)
        with mock.patch.object(device_token, "open_store", side_effect=authz.PolicyPhaseUnsupported("future")):
            incompatible = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
                headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(incompatible.status, 503)
        with mock.patch.object(store_sql, "authorization_snapshot", side_effect=store_sql.SessionPhaseUnsupported("future")):
            features = await (await self.client.get("/api/auth/features")).json()
            self.assertNotIn("device_exchange", features["features"])
            self.assertIsNone(features["qr_origin"])
        viewer = await self.client.post("/api/login", json={
            "user": "viewer", "password": "viewer password", "bearer": True,
        })
        viewer_token = (await viewer.json())["token"]
        denied = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
            headers={"Authorization": f"Bearer {viewer_token}"})
        self.assertEqual(denied.status, 403)
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_RECEIPTS_ENABLED": "0"}):
            disabled = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
                headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(disabled.status, 404)
            features = await (await self.client.get("/api/auth/features")).json()
            self.assertNotIn("device_exchange", features["features"])
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "0"}):
            disabled = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
                headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(disabled.status, 404)

    async def test_native_expiry_during_upload_cannot_issue(self):
        expiring = auth.make_session(
            "owner", now=web._now(), secret=self.secret, ttl=30,
            generation=0, purpose=auth.SESSION_PURPOSE_NATIVE,
        )
        now = web._now()
        with mock.patch.object(web, "_now", side_effect=[now, now, now + 31]):
            response = await self.client.post(device_token.EXCHANGE_PATH, json={"device_name": "Vesta"},
                headers={"Authorization": f"Bearer {expiring}"})
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), {"error": {"code": "native_session_required"}})
        with self.sessions() as session:
            self.assertEqual(session.query(db.BudgetReceiptDeviceToken).count(), 0)


if __name__ == "__main__":
    unittest.main()
