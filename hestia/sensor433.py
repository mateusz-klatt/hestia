"""Temperature + humidity from local 433 / 868 MHz weather sensors via the ``rtl_433`` binary — a
stdlib-only, opt-in, on-LAN/zero-egress PUSH feeder for the ``outdoor_temp`` and ``crib_temp`` GLOBAL
automation fields (plus display-only ``outdoor_humidity``).

``rtl_433 -F json`` emits one JSON object per decoded packet to stdout and flushes it the instant the
packet is received (empirically verified over a pipe: a keyfob press and the sensor's reading both
arrive in real time, not batched at exit). So hestia spawns ONE long-lived ``rtl_433`` and reacts to
every matching line as it streams in — no polling interval, no reception window, no dead sleep. Optional
``HESTIA_RTL433_EXTRA_ARGS`` lets that single process hop across bands (e.g. 433.92 MHz + 868 MHz via
``-f 433.92M -f 868M -s 1024k -Y classic -H 30``) and route each decoded packet to at most one target
(outdoor or crib). ``rtl_433`` is an external SYSTEM binary (same category as the dnsmasq/Pi-hole the
project already relies on), spawned via :func:`asyncio.create_subprocess_exec` — NOT a Python import, so
the zero-runtime-deps rule holds. OFF by default; see ``docs/AUTOMATIONS.md``.

CRITICAL: an ``rtl_tcp`` endpoint serves a SINGLE client, so the long-lived child MUST be terminated
*and reaped* when the stream ends or is cancelled — a leaked ``rtl_433`` would hold the SDR and silently
block every future reader (this exact failure was observed in testing). :func:`stream_readings`' ``finally``
clause guarantees the child is signalled and awaited on every exit path.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
import contextlib
import json
import logging
import math
import re
import shlex
from typing import Awaitable, Callable, NamedTuple

log = logging.getLogger("hestia.sensor433")

RTL_433_BIN = "rtl_433"
# A DEDICATED rtl_tcp endpoint. Do NOT point this at an rtl_tcp shared with another consumer (e.g. an
# FM/RDS receiver): rtl_433 retunes/monopolises the SDR and breaks the other consumer. Use its own SDR.
DEFAULT_DEVICE = "rtl_tcp:127.0.0.1:1234"

# Short options accepted by rtl_433's getopt OPTSTRING:
#   "hVvqD:c:x:z:p:a:AI:S:m:M:r:w:W:l:d:t:f:H:g:s:b:n:R:X:F:K:C:T:UGy:E:Y:"
# Disallow flags that conflict with hestia's managed base argv (-d/-F/-T), write/auto-save files (-w/-W/-S,
# plus -F output targets), or load an external config file that can re-enable them (-c).
_NO_ARG_OPTS = frozenset("hVvqAUG")
_ARG_OPTS = frozenset("DcxzpaISmMrwWldtfHgsbnRXFKCTyEY")
_FORBIDDEN_OPTS = frozenset("dFTwWSc")
_NEG_NUM_RE = re.compile(r"^-(?:\d+(?:\.\d*)?|\.\d+)$")


class Reading(NamedTuple):
    """One decoded weather reading. ``humidity`` / ``battery_ok`` are ``None`` when the packet
    omits them. ``battery_ok`` mirrors rtl_433's flag: ``1`` = battery OK, ``0`` = low."""
    temperature_C: float
    humidity: "float | None"
    battery_ok: "int | None" = None


