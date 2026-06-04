"""Transparent proxy: device ↔ hestia ↔ the Keemple cloud.

The gateway is redirected to us (a Pi-hole override of ``gateway.keemple.com`` →
this host). For every device connection we open an upstream socket to the real
cloud (``1.2.3.4:8925``) and relay **raw bytes verbatim** in both directions
— we never reserialize the stream, so we cannot corrupt it. In parallel we *tap*
a copy through a `Deframer`: decode + log every frame and feed device reports
into a shared `State`.

This is also where we *inject*: a newline-JSON control listener
(``127.0.0.1:8926``) forges commands with `hestia.commands` and writes them
straight to the device — letting us drive live devices through the proxy while
the cloud stays in charge. That control surface is the seed of the future Home
Assistant API (newline-delimited JSON, not MQTT) and reuses the same session /
reader / writer the standalone server will need.

All mutable state lives in a `ProxyRuntime` passed explicitly (no module
globals), which keeps the moving parts unit-testable. The control port is
unauthenticated, so it refuses to bind beyond loopback without an explicit
opt-in. Injected command sequence numbers avoid the ``0x7e`` frame delimiter so a
forged frame never embeds a false flag byte.

Run:  python -m hestia.proxy
Env:  HESTIA_CLOUD_HOST (seed / literal pin) / HESTIA_CLOUD_HOSTNAME (opt-in DoH
      resolution) / HESTIA_CLOUD_PORT / HESTIA_LISTEN_HOST /
      HESTIA_LISTEN_PORT / HESTIA_CONTROL_HOST / HESTIA_CONTROL_PORT /
      HESTIA_CONTROL_ALLOW_REMOTE (=1 to allow a non-loopback control bind)
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import os
import signal
import time
from dataclasses import dataclass, field

from . import commands, resolve
from .automations import AutomationEngine, AutomationStore, Rule, read_present_macs
from .classifier import Classifier, DeviceType
from .protocol import FLAG, FRAME_TYPES, Deframer, Frame
from .registry import Registry
from .rf433 import Rf433Registry
from .state import State, _bool, _int, tlv_value
from .tuya import TuyaDevice, TuyaError
from . import flipper, sensor433, weather

log = logging.getLogger("hestia.proxy")

CLOUD_HOST = os.environ.get("HESTIA_CLOUD_HOST", "1.2.3.4")   # NOSONAR S1313: placeholder default; real cloud IP via $HESTIA_CLOUD_HOST
CLOUD_HOSTNAME = os.environ.get("HESTIA_CLOUD_HOSTNAME")          # opt-in: DoH-resolve this instead
CLOUD_PORT = int(os.environ.get("HESTIA_CLOUD_PORT", "8925"))
LISTEN_HOST = os.environ.get("HESTIA_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("HESTIA_LISTEN_PORT", "8925"))
CONTROL_HOST = os.environ.get("HESTIA_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("HESTIA_CONTROL_PORT", "8926"))
REGISTRY_PATH = os.environ.get("HESTIA_REGISTRY", "registry.json")
AUTOMATIONS_PATH = os.environ.get("HESTIA_AUTOMATIONS", "automations.json")
# DHCP lease file for `presence` automation triggers (dnsmasq format). Pi-hole uses
# `/etc/pihole/dhcp.leases`; set HESTIA_LEASES to it. hestia must be able to READ this file.
LEASES_PATH = os.environ.get("HESTIA_LEASES", "/var/lib/misc/dnsmasq.leases")
AUTOSAVE_SECS = float(os.environ.get("HESTIA_AUTOSAVE_SECS", "30"))
def _scheduler_interval(raw: str) -> float:
    """Parse HESTIA_SCHEDULER_SECS, clamped to [1, 59] — under 60 so every minute is
    observed, at least 1 so a misconfigured value can't busy-loop the scheduler. A
    non-numeric or non-finite (NaN/Infinity) value falls back to the 20 s default rather
    than producing a pathological sleep."""
    try:
        value = float(raw)
    except ValueError:
        return 20.0
    if not math.isfinite(value):
        return 20.0
    return min(max(value, 1.0), 59.0)


SCHEDULER_SECS = _scheduler_interval(os.environ.get("HESTIA_SCHEDULER_SECS", "20"))


def _thermostat_poll_interval(raw: "str | None") -> float:
    """Parse HESTIA_THERMOSTAT_POLL_SECS. ``<= 0`` disables the poller; otherwise clamp to [30, 3600]
    (gentle on battery FLiRS thermostats — the on/off mode rarely changes). Non-numeric / non-finite
    → the 90 s default."""
    try:
        value = float(raw) if raw is not None else 90.0
    except ValueError:
        return 90.0
    if not math.isfinite(value):
        return 90.0
    if value <= 0:
        return 0.0
    return min(max(value, 30.0), 3600.0)


THERMOSTAT_POLL_SECS = _thermostat_poll_interval(os.environ.get("HESTIA_THERMOSTAT_POLL_SECS"))


def _coord(raw: "str | None", lo: float, hi: float) -> "float | None":
    """Parse a coordinate env var (decimal degrees) to a float in ``[lo, hi]``, or ``None`` when
    unset / malformed / non-finite / out of range — so an unset or bad ``HESTIA_LAT``/``HESTIA_LON``
    simply disables `sun` automation rules instead of crashing startup."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or not lo <= value <= hi:
        return None
    return value


