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


class SnapshotTests(unittest.TestCase):
    def test_roundtrip_node_keyed_maps_and_gang_only(self):
        st = State()
        st.doors[0x12] = "open"
        st.levels[0x05] = 42
        st.switches[0x0E] = True
        st.thermostat_setpoint[0x0D] = 21
        st.thermostat_on[0x0D] = False
        st.temperature[0x0D] = 19
        st.plug_w[0x13] = 13
        st.plug_kwh[0x13] = 8.75
        st.plug_v[0x14] = 244.58
        st.gang[0x07] = {1: True, 2: False}
        st.scene_seq[0x05] = 7
        st.crib_temp = 22.0
        st.crib_temp_ts = 1_699_900_000.0                  # crib sample time IS cached too
        st.outdoor_temp = -3.0
        st.outdoor_humidity = 44.0
        st.outdoor_temp_ts = 1_700_000_000.0               # sample time IS cached (freshness survives a restart)
        st.outdoor_battery_ok = False                      # battery flag is runtime-only → NOT cached
        st.klima = {"power": True, "mode": "cool", "temp": 22}

        snap = st.to_snapshot()
        self.assertEqual(snap["doors"], {"18": "open"})
        self.assertEqual(snap["gang"], {"7": {"1": True, "2": False}})
        self.assertNotIn("scene_seq", snap)                # dedup bookkeeping stays runtime-only
        self.assertEqual(snap["globals"],                  # global temps + ts ARE cached (survive a restart)
                         {"crib_temp": 22.0, "crib_temp_ts": 1_699_900_000.0, "outdoor_temp": -3.0,
                          "outdoor_humidity": 44.0,
                          "outdoor_temp_ts": 1_700_000_000.0})  # outdoor_battery_ok omitted — runtime-only
        self.assertEqual(snap["klima"], {"power": True, "mode": "cool", "temp": 22})

        restored = State()
        restored.load_snapshot(snap)
        self.assertEqual(restored.doors, {0x12: "open"})
        self.assertEqual(restored.levels, {0x05: 42})
        self.assertEqual(restored.switches, {0x0E: True})
        self.assertEqual(restored.thermostat_setpoint, {0x0D: 21})
        self.assertEqual(restored.thermostat_on, {0x0D: False})
        self.assertEqual(restored.temperature, {0x0D: 19})
        self.assertEqual(restored.plug_w, {0x13: 13})
        self.assertEqual(restored.plug_kwh, {0x13: 8.75})
        self.assertEqual(restored.plug_v, {0x14: 244.58})
        self.assertEqual(restored.gang, {0x07: {1: True, 2: False}})
        self.assertEqual((restored.crib_temp, restored.outdoor_temp, restored.outdoor_humidity),
                         (22.0, -3.0, 44.0))               # globals restored → no "—" after a restart
        self.assertEqual((restored.crib_temp_ts, restored.outdoor_temp_ts),
                         (1_699_900_000.0, 1_700_000_000.0))  # both freshness ts restored
        self.assertIsNone(restored.outdoor_battery_ok)     # runtime-only flag NOT restored (re-read next sample)
        self.assertEqual(restored.klima, {"power": True, "mode": "cool", "temp": 22})  # A/C state survives
        self.assertFalse(restored.dirty)

    def test_load_snapshot_restores_partial_and_ignores_bad_globals(self):
        st = State()
        st.load_snapshot({"globals": {"crib_temp": 21.5, "outdoor_temp": "nan",
                                      "outdoor_humidity": True}})  # str + bool rejected
        self.assertEqual(st.crib_temp, 21.5)
        self.assertIsNone(st.outdoor_temp)                 # non-numeric ignored
        self.assertIsNone(st.outdoor_humidity)             # bool is an int subclass — explicitly rejected
        st.load_snapshot({"globals": "not a dict"})        # malformed section → no-op
        self.assertEqual(st.crib_temp, 21.5)
        st.load_snapshot({"globals": {"crib_temp": float("nan"), "outdoor_temp": float("inf")}})
        self.assertEqual(st.crib_temp, 21.5)               # NaN/inf rejected → values unchanged
        self.assertIsNone(st.outdoor_temp)

    def test_load_klima_accepts_valid_shapes_and_rejects_malformed(self):
        st = State()
        self.assertIsNone(st.klima)                                   # default: never commanded
        st.load_snapshot({"klima": {"power": True, "mode": "cool", "temp": 22}})
        self.assertEqual(st.klima, {"power": True, "mode": "cool", "temp": 22})
        st.load_snapshot({"klima": {"power": False, "mode": None, "temp": None}})   # off, no mode/temp
        self.assertEqual(st.klima, {"power": False, "mode": None, "temp": None})
        for bad in ("not a dict",
                    {"power": "on"},                                  # power must be a real bool
                    {"power": 1, "mode": "cool", "temp": 22},         # 1 is not a bool
                    {"power": True, "mode": 5, "temp": 22},           # mode must be str/None
                    {"power": True, "mode": "cool", "temp": "22"},    # temp must be int/None
                    {"power": True, "mode": "cool", "temp": True}):   # bool is an int subclass → rejected
            st.load_snapshot({"klima": bad})
            self.assertIsNone(st.klima, bad)                          # a corrupt section clears to None

    def test_load_snapshot_tolerates_corrupt_partial_wrong_type_blob(self):
        st = State()
        st.load_snapshot("not a dict")
        self.assertEqual(st.doors, {})

        st.load_snapshot({
            "doors": {"18": "open", "bad": "closed", None: "closed"},
            "levels": "not a dict",
            "switches": {"14": True},
            "thermostat_setpoint": {"13": 21},
            "thermostat_on": {"13": False},
            "temperature": {"13": 19},
            "plug_w": {"19": 13},
            "plug_kwh": {"19": 8.75},
            "plug_v": {"20": 244.58},
            "gang": {
                "7": {"1": True, "bad": False, None: True},
                "bad": {"1": True},
                "8": "not a dict",
                "9": {"bad": True},
            },
        })
        self.assertEqual(st.doors, {18: "open"})
        self.assertEqual(st.levels, {})
        self.assertEqual(st.switches, {14: True})
        self.assertEqual(st.thermostat_setpoint, {13: 21})
        self.assertEqual(st.thermostat_on, {13: False})
        self.assertEqual(st.temperature, {13: 19})
        self.assertEqual(st.plug_w, {19: 13})
        self.assertEqual(st.plug_kwh, {19: 8.75})
        self.assertEqual(st.plug_v, {20: 244.58})
        self.assertEqual(st.gang, {7: {1: True}})
        self.assertFalse(st.dirty)

        st.load_snapshot({"gang": "not a dict"})
        self.assertEqual(st.gang, {7: {1: True}})


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

    def test_dirty_set_only_when_apply_returns_changes(self):
        st = State()
        self.assertFalse(st.dirty)
        self.assertEqual(st.apply(event(0x05, b"\x26\x03\x30\x00\xfe")), {"level": 0x30})
        self.assertTrue(st.dirty)
        st.dirty = False
        self.assertEqual(st.apply(event(0x05, b"\x26\x03\x30\x00\xfe")), {})
        self.assertFalse(st.dirty)
        self.assertEqual(st.apply(event(0x05, b"\x99\x99")), {})
        self.assertFalse(st.dirty)

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


