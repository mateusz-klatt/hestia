"""Device state model, per ``docs/PROTOCOL.md`` §3 (state events ``[1e 09]``).

Transport-agnostic: feed it `Frame`s from the proxy, the standalone server, or
any frame source. Encodings per `docs/PROTOCOL.md`. This is the "brain" that
the networking layer wraps; automations read this state and emit commands.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .protocol import Frame

_SNAPSHOT_MAPS = (
    "doors",
    "motion",
    "levels",
    "switches",
    "thermostat_setpoint",
    "thermostat_on",
    "temperature",
    "plug_w",
    "plug_kwh",
    "plug_v",
)

# Node-less GLOBAL scalars worth caching across a restart so the dashboard shows
# the last crib/outdoor temps immediately instead of "—" until the next poll
# (the niania poller is ~per-minute, the weather poller ~per-10-min).
_SNAPSHOT_GLOBALS = ("crib_temp", "outdoor_temp", "outdoor_humidity")


def tlv_value(frame: Frame, tag: int) -> "bytes | None":
    for t in frame.tlvs():
        if t.tag == tag:
            return t.value
    return None


def _set_changed(store, node: int, changed: dict, key: str, value) -> None:
    if store.get(node) != value:                # only emit a real change
        store[node] = value
        changed[key] = value


def _apply_door(state, node: int, data: bytes, changed: dict) -> None:
    _set_changed(state.doors, node, changed, "door",
                 {0x16: "open", 0x17: "closed"}.get(data[7], f"?{data[7]:02x}"))


def _apply_motion(state, node: int, data: bytes, changed: dict) -> None:
    # PIR / Home-Security notification `71 05 .. ff 07 <event>`: 0x08 = motion detected, anything else
    # (0x00 idle — the "no motion" clear, plus a trailing cleared-event param) = no motion.
    _set_changed(state.motion, node, changed, "motion", data[7] == 0x08)


def _apply_level(state, node: int, data: bytes, changed: dict) -> None:
    _set_changed(state.levels, node, changed, "level", data[2])


def _apply_switch(state, node: int, data: bytes, changed: dict) -> None:
    _set_changed(state.switches, node, changed, "switch", data[2] == 0xFF)


def _apply_gang(state, node: int, data: bytes, changed: dict) -> None:
    # 2-gang endpoint switch: 60 0d <ep> 00 25 03 <ff=on/00=off>. Track per
    # endpoint; on a change emit the full per-node map so the client can
    # render the roll-up without a nested merge.
    ep, on = data[2], data[6] == 0xFF
    eps = state.gang.setdefault(node, {})
    if eps.get(ep) != on:
        eps[ep] = on
        changed["endpoints"] = dict(eps)


def _apply_thermostat_setpoint(state, node: int, data: bytes, changed: dict) -> None:
    _set_changed(state.thermostat_setpoint, node, changed, "setpoint", data[-1])


def _apply_thermostat_power(state, node: int, data: bytes, changed: dict) -> None:
    # Thermostat Mode report `40 03 <mode>`: 0x00 = Off, ANY non-zero = an active mode
    # (Heat 0x01 / Cool 0x02 / Auto 0x03 / manufacturer). Treat every active mode as "on" so a
    # unit running in something other than plain Heat doesn't read as Off.
    _set_changed(state.thermostat_on, node, changed, "thermostat_on", data[2] != 0x00)


def _apply_temperature(state, node: int, data: bytes, changed: dict) -> None:
    _set_changed(state.temperature, node, changed, "temperature", data[-1])


def _apply_metering(state, node: int, data: bytes, changed: dict) -> None:
    # Frame: 32 02 <sub:2> <A:w> <mid:2> <B:w>. The live reading lands in
    # slot A or B (the other reads 0), so take the larger. Energy carries a
    # meter-flag high bit (plug 0x15) — mask it off (a harmless no-op for the
    # 2-byte power/voltage subs).
    sub, body = data[2:4], data[4:]
    width = {b"\x21\x44": 4, b"\xa1\x4a": 2, b"\xa1\x42": 2}.get(sub)
    if width and len(body) >= width:
        a = int.from_bytes(body[:width], "big") & 0x7FFFFFFF
        b = (int.from_bytes(body[width + 2:2 * width + 2], "big") & 0x7FFFFFFF
             if len(body) >= 2 * width + 2 else 0)
        _set_metering_value(state, node, changed, sub, max(a, b))


def _set_metering_value(state, node: int, changed: dict, sub: bytes, raw: int) -> None:
    if sub == b"\x21\x44":                       # cumulative energy ×0.01 kWh
        _set_changed(state.plug_kwh, node, changed, "energy_kwh", raw / 100)
    elif sub == b"\xa1\x4a":                     # instantaneous power, W
        _set_changed(state.plug_w, node, changed, "power_w", raw)
    else:                                        # a1 42 — mains voltage ×0.01 V
        _set_changed(state.plug_v, node, changed, "voltage_v", raw / 100)


def _apply_scene_activation(_state, _node: int, data: bytes, changed: dict) -> None:
    # `2b 01 <sceneId> <dimDuration>`. A discrete press; the scene id is fixed
    # per device (configured in the app), NOT derived from the gesture. This is
    # an EVENT, not state — emit every time (no value-gating, no dedup).
    changed["scene"] = {"id": data[2], "kind": "scene"}


def _apply_central_scene(state, node: int, data: bytes, changed: dict) -> None:
    # `5b 03 <seq> <keyAttr> <sceneId>`. Accept ANY keyAttr intentionally —
    # every notification is a press and the gesture isn't reliably encoded; do
    # NOT filter on 0x80. `seq` increments per press, but slow-refresh can resend
    # the same seq, so dedup a consecutive identical seq per node. (A device
    # power-cycle that restarts seq to a value equal to the last-seen one drops
    # one press, ~1/256 — accepted; the user just presses again.)
    seq = data[2]
    if state.scene_seq.get(node) != seq:
        state.scene_seq[node] = seq
        changed["scene"] = {"id": data[4], "kind": "central"}


_PREFIX4_APPLIERS = {
    b"\x43\x03\x01\x04": _apply_thermostat_setpoint,
    b"\x31\x05\x01\x04": _apply_temperature,
}

_PREFIX2_APPLIERS = {
    b"\x26\x03": _apply_level,
    b"\x25\x03": _apply_switch,
    b"\x40\x03": _apply_thermostat_power,
    b"\x2b\x01": _apply_scene_activation,
    b"\x5b\x03": _apply_central_scene,
}


def _state_applier(data: bytes):
    if data[:1] == b"\x71" and len(data) >= 8 and data[5:7] == b"\xff\x06":
        return _apply_door
    if data[:1] == b"\x71" and len(data) >= 8 and data[5:7] == b"\xff\x07":   # Home-Security: motion (PIR)
        return _apply_motion
    if data[:2] == b"\x60\x0d" and len(data) >= 7 and data[4:6] == b"\x25\x03":
        return _apply_gang
    if data[:2] == b"\x32\x02" and len(data) >= 4:
        return _apply_metering
    if data[:2] == b"\x2b\x01" and len(data) < 4:
        return None
    if data[:2] == b"\x5b\x03" and len(data) < 5:
        return None
    applier = _PREFIX4_APPLIERS.get(data[:4])
    if applier is not None:
        return applier
    return _PREFIX2_APPLIERS.get(data[:2])


# --- Control-op value coercion -----------------------------------------------
# Shared with the command builder (``proxy.build_command``) so an optimistic echo
# can NEVER disagree with the bytes actually put on the wire — a control op's
# fields are interpreted identically whether we encode them or mirror them.

_TRUE = {"true", "1", "on", "yes"}
_FALSE = {"false", "0", "off", "no"}


def _int(value) -> int:
    """Accept ints or hex/dec strings ('0x0e', '14') from JSON control ops."""
    return int(value, 0) if isinstance(value, str) else int(value)


def _bool(value) -> bool:
    """Strictly parse a control boolean: a real JSON bool, or an explicit
    true/false-like string. Reject anything ambiguous, so a switch or thermostat
    never silently does the opposite of what was asked (``bool("false")`` is True)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    raise ValueError(f"expected a boolean, got {value!r}")


