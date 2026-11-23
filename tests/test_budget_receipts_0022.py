"""Migration 0022 receipt schema, restrictive constraints and reversible roundtrip.

``down_revision`` is temporarily ``0020`` (category parent on current master) so
the branch has one Alembic head before PR #251 lands migration 0021. Re-parent
to ``0021`` when that pull request merges.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hestia import db

RECEIPT_A = "11111111-1111-4111-8111-111111111111"
RECEIPT_B = "22222222-2222-4222-8222-222222222222"
_RECEIPT_TABLES = {"budget_shops", "budget_receipts", "budget_receipt_lines"}


class ReceiptMigration0022Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "receipts-0022.db"

    def _revision(self, engine):
        with engine.connect() as conn:
            return conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()

    def _tables(self, engine):
        return set(inspect(engine).get_table_names())

    def _assert_clean(self, engine):
        with engine.connect() as conn:
            integrity = conn.exec_driver_sql("PRAGMA integrity_check").all()
            self.assertEqual([tuple(row) for row in integrity], [("ok",)])
            leftovers = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'"
            ).all()
            self.assertEqual(list(leftovers), [])

    def _open(self):
        engine = db.make_engine(self.path)
        self.addCleanup(engine.dispose)
        return engine

    def test_receipt_migration_upgrade_downgrade(self):
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0022")
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('owner', 'h', 'admin')")
            conn.exec_driver_sql(
                "INSERT INTO budget_analytics_audit "
                "(owner, subject_type, subject_id, action, before_json, after_json, changed_at) "
                "VALUES ('owner', 'category', 'safe', 'create', 'null', '{}', 1.0)")

        tables = self._tables(engine)
        self.assertTrue(_RECEIPT_TABLES <= tables)
        self.assertNotIn("budget_normalized_products", tables)
        indexes = {row["name"]: row for row in inspect(engine).get_indexes("budget_receipts")}
        self.assertIn("ix_budget_receipts_owner_date", indexes)
        self.assertTrue(indexes["uq_budget_receipts_tx_link"]["unique"])
        categories = {column["name"] for column in inspect(engine).get_columns("budget_categories")}
        self.assertIn("parent_id", categories)

        with Session(engine) as session:
            session.add(db.BudgetShop(id="shop", owner="owner", name="Synthetic shop", chain_key="synthetic",
                                      nip="7791011327", version=1, created_at=1, updated_at=1))
            session.add(db.BudgetConnection(id="bank", owner="owner", application_id="app", bank_name="Bank",
                                            country="PL", enabled=True, next_attempt=0, failures=0))
            session.flush()
            session.add(db.BudgetAccount(id="account", connection_id="bank", owner="owner",
                                         identification_hash="hash", enabled=True, balances_json="[]"))
            session.flush()
            payload = {"entry_reference": "entry", "amount": "100", "currency": "PLN",
                       "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-20",
                       "value_date": None, "transaction_date": None, "remittance_information": []}
            session.add(db.BudgetTransaction(account_id="account", entry_reference="entry",
                                             payload_json=json.dumps(payload), observed_at=1, uncertain=False))
            session.flush()
            session.add(db.BudgetReceipt(id=RECEIPT_A, owner="owner", shop_id="shop", status="confirmed",
                source_kind="live_text", receipt_date="2026-11-20", total_amount="100", currency="PLN",
                payment_method="card", linked_account_id="account", linked_entry_ref="entry",
                linked_fingerprint="fingerprint", linked_evidence="{}", version=1, created_at=1, updated_at=1))
            session.add(db.BudgetReceiptLine(receipt_id=RECEIPT_A, line_no=1, owner="owner", line_kind="item",
                raw_name="Synthetic item", raw_name_key="synthetic item", ptu_code="A", quantity="1",
                gross_line_total="100", net_line_total="100"))
            session.commit()

        def rejected(statement, parameters=()):
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(statement, parameters)

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, version, created_at, updated_at) "
                "VALUES (?, 'owner', 'draft', 'live_text', 1, 1.0, 1.0)", (RECEIPT_B,))
        rejected(
            "INSERT INTO budget_receipts (id, owner, status, source_kind, version, created_at, updated_at) "
            "VALUES ('33333333-3333-4333-8333-333333333333', 'owner', 'confirmed', 'live_text', 1, 1, 1)")
        rejected(
            "INSERT INTO budget_receipts (id, owner, status, source_kind, linked_account_id, "
            "linked_entry_ref, linked_fingerprint, version, created_at, updated_at) VALUES "
            "('33333333-3333-4333-8333-333333333333', 'owner', 'draft', 'live_text', "
            "'account', 'entry', 'fp', 1, 1, 1)")
        rejected(
            "INSERT INTO budget_receipts (id, owner, status, source_kind, receipt_date, total_amount, "
            "currency, payment_method, linked_account_id, linked_entry_ref, linked_fingerprint, version, "
            "created_at, updated_at) VALUES ('33333333-3333-4333-8333-333333333333', 'owner', "
            "'confirmed', 'live_text', '2026-11-20', '100', 'PLN', 'card', 'account', 'entry', "
            "'other-fingerprint', 1, 1, 1)")
        rejected(
            "INSERT INTO budget_receipt_lines (receipt_id, line_no, owner, line_kind, raw_name, "
            "raw_name_key, quantity, gross_line_total, net_line_total) VALUES "
            "(?, 301, 'owner', 'item', 'x', 'x', '1', '1', '1')", (RECEIPT_A,))
        rejected("DELETE FROM budget_shops WHERE id='shop'")
        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM budget_receipt_lines WHERE receipt_id=?", (RECEIPT_A,))
            conn.exec_driver_sql("DELETE FROM budget_receipts WHERE id=?", (RECEIPT_A,))
            conn.exec_driver_sql("DELETE FROM budget_receipts WHERE id=?", (RECEIPT_B,))
            conn.exec_driver_sql("DELETE FROM budget_shops WHERE id='shop'")

        db.downgrade_db(self.path, "0020")
        engine = self._open()
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0020")
        self.assertFalse(_RECEIPT_TABLES & self._tables(engine))
        categories = {column["name"] for column in inspect(engine).get_columns("budget_categories")}
        self.assertIn("parent_id", categories)
        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_analytics_audit").scalar(), 1)

        engine, _ = db.init_db(self.path)
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0022")
        with engine.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.Base.metadata), [])

    def test_upgrade_interruption_rolls_back_and_retries(self):
        from alembic import op

        db.init_db(self.path)
        db.downgrade_db(self.path, "0020")
        engine = self._open()
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0020")
        real = op.create_table
        calls = {"n": 0}

        def fail_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("injected upgrade failure")
            return real(*args, **kwargs)

        op.create_table = fail_second
        try:
            with self.assertRaisesRegex(RuntimeError, "injected upgrade failure"):
                db.init_db(self.path)
            engine = self._open()
            self._assert_clean(engine)
            self.assertEqual(self._revision(engine), "0020")
            self.assertNotIn("budget_shops", self._tables(engine))
            self.assertNotIn("budget_receipts", self._tables(engine))
            self.assertIn("parent_id", {column["name"] for column in inspect(engine).get_columns("budget_categories")})
        finally:
            op.create_table = real
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0022")
        self.assertTrue(_RECEIPT_TABLES <= self._tables(engine))

    def test_downgrade_interruption_keeps_populated_rows_and_retries(self):
        from alembic import op

        engine, _ = db.init_db(self.path)
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('owner', 'h', 'admin')")
            conn.exec_driver_sql(
                "INSERT INTO budget_shops (id, owner, name, version, created_at, updated_at) "
                "VALUES ('shop', 'owner', 'Synthetic shop', 1, 1.0, 1.0)")
            conn.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, raw_shop_name, version, "
                "created_at, updated_at) VALUES (?, 'owner', 'draft', 'live_text', 'Synthetic shop', 1, 1.0, 1.0)",
                (RECEIPT_A,))
        self._assert_clean(engine)
        real = op.drop_table
        calls = {"n": 0}

        def fail_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("injected downgrade failure")
            return real(*args, **kwargs)

        op.drop_table = fail_second
        try:
            with self.assertRaisesRegex(RuntimeError, "injected downgrade failure"):
                db.downgrade_db(self.path, "0020")
            engine = self._open()
            self._assert_clean(engine)
            self.assertEqual(self._revision(engine), "0022")
            self.assertTrue(_RECEIPT_TABLES <= self._tables(engine))
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT name FROM budget_shops").scalar(), "Synthetic shop")
                self.assertEqual(conn.exec_driver_sql("SELECT raw_shop_name FROM budget_receipts").scalar(),
                                 "Synthetic shop")
        finally:
            op.drop_table = real
        db.downgrade_db(self.path, "0020")
        engine = self._open()
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0020")
        self.assertFalse(_RECEIPT_TABLES & self._tables(engine))
        self.assertIn("parent_id", {column["name"] for column in inspect(engine).get_columns("budget_categories")})
        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT username FROM users").scalar(), "owner")

    def test_cross_owner_link_and_raw_text_without_expiry_are_rejected(self):
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine)
        self.assertEqual(self._revision(engine), "0022")
        with Session(engine) as session:
            session.add(db.User(username="owner", password_hash="h", role="admin"))
            session.add(db.User(username="other", password_hash="h", role="admin"))
            session.flush()
            session.add(db.BudgetConnection(id="bank", owner="owner", application_id="app", bank_name="Bank",
                                            country="PL", enabled=True, next_attempt=0, failures=0))
            session.add(db.BudgetConnection(id="other-bank", owner="other", application_id="app",
                                            bank_name="Bank", country="PL", enabled=True, next_attempt=0,
                                            failures=0))
            session.flush()
            session.add(db.BudgetAccount(id="account", connection_id="bank", owner="owner",
                                         identification_hash="hash", enabled=True, balances_json="[]"))
            session.add(db.BudgetAccount(id="foreign", connection_id="other-bank", owner="other",
                                         identification_hash="foreign", enabled=True, balances_json="[]"))
            session.flush()
            payload = {"entry_reference": "entry", "amount": "100", "currency": "PLN",
                       "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-11-20",
                       "value_date": None, "transaction_date": None, "remittance_information": []}
            session.add(db.BudgetTransaction(account_id="foreign", entry_reference="entry",
                                             payload_json=json.dumps(payload), observed_at=1, uncertain=False))
            session.commit()
        link = (
            "INSERT INTO budget_receipts (id, owner, status, source_kind, receipt_date, total_amount, "
            "currency, payment_method, linked_account_id, linked_entry_ref, linked_fingerprint, "
            "linked_evidence, version, created_at, updated_at) VALUES "
            "(?, ?, 'confirmed', 'live_text', '2026-11-20', '100', 'PLN', 'card', "
            "'foreign', 'entry', 'fingerprint', '{}', 1, 1, 1)"
        )
        with self.assertRaises(IntegrityError) as rejected:
            with engine.begin() as conn:
                conn.exec_driver_sql(link, (RECEIPT_A, "owner"))
        self.assertIn("FOREIGN KEY", str(rejected.exception).upper())
        with engine.begin() as conn:
            conn.exec_driver_sql(link, (RECEIPT_A, "other"))
            self.assertEqual(conn.exec_driver_sql(
                "SELECT owner FROM budget_receipts WHERE id = ?", (RECEIPT_A,)).scalar(), "other")
            conn.exec_driver_sql("DELETE FROM budget_receipts WHERE id = ?", (RECEIPT_A,))
        with self.assertRaises(IntegrityError):
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipts (id, owner, status, source_kind, raw_ocr_text, version, "
                    "created_at, updated_at) VALUES (?, 'owner', 'draft', 'live_text', 'synthetic', 1, 1, 1)",
                    (RECEIPT_B,))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, raw_ocr_text, raw_text_expires_at, "
                "version, created_at, updated_at) VALUES (?, 'owner', 'draft', 'live_text', 'synthetic', 2.0, 1, 1, 1)",
                (RECEIPT_B,))
            self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_receipts").scalar(), 1)
        self._assert_clean(engine)


if __name__ == "__main__":
    unittest.main()
