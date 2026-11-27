"""Project owner-private upcoming commitments from a single analytics snapshot."""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import cache, cmp_to_key, lru_cache
from itertools import chain
import json
import os
import re
from types import MappingProxyType

from . import analytics_core as core
from .analytics_core import AnalyticsError, MAX_ROWS, canonical_money, compare_money, negate_money, sum_money
from .config import BudgetConfigError
from .loan_references import extract_reference_hashes, mask_loan_references, normalized_loan_label
from .mortgage_reminders import (DEFAULT_STALE_AFTER_DAYS, MortgageInputError, MortgageLimitError,
                                 MortgageSchedule, evaluate_mortgage_reminders, installment_due_date)
from .recurring import RecurringHint, RecurringOccurrence

RULE_VERSION = 2
LAPSED_AFTER_DAYS = 45
MAX_FORECAST_HINTS = 200
MAX_BASELINE_CATEGORIES = 32
MAX_BASELINE_SERIES = 64
# One configured category can conflict as both a recurring hint and a mortgage.
MAX_BASELINE_CONFLICTS = MAX_BASELINE_CATEGORIES * 2
_CONFLICT_REASONS = frozenset({"recurring", "mortgage"})
# Schedules, recurring hints, and one series per configured category and currency.
MAX_FORECAST_ITEMS = (100 + MAX_FORECAST_HINTS + MAX_BASELINE_SERIES) * 12
MAX_FORECAST_LABEL = 256
MAX_STALE_ACCOUNT_IDS = 200
FORECAST_CONFIG_ENV = "HESTIA_BUDGET_FORECAST_CONFIG"
MAX_FORECAST_CONFIG_BYTES = 16 * 1024
DEFAULT_BASELINE_HISTORY_MONTHS = 6
MIN_BASELINE_MONTHS = 3
MAX_BASELINE_HISTORY_MONTHS = 24
_MEAN_EXTRA_DIGITS = 7
_BASELINE_ESTIMATORS = frozenset({"median", "trimmed_mean"})
_CONFIG_ERROR = "Invalid forecast configuration"
_MAX_MATCH_DISTANCE = 5
_INFERRED_MAX_MATCH_DISTANCE = 3
_LOAN_KEYWORDS = re.compile(r"\b(?:RATA\s+KREDYTU|KREDYT|POZYCZKA|LEASING)\b")
_FIRST_MONTH = 1900 * 12
_LAST_MONTH = 9999 * 12 + 11
# Prefer complete Polish and compact forms, then uppercase print forms, before
# the loose spaced form. This keeps ordinary trailing words out of common matches.
_IBAN_LIKE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:[Pp][Ll]\s*)?[0-9](?:\s*[0-9]){25}"
    r"|[A-Za-z]{2}[0-9]{2}[A-Za-z0-9]{11,30}(?!\s+[0-9])"
    r"|[A-Z]{2}\s*[0-9]\s*[0-9](?:\s*[A-Z0-9]){11,30}(?!\s+[A-Za-z0-9]{0,29}[0-9])"
    r"|[A-Za-z]{2}\s*[0-9]\s*[0-9](?:\s*[A-Za-z0-9]){11,30}"
    r")(?![A-Za-z0-9])"
)


class ForecastInputError(ValueError):
    """Invalid query parameters, with no values in the error."""

    def __init__(self):
        super().__init__("Invalid forecast query")


class ForecastDataError(ValueError):
    """Invalid forecast inputs gathered from the snapshot or server clock."""

    def __init__(self):
        super().__init__("Invalid forecast data")


class ForecastLimitError(ValueError):
    """A forecast bound was exceeded before returning any totals."""

    def __init__(self):
        super().__init__("Forecast limit exceeded")


@dataclass(frozen=True, repr=False)
class ForecastBaselineSettings:
    category_ids: tuple[str, ...]
    history_months: int = DEFAULT_BASELINE_HISTORY_MONTHS
    estimator: str = "median"


@dataclass(frozen=True, repr=False)
class CategoryBaseline:
    """One category and currency over complete months inside the account coverage window."""

    category_id: str
    name: str
    currency: str
    monthly_amounts: tuple[str, ...]
    typical_day: int
    current_spend: str
    account_ids: frozenset[str]
    last_observed_date: str


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z", re.ASCII)


def _config_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128 or _CONTROL_RE.search(value)
            or value == "uncategorised"):
        raise BudgetConfigError(_CONFIG_ERROR)
    return value


def _conflict_row(row):
    """One wire conflict. Amounts and any other key are rejected."""
    if not isinstance(row, Mapping):
        raise ForecastDataError()
    category_id = row.get("category_id")
    reason = row.get("reason")
    if set(row) - {"category_id", "reason"}:
        raise ForecastDataError()
    if reason not in _CONFLICT_REASONS:
        raise ForecastDataError()
    if (not isinstance(category_id, str) or not category_id or len(category_id) > 128
            or _CONTROL_RE.search(category_id) or category_id == "uncategorised"):
        raise ForecastDataError()
    return {"category_id": category_id, "reason": reason}