# Deployment location for `sun` (sunrise/sunset) automation triggers; unset → sun rules don't fire.
HESTIA_LAT = _coord(os.environ.get("HESTIA_LAT"), -90.0, 90.0)
HESTIA_LON = _coord(os.environ.get("HESTIA_LON"), -180.0, 180.0)
WEB_HOST = os.environ.get("HESTIA_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("HESTIA_WEB_PORT", "8927"))

# Tuya baby-monitor (Neno) temperature poller — reads the crib temp (DP, ÷scale = °C) over the LAN,
# cloud-free, into State.crib_temp + the `crib_temp` global-field triggers. OFF (zero network) unless
# HESTIA_NIANIA_IP/ID/KEY are all set. The Neno LUI is a Tuya v3.3 device22; its temp is local DP 238.
NIANIA_IP = os.environ.get("HESTIA_NIANIA_IP", "")
NIANIA_ID = os.environ.get("HESTIA_NIANIA_ID", "")
NIANIA_KEY = os.environ.get("HESTIA_NIANIA_KEY", "")


def _int_env(raw: "str | None", default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _pos_float_env(raw: "str | None", default: float) -> float:
    """A finite, strictly-positive float (else the default) — for HESTIA_NIANIA_SCALE."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _clamp_secs(raw: "str | None", default: float, lo: float, hi: float) -> float:
    """A poll-interval seconds value clamped to ``[lo, hi]``; non-numeric/non-finite → ``default``.
    Shared by the baby-monitor and weather pollers so a misconfigured interval can't busy-loop or stall."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, lo), hi)


def _niania_interval(raw: "str | None") -> float:
    """HESTIA_NIANIA_SECS clamped to [30, 3600]; non-numeric/non-finite → 90 s default."""
    return _clamp_secs(raw, 90.0, 30.0, 3600.0)


def _truthy_env(raw: "str | None") -> bool:
    """True ONLY for an explicit opt-in token (``1/true/yes/on``, case-insensitive). An unset / blank /
    false-ish / unknown value is False — so a feature that performs network egress (the weather poller)
    can never be enabled by accident."""
    return raw is not None and raw.strip().lower() in ("1", "true", "yes", "on")


def _num_env(name: str, default: float, lo: float, hi: float) -> float:
    """A numeric env knob clamped to ``[lo, hi]``: an unset / non-numeric / non-finite / out-of-range
    value falls back to ``default`` (so a typo like ``HESTIA_SSE_KEEPALIVE=0`` can't tight-loop, and a
    negative cap can't crash the Semaphore). The range check rejects NaN/inf (any comparison is False)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if lo <= value <= hi else default


NIANIA_TEMP_DP = _int_env(os.environ.get("HESTIA_NIANIA_TEMP_DP"), 238)
NIANIA_SCALE = _pos_float_env(os.environ.get("HESTIA_NIANIA_SCALE"), 10.0)
NIANIA_SECS = _niania_interval(os.environ.get("HESTIA_NIANIA_SECS", "90"))

# Outdoor temperature poller (Open-Meteo) — OPT-IN, OFF by default → zero network egress unless
# HESTIA_OUTDOOR_TEMP is an explicit truthy token. When enabled it sends HESTIA_LAT/LON to
# api.open-meteo.com over HTTPS to fill State.outdoor_temp + the `outdoor_temp` global-field triggers.
OUTDOOR_TEMP_ENABLED = _truthy_env(os.environ.get("HESTIA_OUTDOOR_TEMP"))
OUTDOOR_SECS = _clamp_secs(os.environ.get("HESTIA_OUTDOOR_SECS"), 600.0, 60.0, 3600.0)

# Which feeder fills outdoor_temp when the feature is enabled: "open-meteo" (default) = the cloud
# forecast (hestia.weather); "local" = a 433 MHz sensor via rtl_433 (on-LAN, zero egress). Mutually
# exclusive — at most one feeder polls; an unknown/typo value disables outdoor_temp (fail-safe).
OUTDOOR_TEMP_SOURCE = os.environ.get("HESTIA_OUTDOOR_TEMP_SOURCE", "open-meteo").strip().lower()

# Local 433 MHz sensor (rtl_433) feeder — used only when OUTDOOR_TEMP_SOURCE == "local". rtl_433 is an
# external system binary streamed long-lived (a PUSH source, not polled). RTL433_DEVICE must be a DEDICATED
# SDR/rtl_tcp endpoint (NOT one shared with an FM/RDS receiver, and not two readers at once — rtl_tcp serves
# a single client, so a second rtl_433 silently blocks). MODEL/ID/PROTOCOL narrow which sensor.
RTL433_DEVICE = os.environ.get("HESTIA_RTL433_DEVICE", sensor433.DEFAULT_DEVICE)
RTL433_MODEL = os.environ.get("HESTIA_RTL433_MODEL")        # None/"" -> no model filter
RTL433_ID = os.environ.get("HESTIA_RTL433_ID")              # None/"" -> no id filter
RTL433_PROTOCOL = os.environ.get("HESTIA_RTL433_PROTOCOL")  # None/"" -> all default decoders
# Delay before relaunching rtl_433 after it exits (rtl_tcp restart / SDR hiccup) — avoids a tight respawn
# loop when the SDR is unreachable, while staying responsive once it returns.
RTL433_RESTART_SECS = _clamp_secs(os.environ.get("HESTIA_RTL433_RESTART_SECS"), 30.0, 1.0, 600.0)

# Flipper Zero IR transmit (HESTIA_FLIPPER truthy = enabled). Drives the Flipper over USB-serial via its
# RPC protobuf protocol to transmit a saved `.ir` signal (see hestia.flipper). OFF by default → no serial
# access at all, the `ir` control op errors, and `ir` rule actions are skipped. FLIPPER_DEV is the CDC
# serial port. IR_OP_TIMEOUT bounds how long a control-op request waits for its transmit; IR_QUEUE_MAX
# bounds the transmit backlog (a full queue drops fire-and-forget rule actions / errors the control op).
FLIPPER_ENABLED = _truthy_env(os.environ.get("HESTIA_FLIPPER"))
FLIPPER_DEV = os.environ.get("HESTIA_FLIPPER_DEV", flipper.DEFAULT_DEVICE)
IR_OP_TIMEOUT = _clamp_secs(os.environ.get("HESTIA_IR_OP_TIMEOUT"), 20.0, 5.0, 120.0)
IR_QUEUE_MAX = 16

# Klima dashboard panel: hestia parses the SIGNAL NAMES of a local copy of the generated klima `.ir`
# (HESTIA_KLIMA_IR, default <repo>/tools/klima.ir from tools/gen_klima_ir.py) into a mode+temp picker,
# and transmits the chosen signal from the on-Flipper SD file HESTIA_KLIMA_IR_FILE (default
# /ext/infrared/klima.ir — where the generator uploads it; FAT32 is case-insensitive). A missing /
# unreadable file → the panel simply does not appear (IR control is unaffected). NB a future Docker
# image must also ship the .ir (or set HESTIA_KLIMA_IR), else the panel is absent there — graceful.
KLIMA_IR_PATH = os.environ.get("HESTIA_KLIMA_IR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "klima.ir")
KLIMA_IR_FILE = os.environ.get("HESTIA_KLIMA_IR_FILE", "/ext/infrared/klima.ir")
_KLIMA_MAX_BYTES = 1_000_000   # cap the import-time read of HESTIA_KLIMA_IR (a regular .ir file is ~KB)


def _ir_buttons(raw: "str | None") -> list:
    """Parse ``HESTIA_IR_BUTTONS`` — an optional JSON array of ``{"label","file","button"}`` the
    dashboard renders as one-tap IR buttons. Keep only well-formed entries; any parse error / non-list
    → ``[]`` (the dashboard simply shows no IR buttons). Pure (no I/O) so it is import-time safe."""
    try:
        items = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if (isinstance(it, dict) and isinstance(it.get("label"), str) and it["label"]
                and isinstance(it.get("file"), str) and it["file"]
                and isinstance(it.get("button"), str) and it["button"]):
            out.append({"label": it["label"], "file": it["file"], "button": it["button"]})
    return out


IR_BUTTONS = _ir_buttons(os.environ.get("HESTIA_IR_BUTTONS"))


def _klima_signals(path: str, sd_file: str) -> dict:
    """Parse the signal NAMES of a Flipper ``.ir`` file into a structured klima control map for the
    dashboard: ``{"file", "modes": {mode:[sorted temps]}, "power_on": {mode:[sorted temps]}, "presets":
    [...]}``. ``"<mode>_<temp>"`` (temp all-digits) is a SET-MODE signal (adjusts a running unit) →
    ``modes``; an ``"on_"`` prefix marks the POWER-ON-to-mode class (turns an OFF unit on directly into
    that mode+temp) → ``power_on``; any other name (``off``, ``fan``, ``on_fan``) is a preset. A missing/
    unreadable file, or one with no signals, → ``{}`` (the panel just doesn't show). Data-driven (no
    hard-coded mode list) and total — it never raises (undecodable bytes are replaced, I/O errors
    swallowed) — so it is safe to call once at import like ``IR_BUTTONS``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            blob = fh.read(_KLIMA_MAX_BYTES)             # bound memory: a real .ir is ~KB, never MB
    except OSError:
        return {}
    names = [ln.split(":", 1)[1].strip() for ln in blob.splitlines() if ln.startswith("name:")]
    modes: "dict[str, set]" = {}
    power_on: "dict[str, set]" = {}
    presets: list = []
    for name in names:
        if not name:
            continue
        group, key = (power_on, name[3:]) if name.startswith("on_") else (modes, name)
        mode, sep, temp = key.rpartition("_")
        # ASCII decimal + bounded length so int() is ALWAYS safe: str.isdigit() is also true for
        # superscripts / other-script digits (e.g. "²") that int() REJECTS, and a giant digit run trips
        # int()'s string-conversion limit — either would raise at import. Such names fall to presets.
        if sep and mode and temp.isascii() and temp.isdigit() and len(temp) <= 3:
            group.setdefault(mode, set()).add(int(temp))
        elif name not in presets:
            presets.append(name)
    if not modes and not power_on and not presets:
        return {}
    return {"file": sd_file,
            "modes": {m: sorted(modes[m]) for m in sorted(modes)},
            "power_on": {m: sorted(power_on[m]) for m in sorted(power_on)},
            "presets": presets}


KLIMA = _klima_signals(KLIMA_IR_PATH, KLIMA_IR_FILE)


def parse_klima_command(button: str) -> "dict | None":
    """The optimistic klima (A/C) state implied by a successfully-transmitted IR signal NAME.

    IR is one-way (no device feedback), so the last command IS the state. ``off`` (when it is a real
    preset in the parsed signal map) → power off, no mode/temp — the caller retains the previous ones
    for display context. ``on_<mode>_<temp>`` that exists in the map's ``power_on`` group → power on at
    that mode+temp. Anything else (a set-mode *adjust*, a ``fan`` preset, an unknown / unsupported name)
    → ``None`` = leave klima state untouched. Validating against ``KLIMA`` means an arbitrary
    ``/api/ir`` body can never poison the state with an unsupported mode/temp shape."""
    if button == "off" and "off" in KLIMA.get("presets", ()):
        return {"power": False}
    if button.startswith("on_"):
        mode, sep, temp = button[3:].rpartition("_")
        if sep and mode and temp.isascii() and temp.isdigit() and len(temp) <= 3:
            value = int(temp)
            if value in KLIMA.get("power_on", {}).get(mode, ()):
                return {"power": True, "mode": mode, "temp": value}
    return None


def _niania_device():
    """A configured Tuya baby-monitor ``TuyaDevice`` (set to query the temp DP), or ``None`` when not
    configured (HESTIA_NIANIA_IP/ID/KEY) or the key is malformed → the poller no-ops, zero network."""
    if not (NIANIA_IP and NIANIA_ID and NIANIA_KEY):
        return None
    try:
        dev = TuyaDevice(NIANIA_IP, NIANIA_ID, NIANIA_KEY, timeout=5.0)
    except TuyaError as exc:
        log.warning("niania: bad config (%s) — crib-temperature polling disabled", exc)
        return None
    dev.dps_to_request = [NIANIA_TEMP_DP]
    return dev

# A function-button press (D->C) makes the cloud push its scene reaction — a batch
# [1e 32] (C->D) — within a fraction of a second. We learn that reaction by
# correlating the two within this window (see ProxySession._observe / §5.7a).
SCENE_CAPTURE_WINDOW = 2.0

# (type, cmd) pairs that are pure plumbing — logged at DEBUG, not INFO.
_NOISE = {(0x66, 0x01), (0x64, 0x03), (0x00, 0x00), (0x1E, 0x0A)}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _safe_seq_counter(start: int = 0x00010000):
    """Yield increasing 4-byte command sequence numbers whose big-endian encoding
    never contains FLAG (0x7e).

    The protocol does not byte-stuff, so a 0x7e inside a forged frame would read
    as a false delimiter; the cloud avoids this with small values, and so must
    we. Starts above the cloud's observed range and wraps rather than overflowing.
    """
    value = start
    while True:
        if FLAG not in value.to_bytes(4, "big"):
            yield value
        value = value + 1 if value < 0xFFFFFFFF else start


def _require_safe_control_bind(host: str) -> None:
    """The control port is unauthenticated and can forge arbitrary device
    commands, so refuse to expose it beyond loopback without an explicit opt-in."""
    if host not in _LOOPBACK and os.environ.get("HESTIA_CONTROL_ALLOW_REMOTE") != "1":
        raise RuntimeError(
            f"refusing to bind the unauthenticated control port to {host!r}; "
            "set HESTIA_CONTROL_ALLOW_REMOTE=1 to override (not recommended)"
        )


# --- Event bus: fan-out of `[1e 09]` activity + discovery diffs to SSE clients ---

_CLOSED_SENTINEL = object()                                  # marker pushed on shutdown


class Subscription:
    """One SSE client's view onto the event bus. ``close()`` is **synchronous**
    (def, not async def) because aiohttp SSE cleanup runs it in the event loop
    without awaiting another task."""

    def __init__(self, bus: "EventBus", queue: asyncio.Queue, sem: asyncio.Semaphore):
        self.bus = bus
        self.queue = queue
        self.sem = sem

    def close(self) -> None:
        if self.queue is None:
            return                                            # idempotent
        self.bus._drop(self.queue)
        self.sem.release()
        self.queue = None


# Max concurrent SSE subscribers. A page reload opens a new stream while the old one still holds its
# slot until the server's next keepalive write detects the disconnect (~HESTIA_SSE_KEEPALIVE s), so a
# burst of reloads (rapid F5) could exhaust a small cap → 429. 32 (was 8) absorbs that for a home box;
# env-tunable. NOTE: a WebSocket would NOT change this — it's connection churn, not the SSE transport.
_SSE_MAX_SUBS = int(_num_env("HESTIA_SSE_MAX_SUBS", 32, 1, 4096))   # ≥1 so the Semaphore is always valid


class EventBus:
    """Loop-owned fan-out for activity / discovery events.

    All methods run in the asyncio loop thread. ``publish`` is non-blocking — a slow
    subscriber's full queue silently drops the event rather than back-pressuring
    the proxy/standalone session. ``close()`` forces a ``_CLOSED_SENTINEL`` into
    every subscriber queue, draining one stale event if needed so the sentinel
    reaches even a full-queue subscriber — guarantees clean SSE handler exit on shutdown."""

    def __init__(self, max_subs: int = _SSE_MAX_SUBS):
        self._subs: "set[asyncio.Queue]" = set()
        self._sem = asyncio.Semaphore(max_subs)
        self._closing = False

    async def try_subscribe(self, maxsize: int = 64) -> "Subscription | None":
        """Returns ``None`` if the cap is reached or the bus is closing; else a
        ready subscription. Double-checks ``_closing`` after the acquire to
        avoid handing out a subscription the shutdown sentinel has missed."""
        if self._closing or self._sem.locked():
            return None
        await self._sem.acquire()
        if self._closing:  # NOSONAR S2583: close() flips _closing on another task while we await the semaphore — concurrency re-check, not dead code
            self._sem.release()
            return None
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return Subscription(self, q, self._sem)

    def _drop(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event) -> None:
        if self._closing:
            return                                            # deterministic post-close no-op
        for q in self._subs:                                  # sync loop (no await) → no concurrent unsubscribe can mutate the set mid-iteration
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass                                          # slow consumer; never back-pressure publisher

    def close(self) -> None:
        """Push ``_CLOSED_SENTINEL`` into every subscriber queue so SSE handlers
        wake from ``_wait_event`` and exit cleanly. If a queue is full, drain
        one event to make room — the operator's shutdown intent takes priority
        over a pending activity event."""
        self._closing = True
        for q in self._subs:                                  # sync loop (no await) → set can't change mid-iteration
            while True:
                try:
                    q.put_nowait(_CLOSED_SENTINEL)
                    break
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()                        # drop one stale event
                    except asyncio.QueueEmpty:                # racy emptied → give up
                        break


@dataclass
class ProxyConfig:
    """Where the proxy listens and what it dials upstream."""
    listen_host: str = LISTEN_HOST
    listen_port: int = LISTEN_PORT
    cloud_host: str = CLOUD_HOST
    cloud_port: int = CLOUD_PORT
    control_host: str = CONTROL_HOST
    control_port: int = CONTROL_PORT
    registry_path: str = REGISTRY_PATH
    automations_path: str = AUTOMATIONS_PATH
    web_host: str = WEB_HOST
    web_port: int = WEB_PORT


@dataclass
class ProxyRuntime:
    """Shared mutable state for one running proxy (replaces module globals).

    `save_lock` serialises every registry write so two `os.replace` operations
    can never race — without it, the default `ThreadPoolExecutor` runs writes
    concurrently and a stale autosave snapshot can clobber a fresh user edit."""
    state: State = field(default_factory=State)
    sessions: list = field(default_factory=list)
    classifier: Classifier = field(default_factory=Classifier)
    registry: Registry = field(default_factory=lambda: Registry(REGISTRY_PATH))
    save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    event_bus: EventBus = field(default_factory=EventBus)
    # Which session implementation is running — automations carry a `modes` allow-list so
    # a rule can opt out of one mode (e.g. skip proxy if it duplicates a cloud automation).
    mode: str = "proxy"
    engine: AutomationEngine = field(
        default_factory=lambda: AutomationEngine(AutomationStore(AUTOMATIONS_PATH)))
    # Location for `sun` triggers (decimal degrees, N/E positive); default from HESTIA_LAT/LON env,
    # None when unset/invalid → sun rules are skipped. The engine reads these in `on_time`.
    lat: "float | None" = HESTIA_LAT
    lon: "float | None" = HESTIA_LON
    # Flipper IR transmit backlog. Created in ``main()`` only when ``HESTIA_FLIPPER`` is enabled; ``None``
    # otherwise, so every IR entry point (the ``ir`` control op, ``_ir_worker``, the engine's ``_fire``)
    # degrades to a clean no-op in BOTH proxy and standalone modes when the feature is off. FIFO of
    # ``(ir_file, button, future|None)`` drained by ``_ir_worker``.
    ir_queue: "asyncio.Queue | None" = None
    # SQLite audit-log engine (#56). Set in ``main()`` (the DB always exists post-boot); ``None`` in
    # tests / bare runtimes, where ``_audit`` degrades to a clean no-op. Used off the event loop.
    audit_engine: "object | None" = None
    # 433 MHz device discovery: in-memory roll-up of every decoded rtl_433 packet (the local-433
    # feeder filters to one sensor; this captures all of them). Display-only; reset on restart.
    rf433: Rf433Registry = field(default_factory=Rf433Registry)

    def __post_init__(self) -> None:
        self._seq = _safe_seq_counter()

    def next_seq(self) -> int:
        return next(self._seq)


def summarize(frame: Frame) -> str:
    """One compact line for live logging (Frame.__str__ is multi-line/verbose)."""
    if len(frame.body) < 4:
        return f"[short] {frame.body.hex()}"
    tname = FRAME_TYPES.get(frame.type, f"{frame.type:#04x}")
    tlvs = " ".join(f"{t.tag:#06x}={t.value.hex()}" for t in frame.tlvs())
    bad = "" if frame.checksum_ok else " !cksum"
    return f"[{tname} c{frame.cmd:02x}]{bad} {tlvs}".rstrip()


async def _close(writer: "asyncio.StreamWriter | None") -> None:
    if writer is None:
        return
    try:
        writer.close()
        await writer.wait_closed()
    except OSError:
        pass


def _event_node(frame: Frame) -> "bytes | None":
    return tlv_value(frame, 0x0047) if (frame.type, frame.cmd) == (0x1E, 0x09) else None


def _log_state_changes(node_b: bytes, changed: dict) -> None:
    for key, value in changed.items():
        log.info("  ~ [%#04x] %s = %s", node_b[0], key, value)


def _publish_proxy_events(rt, frame: Frame, node_b: "bytes | None", changed: dict, scene) -> None:
    if _feed_discovery(rt, frame):          # identity changed → full refetch
        rt.event_bus.publish({"type": "discovery_changed"})
    elif changed and node_b:                # live value(s) → cheap cell patch
        rt.event_bus.publish({"type": "state", "node": node_b[0], "fields": changed})
    if node_b:
        event = {"type": "activity", "node": node_b[0], "ts": time.time()}
        if scene:                           # function-button scene rides the flash
            event["scene"] = scene
        rt.event_bus.publish(event)         # heatmap: row flashes (every event)


def _switch_op_from_payload(node: int, data: bytes) -> "dict | None":
    """A switch / 2-gang control op for a command payload — the ``0x0046`` of a ``[1e 07]`` SET, or a
    ``[1e 32]`` scene-batch element body. Cover/level/thermostat payloads → None (they report their own
    state, so we trust the report — mirrors ``State.apply_command``'s switch-only policy)."""
    if data[:2] == b"\x25\x01" and len(data) >= 3:                  # binary switch SET: 25 01 <ff/00>
        return {"op": "switch", "node": node, "on": data[2] == 0xFF}
    if data[:2] == b"\x60\x0d" and len(data) >= 7 and data[4:6] == b"\x25\x01":  # 2-gang SET: 60 0d 00 <ep> 25 01 <v>
        return {"op": "switch", "node": node, "endpoint": data[3], "on": data[6] == 0xFF}
    return None


def _scene_batch_ops(frame) -> list:
    """The switch / 2-gang ops bundled in a ``[1e 32]`` scene batch's ``0x005a`` element block — e.g.
    the Keemple app's "all lights on/off", which is NOT individual ``[1e 07]`` commands but one batch.
    Each element is ``<idx> <01> <cmdlen> <node> <cmd-payload>``, the payload being the same SET form a
    ``[1e 07]`` carries. A truncated trailing element stops the walk."""
    elements = tlv_value(frame, 0x005A)
    ops: list = []
    if not elements:
        return ops
    i = 0
    while i + 4 <= len(elements):
        cmdlen = elements[i + 2]
        payload = elements[i + 4: i + 4 + cmdlen]
        if len(payload) < cmdlen:                                  # truncated trailing element
            break
        op = _switch_op_from_payload(elements[i + 3], payload)     # elements[i+3] = the node
        if op is not None:
            ops.append(op)
        i += 4 + cmdlen
    return ops


def _command_ops_from_frame(frame) -> list:
    """Every switch / 2-gang state change a cloud→device command frame would actuate — so proxy mode
    learns it from the cloud/app (those relays only ACK ``[1e 08]``, never report ``[1e 09]``): a single
    ``[1e 07]`` SET, or each element of a ``[1e 32]`` scene batch. Cover/level/thermostat → skipped."""
    if (frame.type, frame.cmd) == (0x1E, 0x07):
        node_b = tlv_value(frame, 0x0047)
        data = tlv_value(frame, 0x0046)
        if node_b and data:
            op = _switch_op_from_payload(node_b[0], data)
            return [op] if op is not None else []
        return []
    if (frame.type, frame.cmd) == (0x1E, 0x32):
        return _scene_batch_ops(frame)
    return []


def _echo_command_frame(rt, raw: bytes) -> None:
    """Echo the switch/2-gang state a command frame hestia JUST WROTE to a device produces — those
    relays only ACK (never report ``[1e 09]``), so without this a hestia-sent switch change (UI
    control, a fired automation, a scheduled rule) would never reach State / the live UI. Called
    AFTER a successful write, so a dropped or failed send can never fake a state change.

    The Deframer yields ONLY checksum-valid frames, so a ``raw`` control-op frame the device would
    ignore (bad checksum / no flags) yields nothing here and can't fake state. Cover/level/thermostat
    commands echo nothing (they report their own state). A ``[1e 32]`` scene batch echoes each of its
    bundled switch/2-gang elements (see ``_command_ops_from_frame`` / ``_scene_batch_ops``)."""
    for body in Deframer().feed(raw):
        for op in _command_ops_from_frame(Frame(body)):
            _publish_command_state(rt, op)


def _arm_pending_scene(session, node_b: "bytes | None", scene, direction: str) -> None:
    if scene and node_b and direction == "D->C":  # press → await its cloud reaction
        session._pending_scene = (node_b[0], scene["id"], time.monotonic())


def _proxy_engine_injects(rt, node_b: "bytes | None", changed: dict, scene, direction: str) -> list:
    # Automations: only device->cloud reports yield changed/scene, so gate on D->C
    # (belt-and-suspenders + skips per-heartbeat iteration).
    if direction == "D->C" and node_b and (changed or scene):
        return rt.engine.on_event(rt, node_b[0], changed, scene)
    return []


class ProxySession:
    """One device↔cloud pairing: raw relay both ways plus a decoding tap."""

    def __init__(self, rt, dev_reader, dev_writer, cloud_host, cloud_port):
        self.rt = rt
        self.dev_reader = dev_reader
        self.dev_writer = dev_writer
        self.cloud_host = cloud_host
        self.cloud_port = cloud_port
        self.peer = dev_writer.get_extra_info("peername")
        # A function-button press (D->C) awaiting its cloud scene reaction (C->D),
        # as (node, scene_id, monotonic_ts). Session-local: the press and its batch
        # both flow through THIS session's _observe, so a press can never be matched
        # to another stream's traffic, and it is discarded with the session.
        self._pending_scene = None

    async def run(self) -> None:
        try:
            cloud_reader, cloud_writer = await asyncio.open_connection(
                self.cloud_host, self.cloud_port
            )
        except OSError:
            log.exception("upstream %s:%d unreachable for %s",
                          self.cloud_host, self.cloud_port, self.peer)
            await _close(self.dev_writer)
            return

        log.info("+ device %s <-> cloud %s:%d", self.peer, self.cloud_host, self.cloud_port)
        self.rt.sessions.append(self)
        up = asyncio.create_task(self._pump(self.dev_reader, cloud_writer, "D->C"))
        down = asyncio.create_task(self._pump(cloud_reader, self.dev_writer, "C->D"))
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    log.warning("pump for %s failed: %r", self.peer, exc)
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            await _close(cloud_writer)
            await _close(self.dev_writer)
            self.rt.sessions.remove(self)   # always present once we reach here
            log.info("- device %s", self.peer)

    async def _pump(self, reader, writer, direction) -> None:
        """Relay raw bytes from reader to writer, tapping a decoded copy."""
        deframer = Deframer()
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)            # verbatim — the stream is never rebuilt
            await writer.drain()
            for body in deframer.feed(data):
                for raw in self._observe(Frame(body), direction):
                    await self.inject_to_device(raw)   # automation actions -> the DEVICE,
                                                       # never `writer` (cloud-bound for D->C)

    def _observe(self, frame: Frame, direction: str) -> "list[bytes]":
        if len(frame.body) < 4:           # a mis-split / corrupt fragment
            log.debug("%s [short] %s", direction, frame.body.hex())
            return []
        level = logging.DEBUG if (frame.type, frame.cmd) in _NOISE else logging.INFO
        log.log(level, "%s %s", direction, summarize(frame))
        if not frame.checksum_ok:         # don't act on a corrupt frame
            return []
        changed = self.rt.state.apply(frame)
        node_b = _event_node(frame)
        if node_b:                        # changed is only ever non-empty here
            _log_state_changes(node_b, changed)
        scene = changed.pop("scene", None)   # a button press: an event, not state
        _arm_pending_scene(self, node_b, scene, direction)
        _publish_proxy_events(self.rt, frame, node_b, changed, scene)
        if direction == "C->D":              # cloud/app commanded switch(es) — echo (relays don't report)
            for cmd_op in _command_ops_from_frame(frame):   # a single SET or a whole scene batch (all-lights)
                _publish_command_state(self.rt, cmd_op)
        self._capture_scene_batch(frame, direction)
        return _proxy_engine_injects(self.rt, node_b, changed, scene, direction)

    def _capture_scene_batch(self, frame: Frame, direction: str) -> None:
        """Learn the cloud's reaction to a function-button press so standalone mode
        can replay it (§5.7a). When a batch ``[1e 32]`` (cloud->device) arrives within
        ``SCENE_CAPTURE_WINDOW`` of a press, store its ``0x005a`` element block keyed by
        ``(node, scene_id)``. First eligible batch wins, then the pending press is
        consumed; a ``[1e 32]`` with no ``0x005a`` leaves the press pending, and a stale
        press (outside the window) is inert. Only checksum-valid frames reach here.

        Heuristic limit: one pending slot per session means two presses on *different*
        nodes within the window can cross-attribute the batch. The window is short, a
        human presses one button at a time, re-pressing re-learns, and replay is
        standalone-only — so a mis-learn cannot actuate until the operator verifies it
        via the ``scenes`` control op and the "learned scene" log line."""
        if direction != "C->D" or (frame.type, frame.cmd) != (0x1E, 0x32):
            return
        pend = self._pending_scene
        if not pend:
            return
        if time.monotonic() - pend[2] > SCENE_CAPTURE_WINDOW:
            self._pending_scene = None               # stale press → forget it (explicit lifecycle)
            return
        elements = tlv_value(frame, 0x005A)
        if not elements:                             # not a switch-batch; leave the press pending
            return
        if self.rt.registry.record_scene(pend[0], pend[1], elements.hex()):
            log.info("learned scene %d for node %#04x (%d-byte batch)",
                     pend[1], pend[0], len(elements))
        self._pending_scene = None                   # first eligible batch consumed

    async def inject_to_device(self, raw: bytes) -> None:
        self.dev_writer.write(raw)
        await self.dev_writer.drain()
        log.info("INJECT -> %s %s", self.peer, raw.hex())
        _echo_command_frame(self.rt, raw)            # post-send: a switch/2-gang set never reports — echo it


# --- Control: newline-JSON op -> forged command injected to the device --------

def build_command(rt, op) -> bytes:
    """Translate one control op into a complete device-command frame."""
    if not isinstance(op, dict):
        raise ValueError("expected a JSON object")
    handler = _OPS.get(op.get("op"))
    if handler is None:
        raise ValueError(f"unknown op {op.get('op')!r}")
    return handler(rt, op)


def _switch_command(rt, op) -> bytes:
    endpoint = op.get("endpoint")
    if endpoint is not None:
        return commands.set_endpoint_switch(
            rt.next_seq(), _int(op["node"]), _int(endpoint), _bool(op["on"]))
    return commands.set_switch(rt.next_seq(), _int(op["node"]), _bool(op["on"]))


_OPS = {
    "raw": lambda rt, op: bytes.fromhex(op["hex"]),
    "cover": lambda rt, op: commands.set_cover(rt.next_seq(), _int(op["node"]), _int(op["value"])),
    "level": lambda rt, op: commands.set_level(rt.next_seq(), _int(op["node"]), _int(op["value"])),
    "switch": _switch_command,
    "lights": lambda rt, op: commands.set_lights(rt.next_seq(), [(_int(c), _int(v)) for c, v in op["channels"]]),
    "thermostat": lambda rt, op: commands.set_thermostat(rt.next_seq(), _int(op["node"]), float(op["celsius"])),
    "thermostat_power": lambda rt, op: commands.set_thermostat_power(rt.next_seq(), _int(op["node"]), _bool(op["on"])),
}


def globals_snapshot(state: State) -> dict:
    """The node-less GLOBAL fields — surfaced to the dashboard (``/api/discovery``) and the ``state``
    control op. ``crib_temp`` / ``outdoor_temp`` are °C automation fields; ``outdoor_humidity`` is a
    display-only %RH companion from the local 433 feeder (no rule trigger). ``None`` when the relevant
    poller is off → JSON ``null``; the UI renders "—"."""
    return {"crib_temp": state.crib_temp, "outdoor_temp": state.outdoor_temp,
            "outdoor_humidity": state.outdoor_humidity}


def state_snapshot(state: State) -> dict:
    def hexkeys(mapping):
        return {f"{k:#04x}": v for k, v in mapping.items()}
    return {
        "doors": hexkeys(state.doors),
        "motion": hexkeys(state.motion),
        "levels": hexkeys(state.levels),
        "switches": hexkeys(state.switches),
        "thermostat_setpoint": hexkeys(state.thermostat_setpoint),
        "thermostat_on": hexkeys(state.thermostat_on),
        "temperature": hexkeys(state.temperature),
        # multi-gang: node→{ep:on}; stringify both key levels for clean JSON.
        "gang": {f"{n:#04x}": {str(ep): on for ep, on in eps.items()} for n, eps in state.gang.items()},
        "globals": globals_snapshot(state),
    }


def _feed_discovery(rt, frame) -> bool:
    """Tap one frame into device-type discovery. Only the *affected* nodes get
    mirrored to the registry: every roster node on a `[1e 15]`, the single
    `0x0047` node on a `[1e 09]` event, and nothing for other frames — so a
    heartbeat doesn't churn 22 entries' `last_seen` or dirty the file.

    Returns True iff a node's discovery *identity* (new node / type / power /
    battery) changed — the caller publishes a full `discovery_changed` then, vs a
    cheap `state` delta for a pure live-state change."""
    rt.classifier.ingest_roster(frame)
    rt.classifier.observe(frame)
    if (frame.type, frame.cmd) == (0x1E, 0x15):
        affected = list(rt.classifier.nodes.keys())
    elif (frame.type, frame.cmd) == (0x1E, 0x09):
        node_b = tlv_value(frame, 0x0047)
        affected = [node_b[0]] if node_b else []
    else:
        return False
    report = rt.classifier.report()
    identity_changed = False
    for node in affected:
        info = report[node]   # classifier.observe/ingest_roster already added the node
        if rt.registry.observe(node, info["type"], info["confidence"],
                               info.get("power"), info.get("battery")):
            identity_changed = True   # full scan — every node still mirrored
    return identity_changed


def _safe_int_key(key) -> "int | None":
    """Parse a registry node-key (decimal string) to int; warn + skip if bad."""
    try:
        return int(key)
    except ValueError:
        log.warning("ignoring non-integer registry key %r", key)
        return None


_UNKNOWN = "unknown"


def _discovery_type(cls: dict, reg: dict) -> tuple:
    cls_type, reg_type = cls.get("type"), reg.get("type")
    if reg.get("type_confirmed"):
        return reg_type, reg.get("confidence", "confirmed")
    if cls_type and cls_type != _UNKNOWN:
        return cls_type, cls.get("confidence")
    if reg_type and reg_type != _UNKNOWN:
        return reg_type, reg.get("confidence")
    # Nothing useful → "unknown"; never None — UI shouldn't have to handle null.
    return cls_type or reg_type or _UNKNOWN, cls.get("confidence") or reg.get("confidence") or _UNKNOWN


def _discovery_battery(cls: dict, reg: dict):
    cls_batt = cls.get("battery")
    return cls_batt if cls_batt is not None else reg.get("battery")


def _add_live_state(entry: dict, state: State, node: int) -> None:
    # Live device state (from rt.state, populated by the same `[1e 09]` events
    # that trigger this re-fetch). Always present, null when unseen — so the
    # client has one stable contract and `0`/`False` are never lost. The web
    # UI renders a type-aware "stan" column from these.
    entry["level"] = state.levels.get(node)                  # blind/dimmer 0..99
    entry["switch"] = state.switches.get(node)               # on/off relay
    entry["door"] = state.doors.get(node)                    # "open"/"closed"
    entry["motion"] = state.motion.get(node)                 # PIR: True=motion / False=idle
    entry["setpoint"] = state.thermostat_setpoint.get(node)  # °C target
    entry["thermostat_on"] = state.thermostat_on.get(node)   # bool
    entry["temperature"] = state.temperature.get(node)       # °C measured
    entry["power_w"] = state.plug_w.get(node)                # plug power, W
    entry["energy_kwh"] = state.plug_kwh.get(node)           # plug cumulative energy, kWh
    entry["voltage_v"] = state.plug_v.get(node)              # plug mains voltage, V
    entry["endpoints"] = state.gang.get(node)                # {ep: on} for a multi-gang switch


def _add_registry_labels(entry: dict, reg: dict) -> None:
    if "name" in reg:
        entry["name"] = reg["name"]
    if "room" in reg:
        entry["room"] = reg["room"]
    if "endpoint_names" in reg:                           # per-endpoint labels for a 2-gang switch
        entry["endpoint_names"] = dict(reg["endpoint_names"])   # copy — detach from registry state


def _discovery_entry(rt, node: int, cls: dict, reg: dict) -> dict:
    dtype, conf = _discovery_type(cls, reg)
    # Battery: live reading wins; else the last-known persisted value (so the
    # column isn't blank right after a restart). 0 % is valid, so test `is not
    # None`, never `or`. A node that ever reports a battery level IS battery-
    # powered — that overrides the roster flag, which mislabels battery FLiRS
    # devices (thermostats) as mains.
    battery = _discovery_battery(cls, reg)
    entry = {
        "power": "battery" if battery is not None else (cls.get("power") or reg.get("power")),
        "type": dtype,
        "confidence": conf,
        "battery": battery,
    }
    _add_live_state(entry, rt.state, node)
    _add_registry_labels(entry, reg)
    return entry


def _merged_discovery(rt) -> dict:
    """Merge the classifier's inference with the user registry. Canonical
    decimal-string keys. Precedence per field:
    - registry wins on a *user-confirmed* type / name / room;
    - otherwise the classifier wins when it has a real (non-`unknown`) type;
    - otherwise the registry's stored type (e.g. inferred in a previous session)
      survives — so restarting before classification re-runs doesn't lose data."""
    report = rt.classifier.report()                       # int-keyed
    reg_nodes = rt.registry.nodes                          # str-keyed
    reg_node_ids = {n for n in (_safe_int_key(k) for k in reg_nodes) if n is not None}
    devices = {}
    for node in sorted(set(report) | reg_node_ids):
        cls = report.get(node, {})
        reg = reg_nodes.get(str(node), {})
        devices[str(node)] = _discovery_entry(rt, node, cls, reg)
    return devices


async def _write_and_settle(write_payload, payload) -> "asyncio.CancelledError | None":
    """Run a blocking atomic write (``write_payload(payload)``) off the event loop and
    WAIT for it to settle even across our own cancellation. The executor THREAD can't be
    cancelled, so letting its ``os.replace`` land under the caller's lock is what stops a
    later save from starting a second, racing ``os.replace`` that reorders behind this
    one and clobbers newer state. We wait with ``asyncio.wait`` (NOT ``asyncio.shield``)
    and retrieve the future ourselves — so a cancelled wait never logs a stray
    'exception in shielded future', and the result is never an unretrieved exception.

    On SUCCESS, RETURNS the pending ``CancelledError`` (or ``None``) so the caller can
    bring live state in line with what just landed on disk BEFORE propagating the cancel
    — otherwise disk sits ahead of memory and a *later* write clobbers the durable change.
    On FAILURE, raises: the ``CancelledError`` if a cancel is also pending (so a caller's
    ``except OSError`` can't swallow a shutdown cancel), else the ``OSError``. Shared by
    ``_persist_obj`` and ``_commit_automation`` so both writes are detach-safe identically."""
    loop = asyncio.get_running_loop()
    fut = asyncio.ensure_future(loop.run_in_executor(None, write_payload, payload))
    cancelled = None
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError as exc:  # NOSONAR S7497: cancel is captured here and re-raised by the caller after the write settles (see docstring)
            cancelled = exc              # remember; keep waiting for the write to settle
    err = fut.exception()                # always retrieve — never an unretrieved-exception leak
    if err is not None:
        raise cancelled or err           # write failed: a pending cancel wins over the OSError
    return cancelled                     # write landed: hand the pending cancel back to the caller


async def _persist_obj(lock, obj) -> None:
    """Single-writer flush of one persistable (a `Registry` or an `AutomationStore` —
    anything with `dirty`/`serialize()`/`write_payload()`). Under `lock` (so no two
    writes can race-overwrite each other), we serialise in the event loop — atomic
    under the GIL, no concurrent-mutation race — then clear `dirty` *before* the await
    so a fresh mutation during the I/O re-arms the next tick rather than being silently
    clobbered when the in-flight write finishes. A transient I/O failure restores
    `dirty` and re-raises; the caller decides whether to log-and-retry (autosave) or
    report failure (a user op).

    The lock-wait re-check skips a redundant write if another caller (the autosave or a
    control op) already flushed our state while we were queued on the lock."""
    async with lock:
        if not obj.dirty:
            return
        payload = obj.serialize()
        obj.dirty = False
        try:
            cancelled = await _write_and_settle(obj.write_payload, payload)
        except (OSError, asyncio.CancelledError):
            obj.dirty = True             # write failed (± cancel) → re-arm for a retry
            raise
        if cancelled is not None:        # write LANDED but we were cancelled: state is saved
            raise cancelled              #   (disk == what we serialised; a concurrent observe()
            #                              during the write left dirty=True on its own) — propagate


async def _persist(rt) -> None:
    """Flush the device registry (see `_persist_obj`)."""
    await _persist_obj(rt.save_lock, rt.registry)


async def _persist_store(rt) -> None:
    """Flush the automations store — shares `save_lock` with the registry so the two
    `os.replace` operations can never interleave."""
    await _persist_obj(rt.save_lock, rt.engine.store)


def seed_device_state(rt) -> None:
    """Best-effort boot seed from the SQLite telemetry cache."""
    from . import store_sql
    snap = store_sql.load_device_state()
    if snap:
        rt.state.load_snapshot(snap)


async def _persist_state(rt) -> None:
    """Flush the best-effort live telemetry cache to SQLite, if that backend is active."""
    from . import store_sql
    if not store_sql._settings_enabled():
        return
    if not rt.state.dirty:
        return
    rt.state.dirty = False
    snapshot = rt.state.to_snapshot()
    loop = asyncio.get_running_loop()
    try:
        ok = await loop.run_in_executor(None, lambda: store_sql.save_device_state(snapshot))
    except Exception:
        log.exception("device-state cache save failed")
        rt.state.dirty = True
        return
    if not ok:
        rt.state.dirty = True


def _audit(rt, actor, action, *, target=None, detail=None, result=None):
    """Schedule a best-effort audit-log append (#56) OFF the event loop, FIRE-AND-FORGET: never
    awaited, so a slow or locked audit DB can't add latency to the action being recorded (a failed
    write is logged in the done-callback). ``actor`` is a username, ``automation:<rule_id>``, or
    ``system``/``anonymous``. No-op (returns None) when ``rt.audit_engine`` is unset. Returns the
    scheduled future (tests await it; callers ignore it)."""
    if rt.audit_engine is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None                         # no running loop (best-effort) — also lets the engine call this safely
    from . import store_sql
    fut = loop.run_in_executor(
        None, lambda: store_sql.append_audit(rt.audit_engine, actor=actor, action=action,
                                             target=target, detail=detail, result=result, ts=time.time()))
    fut.add_done_callback(_audit_done)
    return fut


def _audit_done(fut) -> None:
    """Done-callback for a fire-and-forget audit write — retrieve + log any failure (best-effort)."""
    try:
        exc = fut.exception()
    except asyncio.CancelledError:          # pragma: no cover - a pending audit write cancelled at shutdown
        return
    if exc is not None:
        log.debug("audit write failed", exc_info=exc)


# Meaningful device state TRANSITIONS to audit as a physical/external change (actor="device"). Telemetry
# (power_w/voltage_v/energy_kwh/temperature) is excluded so a chatty power meter can't flood the log.
_OBSERVED_AUDIT_FIELDS = ("door", "switch", "level", "endpoints", "thermostat_on", "setpoint")


def _audit_observed(rt, node, changed, scene) -> None:
    """Log a physical/external device state change (#56): someone flipped a switch / opened a door, or
    — in proxy mode — the cloud commanded it (hestia relays the cloud frame verbatim and only sees the
    device's resulting report, so the cause shows as ``device``). A change hestia itself caused is also
    recorded by its own user/automation row, so the log shows intent + the observed confirmation."""
    if rt.audit_engine is None:
        return
    for field in _OBSERVED_AUDIT_FIELDS:
        if field in changed:
            _audit(rt, "device", field, target=str(node), detail=str(changed[field])[:64], result="reported")
    if scene:
        _audit(rt, "device", "scene", target=str(node), result="reported")


def _install_term_handler(loop, task) -> bool:
    """Route SIGTERM into the same graceful unwind as SIGINT: cancel the running
    ``main`` task so the persist-on-exit ``finally`` runs and state is flushed.

    ``docker stop`` and ``systemctl stop`` send SIGTERM, whose default disposition
    terminates the process immediately — skipping the shutdown save (only the
    ~30 s autosave caps the loss, so a just-graduated mode or fresh rule edit can be
    lost). Cancelling the main task delivers one ``CancelledError`` at the
    ``serve_forever`` await, exactly the shape the SIGINT path already relies on.
    Best-effort and idempotent for a single TERM: returns ``False`` where a signal
    handler can't be installed (non-main thread, or a platform such as Windows
    without ``add_signal_handler``)."""
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        return True
    except (RuntimeError, ValueError):           # NotImplementedError ⊂ RuntimeError
        return False


async def _commit_automation(rt, change):
    """Durably create/replace/delete a rule, transactionally. Holding `save_lock` for
    the whole operation, we snapshot the *current* live rules (so concurrent commits can't
    lose each other's writes), apply `change(candidate) -> touched_id` to a copy, persist
    that copy, and only then swap it into the live engine and reset that rule's loop-guard
    state. A failed write therefore raises with the engine completely untouched — a rule
    the client is told failed never went live, and no other rule's edge/debounce is
    disturbed. The new rule is invisible to `on_event` until after the bytes are on disk,
    so it cannot actuate during the write window. `change` returning ``None`` means "no
    change" (e.g. deleting an absent id) — decided inside the lock so a concurrent
    double-delete is linearizable, not a KeyError; we then write nothing and return
    ``None``. Returns the touched id, or ``None`` for a no-op."""
    store = rt.engine.store
    async with rt.save_lock:
        candidate = dict(store.rules)
        touched = change(candidate)
        if touched is None:              # nothing to do (decided under the lock)
            return None
        payload = store.serialize_rules(candidate)
        # Detach-safe write (see `_write_and_settle`): a cancel can't leave the executor
        # thread racing a later os.replace, and a landed-but-cancelled write is reported
        # back so we still swap live state below — keeping disk and memory in agreement so
        # a *later* commit (which snapshots `store.rules`) can't clobber the durable change.
        cancelled = await _write_and_settle(store.write_payload, payload)
        store.rules = candidate          # swap live only after the durable write (even on cancel)
        rt.engine.reset_runtime(touched)
        store.dirty = False              # live now equals what we just wrote
        if cancelled is not None:        # write landed + live state synced → now propagate the cancel
            raise cancelled
        return touched


async def _autosave(rt, interval: float = AUTOSAVE_SECS) -> None:
    """Periodic flush — saves the registry and the automations store *independently*
    (a failed store flush that restores its `dirty` is retried next tick even when the
    registry is clean, and vice versa), off the event loop, surviving a transient I/O
    error so the loop keeps running."""
    while True:
        await asyncio.sleep(interval)
        for persist, obj in ((_persist, rt.registry), (_persist_store, rt.engine.store),
                             (_persist_state, rt.state)):
            if not obj.dirty:
                continue
            try:
                await persist(rt)
            except OSError:
                log.exception("autosave failed — will retry in %ss", interval)


async def _inject(rt, frames, source: str = "scheduler") -> None:
    """Inject automation frames to the current device session: no device connected → drop + warn (not
    deferred); a per-frame OSError is logged and aborts the rest (the session is broken). Shared by the
    scheduler and the niania poller — ``source`` tags the log lines so an incident is traced to the
    right producer (the scheduler tests pin the default ``"scheduler"`` wording)."""
    if not frames:
        return
    session = rt.sessions[-1] if rt.sessions else None
    if session is None:
        log.warning("%s: dropping %d automation frame(s) — no device connected", source, len(frames))
        return
    for i, raw in enumerate(frames):
        try:
            await session.inject_to_device(raw)
        except OSError as exc:
            log.warning("%s inject failed (%d of %d frame(s) not sent): %r",
                        source, len(frames) - i, len(frames), exc)
            break


async def _scheduler(rt, now=datetime.datetime.now, interval: float = SCHEDULER_SECS) -> None:
    """Wall-clock scheduler for time/sun/presence automations. Wakes every `interval` seconds
    (< 60 so each minute is observed at least once; the engine's minute-slot dedup makes the
    extra ticks idempotent), asks the engine which time/sun rules are due — and, when any
    `presence` rule exists, reads the DHCP lease file and asks for presence (arrive/leave) edges —
    then injects their actions to the current device session. `now` is injectable so the loop is
    unit-testable. Survives an inject failure: it logs and waits for the next tick."""
    while True:
        await asyncio.sleep(interval)
        moment = now()
        frames = rt.engine.on_time(rt, moment)
        if rt.engine.has_presence_rules():          # skip the lease read entirely when unused
            frames += rt.engine.on_presence(rt, read_present_macs(LEASES_PATH, moment.timestamp()))
        await _inject(rt, frames)


async def _poll_global_field(rt, field, read, interval, source) -> None:
    """Generic poller for a GLOBAL automation field. Every ``interval`` seconds, call the BLOCKING
    ``read()`` OFF the event loop (a worker thread, so the loop stays free); a non-``None`` return sets
    ``State.<field>``, fires ``on_global``, and injects the resulting frames (log-tagged ``source``). A
    ``None`` read keeps the last value (retry next tick). The whole tick is wrapped so a daemon loop
    never dies on an error — ``CancelledError`` is a ``BaseException``, so cancellation still propagates
    and the task ends cleanly. Caller gates configuration (don't start the task when the feature is off)."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval)
        try:
            value = await loop.run_in_executor(None, read)
            if value is None:                        # failed read -> keep the last value
                continue
            log.debug("%s: %s = %r", source, field, value)
            if getattr(rt.state, field) != value:                                # only a REAL change needs persisting —
                rt.state.dirty = True                                            # else a steady poll re-flushes (+ re-runs
            setattr(rt.state, field, value)                                      # Alembic) the device-state cache every tick
            rt.event_bus.publish({"type": "globals", "fields": {field: value}})   # live dashboard update (every tick)
            await _inject(rt, rt.engine.on_global(rt, field, value), source=source)
        except Exception:                            # a daemon loop must never die on a tick error
            log.exception("%s poller tick failed", source)


def _niania_read(dev):
    """Build a BLOCKING ``read()`` returning the crib temperature (°C, finite) or ``None``. The camera
    allows one connection at a time and returns partial/varying dps, so retry a few times within the
    call; accept only a real FINITE number (reject bool, and NaN/Infinity — json.loads accepts those —
    so a garbage reading can never poison crib_temp / mis-fire a rule)."""
    def read():
        for _ in range(3):                           # flaky camera: retry within the tick
            try:
                dps = dev.status()
            except TuyaError as exc:
                log.debug("niania poll attempt failed: %s", exc)
                continue
            raw = dps.get(str(NIANIA_TEMP_DP))
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
                return raw / NIANIA_SCALE
            log.debug("niania poll: DP %s missing/non-finite in %r", NIANIA_TEMP_DP, dps)
        return None
    return read


async def _niania_poller(rt, make_device=_niania_device, interval: float = NIANIA_SECS) -> None:
    """Poll the Tuya baby monitor for the crib temperature into ``State.crib_temp`` (the `crib_temp`
    global field). OFF when no device is configured. See ``_poll_global_field`` / ``_niania_read``."""
    dev = make_device()
    if dev is None:                                  # not configured -> nothing to poll
        return
    await _poll_global_field(rt, "crib_temp", _niania_read(dev), interval, "niania")


async def _weather_poller(rt, fetch=weather.fetch_outdoor_temp, lat=HESTIA_LAT, lon=HESTIA_LON,
                          interval: float = OUTDOOR_SECS, enabled: bool = OUTDOOR_TEMP_ENABLED,
                          source: str = OUTDOOR_TEMP_SOURCE) -> None:
    """Poll Open-Meteo for the outdoor temperature into ``State.outdoor_temp`` (the `outdoor_temp` global
    field). OPT-IN: returns immediately (zero network egress) unless explicitly ``enabled``, the source
    selector is ``"open-meteo"`` (mutually exclusive with the local 433 feeder), AND a location is
    configured (``HESTIA_LAT``/``HESTIA_LON``). One fetch per tick (a slow, reliable signal); a failed
    fetch keeps the last value. See ``_poll_global_field`` / ``_sensor433_poller``."""
    if not (enabled and source == "open-meteo" and lat is not None and lon is not None):
        return                                       # off / not selected / no location -> no poll, zero egress
    await _poll_global_field(rt, "outdoor_temp", lambda: fetch(lat, lon), interval, "weather")


async def _sensor433_poller(rt, stream=sensor433.stream_readings, enabled: bool = OUTDOOR_TEMP_ENABLED,
                            source: str = OUTDOOR_TEMP_SOURCE, device: str = RTL433_DEVICE,
                            model=RTL433_MODEL, sensor_id=RTL433_ID, protocol=RTL433_PROTOCOL,
                            backoff: float = RTL433_RESTART_SECS) -> None:
    """Stream a local 433 MHz weather sensor (rtl_433) into ``State.outdoor_temp`` (the `outdoor_temp`
    global field) + ``State.outdoor_humidity`` (display-only). OPT-IN: returns immediately (zero work,
    no rtl_433 spawned) unless explicitly ``enabled`` AND ``source == "local"`` — mutually exclusive with
    the Open-Meteo poller. A PUSH source: one long-lived rtl_433 is consumed and each reading applied the
    instant it arrives (no interval). If rtl_433 exits (rtl_tcp restart / SDR hiccup) the stream is
    relaunched after ``backoff`` seconds. One bad reading never drops the stream; the daemon loop never
    dies on an error (``CancelledError`` is a ``BaseException``, so shutdown still cancels cleanly and
    ``stream`` reaps the child). See ``sensor433.stream_readings``."""
    if not (enabled and source == "local"):
        return                                       # off / not selected -> no rtl_433

    async def on_reading(reading) -> None:
        try:
            if (rt.state.outdoor_temp != reading.temperature_C        # only a REAL change needs persisting
                    or rt.state.outdoor_humidity != reading.humidity):
                rt.state.dirty = True
            rt.state.outdoor_temp = reading.temperature_C
            rt.state.outdoor_humidity = reading.humidity
            log.debug("sensor433: outdoor_temp=%r humidity=%r", reading.temperature_C, reading.humidity)
            rt.event_bus.publish({"type": "globals", "fields": {                  # live dashboard delta
                "outdoor_temp": reading.temperature_C, "outdoor_humidity": reading.humidity}})
            await _inject(rt, rt.engine.on_global(rt, "outdoor_temp", reading.temperature_C),
                          source="sensor433")
        except Exception:                            # one bad reading/rule-eval must NOT drop the SDR stream
            log.exception("sensor433 reading failed")

    def on_packet(obj) -> None:
        rt.rf433.record(obj, time.time())            # 433 discovery: capture EVERY decoded packet (never raises)

    while True:
        try:
            await stream(on_reading, device=device, model=model, sensor_id=sensor_id,
                         protocol=protocol, on_packet=on_packet)
        except asyncio.CancelledError:
            raise                                    # shutdown -> stream already reaped rtl_433; propagate
        except Exception:                            # spawn/stream blew up -> log, then back off and relaunch
            log.exception("sensor433 stream failed")
        await asyncio.sleep(backoff)                 # rtl_433 exited (rtl_tcp down / hiccup) -> back off + relaunch


def _thermostat_nodes(rt) -> "list[int]":
    """The node ids currently classified as thermostats (classifier ∪ user registry). Sorted for a
    stable poll order."""
    return sorted(int(node) for node, entry in _merged_discovery(rt).items()
                  if entry.get("type") == "thermostat")


async def _thermostat_poller(rt, interval: float = THERMOSTAT_POLL_SECS) -> None:
    """Keep each thermostat's on/off Mode (and room temperature) live by GET-polling it — these Keemple
    TRVs only REPORT their Mode (``40 03``) / temperature (``31 05``) when asked (``40 02`` / ``31 04``).
    The Keemple cloud does this continuously, so in PROXY the relay tap already sees fresh values; in
    STANDALONE there is no cloud, so ``thermostat_on`` would freeze without this. Hence: STANDALONE-only,
    and disabled when ``interval <= 0``. One bad tick never kills the loop. The injected GETs' replies
    flow through the normal decode → State + a live SSE delta, exactly like a cloud-driven report."""
    if interval <= 0 or rt.mode != "standalone":         # proxy: the cloud polls; <=0: operator-disabled
        return
    while True:
        await asyncio.sleep(interval)
        try:
            frames = []
            for node in _thermostat_nodes(rt):
                frames.append(commands.get_thermostat_mode(rt.next_seq(), node))
                frames.append(commands.get_temperature(rt.next_seq(), node))
            await _inject(rt, frames, source="thermostat-poll")
        except Exception:                                # a classify/inject hiccup must not drop the loop
            log.exception("thermostat poll tick failed")


def _record_klima_state(rt, ir_file: str, button: str) -> None:
    """On a SUCCESSFUL klima IR transmit, update the optimistic klima state + publish a live ``klima``
    delta. No-op for a non-klima file or a button that implies no power state (a set-mode adjust / fan
    preset / unknown name — ``parse_klima_command`` returns None). ``off`` retains the last mode/temp for
    display context. An unchanged re-send publishes nothing — avoids autosave/SSE churn on a repeat press."""
    klima_file = KLIMA.get("file")
    if not klima_file or ir_file != klima_file:
        return
    parsed = parse_klima_command(button)
    if parsed is None:
        return
    prev = rt.state.klima or {}
    new_state = {"power": parsed["power"],
                 "mode": parsed.get("mode", prev.get("mode")),     # `off` keeps the last mode/temp
                 "temp": parsed.get("temp", prev.get("temp"))}
    if new_state == rt.state.klima:
        return
    rt.state.klima = new_state
    rt.state.dirty = True                                          # cached in the device-state snapshot
    rt.event_bus.publish({"type": "klima", "klima": new_state})


async def _ir_worker(rt) -> None:
    """The SOLE owner of the Flipper serial port — serialises every IR transmit so two never overlap.
    Drains ``rt.ir_queue`` of ``(ir_file, button, future)`` and transmits each via the BLOCKING
    ``flipper.transmit_ir`` run OFF the loop. A ``future`` (a control-op request) is resolved with the
    result or its ``FlipperError``; a ``None`` future (a fire-and-forget rule action) just logs on error.
    Disabled (no queue) → returns at once, like the other opt-in tasks. A transmit error never kills the
    loop (``CancelledError`` is a ``BaseException`` so cancellation still propagates and the task ends
    cleanly). Because ONE worker owns the port, a cancelled control-op request can never leave a second
    transmit running concurrently — the worker simply moves on once the current transmit returns."""
    queue = rt.ir_queue
    if queue is None:                                 # Flipper IR disabled -> nothing to drain
        return
    loop = asyncio.get_running_loop()
    while True:
        ir_file, button, future = await queue.get()
        try:
            await loop.run_in_executor(
                None, lambda i=ir_file, b=button: flipper.transmit_ir(i, b, device=FLIPPER_DEV))
        except Exception as exc:
            if future is not None and not future.done():
                future.set_exception(exc)
            else:
                log.warning("ir transmit failed for %s/%s: %r", ir_file, button, exc)
        else:
            # The transmit landed — record the optimistic klima state even if the control-op request
            # was meanwhile cancelled (the IR still went out). No-op for non-klima signals.
            _record_klima_state(rt, ir_file, button)
            if future is not None and not future.done():
                future.set_result(None)


def _control_state(rt, _op):
    return {"ok": True, "state": state_snapshot(rt.state)}


def _control_discovery(rt, _op):
    return {"ok": True, "devices": _merged_discovery(rt)}


def _control_scenes(rt, _op):
    return {"ok": True,                          # copy each sub-dict — detach from registry state
            "scenes": {n: dict(e["scenes"]) for n, e in rt.registry.nodes.items() if e.get("scenes")}}


def _control_automations(rt, _op):
    return {"ok": True, "automations": rt.engine.store.snapshot()}


async def _control_automation_set(rt, op):
    rule = Rule.from_dict(op.get("rule"))        # ValueError -> execute_control_line's catch

    def _add(candidate):
        candidate[rule.id] = rule
        return rule.id

    try:
        await _commit_automation(rt, _add)       # persist-before-live; engine untouched on failure
    except OSError as exc:
        return {"ok": False, "error": f"automations save failed: {exc!r}"}
    return {"ok": True, "id": rule.id}


async def _control_automation_delete(rt, op):
    rid = op.get("id")
    if not isinstance(rid, str):                  # match Rule.from_dict's strictness
        raise ValueError(f"automation_delete: id must be a string, got {rid!r}")

    def _remove(candidate):                       # absence decided INSIDE the lock
        if rid not in candidate:                  # → concurrent double-delete is linearizable
            return None                           #   (no write), never a KeyError
        del candidate[rid]
        return rid

    try:
        touched = await _commit_automation(rt, _remove)
    except OSError as exc:
        return {"ok": False, "error": f"automations save failed: {exc!r}"}
    return {"ok": True, "deleted": touched is not None}


async def _control_name(rt, op):
    if "node" not in op:
        raise ValueError("'name' op requires 'node'")
    dtype = op.get("type")
    if dtype is not None and dtype not in {t.value for t in DeviceType}:
        raise ValueError(f"invalid type {dtype!r}")
    ep = op.get("ep")
    if ep is not None and (not isinstance(ep, int) or isinstance(ep, bool) or ep < 0):
        raise ValueError(f"invalid endpoint {ep!r}")
    rt.registry.set_user(op["node"], name=op.get("name"), room=op.get("room"),
                         dtype=dtype, ep=ep)
    try:
        await _persist(rt)                       # share the autosave lock — no
    except OSError as exc:                       # racing os.replace with autosave
        return {"ok": False, "error": f"registry save failed: {exc!r}"}
    rt.event_bus.publish({"type": "discovery_changed"})  # UI re-fetches new state
    return {"ok": True}


async def _control_ir(rt, op):
    ir_file, button = op.get("file"), op.get("button")
    if not (isinstance(ir_file, str) and ir_file and isinstance(button, str) and button):
        return {"ok": False, "error": "ir requires non-empty 'file' and 'button'"}
    if rt.ir_queue is None:
        return {"ok": False, "error": "flipper IR is disabled"}
    future = asyncio.get_running_loop().create_future()
    try:
        rt.ir_queue.put_nowait((ir_file, button, future))    # the worker owns the serial port
    except asyncio.QueueFull:
        return {"ok": False, "error": "ir queue full"}
    try:
        await asyncio.wait_for(future, timeout=IR_OP_TIMEOUT)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "ir transmit timed out"}
    except flipper.FlipperError as exc:
        return {"ok": False, "error": f"ir transmit failed: {exc}"}
    return {"ok": True}


async def _control_graduate(rt, _op):
    async with rt.save_lock:                     # serialise the whole flip → concurrent callers see the committed result
        if rt.registry.mode == "standalone":     # already (durably) graduated → idempotent, no re-write
            return {"ok": True, "mode": "standalone", "restart_required": rt.mode != "standalone"}
        try:
            # SYNCHRONOUS small write (a registry is ~KB; graduation is a one-time click) with the
            # in-memory flip on the very next line and NO await between — so a cancelled control op
            # can never leave disk and memory disagreeing: the persist→publish pair is atomic.
            rt.registry.write_payload(rt.registry.payload_for_mode("standalone"))
        except OSError as exc:                   # write failed → in-memory mode untouched, target_mode stays honest
            return {"ok": False, "error": f"graduate persist failed: {exc!r}"}
        rt.registry.mode = "standalone"          # durable → publish (persist-before-publish; no false success)
        return {"ok": True, "mode": "standalone", "restart_required": rt.mode != "standalone"}


_CONTROL_OP_HANDLERS = {
    "state": _control_state,
    "discovery": _control_discovery,
    "scenes": _control_scenes,
    "automations": _control_automations,
    "automation_set": _control_automation_set,
    "automation_delete": _control_automation_delete,
    "name": _control_name,
    "ir": _control_ir,
    "graduate": _control_graduate,
}


def _publish_command_state(rt, op) -> None:
    """Reflect a just-injected control command into State + the live feed. A
    remote SET is only ACKed (``[1e 08]``), never reported (``[1e 09]``), so we
    echo the commanded value ourselves — otherwise the dashboard never tracks a
    control press (see ``State.apply_command``). Same ``state`` event the genuine
    report path publishes, so the client patch is identical."""
    changed = rt.state.apply_command(op)
    if changed:
        rt.event_bus.publish({"type": "state", "node": _int(op["node"]), "fields": changed})


async def _control_device_command(rt, op):
    session = rt.sessions[-1] if rt.sessions else None
    if session is None:
        return {"ok": False, "error": "no device connected"}
    raw = build_command(rt, op)
    try:
        await session.inject_to_device(raw)          # echoes the commanded switch/2-gang state post-send
    except OSError as exc:
        return {"ok": False, "error": f"device write failed: {exc!r}"}
    return {"ok": True, "sent": raw.hex()}


async def process_control_op(rt, op) -> dict:
    """Execute one decoded control op; return a JSON-able response dict."""
    if not isinstance(op, dict):
        raise ValueError("expected a JSON object")
    handler = _CONTROL_OP_HANDLERS.get(op.get("op"), _control_device_command)
    if asyncio.iscoroutinefunction(handler):     # async handlers (mutations / device IO) vs sync read-only snapshots
        return await handler(rt, op)
    return handler(rt, op)


async def execute_control_line(rt, line: bytes) -> "dict | None":
    """Decode + execute one control line; return a response dict, or None for blank."""
    line = line.strip()
    if not line:
        return None
    try:
        op = json.loads(line)
        return await process_control_op(rt, op)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return {"ok": False, "error": str(exc)}


async def handle_control(rt, reader, writer) -> None:
    peer = writer.get_extra_info("peername")
    log.info("control + %s", peer)
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            resp = await execute_control_line(rt, line)
            if resp is None:
                continue
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        await _close(writer)
        log.info("control - %s", peer)


def _proxy_session(rt, reader, writer, config):
    return ProxySession(rt, reader, writer, config.cloud_host, config.cloud_port)


async def _start(rt, config, session_factory=_proxy_session):
    """Create and start the device + control servers; return them (serve or test).

    ``session_factory(rt, reader, writer, config)`` builds the per-connection session
    — a `ProxySession` (proxy mode) or a standalone session — both exposing ``.run()``
    and ``.inject_to_device()``. This is the single seam between the two modes.
    """
    _require_safe_control_bind(config.control_host)

    async def on_device(reader, writer):
        await session_factory(rt, reader, writer, config).run()

    async def on_control(reader, writer):
        await handle_control(rt, reader, writer)

    proxy_srv = await asyncio.start_server(on_device, config.listen_host, config.listen_port)
    control_srv = await asyncio.start_server(on_control, config.control_host, config.control_port)
    return proxy_srv, control_srv


def _resolve_cloud(config) -> str:
    """Return the IP to dial: if ``HESTIA_CLOUD_HOSTNAME`` is set, DoH-resolve it
    (seed = the configured ``cloud_host`` = ``HESTIA_CLOUD_HOST``); otherwise the host
    verbatim (a literal pin). Pure (no mutation) and synchronous (blocking DoH/probe) —
    the caller runs it in an executor with a timeout ceiling and assigns the result, so
    an orphaned thread after a timeout can't race-mutate the config."""
    if CLOUD_HOSTNAME:
        return resolve.resolve_cloud_ip(
            CLOUD_HOSTNAME, seed=config.cloud_host, port=config.cloud_port)
    return config.cloud_host


def _shadow_sync_db(rt) -> None:
    """Phase-2 (#57): best-effort mirror of the JSON stores into the SQLite shadow DB at boot.
    Opt out with ``HESTIA_DB_SHADOW=0``. JSON stays authoritative; ``shadow_import`` swallows +
    logs any failure, so a DB problem can never stop the house from booting on JSON."""
    if os.environ.get("HESTIA_DB_SHADOW", "1") == "0":
        return
    if os.environ.get("HESTIA_PERSIST", "json").lower() == "sqlite":
        return                                  # DB is already the authoritative store — no shadow needed
    try:
        from .auth import load_users
        from .store_sql import shadow_import
        shadow_import(rt.registry, rt.engine.store, load_users())
    except Exception:   # the import / load_users (outside shadow_import's own guard) must never break boot
        log.exception("SQLite shadow setup failed — continuing on JSON")


async def main() -> None:  # pragma: no cover
    from .web import start_web, stop_web
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = ProxyConfig()
    # Resolve the upstream once at startup (off-loop), with a hard ceiling so a slow or
    # hostile DoH/probe can never stall startup — fall back to the seed on timeout.
    try:
        config.cloud_host = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _resolve_cloud, config),
            timeout=15.0)
    except asyncio.TimeoutError:
        log.error("cloud resolution timed out; using seed %s", config.cloud_host)
    from .auth import users_path
    from .store_sql import open_audit_engine, open_stores
    registry, store = open_stores(registry_path=config.registry_path,      # HESTIA_PERSIST=sqlite → DB authoritative
                                  automations_path=config.automations_path,
                                  users_path=str(users_path()))
    rt = ProxyRuntime(registry=registry, engine=AutomationEngine(store), mode="proxy")
    seed_device_state(rt)
    _shadow_sync_db(rt)                                # Phase-2 #57: mirror JSON -> SQLite (no-op in sqlite mode)
    rt.audit_engine = open_audit_engine()              # Phase-5 #56: who-did-what audit log
    if FLIPPER_ENABLED:                                # create the IR backlog before anything can fire
        rt.ir_queue = asyncio.Queue(maxsize=IR_QUEUE_MAX)
    proxy_srv, control_srv = await _start(rt, config)
    autosave = asyncio.create_task(_autosave(rt))
    scheduler = asyncio.create_task(_scheduler(rt))
    niania = asyncio.create_task(_niania_poller(rt))   # no-op unless HESTIA_NIANIA_* configured
    weather_task = asyncio.create_task(_weather_poller(rt))  # no-op unless HESTIA_OUTDOOR_TEMP + source=open-meteo
    sensor433_task = asyncio.create_task(_sensor433_poller(rt))  # no-op unless HESTIA_OUTDOOR_TEMP + source=local
    ir_worker = asyncio.create_task(_ir_worker(rt))    # no-op unless HESTIA_FLIPPER enabled
    thermostat_task = asyncio.create_task(_thermostat_poller(rt))  # no-op in proxy (the cloud polls)
    loop = asyncio.get_running_loop()
    _install_term_handler(loop, asyncio.current_task())   # SIGTERM -> graceful persist (docker/systemd stop)
    log.info("hestia proxy: device :%d -> cloud %s:%d | control %s:%d | web %s:%d | registry %s",
             config.listen_port, config.cloud_host, config.cloud_port,
             config.control_host, config.control_port,
             config.web_host, config.web_port, config.registry_path)
    try:
        web_handle = await start_web(rt, config.web_host, config.web_port)
        try:
            async with proxy_srv, control_srv:
                await asyncio.gather(proxy_srv.serve_forever(), control_srv.serve_forever())
        finally:
            rt.event_bus.close()
            await stop_web(web_handle)
    finally:
        autosave.cancel()
        scheduler.cancel()
        niania.cancel()
        weather_task.cancel()
        sensor433_task.cancel()
        ir_worker.cancel()
        thermostat_task.cancel()
        await asyncio.gather(autosave, scheduler, niania, weather_task, sensor433_task, ir_worker,
                             thermostat_task, return_exceptions=True)
        try:
            await _persist(rt)                 # share save_lock — any in-flight
        except OSError:                        # control-op write finishes first
            log.exception("final save failed at shutdown")
        try:
            await _persist_store(rt)           # symmetric: don't lose last-interval rule edits
        except OSError:
            log.exception("final automations save failed at shutdown")
        try:
            await _persist_state(rt)           # best-effort last telemetry cache
        except Exception:
            log.exception("final device-state cache save failed at shutdown")


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):   # SIGINT / SIGTERM-driven graceful exit
        pass