class ApplyMotionTests(unittest.TestCase):
    """PIR / Home-Security notification `71 05 .. ff 07 <event>` — 0x08 = motion, else idle."""

    def test_motion_detected(self):
        st = State()
        self.assertEqual(st.apply(event(0x0F, bytes.fromhex("7105000000ff070800"))), {"motion": True})
        self.assertIs(st.motion[0x0F], True)

    def test_idle_clears_motion(self):
        st = State()
        # the idle/clear frame: event 0x00 with a trailing param naming the cleared event (0x08 = motion)
        self.assertEqual(st.apply(event(0x0F, bytes.fromhex("7105000000ff07000108"))), {"motion": False})
        self.assertIs(st.motion[0x0F], False)

    def test_door_and_motion_are_distinct_notification_types(self):
        st = State()
        st.apply(event(0x11, bytes.fromhex("7105000000ff061600")))   # ff 06 → door
        st.apply(event(0x0F, bytes.fromhex("7105000000ff070800")))   # ff 07 → motion
        self.assertEqual((st.doors, st.motion), ({0x11: "open"}, {0x0F: True}))


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

    def test_thermostat_active_non_heat_mode_is_on(self):
        # Any non-zero Thermostat Mode (Cool 0x02 / Auto 0x03 / manufacturer) = on, not just Heat 0x01.
        for mode in (0x02, 0x03, 0x0B):
            st = State()
            st.apply(event(0x0D, bytes([0x40, 0x03, mode])))
            self.assertIs(st.thermostat_on[0x0D], True, mode)

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


