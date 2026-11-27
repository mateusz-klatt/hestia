"""Pure recurring-charge hint detection: evidence-backed hints only, never decisions.

The caller passes canonical observations of the owner's selected accounts and
marks entries that cannot support hints (pending, uncertain, undated) as
ineligible. Only booked DBIT expenses with a non-zero exact decimal amount and
deterministic merchant evidence (structured counterparty name, falling back to
remittance description) are grouped, separately per account, currency and label
source.

A hint requires at least three observed monthly occurrences across at least
three distinct calendar months with consecutive gaps in [23, 38] days. A monthly
history may change its amount. A stable reference can connect histories with
different labels when the cadence and amounts agree. Overlapping charges on the
same account first separate unambiguous monthly cadence lanes before allowing
amount transitions, and otherwise retain independently monthly fixed-amount
histories. Other short-gap clusters (weekly/bi-weekly charges, frequent
purchases, duplicate charges) are rejected. Hints show every observed
occurrence as evidence and never assert that a subscription is unwanted or
unused.

Errors carry fixed messages and never echo amounts, references, descriptions or
identifiers.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
import re
import unicodedata

RULE_VERSION = 4
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
_REMITTANCE_TOKEN = re.compile(r"\w+(?:[-/.,]\w+)*")
# Keep whole identifiers, including their prefix and separators. Require at
# least six bare digits, or letters and a run of four digits in a policy-like ID.
_REFERENCE_ID = re.compile(
    r"[0-9]{6,32}|(?=[a-z0-9/-]{5,40}\Z)(?=.*[a-z])(?=.*[0-9]{4})[a-z0-9]+(?:[-/][a-z0-9]+)*"
)


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


@dataclass(frozen=True, repr=False)
class _Charge:
    day: int
    day_of_month: int
    year_month: tuple[int, int]
    occurrence: RecurringOccurrence
    display_label: str
    label_source: str
    amount: str
    references: frozenset[str]


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
    return parsed.toordinal(), parsed.day, (parsed.year, parsed.month)


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


def _reference_tokens(value):
    return frozenset(match[0] for match in _REMITTANCE_TOKEN.finditer(value.casefold())
                     if _REFERENCE_ID.fullmatch(match[0]))


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
    day, day_of_month, year_month = _parse_date(entry.date)
    merchant = _label(entry.counterparty_name, entry.description)
    if entry.direction != "DBIT" or entry.effective_kind != "expense" or amount == "0" or merchant is None:
        return None
    source, display, normalized = merchant
    occurrence = RecurringOccurrence(entry.account_id, entry.entry_reference, entry.date, entry.fingerprint)
    references = _reference_tokens(entry.description)
    if source == "counterparty_name":
        references |= _reference_tokens(display)
    return (
        (entry.account_id, entry.currency, source, normalized),
        _Charge(day, day_of_month, year_month, occurrence, display, source, amount, references),
    )


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


def _build_hint(currency, cluster):
    latest = cluster[-1]
    gaps = [right.day - left.day for left, right in zip(cluster, cluster[1:])]
    return RecurringHint(
        label=latest.display_label,
        normalized_label=latest.display_label.casefold(),
        label_source=latest.label_source,
        amount=latest.amount,
        currency=currency,
        cadence="monthly",
        occurrence_count=len(cluster),
        distinct_months=len({item.year_month for item in cluster}),
        first_date=cluster[0].occurrence.date,
        last_date=cluster[-1].occurrence.date,
        min_gap_days=min(gaps),
        max_gap_days=max(gaps),
        account_ids=tuple(sorted({item.occurrence.account_id for item in cluster})),
        evidence=tuple(item.occurrence for item in cluster),
    )


def _dom_distance(left: _Charge, right: _Charge) -> int:
    left_clamped = min(left.day_of_month, calendar.monthrange(*right.year_month)[1])
    right_clamped = min(right.day_of_month, calendar.monthrange(*left.year_month)[1])
    return min(abs(right.day_of_month - left_clamped), abs(left.day_of_month - right_clamped))


def _is_strict_monthly_lane(lane):
    if len(lane) < MIN_OCCURRENCES:
        return False
    for left, right in zip(lane, lane[1:]):
        if left.year_month >= right.year_month or not (MIN_GAP_DAYS <= right.day - left.day <= MAX_GAP_DAYS):
            return False
    return True


def _is_submonthly_rhythm(raw_gaps, lanes):
    if max(raw_gaps) > 15 or max(raw_gaps) - min(raw_gaps) > 2:
        return False
    return all(
        right.day - left.day <= 29
        for lane in lanes
        for left, right in zip(lane, lane[1:])
        if calendar.monthrange(*left.year_month)[1] >= 30
    )


def _cadence_lanes(cluster, raw_gaps):
    n = len(cluster)
    if n < 2 * MIN_OCCURRENCES:
        return None
    all_edges = []
    tight_edges = []
    for i, left in enumerate(cluster):
        for j in range(i + 1, n):
            right = cluster[j]
            gap = right.day - left.day
            if gap > MAX_GAP_DAYS:
                break
            if gap >= MIN_GAP_DAYS and left.year_month < right.year_month:
                dist = _dom_distance(left, right)
                all_edges.append((i, j, dist))
                if dist <= 4:
                    tight_edges.append((i, j))
    for edges, check_unused in (
        ([(i, j) for i, j, _ in all_edges], False),
        (tight_edges, True),
    ):
        succ, pred, unique = {}, {}, True
        for i, j in edges:
            if i in succ or j in pred:
                unique = False
                break
            succ[i] = j
            pred[j] = i
        if not unique:
            continue
        if check_unused:
            used = set(edges)
            if any((i, j) not in used and dist <= 5 for i, j, dist in all_edges):
                continue
        lanes = []
        for i in range(n):
            if i not in pred:
                lane = []
                cur = i
                while cur is not None:
                    lane.append(cluster[cur])
                    cur = succ.get(cur)
                if len(lane) < MIN_OCCURRENCES:
                    lanes = None
                    break
                lanes.append(lane)
        if lanes is not None and not _is_submonthly_rhythm(raw_gaps, lanes):
            return lanes
    return None


def _same_monthly_cadence(left, right):
    return (
        left.year_month < right.year_month
        and MIN_GAP_DAYS <= right.day - left.day <= MAX_GAP_DAYS
        and _dom_distance(left, right) <= 5
    )


def _reference_continues(left, right):
    if not _same_monthly_cadence(left, right):
        return False
    if left.amount == right.amount:
        return True
    # Ten percent of the previous premium allows modest adjustments while
    # keeping substantially different commitments separate despite a shared ID.
    # Fractions preserve the inclusive boundary for all supported decimals.
    previous = Fraction(left.amount)
    return abs(Fraction(right.amount) - previous) * 10 <= previous


def _merge_sequential_lanes(lanes, can_chain):
    ordered = sorted(lanes, key=lambda lane: (lane[0].day, lane[0].occurrence.entry_reference))
    while True:
        merged_pair = None
        for i, left in enumerate(ordered):
            next_candidates = [j for j, right in enumerate(ordered) if j != i and can_chain(left[-1], right[0])]
            if len(next_candidates) == 1:
                target = next_candidates[0]
                right = ordered[target]
                prev_candidates = [k for k, other in enumerate(ordered)
                                   if k != target and can_chain(other[-1], right[0])]
                if len(prev_candidates) == 1:
                    merged_pair = (i, target)
                    break
        if merged_pair is None:
            return ordered
        i, target = merged_pair
        combined = ordered[i] + ordered[target]
        ordered = [lane for idx, lane in enumerate(ordered) if idx not in (i, target)]
        ordered.append(combined)
        ordered.sort(key=lambda lane: (lane[0].day, lane[0].occurrence.entry_reference))


def _fixed_fallback_lanes(cluster, raw_gaps, can_chain):
    by_amount = {}
    for item in cluster:
        by_amount.setdefault(item.amount, []).append(item)
    if len(by_amount) == 1:
        return ()
    fixed_lanes = []
    leftover = []
    for charges in by_amount.values():
        for fixed_cluster in _clusters(charges):
            if _is_strict_monthly_lane(fixed_cluster):
                fixed_lanes.append(fixed_cluster)
            else:
                leftover.extend(fixed_cluster)
    if not fixed_lanes:
        return ()
    if leftover:
        recovered = []
        for rem_cluster in _clusters(leftover):
            if _is_strict_monthly_lane(rem_cluster):
                recovered.append(rem_cluster)
                continue
            rem_gaps = [right.day - left.day for left, right in zip(rem_cluster, rem_cluster[1:])]
            sub_lanes = _cadence_lanes(rem_cluster, rem_gaps) if rem_gaps else None
            if sub_lanes is not None:
                recovered.extend(sub_lanes)
            else:
                recovered = None
                break
        if recovered:
            fixed_lanes.extend(recovered)
    merged = _merge_sequential_lanes(fixed_lanes, can_chain)
    if _is_submonthly_rhythm(raw_gaps, merged):
        return ()
    return merged


def _monthly_lanes(items, can_chain=_same_monthly_cadence):
    if len(items) < MIN_OCCURRENCES:
        return
    for cluster in _clusters(items):
        if len(cluster) < MIN_OCCURRENCES:
            continue
        raw_gaps = [right.day - left.day for left, right in zip(cluster, cluster[1:])]
        if min(raw_gaps) >= MIN_GAP_DAYS:
            if len({item.year_month for item in cluster}) >= MIN_OCCURRENCES:
                yield cluster
            continue
        if len(cluster) == MIN_OCCURRENCES or max(raw_gaps) <= 8:
            continue
        lanes = _cadence_lanes(cluster, raw_gaps)
        if lanes is None:
            lanes = _fixed_fallback_lanes(cluster, raw_gaps, can_chain)
        yield from lanes


def _reference_lanes(groups):
    by_reference = defaultdict(list)
    for (account, currency, _, _), items in groups.items():
        for item in items:
            for reference in item.references:
                by_reference[account, currency, reference].append(item)

    # A recurring candidate can coexist with one-off IDs in the title. Require
    # exactly one repeated candidate per charge to use it as reference evidence.
    candidates = {key: items for key, items in by_reference.items()
                  if len({item.year_month for item in items}) >= MIN_OCCURRENCES}
    if not candidates:
        return
    matches = Counter(item.occurrence for items in candidates.values() for item in items)

    # Keep a complete label-based lane together if only part of its history
    # carries a usable reference, including when the latest title omits it.
    deferred = set()
    for items in groups.values():
        matched = {item.occurrence for item in items if matches[item.occurrence] == 1}
        if 0 < len(matched) < len(items):
            for lane in _monthly_lanes(items):
                evidence = {item.occurrence for item in lane}
                if evidence - matched:
                    deferred.update(evidence)

    for (_, currency, _), items in sorted(candidates.items()):
        unambiguous = [item for item in items if matches[item.occurrence] == 1 and item.occurrence not in deferred]
        for lane in _monthly_lanes(unambiguous, _reference_continues):
            segments = [[lane[0]]]
            for previous, current in zip(lane, lane[1:]):
                if not _reference_continues(previous, current):
                    segments.append([])
                segments[-1].append(current)
            established = [segment for segment in segments if len(segment) >= MIN_OCCURRENCES]
            # Defer a lone run with outliers to the label-based detector, which
            # can retain the amount history of an existing variable bill.
            if len(established) > 1 or len(established) == len(segments):
                for segment in established:
                    yield currency, segment


def detect_recurring_charges(owner: str, entries: Iterable[RecurringEntry],
                             selected_accounts: set[str] | frozenset[str], *,
                             max_entries: int = MAX_ENTRIES) -> tuple[RecurringHint, ...]:
    """Return evidence-backed monthly recurring-charge hints, deterministically ordered.

    Only eligible DBIT expense entries on ``selected_accounts`` with non-zero exact
    amounts and non-empty merchant evidence (``counterparty_name`` preferred over
    ``description``) are considered. Entries are separated by account and currency,
    and ordinarily by label source and normalized label. Each hint requires at
    least three monthly occurrences across at least three distinct calendar
    months with consecutive date gaps in [23, 38] days. Within a monthly lane,
    amount changes stay in one history with the latest observed amount used for
    the estimate. Overlapping charges on the same account separate unambiguous
    cadence lanes first, and otherwise retain independently monthly fixed-amount
    histories. A shared reference token can connect changing labels within the
    same scope and monthly cadence when amounts differ by at most ten percent.
    """
    owner, accounts = _scope(owner, selected_accounts, max_entries)
    groups = _groups(owner, entries, accounts, max_entries)
    hints = []
    used = set()
    for currency, lane in _reference_lanes(groups):
        hints.append(_build_hint(currency, lane))
        used.update(item.occurrence for item in lane)
    for (_, currency, _, _), items in sorted(groups.items()):
        remaining = [item for item in items if item.occurrence not in used]
        hints.extend(_build_hint(currency, lane) for lane in _monthly_lanes(remaining))
    hints.sort(key=lambda hint: (hint.currency, hint.normalized_label, hint.label_source,
                                 hint.amount, hint.first_date, hint.last_date, hint.account_ids))
    return tuple(hints)
