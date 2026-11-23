"""Populated downgrades through the production runner, including referenced batch tables."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from hestia import db


# Creation revision, table, and durable fields (columns intentionally removed by
# downgrades are checked separately). Insertion order satisfies the real FKs.
_ROWS = (
    (1, "users", dict(username="alice", password_hash="hash")),
    (1, "user_settings", dict(username="alice", locale="pl", temp_scale="C", theme="dark")),
    (6, "budget_connections", dict(id="bank", owner="alice", application_id="app",
        bank_name="Bank", country="PL", enabled=True, next_attempt=0, failures=0)),
    (6, "budget_accounts", dict(id="account", connection_id="bank", owner="alice",
        identification_hash="hash", enabled=True, balances_json="[]")),
    (6, "budget_transactions", dict(account_id="account", entry_reference="entry",
        payload_json="{}", observed_at=1, uncertain=False)),
    (15, "budget_categories", dict(id="category", owner="alice", name="Food", name_key="food",
        archived=False, version=1, created_at=1, updated_at=1)),
    (15, "budget_rules", dict(id="rule", owner="alice", field="memo", operator="contains",
        value="shop", category_id="category", kind="expense", priority=1, enabled=True,
        version=1, created_at=1, updated_at=1)),
    (15, "budget_annotations", dict(account_id="account", entry_reference="entry", owner="alice",
        mode="manual", category_id="category", kind="expense", ledger_fingerprint="fp",
        version=1, updated_at=1)),
    (15, "budget_analytics_audit", dict(owner="alice", subject_type="category", subject_id="category",
        action="create", before_json="null", after_json="{}", changed_at=1)),
    (17, "budget_category_limits", dict(id="limit", owner="alice", category_id="category",
        currency="PLN", monthly_limit="100", warning_percent=80, enabled=True,
        version=1, created_at=1, updated_at=1)),
    (19, "budget_owner_inbox_state", dict(owner="alice", reads_revision=1, updated_at=1)),
    (19, "budget_owner_inbox_reads", dict(owner="alice", kind="spending_limit",
        subject_id="a" * 64, milestone="warning", read_at=1)),
    (22, "budget_shops", dict(id="shop", owner="alice", name="Shop", version=1,
        created_at=1, updated_at=1)),
    (22, "budget_receipts", dict(id="receipt", owner="alice", shop_id="shop", status="confirmed",
        source_kind="live_text", receipt_date="2026-11-20", total_amount="100", currency="PLN",
        payment_method="card", linked_account_id="account", linked_entry_ref="entry",
        linked_fingerprint="fp", linked_evidence="{}", version=1, created_at=1, updated_at=1)),
    (22, "budget_receipt_lines", dict(receipt_id="receipt", line_no=1, owner="alice", line_kind="item",
        raw_name="Item", raw_name_key="item", quantity="1", gross_line_total="100", net_line_total="100")),
)


class MigrationDowngradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(directory.name) / "roundtrip.db"
        engine, _ = db.init_db(self.path)
        with engine.begin() as connection:
            scripts = ScriptDirectory.from_config(db._alembic_config(connection))
            self.revisions = tuple(int(script.revision) for script in scripts.walk_revisions())
            for _, table, values in _ROWS:
                connection.execute(db.metadata.tables[table].insert().values(**values))
            connection.exec_driver_sql("UPDATE users SET role='operator', disabled=1")
            connection.exec_driver_sql("UPDATE budget_annotations SET label='Synthetic label'")
            connection.exec_driver_sql(
                "INSERT INTO budget_analytics_audit "
                "(owner, subject_type, subject_id, action, before_json, after_json, changed_at) "
                "VALUES ('alice', 'receipt', 'receipt', 'confirm', 'null', '{}', 1)")

    def _assert_database(self, revision, *, surviving_revision=None):
        # After upgrading again, tables removed by the downgrade must stay empty.
        surviving_revision = revision if surviving_revision is None else surviving_revision
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'"
            ).fetchall(), [])
            self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchall(),
                             [(f"{revision:04d}",)])
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for created, table, values in _ROWS:
                with self.subTest(table=table):
                    if created > revision:
                        self.assertNotIn(table, tables)
                        continue
                    columns = ", ".join(f'"{column}"' for column in values)
                    rows = connection.execute(f'SELECT {columns} FROM "{table}"').fetchall()
                    expected = [tuple(values.values())] if created <= surviving_revision else []
                    if table == "budget_analytics_audit" and surviving_revision >= 22:
                        expected.append(("alice", "receipt", "receipt", "confirm", "null", "{}", 1))
                    self.assertCountEqual(rows, expected)
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            self.assertEqual("role" in user_columns, revision >= 2)
            self.assertEqual("disabled" in user_columns, revision >= 3)
            if revision >= 2:
                self.assertEqual(connection.execute("SELECT role FROM users").fetchall(),
                                 [("operator" if surviving_revision >= 2 else "admin",)])
            if revision >= 3:
                self.assertEqual(connection.execute("SELECT disabled FROM users").fetchall(),
                                 [(int(surviving_revision >= 3),)])
            if revision >= 15:
                annotation_columns = {row[1] for row in connection.execute("PRAGMA table_info(budget_annotations)")}
                self.assertEqual("label" in annotation_columns, revision >= 21)

    def test_populated_head_downgrades_stepwise_to_0001_and_upgrades_again(self):
        head, *parents = self.revisions
        self._assert_database(head)
        for revision in parents:
            with self.subTest(revision=revision):
                db.downgrade_db(self.path, f"{revision:04d}")
                self._assert_database(revision)
        db.init_db(self.path)
        self._assert_database(head, surviving_revision=1)

    def _leave_batch_table(self, table):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(f'CREATE TABLE "_alembic_tmp_{table}" (stale TEXT)')

    def test_0020_and_0021_recover_leftovers_in_both_directions(self):
        # 0019 and 0022 do not use batch_alter_table. Both batch migrations
        # between them already guard upgrade and downgrade; exercise every guard.
        for revision, table in ((20, "budget_annotations"), (19, "budget_categories")):
            with self.subTest(direction="downgrade", table=table):
                self._leave_batch_table(table)
                db.downgrade_db(self.path, f"{revision:04d}")
                self._assert_database(revision)
        self._leave_batch_table("budget_categories")
        self._leave_batch_table("budget_annotations")
        engine, _ = db.init_db(self.path)
        self._assert_database(self.revisions[0], surviving_revision=19)
        with engine.connect() as connection:
            self.assertIsNone(connection.exec_driver_sql("SELECT label FROM budget_annotations").scalar())
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), db.metadata), [])
