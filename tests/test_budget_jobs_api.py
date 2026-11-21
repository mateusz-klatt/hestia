"""HTTP refresh, job reads and background opt-in through the real aiohttp app (synthetic data)."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from hestia import api_contract, auth, db, store_sql, web
from hestia.budget import api, jobs, runtime
from tests.test_budget_store import configuration, prepare_selected
from hestia.budget.job_store import BudgetJobStore

SECRET = b"synthetic-budget-jobs-session-secret"
ORIGIN = "https://hestia.example.test"
KEY = "synthetic-http-refresh-0001"


class FakeService:
    def __init__(self, *, accepting=True, poller=False):
        self.accepting, self.poller = accepting, poller
        self.wakes = 0

    def start(self):
        pass

    async def stop(self):
        self.accepting = False

    def wake(self):
        self.wakes += 1


class BudgetJobsAPITests(unittest.IsolatedAsyncioTestCase):
    service = FakeService()

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private.db"
        type(self).service = FakeService()
        for patch in (
            mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path), "HESTIA_PERSIST": "sqlite",
                                         "HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_BUDGET_PUBLIC_ORIGIN": ORIGIN}),
            mock.patch.object(web, "_AUTH_ENABLED", True), mock.patch.object(web, "_SESSION_SECRET", SECRET),
            mock.patch.object(auth, "load_users", return_value={"alice": "synthetic-unused-password"}),
            mock.patch.object(jobs, "build_service", side_effect=lambda **_: self.service),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, self.sessions = db.init_db()
        store_sql.cutover_users(engine, {"alice": "unused", "bob": "unused"})
        with self.sessions.begin() as session:
            session.add(db.User(username="viewer", password_hash="unused", role="viewer"))
        base = configuration(accounts=("PRIVATE-ALICE-HASH",), owner="alice")
        other = replace(base.connections[0], id="bob-bank", owner="bob", accounts=("PRIVATE-BOB-HASH",))
        self.config = replace(base, connections=(*base.connections, other))
        self.store = BudgetJobStore(self.sessions)
        prepare_selected(self.store, self.config)
        self.alice_account = next(row.id for row in self.rows(db.BudgetAccount) if row.owner == "alice")
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def rows(self, model):
        with self.sessions() as session:
            return list(session.scalars(select(model)))

    def headers(self, user="alice", *, key=KEY, origin=True, bearer=False):
        token = auth.make_session(user, now=web._now(), secret=SECRET)
        headers = {"Authorization": "Bearer " + token} if bearer else {"Cookie": "hestia_session=" + token}
        if origin:
            headers["Origin"] = ORIGIN
        if key is not None:
            headers["Idempotency-Key"] = key
        headers["Content-Type"] = "application/json"
        return headers

    async def version(self):
        response = await self.client.get(api.PREFIX + "/connections", headers=self.headers(key=None, origin=False))
        return (await response.json())["connections"][0]["control_version"]

    async def refresh(self, body=None, headers=None, **changes):
        body = {"connection_id": "bank", "expected_version": await self.version(), **changes} if body is None else body
        return await self.client.post(api.PREFIX + "/refresh", data=json.dumps(body),
                                      headers=self.headers() if headers is None else headers)

    async def test_refresh_accepts_replays_and_wakes_only_for_new_jobs(self):
        with mock.patch.object(runtime, "credentials") as credentials, \
                mock.patch("urllib.request.OpenerDirector.open") as http:
            response = await self.refresh()
            self.assertEqual(response.status, 202, await response.text())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            body = api_contract.BudgetRefreshAccepted.model_validate(await response.json())
            self.assertTrue(body.job.created)
            self.assertEqual((body.job.status, body.job.source), ("queued", "manual"))
            replay = await self.refresh(expected_version=999)
            self.assertEqual(replay.status, 202)
            replayed = (await replay.json())["job"]
            self.assertEqual((replayed["id"], replayed["created"]), (body.job.id, False))
            coalesced = await self.refresh(headers=self.headers(key="synthetic-http-refresh-0002"))
            self.assertEqual((await coalesced.json())["job"]["id"], body.job.id)
        self.assertEqual(self.service.wakes, 1)
        credentials.assert_not_called()
        http.assert_not_called()
        listed = await self.client.get(api.PREFIX + "/jobs", headers=self.headers(key=None, origin=False))
        jobs_body = api_contract.BudgetJobs.model_validate(await listed.json())
        self.assertEqual([job.id for job in jobs_body.jobs], [body.job.id])
        filtered = await self.client.get(api.PREFIX + "/jobs", params={"connection_id": "bank"},
                                         headers=self.headers(key=None, origin=False))
        self.assertEqual(len((await filtered.json())["jobs"]), 1)
        foreign = await self.client.get(api.PREFIX + "/jobs", params={"connection_id": "bob-bank"},
                                        headers=self.headers(key=None, origin=False))
        self.assertEqual(foreign.status, 404)
        bob = await self.client.get(api.PREFIX + "/jobs", headers=self.headers("bob", key=None, origin=False))
        self.assertEqual((await bob.json())["jobs"], [])

    async def test_refresh_conflicts_and_fixed_refusals(self):
        first = await self.refresh(operation="balances")
        self.assertEqual(first.status, 202)
        cases = (
            ({"headers": self.headers(), "operation": "transactions"}, 409, "budget_idempotency_conflict"),
            ({"headers": self.headers(key="synthetic-http-refresh-0002"), "operation": "transactions"},
             409, "budget_refresh_in_progress"),
            ({"headers": self.headers(key="synthetic-http-refresh-0003"), "expected_version": 99},
             409, "budget_version_conflict"),
            ({"headers": self.headers(key="synthetic-http-refresh-0004"), "account_id": "missing"},
             404, "budget_not_found"),
            ({"headers": self.headers(key="synthetic-http-refresh-0005"), "operation": "payments"},
             400, "budget_invalid_request"),
        )
        for changes, status, code in cases:
            with self.subTest(code=code):
                response = await self.refresh(**changes)
                self.assertEqual(response.status, status)
                self.assertEqual((await response.json())["error"], code)
        conflict = await self.refresh(headers=self.headers(key="synthetic-http-refresh-0006"), expected_version=99)
        api_contract.BudgetConflict.model_validate(await conflict.json())
        missing_version = await self.refresh(body={"connection_id": "bank"},
                                             headers=self.headers(key="synthetic-http-refresh-0007"))
        self.assertEqual(missing_version.status, 409)
        self.store.set_connection_paused("alice", "bank", True, actor="alice", origin="web",
                                         expected_version=await self.version())
        with self.sessions.begin() as session:
            for job in session.scalars(select(db.BudgetJob)):
                job.status = "deferred"
        paused = await self.refresh(headers=self.headers(key="synthetic-http-refresh-0008"))
        self.assertEqual(((await paused.json())["error"], paused.status), ("budget_connection_paused", 409))

    async def test_header_browser_and_executor_gates_run_before_any_write(self):
        version = await self.version()
        body = {"connection_id": "bank", "expected_version": version}
        duplicate = [(name, value) for name, value in self.headers().items()] + [("Idempotency-Key", KEY)]
        for headers, status, code in (
                (self.headers(key=None), 400, "budget_invalid_request"),
                (self.headers(key="short"), 400, "budget_invalid_request"),
                (self.headers(key="bad key with spaces!!"), 400, "budget_invalid_request"),
                (duplicate, 400, "budget_invalid_request"),
                (self.headers(origin=False), 403, "budget_origin_required"),
                ({**self.headers(), "Authorization": self.headers(bearer=True)["Authorization"]},
                 403, "budget_browser_session_required")):
            with self.subTest(status=status, code=code):
                response = await self.client.post(api.PREFIX + "/refresh", data=json.dumps(body), headers=headers)
                self.assertEqual(response.status, status)
                self.assertEqual((await response.json())["error"], code)
        viewer = await self.client.post(api.PREFIX + "/refresh", data=json.dumps(body), headers=self.headers("viewer"))
        self.assertEqual(viewer.status, 403)
        for service in (None, FakeService(accepting=False)):
            with self.subTest(service=service), mock.patch.object(self.client.app[api.JOBS_KEY], "service", service):
                response = await self.refresh()
                self.assertEqual((response.status, (await response.json())["error"]), (503, "budget_refresh_unavailable"))
        self.assertEqual(self.rows(db.BudgetJob), [])
        self.assertEqual(self.rows(db.BudgetJobRequest), [])

    async def test_executor_can_disappear_while_the_body_is_read(self):
        original = api._payload

        def vanish(operation, body):
            self.client.app[api.JOBS_KEY].service = None
            return original(operation, body)
        with mock.patch.object(api, "_payload", side_effect=vanish):
            response = await self.refresh()
        self.assertEqual(response.status, 503)
        self.assertEqual(self.rows(db.BudgetJob), [])

    async def test_capabilities_follow_the_executor_and_poller(self):
        for service, refresh, polling in ((self.service, True, False), (FakeService(poller=True), True, True),
                                          (FakeService(accepting=False, poller=True), False, False), (None, False, False)):
            with self.subTest(refresh=refresh, polling=polling), \
                    mock.patch.object(self.client.app[api.JOBS_KEY], "service", service):
                response = await self.client.get(api.PREFIX + "/connections", headers=self.headers(key=None, origin=False))
                capabilities = (await response.json())["capabilities"]
                self.assertEqual((capabilities["refresh"], capabilities["background_polling"],
                                  capabilities["background_selection"]), (refresh, polling, True))

    async def test_background_selection_is_a_versioned_private_owner_control(self):
        version = await self.version()
        body = {"connection_id": "bank", "enabled": True, "expected_version": version}
        response = await self.client.post(api.PREFIX + "/background-selection", data=json.dumps(body),
                                          headers=self.headers(key=None))
        self.assertEqual(response.status, 200, await response.text())
        saved = api_contract.BudgetConnectionSaved.model_validate(await response.json())
        self.assertTrue(saved.connection.background_enabled and saved.connection.changed)
        stale = await self.client.post(api.PREFIX + "/background-selection",
                                       data=json.dumps({**body, "enabled": False}), headers=self.headers(key=None))
        self.assertEqual(stale.status, 409)
        foreign = await self.client.post(api.PREFIX + "/background-selection",
                                         data=json.dumps({**body, "connection_id": "bob-bank"}),
                                         headers=self.headers(key=None))
        self.assertEqual(foreign.status, 404)
        bearer = await self.client.post(api.PREFIX + "/background-selection", data=json.dumps(body),
                                        headers=self.headers(key=None, bearer=True))
        self.assertEqual(bearer.status, 403)
        audit = self.rows(db.BudgetConnectionAudit)
        self.assertEqual([(row.field, row.actor, row.origin) for row in audit], [("background", "alice", "web")])
        with mock.patch.object(web, "_audit") as global_audit:
            await self.client.post(api.PREFIX + "/background-selection",
                                   data=json.dumps({**body, "enabled": False, "expected_version": version + 1}),
                                   headers=self.headers(key=None))
        global_audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
