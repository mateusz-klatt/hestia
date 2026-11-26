"""Unit and HTTP tests for appointments owner_inbox negotiation (0.58.0), P1-3 cap/audit isolation, and P2-6 unread reappear."""
from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from sqlalchemy import event

from hestia import api_contract, db
from hestia.appointments import (
    CONFIG_FILE,
    FLAG,
    WARSAW_TZ,
    AppointmentSlot,
    AppointmentsSettings,
    BookedAppointment,
    CmpCredentials,
    PollPolicy,
    WatchCriteria,
)
from hestia.appointments.notify import (
    AppointmentNotifier,
    active_inbox_entries,
    format_slot_message,
    resolve_config_labels,
)
from hestia.appointments.store import AppointmentsStore, FailureResult, prune_expired_appointments
from hestia.budget import analytics_api, household_views, owner_inbox as inbox_mod
from hestia.budget.analytics_core import AnalyticsError
from hestia.budget.analytics_views import BudgetAnalyticsViews
from tests import test_budget_analytics_api as analytics_fixture
from tests.test_budget_analytics_store import AnalyticsCase

SYNTHETIC_HMAC = b"synthetic-appointments-hmac-key-32bytes!"
HEX_A = "a" * 64


def _slot(
    dt: datetime,
    *,
    doctor_id: str = "doc-secret-101",
    location_id: str = "loc-secret-01",
    specialty_id: str = "spec-secret-01",
) -> AppointmentSlot:
    return AppointmentSlot(
        provider="cmp",
        start=dt,
        doctor_id=doctor_id,
        location_id=location_id,
        specialty_id=specialty_id,
        doctor_label="PORTAL-SECRET-DOCTOR-NAME",
        location_label="PORTAL-SECRET-CLINIC-NAME",
    )


