"""Opt-in per-round sync diagnostics on a dedicated non-propagating logger."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
import time

LOGGER_NAME = "hestia.calendar_sync.diagnostics"
diag_log = logging.getLogger(LOGGER_NAME)
diag_log.propagate = False


def diagnostics_enabled(environ=None) -> bool:
    """Enabled ONLY when HESTIA_CALENDAR_SYNC_DEBUG=1; ignores parent logger levels."""
    env = os.environ if environ is None else environ
    return env.get("HESTIA_CALENDAR_SYNC_DEBUG") == "1"


def configure_diagnostics(environ=None) -> bool:
    """Configure the dedicated diagnostics logger without touching parent or sibling loggers."""
    if not diagnostics_enabled(environ):
        diag_log.setLevel(logging.CRITICAL + 1)
        return False
    if not diag_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        diag_log.addHandler(handler)
    diag_log.setLevel(logging.DEBUG)
    return True


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


class _NoopCollector:
    ingest_stats = None

    def start_pull(self, endpoint_id: str, provider: str):
        return None

    def after_pull(self, state):
        return None

    def finish_pull(self, state, snapshot, *, forced_pending: bool) -> None:
        return None

    def fail_pull(self, state) -> None:
        return None

    def finish_ingest(self) -> None:
        return None

    def start_due(self):
        return None

    def finish_due(self, state, healthy) -> None:
        return None

    def start_outbox(self):
        return None

    def finish_outbox(self, endpoint_id: str, state) -> None:
        return None

    def emit(self, read_count: int, delivered: int, error_count: int) -> None:
        return None


_NOOP_COLLECTOR = _NoopCollector()


class _RoundCollector:
    def __init__(self):
        self._round_wall0 = time.perf_counter()
        self._round_cpu0 = time.thread_time()
        self._endpoints: dict[str, _EndpointDiagnostics] = {}
        self.ingest_stats: dict[str, list[float]] = {}

    def start_pull(self, endpoint_id: str, provider: str):
        diag = _EndpointDiagnostics(provider=provider)
        self._endpoints[endpoint_id] = diag
        return [diag, time.perf_counter(), time.thread_time(), 0.0, 0.0]

    def after_pull(self, state) -> None:
        state[3] = time.perf_counter()
        state[4] = time.thread_time()

    def finish_pull(self, state, snapshot, *, forced_pending: bool) -> None:
        diag, pull_wall0, pull_cpu0, pull_wall1, pull_cpu1 = state
        val_wall1, val_cpu1 = time.perf_counter(), time.thread_time()
        diag.path = "full" if snapshot.full else "fast"
        diag.events = len(snapshot.events)
        val_wall_ms = (val_wall1 - pull_wall1) * 1000.0
        val_cpu_ms = (val_cpu1 - pull_cpu1) * 1000.0
        if snapshot.metrics is not None:
            raw_reason = snapshot.metrics.reason
            diag.fetch_wall_ms = snapshot.metrics.fetch_wall_ms
            diag.fetch_cpu_ms = snapshot.metrics.fetch_cpu_ms
            diag.parse_wall_ms = snapshot.metrics.parse_wall_ms + val_wall_ms
            diag.parse_cpu_ms = snapshot.metrics.parse_cpu_ms + val_cpu_ms
        else:
            raw_reason = "full_pull" if snapshot.full else "incremental"
            diag.fetch_wall_ms = (pull_wall1 - pull_wall0) * 1000.0
            diag.fetch_cpu_ms = (pull_cpu1 - pull_cpu0) * 1000.0
            diag.parse_wall_ms = val_wall_ms
            diag.parse_cpu_ms = val_cpu_ms
        diag.reason = "pending_work" if forced_pending else raw_reason

    def fail_pull(self, state) -> None:
        diag, pull_wall0, pull_cpu0, _, _ = state
        diag.fetch_wall_ms = (time.perf_counter() - pull_wall0) * 1000.0
        diag.fetch_cpu_ms = (time.thread_time() - pull_cpu0) * 1000.0

    def finish_ingest(self) -> None:
        for endpoint_id, (rec_wall_ms, rec_cpu_ms) in self.ingest_stats.items():
            diag = self._endpoints[endpoint_id]
            diag.reconcile_wall_ms = rec_wall_ms
            diag.reconcile_cpu_ms = rec_cpu_ms

    def start_due(self):
        return time.perf_counter(), time.thread_time()

    def finish_due(self, state, healthy) -> None:
        due_wall0, due_cpu0 = state
        due_wall_ms = (time.perf_counter() - due_wall0) * 1000.0
        due_cpu_ms = (time.thread_time() - due_cpu0) * 1000.0
        if healthy:
            share = 1.0 / len(healthy)
            for endpoint_id in healthy:
                diag = self._endpoints[endpoint_id]
                diag.outbox_wall_ms += due_wall_ms * share
                diag.outbox_cpu_ms += due_cpu_ms * share

    def start_outbox(self):
        return time.perf_counter(), time.thread_time()

    def finish_outbox(self, endpoint_id: str, state) -> None:
        out_wall0, out_cpu0 = state
        diag = self._endpoints[endpoint_id]
        diag.outbox_wall_ms += (time.perf_counter() - out_wall0) * 1000.0
        diag.outbox_cpu_ms += (time.thread_time() - out_cpu0) * 1000.0

    def emit(self, read_count: int, delivered: int, error_count: int) -> None:
        for endpoint_id, diag in self._endpoints.items():
            diag_log.debug(
                "calendar_sync endpoint=%s provider=%s path=%s reason=%s events=%d "
                "fetch_ms=%.1f fetch_cpu_ms=%.1f parse_ms=%.1f parse_cpu_ms=%.1f "
                "reconcile_ms=%.1f reconcile_cpu_ms=%.1f outbox_ms=%.1f outbox_cpu_ms=%.1f",
                endpoint_id_hash(endpoint_id), diag.provider, diag.path, diag.reason, diag.events,
                diag.fetch_wall_ms, diag.fetch_cpu_ms, diag.parse_wall_ms, diag.parse_cpu_ms,
                diag.reconcile_wall_ms, diag.reconcile_cpu_ms, diag.outbox_wall_ms, diag.outbox_cpu_ms,
            )
        total_wall_ms = (time.perf_counter() - self._round_wall0) * 1000.0
        total_cpu_ms = (time.thread_time() - self._round_cpu0) * 1000.0
        diag_log.debug(
            "calendar_sync round endpoints=%d read=%d delivered=%d errors=%d total_ms=%.1f total_cpu_ms=%.1f",
            len(self._endpoints), read_count, delivered, error_count, total_wall_ms, total_cpu_ms,
        )


def round_collector(environ=None):
    return _RoundCollector() if configure_diagnostics(environ) else _NOOP_COLLECTOR
