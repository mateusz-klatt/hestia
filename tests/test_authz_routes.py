"""Exhaustive route authorization parity and PR 2 discovery contracts."""

import asyncio
import io
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
from hestia import auth, authz, db, proxy, store_sql, web
from hestia.budget import device_token, household


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

    def test_legacy_household_availability_stays_boolean_and_role_scoped(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_LEGACY
        with self.sessions() as session:
            self.assertTrue(authz.household_available(session, "viewer"))
            self.assertTrue(authz.household_available(session, "operator"))
            self.assertFalse(authz.household_available(session, "admin"))
            self.assertFalse(authz.household_available(session, "ghost"))
        with self.sessions.begin() as session:
            session.get(db.User, "admin").disabled = True
            session.get(db.User, "viewer").disabled = True
        with self.sessions() as session:
            self.assertFalse(authz.household_available(session, "viewer"))
            self.assertFalse(authz.household_available(session, "operator"))

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
            store_sql.sync_legacy_grants(session)
            self.assertNotIn("ioc.control", set(session.execute(
                select(db.PermissionGrant.permission_id).where(
                    db.PermissionGrant.username == "viewer",
                    db.PermissionGrant.target_owner.is_(None),
                )
            ).scalars()))
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

    def test_custom_account_writes_and_projected_last_admin_guard(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        self.assertEqual(store_sql.set_user_db("custom-new", "h", path=self.path), "viewer")
        self.assertEqual(store_sql.add_user("candidate", "h", "viewer", path=self.path), "ok")
        self.assertEqual(
            store_sql.set_user_db("candidate", "h2", "operator", path=self.path),
            "legacy_role_changes_disabled",
        )
        self.assertEqual(store_sql.provision_budget_owner("candidate", path=self.path), "ok")
        with self.sessions.begin() as session:
            session.get(db.User, "admin").role = "viewer"
            session.get(db.User, "owner2").disabled = True
        self.assertEqual(store_sql.set_user_disabled("admin", True, path=self.path), "last_admin")
        granted = household.grant_member("candidate", "admin", path=str(self.path))
        with self.sessions.begin() as session:
            permission = session.execute(select(db.PermissionGrant).where(
                db.PermissionGrant.username == "candidate",
                db.PermissionGrant.permission_id == "household.view",
                db.PermissionGrant.target_owner == "admin",
            )).scalar_one()
            session.delete(permission)
        self.assertEqual(
            household.revoke_grant("candidate", "admin", granted["grant_id"], path=str(self.path))["status"],
            "revoked",
        )
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = "future_phase"
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result = household.run_household_command(SimpleNamespace(
                action="grant", member="candidate", owner="admin", db=str(self.path),
            ))
        self.assertEqual(result, 1)
        self.assertIn("authorization_bridge_incompatible", stderr.getvalue())

    def test_custom_household_grant_rejects_owner_and_second_target(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        for member, owner in (("owner2", "admin"), ("viewer", "owner2")):
            with self.subTest(member=member, owner=owner):
                with self.assertRaises(household.HouseholdCommandError) as denied:
                    household.grant_member(member, owner, path=str(self.path))
                self.assertEqual(denied.exception.code, "household_member_invalid")
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetHouseholdMember, "owner2"))
            self.assertEqual(authz.resolve_budget_target(session, "owner2"), "owner2")
            self.assertEqual(authz.resolve_budget_target(session, "viewer"), "admin")

    def test_last_account_manager_guard_uses_effective_grant_not_role_projection(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
            session.get(db.User, "admin").disabled = True
            session.get(db.User, "owner2").disabled = True
            session.add(db.User(username="manager", password_hash="h", role="viewer"))
            session.flush()
            session.add(db.PermissionGrant(
                username="manager", permission_id="system.manage_users", target_owner=None,
            ))
        with self.sessions() as session:
            self.assertEqual(authz.project_legacy_role(session, "manager"), "viewer")
        self.assertEqual(store_sql.set_user_disabled("manager", True, path=self.path), "last_admin")
        with self.sessions() as session:
            self.assertFalse(session.get(db.User, "manager").disabled)

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


class CapabilityUnitTests(unittest.TestCase):
    def _state(self, role="admin", *, enabled=True, phase="legacy"):
        return store_sql.AuthState(
            backend="json", session_phase="legacy", policy_phase=phase,
            enabled=enabled, role=role if enabled else None, generation=0,
        )

    def test_pure_json_capabilities_do_not_open_sqlite(self):
        with mock.patch.object(store_sql, "authorization_snapshot", side_effect=AssertionError("opened SQLite")):
            admin = web._capability_payload("admin", self._state())
            viewer = web._capability_payload("viewer", self._state("viewer"))
        self.assertFalse(admin["own_budget"]["can_view"])
        self.assertEqual(admin["installation_permissions"], sorted(store_sql._LEGACY_ADMIN_GRANTS))
        self.assertIsNone(viewer["own_budget"])
        self.assertEqual(viewer["installation_permissions"], sorted(store_sql._LEGACY_VIEWER_GRANTS))

    def test_issued_masks_accept_only_valid_legacy_self_profiles(self):
        self.assertEqual(
            device_token._issued_mask(SimpleNamespace(actor="owner", owner="owner", issued_permissions=None)),
            authz.LEGACY_RECEIPT_PERMISSIONS,
        )
        self.assertEqual(
            device_token._issued_mask(SimpleNamespace(actor="delegate", owner="owner", issued_permissions=None)),
            frozenset(),
        )
        self.assertEqual(
            device_token._issued_mask(SimpleNamespace(
                actor="owner", owner="owner",
                issued_permissions=json.dumps(sorted(authz.LEGACY_RECEIPT_PERMISSIONS)),
            )),
            authz.LEGACY_RECEIPT_PERMISSIONS,
        )
        for invalid in ("{", "{}", '["unknown.permission"]', '["budget.view",1]', 1):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    device_token._issued_mask(SimpleNamespace(
                        actor="owner", owner="owner", issued_permissions=invalid,
                    )),
                    frozenset(),
                )

    def test_session_context_recheck_covers_legacy_custom_and_failures(self):
        ctx = web.ValidatedSessionContext(
            username="admin", role="admin", carrier="cookie", version=1, purpose=None,
            generation=0, expiry=2**40, session_phase="legacy", policy_phase="legacy", raw_token="bad",
        )
        self.assertFalse(web._session_context_current(ctx))
        valid = auth.make_session("admin", now=web._now(), secret=b"route-secret")
        ctx = mock.Mock(raw_token=valid, username="admin", carrier="cookie")
        with mock.patch.object(web, "_SESSION_SECRET", b"route-secret"), \
             mock.patch.object(store_sql, "load_auth_state", return_value=self._state(enabled=False)):
            self.assertFalse(web._session_context_current(ctx))
        with mock.patch.object(web, "_SESSION_SECRET", b"route-secret"), \
             mock.patch.object(store_sql, "load_auth_state", return_value=self._state()):
            self.assertTrue(web._session_context_current(ctx))
        with mock.patch.object(web, "_SESSION_SECRET", b"route-secret"), \
             mock.patch.object(store_sql, "load_auth_state", return_value=self._state(phase="catalog_custom")), \
             mock.patch.object(web, "_catalog_request_allowed", return_value=True):
            self.assertTrue(web._session_context_current(ctx))
        with mock.patch.object(web, "_SESSION_SECRET", b"route-secret"), \
             mock.patch.object(store_sql, "load_auth_state", side_effect=authz.PolicyPhaseUnsupported("future")):
            self.assertFalse(web._session_context_current(ctx))


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
        app = web.make_app(SimpleNamespace())
        app[web._RT_KEY].event_bus = proxy.EventBus()
        self.client = TestClient(TestServer(app))
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

    async def test_capability_handler_rechecks_disabled_and_incompatible_states(self):
        request = make_mocked_request("GET", "/api/me/capabilities", app=self.client.server.app)
        request[web._USER_KEY] = "admin"
        disabled = store_sql.AuthState(
            backend="sqlite", session_phase="legacy", policy_phase="catalog_compat",
            enabled=False, role=None, generation=0,
        )
        with mock.patch.object(store_sql, "load_auth_state", return_value=disabled):
            response = await web._me_capabilities(request)
        self.assertEqual(response.status, 401)
        with mock.patch.object(
            store_sql, "load_auth_state", side_effect=store_sql.SessionPhaseUnsupported("future"),
        ):
            response = await web._me_capabilities(request)
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["error"], "authorization_bridge_incompatible")

        with mock.patch.object(
            web, "_catalog_request_allowed", side_effect=authz.PolicyPhaseUnsupported("future"),
        ):
            response = await self.client.get("/api/discovery", headers=self._cookie("admin"))
        self.assertEqual(response.status, 503)

    async def test_sse_rechecks_and_closes_after_authorization_loss(self):
        with mock.patch.multiple(web, SSE_IDLE_TIMEOUT=0.05, SSE_MAX_LIFETIME=0.5):
            response = await self.client.get("/api/events", headers=self._cookie("admin"))
            self.assertEqual(response.status, 200)
            chunk, _ = await asyncio.wait_for(response.content.readchunk(), 2)
            self.assertTrue(chunk)
            with self.sessions.begin() as session:
                session.get(db.User, "admin").disabled = True
            await asyncio.wait_for(response.read(), 2)

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
