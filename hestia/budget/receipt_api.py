"""Default-off, browser-reviewed owner receipt routes."""
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


def _store(secret):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return BudgetReceiptStore(sessions, clock=time.time, cursor_secret=secret)


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


def _run(owner, operation, values, secret, actor=None):
    store = _store(secret)
    if operation == "parse-text":
        return store.parse_text(owner, values["text"], source_kind=values.get("source_kind", "paste"), actor=actor)
    if operation == "confirm":
        return store.confirm(owner, values, actor=actor)
    if operation == "delete":
        return store.delete(owner, values, actor=actor)
    return getattr(store, operation)(owner, values)


def _device_error(status, code, retry=None):
    return device_token.json_error({"status": status, "code": code, "retry": retry})


def _bearer(request):
    header = request.headers.get("Authorization", "")
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return ""
    return header[len("Bearer "):]


_DEVICE_BODY = 4096


def register(app, read_json_body, *, auth_enabled, session_secret):
    async def handle(request):
        if request.get(device_token.REQUEST_KEY):
            return await device_receipt(request)
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

    async def device_receipt(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            found = await _identify(secret, _bearer(request))
            if found.get("status") != "ok" or found.get("owner") != owner:
                return device_token.status_response(found)
            token_id = found["token_id"]
            if operation == "parse-text":
                retry = device_token.parse_limited(token_id, device_token.now())
                if retry is not None:
                    return _device_error(429, "device_token_rate_limited", retry)
            if operation == "detail":
                receipt_id = _receipt_id(request)
                result = await api._settled_thread(lambda: _run_read(owner, operation, receipt_id, secret))
                return web.json_response(result, headers=api.PRIVATE_HEADERS)
            body, error = await read_json_body(request, WRITES[operation][1], reject_duplicates=True)
            if error:
                return body
            values = WRITES[operation][0].model_validate(body).model_dump()
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            found = await _identify(secret, _bearer(request))
            if found.get("status") != "ok" or found.get("owner") != owner:
                return device_token.status_response(found)
            actor = "device:" + found["token_id"]
            result = await api._settled_thread(lambda: _run(owner, operation, values, secret, actor=actor))
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except ReceiptError as exc:
            return api._error(exc.status, exc.code)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_receipt_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_" + operation)

    async def redeem(request):
        try:
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            if not auth_enabled():
                raise api._Rejected(401, "budget_not_signed_in")
            if "hestia_session" in request.cookies or "Authorization" in request.headers:
                return _device_error(400, "device_token_rejected")
            retry = device_token.redeem_limited(device_token.client_ip(request), device_token.now())
            if retry is not None:
                return _device_error(429, "device_token_rejected", retry)
            secret = api._secret(session_secret)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error:
                if getattr(body, "status", None) == 400:
                    return _device_error(400, "device_token_rejected")
                return body
            if not isinstance(body, dict) or set(body) != {"code", "device_name"}:
                return _device_error(400, "device_token_rejected")
            result = await api._settled_thread(
                lambda: device_token.open_store(secret).redeem(body.get("code"), body.get("device_name")))
            if result is None:
                return _device_error(400, "device_token_rejected")
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except Exception as exc:
            return api._failure(exc, "receipts_redeem")

    async def browser_device(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            if request.get(device_token.REQUEST_KEY):
                if operation != "revoke":
                    return api._error(403, "budget_forbidden")
                return await device_revoke(request)
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            api._browser(request, owner, secret)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error:
                return body
            model = (api_contract.BudgetReceiptDeviceRevokeRequest if operation == "revoke"
                     else api_contract.BudgetReceiptDeviceBrowserRequest)
            values = model.model_validate(body).model_dump()
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            api._browser(request, owner, secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            store = device_token.open_store(secret)
            if operation == "pair-code":
                result = await api._settled_thread(lambda: store.issue_code(owner))
            elif operation == "list":
                result = await api._settled_thread(lambda: store.list_tokens(owner))
            else:
                result = await api._settled_thread(lambda: store.revoke(owner, values["token_id"]))
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except ValidationError:
            return api._error(400, "budget_invalid_request")
        except Exception as exc:
            return api._failure(exc, "receipts_device_" + operation)

    async def device_revoke(request):
        try:
            owner = api._principal(request, auth_enabled)
            secret = api._secret(session_secret)
            if not enabled():
                raise api._Rejected(404, "budget_receipts_disabled")
            actor = request.get(device_token.REQUEST_KEY)
            body, error = await read_json_body(request, _DEVICE_BODY, reject_duplicates=True)
            if error:
                if getattr(body, "status", None) in (411, 413, 415, 408):
                    return body
                return api._error(403, "budget_forbidden")
            token_id = body.get("token_id") if isinstance(body, dict) and set(body) == {"token_id"} else None
            if not device_token.ids_match(actor, token_id):
                return api._error(403, "budget_forbidden")
            found = await _identify(secret, _bearer(request))
            if found.get("status") != "ok" or found.get("token_id") != actor or found.get("owner") != owner:
                return device_token.status_response(found)
            result = await api._settled_thread(
                lambda: device_token.open_store(secret).revoke(owner, token_id, actor_token_id=actor))
            return web.json_response(result, headers=api.PRIVATE_HEADERS)
        except device_token.DeviceTokenError as exc:
            return api._error(exc.status, exc.code)
        except Exception as exc:
            return api._failure(exc, "receipts_device_revoke")

    async def _identify(secret, token):
        return await api._settled_thread(
            lambda: device_token.open_store(secret).identify(token, slide=True))

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


def _run_read(owner, operation, receipt_id, secret):
    store = _store(secret)
    if operation == "detail":
        return store.detail(owner, receipt_id)
    store.image(owner, receipt_id)
    raise ReceiptError("budget_receipt_not_found", 404)
