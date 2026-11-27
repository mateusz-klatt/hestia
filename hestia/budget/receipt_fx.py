"""Known and reference-rate evidence for cross-currency, one-to-one receipt links."""
from __future__ import annotations

from decimal import Decimal, localcontext
import json
import re

from ..db import AppMeta, BudgetTransactionMetadata
from .bank_metadata import exchange_evidence
from .receipt_fx_rates import cached_quote, quote_currency, reference_day, validate_quote

_MONEY = r"[0-9]{1,16}(?:[.,][0-9]{1,12})?"
_CURRENCY = r"[A-Z]{3}"
_AMOUNT = re.compile(rf"(?:({_MONEY})[ \t]+({_CURRENCY})|({_CURRENCY})[ \t]+({_MONEY}))", re.I | re.ASCII)
_ORIGINAL = re.compile(
    r"\b(?:kwota (?:oryginalna|w walucie (?:oryginalnej|transakcji))|original (?:transaction )?amount)"
    rf"\s*[:=]?\s*(?:({_CURRENCY})\s+)?({_MONEY})(?:\s+({_CURRENCY})\b)?(?![\d,.])",
    re.ASCII | re.IGNORECASE,
)
_CHARGED = re.compile(r"(?:charged amount|amount charged|kwota obciążenia|obciążono kartę(?: kwotą)?)\s*:\s*(.+)",
                      re.IGNORECASE)
_FEE = re.compile(r"(?:fx fee|conversion fee|opłata za przewalutowanie|prowizja za przewalutowanie)\s*:\s*(.+)",
                  re.IGNORECASE)
_RATE = re.compile(rf"1[ \t]+({_CURRENCY})[ \t]*=[ \t]*({_MONEY})[ \t]+({_CURRENCY})", re.I | re.ASCII)
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)
_RECEIPT_META = "budget:receipt:fx:v1:"
_TOLERANCE = Decimal("0.03")
_QUANT = Decimal("0.00000001")


def _decimal(value):
    if isinstance(value, str) and len(value) <= 80 and _DECIMAL.fullmatch(value):
        return Decimal(value)
    return Decimal(0)


def _format(value):
    integer, _, fraction = format(value, "f").partition(".")
    fraction = fraction.rstrip("0")
    return f"{integer}.{fraction}" if fraction else integer


def _amount(match):
    first, second, third, fourth = match.groups()
    amount, currency = (first, second) if first is not None else (fourth, third)
    return {"amount": amount.replace(",", "."), "currency": currency.upper()}


def _labels(lines, label):
    result = []
    for line in lines:
        match = label.fullmatch(line.strip())
        if match and (amount := _AMOUNT.fullmatch(match[1])):
            found = _amount(amount)
            if _decimal(found["amount"]) > 0:
                result.append(found)
    return result


def remittance_original_amounts(lines):
    """A labelled original amount has one explicit currency, in either field order."""
    amounts = []
    for line in lines:
        for match in _ORIGINAL.finditer(line):
            before, quantity, after = match.groups()
            if (before is None) != (after is None):
                amounts.append({"amount": quantity.replace(",", "."), "currency": (before or after).upper()})
    return amounts


def _stated_rates(lines):
    result = []
    for line in lines:
        match = _RATE.fullmatch(line.strip())
        if match:
            unit, rate, counter = match.groups()
            unit, rate, counter = unit.upper(), rate.replace(",", "."), counter.upper()
            if unit != counter and _decimal(rate) > 0:
                result.append({"rate": rate, "unit_currency": unit, "counter_currency": counter})
    return result


def receipt_conversion_fields(text):
    """Extract only independently labelled evidence, with no retained document text."""
    lines = text[:65_536].splitlines()
    fields = {}
    for field, values in (("charged", _labels(lines, _CHARGED)), ("fee", _labels(lines, _FEE)),
                          ("rate", _stated_rates(lines))):
        if values:
            fields[field] = values
    return merge_receipt_conversion(fields, None)


def write_receipt_conversion(session, owner, receipt_id, fields):
    if fields:
        session.merge(AppMeta(key=receipt_conversion_key(receipt_id), value=json.dumps({**fields, "owner": owner})))


def receipt_conversion_key(receipt_id):
    return _RECEIPT_META + receipt_id


def read_receipt_conversion(session, owner, receipt_id):
    row = session.get(AppMeta, receipt_conversion_key(receipt_id))
    if row is None or len(row.value) > 1024:
        return {}
    try:
        data = json.loads(row.value)
    except ValueError:
        return {}
    if not isinstance(data, dict) or data.get("owner") != owner:
        return {}
    return {key: data[key] for key in ("charged", "fee", "rate") if key in data}


def erase_receipt_conversion(session, receipt_id):
    row = session.get(AppMeta, receipt_conversion_key(receipt_id))
    if row is not None:
        session.delete(row)


def _bank_exchange(session, owner, account_id, reference, fingerprint):
    row = session.get(BudgetTransactionMetadata, (account_id, reference))
    if row is None or row.owner != owner or row.ledger_fingerprint != fingerprint:
        return {}, False
    payload = json.loads(row.payload_json)
    if not isinstance(payload, dict):
        return {}, False
    return exchange_evidence(payload.get("exchange_rate")), "exchange_rate" in payload and payload["exchange_rate"] is None


def _money_key(money):
    return (money["currency"], _decimal(money["amount"]))


def _rate_key(rate):
    return (rate["unit_currency"], rate["counter_currency"], _decimal(rate["rate"]))


