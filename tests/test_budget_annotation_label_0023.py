"""Migration 0023 and one-off label normalization.

down_revision is 0019, the current master head. Re-parent after 0020, 0021 and 0022 at merge.
The label text is owner-private: these tests use synthetic phrases and never log them.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from hestia import db
from hestia.budget.analytics_core import (
    AnalyticsError, active_one_off_label, clean_one_off_label, stored_one_off_label,
)

_INSERT = (
    "INSERT INTO budget_annotations (account_id, entry_reference, owner, mode, category_id, kind, "
    "ledger_fingerprint, version, updated_at, label) VALUES ('acct', ?, 'owner', 'manual', NULL, "
    "'expense', 'fp', 1, 1.0, ?)"
)


def _fk_off(conn):
    # foreign_keys cannot change inside a transaction. The connect listener turns them on.
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.commit()


class AnnotationLabelMigration0023Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "m0023.db"

    def test_upgrade_downgrade_check_and_zero_drift(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
            with engine.connect() as conn:
                _fk_off(conn)
                conn.execute(db.text(
                    "INSERT INTO budget_annotations (account_id, entry_reference, owner, mode, category_id, kind, "
                    "ledger_fingerprint, version, updated_at) VALUES ('acct', 'kept', 'owner', 'manual', NULL, "
                    "'expense', 'fp', 1, 1.0)"))
                conn.commit()
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0023")

            columns = {column["name"]: column for column in inspect(engine).get_columns("budget_annotations")}
            self.assertIn("label", columns)
            self.assertTrue(columns["label"]["nullable"])
            checks = inspect(engine).get_check_constraints("budget_annotations")
            self.assertTrue(any("trim(label)" in (item.get("sqltext") or "") for item in checks))

            with engine.connect() as conn:
                self.assertIsNone(conn.exec_driver_sql(
                    "SELECT label FROM budget_annotations WHERE entry_reference='kept'").scalar())

            def insert(reference, label):
                with engine.connect() as conn:
                    _fk_off(conn)
                    conn.exec_driver_sql(_INSERT, (reference, label))
                    conn.commit()

            insert("ok", "Remont lazienki")
            insert("eighty", "a" * 80)
            insert("blank-check", None)
            # SQLite trim() strips spaces only, so a newline satisfies the CHECK. The app rejects it.
            insert("newline", "line\n")
            for reference, label in (
                ("empty", ""),
                ("spaces", "  "),
                ("lead", " padded"),
                ("trail", "padded "),
                ("long", "b" * 81),
            ):
                with self.subTest(reference=reference), self.assertRaises(IntegrityError):
                    insert(reference, label)

            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0019")
            self.assertNotIn("label", {column["name"] for column in inspect(engine).get_columns("budget_annotations")})
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql(
                    "SELECT COUNT(*) FROM budget_annotations WHERE entry_reference='kept'").scalar(), 1)
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")
            with engine.connect() as conn:
                context = MigrationContext.configure(conn)
                self.assertEqual(compare_metadata(context, db.Base.metadata), [])
            with engine.connect() as conn:
                self.assertIsNone(conn.exec_driver_sql(
                    "SELECT label FROM budget_annotations WHERE entry_reference='kept'").scalar())
                with self.assertRaises(IntegrityError):
                    _fk_off(conn)
                    conn.exec_driver_sql(_INSERT, ("after", ""))
                    conn.commit()
        finally:
            engine.dispose()


class OneOffLabelCoreTests(unittest.TestCase):
    def test_clean_trims_normalizes_and_clears_blank(self):
        self.assertIsNone(clean_one_off_label(None))
        self.assertIsNone(clean_one_off_label(""))
        self.assertIsNone(clean_one_off_label("   "))
        self.assertEqual(clean_one_off_label("  Remont łazienki  "), "Remont łazienki")
        self.assertEqual(clean_one_off_label("e\u0301"), "\u00e9")
        self.assertEqual(clean_one_off_label("a" * 80), "a" * 80)

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
