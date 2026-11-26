"""Unit tests for migration 0034, minimal HMAC store, generation fence, read-time expiry, and immediate purge."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError

from hestia import db
from hestia.appointments import (
    CONFIG_FILE,
    FLAG,
    SECRETS_DIR,
    WARSAW_TZ,
    AppointmentsConfigError,
    AppointmentSlot,
    BookedAppointment,
    PollPolicy,
    WatchCriteria,
    slot_digest,
)
from hestia.appointments.config import load_runtime_settings, load_settings
from hestia.appointments.store import (
    DISABLED_AT_META_KEY,
    GENERATION_META_KEY,
    AppointmentsStore,
    AppointmentsStoreError,
    PurgeResult,
    _is_appointment_inbox_audit,
    diff_with_stored_hashes,
    prune_expired_appointments,
)
from hestia.budget import owner_inbox as inbox_mod
from tests.migration_head import HEAD

SYNTHETIC_HMAC = b"synthetic-appointments-hmac-key-32bytes!"
SYNTHETIC_TOKEN = "synthetic-cmp-bearer-token-0001"
HEX_A = "a" * 64
HEX_B = "b" * 64
DAY = 86400.0


def _slot(
    dt: datetime,
    *,
    doctor_id: str = "doc-secret-101",
    location_id: str = "loc-secret-01",
    specialty_id: str = "spec-secret-01",
    doctor_label: str | None = "Dr Synthetic Hidden",
    location_label: str | None = "Hidden Portal Clinic Name",
) -> AppointmentSlot:
    return AppointmentSlot(
        provider="cmp",
        start=dt,
        doctor_id=doctor_id,
        location_id=location_id,
        specialty_id=specialty_id,
        doctor_label=doctor_label,
        location_label=location_label,
    )


def _watch(
    watch_id: str = "cmp-watch-1",
    *,
    booked_slot: BookedAppointment | None = None,
    only_better: bool = False,
) -> WatchCriteria:
    return WatchCriteria(
        watch_id=watch_id,
        provider="cmp",
        date_from=date(2026, 12, 1),
        date_to=date(2026, 12, 31),
        specialty_id="spec-secret-01",
        doctor_ids=("doc-secret-101", "doc-secret-102"),
        location_ids=("loc-secret-01",),
        booked_slot=booked_slot,
        only_better_than_booked=only_better,
        label="Checkup A",
        specialty_label="Spec Label A",
        clinic_label="Clinic Label A",
    )


class Migration0034Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "appt0034.db"

    @staticmethod
    def _down_revision_0034() -> str:
        cfg = Config()
        cfg.set_main_option("script_location", str(db._MIGRATIONS_DIR))
        script = ScriptDirectory.from_config(cfg).get_revision("0034")
        assert script is not None and isinstance(script.down_revision, str)
        return script.down_revision

    def test_migration_0034_up_down_constraints_and_leftover_recovery(self) -> None:
        """Round-trip from 0034's parent and verify the CHECK constraints."""
        down_rev = self._down_revision_0034()
        engine = db.make_engine(self.path)
        try:
            with engine.begin() as conn:
                command.upgrade(db._alembic_config(conn), down_rev)
                conn.exec_driver_sql("INSERT INTO users (username, password_hash, role) VALUES ('alice', 'h', 'admin')")
                conn.exec_driver_sql(
                    "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                    "VALUES ('alice', 'mortgage_due', ?, '2026-11:overdue', 100.0)",
                    (HEX_A,),
                )
            # Before 0034: appointment inbox kinds MUST be rejected by ck_budget_owner_inbox_reads_kind.
            for appt_kind in ("appointment_slot", "appointment_attention"):
                with self.subTest(phase="pre_upgrade", kind=appt_kind), self.assertRaises(IntegrityError):
                    with engine.begin() as conn:
                        conn.exec_driver_sql(
                            "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                            "VALUES ('alice', ?, ?, 'new_slot', 100.0)",
                            (appt_kind, HEX_B),
                        )
        finally:
            engine.dispose()

        engine, _ = db.init_db(self.path)
        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD)
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])
            tables = set(inspect(engine).get_table_names())
            self.assertTrue({
                "appointments_watch_state",
                "appointments_active_slots",
                "appointments_inbox_alerts",
                "appointments_poller_state",
            } <= tables)
            # P1-2: no plaintext IDs, no human message column, and no exact slot_time column anywhere!
            watch_cols = {c["name"] for c in inspect(engine).get_columns("appointments_watch_state")}
            slot_cols = {c["name"] for c in inspect(engine).get_columns("appointments_active_slots")}
            alert_cols = {c["name"] for c in inspect(engine).get_columns("appointments_inbox_alerts")}
            self.assertTrue({"specialty_id", "doctor_ids_json", "location_ids_json"}.isdisjoint(watch_cols))
            self.assertNotIn("slot_time", slot_cols)
            self.assertIn("pending_notify", slot_cols)
            self.assertTrue({"message", "slot_time", "better_than_booked"}.isdisjoint(alert_cols))
            self.assertEqual(
                alert_cols,
                {"owner", "watch_id", "slot_hash", "slot_date", "alert_kind", "created_at", "expires_at"},
            )

        # Valid metadata-only inserts into all 4 tables + generation marker + appointment inbox reads.
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO appointments_watch_state (owner, watch_id, updated_at) "
                "VALUES ('alice', 'w1', 1000.0)"
            )
            conn.exec_driver_sql(
                "INSERT INTO appointments_active_slots "
                "(owner, watch_id, slot_hash, slot_date, first_seen_at, last_seen_at, expires_at) "
                "VALUES ('alice', 'w1', ?, '2026-12-10', 1000.0, 1000.0, 2000.0)",
                (HEX_A,),
            )
            conn.exec_driver_sql(
                "INSERT INTO appointments_inbox_alerts "
                "(owner, watch_id, slot_hash, slot_date, alert_kind, created_at, expires_at) "
                "VALUES ('alice', 'w1', ?, '2026-12-10', 'better_slot', 1000.0, 2000.0)",
                (HEX_A,),
            )
            conn.exec_driver_sql(
                "INSERT INTO appointments_poller_state (owner, next_watch_index, updated_at) "
                "VALUES ('alice', 2, 1000.0)"
            )
            conn.exec_driver_sql(
                "INSERT INTO app_meta (key, value) VALUES (?, '3')",
                (GENERATION_META_KEY,),
            )
            conn.exec_driver_sql(
                "INSERT INTO app_meta (key, value) VALUES (?, '1000.0')",
                (DISABLED_AT_META_KEY,),
            )
            conn.exec_driver_sql(
                "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                "VALUES ('alice', 'appointment_slot', ?, 'new_slot', 1000.0)",
                (HEX_B,),
            )
            conn.exec_driver_sql(
                "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                "VALUES ('alice', 'appointment_attention', ?, 'circuit_open:1000', 1000.0)",
                (HEX_B,),
            )

        # Constraint enforcement on all tables.
        bad_watches = (
            ("ghost", "w2", "cmp", "ok", None, 0, 100.0),
            ("alice", "", "cmp", "ok", None, 0, 100.0),
            ("alice", "w2", "luxmed", "ok", None, 0, 100.0),
            ("alice", "w2", "cmp", "unknown", None, 0, 100.0),
            ("alice", "w2", "cmp", "backoff", "raw_text", 1, 100.0),
            ("alice", "w2", "cmp", "ok", None, -1, 100.0),
            ("alice", "w2", "cmp", "ok", None, 0, -1.0),
        )
        for params in bad_watches:
            with self.subTest(bad_watch=params), self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO appointments_watch_state "
                        "(owner, watch_id, provider, last_status, last_error_code, consecutive_failures, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        params,
                    )

        bad_slots = (
            ("alice", "missing", HEX_B, "2026-12-10", 0, 10.0, 10.0, 20.0),
            ("alice", "w1", "short", "2026-12-10", 0, 10.0, 10.0, 20.0),
            ("alice", "w1", "G" * 64, "2026-12-10", 0, 10.0, 10.0, 20.0),
            ("alice", "w1", HEX_B, "2026/12/10", 0, 10.0, 10.0, 20.0),
            ("alice", "w1", HEX_B, "2026-12-10", 2, 10.0, 10.0, 20.0),
            ("alice", "w1", HEX_B, "2026-12-10", 0, 10.0, 20.0, 15.0),
        )
        for params in bad_slots:
            with self.subTest(bad_slot=params), self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO appointments_active_slots "
                        "(owner, watch_id, slot_hash, slot_date, pending_notify, first_seen_at, last_seen_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        params,
                    )

        for bad_alert in (
            ("alice", "w1", HEX_B, "2026-12-10", "new_slot", 10.0, 20.0),  # missing parent slot FK
            ("alice", "w1", HEX_A, "2026/12/10", "new_slot", 10.0, 20.0),  # invalid date format
            ("alice", "w1", HEX_A, "2026-12-10", "unknown", 10.0, 20.0),  # invalid alert_kind
            ("alice", "w1", HEX_A, "2026-12-10", "new_slot", -1.0, 20.0),  # negative created_at
            ("alice", "w1", HEX_A, "2026-12-10", "new_slot", 20.0, 10.0),  # expires < created
        ):
            with self.subTest(bad_alert=bad_alert), self.assertRaises(IntegrityError):
                with engine.begin() as conn:
                    conn.exec_driver_sql(
                        "INSERT INTO appointments_inbox_alerts "
                        "(owner, watch_id, slot_hash, slot_date, alert_kind, created_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        bad_alert,
                    )

        with self.assertRaises(IntegrityError):
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "INSERT INTO appointments_poller_state (owner, next_watch_index, updated_at) "
                    "VALUES ('alice', -1, 10.0)"
                )

        # Downgrade 0034 -> down_rev (with leftover _alembic_tmp recovery) drops tables, generation meta, and reads.
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE "_alembic_tmp_budget_owner_inbox_reads" (stale TEXT)')
        db.downgrade_db(self.path, down_rev)

        with closing(sqlite3.connect(self.path)) as raw:
            self.assertEqual(raw.execute("SELECT version_num FROM alembic_version").fetchall(), [(down_rev,)])
            tables_down = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for tbl in (
                "appointments_watch_state",
                "appointments_active_slots",
                "appointments_inbox_alerts",
                "appointments_poller_state",
                "_alembic_tmp_budget_owner_inbox_reads",
            ):
                self.assertNotIn(tbl, tables_down)
            self.assertEqual(
                raw.execute(
                    "SELECT key FROM app_meta WHERE key IN (?, ?)",
                    (GENERATION_META_KEY, DISABLED_AT_META_KEY),
                ).fetchall(),
                [],
            )
            self.assertEqual(
                raw.execute("SELECT kind FROM budget_owner_inbox_reads").fetchall(),
                [("mortgage_due",)],
            )
            # After downgrade: CHECK constraint MUST reject appointment kinds again!
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute(
                    "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                    "VALUES ('alice', 'appointment_slot', ?, 'new_slot', 100.0)",
                    (HEX_B,),
                )
            self.assertEqual(raw.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(raw.execute("PRAGMA foreign_key_check").fetchall(), [])

        # Re-upgrade down_rev -> 0034 cleanly and verify constraint round-trip accepts appointment kinds again.
        engine, _ = db.init_db(self.path)
        with engine.begin() as conn:
            self.assertEqual(conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one(), HEAD)
            self.assertEqual(compare_metadata(MigrationContext.configure(conn), db.metadata), [])
            conn.exec_driver_sql(
                "INSERT INTO budget_owner_inbox_reads (owner, kind, subject_id, milestone, read_at) "
                "VALUES ('alice', 'appointment_slot', ?, 'better_slot', 100.0)",
                (HEX_B,),
            )
            self.assertEqual(conn.exec_driver_sql("PRAGMA foreign_key_check").all(), [])


class RuntimeConfigAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.secrets_dir = Path(self.tmp.name) / "data" / "secrets"
        self.secrets_dir.mkdir(parents=True)
        self.cfg_path = self.secrets_dir / "appointments.json"
        self.hmac_path = self.secrets_dir / "appointments-hmac.key"
        self.token_path = self.secrets_dir / "appointments-cmp.token"
        self.hmac_path.write_bytes(SYNTHETIC_HMAC)
        self.token_path.write_bytes(SYNTHETIC_TOKEN.encode("ascii"))
        self.hmac_path.chmod(0o400)
        self.token_path.chmod(0o400)
        self.payload = {
            "owner": "alice",
            "auth_mode": "bearer",
            "auth_token_env": "TEST_CMP_TOKEN_FILE",
            "hmac_key_env": "TEST_APPT_HMAC_FILE",
            "poll_policy": {
                "active_start": "06:00",
                "active_end": "23:00",
                "circuit_breaker_threshold": 3,
                "failure_backoff_base_seconds": 300,
                "failure_backoff_max_seconds": 1800,
                "slot_ttl_seconds": 86400,
                "alert_ttl_seconds": 3600,
                "notify_window_seconds": 600,
                "notify_max_per_window": 4,
            },
            "watches": [
                {
                    "watch_id": "cmp-1",
                    "provider": "cmp",
                    "specialty_id": "spec-01",
                    "specialty_label": "Cardiology A",
                    "clinic_label": "Clinic Center",
                    "date_from": "2026-12-01",
                    "date_to": "2026-12-31",
                }
            ],
        }
        self.cfg_path.write_text(json.dumps(self.payload), encoding="utf-8")
        self.cfg_path.chmod(0o600)
        self.env = {
            FLAG: "1",
            CONFIG_FILE: str(self.cfg_path),
            SECRETS_DIR: str(self.secrets_dir),
            "TEST_CMP_TOKEN_FILE": str(self.token_path),
            "TEST_APPT_HMAC_FILE": str(self.hmac_path),
        }

    def test_runtime_settings_enforce_0400_secrets_and_secrets_dir(self) -> None:
        settings = load_runtime_settings(environ=self.env)
        assert settings is not None
        self.assertEqual(settings.owner, "alice")
        self.assertEqual(settings.hmac_secret, SYNTHETIC_HMAC)
        self.assertEqual(settings.poll_policy.alert_ttl_seconds, 3600)

        self.hmac_path.chmod(0o600)
        with self.assertRaises(AppointmentsConfigError):
            load_runtime_settings(environ=self.env)
        self.hmac_path.chmod(0o400)

        self.token_path.chmod(0o600)
        with self.assertRaises(AppointmentsConfigError):
            load_runtime_settings(environ=self.env)
        self.token_path.chmod(0o400)

        no_hmac = {k: v for k, v in self.payload.items() if k != "hmac_key_env"}
        self.cfg_path.write_text(json.dumps(no_hmac), encoding="utf-8")
        with self.assertRaises(AppointmentsConfigError):
            load_runtime_settings(environ=self.env)
        self.cfg_path.write_text(json.dumps(self.payload), encoding="utf-8")

        with self.assertRaises(AppointmentsConfigError):
            load_runtime_settings(environ=self.env | {SECRETS_DIR: "relative/secrets"})
        outside = Path(self.tmp.name) / "outside.key"
        outside.write_bytes(SYNTHETIC_HMAC)
        outside.chmod(0o400)
        with self.assertRaises(AppointmentsConfigError):
            load_runtime_settings(environ=self.env | {"TEST_APPT_HMAC_FILE": str(outside)})

        env_no_dir = {k: v for k, v in self.env.items() if k != SECRETS_DIR}
        self.assertIsNotNone(load_runtime_settings(environ=env_no_dir))
        self.assertIsNone(load_runtime_settings(environ=self.env | {FLAG: "0"}))

    def test_quiet_hours_backoff_and_label_validation(self) -> None:
        policy = PollPolicy(
            active_start=time(6, 0),
            active_end=time(23, 0),
            failure_backoff_base_seconds=300,
            failure_backoff_max_seconds=1000,
        )
        before_open = datetime(2026, 12, 10, 5, 30, tzinfo=WARSAW_TZ).timestamp()
        at_open = datetime(2026, 12, 10, 6, 0, tzinfo=WARSAW_TZ).timestamp()
        at_close = datetime(2026, 12, 10, 23, 0, tzinfo=WARSAW_TZ).timestamp()
        self.assertFalse(policy.is_active_window(before_open))
        self.assertEqual(policy.seconds_until_active(before_open), 1800.0)
        self.assertTrue(policy.is_active_window(at_open))
        self.assertEqual(policy.seconds_until_active(at_open), 0.0)
        self.assertFalse(policy.is_active_window(at_close))
        self.assertEqual(policy.seconds_until_active(at_close), 7 * 3600.0)

        self.assertEqual(policy.failure_backoff_delay(1), 300.0)
        self.assertEqual(policy.failure_backoff_delay(2), 600.0)
        self.assertEqual(policy.failure_backoff_delay(3), 1000.0)
        self.assertEqual(policy.failure_backoff_delay(1, retry_after=750.0), 750.0)
        self.assertEqual(policy.failure_backoff_delay(1, retry_after=float("nan")), 300.0)
        self.assertEqual(policy.failure_backoff_delay(1, retry_after=-10.0), 300.0)

        for bad_policy in (
            {"active_start": time(23, 0), "active_end": time(6, 0)},
            {"active_start": time(6, 0), "active_end": time(6, 0)},
            {"active_start": time(6, 0, tzinfo=timezone.utc)},
            {"active_end": "23:00"},
            {"circuit_breaker_threshold": 0},
            {"failure_backoff_base_seconds": 30},
            {"failure_backoff_base_seconds": 600, "failure_backoff_max_seconds": 300},
            {"slot_ttl_seconds": 100},
            {"alert_ttl_seconds": 100},
            {"slot_ttl_seconds": 3600, "alert_ttl_seconds": 7200},
            {"notify_window_seconds": 10},
            {"notify_max_per_window": 0},
        ):
            with self.subTest(bad_policy=bad_policy), self.assertRaises(AppointmentsConfigError):
                PollPolicy(**bad_policy)

        for bad_label in ({"specialty_label": "\n"}, {"clinic_label": "   "}):
            with self.subTest(bad_label=bad_label), self.assertRaises(AppointmentsConfigError):
                WatchCriteria(
                    watch_id="w1",
                    provider="cmp",
                    date_from=date(2026, 12, 1),
                    date_to=date(2026, 12, 10),
                    specialty_id="spec-01",
                    **bad_label,
                )

        minimal_policy = self.payload | {"poll_policy": {"min_interval_seconds": 600}}
        self.cfg_path.write_text(json.dumps(minimal_policy), encoding="utf-8")
        self.assertEqual(load_settings(environ=self.env).poll_policy.active_start, time(6, 0))


class AppointmentsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.path = Path(self.directory.name) / "store.db"
        self.engine, self.sessions = db.init_db(self.path)
        with self.sessions.begin() as session:
            session.add(db.User(username="alice", password_hash="h", role="admin"))
            session.add(db.User(username="bob", password_hash="h", role="admin"))
        self.now = datetime(2026, 12, 5, 10, 0, tzinfo=WARSAW_TZ).timestamp()
        self.store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        self.policy = PollPolicy(
            circuit_breaker_threshold=3,
            failure_backoff_base_seconds=300,
            failure_backoff_max_seconds=1200,
            slot_ttl_seconds=3 * 86400,
            alert_ttl_seconds=3600,
            notify_window_seconds=3600,
            notify_max_per_window=2,
        )

    def test_minimisation_reconcile_cursor_and_read_marker_clearing(self) -> None:
        watch = _watch(
            booked_slot=BookedAppointment(
                start=datetime(2026, 12, 15, 14, 0, tzinfo=WARSAW_TZ),
                min_improvement_minutes=60,
            ),
            only_better=True,
        )
        for bad_key in (b"short", "not-bytes"):
            with self.subTest(bad_key=bad_key), self.assertRaises(AppointmentsStoreError):
                diff_with_stored_hashes((), (), watch, hmac_key=bad_key)  # type: ignore[arg-type]

        past_slot = _slot(datetime(2026, 12, 4, 9, 0, tzinfo=WARSAW_TZ))
        early_slot = _slot(datetime(2026, 12, 10, 9, 30, tzinfo=WARSAW_TZ))
        later_slot = _slot(datetime(2026, 12, 20, 15, 0, tzinfo=WARSAW_TZ))

        r1 = self.store.reconcile_watch(
            "alice",
            watch,
            (past_slot, later_slot, early_slot),
            hmac_key=SYNTHETIC_HMAC,
            policy=self.policy,
            next_poll_at=self.now + 600,
        )
        self.assertEqual((r1.active_count, len(r1.new_slots), len(r1.better_slots), len(r1.notifiable_slots)), (2, 2, 1, 1))
        early_hash = slot_digest(watch.watch_id, early_slot, hmac_key=SYNTHETIC_HMAC)
        later_hash = slot_digest(watch.watch_id, later_slot, hmac_key=SYNTHETIC_HMAC)
        self.assertEqual(self.store.load_slot_hashes("alice", watch.watch_id), {early_hash, later_hash})

        # P1-2: ZERO plaintext doctor IDs, location IDs, specialty IDs, names, or exact slot times in SQLite!
        with closing(sqlite3.connect(self.path)) as raw:
            dump = "\n".join(raw.iterdump())
            for forbidden in (
                "doc-secret-101",
                "doc-secret-102",
                "loc-secret-01",
                "spec-secret-01",
                "Dr Synthetic Hidden",
                "Hidden Portal Clinic Name",
                "09:30",
                "15:00",
            ):
                self.assertNotIn(forbidden, dump)

        state = self.store.get_watch_state("alice", watch.watch_id)
        assert state is not None
        self.assertEqual((state.last_status, state.consecutive_failures, state.last_run_at), ("ok", 0, self.now))
        self.assertIsNone(self.store.get_watch_state("alice", "missing"))
        self.assertEqual(len(self.store.list_watch_states("alice")), 1)
        self.assertEqual(len(self.store.list_active_slots("alice")), 2)
        self.assertEqual(len(self.store.list_active_slots("alice", watch.watch_id)), 2)

        # Claim notification persists ONLY metadata (watch_id, slot_hash, slot_date, alert_kind, created_at, expires_at).
        approved, limited = self.store.claim_slot_notifications(
            "alice",
            watch.watch_id,
            ((early_hash, "2026-12-10", True), (early_hash, "2026-12-10", True), ("0" * 64, "2026-12-10", True)),
            policy=self.policy,
        )
        self.assertEqual((approved, limited), ((early_hash,), 0))
        alerts = self.store.list_inbox_alerts("alice", watch.watch_id)
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0].slot_date, alerts[0].alert_kind, alerts[0].expires_at), ("2026-12-10", "better_slot", self.now + 3600))
        self.assertEqual(len(self.store.list_inbox_alerts("alice")), 1)

        # P1-2: After BOTH reconcile AND claim_slot_notifications, ZERO HH:MM times, plaintext IDs,
        # portal names, or human labels exist anywhere in SQLite!
        with closing(sqlite3.connect(self.path)) as raw:
            dump = "\n".join(raw.iterdump())
            self.assertIsNone(re.search(r"\b\d{2}:\d{2}\b", dump))
            for forbidden in (
                "doc-secret-101",
                "doc-secret-102",
                "loc-secret-01",
                "spec-secret-01",
                "Dr Synthetic Hidden",
                "Hidden Portal Clinic Name",
                "Spec Label A",
                "Clinic Label A",
                "Checkup A",
                "Earlier appointment slot",
                "New appointment slot",
            ):
                self.assertNotIn(forbidden, dump)

        # Re-claiming existing alert row updates metadata cleanly.
        with self.sessions.begin() as session:
            session.get(db.AppointmentsActiveSlot, ("alice", watch.watch_id, early_hash)).notified_at = None
        approved_reclaim, _ = self.store.claim_slot_notifications(
            "alice", watch.watch_id, ((early_hash, "2026-12-10", False),), policy=self.policy
        )
        self.assertEqual(approved_reclaim, (early_hash,))
        self.assertEqual(self.store.list_inbox_alerts("alice", watch.watch_id)[0].alert_kind, "new_slot")
        self.assertEqual(
            self.store.claim_slot_notifications("alice", "unknown-watch", ((early_hash, "2026-12-10", True),), policy=self.policy),
            ((), 0),
        )
        self.assertEqual(
            self.store.claim_slot_notifications("alice", watch.watch_id, ((early_hash, "2026-12-10", True),), policy=self.policy),
            ((), 0),
        )

        # P2-6: Seed a read marker for early_slot, then let early_slot disappear -> alert AND read marker cleared!
        subject = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_APPOINTMENT_SLOT, f"{watch.watch_id}\0{early_hash}")
        with self.sessions.begin() as session:
            session.add(
                db.BudgetOwnerInboxRead(
                    owner="alice",
                    kind=inbox_mod.KIND_APPOINTMENT_SLOT,
                    subject_id=subject,
                    milestone="better_slot",
                    read_at=self.now,
                )
            )

        self.now += 600
        r2 = self.store.reconcile_watch("alice", watch, (), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        self.assertEqual((r2.active_count, r2.removed_count), (0, 2))
        self.assertEqual(self.store.list_inbox_alerts("alice"), ())
        with self.sessions() as session:
            self.assertIsNone(
                session.get(
                    db.BudgetOwnerInboxRead,
                    ("alice", inbox_mod.KIND_APPOINTMENT_SLOT, subject, "better_slot"),
                )
            )
            self.assertEqual(session.get(db.BudgetOwnerInboxState, "alice").reads_revision, 1)

        # Poll cursor persistence + validation (P1-4).
        self.assertEqual(self.store.get_poll_cursor("alice"), 0)
        self.store.set_poll_cursor("alice", 4)
        self.assertEqual(self.store.get_poll_cursor("alice"), 4)
        self.store.set_poll_cursor("alice", 1)
        self.assertEqual(self.store.get_poll_cursor("alice"), 1)
        for bad_cursor in (-1, True, "2"):
            with self.subTest(bad_cursor=bad_cursor), self.assertRaises(AppointmentsStoreError):
                self.store.set_poll_cursor("alice", bad_cursor)  # type: ignore[arg-type]

        # Validation errors: unknown owner & invalid clock.
        with self.assertRaises(AppointmentsStoreError):
            self.store.reconcile_watch("ghost", watch, (), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        with self.assertRaises(AppointmentsStoreError):
            AppointmentsStore(self.sessions, clock=lambda: -1.0).reconcile_watch(
                "alice", watch, (), hmac_key=SYNTHETIC_HMAC, policy=self.policy
            )

        # Direct diff_with_stored_hashes without today_warsaw + only_better_than_booked=False.
        plain_watch = _watch()
        cur, new_s, bet_s, notif_s, rem = diff_with_stored_hashes(
            (HEX_A,), (early_slot,), plain_watch, hmac_key=SYNTHETIC_HMAC
        )
        self.assertEqual((len(cur), len(new_s), bet_s, len(notif_s), rem), (1, 1, (), 1, frozenset({HEX_A})))

    def test_pending_notify_retried_until_notified_or_disappeared_or_ineligible(self) -> None:
        watch = _watch()
        s1 = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ))
        s2 = _slot(datetime(2026, 12, 10, 10, 0, tzinfo=WARSAW_TZ))
        s3 = _slot(datetime(2026, 12, 10, 11, 0, tzinfo=WARSAW_TZ))

        # Poll 1: 3 new slots, rate limit is 2 -> s3 stays pending_notify=True!
        r1 = self.store.reconcile_watch("alice", watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r1 is not None
        candidates = [(h, "2026-12-10", False) for h, _, _ in r1.notifiable_slots]
        approved, limited = self.store.claim_slot_notifications("alice", watch.watch_id, candidates, policy=self.policy)
        self.assertEqual((len(approved), limited), (2, 1))
        h3 = r1.notifiable_slots[2][0]
        by_hash = {rec.slot_hash: rec for rec in self.store.list_active_slots("alice", watch.watch_id)}
        self.assertTrue(by_hash[h3].pending_notify)
        self.assertIsNone(by_hash[h3].notified_at)

        # Poll 2 (still inside rate-limit window): s3 is STILL returned in notifiable_slots for retry!
        self.now += 600
        r2 = self.store.reconcile_watch("alice", watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r2 is not None
        self.assertEqual(r2.new_slots, ())
        self.assertEqual([h for h, _, _ in r2.notifiable_slots], [h3])
        approved2, limited2 = self.store.claim_slot_notifications(
            "alice", watch.watch_id, ((h3, "2026-12-10", False),), policy=self.policy
        )
        self.assertEqual((approved2, limited2), ((), 1))

        # Poll 3 (after window resets): s3 is retried, notified, and pending_notify clears to False!
        self.now += 3100
        r3 = self.store.reconcile_watch("alice", watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r3 is not None
        self.assertEqual([h for h, _, _ in r3.notifiable_slots], [h3])
        approved3, limited3 = self.store.claim_slot_notifications(
            "alice", watch.watch_id, ((h3, "2026-12-10", False),), policy=self.policy
        )
        self.assertEqual((approved3, limited3), ((h3,), 0))
        by_hash3 = {rec.slot_hash: rec for rec in self.store.list_active_slots("alice", watch.watch_id)}
        self.assertFalse(by_hash3[h3].pending_notify)
        self.assertIsNotNone(by_hash3[h3].notified_at)

        # Poll 4: nothing pending anymore -> notifiable_slots is empty!
        self.now += 600
        r4 = self.store.reconcile_watch("alice", watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r4 is not None
        self.assertEqual(r4.notifiable_slots, ())

        # If criteria tightens so a pending slot is no longer eligible, pending_notify clears cleanly!
        s4 = _slot(datetime(2026, 12, 25, 16, 0, tzinfo=WARSAW_TZ))
        self.store.reconcile_watch("alice", watch, (s4,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        tight_watch = _watch(
            booked_slot=BookedAppointment(start=datetime(2026, 12, 20, 14, 0, tzinfo=WARSAW_TZ)),
            only_better=True,
        )
        r_tight = self.store.reconcile_watch("alice", tight_watch, (s4,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r_tight is not None
        self.assertEqual(r_tight.notifiable_slots, ())
        self.assertFalse(self.store.list_active_slots("alice", watch.watch_id)[0].pending_notify)

        # Expired active slot pruned during reconcile_watch clears its read marker too.
        self.now += 4 * DAY
        r_exp = self.store.reconcile_watch("alice", watch, (s4,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r_exp is not None
        self.assertEqual((r_exp.expired_pruned, len(r_exp.new_slots)), (1, 1))

    def test_failure_backoff_circuit_break_auth_attention_and_recovery_clears_read_markers(self) -> None:
        watch = _watch()
        with self.assertRaises(AppointmentsStoreError):
            self.store.record_failure("alice", watch, error_code="not_a_code", policy=self.policy)

        f1 = self.store.record_failure("alice", watch, error_code="network", policy=self.policy)
        self.assertEqual((f1.last_status, f1.consecutive_failures, f1.should_notify_attention), ("backoff", 1, False))
        self.assertEqual(f1.cooldown_until, self.now + 300.0)

        self.now += 300
        f2 = self.store.record_failure("alice", watch, error_code="rate_limit", policy=self.policy, retry_after=900.0)
        self.assertEqual((f2.last_status, f2.consecutive_failures, f2.should_notify_attention), ("backoff", 2, False))

        self.now += 900
        f3 = self.store.record_failure("alice", watch, error_code="provider_http", policy=self.policy)
        self.assertEqual((f3.last_status, f3.consecutive_failures, f3.should_notify_attention), ("circuit_open", 3, True))

        # Seed a read marker on the circuit_open attention alert.
        att_subj = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_APPOINTMENT_ATTENTION, watch.watch_id)
        with self.sessions.begin() as session:
            session.add(
                db.BudgetOwnerInboxRead(
                    owner="alice",
                    kind=inbox_mod.KIND_APPOINTMENT_ATTENTION,
                    subject_id=att_subj,
                    milestone=f"circuit_open:{int(f3.attention_notified_at)}",
                    read_at=self.now,
                )
            )

        self.now += 1200
        f4 = self.store.record_failure("alice", watch, error_code="provider_http", policy=self.policy)
        self.assertEqual((f4.last_status, f4.should_notify_attention), ("circuit_open", False))

        # Recovery clears failures, attention_notified_at, AND attention read marker!
        self.now += 1200
        self.store.reconcile_watch("alice", watch, (), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        with self.sessions() as session:
            self.assertIsNone(
                session.get(
                    db.BudgetOwnerInboxRead,
                    ("alice", inbox_mod.KIND_APPOINTMENT_ATTENTION, att_subj, f"circuit_open:{int(f3.attention_notified_at)}"),
                )
            )

        a1 = self.store.record_failure(
            "alice", watch, error_code="authorization", policy=self.policy, attention="authorization"
        )
        self.assertEqual((a1.last_status, a1.should_notify_attention), ("auth_attention", True))
        self.now += 1200
        a2 = self.store.record_failure(
            "alice", watch, error_code="authorization", policy=self.policy, attention="authorization"
        )
        self.assertEqual((a2.last_status, a2.should_notify_attention), ("auth_attention", False))

    def test_immediate_purge_on_disable_ttl_reads_and_audits(self) -> None:
        w1 = _watch("w1")
        w2 = _watch("w2")
        s1 = _slot(datetime(2026, 12, 20, 9, 0, tzinfo=WARSAW_TZ))
        r1 = self.store.reconcile_watch("alice", w1, (s1,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r1 is not None
        self.store.claim_slot_notifications(
            "alice", "w1", ((r1.notifiable_slots[0][0], "2026-12-20", False),), policy=self.policy
        )
        r2 = self.store.reconcile_watch("alice", w2, (s1,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r2 is not None
        self.store.claim_slot_notifications(
            "alice", "w2", ((r2.notifiable_slots[0][0], "2026-12-20", False),), policy=self.policy
        )
        self.store.reconcile_watch("bob", w1, (s1,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        self.store.set_poll_cursor("alice", 1)
        self.store.set_poll_cursor("bob", 1)

        # Seed appointment + budget read markers AND legacy appointment + budget audit rows.
        h1 = r1.notifiable_slots[0][0]
        h2 = r2.notifiable_slots[0][0]
        subj1 = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_APPOINTMENT_SLOT, f"w1\0{h1}")
        subj2 = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_APPOINTMENT_SLOT, f"w2\0{h2}")
        with self.sessions.begin() as session:
            session.add(db.BudgetOwnerInboxState(owner="alice", reads_revision=2, updated_at=self.now))
            session.add(db.BudgetOwnerInboxRead(
                owner="alice", kind=inbox_mod.KIND_APPOINTMENT_SLOT, subject_id=subj1, milestone="new_slot", read_at=self.now
            ))
            session.add(db.BudgetOwnerInboxRead(
                owner="alice", kind=inbox_mod.KIND_APPOINTMENT_SLOT, subject_id=subj2, milestone="new_slot", read_at=self.now
            ))
            session.add(db.BudgetOwnerInboxRead(
                owner="alice", kind=inbox_mod.KIND_MORTGAGE, subject_id=HEX_A, milestone="2026-12:overdue", read_at=self.now
            ))
            session.add(db.BudgetAnalyticsAudit(
                owner="alice", subject_type="owner_inbox_read", subject_id=HEX_A, action="mark_read",
                before_json='{"kind":"appointment_slot","read":false}',
                after_json='{"kind":"appointment_slot","read":true}',
                changed_at=self.now,
            ))
            session.add(db.BudgetAnalyticsAudit(
                owner="alice", subject_type="owner_inbox_read", subject_id=HEX_B, action="mark_read",
                before_json='{"kind":"mortgage_due","read":false}',
                after_json='{"kind":"mortgage_due","read":true}',
                changed_at=self.now,
            ))
            session.add(db.BudgetAnalyticsAudit(
                owner="alice", subject_type="owner_inbox_read", subject_id=HEX_B, action="mark_read",
                before_json="not-json", after_json="not-json", changed_at=self.now,
            ))
            session.add(db.BudgetAnalyticsAudit(
                owner="bob", subject_type="owner_inbox_read", subject_id=HEX_A, action="mark_read",
                before_json='{"kind":"appointment_attention","read":false}',
                after_json='{"kind":"appointment_attention","read":true}',
                changed_at=self.now,
            ))

        # Helper unit check for malformed JSON audit rows.
        self.assertFalse(
            _is_appointment_inbox_audit(
                db.BudgetAnalyticsAudit(
                    owner="alice", subject_type="owner_inbox_read", subject_id=HEX_A,
                    action="mark_read", before_json="not-json", after_json="[]", changed_at=1.0,
                )
            )
        )

        # prune_removed_watches removes w2's watch, slot, alert, AND read marker.
        pr = self.store.prune_removed_watches("alice", ("w1",))
        self.assertEqual((pr.watches_deleted, pr.slots_deleted, pr.alerts_deleted, pr.reads_deleted), (1, 1, 1, 1))
        self.assertEqual(self.store.prune_removed_watches("alice", ("w1",)), PurgeResult(0, 0))

        # purge_owner removes bob's watch, slot, cursor, and appointment audit row (leaves alice untouched).
        bp = self.store.purge_owner("bob")
        self.assertEqual((bp.watches_deleted, bp.slots_deleted, bp.audits_deleted), (1, 1, 1))
        self.assertEqual(self.store.get_poll_cursor("bob"), 0)

        # Flag ON after short alert TTL (3601s): expired alert pruned while active slot stays;
        # after slot TTL (4 days): expired slot AND its read marker pruned while watch stays!
        self.now += 3601
        ttl_alert = prune_expired_appointments(session_factory=self.sessions, now=self.now, enabled=True)
        self.assertEqual((ttl_alert.watches_deleted, ttl_alert.slots_deleted, ttl_alert.alerts_deleted), (0, 0, 1))
        self.assertEqual(len(self.store.list_active_slots("alice", "w1")), 1)

        self.now += 4 * DAY
        ttl_slot = prune_expired_appointments(session_factory=self.sessions, now=self.now, enabled=True)
        self.assertEqual((ttl_slot.watches_deleted, ttl_slot.slots_deleted, ttl_slot.reads_deleted), (0, 1, 1))
        self.assertIsNotNone(self.store.get_watch_state("alice", "w1"))

        # Re-seed active slot + alert + appointment read marker, then disable flag ->
        # P2-7 + P1-3: IMMEDIATELY purged even 1 second later (no 7-day wait!), preserving mortgage read & audit!
        r_fresh = self.store.reconcile_watch("alice", w1, (s1,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r_fresh is not None
        self.store.claim_slot_notifications(
            "alice", "w1", ((r_fresh.notifiable_slots[0][0], "2026-12-20", False),), policy=self.policy
        )
        with self.sessions.begin() as session:
            session.add(db.BudgetOwnerInboxRead(
                owner="alice", kind=inbox_mod.KIND_APPOINTMENT_SLOT, subject_id=subj1, milestone="new_slot", read_at=self.now
            ))
        self.now += 1.0
        with self.assertLogs("hestia.appointments.store", level="INFO") as logs:
            off_res = prune_expired_appointments(self.path, now=self.now, enabled=False)
        self.assertEqual(
            off_res,
            PurgeResult(watches_deleted=1, slots_deleted=1, alerts_deleted=1, reads_deleted=1, audits_deleted=1),
        )
        self.assertIsNone(self.store.get_watch_state("alice", "w1"))
        self.assertEqual(self.store.list_active_slots("alice"), ())
        self.assertEqual(self.store.list_inbox_alerts("alice"), ())
        self.assertEqual(self.store.get_poll_cursor("alice"), 0)
        self.assertIn("reads_deleted=1 audits_deleted=1", "\n".join(logs.output))
        self.assertNotIn("alice", "\n".join(logs.output))

        # Budget mortgage read marker & audit survive intact, and reads_revision bumped monotonically!
        with self.sessions() as session:
            remaining_reads = list(session.execute(db.BudgetOwnerInboxRead.__table__.select()).all())
            self.assertEqual(len(remaining_reads), 1)
            self.assertEqual(remaining_reads[0].kind, inbox_mod.KIND_MORTGAGE)
            remaining_audits = list(session.execute(db.BudgetAnalyticsAudit.__table__.select()).all())
            self.assertEqual(len(remaining_audits), 2)
            self.assertGreater(session.get(db.BudgetOwnerInboxState, "alice").reads_revision, 2)

        # Idempotent second disable purge returns all zeroes without logging.
        self.assertEqual(prune_expired_appointments(self.path, now=self.now, enabled=False), PurgeResult(0, 0))

        # Non-existent DB path returns empty without creating file; pre-0034 DB returns empty safely.
        missing = Path(self.directory.name) / "never-created.db"
        self.assertEqual(prune_expired_appointments(missing, now=self.now, enabled=False), PurgeResult(0, 0))
        self.assertFalse(missing.exists())

        legacy_path = Path(self.directory.name) / "legacy0032.db"
        legacy_engine = db.make_engine(legacy_path)
        self.addCleanup(legacy_engine.dispose)
        with legacy_engine.begin() as conn:
            command.upgrade(db._alembic_config(conn), "0032")
        from sqlalchemy.orm import sessionmaker

        self.assertEqual(
            prune_expired_appointments(session_factory=sessionmaker(bind=legacy_engine), now=self.now),
            PurgeResult(0, 0),
        )

    def test_read_time_expired_alert_deletion_and_generation_fence(self) -> None:
        """Perf + P1 Round 3: read-first alert pruning avoids BEGIN IMMEDIATE; disabled_at + generation fence writes."""
        watch = _watch("w-fence")
        slot = _slot(datetime(2026, 12, 20, 9, 30, tzinfo=WARSAW_TZ))
        self.assertEqual(self.store.get_generation(), 0)
        self.assertIsNone(self.store.get_disabled_at())
        self.assertFalse(self.store.is_fenced(expected_generation=0))

        rec = self.store.reconcile_watch("alice", watch, (slot,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert rec is not None
        h = rec.notifiable_slots[0][0]
        self.store.claim_slot_notifications("alice", watch.watch_id, ((h, "2026-12-20", False),), policy=self.policy)

        # Perf regression: when NO alert has expired, prune_expired_alerts & list_inbox_alerts open ZERO write tx!
        recorded_sql: list[str] = []

        def _capture_sql(_conn, _cursor, statement, _params, _ctx, _executemany):
            recorded_sql.append(statement)

        event.listen(self.engine, "before_cursor_execute", _capture_sql)
        try:
            self.assertEqual(self.store.prune_expired_alerts("alice"), 0)
            self.assertEqual(len(self.store.list_inbox_alerts("alice")), 1)
            self.assertFalse(any("BEGIN IMMEDIATE" in stmt.upper() for stmt in recorded_sql))
            self.assertFalse(any(stmt.lstrip().upper().startswith("DELETE") for stmt in recorded_sql))

            # Advance past alert TTL -> list_inbox_alerts opens BEGIN IMMEDIATE and deletes the expired row!
            recorded_sql.clear()
            self.now += self.policy.alert_ttl_seconds + 1
            self.assertEqual(self.store.list_inbox_alerts("alice"), ())
            self.assertTrue(any("BEGIN IMMEDIATE" in stmt.upper() for stmt in recorded_sql))
            self.assertTrue(any(stmt.lstrip().upper().startswith("DELETE") for stmt in recorded_sql))
        finally:
            event.remove(self.engine, "before_cursor_execute", _capture_sql)
        with closing(sqlite3.connect(self.path)) as raw:
            self.assertEqual(raw.execute("SELECT COUNT(*) FROM appointments_inbox_alerts").fetchone()[0], 0)

        # Re-claim & verify prune_expired_alerts directly deletes expired rows.
        with self.sessions.begin() as session:
            session.get(db.AppointmentsActiveSlot, ("alice", watch.watch_id, h)).notified_at = None
        self.store.claim_slot_notifications("alice", watch.watch_id, ((h, "2026-12-20", False),), policy=self.policy)
        self.assertEqual(self.store.prune_expired_alerts("alice"), 0)
        self.now += self.policy.alert_ttl_seconds + 1
        self.assertEqual(self.store.prune_expired_alerts("alice"), 1)

        # Disable transition (mark_disabled) bumps generation (0 -> 1), persists disabled_at, and returns bound epoch!
        stale_gen = self.store.get_generation()
        self.assertEqual(stale_gen, 0)
        dis_gen = self.store.mark_disabled()
        self.assertEqual(dis_gen, 1)
        self.assertEqual(self.store.get_generation(), 1)
        self.assertEqual(self.store.get_disabled_at(), self.now)
        self.assertTrue(self.store.is_fenced(expected_generation=0))
        self.assertTrue(self.store.is_fenced(expected_generation=1))

        # Bound disabled sweep while still disabled at dis_gen succeeds without bumping generation or changing disabled_at!
        self.now += 60.0
        still_disabled = self.store.prune_expired(enabled=False, expected_generation=dis_gen)
        self.assertEqual(still_disabled, PurgeResult(0, 0, fenced=False))
        self.assertEqual((still_disabled.generation, self.store.get_generation()), (1, 1))
        self.assertEqual(self.store.get_disabled_at(), self.now - 60.0)

        # A second disable transition bumps generation (1 -> 2), fencing the older disabled generation=1!
        dis_gen_2 = self.store.mark_disabled()
        self.assertEqual(dis_gen_2, 2)
        stale_dis = self.store.prune_expired(enabled=False, expected_generation=dis_gen)
        self.assertEqual(stale_dis, PurgeResult(0, 0, fenced=True))
        self.assertEqual(self.store.get_generation(), 2)

        # While disabled_at is set, even matching generation=2 CANNOT write!
        self.assertFalse(self.store.set_poll_cursor("alice", 3, expected_generation=2))
        self.assertIsNone(
            self.store.reconcile_watch(
                "alice", watch, (slot,), hmac_key=SYNTHETIC_HMAC, policy=self.policy, expected_generation=2
            )
        )
        # Non-enable housekeeping cannot clear disabled_at either!
        self.assertEqual(
            prune_expired_appointments(session_factory=self.sessions, now=self.now, enabled=True),
            PurgeResult(0, 0, fenced=True),
        )
        self.assertIsNotNone(self.store.get_disabled_at())

        # Atomic enable transition (mark_enabled) clears disabled_at, bumps generation (2 -> 3), and returns 3!
        enabled_gen = self.store.mark_enabled()
        self.assertEqual(enabled_gen, 3)
        self.assertIsNone(self.store.get_disabled_at())
        self.assertEqual(self.store.get_generation(), 3)
        self.assertFalse(self.store.is_fenced(expected_generation=3))
        self.assertTrue(self.store.is_fenced(expected_generation=2))
        self.assertTrue(self.store.is_fenced(expected_generation=0))

        # Bound disabled sweep after mark_enabled is fenced (no-op, generation & disabled_at unchanged)!
        self.assertEqual(
            self.store.prune_expired(enabled=False, expected_generation=dis_gen_2),
            PurgeResult(0, 0, fenced=True),
        )
        self.assertEqual(self.store.get_generation(), 3)
        self.assertIsNone(self.store.get_disabled_at())

        # purge_owner also bumps generation (3 -> 4) without setting disabled_at.
        self.store.purge_owner("alice")
        self.assertEqual(self.store.get_generation(), 4)
        self.assertIsNone(self.store.get_disabled_at())

        # Every write with stale_gen=0 MUST become a clean no-op inside BEGIN IMMEDIATE!
        self.assertFalse(self.store.set_poll_cursor("alice", 3, expected_generation=stale_gen))
        self.assertEqual(self.store.get_poll_cursor("alice"), 0)
        self.assertIsNone(
            self.store.reconcile_watch(
                "alice", watch, (slot,), hmac_key=SYNTHETIC_HMAC, policy=self.policy, expected_generation=stale_gen
            )
        )
        self.assertEqual(
            self.store.claim_slot_notifications(
                "alice", watch.watch_id, ((h, "2026-12-20", False),), policy=self.policy, expected_generation=stale_gen
            ),
            ((), 0),
        )
        self.assertIsNone(
            self.store.record_failure(
                "alice", watch, error_code="network", policy=self.policy, expected_generation=stale_gen
            )
        )
        self.assertEqual(
            self.store.prune_removed_watches("alice", (), expected_generation=stale_gen),
            PurgeResult(0, 0),
        )
        self.assertEqual(
            self.store.prune_expired(enabled=True, expected_generation=stale_gen),
            PurgeResult(0, 0, fenced=True),
        )

        # Stop check ALSO fences writes even when disabled_at is None and generation matches!
        current_gen = self.store.get_generation()
        self.assertFalse(
            self.store.set_poll_cursor("alice", 2, expected_generation=current_gen, stop_check=lambda: True)
        )
        self.assertIsNone(
            self.store.reconcile_watch(
                "alice",
                watch,
                (slot,),
                hmac_key=SYNTHETIC_HMAC,
                policy=self.policy,
                expected_generation=current_gen,
                stop_check=lambda: True,
            )
        )
        self.assertIsNone(self.store.get_watch_state("alice", watch.watch_id))
        self.assertEqual(self.store.list_active_slots("alice"), ())
        self.assertEqual(self.store.get_poll_cursor("alice"), 0)


if __name__ == "__main__":
    unittest.main()
