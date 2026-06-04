"""Unit + loopback tests for the standalone server (hestia.server) and the
mode dispatch (hestia.__main__)."""
from __future__ import annotations

import asyncio
import unittest

from hestia import __main__ as entry
from hestia import proxy, server
from hestia.automations import Rule
from hestia.protocol import Deframer, Frame, build_frame, iter_frames, tlv
from hestia.proxy import ProxyConfig, ProxyRuntime
from hestia.state import tlv_value

HELLO = build_frame(0x66, 0x01)
TICK = build_frame(0x00, 0x00)
DOOR_EVENT = build_frame(
    0x1E, 0x09,
    tlv(0x0047, b"\x12") + tlv(0x0046, bytes.fromhex("7105000000ff061600")) + tlv(0x001F, b"\x00\xb4"),
)


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


class FakeReader:
    def __init__(self, chunks=None, raise_exc=None):
        self._chunks = list(chunks or [])
        self._raise = raise_exc

    async def read(self, _n=4096):
        if self._raise is not None:
            raise self._raise
        return self._chunks.pop(0) if self._chunks else b""


def make_session(rt, chunks=None, raise_exc=None, heartbeat_secs=60.0):
    return server.StandaloneSession(rt, FakeReader(chunks, raise_exc), FakeWriter(), heartbeat_secs)


async def read_frame(reader, timeout=2.0):
    deframer = Deframer()
    while True:
        data = await asyncio.wait_for(reader.read(4096), timeout)
        if not data:
            raise EOFError("connection closed")
        for body in deframer.feed(data):
            return body


class FrameBuilderTests(unittest.TestCase):
    def test_session_assign(self):
        frame = Frame(server.make_session_assign()[1:-1])
        self.assertEqual((frame.type, frame.cmd), (0x64, 0x01))
        self.assertTrue(frame.checksum_ok)
        tags = {t.tag for t in frame.tlvs()}
        self.assertLessEqual({0x0064, 0x001F, 0x0069}, tags)

    def test_timestamp(self):
        frame = Frame(server.make_timestamp()[1:-1])
        self.assertEqual((frame.type, frame.cmd), (0x64, 0x03))
        self.assertTrue(frame.checksum_ok)
        self.assertLessEqual({0x0066, 0x0068, 0x00D3}, {t.tag for t in frame.tlvs()})


class ReactTests(unittest.TestCase):
    def setUp(self):
        self.sess = make_session(ProxyRuntime())

    def test_hello_assigns_session_once(self):
        first = self.sess.react(Frame(HELLO[1:-1]))
        self.assertEqual(len(first), 1)
        self.assertEqual(Frame(first[0][1:-1]).cmd, 0x01)
        self.assertEqual(self.sess.react(Frame(HELLO[1:-1])), [])   # second hello → silent

    def test_event_acks_with_seq(self):
        replies = self.sess.react(Frame(DOOR_EVENT[1:-1]))
        ack = Frame(replies[0][1:-1])
        self.assertEqual((ack.type, ack.cmd), (0x1E, 0x0A))
        self.assertEqual(tlv_value(ack, 0x001F), b"\x00\xb4")

    def test_event_without_seq_no_reply(self):
        ev = build_frame(0x1E, 0x09, tlv(0x0047, b"\x12") + tlv(0x0046, b"\x26\x03\x10\x00\xfe"))
        self.assertEqual(self.sess.react(Frame(ev[1:-1])), [])

    def test_ip_report_ack(self):
        ip = build_frame(0x67, 0x01, tlv(0x0011, b"192.0.2.17"))
        self.assertEqual(Frame(self.sess.react(Frame(ip[1:-1]))[0][1:-1]).cmd, 0x02)

    def test_periodic_report_ack(self):
        rep = build_frame(0x67, 0x03, tlv(0x003F, b"\x00"))
        self.assertEqual(Frame(self.sess.react(Frame(rep[1:-1]))[0][1:-1]).cmd, 0x04)

    def test_other_frame_no_reply(self):
        self.assertEqual(self.sess.react(Frame(TICK[1:-1])), [])


