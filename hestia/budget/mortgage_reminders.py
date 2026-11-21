"""Pure private mortgage reminder schedule evaluation (Budget 2D).

Evaluates owner-configured monthly mortgage schedules against an explicit ``today``
date and ledger freshness indicator. Enforces exact decimal amounts, calendar month-end
clamping (including leap years), owner-private notification destinations only (never
shared calendars), and introduces no bank payment or external calendar side effects.

Errors carry fixed messages and never echo amounts, labels, targets or owners.
"""
from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
import re

RULE_VERSION = 1
DEFAULT_REMIND_DAYS = 5
DEFAULT_STALE_AFTER_DAYS = 7
MAX_SCHEDULES = 100
MAX_PAID_MONTHS = 1_200
MAX_TEXT = 1024
MAX_ID = 128
MAX_LABEL = 256
MAX_AMOUNT = 80
PRIVATE_DESTINATIONS = frozenset({"owner_inbox", "owner_push", "owner_email"})
STATUSES = ("overdue", "due_today", "due_soon", "upcoming", "completed")

_ZERO = "0"
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_AMOUNT = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z", re.ASCII)
_MONTH = re.compile(r"([0-9]{4})-([0-9]{2})\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class MortgageInputError(ValueError):
    """Malformed or out-of-scope input; the message never contains input values."""

    def __init__(self):
        super().__init__("Invalid mortgage reminder input")


class MortgageLimitError(ValueError):
    """Too many schedules or paid-month markers; no partial result is returned."""

    def __init__(self):
        super().__init__("Mortgage reminder limit exceeded")


@dataclass(frozen=True, repr=False)
class MortgageSchedule:
    owner: str
    schedule_id: str
    label: str
    amount: str
    currency: str
    due_day: int
    start_date: str
    destination_kind: str
    destination_target: str | None = None
    end_date: str | None = None
    remind_days_before: int = DEFAULT_REMIND_DAYS
    paid_months: frozenset[str] = frozenset()
    enabled: bool = True


@dataclass(frozen=True, repr=False)
class MortgageReminder:
    schedule_id: str
    label: str
    amount: str
    currency: str
    due_date: str | None
    days_until_due: int | None
    status: str
    reminder_active: bool
    destination_kind: str
    destination_target: str | None
    freshness_stale: bool
    ledger_as_of: str | None
    rule_version: int = RULE_VERSION


def _text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or len(value) > maximum or not value.strip() or _CONTROL.search(value):
        raise MortgageInputError()
    return value


def _currency(value):
    if not isinstance(value, str) or not _CURRENCY.fullmatch(value):
        raise MortgageInputError()
    return value


def _parse_date(value):
    match = _DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None or not 1900 <= int(match[1]) <= 9999:
        raise MortgageInputError()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise MortgageInputError() from None


def _parse_month(value):
    match = _MONTH.fullmatch(value) if isinstance(value, str) else None
    if match is None or not 1900 <= int(match[1]) <= 9999 or not 1 <= int(match[2]) <= 12:
        raise MortgageInputError()
    return value


def _parse_amount(value):
    if not isinstance(value, str) or len(value) > MAX_AMOUNT or not _AMOUNT.fullmatch(value):
        raise MortgageInputError()
    integer, _, fraction = value.partition(".")
    integer, fraction = integer.lstrip(_ZERO) or _ZERO, fraction.rstrip(_ZERO)
    if integer == _ZERO and not fraction:
        raise MortgageInputError()
    return integer + ("." + fraction if fraction else "")


def _parse_paid_months(values):
    if not isinstance(values, (set, frozenset)):
        raise MortgageInputError()
    if len(values) > MAX_PAID_MONTHS:
        raise MortgageLimitError()
    return frozenset(_parse_month(item) for item in values)


def _due_date(year, month, due_day):
    return date(year, month, min(due_day, calendar.monthrange(year, month)[1]))


def installment_due_date(due_day: int, start_date: str, end_date: str | None, month: str) -> str | None:
    """Return the actual clamped ISO due date for ``month`` (YYYY-MM) iff it falls in ``[start_date, end_date]``.

    Never shifts a due date onto ``start_date`` or ``end_date``; month-end days clamp to
    the real last day of that calendar month (including leap years).
    """
    if type(due_day) is not int or not 1 <= due_day <= 31:
        raise MortgageInputError()
    start = _parse_date(start_date)
    end = None if end_date is None else _parse_date(end_date)
    if end is not None and end < start:
        raise MortgageInputError()
    parsed = _parse_month(month)
    due = _due_date(int(parsed[:4]), int(parsed[5:7]), due_day)
    if due < start or (end is not None and due > end):
        return None
    return due.isoformat()


def _next_month(year, month):
    if month < 12:
        return year, month + 1
    return None if year >= 9999 else (year + 1, 1)


def _next_due_date(due_day, start, end, paid_months):
    ym = (start.year, start.month)
    while ym is not None:
        year, month = ym
        candidate = _due_date(year, month, due_day)
        if end is not None and candidate > end:
            return None
        if candidate >= start and f"{year:04d}-{month:02d}" not in paid_months:
            return candidate
        ym = _next_month(year, month)
    return None


def _status(days_until_due, remind_days_before):
    if days_until_due is None:
        return "completed", False
    if days_until_due < 0:
        return "overdue", True
    if days_until_due == 0:
        return "due_today", True
    if days_until_due <= remind_days_before:
        return "due_soon", True
    return "upcoming", False


def _scope(owner, today, ledger_as_of, stale_after_days, max_schedules):
    owner = _text(owner)
    today_date = _parse_date(today)
    ledger_date = None if ledger_as_of is None else _parse_date(ledger_as_of)
    if ledger_date is not None and ledger_date > today_date:
        raise MortgageInputError()
    if (type(stale_after_days) is not int or not 0 <= stale_after_days <= 365
            or type(max_schedules) is not int or max_schedules < 0):
        raise MortgageInputError()
    freshness_stale = ledger_date is None or (today_date - ledger_date).days > stale_after_days
    return owner, today_date, freshness_stale


def _validate_schedule(owner, item, seen):
    if (not isinstance(item, MortgageSchedule) or item.owner != owner
            or type(item.enabled) is not bool
            or type(item.due_day) is not int or not 1 <= item.due_day <= 31
            or type(item.remind_days_before) is not int or not 0 <= item.remind_days_before <= 31
            or not isinstance(item.destination_kind, str) or item.destination_kind not in PRIVATE_DESTINATIONS):
        raise MortgageInputError()
    schedule_id = _text(item.schedule_id, MAX_ID)
    if schedule_id in seen:
        raise MortgageInputError()
    seen.add(schedule_id)
    label = _text(item.label, MAX_LABEL)
    amount = _parse_amount(item.amount)
    currency = _currency(item.currency)
    start = _parse_date(item.start_date)
    end = None if item.end_date is None else _parse_date(item.end_date)
    if end is not None and end < start:
        raise MortgageInputError()
    target = None if item.destination_target is None else _text(item.destination_target, MAX_LABEL)
    paid = _parse_paid_months(item.paid_months)
    if not item.enabled:
        return None
    return (schedule_id, label, amount, currency, item.due_day, start, end,
            item.remind_days_before, item.destination_kind, target, paid)


def evaluate_mortgage_reminders(owner: str, today: str, schedules: Iterable[MortgageSchedule], *,
                                ledger_as_of: str | None = None,
                                stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
                                max_schedules: int = MAX_SCHEDULES) -> tuple[MortgageReminder, ...]:
    """Return deterministically ordered private mortgage reminders for ``today`` (YYYY-MM-DD).

    Finds each enabled schedule's earliest unpaid qualifying installment whose actual
    clamped due date satisfies ``start_date <= due_date <= end_date`` (never shifting a
    due date onto a boundary). Unpaid overdue installments remain visible even after
    subsequent months or ``end_date``; ``end_date`` stops new installments from being
    generated. A reminder lead window (``remind_days_before``) may become active before
    ``start_date`` when the first due date is within the lead window. Valid ``paid_months``
    outside the schedule window are safely ignored. Only owner-private notification
    destinations are accepted; shared calendars are rejected even when disabled.
    """
    owner, today_date, freshness_stale = _scope(owner, today, ledger_as_of, stale_after_days, max_schedules)
    if not isinstance(schedules, Iterable) or isinstance(schedules, (str, bytes, Mapping)):
        raise MortgageInputError()
    seen, results = set(), []
    for count, item in enumerate(schedules, 1):
        if count > max_schedules:
            raise MortgageLimitError()
        validated = _validate_schedule(owner, item, seen)
        if validated is None:
            continue
        (schedule_id, label, amount, currency, due_day, start, end,
         remind_days, destination_kind, target, paid) = validated
        due = _next_due_date(due_day, start, end, paid)
        days = None if due is None else (due - today_date).days
        status, active = _status(days, remind_days)
        results.append(MortgageReminder(
            schedule_id=schedule_id,
            label=label,
            amount=amount,
            currency=currency,
            due_date=None if due is None else due.isoformat(),
            days_until_due=days,
            status=status,
            reminder_active=active,
            destination_kind=destination_kind,
            destination_target=target,
            freshness_stale=freshness_stale,
            ledger_as_of=ledger_as_of,
        ))
    results.sort(key=lambda item: (item.due_date is None, item.due_date or "", item.currency, item.schedule_id))
    return tuple(results)
