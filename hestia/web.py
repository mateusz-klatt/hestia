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
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from aiohttp import ClientConnectionError, web

from . import auth, store_sql
from .classifier import DeviceType
from .automations import rule_vocab
from .proxy import (IR_BUTTONS, KLIMA, _CLOSED_SENTINEL, _LOOPBACK, _audit, _merged_discovery,
                    _num_env, _truthy_env, globals_snapshot, process_control_op)

log = logging.getLogger("hestia.web")

MAX_BODY = 8192                                      # cap POST body size (bytes)
MAX_RULE_BODY = 65536                                # larger cap for an automation rule
MAX_STRING = 256                                     # cap name / room length (chars)
SSE_IDLE_TIMEOUT = _num_env("HESTIA_SSE_KEEPALIVE", 5.0, 1.0, 3600.0)  # idle→keepalive; also how fast a
#       reloaded-away client's slot is reclaimed (write fails). Clamped ≥1s so a typo can't tight-loop.
SSE_MAX_LIFETIME = float(os.environ.get("HESTIA_SSE_LIFETIME", "3600"))
_TYPES = {t.value for t in DeviceType}
_clock = time.monotonic                              # module-level rebindable for tests
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
_TEMP_SCALES = {"C", "F", "K"}
_EMPTY_SETTINGS = {"locale": None, "temp_scale": None, "theme": None}
_RT_KEY = web.AppKey("rt", object)
_UI_DIST_ENV = "HESTIA_UI_DIST"
_DEFAULT_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"

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
_now = time.time          # WALL clock for session expiry (must survive restarts, unlike _clock); rebindable for tests


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
    redirect (GET, so the login form can load), and login/logout (POST). Everything else is gated —
    incl. a wrong-method hit on a public path (e.g. GET /api/login → gated, not a public 405)."""
    if method == "GET":
        return path in ("/", "/ui/") or path.startswith("/assets/")
    return method == "POST" and path in ("/api/login", "/api/logout")


# RBAC route policy (#73). The MINIMUM role for each non-public route, kept adjacent to make_app's route
# table; ``test_every_route_is_classified`` asserts every registered route is here (or public), so this
# can't silently drift from the routes. Public routes are short-circuited in the middleware before this.
_VIEWER, _OPERATOR, _ADMIN = "viewer", "operator", "admin"
# Route paths that appear in BOTH the floor map and the route registration — named once so the two
# can't drift (and to satisfy the no-duplicate-literal lint). Single-use paths stay inline literals.
_PATH_SETTINGS = "/api/settings"
_PATH_ROOM_ICONS = "/api/rooms/icons"
_PATH_AUTOMATIONS = "/api/automations"
_PATH_USERS = "/api/users"
_PATH_WHOLE_HOME = "/api/whole-home"
_USERNAME_REQUIRED = "username is required"   # shared 400 message across the admin user verbs
_ROUTE_MIN_ROLE = {
    # reads every signed-in user (incl. a read-only viewer) needs — the room / event-log / temps / own-prefs surface
    ("GET", "/api/discovery"): _VIEWER,
    ("GET", "/api/events"): _VIEWER,
    ("GET", "/api/audit"): _VIEWER,
    ("GET", _PATH_SETTINGS): _VIEWER,
    ("POST", _PATH_SETTINGS): _VIEWER,            # a user's OWN UI prefs (locale / temp scale / theme)
    ("GET", _PATH_ROOM_ICONS): _VIEWER,
    ("GET", "/api/whoami"): _VIEWER,
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
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _required_role(method: str, path: str) -> str:
    """The minimum role for a non-public route. Fail-closed: an UNclassified mutating method → admin (a
    new write route stays locked down until explicitly classified); any other unclassified method
    (GET/HEAD) → viewer. ``test_every_route_is_classified`` keeps the map complete, so the fallbacks are
    a backstop, not the normal path."""
    floor = _ROUTE_MIN_ROLE.get((method, path))
    if floor is not None:
        return floor
    return _ADMIN if method in _MUTATING_METHODS else _VIEWER


def _role_allows(role, floor: str) -> bool:
    """True iff the user's resolved ``role`` clears the route's ``floor``. An unknown / None role fails
    every floor (denied) — fail closed."""
    rank = store_sql.ROLE_RANK.get(role)
    return rank is not None and rank >= store_sql.ROLE_RANK[floor]


_BEARER_PREFIX = "Bearer "


def _request_token(request) -> str:
    """The session token for this request: ``Authorization: Bearer <token>`` (native clients, e.g. the
    iOS app) when present, else the ``hestia_session`` cookie (the browser). Both carry the SAME signed
    ``username|expiry`` token; the bearer header wins so a native client never depends on the cookie jar."""
    header = request.headers.get("Authorization", "")
    if header.startswith(_BEARER_PREFIX):
        return header[len(_BEARER_PREFIX):]
    return request.cookies.get(_SESSION_COOKIE, "")


@web.middleware
async def _auth_middleware(request, handler):
    """Authenticate + authorize every non-public route when auth is enabled (a HESTIA_SESSION_SECRET is
    set). An absent / expired / forged token → 401 (the TS app then shows the login form); a token for
    an account that no longer exists → 401 (immediate revocation, not at token expiry); a valid user
    whose role is below the route's floor → 403. No-op when auth is off (loopback/dev stays fully open).
    The token comes from the bearer header or the session cookie (see ``_request_token``). Runs OUTERMOST
    so an unauthenticated/unauthorized request is rejected before it reaches any handler."""
    if not _AUTH_ENABLED or _is_public_route(request.method, request.path):
        return await handler(request)
    user = auth.verify_session(_request_token(request), now=_now(), secret=_SESSION_SECRET)
    if user is None:
        return _empty(HTTPStatus.UNAUTHORIZED)
    # Resolve the role fresh on EVERY authenticated request (offloaded off the loop): a demotion or a
    # removed account takes effect immediately, instead of living on in the 30-day signed cookie.
    role = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.current_user_role(user))
    if role is None:                              # the account is gone → the still-signed cookie is moot
        return _empty(HTTPStatus.UNAUTHORIZED)
    request[_USER_KEY] = user                     # for /api/whoami + the audit actor
    request[_ROLE_KEY] = role                     # for /api/whoami + the floor check below
    if not _role_allows(role, _required_role(request.method, request.path)):
        return _empty(HTTPStatus.FORBIDDEN)
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
    if not (_AUTH_ENABLED and auth.authenticate(user, password, store_sql.current_users())):
        _audit(_rt(request), user if isinstance(user, str) else "?", "login", result="invalid")
        return _json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "invalid credentials"})
    _audit(_rt(request), user, "login", result="ok")
    token = auth.make_session(user, now=_now(), secret=_SESSION_SECRET)
    body = {"ok": True, "user": user}
    # A native client opts in with {"bearer": true} and stores the returned token (sent back as
    # `Authorization: Bearer`). The browser does NOT ask, so the token never reaches page JS — the
    # httponly cookie below stays the web session's sole carrier (XSS can't read it).
    if op.get("bearer") is True:
        body["token"] = token
    resp = _json(HTTPStatus.OK, body)
    resp.set_cookie(_SESSION_COOKIE, token, max_age=int(auth.SESSION_TTL), httponly=True,
                    samesite="Strict", secure=_COOKIE_SECURE, path="/")
    return resp


async def _logout(_request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    # Not audited: /api/logout is public (no session lookup in the middleware), so the actor would
    # always be "anonymous" — logout is low-value to attribute. Login (success/failure) is audited.
    resp = _json(HTTPStatus.OK, {"ok": True})
    resp.del_cookie(_SESSION_COOKIE, path="/")
    return resp


async def _whoami(request):  # NOSONAR S7503: aiohttp route handlers must be coroutines (the framework awaits them)
    # The role is resolved once by the auth middleware (request[_ROLE_KEY]); both are None when auth is
    # off (loopback/dev), which the TS app reads as "fully open, no role gating".
    return _json(HTTPStatus.OK, {"user": request.get(_USER_KEY), "role": request.get(_ROLE_KEY)})


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
    return None


def _settings_update_fields(body) -> dict:
    # Pass only the fields actually present so the store's single-transaction upsert preserves the
    # rest (no read-merge-write race between two concurrent partial POSTs).
    return {k: body[k] for k in ("locale", "temp_scale", "theme") if k in body}


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
    verified = await loop.run_in_executor(
        None, lambda: auth.authenticate(user, current, store_sql.current_users()))
    if not verified:
        _audit(_rt(request), user, "password", result="invalid")
        return _json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "current password is incorrect"})
    new_hash = await loop.run_in_executor(None, lambda: auth.hash_password(new))
    wrote = await loop.run_in_executor(None, lambda: store_sql.set_user_password(user, new_hash))
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


def _scene_device_op(target_type: str, node_id: int, info: dict, active: bool) -> dict:
    """One per-device control op for a scene, mirroring the per-device UI buttons: a dimmable light
    (has a level) → ``level`` 0/99; a plain light → ``switch`` off/on; a blind → ``cover`` 0/99."""
    if target_type == "light" and info.get("level") is None:
        return {"op": "switch", "node": node_id, "on": active}
    op = "level" if target_type == "light" else "cover"
    return {"op": op, "node": node_id, "value": 99 if active else 0}


def _scene_node_ops(target_type: str, node_id: int, info: dict, active: bool, entry: dict) -> "list[dict]":
    """The scene ops for ONE device. A plain switch light whose registry entry opts single GANGS out
    (``endpoint_exclude``) is driven per-gang — one endpoint-addressed switch op per NON-excluded gang
    (the same op the room UI's per-gang buttons post) — so e.g. a 2-gang hall/nightlight node keeps the
    nightlight untouched. Every other device keeps today's single node-level op (one frame)."""
    excluded = {int(k) for k, v in (entry.get("endpoint_exclude") or {}).items() if v}
    if target_type != "light" or info.get("level") is not None or not excluded:
        return [_scene_device_op(target_type, node_id, info, active)]
    # Any truthy opt-out takes THIS branch unconditionally — the node-level frame (which would drive
    # every gang at once) is never used for such a node. Gangs we can't enumerate (live gang state ∪
    # labels) are therefore deliberately skipped, not swept: fail-safe in the excluded gang's favour.
    eps = {int(k) for k in info.get("endpoints") or {}}
    eps |= {int(k) for k in entry.get("endpoint_names") or {}}
    return [{"op": "switch", "node": node_id, "on": active, "endpoint": ep}
            for ep in sorted(eps - excluded)]


def _scene_ops(rt, scene: str) -> list[dict]:
    """Expand a house-wide scene into ordinary per-device control ops."""
    target_type, active = _SCENE_TARGETS[scene]
    # The house-wide sweep skips any device an admin opted out of "all" (e.g. a nightlight), so
    # SceneResult.total reflects only the devices the scene targets. The opt-out is read straight from
    # the registry (NOT from discovery) so the DeviceInfo wire shape stays stable for native clients.
    reg = rt.registry.nodes
    return [op
            for node, info in _merged_discovery(rt).items()
            if info.get("type") == target_type and not reg.get(node, {}).get("exclude_from_all")
            for op in _scene_node_ops(target_type, int(node), info, active, reg.get(node, {}))]


async def _scene(request):
    """POST /api/scene — fan out one house-wide scene through the normal per-device control path."""
    body, err = await _read_json_body(request)
    if err:
        return body
    scene = body.get("op") if isinstance(body, dict) else None
    if scene not in _SCENE_TARGETS:
        return _json(HTTPStatus.BAD_REQUEST,
                     {"ok": False, "error": f"op must be one of {', '.join(_SCENE_TARGETS)}"})

    rt = _rt(request)
    ops = _scene_ops(rt, scene)
    sent = 0
    for op in ops:
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
        while _clock() < deadline:
            event = await _wait_event(sub.queue)
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


async def _read_json_body(request, max_body=MAX_BODY):
    """Read and JSON-parse the request body, enforcing framing limits. On any
    content-type/framing/size/parse error this returns ``(response, True)``;
    on success returns ``(obj, False)``. A zero-length body parses to ``{}``
    (preserving the original ``/api/name`` contract); ``max_body`` caps the byte
    size per endpoint (names are small, rules larger).

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
    body = await request.content.read(cl) if cl > 0 else b""
    try:
        return (json.loads(body) if body else {}), False
    except ValueError:
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
    app = web.Application(
        client_max_size=MAX_RULE_BODY + 4096,
        middlewares=[_auth_middleware, _empty_404_405_middleware],   # auth OUTERMOST: gate before routing
    )
    app[_RT_KEY] = rt
    app.router.add_get("/", _ui_index, allow_head=False)                          # the TS app (Vite build)
    app.router.add_get("/assets/{path:[A-Za-z0-9._-]+}", _ui_asset, allow_head=False)
    app.router.add_get("/ui/", _ui_redirect, allow_head=False)                    # retired alias -> /
    app.router.add_post("/api/login", _login)
    app.router.add_post("/api/logout", _logout)
    app.router.add_get("/api/whoami", _whoami, allow_head=False)
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
