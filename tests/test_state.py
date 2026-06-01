"""Unit tests for the device state model in hestia.state."""
from __future__ import annotations

import unittest

from hestia.protocol import Frame, build_frame, tlv
from hestia.state import State, tlv_value


def event(node: int, data: bytes) -> Frame:
    """A device state event [1e c09] carrying node (0x0047) + data (0x0046)."""
    payload = tlv(0x0047, bytes([node])) + tlv(0x0046, data)
    return Frame(build_frame(0x1E, 0x09, payload)[1:-1])


class TlvValueTests(unittest.TestCase):
    def test_found_and_missing(self):
        frame = event(0x11, b"\x26\x03\x10\x00\xfe")
        self.assertEqual(tlv_value(frame, 0x0047), b"\x11")
        self.assertIsNone(tlv_value(frame, 0x9999))


class DefaultsTests(unittest.TestCase):
    def test_crib_temp_defaults_none(self):
        # global (node-less) field, set only by the baby-monitor poller — no device event touches it.
        self.assertIsNone(State().crib_temp)

    def test_crib_temp_not_touched_by_events(self):
        st = State()
        st.apply(event(0x07, b"\x31\x05\x01\x04\x15"))   # a measured-temperature event
        self.assertIsNone(st.crib_temp)                  # per-node temp, never the global crib_temp

    def test_outdoor_temp_defaults_none(self):
        self.assertIsNone(State().outdoor_temp)          # global field, set only by the weather poller


class ApplyNonEventTests(unittest.TestCase):
    def test_non_c09_ignored(self):
        keepalive = Frame(build_frame(0x66, 0x01)[1:-1])
        self.assertEqual(State().apply(keepalive), {})

    def test_missing_node_or_data_ignored(self):
        only_node = Frame(build_frame(0x1E, 0x09, tlv(0x0047, b"\x11"))[1:-1])
        only_data = Frame(build_frame(0x1E, 0x09, tlv(0x0046, b"\x26\x03\x10\x00\xfe"))[1:-1])
        self.assertEqual(State().apply(only_node), {})
        self.assertEqual(State().apply(only_data), {})

    def test_unrecognised_attribute_changes_nothing(self):
        st = State()
        self.assertEqual(st.apply(event(0x11, b"\x99\x99")), {})
        self.assertEqual(st.doors, {})


class ApplyReturnsChangesTests(unittest.TestCase):
    def test_returns_changed_fields_with_discovery_keys(self):
        st = State()
        self.assertEqual(st.apply(event(0x05, b"\x26\x03\x30\x00\xfe")), {"level": 0x30})
        self.assertEqual(st.apply(event(0x09, b"\x43\x03\x01\x04\x00\x00\x00\x15")), {"setpoint": 0x15})

    def test_unchanged_value_returns_empty(self):
        st = State()
        first = st.apply(event(0x05, b"\x26\x03\x30\x00\xfe"))
        self.assertEqual(first, {"level": 0x30})
        again = st.apply(event(0x05, b"\x26\x03\x30\x00\xfe"))   # same value → no change
        self.assertEqual(again, {})


class ApplyDoorTests(unittest.TestCase):
    def test_open(self):
        st = State()
        st.apply(event(0x12, bytes.fromhex("7105000000ff061600")))
        self.assertEqual(st.doors[0x12], "open")

    def test_closed(self):
        st = State()
        st.apply(event(0x12, bytes.fromhex("7105000000ff061700")))
        self.assertEqual(st.doors[0x12], "closed")

    def test_unknown_state_byte(self):
        st = State()
        st.apply(event(0x12, bytes.fromhex("7105000000ff069900")))
        self.assertEqual(st.doors[0x12], "?99")


class ApplyOtherTests(unittest.TestCase):
    def test_level(self):
        st = State()
        st.apply(event(0x05, b"\x26\x03\x30\x00\xfe"))
        self.assertEqual(st.levels[0x05], 0x30)

    def test_switch_on(self):
        st = State()
        st.apply(event(0x0E, b"\x25\x03\xff\xff\x00"))
        self.assertIs(st.switches[0x0E], True)

    def test_switch_off(self):
        st = State()
        st.apply(event(0x0E, b"\x25\x03\x00\x00\x00"))
        self.assertIs(st.switches[0x0E], False)

    def test_thermostat_setpoint(self):
        st = State()
        st.apply(event(0x0D, b"\x43\x03\x01\x04\x00\x00\x00\x15"))
        self.assertEqual(st.thermostat_setpoint[0x0D], 0x15)

    def test_thermostat_power_on(self):
        st = State()
        st.apply(event(0x0D, b"\x40\x03\x01"))
        self.assertIs(st.thermostat_on[0x0D], True)

    def test_thermostat_power_off(self):
        st = State()
        st.apply(event(0x0D, b"\x40\x03\x00"))
        self.assertIs(st.thermostat_on[0x0D], False)

    def test_measured_temperature(self):
        st = State()
        st.apply(event(0x0D, b"\x31\x05\x01\x04\x00\x00\x00\x16"))
        self.assertEqual(st.temperature[0x0D], 0x16)


class ApplyGangTests(unittest.TestCase):
    def test_two_endpoints_tracked(self):
        st = State()
        # 60 0d <ep> 00 25 03 <ff/00>
        self.assertEqual(st.apply(event(0x07, bytes.fromhex("600d01002503ff"))), {"endpoints": {1: True}})
        self.assertEqual(st.apply(event(0x07, bytes.fromhex("600d02002503 00".replace(" ", "")))),
                         {"endpoints": {1: True, 2: False}})
        self.assertEqual(st.gang[0x07], {1: True, 2: False})

    def test_unchanged_endpoint_returns_empty(self):
        st = State()
        st.apply(event(0x07, bytes.fromhex("600d01002503ff")))
        self.assertEqual(st.apply(event(0x07, bytes.fromhex("600d01002503ff"))), {})


