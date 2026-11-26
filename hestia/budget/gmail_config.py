"""Explicit operator configuration for Gmail's separate local grant."""
from __future__ import annotations

import hmac
import json
import os
import re

from . import config
from .client import _reject_constant, _unique_error_fields
from .gmail_address import canonical_mailbox
from .gmail_client import GmailCredentials
from .gmail_sync import GmailSettings
from .gmail_trust import normalize_document_senders, normalize_payment_senders, normalize_sender_rules

CONFIG_FILE = "HESTIA_BUDGET_RECEIPTS_GMAIL_CONFIG"
HMAC_FILE = "HESTIA_BUDGET_RECEIPTS_HMAC_SECRET_FILE"
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")
_REQUIRED_FIELDS = {"owner", "owner_mailbox", "client_id_env", "client_secret_env", "refresh_token_env",
                    "sender_allowlist", "spouse_forward_senders"}
_OPTIONAL_FIELDS = {"known_merchant_domains", "require_known_forwarded_merchant", "label_name",
                    "backfill_days", "backfill_max_messages", "sync_interval_seconds", "payment_senders", "document_senders"}


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


def load_settings(*, environ=None, session_secret=b"") -> GmailSettings | None:
    """Resolve a Gmail-only refresh-token file reference. Called only when enabled."""
    env = os.environ if environ is None else environ
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
        senders, spouse, domains = normalize_sender_rules(
            data["sender_allowlist"], data["spouse_forward_senders"], data.get("known_merchant_domains", []),
            payment_senders=payments, document_senders=documents,
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
                         payment_senders=payments, document_senders=documents)
