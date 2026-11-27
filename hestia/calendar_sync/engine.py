"""Pull, reconcile atomically, then deliver the durable outbox.

A failed read never advances the cursor, infers deletions, or enables writes to
that endpoint. A later round reads every source before retrying failed writes.
"""
from __future__ import annotations

import math
import threading

from .diagnostics import round_collector
from .events import validate_payload
from .types import PollContext, ProviderError


def error_code(exc):
    """Diagnostics are deliberately independent of exception messages/HTTP bodies."""
    if isinstance(exc, ProviderError):
        if exc.code == "google_event_type_restricted":
            return exc.code
        if exc.conflict:
            return "conflict"
        if exc.status in (401, 403):
            return "permission_denied"
        return "provider_error" if exc.status is None else "http_" + str(exc.status)
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_calendar_data"
    return "connection_or_provider_failure"


def validate_snapshot(snapshot):
    if not snapshot.complete:
        raise ValueError("Incomplete calendar snapshot")
    ids, uids = set(), set()
    for event in snapshot.events:
        if (not event.remote_id or not event.uid or event.remote_id in ids or event.uid in uids
                or not isinstance(event.updated, (int, float)) or not math.isfinite(event.updated)):
            raise ValueError("Invalid calendar snapshot identities")
        ids.add(event.remote_id)
        uids.add(event.uid)
        if not event.deleted:
            validate_payload(event.payload)


class SyncEngine:
    def __init__(self, store, providers):
        self.store = store
        self.providers = providers
        self._lock = threading.Lock()

    def run_once(self):
        with self._lock:
            collector = round_collector()
            poll_context = PollContext()
            snapshots, errors = {}, {}
            for endpoint in self.store.endpoints():
                endpoint_id = endpoint["id"]
                forced_pending = endpoint["needs_reobservation"] and endpoint["provider"] == "caldav"
                state = collector.start_pull(endpoint_id, endpoint["provider"])
                try:
                    cursor = None if forced_pending else endpoint["cursor"]
                    provider = self.providers[endpoint_id]
                    if getattr(provider, "supports_poll_context", False):
                        snapshot = provider.pull(cursor, poll_context=poll_context)
                    else:
                        snapshot = provider.pull(cursor)
                    collector.after_pull(state)
                    validate_snapshot(snapshot)
                    snapshots[endpoint_id] = snapshot
                    collector.finish_pull(state, snapshot, forced_pending=forced_pending)
                except Exception as exc:
                    collector.fail_pull(state)
                    code = error_code(exc)
                    self.store.endpoint_error(endpoint_id, code)
                    errors[endpoint_id] = code
            ingestion_errors = self.store.ingest(snapshots, isolate_errors=True, stats=collector.ingest_stats)
            collector.finish_ingest()
            for endpoint_id, exc in ingestion_errors.items():
                code = error_code(exc)
                self.store.endpoint_error(endpoint_id, code)
                errors[endpoint_id] = code
                snapshots.pop(endpoint_id)
            delivered = 0
            due_state = collector.start_due()
            due_jobs = self.store.due(list(snapshots))
            collector.finish_due(due_state, snapshots)
            for endpoint_id, event_id in due_jobs:
                out_state = collector.start_outbox()
                try:
                    provider = self.providers[endpoint_id]
                    intent = self.store.prepare(endpoint_id, event_id, provider.allocate_id)
                    if intent is None:
                        continue
                    try:
                        if intent["deleted"]:
                            provider.delete(intent["remote_id"], intent["etag"], uid=intent["uid"])
                            remote = None
                        else:
                            remote = provider.put(intent["remote_id"], intent["payload"], intent["etag"],
                                                  create=intent["create"], uid=intent["uid"])
                        self.store.acknowledge(endpoint_id, event_id, intent, remote)
                        delivered += 1
                    except Exception as exc:
                        code = error_code(exc)
                        self.store.failed(endpoint_id, event_id, code,
                                          no_mutation=isinstance(exc, ProviderError) and exc.no_mutation,
                                          prior_uncertain=intent.get("prior_uncertain", True))
                        errors[endpoint_id] = code
                finally:
                    collector.finish_outbox(endpoint_id, out_state)
            collector.emit(len(snapshots), delivered, len(errors))
            return {"read": len(snapshots), "delivered": delivered, "errors": errors}
