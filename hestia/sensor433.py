"""Outdoor temperature from a local 433 MHz weather sensor via the ``rtl_433`` binary — a stdlib-only,
opt-in, on-LAN/zero-egress alternative feeder for the ``outdoor_temp`` GLOBAL automation field.

Mirrors :mod:`hestia.weather`'s contract: a BLOCKING read the proxy poller runs OFF the event loop,
returning ``None`` on ANY failure (never raising) so a bad read keeps the last value and the daemon
loop survives. ``rtl_433`` is an external SYSTEM binary (same category as dnsmasq/Pi-hole the project
already relies on), invoked via :mod:`subprocess` — NOT a Python import, so the zero-runtime-deps rule
holds. OFF by default; selected by ``HESTIA_OUTDOOR_TEMP_SOURCE=local``. See ``docs/AUTOMATIONS.md``.
"""
from __future__ import annotations

import json
import logging
import math
import subprocess

log = logging.getLogger("hestia.sensor433")

RTL_433_BIN = "rtl_433"
# A DEDICATED rtl_tcp endpoint. Do NOT point this at an rtl_tcp shared with another consumer (e.g. an
# FM/RDS receiver): a reception window retunes/monopolises the SDR and breaks the other consumer. Use its own SDR.
DEFAULT_DEVICE = "rtl_tcp:127.0.0.1:1234"


def _rtl433_command(binary: str, device: str, window: float, protocol: "str | None") -> list:
    cmd = [binary, "-d", device, "-F", "json", "-T", str(int(window))]
    if protocol:
        cmd += ["-R", protocol]
    return cmd


def _run_rtl433(cmd: list, window: float, run):
    try:
        proc = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   text=True, encoding="utf-8", errors="replace", timeout=window + 15.0)
    except (OSError, subprocess.SubprocessError):
        return None                                  # binary missing / spawn error / timeout
    return None if proc.returncode else proc         # non-zero -> failed read, keep last


def _json_line(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:                               # a non-JSON / partial line — skip it
        return None
    return obj if isinstance(obj, dict) else None


def _matching_temperature(obj: dict, model: "str | None", sensor_id: "str | None"):
    if model and obj.get("model") != model:
        return None
    if sensor_id and str(obj.get("id")) != sensor_id:
        return None
    value = obj.get("temperature_C")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def read_outdoor_temp(*, device: str = DEFAULT_DEVICE, window: float = 60.0,
                      model: "str | None" = None, sensor_id: "str | None" = None,
                      protocol: "str | None" = None, binary: str = RTL_433_BIN,
                      run=subprocess.run) -> "float | None":
    """The latest matching 433 MHz sensor's temperature (°C, finite), or ``None`` on ANY failure.

    Runs ``rtl_433 -d <device> -F json -T <window> [-R <protocol>]`` ONCE — ``-T`` is a wall-clock run
    duration (``rtl_433 -h``: "Specify number of seconds to run"), so the process self-exits after
    ``window`` seconds; the ``subprocess`` ``timeout`` is only a safety net. Parses each JSON line and
    returns the LAST reading whose ``temperature_C`` is a real finite number (rejecting bool and
    NaN/Infinity, which ``json`` accepts) and matches the optional ``model`` / ``sensor_id`` filters —
    rtl_433 emits chronologically, so the last match is the freshest. ``-R <protocol>`` restricts
    decoding to that protocol only (``rtl_433 -h``: "Enable ONLY the specified device decoding
    protocol"). Returns ``None`` when rtl_433 is missing (``FileNotFoundError``), the spawn fails or
    times out (``OSError`` / ``subprocess.SubprocessError``), exits non-zero (a failed run — even one
    that emitted partial JSON — is discarded so the poller keeps the last value), or no matching finite
    reading appears. ``errors="replace"`` keeps decoding from ever raising. BLOCKING; runs off the loop.
    ``run`` is injectable so tests never touch a real SDR."""
    proc = _run_rtl433(_rtl433_command(binary, device, window, protocol), window, run)
    if proc is None:
        return None                                  # rtl_433 exited non-zero -> failed read, keep last
    temp = None
    for line in (proc.stdout or "").splitlines():
        obj = _json_line(line)
        if obj is not None:
            candidate = _matching_temperature(obj, model, sensor_id)
            if candidate is not None:
                temp = candidate                       # last matching finite reading wins
    return temp
