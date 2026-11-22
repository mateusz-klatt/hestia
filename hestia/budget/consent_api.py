"""Browser-bound bank consent endpoints; the callback only stages a private code."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import logging
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

from aiohttp import web

from .. import auth, store_sql
from ..api_contract import BudgetConsentFlowRequest, BudgetConsentStartRequest
from ..db import db_path, init_db
from . import api
from .config import BudgetConfigError, load_config
from .consent_context import psu_context
from .consent_control import BudgetConsentControlStore
from .consent_files import ConsentFiles
from .consent_service import BudgetConsentService
from .consent_store import BudgetConsentError
from .runtime import budget_lock

PREFIX = "/api/budget/consent"
CALLBACK = "/api/budget/enable-banking/callback"
COMPLETE = "/budget/connect/complete"
SCRIPT = COMPLETE + ".js"
PUBLIC_GET = frozenset((CALLBACK, COMPLETE, SCRIPT))
COOKIE_PREFIX = "__Host-hestia_budget_"
FLAG = "HESTIA_BUDGET_CONSENT_ENABLED"
KEY = web.AppKey("budget_consent", object)
_NONCE = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
_STATIC = Path(__file__).with_name("static")
log = logging.getLogger("hestia.budget.consent_api")


@dataclass
class ConsentHolder:
    service: object = None


def _origin():
    origin = os.environ.get("HESTIA_BUDGET_PUBLIC_ORIGIN", "")
    try:
        parsed = urlsplit(origin)
        if (not origin.isascii() or any(c.isspace() for c in origin) or "\\" in origin
                or parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.path or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
    except ValueError:
        raise BudgetConfigError("Budget consent requires a fixed HTTPS public origin") from None
    return origin


def _fingerprint(token, secret):
    return hmac.new(secret, b"hestia-budget-consent-v1\0" + token.encode("utf-8"), hashlib.sha256).hexdigest()


def cookie_name(flow_id):
    try:
        if not isinstance(flow_id, str) or str(uuid.UUID(flow_id)) != flow_id:
            raise ValueError
    except ValueError:
        raise BudgetConsentError("budget_consent_invalid_request", 400) from None
    return COOKIE_PREFIX + flow_id


def _bindings(request):
    result = {}
    for name, value in request.cookies.items():
        if not name.startswith(COOKIE_PREFIX):
            continue
        flow_id = name[len(COOKIE_PREFIX):]
        try:
            cookie_name(flow_id)
        except BudgetConsentError:
            continue
        if _NONCE.fullmatch(value):
            result[flow_id] = value
        if len(result) > 16:
            raise BudgetConsentError("budget_consent_invalid_request", 400)
    return result


def _service(request):
    service = request.app[KEY].service
    if os.environ.get(FLAG) != "1" or service is None:
        raise api._Rejected(503, "budget_consent_unavailable")
    return service


def enabled(app):
    return os.environ.get(FLAG) == "1" and app[KEY].service is not None


def _browser(request, auth_enabled, session_secret, *, mutation):
    owner = api._principal(request, auth_enabled)
    secret = api._secret(session_secret)
    token = request.cookies.get("hestia_session", "")
    if "Authorization" in request.headers:
        raise api._Rejected(403, "budget_browser_session_required")
    if auth.verify_session(token, now=api.time.time(), secret=secret) != owner:
        raise api._Rejected(401, "budget_not_signed_in")
    if mutation:
        api._browser(request, owner, secret)
    return owner, _fingerprint(token, secret)


def _psu(request):
    return psu_context(request)


def create_service(*, auth_enabled, session_secret, secure_cookie):
    if (not auth_enabled() or not secure_cookie() or not api._secret(session_secret)
            or os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite"
            or os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") != "1"):
        raise BudgetConfigError("Budget consent prerequisites are unavailable")
    origin = _origin()
    config = load_config()
    if config is None:
        raise BudgetConfigError("Budget configuration is required")
    path = db_path()
    with budget_lock(path):
        engine, sessions = init_db(path)
        if not store_sql.is_users_db_authoritative(engine):
            raise BudgetConfigError("Budget consent requires authoritative SQLite users")
        store = BudgetConsentControlStore(sessions)
        store.configure(config)
        store.recover_interrupted()
    files = ConsentFiles(os.environ.get("HESTIA_BUDGET_CONSENT_DIR", "/data/budget-consent"))
    return BudgetConsentService(config, store, files, path=path, redirect_url=origin + CALLBACK,
                                opaque_secret=api._secret(session_secret))


async def auth_transition(request, response=None):
    """Fence old flows before a login/logout succeeds; default-off paths perform no I/O."""
    bindings = _bindings(request)
    token = request.cookies.get("hestia_session", "")
    service = request.app[KEY].service
    if token and bindings:
        fingerprint = _fingerprint(token, api._secret(lambda: request.app[_SECRET_KEY]()))
        if service is None:
            # A disabled or failed control-plane must not resurrect old flows after
            # a later restart. This fallback reads no configuration or secret file.
            def invalidate():
                if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
                    raise BudgetConfigError("Consent binding invalidation is unavailable")
                engine, sessions = init_db()
                if not store_sql.is_users_db_authoritative(engine):
                    raise BudgetConfigError("Consent binding invalidation is unavailable")
                hashes = {flow_id: hashlib.sha256(nonce.encode("ascii")).hexdigest()
                          for flow_id, nonce in bindings.items()}
                return BudgetConsentControlStore(sessions).invalidate_bindings(fingerprint, hashes)
            await api._settled_thread(invalidate)
        else:
            await api._settled_thread(lambda: service.invalidate_bindings(fingerprint, bindings))
    if response is not None:
        for flow_id in bindings:
            response.del_cookie(cookie_name(flow_id), path="/", secure=True, httponly=True, samesite="Lax")


_SECRET_KEY = web.AppKey("budget_consent_session_secret", object)


def _failure(exc, operation):
    if isinstance(exc, BudgetConsentError):
        return api._error(exc.status, exc.code)
    return api._failure(exc, "consent_" + operation)


def _query(request):
    if len(request.query_string) > 8192:
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    values = {}
    for name in request.query:
        if name not in {"state", "code", "error", "error_description"} or len(request.query.getall(name)) != 1:
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        value = request.query[name]
        maximum = 4096 if name == "code" else 1024
        if not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise BudgetConsentError("budget_consent_invalid_request", 400)
        values[name] = value
    if (not _NONCE.fullmatch(values.get("state", ""))
            or ("code" in values) == ("error" in values)
            or ("error_description" in values and "error" not in values)):
        raise BudgetConsentError("budget_consent_invalid_request", 400)
    values.pop("error_description", None)
    return values


def register(app, read_json_body, *, auth_enabled, session_secret, secure_cookie):
    app[KEY] = ConsentHolder()
    app[_SECRET_KEY] = session_secret

    async def lifetime(application):
        if os.environ.get(FLAG) == "1":
            try:
                application[KEY].service = await asyncio.to_thread(
                    create_service, auth_enabled=auth_enabled, session_secret=session_secret, secure_cookie=secure_cookie)
            except Exception as exc:
                log.error("Budget consent startup exception=%s", type(exc).__name__)
        yield
        application[KEY].service = None

    async def callback(request):
        try:
            service = _service(request)
            values = _query(request)
            flow = await asyncio.to_thread(service.flow_for_state, values["state"])
            nonce = _bindings(request).get(flow["id"], "")
            await api._settled_thread(lambda: service.callback(nonce=nonce, **values))
            return web.Response(status=303, headers={**api.PRIVATE_HEADERS, "Location": COMPLETE})
        except Exception as exc:
            response = _failure(exc, "callback")
            response.set_status(303)
            response.headers["Location"] = COMPLETE
            return response

    async def bridge(request):
        name = "complete.js" if request.path == SCRIPT else "complete.html"
        response = web.FileResponse(_STATIC / name, headers=api.PRIVATE_HEADERS)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})
        return response

    async def handle(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            service = _service(request)
            owner, fingerprint = _browser(request, auth_enabled, session_secret, mutation=request.method == "POST")
            if request.query:
                raise BudgetConsentError("budget_consent_invalid_request", 400)
            bindings = _bindings(request)
            if request.method == "GET":
                if operation == "pending":
                    payload = {"pending": await asyncio.to_thread(service.pending, owner, fingerprint, bindings)}
                elif operation == "status":
                    payload = {"consents": await asyncio.to_thread(service.status, owner)}
                elif operation == "renewal-reminders":
                    payload = await asyncio.to_thread(service.renewal_reminders, owner)
                else:
                    payload = await asyncio.to_thread(service.reconcile, owner)
                return web.json_response(payload, headers=api.PRIVATE_HEADERS)
            body, error = await read_json_body(request, 4096, reject_duplicates=True)
            if error:
                return body
            model = BudgetConsentStartRequest if operation == "start" else BudgetConsentFlowRequest
            values = model.model_validate(body).model_dump()
            owner, fingerprint = _browser(request, auth_enabled, session_secret, mutation=True)
            service = _service(request)
            if operation == "start":
                result = await api._settled_thread(lambda: service.start(owner, auth_fingerprint=fingerprint, psu=_psu(request), **values))
                nonce = result.pop("_nonce")
                response = web.json_response({"ok": True, **result}, headers=api.PRIVATE_HEADERS)
                response.set_cookie(cookie_name(result["flow_id"]), nonce, max_age=900,
                                    secure=True, httponly=True, samesite="Lax", path="/")
                return response
            flow_id = values["flow_id"]
            nonce = bindings.get(flow_id, "")
            if operation == "complete":
                result = await api._settled_thread(lambda: service.complete(owner, flow_id, auth_fingerprint=fingerprint,
                                                                            nonce=nonce, psu=_psu(request)))
                status = 200 if result["status"] == "adopted" else 202
            else:
                result = await api._settled_thread(lambda: service.cancel(owner, flow_id, fingerprint, nonce))
                status = 200
            return web.json_response({"ok": True, **result}, status=status, headers=api.PRIVATE_HEADERS)
        except Exception as exc:
            return _failure(exc, operation)

    app.cleanup_ctx.append(lifetime)
    app.router.add_get(CALLBACK, callback, allow_head=False)
    app.router.add_get(COMPLETE, bridge, allow_head=False)
    app.router.add_get(SCRIPT, bridge, allow_head=False)
    for operation in ("pending", "status", "renewal-reminders", "reconcile"):
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
    for operation in ("start", "complete", "cancel"):
        app.router.add_post(PREFIX + "/" + operation, handle)
