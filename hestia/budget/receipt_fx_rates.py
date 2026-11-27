"""Bounded, public NBP table A cache. Only the background task fetches rates."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.client import HTTPException, HTTPSConnection
import json
import re
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select, tuple_

from ..db import AppMeta, BudgetAccount, BudgetConnection, BudgetReceipt, BudgetTransaction

NBP_HOST = "api.nbp.pl"
NBP_TIMEOUT = 3
# ISO codes quoted by table A. PLN is the implicit countercurrency.
NBP_CURRENCIES = frozenset("""
AUD BGN BRL CAD CHF CLP CNY CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KRW
MXN MYR NOK NZD PHP RON SEK SGD THB TRY UAH USD XDR ZAR
""".split())
LOOKBACK_DAYS = 10
MAX_CACHE_ROWS = 512
MAX_FETCHES = 12
RETRY_SECONDS = 3600
_HISTORICAL_TTL_SECONDS = 30 * 86400
_FIRST_NBP_DATE = date(2002, 1, 2)
_PREFIX = "budget:receipt:nbp:v1:"
_MAX_RESPONSE = 16_384
_TABLE = re.compile(r"[0-9]{3}/A/NBP/[0-9]{4}", re.ASCII)
_MID = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,12})?", re.ASCII)


def reference_day(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day.isoformat() == value and day >= _FIRST_NBP_DATE else None


def quote_currency(receipt_currency, booked_currency):
    if booked_currency == "PLN" and receipt_currency in NBP_CURRENCIES:
        return receipt_currency
    if receipt_currency == "PLN" and booked_currency in NBP_CURRENCIES:
        return booked_currency
    return None


def _quote(raw, start, end):
    if not isinstance(raw, dict):
        return None
    day = reference_day(raw.get("effectiveDate"))
    table, mid = raw.get("no"), raw.get("mid")
    if (day is None or not start <= day <= end or not isinstance(table, str)
            or not _TABLE.fullmatch(table) or table[-4:] != str(day.year)
            or not isinstance(mid, str) or not _MID.fullmatch(mid) or Decimal(mid) <= 0):
        return None
    return {"effectiveDate": day.isoformat(), "no": table, "mid": mid}


def fetch_nbp_quote(currency, day):
    """Fetch a bounded date range against one HTTPS host, without redirects."""
    if currency not in NBP_CURRENCIES or reference_day(day.isoformat()) is None:
        return None
    start = max(_FIRST_NBP_DATE, day - timedelta(days=LOOKBACK_DAYS))
    path = f"/api/exchangerates/rates/a/{currency}/{start}/{day}/?format=json"
    try:
        with closing(HTTPSConnection(NBP_HOST, timeout=NBP_TIMEOUT)) as connection:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                return None
            body = response.read(_MAX_RESPONSE + 1)
        if len(body) > _MAX_RESPONSE:
            return None
        data = json.loads(body, parse_float=str, parse_int=str)
    except (OSError, HTTPException, ValueError, RecursionError):
        return None
    if (not isinstance(data, dict) or data.get("code") != currency or data.get("table") != "A"
            or not isinstance(data.get("rates"), list)):
        return None
    quotes = [quote for raw in data["rates"] if (quote := _quote(raw, start, day)) is not None]
    if not quotes:
        return None
    latest = max(quote["effectiveDate"] for quote in quotes)
    eligible = [quote for quote in quotes if quote["effectiveDate"] == latest]
    return eligible[0] if all(quote == eligible[0] for quote in eligible) else None


def _key(currency, day):
    return f"{_PREFIX}{currency}:{day.isoformat()}"


def _cache_value(row):
    if row is None or len(row.value) > 512:
        return {}
    try:
        value = json.loads(row.value)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def validate_quote(raw, day):
    return None if day is None else _quote(raw, max(_FIRST_NBP_DATE, day - timedelta(days=LOOKBACK_DAYS)), day)


def cached_quote(session, currency, day):
    """Resolve a full, validated observation for this reference date without I/O outside SQLite."""
    if currency is None or day is None:
        return None
    value = _cache_value(session.get(AppMeta, _key(currency, day)))
    return validate_quote(value.get("quote"), day)


def _quote_unit():
    raw = BudgetReceipt.linked_evidence
    valid_json = case((func.json_valid(raw), raw), else_="{}")
    return func.json_extract(valid_json, "$.fx.details.quote.unit_currency")


def _cache_demands(session, today):
    # Aggregate currencies per owner before joining to receipts, avoiding a row
    # for every receipt times every transaction in that owner's ledger.
    raw = BudgetTransaction.payload_json
    valid_json = case((func.json_valid(raw), raw), else_="{}")
    currency = func.json_extract(valid_json, "$.currency")
    bookings = (
        select(BudgetAccount.owner, currency.label("currency"))
        .join_from(BudgetAccount, BudgetTransaction)
        .join(BudgetConnection, and_(BudgetConnection.id == BudgetAccount.connection_id,
                                     BudgetConnection.owner == BudgetAccount.owner))
        .where(BudgetAccount.enabled.is_(True), BudgetConnection.enabled.is_(True),
               BudgetTransaction.uncertain.is_(False), func.json_extract(valid_json, "$.status") == "BOOK",
               currency.in_((*sorted(NBP_CURRENCIES), "PLN")))
        .distinct().subquery()
    )
    receipt_day = BudgetReceipt.receipt_date
    booked_or_receipt = case((BudgetReceipt.currency == "PLN", bookings.c.currency), else_=BudgetReceipt.currency)
    pairs = (
        select(receipt_day.label("day"), booked_or_receipt.label("currency"))
        .join(bookings, bookings.c.owner == BudgetReceipt.owner)
        .where(
            BudgetReceipt.status.in_(("imported", "draft", "needs_review")),
            BudgetReceipt.evidence_for_receipt_id.is_(None),
            BudgetReceipt.total_amount.is_not(None),
            or_(BudgetReceipt.payment_method.is_(None), BudgetReceipt.payment_method.not_in(("cash", "mixed"))),
            or_(and_(BudgetReceipt.currency == "PLN", bookings.c.currency != "PLN"),
                and_(BudgetReceipt.currency.in_(sorted(NBP_CURRENCIES)), bookings.c.currency == "PLN")),
        )
    )
    accepted = select(receipt_day, _quote_unit()).where(BudgetReceipt.status == "confirmed")
    demands = pairs.union(accepted).subquery()
    query = select(demands.c.day, demands.c.currency).where(
        demands.c.day <= today.isoformat(), demands.c.currency.in_(sorted(NBP_CURRENCIES)),
    ).order_by(demands.c.day.desc(), demands.c.currency).limit(MAX_CACHE_ROWS)
    return [(code, day) for stamp, code in session.execute(query) if (day := reference_day(stamp)) is not None]


def _recent_enough(cached, day, today, now):
    fetched = cached.get("fetched_at")
    if type(fetched) not in (int, float):
        return False
    quote = validate_quote(cached.get("quote"), day)
    historical = quote is not None and today - day > timedelta(days=7)
    return 0 <= now - fetched < (_HISTORICAL_TTL_SECONDS if historical else RETRY_SECONDS)


def prefetch_nbp_quotes(sessions, now):
    """Prefetch missing quotes in bounded batches with no DB transaction held across HTTP."""
    today = datetime.fromtimestamp(now, ZoneInfo("Europe/Warsaw")).date()
    with sessions() as session:
        pending = []
        for currency, day in _cache_demands(session, today):
            key = _key(currency, day)
            if _recent_enough(_cache_value(session.get(AppMeta, key)), day, today, now):
                continue
            pending.append((currency, day))
            if len(pending) >= MAX_FETCHES:
                break
    if not pending:
        return 0
    data = [(currency, day, fetch_nbp_quote(currency, day)) for currency, day in pending]
    with sessions.begin() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        changed = []
        for currency, day, quote in data:
            key = _key(currency, day)
            prior = _cache_value(session.get(AppMeta, key))
            fetched = prior.get("fetched_at")
            if type(fetched) in (int, float) and fetched > now:
                continue
            previous = validate_quote(prior.get("quote"), day)
            # Keep the latest eligible observation through outages and stale responses.
            if quote is None or (previous is not None and quote["effectiveDate"] < previous["effectiveDate"]):
                quote = previous
            elif quote != previous:
                changed.append((day.isoformat(), currency))
            session.merge(AppMeta(key=key, value=json.dumps({"fetched_at": now, "quote": quote})))
        session.flush()
        keys = session.scalars(select(AppMeta.key).where(AppMeta.key.startswith(_PREFIX))).all()
        # Evict the oldest reference dates, independent of how often they were queried.
        for key in sorted(keys, key=lambda item: (item[-10:], item))[:max(0, len(keys) - MAX_CACHE_ROWS)]:
            session.delete(session.get(AppMeta, key))
        if changed:
            from .receipt_reconcile import reconcile_receipt_links

            owners = session.scalars(select(BudgetReceipt.owner).where(
                BudgetReceipt.status == "confirmed",
                tuple_(BudgetReceipt.receipt_date, _quote_unit()).in_(changed),
            ).distinct()).all()
            for owner in owners:
                reconcile_receipt_links(session, owner, now)
    return len(data)
