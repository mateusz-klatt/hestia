"""Calendar HTTP surface with real SQLite roles, revision checks and durable jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from hestia import api_contract, auth, db, store_sql, web
from hestia.calendar_sync import api
from hestia.calendar_sync.config import ConfigError
from hestia.calendar_sync.store import CalendarConflict


def content(summary="Family"):
    return {"summary": summary, "start": {"date": "2026-10-21"}, "end": {"date": "2026-10-22"}}


class CalendarApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config_path = self.path / "calendars.json"
        self.config = {"version": 1, "accounts": {"google": {"provider": "google", "client_id_env": "G_ID",
                       "client_secret_env": "G_SECRET", "refresh_token_env": "G_REFRESH"}},
                       "groups": [{"id": "family", "calendars": [{"id": "family-google", "account": "google", "calendar_id": "private@example.test"}]}]}
        self.config_path.write_text(json.dumps(self.config))
        patches = [mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path / "hestia.db"),
                   "HESTIA_PERSIST": "sqlite", "HESTIA_CALENDAR_CONFIG": str(self.config_path),
                   "G_SECRET": "never-leak-secret", "G_REFRESH": "never-leak-token"}),
                   mock.patch.object(web, "_AUTH_ENABLED", True),
                   mock.patch.object(web, "_SESSION_SECRET", b"calendar-api-test")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, sessions = db.init_db()
        store_sql.cutover_users(engine, {"admin": "unused-password-hash"})
        with sessions.begin() as session:
            session.add(db.User(username="operator", password_hash="unused", role="operator", disabled=False))
            session.add(db.User(username="viewer", password_hash="unused", role="viewer", disabled=False))
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def headers(self, role="operator"):
        return {"Authorization": "Bearer " + auth.make_session(role, now=web._now(), secret=b"calendar-api-test")}

    async def post(self, body, *, deleting=False, role="operator"):
        return await self.client.post(api.DELETE_PATH if deleting else api.EVENTS_PATH,
                                      json=body, headers=self.headers(role))

    async def test_all_crud_and_revision_conflicts_are_persisted_and_serialized(self):
        response = await self.post({"group": "family", "payload": content()})
        self.assertEqual(response.status, 200)
        created = await response.json()
        api_contract.CalendarSaved.model_validate(created)
        event = created["event"]
        request = {"group": "family", "id": event["id"], "revision": event["revision"], "payload": content("Updated")}
        api_contract.CalendarUpdateRequest.model_validate(request)
        response = await self.post(request, role="admin")
        self.assertEqual(response.status, 200)
        edited = (await response.json())["event"]
        self.assertEqual(edited["revision"], 2)
        self.assertEqual((await self.post(request)).status, 409)
        response = await self.client.get(api.PATH, headers=self.headers("viewer"))
        state = await response.json()
        api_contract.Calendars.model_validate(state)
        self.assertEqual(state["groups"], [{"id": "family"}])
        self.assertEqual(state["events"], [edited])
        self.assertEqual(state["status"]["pending"][0]["revision"], 2)
        delete = {"group": "family", "id": event["id"], "revision": 1}
        self.assertEqual((await self.post(delete, deleting=True)).status, 409)
        delete["revision"] = 2
        api_contract.CalendarDeleteRequest.model_validate(delete)
        response = await self.post(delete, deleting=True)
        self.assertEqual(response.status, 200)
        tombstone = (await response.json())["event"]
        self.assertTrue(tombstone["deleted"])
        self.assertEqual(tombstone["revision"], 3)
        self.assertTrue(api._store().list_events("family", include_deleted=True)[0]["deleted"])
        response = await self.client.get(api.PATH + "?group=family", headers=self.headers())
        self.assertEqual((await response.json())["events"], [])
        request["revision"] = 3
        self.assertEqual((await self.post(request)).status, 409)

    async def test_real_auth_roles_allow_read_and_gate_both_writes(self):
        self.assertEqual((await self.client.get(api.PATH)).status, 401)
        self.assertEqual((await self.client.post(api.EVENTS_PATH, json={})).status, 401)
        for role in ("viewer", "operator", "admin"):
            self.assertEqual((await self.client.get(api.PATH, headers=self.headers(role))).status, 200)
        for deleting in (False, True):
            self.assertEqual((await self.post({"group": "family", "payload": content()}, deleting=deleting, role="viewer")).status, 403)

    async def test_invalid_request_fields_and_revisions(self):
        invalid = [[], {"group": "family", "payload": content(), "unexpected": True},
                   {"payload": content()}, {"group": 1}, {"group": "family", "id": ""},
                   {"group": "family", "id": None}, {"group": "family", "id": 1},
                   {"group": "family", "id": "event"}, {"group": "family", "id": "event", "revision": True},
                   {"group": "family", "id": "event", "revision": 0},
                   {"group": "family", "revision": 1, "payload": content()},
                   {"group": "family", "payload": None}, {"group": "unknown", "payload": content()}]
        for body in invalid:
            with self.subTest(body=body):
                response = await self.post(body)
                self.assertEqual(response.status, 400)
                self.assertEqual(await response.json(), {"error": "invalid_calendar_event"})
        self.assertEqual((await self.post({"group": "family"}, deleting=True)).status, 400)
        self.assertEqual((await self.post({"group": "family", "id": "event", "revision": 1, "payload": {}}, deleting=True)).status, 400)
        self.assertEqual((await self.client.get(api.PATH + "?group=unknown", headers=self.headers())).status, 400)

    async def test_json_body_guards(self):
        response = await self.client.post(api.EVENTS_PATH, data="{}", headers=self.headers())
        self.assertEqual(response.status, 415)
        response = await self.client.post(api.EVENTS_PATH, data="{broken", headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response = await self.post({"group": "family", "payload": content("x" * 70000)})
        self.assertEqual(response.status, 413)

    async def test_disabled_empty_and_invalid_configuration_without_secret_disclosure(self):
        with mock.patch.object(api, "load_config", return_value=None):
            response = await self.client.get(api.PATH, headers=self.headers())
            self.assertEqual(await response.json(), {"enabled": False, "groups": [], "events": [], "status": {"endpoints": [], "pending": []}})
            self.assertEqual((await self.post({"group": "family", "payload": content()})).status, 503)
        empty = {**self.config, "groups": []}
        self.config_path.write_text(json.dumps(empty))
        response = await self.client.get(api.PATH, headers=self.headers())
        self.assertEqual((await response.json())["events"], [])
        for exception in (ConfigError("never-leak-secret"), CalendarConflict("never-leak-token")):
            with mock.patch.object(api, "_store", side_effect=exception):
                response = await self.client.get(api.PATH, headers=self.headers())
                self.assertEqual(response.status, 503)
                self.assertNotIn("never-leak", await response.text())
        with mock.patch.object(api, "_write", side_effect=TypeError("never-leak-token")):
            response = await self.post({"group": "family", "payload": content()})
            self.assertEqual(response.status, 400)
            self.assertNotIn("never-leak", await response.text())

    async def test_config_retirement_filters_endpoints_pending_and_private_identifiers(self):
        response = await self.post({"group": "family", "payload": content()})
        self.assertEqual(response.status, 200)
        new = {**self.config, "groups": [{"id": "new", "calendars": [{"id": "new-google", "account": "google", "calendar_id": "new-private@example.test"}]}]}
        self.config_path.write_text(json.dumps(new))
        response = await self.client.get(api.PATH, headers=self.headers())
        text = await response.text()
        state = json.loads(text)
        self.assertEqual([row["id"] for row in state["status"]["endpoints"]], ["new-google"])
        self.assertEqual(state["status"]["pending"], [])
        self.assertNotIn("private@example.test", text)
        self.assertNotIn("never-leak", text)
        self.assertNotIn("client_secret", text)


class CalendarApiContractTests(unittest.TestCase):
    def test_contract_requests_and_optimistic_revision_shape(self):
        api_contract.CalendarCreateRequest.model_validate({"group": "family", "payload": content()})
        for model in (api_contract.CalendarUpdateRequest, api_contract.CalendarDeleteRequest):
            for revision in (None, True, 0, "1"):
                body = {"group": "family", "id": "event", "revision": revision}
                if model is api_contract.CalendarUpdateRequest:
                    body["payload"] = content()
                with self.assertRaises(ValidationError):
                    model.model_validate(body)
        schemas = api_contract.build_openapi()["components"]["schemas"]
        self.assertIn("CalendarPayload", schemas)
        self.assertEqual(set(schemas["CalendarUpdateRequest"]["required"]), {"group", "id", "revision", "payload"})

    def test_parallel_web_and_runtime_db_initialization_has_one_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "parallel.db"
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _: db.init_db(path), range(4)))
                self.assertTrue(all(result is results[0] for result in results))
            finally:
                db.reset_engine_cache()
