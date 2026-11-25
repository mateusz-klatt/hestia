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
import ipaddress
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
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .. import auth, authz, store_sql
from ..db import BudgetAnalyticsAudit, BudgetConnection
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
ACTOR_REQUEST_KEY = "hestia_device_actor"
PAIR_PATH = PREFIX + "/device-token/pair-code"
PAIR_CANCEL_PATH = PREFIX + "/device-token/pair-code-cancel"
REDEEM_PATH = PREFIX + "/device-token/redeem"
LOGIN_PATH = PREFIX + "/device-token/login"
LIST_PATH = PREFIX + "/device-token/list"
REVOKE_PATH = PREFIX + "/device-token/revoke"
EXCHANGE_PATH = "/api/auth/device-token"
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
NATIVE_READ_ALLOWED = frozenset({
    ("GET", "/api/budget/status"),
    ("GET", "/api/budget/analytics/monthly-budget"),
    ("GET", "/api/budget/analytics/monthly-budget/charts"),
    ("GET", "/api/budget/analytics/transactions"),
    ("GET", "/api/budget/analytics/overview"),
    ("GET", "/api/budget/analytics/categories"),
})

PAIR_TTL = 300
MAX_ACTIVE_CODES = 3
MAX_ACTIVE_TOKENS = 5
NEW_DEVICE_LOGIN = "receipt_device_login"
DEVICE_ROTATE = "receipt_device_rotate"
SLIDE_TTL = 90 * 86400
HARD_TTL = 180 * 86400
SLIDE_WRITE_INTERVAL = 3600
PARSE_LIMIT = 30
PARSE_WINDOW = 60
REDEEM_IP_LIMIT = 10
REDEEM_GLOBAL_LIMIT = 300
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
DEVICE_TOKEN_PREFIX = "hdt1."
_DEVICE_TOKEN_DOMAIN = b"hestia/auth/device-token/v1"
_OWNER_KEY_DOMAIN = b"hestia/auth/owner-key/v1"
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


def _owner_eligible(session, user):
    store_sql.auth_session_phase(session)
    phase = authz.require_supported_policy_phase(session)
    return (
        user is not None
        and not user.disabled
        and (phase == authz.POLICY_PHASE_CUSTOM or user.role == "admin")
        and (phase == authz.POLICY_PHASE_LEGACY or bool(user.budget_owner))
    )


def _issued_mask(row):
    if row.issued_permissions is None:
        return authz.LEGACY_RECEIPT_PERMISSIONS if row.actor in (None, row.owner) else frozenset()
    try:
        values = json.loads(row.issued_permissions)
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(values, list) or any(
        not isinstance(value, str) or value not in authz.CATALOG for value in values
    ):
        return frozenset()
    return frozenset(values)


def _legacy_bearer_allowed(session, row):
    return (
        row.actor in (None, row.owner)
        and authz.LEGACY_RECEIPT_PERMISSIONS <= _issued_mask(row)
        and authz.has_legacy_receipt_profile(session, row.owner)
    )


def _owner_login_state(session, username):
    store_sql.auth_session_phase(session)
    phase = authz.require_supported_policy_phase(session)
    criteria = [
        User.username == username,
        User.disabled.is_(False),
        select(BudgetConnection.id).where(BudgetConnection.owner == username).exists(),
    ]
    if phase != authz.POLICY_PHASE_CUSTOM:
        criteria.append(User.role == "admin")
    if phase != authz.POLICY_PHASE_LEGACY:
        criteria.append(User.budget_owner.is_(True))
    row = session.execute(select(User.password_hash, User.credential_generation).where(*criteria)).first()
    if row is None or (
        phase != authz.POLICY_PHASE_LEGACY
        and not authz.has_legacy_receipt_profile(session, username)
    ):
        return None
    return row[0], int(row[1])


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


def _modern_bearer(value):
    if not isinstance(value, str) or not value.startswith(DEVICE_TOKEN_PREFIX):
        return None
    encoded = value[len(DEVICE_TOKEN_PREFIX):]
    return value if _bearer_token(encoded) is not None else None


