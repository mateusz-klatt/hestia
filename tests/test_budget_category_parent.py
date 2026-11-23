"""Category parent migration, store rules, and month-budget limit roll-up."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import db
from hestia.budget import month_budget
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.month_budget import MonthBudgetSettings, MonthDataError
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_month import _cat, _eval, _tx


class CategoryParentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "categories.db"

    def test_migration_0020_adds_parent_and_downgrade_keeps_categories(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_categories "
                    "(id, owner, name, name_key, archived, version, created_at, updated_at) "
                    "VALUES ('transport', 'alice', 'Transport', 'transport', 0, 1, 1, 1),"
                    "('fuel', 'alice', 'Fuel', 'fuel', 0, 1, 1, 1)")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0020")
            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("budget_categories")}
            self.assertIn("parent_id", columns)
            self.assertTrue(columns["parent_id"]["nullable"])
            self.assertIn("ix_budget_categories_parent_id", {idx["name"] for idx in inspector.get_indexes("budget_categories")})
            with engine.begin() as conn:
                conn.exec_driver_sql("UPDATE budget_categories SET parent_id='transport' WHERE id='fuel'")
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("UPDATE budget_categories SET parent_id=id WHERE id='fuel'")
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("UPDATE budget_categories SET parent_id='missing' WHERE id='fuel'")
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM budget_categories WHERE id='transport'")
            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0019")
            self.assertNotIn("parent_id", {column["name"] for column in inspect(engine).get_columns("budget_categories")})
            with engine.connect() as conn:
                names = {row[0] for row in conn.exec_driver_sql("SELECT name FROM budget_categories").all()}
            self.assertEqual(names, {"Transport", "Fuel"})
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")
            with engine.connect() as conn:
                self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])
        finally:
            engine.dispose()


class CategoryParentInitDbTests(unittest.TestCase):
    """Category parent through real ``init_db``. Head is now 0022, which also adds the annotation label and household grants.

    Production turns foreign keys on, and child rows reference categories. The runner
    upgrades to that head with foreign keys off inside one transaction, then checks them.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "categories.db"

    def _database_at_0019(self, *, orphan_rule=False, leftover_temp=False):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
                conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_categories "
                    "(id, owner, name, name_key, archived, version, created_at, updated_at) "
                    "VALUES ('cat', 'alice', 'Food', 'food', 0, 1, 1, 1)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_rules "
                    "(id, owner, field, operator, value, category_id, kind, priority, enabled, version, "
                    "created_at, updated_at) "
                    "VALUES ('rule', 'alice', 'memo', 'contains', 'shop', 'cat', 'expense', 1, 1, 1, 1, 1)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_category_limits "
                    "(id, owner, category_id, currency, monthly_limit, warning_percent, enabled, version, "
                    "created_at, updated_at) "
                    "VALUES ('lim', 'alice', 'cat', 'PLN', '10', 80, 1, 1, 1, 1)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_connections "
                    "(id, owner, application_id, bank_name, country, enabled, next_attempt, failures) "
                    "VALUES ('c1', 'alice', 'app', 'bank', 'PL', 1, 0, 0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_accounts "
                    "(id, connection_id, owner, identification_hash, enabled, balances_json) "
                    "VALUES ('a1', 'c1', 'alice', 'hash', 1, '{}')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_transactions "
                    "(account_id, entry_reference, payload_json, observed_at, uncertain) "
                    "VALUES ('a1', 'e1', '{}', 1, 0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_annotations "
                    "(account_id, entry_reference, owner, mode, category_id, kind, ledger_fingerprint, "
                    "version, updated_at) "
                    "VALUES ('a1', 'e1', 'alice', 'manual', 'cat', 'expense', 'fp', 1, 1)")
        finally:
            engine.dispose()
        if orphan_rule or leftover_temp:
            with closing(sqlite3.connect(self.path)) as raw:
                raw.execute("PRAGMA foreign_keys=OFF")
                if orphan_rule:
                    raw.execute(
                        "INSERT INTO budget_rules "
                        "(id, owner, field, operator, value, category_id, kind, priority, enabled, version, "
                        "created_at, updated_at) "
                        "VALUES ('orphan', 'alice', 'memo', 'contains', 'x', 'missing', 'expense', 1, 1, 1, 1, 1)")
                if leftover_temp:
                    raw.execute("CREATE TABLE _alembic_tmp_budget_categories (id TEXT)")
                raw.commit()

    def _snapshot(self):
        with closing(sqlite3.connect(self.path)) as raw:
            version = raw.execute("SELECT version_num FROM alembic_version").fetchall()
            columns = [row[1] for row in raw.execute("PRAGMA table_info(budget_categories)")]
            rules = raw.execute("SELECT id, category_id FROM budget_rules ORDER BY id").fetchall()
            limits = raw.execute("SELECT id, category_id FROM budget_category_limits ORDER BY id").fetchall()
            annotations = raw.execute(
                "SELECT account_id, entry_reference, category_id FROM budget_annotations ORDER BY account_id"
            ).fetchall()
            temp_tables = raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%alembic_tmp%'"
            ).fetchall()
            raw.execute("PRAGMA foreign_keys=ON")
            violations = raw.execute("PRAGMA foreign_key_check").fetchall()
        return version, columns, rules, limits, annotations, temp_tables, violations

    def _assert_integrity_and_no_batch_temp(self):
        """``PRAGMA integrity_check`` is ok and no ``_alembic_tmp_%`` table remains."""
        with closing(sqlite3.connect(self.path)) as raw:
            integrity = [row[0] for row in raw.execute("PRAGMA integrity_check")]
            leftovers = [row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'")]
        self.assertEqual(integrity, ["ok"])
        self.assertEqual(leftovers, [])

    def test_init_db_roundtrip_preserves_rows_that_reference_categories(self):
        self._database_at_0019()
        observed = []
        real_upgrade = command.upgrade

        def spy(config, revision):
            connection = config.attributes["connection"]
            raw = connection.connection.dbapi_connection
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                contender.isolation_level = None
                with self.assertRaises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
            observed.append((
                raw.in_transaction,
                raw.isolation_level,
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                revision,
            ))
            return real_upgrade(config, revision)

        with mock.patch("hestia.db.command.upgrade", spy):
            engine, _ = db.init_db(self.path)
        self.assertEqual(observed, [(True, None, 0, "head")])
        version, columns, rules, limits, annotations, temp_tables, violations = self._snapshot()
        self.assertEqual(version, [("0022",)])
        self.assertIn("parent_id", columns)
        self.assertEqual(rules, [("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertEqual(violations, [])
        self._assert_integrity_and_no_batch_temp()
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
            self.assertEqual(connection.exec_driver_sql("PRAGMA synchronous").scalar(), 2)
            self.assertEqual(connection.exec_driver_sql("PRAGMA busy_timeout").scalar(), 5000)
            self.assertEqual(connection.connection.dbapi_connection.isolation_level, "")

        real_downgrade = command.downgrade

        def spy_downgrade(config, revision):
            connection = config.attributes["connection"]
            raw = connection.connection.dbapi_connection
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                contender.isolation_level = None
                with self.assertRaises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
            self.assertTrue(raw.in_transaction)
            self.assertIsNone(raw.isolation_level)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 0)
            self.assertEqual(revision, "0019")
            return real_downgrade(config, revision)

        with mock.patch("hestia.db.command.downgrade", spy_downgrade):
            db.downgrade_db(self.path, "0019")
        self.assertNotIn(str(self.path), db._ENGINE_CACHE)
        version, columns, rules, limits, annotations, temp_tables, violations = self._snapshot()
        self.assertEqual(version, [("0019",)])
        self.assertNotIn("parent_id", columns)
        self.assertEqual(rules, [("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertEqual(violations, [])
        self._assert_integrity_and_no_batch_temp()

        with mock.patch("hestia.db.command.upgrade", spy):
            engine, _ = db.init_db(self.path)
        self.assertEqual(observed, [(True, None, 0, "head"), (True, None, 0, "head")])
        version, columns, rules, limits, annotations, temp_tables, violations = self._snapshot()
        self.assertEqual(version, [("0022",)])
        self.assertIn("parent_id", columns)
        self.assertEqual(rules, [("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertEqual(violations, [])
        self._assert_integrity_and_no_batch_temp()
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)

    def test_downgrade_db_opens_its_own_engine_when_uncached(self):
        self._database_at_0019()
        db.init_db(self.path)
        db.reset_engine_cache()
        self.assertNotIn(str(self.path), db._ENGINE_CACHE)
        db.downgrade_db(self.path, "0019")
        version, columns, rules, limits, annotations, temp_tables, violations = self._snapshot()
        self.assertEqual(version, [("0019",)])
        self.assertNotIn("parent_id", columns)
        self.assertEqual(rules, [("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertEqual(violations, [])
        self.assertNotIn(str(self.path), db._ENGINE_CACHE)
        self._assert_integrity_and_no_batch_temp()

    def test_downgrade_db_rolls_back_when_foreign_key_check_fails(self):
        self._database_at_0019()
        db.init_db(self.path)
        with closing(sqlite3.connect(self.path)) as raw:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute(
                "INSERT INTO budget_rules "
                "(id, owner, field, operator, value, category_id, kind, priority, enabled, version, "
                "created_at, updated_at) "
                "VALUES ('orphan', 'alice', 'memo', 'contains', 'x', 'missing', 'expense', 1, 1, 1, 1, 1)")
            raw.commit()
        with self.assertRaises(RuntimeError) as caught:
            db.downgrade_db(self.path, "0019")
        self.assertIn("foreign_key_check", str(caught.exception))
        version, columns, rules, limits, annotations, temp_tables, _violations = self._snapshot()
        self.assertEqual(version, [("0022",)])
        self.assertIn("parent_id", columns)
        self.assertEqual(rules, [("orphan", "missing"), ("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertNotIn(str(self.path), db._ENGINE_CACHE)

    def test_leftover_batch_temp_table_does_not_block_upgrade(self):
        self._database_at_0019(leftover_temp=True)
        with closing(sqlite3.connect(self.path)) as raw:
            leftovers = [row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'")]
        self.assertEqual(leftovers, ["_alembic_tmp_budget_categories"])
        db.init_db(self.path)
        version, columns, rules, limits, annotations, temp_tables, violations = self._snapshot()
        self.assertEqual(version, [("0022",)])
        self.assertIn("parent_id", columns)
        self.assertEqual(rules, [("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertEqual(violations, [])

    def test_foreign_key_violation_rolls_back_to_0019_without_partial_ddl(self):
        self._database_at_0019(orphan_rule=True)
        with self.assertRaises(RuntimeError) as caught:
            db.init_db(self.path)
        self.assertIn("foreign_key_check", str(caught.exception))
        version, columns, rules, limits, annotations, temp_tables, _violations = self._snapshot()
        self.assertEqual(version, [("0019",)])
        self.assertNotIn("parent_id", columns)
        self.assertEqual(rules, [("orphan", "missing"), ("rule", "cat")])
        self.assertEqual(limits, [("lim", "cat")])
        self.assertEqual(annotations, [("a1", "e1", "cat")])
        self.assertEqual(temp_tables, [])
        self.assertNotIn(str(self.path), db._ENGINE_CACHE)


class CategoryParentStoreTests(AnalyticsCase):
    def test_create_update_list_depth_and_idempotent_retry(self):
        transport = self.store.create_category("owner", "Transport")
        car = self.store.create_category("owner", "Car", transport["id"])
        fuel = self.store.create_category("owner", "Fuel", car["id"])
        self.assertEqual((car["parent_id"], fuel["parent_id"], fuel["changed"]), (transport["id"], car["id"], True))
        retry = self.store.create_category("owner", "Fuel", car["id"])
        self.assertEqual((retry["changed"], retry["parent_id"], retry["id"]), (False, car["id"], fuel["id"]))
        with self.assertRaises(AnalyticsError) as conflict:
            self.store.create_category("owner", "Fuel", None)
        self.assertEqual((conflict.exception.code, conflict.exception.status), ("budget_analytics_conflict", 409))
        self.assertEqual(next(row for row in self.store.categories("owner") if row["id"] == fuel["id"])["parent_id"], car["id"])
        self.assertIsNone(transport["parent_id"])
        listed = {row["name"]: row["parent_id"] for row in self.store.categories("owner")}
        self.assertEqual(listed, {"Car": transport["id"], "Fuel": car["id"], "Transport": None})
        same = self.store.update_category("owner", fuel["id"], name="Fuel", archived=False, expected_version=1,
                                           parent_id=car["id"])
        self.assertFalse(same["changed"])
        self.assertEqual(same["version"], 1)
        kept = self.store.update_category("owner", fuel["id"], name="Fuel renamed", archived=False, expected_version=1)
        self.assertEqual((kept["parent_id"], kept["version"]), (car["id"], 2))
        detached = self.store.update_category("owner", fuel["id"], name="Fuel renamed", archived=False,
                                              expected_version=2, parent_id=None)
        self.assertIsNone(detached["parent_id"])
        reattached = self.store.update_category("owner", fuel["id"], name="Fuel renamed", archived=False,
                                                expected_version=3, parent_id=car["id"])
        self.assertEqual((reattached["parent_id"], reattached["version"]), (car["id"], 4))
        with self.assertRaises(AnalyticsError):
            self.store.create_category("owner", "Too deep", fuel["id"])
        sport = self.store.create_category("owner", "Sport")
        hobby = self.store.create_category("owner", "Hobby", sport["id"])
        # A second child keeps subtree height at 1, so the move still fits under a root.
        wash = self.store.create_category("owner", "Wash", car["id"])
        self.assertEqual(wash["parent_id"], car["id"])
        moved = self.store.update_category("owner", car["id"], name="Car", archived=False, expected_version=1,
                                            parent_id=sport["id"])
        self.assertEqual(moved["parent_id"], sport["id"])
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", car["id"], name="Car", archived=False, expected_version=2,
                                       parent_id=hobby["id"])
        stayed = next(row for row in self.store.categories("owner") if row["id"] == car["id"])
        self.assertEqual(stayed["parent_id"], sport["id"])
        self.assertEqual(next(row["parent_id"] for row in self.store.categories("owner") if row["id"] == wash["id"]),
                         car["id"])
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", sport["id"], name="Sport", archived=True, expected_version=1)
        archived = self.store.update_category("owner", fuel["id"], name="Fuel renamed", archived=True,
                                              expected_version=4)
        self.assertTrue(archived["archived"])

    def test_parent_must_be_active_same_owner_and_acyclic(self):
        transport = self.store.create_category("owner", "Transport")
        car = self.store.create_category("owner", "Car", transport["id"])
        other = self.store.create_category("other", "Other")
        archived = self.store.create_category("owner", "Old")
        self.store.update_category("owner", archived["id"], name="Old", archived=True, expected_version=1)
        for parent in ("", 1, transport["id"]):
            with self.subTest(parent=parent), self.assertRaises(AnalyticsError) as denied:
                self.store.update_category("owner", transport["id"], name="Transport", archived=False,
                                           expected_version=1, parent_id=parent)
            self.assertEqual(denied.exception.status, 400)
        for parent in ("missing", other["id"]):
            with self.subTest(parent=parent), self.assertRaises(AnalyticsError) as missing:
                self.store.create_category("owner", f"Child {parent}", parent)
            self.assertEqual((missing.exception.status, missing.exception.code), (404, "budget_analytics_not_found"))
        with self.assertRaises(AnalyticsError) as inactive:
            self.store.create_category("owner", "Under old", archived["id"])
        self.assertEqual(inactive.exception.status, 400)
        with self.assertRaises(AnalyticsError) as cycle:
            self.store.update_category("owner", transport["id"], name="Transport", archived=False,
                                       expected_version=1, parent_id=car["id"])
        self.assertEqual(cycle.exception.status, 400)
        self.assertIsNone(next(row["parent_id"] for row in self.store.categories("owner")
                               if row["id"] == transport["id"]))

    def test_corrupt_links_are_rejected(self):
        transport = self.store.create_category("owner", "Transport")
        car = self.store.create_category("owner", "Car", transport["id"])
        fuel = self.store.create_category("owner", "Fuel", car["id"])
        other = self.store.create_category("other", "Other")
        self._force_parent(transport["id"], car["id"])
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", transport["id"], name="Transport", archived=False,
                                       expected_version=1, parent_id=other["id"])
        with self.assertRaises(AnalyticsError):
            self.store.create_category("owner", "On cycle", transport["id"])
        self._force_parent(transport["id"], None)
        self._force_parent(car["id"], transport["id"])
        self._force_parent(transport["id"], "missing-parent", foreign_keys=False)
        with self.assertRaises(AnalyticsError):
            self.store.create_category("owner", "Under broken", transport["id"])
        self._force_parent(transport["id"], None, foreign_keys=False)
        self._force_parent(transport["id"], other["id"])
        with self.assertRaises(AnalyticsError):
            self.store.create_category("owner", "Under foreign parent", transport["id"])
        self._force_parent(transport["id"], None)
        self._force_parent(car["id"], transport["id"])
        self._force_parent(fuel["id"], car["id"])
        extra = self.store.create_category("owner", "Extra")
        self._force_parent(extra["id"], fuel["id"])
        with self.assertRaises(AnalyticsError):
            self.store.create_category("owner", "Fifth", extra["id"])
        self._force_parent(transport["id"], car["id"])
        root = self.store.create_category("owner", "Root")
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", transport["id"], name="Transport", archived=False,
                                       expected_version=1, parent_id=root["id"])

    def _force_parent(self, category_id, parent_id, *, foreign_keys=True):
        with self.engine.connect() as conn:
            if not foreign_keys:
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                conn.exec_driver_sql("UPDATE budget_categories SET parent_id=? WHERE id=?", (parent_id, category_id))
                conn.commit()
            finally:
                if not foreign_keys:
                    conn.exec_driver_sql("PRAGMA foreign_keys=ON")


class MonthBudgetParentLimitTests(unittest.TestCase):
    def test_parent_limit_uses_subtree_amounts_and_direct_rows_stay_direct(self):
        categories = [
            _cat("salary", "Wynagrodzenie"),
            _cat("transport", "Transport"),
            {**_cat("fuel", "Paliwo"), "parent_id": "transport"},
        ]
        result = _eval(
            categories=categories,
            transactions=[_tx("acc-a", "fuel-1", amount="40", kind="expense", category_id="fuel", source="manual")],
            limits=[{"category_id": "transport", "currency": "PLN", "monthly_limit": "30",
                     "warning_percent": 80, "enabled": True}],
            settings=MonthBudgetSettings(income_category_ids=("salary",)),
        )
        by_id = {row["category_id"]: row for row in result["category_spend"]}
        self.assertEqual((by_id["transport"]["expenses"], by_id["transport"]["net_spending"]), ("0", "0"))
        self.assertEqual(by_id["transport"]["limit"]["status"], "exceeded")
        self.assertEqual(by_id["transport"]["limit"]["over_by"], "10")
        self.assertEqual(by_id["transport"]["limit"]["percent_used_floor"], "133")
        self.assertIsNone(by_id["fuel"]["limit"])
        self.assertEqual(by_id["fuel"]["expenses"], "40")

    def test_subtree_helper_sums_descendants_and_rejects_a_cycle(self):
        children = month_budget._category_children({
            "t": {"parent_id": None}, "c": {"parent_id": "t"}, "f": {"parent_id": "c"},
            "blank": {"parent_id": ""}, "plain": {},
        })
        self.assertEqual(children, {"t": ["c"], "c": ["f"]})
        direct = {"p": ("1", "0"), "z": ("0", "0"), "g": ("4", "0"), "r": ("0", "2")}
        tree = {"p": ["mid", "z", "r"], "mid": ["g"]}
        self.assertEqual(month_budget._subtree_limit_amounts("p", direct, tree), ("5", "2"))
        self.assertEqual(month_budget._subtree_limit_amounts("t", {"f": ("4", "1")}, children), ("4", "1"))
        self.assertIsNone(month_budget._subtree_limit_amounts("f", {"f": ("4", "1")}, children))
        with self.assertRaises(MonthDataError):
            month_budget._subtree_limit_amounts("a", {}, {"a": ["b"], "b": ["a"]})
