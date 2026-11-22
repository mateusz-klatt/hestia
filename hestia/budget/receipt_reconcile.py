"""Latch confirmed receipt links when their committed bank evidence changes."""
from __future__ import annotations

import json
import math

from sqlalchemy import select, tuple_

from ..db import (BudgetAccount as Account, BudgetAnalyticsAudit as Audit, BudgetConnection as Connection,
                  BudgetReceipt as Receipt, BudgetTransaction as Transaction, BudgetTransferLeg as Leg)
from .analytics_core import AnalyticsError, financial_fingerprint

_CHUNK = 400


def audit_payload(row, action):
    return {
        "receipt_id": row.id, "action": action, "status": row.status, "version": row.version,
        "linked": row.linked_account_id is not None, "source_kind": row.source_kind,
    }


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


def _reserved(session, keys):
    result = set()
    for offset in range(0, len(keys), _CHUNK):
        chunk = keys[offset:offset + _CHUNK]
        result.update(session.execute(select(Leg.account_id, Leg.entry_reference).where(
            tuple_(Leg.account_id, Leg.entry_reference).in_(chunk))).all())
    return result


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
    reserved = _reserved(session, keys)
    changed = 0
    for row in linked:
        if row.status != "confirmed":
            continue
        key = (row.linked_account_id, row.linked_entry_ref)
        current = None if key in reserved else _fingerprint(transactions.get(key))
        if current == row.linked_fingerprint:
            continue
        before = audit_payload(row, "source_changed")
        row.status, row.version, row.updated_at = "needs_review", row.version + 1, now
        write_audit(session, owner, row, "source_changed", before, audit_payload(row, "source_changed"), now)
        changed += 1
    return changed
