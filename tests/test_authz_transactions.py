"""Authorization must be rechecked inside each Budget operation's own SQLite snapshot."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sqlalchemy import delete

from hestia import db, store_sql
from hestia.budget import analytics_api, receipt_api
from hestia.budget.store import BudgetStore
from hestia.budget.views import BudgetAccessDenied, BudgetHTTPStore
from tests.test_budget_api import SECRET
from tests.test_budget_store import configuration


class TransactionAuthorizationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "authorization.db"
        self.enterContext(mock.patch.dict("os.environ", {"HESTIA_DB": str(path)}))
        self.addCleanup(db.reset_engine_cache)
        engine, self.sessions = db.init_db(path)
        store_sql.cutover_users(engine, {"alice": "unused"})
        BudgetStore(self.sessions).configure(configuration(owner="alice"))
        store_sql.activate_catalog_compat(path=path)
        with self.sessions.begin() as session:
            session.get(db.AppMeta, "authz_policy_phase").value = "catalog_custom"
        self.store = analytics_api._store(SECRET)

    @staticmethod
    def _revoke_grants(session, username, *permissions):
        session.execute(delete(db.PermissionGrant).where(
            db.PermissionGrant.username == username,
            db.PermissionGrant.permission_id.in_(permissions),
            db.PermissionGrant.target_owner == username,
        ))

    def test_category_create_rolls_back_when_edit_is_revoked_on_transaction_entry(self):
        original = BudgetHTTPStore._owner

        def revoke_first(session, username):
            self._revoke_grants(session, username, "budget.edit")
            original(session, username)

        with mock.patch.object(BudgetHTTPStore, "_owner", new=staticmethod(revoke_first)):
            with self.assertRaises(BudgetAccessDenied) as denied:
                self.store.create_category("alice", "Revoked write")
        self.assertEqual(denied.exception.status, 403)
        with self.sessions() as session:
            self.assertEqual(session.query(db.BudgetCategory).count(), 0)

    def test_account_read_is_denied_when_view_is_revoked_on_snapshot_entry(self):
        self.assertEqual(len(self.store.accounts("alice")), 1)

        def revoke_before_snapshot():
            # Commit the policy change before the store opens its read transaction.
            with self.sessions.begin() as session:
                self._revoke_grants(session, "alice", "budget.view", "budget.edit")
            return self.sessions()

        with mock.patch.object(self.store, "sessions", side_effect=revoke_before_snapshot):
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
                self._revoke_grants(session, owner, "budget.view", "budget.edit")
            return result

        with mock.patch.object(type(self.store), "categories", side_effect=revoke_before_decoration):
            with self.assertRaises(BudgetAccessDenied) as denied:
                analytics_api._read("alice", "categories", {}, SECRET)
        self.assertEqual(denied.exception.status, 403)

    def test_inbox_decoration_rechecks_owner_without_requiring_ledger_view(self):
        original = type(self.store).inbox

        def disable_before_decoration(owner):
            result = original(self.store, owner)
            with self.sessions.begin() as session:
                session.get(db.User, owner).disabled = True
            return result

        with mock.patch.object(type(self.store), "inbox", side_effect=disable_before_decoration):
            with self.assertRaises(BudgetAccessDenied) as denied:
                analytics_api._read("alice", "inbox", {}, SECRET)
        self.assertEqual(denied.exception.status, 401)

    def test_receipt_profile_does_not_require_full_ledger_view(self):
        with self.sessions.begin() as session:
            self._revoke_grants(session, "alice", "budget.view", "budget.edit")
        result = receipt_api._store(SECRET).list("alice", {})
        self.assertEqual(result, {"receipts": [], "next_cursor": None})


if __name__ == "__main__":
    unittest.main()
