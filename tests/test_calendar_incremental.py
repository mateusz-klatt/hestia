"""Phase-1 incremental calendar sync: CalDAV metadata-only fast path and no-change reconcile skip."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

from icalendar import Calendar
from sqlalchemy import event as sqlalchemy_event

from hestia import db
from hestia.calendar_sync import config
from hestia.calendar_sync import engine as engine_mod
from hestia.calendar_sync import events as codec
from hestia.calendar_sync import providers as p
from hestia.calendar_sync import runtime
from hestia.calendar_sync import store as store_mod
from hestia.calendar_sync.engine import SyncEngine
from hestia.calendar_sync.store import SyncStore
from tests import test_calendar_providers as http


DEFAULT_PRINCIPAL = "https://caldav.icloud.com/principal/"


def fixed_ical(payload=None, *, uid="family-uid", stamp="20261021T100000Z"):
    raw = codec.to_ical(payload or http.PAYLOAD, uid)
    lines = [b"DTSTAMP:" + stamp.encode() if line.startswith(b"DTSTAMP:") else line
             for line in raw.split(b"\r\n")]
    return b"\r\n".join(lines)


def fixed_dav_item(payload=None, *, uid="family-uid", href=http.RESOURCE, etag='"v1"',
                   stamp="20261021T100000Z"):
    data = fixed_ical(payload, uid=uid, stamp=stamp)
    return http.dav_response(
        '<d:getetag>' + escape(etag) + '</d:getetag><c:calendar-data>' + escape(data.decode()) + '</c:calendar-data>',
        href=href,
    )


class CalendarIncrementalFastPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/incremental.db"
        self.now = datetime(2026, 10, 21, 10, 10, tzinfo=timezone.utc).timestamp()
        self.db_engine, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)

    def tearDown(self):
        db.reset_engine_cache()
        self.temp.cleanup()

    @contextmanager
    def statements(self):
        recorded = []

        def record(connection, cursor, statement, parameters, context, executemany):
            recorded.append((" ".join(statement.upper().split()), parameters))

        sqlalchemy_event.listen(self.db_engine, "before_cursor_execute", record)
        try:
            yield recorded
        finally:
            sqlalchemy_event.remove(self.db_engine, "before_cursor_execute", record)

    def configure_endpoints(self, *kinds):
        calendars = []
        if "apple" in kinds:
            calendars.append({"id": "apple", "provider": "caldav", "account": "wife",
                              "calendar_id": http.CALENDAR})
        if "google" in kinds:
            calendars.append({"id": "google", "provider": "google", "account": "me",
                              "calendar_id": "family"})
        self.store.configure([{"id": "family", "calendars": calendars}])

    def test_idle_round_skips_report_parse_fingerprint_reconcile_and_row_writes(self):
        self.configure_endpoints("apple", "google")
        principal_url = "https://caldav.icloud.com/1/principal/"
        caldav_transport = http.QueueTransport(
            http.dav_acl(token="tok-1", principal="/1/principal/"),
            http.multistatus(fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')),
            http.dav_acl(token="tok-1", principal="/1/principal/"),
        )
        caldav = p.CalDAVProvider(http.CALENDAR, username="user", password="secret", transport=caldav_transport)
        google_transport = http.QueueTransport(
            http.response({"access_token": "tok-a", "expires_in": 3600}),
            http.google_acl(),
            http.google_page([http.google_event("event", http.PAYLOAD, etag='"g1"')], token="g-1"),
            http.google_acl(),
        )
        google = p.GoogleProvider("family", client_id="id", client_secret="secret",
                                  refresh_token="refresh", transport=google_transport)
        engine = SyncEngine(self.store, {"apple": caldav, "google": google})
        monotonic_now = 100.0

        with patch.object(p.time, "monotonic", side_effect=lambda: monotonic_now):
            self.assertEqual(engine.run_once(), {"read": 2, "delivered": 0, "errors": {}})
            self.assertEqual(caldav_transport.responses, [])
            self.assertEqual(google_transport.responses, [])
            self.assertEqual([call[0] for call in caldav_transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
            self.assertEqual([call[0] for call in google_transport.calls], ["POST", "GET", "GET", "GET"])

            # Round 2: idle poll with unchanged CalDAV token/principal and empty Google delta.
            caldav_transport.calls.clear()
            google_transport.calls.clear()
            self.now += 60
            monotonic_now = 160.0
            caldav_transport.responses.append(http.dav_acl(token="tok-1", principal="/1/principal/"))
            google_transport.responses.extend([
                http.google_acl(),
                http.google_page([], token="g-2"),
                http.google_acl(),
            ])

            with self.statements() as statements, \
                 patch.object(Calendar, "from_ical", wraps=Calendar.from_ical) as ical_parse, \
                 patch.object(codec, "from_ical", wraps=codec.from_ical) as codec_ical, \
                 patch.object(codec, "from_google_bundle", wraps=codec.from_google_bundle) as codec_google, \
                 patch.object(codec, "validate_payload", wraps=codec.validate_payload) as codec_validate, \
                 patch.object(engine_mod, "validate_payload", wraps=engine_mod.validate_payload) as engine_validate, \
                 patch.object(store_mod, "fingerprint", wraps=store_mod.fingerprint) as store_fp, \
                 patch.object(codec, "fingerprint", wraps=codec.fingerprint) as codec_fp, \
                 patch.object(self.store, "_inventory", wraps=self.store._inventory) as inventory, \
                 patch.object(self.store, "_reconcile", wraps=self.store._reconcile) as reconcile:
                result = engine.run_once()

            self.assertEqual(result, {"read": 2, "delivered": 0, "errors": {}})
            self.assertEqual(caldav_transport.responses, [])
            self.assertEqual(google_transport.responses, [])
            self.assertEqual([call[0] for call in caldav_transport.calls], ["PROPFIND"])
            self.assertEqual([call[0] for call in google_transport.calls], ["GET", "GET", "GET"])
            self.assertEqual(ical_parse.call_count, 0)
            self.assertEqual(codec_ical.call_count, 0)
            self.assertEqual(codec_google.call_count, 0)
            self.assertEqual(codec_validate.call_count, 0)
            self.assertEqual(engine_validate.call_count, 0)
            self.assertEqual(store_fp.call_count, 0)
            self.assertEqual(codec_fp.call_count, 0)
            self.assertEqual(inventory.call_count, 0)
            self.assertEqual(reconcile.call_count, 0)
            writes = [sql for sql, _ in statements if sql.startswith(("INSERT ", "UPDATE ", "DELETE "))]
            self.assertEqual(len(writes), 2)
            self.assertTrue(all(sql.startswith("UPDATE CALENDAR_ENDPOINTS ") for sql in writes))
            self.assertFalse(any("CALENDAR_EVENTS" in sql or "CALENDAR_REPLICAS" in sql or "CALENDAR_OUTBOX" in sql
                                 for sql in writes))

            with self.sessions() as session:
                apple_row = session.get(db.CalendarEndpoint, "apple")
                google_row = session.get(db.CalendarEndpoint, "google")
                self.assertEqual(apple_row.cursor, caldav._checkpoint("tok-1", principal_url))
                self.assertEqual(apple_row.last_success, self.now)
                self.assertIsNone(apple_row.last_error)
                self.assertEqual(google_row.cursor, p.GoogleProvider.CURSOR_PREFIX + "g-2")
                self.assertEqual(google_row.last_success, self.now)
                self.assertIsNone(google_row.last_error)
            self.assertEqual(len(self.store.list_events("family")), 1)

            # Round 3: unchanged clock and unchanged tokens produce zero SQL writes at all.
            caldav_transport.calls.clear()
            google_transport.calls.clear()
            caldav_transport.responses.append(http.dav_acl(token="tok-1", principal="/1/principal/"))
            google_transport.responses.extend([
                http.google_acl(),
                http.google_page([], token="g-2"),
                http.google_acl(),
            ])
            with self.statements() as statements:
                self.assertEqual(engine.run_once(), {"read": 2, "delivered": 0, "errors": {}})
            self.assertFalse(any(sql.startswith(("INSERT ", "UPDATE ", "DELETE ")) for sql, _ in statements))

    def test_changed_caldav_token_takes_full_path_without_writing_unchanged_rows(self):
        self.configure_endpoints("apple")
        second_href = http.CALENDAR + "two.ics"
        second_payload = codec.validate_payload({**http.PAYLOAD, "summary": "Unchanged"})
        item1_v1 = fixed_dav_item(http.PAYLOAD, uid="one@synthetic.test", href=http.RESOURCE, etag='"v1"')
        item2_v1 = fixed_dav_item(second_payload, uid="two@synthetic.test", href=second_href, etag='"v1"')
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item1_v1, item2_v1),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        by_uid = {item["uid"]: item for item in self.store.list_events("family")}
        edited_id = by_uid["one@synthetic.test"]["id"]
        unchanged_id = by_uid["two@synthetic.test"]["id"]

        # Token changes upstream: take full path, update only the changed event, skip writes for unchanged event.
        transport.calls.clear()
        self.now += 60
        edited_payload = codec.validate_payload({**http.PAYLOAD, "summary": "Edited upstream"})
        item1_v2 = fixed_dav_item(edited_payload, uid="one@synthetic.test", href=http.RESOURCE,
                                  etag='"v2"', stamp="20261021T101030Z")
        transport.responses.extend([
            http.dav_acl(token="tok-2"),
            http.multistatus(item1_v2, item2_v1),
            http.dav_acl(token="tok-2"),
        ])
        with self.statements() as statements:
            self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual(transport.responses, [])
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
        self.assertFalse(any(sql.startswith(("INSERT ", "DELETE ")) for sql, _ in statements))
        row_updates = [(sql, params) for sql, params in statements
                       if sql.startswith("UPDATE CALENDAR_EVENTS ") or sql.startswith("UPDATE CALENDAR_REPLICAS ")]
        self.assertTrue(row_updates)
        self.assertFalse(any(unchanged_id in str(params) for _, params in row_updates))
        self.assertTrue(all(edited_id in str(params) for _, params in row_updates))
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarEndpoint, "apple").cursor,
                             caldav._checkpoint("tok-2", DEFAULT_PRINCIPAL))
            self.assertEqual(session.get(db.CalendarEvent, edited_id).revision, 2)
            self.assertEqual(session.get(db.CalendarEvent, unchanged_id).revision, 1)

        # Next round with the new token unchanged takes the metadata-only fast path.
        transport.calls.clear()
        self.now += 60
        transport.responses.append(http.dav_acl(token="tok-2"))
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND"])

    def test_switching_caldav_principal_forces_full_read(self):
        self.configure_endpoints("apple")
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1", principal="/1/principal/"),
            http.multistatus(item),
            http.dav_acl(token="tok-1", principal="/1/principal/"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        with self.sessions() as session:
            first_cursor = session.get(db.CalendarEndpoint, "apple").cursor
        self.assertEqual(first_cursor, caldav._checkpoint("tok-1", "https://caldav.icloud.com/1/principal/"))
        self.assertNotIn("/1/principal/", first_cursor)
        self.assertNotIn("user", first_cursor)

        # Same principal and same token take the metadata-only fast path.
        transport.calls.clear()
        self.now += 60
        transport.responses.append(http.dav_acl(token="tok-1", principal="/1/principal/"))
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND"])

        # Switching the server's current-user-principal href forces a full read despite identical token.
        transport.calls.clear()
        self.now += 60
        transport.responses.extend([
            http.dav_acl(token="tok-1", principal="/2/principal/"),
            http.multistatus(item),
            http.dav_acl(token="tok-1", principal="/2/principal/"),
        ])
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
        with self.sessions() as session:
            second_cursor = session.get(db.CalendarEndpoint, "apple").cursor
        self.assertEqual(second_cursor, caldav._checkpoint("tok-1", "https://caldav.icloud.com/2/principal/"))
        self.assertNotEqual(second_cursor, first_cursor)

        # Switching the authenticated CalDAV account username forces a full read even when token and principal match.
        self.now += 60
        other_transport = http.QueueTransport(
            http.dav_acl(token="tok-1", principal="/2/principal/"),
            http.multistatus(item),
            http.dav_acl(token="tok-1", principal="/2/principal/"),
        )
        other_user = p.CalDAVProvider(http.CALENDAR, username="other-account", password="secret",
                                      transport=other_transport)
        self.assertEqual(SyncEngine(self.store, {"apple": other_user}).run_once(),
                         {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in other_transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
        with self.sessions() as session:
            switched_cursor = session.get(db.CalendarEndpoint, "apple").cursor
        self.assertEqual(switched_cursor, other_user._checkpoint("tok-1", "https://caldav.icloud.com/2/principal/"))
        self.assertNotIn("other-account", switched_cursor)

        # A principal change during inventory aborts without advancing the checkpoint.
        self.now += 60
        other_transport.responses.extend([
            http.dav_acl(token="tok-1", principal="/1/principal/"),
            http.multistatus(item),
            http.dav_acl(token="tok-1", principal="/2/principal/"),
        ])
        self.assertEqual(SyncEngine(self.store, {"apple": other_user}).run_once(),
                         {"read": 0, "delivered": 0, "errors": {"apple": "provider_error"}})
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarEndpoint, "apple").cursor, switched_cursor)

    def test_missing_and_unauthenticated_principals_require_full_read_without_checkpoint(self):
        self.configure_endpoints("apple")
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')

        # Missing principal -> always full path, never writes a fast-path-eligible checkpoint.
        missing_acl = http.dav_acl(token="tok-1", principal=None)
        caldav, transport = http.dav(
            missing_acl, http.multistatus(item), missing_acl,
            missing_acl, http.multistatus(item), missing_acl,
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        for _ in range(2):
            transport.calls.clear()
            self.now += 60
            self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
            self.assertEqual([call[0] for call in transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
            with self.sessions() as session:
                self.assertIsNone(session.get(db.CalendarEndpoint, "apple").cursor)
        self.assertEqual(transport.responses, [])

        # DAV:unauthenticated (including malformed sibling href or blank href) -> always full path, no checkpoint.
        unverified_acls = [
            http.dav_acl(token="tok-1", principal=None,
                         extra='<d:current-user-principal><d:unauthenticated/></d:current-user-principal>'),
            http.dav_acl(token="tok-1", principal=None,
                         extra='<d:current-user-principal><d:unauthenticated/><d:href>/1/principal/</d:href></d:current-user-principal>'),
            http.dav_acl(token="tok-1", principal="   "),
        ]
        for acl in unverified_acls:
            with self.subTest(acl=acl):
                provider, tr = http.dav(
                    acl, http.multistatus(item), acl,
                    acl, http.multistatus(item), acl,
                )
                first = provider.pull(None)
                self.assertTrue(first.complete and first.full)
                self.assertIsNone(first.cursor)
                second = provider.pull(first.cursor)
                self.assertTrue(second.complete and second.full)
                self.assertIsNone(second.cursor)
                self.assertEqual([call[0] for call in tr.calls],
                                 ["PROPFIND", "REPORT", "PROPFIND", "PROPFIND", "REPORT", "PROPFIND"])

        # Verified principal -> saves checkpoint, then transition to missing principal forces full read and clears it.
        verified_acl = http.dav_acl(token="tok-1", principal="/1/principal/")
        transport.calls.clear()
        self.now += 60
        transport.responses.extend([verified_acl, http.multistatus(item), verified_acl])
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        with self.sessions() as session:
            verified_cursor = session.get(db.CalendarEndpoint, "apple").cursor
        self.assertEqual(verified_cursor, caldav._checkpoint("tok-1", "https://caldav.icloud.com/1/principal/"))

        transport.calls.clear()
        self.now += 60
        transport.responses.extend([missing_acl, http.multistatus(item), missing_acl])
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
        with self.sessions() as session:
            self.assertIsNone(session.get(db.CalendarEndpoint, "apple").cursor)

        # Verified checkpoint presented with unauthenticated principal also forces full read and clears it.
        unauth_acl = unverified_acls[0]
        provider, tr = http.dav(unauth_acl, http.multistatus(item), unauth_acl)
        snapshot = provider.pull(verified_cursor)
        self.assertTrue(snapshot.complete and snapshot.full)
        self.assertIsNone(snapshot.cursor)
        self.assertEqual([call[0] for call in tr.calls], ["PROPFIND", "REPORT", "PROPFIND"])

    def test_legacy_invalid_mismatched_and_empty_token_checkpoints_take_full_path(self):
        self.configure_endpoints("apple")
        item = fixed_dav_item()
        with self.store.transaction() as session:
            session.get(db.CalendarEndpoint, "apple").cursor = "tok-1"
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarEndpoint, "apple").cursor,
                             caldav._checkpoint("tok-1", DEFAULT_PRINCIPAL))

        other_calendar = p.CalDAVProvider(http.CALENDAR + "other/", username="user", password="secret")
        warsaw_provider = p.CalDAVProvider(http.CALENDAR, username="user", password="secret",
                                           floating_time_zone="Europe/Warsaw")
        other_account = p.CalDAVProvider(http.CALENDAR, username="different", password="secret")
        principal_digest = caldav._principal_digest(DEFAULT_PRINCIPAL)
        invalid_cursors = [
            None,
            "tok-1",
            p.CalDAVProvider.CURSOR_PREFIX + "{invalid",
            p.CalDAVProvider.CURSOR_PREFIX + "[]",
            other_calendar._checkpoint("tok-1", DEFAULT_PRINCIPAL),
            warsaw_provider._checkpoint("tok-1", DEFAULT_PRINCIPAL),
            other_account._checkpoint("tok-1", DEFAULT_PRINCIPAL),
            caldav._checkpoint("tok-1", "https://caldav.icloud.com/other/principal/"),
            p.CalDAVProvider.CURSOR_PREFIX + json.dumps(
                {"calendar_id": http.CALENDAR, "floating_time_zone": None, "token": "tok-1"}),
            p.CalDAVProvider.CURSOR_PREFIX + json.dumps(
                {"calendar_id": http.CALENDAR, "floating_time_zone": None,
                 "principal": principal_digest, "token": ""}),
            p.CalDAVProvider.CURSOR_PREFIX + json.dumps(
                {"calendar_id": http.CALENDAR, "floating_time_zone": None,
                 "principal": principal_digest, "token": 123}),
        ]
        for bad_cursor in invalid_cursors:
            with self.subTest(bad_cursor=bad_cursor):
                provider, tr = http.dav(
                    http.dav_acl(token="tok-1"),
                    http.multistatus(item),
                    http.dav_acl(token="tok-1"),
                )
                snapshot = provider.pull(bad_cursor)
                self.assertTrue(snapshot.complete and snapshot.full)
                self.assertEqual(snapshot.cursor, provider._checkpoint("tok-1", DEFAULT_PRINCIPAL))
                self.assertEqual([call[0] for call in tr.calls], ["PROPFIND", "REPORT", "PROPFIND"])

        # Changing floating_time_zone invalidates the checkpoint; matching policy takes the fast path.
        zoned, tr = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
            http.dav_acl(token="tok-1"),
            floating_time_zone="Europe/Warsaw",
        )
        full_snap = zoned.pull(caldav._checkpoint("tok-1", DEFAULT_PRINCIPAL))
        self.assertTrue(full_snap.complete and full_snap.full)
        self.assertEqual(full_snap.cursor, zoned._checkpoint("tok-1", DEFAULT_PRINCIPAL))
        fast_snap = zoned.pull(full_snap.cursor)
        self.assertTrue(fast_snap.complete and not fast_snap.full)
        self.assertEqual(fast_snap.events, [])
        self.assertEqual(fast_snap.cursor, full_snap.cursor)
        self.assertEqual([call[0] for call in tr.calls], ["PROPFIND", "REPORT", "PROPFIND", "PROPFIND"])

        # Missing, empty, or whitespace server sync-token always takes the full path and never saves a token.
        for empty_token in (None, "", "   "):
            with self.subTest(empty_token=empty_token):
                provider, tr = http.dav(
                    http.dav_acl(token=empty_token),
                    http.multistatus(item),
                    http.dav_acl(token=empty_token),
                )
                snapshot = provider.pull(provider._checkpoint("tok-1", DEFAULT_PRINCIPAL))
                self.assertTrue(snapshot.complete and snapshot.full)
                self.assertIsNone(snapshot.cursor)
                self.assertEqual([call[0] for call in tr.calls], ["PROPFIND", "REPORT", "PROPFIND"])

    def test_pending_outbox_and_uncertain_replica_force_caldav_full_path(self):
        self.configure_endpoints("apple")
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})

        # Pending outbox job forces full path even when the CalDAV token matches.
        transport.calls.clear()
        self.now += 60
        edited = codec.validate_payload({**http.PAYLOAD, "summary": "Queued local edit"})
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
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 1, "errors": {}})
        self.assertEqual(transport.responses, [])
        self.assertEqual([call[0] for call in transport.calls],
                         ["PROPFIND", "REPORT", "PROPFIND", "PROPFIND", "GET", "PUT", "GET"])
        self.assertEqual(self.store.status()["pending"], [])

        # Uncertain replica without an outbox job also forces full re-observation.
        with self.store.transaction() as session:
            replica = session.get(db.CalendarReplica, ("apple", event["id"]))
            replica.uncertain = True
            replica.outbound_hash = codec.fingerprint(edited)
        transport.calls.clear()
        self.now += 60
        edited_item = fixed_dav_item(edited, uid="family-uid", href=http.RESOURCE,
                                     etag='"v2"', stamp="20261021T101100Z")
        transport.responses.extend([
            http.dav_acl(token="tok-1"),
            http.multistatus(edited_item),
            http.dav_acl(token="tok-1"),
        ])
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual(transport.responses, [])
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND", "REPORT", "PROPFIND"])
        with self.sessions() as session:
            self.assertFalse(session.get(db.CalendarReplica, ("apple", event["id"])).uncertain)

        # Once clear, an unchanged token takes the fast path again.
        transport.calls.clear()
        self.now += 60
        transport.responses.append(http.dav_acl(token="tok-1"))
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND"])

    def test_due_retry_still_delivers_after_empty_delta(self):
        self.configure_endpoints("google")
        initial = http.google_event("event", http.PAYLOAD, etag='"g1"')
        google, transport = http.google(
            http.google_acl(),
            http.google_page([initial], token="g-1"),
            http.google_acl(),
        )
        engine = SyncEngine(self.store, {"google": google})
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})

        # Queue a local edit and fail delivery with retryable 503 so it enters backoff.
        self.now += 60
        edited = codec.validate_payload({**http.PAYLOAD, "summary": "Retry after empty delta"})
        event = self.store.list_events("family")[0]
        self.store.save_local("family", edited, event["id"], event["revision"])
        transport.responses.extend([
            http.google_acl(),
            http.google_page([], token="g-2"),
            http.google_acl(),
            http.google_acl(),
            http.response(initial),
            http.response(status=503),
        ])
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {"google": "http_503"}})
        self.assertEqual(transport.responses, [])
        pending = self.store.status()["pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertEqual(pending[0]["next_attempt"], self.now + 5)

        # Advance past backoff: empty delta skips inventory/reconcile, updates cursor/status, and delivers retry.
        self.now += 10
        transport.calls.clear()
        updated = http.google_event(
            "event", edited, etag='"g2"',
            extendedProperties={"private": {"hestia_uid": "family-uid", "hestia_end_unspecified": "false"}},
        )
        transport.responses.extend([
            http.google_acl(),
            http.google_page([], token="g-3"),
            http.google_acl(),
            http.google_acl(),
            http.response(initial),
            http.response(updated),
            http.response(updated),
        ])
        with patch.object(self.store, "_inventory", wraps=self.store._inventory) as inventory, \
             patch.object(self.store, "_reconcile", wraps=self.store._reconcile) as reconcile:
            result = engine.run_once()
        self.assertEqual(result, {"read": 1, "delivered": 1, "errors": {}})
        self.assertEqual(transport.responses, [])
        self.assertEqual(inventory.call_count, 0)
        self.assertEqual(reconcile.call_count, 0)
        self.assertIn("syncToken=g-2", transport.calls[1][1])
        self.assertEqual(transport.calls[5][0], "PATCH")
        self.assertEqual(self.store.status()["pending"], [])
        with self.sessions() as session:
            row = session.get(db.CalendarEndpoint, "google")
            self.assertEqual(row.cursor, p.GoogleProvider.CURSOR_PREFIX + "g-3")
            self.assertEqual(row.last_success, self.now)
            self.assertIsNone(row.last_error)

    def test_failed_read_preserves_checkpoint_and_recovers_on_fast_path(self):
        self.configure_endpoints("apple")
        item = fixed_dav_item()
        caldav, transport = http.dav(
            http.dav_acl(token="tok-1"),
            http.multistatus(item),
            http.dav_acl(token="tok-1"),
        )
        engine = SyncEngine(self.store, {"apple": caldav})
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        saved_cursor = caldav._checkpoint("tok-1", DEFAULT_PRINCIPAL)
        saved_success = self.now

        failures = [
            ([http.response(status=503)], "http_503"),
            ([http.dav_acl(token="tok-2"), http.response(status=503)], "http_503"),
            ([http.dav_acl(token="tok-2"), http.multistatus(item), http.dav_acl(token="tok-3")], "provider_error"),
            ([http.dav_acl(("read",), token="tok-1")], "permission_denied"),
        ]
        for replies, expected_error in failures:
            with self.subTest(expected_error=expected_error, count=len(replies)):
                self.now += 60
                transport.responses.extend(replies)
                self.assertEqual(engine.run_once(), {"read": 0, "delivered": 0, "errors": {"apple": expected_error}})
                self.assertEqual(transport.responses, [])
                with self.sessions() as session:
                    row = session.get(db.CalendarEndpoint, "apple")
                    self.assertEqual(row.cursor, saved_cursor)
                    self.assertEqual(row.last_success, saved_success)
                    self.assertEqual(row.last_error, expected_error)

        # Next healthy poll with the preserved checkpoint takes the fast path and clears last_error.
        self.now += 60
        transport.calls.clear()
        transport.responses.append(http.dav_acl(token="tok-1"))
        self.assertEqual(engine.run_once(), {"read": 1, "delivered": 0, "errors": {}})
        self.assertEqual([call[0] for call in transport.calls], ["PROPFIND"])
        with self.sessions() as session:
            row = session.get(db.CalendarEndpoint, "apple")
            self.assertEqual(row.cursor, saved_cursor)
            self.assertEqual(row.last_success, self.now)
            self.assertIsNone(row.last_error)


class CalendarPollerCredentialRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.addCleanup(runtime.close_executor)
        self.db_path = Path(self.temp.name) / "rotate.db"

    async def test_rotating_credentials_between_polls_uses_new_credentials(self):
        parsed = config.parse_config({
            "version": 1,
            "accounts": {
                "wife": {"provider": "caldav", "username_env": "ROTATE_C_USER", "password_env": "ROTATE_C_PASS"},
                "me": {"provider": "google", "client_id_env": "ROTATE_G_ID",
                       "client_secret_env": "ROTATE_G_SECRET", "refresh_token_env": "ROTATE_G_TOKEN"},
            },
            "groups": [{"id": "family", "calendars": [
                {"id": "apple", "account": "wife", "calendar_id": http.CALENDAR},
                {"id": "google", "account": "me", "calendar_id": "family"},
            ]}],
        })
        item = fixed_dav_item(http.PAYLOAD, uid="family-uid", href=http.RESOURCE, etag='"v1"')
        google_ev = http.google_event("event", http.PAYLOAD, etag='"g1"')
        env = {
            "ROTATE_C_USER": "alice",
            "ROTATE_C_PASS": "pass-1",
            "ROTATE_G_ID": "id-1",
            "ROTATE_G_SECRET": "sec-1",
            "ROTATE_G_TOKEN": "ref-1",
        }
        transport = http.QueueTransport(
            # Poll 1: CalDAV full pull (alice) + Google OAuth + full pull (ref-1)
            http.dav_acl(token="tok-1", principal="/alice/principal/"),
            http.multistatus(item),
            http.dav_acl(token="tok-1", principal="/alice/principal/"),
            http.response({"access_token": "acc-1", "expires_in": 3600}),
            http.google_acl(),
            http.google_page([google_ev], token="g-1"),
            http.google_acl(),
            # Poll 2: credentials rotated -> CalDAV forced full pull (bob, same tok-1) + Google OAuth (ref-2) + incremental pull
            http.dav_acl(token="tok-1", principal="/bob/principal/"),
            http.multistatus(item),
            http.dav_acl(token="tok-1", principal="/bob/principal/"),
            http.response({"access_token": "acc-2", "expires_in": 3600}),
            http.google_acl(),
            http.google_page([], token="g-2"),
            http.google_acl(),
        )
        sleep_calls = 0

        async def rotate_between_polls(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                os.environ.update({
                    "ROTATE_C_USER": "bob",
                    "ROTATE_C_PASS": "pass-2",
                    "ROTATE_G_ID": "id-2",
                    "ROTATE_G_SECRET": "sec-2",
                    "ROTATE_G_TOKEN": "ref-2",
                })
                return
            raise asyncio.CancelledError()

        with patch.dict(os.environ, env), \
             patch.object(runtime, "load_config", return_value=parsed), \
             patch.object(db, "db_path", return_value=self.db_path), \
             patch.object(p, "HTTPTransport", return_value=transport), \
             patch.object(runtime.asyncio, "sleep", side_effect=rotate_between_polls):
            with self.assertRaises(asyncio.CancelledError):
                await runtime.poll_calendars()

        self.assertEqual(transport.responses, [])
        alice_auth = "Basic " + base64.b64encode(b"alice:pass-1").decode()
        bob_auth = "Basic " + base64.b64encode(b"bob:pass-2").decode()
        self.assertEqual([call[0] for call in transport.calls[:3]], ["PROPFIND", "REPORT", "PROPFIND"])
        self.assertTrue(all(call[2]["headers"]["Authorization"] == alice_auth for call in transport.calls[:3]))
        self.assertEqual(transport.calls[3][0], "POST")
        self.assertIn(b"client_id=id-1", transport.calls[3][2]["body"])
        self.assertIn(b"client_secret=sec-1", transport.calls[3][2]["body"])
        self.assertIn(b"refresh_token=ref-1", transport.calls[3][2]["body"])
        self.assertTrue(all(call[2]["headers"]["Authorization"] == "Bearer acc-1" for call in transport.calls[4:7]))
        self.assertEqual([call[0] for call in transport.calls[7:10]], ["PROPFIND", "REPORT", "PROPFIND"])
        self.assertTrue(all(call[2]["headers"]["Authorization"] == bob_auth for call in transport.calls[7:10]))
        self.assertEqual(transport.calls[10][0], "POST")
        self.assertIn(b"client_id=id-2", transport.calls[10][2]["body"])
        self.assertIn(b"client_secret=sec-2", transport.calls[10][2]["body"])
        self.assertIn(b"refresh_token=ref-2", transport.calls[10][2]["body"])
        self.assertTrue(all(call[2]["headers"]["Authorization"] == "Bearer acc-2" for call in transport.calls[11:14]))


if __name__ == "__main__":
    unittest.main()
