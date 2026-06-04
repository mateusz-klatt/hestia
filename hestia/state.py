"""Device state model, per ``docs/PROTOCOL.md`` §3 (state events ``[1e 09]``).

Transport-agnostic: feed it `Frame`s from the proxy, the standalone server, or
any frame source. Encodings per `docs/PROTOCOL.md`. This is the "brain" that
the networking layer wraps; automations read this state and emit commands.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import Frame

_SNAPSHOT_MAPS = (
    "doors",
    "levels",
    "switches",
    "thermostat_setpoint",
    "thermostat_on",
    "temperature",
    "plug_w",
    "plug_kwh",
    "plug_v",
)


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
    _set_changed(state.thermostat_on, node, changed, "thermostat_on", data[2] == 0x01)


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


def _load_int_keyed(target: dict, data) -> None:
    if not isinstance(data, dict):
        return
    for node, value in data.items():
        try:
            target[int(node)] = value
        except (TypeError, ValueError):
            continue


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

    def to_snapshot(self) -> dict:
        """JSON-safe telemetry cache: node-keyed live maps only.

        This is deliberately narrower than the API state snapshot: scene dedup
        bookkeeping and node-less globals are runtime-only and are not persisted.
        """
        snap = {name: {str(node): value for node, value in getattr(self, name).items()}
                for name in _SNAPSHOT_MAPS}
        snap["gang"] = {
            str(node): {str(endpoint): value for endpoint, value in endpoints.items()}
            for node, endpoints in self.gang.items()
        }
        return snap

    def load_snapshot(self, snap) -> None:
        """Restore a best-effort telemetry cache, ignoring any malformed part.

        A corrupt AppMeta blob must never crash boot. Only dict-shaped sections
        with int-parseable node/endpoint keys are loaded.
        """
        if not isinstance(snap, dict):
            return
        for name in _SNAPSHOT_MAPS:
            _load_int_keyed(getattr(self, name), snap.get(name))
        self.gang.update(_load_gang(snap.get("gang")))
