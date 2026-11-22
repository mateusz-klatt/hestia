"""Owner-private local budget reports and reviewed classification mutations."""
from __future__ import annotations

import asyncio
import json
import os
import re

from aiohttp import web

from .. import api_contract, store_sql
from ..db import init_db
from . import api
from .analytics_core import AnalyticsError
from .analytics_views import (BudgetAnalyticsViews, _forecast_query, _monthly_budget_charts_query,
                              _monthly_budget_query)
from .views import BudgetUsersBackendRequired

PREFIX = "/api/budget/analytics"
FLAG = "HESTIA_BUDGET_ANALYTICS_ENABLED"
MAX_FORECAST_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_MONTHLY_BUDGET_RESPONSE_BYTES = 4 * 1024 * 1024
_MONTHLY_BUDGET_OP = "monthly-budget"
_MONTHLY_BUDGET_CHARTS_OP = "monthly-budget/charts"
READS = ("overview", "transactions", "categories", "rules", "transfers", "recurring", "spending-limits",
         "mortgages", "inbox", "quarantine", "forecast", _MONTHLY_BUDGET_OP, _MONTHLY_BUDGET_CHARTS_OP)
WRITES = {
    "category-create": (api_contract.BudgetAnalyticsCategoryCreateRequest, "create_category", "category"),
    "category-update": (api_contract.BudgetAnalyticsCategoryUpdateRequest, "update_category", "category"),
    "annotation": (api_contract.BudgetAnalyticsAnnotationRequest, "annotate", "annotation"),
    "rule-preview": (api_contract.BudgetAnalyticsRulePreviewRequest, "preview_rule", "preview"),
    "rule-save": (api_contract.BudgetAnalyticsRuleSaveRequest, "save_rule", "rule"),
    "rule-batch-preview": (api_contract.BudgetAnalyticsRuleBatchPreviewRequest, "preview_rules", "preview"),
    "rule-batch-save": (api_contract.BudgetAnalyticsRuleBatchSaveRequest, "save_rules", "batch"),
    "transfer-decision": (api_contract.BudgetAnalyticsTransferDecisionRequest, "decide_transfer", "decision"),
    "spending-limit-save": (api_contract.BudgetAnalyticsSpendingLimitSaveRequest, "save_spending_limit", "limit"),
    "mortgage-save": (api_contract.BudgetAnalyticsMortgageSaveRequest, "save_mortgage_schedule", "schedule"),
    "mortgage-payment-set": (api_contract.BudgetAnalyticsMortgagePaymentSetRequest, "set_mortgage_payment", "payment"),
    "inbox-mark": (api_contract.BudgetAnalyticsInboxMarkRequest, "mark_inbox", "inbox"),
}
_QUERY_FIELDS = {"overview": {"period", "currency"},
                 "transactions": {"period", "currency", "category_id", "kind", "query", "limit", "cursor"},
                 "transfers": {"period", "currency", "status", "limit", "cursor"},
                 "recurring": {"currency"},
                 "spending-limits": {"period", "currency"},
                 "quarantine": {"account_id"},
                 "forecast": {"months", "currency"},
                 _MONTHLY_BUDGET_OP: {"period", "currency"},
                 _MONTHLY_BUDGET_CHARTS_OP: {"period", "currency", "history"}}
_OPTIONAL_PERIOD_READS = frozenset({_MONTHLY_BUDGET_OP, _MONTHLY_BUDGET_CHARTS_OP})
_BOUNDED_READ_CAPS = {
    "forecast": lambda: MAX_FORECAST_RESPONSE_BYTES,
    _MONTHLY_BUDGET_OP: lambda: MAX_MONTHLY_BUDGET_RESPONSE_BYTES,
    _MONTHLY_BUDGET_CHARTS_OP: lambda: MAX_MONTHLY_BUDGET_RESPONSE_BYTES,
}


def enabled():
    return os.environ.get(FLAG) == "1"


