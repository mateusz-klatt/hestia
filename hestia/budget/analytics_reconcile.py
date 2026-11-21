"""Latch accepted transfer decisions for review when ingested evidence changes.

Runs inside the source-ingestion transaction. It never releases reservations,
replaces recorded fingerprints, or restores a reviewed decision automatically.
"""
from __future__ import annotations

import json
import math

from sqlalchemy import select, tuple_

from ..db import (BudgetAccount as Account, BudgetAccountIdentity as Identity,
                  BudgetAnalyticsAudit as Audit, BudgetConnection as Connection,
                  BudgetTransaction as Transaction, BudgetTransactionMetadata as Metadata,
                  BudgetTransferDecision as Decision)
from .analytics_core import AnalyticsError, effective_date, financial_fingerprint, pair_fingerprint

_CHUNK = 400  # Two bind parameters per composite key, below legacy SQLite's 999.


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def _json(raw):
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)


def _owned_identities(session, owner):
    accounts = list(session.scalars(select(Account.id).join(Connection).where(
        Account.owner == owner, Connection.owner == owner)))
    result = dict.fromkeys(accounts, ())
    for row in session.scalars(select(Identity).join(Account).join(Connection).where(
            Identity.owner == owner, Account.owner == owner, Connection.owner == owner)):
        try:
            result[row.account_id] = _json(row.identities_json)
        except (ValueError, TypeError):
            # Corrupt identity evidence must not leave an accepted exclusion in
            # place. The ordinary fingerprint validator will fail closed on None.
            result[row.account_id] = None
    return result


def _source_rows(session, owner, keys):
    ledger, metadata = {}, {}
    for offset in range(0, len(keys), _CHUNK):
        chunk = keys[offset:offset + _CHUNK]
        query = select(Transaction).join(Account).join(Connection).where(
            Account.owner == owner, Connection.owner == owner,
            tuple_(Transaction.account_id, Transaction.entry_reference).in_(chunk))
        ledger.update({(row.account_id, row.entry_reference): row for row in session.scalars(query)})
        query = select(Metadata).where(Metadata.owner == owner,
            tuple_(Metadata.account_id, Metadata.entry_reference).in_(chunk))
        metadata.update({(row.account_id, row.entry_reference): row for row in session.scalars(query)})
    return ledger, metadata


def _current_fingerprint(key, ledger, metadata, owned):
    row = ledger.get(key)
    if row is None or row.uncertain:
        return None
    try:
        payload = _json(row.payload_json)
        if not isinstance(payload, dict) or payload.get("status") != "BOOK" or effective_date(payload) is None:
            return None
        fingerprint = financial_fingerprint(payload)
        sidecar = metadata.get(key)
        evidence = (_json(sidecar.payload_json) if sidecar is not None
                    and sidecar.ledger_fingerprint == fingerprint else None)
        return pair_fingerprint(row.account_id, row.entry_reference, payload, evidence, owned)
    except (AnalyticsError, ValueError, TypeError, KeyError):
        return None


def reconcile_transfers(session, owner, observed_at):
    """Return the number latched; caller controls the transaction and owner gate."""
    try:
        if type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at < 0:
            raise ValueError
    except (ValueError, OverflowError):
        raise AnalyticsError("budget_analytics_unavailable", 503) from None
    session.flush()
    accepted = list(session.scalars(select(Decision).where(Decision.owner == owner, Decision.status == "accepted")))
    if not accepted:
        return 0
    keys = sorted({key for row in accepted for key in
                   ((row.debit_account_id, row.debit_ref), (row.credit_account_id, row.credit_ref))})
    owned = _owned_identities(session, owner)
    ledger, metadata = _source_rows(session, owner, keys)
    changed = 0
    for row in accepted:
        debit = _current_fingerprint((row.debit_account_id, row.debit_ref), ledger, metadata, owned)
        credit = _current_fingerprint((row.credit_account_id, row.credit_ref), ledger, metadata, owned)
        if debit == row.debit_fingerprint and credit == row.credit_fingerprint:
            continue
        before = {"status": row.status, "version": row.version}
        row.status, row.version, row.updated_at = "review", row.version + 1, observed_at
        session.add(Audit(owner=owner, subject_type="transfer", subject_id=row.id, action="source_changed",
                          before_json=json.dumps(before, sort_keys=True),
                          after_json=json.dumps({"status": row.status, "version": row.version}, sort_keys=True),
                          changed_at=observed_at))
        changed += 1
    return changed
