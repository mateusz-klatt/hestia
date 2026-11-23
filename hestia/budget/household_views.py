"""Allowlisted household serializers. Owner projections stay on the owner routes.

Reads authorize before the shared analytics lock, then again inside the snapshot.
One-off month rows are summed into their category. Cursors seal the page under a
household domain; the actor, owner, grant, route and filters are additional data,
so the token length does not depend on the owner's name.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select, text

from .. import db
from . import analytics_core as core
from . import analytics_views
from . import month_budget as month_budget_mod
from . import owner_inbox as inbox_mod
from .analytics_core import AnalyticsError
from .analytics_views import (
    MAX_RECURRING_HINTS, BudgetAnalyticsViews, _KINDS, _category_totals, _currency, _period_rows,
    _read_lock, _snapshot_recurring, _spending_limit_statuses, _totals,
)
from .config import BudgetConfigError
from .household import HouseholdAuthError, category_label, entity_alias, require_household_data_grant
from .i18n import DisplayResolver

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_CURSOR_TTL = 900
_MAX_CURSOR = 8192
_CURSOR_DOMAIN = b"hestia/budget/household/cursor/v1"
_AUDIT_INTERVAL = 60
_AUDIT_RETENTION = 180 * 86400
_TRANSFER_STATUSES = frozenset({"suggested", "accepted", "review", "boundary", "rejected", "undone"})
_RULE_FIELDS = frozenset({"description", "counterparty_name", "mcc"})
_RULE_OPERATORS = frozenset({"contains", "equals"})
_OVERVIEW_MONEY = ("expenses", "refunds", "income", "net_spending")
_BUDGET_KEYS = (
    "period", "start", "end", "cutoff", "current_month", "evaluated_at", "evaluation_date",
    "evaluation_timezone", "currency", "available_currencies", "configuration", "coverage",
    "summary", "expected_income", "pace", "category_spend", "pass_through", "card_exposure",
)
_QUERY = {
    "access": frozenset(),
    "report-accounts": frozenset(),
    "monthly-budget": frozenset({"period", "currency"}),
    "overview": frozenset({"period", "currency"}),
    "transactions": frozenset({"period", "currency", "kind", "account_id", "category_id", "limit", "cursor"}),
    "categories": frozenset(),
    "rules": frozenset(),
    "transfers": frozenset({"period", "currency", "status", "limit", "cursor"}),
    "spending-limits": frozenset({"period", "currency"}),
    "recurring": frozenset({"currency"}),
    "mortgages": frozenset(),
    "inbox": frozenset(),
    "quarantine": frozenset(),
    "monthly-budget-settings": frozenset(),
}
_REQUIRED_QUERY = {
    "overview": frozenset({"period"}),
    "transactions": frozenset({"period"}),
    "transfers": frozenset({"period"}),
    "spending-limits": frozenset({"period"}),
}
_MONTHS = 12


class AliasBook:
    """Grant-scoped aliases for the visible selected accounts and owner categories."""

    def __init__(self, secret, grant_id, accounts, categories, display=None):
        self.secret = secret
        self.display = display
        self.grant_id = grant_id
        self.account_ordinal = {identifier: index for index, identifier in enumerate(sorted(accounts), 1)}
        self.category_ordinal = {row["id"]: index for index, row in enumerate(sorted(categories, key=lambda item: item["id"]), 1)}
        self.category_meta = {row["id"]: row for row in categories}
        self.accounts = {self.alias("account", identifier): identifier for identifier in accounts}
        self.categories = {self.alias("category", row["id"]): row["id"] for row in categories}

    def alias(self, domain, key):
        return entity_alias(self.secret, self.grant_id, domain, key)

    def account(self, identifier):
        if identifier not in self.account_ordinal:
            raise AnalyticsError("budget_analytics_data_error", 500)
        return self.alias("account", identifier)

    def category(self, identifier):
        if identifier is None:
            return None
        if identifier not in self.category_ordinal:
            raise AnalyticsError("budget_analytics_data_error", 500)
        return self.alias("category", identifier)

    def label(self, identifier):
        if identifier is None:
            return None
        meta = self.category_meta.get(identifier)
        if meta is None:
            raise AnalyticsError("budget_analytics_data_error", 500)
        return category_label(meta["name"], self.category_ordinal[identifier])

    def display_name(self, identifier):
        if identifier is None:
            return None
        canonical = self.category_meta[identifier]["name"]
        return canonical if self.display is None else self.display.category(identifier, canonical)

    def resolve_account(self, alias):
        identifier = self.accounts.get(alias)
        if identifier is None:
            raise AnalyticsError("budget_analytics_not_found", 404)
        return identifier

    def resolve_category(self, alias):
        identifier = self.categories.get(alias)
        if identifier is None:
            raise AnalyticsError("budget_analytics_not_found", 404)
        return identifier


class HouseholdReader:
    def __init__(self, sessions, *, cursor_secret):
        if not isinstance(cursor_secret, bytes) or not cursor_secret:
            raise AnalyticsError("budget_analytics_unavailable", 503)
        self.sessions = sessions
        self.secret = cursor_secret
        self.views = BudgetAnalyticsViews(sessions, cursor_secret=cursor_secret)
        self._cipher = AESGCM(hashlib.sha256(_CURSOR_DOMAIN + b"\0" + cursor_secret).digest())

    def read(self, member, operation, query, header):
        """Authorize and serialize inside one snapshot. Returns ``(body, owner, grant_id)``.

        Eligibility runs before ``_read_lock`` so a denial does not block owner analytics.
        The snapshot repeats that check. The audit row is written later, in the same
        immediate transaction as the final grant check.
        """
        require_header = operation != "access"
        self._eligible(member, header, require_header)
        with _read_lock, self.sessions() as session:
            session.execute(text("BEGIN"))
            _member, owner, grant_id = require_household_data_grant(
                session, member, header=header, require_header=require_header)
            values = _normalize_query(operation, query)
            payload = self._build(session, member, owner, grant_id, operation, values)
            body = _encode(payload)
            return body, owner, grant_id

    def _eligible(self, member, header, require_header):
        """Grant check with no analytics lock. An invalid header fails before any lookup."""
        with self.sessions() as session:
            session.execute(text("BEGIN"))
            require_household_data_grant(session, member, header=header, require_header=require_header)

    def commit_authorized_read(self, member, owner, grant_id, header, view):
        """Re-authorize exactly this grant, then audit it, in one ``BEGIN IMMEDIATE`` transaction.

        The caller sends the body only after this returns. A denial rolls the transaction
        back, so no audit row is kept. Discovery re-authorizes and writes nothing. Any other
        allowlisted view inserts at most one row per member and view per 60 seconds, and
        that insert deletes household audit rows older than 180 days. A recent row skips
        the insert and still re-authorizes in this same transaction.
        """
        if view not in _QUERY:
            raise AnalyticsError()
        require_header = view != "access"
        with self.views.transaction() as session:
            _member, current_owner, current_grant = require_household_data_grant(
                session, member, header=header, require_header=require_header)
            if current_owner != owner or not hmac.compare_digest(current_grant, grant_id):
                raise HouseholdAuthError(403, "budget_forbidden")
            if view == "access":
                return
            now = self.views._now()
            if self._household_read_is_recent(session, member, view, now):
                return
            self._prune_household_reads(session, now)
            self.views._analytics_audit(session, owner, "household_read", member, view, None, None)

    def _household_read_is_recent(self, session, member, view, now):
        found = session.scalar(select(db.BudgetAnalyticsAudit.id).where(
            db.BudgetAnalyticsAudit.subject_type == "household_read",
            db.BudgetAnalyticsAudit.subject_id == member,
            db.BudgetAnalyticsAudit.action == view,
            db.BudgetAnalyticsAudit.changed_at >= now - _AUDIT_INTERVAL,
        ).limit(1))
        return found is not None

    def _prune_household_reads(self, session, now):
        session.execute(delete(db.BudgetAnalyticsAudit).where(
            db.BudgetAnalyticsAudit.subject_type == "household_read",
            db.BudgetAnalyticsAudit.changed_at < now - _AUDIT_RETENTION,
        ))

    def _build(self, session, member, owner, grant_id, operation, values):
        if operation == "access":
            return {"grant_id": grant_id}
        book = self._book(session, owner, grant_id, member)
        if operation == "report-accounts":
            return _report_accounts(book, grant_id)
        if operation == "categories":
            return _categories(book, grant_id)
        if operation == "rules":
            return _rules(session, owner, book, grant_id)
        if operation == "quarantine":
            return _quarantine(session, owner, grant_id)
        if operation == "monthly-budget-settings":
            return _settings(session, owner, book, grant_id)
        if operation == "mortgages":
            return self._mortgages(session, owner, book, grant_id)
        if operation == "inbox":
            return self._inbox(session, owner, book, grant_id)
        snapshot = self.views._projection(session, owner)
        if operation == "overview":
            return _overview(snapshot, values, book, grant_id)
        if operation == "transactions":
            return self._transactions(snapshot, member, owner, values, book, grant_id)
        if operation == "transfers":
            return self._transfers(snapshot, member, owner, values, book, grant_id)
        if operation == "recurring":
            return _recurring(snapshot, values, grant_id)
        if operation == "spending-limits":
            return _limits(session, snapshot, owner, values, book, grant_id)
        if operation == "monthly-budget":
            return self._monthly(session, snapshot, owner, values, book, grant_id)
        raise AnalyticsError()

    def _book(self, session, owner, grant_id, member):
        accounts = session.scalars(select(db.BudgetAccount.id).join(db.BudgetConnection).where(
            db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled)
            .order_by(db.BudgetAccount.id))
        categories = [{"id": row.id, "name": row.name, "archived": row.archived} for row in session.scalars(
            select(db.BudgetCategory).where(db.BudgetCategory.owner == owner).order_by(db.BudgetCategory.id))]
        return AliasBook(self.secret, grant_id, list(accounts), categories, DisplayResolver(session, owner, member))

    def _monthly(self, session, snapshot, owner, values, book, grant_id):
        period, currency = values["period"], values["currency"]
        selected = self._evaluate(session, snapshot, owner, period, currency, include_series=True)
        daily = selected.pop("_series_daily", None)
        if not isinstance(daily, list) or not daily:
            raise AnalyticsError("budget_analytics_data_error", 500)
        current = selected["current_month"]
        chosen = selected["period"]
        months = []
        labels = {}
        for month in _month_span(chosen):
            payload = selected if month == chosen else self._evaluate(
                session, snapshot, owner, month, currency, include_series=False)
            months.append(_month_bucket(payload, current, book))
            for row in _fold_category_spend(payload["category_spend"]):
                if row["category_id"] is not None:
                    labels[row["category_id"]] = row["archived"]
        return {
            "grant_id": grant_id,
            "budget": _sanitize_budget(selected, book),
            "series": {
                "daily": daily,
                "months": months,
                "category_labels": [
                    {"category_id": book.category(identifier), "name": book.label(identifier),
                     "display_name": book.display_name(identifier), "archived": archived}
                    for identifier, archived in sorted(labels.items())
                ],
            },
        }

    def _evaluate(self, session, snapshot, owner, period, currency, *, include_series):
        try:
            evaluated_at = float(self.views._now())
            _date, current_month = month_budget_mod.warsaw_date(evaluated_at)
            if period is not None and period > current_month:
                raise month_budget_mod.MonthInputError()
            month_configs = month_budget_mod.load_month_config()
            settings = None if month_configs is None else month_configs.get(owner)
            limits = [_limit_dict(row) for row in session.scalars(
                select(db.BudgetCategoryLimit).where(db.BudgetCategoryLimit.owner == owner)
                .order_by(db.BudgetCategoryLimit.currency, db.BudgetCategoryLimit.category_id,
                          db.BudgetCategoryLimit.id))]
            timestamps = self.views._selected_transaction_component_timestamps(session, owner)
            kinds = {row["id"]: row["kind"] for row in snapshot["accounts"]
                     if row["enabled"] and row["kind_confirmed_at"] is not None}
            return month_budget_mod.evaluate_monthly_budget(
                owner=owner, period=period, currency=currency, evaluated_at=evaluated_at,
                transactions=snapshot["transactions"], transfers=snapshot["_transfers"],
                scan_limited=snapshot["_scan_limited"], categories=snapshot["categories"],
                rules=snapshot["rules"], identities=snapshot["identities"], limits=limits,
                account_component_timestamps=timestamps, account_kinds=kinds, settings=settings,
                one_off_secret=self.secret, include_series=include_series,
            )
        except month_budget_mod.MonthInputError:
            raise AnalyticsError() from None
        except month_budget_mod.MonthLimitError:
            raise AnalyticsError("budget_analytics_limit", 413) from None
        except month_budget_mod.MonthDataError:
            raise AnalyticsError("budget_analytics_data_error", 500) from None

    def _transactions(self, snapshot, member, owner, values, book, grant_id):
        start, end = core.parse_period(values["period"])
        account_filter = None if values["account_id"] is None else book.resolve_account(values["account_id"])
        category_filter = None if values["category_id"] is None else book.resolve_category(values["category_id"])
        kind = values["kind"]
        rows = []
        for row in _period_rows(snapshot, start, end, values["currency"]):
            if account_filter is not None and row["account_id"] != account_filter:
                continue
            if kind is not None and row["effective"]["kind"] != kind:
                continue
            category_id = row["effective"]["category_id"]
            if values["category_id"] is not None and category_id != category_filter:
                continue
            rows.append(row)
        rows.sort(key=lambda row: (core.effective_date(row["payload"]), row["account_id"], row["entry_reference"]),
                  reverse=True)
        safe = [_safe_transaction(row, book) for row in rows]
        filters = {"period": values["period"], "currency": values["currency"], "kind": kind,
                   "account_id": values["account_id"], "category_id": values["category_id"], "limit": values["limit"]}
        page, cursor = self._page(safe, values["cursor"], _cursor_context(
            member, owner, grant_id, "transactions", filters), _safe_digest(safe), values["limit"])
        return {"grant_id": grant_id, "transactions": page, "filtered_total": len(safe), "next_cursor": cursor}

    def _transfers(self, snapshot, member, owner, values, book, grant_id):
        start, end = core.parse_period(values["period"])
        status = values["status"]
        rows = []
        for row in analytics_views._period_transfers(snapshot, start, end, values["currency"]):
            if row["status"] != status:
                continue
            debit, credit = _safe_leg(row["debit"], book), _safe_leg(row["credit"], book)
            if debit is None or credit is None:
                continue
            key = "\0".join((row["debit"]["account_id"], row["debit"]["entry_reference"],
                             row["credit"]["account_id"], row["credit"]["entry_reference"]))
            rows.append({"id": book.alias("transfer", key), "status": row["status"], "debit": debit, "credit": credit})
        rows.sort(key=lambda row: (max(row["debit"]["date"], row["credit"]["date"]), row["id"]), reverse=True)
        filters = {"period": values["period"], "currency": values["currency"], "status": status, "limit": values["limit"]}
        page, cursor = self._page(rows, values["cursor"], _cursor_context(
            member, owner, grant_id, "transfers", filters), _safe_digest(rows), values["limit"])
        return {"grant_id": grant_id, "transfers": page, "filtered_total": len(rows), "next_cursor": cursor}

    def _mortgages(self, session, owner, book, grant_id):
        evaluation_date = datetime.fromtimestamp(self.views._now(), timezone.utc).date().isoformat()
        schedules, _ledger, reminders = self.views._mortgage_reminders_in_session(session, owner, evaluation_date)
        ordered = sorted(schedules, key=lambda row: row["id"])
        ordinals = {row["id"]: index for index, row in enumerate(ordered, 1)}
        safe_schedules = []
        for row in ordered:
            safe_schedules.append({
                "id": book.alias("mortgage", row["id"]),
                "ordinal": ordinals[row["id"]],
                "currency": row["currency"],
                "amount": row["amount"],
                "enabled": bool(row["enabled"]),
                "due_day": row["due_day"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "paid_months": list(row["paid_months"]),
            })
        safe_reminders = []
        for item in reminders:
            if item.schedule_id not in ordinals:
                raise AnalyticsError("budget_analytics_data_error", 500)
            safe_reminders.append({
                "schedule_id": book.alias("mortgage", item.schedule_id),
                "due_date": item.due_date,
                "status": item.status,
            })
        return {"grant_id": grant_id, "evaluation_date": evaluation_date,
                "schedules": safe_schedules, "reminders": safe_reminders}

    def _inbox(self, session, owner, book, grant_id):
        evaluated_at = float(self.views._now())
        evaluation_date = datetime.fromtimestamp(evaluated_at, timezone.utc).date().isoformat()
        _state, _revision, _rows, tuples, _version = self.views._inbox_read_state(session, owner)
        active = self.views._evaluate_active_inbox(
            session, owner, evaluated_at, evaluation_date, use_cache=True,
            kinds={inbox_mod.KIND_MORTGAGE, inbox_mod.KIND_SPENDING})
        read_map = {(kind, subject_id, milestone): read_at for kind, subject_id, milestone, read_at in tuples}
        try:
            items = inbox_mod.build_inbox_items(active, read_map)
        except inbox_mod.OwnerInboxDataError:
            raise AnalyticsError("budget_analytics_data_error", 500) from None
        safe = []
        for item in items:
            if item["kind"] not in (inbox_mod.KIND_MORTGAGE, inbox_mod.KIND_SPENDING):
                continue
            params, ref = item["params"], item["ref"]
            shared = {
                "id": book.alias("inbox", item["kind"] + "\0" + item["subject_id"] + "\0" + item["milestone"]),
                "kind": item["kind"],
                "read": bool(item["read"]),
                "status": params["status"],
            }
            if item["kind"] == inbox_mod.KIND_MORTGAGE:
                shared["schedule_id"] = book.alias("mortgage", ref["schedule_id"])
                shared["due_date"] = params["due_date"]
            else:
                shared["category_id"] = book.category(ref["category_id"])
                shared["currency"] = params["currency"]
                shared["period"] = params["period"]
            safe.append(shared)
        return {"grant_id": grant_id, "evaluation_date": evaluation_date,
                "unread_count": sum(not item["read"] for item in safe), "items": safe}

    def _page(self, rows, cursor, context, digest, limit):
        now = self.views._now()
        index = self._cursor_index(cursor, context, digest, now)
        if index > len(rows):
            raise AnalyticsError()
        end = min(len(rows), index + limit)
        nxt = self._cursor_encode(context, digest, end, now) if end < len(rows) else None
        return rows[index:end], nxt

    def _cursor_encode(self, context, digest, index, now):
        payload = {"digest": digest, "index": index, "issued": now, "expires": now + _CURSOR_TTL}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        token = base64.urlsafe_b64encode(
            nonce + self._cipher.encrypt(nonce, data, _cursor_aad(context))).rstrip(b"=").decode("ascii")
        if len(token) > _MAX_CURSOR:
            raise AnalyticsError("budget_analytics_limit", 413)
        return token

    def _cursor_index(self, cursor, context, digest, now):
        if cursor is None:
            return 0
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= _MAX_CURSOR:
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            payload = json.loads(self._cipher.decrypt(raw[:12], raw[12:], _cursor_aad(context)))
            if (not isinstance(payload, dict) or set(payload) != {"digest", "index", "issued", "expires"}
                    or type(payload["index"]) is not int or payload["index"] < 0
                    or not isinstance(payload["digest"], str)):
                raise ValueError
            issued, expires = payload["issued"], payload["expires"]
            if (type(issued) not in (int, float) or type(expires) not in (int, float)
                    or not math.isfinite(issued) or not math.isfinite(expires)
                    or not issued <= now < expires <= issued + _CURSOR_TTL):
                raise ValueError
        except (ValueError, TypeError, InvalidTag, RecursionError, OverflowError):
            raise AnalyticsError() from None
        if not hmac.compare_digest(payload["digest"], digest):
            raise AnalyticsError("budget_analytics_stale_cursor", 409)
        return payload["index"]


def _encode(payload):
    from .analytics_api import _encode_bounded
    return _encode_bounded(payload, MAX_RESPONSE_BYTES)


def _normalize_query(operation, query):
    allowed = _QUERY.get(operation)
    if allowed is None or not isinstance(query, dict):
        raise AnalyticsError()
    if any(key not in allowed for key in query):
        raise AnalyticsError()
    values = dict(query)
    if not _REQUIRED_QUERY.get(operation, frozenset()) <= values.keys():
        raise AnalyticsError()
    if "period" in values and operation == "monthly-budget":
        try:
            values["period"], values["currency"] = month_budget_mod.validate_monthly_budget_query(
                values.get("period"), values.get("currency"))
        except month_budget_mod.MonthInputError:
            raise AnalyticsError() from None
    elif "period" in values and operation == "spending-limits":
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", values["period"], re.ASCII):
            raise AnalyticsError()
        core.parse_period(values["period"])
        values["currency"] = _currency(values.get("currency"))
    elif "period" in values:
        core.parse_period(values["period"])
        values["currency"] = _currency(values.get("currency"))
    elif operation == "monthly-budget":
        try:
            values["period"], values["currency"] = month_budget_mod.validate_monthly_budget_query(
                None, values.get("currency"))
        except month_budget_mod.MonthInputError:
            raise AnalyticsError() from None
    elif "currency" in values or operation == "recurring":
        values["currency"] = _currency(values.get("currency"))
    if "limit" in values:
        raw = values["limit"]
        if isinstance(raw, str):
            if not re.fullmatch(r"[0-9]{1,3}", raw, re.ASCII):
                raise AnalyticsError()
            raw = int(raw)
        if type(raw) is not int or not 1 <= raw <= 200:
            raise AnalyticsError()
        values["limit"] = raw
    elif operation in ("transactions", "transfers"):
        values["limit"] = 50
    if operation == "transactions":
        kind = values.get("kind")
        if kind is not None and kind not in _KINDS:
            raise AnalyticsError()
        values["kind"] = kind
        values.setdefault("account_id", None)
        values.setdefault("category_id", None)
        values.setdefault("cursor", None)
        for key in ("account_id", "category_id", "cursor"):
            if values[key] is not None and (not isinstance(values[key], str) or not values[key]
                                            or len(values[key]) > (8192 if key == "cursor" else 128)):
                raise AnalyticsError()
    if operation == "transfers":
        status = values.get("status", "suggested")
        if status not in _TRANSFER_STATUSES:
            raise AnalyticsError()
        values["status"] = status
        values.setdefault("cursor", None)
        if values["cursor"] is not None and (not isinstance(values["cursor"], str) or not values["cursor"]
                                              or len(values["cursor"]) > 8192):
            raise AnalyticsError()
    return values


def _report_accounts(book, grant_id):
    rows = [{"account_id": book.account(identifier), "ordinal": ordinal}
            for identifier, ordinal in sorted(book.account_ordinal.items(), key=lambda item: item[1])]
    return {"grant_id": grant_id, "accounts": rows}


def _categories(book, grant_id):
    rows = [{"id": book.category(identifier), "label": book.label(identifier),
             "display_name": book.display_name(identifier),
             "archived": bool(book.category_meta[identifier]["archived"])}
            for identifier in sorted(book.category_ordinal, key=book.category_ordinal.get)]
    return {"grant_id": grant_id, "categories": rows}


def _rules(session, owner, book, grant_id):
    rule = db.BudgetRule
    rows = session.execute(select(
        rule.id, rule.field, rule.operator, rule.category_id, rule.kind, rule.priority, rule.enabled,
    ).where(rule.owner == owner).order_by(rule.priority, rule.id))
    safe = []
    for identifier, field, operator, category_id, kind, priority, enabled in rows:
        # Account rules carry a masked number and a key digest. Omit the whole row.
        if field == "counterparty_account":
            continue
        if field not in _RULE_FIELDS or operator not in _RULE_OPERATORS or kind not in core.KINDS:
            raise AnalyticsError("budget_analytics_data_error", 500)
        if type(priority) is not int or type(enabled) is not bool:
            # SQLite may surface the stored boolean as 0 or 1. Coerce only that pair.
            if type(enabled) is int and enabled in (0, 1) and type(priority) is int:
                enabled = bool(enabled)
            else:
                raise AnalyticsError("budget_analytics_data_error", 500)
        safe.append({
            "id": book.alias("rule", identifier),
            "field": field,
            "operator": operator,
            "category_id": book.category(category_id),
            "kind": kind,
            "priority": priority,
            "enabled": enabled,
        })
    return {"grant_id": grant_id, "rules": safe}


def _quarantine(session, owner, grant_id):
    account_ids = list(session.scalars(select(db.BudgetAccount.id).join(db.BudgetConnection).where(
        db.BudgetAccount.owner == owner, db.BudgetConnection.owner == owner, db.BudgetAccount.enabled)))
    analytics_views.BudgetAnalyticsViews._check_quarantine_integrity(session, account_ids)
    uncertain, unresolved, imported = analytics_views.BudgetAnalyticsViews._quarantine_counts(session, account_ids)
    return {
        "grant_id": grant_id,
        "uncertain_count": int(sum(uncertain.values())),
        "unresolved_count": int(sum(unresolved.values())),
        "import_unresolved_count": int(sum(imported.values())),
    }


def _settings(session, owner, book, grant_id):
    del session
    configs = month_budget_mod.load_month_config()
    settings = None if configs is None else configs.get(owner)
    if settings is None:
        return {"grant_id": grant_id, "income_category_ids": [], "pass_through": None}
    try:
        income = [book.category(identifier) for identifier in settings.income_category_ids]
        pair = settings.pass_through
        pass_through = None if pair is None else {
            "rent_category_id": book.category(pair.rent_category_id),
            "mortgage_category_id": book.category(pair.mortgage_category_id),
        }
    except AnalyticsError:
        raise BudgetConfigError("Invalid monthly budget configuration") from None
    return {"grant_id": grant_id, "income_category_ids": income, "pass_through": pass_through}


def _overview(snapshot, values, book, grant_id):
    start, end = core.parse_period(values["period"])
    rows = _period_rows(snapshot, start, end, values["currency"])
    months = {}
    for row in rows:
        months.setdefault(core.effective_date(row["payload"])[:7], []).append(row)
    categories = _category_rows(rows, book)
    return {
        "grant_id": grant_id,
        "period": values["period"],
        "currencies": [_overview_money(row) for row in _totals(rows)],
        "months": [{"month": month, "currencies": [_overview_money(row) for row in _totals(grouped)]}
                   for month, grouped in sorted(months.items())],
        "categories": categories,
    }


def _overview_money(row):
    return {"currency": row["currency"], **{field: row.get(field, "0") for field in _OVERVIEW_MONEY}}


def _category_rows(rows, book):
    totals = _category_totals(rows, [{"id": identifier, "name": meta["name"], "archived": meta["archived"]}
                                     for identifier, meta in book.category_meta.items()])
    income = {}
    for row in rows:
        if row["effective"]["kind"] != "income":
            continue
        key = (row["effective"]["category_id"], row["payload"]["currency"])
        income.setdefault(key, []).append(row["payload"]["amount"])
    seen = {(row["category_id"], row["currency"]) for row in totals}
    safe = []
    for row in totals:
        key = (row["category_id"], row["currency"])
        safe.append({
            "category_id": book.category(row["category_id"]),
            "label": book.label(row["category_id"]) if row["category_id"] is not None else None,
            "display_name": book.display_name(row["category_id"]),
            "archived": False if row["category_id"] is None else bool(book.category_meta[row["category_id"]]["archived"]),
            "currency": row["currency"],
            "expenses": row["expenses"],
            "refunds": row["refunds"],
            "income": core.sum_money(income.get(key, ())),
            "net_spending": row["net_spending"],
        })
    for key, amounts in sorted(income.items(), key=lambda item: ((item[0][0] or ""), item[0][1])):
        if key in seen:
            continue
        identifier, currency = key
        safe.append({
            "category_id": book.category(identifier),
            "label": book.label(identifier) if identifier is not None else None,
            "display_name": book.display_name(identifier),
            "archived": False if identifier is None else bool(book.category_meta[identifier]["archived"]),
            "currency": currency,
            "expenses": "0",
            "refunds": "0",
            "income": core.sum_money(amounts),
            "net_spending": "0",
        })
    return safe


def _safe_transaction(row, book):
    payload = row["payload"]
    kind = row["effective"]["kind"]
    direction = payload.get("credit_debit_indicator")
    if kind not in _KINDS or direction not in ("DBIT", "CRDT"):
        raise AnalyticsError("budget_analytics_data_error", 500)
    return {
        "id": book.alias("transaction", row["account_id"] + "\0" + row["entry_reference"]),
        "date": core.effective_date(payload),
        "account_id": book.account(row["account_id"]),
        "category_id": book.category(row["effective"]["category_id"]),
        "kind": kind,
        "currency": payload["currency"],
        "amount": core.canonical_money(payload["amount"]),
        "direction": direction,
    }


def _safe_leg(leg, book):
    if not leg.get("selected") or leg.get("date") is None or leg.get("amount") is None or leg.get("currency") is None:
        return None
    if leg["account_id"] not in book.account_ordinal:
        return None
    return {
        "account_id": book.account(leg["account_id"]),
        "date": leg["date"],
        "amount": core.canonical_money(leg["amount"]),
        "currency": leg["currency"],
    }


def _recurring(snapshot, values, grant_id):
    hints = _snapshot_recurring(snapshot)
    currency = values.get("currency")
    if currency is not None:
        hints = tuple(item for item in hints if item.currency == currency)
    ordered = sorted(hints, key=lambda hint: (
        hint.currency, hint.cadence, hint.amount, hint.first_date, hint.last_date, hint.occurrence_count,
        tuple(item.date for item in hint.evidence),
    ))
    page = ordered[:MAX_RECURRING_HINTS]
    return {"grant_id": grant_id, "hints": [
        {"ordinal": index, "amount": hint.amount, "currency": hint.currency, "cadence": hint.cadence,
         "first_date": hint.first_date, "last_date": hint.last_date, "occurrence_count": hint.occurrence_count}
        for index, hint in enumerate(page, 1)
    ]}


def _limits(session, snapshot, owner, values, book, grant_id):
    start, end = core.parse_period(values["period"])
    rows = [_limit_dict(row) for row in session.scalars(
        select(db.BudgetCategoryLimit).where(db.BudgetCategoryLimit.owner == owner,
                                              db.BudgetCategoryLimit.enabled.is_(True))
        .order_by(db.BudgetCategoryLimit.currency, db.BudgetCategoryLimit.category_id, db.BudgetCategoryLimit.id))]
    if values["currency"] is not None:
        rows = [row for row in rows if row["currency"] == values["currency"]]
    categories = {row["id"]: row for row in snapshot["categories"]}
    if any(row["category_id"] not in categories for row in rows):
        raise AnalyticsError("budget_analytics_data_error", 500)
    totals = _category_totals(_period_rows(snapshot, start, end, values["currency"]), snapshot["categories"])
    statuses = _spending_limit_statuses(owner, values["period"], rows, totals, categories)
    return {"grant_id": grant_id, "limits": [
        {"category_id": book.category(item["category_id"]), "currency": item["currency"],
         "monthly_limit": item["monthly_limit"], "warning_percent": item["warning_percent"],
         "status": item["status"], "net_spending": item["net_spending"]}
        for item in statuses
    ]}


def _limit_dict(row):
    from .analytics_store import _CATEGORY_LIMIT_FIELDS, _dict
    return _dict(row, _CATEGORY_LIMIT_FIELDS)


def _sanitize_budget(budget, book):
    safe = {key: budget[key] for key in _BUDGET_KEYS}
    # Row keys are owner account ids. The spouse keeps the counts and the provisional flag.
    coverage = dict(budget["coverage"])
    coverage["pending_row_keys"] = []
    safe["coverage"] = coverage
    safe["category_spend"] = [
        _sanitize_spend(row, book) for row in _folded_category_rows(budget["category_spend"], book)]
    expected = budget["expected_income"]
    if expected is not None:
        safe["expected_income"] = {**expected, "items": [
            {**item, "category_id": book.category(item["category_id"])} for item in expected["items"]
        ]}
    pair = budget["pass_through"]
    if pair is not None:
        safe["pass_through"] = {
            **pair,
            "rent_category_id": book.category(pair["rent_category_id"]),
            "mortgage_category_id": book.category(pair["mortgage_category_id"]),
        }
    return safe


def _sanitize_spend(row, book):
    identifier = row["category_id"]
    return {
        "category_id": book.category(identifier),
        # Category code or ordinal. A one-off label is owner-private and is not copied.
        "name": book.label(identifier) if identifier is not None else None,
        "display_name": book.display_name(identifier),
        "archived": row["archived"],
        "expenses": row["expenses"],
        "refunds": row["refunds"],
        "net_spending": row["net_spending"],
        "limit": row["limit"],
    }


def _month_span(period):
    months = []
    current = period
    for _ in range(_MONTHS):
        if current < "1900-01":
            break
        months.append(current)
        if current == "1900-01":
            break
        current = month_budget_mod._shift_period(current, -1)
    months.reverse()
    return months


def _month_bucket(payload, current_month, book):
    summary = payload["summary"]
    expected = payload["expected_income"]
    partial = bool(payload["period"] == current_month and expected is not None and expected["status"] == "partial")
    return {
        "period": payload["period"],
        "has_activity": bool(payload["coverage"]["has_activity"]),
        "is_current_month": payload["period"] == current_month,
        "income_estimate_partial": partial,
        "provisional": bool(payload["coverage"]["provisional"]),
        "start_budget": "0" if summary is None else summary["start_budget"],
        "spent": "0" if summary is None else summary["spent"],
        "categories": [
            {"category_id": book.category(row["category_id"]), "net_spending": row["net_spending"]}
            for row in _folded_category_rows(payload["category_spend"], book)
        ],
    }


def _folded_category_rows(rows, book):
    """Category totals ordered by the category alone, never by a private one-off label."""
    folded = _fold_category_spend(rows)
    folded.sort(key=lambda row: _category_sort_key(row, book))
    return folded


def _category_sort_key(row, book):
    identifier = row["category_id"]
    ordinal = book.category_ordinal.get(identifier)
    alias = book.category(identifier)
    # Amount descending, then the category ordinal and alias. None sorts last.
    return (-Decimal(row["net_spending"]), ordinal is None, ordinal or 0, alias or "")


def _fold_category_spend(rows):
    """Sum one-off splits into their category. The spouse sees category totals, not private labels."""
    folded = []
    index = {}
    for row in rows:
        key = row["category_id"]
        slot = index.get(key)
        if slot is None:
            slot = {
                "category_id": key,
                "archived": row["archived"],
                "expenses": row["expenses"],
                "refunds": row["refunds"],
                "net_spending": row["net_spending"],
                "limit": None if row.get("one_off") else row.get("limit"),
            }
            index[key] = slot
            folded.append(slot)
            continue
        slot["expenses"] = core.sum_money((slot["expenses"], row["expenses"]))
        slot["refunds"] = core.sum_money((slot["refunds"], row["refunds"]))
        slot["net_spending"] = core.sum_money((slot["net_spending"], row["net_spending"]))
        if not row.get("one_off"):
            slot["archived"] = row["archived"]
            slot["limit"] = row.get("limit")
    return folded


def _cursor_aad(context):
    """Bind cursor context as additional data. It does not change the ciphertext length."""
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return _CURSOR_DOMAIN + b"\0" + encoded


def _cursor_context(member, owner, grant_id, route, filters):
    return {"actor": member, "owner": owner, "grant_id": grant_id, "route": route, "filters": filters}


def _safe_digest(rows):
    return core.projection_digest(rows)
