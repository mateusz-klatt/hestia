"""Persistent root-category hues, migration spacing, and explicit re-spreading."""
from __future__ import annotations

from contextlib import closing
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest

from hestia import db
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_store import _even_hues, _largest_gap_hue
from tests.test_budget_analytics_store import AnalyticsCase


def _distance(left, right):
    direct = abs(left - right) % 360
    return min(direct, 360 - direct)


def _minimum_distance(hues):
    return min((_distance(left, right) for index, left in enumerate(hues) for right in hues[index + 1:]),
               default=360)


class CategoryHueMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "hues.db"

    def _clean(self, revision):
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%alembic_tmp%'").fetchall(), [])
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone(), (revision,))

    def test_backfill_is_per_owner_ordered_spaced_and_downgrade_clean(self):
        db.init_db(self.path)
        db.downgrade_db(self.path, "0025")
        with closing(sqlite3.connect(self.path)) as connection:
            for owner in ("alice", "bob"):
                connection.execute("INSERT INTO users (username, password_hash, role, disabled) VALUES (?, 'hash', 'admin', 0)",
                                   (owner,))
            for owner, count in (("alice", 7), ("bob", 3)):
                for index in reversed(range(count)):
                    name = f"Root {index:02d}"
                    connection.execute(
                        "INSERT INTO budget_categories "
                        "(id, owner, name, name_key, archived, version, created_at, updated_at, parent_id) "
                        "VALUES (?, ?, ?, ?, 0, 1, 1, 1, NULL)",
                        (f"{owner}-{index}", owner, name, name.lower()))
            connection.execute(
                "INSERT INTO budget_categories "
                "(id, owner, name, name_key, archived, version, created_at, updated_at, parent_id) "
                "VALUES ('child', 'alice', 'Child', 'child', 0, 1, 1, 1, 'alice-0')")
            connection.execute(
                "INSERT INTO budget_categories "
                "(id, owner, name, name_key, archived, version, created_at, updated_at, parent_id) "
                "VALUES ('archived', 'alice', 'Archived', 'archived', 1, 1, 1, 1, NULL)")
            connection.execute('CREATE TABLE "_alembic_tmp_budget_categories" (stale TEXT)')
            connection.commit()

        for upgrade in (1, 2):
            with self.subTest(upgrade=upgrade):
                db.init_db(self.path)
                self._clean("0027")
                with closing(sqlite3.connect(self.path)) as connection:
                    for owner, count in (("alice", 7), ("bob", 3)):
                        rows = connection.execute(
                            "SELECT name_key, color_hue FROM budget_categories "
                            "WHERE owner = ? AND parent_id IS NULL AND archived = 0 ORDER BY name_key, id",
                            (owner,)).fetchall()
                        hues = [hue for _name, hue in rows]
                        self.assertEqual(hues, [round(index * 360 / count + 20) % 360 for index in range(count)])
                        self.assertGreaterEqual(_minimum_distance(hues), math.floor(360 / count) - 1)
                    self.assertEqual(connection.execute(
                        "SELECT color_hue FROM budget_categories WHERE id IN ('child', 'archived')").fetchall(),
                        [(None,), (None,)])
                db.downgrade_db(self.path, "0025")
                self._clean("0025")
                with closing(sqlite3.connect(self.path)) as connection:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(budget_categories)")}
                    self.assertNotIn("color_hue", columns)


