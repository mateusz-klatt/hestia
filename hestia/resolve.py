"""Resolve the Keemple cloud host without tripping our own Pi-hole override.

Our LAN Pi-hole points ``gateway.keemple.com`` at this box (so the gateway reaches
hestia), which means a normal DNS lookup of that name self-loops. This module finds
the *real* upstream IP instead:

* **DoH at resolver IP literals** — query the Google / Cloudflare DNS-over-HTTPS JSON
  APIs by their IP (``https://8.8.8.8/resolve`` etc.), so resolving the resolver's own
  name can't hit Pi-hole. Both have IP-SAN certs, so normal TLS verification holds.
* **Loop-guard** — reject any answer that isn't a *global* address (catches the
  Pi-hole poison ``192.0.2.1``, loopback, link-local, and our own private IP).
* **Connect-probe** — a Cloudflare-fronted A record may serve HTTPS but not the raw
  device port; only accept an IP that actually answers on ``:8925``.
* **Seed fallback** — on any failure fall back to the caller's last-known-good seed
  (the hardcoded Alibaba IP). Never return a private address.

stdlib only (``urllib`` + ``json`` + ``socket`` + ``ipaddress``). Opt-in: the proxy
calls this only when ``HESTIA_CLOUD_HOSTNAME`` is set; otherwise ``HESTIA_CLOUD_HOST``
is used verbatim (a literal pin).

``timeout`` is **per operation** (each DoH query and each connect-probe), so the
worst case is ``len(endpoints) × (1 + MAX_CANDIDATES) × timeout`` if every probe
times out. The proxy runs this once at startup and caps it with its own
``asyncio.wait_for`` ceiling, so a slow/hostile upstream can't stall startup.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.parse
import urllib.request

log = logging.getLogger("hestia.resolve")

# IP-literal DoH endpoints (no bootstrap name lookup → can't self-loop through Pi-hole).
DOH_ENDPOINTS = ("https://8.8.8.8/resolve", "https://1.1.1.1/dns-query")
MAX_CANDIDATES = 8   # cap probes per endpoint so many non-serving A records can't stretch startup


def _doh_a_records(endpoint: str, hostname: str, timeout: float) -> "list[str]":
    """Query one DoH JSON endpoint for the A records of ``hostname``. Defensive about
    the response shape (a hostile/garbled answer must not raise): anything that isn't a
    well-formed A record is dropped. Only `urlopen` (OSError) and `json.load` (ValueError)
    can raise, and the caller catches both."""
    url = f"{endpoint}?name={urllib.parse.quote(hostname, safe='')}&type=A"   # encode '/' etc.
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    answers = data.get("Answer") if isinstance(data, dict) else None
    if not isinstance(answers, list):
        return []                                  # missing / null / non-list "Answer"
    return [a["data"] for a in answers             # DNS record type 1 = A; ignore the rest
            if isinstance(a, dict) and a.get("type") == 1 and isinstance(a.get("data"), str)]


def _is_global(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _serves(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_cloud_ip(hostname: str, seed: str, *, port: int = 8925,
                     timeout: float = 3.0, endpoints=DOH_ENDPOINTS) -> str:
    """Return the IP to dial for ``hostname`` — a DoH-resolved, global, port-serving
    address, or ``seed`` if none qualifies. Never raises. Contract: a *DoH answer* is
    never returned unless it's global (loop-guard), but the explicit operator ``seed``
    (``HESTIA_CLOUD_HOST``) is honoured even if non-global — a loud error is logged
    rather than crashing startup or silently overriding a deliberate pin."""
    for endpoint in endpoints:
        try:
            answers = _doh_a_records(endpoint, hostname, timeout)
        except (OSError, ValueError) as exc:             # network (URLError⊂OSError) / bad-JSON
            log.warning("DoH %s failed for %s: %r", endpoint, hostname, exc)
            continue
        for ip in answers[:MAX_CANDIDATES]:
            if not _is_global(ip):
                log.warning("rejecting non-global %s for %s (loop-guard)", ip, hostname)
                continue
            if _serves(ip, port, timeout):
                log.info("resolved %s -> %s (serves :%d)", hostname, ip, port)
                return ip
            log.warning("%s for %s does not serve :%d — skipping", ip, hostname, port)
    # Fallback: the operator's last-known-good seed. It SHOULD be a global address; if
    # it isn't, that's a misconfigured HESTIA_CLOUD_HOST — warn loudly but honour it
    # (never crash startup, never silently override the operator's explicit pin).
    if not _is_global(seed):
        log.error("seed %s for %s is not a global address — dialling it anyway; "
                  "check HESTIA_CLOUD_HOST", seed, hostname)
    else:
        log.warning("DoH resolution failed for %s; falling back to seed %s", hostname, seed)
    return seed