def _session_mac(secret, domain, value):
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=domain).derive(secret)
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def owner_key(secret, owner):
    digest = hmac.new(
        HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_OWNER_KEY_DOMAIN).derive(secret),
        owner.encode("utf-8"), hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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
    # Charge malformed input to its source too. IPv6 privacy addresses share
    # the same /64 quota so rotating the host part cannot bypass the backoff.
    try:
        address = ipaddress.ip_address(ip)
        ip = str(ipaddress.ip_network((address, 64), strict=False)) if address.version == 6 else str(address)
    except ValueError:
        pass
    return _charge(
        ((("redeem", "ip", ip), REDEEM_IP_LIMIT, REDEEM_BACKOFF_AFTER),),
        moment, REDEEM_WINDOW,
    )


def redeem_global_limited(moment):
    """Charge the shared quota only after the request's pairing code is well formed."""
    return _charge(((("redeem", "global"), REDEEM_GLOBAL_LIMIT, None),), moment, REDEEM_WINDOW)


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


def write_device_audit(session, owner, token_id, action, moment, *, actor=None, device_name=None):
    """Record device activity in the owner's budget audit."""
    details = {"actor": "device:" + token_id} if actor is None else {
        "actor": actor, "device_name": device_name, "token_id": token_id,
    }
    session.add(BudgetAnalyticsAudit(
        owner=owner, subject_type="receipt_device_token", subject_id=token_id, action=action,
        before_json="null", after_json=json.dumps(details), changed_at=moment,
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


def open_store(*, clock=None, session_secret=None):
    from .views import BudgetUsersBackendRequired
    from ..db import init_db

    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        raise BudgetUsersBackendRequired()
    engine, sessions = init_db()
    with sessions() as session:
        store_sql.auth_session_phase(session)
        authz.require_supported_policy_phase(session)
    if not store_sql.is_users_db_authoritative(engine):
        raise BudgetUsersBackendRequired()
    if session_secret is None:
        session_secret = os.environ.get("HESTIA_SESSION_SECRET", "").encode("utf-8")
    return DeviceTokenStore(sessions, clock=clock or now, session_secret=session_secret)


def decide(method, path, headers, cookies, secret, moment):
    """Classify a bearer without logging it.

    Login sessions pass through to the web session policy.
    Device-shaped credentials are forbidden outside the receipts allowlist.
    """
    from .views import BudgetUsersBackendRequired

    receipts = receipts_path(path)
    try:
        has_cookie = COOKIE in cookies
        has_authorization = "Authorization" in headers
    except TypeError:
        return {"action": "pass"}
    token = _bearer(headers)
    device_shaped = _modern_bearer(token) is not None or _bearer_token(token) is not None
    if (receipts or device_shaped) and has_cookie and has_authorization:
        return {"action": "response", "status": 400, "code": "budget_auth_ambiguous"}
    if receipts and not budget_http_enabled():
        return {"action": "response", "status": 404, "code": "budget_disabled"}
    if token is None:
        return {"action": "pass"}
    if auth.decode_session(token, now=moment, secret=secret) is not None:
        return {"action": "pass"}
    modern = _modern_bearer(token) is not None
    allow = (method, path) in ALLOWED or (modern and (method, path) in NATIVE_READ_ALLOWED)
    if not modern and _bearer_token(token) is None:
        if (method, path) in ALLOWED:
            return {"action": "response", "status": 401, "code": "device_token_invalid"}
        return {"action": "pass"}
    if not allow:
        return {"action": "response", "status": 403, "code": "budget_forbidden"}
    try:
        found = open_store(clock=lambda: moment, session_secret=secret).identify(
            token, slide=path != REVOKE_PATH, require_profile=path != REVOKE_PATH,
        )
    except (authz.PolicyPhaseUnsupported, store_sql.SessionPhaseUnsupported):
        return {"action": "response", "status": 503, "code": "authorization_bridge_incompatible"}
    except (BudgetUsersBackendRequired, BudgetConfigError):
        found = {"status": "unknown"}
    except Exception as exc:
        log.error("device token check failed: %s", type(exc).__name__)
        return {"action": "response", "status": 503, "code": "budget_unavailable"}
    if found.get("status") == "unknown":
        return {"action": "response", "status": 401, "code": "device_token_invalid"}
    if found.get("status") == "forbidden":
        return {"action": "response", "status": 403, "code": "budget_forbidden"}
    if found.get("status") == "revoked" and path != REVOKE_PATH:
        return {"action": "response", "status": 401, "code": "device_token_revoked"}
    if found.get("status") == "expired":
        return {"action": "response", "status": 401, "code": "device_token_expired"}
    if found.get("status") not in ("ok", "revoked") or not found.get("owner") or not found.get("token_id"):
        return {"action": "response", "status": 401, "code": "device_token_invalid"}
    if (method, path) in NATIVE_READ_ALLOWED and "budget.view" not in found.get("permissions", ()):
        return {"action": "response", "status": 403, "code": "budget_forbidden"}
    return {
        "action": "accept", "owner": found["owner"], "actor": found["actor"],
        "token_id": found["token_id"], "permissions": found.get("permissions", ()),
    }


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
    def __init__(self, sessions, *, clock, session_secret=b""):
        self.sessions = sessions
        self.clock = clock
        self.session_secret = session_secret

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
        if not _owner_eligible(session, user):
            raise DeviceTokenError(403, "budget_forbidden")
        return user

    def _owner_ok(self, session, owner):
        return _owner_eligible(session, session.get(User, owner))

    def _require_legacy_receipts(self, session, owner):
        self._require_owner(session, owner)
        if (authz.policy_phase(session) != authz.POLICY_PHASE_LEGACY
                and not authz.has_legacy_receipt_profile(session, owner)):
            raise DeviceTokenError(403, "budget_forbidden")

    def issue_code(self, owner):
        root = _rule_key()
        with self._transaction() as session:
            self._require_legacy_receipts(session, owner)
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
            return {"code_id": row.id, "code": code, "expires_at": row.expires_at,
                    "qr_payload": "hestia-pair:" + code}

    def cancel_code(self, owner, code_id):
        with self._transaction() as session:
            self._require_owner(session, owner)
            moment = self._moment()
            session.execute(update(PairCode).where(
                PairCode.id == code_id, PairCode.owner == owner,
                PairCode.consumed_at.is_(None), PairCode.expires_at > moment,
            ).values(consumed_at=moment))
        return {"ok": True}

    def login(self, username, password, name):
        with self.sessions() as session:
            state = _owner_login_state(session, username)
        stored = None if state is None else state[0]
        # Every valid-shaped attempt does one scrypt. Ineligible users take the
        # same dummy-hash path as unknown users, even with a correct password.
        if not auth.authenticate(username, password, {username: stored}):
            return None
        with self._transaction() as session:
            # Recheck under the same write lock used for account mutations. A
            # password reset, generation bump, or demotion during scrypt cannot issue a new token.
            if _owner_login_state(session, username) != state:
                return None
            return self._issue_token(session, username, name, self._moment(), _rule_key(), rotate=True)

    def redeem(self, code, device_name):
        normalized = normalize_code(code)
        name = _device_name(device_name)
        if normalized is None or name is None:
            return None
        root = _rule_key()
        digest = _mac(root, _CODE_DOMAIN, normalized)
        with self._transaction() as session:
            store_sql.auth_session_phase(session)
            phase = authz.require_supported_policy_phase(session)
            moment = self._moment()
            conditions = [
                PairCode.code_hmac == digest,
                PairCode.consumed_at.is_(None),
                PairCode.expires_at > moment,
                User.disabled.is_(False),
            ]
            if phase != authz.POLICY_PHASE_CUSTOM:
                conditions.append(User.role == "admin")
            if phase != authz.POLICY_PHASE_LEGACY:
                conditions.append(User.budget_owner.is_(True))
            row = session.scalar(select(PairCode).join(User, PairCode.owner == User.username).where(*conditions))
            if not _accepted(None if row is None else row.code_hmac, digest):
                return None
            if (phase != authz.POLICY_PHASE_LEGACY
                    and not authz.has_legacy_receipt_profile(session, row.owner)):
                return None
            row.consumed_at = moment
            return self._issue_token(session, row.owner, name, moment, root)

    def _make_token_room(self, session, owner, name, moment, *, rotate):
        self_slot = or_(DeviceToken.actor == owner, DeviceToken.actor.is_(None))
        rotated = False
        if rotate:
            # A name alone is not proof of an earlier new-device alert.
            # Unaudited and expired credentials must still produce an alert.
            replacing = session.scalar(select(DeviceToken).where(
                DeviceToken.owner == owner, DeviceToken.device_name == name, self_slot,
                DeviceToken.revoked_at.is_(None),
                DeviceToken.expires_at > moment, DeviceToken.hard_expires_at > moment,
            ).order_by(DeviceToken.created_at.desc(), DeviceToken.id.desc()).limit(1))
            # Only the token actually being replaced decides whether this is a
            # rotation: an older password-login sibling must not hide the alert.
            rotated = replacing is not None and session.scalar(select(BudgetAnalyticsAudit.id).where(
                BudgetAnalyticsAudit.owner == owner,
                BudgetAnalyticsAudit.subject_id == replacing.id,
                BudgetAnalyticsAudit.subject_type == "receipt_device_token",
                BudgetAnalyticsAudit.action == NEW_DEVICE_LOGIN,
            ).limit(1)) is not None
            if replacing is None:
                active = session.scalar(select(func.count()).select_from(DeviceToken).where(
                    DeviceToken.owner == owner, self_slot, DeviceToken.revoked_at.is_(None),
                    DeviceToken.expires_at > moment, DeviceToken.hard_expires_at > moment,
                ))
                if active >= MAX_ACTIVE_TOKENS:
                    raise DeviceTokenError(409, "device_token_limit_reached")
            else:
                replacing.revoked_at = moment
        overflow = session.scalars(select(DeviceToken).where(
            DeviceToken.owner == owner, self_slot, DeviceToken.revoked_at.is_(None),
            DeviceToken.expires_at > moment, DeviceToken.hard_expires_at > moment,
        ).order_by(func.coalesce(DeviceToken.last_used_at, DeviceToken.created_at).desc(),
                   DeviceToken.created_at.desc(), DeviceToken.id).offset(MAX_ACTIVE_TOKENS - 1))
        for row in overflow:
            row.revoked_at = moment
        return rotated

    def _issue_token(self, session, owner, name, moment, root, *, rotate=False):
        rotated = self._make_token_room(session, owner, name, moment, rotate=rotate)
        issued = {}

        def factory():
            token = base64.urlsafe_b64encode(os.urandom(_TOKEN_BYTES)).rstrip(b"=").decode("ascii")
            issued["token"] = token
            return DeviceToken(
                id=str(uuid.uuid4()), owner=owner, actor=owner, device_name=name,
                token_hmac=_mac(root, _TOKEN_DOMAIN, token),
                created_at=moment, expires_at=moment + SLIDE_TTL, hard_expires_at=moment + HARD_TTL,
                sliding_written_at=moment, last_used_at=None, revoked_at=None,
            )

        row = _insert_unique(session, factory)
        write_device_audit(session, owner, row.id, NEW_DEVICE_LOGIN, moment)
        if rotated:
            write_device_audit(session, owner, row.id, DEVICE_ROTATE, moment)
        expires_at = datetime.fromtimestamp(row.expires_at, timezone.utc).isoformat(timespec="seconds")
        return {"token": issued["token"], "token_id": row.id, "scope": "receipts", "expires_at": expires_at}

    def exchange(self, actor, name, prior_token=None, *, generation):
        name = _device_name(name)
        if name is None or not self.session_secret:
            raise DeviceTokenError(400, "budget_invalid_request")
        with self._transaction() as session:
            if store_sql.auth_session_phase(session) != store_sql.SESSION_PHASE_V2:
                raise DeviceTokenError(401, "native_session_required")
            authz.require_supported_policy_phase(session)
            moment = self._moment()
            actor_row = session.get(User, actor)
            if actor_row is not None and actor_row.credential_generation != generation:
                raise DeviceTokenError(401, "native_session_required")
            if actor_row is None or actor_row.disabled:
                raise DeviceTokenError(403, "budget_forbidden")
            owner = authz.resolve_budget_target(session, actor)
            if owner is None:
                raise DeviceTokenError(403, "budget_forbidden")
            live = authz.effective_permissions(session, actor, owner=owner)
            issued = live & authz.DEVICE_ELIGIBLE_PERMISSIONS
            if not issued:
                raise DeviceTokenError(403, "budget_forbidden")
            previous = None
            if prior_token is not None:
                modern = _modern_bearer(prior_token) is not None
                legacy = _bearer_token(prior_token)
                if not modern and legacy is None:
                    raise DeviceTokenError(409, "device_token_prior_invalid")
                try:
                    digest = (
                        _session_mac(self.session_secret, _DEVICE_TOKEN_DOMAIN, prior_token)
                        if modern else _mac(_rule_key(), _TOKEN_DOMAIN, legacy)
                    )
                except BudgetConfigError:
                    raise DeviceTokenError(409, "device_token_prior_invalid") from None
                previous = session.scalar(select(DeviceToken).where(DeviceToken.token_hmac == digest))
                if not _accepted(None if previous is None else previous.token_hmac, digest):
                    raise DeviceTokenError(409, "device_token_prior_invalid")
                if (
                    previous.revoked_at is not None
                    or moment >= min(previous.expires_at, previous.hard_expires_at)
                    or previous.owner != owner
                    or (previous.actor or previous.owner) != actor
                ):
                    raise DeviceTokenError(409, "device_token_prior_invalid")
                ceiling = _issued_mask(previous)
                if previous.actor is None and previous.owner == actor:
                    ceiling = authz.LEGACY_RECEIPT_PERMISSIONS
                issued &= ceiling
                if not issued:
                    raise DeviceTokenError(403, "budget_forbidden")

            token = DEVICE_TOKEN_PREFIX + base64.urlsafe_b64encode(os.urandom(_TOKEN_BYTES)).rstrip(b"=").decode("ascii")
            digest = _session_mac(self.session_secret, _DEVICE_TOKEN_DOMAIN, token)
            encoded_permissions = json.dumps(sorted(issued))
            if previous is not None:
                previous.actor = actor
                previous.device_name = name
                previous.token_hmac = digest
                previous.issued_permissions = encoded_permissions
                previous.expires_at = min(moment + SLIDE_TTL, previous.hard_expires_at)
                previous.sliding_written_at = moment
                previous.last_used_at = None
                row = previous
            else:
                self_slot = or_(DeviceToken.actor == actor, DeviceToken.actor.is_(None)) if actor == owner else DeviceToken.actor == actor
                active = session.scalar(select(func.count()).select_from(DeviceToken).where(
                    DeviceToken.owner == owner, self_slot, DeviceToken.revoked_at.is_(None),
                    DeviceToken.expires_at > moment, DeviceToken.hard_expires_at > moment,
                ))
                if active >= MAX_ACTIVE_TOKENS:
                    raise DeviceTokenError(409, "device_token_limit_reached")
                def factory():
                    return DeviceToken(
                        id=str(uuid.uuid4()), owner=owner, actor=actor, device_name=name,
                        token_hmac=digest, issued_permissions=encoded_permissions,
                        created_at=moment, expires_at=moment + SLIDE_TTL, hard_expires_at=moment + HARD_TTL,
                        sliding_written_at=moment, last_used_at=None, revoked_at=None,
                    )

                row = _insert_unique(session, factory)
                write_device_audit(
                    session, owner, row.id, NEW_DEVICE_LOGIN, moment, actor=actor, device_name=name,
                )
            expires_at = datetime.fromtimestamp(row.expires_at, timezone.utc).isoformat(timespec="seconds")
            return {
                "token": token, "token_id": row.id,
                "scope": "device" if "budget.view" in issued else "receipts",
                "actor": actor, "owner": owner, "owner_key": owner_key(self.session_secret, owner),
                "issued_permissions": sorted(issued), "expires_at": expires_at,
            }

    def identify(self, token, *, slide, require_profile=True):
        modern = _modern_bearer(token) is not None
        canonical = token if modern else _bearer_token(token)
        if canonical is None:
            _accepted(None, _BLANK)
            return {"status": "unknown"}
        digest = (
            _session_mac(self.session_secret, _DEVICE_TOKEN_DOMAIN, canonical)
            if modern else _mac(_rule_key(), _TOKEN_DOMAIN, canonical)
        )
        with self.sessions() as session:
            result, needs_write = self._inspect_token(
                session, digest, slide=slide, write=False, require_profile=require_profile, modern=modern,
            )
        if needs_write:
            with self._transaction() as session:
                result, _ = self._inspect_token(
                    session, digest, slide=slide, write=True, require_profile=require_profile, modern=modern,
                )
        return result

    def _inspect_token(self, session, digest, *, slide, write, require_profile, modern=False):
        store_sql.auth_session_phase(session)
        authz.require_supported_policy_phase(session)
        row = session.scalar(select(DeviceToken).where(DeviceToken.token_hmac == digest))
        if not _accepted(None if row is None else row.token_hmac, digest):
            return {"status": "unknown"}, False
        if row.revoked_at is not None:
            return {
                "status": "revoked", "owner": row.owner, "actor": row.actor or row.owner,
                "token_id": row.id,
            }, False
        moment = self._moment()
        actor = row.actor or row.owner
        actor_row = session.get(User, actor)
        if not self._owner_ok(session, row.owner) or (modern and (actor_row is None or actor_row.disabled)):
            if write:
                row.revoked_at = moment
            return {
                "status": "revoked", "owner": row.owner, "actor": actor, "token_id": row.id,
            }, not write
        if moment >= min(row.expires_at, row.hard_expires_at):
            return {"status": "expired", "token_id": row.id}, False
        permissions = _issued_mask(row)
        needs_narrow = False
        if require_profile and modern:
            allowed = permissions & authz.effective_permissions(session, actor, owner=row.owner)
            allowed &= authz.DEVICE_ELIGIBLE_PERMISSIONS
            if not allowed:
                if write:
                    row.revoked_at = moment
                return {
                    "status": "forbidden", "owner": row.owner, "actor": actor, "token_id": row.id,
                }, not write
            if allowed != permissions:
                needs_narrow = True
                permissions = allowed
                if write:
                    row.issued_permissions = json.dumps(sorted(permissions))
        elif (require_profile and authz.policy_phase(session) != authz.POLICY_PHASE_LEGACY
                and not _legacy_bearer_allowed(session, row)):
            return {
                "status": "forbidden", "owner": row.owner, "actor": actor, "token_id": row.id,
            }, False
        needs_slide = slide and (row.last_used_at is None or moment - row.sliding_written_at >= SLIDE_WRITE_INTERVAL)
        if needs_slide and write:
            row.expires_at = min(moment + SLIDE_TTL, row.hard_expires_at)
            row.last_used_at = moment
            row.sliding_written_at = moment
        return {
            "status": "ok", "owner": row.owner, "actor": actor,
            "token_id": row.id, "expires_at": row.expires_at, "permissions": permissions,
        }, (needs_slide or needs_narrow) and not write

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
            store_sql.auth_session_phase(session)
            authz.require_supported_policy_phase(session)
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
