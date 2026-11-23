"""Pure owner-private live inbox projection helpers (Budget 2D, Variant D).

Computes stable local opaque subject IDs, generation/expiry-aware milestones,
strictly allowlisted alert params, deterministic ordering without ``created_at`` and anti-ABA CAS
read-marker digests. Performs no SQLite, network or filesystem I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import math
import re

from .analytics_core import AnalyticsError, projection_digest

RULE_VERSION = 1
MAX_INBOX_READS = 500
MAX_MARK_BATCH = 100
KIND_CONSENT = "consent_renewal"
KIND_MORTGAGE = "mortgage_due"
KIND_SPENDING = "spending_limit"
KIND_DEVICE = "device_added"
INBOX_KINDS = (KIND_DEVICE, KIND_CONSENT, KIND_MORTGAGE, KIND_SPENDING)

_KIND_ORDER = {kind: index for index, kind in enumerate(INBOX_KINDS)}
_SEVERITY_ORDER = {
    KIND_DEVICE: {"added": 0},
    KIND_CONSENT: {"attention_required": 0, "expired": 1, "due_soon": 2},
    KIND_MORTGAGE: {"overdue": 0, "due_today": 1, "due_soon": 2},
    KIND_SPENDING: {"exceeded": 0, "at_limit": 1, "approaching": 2},
}
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MILESTONE = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
_PERIOD = re.compile(r"[0-9]{4}-[0-9]{2}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_ATTENTION = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_LOCAL_ID = 128
_SUBJECT_DOMAIN = "hestia/budget/inbox/subject/v1"
_KEY_TOKEN_DOMAIN = "hestia/budget/inbox/key-token/v1"
_READS_DIGEST_VERSION = "owner-inbox-reads-v1"
_KEY_FIELDS = frozenset({"kind", "subject_id", "milestone"})


class OwnerInboxDataError(ValueError):
    """Stored or evaluated inbox integrity failure; fixed safe diagnostic only."""

    def __init__(self):
        super().__init__("Invalid owner inbox data")


def _non_negative_float(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise OwnerInboxDataError()
    return float(value)


def _check_milestone(value):
    if not isinstance(value, str) or not _MILESTONE.fullmatch(value):
        raise OwnerInboxDataError()
    return value


def _local_id(value):
    if not isinstance(value, str) or len(value) > _MAX_LOCAL_ID or not value.strip() or _CONTROL.search(value):
        raise OwnerInboxDataError()
    return value


def consent_ref(*, connection_id: str) -> dict:
    """Owner-known local connection ID for UI lookup (computed live, never stored or logged)."""
    return {"connection_id": _local_id(connection_id)}


def mortgage_ref(*, schedule_id: str) -> dict:
    """Owner-known local mortgage schedule ID for UI lookup (computed live, never stored or logged)."""
    return {"schedule_id": _local_id(schedule_id)}


def spending_limit_ref(*, category_id: str, currency: str) -> dict:
    """Owner-known local category ID and currency for UI lookup (computed live, never stored or logged)."""
    clean_id = _local_id(category_id)
    if clean_id == "uncategorised" or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise OwnerInboxDataError()
    return {"category_id": clean_id, "currency": currency}


def opaque_subject_id(owner: str, kind: str, local_id: str) -> str:
    """Stable local SHA-256 subject identifier; hides raw DB and provider IDs."""
    if not isinstance(owner, str) or not owner or kind not in _KIND_ORDER or not isinstance(local_id, str) or not local_id:
        raise OwnerInboxDataError()
    payload = f"{_SUBJECT_DOMAIN}\0{kind}\0{owner}\0{local_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def opaque_key_token(kind: str, subject_id: str, milestone: str) -> str:
    """Opaque SHA-256 audit token for one inbox read-marker key (never echoes milestone prose)."""
    if kind not in _KIND_ORDER or not isinstance(subject_id, str) or not _HEX64.fullmatch(subject_id):
        raise OwnerInboxDataError()
    _check_milestone(milestone)
    payload = f"{_KEY_TOKEN_DOMAIN}\0{kind}\0{subject_id}\0{milestone}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def consent_milestone(generation: int, valid_until: int | float | None, status: str,
                      active_milestone: int | str | None, attention: str | None) -> str:
    """Build a consent milestone incorporating both ``consent_generation`` and ``valid_until``."""
    if type(generation) is not int or generation < 0:
        raise OwnerInboxDataError()
    expiry_tag = "none" if valid_until is None else str(int(_non_negative_float(valid_until)))
    if status == "attention_required":
        if not isinstance(attention, str) or not _ATTENTION.fullmatch(attention):
            raise OwnerInboxDataError()
        value = f"g{generation}:exp{expiry_tag}:attention:{attention}"
    elif status == "expired":
        value = f"g{generation}:exp{expiry_tag}:expired"
    elif status == "due_soon":
        if type(active_milestone) is not int or not 1 <= active_milestone <= 365:
            raise OwnerInboxDataError()
        value = f"g{generation}:exp{expiry_tag}:lead:{active_milestone}"
    else:
        raise OwnerInboxDataError()
    return _check_milestone(value)


def mortgage_milestone(due_date: str, status: str) -> str:
    """One durable dedupe milestone per installment month and active mortgage status."""
    if not isinstance(due_date, str) or not _DATE.fullmatch(due_date) or status not in _SEVERITY_ORDER[KIND_MORTGAGE]:
        raise OwnerInboxDataError()
    return f"{due_date[:7]}:{status}"


def spending_limit_milestone(period: str, status: str) -> str:
    """One durable dedupe milestone per UTC month and active spending-limit status."""
    if not isinstance(period, str) or not _PERIOD.fullmatch(period) or status not in _SEVERITY_ORDER[KIND_SPENDING]:
        raise OwnerInboxDataError()
    return f"{period}:{status}"


def consent_params(*, status: str, valid_until: int | float | None, days_remaining: int | None,
                   attention: str | None, paused: bool) -> dict:
    """Strictly allowlisted live params for a ``consent_renewal`` alert (zero bank/provider IDs)."""
    if (status not in _SEVERITY_ORDER[KIND_CONSENT]
            or (days_remaining is not None and type(days_remaining) is not int)
            or (attention is not None and (not isinstance(attention, str) or not _ATTENTION.fullmatch(attention)))
            or type(paused) is not bool):
        raise OwnerInboxDataError()
    return {
        "status": status,
        "valid_until": None if valid_until is None else _non_negative_float(valid_until),
        "days_remaining": days_remaining,
        "attention": attention,
        "paused": paused,
    }


def mortgage_params(*, status: str, due_date: str, days_until_due: int,
                    currency: str, freshness_stale: bool) -> dict:
    """Strictly allowlisted live params for a ``mortgage_due`` alert (zero labels or amounts)."""
    if (status not in _SEVERITY_ORDER[KIND_MORTGAGE]
            or not isinstance(due_date, str) or not _DATE.fullmatch(due_date)
            or type(days_until_due) is not int
            or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
            or type(freshness_stale) is not bool):
        raise OwnerInboxDataError()
    return {
        "status": status,
        "due_date": due_date,
        "days_until_due": days_until_due,
        "currency": currency,
        "freshness_stale": freshness_stale,
    }


def spending_limit_params(*, status: str, period: str, currency: str,
                          warning_percent: int, percent_used_floor: int) -> dict:
    """Strictly allowlisted live params for a ``spending_limit`` alert (zero category names or amounts)."""
    if (status not in _SEVERITY_ORDER[KIND_SPENDING]
            or not isinstance(period, str) or not _PERIOD.fullmatch(period)
            or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
            or type(warning_percent) is not int or not 1 <= warning_percent <= 99
            or type(percent_used_floor) is not int or percent_used_floor < 0):
        raise OwnerInboxDataError()
    return {
        "status": status,
        "period": period,
        "currency": currency,
        "warning_percent": warning_percent,
        "percent_used_floor": percent_used_floor,
    }


def device_entry(owner: str, token_id: str, device_name: str, added_at: float) -> dict:
    if not isinstance(device_name, str) or not 1 <= len(device_name) <= 64 or not device_name.isprintable():
        raise OwnerInboxDataError()
    try:
        added_at = _non_negative_float(added_at)
        date = datetime.fromtimestamp(added_at, timezone.utc).date().isoformat()
    except (ValueError, OverflowError, OSError):
        raise OwnerInboxDataError() from None
    return {
        "kind": KIND_DEVICE,
        "subject_id": opaque_subject_id(owner, KIND_DEVICE, token_id),
        "milestone": "added",
        "ref": {"token_id": token_id},
        "params": {"status": "added", "device_name": device_name,
                   "date": date},
    }


def reads_version_digest(owner: str, reads_revision: int,
                         read_rows: Iterable[tuple[str, str, str, int | float]]) -> str:
    """CAS digest over ``owner``, monotonic ``reads_revision`` and read markers only (never live alerts)."""
    if not isinstance(owner, str) or not owner or type(reads_revision) is not int or reads_revision < 0:
        raise OwnerInboxDataError()
    normalized = []
    for kind, subject_id, milestone, read_at in read_rows:
        if kind not in _KIND_ORDER or not isinstance(subject_id, str) or not _HEX64.fullmatch(subject_id):
            raise OwnerInboxDataError()
        _check_milestone(milestone)
        normalized.append((kind, subject_id, milestone, _non_negative_float(read_at)))
    normalized.sort(key=lambda item: (item[0], item[1], item[2]))
    return projection_digest([
        _READS_DIGEST_VERSION,
        owner,
        reads_revision,
        [[kind, subject_id, milestone, read_at] for kind, subject_id, milestone, read_at in normalized],
    ])


def build_inbox_items(active_entries: Iterable[dict],
                      read_map: Mapping[tuple[str, str, str], int | float]) -> list[dict]:
    """Attach read state to live active alerts and sort deterministically without ``created_at``."""
    seen = set()
    items = []
    for entry in active_entries:
        kind = entry["kind"]
        subject_id = entry["subject_id"]
        milestone = entry["milestone"]
        params = entry["params"]
        key = (kind, subject_id, milestone)
        if key in seen:
            raise OwnerInboxDataError()
        seen.add(key)
        raw_read_at = read_map.get(key)
        read_at = None if raw_read_at is None else _non_negative_float(raw_read_at)
        items.append({
            "kind": kind,
            "subject_id": subject_id,
            "milestone": milestone,
            "ref": entry["ref"],
            "read": read_at is not None,
            "read_at": read_at,
            "params": params,
        })
    items.sort(key=lambda item: (
        item["read"],
        _KIND_ORDER[item["kind"]],
        _SEVERITY_ORDER[item["kind"]][item["params"]["status"]],
        item["subject_id"],
        item["milestone"],
    ))
    return items


def validate_mark_items(items) -> list[tuple[str, str, str]]:
    """Validate and order-preserving deduplicate 1..100 inbox mark keys from a write request."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_MARK_BATCH:
        raise AnalyticsError()
    seen = set()
    result = []
    for entry in items:
        if not isinstance(entry, dict) or set(entry) != _KEY_FIELDS:
            raise AnalyticsError()
        kind, subject_id, milestone = entry["kind"], entry["subject_id"], entry["milestone"]
        if (kind not in _KIND_ORDER
                or not isinstance(subject_id, str) or not _HEX64.fullmatch(subject_id)
                or not isinstance(milestone, str) or not _MILESTONE.fullmatch(milestone)):
            raise AnalyticsError()
        key = (kind, subject_id, milestone)
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result
