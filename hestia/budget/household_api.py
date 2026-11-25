"""Household budget API. Grant members see an allowlisted copy of one owner's data.

The route floor is ``viewer``, which also admits operator and admin. ``require_household_data_grant``
requires an enabled viewer or operator. Eligibility runs before the shared analytics
lock, then again inside the read snapshot, before the projection cache, filters or cursor decryption.
After the body is encoded, one ``BEGIN IMMEDIATE`` transaction re-authorizes that same grant and,
except for discovery, writes at most one sanitised analytics-audit row per member and view per 60
seconds. The body is sent only after that transaction commits. ``before_grant_recheck`` runs before
the transaction and ``after_grant_commit`` runs after it, so tests can revoke on either side.

Discovery (``GET /access``) ignores ``X-Budget-Grant`` and returns only ``grant_id``. It uses the
same immediate transaction and writes no audit row. Every other household GET requires exactly one
header of 64 lowercase hex characters. A denial, a failed audit, or a grant change discards the
encoded body and returns a fixed 403. The first check still reports a missing flag as 404 and a
non-SQLite backend as 409.

An audit row names the member, the grant owner and the view. It stores no amounts, aliases, grant
id or query values, and it is not emitted to ``/api/audit`` or the SSE stream. Household rows older
than 180 days are deleted when a new row is inserted. A denied or failed read writes nothing.

``GET /monthly-budget-settings`` reads the P1 bootstrap ``HESTIA_BUDGET_MONTH_CONFIG`` only.
P4's ``budget_month_settings`` table is not part of this API.
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web

from .. import authz, store_sql
from ..db import init_db
from . import api
from .analytics_core import AnalyticsError
from .config import BudgetConfigError
from .household import HouseholdAuthError, valid_grant_id
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
    """Called after the read snapshot closes and before the authorizing transaction."""
    return None


def after_grant_commit():
    """Called after that transaction commits and before the body is sent."""
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
        if len(values) != 1 or not valid_grant_id(values[0]):
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
    with sessions() as session:
        store_sql.auth_session_phase(session)
        authz.require_supported_policy_phase(session)
    return HouseholdReader(sessions, cursor_secret=secret)


def _read(member, operation, query, header, secret):
    return _reader(secret).read(member, operation, query, header)


def _commit(member, owner, grant_id, header, view, secret):
    """Re-authorize this grant and audit the read in one immediate transaction."""
    _reader(secret).commit_authorized_read(member, owner, grant_id, header, view)


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
            if len(body) > MAX_RESPONSE_BYTES:
                body = None
                return api._error(413, "budget_analytics_limit")
            try:
                await asyncio.to_thread(_commit, member, owner, grant_id, header, operation, secret)
            except HouseholdAuthError:
                raise
            except Exception as exc:
                body = None
                _log_failure(operation, exc)
                return api._error(500, "budget_internal_error")
            after_grant_commit()
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