# --- Command echo: learn state from commands, not just reports ---------------
# A Keemple binary switch / 2-gang ACKs a remote SET with a bare ``[1e 08]`` status
# frame and does NOT volunteer a ``[1e 09]`` report for it — so without echoing the
# COMMANDED value, a remotely-set switch never reaches State (or the live UI),
# whatever the source (this UI's control, a hestia automation, or — in proxy mode —
# the Keemple cloud/app). We mirror the report applier and record the SAME discovery
# field a genuine report would; a later real report overwrites it with ground truth.
#
# DELIBERATELY switch/2-gang ONLY. Covers + thermostats DO reliably report their own
# state (level / setpoint / thermostat_on) shortly after acting, so we trust those
# reports rather than optimistically guess an intermediate ("reports win where they
# exist") — an operator decision after observing real device behaviour.

def _command_switch(state, node: int, op: dict, changed: dict) -> None:
    endpoint = op.get("endpoint")
    if endpoint is None:
        _set_changed(state.switches, node, changed, "switch", _bool(op["on"]))
        return
    ep, on = _int(endpoint), _bool(op["on"])
    eps = state.gang.setdefault(node, {})
    if eps.get(ep) != on:                        # emit the full per-node roll-up (matches _apply_gang)
        eps[ep] = on
        changed["endpoints"] = dict(eps)


