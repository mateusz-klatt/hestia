"""Per-round calendar-sync DEBUG diagnostics: opt-in gating, isolation, phase wall/CPU ms, and redaction."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import itertools
import logging
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from hestia import db
from hestia.calendar_sync import config
from hestia.calendar_sync import diagnostics as diag_mod
from hestia.calendar_sync import events as codec
from hestia.calendar_sync import providers as p
from hestia.calendar_sync import runtime
from hestia.calendar_sync.diagnostics import (
    LOGGER_NAME,
    configure_diagnostics,
    diag_log,
    diagnostics_enabled,
    endpoint_id_hash,
)
from hestia.calendar_sync.engine import SyncEngine
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
        self._orig_diag_level = diag_log.level
        self._orig_diag_handlers = list(diag_log.handlers)

    def tearDown(self):
        diag_log.handlers[:] = self._orig_diag_handlers
        diag_log.setLevel(self._orig_diag_level)
        db.reset_engine_cache()
        self.temp.cleanup()

    def _run_debug(self, engine: SyncEngine) -> tuple[dict, list[dict[str, str]], dict[str, str]]:
        with patch.dict(os.environ, {"HESTIA_CALENDAR_SYNC_DEBUG": "1"}), \
             self.assertLogs(LOGGER_NAME, "DEBUG") as logs:
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

        # 7. principal_unverified -> full (missing collection principal + failing root lookup)
        self.now += 60
        transport.responses.extend([
            http.dav_acl(token="tok-2", principal=None),
            http.multistatus(),
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

    def test_disabled_diagnostics_has_zero_overhead_and_ignores_parent_debug(self):
        self.store.configure([{"id": "family", "calendars": [
            {"id": "apple", "provider": "caldav", "account": "wife", "calendar_id": http.CALENDAR},
            {"id": "google", "provider": "google", "account": "me", "calendar_id": "family"},
        ]}])
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, caldav_tr = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
            http.dav_acl(token="tok-1"),
        )
        google, google_tr = http.google(
            http.google_acl(),
            http.google_page([http.google_event("ev", http.PAYLOAD, etag='"g1"')], token="g-1"),
            http.google_acl(),
            http.google_acl(),
            http.google_page([], token="g-2"),
            http.google_acl(),
        )
        engine = SyncEngine(self.store, {"apple": caldav, "google": google})

        root_logger = logging.getLogger()
        parent_logger = logging.getLogger("hestia.calendar_sync")
        orig_root, orig_parent = root_logger.level, parent_logger.level
        diag_handler = _ListHandler()
        parent_handler = _ListHandler()
        diag_log.addHandler(diag_handler)
        parent_logger.addHandler(parent_handler)
        self.addCleanup(diag_log.removeHandler, diag_handler)
        self.addCleanup(parent_logger.removeHandler, parent_handler)
        self.addCleanup(root_logger.setLevel, orig_root)
        self.addCleanup(parent_logger.setLevel, orig_parent)

        # Inherited DEBUG on root and parent loggers must NOT enable diagnostics or collect metrics.
        root_logger.setLevel(logging.DEBUG)
        parent_logger.setLevel(logging.DEBUG)
        self.assertFalse(diagnostics_enabled(environ={}))
        self.assertFalse(diagnostics_enabled(environ={"HESTIA_CALENDAR_SYNC_DEBUG": "0"}))

        with patch.dict(os.environ, {}, clear=False), \
             patch.object(diag_log, "setLevel", wraps=diag_log.setLevel) as set_level, \
             patch.object(diag_log, "addHandler", wraps=diag_log.addHandler) as add_handler, \
             patch.object(time, "thread_time", wraps=time.thread_time) as cpu_clock, \
             patch.object(p, "_clocks", wraps=p._clocks) as provider_clocks, \
             patch.object(p, "PullMetrics", wraps=p.PullMetrics) as pull_metrics, \
             patch.object(diag_mod, "_RoundCollector", wraps=diag_mod._RoundCollector) as round_col:
            os.environ.pop("HESTIA_CALENDAR_SYNC_DEBUG", None)
            self.assertFalse(configure_diagnostics())
            self.assertEqual(engine.run_once(), {"read": 2, "delivered": 0, "errors": {}})
            self.assertEqual(engine.run_once(), {"read": 2, "delivered": 0, "errors": {}})

        self.assertEqual(caldav_tr.responses, [])
        self.assertEqual(google_tr.responses, [])
        self.assertEqual(set_level.call_count, 0)
        self.assertEqual(add_handler.call_count, 0)
        self.assertEqual(cpu_clock.call_count, 0)
        self.assertEqual(provider_clocks.call_count, 0)
        self.assertEqual(pull_metrics.call_count, 0)
        self.assertEqual(round_col.call_count, 0)
        self.assertEqual(diag_handler.records, [])
        self.assertEqual(parent_handler.records, [])

        # Even if diag_log was previously set to DEBUG, a disabled round makes no logging-config calls and emits nothing.
        diag_log.setLevel(logging.DEBUG)
        caldav_tr.responses.append(http.dav_acl(token="tok-1"))
        google_tr.responses.extend([http.google_acl(), http.google_page([], token="g-3"), http.google_acl()])
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(diag_log, "setLevel", wraps=diag_log.setLevel) as set_level, \
             patch.object(diag_log, "addHandler", wraps=diag_log.addHandler) as add_handler, \
             patch.object(time, "thread_time", wraps=time.thread_time) as cpu_clock:
            os.environ.pop("HESTIA_CALENDAR_SYNC_DEBUG", None)
            self.assertFalse(configure_diagnostics())
            self.assertEqual(engine.run_once(), {"read": 2, "delivered": 0, "errors": {}})
        self.assertEqual(caldav_tr.responses, [])
        self.assertEqual(google_tr.responses, [])
        self.assertEqual(set_level.call_count, 0)
        self.assertEqual(add_handler.call_count, 0)
        self.assertEqual(cpu_clock.call_count, 0)
        self.assertEqual(diag_handler.records, [])

        # Direct provider pulls with diagnostics disabled allocate no PullMetrics.
        caldav_tr.responses.append(http.dav_acl(token="tok-1"))
        google_tr.responses.extend([http.google_acl(), http.google_page([], token="g-4"), http.google_acl()])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HESTIA_CALENDAR_SYNC_DEBUG", None)
            self.assertIsNone(caldav.pull(caldav._checkpoint("tok-1", DEFAULT_PRINCIPAL)).metrics)
            self.assertIsNone(google.pull(p.GoogleProvider.CURSOR_PREFIX + "g-3").metrics)
        self.assertEqual(caldav_tr.responses, [])
        self.assertEqual(google_tr.responses, [])

    def test_dedicated_logger_does_not_propagate_or_raise_instances_logger(self):
        self.store.configure([{"id": "family", "calendars": [
            {"id": "apple", "provider": "caldav", "account": "wife", "calendar_id": http.CALENDAR},
        ]}])
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, _ = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})

        root_logger = logging.getLogger()
        parent_logger = logging.getLogger("hestia.calendar_sync")
        instances_logger = logging.getLogger("hestia.calendar_sync.instances")
        orig_root = root_logger.level
        orig_parent = parent_logger.level
        orig_instances = instances_logger.level
        root_handler = _ListHandler()
        root_logger.addHandler(root_handler)
        self.addCleanup(root_logger.removeHandler, root_handler)
        self.addCleanup(root_logger.setLevel, orig_root)
        self.addCleanup(parent_logger.setLevel, orig_parent)
        self.addCleanup(instances_logger.setLevel, orig_instances)

        root_logger.setLevel(logging.INFO)
        parent_logger.setLevel(logging.NOTSET)
        instances_logger.setLevel(logging.NOTSET)
        diag_log.handlers.clear()
        diag_log.setLevel(logging.CRITICAL + 1)

        # Concurrent configure calls install exactly one marked StreamHandler and call setLevel once.
        barrier = threading.Barrier(8)
        results: list[bool] = []
        results_lock = threading.Lock()

        def configure_worker():
            barrier.wait(timeout=5)
            enabled = configure_diagnostics(environ={"HESTIA_CALENDAR_SYNC_DEBUG": "1"})
            with results_lock:
                results.append(enabled)

        with patch.object(diag_log, "addHandler", wraps=diag_log.addHandler) as add_handler, \
             patch.object(diag_log, "setLevel", wraps=diag_log.setLevel) as set_level:
            threads = [threading.Thread(target=configure_worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertTrue(configure_diagnostics(environ={"HESTIA_CALENDAR_SYNC_DEBUG": "1"}))

        self.assertEqual(results, [True] * 8)
        self.assertEqual(add_handler.call_count, 1)
        self.assertEqual(set_level.call_count, 1)
        self.assertFalse(diag_log.propagate)
        self.assertEqual(len(diag_log.handlers), 1)
        self.assertIsInstance(diag_log.handlers[0], logging.StreamHandler)
        self.assertTrue(getattr(diag_log.handlers[0], "_hestia_calendar_diagnostics", False))
        self.assertEqual(diag_log.level, logging.DEBUG)
        self.assertEqual(root_logger.level, logging.INFO)
        self.assertEqual(parent_logger.getEffectiveLevel(), logging.INFO)
        self.assertEqual(instances_logger.getEffectiveLevel(), logging.INFO)

        # An unrelated (unmarked) handler does not block installing the marked handler once,
        # and setLevel is skipped when the level is already DEBUG.
        unmarked = _ListHandler()
        diag_log.handlers[:] = [unmarked]
        with patch.object(diag_log, "addHandler", wraps=diag_log.addHandler) as add_handler, \
             patch.object(diag_log, "setLevel", wraps=diag_log.setLevel) as set_level:
            self.assertTrue(configure_diagnostics(environ={"HESTIA_CALENDAR_SYNC_DEBUG": "1"}))
            self.assertTrue(configure_diagnostics(environ={"HESTIA_CALENDAR_SYNC_DEBUG": "1"}))
        self.assertEqual(add_handler.call_count, 1)
        self.assertEqual(set_level.call_count, 0)
        self.assertEqual(len(diag_log.handlers), 2)
        self.assertTrue(getattr(diag_log.handlers[1], "_hestia_calendar_diagnostics", False))

        # Diagnostics emit only on diag_log; parent/root stay clean and instances DEBUG stays suppressed.
        diag_handler = _ListHandler()
        setattr(diag_handler, "_hestia_calendar_diagnostics", True)
        diag_log.handlers[:] = [diag_handler]
        with patch.dict(os.environ, {"HESTIA_CALENDAR_SYNC_DEBUG": "1"}), \
             patch.object(diag_log, "addHandler", wraps=diag_log.addHandler) as add_handler, \
             patch.object(diag_log, "setLevel", wraps=diag_log.setLevel) as set_level:
            engine.run_once()
            instances_logger.debug("Skipping orphan override for calendar event secret-id at 2026-11-01")

        self.assertEqual(add_handler.call_count, 0)
        self.assertEqual(set_level.call_count, 0)
        self.assertEqual(root_handler.records, [])
        endpoints, summary = _parse_logs(diag_handler.records)
        self.assertEqual(endpoints[0]["reason"], "no_checkpoint")
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
        with patch.dict(os.environ, {"HESTIA_CALENDAR_SYNC_DEBUG": "1"}):
            for _ in range(3):
                with self.assertLogs(LOGGER_NAME, "DEBUG") as logs:
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
        self._orig_diag_level = diag_log.level
        self._orig_diag_handlers = list(diag_log.handlers)

    async def asyncTearDown(self):
        diag_log.handlers[:] = self._orig_diag_handlers
        diag_log.setLevel(self._orig_diag_level)

    async def test_poll_calendars_enables_dedicated_diagnostics_without_touching_parent_or_instances(self):
        parsed = config.parse_config({
            "version": 1,
            "accounts": {"wife": {"provider": "caldav", "username_env": "U", "password_env": "P"}},
            "groups": [{"id": "family", "calendars": [
                {"id": "apple", "account": "wife", "calendar_id": http.CALENDAR},
            ]}],
        })
        root_logger = logging.getLogger()
        parent_logger = logging.getLogger("hestia.calendar_sync")
        instances_logger = logging.getLogger("hestia.calendar_sync.instances")
        orig_root, orig_parent, orig_instances = root_logger.level, parent_logger.level, instances_logger.level
        self.addCleanup(root_logger.setLevel, orig_root)
        self.addCleanup(parent_logger.setLevel, orig_parent)
        self.addCleanup(instances_logger.setLevel, orig_instances)
        root_logger.setLevel(logging.INFO)
        parent_logger.setLevel(logging.NOTSET)
        instances_logger.setLevel(logging.NOTSET)

        with patch.dict(os.environ, {"HESTIA_CALENDAR_SYNC_DEBUG": "1"}), \
             patch.object(runtime, "load_config", return_value=parsed), \
             patch.object(runtime, "_settled_thread", AsyncMock(return_value={})), \
             patch.object(runtime.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await runtime.poll_calendars()

        self.assertEqual(diag_log.level, logging.DEBUG)
        self.assertFalse(diag_log.propagate)
        self.assertEqual(root_logger.level, logging.INFO)
        self.assertEqual(parent_logger.getEffectiveLevel(), logging.INFO)
        self.assertEqual(instances_logger.getEffectiveLevel(), logging.INFO)


if __name__ == "__main__":
    unittest.main()
