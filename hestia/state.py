"""Device state model, per ``docs/PROTOCOL.md`` §3 (state events ``[1e 09]``).

Transport-agnostic: feed it `Frame`s from the proxy, the standalone server, or
any frame source. Encodings per `docs/PROTOCOL.md`. This is the "brain" that
the networking layer wraps; automations read this state and emit commands.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import Frame


def tlv_value(frame: Frame, tag: int) -> "bytes | None":
    for t in frame.tlvs():
        if t.tag == tag:
            return t.value
    return None


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
    outdoor_temp: "float | None" = None                      # GLOBAL (node-less) °C from the Open-Meteo poller

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

        def _set(store, key, value):
            if store.get(node) != value:                # only emit a real change
                store[node] = value
                changed[key] = value

        if data[:1] == b"\x71" and len(data) >= 8 and data[5:7] == b"\xff\x06":
            _set(self.doors, "door", {0x16: "open", 0x17: "closed"}.get(data[7], f"?{data[7]:02x}"))
        elif data[:2] == b"\x26\x03":  # level report (dimmer / blind position)
            _set(self.levels, "level", data[2])
        elif data[:2] == b"\x25\x03":  # on/off switch report (25 03 ff..=on / 00..=off)
            _set(self.switches, "switch", data[2] == 0xFF)
        elif data[:2] == b"\x60\x0d" and len(data) >= 7 and data[4:6] == b"\x25\x03":
            # 2-gang endpoint switch: 60 0d <ep> 00 25 03 <ff=on/00=off>. Track per
            # endpoint; on a change emit the full per-node map so the client can
            # render the roll-up without a nested merge.
            ep, on = data[2], data[6] == 0xFF
            eps = self.gang.setdefault(node, {})
            if eps.get(ep) != on:
                eps[ep] = on
                changed["endpoints"] = dict(eps)
        elif data[:4] == b"\x43\x03\x01\x04":  # thermostat setpoint
            _set(self.thermostat_setpoint, "setpoint", data[-1])
        elif data[:2] == b"\x40\x03":  # thermostat on/off
            _set(self.thermostat_on, "thermostat_on", data[2] == 0x01)
        elif data[:4] == b"\x31\x05\x01\x04":  # measured temperature
            _set(self.temperature, "temperature", data[-1])
        elif data[:2] == b"\x32\x02" and len(data) >= 4:  # smart-plug metering (§5.6)
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
                raw = max(a, b)
                if sub == b"\x21\x44":                       # cumulative energy ×0.01 kWh
                    _set(self.plug_kwh, "energy_kwh", raw / 100)
                elif sub == b"\xa1\x4a":                     # instantaneous power, W
                    _set(self.plug_w, "power_w", raw)
                else:                                        # a1 42 — mains voltage ×0.01 V
                    _set(self.plug_v, "voltage_v", raw / 100)
        elif data[:2] == b"\x2b\x01" and len(data) >= 4:  # Scene Activation Set — switch function button
            # `2b 01 <sceneId> <dimDuration>`. A discrete press; the scene id is fixed
            # per device (configured in the app), NOT derived from the gesture. This is
            # an EVENT, not state — emit every time (no value-gating, no dedup).
            changed["scene"] = {"id": data[2], "kind": "scene"}
        elif data[:2] == b"\x5b\x03" and len(data) >= 5:  # Central Scene Notification — blind function button
            # `5b 03 <seq> <keyAttr> <sceneId>`. Accept ANY keyAttr intentionally —
            # every notification is a press and the gesture isn't reliably encoded; do
            # NOT filter on 0x80. `seq` increments per press, but slow-refresh can resend
            # the same seq, so dedup a consecutive identical seq per node. (A device
            # power-cycle that restarts seq to a value equal to the last-seen one drops
            # one press, ~1/256 — accepted; the user just presses again.)
            seq = data[2]
            if self.scene_seq.get(node) != seq:
                self.scene_seq[node] = seq
                changed["scene"] = {"id": data[4], "kind": "central"}
        return changed
