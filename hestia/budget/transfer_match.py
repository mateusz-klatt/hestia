"""Pure own-transfer candidate matching: suggestions only, never decisions.

The caller passes canonical observations of the owner's selected accounts and
marks entries that may not pair (pending, uncertain, undated) as ineligible.
A candidate joins one DBIT and one CRDT on different selected accounts with the
same exact amount and currency and dates at most three calendar days apart.

Structured counterparty identifiers are evidence, never a requirement. When a
leg names an account in a scheme the other account also uses and none of the
identifiers agree, the legs contradict each other and no heuristic fallback is
offered. Every entry with more than one remaining candidate makes all of its
candidates ambiguous: nothing here picks the nearest date or the first row.

Errors carry fixed messages and never echo amounts, references or identifiers.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
import re

RULE_VERSION = 1
MAX_DAYS = 3
MAX_EDGES = 100_000
MAX_TEXT = 1024
MAX_AMOUNT = 80
MAX_IDENTIFIERS = 16
MAX_SCHEME = 32
MAX_IDENTIFICATION = 128
SOURCES = ("account_identity", "heuristic")

_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)


class TransferInputError(ValueError):
    """Malformed or out-of-scope input; the message never contains input values."""

    def __init__(self):
        super().__init__("Invalid transfer matching input")


class TransferMatchLimitError(ValueError):
    """Too many compatible pairs to examine; no partial result is returned."""

    def __init__(self):
        super().__init__("Transfer matching limit exceeded")


@dataclass(frozen=True, repr=False)
class TransferEntry:
    owner: str
    account_id: str
    entry_reference: str
    amount: str
    currency: str
    direction: str
    date: str
    fingerprint: str
    counterparty_accounts: tuple[tuple[str, str], ...] = ()
    eligible: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.account_id, self.entry_reference)


@dataclass(frozen=True, repr=False)
class TransferCandidate:
    debit_key: tuple[str, str]
    credit_key: tuple[str, str]
    debit_fingerprint: str
    credit_fingerprint: str
    source: str
    date_distance: int
    ambiguous: bool


@dataclass(frozen=True)
class _Leg:
    key: tuple[str, str]
    day: int
    fingerprint: str
    counterparties: frozenset[tuple[str, str]]


def _text(value, maximum=MAX_TEXT):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise TransferInputError()
    return value


def _reference(value):
    """Entry references are opaque bank values: kept verbatim, as the normaliser stores them."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TEXT:
        raise TransferInputError()
    return value


def _key(value):
    if not isinstance(value, tuple) or len(value) != 2:
        raise TransferInputError()
    return (_text(value[0]), _reference(value[1]))


def _identifiers(value):
    if not isinstance(value, tuple) or len(value) > MAX_IDENTIFIERS:
        raise TransferInputError()
    result = set()
    for item in value:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TransferInputError()
        result.add((_text(item[0], MAX_SCHEME), _text(item[1], MAX_IDENTIFICATION)))
    return frozenset(result)


def _amount(value):
    """Canonical exact decimal string: equal values compare equal as strings, no float or Decimal context."""
    if not isinstance(value, str) or len(value) > MAX_AMOUNT or not _AMOUNT.fullmatch(value):
        raise TransferInputError()
    integer, _, fraction = value.partition(".")
    integer, fraction = integer.lstrip("0") or "0", fraction.rstrip("0")
    return integer + ("." + fraction if fraction else "")