class OwnerInboxAppointmentHelpersTests(unittest.TestCase):
    def test_owner_inbox_appointment_entry_validation(self) -> None:
        slot_item = inbox_mod.appointment_slot_entry(
            "alice",
            "cmp-watch-1",
            HEX_A,
            slot_date="2026-12-10",
            message="Earlier appointment slot: 2026-12-10 09:30 — Config Spec (Config Clinic)",
            better_than_booked=True,
        )
        self.assertEqual(slot_item["kind"], inbox_mod.KIND_APPOINTMENT_SLOT)
        self.assertEqual(slot_item["milestone"], "better_slot")
        self.assertEqual(slot_item["ref"], {"watch_id": "cmp-watch-1"})
        self.assertEqual(
            slot_item["params"],
            {
                "status": "better_slot",
                "date": "2026-12-10",
                "message": "Earlier appointment slot: 2026-12-10 09:30 — Config Spec (Config Clinic)",
                "better_than_booked": True,
            },
        )

        for bad_slot in (
            dict(watch_id="", slot_hash=HEX_A, slot_date="2026-12-10", message="ok", better_than_booked=False),
            dict(watch_id="w1", slot_hash="short", slot_date="2026-12-10", message="ok", better_than_booked=False),
            dict(watch_id="w1", slot_hash=HEX_A, slot_date="2026/12/10", message="ok", better_than_booked=False),
            dict(watch_id="w1", slot_hash=HEX_A, slot_date="2026-12-10", message="   ", better_than_booked=False),
            dict(watch_id="w1", slot_hash=HEX_A, slot_date="2026-12-10", message="bad\nmsg", better_than_booked=False),
            dict(watch_id="w1", slot_hash=HEX_A, slot_date="2026-12-10", message="x" * 257, better_than_booked=False),
            dict(watch_id="w1", slot_hash=HEX_A, slot_date="2026-12-10", message="ok", better_than_booked=1),
        ):
            with self.subTest(bad_slot=bad_slot), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.appointment_slot_entry("alice", **bad_slot)

        att = inbox_mod.appointment_attention_entry(
            "alice",
            "cmp-watch-1",
            status="circuit_open",
            error_code="network",
            consecutive_failures=5,
            notified_at=1700000000.5,
        )
        self.assertEqual(att["kind"], inbox_mod.KIND_APPOINTMENT_ATTENTION)
        self.assertEqual(att["milestone"], "circuit_open:1700000000")

        for bad_att in (
            dict(watch_id="w1", status="unknown", error_code="network", consecutive_failures=1, notified_at=1.0),
            dict(watch_id="w1", status="circuit_open", error_code="raw_msg", consecutive_failures=1, notified_at=1.0),
            dict(watch_id="w1", status="circuit_open", error_code="network", consecutive_failures=0, notified_at=1.0),
            dict(watch_id="w1", status="circuit_open", error_code="network", consecutive_failures=True, notified_at=1.0),
            dict(watch_id="w1", status="circuit_open", error_code="network", consecutive_failures=1, notified_at=-1.0),
        ):
            with self.subTest(bad_att=bad_att), self.assertRaises(inbox_mod.OwnerInboxDataError):
                inbox_mod.appointment_attention_entry("alice", **bad_att)

    def test_config_label_resolution_and_message_formatting(self) -> None:
        explicit = WatchCriteria(
            watch_id="w1",
            provider="cmp",
            date_from=date(2026, 12, 1),
            date_to=date(2026, 12, 31),
            specialty_id="spec-01",
            location_ids=("loc-01",),
            specialty_label="Config Spec",
            clinic_label="Config Clinic",
        )
        self.assertEqual(resolve_config_labels(explicit), ("Config Spec", "Config Clinic"))

        # P1-2: IDs fallback removed completely — generic text used when labels are omitted!
        no_labels = WatchCriteria(
            watch_id="w2",
            provider="cmp",
            date_from=date(2026, 12, 1),
            date_to=date(2026, 12, 31),
            specialty_id="spec-secret-02",
            doctor_ids=("doc-secret-02",),
            location_ids=("loc-secret-02",),
        )
        spec_fallback, clinic_fallback = resolve_config_labels(no_labels)
        self.assertEqual((spec_fallback, clinic_fallback), ("Configured specialty", "Configured clinic"))
        for forbidden in ("spec-secret-02", "doc-secret-02", "loc-secret-02", "w2"):
            self.assertNotIn(forbidden, spec_fallback)
            self.assertNotIn(forbidden, clinic_fallback)

        label_only = WatchCriteria(
            watch_id="w3",
            provider="cmp",
            date_from=date(2026, 12, 1),
            date_to=date(2026, 12, 31),
            doctor_ids=("doc-1",),
            label="Neutral Watch",
        )
        self.assertEqual(resolve_config_labels(label_only), ("Neutral Watch", "Configured clinic"))

        msg_day_only = format_slot_message(
            slot_date="2026-12-10",
            specialty_label="Config Spec",
            clinic_label="Config Clinic",
            better_than_booked=False,
        )
        msg_better_live = format_slot_message(
            slot_date="2026-12-10",
            slot_time="09:30",
            specialty_label="Config Spec",
            clinic_label="Config Clinic",
            better_than_booked=True,
        )
        self.assertEqual(msg_day_only, "New appointment slot: Config Spec (Config Clinic), 2026-12-10")
        self.assertEqual(msg_better_live, "Earlier appointment slot: Config Spec (Config Clinic), 2026-12-10 09:30")


