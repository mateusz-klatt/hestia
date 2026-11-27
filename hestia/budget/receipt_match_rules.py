"""Additional one-to-one bank match rules for receipts."""
from __future__ import annotations

from datetime import date
import re
from types import SimpleNamespace

from sqlalchemy import case, func, or_, select

from ..db import BudgetReceipt as Receipt, BudgetTransaction as Transaction
from .analytics_core import AnalyticsError, canonical_money, compare_money, financial_fingerprint, negate_money, sum_money
from .receipt_parser import chain_for, fold_text, merchant_score_groups, normalize_shop_name
from .receipt_reconcile import _bound_date, _json, entry_reference_digest, link_evidence, resolved_party_names

MIN_DOCUMENT_ID_LENGTH = 6
_BANK_DATE_FIELDS = ("transaction_date", "booking_date", "value_date")
_ROUNDING_DAYS = 2


def invoice_number_pattern(invoice_number):
    if invoice_number is None:
        return None
    chars = [char for char in fold_text(invoice_number) if char.isalnum()]
    if len(chars) < MIN_DOCUMENT_ID_LENGTH:
        return None
    # Match the complete ID while letting the bank format its separators independently.
    number = r"[\W_]*".join(chars)
    return re.compile(rf"(?<![^\W_]){number}(?![^\W_])")


def remittance_has_invoice_number(pattern, payload):
    lines = payload.get("remittance_information")
    return isinstance(lines, list) and any(
        isinstance(line, str) and pattern.search(fold_text(line)) for line in lines
    )


def rounding_delta(total, bank_amount):
    if "." in bank_amount or compare_money(total, "0") <= 0:
        return None
    delta = sum_money((bank_amount, negate_money(total)))
    return delta if compare_money(delta, "0") > 0 and compare_money(delta, "0.99") <= 0 else None


def stored_rounding_delta(evidence_json):
    if evidence_json is None:
        return None
    try:
        evidence = _json(evidence_json)
    except (ValueError, TypeError, RecursionError):
        return None
    delta = evidence.get("rounding_delta") if isinstance(evidence, dict) else None
    return delta if isinstance(delta, str) else None


def _purchase_date(payload):
    return next((day for key in _BANK_DATE_FIELDS if (day := _bound_date(payload.get(key)))), None)


def _distinctive_merchant_hit(row, payload, creditor, debtor):
    from .receipt_store import _merchant_tx_match

    groups, generic_tokens = merchant_score_groups(row.raw_shop_name, row.raw_nip)
    return _merchant_tx_match(
        payload, creditor, debtor, direction="DBIT", raw_nip=row.raw_nip,
        chain=chain_for(row.raw_shop_name, row.raw_nip), groups=groups, generic_tokens=generic_tokens,
        is_gateway=False, id_hit=False,
    )["distinctive_score"] > 0


def _date_limits(day):
    from .receipt_store import _shift_clamped

    return (_shift_clamped(day, -_ROUNDING_DAYS).isoformat(), _shift_clamped(day, _ROUNDING_DAYS).isoformat())


def _uses_card(store, row, transaction, account_kind, lines):
    from .gmail_card_match import _payment_matches

    last4 = getattr(row, "card_last4", None)
    return _payment_matches(SimpleNamespace(card_last4=last4), transaction, account_kind, lines,
                            store._card_accounts.get(last4))


def _rounded_up_match(store, session, owner, row, total, target, transaction, account_kind):
    payload = _json(transaction.payload_json)
    amount = canonical_money(payload.get("amount"))
    delta = rounding_delta(total, amount)
    day = _purchase_date(payload)
    if delta is None or day is None:
        return None
    distance = abs((date.fromisoformat(day) - target).days)
    if distance > _ROUNDING_DAYS:
        return None
    lines = payload.get("remittance_information", [])
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        return None
    if not _uses_card(store, row, transaction, account_kind, lines):
        return None
    fingerprint = financial_fingerprint(payload)
    creditor, debtor = resolved_party_names(session, owner, transaction.account_id, transaction.entry_reference,
                                           payload, fingerprint)
    if not _distinctive_merchant_hit(row, payload, creditor, debtor):
        return None
    return payload, amount, delta, day, distance, fingerprint, creditor, debtor


