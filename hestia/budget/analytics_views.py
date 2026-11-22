"""Owner-selected, exact spending projections; read-only SQLite snapshots.

The same snapshot digest fences rule previews and encrypted pagination. Transfer
matching always precedes period filtering; only current, accepted pairs affect
totals. No configuration, provider, file, secret or acquisition operation runs here.

Reads memoise the projection in process memory under a digest of every source row it
can depend on, so any committed change yields a new key and nothing is ever invalidated
by hand. Authorization is checked before every lookup; writes always recompute.
"""
from __future__ import annotations

import base64
from collections import OrderedDict, defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import math
import os
import re
import threading

from cryptography.exceptions import InvalidTag
from sqlalchemy import func, or_, select

from .. import db
from . import analytics_core as core
from . import owner_inbox as inbox_mod
from . import quarantine as quarantine_mod
from .analytics_core import AnalyticsError
from .analytics_store import BudgetAnalyticsStore, _CATEGORY_LIMIT_FIELDS, _dict
from .client import _reject_constant, _unique_error_fields
from .scheduler import shared_state
from .views import BudgetHTTPStore

MAX_ROWS = 100_000
MAX_EDGES = 100_000
MAX_RECURRING_HINTS = 200
MAX_RECURRING_EVIDENCE = 24
CURSOR_TTL = 900
MAX_CURSOR = 8192
_DATA_ERROR = "budget_analytics_data_error"
_LIMIT_ERROR = "budget_analytics_limit"
_DOMAIN = b"hestia/budget/analytics/cursor/v1"
_PAYLOAD_FIELDS = ("amount", "currency", "credit_debit_indicator", "status", "booking_date", "value_date",
                   "transaction_date", "remittance_information")
_KINDS = frozenset({"expense", "income", "refund", "excluded", "unclassified_credit", "transfer"})
CACHE_ENTRIES = 4
_cache = OrderedDict()
_cache_lock = threading.Lock()
# One analytics read at a time: CPU-bound reads only interleave under the GIL, and their many short
# SQLite calls then wait for each other's switch interval. Taken before the read transaction opens.
_read_lock = threading.Lock()
_TOTAL_FIELDS = ("gross_debits", "gross_credits", "expenses", "income", "refunds", "unclassified_credit",
                 "excluded_debits", "excluded_credits", "transfer_debits", "transfer_credits")


def _json(raw, expected=dict):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_error_fields, parse_constant=_reject_constant)
        if not isinstance(value, expected):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise AnalyticsError() from None
    return value


def _entity(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns if column.name != "owner"}


def _semantic(value):
    return {key: item for key, item in value.items() if key not in ("created_at", "updated_at")}


def _payload(raw):
    value = _json(raw)
    fingerprint = core.financial_fingerprint(value)
    descriptions = value.get("remittance_information")
    if not isinstance(descriptions, list) or any(not isinstance(line, str) for line in descriptions):
        raise AnalyticsError()
    for name in ("booking_date", "value_date", "transaction_date"):
        stamp = value.get(name)
        if stamp is not None:
            try:
                if not isinstance(stamp, str) or date.fromisoformat(stamp).isoformat() != stamp:
                    raise ValueError
            except ValueError:
                raise AnalyticsError() from None
    return value, fingerprint


def _identity_pairs(values):
    if not isinstance(values, list):
        raise AnalyticsError()
    result = []
    for value in values:
        if (not isinstance(value, dict) or set(value) != {"scheme", "identification"}
                or any(not isinstance(item, str) or not item or len(item) > 1024 for item in value.values())):
            raise AnalyticsError()
        result.append((value["scheme"], value["identification"]))
    return tuple(sorted(set(result)))


def _metadata(value):
    raw = _json(value)
    allowed = {"creditor_name", "debtor_name", "creditor_accounts", "debtor_accounts", "mcc"}
    if raw.keys() - allowed:
        raise AnalyticsError()
    for field in ("creditor_name", "debtor_name", "mcc"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            raise AnalyticsError()
    for field in ("creditor_accounts", "debtor_accounts"):
        _identity_pairs(raw.get(field, []))
    return raw


def _selected_query(model, owner):
    return select(model).join(db.BudgetAccount, model.account_id == db.BudgetAccount.id).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled, model.owner == owner)


def _auxiliary(session, owner, accounts):
    identities = {account.id: [] for account in accounts}
    identity_state = []
    query = select(db.BudgetAccountIdentity).join(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccountIdentity.owner == owner)
    for row in session.scalars(query.order_by(db.BudgetAccountIdentity.account_id)):
        values = _json(row.identities_json, list)
        _identity_pairs(values)
        identities[row.account_id] = values
        identity_state.append([row.account_id, values, row.version])
    metadata = { (row.account_id, row.entry_reference): row for row in session.scalars(
        _selected_query(db.BudgetTransactionMetadata, owner)) }
    annotations = { (row.account_id, row.entry_reference): row for row in session.scalars(
        _selected_query(db.BudgetAnnotation, owner)) }
    return identities, identity_state, metadata, annotations


def _category(category_id, categories):
    if category_id is not None and category_id not in categories:
        raise AnalyticsError()
    return category_id


def _annotation(row, financial_fp):
    if row is None:
        return {"version": 0, "mode": "auto", "category_id": None, "kind": "auto", "stale": False}
    if row.mode not in ("auto", "manual") or row.kind not in core.KINDS:
        raise AnalyticsError()
    return {"version": row.version, "mode": row.mode, "category_id": row.category_id,
            "kind": row.kind, "stale": row.ledger_fingerprint != financial_fp}


def _effective(payload, metadata, annotation, categories, rules):
    default = "expense" if payload["credit_debit_indicator"] == "DBIT" else "unclassified_credit"
    result = {"kind": default, "category_id": None, "source": "default", "rule_id": None}
    if annotation["mode"] == "manual":
        if not annotation["stale"]:
            if not core.compatible_kind(annotation["kind"], payload):
                raise AnalyticsError()
            result.update(kind=default if annotation["kind"] == "auto" else annotation["kind"],
                          category_id=_category(annotation["category_id"], categories), source="manual")
        return result
    for rule in rules:
        if core.rule_matches(rule, payload, metadata):
            result.update(kind=default if rule["kind"] == "auto" else rule["kind"],
                          category_id=_category(rule["category_id"], categories), source="rule", rule_id=rule["id"])
            break
    return result


def _records(ledger, identities, metadata, annotations, categories, rules):
    result = []
    owned = core.owned_identities(identities) if ledger else None
    for row in ledger:
        payload, financial_fp = _payload(row.payload_json)
        key = (row.account_id, row.entry_reference)
        sidecar = metadata.get(key)
        evidence = _metadata(sidecar.payload_json) if sidecar is not None and sidecar.ledger_fingerprint == financial_fp else None
        annotation = _annotation(annotations.get(key), financial_fp)
        effective = _effective(payload, evidence, annotation, categories, rules)
        result.append({"account_id": row.account_id, "entry_reference": row.entry_reference, "payload": payload,
                       "financial_fp": financial_fp,
                       "transfer_fp": core.pair_fingerprint(row.account_id, row.entry_reference, payload, evidence, identities,
                                                            financial=financial_fp, owned_ids=owned),
                       "metadata": evidence, "annotation": annotation, "effective": effective,
                       "base_effective": dict(effective), "source": row.source, "observed_at": row.observed_at,
                       "api_observed_at": row.api_observed_at, "uncertain": row.uncertain,
                       "eligible": not row.uncertain and payload["status"] == "BOOK" and core.effective_date(payload) is not None,
                       "transfer": {"status": "none", "decision_id": None, "candidate_count": 0, "ambiguous": False}})
    return result


