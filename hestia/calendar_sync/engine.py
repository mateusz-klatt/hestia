"""Pull, reconcile atomically, then deliver the durable outbox.

A failed read never advances the cursor, infers deletions, or enables writes to
that endpoint. A later round reads every source before retrying failed writes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import math
import threading
import time

from .events import validate_payload
from .runtime import configure_debug_logging, log
from .types import ProviderError


def endpoint_id_hash(endpoint_id: str) -> str:
    return hashlib.sha256(endpoint_id.encode("utf-8")).hexdigest()[:12]


@dataclass
class _EndpointDiagnostics:
    provider: str
    path: str = "full"
    reason: str = "failed_read"
    events: int = 0
    fetch_wall_ms: float = 0.0
    fetch_cpu_ms: float = 0.0
    parse_wall_ms: float = 0.0
    parse_cpu_ms: float = 0.0
    reconcile_wall_ms: float = 0.0
    reconcile_cpu_ms: float = 0.0
    outbox_wall_ms: float = 0.0
    outbox_cpu_ms: float = 0.0


def _record_pull(diag: _EndpointDiagnostics, snapshot, *, forced_pending: bool,
                 pull_wall_ms: float, pull_cpu_ms: float, val_wall_ms: float, val_cpu_ms: float) -> None:
    diag.path = "full" if snapshot.full else "fast"
    diag.events = len(snapshot.events)
    if snapshot.metrics is not None:
        diag.reason = "pending_work" if forced_pending else snapshot.metrics.reason
        diag.fetch_wall_ms = snapshot.metrics.fetch_wall_ms
        diag.fetch_cpu_ms = snapshot.metrics.fetch_cpu_ms
        diag.parse_wall_ms = snapshot.metrics.parse_wall_ms + val_wall_ms
        diag.parse_cpu_ms = snapshot.metrics.parse_cpu_ms + val_cpu_ms
    else:
        diag.reason = ("pending_work" if forced_pending
                       else ("full_pull" if snapshot.full else "incremental"))
        diag.fetch_wall_ms = pull_wall_ms
        diag.fetch_cpu_ms = pull_cpu_ms
        diag.parse_wall_ms = val_wall_ms
        diag.parse_cpu_ms = val_cpu_ms


def _log_round(diagnostics: dict[str, _EndpointDiagnostics], read_count: int, delivered: int,
               error_count: int, round_wall0: float, round_cpu0: float) -> None:
    if not log.isEnabledFor(logging.DEBUG):
        return
    for endpoint_id, diag in diagnostics.items():
        log.debug(
            "calendar_sync endpoint=%s provider=%s path=%s reason=%s events=%d "
            "fetch_ms=%.1f fetch_cpu_ms=%.1f parse_ms=%.1f parse_cpu_ms=%.1f "
            "reconcile_ms=%.1f reconcile_cpu_ms=%.1f outbox_ms=%.1f outbox_cpu_ms=%.1f",
            endpoint_id_hash(endpoint_id), diag.provider, diag.path, diag.reason, diag.events,
            diag.fetch_wall_ms, diag.fetch_cpu_ms, diag.parse_wall_ms, diag.parse_cpu_ms,
            diag.reconcile_wall_ms, diag.reconcile_cpu_ms, diag.outbox_wall_ms, diag.outbox_cpu_ms,
        )
    total_wall_ms = (time.perf_counter() - round_wall0) * 1000.0
    total_cpu_ms = (time.thread_time() - round_cpu0) * 1000.0
    log.debug(
        "calendar_sync round endpoints=%d read=%d delivered=%d errors=%d total_ms=%.1f total_cpu_ms=%.1f",
        len(diagnostics), read_count, delivered, error_count, total_wall_ms, total_cpu_ms,
    )


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
            configure_debug_logging()
            round_wall0, round_cpu0 = time.perf_counter(), time.thread_time()
            snapshots, errors = {}, {}
            diagnostics: dict[str, _EndpointDiagnostics] = {}
            for endpoint in self.store.endpoints():
                endpoint_id = endpoint["id"]
                forced_pending = endpoint["needs_reobservation"] and endpoint["provider"] == "caldav"
                diag = _EndpointDiagnostics(provider=endpoint["provider"])
                diagnostics[endpoint_id] = diag
                pull_wall0, pull_cpu0 = time.perf_counter(), time.thread_time()
                try:
                    cursor = None if forced_pending else endpoint["cursor"]
                    snapshot = self.providers[endpoint_id].pull(cursor)
                    pull_wall1, pull_cpu1 = time.perf_counter(), time.thread_time()
                    validate_snapshot(snapshot)
                    val_wall1, val_cpu1 = time.perf_counter(), time.thread_time()
                    snapshots[endpoint_id] = snapshot
                    _record_pull(
                        diag, snapshot, forced_pending=forced_pending,
                        pull_wall_ms=(pull_wall1 - pull_wall0) * 1000.0,
                        pull_cpu_ms=(pull_cpu1 - pull_cpu0) * 1000.0,
                        val_wall_ms=(val_wall1 - pull_wall1) * 1000.0,
                        val_cpu_ms=(val_cpu1 - pull_cpu1) * 1000.0,
                    )
                except Exception as exc:
                    fail_wall1, fail_cpu1 = time.perf_counter(), time.thread_time()
                    diag.fetch_wall_ms = (fail_wall1 - pull_wall0) * 1000.0
                    diag.fetch_cpu_ms = (fail_cpu1 - pull_cpu0) * 1000.0
                    code = error_code(exc)
                    self.store.endpoint_error(endpoint_id, code)
                    errors[endpoint_id] = code
            ingest_stats: dict[str, list[float]] = {}
            ingestion_errors = self.store.ingest(snapshots, isolate_errors=True, stats=ingest_stats)
            for endpoint_id, (rec_wall_ms, rec_cpu_ms) in ingest_stats.items():
                diagnostics[endpoint_id].reconcile_wall_ms = rec_wall_ms
                diagnostics[endpoint_id].reconcile_cpu_ms = rec_cpu_ms
            for endpoint_id, exc in ingestion_errors.items():
                code = error_code(exc)
                self.store.endpoint_error(endpoint_id, code)
                errors[endpoint_id] = code
                snapshots.pop(endpoint_id)
            delivered = 0
            due_wall0, due_cpu0 = time.perf_counter(), time.thread_time()
            due_jobs = self.store.due(list(snapshots))
            due_wall_ms = (time.perf_counter() - due_wall0) * 1000.0
            due_cpu_ms = (time.thread_time() - due_cpu0) * 1000.0
            if snapshots:
                share = 1.0 / len(snapshots)
                for endpoint_id in snapshots:
                    diagnostics[endpoint_id].outbox_wall_ms += due_wall_ms * share
                    diagnostics[endpoint_id].outbox_cpu_ms += due_cpu_ms * share
            for endpoint_id, event_id in due_jobs:
                out_wall0, out_cpu0 = time.perf_counter(), time.thread_time()
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
                    diagnostics[endpoint_id].outbox_wall_ms += (time.perf_counter() - out_wall0) * 1000.0
                    diagnostics[endpoint_id].outbox_cpu_ms += (time.thread_time() - out_cpu0) * 1000.0
            _log_round(diagnostics, len(snapshots), delivered, len(errors), round_wall0, round_cpu0)
            return {"read": len(snapshots), "delivered": delivered, "errors": errors}
