"""Default-off, browser-reviewed owner receipt routes."""
from __future__ import annotations

import asyncio
import os
import time

from aiohttp import web
from pydantic import ValidationError

from .. import api_contract, store_sql
from ..db import init_db
from . import api
from .receipt_parser import ReceiptError
from .receipt_store import BudgetReceiptStore
from .views import BudgetUsersBackendRequired

PREFIX = "/api/budget/receipts"
FLAG = "HESTIA_BUDGET_RECEIPTS_ENABLED"
READS = ("detail", "image")
WRITES = {
    "parse-text": (api_contract.BudgetReceiptParseTextRequest, 49152),
    "confirm": (api_contract.BudgetReceiptConfirmRequest, 131072),
    "list": (api_contract.BudgetReceiptListRequest, 16384),
    "search": (api_contract.BudgetReceiptSearchRequest, 16384),
    "delete": (api_contract.BudgetReceiptDeleteRequest, 16384),
}


def enabled():
    return os.environ.get(FLAG) == "1"


def _store(secret):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return BudgetReceiptStore(sessions, clock=time.time, cursor_secret=secret)


def _receipt_id(request):
    if set(request.query) != {"receipt_id"} or len(request.query.getall("receipt_id")) != 1:
        raise ReceiptError()
    return request.query["receipt_id"]


def _run(owner, operation, values, secret):
    store = _store(secret)
    if operation == "parse-text":
        return store.parse_text(owner, values["text"])
    if operation == "confirm":
        return store.confirm(owner, values)
    if operation == "delete":
        return store.delete(owner, values)
    return getattr(store, operation)(owner, values)


def register(app, read_json_body, *, auth_enabled, session_secret):
    async def handle(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if request.method == "GET":
                receipt_id = _receipt_id(request)
                result = await api._settled_thread(lambda: _run_read(owner, operation, receipt_id, secret))
                return web.json_response(result, headers=api.PRIVATE_HEADERS)
            api._browser(request, owner, secret)
            body, error = await read_json_body(request, WRITES[operation][1], reject_duplicates=True)
            if error:
                return body
            values = WRITES[operation][0].model_validate(body).model_dump()
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            api._browser(request, owner, secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            result = await api._settled_thread(lambda: _run(owner, operation, values, secret))
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except ReceiptError as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_receipt_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_" + operation)

    async def retention(_app):
        if not enabled():
            yield
            return

        async def sweep():
            while True:
                try:
                    secret = api._secret(session_secret)
                    await api._settled_thread(lambda: _store(secret).purge_expired())
                except (Exception, asyncio.CancelledError):
                    if asyncio.current_task().cancelling():
                        raise
                await asyncio.sleep(3600)

        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app.cleanup_ctx.append(retention)
    for operation in READS:
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
    for operation in WRITES:
        app.router.add_post(PREFIX + "/" + operation, handle)


def _run_read(owner, operation, receipt_id, secret):
    store = _store(secret)
    if operation == "detail":
        return store.detail(owner, receipt_id)
    store.image(owner, receipt_id)
    raise ReceiptError("budget_receipt_not_found", 404)