class ObserveTests(unittest.TestCase):
    def test_short_frame_ignored(self):
        rt = ProxyRuntime()
        make_session(rt)._observe(Frame(b"\x66"))
        self.assertEqual(rt.state.doors, {})

    def test_noise_frame_no_state(self):
        rt = ProxyRuntime()
        make_session(rt)._observe(Frame(HELLO[1:-1]))
        self.assertEqual(rt.state.doors, {})

    def test_event_updates_state(self):
        rt = ProxyRuntime()
        make_session(rt)._observe(Frame(DOOR_EVENT[1:-1]))
        self.assertEqual(rt.state.doors[0x12], "open")

    def test_bad_checksum_skipped(self):
        rt = ProxyRuntime()
        body = bytearray(DOOR_EVENT[1:-1])
        body[-1] ^= 0xFF
        make_session(rt)._observe(Frame(bytes(body)))
        self.assertEqual(rt.state.doors, {})


class SceneReplayTests(unittest.TestCase):
    """Standalone-only: a function-button press replays the cloud's learned batch
    reaction (§5.7a), since there is no cloud to run the scene."""

    SWITCH_PRESS = build_frame(0x1E, 0x09, tlv(0x0047, b"\x02") + tlv(0x0046, b"\x2b\x01\x03\x00"))
    ELEMENTS = bytes.fromhex("0101030626010002010308260100")

    def test_replay_injects_learned_batch_with_fresh_seq(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(2, 3, self.ELEMENTS.hex())
        replay = make_session(rt)._observe(Frame(self.SWITCH_PRESS[1:-1]))
        self.assertEqual(len(replay), 1)
        f = Frame(replay[0][1:-1])
        self.assertEqual((f.type, f.cmd), (0x1E, 0x32))
        self.assertTrue(f.checksum_ok)
        self.assertEqual(tlv_value(f, 0x005A), self.ELEMENTS)        # batch replayed verbatim
        self.assertNotIn(0x7E, tlv_value(f, 0x001F))                 # FLAG-safe fresh seq

    def test_no_replay_without_learned_batch(self):
        rt = ProxyRuntime()
        self.assertEqual(make_session(rt)._observe(Frame(self.SWITCH_PRESS[1:-1])), [])

    def test_non_scene_event_no_replay(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(2, 3, self.ELEMENTS.hex())          # node 2 has a scene...
        lvl = build_frame(0x1E, 0x09,                                # ...but this is a level report
              tlv(0x0047, b"\x02") + tlv(0x0046, bytes([0x26, 0x03, 0x10, 0x00, 0xFE])))
        self.assertEqual(make_session(rt)._observe(Frame(lvl[1:-1])), [])

    def test_short_and_bad_checksum_return_empty(self):
        rt = ProxyRuntime()
        self.assertEqual(make_session(rt)._observe(Frame(b"\x1e")), [])    # short → []
        body = bytearray(self.SWITCH_PRESS[1:-1])
        body[-1] ^= 0xFF
        self.assertEqual(make_session(rt)._observe(Frame(bytes(body))), [])  # bad checksum → []

    def test_corrupt_learned_hex_skips_replay(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(2, 3, "zz")                # invalid hex (hand-edited registry)
        with self.assertLogs("hestia.server", level="ERROR"):
            replay = make_session(rt)._observe(Frame(self.SWITCH_PRESS[1:-1]))
        self.assertEqual(replay, [])                         # session survives, just no replay

    def test_oversized_learned_batch_skips_replay(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(2, 3, "00" * 70000)        # valid hex but > 64KB → TLV len overflows
        with self.assertLogs("hestia.server", level="ERROR"):
            replay = make_session(rt)._observe(Frame(self.SWITCH_PRESS[1:-1]))
        self.assertEqual(replay, [])                         # OverflowError caught, session survives

    def test_non_string_learned_batch_skips_replay(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(2, 3, 123)                 # non-string (hand-edited JSON number)
        with self.assertLogs("hestia.server", level="ERROR"):
            replay = make_session(rt)._observe(Frame(self.SWITCH_PRESS[1:-1]))
        self.assertEqual(replay, [])                         # TypeError caught, session survives

    def test_central_scene_dedup_replays_once(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(5, 1, self.ELEMENTS.hex())
        blind = build_frame(0x1E, 0x09, tlv(0x0047, b"\x05") + tlv(0x0046, b"\x5b\x03\x02\x80\x01"))
        sess = make_session(rt)
        self.assertEqual(len(sess._observe(Frame(blind[1:-1]))), 1)  # first press replays
        self.assertEqual(sess._observe(Frame(blind[1:-1])), [])      # repeated seq deduped → no replay


class SceneReplayRunOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_precedes_replayed_batch(self):
        rt = ProxyRuntime()
        rt.registry.record_scene(2, 3, bytes.fromhex("0101030626010002010308260100").hex())
        press = build_frame(0x1E, 0x09, tlv(0x0047, b"\x02")
                + tlv(0x0046, b"\x2b\x01\x03\x00") + tlv(0x001F, b"\x00\x7a"))
        sess = make_session(rt, chunks=[press])
        await sess.run()
        kinds = [(Frame(b).type, Frame(b).cmd) for b in iter_frames(bytes(sess.writer.buf))]
        self.assertLess(kinds.index((0x1E, 0x0A)), kinds.index((0x1E, 0x32)))   # ACK before batch


class StandaloneEngineTests(unittest.TestCase):
    """Standalone runs automations too; engine frames append after any scene-replay."""

    SCENE_PRESS = build_frame(0x1E, 0x09, tlv(0x0047, b"\x02") + tlv(0x0046, b"\x2b\x01\x03\x00"))
    RULE = {
        "id": "a1",
        "trigger": {"type": "scene", "node": 2, "scene_id": 3},
        "actions": [{"op": "switch", "node": 14, "on": True}],
    }

    def _rt(self):
        rt = ProxyRuntime()
        rt.mode = "standalone"
        rt.engine.set_rule(Rule.from_dict(self.RULE))
        return rt

    def test_observe_appends_automation_frame(self):
        rt = self._rt()                                              # no learned scene batch
        out = make_session(rt)._observe(Frame(self.SCENE_PRESS[1:-1]))
        self.assertEqual(len(out), 1)                               # just the automation action

    def test_scene_replay_then_automation(self):
        rt = self._rt()
        rt.registry.record_scene(2, 3, bytes.fromhex("0101030626010002010308260100").hex())
        out = make_session(rt)._observe(Frame(self.SCENE_PRESS[1:-1]))
        self.assertEqual(len(out), 2)                               # batch replay THEN automation
        first = Frame(out[0][1:-1])
        self.assertEqual((first.type, first.cmd), (0x1E, 0x32))     # scene-replay batch comes first


class StandaloneCommandEchoTests(unittest.IsolatedAsyncioTestCase):
    """Standalone learns switch/2-gang state from the commands it SENDS — control / scheduler via
    inject_to_device, a fired automation via _write_replies. Those relays never report; the echo is
    post-send. react ACKs / scene batches are not switch commands → not echoed."""

    @staticmethod
    def _drain(sub):
        out = []
        while not sub.queue.empty():
            out.append(sub.queue.get_nowait())
        return out

    async def test_inject_to_device_echoes_switch(self):
        rt = ProxyRuntime()
        sess = make_session(rt)
        sub = await rt.event_bus.try_subscribe()
        await sess.inject_to_device(proxy.build_command(rt, {"op": "switch", "node": 0x0E, "on": True}))
        self.assertIn({"type": "state", "node": 0x0E, "fields": {"switch": True}}, self._drain(sub))
        self.assertIs(rt.state.switches[0x0E], True)

    async def test_write_replies_echoes_only_the_fired_switch(self):
        rt = ProxyRuntime()
        sess = make_session(rt)
        sub = await rt.event_bus.try_subscribe()
        switch_cmd = proxy.build_command(rt, {"op": "switch", "node": 0x0E, "on": False})
        ack = build_frame(0x1E, 0x0A, tlv(0x001F, b"\x00\x01"))          # a react ACK — must NOT echo
        await sess._write_replies([ack, switch_cmd])
        states = [e for e in self._drain(sub) if e.get("type") == "state"]
        self.assertEqual(states, [{"type": "state", "node": 0x0E, "fields": {"switch": False}}])
        self.assertIs(rt.state.switches[0x0E], False)


class StandaloneActivityHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_observe_event_publishes_activity(self):
        rt = ProxyRuntime()
        sub = await rt.event_bus.try_subscribe()
        try:
            make_session(rt)._observe(Frame(DOOR_EVENT[1:-1]))
            kinds = []
            for _ in range(2):
                kinds.append((await asyncio.wait_for(sub.queue.get(), timeout=1.0))["type"])
            self.assertEqual(sorted(kinds), ["activity", "discovery_changed"])
        finally:
            sub.close()

    async def test_observe_state_delta_when_identity_unchanged(self):
        """Standalone mirror: a live value change on an already-classified node
        publishes a cheap `state` delta (+ activity), not `discovery_changed`."""
        rt = ProxyRuntime()
        sess = make_session(rt)
        lvl = lambda v: Frame(build_frame(0x1E, 0x09,
              tlv(0x0047, b"\x05") + tlv(0x0046, bytes([0x26, 0x03, v, 0x00, 0xFE])))[1:-1])
        sub = await rt.event_bus.try_subscribe()
        try:
            sess._observe(lvl(0x10))                 # new blind → discovery_changed + activity
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            sess._observe(lvl(0x30))                 # level changed, identity same → state + activity
            kinds = {(await asyncio.wait_for(sub.queue.get(), timeout=1.0))["type"] for _ in range(2)}
            self.assertEqual(kinds, {"state", "activity"})
        finally:
            sub.close()

    async def test_observe_scene_press_rides_activity_no_state_delta(self):
        """Standalone mirror of the proxy: a function-button press publishes `activity`
        with the scene and NO `state` delta."""
        rt = ProxyRuntime()
        sess = make_session(rt)
        lvl = Frame(build_frame(0x1E, 0x09,
              tlv(0x0047, b"\x05") + tlv(0x0046, bytes([0x26, 0x03, 0x10, 0x00, 0xFE])))[1:-1])
        scene = Frame(build_frame(0x1E, 0x09,
                tlv(0x0047, b"\x05") + tlv(0x0046, b"\x5b\x03\x02\x80\x01"))[1:-1])
        sub = await rt.event_bus.try_subscribe()
        try:
            sess._observe(lvl)                       # classify node → discovery_changed + activity
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            sess._observe(scene)                     # blind function button (Central Scene)
            e = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
            self.assertEqual(e["type"], "activity")
            self.assertEqual(e["scene"], {"id": 1, "kind": "central"})
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        finally:
            sub.close()

    async def test_observe_event_without_node_no_activity(self):
        """`[1e 09]` without `0x0047` — guard skips activity publish."""
        rt = ProxyRuntime()
        sub = await rt.event_bus.try_subscribe()
        try:
            from hestia.protocol import build_frame, tlv
            no_node = build_frame(0x1E, 0x09, tlv(0x0046, b"\x00"))
            make_session(rt)._observe(Frame(no_node[1:-1]))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(sub.queue.get(), timeout=0.05)
        finally:
            sub.close()


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_processes_frames_and_cleans_up(self):
        rt = ProxyRuntime()
        sess = make_session(rt, chunks=[HELLO, TICK, DOOR_EVENT])
        await sess.run()
        out = list(iter_frames(bytes(sess.writer.buf)))
        kinds = [(Frame(b).type, Frame(b).cmd) for b in out]
        self.assertIn((0x64, 0x01), kinds)        # session assigned (from hello)
        self.assertIn((0x1E, 0x0A), kinds)        # event ACK
        self.assertEqual(rt.state.doors[0x12], "open")
        self.assertEqual(rt.sessions, [])         # removed on exit

    async def test_connection_error_cleaned_up(self):
        rt = ProxyRuntime()
        sess = make_session(rt, raise_exc=ConnectionResetError())
        await sess.run()
        self.assertEqual(rt.sessions, [])
        self.assertTrue(sess.writer.closed)


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_emits_timestamp(self):
        sess = make_session(ProxyRuntime(), heartbeat_secs=0.02)
        task = asyncio.create_task(sess._heartbeat())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        kinds = [(Frame(b).type, Frame(b).cmd) for b in iter_frames(bytes(sess.writer.buf))]
        self.assertIn((0x64, 0x03), kinds)


class InjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_inject_writes_bytes(self):
        sess = make_session(ProxyRuntime())
        await sess.inject_to_device(b"\xab\xcd")
        self.assertEqual(bytes(sess.writer.buf), b"\xab\xcd")


class FactoryTests(unittest.TestCase):
    def test_standalone_session_factory(self):
        sess = server._standalone_session(ProxyRuntime(), FakeReader(), FakeWriter(), ProxyConfig())
        self.assertIsInstance(sess, server.StandaloneSession)


class SelectMainTests(unittest.TestCase):
    def test_standalone(self):
        self.assertIs(entry.select_main("standalone"), server.main)

    def test_proxy(self):
        self.assertIs(entry.select_main("proxy"), proxy.main)

    def test_unknown_raises(self):
        with self.assertRaises(SystemExit):
            entry.select_main("bogus")


class DuplicateGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_serial_session_is_dropped(self):
        rt = ProxyRuntime()
        existing = make_session(rt)
        existing.serial = b"SER123"
        rt.sessions.append(existing)                      # a live session for this serial
        reg = build_frame(0x64, 0x02, tlv(0x0002, b"SER123"))
        dup = make_session(rt, chunks=[reg])
        await dup.run()
        self.assertNotIn(dup, rt.sessions)                # the duplicate dropped itself
        self.assertIn(existing, rt.sessions)              # the original is untouched

    async def test_distinct_serial_session_runs_normally(self):
        rt = ProxyRuntime()
        existing = make_session(rt)
        existing.serial = b"SER123"
        rt.sessions.append(existing)
        reg = build_frame(0x64, 0x02, tlv(0x0002, b"OTHER"))
        sess = make_session(rt, chunks=[reg])             # registers, then EOF
        await sess.run()
        self.assertEqual(sess.serial, b"OTHER")
        self.assertNotIn(sess, rt.sessions)               # completed via EOF, removed cleanly
        self.assertIn(existing, rt.sessions)

    async def test_missing_serial_is_not_a_duplicate(self):
        # A registration without TLV 0x0002 must NOT collide with another
        # not-yet-registered session (both serial None) and self-eject.
        rt = ProxyRuntime()
        ghost = make_session(rt)                          # live, serial still None
        rt.sessions.append(ghost)
        reg = build_frame(0x64, 0x02, tlv(0x0004, b"fw"))  # no 0x0002 serial
        sess = make_session(rt, chunks=[reg])
        with self.assertNoLogs("hestia.server", level="WARNING"):
            await sess.run()
        self.assertIsNone(sess.serial)
        self.assertIn(ghost, rt.sessions)                 # ghost not evicted


class StandaloneIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_and_event_ack_over_socket(self):
        rt = ProxyRuntime()
        config = ProxyConfig(listen_host="127.0.0.1", listen_port=0,
                             control_host="127.0.0.1", control_port=0)
        device_srv, control_srv = await proxy._start(rt, config, server._standalone_session)
        port = device_srv.sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(HELLO)
        await writer.drain()
        assign = Frame(await read_frame(reader))
        self.assertEqual((assign.type, assign.cmd), (0x64, 0x01))   # session assigned

        writer.write(DOOR_EVENT)
        await writer.drain()
        ack = Frame(await read_frame(reader))
        self.assertEqual((ack.type, ack.cmd), (0x1E, 0x0A))         # event ACK
        self.assertEqual(tlv_value(ack, 0x001F), b"\x00\xb4")
        await asyncio.sleep(0.05)
        self.assertEqual(rt.state.doors.get(0x12), "open")

        writer.close()
        device_srv.close()
        control_srv.close()
        try:
            await asyncio.wait_for(
                asyncio.gather(device_srv.wait_closed(), control_srv.wait_closed(),
                               return_exceptions=True),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    unittest.main()
