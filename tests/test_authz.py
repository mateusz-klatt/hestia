"""Tests for the Hestia authorization catalog, phase boundary, and scope evaluator."""

import tempfile
import unittest
from pathlib import Path

from sqlalchemy.orm import Session

from hestia import authz, db


def _add_user(session: Session, username: str, *, role: str = "viewer", disabled: bool = False,
              budget_owner: bool = False) -> None:
    session.add(
        db.User(
            username=username,
            password_hash="scrypt$stub",
            role=role,
            disabled=disabled,
            budget_owner=budget_owner,
        )
    )
    session.flush()


def _add_grant(session: Session, username: str, permission_id: str, target_owner: "str | None" = None) -> None:
    session.add(
        db.PermissionGrant(
            username=username,
            permission_id=permission_id,
            target_owner=target_owner,
        )
    )


class CatalogValidationTests(unittest.TestCase):

    def test_builtin_catalog_matches_expected_14_entries_and_rules(self):
        self.assertEqual(
            set(authz.CATALOG),
            {
                "ioc.view",
                "ioc.control",
                "ioc.configure",
                "calendar.view",
                "calendar.edit",
                "system.audit.view",
                "system.manage_users",
                "budget.view",
                "budget.edit",
                "budget.receipts.add",
                "budget.receipts.view",
                "budget.receipts.edit",
                "budget.receipts.match",
                "household.view",
            },
        )
        self.assertEqual(
            authz.DEVICE_ELIGIBLE_PERMISSIONS,
            {
                "budget.receipts.add",
                "budget.receipts.view",
                "budget.receipts.edit",
                "budget.receipts.match",
            },
        )
        self.assertTrue(authz.CATALOG["system.manage_users"].protected)
        self.assertFalse(authz.CATALOG["ioc.view"].protected)
        self.assertEqual(authz.CATALOG["household.view"].domain, "budget")
        self.assertEqual(authz.CATALOG["household.view"].storage_scope, "owner")
        self.assertEqual(authz.CATALOG["household.view"].delegation, "delegable")
        # Self-only budget.receipts.match implies delegable budget.receipts.view on the same owner.
        self.assertEqual(authz.CATALOG["budget.receipts.match"].delegation, "self_only")
        self.assertEqual(authz.CATALOG["budget.receipts.match"].requires, ("budget.receipts.view",))

    def test_validate_catalog_rejects_invalid_declarations(self):
        base = authz.PermissionSpec("ioc.view", "ioc", "installation", None, (), False, "View")
        cases = [
            (
                "duplicate id",
                (base, authz.PermissionSpec("ioc.view", "ioc", "installation", None, (), False, "Dup")),
            ),
            (
                "installation with delegation",
                (authz.PermissionSpec("ioc.view", "ioc", "installation", "delegable", (), False, "Bad"),),
            ),
            (
                "owner with invalid delegation",
                (authz.PermissionSpec("budget.view", "budget", "owner", None, (), False, "Bad"),),
            ),
            (
                "invalid storage scope",
                (authz.PermissionSpec("ioc.view", "ioc", "tenant", None, (), False, "Bad"),),
            ),
            (
                "unknown requirement",
                (authz.PermissionSpec("ioc.control", "ioc", "installation", None, ("ioc.missing",), False, "Ctrl"),),
            ),
            (
                "cross-domain implication",
                (
                    base,
                    authz.PermissionSpec("calendar.edit", "calendar", "installation", None, ("ioc.view",), False, "Cal"),
                ),
            ),
            (
                "cross-storage-scope implication",
                (
                    authz.PermissionSpec("budget.view", "budget", "owner", "self_only", (), False, "View"),
                    authz.PermissionSpec("budget.admin", "budget", "installation", None, ("budget.view",), False, "Adm"),
                ),
            ),
            (
                "delegable requiring self-only",
                (
                    authz.PermissionSpec("budget.view", "budget", "owner", "self_only", (), False, "View"),
                    authz.PermissionSpec("budget.bad", "budget", "owner", "delegable", ("budget.view",), False, "Bad"),
                ),
            ),
            (
                "direct cycle",
                (authz.PermissionSpec("ioc.a", "ioc", "installation", None, ("ioc.a",), False, "A"),),
            ),
            (
                "indirect cycle",
                (
                    authz.PermissionSpec("ioc.a", "ioc", "installation", None, ("ioc.b",), False, "A"),
                    authz.PermissionSpec("ioc.b", "ioc", "installation", None, ("ioc.a",), False, "B"),
                ),
            ),
        ]
        for label, specs in cases:
            with self.subTest(label=label):
                with self.assertRaises(authz.CatalogValidationError):
                    authz.validate_catalog(specs)

    def test_validate_catalog_handles_diamond_dag(self):
        validated = authz.validate_catalog(
            (
                authz.PermissionSpec("ioc.root", "ioc", "installation", None, (), False, "Root"),
                authz.PermissionSpec("ioc.left", "ioc", "installation", None, ("ioc.root",), False, "Left"),
                authz.PermissionSpec("ioc.right", "ioc", "installation", None, ("ioc.root",), False, "Right"),
                authz.PermissionSpec("ioc.top", "ioc", "installation", None, ("ioc.left", "ioc.right"), False, "Top"),
            )
        )
        self.assertEqual(set(validated), {"ioc.root", "ioc.left", "ioc.right", "ioc.top"})


class AuthzEvaluationTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.engine, _ = db.init_db(Path(self._dir.name) / "authz.db")
        self.addCleanup(self.engine.dispose)

    def test_policy_phase_check(self):
        with Session(self.engine) as session:
            self.assertEqual(authz.require_supported_policy_phase(session), "legacy")
            session.add(db.AppMeta(key="authz_policy_phase", value="catalog_compat"))
            session.commit()
            self.assertEqual(authz.require_supported_policy_phase(session), "catalog_compat")
            session.get(db.AppMeta, "authz_policy_phase").value = "catalog_custom"
            session.commit()
            with self.assertRaises(authz.PolicyPhaseUnsupported) as exc_info:
                authz.require_supported_policy_phase(session)
            self.assertEqual(exc_info.exception.phase, "catalog_custom")

    def test_installation_closure_without_direct_view_and_diamond_overlap(self):
        with Session(self.engine) as session, session.begin():
            _add_user(session, "alice", role="operator")
            _add_grant(session, "alice", "ioc.control")
            _add_grant(session, "alice", "ioc.configure")
            _add_grant(session, "alice", "calendar.view")

        with Session(self.engine) as session:
            perms = authz.effective_permissions(session, "alice")
            self.assertEqual(perms, frozenset({"ioc.control", "ioc.configure", "ioc.view", "calendar.view"}))
            self.assertTrue(authz.authorize(session, "alice", "ioc.view"))
            self.assertFalse(authz.authorize(session, "alice", "calendar.edit"))
            # Owner-scoped permission checked in installation scope must be rejected.
            self.assertFalse(authz.authorize(session, "alice", "budget.view"))

    def test_calendar_only_grants_and_wrong_scope_ignored(self):
        with Session(self.engine) as session, session.begin():
            _add_user(session, "owner", role="admin", budget_owner=True)
            _add_user(session, "cal", role="viewer")
            _add_grant(session, "cal", "calendar.edit")
            # Bogus wrong-scope row and unknown stored ID must be ignored gracefully.
            _add_grant(session, "cal", "budget.view", None)
            _add_grant(session, "cal", "nonexistent.perm", None)
            _add_grant(session, "cal", "ioc.view", "owner")
            _add_grant(session, "cal", "nonexistent.owner_perm", "owner")

        with Session(self.engine) as session:
            self.assertEqual(authz.effective_permissions(session, "cal"), frozenset({"calendar.edit", "calendar.view"}))
            self.assertEqual(authz.effective_permissions(session, "cal", owner="owner"), frozenset())
            self.assertIsNone(authz.resolve_budget_target(session, "cal"))

    def test_owner_self_closure_and_delegation_filtering(self):
        with Session(self.engine) as session, session.begin():
            _add_user(session, "owner", role="admin", budget_owner=True)
            _add_user(session, "spouse", role="operator")
            # Self-only receipts.match implies delegable receipts.view for owner.
            _add_grant(session, "owner", "budget.receipts.match", "owner")
            _add_grant(session, "owner", "budget.edit", "owner")
            # Delegate gets receipts.edit (implies add + view) plus household.view, while a rogue self-only row is ignored.
            _add_grant(session, "spouse", "budget.receipts.edit", "owner")
            _add_grant(session, "spouse", "household.view", "owner")
            _add_grant(session, "spouse", "budget.view", "owner")

        with Session(self.engine) as session:
            self.assertEqual(
                authz.effective_permissions(session, "owner", owner="owner"),
                frozenset({"budget.receipts.match", "budget.receipts.view", "budget.edit", "budget.view"}),
            )
            self.assertEqual(authz.resolve_budget_target(session, "owner"), "owner")
            self.assertEqual(
                authz.effective_permissions(session, "spouse", owner="owner"),
                frozenset({"budget.receipts.edit", "budget.receipts.add", "budget.receipts.view", "household.view"}),
            )
            self.assertFalse(authz.authorize(session, "spouse", "budget.view", owner="owner"))
            self.assertTrue(authz.authorize(session, "spouse", "budget.receipts.add", owner="owner"))
            self.assertEqual(authz.resolve_budget_target(session, "spouse"), "owner")

    def test_disabled_missing_unprovisioned_and_demoted_owner_rejected(self):
        with Session(self.engine) as session, session.begin():
            _add_user(session, "owner", role="admin", budget_owner=True)
            _add_user(session, "disabled_actor", role="admin", disabled=True, budget_owner=True)
            _add_user(session, "disabled_owner", role="admin", disabled=True, budget_owner=True)
            _add_user(session, "unprovisioned", role="admin", budget_owner=False)
            _add_user(session, "demoted", role="operator", budget_owner=True)
            _add_user(session, "spouse", role="viewer")
            _add_grant(session, "disabled_actor", "ioc.view")
            _add_grant(session, "disabled_actor", "household.view", "owner")
            _add_grant(session, "spouse", "household.view", "disabled_owner")
            _add_grant(session, "spouse", "household.view", "unprovisioned")
            _add_grant(session, "spouse", "household.view", "demoted")
            _add_grant(session, "demoted", "budget.view", "demoted")

        with Session(self.engine) as session:
            self.assertEqual(authz.effective_permissions(session, ""), frozenset())
            self.assertEqual(authz.effective_permissions(session, "ghost"), frozenset())
            self.assertEqual(authz.effective_permissions(session, "disabled_actor"), frozenset())
            self.assertEqual(authz.effective_permissions(session, "disabled_actor", owner="owner"), frozenset())
            self.assertEqual(authz.effective_permissions(session, "spouse", owner="ghost"), frozenset())
            self.assertEqual(authz.effective_permissions(session, "spouse", owner="disabled_owner"), frozenset())
            self.assertEqual(authz.effective_permissions(session, "spouse", owner="unprovisioned"), frozenset())
            # Marker on demoted admin does not activate an effective self target before custom activation.
            self.assertEqual(authz.effective_permissions(session, "demoted", owner="demoted"), frozenset())
            self.assertIsNone(authz.resolve_budget_target(session, "demoted"))
            self.assertIsNone(authz.resolve_budget_target(session, ""))
            self.assertIsNone(authz.resolve_budget_target(session, "ghost"))
            self.assertIsNone(authz.resolve_budget_target(session, "disabled_actor"))
            self.assertIsNone(authz.resolve_budget_target(session, "spouse"))

    def test_resolve_budget_target_denies_multiple_active_owners(self):
        with Session(self.engine) as session, session.begin():
            _add_user(session, "owner1", role="admin", budget_owner=True)
            _add_user(session, "owner2", role="admin", budget_owner=True)
            _add_user(session, "spouse", role="viewer")
            _add_grant(session, "owner1", "household.view", "owner2")
            _add_grant(session, "spouse", "household.view", "owner1")
            _add_grant(session, "spouse", "household.view", "owner2")

        with Session(self.engine) as session:
            self.assertIsNone(authz.resolve_budget_target(session, "owner1"))
            self.assertIsNone(authz.resolve_budget_target(session, "spouse"))
