"""Upgrade, downgrade and re-upgrade for the gateway payment evidence columns."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import db
from tests.migration_head import HEAD


class GatewayEvidenceMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "gateway-evidence.db"

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

    def test_populated_0030_upgrade_downgrade_and_reupgrade(self):
        db.init_db(self.path)
        db.downgrade_db(self.path, "0030")
        original = self._open()
        self._assert_clean(original, "0030")
        with original.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (username, password_hash, role) VALUES ('owner', 'synthetic', 'admin')")
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, version, "
                "created_at, updated_at) VALUES ('doc', 'owner', 'imported', 'email', 'invoice', 1, 1, 1)")
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, version, "
                "created_at, updated_at) VALUES ('pay', 'owner', 'imported', 'email', 'payment_confirmation', 1, 1, 1)")
        columns = [column["name"] for column in inspect(original).get_columns("budget_receipts")]
        with original.connect() as connection:
            before = connection.exec_driver_sql(
                "SELECT " + ", ".join(columns) + " FROM budget_receipts ORDER BY id").all()

        upgraded, _ = db.init_db(self.path)
        self._assert_clean(upgraded, HEAD)
        with upgraded.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT " + ", ".join(columns) + " FROM budget_receipts ORDER BY id").all(), before)
            self.assertEqual(connection.exec_driver_sql(
                "SELECT gateway, gateway_status, gateway_ref_hmac, fee_amount, payee_host, "
                "evidence_for_receipt_id FROM budget_receipts").all(),
                [(None, None, None, None, None, None)] * 2)

        with upgraded.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, version, "
                "created_at, updated_at, gateway, gateway_status, gateway_ref_hmac, fee_amount, payee_host) "
                "VALUES ('g1', 'owner', 'imported', 'email', 'payment_confirmation', 1, 1, 1, "
                "'Example Pay', 'registered', ?, '0', 'shop.example.test')",
                ("ab" * 32,))
            # The same reference under another gateway and further NULL references stay distinct.
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, version, "
                "created_at, updated_at, gateway, gateway_status, gateway_ref_hmac) "
                "VALUES ('g2', 'owner', 'imported', 'email', 'payment_confirmation', 1, 1, 1, "
                "'Other Pay', 'paid', ?)",
                ("ab" * 32,))
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, version, "
                "created_at, updated_at, gateway, gateway_status) "
                "VALUES ('g3', 'owner', 'imported', 'email', 'payment_confirmation', 1, 1, 1, "
                "'Example Pay', 'registered')")
        for statement, parameters in (
            ("UPDATE budget_receipts SET gateway = 'Example Pay' WHERE id = 'g2'", ()),
            ("UPDATE budget_receipts SET gateway_status = 'pending' WHERE id = 'g1'", ()),
            ("UPDATE budget_receipts SET gateway_status = 'paid', gateway = NULL WHERE id = 'g1'", ()),
            ("UPDATE budget_receipts SET gateway_ref_hmac = 'not-hex' WHERE id = 'g1'", ()),
            ("UPDATE budget_receipts SET fee_amount = '-1' WHERE id = 'g1'", ()),
            ("UPDATE budget_receipts SET payee_host = 'SHOP.example.test' WHERE id = 'g1'", ()),
            ("UPDATE budget_receipts SET payee_host = 'localhost' WHERE id = 'g1'", ()),
            ("UPDATE budget_receipts SET evidence_for_receipt_id = 'ghost' WHERE id = 'g1'", ()),
        ):
            with self.subTest(statement=statement), self.assertRaises(IntegrityError), upgraded.begin() as connection:
                connection.exec_driver_sql(statement, parameters)

        # Deleting the shop document nulls the evidence pointer without touching owner.
        with upgraded.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE budget_receipts SET evidence_for_receipt_id = 'doc' WHERE id = 'g1'")
            connection.exec_driver_sql("DELETE FROM budget_receipts WHERE id = 'doc'")
        with upgraded.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT owner, evidence_for_receipt_id FROM budget_receipts WHERE id = 'g1'").one(),
                ("owner", None))

        db.downgrade_db(self.path, "0030")
        downgraded = self._open()
        self._assert_clean(downgraded, "0030")
        self.assertEqual([column["name"] for column in inspect(downgraded).get_columns("budget_receipts")], columns)
        with downgraded.connect() as connection:
            # The 'doc' row was deleted above; every surviving row keeps its pre-0031 columns.
            self.assertEqual(connection.exec_driver_sql(
                "SELECT id, owner, status, source_kind, document_kind, version FROM budget_receipts "
                "ORDER BY id").all(),
                [("g1", "owner", "imported", "email", "payment_confirmation", 1),
                 ("g2", "owner", "imported", "email", "payment_confirmation", 1),
                 ("g3", "owner", "imported", "email", "payment_confirmation", 1),
                 ("pay", "owner", "imported", "email", "payment_confirmation", 1)])

        reupgraded, _ = db.init_db(self.path)
        self._assert_clean(reupgraded, HEAD)
        with reupgraded.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT gateway, gateway_status, gateway_ref_hmac, fee_amount, payee_host, "
                "evidence_for_receipt_id FROM budget_receipts").all(),
                [(None, None, None, None, None, None)] * 4)

    def test_evidence_pointer_rejects_cross_owner_insert_and_updates(self):
        engine, _ = db.init_db(self.path)
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
                                                 ("other-payment", "other", None)])
        for statement, values in (
            (insert, ("new-payment", "other", "doc")),
            ("UPDATE budget_receipts SET evidence_for_receipt_id = 'doc' WHERE id = 'other-payment'", ()),
            ("UPDATE budget_receipts SET owner = 'other' WHERE id = 'pay'", ()),
            ("UPDATE budget_receipts SET owner = 'other' WHERE id = 'doc'", ()),
        ):
            with self.subTest(statement=statement), engine.begin() as connection:
                with self.assertRaisesRegex(IntegrityError, "invalid receipt evidence owner"):
                    connection.exec_driver_sql(statement, values)
