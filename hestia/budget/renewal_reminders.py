"""Pure private bank-consent renewal reminder evaluation (Budget 2D).

Evaluates owner-configured bank connections using each provider's actual
``valid_until`` timestamp — never assuming a universal 180-day consent lifetime —
together with explicit bank ``attention`` states. Enforces owner-private
notification destinations only (rejecting shared calendars), performs no bank
requests and never initiates consent renewal or modifies stored credentials.

Errors carry fixed messages and never echo connection IDs, bank names, targets
or owners.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
import re

RULE_VERSION = 1
SECONDS_PER_DAY = 86_400
DEFAULT_LEAD_DAYS = (14, 7, 3, 1)
MAX_CONNECTIONS = 100
MAX_LEAD_DAYS = 16
MAX_NOTIFIED = 32
MAX_TEXT = 1024
MAX_ID = 128
MAX_LABEL = 256
PRIVATE_DESTINATIONS = frozenset({"owner_inbox", "owner_push", "owner_email"})
STATUSES = ("attention_required", "expired", "due_soon", "healthy", "unknown_expiry")

_STATUS_ORDER = {name: index for index, name in enumerate(STATUSES)}
_ATTENTION = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RenewalInputError(ValueError):
    """Malformed or out-of-scope input; the message never contains input values."""

    def __init__(self):
        super().__init__("Invalid renewal reminder input")


class RenewalLimitError(ValueError):
    """Too many connections, lead tiers or notified markers; no partial result."""

    def __init__(self):
        super().__init__("Renewal reminder limit exceeded")


@dataclass(frozen=True, repr=False)
class ConsentRenewalTarget:
    owner: str
    connection_id: str
    bank_name: str
    valid_until: int | float | None
    destination_kind: str
    destination_target: str
    enabled: bool = True
    paused: bool = False
    attention: str | None = None
    notified_milestones: frozenset[int] = frozenset()


@dataclass(frozen=True, repr=False)
class ConsentRenewalReminder:
    connection_id: str
    bank_name: str
    valid_until: float | None
    seconds_remaining: int | None
    days_remaining: int | None
    status: str
    active_milestone_days: int | None
    reminder_due: bool
    paused: bool
    attention: str | None
    destination_kind: str
    destination_target: str
    rule_version: int = RULE_VERSION


def _text(value, maximum=MAX_TEXT):
    if not isinstance(value, str) or len(value) > maximum or not value.strip() or _CONTROL.search(value):
        raise RenewalInputError()
    return value


def _timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RenewalInputError()
    return float(value)


def _iterable(values):
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        raise RenewalInputError()
    return values


def _parse_lead_days(values):
    seen = set()
    for count, item in enumerate(_iterable(values), 1):
        if count > MAX_LEAD_DAYS:
            raise RenewalLimitError()
        if type(item) is not int or not 1 <= item <= 365 or item in seen:
            raise RenewalInputError()
        seen.add(item)
    if not seen:
        raise RenewalInputError()
    return tuple(sorted(seen))


def _parse_notified(values):
    if not isinstance(values, (set, frozenset)):
        raise RenewalInputError()
    if len(values) > MAX_NOTIFIED:
        raise RenewalLimitError()
    for item in values:
        if type(item) is not int or not 0 <= item <= 365:
            raise RenewalInputError()
    return frozenset(values)


def _parse_attention(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _ATTENTION.fullmatch(value):
        raise RenewalInputError()
    return value


def _classify(seconds_remaining, attention, lead_days, notified):
    if attention is not None:
        return "attention_required", 0, 0 not in notified
    if seconds_remaining is None:
        return "unknown_expiry", None, False
    if seconds_remaining <= 0:
        return "expired", 0, 0 not in notified
    for milestone in lead_days:
        if seconds_remaining <= milestone * SECONDS_PER_DAY:
            return "due_soon", milestone, milestone not in notified
    return "healthy", None, False


def _scope(owner, now, lead_days, max_connections):
    owner = _text(owner)
    now_ts = _timestamp(now)
    tiers = _parse_lead_days(lead_days)
    if type(max_connections) is not int or max_connections < 0:
        raise RenewalInputError()
    return owner, now_ts, tiers


def _validate_target(owner, item, seen):
    if (not isinstance(item, ConsentRenewalTarget) or item.owner != owner
            or type(item.enabled) is not bool or type(item.paused) is not bool
            or not isinstance(item.destination_kind, str) or item.destination_kind not in PRIVATE_DESTINATIONS):
        raise RenewalInputError()
    connection_id = _text(item.connection_id, MAX_ID)
    if connection_id in seen:
        raise RenewalInputError()
    seen.add(connection_id)
    bank_name = _text(item.bank_name, MAX_LABEL)
    valid_until = None if item.valid_until is None else _timestamp(item.valid_until)
    target = _text(item.destination_target, MAX_LABEL)
    attention = _parse_attention(item.attention)
    notified = _parse_notified(item.notified_milestones)
    if not item.enabled:
        return None
    return connection_id, bank_name, valid_until, item.paused, attention, item.destination_kind, target, notified


def evaluate_renewal_reminders(owner: str, now: int | float, targets: Iterable[ConsentRenewalTarget], *,
                               lead_days: Iterable[int] = DEFAULT_LEAD_DAYS,
                               max_connections: int = MAX_CONNECTIONS) -> tuple[ConsentRenewalReminder, ...]:
    """Return deterministically ordered private consent-renewal reminders for ``now``.

    Uses only each target's actual provider ``valid_until`` timestamp and explicit
    ``attention`` state (never assuming 180 days). Only owner-private notification
    destinations are permitted; shared calendars are rejected even when disabled.
    """
    owner, now_ts, tiers = _scope(owner, now, lead_days, max_connections)
    seen, results = set(), []
    for count, item in enumerate(_iterable(targets), 1):
        if count > max_connections:
            raise RenewalLimitError()
        validated = _validate_target(owner, item, seen)
        if validated is None:
            continue
        connection_id, bank_name, valid_until, paused, attention, dest_kind, dest_target, notified = validated
        seconds = None if valid_until is None else math.floor(valid_until - now_ts)
        days = None if seconds is None else seconds // SECONDS_PER_DAY
        status, milestone, due = _classify(seconds, attention, tiers, notified)
        results.append(ConsentRenewalReminder(
            connection_id=connection_id,
            bank_name=bank_name,
            valid_until=valid_until,
            seconds_remaining=seconds,
            days_remaining=days,
            status=status,
            active_milestone_days=milestone,
            reminder_due=due,
            paused=paused,
            attention=attention,
            destination_kind=dest_kind,
            destination_target=dest_target,
        ))
    results.sort(key=lambda item: (
        _STATUS_ORDER[item.status],
        item.seconds_remaining is None,
        0 if item.seconds_remaining is None else item.seconds_remaining,
        item.connection_id,
    ))
    return tuple(results)
