"""Passive device-type classifier — infers *what* each node is from the `0x0046`
attributes hestia observes (the proxy/standalone tap) plus the gateway roster.

Universal: works on any Keemple install. It does NOT assign room/name — that is
the per-install `Registry` the user fills in; this only guesses the **type** and
power class. Feed it `[1e 15]` roster frames (`ingest_roster`) and `[1e 09]`
events (`observe`); read back `classify(node)` / `report()`.

Blind vs dimmer share the `26` level primitive and are indistinguishable on the
wire (Keemple sells both). With `has_dimmers=False` (the common case) a level
device is taken as a blind; otherwise it stays a lower-confidence guess for the
user to confirm.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .protocol import Frame
from .state import tlv_value


class DeviceType(str, Enum):
    LIGHT = "light"
    BLIND = "blind"
    THERMOSTAT = "thermostat"
    DOOR = "door"
    MOTION = "motion"
    SMOKE = "smoke"
    WATER = "water"
    PLUG = "plug"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    CONFIRMED = "confirmed"   # the user said so (set by the Registry, never here)
    INFERRED = "inferred"     # an unambiguous wire fingerprint
    PROBABLE = "probable"     # one signal, but the type could still be another
    UNKNOWN = "unknown"       # roster-seen only, no type-revealing frame yet


def attribute_signal(data: bytes) -> "str | None":
    """Map one `0x0046` attribute value to a classification signal, or None if it
    is not type-revealing (battery, companion, flag …)."""
    if not data:
        return None
    if data[:1] == b"\x71" and len(data) >= 7 and data[5] == 0xFF:
        # IAS-Zone-ish type byte: 06=contact, 07=motion. 01/04 both seen from the
        # smoke/fire detector (per Z-Wave Notification CC, 01=smoke, 04=heat — both
        # → a fire detector here). 05=water-alarm from the flood
        # sensor (node 0x17), per ``docs/PROTOCOL.md`` §5.4: `71 05 00 00 00 ff 05 …`, with wet/dry mirrored on
        # the `30 03 <ff/00> 06` companion attribute.
        return {0x06: "door", 0x07: "motion", 0x01: "smoke", 0x04: "smoke",
                0x05: "water"}.get(data[6])
    head = data[:2]
    if head == b"\x32\x02":
        return "metering"
    if head in (b"\x43\x01", b"\x43\x03", b"\x40\x01", b"\x40\x02", b"\x40\x03"):
        return "thermostat"
    if head == b"\x31\x05":
        # 31 05 03 0a = a PIR's light sensor. 31 05 01 04 = a temperature report,
        # which any multisensor may send — NOT type-revealing (thermostats are
        # identified by the 43/40 control attributes above), so don't signal here.
        return "illuminance" if data[2:4] == b"\x03\x0a" else None
    if data[:1] == b"\x60":                       # 60 0d <ep> .. — endpoint switch
        return "multigang"
    if head in (b"\x26\x01", b"\x26\x03"):
        return "level"
    if head in (b"\x25\x01", b"\x25\x03"):
        return "onoff"
    return None


@dataclass
class NodeInfo:
    power: "str | None" = None                    # "mains" / "battery" (from roster — unreliable)
    battery: "int | None" = None                  # last 0x80-03 level %, if the node reports one
    signals: set = field(default_factory=set)

    def classify(self, has_dimmers: bool) -> "tuple[DeviceType, Confidence]":
        s = self.signals
        if "metering" in s:
            return DeviceType.PLUG, Confidence.INFERRED
        if "thermostat" in s:
            return DeviceType.THERMOSTAT, Confidence.INFERRED
        if "door" in s:
            return DeviceType.DOOR, Confidence.INFERRED
        if "smoke" in s:
            return DeviceType.SMOKE, Confidence.INFERRED
        if "water" in s:
            return DeviceType.WATER, Confidence.INFERRED
        if "motion" in s:
            return DeviceType.MOTION, Confidence.INFERRED
        if "illuminance" in s:                    # a PIR's light sensor, no motion yet
            return DeviceType.MOTION, Confidence.PROBABLE
        if "multigang" in s or "onoff" in s:
            return DeviceType.LIGHT, Confidence.INFERRED
        if "level" in s:                          # blind or (rarely) a dimmer
            return DeviceType.BLIND, Confidence.PROBABLE if has_dimmers else Confidence.INFERRED
        return DeviceType.UNKNOWN, Confidence.UNKNOWN


class Classifier:
    """Accumulates per-node evidence and maps it to a device type + confidence."""

    def __init__(self, has_dimmers: bool = False):
        self.has_dimmers = has_dimmers
        self.nodes: "dict[int, NodeInfo]" = {}

    def ingest_roster(self, frame: Frame) -> None:
        """Seed nodes from a `[1e 15]` roster (TLV `0x004d` = `<node><flag>` pairs;
        flag `01` = battery sensor, `00` = mains)."""
        if (frame.type, frame.cmd) != (0x1E, 0x15):
            return
        data = tlv_value(frame, 0x004D)
        if not data:
            return
        for i in range(0, len(data) - 1, 2):
            node, flag = data[i], data[i + 1]
            self.nodes.setdefault(node, NodeInfo()).power = "battery" if flag == 0x01 else "mains"

    def observe(self, frame: Frame) -> None:
        """Add a type signal from a `[1e 09]` state event."""
        if (frame.type, frame.cmd) != (0x1E, 0x09):
            return
        node_b = tlv_value(frame, 0x0047)
        data = tlv_value(frame, 0x0046)
        if not node_b or not data:
            return
        signal = attribute_signal(data)
        info = self.nodes.setdefault(node_b[0], NodeInfo())
        if signal:
            info.signals.add(signal)
        if data[:2] == b"\x80\x03" and len(data) >= 3:
            info.battery = data[2]                # Z-Wave Battery CC level (0x80 03 <pct>)

    def classify(self, node: int) -> "tuple[DeviceType, Confidence]":
        info = self.nodes.get(node)
        if info is None:
            return DeviceType.UNKNOWN, Confidence.UNKNOWN
        return info.classify(self.has_dimmers)

    def report(self) -> dict:
        """Snapshot: {node: {power, type, confidence, battery}} for every seen node.
        `power` is the raw roster flag; the true power class is derived downstream
        from `battery` presence (the roster flag mislabels battery FLiRS devices
        such as thermostats as mains)."""
        out = {}
        for node, info in self.nodes.items():
            dtype, conf = info.classify(self.has_dimmers)
            out[node] = {"power": info.power, "type": dtype.value,
                         "confidence": conf.value, "battery": info.battery}
        return out
