"""Explicit operator configuration for Gmail's separate local grant."""
from __future__ import annotations

from collections.abc import Mapping
import hmac
import json
import os
import re
from types import MappingProxyType

from . import config
from .client import _reject_constant, _unique_error_fields
from .gmail_address import canonical_mailbox
from .gmail_client import GmailCredentials
from .gmail_sync import GmailSettings
from .gmail_trust import normalize_document_senders, normalize_payment_senders, normalize_sender_rules

CONFIG_FILE = "HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG"
HMAC_FILE = "HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE"
CARD_ACCOUNTS_ENV = "HESTIA_BUDGET_CARD_ACCOUNTS"
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_MAX_CARD_ACCOUNTS_BYTES = 16 * 1024
_MAX_CARD_KEYS = 64
_MAX_CARD_ACCOUNT_IDS = 32
_CARD_LAST4 = re.compile(r"[0-9]{4}\Z", re.ASCII)
_CARD_ACCOUNT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z", re.ASCII)
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_REQUIRED_FIELDS = {"owner", "owner_mailbox", "client_id_env", "client_secret_env", "refresh_token_env",
                    "sender_allowlist", "spouse_forward_senders"}
_OPTIONAL_FIELDS = {"known_merchant_domains", "require_known_forwarded_merchant", "label_name",
                    "backfill_days", "backfill_max_messages", "sync_interval_seconds", "payment_senders", "document_senders",
                    "require_sender_authentication"}


def _read_config(path):
    raw = config._read_secret(path, max_bytes=_MAX_CONFIG_BYTES)
    return json.loads(raw, object_pairs_hook=_unique_error_fields, parse_constant=_reject_constant)


def _env_reference(data, environ, field):
    name = data[field]
    if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
        raise ValueError
    return environ[name]


def _oauth_value(value):
    if not 0 < len(value) <= _MAX_TOKEN_BYTES or not value.isascii() or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError
    return value


def _integer(data, name, default, minimum, maximum):
    value = data.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError
    return value


def normalize_card_accounts(value: object, *, allow_tuples: bool = False) -> Mapping[str, tuple[str, ...]]:
    """Validate and freeze a mapping of 4-digit card suffixes to account IDs."""
    try:
        seq_types = (list, tuple) if allow_tuples else (list,)
        if not isinstance(value, Mapping) or len(value) > _MAX_CARD_KEYS:
            raise ValueError
        normalized: dict[str, tuple[str, ...]] = {}
        for last4, account_ids in value.items():
            if not isinstance(last4, str) or _CARD_LAST4.fullmatch(last4) is None:
                raise ValueError
            if not isinstance(account_ids, seq_types) or not 1 <= len(account_ids) <= _MAX_CARD_ACCOUNT_IDS:
                raise ValueError
            seen: set[str] = set()
            cleaned: list[str] = []
            for account_id in account_ids:
                if (not isinstance(account_id, str)
                        or _CARD_ACCOUNT_ID.fullmatch(account_id) is None
                        or account_id in seen):
                    raise ValueError
                seen.add(account_id)
                cleaned.append(account_id)
            normalized[last4] = tuple(cleaned)
        return MappingProxyType(normalized)
    except ValueError:
        raise config.BudgetConfigError("Invalid card account mapping") from None


