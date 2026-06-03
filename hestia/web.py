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
    "switch": {"op", "node", "on"},
    "level": {"op", "node", "value"},
    "cover": {"op", "node", "value"},
    "thermostat": {"op", "node", "celsius"},
    "thermostat_power": {"op", "node", "on"},
}
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


@web.middleware
async def _auth_middleware(request, handler):
    """Gate every non-public route on a valid session cookie when auth is enabled (a HESTIA_SESSION_SECRET
    is set). An absent / expired / forged cookie → 401 (the TS app then shows the login form). No-op when
    auth is off. Runs OUTERMOST so an unauthenticated request is rejected before it reaches any handler."""
    if not _AUTH_ENABLED or _is_public_route(request.method, request.path):
        return await handler(request)
    user = auth.verify_session(request.cookies.get(_SESSION_COOKIE, ""), now=_now(), secret=_SESSION_SECRET)
    if user is None:
        return _empty(HTTPStatus.UNAUTHORIZED)
    request[_USER_KEY] = user                     # for /api/whoami (and future per-user UI)
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
    resp = _json(HTTPStatus.OK, {"ok": True, "user": user})
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
    return _json(HTTPStatus.OK, {"user": request.get(_USER_KEY)})


async def _audit_feed(request):
    """GET /api/audit — recent audit-log rows (newest first), auth-gated like every /api/* route."""
    rt = _rt(request)
    if rt.audit_engine is None:                  # audit backend not wired (bare runtime) → empty feed
        return _json(HTTPStatus.OK, {"events": []})
    events = await asyncio.get_running_loop().run_in_executor(
        None, lambda: store_sql.recent_audit(rt.audit_engine))
    return _json(HTTPStatus.OK, {"events": events})


async def _db_stats(_request):
    """GET /api/db/stats — SQLite file size + row counts, auth-gated like every /api/* route."""
    stats = await asyncio.get_running_loop().run_in_executor(None, store_sql.db_stats)
    return _json(HTTPStatus.OK, stats)


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
                  "ir_buttons": IR_BUTTONS, "klima": KLIMA, "rule_vocab": rule_vocab(),
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


def _control_value_error(op) -> "str | None":
    value = op.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 99:
        return "value must be an integer 0..99"
    return None


def _control_celsius_error(op) -> "str | None":
    # Reject non-numbers, bool (True == 1), and json's NaN/Infinity (a float that isn't finite)
    # before the range check; round(°C*10) is the wire encoding, so 5..30 °C is the safe band.
    celsius = op.get("celsius")
    if (not isinstance(celsius, (int, float)) or isinstance(celsius, bool)
            or (isinstance(celsius, float) and not math.isfinite(celsius))
            or not 5 <= celsius <= 30):
        return "celsius must be a number between 5 and 30"
    return None


# Per-op field validator (node is checked separately, for every op).
_CONTROL_FIELD_VALIDATORS = {
    "switch": _control_on_error,
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
    app.router.add_get("/api/automations", _automations_list, allow_head=False)
    app.router.add_get("/api/audit", _audit_feed, allow_head=False)
    app.router.add_get("/api/db/stats", _db_stats, allow_head=False)
    app.router.add_post("/api/name", _name)
    app.router.add_post("/api/ir", _ir)
    app.router.add_post("/api/control", _control)
    app.router.add_post("/api/automations", _automations_set)
    app.router.add_post("/api/automations/delete", _automations_delete)
    app.router.add_post("/api/graduate", _graduate)
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
