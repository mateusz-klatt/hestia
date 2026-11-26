"""Pure owner-private monthly budget dashboard evaluation (Budget Month P1 & P2).

Evaluates one selected month and currency (plus optional chart series) from an
immutable analytics snapshot using exact scaled integer arithmetic (never floats
for money), Europe/Warsaw calendar cutoffs from a single UTC instant, verified
per-leg transfer exclusions, provisional warning signals, rent/mortgage
pass-through allocation, expected income matching across accounts, and bounded
UTF-8/decimal outputs.

Errors carry fixed diagnostic messages without amounts, labels or owners.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
from .store import STALE_AFTER, BudgetStoreError, parse_credit_limit

RULE_VERSION = 1
MONTH_CONFIG_ENV = "HESTIA_BUDGET_MONTH_CONFIG"
MAX_CONFIG_BYTES = 16 * 1024
MAX_AGGREGATE_CHARS = 192
MAX_INCOME_CATEGORIES = 100
WARSAW_TIMEZONE = "Europe/Warsaw"
STALE_AFTER_SECONDS = STALE_AFTER["transactions"]
EARLIEST_PERIOD = "1900-01"
DEFAULT_HISTORY_MONTHS = 6
ALLOWED_HISTORY_MONTHS = frozenset({6, 12})
MAX_PENDING_ROW_KEYS = 500

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
    income_window_start_day: int = 1


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
                or set(entry) - {"income_category_ids", "pass_through", "income_window_start_day"}):
            raise BudgetConfigError("Invalid monthly budget configuration")
        income_window_start_day = entry.get("income_window_start_day", 1)
        if type(income_window_start_day) is not int or not 1 <= income_window_start_day <= 28:
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
        result[owner] = MonthBudgetSettings(
            income_category_ids=tuple(income_ids), pass_through=pair,
            income_window_start_day=income_window_start_day,
        )
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


def validate_monthly_budget_charts_query(
    period: str | None,
    currency: str | None,
    history: str | None = None,
) -> tuple[str | None, str, int]:
    """Validate optional ``period``, ``currency``, and ``history`` (``\"6\"`` or ``\"12\"``, default 6)."""
    validated_period, target_currency = validate_monthly_budget_query(period, currency)
    if history is None:
        history_months = DEFAULT_HISTORY_MONTHS
    elif isinstance(history, str) and history in ("6", "12"):
        history_months = int(history)
    else:
        raise MonthInputError()
    return validated_period, target_currency, history_months


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


def _shift_period(period: str, offset: int) -> str:
    year, month = int(period[:4]), int(period[5:7])
    shifted_year, shifted_month = divmod(year * 12 + month - 1 + offset, 12)
    return f"{shifted_year:04d}-{shifted_month + 1:02d}"


def _income_window(period: str, start_day: int) -> tuple[str, str]:
    if start_day == 1:
        return core.parse_period(period)
    start = f"{_shift_period(period, -1)}-{start_day:02d}"
    end = (date.fromisoformat(f"{period}-{start_day:02d}") - timedelta(days=1)).isoformat()
    return start, end


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


def _other_owned_identities(
    per_account: Mapping[str, frozenset[tuple[str, str]]],
) -> dict[str, frozenset[tuple[str, str]]]:
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


def _has_unresolved_transfer_evidence(
    row: Mapping,
    *,
    counterpart_outside_horizon: bool,
    other_identities: frozenset[tuple[str, str]],
    unresolved_bank_repayment: bool = False,
) -> bool:
    if counterpart_outside_horizon or unresolved_bank_repayment:
        return True
    if row["transfer"]["status"] in ("suggested", "review", "boundary"):
        return True
    annotation = row["annotation"]
    if annotation["mode"] == "manual" and annotation["stale"] and annotation["kind"] == "excluded":
        return True
    metadata = row["metadata"]
    if metadata is not None and other_identities:
        field = "creditor_accounts" if row["payload"]["credit_debit_indicator"] == "DBIT" else "debtor_accounts"
        if core.identity_pair_set(metadata.get(field, ())) & other_identities:
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


def _valid_iso_date(value: object) -> str:
    try:
        if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise MonthDataError() from None
    return value


def _extract_itav_by_currency(entries: object) -> dict[str, list[tuple[str, str | None]]]:
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise MonthDataError()
    by_currency: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise MonthDataError()
        if entry.get("balance_type") != "ITAV":
            continue
        amount_obj = entry.get("balance_amount")
        if not isinstance(amount_obj, Mapping):
            raise MonthDataError()
        currency = amount_obj.get("currency")
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            raise MonthDataError()
        try:
            available = core.canonical_money(amount_obj.get("amount"))
        except core.AnalyticsError:
            raise MonthDataError() from None
        ref_date = entry.get("reference_date")
        if ref_date is not None:
            _valid_iso_date(ref_date)
        by_currency[currency].append((available, ref_date))
    return by_currency


def _is_card_balance_stale(
    fetch_ts: float | None,
    as_of_date: str | None,
    evaluation_date: str,
    evaluated_at: float,
) -> bool:
    if fetch_ts is None or (evaluated_at - fetch_ts) >= STALE_AFTER["balances"]:
        return True
    if as_of_date is not None and (date.fromisoformat(evaluation_date) - date.fromisoformat(as_of_date)).days > 1:
        return True
    return False


def _compute_card_debt(
    *,
    account_kinds: Mapping[str, str],
    account_balances: Mapping[str, Sequence[Mapping]] | None,
    account_credit_limits: Mapping[str, Mapping[str, str] | None] | None,
    account_balance_timestamps: Mapping[str, int | float | None] | None,
    target_currency: str,
    evaluation_date: str,
    evaluated_at: float,
) -> dict:
    card_ids = sorted(account_id for account_id, kind in account_kinds.items() if kind == "credit_card")
    if not card_ids:
        return {
            "status": "unavailable",
            "amount": None,
            "currency": target_currency,
            "as_of_date": None,
            "accounts": [],
        }
    accounts_out: list[dict] = []
    has_partial = False
    for account_id in card_ids:
        raw_ts = None if account_balance_timestamps is None else account_balance_timestamps.get(account_id)
        fetch_ts = None if raw_ts is None else min(_validate_timestamp(raw_ts), evaluated_at)
        raw_balances = () if account_balances is None else account_balances.get(account_id, ())
        itav_by_currency = _extract_itav_by_currency(raw_balances)
        raw_limit = None if account_credit_limits is None else account_credit_limits.get(account_id)
        if raw_limit is None:
            has_partial = True
            continue
        try:
            limit = parse_credit_limit(raw_limit)
        except BudgetStoreError:
            raise MonthDataError() from None
        limit_amount, limit_currency = limit["amount"], limit["currency"]
        if limit_currency != target_currency:
            has_partial = True
            continue
        matching = itav_by_currency.get(limit_currency, ())
        if len(matching) != 1:
            has_partial = True
            continue
        available, ref_date = matching[0]
        # Reject future-dated bank balances (> Warsaw evaluation_date) as unusable and mark partial.
        if ref_date is not None and ref_date > evaluation_date:
            has_partial = True
            continue
        # Prefer bank's reference_date when present; otherwise use fetch date in Europe/Warsaw.
        card_as_of = ref_date if ref_date is not None else (warsaw_date(fetch_ts)[0] if fetch_ts is not None else None)
        if _is_card_balance_stale(fetch_ts, card_as_of, evaluation_date, evaluated_at):
            has_partial = True
        accounts_out.append({
            "account_id": account_id,
            "debt": _max_money(_sub_money(limit_amount, available), _ZERO),
            "limit": _check_aggregate(limit_amount),
            "available": _check_aggregate(available),
            "currency": limit_currency,
            "as_of": card_as_of,
        })
    dates = [item["as_of"] for item in accounts_out if item["as_of"] is not None]
    return {
        "status": "partial" if has_partial else "available",
        "amount": _sum_money_checked(item["debt"] for item in accounts_out) if accounts_out else None,
        "currency": target_currency,
        "as_of_date": min(dates) if (accounts_out and dates) else None,
        "accounts": accounts_out,
    }


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


def _category_children(categories_by_id: Mapping[str, Mapping]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = defaultdict(list)
    for identifier, row in categories_by_id.items():
        parent = row.get("parent_id")
        if isinstance(parent, str) and parent:
            children[parent].append(identifier)
    return children


def _subtree_limit_amounts(
    category_id: str,
    direct: Mapping[str | None, tuple[str, str]],
    children: Mapping[str, Sequence[str]],
) -> tuple[str, str] | None:
    """Own expenses and refunds plus descendants. None when descendants add no amount."""
    extras_exp: list[str] = []
    extras_ref: list[str] = []
    stack = list(children.get(category_id, ()))
    seen = {category_id}
    while stack:
        child = stack.pop()
        if child in seen:
            raise MonthDataError()
        seen.add(child)
        amounts = direct.get(child)
        if amounts is not None and (core.compare_money(amounts[0], _ZERO) != 0 or core.compare_money(amounts[1], _ZERO) != 0):
            extras_exp.append(amounts[0])
            extras_ref.append(amounts[1])
        stack.extend(children.get(child, ()))
    if not extras_exp:
        return None
    own = direct.get(category_id)
    if own is not None:
        extras_exp.append(own[0])
        extras_ref.append(own[1])
    return _sum_money_checked(extras_exp), _sum_money_checked(extras_ref)


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


def _clamp_expected_date(day: int, period: str, income_start: str, income_end: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    last_day = calendar.monthrange(year, month)[1]
    expected = date(year, month, min(day, last_day)).isoformat()
    return min(max(expected, income_start), income_end)


def _expected_income_for_category(
    category_id: str,
    period: str,
    income_start: str,
    income_end: str,
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
                    "expected_date": _clamp_expected_date(row.day, period, income_start, income_end),
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
            "expected_date": _clamp_expected_date(row.day, period, income_start, income_end),
            "source": "previous_month",
        })
    return items, partial


def _compute_expected_income(
    period: str,
    current_month: str,
    income_start: str,
    income_end: str,
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
            category_id, period, income_start, income_end, prior_rows, current_by_cat.get(category_id, []),
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


@dataclass
class _BucketAccum:
    period: str
    start: str
    cutoff: str
    has_activity: bool = False
    pending_transfer_rows: int = 0
    pending_row_keys: list[str] = field(default_factory=list)
    has_affecting_unexcluded: bool = False
    start_budget_amounts: list[str] = field(default_factory=list)
    rent_amounts: list[str] = field(default_factory=list)
    mortgage_debits_legs: list[_MortgageLeg] = field(default_factory=list)
    mortgage_refund_legs: list[_MortgageLeg] = field(default_factory=list)
    ordinary_expenses: dict[str | None, list[str]] = field(default_factory=lambda: defaultdict(list))
    ordinary_refunds: dict[str | None, list[str]] = field(default_factory=lambda: defaultdict(list))
    # (label, category_id) amounts that also sit in the ordinary maps.
    # Mortgage pass-through legs are not recorded here. ``members`` are (account_id, entry_reference).
    one_offs: dict[tuple[str, str | None], dict[str, list]] = field(default_factory=dict)
    observed_spend_categories: set[str | None] = field(default_factory=set)

    def record_pending(self, row_key: str) -> None:
        self.pending_transfer_rows += 1
        if len(self.pending_row_keys) < MAX_PENDING_ROW_KEYS:
            self.pending_row_keys.append(row_key)


def _chart_periods(effective_period: str, history: int) -> list[str]:
    periods = []
    for offset in range(-(history - 1), 1):
        candidate = _shift_period(effective_period, offset)
        if candidate >= EARLIEST_PERIOD:
            periods.append(candidate)
    return periods


def _income_period_for_posting_date(posting_date: str, start_day: int) -> str:
    period = posting_date[:7]
    if start_day > 1 and int(posting_date[8:10]) >= start_day:
        return _shift_period(period, 1)
    return period


def _finalize_pass_through(
    bucket: _BucketAccum,
    pass_through_cfg: PassThroughSettings | None,
) -> tuple[dict | None, tuple[str, str, str] | None, dict[str, list[str]], dict[str, list[str]]]:
    if pass_through_cfg is None:
        return None, None, {}, {}
    r_total = _sum_money_checked(bucket.rent_amounts)
    m_total = _sum_money_checked(leg.amount for leg in bucket.mortgage_debits_legs)
    f_total = _sum_money_checked(leg.amount for leg in bucket.mortgage_refund_legs)
    m_minus_f = _max_money(_sub_money(m_total, f_total), _ZERO)
    matched = _min_money(r_total, m_minus_f)
    adj_expenses_by_date = _allocate_dated_adjustments(bucket.mortgage_debits_legs, matched)
    adj_expenses = _sum_money_checked(val for vals in adj_expenses_by_date.values() for val in vals)
    recognized_refunds = _min_money(f_total, _sub_money(m_total, matched))
    adj_refunds_by_date = _recognize_dated_refunds(bucket.mortgage_refund_legs, recognized_refunds)
    adj_refunds = _sum_money_checked(val for vals in adj_refunds_by_date.values() for val in vals)
    uncovered_mortgage = _sub_money(adj_expenses, adj_refunds)
    wire = {
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
    return wire, (adj_expenses, adj_refunds, uncovered_mortgage), adj_expenses_by_date, adj_refunds_by_date


def _bucket_category_totals(
    bucket: _BucketAccum,
    pass_through_cfg: PassThroughSettings | None,
    *,
    extra_categories: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict | None, dict[str, list[str]], dict[str, list[str]], list[tuple[str | None, str, str, str]], str, str]:
    pass_through_wire, mortgage_net, adj_exp_dates, adj_ref_dates = _finalize_pass_through(bucket, pass_through_cfg)
    mortgage_cat_id = None if pass_through_cfg is None else pass_through_cfg.mortgage_category_id
    category_rows: list[tuple[str | None, str, str, str]] = []
    spent_terms: list[str] = []
    for cat_id in sorted(bucket.observed_spend_categories | set(extra_categories), key=lambda cid: (cid is None, cid or "")):
        if mortgage_net is not None and cat_id == mortgage_cat_id:
            exp_total, ref_total, net_total = mortgage_net
        else:
            exp_total = _sum_money_checked(bucket.ordinary_expenses.get(cat_id, ()))
            ref_total = _sum_money_checked(bucket.ordinary_refunds.get(cat_id, ()))
            net_total = _sub_money(exp_total, ref_total)
        spent_terms.append(net_total)
        category_rows.append((cat_id, exp_total, ref_total, net_total))
    start_budget = _sum_money_checked(bucket.start_budget_amounts)
    spent = _sum_money_checked(spent_terms)
    return pass_through_wire, adj_exp_dates, adj_ref_dates, category_rows, start_budget, spent


def _note_one_off(bucket: _BucketAccum, annotation: Mapping, cat_id: str | None, amount: str, *,
                  refund: bool, account_id: str, entry_reference: str) -> None:
    """Remember a labelled ordinary expense or refund. The amount stays in the ordinary map too."""
    label = core.active_one_off_label(annotation, amount)
    if not isinstance(label, str):
        return
    slot = bucket.one_offs.setdefault((label, cat_id), {"expenses": [], "refunds": [], "members": []})
    slot["refunds" if refund else "expenses"].append(amount)
    slot["members"].append((account_id, entry_reference))


def _zero_money(value: str) -> bool:
    return core.compare_money(value, _ZERO) == 0


def _one_off_identity(members, secret: bytes | None) -> str:
    """HMAC row key. A missing secret is stored-data failure, not a reversible hash."""
    try:
        return core.one_off_key(members, secret)
    except core.AnalyticsError:
        raise MonthDataError() from None


def _split_labelled(
    category_rows: Sequence[tuple[str | None, str, str, str]],
    one_offs: Mapping[tuple[str, str | None], Mapping[str, Sequence]],
    *,
    observed: set[str | None] | None = None,
    keep_ids: set[str | None] | frozenset[str | None] = frozenset(),
    one_off_secret: bytes | None = None,
) -> list[dict]:
    """Move labelled nets out of their category. The sum of nets stays equal to the category rows.

    A category row disappears only when a label took every remaining expense and refund and the
    category is not in ``keep_ids`` (an active limit still shows a zero row). A genuine zero stays.
    Mortgage pass-through rows are untouched because those legs are never recorded in ``one_offs``.
    """
    taken_exp: dict[str | None, str] = {}
    taken_ref: dict[str | None, str] = {}
    prepared: list[tuple] = []
    for (label, cat_id), slot in one_offs.items():
        exp = _sum_money_checked(slot.get("expenses", ()))
        ref = _sum_money_checked(slot.get("refunds", ()))
        taken_exp[cat_id] = _sum_money_checked((taken_exp.get(cat_id, _ZERO), exp))
        taken_ref[cat_id] = _sum_money_checked((taken_ref.get(cat_id, _ZERO), ref))
        prepared.append((label, cat_id, exp, ref, tuple(slot.get("members", ()))))
    result: list[dict] = []
    for cat_id, exp_total, ref_total, _net_total in category_rows:
        if observed is not None and cat_id not in observed:
            continue
        if cat_id in taken_exp:
            exp_total = _sub_money(exp_total, taken_exp.get(cat_id, _ZERO))
            ref_total = _sub_money(ref_total, taken_ref.get(cat_id, _ZERO))
            if _zero_money(exp_total) and _zero_money(ref_total) and cat_id not in keep_ids:
                continue
        result.append({
            "category_id": cat_id, "name": None, "one_off": False, "one_off_key": None,
            "expenses": exp_total, "refunds": ref_total, "net_spending": _sub_money(exp_total, ref_total),
        })
    for label, cat_id, exp, ref, members in sorted(prepared, key=lambda item: (item[0], item[1] is None, item[1] or "")):
        if observed is not None and cat_id not in observed:
            continue
        if _zero_money(exp) and _zero_money(ref):
            continue
        result.append({
            "category_id": cat_id, "name": label, "one_off": True,
            "one_off_key": _one_off_identity(members, one_off_secret),
            "expenses": exp, "refunds": ref, "net_spending": _sub_money(exp, ref),
        })
    return result


def _series_categories(
    category_rows: Sequence[tuple[str | None, str, str, str]],
    one_offs: Mapping[tuple[str, str | None], Mapping[str, Sequence]],
    *,
    observed: set[str | None] | None = None,
    one_off_secret: bytes | None = None,
) -> list[dict]:
    """Chart rows for ``_split_labelled``. Names of ordinary categories stay on ``category_labels``."""
    return [
        {"category_id": row["category_id"], "name": row["name"], "one_off": row["one_off"],
         "one_off_key": row["one_off_key"], "net_spending": row["net_spending"]}
        for row in _split_labelled(category_rows, one_offs, observed=observed, one_off_secret=one_off_secret)
    ]


def _build_daily_series(
    start: str,
    cutoff: str,
    ordinary_by_date: Mapping[str, list[str]],
    adj_expenses_by_date: Mapping[str, list[str]],
    adj_refunds_by_date: Mapping[str, list[str]],
) -> list[dict]:
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(cutoff)
    running = _ZERO
    daily: list[dict] = []
    for offset in range((end_d - start_d).days + 1):
        day_iso = (start_d + timedelta(days=offset)).isoformat()
        day_terms = list(ordinary_by_date.get(day_iso, ()))
        day_terms.extend(adj_expenses_by_date.get(day_iso, ()))
        for ref_amt in adj_refunds_by_date.get(day_iso, ()):
            day_terms.append(core.negate_money(ref_amt))
        day_delta = _sum_money_checked(day_terms) if day_terms else _ZERO
        running = _add_money(running, day_delta)
        daily.append({"date": day_iso, "cumulative_spent": running})
    return daily


def _evaluate_monthly_budget_core(
    *,
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
    history: int | None,
    account_balances: Mapping[str, Sequence[Mapping]] | None = None,
    account_credit_limits: Mapping[str, str | None] | None = None,
    account_balance_timestamps: Mapping[str, int | float | None] | None = None,
    one_off_secret: bytes | None = None,
    household_daily: bool = False,
) -> tuple[dict, dict | None]:
    requested_period, target_currency = validate_monthly_budget_query(period, currency)
    evaluation_date, current_month = warsaw_date(evaluated_at)
    effective_period = current_month if requested_period is None else requested_period
    if effective_period > current_month:
        raise MonthInputError()

    start, end = core.parse_period(effective_period)
    cutoff = evaluation_date if effective_period == current_month else end
    income_window_start_day = settings.income_window_start_day if settings is not None else 1
    income_start, income_end = _income_window(effective_period, income_window_start_day)
    prev_period = _shift_period(effective_period, -1)

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

    per_account_identities = {account_id: core.identity_pair_set(values) for account_id, values in identities.items()}
    selected_accounts = frozenset(account_component_timestamps) | {row["account_id"] for row in transactions}
    other_owned = _other_owned_identities(per_account_identities)
    future_accepted = _future_accepted_keys(transfers, evaluation_date)

    configured = settings is not None
    include_series = configured and history is not None
    # Household reads ask for the selected month's daily totals without the chart window.
    # Unconfigured months stay zero-filled so the series does not publish a withheld summary.
    track_selected_daily = include_series or (household_daily and configured)
    window_periods = _chart_periods(effective_period, history) if include_series else [effective_period]
    buckets: dict[str, _BucketAccum] = {}
    for p in window_periods:
        p_start, p_end = core.parse_period(p)
        p_cutoff = evaluation_date if p == current_month else p_end
        buckets[p] = _BucketAccum(p, p_start, p_cutoff)

    income_set = frozenset(settings.income_category_ids) if settings is not None else frozenset()
    pass_through_cfg = settings.pass_through if settings is not None else None
    rent_cat_id = pass_through_cfg.rent_category_id if pass_through_cfg is not None else None
    mortgage_cat_id = pass_through_cfg.mortgage_category_id if pass_through_cfg is not None else None

    tx_latest_by_account: dict[str, float] = {}
    purchase_date_fallback_count = 0
    ordinary_by_date: dict[str, list[str]] = defaultdict(list)
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
        unresolved_bank_repayment = False
        if (
            eff["source"] == "default"
            and kind == "expense"
            and core.is_bank_card_repayment(payload, row["metadata"])
        ):
            if core.bank_card_transfer_allowed(
                acct_id,
                row["metadata"],
                selected_accounts=selected_accounts,
                account_identities=per_account_identities,
            ):
                kind = "transfer"
            else:
                unresolved_bank_repayment = True

        cal_period = report_date[:7]
        cal_bucket = buckets.get(cal_period)
        in_calendar = cal_bucket is not None and cal_bucket.start <= report_date <= cal_bucket.cutoff
        inc_period = (
            _income_period_for_posting_date(posting_date, income_window_start_day)
            if configured and direction == "CRDT" and kind == "income" and cat_id in income_set
            else None
        )
        inc_bucket = None if inc_period is None else buckets.get(inc_period)

        if in_calendar or inc_bucket is not None:
            nonzero = core.compare_money(amount, _ZERO) > 0
            unresolved = _has_unresolved_transfer_evidence(
                row,
                counterpart_outside_horizon=counterpart_outside,
                other_identities=other_owned.get(acct_id, frozenset()),
                unresolved_bank_repayment=unresolved_bank_repayment,
            )
            row_key = f"{acct_id}/{row['entry_reference']}"
            if in_calendar:
                cal_bucket.has_activity = True
                if direction == "DBIT" and kind == "expense":
                    if cal_period == effective_period and tx_date is None:
                        purchase_date_fallback_count += 1
                    if nonzero:
                        cal_bucket.has_affecting_unexcluded = True
                        if unresolved:
                            cal_bucket.record_pending(row_key)
                    cal_bucket.observed_spend_categories.add(cat_id)
                    if mortgage_cat_id is not None and cat_id == mortgage_cat_id:
                        cal_bucket.mortgage_debits_legs.append(
                            _MortgageLeg(report_date, acct_id, row["entry_reference"], amount),
                        )
                    else:
                        cal_bucket.ordinary_expenses[cat_id].append(amount)
                        _note_one_off(cal_bucket, row["annotation"], cat_id, amount, refund=False,
                                      account_id=acct_id, entry_reference=row["entry_reference"])
                        if track_selected_daily and cal_period == effective_period:
                            ordinary_by_date[report_date].append(amount)
                elif direction == "CRDT":
                    if kind == "refund":
                        if nonzero:
                            cal_bucket.has_affecting_unexcluded = True
                            if unresolved:
                                cal_bucket.record_pending(row_key)
                        cal_bucket.observed_spend_categories.add(cat_id)
                        if mortgage_cat_id is not None and cat_id == mortgage_cat_id:
                            cal_bucket.mortgage_refund_legs.append(
                                _MortgageLeg(report_date, acct_id, row["entry_reference"], amount),
                            )
                        else:
                            cal_bucket.ordinary_refunds[cat_id].append(amount)
                            _note_one_off(cal_bucket, row["annotation"], cat_id, amount, refund=True,
                                          account_id=acct_id, entry_reference=row["entry_reference"])
                            if track_selected_daily and cal_period == effective_period:
                                ordinary_by_date[report_date].append(core.negate_money(amount))
                    elif kind == "income" and rent_cat_id is not None and cat_id == rent_cat_id:
                        if nonzero:
                            cal_bucket.has_affecting_unexcluded = True
                            if unresolved:
                                cal_bucket.record_pending(row_key)
                        cal_bucket.rent_amounts.append(amount)
            if inc_bucket is not None:
                inc_bucket.has_activity = True
                if nonzero:
                    inc_bucket.has_affecting_unexcluded = True
                    if unresolved:
                        inc_bucket.record_pending(row_key)
                    if inc_period == effective_period == current_month:
                        current_income_by_cat[cat_id].append(_IncomeCredit(
                            acct_id, row["entry_reference"], amount,
                            date.fromisoformat(posting_date).day, _normalize_debtor_name(row["metadata"]),
                        ))
                inc_bucket.start_budget_amounts.append(amount)

        if (inc_period == prev_period and effective_period == current_month
                and settings.income_category_ids and core.compare_money(amount, _ZERO) > 0):
            prior_income_by_cat[cat_id].append(_IncomeCredit(
                acct_id, row["entry_reference"], amount,
                date.fromisoformat(posting_date).day, _normalize_debtor_name(row["metadata"]),
            ))

    selected_bucket = buckets[effective_period]
    ledger_as_of, stale_selected_accounts = _compute_ledger_freshness(
        account_component_timestamps, tx_latest_by_account, float(evaluated_at),
    )
    transfer_scan_limited = bool(scan_limited and selected_bucket.has_affecting_unexcluded)
    provisional = selected_bucket.pending_transfer_rows > 0 or transfer_scan_limited
    available_currencies = sorted(observed_currencies, key=lambda code: (code != "PLN", code != "EUR", code))

    coverage = {
        "ledger_as_of": ledger_as_of,
        "stale_selected_accounts": stale_selected_accounts,
        "purchase_date_fallback_count": purchase_date_fallback_count,
        "pending_transfer_rows": selected_bucket.pending_transfer_rows,
        "pending_row_keys": selected_bucket.pending_row_keys,
        "pending_row_keys_truncated": selected_bucket.pending_transfer_rows > len(selected_bucket.pending_row_keys),
        "transfer_scan_limited": transfer_scan_limited,
        "provisional": provisional,
        "has_activity": selected_bucket.has_activity,
    }
    card_exposure = {"status": "unavailable", "amount": None, "as_of_date": None}
    card_debt = _compute_card_debt(
        account_kinds=account_kinds,
        account_balances=account_balances,
        account_credit_limits=account_credit_limits,
        account_balance_timestamps=account_balance_timestamps,
        target_currency=target_currency,
        evaluation_date=evaluation_date,
        evaluated_at=float(evaluated_at),
    )
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
        "card_debt": card_debt,
    }

    if not configured:
        unconfigured = {
            **envelope,
            "configuration": {"status": "unconfigured", "source": "none"},
            "summary": None,
            "expected_income": None,
            "pace": None,
            "category_spend": [],
            "pass_through": None,
        }
        if household_daily:
            unconfigured["_series_daily"] = _build_daily_series(start, cutoff, {}, {}, {})
        return unconfigured, None

    pass_through_wire, adj_exp_dates, adj_ref_dates, selected_rows, start_budget, spent = _bucket_category_totals(
        selected_bucket, pass_through_cfg, extra_categories=set(active_limits),
    )
    # The split row shows only the remainder. The badge counts the full category total,
    # and a parent limit adds descendant totals the same way. Those are the amounts an
    # unlabelled month would use: a one-off label is not a budget exemption. One-off rows
    # carry no limit, so the badge is not shown twice.
    full_by_category = {cat_id: (exp, ref) for cat_id, exp, ref, _net in selected_rows}
    child_map = _category_children(categories_by_id)
    category_spend: list[dict] = []
    for item in _split_labelled(
        selected_rows, selected_bucket.one_offs, keep_ids=set(active_limits), one_off_secret=one_off_secret,
    ):
        cat_id = item["category_id"]
        category_meta = None if cat_id is None else categories_by_id[cat_id]
        if item["one_off"] or cat_id is None:
            limit = None
        else:
            full_expenses, full_refunds = full_by_category[cat_id]
            if cat_id in active_limits:
                rolled = _subtree_limit_amounts(cat_id, full_by_category, child_map)
                if rolled is not None:
                    full_expenses, full_refunds = rolled
            limit = _evaluate_category_limit(full_expenses, full_refunds, active_limits.get(cat_id))
        category_spend.append({
            "category_id": cat_id,
            "name": item["name"] if item["one_off"] else (None if category_meta is None else category_meta["name"]),
            "archived": False if category_meta is None else category_meta["archived"],
            "expenses": item["expenses"],
            "refunds": item["refunds"],
            "net_spending": item["net_spending"],
            "limit": limit,
            "one_off": item["one_off"],
            "one_off_key": item["one_off_key"],
        })

    remaining = _sub_money(start_budget, spent)
    summary = {"start_budget": start_budget, "spent": spent, "remaining": remaining}
    expected_income = _compute_expected_income(
        effective_period, current_month, income_start, income_end,
        settings.income_category_ids, prior_income_by_cat, current_income_by_cat,
    )
    pace = _compute_pace(effective_period, current_month, evaluation_date, start_budget, spent, expected_income)

    configuration = {"status": "ready", "source": "bootstrap"}
    if income_window_start_day > 1:
        configuration["income_window"] = {
            "start": income_start,
            "end": income_end,
            "start_day": income_window_start_day,
        }

    budget_wire = {
        **envelope,
        "configuration": configuration,
        "summary": summary,
        "expected_income": expected_income,
        "pace": pace,
        "category_spend": category_spend,
        "pass_through": pass_through_wire,
    }
    if household_daily:
        points = _build_daily_series(start, cutoff, ordinary_by_date, adj_exp_dates, adj_ref_dates)
        if points[-1]["cumulative_spent"] != spent:
            raise MonthDataError()
        budget_wire["_series_daily"] = points
    if not include_series:
        return budget_wire, None

    daily = _build_daily_series(start, cutoff, ordinary_by_date, adj_exp_dates, adj_ref_dates)
    months_wire: list[dict] = []
    seen_label_ids: set[str] = set()
    for p in window_periods:
        bucket = buckets[p]
        if p == effective_period:
            b_start_budget, b_spent = start_budget, spent
            b_categories = _series_categories(
                selected_rows, bucket.one_offs, observed=bucket.observed_spend_categories,
                one_off_secret=one_off_secret,
            )
            b_provisional = provisional
            b_partial = expected_income["status"] == "partial"
        else:
            _, _, _, b_rows, b_start_budget, b_spent = _bucket_category_totals(bucket, pass_through_cfg)
            b_categories = _series_categories(b_rows, bucket.one_offs, one_off_secret=one_off_secret)
            b_provisional = bucket.pending_transfer_rows > 0 or bool(scan_limited and bucket.has_affecting_unexcluded)
            b_partial = False
        for item in b_categories:
            if item["category_id"] is not None:
                seen_label_ids.add(item["category_id"])
        months_wire.append({
            "period": p,
            "has_activity": bucket.has_activity,
            "is_current_month": p == current_month,
            "income_estimate_partial": b_partial,
            "provisional": b_provisional,
            "start_budget": b_start_budget,
            "spent": b_spent,
            "categories": b_categories,
        })
    category_labels = [
        {"category_id": cid, "name": categories_by_id[cid]["name"], "archived": categories_by_id[cid]["archived"]}
        for cid in sorted(seen_label_ids)
    ]
    return budget_wire, {"daily": daily, "months": months_wire, "category_labels": category_labels}


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
    account_balances: Mapping[str, Sequence[Mapping]] | None = None,
    account_credit_limits: Mapping[str, str | None] | None = None,
    account_balance_timestamps: Mapping[str, int | float | None] | None = None,
    one_off_secret: bytes | None = None,
    include_series: bool = False,
) -> dict:
    """Pure single-snapshot evaluation for ``GET /api/budget/analytics/monthly-budget``.

    ``include_series`` attaches a private ``_series_daily`` list for the household
    serializer. The owner response omits it. The flag does not change any public field.
    """
    budget, _ = _evaluate_monthly_budget_core(
        period=period, currency=currency, evaluated_at=evaluated_at, transactions=transactions,
        transfers=transfers, scan_limited=scan_limited, categories=categories, rules=rules,
        identities=identities, limits=limits, account_component_timestamps=account_component_timestamps,
        account_kinds=account_kinds, settings=settings, history=None,
        account_balances=account_balances, account_credit_limits=account_credit_limits,
        account_balance_timestamps=account_balance_timestamps, one_off_secret=one_off_secret,
        household_daily=include_series,
    )
    return budget


def evaluate_monthly_budget_charts(
    *,
    owner: str,
    period: str | None,
    currency: str | None,
    history: int = DEFAULT_HISTORY_MONTHS,
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
    account_balances: Mapping[str, Sequence[Mapping]] | None = None,
    account_credit_limits: Mapping[str, str | None] | None = None,
    account_balance_timestamps: Mapping[str, int | float | None] | None = None,
    one_off_secret: bytes | None = None,
) -> dict:
    """Pure single-snapshot evaluation for ``GET /api/budget/analytics/monthly-budget/charts``."""
    if type(history) is not int or history not in ALLOWED_HISTORY_MONTHS:
        raise MonthInputError()
    budget, series = _evaluate_monthly_budget_core(
        period=period, currency=currency, evaluated_at=evaluated_at, transactions=transactions,
        transfers=transfers, scan_limited=scan_limited, categories=categories, rules=rules,
        identities=identities, limits=limits, account_component_timestamps=account_component_timestamps,
        account_kinds=account_kinds, settings=settings, history=history,
        account_balances=account_balances, account_credit_limits=account_credit_limits,
        account_balance_timestamps=account_balance_timestamps, one_off_secret=one_off_secret,
    )
    return {"budget": budget, "series": series}
