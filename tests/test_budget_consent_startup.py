"""Consent stays available when the normal executor starts a round immediately."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from hestia import auth, db, store_sql, web
from hestia.budget import consent_api, consent_service, jobs
from hestia.budget.runtime import budget_lock
from tests.test_budget_store import configuration


class _ActiveWorker:
    """A normal executor round owns the ordinary lock; no provider is involved."""

    accepting = True
    poller = False

    def __init__(self, path):
        self.path = path
        self.held = None

    def start(self):
        self.held = budget_lock(self.path)
        self.held.__enter__()

    def release(self):
        if self.held is not None:
            held, self.held = self.held, None
            held.__exit__(None, None, None)

    async def stop(self):
        self.release()


class ConsentStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediately_active_executor_does_not_permanently_disable_consent(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        target = Path(directory.name) / "synthetic.db"
        secret = b"synthetic-startup-session-secret"
        config = configuration(accounts=("synthetic-account-hash",))
        env = {"HESTIA_DB": str(target), "HESTIA_PERSIST": "sqlite", "HESTIA_BUDGET_HTTP_ENABLED": "1",
               "HESTIA_BUDGET_JOBS_ENABLED": "1", "HESTIA_BUDGET_POLLER_ENABLED": "0",
               consent_api.FLAG: "1", "HESTIA_BUDGET_PUBLIC_ORIGIN": "https://hestia.example.test",
               "HESTIA_BUDGET_CONSENT_DIR": str(Path(directory.name) / "capsules")}
        worker = _ActiveWorker(target)
        with mock.patch.dict(os.environ, env), mock.patch.object(web, "_AUTH_ENABLED", True), \
             mock.patch.object(web, "_SESSION_SECRET", secret), mock.patch.object(web, "_COOKIE_SECURE", True), \
             mock.patch.object(consent_api, "load_config", return_value=config), \
             mock.patch.object(jobs, "build_service", return_value=worker), \
             mock.patch.object(consent_service, "_read_secret", side_effect=AssertionError("No secret reads")), \
             mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No provider requests")):
            engine, _ = db.init_db(target)
            store_sql.cutover_users(engine, {"owner": "synthetic-unused-password-hash"})
            client = TestClient(TestServer(web.make_app(SimpleNamespace(audit_engine=None))))
            try:
                await client.start_server()
                # A real round can now hold the lock for minutes. Stored consent reads
                # and capabilities must remain ready both during and after that round.
                self.assertIsNotNone(worker.held)
                self.assertIsNotNone(client.server.app[consent_api.KEY].service)
                headers = {"Cookie": "hestia_session=" + auth.make_session("owner", now=web._now(), secret=secret)}
                response = await client.get(consent_api.PREFIX + "/status", headers=headers)
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), {"consents": []})
                worker.release()
                response = await client.get("/api/budget/connections", headers=headers)
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())["capabilities"]["consent"])
                self.assertFalse((Path(directory.name) / "capsules").exists())
            finally:
                await client.close()
