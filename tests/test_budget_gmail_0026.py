"""Upgrade and downgrade for the Gmail schedule columns."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from hestia import db
from hestia.budget.gmail_store import GmailStore, PendingMessage


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

    def test_schedule_columns_roundtrip_through_init_and_downgrade(self):
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, "0026")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (username, password_hash, role) VALUES ('owner', 'synthetic', 'admin')"
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_gmail_state (owner, grant_status, run_requested, force_full_resync) "
                "VALUES ('owner', 'disabled', 1, 1)"
            )
            self.assertEqual(connection.exec_driver_sql(
                "SELECT run_requested, force_full_resync FROM budget_gmail_state"
            ).one(), (1, 1))
        for statement in (
            "UPDATE budget_gmail_state SET run_requested = 2",
            "UPDATE budget_gmail_state SET force_full_resync = -1",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(IntegrityError), engine.begin() as connection:
                    connection.exec_driver_sql(statement)

        db.downgrade_db(self.path, "0024")
        downgraded = self._open()
        self._assert_clean(downgraded, "0024")
        columns = {column["name"] for column in inspect(downgraded).get_columns("budget_gmail_state")}
        self.assertTrue({"run_requested", "force_full_resync"}.isdisjoint(columns))
        with downgraded.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT grant_status FROM budget_gmail_state"
            ).scalar(), "disabled")

        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, "0026")
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT run_requested, force_full_resync FROM budget_gmail_state"
            ).one(), (0, 0))

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
            db.downgrade_db(self.path, "0024")
        _engine, sessions = db.init_db(self.path)
        with sessions.begin() as session:
            session.execute(db.BudgetGmailMessage.__table__.delete())
            session.execute(db.BudgetReceipt.__table__.delete())
        db.reset_engine_cache()
        db.downgrade_db(self.path, "0024")
        self._assert_clean(self._open(), "0024")

    def test_leftover_batch_table_does_not_block_upgrade(self):
        db.init_db(self.path)
        db.reset_engine_cache()
        db.downgrade_db(self.path, "0024")
        with self._open().begin() as connection:
            connection.exec_driver_sql('CREATE TABLE "_alembic_tmp_budget_gmail_state" (id TEXT)')
        engine, _ = db.init_db(self.path)
        self._assert_clean(engine, "0026")
