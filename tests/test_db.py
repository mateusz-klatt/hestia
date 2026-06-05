"""Unit tests for the SQLite persistence foundation (hestia.db) — Phase 1, inert scaffolding.

Exercises the engine PRAGMAs, the Alembic-managed schema (upgrade + downgrade roundtrip),
the session-scope transaction helper, and a roundtrip through every model.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from hestia import db

ALL_TABLES = {"app_meta", "nodes", "automations", "users", "user_settings", "audit"}


class DbPathTests(unittest.TestCase):
    def test_default_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HESTIA_DB", None)
            self.assertEqual(db.db_path(), Path(db.DEFAULT_DB_PATH))

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"HESTIA_DB": "/tmp/whatever.db"}):
            self.assertEqual(db.db_path(), Path("/tmp/whatever.db"))


class DbSchemaTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "hestia.db"

    def tearDown(self):
        db.reset_engine_cache()   # dispose this test's cached engine(s) before deleting its files
        shutil.rmtree(self.dir)

    def test_init_db_caches_engine_per_path(self):
        first = db.init_db(self.path)
        second = db.init_db(self.path)   # cache hit: same engine + factory, Alembic NOT re-run
        self.assertIs(first[0], second[0])
        self.assertIs(first[1], second[1])

    def test_reset_engine_cache_disposes_and_rebuilds(self):
        engine, _ = db.init_db(self.path)
        db.reset_engine_cache()
        fresh, _ = db.init_db(self.path)
        self.assertIsNot(engine, fresh)   # a brand-new engine after a reset

    def test_init_db_creates_all_tables(self):
        engine, _ = db.init_db(self.path)
        names = set(inspect(engine).get_table_names())
        self.assertLessEqual(ALL_TABLES, names, names)
        self.assertIn("alembic_version", names)

    def test_init_db_is_idempotent(self):
        db.init_db(self.path)
        engine, _ = db.init_db(self.path)  # second run upgrades to a no-op
        self.assertLessEqual(ALL_TABLES, set(inspect(engine).get_table_names()))

    def test_init_db_uses_env_path_when_none(self):
        target = self.dir / "from_env.db"
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(target)}):
            engine, _ = db.init_db()  # path=None -> db_path() -> env
        self.assertTrue(target.exists())
        self.assertLessEqual(ALL_TABLES, set(inspect(engine).get_table_names()))

    def test_pragmas_applied_on_connect(self):
        engine, _ = db.init_db(self.path)
        with engine.connect() as c:
            self.assertEqual(c.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
            self.assertEqual(c.exec_driver_sql("PRAGMA synchronous").scalar(), 2)  # FULL
            self.assertEqual(c.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(c.exec_driver_sql("PRAGMA busy_timeout").scalar(), 5000)

    def test_make_engine_creates_parent_dir(self):
        nested = self.dir / "a" / "b" / "hestia.db"
        db.make_engine(nested)
        self.assertTrue(nested.parent.is_dir())

    def test_models_match_migration_no_drift(self):
        # Lock model↔migration parity into CI: a model change without a matching migration
        # (or a migration that drifts from the models) makes this fail, not just manual review.
        engine, _ = db.init_db(self.path)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
        self.assertEqual(diff, [], f"model/migration drift: {diff}")

    def test_downgrade_roundtrip_drops_every_table(self):
        engine, _ = db.init_db(self.path)
        with engine.begin() as conn:
            command.downgrade(db._alembic_config(conn), "base")
        names = set(inspect(engine).get_table_names())
        self.assertEqual(ALL_TABLES & names, set(), names)

    def test_migration_0002_backfills_existing_users_to_admin(self):
        # The #73 guarantee: accounts that existed before roles must come out of the migration as admin
        # (no operator lockout), while the column default for any new row is the least-privilege viewer.
        engine = db.make_engine(self.path)   # not init_db: we step the revision manually (0001 → head)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0001")           # schema BEFORE the role column
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO users (username, password_hash) VALUES ('legacy', 'h')")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")           # 0002 adds role + backfills
            with engine.connect() as conn:
                legacy = conn.exec_driver_sql("SELECT role FROM users WHERE username='legacy'").scalar()
                fresh = conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash) VALUES ('fresh', 'h')").rowcount
                new_role = conn.exec_driver_sql("SELECT role FROM users WHERE username='fresh'").scalar()
            self.assertEqual(legacy, "admin")        # pre-existing → admin (operator keeps full access)
            self.assertEqual(fresh, 1)
            self.assertEqual(new_role, "viewer")     # a row inserted without a role defaults to viewer
        finally:
            engine.dispose()

    def test_migration_0003_keeps_existing_users_enabled(self):
        # The PR-D guarantee: adding `disabled` must not lock anyone out — every pre-existing account, and
        # every new row, comes out ENABLED (disabled = 0).
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0002")              # schema BEFORE the disabled column
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash, role) VALUES ('legacy', 'h', 'admin')")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")              # 0003 adds disabled
            with engine.connect() as conn:
                legacy = conn.exec_driver_sql("SELECT disabled FROM users WHERE username='legacy'").scalar()
                conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash, role) VALUES ('fresh', 'h', 'viewer')")
                fresh = conn.exec_driver_sql("SELECT disabled FROM users WHERE username='fresh'").scalar()
            self.assertEqual(legacy, 0)              # pre-existing account stays enabled (no migration lockout)
            self.assertEqual(fresh, 0)               # a new row defaults to enabled
        finally:
            engine.dispose()


class SessionScopeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        _, self.Session = db.init_db(self.dir / "hestia.db")

    def tearDown(self):
        db.reset_engine_cache()
        shutil.rmtree(self.dir)

    def test_commits_on_success(self):
        with db.session_scope(self.Session) as s:
            s.add(db.AppMeta(key="mode", value="standalone"))
        with db.session_scope(self.Session) as s:
            self.assertEqual(s.get(db.AppMeta, "mode").value, "standalone")

    def test_rolls_back_on_error(self):
        with self.assertRaises(ValueError):
            with db.session_scope(self.Session) as s:
                s.add(db.AppMeta(key="x", value="y"))
                raise ValueError("boom")
        with db.session_scope(self.Session) as s:
            self.assertIsNone(s.get(db.AppMeta, "x"))

    def test_every_model_roundtrips(self):
        with db.session_scope(self.Session) as s:
            s.add(db.User(username="tata", password_hash="scrypt$x"))          # role omitted → default
            s.add(db.User(username="mama", password_hash="scrypt$y", role="operator"))
            s.flush()  # satisfy the user_settings FK before inserting the setting
            s.add(db.UserSetting(username="tata", locale="pl", temp_scale="C", theme=None))
            s.add(db.Node(key="5", entry_json='{"type": "blind"}'))
            s.add(db.Automation(id="r1", position=0, rule_json="{}"))
            s.add(db.Audit(ts=1.5, actor="tata", action="control", target="5", detail=None, result="ok"))
        with db.session_scope(self.Session) as s:
            self.assertEqual(s.get(db.User, "tata").password_hash, "scrypt$x")
            self.assertEqual(s.get(db.User, "tata").role, "viewer")        # column default (least privilege)
            self.assertEqual(s.get(db.User, "mama").role, "operator")      # explicit role roundtrips
            setting = s.get(db.UserSetting, "tata")
            self.assertEqual((setting.locale, setting.temp_scale, setting.theme), ("pl", "C", None))
            self.assertEqual(s.get(db.Node, "5").entry_json, '{"type": "blind"}')
            rule = s.get(db.Automation, "r1")
            self.assertEqual((rule.position, rule.rule_json), (0, "{}"))
            audit = s.get(db.Audit, 1)
            self.assertEqual(
                (audit.ts, audit.actor, audit.action, audit.target, audit.detail, audit.result),
                (1.5, "tata", "control", "5", None, "ok"),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
