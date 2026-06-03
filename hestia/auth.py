"""App-level per-user authentication — stdlib only (no new runtime deps).

Three primitives, deliberately pure so they unit-test without a clock, filesystem, or env:

* ``hash_password`` / ``verify_password`` — ``hashlib.scrypt`` (memory-hard) with a per-password random
  salt; the stored form is ``scrypt$n$r$p$salt_b64$hash_b64``. Verification is constant-time
  (``hmac.compare_digest``) and never raises on a malformed stored value (returns ``False``).
* ``make_session`` / ``verify_session`` — an HMAC-SHA256-signed ``username|expiry`` token. Verification
  checks the signature (constant-time) AND the expiry, returning the username or ``None``. The signing
  ``secret`` is passed in (the web layer reads ``HESTIA_SESSION_SECRET`` and fails closed if it is empty).
* ``load_users`` / ``authenticate`` — the users store is a JSON map ``{username: stored_hash}`` on the
  ``/data`` volume (``HESTIA_AUTH_USERS_FILE``). ``authenticate`` runs a dummy hash for an unknown user so
  a missing account cannot be distinguished from a wrong password by timing.

A small CLI (``python -m hestia.auth add <user>``) hashes a prompted password into the store, so the
operator creates accounts without the daemon. NOTHING imports this module yet — wiring it into the web
layer (login endpoint + auth middleware) is a separate, deploy-coordinated step.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

# scrypt cost — n=2**14/r=8/p=1 is ~16 MiB and ~50 ms per hash here: cheap for an interactive login,
# expensive for offline brute force. maxmem is set explicitly so the call can't hit OpenSSL's default cap.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16
_DKLEN = 32

# A fixed dummy password whose hash is precomputed (below) so authenticate() does EXACTLY ONE scrypt
# whether or not the username exists — equalising login timing so a missing account can't be enumerated.
_DUMMY_PASSWORD = "hestia-dummy-password"

SESSION_TTL = 30 * 24 * 3600.0   # 30-day "remember me" cookie

DEFAULT_USERS_FILE = "/data/users.json"


def _b64e(raw: bytes) -> str:
    # URL-safe (no '+' '/') so the session token is a clean cookie value; '$'-free so the stored-hash split holds.
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """Hash ``password`` to the storable ``scrypt$n$r$p$salt$hash`` string (fresh random salt)."""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM, dklen=_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


# Precomputed ONCE at import: authenticate() verifies an unknown user's password against this so it always
# runs a single scrypt (matching the real path's timing) — never as a successful login (see authenticate).
_DUMMY_STORED = hash_password(_DUMMY_PASSWORD)


def verify_password(password: str, stored: str) -> bool:
    """``True`` iff ``password`` matches the ``scrypt$…`` ``stored`` value. Constant-time; never raises
    (a malformed/foreign/non-string ``stored`` returns ``False``)."""
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    try:
        algo, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n_s), r=int(r_s),
                            p=int(p_s), maxmem=_SCRYPT_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError, OverflowError):   # wrong field count / bad base64 / bad ints / out-of-range params
        return False
    return hmac.compare_digest(dk, expected)


def make_session(username: str, *, now: float, secret: bytes, ttl: float = SESSION_TTL) -> str:
    """An HMAC-SHA256-signed ``username|expiry`` session token (``payload_b64.sig_b64``)."""
    payload = f"{username}|{int(now + ttl)}".encode("utf-8")
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_session(token: str, *, now: float, secret: bytes) -> "str | None":
    """The username carried by a valid, unexpired, correctly-signed ``token`` — else ``None``. The
    signature is checked constant-time BEFORE the expiry so a forged token is rejected the same way."""
    try:
        payload_b64, sig_b64 = token.split(".")
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        username, expiry_s = payload.decode("utf-8").rsplit("|", 1)
        expiry = int(expiry_s)
    except (ValueError, UnicodeDecodeError):
        return None
    if now >= expiry:
        return None
    return username


def users_path() -> Path:
    return Path(os.environ.get("HESTIA_AUTH_USERS_FILE", DEFAULT_USERS_FILE))


def load_users(path: "Path | None" = None) -> dict:
    """The ``{username: stored_hash}`` map, or ``{}`` if the file is missing/unreadable/not an object."""
    path = users_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def authenticate(username: str, password: str, users: dict) -> bool:
    """``True`` iff ``username`` exists in ``users`` and ``password`` matches its hash. Runs a dummy hash
    for an unknown user so timing can't reveal which usernames exist."""
    if not isinstance(username, str) or not isinstance(password, str):
        return False                               # malformed login input (raw JSON) -> fail closed, never raise
    stored = users.get(username)
    if not isinstance(stored, str):
        verify_password(password, _DUMMY_STORED)   # one scrypt to equalise timing — NEVER a successful login
        return False
    return verify_password(password, stored)


def save_user_json(username: str, password_hash: str, path: "Path | None" = None) -> Path:
    """Atomically upsert one user into the JSON store (per-PID temp + fsync + os.replace). The
    per-PID temp name keeps two concurrent ``add`` runs from clobbering the same temp file (each
    os.replace is atomic; last wins). Returns the file written."""
    target = users_path() if path is None else path
    users = load_users(target)
    users[username] = password_hash
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(users, indent=2, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return target


def _cli(argv: list, *, prompt=getpass.getpass, path: "Path | None" = None) -> int:
    """``add <username>`` — prompt a password (twice), hash it, and upsert it into the AUTHORITATIVE
    users store: the SQLite DB when it is in charge (HESTIA_PERSIST=sqlite and the users table is cut
    over), else the JSON file. Writing where the daemon reads avoids a silent no-op after cutover. A
    test ``path`` forces the JSON file (and keeps this module import-clean of the DB layer)."""
    if len(argv) != 2 or argv[0] != "add":
        print("usage: python -m hestia.auth add <username>", file=sys.stderr)
        return 2
    username = argv[1]
    if not username or "|" in username or "/" in username:
        print("invalid username (no empty, '|' or '/')", file=sys.stderr)
        return 2
    first = prompt("password: ")
    if not first:
        print("empty password", file=sys.stderr)
        return 1
    if first != prompt("repeat:   "):
        print("passwords differ", file=sys.stderr)
        return 1
    password_hash = hash_password(first)
    if path is None and os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
        from . import store_sql
        if store_sql.users_db_authoritative():
            store_sql.set_user_db(username, password_hash)
            print(f"saved user {username!r} to the SQLite DB")
            return 0
    target = save_user_json(username, password_hash, path)
    print(f"saved user {username!r} to {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli(sys.argv[1:]))
