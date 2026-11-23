"""Receipts-scoped device tokens and one-time pairing codes.

The browser shows a pairing code once. The device shows a bearer once. The
database stores only HMAC-SHA256 digests of those secrets. Comparisons use
``hmac.compare_digest``. Nothing in this module logs a code or a bearer.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import uuid

from aiohttp import web
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from ..db import BudgetAnalyticsAudit
from ..db import BudgetReceiptDeviceToken as DeviceToken
from ..db import BudgetReceiptPairCode as PairCode
from ..db import User
from . import consent_context
from .config import BudgetConfigError, _read_secret

log = logging.getLogger("hestia.budget.device_token")

FLAG = "HESTIA_BUDGET_RECEIPTS_ENABLED"
PREFIX = "/api/budget/receipts"
COOKIE = "hestia_session"
REQUEST_KEY = "hestia_device_token"
PAIR_PATH = PREFIX + "/device-token/pair-code"
REDEEM_PATH = PREFIX + "/device-token/redeem"
LIST_PATH = PREFIX + "/device-token/list"
REVOKE_PATH = PREFIX + "/device-token/revoke"
PARSE_PATH = PREFIX + "/parse-text"
CONFIRM_PATH = PREFIX + "/confirm"
LIST_RECEIPTS_PATH = PREFIX + "/list"
SEARCH_PATH = PREFIX + "/search"
DETAIL_PATH = PREFIX + "/detail"
IMAGE_PATH = PREFIX + "/image"
DELETE_PATH = PREFIX + "/delete"

ALLOWED = frozenset({
    ("POST", PARSE_PATH),
    ("POST", CONFIRM_PATH),
    ("POST", LIST_RECEIPTS_PATH),
    ("POST", SEARCH_PATH),
    ("GET", DETAIL_PATH),
    ("GET", IMAGE_PATH),
    ("POST", DELETE_PATH),
    ("POST", REVOKE_PATH),
})

PAIR_TTL = 300
MAX_ACTIVE_CODES = 3
SLIDE_TTL = 90 * 86400
HARD_TTL = 180 * 86400
SLIDE_WRITE_INTERVAL = 3600
PARSE_LIMIT = 30
PARSE_WINDOW = 60
REDEEM_IP_LIMIT = 10
REDEEM_GLOBAL_LIMIT = 30
REDEEM_WINDOW = 60
REDEEM_BACKOFF_AFTER = 5
_MAX_BACKOFF_EXPONENT = 5
_MAX_BUCKETS = 4096
_INSERT_ATTEMPTS = 3
_CODE_LENGTH = 8
_TOKEN_BYTES = 32
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_TRANSLATION = str.maketrans("ILO", "110", " -")
_RULE_KEY_FILE = "HESTIA_BUDGET_RULE_KEY_FILE"
_RULE_KEY_BYTES = 32
_CODE_DOMAIN = b"hestia/budget/receipt-device-token/pair-code/v1"
_TOKEN_DOMAIN = b"hestia/budget/receipt-device-token/v1"
_BLANK = "0" * 64
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_BEARER = "Bearer "
_buckets = {}
_bucket_lock = threading.Lock()


class DeviceTokenError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def now():
    return time.time()


def receipts_enabled():
    """Read the environment directly. Callers must not go through ``receipt_api.enabled``."""
    return os.environ.get(FLAG) == "1"


def budget_http_enabled():
    return os.environ.get("HESTIA_BUDGET_HTTP_ENABLED") == "1"


def receipts_path(path):
    return isinstance(path, str) and path.startswith(PREFIX + "/")


def reset_limits():
    with _bucket_lock:
        _buckets.clear()


def client_ip(request):
    remote = getattr(request, "remote", None)
    if not isinstance(remote, str) or not remote:
        return "unknown"
    if os.environ.get(consent_context.TRUSTED_PROXY_ENV):
        try:
            return consent_context.psu_context(request).ip_address or remote
        except BudgetConfigError:
            return remote
    return remote


def _same(left, right):
    return hmac.compare_digest(left, right)


def _accepted(stored, digest):
    """Timing-safe HMAC check. A missing row is compared against a fixed blank digest."""
    if stored is None:
        _same(_BLANK, digest)
        return False
    return _same(stored, digest)


def ids_match(left, right):
    if (not isinstance(left, str) or not isinstance(right, str) or not left
            or not left.isascii() or not right.isascii()):
        return False
    return _same(left, right)


def _rule_key():
    root = _read_secret(os.environ.get(_RULE_KEY_FILE, ""), max_bytes=_RULE_KEY_BYTES)
    if len(root) != _RULE_KEY_BYTES:
        raise BudgetConfigError("The receipt device key must be 32 bytes")
    return root


def _mac(root, domain, value):
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=domain).derive(root)
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _pairing_code():
    return "".join(_ALPHABET[byte & 31] for byte in os.urandom(_CODE_LENGTH))


def normalize_code(value):
    if not isinstance(value, str) or not value.isascii():
        return None
    folded = value.upper().translate(_CODE_TRANSLATION)
    if len(folded) != _CODE_LENGTH or any(char not in _ALPHABET for char in folded):
        return None
    return folded


def _bearer_token(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    padding = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + padding)
    except (ValueError, UnicodeError):
        return None
    if len(raw) != _TOKEN_BYTES:
        return None
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        return None
    return value


def _device_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isprintable():
        return None
    return value


def _bearer(headers):
    try:
        header = headers.get("Authorization", "")
    except (AttributeError, TypeError):
        return None
    if not isinstance(header, str) or not header.startswith(_BEARER):
        return None
    token = header[len(_BEARER):]
    if not token or any(char.isspace() for char in token):
        return None
    return token


def _quota_wait(stamps, moment, window, limit, backoff_after):
    if len(stamps) >= limit:
        return max(1, math.ceil(window - (moment - stamps[0])))
    if backoff_after is not None and len(stamps) >= backoff_after:
        delay = 2 ** min(len(stamps) - backoff_after, _MAX_BACKOFF_EXPONENT)
        return math.ceil(delay - (moment - stamps[-1]))
    return 0


def _charge(keys, moment, window):
    """Reserve all quota for one request or return the largest required wait."""
    with _bucket_lock:
        if len(_buckets) + len(keys) > _MAX_BUCKETS:
            stale_after = max(REDEEM_WINDOW, PARSE_WINDOW)
            for key, stamps in tuple(_buckets.items()):
                if moment - stamps[-1] >= stale_after:
                    del _buckets[key]
        waits = []
        updated = {}
        available = _MAX_BUCKETS - len(_buckets)
        for key, limit, backoff_after in keys:
            if key not in _buckets:
                if available <= 0:
                    waits.append(window)
                    continue
                available -= 1
            stamps = [stamp for stamp in _buckets.get(key, ()) if moment - stamp < window]
            wait = _quota_wait(stamps, moment, window, limit, backoff_after)
            if wait > 0:
                waits.append(wait)
            else:
                updated[key] = [*stamps, moment]
        if waits:
            return max(waits)
        _buckets.update(updated)
        return None


def redeem_limited(ip, moment):
    # Admit attempts by source IP before looking at the body. This makes the
    # delay independent of code validity and leaves rejected attempts out of global quota.
    return _charge(
        ((("redeem", "ip", ip), REDEEM_IP_LIMIT, REDEEM_BACKOFF_AFTER),
         (("redeem", "global"), REDEEM_GLOBAL_LIMIT, None)),
        moment, REDEEM_WINDOW,
    )


def parse_limited(token_id, moment):
    return _charge(((("parse", token_id), PARSE_LIMIT, None),), moment, PARSE_WINDOW)


def json_error(decision):
    headers = dict(_PRIVATE)
    if decision.get("retry") is not None:
        headers["Retry-After"] = str(decision["retry"])
    return web.json_response(
        {"ok": False, "error": decision["code"]}, status=decision["status"], headers=headers,
    )


def disabled_response():
    return json_error({"status": 404, "code": "budget_receipts_disabled"})


def write_device_audit(session, owner, token_id, action, moment):
    """Record device activity in the owner's budget audit."""
    session.add(BudgetAnalyticsAudit(
        owner=owner, subject_type="receipt_device_token", subject_id=token_id, action=action,
        before_json="null", after_json=json.dumps({"actor": "device:" + token_id}), changed_at=moment,
    ))