def load_card_accounts(*, environ=None) -> Mapping[str, tuple[str, ...]]:
    """Parse and validate optional HESTIA_BUDGET_CARD_ACCOUNTS JSON env setting."""
    env = os.environ if environ is None else environ
    raw = env.get(CARD_ACCOUNTS_ENV)
    if raw is None or raw == "":
        return MappingProxyType({})
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_CARD_ACCOUNTS_BYTES:
            raise ValueError
        parsed = json.loads(raw, object_pairs_hook=_unique_error_fields, parse_constant=_reject_constant)
        if not isinstance(parsed, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
        raise config.BudgetConfigError("Invalid card account mapping") from None
    return normalize_card_accounts(parsed)


def load_ref_hmac_secret(*, environ=None, session_secret=b"") -> bytes | None:
    """Load optional HMAC secret used for in-memory receipt gateway token verification."""
    env = os.environ if environ is None else environ
    raw_path = env.get(HMAC_FILE)
    if not raw_path:
        return None
    try:
        hmac_path = config._path(raw_path, "Gmail dedupe key file")
        hmac_secret = config._read_secret(hmac_path)
        if len(hmac_secret) < 32 or hmac.compare_digest(hmac_secret, session_secret):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise config.BudgetConfigError("Invalid Gmail receipt configuration") from None
    return hmac_secret


def load_settings(*, environ=None, session_secret=b"") -> GmailSettings | None:
    """Resolve a Gmail-only refresh-token file reference. Called only when enabled."""
    env = os.environ if environ is None else environ
    card_accounts = load_card_accounts(environ=env)
    path = env.get(CONFIG_FILE)
    if not path:
        return None
    try:
        data = _read_config(path)
        if (not isinstance(data, dict) or not _REQUIRED_FIELDS <= data.keys()
                or data.keys() - (_REQUIRED_FIELDS | _OPTIONAL_FIELDS)):
            raise ValueError
        owner = config._text(data["owner"], "owner")
        owner.encode("utf-8")
        mailbox = canonical_mailbox(data["owner_mailbox"])
        if mailbox is None:
            raise ValueError
        payments = normalize_payment_senders(data.get("payment_senders", []))
        documents = normalize_document_senders(data.get("document_senders", []))
        require_auth = data.get("require_sender_authentication", False)
        if type(require_auth) is not bool:
            raise ValueError
        senders, spouse, domains = normalize_sender_rules(
            data["sender_allowlist"], data["spouse_forward_senders"], data.get("known_merchant_domains", []),
            payment_senders=payments, document_senders=documents, allow_empty=not require_auth,
        )
        require_known = data.get("require_known_forwarded_merchant", True)
        if type(require_known) is not bool:
            raise ValueError
        label_name = data.get("label_name")
        if label_name is not None:
            label_name = config._text(label_name, "Gmail label", limit=225)
            label_name.encode("utf-8")
        backfill_days = _integer(data, "backfill_days", 365, 1, 365)
        backfill_max = _integer(data, "backfill_max_messages", 500, 1, 500)
        # Cap at the largest exactly representable integer timestamp in SQLite REAL.
        interval = _integer(data, "sync_interval_seconds", 3600, 3600, 2**53)
        client_id = _oauth_value(_env_reference(data, env, "client_id_env"))
        client_secret = _oauth_value(_env_reference(data, env, "client_secret_env"))
        refresh_path = config._path(_env_reference(data, env, "refresh_token_env"), "Gmail refresh-token file")
        refresh_bytes = config._read_secret(refresh_path, max_bytes=_MAX_TOKEN_BYTES)
        refresh_token = _oauth_value(refresh_bytes.decode("ascii").strip("\r\n"))
        hmac_path = config._path(env[HMAC_FILE], "Gmail dedupe key file")
        hmac_secret = config._read_secret(hmac_path)
        if (len(hmac_secret) < 32 or hmac.compare_digest(hmac_secret, session_secret)
                or hmac.compare_digest(hmac_secret, refresh_bytes)
                or hmac.compare_digest(hmac_secret, refresh_token.encode("ascii"))):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise config.BudgetConfigError("Invalid Gmail receipt configuration") from None

    return GmailSettings(owner, mailbox, GmailCredentials(client_id, client_secret, refresh_token), hmac_secret,
                         senders, spouse, domains, require_known, label_name, backfill_days, backfill_max, interval,
                         payment_senders=payments, document_senders=documents, card_accounts=card_accounts,
                         require_sender_authentication=require_auth)