def _conflict_maps(rows):
    """Unique ``{category_id, reason}`` rows, sorted, with no amounts attached."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ForecastDataError()
    unique = {}
    for row in rows:
        item = _conflict_row(row)
        unique.setdefault((item["category_id"], item["reason"]), item)
    return [unique[key] for key in sorted(unique)]


def _named_categories(category_ids, category_names):
    """Validate configured ids against the snapshot. Unknown ids are configuration errors."""
    if (not isinstance(category_ids, Sequence) or isinstance(category_ids, (str, bytes))
            or not isinstance(category_names, Mapping)):
        raise ForecastDataError()
    if len(category_ids) != len(set(category_ids)):
        raise ForecastDataError()
    names = {}
    for category_id in category_ids:
        if not isinstance(category_id, str) or category_id not in category_names:
            raise BudgetConfigError(_CONFIG_ERROR)
        name = category_names[category_id]
        if not isinstance(name, str) or not name.strip() or _CONTROL_RE.search(name):
            raise ForecastDataError()
        names[category_id] = name
    return names


def _unique_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_non_finite(_value):
    raise ValueError


@lru_cache(maxsize=16)
def _parse_forecast_config_text(raw: str) -> Mapping[str, ForecastBaselineSettings]:
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        raise BudgetConfigError(_CONFIG_ERROR) from None
    if len(encoded) > MAX_FORECAST_CONFIG_BYTES:
        raise BudgetConfigError(_CONFIG_ERROR)
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_object_pairs, parse_constant=_reject_non_finite)
    except (ValueError, TypeError, RecursionError):
        raise BudgetConfigError(_CONFIG_ERROR) from None
    if not isinstance(parsed, dict):
        raise BudgetConfigError(_CONFIG_ERROR)
    result = {}
    for owner_key, entry in parsed.items():
        owner = _config_text(owner_key)
        if (not isinstance(entry, dict) or "category_ids" not in entry
                or set(entry) - {"category_ids", "history_months", "estimator"}):
            raise BudgetConfigError(_CONFIG_ERROR)
        history_months = entry.get("history_months", DEFAULT_BASELINE_HISTORY_MONTHS)
        if type(history_months) is not int or not MIN_BASELINE_MONTHS <= history_months <= MAX_BASELINE_HISTORY_MONTHS:
            raise BudgetConfigError(_CONFIG_ERROR)
        estimator = entry.get("estimator", "median")
        if not isinstance(estimator, str) or estimator not in _BASELINE_ESTIMATORS:
            raise BudgetConfigError(_CONFIG_ERROR)
        raw_ids = entry["category_ids"]
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > MAX_BASELINE_CATEGORIES:
            raise BudgetConfigError(_CONFIG_ERROR)
        category_ids, seen = [], set()
        for item in raw_ids:
            category_id = _config_text(item)
            if category_id in seen:
                raise BudgetConfigError(_CONFIG_ERROR)
            seen.add(category_id)
            category_ids.append(category_id)
        result[owner] = ForecastBaselineSettings(tuple(category_ids), history_months, estimator)
    return MappingProxyType(result)


def load_forecast_config(environ: Mapping[str, str] | None = None) -> Mapping[str, ForecastBaselineSettings] | None:
    """Read optional server-only ``HESTIA_BUDGET_FORECAST_CONFIG`` (bounded to 16 KiB).

    Absent or empty configuration selects no categories, so the forecast stays on
    mortgage schedules and recurring hints alone.
    """
    env = os.environ if environ is None else environ
    raw = env.get(FORECAST_CONFIG_ENV)
    if raw is None or raw == "":
        return None
    return _parse_forecast_config_text(raw)


def _parts(value):
    try:
        return core._parts(value)
    except AnalyticsError:
        raise ForecastDataError() from None


def _mean_money(values):
    """Exact mean. Non-terminating quotients round half away from zero after 7 extra digits.

    Negative totals use the same division on the absolute value, then restore the sign.
    """
    if not values:
        raise ForecastDataError()
    total, scale = 0, 0
    for value in values:
        coefficient, current = _parts(value)
        if current > scale:
            total *= 10 ** (current - scale)
            scale = current
        total += coefficient * 10 ** (scale - current)
    divisor = len(values)
    negative = total < 0
    coefficient, extra = abs(total), 0
    while coefficient % divisor and extra < _MEAN_EXTRA_DIGITS:
        coefficient *= 10
        extra += 1
    quotient, remainder = divmod(coefficient, divisor)
    if remainder * 2 >= divisor:
        quotient += 1
    if negative:
        quotient = -quotient
    return core._format(quotient, scale + extra)


def _ordered_amounts(values):
    return sorted(values, key=cmp_to_key(compare_money))


def _median_money(values):
    ordered = _ordered_amounts(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return canonical_money(ordered[middle])
    return _mean_money(ordered[middle - 1:middle + 1])


def _trimmed_mean_money(values):
    """Drop the lowest and highest month, then take the mean of what remains."""
    return _mean_money(_ordered_amounts(values)[1:-1])


def _baseline_estimate(amounts, estimator):
    if len(amounts) < MIN_BASELINE_MONTHS:
        return None
    if estimator == "median":
        return _median_money(amounts)
    if estimator == "trimmed_mean":
        return _trimmed_mean_money(amounts)
    raise ForecastDataError()


def _sub_money(total, amount):
    try:
        if compare_money(amount, "0") < 0 or compare_money(total, "0") < 0:
            raise ForecastDataError()
        if compare_money(total, amount) <= 0:
            applied = total if compare_money(total, "0") > 0 else "0"
            return "0", applied
        return sum_money((total, negate_money(amount))), amount
    except AnalyticsError:
        raise ForecastDataError() from None


def _iso_day(value):
    if not isinstance(value, str):
        raise ForecastDataError()
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ForecastDataError() from None
    if parsed.isoformat() != value:
        raise ForecastDataError()
    return parsed


def _previous_months(evaluation, count):
    start = _month_number(evaluation)
    first = max(_FIRST_MONTH, start - count)
    return tuple(_month_name(number) for number in range(first, start))


def _hint_identity(hint):
    try:
        amount = canonical_money(hint.amount)
    except AnalyticsError:
        raise ForecastDataError() from None
    return (hint.currency, hint.normalized_label, hint.label_source, amount,
            hint.first_date, hint.last_date, hint.account_ids)


def _evidence_key(item):
    if not isinstance(item, RecurringOccurrence):
        raise ForecastDataError()
    return (item.account_id, item.entry_reference)


def expenses_from_transactions(transactions):
    """Snapshot rows → expense tuples, refund tuples, evidence categories and each account's first date.

    Refunds are credits (``CRDT``), matching analytics net spending. Any dated row sets the account's
    coverage start, including income and transfers. Evidence is recorded for every row.
    """
    if not isinstance(transactions, Sequence) or isinstance(transactions, (str, bytes)):
        raise ForecastDataError()
    if len(transactions) > MAX_ROWS:
        raise ForecastLimitError()
    expenses, refunds, evidence, first_dates = [], [], {}, {}
    for row in transactions:
        if not isinstance(row, Mapping):
            raise ForecastDataError()
        payload, effective = row.get("payload"), row.get("effective")
        account_id, entry_reference = row.get("account_id"), row.get("entry_reference")
        if (not isinstance(payload, Mapping) or not isinstance(effective, Mapping)
                or not isinstance(account_id, str) or not isinstance(entry_reference, str)):
            raise ForecastDataError()
        category_id = effective.get("category_id")
        if category_id is not None and not isinstance(category_id, str):
            raise ForecastDataError()
        evidence[(account_id, entry_reference)] = category_id
        try:
            iso = core.effective_date(payload)
        except AnalyticsError:
            raise ForecastDataError() from None
        if iso is not None:
            previous = first_dates.get(account_id)
            if previous is None or iso < previous:
                first_dates[account_id] = iso
        if iso is None or category_id is None:
            continue
        kind = effective.get("kind")
        indicator = payload.get("credit_debit_indicator")
        if kind == "expense" and indicator == "DBIT":
            expenses.append((category_id, payload.get("currency"), iso, payload.get("amount"), account_id))
        elif kind == "refund" and indicator == "CRDT":
            refunds.append((category_id, payload.get("currency"), iso, payload.get("amount"), account_id))
    return tuple(expenses), tuple(refunds), evidence, first_dates


def _unpack_flow(item):
    if not isinstance(item, tuple) or len(item) not in (4, 5):
        raise ForecastDataError()
    if len(item) == 4:
        return (*item, "")
    return item


def _normalize_flow(category_id, currency, iso_date, amount, account_id):
    if not isinstance(category_id, str) or not category_id or len(category_id) > 128 or _CONTROL_RE.search(category_id):
        raise ForecastDataError()
    if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        raise ForecastDataError()
    if not isinstance(account_id, str) or len(account_id) > 128 or (account_id and _CONTROL_RE.search(account_id)):
        raise ForecastDataError()
    parsed = _iso_day(iso_date)
    try:
        canonical = canonical_money(amount)
    except AnalyticsError:
        raise ForecastDataError() from None
    if compare_money(canonical, "0") <= 0:
        return None
    return category_id, currency, iso_date, canonical, parsed.day, account_id


def _net_money(expenses, refunds):
    total = sum_money(expenses) if expenses else "0"
    if refunds:
        total = sum_money((total, negate_money(sum_money(refunds))))
    return total


def _hint_categories(hints, evidence_categories, allowed):
    if (not isinstance(hints, Sequence) or isinstance(hints, (str, bytes))
            or not isinstance(evidence_categories, Mapping)):
        raise ForecastDataError()
    assigned = {}
    for hint in hints:
        if not isinstance(hint, RecurringHint):
            raise ForecastDataError()
        if not hint.evidence:
            continue
        found = set()
        for item in hint.evidence:
            key = _evidence_key(item)
            if key not in evidence_categories:
                continue
            found.add(evidence_categories[key])
        overlap = found & allowed
        if overlap:
            assigned[_hint_identity(hint)] = frozenset(overlap)
    return assigned


def _schedule_categories(schedules, expenses, allowed):
    if not isinstance(schedules, Sequence) or isinstance(schedules, (str, bytes)):
        raise ForecastDataError()
    buckets = defaultdict(list)
    for category_id, currency, iso_date, amount, _day in expenses:
        buckets[(currency, amount)].append((category_id, iso_date))
    assigned = {}
    for schedule in schedules:
        if not isinstance(schedule, MortgageSchedule):
            raise ForecastDataError()
        if not schedule.enabled:
            continue
        try:
            amount = canonical_money(schedule.amount)
        except AnalyticsError:
            raise ForecastDataError() from None
        hits = set()
        for category_id, iso_date in buckets.get((schedule.currency, amount), ()):
            if _schedule_debit_matches(schedule, iso_date):
                hits.add(category_id)
        overlap = hits & allowed
        if overlap:
            assigned[schedule.schedule_id] = (frozenset(overlap), schedule.currency)
    return assigned


def _schedule_debit_matches(schedule, iso_date):
    """True when the debit is within five days of a due date in this month or either neighbour.

    The neighbour months match ``_deduplicate``: a due day of 1 paid on the 28th–31st of the
    previous month is the same instalment.
    """
    observed = _iso_day(iso_date)
    center = _month_number(observed)
    for month_number in range(max(_FIRST_MONTH, center - 1), min(_LAST_MONTH, center + 1) + 1):
        try:
            due = installment_due_date(
                schedule.due_day, schedule.start_date, schedule.end_date, _month_name(month_number))
        except MortgageInputError:
            raise ForecastDataError() from None
        if due is None:
            continue
        if abs((observed - date.fromisoformat(due)).days) <= _MAX_MATCH_DISTANCE:
            return True
    return False


def _account_start(account_id, flow_first, account_first_dates):
    candidates = []
    for source in (flow_first, account_first_dates):
        value = source.get(account_id)
        if value is not None:
            candidates.append(value)
    return min(candidates) if candidates else None


def _covered_months(accounts, flow_first, account_first_dates, history):
    """History months from each contributing account's first dated row through the month before evaluation."""
    covered = []
    for month in history:
        for account_id in accounts:
            start = _account_start(account_id, flow_first, account_first_dates)
            if start is not None and start[:7] <= month:
                covered.append(month)
                break
    return covered


