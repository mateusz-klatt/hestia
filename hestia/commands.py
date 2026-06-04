"""Builders for Keemple device control commands (the cloud→device frames).

Command frames per ``docs/PROTOCOL.md`` §5 (control-frame formats).

Lights / dimmers — command frame ``[1e cmd=0x32]``:
    TLV 0x001f = 4-byte command sequence
    TLV 0x005a = concatenated 7-byte elements, one per channel:
        <idx:1> 01 03 <channel:1> 26 01 <level:1>
    level 0x00 = off, 0x63 (=99) = on/100% (dimmer, 0x00..0x63).
The device confirms with ``[1e cmd=0x33]`` (TLV 0x005b = per-element result)
and reports the resulting state via ``[1e cmd=0x09]`` (node=channel,
TLV 0x0046 = ``2603 <level> 00 fe``).
"""
from __future__ import annotations

from .protocol import build_frame, tlv

LIGHT_OFF = 0x00
LIGHT_ON = 0x63  # 99 = 100%


def _light_element(idx: int, channel: int, level: int) -> bytes:
    return bytes([idx, 0x01, 0x03, channel, 0x26, 0x01, level])


def set_lights(seq: int, channel_levels: "list[tuple[int, int]]") -> bytes:
    """Build a light-set command frame.

    ``channel_levels`` is an ordered list of ``(channel, level)`` pairs;
    ``level`` is 0x00..0x63 (off..100%). Returns a complete 0x7e frame.
    """
    elements = b"".join(
        _light_element(i + 1, ch, level) for i, (ch, level) in enumerate(channel_levels)
    )
    payload = tlv(0x001F, seq.to_bytes(4, "big")) + tlv(0x005A, elements)
    return build_frame(0x1E, 0x32, payload)


def set_light(seq: int, channel: int, level: int) -> bytes:
    """Convenience: set a single light channel (batch [1e c32] form)."""
    return set_lights(seq, [(channel, level)])


def scene_batch(seq: int, elements: bytes) -> bytes:
    """Rebuild a batch ``[1e 32]`` from previously-learned ``0x005a`` element bytes,
    with a fresh FLAG-safe ``seq``. Used by standalone scene-replay: the cloud's
    learned reaction to a function-button press is replayed verbatim (we never need to
    parse the per-element encoding, which may address 2-gang endpoints via ``60 0d``)."""
    payload = tlv(0x001F, seq.to_bytes(4, "big")) + tlv(0x005A, elements)
    return build_frame(0x1E, 0x32, payload)


# --- Single-node commands: [1e cmd=0x07] (set one attribute on one node) ------
# TLV order per ``docs/PROTOCOL.md`` §5: 0x0046 (attr), 0x0048, 0x0047 (node),
# 0x001f (4-byte seq). The "set level/position" attribute `26 01 <0..0x63>` is
# shared by dimmers (brightness) and roller blinds (position).

def _node_command(seq: int, node: int, attr: bytes) -> bytes:
    payload = (
        tlv(0x0046, attr)
        + tlv(0x0048, b"\x00")
        + tlv(0x0047, bytes([node]))
        + tlv(0x001F, seq.to_bytes(4, "big"))
    )
    return build_frame(0x1E, 0x07, payload)


def set_level(seq: int, node: int, level: int) -> bytes:
    """Set a dimmer level on a single node. level 0x00..0x63 (0..99%)."""
    return _node_command(seq, node, bytes([0x26, 0x01, level]))


def set_cover(seq: int, node: int, percent: int) -> bytes:
    """Set a roller-blind position, 0..99 (%). Same primitive as a dimmer level."""
    return _node_command(seq, node, bytes([0x26, 0x01, percent]))


def set_switch(seq: int, node: int, on: bool) -> bytes:
    """Toggle a non-dimmable on/off node (relay / switched light).

    0x0046 = ``25 01 <ff=on / 00=off>`` — byte-for-byte the cloud's motion→light
    command to an on/off light (node 0x0e); distinct from the ``26 01`` level
    primitive used by dimmers and blinds. Also drives the smart plugs
    (nodes 0x13/0x14/0x15); their power metering (``32 02``) is report-only.
    """
    return _node_command(seq, node, bytes([0x25, 0x01, 0xFF if on else 0x00]))


def set_endpoint_switch(seq: int, node: int, endpoint: int, on: bool) -> bytes:
    """Toggle one channel of a 2-gang switch.

    Per docs/PROTOCOL.md §5.1, endpoint-addressed SET uses
    ``60 0d 00 <ep> 25 01 <ff/00>``. The SET form carries ``00 <ep>``,
    while reports decoded by ``State._apply_gang`` carry ``<ep> 00``.
    """
    return _node_command(
        seq,
        node,
        bytes([0x60, 0x0D, 0x00, endpoint, 0x25, 0x01, 0xFF if on else 0x00]),
    )


def set_thermostat(seq: int, node: int, celsius: float) -> bytes:
    """Set a thermostat setpoint in °C (encoded as round(°C*10), 2 bytes BE)."""
    temp10 = round(celsius * 10)
    return _node_command(seq, node, bytes([0x43, 0x01, 0x01, 0x22]) + temp10.to_bytes(2, "big"))


def set_thermostat_power(seq: int, node: int, on: bool) -> bytes:
    """Turn a thermostat on/off. 0x0046 = 40 01 <01=on / 00=off>."""
    return _node_command(seq, node, bytes([0x40, 0x01, 0x01 if on else 0x00]))


def get_thermostat_mode(seq: int, node: int) -> bytes:
    """GET a thermostat's Mode (0x0046 = 40 02). The device replies `40 03 <mode>` (0x00 = off, any
    non-zero = an active mode). These TRVs only report mode when polled — the Keemple cloud does this
    continuously; standalone hestia must, to keep `thermostat_on` live."""
    return _node_command(seq, node, bytes([0x40, 0x02]))


def get_temperature(seq: int, node: int) -> bytes:
    """GET a node's measured temperature (0x0046 = 31 04 01, Multilevel Sensor GET). The device replies
    `31 05 01 04 …` — same poll the Keemple cloud uses to keep a thermostat's room temp fresh."""
    return _node_command(seq, node, bytes([0x31, 0x04, 0x01]))