def _owner_counts(session, owner):
    """Owner-wide uncertainty counts, over selected and excluded accounts alike."""
    def count(model, on):
        return session.scalar(select(func.count()).select_from(model).join(db.BudgetAccount, on).join(db.BudgetConnection).where(
            db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner))
    uncertain = session.scalar(select(func.count()).select_from(db.BudgetTransaction).join(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetTransaction.uncertain))
    imports = select(db.BudgetImportBatch.id).join(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner)
    imported = session.scalar(select(func.count()).select_from(db.BudgetImportUnresolved).where(
        db.BudgetImportUnresolved.batch_id.in_(imports)))
    return {"uncertain_count": uncertain,
            "unresolved_count": count(db.BudgetUnresolved, db.BudgetUnresolved.account_id == db.BudgetAccount.id),
            "import_unresolved_count": imported}


def _scope_counts(session, owner, accounts):
    return {"selected_accounts": sum(account.enabled for account in accounts),
            "excluded_accounts": sum(not account.enabled for account in accounts), **_owner_counts(session, owner)}


def projection_snapshot(session, owner):
    """Shared read/preview snapshot; caller owns the existing SQLite transaction."""
    BudgetHTTPStore._owner(session, owner)
    accounts = list(session.scalars(select(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner).order_by(db.BudgetAccount.id)))
    ledger = list(session.scalars(select(db.BudgetTransaction).join(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled).order_by(
        db.BudgetTransaction.account_id, db.BudgetTransaction.entry_reference).limit(MAX_ROWS + 1)))
    if len(ledger) > MAX_ROWS:
        raise AnalyticsError("budget_analytics_limit", 413)
    categories = [_entity(row) for row in session.scalars(select(db.BudgetCategory).where(
        db.BudgetCategory.owner == owner).order_by(db.BudgetCategory.id))]
    rules = [_entity(row) for row in session.scalars(select(db.BudgetRule).where(
        db.BudgetRule.owner == owner).order_by(db.BudgetRule.priority, db.BudgetRule.id))]
    decisions = [_entity(row) for row in session.scalars(select(db.BudgetTransferDecision).where(
        db.BudgetTransferDecision.owner == owner).order_by(db.BudgetTransferDecision.id))]
    identities, identity_state, metadata, annotations = _auxiliary(session, owner, accounts)
    records = _records(ledger, identities, metadata, annotations, {row["id"]: row for row in categories}, rules)
    account_state = [{"id": row.id, "connection_id": row.connection_id, "enabled": row.enabled,
                      "configured": row.configured, "control_version": row.control_version, "kind": row.kind} for row in accounts]
    snapshot = {"owner": owner, "accounts": account_state, "categories": categories, "rules": rules,
                "decisions": decisions, "identities": identities, "transactions": [row for row in records if row["eligible"]],
                "counts": _scope_counts(session, owner, accounts), "_records": records, "_accounts": accounts}
    semantic = {"version": 1, "owner": owner,
                "accounts": [{"id": row.id, "enabled": row.enabled} for row in accounts], "identities": identity_state,
                "ledger": [[row.account_id, row.entry_reference, record["payload"], row.uncertain, row.source]
                           for row, record in zip(ledger, records, strict=True)],
                "metadata": sorted([[row.account_id, row.entry_reference, row.ledger_fingerprint,
                                     _json(row.payload_json), row.version] for row in metadata.values()]),
                "annotations": sorted([_semantic(_entity(row)) for row in annotations.values()], key=lambda item: (item["account_id"], item["entry_reference"])),
                "categories": [_semantic(row) for row in categories], "rules": [_semantic(row) for row in rules],
                "decisions": [_semantic(row) for row in decisions]}
    snapshot["digest"] = core.projection_digest(semantic)
    _accepted_projection(snapshot)
    return snapshot


def _decision_keys(decision):
    return ((decision["debit_account_id"], decision["debit_ref"]),
            (decision["credit_account_id"], decision["credit_ref"]))


def _leg(key, records, selected):
    row = records.get(key)
    return {"account_id": key[0], "entry_reference": key[1], "selected": key[0] in selected,
            "fingerprint": None if row is None else row["transfer_fp"],
            "amount": None if row is None else row["payload"]["amount"],
            "currency": None if row is None else row["payload"]["currency"],
            "date": None if row is None else core.effective_date(row["payload"])}


def _valid_pair(debit, credit):
    if debit is None or credit is None or not debit["eligible"] or not credit["eligible"]:
        return False
    first, second = debit["payload"], credit["payload"]
    return (debit["account_id"] != credit["account_id"] and first["credit_debit_indicator"] == "DBIT"
            and second["credit_debit_indicator"] == "CRDT" and first["currency"] == second["currency"]
            and core.compare_money(first["amount"], second["amount"]) == 0 and core.compare_money(first["amount"], "0") > 0
            and abs((date.fromisoformat(core.effective_date(first)) - date.fromisoformat(core.effective_date(second))).days) <= 3)


def _decision_view(decision, records, selected, owned):
    debit_key, credit_key = _decision_keys(decision)
    if (debit_key[0] not in owned or credit_key[0] not in owned
            or decision["status"] not in ("accepted", "review", "rejected", "undone")
            or decision["source"] not in ("account_identity", "heuristic", "manual")):
        raise AnalyticsError()
    debit, credit = records.get(debit_key), records.get(credit_key)
    boundary = debit_key[0] not in selected or credit_key[0] not in selected
    stale = decision["status"] == "review" or decision["rule_version"] != 1
    for key, row, fingerprint in ((debit_key, debit, decision["debit_fingerprint"]),
                                   (credit_key, credit, decision["credit_fingerprint"])):
        if key[0] in selected and (row is None or not row["eligible"] or row["transfer_fp"] != fingerprint):
            stale = True
    status = decision["status"]
    if status == "accepted":
        if boundary:
            status = "boundary"
        elif stale or not _valid_pair(debit, credit):
            status, stale = "review", True
    first, second = _leg(debit_key, records, selected), _leg(credit_key, records, selected)
    distance = None if first["date"] is None or second["date"] is None else abs(
        (date.fromisoformat(first["date"]) - date.fromisoformat(second["date"])).days)
    return {"id": decision["id"], "version": decision["version"], "status": status,
            "source": decision["source"], "debit": first, "credit": second,
            "date_distance": distance, "ambiguous": False, "stale": stale, "boundary": boundary}


def _candidates(snapshot, blocked, reserved):
    from .transfer_match import TransferEntry, TransferInputError, TransferMatchLimitError, match_transfers

    entries = []
    for row in snapshot["transactions"]:
        payload, metadata = row["payload"], row["metadata"] or {}
        field = "creditor_accounts" if payload["credit_debit_indicator"] == "DBIT" else "debtor_accounts"
        entries.append(TransferEntry(snapshot["owner"], row["account_id"], row["entry_reference"],
                      core.canonical_money(payload["amount"]), payload["currency"], payload["credit_debit_indicator"],
                      core.effective_date(payload), row["transfer_fp"], _identity_pairs(metadata.get(field, [])),
                      eligible=row["effective"]["kind"] != "excluded"))
    selected = {row["id"] for row in snapshot["accounts"] if row["enabled"]}
    own = {key: _identity_pairs(value) for key, value in snapshot["identities"].items() if key in selected}
    try:
        return match_transfers(snapshot["owner"], entries, own, blocked_pairs=frozenset(blocked),
                               reserved_entries=frozenset(reserved), max_edges=MAX_EDGES), False
    except TransferMatchLimitError:
        return (), True
    except TransferInputError:
        raise AnalyticsError() from None


