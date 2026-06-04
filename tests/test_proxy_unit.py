"""Unit tests for hestia.proxy — pure helpers and the async session/control
paths, exercised with in-memory fakes (no real sockets)."""
from __future__ import annotations

import asyncio
import datetime
import shutil
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hestia import commands, proxy, sensor433
from hestia.automations import AutomationEngine, AutomationStore, Rule
from hestia.protocol import Frame, build_frame, tlv

# A scene rule whose trigger matches SCENE_PRESS (node 0x02 → scene 3).
AUTO_SCENE_RULE = {
    "id": "a1",
    "trigger": {"type": "scene", "node": 2, "scene_id": 3},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
AUTO_TIME_RULE = {
    "id": "t1",
    "trigger": {"type": "time", "at": "07:30"},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
TIME_0730 = datetime.datetime(2026, 1, 5, 7, 30)   # a Monday at 07:30
AUTO_PRESENCE_RULE = {
    "id": "p1",
    "trigger": {"type": "presence", "mac": "aa:bb:cc:dd:ee:ff", "event": "arrive"},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}

DOOR_OPEN = build_frame(
    0x1E, 0x09,
    tlv(0x0047, b"\x12") + tlv(0x0046, bytes.fromhex("7105000000ff061600")),
)
NOISE = build_frame(0x66, 0x01)

# A function-button press (node 0x02 → scene 3) and the cloud's batch reaction.
SCENE_PRESS = build_frame(0x1E, 0x09, tlv(0x0047, b"\x02") + tlv(0x0046, b"\x2b\x01\x03\x00"))
SCENE_ELEMENTS = bytes.fromhex("0101030626010002010308260100")
SCENE_BATCH = build_frame(0x1E, 0x32, tlv(0x001F, b"\x00\x00\x00\x05") + tlv(0x005A, SCENE_ELEMENTS))


# --- fakes -------------------------------------------------------------------

class FakeWriter:
    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def get_extra_info(self, _key):
        return ("fake", 0)


class OSErrorOnCloseWriter(FakeWriter):
    async def wait_closed(self):
        raise OSError("boom")


class FakeReader:
    def __init__(self, chunks=None, raise_exc=None, block=False):
        self._chunks = list(chunks or [])
        self._raise = raise_exc
        self._block = block

    async def read(self, _n=4096):
        if self._raise is not None:
            raise self._raise
        if self._block:
            await asyncio.Event().wait()      # never resolves (cancelled in teardown)
        return self._chunks.pop(0) if self._chunks else b""


class LineReader:
    def __init__(self, lines=None, raise_exc=None):
        self._lines = list(lines or [])
        self._raise = raise_exc

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._raise is not None:
            raise self._raise
        return b""


class FakeSession:
    def __init__(self, raise_exc=None):
        self.sent = []
        self._raise = raise_exc

    async def inject_to_device(self, raw):
        if self._raise is not None:
            raise self._raise
        self.sent.append(raw)


def make_session(rt, dev_reader=None, dev_writer=None, cloud_host="cloud", cloud_port=1):
    return proxy.ProxySession(rt, dev_reader or FakeReader(), dev_writer or FakeWriter(),
                              cloud_host, cloud_port)


# --- pure helpers ------------------------------------------------------------

class SafeSeqTests(unittest.TestCase):
    def test_first_value_is_start(self):
        self.assertEqual(next(proxy._safe_seq_counter(0x00010000)), 0x00010000)

    def test_skips_values_containing_flag_byte(self):
        # 0x0000007e has a 0x7e byte -> skipped, 0x7f yielded next.
        self.assertEqual(next(proxy._safe_seq_counter(0x7E)), 0x7F)

    def test_wraps_at_max_instead_of_overflowing(self):
        gen = proxy._safe_seq_counter(0xFFFFFFFF)
        self.assertEqual(next(gen), 0xFFFFFFFF)
        self.assertEqual(next(gen), 0xFFFFFFFF)   # wrapped back to start

    def test_no_emitted_value_contains_flag(self):
        gen = proxy._safe_seq_counter()
        self.assertTrue(all(0x7E not in next(gen).to_bytes(4, "big") for _ in range(2000)))


class RuntimeTests(unittest.TestCase):
    def test_next_seq_increments(self):
        rt = proxy.ProxyRuntime()
        self.assertEqual([rt.next_seq(), rt.next_seq()], [0x00010000, 0x00010001])


class IntTests(unittest.TestCase):
    def test_hex_string(self):
        self.assertEqual(proxy._int("0x0e"), 14)

    def test_decimal_string(self):
        self.assertEqual(proxy._int("14"), 14)

    def test_plain_int(self):
        self.assertEqual(proxy._int(14), 14)

    def test_invalid_string_raises(self):
        with self.assertRaises(ValueError):
            proxy._int("zz")


class BoolTests(unittest.TestCase):
    def test_real_bool(self):
        self.assertIs(proxy._bool(True), True)
        self.assertIs(proxy._bool(False), False)

    def test_true_strings(self):
        self.assertIs(proxy._bool("true"), True)
        self.assertIs(proxy._bool(" ON "), True)

    def test_false_strings(self):
        self.assertIs(proxy._bool("false"), False)
        self.assertIs(proxy._bool("off"), False)

    def test_ambiguous_string_rejected(self):
        with self.assertRaises(ValueError):
            proxy._bool("maybe")

    def test_non_bool_non_string_rejected(self):
        with self.assertRaises(ValueError):
            proxy._bool(1)


class ControlBindTests(unittest.TestCase):
    def test_loopback_allowed(self):
        proxy._require_safe_control_bind("127.0.0.1")   # no raise

    def test_remote_rejected_without_optin(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HESTIA_CONTROL_ALLOW_REMOTE", None)
            with self.assertRaises(RuntimeError):
                proxy._require_safe_control_bind("0.0.0.0")

    def test_remote_allowed_with_optin(self):
        with mock.patch.dict("os.environ", {"HESTIA_CONTROL_ALLOW_REMOTE": "1"}):
            proxy._require_safe_control_bind("0.0.0.0")   # no raise


class SummarizeTests(unittest.TestCase):
    def test_short_frame(self):
        self.assertTrue(proxy.summarize(Frame(b"\x1e")).startswith("[short]"))

    def test_ok_frame(self):
        s = proxy.summarize(Frame(DOOR_OPEN[1:-1]))
        self.assertIn("[1e c09]", s)
        self.assertNotIn("!cksum", s)

    def test_bad_checksum_marked(self):
        body = bytearray(DOOR_OPEN[1:-1])
        body[-1] ^= 0xFF
        self.assertIn("!cksum", proxy.summarize(Frame(bytes(body))))

    def test_unknown_type_uses_hex(self):
        self.assertIn("0x99", proxy.summarize(Frame(build_frame(0x99, 0x01)[1:-1])))


class BuildCommandTests(unittest.TestCase):
    def setUp(self):
        self.rt = proxy.ProxyRuntime()
        self.seq = 0x00010000   # first seq a fresh runtime hands out

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            proxy.build_command(self.rt, 5)

    def test_unknown_op_rejected(self):
        with self.assertRaises(ValueError):
            proxy.build_command(self.rt, {"op": "nope"})

    def test_raw_passthrough_does_not_consume_seq(self):
        out = proxy.build_command(self.rt, {"op": "raw", "hex": "7e1e00007e"})
        self.assertEqual(out, bytes.fromhex("7e1e00007e"))
        self.assertEqual(self.rt.next_seq(), self.seq)   # seq untouched by raw

    def test_cover(self):
        out = proxy.build_command(self.rt, {"op": "cover", "node": "0x05", "value": 0})
        self.assertEqual(out, commands.set_cover(self.seq, 5, 0))

    def test_level(self):
        out = proxy.build_command(self.rt, {"op": "level", "node": 14, "value": 64})
        self.assertEqual(out, commands.set_level(self.seq, 14, 64))

    def test_switch_on_off(self):
        on = proxy.build_command(self.rt, {"op": "switch", "node": 14, "on": True})
        self.assertEqual(on, commands.set_switch(self.seq, 14, True))
        off = proxy.build_command(self.rt, {"op": "switch", "node": 14, "on": False})
        self.assertEqual(off, commands.set_switch(self.seq + 1, 14, False))

    def test_switch_endpoint(self):
        out = proxy.build_command(self.rt, {"op": "switch", "node": 7, "endpoint": 2, "on": True})
        self.assertEqual(out, commands.set_endpoint_switch(self.seq, 7, 2, True))

    def test_lights(self):
        out = proxy.build_command(self.rt, {"op": "lights", "channels": [[4, 0], [8, 0x63]]})
        self.assertEqual(out, commands.set_lights(self.seq, [(4, 0), (8, 0x63)]))

    def test_thermostat(self):
        out = proxy.build_command(self.rt, {"op": "thermostat", "node": 13, "celsius": 21.0})
        self.assertEqual(out, commands.set_thermostat(self.seq, 13, 21.0))

    def test_thermostat_power(self):
        out = proxy.build_command(self.rt, {"op": "thermostat_power", "node": 13, "on": True})
        self.assertEqual(out, commands.set_thermostat_power(self.seq, 13, True))


class StateSnapshotTests(unittest.TestCase):
    def test_hex_keys_and_switches_present(self):
        rt = proxy.ProxyRuntime()
        rt.state.doors[0x12] = "open"
        rt.state.switches[0x0E] = True
        rt.state.gang[0x07] = {1: True, 2: False}
        snap = proxy.state_snapshot(rt.state)
        self.assertEqual(snap["doors"], {"0x12": "open"})
        self.assertEqual(snap["switches"], {"0x0e": True})
        self.assertIn("temperature", snap)
        self.assertEqual(snap["gang"], {"0x07": {"1": True, "2": False}})   # both key levels stringified
        self.assertEqual(snap["globals"],
                         {"crib_temp": None, "outdoor_temp": None, "outdoor_humidity": None})   # node-less globals

    def test_snapshot_reflects_global_temps(self):
        rt = proxy.ProxyRuntime()
        rt.state.crib_temp = 22.0
        snap = proxy.state_snapshot(rt.state)
        self.assertEqual(snap["globals"], {"crib_temp": 22.0, "outdoor_temp": None, "outdoor_humidity": None})

    def test_globals_snapshot_set_and_none(self):
        st = proxy.State()
        self.assertEqual(proxy.globals_snapshot(st),
                         {"crib_temp": None, "outdoor_temp": None, "outdoor_humidity": None})
        st.crib_temp, st.outdoor_temp, st.outdoor_humidity = 21.5, -3.0, 44.0
        self.assertEqual(proxy.globals_snapshot(st),
                         {"crib_temp": 21.5, "outdoor_temp": -3.0, "outdoor_humidity": 44.0})


class ObserveTests(unittest.TestCase):
    def test_short_frame_is_ignored(self):
        rt = proxy.ProxyRuntime()
        make_session(rt)._observe(Frame(b"\x1e"), "D->C")   # no crash
        self.assertEqual(rt.state.doors, {})

    def test_noise_frame_logs_debug(self):
        rt = proxy.ProxyRuntime()
        with self.assertLogs("hestia.proxy", level="DEBUG"):
            make_session(rt)._observe(Frame(NOISE[1:-1]), "C->D")

    def test_event_updates_state(self):
        rt = proxy.ProxyRuntime()
        make_session(rt)._observe(Frame(DOOR_OPEN[1:-1]), "D->C")
        self.assertEqual(rt.state.doors[0x12], "open")

    def test_bad_checksum_does_not_update_state(self):
        rt = proxy.ProxyRuntime()
        body = bytearray(DOOR_OPEN[1:-1])
        body[-1] ^= 0xFF
        make_session(rt)._observe(Frame(bytes(body)), "D->C")
        self.assertEqual(rt.state.doors, {})


class ObserveEngineTests(unittest.TestCase):
    """The proxy taps device events into the automation engine and returns any
    resulting command frames (which `_pump` injects to the device)."""

    def test_returns_engine_frames_on_match(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        out = make_session(rt)._observe(Frame(SCENE_PRESS[1:-1]), "D->C")
        self.assertEqual(len(out), 1)

    def test_direction_gate_blocks_cloud_to_device(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        self.assertEqual(make_session(rt)._observe(Frame(SCENE_PRESS[1:-1]), "C->D"), [])

    def test_no_change_no_engine_call(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        sess = make_session(rt)
        sess._observe(Frame(DOOR_OPEN[1:-1]), "D->C")            # door opens (no scene)
        out = sess._observe(Frame(DOOR_OPEN[1:-1]), "D->C")      # identical → no changed/scene
        self.assertEqual(out, [])


class SceneCaptureTests(unittest.TestCase):
    """Proxy-only: correlate a function-button press with the cloud's batch reaction
    and learn it for standalone replay (§5.7a)."""

    def _press_then(self, rt, batch, direction="C->D"):
        sess = make_session(rt)
        sess._observe(Frame(SCENE_PRESS[1:-1]), "D->C")     # press → pending set (session-local)
        sess._observe(Frame(batch[1:-1]), direction)        # cloud reaction
        return sess

    def test_capture_within_window(self):
        rt = proxy.ProxyRuntime()
        sess = self._press_then(rt, SCENE_BATCH)
        self.assertEqual(rt.registry.scene_batch(2, 3), SCENE_ELEMENTS.hex())
        self.assertIsNone(sess._pending_scene)              # consumed

    def test_capture_outside_window_is_inert(self):
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        sess._pending_scene = (2, 3, time.monotonic() - 100)  # stale press
        sess._observe(Frame(SCENE_BATCH[1:-1]), "C->D")
        self.assertIsNone(rt.registry.scene_batch(2, 3))
        self.assertIsNone(sess._pending_scene)              # stale → cleared

    def test_capture_requires_a_pending_press(self):
        rt = proxy.ProxyRuntime()
        make_session(rt)._observe(Frame(SCENE_BATCH[1:-1]), "C->D")   # no prior press
        self.assertIsNone(rt.registry.scene_batch(2, 3))

    def test_press_arms_only_device_to_cloud(self):
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        sess._observe(Frame(SCENE_PRESS[1:-1]), "C->D")     # a scene frame tapped cloud→device
        self.assertIsNone(sess._pending_scene)              # not armed (presses are D->C)

    def test_capture_direction_guard(self):
        rt = proxy.ProxyRuntime()
        sess = self._press_then(rt, SCENE_BATCH, direction="D->C")  # batch tapped device→cloud
        self.assertIsNone(rt.registry.scene_batch(2, 3))
        self.assertIsNotNone(sess._pending_scene)           # press left pending

    def test_batch_without_005a_leaves_press_pending(self):
        rt = proxy.ProxyRuntime()
        no_005a = build_frame(0x1E, 0x32, tlv(0x001F, b"\x00\x00\x00\x05"))
        sess = self._press_then(rt, no_005a)
        self.assertIsNone(rt.registry.scene_batch(2, 3))
        self.assertIsNotNone(sess._pending_scene)           # not consumed

    def test_bad_checksum_batch_not_captured(self):
        rt = proxy.ProxyRuntime()
        bad = bytearray(SCENE_BATCH[1:-1])
        bad[-1] ^= 0xFF
        sess = make_session(rt)
        sess._observe(Frame(SCENE_PRESS[1:-1]), "D->C")
        sess._observe(Frame(bytes(bad)), "C->D")             # corrupt → gated out
        self.assertIsNone(rt.registry.scene_batch(2, 3))

    def test_identical_batch_recaptured_idempotently(self):
        rt = proxy.ProxyRuntime()
        with self.assertLogs("hestia.proxy", level="INFO") as first:
            self._press_then(rt, SCENE_BATCH)                # learns → logs
        self.assertTrue(any("learned scene 3" in m for m in first.output))
        with self.assertLogs("hestia.proxy", level="INFO") as second:
            self._press_then(rt, SCENE_BATCH)                # identical → record_scene False
        self.assertFalse(any("learned scene" in m for m in second.output))   # no re-learn log
        self.assertEqual(rt.registry.scene_batch(2, 3), SCENE_ELEMENTS.hex())

    def test_latest_press_wins_on_overwrite(self):
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        sess._observe(Frame(SCENE_PRESS[1:-1]), "D->C")      # node 2 / scene 3
        press_b = build_frame(0x1E, 0x09, tlv(0x0047, b"\x07") + tlv(0x0046, b"\x2b\x01\x02\x00"))
        sess._observe(Frame(press_b[1:-1]), "D->C")          # node 7 / scene 2 (overwrites)
        sess._observe(Frame(SCENE_BATCH[1:-1]), "C->D")
        self.assertEqual(rt.registry.scene_batch(7, 2), SCENE_ELEMENTS.hex())
        self.assertIsNone(rt.registry.scene_batch(2, 3))     # earlier press lost


# --- async paths -------------------------------------------------------------

class CloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_is_noop(self):
        await proxy._close(None)

    async def test_closes_writer(self):
        w = FakeWriter()
        await proxy._close(w)
        self.assertTrue(w.closed)

    async def test_swallows_oserror(self):
        await proxy._close(OSErrorOnCloseWriter())   # no raise


class ProcessControlOpTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            await proxy.process_control_op(proxy.ProxyRuntime(), 5)

    async def test_state_op(self):
        rt = proxy.ProxyRuntime()
        rt.state.doors[0x12] = "open"
        resp = await proxy.process_control_op(rt, {"op": "state"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"]["doors"], {"0x12": "open"})

    async def test_scenes_op_lists_only_nodes_with_scenes(self):
        rt = proxy.ProxyRuntime()
        rt.registry.record_scene(2, 3, "deadbeef")          # node WITH scenes → included
        rt.registry.observe(7, "switch", "inferred")        # node WITHOUT scenes → excluded
        resp = await proxy.process_control_op(rt, {"op": "scenes"})
        self.assertEqual(resp, {"ok": True, "scenes": {"2": {"3": "deadbeef"}}})

    async def test_scenes_op_empty(self):
        resp = await proxy.process_control_op(proxy.ProxyRuntime(), {"op": "scenes"})
        self.assertEqual(resp, {"ok": True, "scenes": {}})

    async def test_scenes_op_works_with_no_device(self):
        rt = proxy.ProxyRuntime()                            # no session connected
        rt.registry.record_scene(2, 3, "deadbeef")
        resp = await proxy.process_control_op(rt, {"op": "scenes"})
        self.assertTrue(resp["ok"])                          # served before the session check

    async def test_no_device(self):
        resp = await proxy.process_control_op(proxy.ProxyRuntime(), {"op": "cover", "node": 5, "value": 0})
        self.assertEqual(resp, {"ok": False, "error": "no device connected"})

    async def test_injects_and_reports_sent(self):
        rt = proxy.ProxyRuntime()
        sess = FakeSession()
        rt.sessions.append(sess)
        resp = await proxy.process_control_op(rt, {"op": "cover", "node": 5, "value": 0})
        self.assertTrue(resp["ok"])
        self.assertEqual(bytes.fromhex(resp["sent"]), sess.sent[0])

    async def test_device_write_failure(self):
        rt = proxy.ProxyRuntime()
        rt.sessions.append(FakeSession(raise_exc=OSError("pipe")))
        resp = await proxy.process_control_op(rt, {"op": "cover", "node": 5, "value": 0})
        self.assertFalse(resp["ok"])
        self.assertIn("device write failed", resp["error"])

    async def test_success_publishes_optimistic_state(self):
        # The device only ACKs a remote SET ([1e 08]); it never reports a [1e 09].
        # A successful control must therefore echo the commanded state onto the live
        # feed itself, or the dashboard never tracks a control press (the live bug).
        rt = proxy.ProxyRuntime()
        rt.sessions.append(FakeSession())
        sub = await rt.event_bus.try_subscribe()
        await proxy.process_control_op(rt, {"op": "switch", "node": 5, "on": True})
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        self.assertIn({"type": "state", "node": 5, "fields": {"switch": True}}, events)
        self.assertIs(rt.state.switches[5], True)                  # State updated too

    async def test_non_stateful_op_publishes_no_state(self):
        rt = proxy.ProxyRuntime()
        rt.sessions.append(FakeSession())
        sub = await rt.event_bus.try_subscribe()
        await proxy.process_control_op(rt, {"op": "raw", "hex": "abcd"})  # raw → no commanded field
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        self.assertFalse([e for e in events if e.get("type") == "state"])


class AutomationOpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "automations.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _rt(self):
        return proxy.ProxyRuntime(engine=AutomationEngine(AutomationStore(self.path)))

    async def test_list_returns_rules(self):
        rt = self._rt()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        resp = await proxy.process_control_op(rt, {"op": "automations"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["automations"][0]["id"], "a1")

    async def test_list_time_rule_is_public_schema(self):
        rt = self._rt()
        rt.engine.set_rule(Rule.from_dict(AUTO_TIME_RULE))
        resp = await proxy.process_control_op(rt, {"op": "automations"})
        self.assertEqual(resp["automations"][0]["trigger"],   # no leaked hour/minute
                         {"type": "time", "at": "07:30", "days": None})

    async def test_set_persists(self):
        rt = self._rt()
        resp = await proxy.process_control_op(rt, {"op": "automation_set", "rule": AUTO_SCENE_RULE})
        self.assertEqual(resp, {"ok": True, "id": "a1"})
        self.assertIn("a1", rt.engine.store.rules)
        self.assertTrue(self.path.exists())

    async def test_set_invalid_rule_via_execute(self):
        rt = self._rt()
        resp = await proxy.execute_control_line(rt, b'{"op": "automation_set", "rule": {"id": ""}}\n')
        self.assertFalse(resp["ok"])
        self.assertEqual(rt.engine.store.rules, {})

    async def test_set_save_failure_leaves_engine_untouched(self):
        rt = self._rt()
        with mock.patch.object(rt.engine.store, "write_payload", side_effect=OSError("disk full")):
            resp = await proxy.process_control_op(rt, {"op": "automation_set", "rule": AUTO_SCENE_RULE})
        self.assertFalse(resp["ok"])
        self.assertIn("automations save failed", resp["error"])
        self.assertEqual(rt.engine.store.rules, {})          # rule never went live (persist-before-live)
        self.assertEqual(rt.engine._last_match, {})          # no loop-guard state created either
        self.assertEqual(rt.engine._last_fired, {})

    async def test_set_save_failure_keeps_previous_rule(self):
        rt = self._rt()
        await proxy.process_control_op(rt, {"op": "automation_set", "rule": AUTO_SCENE_RULE})  # persisted
        newer = dict(AUTO_SCENE_RULE, debounce=9)
        with mock.patch.object(rt.engine.store, "write_payload", side_effect=OSError("disk full")):
            resp = await proxy.process_control_op(rt, {"op": "automation_set", "rule": newer})
        self.assertFalse(resp["ok"])
        self.assertEqual(rt.engine.store.rules["a1"].debounce, 0.0)   # original definition intact

    async def test_delete_found(self):
        rt = self._rt()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        resp = await proxy.process_control_op(rt, {"op": "automation_delete", "id": "a1"})
        self.assertEqual(resp, {"ok": True, "deleted": True})
        self.assertEqual(rt.engine.store.rules, {})
        self.assertTrue(self.path.exists())

    async def test_delete_absent_does_not_persist(self):
        rt = self._rt()
        resp = await proxy.process_control_op(rt, {"op": "automation_delete", "id": "ghost"})
        self.assertEqual(resp, {"ok": True, "deleted": False})
        self.assertFalse(self.path.exists())               # nothing changed → no write

    async def test_delete_save_failure_keeps_rule(self):
        rt = self._rt()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        with mock.patch.object(rt.engine.store, "write_payload", side_effect=OSError("disk full")):
            resp = await proxy.process_control_op(rt, {"op": "automation_delete", "id": "a1"})
        self.assertFalse(resp["ok"])
        self.assertIn("automations save failed", resp["error"])
        self.assertIn("a1", rt.engine.store.rules)           # never deleted (persist-before-live)

    async def test_commit_cancel_after_write_lands_still_swaps_live(self):
        """A commit cancelled AFTER its write lands still swaps the rule into the live
        engine (disk == memory), so a later commit snapshotting `store.rules` can't
        clobber the durable change — then it propagates the cancel."""
        rt = self._rt()
        loop = asyncio.get_running_loop()
        controlled = loop.create_future()                    # drive the write future directly
        rule = Rule.from_dict(AUTO_SCENE_RULE)

        def change(candidate):
            candidate[rule.id] = rule
            return rule.id

        with mock.patch.object(loop, "run_in_executor", side_effect=lambda *a: controlled):
            task = asyncio.create_task(proxy._commit_automation(rt, change))
            await asyncio.sleep(0)                            # suspended in the write-wait
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())                    # waiting the write out under save_lock
            controlled.set_result(None)                       # the write LANDS
            with self.assertRaises(asyncio.CancelledError):  # the cancel is then propagated
                await task
        self.assertIn("a1", rt.engine.store.rules)            # swapped live despite the cancel
        self.assertFalse(rt.engine.store.dirty)               # synced to disk
        self.assertFalse(rt.save_lock.locked())

    async def test_delete_non_string_id_via_execute(self):
        rt = self._rt()
        resp = await proxy.execute_control_line(rt, b'{"op": "automation_delete", "id": 5}\n')
        self.assertFalse(resp["ok"])


class ExecuteControlLineTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_returns_none(self):
        self.assertIsNone(await proxy.execute_control_line(proxy.ProxyRuntime(), b"  \n"))

    async def test_bad_json_returns_error(self):
        resp = await proxy.execute_control_line(proxy.ProxyRuntime(), b"not json\n")
        self.assertFalse(resp["ok"])

    async def test_unknown_op_returns_error(self):
        rt = proxy.ProxyRuntime()
        rt.sessions.append(FakeSession())
        resp = await proxy.execute_control_line(rt, b'{"op": "nope"}\n')
        self.assertFalse(resp["ok"])

    async def test_overflow_returns_error(self):
        rt = proxy.ProxyRuntime()
        rt.sessions.append(FakeSession())
        resp = await proxy.execute_control_line(rt, b'{"op": "thermostat", "node": 13, "celsius": 1e9}\n')
        self.assertFalse(resp["ok"])

    async def test_valid_op(self):
        rt = proxy.ProxyRuntime()
        rt.sessions.append(FakeSession())
        resp = await proxy.execute_control_line(rt, b'{"op": "cover", "node": 5, "value": 0}\n')
        self.assertTrue(resp["ok"])


class HandleControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_processes_lines_and_skips_blank(self):
        rt = proxy.ProxyRuntime()
        writer = FakeWriter()
        reader = LineReader([b"\n", b'{"op": "state"}\n'])
        await proxy.handle_control(rt, reader, writer)
        # exactly one response line (blank skipped), and the writer was closed.
        self.assertEqual(writer.buf.decode().count("\n"), 1)
        self.assertIn('"ok": true', writer.buf.decode())
        self.assertTrue(writer.closed)

    async def test_outer_connection_error_is_handled(self):
        writer = FakeWriter()
        reader = LineReader(raise_exc=ConnectionResetError())
        await proxy.handle_control(proxy.ProxyRuntime(), reader, writer)
        self.assertTrue(writer.closed)


class InjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_raw_bytes(self):
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        await sess.inject_to_device(b"\xab\xcd")
        self.assertEqual(bytes(sess.dev_writer.buf), b"\xab\xcd")


class PumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_relays_verbatim_and_observes(self):
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        dst = FakeWriter()
        await sess._pump(FakeReader([DOOR_OPEN]), dst, "D->C")
        self.assertEqual(bytes(dst.buf), DOOR_OPEN)        # verbatim
        self.assertEqual(rt.state.doors[0x12], "open")     # tapped

    async def test_partial_frame_relayed_without_observe(self):
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        dst = FakeWriter()
        await sess._pump(FakeReader([b"\x7e\x1e\x09"]), dst, "D->C")   # no closing flag
        self.assertEqual(bytes(dst.buf), b"\x7e\x1e\x09")
        self.assertEqual(rt.state.doors, {})

    async def test_injects_automation_actions_to_device(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        sess = make_session(rt)
        dst = FakeWriter()                                            # the cloud-bound side
        await sess._pump(FakeReader([SCENE_PRESS]), dst, "D->C")
        self.assertEqual(bytes(dst.buf), SCENE_PRESS)                 # relayed verbatim to cloud
        self.assertGreater(len(sess.dev_writer.buf), 0)               # automation injected to device


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_unreachable_closes_device(self):
        rt = proxy.ProxyRuntime()
        dev_w = FakeWriter()
        sess = make_session(rt, dev_writer=dev_w)
        with mock.patch("asyncio.open_connection", new=mock.AsyncMock(side_effect=OSError("refused"))):
            await sess.run()
        self.assertTrue(dev_w.closed)
        self.assertEqual(rt.sessions, [])

    async def test_pump_exception_is_logged_and_cleaned_up(self):
        rt = proxy.ProxyRuntime()
        dev_w, cloud_w = FakeWriter(), FakeWriter()
        sess = make_session(rt, dev_reader=FakeReader(raise_exc=ValueError("boom")), dev_writer=dev_w)
        conn = mock.AsyncMock(return_value=(FakeReader(block=True), cloud_w))
        with mock.patch("asyncio.open_connection", new=conn):
            with self.assertLogs("hestia.proxy", level="WARNING") as cm:
                await sess.run()
        self.assertTrue(any("pump for" in line for line in cm.output))
        self.assertTrue(dev_w.closed and cloud_w.closed)
        self.assertEqual(rt.sessions, [])

    async def test_clean_disconnect_no_warning(self):
        rt = proxy.ProxyRuntime()
        dev_w, cloud_w = FakeWriter(), FakeWriter()
        sess = make_session(rt, dev_reader=FakeReader([]), dev_writer=dev_w)
        conn = mock.AsyncMock(return_value=(FakeReader([]), cloud_w))
        with mock.patch("asyncio.open_connection", new=conn):
            await sess.run()
        self.assertTrue(dev_w.closed and cloud_w.closed)
        self.assertEqual(rt.sessions, [])


DOOR_EVENT_NODE12 = build_frame(
    0x1E, 0x09,
    tlv(0x0047, b"\x12") + tlv(0x0046, bytes.fromhex("7105000000ff061600")),
)


class SchedulerIntervalTests(unittest.TestCase):
    def test_default_passthrough(self):
        self.assertEqual(proxy._scheduler_interval("20"), 20.0)

    def test_clamped_low_and_high(self):
        self.assertEqual(proxy._scheduler_interval("0.1"), 1.0)    # busy-loop floor
        self.assertEqual(proxy._scheduler_interval("300"), 59.0)   # miss-minute ceiling

    def test_non_numeric_falls_back(self):
        self.assertEqual(proxy._scheduler_interval("soon"), 20.0)

    def test_non_finite_falls_back(self):
        self.assertEqual(proxy._scheduler_interval("nan"), 20.0)
        self.assertEqual(proxy._scheduler_interval("inf"), 20.0)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    """The wall-clock scheduler injects due time-rule actions to the device session."""

    async def _tick(self, rt, now, settle=0.06):
        """Run _scheduler at a fixed clock for ~6 ticks (interval 0.01), then cancel. The
        first tick fires the due rule; slot-dedup makes every later tick a no-op, so the
        observable effect (inject / warning) happens exactly once."""
        task = asyncio.create_task(proxy._scheduler(rt, now=lambda: now, interval=0.01))
        await asyncio.sleep(settle)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    async def test_injects_due_rule_to_session(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_TIME_RULE))
        sess = FakeSession()
        rt.sessions.append(sess)
        await self._tick(rt, TIME_0730)
        self.assertEqual(len(sess.sent), 1)              # injected once (slot-deduped after)

    async def test_no_session_drops_and_warns(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_TIME_RULE))   # no session connected
        with self.assertLogs("hestia.proxy", level="WARNING") as logs:
            await self._tick(rt, TIME_0730)
        self.assertTrue(any("no device connected" in m for m in logs.output))

    async def test_inject_error_is_logged_and_loop_survives(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_TIME_RULE))
        rt.sessions.append(FakeSession(raise_exc=OSError("pipe")))
        with self.assertLogs("hestia.proxy", level="WARNING") as logs:
            task = await self._tick(rt, TIME_0730)
        self.assertTrue(any("scheduler inject failed" in m for m in logs.output))
        self.assertTrue(task.cancelled())               # survived the error, ended via our cancel


class SchedulerPresenceTests(unittest.IsolatedAsyncioTestCase):
    """The scheduler folds presence edges in alongside time/sun, with the not-empty guard placed
    AFTER on_presence (so presence fires on ticks where no time rule is due)."""

    MAC = "aa:bb:cc:dd:ee:ff"

    async def _run(self, rt, leases, settle=0.06):
        with mock.patch("hestia.proxy.read_present_macs", side_effect=leases) as m:
            task = asyncio.create_task(proxy._scheduler(rt, now=lambda: TIME_0730, interval=0.01))
            await asyncio.sleep(settle)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return m

    def _rt(self):
        rt = proxy.ProxyRuntime()                        # default engine; no time rule → on_time == []
        rt.engine.set_rule(Rule.from_dict(AUTO_PRESENCE_RULE))
        sess = FakeSession()
        rt.sessions.append(sess)
        return rt, sess

    async def test_arrival_injected_without_any_time_rule(self):
        rt, sess = self._rt()
        n = {"i": 0}

        def leases(*_a):                                 # tick 1: absent (baseline) → tick 2+: present
            n["i"] += 1
            return set() if n["i"] == 1 else {self.MAC}

        await self._run(rt, leases)
        self.assertEqual(len(sess.sent), 1)              # arrival edge fired once (guard is AFTER presence)

    async def test_no_presence_rule_skips_lease_read(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(AUTO_TIME_RULE))   # only a time rule → has_presence_rules False
        rt.sessions.append(FakeSession())
        m = await self._run(rt, leases=lambda *_a: {self.MAC})
        m.assert_not_called()                            # lease file never read when no presence rule

    async def test_unreadable_lease_survives_no_fire(self):
        rt, sess = self._rt()
        await self._run(rt, leases=lambda *_a: None)     # read_present_macs → None every tick
        self.assertEqual(sess.sent, [])                  # presence unknown → no fire, loop survives


CRIB_RULE = {       # global threshold rule the baby-monitor poller drives via on_global
    "id": "crib-hot",
    "trigger": {"type": "state", "field": "crib_temp", "op": "gt", "value": 24},
    "actions": [{"op": "switch", "node": 14, "on": True}],
}
OUTDOOR_RULE = {    # global threshold rule the weather poller drives via on_global
    "id": "cold",
    "trigger": {"type": "state", "field": "outdoor_temp", "op": "lt", "value": 0},
    "actions": [{"op": "switch", "node": 9, "on": True}],
}


class NianiaConfigTests(unittest.TestCase):
    def test_int_env(self):
        self.assertEqual(proxy._int_env("238", 0), 238)
        self.assertEqual(proxy._int_env("x", 238), 238)        # parse error → default
        self.assertEqual(proxy._int_env(None, 238), 238)       # unset → default

    def test_pos_float_env(self):
        self.assertEqual(proxy._pos_float_env("10", 1.0), 10.0)
        self.assertEqual(proxy._pos_float_env("x", 10.0), 10.0)    # parse error → default
        self.assertEqual(proxy._pos_float_env(None, 10.0), 10.0)   # unset → default
        self.assertEqual(proxy._pos_float_env("0", 10.0), 10.0)    # not > 0 → default
        self.assertEqual(proxy._pos_float_env("-5", 10.0), 10.0)   # negative → default
        self.assertEqual(proxy._pos_float_env("nan", 10.0), 10.0)  # non-finite → default

    def test_niania_interval(self):
        self.assertEqual(proxy._niania_interval("90"), 90.0)
        self.assertEqual(proxy._niania_interval("5"), 30.0)        # clamp to floor
        self.assertEqual(proxy._niania_interval("99999"), 3600.0)  # clamp to ceiling
        self.assertEqual(proxy._niania_interval("x"), 90.0)        # parse error → default
        self.assertEqual(proxy._niania_interval(None), 90.0)       # unset → default
        self.assertEqual(proxy._niania_interval("inf"), 90.0)      # non-finite → default

    def test_device_not_configured(self):
        with mock.patch.multiple(proxy, NIANIA_IP="", NIANIA_ID="", NIANIA_KEY=""):
            self.assertIsNone(proxy._niania_device())              # off → zero network

    def test_device_configured(self):
        with mock.patch.multiple(proxy, NIANIA_IP="192.0.2.19",
                                 NIANIA_ID="0123456789abcdefghijkl",
                                 NIANIA_KEY="0123456789abcdef", NIANIA_TEMP_DP=238):
            dev = proxy._niania_device()
            self.assertEqual(dev.ip, "192.0.2.19")
            self.assertEqual(dev.dps_to_request, [238])           # set to query only the temp DP

    def test_device_bad_key_disabled(self):
        with mock.patch.multiple(proxy, NIANIA_IP="1.2.3.4", NIANIA_ID="x", NIANIA_KEY="short"):
            with self.assertLogs("hestia.proxy", level="WARNING") as logs:
                self.assertIsNone(proxy._niania_device())         # TuyaError caught → None
        self.assertTrue(any("niania" in m for m in logs.output))


class NianiaPollerTests(unittest.IsolatedAsyncioTestCase):
    """The poller reads the crib temp off the loop (executor), writes State.crib_temp, and feeds
    on_global → inject. Configured via injected make_device/interval (module env is empty in tests)."""

    def _rt(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(CRIB_RULE))
        sess = FakeSession()
        rt.sessions.append(sess)
        return rt, sess

    async def _run(self, rt, make_device, settle=0.06):
        task = asyncio.create_task(proxy._niania_poller(rt, make_device=make_device, interval=0.01))
        await asyncio.sleep(settle)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    async def test_not_configured_noop(self):
        rt, sess = self._rt()
        task = await self._run(rt, make_device=lambda: None)      # no device → returns immediately
        self.assertTrue(task.done())
        self.assertIsNone(rt.state.crib_temp)
        self.assertEqual(sess.sent, [])

    async def test_reads_temp_and_fires_once(self):
        rt, sess = self._rt()
        dev = mock.Mock()
        dev.status.return_value = {"238": 256}                    # DP 238 ÷10 = 25.6 °C
        await self._run(rt, make_device=lambda: dev)
        self.assertEqual(rt.state.crib_temp, 25.6)
        self.assertEqual(len(sess.sent), 1)                       # 25.6 > 24 edge → fired once, then no re-edge

    async def test_retry_then_success(self):
        rt, sess = self._rt()
        dev = mock.Mock()
        dev.status.side_effect = [proxy.TuyaError("flaky"), {"238": 252}] + [{"238": 252}] * 30
        await self._run(rt, make_device=lambda: dev)
        self.assertEqual(rt.state.crib_temp, 25.2)               # second attempt within the tick won

    async def test_all_fail_keeps_last(self):
        rt, sess = self._rt()
        dev = mock.Mock()
        dev.status.side_effect = proxy.TuyaError("down")          # every attempt raises
        await self._run(rt, make_device=lambda: dev)
        self.assertIsNone(rt.state.crib_temp)                     # never set → last value kept
        self.assertEqual(sess.sent, [])

    async def test_non_numeric_dp_kept(self):
        rt, sess = self._rt()
        dev = mock.Mock()
        dev.status.return_value = {"238": "oops"}                 # present but non-numeric → failed attempt
        await self._run(rt, make_device=lambda: dev)
        self.assertIsNone(rt.state.crib_temp)
        self.assertEqual(sess.sent, [])

    async def test_non_finite_dp_rejected(self):
        # json.loads accepts NaN/Infinity — a non-finite DP must never poison crib_temp.
        for bad in (float("nan"), float("inf"), float("-inf")):
            rt, sess = self._rt()
            dev = mock.Mock()
            dev.status.return_value = {"238": bad}
            await self._run(rt, make_device=lambda: dev)
            self.assertIsNone(rt.state.crib_temp)                 # treated as a failed attempt
            self.assertEqual(sess.sent, [])

    async def test_no_session_logs_niania_source(self):
        # a crib rule fires but no device is connected → the drop is tagged "niania:", not "scheduler:".
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(CRIB_RULE))             # no session appended
        dev = mock.Mock()
        dev.status.return_value = {"238": 256}                    # 25.6 > 24 → on_global yields a frame
        with self.assertLogs("hestia.proxy", level="WARNING") as logs:
            await self._run(rt, make_device=lambda: dev)
        self.assertTrue(any("niania: dropping" in m for m in logs.output))
        self.assertFalse(any("scheduler: dropping" in m for m in logs.output))

    async def test_tick_error_survives(self):
        rt, sess = self._rt()
        dev = mock.Mock()
        dev.status.return_value = {"238": 256}
        with mock.patch.object(rt.engine, "on_global", side_effect=ValueError("boom")):
            with self.assertLogs("hestia.proxy", level="ERROR") as logs:
                task = await self._run(rt, make_device=lambda: dev)
        self.assertTrue(task.cancelled())                         # loop survived the tick error
        self.assertTrue(any("niania poller tick failed" in m for m in logs.output))


class OutdoorConfigTests(unittest.TestCase):
    def test_truthy_env_true_only_for_opt_in_tokens(self):
        for t in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(proxy._truthy_env(t), t)
        for f in (None, "", "   ", "0", "false", "off", "no", "nonsense"):
            self.assertFalse(proxy._truthy_env(f), f)               # unset/blank/false-ish → never enabled

    def test_clamp_secs(self):
        self.assertEqual(proxy._clamp_secs("600", 600.0, 60.0, 3600.0), 600.0)
        self.assertEqual(proxy._clamp_secs("5", 600.0, 60.0, 3600.0), 60.0)      # clamp to floor
        self.assertEqual(proxy._clamp_secs("99999", 600.0, 60.0, 3600.0), 3600.0)  # clamp to ceiling
        self.assertEqual(proxy._clamp_secs("x", 600.0, 60.0, 3600.0), 600.0)     # non-numeric → default
        self.assertEqual(proxy._clamp_secs(None, 600.0, 60.0, 3600.0), 600.0)    # unset → default
        self.assertEqual(proxy._clamp_secs("inf", 600.0, 60.0, 3600.0), 600.0)   # non-finite → default

    def test_niania_interval_delegates(self):
        self.assertEqual(proxy._niania_interval("90"), 90.0)
        self.assertEqual(proxy._niania_interval("5"), 30.0)        # niania floor differs (30)
        self.assertEqual(proxy._niania_interval("x"), 90.0)


class PollGlobalFieldTests(unittest.IsolatedAsyncioTestCase):
    """The generic global-field poller used by both the baby-monitor and weather pollers."""

    def _rt(self, with_session=True):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(OUTDOOR_RULE))
        sess = FakeSession() if with_session else None
        if sess is not None:
            rt.sessions.append(sess)
        return rt, sess

    async def _run(self, rt, read, source="weather", settle=0.06):
        task = asyncio.create_task(
            proxy._poll_global_field(rt, "outdoor_temp", read, 0.01, source))
        await asyncio.sleep(settle)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    async def test_success_sets_state_fires_injects(self):
        rt, sess = self._rt()
        await self._run(rt, read=lambda: -5.0)
        self.assertEqual(rt.state.outdoor_temp, -5.0)
        self.assertEqual(len(sess.sent), 1)                        # -5 < 0 edge → fired once
        self.assertTrue(rt.state.dirty)                            # global cached across a restart

    async def test_none_read_keeps_last(self):
        rt, sess = self._rt()
        await self._run(rt, read=lambda: None)
        self.assertIsNone(rt.state.outdoor_temp)
        self.assertEqual(sess.sent, [])
        self.assertFalse(rt.state.dirty)                           # nothing read → not dirtied

    async def test_tick_error_survives(self):
        rt, sess = self._rt()
        with mock.patch.object(rt.engine, "on_global", side_effect=ValueError("boom")):
            with self.assertLogs("hestia.proxy", level="ERROR") as logs:
                task = await self._run(rt, read=lambda: -5.0)
        self.assertTrue(task.cancelled())                          # survived the tick error
        self.assertTrue(any("weather poller tick failed" in m for m in logs.output))

    async def test_cancellation_propagates(self):
        rt, _ = self._rt()
        task = await self._run(rt, read=lambda: -5.0)
        self.assertTrue(task.cancelled())                          # CancelledError not swallowed

    async def test_source_tag_flows_to_inject(self):
        rt, _ = self._rt(with_session=False)                       # no device → drop is logged
        with self.assertLogs("hestia.proxy", level="WARNING") as logs:
            await self._run(rt, read=lambda: -5.0, source="testsrc")
        self.assertTrue(any("testsrc: dropping" in m for m in logs.output))

    async def test_publishes_globals_event_on_success(self):
        rt, _ = self._rt()
        sub = await rt.event_bus.try_subscribe()
        await self._run(rt, read=lambda: -5.0)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        gl = [e for e in events if e.get("type") == "globals"]
        self.assertTrue(gl)                                        # live dashboard delta published
        self.assertEqual(gl[0], {"type": "globals", "fields": {"outdoor_temp": -5.0}})

    async def test_no_globals_event_on_none_read(self):
        rt, _ = self._rt()
        sub = await rt.event_bus.try_subscribe()
        await self._run(rt, read=lambda: None)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        self.assertFalse([e for e in events if e.get("type") == "globals"])   # keep-last → no event


class WeatherPollerTests(unittest.IsolatedAsyncioTestCase):
    """The opt-in Open-Meteo poller — must do ZERO network unless enabled AND located."""

    async def _run(self, *, enabled=True, lat=52.2, lon=21.0, fetch=None, source="open-meteo",
                   settle=0.06):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(OUTDOOR_RULE))
        sess = FakeSession()
        rt.sessions.append(sess)
        fetch = fetch or mock.Mock(return_value=-5.0)
        task = asyncio.create_task(proxy._weather_poller(
            rt, fetch=fetch, lat=lat, lon=lon, interval=0.01, enabled=enabled, source=source))
        await asyncio.sleep(settle)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rt, sess, fetch, task

    async def test_disabled_does_no_fetch(self):
        rt, sess, fetch, task = await self._run(enabled=False)
        fetch.assert_not_called()
        self.assertTrue(task.done())                               # returned immediately
        self.assertIsNone(rt.state.outdoor_temp)

    async def test_source_local_does_no_fetch(self):
        _, _, fetch, _ = await self._run(source="local")           # mutual exclusion: 433 owns this source
        fetch.assert_not_called()

    async def test_lat_none_does_no_fetch(self):
        _, _, fetch, _ = await self._run(lat=None)
        fetch.assert_not_called()

    async def test_lon_none_does_no_fetch(self):
        _, _, fetch, _ = await self._run(lon=None)
        fetch.assert_not_called()

    async def test_enabled_and_located_fetches_and_fires(self):
        rt, sess, fetch, _ = await self._run()
        fetch.assert_called_with(52.2, 21.0)
        self.assertEqual(rt.state.outdoor_temp, -5.0)
        self.assertGreaterEqual(len(sess.sent), 1)

    async def test_fetch_none_keeps_last(self):
        rt, sess, fetch, _ = await self._run(fetch=mock.Mock(return_value=None))
        self.assertIsNone(rt.state.outdoor_temp)
        self.assertEqual(sess.sent, [])


class _FakeStream:
    """Fake ``sensor433.stream_readings``: records each call's kwargs, pushes the given readings, then
    either blocks (a live rtl_433 stream, until cancelled) or returns (rtl_433 exited → poller relaunches).
    With ``exc`` set, the FIRST call raises it (then later calls behave per ``behavior``)."""
    def __init__(self, readings=(), behavior="block", exc=None):
        self._readings = list(readings)
        self._behavior = behavior
        self._exc = exc
        self.calls = []

    async def __call__(self, on_reading, **kw):
        self.calls.append(kw)
        if self._exc is not None and len(self.calls) == 1:
            raise self._exc
        for r in self._readings:
            await on_reading(r)
        if self._behavior == "block":
            await asyncio.Event().wait()                           # live stream → runs until cancelled


class Sensor433PollerTests(unittest.IsolatedAsyncioTestCase):
    """The opt-in local-433 (rtl_433) PUSH streamer — ZERO work (no rtl_433 spawned) unless enabled AND
    source == 'local' (mutually exclusive with Open-Meteo). Consumes one long-lived stream, applies each
    reading as it arrives, and relaunches after a backoff if the stream exits."""

    async def _run(self, *, enabled=True, source="local", stream=None, settle=0.06, backoff=0.0,
                   model=None, sensor_id=None, device="rtl_tcp:127.0.0.1:1234"):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(OUTDOOR_RULE))
        sess = FakeSession()
        rt.sessions.append(sess)
        stream = stream if stream is not None else _FakeStream([sensor433.Reading(-5.0, 44.0)])
        task = asyncio.create_task(proxy._sensor433_poller(
            rt, stream=stream, enabled=enabled, source=source, device=device,
            model=model, sensor_id=sensor_id, protocol=None, backoff=backoff))
        await asyncio.sleep(settle)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return rt, sess, stream, task

    async def test_disabled_does_no_stream(self):
        rt, _, stream, task = await self._run(enabled=False)
        self.assertEqual(stream.calls, [])                         # no rtl_433 spawned
        self.assertTrue(task.done())                               # returned immediately
        self.assertIsNone(rt.state.outdoor_temp)
        self.assertIsNone(rt.state.outdoor_humidity)

    async def test_source_open_meteo_does_no_stream(self):
        _, _, stream, _ = await self._run(source="open-meteo")     # mutual exclusion: weather owns it
        self.assertEqual(stream.calls, [])

    async def test_unknown_source_does_no_stream(self):
        _, _, stream, _ = await self._run(source="bogus")          # fail-safe: unknown source -> off
        self.assertEqual(stream.calls, [])

    async def test_local_streams_sets_state_and_fires(self):
        rt, sess, stream, _ = await self._run(model="Prologue-TH", sensor_id="204")
        self.assertEqual(rt.state.outdoor_temp, -5.0)
        self.assertEqual(rt.state.outdoor_humidity, 44.0)
        self.assertTrue(rt.state.dirty)                            # globals cached across a restart
        self.assertGreaterEqual(len(sess.sent), 1)                 # -5 < 0 edge → fired
        self.assertEqual(stream.calls[0], {"device": "rtl_tcp:127.0.0.1:1234",
                                           "model": "Prologue-TH", "sensor_id": "204", "protocol": None})

    async def test_humidity_none_when_absent(self):
        rt, _, _, _ = await self._run(stream=_FakeStream([sensor433.Reading(-5.0, None)]))
        self.assertEqual(rt.state.outdoor_temp, -5.0)
        self.assertIsNone(rt.state.outdoor_humidity)

    async def test_publishes_globals_event_with_temp_and_humidity(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(OUTDOOR_RULE))
        sub = await rt.event_bus.try_subscribe()
        task = asyncio.create_task(proxy._sensor433_poller(
            rt, stream=_FakeStream([sensor433.Reading(-5.0, 44.0)]), enabled=True, source="local",
            device="d", model=None, sensor_id=None, protocol=None, backoff=0.0))
        await asyncio.sleep(0.06)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        events = []
        while not sub.queue.empty():
            events.append(sub.queue.get_nowait())
        gl = [e for e in events if e.get("type") == "globals"]
        self.assertTrue(gl)                                        # live dashboard delta published
        self.assertEqual(gl[0], {"type": "globals",
                                 "fields": {"outdoor_temp": -5.0, "outdoor_humidity": 44.0}})

    async def test_reading_error_does_not_drop_stream(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(OUTDOOR_RULE))
        rt.sessions.append(FakeSession())
        stream = _FakeStream([sensor433.Reading(-5.0, 44.0)])      # blocks (stays live) after the reading
        with mock.patch.object(rt.engine, "on_global", side_effect=ValueError("boom")):
            with self.assertLogs("hestia.proxy", level="ERROR") as logs:
                task = asyncio.create_task(proxy._sensor433_poller(
                    rt, stream=stream, enabled=True, source="local",
                    device="d", model=None, sensor_id=None, protocol=None, backoff=0.0))
                await asyncio.sleep(0.06)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any("sensor433 reading failed" in m for m in logs.output))
        self.assertEqual(rt.state.outdoor_temp, -5.0)              # state set before the failing inject
        self.assertEqual(len(stream.calls), 1)                     # stream NOT relaunched → still alive

    async def test_stream_exit_relaunches(self):
        stream = _FakeStream([sensor433.Reading(-5.0, 44.0)], behavior="return")
        _, _, stream, _ = await self._run(stream=stream, backoff=0.01)
        self.assertGreaterEqual(len(stream.calls), 2)              # exited → relaunched after backoff

    async def test_stream_error_logged_and_relaunches(self):
        rt = proxy.ProxyRuntime()
        rt.engine.set_rule(Rule.from_dict(OUTDOOR_RULE))
        rt.sessions.append(FakeSession())
        stream = _FakeStream([], behavior="block", exc=RuntimeError("rtl boom"))   # 1st call raises, 2nd blocks
        with self.assertLogs("hestia.proxy", level="ERROR") as logs:
            task = asyncio.create_task(proxy._sensor433_poller(
                rt, stream=stream, enabled=True, source="local",
                device="d", model=None, sensor_id=None, protocol=None, backoff=0.01))
            await asyncio.sleep(0.08)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(any("sensor433 stream failed" in m for m in logs.output))
        self.assertGreaterEqual(len(stream.calls), 2)              # relaunched after the error

    async def test_cancellation_propagates(self):
        _, _, _, task = await self._run()
        self.assertTrue(task.cancelled())                          # CancelledError not swallowed


class ProxyRuntimeDefaultsTests(unittest.TestCase):
    def test_has_classifier_and_registry(self):
        rt = proxy.ProxyRuntime()
        self.assertIsInstance(rt.classifier, proxy.Classifier)
        self.assertIsInstance(rt.registry, proxy.Registry)

    def test_lat_lon_default_to_module_constants(self):
        rt = proxy.ProxyRuntime()                       # env HESTIA_LAT/LON unset in tests → None
        self.assertEqual(rt.lat, proxy.HESTIA_LAT)
        self.assertEqual(rt.lon, proxy.HESTIA_LON)


class CoordTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(proxy._coord("51.5", -90, 90), 51.5)
        self.assertEqual(proxy._coord("-0.12", -180, 180), -0.12)

    def test_missing_is_none(self):
        self.assertIsNone(proxy._coord(None, -90, 90))

    def test_non_numeric_is_none(self):
        self.assertIsNone(proxy._coord("north", -90, 90))

    def test_out_of_range_is_none(self):
        self.assertIsNone(proxy._coord("91", -90, 90))
        self.assertIsNone(proxy._coord("-200", -180, 180))

    def test_non_finite_is_none(self):
        self.assertIsNone(proxy._coord("nan", -90, 90))
        self.assertIsNone(proxy._coord("inf", -180, 180))


class FeedDiscoveryTests(unittest.TestCase):
    def test_event_feeds_classifier_and_mirrors_registry(self):
        rt = proxy.ProxyRuntime()
        proxy._feed_discovery(rt, Frame(DOOR_EVENT_NODE12[1:-1]))
        self.assertEqual(rt.classifier.nodes[0x12].signals, {"door"})
        self.assertEqual(rt.registry.nodes["18"]["type"], "door")

    def test_roster_feeds_power_class(self):
        rt = proxy.ProxyRuntime()
        roster = build_frame(0x1E, 0x15, tlv(0x0001, b"\x00\x00") + tlv(0x004D, b"\x05\x00"))
        proxy._feed_discovery(rt, Frame(roster[1:-1]))
        self.assertEqual(rt.classifier.nodes[5].power, "mains")
        self.assertEqual(rt.registry.nodes["5"]["power"], "mains")

    def test_heartbeat_does_not_touch_registry(self):
        """A `[64 03]` heartbeat must not dirty or mirror — every minute would
        flood the autosave with no-op `last_seen` churn for every roster node."""
        with tempfile.TemporaryDirectory() as tmp:
            rt = proxy.ProxyRuntime(registry=proxy.Registry(Path(tmp) / "registry.json"))
            rt.registry.observe(5, "blind", "inferred"); rt.registry.save()
            before = dict(rt.registry.nodes["5"])
            proxy._feed_discovery(rt, Frame(build_frame(0x64, 0x03)[1:-1]))
            self.assertEqual(rt.registry.nodes["5"], before)
            self.assertFalse(rt.registry.dirty)

    def test_event_without_node_tlv_is_safe(self):
        """An event missing `0x0047` shouldn't crash and shouldn't mirror anything."""
        rt = proxy.ProxyRuntime()
        proxy._feed_discovery(rt, Frame(build_frame(0x1E, 0x09, tlv(0x0046, b"\x00"))[1:-1]))
        self.assertEqual(rt.registry.nodes, {})


class MergedDiscoveryTests(unittest.TestCase):
    def test_classifier_only(self):
        rt = proxy.ProxyRuntime()
        proxy._feed_discovery(rt, Frame(DOOR_EVENT_NODE12[1:-1]))
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["18"]["type"], "door")
        self.assertEqual(out["18"]["confidence"], "inferred")
        self.assertIsNone(out["18"]["battery"])             # no 80-03 → mains-style blank
        self.assertNotIn("name", out["18"])
        self.assertNotIn("room", out["18"])

    def test_battery_report_marks_power_battery(self):
        """A node that reports a battery level is battery-powered — even when the
        roster flag says mains (the thermostat case)."""
        rt = proxy.ProxyRuntime()
        roster = build_frame(0x1E, 0x15, tlv(0x0001, b"\x00\x00") + tlv(0x004D, b"\x0c\x00"))
        proxy._feed_discovery(rt, Frame(roster[1:-1]))      # roster: 0x0c flagged mains
        batt = build_frame(0x1E, 0x09, tlv(0x0047, b"\x0c") + tlv(0x0046, b"\x80\x03\x4a"))
        proxy._feed_discovery(rt, Frame(batt[1:-1]))        # 74 % battery report
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["12"]["battery"], 0x4a)
        self.assertEqual(out["12"]["power"], "battery")     # not "mains"

    def test_battery_falls_back_to_registry_after_restart(self):
        """Last-known battery (persisted) shows until the node next reports, so the
        column isn't blank right after a restart; power follows it."""
        rt = proxy.ProxyRuntime()
        rt.registry.observe(12, "thermostat", "inferred", power="mains", battery=74)
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["12"]["battery"], 74)
        self.assertEqual(out["12"]["power"], "battery")     # derived from persisted level

    def test_live_state_merged_into_discovery(self):
        """rt.state values land on the matching node entry — incl. the falsy
        traps (blind level 0, switch False) which must survive, not vanish."""
        rt = proxy.ProxyRuntime()
        for n, t in ((4, "blind"), (9, "thermostat"), (2, "light"), (17, "door")):
            rt.registry.observe(n, t, "inferred")            # put the node in the roster
        rt.state.levels[4] = 0                               # fully closed — the 0 trap
        rt.state.temperature[9] = 22
        rt.state.thermostat_setpoint[9] = 21
        rt.state.thermostat_on[9] = True
        rt.state.switches[2] = False                         # off — the False trap
        rt.state.doors[17] = "open"
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["4"]["level"], 0)
        self.assertEqual(out["9"]["temperature"], 22)
        self.assertEqual(out["9"]["setpoint"], 21)
        self.assertIs(out["9"]["thermostat_on"], True)
        self.assertIs(out["2"]["switch"], False)
        self.assertEqual(out["17"]["door"], "open")

    def test_live_state_null_when_unseen(self):
        """Every state key is always present (null when no report) — stable contract."""
        rt = proxy.ProxyRuntime()
        rt.registry.set_user(5, room="x")
        out = proxy._merged_discovery(rt)
        for k in ("level", "switch", "door", "setpoint", "thermostat_on", "temperature",
                  "power_w", "energy_kwh", "voltage_v", "endpoints"):
            self.assertIsNone(out["5"][k])

    def test_gang_endpoints_in_discovery(self):
        """A multi-gang switch exposes its per-endpoint on/off map."""
        rt = proxy.ProxyRuntime()
        rt.registry.observe(7, "light", "inferred")
        rt.state.gang[7] = {1: True, 2: False}
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["7"]["endpoints"], {1: True, 2: False})

    def test_plug_metering_in_discovery(self):
        """Plug power/energy/voltage from rt.state land on the node entry."""
        rt = proxy.ProxyRuntime()
        rt.registry.observe(19, "plug", "inferred")
        rt.state.plug_w[19] = 120
        rt.state.plug_kwh[19] = 8.75
        rt.state.plug_v[19] = 242.5
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["19"]["power_w"], 120)
        self.assertEqual(out["19"]["energy_kwh"], 8.75)
        self.assertEqual(out["19"]["voltage_v"], 242.5)

    def test_resolve_cloud_no_hostname_keeps_host(self):
        cfg = proxy.ProxyConfig(cloud_host="9.9.9.9")
        with mock.patch.object(proxy, "CLOUD_HOSTNAME", None):
            self.assertEqual(proxy._resolve_cloud(cfg), "9.9.9.9")     # literal pin — no resolution

    def test_resolve_cloud_hostname_resolves(self):
        cfg = proxy.ProxyConfig(cloud_host="9.9.9.9")                  # 9.9.9.9 = seed
        with mock.patch.object(proxy, "CLOUD_HOSTNAME", "gw.keemple.com"), \
             mock.patch.object(proxy.resolve, "resolve_cloud_ip", return_value="1.2.3.4") as r:
            self.assertEqual(proxy._resolve_cloud(cfg), "1.2.3.4")     # pure: returns, doesn't mutate
            r.assert_called_once_with("gw.keemple.com", seed="9.9.9.9", port=cfg.cloud_port)

    def test_endpoint_names_in_discovery(self):
        """Per-endpoint labels (registry) ride alongside the live endpoint states."""
        rt = proxy.ProxyRuntime()
        rt.registry.observe(7, "light", "inferred")
        rt.registry.set_user(7, ep=1, name="top")
        rt.state.gang[7] = {1: True, 2: False}
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["7"]["endpoints"], {1: True, 2: False})
        self.assertEqual(out["7"]["endpoint_names"], {"1": "top"})
        out["7"]["endpoint_names"]["1"] = "mutated"           # discovery output is a copy…
        self.assertEqual(rt.registry.nodes["7"]["endpoint_names"]["1"], "top")   # …registry untouched

    def test_scene_never_leaks_into_discovery(self):
        """A function-button press is a transient event — `scene`/`scene_seq` must
        never appear on a discovery entry (the dashboard treats it as a flash)."""
        rt = proxy.ProxyRuntime()
        rt.registry.observe(3, "light", "inferred")
        rt.state.scene_seq[3] = 2                          # as if a Central Scene was seen
        out = proxy._merged_discovery(rt)
        self.assertNotIn("scene", out["3"])
        self.assertNotIn("scene_seq", out["3"])

    def test_user_confirmed_type_wins(self):
        rt = proxy.ProxyRuntime()
        rt.registry.set_user(18, name="Room F", room="Room A", dtype="blind")
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["18"]["type"], "blind")
        self.assertEqual(out["18"]["confidence"], "confirmed")
        self.assertEqual(out["18"]["name"], "Room F")
        self.assertEqual(out["18"]["room"], "Room A")

    def test_registry_only_node(self):
        rt = proxy.ProxyRuntime()
        rt.registry.set_user(99, room="Room B")            # registry only
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["99"]["room"], "Room B")
        # neither side has a type → fall back to "unknown" (UI never sees null).
        self.assertEqual(out["99"]["type"], "unknown")
        self.assertEqual(out["99"]["confidence"], "unknown")

    def test_unknown_classifier_does_not_clobber_registry_type(self):
        """A roster-only classifier verdict of `unknown` must not erase a real
        type the registry already inferred in a previous session."""
        rt = proxy.ProxyRuntime()
        rt.registry.observe(18, "door", "inferred")                    # from a past run
        roster = build_frame(0x1E, 0x15, tlv(0x0001, b"\x00\x00") + tlv(0x004D, b"\x12\x01"))
        rt.classifier.ingest_roster(Frame(roster[1:-1]))               # classifier: unknown
        out = proxy._merged_discovery(rt)
        self.assertEqual(out["18"]["type"], "door")

    def test_bad_registry_key_is_skipped(self):
        """Hand-edited junk in `registry.json` shouldn't take down the discovery
        endpoint; the bad key is warned about and dropped."""
        rt = proxy.ProxyRuntime()
        rt.registry.nodes["gateway"] = {"type": "light"}
        with self.assertLogs("hestia.proxy", level="WARNING"):
            out = proxy._merged_discovery(rt)
        self.assertNotIn("gateway", out)

    def test_output_is_sorted_by_node_id(self):
        """Stable ordering for the UI (sorted by integer node id, not str)."""
        rt = proxy.ProxyRuntime()
        rt.registry.set_user(2, room="a")
        rt.registry.set_user(10, room="b")
        self.assertEqual(list(proxy._merged_discovery(rt)), ["2", "10"])


class DiscoveryOpTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_returns_devices(self):
        rt = proxy.ProxyRuntime()
        proxy._feed_discovery(rt, Frame(DOOR_EVENT_NODE12[1:-1]))
        resp = await proxy.process_control_op(rt, {"op": "discovery"})
        self.assertTrue(resp["ok"])
        self.assertIn("18", resp["devices"])
        self.assertEqual(resp["devices"]["18"]["type"], "door")


class NameOpTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "registry.json"
        return proxy.ProxyRuntime(registry=proxy.Registry(self.path))

    def tearDown(self):
        if hasattr(self, "tmp"):
            shutil.rmtree(self.tmp)

    async def test_name_persists(self):
        rt = self._runtime()
        resp = await proxy.process_control_op(rt, {"op": "name", "node": 5, "name": "Room A", "type": "blind"})
        self.assertTrue(resp["ok"])
        self.assertEqual(rt.registry.nodes["5"]["name"], "Room A")
        self.assertEqual(rt.registry.nodes["5"]["type"], "blind")
        self.assertTrue((self.tmp / "registry.json").exists())

    async def test_name_invalid_type_raises(self):
        rt = self._runtime()
        with self.assertRaises(ValueError):
            await proxy.process_control_op(rt, {"op": "name", "node": 5, "type": "bogus"})

    async def test_name_missing_node_via_execute_returns_error(self):
        rt = self._runtime()
        resp = await proxy.execute_control_line(rt, b'{"op":"name","name":"x"}\n')
        self.assertFalse(resp["ok"])

    async def test_name_missing_node_raises_value_error(self):
        """Direct call: a missing `node` is a contract violation, not a KeyError."""
        rt = self._runtime()
        with self.assertRaises(ValueError):
            await proxy.process_control_op(rt, {"op": "name", "name": "x"})

    async def test_name_save_failure_reported(self):
        """If the registry write fails, `name` op returns an explicit error
        instead of crashing the control connection."""
        rt = self._runtime()
        with mock.patch.object(rt.registry, "write_payload", side_effect=OSError("disk")):
            resp = await proxy.process_control_op(rt, {"op": "name", "node": 5, "name": "x"})
        self.assertFalse(resp["ok"])
        self.assertIn("registry save failed", resp["error"])
        self.assertTrue(rt.registry.dirty)            # restored for retry

    async def test_name_op_serialises_with_inflight_autosave(self):
        """The `save_lock` must prevent the name op's write from racing the
        autosave's in-flight write. Without the lock, two concurrent
        `os.replace` calls in the ThreadPoolExecutor would non-deterministically
        order, and a stale autosave snapshot could clobber the user edit."""
        rt = self._runtime()
        rt.registry.observe(5, "blind", "inferred")   # something for autosave to write

        loop = asyncio.get_running_loop()
        order: "list[str]" = []
        autosave_started = asyncio.Event()
        autosave_unblock = asyncio.Event()

        async def fake_run(_exec, func, *args):
            order.append("enter")
            if not autosave_started.is_set():
                autosave_started.set()
                await autosave_unblock.wait()         # hold the autosave write
            func(*args)
            order.append("exit")

        with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
            autosave_task = asyncio.create_task(proxy._autosave(rt, interval=0.005))
            await autosave_started.wait()
            # The name op must NOT proceed until the autosave write releases the lock.
            name_task = asyncio.create_task(
                proxy.process_control_op(rt, {"op": "name", "node": 5, "name": "Room A"})
            )
            await asyncio.sleep(0.02)
            self.assertEqual(order, ["enter"])        # name op is blocked on the lock
            autosave_unblock.set()
            resp = await asyncio.wait_for(name_task, timeout=2.0)
            autosave_task.cancel()
            await asyncio.gather(autosave_task, return_exceptions=True)

        self.assertTrue(resp["ok"])
        on_disk = proxy.Registry.load(self.path).nodes
        self.assertEqual(on_disk["5"]["name"], "Room A")   # user edit landed last


class AutosaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "registry.json"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    async def _wait_for(self, predicate, timeout=2.0):
        loop = asyncio.get_running_loop()
        start = loop.time()
        while not predicate():
            if loop.time() - start > timeout:
                return False
            await asyncio.sleep(0.005)
        return True

    async def test_saves_when_dirty(self):
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")                # marks dirty
        rt = proxy.ProxyRuntime(registry=reg)
        task = asyncio.create_task(proxy._autosave(rt, interval=0.02))
        try:
            self.assertTrue(await self._wait_for(self.path.exists))
            # Check dirty BEFORE cancel — cancellation during a future executor
            # await would restore dirty=True (`_persist`'s CancelledError handler
            # preserves operator intent), which is correct behaviour but races
            # the assertion.
            self.assertFalse(reg.dirty)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_skips_when_clean(self):
        reg = proxy.Registry(self.path)
        rt = proxy.ProxyRuntime(registry=reg)
        self.assertFalse(reg.dirty)
        task = asyncio.create_task(proxy._autosave(rt, interval=0.02))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.path.exists())

    async def test_survives_save_oserror_and_keeps_dirty(self):
        """A transient I/O failure must not kill the autosave loop — it logs and
        retries on the next tick (and leaves the registry dirty for that retry)."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")
        rt = proxy.ProxyRuntime(registry=reg)
        with mock.patch.object(reg, "write_payload", side_effect=OSError("disk full")):
            with self.assertLogs("hestia.proxy", level="ERROR"):
                task = asyncio.create_task(proxy._autosave(rt, interval=0.02))
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(reg.dirty)               # never successfully persisted
        self.assertTrue(task.cancelled())        # ended via our cancel, not a crash

    async def test_recovers_after_transient_failure(self):
        """After the I/O failure clears, the next tick must successfully persist."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")
        rt = proxy.ProxyRuntime(registry=reg)
        calls = {"n": 0}
        real_write = reg.write_payload

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            real_write(payload)

        with mock.patch.object(reg, "write_payload", side_effect=flaky):
            task = asyncio.create_task(proxy._autosave(rt, interval=0.02))
            try:
                self.assertTrue(await self._wait_for(self.path.exists))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertGreaterEqual(calls["n"], 2)   # retried after the failure
        self.assertFalse(reg.dirty)

    def test_seed_device_state_loads_cached_blob_when_present(self):
        rt = proxy.ProxyRuntime()
        with mock.patch("hestia.store_sql.load_device_state",
                        return_value={"switches": {"14": True}, "gang": {"7": {"1": False}}}):
            proxy.seed_device_state(rt)
        self.assertEqual(rt.state.switches, {14: True})
        self.assertEqual(rt.state.gang, {7: {1: False}})
        self.assertFalse(rt.state.dirty)

    def test_seed_device_state_ignores_absent_blob(self):
        rt = proxy.ProxyRuntime()
        with mock.patch("hestia.store_sql.load_device_state", return_value=None):
            proxy.seed_device_state(rt)
        self.assertEqual(rt.state.switches, {})

    async def test_persist_state_saves_and_clears_dirty(self):
        rt = proxy.ProxyRuntime()
        rt.state.switches[14] = True
        rt.state.dirty = True
        saved = []
        loop = asyncio.get_running_loop()

        async def fake_run(_executor, func):
            return func()

        with mock.patch("hestia.store_sql._settings_enabled", return_value=True):
            with mock.patch("hestia.store_sql.save_device_state",
                            side_effect=lambda snap: saved.append(snap) or True):
                with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
                    await proxy._persist_state(rt)
        self.assertFalse(rt.state.dirty)
        self.assertEqual(saved, [{"doors": {}, "levels": {}, "switches": {"14": True},
                                  "thermostat_setpoint": {}, "thermostat_on": {},
                                  "temperature": {}, "plug_w": {}, "plug_kwh": {},
                                  "plug_v": {}, "gang": {}}])

    async def test_persist_state_dirty_false_is_noop(self):
        rt = proxy.ProxyRuntime()
        loop = asyncio.get_running_loop()
        with mock.patch("hestia.store_sql._settings_enabled", return_value=True):
            with mock.patch.object(loop, "run_in_executor") as run:
                await proxy._persist_state(rt)
        run.assert_not_called()

    async def test_persist_state_json_mode_leaves_dirty_and_skips_executor(self):
        rt = proxy.ProxyRuntime()
        rt.state.dirty = True
        loop = asyncio.get_running_loop()
        with mock.patch("hestia.store_sql._settings_enabled", return_value=False):
            with mock.patch.object(loop, "run_in_executor") as run:
                await proxy._persist_state(rt)
        run.assert_not_called()
        self.assertTrue(rt.state.dirty)

    async def test_persist_state_save_false_rearms_dirty(self):
        rt = proxy.ProxyRuntime()
        rt.state.switches[14] = True
        rt.state.dirty = True
        loop = asyncio.get_running_loop()

        async def fake_run(_executor, func):
            return func()

        with mock.patch("hestia.store_sql._settings_enabled", return_value=True):
            with mock.patch("hestia.store_sql.save_device_state", return_value=False):
                with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
                    await proxy._persist_state(rt)
        self.assertTrue(rt.state.dirty)

    async def test_persist_state_executor_exception_rearms_dirty(self):
        rt = proxy.ProxyRuntime()
        rt.state.switches[14] = True
        rt.state.dirty = True
        loop = asyncio.get_running_loop()

        async def fake_run(_executor, _func):
            raise OSError("executor")

        with mock.patch("hestia.store_sql._settings_enabled", return_value=True):
            with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
                with self.assertLogs("hestia.proxy", level="ERROR"):
                    await proxy._persist_state(rt)
        self.assertTrue(rt.state.dirty)

    async def test_autosave_flushes_state_independently(self):
        rt = proxy.ProxyRuntime(registry=proxy.Registry(self.path))
        rt.state.switches[14] = True
        rt.state.dirty = True
        saved = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def fake_run(_executor, func):
            return func()

        def save(_snapshot):
            saved.set()
            return True

        with mock.patch("hestia.store_sql._settings_enabled", return_value=True):
            with mock.patch("hestia.store_sql.save_device_state", side_effect=save):
                with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
                    task = asyncio.create_task(proxy._autosave(rt, interval=0.02))
                    try:
                        self.assertTrue(await self._wait_for(saved.is_set))
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(rt.state.dirty)

    async def test_persist_store_writes_when_dirty(self):
        store_path = self.tmp / "automations.json"
        store = AutomationStore(store_path)
        store.set_rule(Rule.from_dict(AUTO_SCENE_RULE))    # marks dirty
        rt = proxy.ProxyRuntime(engine=AutomationEngine(store))
        await proxy._persist_store(rt)
        self.assertTrue(store_path.exists())
        self.assertFalse(store.dirty)

    async def test_autosave_flushes_store_independently(self):
        """Registry clean, automations store dirty: the store must still be flushed."""
        store_path = self.tmp / "automations.json"
        store = AutomationStore(store_path)
        store.set_rule(Rule.from_dict(AUTO_SCENE_RULE))
        rt = proxy.ProxyRuntime(registry=proxy.Registry(self.path),  # registry stays clean
                                engine=AutomationEngine(store))
        task = asyncio.create_task(proxy._autosave(rt, interval=0.02))
        try:
            self.assertTrue(await self._wait_for(store_path.exists))
            self.assertFalse(self.path.exists())           # registry never written (was clean)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_persist_is_a_noop_when_already_clean(self):
        """The simplest re-check case — a clean registry never writes."""
        reg = proxy.Registry(self.path)
        rt = proxy.ProxyRuntime(registry=reg)
        await proxy._persist(rt)
        self.assertFalse(self.path.exists())

    async def test_persist_skips_when_lock_wait_finds_clean(self):
        """The contention case the re-check actually exists for: a queued
        `_persist` must short-circuit when the writer ahead of us cleared
        `dirty` before releasing the lock — otherwise we'd write the same
        payload to disk twice for no reason."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")               # initially dirty
        rt = proxy.ProxyRuntime(registry=reg)
        await rt.save_lock.acquire()                       # simulate writer holding lock
        try:
            reg.dirty = False                              # …who just cleared dirty
            queued = asyncio.create_task(proxy._persist(rt))
            await asyncio.sleep(0)
            self.assertFalse(queued.done())                # blocked on the lock
        finally:
            rt.save_lock.release()
        await asyncio.wait_for(queued, timeout=1.0)
        self.assertFalse(self.path.exists())               # re-check skipped the write

    async def test_persist_waits_for_inflight_write_on_cancel(self):
        """A cancel mid-write must NOT abandon the in-flight executor thread: its
        atomic `os.replace` is awaited to completion UNDER the lock, so a later save
        cannot start a second, racing `os.replace` that reorders behind it and
        clobbers newer state. The write LANDS, so `dirty` stays clear (state is saved)
        before the cancellation propagates."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")
        rt = proxy.ProxyRuntime(registry=reg)
        loop = asyncio.get_running_loop()
        write_started = asyncio.Event()
        release = asyncio.Event()                          # the in-flight write lands when set

        async def fake_run(_executor, _func, *_args):
            write_started.set()
            await release.wait()

        with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
            task = asyncio.create_task(proxy._persist(rt))
            await write_started.wait()
            self.assertFalse(reg.dirty)                    # cleared before the await
            task.cancel()                                  # SIGTERM-style cancel mid-write
            for _ in range(3):
                await asyncio.sleep(0)                      # deliver + enter the wait-for-write loop
            self.assertFalse(task.done())                  # NOT done — still awaiting the write
            self.assertTrue(rt.save_lock.locked())         # lock held throughout the wait
            release.set()                                  # let the in-flight write land
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(reg.dirty)                        # write LANDED → state saved → dirty stays clear
        self.assertFalse(rt.save_lock.locked())            # `async with` released it

    async def test_persist_repeated_cancel_still_lands_write(self):
        """A second cancel DURING the cancel-wait must not detach the write — the
        loop keeps re-waiting until the executor thread's `os.replace` lands."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")
        rt = proxy.ProxyRuntime(registry=reg)
        loop = asyncio.get_running_loop()
        write_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_run(_executor, _func, *_args):
            write_started.set()
            await release.wait()

        with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
            task = asyncio.create_task(proxy._persist(rt))
            await write_started.wait()
            task.cancel()                                  # 1st cancel → enters the wait loop
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())                  # confirm we're IN the wait before re-cancel
            self.assertTrue(rt.save_lock.locked())
            task.cancel()                                  # 2nd cancel → interrupts the wait, re-remembered
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())                  # still waiting for the write
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(reg.dirty)                        # write landed → dirty clear
        self.assertFalse(rt.save_lock.locked())

    async def test_persist_cancel_beats_a_failing_write(self):
        """A write that FAILS while we wait it out post-cancel is absorbed + RETRIEVED
        (no 'exception never retrieved'), and the CANCELLATION still wins — so a
        cancelled caller (e.g. `_autosave`, whose `except OSError` treats a write error
        as retry-and-continue) terminates instead of swallowing the shutdown cancel."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")
        rt = proxy.ProxyRuntime(registry=reg)
        loop = asyncio.get_running_loop()
        controlled = loop.create_future()                  # we drive the "write" future directly
        handler_calls: "list[dict]" = []
        loop.set_exception_handler(lambda _l, ctx: handler_calls.append(ctx))

        with mock.patch.object(loop, "run_in_executor", side_effect=lambda *a: controlled):
            task = asyncio.create_task(proxy._persist(rt))
            await asyncio.sleep(0)                          # suspended in the write-wait, fut pending
            task.cancel()                                  # cancel → enter the wait loop (fut still pending)
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(task.done())                  # waiting the write out under the lock
            controlled.set_exception(OSError("disk"))       # the write FAILS mid-wait
            with self.assertRaises(asyncio.CancelledError): # cancellation WINS (not OSError)
                await task
        self.assertTrue(reg.dirty)                         # re-armed
        self.assertFalse(rt.save_lock.locked())            # `async with` released the lock
        self.assertEqual(handler_calls, [])                # ZERO loop-handler noise (no shield leak,
        #                                                    no unretrieved exception) — the write was consumed

    async def test_observe_during_save_is_not_clobbered(self):
        """Race: a new `observe()` arriving mid-write must NOT be lost. We clear
        `dirty` before dispatching the I/O, so a fresh `dirty=True` set by an
        `observe()` running during the await re-arms the next tick rather than
        being silently overwritten when the in-flight save finishes."""
        reg = proxy.Registry(self.path)
        reg.observe(5, "blind", "inferred")
        rt = proxy.ProxyRuntime(registry=reg)

        loop = asyncio.get_running_loop()
        writes: "list[bytes]" = []
        first_write_running = asyncio.Event()
        first_write_unblock = asyncio.Event()
        second_write_seen = asyncio.Event()

        async def fake_run(_executor, func, *args):
            writes.append(args[0] if args else b"")
            if len(writes) == 1:                      # hold the first call until
                first_write_running.set()             # the test injects the race
                await first_write_unblock.wait()
            else:
                second_write_seen.set()               # deterministic signal — no
            func(*args)                               # poll loop, no CI flakiness

        with mock.patch.object(loop, "run_in_executor", side_effect=fake_run):
            task = asyncio.create_task(proxy._autosave(rt, interval=0.005))
            await first_write_running.wait()          # autosave is mid-flight
            reg.observe(7, "blind", "inferred")       # injection during the await
            first_write_unblock.set()                 # let the first write complete
            await asyncio.wait_for(second_write_seen.wait(), timeout=2.0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(len(writes), 2)              # exactly one retry tick
        on_disk = proxy.Registry.load(self.path).nodes
        self.assertIn("5", on_disk)
        self.assertIn("7", on_disk)                   # racing observe persisted


# --- EventBus + Subscription ------------------------------------------------

class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_publish_receive(self):
        bus = proxy.EventBus(max_subs=2)
        sub = await bus.try_subscribe()
        self.assertIsNotNone(sub)
        bus.publish({"a": 1})
        self.assertEqual(await sub.queue.get(), {"a": 1})
        sub.close()

    async def test_cap_reached_returns_none(self):
        bus = proxy.EventBus(max_subs=1)
        first = await bus.try_subscribe()
        self.assertIsNotNone(first)
        self.assertIsNone(await bus.try_subscribe())   # cap full
        first.close()
        self.assertIsNotNone(await bus.try_subscribe())  # slot reclaimed

    async def test_closed_bus_rejects_subscribe(self):
        bus = proxy.EventBus(max_subs=2)
        bus.close()
        self.assertIsNone(await bus.try_subscribe())

    async def test_close_races_acquire(self):
        """If `close()` runs after `_sem.acquire()` but before the inner check,
        the second `_closing` check inside `try_subscribe` must release the
        semaphore and return None — otherwise we'd hand out a subscription
        the sentinel has missed."""
        bus = proxy.EventBus(max_subs=1)
        # Force the post-acquire branch by overriding _closing between acquire
        # and the check. We patch `_sem.acquire` to call close() once acquired.
        original_acquire = bus._sem.acquire

        async def acquire_then_close():
            await original_acquire()
            bus._closing = True                        # simulate race

        with mock.patch.object(bus._sem, "acquire", side_effect=acquire_then_close):
            self.assertIsNone(await bus.try_subscribe())
        # the semaphore should be back to its full value (released after the race)
        self.assertFalse(bus._sem.locked())

    async def test_publish_drops_on_full_queue(self):
        bus = proxy.EventBus(max_subs=2)
        sub = await bus.try_subscribe(maxsize=1)
        bus.publish({"keep": True})                    # fills the queue
        bus.publish({"drop": True})                    # dropped silently
        self.assertEqual(await sub.queue.get(), {"keep": True})
        sub.close()

    async def test_publish_after_close_is_noop(self):
        bus = proxy.EventBus(max_subs=2)
        sub = await bus.try_subscribe(maxsize=2)
        bus.close()
        bus.publish({"after": True})                   # must not raise, must not enqueue
        # the sentinel was already pushed; nothing else
        self.assertIs(await sub.queue.get(), proxy._CLOSED_SENTINEL)
        sub.close()

    async def test_close_forces_sentinel_through_full_queue(self):
        """A subscriber whose queue is full at shutdown must still see the
        sentinel — `close()` drains one stale event to make room."""
        bus = proxy.EventBus(max_subs=2)
        sub = await bus.try_subscribe(maxsize=1)
        bus.publish({"stale": True})                   # fills queue (1/1)
        bus.close()                                    # forces sentinel
        first = await sub.queue.get()
        self.assertIs(first, proxy._CLOSED_SENTINEL)   # stale dropped, sentinel reached us
        sub.close()

    async def test_close_handles_concurrently_emptied_queue(self):
        """If the close-helper finds the queue empty after the QueueFull check
        (racy drain), it gives up gracefully without crashing."""
        bus = proxy.EventBus(max_subs=2)
        sub = await bus.try_subscribe(maxsize=1)

        # craft a queue that always raises QueueFull on put_nowait and
        # QueueEmpty on get_nowait — both sentinel paths exercised at once
        class _ImpossibleQueue:
            def put_nowait(self, _item): raise asyncio.QueueFull
            def get_nowait(self): raise asyncio.QueueEmpty
        bus._subs.clear(); bus._subs.add(_ImpossibleQueue())
        bus.close()                                    # must return cleanly
        self.assertTrue(bus._closing)
        sub.close()


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_is_idempotent(self):
        bus = proxy.EventBus(max_subs=2)
        sub = await bus.try_subscribe()
        sub.close()
        sub.close()                                    # no AttributeError, no double-release
        # If the close were not idempotent, the semaphore would over-release.
        # Verify by taking the slot back and immediately releasing again.
        sub2 = await bus.try_subscribe()
        self.assertIsNotNone(sub2)
        sub2.close()


# --- discovery_changed / activity publishing --------------------------------

def _level_event(node, value):
    return Frame(build_frame(0x1E, 0x09,
                 tlv(0x0047, bytes([node])) + tlv(0x0046, bytes([0x26, 0x03, value, 0x00, 0xFE])))[1:-1])


def _scene_event(node, data):
    return Frame(build_frame(0x1E, 0x09, tlv(0x0047, bytes([node])) + tlv(0x0046, data))[1:-1])


class DiscoveryChangedHookTests(unittest.IsolatedAsyncioTestCase):
    def test_feed_discovery_returns_true_on_identity_change(self):
        """A new node / (re)classification returns True (caller → discovery_changed);
        an identical re-observe returns False. `_feed_discovery` no longer publishes."""
        rt = proxy.ProxyRuntime()
        self.assertTrue(proxy._feed_discovery(rt, Frame(DOOR_EVENT_NODE12[1:-1])))   # new node 0x12
        self.assertFalse(proxy._feed_discovery(rt, Frame(DOOR_EVENT_NODE12[1:-1])))  # same → no change

    def test_feed_discovery_false_on_heartbeat(self):
        rt = proxy.ProxyRuntime()
        self.assertFalse(proxy._feed_discovery(rt, Frame(build_frame(0x64, 0x03)[1:-1])))

    async def test_observe_state_delta_when_identity_unchanged(self):
        """A live value change on an already-classified node publishes a cheap
        `state` delta (+ activity), NOT a full `discovery_changed`."""
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        sub = await rt.event_bus.try_subscribe()
        try:
            sess._observe(_level_event(0x05, 0x10), "D->C")     # new blind → discovery_changed + activity
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            sess._observe(_level_event(0x05, 0x30), "D->C")     # level changed, identity same
            got = {(await asyncio.wait_for(sub.queue.get(), timeout=1.0))["type"]: True for _ in range(2)}
            self.assertEqual(set(got), {"state", "activity"})
        finally:
            sub.close()

    async def test_observe_duplicate_event_only_activity(self):
        """An identical repeat (same value) publishes only `activity` — no refetch,
        no state delta."""
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        sub = await rt.event_bus.try_subscribe()
        try:
            sess._observe(_level_event(0x05, 0x30), "D->C")     # first → discovery_changed + activity
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            sess._observe(_level_event(0x05, 0x30), "D->C")     # identical → only activity
            e = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(e["type"], "activity")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        finally:
            sub.close()

    async def test_observe_scene_press_rides_activity_no_state_delta(self):
        """A function-button press on an already-classified node publishes `activity`
        carrying the scene, and NO `state` delta (scene is popped — an event, not state)."""
        rt = proxy.ProxyRuntime()
        sess = make_session(rt)
        sub = await rt.event_bus.try_subscribe()
        try:
            sess._observe(_level_event(0x05, 0x10), "D->C")     # classify node → discovery_changed + activity
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            sess._observe(_scene_event(0x05, b"\x2b\x01\x02\x00"), "D->C")   # function button
            e = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(e["type"], "activity")
            self.assertEqual(e["scene"], {"id": 2, "kind": "scene"})
            with self.assertRaises(asyncio.TimeoutError):        # no state delta follows
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        finally:
            sub.close()

    async def test_name_op_publishes_discovery_changed(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            rt = proxy.ProxyRuntime(registry=proxy.Registry(tmp / "r.json"))
            sub = await rt.event_bus.try_subscribe()
            try:
                await proxy.process_control_op(rt, {"op": "name", "node": 5, "name": "x"})
                event = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
                self.assertEqual(event, {"type": "discovery_changed"})
            finally:
                sub.close()
        finally:
            shutil.rmtree(tmp)

    async def test_name_op_with_endpoint_routes_to_endpoint_names(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            rt = proxy.ProxyRuntime(registry=proxy.Registry(tmp / "r.json"))
            await proxy.process_control_op(rt, {"op": "name", "node": 7, "ep": 2, "name": "channel"})
            self.assertEqual(rt.registry.nodes["7"]["endpoint_names"], {"2": "channel"})
        finally:
            shutil.rmtree(tmp)

    async def test_name_op_bad_endpoint_raises(self):
        rt = proxy.ProxyRuntime()
        with self.assertRaises(ValueError):
            await proxy.process_control_op(rt, {"op": "name", "node": 7, "ep": -1, "name": "x"})


class ActivityHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_observe_event_publishes_activity(self):
        rt = proxy.ProxyRuntime()
        sub = await rt.event_bus.try_subscribe()
        try:
            sess = make_session(rt)
            sess._observe(Frame(DOOR_EVENT_NODE12[1:-1]), "D->C")
            # We expect TWO events: activity (from the hook) and
            # discovery_changed (from _feed_discovery). Order: activity is
            # published AFTER feed_discovery, so order is: discovery_changed,
            # then activity.
            first = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            second = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            kinds = sorted([first["type"], second["type"]])
            self.assertEqual(kinds, ["activity", "discovery_changed"])
            activity = first if first["type"] == "activity" else second
            self.assertEqual(activity["node"], 0x12)
            self.assertIn("ts", activity)
        finally:
            sub.close()

    async def test_observe_noise_does_not_publish_activity(self):
        rt = proxy.ProxyRuntime()
        sub = await rt.event_bus.try_subscribe()
        try:
            sess = make_session(rt)
            sess._observe(Frame(NOISE[1:-1]), "C->D")     # [66 01] hello — no event
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        finally:
            sub.close()

    async def test_event_without_node_tlv_no_activity(self):
        """An `[1e 09]` lacking `0x0047` is a malformed event — no activity
        published, but `_feed_discovery` still runs as before."""
        rt = proxy.ProxyRuntime()
        sub = await rt.event_bus.try_subscribe()
        try:
            sess = make_session(rt)
            sess._observe(Frame(build_frame(0x1E, 0x09, tlv(0x0046, b"\x00"))[1:-1]), "D->C")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        finally:
            sub.close()


class GraduateOpTests(unittest.IsolatedAsyncioTestCase):
    """Phase-3 `graduate` op: atomic under save_lock, persists standalone to disk BEFORE flipping the
    in-memory mode (no false success / dishonest target_mode), idempotent, applied on the next restart."""

    async def test_persists_then_publishes_standalone(self):
        rt = proxy.ProxyRuntime()
        with mock.patch.object(rt.registry, "write_payload") as wp:
            resp = await proxy.process_control_op(rt, {"op": "graduate"})
        self.assertEqual(resp, {"ok": True, "mode": "standalone", "restart_required": True})
        self.assertEqual(rt.registry.mode, "standalone")      # flipped only after the durable write
        wp.assert_called_once()
        self.assertIn(b'"mode": "standalone"', wp.call_args.args[0])   # the written payload carried standalone

    async def test_idempotent_running_standalone_no_restart(self):
        rt = proxy.ProxyRuntime(mode="standalone")            # already running standalone
        rt.registry.set_mode("standalone")
        with mock.patch.object(rt.registry, "write_payload") as wp:
            resp = await proxy.process_control_op(rt, {"op": "graduate"})
        self.assertEqual(resp, {"ok": True, "mode": "standalone", "restart_required": False})
        wp.assert_not_called()                                # already standalone → no re-write

    async def test_stays_proxy_on_persist_error(self):
        rt = proxy.ProxyRuntime()
        with mock.patch.object(rt.registry, "write_payload", side_effect=OSError("disk full")):
            resp = await proxy.process_control_op(rt, {"op": "graduate"})
        self.assertFalse(resp["ok"])
        self.assertIn("disk full", resp["error"])
        self.assertEqual(rt.registry.mode, "proxy")           # persist-before-publish → never falsely standalone


class TermHandlerTests(unittest.TestCase):
    """proxy._install_term_handler — routes SIGTERM into the same graceful, SIGINT-shaped
    unwind (cancel the main task → the persist-on-exit finally runs)."""

    def test_installs_sigterm_to_cancel(self):
        loop, task = mock.Mock(), mock.Mock()
        self.assertTrue(proxy._install_term_handler(loop, task))
        loop.add_signal_handler.assert_called_once_with(signal.SIGTERM, task.cancel)

    def test_unsupported_platform_returns_false(self):
        loop, task = mock.Mock(), mock.Mock()
        loop.add_signal_handler.side_effect = NotImplementedError   # Windows / non-main thread
        self.assertFalse(proxy._install_term_handler(loop, task))
        task.cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
