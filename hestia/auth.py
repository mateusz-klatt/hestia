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
from dataclasses import dataclass
import getpass
import hashlib
import hmac
import ipaddress
import json
import math
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
SESSION_PURPOSE_BROWSER = "browser"
SESSION_PURPOSE_NATIVE = "native"
SESSION_PURPOSES = frozenset({SESSION_PURPOSE_BROWSER, SESSION_PURPOSE_NATIVE})
_V2_MAC_PREFIX = b"hestia/session/v2\0"
_V2_FIELDS = frozenset({"username", "expiry", "generation", "purpose"})
LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 60
_MAX_LOGIN_BUCKETS = 4096
_MAX_LOGIN_SOURCE_BUCKETS = 64

DEFAULT_USERS_FILE = "/data/users.json"


class LoginLimiter:
    """Bounded attempt budget shared by password endpoints on one web event loop."""

    def __init__(self):
        self._attempts = {}
        self._sources = {}

    def take(self, username: str, ip: str, now: float) -> int | None:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            address = None
        if isinstance(address, ipaddress.IPv6Address):
            address = address.ipv4_mapped or ipaddress.ip_network((address, 64), strict=False)
        source = str(address)
        key = (hashlib.sha256(username.encode("utf-8", errors="surrogatepass")).digest(), source)
        # Entries are inserted in deadline order. Updating a count keeps its position.
        while self._attempts:
            oldest = next(iter(self._attempts))
            if self._attempts[oldest][0] > now:
                break
            del self._attempts[oldest]
            source_buckets = self._sources[oldest[1]]
            del source_buckets[oldest]
            if not source_buckets:
                del self._sources[oldest[1]]
        attempt = self._attempts.get(key)
        if attempt is None:
            source_buckets = self._sources.get(source, {})
            if len(source_buckets) >= _MAX_LOGIN_SOURCE_BUCKETS:
                return math.ceil(next(iter(source_buckets.values())) - now)
            if len(self._attempts) >= _MAX_LOGIN_BUCKETS:
                deadline, _ = next(iter(self._attempts.values()))
                return math.ceil(deadline - now)
            attempt = now + LOGIN_WINDOW, 0
            self._sources.setdefault(source, {})[key] = attempt[0]
        deadline, count = attempt
        if count >= LOGIN_ATTEMPTS:
            return math.ceil(deadline - now)
        self._attempts[key] = deadline, count + 1
        return None


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


