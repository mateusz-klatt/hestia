"""Unit tests for the command encoders in hestia.commands.

Where possible, assertions are anchored to the example frames in
``docs/PROTOCOL.md`` §5 so the encoders stay faithful to the spec.
"""
from __future__ import annotations

import unittest

from hestia import commands
from hestia.protocol import Frame


def decode(raw: bytes) -> dict:
    """Decode a complete command frame into {tag: value} plus type/cmd."""
    frame = Frame(raw[1:-1])
    assert frame.checksum_ok, "encoder produced a bad checksum"
    tlvs = {t.tag: t.value for t in frame.tlvs()}
    tlvs["__type__"] = frame.type
    tlvs["__cmd__"] = frame.cmd
    return tlvs


class ConstantTests(unittest.TestCase):
    def test_light_levels(self):
        self.assertEqual(commands.LIGHT_OFF, 0x00)
        self.assertEqual(commands.LIGHT_ON, 0x63)


class BatchLightTests(unittest.TestCase):
    def test_set_lights_batch_format(self):
        # Per docs/PROTOCOL.md §5.1, the batch "close all blinds" (seq 0x000000b3).
        raw = commands.set_lights(0xB3, [(4, 0), (8, 0), (5, 0), (0x0B, 0)])
        d = decode(raw)
        self.assertEqual((d["__type__"], d["__cmd__"]), (0x1E, 0x32))
        self.assertEqual(d[0x001F], bytes.fromhex("000000b3"))
        self.assertEqual(
            d[0x005A],
            bytes.fromhex("0101030426010002010308260100030103052601000401030b260100"),
        )

    def test_set_light_single_channel(self):
        d = decode(commands.set_light(0x10000, 5, 0x63))
        self.assertEqual(d[0x005A], bytes([1, 0x01, 0x03, 5, 0x26, 0x01, 0x63]))

    def test_scene_batch_replays_elements_verbatim(self):
        # A previously-learned scene batch's 0x005a block is replayed unchanged, with a fresh seq.
        elements = bytes.fromhex("0101030426010002010308260100")
        d = decode(commands.scene_batch(0x10000, elements))
        self.assertEqual((d["__type__"], d["__cmd__"]), (0x1E, 0x32))
        self.assertEqual(d[0x005A], elements)                 # bytes preserved exactly
        self.assertEqual(d[0x001F], (0x10000).to_bytes(4, "big"))


class NodeCommandTests(unittest.TestCase):
    def _node(self, raw):
        d = decode(raw)
        self.assertEqual((d["__type__"], d["__cmd__"]), (0x1E, 0x07))
        return d[0x0046], d[0x0047], d[0x001F]

    def test_set_cover_format(self):
        attr, node, seq = self._node(commands.set_cover(0x10000, 0x08, 0))
        self.assertEqual(attr, b"\x26\x01\x00")
        self.assertEqual(node, b"\x08")
        self.assertEqual(seq, (0x10000).to_bytes(4, "big"))

    def test_set_level(self):
        attr, node, _ = self._node(commands.set_level(1, 0x0E, 0x40))
        self.assertEqual(attr, b"\x26\x01\x40")
        self.assertEqual(node, b"\x0e")

    def test_set_switch_on_format(self):
        attr, node, _ = self._node(commands.set_switch(1, 0x0E, True))
        self.assertEqual(attr, b"\x25\x01\xff")
        self.assertEqual(node, b"\x0e")

    def test_set_switch_off(self):
        attr, _, _ = self._node(commands.set_switch(1, 0x0E, False))
        self.assertEqual(attr, b"\x25\x01\x00")

    def test_set_endpoint_switch_exact_frames(self):
        self.assertEqual(
            commands.set_endpoint_switch(1, 0x07, 1, True),
            bytes.fromhex("7e1e07001d00460007600d00012501ff00480001000047000107001f000400000001e07e"),
        )
        self.assertEqual(
            commands.set_endpoint_switch(1, 0x07, 1, False),
            bytes.fromhex("7e1e07001d00460007600d000125010000480001000047000107001f0004000000011f7e"),
        )
        self.assertEqual(
            commands.set_endpoint_switch(1, 0x07, 2, True),
            bytes.fromhex("7e1e07001d00460007600d00022501ff00480001000047000107001f000400000001e37e"),
        )

    def test_set_thermostat_setpoint_encoding(self):
        attr, node, _ = self._node(commands.set_thermostat(1, 0x0D, 21.0))
        self.assertEqual(attr, b"\x43\x01\x01\x22" + (210).to_bytes(2, "big"))
        self.assertEqual(node, b"\x0d")

    def test_set_thermostat_power_on(self):
        attr, _, _ = self._node(commands.set_thermostat_power(1, 0x0D, True))
        self.assertEqual(attr, b"\x40\x01\x01")

    def test_set_thermostat_power_off(self):
        attr, _, _ = self._node(commands.set_thermostat_power(1, 0x0D, False))
        self.assertEqual(attr, b"\x40\x01\x00")

    def test_get_thermostat_mode(self):
        attr, node, _ = self._node(commands.get_thermostat_mode(1, 0x0C))
        self.assertEqual(attr, b"\x40\x02")              # Thermostat Mode GET → device replies 40 03 <mode>
        self.assertEqual(node, b"\x0c")

    def test_get_temperature(self):
        attr, node, _ = self._node(commands.get_temperature(1, 0x0C))
        self.assertEqual(attr, b"\x31\x04\x01")          # Multilevel Sensor GET → device replies 31 05 …
        self.assertEqual(node, b"\x0c")


if __name__ == "__main__":
    unittest.main()