def _other_receipt_matches(store, session, owner, row, transaction, account_kind, match):
    from .receipt_store import _receipt_in_group

    payload, amount, _delta, day, _distance, _fingerprint, creditor, debtor = match
    peers = select(Receipt).where(
        Receipt.owner == owner, Receipt.currency == row.currency,
        Receipt.receipt_date.between(*_date_limits(date.fromisoformat(day))),
        Receipt.receipt_kind == "purchase",
        or_(Receipt.payment_method.is_(None), Receipt.payment_method.not_in(("cash", "mixed"))),
        Receipt.evidence_for_receipt_id.is_(None), Receipt.linked_account_id.is_(None),
        Receipt.total_amount.is_not(None),
    )
    row_id = getattr(row, "id", None)
    if row_id is not None:
        peers = peers.where(Receipt.id != row_id)
    for peer in session.scalars(peers):
        try:
            peer_total = canonical_money(peer.total_amount)
            amount_matches = peer_total == amount or rounding_delta(peer_total, amount) is not None
        except AnalyticsError:
            continue
        if (amount_matches and _uses_card(store, peer, transaction, account_kind, payload.get("remittance_information", []))
                and _distinctive_merchant_hit(peer, payload, creditor, debtor)
                and not _receipt_in_group(session, owner, peer.id)):
            return True
    return False


def rounded_up_candidates(store, session, owner, row, total, scope_accounts):
    from .receipt_store import _ROW_ID, _valid_date

    if row.receipt_kind != "purchase" or "." not in total or not _valid_date(row.receipt_date):
        return []
    target = date.fromisoformat(row.receipt_date)
    start, end = _date_limits(target)
    raw = Transaction.payload_json
    valid_json = case((func.json_valid(raw), raw), else_="{}")
    filters = (
        Transaction.account_id.in_(scope_accounts),
        func.json_extract(valid_json, "$.status") == "BOOK",
        func.json_extract(valid_json, "$.credit_debit_indicator") == "DBIT",
        func.json_extract(valid_json, "$.currency") == row.currency,
        or_(*(func.json_extract(valid_json, "$." + key).between(start, end) for key in _BANK_DATE_FIELDS)),
    )
    sole = None
    for transaction, account_kind, occupied in store._candidate_rows(
        session, owner, include_receipt_id=getattr(row, "id", None), card_filters=filters,
    ):
        try:
            match = _rounded_up_match(store, session, owner, row, total, target, transaction, account_kind)
        except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
            continue
        if match is None:
            continue
        if occupied or sole is not None:
            return []
        sole = transaction, account_kind, match
    if sole is None:
        return []

    transaction, account_kind, match = sole
    account_id, reference = transaction.account_id, transaction.entry_reference
    if _other_receipt_matches(store, session, owner, row, transaction, account_kind, match):
        return []
    payload, amount, delta, _day, distance, fingerprint, creditor, debtor = match
    row_id = session.scalar(select(_ROW_ID).select_from(Transaction).where(
        Transaction.account_id == account_id, Transaction.entry_reference == reference,
    ))
    try:
        evidence = link_evidence(
            owner, account_id, reference, payload, normalize_shop_name(row.raw_shop_name), row.raw_nip,
            creditor, fingerprint, debtor=debtor,
        )
        token = store._candidate_token(evidence, account_id, row_id, rule="rounded_up")
    except (AnalyticsError, ValueError, TypeError, RecursionError):
        return []
    return [{
        "account_id": account_id, "entry_reference": entry_reference_digest(reference),
        "amount": amount, "currency": row.currency, "credit_debit_indicator": "DBIT",
        "transaction_date": evidence["transaction_date"], "booking_date": evidence["booking_date"],
        "value_date": evidence["value_date"], "date_distance": distance, "merchant_token_hit": True,
        "match_strength": "strong" if distance == 0 else "medium",
        "match_rule": "rounded_up", "rounding_delta": delta, "candidate_token": token,
    }]
