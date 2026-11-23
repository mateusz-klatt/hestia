"""Default-off Gmail receipt import routes and app-scoped poller."""
from __future__ import annotations

import asyncio
import hmac
import os
import time

from aiohttp import web
from pydantic import ValidationError

from .. import api_contract, store_sql
from ..db import db_path, init_db
from . import api, gmail_config, receipt_api
from .config import BudgetConfigError
from .gmail_client import GmailClient, GmailError
from .gmail_message import GmailMessageProcessor
from .gmail_store import GmailStore
from .gmail_sync import GmailSync, grant_transition_lock
from .gmail_worker import ReceiptParserWorker
from .views import BudgetAccessDenied, BudgetUsersBackendRequired

FLAG = "HESTIA_BUDGET_GMAIL_IMPORT_ENABLED"
PREFIX = receipt_api.PREFIX
MAX_BODY_BYTES = 4096
_POLL_CHECK_SECONDS = 60
_WRITES = {
    "gmail-grant": api_contract.BudgetReceiptGmailGrantRequest,
    "gmail-revoke": api_contract.BudgetReceiptGmailRevokeRequest,
    "gmail-sync": api_contract.BudgetReceiptGmailSyncRequest,
}


def enabled():
    return (os.environ.get(FLAG) == "1" and os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") == "1"
            and receipt_api.enabled())


def _poll_enabled(auth_enabled):
    return enabled() and auth_enabled() and os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite"


def _store(path):
    engine, sessions = init_db(path)
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return GmailStore(sessions, clock=time.time)


def _settings(secret):
    try:
        settings = gmail_config.load_settings(session_secret=secret)
    except BudgetConfigError:
        raise GmailError("budget_receipt_gmail_unconfigured") from None
    if settings is None:
        raise GmailError("budget_receipt_gmail_unconfigured")
    return settings


def _grant_result(state):
    return {"ok": True, "grant_status": state["grant_status"], "grant_generation": state["grant_generation"],
            "version": state["version"]}


class _Runtime:
    def __init__(self):
        self.client = GmailClient()
        self.worker = ReceiptParserWorker()

    def _service(self, path, store, settings):
        return GmailSync(settings, store, self.client, GmailMessageProcessor(settings, self.worker), path)

    def _status(self, store, owner, secret):
        state = store.status(owner)
        try:
            settings = _settings(secret)
        except GmailError:
            configured = False
        else:
            configured = (hmac.compare_digest(owner.encode(), settings.owner.encode()) and self.worker.ready())
        return {"enabled": True, "configured": configured, **state,
                "google_revoke_url": "https://myaccount.google.com/linkedapps",
                "google_revoke_affects_calendar_sync": True}

    def run(self, owner, operation, values, secret):
        path = db_path()
        store = _store(path)
        if operation == "gmail-status":
            return self._status(store, owner, secret)
        if operation == "gmail-revoke" or (operation == "gmail-grant" and values["action"] == "disable"):
            version = values["expected_version"]
            with grant_transition_lock(self.client, owner):
                store.check_version(owner, version)
                self.client.forget(owner)
                store.transition(owner, version, "revoked" if operation == "gmail-revoke" else "disabled")
                return _grant_result(store.status(owner))
        service = self._service(path, store, _settings(secret))
        if operation == "gmail-grant":
            if not self.worker.ready():
                raise GmailError("parser_unavailable")
            return _grant_result(service.transition(owner, values["expected_version"], "active", defer_profile=True))
        return service.schedule(owner, force_full_resync=values["force_full_resync"])

    def poll(self, auth_enabled, session_secret):
        if not _poll_enabled(auth_enabled):
            return
        try:
            settings = _settings(api._secret(session_secret))
            path = db_path()
            service = self._service(path, _store(path), settings)
            service.sync(settings.owner, poll=True)
        except (GmailError, BudgetConfigError, BudgetAccessDenied, BudgetUsersBackendRequired):
            return
        except Exception as error:
            api.log.warning("Gmail receipt poll failed exception=%s", type(error).__name__)


def _authorize(request, auth_enabled, session_secret):
    if not enabled():
        raise api._Rejected(404, "budget_receipts_gmail_disabled")
    owner = api._principal(request, auth_enabled)
    secret = api._secret(session_secret)
    api._browser(request, owner, secret, allow_same_origin_fetch=request.method == "GET")
    return owner, secret


def register(app, read_json_body, *, auth_enabled, session_secret):
    runtime = None

    def service_runtime():
        nonlocal runtime
        if runtime is None:
            runtime = _Runtime()
        return runtime

    async def handle(request):
        operation = request.path.rsplit("/", 1)[-1]
        try:
            owner, secret = _authorize(request, auth_enabled, session_secret)
            if request.query:
                raise api._Rejected(400, "budget_receipt_gmail_invalid_request")
            values = {}
            if request.method == "POST":
                body, error = await read_json_body(request, MAX_BODY_BYTES, reject_duplicates=True)
                if error:
                    return body
                values = _WRITES[operation].model_validate(body).model_dump()
                owner, secret = _authorize(request, auth_enabled, session_secret)
            active_runtime = service_runtime()
            result = await api._settled_thread(lambda: active_runtime.run(owner, operation, values, secret))
            status = 202 if operation == "gmail-sync" else 200
            return web.json_response(result, status=status, headers=api.PRIVATE_HEADERS)
        except GmailError as error:
            code = error.code
            if not code.startswith("budget_receipt_gmail_"):
                code = "budget_receipt_gmail_" + code
            return api._error(error.status, code)
        except ValidationError:
            return api._error(400, "budget_receipt_gmail_invalid_request")
        except Exception as error:
            return api._failure(error, operation)

    async def polling(_app):
        if not _poll_enabled(auth_enabled):
            yield
            return
        active_runtime = service_runtime()

        async def loop():
            while True:
                await api._settled_thread(lambda: active_runtime.poll(auth_enabled, session_secret))
                await asyncio.sleep(_POLL_CHECK_SECONDS)

        task = asyncio.create_task(loop())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(polling)
    app.router.add_get(PREFIX + "/gmail-status", handle, allow_head=False)
    for operation in _WRITES:
        app.router.add_post(PREFIX + "/" + operation, handle)