def build_category_baselines(*, category_ids: Sequence[str], category_names: Mapping[str, str], history_months: int,
                             evaluation_date: str, expenses: Sequence[tuple], refunds: Sequence[tuple] = (),
                             account_first_dates: Mapping[str, str] | None = None,
                             ) -> tuple[CategoryBaseline, ...]:
    """Aggregate configured categories over the last ``history_months`` complete months.

    A month inside an account's coverage window counts as zero when that account posted no net in it.
    Coverage starts at the account's earliest dated row and runs through the month before evaluation,
    clipped to the history window. Net spending is expenses minus refunds and may be negative.
    Months before the account's first dated row are omitted. The series cap is applied later, after
    the request currency filter.
    """
    if type(history_months) is not int or not MIN_BASELINE_MONTHS <= history_months <= MAX_BASELINE_HISTORY_MONTHS:
        raise ForecastDataError()
    names = _named_categories(category_ids, category_names)
    evaluation = _iso_day(evaluation_date)
    allowed = set(names)
    if (not isinstance(expenses, Sequence) or isinstance(expenses, (str, bytes))
            or not isinstance(refunds, Sequence) or isinstance(refunds, (str, bytes))):
        raise ForecastDataError()
    if len(expenses) > MAX_ROWS or len(refunds) > MAX_ROWS or len(expenses) + len(refunds) > MAX_ROWS:
        raise ForecastLimitError()
    if account_first_dates is None:
        explicit_first = {}
    elif not isinstance(account_first_dates, Mapping):
        raise ForecastDataError()
    else:
        explicit_first = {}
        for account_id, iso in account_first_dates.items():
            if not isinstance(account_id, str):
                raise ForecastDataError()
            explicit_first[account_id] = _iso_day(iso).isoformat()
    history = _previous_months(evaluation, history_months)
    history_set = set(history)
    evaluation_month = evaluation_date[:7]
    by_month = defaultdict(lambda: defaultdict(lambda: {"expense": [], "refund": [], "days": [], "dates": []}))
    current = defaultdict(lambda: {"expense": [], "refund": []})
    series_accounts = defaultdict(set)
    flow_first = {}

    def _scan(items, kind):
        for item in items:
            normalized = _normalize_flow(*_unpack_flow(item))
            if normalized is None:
                continue
            category_id, currency, iso_date, amount, day, account_id = normalized
            previous = flow_first.get(account_id)
            if previous is None or iso_date < previous:
                flow_first[account_id] = iso_date
            if category_id not in allowed:
                continue
            key = (category_id, currency)
            series_accounts[key].add(account_id)
            month = iso_date[:7]
            if month == evaluation_month:
                current[key][kind].append(amount)
            elif month in history_set:
                bucket = by_month[key][month]
                bucket[kind].append(amount)
                if kind == "expense":
                    bucket["days"].append(day)
                bucket["dates"].append(iso_date)

    _scan(expenses, "expense")
    _scan(refunds, "refund")
    baselines = []
    for category_id, currency in sorted(series_accounts):
        key = (category_id, currency)
        covered = _covered_months(series_accounts[key], flow_first, explicit_first, history)
        if len(covered) < MIN_BASELINE_MONTHS:
            continue
        amounts, days, observed = [], [], []
        months = by_month[key]
        for month in covered:
            bucket = months.get(month)
            if bucket is None:
                amounts.append("0")
                continue
            amounts.append(_net_money(bucket["expense"], bucket["refund"]))
            days.extend(bucket["days"])
            observed.extend(bucket["dates"])
        if not days:
            continue
        spent = current[key]
        baselines.append(CategoryBaseline(
            category_id=category_id, name=names[category_id], currency=currency,
            monthly_amounts=tuple(amounts), typical_day=sorted(days)[(len(days) - 1) // 2],
            current_spend=_net_money(spent["expense"], spent["refund"]),
            account_ids=frozenset(series_accounts[key]),
            last_observed_date=max(observed),
        ))
    return tuple(baselines)


def _baseline_conflicts(*, category_ids, hints, schedules, expenses, evidence_categories,
                        evaluation_date, account_timestamps):
    """Return ``(conflicts, blocked_ids)`` for configured categories an active hint or mortgage already covers.

    Conflicts are ``{category_id, reason}`` only. ``reason`` is ``recurring`` or ``mortgage``. A category
    in both sets produces both rows. Nothing here is emitted as a baseline: callers drop every id in
    ``blocked_ids``, including ids omitted when the wire list is later capped.

    Hints and mortgages have no category column. A hint conflicts when it is active and any mapped
    evidence row is in a configured category, including a partial overlap such as five of six
    occurrences. A mortgage conflicts when it is enabled and any same-currency debit of the instalment
    amount falls in a configured category within five days of a due date in that debit's month or the
    month on either side. Evidence with no category mapping, and a mortgage with no such debit, do not
    conflict. Stale hints stay active. Lapsed hints and disabled schedules do not conflict. The check
    ignores the request currency. Matching rows are not subtracted from the baseline.
    """
    if not isinstance(account_timestamps, Mapping):
        raise ForecastDataError()
    evaluation = _iso_day(evaluation_date)
    _latest, stale_accounts = _freshness(account_timestamps, evaluation)
    if (not isinstance(hints, Sequence) or isinstance(hints, (str, bytes))
            or not isinstance(evidence_categories, Mapping)):
        raise ForecastDataError()
    active = []
    for hint in hints:
        if not isinstance(hint, RecurringHint):
            raise ForecastDataError()
        try:
            last_date = date.fromisoformat(hint.last_date)
            stale = any(account in stale_accounts for account in hint.account_ids)
        except (ValueError, TypeError):
            raise ForecastDataError() from None
        if not stale and (evaluation - last_date).days > LAPSED_AFTER_DAYS:
            continue
        active.append(hint)
    allowed = set(category_ids)
    recurring = set()
    for overlap in _hint_categories(active, evidence_categories, allowed).values():
        recurring.update(overlap)
    if not isinstance(expenses, Sequence) or isinstance(expenses, (str, bytes)):
        raise ForecastDataError()
    normalized = []
    for item in expenses:
        flow = _normalize_flow(*_unpack_flow(item))
        if flow is not None:
            category_id, currency, iso_date, amount, day, _account_id = flow
            normalized.append((category_id, currency, iso_date, amount, day))
    mortgage = set()
    for overlap, _currency in _schedule_categories(schedules, normalized, allowed).values():
        mortgage.update(overlap)
    rows = []
    for category_id in category_ids:
        if category_id in recurring:
            rows.append({"category_id": category_id, "reason": "recurring"})
        if category_id in mortgage:
            rows.append({"category_id": category_id, "reason": "mortgage"})
    return _conflict_maps(rows), frozenset(recurring | mortgage)


def baselines_for_snapshot(snapshot, evaluation_date, hints, schedules, *, account_timestamps=None, environ=None):
    """Return ``(baselines, estimator, conflicts)``. Missing config yields no baselines and does not read the snapshot.

    A configured owner requires ``account_timestamps`` so lapsed hints can be told from active ones.
    A category that already has an active hint or an enabled mortgage is omitted from ``baselines`` and
    listed in ``conflicts``. Unknown category ids still raise ``BudgetConfigError``.
    """
    settings = load_forecast_config(environ)
    if not settings:
        return (), "median", ()
    if not isinstance(snapshot, Mapping):
        raise ForecastDataError()
    owner = snapshot.get("owner")
    owner_settings = settings.get(owner) if isinstance(owner, str) else None
    if owner_settings is None:
        return (), "median", ()
    categories = snapshot.get("categories")
    if not isinstance(categories, Sequence) or isinstance(categories, (str, bytes)):
        raise ForecastDataError()
    names = {}
    for row in categories:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise ForecastDataError()
        names[row["id"]] = row.get("name")
    if not isinstance(account_timestamps, Mapping):
        raise ForecastDataError()
    expenses, refunds, evidence, first_dates = expenses_from_transactions(snapshot.get("transactions"))
    _named_categories(owner_settings.category_ids, names)
    conflicts, blocked = _baseline_conflicts(
        category_ids=owner_settings.category_ids, hints=hints, schedules=schedules, expenses=expenses,
        evidence_categories=evidence, evaluation_date=evaluation_date, account_timestamps=account_timestamps,
    )
    clean = [category_id for category_id in owner_settings.category_ids if category_id not in blocked]
    return build_category_baselines(
        category_ids=clean, category_names=names, history_months=owner_settings.history_months,
        evaluation_date=evaluation_date, expenses=expenses, refunds=refunds, account_first_dates=first_dates,
    ), owner_settings.estimator, tuple(conflicts)


def _project_category_baselines(projected, horizon, baselines, estimator, evaluation_month, stale_accounts):
    ordered = sorted(baselines, key=lambda item: (item.currency, item.name, item.category_id))
    for baseline in ordered:
        if (not isinstance(baseline.name, str) or not _CURRENCY_RE.fullmatch(baseline.currency)
                or type(baseline.typical_day) is not int or not 1 <= baseline.typical_day <= 31
                or not isinstance(baseline.account_ids, frozenset)):
            raise ForecastDataError()
        estimate = _baseline_estimate(baseline.monthly_amounts, estimator)
        if estimate is None or compare_money(estimate, "0") <= 0:
            continue
        try:
            current_spend = canonical_money(baseline.current_spend)
        except AnalyticsError:
            raise ForecastDataError() from None
        spent = current_spend if compare_money(current_spend, "0") > 0 else "0"
        account_stale = any(account_id in stale_accounts for account_id in baseline.account_ids)
        for month, month_items in zip(horizon, projected):
            remaining = estimate
            if month == evaluation_month and compare_money(spent, "0") > 0:
                remaining, _applied = _sub_money(remaining, spent)
            if compare_money(remaining, "0") <= 0:
                continue
            year, month_number = int(month[:4]), int(month[5:7])
            last_day = calendar.monthrange(year, month_number)[1]
            expected = date(year, month_number, min(baseline.typical_day, last_day)).isoformat()
            month_items.append(({
                "source": "category_baseline", "schedule_id": None, "label": baseline.name,
                "amount": remaining, "currency": baseline.currency, "expected_date": expected,
                "confidence": "estimated", "past_due": False, "account_stale": account_stale,
            }, baseline.category_id))


@dataclass(frozen=True, repr=False)
class _ActiveHint:
    hint: RecurringHint
    key: tuple[str, str, str, str, str, str, tuple[str, ...]]
    amount: str
    last_date: date
    account_stale: bool


@dataclass(frozen=True, repr=False)
class _DeduplicationMatch:
    schedule: MortgageSchedule
    active: _ActiveHint
    due: str
    distance: int
    match_type: str

    @property
    def installment(self):
        return self.schedule.schedule_id, self.due

    def audit(self):
        row = {
            "schedule_id": self.schedule.schedule_id, "schedule_label": self.schedule.label,
            "recurring_label": self.active.hint.label, "amount": self.active.amount, "currency": self.schedule.currency,
            "matched_due_date": self.due, "last_observed_date": self.active.hint.last_date,
            "day_distance": self.distance, "account_stale": self.active.account_stale,
        }
        if self.match_type != "exact":
            row.update(match_type=self.match_type, amount_delta=_amount_delta(self.active.amount, self.schedule.amount))
        return row


def validate_forecast_query(months: str | None, currency: str | None) -> tuple[int, str | None]:
    """Validate the optional values of the forecast's two query parameters."""
    horizon = 3
    if months is not None:
        if not isinstance(months, str) or not re.fullmatch(r"[0-9]{1,2}", months) or not 1 <= int(months) <= 12:
            raise ForecastInputError()
        horizon = int(months)
    if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
        raise ForecastInputError()
    return horizon, currency


def _month_number(day: date) -> int:
    return day.year * 12 + day.month - 1


def _month_name(month_number: int) -> str:
    year, offset = divmod(month_number, 12)
    return f"{year:04d}-{offset + 1:02d}"


def _freshness(account_timestamps, evaluation):
    latest = None
    stale = set()
    for account_id, timestamps in account_timestamps.items():
        try:
            timestamp = max((stamp for stamp in timestamps if stamp is not None), default=None)
            if timestamp is None:
                stale.add(account_id)
                continue
            as_of = min(datetime.fromtimestamp(timestamp, timezone.utc).date(), evaluation)
        except (ValueError, OverflowError, OSError, TypeError):
            raise ForecastDataError() from None
        if (evaluation - as_of).days > DEFAULT_STALE_AFTER_DAYS:
            stale.add(account_id)
        latest = as_of if latest is None else max(latest, as_of)
    return latest.isoformat() if latest is not None else None, stale


def _partition_hints(hints, evaluation, stale_accounts):
    active = []
    lapsed_count = stale_count = 0
    for hint in hints:
        try:
            last_date = date.fromisoformat(hint.last_date)
        except (ValueError, TypeError):
            raise ForecastDataError() from None
        account_stale = any(account in stale_accounts for account in hint.account_ids)
        if account_stale:
            stale_count += 1
        elif (evaluation - last_date).days > LAPSED_AFTER_DAYS:
            lapsed_count += 1
            continue
        if len(active) >= MAX_FORECAST_HINTS:
            raise ForecastLimitError()
        amount = canonical_money(hint.amount)
        key = (hint.currency, hint.normalized_label, hint.label_source, amount,
               hint.first_date, hint.last_date, hint.account_ids)
        active.append(_ActiveHint(hint, key, amount, last_date, account_stale))
    return active, lapsed_count, stale_count


def _due_date(schedule, month):
    return installment_due_date(schedule.due_day, schedule.start_date, schedule.end_date, month)


def _amount_delta(hint_amount, schedule_amount):
    return sum_money((hint_amount, negate_money(schedule_amount)))


def _within_inferred_amount(hint_amount, schedule_amount):
    coefficient, scale = _parts(schedule_amount)
    tolerance = core._format(coefficient, scale + 1)
    return compare_money(_amount_delta(hint_amount, schedule_amount).removeprefix("-"), tolerance) <= 0


def _deduplicate(schedules, active_hints):
    schedule_references = {schedule.schedule_id: {reference["hash"] for reference in schedule.match_references or ()}
                           for schedule in schedules}
    linked = defaultdict(list)
    inferred = []
    exact = []
    for active in active_hints:
        label = active.hint.normalized_label
        hint_references = extract_reference_hashes(active.hint.label)
        loan_label = _LOAN_KEYWORDS.search(normalized_loan_label(label))
        last_month = _month_number(active.last_date)
        first_month = max(_FIRST_MONTH, last_month - 1)
        last_candidate_month = min(_LAST_MONTH, last_month + 1)
        candidate_months = [_month_name(number) for number in range(first_month, last_candidate_month + 1)]
        for schedule in schedules:
            if schedule.currency != active.hint.currency:
                continue
            references = schedule_references[schedule.schedule_id]
            max_distance = _MAX_MATCH_DISTANCE
            if references:
                if references.isdisjoint(hint_references):
                    continue
                match_type = "linked"
            elif loan_label:
                if not _within_inferred_amount(active.amount, schedule.amount):
                    continue
                match_type = "inferred"
                max_distance = _INFERRED_MAX_MATCH_DISTANCE
            else:
                if compare_money(active.amount, schedule.amount) != 0:
                    continue
                match_type = "exact"
            for month in candidate_months:
                due = _due_date(schedule, month)
                if due is None:
                    continue
                distance = abs((active.last_date - date.fromisoformat(due)).days)
                if distance > max_distance:
                    continue
                match = _DeduplicationMatch(schedule, active, due, distance, match_type)
                if match_type == "linked":
                    linked[active.key].append(match)
                elif match_type == "inferred":
                    inferred.append(match)
                else:
                    exact.append(match)
                break

    # A reference can cover several debits on one schedule. Two schedules claiming
    # the same debit leave it visible, without falling back to inference.
    matches = [candidates[0] for candidates in linked.values() if len(candidates) == 1]
    inferred = [match for match in inferred if match.active.key not in linked]
    inferred_by_hint = Counter(match.active.key for match in inferred)
    inferred_by_schedule = Counter(match.schedule.schedule_id for match in inferred)
    matches.extend(match for match in inferred
                   if inferred_by_hint[match.active.key] == inferred_by_schedule[match.schedule.schedule_id] == 1)

    used_installments = {match.installment for match in matches}
    used_hints = {match.active.key for match in matches}
    for match in sorted(exact, key=lambda item: (item.distance, item.due, *item.active.key, item.schedule.schedule_id)):
        if match.installment in used_installments or match.active.key in used_hints:
            # An occupied instalment leaves its competing hint eligible for a later edge.
            continue
        used_installments.add(match.installment)
        used_hints.add(match.active.key)
        matches.append(match)
    return [match.audit() for match in matches], [active for active in active_hints if active.key not in used_hints]


def _project_month(month, evaluation_date, schedules, hints):
    items = []
    for schedule in schedules:
        if month in schedule.paid_months:
            continue
        due = _due_date(schedule, month)
        if due is None:
            continue
        items.append(({
            "source": "mortgage", "schedule_id": schedule.schedule_id, "label": schedule.label,
            "amount": canonical_money(schedule.amount), "currency": schedule.currency, "expected_date": due,
            "confidence": "scheduled", "past_due": due < evaluation_date, "account_stale": False,
        }, ""))

    year, month_number = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, month_number)[1]
    for active in hints:
        hint = active.hint
        if month <= hint.last_date[:7]:
            continue
        expected = date(year, month_number, min(active.last_date.day, last_day)).isoformat()
        high_confidence = hint.occurrence_count >= 6 and hint.max_gap_days - hint.min_gap_days <= 7
        tie_breaker = (hint.normalized_label, hint.label_source, hint.first_date, hint.last_date, hint.account_ids)
        items.append(({
            "source": "recurring", "schedule_id": None, "label": hint.label, "amount": active.amount,
            "currency": hint.currency, "expected_date": expected, "confidence": "high" if high_confidence else "medium",
            "past_due": False, "account_stale": active.account_stale,
        }, tie_breaker))
    return items