def _day(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise TransferInputError()
    try:
        return date.fromisoformat(value).toordinal()
    except ValueError:
        raise TransferInputError() from None


def _evidence(counterparties, own):
    """True: an identifier names the other account. False: comparable but different. None: unknown."""
    if not counterparties:
        return None
    if counterparties & own:
        return True
    if {scheme for scheme, _ in counterparties} & {scheme for scheme, _ in own}:
        return False
    return None


def _scope(owner, own_accounts, blocked_pairs, reserved_entries, max_edges):
    owner = _text(owner)
    if not isinstance(own_accounts, Mapping):
        raise TransferInputError()
    accounts = {_text(account): _identifiers(ids) for account, ids in own_accounts.items()}
    if not isinstance(blocked_pairs, (set, frozenset)) or not isinstance(reserved_entries, (set, frozenset)):
        raise TransferInputError()
    blocked = set()
    for pair in blocked_pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise TransferInputError()
        blocked.add((_key(pair[0]), _key(pair[1])))
    reserved = {_key(item) for item in reserved_entries}
    if type(max_edges) is not int or max_edges < 0:
        raise TransferInputError()
    return owner, accounts, blocked, reserved


def _buckets(owner, entries, accounts, reserved):
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, Mapping)):
        raise TransferInputError()
    seen, buckets = set(), {}
    for entry in entries:
        if not isinstance(entry, TransferEntry) or entry.owner != owner:
            raise TransferInputError()
        key = _key(entry.key)
        if key in seen or type(entry.eligible) is not bool:
            raise TransferInputError()
        seen.add(key)
        if not entry.eligible:
            continue
        if (entry.account_id not in accounts or entry.direction not in ("DBIT", "CRDT")
                or not isinstance(entry.currency, str) or not _CURRENCY.fullmatch(entry.currency)
                or not isinstance(entry.fingerprint, str) or not _FINGERPRINT.fullmatch(entry.fingerprint)):
            raise TransferInputError()
        amount, day = _amount(entry.amount), _day(entry.date)
        leg = _Leg(key, day, entry.fingerprint, _identifiers(entry.counterparty_accounts))
        if amount == "0" or key in reserved:
            continue
        debits, credits = buckets.setdefault((entry.currency, amount), ([], []))
        (debits if entry.direction == "DBIT" else credits).append(leg)
    return buckets


def _edges(buckets, accounts, blocked, max_edges):
    edges, examined = [], 0
    for _, (debits, credits) in sorted(buckets.items()):
        credits.sort(key=lambda leg: (leg.day, leg.key))
        days = [leg.day for leg in credits]
        for debit in debits:
            low, high = bisect_left(days, debit.day - MAX_DAYS), bisect_right(days, debit.day + MAX_DAYS)
            examined += high - low
            if examined > max_edges:
                raise TransferMatchLimitError()
            for credit in credits[low:high]:
                if debit.key[0] == credit.key[0] or (debit.key, credit.key) in blocked:
                    continue
                sides = (_evidence(debit.counterparties, accounts[credit.key[0]]),
                         _evidence(credit.counterparties, accounts[debit.key[0]]))
                if False in sides:
                    continue
                edges.append((debit, credit, "account_identity" if True in sides else "heuristic"))
    return edges


def match_transfers(owner: str, entries: Iterable[TransferEntry],
                    own_accounts: Mapping[str, tuple[tuple[str, str], ...]], *,
                    blocked_pairs: frozenset = frozenset(), reserved_entries: frozenset = frozenset(),
                    max_edges: int = MAX_EDGES) -> tuple[TransferCandidate, ...]:
    """Return every possible own-transfer pair among the owner's selected accounts, deterministically ordered.

    ``own_accounts`` maps each selected account of ``owner`` to its canonical (scheme, identification) pairs.
    ``blocked_pairs`` holds (debit_key, credit_key) pairs the owner rejected or undid; ``reserved_entries``
    holds entry keys already in an effective pair. ``max_edges`` bounds the compatible pairs examined,
    counted before account, block and evidence checks.
    """
    owner, accounts, blocked, reserved = _scope(owner, own_accounts, blocked_pairs, reserved_entries, max_edges)
    edges = _edges(_buckets(owner, entries, accounts, reserved), accounts, blocked, max_edges)
    degree = {}
    for debit, credit, _ in edges:
        degree[debit.key] = degree.get(debit.key, 0) + 1
        degree[credit.key] = degree.get(credit.key, 0) + 1
    edges.sort(key=lambda edge: (edge[0].day, edge[0].key, edge[1].day, edge[1].key))
    return tuple(TransferCandidate(debit.key, credit.key, debit.fingerprint, credit.fingerprint, source,
                                   abs(debit.day - credit.day), degree[debit.key] > 1 or degree[credit.key] > 1)
                 for debit, credit, source in edges)
