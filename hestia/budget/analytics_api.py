"""Owner-private local budget reports and reviewed classification mutations."""
from __future__ import annotations

import asyncio
import os
import re

from aiohttp import web

from .. import api_contract, store_sql
from ..db import init_db
from . import api
from .analytics_core import AnalyticsError
from .analytics_views import BudgetAnalyticsViews
from .views import BudgetUsersBackendRequired

PREFIX = "/api/budget/analytics"
FLAG = "HESTIA_BUDGET_ANALYTICS_ENABLED"
READS = ("overview", "transactions", "categories", "rules", "transfers", "recurring", "spending-limits",
         "mortgages", "inbox", "quarantine")
WRITES = {
    "category-create": (api_contract.BudgetAnalyticsCategoryCreateRequest, "create_category", "category"),
    "category-update": (api_contract.BudgetAnalyticsCategoryUpdateRequest, "update_category", "category"),
    "annotation": (api_contract.BudgetAnalyticsAnnotationRequest, "annotate", "annotation"),
    "rule-preview": (api_contract.BudgetAnalyticsRulePreviewRequest, "preview_rule", "preview"),
    "rule-save": (api_contract.BudgetAnalyticsRuleSaveRequest, "save_rule", "rule"),
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
                 "quarantine": {"account_id"}}


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
    if "period" in _QUERY_FIELDS.get(operation, ()) and "period" not in values:
        raise AnalyticsError()
    if "limit" in values:
        value = values["limit"]
        if not re.fullmatch(r"[0-9]{1,3}", value, re.ASCII) or not 1 <= int(value) <= 200:
            raise AnalyticsError()
        values["limit"] = int(value)
    return values


def _store(secret):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return BudgetAnalyticsViews(sessions, cursor_secret=secret)


def _read(owner, operation, query, secret):
    result = getattr(_store(secret), operation.replace("-", "_"))(owner, **query)
    return {operation: result} if operation in ("categories", "rules") else result


def _write(owner, operation, values, secret):
    store = _store(secret)
    _, method, response_key = WRITES[operation]
    if operation == "category-create":
        result = store.create_category(owner, values["name"])
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
    else:
        result = getattr(store, method)(owner, **values)
    return {"ok": True, response_key: result}


def register(app, read_json_body, *, auth_enabled, session_secret):
    async def handle(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_analytics_disabled")
            if request.method == "GET":
                query = _query(request, operation)
                result = await asyncio.to_thread(_read, owner, operation, query, secret)
            else:
                api._browser(request, owner, secret)
                max_bytes = 65536 if operation == "inbox-mark" else 16384
                body, error = await read_json_body(request, max_bytes, reject_duplicates=True)
                if error:
                    return body
                values = WRITES[operation][0].model_validate(body).model_dump()
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
