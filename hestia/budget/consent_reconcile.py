"""Pure bank-consent reconciliation and retention classification (Budget 2D).

Classifies stored consent and consent-flow metadata into owner-review, remote-close
candidate and local-retention buckets without performing any network request,
remote session closure, credential modification or file/row deletion.

Sessions not created by Hestia (``external_file``, ``poc_retired``) are always
classified ``external_read_only`` with both ``remote_close_eligible=False`` and
``local_retire_eligible=False`` so retired PoC and external credentials are never
touched or overwritten. Uncertain exchanges (``exchange_uncertain``) always remain
flagged for owner review and are never auto-pruned.

All timestamps are UTC epoch seconds in ``[0, MAX_TIMESTAMP]``. Errors carry fixed
messages and never echo consent IDs, flow IDs, connection IDs or owners.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import re

RULE_VERSION = 1
SECONDS_PER_DAY = 86_400
MAX_TIMESTAMP = 253_402_300_799  # 9999-12-31T23:59:59Z (UTC epoch seconds)
DEFAULT_RETENTION_DAYS = 90
MAX_RETENTION_DAYS = 3_650
MAX_CONSENTS = 1_000
MAX_FLOWS = 1_000
MAX_TEXT = 1024
MAX_ID = 128

HESTIA_PROVENANCE = "hestia_created"
KNOWN_PROVENANCES = frozenset({HESTIA_PROVENANCE, "external_file", "poc_retired"})
CONSENT_STATUSES = frozenset({"ACTIVE", "UNADOPTED", "RETIRED"})
ACTIVE_FLOW_STATUSES = frozenset({"created", "started", "staged", "exchanging", "candidate"})
TERMINAL_FLOW_STATUSES = frozenset({"adopted", "failed", "canceled", "expired", "exchange_uncertain"})
FLOW_STATUSES = ACTIVE_FLOW_STATUSES | TERMINAL_FLOW_STATUSES

CONSENT_CLASSIFICATIONS = (
    "external_read_only",
    "active_pointer_mismatch",
    "uncertain_flow_evidence",
    "active_current",
    "active_expired",
    "unadopted_in_progress",
    "unadopted_orphan",
    "unadopted_expired_retained",
    "unadopted_expired_prunable",
    "retired_still_valid",
    "retired_retained",
    "retired_prunable",
)
FLOW_CLASSIFICATIONS = (
    "uncertain_exchange_review",
    "stale_active_flow",
    "flow_in_progress",
    "terminal_retained",
    "terminal_prunable",
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ConsentReconcileInputError(ValueError):
    """Malformed or out-of-scope input; the message never contains input values."""

    def __init__(self):
        super().__init__("Invalid consent reconciliation input")


class ConsentReconcileLimitError(ValueError):
    """Too many consent or flow records to examine; no partial result is returned."""

    def __init__(self):
        super().__init__("Consent reconciliation limit exceeded")


@dataclass(frozen=True, repr=False)
class ConsentRecord:
    owner: str
    consent_id: str
    connection_id: str
    provenance: str
    status: str
    created_at: int | float
    valid_until: int | float | None = None
    generation: int = 1


@dataclass(frozen=True, repr=False)
class ConsentFlowRecord:
    owner: str
    flow_id: str
    connection_id: str
    status: str
    created_at: int | float
    expires_at: int | float
    candidate_consent_id: str | None = None


@dataclass(frozen=True, repr=False)
class ConsentFinding:
    consent_id: str
    connection_id: str
    provenance: str
    status: str
    classification: str
    needs_owner_review: bool
    remote_close_eligible: bool
    local_retire_eligible: bool


@dataclass(frozen=True, repr=False)
class FlowFinding:
    flow_id: str
    connection_id: str
    status: str
    classification: str
    needs_owner_review: bool
    local_retire_eligible: bool
    candidate_consent_id: str | None


@dataclass(frozen=True, repr=False)
class ReconciliationReport:
    consents: tuple[ConsentFinding, ...]
    flows: tuple[FlowFinding, ...]
    rule_version: int = RULE_VERSION


def _text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or len(value) > maximum or not value.strip() or _CONTROL.search(value):
        raise ConsentReconcileInputError()
    return value


def _timestamp(value):
    if type(value) is int:
        if not 0 <= value <= MAX_TIMESTAMP:
            raise ConsentReconcileInputError()
        return value
    if type(value) is float:
        if not math.isfinite(value) or not 0.0 <= value <= MAX_TIMESTAMP:
            raise ConsentReconcileInputError()
        return value
    raise ConsentReconcileInputError()


def _iterable(values):
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        raise ConsentReconcileInputError()
    return values


def _past_retention(now, anchor, retention_seconds):
    return math.floor(now - anchor) >= retention_seconds


def _validate_consent(owner, item, seen):
    if (not isinstance(item, ConsentRecord) or item.owner != owner
            or not isinstance(item.provenance, str) or item.provenance not in KNOWN_PROVENANCES
            or not isinstance(item.status, str) or item.status not in CONSENT_STATUSES
            or type(item.generation) is not int or item.generation < 1):
        raise ConsentReconcileInputError()
    consent_id = _text(item.consent_id, MAX_ID)
    if consent_id in seen:
        raise ConsentReconcileInputError()
    seen.add(consent_id)
    connection_id = _text(item.connection_id, MAX_ID)
    created_at = _timestamp(item.created_at)
    valid_until = None if item.valid_until is None else _timestamp(item.valid_until)
    return consent_id, connection_id, item.provenance, item.status, created_at, valid_until


def _validate_flow(owner, item, seen):
    if (not isinstance(item, ConsentFlowRecord) or item.owner != owner
            or not isinstance(item.status, str) or item.status not in FLOW_STATUSES):
        raise ConsentReconcileInputError()
    flow_id = _text(item.flow_id, MAX_ID)
    if flow_id in seen:
        raise ConsentReconcileInputError()
    seen.add(flow_id)
    connection_id = _text(item.connection_id, MAX_ID)
    created_at = _timestamp(item.created_at)
    expires_at = _timestamp(item.expires_at)
    if expires_at < created_at:
        raise ConsentReconcileInputError()
    candidate = None if item.candidate_consent_id is None else _text(item.candidate_consent_id, MAX_ID)
    return flow_id, connection_id, item.status, created_at, expires_at, candidate


def _classify_unadopted(consent_id, connection_id, valid_until, now, retention_seconds,
                        active_candidates, unresolved_flow_consents):
    if (connection_id, consent_id) in active_candidates:
        return "unadopted_in_progress", False, False, False
    if valid_until is None or valid_until > now:
        return "unadopted_orphan", True, True, False
    if consent_id in unresolved_flow_consents or not _past_retention(now, valid_until, retention_seconds):
        return "unadopted_expired_retained", True, False, False
    return "unadopted_expired_prunable", False, False, True


def _classify_retired(consent_id, valid_until, now, retention_seconds, unresolved_flow_consents):
    """Classify a RETIRED consent.

    Store invariant (``consent_store.py``): ``RETIRED`` is written exclusively when a
    verified successor candidate with identical connection identity is adopted as the
    connection's ``active_consent_id``. A still-valid ``RETIRED`` consent is therefore
    a replaced session eligible for owner-approved remote closure, unless protected by
    ``active_consent_ids`` defense-in-depth or an unresolved flow reference.
    """
    if valid_until is None or valid_until > now:
        return "retired_still_valid", True, True, False
    if consent_id in unresolved_flow_consents:
        return "retired_retained", True, False, False
    if _past_retention(now, valid_until, retention_seconds):
        return "retired_prunable", False, False, True
    return "retired_retained", False, False, False


def _classify_consent(record, now, retention_seconds, active_candidates,
                      uncertain_flow_consents, unresolved_flow_consents, active_pointers):
    consent_id, connection_id, provenance, status, _, valid_until = record
    if provenance != HESTIA_PROVENANCE:
        classification, review, remote, local = "external_read_only", False, False, False
    elif status != "ACTIVE" and (connection_id, consent_id) in active_pointers:
        classification, review, remote, local = "active_pointer_mismatch", True, False, False
    elif consent_id in uncertain_flow_consents:
        classification, review, remote, local = "uncertain_flow_evidence", True, False, False
    elif status == "ACTIVE":
        expired = valid_until is not None and valid_until <= now
        classification, review, remote, local = ("active_expired" if expired else "active_current"), expired, False, False
    elif status == "UNADOPTED":
        classification, review, remote, local = _classify_unadopted(
            consent_id, connection_id, valid_until, now, retention_seconds,
            active_candidates, unresolved_flow_consents)
    else:
        classification, review, remote, local = _classify_retired(
            consent_id, valid_until, now, retention_seconds, unresolved_flow_consents)
    return ConsentFinding(
        consent_id=consent_id,
        connection_id=connection_id,
        provenance=provenance,
        status=status,
        classification=classification,
        needs_owner_review=review,
        remote_close_eligible=remote,
        local_retire_eligible=local,
    )


def _classify_flow(record, now, retention_seconds):
    flow_id, connection_id, status, created_at, expires_at, candidate = record
    if status == "exchange_uncertain":
        classification, review, local = "uncertain_exchange_review", True, False
    elif status in ACTIVE_FLOW_STATUSES:
        stale = expires_at <= now
        classification, review, local = ("stale_active_flow" if stale else "flow_in_progress"), stale, False
    else:
        anchor = expires_at if expires_at >= created_at else created_at
        prunable = _past_retention(now, anchor, retention_seconds)
        classification, review, local = ("terminal_prunable" if prunable else "terminal_retained"), False, prunable
    return FlowFinding(
        flow_id=flow_id,
        connection_id=connection_id,
        status=status,
        classification=classification,
        needs_owner_review=review,
        local_retire_eligible=local,
        candidate_consent_id=candidate,
    )


def _scope(owner, now, retention_days, max_consents, max_flows):
    owner = _text(owner)
    now_ts = _timestamp(now)
    if (type(retention_days) is not int or not 0 <= retention_days <= MAX_RETENTION_DAYS
            or type(max_consents) is not int or max_consents < 0
            or type(max_flows) is not int or max_flows < 0):
        raise ConsentReconcileInputError()
    return owner, now_ts, retention_days * SECONDS_PER_DAY


def _active_pointers(active_consent_ids):
    if active_consent_ids is None:
        return frozenset()
    if not isinstance(active_consent_ids, Mapping):
        raise ConsentReconcileInputError()
    return frozenset((_text(connection_id, MAX_ID), _text(consent_id, MAX_ID))
                     for connection_id, consent_id in active_consent_ids.items())


def _flow_reference_sets(parsed_flows, now_ts):
    active_candidates, uncertain_consents, unresolved_consents = set(), set(), set()
    for _, connection_id, status, _, expires_at, candidate in parsed_flows:
        if candidate is None:
            continue
        if status == "exchange_uncertain":
            uncertain_consents.add(candidate)
        elif status == "candidate" and expires_at > now_ts:
            active_candidates.add((connection_id, candidate))
        elif status in ACTIVE_FLOW_STATUSES and expires_at <= now_ts:
            unresolved_consents.add(candidate)
    return frozenset(active_candidates), frozenset(uncertain_consents), frozenset(unresolved_consents)


def classify_consents(owner: str, now: int | float, consents: Iterable[ConsentRecord],
                      flows: Iterable[ConsentFlowRecord], *,
                      retention_days: int = DEFAULT_RETENTION_DAYS,
                      max_consents: int = MAX_CONSENTS,
                      max_flows: int = MAX_FLOWS,
                      active_consent_ids: Mapping[str, str] | None = None) -> ReconciliationReport:
    """Classify consent and flow records for owner review, remote closure and local retention.

    Read-only pure classification: never closes remote sessions, never deletes or
    overwrites local files/rows, and always preserves external/PoC sessions untouched.
    Passing ``active_consent_ids`` (``{connection_id: active_consent_id}``) enables
    defense-in-depth protection so the live pointer is never marked closable or prunable
    even if its consent status drifted from ``ACTIVE``.
    """
    owner, now_ts, retention_seconds = _scope(owner, now, retention_days, max_consents, max_flows)
    active_pointers = _active_pointers(active_consent_ids)
    seen_consents, parsed_consents = set(), []
    for count, item in enumerate(_iterable(consents), 1):
        if count > max_consents:
            raise ConsentReconcileLimitError()
        parsed_consents.append(_validate_consent(owner, item, seen_consents))

    seen_flows, parsed_flows = set(), []
    for count, item in enumerate(_iterable(flows), 1):
        if count > max_flows:
            raise ConsentReconcileLimitError()
        parsed_flows.append(_validate_flow(owner, item, seen_flows))

    active_candidates, uncertain_flow_consents, unresolved_flow_consents = _flow_reference_sets(parsed_flows, now_ts)
    consent_findings = [
        _classify_consent(record, now_ts, retention_seconds, active_candidates,
                          uncertain_flow_consents, unresolved_flow_consents, active_pointers)
        for record in parsed_consents
    ]
    flow_findings = [_classify_flow(record, now_ts, retention_seconds) for record in parsed_flows]

    consent_findings.sort(key=lambda item: (
        not item.needs_owner_review, not item.remote_close_eligible,
        not item.local_retire_eligible, item.connection_id, item.consent_id,
    ))
    flow_findings.sort(key=lambda item: (
        not item.needs_owner_review, not item.local_retire_eligible,
        item.connection_id, item.flow_id,
    ))
    return ReconciliationReport(consents=tuple(consent_findings), flows=tuple(flow_findings))