def _validate_extra_tokens(tokens: list[str]) -> None:
    """Reject shell/getopt traps, disallowed flags (``-d``, ``-F``, ``-T``), and any flag writing or
    auto-saving files (``-w``, ``-W``, ``-S``, ``-c``), catching standalone, attached, and clustered forms."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "\x00" in tok:
            raise ValueError("NUL byte in extra args")
        if tok == "--" or tok.startswith("--"):
            raise ValueError(f"unsupported long/separator option {tok!r} in HESTIA_RTL433_EXTRA_ARGS")
        if not tok.startswith("-") or tok == "-":
            raise ValueError(f"unexpected positional argument {tok!r} in HESTIA_RTL433_EXTRA_ARGS")
        j = 1
        consumed_next = False
        while j < len(tok):
            ch = tok[j]
            if ch in _FORBIDDEN_OPTS:
                raise ValueError(f"option -{ch} is disallowed in HESTIA_RTL433_EXTRA_ARGS")
            if ch in _NO_ARG_OPTS:
                j += 1
                continue
            if ch in _ARG_OPTS:
                if j + 1 < len(tok):
                    break                                # attached argument (e.g. -f433.92M / -f=433.92M)
                if i + 1 >= len(tokens):
                    raise ValueError(f"option -{ch} requires an argument")
                nxt = tokens[i + 1]
                if "\x00" in nxt:
                    raise ValueError("NUL byte in extra args")
                if nxt.startswith("-") and not _NEG_NUM_RE.match(nxt):
                    raise ValueError(f"option -{ch} has flag-like argument {nxt!r}")
                consumed_next = True
                break
            raise ValueError(f"unknown or unsupported option -{ch} in HESTIA_RTL433_EXTRA_ARGS")
        i += 2 if consumed_next else 1


def parse_extra_args(raw: "str | Sequence[str] | None") -> list[str]:
    """Split (via :func:`shlex.split`) and validate ``HESTIA_RTL433_EXTRA_ARGS``. Empty / ``None`` -> ``[]``.
    Raises :class:`ValueError` on malformed quoting or any disallowed/file-writing flag."""
    if raw is None:
        return []
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            tokens = shlex.split(raw)
        except ValueError as exc:
            raise ValueError(f"invalid quoting in HESTIA_RTL433_EXTRA_ARGS: {exc}") from exc
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        if not all(isinstance(tok, str) for tok in raw):
            raise ValueError("HESTIA_RTL433_EXTRA_ARGS sequence items must be strings")
        tokens = list(raw)
    else:
        raise ValueError("HESTIA_RTL433_EXTRA_ARGS must be a string or sequence of strings")
    _validate_extra_tokens(tokens)
    return tokens


def _rtl433_command(binary: str, device: str, protocol: "str | None" = None,
                    extra_args: "str | Sequence[str] | None" = None) -> list:
    # No -T: rtl_433 runs until terminated, streaming every decoded packet as JSON (push, not poll).
    cmd = [binary, "-d", device, "-F", "json"]
    if protocol:
        cmd += ["-R", protocol]                       # restrict decoding to ONE protocol (rtl_433 -R)
    extra = parse_extra_args(extra_args)
    if extra:
        cmd.extend(extra)
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


def _battery_ok(value):
    """rtl_433's ``battery_ok`` flag normalised to ``1`` (ok) / ``0`` (low), or ``None`` when the packet
    omits it. Reuses :func:`_finite_number` (rejects bool / NaN / non-numbers) then collapses to 0/1 so a
    stray ``2.0`` can't render as a bogus level — any non-zero reading counts as "ok"."""
    n = _finite_number(value)
    return None if n is None else int(n != 0)


def _matching_reading(obj: dict, model: "str | None", sensor_id: "str | None",
                      channel: "str | None" = None):
    """A :class:`Reading` if ``obj`` matches the optional ``model`` / ``sensor_id`` / ``channel`` filters
    AND carries a finite ``temperature_C`` — else ``None`` (so a same-id non-TH packet, e.g. a keyfob, is
    ignored). Each filter is an EXACT, case-sensitive string match (``id`` / ``channel`` are ``str()``-ed
    first, so int ``1`` matches ``"1"``), skipped when its value is unset/empty. ``sensor_id == "*"`` is a
    WILDCARD (match any id) — for a sensor whose id randomises on each battery change. ``channel`` is the
    physical DIP switch, stable across battery changes; when a channel filter is set, a packet that omits
    ``channel`` is REJECTED (it is a different device)."""
    if model and obj.get("model") != model:
        return None
    if sensor_id and sensor_id != "*" and str(obj.get("id")) != sensor_id:
        return None
    if channel:
        ch = obj.get("channel")
        if ch is None or str(ch) != channel:
            return None
    temp = _finite_number(obj.get("temperature_C"))
    if temp is None:
        return None
    return Reading(temp, _finite_number(obj.get("humidity")), _battery_ok(obj.get("battery_ok")))


_TERMINATE_GRACE = 5.0   # seconds to let rtl_433 exit on SIGTERM before escalating to SIGKILL


async def _reap(proc) -> None:
    """Await the (already-signalled) child's exit to completion, DEFERRING a single cancellation until the
    reap is done — then re-raising it. Safe because the caller has already signalled the child, so
    ``proc.wait()`` returns promptly; this guarantees a shutdown cancel can never leave a SIGKILLed child
    unreaped (a zombie) while still propagating the cancellation."""
    pending = None
    while proc.returncode is None:
        try:
            await proc.wait()
        except asyncio.CancelledError as exc:            # NOSONAR S7497: deferred re-raise — the cancel is re-raised via `raise pending` after the child is fully reaped (see docstring)
            pending = exc
    if pending is not None:
        raise pending


