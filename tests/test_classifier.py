"""Unit tests for the device-type classifier (hestia.classifier)."""
from __future__ import annotations

import unittest

from hestia.classifier import (
    Classifier,
    Confidence,
    DeviceType,
    NodeInfo,
    attribute_signal,
)
from hestia.protocol import Frame, build_frame, tlv


def event(node: int, data: bytes) -> Frame:
    return Frame(build_frame(0x1E, 0x09, tlv(0x0047, bytes([node])) + tlv(0x0046, data))[1:-1])


def roster(pairs) -> Frame:
    value = b"".join(bytes([node, flag]) for node, flag in pairs)
    return Frame(build_frame(0x1E, 0x15, tlv(0x0001, b"\x00\x00") + tlv(0x004D, value))[1:-1])


class AttributeSignalTests(unittest.TestCase):
    def test_known_signals(self):
        self.assertIsNone(attribute_signal(b""))
        self.assertEqual(attribute_signal(bytes.fromhex("7105000000ff061600")), "door")
        self.assertEqual(attribute_signal(bytes.fromhex("7105000000ff070800")), "motion")
        self.assertEqual(attribute_signal(bytes.fromhex("7105000000ff010300")), "smoke")
        self.assertEqual(attribute_signal(bytes.fromhex("7105000000ff040700")), "smoke")
        self.assertEqual(attribute_signal(bytes.fromhex("7105000000ff05000102")), "water")  # flood: alarm event
        self.assertEqual(attribute_signal(bytes.fromhex("7105000000ff050200")), "water")     # flood: other event byte
        self.assertEqual(attribute_signal(b"\x32\x02\x21\x44"), "metering")
        self.assertEqual(attribute_signal(b"\x43\x03\x01\x04"), "thermostat")
        self.assertEqual(attribute_signal(b"\x40\x03\x00"), "thermostat")
        self.assertIsNone(attribute_signal(b"\x31\x05\x01\x04\x00\x00\x00\x16"))   # temperature → not type-revealing
        self.assertEqual(attribute_signal(b"\x31\x05\x03\x0a\x00\x7b"), "illuminance")
        self.assertEqual(attribute_signal(b"\x60\x0d\x01\x00\x25\x03\xff"), "multigang")
        self.assertEqual(attribute_signal(b"\x26\x03\x30\x00\xfe"), "level")
        self.assertEqual(attribute_signal(b"\x25\x03\xff\xff\x00"), "onoff")
        self.assertIsNone(attribute_signal(b"\x80\x03\x64"))         # battery → not type-revealing
        self.assertIsNone(attribute_signal(b"\x2b\x01\x02\x00"))     # Scene Activation → button press, not a type
        self.assertIsNone(attribute_signal(b"\x5b\x03\x02\x80\x01")) # Central Scene → button press, not a type

    def test_71_edge_cases(self):
        self.assertIsNone(attribute_signal(b"\x71\x05\x00\x00\x00\x00\x06\x16"))   # byte5 != 0xff
        self.assertIsNone(attribute_signal(b"\x71\x05\x00"))                       # too short
        self.assertIsNone(attribute_signal(bytes.fromhex("7105000000ff990000")))  # unknown type byte


class NodeClassifyTests(unittest.TestCase):
    def c(self, signals, has_dimmers=False):
        return NodeInfo(signals=set(signals)).classify(has_dimmers)

    def test_every_branch(self):
        self.assertEqual(self.c(["metering"]), (DeviceType.PLUG, Confidence.INFERRED))
        self.assertEqual(self.c(["thermostat"]), (DeviceType.THERMOSTAT, Confidence.INFERRED))
        self.assertEqual(self.c(["door"]), (DeviceType.DOOR, Confidence.INFERRED))
        self.assertEqual(self.c(["smoke"]), (DeviceType.SMOKE, Confidence.INFERRED))
        self.assertEqual(self.c(["water"]), (DeviceType.WATER, Confidence.INFERRED))
        self.assertEqual(self.c(["motion"]), (DeviceType.MOTION, Confidence.INFERRED))
        self.assertEqual(self.c(["illuminance"]), (DeviceType.MOTION, Confidence.PROBABLE))
        self.assertEqual(self.c(["multigang"]), (DeviceType.LIGHT, Confidence.INFERRED))
        self.assertEqual(self.c(["onoff"]), (DeviceType.LIGHT, Confidence.INFERRED))
        self.assertEqual(self.c(["level"]), (DeviceType.BLIND, Confidence.INFERRED))
        self.assertEqual(self.c(["level"], has_dimmers=True), (DeviceType.BLIND, Confidence.PROBABLE))
        self.assertEqual(self.c([]), (DeviceType.UNKNOWN, Confidence.UNKNOWN))


