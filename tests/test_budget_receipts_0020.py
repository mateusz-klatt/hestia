"""Migration 0020 receipt schema, restrictive constraints and reversible roundtrip."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hestia import db

RECEIPT_A = "11111111-1111-4111-8111-111111111111"
RECEIPT_B = "22222222-2222-4222-8222-222222222222"


class ReceiptMigration0020Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "receipts-0020.db"

    def test_receipt_migration_upgrade_downgrade(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('owner', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_analytics_audit "
                    "(owner, subject_type, subject_id, action, before_json, after_json, changed_at) "
                    "VALUES ('owner', 'category', 'safe', 'create', 'null', '{}', 1.0)")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0020")

            tables = set(inspect(engine).get_table_names())
            self.assertTrue({"budget_shops", "budget_receipts", "budget_receipt_lines"} <= tables)
            self.assertNotIn("budget_normalized_products", tables)
            indexes = {row["name"]: row for row in inspect(engine).get_indexes("budget_receipts")}
            self.assertIn("ix_budget_receipts_owner_date", indexes)
            self.assertTrue(indexes["uq_budget_receipts_tx_link"]["unique"])

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
                    linked_fingerprint="fingerprint", version=1, created_at=1, updated_at=1))
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
                conn.exec_driver_sql("DELETE FROM budget_shops WHERE id='shop'")

            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0019")
            tables = set(inspect(engine).get_table_names())
            self.assertFalse({"budget_shops", "budget_receipts", "budget_receipt_lines"} & tables)
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_analytics_audit").scalar(), 1)
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")
                self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.Base.metadata), [])
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
