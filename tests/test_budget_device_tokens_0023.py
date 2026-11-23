"""Migration 0023 device-token schema. HMAC columns only, owner foreign keys, reversible."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import db

_TABLES = {"budget_receipt_device_tokens", "budget_receipt_pair_codes", "budget_receipt_pair_locks"}
_HEX = "ab" * 32


class DeviceTokenMigration0023Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "device-tokens-0023.db"

    def _revision(self, engine):
        with engine.connect() as conn:
            return conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()

    def _open(self):
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        return engine

    def _assert_clean(self, engine):
        with engine.connect() as conn:
            integrity = conn.exec_driver_sql("PRAGMA integrity_check").all()
            self.assertEqual([tuple(row) for row in integrity], [("ok",)])
            leftovers = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'"
            ).all()
            self.assertEqual(list(leftovers), [])

    def _columns(self, engine, table):
        return {column["name"] for column in inspect(engine).get_columns(table)}

    def test_upgrade_downgrade_and_metadata_match(self):
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0023")
        self.assertTrue(_TABLES <= set(inspect(engine).get_table_names()))
        self.assertFalse({"token", "code", "token_hash", "code_hash"} & self._columns(engine, "budget_receipt_device_tokens"))
        self.assertIn("token_hmac", self._columns(engine, "budget_receipt_device_tokens"))
        self.assertIn("code_hmac", self._columns(engine, "budget_receipt_pair_codes"))
        self.assertFalse({"code", "code_hash"} & self._columns(engine, "budget_receipt_pair_codes"))
        with engine.connect() as conn:
            fks = conn.exec_driver_sql("PRAGMA foreign_key_list(budget_receipt_device_tokens)").all()
            code_fks = conn.exec_driver_sql("PRAGMA foreign_key_list(budget_receipt_pair_codes)").all()
            lock_fks = conn.exec_driver_sql("PRAGMA foreign_key_list(budget_receipt_pair_locks)").all()
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.Base.metadata), [])
        self.assertTrue(fks and code_fks and lock_fks)
        self.assertTrue(all(row[2] == "users" for row in (*fks, *code_fks, *lock_fks)))
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('owner', 'h', 'admin')")
            conn.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, version, created_at, updated_at) "
                "VALUES ('11111111-1111-4111-8111-111111111111', 'owner', 'draft', 'live_text', 1, 1.0, 1.0)")

        def rejected(statement, parameters=()):
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(statement, parameters)

        rejected(
            "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
            "expires_at, hard_expires_at, sliding_written_at) VALUES ('t', 'missing', 'Vesta', ?, 1, 2, 3, 1)",
            (_HEX,))
        rejected(
            "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
            "expires_at, hard_expires_at, sliding_written_at) VALUES ('t', 'owner', '', ?, 1, 2, 3, 1)",
            (_HEX,))
        rejected(
            "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
            "expires_at, hard_expires_at, sliding_written_at) VALUES ('t', 'owner', ?, ?, 1, 2, 3, 1)",
            ("n" * 65, _HEX))
        rejected(
            "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
            "expires_at, hard_expires_at, sliding_written_at) VALUES ('t', 'owner', 'Vesta', ?, 1, 2, 3, 1)",
            ("zz" * 32,))
        rejected(
            "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
            "expires_at, hard_expires_at, sliding_written_at) VALUES ('t', 'owner', 'Vesta', ?, 1, 4, 3, 1)",
            (_HEX,))
        rejected(
            "INSERT INTO budget_receipt_pair_locks (owner, failures, locked) VALUES ('owner', -1, 0)")
        rejected(
            "INSERT INTO budget_receipt_pair_locks (owner, failures, locked) VALUES ('owner', 0, 2)")
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
                "expires_at, hard_expires_at, sliding_written_at) VALUES ('t', 'owner', 'Vesta', ?, 1, 2, 3, 1)",
                (_HEX,))
        db.downgrade_db(self.path, "0022")
        engine = self._open()
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0022")
        self.assertFalse(_TABLES & set(inspect(engine).get_table_names()))
        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT id FROM budget_receipts").scalar(),
                             "11111111-1111-4111-8111-111111111111")
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0023")
        with engine.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.Base.metadata), [])
