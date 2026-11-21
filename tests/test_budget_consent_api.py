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
        for operation, key in (("pending", "pending"), ("status", "consents")):
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
