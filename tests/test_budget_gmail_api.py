"""Synthetic HTTP integration for the default-off Gmail receipt routes."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

from aiohttp import web as aio_web
from aiohttp.test_utils import TestClient, TestServer

from hestia import api_contract, auth, db, store_sql, web
from hestia.budget import gmail_api, gmail_config, receipt_api
from hestia.budget.gmail_client import GmailClient
from tests.test_budget_gmail_config import HMAC_KEY, MAILBOX, OAUTH_KEY, OAUTH_SECRET, REFRESH_TOKEN
from tests.test_budget_gmail_sync import FakeClock, FakeGmailTransport

_ORIGIN = "https://hestia.example.test"
_SESSION = b"synthetic-session-material-for-http-tests"
_GMAIL_ROUTES = {"gmail-status": "GET", "gmail-grant": "POST", "gmail-sync": "POST", "gmail-revoke": "POST"}
_RECEIPT_ID = "5e7e0ddd-8374-4f87-8b25-6f0360330a4a"


class GmailHTTPTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        path = Path(directory.name)
        self.settings = path / "gmail.json"
        refresh = path / "gmail.refresh"
        key = path / "gmail.key"
        refresh.write_text(REFRESH_TOKEN, encoding="ascii")
        key.write_bytes(HMAC_KEY)
        refresh.chmod(0o600)
        key.chmod(0o600)
        self.settings.write_text(json.dumps({
            "owner": "alice", "owner_mailbox": MAILBOX, "client_id_env": "SYNTHETIC_CLIENT_ID",
            "client_secret_env": "SYNTHETIC_CLIENT_SECRET", "refresh_token_env": "SYNTHETIC_REFRESH_FILE",
            "sender_allowlist": ["@merchant.example.test"], "spouse_forward_senders": ["spouse@example.test"],
        }), encoding="utf-8")
        self.settings.chmod(0o600)
        self.env = {
            "HESTIA_DB": str(path / "gmail-api.db"), "HESTIA_PERSIST": "sqlite", "HESTIA_BUDGET_HTTP_ENABLED": "1",
            receipt_api.FLAG: "1", "HESTIA_BUDGET_PUBLIC_ORIGIN": _ORIGIN,
            gmail_config.CONFIG_FILE: str(self.settings), gmail_config.HMAC_FILE: str(key),
            "SYNTHETIC_CLIENT_ID": OAUTH_KEY, "SYNTHETIC_CLIENT_SECRET": OAUTH_SECRET,
            "SYNTHETIC_REFRESH_FILE": str(refresh),
        }
        self.clock = FakeClock()
        self.transport = FakeGmailTransport()
        self.gmail = GmailClient(self.transport, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.worker = mock.Mock(ready=mock.Mock(return_value=True))
        for patch in (
            mock.patch.dict(os.environ, self.env, clear=True),
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", _SESSION),
            mock.patch.object(gmail_api, "GmailClient", return_value=self.gmail),
            mock.patch.object(gmail_api, "ReceiptParserWorker", return_value=self.worker),
            mock.patch.object(gmail_api.time, "time", self.clock.wall),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        engine, self.sessions = db.init_db()
        store_sql.cutover_users(engine, {"alice": "unused", "other": "unused"})
        with self.sessions.begin() as session:
            session.add(db.User(username="spouse", role="viewer", password_hash="unused"))
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def headers(self, user="alice", *, bearer=False, origin=True):
        token = auth.make_session(user, now=self.clock.wall(), secret=_SESSION)
        headers = {"Authorization": "Bearer " + token} if bearer else {"Cookie": "hestia_session=" + token}
        if origin:
            headers["Origin"] = _ORIGIN
        return headers

    async def call(self, operation, *, headers=None, **kwargs):
        return await self.client.request(_GMAIL_ROUTES[operation], gmail_api.PREFIX + "/" + operation,
                                         headers=self.headers() if headers is None else headers, **kwargs)

    def assert_private(self, response):
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")

    async def test_flag_default_off_does_not_read_credentials_or_start_import(self):
        with mock.patch.object(gmail_config, "load_settings", side_effect=AssertionError) as load:
            for operation, method in _GMAIL_ROUTES.items():
                with self.subTest(operation=operation):
                    response = await self.call(operation, **({"data": "bad JSON"} if method == "POST" else {}))
                    self.assertEqual(response.status, 404)
                    self.assertEqual((await response.json())["error"], "budget_receipts_gmail_disabled")
                    self.assert_private(response)
            load.assert_not_called()
        self.assertEqual(self.transport.calls, [])
        self.worker.ready.assert_not_called()

    async def test_admin_and_same_origin_browser_are_required_on_every_new_route(self):
        os.environ[gmail_api.FLAG] = "1"
        for operation in _GMAIL_ROUTES:
            with self.subTest(operation=operation):
                self.assertEqual((await self.call(operation, headers={})).status, 401)
                spouse = await self.call(operation, headers=self.headers("spouse"))
                self.assertEqual(spouse.status, 403)
                self.assert_private(spouse)
                native = await self.call(operation, headers=self.headers(bearer=True))
                self.assertEqual(native.status, 403)
                self.assertEqual((await native.json())["error"], "budget_browser_session_required")
                self.assertEqual((await self.call(operation, headers=self.headers(origin=False))).status, 403)

        same_origin_get = self.headers(origin=False) | {"Sec-Fetch-Site": "same-origin"}
        response = await self.call("gmail-status", headers=same_origin_get)
        self.assertEqual(response.status, 200)
        self.assertTrue(api_contract.BudgetReceiptGmailStatus.model_validate(await response.json()).configured)
        wrong_origin = same_origin_get | {"Origin": "https://other.example.test"}
        self.assertEqual((await self.call("gmail-status", headers=wrong_origin)).status, 403)
        self.assertEqual((await self.call("gmail-grant", headers=same_origin_get)).status, 403)
        self.assertEqual((await self.call("gmail-status", params={"test": "1"})).status, 400)
        self.assertEqual((await self.client.head(gmail_api.PREFIX + "/gmail-status", headers=self.headers())).status, 405)
        self.assertEqual(self.transport.calls, [])

    async def test_routes_require_receipt_flag_and_authoritative_sqlite(self):
        os.environ[gmail_api.FLAG] = "1"
        for key in ("HESTIA_BUDGET_HTTP_ENABLED", receipt_api.FLAG):
            with mock.patch.dict(os.environ, {key: "0"}):
                self.assertEqual((await self.call("gmail-status")).status, 404)
        with mock.patch.object(store_sql, "current_user_role", return_value="admin"):
            with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
                self.assertEqual((await self.call("gmail-status")).status, 409)
            with mock.patch.object(store_sql, "is_users_db_authoritative", return_value=False):
                self.assertEqual((await self.call("gmail-status")).status, 409)
        self.assertEqual(self.transport.calls, [])

    async def test_strict_json_frames_and_body_size_before_gmail_access(self):
        os.environ[gmail_api.FLAG] = "1"
        for operation in ("gmail-grant", "gmail-sync", "gmail-revoke"):
            with self.subTest(operation=operation):
                headers = self.headers() | {"Content-Type": "application/json"}
                self.assertEqual((await self.call(operation, data=b" " * 4097, headers=headers)).status, 413)
                self.assertEqual((await self.call(operation, data="{}", chunked=True, headers=headers)).status, 411)
                self.assertEqual((await self.call(operation, data="{}")).status, 415)
                duplicate = '{"expected_version":1,"expected_version":1}'
                self.assertEqual((await self.call(operation, data=duplicate, headers=headers)).status, 400)
                self.assertEqual((await self.call(operation, json={"unknown": 1})).status, 400)
        self.assertEqual((await self.call("gmail-sync", json={"force_full_resync": "false"})).status, 400)
        self.assertEqual((await self.call("gmail-grant", json={"action": "yes", "expected_version": 1})).status, 400)
        self.assertEqual((await self.call("gmail-revoke", json={"expected_version": True})).status, 400)
        self.assertEqual(self.transport.calls, [])

    async def test_explicit_grant_cas_empty_sync_pause_and_revoke(self):
        os.environ[gmail_api.FLAG] = "1"
        status = api_contract.BudgetReceiptGmailStatus.model_validate(await (await self.call("gmail-status")).json())
        self.assertEqual((status.grant_status, status.grant_generation, status.version), ("disabled", 0, 1))
        self.assertTrue(status.google_revoke_affects_calendar_sync)
        self.assertEqual(status.google_revoke_url, "https://myaccount.google.com/linkedapps")
        self.assertEqual((await self.call("gmail-sync", json={})).status, 409)
        stale = await self.call("gmail-grant", json={"action": "enable", "expected_version": 2})
        self.assertEqual(stale.status, 409)
        self.assertEqual(self.transport.calls, [])

        granted = await self.call("gmail-grant", json={"action": "enable", "expected_version": status.version})
        self.assertEqual(granted.status, 200, await granted.text())
        grant = api_contract.BudgetReceiptGmailGrantResult.model_validate(await granted.json())
        self.assertEqual((grant.grant_status, grant.grant_generation, grant.version), ("active", 1, 2))
        self.assertEqual([path for path, _params in self.transport.calls], ["token", "/profile"])
        synced = await self.call("gmail-sync", json={"force_full_resync": True})
        self.assertEqual(synced.status, 200, await synced.text())
        batch = api_contract.BudgetReceiptGmailSyncResult.model_validate(await synced.json())
        self.assertEqual((batch.sync_phase, batch.imported_count, batch.skipped_count, batch.has_more), ("idle", 0, 0, False))
        self.assertEqual((await self.call("gmail-sync", json={})).status, 429)
        self.assertEqual(len(self.transport.requests("/profile")), 2)

        self.settings.unlink()
        self.worker.ready.return_value = False
        calls = self.transport.calls[:]
        paused = await self.call("gmail-grant", json={"action": "disable", "expected_version": grant.version})
        self.assertEqual(paused.status, 200)
        self.assertEqual((await paused.json())["version"], 3)
        revoked = await self.call("gmail-revoke", json={"expected_version": 3})
        self.assertEqual(revoked.status, 200)
        self.assertEqual((await revoked.json())["grant_status"], "revoked")
        body = await (await self.call("gmail-status")).json()
        self.assertEqual((body["configured"], body["pending_imported_count"], body["version"]), (False, 0, 4))
        self.assertEqual(self.transport.calls, calls)

    async def test_profile_mismatch_and_parser_unavailability_use_safe_errors(self):
        os.environ[gmail_api.FLAG] = "1"
        other = await self.call("gmail-status", headers=self.headers("other"))
        self.assertFalse((await other.json())["configured"])
        self.worker.ready.return_value = False
        status = await self.call("gmail-status")
        self.assertFalse((await status.json())["configured"])
        request = {"action": "enable", "expected_version": 1}
        unavailable = await self.call("gmail-grant", json=request)
        self.assertEqual(unavailable.status, 503)
        self.assertEqual((await unavailable.json())["error"], "budget_receipt_gmail_parser_unavailable")
        self.assertEqual(self.transport.calls, [])
        self.worker.ready.return_value = True
        other_mailbox = "unrelated@example.test"
        self.transport.profile["emailAddress"] = other_mailbox
        denied = await self.call("gmail-grant", json=request)
        self.assertEqual(denied.status, 403, await denied.text())
        self.assertEqual((await denied.json())["error"], "budget_receipt_gmail_mailbox_mismatch")
        state_text = await (await self.call("gmail-status")).text()
        self.assertNotIn(other_mailbox, state_text)
        self.assertNotIn(MAILBOX, state_text)
        self.assertNotIn(REFRESH_TOKEN, state_text)
        self.assertEqual(json.loads(state_text)["attention"], "identity_mismatch")
        self.settings.write_text("missing valid configuration", encoding="utf-8")
        blocked = await self.call("gmail-sync", json={})
        self.assertEqual(blocked.status, 503)
        self.assertEqual((await blocked.json())["error"], "budget_receipt_gmail_unconfigured")
        del os.environ[gmail_config.CONFIG_FILE]
        absent_settings = await self.call("gmail-status")
        self.assertFalse((await absent_settings.json())["configured"])

    async def test_receipt_reads_negotiated_enum_values_and_document_fields(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(
                id=_RECEIPT_ID, owner="alice", status="imported", source_kind="email", version=1,
                created_at=self.clock.now, updated_at=self.clock.now, document_kind="invoice",
                invoice_number="FV/123", receipt_date="2026-11-02", due_date="2026-11-25", currency="PLN",
                total_amount="85", import_expires_at=self.clock.now + 60,
            ))
        for version in (None, "0.36.0", "0.40.0"):
            with self.subTest(version=version):
                headers = self.headers()
                if version is not None:
                    headers["X-Hestia-Contract-Version"] = version
                native = version == "0.40.0"
                listing = await self.client.post(receipt_api.PREFIX + "/list", json=(
                    {"status": "imported", "document_kind": "invoice"} if native else {}), headers=headers)
                detail = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": _RECEIPT_ID},
                                               headers=headers)
                self.assertEqual(listing.status, 200, await listing.text())
                self.assertEqual(detail.status, 200, await detail.text())
                for receipt in ((await listing.json())["receipts"][0], (await detail.json())["receipt"]):
                    self.assertEqual(receipt["status"], "imported" if native else "draft")
                    self.assertEqual(receipt["source_kind"], "email" if native else "live_text")
                    self.assertEqual(receipt.get("invoice_number"), "FV/123" if native else None)
                    self.assertEqual(receipt.get("due_date"), "2026-11-25" if native else None)

    async def test_receipt_version_is_validated_before_reading_body(self):
        for version in ("bad", "1.0.0", "0.40.1", "0.040.0"):
            with self.subTest(version=version):
                headers = self.headers() | {"X-Hestia-Contract-Version": version}
                response = await self.client.post(receipt_api.PREFIX + "/parse-text", data="not JSON", headers=headers)
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "budget_receipt_contract_version_invalid")
        token_headers = [*self.headers().items(), ("X-Hestia-Contract-Version", "0.36.0"),
                         ("X-Hestia-Contract-Version", "0.40.0")]
        response = await self.client.post(receipt_api.PREFIX + "/list", json={}, headers=token_headers)
        self.assertEqual(response.status, 400)


class GmailPollLifecycleTests(IsolatedAsyncioTestCase):
    async def test_feature_off_never_creates_poll_task(self):
        app = aio_web.Application()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(gmail_api, "_Runtime") as runtime:
            gmail_api.register(app, None, auth_enabled=lambda: True, session_secret=lambda: _SESSION)
            context = app.cleanup_ctx[-1](app)
            await context.__anext__()
            with self.assertRaises(StopAsyncIteration):
                await context.__anext__()
            runtime.assert_not_called()

    async def test_enabled_poll_runs_off_loop_and_cancels(self):
        app = aio_web.Application()
        before_next_tick = asyncio.Event()
        worker_threads = []

        def poll(*_args):
            worker_threads.append(threading.get_ident())

        async def sleep_for_next_tick(seconds):
            self.assertEqual(seconds, 60)
            before_next_tick.set()
            await asyncio.Event().wait()

        setup = {gmail_api.FLAG: "1", "HESTIA_BUDGET_HTTP_ENABLED": "1", receipt_api.FLAG: "1",
                 "HESTIA_PERSIST": "sqlite"}
        with mock.patch.dict(os.environ, setup, clear=True), \
                mock.patch.object(gmail_api, "_Runtime") as runtime, \
                mock.patch.object(gmail_api.asyncio, "sleep", sleep_for_next_tick):
            runtime.return_value.poll.side_effect = poll
            gmail_api.register(app, None, auth_enabled=lambda: True, session_secret=lambda: _SESSION)
            context = app.cleanup_ctx[-1](app)
            await context.__anext__()
            await asyncio.wait_for(before_next_tick.wait(), 2)
            with self.assertRaises(StopAsyncIteration):
                await context.__anext__()
            runtime.return_value.poll.assert_called_once()
            worker_thread, = worker_threads
            self.assertNotEqual(worker_thread, threading.get_ident())

    def test_poll_uses_configured_owner_with_safe_error_handling(self):
        service = mock.Mock()
        settings = SimpleNamespace(owner="alice")
        flags = {gmail_api.FLAG: "1", "HESTIA_BUDGET_HTTP_ENABLED": "1", receipt_api.FLAG: "1",
                 "HESTIA_PERSIST": "sqlite"}
        with mock.patch.dict(os.environ, flags, clear=True), \
                mock.patch.object(gmail_api, "GmailClient", return_value=mock.Mock()), \
                mock.patch.object(gmail_api, "_settings", return_value=settings), \
                mock.patch.object(gmail_api, "_store"), \
                mock.patch.object(gmail_api._Runtime, "_service", return_value=service):
            runtime = gmail_api._Runtime()
            runtime.poll(lambda: False, lambda: _SESSION)
            service.sync.assert_not_called()
            runtime.poll(lambda: True, lambda: _SESSION)
            service.sync.assert_called_once_with("alice", poll=True)
            service.sync.side_effect = gmail_api.GmailError("quota", 429)
            runtime.poll(lambda: True, lambda: _SESSION)
            service.sync.side_effect = RuntimeError("synthetic-PII")
            with self.assertLogs("hestia.budget.api", "WARNING") as logs:
                runtime.poll(lambda: True, lambda: _SESSION)
            self.assertIn("RuntimeError", logs.output[0])
            self.assertNotIn("synthetic-PII", logs.output[0])
