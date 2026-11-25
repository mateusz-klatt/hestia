"""Origin-bound, single-use QR login codes for native sessions."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, authz, store_sql
from .db import AppMeta, NativeLoginCode, User
from .budget import device_token

CODE_PATH = "/api/auth/qr-code"
REDEEM_PATH = "/api/auth/qr-redeem"
CODE_TTL = 300.0
MAX_ACTIVE_CODES = 3
MAX_ORIGIN_BYTES = 45
_CODE_BYTES = 14
_CODE_LENGTH = 19
_KEY_INFO = b"hestia/auth/qr-login/v1"
_INSERT_ATTEMPTS = 3
_BLANK = "0" * 64


class QrLoginError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(code)


def _key(secret: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(secret)


def _digest(secret: bytes, origin: str, code: str) -> str:
    return hmac.new(_key(secret), (origin + "\0" + code).encode("ascii"), hashlib.sha256).hexdigest()


def _new_code() -> str:
    return base64.urlsafe_b64encode(os.urandom(_CODE_BYTES)).rstrip(b"=").decode("ascii")


def normalize_code(value) -> str | None:
    if not isinstance(value, str) or len(value) != _CODE_LENGTH or not value.isascii():
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        return None
    if len(raw) != _CODE_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != value:
        return None
    return value


def redeem_limited(ip: str, moment: float) -> int | None:
    try:
        address = ipaddress.ip_address(ip)
        source = str(ipaddress.ip_network((address, 64), strict=False)) if address.version == 6 else str(address)
    except ValueError:
        source = ip
    return device_token._charge(
        ((("qr", "ip", source), device_token.REDEEM_IP_LIMIT, device_token.REDEEM_BACKOFF_AFTER),),
        moment, device_token.REDEEM_WINDOW,
    )


def redeem_global_limited(moment: float) -> int | None:
    return device_token._charge(
        ((("qr", "global"), device_token.REDEEM_GLOBAL_LIMIT, None),),
        moment, device_token.REDEEM_WINDOW,
    )


def _origin_bytes(origin: str) -> bytes:
    try:
        return origin.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        return b""


def issue(username: str, origin: str, generation: int, *, now: float, secret: bytes) -> str:
    encoded_origin = _origin_bytes(origin)
    if not encoded_origin:
        raise QrLoginError(400, "qr_origin_mismatch")
    if len(encoded_origin) > MAX_ORIGIN_BYTES:
        raise QrLoginError(400, "qr_origin_too_long")
    engine = store_sql._immediate_engine()
    try:
        with Session(engine) as session, session.begin():
            if store_sql.auth_session_phase(session) != store_sql.SESSION_PHASE_V2:
                raise QrLoginError(401, "qr_code_invalid")
            authz.require_supported_policy_phase(session)
            user = session.get(User, username)
            if (
                session.get(AppMeta, store_sql._USERS_AUTHORITATIVE) is None
                or user is None
                or user.disabled
                or user.credential_generation != generation
            ):
                raise QrLoginError(401, "qr_code_invalid")
            active = session.scalars(select(NativeLoginCode).where(
                NativeLoginCode.username == username,
                NativeLoginCode.consumed_at.is_(None),
                NativeLoginCode.expires_at > now,
            ).order_by(NativeLoginCode.created_at, NativeLoginCode.id)).all()
            for stale in active[MAX_ACTIVE_CODES - 1:]:
                stale.consumed_at = now
            for _attempt in range(_INSERT_ATTEMPTS):
                code = _new_code()
                row = NativeLoginCode(
                    id=str(uuid.uuid4()), username=username,
                    code_hmac=_digest(secret, origin, code), generation=generation,
                    created_at=now, expires_at=now + CODE_TTL, consumed_at=None,
                )
                try:
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    continue
                return "hestia-login:" + origin + "#" + code
            raise QrLoginError(503, "auth_origin_unavailable")
    finally:
        engine.dispose()


def redeem(origin: str, code: str, *, now: float, secret: bytes) -> dict | None:
    canonical = normalize_code(code)
    digest = _digest(secret, origin, canonical) if canonical is not None else _BLANK
    engine = store_sql._immediate_engine()
    try:
        with Session(engine) as session, session.begin():
            if store_sql.auth_session_phase(session) != store_sql.SESSION_PHASE_V2:
                return None
            authz.require_supported_policy_phase(session)
            row = session.scalar(select(NativeLoginCode).where(NativeLoginCode.code_hmac == digest))
            stored = _BLANK if row is None else row.code_hmac
            accepted = hmac.compare_digest(stored, digest)
            user = None if row is None else session.get(User, row.username)
            if (
                not accepted
                or row is None
                or row.consumed_at is not None
                or row.expires_at <= now
                or user is None
                or user.disabled
                or user.credential_generation != row.generation
                or session.get(AppMeta, store_sql._USERS_AUTHORITATIVE) is None
            ):
                return None
            row.consumed_at = now
            token = auth.make_session(
                user.username, now=now, secret=secret,
                generation=user.credential_generation, purpose=auth.SESSION_PURPOSE_NATIVE_QR,
            )
            owner = authz.resolve_budget_target(session, user.username)
            if owner is not None:
                device_token.write_device_audit(
                    session, owner, row.id, device_token.NEW_DEVICE_LOGIN, now,
                    actor=user.username, device_name="Vesta QR",
                )
            return {"ok": True, "user": user.username, "token": token}
    finally:
        engine.dispose()
