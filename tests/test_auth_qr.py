import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from sqlalchemy import select

from hestia import auth, auth_qr, authz, db, store_sql, web
from hestia.budget import consent_context, device_token


class QrLoginStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.temp.name) / "qr.db"
        self.secret = b"qr-login-secret-not-budget"
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"owner": "hash", "other": "hash2"})
        store_sql.activate_sessions_v2(path=self.path)
        self.origin = "https://Hestia.Example:443"
        self.canonical = "https://hestia.example"

    def test_issue_is_hmac_only_capped_and_redeem_is_single_use_native(self):
        with mock.patch.object(device_token, "_rule_key", side_effect=AssertionError("budget key used")):
            payloads = [auth_qr.issue("owner", self.canonical, 0, now=100 + i, secret=self.secret) for i in range(4)]
        self.assertTrue(all(value.startswith("hestia-login:https://hestia.example#") for value in payloads))
        with self.sessions() as session:
            rows = session.scalars(select(db.NativeLoginCode)).all()
            active = [row for row in rows if row.consumed_at is None]
            self.assertEqual(len(active), 3)
            self.assertNotEqual(rows[0].code_hmac, rows[1].code_hmac)
            self.assertNotIn(payloads[-1].split("#")[1], json.dumps([row.code_hmac for row in rows]))
        result = auth_qr.redeem(self.canonical, payloads[-1].split("#")[1], now=200, secret=self.secret)
        self.assertEqual(result["user"], "owner")
        envelope = auth.decode_session(result["token"], now=200, secret=self.secret)
        self.assertEqual((envelope.version, envelope.purpose, envelope.generation), (2, "native_qr", 0))
        legacy_auth = types.ModuleType("hestia_auth_pr2")
        source = subprocess.check_output(
            ["git", "show", "3932e18:hestia/auth.py"],
            cwd=Path(__file__).resolve().parent.parent, text=True,
        )
        sys.modules[legacy_auth.__name__] = legacy_auth
        try:
            exec(compile(source, "pr2-auth.py", "exec"), legacy_auth.__dict__)
            self.assertIsNone(legacy_auth.decode_session(result["token"], now=200, secret=self.secret))
        finally:
            del sys.modules[legacy_auth.__name__]
        self.assertIsNone(auth_qr.redeem(self.canonical, payloads[-1].split("#")[1], now=201, secret=self.secret))
        self.assertIsNone(auth_qr.redeem("https://other.example", payloads[-2].split("#")[1], now=201, secret=self.secret))
        self.assertIsNone(auth_qr.redeem(self.canonical, "A" * 19, now=201, secret=self.secret))

    def test_redeem_records_one_private_owner_inbox_event(self):
        store_sql.activate_catalog_compat(path=self.path)
        payload = auth_qr.issue("owner", self.canonical, 0, now=100, secret=self.secret)
        result = auth_qr.redeem(self.canonical, payload.split("#")[1], now=200, secret=self.secret)
        self.assertTrue(result["ok"])
        self.assertIsNone(auth_qr.redeem(self.canonical, payload.split("#")[1], now=201, secret=self.secret))
        from hestia.budget.analytics_views import BudgetAnalyticsViews
        with self.sessions() as session:
            code = session.scalar(select(db.NativeLoginCode))
            audit = session.scalar(select(db.BudgetAnalyticsAudit).where(
                db.BudgetAnalyticsAudit.subject_id == code.id,
                db.BudgetAnalyticsAudit.action == device_token.NEW_DEVICE_LOGIN,
            ))
            self.assertEqual(json.loads(audit.after_json), {
                "actor": "owner", "device_name": "Vesta QR", "token_id": code.id,
            })
            entries = BudgetAnalyticsViews._inbox_device_entries(session, "owner")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["ref"], {"token_id": code.id})
            self.assertEqual(entries[0]["params"]["device_name"], "Vesta QR")

    def test_redeem_rejects_expiry_disablement_generation_and_legacy_phase(self):
        payload = auth_qr.issue("owner", self.canonical, 0, now=10, secret=self.secret)
        code = payload.split("#")[1]
        self.assertIsNone(auth_qr.redeem(self.canonical, code, now=310, secret=self.secret))
        payload = auth_qr.issue("owner", self.canonical, 0, now=20, secret=self.secret)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = True
        self.assertIsNone(auth_qr.redeem(self.canonical, payload.split("#")[1], now=30, secret=self.secret))
        with self.sessions.begin() as session:
            user = session.get(db.User, "owner")
            user.disabled = False
            user.credential_generation = 1
        self.assertIsNone(auth_qr.redeem(self.canonical, payload.split("#")[1], now=30, secret=self.secret))
        with self.sessions.begin() as session:
            session.get(db.AppMeta, store_sql._SESSION_PHASE_KEY).value = store_sql.SESSION_PHASE_LEGACY
        self.assertIsNone(auth_qr.redeem(self.canonical, payload.split("#")[1], now=30, secret=self.secret))
        with self.assertRaises(auth_qr.QrLoginError):
            auth_qr.issue("owner", self.canonical, 1, now=30, secret=self.secret)

    def test_issue_state_validation_size_and_collision(self):
        for value, code in (("x" * 46, "qr_origin_too_long"),
                            ("https://é.example", "qr_origin_mismatch"),
                            (None, "qr_origin_mismatch")):
            with self.subTest(value=value), self.assertRaises(auth_qr.QrLoginError) as caught:
                auth_qr.issue("owner", value, 0, now=1, secret=self.secret)
            self.assertEqual(caught.exception.code, code)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, store_sql._USERS_AUTHORITATIVE).value = "0"
            session.delete(session.get(db.AppMeta, store_sql._USERS_AUTHORITATIVE))
        with self.assertRaises(auth_qr.QrLoginError):
            auth_qr.issue("owner", self.canonical, 0, now=1, secret=self.secret)
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key=store_sql._USERS_AUTHORITATIVE, value="1"))
            session.get(db.User, "owner").disabled = True
        with self.assertRaises(auth_qr.QrLoginError):
            auth_qr.issue("owner", self.canonical, 0, now=1, secret=self.secret)
        with self.sessions.begin() as session:
            session.get(db.User, "owner").disabled = False
        with self.assertRaises(auth_qr.QrLoginError):
            auth_qr.issue("owner", self.canonical, 1, now=1, secret=self.secret)
        with self.assertRaises(auth_qr.QrLoginError):
            auth_qr.issue("ghost", self.canonical, 0, now=1, secret=self.secret)
        with mock.patch.object(auth_qr, "_new_code", return_value="A" * 19), \
             mock.patch.object(auth_qr, "_digest", return_value="a" * 64):
            auth_qr.issue("owner", self.canonical, 0, now=1, secret=self.secret)
            with self.assertRaises(auth_qr.QrLoginError) as caught:
                auth_qr.issue("owner", self.canonical, 0, now=1, secret=self.secret)
        self.assertEqual(caught.exception.status, 503)
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key=authz.POLICY_PHASE_KEY, value="future"))
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            auth_qr.issue("owner", self.canonical, 0, now=1, secret=self.secret)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            auth_qr.redeem(self.canonical, "A" * 19, now=1, secret=self.secret)

    def test_code_validation_and_bounded_throttle_sources(self):
        valid = base64.urlsafe_b64encode(bytes(range(14))).rstrip(b"=").decode("ascii")
        self.assertEqual(auth_qr.normalize_code(valid), valid)
        for value in (None, 1, "", "A" * 18, "A" * 20, "!" + "A" * 18, "é" + "A" * 18, valid[:-1] + "B"):
            with self.subTest(value=value):
                if value == valid:
                    continue
                self.assertIsNone(auth_qr.normalize_code(value))
        device_token.reset_limits()
        self.addCleanup(device_token.reset_limits)
        self.assertIsNone(auth_qr.redeem_limited("2001:db8::1", 1.0))
        self.assertIsNone(auth_qr.redeem_limited("not-an-ip", 1.0))
        self.assertIsNone(auth_qr.redeem_global_limited(1.0))
        with mock.patch.object(device_token, "_charge", return_value=7) as charge:
            self.assertEqual(auth_qr.redeem_limited("127.0.0.1", 2.0), 7)
            self.assertEqual(auth_qr.redeem_global_limited(2.0), 7)
            self.assertEqual(charge.call_count, 2)


class QrLoginWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.temp.name) / "qr-web.db"
        self.secret = b"qr-web-secret"
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path),
            "HESTIA_PUBLIC_ORIGIN": "https://Hestia.Example:443/",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for patch in (mock.patch.object(web, "_AUTH_ENABLED", True),
                      mock.patch.object(web, "_SESSION_SECRET", self.secret)):
            patch.start()
            self.addCleanup(patch.stop)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"owner": auth.hash_password("correct horse battery")})
        store_sql.activate_sessions_v2(path=self.path)
        from types import SimpleNamespace
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace(audit_engine=None))))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        device_token.reset_limits()
        self.addCleanup(device_token.reset_limits)
        login = await self.client.post("/api/login", json={"user": "owner", "password": "correct horse battery"})
        self.cookie = login.cookies["hestia_session"].value
        self.client.session.cookie_jar.clear()

    def _browser_headers(self, origin="https://hestia.example"):
        return {"Cookie": f"hestia_session={self.cookie}", "Origin": origin}

    def _post_redeem(self, *, headers=None, **kwargs):
        return self.client.post(auth_qr.REDEEM_PATH, headers={"Host": "hestia.example", **(headers or {})}, **kwargs)

    def test_transport_parsers_fail_closed_on_bad_peer_and_duplicate_host(self):
        bad_peer = mock.Mock(
            headers=make_mocked_request("POST", auth_qr.REDEEM_PATH, headers={"X-Forwarded-Proto": "https"}).headers,
            remote="not-an-ip",
        )
        self.assertFalse(web._trusted_forwarded_https(bad_peer))
        duplicate = make_mocked_request("POST", auth_qr.REDEEM_PATH,
            headers=[("Host", "hestia.example"), ("Host", "other.example")])
        self.assertFalse(web._qr_public_transport(duplicate, "https://hestia.example"))
        self.assertFalse(web._qr_public_transport(duplicate, "not-an-origin"))

    async def test_issue_redeem_origin_matrix_and_feature_probe(self):
        issue = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://HESTIA.example:443"},
                                       headers=self._browser_headers("https://HESTIA.example"))
        self.assertEqual(issue.status, 200, await issue.text())
        self.assertNotIn("Set-Cookie", issue.headers)
        payload = (await issue.json())["qr_payload"]
        origin, code = payload.removeprefix("hestia-login:").split("#")
        self.assertEqual(origin, "https://hestia.example")
        self.assertEqual(len(payload.encode("ascii")), len("hestia-login:") + len(origin) + 20)
        features = await self.client.get("/api/auth/features")
        self.assertEqual((await features.json())["qr_origin"], "https://hestia.example")
        self.assertIn("qr_login", (await features.json())["features"])
        redeem = await self._post_redeem( json={"origin": origin, "code": code})
        self.assertEqual(redeem.status, 200, await redeem.text())
        self.assertEqual(set(await redeem.json()), {"ok", "user", "token"})
        self.assertNotIn("Set-Cookie", redeem.headers)
        replay = await self._post_redeem( json={"origin": origin, "code": code})
        self.assertEqual(replay.status, 401)
        self.assertEqual(await replay.json(), {"error": {"code": "qr_code_invalid"}})
        for headers in ({"Cookie": "x=y"}, {"Authorization": "Bearer " + "A" * 43}):
            ambiguous = await self._post_redeem( json={"origin": origin, "code": code}, headers=headers)
            self.assertEqual(ambiguous.status, 400)
            self.assertEqual(await ambiguous.json(), {"error": {"code": "qr_auth_ambiguous"}})

    async def test_redeem_requires_public_host_exact_origin_and_trusted_forwarded_https(self):
        issue = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://hestia.example"},
                                       headers=self._browser_headers())
        payload = (await issue.json())["qr_payload"]
        origin, code = payload.removeprefix("hestia-login:").split("#")
        wrong_host = await self.client.post(auth_qr.REDEEM_PATH, json={"origin": origin, "code": code})
        self.assertEqual(wrong_host.status, 400)
        noncanonical = await self._post_redeem(json={"origin": "https://HESTIA.example:443", "code": code})
        self.assertEqual(noncanonical.status, 400)
        untrusted = await self._post_redeem(json={"origin": origin, "code": code},
                                            headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(untrusted.status, 400)
        accepted = await self._post_redeem(json={"origin": origin, "code": code})
        self.assertEqual(accepted.status, 200, await accepted.text())
        token = (await accepted.json())["token"]
        whoami = await self.client.get("/api/whoami", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(whoami.status, 200)

        issue = await self.client.post(auth_qr.CODE_PATH, json={"origin": origin}, headers=self._browser_headers())
        code = (await issue.json())["qr_payload"].split("#")[1]
        with mock.patch.dict(os.environ, {consent_context.TRUSTED_PROXY_ENV: "127.0.0.1/32"}):
            accepted = await self._post_redeem(json={"origin": origin, "code": code},
                                               headers={"X-Forwarded-Proto": "https"})
            self.assertEqual(accepted.status, 200, await accepted.text())
            issue = await self.client.post(auth_qr.CODE_PATH, json={"origin": origin}, headers=self._browser_headers())
            code = (await issue.json())["qr_payload"].split("#")[1]
            device_token.reset_limits()
            wrong_scheme = await self._post_redeem(json={"origin": origin, "code": code},
                                                   headers={"X-Forwarded-Proto": "http"})
            self.assertEqual(wrong_scheme.status, 400)

    async def test_origin_errors_session_purpose_and_rate_limits(self):
        malformed_issue = await self.client.post(auth_qr.CODE_PATH, data="{", headers={
            **self._browser_headers(), "Content-Type": "application/json",
        })
        self.assertEqual(malformed_issue.status, 400)
        self.assertEqual(await malformed_issue.json(), {"error": {"code": "qr_invalid_request"}})
        malformed_redeem = await self._post_redeem( data="{", headers={
            "Content-Type": "application/json",
        })
        self.assertEqual(malformed_redeem.status, 400)
        self.assertEqual(await malformed_redeem.json(), {"error": {"code": "qr_invalid_request"}})
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            disabled_features = await (await self.client.get("/api/auth/features")).json()
            self.assertIsNone(disabled_features["qr_origin"])
            self.assertNotIn("qr_login", disabled_features["features"])
        for body, header in (("http://hestia.example", "https://hestia.example"),
                             ("https://other.example", "https://hestia.example"),
                             ("https://hestia.example", "https://other.example")):
            response = await self.client.post(auth_qr.CODE_PATH, json={"origin": body}, headers=self._browser_headers(header))
            self.assertEqual(response.status, 400)
            self.assertEqual(await response.json(), {"error": {"code": "qr_origin_mismatch"}})
        native = await self.client.post("/api/login", json={"user": "owner", "password": "correct horse battery", "bearer": True})
        token = (await native.json())["token"]
        denied = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://hestia.example"},
                                        headers={"Authorization": f"Bearer {token}", "Origin": "https://hestia.example"})
        self.assertEqual(denied.status, 403)
        bad = await self._post_redeem( json={"origin": "https://hestia.example", "code": "bad"})
        self.assertEqual(bad.status, 401)
        mismatch = await self._post_redeem( json={"origin": "https://other.example", "code": "A" * 19})
        self.assertEqual(mismatch.status, 400)
        with mock.patch.object(auth_qr, "redeem_limited", return_value=9):
            limited = await self._post_redeem( json={})
            self.assertEqual(limited.status, 429)
            self.assertEqual(limited.headers["Retry-After"], "9")
        with mock.patch.object(auth_qr, "redeem_global_limited", return_value=8):
            limited = await self._post_redeem(
                json={"origin": "https://hestia.example", "code": "A" * 19})
            self.assertEqual(limited.status, 429)
        with mock.patch.dict(os.environ, {"HESTIA_PUBLIC_ORIGIN": "http://lan.example"}):
            self.assertEqual((await self.client.post(auth_qr.CODE_PATH, json={}, headers=self._browser_headers())).status, 503)
            self.assertEqual((await self._post_redeem( json={})).status, 503)
            features = await (await self.client.get("/api/auth/features")).json()
            self.assertIsNone(features["qr_origin"])
            self.assertNotIn("qr_login", features["features"])

    async def test_duplicate_json_fields_use_qr_error_envelope(self):
        headers = {"Content-Type": "application/json"}
        issue = await self.client.post(
            auth_qr.CODE_PATH,
            data='{"origin":"https://different.example","origin":"https://hestia.example"}',
            headers={**self._browser_headers(), **headers},
        )
        redeem = await self._post_redeem(
            data='{"origin":"https://hestia.example","code":"bad","code":"bad"}',
            headers=headers,
        )
        for response in (issue, redeem):
            self.assertEqual(response.status, 400)
            self.assertEqual(await response.json(), {"error": {"code": "qr_invalid_request"}})
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_expiry_during_upload_and_auth_off_cannot_issue(self):
        now = web._now()
        with mock.patch.object(web, "_now", side_effect=[now, now + auth.SESSION_TTL + 1]):
            expired = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://hestia.example"},
                                             headers=self._browser_headers())
        self.assertEqual(expired.status, 401)
        with self.sessions() as session:
            self.assertEqual(session.query(db.NativeLoginCode).count(), 0)
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            anonymous = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://hestia.example"},
                                               headers={"Origin": "https://hestia.example"})
            self.assertEqual(anonymous.status, 401)
            self.assertEqual(await anonymous.json(), {"error": {"code": "browser_session_required"}})

    async def test_oversize_origin_and_bridge_errors(self):
        with mock.patch.dict(os.environ, {"HESTIA_PUBLIC_ORIGIN": "https://" + "a" * 40 + ".example"}):
            response = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://" + "a" * 40 + ".example"},
                headers=self._browser_headers("https://" + "a" * 40 + ".example"))
            self.assertEqual(response.status, 400)
            self.assertEqual(await response.json(), {"error": {"code": "qr_origin_too_long"}})
            features = await (await self.client.get("/api/auth/features")).json()
            self.assertIsNone(features["qr_origin"])
            self.assertNotIn("qr_login", features["features"])
        with mock.patch.object(auth_qr, "issue", side_effect=authz.PolicyPhaseUnsupported("future")):
            response = await self.client.post(auth_qr.CODE_PATH, json={"origin": "https://hestia.example"},
                                              headers=self._browser_headers())
            self.assertEqual(response.status, 503)
        with mock.patch.object(auth_qr, "redeem", side_effect=store_sql.SessionPhaseUnsupported("future")):
            response = await self._post_redeem(
                json={"origin": "https://hestia.example", "code": "A" * 19})
            self.assertEqual(response.status, 503)


if __name__ == "__main__":
    unittest.main()
