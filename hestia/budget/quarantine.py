"""Pure read-only quarantine extraction and deterministic ordering (Budget 2D).

Extracts strictly allowlisted metadata (bucket, account_id, selected, entry_reference,
date, status, reason, source, observed_at) from quarantined bank observations without
ever exposing amounts, currencies, descriptions (remittance_information) or counterparty
names. Performs no SQLite, network or filesystem I/O.
"""
from __future__ import annotations

from datetime import date
import json
import math

from .client import _reject_constant, _unique_error_fields

RULE_VERSION = 1
MAX_QUARANTINE_ITEMS = 200
BUCKET_UNCERTAIN = "uncertain"
BUCKET_UNRESOLVED = "unresolved"
BUCKET_IMPORT_UNRESOLVED = "import_unresolved"
BUCKETS = (BUCKET_UNCERTAIN, BUCKET_UNRESOLVED, BUCKET_IMPORT_UNRESOLVED)

_BUCKET_ORDER = {name: index for index, name in enumerate(BUCKETS)}
_VALID_STATUSES = frozenset({"BOOK", "PDNG", "HOLD", "CNCL", "RJCT", "SCHD", "OTHR"})
_VALID_SOURCES = frozenset({"api", "offline_import"})
_IMPORT_REASONS = frozenset({
    "non_canonical",
    "api_preserved",
    "stale_conflict",
    "reference_conflict",
    "same_time_conflict",
})
_MAX_ACCOUNT_ID = 128
_MAX_REFERENCE = 1024


class QuarantineDataError(ValueError):
    """Stored quarantine row failed integrity validation; fixed safe diagnostic only."""

    def __init__(self):
        super().__init__("Invalid quarantine data")


def _parse_payload(raw):
    if not isinstance(raw, str):
        raise QuarantineDataError()
    try:
        value = json.loads(raw, object_pairs_hook=_unique_error_fields, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise QuarantineDataError() from None
    if not isinstance(value, dict):
        raise QuarantineDataError()
    return value


def _safe_reference(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= _MAX_REFERENCE:
        raise QuarantineDataError()
    return value


def _safe_date(payload):
    raw = payload.get("booking_date") or payload.get("value_date")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise QuarantineDataError()
    try:
        if date.fromisoformat(raw).isoformat() != raw:
            raise ValueError
    except ValueError:
        raise QuarantineDataError() from None
    return raw


def _safe_status(value):
    if value is None:
        return None
    if not isinstance(value, str) or value not in _VALID_STATUSES:
        raise QuarantineDataError()
    return value


def _derive_reason(bucket, status, entry_reference, eff_date, stored_reason):
    if bucket == BUCKET_UNCERTAIN:
        return "reference_conflict"
    if bucket == BUCKET_UNRESOLVED:
        if status != "BOOK":
            return "non_booked_status"
        if entry_reference is None:
            return "missing_reference"
        if eff_date is None:
            return "missing_date"
        return "reference_conflict"
    if not isinstance(stored_reason, str) or stored_reason not in _IMPORT_REASONS:
        raise QuarantineDataError()
    return stored_reason


def extract_safe_item(*, bucket: str, account_id: str, selected: bool, payload_json: str,
                      source: str, observed_at: int | float, stored_reason: str | None = None) -> dict:
    """Return one safe, non-prose quarantine item explaining why the observation is excluded."""
    if (bucket not in _BUCKET_ORDER
            or not isinstance(account_id, str) or not 1 <= len(account_id) <= _MAX_ACCOUNT_ID
            or type(selected) is not bool
            or source not in _VALID_SOURCES
            or type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at < 0):
        raise QuarantineDataError()
    payload = _parse_payload(payload_json)
    entry_reference = _safe_reference(payload.get("entry_reference"))
    eff_date = _safe_date(payload)
    status = _safe_status(payload.get("status"))
    reason = _derive_reason(bucket, status, entry_reference, eff_date, stored_reason)
    return {
        "bucket": bucket,
        "account_id": account_id,
        "selected": selected,
        "entry_reference": entry_reference,
        "date": eff_date,
        "status": status,
        "reason": reason,
        "source": source,
        "observed_at": float(observed_at),
    }


def sort_quarantine_items(items: list[dict], *, limit: int = MAX_QUARANTINE_ITEMS) -> list[dict]:
    """Deterministically order newest dated/observed rows first and bound to ``limit``."""
    ordered = sorted(
        items,
        key=lambda item: (
            _BUCKET_ORDER[item["bucket"]],
            item["account_id"],
            item["entry_reference"] or "",
            item["reason"],
        ),
    )
    ordered.sort(
        key=lambda item: (item["date"] is not None, item["date"] or "", item["observed_at"]),
        reverse=True,
    )
    return ordered[:limit]
