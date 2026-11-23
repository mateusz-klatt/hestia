"""Canonical ASCII addresses for Gmail identity and sender rules."""
from __future__ import annotations

import re


_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+", re.ASCII | re.IGNORECASE)
_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_PART = re.compile(rf"{_ATOM}(?:\.{_ATOM})*", re.ASCII | re.IGNORECASE)


def canonical_domain(value):
    if len(value) <= 253 and _DOMAIN.fullmatch(value):
        return value.lower()
    return None


def canonical_mailbox(value):
    """Canonicalize a bare ASCII mailbox for config and Gmail profile checks."""
    if not isinstance(value, str):
        return None
    local, _, domain = value.strip(" \t").rpartition("@")
    normalized_domain = canonical_domain(domain)
    if normalized_domain and len(local) <= 64 and _LOCAL_PART.fullmatch(local):
        return f"{local.lower()}@{normalized_domain}"
    return None
