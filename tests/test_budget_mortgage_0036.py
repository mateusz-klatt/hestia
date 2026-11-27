"""Mortgage bank reference migration through the SQLite migration runner."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from alembic import command
from sqlalchemy import inspect, select

from hestia import db


class MortgageReferencesMigrationTests(unittest.TestCase):
    def test_populated_schedule_and_payment_survive_reference_roundtrip(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        path = Path(directory.name) / "loan-references.db"
        old_engine = db.make_engine(path)
        self.addCleanup(old_engine.dispose)
        db._run_sqlite_migration(
            old_engine, lambda conn: command.upgrade(db._alembic_config(conn), "0035"),
        )
        with old_engine.begin() as conn:
            conn.execute(db.User.__table__.insert().values(username="owner", password_hash="synthetic"))
            conn.exec_driver_sql(
                "INSERT INTO budget_mortgage_schedules "
                "(id, owner, label, amount, currency, due_day, start_date, remind_days_before, "
                "destination_kind, enabled, version, created_at, updated_at) "
                "VALUES ('test-schedule', 'owner', 'Synthetic loan', '100', 'PLN', 5, '2026-01-01', 5, "
                "'owner_inbox', 1, 1, 1, 1)"
            )
            conn.execute(db.BudgetMortgagePayment.__table__.insert().values(
                schedule_id="test-schedule", month="2026-01", owner="owner", marked_at=1,
            ))

        engine, session_factory = db.init_db(path)
        tokens = ("TESTLOAN0001", "TESTPOLICY0002")
        references = [{"hash": sha256(token.encode()).hexdigest(), "last4": token[-4:]} for token in tokens]
        with db.session_scope(session_factory) as session:
            schedule = session.get(db.BudgetMortgageSchedule, "test-schedule")
            self.assertIsNone(schedule.match_references)
            schedule.match_references = references
        with engine.connect() as conn:
            stored = conn.exec_driver_sql("SELECT match_references FROM budget_mortgage_schedules").scalar_one()
            self.assertEqual(json.loads(stored), references)
            for token in tokens:
                self.assertNotIn(token, stored)

        db.downgrade_db(path, "0035")
        with old_engine.connect() as conn:
            self.assertNotIn("match_references", {
                column["name"] for column in inspect(conn).get_columns("budget_mortgage_schedules")
            })
            self.assertEqual(conn.exec_driver_sql(
                "SELECT id, label, amount FROM budget_mortgage_schedules",
            ).one(), ("test-schedule", "Synthetic loan", "100"))
            self.assertEqual(conn.exec_driver_sql(
                "SELECT schedule_id, month FROM budget_mortgage_payments",
            ).one(), ("test-schedule", "2026-01"))
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

        engine, _ = db.init_db(path)
        with engine.connect() as conn:
            self.assertIsNone(conn.execute(select(db.BudgetMortgageSchedule.match_references)).scalar_one())
