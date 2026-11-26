"""Unit tests for appointments poller, watch-cursor rotation (P1-4), pending_notify retry (P1-5), and immediate purge (P2-7)."""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

from hestia import db
from hestia.appointments import (
    APPOINTMENTS_KEY,
    CONFIG_FILE,
    FLAG,
    SECRETS_DIR,
    WARSAW_TZ,
    AppointmentsConfigError,
    AppointmentsPollerService,
    AppointmentsRunner,
    AppointmentsSettings,
    AppointmentsStore,
    CmpCredentials,
    HTTPResponse,
    PollPolicy,
    TransportError,
    build_service,
    load_runtime_settings,
    register,
)
from hestia.appointments import poller as poller_mod
from hestia.budget import owner_inbox as inbox_mod

SYNTHETIC_HMAC = b"synthetic-appointments-hmac-key-32bytes!"
SYNTHETIC_TOKEN = "synthetic-cmp-bearer-token-9999"


class FakeTransport:
    """Synthetic in-memory transport returning queued HTTP responses or raising TransportError."""

    def __init__(self, responses: list[HTTPResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        self.calls.append((method, url, dict(headers), body))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _cmp_response(slots: list[dict[str, object]], *, day: str = "2026-12-10") -> HTTPResponse:
    body = json.dumps({"days": [{"date": day, "slots": slots}]}).encode("utf-8")
    return HTTPResponse(status=200, headers={"Content-Type": "application/json"}, body=body)


class AppointmentsPollerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.root = Path(self.tmp.name)
        self.secrets_dir = self.root / "data" / "secrets"
        self.secrets_dir.mkdir(parents=True)
        self.db_path = self.root / "hestia.db"
        _, self.sessions = db.init_db(self.db_path)
        with self.sessions.begin() as session:
            session.add(db.User(username="alice", password_hash="h", role="admin"))

        self.cfg_path = self.secrets_dir / "appointments.json"
        self.hmac_path = self.secrets_dir / "appointments-hmac.key"
        self.token_path = self.secrets_dir / "appointments-cmp.token"
        self.hmac_path.write_bytes(SYNTHETIC_HMAC)
        self.token_path.write_bytes(SYNTHETIC_TOKEN.encode("ascii"))
        self.hmac_path.chmod(0o400)
        self.token_path.chmod(0o400)

        self.config_data = {
            "owner": "alice",
            "auth_mode": "bearer",
            "auth_token_env": "TEST_APPT_TOKEN_FILE",
            "hmac_key_env": "TEST_APPT_HMAC_FILE",
            "poll_policy": {
                "min_interval_seconds": 600,
                "max_interval_seconds": 900,
                "max_requests_per_run": 4,
                "max_retries": 0,
                "min_request_spacing_seconds": 0,
                "active_start": "06:00",
                "active_end": "23:00",
                "circuit_breaker_threshold": 2,
                "failure_backoff_base_seconds": 300,
                "failure_backoff_max_seconds": 1200,
                "slot_ttl_seconds": 7 * 86400,
                "alert_ttl_seconds": 3600,
                "notify_window_seconds": 3600,
                "notify_max_per_window": 5,
            },
            "watches": [
                {
                    "watch_id": "cmp-checkup-1",
                    "provider": "cmp",
                    "specialty_id": "spec-01",
                    "doctor_ids": ["doc-101"],
                    "location_ids": ["loc-01"],
                    "date_from": "2026-12-01",
                    "date_to": "2026-12-31",
                    "booked_slot": {
                        "start": "2026-12-20T14:00:00+01:00",
                        "min_improvement_minutes": 60,
                    },
                    "only_better_than_booked": True,
                    "specialty_label": "Config Spec A",
                    "clinic_label": "Config Clinic A",
                }
            ],
        }
        self._write_config(self.config_data)
        self.env = {
            FLAG: "1",
            CONFIG_FILE: str(self.cfg_path),
            SECRETS_DIR: str(self.secrets_dir),
            "TEST_APPT_TOKEN_FILE": str(self.token_path),
            "TEST_APPT_HMAC_FILE": str(self.hmac_path),
        }
        self.now = datetime(2026, 12, 5, 10, 0, tzinfo=WARSAW_TZ).timestamp()
        self.mono = 100.0

    def _write_config(self, payload: dict) -> None:
        if self.cfg_path.exists():
            self.cfg_path.chmod(0o600)
        self.cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        self.cfg_path.chmod(0o600)

    def _settings(self) -> AppointmentsSettings:
        settings = load_runtime_settings(environ=self.env)
        assert settings is not None
        return settings

    def test_flag_off_and_missing_or_wrong_mode_secrets_stay_inactive(self) -> None:
        with mock.patch.object(poller_mod, "load_runtime_settings") as loader:
            self.assertIsNone(build_service(environ={CONFIG_FILE: str(self.cfg_path)}, path=self.db_path))
            self.assertIsNone(build_service(environ={FLAG: "0", CONFIG_FILE: str(self.cfg_path)}, path=self.db_path))
            loader.assert_not_called()

        with self.assertLogs("hestia.appointments.poller", level="INFO") as logs:
            self.assertIsNone(build_service(environ={FLAG: "1"}, path=self.db_path))
        self.assertIn("reason=unconfigured", "\n".join(logs.output))

        self.token_path.unlink()
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            self.assertIsNone(build_service(environ=self.env, path=self.db_path))
        self.assertIn("reason=config_invalid", "\n".join(logs.output))
        self.assertNotIn(str(self.token_path), "\n".join(logs.output))
        self.token_path.write_bytes(SYNTHETIC_TOKEN.encode("ascii"))

        for bad_mode in (0o600, 0o644):
            self.token_path.chmod(bad_mode)
            self.assertIsNone(build_service(environ=self.env, path=self.db_path))
        self.token_path.chmod(0o400)

        self.hmac_path.chmod(0o600)
        self.assertIsNone(build_service(environ=self.env, path=self.db_path))
        self.hmac_path.chmod(0o400)

        bad_owner = self.config_data | {"owner": "nonexistent-user"}
        self._write_config(bad_owner)
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            self.assertIsNone(build_service(environ=self.env, path=self.db_path))
        self.assertIn("reason=prerequisites_unmet", "\n".join(logs.output))
        self._write_config(self.config_data)

        no_hmac_settings = AppointmentsSettings(
            owner="alice",
            cmp_base_url="https://ic-api.cmp.med.pl/api/v1",
            cmp_credentials=CmpCredentials("none", None),
            poll_policy=PollPolicy(),
            watches=self._settings().watches,
            hmac_secret=None,
        )
        with self.assertRaises(AppointmentsConfigError):
            AppointmentsRunner(no_hmac_settings, session_factory=self.sessions).check()

        with mock.patch.object(AppointmentsRunner, "check", side_effect=RuntimeError("db-locked")):
            with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
                self.assertIsNone(build_service(environ=self.env, path=self.db_path))
            self.assertIn("reason=init_failed error_type=RuntimeError", "\n".join(logs.output))
            self.assertNotIn("db-locked", "\n".join(logs.output))

        svc = build_service(
            environ=self.env,
            path=self.db_path,
            session_secret=lambda: b"distinct-session-secret-32-bytes!!",
        )
        self.assertIsNotNone(svc)
        assert svc is not None
        svc._pool.shutdown(wait=False)
        self.assertEqual(poller_mod._resolve_secret(lambda: None), b"")  # type: ignore[arg-type]

    def test_quiet_hours_diff_notify_dedupe_backoff_circuit_break_and_pending_retry(self) -> None:
        emitted: list[dict] = []
        slot_1 = {
            "slotId": "s-1",
            "startTime": "2026-12-10T09:30:00+01:00",
            "doctorId": "doc-101",
            "locationId": "loc-01",
            "specialtyId": "spec-01",
            "doctorName": "Dr Patient Leak Guard",
            "locationName": "Portal Hidden Clinic",
            "available": True,
        }
        slot_2 = {
            "slotId": "s-2",
            "startTime": "2026-12-11T10:15:00+01:00",
            "doctorId": "doc-101",
            "locationId": "loc-01",
            "specialtyId": "spec-01",
            "available": True,
        }
        # Limit to 1 notification/window to exercise P1-5 pending_notify retry end-to-end!
        self._write_config(
            self.config_data
            | {"poll_policy": self.config_data["poll_policy"] | {"notify_max_per_window": 1}}
        )
        transport = FakeTransport([
            _cmp_response([slot_1, slot_2]),  # Poll 1: slot_1 notified, slot_2 rate-limited -> pending_notify
            _cmp_response([slot_1, slot_2]),  # Poll 2 (+750s): slot_1 deduped, slot_2 still rate-limited
            _cmp_response([slot_1, slot_2]),  # Poll 3 (+3000s, window reset): slot_2 notified via pending_notify!
            TransportError("socket timeout"),  # Poll 4: failure 1 -> backoff 300s
            HTTPResponse(503, {}),  # Poll 5: failure 2 -> circuit_open, notifies ONCE
            HTTPResponse(503, {}),  # Poll 6: failure 3 -> circuit stays open, NO 2nd alert
            _cmp_response([slot_1, slot_2]),  # Poll 7: recovery -> ok, failures reset to 0
        ])
        runner = AppointmentsRunner(
            self._settings(),
            path=self.db_path,
            transport=transport,
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            jitter=lambda: 0.5,
            sink=emitted.append,
        )
        runner.check()

        # Quiet hours (05:00 & 23:30 Europe/Warsaw): zero transport calls.
        self.now = datetime(2026, 12, 5, 5, 0, tzinfo=WARSAW_TZ).timestamp()
        with self.assertLogs("hestia.appointments.poller", level="INFO") as logs:
            quiet = runner.poll_once()
        self.assertFalse(quiet.active_window)
        self.assertEqual((quiet.next_delay_seconds, runner.next_delay(), len(transport.calls)), (3600.0, 3600.0, 0))
        self.assertIn("reason=quiet_hours", "\n".join(logs.output))

        self.now = datetime(2026, 12, 5, 23, 30, tzinfo=WARSAW_TZ).timestamp()
        self.assertFalse(runner.poll_once().active_window)
        self.assertEqual(len(transport.calls), 0)

        # Active hours (10:00 Europe/Warsaw): Poll 1 -> slot_1 notified, slot_2 rate-limited (pending_notify).
        self.now = datetime(2026, 12, 5, 10, 0, tzinfo=WARSAW_TZ).timestamp()
        with self.assertLogs("hestia.appointments", level="INFO") as logs:
            r1 = runner.poll_once()
        self.assertEqual((r1.polled, r1.notified, r1.rate_limited, r1.errors), (1, 1, 1, 0))
        self.assertEqual(r1.next_delay_seconds, 750.0)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(
            emitted[0]["params"]["message"],
            "Earlier appointment slot: Config Spec A (Config Clinic A), 2026-12-10 09:30",
        )
        joined = "\n".join(logs.output)
        for forbidden in (SYNTHETIC_TOKEN, "Dr Patient Leak Guard", "Portal Hidden Clinic", "Config Spec A", "alice"):
            self.assertNotIn(forbidden, joined)

        # Poll 2 inside window: slot_1 not duplicated, slot_2 retried and still rate-limited.
        self.now += 750.0
        r2 = runner.poll_once()
        self.assertEqual((r2.polled, r2.notified, r2.rate_limited), (1, 0, 1))
        self.assertEqual(len(emitted), 1)

        # Poll 3 after window reset: pending slot_2 is notified cleanly!
        self.now += 3000.0
        r3 = runner.poll_once()
        self.assertEqual((r3.polled, r3.notified, r3.rate_limited), (1, 1, 0))
        self.assertEqual(len(emitted), 2)
        self.assertEqual(
            emitted[1]["params"]["message"],
            "Earlier appointment slot: Config Spec A (Config Clinic A), 2026-12-11 10:15",
        )

        # Poll 4: failure 1 -> backoff 300s, NO circuit-break alert yet.
        self.now += 750.0
        r4 = runner.poll_once()
        self.assertEqual((r4.polled, r4.errors, r4.attention_alerts), (0, 1, 0))

        # Inside cooldown (+100s < 300s): skipped without calling transport!
        calls_before_cd = len(transport.calls)
        self.now += 100.0
        r_cd = runner.poll_once()
        self.assertEqual((r_cd.polled, r_cd.skipped_cooldown), (0, 1))
        self.assertEqual(len(transport.calls), calls_before_cd)

        # Poll 5 (+200s -> +300s): failure 2 -> circuit_open -> notifies ONCE!
        self.now += 200.0
        r5 = runner.poll_once()
        self.assertEqual((r5.errors, r5.attention_alerts), (1, 1))
        self.assertEqual(emitted[2]["kind"], "appointment_attention")
        self.assertEqual(emitted[2]["params"]["status"], "circuit_open")

        # Poll 6 (+600s): failure 3 while circuit open -> NO duplicate notification!
        self.now += 600.0
        r6 = runner.poll_once()
        self.assertEqual((r6.errors, r6.attention_alerts), (1, 0))
        self.assertEqual(len(emitted), 3)

        # Poll 7 (+1200s): recovery -> status='ok', failures=0!
        self.now += 1200.0
        r7 = runner.poll_once()
        self.assertEqual((r7.polled, r7.errors, r7.notified), (1, 0, 0))
        state = AppointmentsStore(self.sessions, clock=lambda: self.now).get_watch_state("alice", "cmp-checkup-1")
        assert state is not None
        self.assertEqual((state.last_status, state.consecutive_failures, state.attention_notified_at), ("ok", 0, None))

    def test_watch_cursor_rotation_prevents_starvation_with_7_watches_and_cap_3(self) -> None:
        """P1-4 regression: 7 watches under max_requests_per_run=3 must rotate and run every watch."""
        seven_watches = [
            {
                "watch_id": f"watch-{idx}",
                "provider": "cmp",
                "specialty_id": f"spec-{idx:02d}",
                "date_from": "2026-12-01",
                "date_to": "2026-12-31",
            }
            for idx in range(7)
        ]
        self._write_config(
            self.config_data
            | {
                "poll_policy": self.config_data["poll_policy"] | {"max_requests_per_run": 3},
                "watches": seven_watches,
            }
        )
        seen_per_cycle: list[list[str]] = []

        class RecordingTransport:
            def __init__(self, bucket: list[str]) -> None:
                self.bucket = bucket

            def request(self, method, url, *, headers, body=None, timeout=15.0):
                payload = json.loads((body or b"{}").decode("utf-8"))
                self.bucket.append(payload["specialtyId"])
                return _cmp_response([])

        # Run 3 cycles with a brand-new AppointmentsRunner each cycle (proving cursor persistence!).
        for _ in range(3):
            bucket: list[str] = []
            runner = AppointmentsRunner(
                self._settings(),
                session_factory=self.sessions,
                transport=RecordingTransport(bucket),
                clock=lambda: self.now,
                monotonic=lambda: self.mono,
                sleeper=lambda _s: None,
                jitter=lambda: 0.0,
            )
            runner.check()
            res = runner.poll_once()
            self.assertEqual((res.polled, res.deferred_run_limit, res.errors), (3, 4, 0))
            seen_per_cycle.append(bucket)
            self.now += 700.0

        self.assertEqual(
            seen_per_cycle,
            [
                ["spec-00", "spec-01", "spec-02"],
                ["spec-03", "spec-04", "spec-05"],
                ["spec-06", "spec-00", "spec-01"],
            ],
        )
        # All 7 watches were polled and persisted cursor now points at index 2.
        self.assertEqual({spec for cycle in seen_per_cycle for spec in cycle}, {f"spec-{i:02d}" for i in range(7)})
        self.assertEqual(AppointmentsStore(self.sessions, clock=lambda: self.now).get_poll_cursor("alice"), 2)

        # Stopping before cycle preserves cursor without polling.
        runner_stop = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=FakeTransport([]),
            clock=lambda: self.now,
            sleeper=lambda _s: None,
        )
        runner_stop.check()
        runner_stop.stopping.set()
        r_stop = runner_stop.poll_once()
        self.assertEqual((r_stop.polled, r_stop.errors), (0, 0))
        self.assertEqual(AppointmentsStore(self.sessions, clock=lambda: self.now).get_poll_cursor("alice"), 2)

    def test_stopping_between_watches_dispatches_no_further_provider_request(self) -> None:
        """P2 Round 4: stop flag checked at the top of the per-watch loop prevents further provider requests."""
        three_watches = [
            {
                "watch_id": f"watch-{idx}",
                "provider": "cmp",
                "specialty_id": f"spec-{idx:02d}",
                "date_from": "2026-12-01",
                "date_to": "2026-12-31",
            }
            for idx in range(3)
        ]
        self._write_config(
            self.config_data
            | {
                "poll_policy": self.config_data["poll_policy"] | {"max_requests_per_run": 6},
                "watches": three_watches,
            }
        )
        requested_specs: list[str] = []

        class RecordingSlotTransport:
            def request(self, method, url, *, headers, body=None, timeout=15.0):
                spec = json.loads((body or b"{}").decode("utf-8"))["specialtyId"]
                requested_specs.append(spec)
                return _cmp_response([{
                    "slotId": f"s-{spec}",
                    "startTime": "2026-12-10T09:30:00+01:00",
                    "doctorId": "doc-101",
                    "locationId": "loc-01",
                    "specialtyId": spec,
                }])

        runner: AppointmentsRunner

        def stop_after_first_watch(_entry: dict) -> None:
            runner.stopping.set()

        runner = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=RecordingSlotTransport(),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            sink=stop_after_first_watch,
        )
        runner.check()
        res = runner.poll_once()
        self.assertEqual((res.polled, res.notified), (1, 1))
        self.assertEqual(requested_specs, ["spec-00"])

    def test_failing_watch_under_cap_1_advances_cursor_cools_down_and_runs_watches_1_to_6(self) -> None:
        """P1-4 regression: max_requests_per_run=1 + network failure on watch 0 must still run watches 1..6."""
        seven_watches = [
            {
                "watch_id": f"watch-{idx}",
                "provider": "cmp",
                "specialty_id": f"spec-{idx:02d}",
                "date_from": "2026-12-01",
                "date_to": "2026-12-31",
            }
            for idx in range(7)
        ]
        self._write_config(
            self.config_data
            | {
                "poll_policy": self.config_data["poll_policy"]
                | {"max_requests_per_run": 1, "max_retries": 3},
                "watches": seven_watches,
            }
        )
        attempted_specs: list[str] = []

        class FlakyWatch0Transport:
            def request(self, method, url, *, headers, body=None, timeout=15.0):
                spec = json.loads((body or b"{}").decode("utf-8"))["specialtyId"]
                attempted_specs.append(spec)
                if spec == "spec-00":
                    raise TransportError("watch-0 network down")
                return _cmp_response([])

        store = AppointmentsStore(self.sessions, clock=lambda: self.now)

        # Cycle 1: watch-0 fails with TransportError -> CmpRunLimitError(attempted=True, cause_code='network'),
        # records failure + 300s cooldown, AND advances cursor to 1!
        r0_runner = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=FlakyWatch0Transport(),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            jitter=lambda: 0.0,
        )
        r0_runner.check()
        r0 = r0_runner.poll_once()
        self.assertEqual((r0.polled, r0.errors, r0.deferred_run_limit), (0, 1, 6))
        self.assertEqual(store.get_poll_cursor("alice"), 1)
        w0_state = store.get_watch_state("alice", "watch-0")
        assert w0_state is not None
        self.assertEqual((w0_state.last_status, w0_state.last_error_code, w0_state.consecutive_failures), ("backoff", "network", 1))
        self.assertEqual(w0_state.cooldown_until, self.now + 300.0)

        # Cycles 2..7: watches 1..6 ALL run cleanly without starvation!
        # On cycle 7 (watch-6), watch-0 is still inside its 300s cooldown so it is skipped (skipped_cooldown=1)
        # and the cursor advances cleanly past cooling watch-0 to index 1.
        for expected_idx in range(1, 7):
            self.now += 20.0
            cycle_runner = AppointmentsRunner(
                self._settings(),
                session_factory=self.sessions,
                transport=FlakyWatch0Transport(),
                clock=lambda: self.now,
                monotonic=lambda: self.mono,
                sleeper=lambda _s: None,
                jitter=lambda: 0.0,
            )
            cycle_runner.check()
            res = cycle_runner.poll_once()
            self.assertEqual((res.polled, res.errors), (1, 0))
            if expected_idx < 6:
                self.assertEqual((res.skipped_cooldown, store.get_poll_cursor("alice")), (0, expected_idx + 1))
            else:
                self.assertEqual((res.skipped_cooldown, store.get_poll_cursor("alice")), (1, 1))

        self.assertEqual(attempted_specs, [f"spec-{i:02d}" for i in range(7)])

        # Cycle 8 (cursor already at 1 while watch-0 cools): watch-1 runs immediately without wasting budget on watch-0!
        self.now += 20.0
        cd_runner = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=FlakyWatch0Transport(),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            jitter=lambda: 0.0,
        )
        cd_runner.check()
        r_cd = cd_runner.poll_once()
        self.assertEqual((r_cd.polled, r_cd.skipped_cooldown, r_cd.errors), (1, 0, 0))
        self.assertEqual(attempted_specs[-1], "spec-01")

        # Also cover retryable HTTP 503 hitting CmpRunLimitError(attempted=True) and tripping circuit breaker.
        self.now += 400.0
        store.set_poll_cursor("alice", 0)
        r_503_runner = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=FakeTransport([HTTPResponse(503, {})]),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            jitter=lambda: 0.0,
        )
        r_503_runner.check()
        r_503 = r_503_runner.poll_once()
        self.assertEqual((r_503.errors, r_503.attention_alerts), (1, 1))
        self.assertEqual(store.get_poll_cursor("alice"), 1)

    def test_generation_fence_and_stop_flag_prevent_writes_after_purge_between_fetch_and_write(self) -> None:
        """P2-7 race regression: disable purge or stop flag during in-flight fetch leaves zero DB writes."""
        store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        slot_payload = [{
            "slotId": "s-race",
            "startTime": "2026-12-10T09:30:00+01:00",
            "doctorId": "doc-101",
            "locationId": "loc-01",
            "specialtyId": "spec-01",
        }]

        # 1. Disable purge runs between fetch and write on 200 OK -> zero rows written, zero notifications!
        emitted: list[dict] = []

        class PurgeMidFetchTransport:
            def __init__(self, outcome):
                self.outcome = outcome

            def request(self, method, url, *, headers, body=None, timeout=15.0):
                poller_mod.prune_expired_appointments(
                    session_factory=self_outer.sessions,
                    now=self_outer.now,
                    enabled=False,
                )
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome

        self_outer = self
        runner_ok_race = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=PurgeMidFetchTransport(_cmp_response(slot_payload)),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            sink=emitted.append,
        )
        g_ok = runner_ok_race.check()
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            r_ok_race = runner_ok_race.poll_once()
        self.assertEqual((r_ok_race.polled, r_ok_race.notified, r_ok_race.errors), (0, 0, 0))
        self.assertTrue(runner_ok_race.stopping.is_set())
        self.assertIn("reason=generation_fenced", "\n".join(logs.output))
        self.assertEqual(emitted, [])
        self.assertEqual(store.list_watch_states("alice"), ())
        self.assertEqual(store.list_active_slots("alice"), ())
        self.assertEqual(store.list_inbox_alerts("alice"), ())
        self.assertEqual(store.get_poll_cursor("alice"), 0)
        self.assertEqual(store.get_generation(), g_ok + 1)
        self.assertIsNotNone(store.get_disabled_at())

        # 2. Disable purge runs between fetch and write on TransportError (both CmpClientError & CmpRunLimitError).
        runner_err_race = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=PurgeMidFetchTransport(TransportError("late socket fail")),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
        )
        runner_err_race.check()
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            r_err_race = runner_err_race.poll_once()
        self.assertEqual((r_err_race.polled, r_err_race.errors, r_err_race.attention_alerts), (0, 0, 0))
        self.assertTrue(runner_err_race.stopping.is_set())
        self.assertIn("reason=generation_fenced", "\n".join(logs.output))
        self.assertEqual(store.list_watch_states("alice"), ())
        self.assertEqual(store.get_poll_cursor("alice"), 0)

        self._write_config(
            self.config_data
            | {"poll_policy": self.config_data["poll_policy"] | {"max_requests_per_run": 1, "max_retries": 3}}
        )
        runner_lim_race = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=PurgeMidFetchTransport(TransportError("late cap fail")),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
        )
        runner_lim_race.check()
        r_lim_race = runner_lim_race.poll_once()
        self.assertEqual((r_lim_race.polled, r_lim_race.errors), (0, 0))
        self.assertTrue(runner_lim_race.stopping.is_set())
        self.assertEqual(store.list_watch_states("alice"), ())

        # 3. Stop flag set during in-flight fetch aborts before any DB write!
        runner_stop = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=None,
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
            sink=emitted.append,
        )
        runner_stop.check()

        class StopMidFetchTransport:
            def request(self, method, url, *, headers, body=None, timeout=15.0):
                runner_stop.stopping.set()
                return _cmp_response(slot_payload)

        runner_stop.transport = StopMidFetchTransport()
        r_stop_race = runner_stop.poll_once()
        self.assertEqual((r_stop_race.polled, r_stop_race.notified), (0, 0))
        self.assertEqual(emitted, [])
        self.assertEqual(store.list_watch_states("alice"), ())
        self.assertEqual(store.list_active_slots("alice"), ())
        self.assertEqual(store.list_inbox_alerts("alice"), ())
        self.assertEqual(store.get_poll_cursor("alice"), 0)

    async def test_runner_captures_generation_once_and_stops_permanently_on_later_cycle_after_purge(self) -> None:
        """P1 Round 4: atomic enable transition binds unique generation; A->B(disable)->A(fail)->C(enable)->A(still blocked)."""
        store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        slot_payload = [{
            "slotId": "s-r4",
            "startTime": "2026-12-10T09:30:00+01:00",
            "doctorId": "doc-101",
            "locationId": "loc-01",
            "specialtyId": "spec-01",
        }]

        # Un-activated runner (generation=None, check() never called) cannot self-authorize or poll!
        t_uninit = FakeTransport([_cmp_response(slot_payload)])
        runner_uninit = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=t_uninit,
            clock=lambda: self.now,
            sleeper=lambda _s: None,
        )
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            r_uninit = runner_uninit.poll_once()
        self.assertEqual((r_uninit.polled, len(t_uninit.calls)), (0, 0))
        self.assertTrue(runner_uninit.stopping.is_set())
        self.assertIn("reason=generation_fenced", "\n".join(logs.output))

        # Exact Round 4 interleaving:
        # 1) Enabled A clears disabled_at and atomically binds unique generation g_a.
        t_a = FakeTransport([_cmp_response(slot_payload)])
        runner_a = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=t_a,
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
        )
        g_a = runner_a.check()
        self.assertEqual((g_a, runner_a.generation), (1, 1))
        self.assertIsNone(store.get_disabled_at())

        # 2) Disabled B purges BEFORE A polls, bumping generation (1 -> 2) and setting disabled_at.
        poller_mod.prune_expired_appointments(session_factory=self.sessions, now=self.now + 10.0, enabled=False)
        self.assertEqual(store.get_generation(), 2)
        self.assertEqual(store.get_disabled_at(), self.now + 10.0)

        # 3) A's poll MUST fail the fence (0 network calls, 0 writes, permanently stopped)!
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            res_a = runner_a.poll_once()
        self.assertEqual((res_a.polled, res_a.notified, len(t_a.calls)), (0, 0, 0))
        self.assertTrue(runner_a.stopping.is_set())
        self.assertIn("Appointments poller stopped reason=generation_fenced", "\n".join(logs.output))
        self.assertEqual(store.list_watch_states("alice"), ())
        self.assertEqual(store.get_poll_cursor("alice"), 0)

        # 4) Enabled C starts later: clears disabled_at, bumps generation (2 -> 3), binds g_c=3.
        t_c = FakeTransport([_cmp_response(slot_payload), _cmp_response(slot_payload)])
        runner_c = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=t_c,
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
        )
        g_c = runner_c.check()
        self.assertEqual((g_c, runner_c.generation), (3, 3))
        self.assertIsNone(store.get_disabled_at())

        # 5) Old A STILL CANNOT write (even with disabled_at now cleared, A's g_a=1 != 3)!
        self.assertEqual(runner_a.poll_once().polled, 0)
        self.assertEqual(len(t_a.calls), 0)
        self.assertFalse(store.set_poll_cursor("alice", 1, expected_generation=g_a))
        self.assertIsNone(
            store.reconcile_watch(
                "alice",
                self._settings().watches[0],
                (),
                hmac_key=SYNTHETIC_HMAC,
                policy=self._settings().poll_policy,
                expected_generation=g_a,
            )
        )
        # Meanwhile C polls and writes cleanly under its unique generation g_c=3!
        self.assertEqual((runner_c.poll_once().polled, len(t_c.calls)), (1, 1))
        self.assertIsNotNone(store.get_watch_state("alice", "cmp-checkup-1"))

        # 6) Enabled D starting while C is still running bumps generation (3 -> 4), invalidating older enabled C!
        runner_d = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=FakeTransport([]),
            clock=lambda: self.now,
            sleeper=lambda _s: None,
        )
        g_d = runner_d.check()
        self.assertEqual(g_d, 4)
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            res_c_stale = runner_c.poll_once()
        self.assertEqual((res_c_stale.polled, len(t_c.calls)), (0, 1))
        self.assertTrue(runner_c.stopping.is_set())
        self.assertIn("reason=generation_fenced", "\n".join(logs.output))

        # 7) End-to-end AppointmentsPollerService exits its loop permanently when fenced on a later cycle!
        loop = asyncio.get_running_loop()
        first_done = asyncio.Event()
        svc_calls = 0

        class ServiceFenceTransport:
            def request(self, method, url, *, headers, body=None, timeout=15.0):
                nonlocal svc_calls
                svc_calls += 1
                loop.call_soon_threadsafe(first_done.set)
                return _cmp_response(slot_payload)

        runner_svc = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=ServiceFenceTransport(),
            clock=lambda: self.now,
            monotonic=lambda: self.mono,
            sleeper=lambda _s: None,
        )
        runner_svc.check()
        svc = AppointmentsPollerService(runner_svc)
        svc.start()
        try:
            await asyncio.wait_for(first_done.wait(), timeout=2.0)
            for _ in range(50):
                if store.get_watch_state("alice", "cmp-checkup-1") is not None:
                    break
                await asyncio.sleep(0.01)
            poller_mod.prune_expired_appointments(session_factory=self.sessions, now=self.now + 10.0, enabled=False)
            svc.wake()
            assert svc._task is not None
            await asyncio.wait_for(svc._task, timeout=2.0)
            self.assertTrue(runner_svc.stopping.is_set())
            self.assertFalse(svc.active)
            self.assertEqual(svc_calls, 1)
            self.assertEqual(store.list_watch_states("alice"), ())
        finally:
            await svc.stop()

    async def test_old_disabled_server_housekeeping_cannot_purge_newer_enabled_server_and_exits(self) -> None:
        """Round 5 regression: old flag-OFF housekeeping sweep is fenced by newer mark_enabled and exits permanently."""
        store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        slot_payload = [{
            "slotId": "s-r5",
            "startTime": "2026-12-10T09:30:00+01:00",
            "doctorId": "doc-101",
            "locationId": "loc-01",
            "specialtyId": "spec-01",
        }]
        app_old_disabled = aioweb.Application()
        register(
            app_old_disabled,
            path=self.db_path,
            environ={FLAG: "0", CONFIG_FILE: str(self.cfg_path)},
            clock=lambda: self.now,
            housekeeping_interval=0.05,
        )
        with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
            async with TestClient(TestServer(app_old_disabled)):
                old_task = app_old_disabled[APPOINTMENTS_KEY].housekeeping_task
                self.assertIsNotNone(old_task)
                assert old_task is not None
                self.assertFalse(old_task.done())
                disabled_gen = store.get_generation()
                self.assertEqual(disabled_gen, 1)
                self.assertEqual(store.get_disabled_at(), self.now)

                # A newer enabled server starts: mark_enabled clears disabled_at, bumps generation, and polls state!
                runner_new = AppointmentsRunner(
                    self._settings(),
                    session_factory=self.sessions,
                    transport=FakeTransport([_cmp_response(slot_payload)]),
                    clock=lambda: self.now + 10.0,
                    monotonic=lambda: self.mono,
                    sleeper=lambda _s: None,
                )
                enabled_gen = runner_new.check()
                self.assertEqual(enabled_gen, disabled_gen + 1)
                self.assertIsNone(store.get_disabled_at())
                res = runner_new.poll_once()
                self.assertEqual((res.polled, res.notified), (1, 1))
                self.assertIsNotNone(store.get_watch_state("alice", "cmp-checkup-1"))
                self.assertEqual(len(store.list_active_slots("alice")), 1)
                self.assertEqual(len(store.list_inbox_alerts("alice")), 1)

                # Wait for old disabled server's housekeeping sweep to run -> MUST NOT purge and MUST exit permanently!
                await asyncio.wait_for(old_task, timeout=2.0)
                self.assertTrue(old_task.done())

                # Newer enabled server's state, generation, and disabled_at are ALL completely untouched!
                self.assertIsNotNone(store.get_watch_state("alice", "cmp-checkup-1"))
                self.assertEqual(len(store.list_active_slots("alice")), 1)
                self.assertEqual(len(store.list_inbox_alerts("alice")), 1)
                self.assertEqual(store.get_generation(), enabled_gen)
                self.assertIsNone(store.get_disabled_at())
        self.assertIn("Appointments housekeeping stopped reason=generation_fenced", "\n".join(logs.output))

    async def test_app_lifecycle_immediate_purge_on_disable_and_active_service(self) -> None:
        # Seed watch state, active slot, inbox alert, poller cursor, AND appointment inbox read marker.
        store = AppointmentsStore(self.sessions, clock=lambda: self.now)
        watch = self._settings().watches[0]
        transport_seed = FakeTransport([
            _cmp_response([{
                "slotId": "s-1",
                "startTime": "2026-12-10T09:30:00+01:00",
                "doctorId": "doc-101",
                "locationId": "loc-01",
                "specialtyId": "spec-01",
            }])
        ])
        seed_runner = AppointmentsRunner(
            self._settings(),
            session_factory=self.sessions,
            transport=transport_seed,
            clock=lambda: self.now,
            sleeper=lambda _s: None,
        )
        seed_runner.check()
        seed_res = seed_runner.poll_once()
        self.assertEqual(seed_res.notified, 1)
        slot_hash = seed_res.notifications[0].slot_hash
        subj = inbox_mod.opaque_subject_id("alice", inbox_mod.KIND_APPOINTMENT_SLOT, f"{watch.watch_id}\0{slot_hash}")
        with self.sessions.begin() as session:
            session.add(
                db.BudgetOwnerInboxRead(
                    owner="alice",
                    kind=inbox_mod.KIND_APPOINTMENT_SLOT,
                    subject_id=subj,
                    milestone="better_slot",
                    read_at=self.now,
                )
            )
        self.assertIsNotNone(store.get_watch_state("alice", watch.watch_id))
        self.assertEqual(len(store.list_inbox_alerts("alice")), 1)

        # P2-7 + P1-3: App startup with flag OFF only 1 second later IMMEDIATELY purges ALL appointment data!
        immediate_time = self.now + 1.0
        app_off = aioweb.Application()
        register(
            app_off,
            path=self.db_path,
            environ={FLAG: "0", CONFIG_FILE: str(self.cfg_path)},
            clock=lambda: immediate_time,
            housekeeping_interval=0.02,
        )
        async with TestClient(TestServer(app_off)):
            self.assertIsNone(app_off[APPOINTMENTS_KEY].service)
            self.assertIsNone(store.get_watch_state("alice", watch.watch_id))
            self.assertEqual(store.list_active_slots("alice"), ())
            self.assertEqual(store.list_inbox_alerts("alice"), ())
            self.assertEqual(store.get_poll_cursor("alice"), 0)
            with self.sessions() as session:
                self.assertIsNone(
                    session.get(
                        db.BudgetOwnerInboxRead,
                        ("alice", inbox_mod.KIND_APPOINTMENT_SLOT, subj, "better_slot"),
                    )
                )
            await asyncio.sleep(0.05)

        # Startup & housekeeping exceptions are caught and logged cleanly without crashing aiohttp app.
        app_err = aioweb.Application()
        with mock.patch.object(poller_mod, "_run_housekeeping_purge", side_effect=RuntimeError("boom")):
            register(app_err, path=self.db_path, environ={FLAG: "0"}, housekeeping_interval=0.01)
            with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
                async with TestClient(TestServer(app_err)):
                    await asyncio.sleep(0.03)
            joined = "\n".join(logs.output)
            self.assertIn("startup purge failed error_type=RuntimeError", joined)
            self.assertIn("housekeeping purge failed error_type=RuntimeError", joined)

        # Active poller service via register().
        loop = asyncio.get_running_loop()
        called = asyncio.Event()
        calls = 0

        class CountingTransport:
            def request(self, method, url, *, headers, body=None, timeout=15.0):
                nonlocal calls
                calls += 1
                loop.call_soon_threadsafe(called.set)
                return _cmp_response([])

        app_on = aioweb.Application()
        register(
            app_on,
            path=self.db_path,
            environ=self.env,
            transport=CountingTransport(),
            clock=lambda: self.now,
            housekeeping_interval=3600.0,
        )
        async with TestClient(TestServer(app_on)):
            svc = app_on[APPOINTMENTS_KEY].service
            self.assertIsNotNone(svc)
            assert svc is not None
            self.assertTrue(svc.active)
            await asyncio.wait_for(called.wait(), timeout=2.0)
            self.assertGreaterEqual(calls, 1)
            svc.wake()
        self.assertIsNone(app_on[APPOINTMENTS_KEY].service)

        # Direct service loop branches: stop before start, pre-stopped start, unexpected error + timeout wait.
        bad_runner = AppointmentsRunner(self._settings(), session_factory=self.sessions, clock=lambda: self.now)
        await AppointmentsPollerService(bad_runner).stop()

        prestopped_runner = AppointmentsRunner(self._settings(), session_factory=self.sessions, clock=lambda: self.now)
        prestopped_runner.stopping.set()
        svc_pre = AppointmentsPollerService(prestopped_runner)
        svc_pre.start()
        await svc_pre.stop()

        err_runner = AppointmentsRunner(self._settings(), session_factory=self.sessions, clock=lambda: self.now)
        attempts = 0

        def flaky_poll():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient-bug")
            if attempts == 2:
                return poller_mod.PollCycleResult(active_window=True, next_delay_seconds=0.01)
            err_runner.stopping.set()
            return poller_mod.PollCycleResult(active_window=True, next_delay_seconds=0.01)

        with mock.patch.object(err_runner, "poll_once", side_effect=flaky_poll):
            svc_err = AppointmentsPollerService(err_runner)
            with self.assertLogs("hestia.appointments.poller", level="WARNING") as logs:
                svc_err.start()
                await asyncio.sleep(0.03)
                svc_err.wake()
                await asyncio.sleep(0.06)
                await svc_err.stop()
            self.assertGreaterEqual(attempts, 3)
            self.assertIn("poll loop failed error_type=RuntimeError", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
