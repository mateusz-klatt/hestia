"""Outdoor temperature from Open-Meteo — a stdlib-only, opt-in weather fetch.

Mirrors :mod:`hestia.resolve`'s outbound-HTTP idiom (``urllib.request.urlopen`` +
``json.load``, defensive about the response shape). Feeds the ``outdoor_temp`` GLOBAL
automation field via ``proxy._weather_poller`` (which runs the BLOCKING fetch off the
event loop). Open-Meteo is free and needs no API key. OFF by default — see
``docs/AUTOMATIONS.md`` *Deployment — outdoor temperature* for the egress/privacy note.
"""
from __future__ import annotations

import http.client
import json
import logging
import math
import urllib.request

log = logging.getLogger("hestia.weather")

# Open-Meteo current-weather endpoint (no key). ``current=temperature_2m`` → the 2 m air temp in °C.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_outdoor_temp(lat, lon, *, timeout: float = 5.0,
                       base_url: str = OPEN_METEO_URL) -> "float | None":
    """The current outdoor temperature (°C) for ``lat``/``lon``, or ``None`` on ANY failure —
    network/transport (``OSError``), a truncated/garbled HTTP body (``http.client.HTTPException``, e.g.
    ``IncompleteRead`` mid-read), a non-JSON body (``ValueError``), a missing/garbled response shape
    (``KeyError``/``TypeError``), or a non-finite/non-number reading. Returning ``None`` (never
    raising) lets the poller keep the last value and retry, so a bad fetch can't poison ``outdoor_temp``
    or crash the loop. BLOCKING (``urlopen``); the caller runs it off the event loop. ``base_url`` is
    injectable so tests mock ``urllib.request.urlopen`` without real network."""
    url = f"{base_url}?latitude={lat}&longitude={lon}&current=temperature_2m"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.load(resp)                       # may raise HTTPException on a truncated body
        temp = data["current"]["temperature_2m"]
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return None
    if isinstance(temp, (int, float)) and not isinstance(temp, bool) and math.isfinite(temp):
        return float(temp)
    return None
