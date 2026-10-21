"""Scoped reconciliation and bounded SQL for large, mostly unchanged calendars."""
from __future__ import annotations

from contextlib import contextmanager
import json
import unittest
from unittest.mock import patch

from sqlalchemy import event as sqlalchemy_event, select, text

from hestia import db
from hestia.calendar_sync.events import fingerprint
from hestia.calendar_sync.types import RemoteEvent, Snapshot
from tests import test_calendar_sync as fixtures


class CalendarStoreIngestTests(unittest.TestCase):
    setUp = fixtures.SyncTests.setUp
    tearDown = fixtures.SyncTests.tearDown

    @contextmanager
    def statements(self):
        statements = []
        engine = self.sessions.kw["bind"]

        def record(connection, cursor, statement, parameters, context, executemany):
            statements.append((" ".join(statement.upper().split()), parameters))

        sqlalchemy_event.listen(engine, "before_cursor_execute", record)
        try:
            yield statements
        finally:
            sqlalchemy_event.remove(engine, "before_cursor_execute", record)

    def seed(self, count, prefix, *, group="family", deleted=False, replicas=True):
        content = fixtures.payload(prefix)
        digest = fingerprint(content)
        records = [RemoteEvent(f"{prefix}-remote-{index}", f"{prefix}-{index}@synthetic.test", content,
                               '"initial"', self.now, deleted) for index in range(count)]
        with self.store.transaction() as session:
            session.add_all([db.CalendarEvent(id=f"{prefix}-{index}", group_id=group, uid=remote.uid,
                payload_json=json.dumps(content), fingerprint=digest, deleted=deleted,
                deleted_at=self.now if deleted else None, revision=1, modified_at=self.now, source="apple")
                for index, remote in enumerate(records)])
            session.flush()
            if replicas:
                session.add_all([db.CalendarReplica(endpoint_id=endpoint, event_id=f"{prefix}-{index}",
                    remote_id=remote.remote_id, etag=remote.etag, observed_hash=digest,
                    applied_revision=1, seen=True, deleted=deleted, uncertain=False)
                    for index, remote in enumerate(records) for endpoint in ("apple", "google")])
        return records

    def add_history(self, count):
        self.store.configure(self.groups + [{"id": "private", "calendars": []}])
        self.seed(count, "old-deleted", deleted=True)
        self.seed(count, "private", group="private", replicas=False)

    def test_empty_incremental_reads_no_event_replica_or_outbox_and_keeps_uncertain_job(self):
        self.seed(50, "live")
        self.add_history(50)
        created = self.store.save_local("family", fixtures.payload("Pending"))
        self.store.prepare("apple", created["id"], self.providers["apple"].allocate_id)
        self.store.failed("apple", created["id"], "timeout")
        before = self.store.status()["pending"]
        with self.statements() as statements:
            self.assertEqual(self.store.ingest({"apple": Snapshot([], "next", True, False)}), {})
        selects = [sql for sql, _ in statements if sql.startswith("SELECT ")]
        self.assertEqual(len(selects), 1)
        self.assertIn("FROM CALENDAR_ENDPOINTS", selects[0])
        self.assertFalse(any("CALENDAR_EVENTS" in sql or "CALENDAR_REPLICAS" in sql or "CALENDAR_OUTBOX" in sql
                             for sql, _ in statements))
        self.assertEqual(self.store.status()["pending"], before)
        with self.sessions() as session:
            self.assertTrue(session.get(db.CalendarReplica, ("apple", created["id"])).uncertain)
            self.assertEqual(session.get(db.CalendarEndpoint, "apple").cursor, "next")

    def test_large_unchanged_full_snapshot_has_chunked_sql_budget_without_history_reads(self):
        records = self.seed(801, "live")
        self.add_history(601)
        with self.statements() as statements:
            self.assertEqual(self.store.ingest({"apple": Snapshot(records, "full", True, True)}), {})
        selects = [(sql, parameters) for sql, parameters in statements if sql.startswith("SELECT ")]
        # Four batched event-ID reads per 400 rows, plus endpoint metadata and
        # the selected endpoint's live mapping inventory; never one query/event.
        self.assertLessEqual(len(selects), 15)
        self.assertTrue(all(len(parameters) <= 400 for _, parameters in selects))
        self.assertTrue(all(" WHERE " in sql for sql, _ in selects))
        self.assertFalse(any("old-deleted-" in str(parameters) or "private-" in str(parameters)
                             for _, parameters in selects))
        self.assertFalse(any(sql.startswith(("INSERT ", "DELETE ")) for sql, _ in statements))
        self.assertEqual(self.store.status()["pending"], [])
        self.assertEqual(len(self.store.list_events("family")), 801)

    def test_single_incremental_change_reconciles_only_one_event_and_other_copy(self):
        records = self.seed(401, "live")
        self.add_history(401)
        changed = RemoteEvent(records[0].remote_id, records[0].uid, fixtures.payload("Changed"),
                              '"new"', self.now + 10)
        with self.statements() as statements:
            self.assertEqual(self.store.ingest({"apple": Snapshot([changed], "incremental", True, False)}), {})
        selects = [(sql, parameters) for sql, parameters in statements if sql.startswith("SELECT ")]
        self.assertLessEqual(len(selects), 7)
        self.assertTrue(all(" WHERE " in sql for sql, _ in selects))
        self.assertFalse(any("old-deleted-" in str(parameters) or "private-" in str(parameters)
                             for _, parameters in selects))
        with self.sessions() as session:
            jobs = list(session.scalars(select(db.CalendarOutbox)))
            self.assertEqual([(job.endpoint_id, job.event_id, job.revision) for job in jobs], [("google", "live-0", 2)])
            self.assertEqual(session.get(db.CalendarReplica, ("apple", "live-0")).applied_revision, 2)
            self.assertEqual(session.get(db.CalendarReplica, ("google", "live-0")).applied_revision, 0)
            self.assertEqual(session.get(db.CalendarEvent, "live-1").revision, 1)

    def test_full_missing_mapping_tombstones_only_that_group_and_queues_delete(self):
        records = self.seed(2, "live")
        self.add_history(20)
        self.store.ingest({"apple": Snapshot(records[:1], "full", True, True)})
        with self.sessions() as session:
            self.assertFalse(session.get(db.CalendarEvent, "live-0").deleted)
            missing = session.get(db.CalendarEvent, "live-1")
            self.assertTrue(missing.deleted)
            self.assertEqual((missing.source, missing.revision), ("apple", 2))
            self.assertFalse(session.get(db.CalendarEvent, "private-0").deleted)
            self.assertIsNone(session.get(db.CalendarOutbox, ("apple", missing.id)))
            self.assertEqual(session.get(db.CalendarOutbox, ("google", missing.id)).revision, missing.revision)

    def test_failed_savepoint_does_not_leak_created_or_modified_ids_into_reconciliation(self):
        records = self.seed(2, "live")
        discarded = []
        observe = self.store._observe

        def capture_observation(session, endpoint, remote, inventory):
            identifier = observe(session, endpoint, remote, inventory)
            if remote.remote_id == "temporary":
                discarded.append(identifier)
            return identifier

        malformed = Snapshot([
            RemoteEvent("temporary", "temporary@synthetic.test", fixtures.payload("Discard"), "tmp", self.now + 10),
            RemoteEvent(records[0].remote_id, "wrong-uid", fixtures.payload("Invalid"), "bad", self.now + 10),
        ], "must-not-commit", True, False)
        healthy = Snapshot([RemoteEvent(records[1].remote_id, records[1].uid, fixtures.payload("Healthy"),
                                        "healthy", self.now + 10)], "committed", True, False)
        with self.statements() as statements, patch.object(self.store, "_observe", new=capture_observation):
            errors = self.store.ingest({"apple": malformed, "google": healthy}, isolate_errors=True)
        self.assertEqual(set(errors), {"apple"})
        self.assertEqual(len(discarded), 1)
        final_queries = [(sql, parameters) for sql, parameters in statements
                         if sql.startswith("SELECT ") and "CALENDAR_EVENTS.ID IN" in sql]
        self.assertTrue(final_queries)
        self.assertFalse(any(discarded[0] in parameters for _, parameters in final_queries))
        with self.sessions() as session:
            self.assertIsNone(session.get(db.CalendarEvent, discarded[0]))
            self.assertEqual(session.get(db.CalendarEvent, "live-0").revision, 1)
            self.assertEqual(json.loads(session.get(db.CalendarEvent, "live-1").payload_json), fixtures.payload("Healthy"))
            self.assertIsNone(session.get(db.CalendarEndpoint, "apple").cursor)
            self.assertEqual(session.get(db.CalendarEndpoint, "google").cursor, "committed")

    def test_reconciliation_lookups_use_event_id_indexes(self):
        with self.sessions() as session:
            for table in ("calendar_replicas", "calendar_outbox"):
                plan = session.execute(text(f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE event_id IN (:first, :second)"),
                                       {"first": "synthetic-first", "second": "synthetic-second"})
                self.assertTrue(any("USING INDEX ix_" + table + "_event_id" in row[3] for row in plan))