class ApplyCommandTests(unittest.TestCase):
    """The optimistic echo of a control command — devices ACK a remote SET but
    never report a [1e 09], so apply_command mirrors the commanded value into
    State so the live UI tracks a control press (same fields a report would emit)."""

    def test_switch_on_off(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "node": 5, "on": True}), {"switch": True})
        self.assertIs(st.switches[5], True)
        self.assertTrue(st.dirty)
        self.assertEqual(st.apply_command({"op": "switch", "node": 5, "on": False}), {"switch": False})
        self.assertIs(st.switches[5], False)

    def test_switch_unchanged_emits_nothing(self):
        st = State()
        st.apply_command({"op": "switch", "node": 5, "on": True})
        st.dirty = False
        self.assertEqual(st.apply_command({"op": "switch", "node": 5, "on": True}), {})  # already on
        self.assertFalse(st.dirty)                                 # no change → not re-dirtied

    def test_endpoint_switch_emits_full_rollup(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "node": 7, "endpoint": 1, "on": True}),
                         {"endpoints": {1: True}})
        # a second channel merges into the same per-node roll-up
        self.assertEqual(st.apply_command({"op": "switch", "node": 7, "endpoint": 2, "on": False}),
                         {"endpoints": {1: True, 2: False}})
        self.assertEqual(st.gang[7], {1: True, 2: False})

    def test_endpoint_switch_unchanged_emits_nothing(self):
        st = State()
        st.apply_command({"op": "switch", "node": 7, "endpoint": 1, "on": True})
        self.assertEqual(st.apply_command({"op": "switch", "node": 7, "endpoint": 1, "on": True}), {})

    def test_string_boolean_matches_the_wire(self):
        # Legacy/control-socket string forms coerce exactly like build_command's _bool/_int,
        # so the echo can NEVER publish the opposite of what was sent on the wire.
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "node": 5, "on": "false"}), {"switch": False})
        self.assertEqual(st.apply_command({"op": "switch", "node": 5, "on": "on"}), {"switch": True})

    def test_hex_string_node_matches_the_wire(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "node": "0x05", "on": "on"}), {"switch": True})
        self.assertIs(st.switches[5], True)

    def test_reporting_ops_are_not_echoed(self):
        # "reports win where they exist": cover/level report their own state reliably, so they are NOT
        # optimistically echoed. (Switch/2-gang never report; thermostat POWER and SETPOINT do not report
        # reliably either, so those ARE echoed — see test_thermostat_power_echoed / _setpoint_echoed.)
        st = State()
        self.assertEqual(st.apply_command({"op": "level", "node": 4, "value": 60}), {})
        self.assertEqual(st.apply_command({"op": "cover", "node": 8, "value": 99}), {})
        self.assertEqual(st.levels, {})
        self.assertFalse(st.dirty)

    def test_thermostat_setpoint_echoed(self):
        # Setpoint is echoed too: a live capture showed these TRVs frequently do NOT report 43 03 after a
        # SET, so a report-only setpoint left State stale (the UI then re-pinned its dropdown to the old
        # value — "set 18, jumps back to 28"). Echo immediately, stored as the integer the device reports.
        st = State()
        self.assertEqual(st.apply_command({"op": "thermostat", "node": 9, "celsius": 18}),
                         {"setpoint": 18})
        self.assertEqual(st.thermostat_setpoint[9], 18)
        self.assertTrue(st.dirty)
        self.assertEqual(st.apply_command({"op": "thermostat", "node": 9, "celsius": 21.4}),
                         {"setpoint": 21})            # float rounds to the device's integer °C
        st.dirty = False
        self.assertEqual(st.apply_command({"op": "thermostat", "node": 9, "celsius": 21}), {})  # unchanged
        self.assertFalse(st.dirty)
        # a missing / non-numeric (incl. bool) celsius is a defensive no-op
        self.assertEqual(st.apply_command({"op": "thermostat", "node": 9}), {})
        self.assertEqual(st.apply_command({"op": "thermostat", "node": 9, "celsius": True}), {})

    def test_thermostat_power_echoed(self):
        # Thermostat ON/OFF (mode) only reports when GET-polled, so the press is echoed optimistically.
        st = State()
        self.assertEqual(st.apply_command({"op": "thermostat_power", "node": 9, "on": True}),
                         {"thermostat_on": True})
        self.assertIs(st.thermostat_on[9], True)
        self.assertTrue(st.dirty)
        self.assertEqual(st.apply_command({"op": "thermostat_power", "node": 9, "on": "false"}),
                         {"thermostat_on": False})        # string coerces like the wire
        self.assertIs(st.thermostat_on[9], False)
        st.dirty = False
        self.assertEqual(st.apply_command({"op": "thermostat_power", "node": 9, "on": False}), {})  # unchanged
        self.assertFalse(st.dirty)

    def test_non_stateful_ops_emit_nothing(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "raw", "hex": "deadbeef"}), {})       # not in the table
        self.assertEqual(st.apply_command({"op": "lights", "channels": []}), {})       # multi-channel, skip
        self.assertFalse(st.dirty)

    def test_bad_node_emits_nothing(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "on": True}), {})           # no node
        self.assertEqual(st.apply_command({"op": "switch", "node": "nope", "on": True}), {})  # unparseable

    def test_missing_field_is_swallowed(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "node": 5}), {})            # no "on" → KeyError → {}
        self.assertFalse(st.dirty)

    def test_ambiguous_boolean_swallowed(self):
        st = State()
        self.assertEqual(st.apply_command({"op": "switch", "node": 5, "on": "maybe"}), {})  # _bool → ValueError → {}
        self.assertEqual(st.switches, {})


if __name__ == "__main__":
    unittest.main()
