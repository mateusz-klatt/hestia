"""Per-round calendar-sync DEBUG diagnostics: path, reason, phase wall/CPU ms, and redaction."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import itertools
import logging
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from hestia import db
from hestia.calendar_sync import config
from hestia.calendar_sync import events as codec
from hestia.calendar_sync import providers as p
from hestia.calendar_sync import runtime
from hestia.calendar_sync.engine import SyncEngine, endpoint_id_hash
from hestia.calendar_sync.store import SyncStore
from tests import test_calendar_providers as http
from tests.test_calendar_incremental import DEFAULT_PRINCIPAL, fixed_dav_item, fixed_ical
from tests.test_calendar_sync import FakeCalendar


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _parse_logs(records: list[logging.LogRecord]) -> tuple[list[dict[str, str]], dict[str, str]]:
    endpoints, rounds = [], []
    for record in records:
        msg = record.getMessage()
        if msg.startswith("calendar_sync endpoint="):
            endpoints.append(dict(token.split("=", 1) for token in msg.split()[1:]))
        elif msg.startswith("calendar_sync round "):
            rounds.append(dict(token.split("=", 1) for token in msg.split()[2:]))
    if len(rounds) != 1:
        raise AssertionError(f"Expected 1 round summary line, got {rounds!r}")
    return endpoints, rounds[0]


class CalendarDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/diag.db"
        self.now = datetime(2026, 10, 21, 10, 10, tzinfo=timezone.utc).timestamp()
        _, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)

    def tearDown(self):
        db.reset_engine_cache()
        self.temp.cleanup()

    def _run_debug(self, engine: SyncEngine) -> tuple[dict, list[dict[str, str]], dict[str, str]]:
        with self.assertLogs("hestia.calendar_sync", "DEBUG") as logs:
            result = engine.run_once()
        endpoints, summary = _parse_logs(logs.records)
        return result, endpoints, summary

    def test_caldav_reasons_logged_across_fast_full_pending_and_failure(self):
        self.store.configure([{"id": "family", "calendars": [
            {"id": "apple", "provider": "caldav", "account": "wife", "calendar_id": http.CALENDAR},
        ]}])
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        expected_hash = endpoint_id_hash("apple")

        # 1. no_checkpoint -> full
        _, endpoints, summary = self._run_debug(engine)
        self.assertEqual(len(endpoints), 1)
        ep = endpoints[0]
        self.assertEqual((ep["endpoint"], ep["provider"], ep["path"], ep["reason"], ep["events"]),
                         (expected_hash, "caldav", "full", "no_checkpoint", "1"))
        self.assertEqual((summary["endpoints"], summary["read"], summary["delivered"], summary["errors"]),
                         ("1", "1", "0", "0"))

        # 2. token_unchanged -> fast
        self.now += 60
        transport.responses.append(http.dav_acl(token="tok-1"))
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("fast", "token_unchanged", "0"))
        self.assertEqual((endpoints[0]["parse_ms"], endpoints[0]["parse_cpu_ms"]), ("0.0", "0.0"))

        # 3. token_changed -> full
        self.now += 60
        transport.responses.extend([http.dav_acl(token="tok-2"), http.multistatus(item), http.dav_acl(token="tok-2")])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("full", "token_changed", "1"))

        # 4. legacy_checkpoint -> full
        with self.store.transaction() as session:
            session.get(db.CalendarEndpoint, "apple").cursor = "tok-2"
        self.now += 60
        transport.responses.extend([http.dav_acl(token="tok-2"), http.multistatus(item), http.dav_acl(token="tok-2")])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "legacy_checkpoint"))

        # 5. checkpoint_mismatch -> full
        with self.store.transaction() as session:
            session.get(db.CalendarEndpoint, "apple").cursor = p.CalDAVProvider.CURSOR_PREFIX + "{invalid"
        self.now += 60
        transport.responses.extend([http.dav_acl(token="tok-2"), http.multistatus(item), http.dav_acl(token="tok-2")])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "checkpoint_mismatch"))

        # 6. no_token -> full
        self.now += 60
        transport.responses.extend([http.dav_acl(token=None), http.multistatus(item), http.dav_acl(token=None)])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "no_token"))

        # 7. principal_unverified -> full
        self.now += 60
        transport.responses.extend([
            http.dav_acl(token="tok-2", principal=None),
            http.multistatus(item),
            http.dav_acl(token="tok-2", principal=None),
        ])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "principal_unverified"))

        # 8. pending_work -> full + outbox delivery
        with self.store.transaction() as session:
            session.get(db.CalendarEndpoint, "apple").cursor = caldav._checkpoint("tok-2", DEFAULT_PRINCIPAL)
        self.now += 60
        edited = codec.validate_payload({**http.PAYLOAD, "summary": "Pending edit"})
        event = self.store.list_events("family")[0]
        self.store.save_local("family", edited, event["id"], event["revision"])
        transport.responses.extend([
            http.dav_acl(token="tok-2"),
            http.multistatus(item),
            http.dav_acl(token="tok-2"),
            http.dav_acl(token="tok-2"),
            http.response(fixed_ical(http.PAYLOAD, uid="family-uid"), headers={"ETag": '"v1"'}),
            http.response(status=204),
            http.response(fixed_ical(edited, uid="family-uid", stamp="20261021T101800Z"), headers={"ETag": '"v2"'}),
        ])
        result, endpoints, summary = self._run_debug(engine)
        self.assertEqual(result, {"read": 1, "delivered": 1, "errors": {}})
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "pending_work"))
        self.assertEqual(summary["delivered"], "1")

        # 9. failed_read -> full with error count
        self.now += 60
        transport.responses.append(http.response(status=503))
        result, endpoints, summary = self._run_debug(engine)
        self.assertEqual(result, {"read": 0, "delivered": 0, "errors": {"apple": "http_503"}})
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("full", "failed_read", "0"))
        self.assertEqual((summary["read"], summary["errors"]), ("0", "1"))
        self.assertEqual(transport.responses, [])

    def test_google_and_fake_provider_reasons_logged(self):
        self.store.configure([{"id": "family", "calendars": [
            {"id": "google", "provider": "google", "account": "me", "calendar_id": "family"},
        ]}])
        initial = http.google_event("event", http.PAYLOAD, etag='"g1"')
        google, transport = http.google(
            http.google_acl(),
            http.google_page([initial], token="g-1"),
            http.google_acl(),
        )
        engine = SyncEngine(self.store, {"google": google})
        expected_hash = endpoint_id_hash("google")

        # 1. no_checkpoint -> full
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["endpoint"], endpoints[0]["provider"], endpoints[0]["path"],
                          endpoints[0]["reason"], endpoints[0]["events"]),
                         (expected_hash, "google", "full", "no_checkpoint", "1"))

        # 2. google_sync_token -> fast
        self.now += 60
        transport.responses.extend([http.google_acl(), http.google_page([], token="g-2"), http.google_acl()])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("fast", "google_sync_token", "0"))

        # 3. google_410 -> full
        self.now += 60
        transport.responses.extend([
            http.google_acl(),
            http.response(status=410),
            http.google_page([initial], token="g-3"),
            http.google_acl(),
        ])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("full", "google_410", "1"))

        # 4. google_recurring_delta -> full
        series = http.google_event("series", {**http.PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]},
                                   etag='"s1"', iCalUID="series-uid")
        self.now += 60
        transport.responses.extend([
            http.google_acl(),
            http.google_page([series], token="g-4"),
            http.google_page([initial, series], token="g-5"),
            http.google_acl(),
        ])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("full", "google_recurring_delta", "2"))

        # 5. legacy_checkpoint -> full
        with self.store.transaction() as session:
            session.get(db.CalendarEndpoint, "google").cursor = "legacy-token"
        self.now += 60
        transport.responses.extend([http.google_acl(), http.google_page([initial, series], token="g-6"), http.google_acl()])
        _, endpoints, _ = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "legacy_checkpoint"))

        # 6. failed_read -> full
        self.now += 60
        transport.responses.append(http.response(status=503))
        _, endpoints, summary = self._run_debug(engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"], endpoints[0]["events"]),
                         ("full", "failed_read", "0"))
        self.assertEqual(summary["errors"], "1")
        self.assertEqual(transport.responses, [])

        # Non-instrumented provider without PullMetrics falls back cleanly to full_pull / incremental.
        fake = FakeCalendar(lambda: self.now)
        fake_engine = SyncEngine(self.store, {"google": fake})
        _, endpoints, _ = self._run_debug(fake_engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("full", "full_pull"))
        fake.full = False
        _, endpoints, _ = self._run_debug(fake_engine)
        self.assertEqual((endpoints[0]["path"], endpoints[0]["reason"]), ("fast", "incremental"))

    def test_phase_timings_record_wall_and_thread_cpu_ms(self):
        self.store.configure([{"id": "family", "calendars": [
            {"id": "apple", "provider": "caldav", "account": "wife", "calendar_id": http.CALENDAR},
        ]}])
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        engine.run_once()

        # Queue a local edit so fetch, parse, reconcile, and outbox all execute in one round.
        self.now += 60
        edited = codec.validate_payload({**http.PAYLOAD, "summary": "Timed edit"})
        event = self.store.list_events("family")[0]
        self.store.save_local("family", edited, event["id"], event["revision"])
        transport.responses.extend([
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
            http.dav_acl(token="tok-1"),
            http.response(fixed_ical(http.PAYLOAD, uid="family-uid"), headers={"ETag": '"v1"'}),
            http.response(status=204),
            http.response(fixed_ical(edited, uid="family-uid", stamp="20261021T101100Z"), headers={"ETag": '"v2"'}),
        ])

        wall_ticks = (i * 0.010 for i in itertools.count(1))
        cpu_ticks = (i * 0.002 for i in itertools.count(1))
        with patch.object(time, "perf_counter", side_effect=lambda: next(wall_ticks)), \
             patch.object(time, "thread_time", side_effect=lambda: next(cpu_ticks)):
            _, endpoints, summary = self._run_debug(engine)

        ep = endpoints[0]
        for key in ("fetch_ms", "fetch_cpu_ms", "parse_ms", "parse_cpu_ms",
                    "reconcile_ms", "reconcile_cpu_ms", "outbox_ms", "outbox_cpu_ms"):
            self.assertGreater(float(ep[key]), 0.0, key)
        wall_sum = sum(float(ep[k]) for k in ("fetch_ms", "parse_ms", "reconcile_ms", "outbox_ms"))
        cpu_sum = sum(float(ep[k]) for k in ("fetch_cpu_ms", "parse_cpu_ms", "reconcile_cpu_ms", "outbox_cpu_ms"))
        self.assertGreaterEqual(float(summary["total_ms"]) + 0.5, wall_sum)
        self.assertGreaterEqual(float(summary["total_cpu_ms"]) + 0.5, cpu_sum)
        self.assertGreater(float(summary["total_ms"]), float(summary["total_cpu_ms"]))

    def test_debug_env_var_gates_only_calendar_sync_logger_without_global_debug(self):
        self.store.configure([{"id": "family", "calendars": [
            {"id": "apple", "provider": "caldav", "account": "wife", "calendar_id": http.CALENDAR},
        ]}])
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})

        root_logger = logging.getLogger()
        cal_logger = logging.getLogger("hestia.calendar_sync")
        other_logger = logging.getLogger("hestia.server")
        orig_root, orig_cal, orig_other = root_logger.level, cal_logger.level, other_logger.level
        handler = _ListHandler()
        root_logger.addHandler(handler)
        self.addCleanup(root_logger.removeHandler, handler)
        self.addCleanup(root_logger.setLevel, orig_root)
        self.addCleanup(cal_logger.setLevel, orig_cal)
        self.addCleanup(other_logger.setLevel, orig_other)

        root_logger.setLevel(logging.INFO)
        cal_logger.setLevel(logging.NOTSET)
        other_logger.setLevel(logging.NOTSET)

        self.assertFalse(runtime.configure_debug_logging(environ={}))
        self.assertFalse(runtime.configure_debug_logging(environ={"HESTIA_CALENDAR_SYNC_DEBUG": "0"}))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HESTIA_CALENDAR_SYNC_DEBUG", None)
            engine.run_once()
        self.assertEqual([r for r in handler.records if r.levelno == logging.DEBUG], [])

        # Enabling HESTIA_CALENDAR_SYNC_DEBUG=1 bumps only hestia.calendar_sync to DEBUG.
        transport.responses.append(http.dav_acl(token="tok-1"))
        with patch.dict(os.environ, {"HESTIA_CALENDAR_SYNC_DEBUG": "1"}):
            engine.run_once()
            other_logger.debug("unrelated-server-debug")

        self.assertEqual(cal_logger.level, logging.DEBUG)
        self.assertEqual(root_logger.level, logging.INFO)
        self.assertEqual(other_logger.getEffectiveLevel(), logging.INFO)
        debug_messages = [r.getMessage() for r in handler.records if r.levelno == logging.DEBUG]
        self.assertNotIn("unrelated-server-debug", debug_messages)
        endpoints, summary = _parse_logs([r for r in handler.records if r.name == "hestia.calendar_sync"])
        self.assertEqual(endpoints[0]["reason"], "token_unchanged")
        self.assertEqual(summary["read"], "1")

    def test_debug_logs_never_leak_secrets_urls_emails_account_names_or_event_content(self):
        secret_endpoint_id = "alice@example.com-endpoint"
        secret_cal_url = "https://caldav.icloud.com/1/calendars/secret-cal-name/"
        self.store.configure([{"id": "family", "calendars": [
            {"id": secret_endpoint_id, "provider": "caldav", "account": "alice-private-account",
             "calendar_id": secret_cal_url},
            {"id": "bob@example.com-google", "provider": "google", "account": "bob-private-account",
             "calendar_id": "secret-google-calendar@group.calendar.google.com"},
        ]}])
        sensitive_payload = codec.validate_payload({
            "summary": "Private Doctor Appointment",
            "description": "Confidential medical notes",
            "location": "123 Secret Street",
            "start": {"date": "2026-11-01"},
            "end": {"date": "2026-11-02"},
        })
        item = fixed_dav_item(sensitive_payload, uid="sensitive-uid@private.example",
                              href=secret_cal_url + "one.ics", etag='"v1"')
        caldav_transport = http.QueueTransport(
            http.dav_acl(token="secret-sync-token", href=secret_cal_url, principal="/alice@example.com/principal/"),
            http.multistatus(item),
            http.dav_acl(token="secret-sync-token", href=secret_cal_url, principal="/alice@example.com/principal/"),
            http.dav_acl(token="secret-sync-token", href=secret_cal_url, principal="/alice@example.com/principal/"),
            http.response(status=503, data=b"UPSTREAM-SECRET-BODY"),
        )
        caldav = p.CalDAVProvider(secret_cal_url, username="alice@example.com",
                                  password="SUPER-SECRET-PASSWORD", transport=caldav_transport)
        google_transport = http.QueueTransport(
            http.response({"access_token": "ACCESS-SECRET-TOKEN", "expires_in": 3600}),
            http.google_acl(id="secret-google-calendar@group.calendar.google.com"),
            http.google_page([http.google_event("ev1", sensitive_payload, etag='"g1"',
                                                iCalUID="sensitive-uid@private.example")], token="secret-g-token"),
            http.google_acl(id="secret-google-calendar@group.calendar.google.com"),
            http.google_acl(id="secret-google-calendar@group.calendar.google.com"),
            http.google_page([], token="secret-g-token-2"),
            http.google_acl(id="secret-google-calendar@group.calendar.google.com"),
            http.response(status=401, data=b"OAUTH-SECRET-ERR"),
            http.response(status=401, data=b"OAUTH-SECRET-ERR-2"),
        )
        google = p.GoogleProvider("secret-google-calendar@group.calendar.google.com",
                                  client_id="GOOGLE-CLIENT-ID", client_secret="GOOGLE-SECRET",
                                  refresh_token="REFRESH-SECRET", transport=google_transport)
        engine = SyncEngine(self.store, {secret_endpoint_id: caldav, "bob@example.com-google": google})

        all_messages = []
        for _ in range(3):
            with self.assertLogs("hestia.calendar_sync", "DEBUG") as logs:
                engine.run_once()
            all_messages.extend(r.getMessage() for r in logs.records)

        self.assertEqual(caldav_transport.responses, [])
        self.assertEqual(google_transport.responses, [])
        joined = "\n".join(all_messages)
        self.assertIn(endpoint_id_hash(secret_endpoint_id), joined)
        self.assertIn(endpoint_id_hash("bob@example.com-google"), joined)
        forbidden = (
            "alice@example.com",
            "bob@example.com",
            "alice-private-account",
            "bob-private-account",
            "secret-cal-name",
            "caldav.icloud.com",
            "secret-google-calendar",
            "SUPER-SECRET-PASSWORD",
            "GOOGLE-CLIENT-ID",
            "GOOGLE-SECRET",
            "REFRESH-SECRET",
            "ACCESS-SECRET-TOKEN",
            "secret-sync-token",
            "secret-g-token",
            "Private Doctor",
            "Confidential medical",
            "123 Secret Street",
            "sensitive-uid",
            "UPSTREAM-SECRET-BODY",
            "OAUTH-SECRET-ERR",
            "https://",
        )
        for secret in forbidden:
            self.assertNotIn(secret, joined)


class CalendarPollerDebugEnvTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addCleanup(runtime.close_executor)

    async def test_poll_calendars_enables_debug_from_env_without_touching_root(self):
        parsed = config.parse_config({
            "version": 1,
            "accounts": {"wife": {"provider": "caldav", "username_env": "U", "password_env": "P"}},
            "groups": [{"id": "family", "calendars": [
                {"id": "apple", "account": "wife", "calendar_id": http.CALENDAR},
            ]}],
        })
        root_logger = logging.getLogger()
        cal_logger = logging.getLogger("hestia.calendar_sync")
        orig_root, orig_cal = root_logger.level, cal_logger.level
        self.addCleanup(root_logger.setLevel, orig_root)
        self.addCleanup(cal_logger.setLevel, orig_cal)
        root_logger.setLevel(logging.INFO)
        cal_logger.setLevel(logging.INFO)

        with patch.dict(os.environ, {"HESTIA_CALENDAR_SYNC_DEBUG": "1"}), \
             patch.object(runtime, "load_config", return_value=parsed), \
             patch.object(runtime, "_settled_thread", AsyncMock(return_value={})), \
             patch.object(runtime.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await runtime.poll_calendars()

        self.assertEqual(cal_logger.level, logging.DEBUG)
        self.assertEqual(root_logger.level, logging.INFO)


if __name__ == "__main__":
    unittest.main()
