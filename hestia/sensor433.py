"""Outdoor temperature + humidity from a local 433 MHz weather sensor via the ``rtl_433`` binary — a
stdlib-only, opt-in, on-LAN/zero-egress PUSH feeder for the ``outdoor_temp`` GLOBAL automation field
(plus a display-only ``outdoor_humidity`` global).

``rtl_433 -F json`` emits one JSON object per decoded packet to stdout and flushes it the instant the
packet is received (empirically verified over a pipe: a keyfob press and the sensor's reading both
arrive in real time, not batched at exit). So hestia spawns ONE long-lived ``rtl_433`` and reacts to
every matching line as it streams in — no polling interval, no reception window, no dead sleep. ``rtl_433``
is an external SYSTEM binary (same category as the dnsmasq/Pi-hole the project already relies on), spawned
via :func:`asyncio.create_subprocess_exec` — NOT a Python import, so the zero-runtime-deps rule holds.
OFF by default; selected by ``HESTIA_OUTDOOR_TEMP_SOURCE=local``. See ``docs/AUTOMATIONS.md``.

CRITICAL: an ``rtl_tcp`` endpoint serves a SINGLE client, so the long-lived child MUST be terminated
*and reaped* when the stream ends or is cancelled — a leaked ``rtl_433`` would hold the SDR and silently
block every future reader (this exact failure was observed in testing). :func:`stream_readings`' ``finally``
clause guarantees the child is signalled and awaited on every exit path.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from typing import Awaitable, Callable, NamedTuple

log = logging.getLogger("hestia.sensor433")

RTL_433_BIN = "rtl_433"
# A DEDICATED rtl_tcp endpoint. Do NOT point this at an rtl_tcp shared with another consumer (e.g. an
# FM/RDS receiver): rtl_433 retunes/monopolises the SDR for 433 MHz and breaks the other consumer. Use its own SDR.
DEFAULT_DEVICE = "rtl_tcp:127.0.0.1:1234"


class Reading(NamedTuple):
    """One decoded 433 MHz weather reading. ``humidity`` is ``None`` when the packet omits it."""
    temperature_C: float
    humidity: "float | None"


def _rtl433_command(binary: str, device: str, protocol: "str | None") -> list:
    # No -T: rtl_433 runs until terminated, streaming every decoded packet as JSON (push, not poll).
    cmd = [binary, "-d", device, "-F", "json"]
    if protocol:
        cmd += ["-R", protocol]                       # restrict decoding to ONE protocol (rtl_433 -R)
    return cmd


def _json_line(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:                               # a non-JSON / partial line — skip it
        return None
    return obj if isinstance(obj, dict) else None


def _finite_number(value):
    """``value`` as a finite ``float``, or ``None`` — rejecting bool (``json`` makes ``True`` an int) and
    NaN/Infinity (which ``json.loads`` accepts) so a garbage reading can never poison a global / mis-fire a rule."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _matching_reading(obj: dict, model: "str | None", sensor_id: "str | None"):
    """A :class:`Reading` if ``obj`` matches the optional ``model`` / ``sensor_id`` filters AND carries a
    finite ``temperature_C`` — else ``None`` (so a same-id non-TH packet, e.g. a keyfob, is ignored)."""
    if model and obj.get("model") != model:
        return None
    if sensor_id and str(obj.get("id")) != sensor_id:
        return None
    temp = _finite_number(obj.get("temperature_C"))
    if temp is None:
        return None
    return Reading(temp, _finite_number(obj.get("humidity")))


_TERMINATE_GRACE = 5.0   # seconds to let rtl_433 exit on SIGTERM before escalating to SIGKILL


async def _terminate(proc) -> None:
    """Terminate + reap ``proc`` so a finished/cancelled stream never leaves an orphan ``rtl_433`` holding
    the single-client SDR. BOUNDED — SIGTERM, a short grace period, then SIGKILL — so a child wedged in a
    blocking USB/socket call (an SDR/rtl_tcp failure) can never hang the poller's relaunch loop or the
    shutdown ``gather()``. Tolerant of an already-exited child; if cancellation arrives mid-reap it still
    SIGKILLs the child (so it can't keep holding the SDR) before letting the cancellation propagate."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):    # raced us to exit between the check and the signal
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), _TERMINATE_GRACE)
    except TimeoutError:                                 # SIGTERM ignored/wedged -> force it, then reap
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()                                # SIGKILL is uncatchable -> returns promptly
    except asyncio.CancelledError:                       # shutdown cancelling us mid-reap -> kill, don't hang
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise


async def stream_readings(
    on_reading: "Callable[[Reading], Awaitable[None]]",
    *,
    device: str = DEFAULT_DEVICE,
    model: "str | None" = None,
    sensor_id: "str | None" = None,
    protocol: "str | None" = None,
    binary: str = RTL_433_BIN,
    create=asyncio.create_subprocess_exec,
) -> None:
    """Spawn a long-lived ``rtl_433 -F json`` and ``await on_reading(reading)`` for every matching finite
    reading as it streams in (PUSH — no interval). Returns when the process exits or its stdout closes, so
    the caller can restart it after a backoff. NEVER raises on a malformed line. The child is always
    terminated AND reaped on return/cancel (see :func:`_terminate`) so no ``rtl_433`` is orphaned. ``create``
    is injectable (defaults to :func:`asyncio.create_subprocess_exec`) so tests never spawn a real process."""
    cmd = _rtl433_command(binary, device, protocol)
    try:
        proc = await create(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (OSError, ValueError):                        # binary missing / bad argv -> caller backs off + retries
        return
    try:
        stdout = proc.stdout
        if stdout is not None:
            async for raw in stdout:                     # yields one buffered line per decoded packet, in real time
                obj = _json_line(raw.decode("utf-8", "replace"))   # never raises on undecodable bytes
                if obj is not None:
                    reading = _matching_reading(obj, model, sensor_id)
                    if reading is not None:
                        await on_reading(reading)
    finally:
        await _terminate(proc)
