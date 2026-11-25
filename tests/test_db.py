"""Unit tests for the SQLite persistence foundation (hestia.db) — Phase 1, inert scaffolding.

Exercises the engine PRAGMAs, the Alembic-managed schema (upgrade + downgrade roundtrip),
the session-scope transaction helper, and a roundtrip through every model.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
from sqlalchemy.exc import IntegrityError, OperationalError

from hestia import db

ALL_TABLES = {"app_meta", "nodes", "automations", "users", "user_settings", "audit"}



def _head_revision():
    cfg = Config()
    cfg.set_main_option("script_location", str(db._MIGRATIONS_DIR))
    (head,) = ScriptDirectory.from_config(cfg).get_heads()
    return head

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

    def test_foreign_keys_pragma_is_rejected_inside_a_transaction(self):
        # Production connections enable foreign keys on connect. SQLite ignores
        # PRAGMA foreign_keys inside an open transaction, which is why turning it
        # off around engine.begin() never let a batch DROP of a referenced table through.
        engine = db.make_engine(self.path)
        try:
            with engine.connect() as connection:
                raw = connection.connection.dbapi_connection
                raw.execute("BEGIN")
                try:
                    with self.assertRaises(RuntimeError) as caught:
                        db._set_sqlite_foreign_keys(connection, False)
                    self.assertIn("did not apply", str(caught.exception))
                    self.assertEqual(raw.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                finally:
                    raw.rollback()
        finally:
            engine.dispose()

    def test_make_engine_creates_parent_dir(self):
        nested = self.dir / "a" / "b" / "hestia.db"
        db.make_engine(nested)
        self.assertTrue(nested.parent.is_dir())

    def test_reserved_url_characters_are_literal_sqlite_filename_characters(self):
        for index, filename in enumerate(("calendar?mode=ro.db", "calendar#family.db",
                                          "calendar?uri=true&mode=ro#family.db")):
            with self.subTest(filename=filename):
                directory = self.dir / str(index)
                target = directory / filename
                engine, sessions = db.init_db(target)
                self.assertEqual(engine.url.database, str(target))
                self.assertEqual(dict(engine.url.query), {})
                with db.session_scope(sessions) as session:
                    session.add(db.AppMeta(key="filename", value=filename))
                with engine.connect() as connection:
                    databases = {row.name: row.file for row in connection.exec_driver_sql("PRAGMA database_list")}
                    self.assertEqual(Path(databases["main"]), target.resolve())
                db.reset_engine_cache()
                self.assertTrue(target.is_file())
                with closing(sqlite3.connect(target)) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT value FROM app_meta WHERE key='filename'").fetchone(), (filename,))
                self.assertEqual(set(directory.iterdir()), {target})

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

    def test_downgrade_db_rejects_none_empty_and_invalid_revisions(self):
        db.init_db(self.path)
        for bad_revision in (None, "", "   ", 123):
            with self.subTest(revision=bad_revision), self.assertRaises(ValueError):
                db.downgrade_db(self.path, bad_revision)
        self.assertIn(str(self.path.resolve()), db._ENGINE_CACHE)
        for bad_revision in ("nonexistent", "+1"):
            with self.subTest(revision=bad_revision), self.assertRaises(ValueError):
                db.downgrade_db(self.path, bad_revision)
        db.downgrade_db(self.path, "0019")
        with self.assertRaises(ValueError):
            db.downgrade_db(self.path, "0020")

    def test_downgrade_db_normalizes_path_key_with_init_db(self):
        subdir = self.dir / "sub"
        subdir.mkdir()
        unnormalized = subdir / ".." / "hestia.db"
        first_engine, _ = db.init_db(unnormalized)
        db.downgrade_db(str(self.path), "0019")
        self.assertEqual(db._ENGINE_CACHE, {})
        fresh_engine, _ = db.init_db(self.path)
        self.assertIsNot(fresh_engine, first_engine)

    def test_downgrade_db_requires_explicit_path_without_touching_default(self):
        env_target = self.dir / "env_target.db"
        with mock.patch.dict(os.environ, {"HESTIA_DB": str(env_target)}):
            env_engine, _ = db.init_db(None)
            for bad_path in (None, "", "   "):
                with self.subTest(path=bad_path), mock.patch.object(db, "_resolve_db_target") as resolve:
                    with self.assertRaisesRegex(ValueError, "explicit database path"):
                        db.downgrade_db(bad_path, "0019")
                    resolve.assert_not_called()
            self.assertIs(db.init_db(None)[0], env_engine)
            with env_engine.connect() as connection:
                self.assertEqual(connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version").scalar(), _head_revision())

    def test_commit_failure_rolls_back_and_restores_foreign_keys(self):
        setup_engine = db.make_engine(self.path)
        try:
            with setup_engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
                conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_categories "
                    "(id, owner, name, name_key, archived, version, created_at, updated_at) "
                    "VALUES ('cat', 'alice', 'Food', 'food', 0, 1, 1, 1)"
                )
        finally:
            setup_engine.dispose()

        fk_calls = []
        real_set_fk = db._set_sqlite_foreign_keys

        def spy_set_fk(connection, enabled):
            fk_calls.append(enabled)
            return real_set_fk(connection, enabled)

        with (
            mock.patch.object(db, "_set_sqlite_foreign_keys", spy_set_fk),
            mock.patch.object(
                SQLiteDialect_pysqlite,
                "do_commit",
                side_effect=sqlite3.OperationalError("simulated commit failure"),
            ),
        ):
            with self.assertRaisesRegex(OperationalError, "simulated commit failure"):
                db.init_db(self.path)

        self.assertEqual(fk_calls, [False, True])
        self.assertEqual(db._ENGINE_CACHE, {})

        fresh_engine = db.make_engine(self.path)
        try:
            with fresh_engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
                self.assertEqual(
                    conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall(),
                    [("0019",)],
                )
                columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(budget_categories)")]
                self.assertNotIn("parent_id", columns)
                temp_tables = conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%alembic_tmp%'"
                ).fetchall()
                self.assertEqual(temp_tables, [])
            with self.assertRaises(IntegrityError):
                with fresh_engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO budget_rules "
                        "(id, owner, field, operator, value, category_id, kind, priority, enabled, "
                        "version, created_at, updated_at) "
                        "VALUES ('r1', 'alice', 'memo', 'contains', 'x', 'missing', 'expense', 1, 1, 1, 1, 1)"
                    )
        finally:
            fresh_engine.dispose()

        rebuilt, _ = db.init_db(self.path)
        with rebuilt.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(
                conn.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall(),
                [(_head_revision(),)],
            )
            annotation_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(budget_annotations)")]
            category_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(budget_categories)")]
            self.assertIn("label", annotation_columns)
            self.assertIn("parent_id", category_columns)

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

    def test_migration_0017_constraints_and_downgrade_preserve_audit(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0016")
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('owner', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_categories (id, owner, name, name_key, archived, version, created_at, updated_at) "
                    "VALUES ('cat-1', 'owner', 'Food', 'food', 0, 1, 100.0, 100.0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_analytics_audit (owner, subject_type, subject_id, action, before_json, after_json, changed_at) "
                    "VALUES ('owner', 'category_limit', 'lim-1', 'create', 'null', '{}', 100.0)")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0017")
            self.assertIn("budget_category_limits", set(inspect(engine).get_table_names()))
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO budget_category_limits "
                    "(id, owner, category_id, currency, monthly_limit, warning_percent, enabled, version, created_at, updated_at) "
                    "VALUES ('lim-1', 'owner', 'cat-1', 'PLN', '500', 80, 1, 1, 100.0, 100.0)")
            bad_rows = (
                ("lim-dup", "owner", "cat-1", "PLN", "100", 80, 1, 1),
                ("lim-v0", "owner", "cat-1", "EUR", "100", 80, 1, 0),
                ("lim-w0", "owner", "cat-1", "EUR", "100", 0, 1, 1),
                ("lim-w100", "owner", "cat-1", "EUR", "100", 100, 1, 1),
                ("lim-en2", "owner", "cat-1", "EUR", "100", 80, 2, 1),
                ("lim-empty", "owner", "cat-1", "EUR", "", 80, 1, 1),
                ("lim-long", "owner", "cat-1", "EUR", "1" * 81, 80, 1, 1),
                ("lim-fk", "owner", "missing", "EUR", "100", 80, 1, 1),
                ("lim-owner-fk", "missing-user", "cat-1", "EUR", "100", 80, 1, 1),
            )
            for row in bad_rows:
                with self.subTest(row=row[0]), self.assertRaises(Exception):
                    with engine.begin() as conn:
                        conn.exec_driver_sql(
                            "INSERT INTO budget_category_limits "
                            "(id, owner, category_id, currency, monthly_limit, warning_percent, enabled, version, created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 100.0, 100.0)", row)
            # Restrictive FKs prevent deleting a referenced category or a referenced owner user.
            with self.assertRaises(Exception):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM budget_categories WHERE id='cat-1'")
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('u-lim', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_category_limits "
                    "(id, owner, category_id, currency, monthly_limit, warning_percent, enabled, version, created_at, updated_at) "
                    "VALUES ('lim-u', 'u-lim', 'cat-1', 'USD', '250', 80, 1, 1, 100.0, 100.0)")
            with self.assertRaises(Exception):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM users WHERE username='u-lim'")
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM budget_category_limits WHERE id='lim-u'")
                conn.exec_driver_sql("DELETE FROM users WHERE username='u-lim'")
            # Full roundtrip: 0016 -> 0017 -> 0016 -> 0017 preserves audit and recreates constraints cleanly.
            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0016")
            self.assertNotIn("budget_category_limits", set(inspect(engine).get_table_names()))
            with engine.connect() as conn:
                audit_count = conn.exec_driver_sql("SELECT COUNT(*) FROM budget_analytics_audit").scalar()
            self.assertEqual(audit_count, 1)
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0017")
            self.assertIn("budget_category_limits", set(inspect(engine).get_table_names()))
            with engine.begin() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_category_limits").scalar(), 0)
                self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_analytics_audit").scalar(), 1)
                conn.exec_driver_sql(
                    "INSERT INTO budget_category_limits "
                    "(id, owner, category_id, currency, monthly_limit, warning_percent, enabled, version, created_at, updated_at) "
                    "VALUES ('lim-2', 'owner', 'cat-1', 'PLN', '600', 85, 1, 1, 200.0, 200.0)")
            with self.assertRaises(Exception):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO budget_category_limits "
                        "(id, owner, category_id, currency, monthly_limit, warning_percent, enabled, version, created_at, updated_at) "
                        "VALUES ('lim-bad', 'owner', 'cat-1', 'EUR', '100', 80, 1, 0, 200.0, 200.0)")
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
        failure = ValueError("boom")

        def failed_transaction():
            with db.session_scope(self.Session) as s:
                s.add(db.AppMeta(key="x", value="y"))
                s.flush()                         # rollback must undo a write already sent to the DB
                raise failure

        with self.assertRaisesRegex(ValueError, "^boom$") as raised:
            failed_transaction()
        self.assertIs(raised.exception, failure)
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

    def test_0030_models_defaults_constraints_and_foreign_keys(self):
        with db.session_scope(self.Session) as s:
            s.add(db.User(username="owner", password_hash="h", role="admin"))
            s.add(db.User(username="delegate", password_hash="h", role="viewer"))
            s.flush()
            owner = s.get(db.User, "owner")
            self.assertFalse(owner.budget_owner)
            self.assertEqual(owner.credential_generation, 0)
            s.add(db.PermissionGrant(username="delegate", permission_id="ioc.view", target_owner=None))
            s.add(db.PermissionGrant(username="delegate", permission_id="household.view", target_owner="owner"))
            s.add(db.NativeLoginCode(
                id="code-1", username="delegate", code_hmac="a" * 64, generation=0,
                created_at=10.0, expires_at=20.0, consumed_at=None,
            ))
            s.add(db.BudgetReceipt(
                id="r-1", owner="owner", status="draft", source_kind="live_text",
                version=1, created_at=1.0, updated_at=1.0,
            ))
            s.add(db.BudgetReceiptDeviceToken(
                id="tok-1", owner="owner", device_name="Vesta", token_hmac="b" * 64,
                created_at=1.0, expires_at=2.0, hard_expires_at=3.0, sliding_written_at=1.0,
            ))
        with db.session_scope(self.Session) as s:
            receipt = s.get(db.BudgetReceipt, "r-1")
            token = s.get(db.BudgetReceiptDeviceToken, "tok-1")
            self.assertIsNone(receipt.submitted_by)
            self.assertIsNone(token.actor)
            self.assertIsNone(token.issued_permissions)

        bad_ops = (
            lambda s: s.add(db.PermissionGrant(username="delegate", permission_id="ioc.view", target_owner=None)),
            lambda s: s.add(db.PermissionGrant(username="delegate", permission_id="household.view", target_owner="owner")),
            lambda s: s.add(db.PermissionGrant(username="missing", permission_id="ioc.view", target_owner=None)),
            lambda s: s.add(db.PermissionGrant(username="delegate", permission_id="household.view", target_owner="missing")),
            lambda s: s.add(db.NativeLoginCode(
                id="code-dup", username="delegate", code_hmac="a" * 64, generation=0,
                created_at=10.0, expires_at=20.0,
            )),
            lambda s: s.add(db.NativeLoginCode(
                id="code-bad-hmac", username="delegate", code_hmac="Z" * 64, generation=0,
                created_at=10.0, expires_at=20.0,
            )),
            lambda s: s.add(db.NativeLoginCode(
                id="code-neg-gen", username="delegate", code_hmac="c" * 64, generation=-1,
                created_at=10.0, expires_at=20.0,
            )),
            lambda s: s.add(db.NativeLoginCode(
                id="code-bad-exp", username="delegate", code_hmac="d" * 64, generation=0,
                created_at=20.0, expires_at=20.0,
            )),
            lambda s: s.add(db.BudgetReceipt(
                id="r-bad-sub", owner="owner", submitted_by="missing", status="draft",
                source_kind="live_text", version=1, created_at=1.0, updated_at=1.0,
            )),
            lambda s: s.add(db.BudgetReceiptDeviceToken(
                id="tok-bad-actor", owner="owner", actor="missing", device_name="Vesta",
                token_hmac="e" * 64, created_at=1.0, expires_at=2.0, hard_expires_at=3.0, sliding_written_at=1.0,
            )),
        )
        for index, op in enumerate(bad_ops):
            with self.subTest(case=index), self.assertRaises(IntegrityError):
                with db.session_scope(self.Session) as s:
                    op(s)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