def revoke_owner_tokens(session, owner, moment=None):
    """Invalidate the owner's device credentials and pending pairing codes together."""
    when = time.time() if moment is None else moment
    session.execute(update(DeviceToken).where(
        DeviceToken.owner == owner, DeviceToken.revoked_at.is_(None),
    ).values(revoked_at=when))
    session.execute(update(PairCode).where(
        PairCode.owner == owner, PairCode.consumed_at.is_(None),
    ).values(consumed_at=when))


def open_store(*, clock=None):
    from .views import BudgetUsersBackendRequired
    from .. import store_sql
    from ..db import init_db

    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        raise BudgetUsersBackendRequired()
    engine, sessions = init_db()
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    return DeviceTokenStore(sessions, clock=clock or now)


def decide(method, path, headers, cookies, secret, moment):
    """Classify a bearer without logging it.

    Login sessions pass through to the web session policy.
    Device-shaped credentials are forbidden outside the receipts allowlist.
    """
    from .. import auth
    from .views import BudgetUsersBackendRequired

    receipts = receipts_path(path)
    try:
        has_cookie = COOKIE in cookies
        has_authorization = "Authorization" in headers
    except TypeError:
        return {"action": "pass"}
    if receipts and has_cookie and has_authorization:
        return {"action": "response", "status": 400, "code": "budget_auth_ambiguous"}
    if receipts and not budget_http_enabled():
        return {"action": "response", "status": 404, "code": "budget_disabled"}
    token = _bearer(headers)
    if token is None:
        return {"action": "pass"}
    if auth.verify_session(token, now=moment, secret=secret) is not None:
        return {"action": "pass"}
    allow = (method, path) in ALLOWED
    if _bearer_token(token) is None:
        if allow:
            return {"action": "response", "status": 401, "code": "device_token_invalid"}
        return {"action": "pass"}
    if not allow:
        return {"action": "response", "status": 403, "code": "budget_forbidden"}
    try:
        found = open_store(clock=lambda: moment).identify(token, slide=path != REVOKE_PATH)
    except (BudgetUsersBackendRequired, BudgetConfigError):
        found = {"status": "unknown"}
    except Exception as exc:
        log.error("device token check failed: %s", type(exc).__name__)
        return {"action": "response", "status": 503, "code": "budget_unavailable"}
    if found.get("status") == "unknown":
        return {"action": "response", "status": 401, "code": "device_token_invalid"}
    if found.get("status") == "revoked" and path != REVOKE_PATH:
        return {"action": "response", "status": 401, "code": "device_token_revoked"}
    if found.get("status") == "expired":
        return {"action": "response", "status": 401, "code": "device_token_expired"}
    if found.get("status") not in ("ok", "revoked") or not found.get("owner") or not found.get("token_id"):
        return {"action": "response", "status": 401, "code": "device_token_invalid"}
    return {"action": "accept", "owner": found["owner"], "token_id": found["token_id"]}


