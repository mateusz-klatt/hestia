"""Explicit private-file ingestion of one complete Enable Banking account.

Only normalized observations leave this module. No API calls, credential reads,
database writes, or account discovery occur while an import is being validated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math

from .config import BudgetConfig, BudgetConfigError, _read_secret
from .account_metadata import AccountMetadata, BudgetMetadataError, parse_account_metadata
from . import normalize

MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_NORMALIZED_BYTES = normalize.MAX_NORMALIZED_BYTES
MAX_RECORDS = normalize.MAX_TRANSACTIONS


class BudgetImportError(ValueError):
    """Safe diagnostics without file paths, bank bodies, identifiers or secrets."""

    CODES = frozenset({"invalid_file", "invalid_json", "invalid_import", "context_mismatch",
                       "invalid_timestamp", "incomplete_import", "limit_exceeded",
                       "invalid_data", "unsupported_amount", "invalid_metadata"})

    def __init__(self, code="invalid_import"):
        self.code = code if isinstance(code, str) and code in self.CODES else "invalid_import"
        super().__init__("Invalid budget import: " + self.code)


@dataclass(frozen=True)
class ImportSnapshot:
    connection_id: str
    identification_hash: str
    fetched_at: float
    transactions: tuple[dict, ...]
    balances: tuple[dict, ...] | None
    digest: str
    metadata: AccountMetadata | None = None


def _object(value, required, optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or value.keys() - set(required) - set(optional)):
        raise BudgetImportError()
    return value


def _input_size(raw):
    total = 0
    encoder = json.JSONEncoder(allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    try:
        for chunk in encoder.iterencode(raw):
            total += len(chunk.encode("utf-8"))
            if total > MAX_IMPORT_BYTES:
                raise BudgetImportError("limit_exceeded")
    except BudgetImportError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BudgetImportError("invalid_json") from None


def _context(raw, config):
    connections = [item for item in config.connections if item.id == raw["connection_id"]]
    if len(connections) != 1:
        raise BudgetImportError("context_mismatch")
    connection = connections[0]
    bank = _object(raw["aspsp"], {"name", "country"})
    if (bank["name"] != connection.bank_name or bank["country"] != connection.country
            or raw["identification_hash"] not in connection.accounts):
        raise BudgetImportError("context_mismatch")
    return connection


def _timestamp(value, now):
    try:
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError
        if not isinstance(value, str) or len(value) > 64 or "T" not in value:
            raise ValueError
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError
        result = stamp.timestamp()
        if not math.isfinite(result) or not 0 <= result <= now:
            raise ValueError
        return result
    except (ValueError, OverflowError, OSError):
        raise BudgetImportError("invalid_timestamp") from None


def _records(value, budget):
    if not isinstance(value, list):
        raise BudgetImportError("invalid_data")
    budget["records"] -= len(value)
    if budget["records"] < 0:
        raise BudgetImportError("limit_exceeded")
    return value


def _transactions(response, budget):
    response = _object(response, {"transactions"}, {"continuation_key"})
    if response.get("continuation_key") is not None:
        raise BudgetImportError("incomplete_import")
    result, references = [], {}
    for raw in _records(response["transactions"], budget):
        transaction = normalize.normalize_transaction(raw)
        normalize._charge(transaction, budget)
        normalize._append_unique(transaction, result, references)
    return tuple(result)


def _balances(response, budget):
    response = _object(response, {"balances"})
    _records(response["balances"], budget)
    balances = normalize.normalize_balances(response)
    for balance in balances:
        normalize._charge(balance, budget)
    return balances


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(config, connection, primary, fetched_at, transactions, balances):
    context = {"version": 1, "application_id": config.application_id, "owner": connection.owner,
               "connection_id": connection.id, "aspsp": {"name": connection.bank_name, "country": connection.country},
               "identification_hash": primary, "fetched_at": fetched_at,
               "transactions": sorted(_canonical(row) for row in transactions),
               "balances": None if balances is None else sorted(_canonical(row) for row in balances)}
    return hashlib.sha256(_canonical(context).encode("utf-8")).hexdigest()


def parse_import(raw, config: BudgetConfig, *, now: float) -> ImportSnapshot:
    """Validate an explicit one-account document without filtering its history."""
    raw = _object(raw, {"version", "connection_id", "aspsp", "identification_hash", "fetched_at",
                        "complete", "transactions"}, {"balances", "account"})
    _input_size(raw)
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise BudgetImportError()
    if raw["complete"] is not True:
        raise BudgetImportError("incomplete_import")
    connection = _context(raw, config)
    fetched_at = _timestamp(raw["fetched_at"], now)
    primary = raw["identification_hash"]
    metadata = None
    if "account" in raw:
        context = {"application_id": config.application_id, "owner": connection.owner,
                   "connection_id": connection.id,
                   "aspsp": {"name": connection.bank_name, "country": connection.country},
                   "identification_hash": primary}
        try:
            metadata = parse_account_metadata(raw["account"], now=now, context=context)
        except BudgetMetadataError:
            raise BudgetImportError("invalid_metadata") from None
    budget = {"records": MAX_RECORDS, "bytes": MAX_NORMALIZED_BYTES}
    try:
        transactions = _transactions(raw["transactions"], budget)
        balances = _balances(raw["balances"], budget) if "balances" in raw else None
    except normalize.BudgetDataError as exc:
        raise BudgetImportError(exc.code) from None
    digest = _digest(config, connection, primary, fetched_at, transactions, balances)
    return ImportSnapshot(connection.id, primary, fetched_at, transactions, balances, digest, metadata)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetImportError("invalid_json")
        result[key] = value
    return result


def _reject_constant(_value):
    raise BudgetImportError("invalid_json")


def load_import(path, config: BudgetConfig, *, now: float) -> ImportSnapshot:
    """Read a bounded private regular file, then validate it before any DB use."""
    try:
        data = _read_secret(path, max_bytes=MAX_IMPORT_BYTES)
    except (BudgetConfigError, TypeError):
        raise BudgetImportError("invalid_file") from None
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except BudgetImportError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise BudgetImportError("invalid_json") from None
    return parse_import(raw, config, now=now)
