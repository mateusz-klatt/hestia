"""PR 2's explicit, transactional legacy-to-catalog compatibility cutover."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.orm import Session

from hestia import auth, authz, db, store_sql


class CatalogCutoverTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "hestia.db"
        self.addCleanup(db.reset_engine_cache)
        self.engine, self.sessions = db.init_db(self.path)
        store_sql.cutover_users(
            self.engine,
            {"owner": "h1", "second": "h2", "disabled": "h3", "operator": "h4", "conflicted": "h5"},
        )
        store_sql.set_user_db("operator", "h4", "operator", path=self.path)
        with self.sessions.begin() as session:
            session.get(db.User, "disabled").disabled = True
            session.add(db.BudgetHouseholdMember(
                member_username="operator", owner="owner", grant_id="a" * 64,
            ))
            session.add(db.BudgetHouseholdMember(
                member_username="conflicted", owner="owner", grant_id="b" * 64,
            ))
            session.add(db.BudgetReceiptDeviceToken(
                id="old-token", owner="owner", actor=None, device_name="Old Vesta",
                token_hmac="c" * 64, created_at=10.0, expires_at=20.0, hard_expires_at=30.0,
                sliding_written_at=11.0, last_used_at=12.0, revoked_at=13.0,
            ))

    def test_cutover_seeds_existing_admins_households_and_legacy_token_actors(self):
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path)}):
            self.assertEqual(store_sql.activate_catalog_compat(), "activated")
            self.assertEqual(store_sql.activate_catalog_compat(), "already_active")

        with self.sessions() as session:
            self.assertEqual(
                session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value,
                authz.POLICY_PHASE_COMPAT,
            )
            self.assertEqual({
                name: session.get(db.User, name).budget_owner
                for name in ("owner", "second", "disabled", "operator", "conflicted")
            }, {
                "owner": True, "second": True, "disabled": True,
                "operator": False, "conflicted": True,
            })
            token = session.get(db.BudgetReceiptDeviceToken, "old-token")
            self.assertEqual(token.actor, "owner")
            self.assertEqual(
                (token.token_hmac, token.created_at, token.expires_at, token.hard_expires_at,
                 token.sliding_written_at, token.last_used_at, token.revoked_at),
                ("c" * 64, 10.0, 20.0, 30.0, 11.0, 12.0, 13.0),
            )
            household = session.execute(select(db.PermissionGrant).where(
                db.PermissionGrant.username == "operator",
                db.PermissionGrant.permission_id == "household.view",
            )).scalar_one()
            self.assertEqual(household.target_owner, "owner")
            self.assertNotIn(
                "budget.view",
                set(session.execute(select(db.PermissionGrant.permission_id).where(
                    db.PermissionGrant.username == "operator"
                )).scalars()),
            )
            self.assertTrue(authz.authorize(session, "disabled", "ioc.view") is False)
            self.assertEqual(authz.project_legacy_role(session, "disabled", include_disabled=True), "admin")

    def test_promoted_admin_keeps_self_ledger_and_viewers_keep_household(self):
        with self.sessions.begin() as session:
            session.add_all([
                db.User(username="promoted", password_hash="h6", role="operator"),
                db.User(username="household_viewer", password_hash="h7", role="viewer"),
            ])
            session.flush()
            session.add_all([
                db.BudgetHouseholdMember(
                    member_username="promoted", owner="owner", grant_id="d" * 64,
                ),
                db.BudgetHouseholdMember(
                    member_username="household_viewer", owner="conflicted", grant_id="e" * 64,
                ),
            ])
            session.get(db.User, "promoted").role = "admin"
        self.assertEqual(store_sql.activate_catalog_compat(path=self.path), "activated")
        with self.sessions() as session:
            for username in ("promoted", "conflicted"):
                self.assertTrue(session.get(db.User, username).budget_owner)
                self.assertTrue(authz.has_legacy_receipt_profile(session, username))
            self.assertTrue(
                authz.authorize(session, "household_viewer", "household.view", owner="conflicted")
            )
            self.assertIsNone(
                session.execute(select(db.PermissionGrant).where(
                    db.PermissionGrant.username == "promoted",
                    db.PermissionGrant.permission_id == "household.view",
                )).scalar_one_or_none()
            )

    def test_cutover_is_transactional(self):
        before = {
            name: session.get(db.User, name).budget_owner
            for name in ("owner", "second", "disabled", "operator", "conflicted")
            for session in [self.sessions()]
        }
        with mock.patch.object(store_sql, "sync_legacy_grants", side_effect=RuntimeError("abort")):
            with self.assertRaisesRegex(RuntimeError, "abort"):
                store_sql.activate_catalog_compat(path=self.path)
        with self.sessions() as session:
            self.assertIsNone(session.get(db.AppMeta, authz.POLICY_PHASE_KEY))
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "old-token").actor)
            self.assertEqual(session.execute(select(db.PermissionGrant)).scalars().all(), [])
            self.assertEqual(
                {name: session.get(db.User, name).budget_owner for name in before}, before,
            )

    def test_cutover_requires_authority_and_never_overwrites_custom(self):
        fresh = Path(self._dir.name) / "fresh.db"
        self.assertEqual(
            store_sql.activate_catalog_compat(path=fresh), "users_not_authoritative",
        )
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key=authz.POLICY_PHASE_KEY, value=authz.POLICY_PHASE_CUSTOM))
            session.add(db.PermissionGrant(
                username="operator", permission_id="ioc.view", target_owner=None,
            ))
        self.assertEqual(store_sql.activate_catalog_compat(path=self.path), "already_custom")
        with self.sessions() as session:
            self.assertEqual(
                session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value,
                authz.POLICY_PHASE_CUSTOM,
            )
            self.assertFalse(session.get(db.User, "owner").budget_owner)
            self.assertIsNone(session.get(db.BudgetReceiptDeviceToken, "old-token").actor)

    def test_custom_role_cli_is_read_only(self):
        with self.sessions.begin() as session:
            session.add(db.AppMeta(key=authz.POLICY_PHASE_KEY, value=authz.POLICY_PHASE_CUSTOM))
        env = {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path)}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(auth._cli(["role", "operator", "admin"]), 1)
            self.assertEqual(
                auth._cli(["add", "operator", "--role", "admin"], prompt=mock.Mock(side_effect=["pw", "pw"])),
                1,
            )

    def test_auth_cli_runs_the_explicit_step(self):
        env = {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.path)}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(auth._cli(["activate-authz-compat", "extra"]), 2)
            self.assertEqual(auth._cli(["activate-authz-compat"]), 0)
            self.assertEqual(auth._cli(["activate-authz-compat"]), 0)
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            self.assertEqual(auth._cli(["activate-authz-compat"]), 1)
        fresh = Path(self._dir.name) / "cli-fresh.db"
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(fresh)}):
            self.assertEqual(auth._cli(["activate-authz-compat"]), 1)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, authz.POLICY_PHASE_KEY).value = authz.POLICY_PHASE_CUSTOM
        with mock.patch.dict(os.environ, env):
            self.assertEqual(auth._cli(["activate-authz-compat"]), 1)


if __name__ == "__main__":
    unittest.main()
