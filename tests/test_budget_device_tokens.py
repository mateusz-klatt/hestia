"""Receipts device tokens: secrets at rest, pairing lockout, and bearer scope."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from hestia import auth, db, store_sql, web
from hestia.budget import api, device_token, receipt_api
from hestia.budget.config import BudgetConfigError
from hestia.budget.views import BudgetUsersBackendRequired
from tests import test_budget_api as fixture
from tests.test_budget_receipts import confirm_values, receipt_text

SECRET = fixture.SECRET
ORIGIN = fixture.ORIGIN


def _status(response, expected):
    if response.status != expected:
        raise AssertionError(f"status {response.status} != {expected}")


class DeviceTokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.addCleanup(device_token.reset_limits)
        self.path = Path(self.directory.name) / "tokens.db"
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_DB": str(self.path), "HESTIA_PERSIST": "sqlite",
            device_token.FLAG: "1", "HESTIA_BUDGET_HTTP_ENABLED": "1",
            "HESTIA_BUDGET_PUBLIC_ORIGIN": ORIGIN,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"alice": "hash", "bob": "hash"})
        with self.sessions.begin() as session:
            session.add(db.User(username="viewer", password_hash="h", role="viewer"))
            session.add(db.User(username="off", password_hash="h", role="admin", disabled=True))
        self.moment = {"now": 1_700_000_000.0}
        self.secret = b"synthetic-device-token-secret"
        self.store = device_token.DeviceTokenStore(
            self.sessions, secret=self.secret, clock=lambda: self.moment["now"])

    def _codes(self):
        with self.sessions() as session:
            return list(session.scalars(select(db.BudgetReceiptPairCode)))

    def _tokens(self):
        with self.sessions() as session:
            return list(session.scalars(select(db.BudgetReceiptDeviceToken)))

    def _lock(self, owner="alice"):
        with self.sessions() as session:
            return session.get(db.BudgetReceiptPairLock, owner)

    def _absent(self, secret):
        for row in self._codes():
            self.assertRegex(row.code_hmac, r"^[0-9a-f]{64}$")
            self.assertNotEqual(row.code_hmac, secret)
            self.assertNotIn(secret, row.id)
        for row in self._tokens():
            self.assertRegex(row.token_hmac, r"^[0-9a-f]{64}$")
            self.assertNotEqual(row.token_hmac, secret)
            self.assertNotEqual(row.device_name, secret)
            self.assertNotEqual(row.id, secret)

    def test_constants_and_normalisation(self):
        self.assertEqual(device_token.PAIR_TTL, 300)
        self.assertEqual(device_token.MAX_ACTIVE_CODES, 3)
        self.assertEqual(device_token.LOCK_AFTER, 5)
        self.assertEqual(device_token.SLIDE_TTL, 90 * 86400)
        self.assertEqual(device_token.HARD_TTL, 180 * 86400)
        self.assertEqual(device_token.SLIDE_WRITE_INTERVAL, 3600)
        self.assertEqual((device_token.PARSE_LIMIT, device_token.PARSE_WINDOW), (30, 60))
        self.assertEqual((device_token.REDEEM_IP_LIMIT, device_token.REDEEM_GLOBAL_LIMIT, device_token.REDEEM_WINDOW),
                         (10, 30, 60))
        self.assertEqual(device_token.COOKIE, web._SESSION_COOKIE)
        self.assertEqual(normalize := device_token.normalize_code("iLoO1234"), "11001234")
        self.assertEqual(len(normalize), 8)
        for bad in (None, "", "short", "123456789", "!!!!!!!!", 12345678, "U2345678"):
            self.assertIsNone(device_token.normalize_code(bad))
        self.assertIsNone(device_token._device_name(""))
        self.assertIsNone(device_token._device_name("n" * 65))
        self.assertIsNone(device_token._device_name("bad\nname"))
        self.assertIsNone(device_token._device_name(None))
        self.assertEqual(device_token._device_name("n" * 64), "n" * 64)
        self.assertIsNone(device_token._bearer_token("%%%"))
        self.assertIsNone(device_token._bearer_token("a" * 129))
        self.assertFalse(device_token._accepted("abcd", "a" * 64))
        self.assertTrue(device_token._accepted("ab" * 32, "ab" * 32))
        self.assertFalse(device_token.ids_match("aa", "ab"))
        self.assertFalse(device_token.ids_match("aa", "a"))
        self.assertIsNone(device_token._bearer({"Authorization": "Basic x"}))
        self.assertIsNone(device_token._bearer({"Authorization": "Bearer "}))
        self.assertIsNone(device_token._bearer({"Authorization": "Bearer a b"}))
        self.assertIsNone(device_token._bearer(object()))
        self.assertEqual(device_token.client_ip(mock.Mock(remote=None)), "unknown")
        self.assertEqual(device_token.client_ip(mock.Mock(remote="")), "unknown")
        self.assertEqual(device_token.client_ip(mock.Mock(remote=5)), "unknown")
        self.assertEqual(device_token.client_ip(mock.Mock(remote="203.0.113.4")), "203.0.113.4")
        self.assertFalse(device_token.receipts_path(None))
        self.assertFalse(device_token.receipts_path("/api/budget/receipts"))
        self.assertTrue(device_token.receipts_path(device_token.PARSE_PATH))

    def test_issue_redeem_and_secrets_stay_hashed(self):
        issued = self.store.issue_code("alice")
        self.assertEqual(issued["qr_payload"], "hestia-pair:" + issued["code"])
        self.assertEqual(len(issued["code"]), 8)
        self.assertEqual(issued["expires_at"], self.moment["now"] + device_token.PAIR_TTL)
        self.assertEqual(self._lock().failures, 0)
        self._absent(issued["code"])
        self.assertIsNone(self.store.redeem(issued["code"], "bad\nname"))
        self.assertIsNone(self._codes()[0].consumed_at)
        redeemed = self.store.redeem(issued["code"].lower(), "Vesta")
        self.assertEqual(redeemed["scope"], "receipts")
        self.assertEqual(len(redeemed["token"]), 43)
        self.assertEqual(redeemed["expires_at"], self.moment["now"] + device_token.SLIDE_TTL)
        self.assertIsNotNone(self._codes()[0].consumed_at)
        self._absent(redeemed["token"])
        self._absent(issued["code"])
        self.assertIsNone(self.store.redeem(issued["code"], "Vesta"))
        found = self.store.identify(redeemed["token"], slide=True)
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["owner"], "alice")
        self.assertIsNone(self._tokens()[0].last_used_at)
        self.moment["now"] += device_token.SLIDE_WRITE_INTERVAL
        slid = self.store.identify(redeemed["token"], slide=True)
        self.assertEqual(slid["expires_at"], min(
            self.moment["now"] + device_token.SLIDE_TTL, self._tokens()[0].hard_expires_at))
        self.assertEqual(self._tokens()[0].last_used_at, self.moment["now"])
        self.store.identify(redeemed["token"], slide=False)
        self.assertEqual(self._tokens()[0].sliding_written_at, self.moment["now"])

    def test_pair_limit_lockout_and_identical_rejection(self):
        for _ in range(3):
            self.store.issue_code("alice")
        with self.assertRaises(device_token.DeviceTokenError) as limited:
            self.store.issue_code("alice")
        self.assertEqual((limited.exception.status, limited.exception.code), (409, "device_pair_limit"))
        with self.sessions.begin() as session:
            session.get(db.BudgetReceiptPairLock, "alice").locked = 1
        with self.assertRaises(device_token.DeviceTokenError):
            self.store.issue_code("alice")
        self.assertEqual(self._lock().locked, 1)
        self.moment["now"] += device_token.PAIR_TTL + 1
        fresh = self.store.issue_code("alice")
        self.assertEqual(self._lock().locked, 0)
        self.moment["now"] += device_token.PAIR_TTL + 1
        calls = {"n": 0}
        real = device_token._same

        def spy(left, right):
            calls["n"] += 1
            return real(left, right)

        with mock.patch.object(device_token, "_same", spy):
            unknown = self.store.redeem("ZZZZZZZZ", "Vesta")
            unknown_calls = calls["n"]
            expired = self.store.redeem(fresh["code"], "Vesta")
            expired_calls = calls["n"]
        self.assertIsNone(unknown)
        self.assertIsNone(expired)
        self.assertGreater(unknown_calls, 0)
        self.assertGreater(expired_calls, unknown_calls)
        self.assertEqual(self._lock().locked, 0)
        for _ in range(device_token.LOCK_AFTER - 1):
            self.assertIsNone(self.store.redeem(fresh["code"], "Vesta"))
        self.assertEqual(self._lock().locked, 1)
        held = self.store.issue_code("bob")
        self.moment["now"] += 1
        self.assertIsNone(self.store.redeem(fresh["code"], "Vesta"))
        self.assertIsNone(self._codes()[-1].consumed_at if False else
                          next(row.consumed_at for row in self._codes() if row.owner == "alice"
                               and row.expires_at == fresh["expires_at"]))
        self.assertIsNotNone(self.store.redeem(held["code"], "Vesta"))

    def test_owner_checks_revocation_and_expiry(self):
        with self.assertRaises(device_token.DeviceTokenError) as denied:
            self.store.issue_code("viewer")
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(device_token.DeviceTokenError):
            self.store.issue_code("off")
        with self.assertRaises(device_token.DeviceTokenError):
            self.store.issue_code("missing")
        self.store.clock = lambda: float("nan")
        with self.assertRaises(device_token.DeviceTokenError) as bad_clock:
            self.store.issue_code("alice")
        self.assertEqual(bad_clock.exception.status, 503)
        self.store.clock = lambda: self.moment["now"]
        issued = self.store.issue_code("alice")
        redeemed = self.store.redeem(issued["code"], "Vesta")
        self.assertEqual(self.store.identify("not-a-token", slide=True)["status"], "unknown")
        with self.sessions.begin() as session:
            session.get(db.User, "alice").role = "viewer"
        self.assertEqual(self.store.identify(redeemed["token"], slide=True)["status"], "revoked")
        self.assertIsNotNone(self._tokens()[0].revoked_at)
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceiptDeviceToken, redeemed["token_id"])
            row.revoked_at = None
            row.expires_at = self.moment["now"] - 1
            session.get(db.User, "alice").role = "admin"
        self.assertEqual(self.store.identify(redeemed["token"], slide=True)["status"], "expired")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceiptDeviceToken, redeemed["token_id"])
            row.expires_at = self.moment["now"] + 10
            row.hard_expires_at = self.moment["now"]
        self.assertEqual(self.store.identify(redeemed["token"], slide=False)["status"], "expired")
        listed = self.store.list_tokens("alice")
        self.assertEqual(set(listed[0]), {"token_id", "device_name", "created_at", "last_used_at", "expires_at"})
        self.assertEqual(self.store.revoke("alice", "not-a-uuid"), {"ok": False} if False else None)
        with self.assertRaises(device_token.DeviceTokenError) as bad_id:
            self.store.revoke("alice", "not-a-uuid")
        self.assertEqual(bad_id.exception.status, 400)
        self.assertEqual(self.store.revoke("alice", "22222222-2222-4222-8222-222222222222"), {"ok": True})
        self.assertEqual(self.store.revoke("alice", redeemed["token_id"]), {"ok": True})
        self.assertEqual(self.store.revoke("alice", redeemed["token_id"]), {"ok": True})
        self.assertEqual(self.store.list_tokens("alice"), [])
        with self.assertRaises(device_token.DeviceTokenError) as mismatch:
            self.store.revoke("alice", "33333333-3333-4333-8333-333333333333",
                              actor_token_id=redeemed["token_id"])
        self.assertEqual(mismatch.exception.status, 403)
        with self.assertRaises(device_token.DeviceTokenError):
            self.store.revoke("alice", redeemed["token_id"], actor_token_id=redeemed["token_id"])

    def test_unique_insert_retries_then_fails_closed(self):
        real = os.urandom
        sequence = [b"\x11" * 8, b"\x11" * 8, b"\x22" * 8]

        def sequenced(size):
            if size == 8 and sequence:
                return sequence.pop(0)
            return real(size)

        with mock.patch.object(device_token.os, "urandom", sequenced):
            self.store.issue_code("alice")
            self.store.issue_code("alice")
        self.assertEqual(len(self._codes()), 2)

        def stuck(size):
            if size == 8:
                return b"\x11" * 8
            return real(size)

        with mock.patch.object(device_token.os, "urandom", stuck):
            with self.assertRaises(device_token.DeviceTokenError) as failed:
                self.store.issue_code("alice")
        self.assertEqual(failed.exception.status, 503)
        self.assertEqual(len(self._codes()), 2)

    def test_password_role_and_disable_revoke_tokens(self):
        issued = self.store.issue_code("alice")
        redeemed = self.store.redeem(issued["code"], "Vesta")
        self.assertTrue(store_sql.set_user_password("alice", "next-hash", path=self.path))
        self.assertIsNotNone(self._tokens()[0].revoked_at)
        self.assertFalse(store_sql.set_user_password("ghost", "x", path=self.path))
        bob_code = self.store.issue_code("bob")
        bob = self.store.redeem(bob_code["code"], "Other")
        self.assertEqual(store_sql.set_user_role("bob", "admin", path=self.path), "ok")
        self.assertIsNone(next(row.revoked_at for row in self._tokens() if row.id == bob["token_id"]))
        self.assertEqual(store_sql.set_user_role("bob", "viewer", path=self.path), "ok")
        self.assertIsNotNone(next(row.revoked_at for row in self._tokens() if row.id == bob["token_id"]))
        self.assertEqual(store_sql.set_user_role("alice", "viewer", path=self.path), "last_admin")
        again = self.store.issue_code("alice")
        live = self.store.redeem(again["code"], "Vesta")
        self.assertEqual(store_sql.set_user_disabled("bob", True, path=self.path), "ok")
        self.assertEqual(store_sql.set_user_disabled("bob", False, path=self.path), "ok")
        self.assertIsNotNone(next(row.revoked_at for row in self._tokens() if row.id == bob["token_id"]))
        self.assertIsNone(next(row.revoked_at for row in self._tokens() if row.id == live["token_id"]))
        self.assertEqual(store_sql.set_user_disabled("alice", True, path=self.path), "last_admin")
        self.assertIsNone(next(row.revoked_at for row in self._tokens() if row.id == live["token_id"]))

    def test_rate_limit_windows_and_decide(self):
        device_token.reset_limits()
        with mock.patch.object(device_token, "PARSE_LIMIT", 1):
            self.assertIsNone(device_token.parse_limited("one", 1_000.0))
            self.assertGreaterEqual(device_token.parse_limited("one", 1_000.0), 1)
            self.assertIsNone(device_token.parse_limited("one", 1_000.0 + device_token.PARSE_WINDOW))
            self.assertIsNone(device_token.parse_limited("two", 1_000.0))
        device_token.reset_limits()
        with mock.patch.object(device_token, "REDEEM_IP_LIMIT", 1), \
                mock.patch.object(device_token, "REDEEM_GLOBAL_LIMIT", 5):
            self.assertIsNone(device_token.redeem_limited("198.51.100.1", 10.0))
            self.assertGreaterEqual(device_token.redeem_limited("198.51.100.1", 10.0), 1)
        device_token.reset_limits()
        with mock.patch.object(device_token, "REDEEM_IP_LIMIT", 10), \
                mock.patch.object(device_token, "REDEEM_GLOBAL_LIMIT", 2):
            self.assertIsNone(device_token.redeem_limited("198.51.100.2", 20.0))
            self.assertIsNone(device_token.redeem_limited("198.51.100.3", 20.0))
            self.assertGreaterEqual(device_token.redeem_limited("198.51.100.4", 20.0), 1)
        device_token.reset_limits()
        device_token._buckets.update({("seed", index): (0.0,) for index in range(device_token._MAX_BUCKETS)})
        self.assertEqual(device_token.redeem_limited("198.51.100.9", 1.0), device_token.REDEEM_WINDOW)
        issued = self.store.issue_code("alice")
        redeemed = self.store.redeem(issued["code"], "Vesta")
        session = auth.make_session("alice", now=self.moment["now"], secret=self.secret)
        headers = {"Authorization": "Bearer " + session}
        self.assertEqual(device_token.decide(
            "POST", device_token.PARSE_PATH, headers, {}, self.secret, self.moment["now"])["action"], "pass")
        self.assertEqual(device_token.decide(
            "POST", device_token.PARSE_PATH, {"Authorization": "Bearer " + redeemed["token"]},
            {device_token.COOKIE: "cookie"}, self.secret, self.moment["now"])["code"], "budget_auth_ambiguous")
        unknown = device_token.decide(
            "POST", device_token.PARSE_PATH, {"Authorization": "Bearer not-a-device-token"},
            {}, self.secret, self.moment["now"])
        self.assertEqual((unknown["status"], unknown["code"]), (401, "device_token_invalid"))
        other = device_token.decide(
            "GET", "/api/whoami", {"Authorization": "Bearer not-a-device-token"}, {}, self.secret, self.moment["now"])
        self.assertEqual(other["action"], "pass")
        forbidden = device_token.decide(
            "GET", "/api/whoami", {"Authorization": "Bearer " + redeemed["token"]}, {}, self.secret, self.moment["now"])
        self.assertEqual((forbidden["status"], forbidden["code"]), (403, "budget_forbidden"))
        accepted = device_token.decide(
            "POST", device_token.PARSE_PATH, {"Authorization": "Bearer " + redeemed["token"]},
            {}, self.secret, self.moment["now"])
        self.assertEqual(accepted["action"], "accept")
        self.assertEqual(accepted["token_id"], redeemed["token_id"])
        self.assertEqual(device_token.decide("GET", "/api/whoami", {}, {}, self.secret, self.moment["now"])["action"], "pass")
        self.assertEqual(device_token.decide("POST", device_token.PARSE_PATH, None, None, self.secret, 0)["action"], "pass")
        with mock.patch.object(device_token, "open_store", side_effect=BudgetUsersBackendRequired()):
            missed = device_token.decide(
                "POST", device_token.PARSE_PATH, {"Authorization": "Bearer not-a-device-token"},
                {}, self.secret, self.moment["now"])
        self.assertEqual(missed["code"], "device_token_invalid")
        with mock.patch.object(device_token, "open_store", side_effect=BudgetConfigError("hidden")):
            configured = device_token.decide(
                "GET", "/api/whoami", {"Authorization": "Bearer not-a-device-token"}, {}, self.secret, self.moment["now"])
        self.assertEqual(configured["action"], "pass")
        with mock.patch.object(device_token, "open_store", side_effect=RuntimeError("boom")), \
                self.assertLogs("hestia.budget.device_token", level="ERROR") as logs:
            failed = device_token.decide(
                "POST", device_token.PARSE_PATH, {"Authorization": "Bearer not-a-device-token"},
                {}, self.secret, self.moment["now"])
            ignored = device_token.decide(
                "GET", "/api/whoami", {"Authorization": "Bearer not-a-device-token"},
                {}, self.secret, self.moment["now"])
        self.assertEqual(failed["code"], "budget_unavailable")
        self.assertEqual(ignored["action"], "pass")
        self.assertNotIn("not-a-device-token", "\n".join(logs.output))
        with mock.patch.object(device_token.DeviceTokenStore, "identify", return_value={"status": "weird"}):
            weird = device_token.decide(
                "POST", device_token.PARSE_PATH, {"Authorization": "Bearer " + redeemed["token"]},
                {}, self.secret, self.moment["now"])
        self.assertEqual(weird["code"], "device_token_invalid")
        response = device_token.json_error({"status": 429, "code": "device_token_rejected", "retry": 3})
        self.assertEqual(response.headers["Retry-After"], "3")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("device_token_rejected", response.text)
        with mock.patch.dict(os.environ, {device_token.FLAG: "0"}):
            self.assertFalse(device_token.receipts_enabled())
            self.assertEqual(device_token.disabled_response().status, 404)


class DeviceTokenHTTPTests(fixture.unittest.IsolatedAsyncioTestCase):
    rows = fixture.BudgetAPITests.rows
    token = fixture.BudgetAPITests.token
    headers = fixture.BudgetAPITests.headers
    assert_private_headers = fixture.BudgetAPITests.assert_private_headers

    async def asyncSetUp(self):
        patch = mock.patch.dict(os.environ, {receipt_api.FLAG: "1", "HESTIA_BUDGET_JOBS_ENABLED": "0",
                                             "HESTIA_BUDGET_POLLER_ENABLED": "0", "HESTIA_BUDGET_CONSENT_ENABLED": "0"})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(device_token.reset_limits)
        await fixture.BudgetAPITests.asyncSetUp(self)

    async def _post(self, path, body, headers):
        return await self.client.post(path, data=json.dumps(body), headers={
            "Content-Type": "application/json", **headers})

    async def _pair(self):
        response = await self._post(device_token.PAIR_PATH, {}, self.headers(origin=True))
        _status(response, 200)
        self.assert_private_headers(response)
        return await response.json()

    async def _redeem(self, code, name="Vesta", headers=None):
        response = await self._post(device_token.REDEEM_PATH, {"code": code, "device_name": name}, headers or {})
        return response

    async def _device(self):
        paired = await self._pair()
        response = await self._redeem(paired["code"])
        _status(response, 200)
        body = await response.json()
        self.assertEqual(body["scope"], "receipts")
        self.assertNotIn(body["token"], json.dumps(self._stored_text()))
        self.assertNotIn(paired["code"], json.dumps(self._stored_text()))
        return body

    def _stored_text(self):
        with self.sessions() as session:
            codes = [row.code_hmac for row in session.scalars(select(db.BudgetReceiptPairCode))]
            tokens = [(row.token_hmac, row.device_name, row.id)
                      for row in session.scalars(select(db.BudgetReceiptDeviceToken))]
        return {"codes": codes, "tokens": tokens}

    def _auth(self, device):
        return {"Authorization": "Bearer " + device["token"]}

    async def test_browser_pair_and_device_scope(self):
        device = await self._device()
        listed = await self._post(device_token.LIST_PATH, {}, self.headers(origin=True))
        _status(listed, 200)
        rows = await listed.json()
        self.assertEqual(rows[0]["token_id"], device["token_id"])
        self.assertEqual(rows[0]["device_name"], "Vesta")
        self.assertIsNone(rows[0]["last_used_at"])
        parsed = await self._post(receipt_api.PREFIX + "/parse-text",
                                  {"text": receipt_text(), "source_kind": "local_ocr"}, self._auth(device))
        _status(parsed, 200)
        draft = await parsed.json()
        with self.sessions() as session:
            row = session.get(db.BudgetReceipt, draft["receipt_id"])
            self.assertEqual(row.source_kind, "local_ocr")
            audit = next(item for item in session.scalars(select(db.BudgetAnalyticsAudit))
                         if item.subject_id == draft["receipt_id"] and item.action == "create")
            self.assertEqual(json.loads(audit.after_json)["actor"], "device:" + device["token_id"])
        confirmed = await self._post(receipt_api.PREFIX + "/confirm", confirm_values(draft), self._auth(device))
        _status(confirmed, 200)
        receipt = (await confirmed.json())["receipt"]
        removed = await self._post(receipt_api.PREFIX + "/delete",
                                   {"receipt_id": receipt["receipt_id"], "expected_version": receipt["version"]},
                                   self._auth(device))
        _status(removed, 200)
        with self.sessions() as session:
            house = [item for item in session.scalars(select(db.Audit)) if item.action == "receipt_delete"]
        self.assertEqual(house[0].actor, "device:" + device["token_id"])
        found = await self._post(receipt_api.PREFIX + "/list", {}, self._auth(device))
        _status(found, 200)
        searched = await self._post(receipt_api.PREFIX + "/search", {"query": "mleko"}, self._auth(device))
        _status(searched, 200)
        detail = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": draft["receipt_id"]},
                                       headers=self._auth(device))
        self.assertEqual(detail.status, 404)
        image = await self.client.get(receipt_api.PREFIX + "/image", params={"receipt_id": draft["receipt_id"]},
                                      headers=self._auth(device))
        self.assertEqual(image.status, 403)
        who = await self.client.get("/api/whoami", headers=self._auth(device))
        self.assertEqual(who.status, 403)

    async def test_login_bearer_cannot_write_receipts(self):
        password = "synthetic-login-secret"
        with self.sessions.begin() as session:
            session.get(db.User, "alice").password_hash = auth.hash_password(password)
        login = await self._post("/api/login", {"user": "alice", "password": password, "bearer": True}, {})
        _status(login, 200)
        payload = await login.json()
        bearer = payload["token"]
        self.client.session.cookie_jar.clear()
        who = await self.client.get("/api/whoami", headers={"Authorization": "Bearer " + bearer})
        _status(who, 200)
        denied = await self._post(receipt_api.PREFIX + "/parse-text", {"text": receipt_text()},
                                  {"Authorization": "Bearer " + bearer})
        self.assertEqual(denied.status, 403)
        self.assertEqual((await denied.json())["error"], "budget_browser_session_required")

    async def test_failures_are_generic_and_rate_limited(self):
        paired = await self._pair()
        unknown = await self._redeem("ZZZZZZZZ")
        self.moment = None
        expired_code = paired["code"]
        with self.sessions.begin() as session:
            session.scalars(select(db.BudgetReceiptPairCode)).first().expires_at = 1
        expired = await self._redeem(expired_code)
        self.assertEqual(unknown.status, 400)
        self.assertEqual(expired.status, 400)
        self.assertEqual(await unknown.json(), await expired.json())
        self.assertEqual((await unknown.json())["error"], "device_token_rejected")
        bad_name = await self._redeem(expired_code, "\n")
        self.assertEqual(await bad_name.json(), await unknown.json())
        both = await self._post(device_token.REDEEM_PATH, {"code": "ZZZZZZZZ", "device_name": "Vesta"},
                                {"Authorization": "Bearer nope", "Cookie": "hestia_session=nope"})
        self.assertEqual(both.status, 400)
        self.assertEqual((await both.json())["error"], "budget_auth_ambiguous")
        cookie_only = await self._redeem("ZZZZZZZZ", headers={"Cookie": "hestia_session=nope"})
        self.assertEqual(await cookie_only.json(), await unknown.json())
        device_token.reset_limits()
        with mock.patch.object(device_token, "REDEEM_IP_LIMIT", 1):
            first = await self._redeem("ZZZZZZZZ")
            second = await self._redeem("YYYYYYYY")
        self.assertEqual(first.status, 400)
        self.assertEqual(second.status, 429)
        self.assertEqual((await second.json())["error"], "device_token_rejected")
        self.assertGreaterEqual(int(second.headers["Retry-After"]), 1)
        fresh = await self._pair()
        device_token.reset_limits()
        with mock.patch.object(device_token, "REDEEM_IP_LIMIT", 1):
            blocked = await self._redeem(fresh["code"])
            self.assertEqual(blocked.status, 400)
            limited = await self._redeem(fresh["code"])
        self.assertEqual(limited.status, 429)
        device_token.reset_limits()
        redeemed = await self._redeem(fresh["code"])
        _status(redeemed, 200)
        body = await redeemed.json()
        device_token.reset_limits()
        with mock.patch.object(device_token, "PARSE_LIMIT", 2):
            for _ in range(2):
                ok = await self._post(receipt_api.PREFIX + "/parse-text", {"text": receipt_text()},
                                      {"Authorization": "Bearer " + body["token"]})
                _status(ok, 200)
                await ok.release()
            limited_parse = await self._post(receipt_api.PREFIX + "/parse-text", {"text": receipt_text()},
                                             {"Authorization": "Bearer " + body["token"]})
        self.assertEqual(limited_parse.status, 429)
        self.assertEqual((await limited_parse.json())["error"], "device_token_rate_limited")
        self.assertIn("Retry-After", limited_parse.headers)

    async def test_revoke_is_idempotent_and_self_scoped(self):
        device = await self._device()
        other = "22222222-2222-4222-8222-222222222222"
        foreign = await self._post(device_token.REVOKE_PATH, {"token_id": other}, self._auth(device))
        missing = await self._post(device_token.REVOKE_PATH, {"token_id": "33333333-3333-4333-8333-333333333333"},
                                   self._auth(device))
        garbage = await self._post(device_token.REVOKE_PATH, {"token_id": "nope"}, self._auth(device))
        self.assertEqual(foreign.status, 403)
        self.assertEqual(await foreign.json(), await missing.json())
        self.assertEqual(await garbage.json(), await missing.json())
        own = await self._post(device_token.REVOKE_PATH, {"token_id": device["token_id"]}, self._auth(device))
        _status(own, 200)
        again = await self._post(device_token.REVOKE_PATH, {"token_id": device["token_id"]}, self.headers(origin=True))
        _status(again, 200)
        used = await self._post(receipt_api.PREFIX + "/list", {}, self._auth(device))
        self.assertEqual(used.status, 401)
        self.assertEqual((await used.json())["error"], "device_token_revoked")
        browser_bad = await self._post(device_token.REVOKE_PATH, {"token_id": "nope"}, self.headers(origin=True))
        self.assertEqual(browser_bad.status, 400)
        self.assertEqual((await browser_bad.json())["error"], "budget_invalid_request")

    async def test_route_map_rejects_a_device_token_off_the_allowlist(self):
        device = await self._device()
        seen = set()
        app = self.client.server.app
        for route in app.router.routes():
            path = route.resource.canonical
            if "{path}" in path:
                path = path.replace("{path}", "asset.js")
            method = route.method
            if method not in {"GET", "POST", "HEAD", "PUT", "PATCH", "DELETE"}:
                continue
            headers = self._auth(device)
            data = b"{}" if method == "POST" else None
            if method == "POST":
                headers = {**headers, "Content-Type": "application/json"}
            response = await self.client.request(method, path, headers=headers, data=data)
            key = (method, path)
            if key in device_token.ALLOWED:
                self.assertNotEqual(response.status, 403)
                seen.add(key)
            else:
                self.assertEqual(response.status, 403)
            await response.read()
        self.assertEqual(seen, device_token.ALLOWED)

    async def test_flag_off_ambiguous_and_auth_disabled(self):
        for path in (device_token.PAIR_PATH, device_token.REDEEM_PATH, device_token.LIST_PATH,
                     device_token.REVOKE_PATH, receipt_api.PREFIX + "/parse-text"):
            with mock.patch.dict(os.environ, {receipt_api.FLAG: "0"}):
                response = await self._post(path, {}, {})
            self.assertEqual(response.status, 404)
            self.assertEqual((await response.json())["error"], "budget_receipts_disabled")
        mixed = await self._post(receipt_api.PREFIX + "/parse-text", {"text": "x"},
                                 {**self.headers(origin=True), "Authorization": "Bearer nope"})
        self.assertEqual(mixed.status, 400)
        self.assertEqual((await mixed.json())["error"], "budget_auth_ambiguous")
        account = await self.client.post(api.PREFIX + "/account-selection", data=b"{}", headers={
            **self.headers(origin=True), "Authorization": "Bearer nope", "Content-Type": "application/json"})
        self.assertEqual(account.status, 403)
        device = await self._device()
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            opened = await self._post(receipt_api.PREFIX + "/parse-text", {"text": receipt_text()}, self._auth(device))
        self.assertEqual(opened.status, 401)
        with mock.patch.object(receipt_api, "enabled", return_value=False):
            disabled = await self._post(device_token.PAIR_PATH, {}, self.headers(origin=True))
        self.assertEqual(disabled.status, 404)

    async def test_expired_token_and_post_body_recheck(self):
        device = await self._device()
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceiptDeviceToken, device["token_id"])
            row.expires_at = 1
            row.hard_expires_at = 1
        expired = await self._post(receipt_api.PREFIX + "/list", {}, self._auth(device))
        self.assertEqual(expired.status, 401)
        self.assertEqual((await expired.json())["error"], "device_token_expired")
        who = await self.client.get("/api/whoami", headers=self._auth(device))
        self.assertEqual(who.status, 403)
        self.assertEqual(await who.read(), b"")
