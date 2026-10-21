"""End-to-end reconciliation using fake external calendars and real migrated SQLite."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import tempfile
import unittest

from sqlalchemy import select

from hestia import db
from hestia.calendar_sync.engine import SyncEngine, error_code, validate_snapshot
from hestia.calendar_sync.events import fingerprint, validate_payload
from hestia.calendar_sync.store import CalendarConflict, SyncStore
from hestia.calendar_sync.types import ProviderError, RemoteEvent, Snapshot


def payload(title="Family", **changes):
    result = {"summary": title, "start": {"dateTime": "2026-10-24T12:00:00+02:00", "timeZone": "Europe/Warsaw"},
              "end": {"dateTime": "2026-10-24T13:00:00+02:00", "timeZone": "Europe/Warsaw"}}
    result.update(changes)
    return validate_payload(result)


class FakeCalendar:
    def __init__(self, clock):
        self.clock = clock
        self.events = {}
        self.sequence = 0
        self.puts = self.deletes = 0
        self.pull_error = self.write_error = None
        self.after_write = None
        self.full = self.complete = True

    def allocate_id(self, uid):
        return "h" + hashlib.sha256(uid.encode()).hexdigest()

    def remote(self, identifier, content, *, uid=None, deleted=False):
        self.sequence += 1
        prior = self.events.get(identifier)
        event = RemoteEvent(identifier, uid or (prior.uid if prior else identifier + "@source"),
                            deepcopy(content), str(self.sequence), self.clock(), deleted)
        self.events[identifier] = event
        return event

    def pull(self, cursor):
        if self.pull_error:
            raise self.pull_error
        return Snapshot(list(self.events.values()), str(self.sequence), self.complete, self.full)

    def put(self, remote_id, content, etag, *, create, uid=None):
        self.puts += 1
        if self.write_error:
            raise self.write_error
        current = self.events.get(remote_id)
        if current and not current.deleted and fingerprint(current.payload) == fingerprint(content):
            return current
        if current and etag != current.etag:
            raise ProviderError("precondition", status=412, conflict=True)
        result = self.remote(remote_id, content, uid=uid)
        if self.after_write:
            self.after_write()
        return result

    def delete(self, remote_id, etag, *, uid=None):
        self.deletes += 1
        if self.write_error:
            raise self.write_error
        current = self.events.get(remote_id)
        if current and not current.deleted:
            if current.etag != etag:
                raise ProviderError("precondition", status=412, conflict=True)
            self.remote(remote_id, None, deleted=True)
        if self.after_write:
            self.after_write()

    def active(self):
        return [event for event in self.events.values() if not event.deleted]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.path = self.temp.name + "/sync.db"
        _, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)
        self.groups = [{"id": "family", "calendars": [
            {"id": name, "provider": name, "account": name, "calendar_id": name + "/family"}
            for name in ("apple", "google")]}]
        self.store.configure(self.groups)
        self.providers = {name: FakeCalendar(lambda: self.now) for name in ("apple", "google")}
        self.engine = SyncEngine(self.store, self.providers)

    def tearDown(self):
        db.reset_engine_cache()
        self.temp.cleanup()

    def tick(self, seconds=10):
        self.now += seconds

    def local(self):
        return self.store.list_events("family")

    def run_sync(self):
        result = self.engine.run_once()
        self.assertEqual(result["errors"], {}, result)
        return result

    def assert_content(self, expected):
        self.assertEqual(len(self.local()), 1)
        self.assertEqual(self.local()[0]["payload"], expected)
        for provider in self.providers.values():
            self.assertEqual(len(provider.active()), 1)
            self.assertEqual(provider.active()[0].payload, expected)

    def crud(self, origin):
        first = payload()
        if origin == "local":
            self.store.save_local("family", first)
        else:
            self.providers[origin].remote("source", first)
        self.run_sync()
        self.assert_content(first)
        identifier = self.local()[0]["id"]
        self.tick()
        edited = payload("Dinner", start={"date": "2026-10-26"}, end={"date": "2026-10-27"})
        if origin == "local":
            self.store.save_local("family", edited, identifier, self.local()[0]["revision"])
        else:
            event = self.providers[origin].active()[0]
            self.providers[origin].remote(event.remote_id, edited)
        self.run_sync()
        self.assert_content(edited)
        self.tick()
        if origin == "local":
            self.store.delete_local("family", identifier, self.local()[0]["revision"])
        else:
            event = self.providers[origin].active()[0]
            self.providers[origin].remote(event.remote_id, None, deleted=True)
        self.run_sync()
        self.assertEqual(self.local(), [])
        self.assertTrue(self.store.list_events("family", True)[0]["deleted"])
        self.assertTrue(all(not provider.active() for provider in self.providers.values()))
        self.run_sync()
        self.assertEqual(self.store.status()["pending"], [])

    def test_local_create_edit_delete_all_external_copies(self):
        self.crud("local")

    def test_apple_create_edit_delete_google_and_local(self):
        self.crud("apple")

    def test_google_create_edit_delete_apple_and_local(self):
        self.crud("google")

    def test_repeated_rounds_do_not_write_or_duplicate(self):
        self.store.save_local("family", payload())
        self.run_sync()
        counts = [p.puts for p in self.providers.values()]
        for _ in range(5):
            self.run_sync()
        self.assertEqual(counts, [p.puts for p in self.providers.values()])
        self.assert_content(payload())

    def test_response_lost_after_create_then_restart_uses_same_id(self):
        self.store.save_local("family", payload())
        provider = self.providers["apple"]
        provider.after_write = lambda: (_ for _ in ()).throw(SystemExit("simulated process exit"))
        with self.assertRaises(SystemExit):
            self.engine.run_once()
        self.assertEqual(len(provider.active()), 1)
        provider.after_write = None
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)
        self.engine = SyncEngine(self.store, self.providers)
        self.run_sync()
        self.assert_content(payload())
        self.assertEqual(provider.puts, 1)

    def test_timeout_after_success_does_not_duplicate(self):
        self.store.save_local("family", payload())
        provider = self.providers["apple"]
        provider.after_write = lambda: (_ for _ in ()).throw(TimeoutError("sensitive body"))
        result = self.engine.run_once()
        self.assertEqual(result["errors"]["apple"], "connection_or_provider_failure")
        provider.after_write = None
        self.run_sync()
        self.assert_content(payload())
        self.assertEqual(provider.puts, 1)

    def test_restart_with_pending_offline_operations(self):
        self.providers["apple"].pull_error = OSError("offline")
        self.store.save_local("family", payload())
        self.engine.run_once()
        self.assertEqual(len(self.store.status()["pending"]), 1)
        self.providers["apple"].pull_error = None
        self.engine = SyncEngine(SyncStore(self.sessions, clock=lambda: self.now), self.providers)
        self.run_sync()
        self.assert_content(payload())

    def test_write_failures_backoff_then_retry_without_losing_other_endpoint(self):
        self.providers["apple"].write_error = ProviderError("unavailable", status=503, retryable=True)
        self.store.save_local("family", payload())
        self.assertEqual(self.engine.run_once()["delivered"], 1)
        pending = self.store.status()["pending"][0]
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["next_attempt"], self.now + 5)
        self.engine.run_once()
        self.assertEqual(self.providers["apple"].puts, 1)
        self.tick()
        self.providers["apple"].write_error = None
        self.run_sync()
        self.assert_content(payload())

    def test_network_permission_and_partial_errors_never_delete(self):
        self.store.save_local("family", payload())
        self.run_sync()
        for error in (OSError("network"), ProviderError("forbidden", status=403), ValueError("incomplete")):
            self.providers["apple"].pull_error = error
            self.engine.run_once()
            self.assert_content(payload())
        self.providers["apple"].pull_error = None
        self.providers["apple"].complete = False
        self.providers["apple"].events.clear()
        self.assertIn("apple", self.engine.run_once()["errors"])
        self.assertEqual(len(self.local()), 1)
        self.assertEqual(len(self.providers["google"].active()), 1)

    def test_complete_full_missing_event_is_a_deletion(self):
        self.store.save_local("family", payload())
        self.run_sync()
        self.providers["apple"].events.clear()
        self.run_sync()
        self.assertEqual(self.local(), [])
        self.assertEqual(self.providers["google"].active(), [])

    def test_incremental_absence_is_not_deletion_or_cursor_reset(self):
        self.store.save_local("family", payload())
        self.run_sync()
        self.providers["apple"].events.clear()
        self.providers["apple"].full = False
        self.run_sync()
        self.assertEqual(len(self.local()), 1)
        self.assertEqual(self.store.endpoints()[0]["cursor"], "1")

    def test_failed_read_does_not_advance_cursor(self):
        self.run_sync()
        self.providers["apple"].sequence = 20
        self.providers["apple"].complete = False
        self.engine.run_once()
        self.assertEqual(self.store.endpoints()[0]["cursor"], "0")

    def test_concurrent_edits_latest_source_timestamp_wins(self):
        self.store.save_local("family", payload())
        self.run_sync()
        self.tick()
        apple = self.providers["apple"].active()[0]
        self.providers["apple"].remote(apple.remote_id, payload("Apple"))
        self.tick()
        google = self.providers["google"].active()[0]
        self.providers["google"].remote(google.remote_id, payload("Google"))
        self.run_sync()
        self.assert_content(payload("Google"))

    def test_equal_timestamps_use_stable_endpoint_id(self):
        self.providers["apple"].remote("a", payload("Apple"), uid="same-uid")
        self.providers["google"].remote("g", payload("Google"), uid="same-uid")
        self.run_sync()
        self.assert_content(payload("Google"))

    def test_older_offline_edit_is_corrected_without_overriding_newer_source(self):
        event = self.store.save_local("family", payload())
        self.run_sync()
        apple = self.providers["apple"].active()[0]
        self.now = 900
        self.providers["apple"].remote(apple.remote_id, payload("Old offline"))
        self.now = 1100
        self.store.save_local("family", payload("Newest"), event["id"])
        self.run_sync()
        self.assert_content(payload("Newest"))

    def test_remote_edit_back_to_former_delivered_value_is_not_ignored(self):
        event = self.store.save_local("family", payload())
        self.run_sync()
        self.tick()
        self.store.save_local("family", payload("Second"), event["id"])
        self.run_sync()
        self.tick()
        apple = self.providers["apple"].active()[0]
        self.providers["apple"].remote(apple.remote_id, payload())
        self.run_sync()
        self.assert_content(payload())

    def test_delete_wins_over_newer_edit_and_stale_copy_never_resurrects(self):
        event = self.store.save_local("family", payload())
        self.run_sync()
        apple = self.providers["apple"].active()[0]
        google = self.providers["google"].active()[0]
        self.providers["apple"].remote(apple.remote_id, None, deleted=True)
        self.tick()
        self.providers["google"].remote(google.remote_id, payload("Newer edit"))
        self.run_sync()
        self.assertEqual(self.local(), [])
        self.tick()
        self.providers["google"].remote(google.remote_id, payload("Stale resurrection"))
        self.run_sync()
        self.assertEqual(self.local(), [])
        self.assertEqual(self.providers["google"].active(), [])
        with self.assertRaises(CalendarConflict):
            self.store.save_local("family", payload(), event["id"])

    def test_only_selected_group_calendars_receive_events(self):
        self.store.configure(self.groups + [{"id": "private", "calendars": []}])
        self.store.save_local("private", payload("Private"))
        self.run_sync()
        self.assertEqual(self.local(), [])
        self.assertTrue(all(not p.active() for p in self.providers.values()))

    def test_disabled_endpoint_preserves_queue_and_reenable_catches_up(self):
        self.store.save_local("family", payload())
        self.store.configure([{**self.groups[0], "calendars": self.groups[0]["calendars"][:1]}])
        self.run_sync()
        self.assertEqual(self.providers["google"].active(), [])
        self.store.configure(self.groups)
        self.run_sync()
        self.assert_content(payload())

    def test_endpoint_identity_reassignment_rolls_back(self):
        changed = deepcopy(self.groups)
        changed[0]["calendars"][0]["calendar_id"] = "other"
        with self.assertRaises(CalendarConflict):
            self.store.configure(changed)
        self.assertEqual(len(self.store.endpoints()), 2)

    def test_local_optimistic_revision_and_idempotent_operations(self):
        event = self.store.save_local("family", payload())
        same = self.store.save_local("family", payload(), event["id"], 1)
        self.assertEqual(same["revision"], 1)
        with self.assertRaises(CalendarConflict):
            self.store.save_local("family", payload("Changed"), event["id"], 0)
        for revision in (True, "1"):
            with self.assertRaises(CalendarConflict):
                self.store.delete_local("family", event["id"], revision)
        deleted = self.store.delete_local("family", event["id"], 1)
        again = self.store.delete_local("family", event["id"], deleted["revision"])
        self.assertEqual(deleted, again)
        for operation in (lambda: self.store.list_events("missing"),
                          lambda: self.store.save_local("missing", payload()),
                          lambda: self.store.delete_local("family", "missing")):
            with self.assertRaises(ValueError):
                operation()

    def test_local_edit_while_http_in_flight_keeps_new_revision_queued(self):
        event = self.store.save_local("family", payload())
        def concurrent_edit():
            self.providers["apple"].after_write = None
            self.tick()
            self.store.save_local("family", payload("Concurrent local"), event["id"])
        self.providers["apple"].after_write = concurrent_edit
        self.run_sync()
        self.assertTrue(self.store.status()["pending"])
        self.run_sync()
        self.assert_content(payload("Concurrent local"))

    def test_precondition_conflict_reloads_remote_and_reconciles(self):
        event = self.store.save_local("family", payload())
        self.run_sync()
        self.tick()
        self.store.save_local("family", payload("Local"), event["id"])
        provider = self.providers["apple"]
        original_put = provider.put
        def race(remote_id, content, etag, **kwargs):
            self.tick()
            provider.remote(remote_id, payload("Concurrent remote"))
            provider.put = original_put
            return original_put(remote_id, content, etag, **kwargs)
        provider.put = race
        self.assertEqual(self.engine.run_once()["errors"], {"apple": "conflict"})
        self.tick()
        self.run_sync()
        self.assert_content(payload("Concurrent remote"))

    def test_partial_destination_write_is_retried_from_durable_intent(self):
        event = self.store.save_local("family", payload())
        self.run_sync()
        self.tick()
        desired = payload("New series", recurrence=["RRULE:FREQ=DAILY;COUNT=3"])
        self.store.save_local("family", desired, event["id"])
        provider = self.providers["apple"]
        original_put = provider.put
        def partial(remote_id, content, etag, **kwargs):
            provider.remote(remote_id, payload("Partially copied"), uid=kwargs["uid"])
            raise ProviderError("partial write", retryable=True)
        provider.put = partial
        self.engine.run_once()
        provider.put = original_put
        self.tick()
        self.run_sync()
        self.assert_content(desired)

    def test_delete_response_lost_and_pending_delete_survives_restart(self):
        event = self.store.save_local("family", payload())
        self.run_sync()
        self.store.delete_local("family", event["id"])
        self.providers["apple"].after_write = lambda: (_ for _ in ()).throw(TimeoutError())
        self.engine.run_once()
        self.providers["apple"].after_write = None
        self.engine = SyncEngine(self.store, self.providers)
        self.run_sync()
        self.assertEqual(self.store.status()["pending"], [])
        self.assertEqual(self.local(), [])

    def test_all_day_recurrence_dst_and_exceptions_are_kept_as_one_series(self):
        recurring = payload("Weekly", recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"], exceptions=[
            {"originalStartTime": {"dateTime": "2026-10-31T12:00:00+01:00", "timeZone": "Europe/Warsaw"},
             "status": "cancelled"}])
        self.providers["apple"].remote("series", recurring)
        self.run_sync()
        self.assert_content(recurring)

    def test_snapshot_validation_and_sanitized_errors(self):
        good = RemoteEvent("id", "uid", payload(), "tag", 1)
        for snapshot in (Snapshot([], None, False, True), Snapshot([good, good], None, True, True),
                         Snapshot([RemoteEvent("", "uid", payload(), None, 1)], None, True, True),
                         Snapshot([RemoteEvent("id", "", payload(), None, 1)], None, True, True),
                         Snapshot([RemoteEvent("id", "uid", payload(), None, "x")], None, True, True),
                         Snapshot([RemoteEvent("id", "uid", payload(), None, float("nan"))], None, True, True)):
            with self.assertRaises(ValueError):
                validate_snapshot(snapshot)
        validate_snapshot(Snapshot([RemoteEvent("id", "uid", None, None, 1, True)], None, True, True))
        self.assertEqual(error_code(ProviderError("token=secret")), "provider_error")
        self.assertEqual(error_code(ProviderError("token=secret", status=401)), "permission_denied")
        self.assertEqual(error_code(TypeError("token=secret")), "invalid_calendar_data")
        self.assertEqual(error_code(ProviderError("private", code="google_event_type_restricted")),
                         "google_event_type_restricted")
        self.assertEqual(error_code(ProviderError("private", code="token=secret")), "provider_error")

    def test_provider_restricted_write_is_visible_and_does_not_hide_later_source_edits(self):
        created = self.store.save_local("family", payload())
        self.run_sync()
        self.tick()
        self.store.save_local("family", payload("Restricted local edit"), created["id"])
        google = self.providers["google"]
        google.write_error = ProviderError("Cannot change fields of this event type",
                                           code="google_event_type_restricted")
        result = self.engine.run_once()
        self.assertEqual(result["errors"], {"google": "google_event_type_restricted"})
        self.assertEqual(self.store.status()["pending"][0]["last_error"], "google_event_type_restricted")
        self.tick()
        google.remote(google.active()[0].remote_id, payload("Newer allowed source edit"))
        self.run_sync()
        self.assert_content(payload("Newer allowed source edit"))

    def test_unknown_remote_tombstone_prevents_later_stale_import(self):
        self.providers["apple"].remote("gone", None, uid="deleted-uid", deleted=True)
        self.run_sync()
        self.providers["google"].remote("stale", payload(), uid="deleted-uid")
        self.run_sync()
        self.assertEqual(self.local(), [])
        self.assertEqual(self.providers["google"].active(), [])

    def test_mapping_integrity_prepare_removed_job_and_wrong_provider_id(self):
        self.assertIsNone(self.store.prepare("apple", "missing", lambda uid: uid))
        event = self.store.save_local("family", payload())
        intent = self.store.prepare("apple", event["id"], lambda uid: "reserved")
        with self.assertRaises(ValueError):
            self.store.acknowledge("apple", event["id"], intent,
                                   RemoteEvent("unexpected", event["uid"], payload(), "tag", 1))
        with self.assertRaises(ValueError):
            self.store.ingest({"apple": Snapshot([], None, False, True)})
        self.store.ingest({"apple": Snapshot([RemoteEvent("one", "uid", payload(), "tag", self.now)], None, True, False)})
        with self.assertRaises(ValueError):
            self.store.ingest({"apple": Snapshot([RemoteEvent("two", "uid", payload(), "tag", self.now)], None, True, False)})
        with self.sessions() as s:
            row = s.scalar(select(db.CalendarReplica).where(db.CalendarReplica.remote_id == "one"))
            self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
