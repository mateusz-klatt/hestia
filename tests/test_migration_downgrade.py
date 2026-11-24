"""Populated downgrades through the production runner, including referenced batch tables."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

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

    def test_0030_populated_roundtrip_0029_to_0030_to_0029_to_0030(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "bridge.db"
        engine = db.make_engine(target)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0029")
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h1', 'admin')")
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('bob', 'h2', 'viewer')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_connections (id, owner, application_id, bank_name, country, enabled, next_attempt, failures) "
                    "VALUES ('bank', 'alice', 'app', 'Bank', 'PL', 1, 0, 0)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_accounts (id, connection_id, owner, identification_hash, enabled, balances_json) "
                    "VALUES ('acc', 'bank', 'alice', 'ih', 1, '[]')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_transactions (account_id, entry_reference, payload_json, observed_at, uncertain) "
                    "VALUES ('acc', 'ref1', '{}', 1.0, 0)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_shops (id, owner, name, version, created_at, updated_at) "
                    "VALUES ('shop', 'alice', 'Shop', 1, 1.0, 1.0)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipts "
                    "(id, owner, shop_id, status, source_kind, document_kind, receipt_date, total_amount, currency, "
                    "payment_method, linked_account_id, linked_entry_ref, linked_fingerprint, linked_evidence, "
                    "version, created_at, updated_at) "
                    "VALUES ('rcpt-1', 'alice', 'shop', 'confirmed', 'email', 'invoice', '2026-11-20', '10.00', 'PLN', "
                    "'card', 'acc', 'ref1', 'fp', '{}', 1, 1.0, 1.0)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipt_lines "
                    "(receipt_id, line_no, owner, line_kind, raw_name, raw_name_key, quantity, gross_line_total, net_line_total) "
                    "VALUES ('rcpt-1', 1, 'alice', 'item', 'Item', 'item', '1', '10.00', '10.00')"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_gmail_state (owner, grant_status, grant_generation, version) "
                    "VALUES ('alice', 'active', 1, 2)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_gmail_messages "
                    "(owner, gmail_id_hmac, rfc_message_id_hmac, internal_date, trust_path, outcome, receipt_id, created_at) "
                    "VALUES ('alice', ?, ?, 100.0, 'spouse_forward', 'imported', 'rcpt-1', 100.0)",
                    ("a" * 64, "b" * 64),
                )
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipt_device_tokens "
                    "(id, owner, device_name, token_hmac, created_at, expires_at, hard_expires_at, sliding_written_at) "
                    "VALUES ('tok-1', 'alice', 'Vesta', ?, 1.0, 2.0, 3.0, 1.0)",
                    ("c" * 64,),
                )
        finally:
            engine.dispose()

        # Upgrade 0029 -> 0030 through production runner.
        engine, _ = db.init_db(target)
        with engine.begin() as conn:
            # Legacy inserts without 0030 columns continue working and default new nullable cols to NULL.
            conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('carol', 'h3', 'viewer')")
            conn.exec_driver_sql(
                "INSERT INTO budget_receipts (id, owner, status, source_kind, version, created_at, updated_at) "
                "VALUES ('rcpt-legacy', 'alice', 'draft', 'live_text', 1, 2.0, 2.0)"
            )
            conn.exec_driver_sql(
                "INSERT INTO budget_receipt_device_tokens "
                "(id, owner, device_name, token_hmac, created_at, expires_at, hard_expires_at, sliding_written_at) "
                "VALUES ('tok-legacy', 'alice', 'Old', ?, 2.0, 3.0, 4.0, 2.0)",
                ("d" * 64,),
            )
            self.assertEqual(
                conn.exec_driver_sql("SELECT budget_owner, credential_generation FROM users WHERE username='alice'").one(),
                (0, 0),
            )
            self.assertEqual(
                conn.exec_driver_sql("SELECT submitted_by FROM budget_receipts WHERE id='rcpt-1'").scalar(),
                None,
            )
            self.assertEqual(
                conn.exec_driver_sql(
                    "SELECT actor, issued_permissions, token_hmac FROM budget_receipt_device_tokens WHERE id='tok-1'"
                ).one(),
                (None, None, "c" * 64),
            )
            # Populate 0030-specific tables/columns/markers.
            conn.exec_driver_sql("INSERT INTO app_meta (key, value) VALUES ('auth_session_phase', 'v2')")
            conn.exec_driver_sql("INSERT INTO app_meta (key, value) VALUES ('authz_policy_phase', 'catalog_compat')")
            conn.exec_driver_sql(
                "INSERT INTO permission_grants (username, permission_id, target_owner) VALUES ('bob', 'household.view', 'alice')"
            )
            conn.exec_driver_sql(
                "INSERT INTO native_login_codes (id, username, code_hmac, generation, created_at, expires_at) "
                "VALUES ('nc-1', 'bob', ?, 0, 1.0, 10.0)",
                ("e" * 64,),
            )
            conn.exec_driver_sql("UPDATE budget_receipts SET submitted_by='bob' WHERE id='rcpt-1'")
            conn.exec_driver_sql(
                "UPDATE budget_receipt_device_tokens SET actor='bob', issued_permissions='[\"budget.receipts.add\"]' "
                "WHERE id='tok-1'"
            )

        # Both new FKs enforce references at 0030.
        with self.assertRaises(IntegrityError):
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipts (id, owner, submitted_by, status, source_kind, version, created_at, updated_at) "
                    "VALUES ('rcpt-bad', 'alice', 'ghost', 'draft', 'live_text', 1, 2.0, 2.0)"
                )
        with self.assertRaises(IntegrityError):
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO budget_receipt_device_tokens "
                    "(id, owner, actor, device_name, token_hmac, created_at, expires_at, hard_expires_at, sliding_written_at) "
                    "VALUES ('tok-bad', 'alice', 'ghost', 'Bad', ?, 2.0, 3.0, 4.0, 2.0)",
                    ("f" * 64,),
                )

        # Downgrade 0030 -> 0029 through production runner (with a leftover temp table to exercise cleanup).
        with closing(sqlite3.connect(target)) as raw_conn:
            raw_conn.execute('CREATE TABLE "_alembic_tmp_users" (stale TEXT)')
        db.downgrade_db(target, "0029")

        with closing(sqlite3.connect(target)) as raw_conn:
            self.assertEqual(raw_conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(raw_conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                raw_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'"
                ).fetchall(),
                [],
            )
            self.assertEqual(raw_conn.execute("SELECT version_num FROM alembic_version").fetchall(), [("0029",)])
            self.assertEqual(
                raw_conn.execute(
                    "SELECT key FROM app_meta WHERE key IN ('auth_session_phase', 'authz_policy_phase')"
                ).fetchall(),
                [],
            )
            tables = {row[0] for row in raw_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("permission_grants", tables)
            self.assertNotIn("native_login_codes", tables)
            self.assertNotIn("budget_owner", {r[1] for r in raw_conn.execute("PRAGMA table_info(users)")})
            self.assertNotIn("submitted_by", {r[1] for r in raw_conn.execute("PRAGMA table_info(budget_receipts)")})
            self.assertNotIn(
                "actor", {r[1] for r in raw_conn.execute("PRAGMA table_info(budget_receipt_device_tokens)")}
            )
            self.assertEqual(
                raw_conn.execute("SELECT token_hmac FROM budget_receipt_device_tokens WHERE id='tok-1'").fetchone()[0],
                "c" * 64,
            )
            self.assertEqual(
                raw_conn.execute("SELECT line_no, raw_name FROM budget_receipt_lines WHERE receipt_id='rcpt-1'").fetchall(),
                [(1, "Item")],
            )
            self.assertEqual(
                raw_conn.execute("SELECT receipt_id, trust_path FROM budget_gmail_messages WHERE owner='alice'").fetchall(),
                [("rcpt-1", "spouse_forward")],
            )
            index_xinfo = raw_conn.execute("PRAGMA index_xinfo('ix_budget_receipts_owner_date')").fetchall()
            # (seqno, cid, name, desc, coll, key) -> receipt_date must keep DESC (desc == 1).
            by_col = {row[2]: row[3] for row in index_xinfo if row[2] is not None}
            self.assertEqual(by_col, {"owner": 0, "receipt_date": 1, "id": 0})

        # Upgrade 0029 -> 0030 again and check parity + survival.
        engine, _ = db.init_db(target)
        with engine.connect() as conn:
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])
            self.assertEqual(conn.exec_driver_sql("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                conn.exec_driver_sql("SELECT submitted_by FROM budget_receipts WHERE id='rcpt-1'").scalar(),
                None,
            )
            self.assertEqual(
                conn.exec_driver_sql(
                    "SELECT actor, issued_permissions, token_hmac FROM budget_receipt_device_tokens WHERE id='tok-1'"
                ).one(),
                (None, None, "c" * 64),
            )
            self.assertEqual(
                conn.exec_driver_sql("SELECT receipt_id FROM budget_gmail_messages WHERE owner='alice'").scalar(),
                "rcpt-1",
            )
