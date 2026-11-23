"""Upgrade and downgrade for the Gmail schedule columns."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from hestia import db
from hestia.budget.gmail_store import GmailStore, PendingMessage
from tests.migration_head import HEAD


class GmailScheduleMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "gmail-schedule.db"

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

    def test_populated_0025_to_0026_to_0025_roundtrip(self):
        db.init_db(self.path)
        db.downgrade_db(self.path, "0026")
        original = self._open()
        self._assert_clean(original, "0026")
        with original.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (username, password_hash, role) VALUES ('owner', 'synthetic', 'admin')"
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_gmail_state (owner, grant_status, history_id) VALUES ('owner', 'active', '100')"
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, document_kind, version, "
                "created_at, updated_at) VALUES ('receipt', 'owner', 'imported', 'email', 'invoice', 1, 1, 1)"
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_gmail_messages (owner, gmail_id_hmac, internal_date, trust_path, outcome, "
                "created_at, receipt_id) VALUES ('owner', ?, 1, 'spouse_forward', 'imported', 1, 'receipt')",
                ("aa" * 32,),
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_receipt_device_tokens (id, owner, device_name, token_hmac, created_at, "
                "expires_at, hard_expires_at, sliding_written_at) VALUES ('device', 'owner', 'Synthetic', ?, 1, 2, 3, 1)",
                ("bb" * 32,),
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_receipt_pair_codes (id, owner, code_hmac, created_at, expires_at) "
                "VALUES ('pair', 'owner', ?, 1, 2)",
                ("cc" * 32,),
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_analytics_audit (owner, subject_type, subject_id, action, before_json, "
                "after_json, changed_at) VALUES ('owner', 'receipt_device_token', 'device', 'create', '{}', '{}', 1)"
            )

        tables = (
            "budget_gmail_state", "budget_receipts", "budget_gmail_messages", "budget_receipt_device_tokens",
            "budget_receipt_pair_codes", "budget_analytics_audit",
        )
        columns = {table: [column["name"] for column in inspect(original).get_columns(table)] for table in tables}

        def snapshot(engine):
            with engine.connect() as connection:
                return {table: connection.exec_driver_sql(
                    "SELECT " + ", ".join(columns[table]) + " FROM " + table
                ).all() for table in tables}

        before = snapshot(original)
        upgraded, _ = db.init_db(self.path)
        self._assert_clean(upgraded, HEAD)
        self.assertEqual(snapshot(upgraded), before)
        with upgraded.begin() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT run_requested, force_full_resync FROM budget_gmail_state"
            ).one(), (0, 0))
            self.assertEqual(connection.exec_driver_sql(
                "SELECT forwarded_manually, forwarded_sender_domain FROM budget_receipts"
            ).one(), (0, None))
            connection.exec_driver_sql(
                "UPDATE budget_gmail_state SET run_requested = 1, force_full_resync = 1"
            )
        for statement in (
            "UPDATE budget_gmail_state SET run_requested = 2",
            "UPDATE budget_gmail_state SET force_full_resync = -1",
        ):
            with self.subTest(statement=statement), self.assertRaises(IntegrityError), upgraded.begin() as connection:
                connection.exec_driver_sql(statement)

        db.downgrade_db(self.path, "0026")
        downgraded = self._open()
        self._assert_clean(downgraded, "0026")
        self.assertEqual(snapshot(downgraded), before)
        for table in tables:
            self.assertEqual([column["name"] for column in inspect(downgraded).get_columns(table)], columns[table])

    def test_manual_forward_is_stored_without_the_address_and_blocks_downgrade(self):
        _engine, sessions = db.init_db(self.path)
        store = GmailStore(sessions, clock=lambda: 1_800_000_000.0)
        with sessions.begin() as session:
            session.add(db.AppMeta(key="users_authoritative", value="1"))
            session.add(db.User(username="owner", password_hash="synthetic", role="admin"))
            session.flush()
            session.add(db.BudgetGmailState(
                owner="owner", grant_status="active", grant_generation=1, version=2,
                sync_phase="idle", next_sync_at=0, backoff_until=0, failures=0,
                batch_processed_count=0, run_requested=0, force_full_resync=0,
            ))
        item = PendingMessage("ab" * 32, 1_700_000_000.0, "spouse_forward", "imported",
                              forwarded_manually=True, sender_domain="corner-shop.example", source_kind="email")
        self.assertEqual(store.commit("owner", 1, {}, (item,)), (1, 0, True))
        with sessions.begin() as session:
            receipt = session.scalar(select(db.BudgetReceipt))
            self.assertEqual((receipt.status, receipt.raw_shop_name, receipt.forwarded_manually,
                              receipt.forwarded_sender_domain, receipt.linked_account_id, receipt.raw_ocr_text),
                             ("needs_review", "przekazane ręcznie", 1, "corner-shop.example", None, None))
            session.add(db.BudgetGmailMessage(
                owner="owner", gmail_id_hmac="cd" * 32, internal_date=1, trust_path="owner_forward",
                outcome="skipped_non_receipt", created_at=1,
            ))
            session.connection().exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        stored = self.path.read_bytes()
        self.assertNotIn(b"billing@corner-shop.example", stored)
        self.assertNotIn(b"Please pay this shop bill", stored)
        with self.assertRaisesRegex(RuntimeError, "manual forwards"):
            db.downgrade_db(self.path, "0026")
        _engine, sessions = db.init_db(self.path)
        with sessions.begin() as session:
            session.execute(db.BudgetGmailMessage.__table__.delete())
            session.execute(db.BudgetReceipt.__table__.delete())
        db.reset_engine_cache()
        db.downgrade_db(self.path, "0026")
        self._assert_clean(self._open(), "0026")

    def test_leftover_batch_table_does_not_block_upgrade(self):
        db.init_db(self.path)
        db.reset_engine_cache()
        db.downgrade_db(self.path, "0026")
        with self._open().begin() as connection:
            connection.exec_driver_sql('CREATE TABLE "_alembic_tmp_budget_gmail_state" (id TEXT)')
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, HEAD)
