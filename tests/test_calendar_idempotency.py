"""Durable create identities under response loss, concurrent retries and deletion."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from alembic import command
from sqlalchemy import select

from hestia import db
from hestia.calendar_sync.engine import SyncEngine
from hestia.calendar_sync.store import CalendarConflict, SyncStore
from tests import test_calendar_sync as fixtures


class CalendarCreateIdentityTests(unittest.TestCase):
    setUp = fixtures.SyncTests.setUp
    tearDown = fixtures.SyncTests.tearDown
    tick = fixtures.SyncTests.tick
    run_sync = fixtures.SyncTests.run_sync
    key = "calendar-create-key-0001"

    def create(self, title="Family", key=None):
        return self.store.save_local("family", fixtures.payload(title), idempotency_key=key or self.key)

    def restart(self):
        db.reset_engine_cache()
        _, self.sessions = db.init_db(self.path)
        self.store = SyncStore(self.sessions, clock=lambda: self.now)
        self.engine = SyncEngine(self.store, self.providers)

    def test_lost_response_retry_survives_restart_without_resetting_delivery_backoff(self):
        created = self.create()
        self.providers["apple"].write_error = TimeoutError("Synthetic lost provider response")
        self.engine.run_once()
        pending = self.store.status()["pending"]
        self.restart()
        self.assertEqual(self.create(), created)
        self.assertEqual(self.store.status()["pending"], pending)
        self.assertEqual(len(self.store.list_events("family")), 1)
        self.providers["apple"].write_error = None
        self.tick(10)
        self.run_sync()
        self.assertEqual(len(self.providers["apple"].active()), 1)
        self.assertEqual(len(self.providers["google"].active()), 1)

    def test_original_retry_returns_current_remote_edit_and_tombstone_without_rewriting(self):
        created = self.create()
        self.run_sync()
        self.tick()
        google = self.providers["google"]
        google.remote(google.active()[0].remote_id, fixtures.payload("Remote edit"))
        self.run_sync()
        edited = self.store.list_events("family")[0]
        self.assertEqual(edited["payload"], fixtures.payload("Remote edit"))
        self.assertEqual(self.create(), edited)
        self.assertEqual(self.store.status()["pending"], [])
        with self.assertRaises(CalendarConflict):
            self.create("Changed retry")
        self.assertEqual(self.store.list_events("family"), [edited])
        tombstone = self.store.delete_local("family", created["id"])
        self.run_sync()
        self.restart()
        self.assertEqual(self.create(), tombstone)
        with self.assertRaises(CalendarConflict):
            self.create("Changed deleted retry")
        self.assertEqual(self.store.list_events("family"), [])
        self.assertEqual(self.store.list_events("family", include_deleted=True), [tombstone])
        self.assertEqual(self.store.status()["pending"], [])

    def test_simultaneous_same_key_requests_commit_one_event_and_one_set_of_jobs(self):
        def attempt(_):
            store = SyncStore(self.sessions, clock=lambda: self.now)
            return store.save_local("family", fixtures.payload(), idempotency_key=self.key)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(attempt, range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.store.list_events("family")), 1)
        self.assertEqual(len(self.store.status()["pending"]), 2)

    def test_key_scope_is_group_and_different_keys_preserve_intentional_equal_content(self):
        first = self.create()
        second = self.create(key="calendar-create-key-0002")
        self.assertNotEqual(first["id"], second["id"])
        self.store.configure(self.groups + [{"id": "other", "calendars": []}])
        other = self.store.save_local("other", fixtures.payload(), idempotency_key=self.key)
        self.assertNotIn(other["id"], (first["id"], second["id"]))
        with self.sessions() as session:
            original = session.get(db.CalendarEvent, first["id"])
            self.assertEqual(original.create_key, self.key)
            self.assertEqual(original.create_fingerprint, original.fingerprint)
        self.assertNotIn("create_key", first)
        self.assertNotIn("create_fingerprint", first)

    def test_validation_and_keyed_edit_rejection_do_not_allocate_events(self):
        for key in (False, 1, [], "", "short", "x" * 129, "calendar/path/key", "calendar-key-line\n"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.save_local("family", fixtures.payload(), idempotency_key=key)
        self.assertEqual(self.store.list_events("family"), [])
        created = self.create()
        with self.assertRaises(ValueError):
            self.store.save_local("family", fixtures.payload("Edit"), event_id=created["id"], idempotency_key=self.key)
        self.assertEqual(self.store.list_events("family"), [created])

    def test_failed_transaction_does_not_reserve_key_or_leave_orphaned_event(self):
        with mock.patch.object(self.store, "_enqueue", side_effect=RuntimeError("Synthetic interrupted commit")):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(self.store.list_events("family"), [])
        self.assertEqual(self.store.status()["pending"], [])
        created = self.create()
        self.assertEqual(self.create(), created)
        with self.sessions() as session:
            self.assertEqual(len(list(session.scalars(select(db.CalendarEvent)))), 1)


class CalendarCreateMigrationTests(unittest.TestCase):
    def test_0005_preserves_existing_events_replicas_and_jobs_in_both_directions(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = db.make_engine(Path(temporary) / "legacy.db")
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                command.upgrade(db._alembic_config(connection), "0004")
                connection.exec_driver_sql("INSERT INTO calendar_groups VALUES ('family',1)")
                connection.exec_driver_sql("INSERT INTO calendar_endpoints "
                    "(id,group_id,provider,account,calendar_id,enabled) VALUES ('google','family','google','test','test',1)")
                connection.exec_driver_sql("INSERT INTO calendar_events "
                    "(id,group_id,uid,payload_json,fingerprint,deleted,revision,modified_at,source) "
                    "VALUES ('legacy','family','legacy@test','{}','original',0,1,1000,'local')")
                connection.exec_driver_sql("INSERT INTO calendar_replicas "
                    "(endpoint_id,event_id,remote_id,applied_revision,seen,deleted,uncertain) "
                    "VALUES ('google','legacy','reserved',0,0,0,1)")
                connection.exec_driver_sql("INSERT INTO calendar_outbox VALUES ('google','legacy',1,2,1005,'timeout')")
            with engine.begin() as connection:
                command.upgrade(db._alembic_config(connection), "head")
                self.assertEqual(connection.exec_driver_sql(
                    "SELECT id,create_key,create_fingerprint FROM calendar_events").all(), [("legacy", None, None)])
                for table in ("calendar_replicas", "calendar_outbox"):
                    plan = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE event_id='legacy'").all()
                    self.assertIn(f"ix_{table}_event_id", " ".join(str(row) for row in plan))
                connection.exec_driver_sql("UPDATE calendar_events SET create_key='calendar-legacy-key',create_fingerprint='original'")
            with engine.begin() as connection:
                command.downgrade(db._alembic_config(connection), "0004")
                self.assertEqual(connection.exec_driver_sql("SELECT id,fingerprint FROM calendar_events").all(), [("legacy", "original")])
                self.assertEqual(connection.exec_driver_sql("SELECT event_id,uncertain FROM calendar_replicas").all(), [("legacy", 1)])
                self.assertEqual(connection.exec_driver_sql("SELECT event_id,attempts FROM calendar_outbox").all(), [("legacy", 2)])
            with engine.begin() as connection:
                command.upgrade(db._alembic_config(connection), "head")
                self.assertEqual(connection.exec_driver_sql("SELECT create_key FROM calendar_events").all(), [(None,)])