def _command_thermostat_power(state, node: int, op: dict, changed: dict) -> None:
    # Thermostat ON/OFF is OPTIMISTIC: the device only REPORTS its mode (40 03) when GET-polled, so a
    # press would otherwise not move the badge until the (debounced) confirmation poll. Echo the commanded
    # power immediately; the confirm poll corrects it if the device disagreed.
    _set_changed(state.thermostat_on, node, changed, "thermostat_on", _bool(op["on"]))


def _command_thermostat(state, node: int, op: dict, changed: dict) -> None:
    # Setpoint is ALSO optimistic. The earlier assumption ("the device reports 43 03 reliably") was
    # falsified by a live capture: these TRVs frequently do NOT echo a setpoint report after a SET (only
    # some nodes, sometimes), so a report-only setpoint left State stale at the OLD value — and the UI
    # then re-pinned its dropdown to that stale value on the next refresh ("I set 18 °C, it jumps back to
    # 28"). Echo the commanded °C immediately, stored as an integer to match the device's integer-°C
    # report; a later 43 03 still wins where it arrives.
    celsius = op.get("celsius")
    if isinstance(celsius, (int, float)) and not isinstance(celsius, bool):
        _set_changed(state.thermostat_setpoint, node, changed, "setpoint", round(celsius))


_COMMAND_APPLIERS = {
    "switch": _command_switch,                   # 2-gang rides this too (op carries an `endpoint`)
    "thermostat_power": _command_thermostat_power,
    "thermostat": _command_thermostat,
}


def _load_int_keyed(target: dict, data) -> None:
    if not isinstance(data, dict):
        return
    for node, value in data.items():
        try:
            target[int(node)] = value
        except (TypeError, ValueError):
            continue


def _load_globals(state, data) -> None:
    """Restore the cached global temps, ignoring anything non-numeric (a corrupt
    blob must never crash boot or poison a temp). ``bool`` is rejected explicitly
    (it is an ``int`` subclass, but a temperature is never a boolean)."""
    if not isinstance(data, dict):
        return
    for name in _SNAPSHOT_GLOBALS:
        value = data.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            setattr(state, name, value)            # reject NaN/inf (a corrupt JSON blob round-trips them)


def _load_klima(data) -> "dict | None":
    """Restore the cached optimistic klima (A/C) state, ignoring a malformed blob.

    Shape only — ``power`` must be a bool, ``mode`` a str or None, ``temp`` an int
    (not bool) or None. Semantic validity (a *known* mode/temp) is enforced at set
    time against the live signal map; here we only refuse a corrupt cache section.
    """
    if not isinstance(data, dict):
        return None
    power, mode, temp = data.get("power"), data.get("mode"), data.get("temp")
    if not isinstance(power, bool):
        return None
    if mode is not None and not isinstance(mode, str):
        return None
    if temp is not None and (isinstance(temp, bool) or not isinstance(temp, int)):
        return None
    return {"power": power, "mode": mode, "temp": temp}


def _load_gang(data) -> dict:
    if not isinstance(data, dict):
        return {}
    loaded = {}
    for node, endpoints in data.items():
        if not isinstance(endpoints, dict):
            continue
        try:
            node_id = int(node)
        except (TypeError, ValueError):
            continue
        parsed = {}
        for endpoint, value in endpoints.items():
            try:
                parsed[int(endpoint)] = value
            except (TypeError, ValueError):
                continue
        if parsed:
            loaded[node_id] = parsed
    return loaded


