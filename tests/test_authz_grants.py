"""Transactional store and browser contracts for the PR 3 grant editor."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import event, select

from hestia import auth, authz, db, store_sql, web
from hestia.budget import household


def _grant(permission_id, target_owner=None):
    return {"permission_id": permission_id, "target_owner": target_owner}


def _grant_keys(payload, field="direct"):
    return {(row["permission_id"], row["target_owner"]) for row in payload[field]}


class GrantEditorStoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self._dir.name) / "grants.db"
        self.engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(self.engine, {
            "admin": "h",
            "owner": "h",
            "owner2": "h",
            "delegate": "h",
            "other": "h",
            "disabled": "h",
            "disabled_owner": "h",
            "dormant_owner": "h",
        })
        for username in ("delegate", "other", "disabled"):
            store_sql.set_user_db(username, "h", "viewer", path=self.path)
        store_sql.set_user_db("dormant_owner", "h", "operator", path=self.path)
        with self.sessions.begin() as session:
            session.get(db.User, "disabled").disabled = True
            session.get(db.User, "disabled_owner").disabled = True
        self.assertEqual(store_sql.activate_catalog_compat(path=self.path), "activated")
        with self.sessions.begin() as session:
            session.get(db.User, "dormant_owner").budget_owner = True

    def _rows(self, username=None):
        with self.sessions() as session:
            query = select(db.PermissionGrant).order_by(db.PermissionGrant.id)
            if username is not None:
                query = query.where(db.PermissionGrant.username == username)
            return [
                (row.id, row.username, row.permission_id, row.target_owner)
                for row in session.execute(query).scalars()
            ]

    def _ids(self, username):
        return {
            (permission_id, target_owner): row_id
            for row_id, _username, permission_id, target_owner in self._rows(username)
        }

    def _phase(self):
        with self.sessions() as session:
            return session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value

    def _get(self, username="delegate", actor="admin"):
        return store_sql.get_user_grants(username, actor=actor, path=self.path)

    def _replace(self, grants, username="delegate", actor="admin"):
        return store_sql.replace_user_grants(username, grants, actor=actor, path=self.path)

    def _assert_error(self, code, grants, username="delegate", actor="admin"):
        before = (self._phase(), self._rows())
        with self.assertRaises(store_sql.GrantEditorError) as caught:
            self._replace(grants, username=username, actor=actor)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self._phase(), self._rows()), before)

    def test_read_shape_direct_effective_closure_and_custom_owner_states(self):
        with self.sessions() as session:
            compat = store_sql.get_user_grants("delegate", actor="admin", path=self.path)
            self.assertEqual(compat["phase"], authz.POLICY_PHASE_COMPAT)
        self.assertNotIn("dormant_owner", compat["budget_owners"])

        selected = [
            _grant("budget.receipts.edit", "owner"),
            _grant("calendar.edit"),
            _grant("ioc.control"),
        ]
        self.assertEqual(self._replace(selected), {"ok": True, "phase": authz.POLICY_PHASE_CUSTOM})
        payload = self._get()
        self.assertEqual(set(payload), {
            "phase", "username", "budget_owner", "budget_owners", "catalog", "direct", "effective",
        })
        self.assertEqual(payload["username"], "delegate")
        self.assertFalse(payload["budget_owner"])
        self.assertIn("dormant_owner", payload["budget_owners"])
        self.assertNotIn("disabled_owner", payload["budget_owners"])
        self.assertEqual(_grant_keys(payload), {
            ("budget.receipts.edit", "owner"),
            ("calendar.edit", None),
            ("ioc.control", None),
        })
        self.assertEqual(_grant_keys(payload, "effective"), {
            ("budget.receipts.edit", "owner"),
            ("budget.receipts.add", "owner"),
            ("budget.receipts.view", "owner"),
            ("calendar.edit", None),
            ("calendar.view", None),
            ("ioc.control", None),
            ("ioc.view", None),
        })
        self.assertNotIn(("budget.receipts.add", "owner"), _grant_keys(payload))
        self.assertEqual(len(payload["catalog"]), len(authz.CATALOG))
        for entry in payload["catalog"]:
            spec = authz.CATALOG[entry["permission_id"]]
            self.assertEqual(set(entry), {
                "permission_id", "domain", "storage_scope", "delegation", "requires",
                "device_eligible", "label", "protected",
            })
            self.assertEqual(entry["requires"], list(spec.requires))
            self.assertEqual(entry["protected"], spec.protected)
        self.assertTrue(self._get("owner")["budget_owner"])

    def test_get_and_identical_post_do_not_activate_custom_and_real_write_is_immediate(self):
        before = self._get()
        ids = self._ids("delegate")
        self.assertEqual(self._get()["phase"], authz.POLICY_PHASE_COMPAT)
        self.assertEqual(
            self._replace(list(reversed(before["direct"]))),
            {"ok": True, "phase": authz.POLICY_PHASE_COMPAT},
        )
        self.assertEqual(self._phase(), authz.POLICY_PHASE_COMPAT)
        self.assertEqual(self._ids("delegate"), ids)

        statements = []
        real_engine = store_sql._immediate_engine

        def captured(path):
            engine = real_engine(path)
            event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
            return engine

        with mock.patch.object(store_sql, "_immediate_engine", captured):
            result = self._replace([])
        self.assertEqual(result, {"ok": True, "phase": authz.POLICY_PHASE_CUSTOM})
        self.assertIn("BEGIN IMMEDIATE", statements)
        self.assertEqual(self._rows("delegate"), [])

    def test_unchanged_permission_ids_survive_reorder_add_remove_and_idempotent_posts(self):
        selected = [
            _grant("ioc.control"),
            _grant("calendar.view"),
            _grant("budget.receipts.add", "owner"),
        ]
        self._replace(selected)
        original = self._ids("delegate")
        unchanged = original.copy()
        self.assertEqual(self._replace(list(reversed(selected)))["phase"], authz.POLICY_PHASE_CUSTOM)
        self.assertEqual(self._ids("delegate"), unchanged)

        expanded = selected + [_grant("ioc.configure")]
        self._replace(expanded)
        after_add = self._ids("delegate")
        for key in unchanged:
            self.assertEqual(after_add[key], unchanged[key])

        self._replace([_grant("ioc.control"), _grant("budget.receipts.add", "owner")])
        remaining = self._ids("delegate")
        self.assertEqual(remaining[_grant_key("ioc.control")], original[_grant_key("ioc.control")])
        self.assertEqual(
            remaining[_grant_key("budget.receipts.add", "owner")],
            original[_grant_key("budget.receipts.add", "owner")],
        )

    def test_self_admin_protection_ordinary_self_edit_and_nonmanager_denial(self):
        current = self._get("admin")["direct"]
        without_manager = [row for row in current if row["permission_id"] != "system.manage_users"]
        self._assert_error("self_manage_users", without_manager, username="admin")

        ordinary = [row for row in current if row["permission_id"] != "ioc.control"]
        result = self._replace(ordinary, username="admin")
        self.assertEqual(result, {"ok": True, "phase": authz.POLICY_PHASE_CUSTOM})
        self.assertIn(("system.manage_users", None), _grant_keys(self._get("admin")))
        self.assertNotIn(("ioc.control", None), _grant_keys(self._get("admin")))
        self._assert_error("manage_users_required", [], actor="delegate")

    def test_last_manager_guard_applies_to_other_accounts(self):
        with mock.patch.object(store_sql, "_other_enabled_admins", return_value=0):
            self._assert_error("last_admin", [], username="owner")

        result = self._replace([], username="owner")
        self.assertEqual(result["phase"], authz.POLICY_PHASE_CUSTOM)
        with self.sessions() as session:
            self.assertFalse(authz.authorize(session, "owner", "system.manage_users"))
            self.assertTrue(authz.authorize(session, "admin", "system.manage_users"))

    def test_invalid_inputs_targets_and_delegation_leave_everything_unchanged(self):
        cases = [
            ("invalid_grants", {}, "delegate", "admin"),
            ("invalid_grant", [{"permission_id": "calendar.view"}], "delegate", "admin"),
            ("unknown_permission", [_grant("calendar.unknown")], "delegate", "admin"),
            ("duplicate_grant", [_grant("calendar.view"), _grant("calendar.view")], "delegate", "admin"),
            ("invalid_scope", [_grant("ioc.view", "owner")], "delegate", "admin"),
            ("invalid_scope", [_grant("budget.view")], "delegate", "admin"),
            ("invalid_target_owner", [_grant("budget.receipts.add", "ghost")], "delegate", "admin"),
            ("invalid_target_owner", [_grant("budget.receipts.add", "other")], "delegate", "admin"),
            ("invalid_target_owner", [_grant("budget.receipts.add", "disabled_owner")], "delegate", "admin"),
            ("invalid_target_owner", [_grant("budget.receipts.add", 3)], "delegate", "admin"),
            ("self_only_delegation", [_grant("budget.view", "owner")], "delegate", "admin"),
            (
                "multiple_targets",
                [_grant("budget.receipts.add", "owner"), _grant("household.view", "owner2")],
                "delegate",
                "admin",
            ),
            (
                "owner_target_invalid",
                [_grant("system.manage_users"), _grant("budget.receipts.add", "owner2")],
                "owner",
                "admin",
            ),
            (
                "household_self_target",
                [_grant("system.manage_users"), _grant("household.view", "owner")],
                "owner",
                "admin",
            ),
            ("user_disabled", [], "disabled", "admin"),
            ("invalid_username", [], "", "admin"),
            ("not_found", [], "ghost", "admin"),
            ("manage_users_required", [], "delegate", "delegate"),
        ]
        for code, grants, username, actor in cases:
            with self.subTest(code=code, username=username, actor=actor):
                self._assert_error(code, grants, username=username, actor=actor)

    def test_provisioned_owner_can_target_only_self_including_self_only_grants(self):
        current = self._get("owner")["direct"]
        selected = [row for row in current if row["permission_id"] != "budget.edit"]
        result = self._replace(selected, username="owner")
        self.assertEqual(result["phase"], authz.POLICY_PHASE_CUSTOM)
        self.assertIn(("budget.receipts.view", "owner"), _grant_keys(self._get("owner"), "effective"))

        self._assert_error("invalid_target_owner", [_grant("budget.view", "delegate")])

    def test_first_custom_write_uses_custom_owner_eligibility(self):
        current = self._get("dormant_owner")["direct"]
        selected = current + [
            _grant("budget.view", "dormant_owner"),
            _grant("budget.receipts.match", "dormant_owner"),
        ]
        result = self._replace(selected, username="dormant_owner")
        self.assertEqual(result["phase"], authz.POLICY_PHASE_CUSTOM)
        with self.sessions() as session:
            self.assertTrue(authz.is_active_budget_owner(session, "dormant_owner"))
        self.assertIn(
            ("budget.receipts.view", "dormant_owner"),
            _grant_keys(self._get("dormant_owner"), "effective"),
        )

    def test_unexpected_write_failure_rolls_back_grants_and_phase(self):
        before = self._rows()
        with mock.patch.object(store_sql, "_revoke_changed_grant_devices", side_effect=RuntimeError("abort")):
            with self.assertRaisesRegex(RuntimeError, "abort"):
                self._replace([])
        self.assertEqual(self._phase(), authz.POLICY_PHASE_COMPAT)
        self.assertEqual(self._rows(), before)

    def _device(self, token_id, owner, actor, revoked_at=None, issued_permissions=None):
        return db.BudgetReceiptDeviceToken(
            id=token_id,
            owner=owner,
            actor=actor,
            device_name=token_id,
            token_hmac=hashlib.sha256(token_id.encode("ascii")).hexdigest(),
            created_at=1.0,
            expires_at=10000.0,
            hard_expires_at=20000.0,
            sliding_written_at=1.0,
            last_used_at=None,
            revoked_at=revoked_at,
            issued_permissions=issued_permissions,
        )

    def test_real_changes_narrow_owner_and_actor_devices_but_idempotent_posts_do_not(self):
        with self.sessions.begin() as session:
            session.add_all([
                self._device("admin-self", "admin", "admin"),
                self._device("admin-delegate", "admin", "delegate"),
                self._device("other-self", "owner2", "owner2"),
                self._device("already-revoked", "admin", "admin", revoked_at=50.0),
            ])
        admin_current = self._get("admin")["direct"]
        admin_selected = [row for row in admin_current if row["permission_id"] != "ioc.control"]
        with mock.patch("hestia.store_sql.time.time", return_value=1000.0):
            self._replace(admin_selected, username="admin")
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "admin-self").revoked_at)
            self.assertEqual(session.get(db.BudgetReceiptDeviceToken, "admin-delegate").revoked_at, 1000.0)
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "other-self").revoked_at)
            self.assertEqual(session.get(db.BudgetReceiptDeviceToken, "already-revoked").revoked_at, 50.0)

        with self.sessions.begin() as session:
            session.add(self._device("delegate-other", "owner2", "delegate"))
        delegate_current = self._get()["direct"]
        delegate_selected = delegate_current + [_grant("calendar.edit")]
        with mock.patch("hestia.store_sql.time.time", return_value=2000.0):
            self._replace(delegate_selected)
        with mock.patch("hestia.store_sql.time.time", return_value=3000.0):
            self._replace(list(reversed(delegate_selected)))
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetReceiptDeviceToken, "delegate-other").revoked_at, 2000.0)
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "other-self").revoked_at)
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "admin-self").revoked_at)

    def test_custom_activation_preflights_existing_foreign_owner_target(self):
        with self.sessions.begin() as session:
            session.add(db.PermissionGrant(
                username="dormant_owner", permission_id="household.view", target_owner="owner",
            ))
        self._assert_error("target_conflict", [], username="other")

    def test_custom_activation_rejects_an_existing_target_without_a_budget(self):
        with self.sessions.begin() as session:
            session.add(db.PermissionGrant(
                username="delegate", permission_id="household.view", target_owner="other",
            ))
        self._assert_error("target_conflict", [_grant("calendar.edit")], username="other")

    def test_partial_receipt_withdrawal_narrows_then_empty_intersection_revokes(self):
        full = sorted(authz.DEVICE_ELIGIBLE_PERMISSIONS)
        with self.sessions.begin() as session:
            session.add(self._device("modern-self", "admin", "admin", issued_permissions=json.dumps(full)))
        current = self._get("admin")["direct"]
        narrowed = [row for row in current if row["permission_id"] != "budget.receipts.match"]
        self._replace(narrowed, username="admin")
        with self.sessions() as session:
            row = session.get(db.BudgetReceiptDeviceToken, "modern-self")
            self.assertIsNone(row.revoked_at)
            self.assertEqual(json.loads(row.issued_permissions),
                             [pid for pid in full if pid != "budget.receipts.match"])
        installation_only = [row for row in narrowed if row["target_owner"] is None]
        self._replace(installation_only, username="admin")
        with self.sessions() as session:
            self.assertIsNotNone(session.get(db.BudgetReceiptDeviceToken, "modern-self").revoked_at)

    def test_household_metadata_and_permission_ids_are_atomic_stable_and_fresh_on_regrant(self):
        granted = household.grant_member("delegate", "owner", path=str(self.path))
        original_scope = granted["grant_id"]
        household_key = _grant_key("household.view", "owner")
        original_permission = self._ids("delegate")[household_key]
        current = self._get()["direct"]
        self.assertEqual(self._replace(current)["phase"], authz.POLICY_PHASE_COMPAT)
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetHouseholdMember, "delegate").grant_id, original_scope)

        with_calendar = current + [_grant("calendar.edit")]
        self.assertEqual(self._replace(with_calendar)["phase"], authz.POLICY_PHASE_CUSTOM)
        self.assertEqual(self._ids("delegate")[household_key], original_permission)
        with self.sessions() as session:
            self.assertEqual(session.get(db.BudgetHouseholdMember, "delegate").grant_id, original_scope)

        without_household = [row for row in with_calendar if row["permission_id"] != "household.view"]
        self._replace(without_household)
        with self.sessions() as session:
            self.assertIsNone(session.get(db.BudgetHouseholdMember, "delegate"))
        self.assertNotIn(household_key, self._ids("delegate"))
        calendar_id = self._ids("delegate")[_grant_key("calendar.edit")]

        self._replace(without_household + [_grant("household.view", "owner")])
        with self.sessions() as session:
            renewed = session.get(db.BudgetHouseholdMember, "delegate")
            self.assertIsNotNone(renewed)
            self.assertNotEqual(renewed.grant_id, original_scope)
        self.assertEqual(self._ids("delegate")[_grant_key("calendar.edit")], calendar_id)

    def test_retargeting_household_access_replaces_the_scope(self):
        previous = household.grant_member("delegate", "owner", path=str(self.path))
        current = self._get()["direct"]
        selected = [grant for grant in current if grant["permission_id"] != "household.view"]
        self._replace(selected + [_grant("household.view", "owner2")])

        self.assertNotIn(("household.view", "owner"), self._ids("delegate"))
        self.assertIn(("household.view", "owner2"), self._ids("delegate"))
        with self.sessions() as session:
            metadata = session.get(db.BudgetHouseholdMember, "delegate")
            self.assertEqual(metadata.owner, "owner2")
            self.assertNotEqual(metadata.grant_id, previous["grant_id"])

    def test_restoring_a_missing_household_grant_rotates_the_existing_metadata_scope(self):
        previous = household.grant_member("delegate", "owner", path=str(self.path))
        with self.sessions.begin() as session:
            grant = session.scalar(select(db.PermissionGrant).filter_by(
                username="delegate", permission_id="household.view", target_owner="owner",
            ))
            session.delete(grant)

        self._replace(self._get()["direct"] + [_grant("household.view", "owner")])

        self.assertIn(("household.view", "owner"), self._ids("delegate"))
        with self.sessions() as session:
            metadata = session.get(db.BudgetHouseholdMember, "delegate")
            self.assertEqual(metadata.owner, "owner")
            self.assertNotEqual(metadata.grant_id, previous["grant_id"])

    def test_repairing_only_household_metadata_preserves_grants_and_skips_device_narrowing(self):
        previous = household.grant_member("delegate", "owner", path=str(self.path))
        selected = self._get()["direct"]
        previous_ids = self._ids("delegate")
        with self.sessions.begin() as session:
            session.delete(session.get(db.BudgetHouseholdMember, "delegate"))

        with mock.patch.object(store_sql, "_revoke_changed_grant_devices") as narrow:
            self._replace(selected)
        narrow.assert_not_called()

        self.assertEqual(self._ids("delegate"), previous_ids)
        with self.sessions() as session:
            metadata = session.get(db.BudgetHouseholdMember, "delegate")
            self.assertEqual(metadata.owner, "owner")
            self.assertNotEqual(metadata.grant_id, previous["grant_id"])

    def test_phase_and_authoritative_backend_are_required(self):
        fresh = Path(self._dir.name) / "fresh.db"
        db.init_db(fresh)
        with self.assertRaises(store_sql.GrantEditorError) as caught:
            store_sql.get_user_grants("admin", actor="admin", path=fresh)
        self.assertEqual(caught.exception.code, "users_not_authoritative")

        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_LEGACY
        with self.assertRaises(store_sql.GrantEditorError) as caught:
            self._get()
        self.assertEqual(caught.exception.code, "catalog_not_active")

        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = "future_phase"
        with self.assertRaises(authz.PolicyPhaseUnsupported):
            self._get()


def _grant_key(permission_id, target_owner=None):
    return permission_id, target_owner


class GrantEditorHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self._dir.name) / "grant-http.db"
        self.secret = b"grant-editor-http-secret"
        self.env = mock.patch.dict(os.environ, {
            "HESTIA_PERSIST": "sqlite",
            "HESTIA_DB": str(self.path),
            "HESTIA_AUTH_USERS_FILE": str(Path(self._dir.name) / "missing-users.json"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        patches = (
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", self.secret),
            mock.patch.object(web, "_COOKIE_SECURE", False),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(self.engine, {"admin": "h", "owner": "h", "viewer": "h"})
        store_sql.set_user_db("viewer", "h", "viewer", path=self.path)
        store_sql.activate_catalog_compat(path=self.path)
        store_sql.activate_sessions_v2(path=self.path)
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace(audit_engine=None))))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def _token(self, username, purpose=auth.SESSION_PURPOSE_BROWSER):
        return auth.make_session(
            username,
            now=web._now(),
            secret=self.secret,
            generation=0,
            purpose=purpose,
        )

    def _headers(self, username="admin", *, native=False, origin=False):
        if native:
            headers = {"Authorization": "Bearer " + self._token(username, auth.SESSION_PURPOSE_NATIVE)}
        else:
            headers = {"Cookie": "hestia_session=" + self._token(username)}
        if origin:
            headers["Origin"] = f"http://{self.client.host}:{self.client.port}"
        return headers

    async def test_read_requires_manager_and_v2_browser_and_does_not_switch_phase(self):
        response = await self.client.get(
            "/api/users/grants?username=viewer",
            headers=self._headers(),
        )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = await response.json()
        self.assertEqual(payload["phase"], authz.POLICY_PHASE_COMPAT)
        self.assertEqual(payload["username"], "viewer")

        native = await self.client.get(
            "/api/users/grants?username=viewer",
            headers=self._headers(native=True),
        )
        self.assertEqual(native.status, 403)
        self.assertEqual((await native.json())["error"], "browser_session_required")
        v1 = auth.make_session("admin", now=web._now(), secret=self.secret)
        upgraded = await self.client.get(
            "/api/users/grants?username=viewer",
            headers={"Cookie": "hestia_session=" + v1},
        )
        self.assertEqual(upgraded.status, 401)
        self.assertEqual((await upgraded.json())["error"], "browser_session_upgrade_required")
        denied = await self.client.get(
            "/api/users/grants?username=viewer",
            headers=self._headers("viewer"),
        )
        self.assertEqual(denied.status, 403)
        self.assertEqual(await denied.read(), b"")
        for query in ("", "?username=viewer&username=admin", "?username=viewer&extra=1"):
            malformed = await self.client.get("/api/users/grants" + query, headers=self._headers())
            self.assertEqual(malformed.status, 400)
        with self.sessions() as session:
            self.assertEqual(
                session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value,
                authz.POLICY_PHASE_COMPAT,
            )

    async def test_write_origin_native_v2_validation_and_atomic_contract(self):
        viewer = await self.client.get("/api/users/grants?username=viewer", headers=self._headers())
        direct = (await viewer.json())["direct"]
        body = {"username": "viewer", "grants": direct}
        for headers, status, code in (
            (self._headers(), 403, "origin_required"),
            ({**self._headers(), "Origin": "https://wrong.example"}, 403, "origin_required"),
            (self._headers(native=True, origin=True), 403, "browser_session_required"),
        ):
            with self.subTest(status=status, code=code):
                response = await self.client.post("/api/users/grants", json=body, headers=headers)
                self.assertEqual(response.status, status)
                self.assertEqual((await response.json())["error"], code)
        v1 = auth.make_session("admin", now=web._now(), secret=self.secret)
        upgraded = await self.client.post(
            "/api/users/grants",
            json=body,
            headers={"Cookie": "hestia_session=" + v1, "Origin": self._headers(origin=True)["Origin"]},
        )
        self.assertEqual(upgraded.status, 401)
        self.assertEqual((await upgraded.json())["error"], "browser_session_upgrade_required")

        no_op = await self.client.post(
            "/api/users/grants",
            json=body,
            headers=self._headers(origin=True),
        )
        self.assertEqual(no_op.status, 200, await no_op.text())
        self.assertEqual(await no_op.json(), {"ok": True, "phase": authz.POLICY_PHASE_COMPAT})

        changed = await self.client.post(
            "/api/users/grants",
            json={"username": "viewer", "grants": [_grant("ioc.control")]},
            headers=self._headers(origin=True),
        )
        self.assertEqual(changed.status, 200, await changed.text())
        self.assertEqual(await changed.json(), {"ok": True, "phase": authz.POLICY_PHASE_CUSTOM})
        effective = await self.client.get("/api/users/grants?username=viewer", headers=self._headers())
        self.assertEqual(
            _grant_keys(await effective.json(), "effective"),
            {("ioc.control", None), ("ioc.view", None)},
        )

        denied = await self.client.post(
            "/api/users/grants",
            json={"username": "viewer", "grants": []},
            headers=self._headers("viewer", origin=True),
        )
        self.assertEqual(denied.status, 403)
        self.assertEqual(await denied.read(), b"")
        invalid = await self.client.post(
            "/api/users/grants",
            json={"username": "viewer", "grants": [_grant("unknown.permission")]},
            headers=self._headers(origin=True),
        )
        self.assertEqual(invalid.status, 400)
        self.assertEqual((await invalid.json())["error"], "unknown_permission")
        after = await self.client.get("/api/users/grants?username=viewer", headers=self._headers())
        self.assertEqual(_grant_keys(await after.json()), {("ioc.control", None)})

    async def test_grant_audit_is_manager_only(self):
        self.client.server.app[web._RT_KEY].audit_engine = self.engine
        changed = await self.client.post(
            "/api/users/grants",
            json={"username": "viewer", "grants": [_grant("ioc.control"), _grant("system.audit.view")]},
            headers=self._headers(origin=True),
        )
        self.assertEqual(changed.status, 200, await changed.text())
        viewer_feed = await self.client.get("/api/audit", headers=self._headers("viewer"))
        self.assertEqual(viewer_feed.status, 200)
        self.assertNotIn("user_grants", json.dumps(await viewer_feed.json()))
        admin_feed = await self.client.get("/api/audit", headers=self._headers())
        events = (await admin_feed.json())["events"]
        grant_event = next(event for event in events if event["action"] == "user_grants")
        self.assertEqual((grant_event["actor"], grant_event["target"]), ("admin", "viewer"))

    async def test_grant_audit_fails_closed_if_authorization_phase_changes(self):
        self.client.server.app[web._RT_KEY].audit_engine = self.engine
        state = store_sql.load_auth_state("admin")
        with mock.patch.object(store_sql, "load_auth_state", side_effect=[
            state, store_sql.SessionPhaseUnsupported("future"),
        ]):
            response = await self.client.get("/api/audit", headers=self._headers())
        self.assertEqual(response.status, 503)
        self.assertEqual(await response.json(), {"ok": False, "error": "authorization_bridge_incompatible"})

    async def test_legacy_issuance_cannot_activate_custom_with_v1_bearer_or_cookie(self):
        with self.sessions.begin() as session:
            session.get(db.AppMeta, store_sql._SESSION_PHASE_KEY).value = store_sql.SESSION_PHASE_LEGACY
        v1 = auth.make_session("admin", now=web._now(), secret=self.secret)
        for headers, status in (
            ({"Authorization": f"Bearer {v1}"}, 403),
            ({"Cookie": f"hestia_session={v1}", "Origin": self._headers(origin=True)["Origin"]}, 401),
        ):
            response = await self.client.post(
                "/api/users/grants",
                json={"username": "viewer", "grants": [_grant("ioc.control")]},
                headers=headers,
            )
            self.assertEqual(response.status, status)
        with self.sessions() as session:
            self.assertEqual(
                session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value,
                authz.POLICY_PHASE_COMPAT,
            )
            self.assertFalse(session.query(db.PermissionGrant).filter_by(
                username="viewer", permission_id="ioc.control",
            ).first())

    async def test_grant_endpoints_require_an_authoritative_sqlite_backend(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            read = await self.client.get("/api/users/grants?username=viewer", headers=self._headers())
            write = await self.client.post(
                "/api/users/grants",
                json={"username": "viewer", "grants": []},
                headers=self._headers(origin=True),
            )
        for response in (read, write):
            self.assertEqual(response.status, 409)
            self.assertEqual(await response.json(), {
                "ok": False, "error": "user management requires the SQLite backend",
            })

    async def test_grant_endpoints_reject_unknown_users_and_invalid_write_bodies(self):
        missing = await self.client.get("/api/users/grants?username=unknown", headers=self._headers())
        self.assertEqual(missing.status, 404)
        self.assertEqual(await missing.json(), {"ok": False, "error": "not_found"})
        malformed = await self.client.post(
            "/api/users/grants",
            data='{"username":"admin","username":"viewer","grants":[]}',
            headers={**self._headers(origin=True), "Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual(await malformed.json(), {"ok": False, "error": "invalid JSON"})
        incomplete = await self.client.post(
            "/api/users/grants", json={"username": "viewer"}, headers=self._headers(origin=True),
        )
        self.assertEqual(incomplete.status, 400)
        self.assertEqual(await incomplete.json(), {"ok": False, "error": "invalid_grant_request"})

    async def test_grant_endpoints_reject_unsupported_phases_from_the_editor(self):
        with mock.patch.object(store_sql, "get_user_grants", side_effect=authz.PolicyPhaseUnsupported("future")):
            read = await self.client.get("/api/users/grants?username=viewer", headers=self._headers())
        with mock.patch.object(
            store_sql, "replace_user_grants", side_effect=store_sql.SessionPhaseUnsupported("future"),
        ):
            write = await self.client.post(
                "/api/users/grants",
                json={"username": "viewer", "grants": []},
                headers=self._headers(origin=True),
            )
        for response in (read, write):
            self.assertEqual(response.status, 503)
            self.assertEqual(await response.json(), {"ok": False, "error": "authorization_bridge_incompatible"})

    async def test_self_manage_users_cannot_be_removed_over_http(self):
        current = await self.client.get("/api/users/grants?username=admin", headers=self._headers())
        grants = [
            row for row in (await current.json())["direct"]
            if row["permission_id"] != "system.manage_users"
        ]
        response = await self.client.post(
            "/api/users/grants",
            json={"username": "admin", "grants": grants},
            headers=self._headers(origin=True),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"], "self_manage_users")
        unchanged = await self.client.get("/api/users/grants?username=admin", headers=self._headers())
        self.assertIn(("system.manage_users", None), _grant_keys(await unchanged.json()))


if __name__ == "__main__":
    unittest.main()
