"""Real aiohttp consent boundaries with synthetic SQLite, cookies and provider."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from hestia import auth, db, store_sql, web
from hestia.budget import consent_api as module, consent_service
from hestia.budget import api, runtime
from hestia.budget.config import BudgetConfigError
from hestia.budget.consent_control import BudgetConsentControlStore
from hestia.budget.consent_files import ConsentFiles
from hestia.budget.consent_service import BudgetConsentService
from hestia.budget.consent_store import BudgetConsentError
from tests.test_budget_store import configuration, prepare_selected


SECRET = b"synthetic-consent-http-session-secret"
ORIGIN = "https://hestia.example.test"
FLOW = "11111111-2222-4333-8444-555555555555"
SESSION = "22222222-2222-4333-8444-555555555555"
UID = "33333333-2222-4333-8444-555555555555"
NONCE, STATE = "n" * 43, "s" * 43
PRIVATE = "PRIVATE-CODE-NEVER-LOG-OR-RETURN"
REAL_CREATE = module.create_service


class ConsentAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "synthetic.db"
        self.env = {"HESTIA_DB": str(self.path), "HESTIA_PERSIST": "sqlite", "HESTIA_BUDGET_HTTP_ENABLED": "1",
                    "HESTIA_BUDGET_JOBS_ENABLED": "0", "HESTIA_BUDGET_POLLER_ENABLED": "0", module.FLAG: "1",
                    "HESTIA_BUDGET_PUBLIC_ORIGIN": ORIGIN, "HESTIA_BUDGET_CONSENT_DIR": str(self.root / "capsules")}
        self.start_result = {"flow_id": FLOW, "authorization_url": "https://auth.enablebanking.com/ais/start",
                             "expires_at": time.time() + 900, "_nonce": NONCE}
        self.service = mock.Mock()
        self.service.start.side_effect = lambda *args, **kwargs: dict(self.start_result)
        self.service.flow_for_state.return_value = {"id": FLOW}
        self.service.callback.return_value = {"status": "staged"}
        self.service.pending.return_value = [{"id": FLOW, "status": "staged"}]
        self.service.status.return_value = []
        self.service.renewal_reminders.return_value = {
            "evaluated_at": 1_800_000_000.0,
            "lead_days": [14, 7, 3, 1],
            "rule_version": 1,
            "reminders": [],
        }
        self.service.reconcile.return_value = {
            "evaluated_at": 1_800_000_000.0,
            "retention_days": 90,
            "rule_version": 1,
            "summary": {
                "consents_total": 0,
                "flows_total": 0,
                "needs_owner_review": 0,
                "remote_close_eligible": 0,
                "local_retire_eligible": 0,
            },
            "consents": [],
            "flows": [],
        }
        self.service.complete.return_value = {"flow_id": FLOW, "status": "adopted"}
        self.service.cancel.return_value = {"flow_id": FLOW, "status": "canceled"}
        self.service.invalidate_bindings.return_value = 1
        self.factory = mock.Mock(side_effect=lambda **kwargs: self.service)
        self.tokens = {}
        for patcher in (mock.patch.dict(os.environ, self.env), mock.patch.object(web, "_AUTH_ENABLED", True),
                        mock.patch.object(web, "_SESSION_SECRET", SECRET), mock.patch.object(web, "_COOKIE_SECURE", True),
                        mock.patch.object(module, "create_service", self.factory), mock.patch.object(web, "_audit"),
                        mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No bank calls"))):
            value = patcher.start()
            self.addCleanup(patcher.stop)
            if getattr(patcher, "attribute", None) == "_audit":
                self.audit = value
        self.addCleanup(db.reset_engine_cache)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"alice": "unused", "bob": "unused"})
        with self.sessions.begin() as session:
            session.add_all([db.User(username="viewer", password_hash="unused", role="viewer"),
                             db.User(username="operator", password_hash="unused", role="operator"),
                             db.User(username="disabled", password_hash="unused", role="admin", disabled=True)])
        self.config = configuration(accounts=("synthetic-alice-hash",), owner="alice")
        other = replace(self.config.connections[0], id="bob-bank", owner="bob", accounts=("synthetic-bob-hash",))
        self.config = replace(self.config, connections=(*self.config.connections, other))
        self.store = BudgetConsentControlStore(self.sessions)
        prepare_selected(self.store, self.config)
        self.client = await self.new_client()

    async def new_client(self):
        client = TestClient(TestServer(web.make_app(SimpleNamespace(audit_engine=None))))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    def headers(self, user="alice", *, nonce=None, flow=FLOW, origin=True, bearer=False):
        if user not in self.tokens:
            self.tokens[user] = auth.make_session(user, now=web._now(), secret=SECRET)
        token = self.tokens[user]
        cookies = [] if bearer else ["hestia_session=" + token]
        if nonce is not None:
            cookies.append(module.cookie_name(flow) + "=" + nonce)
        headers = {"Cookie": "; ".join(cookies)} if cookies else {}
        if bearer:
            headers["Authorization"] = "Bearer " + token
        if origin:
            headers["Origin"] = ORIGIN
        return headers

    async def get(self, operation, *, headers=None, params=None):
        return await self.client.get(module.PREFIX + "/" + operation, params=params,
                                     headers=self.headers(nonce=NONCE, origin=False) if headers is None else headers)

    async def post(self, operation, body=None, *, headers=None, data=None):
        headers = dict(self.headers(nonce=NONCE) if headers is None else headers)
        headers.setdefault("Content-Type", "application/json")
        return await self.client.post(module.PREFIX + "/" + operation,
                                      data=json.dumps(body) if data is None else data, headers=headers)

    def private(self, response):
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def test_all_private_operations_use_cookie_owner_origin_and_per_flow_nonce(self):
        response = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "renew"})
        self.assertEqual(response.status, 200, await response.text())
        self.private(response)
        body = await response.json()
        self.assertNotIn("_nonce", body)
        cookie = response.cookies[module.cookie_name(FLOW)]
        self.assertEqual((cookie.value, cookie["max-age"], cookie["path"], cookie["samesite"]), (NONCE, "900", "/", "Lax"))
        self.assertTrue(cookie["secure"] and cookie["httponly"])
        for operation, key in (("pending", "pending"), ("status", "consents"),
                               ("renewal-reminders", "reminders"), ("reconcile", "summary")):
            response = await self.get(operation)
            self.assertEqual(response.status, 200)
            self.assertIn(key, await response.json())
            self.private(response)
        response = await self.post("complete", {"flow_id": FLOW})
        self.assertEqual((response.status, (await response.json())["status"]), (200, "adopted"))
        self.service.complete.assert_called_once()
        self.assertEqual(self.service.complete.call_args.kwargs["nonce"], NONCE)
        self.service.complete.return_value = {"flow_id": FLOW, "status": "in_progress"}
        response = await self.post("complete", {"flow_id": FLOW}, headers=self.headers())
        self.assertEqual(response.status, 202)
        await response.read()
        response = await self.post("cancel", {"flow_id": FLOW})
        self.assertEqual((response.status, (await response.json())["status"]), (200, "canceled"))
        self.assertEqual(self.service.cancel.call_args.args, ("alice", FLOW, mock.ANY, NONCE))
        other_flow = "aaaaaaaa-2222-4333-8444-555555555555"
        self.start_result.update(flow_id=other_flow, _nonce="m" * 43)
        response = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "connect"})
        self.assertEqual(response.status, 200); await response.read()
        self.assertIn(module.cookie_name(other_flow), response.cookies)
        self.assertNotIn(module.cookie_name(FLOW), response.cookies)
        self.audit.assert_not_called()

    async def test_public_callback_has_lax_nonce_but_needs_no_strict_main_cookie(self):
        response = await self.client.get(module.CALLBACK, params={"state": STATE, "code": PRIVATE},
                                         headers={"Cookie": module.cookie_name(FLOW) + "=" + NONCE}, allow_redirects=False)
        self.assertEqual(response.status, 303, await response.text())
        self.assertEqual(response.headers["Location"], module.COMPLETE)
        self.private(response)
        self.assertNotIn(PRIVATE, str(response.headers))
        self.assertNotIn(PRIVATE, await response.text())
        self.service.callback.assert_called_once_with(state=STATE, code=PRIVATE, nonce=NONCE)
        self.service.start.assert_not_called()
        self.service.complete.assert_not_called()
        response = await self.client.get(module.CALLBACK, params={"state": STATE, "error": "access_denied",
                                        "error_description": PRIVATE}, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertNotIn(PRIVATE, await response.text())
        self.service.callback.assert_called_with(state=STATE, error="access_denied", nonce="")

    async def test_callback_query_rejects_duplicates_unknown_controls_and_oversize_before_stage(self):
        queries = [[("state", STATE), ("state", STATE), ("code", PRIVATE)],
                   {"state": STATE, "code": PRIVATE, "unknown": "x"}, {"state": "bad", "code": PRIVATE},
                   {"state": STATE}, {"state": STATE, "code": PRIVATE, "error": "denied"},
                   {"state": STATE, "code": PRIVATE, "error_description": PRIVATE},
                   {"state": STATE, "code": ""}, {"state": STATE, "code": "x" * 4097},
                   {"state": STATE, "error": "x" * 1025}, {"state": STATE, "code": "bad\ncode"}]
        for query in queries:
            response = await self.client.get(module.CALLBACK, params=query, allow_redirects=False)
            self.assertEqual(response.status, 303, await response.text())
            self.assertEqual(response.headers["Location"], module.COMPLETE)
            self.assertEqual((await response.json())["error"], "budget_consent_invalid_request")
            self.private(response)
            self.assertNotIn(PRIVATE, await response.text())
        self.service.callback.assert_not_called()
        with self.assertRaises(BudgetConsentError):
            module._query(SimpleNamespace(query_string="x" * 8193))

    async def test_public_routes_are_exact_gets_and_bridge_never_contains_callback_code(self):
        for path in (module.COMPLETE, module.SCRIPT):
            response = await self.client.get(path, params={"code": PRIVATE})
            self.assertEqual(response.status, 200, await response.text())
            self.private(response)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            content = await response.text()
            self.assertNotIn(PRIVATE, content)
            self.assertNotIn("location.search", content)
        for method, path in (("HEAD", module.CALLBACK), ("POST", module.CALLBACK),
                             ("HEAD", module.COMPLETE), ("GET", module.COMPLETE + "/")):
            response = await self.client.request(method, path)
            self.assertEqual(response.status, 401)
            await response.read()

    async def test_roles_bearer_and_origin_are_rejected_before_service(self):
        for user, status in (("viewer", 403), ("operator", 403), ("disabled", 401), ("missing", 401)):
            response = await self.get("pending", headers=self.headers(user))
            self.assertEqual(response.status, status)
            await response.read()
        response = await self.get("pending", headers={})
        self.assertEqual(response.status, 401); await response.read()
        response = await self.get("pending", headers=self.headers(bearer=True))
        self.assertEqual(response.status, 403); await response.read()
        for headers in (self.headers(origin=False), {**self.headers(), "Origin": "https://outside.invalid"},
                        self.headers(bearer=True)):
            response = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "connect"}, headers=headers)
            self.assertEqual(response.status, 403); await response.read()
        response = await self.client.post(module.PREFIX + "/cancel", json={"flow_id": FLOW},
                    headers=[("Cookie", self.headers()["Cookie"]), ("Origin", ORIGIN), ("Origin", ORIGIN)])
        self.assertEqual(response.status, 403); await response.read()
        self.service.start.assert_not_called()

    async def test_strict_body_and_private_query_rejections_never_reach_service(self):
        for body in ({"connection_id": "bank", "intent": "connect"}, {"connection_id": "bank", "intent": "connect", "expected_version": True},
                     {"connection_id": "bank", "intent": "connect", "expected_version": 1, "code": PRIVATE}, []):
            response = await self.post("start", body)
            self.assertEqual(response.status, 400, await response.text())
        for data, status in (("{", 400), ('{"flow_id":"' + FLOW + '","flow_id":"' + FLOW + '"}', 400), ("x" * 4097, 413)):
            response = await self.post("complete", data=data)
            self.assertEqual(response.status, status, await response.text())
        response = await self.post("cancel", {"flow_id": FLOW}, headers={**self.headers(), "Content-Type": "text/plain"})
        self.assertEqual(response.status, 415); await response.read()
        response = await self.get("pending", params={"code": PRIVATE})
        self.assertEqual(response.status, 400); self.assertNotIn(PRIVATE, await response.text())
        self.service.start.assert_not_called()
        self.service.complete.assert_not_called()

    async def test_cookie_filtering_is_bounded_and_psu_ignores_forwarded_headers_and_referrer(self):
        cookies = self.headers()["Cookie"] + "; unrelated=x; " + module.COOKIE_PREFIX + "bad=x; " + module.cookie_name(FLOW) + "=invalid"
        response = await self.get("pending", headers={"Cookie": cookies})
        self.assertEqual(response.status, 200); await response.read()
        self.assertEqual(self.service.pending.call_args.args[-1], {})
        cookies += "".join("; " + module.cookie_name(str(uuid.uuid4())) + "=" + NONCE for _ in range(17))
        response = await self.get("pending", headers={"Cookie": cookies})
        self.assertEqual(response.status, 400); await response.read()
        headers = {**self.headers(), "X-Forwarded-For": "203.0.113.99", "Referer": ORIGIN + "/?code=" + PRIVATE,
                   "User-Agent": "Synthetic Browser", "Accept-Language": "pl"}
        response = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "connect"}, headers=headers)
        self.assertEqual(response.status, 200, await response.text())
        context = self.service.start.call_args.kwargs["psu"]
        self.assertNotEqual(context.ip_address, "203.0.113.99")
        self.assertEqual((context.user_agent, context.accept_language, context.referer),
                         ("Synthetic Browser", "pl", None))
        for value in (None, "bad", "AAAAAAAA-2222-4333-8444-555555555555"):
            with self.assertRaises(BudgetConsentError):
                module.cookie_name(value)

    async def test_safe_failures_and_dynamic_capability(self):
        response = await self.client.get("/api/budget/connections", headers=self.headers())
        self.assertTrue((await response.json())["capabilities"]["consent"])
        with mock.patch.dict(os.environ, {module.FLAG: "0"}):
            response = await self.get("status")
            self.assertEqual(response.status, 503); await response.read()
            response = await self.client.get("/api/budget/connections", headers=self.headers())
            self.assertFalse((await response.json())["capabilities"]["consent"])
        self.client.server.app[module.KEY].service = None
        response = await self.get("status")
        self.assertEqual(response.status, 503); await response.read()
        self.client.server.app[module.KEY].service = self.service
        self.service.pending.side_effect = BudgetConsentError("budget_consent_binding_invalid", 403)
        response = await self.get("pending")
        self.assertEqual(response.status, 403); await response.read()
        self.service.callback.side_effect = RuntimeError(PRIVATE)
        with self.assertLogs(api.log, level="ERROR") as logged:
            response = await self.client.get(module.CALLBACK, params={"state": STATE, "code": PRIVATE}, allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers["Location"], module.COMPLETE)
            self.assertEqual((await response.json())["error"], "budget_internal_error")
            self.private(response)
            self.assertNotIn(PRIVATE, await response.text())
        self.assertNotIn(PRIVATE, "".join(logged.output))

    async def test_login_logout_wait_for_binding_fence_and_never_claim_failed_invalidation_succeeded(self):
        entered, release = threading.Event(), threading.Event()
        def invalidate(*args):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Synthetic test release missing")
            return 1
        self.service.invalidate_bindings.side_effect = invalidate
        pending = asyncio.create_task(self.client.post("/api/logout", headers=self.headers(nonce=NONCE)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.assertFalse(pending.done())
        finally:
            release.set()
        response = await pending
        self.assertEqual(response.status, 200); await response.read()
        self.assertEqual(response.cookies[module.cookie_name(FLOW)]["max-age"], "0")
        self.assertEqual(response.cookies["hestia_session"]["max-age"], "0")
        self.service.invalidate_bindings.side_effect = RuntimeError(PRIVATE)
        for endpoint in ("logout", "login"):
            with mock.patch.object(auth, "authenticate", return_value=True), self.assertLogs(api.log, level="ERROR") as logged:
                response = await self.client.post("/api/" + endpoint, json={"user": "alice", "password": "synthetic"},
                                                  headers=self.headers(nonce=NONCE))
                self.assertEqual(response.status, 500)
                self.assertFalse((await response.json())["ok"])
                self.assertNotIn("hestia_session", response.cookies)
            self.assertNotIn(PRIVATE, "".join(logged.output))
        self.service.invalidate_bindings.side_effect = None
        with mock.patch.object(auth, "authenticate", return_value=True):
            response = await self.client.post("/api/login", json={"user": "alice", "password": "synthetic"},
                                              headers=self.headers(nonce=NONCE))
            self.assertEqual(response.status, 200); await response.read()
            self.assertEqual(response.cookies["hestia_session"]["samesite"], "Strict")
            self.assertTrue(response.cookies["hestia_session"]["secure"])

    def bound_flow(self):
        self.headers()
        token = self.tokens["alice"]
        fingerprint = module._fingerprint(token, SECRET)
        hashed = lambda value: hashlib.sha256(value.encode()).hexdigest()
        return self.store.create_flow("alice", "bank", intent="connect", expected_version=1,
                    state_hash=hashed(STATE), nonce_hash=hashed(NONCE), auth_fingerprint=fingerprint)

    async def test_disabled_or_failed_service_still_fences_persisted_flows_without_config_or_files(self):
        flow = self.bound_flow()
        self.client.server.app[module.KEY].service = None
        with mock.patch.object(module, "load_config", side_effect=AssertionError("No config")), \
             mock.patch.object(module, "ConsentFiles", side_effect=AssertionError("No files")):
            response = await self.client.post("/api/logout", headers=self.headers(nonce=NONCE, flow=flow["id"]))
            self.assertEqual(response.status, 200, await response.text())
        with self.sessions() as session:
            self.assertGreater(session.get(db.BudgetConsentFlow, flow["id"]).binding_revision, 1)
        for mode in ("json", "sqlite"):
            with mock.patch.dict(os.environ, {"HESTIA_PERSIST": mode}), \
                 mock.patch.object(store_sql, "is_users_db_authoritative", return_value=False):
                response = await self.client.post("/api/logout", headers=self.headers(nonce=NONCE))
                self.assertEqual(response.status, 503); await response.read()
                self.assertNotIn("hestia_session", response.cookies)

    async def test_default_off_creates_no_service_or_private_files_and_startup_errors_are_sanitized(self):
        with mock.patch.dict(os.environ, {module.FLAG: "0"}), \
             mock.patch.object(module, "load_config", side_effect=AssertionError("No config")), \
             mock.patch.object(module, "init_db", side_effect=AssertionError("No DB initialization")), \
             mock.patch.object(module, "ConsentFiles", side_effect=AssertionError("No private files")):
            before = self.factory.call_count
            client = await self.new_client()
            self.assertEqual(self.factory.call_count, before)
            response = await client.get(module.CALLBACK, params={"state": STATE, "code": PRIVATE}, allow_redirects=False)
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers["Location"], module.COMPLETE)
            self.assertEqual((await response.json())["error"], "budget_consent_unavailable")
            self.private(response)
            response = await client.post("/api/logout")
            self.assertEqual(response.status, 200); await response.read()
        self.factory.side_effect = RuntimeError(PRIVATE)
        with self.assertLogs(module.log, level="ERROR") as logged:
            client = await self.new_client()
        self.assertNotIn(PRIVATE, "".join(logged.output))
        self.assertIsNone(client.server.app[module.KEY].service)

    async def test_startup_prerequisites_origin_full_config_and_recovery(self):
        defaults = {"auth_enabled": lambda: True, "session_secret": lambda: SECRET, "secure_cookie": lambda: True}
        for changes in ({"auth_enabled": lambda: False}, {"secure_cookie": lambda: False}, {"session_secret": lambda: b""}):
            with self.assertRaises(BudgetConfigError):
                REAL_CREATE(**(defaults | changes))
        for env in ({"HESTIA_PERSIST": "json"}, {"HESTIA_BUDGET_HTTP_ENABLED": "0"}):
            with mock.patch.dict(os.environ, env), self.assertRaises(BudgetConfigError):
                REAL_CREATE(**defaults)
        for value in ("", "http://example.test", ORIGIN + "/", ORIGIN + "?x=y", "https://user@example.test",
                      "https://example.test:bad", ORIGIN + "\\bad", ORIGIN + " bad"):
            with mock.patch.dict(os.environ, {"HESTIA_BUDGET_PUBLIC_ORIGIN": value}), self.assertRaises(BudgetConfigError):
                module._origin()
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_PUBLIC_ORIGIN": ORIGIN + ":8443"}):
            self.assertEqual(module._origin(), ORIGIN + ":8443")
        with mock.patch.object(module, "load_config", return_value=None), self.assertRaises(BudgetConfigError):
            REAL_CREATE(**defaults)
        with mock.patch.object(module, "load_config", return_value=self.config), \
             mock.patch.object(store_sql, "is_users_db_authoritative", return_value=False), self.assertRaises(BudgetConfigError):
            REAL_CREATE(**defaults)
        flow = self.bound_flow()
        with self.sessions.begin() as session:
            session.get(db.BudgetConsentFlow, flow["id"]).status = "exchanging"
        with mock.patch.object(module, "load_config", return_value=self.config), \
             mock.patch.object(module, "ConsentFiles") as files, mock.patch.object(module, "BudgetConsentService") as service:
            self.assertIs(REAL_CREATE(**defaults), service.return_value)
            self.assertIs(service.call_args.args[0], self.config)
            self.assertEqual(service.call_args.kwargs["redirect_url"], ORIGIN + module.CALLBACK)
            files.assert_called_once_with(str(self.root / "capsules"))
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetConsentFlow, flow["id"]).status, "exchange_uncertain")

    async def test_internal_auth_and_response_optional_paths(self):
        request = make_mocked_request("GET", module.PREFIX + "/pending", headers={"Cookie": "hestia_session=invalid"})
        request["hestia_user"], request["hestia_role"] = "alice", "admin"
        with self.assertRaises(api._Rejected) as caught:
            module._browser(request, lambda: True, lambda: SECRET, mutation=False)
        self.assertEqual(caught.exception.status, 401)
        request = make_mocked_request("POST", "/api/logout", headers=self.headers(nonce=NONCE), app=self.client.server.app)
        await module.auth_transition(request)
        self.service.invalidate_bindings.assert_called_once()

    def real_service(self, *, exchange_hook=None):
        now, mono, calls = [time.time()], [100.0], []
        connection = self.config.connections[0]
        def sleep(delay):
            now[0] += delay
            mono[0] += delay
        class Provider:
            state = None
            def get(self, path, query=None, **kwargs):
                calls.append(("GET", path))
                if path == "/aspsps":
                    return {"aspsps": [{"name": connection.bank_name, "country": connection.country,
                            "psu_types": ["personal"], "maximum_consent_validity": 86400}]}
                return {"status": "AUTHORIZED", "aspsp": {"name": connection.bank_name, "country": connection.country},
                        "psu_type": "personal", "access": {"valid_until": datetime.fromtimestamp(now[0] + 86400, timezone.utc).isoformat()},
                        "accounts": [UID], "accounts_data": [{"uid": UID, "identification_hash": connection.accounts[0]}]}
            def post(self, path, body, **kwargs):
                calls.append(("POST", path))
                if path == "/auth":
                    self.state = body["state"]
                    return {"url": "https://auth.enablebanking.com/ais/start?sessionid=" + FLOW}
                if exchange_hook is not None:
                    exchange_hook()
                return {"session_id": SESSION}
        provider = Provider()
        store = BudgetConsentControlStore(self.sessions, clock=lambda: now[0])
        real = BudgetConsentService(self.config, store, ConsentFiles(self.root / "real-synthetic-capsules"), path=self.path,
                    redirect_url=ORIGIN + module.CALLBACK, clock=lambda: now[0], monotonic=lambda: mono[0],
                    sleeper=sleep, client_factory=lambda *args, **kwargs: provider)
        self.client.server.app[module.KEY].service = real
        return provider, calls

    async def test_real_store_service_http_flow_stages_without_provider_calls_and_adopts_once(self):
        provider, calls = self.real_service()
        connection = self.config.connections[0]
        with mock.patch.object(consent_service, "_read_secret", return_value=b"synthetic-key-no-real-secret"):
            started = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "connect"})
            self.assertEqual(started.status, 200, await started.text())
            flow = (await started.json())["flow_id"]
            nonce = started.cookies[module.cookie_name(flow)].value
            response = await self.client.get(module.CALLBACK, params={"state": provider.state, "code": PRIVATE},
                        headers={"Cookie": module.cookie_name(flow) + "=" + nonce}, allow_redirects=False)
            self.assertEqual(response.status, 303, await response.text())
            self.assertEqual(calls, [("GET", "/aspsps"), ("POST", "/auth")])
            foreign = await self.post("complete", {"flow_id": flow}, headers=self.headers("bob", nonce=nonce, flow=flow))
            self.assertEqual(foreign.status, 404); await foreign.read()
            response = await self.post("complete", {"flow_id": flow}, headers=self.headers(nonce=nonce, flow=flow))
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await response.json())["status"], "adopted")
            for forbidden in (PRIVATE, SESSION, connection.accounts[0], self.config.application_id):
                self.assertNotIn(forbidden, await response.text())
            repeated = await self.post("complete", {"flow_id": flow}, headers=self.headers(nonce=nonce, flow=flow))
            self.assertEqual(repeated.status, 200, await repeated.text())
            self.assertEqual(calls.count(("POST", "/sessions")), 1)
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetConnection, "bank").consent_generation, 1)

    async def test_callback_failures_always_redirect_cleanly_without_consuming_the_bound_flow(self):
        provider, calls = self.real_service()
        with mock.patch.object(consent_service, "_read_secret", return_value=b"synthetic-key-no-real-secret"):
            started = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "connect"})
        self.assertEqual(started.status, 200)
        flow = (await started.json())["flow_id"]
        nonce = started.cookies[module.cookie_name(flow)].value
        bound = {"Cookie": module.cookie_name(flow) + "=" + nonce}
        valid = {"state": provider.state, "code": PRIVATE}
        cases = (({**valid, "unknown": PRIVATE}, bound, "budget_consent_invalid_request"),
                 ([("state", provider.state), ("state", provider.state), ("code", PRIVATE)], bound,
                  "budget_consent_invalid_request"),
                 (valid, {"Cookie": "unrelated=foreign-browser"}, "budget_consent_invalid_request"),
                 (valid, {"Cookie": module.cookie_name(flow) + "=" + "x" * 43}, "budget_consent_binding_invalid"),
                 ({**valid, "state": "u" * 43}, bound, "budget_not_found"))
        for query, headers, error in cases:
            with self.subTest(error=error, headers=headers):
                response = await self.client.get(module.CALLBACK, params=query, headers=headers, allow_redirects=False)
                self.assertEqual(response.status, 303, await response.text())
                self.assertEqual(response.headers["Location"], module.COMPLETE)
                self.assertEqual(await response.json(), {"ok": False, "error": error})
                self.private(response)
                self.assertNotIn(PRIVATE, str(response.headers))
                self.assertNotIn(PRIVATE, await response.text())
                self.assertNotIn(provider.state, str(response.headers))
                with self.sessions() as session:
                    row = session.get(db.BudgetConsentFlow, flow)
                    self.assertEqual((row.status, row.callback_at), ("started", None))
        # A valid bound refusal consumes only its own flow; it also leaves a clean URL.
        response = await self.client.get(module.CALLBACK, params={"state": provider.state, "error": "access_denied",
                    "error_description": PRIVATE}, headers=bound, allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(response.headers["Location"], module.COMPLETE)
        self.private(response)
        self.assertNotIn(PRIVATE, await response.text())
        with self.sessions() as session:
            row = session.get(db.BudgetConsentFlow, flow)
            self.assertEqual((row.status, row.error), ("failed", "authorization"))
            self.assertIsNone(session.get(db.BudgetConnection, "bank").active_consent_id)
        self.assertEqual(calls, [("GET", "/aspsps"), ("POST", "/auth")])
        self.assertFalse((self.root / "real-synthetic-capsules").exists())

    async def test_real_logout_fences_inflight_exchange_and_retains_known_session_without_adoption(self):
        entered, release = threading.Event(), threading.Event()
        def exchange():
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("Synthetic exchange release missing")
        provider, calls = self.real_service(exchange_hook=exchange)
        with mock.patch.object(consent_service, "_read_secret", return_value=b"synthetic-key-no-real-secret"):
            started = await self.post("start", {"connection_id": "bank", "expected_version": 1, "intent": "connect"})
            self.assertEqual(started.status, 200, await started.text())
            flow = (await started.json())["flow_id"]
            nonce = started.cookies[module.cookie_name(flow)].value
            response = await self.client.get(module.CALLBACK, params={"state": provider.state, "code": PRIVATE},
                        headers={"Cookie": module.cookie_name(flow) + "=" + nonce}, allow_redirects=False)
            self.assertEqual(response.status, 303); await response.read()
            pending = asyncio.create_task(self.post("complete", {"flow_id": flow}, headers=self.headers(nonce=nonce, flow=flow)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                logout = await self.client.post("/api/logout", headers=self.headers(nonce=nonce, flow=flow))
                self.assertEqual(logout.status, 200, await logout.text())
            finally:
                release.set()
            response = await pending
            self.assertEqual(response.status, 202, await response.text())
            self.assertNotEqual((await response.json())["status"], "adopted")
        self.assertEqual(calls.count(("POST", "/sessions")), 1)
        self.assertNotIn(("GET", "/sessions/" + SESSION), calls)
        with self.sessions() as session:
            connection = session.get(db.BudgetConnection, "bank")
            self.assertEqual((connection.credential_mode, connection.consent_generation, connection.active_consent_id),
                             ("external", 0, None))
            row = session.get(db.BudgetConsentFlow, flow)
            self.assertEqual(row.status, "canceled")
            self.assertEqual(session.get(db.BudgetConsent, row.candidate_consent_id).status, "UNADOPTED")

    async def test_real_renewal_reminders_read_only_owner_scoped_no_bank_calls_and_errors(self):
        from hestia import api_contract
        from hestia.budget import renewal_reminders as rr

        fixed_now = 1_800_000_000.0
        _, calls = self.real_service()
        real = self.client.server.app[module.KEY].service
        real.clock = lambda: fixed_now
        real.store.clock = lambda: fixed_now

        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").valid_until = fixed_now + 40 * rr.SECONDS_PER_DAY
            session.add_all([
                db.BudgetConnection(id="alice-attention", owner="alice", application_id=self.config.application_id,
                                    bank_name="Attention Bank", country="PL", enabled=True, paused=False,
                                    attention="expired_session", valid_until=fixed_now + 45 * rr.SECONDS_PER_DAY,
                                    next_attempt=0, failures=0),
                db.BudgetConnection(id="alice-expired", owner="alice", application_id=self.config.application_id,
                                    bank_name="Expired Bank", country="PL", enabled=True, paused=False,
                                    valid_until=fixed_now - 3600, next_attempt=0, failures=0),
                db.BudgetConnection(id="alice-due", owner="alice", application_id=self.config.application_id,
                                    bank_name="Due Bank", country="PL", enabled=True, paused=True,
                                    valid_until=fixed_now + 3 * rr.SECONDS_PER_DAY, next_attempt=0, failures=0),
                db.BudgetConnection(id="alice-unknown", owner="alice", application_id=self.config.application_id,
                                    bank_name="Unknown Bank", country="PL", enabled=True, paused=False,
                                    valid_until=None, next_attempt=0, failures=0),
                db.BudgetConnection(id="alice-disabled", owner="alice", application_id=self.config.application_id,
                                    bank_name="Disabled Bank", country="PL", enabled=False, paused=False,
                                    valid_until=fixed_now - 10, next_attempt=0, failures=0),
            ])

        response = await self.get("renewal-reminders")
        self.assertEqual(response.status, 200, await response.text())
        self.private(response)
        payload = await response.json()
        parsed = api_contract.BudgetConsentRenewalReminders.model_validate(payload)
        self.assertEqual(parsed.model_dump(mode="json"), payload)
        self.assertEqual(payload["evaluated_at"], fixed_now)
        self.assertEqual(payload["lead_days"], list(rr.DEFAULT_LEAD_DAYS))
        self.assertEqual(payload["rule_version"], rr.RULE_VERSION)
        self.assertEqual([row["connection_id"] for row in payload["reminders"]], [
            "alice-attention", "alice-expired", "alice-due", "bank", "alice-unknown",
        ])
        by_id = {row["connection_id"]: row for row in payload["reminders"]}
        self.assertEqual((by_id["alice-attention"]["status"], by_id["alice-attention"]["active_milestone"],
                          by_id["alice-attention"]["reminder_due"], by_id["alice-attention"]["attention"]),
                         ("attention_required", "attention", True, "expired_session"))
        self.assertEqual((by_id["alice-expired"]["status"], by_id["alice-expired"]["active_milestone"],
                          by_id["alice-expired"]["days_remaining"]), ("expired", 0, -1))
        self.assertEqual((by_id["alice-due"]["status"], by_id["alice-due"]["active_milestone"],
                          by_id["alice-due"]["paused"]), ("due_soon", 3, True))
        self.assertEqual((by_id["bank"]["status"], by_id["bank"]["reminder_due"]), ("healthy", False))
        self.assertEqual((by_id["alice-unknown"]["status"], by_id["alice-unknown"]["valid_until"]),
                         ("unknown_expiry", None))
        for row in payload["reminders"]:
            for forbidden in ("destination_target", "session_id", "authorization_url", "capsule_ref", "aspsp_id", "psu_type", "code"):
                self.assertNotIn(forbidden, row)

        # Strict owner isolation: Bob only sees bob-bank.
        bob_resp = await self.get("renewal-reminders", headers=self.headers("bob", origin=False))
        self.assertEqual(bob_resp.status, 200)
        bob_body = await bob_resp.json()
        self.assertEqual([row["connection_id"] for row in bob_body["reminders"]], ["bob-bank"])

        # Direct store call without explicit `now` uses store.clock.
        direct = real.store.renewal_reminders("alice")
        self.assertEqual(direct["evaluated_at"], fixed_now)

        # Auth, method, query and feature-flag guards fail closed without contacting any bank.
        for user, status in (("viewer", 403), ("operator", 403), ("disabled", 401)):
            denied = await self.get("renewal-reminders", headers=self.headers(user, origin=False))
            self.assertEqual(denied.status, status)
            await denied.read()
        bearer = await self.get("renewal-reminders", headers=self.headers(bearer=True, origin=False))
        self.assertEqual(bearer.status, 403); await bearer.read()
        query = await self.get("renewal-reminders", params={"days": "7"})
        self.assertEqual(query.status, 400); await query.read()
        head = await self.client.head(module.PREFIX + "/renewal-reminders", headers=self.headers(origin=False))
        self.assertEqual(head.status, 405); await head.read()
        with mock.patch.dict(os.environ, {module.FLAG: "0"}):
            off = await self.get("renewal-reminders")
            self.assertEqual(off.status, 503); await off.read()

        # Stored data integrity error -> 500; exceeding MAX_CONNECTIONS -> 413.
        real.clock = lambda: -1.0
        bad = await self.get("renewal-reminders")
        self.assertEqual(bad.status, 500)
        self.assertEqual((await bad.json())["error"], "budget_consent_data_error")

        real.clock = lambda: fixed_now
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetConnection(id=f"alice-cap-{idx:03d}", owner="alice", application_id=self.config.application_id,
                                    bank_name="Cap Bank", country="PL", enabled=True, paused=False,
                                    next_attempt=0, failures=0)
                for idx in range(rr.MAX_CONNECTIONS)
            ])
        over = await self.get("renewal-reminders")
        self.assertEqual(over.status, 413)
        self.assertEqual((await over.json())["error"], "budget_consent_limit")
        self.assertEqual(calls, [])
        self.audit.assert_not_called()

    async def test_real_consent_reconcile_read_only_opaque_ids_owner_scoped_and_errors(self):
        from hestia import api_contract
        from hestia.budget import consent_reconcile as cr

        fixed_now = 1_800_000_000.0
        _, calls = self.real_service()
        real = self.client.server.app[module.KEY].service
        real.clock = lambda: fixed_now
        real.store.clock = lambda: fixed_now
        day = cr.SECONDS_PER_DAY
        raw_ids = ("c-active", "c-retired-valid", "c-retired-prunable", "c-poc", "c-uncertain",
                   "c-mismatch", "f-uncertain", "f-prunable", "bob-consent", "bob-flow")

        with self.sessions.begin() as session:
            session.add(db.BudgetConnection(
                id="alice-conn-2", owner="alice", application_id=self.config.application_id,
                bank_name="Second Bank", country="PL", enabled=True, paused=False,
                active_consent_id="c-mismatch", next_attempt=0, failures=0,
            ))
            session.get(db.BudgetConnection, "bank").active_consent_id = "c-active"
            session.add_all([
                db.BudgetConsent(id="c-active", owner="alice", connection_id="bank",
                                 application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                 provenance="hestia_created", status="ACTIVE", capsule_ref="cap-1",
                                 valid_until=fixed_now + 30 * day, generation=2, metadata_json="{}", created_at=fixed_now - 5 * day),
                db.BudgetConsent(id="c-retired-valid", owner="alice", connection_id="bank",
                                 application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                 provenance="hestia_created", status="RETIRED", capsule_ref="cap-2",
                                 valid_until=fixed_now + 10 * day, generation=1, metadata_json="{}", created_at=fixed_now - 20 * day),
                db.BudgetConsent(id="c-retired-prunable", owner="alice", connection_id="bank",
                                 application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                 provenance="hestia_created", status="RETIRED", capsule_ref="cap-3",
                                 valid_until=fixed_now - 100 * day, generation=1, metadata_json="{}", created_at=fixed_now - 120 * day),
                db.BudgetConsent(id="c-poc", owner="alice", connection_id="bank",
                                 application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                 provenance="poc_retired", status="ACTIVE", capsule_ref="cap-poc",
                                 valid_until=fixed_now - 100 * day, generation=1, metadata_json="{}", created_at=fixed_now - 130 * day),
                db.BudgetConsent(id="c-uncertain", owner="alice", connection_id="bank",
                                 application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                 provenance="hestia_created", status="UNADOPTED", capsule_ref="cap-4",
                                 valid_until=fixed_now + 10 * day, generation=3, metadata_json="{}", created_at=fixed_now - day),
                db.BudgetConsent(id="c-mismatch", owner="alice", connection_id="alice-conn-2",
                                 application_id=self.config.application_id, bank_name="Second Bank", country="PL",
                                 provenance="hestia_created", status="RETIRED", capsule_ref="cap-5",
                                 valid_until=fixed_now + 5 * day, generation=1, metadata_json="{}", created_at=fixed_now - 2 * day),
                db.BudgetConsent(id="bob-consent", owner="bob", connection_id="bob-bank",
                                 application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                 provenance="hestia_created", status="ACTIVE", capsule_ref="cap-bob",
                                 valid_until=fixed_now + 10 * day, generation=1, metadata_json="{}", created_at=fixed_now - day),
            ])
            session.add_all([
                db.BudgetConsentFlow(id="f-uncertain", owner="alice", connection_id="bank",
                                     application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                     intent="renew", expected_connection_version=1, expected_generation=2,
                                     state_hash="1" * 64, nonce_hash="2" * 64, auth_fingerprint="3" * 64,
                                     binding_revision=1, flow_revision=1, created_at=fixed_now - day,
                                     expires_at=fixed_now - day + 900, status="exchange_uncertain",
                                     error="exchange_uncertain", candidate_consent_id="c-uncertain"),
                db.BudgetConsentFlow(id="f-prunable", owner="alice", connection_id="bank",
                                     application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                     intent="connect", expected_connection_version=1, expected_generation=1,
                                     state_hash="4" * 64, nonce_hash="5" * 64, auth_fingerprint="6" * 64,
                                     binding_revision=2, flow_revision=2, created_at=fixed_now - 101 * day,
                                     expires_at=fixed_now - 100 * day, status="canceled",
                                     error="canceled", candidate_consent_id=None),
                db.BudgetConsentFlow(id="bob-flow", owner="bob", connection_id="bob-bank",
                                     application_id=self.config.application_id, bank_name="Synthetic Bank", country="PL",
                                     intent="connect", expected_connection_version=1, expected_generation=0,
                                     state_hash="7" * 64, nonce_hash="8" * 64, auth_fingerprint="9" * 64,
                                     binding_revision=1, flow_revision=1, created_at=fixed_now - day,
                                     expires_at=fixed_now + 900, status="started",
                                     error=None, candidate_consent_id=None),
            ])

        response = await self.get("reconcile")
        self.assertEqual(response.status, 200, await response.text())
        self.private(response)
        payload = await response.json()
        parsed = api_contract.BudgetConsentReconcile.model_validate(payload)
        self.assertEqual(parsed.model_dump(mode="json"), payload)
        self.assertEqual((payload["evaluated_at"], payload["retention_days"], payload["rule_version"]),
                         (fixed_now, cr.DEFAULT_RETENTION_DAYS, cr.RULE_VERSION))
        self.assertEqual(payload["summary"], {
            "consents_total": 6,
            "flows_total": 2,
            "needs_owner_review": 4,
            "remote_close_eligible": 1,
            "local_retire_eligible": 2,
        })

        # Raw DB IDs and sensitive fields never appear anywhere in the serialized response.
        wire = json.dumps(payload)
        for raw in raw_ids:
            self.assertNotIn(raw, wire)
        for forbidden in ("session_id", "capsule_ref", "authorization_url", "aspsp_id", "psu_type",
                          "state_hash", "nonce_hash", "auth_fingerprint", "cap-"):
            self.assertNotIn(forbidden, wire)

        opaque = real.store._opaque_reconcile_id
        by_consent = {row["consent_id"]: row for row in payload["consents"]}
        by_flow = {row["flow_id"]: row for row in payload["flows"]}
        for cid in by_consent:
            self.assertRegex(cid, r"^[0-9a-f]{64}$")
        for fid in by_flow:
            self.assertRegex(fid, r"^[0-9a-f]{64}$")

        self.assertEqual(by_consent[opaque("alice", "consent", "c-active")]["classification"], "active_current")
        self.assertEqual(by_consent[opaque("alice", "consent", "c-retired-valid")]["classification"], "retired_still_valid")
        self.assertTrue(by_consent[opaque("alice", "consent", "c-retired-valid")]["remote_close_eligible"])
        self.assertEqual(by_consent[opaque("alice", "consent", "c-retired-prunable")]["classification"], "retired_prunable")
        self.assertTrue(by_consent[opaque("alice", "consent", "c-retired-prunable")]["local_retire_eligible"])
        poc = by_consent[opaque("alice", "consent", "c-poc")]
        self.assertEqual((poc["classification"], poc["needs_owner_review"], poc["remote_close_eligible"], poc["local_retire_eligible"]),
                         ("external_read_only", False, False, False))
        unc = by_consent[opaque("alice", "consent", "c-uncertain")]
        self.assertEqual((unc["classification"], unc["needs_owner_review"], unc["remote_close_eligible"], unc["local_retire_eligible"]),
                         ("uncertain_flow_evidence", True, False, False))
        mis = by_consent[opaque("alice", "consent", "c-mismatch")]
        self.assertEqual((mis["classification"], mis["needs_owner_review"], mis["remote_close_eligible"], mis["local_retire_eligible"]),
                         ("active_pointer_mismatch", True, False, False))
        f_unc = by_flow[opaque("alice", "flow", "f-uncertain")]
        self.assertEqual((f_unc["classification"], f_unc["candidate_consent_id"]),
                         ("uncertain_exchange_review", opaque("alice", "consent", "c-uncertain")))
        f_pru = by_flow[opaque("alice", "flow", "f-prunable")]
        self.assertEqual((f_pru["classification"], f_pru["candidate_consent_id"], f_pru["local_retire_eligible"]),
                         ("terminal_prunable", None, True))

        # Owner isolation: Bob sees only Bob's rows.
        bob_resp = await self.get("reconcile", headers=self.headers("bob", origin=False))
        self.assertEqual(bob_resp.status, 200)
        bob_body = await bob_resp.json()
        self.assertEqual((bob_body["summary"]["consents_total"], bob_body["summary"]["flows_total"]), (1, 1))
        self.assertEqual(bob_body["consents"][0]["connection_id"], "bob-bank")

        # Direct store call with default now=None uses store.clock.
        self.assertEqual(real.store.reconcile("alice")["evaluated_at"], fixed_now)

        # Auth, method, query and flag guards fail closed without touching provider or DB rows.
        for user, status in (("viewer", 403), ("operator", 403), ("disabled", 401)):
            denied = await self.get("reconcile", headers=self.headers(user, origin=False))
            self.assertEqual(denied.status, status); await denied.read()
        bearer = await self.get("reconcile", headers=self.headers(bearer=True, origin=False))
        self.assertEqual(bearer.status, 403); await bearer.read()
        query = await self.get("reconcile", params={"retention_days": "1"})
        self.assertEqual(query.status, 400); await query.read()
        head = await self.client.head(module.PREFIX + "/reconcile", headers=self.headers(origin=False))
        self.assertEqual(head.status, 405); await head.read()
        with mock.patch.dict(os.environ, {module.FLAG: "0"}):
            off = await self.get("reconcile")
            self.assertEqual(off.status, 503); await off.read()

        # Stored DB integrity error -> 500; evaluation cap -> 413.
        real.clock = lambda: -1.0
        bad = await self.get("reconcile")
        self.assertEqual(bad.status, 500)
        self.assertEqual((await bad.json())["error"], "budget_consent_data_error")

        real.clock = lambda: fixed_now
        with mock.patch.object(cr, "classify_consents", side_effect=cr.ConsentReconcileLimitError()):
            over = await self.get("reconcile")
            self.assertEqual(over.status, 413)
            self.assertEqual((await over.json())["error"], "budget_consent_limit")
        self.assertEqual(calls, [])
        self.audit.assert_not_called()
