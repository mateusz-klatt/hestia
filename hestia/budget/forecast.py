"""Project owner-private upcoming commitments from a single analytics snapshot."""
from __future__ import annotations

import calendar
from collections import defaultdict
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
from .mortgage_reminders import (DEFAULT_STALE_AFTER_DAYS, MortgageInputError, MortgageLimitError,
                                 MortgageSchedule, evaluate_mortgage_reminders, installment_due_date)
from .recurring import RecurringHint, RecurringOccurrence

RULE_VERSION = 1
LAPSED_AFTER_DAYS = 45
MAX_FORECAST_HINTS = 200
MAX_BASELINE_CATEGORIES = 32
MAX_BASELINE_SERIES = 64
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
    """One category and currency, already limited to complete months that had expenses."""

    category_id: str
    name: str
    currency: str
    monthly_amounts: tuple[str, ...]
    typical_day: int
    current_spend: str
    recurring_keys: frozenset[tuple[str, str, str, str, str, str]]
    schedule_ids: frozenset[str]
    last_observed_date: str


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z", re.ASCII)


def _config_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128 or _CONTROL_RE.search(value)
            or value == "uncategorised"):
        raise BudgetConfigError(_CONFIG_ERROR)
    return value


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
        if estimator not in _BASELINE_ESTIMATORS:
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
    """Exact mean. Non-terminating quotients round half away from zero after 7 extra digits."""
    if not values:
        raise ForecastDataError()
    total, scale = 0, 0
    for value in values:
        coefficient, current = _parts(value)
        if coefficient < 0:
            raise ForecastDataError()
        if current > scale:
            total *= 10 ** (current - scale)
            scale = current
        total += coefficient * 10 ** (scale - current)
    divisor = len(values)
    coefficient, extra = total, 0
    while coefficient % divisor and extra < _MEAN_EXTRA_DIGITS:
        coefficient *= 10
        extra += 1
    quotient, remainder = divmod(coefficient, divisor)
    if remainder * 2 >= divisor:
        quotient += 1
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
    return (hint.currency, hint.normalized_label, hint.label_source, amount, hint.first_date, hint.last_date)


def _evidence_key(item):
    if not isinstance(item, RecurringOccurrence):
        raise ForecastDataError()
    return (item.account_id, item.entry_reference)


def expenses_from_transactions(transactions):
    """Eligible snapshot rows → expense tuples and evidence categories, including rule results."""
    if not isinstance(transactions, Sequence) or isinstance(transactions, (str, bytes)):
        raise ForecastDataError()
    if len(transactions) > MAX_ROWS:
        raise ForecastLimitError()
    expenses, evidence = [], {}
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
        if effective.get("kind") != "expense" or payload.get("credit_debit_indicator") != "DBIT" or category_id is None:
            continue
        try:
            iso = core.effective_date(payload)
        except AnalyticsError:
            raise ForecastDataError() from None
        if iso is None:
            continue
        expenses.append((category_id, payload.get("currency"), iso, payload.get("amount")))
    return tuple(expenses), evidence


def _normalize_expense(category_id, currency, iso_date, amount):
    if not isinstance(category_id, str) or not category_id or len(category_id) > 128 or _CONTROL_RE.search(category_id):
        raise ForecastDataError()
    if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
        raise ForecastDataError()
    parsed = _iso_day(iso_date)
    try:
        canonical = canonical_money(amount)
    except AnalyticsError:
        raise ForecastDataError() from None
    if compare_money(canonical, "0") <= 0:
        return None
    return category_id, currency, iso_date, canonical, parsed.day


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
        found = []
        complete = True
        for item in hint.evidence:
            key = _evidence_key(item)
            if key not in evidence_categories:
                complete = False
                break
            found.append(evidence_categories[key])
        if not complete or len(set(found)) != 1:
            continue
        category_id = found[0]
        if category_id in allowed:
            assigned[_hint_identity(hint)] = category_id
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
            try:
                due = installment_due_date(schedule.due_day, schedule.start_date, schedule.end_date, iso_date[:7])
            except MortgageInputError:
                raise ForecastDataError() from None
            if due is None:
                continue
            if abs((_iso_day(iso_date) - date.fromisoformat(due)).days) <= _MAX_MATCH_DISTANCE:
                hits.add(category_id)
        if len(hits) == 1:
            category_id = next(iter(hits))
            if category_id in allowed:
                assigned[schedule.schedule_id] = (category_id, schedule.currency)
    return assigned


