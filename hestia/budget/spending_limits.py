"""Pure monthly category spending-limit evaluation: organisational aids only.

Evaluates optional owner-configured monthly category limits against exact server-side
category spending totals, separately per currency. Uses arbitrary-precision scaled
integers only (never floats, SQLite REAL or implicit Decimal precision), performs no
currency conversion, and never mutates accounts, categories or ledger records.

Errors carry fixed messages and never echo amounts, category IDs or owners.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re

RULE_VERSION = 1
DEFAULT_WARNING_PERCENT = 80
MAX_LIMITS = 1_000
MAX_SPENDING = 10_000
MAX_TEXT = 1024
MAX_CATEGORY_ID = 128
MAX_AMOUNT = 80
STATUSES = ("within", "approaching", "at_limit", "exceeded")

_ZERO = "0"
_UNCATEGORISED = "uncategorised"
_PERIOD = re.compile(r"([0-9]{4})-([0-9]{2})\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SpendingLimitInputError(ValueError):
    """Malformed or out-of-scope input; the message never contains input values."""

    def __init__(self):
        super().__init__("Invalid spending limit input")


class SpendingLimitCapError(ValueError):
    """Too many limits or spending rows to examine; no partial result is returned."""

    def __init__(self):
        super().__init__("Spending limit evaluation cap exceeded")


@dataclass(frozen=True, repr=False)
class CategoryLimit:
    owner: str
    category_id: str
    currency: str
    monthly_limit: str
    enabled: bool = True
    warning_percent: int = DEFAULT_WARNING_PERCENT


@dataclass(frozen=True, repr=False)
class CategorySpending:
    owner: str
    category_id: str | None
    currency: str
    expenses: str
    refunds: str = _ZERO
    archived: bool = False


@dataclass(frozen=True, repr=False)
class CategoryLimitStatus:
    period: str
    category_id: str
    currency: str
    monthly_limit: str
    expenses: str
    refunds: str
    net_spending: str
    remaining: str
    over_by: str
    percent_used_floor: int
    warning_percent: int
    status: str
    archived: bool
    rule_version: int = RULE_VERSION


def _text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or len(value) > maximum or not value.strip() or _CONTROL.search(value):
        raise SpendingLimitInputError()
    return value


def _category_id(value, *, allow_none=False):
    if allow_none and value is None:
        return None
    cleaned = _text(value, MAX_CATEGORY_ID)
    if cleaned == _UNCATEGORISED:
        raise SpendingLimitInputError()
    return cleaned


def _currency(value):
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise SpendingLimitInputError()
    return value


def _period(value):
    match = _PERIOD.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise SpendingLimitInputError()
    year, month = int(match[1]), int(match[2])
    if not 1900 <= year <= 9999 or not 1 <= month <= 12:
        raise SpendingLimitInputError()
    return value


def _parse_amount(value, *, positive=False):
    if not isinstance(value, str) or len(value) > MAX_AMOUNT or not _AMOUNT.fullmatch(value):
        raise SpendingLimitInputError()
    integer, _, fraction = value.partition(".")
    fraction = fraction.rstrip(_ZERO)
    coefficient = int((integer.lstrip(_ZERO) or _ZERO) + fraction)
    if positive and coefficient <= 0:
        raise SpendingLimitInputError()
    return coefficient, len(fraction)


def _format_scaled(coefficient, scale):
    negative = coefficient < 0
    digits = str(abs(coefficient)).zfill(scale + 1)
    if scale:
        integer, fraction = digits[:-scale], digits[-scale:].rstrip(_ZERO)
        result = integer + ("." + fraction if fraction else "")
    else:
        result = digits
    return ("-" + result) if negative and coefficient else result


def _subtract(left, right):
    lc, ls = left
    rc, rs = right
    scale = max(ls, rs)
    return lc * (10 ** (scale - ls)) - rc * (10 ** (scale - rs)), scale


def _iterable(values):
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        raise SpendingLimitInputError()
    return values


def _scope(owner, period, max_limits, max_spending):
    owner = _text(owner)
    period = _period(period)
    if type(max_limits) is not int or max_limits < 0 or type(max_spending) is not int or max_spending < 0:
        raise SpendingLimitInputError()
    return owner, period


def _validate_limit(owner, item, seen):
    if (not isinstance(item, CategoryLimit) or item.owner != owner
            or type(item.enabled) is not bool
            or type(item.warning_percent) is not int or not 1 <= item.warning_percent <= 99):
        raise SpendingLimitInputError()
    key = (_category_id(item.category_id), _currency(item.currency))
    if key in seen:
        raise SpendingLimitInputError()
    seen.add(key)
    amount = _parse_amount(item.monthly_limit, positive=True)
    return (key, amount, item.warning_percent) if item.enabled else None


def _validated_limits(owner, limits, max_limits):
    seen, active = set(), []
    for count, item in enumerate(_iterable(limits), 1):
        if count > max_limits:
            raise SpendingLimitCapError()
        validated = _validate_limit(owner, item, seen)
        if validated is not None:
            active.append(validated)
    return active


def _validated_spending(owner, spending, max_spending):
    totals = {}
    for count, item in enumerate(_iterable(spending), 1):
        if count > max_spending:
            raise SpendingLimitCapError()
        if not isinstance(item, CategorySpending) or item.owner != owner or type(item.archived) is not bool:
            raise SpendingLimitInputError()
        key = (_category_id(item.category_id, allow_none=True), _currency(item.currency))
        if key in totals:
            raise SpendingLimitInputError()
        totals[key] = (_parse_amount(item.expenses), _parse_amount(item.refunds), item.archived)
    return totals


def _status(net_aligned, limit_aligned, warning_percent):
    if net_aligned > limit_aligned:
        return "exceeded"
    if net_aligned == limit_aligned:
        return "at_limit"
    if net_aligned * 100 >= limit_aligned * warning_percent:
        return "approaching"
    return "within"


def _evaluate_one(period, key, limit_amount, warning_percent, observed):
    category_id, currency = key
    expenses, refunds, archived = ((0, 0), (0, 0), False) if observed is None else observed
    net_c, net_s = _subtract(expenses, refunds)
    limit_c, limit_s = limit_amount
    scale = max(net_s, limit_s)
    net_aligned = net_c * (10 ** (scale - net_s))
    limit_aligned = limit_c * (10 ** (scale - limit_s))
    margin = limit_aligned - net_aligned
    remaining = _format_scaled(margin, scale) if margin > 0 else _ZERO
    over_by = _format_scaled(-margin, scale) if margin < 0 else _ZERO
    percent_used_floor = 0 if net_aligned <= 0 else (net_aligned * 100) // limit_aligned
    return CategoryLimitStatus(
        period=period,
        category_id=category_id,
        currency=currency,
        monthly_limit=_format_scaled(limit_c, limit_s),
        expenses=_format_scaled(*expenses),
        refunds=_format_scaled(*refunds),
        net_spending=_format_scaled(net_c, net_s),
        remaining=remaining,
        over_by=over_by,
        percent_used_floor=percent_used_floor,
        warning_percent=warning_percent,
        status=_status(net_aligned, limit_aligned, warning_percent),
        archived=archived,
    )


def evaluate_spending_limits(owner: str, period: str, limits: Iterable[CategoryLimit],
                             spending: Iterable[CategorySpending], *,
                             max_limits: int = MAX_LIMITS,
                             max_spending: int = MAX_SPENDING) -> tuple[CategoryLimitStatus, ...]:
    """Evaluate active monthly category limits for ``period`` (YYYY-MM), deterministically ordered.

    Disabled limits are validated for owner scope, shape and duplicates, then excluded
    from output. Active limits without matching spending rows evaluate against zero.
    Uncategorised spending rows (``category_id=None``) never match a category limit.
    """
    owner, period = _scope(owner, period, max_limits, max_spending)
    active = _validated_limits(owner, limits, max_limits)
    totals = _validated_spending(owner, spending, max_spending)
    results = [_evaluate_one(period, key, limit_amount, warning_percent, totals.get(key))
               for key, limit_amount, warning_percent in active]
    results.sort(key=lambda item: (item.currency, item.category_id))
    return tuple(results)
