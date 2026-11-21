"""Pure recurring-charge hint detection: evidence-backed hints only, never decisions.

The caller passes canonical observations of the owner's selected accounts and
marks entries that cannot support hints (pending, uncertain, undated) as
ineligible. Only booked DBIT expenses with a non-zero exact decimal amount and
deterministic merchant evidence (structured counterparty name, falling back to
remittance description) are grouped, separately per currency and label source.

A hint requires at least three observed monthly occurrences across at least
three distinct calendar months with consecutive gaps in [23, 38] days. Clusters
containing shorter gaps (weekly/bi-weekly charges, frequent purchases, duplicate
charges) are rejected rather than arbitrarily pruned. Hints show every observed
occurrence as evidence and never assert that a subscription is unwanted or
unused.

Errors carry fixed messages and never echo amounts, references, descriptions or
identifiers.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
import re
import unicodedata

RULE_VERSION = 1
MIN_OCCURRENCES = 3
MIN_GAP_DAYS = 23
MAX_GAP_DAYS = 38
MAX_ENTRIES = 100_000
MAX_TEXT = 1024
MAX_DESCRIPTION = 512 * 1024
MAX_AMOUNT = 80
LABEL_SOURCES = ("counterparty_name", "description")
EFFECTIVE_KINDS = frozenset({"expense", "income", "refund", "excluded", "unclassified_credit", "transfer"})

_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RecurringInputError(ValueError):
    """Malformed or out-of-scope input; the message never contains input values."""

    def __init__(self):
        super().__init__("Invalid recurring charge input")


class RecurringLimitError(ValueError):
    """Too many entries to examine; no partial result is returned."""

    def __init__(self):
        super().__init__("Recurring charge detection limit exceeded")


@dataclass(frozen=True, repr=False)
class RecurringEntry:
    owner: str
    account_id: str
    entry_reference: str
    amount: str
    currency: str
    direction: str
    effective_kind: str
    date: str
    fingerprint: str
    counterparty_name: str | None = None
    description: str = ""
    eligible: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.account_id, self.entry_reference)


@dataclass(frozen=True, repr=False)
class RecurringOccurrence:
    account_id: str
    entry_reference: str
    date: str
    fingerprint: str


@dataclass(frozen=True, repr=False)
class RecurringHint:
    label: str
    normalized_label: str
    label_source: str
    amount: str
    currency: str
    cadence: str
    occurrence_count: int
    distinct_months: int
    first_date: str
    last_date: str
    min_gap_days: int
    max_gap_days: int
    account_ids: tuple[str, ...]
    evidence: tuple[RecurringOccurrence, ...]
    rule_version: int = RULE_VERSION


@dataclass(frozen=True)
class _Charge:
    day: int
    year_month: tuple[int, int]
    occurrence: RecurringOccurrence
    display_label: str


def _text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or _CONTROL.search(value):
        raise RecurringInputError()
    return value


def _reference(value):
    """Entry references are opaque bank values: kept verbatim, as the normaliser stores them."""
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_TEXT:
        raise RecurringInputError()
    return value


def _key(value):
    if not isinstance(value, tuple) or len(value) != 2:
        raise RecurringInputError()
    return (_text(value[0]), _reference(value[1]))


def _amount(value):
    """Canonical exact decimal string: equal values compare equal as strings, no float or Decimal context."""
    if not isinstance(value, str) or len(value) > MAX_AMOUNT or not _AMOUNT.fullmatch(value):
        raise RecurringInputError()
    integer, _, fraction = value.partition(".")
    integer, fraction = integer.lstrip("0") or "0", fraction.rstrip("0")
    return integer + ("." + fraction if fraction else "")


def _parse_date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise RecurringInputError()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise RecurringInputError() from None
    return parsed.toordinal(), (parsed.year, parsed.month)


def _clean_label(value):
    return " ".join(_CONTROL.sub(" ", unicodedata.normalize("NFC", value)).split())


def _label(counterparty_name, description):
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
        raise RecurringInputError()
    if counterparty_name is not None:
        if not isinstance(counterparty_name, str) or len(counterparty_name) > MAX_TEXT:
            raise RecurringInputError()
        cleaned = _clean_label(counterparty_name)
        if cleaned:
            return "counterparty_name", cleaned, cleaned.casefold()
    cleaned = _clean_label(description)
    return ("description", cleaned, cleaned.casefold()) if cleaned else None


def _scope(owner, selected_accounts, max_entries):
    owner = _text(owner)
    if not isinstance(selected_accounts, (set, frozenset)):
        raise RecurringInputError()
    accounts = frozenset(_text(account) for account in selected_accounts)
    if type(max_entries) is not int or max_entries < 0:
        raise RecurringInputError()
    return owner, accounts


def _validated_charge(owner, entry, accounts, seen):
    if not isinstance(entry, RecurringEntry) or entry.owner != owner:
        raise RecurringInputError()
    key = _key(entry.key)
    if key in seen or type(entry.eligible) is not bool:
        raise RecurringInputError()
    seen.add(key)
    if not entry.eligible:
        return None
    if (entry.account_id not in accounts or entry.direction not in ("DBIT", "CRDT")
            or entry.effective_kind not in EFFECTIVE_KINDS
            or not isinstance(entry.currency, str) or not _CURRENCY.fullmatch(entry.currency)
            or not isinstance(entry.fingerprint, str) or not _FINGERPRINT.fullmatch(entry.fingerprint)):
        raise RecurringInputError()
    amount = _amount(entry.amount)
    day, year_month = _parse_date(entry.date)
    merchant = _label(entry.counterparty_name, entry.description)
    if entry.direction != "DBIT" or entry.effective_kind != "expense" or amount == "0" or merchant is None:
        return None
    source, display, normalized = merchant
    occurrence = RecurringOccurrence(entry.account_id, entry.entry_reference, entry.date, entry.fingerprint)
    return (entry.currency, source, normalized, amount), _Charge(day, year_month, occurrence, display)


def _groups(owner, entries, accounts, max_entries):
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes, Mapping)):
        raise RecurringInputError()
    seen, groups = set(), {}
    for count, entry in enumerate(entries, 1):
        if count > max_entries:
            raise RecurringLimitError()
        item = _validated_charge(owner, entry, accounts, seen)
        if item is not None:
            group_key, charge = item
            groups.setdefault(group_key, []).append(charge)
    return groups


def _clusters(items):
    ordered = sorted(items, key=lambda item: (item.day, item.occurrence.account_id, item.occurrence.entry_reference))
    clusters = [[ordered[0]]]
    for item in ordered[1:]:
        if item.day - clusters[-1][-1].day > MAX_GAP_DAYS:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _hint_from_cluster(key, cluster):
    if len(cluster) < MIN_OCCURRENCES:
        return None
    gaps = [right.day - left.day for left, right in zip(cluster, cluster[1:])]
    min_gap, max_gap = min(gaps), max(gaps)
    if min_gap < MIN_GAP_DAYS:
        return None
    distinct_months = len({item.year_month for item in cluster})
    if distinct_months < MIN_OCCURRENCES:
        return None
    currency, source, normalized, amount = key
    return RecurringHint(
        label=cluster[-1].display_label,
        normalized_label=normalized,
        label_source=source,
        amount=amount,
        currency=currency,
        cadence="monthly",
        occurrence_count=len(cluster),
        distinct_months=distinct_months,
        first_date=cluster[0].occurrence.date,
        last_date=cluster[-1].occurrence.date,
        min_gap_days=min_gap,
        max_gap_days=max_gap,
        account_ids=tuple(sorted({item.occurrence.account_id for item in cluster})),
        evidence=tuple(item.occurrence for item in cluster),
    )


def detect_recurring_charges(owner: str, entries: Iterable[RecurringEntry],
                             selected_accounts: set[str] | frozenset[str], *,
                             max_entries: int = MAX_ENTRIES) -> tuple[RecurringHint, ...]:
    """Return evidence-backed monthly recurring-charge hints, deterministically ordered.

    Only eligible DBIT expense entries on ``selected_accounts`` with non-zero exact
    amounts and non-empty merchant evidence (``counterparty_name`` preferred over
    ``description``) are considered. Each hint requires at least three monthly
    occurrences across at least three distinct calendar months with consecutive
    date gaps in [23, 38] days.
    """
    owner, accounts = _scope(owner, selected_accounts, max_entries)
    groups = _groups(owner, entries, accounts, max_entries)
    hints = []
    for key, items in sorted(groups.items()):
        for cluster in _clusters(items):
            hint = _hint_from_cluster(key, cluster)
            if hint is not None:
                hints.append(hint)
    hints.sort(key=lambda hint: (hint.currency, hint.normalized_label, hint.label_source,
                                 hint.amount, hint.first_date, hint.last_date))
    return tuple(hints)