def _recurring_hints(snapshot):
    from .recurring import RecurringEntry, RecurringInputError, detect_recurring_charges

    entries = []
    for row in snapshot["transactions"]:
        payload, metadata = row["payload"], row["metadata"] or {}
        entries.append(RecurringEntry(
            owner=snapshot["owner"],
            account_id=row["account_id"],
            entry_reference=row["entry_reference"],
            amount=core.canonical_money(payload["amount"]),
            currency=payload["currency"],
            direction=payload["credit_debit_indicator"],
            effective_kind=row["effective"]["kind"],
            date=core.effective_date(payload),
            fingerprint=row["financial_fp"],
            counterparty_name=metadata.get("creditor_name"),
            description="\n".join(payload["remittance_information"]),
        ))
    selected = frozenset(row["id"] for row in snapshot["accounts"] if row["enabled"])
    try:
        return detect_recurring_charges(snapshot["owner"], entries, selected, max_entries=MAX_ROWS)
    except RecurringInputError:
        raise AnalyticsError() from None


def _wire_recurring(hint):
    evidence = hint.evidence[-MAX_RECURRING_EVIDENCE:]
    return {
        "label": hint.label,
        "label_source": hint.label_source,
        "amount": hint.amount,
        "currency": hint.currency,
        "cadence": hint.cadence,
        "occurrence_count": hint.occurrence_count,
        "distinct_months": hint.distinct_months,
        "first_date": hint.first_date,
        "last_date": hint.last_date,
        "min_gap_days": hint.min_gap_days,
        "max_gap_days": hint.max_gap_days,
        "account_ids": list(hint.account_ids),
        "evidence": [
            {"account_id": item.account_id, "entry_reference": item.entry_reference, "date": item.date}
            for item in evidence
        ],
        "evidence_truncated": len(hint.evidence) > MAX_RECURRING_EVIDENCE,
    }


def _limits_version(rows):
    return core.projection_digest([
        "category-limits-v1",
        [[row["id"], row["category_id"], row["currency"], row["monthly_limit"],
          row["warning_percent"], row["enabled"], row["version"]] for row in rows],
    ])


def _schedules_version(owner, schedules):
    return core.projection_digest([
        "mortgage-schedules-v1",
        owner,
        [[row["id"], row["label"], row["amount"], row["currency"], row["due_day"], row["start_date"],
          row["end_date"], row["remind_days_before"], row["destination_kind"], row["enabled"],
          row["version"], row["paid_months"]] for row in schedules],
    ])


def _wire_mortgage_reminder(item):
    return {
        "schedule_id": item.schedule_id,
        "label": item.label,
        "amount": item.amount,
        "currency": item.currency,
        "due_date": item.due_date,
        "days_until_due": item.days_until_due,
        "status": item.status,
        "reminder_active": item.reminder_active,
        "destination_kind": item.destination_kind,
        "freshness_stale": item.freshness_stale,
        "ledger_as_of": item.ledger_as_of,
        "rule_version": item.rule_version,
    }


def _spending_limit_statuses(owner, period, limit_rows, category_totals, categories):
    from .spending_limits import (CategoryLimit, CategorySpending, SpendingLimitInputError,
                                  evaluate_spending_limits)

    limits = [
        CategoryLimit(owner=owner, category_id=row["category_id"], currency=row["currency"],
                      monthly_limit=row["monthly_limit"], enabled=row["enabled"],
                      warning_percent=row["warning_percent"])
        for row in limit_rows
    ]
    spending = [
        CategorySpending(owner=owner, category_id=row["category_id"], currency=row["currency"],
                         expenses=row["expenses"], refunds=row["refunds"], archived=row["archived"])
        for row in category_totals
    ]
    try:
        evaluated = evaluate_spending_limits(owner, period, limits, spending)
    except SpendingLimitInputError:
        raise AnalyticsError() from None
    return [
        {
            "period": item.period,
            "category_id": item.category_id,
            "name": categories[item.category_id]["name"],
            "archived": categories[item.category_id]["archived"],
            "currency": item.currency,
            "monthly_limit": item.monthly_limit,
            "expenses": item.expenses,
            "refunds": item.refunds,
            "net_spending": item.net_spending,
            "remaining": item.remaining,
            "over_by": item.over_by,
            "percent_used_floor": item.percent_used_floor,
            "warning_percent": item.warning_percent,
            "status": item.status,
        }
        for item in evaluated
    ]


def _mark_transfer(row, status, identifier):
    priority = {"none": 0, "rejected": 1, "undone": 1, "suggested": 2, "boundary": 3, "review": 4, "accepted": 5}
    if priority[status] > priority[row["transfer"]["status"]]:
        row["transfer"].update(status="rejected" if status == "undone" else status, decision_id=identifier)


def _accepted_projection(snapshot):
    records = {(row["account_id"], row["entry_reference"]): row for row in snapshot["_records"]}
    selected = {row["id"] for row in snapshot["accounts"] if row["enabled"]}
    owned = {row["id"] for row in snapshot["accounts"]}
    transfers, blocked, reserved = [], set(), set()
    for decision in snapshot["decisions"]:
        debit, credit = _decision_keys(decision)
        view = _decision_view(decision, records, selected, owned)
        transfers.append(view)
        if decision["status"] in ("accepted", "review"):
            reserved.update((debit, credit))
        else:
            blocked.add((debit, credit))
        for key in (debit, credit):
            row = records.get(key)
            if row is not None:
                _mark_transfer(row, view["status"], view["id"])
                if view["status"] == "accepted":
                    row["effective"] = {"kind": "transfer", "category_id": None, "source": "transfer", "rule_id": None}
    snapshot["_transfers"], snapshot["_blocked"], snapshot["_reserved"] = transfers, blocked, reserved


def _transfer_projection(snapshot):
    records = {(row["account_id"], row["entry_reference"]): row for row in snapshot["_records"]}
    selected = {row["id"] for row in snapshot["accounts"] if row["enabled"]}
    transfers = snapshot["_transfers"]
    candidates, limited = _candidates(snapshot, snapshot["_blocked"], snapshot["_reserved"])
    for candidate in candidates:
        transfers.append({"id": None, "version": 0, "status": "suggested", "source": candidate.source,
                          "debit": _leg(candidate.debit_key, records, selected),
                          "credit": _leg(candidate.credit_key, records, selected),
                          "date_distance": candidate.date_distance, "ambiguous": candidate.ambiguous,
                          "stale": False, "boundary": False})
        for key in (candidate.debit_key, candidate.credit_key):
            row = records[key]
            _mark_transfer(row, "suggested", None)
            row["transfer"].update(candidate_count=row["transfer"]["candidate_count"] + 1,
                                   ambiguous=row["transfer"]["ambiguous"] or candidate.ambiguous)
    snapshot["_transfers"], snapshot["_scan_limited"] = transfers, limited


