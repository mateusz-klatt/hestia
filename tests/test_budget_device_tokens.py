"""Receipts device tokens: secrets at rest, pairing lockout, and bearer scope."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
            self.assertFalse(row.code_hmac == secret)
            self.assertFalse(secret in row.id)
        for row in self._tokens():
            self.assertRegex(row.token_hmac, r"^[0-9a-f]{64}$")
            self.assertFalse(row.token_hmac == secret)
            self.assertFalse(row.device_name == secret)
            self.assertFalse(row.id == secret)

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
        folded = device_token.normalize_code(" iL-o O Ab -23 ")
        self.assertTrue(folded == "11" + "00" + "AB23")
        self.assertEqual(len(folded), 8)
        for bad in (None, "", "short", "U" * 8, "!" * 8, 12, "Ａ" * 8):
            self.assertIsNone(device_token.normalize_code(bad))
        self.assertIsNone(device_token._device_name(""))
        self.assertIsNone(device_token._device_name("n" * 65))
        self.assertIsNone(device_token._device_name("bad\nname"))
        self.assertIsNone(device_token._device_name(None))
        self.assertEqual(device_token._device_name("n" * 64), "n" * 64)
        self.assertIsNone(device_token._bearer_token("%%%"))
        self.assertIsNone(device_token._bearer_token("x"))
        self.assertIsNone(device_token._bearer_token("a" * 129))
        canonical = base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode("ascii")
        self.assertIsNone(device_token._bearer_token(canonical[:-1] + "B"))
        self.assertFalse(device_token._accepted("abcd", "a" * 64))
        self.assertFalse(device_token._accepted(None, device_token._BLANK))
        self.assertTrue(device_token._accepted("ab" * 32, "ab" * 32))
        self.assertFalse(device_token.ids_match("aa", "ab"))
        self.assertFalse(device_token.ids_match("aa", "a"))
        self.assertFalse(device_token.ids_match("a", "é"))
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
        self.assertTrue(issued["qr_payload"] == "hestia-pair:" + issued["code"])
        self.assertEqual(len(issued["code"]), 8)
        self.assertEqual(issued["expires_at"], self.moment["now"] + device_token.PAIR_TTL)
        self.assertEqual(self._lock().failures, 0)
        self._absent(issued["code"])
        self.assertIsNone(self.store.redeem(issued["code"], "bad\nname"))
        self.assertIsNone(self._codes()[0].consumed_at)
        redeemed = self.store.redeem(issued["code"].lower(), "Vesta")
        self.assertEqual(redeemed["scope"], "receipts")
        self.assertEqual(len(redeemed["token"]), 43)
        expiry = datetime.fromisoformat(redeemed["expires_at"])
        self.assertEqual(expiry.tzinfo, timezone.utc)
        self.assertEqual(expiry.timestamp(), self.moment["now"] + device_token.SLIDE_TTL)
        self.assertIsNotNone(self._codes()[0].consumed_at)
        self._absent(redeemed["token"])
        self._absent(issued["code"])
        self.assertIsNone(self.store.redeem(issued["code"], "Vesta"))
        self.assertIsNone(self._tokens()[0].last_used_at)
        self.moment["now"] += 1
        found = self.store.identify(redeemed["token"], slide=True)
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["owner"], "alice")
        first_use = self.moment["now"]
        self.assertEqual(self._tokens()[0].last_used_at, first_use)
        self.moment["now"] += device_token.SLIDE_WRITE_INTERVAL - 1
        self.store.identify(redeemed["token"], slide=True)
        self.assertEqual(self._tokens()[0].last_used_at, first_use)
        self.moment["now"] += 1
        slid = self.store.identify(redeemed["token"], slide=True)
        self.assertEqual(slid["expires_at"], min(
            self.moment["now"] + device_token.SLIDE_TTL, self._tokens()[0].hard_expires_at))
        self.assertEqual(self._tokens()[0].last_used_at, self.moment["now"])
        self.store.identify(redeemed["token"], slide=False)
        self.assertEqual(self._tokens()[0].sliding_written_at, self.moment["now"])

    def test_slide_stops_at_hard_cap(self):
        started = self.moment["now"]
        issued = self.store.issue_code("alice")
        device = self.store.redeem(issued["code"], "Vesta")
        hard_expiry = started + device_token.HARD_TTL
        self.moment["now"] = started + device_token.SLIDE_TTL - 1
        self.assertEqual(self.store.identify(device["token"], slide=True)["expires_at"], hard_expiry - 1)
        self.moment["now"] += 2 * device_token.SLIDE_WRITE_INTERVAL
        self.assertEqual(self.store.identify(device["token"], slide=True)["expires_at"], hard_expiry)
        self.moment["now"] = hard_expiry
        self.assertEqual(self.store.identify(device["token"], slide=True)["status"], "expired")

    def test_parallel_redemption_is_single_use(self):
        issued = self.store.issue_code("alice")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = pool.map(lambda name: self.store.redeem(issued["code"], name), ("One", "Two"))
            self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self._tokens()), 1)

    def test_pair_limit_lockout_and_identical_rejection(self):
        expired = self.store.issue_code("alice")
        self.moment["now"] += device_token.PAIR_TTL
        alice = [self.store.issue_code("alice") for _ in range(3)]
        bob = self.store.issue_code("bob")
        with self.assertRaises(device_token.DeviceTokenError) as limited:
            self.store.issue_code("alice")
        self.assertEqual((limited.exception.status, limited.exception.code), (409, "device_pair_limit"))

        with mock.patch.object(device_token.hmac, "compare_digest", wraps=device_token.hmac.compare_digest) as compare:
            for attempt in (expired["code"], "U" * 8):
                compare.reset_mock()
                self.assertIsNone(self.store.redeem(attempt, "Vesta"))
                compare.assert_called_once()
                self.assertTrue(all(len(digest) == 64 for digest in compare.call_args.args))
        self.assertEqual(self._lock().failures, 2)
        self.assertEqual(self._lock("bob").failures, 2)
        for _ in range(device_token.LOCK_AFTER - 2):
            self.assertIsNone(self.store.redeem("U" * 8, "Vesta"))
        self.assertEqual(self._lock().locked, 1)
        self.assertEqual(self._lock("bob").locked, 1)
        self.assertIsNone(self.store.redeem(alice[0]["code"], "Vesta"))
        self.assertIsNone(self.store.redeem(bob["code"], "Vesta"))

        fresh = self.store.issue_code("alice")
        self.assertEqual(self._lock().locked, 0)
        for issued in alice:
            self.assertIsNone(self.store.redeem(issued["code"], "Vesta"))
        self.assertTrue(self.store.redeem(fresh["code"], "Vesta") is not None)
        self.assertIsNone(self.store.redeem(bob["code"], "Vesta"))

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
        unknown = base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode("ascii")
        self.assertEqual(self.store.identify(unknown, slide=True)["status"], "unknown")
        with self.sessions.begin() as session:
            session.get(db.User, "alice").role = "viewer"
        self.assertEqual(self.store.identify(redeemed["token"], slide=True)["status"], "revoked")
        self.assertIsNotNone(self._tokens()[0].revoked_at)
        headers = {"Authorization": "Bearer " + redeemed["token"]}
        decision = device_token.decide(
            "GET", device_token.DETAIL_PATH, headers, {}, self.secret, self.moment["now"])
        self.assertEqual((decision["status"], decision["code"]), (401, "device_token_revoked"))
        self.assertEqual(device_token.decide(
            "POST", device_token.REVOKE_PATH, headers, {}, self.secret, self.moment["now"])["action"], "accept")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceiptDeviceToken, redeemed["token_id"])
            row.revoked_at = None
            row.expires_at = self.moment["now"] - 1
            session.get(db.User, "alice").role = "admin"
        self.assertEqual(self.store.identify(redeemed["token"], slide=True)["status"], "expired")
        decision = device_token.decide(
            "GET", device_token.DETAIL_PATH, headers, {}, self.secret, self.moment["now"])
        self.assertEqual((decision["status"], decision["code"]), (401, "device_token_expired"))
        with self.sessions.begin() as session:
            row = session.get(db.BudgetReceiptDeviceToken, redeemed["token_id"])
            row.expires_at = self.moment["now"]
            row.hard_expires_at = self.moment["now"]
        self.assertEqual(self.store.identify(redeemed["token"], slide=False)["status"], "expired")
        listed = self.store.list_tokens("alice")
        self.assertEqual(set(listed[0]), {"token_id", "device_name", "created_at", "last_used_at", "expires_at"})
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
        with self.assertRaises(device_token.DeviceTokenError) as foreign:
            self.store.revoke("bob", redeemed["token_id"], actor_token_id=redeemed["token_id"])
        self.assertEqual(foreign.exception.status, 403)
        self.assertEqual(
            self.store.revoke("alice", redeemed["token_id"], actor_token_id=redeemed["token_id"]), {"ok": True})

    def test_self_revoke_is_audited_once(self):
        paired = self.store.issue_code("alice")
        device = self.store.redeem(paired["code"], "Vesta")
        token_id = device["token_id"]
        for _ in range(2):
            self.assertEqual(self.store.revoke("alice", token_id, actor_token_id=token_id), {"ok": True})
        with self.sessions() as session:
            rows = list(session.scalars(select(db.Audit)))
        self.assertEqual([(row.actor, row.action, row.target) for row in rows],
                         [("device:" + token_id, "receipt_device_revoke", token_id)])

    def test_expired_token_cannot_revoke_itself(self):
        paired = self.store.issue_code("alice")
        device = self.store.redeem(paired["code"], "Vesta")
        self.moment["now"] += device_token.SLIDE_TTL
        with self.assertRaises(device_token.DeviceTokenError) as expired:
            self.store.revoke("alice", device["token_id"], actor_token_id=device["token_id"])
        self.assertEqual((expired.exception.status, expired.exception.code), (401, "device_token_expired"))
        self.assertIsNone(self._tokens()[0].revoked_at)

    def test_open_store_requires_configured_sqlite_authentication(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            with self.assertRaises(BudgetUsersBackendRequired):
                device_token.open_store(self.secret)
        with mock.patch.object(store_sql, "is_users_db_authoritative", return_value=False):
            with self.assertRaises(BudgetUsersBackendRequired):
                device_token.open_store(self.secret)
        with self.assertRaises(BudgetConfigError):
            device_token.open_store(b"")
        with self.assertRaises(BudgetConfigError):
            device_token.open_store(None)
        self.assertGreater(device_token.open_store(self.secret).clock(), 0)

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
                mock.patch.object(device_token, "REDEEM_GLOBAL_LIMIT", 2):
            self.assertIsNone(device_token.redeem_limited("198.51.100.1", 10.0))
            self.assertGreaterEqual(device_token.redeem_limited("198.51.100.1", 10.0), 1)
            self.assertIsNone(device_token.redeem_limited("198.51.100.2", 10.0))
        device_token.reset_limits()
        with mock.patch.object(device_token, "REDEEM_IP_LIMIT", 10), \
                mock.patch.object(device_token, "REDEEM_GLOBAL_LIMIT", 2):
            self.assertIsNone(device_token.redeem_limited("198.51.100.2", 20.0))
            self.assertIsNone(device_token.redeem_limited("198.51.100.3", 20.0))
            self.assertGreaterEqual(device_token.redeem_limited("198.51.100.4", 20.0), 1)
        device_token.reset_limits()
        device_token._buckets.update({("seed", index): (0.0,) for index in range(device_token._MAX_BUCKETS)})
        self.assertEqual(device_token.redeem_limited("198.51.100.9", 1.0), device_token.REDEEM_WINDOW)
        self.assertIsNone(device_token.redeem_limited("198.51.100.9", device_token.REDEEM_WINDOW))
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
        device_headers = {"Authorization": "Bearer " + redeemed["token"]}
        with mock.patch.object(device_token, "open_store", side_effect=BudgetUsersBackendRequired()):
            missed = device_token.decide(
                "POST", device_token.PARSE_PATH, device_headers, {}, self.secret, self.moment["now"])
        self.assertEqual(missed["code"], "device_token_invalid")
        with mock.patch.object(device_token, "open_store", side_effect=BudgetConfigError("hidden")):
            configured = device_token.decide(
                "GET", "/api/whoami", device_headers, {}, self.secret, self.moment["now"])
        self.assertEqual(configured["action"], "pass")
        with mock.patch.object(device_token, "open_store", side_effect=RuntimeError("boom")), \
                self.assertLogs("hestia.budget.device_token", level="ERROR") as logs:
            failed = device_token.decide(
                "POST", device_token.PARSE_PATH, device_headers, {}, self.secret, self.moment["now"])
            ignored = device_token.decide(
                "GET", "/api/whoami", device_headers, {}, self.secret, self.moment["now"])
        self.assertEqual(failed["code"], "budget_unavailable")
        self.assertEqual(ignored["action"], "pass")
        self.assertFalse(redeemed["token"] in "\n".join(logs.output))
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
        self.assertIsNotNone(datetime.fromisoformat(body["expires_at"]).tzinfo)
        stored = json.dumps(self._stored_text())
        self.assertTrue(body["token"] not in stored)
        self.assertTrue(paired["code"] not in stored)
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
        detail = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": receipt["receipt_id"]},
                                       headers=self._auth(device))
        self.assertEqual(detail.status, 200)
        image = await self.client.get(receipt_api.PREFIX + "/image", params={"receipt_id": receipt["receipt_id"]},
                                      headers=self._auth(device))
        self.assertEqual(image.status, 404)
        self.assertEqual((await image.json())["error"], "budget_receipt_not_found")
        with self.sessions() as session:
            audit = session.scalar(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_id == receipt["receipt_id"],
                db.BudgetAnalyticsAudit.action == "confirm",
            ))
            self.assertEqual(json.loads(audit.after_json)["actor"], "device:" + device["token_id"])
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
        who = await self.client.get("/api/whoami", headers=self._auth(device))
        self.assertEqual(who.status, 403)

    async def test_login_bearer_cannot_access_budget_routes(self):
        password = "synthetic-login-secret"
        with self.sessions.begin() as session:
            session.get(db.User, "alice").password_hash = auth.hash_password(password)
        with mock.patch.object(web, "_audit"):
            login = await self._post("/api/login", {"user": "alice", "password": password, "bearer": True}, {})
        _status(login, 200)
        payload = await login.json()
        bearer = payload["token"]
        self.client.session.cookie_jar.clear()
        who = await self.client.get("/api/whoami", headers={"Authorization": "Bearer " + bearer})
        _status(who, 200)
        for route in self.client.server.app.router.routes():
            path = route.resource.canonical
            if not path.startswith(api.PREFIX + "/"):
                continue
            denied = await self.client.request(route.method, path, headers={"Authorization": "Bearer " + bearer})
            expected = 400 if path == device_token.REDEEM_PATH else 403
            self.assertEqual(denied.status, expected, f"{route.method} {path}")
            await denied.read()

    async def test_failures_are_generic_and_rate_limited(self):
        paired = await self._pair()
        unknown = await self._redeem("ZZZZZZZZ")
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
        locked = await self._pair()
        device_token.reset_limits()
        for _ in range(device_token.LOCK_AFTER):
            failure = await self._redeem("ZZZZZZZZ")
            self.assertEqual(failure.status, 400)
            self.assertEqual(await failure.json(), await unknown.json())
        rejected = await self._redeem(locked["code"])
        self.assertEqual(rejected.status, 400)
        self.assertEqual(await rejected.json(), await unknown.json())
        fresh = await self._pair()
        device_token.reset_limits()
        with mock.patch.object(device_token, "REDEEM_IP_LIMIT", 1):
            blocked = await self._redeem("ZZZZZZZZ")
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
        other = await self._device()
        foreign = await self._post(device_token.REVOKE_PATH, {"token_id": other["token_id"]}, self._auth(device))
        missing = await self._post(device_token.REVOKE_PATH, {"token_id": "33333333-3333-4333-8333-333333333333"},
                                   self._auth(device))
        garbage = await self._post(device_token.REVOKE_PATH, {"token_id": "nope"}, self._auth(device))
        self.assertEqual(foreign.status, 403)
        self.assertEqual(await foreign.json(), await missing.json())
        self.assertEqual(await garbage.json(), await missing.json())
        own = await self._post(device_token.REVOKE_PATH, {"token_id": device["token_id"]}, self._auth(device))
        _status(own, 200)
        self_repeat = await self._post(device_token.REVOKE_PATH, {"token_id": device["token_id"]}, self._auth(device))
        _status(self_repeat, 200)
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
        routes = {(route.method, route.resource.canonical.replace("{path}", "asset.js"))
                  for route in self.client.server.app.router.routes()}
        allowed = {("POST", receipt_api.PREFIX + "/" + operation)
                   for operation in ("parse-text", "confirm", "list", "search", "delete", "device-token/revoke")}
        allowed |= {("GET", receipt_api.PREFIX + "/" + operation) for operation in ("detail", "image")}
        self.assertEqual(device_token.ALLOWED, allowed)
        self.assertLessEqual(allowed, routes)
        for method, path in sorted(routes - allowed):
            response = await self.client.request(method, path, headers=self._auth(device), allow_redirects=False)
            self.assertEqual(response.status, 403, f"{method} {path}")
            await response.read()

    async def test_flag_off_ambiguous_and_auth_disabled(self):
        device = await self._device()
        with mock.patch.dict(os.environ, {receipt_api.FLAG: "0"}):
            for route in self.client.server.app.router.routes():
                path = route.resource.canonical
                if path.startswith(receipt_api.PREFIX + "/"):
                    response = await self.client.request(route.method, path, headers=self._auth(device))
                    self.assertEqual(response.status, 404, f"{route.method} {path}")
                    self.assertEqual((await response.json())["error"], "budget_receipts_disabled")
            off_scope = await self.client.get("/api/whoami", headers=self._auth(device))
            self.assertEqual(off_scope.status, 403)
        mixed = await self._post(receipt_api.PREFIX + "/parse-text", {"text": "x"},
                                 {**self.headers(origin=True), "Authorization": "Bearer nope"})
        self.assertEqual(mixed.status, 400)
        self.assertEqual((await mixed.json())["error"], "budget_auth_ambiguous")
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
