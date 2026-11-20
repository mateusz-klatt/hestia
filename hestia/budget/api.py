"""Default-off, owner-private stored banking data and browser-only controls."""
from __future__ import annotations

import asyncio
from datetime import date
import logging
import os
import re
import time
from urllib.parse import urlsplit

from aiohttp import web
from pydantic import ValidationError

from .. import auth, store_sql
from ..api_contract import BudgetConnectionStateRequest, BudgetKindRequest, BudgetSelectionRequest
from ..db import init_db
from .config import BudgetConfigError
from .store import BudgetStoreError
from .views import (BudgetAccessDenied, BudgetControlConflict, BudgetHTTPStore, BudgetNotFound,
                    BudgetQueryError, BudgetUsersBackendRequired)


PREFIX = "/api/budget"
READS = ("connections", "accounts", "status", "transactions", "balances", "limits")
MUTATIONS = {
    "account-selection": (BudgetSelectionRequest, "set_account_selected", "account", "account_id", "selected"),
    "account-kind": (BudgetKindRequest, "confirm_account_kind", "account", "account_id", "kind"),
    "connection-state": (BudgetConnectionStateRequest, "set_connection_paused", "connection", "connection_id", "paused"),
}
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
log = logging.getLogger("hestia.budget.api")
_QUERY_FIELDS = {"accounts": {"connection_id"}, "balances": {"account_id"},
                 "transactions": {"account_id", "date_from", "date_to", "limit", "cursor"}}


class _Rejected(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def _error(status, code, **extra):
    return web.json_response({"ok": False, "error": code, **extra}, status=status, headers=PRIVATE_HEADERS)


def _principal(request, auth_enabled):
    if os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") != "1":
        raise _Rejected(404, "budget_disabled")
    user = request.get("hestia_user")
    if not auth_enabled() or not isinstance(user, str) or not user:
        raise _Rejected(401, "budget_not_signed_in")
    if request.get("hestia_role") != "admin":
        raise _Rejected(403, "budget_forbidden")
    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        raise BudgetUsersBackendRequired()
    return user


def _secret(session_secret):
    secret = session_secret()
    if not isinstance(secret, bytes) or not secret:
        raise BudgetConfigError("Budget HTTP requires configured authentication")
    return secret


def _browser(request, user, secret):
    if "Authorization" in request.headers:
        raise _Rejected(403, "budget_browser_session_required")
    token = request.cookies.get("hestia_session", "")
    if auth.verify_session(token, now=time.time(), secret=secret) != user:
        raise _Rejected(401, "budget_not_signed_in")
    origin = os.environ.get("HESTIA_BUDGET_PUBLIC_ORIGIN", "")
    try:
        parsed = urlsplit(origin)
        if (not origin.isascii() or any(char.isspace() for char in origin) or "\\" in origin
                or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.path or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
    except ValueError:
        raise BudgetConfigError("Budget HTTP requires a fixed HTTPS public origin") from None
    if request.headers.getall("Origin", []) != [origin]:
        raise _Rejected(403, "budget_origin_required")


def _query(request, operation):
    allowed = _QUERY_FIELDS.get(operation, set())
    values = {}
    for key in request.query:
        if key not in allowed or len(request.query.getall(key)) != 1:
            raise BudgetQueryError()
        value = request.query[key]
        maximum = 8192 if key == "cursor" else 128
        if not value or len(value) > maximum:
            raise BudgetQueryError()
        values[key] = value
    if operation in ("transactions", "balances") and "account_id" not in values:
        raise BudgetQueryError()
    if "limit" in values:
        if not re.fullmatch(r"[0-9]{1,3}", values["limit"]) or not 1 <= int(values["limit"]) <= 200:
            raise BudgetQueryError()
        values["limit"] = int(values["limit"])
    for key in ("date_from", "date_to"):
        if key in values:
            try:
                if date.fromisoformat(values[key]).isoformat() != values[key]:
                    raise ValueError
            except ValueError:
                raise BudgetQueryError() from None
    return values


def _store(secret):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return BudgetHTTPStore(sessions, cursor_secret=secret)


def _read(owner, operation, query, secret):
    result = getattr(_store(secret), operation)(owner, **query)
    if operation == "connections":
        return {"connections": result, "capabilities": {"stored_reads": True, "account_controls": True,
                                                        "refresh": False, "background_selection": False, "consent": False}}
    return {operation: result} if operation in ("connections", "accounts", "status", "limits") else result


def _payload(operation, body):
    model = MUTATIONS[operation][0]
    missing_version = isinstance(body, dict) and "expected_version" not in body
    validated = model.model_validate({**body, "expected_version": 1} if missing_version else body)
    values = validated.model_dump()
    if missing_version:
        values["expected_version"] = None
    return values


def _write(owner, operation, values, secret):
    _, method, response_key, target, change = MUTATIONS[operation]
    result = getattr(_store(secret), method)(owner, values[target], values[change],
                                            expected_version=values["expected_version"], actor=owner, origin="web")
    return {"ok": True, response_key: result}


async def _settled_thread(operation):
    """Drain the executor result across repeated cancellations before propagating one."""
    worker = asyncio.get_running_loop().run_in_executor(None, operation)
    cancelled = None
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError as exc:  # NOSONAR S7497: re-raised after the database write settles.
            cancelled = exc
    error = worker.exception()
    if error is not None:
        raise cancelled or error
    if cancelled is not None:
        raise cancelled
    return worker.result()


def _failure(exc, operation):
    if isinstance(exc, _Rejected):
        return _error(exc.status, exc.code)
    if isinstance(exc, BudgetAccessDenied):
        return _error(exc.status, "budget_not_signed_in" if exc.status == 401 else "budget_forbidden")
    if isinstance(exc, BudgetControlConflict):
        return _error(409, "budget_version_conflict", current=exc.current)
    if isinstance(exc, BudgetUsersBackendRequired):
        return _error(409, "budget_requires_sqlite_users")
    if isinstance(exc, BudgetNotFound):
        return _error(404, "budget_not_found")
    if isinstance(exc, (BudgetStoreError, ValidationError)):
        return _error(400, "budget_invalid_request")
    if isinstance(exc, BudgetConfigError):
        return _error(503, "budget_unavailable")
    log.error("Budget API operation=%s exception=%s", operation, type(exc).__name__)
    return _error(500, "budget_internal_error")


def register(app, read_json_body, *, auth_enabled, session_secret):
    async def private_headers(request, response):
        if request.path == PREFIX or request.path.startswith(PREFIX + "/"):
            response.headers.update(PRIVATE_HEADERS)

    async def handle(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            owner = _principal(request, auth_enabled)
            secret = _secret(session_secret)
            if request.method == "GET":
                query = _query(request, operation)
                result = await asyncio.to_thread(_read, owner, operation, query, secret)
            else:
                _browser(request, owner, secret)
                body, error = await read_json_body(request, 16384, reject_duplicates=True)
                if error:
                    return body
                values = _payload(operation, body)
                owner = _principal(request, auth_enabled)
                secret = _secret(session_secret)
                _browser(request, owner, secret)
                result = await _settled_thread(lambda: _write(owner, operation, values, secret))
            return web.json_response(result, headers=PRIVATE_HEADERS)
        except Exception as exc:
            return _failure(exc, operation)

    app.on_response_prepare.append(private_headers)
    for operation in READS:
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
    for operation in MUTATIONS:
        app.router.add_post(PREFIX + "/" + operation, handle)