def _period_rows(snapshot, start, end, currency):
    return [row for row in snapshot["transactions"] if start <= core.effective_date(row["payload"]) <= end
            and (currency is None or row["payload"]["currency"] == currency)]


def _period_transfers(snapshot, start, end, currency):
    return [row for row in snapshot["_transfers"] if any(leg["selected"] and leg["date"] is not None
            and start <= leg["date"] <= end and (currency is None or leg["currency"] == currency)
            for leg in (row["debit"], row["credit"]))]


def _totals(rows):
    grouped = {}
    for row in rows:
        payload, kind = row["payload"], row["effective"]["kind"]
        values = grouped.setdefault(payload["currency"], {field: [] for field in _TOTAL_FIELDS})
        debit = payload["credit_debit_indicator"] == "DBIT"
        values["gross_debits" if debit else "gross_credits"].append(payload["amount"])
        if kind in ("excluded", "transfer"):
            field = kind + ("_debits" if debit else "_credits")
        else:
            field = {"expense": "expenses", "refund": "refunds"}.get(kind, kind)
        values[field].append(payload["amount"])
    result = []
    for currency, values in sorted(grouped.items()):
        total = {field: core.sum_money(amounts) for field, amounts in values.items()}
        total["net_spending"] = core.sum_money([total["expenses"], core.negate_money(total["refunds"])])
        result.append({"currency": currency, **total})
    return result


def _category_totals(rows, categories):
    names = {row["id"]: row for row in categories}
    grouped = defaultdict(list)
    for row in rows:
        if row["effective"]["kind"] in ("expense", "refund"):
            grouped[(row["effective"]["category_id"], row["payload"]["currency"])].append(row)
    result = []
    for (identifier, currency), values in sorted(grouped.items(), key=lambda item: (item[0][0] or "", item[0][1])):
        total = _totals(values)[0]
        category = names.get(identifier)
        result.append({"category_id": identifier, "name": None if category is None else category["name"],
                       "archived": False if category is None else category["archived"], "currency": currency,
                       **{field: total[field] for field in ("expenses", "refunds", "net_spending")}})
    return result


def _wire_transaction(row):
    metadata = row["metadata"]
    return {"account_id": row["account_id"], "entry_reference": row["entry_reference"],
            **{field: row["payload"].get(field) for field in _PAYLOAD_FIELDS},
            "source": row["source"], "observed_at": row["observed_at"], "api_observed_at": row["api_observed_at"],
            "fingerprint": row["financial_fp"], "transfer_fingerprint": row["transfer_fp"],
            "annotation": row["annotation"], "effective": row["effective"], "transfer": row["transfer"],
            "metadata": None if metadata is None else {key: metadata.get(key) for key in ("creditor_name", "debtor_name", "mcc")}}


def _currency(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value, re.ASCII)):
        raise AnalyticsError()
    return value


def _source_digest(session, owner):
    """Digest of everything the projection reads for ``owner``: all columns of the rows it loads
    (equal to or a superset of its own filters) and the owner-wide counts it reports.

    Any committed insert, update or delete in these rows changes the digest. Columns are taken
    from the table definition, so a column added later enters the key without a code change.
    Excluded accounts contribute only their account rows and the counts, never their ledger.
    The selected ledger is bounded by MAX_ROWS before anything else is read, as in the projection.
    """
    selected = (db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled)
    owned = (db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner)
    connections = select(db.BudgetAccount.connection_id).where(db.BudgetAccount.owner == owner)
    # (model, conditions, joined through its account and connection like the projection's own query)
    sources = (
        (db.BudgetTransaction, selected, True),
        (db.BudgetConnection, (or_(db.BudgetConnection.owner == owner, db.BudgetConnection.id.in_(connections)),), False),
        (db.BudgetAccount, (db.BudgetAccount.owner == owner,), False),
        (db.BudgetTransactionMetadata, (*selected, db.BudgetTransactionMetadata.owner == owner), True),
        (db.BudgetAnnotation, (*selected, db.BudgetAnnotation.owner == owner), True),
        (db.BudgetAccountIdentity, (*owned, db.BudgetAccountIdentity.owner == owner), True),
        (db.BudgetCategory, (db.BudgetCategory.owner == owner,), False),
        (db.BudgetRule, (db.BudgetRule.owner == owner,), False),
        (db.BudgetTransferDecision, (db.BudgetTransferDecision.owner == owner,), False),
    )
    digest = hashlib.sha256(b"hestia/budget/analytics/source/v2")

    def put(value):
        data = (type(value).__name__ + ":" + repr(value)).encode("utf-8", "surrogatepass")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    for model, conditions, joined in sources:
        table = model.__table__
        query = select(*table.columns)
        if joined:
            query = query.join(db.BudgetAccount, table.c.account_id == db.BudgetAccount.id).join(
                db.BudgetConnection, db.BudgetAccount.connection_id == db.BudgetConnection.id)
        query = query.where(*conditions).order_by(*table.primary_key.columns)
        if model is db.BudgetTransaction:
            query = query.limit(MAX_ROWS + 1)
        rows = session.execute(query).all()
        if model is db.BudgetTransaction and len(rows) > MAX_ROWS:
            raise AnalyticsError("budget_analytics_limit", 413)
        put(table.name)
        put([column.name for column in table.columns])
        for row in rows:
            put(tuple(row))
    put(sorted(_owner_counts(session, owner).items()))
    return digest.hexdigest()


def clear_projection_cache():
    with _cache_lock:
        _cache.clear()


def _cached(key):
    with _cache_lock:
        value = _cache.get(key)
        if value is not None:
            _cache.move_to_end(key)
        return value


def _remember(key, value):
    with _cache_lock:
        for other in [item for item in _cache if item[:2] == key[:2]]:
            del _cache[other]  # one projection per database and owner
        _cache[key] = value
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)


