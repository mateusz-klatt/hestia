"""Migration 0021 and one-off label normalization.

Revision 0021 revises 0020. Upgrade and downgrade go through ``init_db`` and
``downgrade_db``: foreign keys off, ``BEGIN IMMEDIATE``, then
``PRAGMA foreign_key_check``. After upgrade, downgrade, and upgrade again,
``PRAGMA integrity_check`` is ok and no ``_alembic_tmp_*`` table remains.
No table references ``budget_annotations``. The annotation row itself
references transactions, users and categories, so the runner's check still
sees those outgoing keys after the batch rebuild.

The label text is owner-private: these tests use synthetic phrases and never log them.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import api_contract, db
from hestia.budget import month_budget
from hestia.budget.analytics_core import (
    AnalyticsError, active_one_off_label, clean_one_off_label, one_off_key, stored_one_off_label, sum_money,
)
from tests import test_budget_analytics_api as analytics_api_tests
from tests.test_budget_analytics_store import AnalyticsCase
from tests.test_budget_analytics_views import ProjectionCase
from tests.test_budget_month import DEFAULT_SETTINGS, ONE_OFF_TEST_SECRET, _cat, _eval, _eval_charts, _tx

# One scenario for the overview and the chart series. A labelled mortgage leg stays in its category.
_FOOD_LABEL = "Remont lazienki"
_REFUND_LABEL = "Zwrot materialu"
_HIDDEN_LABEL = "Ukryte"
_MORTGAGE_LABEL = "Nie ruszaj"
# role, reference, amount, direction, kind, label, stale
ONE_OFF_FIXTURE = (
    ("food", "food", "10", "DBIT", "expense", None, False),
    ("food", "food-big", "40", "DBIT", "expense", _FOOD_LABEL, False),
    ("none", "cash", "2000", "DBIT", "expense", _FOOD_LABEL, False),
    ("none", "cash-2", "15", "DBIT", "expense", None, False),
    ("none", "stale", "7", "DBIT", "expense", _HIDDEN_LABEL, True),
    ("none", "refund", "5", "CRDT", "refund", _REFUND_LABEL, False),
    ("none", "same", "3", "DBIT", "expense", _FOOD_LABEL, False),
    ("mortgage", "mort", "500", "DBIT", "expense", _MORTGAGE_LABEL, False),
)


def _expected_split(ids):
    """Nets after the shared split. Keys are (one_off, label or None, category id)."""
    return {
        (False, None, ids["food"]): "10",
        (False, None, ids["none"]): "22",
        (False, None, ids["mortgage"]): "500",
        (True, _FOOD_LABEL, ids["food"]): "40",
        (True, _FOOD_LABEL, ids["none"]): "2003",
        (True, _REFUND_LABEL, ids["none"]): "-5",
    }


def _actual_split(rows):
    return {
        (row["one_off"], row["name"] if row["one_off"] else None, row["category_id"]): row["net_spending"]
        for row in rows
    }

_INSERT = (
    "INSERT INTO budget_annotations (account_id, entry_reference, owner, mode, category_id, kind, "
    "ledger_fingerprint, version, updated_at, label) VALUES ('acct', ?, 'owner', 'manual', NULL, "
    "'expense', 'fp', 1, 1.0, ?)"
)


def _fk_off(conn):
    # foreign_keys cannot change inside a transaction. The connect listener turns them on.
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.commit()


_KEPT_ROW = ("a1", "kept", "alice", "manual", None, "expense", "fp", 1)


class AnnotationLabelMigration0021Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "m0021.db"

    def test_single_head_and_receipt_lineage(self):
        cfg = Config()
        cfg.set_main_option("script_location", str(db._MIGRATIONS_DIR))
        script = ScriptDirectory.from_config(cfg)
        self.assertEqual(script.get_heads(), ["0026"])
        hues = script.get_revision("0026")
        self.assertEqual(hues.down_revision, "0025")
        devices = script.get_revision("0025")
        self.assertEqual(devices.down_revision, "0024")
        gmail = script.get_revision("0024")
        self.assertIsNotNone(gmail)
        self.assertEqual(gmail.down_revision, "0023")
        household = script.get_revision("0023")
        self.assertIsNotNone(household)
        self.assertEqual(household.down_revision, "0022")
        receipts = script.get_revision("0022")
        self.assertIsNotNone(receipts)
        self.assertEqual(receipts.down_revision, "0021")
        label = script.get_revision("0021")
        self.assertIsNotNone(label)
        self.assertEqual(label.down_revision, "0020")

    def _database_at_0020(self, *, leftover_temp=False):
        engine = db.make_engine(self.path)
        try:
            db._run_sqlite_migration(
                engine, lambda connection: command.upgrade(db._alembic_config(connection), "0020"),
            )
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_connections "
                    "(id, owner, application_id, bank_name, country, enabled, next_attempt, failures) "
                    "VALUES ('c1', 'alice', 'app', 'bank', 'PL', 1, 0, 0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_accounts "
                    "(id, connection_id, owner, identification_hash, enabled, balances_json) "
                    "VALUES ('a1', 'c1', 'alice', 'hash', 1, '{}')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_transactions "
                    "(account_id, entry_reference, payload_json, observed_at, uncertain) "
                    "VALUES ('a1', 'kept', '{}', 1, 0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_annotations "
                    "(account_id, entry_reference, owner, mode, category_id, kind, ledger_fingerprint, "
                    "version, updated_at) "
                    "VALUES ('a1', 'kept', 'alice', 'manual', NULL, 'expense', 'fp', 1, 1)")
        finally:
            engine.dispose()
        if leftover_temp:
            with closing(sqlite3.connect(self.path)) as raw:
                raw.execute('CREATE TABLE "_alembic_tmp_budget_annotations" (id TEXT)')
                raw.commit()

    def _assert_integrity_and_no_batch_temp(self):
        with closing(sqlite3.connect(self.path)) as raw:
            integrity = [row[0] for row in raw.execute("PRAGMA integrity_check")]
            leftovers = [row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'")]
        self.assertEqual(integrity, ["ok"])
        self.assertEqual(leftovers, [])

    def _inbound_annotation_fks(self):
        """Tables whose foreign keys name ``budget_annotations`` as the parent."""
        with closing(sqlite3.connect(self.path)) as raw:
            tables = [row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            inbound = []
            for table in tables:
                for fk in raw.execute(f'PRAGMA foreign_key_list("{table}")'):
                    if fk[2] == "budget_annotations":
                        inbound.append((table, fk[3], fk[4]))
        return inbound

    def _kept_row(self):
        with closing(sqlite3.connect(self.path)) as raw:
            return raw.execute(
                "SELECT account_id, entry_reference, owner, mode, category_id, kind, "
                "ledger_fingerprint, version FROM budget_annotations WHERE entry_reference = 'kept'"
            ).fetchone()

    def test_init_db_downgrade_upgrade_preserves_the_annotation(self):
        self._database_at_0020()
        observed = []
        real_upgrade = command.upgrade

        def spy(config, revision):
            connection = config.attributes["connection"]
            raw = connection.connection.dbapi_connection
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                contender.isolation_level = None
                with self.assertRaises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
            observed.append((
                raw.in_transaction,
                raw.isolation_level,
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar(),
                revision,
            ))
            return real_upgrade(config, revision)

        with mock.patch("hestia.db.command.upgrade", spy):
            engine, _ = db.init_db(self.path)
        self.assertEqual(observed, [(True, None, 0, "head")])
        columns = {column["name"]: column for column in inspect(engine).get_columns("budget_annotations")}
        self.assertIn("label", columns)
        self.assertTrue(columns["label"]["nullable"])
        checks = inspect(engine).get_check_constraints("budget_annotations")
        sql = " ".join(item.get("sqltext") or "" for item in checks)
        self.assertIn("trim(label)", sql)
        self.assertIn("char(10)", sql)
        self.assertIn("char(13)", sql)
        self.assertIn("char(9)", sql)
        self.assertEqual(self._inbound_annotation_fks(), [])
        with engine.connect() as conn:
            self.assertIsNone(conn.exec_driver_sql(
                "SELECT label FROM budget_annotations WHERE entry_reference='kept'").scalar())
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), "0026")
        self._assert_integrity_and_no_batch_temp()

        def insert(reference, label):
            with engine.connect() as conn:
                _fk_off(conn)
                conn.exec_driver_sql(_INSERT, (reference, label))
                conn.commit()

        insert("ok", "Remont lazienki")
        insert("eighty", "a" * 80)
        insert("blank-check", None)
        for reference, label in (
            ("empty", ""),
            ("spaces", "  "),
            ("lead", " padded"),
            ("trail", "padded "),
            ("long", "b" * 81),
            ("newline", "line\n"),
            ("cr", "line\r"),
            ("tab", "line\t"),
        ):
            with self.subTest(reference=reference), self.assertRaises(IntegrityError):
                insert(reference, label)

        # Probe rows point at an account that does not exist. foreign_key_check would
        # roll the downgrade back, so only the real annotation remains.
        with engine.connect() as conn:
            conn.exec_driver_sql("DELETE FROM budget_annotations WHERE entry_reference != 'kept'")
            conn.commit()
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

        real_downgrade = command.downgrade

        def spy_downgrade(config, revision):
            connection = config.attributes["connection"]
            raw = connection.connection.dbapi_connection
            with closing(sqlite3.connect(self.path, timeout=0)) as contender:
                contender.isolation_level = None
                with self.assertRaises(sqlite3.OperationalError):
                    contender.execute("BEGIN IMMEDIATE")
            self.assertTrue(raw.in_transaction)
            self.assertIsNone(raw.isolation_level)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 0)
            self.assertEqual(revision, "0020")
            return real_downgrade(config, revision)

        with mock.patch("hestia.db.command.downgrade", spy_downgrade):
            db.downgrade_db(self.path, "0020")
        self.assertNotIn(str(self.path), db._ENGINE_CACHE)
        with closing(sqlite3.connect(self.path)) as raw:
            names = [row[1] for row in raw.execute("PRAGMA table_info(budget_annotations)")]
            version = raw.execute("SELECT version_num FROM alembic_version").fetchone()
        self.assertNotIn("label", names)
        self.assertEqual(version, ("0020",))
        self.assertEqual(self._kept_row(), _KEPT_ROW)
        self._assert_integrity_and_no_batch_temp()

        with mock.patch("hestia.db.command.upgrade", spy):
            engine, _ = db.init_db(self.path)
        self.assertEqual(observed, [(True, None, 0, "head"), (True, None, 0, "head")])
        with engine.connect() as conn:
            self.assertIsNone(conn.exec_driver_sql(
                "SELECT label FROM budget_annotations WHERE entry_reference='kept'").scalar())
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])
            with self.assertRaises(IntegrityError):
                _fk_off(conn)
                conn.exec_driver_sql(_INSERT, ("after", ""))
                conn.commit()
        self.assertEqual(self._kept_row(), _KEPT_ROW)
        self.assertEqual(self._inbound_annotation_fks(), [])
        self._assert_integrity_and_no_batch_temp()

    def test_leftover_batch_temp_table_does_not_block_upgrade(self):
        self._database_at_0020(leftover_temp=True)
        with closing(sqlite3.connect(self.path)) as raw:
            leftovers = [row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE '!_alembic!_tmp!_%' ESCAPE '!'")]
        self.assertEqual(leftovers, ["_alembic_tmp_budget_annotations"])
        db.init_db(self.path)
        with closing(sqlite3.connect(self.path)) as raw:
            version = raw.execute("SELECT version_num FROM alembic_version").fetchone()
            label = raw.execute(
                "SELECT label FROM budget_annotations WHERE entry_reference='kept'").fetchone()
        self.assertEqual(version, ("0026",))
        self.assertEqual(label, (None,))
        self.assertEqual(self._kept_row(), _KEPT_ROW)
        self._assert_integrity_and_no_batch_temp()


class OneOffParentLimitTests(unittest.TestCase):
    def test_parent_limit_counts_a_labelled_child_and_the_one_off_row_has_no_limit(self):
        categories = [
            _cat("salary", "Wynagrodzenie"),
            _cat("transport", "Transport"),
            {**_cat("fuel", "Paliwo"), "parent_id": "transport"},
        ]
        labelled = _tx(
            "acc-a", "tyres", amount="40", kind="expense", category_id="fuel", source="manual",
            annotation={"version": 1, "mode": "manual", "category_id": "fuel", "kind": "expense",
                        "stale": False, "label": "Opony"},
        )
        plain = _tx("acc-a", "fuel-1", amount="10", kind="expense", category_id="fuel", source="manual")
        result = _eval(
            categories=categories,
            transactions=[labelled, plain],
            limits=[{"category_id": "transport", "currency": "PLN", "monthly_limit": "30",
                     "warning_percent": 80, "enabled": True}],
            settings=month_budget.MonthBudgetSettings(income_category_ids=("salary",)),
        )
        regular = {row["category_id"]: row for row in result["category_spend"] if not row["one_off"]}
        one_offs = [row for row in result["category_spend"] if row["one_off"]]
        self.assertEqual((regular["transport"]["expenses"], regular["transport"]["net_spending"]), ("0", "0"))
        self.assertEqual(regular["transport"]["limit"]["status"], "exceeded")
        self.assertEqual(regular["transport"]["limit"]["over_by"], "20")
        self.assertIsNone(regular["fuel"]["limit"])
        self.assertEqual(regular["fuel"]["expenses"], "10")
        self.assertEqual(len(one_offs), 1)
        self.assertEqual(
            (one_offs[0]["name"], one_offs[0]["expenses"], one_offs[0]["category_id"]),
            ("Opony", "40", "fuel"),
        )
        self.assertIsNone(one_offs[0]["limit"])
        self.assertEqual(len(one_offs[0]["one_off_key"]), 64)

    def test_a_fully_labelled_category_without_a_limit_drops_the_remainder_row(self):
        labelled = _tx(
            "acc-a", "tyres", amount="40", kind="expense", category_id="fuel", source="manual",
            annotation={"version": 1, "mode": "manual", "category_id": "fuel", "kind": "expense",
                        "stale": False, "label": "Opony"},
        )
        result = _eval(
            categories=[_cat("salary", "Wynagrodzenie"), _cat("fuel", "Paliwo")],
            transactions=[labelled],
            settings=month_budget.MonthBudgetSettings(income_category_ids=("salary",)),
        )
        regular = [row for row in result["category_spend"] if not row["one_off"]]
        one_offs = [row for row in result["category_spend"] if row["one_off"]]
        self.assertEqual(regular, [])
        self.assertEqual([(row["name"], row["expenses"]) for row in one_offs], [("Opony", "40")])

    def test_a_bad_one_off_secret_is_a_month_data_error(self):
        labelled = _tx(
            "acc-a", "tyres", amount="40", kind="expense", category_id="fuel", source="manual",
            annotation={"version": 1, "mode": "manual", "category_id": "fuel", "kind": "expense",
                        "stale": False, "label": "Opony"},
        )
        with self.assertRaises(month_budget.MonthDataError):
            _eval(
                categories=[_cat("salary", "Wynagrodzenie"), _cat("fuel", "Paliwo")],
                transactions=[labelled],
                settings=month_budget.MonthBudgetSettings(income_category_ids=("salary",)),
                one_off_secret=b"",
            )

    def test_split_skips_a_zero_label_and_a_label_outside_the_observed_set(self):
        rows = month_budget._split_labelled(
            [("fuel", "10", "0", "10")],
            {
                ("Pusty", "fuel"): {"expenses": ["0"], "refunds": ["0"], "members": [("acc-a", "zero")]},
                ("Inne", "other"): {"expenses": ["5"], "refunds": [], "members": [("acc-a", "out")]},
            },
            observed={"fuel"},
            one_off_secret=ONE_OFF_TEST_SECRET,
        )
        self.assertEqual(
            [(row["one_off"], row["category_id"], row["name"], row["expenses"]) for row in rows],
            [(False, "fuel", None, "10")],
        )


class OneOffLabelCoreTests(unittest.TestCase):
    def test_clean_trims_normalizes_and_clears_blank(self):
        self.assertIsNone(clean_one_off_label(None))
        self.assertIsNone(clean_one_off_label(""))
        self.assertIsNone(clean_one_off_label("   "))
        self.assertEqual(clean_one_off_label("  Remont łazienki  "), "Remont łazienki")
        self.assertEqual(clean_one_off_label("e\u0301"), "\u00e9")
        self.assertEqual(clean_one_off_label("a" * 80), "a" * 80)

    def test_clean_rejects_a_label_that_normalizes_to_a_hidden_character(self):
        with mock.patch("hestia.budget.analytics_core.unicodedata.normalize", return_value="\u200b"):
            with self.assertRaises(AnalyticsError) as error:
                clean_one_off_label("Ok")
        self.assertEqual(error.exception.code, "budget_analytics_invalid_request")
        self.assertNotIn("Ok", str(error.exception))

    def test_clean_rejects_controls_length_and_non_text(self):
        for value in ("\n", "a\nb", "\t", "\u200b", "\u202e", "a" * 81, 80, b"label"):
            with self.subTest(value=value), self.assertRaises(AnalyticsError) as error:
                clean_one_off_label(value)
            self.assertEqual((error.exception.code, str(error.exception)),
                             ("budget_analytics_invalid_request", "budget_analytics_invalid_request"))
            self.assertNotIn("label", str(error.exception))

    def test_stored_accepts_only_canonical_text(self):
        self.assertIsNone(stored_one_off_label(None))
        self.assertEqual(stored_one_off_label("Remont"), "Remont")
        for value in (" padded", "e\u0301", "", "a" * 81):
            with self.subTest(value=value), self.assertRaises(AnalyticsError):
                stored_one_off_label(value)

    def test_active_label_ignores_stale_blank_and_optional_minimum(self):
        fresh = {"stale": False, "label": "Fachowiec"}
        self.assertEqual(active_one_off_label(fresh, "2000"), "Fachowiec")
        self.assertEqual(active_one_off_label(fresh, "-2000"), "Fachowiec")
        self.assertIsNone(active_one_off_label({"stale": True, "label": "Fachowiec"}, "2000"))
        self.assertIsNone(active_one_off_label({"stale": False, "label": ""}, "2000"))
        self.assertIsNone(active_one_off_label(None, "2000"))
        self.assertIsNone(active_one_off_label(fresh, "99.99", minimum="100"))
        self.assertEqual(active_one_off_label(fresh, "100", minimum="100"), "Fachowiec")
        self.assertEqual(active_one_off_label(fresh, "-100.00", minimum="100"), "Fachowiec")
        self.assertIsNone(active_one_off_label(fresh, "not-money", minimum="100"))
        self.assertIsNone(active_one_off_label(fresh, "10", minimum="not-money"))

    def test_one_off_key_is_a_label_free_hash_of_the_member_transactions(self):
        secret = b"synthetic-one-off-secret"
        members = [("acc-a", "cash"), ("acc-a", "same")]
        first = one_off_key(members, secret)
        self.assertEqual(first, one_off_key([("acc-a", "same"), ("acc-a", "cash")], secret))
        self.assertEqual(len(first), 64)
        self.assertNotIn(_FOOD_LABEL, first)
        raw = json.dumps(sorted(members), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        plain = hashlib.sha256(b"hestia/budget/one-off-key/v1\n" + raw).hexdigest()
        self.assertNotEqual(first, plain)
        self.assertNotEqual(first, hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(first, one_off_key([("acc-a", "food-big")], secret))
        self.assertNotEqual(first, one_off_key(members, b"other-server-secret"))
        with self.assertRaises(AnalyticsError):
            one_off_key([("acc-a", None)], secret)
        for bad in (b"", None, "secret", 1):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(AnalyticsError) as error:
                one_off_key(members, bad)
            self.assertEqual(error.exception.code, "budget_analytics_data_error")
            self.assertNotIn("secret", str(error.exception))


class AnnotationLabelStoreTests(AnalyticsCase):
    def _audits(self):
        return self.rows(db.BudgetAnalyticsAudit)

    def test_label_is_stored_trimmed_and_kept_out_of_the_audit(self):
        phrase = "Remont \u0142azienki \u2013 fachowiec"
        saved = self.annotation(kind="expense", label=f"  {phrase}  ")
        self.assertEqual((saved["label"], saved["changed"], saved["version"]), (phrase, True, 1))
        self.assertEqual(self.rows(db.BudgetAnnotation)[0].label, phrase)
        again = self.annotation(kind="expense", label=phrase, expected_version=1)
        self.assertEqual((again["changed"], again["version"], again["label"]), (False, 1, phrase))
        combined = " ".join((row.before_json or "") + (row.after_json or "") for row in self._audits())
        self.assertNotIn(phrase, combined)
        self.assertNotIn("\u0142azienki", combined)
        self.assertIn('"has_label":true', self._audits()[-1].after_json)
        self.assertEqual(len(self._audits()), 1)

    def test_blank_clears_controls_do_not_write_and_auto_mode_can_keep_a_label(self):
        first = self.annotation(kind="expense", label="ATM")
        cleared = self.annotation(kind="expense", label="", expected_version=1)
        self.assertEqual((cleared["label"], cleared["changed"], cleared["version"]), (None, True, 2))
        self.assertIsNone(self.rows(db.BudgetAnnotation)[0].label)
        self.assertIn('"has_label":false', self._audits()[-1].after_json)
        self.assertNotIn("ATM", self._audits()[-1].before_json)
        noop = self.annotation(kind="expense", label="   ", expected_version=2)
        self.assertEqual((noop["changed"], noop["label"]), (False, None))
        also_null = self.annotation(kind="expense", label=None, expected_version=2)
        self.assertFalse(also_null["changed"])
        folded = self.annotation(kind="expense", label="e\u0301", expected_version=2)
        self.assertEqual(folded["label"], "\u00e9")
        before = len(self._audits())
        version = folded["version"]
        for bad in ("\n", "\u200b", "\u202e", "a" * 81, 3):
            with self.subTest(bad=type(bad).__name__), self.assertRaises(AnalyticsError):
                self.annotation(kind="expense", label=bad, expected_version=version)
        self.assertEqual(len(self._audits()), before)
        self.assertEqual(self.rows(db.BudgetAnnotation)[0].label, "\u00e9")
        auto = self.annotation(account="b", reference="credit", mode="auto", kind="auto", label="  Wyp\u0142ata  ")
        self.assertEqual((auto["mode"], auto["kind"], auto["label"], auto["version"], auto["changed"]),
                         ("auto", "auto", "Wyp\u0142ata", 1, True))
        self.assertTrue(any(row.entry_reference == "credit" and row.label == "Wyp\u0142ata"
                            for row in self.rows(db.BudgetAnnotation)))

    def test_omitted_label_stays_and_null_clears_it(self):
        phrase = "ATM"
        saved = self.annotation(kind="expense", label=phrase)
        self.assertEqual(saved["label"], phrase)
        kept = self.annotation(kind="expense", expected_version=1)
        self.assertEqual((kept["changed"], kept["label"], kept["version"]), (False, phrase, 1))
        food = self.store.create_category("owner", "Synthetic Food")
        moved = self.annotation(kind="expense", category_id=food["id"], expected_version=1)
        self.assertEqual((moved["changed"], moved["label"], moved["category_id"]), (True, phrase, food["id"]))
        cleared = self.annotation(kind="expense", category_id=food["id"], label=None, expected_version=2)
        self.assertEqual((cleared["label"], cleared["category_id"]), (None, food["id"]))
        self.assertIsNone(self.rows(db.BudgetAnnotation)[0].label)


def _series_transactions(ids):
    rows = []
    for role, reference, amount, direction, kind, label, stale in ONE_OFF_FIXTURE:
        category_id = ids[role]
        rows.append(_tx(
            "acc-a", reference, amount=amount, direction=direction, kind=kind, category_id=category_id,
            source="manual", booking_date="2026-11-11" if direction == "CRDT" else "2026-11-10",
            annotation={"version": 1, "mode": "manual", "category_id": category_id, "kind": kind,
                        "stale": stale, "label": label},
        ))
    return rows


class OneOffSeriesTests(unittest.TestCase):
    def test_labelled_rows_leave_their_category_without_changing_spent(self):
        ids = {"food": "cat-food", "none": None, "mortgage": "cat-mortgage"}
        charts = _eval_charts(transactions=_series_transactions(ids), settings=DEFAULT_SETTINGS)
        budget = charts["budget"]
        series = charts["series"]["months"][-1]["categories"]
        spent = budget["summary"]["spent"]
        expected = _expected_split(ids)
        self.assertEqual(_actual_split(series), expected)
        self.assertEqual(_actual_split(budget["category_spend"]), expected)
        self.assertEqual(month_budget.core.sum_money(row["net_spending"] for row in series), spent)
        self.assertEqual(month_budget.core.sum_money(row["net_spending"] for row in budget["category_spend"]), spent)
        self.assertEqual(budget["pace"]["daily_rate"]["numerator"], spent)
        self.assertEqual(budget["pass_through"]["mortgage_debits"], "500")
        labelled = [row for row in series if row["one_off"]]
        self.assertTrue(all(row["name"] and row["one_off_key"] for row in labelled))
        self.assertTrue(all(_FOOD_LABEL not in row["one_off_key"] and _MORTGAGE_LABEL not in row["one_off_key"]
                            for row in labelled))
        grouped = next(row for row in labelled if row["name"] == _FOOD_LABEL and row["category_id"] is None)
        self.assertEqual(
            grouped["one_off_key"], one_off_key([("acc-a", "cash"), ("acc-a", "same")], ONE_OFF_TEST_SECRET),
        )
        self.assertIsNone(next(row["one_off_key"] for row in series
                               if row["category_id"] == "cat-mortgage" and not row["one_off"]))
        keys = [row["one_off_key"] or row["category_id"] for row in budget["category_spend"]]
        self.assertEqual(len(keys), len(set(keys)))


class OneOffOverviewTests(ProjectionCase):
    def test_labelled_amounts_leave_their_category_and_keep_the_currency_total(self):
        food = self.store.create_category("owner", "Synthetic Food")
        mortgage = self.store.create_category("owner", "Synthetic mortgage")
        income = self.store.create_category("owner", "Synthetic Income")
        rent = self.store.create_category("owner", "Synthetic Rent")
        ids = {"food": food["id"], "none": None, "mortgage": mortgage["id"]}
        for role, reference, amount, direction, kind, label, stale in ONE_OFF_FIXTURE:
            changes = {"amount": amount, "booking_date": "2026-11-10"}
            if direction != "DBIT":
                changes["credit_debit_indicator"] = direction
            self.add_transaction("a", reference, **changes)
            if label is not None or ids[role] is not None or kind != "expense":
                annotation = {"kind": kind}
                if ids[role] is not None:
                    annotation["category_id"] = ids[role]
                if label is not None:
                    annotation["label"] = label
                self.annotation(reference=reference, **annotation)
            if stale:
                # The financial fingerprint covers the booking date, so the stored label no longer splits.
                self.change_transaction("a", reference, booking_date="2026-11-12")
        self.store.save_spending_limit("owner", category_id=food["id"], currency="PLN", monthly_limit="9000",
                                       warning_percent=80, enabled=True, expected_version=0)
        # Same pass-through rule as the chart series: a labelled mortgage leg stays in its category.
        config = json.dumps({"owner": {
            "income_category_ids": [income["id"]],
            "pass_through": {"rent_category_id": rent["id"], "mortgage_category_id": mortgage["id"]},
        }})
        self.now = datetime(2026, 11, 22, 12, tzinfo=timezone.utc).timestamp()
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_MONTH_CONFIG": config}):
            overview = self.store.overview("owner", "2026-11")
            budget = self.store.monthly_budget("owner", period="2026-11")

        api_contract.BudgetAnalyticsOverview.model_validate(overview)
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(budget)
        pln = overview["currencies"][0]
        categories = overview["categories"]
        expected = _expected_split(ids)
        self.assertEqual(_actual_split(categories), expected)
        self.assertEqual(_actual_split(budget["category_spend"]), expected)
        self.assertEqual(sum_money(row["net_spending"] for row in categories), pln["net_spending"])
        self.assertEqual(pln["net_spending"], budget["summary"]["spent"])
        self.assertEqual(budget["pace"]["daily_rate"]["numerator"], budget["summary"]["spent"])
        self.assertEqual(budget["pass_through"]["mortgage_debits"], "500")
        seen_one_off = False
        for row in categories:
            if row["one_off"]:
                seen_one_off = True
            elif seen_one_off:
                self.fail("a regular category follows a one-off entry")
        labelled = [row for row in categories if row["one_off"]]
        self.assertTrue(all(row["one_off_key"] and len(row["one_off_key"]) == 64 for row in labelled))
        self.assertTrue(all(phrase not in (row["one_off_key"] or "")
                            for row in categories for phrase in (_FOOD_LABEL, _MORTGAGE_LABEL, _HIDDEN_LABEL)))
        grouped = next(row for row in labelled if row["name"] == _FOOD_LABEL and row["category_id"] is None)
        account_id = self.accounts["a"]
        self.assertEqual(grouped["one_off_key"], one_off_key(
            [(account_id, "cash"), (account_id, "same")], self.store._cursor_secret))
        mortgage_row = next(row for row in categories if row["category_id"] == mortgage["id"] and not row["one_off"])
        self.assertIsNone(mortgage_row["one_off_key"])
        keys = [row["one_off_key"] if row["one_off"] else f"cat:{row['category_id']}" for row in categories]
        self.assertEqual(len(keys), len(set(keys)))
        spend_keys = [row["one_off_key"] if row["one_off"] else f"cat:{row['category_id']}"
                      for row in budget["category_spend"]]
        self.assertEqual(len(spend_keys), len(set(spend_keys)))
        shown = self.output("stale", period="2026-11")
        self.assertEqual((shown["annotation"]["stale"], shown["annotation"]["label"]), (True, _HIDDEN_LABEL))
        limits = self.store.spending_limits("owner", "2026-11")
        status = next(row for row in limits["statuses"] if row["category_id"] == food["id"])
        # A label is not a budget exemption. The spending-limits endpoint and the month-table
        # badge both count the full food amount (10 unlabelled + 40 labelled). The split row
        # still shows the remainder, 10. 9000 - 50 = 8950; a remainder-only badge would be 8990.
        self.assertEqual((status["expenses"], status["refunds"]), ("50", "0"))
        food_row = next(row for row in budget["category_spend"]
                        if row["category_id"] == food["id"] and not row["one_off"])
        self.assertEqual(food_row["expenses"], "10")
        self.assertEqual((food_row["limit"]["monthly_limit"], food_row["limit"]["remaining"]), ("9000", "8950"))
        one_off_food = next(row for row in budget["category_spend"]
                            if row["category_id"] == food["id"] and row["one_off"])
        self.assertIsNone(one_off_food["limit"])
        self.assertEqual(pln["net_spending"], "2570")

    def test_a_stored_format_character_is_a_fixed_error(self):
        # The CHECK allows a format character. The reader still rejects it and does not echo it.
        self.add_transaction("a", "bad", amount="4")
        self.annotation(reference="bad", kind="expense", label="Ok")
        with self.sessions.begin() as session:
            row = session.get(db.BudgetAnnotation, (self.accounts["a"], "bad"))
            row.label = "Ok\u200b"
        with self.assertRaises(AnalyticsError) as error:
            self.store.overview("owner", "2026-11")
        self.assertEqual(str(error.exception), "budget_analytics_invalid_request")
        self.assertNotIn("Ok", str(error.exception))


class OneOffRecurringForecastTests(ProjectionCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 22, 0, 1, tzinfo=timezone.utc).timestamp()

    def _months(self, prefix, remittance, *, label=None, stale=False):
        for month in (8, 9, 10):
            reference = f"{prefix}-{month}"
            self.add_transaction("a", reference, amount="15", booking_date=f"2026-{month:02d}-15",
                                 remittance_information=[remittance])
            if label is not None:
                self.annotation(reference=reference, kind="expense", label=label)
            if stale:
                with self.sessions.begin() as session:
                    row = session.get(db.BudgetAnnotation, (self.accounts["a"], reference))
                    row.ledger_fingerprint = "0" * 64

    def test_a_labelled_series_is_not_a_commitment_and_november_spend_still_counts_it(self):
        self._months("stream", "Synthetic Stream")
        self._months("labelled", "Synthetic Labelled", label="Wplata")
        self._months("stale", "Synthetic Stale Stream", label="Wplata", stale=True)

        recurring = self.store.recurring("owner")
        api_contract.BudgetAnalyticsRecurring.model_validate(recurring)
        labels = sorted(item["label"] for item in recurring["hints"])
        self.assertEqual((recurring["total_hints"], labels), (2, ["Synthetic Stale Stream", "Synthetic Stream"]))
        evidence = [item["entry_reference"] for hint in recurring["hints"] for item in hint["evidence"]]
        self.assertTrue(all(not reference.startswith("labelled-") for reference in evidence))

        forecast = self.store.forecast("owner")
        api_contract.BudgetAnalyticsForecast.model_validate(forecast)
        forecast_labels = {item["label"] for month in forecast["months"] for item in month["items"]}
        self.assertIn("Synthetic Stream", forecast_labels)
        self.assertIn("Synthetic Stale Stream", forecast_labels)
        self.assertNotIn("Synthetic Labelled", forecast_labels)
        self.assertNotIn("Wplata", forecast_labels)

        self.add_transaction("a", "nov-labelled", amount="80", booking_date="2026-11-10",
                             remittance_information=["November labelled"])
        self.annotation(reference="nov-labelled", kind="expense", label="Wplata")
        self.add_transaction("a", "nov-plain", amount="20", booking_date="2026-11-11",
                             remittance_information=["November plain"])
        income = self.store.create_category("owner", "Synthetic Income")
        config = json.dumps({"owner": {"income_category_ids": [income["id"]]}})
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_MONTH_CONFIG": config}):
            budget = self.store.monthly_budget("owner", period="2026-11")
        api_contract.BudgetAnalyticsMonthlyBudget.model_validate(budget)
        spent = budget["summary"]["spent"]
        self.assertEqual(spent, "100")
        self.assertEqual(budget["pace"]["daily_rate"]["numerator"], spent)
        self.assertEqual(_actual_split(budget["category_spend"]), {
            (False, None, None): "20",
            (True, "Wplata", None): "80",
        })
        self.assertEqual(sum_money(row["net_spending"] for row in budget["category_spend"]), spent)


class OneOffLabelAPITests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_api_tests.BudgetAnalyticsAPITests.rows
    token = analytics_api_tests.BudgetAnalyticsAPITests.token
    headers = analytics_api_tests.BudgetAnalyticsAPITests.headers

    async def asyncSetUp(self):
        await analytics_api_tests.BudgetAnalyticsAPITests.asyncSetUp(self)

    async def post(self, operation, body, *, user="alice", headers=None):
        return await analytics_api_tests.BudgetAnalyticsAPITests.post(
            self, operation, body, user=user, headers=headers)

    async def get(self, operation, *, params=None, user="alice", headers=None):
        return await analytics_api_tests.BudgetAnalyticsAPITests.get(
            self, operation, params=params, user=user, headers=headers)

    def annotation(self, **changes):
        return analytics_api_tests.BudgetAnalyticsAPITests.annotation(self, **changes)

    async def test_set_clear_and_reject_a_one_off_label_without_echoing_it(self):
        phrase = "Synthetic one-off"
        saved = await self.post("annotation", self.annotation(label=f"  {phrase}  "))
        self.assertEqual(saved.status, 200, await saved.text())
        body = await saved.json()
        api_contract.BudgetAnalyticsAnnotationResult.model_validate(body)
        self.assertEqual(body["annotation"]["label"], phrase)
        version = body["annotation"]["version"]
        omitted = self.annotation(expected_version=version)
        self.assertNotIn("label", omitted)
        kept = await self.post("annotation", omitted)
        self.assertEqual(kept.status, 200, await kept.text())
        kept_body = await kept.json()
        self.assertEqual((kept_body["annotation"]["changed"], kept_body["annotation"]["label"],
                          kept_body["annotation"]["version"]), (False, phrase, version))
        audits = "".join((row.before_json or "") + (row.after_json or "") for row in self.rows(db.BudgetAnalyticsAudit))
        self.assertNotIn(phrase, audits)
        self.assertIn("has_label", audits)

        transactions = await (await self.get("transactions")).json()
        row = next(item for item in transactions["transactions"] if item["entry_reference"] == self.debit["entry_reference"])
        self.assertEqual(row["annotation"]["label"], phrase)
        overview = await (await self.get("overview")).json()
        api_contract.BudgetAnalyticsOverview.model_validate(overview)
        labelled = [item for item in overview["categories"] if item.get("one_off")]
        self.assertEqual([(item["name"], item["net_spending"]) for item in labelled], [(phrase, "10")])
        currency = next(item for item in overview["currencies"] if item["currency"] == "PLN")
        same_currency = [item for item in overview["categories"] if item["currency"] == "PLN"]
        self.assertEqual(sum_money(item["net_spending"] for item in same_currency), currency["net_spending"])

        cleared = await self.post("annotation", self.annotation(label="", expected_version=version))
        self.assertIsNone((await cleared.json())["annotation"]["label"])
        version += 1
        unchanged = await self.post("annotation", self.annotation(label=None, expected_version=version))
        again = await unchanged.json()
        self.assertEqual((again["annotation"]["changed"], again["annotation"]["label"]), (False, None))

        hidden = "Synthetic hidden"
        rejected = await self.post("annotation", self.annotation(label=hidden + "\n", expected_version=version))
        self.assertEqual(rejected.status, 400)
        self.assertEqual((await rejected.json())["error"], "budget_analytics_invalid_request")
        self.assertNotIn(hidden, await rejected.text())
        too_long = await self.post("annotation", self.annotation(label="a" * 81, expected_version=version))
        self.assertEqual(too_long.status, 400)
        self.assertEqual((await too_long.json())["error"], "budget_invalid_request")
        denied = await self.post("annotation", self.annotation(label="Viewer phrase", expected_version=version), user="viewer")
        self.assertEqual(denied.status, 403)
        self.assertNotIn("Viewer phrase", await denied.text())
        self.assertNotIn("Viewer phrase", [row.label for row in self.rows(db.BudgetAnnotation)])
