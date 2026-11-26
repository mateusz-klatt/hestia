"""Synthetic HTTP integration for the default-off Gmail receipt routes."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

from aiohttp import web as aio_web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from hestia import api_contract, auth, db, store_sql, web
from hestia.budget import gmail_api, gmail_config, receipt_api
from hestia.budget.gmail_client import GmailClient
from tests.test_budget_gmail_config import HMAC_KEY, MAILBOX, OAUTH_KEY, OAUTH_SECRET, REFRESH_TOKEN
from tests.test_budget_gmail_sync import FakeClock, FakeGmailTransport

_ORIGIN = "https://hestia.example.test"
_SESSION = b"synthetic-session-material-for-http-tests"
_GMAIL_ROUTES = {"gmail-status": "GET", "gmail-grant": "POST", "gmail-sync": "POST", "gmail-revoke": "POST"}
_RECEIPT_ID = "5e7e0ddd-8374-4f87-8b25-6f0360330a4a"
_GATEWAY_RECEIPT_ID = "e88b036b-4560-4568-bda3-d1d6d9c10c05"


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
            "sender_allowlist": ["@merchant.example.test", "@respectenergy.pl"], "spouse_forward_senders": ["spouse@example.test"],
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
        admin_state = store_sql.AuthState(
            backend="json",
            session_phase="legacy",
            policy_phase="legacy",
            enabled=True,
            role="admin",
            generation=0,
        )
        with mock.patch.object(store_sql, "load_auth_state", return_value=admin_state):
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
        self.assertEqual((await self.call("gmail-sync", json={"retry_parse_failed": "false"})).status, 400)
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
        self.assertEqual(self.transport.calls, [])
        synced = await self.call("gmail-sync", json={"force_full_resync": True})
        self.assertEqual(synced.status, 202, await synced.text())
        accepted = api_contract.BudgetReceiptGmailSyncResult.model_validate(await synced.json())
        self.assertEqual((accepted.ok, accepted.sync_phase, accepted.version), (True, "idle", grant.version))
        self.assertEqual(len(self.transport.requests("/profile")), 0)
        queued = await (await self.call("gmail-status")).json()
        self.assertEqual((queued["sync_phase"], queued["batch_processed_count"], queued["next_sync_at"]),
                         ("idle", 0, self.clock.now))

        self.settings.unlink()
        self.worker.ready.return_value = False
        calls = self.transport.calls[:]
        paused = await self.call("gmail-grant", json={"action": "disable", "expected_version": accepted.version})
        self.assertEqual(paused.status, 200)
        self.assertEqual((await paused.json())["version"], accepted.version + 1)
        revoked = await self.call("gmail-revoke", json={"expected_version": accepted.version + 1})
        self.assertEqual(revoked.status, 200)
        self.assertEqual((await revoked.json())["grant_status"], "revoked")
        body = await (await self.call("gmail-status")).json()
        self.assertEqual((body["configured"], body["pending_imported_count"], body["batch_processed_count"],
                          body["version"]), (False, 0, 0, accepted.version + 2))
        self.assertEqual(self.transport.calls, calls)

    async def test_grant_changes_return_busy_promptly_while_the_provider_lock_is_held(self):
        os.environ[gmail_api.FLAG] = "1"
        requests = (
            ("gmail-grant", {"action": "enable", "expected_version": 1}),
            ("gmail-grant", {"action": "disable", "expected_version": 1}),
            ("gmail-revoke", {"expected_version": 1}),
        )
        with self.gmail.lock("alice"):
            for operation, body in requests:
                with self.subTest(operation=operation, body=body):
                    blocked = await asyncio.wait_for(self.call(operation, json=body), timeout=2)
                    self.assertEqual(blocked.status, 409)
                    self.assertEqual((await blocked.json())["error"], "budget_receipt_gmail_busy")
        self.worker.ready.assert_not_called()
        self.assertEqual(self.transport.calls, [])
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetGmailState, "alice"))

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
        granted = await self.call("gmail-grant", json=request)
        self.assertEqual(granted.status, 200, await granted.text())
        self.assertEqual(self.transport.calls, [])
        gmail_api._Runtime().poll(lambda: True, lambda: _SESSION)
        self.assertEqual(self.transport.requests("/profile"), [{}])
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

    async def test_gateway_evidence_requires_contract_0_52_or_later(self):
        shared = dict(owner="alice", status="imported", source_kind="email", version=1,
                      created_at=self.clock.now, updated_at=self.clock.now, receipt_date="2026-11-02",
                      currency="PLN", import_expires_at=self.clock.now + 60)
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(id=_RECEIPT_ID, document_kind="invoice", total_amount="85", **shared))
            session.flush()
            session.add(db.BudgetReceipt(
                id=_GATEWAY_RECEIPT_ID, document_kind="payment_confirmation", total_amount="86",
                gateway="Example Pay", gateway_status="paid", fee_amount="1", payee_host="shop.example.test",
                evidence_for_receipt_id=_RECEIPT_ID, **shared,
            ))
        gateway_fields = {
            "gateway": "Example Pay", "gateway_status": "paid", "fee_amount": "1",
            "payee_host": "shop.example.test", "evidence_for_receipt_id": _RECEIPT_ID, "has_document": True,
        }
        gateway_contracts = ("0.52.0", "0.53.0", api_contract.CONTRACT_VERSION)
        for version in (None, "0.40.0", "0.51.0", *gateway_contracts):
            with self.subTest(version=version):
                headers = self.headers()
                if version is not None:
                    headers["X-Hestia-Contract-Version"] = version
                listing = await self.client.post(
                    receipt_api.PREFIX + "/list", json={"evidence": "only"}, headers=headers)
                self.assertEqual(listing.status, 200, await listing.text())
                (payment_summary,) = (await listing.json())["receipts"]
                details = []
                for receipt_id in (_GATEWAY_RECEIPT_ID, _RECEIPT_ID):
                    response = await self.client.get(receipt_api.PREFIX + "/detail",
                                                     params={"receipt_id": receipt_id}, headers=headers)
                    self.assertEqual(response.status, 200, await response.text())
                    details.append((await response.json())["receipt"])
                payment, document = details
                if version in gateway_contracts:
                    for receipt in (payment_summary, payment):
                        self.assertEqual({key: receipt[key] for key in gateway_fields}, gateway_fields)
                    self.assertNotIn("payment_evidence", payment_summary)
                    self.assertEqual(payment["payment_evidence"], [])
                    self.assertEqual(document["payment_evidence"], [{
                        "receipt_id": _GATEWAY_RECEIPT_ID, "gateway": "Example Pay", "gateway_status": "paid",
                        "total_amount": "86", "fee_amount": "1", "receipt_date": "2026-11-02",
                    }])
                else:
                    for receipt in (payment_summary, payment, document):
                        self.assertTrue(receipt.keys().isdisjoint(gateway_fields))
                        self.assertNotIn("payment_evidence", receipt)

    async def test_default_list_accepts_a_pre_evidence_cursor(self):
        shared = dict(owner="alice", status="imported", source_kind="email", document_kind="invoice", version=1,
                      created_at=self.clock.now, updated_at=self.clock.now, currency="PLN", total_amount="85",
                      import_expires_at=self.clock.now + 60)
        store = receipt_api._store(_SESSION)
        filters = {"month": "2026-11", "status": "imported", "linked": False, "document_kind": "invoice"}
        legacy_context = hashlib.sha256(
            json.dumps(filters, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
        with self.sessions.begin() as session:
            newest = db.BudgetReceipt(id=_RECEIPT_ID, receipt_date="2026-11-03", **shared)
            older = db.BudgetReceipt(id=_GATEWAY_RECEIPT_ID, receipt_date="2026-11-02", **shared)
            session.add_all((newest, older))
        legacy_payload = json.dumps(
            {"owner": "alice", "context": legacy_context, "last_date": "2026-11-03",
             "last_id": _RECEIPT_ID, "expires_at": self.clock.now + 60},
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        legacy_mac = hmac.digest(store._receipt_cursor_key, legacy_payload, "sha256")
        legacy_cursor = ".".join(
            base64.urlsafe_b64encode(part).rstrip(b"=").decode("ascii") for part in (legacy_payload, legacy_mac)
        )

        headers = self.headers() | {"X-Hestia-Contract-Version": api_contract.CONTRACT_VERSION}
        path = receipt_api.PREFIX + "/list"
        first = await self.client.post(path, json={**filters, "limit": 1}, headers=headers)
        self.assertEqual(first.status, 200, await first.text())
        current_cursor = (await first.json())["next_cursor"]
        self.assertIsInstance(current_cursor, str)
        self.assertNotEqual(current_cursor, legacy_cursor)
        for cursor_format, cursor in (("legacy", legacy_cursor), ("current", current_cursor)):
            with self.subTest(cursor_format=cursor_format):
                second = await self.client.post(path, json={**filters, "cursor": cursor}, headers=headers)
                self.assertEqual(second.status, 200, await second.text())
                self.assertEqual([row["receipt_id"] for row in (await second.json())["receipts"]], [_GATEWAY_RECEIPT_ID])
                for evidence in ("only", "include"):
                    with self.subTest(evidence=evidence):
                        wrong_scope = await self.client.post(
                            path, json={**filters, "evidence": evidence, "cursor": cursor}, headers=headers)
                        self.assertEqual(wrong_scope.status, 400)

    async def test_manual_forward_review_metadata_never_exposes_the_inner_address(self):
        with self.sessions.begin() as session:
            session.add(db.BudgetReceipt(
                id=_RECEIPT_ID, owner="alice", status="needs_review", source_kind="email", version=1,
                created_at=self.clock.now, updated_at=self.clock.now, document_kind="invoice",
                raw_shop_name="przekazane ręcznie", forwarded_manually=1,
                forwarded_sender_domain="corner-shop.example",
            ))
        for version in (None, "0.40.0", "0.42.0"):
            with self.subTest(version=version):
                headers = self.headers()
                if version is not None:
                    headers["X-Hestia-Contract-Version"] = version
                native = version is not None
                listing = await self.client.post(
                    receipt_api.PREFIX + "/list", json={"status": "needs_review"} if native else {}, headers=headers)
                detail = await self.client.get(receipt_api.PREFIX + "/detail", params={"receipt_id": _RECEIPT_ID},
                                               headers=headers)
                self.assertEqual(listing.status, 200, await listing.text())
                self.assertEqual(detail.status, 200, await detail.text())
                list_body = await listing.json()
                detail_body = await detail.json()
                api_contract.BudgetReceiptListResult.model_validate(list_body)
                api_contract.BudgetReceiptDetailResult.model_validate(detail_body)
                for receipt in (list_body["receipts"][0], detail_body["receipt"]):
                    self.assertEqual((receipt["status"], receipt["raw_shop_name"], receipt["linked"],
                                      receipt["linked_account_id"]),
                                     ("needs_review", "przekazane ręcznie", False, None))
                    self.assertEqual(receipt.get("forwarded_manually"), True if native else None)
                    self.assertEqual(receipt.get("forwarded_sender_domain"),
                                     "corner-shop.example" if native else None)
                self.assertNotIn("billing@corner-shop.example", await detail.text())

    async def test_receipt_version_is_validated_before_reading_body(self):
        major, minor, patch = (int(part) for part in api_contract.CONTRACT_VERSION.split("."))
        newer = f"{major}.{minor}.{patch + 1}"  # one patch above the served contract
        for version in ("bad", "1.0.0", newer, "0.040.0"):
            with self.subTest(version=version):
                headers = self.headers() | {"X-Hestia-Contract-Version": version}
                response = await self.client.post(receipt_api.PREFIX + "/parse-text", data="not JSON", headers=headers)
                self.assertEqual(response.status, 400)
                self.assertEqual((await response.json())["error"], "budget_receipt_contract_version_invalid")
        token_headers = [*self.headers().items(), ("X-Hestia-Contract-Version", "0.36.0"),
                         ("X-Hestia-Contract-Version", "0.40.0")]
        response = await self.client.post(receipt_api.PREFIX + "/list", json={}, headers=token_headers)
        self.assertEqual(response.status, 400)

    async def test_overlapping_kicks_coalesce_into_the_running_round(self):
        os.environ[gmail_api.FLAG] = "1"
        granted = await self.call("gmail-grant", json={"action": "enable", "expected_version": 1})
        self.assertEqual(granted.status, 200)
        runtime = gmail_api._Runtime()
        await asyncio.to_thread(runtime.poll, lambda: True, lambda: _SESSION)

        self.clock.now += 60
        scheduled = await self.call("gmail-sync", json={})
        self.assertEqual(scheduled.status, 202)
        version = (await scheduled.json())["version"]
        started, release = threading.Event(), threading.Event()

        def slow_history(_params):
            started.set()
            self.assertTrue(release.wait(5))
            return response_json({"history": []})

        self.transport.routes[("/history", None)] = slow_history
        profiles = len(self.transport.requests("/profile"))
        message_scans = len(self.transport.requests("/messages"))
        polling = asyncio.create_task(asyncio.to_thread(runtime.poll, lambda: True, lambda: _SESSION))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            self.clock.now += 61
            kicks = await asyncio.gather(*(self.call("gmail-sync", json=body) for body in (
                {}, {"force_full_resync": True}, {},
            )))
            for kick in kicks:
                self.assertEqual(kick.status, 202)
                self.assertEqual((await kick.json())["version"], version)
        finally:
            release.set()
            await polling

        await asyncio.to_thread(runtime.poll, lambda: True, lambda: _SESSION)
        self.assertEqual(len(self.transport.requests("/profile")), profiles + 1)
        self.assertEqual(len(self.transport.requests("/history")), 1)
        self.assertEqual(len(self.transport.requests("/messages")), message_scans + 1)
        with self.sessions() as session:
            state = session.get(db.BudgetGmailState, "alice")
            self.assertEqual((state.run_requested, state.force_full_resync, state.version), (0, 0, version))

    async def test_sync_schedules_immediately_and_the_poller_imports(self):
        os.environ[gmail_api.FLAG] = "1"
        granted = await self.call("gmail-grant", json={"action": "enable", "expected_version": 1})
        self.assertEqual(granted.status, 200, await granted.text())
        message_id = "mail-scheduled"
        message = _scheduled_invoice()
        self.transport.routes[("/messages", None)] = response_json(page_ids(message_id))
        self.transport.routes[("/messages/" + message_id, None)] = _provider_message(message_id, message)
        parsed = []

        def slow_parse(_kind, payload):
            parsed.append(payload)
            time.sleep(1.2)
            return payload.decode("utf-8")

        self.worker.parse.side_effect = slow_parse
        profiles = len(self.transport.requests("/profile"))
        started = time.perf_counter()
        scheduled = await self.call("gmail-sync", json={})
        elapsed = time.perf_counter() - started
        self.assertEqual(scheduled.status, 202, await scheduled.text())
        self.assertLess(elapsed, 1.0)
        self.assertEqual(parsed, [])
        self.assertEqual(len(self.transport.requests("/profile")), profiles)
        body = api_contract.BudgetReceiptGmailSyncResult.model_validate(await scheduled.json())
        self.assertEqual((body.ok, body.sync_phase), (True, "idle"))
        progress = await (await self.call("gmail-status")).json()
        self.assertEqual((progress["sync_phase"], progress["batch_processed_count"], progress["next_sync_at"]),
                         ("idle", 0, self.clock.now))

        gmail_api._Runtime().poll(lambda: True, lambda: _SESSION)
        self.assertEqual(len(parsed), 1)
        self.assertEqual((await self.call("gmail-sync", json={})).status, 429)
        with self.sessions() as session:
            receipt, = list(session.scalars(select(db.BudgetReceipt)))
            self.assertEqual((receipt.status, receipt.invoice_number, receipt.raw_ocr_text),
                             ("imported", "RE/2026/00042", None))
            session.connection().exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        stored = Path(self.env["HESTIA_DB"]).read_bytes()
        self.assertNotIn(b"synthetic-body-only-marker", stored)
        self.assertNotIn(b"billing@respectenergy.pl", stored)
        after = await (await self.call("gmail-status")).json()
        self.assertEqual((after["sync_phase"], after["pending_imported_count"], after["batch_processed_count"],
                          after["parse_failed_reasons"]),
                         ("idle", 1, 0, {}))

    async def test_pdf_diagnostics_are_version_gated(self):
        os.environ[gmail_api.FLAG] = "1"
        original = gmail_api._Runtime._status
        def status(runtime, store, owner, secret):
            result = original(runtime, store, owner, secret)
            result["parse_failed_reasons"] = {"pdf_no_text": 1, "multiple_documents": 2, "other": 4}
            return result
        with mock.patch.object(gmail_api._Runtime, "_status", status):
            old = await self.call("gmail-status")
            self.assertEqual((await old.json())["parse_failed_reasons"], {"other": 7})
            boundary = await self.call("gmail-status", headers=self.headers() | {
                "X-Hestia-Contract-Version": "0.55.0",
            })
            self.assertEqual((await boundary.json())["parse_failed_reasons"], {"other": 7})
            for version in ("0.56.0", "0.57.0"):
                with self.subTest(version=version):
                    current = await self.call("gmail-status", headers=self.headers() | {
                        "X-Hestia-Contract-Version": version,
                    })
                    self.assertEqual((await current.json())["parse_failed_reasons"], {
                        "pdf_no_text": 1, "multiple_documents": 2, "other": 4,
                    })
            invalid = await self.call("gmail-status", headers=self.headers() | {
                "X-Hestia-Contract-Version": "invalid",
            })
            self.assertEqual(invalid.status, 400)

    async def test_gmail_status_exposes_parse_failed_reasons_and_logs_safely(self):
        os.environ[gmail_api.FLAG] = "1"
        granted = await self.call("gmail-grant", json={"action": "enable", "expected_version": 1})
        self.assertEqual(granted.status, 200)

        from hestia.budget.gmail_worker import PDFUnsupported

        pdf_mail = _scheduled_invoice()
        pdf_mail["Subject"] = "Sensitive Subject Alpha"
        pdf_mail.set_content(b"%PDF-1.4 fixture", maintype="application", subtype="pdf")

        octet_pdf = _scheduled_invoice()
        octet_pdf["Subject"] = "Sensitive Subject Beta"
        octet_pdf.set_content("See attached PDF")
        octet_pdf.add_attachment(b"%PDF-1.4 fixture", maintype="application", subtype="octet-stream",
                                 filename="Invoice.PDF")

        bad_charset = _scheduled_invoice()
        bad_charset["Subject"] = "Sensitive Subject Gamma"
        bad_charset.set_param("charset", "not-a-real-charset")

        messages = {"msg-pdf": pdf_mail, "msg-octet": octet_pdf, "msg-charset": bad_charset}
        self.transport.routes[("/messages", None)] = response_json(page_ids(*messages))
        for msg_id, msg in messages.items():
            self.transport.routes[("/messages/" + msg_id, None)] = _provider_message(msg_id, msg)

        def parse_part(kind, payload):
            if kind == "pdf":
                raise PDFUnsupported("sensitive-pdf-detail")
            return payload.decode("utf-8")

        self.worker.parse.side_effect = parse_part
        with self.assertLogs("hestia.budget.gmail_message", level="WARNING") as logs:
            await asyncio.to_thread(gmail_api._Runtime().poll, lambda: True, lambda: _SESSION)

        self.assertEqual(sorted(logs.output), [
            "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=charset_error",
            "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=pdf_unsupported",
            "WARNING:hestia.budget.gmail_message:gmail parse_failed reason=pdf_unsupported",
        ])
        for entry in logs.output:
            for secret in (*messages, "Sensitive", "billing@merchant.example.test", "sensitive-pdf-detail"):
                self.assertNotIn(secret, entry)

        response = await self.call("gmail-status")
        self.assertEqual(response.status, 200)
        body = await response.json()
        validated = api_contract.BudgetReceiptGmailStatus.model_validate(body)
        self.assertEqual(validated.parse_failed_reasons, {"charset_error": 1, "pdf_unsupported": 2})

        with self.sessions() as session:
            session.connection().exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        stored = Path(self.env["HESTIA_DB"]).read_bytes()
        for secret in (*messages, "Sensitive Subject", "billing@merchant.example.test", "sensitive-pdf-detail"):
            self.assertNotIn(secret.encode(), stored)

    async def test_retry_parse_failed_route_and_cli_reprocess_failed_messages(self):
        os.environ[gmail_api.FLAG] = "1"
        granted = await self.call("gmail-grant", json={"action": "enable", "expected_version": 1})
        self.assertEqual(granted.status, 200)

        from contextlib import redirect_stderr, redirect_stdout
        import io
        from hestia.budget import __main__ as cli
        from hestia.budget.gmail_worker import PDFUnsupported

        ok_mail = _scheduled_invoice()
        pdf_mail = _scheduled_invoice()
        pdf_mail.set_content(b"%PDF-1.4 fixture", maintype="application", subtype="pdf")
        self.transport.routes[("/messages", None)] = response_json(page_ids("msg-ok", "msg-pdf"))
        self.transport.routes[("/messages/msg-ok", None)] = _provider_message("msg-ok", ok_mail)
        self.transport.routes[("/messages/msg-pdf", None)] = _provider_message("msg-pdf", pdf_mail)

        def fail_pdf(kind, payload):
            if kind == "pdf":
                raise PDFUnsupported()
            return payload.decode("utf-8")

        self.worker.parse.side_effect = fail_pdf
        await asyncio.to_thread(gmail_api._Runtime().poll, lambda: True, lambda: _SESSION)
        status = await (await self.call("gmail-status")).json()
        self.assertEqual((status["pending_imported_count"], status["parse_failed_reasons"]),
                         (1, {"pdf_unsupported": 1}))

        # Advance 15 days (> 7d force_full_resync window, < 30d IMPORT_TTL) and trigger retry via HTTP.
        self.clock.now += 15 * 86400
        retry = await self.call("gmail-sync", json={"retry_parse_failed": True})
        self.assertEqual(retry.status, 202, await retry.text())
        api_contract.BudgetReceiptGmailSyncResult.model_validate(await retry.json())
        with self.sessions() as session:
            outcomes = sorted(row.outcome for row in session.scalars(select(db.BudgetGmailMessage)))
        self.assertEqual(outcomes, ["imported"])

        # Now PDF extraction succeeds; poller imports msg-pdf using full backfill window.
        self.worker.parse.side_effect = lambda kind, payload: (
            ok_mail.get_body(("plain",)).get_content() if kind == "pdf" else payload.decode("utf-8")
        )
        self.transport.profile["historyId"] = "250"
        await asyncio.to_thread(gmail_api._Runtime().poll, lambda: True, lambda: _SESSION)
        after = await (await self.call("gmail-status")).json()
        self.assertEqual((after["pending_imported_count"], after["parse_failed_reasons"], after["sync_phase"]),
                         (2, {}, "idle"))
        expected_cutoff = int(self.clock.now - 365 * 86400)
        self.assertIn(f"after:{expected_cutoff}", self.transport.requests("/messages")[-1]["q"][0])

        # Exercise the CLI subcommand path (including error boundaries and flag-less sync).
        def invoke_cli(argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli.time, "time", self.clock.wall), \
                    redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
            return code, out.getvalue(), err.getvalue()

        with self.sessions.begin() as session:
            session.add(db.BudgetGmailMessage(
                owner="alice", gmail_id_hmac="ab" * 32, internal_date=self.clock.now,
                trust_path="direct_allowlist", outcome="parse_failed", created_at=self.clock.now,
            ))
        self.clock.now += 60
        code, out, err = invoke_cli(["--db", self.env["HESTIA_DB"], "gmail", "sync", "--retry-parse-failed"])
        self.assertEqual((code, err), (0, ""))
        cli_result = api_contract.BudgetReceiptGmailSyncResult.model_validate(json.loads(out))
        self.assertTrue(cli_result.ok)
        self.assertNotIn("alice", out)
        self.assertNotIn(MAILBOX, out)
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetGmailMessage, ("alice", "ab" * 32)))
            state = session.get(db.BudgetGmailState, "alice")
            self.assertEqual((state.run_requested, state.force_full_resync), (1, 1))

        # Flag-less CLI sync coalesces without failing, and error branches stay safe.
        code, out, err = invoke_cli(["gmail", "sync"])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(json.loads(out)["ok"])

        with mock.patch.object(gmail_config, "load_settings", return_value=None):
            code, out, err = invoke_cli(["gmail", "sync"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG", err)

        with mock.patch.object(store_sql, "is_users_db_authoritative", return_value=False):
            code, out, err = invoke_cli(["gmail", "sync"])
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.strip(), "budget_requires_sqlite_users")

        with self.sessions.begin() as session:
            session.get(db.BudgetGmailState, "alice").grant_status = "disabled"
        code, out, err = invoke_cli(["gmail", "sync", "--retry-parse-failed"])
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.strip(), "budget_receipt_gmail_disabled")

        with mock.patch.object(gmail_config, "load_settings", side_effect=RuntimeError("SECRET_PATH")):
            code, out, err = invoke_cli(["gmail", "sync"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.strip(), "Gmail sync scheduling failed")
        self.assertNotIn("SECRET_PATH", err)


def _scheduled_invoice():
    # The issuer must match the authenticated sender domain.
    sender = "billing@respectenergy.pl"
    domain = "respectenergy.pl"
    body = (
        "Wystawca: Respect Energy\n"
        "Numer dokumentu: RE/2026/00042\n"
        "Kwota: 123,45 PLN\n"
        "Data wystawienia: 21.11.2026\n"
        "Termin płatności: 2026-12-05\n"
        "synthetic-body-only-marker\n"
    )
    message = EmailMessage()
    message["From"] = sender
    message["Authentication-Results"] = (
        f"mx.google.com; dkim=pass header.d={domain}; dmarc=pass header.from={domain}"
    )
    message.set_content(body)
    return message


def page_ids(*ids, token=None):
    return {"messages": [{"id": value} for value in ids], "nextPageToken": token}


def response_json(data, status=200, headers=None):
    from hestia.calendar_sync.providers import HTTPResponse
    return HTTPResponse(status, headers or {}, json.dumps(data).encode())


def _provider_message(message_id, message):
    payload = message.as_bytes()
    metadata = {"id": message_id, "sizeEstimate": len(payload), "internalDate": "1795566945123",
                "payload": {"headers": [{"name": name, "value": value} for name, value in message.raw_items()
                                        if name in {"From", "Sender", "Authentication-Results", "DKIM-Signature"}]}}
    raw = {"id": message_id, "raw": base64.urlsafe_b64encode(payload).decode().rstrip("=")}

    def select_response(params):
        if params["format"] == ["metadata"]:
            return response_json(metadata)
        assert params["format"] == ["raw"]
        return response_json(raw)

    return select_response


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

    def test_unknown_policy_phase_blocks_store_and_poll_before_secrets(self):
        import tempfile
        from hestia import authz

        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = Path(tmpdir) / "gmail_custom.db"
            self.addCleanup(db.reset_engine_cache)
            engine, sessions = db.init_db(db_file)
            with sessions.begin() as s:
                s.add(db.AppMeta(key="authz_policy_phase", value="future_phase"))
            engine.dispose()
            flags = {gmail_api.FLAG: "1", "HESTIA_BUDGET_HTTP_ENABLED": "1", receipt_api.FLAG: "1",
                     "HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(db_file)}
            with mock.patch.dict(os.environ, flags, clear=True):
                with self.assertRaises(authz.PolicyPhaseUnsupported):
                    gmail_api._store(db_file)
                with mock.patch.object(gmail_api, "_settings", side_effect=AssertionError("read secrets")):
                    with self.assertRaises(authz.PolicyPhaseUnsupported):
                        gmail_api._Runtime().poll(lambda: True, lambda: _SESSION)