def _mask_iban(match):
    compact = "".join(match[0].split())
    country = compact[:2].upper() if compact[0].isalpha() else ""
    return country + "••" + compact[-4:]


def _bounded_label(label):
    label = mask_loan_references(_IBAN_LIKE.sub(_mask_iban, label))
    if len(label) > MAX_FORECAST_LABEL:
        return label[:MAX_FORECAST_LABEL - 1].rstrip() + "…"
    return label


def _item_sort_key(item_and_tie_breaker):
    item, tie_breaker = item_and_tie_breaker
    return (item["expected_date"], item["currency"], item["source"], item["label"], item["schedule_id"] or "",
            item["amount"], tie_breaker)


def _currency_totals(items: Iterable[dict]) -> list[dict]:
    amounts_by_currency = defaultdict(lambda: ([], []))
    for item in items:
        scheduled, estimated = amounts_by_currency[item["currency"]]
        amounts = scheduled if item["source"] == "mortgage" else estimated
        amounts.append(item["amount"])
    totals = []
    for currency, (scheduled, estimated) in sorted(amounts_by_currency.items()):
        scheduled_total = sum_money(scheduled)
        estimated_total = sum_money(estimated)
        totals.append({"currency": currency, "total": sum_money((scheduled_total, estimated_total)),
                       "scheduled_total": scheduled_total, "estimated_total": estimated_total})
    return totals