@dataclass
class State:
    doors: dict = field(default_factory=dict)                # node -> "open"/"closed"
    motion: dict = field(default_factory=dict)               # node -> bool (PIR: motion detected / idle)
    levels: dict = field(default_factory=dict)               # node -> 0..99 (dimmer/blind)
    switches: dict = field(default_factory=dict)             # node -> bool (on/off relay)
    thermostat_setpoint: dict = field(default_factory=dict)  # node -> °C
    thermostat_on: dict = field(default_factory=dict)        # node -> bool
    temperature: dict = field(default_factory=dict)          # node -> °C (measured)
    plug_w: dict = field(default_factory=dict)               # node -> instantaneous power, W
    plug_kwh: dict = field(default_factory=dict)             # node -> cumulative energy, kWh
    plug_v: dict = field(default_factory=dict)               # node -> mains voltage, V
    gang: dict = field(default_factory=dict)                 # node -> {endpoint: on} (multi-gang switch)
    scene_seq: dict = field(default_factory=dict)            # node -> last Central-Scene seq (dedup only; not state)
    crib_temp: "float | None" = None                         # GLOBAL (node-less) °C from the Tuya baby-monitor poller
    outdoor_temp: "float | None" = None                      # GLOBAL (node-less) °C from the Open-Meteo / local-433 feeder
    outdoor_humidity: "float | None" = None                  # GLOBAL (node-less) %RH companion from the local-433 feeder (display-only)
    klima: "dict | None" = None                              # GLOBAL (node-less) optimistic A/C state {power,mode,temp} — last one-way IR command (None=never commanded)
    thermostat_last_cmd: dict = field(default_factory=dict)  # node -> wall-clock ts of the last thermostat SET (for the "not responding" badge; runtime-only, not snapshotted)
    dirty: bool = field(default=False)                       # best-effort SQLite telemetry-cache needs flushing

    def apply(self, frame: Frame) -> dict:
        """Apply a state event; return ``{discovery_key: value}`` for every field
        whose value ACTUALLY changed (empty if nothing changed or not a `[1e 09]`
        state event). Keys match the `/api/discovery` field names so the web client
        can merge a delta straight into its cached row."""
        if not (frame.type == 0x1E and frame.cmd == 0x09):
            return {}
        node_b = tlv_value(frame, 0x0047)
        data = tlv_value(frame, 0x0046)
        if not node_b or not data:
            return {}
        node = node_b[0]
        changed: dict = {}
        applier = _state_applier(data)
        if applier is not None:
            applier(self, node, data, changed)
        if changed:
            self.dirty = True
        return changed

    def apply_command(self, op: dict) -> dict:
        """The state delta a SWITCH / 2-gang / thermostat (power + setpoint) control command WILL produce.

        Those devices ACK a remote SET (``[1e 08]``) but never send a ``[1e 09]``
        report for it (and thermostats report mode/setpoint poll-only or unreliably),
        so without this echo a remotely-set value never updates
        State or the live UI. Reflect the *commanded* value (field names match
        ``apply`` / ``/api/discovery``); a later genuine report overwrites it with
        ground truth. Returns the changed ``{field: value}``, or empty for ANY
        non-switch op (cover / level / thermostat — they report their own state —
        ``raw`` / ``lights``), a bad node, or a value already cached."""
        applier = _COMMAND_APPLIERS.get(op.get("op"))
        if applier is None:
            return {}
        try:
            node = _int(op["node"])
        except (KeyError, TypeError, ValueError):
            return {}
        changed: dict = {}
        try:
            applier(self, node, op, changed)
        except (KeyError, TypeError, ValueError):
            return {}
        if changed:
            self.dirty = True
        return changed

    def to_snapshot(self) -> dict:
        """JSON-safe telemetry cache: node-keyed live maps + the global temps.

        Narrower than the API state snapshot — scene-dedup bookkeeping stays
        runtime-only — but the crib/outdoor globals ARE cached so they survive a
        restart (otherwise the dashboard shows "—" until the next poll).
        """
        snap = {name: {str(node): value for node, value in getattr(self, name).items()}
                for name in _SNAPSHOT_MAPS}
        snap["gang"] = {
            str(node): {str(endpoint): value for endpoint, value in endpoints.items()}
            for node, endpoints in self.gang.items()
        }
        snap["globals"] = {name: getattr(self, name) for name in _SNAPSHOT_GLOBALS}
        snap["klima"] = self.klima                # node-less optimistic A/C state (a plain dict or None — JSON-safe)
        return snap

    def load_snapshot(self, snap) -> None:
        """Restore a best-effort telemetry cache, ignoring any malformed part.

        A corrupt AppMeta blob must never crash boot. Only dict-shaped sections
        with int-parseable node/endpoint keys (and numeric globals) are loaded.
        """
        if not isinstance(snap, dict):
            return
        for name in _SNAPSHOT_MAPS:
            _load_int_keyed(getattr(self, name), snap.get(name))
        self.gang.update(_load_gang(snap.get("gang")))
        _load_globals(self, snap.get("globals"))
        self.klima = _load_klima(snap.get("klima"))
