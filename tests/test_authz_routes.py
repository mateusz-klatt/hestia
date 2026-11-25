"""Exhaustive route authorization parity and PR 2 discovery contracts."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from aiohttp import web as aiohttp_web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from sqlalchemy import select
from sqlalchemy.orm import Session

from hestia import auth, authz, db, store_sql, web
from hestia.budget import household


def _registered_routes(app):
    for route in app.router.routes():
        path = route.resource.canonical
        yield route.method, path.replace("{path}", "asset.js") if "{path}" in path else path


class RouteMatrixTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self._dir.name) / "routes.db"
        self.engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(
            self.engine, {"admin": "a", "operator": "o", "viewer": "v", "owner2": "b"},
        )
        store_sql.set_user_db("operator", "o", "operator", path=self.path)
        store_sql.set_user_db("viewer", "v", "viewer", path=self.path)
        with self.sessions.begin() as session:
            session.add_all([
                db.BudgetHouseholdMember(member_username="operator", owner="admin", grant_id="1" * 64),
                db.BudgetHouseholdMember(member_username="viewer", owner="admin", grant_id="2" * 64),
            ])
        self.assertEqual(store_sql.activate_catalog_compat(path=self.path), "activated")

    def test_every_registered_route_is_classified_and_compat_matches_legacy(self):
        app = web.make_app(SimpleNamespace())
        routes = list(_registered_routes(app))
        self.assertGreater(len(routes), 100)
        with self.sessions() as session:
            for role in ("viewer", "operator", "admin"):
                for method, path in routes:
                    with self.subTest(role=role, method=method, path=path):
                        public = web._is_public_route(method, path)
                        self.assertTrue(
                            public or (method, path) in web._ROUTE_REQUIREMENTS,
                            "unclassified route",
                        )
                        if public:
                            continue
                        legacy = web._role_allows(role, web._required_role(method, path))
                        compat = web._catalog_allows(
                            session, role, web._required_permission(method, path),
                        )
                        if path.startswith("/api/budget/household/"):
                            legacy = legacy and role in {"viewer", "operator"}
                            compat = compat and authz.authorize(
                                session, role, "household.view", owner="admin",
                            )
                        self.assertEqual(compat, legacy)

    def test_calendar_instances_binding_is_ready_when_another_change_registers_it(self):
        self.assertEqual(
            web._required_permission("GET", "/api/calendars/instances"), "calendar.view",
        )
        self.assertEqual(web._required_role("GET", "/api/calendars/instances"), "viewer")
        with self.sessions() as session:
            self.assertTrue(web._catalog_allows(session, "viewer", "calendar.view"))
            self.assertFalse(web._catalog_allows(session, "viewer", "system.manage_users"))

    def test_custom_grants_not_legacy_roles_control_routes(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            session.get(db.User, "viewer").role = "admin"
            session.add(db.PermissionGrant(
                username="viewer", permission_id="calendar.edit", target_owner=None,
            ))
            session.add(db.PermissionGrant(
                username="viewer", permission_id="budget.view", target_owner="admin",
            ))
        with self.sessions() as session:
            self.assertTrue(authz.authorize(session, "viewer", "calendar.edit"))
            self.assertFalse(authz.authorize(session, "viewer", "ioc.control"))
            self.assertFalse(authz.authorize(session, "viewer", "budget.view", owner="viewer"))
            self.assertFalse(authz.has_legacy_receipt_profile(session, "viewer"))
            users = {row["username"]: row["role"] for row in store_sql.list_users(path=self.path)}
            self.assertEqual(users["viewer"], "viewer")
            self.assertEqual(
                web._catalog_allows(session, "viewer", web._required_permission("POST", "/api/control")),
                False,
            )

    def test_custom_role_writes_are_rejected_without_changing_projection(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        self.assertEqual(
            store_sql.set_user_role("viewer", "admin", path=self.path),
            "legacy_role_changes_disabled",
        )
        self.assertEqual(store_sql.get_user_db_role("viewer", path=self.path), "viewer")
        response = web._user_outcome("legacy_role_changes_disabled")
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(response.body)["error"], "legacy_role_changes_disabled")


class DiscoveryHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self._dir.name) / "discovery.db"
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path),
            "HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_BUDGET_RECEIPTS_ENABLED": "1",
            "HESTIA_BUDGET_ANALYTICS_ENABLED": "1", "HESTIA_PUBLIC_ORIGIN": "https://unused.example",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        patches = (
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", b"route-secret"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(self.engine, {"admin": "a", "viewer": "v"})
        store_sql.set_user_db("viewer", "v", "viewer", path=self.path)
        with self.sessions.begin() as session:
            session.add(db.BudgetHouseholdMember(
                member_username="viewer", owner="admin", grant_id="3" * 64,
            ))
        store_sql.activate_catalog_compat(path=self.path)
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addCleanup(self.client.close)

    def _cookie(self, username):
        token = auth.make_session(username, now=web._now(), secret=b"route-secret")
        return {"Cookie": f"hestia_session={token}"}

    async def test_feature_probe_is_public_no_store_and_uses_the_real_router(self):
        response = await self.client.get("/api/auth/features", headers={"Authorization": "Bearer unsupported"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(await response.json(), {
            "auth_contract": 2, "features": ["capabilities"], "qr_origin": None,
        })

        app = web.make_app(SimpleNamespace())

        async def instances(_request):
            return aiohttp_web.json_response({"enabled": False})

        app.router.add_get("/api/calendars/instances", instances, allow_head=False)
        response = await web._auth_features(make_mocked_request("GET", "/api/auth/features", app=app))
        self.assertEqual(json.loads(response.body)["features"], ["capabilities", "calendar_instances"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_capabilities_exact_shape_and_whoami_stays_vesta_compatible(self):
        response = await self.client.get("/api/me/capabilities", headers=self._cookie("admin"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = await response.json()
        self.assertEqual(set(payload), {
            "user", "installation_permissions", "own_budget", "delegated_receipts", "household_available",
        })
        self.assertEqual(payload["user"], "admin")
        self.assertEqual(payload["delegated_receipts"], None)
        self.assertFalse(payload["household_available"])
        self.assertEqual(set(payload["own_budget"]), {
            "permissions", "can_view", "can_use_receipts", "can_manage_devices",
        })
        self.assertTrue(payload["own_budget"]["can_view"])
        self.assertTrue(payload["own_budget"]["can_use_receipts"])
        self.assertTrue(payload["own_budget"]["can_manage_devices"])
        self.assertIn("budget.view", payload["own_budget"]["permissions"])

        whoami = await self.client.get("/api/whoami", headers=self._cookie("admin"))
        self.assertEqual(await whoami.json(), {"user": "admin", "role": "admin"})
        missing = await self.client.get("/api/me/capabilities")
        self.assertEqual(missing.status, 401)

    async def test_household_capability_does_not_expose_owner(self):
        response = await self.client.get("/api/me/capabilities", headers=self._cookie("viewer"))
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertIsNone(payload["own_budget"])
        self.assertIsNone(payload["delegated_receipts"])
        self.assertTrue(payload["household_available"])
        self.assertNotIn("admin", json.dumps(payload))

    async def test_custom_owner_capabilities_survive_projection_and_receipt_narrowing(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            for pid in ("ioc.control", "system.manage_users", "budget.receipts.match"):
                row = session.execute(select(db.PermissionGrant).where(
                    db.PermissionGrant.username == "admin",
                    db.PermissionGrant.permission_id == pid,
                    db.PermissionGrant.target_owner.is_(None) if pid != "budget.receipts.match"
                    else db.PermissionGrant.target_owner == "admin",
                )).scalar_one()
                session.delete(row)
        capabilities = await self.client.get("/api/me/capabilities", headers=self._cookie("admin"))
        payload = (await capabilities.json())
        self.assertTrue(payload["own_budget"]["can_view"])
        self.assertFalse(payload["own_budget"]["can_use_receipts"])
        self.assertTrue(payload["own_budget"]["can_manage_devices"])
        whoami = await self.client.get("/api/whoami", headers=self._cookie("admin"))
        self.assertEqual(await whoami.json(), {"user": "admin", "role": "viewer"})

    async def test_auth_off_capabilities_have_no_private_budget_or_owner(self):
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            response = await self.client.get("/api/me/capabilities")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertIsNone(payload["user"])
        self.assertIsNone(payload["own_budget"])
        self.assertIsNone(payload["delegated_receipts"])
        self.assertFalse(payload["household_available"])
        self.assertNotIn("system.manage_users", payload["installation_permissions"])
        self.assertIn("ioc.configure", payload["installation_permissions"])


if __name__ == "__main__":
    unittest.main()