class AppointmentsNotifyIntegrationTests(AnalyticsCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 12, 5, 10, 0, tzinfo=WARSAW_TZ).timestamp()
        self.store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        self.views = BudgetAnalyticsViews(self.sessions, clock=lambda: self.now, cursor_secret=b"synthetic-cursor-key")
        self.policy = PollPolicy(
            circuit_breaker_threshold=2,
            failure_backoff_base_seconds=300,
            failure_backoff_max_seconds=1200,
            alert_ttl_seconds=3600,
            notify_window_seconds=3600,
            notify_max_per_window=2,
        )
        self.watch = WatchCriteria(
            watch_id="cmp-checkup-1",
            provider="cmp",
            date_from=date(2026, 12, 1),
            date_to=date(2026, 12, 31),
            specialty_id="spec-secret-01",
            doctor_ids=("doc-secret-101",),
            location_ids=("loc-secret-01",),
            booked_slot=BookedAppointment(
                start=datetime(2026, 12, 20, 14, 0, tzinfo=WARSAW_TZ),
                min_improvement_minutes=60,
            ),
            only_better_than_booked=True,
            specialty_label="Config Spec A",
            clinic_label="Config Clinic A",
        )
        self.settings = AppointmentsSettings(
            owner="owner",
            cmp_base_url="https://ic-api.cmp.med.pl/api/v1",
            cmp_credentials=CmpCredentials("none", None),
            poll_policy=self.policy,
            watches=(self.watch,),
            hmac_secret=SYNTHETIC_HMAC,
        )

    def _write_config_env(self, owner: str = "owner") -> dict[str, str]:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "appointments.json"
        cfg.write_text(
            json.dumps({
                "owner": owner,
                "watches": [
                    {
                        "watch_id": self.watch.watch_id,
                        "provider": "cmp",
                        "specialty_id": "spec-secret-01",
                        "doctor_ids": ["doc-secret-101"],
                        "location_ids": ["loc-secret-01"],
                        "date_from": "2026-12-01",
                        "date_to": "2026-12-31",
                        "specialty_label": "Config Spec A",
                        "clinic_label": "Config Clinic A",
                    }
                ],
            }),
            encoding="utf-8",
        )
        cfg.chmod(0o600)
        return {FLAG: "1", CONFIG_FILE: str(cfg)}

    def test_diff_notify_dedupe_cap_isolation_reappear_unread_and_contract_gating(self) -> None:
        emitted: list[dict] = []
        notifier = AppointmentNotifier(self.store, sink=emitted.append)

        empty_rec = self.store.reconcile_watch("owner", self.watch, (), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        self.assertEqual(notifier.notify_slots("owner", self.watch, empty_rec, policy=self.policy).notifications, ())

        s1 = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ))
        s2 = _slot(datetime(2026, 12, 11, 10, 30, tzinfo=WARSAW_TZ))
        s3 = _slot(datetime(2026, 12, 12, 11, 0, tzinfo=WARSAW_TZ))
        rec1 = self.store.reconcile_watch(
            "owner", self.watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy
        )
        with self.assertLogs("hestia.appointments.notify", level="INFO") as logs:
            batch1 = notifier.notify_slots("owner", self.watch, rec1, policy=self.policy)
        self.assertEqual((len(batch1.notifications), batch1.rate_limited), (2, 1))
        self.assertEqual(len(emitted), 2)
        joined_logs = "\n".join(logs.output)
        self.assertIn("notified=2 rate_limited=1", joined_logs)
        for secret in ("PORTAL-SECRET-DOCTOR-NAME", "PORTAL-SECRET-CLINIC-NAME", "Config Spec A", "owner"):
            self.assertNotIn(secret, joined_logs)

        for notif in batch1.notifications:
            self.assertIn("Config Spec A", notif.message)
            self.assertIn("Config Clinic A", notif.message)
            self.assertNotIn("PORTAL-SECRET", notif.message)
            self.assertNotIn("doc-secret", notif.message)

        # Poll 2 inside rate-limit window: s1/s2 deduped, s3 retried as pending_notify (still rate-limited).
        rec2 = self.store.reconcile_watch(
            "owner", self.watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy
        )
        notifier_no_sink = AppointmentNotifier(self.store)
        batch2 = notifier_no_sink.notify_slots("owner", self.watch, rec2, policy=self.policy)
        self.assertEqual((len(batch2.notifications), batch2.rate_limited), (0, 1))

        # After rate-limit window expires, s3 (pending_notify) is retried and sent cleanly!
        self.now += 3601
        rec3 = self.store.reconcile_watch(
            "owner", self.watch, (s1, s2, s3), hmac_key=SYNTHETIC_HMAC, policy=self.policy
        )
        batch3 = notifier_no_sink.notify_slots("owner", self.watch, rec3, policy=self.policy)
        self.assertEqual((len(batch3.notifications), batch3.rate_limited), (1, 0))
        self.assertEqual(batch3.notifications[0].slot_date, "2026-12-12")

        # Refresh s1/s2 short-TTL alerts isn't needed for dup_only check; test dup_only skips logging cleanly.
        only_notified_rec = type(rec1)(
            watch_id=rec1.watch_id,
            active_count=1,
            new_slots=(s1,),
            better_slots=(s1,),
            notifiable_slots=(rec1.notifiable_slots[0],),
            removed_count=0,
            expired_pruned=0,
        )
        dup_only = notifier_no_sink.notify_slots("owner", self.watch, only_notified_rec, policy=self.policy)
        self.assertEqual((len(dup_only.notifications), dup_only.rate_limited), (0, 0))

        # Flag OFF -> owner_inbox returns ZERO appointment items even when include_appointments=True.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.views.inbox("owner", include_appointments=True)["items"], [])

        env = self._write_config_env() | {
            "HESTIA_PERSIST": "sqlite",
            "HESTIA_BUDGET_HTTP_ENABLED": "1",
            "HESTIA_BUDGET_ANALYTICS_ENABLED": "1",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            # P1-1: Default inbox() (include_appointments=False) NEVER returns appointment kinds!
            default_snap = self.views.inbox("owner")
            self.assertEqual(default_snap["items"], [])

            # Negotiated include_appointments=True renders message AT READ TIME from config + coarse day,
            # with NO exact HH:MM time from DB, AND deletes expired s1/s2 alert rows on read!
            self.assertEqual(len(self.rows(db.AppointmentsInboxAlert)), 3)
            snap = self.views.inbox("owner", include_appointments=True)
            api_contract.BudgetAnalyticsInbox.model_validate(snap)
            self.assertEqual(len(snap["items"]), 1)
            self.assertEqual(len(self.rows(db.AppointmentsInboxAlert)), 1)
            self.assertEqual(snap["unread_count"], 1)
            s3_item = snap["items"][0]
            self.assertEqual(
                s3_item["params"]["message"],
                "Earlier appointment slot: Config Spec A (Config Clinic A), 2026-12-12",
            )
            self.assertNotIn("11:00", s3_item["params"]["message"])
            self.assertNotIn("PORTAL-SECRET", json.dumps(snap))

            # Updating config labels changes rendered inbox text at read time with zero DB writes,
            # AND since nothing has expired now, inbox GET opens ZERO BEGIN IMMEDIATE write transactions!
            cfg_path = Path(env[CONFIG_FILE])
            updated_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            updated_cfg["watches"][0]["specialty_label"] = "Relabeled Spec"
            updated_cfg["watches"][0]["clinic_label"] = "Relabeled Clinic"
            cfg_path.write_text(json.dumps(updated_cfg), encoding="utf-8")
            inbox_sql: list[str] = []

            def _capture_inbox_sql(_conn, _cursor, statement, _params, _ctx, _executemany):
                inbox_sql.append(statement)

            event.listen(self.engine, "before_cursor_execute", _capture_inbox_sql)
            try:
                snap_relabeled = self.views.inbox("owner", include_appointments=True)
            finally:
                event.remove(self.engine, "before_cursor_execute", _capture_inbox_sql)
            self.assertFalse(any("BEGIN IMMEDIATE" in stmt.upper() for stmt in inbox_sql))
            self.assertFalse(any(stmt.lstrip().upper().startswith("DELETE") for stmt in inbox_sql))
            self.assertEqual(
                snap_relabeled["items"][0]["params"]["message"],
                "Earlier appointment slot: Relabeled Spec (Relabeled Clinic), 2026-12-12",
            )

            # P1-1: Unnegotiated mark_inbox rejects appointment kinds with 400.
            s3_key = {
                "kind": s3_item["kind"],
                "subject_id": s3_item["subject_id"],
                "milestone": s3_item["milestone"],
            }
            with self.assertRaises(AnalyticsError) as bad_ver:
                self.views.mark_inbox(
                    "owner", items=[s3_key], read=True, expected_reads_version=snap["reads_version"]
                )
            self.assertEqual((bad_ver.exception.code, bad_ver.exception.status), ("budget_analytics_invalid_request", 400))

            # P1-3: Seed 500 budget read markers (at full cap!) -> marking appointment alert STILL succeeds,
            # writes ZERO budget_analytics_audit rows, and reads_count remains 500!
            with self.sessions.begin() as session:
                for idx in range(500):
                    session.add(
                        db.BudgetOwnerInboxRead(
                            owner="owner",
                            kind=inbox_mod.KIND_MORTGAGE,
                            subject_id=f"{idx:064x}",
                            milestone="2025-01:overdue",
                            read_at=100.0,
                        )
                    )
            snap_at_cap = self.views.inbox("owner", include_appointments=True)
            self.assertEqual(snap_at_cap["reads_count"], 500)
            audits_before = len(self.rows(db.BudgetAnalyticsAudit))
            marked = self.views.mark_inbox(
                "owner",
                items=[s3_key],
                read=True,
                expected_reads_version=snap_at_cap["reads_version"],
                include_appointments=True,
            )
            self.assertEqual((marked["changed"], marked["reads_count"]), (True, 500))
            self.assertEqual(len(self.rows(db.BudgetAnalyticsAudit)), audits_before)
            after_mark = self.views.inbox("owner", include_appointments=True)
            self.assertEqual(after_mark["unread_count"], 0)
            self.assertTrue(after_mark["items"][0]["read"])

            # P2-6: s3 disappears on next poll -> alert & read marker cleared; when s3 reappears -> UNREAD!
            self.store.reconcile_watch("owner", self.watch, (), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
            self.assertEqual(self.views.inbox("owner", include_appointments=True)["items"], [])
            rec_reappear = self.store.reconcile_watch(
                "owner", self.watch, (s3,), hmac_key=SYNTHETIC_HMAC, policy=self.policy
            )
            notifier_no_sink.notify_slots("owner", self.watch, rec_reappear, policy=self.policy)
            reappeared = self.views.inbox("owner", include_appointments=True)
            self.assertEqual(len(reappeared["items"]), 1)
            self.assertFalse(reappeared["items"][0]["read"])
            self.assertEqual(reappeared["unread_count"], 1)

            # Non-owning user & household view NEVER see appointment alerts.
            self.assertEqual(self.views.inbox("other", include_appointments=True)["items"], [])
            with self.sessions.begin() as session:
                session.add(db.User(username="spouse", password_hash="h", role="viewer"))
                session.flush()
                session.add(db.BudgetHouseholdMember(member_username="spouse", owner="owner", grant_id=HEX_A))
            hh = household_views.HouseholdReader(self.sessions, cursor_secret=b"synthetic-cursor-key")
            hh.views.clock = lambda: self.now
            hh_body, _, _ = hh.read("spouse", "inbox", {}, HEX_A)
            self.assertEqual(json.loads(hh_body)["items"], [])

    def test_attention_notifications_and_active_inbox_edge_cases(self) -> None:
        emitted: list[dict] = []
        notifier = AppointmentNotifier(self.store, sink=emitted.append)

        f1 = self.store.record_failure("owner", self.watch, error_code="network", policy=self.policy)
        self.assertIsNone(notifier.notify_attention("owner", self.watch, f1))

        f2 = self.store.record_failure("owner", self.watch, error_code="network", policy=self.policy)
        with self.assertLogs("hestia.appointments.notify", level="WARNING") as logs:
            att = notifier.notify_attention("owner", self.watch, f2)
        assert att is not None
        self.assertEqual((att.status, att.error_code, att.consecutive_failures), ("circuit_open", "network", 2))
        self.assertEqual(len(emitted), 1)
        self.assertIn("status=circuit_open code=network failures=2", "\n".join(logs.output))

        f3 = self.store.record_failure("owner", self.watch, error_code="network", policy=self.policy)
        self.assertIsNone(notifier.notify_attention("owner", self.watch, f3))

        weird = FailureResult("w", "backoff", "network", 1, self.now + 10, True, self.now)
        self.assertIsNone(notifier.notify_attention("owner", self.watch, weird))

        notifier_plain = AppointmentNotifier(self.store)
        auth_fail = FailureResult(self.watch.watch_id, "auth_attention", "authorization", 1, self.now + 10, True, self.now)
        att_plain = notifier_plain.notify_attention("owner", self.watch, auth_fail)
        assert att_plain is not None
        self.assertEqual(att_plain.status, "authorization")

        # Unconfigured watch alert & attention are skipped cleanly by active_inbox_entries.
        unconfigured = WatchCriteria(
            watch_id="removed-watch",
            provider="cmp",
            date_from=date(2026, 12, 1),
            date_to=date(2026, 12, 31),
            specialty_id="spec-99",
        )
        s = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ), specialty_id="spec-99")
        r = self.store.reconcile_watch("owner", unconfigured, (s,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert r is not None
        self.store.claim_slot_notifications(
            "owner", "removed-watch", ((r.notifiable_slots[0][0], "2026-12-10", False),), policy=self.policy
        )
        self.store.record_failure(
            "owner", unconfigured, error_code="authorization", policy=self.policy, attention="authorization"
        )

        # Direct active_inbox_entries call on writable session deletes expired alerts at read time too.
        with self.sessions.begin() as session:
            entries = active_inbox_entries(session, "owner", now=self.now, settings=self.settings)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["kind"], inbox_mod.KIND_APPOINTMENT_ATTENTION)
            self.assertEqual(entries[0]["params"]["status"], "circuit_open")

            bad_entries = active_inbox_entries(
                session,
                "owner",
                now=self.now,
                environ={FLAG: "1", CONFIG_FILE: "/nonexistent/appointments.json"},
            )
            self.assertEqual(bad_entries, [])

        # Advance past alert TTL and verify active_inbox_entries(..., prune_expired=True) deletes expired row.
        self.assertEqual(len(self.rows(db.AppointmentsInboxAlert)), 1)
        with self.sessions.begin() as session:
            active_inbox_entries(
                session, "owner", now=self.now + self.policy.alert_ttl_seconds + 1, settings=self.settings
            )
        self.assertEqual(self.rows(db.AppointmentsInboxAlert), [])


class OwnerInboxContractNegotiationHTTPTests(unittest.IsolatedAsyncioTestCase):
    """P1-1 HTTP regression: pinned old clients never receive appointment inbox kinds; 0.58.0+ clients do."""

    rows = analytics_fixture.BudgetAnalyticsAPITests.rows
    token = analytics_fixture.BudgetAnalyticsAPITests.token
    headers = analytics_fixture.BudgetAnalyticsAPITests.headers
    assert_private_headers = analytics_fixture.BudgetAnalyticsAPITests.assert_private_headers
    get = analytics_fixture.BudgetAnalyticsAPITests.get
    post = analytics_fixture.BudgetAnalyticsAPITests.post

    async def asyncSetUp(self) -> None:
        await analytics_fixture.BudgetAnalyticsAPITests.asyncSetUp(self)
        self.now = datetime(2026, 12, 5, 10, 0, tzinfo=WARSAW_TZ).timestamp()
        self.store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        self.watch = WatchCriteria(
            watch_id="cmp-http-1",
            provider="cmp",
            date_from=date(2026, 12, 1),
            date_to=date(2026, 12, 31),
            specialty_id="spec-secret-01",
            doctor_ids=("doc-secret-101",),
            location_ids=("loc-secret-01",),
            specialty_label="Config Spec HTTP",
            clinic_label="Config Clinic HTTP",
        )
        self.policy = PollPolicy(alert_ttl_seconds=3600)

    async def test_pinned_old_client_never_sees_appointment_kinds_and_0_58_negotiates_them(self) -> None:
        self.store.mark_enabled()
        s1 = _slot(datetime(2026, 12, 10, 9, 30, tzinfo=WARSAW_TZ))
        rec = self.store.reconcile_watch("alice", self.watch, (s1,), hmac_key=SYNTHETIC_HMAC, policy=self.policy)
        assert rec is not None
        AppointmentNotifier(self.store).notify_slots("alice", self.watch, rec, policy=self.policy)

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Path(tmp.name) / "appointments.json"
        cfg.write_text(
            json.dumps({
                "owner": "alice",
                "watches": [{
                    "watch_id": self.watch.watch_id,
                    "provider": "cmp",
                    "specialty_id": "spec-secret-01",
                    "doctor_ids": ["doc-secret-101"],
                    "location_ids": ["loc-secret-01"],
                    "date_from": "2026-12-01",
                    "date_to": "2026-12-31",
                    "specialty_label": "Config Spec HTTP",
                    "clinic_label": "Config Clinic HTTP",
                }],
            }),
            encoding="utf-8",
        )
        cfg.chmod(0o600)

        real_store = analytics_api._store

        def frozen_store(secret):
            st = real_store(secret)
            st.clock = lambda: self.now
            return st

        with (
            mock.patch.dict(os.environ, {FLAG: "1", CONFIG_FILE: str(cfg)}, clear=False),
            mock.patch.object(analytics_api, "_store", side_effect=frozen_store),
        ):
            # 1. Default client (no header) -> ZERO appointment kinds!
            r_default = await self.get("inbox")
            self.assertEqual(r_default.status, 200, await r_default.text())
            d_default = await r_default.json()
            self.assertEqual(d_default["items"], [])

            # 2. Old pinned client (e.g. Vesta at 0.55.0 or 0.57.0) -> ZERO appointment kinds!
            for old_ver in ("0.55.0", "0.57.0"):
                r_old = await self.client.get(
                    f"{analytics_api.PREFIX}/inbox",
                    headers=self.headers() | {"X-Hestia-Contract-Version": old_ver},
                )
                self.assertEqual(r_old.status, 200)
                d_old = await r_old.json()
                self.assertEqual(d_old["items"], [])
                self.assertTrue(
                    all(item["kind"] in inbox_mod.LEGACY_INBOX_KINDS for item in d_old["items"])
                )

            # 3. Invalid or future contract version headers -> 400 budget_analytics_contract_version_invalid.
            for bad_ver in ("bad", "0.99.0", "1.0.0"):
                r_bad = await self.client.get(
                    f"{analytics_api.PREFIX}/inbox",
                    headers=self.headers() | {"X-Hestia-Contract-Version": bad_ver},
                )
                self.assertEqual(r_bad.status, 400)
                self.assertEqual((await r_bad.json())["error"], "budget_analytics_contract_version_invalid")

            # 4. Negotiated 0.58.0 client receives appointment_slot and can mark it read!
            r_new = await self.client.get(
                f"{analytics_api.PREFIX}/inbox",
                headers=self.headers() | {"X-Hestia-Contract-Version": api_contract.CONTRACT_VERSION},
            )
            self.assertEqual(r_new.status, 200, await r_new.text())
            d_new = await r_new.json()
            api_contract.BudgetAnalyticsInbox.model_validate(d_new)
            self.assertEqual(len(d_new["items"]), 1)
            item = d_new["items"][0]
            self.assertEqual(item["kind"], inbox_mod.KIND_APPOINTMENT_SLOT)
            self.assertEqual(
                item["params"]["message"],
                "New appointment slot: Config Spec HTTP (Config Clinic HTTP), 2026-12-10",
            )
            self.assertNotIn("09:30", item["params"]["message"])

            mark_payload = {
                "items": [{"kind": item["kind"], "subject_id": item["subject_id"], "milestone": item["milestone"]}],
                "read": True,
                "expected_reads_version": d_new["reads_version"],
            }
            # Marking appointment kind without >= 0.58.0 header -> 400!
            r_mark_old = await self.post("inbox-mark", mark_payload)
            self.assertEqual(r_mark_old.status, 400)
            self.assertEqual((await r_mark_old.json())["error"], "budget_analytics_invalid_request")

            # Marking appointment kind with 0.58.0 header -> 200, zero audit rows written, reads_count stays 0!
            r_mark_new = await self.post(
                "inbox-mark",
                mark_payload,
                headers=self.headers(origin=True) | {"X-Hestia-Contract-Version": api_contract.CONTRACT_VERSION},
            )
            self.assertEqual(r_mark_new.status, 200, await r_mark_new.text())
            mark_body = await r_mark_new.json()
            self.assertEqual((mark_body["inbox"]["changed"], mark_body["inbox"]["reads_count"]), (True, 0))
            self.assertEqual(self.rows(db.BudgetAnalyticsAudit), [])

        # P1-3 + P2-7: Disable purge immediately removes appointment read markers too!
        purged = prune_expired_appointments(session_factory=self.sessions, now=self.now + 1, enabled=False)
        self.assertEqual((purged.watches_deleted, purged.slots_deleted, purged.alerts_deleted, purged.reads_deleted), (1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