def evaluate_forecast(*, owner: str, evaluation_date: str, months: int, currency: str | None,
                      account_timestamps: Mapping[str, tuple[int | float | None, int | float | None]],
                      excluded_accounts: int, schedules: Sequence[MortgageSchedule], hints: Sequence[RecurringHint],
                      snapshot_digest: str, schedules_version: str,
                      baselines: Sequence[CategoryBaseline] = (), baseline_estimator: str = "median",
                      baseline_expenses: Sequence[tuple] = (),
                      baseline_evidence: Mapping = MappingProxyType({}),
                      baseline_conflicts: Sequence[Mapping] = ()) -> dict:
    """Evaluate validated horizon and currency values against gathered owner data."""
    try:
        reminders = evaluate_mortgage_reminders(owner, evaluation_date, schedules)
    except MortgageInputError:
        raise ForecastDataError() from None
    except MortgageLimitError:
        raise ForecastLimitError() from None
    evaluation = date.fromisoformat(evaluation_date)
    start = _month_number(evaluation)
    if start + months - 1 > _LAST_MONTH:
        raise ForecastDataError()
    horizon = [_month_name(number) for number in range(start, start + months)]
    m0 = horizon[0]

    ledger_as_of, stale_accounts = _freshness(account_timestamps, evaluation)
    enabled = [schedule for schedule in schedules if schedule.enabled and (currency is None or schedule.currency == currency)]
    filtered_hints = hints if currency is None else (hint for hint in hints if hint.currency == currency)
    active, lapsed_count, stale_count = _partition_hints(filtered_hints, evaluation, stale_accounts)
    match_audits, remaining = _deduplicate(enabled, active)
    projected = [_project_month(month, evaluation_date, enabled, remaining) for month in horizon]
    if not isinstance(baselines, Sequence) or isinstance(baselines, (str, bytes)):
        raise ForecastDataError()
    for item in baselines:
        if not isinstance(item, CategoryBaseline):
            raise ForecastDataError()
    if baselines and (not isinstance(baseline_estimator, str) or baseline_estimator not in _BASELINE_ESTIMATORS):
        raise ForecastDataError()
    supplied = _conflict_maps(baseline_conflicts)
    blocked = {item["category_id"] for item in supplied}
    detected = []
    if baselines:
        detected, detected_blocked = _baseline_conflicts(
            category_ids=tuple(dict.fromkeys(item.category_id for item in baselines)), hints=hints,
            schedules=schedules, expenses=baseline_expenses, evidence_categories=baseline_evidence,
            evaluation_date=evaluation_date, account_timestamps=account_timestamps,
        )
        blocked.update(detected_blocked)
    # Cap the note only. Every blocked category stays out of the estimate, including ones past the cap.
    reported_conflicts = _conflict_maps([*supplied, *detected])[:MAX_BASELINE_CONFLICTS]
    selected_baselines = [item for item in baselines
                          if item.category_id not in blocked and (currency is None or item.currency == currency)]
    if len(selected_baselines) > MAX_BASELINE_SERIES:
        raise ForecastLimitError()
    _project_category_baselines(projected, horizon, selected_baselines, baseline_estimator, m0, stale_accounts)
    audits = match_audits
    overdue_schedules = sum(reminder.due_date is not None and reminder.due_date[:7] < m0
                            and (currency is None or reminder.currency == currency) for reminder in reminders)
    if sum(map(len, projected)) > MAX_FORECAST_ITEMS:
        raise ForecastLimitError()

    # Month rows repeat labels. Scan each original only once in this evaluation.
    display_label = cache(_bounded_label)
    month_rows = []
    for month, items in zip(horizon, projected):
        for item, _ in items:
            item["label"] = display_label(item["label"])
        items.sort(key=_item_sort_key)
        wire_items = [item for item, _ in items]
        month_rows.append({"month": month, "currencies": _currency_totals(wire_items), "items": wire_items})
    for audit in audits:
        audit["schedule_label"] = display_label(audit["schedule_label"])
        audit["recurring_label"] = display_label(audit["recurring_label"])
    audits.sort(key=lambda audit: (audit["currency"], audit["matched_due_date"], audit["schedule_id"],
                                   audit["recurring_label"], audit["amount"], audit["last_observed_date"],
                                   audit["account_stale"]))

    return {
        "evaluation_date": evaluation_date, "evaluation_timezone": "UTC", "horizon_months": months,
        "start_month": m0, "end_month": horizon[-1], "ledger_as_of": ledger_as_of,
        "freshness_stale": not account_timestamps or bool(stale_accounts),
        "stale_account_ids": sorted(stale_accounts)[:MAX_STALE_ACCOUNT_IDS],
        "stale_account_ids_truncated": len(stale_accounts) > MAX_STALE_ACCOUNT_IDS,
        "snapshot_digest": snapshot_digest, "schedules_version": schedules_version, "rule_version": RULE_VERSION,
        "summary": {"selected_accounts": len(account_timestamps), "excluded_accounts": excluded_accounts,
                    "stale_accounts": len(stale_accounts), "overdue_mortgage_schedule_count": overdue_schedules,
                    "lapsed_recurring_count": lapsed_count, "deduped_recurring_count": len(match_audits),
                    "stale_recurring_count": stale_count, "baseline_conflicts": reported_conflicts},
        "totals": _currency_totals(chain.from_iterable(month["items"] for month in month_rows)),
        "deduplications": audits, "months": month_rows,
    }
