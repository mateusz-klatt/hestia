"""Migration 0019, pure helpers, single-snapshot inbox views, anti-ABA CAS and HTTP API tests."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from hestia import api_contract, db
from hestia.budget import analytics_api, device_token, owner_inbox as inbox_mod
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_views import BudgetAnalyticsViews
from tests import test_budget_analytics_api as analytics_fixture
from tests.migration_head import HEAD
from tests.test_budget_analytics_store import AnalyticsCase

HEX_A = "a" * 64
HEX_B = "b" * 64
FROZEN_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp()


def _key(item):
    return {"kind": item["kind"], "subject_id": item["subject_id"], "milestone": item["milestone"]}


class OwnerInboxPureTests(unittest.TestCase):
    def test_device_entry_validates_names_and_timestamps(self):
        for name in ("Vesta", "Żółw 📱", "x" * 64):
            for moment in (0, 0.0, 1_700_000_000.5):
                entry = inbox_mod.device_entry("alice", "device-1", name, moment)
                self.assertEqual(entry["params"], {"status": "added", "device_name": name,
                                                  "date": "1970-01-01" if moment == 0 else "2023-11-14"})
        delegated = inbox_mod.device_entry("alice", "device-1", "Vesta", 0, actor="bob")
        self.assertEqual(delegated["params"]["actor"], "bob")
        self.assertNotIn("actor", inbox_mod.device_entry("alice", "device-1", "Vesta", 0, actor="alice")["params"])
        for actor in (1, "", "x" * 65, "bad\nactor"):
            with self.subTest(actor=actor), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.device_entry("alice", "device-1", "Vesta", 0, actor=actor)
        for name in (None, 1, "", "x" * 65, "bad\nname", "bad\tname", "bad\x00name", "bad\x7fname", "bad\u200bname"):
            with self.subTest(name=name), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.device_entry("alice", "device-1", name, 0)
        for moment in (None, "0", True, False, -1, -0.1, float("nan"), float("inf"), float("-inf"),
                       1e300, 10 ** 400):
            with self.subTest(moment=moment), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.device_entry("alice", "device-1", "Vesta", moment)
        with mock.patch.object(inbox_mod, "datetime") as clock:
            clock.fromtimestamp.side_effect = OSError("out of range")
            with self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.device_entry("alice", "device-1", "Vesta", 0)

    def test_opaque_ids_milestones_params_digest_sorting_and_validation(self):
        s1 = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_CONSENT, "conn-1")
        s2 = inbox_mod.opaque_subject_id("bob", inbox_mod.KIND_CONSENT, "conn-1")
        s3 = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_MORTGAGE, "conn-1")
        self.assertEqual(len({s1, s2, s3}), 3)
        self.assertRegex(s1, r"^[0-9a-f]{64}$")
        for bad in (("", inbox_mod.KIND_CONSENT, "c"), ("alice", "unknown", "c"), ("alice", inbox_mod.KIND_CONSENT, "")):
            with self.subTest(bad_subject=bad), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.opaque_subject_id(*bad)

        t1 = inbox_mod.opaque_key_token(inbox_mod.KIND_CONSENT, s1, "g0:exp100:expired")
        self.assertRegex(t1, r"^[0-9a-f]{64}$")
        for bad in (("bad", s1, "m"), (inbox_mod.KIND_CONSENT, "short", "m"), (inbox_mod.KIND_CONSENT, s1, "bad space")):
            with self.subTest(bad_token=bad), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.opaque_key_token(*bad)

        # Consent milestones include both generation and expiry tag across all 3 active statuses.
        m_att = inbox_mod.consent_milestone(0, None, "attention_required", "attention", "invalid_session")
        m_exp = inbox_mod.consent_milestone(1, 1700000000.9, "expired", 0, None)
        m_lead = inbox_mod.consent_milestone(2, 1700086400, "due_soon", 7, None)
        self.assertEqual(m_att, "g0:expnone:attention:invalid_session")
        self.assertEqual(m_exp, "g1:exp1700000000:expired")
        self.assertEqual(m_lead, "g2:exp1700086400:lead:7")
        self.assertNotEqual(
            inbox_mod.consent_milestone(1, 1700000000, "due_soon", 7, None),
            inbox_mod.consent_milestone(1, 1700003600, "due_soon", 7, None),
        )
        for bad_cm in (
            (True, 100, "expired", 0, None),
            (-1, 100, "expired", 0, None),
            (0, -1, "expired", 0, None),
            (0, float("nan"), "expired", 0, None),
            (0, True, "expired", 0, None),
            (0, 100, "attention_required", "attention", None),
            (0, 100, "attention_required", "attention", "BAD CODE"),
            (0, 100, "due_soon", 0, None),
            (0, 100, "due_soon", 366, None),
            (0, 100, "due_soon", True, None),
            (0, 100, "healthy", None, None),
            (10 ** 130, 100, "expired", 0, None),
        ):
            with self.subTest(bad_consent_milestone=bad_cm), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.consent_milestone(*bad_cm)

        self.assertEqual(inbox_mod.mortgage_milestone("2026-11-15", "overdue"), "2026-11:overdue")
        for bad_mm in (("2026-11", "overdue"), ("2026-11-15", "upcoming"), (123, "overdue")):
            with self.subTest(bad_mortgage_milestone=bad_mm), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.mortgage_milestone(*bad_mm)

        self.assertEqual(inbox_mod.spending_limit_milestone("2026-11", "exceeded"), "2026-11:exceeded")
        for bad_sm in (("2026-11-01", "exceeded"), ("2026-11", "within"), (123, "exceeded")):
            with self.subTest(bad_spending_milestone=bad_sm), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.spending_limit_milestone(*bad_sm)

        # Strict allowlisted params builders.
        cp = inbox_mod.consent_params(status="attention_required", valid_until=None, days_remaining=None,
                                      attention="invalid_session", paused=False)
        self.assertEqual(cp, {"status": "attention_required", "valid_until": None, "days_remaining": None,
                              "attention": "invalid_session", "paused": False})
        cp2 = inbox_mod.consent_params(status="due_soon", valid_until=100, days_remaining=3, attention=None, paused=True)
        self.assertEqual(cp2["valid_until"], 100.0)
        for bad_cp in (
            {"status": "healthy", "valid_until": 100, "days_remaining": 10, "attention": None, "paused": False},
            {"status": "expired", "valid_until": 100, "days_remaining": True, "attention": None, "paused": False},
            {"status": "expired", "valid_until": 100, "days_remaining": 0, "attention": "BAD", "paused": False},
            {"status": "expired", "valid_until": 100, "days_remaining": 0, "attention": None, "paused": 1},
            {"status": "expired", "valid_until": -5, "days_remaining": 0, "attention": None, "paused": False},
        ):
            with self.subTest(bad_cp=bad_cp), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.consent_params(**bad_cp)

        mp = inbox_mod.mortgage_params(status="due_today", due_date="2026-11-21", days_until_due=0,
                                       currency="PLN", freshness_stale=True)
        self.assertEqual(mp["currency"], "PLN")
        for bad_mp in (
            {"status": "completed", "due_date": "2026-11-21", "days_until_due": 0, "currency": "PLN", "freshness_stale": False},
            {"status": "overdue", "due_date": "bad", "days_until_due": -1, "currency": "PLN", "freshness_stale": False},
            {"status": "overdue", "due_date": "2026-11-21", "days_until_due": False, "currency": "PLN", "freshness_stale": False},
            {"status": "overdue", "due_date": "2026-11-21", "days_until_due": -1, "currency": "pln", "freshness_stale": False},
            {"status": "overdue", "due_date": "2026-11-21", "days_until_due": -1, "currency": "PLN", "freshness_stale": 0},
        ):
            with self.subTest(bad_mp=bad_mp), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.mortgage_params(**bad_mp)

        sp = inbox_mod.spending_limit_params(status="approaching", period="2026-11", currency="EUR",
                                             warning_percent=80, percent_used_floor=85)
        self.assertEqual(sp["percent_used_floor"], 85)
        for bad_sp in (
            {"status": "within", "period": "2026-11", "currency": "PLN", "warning_percent": 80, "percent_used_floor": 50},
            {"status": "exceeded", "period": "2026", "currency": "PLN", "warning_percent": 80, "percent_used_floor": 110},
            {"status": "exceeded", "period": "2026-11", "currency": "PL", "warning_percent": 80, "percent_used_floor": 110},
            {"status": "exceeded", "period": "2026-11", "currency": "PLN", "warning_percent": 0, "percent_used_floor": 110},
            {"status": "exceeded", "period": "2026-11", "currency": "PLN", "warning_percent": True, "percent_used_floor": 110},
            {"status": "exceeded", "period": "2026-11", "currency": "PLN", "warning_percent": 80, "percent_used_floor": -1},
            {"status": "exceeded", "period": "2026-11", "currency": "PLN", "warning_percent": 80, "percent_used_floor": True},
        ):
            with self.subTest(bad_sp=bad_sp), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.spending_limit_params(**bad_sp)

        # reads_version_digest: order-independent, int/float read_at normalized, monotonic anti-ABA.
        d_a = inbox_mod.reads_version_digest("alice", 1, [
            (inbox_mod.KIND_MORTGAGE, HEX_B, "2026-11:overdue", 100),
            (inbox_mod.KIND_CONSENT, HEX_A, "g0:exp0:expired", 200.0),
        ])
        d_b = inbox_mod.reads_version_digest("alice", 1, [
            (inbox_mod.KIND_CONSENT, HEX_A, "g0:exp0:expired", 200),
            (inbox_mod.KIND_MORTGAGE, HEX_B, "2026-11:overdue", 100.0),
        ])
        self.assertEqual(d_a, d_b)
        self.assertNotEqual(inbox_mod.reads_version_digest("alice", 0, []), inbox_mod.reads_version_digest("alice", 2, []))
        for bad_rd in (
            ("", 0, []),
            ("alice", -1, []),
            ("alice", True, []),
            ("alice", 0, [("bad", HEX_A, "m", 1.0)]),
            ("alice", 0, [(inbox_mod.KIND_CONSENT, "bad", "m", 1.0)]),
            ("alice", 0, [(inbox_mod.KIND_CONSENT, HEX_A, "bad space", 1.0)]),
            ("alice", 0, [(inbox_mod.KIND_CONSENT, HEX_A, "m", -1.0)]),
        ):
            with self.subTest(bad_rd=bad_rd), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.reads_version_digest(*bad_rd)

        # Live ref builders (local owner-known IDs only).
        self.assertEqual(inbox_mod.consent_ref(connection_id="bank-1"), {"connection_id": "bank-1"})
        self.assertEqual(inbox_mod.mortgage_ref(schedule_id="sched-1"), {"schedule_id": "sched-1"})
        self.assertEqual(inbox_mod.spending_limit_ref(category_id="cat-1", currency="PLN"),
                         {"category_id": "cat-1", "currency": "PLN"})
        for bad_id in ("", " ", "x" * 129, "bad\x00id", "bad\nid", 123):
            with self.subTest(bad_ref_id=bad_id):
                with self.assertRaises(inbox_mod.OwnerInboxDataError):
                    inbox_mod.consent_ref(connection_id=bad_id)
                with self.assertRaises(inbox_mod.OwnerInboxDataError):
                    inbox_mod.mortgage_ref(schedule_id=bad_id)
                with self.assertRaises(inbox_mod.OwnerInboxDataError):
                    inbox_mod.spending_limit_ref(category_id=bad_id, currency="PLN")
        for bad_sl_ref in (("uncategorised", "PLN"), ("cat-1", "pln"), ("cat-1", "PL"), ("cat-1", 123)):
            with self.subTest(bad_sl_ref=bad_sl_ref), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.spending_limit_ref(category_id=bad_sl_ref[0], currency=bad_sl_ref[1])

        # Deterministic sorting without created_at: unread first -> kind -> severity -> subject_id -> milestone.
        entries = [
            {"kind": inbox_mod.KIND_SPENDING, "subject_id": HEX_A, "milestone": "2026-11:approaching",
             "ref": {"category_id": "cat-1", "currency": "EUR"}, "params": sp},
            {"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_B, "milestone": "g0:exp1:expired",
             "ref": {"connection_id": "bank-2"},
             "params": inbox_mod.consent_params(status="expired", valid_until=1, days_remaining=-1, attention=None, paused=False)},
            {"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_A, "milestone": "g0:exp1:attention:invalid_session",
             "ref": {"connection_id": "bank-1"}, "params": cp},
            {"kind": inbox_mod.KIND_MORTGAGE, "subject_id": HEX_A, "milestone": "2026-11:due_today",
             "ref": {"schedule_id": "sched-1"}, "params": mp},
        ]
        built = inbox_mod.build_inbox_items(entries, {(inbox_mod.KIND_CONSENT, HEX_A, "g0:exp1:attention:invalid_session"): 50})
        self.assertEqual(
            [(i["read"], i["kind"], i["params"]["status"]) for i in built],
            [
                (False, inbox_mod.KIND_CONSENT, "expired"),
                (False, inbox_mod.KIND_MORTGAGE, "due_today"),
                (False, inbox_mod.KIND_SPENDING, "approaching"),
                (True, inbox_mod.KIND_CONSENT, "attention_required"),
            ],
        )
        self.assertEqual(built[-1]["read_at"], 50.0)
        with self.assertRaises(inbox_mod.OwnerInboxDataError):
            inbox_mod.build_inbox_items([entries[0], entries[0]], {})

        # validate_mark_items: order-preserving deduplication + strict rejections.
        deduped = inbox_mod.validate_mark_items([
            {"kind": inbox_mod.KIND_MORTGAGE, "subject_id": HEX_B, "milestone": "2026-11:overdue"},
            {"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_A, "milestone": "g0:exp1:expired"},
            {"kind": inbox_mod.KIND_MORTGAGE, "subject_id": HEX_B, "milestone": "2026-11:overdue"},
        ])
        self.assertEqual(deduped, [
            (inbox_mod.KIND_MORTGAGE, HEX_B, "2026-11:overdue"),
            (inbox_mod.KIND_CONSENT, HEX_A, "g0:exp1:expired"),
        ])
        for bad_items in (
            "not-a-list",
            [],
            [{"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_A, "milestone": f"g0:exp{i}:expired"} for i in range(101)],
            ["not-a-dict"],
            [{"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_A}],
            [{"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_A, "milestone": "m", "extra": 1}],
            [{"kind": "other", "subject_id": HEX_A, "milestone": "m"}],
            [{"kind": inbox_mod.KIND_CONSENT, "subject_id": "A" * 64, "milestone": "m"}],
            [{"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_A, "milestone": "bad space"}],
        ):
            with self.subTest(bad_items=type(bad_items).__name__), self.assertRaises(AnalyticsError) as exc:
                inbox_mod.validate_mark_items(bad_items)
            self.assertEqual(exc.exception.status, 400)


class OwnerInboxMigration0019Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "m0019.db"

    def test_0028_recovers_leftover_batch_table_in_both_directions(self):
        engine, _ = db.init_db(self.path)
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO users (username, password_hash) VALUES ('alice', 'h')")
            conn.exec_driver_sql(
                "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                "VALUES ('alice', 'mortgage_due', ?, 'due', 100)", (HEX_A,))

        for direction in ("downgrade", "upgrade"):
            with self.subTest(direction=direction):
                with engine.begin() as conn:
                    conn.exec_driver_sql('CREATE TABLE "_alembic_tmp_budget_owner_inbox_reads" (stale TEXT)')
                if direction == "downgrade":
                    db.downgrade_db(self.path, "0027")
                    expected = "0027"
                else:
                    engine, _ = db.init_db(self.path)
                    expected = HEAD
                with engine.connect() as conn:
                    self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar(), expected)
                    self.assertEqual(conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE name = '_alembic_tmp_budget_owner_inbox_reads'"
                    ).all(), [])
                    self.assertEqual(conn.exec_driver_sql(
                        "SELECT owner, kind, subject_id, milestone, read_at FROM budget_owner_inbox_reads"
                    ).one(), ("alice", "mortgage_due", HEX_A, "due", 100.0))
                    self.assertEqual(conn.exec_driver_sql("PRAGMA integrity_check").all(), [("ok",)])
                    self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
                    if direction == "upgrade":
                        self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])

    def test_device_markers_roundtrip_preserves_existing_reads(self):
        engine, _ = db.init_db(self.path)
        self.addCleanup(engine.dispose)
        insert = ("INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                  "VALUES ('alice', ?, ?, 'added', 100.0)")
        with engine.begin() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD)
            conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
            conn.exec_driver_sql(insert, ("mortgage_due", HEX_A))
            conn.exec_driver_sql(insert, ("device_added", HEX_B))

        db.downgrade_db(self.path, "0027")
        with engine.begin() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT kind FROM budget_owner_inbox_reads").scalars().all(),
                             ["mortgage_due"])
            with self.assertRaises(IntegrityError):
                conn.exec_driver_sql(insert, ("device_added", HEX_B))

        upgraded, _ = db.init_db(self.path)
        with upgraded.begin() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD)
            conn.exec_driver_sql(insert, ("device_added", HEX_B))
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])

    def test_migration_0019_zero_drift_constraints_restrictive_fk_and_roundtrip(self):
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0018")
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_analytics_audit "
                    "(owner, subject_type, subject_id, action, before_json, after_json, changed_at) "
                    "VALUES ('alice', 'owner_inbox_read', 'tok', 'mark_read', 'null', '{}', 100.0)")
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")

            inspector = inspect(engine)
            self.assertTrue({"budget_owner_inbox_state", "budget_owner_inbox_reads"} <= set(inspector.get_table_names()))
            index_names = {idx["name"] for idx in inspector.get_indexes("budget_owner_inbox_reads")}
            self.assertIn("ix_budget_owner_inbox_reads_owner", index_names)

            # Valid inserts.
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO budget_owner_inbox_state (owner, reads_revision, updated_at) "
                    "VALUES ('alice', 1, 100.0)")
                conn.exec_driver_sql(
                    "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                    "VALUES ('alice', 'consent_renewal', ?, 'g0:exp100:expired', 100.0)", (HEX_A,))

            # State CHECK + FK constraints.
            for bad_state in (("ghost", 0, 1.0), ("alice", -1, 1.0), ("alice", 1, -0.5)):
                with self.subTest(bad_state=bad_state), self.assertRaises(IntegrityError):
                    with engine.begin() as conn:
                        conn.exec_driver_sql(
                            "INSERT INTO budget_owner_inbox_state (owner, reads_revision, updated_at) "
                            "VALUES (?, ?, ?)", bad_state)

            # Reads CHECK + PK + FK constraints.
            for bad_read in (
                ("ghost", "consent_renewal", HEX_B, "m1", 1.0),
                ("alice", "unknown_kind", HEX_B, "m1", 1.0),
                ("alice", "mortgage_due", "short", "m1", 1.0),
                ("alice", "mortgage_due", "A" * 64, "m1", 1.0),
                ("alice", "mortgage_due", "g" * 64, "m1", 1.0),
                ("alice", "spending_limit", HEX_B, "", 1.0),
                ("alice", "spending_limit", HEX_B, "m" * 129, 1.0),
                ("alice", "spending_limit", HEX_B, "m1", -1.0),
                ("alice", "consent_renewal", HEX_A, "g0:exp100:expired", 2.0),  # duplicate PK
            ):
                with self.subTest(bad_read=bad_read), self.assertRaises(IntegrityError):
                    with engine.begin() as conn:
                        conn.exec_driver_sql(
                            "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                            "VALUES (?, ?, ?, ?, ?)", bad_read)

            # Restrictive FKs (no CASCADE) on both tables.
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM users WHERE username='alice'")
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM budget_owner_inbox_reads WHERE owner='alice'")
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql("DELETE FROM users WHERE username='alice'")

            # Roundtrip 0019 -> 0018 -> 0019 preserves budget_analytics_audit and re-applies CHECKs.
            with engine.begin() as conn:
                command.downgrade(db._alembic_config(conn), "0018")
            tables_after_down = set(inspect(engine).get_table_names())
            self.assertNotIn("budget_owner_inbox_reads", tables_after_down)
            self.assertNotIn("budget_owner_inbox_state", tables_after_down)
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("SELECT COUNT(*) FROM budget_analytics_audit").scalar(), 1)
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "0019")
            self.assertTrue({"budget_owner_inbox_state", "budget_owner_inbox_reads"}
                            <= set(inspect(engine).get_table_names()))
            with self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                        "VALUES ('alice', 'bad', ?, 'm1', 1.0)", (HEX_B,))
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), "head")
            with engine.connect() as conn:
                ctx = MigrationContext.configure(conn)
                self.assertEqual(compare_metadata(ctx, db.Base.metadata), [])
        finally:
            engine.dispose()


class OwnerInboxViewsAndCAS0019Tests(AnalyticsCase):
    def setUp(self):
        super().setUp()
        self.now = FROZEN_NOW
        self.views = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")

    def test_pairing_code_redeem_lists_new_device(self):
        key_file = self.path.parent / "device-rule.key"
        key_file.touch(mode=0o600)
        key_file.write_bytes(bytes(32))
        with mock.patch.dict(os.environ, {device_token._RULE_KEY_FILE: str(key_file)}):
            devices = device_token.DeviceTokenStore(self.sessions, clock=lambda: self.now)
            pair = devices.issue_code("owner")
            redeemed = devices.redeem(pair["code"], "Paired phone")

        audit, = [row for row in self.rows(db.BudgetAnalyticsAudit)
                  if row.subject_id == redeemed["token_id"]]
        self.assertEqual((audit.owner, audit.subject_type, audit.action),
                         ("owner", "receipt_device_token", device_token.NEW_DEVICE_LOGIN))
        result = self.views.inbox("owner")
        api_contract.BudgetAnalyticsInbox.model_validate(result)
        item, = [entry for entry in result["items"] if entry["kind"] == inbox_mod.KIND_DEVICE]
        self.assertEqual(item["ref"], {"token_id": redeemed["token_id"]})
        self.assertEqual(item["params"], {"device_name": "Paired phone", "status": "added", "date": "2026-11-21"})
        self.assertFalse(item["read"])
        self.assertFalse(any(entry["kind"] == inbox_mod.KIND_DEVICE
                             for entry in self.views.inbox("other")["items"]))

    def test_second_redeem_with_same_name_is_a_separate_unread_alert(self):
        key_file = self.path.parent / "device-rule.key"
        key_file.touch(mode=0o600)
        key_file.write_bytes(bytes(32))
        with mock.patch.dict(os.environ, {device_token._RULE_KEY_FILE: str(key_file)}):
            devices = device_token.DeviceTokenStore(self.sessions, clock=lambda: self.now)
            first = devices.redeem(devices.issue_code("owner")["code"], "Paired phone")
            snap = self.views.inbox("owner")
            first_item, = [entry for entry in snap["items"] if entry["kind"] == inbox_mod.KIND_DEVICE]
            marked = self.views.mark_inbox("owner", items=[_key(first_item)], read=True,
                                           expected_reads_version=snap["reads_version"])
            second = devices.redeem(devices.issue_code("owner")["code"], "Paired phone")

        self.assertNotEqual(first["token_id"], second["token_id"])
        result = self.views.inbox("owner")
        api_contract.BudgetAnalyticsInbox.model_validate(result)
        self.assertEqual(result["reads_version"], marked["reads_version"])
        by_token = {entry["ref"]["token_id"]: entry for entry in result["items"]
                    if entry["kind"] == inbox_mod.KIND_DEVICE}
        self.assertEqual(set(by_token), {first["token_id"], second["token_id"]})
        self.assertTrue(by_token[first["token_id"]]["read"])
        self.assertFalse(by_token[second["token_id"]]["read"])

    def _seed_all_three_sources(self):
        # 1. Consent renewal: expire owner's existing connection ("bank") within 5 days (lead tier 7).
        with self.sessions.begin() as session:
            conn = session.get(db.BudgetConnection, "bank")
            conn.valid_until = self.now + 5 * 86400
            conn.consent_generation = 2
            conn.attention = None
            # Add a second healthy connection (should NOT appear) and a foreign owner's expiring connection.
            session.add(db.BudgetConnection(
                id="bank-healthy", owner="owner", application_id="app", bank_name="Healthy Bank", country="PL",
                enabled=True, paused=False, control_version=1, background_enabled=False,
                valid_until=self.now + 60 * 86400, next_attempt=0, failures=0,
            ))
            session.add(db.BudgetConnection(
                id="other-bank-expiring", owner="other", application_id="app2", bank_name="Foreign Secret Bank",
                country="PL", enabled=True, paused=False, control_version=1, background_enabled=False,
                valid_until=self.now - 10, attention="invalid_session", next_attempt=0, failures=0,
            ))
        # 2. Mortgage due: overdue schedule for owner + upcoming schedule (should NOT appear).
        sched = self.views.save_mortgage_schedule(
            "owner", schedule_id=None, label="PRIVATE-MORTGAGE-LABEL", amount="3210.99", currency="PLN",
            due_day=15, start_date="2026-11-01", end_date="2026-12-31", remind_days_before=5,
            destination_kind="owner_inbox", enabled=True, expected_version=0,
        )
        self.views.save_mortgage_schedule(
            "owner", schedule_id=None, label="UPCOMING-MORTGAGE", amount="1000", currency="PLN",
            due_day=30, start_date="2026-11-01", end_date=None, remind_days_before=2,
            destination_kind="owner_inbox", enabled=True, expected_version=0,
        )
        # 3. Spending limit: exceeded in 2026-11 (using existing debit tx=100 PLN booked 2026-11-20 in AnalyticsCase)
        #    plus a within-limit category (should NOT appear).
        cat_over = self.views.create_category("owner", "SECRET-GROCERIES")
        cat_ok = self.views.create_category("owner", "SAFE-BOOKS")
        tx = self.views.transactions("owner", "2026-11")["transactions"][0]
        self.views.annotate(
            "owner", tx["account_id"], tx["entry_reference"],
            mode="manual", category_id=cat_over["id"], kind="expense",
            expected_version=0, expected_fingerprint=tx["fingerprint"],
        )
        self.views.save_spending_limit(
            "owner", category_id=cat_over["id"], currency="PLN", monthly_limit="50",
            warning_percent=80, enabled=True, expected_version=0,
        )
        self.views.save_spending_limit(
            "owner", category_id=cat_ok["id"], currency="PLN", monthly_limit="500",
            warning_percent=80, enabled=True, expected_version=0,
        )
        return sched, cat_over

    def test_pure_readonly_live_sources_anti_aba_cas_no_pruning_and_sanitized_audit(self):
        # Empty inbox read performs ZERO writes.
        empty = self.views.inbox("owner")
        self.assertEqual((empty["reads_revision"], empty["reads_count"], empty["reads_cap"], empty["unread_count"]),
                         (0, 0, 500, 0))
        self.assertEqual(empty["items"], [])
        self.assertEqual(self.rows(db.BudgetOwnerInboxState), [])
        self.assertEqual(self.rows(db.BudgetOwnerInboxRead), [])

        sched, cat_over = self._seed_all_three_sources()
        before_digest = self.views.overview("owner", "2026-11")["snapshot_digest"]

        # Read all 3 active sources in one single snapshot; zero secrets/labels/amounts/bank names leaked.
        snap = self.views.inbox("owner")
        api_contract.BudgetAnalyticsInbox.model_validate(snap)
        self.assertEqual((snap["evaluation_date"], snap["evaluation_timezone"], snap["unread_count"],
                          snap["reads_revision"], snap["reads_count"], snap["reads_cap"]),
                         ("2026-11-21", "UTC", 3, 0, 0, 500))
        self.assertEqual([item["kind"] for item in snap["items"]],
                         [inbox_mod.KIND_CONSENT, inbox_mod.KIND_MORTGAGE, inbox_mod.KIND_SPENDING])
        serialized = json.dumps(snap)
        for private in ("PRIVATE-MORTGAGE-LABEL", "3210.99", "SECRET-GROCERIES", "Foreign Secret Bank",
                        "Synthetic Bank", "other-bank-expiring"):
            self.assertNotIn(private, serialized)
        self.assertEqual(self.rows(db.BudgetOwnerInboxState), [])  # pure read-only!

        consent_item, mortgage_item, spending_item = snap["items"]
        self.assertEqual(consent_item["ref"], {"connection_id": "bank"})
        self.assertEqual(mortgage_item["ref"], {"schedule_id": sched["id"]})
        self.assertEqual(spending_item["ref"], {"category_id": cat_over["id"], "currency": "PLN"})
        self.assertTrue(consent_item["milestone"].startswith("g2:exp"))
        self.assertTrue(consent_item["milestone"].endswith(":lead:7"))
        self.assertEqual(mortgage_item["milestone"], "2026-11:overdue")
        self.assertEqual(spending_item["milestone"], "2026-11:exceeded")

        # Mark one item read -> rev 0 -> 1.
        rev0_version = snap["reads_version"]
        m1 = self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=True, expected_reads_version=rev0_version)
        self.assertEqual((m1["changed"], m1["changed_count"], m1["reads_revision"], m1["reads_count"]), (True, 1, 1, 1))
        after1 = self.views.inbox("owner")
        self.assertEqual((after1["reads_version"], after1["unread_count"]), (m1["reads_version"], 2))
        self.assertEqual([i["read"] for i in after1["items"]], [False, False, True])  # unread sorted before read!

        # Overview snapshot_digest is untouched (inbox tables outside _source_digest).
        self.assertEqual(self.views.overview("owner", "2026-11")["snapshot_digest"], before_digest)

        # Repeat mark read at current version -> no-op, rev stays 1, no extra audit row.
        audit_count = len(self.rows(db.BudgetAnalyticsAudit))
        noop1 = self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=True,
                                      expected_reads_version=m1["reads_version"])
        self.assertEqual((noop1["changed"], noop1["changed_count"], noop1["reads_revision"]), (False, 0, 1))
        self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), audit_count)

        # Strict CAS BEFORE no-op: stale version fails 409 even when desired state already matches!
        with self.assertRaises(AnalyticsError) as stale:
            self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=True, expected_reads_version=rev0_version)
        self.assertEqual((stale.exception.code, stale.exception.status), ("budget_analytics_conflict", 409))

        # Anti-ABA proof: mark (0->1) then unmark (1->2) returns to empty read set,
        # yet reads_version differs from rev0 and rev0 CAS is rejected!
        u1 = self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=False,
                                   expected_reads_version=m1["reads_version"])
        self.assertEqual((u1["changed"], u1["changed_count"], u1["reads_revision"], u1["reads_count"]), (True, 1, 2, 0))
        self.assertNotEqual(u1["reads_version"], rev0_version)
        with self.assertRaises(AnalyticsError) as aba:
            self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=True, expected_reads_version=rev0_version)
        self.assertEqual((aba.exception.code, aba.exception.status), ("budget_analytics_conflict", 409))

        # No-op unmark on already-unmarked item preserves revision and audit.
        u_noop = self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=False,
                                       expected_reads_version=u1["reads_version"])
        self.assertEqual((u_noop["changed"], u_noop["reads_revision"]), (False, 2))

        # Mark-all-read batch across all 3 kinds (with duplicate key deduplicated!) -> single rev bump (2 -> 3).
        all_batch = self.views.mark_inbox(
            "owner",
            items=[_key(consent_item), _key(mortgage_item), _key(spending_item), _key(consent_item)],
            read=True,
            expected_reads_version=u1["reads_version"],
        )
        self.assertEqual((all_batch["changed"], all_batch["changed_count"], all_batch["reads_revision"],
                          all_batch["reads_count"]), (True, 3, 3, 3))
        self.assertEqual(self.views.inbox("owner")["unread_count"], 0)

        # Sanitized audit: zero labels, amounts, bank names or raw milestones; shared Audit table untouched.
        self.assertEqual(self.rows(db.Audit), [])
        inbox_audits = [r for r in self.rows(db.BudgetAnalyticsAudit) if r.subject_type == "owner_inbox_read"]
        self.assertEqual(len(inbox_audits), 5)  # 1 mark + 1 unmark + 3 mark-all
        for row in inbox_audits:
            self.assertRegex(row.subject_id, r"^[0-9a-f]{64}$")
            combined = row.before_json + row.after_json
            self.assertEqual(set(json.loads(row.after_json)), {"key_token", "kind", "reads_revision", "read"})
            for secret in ("PRIVATE-MORTGAGE-LABEL", "3210.99", "SECRET-GROCERIES", "2026-11:overdue", "g2:exp",
                           sched["id"], cat_over["id"]):
                self.assertNotIn(secret, combined)

        # Live semantics + NO pruning in v1:
        # Mark mortgage 2026-11 paid -> alert disappears immediately; reads_count stays 3;
        # unmarking mortgage paid brings back 2026-11:overdue STILL marked read (not resurrected as unread)!
        self.views.set_mortgage_payment("owner", schedule_id=sched["id"], month="2026-11", paid=True, expected_version=1)
        resolved = self.views.inbox("owner")
        self.assertEqual([i["kind"] for i in resolved["items"]], [inbox_mod.KIND_CONSENT, inbox_mod.KIND_SPENDING])
        self.assertEqual(resolved["reads_count"], 3)  # durable dedupe receipt NOT pruned!
        # No-op mark read on already-marked (now inactive) key still succeeds at current reads_version.
        still_noop = self.views.mark_inbox("owner", items=[_key(mortgage_item)], read=True,
                                           expected_reads_version=resolved["reads_version"])
        self.assertFalse(still_noop["changed"])
        # Unmarking mortgage payment restores alert as read=True.
        self.views.set_mortgage_payment("owner", schedule_id=sched["id"], month="2026-11", paid=False, expected_version=2)
        returned = self.views.inbox("owner")
        restored_m = next(i for i in returned["items"] if i["kind"] == inbox_mod.KIND_MORTGAGE)
        self.assertTrue(restored_m["read"])

        # Consent generation bump or valid_until expiry change produces a NEW unread milestone!
        with self.sessions.begin() as session:
            conn = session.get(db.BudgetConnection, "bank")
            conn.consent_generation = 3
        gen_bumped = self.views.inbox("owner")
        gen_c = next(i for i in gen_bumped["items"] if i["kind"] == inbox_mod.KIND_CONSENT)
        self.assertFalse(gen_c["read"])
        self.assertTrue(gen_c["milestone"].startswith("g3:exp"))

        # Unread resolved alert disappears automatically too (e.g., renewing valid_until far in future).
        with self.sessions.begin() as session:
            session.get(db.BudgetConnection, "bank").valid_until = self.now + 90 * 86400
        self.assertNotIn(inbox_mod.KIND_CONSENT, [i["kind"] for i in self.views.inbox("owner")["items"]])

        # Owner isolation: "other" sees only its own expiring bank, cannot mark "owner"'s keys.
        other_inbox = self.views.inbox("other")
        self.assertEqual(len(other_inbox["items"]), 1)
        self.assertEqual(other_inbox["items"][0]["params"]["status"], "attention_required")
        with self.assertRaises(AnalyticsError) as foreign_mark:
            self.views.mark_inbox("other", items=[_key(mortgage_item)], read=True,
                                  expected_reads_version=other_inbox["reads_version"])
        self.assertEqual((foreign_mark.exception.code, foreign_mark.exception.status),
                         ("budget_analytics_source_changed", 409))

    def test_atomic_batch_rejection_cap_500_cas_race_and_error_mappings(self):
        self._seed_all_three_sources()
        snap = self.views.inbox("owner")
        c_item, m_item, _ = snap["items"]

        # Atomic batch rejection: 1 active + 1 inactive unmarked key -> 409 source_changed & ZERO rows inserted!
        inactive_key = {"kind": inbox_mod.KIND_CONSENT, "subject_id": HEX_B, "milestone": "g9:exp0:expired"}
        with self.assertRaises(AnalyticsError) as partial:
            self.views.mark_inbox("owner", items=[_key(c_item), inactive_key], read=True,
                                  expected_reads_version=snap["reads_version"])
        self.assertEqual((partial.exception.code, partial.exception.status), ("budget_analytics_source_changed", 409))
        self.assertEqual(self.rows(db.BudgetOwnerInboxRead), [])
        self.assertEqual(self.rows(db.BudgetOwnerInboxState), [])

        # Invalid arguments to mark_inbox -> 400.
        for bad_kwargs in (
            {"items": [_key(c_item)], "read": 1, "expected_reads_version": snap["reads_version"]},
            {"items": [_key(c_item)], "read": True, "expected_reads_version": "not-hex"},
        ):
            with self.subTest(bad_kwargs=bad_kwargs), self.assertRaises(AnalyticsError) as bad_req:
                self.views.mark_inbox("owner", **bad_kwargs)
            self.assertEqual(bad_req.exception.status, 400)

        # Cap 500 on create only: seed 499 read markers, verify 2-item batch fails 413 atomically,
        # 1-item create reaches 500, next create fails 413, no-op & unmark succeed at 500!
        with self.sessions.begin() as session:
            for idx in range(499):
                session.add(db.BudgetOwnerInboxRead(
                    owner="owner", kind=inbox_mod.KIND_MORTGAGE, subject_id=f"{idx:064x}",
                    milestone="2025-01:overdue", read_at=100.0,
                ))
        snap_499 = self.views.inbox("owner")
        self.assertEqual(snap_499["reads_count"], 499)
        with self.assertRaises(AnalyticsError) as cap_batch:
            self.views.mark_inbox("owner", items=[_key(c_item), _key(m_item)], read=True,
                                  expected_reads_version=snap_499["reads_version"])
        self.assertEqual((cap_batch.exception.code, cap_batch.exception.status), ("budget_analytics_limit", 413))
        self.assertEqual(self.views.inbox("owner")["reads_count"], 499)

        at_500 = self.views.mark_inbox("owner", items=[_key(c_item)], read=True,
                                       expected_reads_version=snap_499["reads_version"])
        self.assertEqual(at_500["reads_count"], 500)
        with self.assertRaises(AnalyticsError) as cap_one:
            self.views.mark_inbox("owner", items=[_key(m_item)], read=True,
                                  expected_reads_version=at_500["reads_version"])
        self.assertEqual((cap_one.exception.code, cap_one.exception.status), ("budget_analytics_limit", 413))
        # No-op on existing marker succeeds at cap 500.
        noop_500 = self.views.mark_inbox("owner", items=[_key(c_item)], read=True,
                                         expected_reads_version=at_500["reads_version"])
        self.assertFalse(noop_500["changed"])
        # Unmark of an inactive historical marker succeeds at cap 500 (frees a slot!).
        freed = self.views.mark_inbox(
            "owner", items=[{"kind": inbox_mod.KIND_MORTGAGE, "subject_id": f"{0:064x}", "milestone": "2025-01:overdue"}],
            read=False, expected_reads_version=at_500["reads_version"],
        )
        self.assertEqual(freed["reads_count"], 499)

        # Concurrent two-writer threading.Barrier(2) CAS race!
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()
        shared_version = freed["reads_version"]

        def racer(item, flag):
            writer = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-only")
            barrier.wait(timeout=5)
            try:
                res = writer.mark_inbox("owner", items=[_key(item)], read=flag, expected_reads_version=shared_version)
                out = ("ok", res["reads_revision"])
            except AnalyticsError as exc:
                out = ("err", exc.code, exc.status)
            with lock:
                outcomes.append(out)

        threads = [
            threading.Thread(target=racer, args=(m_item, True)),
            threading.Thread(target=racer, args=(c_item, False)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
        self.assertEqual(len([o for o in outcomes if o[0] == "ok"]), 1)
        self.assertEqual([o for o in outcomes if o[0] == "err"], [("err", "budget_analytics_conflict", 409)])

        # Disabled-only spending limit branch + evaluator/integrity 413 & 500 mappings (100% branch coverage).
        with self.sessions.begin() as session:
            for lim in session.scalars(select(db.BudgetCategoryLimit).where(db.BudgetCategoryLimit.owner == "owner")):
                lim.enabled = False
        self.assertNotIn(inbox_mod.KIND_SPENDING, [i["kind"] for i in self.views.inbox("owner")["items"]])

        from hestia.budget import renewal_reminders as rr, spending_limits as sl
        with mock.patch.object(rr, "evaluate_renewal_reminders", side_effect=rr.RenewalLimitError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_limit", 413))
        with mock.patch.object(rr, "evaluate_renewal_reminders", side_effect=rr.RenewalInputError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_data_error", 500))
        with mock.patch.object(sl, "evaluate_spending_limits", side_effect=sl.SpendingLimitCapError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_limit", 413))
        with mock.patch.object(sl, "evaluate_spending_limits", side_effect=sl.SpendingLimitInputError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_data_error", 500))
        with mock.patch.object(inbox_mod, "consent_milestone", side_effect=inbox_mod.OwnerInboxDataError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_data_error", 500))
        with mock.patch.object(inbox_mod, "reads_version_digest", side_effect=inbox_mod.OwnerInboxDataError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_data_error", 500))
        with mock.patch.object(inbox_mod, "build_inbox_items", side_effect=inbox_mod.OwnerInboxDataError()):
            with self.assertRaises(AnalyticsError) as exc:
                self.views.inbox("owner")
            self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_data_error", 500))
        # Orphaned category_id on spending limit (simulated cross-owner mismatch in DB) -> 500 data error.
        other_cat = self.views.create_category("other", "Other Cat")
        with self.sessions.begin() as session:
            first_lim = session.scalars(select(db.BudgetCategoryLimit).where(db.BudgetCategoryLimit.owner == "owner")).first()
            first_lim.category_id = other_cat["id"]
        with self.assertRaises(AnalyticsError) as exc:
            self.views.inbox("owner")
        self.assertEqual((exc.exception.code, exc.exception.status), ("budget_analytics_data_error", 500))


class OwnerInboxHTTPAPI0019Tests(unittest.IsolatedAsyncioTestCase):
    rows = analytics_fixture.BudgetAnalyticsAPITests.rows
    token = analytics_fixture.BudgetAnalyticsAPITests.token
    headers = analytics_fixture.BudgetAnalyticsAPITests.headers
    assert_private_headers = analytics_fixture.BudgetAnalyticsAPITests.assert_private_headers
    get = analytics_fixture.BudgetAnalyticsAPITests.get
    post = analytics_fixture.BudgetAnalyticsAPITests.post

    async def asyncSetUp(self):
        await analytics_fixture.BudgetAnalyticsAPITests.asyncSetUp(self)

    async def test_inbox_http_roundtrip_strict_validation_100_item_body_and_digest_isolation(self):
        overview_before = await (await self.get("overview")).json()
        before_digest = overview_before["snapshot_digest"]

        # Reject query parameters on GET /inbox.
        bad_q = await self.get("inbox", params={"kind": "consent_renewal"})
        self.assertEqual(bad_q.status, 400)
        self.assert_private_headers(bad_q)

        # Make alice's bank connection due_soon under frozen evaluator clock.
        with self.sessions.begin() as session:
            conn = session.get(db.BudgetConnection, "bank")
            conn.valid_until = FROZEN_NOW + 3 * 86400
            conn.consent_generation = 1

        real_store = analytics_api._store

        def frozen_store(secret):
            store = real_store(secret)
            store.clock = lambda: FROZEN_NOW
            return store

        with mock.patch.object(analytics_api, "_store", side_effect=frozen_store):
            r0 = await self.get("inbox")
            self.assertEqual(r0.status, 200, await r0.text())
            self.assert_private_headers(r0)
            d0 = await r0.json()
            api_contract.BudgetAnalyticsInbox.model_validate(d0)
            self.assertEqual((d0["evaluation_date"], d0["unread_count"], d0["reads_revision"]), ("2026-11-21", 1, 0))
            item = d0["items"][0]

            # Mark read via POST /inbox-mark.
            m_resp = await self.post("inbox-mark", {
                "items": [_key(item)], "read": True, "expected_reads_version": d0["reads_version"],
            })
            self.assertEqual(m_resp.status, 200, await m_resp.text())
            self.assert_private_headers(m_resp)
            m_body = await m_resp.json()
            api_contract.BudgetAnalyticsInboxMarkResult.model_validate(m_body)
            self.assertEqual((m_body["inbox"]["changed"], m_body["inbox"]["reads_revision"]), (True, 1))

            # Stale reads_version -> 409 budget_analytics_conflict.
            stale = await self.post("inbox-mark", {
                "items": [_key(item)], "read": False, "expected_reads_version": d0["reads_version"],
            })
            self.assertEqual(stale.status, 409)
            self.assertEqual((await stale.json())["error"], "budget_analytics_conflict")

            # Unmark at current reads_version -> 200, rev 2.
            u_resp = await self.post("inbox-mark", {
                "items": [_key(item)], "read": False, "expected_reads_version": m_body["inbox"]["reads_version"],
            })
            self.assertEqual(u_resp.status, 200, await u_resp.text())
            u_body = await u_resp.json()
            self.assertEqual(u_body["inbox"]["reads_revision"], 2)

            # 100-item batch (~25 KB) passes JSON body limit and reaches handler (409 source_changed, not 413 body)!
            hundred = [{"kind": inbox_mod.KIND_CONSENT, "subject_id": f"{i:064x}", "milestone": f"g0:exp{i}:expired"}
                       for i in range(100)]
            big_resp = await self.post("inbox-mark", {
                "items": hundred, "read": True, "expected_reads_version": u_body["inbox"]["reads_version"],
            })
            self.assertEqual(big_resp.status, 409)
            self.assertEqual((await big_resp.json())["error"], "budget_analytics_source_changed")

            # Strict request DTO rejections -> 400.
            for bad_body in (
                {"items": [], "read": True, "expected_reads_version": d0["reads_version"]},
                {"items": hundred + [hundred[0]], "read": True, "expected_reads_version": d0["reads_version"]},
                {"items": [_key(item)], "read": 1, "expected_reads_version": d0["reads_version"]},
                {"items": [_key(item)], "read": True, "expected_reads_version": d0["reads_version"], "extra": 1},
                {"items": [{**_key(item), "milestone": "bad space"}], "read": True,
                 "expected_reads_version": d0["reads_version"]},
            ):
                bad = await self.post("inbox-mark", bad_body)
                self.assertEqual(bad.status, 400)
                self.assert_private_headers(bad)

        overview_after = await (await self.get("overview")).json()
        self.assertEqual(overview_after["snapshot_digest"], before_digest)


if __name__ == "__main__":
    unittest.main()