class ApplyMeteringTests(unittest.TestCase):
    def test_energy_kwh(self):
        st = State()
        # 32 02 2144 <A:4> <mid:2> <B:4> — 0x036b = 875 → 8.75 kWh
        st.apply(event(0x13, bytes.fromhex("320221440000036b012d0000036b")))
        self.assertEqual(st.plug_kwh[0x13], 8.75)

    def test_energy_meter_flag_masked(self):
        st = State()
        # plug 0x15 sets the high bit: 0x80000a9c & 0x7fffffff = 0xa9c = 2716 → 27.16 kWh
        st.apply(event(0x15, bytes.fromhex("3202214480000a9c009480000a9c")))
        self.assertEqual(st.plug_kwh[0x15], 27.16)

    def test_power_w(self):
        st = State()
        # 32 02 a14a <A:2> <mid:2> <B:2> — 0x000d = 13 W
        st.apply(event(0x13, bytes.fromhex("3202a14a000d012d0000")))
        self.assertEqual(st.plug_w[0x13], 13)

    def test_power_short_frame_no_b_slot(self):
        st = State()
        st.apply(event(0x13, bytes.fromhex("3202a14a000d")))   # only the A slot present
        self.assertEqual(st.plug_w[0x13], 13)

    def test_voltage_lands_in_b_slot(self):
        st = State()
        # A=0000, B=0x5f8a=24458 → max() picks B → 244.58 V
        st.apply(event(0x14, bytes.fromhex("3202a1420000002c5f8a")))
        self.assertEqual(st.plug_v[0x14], 244.58)

    def test_secondary_counter_ignored(self):
        st = State()
        st.apply(event(0x13, bytes.fromhex("3202215400000000012d00000000")))  # 21 54 — TBD
        self.assertEqual((st.plug_w, st.plug_kwh, st.plug_v), ({}, {}, {}))

    def test_truncated_metering_ignored(self):
        st = State()
        st.apply(event(0x13, bytes.fromhex("320221440000")))   # 2144 but body < 4 bytes
        self.assertEqual(st.plug_kwh, {})

    def test_empty_body_metering_ignored(self):
        st = State()
        st.apply(event(0x13, bytes.fromhex("32022144")))       # sub but no body at all
        self.assertEqual(st.plug_kwh, {})


class ApplySceneTests(unittest.TestCase):
    def test_scene_activation_switch(self):                     # 2b 01 <scene> <dur>
        st = State()
        self.assertEqual(st.apply(event(0x03, bytes.fromhex("2b010200"))),
                         {"scene": {"id": 2, "kind": "scene"}})

    def test_scene_activation_emits_every_press(self):          # an event — not value-gated
        st = State()
        st.apply(event(0x03, bytes.fromhex("2b010200")))
        self.assertEqual(st.apply(event(0x03, bytes.fromhex("2b010200"))),
                         {"scene": {"id": 2, "kind": "scene"}})

    def test_scene_activation_truncated_ignored(self):          # < 4 bytes
        st = State()
        self.assertEqual(st.apply(event(0x03, bytes.fromhex("2b0102"))), {})

    def test_central_scene_blind(self):                         # 5b 03 <seq> <keyAttr> <scene>
        st = State()
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b03028001"))),
                         {"scene": {"id": 1, "kind": "central"}})

    def test_central_scene_dedup_same_seq(self):                # slow-refresh repeat
        st = State()
        st.apply(event(0x05, bytes.fromhex("5b03028001")))
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b03028001"))), {})  # same seq → dropped

    def test_central_scene_new_seq_emits(self):
        st = State()
        st.apply(event(0x05, bytes.fromhex("5b03028001")))
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b03038001"))),     # seq 02 → 03
                         {"scene": {"id": 1, "kind": "central"}})

    def test_central_scene_any_keyattr_accepted(self):          # not gated on 0x80
        st = State()
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b03020003"))),     # keyAttr 0x00, scene 3
                         {"scene": {"id": 3, "kind": "central"}})

    def test_central_scene_seq_wrap(self):                      # 0xff → 0x00 is a new press
        st = State()
        st.apply(event(0x05, bytes.fromhex("5b03ff8001")))
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b03008001"))),
                         {"scene": {"id": 1, "kind": "central"}})

    def test_central_scene_seq_is_per_node(self):               # same seq on a different node still emits
        st = State()
        st.apply(event(0x04, bytes.fromhex("5b03028001")))
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b03028001"))),
                         {"scene": {"id": 1, "kind": "central"}})
        # NB: a device that power-cycles and re-issues a seq equal to its last-seen one
        # is indistinguishable from a slow-refresh repeat → that one press is dropped
        # (see test_central_scene_dedup_same_seq); accepted, the user re-presses.

    def test_central_scene_truncated_ignored(self):             # < 5 bytes
        st = State()
        self.assertEqual(st.apply(event(0x05, bytes.fromhex("5b030280"))), {})
        self.assertEqual(st.scene_seq, {})

    def test_scene_seq_not_in_state_snapshot(self):
        from hestia.proxy import state_snapshot
        st = State()
        st.apply(event(0x05, bytes.fromhex("5b03028001")))
        self.assertNotIn("scene_seq", state_snapshot(st))       # dedup bookkeeping never leaks


if __name__ == "__main__":
    unittest.main()
