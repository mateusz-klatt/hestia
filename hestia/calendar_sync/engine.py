"""Pull, reconcile atomically, then deliver the durable outbox.

A failed read never advances the cursor, infers deletions, or enables writes to
that endpoint. A later round reads every source before retrying failed writes.
"""
from __future__ import annotations

import math
import threading

from .events import validate_payload
from .types import ProviderError


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
            snapshots, errors = {}, {}
            for endpoint in self.store.endpoints():
                endpoint_id = endpoint["id"]
                try:
                    snapshot = self.providers[endpoint_id].pull(endpoint["cursor"])
                    validate_snapshot(snapshot)
                    snapshots[endpoint_id] = snapshot
                except Exception as exc:
                    code = error_code(exc)
                    self.store.endpoint_error(endpoint_id, code)
                    errors[endpoint_id] = code
            ingestion_errors = self.store.ingest(snapshots, isolate_errors=True)
            for endpoint_id, exc in ingestion_errors.items():
                code = error_code(exc)
                self.store.endpoint_error(endpoint_id, code)
                errors[endpoint_id] = code
                snapshots.pop(endpoint_id)
            delivered = 0
            for endpoint_id, event_id in self.store.due(list(snapshots)):
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
                                      conflict=isinstance(exc, ProviderError) and (
                                          exc.conflict or exc.code == "google_event_type_restricted"))
                    errors[endpoint_id] = code
            return {"read": len(snapshots), "delivered": delivered, "errors": errors}
