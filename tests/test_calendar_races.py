"""Inventory isolation and timestamp/identity races against real SQLite checkpoints."""
from copy import deepcopy

from sqlalchemy import select
import unittest

from hestia import db
from hestia.calendar_sync.engine import SyncEngine
from hestia.calendar_sync.types import RemoteEvent
from tests import test_calendar_sync as fixtures


class CalendarRaceTests(unittest.TestCase):
    setUp = fixtures.SyncTests.setUp
    tearDown = fixtures.SyncTests.tearDown
    tick = fixtures.SyncTests.tick
    local = fixtures.SyncTests.local
    run_sync = fixtures.SyncTests.run_sync
    assert_content = fixtures.SyncTests.assert_content

    def test_uid_reuse_rolls_back_one_inventory_without_losing_healthy_endpoint(self):
        original = self.store.save_local("family", fixtures.payload())
        self.run_sync()
        before = self.store.endpoints()[0]["cursor"]
        self.tick()
        apple = self.providers["apple"]
        known = apple.active()[0]
        # Place a new resource before the malformed reused URL to exercise rollback
        # of successful earlier objects from the same inventory.
        apple.events = {}
        apple.remote("temporary", fixtures.payload("Must roll back"), uid="temporary-uid")
        apple.remote(known.remote_id, fixtures.payload("Wrong UID"), uid="replacement-uid")
        google = self.providers["google"]
        google.remote(google.active()[0].remote_id, fixtures.payload("Healthy edit"))
        counts = apple.puts
        result = self.engine.run_once()
        self.assertEqual(result["errors"], {"apple": "invalid_calendar_data"})
        self.assertEqual(result["read"], 1)
        self.assertEqual(self.local()[0]["payload"], fixtures.payload("Healthy edit"))
        self.assertEqual(self.local()[0]["uid"], original["uid"])
        self.assertEqual(len(self.local()), 1)
        self.assertEqual(apple.puts, counts)
        self.assertEqual(self.store.endpoints()[0]["cursor"], before)
        with self.sessions() as session:
            self.assertIsNone(session.scalar(select(db.CalendarEvent).where(db.CalendarEvent.uid == "temporary-uid")))

    def test_new_resource_with_existing_uid_is_quarantined_and_checkpoint_is_retained(self):
        self.store.save_local("family", fixtures.payload())
        self.run_sync()
        provider = self.providers["apple"]
        original = provider.active()[0]
        before = self.store.endpoints()[0]["cursor"]
        provider.events.clear()
        provider.remote("new-url", fixtures.payload("Copied UID"), uid=original.uid)
        result = self.engine.run_once()
        self.assertEqual(result["errors"], {"apple": "invalid_calendar_data"})
        self.assertEqual(self.store.endpoints()[0]["cursor"], before)
        self.assertEqual(self.local()[0]["payload"], fixtures.payload())
        self.assertFalse(self.local()[0]["deleted"])

    def test_google_deleted_stub_uses_reserved_remote_id_and_keeps_delete_provenance(self):
        created = self.store.save_local("family", fixtures.payload())
        self.run_sync()
        provider = self.providers["google"]
        remote = provider.active()[0]
        provider.remote(remote.remote_id, None, uid="google-deleted:" + remote.remote_id, deleted=True)
        self.run_sync()
        with self.sessions() as session:
            tombstone = session.get(db.CalendarEvent, created["id"])
            self.assertTrue(tombstone.deleted)
            self.assertEqual(tombstone.source, "google")
        self.assertEqual(self.providers["apple"].active(), [])

    def test_local_and_missing_full_inventory_deletions_record_source(self):
        created = self.store.save_local("family", fixtures.payload())
        self.run_sync()
        self.store.delete_local("family", created["id"])
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarEvent, created["id"]).source, "local")
        self.run_sync()
        second = self.store.save_local("family", fixtures.payload("Second"))
        self.run_sync()
        self.providers["apple"].events.clear()
        self.run_sync()
        with self.sessions() as session:
            self.assertEqual(session.get(db.CalendarEvent, second["id"]).source, "apple")

    def test_missing_source_timestamp_uses_observation_time_and_subsequent_edits_work(self):
        provider = self.providers["apple"]
        provider.events["source"] = RemoteEvent("source", "uid", fixtures.payload(), "1", 0)
        self.run_sync()
        self.assertEqual(self.local()[0]["modified_at"], self.now)
        self.tick()
        provider.events["source"] = RemoteEvent("source", "uid", fixtures.payload("No timestamp edit"), "2", 0)
        self.run_sync()
        self.assert_content(fixtures.payload("No timestamp edit"))
        self.assertEqual(self.local()[0]["modified_at"], self.now)

    def test_same_winning_source_can_edit_multiple_times_in_the_same_second(self):
        provider = self.providers["google"]
        provider.remote("source", fixtures.payload())
        self.run_sync()
        provider.remote("source", fixtures.payload("Same second edit"))
        self.run_sync()
        self.assert_content(fixtures.payload("Same second edit"))

    def test_uncertain_delivery_finishes_desired_unit_before_destination_edit(self):
        created = self.store.save_local("family", fixtures.payload())
        self.run_sync()
        self.tick()
        wanted = fixtures.payload("Durable desired")
        self.store.save_local("family", wanted, created["id"])
        apple = self.providers["apple"]
        apple.write_error = TimeoutError("unknown whether multi-operation request completed")
        self.engine.run_once()
        self.tick()
        apple.remote(apple.active()[0].remote_id, fixtures.payload("Destination edit during uncertain delivery"))
        apple.write_error = None
        self.run_sync()
        self.assert_content(wanted)
        # Once delivery is confirmed, normal later destination edits win again.
        self.tick()
        apple.remote(apple.active()[0].remote_id, fixtures.payload("After delivery"))
        self.run_sync()
        self.assert_content(fixtures.payload("After delivery"))

    def test_another_source_edit_can_replace_an_uncertain_pending_unit(self):
        created = self.store.save_local("family", fixtures.payload())
        self.run_sync()
        self.tick()
        self.store.save_local("family", fixtures.payload("Pending"), created["id"])
        apple = self.providers["apple"]
        apple.write_error = TimeoutError()
        self.engine.run_once()
        self.tick()
        google = self.providers["google"]
        google.remote(google.active()[0].remote_id, fixtures.payload("Other source newer"))
        apple.write_error = None
        self.run_sync()
        self.assert_content(fixtures.payload("Other source newer"))

    def test_second_engine_can_finish_a_job_between_due_query_and_prepare(self):
        self.store.save_local("family", fixtures.payload())
        due = self.store.due
        second = SyncEngine(self.store, self.providers)

        def concurrent_due(healthy):
            jobs = due(healthy)
            self.store.due = due
            second.run_once()
            return jobs

        self.store.due = concurrent_due
        self.assertEqual(self.run_sync()["delivered"], 0)
        self.assert_content(fixtures.payload())

    def test_local_edit_during_source_pull_wins_over_older_fetched_snapshot(self):
        created = self.store.save_local("family", fixtures.payload())
        self.run_sync()
        provider = self.providers["apple"]
        pull = provider.pull

        def racing_pull(cursor):
            stale = deepcopy(pull(cursor))
            provider.pull = pull
            self.tick()
            self.store.save_local("family", fixtures.payload("Edited while reading"), created["id"], created["revision"])
            return stale

        provider.pull = racing_pull
        self.run_sync()
        self.assert_content(fixtures.payload("Edited while reading"))