def _query(request, operation):
    values = {}
    for key in request.query:
        if key not in _QUERY_FIELDS.get(operation, ()) or len(request.query.getall(key)) != 1:
            raise AnalyticsError()
        value = request.query[key]
        maximum = 8192 if key == "cursor" else 256 if key == "query" else 128
        if not value or len(value) > maximum:
            raise AnalyticsError()
        values[key] = value
    if ("period" in _QUERY_FIELDS.get(operation, ()) and operation not in _OPTIONAL_PERIOD_READS
            and "period" not in values):
        raise AnalyticsError()
    if "limit" in values:
        value = values["limit"]
        if not re.fullmatch(r"[0-9]{1,3}", value, re.ASCII) or not 1 <= int(value) <= 200:
            raise AnalyticsError()
        values["limit"] = int(value)
    if operation == "forecast":
        _forecast_query(**values)
    elif operation == _MONTHLY_BUDGET_OP:
        _monthly_budget_query(**values)
    elif operation == _MONTHLY_BUDGET_CHARTS_OP:
        _monthly_budget_charts_query(**values)
    return values


def _store(secret):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return BudgetAnalyticsViews(sessions, cursor_secret=secret)


def _encode_bounded(payload, limit):
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    body = bytearray()
    try:
        for chunk in encoder.iterencode(payload):
            encoded = chunk.encode("utf-8")
            if len(body) + len(encoded) > limit:
                raise AnalyticsError("budget_analytics_limit", 413)
            body.extend(encoded)
    except UnicodeEncodeError:
        raise AnalyticsError("budget_analytics_data_error", 500) from None
    return body


def _read(owner, operation, query, secret):
    result = getattr(_store(secret), operation.replace("-", "_").replace("/", "_"))(owner, **query)
    if operation in _BOUNDED_READ_CAPS:
        return _encode_bounded(result, _BOUNDED_READ_CAPS[operation]())
    return {operation: result} if operation in ("categories", "rules") else result


def _write(owner, operation, values, secret):
    store = _store(secret)
    _, method, response_key = WRITES[operation]
    if operation == "category-create":
        result = store.create_category(owner, values["name"], parent_id=values.get("parent_id"))
    elif operation == "category-update":
        identifier = values.pop("category_id")
        result = store.update_category(owner, identifier, **values)
    elif operation == "annotation":
        account, reference = values.pop("account_id"), values.pop("entry_reference")
        result = store.annotate(owner, account, reference, **values)
    elif operation == "rule-preview":
        result = store.preview_rule(owner, values)
    elif operation == "rule-save":
        result = store.save_rule(owner, values["preview_id"])
    elif operation == "rule-batch-preview":
        result = store.preview_rules(owner, values["rules"])
    elif operation == "rule-batch-save":
        result = store.save_rules(owner, values["preview_id"])
    else:
        result = getattr(store, method)(owner, **values)
    return {"ok": True, response_key: result}


def register(app, read_json_body, *, auth_enabled, session_secret):
    async def handle(request):
        operation = request.path.removeprefix(PREFIX + "/")
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_analytics_disabled")
            if request.method == "GET":
                query = _query(request, operation)
                result = await asyncio.to_thread(_read, owner, operation, query, secret)
                if operation in _BOUNDED_READ_CAPS:
                    return web.Response(body=result, content_type="application/json", charset="utf-8",
                                        headers=api.PRIVATE_HEADERS)
            else:
                api._browser(request, owner, secret)
                max_bytes = 65536 if operation == "inbox-mark" else 262144 if operation == "rule-batch-preview" else 16384
                body, error = await read_json_body(request, max_bytes, reject_duplicates=True)
                if error:
                    return body
                parsed = WRITES[operation][0].model_validate(body)
                # An omitted parent_id on update leaves the parent; null detaches. Other writes keep defaults.
                values = parsed.model_dump(exclude_unset=operation == "category-update")
                owner = api._principal(request, auth_enabled)
                secret = api._secret(session_secret)
                api._browser(request, owner, secret)
                if not enabled():
                    raise api._Rejected(404, "budget_analytics_disabled")
                result = await api._settled_thread(lambda: _write(owner, operation, values, secret))
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except AnalyticsError as exc:
            return api._error(exc.status, exc.code)
        except Exception as exc:
            return api._failure(exc, operation)

    for operation in READS:
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
    for operation in WRITES:
        app.router.add_post(PREFIX + "/" + operation, handle)