class CategoryHueStoreTests(AnalyticsCase):
    def test_even_spacing_and_deterministic_largest_gap(self):
        for count in range(1, 101):
            with self.subTest(count=count):
                hues = _even_hues(count)
                self.assertEqual(len(hues), count)
                self.assertTrue(all(type(hue) is int and 0 <= hue <= 359 for hue in hues))
                self.assertGreaterEqual(_minimum_distance(hues), math.floor(360 / count) - 1)
        self.assertEqual(_even_hues(0), [])
        self.assertEqual(_largest_gap_hue([]), 20)
        self.assertEqual(_largest_gap_hue([20]), 200)
        self.assertEqual(_largest_gap_hue([20, 200]), 110)
        self.assertEqual(_largest_gap_hue([200, 20, 200]), 110)
        self.assertEqual(_largest_gap_hue([0, 90, 180, 270]), 45)
        self.assertEqual(_largest_gap_hue([10, 11]), 190)

    def test_new_roots_use_gaps_without_recolouring_existing_roots(self):
        first = self.store.create_category("owner", "First")
        second = self.store.create_category("owner", "Second")
        third = self.store.create_category("owner", "Third")
        fourth = self.store.create_category("owner", "Fourth")
        self.assertEqual([row["color_hue"] for row in (first, second, third, fourth)], [20, 200, 110, 290])
        self.assertEqual([(row["name"], row["color_hue"], row["version"]) for row in self.store.categories("owner")],
                         [("First", 20, 1), ("Fourth", 290, 1), ("Second", 200, 1), ("Third", 110, 1)])
        other = self.store.create_category("other", "Other owner")
        self.assertEqual(other["color_hue"], 20)
        child = self.store.create_category("owner", "Child", parent_id=first["id"])
        self.assertIsNone(child["color_hue"])
        retry = self.store.create_category("owner", "First")
        self.assertEqual((retry["color_hue"], retry["changed"]), (20, False))
        archived = self.store.update_category("owner", fourth["id"], name="Fourth", archived=True, expected_version=1)
        self.assertEqual(archived["color_hue"], 290)

    def test_override_clear_validation_and_non_root_rejection(self):
        first = self.store.create_category("owner", "First")
        second = self.store.create_category("owner", "Second")
        child = self.store.create_category("owner", "Child", parent_id=first["id"])
        changed = self.store.update_category("owner", first["id"], name="First", archived=False,
                                             expected_version=1, color_hue=300)
        self.assertEqual((changed["color_hue"], changed["version"]), (300, 2))
        cleared = self.store.update_category("owner", first["id"], name="First", archived=False,
                                             expected_version=2, color_hue=None)
        self.assertEqual((cleared["color_hue"], cleared["version"]), (20, 3))
        self.assertEqual(second["color_hue"], 200)
        for value in (None, 10):
            with self.subTest(child_hue=value), self.assertRaises(AnalyticsError) as rejected:
                self.store.update_category("owner", child["id"], name="Child", archived=False,
                                           expected_version=1, color_hue=value)
            self.assertEqual(rejected.exception.status, 400)
        for value in (True, -1, 360, 1.5, "10"):
            with self.subTest(invalid=value), self.assertRaises(AnalyticsError):
                self.store.update_category("owner", first["id"], name="First", archived=False,
                                           expected_version=3, color_hue=value)
        self.assertEqual(self.store.update_category("owner", first["id"], name="First", archived=False,
                                                    expected_version=3, color_hue=20)["changed"], False)

    def test_parent_moves_assign_or_clear_and_restoring_null_root_assigns(self):
        root = self.store.create_category("owner", "Root")
        other = self.store.create_category("owner", "Other")
        child = self.store.create_category("owner", "Child", parent_id=other["id"])
        promoted = self.store.update_category("owner", child["id"], name="Child", archived=False,
                                              expected_version=1, parent_id=None)
        self.assertEqual(promoted["color_hue"], 110)
        demoted = self.store.update_category("owner", root["id"], name="Root", archived=False,
                                             expected_version=1, parent_id=other["id"])
        self.assertIsNone(demoted["color_hue"])
        with self.assertRaises(AnalyticsError):
            self.store.update_category("owner", promoted["id"], name="Child", archived=False,
                                       expected_version=2, parent_id=other["id"], color_hue=40)
        with self.sessions.begin() as session:
            stored = session.get(db.BudgetCategory, other["id"])
            stored.archived = True
            stored.color_hue = None
        restored = self.store.update_category("owner", other["id"], name="Other", archived=False,
                                              expected_version=1)
        self.assertEqual(restored["color_hue"], 290)

    def test_explicit_respread_current_order_is_repeatable_and_active_only(self):
        for name in ("Delta", "Alpha", "Charlie", "Bravo"):
            self.store.create_category("owner", name)
        archived = self.store.create_category("owner", "Archived")
        self.store.update_category("owner", archived["id"], name="Archived", archived=True, expected_version=1)
        child_parent = next(row for row in self.store.categories("owner") if row["name"] == "Alpha")
        child = self.store.create_category("owner", "Child", parent_id=child_parent["id"])
        before_children = {row["id"]: row for row in self.store.categories("owner") if row["parent_id"] is not None}

        spread = self.store.spread_category_colors("owner")
        self.assertEqual([(row["name"], row["color_hue"]) for row in spread],
                         [("Alpha", 20), ("Bravo", 110), ("Charlie", 200), ("Delta", 290)])
        self.assertTrue(all(row["version"] == 2 for row in spread))
        current = {row["id"]: row for row in self.store.categories("owner")}
        self.assertEqual(current[archived["id"]]["color_hue"], 65)
        self.assertEqual(current[child["id"]]["color_hue"], before_children[child["id"]]["color_hue"])
        versions = {row["id"]: row["version"] for row in spread}
        audits = len(self.rows(db.BudgetAnalyticsAudit))
        repeated = self.store.spread_category_colors("owner")
        self.assertEqual([row["color_hue"] for row in repeated], [20, 110, 200, 290])
        self.assertEqual({row["id"]: row["version"] for row in repeated}, versions)
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), audits)
        self.assertEqual(self.store.spread_category_colors("other"), [])
