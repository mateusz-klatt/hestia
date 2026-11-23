"""Default-off owner receipts and device pairing routes."""
from __future__ import annotations

import asyncio
import os
import time

from aiohttp import web
from pydantic import ValidationError

from .. import api_contract, store_sql
from ..db import init_db
from . import api, device_token
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


_RETENTION_SECRET = b"hestia/budget/receipts/retention/v1"


def _store(secret, device_id=None):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return BudgetReceiptStore(sessions, clock=time.time, cursor_secret=secret, device_id=device_id)


def retain_expired(path=None, clock=time.time):
    """Purge expired drafts and raw text without the route flag or a browser secret.

    A constant non-session key satisfies the store constructor. The sweep never
    reads or writes a cursor, and it skips a database that is not the users backend.
    """
    engine, sessions = init_db(path)
    if not store_sql.is_users_db_authoritative(engine):
        return {"drafts_purged": 0, "raw_texts_purged": 0}
    return BudgetReceiptStore(sessions, clock=clock, cursor_secret=_RETENTION_SECRET).purge_expired()


def _receipt_id(request):
    if set(request.query) != {"receipt_id"} or len(request.query.getall("receipt_id")) != 1:
        raise ReceiptError()
    return request.query["receipt_id"]


def _run(owner, operation, values, secret, device_id):
    store = _store(secret, device_id)
    if operation == "parse-text":
        return store.parse_text(owner, values["text"], source_kind=values["source_kind"])
    return getattr(store, operation)(owner, values)


def _device_error(status, code, retry=None):
    return device_token.json_error({"status": status, "code": code, "retry": retry})


_DEVICE_BODY = 4096


def register(app, read_json_body, *, auth_enabled, session_secret):
    async def handle(request):
        device_id = request.get(device_token.REQUEST_KEY)
        operation = request.path.rsplit("/", 1)[-1]
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if request.method == "GET":
                receipt_id = _receipt_id(request)
                result = await api._settled_thread(lambda: _run_read(owner, operation, receipt_id, secret, device_id))
                return web.json_response(result, headers=api.PRIVATE_HEADERS)
            if device_id is None:
                api._browser(request, owner, secret)
            elif operation == "parse-text":
                retry = device_token.parse_limited(device_id, time.monotonic())
                if retry is not None:
                    return _device_error(429, "device_token_rate_limited", retry)
            body, error = await read_json_body(request, WRITES[operation][1], reject_duplicates=True)
            if error:
                return body
            values = WRITES[operation][0].model_validate(body).model_dump()
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if device_id is None:
                api._browser(request, owner, secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            result = await api._settled_thread(lambda: _run(owner, operation, values, secret, device_id))
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except (ReceiptError, device_token.DeviceTokenError) as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_receipt_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_" + operation)

    async def redeem(request):
        try:
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if not auth_enabled() or "Cookie" in request.headers or "Authorization" in request.headers:
                return _device_error(400, "device_token_rejected")
            retry = device_token.redeem_limited(device_token.client_ip(request), time.monotonic())
            if retry is not None:
                return _device_error(429, "device_token_rejected", retry)
            secret = api._secret(session_secret)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error or not isinstance(body, dict) or set(body) != {"code", "device_name"}:
                return _device_error(400, "device_token_rejected")
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            result = await api._settled_thread(
                lambda: device_token.open_store(secret).redeem(body["code"], body["device_name"]))
            if result is None:
                return _device_error(400, "device_token_rejected")
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except BudgetUsersBackendRequired:
            return _device_error(400, "device_token_rejected")
        except Exception as exc:
            return api._failure(exc, "receipts_redeem")

    async def browser_device(request):
        operation = request.path.rsplit("/", 1)[-1]
        device_id = request.get(device_token.REQUEST_KEY)
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if device_id is None:
                api._browser(request, owner, secret)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error:
                return body
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if device_id is not None:
                token_id = body.get("token_id") if isinstance(body, dict) and set(body) == {"token_id"} else None
            else:
                api._browser(request, owner, secret)
                model = (api_contract.BudgetReceiptDeviceRevokeRequest if operation == "revoke"
                         else api_contract.BudgetReceiptDeviceBrowserRequest)
                values = model.model_validate(body).model_dump()
                token_id = values.get("token_id")

            def run():
                store = device_token.open_store(secret)
                if operation == "pair-code":
                    return store.issue_code(owner)
                if operation == "list":
                    return store.list_tokens(owner)
                return store.revoke(owner, token_id, actor_token_id=device_id)

            result = await api._settled_thread(run)
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_device_" + operation)

    async def retention(_app):
        async def sweep():
            while True:
                try:
                    await api._settled_thread(retain_expired)
                except (Exception, asyncio.CancelledError):
                    if asyncio.current_task().cancelling():
                        raise
                await asyncio.sleep(3600)

        task = asyncio.create_task(sweep())
        yield
        task.cancel()
        # The sweep task ends with CancelledError. Waiting through gather does not
        # catch that exception here, so a cancellation of this cleanup still propagates.
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(retention)
    for operation in READS:
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
    for operation in WRITES:
        app.router.add_post(PREFIX + "/" + operation, handle)
    app.router.add_post(PREFIX + "/device-token/pair-code", browser_device)
    app.router.add_post(PREFIX + "/device-token/list", browser_device)
    app.router.add_post(PREFIX + "/device-token/revoke", browser_device)
    app.router.add_post(PREFIX + "/device-token/redeem", redeem)


def _run_read(owner, operation, receipt_id, secret, device_id=None):
    store = _store(secret, device_id)
    if operation == "detail":
        return store.detail(owner, receipt_id)
    store.image(owner, receipt_id)
    raise ReceiptError("budget_receipt_not_found", 404)
