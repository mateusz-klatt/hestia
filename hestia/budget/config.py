"""Explicit owner/account selection and private credential-file boundaries."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID


class BudgetConfigError(ValueError):
    """Safe diagnostic: never includes configuration values, paths or secrets."""


@dataclass(frozen=True)
class AcquisitionPolicy:
    """Operator ceilings; these values do not establish a bank's allowance."""

    max_acquisitions_24h: int = 2
    minimum_interval_seconds: int = 43200
    request_interval_seconds: int = 1

    def __post_init__(self):
        for value, lower, upper in ((self.max_acquisitions_24h, 1, 2),
                                    (self.minimum_interval_seconds, 43200, 604800),
                                    (self.request_interval_seconds, 1, 60)):
            if type(value) is not int or not lower <= value <= upper:
                raise BudgetConfigError("Invalid acquisition policy")


@dataclass(frozen=True)
class ConnectionConfig:
    id: str
    owner: str
    bank_name: str
    country: str
    session_file: str
    accounts: tuple[str, ...]
    policy: AcquisitionPolicy = AcquisitionPolicy()
    credential_available: bool = True
    consent_generation: int = 0

    def __post_init__(self):
        if type(self.policy) is not AcquisitionPolicy:
            raise BudgetConfigError("Invalid acquisition policy")
        if (type(self.credential_available) is not bool or type(self.consent_generation) is not int
                or self.consent_generation < 0):
            raise BudgetConfigError("Invalid credential configuration")


@dataclass(frozen=True)
class BudgetConfig:
    application_id: str
    key_file: str
    connections: tuple[ConnectionConfig, ...]


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_MAX_SECRET_BYTES = 64 * 1024


def _object(value, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise BudgetConfigError(f"Invalid {label} fields")
    return value


def _text(value, label: str, *, limit=128) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise BudgetConfigError(f"Invalid {label}")
    return value


def _uuid(value, label: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise BudgetConfigError(f"Invalid {label}") from None
    return value


def _path(value, label: str) -> str:
    value = _text(value, label, limit=4096)
    if not Path(value).is_absolute():
        raise BudgetConfigError(f"{label} must be an absolute path")
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BudgetConfigError("Duplicate JSON fields")
        result[key] = value
    return result


def _policy(value):
    fields = {"max_acquisitions_24h", "minimum_interval_seconds", "request_interval_seconds"}
    if not isinstance(value, dict) or not set(value) <= fields:
        raise BudgetConfigError("Invalid acquisition policy fields")
    return AcquisitionPolicy(**value)


def parse_config(raw) -> BudgetConfig:
    data = _object(raw, {"version", "application_id", "key_file", "connections"}, "budget configuration")
    if type(data["version"]) is not int or data["version"] != 1:
        raise BudgetConfigError("Unsupported budget configuration version")
    application_id = _uuid(data["application_id"], "application_id")
    key_file = _path(data["key_file"], "key_file")
    if not isinstance(data["connections"], list):
        raise BudgetConfigError("connections must be a list")
    connections, identifiers = [], set()
    for item in data["connections"]:
        fields = {"id", "owner", "bank_name", "country", "session_file", "accounts"}
        if isinstance(item, dict) and "policy" in item:
            fields.add("policy")
        item = _object(item, fields, "connection")
        policy = _policy(item.get("policy", {}))
        identifier = _text(item["id"], "connection identifier")
        if not _ID.fullmatch(identifier) or identifier in identifiers:
            raise BudgetConfigError("Invalid or duplicate connection identifier")
        identifiers.add(identifier)
        owner = _text(item["owner"], "owner")
        bank_name = _text(item["bank_name"], "bank_name")
        country = _text(item["country"], "country")
        if not _COUNTRY.fullmatch(country):
            raise BudgetConfigError("country must contain two uppercase letters")
        session_file = _path(item["session_file"], "session_file")
        if not isinstance(item["accounts"], list):
            raise BudgetConfigError("accounts must be an explicit list")
        accounts, hashes = [], set()
        for value in item["accounts"]:
            account_hash = _text(value, "account hash", limit=1024)
            if account_hash in hashes:
                raise BudgetConfigError("Duplicate account hash")
            hashes.add(account_hash)
            accounts.append(account_hash)
        connections.append(ConnectionConfig(identifier, owner, bank_name, country, session_file, tuple(accounts), policy))
    return BudgetConfig(application_id, key_file, tuple(connections))


def load_config(path=None, *, environ=None) -> BudgetConfig | None:
    """Read an operator-selected local path; not reachable from HTTP or LLM input."""
    env = os.environ if environ is None else environ
    path = env.get("HESTIA_BUDGET_CONFIG") if path is None else path
    if path is None or path == "":
        return None
    try:
        with open(path, encoding="utf-8") as source:
            raw = json.load(source, object_pairs_hook=_json_object)
    except (OSError, ValueError, TypeError):
        raise BudgetConfigError("Cannot read budget configuration") from None
    return parse_config(raw)


def _read_secret(path: str, *, max_bytes=None) -> bytes:
    limit = _MAX_SECRET_BYTES if max_bytes is None else max_bytes
    if type(limit) is not int or limit < 0:
        raise BudgetConfigError("Invalid budget file size limit")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_size > limit):
                raise ValueError
            data = b""
            while len(data) <= limit:
                chunk = os.read(descriptor, limit + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) > limit:
                raise ValueError
            return data
        finally:
            os.close(descriptor)
    except (OSError, ValueError):
        raise BudgetConfigError("Cannot safely read budget credential file") from None


def credentials(config: BudgetConfig, connection: ConnectionConfig) -> tuple[bytes, str]:
    """Resolve private local files; PEM interpretation belongs to the API client."""
    if not connection.credential_available:
        raise BudgetConfigError("Managed budget credentials are unavailable")
    key = _read_secret(config.key_file)
    try:
        data = json.loads(_read_secret(connection.session_file), object_pairs_hook=_json_object)
    except (ValueError, TypeError):
        raise BudgetConfigError("Invalid budget session file") from None
    data = _object(data, {"session_id"}, "session")
    return key, _uuid(data["session_id"], "session_id")
