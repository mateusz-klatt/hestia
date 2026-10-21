"""Synthetic Google compatibility through real SQLite reconciliation and delivery.

Every HTTP response is queued locally. Fixtures contain no private account data,
OAuth requests never occur, and no real calendar is read or changed.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import tempfile
import unittest

from hestia import db
from hestia.calendar_sync.engine import SyncEngine
from hestia.calendar_sync.events import validate_payload
from hestia.calendar_sync.store import SyncStore
from tests import test_calendar_providers as http
from tests.test_calendar_sync import FakeCalendar


class GoogleCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.temp.name + "/compatibility.db"
        self.now = datetime(2026, 10, 21, 10, 10, tzinfo=timezone.utc).timestamp()
        _, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)
        self.store.configure([{"id": "family", "calendars": [
            {"id": "google", "provider": "google", "account": "synthetic-google", "calendar_id": "family"},
            {"id": "apple", "provider": "caldav", "account": "synthetic-apple", "calendar_id": "https://calendar.test/family/"},
        ]}])
        self.apple = FakeCalendar(lambda: self.now)
        self.provider, self.transport = http.google()
        self.engine = SyncEngine(self.store, {"google": self.provider, "apple": self.apple})
        self.items = self.synthetic_inventory()

    def tearDown(self):
        db.reset_engine_cache()
        self.temp.cleanup()

    @staticmethod
    def synthetic_inventory():
        def event(identifier, content, **extra):
            return http.google_event(identifier, content, iCalUID=identifier + "@synthetic.test", **extra)

        zero_time = {"dateTime": "2026-11-01T09:00:00+00:00", "timeZone": "UTC"}
        unknown_start = {"dateTime": "2026-11-01T10:00:00+00:00", "timeZone": "UTC"}
        return [
            event("ordinary", {"summary": "Synthetic ordinary", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}}),
            event("zero", {"summary": "Synthetic zero", "start": zero_time, "end": zero_time}),
            event("gmail", {"summary": "Synthetic unknown end", "start": unknown_start,
                            "end": {"dateTime": "2026-11-01T11:00:00+00:00", "timeZone": "UTC"}},
                  eventType="fromGmail", endTimeUnspecified=True),
            event("birthday", {"summary": "Synthetic birthday", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"},
                               "recurrence": ["RRULE:FREQ=YEARLY"], "transparency": "transparent", "visibility": "private"},
                  eventType="birthday", birthdayProperties={"type": "birthday"}),
            event("gmt", {"summary": "Synthetic fixed offset", "start": {"dateTime": "2026-11-01T09:00:00+03:30", "timeZone": "GMT+03:30"},
                           "end": {"dateTime": "2026-11-01T10:00:00+03:30", "timeZone": "GMT+03:30"}}),
            event("detached", {"summary": "Synthetic detached", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}},
                  recurringEventId="missing-series", originalStartTime={"date": "2026-11-03"}),
            {"id": "deleted-stub", "status": "cancelled"},
        ]

    @staticmethod
    def expected_content():
        return {item["summary"]: validate_payload(item) for item in [
            {"summary": "Synthetic ordinary", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}},
            {"summary": "Synthetic zero", "start": {"dateTime": "2026-11-01T09:00:00+00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2026-11-01T09:00:00+00:00", "timeZone": "UTC"}},
            {"summary": "Synthetic unknown end", "start": {"dateTime": "2026-11-01T10:00:00+00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2026-11-01T10:00:00+00:00", "timeZone": "UTC"}, "endTimeUnspecified": True},
            {"summary": "Synthetic birthday", "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"},
             "recurrence": ["RRULE:FREQ=YEARLY"], "transparency": "transparent", "visibility": "private"},
            {"summary": "Synthetic fixed offset", "start": {"dateTime": "2026-11-01T05:30:00+00:00", "timeZone": "UTC"},
             "end": {"dateTime": "2026-11-01T06:30:00+00:00", "timeZone": "UTC"}},
            {"summary": "Synthetic detached", "start": {"date": "2026-11-03"}, "end": {"date": "2026-11-04"}},
        ]}

    def queue_full_import(self):
        self.transport.responses.extend([http.google_acl(), http.google_page(self.items, token="first"),
                                         http.response(status=404), http.google_acl()])

    def sync(self):
        result = self.engine.run_once()
        self.assertEqual(result["errors"], {}, result)
        self.assertFalse(self.transport.responses, "All expected provider requests must be consumed")
        return result

    def local_by_summary(self):
        return {event["payload"]["summary"]: event for event in self.store.list_events("family")}

    def test_mixed_source_inventory_is_copied_without_google_mutation_and_survives_restart(self):
        self.queue_full_import()
        self.assertEqual(self.sync()["delivered"], 6)
        expected = self.expected_content()
        local = self.local_by_summary()
        self.assertEqual({name: event["payload"] for name, event in local.items()}, expected)
        self.assertEqual({event.payload["summary"]: event.payload for event in self.apple.active()}, expected)
        self.assertEqual(len(self.store.list_events("family", include_deleted=True)), 7)
        deleted = [event for event in self.store.list_events("family", True) if event["deleted"]]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["uid"], "google-deleted:deleted-stub")
        detached = local["Synthetic detached"]
        self.assertNotEqual(detached["uid"], "detached@synthetic.test")
        self.assertNotIn("recurrence", detached["payload"])
        with self.sessions() as session:
            replica = session.get(db.CalendarReplica, ("google", detached["id"]))
            self.assertEqual(replica.remote_id, "detached")
            self.assertTrue(replica.seen)
        self.assertEqual({call[0] for call in self.transport.calls}, {"GET"})
        self.assertTrue(all("oauth2.googleapis.com" not in call[1] for call in self.transport.calls))
        self.assertEqual(self.store.status()["pending"], [])
        original_ids = {event["uid"]: event["id"] for event in self.store.list_events("family", True)}
        original_apple_ids = set(self.apple.events)

        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)
        self.engine = SyncEngine(self.store, {"google": self.provider, "apple": self.apple})
        self.transport.responses.extend([http.google_acl(), http.google_page([], token="second"), http.google_acl()])
        self.assertEqual(self.sync()["delivered"], 0)
        self.assertIn("syncToken=first", self.transport.calls[-2][1])
        self.assertEqual({event["uid"]: event["id"] for event in self.store.list_events("family", True)}, original_ids)
        self.assertEqual(set(self.apple.events), original_apple_ids)
        self.assertEqual(self.apple.puts, 6)
        self.assertEqual({call[0] for call in self.transport.calls}, {"GET"})
        self.assertEqual(self.store.status()["pending"], [])
        self.assertEqual(self.local_by_summary()["Synthetic unknown end"]["payload"], expected["Synthetic unknown end"])

    def test_detached_source_then_local_edit_keep_mapping_uid_and_source_timestamp(self):
        self.queue_full_import()
        self.sync()
        original = self.local_by_summary()["Synthetic detached"]
        original_uid = original["uid"]
        self.now += 20
        changed = deepcopy(self.items[5])
        changed.update(summary="Synthetic source edit", etag='"source-v2"',
                       updated=datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        self.items[5] = changed
        # An exception delta requires a complete bundle scan. A missing parent
        # is checked again before the visible detached occurrence is accepted.
        self.transport.responses.extend([http.google_acl(), http.google_page([changed], token="delta"),
                                         http.google_page(self.items, token="after-source"), http.response(status=404), http.google_acl()])
        self.assertEqual(self.sync()["delivered"], 1)
        edited = self.local_by_summary()["Synthetic source edit"]
        self.assertEqual(edited["id"], original["id"])
        self.assertEqual(edited["uid"], original_uid)
        self.assertEqual(edited["modified_at"], self.now)
        self.assertEqual(len(self.apple.active()), 6)
        self.assertEqual(next(event for event in self.apple.active() if event.uid == original_uid).payload, edited["payload"])
        self.assertEqual({call[0] for call in self.transport.calls}, {"GET"})
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarEvent, original["id"]).source, "google")

        self.now += 20
        wanted = {**edited["payload"], "summary": "Synthetic local edit"}
        self.store.save_local("family", wanted, edited["id"], edited["revision"])
        after = {**changed, **wanted, "etag": '"local-v3"',
                 "updated": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                 "extendedProperties": {"private": {"hestia_uid": original_uid,
                                                    "hestia_detached_instance": "detached", "hestia_end_unspecified": "false"}}}
        self.transport.responses.extend([http.google_acl(), http.google_page([], token="before-local"), http.google_acl(),
                                         http.google_acl(), http.response(changed), http.response(after), http.response(after)])
        self.assertEqual(self.sync()["delivered"], 2)
        local = self.local_by_summary()["Synthetic local edit"]
        self.assertEqual((local["id"], local["uid"]), (original["id"], original_uid))
        writes = [call for call in self.transport.calls if call[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], "PATCH")
        self.assertTrue(writes[0][1].split("?")[0].endswith("/events/detached"))
        self.assertEqual(writes[0][2]["headers"]["If-Match"], '"source-v2"')
        body = json.loads(writes[0][2]["body"])
        self.assertEqual(body["extendedProperties"]["private"]["hestia_uid"], original_uid)
        self.assertNotIn("recurrence", body)
        self.assertEqual(next(event for event in self.apple.active() if event.uid == original_uid).payload, wanted)
        self.assertEqual(len(self.store.list_events("family")), 6)
        self.assertEqual(self.store.status()["pending"], [])
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarReplica, ("google", original["id"])).remote_id, "detached")
            self.assertEqual(session.get(db.CalendarEvent, original["id"]).source, "local")