def build_category_baselines(*, category_ids: Sequence[str], category_names: Mapping[str, str], history_months: int,
                             evaluation_date: str, expenses: Sequence[tuple],
                             evidence_categories: Mapping[tuple[str, str], str | None],
                             hints: Sequence[RecurringHint], schedules: Sequence[MortgageSchedule],
                             ) -> tuple[CategoryBaseline, ...]:
    """Aggregate configured categories over the last ``history_months`` complete months."""
    if (not isinstance(category_ids, Sequence) or isinstance(category_ids, (str, bytes))
            or not isinstance(category_names, Mapping)
            or type(history_months) is not int
            or not MIN_BASELINE_MONTHS <= history_months <= MAX_BASELINE_HISTORY_MONTHS):
        raise ForecastDataError()
    if len(category_ids) != len(set(category_ids)):
        raise ForecastDataError()
    evaluation = _iso_day(evaluation_date)
    allowed = set()
    names = {}
    for category_id in category_ids:
        if not isinstance(category_id, str) or category_id not in category_names:
            raise BudgetConfigError(_CONFIG_ERROR)
        name = category_names[category_id]
        if not isinstance(name, str) or not name.strip() or _CONTROL_RE.search(name):
            raise ForecastDataError()
        allowed.add(category_id)
        names[category_id] = name
    if not isinstance(expenses, Sequence) or isinstance(expenses, (str, bytes)):
        raise ForecastDataError()
    if len(expenses) > MAX_ROWS:
        raise ForecastLimitError()
    normalized = []
    for item in expenses:
        if not isinstance(item, tuple) or len(item) != 4:
            raise ForecastDataError()
        expense = _normalize_expense(*item)
        if expense is not None:
            normalized.append(expense)
    history = set(_previous_months(evaluation, history_months))
    evaluation_month = evaluation_date[:7]
    by_month = defaultdict(lambda: defaultdict(list))
    current = defaultdict(list)
    for category_id, currency, iso_date, amount, day in normalized:
        if category_id not in allowed:
            continue
        month = iso_date[:7]
        if month == evaluation_month:
            current[(category_id, currency)].append(amount)
        elif month in history:
            by_month[(category_id, currency)][month].append((amount, day, iso_date))
    hint_categories = _hint_categories(hints, evidence_categories, allowed)
    schedule_categories = _schedule_categories(schedules, normalized, allowed)
    baselines = []
    for (category_id, currency), months in sorted(by_month.items()):
        positive, days, observed = [], [], []
        for month in sorted(months):
            rows = months[month]
            positive.append(sum_money(amount for amount, _day, _iso in rows))
            days.extend(day for _amount, day, _iso in rows)
            observed.extend(iso for _amount, _day, iso in rows)
        if len(positive) < MIN_BASELINE_MONTHS:
            continue
        recurring_keys = frozenset(key for key, owner_category in hint_categories.items()
                                   if owner_category == category_id and key[0] == currency)
        schedule_ids = frozenset(
            schedule_id for schedule_id, (owner_category, schedule_currency) in schedule_categories.items()
            if owner_category == category_id and schedule_currency == currency)
        baselines.append(CategoryBaseline(
            category_id=category_id, name=names[category_id], currency=currency,
            monthly_amounts=tuple(positive), typical_day=sorted(days)[(len(days) - 1) // 2],
            current_spend=sum_money(current[(category_id, currency)]) if current[(category_id, currency)] else "0",
            recurring_keys=recurring_keys, schedule_ids=schedule_ids,
            last_observed_date=max(observed),
        ))
    if len(baselines) > MAX_BASELINE_SERIES:
        raise ForecastLimitError()
    return tuple(baselines)


def baselines_for_snapshot(snapshot, evaluation_date, hints, schedules, *, environ=None):
    """Return ``(baselines, estimator)``. Missing config yields no baselines."""
    settings = load_forecast_config(environ)
    if not settings:
        return (), "median"
    if not isinstance(snapshot, Mapping):
        raise ForecastDataError()
    owner = snapshot.get("owner")
    owner_settings = settings.get(owner) if isinstance(owner, str) else None
    if owner_settings is None:
        return (), "median"
    categories = snapshot.get("categories")
    if not isinstance(categories, Sequence) or isinstance(categories, (str, bytes)):
        raise ForecastDataError()
    names = {}
    for row in categories:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            raise ForecastDataError()
        names[row["id"]] = row.get("name")
    expenses, evidence = expenses_from_transactions(snapshot.get("transactions"))
    return build_category_baselines(
        category_ids=owner_settings.category_ids, category_names=names,
        history_months=owner_settings.history_months, evaluation_date=evaluation_date,
        expenses=expenses, evidence_categories=evidence, hints=hints, schedules=schedules,
    ), owner_settings.estimator


def _projected_hint_key(item, tie_breaker):
    if not isinstance(tie_breaker, tuple) or len(tie_breaker) != 4:
        raise ForecastDataError()
    return (item["currency"], tie_breaker[0], tie_breaker[1], item["amount"], tie_breaker[2], tie_breaker[3])


def _baseline_audit(baseline, *, schedule_id, schedule_label, recurring_label, amount, expected_date, account_stale):
    return {
        "schedule_id": schedule_id, "schedule_label": schedule_label, "recurring_label": recurring_label,
        "amount": amount, "currency": baseline.currency, "matched_due_date": expected_date,
        "last_observed_date": baseline.last_observed_date, "day_distance": 0, "account_stale": account_stale,
        "baseline_category_id": baseline.category_id,
    }


def _apply_category_baselines(projected, horizon, baselines, estimator, evaluation_month):
    audits = []
    ordered = sorted(baselines, key=lambda item: (item.currency, item.name, item.category_id))
    for baseline in ordered:
        if (not isinstance(baseline.name, str) or not _CURRENCY_RE.fullmatch(baseline.currency)
                or type(baseline.typical_day) is not int or not 1 <= baseline.typical_day <= 31):
            raise ForecastDataError()
        estimate = _baseline_estimate(baseline.monthly_amounts, estimator)
        if estimate is None:
            continue
        for month, month_items in zip(horizon, projected):
            remaining = estimate
            commitments = []
            for item, tie_breaker in month_items:
                if item["currency"] != baseline.currency:
                    continue
                if item["source"] == "mortgage" and item["schedule_id"] in baseline.schedule_ids:
                    commitments.append((0, item["expected_date"], item["schedule_id"] or "", item, tie_breaker))
                elif item["source"] == "recurring" and _projected_hint_key(item, tie_breaker) in baseline.recurring_keys:
                    commitments.append((1, item["expected_date"], item["label"], item, tie_breaker))
            for _order, _expected, _label, item, _tie in sorted(commitments, key=lambda row: row[:3]):
                remaining, applied = _sub_money(remaining, item["amount"])
                if compare_money(applied, "0") <= 0:
                    continue
                if item["source"] == "mortgage":
                    audits.append(_baseline_audit(
                        baseline, schedule_id=item["schedule_id"], schedule_label=item["label"],
                        recurring_label=baseline.name, amount=applied, expected_date=item["expected_date"],
                        account_stale=False,
                    ))
                else:
                    audits.append(_baseline_audit(
                        baseline, schedule_id=baseline.category_id, schedule_label=baseline.name,
                        recurring_label=item["label"], amount=applied, expected_date=item["expected_date"],
                        account_stale=item["account_stale"],
                    ))
                if remaining == "0":
                    break
            if month == evaluation_month and compare_money(remaining, "0") > 0:
                remaining, _spent = _sub_money(remaining, baseline.current_spend)
            if compare_money(remaining, "0") <= 0:
                continue
            year, month_number = int(month[:4]), int(month[5:7])
            expected = date(year, month_number, min(baseline.typical_day, calendar.monthrange(year, month_number)[1])).isoformat()
            month_items.append(({
                "source": "category_baseline", "schedule_id": None, "label": baseline.name,
                "amount": remaining, "currency": baseline.currency, "expected_date": expected,
                "confidence": "estimated", "past_due": False, "account_stale": False,
            }, baseline.category_id))
    return audits


@dataclass(frozen=True, repr=False)
class _ActiveHint:
    hint: RecurringHint
    key: tuple[str, str, str, str, str, str]
    amount: str
    last_date: date
    account_stale: bool


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
        key = (hint.currency, hint.normalized_label, hint.label_source, amount, hint.first_date, hint.last_date)
        active.append(_ActiveHint(hint, key, amount, last_date, account_stale))
    return active, lapsed_count, stale_count


def _due_date(schedule, month):
    return installment_due_date(schedule.due_day, schedule.start_date, schedule.end_date, month)


def _deduplicate(schedules, active_hints):
    edges = []
    for active in active_hints:
        last_month = _month_number(active.last_date)
        first_month = max(_FIRST_MONTH, last_month - 1)
        last_candidate_month = min(_LAST_MONTH, last_month + 1)
        candidate_months = [_month_name(number) for number in range(first_month, last_candidate_month + 1)]
        for schedule in schedules:
            if schedule.currency != active.hint.currency or compare_money(active.amount, schedule.amount) != 0:
                continue
            for month in candidate_months:
                due = _due_date(schedule, month)
                if due is None:
                    continue
                distance = abs((active.last_date - date.fromisoformat(due)).days)
                if distance <= _MAX_MATCH_DISTANCE:
                    edges.append(((distance, due, *active.key, schedule.schedule_id), schedule, active))
                    break

    audits = []
    used_installments = set()
    used_hints = set()
    for edge_key, schedule, active in sorted(edges, key=lambda edge: edge[0]):
        distance, due = edge_key[:2]
        installment = (schedule.schedule_id, due)
        if installment in used_installments or active.key in used_hints:
            # An occupied instalment leaves its competing hint eligible for a later edge.
            continue
        used_installments.add(installment)
        used_hints.add(active.key)
        audits.append({
            "schedule_id": schedule.schedule_id, "schedule_label": schedule.label,
            "recurring_label": active.hint.label, "amount": active.amount, "currency": schedule.currency,
            "matched_due_date": due, "last_observed_date": active.hint.last_date,
            "day_distance": distance, "account_stale": active.account_stale,
        })
    return audits, [active for active in active_hints if active.key not in used_hints]


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
        tie_breaker = (hint.normalized_label, hint.label_source, hint.first_date, hint.last_date)
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
    label = _IBAN_LIKE.sub(_mask_iban, label)
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
                      baselines: Sequence[CategoryBaseline] = (), baseline_estimator: str = "median") -> dict:
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
    if baselines and baseline_estimator not in _BASELINE_ESTIMATORS:
        raise ForecastDataError()
    for item in baselines:
        if not isinstance(item, CategoryBaseline):
            raise ForecastDataError()
    selected_baselines = [item for item in baselines if currency is None or item.currency == currency]
    baseline_audits = _apply_category_baselines(projected, horizon, selected_baselines, baseline_estimator, m0)
    audits = match_audits + baseline_audits
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
                                   audit.get("baseline_category_id") or "", audit["recurring_label"], audit["amount"]))

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
                    "stale_recurring_count": stale_count},
        "totals": _currency_totals(chain.from_iterable(month["items"] for month in month_rows)),
        "deduplications": audits, "months": month_rows,
    }