class BudgetAnalyticsViews(BudgetAnalyticsStore):
    def _projection(self, session, owner):
        """Shared, read-only projection for this transaction's committed state; callers never mutate it."""
        BudgetHTTPStore._owner(session, owner)
        key = (str(session.get_bind().url), owner, MAX_ROWS, MAX_EDGES, _source_digest(session, owner))
        value = _cached(key)
        if value is None:
            # Callers hold _read_lock, so concurrent readers of one state find this result instead of repeating it.
            value = projection_snapshot(session, owner)
            _transfer_projection(value)
            del value["_accounts"]  # ORM rows never outlive their session
            _remember(key, value)
        return value

    def _freshness(self, session, snapshot, now):
        owner = snapshot["owner"]
        connections = {row.id: row for row in session.scalars(select(db.BudgetConnection).where(db.BudgetConnection.owner == owner))}
        components = {(row.account_id, row.operation): row for row in session.scalars(select(db.BudgetComponent).join(db.BudgetAccount).where(
            db.BudgetAccount.owner == owner, db.BudgetAccount.enabled))}
        accounts = session.scalars(select(db.BudgetAccount).join(db.BudgetConnection).where(
            db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled).order_by(db.BudgetAccount.id))
        shared = {key: shared_state(session, row) for key, row in connections.items()}
        return [{"account_id": account.id, **{operation: self._component_status(
            components.get((account.id, operation)), operation, account, connections[account.connection_id], now,
            shared[account.connection_id]) for operation in ("transactions", "balances")}}
            for account in accounts]

    def overview(self, owner, period, *, currency=None):
        start, end = core.parse_period(period)
        currency = _currency(currency)
        with _read_lock, self._read(owner) as session:
            snapshot = self._projection(session, owner)
            rows = _period_rows(snapshot, start, end, currency)
            months = defaultdict(list)
            for row in rows:
                months[core.effective_date(row["payload"])[:7]].append(row)
            transfers = _period_transfers(snapshot, start, end, currency)
            summary = {status: sum(row["status"] == status for row in transfers)
                       for status in ("suggested", "accepted", "review", "rejected", "boundary")}
            summary["scan_limited"] = snapshot["_scan_limited"]
            return {"period": period, "start": start, "end": end, "snapshot_digest": snapshot["digest"],
                    "scope": {**snapshot["counts"], "freshness": self._freshness(session, snapshot, self._now())},
                    "currencies": _totals(rows), "months": [{"month": month, "currencies": _totals(values)}
                        for month, values in sorted(months.items())],
                    "categories": _category_totals(rows, snapshot["categories"]), "transfer_summary": summary}

    def _cursor_encode(self, context, snapshot, index, now):
        payload = {"context": context, "snapshot": snapshot, "index": index, "issued": now, "expires": now + CURSOR_TTL}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        return base64.urlsafe_b64encode(nonce + self._cipher.encrypt(nonce, data, _DOMAIN)).rstrip(b"=").decode("ascii")

    def _cursor_index(self, cursor, context, snapshot, now):
        if cursor is None:
            return 0
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CURSOR:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload = json.loads(self._cipher.decrypt(raw[:12], raw[12:], _DOMAIN))
            if (not isinstance(payload, dict) or set(payload) != {"context", "snapshot", "index", "issued", "expires"}
                    or payload["context"] != context or type(payload["index"]) is not int or payload["index"] < 0):
                raise ValueError
            issued, expires = payload["issued"], payload["expires"]
            if (type(issued) not in (int, float) or type(expires) not in (int, float)
                    or not math.isfinite(issued) or not math.isfinite(expires)
                    or not issued <= now < expires <= issued + CURSOR_TTL):
                raise ValueError
        except (ValueError, TypeError, InvalidTag, RecursionError, OverflowError):
            raise AnalyticsError() from None
        if payload["snapshot"] != snapshot:
            raise AnalyticsError("budget_analytics_stale_cursor", 409)
        return payload["index"]

    def _page(self, rows, cursor, filters, snapshot):
        limit, now = filters["limit"], self._now()
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AnalyticsError()
        context = core.projection_digest(filters)
        index = self._cursor_index(cursor, context, snapshot, now)
        if index > len(rows):
            raise AnalyticsError()
        end = min(len(rows), index + limit)
        next_cursor = self._cursor_encode(context, snapshot, end, now) if end < len(rows) else None
        return rows[index:end], next_cursor

    def transactions(self, owner, period, *, currency=None, category_id=None, kind=None, query=None, limit=50, cursor=None):
        start, end = core.parse_period(period)
        currency = _currency(currency)
        if kind is not None and (not isinstance(kind, str) or kind not in _KINDS):
            raise AnalyticsError()
        if category_id is not None and (not isinstance(category_id, str) or not category_id or len(category_id) > 128):
            raise AnalyticsError()
        if query is not None and (not isinstance(query, str) or len(query) > 256):
            raise AnalyticsError()
        query = None if query is None else query.casefold().strip()
        filters = {"owner": owner, "operation": "transactions", "period": period, "currency": currency,
                   "category_id": category_id, "kind": kind, "query": query, "limit": limit}
        with _read_lock, self._read(owner) as session:
            snapshot = self._projection(session, owner)
            if category_id not in (None, "uncategorised") and category_id not in {row["id"] for row in snapshot["categories"]}:
                raise AnalyticsError("budget_analytics_not_found", 404)
            rows = [row for row in _period_rows(snapshot, start, end, currency) if _row_filter(row, category_id, kind, query)]
            rows.sort(key=lambda row: (core.effective_date(row["payload"]), row["account_id"], row["entry_reference"]), reverse=True)
            page, next_cursor = self._page(rows, cursor, filters, snapshot["digest"])
            return {"period": period, "start": start, "end": end, "snapshot_digest": snapshot["digest"],
                    "transactions": deepcopy([_wire_transaction(row) for row in page]), "filtered_total": len(rows),
                    "next_cursor": next_cursor}

    def transfers(self, owner, period, *, currency=None, status="suggested", limit=50, cursor=None):
        start, end = core.parse_period(period)
        currency = _currency(currency)
        if status not in ("suggested", "accepted", "review", "boundary", "rejected", "undone"):
            raise AnalyticsError()
        filters = {"owner": owner, "operation": "transfers", "period": period, "currency": currency,
                   "status": status, "limit": limit}
        with _read_lock, self._read(owner) as session:
            snapshot = self._projection(session, owner)
            rows = [row for row in _period_transfers(snapshot, start, end, currency) if row["status"] == status]
            rows.sort(key=lambda row: (max(row["debit"]["date"] or "", row["credit"]["date"] or ""),
                                       row["debit"]["account_id"], row["debit"]["entry_reference"],
                                       row["credit"]["account_id"], row["credit"]["entry_reference"]), reverse=True)
            page, next_cursor = self._page(rows, cursor, filters, snapshot["digest"])
            return {"period": period, "start": start, "end": end, "snapshot_digest": snapshot["digest"],
                    "transfers": deepcopy(page), "filtered_total": len(rows), "next_cursor": next_cursor,
                    "scan_limited": snapshot["_scan_limited"]}

    def recurring(self, owner, *, currency=None):
        from .recurring import RULE_VERSION

        currency = _currency(currency)
        with _read_lock, self._read(owner) as session:
            snapshot = self._projection(session, owner)
            hints = snapshot.get("_recurring")
            if hints is None:
                hints = _recurring_hints(snapshot)
                snapshot["_recurring"] = hints
            if currency is not None:
                hints = tuple(item for item in hints if item.currency == currency)
            page = hints[:MAX_RECURRING_HINTS]
            return {"snapshot_digest": snapshot["digest"], "rule_version": RULE_VERSION,
                    "total_hints": len(hints), "truncated": len(hints) > MAX_RECURRING_HINTS,
                    "hints": [_wire_recurring(item) for item in page]}

    def spending_limits(self, owner, period, *, currency=None):
        from .spending_limits import RULE_VERSION

        if not isinstance(period, str) or len(period) != 7:
            raise AnalyticsError()
        start, end = core.parse_period(period)
        currency = _currency(currency)
        with _read_lock, self._read(owner) as session:
            snapshot = self._projection(session, owner)
            categories = {row["id"]: row for row in snapshot["categories"]}
            all_limits = [_dict(row, _CATEGORY_LIMIT_FIELDS) for row in session.scalars(
                select(db.BudgetCategoryLimit).where(db.BudgetCategoryLimit.owner == owner)
                .order_by(db.BudgetCategoryLimit.currency, db.BudgetCategoryLimit.category_id,
                          db.BudgetCategoryLimit.id))]
            if any(row["category_id"] not in categories for row in all_limits):
                raise AnalyticsError()
            limits_version = _limits_version(all_limits)
            filtered = all_limits if currency is None else [row for row in all_limits if row["currency"] == currency]
            totals = _category_totals(_period_rows(snapshot, start, end, currency), snapshot["categories"])
            statuses = _spending_limit_statuses(owner, period, filtered, totals, categories)
            return {"period": period, "start": start, "end": end, "snapshot_digest": snapshot["digest"],
                    "limits_version": limits_version, "rule_version": RULE_VERSION,
                    "limits": filtered, "statuses": statuses}

    @staticmethod
    def _ledger_as_of(session, owner, evaluation_date):
        selected = select(db.BudgetAccount.id).join(db.BudgetConnection).where(
            db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled)
        component_ts = session.scalar(select(func.max(db.BudgetComponent.last_success)).where(
            db.BudgetComponent.account_id.in_(selected), db.BudgetComponent.operation == "transactions"))
        tx_ts = session.scalar(select(func.max(db.BudgetTransaction.observed_at)).where(
            db.BudgetTransaction.account_id.in_(selected), db.BudgetTransaction.uncertain.is_(False)))
        stamps = [ts for ts in (component_ts, tx_ts) if ts is not None]
        if not stamps:
            return None
        observed = datetime.fromtimestamp(max(stamps), timezone.utc).date().isoformat()
        return min(observed, evaluation_date)

    def _mortgage_reminders_in_session(self, session, owner, evaluation_date):
        from .mortgage_reminders import (MortgageInputError, MortgageLimitError, MortgageSchedule,
                                         evaluate_mortgage_reminders)

        rows = list(session.scalars(
            select(db.BudgetMortgageSchedule).where(db.BudgetMortgageSchedule.owner == owner)
            .order_by(db.BudgetMortgageSchedule.id)))
        payments_by_schedule = defaultdict(list)
        for payment in session.scalars(
            select(db.BudgetMortgagePayment).where(db.BudgetMortgagePayment.owner == owner)
            .order_by(db.BudgetMortgagePayment.schedule_id, db.BudgetMortgagePayment.month)):
            payments_by_schedule[payment.schedule_id].append(payment.month)
        schedules = [self._mortgage_schedule_dict(row, payments_by_schedule[row.id]) for row in rows]
        ledger_as_of = self._ledger_as_of(session, owner, evaluation_date)
        models = [
            MortgageSchedule(
                owner=owner, schedule_id=item["id"], label=item["label"], amount=item["amount"],
                currency=item["currency"], due_day=item["due_day"], start_date=item["start_date"],
                destination_kind=item["destination_kind"], destination_target=None,
                end_date=item["end_date"], remind_days_before=item["remind_days_before"],
                paid_months=frozenset(item["paid_months"]), enabled=item["enabled"],
            )
            for item in schedules
        ]
        try:
            reminders = evaluate_mortgage_reminders(owner, evaluation_date, models, ledger_as_of=ledger_as_of)
        except MortgageLimitError:
            raise AnalyticsError(_LIMIT_ERROR, 413) from None
        except MortgageInputError:
            raise AnalyticsError(_DATA_ERROR, 500) from None
        return schedules, ledger_as_of, reminders

    def mortgages(self, owner):
        """Evaluate owner-private mortgage schedules against server UTC today in one read snapshot."""
        from .mortgage_reminders import RULE_VERSION

        evaluation_date = datetime.fromtimestamp(self._now(), timezone.utc).date().isoformat()
        with _read_lock, self._read(owner) as session:
            schedules, ledger_as_of, reminders = self._mortgage_reminders_in_session(session, owner, evaluation_date)
            return {
                "evaluation_date": evaluation_date,
                "evaluation_timezone": "UTC",
                "ledger_as_of": ledger_as_of,
                "schedules_version": _schedules_version(owner, schedules),
                "rule_version": RULE_VERSION,
                "schedules": schedules,
                "reminders": [_wire_mortgage_reminder(item) for item in reminders],
            }

    @staticmethod
    def _inbox_consent_entries(session, owner, evaluated_at):
        from .renewal_reminders import (ConsentRenewalTarget, RenewalInputError, RenewalLimitError,
                                        evaluate_renewal_reminders)

        rows = list(session.scalars(
            select(db.BudgetConnection).where(db.BudgetConnection.owner == owner)
            .order_by(db.BudgetConnection.id)))
        generations = {row.id: row.consent_generation for row in rows}
        targets = [
            ConsentRenewalTarget(
                owner=owner, connection_id=row.id, bank_name=row.bank_name, valid_until=row.valid_until,
                destination_kind="owner_inbox", destination_target=None,
                enabled=row.enabled, paused=row.paused, attention=row.attention,
            )
            for row in rows
        ]
        try:
            reminders = evaluate_renewal_reminders(owner, evaluated_at, targets)
        except RenewalLimitError:
            raise AnalyticsError(_LIMIT_ERROR, 413) from None
        except RenewalInputError:
            raise AnalyticsError(_DATA_ERROR, 500) from None
        entries = []
        for item in reminders:
            if not item.reminder_due:
                continue
            entries.append({
                "kind": inbox_mod.KIND_CONSENT,
                "subject_id": inbox_mod.opaque_subject_id(owner, inbox_mod.KIND_CONSENT, item.connection_id),
                "milestone": inbox_mod.consent_milestone(
                    generations[item.connection_id], item.valid_until, item.status,
                    item.active_milestone, item.attention,
                ),
                "ref": inbox_mod.consent_ref(connection_id=item.connection_id),
                "params": inbox_mod.consent_params(
                    status=item.status, valid_until=item.valid_until, days_remaining=item.days_remaining,
                    attention=item.attention, paused=item.paused,
                ),
            })
        return entries

    def _inbox_mortgage_entries(self, session, owner, evaluation_date):
        _, _, reminders = self._mortgage_reminders_in_session(session, owner, evaluation_date)
        return [
            {
                "kind": inbox_mod.KIND_MORTGAGE,
                "subject_id": inbox_mod.opaque_subject_id(owner, inbox_mod.KIND_MORTGAGE, item.schedule_id),
                "milestone": inbox_mod.mortgage_milestone(item.due_date, item.status),
                "ref": inbox_mod.mortgage_ref(schedule_id=item.schedule_id),
                "params": inbox_mod.mortgage_params(
                    status=item.status, due_date=item.due_date, days_until_due=item.days_until_due,
                    currency=item.currency, freshness_stale=item.freshness_stale,
                ),
            }
            for item in reminders if item.reminder_active
        ]

    def _inbox_spending_entries(self, session, owner, period, *, use_cache):
        from .spending_limits import (CategoryLimit, CategorySpending, SpendingLimitCapError,
                                      SpendingLimitInputError, evaluate_spending_limits)

        all_limits = [_dict(row, _CATEGORY_LIMIT_FIELDS) for row in session.scalars(
            select(db.BudgetCategoryLimit).where(db.BudgetCategoryLimit.owner == owner)
            .order_by(db.BudgetCategoryLimit.currency, db.BudgetCategoryLimit.category_id,
                      db.BudgetCategoryLimit.id))]
        if any(row["enabled"] for row in all_limits):
            snapshot = self._projection(session, owner) if use_cache else projection_snapshot(session, owner)
            categories = {row["id"]: row for row in snapshot["categories"]}
            start, end = core.parse_period(period)
            totals = _category_totals(_period_rows(snapshot, start, end, None), snapshot["categories"])
        else:
            categories = {row.id: _entity(row) for row in session.scalars(
                select(db.BudgetCategory).where(db.BudgetCategory.owner == owner)
                .order_by(db.BudgetCategory.id))}
            totals = []
        if any(row["category_id"] not in categories for row in all_limits):
            raise AnalyticsError(_DATA_ERROR, 500)
        limits = [
            CategoryLimit(owner=owner, category_id=row["category_id"], currency=row["currency"],
                          monthly_limit=row["monthly_limit"], enabled=row["enabled"],
                          warning_percent=row["warning_percent"])
            for row in all_limits
        ]
        spending = [
            CategorySpending(owner=owner, category_id=row["category_id"], currency=row["currency"],
                             expenses=row["expenses"], refunds=row["refunds"], archived=row["archived"])
            for row in totals
        ]
        try:
            evaluated = evaluate_spending_limits(owner, period, limits, spending)
        except SpendingLimitCapError:
            raise AnalyticsError(_LIMIT_ERROR, 413) from None
        except SpendingLimitInputError:
            raise AnalyticsError(_DATA_ERROR, 500) from None
        return [
            {
                "kind": inbox_mod.KIND_SPENDING,
                "subject_id": inbox_mod.opaque_subject_id(
                    owner, inbox_mod.KIND_SPENDING, f"{item.category_id}\0{item.currency}"),
                "milestone": inbox_mod.spending_limit_milestone(item.period, item.status),
                "ref": inbox_mod.spending_limit_ref(category_id=item.category_id, currency=item.currency),
                "params": inbox_mod.spending_limit_params(
                    status=item.status, period=item.period, currency=item.currency,
                    warning_percent=item.warning_percent, percent_used_floor=item.percent_used_floor,
                ),
            }
            for item in evaluated if item.status != "within"
        ]

    def _evaluate_active_inbox(self, session, owner, evaluated_at, evaluation_date, *, use_cache, kinds=None):
        selected = set(inbox_mod.INBOX_KINDS) if kinds is None else kinds
        entries = []
        try:
            if inbox_mod.KIND_CONSENT in selected:
                entries.extend(self._inbox_consent_entries(session, owner, evaluated_at))
            if inbox_mod.KIND_MORTGAGE in selected:
                entries.extend(self._inbox_mortgage_entries(session, owner, evaluation_date))
            if inbox_mod.KIND_SPENDING in selected:
                entries.extend(self._inbox_spending_entries(session, owner, evaluation_date[:7], use_cache=use_cache))
        except inbox_mod.OwnerInboxDataError:
            raise AnalyticsError(_DATA_ERROR, 500) from None
        return entries

    @staticmethod
    def _inbox_read_state(session, owner):
        state = session.get(db.BudgetOwnerInboxState, owner)
        reads_revision = 0 if state is None else state.reads_revision
        read_rows = list(session.scalars(
            select(db.BudgetOwnerInboxRead).where(db.BudgetOwnerInboxRead.owner == owner)
            .order_by(db.BudgetOwnerInboxRead.kind, db.BudgetOwnerInboxRead.subject_id,
                      db.BudgetOwnerInboxRead.milestone)))
        tuples = [(row.kind, row.subject_id, row.milestone, float(row.read_at)) for row in read_rows]
        try:
            reads_version = inbox_mod.reads_version_digest(owner, reads_revision, tuples)
        except inbox_mod.OwnerInboxDataError:
            raise AnalyticsError(_DATA_ERROR, 500) from None
        return state, reads_revision, read_rows, tuples, reads_version

    def inbox(self, owner):
        """Pure read-only live active alerts inbox evaluated in one SQLite read snapshot."""
        evaluated_at = float(self._now())
        evaluation_date = datetime.fromtimestamp(evaluated_at, timezone.utc).date().isoformat()
        with _read_lock, self._read(owner) as session:
            _, reads_revision, read_rows, tuples, reads_version = self._inbox_read_state(session, owner)
            active = self._evaluate_active_inbox(session, owner, evaluated_at, evaluation_date, use_cache=True)
            read_map = {(kind, subject_id, milestone): read_at for kind, subject_id, milestone, read_at in tuples}
            try:
                items = inbox_mod.build_inbox_items(active, read_map)
            except inbox_mod.OwnerInboxDataError:
                raise AnalyticsError(_DATA_ERROR, 500) from None
            return {
                "evaluation_date": evaluation_date,
                "evaluation_timezone": "UTC",
                "evaluated_at": evaluated_at,
                "reads_revision": reads_revision,
                "reads_version": reads_version,
                "reads_count": len(read_rows),
                "reads_cap": inbox_mod.MAX_INBOX_READS,
                "unread_count": sum(not item["read"] for item in items),
                "rule_version": inbox_mod.RULE_VERSION,
                "items": items,
            }

    def _apply_inbox_mark_read(self, session, owner, changed_keys, read_rows, tuples, now):
        evaluation_date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        active = self._evaluate_active_inbox(
            session, owner, now, evaluation_date, use_cache=False,
            kinds={kind for kind, _, _ in changed_keys},
        )
        active_keys = {(entry["kind"], entry["subject_id"], entry["milestone"]) for entry in active}
        if any(key not in active_keys for key in changed_keys):
            raise AnalyticsError("budget_analytics_source_changed", 409)
        if len(read_rows) + len(changed_keys) > inbox_mod.MAX_INBOX_READS:
            raise AnalyticsError(_LIMIT_ERROR, 413)
        for kind, subject_id, milestone in changed_keys:
            session.add(db.BudgetOwnerInboxRead(
                owner=owner, kind=kind, subject_id=subject_id, milestone=milestone, read_at=now))
        return tuples + [(kind, subject_id, milestone, now) for kind, subject_id, milestone in changed_keys]

    @staticmethod
    def _apply_inbox_unmark(session, changed_keys, existing_by_key, tuples):
        removed = set(changed_keys)
        for key in changed_keys:
            session.delete(existing_by_key[key])
        return [row for row in tuples if (row[0], row[1], row[2]) not in removed]

    def _record_inbox_mark_audit(self, session, owner, changed_keys, read, old_revision, new_revision):
        action = "mark_read" if read else "mark_unread"
        for kind, subject_id, milestone in changed_keys:
            token = inbox_mod.opaque_key_token(kind, subject_id, milestone)
            self._analytics_audit(
                session, owner, "owner_inbox_read", token, action,
                {"key_token": token, "kind": kind, "reads_revision": old_revision, "read": not read},
                {"key_token": token, "kind": kind, "reads_revision": new_revision, "read": read},
            )

    def mark_inbox(self, owner, *, items, read, expected_reads_version):
        """Atomically mark or unmark 1..100 inbox alert keys under anti-ABA reads_version CAS."""
        if (type(read) is not bool
                or not isinstance(expected_reads_version, str)
                or not inbox_mod._HEX64.fullmatch(expected_reads_version)):
            raise AnalyticsError()
        keys = inbox_mod.validate_mark_items(items)
        with self.transaction() as session:
            self._owner(session, owner)
            state, reads_revision, read_rows, tuples, current_version = self._inbox_read_state(session, owner)
            if not hmac.compare_digest(current_version, expected_reads_version):
                raise AnalyticsError("budget_analytics_conflict", 409)
            existing_by_key = {(row.kind, row.subject_id, row.milestone): row for row in read_rows}
            changed_keys = [key for key in keys if (key not in existing_by_key if read else key in existing_by_key)]
            if not changed_keys:
                return {
                    "read": read, "changed": False, "changed_count": 0,
                    "reads_revision": reads_revision, "reads_version": current_version,
                    "reads_count": len(read_rows), "reads_cap": inbox_mod.MAX_INBOX_READS,
                }
            now = float(self._now())
            new_tuples = (
                self._apply_inbox_mark_read(session, owner, changed_keys, read_rows, tuples, now)
                if read else self._apply_inbox_unmark(session, changed_keys, existing_by_key, tuples)
            )
            new_revision = reads_revision + 1
            if state is None:
                session.add(db.BudgetOwnerInboxState(owner=owner, reads_revision=new_revision, updated_at=now))
            else:
                state.reads_revision, state.updated_at = new_revision, now
            session.flush()
            new_version = inbox_mod.reads_version_digest(owner, new_revision, new_tuples)
            self._record_inbox_mark_audit(session, owner, changed_keys, read, reads_revision, new_revision)
            return {
                "read": read, "changed": True, "changed_count": len(changed_keys),
                "reads_revision": new_revision, "reads_version": new_version,
                "reads_count": len(new_tuples), "reads_cap": inbox_mod.MAX_INBOX_READS,
            }

    @staticmethod
    def _quarantine_counts(session, account_ids):
        if not account_ids:
            return {}, {}, {}
        uncertain = dict(session.execute(
            select(db.BudgetTransaction.account_id, func.count())
            .where(db.BudgetTransaction.account_id.in_(account_ids), db.BudgetTransaction.uncertain.is_(True))
            .group_by(db.BudgetTransaction.account_id)).all())
        unresolved = dict(session.execute(
            select(db.BudgetUnresolved.account_id, func.count())
            .where(db.BudgetUnresolved.account_id.in_(account_ids))
            .group_by(db.BudgetUnresolved.account_id)).all())
        imported = dict(session.execute(
            select(db.BudgetImportBatch.account_id, func.count())
            .select_from(db.BudgetImportUnresolved)
            .join(db.BudgetImportBatch, db.BudgetImportUnresolved.batch_id == db.BudgetImportBatch.id)
            .where(db.BudgetImportBatch.account_id.in_(account_ids))
            .group_by(db.BudgetImportBatch.account_id)).all())
        return uncertain, unresolved, imported

    @staticmethod
    def _quarantine_items(session, account_ids, selected_map):
        if not account_ids:
            return []
        cap = quarantine_mod.MAX_QUARANTINE_ITEMS
        raw_items = []
        uncertain_rows = session.scalars(
            select(db.BudgetTransaction)
            .where(db.BudgetTransaction.account_id.in_(account_ids), db.BudgetTransaction.uncertain.is_(True))
            .order_by(db.BudgetTransaction.observed_at.desc(), db.BudgetTransaction.account_id,
                      db.BudgetTransaction.entry_reference)
            .limit(cap))
        for row in uncertain_rows:
            raw_items.append(quarantine_mod.extract_safe_item(
                bucket=quarantine_mod.BUCKET_UNCERTAIN, account_id=row.account_id,
                selected=selected_map[row.account_id], payload_json=row.payload_json,
                source=row.source, observed_at=row.observed_at,
            ))
        unresolved_rows = session.scalars(
            select(db.BudgetUnresolved)
            .where(db.BudgetUnresolved.account_id.in_(account_ids))
            .order_by(db.BudgetUnresolved.observed_at.desc(), db.BudgetUnresolved.account_id,
                      db.BudgetUnresolved.ordinal)
            .limit(cap))
        for row in unresolved_rows:
            raw_items.append(quarantine_mod.extract_safe_item(
                bucket=quarantine_mod.BUCKET_UNRESOLVED, account_id=row.account_id,
                selected=selected_map[row.account_id], payload_json=row.payload_json,
                source="api", observed_at=row.observed_at,
            ))
        import_rows = session.execute(
            select(db.BudgetImportUnresolved, db.BudgetImportBatch.account_id, db.BudgetImportBatch.fetched_at)
            .join(db.BudgetImportBatch, db.BudgetImportUnresolved.batch_id == db.BudgetImportBatch.id)
            .where(db.BudgetImportBatch.account_id.in_(account_ids))
            .order_by(db.BudgetImportBatch.fetched_at.desc(), db.BudgetImportBatch.account_id,
                      db.BudgetImportUnresolved.batch_id, db.BudgetImportUnresolved.ordinal)
            .limit(cap)).all()
        for row, acc_id, fetched_at in import_rows:
            raw_items.append(quarantine_mod.extract_safe_item(
                bucket=quarantine_mod.BUCKET_IMPORT_UNRESOLVED, account_id=acc_id,
                selected=selected_map[acc_id], payload_json=row.payload_json,
                source="offline_import", observed_at=fetched_at, stored_reason=row.reason,
            ))
        return quarantine_mod.sort_quarantine_items(raw_items, limit=cap)

    def quarantine(self, owner, *, account_id=None):
        """Pure read-only quarantine counts and safe ref/date/status/reason details (zero prose/amounts)."""
        if account_id is not None and (not isinstance(account_id, str) or not account_id or len(account_id) > 128):
            raise AnalyticsError()
        with _read_lock, self._read(owner) as session:
            accounts = list(session.scalars(
                select(db.BudgetAccount).join(db.BudgetConnection).where(
                    db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner)
                .order_by(db.BudgetAccount.id)))
            if account_id is not None:
                accounts = [row for row in accounts if row.id == account_id]
                if not accounts:
                    raise AnalyticsError("budget_analytics_not_found", 404)
            account_ids = [row.id for row in accounts]
            selected_map = {row.id: row.enabled for row in accounts}
            uncertain_counts, unresolved_counts, import_counts = self._quarantine_counts(session, account_ids)
            try:
                items = self._quarantine_items(session, account_ids, selected_map)
            except quarantine_mod.QuarantineDataError:
                raise AnalyticsError(_DATA_ERROR, 500) from None
            account_rows = []
            for acc in accounts:
                u_cnt = uncertain_counts.get(acc.id, 0)
                r_cnt = unresolved_counts.get(acc.id, 0)
                i_cnt = import_counts.get(acc.id, 0)
                account_rows.append({
                    "account_id": acc.id,
                    "selected": acc.enabled,
                    "uncertain_count": u_cnt,
                    "unresolved_count": r_cnt,
                    "import_unresolved_count": i_cnt,
                    "total_count": u_cnt + r_cnt + i_cnt,
                })
            total_u = sum(row["uncertain_count"] for row in account_rows)
            total_r = sum(row["unresolved_count"] for row in account_rows)
            total_i = sum(row["import_unresolved_count"] for row in account_rows)
            total_all = total_u + total_r + total_i
            return {
                "rule_version": quarantine_mod.RULE_VERSION,
                "summary": {
                    "uncertain_count": total_u,
                    "unresolved_count": total_r,
                    "import_unresolved_count": total_i,
                    "total_count": total_all,
                },
                "accounts": account_rows,
                "items_cap": quarantine_mod.MAX_QUARANTINE_ITEMS,
                "truncated": total_all > len(items),
                "items": items,
            }


def _row_filter(row, category_id, kind, query):
    identifier = row["effective"]["category_id"]
    if category_id == "uncategorised" and identifier is not None:
        return False
    if category_id not in (None, "uncategorised") and identifier != category_id:
        return False
    if kind is not None and row["effective"]["kind"] != kind:
        return False
    text = "\n".join(row["payload"]["remittance_information"])
    metadata = row["metadata"] or {}
    text += "\n" + (metadata.get("creditor_name") or "") + "\n" + (metadata.get("debtor_name") or "")
    return not query or query in text.casefold()
