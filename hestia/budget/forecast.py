"""Project owner-private upcoming commitments from a single analytics snapshot."""
from __future__ import annotations

import calendar
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import chain
import re

from .analytics_core import canonical_money, compare_money, sum_money
from .mortgage_reminders import (DEFAULT_STALE_AFTER_DAYS, MortgageInputError, MortgageLimitError,
                                 MortgageSchedule, evaluate_mortgage_reminders, installment_due_date)
from .recurring import RecurringHint

RULE_VERSION = 1
LAPSED_AFTER_DAYS = 45
MAX_FORECAST_HINTS = 200
MAX_FORECAST_ITEMS = 3_600
MAX_FORECAST_LABEL = 256
MAX_STALE_ACCOUNT_IDS = 200
_MAX_MATCH_DISTANCE = 5
_FIRST_MONTH = 1900 * 12
_LAST_MONTH = 9999 * 12 + 11


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


def _bounded_label(label):
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
                      snapshot_digest: str, schedules_version: str) -> dict:
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
    audits, remaining = _deduplicate(enabled, active)
    projected = [_project_month(month, evaluation_date, enabled, remaining) for month in horizon]
    overdue_schedules = sum(reminder.due_date is not None and reminder.due_date[:7] < m0
                            and (currency is None or reminder.currency == currency) for reminder in reminders)
    if sum(map(len, projected)) > MAX_FORECAST_ITEMS:
        raise ForecastLimitError()

    month_rows = []
    for month, items in zip(horizon, projected):
        for item, _ in items:
            item["label"] = _bounded_label(item["label"])
        items.sort(key=_item_sort_key)
        wire_items = [item for item, _ in items]
        month_rows.append({"month": month, "currencies": _currency_totals(wire_items), "items": wire_items})
    for audit in audits:
        audit["schedule_label"] = _bounded_label(audit["schedule_label"])
        audit["recurring_label"] = _bounded_label(audit["recurring_label"])
    audits.sort(key=lambda audit: (audit["currency"], audit["matched_due_date"], audit["schedule_id"]))

    return {
        "evaluation_date": evaluation_date, "evaluation_timezone": "UTC", "horizon_months": months,
        "start_month": m0, "end_month": horizon[-1], "ledger_as_of": ledger_as_of,
        "freshness_stale": not account_timestamps or bool(stale_accounts),
        "stale_account_ids": sorted(stale_accounts)[:MAX_STALE_ACCOUNT_IDS],
        "stale_account_ids_truncated": len(stale_accounts) > MAX_STALE_ACCOUNT_IDS,
        "snapshot_digest": snapshot_digest, "schedules_version": schedules_version, "rule_version": RULE_VERSION,
        "summary": {"selected_accounts": len(account_timestamps), "excluded_accounts": excluded_accounts,
                    "stale_accounts": len(stale_accounts), "overdue_mortgage_schedule_count": overdue_schedules,
                    "lapsed_recurring_count": lapsed_count, "deduped_recurring_count": len(audits),
                    "stale_recurring_count": stale_count},
        "totals": _currency_totals(chain.from_iterable(month["items"] for month in month_rows)),
        "deduplications": audits, "months": month_rows,
    }
