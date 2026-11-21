"""Owner-selected, exact spending projections; read-only SQLite snapshots.

The same snapshot digest fences rule previews and encrypted pagination. Transfer
matching always precedes period filtering; only current, accepted pairs affect
totals. No configuration, provider, file, secret or acquisition operation runs here.
"""
from __future__ import annotations

import base64
from collections import defaultdict
from datetime import date
import json
import math
import os
import re

from cryptography.exceptions import InvalidTag
from sqlalchemy import func, select

from .. import db
from . import analytics_core as core
from .analytics_core import AnalyticsError
from .analytics_store import BudgetAnalyticsStore
from .client import _reject_constant, _unique_error_fields
from .scheduler import shared_state
from .views import BudgetHTTPStore

MAX_ROWS = 100_000
MAX_EDGES = 100_000
CURSOR_TTL = 900
MAX_CURSOR = 8192
_DOMAIN = b"hestia/budget/analytics/cursor/v1"
_PAYLOAD_FIELDS = ("amount", "currency", "credit_debit_indicator", "status", "booking_date", "value_date",
                   "transaction_date", "remittance_information")
_KINDS = frozenset({"expense", "income", "refund", "excluded", "unclassified_credit", "transfer"})
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
    for row in ledger:
        payload, financial_fp = _payload(row.payload_json)
        key = (row.account_id, row.entry_reference)
        sidecar = metadata.get(key)
        evidence = _metadata(sidecar.payload_json) if sidecar is not None and sidecar.ledger_fingerprint == financial_fp else None
        annotation = _annotation(annotations.get(key), financial_fp)
        effective = _effective(payload, evidence, annotation, categories, rules)
        result.append({"account_id": row.account_id, "entry_reference": row.entry_reference, "payload": payload,
                       "financial_fp": financial_fp,
                       "transfer_fp": core.pair_fingerprint(row.account_id, row.entry_reference, payload, evidence, identities),
                       "metadata": evidence, "annotation": annotation, "effective": effective,
                       "base_effective": dict(effective), "source": row.source, "observed_at": row.observed_at,
                       "api_observed_at": row.api_observed_at, "uncertain": row.uncertain,
                       "eligible": not row.uncertain and payload["status"] == "BOOK" and core.effective_date(payload) is not None,
                       "transfer": {"status": "none", "decision_id": None, "candidate_count": 0, "ambiguous": False}})
    return result


def _scope_counts(session, owner, accounts):
    def count(model, on):
        return session.scalar(select(func.count()).select_from(model).join(db.BudgetAccount, on).join(db.BudgetConnection).where(
            db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner))
    uncertain = session.scalar(select(func.count()).select_from(db.BudgetTransaction).join(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetTransaction.uncertain))
    imports = select(db.BudgetImportBatch.id).join(db.BudgetAccount).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner)
    imported = session.scalar(select(func.count()).select_from(db.BudgetImportUnresolved).where(
        db.BudgetImportUnresolved.batch_id.in_(imports)))
    return {"selected_accounts": sum(account.enabled for account in accounts),
            "excluded_accounts": sum(not account.enabled for account in accounts), "uncertain_count": uncertain,
            "unresolved_count": count(db.BudgetUnresolved, db.BudgetUnresolved.account_id == db.BudgetAccount.id),
            "import_unresolved_count": imported}


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
                "ledger": [[row.account_id, row.entry_reference, _json(row.payload_json), row.uncertain, row.source] for row in ledger],
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
                      core.effective_date(payload), row["transfer_fp"], _identity_pairs(metadata.get(field, []))))
    selected = {row["id"] for row in snapshot["accounts"] if row["enabled"]}
    own = {key: _identity_pairs(value) for key, value in snapshot["identities"].items() if key in selected}
    try:
        return match_transfers(snapshot["owner"], entries, own, blocked_pairs=frozenset(blocked),
                               reserved_entries=frozenset(reserved), max_edges=MAX_EDGES), False
    except TransferMatchLimitError:
        return (), True
    except TransferInputError:
        raise AnalyticsError() from None


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


class BudgetAnalyticsViews(BudgetAnalyticsStore):
    def _projection(self, session, owner):
        value = projection_snapshot(session, owner)
        _transfer_projection(value)
        return value

    def _freshness(self, session, snapshot, now):
        connections = {row.id: row for row in session.scalars(select(db.BudgetConnection).where(db.BudgetConnection.owner == snapshot["owner"]))}
        components = {(row.account_id, row.operation): row for row in session.scalars(select(db.BudgetComponent).join(db.BudgetAccount).where(
            db.BudgetAccount.owner == snapshot["owner"], db.BudgetAccount.enabled))}
        shared = {key: shared_state(session, row) for key, row in connections.items()}
        return [{"account_id": account.id, **{operation: self._component_status(
            components.get((account.id, operation)), operation, account, connections[account.connection_id], now,
            shared[account.connection_id]) for operation in ("transactions", "balances")}}
            for account in snapshot["_accounts"] if account.enabled]

    def overview(self, owner, period, *, currency=None):
        start, end = core.parse_period(period)
        currency = _currency(currency)
        with self._read(owner) as session:
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
        with self._read(owner) as session:
            snapshot = self._projection(session, owner)
            if category_id not in (None, "uncategorised") and category_id not in {row["id"] for row in snapshot["categories"]}:
                raise AnalyticsError("budget_analytics_not_found", 404)
            rows = [row for row in _period_rows(snapshot, start, end, currency) if _row_filter(row, category_id, kind, query)]
            rows.sort(key=lambda row: (core.effective_date(row["payload"]), row["account_id"], row["entry_reference"]), reverse=True)
            page, next_cursor = self._page(rows, cursor, filters, snapshot["digest"])
            return {"period": period, "start": start, "end": end, "snapshot_digest": snapshot["digest"],
                    "transactions": [_wire_transaction(row) for row in page], "filtered_total": len(rows), "next_cursor": next_cursor}

    def transfers(self, owner, period, *, currency=None, status="suggested", limit=50, cursor=None):
        start, end = core.parse_period(period)
        currency = _currency(currency)
        if status not in ("suggested", "accepted", "review", "boundary", "rejected", "undone"):
            raise AnalyticsError()
        filters = {"owner": owner, "operation": "transfers", "period": period, "currency": currency,
                   "status": status, "limit": limit}
        with self._read(owner) as session:
            snapshot = self._projection(session, owner)
            rows = [row for row in _period_transfers(snapshot, start, end, currency) if row["status"] == status]
            rows.sort(key=lambda row: (max(row["debit"]["date"] or "", row["credit"]["date"] or ""),
                                       row["debit"]["account_id"], row["debit"]["entry_reference"],
                                       row["credit"]["account_id"], row["credit"]["entry_reference"]), reverse=True)
            page, next_cursor = self._page(rows, cursor, filters, snapshot["digest"])
            return {"period": period, "start": start, "end": end, "snapshot_digest": snapshot["digest"],
                    "transfers": page, "filtered_total": len(rows), "next_cursor": next_cursor,
                    "scan_limited": snapshot["_scan_limited"]}


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
