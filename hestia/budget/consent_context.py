"""Foreground PSU context from the peer and an explicitly trusted proxy chain.

Only a trusted immediate peer may supply X-Forwarded-For. Proxy trust is an
operator policy; no Cloudflare-specific or other client-supplied identity header
is inferred. Unavailable context remains absent for the bank capability check.
"""
from __future__ import annotations

import ipaddress
import os
import re

from .config import BudgetConfigError
from .consent_client import PSUContext

TRUSTED_PROXY_ENV = "HESTIA_BUDGET_TRUSTED_PROXY_CIDRS"
MAX_PROXY_CONFIG = 8192
MAX_PROXY_NETWORKS = 128
MAX_FORWARDED = 4096
MAX_PROXY_CHAIN = 32


def _networks(environ):
    value = environ.get(TRUSTED_PROXY_ENV, "")
    if not isinstance(value, str) or len(value) > MAX_PROXY_CONFIG or not value.isascii():
        raise BudgetConfigError("Budget trusted proxy configuration is invalid")
    values = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    if len(values) > MAX_PROXY_NETWORKS:
        raise BudgetConfigError("Budget trusted proxy configuration is invalid")
    try:
        if any("/" not in part or "%" in part for part in values):
            raise ValueError
        return tuple(ipaddress.ip_network(part, strict=True) for part in values)
    except ValueError:
        raise BudgetConfigError("Budget trusted proxy configuration is invalid") from None


def _ip(value):
    if not isinstance(value, str) or len(value) > 64 or "%" in value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _trusted(address, networks):
    return any(address in network for network in networks)


def _header(headers, name, maximum):
    values = headers.getall(name, ())
    if len(values) != 1:
        return None
    value = values[0]
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    return value


def _forwarded(headers, networks):
    value = _header(headers, "X-Forwarded-For", MAX_FORWARDED)
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) > MAX_PROXY_CHAIN:
        return None
    addresses = [_ip(part) for part in parts]
    if any(address is None for address in addresses):
        return None
    for address in reversed(addresses):
        if not _trusted(address, networks):
            return str(address)
    return None


def _address(request, networks):
    peer = _ip(request.remote)
    if peer is None:
        return None
    if _trusted(peer, networks):
        return _forwarded(request.headers, networks)
    # An unverified local/private proxy must not masquerade as the PSU. For a
    # direct public peer, forwarded headers are ignored regardless of contents.
    return str(peer) if peer.is_global and not peer.is_multicast else None


def psu_context(request, *, environ=None):
    """Return bounded server-created context, without persistence or logging.

    ``request`` is an aiohttp request. A missing/invalid forwarded chain produces
    no IP; malformed operator CIDR policy raises a fixed configuration error.
    """
    networks = _networks(os.environ if environ is None else environ)
    return PSUContext(ip_address=_address(request, networks),
                      user_agent=_header(request.headers, "User-Agent", 1024),
                      accept_language=_header(request.headers, "Accept-Language", 256))
