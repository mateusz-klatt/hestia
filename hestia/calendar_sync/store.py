"""Transactional calendar state and outbox on Hestia's existing SQLite engine.

No HTTP runs inside a DB transaction. BEGIN IMMEDIATE serializes local edits and
sync checkpoints; the durable write intent covers a process dying after HTTP.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import select, text

from ..db import CalendarEndpoint as Endpoint, CalendarEvent as Event
from ..db import CalendarGroup as Group, CalendarOutbox as Outbox, CalendarReplica as Replica
from .events import fingerprint, validate_payload


class CalendarConflict(ValueError):
    """The supplied revision is stale, or an immutable identity was reassigned."""


class SyncStore:
    def __init__(self, sessions, *, clock=time.time):
        self.sessions = sessions
        self.clock = clock

    @contextmanager
    def transaction(self):
        with self.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def configure(self, groups):
        """Retire removed endpoints without removing identities, events or tombstones."""
        with self.transaction() as s:
            for row in s.scalars(select(Group)):
                row.enabled = False
            for row in s.scalars(select(Endpoint)):
                row.enabled = False
            for group in groups:
                gid = group["id"]
                row = s.get(Group, gid)
                if row is None:
                    row = Group(id=gid, enabled=True)
                    s.add(row)
                    s.flush()
                row.enabled = True
                for item in group["calendars"]:
                    identity = (gid, item["provider"], item["account"], item["calendar_id"])
                    endpoint = s.get(Endpoint, item["id"])
                    if endpoint is None:
                        endpoint = Endpoint(id=item["id"], group_id=gid, provider=item["provider"],
                                            account=item["account"], calendar_id=item["calendar_id"],
                                            enabled=True)
                        s.add(endpoint)
                    elif (endpoint.group_id, endpoint.provider, endpoint.account, endpoint.calendar_id) != identity:
                        raise CalendarConflict("Calendar endpoint identity cannot be reassigned; use a new identifier")
                    endpoint.enabled = True
            s.flush()
            for event in s.scalars(select(Event)):
                self._enqueue(s, event)

    def groups(self):
        with self.sessions() as s:
            return [{"id": row.id} for row in s.scalars(select(Group).where(Group.enabled).order_by(Group.id))]

    @staticmethod
    def _event_dict(event):
        return {"id": event.id, "group_id": event.group_id, "uid": event.uid,
                "payload": json.loads(event.payload_json), "revision": event.revision,
                "deleted": event.deleted, "modified_at": event.modified_at}

    def list_events(self, group, include_deleted=False):
        with self.sessions() as s:
            self._require_group(s, group)
            query = select(Event).where(Event.group_id == group)
            if not include_deleted:
                query = query.where(Event.deleted.is_(False))
            return [self._event_dict(row) for row in s.scalars(query.order_by(Event.id))]

    @staticmethod
    def _require_group(s, group):
        row = s.get(Group, group)
        if row is None or not row.enabled:
            raise ValueError("Unknown or disabled calendar group")

    @staticmethod
    def _require_event(s, group, event_id, revision):
        event = s.get(Event, event_id)
        if event is None or event.group_id != group:
            raise ValueError("Unknown calendar event")
        if revision is not None and (type(revision) is not int or revision != event.revision):
            raise CalendarConflict("Calendar event changed; reload before editing")
        return event

    def save_local(self, group, payload, event_id=None, expected_revision=None):
        payload = validate_payload(payload)
        digest = fingerprint(payload)
        with self.transaction() as s:
            self._require_group(s, group)
            if event_id is None:
                identifier = uuid.uuid4().hex
                event = Event(id=identifier, group_id=group, uid=identifier + "@hestia",
                              payload_json=json.dumps(payload), fingerprint=digest, deleted=False,
                              revision=1, modified_at=self.clock(), source="local")
                s.add(event)
                s.flush()
            else:
                event = self._require_event(s, group, event_id, expected_revision)
                if event.deleted:
                    raise CalendarConflict("Deleted events cannot be restored; create a new event")
                if event.fingerprint != digest:
                    event.payload_json = json.dumps(payload)
                    event.fingerprint = digest
                    event.revision += 1
                    event.modified_at = self.clock()
                    event.source = "local"
            self._enqueue(s, event)
            return self._event_dict(event)

    def delete_local(self, group, event_id, expected_revision=None):
        with self.transaction() as s:
            self._require_group(s, group)
            event = self._require_event(s, group, event_id, expected_revision)
            self._tombstone(event, "local")
            self._enqueue(s, event)
            return self._event_dict(event)

    def _tombstone(self, event, source):
        if not event.deleted:
            event.deleted = True
            event.deleted_at = self.clock()
            event.modified_at = self.clock()
            event.revision += 1
            event.source = source

    @staticmethod
    def _enqueue(s, event):
        for endpoint in s.scalars(select(Endpoint).where(Endpoint.group_id == event.group_id, Endpoint.enabled)):
            key = (endpoint.id, event.id)
            replica = s.get(Replica, key)
            if event.deleted and replica is None:
                continue
            if replica is not None and replica.applied_revision == event.revision:
                continue
            job = s.get(Outbox, key)
            if job is None:
                s.add(Outbox(endpoint_id=endpoint.id, event_id=event.id, revision=event.revision,
                             attempts=0, next_attempt=0))
            elif job.revision != event.revision:
                job.revision = event.revision
                job.attempts = 0
                job.next_attempt = 0
                job.last_error = None

    def endpoints(self):
        with self.sessions() as s:
            return [{"id": e.id, "group_id": e.group_id, "cursor": e.cursor}
                    for e in s.scalars(select(Endpoint).where(Endpoint.enabled).order_by(Endpoint.id))]

    def endpoint_error(self, endpoint_id, code):
        with self.transaction() as s:
            s.get(Endpoint, endpoint_id).last_error = code

    def ingest(self, snapshots, *, isolate_errors=False):
        """Checkpoint only complete reads. All sources are reconciled before delivery."""
        errors = {}
        with self.transaction() as s:
            for endpoint_id, snapshot in sorted(snapshots.items()):
                try:
                    # A malformed endpoint rolls back its entire inventory, including
                    # any earlier objects and cursor, without blocking healthy sources.
                    with s.begin_nested():
                        if not snapshot.complete:
                            raise ValueError("Incomplete calendar snapshot")
                        endpoint = s.get(Endpoint, endpoint_id)
                        seen_ids = set()
                        for remote in snapshot.events:
                            seen_ids.add(remote.remote_id)
                            self._observe(s, endpoint, remote)
                        if snapshot.full:
                            for replica in s.scalars(select(Replica).where(Replica.endpoint_id == endpoint_id)):
                                if replica.seen and not replica.deleted and replica.remote_id not in seen_ids:
                                    self._tombstone(s.get(Event, replica.event_id), endpoint_id)
                                    replica.deleted = True
                                    replica.uncertain = False
                        endpoint.cursor = snapshot.cursor
                        endpoint.last_success = self.clock()
                        endpoint.last_error = None
                except Exception as exc:
                    if not isolate_errors:
                        raise
                    errors[endpoint_id] = exc
            s.flush()
            # Resolve matching copies against the FINAL winner, including later snapshots.
            for event in s.scalars(select(Event)):
                for replica in s.scalars(select(Replica).where(Replica.event_id == event.id)):
                    matches = replica.deleted if event.deleted else (
                        not replica.deleted and replica.observed_hash == event.fingerprint)
                    if matches:
                        replica.applied_revision = event.revision
                        job = s.get(Outbox, (replica.endpoint_id, event.id))
                        if job is not None:
                            s.delete(job)
                    else:
                        replica.applied_revision = 0
                self._enqueue(s, event)
        return errors

    def _observe(self, s, endpoint, remote):
        replica = s.scalar(select(Replica).where(Replica.endpoint_id == endpoint.id,
                                                Replica.remote_id == remote.remote_id))
        if replica is not None:
            event = s.get(Event, replica.event_id)
            if not remote.deleted and remote.uid != event.uid:
                raise ValueError("A mapped remote event changed its immutable UID")
        else:
            event = s.scalar(select(Event).where(Event.group_id == endpoint.group_id, Event.uid == remote.uid))
            if event is None:
                event = Event(id=uuid.uuid4().hex, group_id=endpoint.group_id, uid=remote.uid,
                              payload_json="{}", fingerprint="", deleted=False, revision=0,
                              modified_at=0, source="")
                s.add(event)
                s.flush()
            key = (endpoint.id, event.id)
            if s.get(Replica, key) is not None:
                raise ValueError("Duplicate UID within calendar; resolve duplicates before synchronization")
            replica = Replica(endpoint_id=endpoint.id, event_id=event.id, remote_id=remote.remote_id,
                              applied_revision=0, seen=False, deleted=False, uncertain=False)
            s.add(replica)
            s.flush()
        replica.seen = True
        replica.etag = remote.etag
        if remote.deleted:
            replica.deleted = True
            replica.uncertain = False
            self._tombstone(event, endpoint.id)
            return
        digest = fingerprint(remote.payload)
        echo = digest == replica.observed_hash or (replica.uncertain and digest == replica.outbound_hash)
        replica.deleted = False
        if not echo and not replica.uncertain and not event.deleted:
            # Last source modification wins. Stable source ID breaks equal-timestamp ties.
            # Sources without timestamps use observation time; a fresh version from
            # the winning source also wins within its clock's one-second precision.
            updated = remote.updated or self.clock()
            if event.revision == 0 or (updated, endpoint.id) >= (event.modified_at, event.source):
                event.payload_json = json.dumps(validate_payload(remote.payload))
                event.fingerprint = digest
                event.revision += 1
                event.modified_at = updated
                event.source = endpoint.id
        replica.observed_hash = digest
        if digest == replica.outbound_hash:
            replica.uncertain = False
            replica.outbound_hash = None
            replica.outbound_revision = None

    def due(self, healthy):
        with self.sessions() as s:
            query = select(Outbox).where(Outbox.endpoint_id.in_(healthy), Outbox.next_attempt <= self.clock())
            return [(job.endpoint_id, job.event_id) for job in s.scalars(query.order_by(Outbox.endpoint_id, Outbox.event_id))]

    def prepare(self, endpoint_id, event_id, allocate_id):
        """Reserve external ID and persist intent before sending a request."""
        with self.transaction() as s:
            key = (endpoint_id, event_id)
            job = s.get(Outbox, key)
            if job is None:
                return None
            event = s.get(Event, event_id)
            replica = s.get(Replica, key)
            if replica is None:
                replica = Replica(endpoint_id=endpoint_id, event_id=event_id,
                                  remote_id=allocate_id(event.uid), applied_revision=0,
                                  seen=False, deleted=False, uncertain=False)
                s.add(replica)
                s.flush()
            replica.outbound_hash = event.fingerprint
            replica.outbound_revision = event.revision
            # An abrupt process exit during the request is an uncertain write too.
            replica.uncertain = True
            return {"remote_id": replica.remote_id, "etag": replica.etag,
                    "create": not replica.seen, "deleted": event.deleted, "uid": event.uid,
                    "payload": json.loads(event.payload_json), "revision": event.revision}

    def acknowledge(self, endpoint_id, event_id, intent, remote):
        with self.transaction() as s:
            key = (endpoint_id, event_id)
            replica = s.get(Replica, key)
            replica.applied_revision = intent["revision"]
            replica.seen = True
            replica.deleted = intent["deleted"]
            replica.uncertain = False
            replica.outbound_hash = None
            replica.outbound_revision = None
            if remote is not None:
                if remote.remote_id != replica.remote_id:
                    raise ValueError("Provider changed a reserved event identifier")
                replica.etag = remote.etag
                replica.observed_hash = fingerprint(remote.payload)
            else:
                replica.etag = None
            event = s.get(Event, event_id)
            job = s.get(Outbox, key)
            if event.revision == intent["revision"]:
                s.delete(job)
            else:
                job.revision = event.revision
                job.attempts = 0
                job.next_attempt = 0

    def failed(self, endpoint_id, event_id, code, *, conflict=False):
        with self.transaction() as s:
            key = (endpoint_id, event_id)
            job = s.get(Outbox, key)
            job.attempts += 1
            job.next_attempt = self.clock() + min(3600, 5 * 2 ** min(job.attempts - 1, 10))
            job.last_error = code
            if conflict:
                replica = s.get(Replica, key)
                replica.uncertain = False
                replica.observed_hash = None
                replica.outbound_hash = None
            s.get(Endpoint, endpoint_id).last_error = code

    def status(self):
        with self.sessions() as s:
            endpoints = [{"id": e.id, "group_id": e.group_id, "enabled": e.enabled,
                          "last_success": e.last_success, "last_error": e.last_error}
                         for e in s.scalars(select(Endpoint).order_by(Endpoint.id))]
            pending = [{"endpoint_id": j.endpoint_id, "event_id": j.event_id,
                        "revision": j.revision, "attempts": j.attempts,
                        "next_attempt": j.next_attempt, "last_error": j.last_error}
                       for j in s.scalars(select(Outbox).order_by(Outbox.endpoint_id, Outbox.event_id))]
            return {"endpoints": endpoints, "pending": pending}
