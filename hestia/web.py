"""Bootstrap web UI — operator browses to ``http://hestia:8927`` and confirms
``name`` / ``room`` / ``type`` for every discovered node.

This is *bootstrap* tooling: the operator runs it once to teach hestia what each
Z-Wave node is, then the confirmed metadata lets hestia (and downstream Home
Assistant) talk about devices by their real names instead of node ids. Phase 3
will graduate proxy mode to standalone once enough nodes are confirmed.

Architecture: aiohttp runs in the same asyncio loop as the proxy/server
runtime, so handlers read ``rt`` directly and every write still goes through
``process_control_op`` / ``_persist``'s ``save_lock``.

Per-field UI saves — the row's name / room / type each have an independent
``[Save]`` (or ``[✓]`` for confirm-type) button. Each posts only its own field
so naming a device never accidentally freezes whatever ``type`` is currently
shown. The confirm button is **disabled** when the classifier hasn't yet
inferred a type, so the operator can't accidentally pin a node to ``unknown``.

Security: the listener defaults to ``127.0.0.1`` (loopback). Non-loopback binds
require ``HESTIA_WEB_ALLOW_REMOTE=1`` — same guard as the control port. The web
UI is unauthenticated; do not expose it.

Run: wired automatically by `hestia.proxy.main()` and `hestia.server.main()`.
Env: ``HESTIA_WEB_HOST`` (default ``127.0.0.1``), ``HESTIA_WEB_PORT``
(default ``8927``), ``HESTIA_WEB_ALLOW_REMOTE=1`` to bypass the loopback guard.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from aiohttp import ClientConnectionError, web

from . import auth, auth_qr, authz, store_sql
from .budget import api as budget_api
from .budget import consent_api, consent_context, device_token
from .budget.config import BudgetConfigError
from .budget.i18n import validate_languages
from .budget.views import BudgetUsersBackendRequired
from .classifier import DeviceType
from .db import AppMeta
from .automations import rule_vocab
from .proxy import (INJECT_GAP_SECS, IR_BUTTONS, KLIMA, _CLOSED_SENTINEL, _LOOPBACK, _audit,
                    _merged_discovery, _num_env, _truthy_env, globals_snapshot, process_control_op)

log = logging.getLogger("hestia.web")

MAX_BODY = 8192                                      # cap POST body size (bytes)
MAX_RULE_BODY = 65536                                # larger cap for an automation rule
BODY_READ_TIMEOUT = 10                               # seconds; a stalled Content-Length read must not pin the handler
MAX_STRING = 256                                     # cap name / room length (chars)
SSE_IDLE_TIMEOUT = _num_env("HESTIA_SSE_KEEPALIVE", 5.0, 1.0, 3600.0)  # idle→keepalive; also how fast a
#       reloaded-away client's slot is reclaimed (write fails). Clamped ≥1s so a typo can't tight-loop.
SSE_MAX_LIFETIME = float(os.environ.get("HESTIA_SSE_LIFETIME", "3600"))
_TYPES = {t.value for t in DeviceType}
_clock = time.monotonic                              # module-level rebindable for tests
_LOGIN_LIMITER_KEY = web.AppKey("login_limiter", auth.LoginLimiter)
_NAME_FIELDS = {"op", "node", "name", "room", "type", "ep"}   # allowlist for /api/name (ep = endpoint label)
_BODY_NOT_OBJECT = "body must be a JSON object"   # shared 4xx message (/api/ir, /api/name, /api/control)
_CONTROL_OPS = {"switch", "level", "cover", "thermostat", "thermostat_power"}
_CONTROL_FIELDS = {
    "switch": {"op", "node", "on", "endpoint"},
    "level": {"op", "node", "value"},
    "cover": {"op", "node", "value"},
    "thermostat": {"op", "node", "celsius"},
    "thermostat_power": {"op", "node", "on"},
}
_SCENE_TARGETS = {
    "lights_off": ("light", False),
    "lights_on": ("light", True),
    "blinds_down": ("blind", False),
    "blinds_up": ("blind", True),
}
# `blinds_set` drives every (non-excluded) blind to an explicit wire position 0-99 (the whole-home
# slider), unlike the on/off extremes above; it carries a `value` and is handled outside _SCENE_TARGETS.
_BLINDS_SET = "blinds_set"
_SCENE_NAMES = (*_SCENE_TARGETS, _BLINDS_SET)
_TEMP_SCALES = {"C", "F", "K"}
_EMPTY_SETTINGS = {"locale": None, "temp_scale": None, "theme": None, "preferred_languages": ["en"]}
_RT_KEY = web.AppKey("rt", object)
_UI_DIST_ENV = "HESTIA_UI_DIST"
_DEFAULT_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
_PUBLIC_DOCUMENTS = {
    "/privacy-policy.html": "privacy-policy.html",
    "/terms-of-service.html": "terms-of-service.html",
}

# App-level login (#43). OPT-IN: gating is enforced ONLY when HESTIA_SESSION_SECRET is set. With no secret
# auth is OFF and routes stay OPEN (loopback/dev and pre-config deployments are unaffected; the reverse
# proxy's own auth, if any, still applies). When a secret IS set, the middleware gates /api/* on a valid
# session cookie; the app shell + /api/login|logout stay public so the login form can load and submit.
# (A secret is required to sign/verify cookies, so login can never half-work without one.)
_SESSION_SECRET = os.environ.get("HESTIA_SESSION_SECRET", "").encode("utf-8")
_AUTH_ENABLED = bool(_SESSION_SECRET)
_COOKIE_SECURE = _truthy_env(os.environ.get("HESTIA_COOKIE_SECURE"))   # set on the HTTPS (Apache) deployment
_SESSION_COOKIE = "hestia_session"
_USER_KEY = "hestia_user"
_ROLE_KEY = "hestia_role"          # the request's resolved RBAC role (set by the auth middleware)
_SESSION_CONTEXT_KEY = "hestia_session_context"
_now = time.time          # WALL clock for session expiry (must survive restarts, unlike _clock); rebindable for tests


@dataclass(frozen=True)
class ValidatedSessionContext:
    """Validated ingress session metadata passed downstream to browser-sensitive guards."""

    username: str
    role: str
    carrier: str
    version: int
    purpose: "str | None"
    generation: int
    expiry: int
    session_phase: str
    policy_phase: str
    raw_token: str


def _require_safe_web_bind(host: str) -> None:
    """Refuse to expose the unauthenticated web UI beyond loopback unless the
    operator explicitly opted in. Mirrors :func:`hestia.proxy._require_safe_control_bind`."""
    if host not in _LOOPBACK and os.environ.get("HESTIA_WEB_ALLOW_REMOTE") != "1":
        raise RuntimeError(
            f"refusing to bind the unauthenticated web UI to {host!r}; "
            "set HESTIA_WEB_ALLOW_REMOTE=1 to override (not recommended)"
        )


def _json(status, payload):
    body = json.dumps(payload).encode("utf-8")
    return web.Response(body=body, status=status, content_type="application/json")


def _empty(status, extra_headers=()):
    headers = {"Content-Length": "0"}
    headers.update(dict(extra_headers))
    return web.Response(status=status, body=b"", headers=headers)


async def _wait_event(queue: asyncio.Queue):
    """Wait on a subscriber queue inside the event loop, capped at ``SSE_IDLE_TIMEOUT``
    via an ``asyncio.timeout`` scope. Returns the event on success or
    ``None`` on idle (so the handler writes a keepalive comment instead of dying)."""
    try:
        async with asyncio.timeout(SSE_IDLE_TIMEOUT):
            return await queue.get()
    except asyncio.TimeoutError:
        return None


def _summary(devices: dict) -> dict:
    """Aggregate counters for the operator: how many nodes total, how many
    user-confirmed, how many still untyped by the classifier."""
    return {
        "total": len(devices),
        "confirmed": sum(1 for d in devices.values() if d.get("confidence") == "confirmed"),
        "unknown": sum(1 for d in devices.values() if d.get("type") == "unknown"),
    }


def _is_public_route(method: str, path: str) -> bool:
    """Exact method+path allowlist reachable WITHOUT a session: the app shell + its assets + the /ui/
    redirect (GET), the two legal documents (GET/HEAD), and login/logout (POST). Everything else is gated —
    incl. a wrong-method hit on a public path (e.g. GET /api/login → gated, not a public 405)."""
    if method in ("GET", "HEAD") and path in _PUBLIC_DOCUMENTS:
        return True
    if method == "GET":
        return (path in ("/", "/ui/", "/api/auth/features")
                or path in consent_api.PUBLIC_GET or path.startswith("/assets/"))
    return method == "POST" and (path in ("/api/login", "/api/logout", device_token.REDEEM_PATH, auth_qr.REDEEM_PATH)
                                 or _ROUTE_MIN_ROLE.get((method, path)) == _PUBLIC)


# RBAC route policy (#73). The MINIMUM role for each non-public route, kept adjacent to make_app's route
# table; ``test_every_route_is_classified`` asserts every registered route is here (or public), so this
# can't silently drift from the routes. Public routes are short-circuited in the middleware before this.
_VIEWER, _OPERATOR, _ADMIN = "viewer", "operator", "admin"
_PUBLIC = "public"
# Route paths that appear in BOTH the floor map and the route registration — named once so the two
# can't drift (and to satisfy the no-duplicate-literal lint). Single-use paths stay inline literals.
_PATH_SETTINGS = "/api/settings"
_PATH_ROOM_ICONS = "/api/rooms/icons"
_PATH_AUTOMATIONS = "/api/automations"
_PATH_USERS = "/api/users"
_PATH_WHOLE_HOME = "/api/whole-home"
_USERNAME_REQUIRED = "username is required"   # shared 400 message across the admin user verbs
_ROUTE_MIN_ROLE = {
    ("GET", "/api/budget/consent/pending"): _ADMIN,
    ("GET", "/api/budget/consent/status"): _ADMIN,
    ("GET", "/api/budget/consent/renewal-reminders"): _ADMIN,
    ("GET", "/api/budget/consent/reconcile"): _ADMIN,
    ("POST", "/api/budget/consent/start"): _ADMIN,
    ("POST", "/api/budget/consent/complete"): _ADMIN,
    ("POST", "/api/budget/consent/cancel"): _ADMIN,
    ("GET", "/api/budget/analytics/overview"): _ADMIN,
    ("GET", "/api/budget/analytics/transactions"): _ADMIN,
    ("POST", "/api/budget/analytics/transactions-by-keys"): _ADMIN,
    ("GET", "/api/budget/analytics/categories"): _ADMIN,
    ("GET", "/api/budget/analytics/rules"): _ADMIN,
    ("GET", "/api/budget/analytics/transfers"): _ADMIN,
    ("GET", "/api/budget/analytics/recurring"): _ADMIN,
    ("GET", "/api/budget/analytics/spending-limits"): _ADMIN,
    ("GET", "/api/budget/analytics/mortgages"): _ADMIN,
    ("GET", "/api/budget/analytics/inbox"): _ADMIN,
    ("GET", "/api/budget/analytics/quarantine"): _ADMIN,
    ("GET", "/api/budget/analytics/forecast"): _ADMIN,
    ("GET", "/api/budget/analytics/monthly-budget"): _ADMIN,
    ("GET", "/api/budget/analytics/monthly-budget/charts"): _ADMIN,
    ("GET", "/api/budget/household/access"): _VIEWER,
    ("GET", "/api/budget/household/report-accounts"): _VIEWER,
    ("GET", "/api/budget/household/monthly-budget"): _VIEWER,
    ("GET", "/api/budget/household/overview"): _VIEWER,
    ("GET", "/api/budget/household/transactions"): _VIEWER,
    ("GET", "/api/budget/household/categories"): _VIEWER,
    ("GET", "/api/budget/household/rules"): _VIEWER,
    ("GET", "/api/budget/household/transfers"): _VIEWER,
    ("GET", "/api/budget/household/spending-limits"): _VIEWER,
    ("GET", "/api/budget/household/recurring"): _VIEWER,
    ("GET", "/api/budget/household/mortgages"): _VIEWER,
    ("GET", "/api/budget/household/inbox"): _VIEWER,
    ("GET", "/api/budget/household/quarantine"): _VIEWER,
    ("GET", "/api/budget/household/monthly-budget-settings"): _VIEWER,
    ("POST", "/api/budget/analytics/category-create"): _ADMIN,
    ("POST", "/api/budget/analytics/category-update"): _ADMIN,
    ("POST", "/api/budget/analytics/category-translation-set"): _ADMIN,
    ("POST", "/api/budget/analytics/category-translation-delete"): _ADMIN,
    ("POST", "/api/budget/analytics/label-translation-set"): _ADMIN,
    ("POST", "/api/budget/analytics/label-translation-delete"): _ADMIN,
    ("POST", "/api/budget/analytics/translation-import"): _ADMIN,
    ("POST", "/api/budget/analytics/category-colors-spread"): _ADMIN,
    ("POST", "/api/budget/analytics/annotation"): _ADMIN,
    ("POST", "/api/budget/analytics/rule-preview"): _ADMIN,
    ("POST", "/api/budget/analytics/rule-save"): _ADMIN,
    ("POST", "/api/budget/analytics/rule-batch-preview"): _ADMIN,
    ("POST", "/api/budget/analytics/rule-batch-save"): _ADMIN,
    ("POST", "/api/budget/analytics/transfer-decision"): _ADMIN,
    ("POST", "/api/budget/analytics/spending-limit-save"): _ADMIN,
    ("POST", "/api/budget/analytics/mortgage-save"): _ADMIN,
    ("POST", "/api/budget/analytics/mortgage-payment-set"): _ADMIN,
    ("POST", "/api/budget/analytics/inbox-mark"): _ADMIN,
    ("POST", "/api/budget/receipts/parse-text"): _ADMIN,
    ("POST", "/api/budget/receipts/confirm"): _ADMIN,
    ("POST", "/api/budget/receipts/list"): _ADMIN,
    ("POST", "/api/budget/receipts/search"): _ADMIN,
    ("GET", "/api/budget/receipts/detail"): _ADMIN,
    ("GET", "/api/budget/receipts/image"): _ADMIN,
    ("POST", "/api/budget/receipts/delete"): _ADMIN,
    ("POST", device_token.PAIR_PATH): _ADMIN,
    ("POST", device_token.PAIR_CANCEL_PATH): _ADMIN,
    ("POST", device_token.LOGIN_PATH): _PUBLIC,
    ("POST", "/api/budget/receipts/device-token/list"): _ADMIN,
    ("POST", "/api/budget/receipts/device-token/revoke"): _ADMIN,

    ("POST", "/api/budget/receipts/gmail-grant"): _ADMIN,
    ("POST", "/api/budget/receipts/gmail-revoke"): _ADMIN,
    ("POST", "/api/budget/receipts/gmail-sync"): _ADMIN,
    ("GET", "/api/budget/receipts/gmail-status"): _ADMIN,
    ("GET", "/api/budget/connections"): _ADMIN,
    ("GET", "/api/budget/accounts"): _ADMIN,
    ("GET", "/api/budget/status"): _ADMIN,
    ("GET", "/api/budget/transactions"): _ADMIN,
    ("GET", "/api/budget/balances"): _ADMIN,
    ("GET", "/api/budget/limits"): _ADMIN,
    ("POST", "/api/budget/account-selection"): _ADMIN,
    ("POST", "/api/budget/account-kind"): _ADMIN,
    ("POST", "/api/budget/account-credit-limit"): _ADMIN,
    ("POST", "/api/budget/connection-state"): _ADMIN,
    ("POST", "/api/budget/background-selection"): _ADMIN,
    ("POST", "/api/budget/refresh"): _ADMIN,
    ("GET", "/api/budget/jobs"): _ADMIN,
    ("GET", "/api/calendars"): _VIEWER,
    ("GET", "/api/calendars/instances"): _VIEWER,
    ("POST", "/api/calendars/events"): _OPERATOR,
    ("POST", "/api/calendars/events/delete"): _OPERATOR,
    # reads every signed-in user (incl. a read-only viewer) needs — the room / event-log / temps / own-prefs surface
    ("GET", "/api/discovery"): _VIEWER,
    ("GET", "/api/events"): _VIEWER,
    ("GET", "/api/audit"): _VIEWER,
    ("GET", _PATH_SETTINGS): _VIEWER,
    ("POST", _PATH_SETTINGS): _VIEWER,            # a user's OWN UI prefs (locale / temp scale / theme)
    ("GET", _PATH_ROOM_ICONS): _VIEWER,
    ("GET", "/api/whoami"): _VIEWER,
    ("GET", "/api/me/capabilities"): _VIEWER,
    ("POST", auth_qr.CODE_PATH): _VIEWER,
    ("POST", device_token.EXCHANGE_PATH): _VIEWER,
    ("POST", "/api/me/password"): _VIEWER,         # any signed-in user changes their OWN password (verifies current)
    # room actions: operator and admin (a viewer is read-only — the UI hides these, the server enforces it)
    ("POST", "/api/control"): _OPERATOR,
    ("POST", "/api/ir"): _OPERATOR,
    ("POST", "/api/scene"): _OPERATOR,
    # config / engineering surface: admin only — incl. the reads that would leak it (automation rules carry
    # presence-trigger MAC addresses; db-stats / rf433 are engineering observability the operator hides)
    ("POST", "/api/name"): _ADMIN,
    ("GET", _PATH_WHOLE_HOME): _ADMIN,             # which devices are in "all" — admin config (registry-only)
    ("POST", _PATH_WHOLE_HOME): _ADMIN,
    ("GET", _PATH_AUTOMATIONS): _ADMIN,
    ("POST", _PATH_AUTOMATIONS): _ADMIN,
    ("POST", "/api/automations/delete"): _ADMIN,
    ("POST", "/api/graduate"): _ADMIN,
    ("POST", _PATH_ROOM_ICONS): _ADMIN,
    ("GET", "/api/db/stats"): _ADMIN,
    ("GET", "/api/rf433"): _ADMIN,
    # user administration: admin only (add/list accounts, change roles, disable, reset another's password)
    ("GET", _PATH_USERS): _ADMIN,
    ("POST", _PATH_USERS): _ADMIN,
    ("POST", "/api/users/role"): _ADMIN,
    ("POST", "/api/users/disabled"): _ADMIN,
    ("POST", "/api/users/reset-password"): _ADMIN,
}
_AUTHENTICATED = "authenticated"
_OWNER_ONLY = "owner_only"
_LEGACY_RECEIPTS = "legacy_receipts"
_DEVICE_ADMIN = "device_admin"
_ACCOUNT_READS = frozenset({
    ("GET", "/api/budget/" + operation)
    for operation in ("connections", "accounts", "status", "transactions", "balances", "limits")
})
_RECEIPT_DATA_ROUTES = frozenset({
    ("POST", device_token.PARSE_PATH),
    ("POST", device_token.CONFIRM_PATH),
    ("POST", device_token.LIST_RECEIPTS_PATH),
    ("POST", device_token.SEARCH_PATH),
    ("GET", device_token.DETAIL_PATH),
    ("GET", device_token.IMAGE_PATH),
    ("POST", device_token.DELETE_PATH),
})
_AUTHENTICATED_ROUTES = frozenset({
    ("GET", "/api/whoami"),
    ("GET", "/api/me/capabilities"),
    ("POST", auth_qr.CODE_PATH),
    ("POST", device_token.EXCHANGE_PATH),
    ("GET", _PATH_SETTINGS),
    ("POST", _PATH_SETTINGS),
    ("POST", "/api/me/password"),
})
_USER_ADMIN_ROUTES = frozenset({
    (method, path) for (method, path) in _ROUTE_MIN_ROLE
    if path == _PATH_USERS or path.startswith(_PATH_USERS + "/")
})
_OWNER_ONLY_ROUTES = frozenset({
    (method, path) for (method, path) in _ROUTE_MIN_ROLE
    if path.startswith("/api/budget/consent/")
    or (method, path) in {
        ("GET", "/api/budget/jobs"),
        ("POST", "/api/budget/account-selection"),
        ("POST", "/api/budget/account-kind"),
        ("POST", "/api/budget/account-credit-limit"),
        ("POST", "/api/budget/connection-state"),
        ("POST", "/api/budget/background-selection"),
        ("POST", "/api/budget/refresh"),
        ("GET", "/api/budget/analytics/inbox"),
        ("POST", "/api/budget/analytics/inbox-mark"),
        ("POST", "/api/budget/receipts/gmail-grant"),
        ("POST", "/api/budget/receipts/gmail-revoke"),
        ("POST", "/api/budget/receipts/gmail-sync"),
        ("GET", "/api/budget/receipts/gmail-status"),
    }
})


def _operation_requirement(method: str, path: str, floor: str) -> str:
    route = method, path
    if route in _AUTHENTICATED_ROUTES:
        return _AUTHENTICATED
    if path.startswith("/api/budget/household/"):
        return "household.view"
    if route in _RECEIPT_DATA_ROUTES or route == ("POST", device_token.PAIR_PATH):
        return _LEGACY_RECEIPTS
    if route in {
        ("POST", device_token.PAIR_CANCEL_PATH),
        ("POST", device_token.LIST_PATH), ("POST", device_token.REVOKE_PATH),
    }:
        return _DEVICE_ADMIN
    if route in _OWNER_ONLY_ROUTES:
        return _OWNER_ONLY
    if route in _ACCOUNT_READS:
        return "budget.view"
    if path.startswith("/api/budget/analytics/"):
        return "budget.view" if method == "GET" or path.endswith("/transactions-by-keys") else "budget.edit"
    if route == ("GET", "/api/audit"):
        return "system.audit.view"
    if route in _USER_ADMIN_ROUTES:
        return "system.manage_users"
    if path == "/api/calendars":
        return "calendar.view"
    if path == "/api/calendars/instances":
        return "calendar.view"
    if path in ("/api/calendars/events", "/api/calendars/events/delete"):
        return "calendar.edit"
    return {_VIEWER: "ioc.view", _OPERATOR: "ioc.control", _ADMIN: "ioc.configure"}[floor]


_ROUTE_REQUIREMENTS = {
    route: _operation_requirement(*route, floor)
    for route, floor in _ROUTE_MIN_ROLE.items() if floor != _PUBLIC
}
assert set(_ROUTE_REQUIREMENTS) == {
    route for route, floor in _ROUTE_MIN_ROLE.items() if floor != _PUBLIC
}


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CORE_BROWSER_READ_ROUTES = frozenset({
    ("GET", _PATH_USERS),
})
_CORE_BROWSER_MUTATION_ROUTES = frozenset({
    ("POST", _PATH_USERS),
    ("POST", "/api/users/role"),
    ("POST", "/api/users/disabled"),
    ("POST", "/api/users/reset-password"),
    ("POST", "/api/me/password"),
    ("POST", "/api/name"),
    ("POST", _PATH_WHOLE_HOME),
    ("POST", _PATH_AUTOMATIONS),
    ("POST", "/api/automations/delete"),
    ("POST", "/api/graduate"),
    ("POST", _PATH_ROOM_ICONS),
})
_CORE_BROWSER_ROUTES = _CORE_BROWSER_READ_ROUTES | _CORE_BROWSER_MUTATION_ROUTES | {
    ("POST", auth_qr.CODE_PATH),
}


_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _parse_canonical_host(authority: str) -> "tuple[str, str | None] | None":
    if authority.startswith("["):
        inner, has_close, remainder = authority[1:].partition("]")
        if not has_close or "%" in inner:
            return None
        if remainder == "":
            port_str = None
        elif remainder.startswith(":"):
            port_str = remainder[1:]
        else:
            return None
        try:
            return str(ipaddress.IPv6Address(inner)), port_str
        except ValueError:
            return None
    if "[" in authority or "]" in authority:
        return None
    host_raw, has_port, port_part = authority.partition(":")
    if has_port and ":" in port_part:
        return None
    port_str = port_part if has_port else None
    has_terminal_dot = host_raw.endswith(".")
    dns_raw = host_raw[:-1] if has_terminal_dot else host_raw
    if not dns_raw or len(dns_raw) > 253:
        return None
    try:
        host_raw.encode("idna")
    except UnicodeError:
        return None
    labels = dns_raw.split(".")
    if not all(_HOST_LABEL_RE.fullmatch(label) for label in labels):
        return None
    try:
        return str(ipaddress.IPv4Address(host_raw)), port_str
    except ValueError:
        if labels[-1].isdigit():
            return None
        return host_raw.lower(), port_str


def _canonical_origin_tuple(origin: str, *, allow_trailing_slash: bool = False) -> "tuple[str, str, int] | None":
    if (
        not origin
        or not origin.isascii()
        or any(ord(ch) <= 32 or ord(ch) == 127 for ch in origin)
        or "\\" in origin
        or "://" not in origin
    ):
        return None
    raw_scheme, rest = origin.split("://", 1)
    scheme = raw_scheme.lower()
    if scheme not in ("http", "https") or "?" in rest or "#" in rest:
        return None
    authority, has_slash, tail = rest.partition("/")
    if (has_slash and (not allow_trailing_slash or bool(tail))) or not authority or "@" in authority:
        return None
    parsed = _parse_canonical_host(authority)
    if parsed is None:
        return None
    canonical_host, port_str = parsed
    default_port = 80 if scheme == "http" else 443
    if port_str is None:
        return scheme, canonical_host, default_port
    if not port_str or len(port_str) > 5 or not port_str.isdigit():
        return None
    port = int(port_str)
    if not 1 <= port <= 65535:
        return None
    return scheme, canonical_host, port


def _canonical_https_origin(origin: "str | None", *, allow_trailing_slash: bool = False) -> "str | None":
    if not isinstance(origin, str):
        return None
    parsed = _canonical_origin_tuple(origin, allow_trailing_slash=allow_trailing_slash)
    if parsed is None or parsed[0] != "https":
        return None
    scheme, host, port = parsed
    authority = f"[{host}]" if ":" in host else host
    return f"{scheme}://{authority}" + ("" if port == 443 else f":{port}")


def _check_core_origin(request) -> "web.Response | None":
    configured = os.environ.get("HESTIA_PUBLIC_ORIGIN")
    if configured is not None and configured != "":
        expected = _canonical_origin_tuple(configured, allow_trailing_slash=True)
        if expected is None:
            return _json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "auth_origin_unavailable"})
    else:
        hosts = request.headers.getall("Host", [])
        if len(hosts) != 1:
            return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_required"})
        expected = _canonical_origin_tuple(f"{request.scheme}://{hosts[0]}")
        if expected is None:
            return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_required"})
    origins = request.headers.getall("Origin", [])
    if len(origins) != 1 or _canonical_origin_tuple(origins[0]) != expected:
        return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "origin_required"})
    return None


def _incompatible_bridge_response():
    return _json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "authorization_bridge_incompatible"})


def _required_role(method: str, path: str) -> str:
    """The minimum role for a non-public route. Fail-closed: an unclassified route is admin, including
    GET and HEAD, so a new read cannot become viewer-readable until it is explicitly classified.
    ``test_every_registered_route_is_public_or_classified`` keeps the map complete; this fallback is
    the backstop."""
    return _ROUTE_MIN_ROLE.get((method, path), _ADMIN)


def _role_allows(role, floor: str) -> bool:
    """True iff the user's resolved ``role`` clears the route's ``floor``. An unknown / None role fails
    every floor (denied) — fail closed."""
    rank = store_sql.ROLE_RANK.get(role)
    return rank is not None and rank >= store_sql.ROLE_RANK[floor]


def _required_permission(method: str, path: str) -> str:
    return _ROUTE_REQUIREMENTS.get((method, path), "system.manage_users")


def _catalog_allows(session, actor: str, requirement: str) -> bool:
    if requirement == _AUTHENTICATED:
        return True
    if requirement == "household.view":
        return True
    if requirement == _OWNER_ONLY or requirement == _DEVICE_ADMIN:
        return authz.is_active_budget_owner(session, actor)
    if requirement == _LEGACY_RECEIPTS:
        if authz.has_legacy_receipt_profile(session, actor):
            return True
        target = authz.resolve_budget_target(session, actor)
        return target is not None and bool(
            authz.effective_permissions(session, actor, owner=target)
            & {pid for pid in authz.CATALOG if pid.startswith("budget.receipts.")}
        )
    return authz.authorize(
        session, actor, requirement,
        owner=actor if requirement.startswith("budget.") else None,
    )


def _catalog_request_allowed(actor: str, method: str, path: str) -> bool:
    with store_sql.authorization_snapshot() as session:
        return _catalog_allows(session, actor, _required_permission(method, path))


def _session_context_current(ctx: ValidatedSessionContext) -> bool:
    envelope = auth.decode_session(ctx.raw_token, now=_now(), secret=_SESSION_SECRET)
    if envelope is None:
        return False
    try:
        state = store_sql.load_auth_state(ctx.username)
        if not state.enabled or not _envelope_matches_state(ctx.carrier, envelope, state):
            return False
        return (
            _role_allows(state.role, _required_role("GET", "/api/events"))
            if state.policy_phase == authz.POLICY_PHASE_LEGACY
            else _catalog_request_allowed(ctx.username, "GET", "/api/events")
        )
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return False


_BEARER_PREFIX = "Bearer "


def _request_carrier(request) -> "tuple[str, str]":
    """Return ``("bearer", token)`` or ``("cookie", token)`` for session extraction."""
    header = request.headers.get("Authorization", "")
    if header.startswith(_BEARER_PREFIX):
        return "bearer", header[len(_BEARER_PREFIX):]
    return "cookie", request.cookies.get(_SESSION_COOKIE, "")


def _envelope_matches_state(carrier: str, envelope: auth.SessionEnvelope, auth_state: store_sql.AuthState) -> bool:
    if auth_state.session_phase == store_sql.SESSION_PHASE_V2:
        if envelope.generation != auth_state.generation:
            return False
        if envelope.version == 2:
            expected_purpose = auth.SESSION_PURPOSE_BROWSER if carrier == "cookie" else auth.SESSION_PURPOSE_NATIVE
            return envelope.purpose == expected_purpose
        return True
    return envelope.version == 1


@web.middleware
async def _auth_middleware(request, handler):
    """Authenticate + authorize requests across legacy and active session/policy bridge phases."""
    if (request.method, request.path) in {
        ("GET", "/api/auth/features"), ("POST", auth_qr.REDEEM_PATH),
    }:
        return await handler(request)
    if device_token.receipts_path(request.path) and not device_token.receipts_enabled():
        return device_token.disabled_response()
    authorization = request.headers.get("Authorization", "")
    if (_AUTH_ENABLED and "Authorization" in request.headers
            and (request.method, request.path) != ("POST", device_token.LOGIN_PATH)):
        target = request.raw_path.partition("?")[0]
        decision = await asyncio.to_thread(
            device_token.decide,
            request.method, target, request.headers, request.cookies, _SESSION_SECRET,
            _now(),
        )
        if decision["action"] == "response":
            return device_token.json_error(decision)
        if decision["action"] == "accept":
            request[device_token.REQUEST_KEY] = decision["token_id"]
            request[device_token.ACTOR_REQUEST_KEY] = decision["actor"]
            request[_USER_KEY] = decision["owner"]
            request[_ROLE_KEY] = _ADMIN
            return await handler(request)
        if (request.path.startswith("/api/budget/")
                and (request.method, target) not in (
                    ("POST", device_token.REDEEM_PATH), ("GET", consent_api.CALLBACK),
                )
                and authorization.startswith(_BEARER_PREFIX)
                and auth.decode_session(
                    authorization[len(_BEARER_PREFIX):], now=_now(), secret=_SESSION_SECRET,
                ) is not None):
            return device_token.json_error({"status": 403, "code": "budget_browser_session_required"})
    public = _is_public_route(request.method, request.path)
    # A redeem request carrying a browser session must also clear the owner
    # role floor. The handler returns the generic rejection for any cookie.
    redeem_cookie = public and request.path == device_token.REDEEM_PATH and _SESSION_COOKIE in request.cookies
    if not _AUTH_ENABLED or (public and not redeem_cookie):
        if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
            try:
                await asyncio.to_thread(store_sql.check_supported_policy_phase)
            except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
                return _incompatible_bridge_response()
        return await handler(request)
    carrier, raw_token = _request_carrier(request)
    envelope = auth.decode_session(raw_token, now=_now(), secret=_SESSION_SECRET) if raw_token else None
    if envelope is None:
        if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
            try:
                await asyncio.to_thread(store_sql.check_supported_policy_phase)
            except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
                return _incompatible_bridge_response()
        if redeem_cookie:
            return await handler(request)
        return _empty(HTTPStatus.UNAUTHORIZED)
    try:
        auth_state = await asyncio.to_thread(store_sql.load_auth_state, envelope.username)
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return _incompatible_bridge_response()
    if (
        not auth_state.enabled
        or auth_state.role is None
        or not _envelope_matches_state(carrier, envelope, auth_state)
    ):
        if redeem_cookie:
            return await handler(request)
        return _empty(HTTPStatus.UNAUTHORIZED)
    request[_USER_KEY] = envelope.username
    request[_ROLE_KEY] = auth_state.role
    request[_SESSION_CONTEXT_KEY] = ValidatedSessionContext(
        username=envelope.username,
        role=auth_state.role,
        carrier=carrier,
        version=envelope.version,
        purpose=envelope.purpose,
        generation=envelope.generation,
        expiry=envelope.expiry,
        session_phase=auth_state.session_phase,
        policy_phase=auth_state.policy_phase,
        raw_token=raw_token,
    )
    if request.path.startswith("/api/budget/") and not budget_api.expected_actor_matches(
        request, envelope.username,
    ):
        if auth_state.policy_phase == authz.POLICY_PHASE_LEGACY:
            return budget_api._error(HTTPStatus.CONFLICT, "budget_actor_changed")
        return _json(HTTPStatus.CONFLICT, {"error": {"code": "budget_actor_changed"}})
    if auth_state.policy_phase == authz.POLICY_PHASE_LEGACY:
        allowed = _role_allows(auth_state.role, _required_role(request.method, request.path))
    else:
        try:
            allowed = await asyncio.to_thread(
                _catalog_request_allowed, envelope.username, request.method, request.path,
            )
        except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
            return _incompatible_bridge_response()
    if not allowed:
        if auth_state.policy_phase != authz.POLICY_PHASE_LEGACY and request.path.startswith("/api/budget/"):
            return _json(HTTPStatus.FORBIDDEN, {"error": {"code": "budget_forbidden"}})
        return _empty(HTTPStatus.FORBIDDEN)
    if (
        auth_state.session_phase == store_sql.SESSION_PHASE_V2
        and (request.method, request.path) in _CORE_BROWSER_ROUTES
    ):
        if carrier != "cookie":
            return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "browser_session_required"})
        if envelope.version != 2:
            return _json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "browser_session_upgrade_required"})
        if (request.method, request.path) in _CORE_BROWSER_MUTATION_ROUTES:
            origin_err = _check_core_origin(request)
            if origin_err is not None:
                return origin_err
    return await handler(request)


@web.middleware
async def _empty_404_405_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPMethodNotAllowed as exc:
        return _empty(HTTPStatus.METHOD_NOT_ALLOWED, [("Allow", exc.headers.get("Allow", ""))])
    except web.HTTPNotFound:
        return _empty(HTTPStatus.NOT_FOUND)


def _rt(request):
    return request.app[_RT_KEY]


async def _login(request):
    op, err = await _read_json_body(request)      # CSRF guard: requires Content-Type: application/json
    if err:
        return op
    user = op.get("user") if isinstance(op, dict) else None
    password = op.get("password") if isinstance(op, dict) else None
    native = isinstance(op, dict) and op.get("bearer") is True
    snapshot = None
    if _AUTH_ENABLED and isinstance(user, str) and isinstance(password, str):
        # Same source resolution as the device login, so both share one bucket per client and a
        # malformed forwarded chain cannot collapse callers onto the trusted proxy's address.
        try:
            source = consent_context.login_ip(request)
        except (ValueError, BudgetConfigError):
            return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid request"})
        retry = request.app[_LOGIN_LIMITER_KEY].take(user, source, time.monotonic())
        if retry is not None:
            response = _json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "login rate limited"})
            response.headers["Retry-After"] = str(retry)
            return response
        try:
            snapshot = await asyncio.to_thread(store_sql.load_login_snapshot, user)
        except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
            return _incompatible_bridge_response()
        users_map = {} if snapshot.password_hash is None else {snapshot.username: snapshot.password_hash}
        authenticated = await asyncio.to_thread(lambda: auth.authenticate(user, password, users_map))
    else:
        authenticated = False
    if not authenticated:
        _audit(_rt(request), user if isinstance(user, str) else "?", "login", result="invalid")
        return _json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid credentials"})
    if snapshot.backend == "sqlite":
        try:
            issued = await asyncio.to_thread(
                lambda: store_sql.finish_sqlite_login(
                    snapshot, native=native, now=_now(), secret=_SESSION_SECRET
                )
            )
        except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
            return _incompatible_bridge_response()
        if issued is None:
            _audit(_rt(request), user, "login", result="invalid")
            return _json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid credentials"})
        token = issued.token
        active_v2 = issued.session_phase == store_sql.SESSION_PHASE_V2
    else:
        token = auth.make_session(user, now=_now(), secret=_SESSION_SECRET)
        active_v2 = False
    body = {"ok": True, "user": user}
    if native:
        body["token"] = token
    resp = _json(HTTPStatus.OK, body)
    if not (active_v2 and native):
        try:
            await consent_api.auth_transition(request, resp)
        except Exception as exc:
            return consent_api._failure(exc, "login_invalidation")
    _audit(_rt(request), user, "login", result="ok")
    if not (active_v2 and native):
        resp.set_cookie(_SESSION_COOKIE, token, max_age=int(auth.SESSION_TTL), httponly=True,
                        samesite="Strict", secure=_COOKIE_SECURE, path="/")
    return resp


async def _logout(request):
    # Not audited: /api/logout is public (no session lookup in the middleware), so the actor would
    # always be "anonymous" — logout is low-value to attribute. Login (success/failure) is audited.
    resp = _json(HTTPStatus.OK, {"ok": True})
    try:
        await consent_api.auth_transition(request, resp)
    except Exception as exc:
        return consent_api._failure(exc, "logout_invalidation")
    resp.del_cookie(_SESSION_COOKIE, path="/")
    return resp


async def _whoami(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    # The role is resolved once by the auth middleware (request[_ROLE_KEY]); both are None when auth is
    # off (loopback/dev), which the TS app reads as "fully open, no role gating".
    return _json(HTTPStatus.OK, {"user": request.get(_USER_KEY), "role": request.get(_ROLE_KEY)})


def _legacy_role_permissions(role):
    return {
        "viewer": store_sql._LEGACY_VIEWER_GRANTS,
        "operator": store_sql._LEGACY_OPERATOR_GRANTS,
        "admin": store_sql._LEGACY_ADMIN_GRANTS,
    }.get(role, frozenset())


def _capability_payload(actor: str, state: store_sql.AuthState) -> dict:
    if state.backend == "json":
        installation = _legacy_role_permissions(state.role)
        own = {
            "permissions": sorted(store_sql._LEGACY_SELF_BUDGET_GRANTS),
            "can_view": False, "can_use_receipts": False, "can_manage_devices": False,
        } if state.role == _ADMIN else None
        return {
            "user": actor, "actor_tag": budget_api.actor_digest(actor),
            "installation_permissions": sorted(installation), "own_budget": own,
            "delegated_receipts": None, "household_available": False,
        }

    budget_enabled = os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") == "1"
    receipts_enabled = budget_enabled and device_token.receipts_enabled()
    household_enabled = budget_enabled and os.environ.get("HESTIA_BUDGET_ANALYTICS_ENABLED") == "1"
    with store_sql.authorization_snapshot() as session:
        authoritative = session.get(AppMeta, "users_authoritative") is not None
        delegated = None
        if state.policy_phase == authz.POLICY_PHASE_LEGACY:
            installation = _legacy_role_permissions(state.role)
            legacy_owner = authoritative and state.role == _ADMIN
            own_permissions = store_sql._LEGACY_SELF_BUDGET_GRANTS if legacy_owner else frozenset()
            own = {
                "permissions": sorted(own_permissions),
                "can_view": legacy_owner and budget_enabled,
                "can_use_receipts": legacy_owner and receipts_enabled,
                "can_manage_devices": legacy_owner and receipts_enabled,
            } if legacy_owner else None
        else:
            installation = authz.effective_permissions(session, actor)
            active_owner = authz.is_active_budget_owner(session, actor)
            own_permissions = (
                authz.effective_permissions(session, actor, owner=actor)
                if active_owner else frozenset()
            )
            own = {
                "permissions": sorted(own_permissions),
                "can_view": budget_enabled and "budget.view" in own_permissions,
                "can_use_receipts": receipts_enabled and bool(own_permissions & authz.LEGACY_RECEIPT_PERMISSIONS),
                "can_manage_devices": receipts_enabled,
            } if active_owner else None
            delegated_permissions = authz.delegated_receipt_permissions(session, actor)
            delegated = {
                "permissions": sorted(delegated_permissions), "can_use": receipts_enabled,
            } if delegated_permissions else None
        return {
            "user": actor,
            "actor_tag": budget_api.actor_digest(actor),
            "installation_permissions": sorted(installation),
            "own_budget": own,
            "delegated_receipts": delegated,
            "household_available": household_enabled and authz.household_available(session, actor),
        }


async def _me_capabilities(request):
    actor = request.get(_USER_KEY)
    if actor is None:
        installation = sorted(
            pid for pid, spec in authz.CATALOG.items()
            if spec.storage_scope == "installation" and not spec.protected
        )
        payload = {
            "user": None, "actor_tag": None, "installation_permissions": installation, "own_budget": None,
            "delegated_receipts": None, "household_available": False,
        }
    else:
        try:
            state = await asyncio.to_thread(store_sql.load_auth_state, actor)
            if not state.enabled:
                return _empty(HTTPStatus.UNAUTHORIZED)
            payload = await asyncio.to_thread(_capability_payload, actor, state)
        except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
            return _incompatible_bridge_response()
    response = _json(HTTPStatus.OK, payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def _qr_error(status: int, code: str, *, retry: "int | None" = None):
    response = _json(status, {"error": {"code": code}})
    response.headers.update(budget_api.PRIVATE_HEADERS)
    if retry is not None:
        response.headers["Retry-After"] = str(retry)
    return response


def _qr_configured_origin() -> "str | None":
    return _canonical_https_origin(os.environ.get("HESTIA_PUBLIC_ORIGIN"), allow_trailing_slash=True)


def _native_auth_available() -> bool:
    if not _AUTH_ENABLED or os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        return False
    try:
        with store_sql.authorization_snapshot() as session:
            return (
                store_sql.auth_session_phase(session) == store_sql.SESSION_PHASE_V2
                and session.get(AppMeta, store_sql._USERS_AUTHORITATIVE) is not None
            )
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return False


async def _qr_code(request):
    configured = _qr_configured_origin()
    if configured is None:
        return _qr_error(HTTPStatus.SERVICE_UNAVAILABLE, "auth_origin_unavailable")
    body, error = await _read_json_body(request, reject_duplicates=True)
    if error:
        return body
    supplied = _canonical_https_origin(body.get("origin")) if isinstance(body, dict) and set(body) == {"origin"} else None
    origins = request.headers.getall("Origin", [])
    if supplied != configured or len(origins) != 1 or _canonical_https_origin(origins[0]) != configured:
        return _qr_error(HTTPStatus.BAD_REQUEST, "qr_origin_mismatch")
    ctx = request[_SESSION_CONTEXT_KEY]
    try:
        payload = await asyncio.to_thread(
            auth_qr.issue, ctx.username, configured, ctx.generation, now=_now(), secret=_SESSION_SECRET,
        )
    except auth_qr.QrLoginError as exc:
        return _qr_error(exc.status, exc.code)
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return _incompatible_bridge_response()
    response = _json(HTTPStatus.OK, {"qr_payload": payload})
    response.headers.update(budget_api.PRIVATE_HEADERS)
    return response


async def _qr_redeem(request):
    if "Cookie" in request.headers or "Authorization" in request.headers:
        return _qr_error(HTTPStatus.BAD_REQUEST, "qr_auth_ambiguous")
    configured = _qr_configured_origin()
    if configured is None:
        return _qr_error(HTTPStatus.SERVICE_UNAVAILABLE, "auth_origin_unavailable")
    retry = auth_qr.redeem_limited(device_token.client_ip(request), time.monotonic())
    if retry is not None:
        return _qr_error(HTTPStatus.TOO_MANY_REQUESTS, "qr_rate_limited", retry=retry)
    body, error = await _read_json_body(request, reject_duplicates=True)
    if error:
        return body
    if not isinstance(body, dict) or set(body) != {"origin", "code"}:
        return _qr_error(HTTPStatus.UNAUTHORIZED, "qr_code_invalid")
    supplied = _canonical_https_origin(body["origin"])
    if supplied != configured:
        return _qr_error(HTTPStatus.BAD_REQUEST, "qr_origin_mismatch")
    code = auth_qr.normalize_code(body["code"])
    if code is None:
        return _qr_error(HTTPStatus.UNAUTHORIZED, "qr_code_invalid")
    retry = auth_qr.redeem_global_limited(time.monotonic())
    if retry is not None:
        return _qr_error(HTTPStatus.TOO_MANY_REQUESTS, "qr_rate_limited", retry=retry)
    try:
        result = await asyncio.to_thread(auth_qr.redeem, configured, code, now=_now(), secret=_SESSION_SECRET)
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return _incompatible_bridge_response()
    if result is None:
        return _qr_error(HTTPStatus.UNAUTHORIZED, "qr_code_invalid")
    response = _json(HTTPStatus.OK, result)
    response.headers.update(budget_api.PRIVATE_HEADERS)
    return response


async def _device_exchange(request):
    ctx = request.get(_SESSION_CONTEXT_KEY)
    if (
        ctx is None or ctx.carrier != "bearer" or ctx.version != 2
        or ctx.purpose != auth.SESSION_PURPOSE_NATIVE
        or ctx.session_phase != store_sql.SESSION_PHASE_V2
    ):
        return _qr_error(HTTPStatus.UNAUTHORIZED, "native_session_required")
    if not device_token.budget_http_enabled():
        return _qr_error(HTTPStatus.NOT_FOUND, "budget_disabled")
    if not device_token.receipts_enabled():
        return _qr_error(HTTPStatus.NOT_FOUND, "budget_receipts_disabled")
    body, error = await _read_json_body(request, 4096, reject_duplicates=True)
    if error:
        return body
    if not isinstance(body, dict) or not set(body) <= {"device_name", "prior_token"} or "device_name" not in body:
        return _qr_error(HTTPStatus.BAD_REQUEST, "budget_invalid_request")
    prior = body.get("prior_token")
    if prior is not None and (not isinstance(prior, str) or not prior):
        return _qr_error(HTTPStatus.BAD_REQUEST, "budget_invalid_request")
    try:
        result = await asyncio.to_thread(
            lambda: device_token.open_store(session_secret=_SESSION_SECRET).exchange(
                ctx.username, body["device_name"], prior, generation=ctx.generation,
            ),
        )
    except device_token.DeviceTokenError as exc:
        return _qr_error(exc.status, exc.code)
    except BudgetUsersBackendRequired:
        return _qr_error(HTTPStatus.FORBIDDEN, "budget_forbidden")
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return _incompatible_bridge_response()
    response = _json(HTTPStatus.OK, result)
    response.headers.update(budget_api.PRIVATE_HEADERS)
    return response


async def _auth_features(request):
    calendar_instances = any(
        route.method == "GET" and route.resource.canonical == "/api/calendars/instances"
        for route in request.app.router.routes()
    )
    native_auth = _native_auth_available()
    qr_origin = _qr_configured_origin() if native_auth else None
    features = ["capabilities"]
    if qr_origin is not None:
        features.append("qr_login")
    if native_auth and device_token.budget_http_enabled() and device_token.receipts_enabled():
        features.extend(["device_exchange", "budget_native_read"])
    if calendar_instances:
        features.append("calendar_instances")
    response = _json(HTTPStatus.OK, {
        "auth_contract": 2, "features": features, "qr_origin": qr_origin,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


async def _audit_feed(request):
    """GET /api/audit — recent audit-log rows (newest first), auth-gated like every /api/* route."""
    rt = _rt(request)
    if rt.audit_engine is None:                  # audit backend not wired (bare runtime) → empty feed
        return _json(HTTPStatus.OK, {"events": []})
    events = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.recent_audit(rt.audit_engine))
    return _json(HTTPStatus.OK, {"events": events})


async def _rf433_feed(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    """GET /api/rf433 — every 433 MHz device hestia has decoded (discovery), newest-seen first;
    auth-gated like every /api/* route. Display-only; empty until the local-433 feeder is running."""
    return _json(HTTPStatus.OK, {"devices": _rt(request).rf433.snapshot()})


async def _db_stats(_request):
    """GET /api/db/stats — SQLite file size + row counts, auth-gated like every /api/* route."""
    stats = await asyncio.get_running_loop().run_in_executor(None, store_sql.db_stats)
    return _json(HTTPStatus.OK, stats)


async def _settings(request):
    """GET /api/settings — persisted UI settings for the logged-in user, when SQLite owns prefs."""
    user = request.get(_USER_KEY)
    if user is None:
        return _json(HTTPStatus.OK, dict(_EMPTY_SETTINGS))
    settings = await asyncio.get_running_loop().run_in_executor(None, lambda: store_sql.get_user_settings(user))
    return _json(HTTPStatus.OK, settings if settings is not None else dict(_EMPTY_SETTINGS))


def _settings_error(body) -> "str | None":
    if not isinstance(body, dict):
        return _BODY_NOT_OBJECT
    locale = body.get("locale")
    if locale is not None and not (isinstance(locale, str) and len(locale) <= 35):
        return "locale must be a string ≤ 35 chars"
    scale = body.get("temp_scale")
    if scale is not None and not (isinstance(scale, str) and scale in _TEMP_SCALES):
        return "temp_scale must be one of C, F, K"
    theme = body.get("theme")
    if theme is not None and not isinstance(theme, str):
        return "theme must be a string or null"
    if "preferred_languages" in body:
        try:
            validate_languages(body["preferred_languages"])
        except ValueError:
            return "preferred_languages must be an ordered unique list of supported languages or null"
    return None


def _settings_update_fields(body) -> dict:
    # Pass only the fields actually present so the store's single-transaction upsert preserves the
    # rest (no read-merge-write race between two concurrent partial POSTs).
    return {k: body[k] for k in ("locale", "temp_scale", "theme", "preferred_languages") if k in body}


async def _persist_user_settings(request, user: str, body: dict) -> None:
    fields = _settings_update_fields(body)
    wrote = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.set_user_settings(user, **fields))
    if wrote:
        _audit(_rt(request), user, "settings",
               detail=f"locale={body.get('locale')},scale={body.get('temp_scale')}")


async def _settings_set(request):
    """POST /api/settings — best-effort server sync for local-first UI preferences."""
    body, err = await _read_json_body(request)
    if err:
        return body
    error = _settings_error(body)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})

    user = request.get(_USER_KEY)
    if user is not None:
        await _persist_user_settings(request, user, body)
    return _json(HTTPStatus.OK, {"ok": True})


_WHOLE_HOME_FIELDS = {"node", "exclude", "ep"}   # allowlist for POST /api/whole-home (ep = one gang)


def _excluded_endpoints(entry: dict) -> "list[int]":
    """The gang numbers this node has opted out (truthy ``endpoint_exclude`` keys), sorted."""
    return sorted(int(k) for k, v in (entry.get("endpoint_exclude") or {}).items() if v)


async def _whole_home(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    """GET /api/whole-home — what is opted out of the house-wide "all lights / all blinds" sweeps:
    whole nodes (``excluded_nodes``) and single gangs of multi-gang switches (``excluded_endpoints``,
    node → gang numbers). Registry-only: deliberately kept OFF the DeviceInfo wire shape (so adding
    it never breaks a strictly-decoding pinned native client); the admin panel reads it here. Admin."""
    nodes = _rt(request).registry.nodes
    excluded = sorted(int(n) for n, e in nodes.items() if n.isdigit() and e.get("exclude_from_all"))
    endpoints = {n: eps for n, e in nodes.items() if n.isdigit() and (eps := _excluded_endpoints(e))}
    return _json(HTTPStatus.OK, {"excluded_nodes": excluded, "excluded_endpoints": endpoints})


def _whole_home_error(body) -> "str | None":
    if not isinstance(body, dict):
        return _BODY_NOT_OBJECT
    unknown = set(body) - _WHOLE_HOME_FIELDS
    if unknown:
        return f"unknown field(s): {sorted(unknown)}"
    node = body.get("node")
    if not isinstance(node, int) or isinstance(node, bool) or not 0 <= node <= 255:
        return "node must be an integer 0..255"
    if not isinstance(body.get("exclude"), bool):
        return "exclude must be a boolean"
    ep = body.get("ep")
    if ep is not None and (not isinstance(ep, int) or isinstance(ep, bool) or ep not in (1, 2)):
        return "ep must be the integer 1 or 2"      # one gang of a 2-gang switch (same as /api/control)
    return None


async def _whole_home_set(request):
    """POST /api/whole-home — opt one device (or, with ``ep``, one GANG of a multi-gang switch) in
    (exclude=false) / out (exclude=true) of the house-wide "all" sweeps. Admin. Registry-only, so it
    never changes the DeviceInfo wire shape."""
    body, err = await _read_json_body(request)
    if err:
        return body
    error = _whole_home_error(body)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
    op = {"op": "whole_home_set", "node": body["node"], "exclude": body["exclude"]}
    if body.get("ep") is not None:
        op["ep"] = body["ep"]
    return await _dispatch_op(_rt(request), op, actor=_actor(request))


async def _room_icons(_request):
    """GET /api/rooms/icons — shared per-room emoji map from SQLite AppMeta."""
    icons = await asyncio.get_running_loop().run_in_executor(None, store_sql.get_room_icons)
    return _json(HTTPStatus.OK, icons)


def _room_icon_error(body) -> "str | None":
    if not isinstance(body, dict):
        return _BODY_NOT_OBJECT
    room = body.get("room")
    if not isinstance(room, str) or len(room) > 64:
        return "room must be a string ≤ 64 chars"
    icon = body.get("icon")
    if not isinstance(icon, str) or len(icon) > 16:
        return "icon must be a string ≤ 16 chars"
    return None


async def _persist_room_icon(request, user: str, body: dict) -> None:
    wrote = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.set_room_icon(body["room"], body["icon"]))
    if wrote:
        _audit(_rt(request), user, "room_icon", target=body["room"])


async def _room_icon_set(request):
    """POST /api/rooms/icons — set/clear one shared room emoji, best-effort."""
    body, err = await _read_json_body(request)
    if err:
        return body
    error = _room_icon_error(body)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})

    user = request.get(_USER_KEY)
    if user is not None:
        await _persist_room_icon(request, user, body)
    return _json(HTTPStatus.OK, {"ok": True})


# ---- user management (#PR-D): own-password change + admin user administration -------------------
# The SERVER is the security boundary. Floors: /api/me/password = any signed-in user (verifies the
# current password); every /api/users* route = admin (the middleware enforces it before these run).
# All require the SQLite users backend (roles/disable live there) → 409 otherwise. Self-targeting is
# blocked on the admin verbs so an admin can't lock themselves out or bypass the current-password check.

_MIN_PASSWORD = 8
_MAX_PASSWORD = 1024


def _username_error(username) -> "str | None":
    """Validate a username for account creation. ``|`` is forbidden because the session token is
    ``username|expiry`` (a ``|`` would corrupt parsing); ``/`` mirrors the JSON-store CLI guard."""
    if not isinstance(username, str) or not username or len(username) > 64 or "|" in username or "/" in username:
        return "invalid username (non-empty, ≤ 64 chars, no '|' or '/')"
    return None


def _password_error(password) -> "str | None":
    if not isinstance(password, str) or len(password) < _MIN_PASSWORD:
        return f"password must be a string of at least {_MIN_PASSWORD} characters"
    if len(password) > _MAX_PASSWORD:
        return f"password must be ≤ {_MAX_PASSWORD} characters"
    return None


async def _users_backend_ready() -> bool:
    """True when SQLite owns the users store (roles + disable + DB writes are meaningful)."""
    return await asyncio.get_running_loop().run_in_executor(None, store_sql.users_db_authoritative)


def _requires_sqlite():
    """A FRESH 409 response (a Response can't be reused across requests) for user-management endpoints
    hit while the JSON backend is active — roles / disable / DB writes only exist under SQLite."""
    return _json(HTTPStatus.CONFLICT,
                 {"ok": False, "error": "user management requires the SQLite backend"})


def _user_outcome(outcome: str):
    """Map a store mutation outcome to an error response, or ``None`` when it succeeded (``"ok"``)."""
    if outcome == "ok":
        return None
    if outcome == "not_found":
        return _json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "no such user"})
    if outcome == "exists":
        return _json(HTTPStatus.CONFLICT, {"ok": False, "error": "a user with that name already exists"})
    if outcome == "last_admin":
        return _json(HTTPStatus.CONFLICT,
                     {"ok": False, "error": "refused: that would leave no enabled admin"})
    if outcome == "legacy_role_changes_disabled":
        return _json(HTTPStatus.CONFLICT, {"ok": False, "error": "legacy_role_changes_disabled"})
    return _json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "unexpected error"})


async def _me_password(request):
    """POST /api/me/password — the signed-in user changes their OWN password. Verifies the CURRENT
    password (so a stolen cookie alone can't change it) before writing the new scrypt hash."""
    body, err = await _read_json_body(request)
    if err:
        return body
    user = request.get(_USER_KEY)
    if user is None:                                   # auth-off / no session → nothing to change
        return _json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "not signed in"})
    current = body.get("current") if isinstance(body, dict) else None
    new = body.get("new") if isinstance(body, dict) else None
    perror = _password_error(new)
    if perror is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": perror})
    if not isinstance(current, str):
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "current password is required"})
    loop = asyncio.get_running_loop()
    if not await _users_backend_ready():
        return _requires_sqlite()
    snapshot = await loop.run_in_executor(None, lambda: store_sql.load_login_snapshot(user))
    users_map = {} if snapshot.password_hash is None else {snapshot.username: snapshot.password_hash}
    verified = await loop.run_in_executor(
        None, lambda: auth.authenticate(user, current, users_map))
    if not verified:
        _audit(_rt(request), user, "password", result="invalid")
        return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "current password is incorrect"})
    new_hash = await loop.run_in_executor(None, lambda: auth.hash_password(new))
    wrote = await loop.run_in_executor(
        None,
        lambda: store_sql.set_user_password(
            user,
            new_hash,
            expected_hash=snapshot.password_hash,
            expected_generation=snapshot.generation,
        ),
    )
    if not wrote:                                      # account vanished between verify and write
        return _json(HTTPStatus.CONFLICT, {"ok": False, "error": "account no longer exists"})
    _audit(_rt(request), user, "password", result="ok")
    return _json(HTTPStatus.OK, {"ok": True})


async def _users_list(request):
    """GET /api/users — admin: every account's username / role / disabled (NEVER the password hash)."""
    if not await _users_backend_ready():
        return _requires_sqlite()
    users = await asyncio.get_running_loop().run_in_executor(None, store_sql.list_users)
    return _json(HTTPStatus.OK, {"users": users})


async def _users_add(request):
    """POST /api/users — admin: create a new account {username, password, role}. 409 on a duplicate."""
    body, err = await _read_json_body(request)
    if err:
        return body
    username = body.get("username") if isinstance(body, dict) else None
    password = body.get("password") if isinstance(body, dict) else None
    role = body.get("role") if isinstance(body, dict) else None
    for problem in (_username_error(username), _password_error(password),
                    None if role in store_sql.ROLE_RANK else "role must be admin, operator or viewer"):
        if problem is not None:
            return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": problem})
    loop = asyncio.get_running_loop()
    if not await _users_backend_ready():
        return _requires_sqlite()
    new_hash = await loop.run_in_executor(None, lambda: auth.hash_password(password))
    outcome = await loop.run_in_executor(None, lambda: store_sql.add_user(username, new_hash, role))
    failed = _user_outcome(outcome)
    if failed is not None:
        return failed
    _audit(_rt(request), _actor(request), "user_add", target=username, detail=f"role={role}")
    return _json(HTTPStatus.OK, {"ok": True})


async def _users_role(request):
    """POST /api/users/role — admin: change another user's role. Refuses self (no self-demote) and the
    last enabled admin (store-guarded)."""
    body, err = await _read_json_body(request)
    if err:
        return body
    username = body.get("username") if isinstance(body, dict) else None
    role = body.get("role") if isinstance(body, dict) else None
    if not isinstance(username, str) or not username:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": _USERNAME_REQUIRED})
    if role not in store_sql.ROLE_RANK:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "role must be admin, operator or viewer"})
    if username == request.get(_USER_KEY):
        return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "you cannot change your own role"})
    if not await _users_backend_ready():
        return _requires_sqlite()
    outcome = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.set_user_role(username, role))
    failed = _user_outcome(outcome)
    if failed is not None:
        return failed
    _audit(_rt(request), _actor(request), "user_role", target=username, detail=f"role={role}")
    return _json(HTTPStatus.OK, {"ok": True})


async def _users_disabled(request):
    """POST /api/users/disabled — admin: enable/disable another account. Refuses self (no self-lockout)
    and the last enabled admin (store-guarded)."""
    body, err = await _read_json_body(request)
    if err:
        return body
    username = body.get("username") if isinstance(body, dict) else None
    disabled = body.get("disabled") if isinstance(body, dict) else None
    if not isinstance(username, str) or not username:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": _USERNAME_REQUIRED})
    if not isinstance(disabled, bool):
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "disabled must be true or false"})
    if username == request.get(_USER_KEY):
        return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "you cannot disable your own account"})
    if not await _users_backend_ready():
        return _requires_sqlite()
    outcome = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.set_user_disabled(username, disabled))
    failed = _user_outcome(outcome)
    if failed is not None:
        return failed
    _audit(_rt(request), _actor(request), "user_disable" if disabled else "user_enable", target=username)
    return _json(HTTPStatus.OK, {"ok": True})


async def _users_reset_password(request):
    """POST /api/users/reset-password — admin: set a new password for ANOTHER user (no current-password
    check). Refuses self: an admin changing their OWN password must use /api/me/password (which verifies
    the current one), so a hijacked admin session can't silently rotate its own credential here."""
    body, err = await _read_json_body(request)
    if err:
        return body
    username = body.get("username") if isinstance(body, dict) else None
    new = body.get("new") if isinstance(body, dict) else None
    if not isinstance(username, str) or not username:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": _USERNAME_REQUIRED})
    perror = _password_error(new)
    if perror is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": perror})
    if username == request.get(_USER_KEY):
        return _json(HTTPStatus.FORBIDDEN,
                     {"ok": False, "error": "use change-password for your own account"})
    loop = asyncio.get_running_loop()
    if not await _users_backend_ready():
        return _requires_sqlite()
    new_hash = await loop.run_in_executor(None, lambda: auth.hash_password(new))
    wrote = await loop.run_in_executor(None, lambda: store_sql.set_user_password(username, new_hash))
    if not wrote:
        return _json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "no such user"})
    _audit(_rt(request), _actor(request), "user_password", target=username)
    return _json(HTTPStatus.OK, {"ok": True})


def _clamp_cover(value: int) -> int:
    """Clamp a whole-home blind position to the wire ``cover`` range 0-99. The perceptual %→wire mapping
    lives in the UI (its blind-scale curve), so ``blinds_set`` already carries a wire value — here we only
    guard the bounds before fanning it out as ``cover`` ops."""
    return min(99, max(0, value))


def _scene_device_op(target_type: str, node_id: int, info: dict, active: bool, cover: "int | None" = None) -> dict:
    """One per-device control op for a scene, mirroring the per-device UI buttons: a dimmable light
    (has a level) → ``level`` 0/99; a plain light → ``switch`` off/on; a blind → ``cover`` 0/99. When
    ``cover`` is given (``blinds_set``) it is the blind's explicit wire position instead of the extreme."""
    if target_type == "light" and info.get("level") is None:
        return {"op": "switch", "node": node_id, "on": active}
    op = "level" if target_type == "light" else "cover"
    value = cover                       # explicit blinds_set position, when given
    if value is None:
        value = 99 if active else 0     # else the on/off extreme
    return {"op": op, "node": node_id, "value": value}


def _scene_node_ops(target_type: str, node_id: int, info: dict, active: bool, entry: dict,
                    cover: "int | None" = None) -> "list[dict]":
    """The scene ops for ONE device. A plain switch light whose registry entry opts single GANGS out
    (``endpoint_exclude``) is driven per-gang — one endpoint-addressed switch op per NON-excluded gang
    (the same op the room UI's per-gang buttons post) — so e.g. a 2-gang hall/nightlight node keeps the
    nightlight untouched. Every other device keeps today's single node-level op (one frame). ``cover``
    (blinds_set position) only ever reaches the single-op path — blinds have no per-gang opt-out."""
    excluded = {int(k) for k, v in (entry.get("endpoint_exclude") or {}).items() if v}
    if target_type != "light" or info.get("level") is not None or not excluded:
        return [_scene_device_op(target_type, node_id, info, active, cover)]
    # Any truthy opt-out takes THIS branch unconditionally — the node-level frame (which would drive
    # every gang at once) is never used for such a node. Gangs we can't enumerate (live gang state ∪
    # labels) are therefore deliberately skipped, not swept: fail-safe in the excluded gang's favour.
    eps = {int(k) for k in info.get("endpoints") or {}}
    eps |= {int(k) for k in entry.get("endpoint_names") or {}}
    return [{"op": "switch", "node": node_id, "on": active, "endpoint": ep}
            for ep in sorted(eps - excluded)]


def _scene_ops(rt, scene: str, value: "int | None" = None) -> list[dict]:
    """Expand a house-wide scene into ordinary per-device control ops. ``blinds_set`` drives every blind
    to the explicit wire ``value`` (0-99); the other scenes drive their device type to an on/off extreme."""
    if scene == _BLINDS_SET:
        target_type, active, cover = "blind", None, _clamp_cover(value or 0)
    else:
        target_type, active = _SCENE_TARGETS[scene]
        cover = None
    # The house-wide sweep skips any device an admin opted out of "all" (e.g. a nightlight), so
    # SceneResult.total reflects only the devices the scene targets. The opt-out is read straight from
    # the registry (NOT from discovery) so the DeviceInfo wire shape stays stable for native clients.
    reg = rt.registry.nodes
    return [op
            for node, info in _merged_discovery(rt).items()
            if info.get("type") == target_type and not reg.get(node, {}).get("exclude_from_all")
            for op in _scene_node_ops(target_type, int(node), info, active, reg.get(node, {}), cover)]


async def _scene(request):
    """POST /api/scene — fan out one house-wide scene through the normal per-device control path."""
    body, err = await _read_json_body(request)
    if err:
        return body
    scene = body.get("op") if isinstance(body, dict) else None
    if scene not in _SCENE_NAMES:
        return _json(HTTPStatus.BAD_REQUEST,
                     {"ok": False, "error": f"op must be one of {', '.join(_SCENE_NAMES)}"})

    value = None
    if scene == _BLINDS_SET:
        value = body.get("value")
        # bool is an int subclass — reject it so `true`/`false` can't masquerade as a position. The value
        # is a wire `cover` position 0-99 (the UI applies the perceptual blind-scale curve before sending).
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 99:
            return _json(HTTPStatus.BAD_REQUEST,
                         {"ok": False, "error": "blinds_set requires an integer value 0-99"})

    rt = _rt(request)
    ops = _scene_ops(rt, scene, value)
    sent = 0
    for i, op in enumerate(ops):
        if i > 0 and INJECT_GAP_SECS > 0:
            await asyncio.sleep(INJECT_GAP_SECS)   # pace the fan-out so a big sweep doesn't wedge the gateway
        resp = await process_control_op(rt, op)
        if resp.get("ok"):
            sent += 1
    total = len(ops)
    _audit(rt, _actor(request), "scene", target=scene, result=f"{sent}/{total}")
    return _json(HTTPStatus.OK, {"ok": True, "sent": sent, "total": total})


async def _ui_redirect(_request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    # The app moved from /ui/ to the root; keep the old bookmark working. A RELATIVE target ("../") so the
    # redirect is correct whether hestia is served at a bare root or behind a reverse-proxy subpath (/hestia/).
    raise web.HTTPFound("../")


def _ui_dist_dir() -> Path:
    return Path(os.environ.get(_UI_DIST_ENV, str(_DEFAULT_UI_DIST)))


def _ui_built_file_response(path: Path):
    # 404 unless the UI is built (index.html present) AND the requested file exists — an asset not in
    # the bundle (or any request before `npm run build`) must 404, never hand a missing path to FileResponse.
    if not (_ui_dist_dir() / "index.html").is_file() or not path.is_file():
        raise web.HTTPNotFound
    return web.FileResponse(path)


async def _ui_index(_request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    return _ui_built_file_response(_ui_dist_dir() / "index.html")


async def _ui_asset(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    return _ui_built_file_response(_ui_dist_dir() / "assets" / request.match_info["path"])


async def _ui_document(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    # Exact registered routes select fixed filenames; no arbitrary dist file is public.
    response = _ui_built_file_response(_ui_dist_dir() / _PUBLIC_DOCUMENTS[request.path])
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def _discovery(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    rt = _rt(request)
    devices = _merged_discovery(rt)
    globs = globals_snapshot(rt.state)
    # mode = the RUNNING server; target_mode = the PERSISTED registry mode (Phase-3 graduation,
    # applied on next restart); env_override = HESTIA_MODE if it is pinning the mode (else null).
    return _json(HTTPStatus.OK,
                 {"devices": devices, "summary": _summary(devices), "globals": globs,
                  "ir_buttons": IR_BUTTONS, "klima": KLIMA, "klima_state": rt.state.klima,
                  "rule_vocab": rule_vocab(),
                  "mode": rt.mode, "target_mode": rt.registry.mode,
                  "env_override": os.environ.get("HESTIA_MODE")})


async def _events(request):
    """Server-Sent Events stream — pushes `activity` (`[1e 09]` frame
    → row flash) and `discovery_changed` (name op / classifier update
    → UI re-fetch). Heatmap UX for onboarding: operator wiggles a
    switch, the row lights up, they know which to name."""
    sub = await _rt(request).event_bus.try_subscribe()
    if sub is None:                            # cap reached or bus closing
        return _empty(HTTPStatus.TOO_MANY_REQUESTS)

    resp = web.StreamResponse(
        status=HTTPStatus.OK,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    try:
        await resp.prepare(request)
        deadline = _clock() + SSE_MAX_LIFETIME
        next_authz_check = _clock() + SSE_IDLE_TIMEOUT
        session_context = request.get(_SESSION_CONTEXT_KEY)
        while _clock() < deadline:
            event = await _wait_event(sub.queue)
            if session_context is not None and _clock() >= next_authz_check:
                current = await asyncio.to_thread(_session_context_current, session_context)
                if not current:
                    break
                next_authz_check = _clock() + SSE_IDLE_TIMEOUT
            if event is _CLOSED_SENTINEL:
                break                          # graceful shutdown
            payload = (b":keepalive\n\n" if event is None
                       else f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            try:
                await resp.write(payload)
            except (ConnectionResetError, ClientConnectionError, asyncio.CancelledError):
                break                          # browser EventSource auto-reconnects
    finally:
        sub.close()
    return resp


def _unique_json_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON fields")
        result[key] = value
    return result


async def _read_json_body(request, max_body=MAX_BODY, *, reject_duplicates=False):
    """Read and JSON-parse the request body, enforcing framing limits. On any
    content-type/framing/size/parse error this returns ``(response, True)``;
    on success returns ``(obj, False)``. A zero-length body parses to ``{}``
    (preserving the original ``/api/name`` contract); ``max_body`` caps the byte
    size per endpoint (names are small, rules larger). The declared length is
    read with ``readexactly`` under ``BODY_READ_TIMEOUT``: a stall is 408, and
    a short read or a reset connection is 400 rather than a partial parse. A
    half-closed peer is already gone, so that 400 may never be written; the
    handler still returns it instead of raising or waiting out the timeout.

    Requiring ``Content-Type: application/json`` is the CSRF guard for these
    device-controlling mutations: it is NOT a CORS "simple" content type, so a
    cross-origin browser request must clear a preflight first — which this server
    never grants — blocking a malicious page from POSTing a forged body (e.g. a
    time rule that actuates devices) to the loopback UI or the auth'd reverse proxy.
    First-party fetches already send it; non-browser clients set it trivially."""
    ctype = request.headers.get("Content-Type", "")
    if ctype.split(";")[0].strip().lower() != "application/json":
        return _json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                     {"ok": False, "error": "Content-Type must be application/json"}), True
    cl_raw = request.headers.get("Content-Length")
    if cl_raw is None:
        return _json(HTTPStatus.LENGTH_REQUIRED,
                     {"ok": False, "error": "Content-Length required"}), True
    cl = int(cl_raw)
    if cl > max_body:
        return _json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                     {"ok": False, "error": f"body must be ≤ {max_body} bytes"}), True
    # Content-Length 0 is an empty payload (EmptyStreamReader.readexactly raises).
    # Keep the historical contract: a zero-length body parses as {}.
    try:
        body = b"" if cl <= 0 else await asyncio.wait_for(
            request.content.readexactly(cl), timeout=BODY_READ_TIMEOUT)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        # IncompleteReadError: payload ended early and the transport can still
        # take a response. ConnectionResetError: the peer dropped the socket
        # (including a TCP half-close), often after aiohttp has already closed
        # the transport. Return 400 either way so the handler does not become
        # a 500 or sit in the read until BODY_READ_TIMEOUT. Do not patch
        # RequestHandler to keep a half-closed socket open.
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete body"}), True
    except asyncio.TimeoutError:
        return _json(HTTPStatus.REQUEST_TIMEOUT,
                     {"ok": False, "error": "body read timed out"}), True
    try:
        return (json.loads(body, object_pairs_hook=_unique_json_fields if reject_duplicates else None)
                if body else {}), False
    except (ValueError, RecursionError):
        # RecursionError: a deeply nested document overflows the decoder. That is
        # a bad request, not a 500.
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON"}), True


def _actor(request) -> str:
    """The audit actor for a request: the logged-in user, else ``anonymous`` (auth-off / loopback)."""
    return request.get(_USER_KEY) or "anonymous"


_AUDIT_DETAIL_MAX = 256                       # cap the recorded params so a 64 KiB rule body can't bloat a row


def _audit_fields(op):
    """(target, detail) for an audit row. For ``automation_set`` the ``rule`` is an ARBITRARY user
    body (Rule.from_dict ignores unknown top-level fields), so it is NEVER dumped raw — record just
    the rule id + a fixed summary. Other ops carry only known, bounded device params, dumped as
    compact JSON (length-capped as a backstop)."""
    name = op.get("op")
    if name in ("automation_set", "automation_delete"):
        # the id is unvalidated user input here (validated inside the op, after this runs) — record it
        # only when it's a string, never serialize an arbitrary object, and use a fixed detail string.
        rule = op.get("rule")
        rid = rule.get("id") if isinstance(rule, dict) else op.get("id")
        return (rid if isinstance(rid, str) else None), ("rule update" if name == "automation_set" else "rule delete")
    target = op.get("node", op.get("file", op.get("id")))
    detail = json.dumps({k: v for k, v in op.items() if k != "op"}, sort_keys=True)
    return (str(target) if target is not None else None), detail[:_AUDIT_DETAIL_MAX]


async def _dispatch_op(rt, op, *, actor="system", fail_status=HTTPStatus.INTERNAL_SERVER_ERROR):
    """Run a control op and map the outcome: ValueError/KeyError/TypeError → 400, ``ok`` → 200, else
    → ``fail_status``. Records the action to the audit log (#56) with ``actor`` + outcome (fire-and-
    forget — ``_audit`` never blocks the response)."""
    target, detail = _audit_fields(op)
    action = op.get("op", "?")
    try:
        resp = await process_control_op(rt, op)
    except (ValueError, KeyError, TypeError) as exc:
        _audit(rt, actor, action, target=target, detail=detail, result=f"error: {exc}")
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
    _audit(rt, actor, action, target=target, detail=detail,
           result="ok" if resp.get("ok") else f"error: {resp.get('error')}")
    if resp.get("ok"):
        return _json(HTTPStatus.OK, resp)
    return _json(fail_status, resp)


async def _name(request):
    op, err = await _read_json_body(request)
    if err:
        return op
    error = _validate_name_payload(op)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
    op["op"] = "name"                          # normalise after validation
    return await _dispatch_op(_rt(request), op, actor=_actor(request))


async def _control(request):
    op, err = await _read_json_body(request)
    if err:
        return op
    error = _validate_control_payload(op)
    if error is not None:
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
    return await _dispatch_op(_rt(request), op, actor=_actor(request), fail_status=HTTPStatus.SERVICE_UNAVAILABLE)


async def _graduate(request):
    """POST /api/graduate — persist standalone mode (Phase-3; applied on the next restart). Takes
    no body, but still requires ``Content-Type: application/json`` — the same CSRF guard the other
    device-affecting mutations use, so a cross-origin form-POST can't trigger graduation."""
    body, err = await _read_json_body(request)         # enforce the JSON content-type; the parsed body is unused
    if err:
        return body
    return await _dispatch_op(_rt(request), {"op": "graduate"}, actor=_actor(request),
                              fail_status=HTTPStatus.SERVICE_UNAVAILABLE)


async def _ir(request):
    """Transmit a saved IR signal via the Flipper. Body: ``{"file","button"}``.
    The op's own ``ok/error`` (disabled / queue full / timed out / failed) is surfaced,
    a non-ok mapping to 503 so the dashboard flags it."""
    op, err = await _read_json_body(request)
    if err:
        return op
    if not isinstance(op, dict):                 # a list/scalar JSON body would crash op.get()
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": _BODY_NOT_OBJECT})
    ir_file, button = op.get("file"), op.get("button")
    if not (isinstance(ir_file, str) and ir_file and isinstance(button, str) and button):
        return _json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "file and button required"})
    return await _dispatch_op(_rt(request), {"op": "ir", "file": ir_file, "button": button},
                              actor=_actor(request), fail_status=HTTPStatus.SERVICE_UNAVAILABLE)


async def _automations_list(request):
    """List every authored rule. The ``automations`` op is read-only and returns ``ok: True``."""
    resp = await process_control_op(_rt(request), {"op": "automations"})
    return _json(HTTPStatus.OK, resp)


async def _automations_set(request):
    """Create/replace a rule. The body IS the rule spec; ``Rule.from_dict`` (inside
    the op) is the authoritative validator — its ValueError maps to 400."""
    body, err = await _read_json_body(request, MAX_RULE_BODY)
    if err:
        return body
    return await _dispatch_op(_rt(request), {"op": "automation_set", "rule": body}, actor=_actor(request))


async def _automations_delete(request):
    """Delete a rule by id. Pull ``id`` out only when the body is an object, so a
    non-dict JSON body yields ``id=None`` → the op's ValueError → 400 (never an
    uncaught AttributeError from ``body.get`` on a list/str/number)."""
    body, err = await _read_json_body(request, MAX_RULE_BODY)   # same cap as set → any settable id is deletable
    if err:
        return body
    rid = body.get("id") if isinstance(body, dict) else None
    return await _dispatch_op(_rt(request), {"op": "automation_delete", "id": rid}, actor=_actor(request))


def _validate_name_payload(op) -> "str | None":
    if not isinstance(op, dict):
        return _BODY_NOT_OBJECT
    unknown = set(op) - _NAME_FIELDS
    if unknown:
        return f"unknown field(s): {sorted(unknown)}"      # explicit allowlist
    explicit_op = op.get("op")
    if explicit_op is not None and explicit_op != "name":
        return "/api/name only accepts op=name"
    if "node" not in op:
        return "'node' field is required"
    fields_present = [k for k in ("name", "room", "type") if k in op]
    if not fields_present:
        return "at least one of name, room, type is required"
    error = _validate_name_strings(op)
    if error is not None:
        return error
    dtype = op.get("type")
    if dtype is not None and dtype not in _TYPES:
        return f"invalid type {dtype!r}"
    ep = op.get("ep")
    if ep is not None and (not isinstance(ep, int) or isinstance(ep, bool) or ep < 0):
        return "ep must be a non-negative integer"
    return None


def _validate_name_strings(op) -> "str | None":
    for key in ("name", "room"):
        value = op.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_STRING):
            return f"{key} must be a string ≤ {MAX_STRING} chars"
    return None


def _control_node_error(op) -> "str | None":
    node = op.get("node")
    if not isinstance(node, int) or isinstance(node, bool) or not 0 <= node <= 255:
        return "node must be an integer 0..255"
    return None


def _control_on_error(op) -> "str | None":
    if not isinstance(op.get("on"), bool):
        return "on must be a boolean"
    return None


def _control_switch_error(op) -> "str | None":
    error = _control_on_error(op)
    if error is not None:
        return error
    if "endpoint" not in op:
        return None
    endpoint = op.get("endpoint")
    if not isinstance(endpoint, int) or isinstance(endpoint, bool) or endpoint not in {1, 2}:
        return "endpoint must be an integer 1 or 2"
    return None


def _control_value_error(op) -> "str | None":
    value = op.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 99:
        return "value must be an integer 0..99"
    return None


def _control_celsius_error(op) -> "str | None":
    # Reject non-numbers, bool (True == 1), and json's NaN/Infinity (a float that isn't finite)
    # before the range check; round(°C*10) is the wire encoding. The Keemple TRVs accept 4..28 °C
    # (verified live against the app — the UI dropdown and the frost-safe OFF's 4 °C both need 4).
    celsius = op.get("celsius")
    if (not isinstance(celsius, (int, float)) or isinstance(celsius, bool)
            or (isinstance(celsius, float) and not math.isfinite(celsius))
            or not 4 <= celsius <= 28):
        return "celsius must be a number between 4 and 28"
    return None


# Per-op field validator (node is checked separately, for every op).
_CONTROL_FIELD_VALIDATORS = {
    "switch": _control_switch_error,
    "thermostat_power": _control_on_error,
    "level": _control_value_error,
    "cover": _control_value_error,
    "thermostat": _control_celsius_error,
}


def _validate_control_payload(op) -> "str | None":
    """Validate a /api/control body: a strict allowlist of device ops (never `raw`/`lights`),
    no unknown fields, and per-op operand bounds. Returns an error string or None when valid."""
    if not isinstance(op, dict):
        return _BODY_NOT_OBJECT
    name = op.get("op")
    if name not in _CONTROL_OPS:
        return f"unsupported control op {name!r}"
    unknown = set(op) - _CONTROL_FIELDS[name]
    if unknown:
        return f"unknown field(s): {sorted(unknown)}"
    return _control_node_error(op) or _CONTROL_FIELD_VALIDATORS[name](op)


def make_app(rt):
    from .calendar_sync.api import register as register_calendars
    from .budget.api import register as register_budget
    app = web.Application(
        client_max_size=MAX_RULE_BODY + 4096,  # 69632; route caps run before the body is read
        middlewares=[_auth_middleware, _empty_404_405_middleware],   # auth OUTERMOST: gate before routing
    )
    app[_RT_KEY] = rt
    app[_LOGIN_LIMITER_KEY] = auth.LoginLimiter()
    register_calendars(app, _read_json_body)
    register_budget(app, _read_json_body, auth_enabled=lambda: _AUTH_ENABLED,
                    session_secret=lambda: _SESSION_SECRET, secure_cookie=lambda: _COOKIE_SECURE,
                    login_limiter=app[_LOGIN_LIMITER_KEY])
    app.router.add_get("/", _ui_index, allow_head=False)                          # the TS app (Vite build)
    app.router.add_get("/assets/{path:[A-Za-z0-9._-]+}", _ui_asset, allow_head=False)
    app.router.add_get("/ui/", _ui_redirect, allow_head=False)                    # retired alias -> /
    for path in _PUBLIC_DOCUMENTS:
        app.router.add_get(path, _ui_document)                                  # GET + HEAD for URL validators
    app.router.add_post("/api/login", _login)
    app.router.add_post("/api/logout", _logout)
    app.router.add_get("/api/auth/features", _auth_features, allow_head=False)
    app.router.add_post(auth_qr.CODE_PATH, _qr_code)
    app.router.add_post(auth_qr.REDEEM_PATH, _qr_redeem)
    app.router.add_post(device_token.EXCHANGE_PATH, _device_exchange)
    app.router.add_get("/api/whoami", _whoami, allow_head=False)
    app.router.add_get("/api/me/capabilities", _me_capabilities, allow_head=False)
    app.router.add_get("/api/discovery", _discovery, allow_head=False)
    app.router.add_get("/api/events", _events, allow_head=False)
    app.router.add_get(_PATH_AUTOMATIONS, _automations_list, allow_head=False)
    app.router.add_get("/api/audit", _audit_feed, allow_head=False)
    app.router.add_get("/api/rf433", _rf433_feed, allow_head=False)
    app.router.add_get("/api/db/stats", _db_stats, allow_head=False)
    app.router.add_get(_PATH_SETTINGS, _settings, allow_head=False)
    app.router.add_post(_PATH_SETTINGS, _settings_set)
    app.router.add_get(_PATH_ROOM_ICONS, _room_icons, allow_head=False)
    app.router.add_post(_PATH_ROOM_ICONS, _room_icon_set)
    app.router.add_get(_PATH_WHOLE_HOME, _whole_home, allow_head=False)
    app.router.add_post(_PATH_WHOLE_HOME, _whole_home_set)
    app.router.add_post("/api/name", _name)
    app.router.add_post("/api/ir", _ir)
    app.router.add_post("/api/control", _control)
    app.router.add_post("/api/scene", _scene)
    app.router.add_post(_PATH_AUTOMATIONS, _automations_set)
    app.router.add_post("/api/automations/delete", _automations_delete)
    app.router.add_post("/api/graduate", _graduate)
    app.router.add_post("/api/me/password", _me_password)
    app.router.add_get(_PATH_USERS, _users_list, allow_head=False)
    app.router.add_post(_PATH_USERS, _users_add)
    app.router.add_post("/api/users/role", _users_role)
    app.router.add_post("/api/users/disabled", _users_disabled)
    app.router.add_post("/api/users/reset-password", _users_reset_password)
    return app


@dataclass(frozen=True)
class _WebHandle:
    runner: web.AppRunner
    address: tuple[str, int]


async def start_web(rt, host: str = None, port: int = None):
    """Bind and start aiohttp in the current asyncio loop; return a small handle.

    ``host`` defaults to ``$HESTIA_WEB_HOST`` (``127.0.0.1``); ``port`` defaults
    to ``$HESTIA_WEB_PORT`` (``8927``). A non-loopback ``host`` requires
    ``HESTIA_WEB_ALLOW_REMOTE=1``."""
    if host is None:
        host = os.environ.get("HESTIA_WEB_HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("HESTIA_WEB_PORT", "8927"))
    _require_safe_web_bind(host)
    if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
        store_sql.check_supported_policy_phase()
    runner = web.AppRunner(make_app(rt), shutdown_timeout=2.0, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    bound = runner.addresses[0]
    address = (bound[0], bound[1])
    log.info("web UI listening on http://%s:%d", *address)
    return _WebHandle(runner=runner, address=address)


async def stop_web(handle: _WebHandle) -> None:
    await handle.runner.cleanup()
