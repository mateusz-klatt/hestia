"""Alembic 0033 migration tests: card_last4 and blik_ref_hmac columns & constraints."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy.exc import IntegrityError

from hestia import db


class Receipt0033MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.db_path = Path(self.directory.name) / "test.db"

    def test_upgrade_roundtrip_and_check_constraints(self):
        engine = db.make_engine(self.db_path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0032")
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipts (id, owner, status, source_kind, version, created_at, updated_at) "
                    "VALUES ('r0', 'alice', 'imported', 'email', 1, 1.0, 1.0)"
                )
        finally:
            engine.dispose()

        engine, sessions = db.init_db(self.db_path)
        try:
            with sessions() as session:
                row = session.get(db.BudgetReceipt, "r0")
                self.assertIsNone(row.card_last4)
                self.assertIsNone(row.blik_ref_hmac)

            valid_hmac = "a" * 64
            with sessions.begin() as session:
                session.add(db.BudgetReceipt(
                    id="r1", owner="alice", status="imported", source_kind="email",
                    card_last4="1234", blik_ref_hmac=valid_hmac, version=1, created_at=2.0, updated_at=2.0,
                ))

            bad_updates = [
                ("UPDATE budget_receipts SET card_last4 = '123' WHERE id = 'r1'", ()),
                ("UPDATE budget_receipts SET card_last4 = '12345' WHERE id = 'r1'", ()),
                ("UPDATE budget_receipts SET card_last4 = '12ab' WHERE id = 'r1'", ()),
                ("UPDATE budget_receipts SET blik_ref_hmac = 'not-hex' WHERE id = 'r1'", ()),
                ("UPDATE budget_receipts SET blik_ref_hmac = ? WHERE id = 'r1'", ("A" * 64,)),
            ]
            for sql, params in bad_updates:
                with self.subTest(sql=sql):
                    with self.assertRaises(IntegrityError):
                        with sessions.begin() as session:
                            session.connection().exec_driver_sql(sql, params)
        finally:
            engine.dispose()

        db.downgrade_db(self.db_path, "0032")
        with sqlite3.connect(self.db_path) as raw:
            cols = {row[1] for row in raw.execute("PRAGMA table_info(budget_receipts)").fetchall()}
        self.assertNotIn("card_last4", cols)
        self.assertNotIn("blik_ref_hmac", cols)

        engine, _ = db.init_db(self.db_path)
        try:
            with sqlite3.connect(self.db_path) as raw:
                cols_after = {row[1] for row in raw.execute("PRAGMA table_info(budget_receipts)").fetchall()}
            self.assertIn("card_last4", cols_after)
            self.assertIn("blik_ref_hmac", cols_after)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
