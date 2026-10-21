"""Configuration reads stay cheap; endpoint activation backfills durable state."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import event as sqlalchemy_event, select

from hestia import db
from hestia.calendar_sync.engine import SyncEngine
from hestia.calendar_sync.store import CalendarConflict, SyncStore
from tests.test_calendar_sync import FakeCalendar, payload


class CalendarStoreConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine, self.sessions = db.init_db(self.temp.name + "/sync.db")
        self.store = SyncStore(self.sessions, clock=lambda: 1000)
        self.groups = [{"id": "family", "calendars": [
            {"id": name, "provider": name, "account": name, "calendar_id": name + "/family"}
            for name in ("apple", "google")]}]
        self.store.configure(self.groups)

    def tearDown(self):
        db.reset_engine_cache()
        self.temp.cleanup()

    @contextmanager
    def statements(self):
        statements = []

        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append((" ".join(statement.upper().split()), parameters))

        sqlalchemy_event.listen(self.engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            sqlalchemy_event.remove(self.engine, "before_cursor_execute", record)

    def test_unchanged_configuration_reads_only_metadata_without_write_lock(self):
        retired = {"id": "retired", "calendars": [
            {"id": "old", "provider": "caldav", "account": "wife", "calendar_id": "old/"}]}
        self.store.configure(self.groups + [retired])
        self.store.configure(self.groups)
        saved = self.store.save_local("family", payload())
        with self.store.transaction() as s:
            endpoint = s.get(db.CalendarEndpoint, "apple")
            endpoint.cursor = "checkpoint"
            endpoint.last_error = "offline"
            job = s.get(db.CalendarOutbox, ("apple", saved["id"]))
            job.attempts, job.next_attempt, job.last_error = 4, 2000, "offline"
        before = self.store.status()
        reordered = deepcopy(self.groups)
        reordered[0]["calendars"].reverse()
        with self.statements() as statements:
            self.store.configure(reordered)
            # API requests construct another store; no process-local cache is required.
            SyncStore(self.sessions).configure(self.groups)
        self.assertEqual([sql for sql, _ in statements if sql == "BEGIN"], ["BEGIN", "BEGIN"])
        selects = [sql for sql, _ in statements if sql != "BEGIN"]
        self.assertEqual(len(selects), 4)
        self.assertTrue(all(sql.startswith("SELECT ") for sql in selects))
        self.assertTrue(all("CALENDAR_GROUPS" in sql or "CALENDAR_ENDPOINTS" in sql for sql in selects))
        self.assertFalse(any("CALENDAR_EVENTS" in sql or "CALENDAR_OUTBOX" in sql or "CALENDAR_REPLICAS" in sql
                             for sql, _ in statements))
        self.assertEqual(self.store.status(), before)

    def test_retiring_endpoints_or_changing_groups_never_scans_events(self):
        self.store.save_local("family", payload())
        before = self.store.status()["pending"]
        with self.statements() as statements:
            self.store.configure([{**self.groups[0], "calendars": self.groups[0]["calendars"][:1]},
                                  {"id": "private", "calendars": []}])
            self.store.configure([])
            self.store.configure([{**self.groups[0], "calendars": []}])
        self.assertTrue(any(sql == "BEGIN IMMEDIATE" for sql, _ in statements))
        self.assertFalse(any("CALENDAR_EVENTS" in sql for sql, _ in statements))
        self.assertEqual(self.store.status()["pending"], before)
        self.assertEqual(self.store.groups(), [{"id": "family"}])
        self.assertEqual(self.store.endpoints(), [])

    def test_new_and_reenabled_endpoints_backfill_only_their_group_and_keep_tombstones(self):
        providers = {name: FakeCalendar(lambda: 1000) for name in ("apple", "google")}
        active = self.store.save_local("family", payload("Active"))
        removed = self.store.save_local("family", payload("To delete"))
        self.assertEqual(SyncEngine(self.store, providers).run_once()["errors"], {})
        active = self.store.save_local("family", payload("Queued version"), active["id"], active["revision"])
        with self.store.transaction() as s:
            job = s.get(db.CalendarOutbox, ("google", active["id"]))
            job.attempts, job.next_attempt, job.last_error = 4, 2000, "offline"
            s.get(db.CalendarEndpoint, "google").cursor = "durable-checkpoint"
        only_apple = [{**self.groups[0], "calendars": self.groups[0]["calendars"][:1]}]
        self.store.configure(only_apple + [{"id": "private", "calendars": []}])
        active = self.store.save_local("family", payload("Latest version"), active["id"], active["revision"])
        removed = self.store.delete_local("family", removed["id"], removed["revision"])
        added = self.store.save_local("family", payload("Created while disabled"))
        never_copied = self.store.save_local("family", payload("Deleted while disabled"))
        self.store.delete_local("family", never_copied["id"], never_copied["revision"])
        private = self.store.save_local("private", payload("Private"))
        with self.store.transaction() as s:
            job = s.get(db.CalendarOutbox, ("apple", active["id"]))
            job.attempts, job.next_attempt, job.last_error = 3, 1500, "offline"
        with self.sessions() as s:
            original_replicas = [(r.event_id, r.remote_id, r.etag, r.applied_revision, r.deleted, r.uncertain)
                                 for r in s.scalars(select(db.CalendarReplica).where(db.CalendarReplica.endpoint_id == "google"))]
        desired = deepcopy(self.groups)
        desired[0]["calendars"].append({"id": "extra", "provider": "caldav", "account": "wife", "calendar_id": "extra/"})
        desired.append({"id": "private", "calendars": []})
        before_events = self.store.list_events("family", include_deleted=True)
        with self.statements() as statements:
            self.store.configure(desired)
        event_queries = [(sql, parameters) for sql, parameters in statements if "FROM CALENDAR_EVENTS" in sql]
        self.assertEqual(len(event_queries), 2)
        self.assertTrue(all("WHERE CALENDAR_EVENTS.GROUP_ID = ?" in sql and parameters == ("family",)
                            for sql, parameters in event_queries))
        target_queries = [(sql, parameters) for sql, parameters in statements
                          if "FROM CALENDAR_REPLICAS" in sql or "FROM CALENDAR_OUTBOX" in sql]
        self.assertTrue(all(parameters[0] in {"google", "extra"} for _, parameters in target_queries))
        self.assertEqual(self.store.list_events("family", include_deleted=True), before_events)
        with self.sessions() as s:
            jobs = {(j.endpoint_id, j.event_id): j for j in s.scalars(select(db.CalendarOutbox))}
            self.assertEqual({event_id for endpoint, event_id in jobs if endpoint == "google"},
                             {active["id"], removed["id"], added["id"]})
            self.assertEqual({event_id for endpoint, event_id in jobs if endpoint == "extra"},
                             {active["id"], added["id"]})
            self.assertFalse(any(event_id == private["id"] for _, event_id in jobs))
            job = jobs[("google", active["id"])]
            self.assertEqual((job.revision, job.attempts, job.next_attempt, job.last_error),
                             (active["revision"], 0, 0, None))
            job = jobs[("apple", active["id"])]
            self.assertEqual((job.attempts, job.next_attempt, job.last_error), (3, 1500, "offline"))
            replicas = [(r.event_id, r.remote_id, r.etag, r.applied_revision, r.deleted, r.uncertain)
                        for r in s.scalars(select(db.CalendarReplica).where(db.CalendarReplica.endpoint_id == "google"))]
            self.assertEqual(replicas, original_replicas)
            self.assertEqual(s.get(db.CalendarEndpoint, "google").cursor, "durable-checkpoint")
            self.assertTrue(s.get(db.CalendarEvent, removed["id"]).deleted)
            self.assertTrue(s.get(db.CalendarEvent, never_copied["id"]).deleted)

    def test_second_configurer_rechecks_under_write_lock_and_skips_duplicate_backfill(self):
        self.store.save_local("family", payload())
        desired = deepcopy(self.groups)
        desired[0]["calendars"].append({"id": "extra", "provider": "caldav", "account": "wife", "calendar_id": "extra/"})
        original_transaction = self.store.transaction
        statements = []

        @contextmanager
        def concurrent_configuration():
            SyncStore(self.sessions).configure(desired)
            statements.clear()
            with original_transaction() as s:
                yield s

        with self.statements() as statements, patch.object(self.store, "transaction", new=concurrent_configuration):
            self.store.configure(desired)
        self.assertEqual(statements[0][0], "BEGIN IMMEDIATE")
        self.assertTrue(all(sql.startswith("SELECT ") for sql, _ in statements[1:]))
        self.assertFalse(any("CALENDAR_EVENTS" in sql or "CALENDAR_OUTBOX" in sql for sql, _ in statements))
        self.assertEqual({row["id"] for row in self.store.endpoints()}, {"apple", "google", "extra"})

    def test_metadata_pair_uses_one_snapshot_during_concurrent_wal_commit(self):
        with self.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar(), "wal")
        initial = deepcopy(self.groups) + [{"id": "private", "calendars": []}]
        self.store.configure(initial)
        desired = deepcopy(initial)
        desired[0]["calendars"] = desired[0]["calendars"][:1]
        concurrent = [{**self.groups[0], "calendars": self.groups[0]["calendars"][:1]}]
        committed_between_reads = False

        def commit_between_reads(connection, cursor, statement, parameters, context, executemany):
            nonlocal committed_between_reads
            if not committed_between_reads and "FROM CALENDAR_GROUPS" in statement.upper():
                committed_between_reads = True
                SyncStore(self.sessions).configure(concurrent)

        sqlalchemy_event.listen(self.engine, "after_cursor_execute", commit_between_reads)
        try:
            self.store.configure(desired)
        finally:
            sqlalchemy_event.remove(self.engine, "after_cursor_execute", commit_between_reads)
        self.assertTrue(committed_between_reads)
        # Stitching the old group SELECT to the new endpoint SELECT would falsely
        # report a match and leave this requested group disabled.
        self.assertEqual(self.store.groups(), [{"id": "family"}, {"id": "private"}])
        self.assertEqual([endpoint["id"] for endpoint in self.store.endpoints()], ["apple"])

    def test_concurrent_new_endpoint_identity_is_validated_inside_transaction(self):
        desired = deepcopy(self.groups)
        desired[0]["calendars"].append({"id": "extra", "provider": "caldav", "account": "wife", "calendar_id": "extra/"})
        concurrent = deepcopy(desired)
        concurrent[0]["calendars"][-1]["calendar_id"] = "another/"
        original_transaction = self.store.transaction

        @contextmanager
        def concurrent_configuration():
            SyncStore(self.sessions).configure(concurrent)
            with original_transaction() as s:
                yield s

        with patch.object(self.store, "transaction", new=concurrent_configuration), self.assertRaises(CalendarConflict):
            self.store.configure(desired)
        with self.sessions() as s:
            endpoint = s.get(db.CalendarEndpoint, "extra")
            self.assertTrue(endpoint.enabled)
            self.assertEqual(endpoint.calendar_id, "another/")
