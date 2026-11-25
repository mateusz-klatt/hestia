"""Real HTTP/SQLite privacy and browser-control boundaries using synthetic data."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from multidict import MultiDict
from sqlalchemy import select

from hestia import api_contract, auth, authz, db, store_sql, web
from hestia.budget import api, config, runtime
from hestia.budget.config import BudgetConfigError
from hestia.budget.consent_files import ConsentFilesError
from hestia.budget.store import BudgetStore, BudgetStoreError
from hestia.budget.views import BudgetHTTPStore
from hestia.proxy import EventBus
from tests.test_budget_store import configuration, prepare_selected, snapshot, transaction


SECRET = b"synthetic-budget-api-session-secret"
ORIGIN = "https://hestia.example.test"
PRIVATE_A = "PRIVATE-ALICE-ACCOUNT-HASH"
PRIVATE_B = "PRIVATE-BOB-ACCOUNT-HASH"


class BudgetAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private.db"
        for patch in (
            mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path), "HESTIA_PERSIST": "sqlite",
                                         "HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_BUDGET_PUBLIC_ORIGIN": ORIGIN}),
            mock.patch.object(web, "_AUTH_ENABLED", True), mock.patch.object(web, "_SESSION_SECRET", SECRET),
            mock.patch.object(auth, "load_users", return_value={"alice": "synthetic-unused-password"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, self.sessions = db.init_db()
        store_sql.cutover_users(engine, {"alice": "unused", "bob": "unused"})
        with self.sessions.begin() as session:
            session.add_all([db.User(username="viewer", password_hash="unused", role="viewer"),
                             db.User(username="operator", password_hash="unused", role="operator"),
                             db.User(username="disabled", password_hash="unused", role="admin", disabled=True)])
        self.config = configuration(accounts=(PRIVATE_A,), owner="alice")
        other = replace(self.config.connections[0], id="bob-bank", owner="bob", accounts=(PRIVATE_B,))
        self.config = replace(self.config, connections=(*self.config.connections, other))
        self.store = BudgetStore(self.sessions)
        prepare_selected(self.store, self.config)
        self.store.commit_snapshot("bank", snapshot(transaction("alice-entry", remittance_information=["Alice private description"]),
                                                    transaction("alice-older", booking_date="2026-10-01"),
                                                    account_hash=PRIVATE_A, valid_until=web._now() + 31536000))
        self.store.commit_snapshot("bob-bank", snapshot(transaction("bob-entry", remittance_information=["Bob private description"]),
                                                        account_hash=PRIVATE_B, valid_until=web._now() + 31536000))
        accounts = self.rows(db.BudgetAccount)
        self.alice_id = next(row.id for row in accounts if row.owner == "alice")
        self.bob_id = next(row.id for row in accounts if row.owner == "bob")
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def token(self, user="alice", **kwargs):
        return auth.make_session(user, now=web._now(), secret=SECRET, **kwargs)

    def headers(self, user="alice", *, bearer=False, origin=False):
        token = self.token(user)
        headers = {"Authorization": "Bearer " + token} if bearer else {"Cookie": "hestia_session=" + token}
        if origin:
            headers["Origin"] = ORIGIN
        return headers

    async def get(self, operation, *, user="alice", params=None, headers=None):
        return await self.client.get(api.PREFIX + "/" + operation, params=params,
                                     headers=self.headers(user) if headers is None else headers)

    async def post(self, operation, body, *, user="alice", headers=None):
        headers = self.headers(user, origin=True) if headers is None else headers
        headers = ({**headers, "Content-Type": "application/json"} if isinstance(headers, dict)
                   else [*headers, ("Content-Type", "application/json")])
        return await self.client.post(api.PREFIX + "/" + operation, data=json.dumps(body), headers=headers)

    async def account(self):
        return (await (await self.get("accounts")).json())["accounts"][0]

    async def connection(self):
        return (await (await self.get("connections")).json())["connections"][0]

    def assert_private_headers(self, response):
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.headers.get("Referrer-Policy"), "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def test_all_reads_are_owner_private_stored_only_and_match_wire_models(self):
        models = {"connections": api_contract.BudgetConnections, "accounts": api_contract.BudgetAccounts,
                  "status": api_contract.BudgetStatus, "transactions": api_contract.BudgetTransactions,
                  "balances": api_contract.BudgetBalances, "limits": api_contract.BudgetLimits}
        with mock.patch.object(config, "load_config", side_effect=AssertionError("No configuration reads")), \
             mock.patch.object(BudgetStore, "configure", side_effect=AssertionError("No reconciliation")), \
             mock.patch.object(runtime, "credentials") as credentials, \
             mock.patch("urllib.request.OpenerDirector.open") as http, mock.patch.object(web, "_audit") as audit:
            for operation, model in models.items():
                params = {"account_id": self.alice_id} if operation in ("transactions", "balances") else None
                response = await self.get(operation, params=params)
                self.assertEqual(response.status, 200, await response.text())
                self.assert_private_headers(response)
                body = await response.json()
                model.model_validate(body)
                text = json.dumps(body)
                self.assertNotIn(PRIVATE_A, text)
                self.assertNotIn(PRIVATE_B, text)
                self.assertNotIn(self.bob_id, text)
                self.assertNotIn("Bob private description", text)
            bearer = await self.get("connections", headers=self.headers(bearer=True))
            self.assertEqual(bearer.status, 403)
        credentials.assert_not_called()
        http.assert_not_called()
        audit.assert_not_called()
        body = await (await self.get("transactions", params={"account_id": self.alice_id})).json()
        self.assertEqual(body["account_total"], 2)
        self.assertEqual(body["transactions"][0]["amount"], "12345678901234567890.12345")
        capabilities = (await (await self.get("connections")).json())["capabilities"]
        self.assertEqual(capabilities, {"stored_reads": True, "account_controls": True, "refresh": False,
                                       "background_selection": True, "background_polling": False,
                                       "consent": False, "analytics": False})

    async def test_display_metadata_and_observed_currencies_are_private_and_not_global_events(self):
        initial = await self.account()
        self.assertIsNone(initial["display"])
        self.assertEqual(initial["observed_balance_currencies"], ["PLN"])
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetAccountDisplay(account_id=self.alice_id, product="Alice synthetic card", currency="CHF",
                                        masked_suffix="A123", source="offline_import", observed_at=900,
                                        imported_at=1000, digest="PRIVATE-ALICE-DISPLAY-DIGEST"),
                db.BudgetAccountDisplay(account_id=self.bob_id, product="Bob private display", currency="USD",
                                        masked_suffix="B0B1", source="offline_import", observed_at=900,
                                        imported_at=1000, digest="PRIVATE-BOB-DISPLAY-DIGEST")])
            balance = {"name": "Synthetic booked", "balance_type": "CLBD",
                       "balance_amount": {"amount": "1", "currency": "EUR"}}
            session.get(db.BudgetAccount, self.alice_id).balances_json = json.dumps([
                balance, {**balance, "balance_amount": {"amount": "2", "currency": "PLN"}}, balance])
            archived = {**balance, "balance_amount": {"amount": "3", "currency": "GBP"}}
            session.add(db.BudgetImportBatch(id="display-archive", fingerprint="display-archive", account_id=self.alice_id,
                                             fetched_at=900, imported_at=1000, balances_json=json.dumps([archived]), report_json="{}"))
        bus = EventBus()
        self.client.server.app[web._RT_KEY].event_bus = bus
        subscription = await bus.try_subscribe()
        self.addCleanup(subscription.close)
        with mock.patch.object(web, "_audit") as global_audit:
            response = await self.get("accounts")
            self.assertEqual(response.status, 200)
            self.assert_private_headers(response)
            body = await response.json()
            api_contract.BudgetAccounts.model_validate(body)
            account = body["accounts"][0]
            self.assertEqual(account["display"], {"product": "Alice synthetic card", "currency": "CHF",
                                                  "masked_suffix": "A123", "source": "offline_import",
                                                  "observed_at": 900, "imported_at": 1000})
            self.assertEqual(account["observed_balance_currencies"], ["EUR", "PLN"])
            for private in (PRIVATE_A, PRIVATE_B, "DISPLAY-DIGEST", "Bob private display", "B0B1"):
                self.assertNotIn(private, json.dumps(body))
            saved = await self.post("account-selection", {"account_id": self.alice_id, "selected": False,
                                                           "expected_version": account["control_version"]})
            self.assertEqual(saved.status, 200)
            saved_body = await saved.json()
            api_contract.BudgetAccountSaved.model_validate(saved_body)
            self.assertEqual(saved_body["account"]["display"], account["display"])
            self.assertEqual(saved_body["account"]["observed_balance_currencies"], ["EUR", "PLN"])
            conflict = await self.post("account-selection", {"account_id": self.alice_id, "selected": True,
                                                              "expected_version": account["control_version"]})
            self.assertEqual(conflict.status, 409)
            current = await conflict.json()
            api_contract.BudgetConflict.model_validate(current)
            self.assertEqual(current["current"]["display"], account["display"])
            for account_id in (self.bob_id, "missing-display-account"):
                denied = await self.post("account-kind", {"account_id": account_id, "kind": "other", "expected_version": 1})
                self.assertEqual(denied.status, 404)
                self.assertEqual(await denied.json(), {"ok": False, "error": "budget_not_found"})
            other = await (await self.get("accounts", user="bob")).json()
            self.assertEqual(other["accounts"][0]["display"]["product"], "Bob private display")
            self.assertNotIn("Alice synthetic card", json.dumps(other))
        global_audit.assert_not_called()
        await asyncio.sleep(0)
        self.assertTrue(subscription.queue.empty())
        audits = [(row.field, row.old_value_json, row.new_value_json) for row in self.rows(db.BudgetAccountAudit)]
        self.assertNotIn("synthetic card", json.dumps(audits))
        self.assertNotIn("A123", json.dumps(audits))

    async def test_disabled_feature_authoff_json_backend_and_roles_fail_closed(self):
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "0"}), \
             mock.patch.object(api, "init_db") as initialize:
            response = await self.get("accounts")
            self.assertEqual(response.status, 404)
            initialize.assert_not_called()
            self.assert_private_headers(response)
        with mock.patch.object(web, "_AUTH_ENABLED", False), mock.patch.object(api, "init_db") as initialize:
            self.assertEqual((await self.get("accounts")).status, 401)
            initialize.assert_not_called()
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}), mock.patch.object(api, "init_db") as initialize:
            response = await self.get("accounts")
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["error"], "budget_requires_sqlite_users")
            initialize.assert_not_called()
        for user, status in (("disabled", 401), ("deleted", 401), ("viewer", 403), ("operator", 403)):
            response = await self.get("accounts", user=user)
            self.assertEqual(response.status, status)
            self.assert_private_headers(response)
        for headers in ({}, {"Cookie": "hestia_session=invalid"},
                        {"Cookie": "hestia_session=" + self.token(ttl=-1)}):
            response = await self.get("accounts", headers=headers)
            self.assertEqual(response.status, 401)
            self.assert_private_headers(response)

    async def test_authoritative_marker_is_required_before_view_construction(self):
        with mock.patch.object(api.store_sql, "is_users_db_authoritative", return_value=False), \
             mock.patch.object(api, "BudgetHTTPStore") as store:
            response = await self.get("accounts")
        self.assertEqual(response.status, 409)
        store.assert_not_called()

    async def test_owner_resource_ids_are_indistinguishable_from_unknown_ids(self):
        for account_id in (self.bob_id, "unknown-account"):
            for operation in ("transactions", "balances"):
                response = await self.get(operation, params={"account_id": account_id})
                self.assertEqual(response.status, 404)
                self.assertEqual(await response.json(), {"ok": False, "error": "budget_not_found"})
            for operation, fields in (("account-selection", {"selected": False}), ("account-kind", {"kind": "other"})):
                response = await self.post(operation, {"account_id": account_id, **fields})
                self.assertEqual(response.status, 404)
                self.assertNotIn(self.bob_id, await response.text())
        for connection_id in ("bob-bank", "unknown-bank"):
            self.assertEqual((await self.get("accounts", params={"connection_id": connection_id})).status, 404)
            self.assertEqual((await self.post("connection-state", {"connection_id": connection_id, "paused": True})).status, 404)
        bob = await self.get("accounts", user="bob")
        self.assertEqual([item["id"] for item in (await bob.json())["accounts"]], [self.bob_id])

    async def test_strict_query_fields_duplicates_bounds_dates_and_cursor_binding(self):
        invalid = [("connections", {"owner": "bob"}), ("status", {"anything": "x"}), ("limits", {"scope": "application"}),
                   ("accounts", [("connection_id", "bank"), ("connection_id", "bank")]),
                   ("accounts", {"connection_id": ""}), ("accounts", {"connection_id": "x" * 129}),
                   ("balances", {}), ("balances", {"account_id": self.alice_id, "owner": "bob"}),
                   ("transactions", {})]
        for value in ("0", "201", "true", "1.0", "-1", "10000"):
            invalid.append(("transactions", {"account_id": self.alice_id, "limit": value}))
        for key in ("date_from", "date_to"):
            for value in ("2026-02-30", "20261120", "2026-1-01"):
                invalid.append(("transactions", {"account_id": self.alice_id, key: value}))
        invalid.append(("transactions", {"account_id": self.alice_id, "date_from": "2026-12-01", "date_to": "2026-01-01"}))
        for operation, params in invalid:
            with self.subTest(operation=operation, params=params):
                self.assertEqual((await self.get(operation, params=params)).status, 400)
        response = await self.get("transactions", params={"account_id": self.alice_id, "limit": "1"})
        first = await response.json()
        self.assertEqual(len(first["transactions"]), 1)
        cursor = first["next_cursor"]
        self.assertIsInstance(cursor, str)
        second = await self.get("transactions", params={"account_id": self.alice_id, "limit": "1", "cursor": cursor})
        self.assertEqual(len((await second.json())["transactions"]), 1)
        for params, status in (({"account_id": self.bob_id, "limit": "1", "cursor": cursor}, 404),
                               ({"account_id": self.alice_id, "limit": "2", "cursor": cursor}, 400),
                               ({"account_id": self.alice_id, "limit": "1", "cursor": cursor + "broken"}, 400)):
            self.assertEqual((await self.get("transactions", params=params)).status, status)
        self.assertEqual((await self.get("transactions", user="bob", params={"account_id": self.bob_id,
                                                                             "limit": "1", "cursor": cursor})).status, 400)
        dated = await self.get("transactions", params={"account_id": self.alice_id, "date_from": "2026-11-01", "date_to": "2026-11-30"})
        self.assertEqual(len((await dated.json())["transactions"]), 1)
        self.assertEqual(len((await (await self.get("accounts", params={"connection_id": "bank"})).json())["accounts"]), 1)

    async def test_three_controls_versions_noops_and_private_web_audits(self):
        account = await self.account()
        before = len(self.rows(db.BudgetAccountAudit))
        with mock.patch.object(web, "_audit") as global_audit, mock.patch.object(runtime, "credentials") as credentials:
            selected = await self.post("account-selection", {"account_id": self.alice_id, "selected": False,
                                                              "expected_version": account["control_version"]})
            self.assertEqual(selected.status, 200, await selected.text())
            changed = await selected.json()
            api_contract.BudgetAccountSaved.model_validate(changed)
            self.assertTrue(changed["account"]["changed"])
            self.assertFalse(changed["account"]["selected"])
            version = changed["account"]["control_version"]
            repeated = await self.post("account-selection", {"account_id": self.alice_id, "selected": False, "expected_version": version})
            self.assertFalse((await repeated.json())["account"]["changed"])
            repeated = await self.post("account-selection", {"account_id": self.alice_id, "selected": False,
                                                              "expected_version": account["control_version"]})
            self.assertEqual(repeated.status, 200)
            self.assertFalse((await repeated.json())["account"]["changed"])
            stale = await self.post("account-selection", {"account_id": self.alice_id, "selected": True,
                                                           "expected_version": account["control_version"]})
            self.assertEqual(stale.status, 409)
            api_contract.BudgetConflict.model_validate(await stale.json())
            missing = await self.post("account-kind", {"account_id": self.alice_id, "kind": "unknown"})
            self.assertEqual(missing.status, 400)
            missing = await self.post("account-kind", {"account_id": self.alice_id, "kind": "credit_card"})
            self.assertEqual(missing.status, 409)
            confirmed = await self.post("account-kind", {"account_id": self.alice_id, "kind": "credit_card", "expected_version": version})
            self.assertEqual(confirmed.status, 200)
            self.assertFalse((await confirmed.json())["account"]["selected"])
            card_version = (await confirmed.json())["account"]["control_version"]
            for bad_body in (
                {"account_id": self.alice_id, "credit_limit": {"amount": "0", "currency": "PLN"}, "expected_version": card_version},
                {"account_id": self.alice_id, "credit_limit": {"amount": "10000000.01", "currency": "PLN"}, "expected_version": card_version},
                {"account_id": self.alice_id, "credit_limit": {"amount": "1000", "currency": "pln"}, "expected_version": card_version},
                {"account_id": self.alice_id, "credit_limit": {"amount": "1000", "currency": "EUR"}, "expected_version": card_version},
                {"account_id": self.alice_id, "credit_limit": "10000", "expected_version": card_version},
            ):
                bad_limit = await self.post("account-credit-limit", bad_body)
                self.assertEqual(bad_limit.status, 400)
            missing_ver = await self.post(
                "account-credit-limit",
                {"account_id": self.alice_id, "credit_limit": {"amount": "10000", "currency": "PLN"}},
            )
            self.assertEqual(missing_ver.status, 409)
            api_contract.BudgetConflict.model_validate(await missing_ver.json())
            saved_limit = await self.post("account-credit-limit",
                                          {"account_id": self.alice_id,
                                           "credit_limit": {"amount": "10000.00", "currency": "PLN"},
                                           "expected_version": card_version})
            self.assertEqual(saved_limit.status, 200)
            limit_payload = await saved_limit.json()
            api_contract.BudgetAccountSaved.model_validate(limit_payload)
            self.assertEqual(limit_payload["account"]["credit_limit"], {"amount": "10000", "currency": "PLN"})
            self.assertEqual((await self.account())["credit_limit"], {"amount": "10000", "currency": "PLN"})
            cleared_limit = await self.post("account-credit-limit",
                                            {"account_id": self.alice_id, "credit_limit": None,
                                             "expected_version": limit_payload["account"]["control_version"]})
            self.assertEqual(cleared_limit.status, 200)
            self.assertIsNone((await cleared_limit.json())["account"]["credit_limit"])
            connection = await self.connection()
            paused = await self.post("connection-state", {"connection_id": "bank", "paused": True,
                                                          "expected_version": connection["control_version"]})
            self.assertEqual(paused.status, 200)
            api_contract.BudgetConnectionSaved.model_validate(await paused.json())
        global_audit.assert_not_called()
        credentials.assert_not_called()
        audits = self.rows(db.BudgetAccountAudit)[before:]
        self.assertEqual(len(audits), 4)
        self.assertNotIn("10000", json.dumps([(row.field, row.old_value_json, row.new_value_json) for row in audits]))
        self.assertEqual({row.owner for row in audits}, {"alice"})
        self.assertEqual({row.actor for row in audits}, {"alice"})
        self.assertEqual({row.origin for row in audits}, {"web"})
        self.assertEqual(self.rows(db.BudgetConnectionAudit)[0].origin, "web")
        self.store.configure(self.config)
        self.assertTrue(next(row for row in self.rows(db.BudgetConnection) if row.id == "bank").paused)
        db.reset_engine_cache()
        self.assertTrue((await self.connection())["paused"])

    async def test_browser_mutations_reject_any_authorization_and_wrong_origin(self):
        account = await self.account()
        body = {"account_id": self.alice_id, "selected": False, "expected_version": account["control_version"]}
        for authorization in ("", "Basic placeholder", "Bearer " + self.token(), "Bearer " + self.token("bob")):
            headers = {**self.headers(origin=True), "Authorization": authorization}
            self.assertEqual((await self.post("account-selection", body, headers=headers)).status, 403)
        self.assertEqual((await self.post("account-selection", body, headers=self.headers(bearer=True, origin=True))).status, 403)
        for origin in (None, "null", "https://evil.example", ORIGIN + "/", "http://hestia.example.test"):
            headers = self.headers()
            if origin is not None:
                headers["Origin"] = origin
            response = await self.post("account-selection", body, headers=headers)
            self.assertEqual(response.status, 403)
            self.assert_private_headers(response)
        headers = [("Cookie", "hestia_session=" + self.token()), ("Origin", ORIGIN), ("Origin", ORIGIN)]
        self.assertEqual((await self.post("account-selection", body, headers=headers)).status, 403)
        self.assertTrue((await self.account())["selected"])

    async def test_invalid_public_origin_is_safe_unavailable_and_never_trusts_forwarded_host(self):
        account = await self.account()
        body = {"account_id": self.alice_id, "selected": False, "expected_version": account["control_version"]}
        for origin in ("", "http://host", "https://", "https://user:PRIVATE@host", "https://host/path",
                       "https://host?query", "https://host#fragment", "https://host:bad", "https://host:0",
                       "https://host:65536", "https://höst", "https://host\\PRIVATE", "https://host\t"):
            with self.subTest(origin=origin), mock.patch.dict(os.environ, {"HESTIA_BUDGET_PUBLIC_ORIGIN": origin}):
                response = await self.post("account-selection", body)
                self.assertEqual(response.status, 503)
                self.assertNotIn("PRIVATE", await response.text())
        headers = {**self.headers(origin=True), "Host": "evil.example", "X-Forwarded-Host": "evil.example",
                   "X-Forwarded-Proto": "http"}
        self.assertEqual((await self.post("account-selection", body, headers=headers)).status, 200)

    async def test_body_guards_duplicate_fields_and_strict_models_do_not_write(self):
        account = await self.account()
        body = {"account_id": self.alice_id, "selected": False, "expected_version": account["control_version"]}
        for value in ([], None, {}, {**body, "owner": "bob"}, {**body, "actor": "bob"}, {**body, "origin": "operator"},
                      {**body, "account_id": ""}, {**body, "account_id": "x" * 129}, {**body, "selected": 1}):
            self.assertEqual((await self.post("account-selection", value)).status, 400)
        for version in (None, True, 0, -1, "1", 1.0):
            self.assertEqual((await self.post("account-selection", {**body, "expected_version": version})).status, 400)
        headers = {**self.headers(origin=True), "Content-Type": "application/json"}
        for data in ('{"broken"', '{"account_id":"a","account_id":"b","selected":true,"expected_version":1}'):
            response = await self.client.post(api.PREFIX + "/account-selection", data=data, headers=headers)
            self.assertEqual(response.status, 400)
            self.assert_private_headers(response)
        response = await self.client.post(api.PREFIX + "/account-selection", data="{}", headers=self.headers(origin=True))
        self.assertEqual(response.status, 415)
        async def chunked():
            yield b"{}"
        response = await self.client.post(api.PREFIX + "/account-selection", data=chunked(), headers=headers)
        self.assertEqual(response.status, 411)
        response = await self.post("account-selection", {**body, "account_id": "x" * 17000})
        self.assertEqual(response.status, 413)
        self.assertTrue((await self.account())["selected"])

    async def test_permission_and_backend_changes_after_middleware_are_rechecked_in_store(self):
        original = api._store
        for field, value, status in (("disabled", True, 401), ("role", "operator", 403)):
            def changed(secret):
                with self.sessions.begin() as session:
                    setattr(session.get(db.User, "alice"), field, value)
                return original(secret)
            with mock.patch.object(api, "_store", side_effect=changed):
                response = await self.get("accounts")
            self.assertEqual(response.status, status)
            with self.sessions.begin() as session:
                row = session.get(db.User, "alice")
                row.disabled, row.role = False, "admin"
        account = await self.account()
        method = BudgetHTTPStore.set_account_selected
        def disabled_during_write(store, *args, **kwargs):
            with self.sessions.begin() as session:
                session.get(db.User, "alice").disabled = True
            return method(store, *args, **kwargs)
        with mock.patch.object(BudgetHTTPStore, "set_account_selected", new=disabled_during_write):
            response = await self.post("account-selection", {"account_id": self.alice_id, "selected": False,
                                                              "expected_version": account["control_version"]})
        self.assertEqual(response.status, 401)
        self.assertTrue(next(row.enabled for row in self.rows(db.BudgetAccount) if row.id == self.alice_id))

    async def test_cookie_expiry_between_body_read_and_write_does_not_mutate(self):
        account = await self.account()
        with mock.patch.object(
            api,
            "_now",
            side_effect=[web._now(), web._now() + auth.SESSION_TTL + 10],
        ):
            response = await self.post(
                "account-selection",
                {"account_id": self.alice_id, "selected": False, "expected_version": account["control_version"]},
            )
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"], "budget_not_signed_in")
        self.assertTrue((await self.account())["selected"])

    async def test_unconfigured_owned_account_cannot_be_selected_and_can_be_deselected(self):
        with self.sessions.begin() as session:
            session.get(db.BudgetAccount, self.alice_id).configured = False
        account = await self.account()
        body = {"account_id": self.alice_id, "selected": True, "expected_version": account["control_version"]}
        response = await self.post("account-selection", body)
        self.assertEqual(response.status, 400)
        self.assertEqual(await response.json(), {"ok": False, "error": "budget_invalid_request"})
        response = await self.post("account-selection", {**body, "selected": False})
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())["account"]["selected"])

    async def test_largest_unicode_and_control_reference_cursors_roundtrip_through_real_http(self):
        for reference in ("🧾" * 1024, "\x00" * 1024):
            with self.subTest(reference_kind=ord(reference[0])):
                with self.sessions.begin() as session:
                    for row in session.scalars(select(db.BudgetTransaction).where(db.BudgetTransaction.account_id == self.alice_id)):
                        session.delete(row)
                self.store.commit_snapshot("bank", snapshot(transaction(reference), transaction("last", booking_date="2026-10-01"),
                                                            account_hash=PRIVATE_A, valid_until=web._now() + 31536000))
                first = await self.get("transactions", params={"account_id": self.alice_id, "limit": "1"})
                self.assertEqual(first.status, 200)
                first_body = await first.json()
                self.assertEqual(first_body["transactions"][0]["entry_reference"], reference)
                cursor = first_body["next_cursor"]
                self.assertLessEqual(len(cursor), 8192)
                self.assertNotIn(reference, cursor)
                response = await self.get("transactions", params={"account_id": self.alice_id, "limit": "1", "cursor": cursor})
                self.assertEqual(response.status, 200)
                body = await response.json()
                self.assertEqual([row["entry_reference"] for row in body["transactions"]], ["last"])
                self.assertIsNone(body["next_cursor"])

    async def test_internal_error_logs_only_fixed_operation_and_exception_class(self):
        sentinel = "PRIVATE-provider-url-session-secret-and-account"
        with mock.patch.object(api, "_store", side_effect=RuntimeError(sentinel)), \
             self.assertLogs(api.log, level="ERROR") as captured:
            response = await self.get("accounts", params={"connection_id": sentinel})
        self.assertEqual(response.status, 500)
        self.assertEqual(await response.json(), {"ok": False, "error": "budget_internal_error"})
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.getMessage(), "Budget API operation=accounts exception=RuntimeError")
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)
        self.assertNotIn(sentinel, captured.output[0])

    async def test_safe_failure_mapping_headers_and_future_routes_remain_absent(self):
        for error, status in ((BudgetConfigError("PRIVATE-key/path"), 503), (RuntimeError("PRIVATE-provider-url"), 500)):
            with mock.patch.object(api, "_store", side_effect=error):
                response = await self.get("accounts")
            self.assertEqual(response.status, status)
            self.assert_private_headers(response)
            self.assertNotIn("PRIVATE", await response.text())
        for method, operation in (("HEAD", "accounts"), ("POST", "enable-banking/start"), ("HEAD", "jobs")):
            response = await self.client.request(method, api.PREFIX + "/" + operation, headers=self.headers(origin=True))
            self.assertIn(response.status, (404, 405))
            self.assert_private_headers(response)
        response = await self.client.get(api.PREFIX + "/enable-banking/callback", allow_redirects=False)
        self.assertEqual(response.status, 303)
        self.assertEqual(await response.json(), {"ok": False, "error": "budget_consent_unavailable"})
        self.assertEqual(response.headers["Location"], "/budget/connect/complete")
        self.assert_private_headers(response)
        response = await self.client.get(api.PREFIX, headers=self.headers())
        self.assertEqual(response.status, 404)
        self.assert_private_headers(response)
        response = await self.client.get("/api/whoami", headers=self.headers())
        self.assertNotIn("Referrer-Policy", response.headers)

    async def test_cancellation_waits_for_real_database_write_or_failure_to_settle(self):
        loop = asyncio.get_running_loop()
        previous_handler, unhandled = loop.get_exception_handler(), []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        self.addCleanup(loop.set_exception_handler, previous_handler)
        audit_before = len(self.rows(db.BudgetAccountAudit))
        for failing in (False, True):
            started, release, finished = threading.Event(), threading.Event(), threading.Event()
            account = await self.account()
            def operation():
                started.set()
                release.wait(2)
                try:
                    if failing:
                        raise RuntimeError("Synthetic write failure")
                    return api._write("alice", "account-selection", {"account_id": self.alice_id, "selected": False,
                                                                      "expected_version": account["control_version"]}, SECRET)
                finally:
                    finished.set()
            task = asyncio.create_task(api._settled_thread(operation))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
        self.assertFalse((await self.account())["selected"])
        self.assertEqual(len(self.rows(db.BudgetAccountAudit)), audit_before + 1)
        gc.collect()
        await asyncio.sleep(0)
        self.assertEqual(unhandled, [])


class BudgetGuardTests(unittest.TestCase):
    def test_handler_principal_and_secret_guards_do_not_rely_on_middleware(self):
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_PERSIST": "sqlite"}):
            for principal in (None, "", 1):
                with self.assertRaises(api._Rejected) as caught:
                    api._principal({"hestia_user": principal, "hestia_role": "admin"}, lambda: True)
                self.assertEqual(caught.exception.status, 401)
            self.assertEqual(
                api._principal({"hestia_user": "alice", "hestia_role": "viewer"}, lambda: True),
                "alice",
            )
            request = SimpleNamespace(
                headers=MultiDict({"X-Hestia-Expected-Actor": "previous-owner"}),
                get=lambda key: "alice" if key == "hestia_user" else None,
            )
            with self.assertRaises(api._Rejected) as caught:
                api._principal(request, lambda: True)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "budget_actor_changed"))
        for secret in (None, "PRIVATE", b""):
            with self.assertRaises(BudgetConfigError) as caught:
                api._secret(lambda: secret)
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_expected_actor_rejects_duplicate_malformed_and_noncanonical_headers(self):
        request = SimpleNamespace(headers=MultiDict())
        for values in (
            ["one", "two"], [""], ["A" * 257], ["Ági"], ["AB"], ["/w=="],
        ):
            with self.subTest(values=values):
                request.headers = MultiDict()
                for value in values:
                    request.headers.add("X-Hestia-Expected-Actor", value)
                self.assertFalse(api.expected_actor_matches(request, "alice"))

    def test_config_errors_stay_unavailable_without_a_forecast_conflict_code(self):
        consent = api._failure(ConsentFilesError(), "consent_start")
        plain = api._failure(BudgetConfigError("PRIVATE-key/path"), "accounts")
        self.assertEqual(consent.status, 503)
        self.assertEqual(json.loads(consent.body), {"ok": False, "error": "budget_unavailable"})
        self.assertEqual(json.loads(plain.body), {"ok": False, "error": "budget_unavailable"})
        self.assertNotIn(b"budget_forecast_config_conflict", plain.body)
        self.assertNotIn(b"PRIVATE", plain.body)
        self.assertNotIn(b"consent_files", consent.body)
        incompat = api._failure(authz.PolicyPhaseUnsupported("catalog_custom"), "accounts")
        self.assertEqual((incompat.status, json.loads(incompat.body)), (503, {"ok": False, "error": "authorization_bridge_incompatible"}))

    def test_query_parser_rejects_cursor_over_transport_limit_without_expanding_http_framing(self):
        request = SimpleNamespace(query=MultiDict({"account_id": "local", "cursor": "x" * 8193}))
        with self.assertRaises(api.BudgetQueryError):
            api._query(request, "transactions")

    def test_validate_browser_context_rejects_missing_tampered_and_wrong_purpose(self):
        with self.assertRaises(api._Rejected) as exc:
            api.validate_browser_context(SimpleNamespace(headers={}, cookies={}, get=lambda _k: None), actor="alice", now=10.0)
        self.assertEqual((exc.exception.status, exc.exception.code), (401, "budget_not_signed_in"))

        bad_ctx = web.ValidatedSessionContext(
            username="alice", role="admin", carrier="cookie", version=2, purpose="native",
            generation=0, expiry=100, session_phase="v2", policy_phase="legacy", raw_token="tok",
        )
        req_native = SimpleNamespace(headers={}, cookies={"hestia_session": "tok"}, get=lambda _k: bad_ctx)
        with self.assertRaises(api._Rejected) as exc2:
            api.validate_browser_context(req_native, actor="alice", now=10.0)
        self.assertEqual((exc2.exception.status, exc2.exception.code), (401, "budget_not_signed_in"))

        bad_v2_in_legacy = web.ValidatedSessionContext(
            username="alice", role="admin", carrier="cookie", version=2, purpose="browser",
            generation=0, expiry=100, session_phase="legacy", policy_phase="legacy", raw_token="tok",
        )
        req_legacy = SimpleNamespace(headers={}, cookies={"hestia_session": "tok"}, get=lambda _k: bad_v2_in_legacy)
        with self.assertRaises(api._Rejected) as exc3:
            api.validate_browser_context(req_legacy, actor="alice", now=10.0)
        self.assertEqual((exc3.exception.status, exc3.exception.code), (401, "budget_not_signed_in"))

        with self.assertRaises(api._Rejected) as exc4:
            api.validate_browser_context(SimpleNamespace(headers={"Authorization": "Bearer x"}), actor="alice", now=10.0)
        self.assertEqual((exc4.exception.status, exc4.exception.code), (403, "budget_browser_session_required"))


class BudgetAuthorizationBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "bridge_budget.db"
        self.env = mock.patch.dict(
            os.environ,
            {
                "HESTIA_DB": str(self.path),
                "HESTIA_PERSIST": "sqlite",
                "HESTIA_BUDGET_HTTP_ENABLED": "1",
                "HESTIA_BUDGET_ANALYTICS_ENABLED": "1",
                "HESTIA_BUDGET_RECEIPTS_ENABLED": "1",
                "HESTIA_BUDGET_PUBLIC_ORIGIN": ORIGIN,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        for patch in (
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", SECRET),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(engine, {"alice": "h", "unprov": "h"})
        store_sql.provision_budget_owner("alice", path=self.path)
        store_sql.activate_sessions_v2(path=self.path)
        self.config = configuration(accounts=(PRIVATE_A,), owner="alice")
        self.store = BudgetStore(self.sessions)
        prepare_selected(self.store, self.config)
        self.alice_acc = self.store.accounts("alice")[0]["id"]
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def _set_phase(self, value: str) -> None:
        with self.sessions.begin() as s:
            row = s.get(db.AppMeta, "authz_policy_phase")
            if row is None:
                s.add(db.AppMeta(key="authz_policy_phase", value=value))
            else:
                row.value = value

    async def test_active_v2_budget_routes_enforce_browser_purpose_and_compat_owner_marker(self):
        v1 = auth.make_session("alice", now=web._now(), secret=SECRET)
        v2_browser = auth.make_session("alice", now=web._now(), secret=SECRET, generation=0, purpose="browser")
        v2_native = auth.make_session("alice", now=web._now(), secret=SECRET, generation=0, purpose="native")
        unprov_b = auth.make_session("unprov", now=web._now(), secret=SECRET, generation=0, purpose="browser")

        acc = (await (await self.client.get(
            api.PREFIX + "/accounts", headers={"Cookie": f"hestia_session={v2_browser}"}
        )).json())["accounts"][0]

        # v1 cookie on protected Budget POST (and analytics POST) -> 401 budget_browser_session_upgrade_required.
        for url, payload in (
            (api.PREFIX + "/account-selection", {"account_id": self.alice_acc, "selected": False, "expected_version": acc["control_version"]}),
            (api.PREFIX + "/analytics/transactions-by-keys", {"keys": []}),
        ):
            r_v1 = await self.client.post(url, json=payload, headers={"Cookie": f"hestia_session={v1}", "Origin": ORIGIN})
            self.assertEqual(r_v1.status, 401)
            self.assertEqual((await r_v1.json())["error"], "budget_browser_session_upgrade_required")
            r_nat = await self.client.post(url, json=payload, headers={"Authorization": f"Bearer {v2_native}"})
            self.assertEqual(r_nat.status, 403)
            self.assertEqual((await r_nat.json())["error"], "budget_browser_session_required")

        ok = await self.client.post(
            api.PREFIX + "/account-selection",
            json={"account_id": self.alice_acc, "selected": False, "expected_version": acc["control_version"]},
            headers={"Cookie": f"hestia_session={v2_browser}", "Origin": ORIGIN},
        )
        self.assertEqual(ok.status, 200)

        # In catalog_compat, an unprovisioned admin is denied owner checks across HTTP & BudgetStore.
        self._set_phase("catalog_compat")
        r_unprov = await self.client.get(api.PREFIX + "/accounts", headers={"Cookie": f"hestia_session={unprov_b}"})
        self.assertEqual(r_unprov.status, 403)
        with self.assertRaises(BudgetStoreError):
            self.store.accounts("unprov")

    async def test_expected_actor_mismatch_precedes_owner_lookup(self):
        token = auth.make_session(
            "alice", now=web._now(), secret=SECRET, generation=0, purpose="browser",
        )
        with mock.patch.object(api, "_store", side_effect=AssertionError("owner lookup")) as store:
            response = await self.client.get(
                api.PREFIX + "/accounts",
                headers={"Cookie": f"hestia_session={token}", "X-Hestia-Expected-Actor": "someone-else"},
            )
        self.assertEqual(response.status, 409)
        self.assertEqual(await response.json(), {"ok": False, "error": "budget_actor_changed"})
        store.assert_not_called()

    def test_unknown_policy_phase_blocks_stores_jobs_runtime_and_resolve_mode(self):
        from hestia.__main__ import resolve_mode
        from hestia.budget import analytics_api, household_api, jobs, job_store, receipt_api

        self._set_phase("future_phase")
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            api._store(SECRET)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            analytics_api._store(SECRET)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            receipt_api._store(SECRET)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            receipt_api.retain_expired(self.path)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            household_api._reader(SECRET)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            self.store.status()
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            self.store.accounts("alice")
        jstore = job_store.BudgetJobStore(self.sessions)
        with self.sessions() as session:
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                jstore._owner(session, "alice")
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                jstore.users_authoritative(session)
        from hestia.budget import __main__ as budget_main
        import io
        cfg_file = self.path.parent / "budget.json"
        cfg_file.write_text(json.dumps({
            "application_id": "app-1",
            "connections": [{"id": "bank", "owner": "alice", "bank_name": "B", "country": "PL", "accounts": [PRIVATE_A]}],
        }), encoding="utf-8")
        with mock.patch.dict(os.environ, {jobs.JOBS_FLAG: "1"}), \
             mock.patch.object(jobs.BudgetJobRunner, "_config", return_value=self.config):
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                jobs.build_service(auth_enabled=lambda: True, path=self.path)
        with mock.patch("hestia.budget.runtime.credentials", side_effect=AssertionError("loaded credentials")):
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                runtime.run_once(self.config, path=self.path)
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            resolve_mode(None, self.path.parent / "reg.json")
        with mock.patch.object(budget_main, "load_config", return_value=self.config), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as buf:
            rc = budget_main.main(["--config", str(cfg_file), "--db", str(self.path), "status", "--stored-only"])
        self.assertEqual(rc, 1)
        self.assertIn("authorization_bridge_incompatible", buf.getvalue())
