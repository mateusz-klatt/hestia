"""Authorization must be rechecked inside each Budget operation's own SQLite snapshot."""

import unittest
from unittest import mock

from sqlalchemy import delete

from hestia import db, store_sql
from hestia.budget import analytics_api, receipt_api
from hestia.budget.views import BudgetAccessDenied, BudgetHTTPStore
from tests import test_budget_api as budget_fixture
from tests import test_budget_device_tokens as device_fixture


class TransactionAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    rows = budget_fixture.BudgetAPITests.rows

    async def asyncSetUp(self):
        await budget_fixture.BudgetAPITests.asyncSetUp(self)
        key = device_fixture._make_rule_key(self.path.parent)
        patch = mock.patch.dict("os.environ", {device_fixture.RULE_KEY_FILE: str(key)})
        patch.start()
        self.addCleanup(patch.stop)
        store_sql.activate_catalog_compat(path=self.path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, "authz_policy_phase").value = "catalog_custom"
        self.store = analytics_api._store(budget_fixture.SECRET)

    def _revoke_on_owner_check(self, permission):
        original = BudgetHTTPStore._owner

        def revoke_first(session, username):
            permissions = ("budget.view", "budget.edit") if permission == "budget.view" else (permission,)
            session.execute(delete(db.PermissionGrant).where(
                db.PermissionGrant.username == username,
                db.PermissionGrant.permission_id.in_(permissions),
                db.PermissionGrant.target_owner == username,
            ))
            original(session, username)

        return mock.patch.object(BudgetHTTPStore, "_owner", new=staticmethod(revoke_first))

    def test_category_create_rolls_back_when_edit_is_revoked_on_transaction_entry(self):
        with self._revoke_on_owner_check("budget.edit"):
            with self.assertRaises(BudgetAccessDenied) as denied:
                self.store.create_category("alice", "Revoked write")
        self.assertEqual(denied.exception.status, 403)
        with self.sessions() as session:
            self.assertEqual(session.query(db.BudgetCategory).count(), 0)

    def test_account_read_is_denied_when_view_is_revoked_on_snapshot_entry(self):
        with self._revoke_on_owner_check("budget.view"):
            with self.assertRaises(BudgetAccessDenied) as denied:
                self.store.accounts("alice")
        self.assertEqual(denied.exception.status, 403)

    def test_category_decoration_rechecks_view_in_second_snapshot(self):
        category = self.store.create_category("alice", "Translated")
        self.store.set_category_translation(
            "alice", category_id=category["id"], lang="en", name="Translated label",
        )
        original = type(self.store).categories

        def revoke_before_decoration(owner):
            result = original(self.store, owner)
            with self.sessions.begin() as session:
                session.execute(delete(db.PermissionGrant).where(
                    db.PermissionGrant.username == owner,
                    db.PermissionGrant.permission_id.in_(("budget.view", "budget.edit")),
                    db.PermissionGrant.target_owner == owner,
                ))
            return result

        with mock.patch.object(type(self.store), "categories", side_effect=revoke_before_decoration):
            with self.assertRaises(BudgetAccessDenied) as denied:
                analytics_api._read("alice", "categories", {}, budget_fixture.SECRET)
        self.assertEqual(denied.exception.status, 403)

    def test_receipt_profile_does_not_require_full_ledger_view(self):
        with self.sessions.begin() as session:
            session.execute(delete(db.PermissionGrant).where(
                db.PermissionGrant.username == "alice",
                db.PermissionGrant.permission_id.in_(("budget.view", "budget.edit")),
                db.PermissionGrant.target_owner == "alice",
            ))
        result = receipt_api._store(budget_fixture.SECRET).list("alice", {})
        self.assertEqual(result, {"receipts": [], "next_cursor": None})


if __name__ == "__main__":
    unittest.main()