def _stated_rates_agree(rates, currency, booked_currency):
    if not rates:
        return True
    intervals = []
    with localcontext() as context:
        context.prec = 80
        for rate in rates:
            unit, counter, value = _rate_key(rate)
            if {unit, counter} != {currency, booked_currency}:
                return False
            half_step = Decimal(5).scaleb(value.as_tuple().exponent - 1)
            low, high = value - half_step, value + half_step
            intervals.append((low, high) if unit == currency else (1 / high, 1 / low))
    return max(low for low, _ in intervals) <= min(high for _, high in intervals)


def merge_receipt_conversion(primary, alternate):
    """Combine independent copies, keeping up to two distinct observations per label."""
    first, second = primary or {}, alternate or {}
    result = {}
    for key in first.keys() | second.keys():
        identity = _rate_key if key == "rate" else _money_key
        values = {}
        for observation in (*first.get(key, ()), *second.get(key, ())):
            values.setdefault(identity(observation), observation)
        result[key] = list(values.values())[:2]
    return result


def _quote_explanation(rate, unit, total, currency, booked):
    mid = _decimal(rate["mid"])
    estimate = total / mid if currency == "PLN" else total * mid
    difference = booked - estimate
    deviation = abs(difference) / estimate
    return {
        "rate": rate["mid"], "unit_currency": unit,
        "publication_date": rate["effectiveDate"], "table_number": rate["no"],
        "estimated_amount": _format(estimate.quantize(_QUANT)),
        "difference": _format(difference.quantize(_QUANT)),
    }, deviation


def fx_candidate(session, owner, row, transaction, payload, fingerprint, *, receipt_fields=None,
                 accepted_quote=None):
    """Return explanation and deviation, honoring explicit evidence before consulting the cache."""
    currency, booked_currency = row.currency, payload.get("currency")
    total, booked = _decimal(row.total_amount), _decimal(payload.get("amount"))
    if not currency or currency == booked_currency or not booked_currency or total <= 0 or booked <= 0:
        return None
    lines = payload.get("remittance_information", [])
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        return None
    bank, bank_conflict = _bank_exchange(session, owner, transaction.account_id,
                                         transaction.entry_reference, fingerprint)
    original = bank.get("instructed_amount")
    remittance = remittance_original_amounts(lines)
    if receipt_fields is None:
        receipt_fields = read_receipt_conversion(session, owner, row.id)
    charged = receipt_fields.get("charged", [])
    bank_rate = bank.get("exchange_rate")
    stated_rates = []
    if bank_rate:
        unit = bank["unit_currency"]
        counter = currency if unit == booked_currency else booked_currency
        stated_rates.append({"source": "bank_original", "rate": bank_rate, "unit_currency": unit,
                             "counter_currency": counter})
    stated_rates.extend({"source": "bank_remittance", **rate} for rate in _stated_rates(lines))
    stated_rates.extend({"source": "receipt_charged", **rate} for rate in receipt_fields.get("rate", []))
    fees = _labels(lines, _FEE) + receipt_fields.get("fee", [])
    if not fees and _decimal(getattr(row, "fee_amount", None)) > 0:
        fees = [{"amount": row.fee_amount, "currency": currency}]

    receipt_key, booked_key = (currency, total), (booked_currency, booked)
    remittance_keys = {_money_key(money) for money in remittance}
    charged_keys = {_money_key(money) for money in charged}
    source = None
    if original and _money_key(original) == receipt_key:
        source = "bank_original"
    elif receipt_key in remittance_keys:
        source = "bank_remittance"
    elif booked_key in charged_keys:
        source = "receipt_charged"
    conflicting = (bank_conflict or bool(remittance_keys - {receipt_key}) or bool(charged_keys - {booked_key})
                   or (original is not None and _money_key(original) != receipt_key)
                   or len({_money_key(fee) for fee in fees}) > 1
                   or not _stated_rates_agree(stated_rates, currency, booked_currency))
    quote, deviation = None, Decimal(0)
    with localcontext() as context:
        # Ledger decimal strings are bounded at 80 digits. Preserve precision until display rounding.
        context.prec = 180
        if source is None:
            if bank_conflict:
                source = "bank_original"
            elif original or remittance or charged or stated_rates:
                return None
            else:
                unit = quote_currency(currency, booked_currency)
                day = reference_day(row.receipt_date)
                rate = cached_quote(session, unit, day)
                if rate is None and accepted_quote is not None:
                    rate = validate_quote(accepted_quote, day)
                if rate is None:
                    return None
                quote, deviation = _quote_explanation(rate, unit, total, currency, booked)
                if deviation > _TOLERANCE:
                    return None
                source = "nbp"
        return {
            "source": source, "receipt_amount": _format(total), "receipt_currency": currency,
            "booked_amount": _format(booked), "booked_currency": booked_currency,
            "implied_rate": _format((booked / total).quantize(_QUANT)),
            "quote": quote, "stated_rate": stated_rates[0] if stated_rates else None,
            "explicit_fee": fees[0] if fees and len({_money_key(fee) for fee in fees}) == 1 else None,
            "conflicting": conflicting,
        }, deviation


def fx_link_evidence(row, fx):
    return {"details": fx, "receipt_id": getattr(row, "id", None), "reference_date": row.receipt_date,
            "receipt_kind": row.receipt_kind, "document_kind": getattr(row, "document_kind", None), "due_date": getattr(row, "due_date", None),
            "card_last4": getattr(row, "card_last4", None)}


def accepted_fx_quote(linked_fx):
    quote = linked_fx["details"]["quote"]
    if quote is None:
        return None
    return {"effectiveDate": quote["publication_date"], "no": quote["table_number"], "mid": quote["rate"]}