def _insert_unique(session, factory):
    error = None
    for _attempt in range(_INSERT_ATTEMPTS):
        row = factory()
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            error = exc
            continue
        return row
    raise DeviceTokenError(503, "budget_unavailable") from error


class DeviceTokenStore:
    def __init__(self, sessions, *, clock):
        self.sessions = sessions
        self.clock = clock

    @contextmanager
    def _transaction(self):
        from sqlalchemy import text
        with self.sessions.begin() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session

    def _moment(self):
        moment = self.clock()
        if type(moment) not in (int, float) or not math.isfinite(moment) or moment < 0:
            raise DeviceTokenError(503, "budget_unavailable")
        return float(moment)

    def _require_owner(self, session, owner):
        user = session.get(User, owner)
        if user is None or user.disabled or user.role != "admin":
            raise DeviceTokenError(403, "budget_forbidden")
        return user

    def _owner_ok(self, session, owner):
        user = session.get(User, owner)
        return user is not None and not user.disabled and user.role == "admin"

    def issue_code(self, owner):
        root = _rule_key()
        with self._transaction() as session:
            self._require_owner(session, owner)
            moment = self._moment()
            active = session.scalar(select(func.count()).select_from(PairCode).where(
                PairCode.owner == owner, PairCode.consumed_at.is_(None), PairCode.expires_at > moment,
            ))
            if active >= MAX_ACTIVE_CODES:
                raise DeviceTokenError(409, "device_pair_limit")
            issued = {}

            def factory():
                code = _pairing_code()
                issued["code"] = code
                return PairCode(
                    id=str(uuid.uuid4()), owner=owner, code_hmac=_mac(root, _CODE_DOMAIN, code),
                    created_at=moment, expires_at=moment + PAIR_TTL, consumed_at=None,
                )

            row = _insert_unique(session, factory)
            code = issued["code"]
            expires_at = row.expires_at
        return {"code": code, "expires_at": expires_at, "qr_payload": "hestia-pair:" + code}

    def redeem(self, code, device_name):
        normalized = normalize_code(code)
        name = _device_name(device_name)
        if normalized is None or name is None:
            return None
        root = _rule_key()
        digest = _mac(root, _CODE_DOMAIN, normalized)
        with self._transaction() as session:
            moment = self._moment()
            row = session.scalar(select(PairCode).join(User, PairCode.owner == User.username).where(
                PairCode.code_hmac == digest, PairCode.consumed_at.is_(None), PairCode.expires_at > moment,
                User.role == "admin", User.disabled.is_(False),
            ))
            if not _accepted(None if row is None else row.code_hmac, digest):
                return None
            row.consumed_at = moment
            token, token_id, expires_at = self._issue_token(session, row.owner, name, moment, root)
            expires_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat(timespec="seconds")
        return {"token": token, "token_id": token_id, "scope": "receipts",
                "expires_at": expires_iso}

    def _issue_token(self, session, owner, name, moment, root):
        issued = {}

        def factory():
            token = base64.urlsafe_b64encode(os.urandom(_TOKEN_BYTES)).rstrip(b"=").decode("ascii")
            issued["token"] = token
            return DeviceToken(
                id=str(uuid.uuid4()), owner=owner, device_name=name,
                token_hmac=_mac(root, _TOKEN_DOMAIN, token),
                created_at=moment, expires_at=moment + SLIDE_TTL, hard_expires_at=moment + HARD_TTL,
                sliding_written_at=moment, last_used_at=None, revoked_at=None,
            )

        row = _insert_unique(session, factory)
        return issued["token"], row.id, row.expires_at

    def identify(self, token, *, slide):
        canonical = _bearer_token(token)
        if canonical is None:
            _accepted(None, _BLANK)
            return {"status": "unknown"}
        digest = _mac(_rule_key(), _TOKEN_DOMAIN, canonical)
        with self.sessions() as session:
            result, needs_write = self._inspect_token(session, digest, slide=slide, write=False)
        if needs_write:
            with self._transaction() as session:
                result, _ = self._inspect_token(session, digest, slide=slide, write=True)
        return result

    def _inspect_token(self, session, digest, *, slide, write):
        row = session.scalar(select(DeviceToken).where(DeviceToken.token_hmac == digest))
        if not _accepted(None if row is None else row.token_hmac, digest):
            return {"status": "unknown"}, False
        if row.revoked_at is not None:
            return {"status": "revoked", "owner": row.owner, "token_id": row.id}, False
        moment = self._moment()
        if not self._owner_ok(session, row.owner):
            if write:
                row.revoked_at = moment
            return {"status": "revoked", "owner": row.owner, "token_id": row.id}, not write
        if moment >= min(row.expires_at, row.hard_expires_at):
            return {"status": "expired", "token_id": row.id}, False
        needs_slide = slide and (row.last_used_at is None or moment - row.sliding_written_at >= SLIDE_WRITE_INTERVAL)
        if needs_slide and write:
            row.expires_at = min(moment + SLIDE_TTL, row.hard_expires_at)
            row.last_used_at = moment
            row.sliding_written_at = moment
        return {
            "status": "ok", "owner": row.owner, "token_id": row.id, "expires_at": row.expires_at,
        }, needs_slide and not write

    def list_tokens(self, owner):
        with self._transaction() as session:
            self._require_owner(session, owner)
            rows = session.scalars(select(DeviceToken).where(
                DeviceToken.owner == owner, DeviceToken.revoked_at.is_(None),
            ).order_by(DeviceToken.created_at, DeviceToken.id))
            return [{
                "token_id": row.id, "device_name": row.device_name, "created_at": row.created_at,
                "last_used_at": row.last_used_at, "expires_at": row.expires_at,
            } for row in rows]

    def revoke(self, owner, token_id, *, actor_token_id=None):
        if actor_token_id is not None:
            if not ids_match(actor_token_id, token_id):
                raise DeviceTokenError(403, "budget_forbidden")
        elif not isinstance(token_id, str) or _UUID.fullmatch(token_id) is None:
            raise DeviceTokenError(400, "budget_invalid_request")
        with self._transaction() as session:
            if actor_token_id is None:
                self._require_owner(session, owner)
            row = session.get(DeviceToken, token_id)
            if row is None or row.owner != owner:
                if actor_token_id is not None:
                    raise DeviceTokenError(403, "budget_forbidden")
                return {"ok": True}
            if row.revoked_at is None:
                moment = self._moment()
                if actor_token_id is not None:
                    if moment >= min(row.expires_at, row.hard_expires_at):
                        raise DeviceTokenError(401, "device_token_expired")
                    write_device_audit(session, owner, token_id, "receipt_device_revoke", moment)
                row.revoked_at = moment
            return {"ok": True}