class ClassifierTests(unittest.TestCase):
    def test_roster_seeds_power(self):
        c = Classifier()
        c.ingest_roster(roster([(0x10, 1), (0x05, 0)]))
        self.assertEqual(c.nodes[0x10].power, "battery")
        self.assertEqual(c.nodes[0x05].power, "mains")

    def test_roster_ignores_non_c15(self):
        c = Classifier()
        c.ingest_roster(event(0x05, b"\x26\x03\x10\x00\xfe"))
        self.assertEqual(c.nodes, {})

    def test_roster_without_004d(self):
        c = Classifier()
        c.ingest_roster(Frame(build_frame(0x1E, 0x15, tlv(0x0001, b"\x00\x00"))[1:-1]))
        self.assertEqual(c.nodes, {})

    def test_observe_classifies(self):
        c = Classifier()
        c.observe(event(0x05, b"\x26\x03\x10\x00\xfe"))
        self.assertEqual(c.classify(0x05), (DeviceType.BLIND, Confidence.INFERRED))

    def test_observe_ignores_non_c09(self):
        c = Classifier()
        c.observe(Frame(build_frame(0x66, 0x01)[1:-1]))
        self.assertEqual(c.nodes, {})

    def test_observe_missing_data_ignored(self):
        c = Classifier()
        c.observe(Frame(build_frame(0x1E, 0x09, tlv(0x0047, b"\x05"))[1:-1]))
        self.assertEqual(c.nodes, {})

    def test_observe_non_revealing_keeps_unknown(self):
        c = Classifier()
        c.observe(event(0x10, b"\x80\x03\x64"))      # battery only
        self.assertIn(0x10, c.nodes)
        self.assertEqual(c.nodes[0x10].battery, 0x64)   # level extracted...
        self.assertEqual(c.classify(0x10), (DeviceType.UNKNOWN, Confidence.UNKNOWN))  # ...but not type-revealing

    def test_battery_extracted_into_report(self):
        c = Classifier()
        c.observe(event(0x0c, b"\x80\x03\x4a"))      # 74 %
        self.assertEqual(c.nodes[0x0c].battery, 0x4a)
        self.assertEqual(c.report()[0x0c]["battery"], 0x4a)

    def test_battery_zero_is_valid(self):
        c = Classifier()
        c.observe(event(0x10, b"\x80\x03\x00"))      # 0 % — a real reading, not "missing"
        self.assertEqual(c.nodes[0x10].battery, 0)

    def test_battery_truncated_frame_ignored(self):
        c = Classifier()
        c.observe(event(0x10, b"\x80\x03"))          # no level byte (len 2) → must not crash
        self.assertIsNone(c.nodes[0x10].battery)

    def test_classify_unknown_node(self):
        self.assertEqual(Classifier().classify(0x99), (DeviceType.UNKNOWN, Confidence.UNKNOWN))

    def test_report(self):
        c = Classifier()
        c.ingest_roster(roster([(0x10, 1)]))
        c.observe(event(0x10, bytes.fromhex("7105000000ff070800")))
        self.assertEqual(c.report()[0x10],
                         {"power": "battery", "type": "motion",
                          "confidence": "inferred", "battery": None})


if __name__ == "__main__":
    unittest.main()
