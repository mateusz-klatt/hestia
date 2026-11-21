"""Pure analytics validation, material fingerprints and exact decimal arithmetic."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import unicodedata

MAX_ROWS = 100_000
KINDS = frozenset({"auto", "expense", "income", "refund", "excluded"})
_FIELDS = frozenset({"description", "counterparty_name", "mcc"})
_MONEY = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_PERIOD = re.compile(r"([0-9]{4})(?:-([0-9]{2}))?\Z", re.ASCII)
_CODES = frozenset({"budget_analytics_invalid_request", "budget_analytics_not_found", "budget_analytics_conflict",
                    "budget_analytics_source_changed", "budget_analytics_stale_preview", "budget_analytics_stale_cursor", "budget_analytics_preview_expired",
                    "budget_analytics_leg_reserved", "budget_analytics_limit", "budget_analytics_unavailable"})


class AnalyticsError(ValueError):
    """Fixed safe diagnostics without bank data, request text or provider identifiers."""

    def __init__(self, code="budget_analytics_invalid_request", status=400):
        self.code = code if isinstance(code, str) and code in _CODES else "budget_analytics_invalid_request"
        self.status = status if type(status) is int and status in (400, 404, 409, 413, 503) else 400
        super().__init__(self.code)


def projection_digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise AnalyticsError() from None
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _parts(value):
    if not isinstance(value, str) or len(value) > 256 or not _MONEY.fullmatch(value):
        raise AnalyticsError()
    integer, _, fraction = value.partition(".")
    negative = integer.startswith("-")
    digits = integer.removeprefix("-") + fraction
    coefficient = int(digits)
    return -coefficient if negative else coefficient, len(fraction)


def _format(coefficient, scale):
    negative = coefficient < 0
    digits = str(abs(coefficient)).zfill(scale + 1)
    if scale:
        integer, fraction = digits[:-scale], digits[-scale:].rstrip("0")
        result = integer + ("." + fraction if fraction else "")
    else:
        result = digits
    return "-" + result if negative and coefficient else result


def canonical_money(value):
    return _format(*_parts(value))


def sum_money(values):
    total, scale = 0, 0
    for count, value in enumerate(values, 1):
        if count > MAX_ROWS:
            raise AnalyticsError("budget_analytics_limit", 413)
        coefficient, current = _parts(value)
        if current > scale:
            total *= 10 ** (current - scale)
            scale = current
        total += coefficient * 10 ** (scale - current)
    return _format(total, scale)


def negate_money(value):
    coefficient, scale = _parts(value)
    return _format(-coefficient, scale)


def compare_money(first, second):
    left, ls = _parts(first)
    right, rs = _parts(second)
    scale = max(ls, rs)
    difference = left * 10 ** (scale - ls) - right * 10 ** (scale - rs)
    return (difference > 0) - (difference < 0)


def effective_date(payload):
    value = payload.get("booking_date") or payload.get("value_date")
    if value is None:
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise AnalyticsError() from None
    return value


def parse_period(value):
    match = _PERIOD.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise AnalyticsError()
    year, month = int(match[1]), int(match[2]) if match[2] is not None else None
    if not 1900 <= year <= 9999 or (month is not None and not 1 <= month <= 12):
        raise AnalyticsError()
    if month is None:
        return f"{year:04d}-01-01", f"{year:04d}-12-31"
    start = date(year, month, 1)
    end = date(year, month + 1, 1).toordinal() - 1 if month < 12 else date(year, 12, 31).toordinal()
    return start.isoformat(), date.fromordinal(end).isoformat()


def financial_fingerprint(payload):
    if not isinstance(payload, dict):
        raise AnalyticsError()
    amount, currency, direction, status = (payload.get(name) for name in
                                          ("amount", "currency", "credit_debit_indicator", "status"))
    if (not isinstance(amount, str) or len(amount) > 80 or compare_money(amount, "0") < 0
            or not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency, re.ASCII)
            or direction not in ("DBIT", "CRDT")
            or status not in ("BOOK", "PDNG", "HOLD", "CNCL", "RJCT", "SCHD", "OTHR")):
        raise AnalyticsError()
    return projection_digest(["financial-v1", canonical_money(amount), currency, direction, status, effective_date(payload)])


def _identities(values):
    if not isinstance(values, (list, tuple)):
        raise AnalyticsError()
    result = set()
    for value in values:
        if (not isinstance(value, dict) or set(value) != {"scheme", "identification"}
                or any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 1024
                       for key in ("scheme", "identification"))):
            raise AnalyticsError()
        result.add((value["scheme"], value["identification"]))
    return sorted(result)


def owned_identities(own_ids):
    """Validated, sorted own identities per account; computed once per projection, not per entry."""
    if not isinstance(own_ids, dict):
        raise AnalyticsError()
    return {account: _identities(values) for account, values in own_ids.items()}


def pair_fingerprint(account_id, ref, payload, metadata=None, own_ids=None, *, financial=None, owned_ids=None):
    """``financial`` may carry ``financial_fingerprint(payload)`` and ``owned_ids`` the result of
    ``owned_identities(own_ids)``, both already computed by the caller for the same inputs."""
    if not isinstance(account_id, str) or not account_id or not isinstance(ref, str) or not ref:
        raise AnalyticsError()
    metadata = {} if metadata is None else metadata
    own_ids = {} if own_ids is None else own_ids
    if not isinstance(metadata, dict) or not isinstance(own_ids, dict):
        raise AnalyticsError()
    evidence = {key: _identities(metadata.get(key, [])) for key in ("creditor_accounts", "debtor_accounts")}
    observed = set(evidence["creditor_accounts"] + evidence["debtor_accounts"])
    owned = {}
    for account, identities in (owned_identities(own_ids) if owned_ids is None else owned_ids).items():
        if account == account_id or observed.intersection(identities):
            owned[account] = identities
    financial = financial_fingerprint(payload) if financial is None else financial
    return projection_digest(["pair-v1", account_id, ref, financial, evidence, owned])


def _value(row, key):
    return row.get(key) if isinstance(row, dict) else getattr(row, key)


def compatible_kind(kind, payload):
    return (kind in ("auto", "excluded") or (kind == "expense" and payload.get("credit_debit_indicator") == "DBIT")
            or (kind in ("income", "refund") and payload.get("credit_debit_indicator") == "CRDT"))


def rule_matches(rule, payload, metadata=None):
    if not _value(rule, "enabled") or not compatible_kind(_value(rule, "kind"), payload):
        return False
    field, operator = _value(rule, "field"), _value(rule, "operator")
    if field not in _FIELDS or operator not in ("equals", "contains"):
        raise AnalyticsError()
    metadata = {} if metadata is None else metadata
    if field == "description":
        text = "\n".join(payload.get("remittance_information", []))
    elif field == "counterparty_name":
        key = "creditor_name" if payload.get("credit_debit_indicator") == "DBIT" else "debtor_name"
        text = metadata.get(key) or ""
    else:
        text = metadata.get("mcc") or ""
    text = unicodedata.normalize("NFC", text).casefold()
    value = unicodedata.normalize("NFC", _value(rule, "value")).casefold()
    return value == text if operator == "equals" else value in text
