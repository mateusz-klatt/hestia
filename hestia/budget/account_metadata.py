"""Portable account labels; never account identity, selection or API freshness."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import unicodedata

from ..db import BudgetAccountDisplay

_FIELDS = {"product", "currency", "masked_suffix", "observed_at"}
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_SUFFIX = re.compile(r"[A-Z0-9]{4}", re.ASCII)
_DIGITS = re.compile(r"[0-9](?:[\W_]*[0-9]){3}")
_UUID = re.compile(r"[A-F0-9]{8}(?:-[A-F0-9]{4}){3}-[A-F0-9]{12}|(?<![A-Z0-9])[A-F0-9]{32}(?![A-Z0-9])")
_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}[ -]*[0-9]{2}(?:[ -]*[A-Z0-9]){11,30}(?![A-Z0-9])", re.ASCII)
_LONG_TOKEN = re.compile(r"[^\W_]{12,}")
_LOOKALIKES = str.maketrans({"А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
                           "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I",
                           "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
                           "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
                           "Υ": "Y", "Χ": "X"})
_DOMAIN = b"hestia/budget/account-display/v1\0"


class BudgetMetadataError(ValueError):
    CODES = frozenset({"invalid_metadata", "metadata_conflict"})

    def __init__(self, code="invalid_metadata"):
        self.code = code if isinstance(code, str) and code in self.CODES else "invalid_metadata"
        super().__init__("Invalid budget account metadata: " + self.code)


@dataclass(frozen=True)
class AccountMetadata:
    product: str | None
    currency: str | None
    masked_suffix: str | None
    observed_at: float
    digest: str
    product_rejected: bool = False


def _clock(value):
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError
        return float(value)
    except (ValueError, OverflowError):
        raise BudgetMetadataError() from None


def _observed(value, now):
    try:
        if not isinstance(value, str) or len(value) > 64 or "T" not in value:
            raise ValueError
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError
        observed = stamp.timestamp()
        if not math.isfinite(observed) or not 0 <= observed <= now:
            raise ValueError
        return observed
    except (ValueError, OverflowError, OSError):
        raise BudgetMetadataError() from None


def _clean_product(value):
    normalized = unicodedata.normalize("NFC", value)
    clean = "".join(" " if char.isspace() else "" if unicodedata.category(char) in {"Cc", "Cf"}
                    else char for char in normalized)
    return " ".join(clean.split())


def _identifier_probe(value):
    # Compatibility/confusable folding is only a conservative rejection probe;
    # accepted display text itself retains NFC spelling.
    probe = unicodedata.normalize("NFKC", value).upper().translate(_LOOKALIKES)
    return "".join(str(unicodedata.digit(char)) if char.isdigit() else
                   "-" if unicodedata.category(char) == "Pd" else char for char in probe)


def _has_identifier(value, primary):
    probe = _identifier_probe(value)
    if isinstance(primary, str):
        known = "".join(char for char in _identifier_probe(_clean_product(primary)) if char.isalnum())
        compact = "".join(char for char in probe if char.isalnum())
        if known and known in compact:
            return True
    if _DIGITS.search(probe) or _UUID.search(probe):
        return True
    if _IBAN.search(probe):
        return True
    return any(any(char.isalpha() for char in token) and any(char.isdigit() for char in token)
               for token in _LONG_TOKEN.findall(probe))


def _product(value, primary):
    if value is None:
        return None, False
    if not isinstance(value, str) or len(value) > 1024:
        raise BudgetMetadataError()
    clean = _clean_product(value)
    if not clean:
        return None, False
    if _has_identifier(clean, primary):
        return None, True
    return clean[:64], False


def _code(value, pattern):
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BudgetMetadataError()
    return value


def parse_account_metadata(raw, *, now, context: dict) -> AccountMetadata:
    """Validate one complete metadata patch without touching files or SQLite."""
    now = _clock(now)
    if not isinstance(raw, dict) or set(raw) != _FIELDS or not isinstance(context, dict):
        raise BudgetMetadataError()
    observed_at = _observed(raw["observed_at"], now)
    product, rejected = _product(raw["product"], context.get("identification_hash"))
    currency = _code(raw["currency"], _CURRENCY)
    suffix = _code(raw["masked_suffix"], _SUFFIX)
    payload = {"context": context, "observed_at": observed_at, "product": product,
               "currency": currency, "masked_suffix": suffix}
    try:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
                               separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BudgetMetadataError() from None
    return AccountMetadata(product, currency, suffix, observed_at, hashlib.sha256(_DOMAIN + canonical).hexdigest(), rejected)


def apply_account_metadata(session, account_id, metadata, *, now):
    """Apply under the caller's owner-checked transaction; never create accounts."""
    now = _clock(now)
    if not isinstance(metadata, AccountMetadata) or not 0 <= metadata.observed_at <= now:
        raise BudgetMetadataError()
    if metadata.product is None and metadata.currency is None and metadata.masked_suffix is None:
        return "empty"
    row = session.get(BudgetAccountDisplay, account_id)
    if row is not None:
        if row.digest == metadata.digest:
            return "unchanged"
        if metadata.observed_at < row.observed_at:
            return "older"
        if metadata.observed_at == row.observed_at:
            raise BudgetMetadataError("metadata_conflict")
    else:
        row = BudgetAccountDisplay(account_id=account_id)
        session.add(row)
    for field in ("product", "currency", "masked_suffix"):
        value = getattr(metadata, field)
        if value is not None:
            setattr(row, field, value)
    row.source, row.observed_at, row.imported_at, row.digest = "offline_import", metadata.observed_at, now, metadata.digest
    return "updated"