@dataclass(frozen=True)
class SessionEnvelope:
    """Decoded session metadata without account or database authorization."""

    username: str
    expiry: int
    generation: int
    version: int
    purpose: str | None


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    if not text or "=" in text or not text.isascii():
        raise ValueError("invalid unpadded base64url")
    padded = text + ("=" * ((4 - len(text) % 4) % 4))
    raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    if _b64u_encode(raw) != text:
        raise ValueError("non-canonical base64url")
    return raw


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def make_session(
    username: str,
    *,
    now: float,
    secret: bytes,
    ttl: float = SESSION_TTL,
    generation: int | None = None,
    purpose: str | None = None,
) -> str:
    """Issue a legacy v1 token or (when generation and purpose are supplied) a v2 session envelope."""
    if (generation is None) != (purpose is None):
        raise ValueError("generation and purpose must be supplied together")
    if generation is None:
        payload = f"{username}|{int(now + ttl)}".encode("utf-8")
        sig = hmac.new(secret, payload, hashlib.sha256).digest()
        return f"{_b64e(payload)}.{_b64e(sig)}"
    if not isinstance(username, str) or not username:
        raise ValueError("invalid username")
    if type(generation) is not int or generation < 0:
        raise ValueError("generation must be a nonnegative int")
    if purpose not in SESSION_PURPOSES:
        raise ValueError(f"invalid session purpose: {purpose!r}")
    body = {
        "username": username,
        "expiry": int(now + ttl),
        "generation": generation,
        "purpose": purpose,
    }
    payload_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = _b64u_encode(payload_bytes)
    mac = hmac.new(secret, _V2_MAC_PREFIX + payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"v2.{payload_b64}.{_b64u_encode(mac)}"


def _decode_v2_session(token: str, *, now: float, secret: bytes) -> "SessionEnvelope | None":
    parts = token.split(".")
    if len(parts) != 3:
        return None
    _, payload_b64, mac_b64 = parts
    if not payload_b64 or not payload_b64.isascii():
        return None
    try:
        mac = _b64u_decode(mac_b64)
    except ValueError:
        return None
    expected = hmac.new(secret, _V2_MAC_PREFIX + payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload_bytes = _b64u_decode(payload_b64)
        data = json.loads(payload_bytes.decode("utf-8"), object_pairs_hook=_strict_json_object)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != _V2_FIELDS:
        return None
    username = data["username"]
    expiry = data["expiry"]
    generation = data["generation"]
    purpose = data["purpose"]
    if type(username) is not str or not username:
        return None
    if type(expiry) is not int or now >= expiry:
        return None
    if type(generation) is not int or generation < 0:
        return None
    if type(purpose) is not str or purpose not in SESSION_PURPOSES:
        return None
    return SessionEnvelope(
        username=username,
        expiry=expiry,
        generation=generation,
        version=2,
        purpose=purpose,
    )


def decode_session(token: str, *, now: float, secret: bytes) -> "SessionEnvelope | None":
    """Decode and verify HMAC + expiry of a v1 or v2 session token without DB authorization."""
    if not isinstance(token, str):
        return None
    if token.startswith("v2."):
        return _decode_v2_session(token, now=now, secret=secret)
    if token.count(".") != 1:
        return None
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
    except ValueError:
        return None
    if now >= expiry:
        return None
    return SessionEnvelope(username=username, expiry=expiry, generation=0, version=1, purpose=None)


def verify_session(token: str, *, now: float, secret: bytes) -> "str | None":
    """The username carried by a valid, unexpired, correctly-signed ``token`` — else ``None``."""
    envelope = decode_session(token, now=now, secret=secret)
    return None if envelope is None else envelope.username


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


_USAGE = ("usage: python -m hestia.auth add <username> [--role admin|operator|viewer]\n"
          "       python -m hestia.auth role <username> <admin|operator|viewer>\n"
          "       python -m hestia.auth disable|enable <username>")


def _cli(argv: list, *, prompt=getpass.getpass, path: "Path | None" = None) -> int:
    """``add <username> [--role …]`` — prompt a password (twice), hash it, and upsert the user into the
    AUTHORITATIVE store: the SQLite DB when it owns users (HESTIA_PERSIST=sqlite and the users table is
    cut over), else the JSON file. ``role <username> <role>`` — change an existing user's role (SQLite
    only). A test ``path`` forces the JSON file (and keeps this module import-clean of the DB layer in
    JSON mode — ``store_sql`` is imported only on the SQLite branches)."""
    if argv[:1] == ["role"]:
        return _cli_role(argv[1:])
    if argv[:1] == ["disable"] or argv[:1] == ["enable"]:
        return _cli_disable(argv[0], argv[1:])
    if argv[:1] != ["add"]:
        print(_USAGE, file=sys.stderr)
        return 2
    return _cli_add(argv[1:], prompt=prompt, path=path)


def _cli_disable(command: str, args: list) -> int:
    """``disable|enable <username>`` — flip a user's account on/off (SQLite users-authoritative only).
    Refuses to disable the only enabled admin (lockout guard)."""
    if len(args) != 1:
        print(f"usage: python -m hestia.auth {command} <username>", file=sys.stderr)
        return 2
    username = args[0]
    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        print("disable/enable require the SQLite backend (HESTIA_PERSIST=sqlite)", file=sys.stderr)
        return 1
    from . import store_sql
    if not store_sql.users_db_authoritative():
        print("disable/enable require the SQLite users store (users not yet cut over)", file=sys.stderr)
        return 1
    outcome = store_sql.set_user_disabled(username, command == "disable")
    if outcome == "not_found":
        print(f"no such user {username!r}", file=sys.stderr)
        return 1
    if outcome == "last_admin":
        print(f"refused: {username!r} is the only enabled admin (would lock everyone out)", file=sys.stderr)
        return 1
    print(f"{command}d user {username!r}")
    return 0


def _parse_add_args(args: list) -> "tuple[str | None, str | None, str | None]":
    """Parse ``<username> [--role ROLE]`` → ``(username, role, error)``. ``role`` is None when omitted;
    on any problem ``(None, None, message)``. Validation only — never touches the store."""
    role = None
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--role":
            if i + 1 >= len(args):
                return None, None, "missing value for --role"
            role = args[i + 1]
            i += 2
        else:
            positional.append(args[i])
            i += 1
    if len(positional) != 1:
        return None, None, _USAGE
    username = positional[0]
    if not username or "|" in username or "/" in username:
        return None, None, "invalid username (no empty, '|' or '/')"
    return username, role, None


def _cli_add(args: list, *, prompt, path: "Path | None") -> int:
    username, role, error = _parse_add_args(args)
    if error is not None:
        print(error, file=sys.stderr)
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
            if role is not None and role not in store_sql.ROLE_RANK:
                print(f"invalid role {role!r} (use admin, operator or viewer)", file=sys.stderr)
                return 2
            # explicit --role wins; a bare password reset preserves the existing role; a brand-new
            # account defaults to the least-privilege viewer.
            effective = role or store_sql.get_user_db_role(username) or store_sql.DEFAULT_NEW_ROLE
            store_sql.set_user_db(username, password_hash, effective)
            print(f"saved user {username!r} (role {effective!r}) to the SQLite DB")
            return 0
    target = save_user_json(username, password_hash, path)
    note = " (roles need the SQLite backend; saved without one)" if role is not None else ""
    print(f"saved user {username!r} to {target}{note}")
    return 0


def _cli_role(args: list) -> int:
    if len(args) != 2:
        print("usage: python -m hestia.auth role <username> <admin|operator|viewer>", file=sys.stderr)
        return 2
    username, role = args
    if os.environ.get("HESTIA_PERSIST", "json").lower() != "sqlite":
        print("roles require the SQLite backend (HESTIA_PERSIST=sqlite)", file=sys.stderr)
        return 1
    from . import store_sql
    if not store_sql.users_db_authoritative():
        print("roles require the SQLite users store (users not yet cut over)", file=sys.stderr)
        return 1
    if role not in store_sql.ROLE_RANK:
        print(f"invalid role {role!r} (use admin, operator or viewer)", file=sys.stderr)
        return 2
    outcome = store_sql.set_user_role(username, role)
    if outcome == "not_found":
        print(f"no such user {username!r}", file=sys.stderr)
        return 1
    if outcome == "last_admin":
        print(f"refused: {username!r} is the only enabled admin (demoting it would lock everyone out)",
              file=sys.stderr)
        return 1
    print(f"set {username!r} role to {role!r}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli(sys.argv[1:]))
