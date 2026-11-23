"""Synthetic shadow migration checks for the Gmail receipt schema."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import op
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import db


_RECEIPT = (
    "INSERT INTO budget_receipts (id, owner, status, source_kind, version, created_at, updated_at) "
    "VALUES ('synthetic', 'owner', 'draft', 'live_text', 1, 1, 1)"
)
_MESSAGE = (
    "INSERT INTO budget_gmail_messages (owner, gmail_id_hmac, internal_date, trust_path, outcome, receipt_id, created_at) "
    "VALUES (?, ?, 1, 'direct_allowlist', 'imported', ?, 1)"
)
_HMAC = "a" * 64


class ReceiptMigration0025Tests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "receipts.db"
        self.engine, _ = db.init_db(self.path)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES ('owner', 'synthetic', 'admin'), ('other', 'synthetic', 'admin')"
            )

    def _open(self):
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        return engine

    def _assert_clean(self, engine, revision):
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), revision)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").all(), [("ok",)])
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            self.assertEqual(connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'"
            ).all(), [])

    def _legacy_snapshot(self, engine):
        with engine.connect() as connection:
            return (
                connection.exec_driver_sql(
                    "SELECT id, owner, status, source_kind, version FROM budget_receipts"
                ).all(),
                connection.exec_driver_sql("SELECT * FROM budget_receipt_lines").all(),
                connection.exec_driver_sql("SELECT * FROM budget_household_members").all(),
                connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                    "AND tbl_name = 'budget_receipts' ORDER BY name"
                ).all(),
            )

    def test_receipt_and_household_rows_and_indexes_survive_roundtrip(self):
        db.downgrade_db(self.path, "0023")
        legacy_engine = self._open()
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql(_RECEIPT)
            connection.exec_driver_sql(
                "INSERT INTO budget_receipt_lines (receipt_id, line_no, owner, line_kind, raw_name, raw_name_key, "
                "quantity, gross_line_total, net_line_total) "
                "VALUES ('synthetic', 1, 'owner', 'item', 'Invoice item', 'invoice item', '1', '10', '10')"
            )
            connection.exec_driver_sql("UPDATE users SET role = 'viewer' WHERE username = 'other'")
            connection.exec_driver_sql(
                "INSERT INTO budget_household_members (member_username, owner, grant_id) "
                "VALUES ('other', 'owner', ?)", ("b" * 64,),
            )
        self._assert_clean(legacy_engine, "0023")
        original = self._legacy_snapshot(legacy_engine)

        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, "0025")
        self.assertEqual(self._legacy_snapshot(engine), original)
        with engine.begin() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT document_kind, import_expires_at, invoice_number, due_date FROM budget_receipts"
            ).one(), ("receipt", None, None, None))
            connection.exec_driver_sql("INSERT INTO budget_gmail_state (owner) VALUES ('owner')")
            connection.exec_driver_sql("INSERT INTO budget_gmail_state (owner, grant_status) VALUES ('other', 'revoked')")

        db.downgrade_db(self.path, "0023")
        rebuilt_legacy = self._open()
        self._assert_clean(rebuilt_legacy, "0023")
        self.assertEqual(self._legacy_snapshot(rebuilt_legacy), original)
        self.assertTrue({"budget_gmail_state", "budget_gmail_messages"}.isdisjoint(inspect(rebuilt_legacy).get_table_names()))
        columns = {column["name"] for column in inspect(rebuilt_legacy).get_columns("budget_receipts")}
        self.assertTrue({"document_kind", "import_expires_at", "invoice_number", "due_date"}.isdisjoint(columns))

        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, "0025")
        self.assertEqual(self._legacy_snapshot(engine), original)

    def test_aborted_upgrade_keeps_original_rows_and_retry_rebuilds(self):
        db.downgrade_db(self.path, "0023")
        legacy_engine = self._open()
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql(_RECEIPT)
        original = self._legacy_snapshot(legacy_engine)
        with patch.object(op, "create_table", side_effect=RuntimeError("injected upgrade failure")):
            with self.assertRaisesRegex(RuntimeError, "injected upgrade failure"):
                db.init_db(self.path)
        self._assert_clean(legacy_engine, "0023")
        self.assertEqual(self._legacy_snapshot(legacy_engine), original)
        with legacy_engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE _alembic_tmp_budget_receipts (id TEXT)")
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, "0025")
        self.assertEqual(self._legacy_snapshot(engine), original)

    def test_downgrade_preflight_blocks_each_kind_of_v1d_data(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(_RECEIPT)
        for clause in (
            "status = 'imported'", "source_kind = 'email'", "source_kind = 'attachment'", "source_kind = 'paste'",
            "source_kind = 'photo'", "document_kind = 'invoice'", "import_expires_at = 1", "invoice_number = 'INV-15'",
            "due_date = '2026-11-25'",
        ):
            with self.subTest(clause=clause):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(f"UPDATE budget_receipts SET {clause}")
                with self.assertRaisesRegex(RuntimeError, "1 incompatible receipts, 0 Gmail provenance rows and 0 active grants"):
                    db.downgrade_db(self.path, "0023")
                self._assert_clean(self.engine, "0025")
                with self.engine.begin() as connection:
                    connection.exec_driver_sql("DELETE FROM budget_receipts")
                    connection.exec_driver_sql(_RECEIPT)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO budget_gmail_state (owner, grant_status) VALUES ('owner', 'active')")
        with self.assertRaisesRegex(RuntimeError, "0 incompatible receipts, 0 Gmail provenance rows and 1 active grants"):
            db.downgrade_db(self.path, "0023")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE budget_gmail_state SET grant_status = 'revoked'")
            connection.exec_driver_sql(_MESSAGE, ("owner", _HMAC, None))
            connection.exec_driver_sql("UPDATE budget_gmail_messages SET outcome = 'deleted'")
        with self.assertRaisesRegex(RuntimeError, "0 incompatible receipts, 1 Gmail provenance rows and 0 active grants"):
            db.downgrade_db(self.path, "0023")
        self._assert_clean(self.engine, "0025")

    def test_owner_isolation_dedupe_and_default_disabled_grant(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("INSERT INTO budget_gmail_state (owner) VALUES ('owner')")
            self.assertEqual(connection.exec_driver_sql(
                "SELECT grant_status, grant_generation, sync_phase, next_sync_at, backoff_until, failures, version "
                "FROM budget_gmail_state"
            ).one(), ("disabled", 0, "idle", 0, 0, 0, 1))
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, raw_shop_name, raw_nip, "
                "invoice_number, receipt_date, due_date, total_amount, currency, import_expires_at, version, created_at, "
                "updated_at) VALUES ('synthetic', 'owner', 'imported', 'email', 'invoice', 'Synthetic Utility', "
                "'7791011327', 'FV/123', '2026-11-23', '2026-12-10', '10', 'PLN', 10, 1, 1, 1)"
            )
        invalid_updates = (
            ("UPDATE budget_receipts SET due_date = 'invalid'", ()),
            ("UPDATE budget_receipts SET document_kind = 'incorrect'", ()),
            ("UPDATE budget_receipts SET invoice_number = ?", ("x" * 129,)),
            ("UPDATE budget_gmail_state SET grant_generation = -1", ()),
            ("UPDATE budget_gmail_state SET last_attempt_at = -1", ()),
            ("UPDATE budget_gmail_state SET mailbox_hmac = ?", ("g" * 64,)),
            ("UPDATE budget_gmail_state SET scope_scan_page_token = ?", ("x" * 513,)),
            (_MESSAGE, ("other", _HMAC, "synthetic")),
            (_MESSAGE, ("owner", "not-a-digest", "synthetic")),
        )
        for statement, parameters in invalid_updates:
            with self.subTest(statement=statement, parameters=parameters):
                with self.assertRaises(IntegrityError), self.engine.begin() as connection:
                    connection.exec_driver_sql(statement, parameters)

        with self.engine.begin() as connection:
            connection.exec_driver_sql(_MESSAGE, ("owner", _HMAC, "synthetic"))
            connection.exec_driver_sql(_MESSAGE, ("other", _HMAC, None))
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.exec_driver_sql(_MESSAGE, ("owner", _HMAC, "synthetic"))
        with self.assertRaises(IntegrityError), self.engine.begin() as connection:
            connection.exec_driver_sql("DELETE FROM budget_receipts WHERE id = 'synthetic'")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("UPDATE budget_gmail_messages SET receipt_id = NULL, outcome = 'deleted'")
            connection.exec_driver_sql("DELETE FROM budget_receipts WHERE id = 'synthetic'")
        self._assert_clean(self.engine, "0025")
