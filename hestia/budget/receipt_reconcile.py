"""Latch confirmed receipt links when their committed bank evidence changes."""
from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy import delete, select, tuple_

from ..db import (BudgetAccount as Account, BudgetAnalyticsAudit as Audit, BudgetConnection as Connection,
                  BudgetReceipt as Receipt, BudgetTransaction as Transaction,
                  BudgetTransactionMetadata as Metadata, BudgetTransferLeg as Leg)
from .analytics_core import AnalyticsError, financial_fingerprint
from .receipt_parser import normalize_shop_name

_CHUNK = 400
_MAX_CREDITOR_LEN = 256
_EVIDENCE_KEYS = (
    "owner", "account_id", "entry_reference", "transaction_date", "booking_date", "value_date",
    "raw_shop_name", "raw_nip", "creditor_digest", "debtor_digest", "remittance_digest",
    "financial_fingerprint",
)


def audit_payload(row, action, actor=None):
    payload = {
        "receipt_id": row.id, "action": action, "status": row.status, "version": row.version,
        "linked": row.linked_account_id is not None, "source_kind": row.source_kind,
    }
    if actor is not None:
        payload["actor"] = actor
    return payload


def write_audit(session, owner, row, action, before, after, changed_at):
    session.add(Audit(
        owner=owner, subject_type="receipt", subject_id=row.id, action=action,
        before_json=json.dumps(before, sort_keys=True, ensure_ascii=True, separators=(",", ":")),
        after_json=json.dumps(after, sort_keys=True, ensure_ascii=True, separators=(",", ":")),
        changed_at=changed_at,
    ))


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_value):
        raise ValueError

    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def _linked_transactions(session, owner, keys):
    result = {}
    for offset in range(0, len(keys), _CHUNK):
        chunk = keys[offset:offset + _CHUNK]
        rows = session.scalars(select(Transaction).join(Account).join(Connection).where(
            Account.owner == owner, Connection.owner == owner,
            tuple_(Transaction.account_id, Transaction.entry_reference).in_(chunk),
        ))
        result.update({(row.account_id, row.entry_reference): row for row in rows})
    return result


def _reserved(session, owner, keys):
    result = set()
    for offset in range(0, len(keys), _CHUNK):
        chunk = keys[offset:offset + _CHUNK]
        result.update(session.execute(select(Leg.account_id, Leg.entry_reference).join(
            Account, Account.id == Leg.account_id).where(
            Account.owner == owner, tuple_(Leg.account_id, Leg.entry_reference).in_(chunk))).all())
    return result


def _bound_date(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        from datetime import date
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _party_string(data, field):
    value = data.get(field) if isinstance(data, dict) else None
    if not isinstance(value, str) or not value or len(value) > _MAX_CREDITOR_LEN:
        return None
    return value


def transaction_party_names(session, owner, account_id, entry_reference, fingerprint):
    """Creditor and debtor names are usable only when metadata matches this ledger fingerprint."""
    if fingerprint is None:
        return None, None
    metadata = session.get(Metadata, (account_id, entry_reference))
    if metadata is None or metadata.owner != owner or metadata.ledger_fingerprint != fingerprint:
        return None, None
    try:
        data = _json(metadata.payload_json)
    except (ValueError, TypeError, RecursionError):
        return None, None
    return _party_string(data, "creditor_name"), _party_string(data, "debtor_name")


def _payload_party_name(payload, party_key):
    if not isinstance(payload, dict):
        return None
    flat = _party_string({"v": payload.get(party_key + "_name")}, "v")
    if flat is not None:
        return flat
    nested = payload.get(party_key)
    if isinstance(nested, dict):
        return _party_string(nested, "name")
    return None


def resolved_party_names(session, owner, account_id, entry_reference, payload, fingerprint):
    """Normalized creditor and debtor names from fingerprint-verified metadata or transaction payload."""
    meta_creditor, meta_debtor = transaction_party_names(
        session, owner, account_id, entry_reference, fingerprint,
    )
    creditor = meta_creditor if meta_creditor is not None else _payload_party_name(payload, "creditor")
    debtor = meta_debtor if meta_debtor is not None else _payload_party_name(payload, "debtor")
    return creditor, debtor


def creditor_name(session, owner, account_id, entry_reference, fingerprint):
    """Creditor text is usable only when its metadata row matches this ledger fingerprint."""
    return transaction_party_names(session, owner, account_id, entry_reference, fingerprint)[0]


def _remittance_digest(payload):
    """Sha256 of the canonical remittance text. The raw lines can be 4096 characters each."""
    value = payload.get("remittance_information") if isinstance(payload, dict) else None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        value = None
    try:
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise AnalyticsError() from None
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _party_digest(party):
    """Sha256 of the NFC/whitespace-normalized party name. Absence stays null so raw names never persist."""
    normalized = normalize_shop_name(party)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def entry_reference_digest(entry_reference):
    """Sha256 of the provider reference. The receipt row still stores the raw key."""
    if not isinstance(entry_reference, str):
        return None
    return hashlib.sha256(entry_reference.encode("utf-8")).hexdigest()


def link_evidence(owner, account_id, entry_reference, payload, shop_name, nip, creditor, fingerprint, debtor=None):
    payload = payload if isinstance(payload, dict) else {}
    if fingerprint is not None and (
            not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)):
        raise AnalyticsError()
    evidence = {
        "owner": owner, "account_id": account_id, "entry_reference": entry_reference_digest(entry_reference),
        "transaction_date": _bound_date(payload.get("transaction_date")),
        "booking_date": _bound_date(payload.get("booking_date")),
        "value_date": _bound_date(payload.get("value_date")),
        "raw_shop_name": shop_name, "raw_nip": nip,
        "creditor_digest": _party_digest(creditor),
        "debtor_digest": _party_digest(debtor),
        "remittance_digest": _remittance_digest(payload), "financial_fingerprint": fingerprint,
    }
    if set(evidence) != set(_EVIDENCE_KEYS):
        raise AnalyticsError()
    return evidence


