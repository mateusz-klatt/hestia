"""Pure Retry-After parsing, separate from durable retry and quota policy.

HTTP dates follow RFC 9110 sections 5.6.7 and 10.2.3. No six-hour ASPSP
minimum, sleeping, I/O, or interpretation of arbitrary provider text occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from fractions import Fraction
import math
import re


MAX_HEADER_CHARS = 128
MAX_AUTOMATIC_DELAY = 7 * 24 * 60 * 60
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAY = "(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_LONG_DAY = "(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MONTH = "(?:" + "|".join(_MONTHS) + ")"
_TIME = r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
_DATE_PATTERNS = (
    (re.compile(rf"(?P<weekday>{_DAY}), (?P<day>[0-9]{{2}}) (?P<month>{_MONTH}) "
                rf"(?P<year>[0-9]{{4}}) {_TIME} GMT", re.ASCII), False),
    (re.compile(rf"(?P<weekday>{_LONG_DAY}), (?P<day>[0-9]{{2}})-(?P<month>{_MONTH})-"
                rf"(?P<year>[0-9]{{2}}) {_TIME} GMT", re.ASCII), True),
    (re.compile(rf"(?P<weekday>{_DAY}) (?P<month>{_MONTH}) (?P<day>[0-9]{{2}}| [0-9]) "
                rf"{_TIME} (?P<year>[0-9]{{4}})", re.ASCII), False),
)
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ACCOUNT_ENDPOINT = re.compile(rf"/accounts/{_UUID}/(?:transactions|balances)", re.ASCII)


@dataclass(frozen=True)
class RetryHint:
    deadline: float | None
    suspended: bool


class RateScope(StrEnum):
    ACCOUNT_ENDPOINT = "account_endpoint"
    CONNECTION = "connection"
    APPLICATION = "application"


def rate_scope(path, provider_code) -> RateScope:
    """Recommend a policy scope from the documented ASPSP code and exact path.

    The Enable Banking API reference documents ASPSP_RATE_LIMIT_EXCEEDED.
    This identifies the source, not the bank's actual quota scope. Runtime policy
    must retain verified bank rules and shared provider/session cooldowns; this
    label never overrides an explicitly wider Retry-After. No application-wide
    code is documented, so unrecognized codes and paths retain connection scope.
    """
    if (provider_code == "ASPSP_RATE_LIMIT_EXCEEDED" and isinstance(path, str)
            and _ACCOUNT_ENDPOINT.fullmatch(path)):
        return RateScope.ACCOUNT_ENDPOINT
    return RateScope.CONNECTION


def _clock(value):
    try:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError("Invalid retry clock") from None
    return value


def _delta_deadline(seconds, now):
    """Round toward the future if an exact sum is not representable as float."""
    exact = Fraction(now) + seconds
    try:
        result = float(exact)
    except OverflowError:
        return None
    if Fraction(result) < exact:
        result = math.nextafter(result, math.inf)
    return result if math.isfinite(result) else None


def _rfc850_year(year, parts, now):
    current = datetime.fromtimestamp(now, timezone.utc)
    year = current.year + (year - current.year % 100) % 100
    proposed = (year, *parts)
    boundary = (current.year + 50, current.month, current.day, current.hour, current.minute, current.second)
    return year - 100 if proposed > boundary else year


def _date_deadline(match, obsolete, now):
    values = match.groupdict()
    try:
        year = int(values["year"])
        month = _MONTHS.index(values["month"]) + 1
        day, hour, minute, second = (int(values[key]) for key in ("day", "hour", "minute", "second"))
        if obsolete:
            year = _rfc850_year(year, (month, day, hour, minute, second), now)
        # HTTP dates explicitly define GMT, including the zone-less asctime form.
        stamp = datetime(year, month, day, hour, minute, min(second, 59), tzinfo=timezone.utc)
        if second > 60 or _WEEKDAYS[stamp.weekday()] != values["weekday"][:3]:
            return None
        return stamp.timestamp() + (second == 60)
    except (ValueError, OverflowError, OSError):
        return None


def _http_deadline(raw, now):
    for pattern, obsolete in _DATE_PATTERNS:
        match = pattern.fullmatch(raw)
        if match:
            return _date_deadline(match, obsolete, now)
    return None


def parse_retry_after(raw, *, now: float, valid_until: float | None = None) -> RetryHint:
    """Parse a bounded HTTP hint without shortening a server-requested delay.

    Invalid, missing and past bounded hints carry no deadline. Oversized metadata
    requires operator review and suspends without scanning or parsing its body.
    Valid delays over seven days, unrepresentable deadlines and deadlines at or
    beyond session expiry also suspend. Runtime owns all other retry policy.
    """
    now = _clock(now)
    if valid_until is not None:
        valid_until = _clock(valid_until)
    if not isinstance(raw, str):
        return RetryHint(None, False)
    if len(raw) > MAX_HEADER_CHARS:
        return RetryHint(None, True)
    raw = raw.strip(" \t")
    if re.fullmatch(r"[0-9]+", raw, re.ASCII):
        seconds = int(raw)
        deadline = _delta_deadline(seconds, now)
        suspended = seconds > MAX_AUTOMATIC_DELAY or deadline is None
    else:
        deadline = _http_deadline(raw, now)
        suspended = deadline is not None and Fraction(deadline) - Fraction(now) > MAX_AUTOMATIC_DELAY
    if deadline is None or deadline < now:
        return RetryHint(None, suspended if deadline is None else False)
    return RetryHint(deadline, suspended or (valid_until is not None and deadline >= valid_until))
