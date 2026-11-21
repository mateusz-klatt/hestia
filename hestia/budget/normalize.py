"""Validate complete components for selected accounts without retaining raw data.

Account UIDs belong to sessions; primary identification hashes identify accounts.
Only entry references can identify transactions across pulls. Missing references
and dates remain explicit so the store can quarantine them without fuzzy matching.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
import json
import re
import time

from .config import ConnectionConfig
from .client import BudgetAPIError

MAX_PAGES = 1000
MAX_TRANSACTIONS = 100_000
MAX_NORMALIZED_BYTES = 32 * 1024 * 1024
TRANSACTION_STATUSES = frozenset({"BOOK", "PDNG", "HOLD", "CNCL", "RJCT", "SCHD", "OTHR"})
BALANCE_TYPES = frozenset({"CLAV", "CLBD", "FWAV", "INFO", "ITAV", "ITBD", "OPAV", "OPBD",
                           "OTHR", "PRCD", "VALU", "XPCD"})
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", re.ASCII)
_MONEY = re.compile(r"-?[0-9]+(?:\.[0-9]+)?", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


class BudgetDataError(ValueError):
    """An allowlisted diagnostic, never containing source data or identifiers."""

    CODES = frozenset({"invalid_data", "unsupported_amount", "invalid_session",
                       "expired_session", "session_mismatch", "account_selection", "invalid_pagination",
                       "limit_exceeded", "deferred"})

    def __init__(self, code="invalid_data"):
        self.code = code if isinstance(code, str) and code in self.CODES else "invalid_data"
        super().__init__("Invalid budget snapshot: " + self.code)


@dataclass(frozen=True)
class AccountSnapshot:
    identification_hash: str
    balances: tuple[dict, ...] | None
    transactions: tuple[dict, ...] | None
    balances_error: str | None = None
    transactions_error: str | None = None


@dataclass(frozen=True)
class AccountFailure:
    identification_hash: str
    code: str


@dataclass(frozen=True)
class Snapshot:
    valid_until: float
    accounts: tuple[AccountSnapshot, ...]
    date_from: str
    date_to: str
    errors: tuple[AccountFailure, ...] = ()


def _object(value):
    if not isinstance(value, dict):
        raise BudgetDataError()
    return value


def _text(value, *, limit=1024):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BudgetDataError()
    return value


def _optional_text(value):
    return None if value is None or value == "" else _text(value)


def _date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise BudgetDataError()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise BudgetDataError() from None


def _money(value, *, negative):
    value = _object(value)
    currency = value.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        raise BudgetDataError()
    amount = value.get("amount")
    if not isinstance(amount, str) or len(amount) > 80 or not _MONEY.fullmatch(amount):
        raise BudgetDataError("unsupported_amount")
    minus = amount.startswith("-")
    if minus and not negative:
        raise BudgetDataError("unsupported_amount")
    integer, _, fraction = amount.removeprefix("-").partition(".")
    integer, fraction = integer.lstrip("0") or "0", fraction.rstrip("0")
    canonical = integer + ("." + fraction if fraction else "")
    if minus and canonical != "0":
        canonical = "-" + canonical
    return {"amount": canonical, "currency": currency}


def normalize_transaction(value):
    """Portable fields only; absent IDs/dates are unresolved rather than guessed."""
    value = _object(value)
    money = _money(value.get("transaction_amount"), negative=False)
    direction, status = value.get("credit_debit_indicator"), value.get("status")
    if direction not in ("CRDT", "DBIT") or not isinstance(status, str) or status not in TRANSACTION_STATUSES:
        raise BudgetDataError()
    lines = value.get("remittance_information")
    if lines is None:
        lines = []
    if (not isinstance(lines, list) or len(lines) > 100
            or any(not isinstance(line, str) or len(line) > 4096 for line in lines)):
        raise BudgetDataError()
    return {"entry_reference": _optional_text(value.get("entry_reference")), **money,
            "credit_debit_indicator": direction, "status": status,
            "booking_date": _date(value.get("booking_date")), "value_date": _date(value.get("value_date")),
            "transaction_date": _date(value.get("transaction_date")), "remittance_information": list(lines)}


def normalize_balances(value):
    value = _object(value).get("balances")
    if not isinstance(value, list):
        raise BudgetDataError()
    result = []
    for item in value:
        item = _object(item)
        kind = item.get("balance_type")
        if not isinstance(kind, str) or kind not in BALANCE_TYPES:
            raise BudgetDataError()
        result.append({"name": _text(item.get("name")), "balance_type": kind,
                       "balance_amount": _money(item.get("balance_amount"), negative=True),
                       "reference_date": _date(item.get("reference_date"))})
    return tuple(result)


def _uid(value, code):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise BudgetDataError(code)
    return value.lower()


def _valid_until(value, now):
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        raise BudgetDataError("invalid_session")
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.utcoffset() is None:
            raise ValueError
        stamp = expiry.timestamp()
    except (ValueError, OverflowError):
        raise BudgetDataError("invalid_session") from None
    if stamp <= now:
        raise BudgetDataError("expired_session")
    return stamp


def _account_uids(session, selected):
    rows = session.get("accounts_data")
    if not isinstance(rows, list):
        raise BudgetDataError("account_selection")
    hashes, uids = {}, set()
    for item in rows:
        item = _object(item)
        primary = _text(item.get("identification_hash"))
        uid = _uid(item.get("uid"), "account_selection")
        if primary in hashes or uid in uids:
            raise BudgetDataError("account_selection")
        hashes[primary] = uid
        uids.add(uid)
    if not set(selected) <= hashes.keys():
        raise BudgetDataError("account_selection")
    return hashes


def _charge(value, budget):
    # ASCII JSON also bounds escaped Unicode and avoids hidden encoding failures.
    budget["bytes"] -= len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    if budget["bytes"] < 0:
        raise BudgetDataError("limit_exceeded")


def _in_window(transaction, start, end):
    stamp = transaction["booking_date"] or transaction["value_date"]
    return stamp is None or start <= stamp <= end


def _normalized_records(rows, budget):
    if not isinstance(rows, list):
        raise BudgetDataError()
    budget["remaining"] -= len(rows)
    if budget["remaining"] < 0:
        raise BudgetDataError("limit_exceeded")
    for row in rows:
        transaction = normalize_transaction(row)
        _charge(transaction, budget)
        yield transaction


def _append_unique(transaction, result, references):
    reference = transaction["entry_reference"]
    if reference is None:
        result.append(transaction)
        return
    # Different versions remain visible to quarantine. A tuple key avoids
    # quadratic pairwise comparisons when a bank reuses a reference.
    identity = (*tuple(value for key, value in transaction.items() if key != "remittance_information"),
                tuple(transaction["remittance_information"]))
    seen_versions = references.setdefault(reference, set())
    if identity not in seen_versions:
        seen_versions.add(identity)
        result.append(transaction)


def _continuation_key(page, seen):
    key = page.get("continuation_key")
    if key is None:
        return None
    if not isinstance(key, str) or not key or len(key) > 8192 or key in seen:
        raise BudgetDataError("invalid_pagination")
    seen.add(key)
    return key


def _windowed_records(result, start, end):
    # Filtering must not hide a conflicting version of a relevant reference.
    # Dateless observations remain available for explicit quarantine.
    relevant = {item["entry_reference"] for item in result
                if item["entry_reference"] is not None and _in_window(item, start, end)}
    return tuple(item for item in result if item["entry_reference"] in relevant or _in_window(item, start, end))


def _budget_exhausted(budget):
    return budget["remaining"] <= 0 or budget["bytes"] <= 0


def _transactions(client, uid, start, end, budget):
    result, references, seen = [], {}, set()
    key = None
    for _ in range(MAX_PAGES):
        if _budget_exhausted(budget):
            raise BudgetDataError("limit_exceeded")
        query = {"date_from": start, "date_to": end}
        if key is not None:
            query["continuation_key"] = key
        page = _object(client.get(f"/accounts/{uid}/transactions", query=query))
        for transaction in _normalized_records(page.get("transactions"), budget):
            _append_unique(transaction, result, references)
        key = _continuation_key(page, seen)
        if key is None:
            break
    else:
        raise BudgetDataError("limit_exceeded")
    return _windowed_records(result, start, end)


def _current_time(now):
    now = time.time() if now is None else now
    try:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise BudgetDataError("invalid_session")
    except OverflowError:
        raise BudgetDataError("invalid_session") from None
    return now


def _session_state(client, session_id, connection, now):
    session_id = _uid(session_id, "invalid_session")
    session = _object(client.get(f"/sessions/{session_id}"))
    if session.get("status") != "AUTHORIZED":
        raise BudgetDataError("invalid_session")
    bank = _object(session.get("aspsp"))
    if bank.get("name") != connection.bank_name or bank.get("country") != connection.country:
        raise BudgetDataError("session_mismatch")
    expiry = _valid_until(_object(session.get("access")).get("valid_until"), now)
    hashes = _account_uids(session, connection.accounts)
    return expiry, hashes


def _balances(client, uid, budget):
    balances = normalize_balances(client.get(f"/accounts/{uid}/balances"))
    for item in balances:
        _charge(item, budget)
    return balances


def _component(primary, operation, budget, before_component, fetch, wanted=None):
    # Untargeted components are not attempted at all: no budget diagnostic, reservation or HTTP.
    if wanted is not None and not wanted(primary, operation):
        return None, None
    if _budget_exhausted(budget):
        return None, "limit_exceeded"
    # Reservation/owner validation precedes HTTP and must remain fatal on error.
    if before_component is not None and not before_component(primary, operation):
        return None, None
    try:
        return fetch(), None
    except BudgetAPIError as exc:
        if exc.status == 401:
            raise
        return None, exc.code
    except BudgetDataError as exc:
        return None, exc.code


def _selected_account(client, primary, uid, start, end, budget, before_component, wanted=None):
    transactions, transactions_error = _component(
        primary, "transactions", budget, before_component,
        lambda: _transactions(client, uid, start, end, budget), wanted)
    balances, balances_error = _component(
        primary, "balances", budget, before_component, lambda: _balances(client, uid, budget), wanted)
    return AccountSnapshot(primary, balances, transactions, balances_error, transactions_error)


def fetch_snapshot(client, session_id: str, connection: ConnectionConfig, *, today: date, now=None,
                   before_component=None, on_session=None, wanted=None) -> Snapshot:
    """Validate the session, then independently fetch complete components.

    A failed component contributes only a safe diagnostic. HTTP401 invalidates
    the entire session; other failures do not discard healthy components.
    before_component reserves once per component; False skips it without error,
    while callback failures propagate before the corresponding HTTP request.
    ``wanted(primary, operation)`` excludes components before any of that, so a
    targeted refresh records nothing for components it did not request.
    """
    if type(today) is not date or today.toordinal() <= 364:
        raise BudgetDataError()
    start, end = (today - timedelta(days=364)).isoformat(), today.isoformat()
    expiry, hashes = _session_state(client, session_id, connection, _current_time(now))
    if on_session is not None:
        on_session(expiry)
    accounts, errors, budget = [], [], {"remaining": MAX_TRANSACTIONS, "bytes": MAX_NORMALIZED_BYTES}
    for primary in connection.accounts:
        requested = wanted is None or any(wanted(primary, operation) for operation in ("transactions", "balances"))
        if requested and _budget_exhausted(budget):
            errors.append(AccountFailure(primary, "limit_exceeded"))
            continue
        accounts.append(_selected_account(client, primary, hashes[primary], start, end, budget, before_component,
                                          wanted))
    return Snapshot(expiry, tuple(accounts), start, end, tuple(errors))