def evidence_json(evidence):
    try:
        raw = json.dumps(evidence, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise AnalyticsError() from None
    if not 1 <= len(raw) <= 4096 or not raw.isascii():
        raise AnalyticsError()
    return raw


def erase_receipt_audit(session, owner, receipt_id):
    session.execute(delete(Audit).where(
        Audit.owner == owner, Audit.subject_type == "receipt", Audit.subject_id == receipt_id,
    ))


def _fingerprint(row):
    if row is None or row.uncertain:
        return None
    try:
        payload = _json(row.payload_json)
        if not isinstance(payload, dict) or payload.get("status") != "BOOK":
            return None
        return financial_fingerprint(payload)
    except (AnalyticsError, ValueError, TypeError, KeyError, RecursionError):
        return None


def reconcile_receipt_links(session, owner, now):
    try:
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError
    except (ValueError, OverflowError):
        raise AnalyticsError("budget_analytics_unavailable", 503) from None
    session.flush()
    linked = list(session.scalars(select(Receipt).where(
        Receipt.owner == owner, Receipt.status.in_(("confirmed", "needs_review")),
        Receipt.linked_account_id.is_not(None),
    )))
    if not linked:
        return 0
    keys = sorted({(row.linked_account_id, row.linked_entry_ref) for row in linked})
    transactions = _linked_transactions(session, owner, keys)
    reserved = _reserved(session, owner, keys)
    changed = 0
    for row in linked:
        if row.status != "confirmed":
            continue
        key = (row.linked_account_id, row.linked_entry_ref)
        transaction = None if key in reserved else transactions.get(key)
        current = _fingerprint(transaction)
        normalized_shop = normalize_shop_name(row.raw_shop_name)
        shop_forms = (normalized_shop,) if normalized_shop == row.raw_shop_name else (normalized_shop, row.raw_shop_name)
        try:
            payload = {} if transaction is None else _json(transaction.payload_json)
            creditor, debtor = resolved_party_names(
                session, owner, row.linked_account_id, row.linked_entry_ref, payload, current,
            )
            evidences = [
                link_evidence(
                    owner, row.linked_account_id, row.linked_entry_ref, payload, candidate_shop, row.raw_nip,
                    creditor, current, debtor=debtor)
                for candidate_shop in shop_forms
            ]
            expected_evidence = {evidence_json(ev) for ev in evidences}
            if normalize_shop_name(debtor) in (None, ""):
                # Pre-debtor_digest confirmed rows stay confirmed as long as debtor stays absent.
                expected_evidence.update(
                    evidence_json({key: val for key, val in ev.items() if key != "debtor_digest"})
                    for ev in evidences
                )
        except (AnalyticsError, ValueError, TypeError, RecursionError):
            expected_evidence = ()
        if current == row.linked_fingerprint and row.linked_evidence in expected_evidence:
            continue
        before = audit_payload(row, "source_changed")
        row.status, row.version, row.updated_at = "needs_review", row.version + 1, now
        write_audit(session, owner, row, "source_changed", before, audit_payload(row, "source_changed"), now)
        changed += 1
    return changed
