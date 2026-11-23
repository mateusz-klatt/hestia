"""Synthetic proof that memoised analytics reads always equal a fresh projection of the current state."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock
import uuid

from sqlalchemy import and_, select

from hestia import db
from hestia.budget import analytics_views as views
from hestia.budget.analytics_core import AnalyticsError, financial_fingerprint
from hestia.budget.views import BudgetAccessDenied
from tests.test_budget_analytics_core import identity
from tests.test_budget_analytics_views import ProjectionCase


class CacheCase(ProjectionCase):
    def setUp(self):
        super().setUp()
        views.clear_projection_cache()
        self.addCleanup(views.clear_projection_cache)
        self.computed = 0
        original = views._transfer_projection

        def counted(snapshot):
            self.computed += 1
            return original(snapshot)

        patch = mock.patch.object(views, "_transfer_projection", side_effect=counted)
        patch.start()
        self.addCleanup(patch.stop)
        self.pair()
        self.add_transaction("c", "shop", amount="12.30", remittance_information=["Synthetic SHOP"])

    def reads(self):
        return {"overview": self.store.overview("owner", "2026"),
                "month": self.store.overview("owner", "2026-11"),
                "transactions": self.store.transactions("owner", "2026"),
                **{status: self.store.transfers("owner", "2026", status=status)
                   for status in ("suggested", "accepted", "rejected", "undone")}}

    def fresh(self):
        views.clear_projection_cache()
        return self.reads()

    def assert_current(self):
        """One projection per committed state; its output equals a recomputation from scratch."""
        before = self.computed
        cached = self.reads()
        self.assertEqual(self.computed, before + 1)
        self.assertEqual(self.reads(), cached)
        self.assertEqual(self.computed, before + 1)
        self.assertEqual(cached, self.fresh())
        return cached

    def digest(self, session=None):
        if session is not None:
            return views._source_digest(session, "owner")
        with self.sessions() as current:
            return views._source_digest(current, "owner")


class MemoTests(CacheCase):
    def test_unchanged_state_is_projected_once_for_every_read(self):
        self.reads()
        self.assertEqual(self.computed, 1)
        self.reads()
        self.assertEqual(self.computed, 1)
        self.assertEqual(len(views._cache), 1)

    def test_every_source_change_yields_a_new_projection_equal_to_a_fresh_one(self):
        def metadata():
            with self.sessions.begin() as session:
                row = session.get(db.BudgetTransaction, (self.accounts["c"], "shop"))
                session.add(db.BudgetTransactionMetadata(
                    account_id=row.account_id, entry_reference=row.entry_reference, owner="owner",
                    ledger_fingerprint=financial_fingerprint(json.loads(row.payload_json)),
                    payload_json=json.dumps({"creditor_name": "Synthetic Grocer"}), source="api",
                    observed_at=self.now, version=1))

        def unresolved():
            with self.sessions.begin() as session:
                session.add(db.BudgetUnresolved(account_id=self.accounts["a"], ordinal=0, payload_json="{}",
                                                observed_at=self.now))

        def imported():
            with self.sessions.begin() as session:
                batch = db.BudgetImportBatch(id=str(uuid.uuid4()), fingerprint=uuid.uuid4().hex,
                                             account_id=self.accounts["a"], fetched_at=self.now, imported_at=self.now,
                                             balances_json=None, report_json="{}")
                session.add(batch)
                session.flush()
                session.add(db.BudgetImportUnresolved(batch_id=batch.id, ordinal=0, reason="synthetic", payload_json="{}"))

        def delete():
            with self.sessions.begin() as session:
                session.delete(session.get(db.BudgetTransaction, (self.accounts["c"], "late")))

        category = {}
        changes = [
            ("bank entry", lambda: self.add_transaction("c", "late", amount="4", booking_date="2026-12-15")),
            ("bank correction", lambda: self.change_transaction("c", "late", amount="5")),
            ("uncertainty", lambda: self.change_transaction("c", "late", uncertain=True)),
            ("deletion", delete),
            ("category", lambda: category.update(self.store.create_category("owner", "Synthetic Food"))),
            ("annotation", lambda: self.annotation(account="c", reference="shop", category_id=category["id"])),
            ("category rename", lambda: self.store.update_category(
                "owner", category["id"], name="Synthetic Groceries", archived=False, expected_version=1)),
            ("rule", lambda: self.rule(value="synthetic", kind="excluded")),
            ("transfer accept", lambda: category.update(decision=self.decide()["id"])),
            ("transfer undo", lambda: self.decide(decision_id=category["decision"], debit=None, credit=None,
                                                  action="undo", expected_version=1, expected_debit_fingerprint=None,
                                                  expected_credit_fingerprint=None)),
            ("account excluded", lambda: self.select_account("b", False)),
            ("account selected", lambda: self.select_account("b", True)),
            ("own identity", lambda: self.identities("a", [identity("PL-SYNTHETIC-1")])),
            ("counterparty evidence", metadata),
            ("unresolved", unresolved),
            ("import uncertainty", imported),
        ]
        self.assert_current()
        previous = self.digest()
        for name, change in changes:
            with self.subTest(change=name):
                change()
                digest = self.digest()
                self.assertNotEqual(digest, previous)
                previous = digest
                self.assert_current()

    def test_every_column_of_every_source_row_enters_the_key(self):
        self.store.create_category("owner", "Synthetic Food")
        self.rule()
        self.decide()
        self.annotation(account="c", reference="shop")
        self.identities("a", [identity("PL-SYNTHETIC-1")])
        with self.sessions.begin() as session:
            row = session.get(db.BudgetTransaction, (self.accounts["c"], "shop"))
            session.add(db.BudgetTransactionMetadata(
                account_id=row.account_id, entry_reference=row.entry_reference, owner="owner",
                ledger_fingerprint="0" * 64, payload_json="{}", source="api", observed_at=self.now, version=1))
            session.add(db.BudgetUnresolved(account_id=self.accounts["a"], ordinal=0, payload_json="{}", observed_at=self.now))
            batch = db.BudgetImportBatch(id="batch", fingerprint="f" * 64, account_id=self.accounts["a"],
                                         fetched_at=self.now, imported_at=self.now, balances_json="[]", report_json="{}")
            session.add(batch)
            session.flush()
            session.add(db.BudgetImportUnresolved(batch_id="batch", ordinal=0, reason="synthetic", payload_json="{}"))
        models = (db.BudgetConnection, db.BudgetAccount, db.BudgetTransaction, db.BudgetTransactionMetadata,
                  db.BudgetAccountIdentity, db.BudgetAnnotation, db.BudgetCategory, db.BudgetRule,
                  db.BudgetTransferDecision)
        exercised = 0
        with self.sessions() as session:
            for model in models:
                table = model.__table__
                row = session.execute(select(table).where(
                    table.c.owner == "owner" if "owner" in table.c else
                    table.c.batch_id == "batch" if "batch_id" in table.c else
                    table.c.account_id == self.accounts["a"])).mappings().first()
                self.assertIsNotNone(row, table.name)
                keys = and_(*[column == row[column.name] for column in table.primary_key.columns])
                for column in table.columns:
                    value = row[column.name]
                    if column.primary_key or column.foreign_keys or value is None:
                        continue
                    changed = (not value if isinstance(value, bool) else value + 1 if isinstance(value, (int, float))
                               else value + " " if isinstance(value, str) else None)
                    with self.subTest(table=table.name, column=column.name):
                        before = self.digest(session)
                        nested = session.begin_nested()
                        session.execute(table.update().where(keys).values({column.name: changed}))
                        self.assertNotEqual(self.digest(session), before)
                        nested.rollback()
                        self.assertEqual(self.digest(session), before)
                        exercised += 1
            # The projection only counts unresolved observations, so their presence is what enters the key.
            for model in (db.BudgetUnresolved, db.BudgetImportUnresolved):
                with self.subTest(table=model.__table__.name):
                    before = self.digest(session)
                    nested = session.begin_nested()
                    session.execute(model.__table__.delete())
                    self.assertNotEqual(self.digest(session), before)
                    nested.rollback()
        self.assertGreater(exercised, 35)

    def test_excluded_ledger_is_never_scanned_but_still_counted(self):
        self.add_transaction("d", "excluded", amount="7")
        self.select_account("d", False)
        self.assert_current()
        before = self.digest()
        self.change_transaction("d", "excluded", amount="8")
        self.add_transaction("d", "excluded-2", amount="9")
        self.assertEqual(self.digest(), before)  # the projection never reads these payloads
        self.change_transaction("d", "excluded", uncertain=True)
        self.assertNotEqual(self.digest(), before)  # but it does count owner-wide uncertainty
        self.assertEqual(self.assert_current()["overview"]["scope"]["uncertain_count"], 1)

    def test_owner_and_connection_scope_changes_enter_the_key(self):
        def move(model, key, owner):
            with self.sessions.begin() as session:
                session.get(model, key).owner = owner

        with self.sessions() as session:
            connection = session.get(db.BudgetAccount, self.accounts["c"]).connection_id
        self.assert_current()
        for name, change in (("connection to other owner", lambda: move(db.BudgetConnection, connection, "other")),
                             ("connection back", lambda: move(db.BudgetConnection, connection, "owner")),
                             ("account to other owner", lambda: move(db.BudgetAccount, self.accounts["c"], "other")),
                             ("account back", lambda: move(db.BudgetAccount, self.accounts["c"], "owner"))):
            with self.subTest(change=name):
                before = self.digest()
                change()
                self.assertNotEqual(self.digest(), before)
                self.assert_current()

    def test_row_limit_is_enforced_before_any_projection_work(self):
        self.reads()
        with mock.patch.object(views, "MAX_ROWS", 2), \
                mock.patch.object(views, "projection_snapshot", side_effect=AssertionError("projected")):
            for read in (lambda: self.store.overview("owner", "2026"), lambda: self.store.transactions("owner", "2026"),
                         lambda: self.store.transfers("owner", "2026")):
                self.assert_invalid(read, code="budget_analytics_limit", status=413)
        self.assertEqual(self.computed, 1)
        self.assertEqual(len(views._cache), 1)

    def test_authorization_is_rechecked_before_any_memoised_projection(self):
        self.reads()
        for change, status in (({"role": "user"}, 403), ({"disabled": True}, 401)):
            with self.subTest(change=change):
                with self.sessions.begin() as session:
                    user = session.get(db.User, "owner")
                    for key, value in change.items():
                        setattr(user, key, value)
                for read in (lambda: self.store.overview("owner", "2026"), lambda: self.store.transactions("owner", "2026"),
                             lambda: self.store.transfers("owner", "2026")):
                    with self.assertRaises(BudgetAccessDenied) as denied:
                        read()
                    self.assertEqual(denied.exception.status, status)
                with self.sessions.begin() as session:
                    user = session.get(db.User, "owner")
                    user.role, user.disabled = "admin", False
        self.assertEqual(self.computed, 1)

    def test_cursors_and_rule_previews_stay_fenced_by_the_current_state(self):
        first = self.store.transactions("owner", "2026", limit=1)
        preview = self.store.preview_rule("owner", {"rule_id": None, "field": "description", "operator": "contains",
                                                    "value": "shop", "category_id": None, "kind": "excluded",
                                                    "priority": 100, "enabled": True, "expected_version": 0})
        self.reads()
        self.change_transaction("c", "shop", amount="13")
        self.reads()
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=first["next_cursor"]),
                            code="budget_analytics_stale_cursor", status=409)
        self.assert_invalid(lambda: self.store.save_rule("owner", preview["preview_id"]),
                            code="budget_analytics_stale_preview", status=409)

    def test_returned_rows_are_private_copies(self):
        cached = self.reads()
        row = cached["transactions"]["transactions"][0]
        row["annotation"]["mode"] = "tampered"
        row["remittance_information"].append("tampered")
        cached["suggested"]["transfers"][0]["debit"]["amount"] = "999"
        self.assertEqual(self.reads(), self.fresh())

    def test_failed_projection_is_never_memoised(self):
        self.reads()
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["c"], "shop")).payload_json = "[]"
        for _ in range(2):
            with self.assertRaises(AnalyticsError):
                self.store.overview("owner", "2026")
        with self.sessions.begin() as session:
            session.get(db.BudgetTransaction, (self.accounts["c"], "shop")).payload_json = json.dumps(
                {"amount": "1", "currency": "PLN", "credit_debit_indicator": "DBIT", "status": "BOOK",
                 "booking_date": "2026-11-30", "value_date": None, "transaction_date": None,
                 "remittance_information": ["Synthetic SHOP"]})
        self.assert_current()

    def test_concurrent_readers_of_one_state_share_one_projection(self):
        original = views.projection_snapshot

        def slow(session, owner, **kwargs):
            time.sleep(0.05)
            return original(session, owner, **kwargs)

        barrier, results, errors = threading.Barrier(3), {}, []

        def run(name, call):
            try:
                barrier.wait()
                results[name] = call()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        calls = {"overview": lambda: self.store.overview("owner", "2026-11"),
                 "transactions": lambda: self.store.transactions("owner", "2026-11"),
                 "transfers": lambda: self.store.transfers("owner", "2026-11")}
        with mock.patch.object(views, "projection_snapshot", side_effect=slow):
            threads = [threading.Thread(target=run, args=item) for item in calls.items()]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.computed, 1)
        views.clear_projection_cache()
        self.assertEqual(results, {name: call() for name, call in calls.items()})

    def test_memo_is_bounded_and_keeps_one_state_per_database_and_owner(self):
        self.reads()
        self.change_transaction("c", "shop", amount="2")
        self.reads()
        self.assertEqual(len(views._cache), 1)
        self.store.overview("other", "2026")
        self.assertEqual(len(views._cache), 2)
        with mock.patch.object(views, "CACHE_ENTRIES", 1):
            self.change_transaction("c", "shop", amount="3")
            self.reads()
            self.assertEqual([key[1] for key in views._cache], ["owner"])


class SnapshotAndClockTests(CacheCase):
    def test_digest_and_projection_come_from_one_read_snapshot(self):
        with self.store._read("owner") as session:
            before = views._source_digest(session, "owner")
            self.change_transaction("c", "shop", amount="99")  # another connection commits meanwhile
            self.assertEqual(views._source_digest(session, "owner"), before)
            amounts = {row["entry_reference"]: row["payload"]["amount"]
                       for row in views.projection_snapshot(session, "owner")["transactions"]}
            self.assertEqual(amounts["shop"], "12.30")
        self.assertNotEqual(self.digest(), before)
        self.assertEqual(self.output("shop")["amount"], "99")

    def test_clock_dependent_fields_are_evaluated_per_request(self):
        with self.sessions.begin() as session:
            for operation in ("transactions", "balances"):
                component = session.get(db.BudgetComponent, (self.accounts["a"], operation))
                if component is None:
                    component = db.BudgetComponent(account_id=self.accounts["a"], operation=operation)
                    session.add(component)
                component.last_attempt = component.last_success = self.now

        def freshness():
            rows = self.store.overview("owner", "2026")["scope"]["freshness"]
            value = next(row for row in rows if row["account_id"] == self.accounts["a"])
            return value["transactions"]["age_seconds"], value["transactions"]["stale"]

        self.assertEqual(freshness(), (0, False))
        self.now += 10 * 86400
        self.assertEqual(freshness(), (10 * 86400, True))
        with self.sessions.begin() as session:
            session.get(db.BudgetComponent, (self.accounts["a"], "transactions")).last_success = self.now
        self.assertEqual(freshness(), (0, False))
        first = self.store.transactions("owner", "2026", limit=1)
        self.now += views.CURSOR_TTL
        self.assert_invalid(lambda: self.store.transactions("owner", "2026", limit=1, cursor=first["next_cursor"]))
        # Acquisition state is read per request and never enters the projection key.
        self.assertEqual(self.computed, 1)

    def test_filters_and_pages_never_change_the_shared_projection(self):
        self.store.create_category("owner", "Synthetic Food")
        baseline = self.reads()
        for filters in ({"currency": "PLN"}, {"kind": "expense"}, {"category_id": "uncategorised"},
                        {"query": "shop"}, {"limit": 1}):
            self.store.transactions("owner", "2026", **filters)
            self.store.transfers("owner", "2026", **{key: value for key, value in filters.items()
                                                      if key in ("currency", "limit")})
        page = self.store.transactions("owner", "2026", limit=1)
        self.store.transactions("owner", "2026", limit=1, cursor=page["next_cursor"])
        self.store.overview("owner", "2026-12")
        self.assertEqual(self.reads(), baseline)
        self.assertEqual(baseline, self.fresh())

    def test_owners_never_share_a_projection(self):
        owner = self.store.overview("owner", "2026")
        other = self.store.overview("other", "2026")
        self.assertNotEqual(owner["snapshot_digest"], other["snapshot_digest"])
        self.assertEqual({key[1] for key in views._cache}, {"owner", "other"})
        views.clear_projection_cache()
        self.assertEqual(self.store.overview("other", "2026"), other)
        self.assertEqual(self.store.overview("owner", "2026"), owner)

    def test_writes_are_not_blocked_while_a_read_is_projecting(self):
        entered, release, results = threading.Event(), threading.Event(), {}
        original = views.projection_snapshot

        def held(session, owner, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(session, owner, **kwargs)

        def read():
            results["read"] = self.store.overview("owner", "2026")

        with mock.patch.object(views, "projection_snapshot", side_effect=held):
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(entered.wait(10))
            started = time.monotonic()
            category = self.store.create_category("owner", "Synthetic Food")
            self.annotation(account="c", reference="shop", category_id=category["id"])
            self.assertLess(time.monotonic() - started, 5)
            release.set()
            reader.join(10)
        self.assertFalse(reader.is_alive())
        def categories(result):
            return {row["category_id"] for row in result["categories"]}

        self.assertEqual(categories(results["read"]), {None})  # its own snapshot, taken before the writes
        self.assertEqual(categories(self.store.overview("owner", "2026")), {None, category["id"]})


if __name__ == "__main__":
    unittest.main()
