"""Upgrade, downgrade and trigger checks for migration 0032 (evidence self-reference guard)."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy.exc import IntegrityError

from hestia import db
from tests.migration_head import HEAD


class ReceiptEvidenceSelfReferenceMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "evidence-self-ref.db"

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

    def test_0032_rejects_self_reference_and_roundtrips_with_0031_triggers(self):
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, HEAD)
        insert = (
            "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, "
            "version, created_at, updated_at, evidence_for_receipt_id) "
            "VALUES (?, ?, 'imported', 'email', 'payment_confirmation', 1, 1, 1, ?)"
        )
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (username, password_hash, role) "
                "VALUES ('owner', 'synthetic', 'admin'), ('other', 'synthetic', 'admin')")
            connection.exec_driver_sql(insert, [("doc", "owner", None), ("pay", "owner", "doc"),
                                                 ("other-pay", "other", None)])

        # Self-reference is rejected on both INSERT and UPDATE at 0032.
        for statement, values in (
            (insert, ("self-row", "owner", "self-row")),
            ("UPDATE budget_receipts SET evidence_for_receipt_id = id WHERE id = 'doc'", ()),
            ("UPDATE budget_receipts SET evidence_for_receipt_id = 'pay' WHERE id = 'pay'", ()),
        ):
            with self.subTest(statement=statement), engine.begin() as connection:
                with self.assertRaisesRegex(IntegrityError, "invalid receipt evidence self-reference"):
                    connection.exec_driver_sql(statement, values)

        # Cross-owner integrity and ON DELETE SET NULL still hold at 0032.
        for statement, values in (
            (insert, ("foreign-pay", "other", "doc")),
            ("UPDATE budget_receipts SET evidence_for_receipt_id = 'doc' WHERE id = 'other-pay'", ()),
            ("UPDATE budget_receipts SET owner = 'other' WHERE id = 'doc'", ()),
        ):
            with self.subTest(statement=statement), engine.begin() as connection:
                with self.assertRaisesRegex(IntegrityError, "invalid receipt evidence owner"):
                    connection.exec_driver_sql(statement, values)

        # Downgrade to 0031 restores the 0031 triggers (same-owner still enforced; self-ref update allowed at 0031).
        db.downgrade_db(self.path, "0031")
        at_0031 = self._open()
        self._assert_clean(at_0031, "0031")
        with at_0031.begin() as connection:
            with self.assertRaisesRegex(IntegrityError, "invalid receipt evidence owner"):
                connection.exec_driver_sql(
                    "UPDATE budget_receipts SET evidence_for_receipt_id = 'doc' WHERE id = 'other-pay'")
        with at_0031.begin() as connection:
            connection.exec_driver_sql("UPDATE budget_receipts SET evidence_for_receipt_id = 'doc' WHERE id = 'doc'")
            connection.exec_driver_sql("UPDATE budget_receipts SET evidence_for_receipt_id = NULL WHERE id = 'doc'")

        # Re-upgrade 0031 -> 0032 restores the self-reference guard and keeps ON DELETE SET NULL intact.
        reupgraded, _ = db.init_db(self.path)
        self._assert_clean(reupgraded, HEAD)
        with reupgraded.begin() as connection:
            with self.assertRaisesRegex(IntegrityError, "invalid receipt evidence self-reference"):
                connection.exec_driver_sql("UPDATE budget_receipts SET evidence_for_receipt_id = id WHERE id = 'doc'")
        with reupgraded.begin() as connection:
            connection.exec_driver_sql("DELETE FROM budget_receipts WHERE id = 'doc'")
        with reupgraded.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT owner, evidence_for_receipt_id FROM budget_receipts WHERE id = 'pay'").one(),
                ("owner", None))

    def test_0032_downgrade_is_trigger_safe_when_triggers_were_absent(self):
        engine, _ = db.init_db(self.path)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER budget_receipts_evidence_owner_insert")
            connection.exec_driver_sql("DROP TRIGGER budget_receipts_evidence_owner_update")
        db.downgrade_db(self.path, "0031")
        at_0031 = self._open()
        self._assert_clean(at_0031, "0031")
        with at_0031.connect() as connection:
            triggers = {row[0] for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'budget_receipts'"
            ).all()}
            self.assertEqual(triggers, {
                "budget_receipts_evidence_owner_insert",
                "budget_receipts_evidence_owner_update",
            })
        db.downgrade_db(self.path, "0030")
        self._assert_clean(self._open(), "0030")
        reupgraded, _ = db.init_db(self.path)
        self._assert_clean(reupgraded, HEAD)
