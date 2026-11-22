"""Pure owner-private monthly budget dashboard evaluation (Budget Month P1).

Evaluates one selected month and currency from an immutable analytics snapshot
using exact scaled integer arithmetic (never floats for money), Europe/Warsaw
calendar cutoffs from a single UTC instant, verified per-leg transfer exclusions,
provisional warning signals, rent/mortgage pass-through allocation, expected
income matching across accounts, and bounded UTF-8/decimal outputs.

Errors carry fixed diagnostic messages without amounts, labels or owners.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache
import json
import math
import os
import re
from types import MappingProxyType
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import analytics_core as core
from .client import _reject_constant, _unique_error_fields
from .config import BudgetConfigError
from .spending_limits import MAX_LIMITS, SpendingLimitInputError, _parse_amount as _parse_limit_amount, _status as _limit_status
from .store import STALE_AFTER

RULE_VERSION = 1
MONTH_CONFIG_ENV = "HESTIA_BUDGET_MONTH_CONFIG"
MAX_CONFIG_BYTES = 16 * 1024
MAX_AGGREGATE_CHARS = 192
MAX_INCOME_CATEGORIES = 100
WARSAW_TIMEZONE = "Europe/Warsaw"
STALE_AFTER_SECONDS = STALE_AFTER["transactions"]

_ZERO = "0"
_PERIOD_RE = re.compile(r"([0-9]{4})-([0-9]{2})\Z", re.ASCII)
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_VALID_LIMIT_CURRENCIES = _CURRENCY_RE


class MonthInputError(ValueError):
    """Invalid query parameters; fixed message without request values."""

    def __init__(self):
        super().__init__("Invalid monthly budget query")


class MonthDataError(ValueError):
    """Invalid stored snapshot, limit, or timestamp data."""

    def __init__(self):
        super().__init__("Invalid monthly budget data")


class MonthLimitError(ValueError):
    """Explicit processing or aggregate bound exceeded; no partial output."""

    def __init__(self):
        super().__init__("Monthly budget limit exceeded")


@dataclass(frozen=True, repr=False)
class PassThroughSettings:
    rent_category_id: str
    mortgage_category_id: str


@dataclass(frozen=True, repr=False)
class MonthBudgetSettings:
    income_category_ids: tuple[str, ...]
    pass_through: PassThroughSettings | None = None


@dataclass(frozen=True, repr=False)
class _IncomeCredit:
    account_id: str
    entry_reference: str
    amount: str
    day: int
    norm_name: str


@dataclass(frozen=True, repr=False)
class _MortgageLeg:
    report_date: str
    account_id: str
    entry_reference: str
    amount: str


def _valid_config_text(value: object, *, allow_uncategorised: bool = True) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or _CONTROL_RE.search(value) or (not allow_uncategorised and value == "uncategorised")):
        raise BudgetConfigError("Invalid monthly budget configuration")
    return value


@lru_cache(maxsize=16)
def _parse_month_config_text(raw: str) -> Mapping[str, MonthBudgetSettings]:
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError:
        raise BudgetConfigError("Invalid monthly budget configuration") from None
    if len(encoded) > MAX_CONFIG_BYTES:
        raise BudgetConfigError("Invalid monthly budget configuration")
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_error_fields, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        raise BudgetConfigError("Invalid monthly budget configuration") from None
    if not isinstance(parsed, dict):
        raise BudgetConfigError("Invalid monthly budget configuration")

    result: dict[str, MonthBudgetSettings] = {}
    for owner_key, entry in parsed.items():
        owner = _valid_config_text(owner_key)
        if (not isinstance(entry, dict) or "income_category_ids" not in entry
                or set(entry) - {"income_category_ids", "pass_through"}):
            raise BudgetConfigError("Invalid monthly budget configuration")
        raw_income = entry["income_category_ids"]
        if not isinstance(raw_income, list) or len(raw_income) > MAX_INCOME_CATEGORIES:
            raise BudgetConfigError("Invalid monthly budget configuration")
        income_ids: list[str] = []
        income_seen: set[str] = set()
        for item in raw_income:
            category_id = _valid_config_text(item, allow_uncategorised=False)
            if category_id in income_seen:
                raise BudgetConfigError("Invalid monthly budget configuration")
            income_seen.add(category_id)
            income_ids.append(category_id)
        raw_pair = entry.get("pass_through")
        pair: PassThroughSettings | None = None
        if raw_pair is not None:
            if not isinstance(raw_pair, dict) or set(raw_pair) != {"rent_category_id", "mortgage_category_id"}:
                raise BudgetConfigError("Invalid monthly budget configuration")
            rent_id = _valid_config_text(raw_pair["rent_category_id"], allow_uncategorised=False)
            mortgage_id = _valid_config_text(raw_pair["mortgage_category_id"], allow_uncategorised=False)
            if rent_id == mortgage_id or rent_id in income_seen or mortgage_id in income_seen:
                raise BudgetConfigError("Invalid monthly budget configuration")
            pair = PassThroughSettings(rent_category_id=rent_id, mortgage_category_id=mortgage_id)
        result[owner] = MonthBudgetSettings(income_category_ids=tuple(income_ids), pass_through=pair)
    return MappingProxyType(result)


def load_month_config(environ: Mapping[str, str] | None = None) -> Mapping[str, MonthBudgetSettings] | None:
    """Read and parse optional server-only HESTIA_BUDGET_MONTH_CONFIG (bounded to 16 KiB)."""
    env = os.environ if environ is None else environ
    raw = env.get(MONTH_CONFIG_ENV)
    if raw is None or raw == "":
        return None
    return _parse_month_config_text(raw)


def validate_monthly_budget_query(period: str | None, currency: str | None) -> tuple[str | None, str]:
    """Validate optional ``period`` (YYYY-MM) and ``currency`` (3-letter uppercase, default PLN)."""
    if period is not None:
        match = _PERIOD_RE.fullmatch(period) if isinstance(period, str) else None
        if match is None or not 1900 <= int(match[1]) <= 9999 or not 1 <= int(match[2]) <= 12:
            raise MonthInputError()
    if currency is None:
        target_currency = "PLN"
    elif isinstance(currency, str) and _CURRENCY_RE.fullmatch(currency):
        target_currency = currency
    else:
        raise MonthInputError()
    return period, target_currency


def warsaw_date(evaluated_at: int | float) -> tuple[str, str]:
    """Derive ``(evaluation_date, current_month)`` in ``Europe/Warsaw`` from one UTC epoch instant."""
    try:
        zone = ZoneInfo(WARSAW_TIMEZONE)
    except ZoneInfoNotFoundError:
        raise BudgetConfigError("Monthly budget time zone is unavailable") from None
    if type(evaluated_at) not in (int, float) or not math.isfinite(evaluated_at) or evaluated_at < 0:
        raise MonthDataError()
    try:
        local_dt = datetime.fromtimestamp(evaluated_at, tz=timezone.utc).astimezone(zone)
    except (OverflowError, OSError, ValueError):
        raise MonthDataError() from None
    # Non-negative epochs map to years >= 1970; >9999 raises OverflowError above.
    evaluation_date = local_dt.date().isoformat()
    return evaluation_date, evaluation_date[:7]


def _previous_month(period: str) -> tuple[str, str]:
    year, month = int(period[:4]), int(period[5:7])
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    last_day = calendar.monthrange(prev_year, prev_month)[1]
    return f"{prev_year:04d}-{prev_month:02d}-01", f"{prev_year:04d}-{prev_month:02d}-{last_day:02d}"


def _check_aggregate(value: str) -> str:
    if len(value) > MAX_AGGREGATE_CHARS:
        raise MonthLimitError()
    return value


def _aligned_pair(first: str, second: str) -> tuple[int, int, int]:
    lc, ls = core._parts(first)
    rc, rs = core._parts(second)
    scale = max(ls, rs)
    return lc * 10 ** (scale - ls), rc * 10 ** (scale - rs), scale


def _add_money(first: str, second: str) -> str:
    left, right, scale = _aligned_pair(first, second)
    return _check_aggregate(core._format(left + right, scale))


def _sub_money(first: str, second: str) -> str:
    left, right, scale = _aligned_pair(first, second)
    return _check_aggregate(core._format(left - right, scale))


def _mul_int_money(value: str, factor: int) -> str:
    coeff, scale = core._parts(value)
    return _check_aggregate(core._format(coeff * factor, scale))


def _sum_money_checked(values) -> str:
    try:
        total = core.sum_money(values)
    except core.AnalyticsError:
        raise MonthLimitError() from None
    return _check_aggregate(total)


def _min_money(first: str, second: str) -> str:
    return first if core.compare_money(first, second) <= 0 else second


def _max_money(first: str, second: str) -> str:
    return first if core.compare_money(first, second) >= 0 else second


def _identity_pair_set(values: Sequence[Mapping]) -> frozenset[tuple[str, str]]:
    return frozenset((item["scheme"], item["identification"]) for item in values)


def _other_owned_identities(identities: Mapping[str, Sequence[Mapping]]) -> dict[str, frozenset[tuple[str, str]]]:
    per_account = {account_id: _identity_pair_set(values) for account_id, values in identities.items()}
    return {
        account_id: frozenset().union(*(pairs for other_id, pairs in per_account.items() if other_id != account_id))
        for account_id in per_account
    }


def _future_accepted_keys(transfers: Sequence[Mapping], evaluation_date: str) -> frozenset[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for view in transfers:
        if view["status"] == "accepted":
            debit, credit = view["debit"], view["credit"]
            if (debit["date"] is None or debit["date"] > evaluation_date
                    or credit["date"] is None or credit["date"] > evaluation_date):
                keys.add((debit["account_id"], debit["entry_reference"]))
                keys.add((credit["account_id"], credit["entry_reference"]))
    return frozenset(keys)


def _is_explicit_expense(eff: Mapping, annotation: Mapping, rules_by_id: Mapping[str, Mapping]) -> bool:
    if eff["source"] == "manual":
        return annotation["kind"] == "expense"
    if eff["source"] == "rule":
        return rules_by_id[eff["rule_id"]]["kind"] == "expense"
    return False


def _has_unresolved_transfer_evidence(
    row: Mapping,
    *,
    counterpart_outside_horizon: bool,
    other_identities: frozenset[tuple[str, str]],
) -> bool:
    if counterpart_outside_horizon:
        return True
    if row["transfer"]["status"] in ("suggested", "review", "boundary"):
        return True
    annotation = row["annotation"]
    if annotation["mode"] == "manual" and annotation["stale"] and annotation["kind"] == "excluded":
        return True
    metadata = row["metadata"]
    if metadata is not None and other_identities:
        field = "creditor_accounts" if row["payload"]["credit_debit_indicator"] == "DBIT" else "debtor_accounts"
        if _identity_pair_set(metadata.get(field, ())) & other_identities:
            return True
    return False


def _validate_timestamp(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise MonthDataError()
    return float(value)


def _compute_ledger_freshness(
    account_component_timestamps: Mapping[str, int | float | None],
    tx_latest_by_account: Mapping[str, float],
    evaluated_at: float,
) -> tuple[float | None, int]:
    ledger_as_of: float | None = None
    stale_count = 0
    for account_id, comp_raw in account_component_timestamps.items():
        comp_ts = None if comp_raw is None else _validate_timestamp(comp_raw)
        tx_ts = tx_latest_by_account.get(account_id)
        if comp_ts is None and tx_ts is None:
            stale_count += 1
            continue
        latest = max(ts for ts in (comp_ts, tx_ts) if ts is not None)
        acct_ts = min(latest, evaluated_at)
        if evaluated_at - acct_ts >= STALE_AFTER_SECONDS:
            stale_count += 1
        ledger_as_of = acct_ts if ledger_as_of is None else max(ledger_as_of, acct_ts)
    return ledger_as_of, stale_count


def _allocate_dated_adjustments(legs: list[_MortgageLeg], covered_total: str) -> dict[str, list[str]]:
    """Allocate ``covered_total`` across legs in ``(report_date, account_id, entry_reference)`` order."""
    ordered = sorted(legs, key=lambda leg: (leg.report_date, leg.account_id, leg.entry_reference))
    remaining_cover = covered_total
    adjusted_by_date: dict[str, list[str]] = defaultdict(list)
    for leg in ordered:
        if core.compare_money(remaining_cover, _ZERO) > 0:
            taken = _min_money(leg.amount, remaining_cover)
            remaining_cover = _sub_money(remaining_cover, taken)
            adjusted = _sub_money(leg.amount, taken)
        else:
            adjusted = leg.amount
        adjusted_by_date[leg.report_date].append(adjusted)
    return adjusted_by_date


def _recognize_dated_refunds(legs: list[_MortgageLeg], recognized_total: str) -> dict[str, list[str]]:
    """Recognize up to ``recognized_total`` across refund legs in ``(report_date, account_id, entry_reference)`` order."""
    ordered = sorted(legs, key=lambda leg: (leg.report_date, leg.account_id, leg.entry_reference))
    remaining = recognized_total
    recognized_by_date: dict[str, list[str]] = defaultdict(list)
    for leg in ordered:
        if core.compare_money(remaining, _ZERO) <= 0:
            break
        taken = _min_money(leg.amount, remaining)
        remaining = _sub_money(remaining, taken)
        recognized_by_date[leg.report_date].append(taken)
    return recognized_by_date


def _validate_limits(
    limits: Sequence[Mapping],
    categories_by_id: Mapping[str, Mapping],
    currency: str,
) -> dict[str, tuple[str, int]]:
    if len(limits) > MAX_LIMITS:
        raise MonthLimitError()
    seen: set[tuple[str, str]] = set()
    active_for_currency: dict[str, tuple[str, int]] = {}
    for row in limits:
        category_id = row["category_id"]
        limit_currency = row["currency"]
        warning_percent = row["warning_percent"]
        enabled = row["enabled"]
        if (not isinstance(category_id, str) or category_id not in categories_by_id
                or not isinstance(limit_currency, str) or not _VALID_LIMIT_CURRENCIES.fullmatch(limit_currency)
                or type(warning_percent) is not int or not 1 <= warning_percent <= 99
                or type(enabled) is not bool):
            raise MonthDataError()
        key = (category_id, limit_currency)
        if key in seen:
            raise MonthDataError()
        seen.add(key)
        try:
            coeff, scale = _parse_limit_amount(row["monthly_limit"], positive=True)
        except SpendingLimitInputError:
            raise MonthDataError() from None
        canonical_limit = core._format(coeff, scale)
        if enabled and limit_currency == currency:
            active_for_currency[category_id] = (canonical_limit, warning_percent)
    return active_for_currency


def _evaluate_category_limit(expenses: str, refunds: str, limit_info: tuple[str, int] | None) -> dict | None:
    if limit_info is None:
        return None
    monthly_limit, warning_percent = limit_info
    net_spending = _sub_money(expenses, refunds)
    net_c, net_s = core._parts(net_spending)
    lim_c, lim_s = core._parts(monthly_limit)
    scale = max(net_s, lim_s)
    net_aligned = net_c * 10 ** (scale - net_s)
    lim_aligned = lim_c * 10 ** (scale - lim_s)
    margin = lim_aligned - net_aligned
    remaining = _check_aggregate(core._format(margin, scale)) if margin > 0 else _ZERO
    over_by = _check_aggregate(core._format(-margin, scale)) if margin < 0 else _ZERO
    percent_str = _check_aggregate(_ZERO if net_aligned <= 0 else str((net_aligned * 100) // lim_aligned))
    return {
        "monthly_limit": monthly_limit,
        "remaining": remaining,
        "over_by": over_by,
        "percent_used_floor": percent_str,
        "warning_percent": warning_percent,
        "status": _limit_status(net_aligned, lim_aligned, warning_percent),
    }


def _normalize_debtor_name(metadata: Mapping | None) -> str:
    if not metadata:
        return ""
    raw = metadata.get("debtor_name")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise MonthDataError()
    return " ".join(_CONTROL_RE.sub(" ", unicodedata.normalize("NFC", raw)).split()).casefold()


def _clamp_expected_date(day: int, period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day)).isoformat()


def _expected_income_for_category(
    category_id: str,
    period: str,
    prior_rows: list[_IncomeCredit],
    current_rows: list[_IncomeCredit],
) -> tuple[list[dict], bool]:
    if not current_rows:
        counts = Counter((row.account_id, row.norm_name) for row in prior_rows if row.norm_name)
        items: list[dict] = []
        fallback_amounts: list[str] = []
        partial = False
        for row in prior_rows:
            if row.norm_name and counts[(row.account_id, row.norm_name)] == 1:
                items.append({
                    "category_id": category_id,
                    "amount": row.amount,
                    "expected_date": _clamp_expected_date(row.day, period),
                    "source": "previous_month",
                })
            else:
                partial = True
                fallback_amounts.append(row.amount)
        if fallback_amounts:
            items.append({
                "category_id": category_id,
                "amount": _sum_money_checked(fallback_amounts),
                "expected_date": None,
                "source": "previous_month",
            })
        return items, partial

    # Step 1: match unique prior and current credits for the same receiving account and nonempty debtor name.
    prior_by_local: dict[tuple[str, str], list[int]] = defaultdict(list)
    curr_by_local: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, row in enumerate(prior_rows):
        if row.norm_name:
            prior_by_local[(row.account_id, row.norm_name)].append(idx)
    for idx, row in enumerate(current_rows):
        if row.norm_name:
            curr_by_local[(row.account_id, row.norm_name)].append(idx)

    matched_prior: set[int] = set()
    matched_curr: set[int] = set()
    for key, p_indices in prior_by_local.items():
        c_indices = curr_by_local.get(key)
        if c_indices is not None and len(p_indices) == 1 and len(c_indices) == 1:
            matched_prior.add(p_indices[0])
            matched_curr.add(c_indices[0])

    unmatched_prior = [idx for idx in range(len(prior_rows)) if idx not in matched_prior]
    unmatched_curr = [idx for idx in range(len(current_rows)) if idx not in matched_curr]

    # Step 2: unique cross-account match per nonempty name when no unresolved local ambiguity exists.
    prior_acct_name: dict[tuple[str, str], int] = Counter()
    curr_acct_name: dict[tuple[str, str], int] = Counter()
    prior_by_name: dict[str, list[int]] = defaultdict(list)
    curr_by_name: dict[str, list[int]] = defaultdict(list)
    for idx in unmatched_prior:
        row = prior_rows[idx]
        if row.norm_name:
            prior_acct_name[(row.account_id, row.norm_name)] += 1
            prior_by_name[row.norm_name].append(idx)
    for idx in unmatched_curr:
        row = current_rows[idx]
        if row.norm_name:
            curr_acct_name[(row.account_id, row.norm_name)] += 1
            curr_by_name[row.norm_name].append(idx)

    locally_ambiguous_names = {
        name for account_id, name in prior_acct_name
        if curr_acct_name.get((account_id, name), 0) > 0
    }
    for name, p_indices in prior_by_name.items():
        c_indices = curr_by_name.get(name)
        if (c_indices is not None and name not in locally_ambiguous_names
                and len(p_indices) == 1 and len(c_indices) == 1):
            # Different accounts are guaranteed because Step 1 matched same-account unique pairs.
            matched_prior.add(p_indices[0])
            matched_curr.add(c_indices[0])

    rem_prior = [prior_rows[idx] for idx in range(len(prior_rows)) if idx not in matched_prior]
    rem_curr = [current_rows[idx] for idx in range(len(current_rows)) if idx not in matched_curr]

    # Step 3: resolve ambiguities and retain independently supported estimates.
    rem_prior_names = {row.norm_name for row in rem_prior if row.norm_name}
    if any(not row.norm_name or row.norm_name not in rem_prior_names for row in rem_curr):
        return [], True

    suppressed_names = {row.norm_name for row in rem_curr}
    local_counts = Counter((row.account_id, row.norm_name) for row in rem_prior if row.norm_name)
    partial = bool(suppressed_names)
    items = []
    for row in rem_prior:
        if not row.norm_name or row.norm_name in suppressed_names or local_counts[(row.account_id, row.norm_name)] > 1:
            partial = True
            continue
        items.append({
            "category_id": category_id,
            "amount": row.amount,
            "expected_date": _clamp_expected_date(row.day, period),
            "source": "previous_month",
        })
    return items, partial


def _compute_expected_income(
    period: str,
    current_month: str,
    income_category_ids: tuple[str, ...],
    prior_by_cat: Mapping[str, list[_IncomeCredit]],
    current_by_cat: Mapping[str, list[_IncomeCredit]],
) -> dict:
    if period != current_month:
        return {"status": "historical", "total": None, "items": []}
    if not income_category_ids:
        return {"status": "none", "total": _ZERO, "items": []}

    any_prior = False
    any_missing = False
    any_partial = False
    items: list[dict] = []
    for category_id in sorted(income_category_ids):
        prior_rows = prior_by_cat.get(category_id, [])
        if not prior_rows:
            any_missing = True
            continue
        any_prior = True
        cat_items, cat_partial = _expected_income_for_category(
            category_id, period, prior_rows, current_by_cat.get(category_id, []),
        )
        any_partial = any_partial or cat_partial
        items.extend(cat_items)

    items.sort(key=lambda item: (item["category_id"], item["expected_date"] is None, item["expected_date"] or "", item["amount"]))
    if not any_prior:
        return {"status": "unavailable", "total": None, "items": []}
    if any_partial or any_missing:
        total = _sum_money_checked(item["amount"] for item in items) if items else None
        return {"status": "partial", "total": total, "items": items}
    if items:
        return {"status": "estimated", "total": _sum_money_checked(item["amount"] for item in items), "items": items}
    return {"status": "none", "total": _ZERO, "items": []}


def _compute_pace(
    period: str,
    current_month: str,
    evaluation_date: str,
    start_budget: str,
    spent: str,
    expected_income: Mapping,
) -> dict:
    if expected_income["status"] in ("estimated", "partial") and expected_income["total"] is not None:
        budget = _add_money(start_budget, expected_income["total"])
        basis = "received_plus_expected"
    else:
        budget = start_budget
        basis = "received"

    if period != current_month:
        return {
            "basis": basis,
            "budget": budget,
            "status": "historical",
            "daily_rate": None,
            "projected_month_spend": None,
            "exhaustion_day": None,
        }

    eval_day = date.fromisoformat(evaluation_date)
    d = eval_day.day
    days_in_month = calendar.monthrange(eval_day.year, eval_day.month)[1]
    s_positive = core.compare_money(spent, _ZERO) > 0
    if s_positive:
        daily_rate = {"numerator": spent, "denominator": d}
        projected = {"numerator": _mul_int_money(spent, days_in_month), "denominator": d}
    else:
        daily_rate = None
        projected = None

    b_positive = core.compare_money(budget, _ZERO) > 0
    if not b_positive:
        status, exhaustion_day = "no_budget", None
    elif core.compare_money(spent, budget) >= 0:
        status, exhaustion_day = "already_reached", d
    elif not s_positive:
        status, exhaustion_day = "no_positive_pace", None
    else:
        s_aligned, b_aligned, _ = _aligned_pair(spent, budget)
        target = b_aligned * d
        candidate = (target + s_aligned - 1) // s_aligned
        if candidate <= days_in_month:
            status, exhaustion_day = "runs_out", candidate
        else:
            status, exhaustion_day = "within_budget", None

    return {
        "basis": basis,
        "budget": budget,
        "status": status,
        "daily_rate": daily_rate,
        "projected_month_spend": projected,
        "exhaustion_day": exhaustion_day,
    }


def evaluate_monthly_budget(
    *,
    owner: str,
    period: str | None,
    currency: str | None,
    evaluated_at: int | float,
    transactions: Sequence[Mapping],
    transfers: Sequence[Mapping],
    scan_limited: bool,
    categories: Sequence[Mapping],
    rules: Sequence[Mapping],
    identities: Mapping[str, Sequence[Mapping]],
    limits: Sequence[Mapping],
    account_component_timestamps: Mapping[str, int | float | None],
    account_kinds: Mapping[str, str],
    settings: MonthBudgetSettings | None,
) -> dict:
    """Pure single-snapshot evaluation for ``GET /api/budget/analytics/monthly-budget``."""
    requested_period, target_currency = validate_monthly_budget_query(period, currency)
    evaluation_date, current_month = warsaw_date(evaluated_at)
    effective_period = current_month if requested_period is None else requested_period
    if effective_period > current_month:
        raise MonthInputError()

    start, end = core.parse_period(effective_period)
    cutoff = evaluation_date if effective_period == current_month else end
    prev_start, prev_end = _previous_month(effective_period)

    categories_by_id = {row["id"]: row for row in categories}
    if settings is not None:
        for cid in settings.income_category_ids:
            if cid not in categories_by_id:
                raise BudgetConfigError("Invalid monthly budget configuration")
        if settings.pass_through is not None:
            if (settings.pass_through.rent_category_id not in categories_by_id
                    or settings.pass_through.mortgage_category_id not in categories_by_id):
                raise BudgetConfigError("Invalid monthly budget configuration")

    active_limits = _validate_limits(limits, categories_by_id, target_currency)
    observed_currencies: set[str] = {"PLN"}
    for limit_row in limits:
        if limit_row["enabled"]:
            observed_currencies.add(limit_row["currency"])

    rules_by_id = {row["id"]: row for row in rules}
    other_owned = _other_owned_identities(identities)
    future_accepted = _future_accepted_keys(transfers, evaluation_date)

    configured = settings is not None
    income_set = frozenset(settings.income_category_ids) if settings is not None else frozenset()
    pass_through_cfg = settings.pass_through if settings is not None else None
    rent_cat_id = pass_through_cfg.rent_category_id if pass_through_cfg is not None else None
    mortgage_cat_id = pass_through_cfg.mortgage_category_id if pass_through_cfg is not None else None

    tx_latest_by_account: dict[str, float] = {}
    purchase_date_fallback_count = 0
    pending_transfer_rows = 0
    has_affecting_unexcluded = False
    has_activity = False

    start_budget_amounts: list[str] = []
    rent_amounts: list[str] = []
    mortgage_debits_legs: list[_MortgageLeg] = []
    mortgage_refund_legs: list[_MortgageLeg] = []
    ordinary_expenses: dict[str | None, list[str]] = defaultdict(list)
    ordinary_refunds: dict[str | None, list[str]] = defaultdict(list)
    observed_spend_categories: set[str | None] = set()
    prior_income_by_cat: dict[str, list[_IncomeCredit]] = defaultdict(list)
    current_income_by_cat: dict[str, list[_IncomeCredit]] = defaultdict(list)

    for row in transactions:
        payload = row["payload"]
        posting_date = core.effective_date(payload)
        if posting_date > evaluation_date:
            continue

        row_currency = payload["currency"]
        observed_currencies.add(row_currency)
        acct_id = row["account_id"]
        obs_ts = _validate_timestamp(row["observed_at"])
        prior_ts = tx_latest_by_account.get(acct_id)
        if prior_ts is None or obs_ts > prior_ts:
            tx_latest_by_account[acct_id] = obs_ts

        if row_currency != target_currency:
            continue

        try:
            amount = core.canonical_money(payload["amount"])
        except core.AnalyticsError:
            raise MonthDataError() from None
        key = (acct_id, row["entry_reference"])
        counterpart_outside = key in future_accepted
        eff = row["base_effective"] if counterpart_outside else row["effective"]
        direction = payload["credit_debit_indicator"]
        tx_date = payload.get("transaction_date")
        report_date = (tx_date or posting_date) if direction == "DBIT" else posting_date
        kind = eff["kind"]
        cat_id = eff["category_id"]

        if start <= report_date <= cutoff:
            has_activity = True
            nonzero = core.compare_money(amount, _ZERO) > 0
            unresolved = _has_unresolved_transfer_evidence(
                row,
                counterpart_outside_horizon=counterpart_outside,
                other_identities=other_owned.get(acct_id, frozenset()),
            )
            if direction == "DBIT" and kind == "expense":
                if tx_date is None:
                    purchase_date_fallback_count += 1
                if nonzero:
                    has_affecting_unexcluded = True
                    if unresolved or (account_kinds.get(acct_id) != "credit_card"
                                      and not _is_explicit_expense(eff, row["annotation"], rules_by_id)):
                        pending_transfer_rows += 1
                observed_spend_categories.add(cat_id)
                if mortgage_cat_id is not None and cat_id == mortgage_cat_id:
                    mortgage_debits_legs.append(_MortgageLeg(report_date, acct_id, row["entry_reference"], amount))
                else:
                    ordinary_expenses[cat_id].append(amount)
            elif direction == "CRDT":
                if kind == "refund":
                    if nonzero:
                        has_affecting_unexcluded = True
                        if unresolved:
                            pending_transfer_rows += 1
                    observed_spend_categories.add(cat_id)
                    if mortgage_cat_id is not None and cat_id == mortgage_cat_id:
                        mortgage_refund_legs.append(_MortgageLeg(report_date, acct_id, row["entry_reference"], amount))
                    else:
                        ordinary_refunds[cat_id].append(amount)
                elif kind == "income":
                    if configured and cat_id in income_set:
                        if nonzero:
                            has_affecting_unexcluded = True
                            if unresolved:
                                pending_transfer_rows += 1
                            if effective_period == current_month:
                                current_income_by_cat[cat_id].append(_IncomeCredit(
                                    acct_id, row["entry_reference"], amount,
                                    date.fromisoformat(posting_date).day, _normalize_debtor_name(row["metadata"]),
                                ))
                        start_budget_amounts.append(amount)
                    elif rent_cat_id is not None and cat_id == rent_cat_id:
                        if nonzero:
                            has_affecting_unexcluded = True
                            if unresolved:
                                pending_transfer_rows += 1
                        rent_amounts.append(amount)
        elif (configured and effective_period == current_month and settings.income_category_ids
              and direction == "CRDT" and kind == "income" and cat_id in income_set
              and prev_start <= posting_date <= prev_end and core.compare_money(amount, _ZERO) > 0):
            prior_income_by_cat[cat_id].append(_IncomeCredit(
                acct_id, row["entry_reference"], amount,
                date.fromisoformat(posting_date).day, _normalize_debtor_name(row["metadata"]),
            ))

    ledger_as_of, stale_selected_accounts = _compute_ledger_freshness(
        account_component_timestamps, tx_latest_by_account, float(evaluated_at),
    )
    transfer_scan_limited = bool(scan_limited and has_affecting_unexcluded)
    provisional = pending_transfer_rows > 0 or transfer_scan_limited
    available_currencies = sorted(observed_currencies, key=lambda code: (code != "PLN", code != "EUR", code))

    coverage = {
        "ledger_as_of": ledger_as_of,
        "stale_selected_accounts": stale_selected_accounts,
        "purchase_date_fallback_count": purchase_date_fallback_count,
        "pending_transfer_rows": pending_transfer_rows,
        "transfer_scan_limited": transfer_scan_limited,
        "provisional": provisional,
        "has_activity": has_activity,
    }
    card_exposure = {"status": "unavailable", "amount": None, "as_of_date": None}
    envelope = {
        "period": effective_period,
        "start": start,
        "end": end,
        "cutoff": cutoff,
        "current_month": current_month,
        "evaluated_at": float(evaluated_at),
        "evaluation_date": evaluation_date,
        "evaluation_timezone": WARSAW_TIMEZONE,
        "currency": target_currency,
        "available_currencies": available_currencies,
        "coverage": coverage,
        "card_exposure": card_exposure,
    }

    if not configured:
        return {
            **envelope,
            "configuration": {"status": "unconfigured", "source": "none"},
            "summary": None,
            "expected_income": None,
            "pace": None,
            "category_spend": [],
            "pass_through": None,
        }

    pass_through_wire: dict | None = None
    category_nets: dict[str | None, tuple[str, str, str]] = {}
    if pass_through_cfg is not None:
        r_total = _sum_money_checked(rent_amounts)
        m_total = _sum_money_checked(leg.amount for leg in mortgage_debits_legs)
        f_total = _sum_money_checked(leg.amount for leg in mortgage_refund_legs)
        m_minus_f = _max_money(_sub_money(m_total, f_total), _ZERO)
        matched = _min_money(r_total, m_minus_f)
        adj_mortgage_expenses_by_date = _allocate_dated_adjustments(mortgage_debits_legs, matched)
        adj_expenses = _sum_money_checked(val for vals in adj_mortgage_expenses_by_date.values() for val in vals)
        max_refund_cap = _sub_money(m_total, matched)
        recognized_refunds = _min_money(f_total, max_refund_cap)
        adj_mortgage_refunds_by_date = _recognize_dated_refunds(mortgage_refund_legs, recognized_refunds)
        adj_refunds = _sum_money_checked(val for vals in adj_mortgage_refunds_by_date.values() for val in vals)
        uncovered_mortgage = _sub_money(adj_expenses, adj_refunds)
        pass_through_wire = {
            "rent_category_id": pass_through_cfg.rent_category_id,
            "mortgage_category_id": pass_through_cfg.mortgage_category_id,
            "rent_received": r_total,
            "mortgage_debits": m_total,
            "mortgage_refunds": f_total,
            "unapplied_mortgage_refunds": _sub_money(f_total, adj_refunds),
            "matched": matched,
            "unapplied_rent": _sub_money(r_total, matched),
            "uncovered_mortgage": uncovered_mortgage,
        }
        if mortgage_cat_id in observed_spend_categories or mortgage_cat_id in active_limits:
            category_nets[mortgage_cat_id] = (adj_expenses, adj_refunds, uncovered_mortgage)

    all_spend_categories = observed_spend_categories | set(active_limits)
    category_spend: list[dict] = []
    spent_terms: list[str] = []
    for cat_id in sorted(all_spend_categories, key=lambda cid: (cid is None, cid or "")):
        if cat_id in category_nets:
            exp_total, ref_total, net_total = category_nets[cat_id]
        else:
            exp_total = _sum_money_checked(ordinary_expenses.get(cat_id, ()))
            ref_total = _sum_money_checked(ordinary_refunds.get(cat_id, ()))
            net_total = _sub_money(exp_total, ref_total)
        spent_terms.append(net_total)
        category_meta = None if cat_id is None else categories_by_id[cat_id]
        category_spend.append({
            "category_id": cat_id,
            "name": None if category_meta is None else category_meta["name"],
            "archived": False if category_meta is None else category_meta["archived"],
            "expenses": exp_total,
            "refunds": ref_total,
            "net_spending": net_total,
            "limit": None if cat_id is None else _evaluate_category_limit(exp_total, ref_total, active_limits.get(cat_id)),
        })

    start_budget = _sum_money_checked(start_budget_amounts)
    spent = _sum_money_checked(spent_terms)
    remaining = _sub_money(start_budget, spent)
    summary = {"start_budget": start_budget, "spent": spent, "remaining": remaining}
    expected_income = _compute_expected_income(
        effective_period, current_month, settings.income_category_ids, prior_income_by_cat, current_income_by_cat,
    )
    pace = _compute_pace(effective_period, current_month, evaluation_date, start_budget, spent, expected_income)

    return {
        **envelope,
        "configuration": {"status": "ready", "source": "bootstrap"},
        "summary": summary,
        "expected_income": expected_income,
        "pace": pace,
        "category_spend": category_spend,
        "pass_through": pass_through_wire,
    }