async def _terminate(proc) -> None:
    """Terminate + reap ``proc`` so a finished/cancelled stream never leaves an orphan ``rtl_433`` holding
    the single-client SDR. BOUNDED — SIGTERM, a short grace period, then SIGKILL — so a child wedged in a
    blocking USB/socket call (an SDR/rtl_tcp failure) can never hang the poller's relaunch loop or the
    shutdown ``gather()``. On a timeout, OR a cancellation arriving mid-reap, it escalates to SIGKILL and
    still reaps the child (:func:`_reap`); a cancellation is re-raised afterwards so shutdown propagates.
    Tolerant of an already-exited child."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):    # raced us to exit between the check and the signal
            proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), _TERMINATE_GRACE)
    except TimeoutError:                                 # SIGTERM ignored/wedged -> force it, then reap
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await _reap(proc)
    except asyncio.CancelledError:                       # shutdown cancelling us mid-reap -> force, reap, propagate
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await _reap(proc)
        raise


async def stream_readings(
    on_reading: "Callable[[Reading], Awaitable[None]] | None" = None,
    *,
    device: str = DEFAULT_DEVICE,
    model: "str | None" = None,
    sensor_id: "str | None" = None,
    channel: "str | None" = None,
    protocol: "str | None" = None,
    extra_args: "str | Sequence[str] | None" = None,
    on_packet: "Callable[[dict], None] | None" = None,
    crib_model: "str | None" = None,
    crib_id: "str | None" = None,
    crib_channel: "str | None" = None,
    on_crib_reading: "Callable[[Reading], Awaitable[None]] | None" = None,
    binary: str = RTL_433_BIN,
    create=asyncio.create_subprocess_exec,
) -> None:
    """Spawn a long-lived ``rtl_433 -F json`` and push each matching finite reading to ``on_reading``
    (outdoor) or ``on_crib_reading`` (crib) in real time. Each decoded packet is routed to AT MOST ONE
    target; if a packet matches both active filters, it is treated as a config error, logged once, and
    routed to neither. Returns when the process exits or its stdout closes, so the caller can restart it
    after a backoff. NEVER raises on a malformed line (raises :class:`ValueError` upfront on invalid
    ``extra_args``). ``on_packet(obj)``, if given, is called for EVERY decoded packet (pre-filter) —
    the 433 discovery tap. The child is always terminated AND reaped on return/cancel (see
    :func:`_terminate`). ``create`` is injectable so tests never spawn a real process."""
    cmd = _rtl433_command(binary, device, protocol, extra_args)
    outdoor_active = on_reading is not None
    crib_active = bool(crib_model)
    if not outdoor_active and not crib_active:
        return
    try:
        proc = await create(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (OSError, ValueError) as exc:                 # binary missing / bad argv -> caller backs off + retries
        # A spawn failure is a CONFIG error (missing rtl_433, bad device/argv), not a transient rtl_tcp drop
        # (which spawns fine then exits via EOF). Log it concisely — once per backoff — so it is not silent,
        # without the traceback spam that re-raising would produce on every retry.
        log.warning("rtl_433 spawn failed (%s): %s", binary, exc)
        return
    ambiguous_logged = False
    try:
        stdout = proc.stdout
        if stdout is not None:
            async for raw in stdout:                     # yields one buffered line per decoded packet, in real time
                obj = _json_line(raw.decode("utf-8", "replace"))   # never raises on undecodable bytes
                if obj is not None:
                    if on_packet is not None:
                        on_packet(obj)                             # discovery: EVERY decoded packet, pre-filter
                    outdoor = _matching_reading(obj, model, sensor_id, channel) if outdoor_active else None
                    crib = _matching_reading(obj, crib_model, crib_id, crib_channel) if crib_active else None
                    if outdoor is not None and crib is not None:
                        if not ambiguous_logged:
                            log.error(
                                "rtl_433 config error: packet (model=%r, id=%r, channel=%r) "
                                "matches both outdoor and crib filters — ignoring",
                                obj.get("model"), obj.get("id"), obj.get("channel"),
                            )
                            ambiguous_logged = True
                    elif outdoor is not None:
                        await on_reading(outdoor)
                    elif crib is not None and on_crib_reading is not None:
                        await on_crib_reading(crib)
    finally:
        await _terminate(proc)
