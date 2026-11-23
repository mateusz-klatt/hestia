"""Read-only household budget API. Viewers see an allowlisted copy of one owner's data.

The route floor is ``viewer``, which also admits operator and admin. ``require_household_data_grant``
rejects every stored role other than an active viewer, and it runs inside the same SQLite snapshot
as the read, before the projection cache, filters or cursor decryption. A second check in a fresh
snapshot runs after the body is encoded and before the response is built. ``before_grant_recheck``
is a no-op seam so tests can revoke the grant at that point.

Discovery (``GET /access``) ignores ``X-Budget-Grant`` and returns only ``grant_id``. Every other
household GET requires exactly one of those headers. Final recheck failure, including a grant
change, discards the encoded body and returns a fixed 403. The first check still reports a missing
flag as 404 and a non-SQLite backend as 409.

A response that is about to be sent writes one ``budget_analytics_audit`` row in this request:
the actor is the member, the owner is the grant owner, and the action is the view name. A denied
or failed read writes nothing. That row is not emitted to ``/api/audit`` or the SSE stream.

``GET /monthly-budget-settings`` reads the P1 bootstrap ``HESTIA_BUDGET_MONTH_CONFIG`` only.
P4's ``budget_month_settings`` table is not part of this API.
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from .. import store_sql
from ..db import init_db
from . import api
from .analytics_core import AnalyticsError
from .config import BudgetConfigError
from .household import HouseholdAuthError
from .household_views import MAX_RESPONSE_BYTES, HouseholdReader, _QUERY
from .views import BudgetUsersBackendRequired

PREFIX = "/api/budget/household"
GRANT_HEADER = "X-Budget-Grant"
READS = (
    "access",
    "report-accounts",
    "monthly-budget",
    "overview",
    "transactions",
    "categories",
    "rules",
    "transfers",
    "spending-limits",
    "recurring",
    "mortgages",
    "inbox",
    "quarantine",
    "monthly-budget-settings",
)
log = logging.getLogger("hestia.budget.household_api")


def before_grant_recheck():
    """Called after the read snapshot closes and before the fresh authorization check."""
    return None


def _scope(request, auth_enabled, *, require_header):
    """Request facts checked before the database is opened. The snapshot helper repeats them."""
    if os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") != "1":
        raise HouseholdAuthError(404, "budget_disabled")
    if os.environ.get("HESTIA_BUDGET_ANALYTICS_ENABLED") != "1":
        raise HouseholdAuthError(404, "budget_analytics_disabled")
    user = request.get("hestia_user")
    if not auth_enabled() or not isinstance(user, str) or not user:
        raise HouseholdAuthError(401, "budget_not_signed_in")
    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        raise BudgetUsersBackendRequired()
    header = None
    if require_header:
        values = request.headers.getall(GRANT_HEADER, [])
        if len(values) != 1 or not isinstance(values[0], str) or not values[0]:
            raise HouseholdAuthError(403, "budget_forbidden")
        header = values[0]
    return user, header


def _query(request, operation):
    allowed = _QUERY.get(operation)
    if allowed is None:
        raise AnalyticsError()
    values = {}
    for key in request.query:
        if key not in allowed or len(request.query.getall(key)) != 1:
            raise AnalyticsError()
        value = request.query[key]
        maximum = 8192 if key == "cursor" else 128
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise AnalyticsError()
        values[key] = value
    return values


def _reader(secret):
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return HouseholdReader(sessions, cursor_secret=secret)


def _read(member, operation, query, header, secret):
    return _reader(secret).read(member, operation, query, header)


def _record(member, owner, view, secret):
    """Audit a read only after the handler has decided the response will be sent."""
    _reader(secret).record_read(member, owner, view)


def _recheck(member, grant_id, header, secret, *, require_header):
    return _reader(secret).recheck(member, grant_id, header, require_header=require_header)


def _log_failure(operation, exc):
    log.error("household operation=%s exception=%s", operation, type(exc).__name__)


def register(app, read_json_body, *, auth_enabled, session_secret):
    del read_json_body

    async def handle(request):
        operation = request.path.rsplit("/", 1)[-1]
        require_header = operation != "access"
        body = None
        try:
            member, header = _scope(request, auth_enabled, require_header=require_header)
            secret = api._secret(session_secret)
            query = _query(request, operation)
            body, owner, grant_id = await asyncio.to_thread(_read, member, operation, query, header, secret)
            before_grant_recheck()
            try:
                fresh = await asyncio.to_thread(
                    _recheck, member, grant_id, header, secret, require_header=require_header)
            except Exception as exc:
                body = None
                _log_failure(operation, exc)
                return api._error(500, "budget_internal_error")
            if not fresh:
                body = None
                return api._error(403, "budget_forbidden")
            if len(body) > MAX_RESPONSE_BYTES:
                body = None
                return api._error(413, "budget_analytics_limit")
            await asyncio.to_thread(_record, member, owner, operation, secret)
            outgoing = bytes(body)
            body = None
            return web.Response(body=outgoing, content_type="application/json", charset="utf-8",
                                headers=api.PRIVATE_HEADERS)
        except HouseholdAuthError as exc:
            body = None
            return api._error(exc.status, exc.code)
        except AnalyticsError as exc:
            body = None
            if exc.status >= 500 or exc.status == 413:
                _log_failure(operation, exc)
            return api._error(exc.status, exc.code)
        except Exception as exc:
            body = None
            return api._failure(exc, operation)
        finally:
            body = None

    for operation in READS:
        app.router.add_get(PREFIX + "/" + operation, handle, allow_head=False)
