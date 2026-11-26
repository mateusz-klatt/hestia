"""Unit tests for hestia.web — the bootstrap discovery UI.

Each test spins a real asyncio loop in a daemon thread, runs the web server on
an ephemeral port, and exercises endpoints via stdlib `http.client`. This
mirrors the production wire-up (aiohttp handlers run in the asyncio loop) and
catches the things mocks would miss: HTTP framing, header parsing, status
codes, SSE lifecycle, and end-to-end persistence via the same `_persist`
lock-protected path the autosave uses.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web_middlewares import middleware

from hestia import auth, authz, db, proxy, store_sql, web
from hestia.automations import AutomationEngine, AutomationStore, rule_vocab
from hestia.budget import household, household_api
from hestia.protocol import Frame, build_frame, tlv


DOOR_EVENT_NODE12 = build_frame(
    0x1E, 0x09,
    tlv(0x0047, b"\x12") + tlv(0x0046, bytes.fromhex("7105000000ff061600")),
)


class _LoopThread:
    """A dedicated event loop running in a background daemon thread, with a
    submit() helper that mirrors `asyncio.run_coroutine_threadsafe` so tests can
    inject runtime mutations into the loop without touching `rt` directly."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="test-loop", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def submit(self, coro, timeout=10.0):  # CI runners can be slow to start aiohttp
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)
        if not self.loop.is_closed():
            self.loop.close()


class _StartedWeb:
    def __init__(self, handle, loop_thread):
        self._handle = handle
        self._loop_thread = loop_thread
        self.address = handle.address
        self._stopped = False

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._loop_thread.submit(web.stop_web(self._handle), timeout=10.0)


def _start_web(rt, loop_thread, host="127.0.0.1", port=0):
    handle = loop_thread.submit(web.start_web(rt, host, port))
    return _StartedWeb(handle, loop_thread)


def _client(address) -> http.client.HTTPConnection:
    host, port = address
    return http.client.HTTPConnection(host, port, timeout=5.0)


def _get(address, path, headers=None) -> "tuple[int, dict, bytes]":
    conn = _client(address)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _post(address, path, body, headers=None) -> "tuple[int, dict, bytes]":
    conn = _client(address)
    try:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        hdrs = {"Content-Type": "application/json"}      # mutating endpoints require it (CSRF guard)
        hdrs.update(headers or {})                        # caller overrides (e.g. to assert 415)
        conn.request("POST", path, body=body, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _head(address, path) -> "tuple[int, dict, bytes]":
    conn = _client(address)
    try:
        conn.request("HEAD", path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _recv_until(sock, marker: bytes, limit=65536) -> bytes:
    data = b""
    while marker not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _raw_http(address, request: bytes) -> bytes:
    host, port = address
    with socket.create_connection((host, port), timeout=5.0) as s:
        s.sendall(request)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _raw_status(response: bytes) -> int:
    return int(response.split(b"\r\n", 1)[0].split()[1])


def _raw_body(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


def _raw_post(address, path, body=b"", headers=()) -> bytes:
    header_lines = b"".join(f"{k}: {v}\r\n".encode("ascii") for k, v in headers)
    return _raw_http(address, (f"POST {path} HTTP/1.0\r\nHost: localhost\r\n".encode("ascii")
                              + header_lines + b"\r\n" + body))


# --- pure helpers (no server) -----------------------------------------------

class SafeWebBindTests(unittest.TestCase):
    def test_loopback_allowed(self):
        web._require_safe_web_bind("127.0.0.1")           # no raise

    def test_remote_refused_without_optin(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HESTIA_WEB_ALLOW_REMOTE", None)
            with self.assertRaises(RuntimeError):
                web._require_safe_web_bind("0.0.0.0")

    def test_remote_allowed_with_optin(self):
        with mock.patch.dict("os.environ", {"HESTIA_WEB_ALLOW_REMOTE": "1"}):
            web._require_safe_web_bind("0.0.0.0")         # no raise


class SummaryTests(unittest.TestCase):
    def test_counts(self):
        devices = {
            "5":  {"type": "blind",   "confidence": "confirmed"},
            "12": {"type": "door",    "confidence": "inferred"},
            "13": {"type": "unknown", "confidence": "unknown"},
        }
        self.assertEqual(web._summary(devices),
                         {"total": 3, "confirmed": 1, "unknown": 1})

    def test_empty(self):
        self.assertEqual(web._summary({}), {"total": 0, "confirmed": 0, "unknown": 0})


class ValidateNamePayloadTests(unittest.TestCase):
    def _validate(self, op):
        return web._validate_name_payload(op)

    def test_non_dict(self):
        self.assertEqual(self._validate(5), "body must be a JSON object")

    def test_wrong_op(self):
        self.assertEqual(self._validate({"op": "switch", "name": "x"}),
                         "/api/name only accepts op=name")

    def test_empty(self):
        self.assertEqual(self._validate({"node": 5}),
                         "at least one of name, room, type is required")

    def test_missing_node(self):
        self.assertEqual(self._validate({"name": "x"}), "'node' field is required")

    def test_unknown_field_rejected(self):
        err = self._validate({"node": 5, "name": "x", "on": True})
        self.assertIn("unknown field", err)
        self.assertIn("on", err)

    def test_long_name(self):
        err = self._validate({"node": 5, "name": "x" * 300})
        self.assertIn("≤ 256", err)

    def test_non_string_name(self):
        err = self._validate({"node": 5, "name": 42})
        self.assertIn("must be a string", err)

    def test_invalid_type(self):
        self.assertEqual(self._validate({"node": 5, "type": "bogus"}),
                         "invalid type 'bogus'")

    def test_ok(self):
        self.assertIsNone(self._validate({"node": 5, "name": "Room A"}))

    def test_endpoint_name_ok(self):
        self.assertIsNone(self._validate({"node": 7, "ep": 2, "name": "channel 2"}))

    def test_bad_endpoint(self):
        self.assertIn("ep must be", self._validate({"node": 7, "ep": -1, "name": "x"}))
        self.assertIn("ep must be", self._validate({"node": 7, "ep": "2", "name": "x"}))
        self.assertIn("ep must be", self._validate({"node": 7, "ep": True, "name": "x"}))


class ValidateControlPayloadTests(unittest.TestCase):
    def _validate(self, op):
        return web._validate_control_payload(op)

    def test_non_dict(self):
        self.assertEqual(self._validate(5), "body must be a JSON object")

    def test_unsupported_op(self):
        self.assertEqual(self._validate({"op": "raw", "node": 7}),
                         "unsupported control op 'raw'")

    def test_unknown_field_rejected(self):
        self.assertEqual(self._validate({"op": "switch", "node": 7, "on": True, "value": 99}),
                         "unknown field(s): ['value']")

    def test_node_must_be_integer_0_255(self):
        bad = [
            {"op": "switch", "on": True},
            {"op": "switch", "node": True, "on": True},
            {"op": "switch", "node": "7", "on": True},
            {"op": "switch", "node": -1, "on": True},
            {"op": "switch", "node": 256, "on": True},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self._validate(payload), "node must be an integer 0..255")

    def test_on_must_be_boolean(self):
        bad = [
            {"op": "switch", "node": 7},
            {"op": "switch", "node": 7, "on": 1},
            {"op": "thermostat_power", "node": 13, "on": "yes"},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self._validate(payload), "on must be a boolean")

    def test_switch_endpoint_must_be_integer_1_or_2(self):
        bad = [
            {"op": "switch", "node": 7, "endpoint": 3, "on": True},
            {"op": "switch", "node": 7, "endpoint": "1", "on": True},
            {"op": "switch", "node": 7, "endpoint": True, "on": True},
            {"op": "switch", "node": 7, "endpoint": None, "on": True},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self._validate(payload), "endpoint must be an integer 1 or 2")

    def test_value_must_be_integer_0_99(self):
        bad = [
            {"op": "level", "node": 7},
            {"op": "level", "node": 7, "value": True},
            {"op": "level", "node": 7, "value": "50"},
            {"op": "level", "node": 7, "value": -1},
            {"op": "cover", "node": 8, "value": 100},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self._validate(payload), "value must be an integer 0..99")

    def test_celsius_must_be_number_between_4_and_28(self):
        bad = [
            {"op": "thermostat", "node": 13},
            {"op": "thermostat", "node": 13, "celsius": True},
            {"op": "thermostat", "node": 13, "celsius": "21"},
            {"op": "thermostat", "node": 13, "celsius": float("nan")},
            {"op": "thermostat", "node": 13, "celsius": float("inf")},
            {"op": "thermostat", "node": 13, "celsius": 3.9},     # below the 4 °C TRV floor
            {"op": "thermostat", "node": 13, "celsius": 28.1},    # above the 28 °C TRV ceiling
            {"op": "thermostat", "node": 13, "celsius": 10**10000},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self._validate(payload),
                                 "celsius must be a number between 4 and 28")

    def test_valid_cases(self):
        good = [
            {"op": "switch", "node": 0, "on": False},
            {"op": "switch", "node": 7, "endpoint": 1, "on": True},
            {"op": "switch", "node": 7, "endpoint": 2, "on": False},
            {"op": "level", "node": 255, "value": 99},
            {"op": "cover", "node": 8, "value": 0},
            {"op": "thermostat", "node": 13, "celsius": 21},
            {"op": "thermostat", "node": 13, "celsius": 21.5},
            {"op": "thermostat", "node": 13, "celsius": 4},      # TRV floor — the frost-safe OFF setpoint
            {"op": "thermostat", "node": 13, "celsius": 28},     # TRV ceiling
            {"op": "thermostat_power", "node": 13, "on": True},
        ]
        for payload in good:
            with self.subTest(payload=payload):
                self.assertIsNone(self._validate(payload))


# --- live server tests ------------------------------------------------------

class _WebTestBase(unittest.TestCase):
    """Shared fixture: a temp registry, a background loop, and a running web
    server bound to an ephemeral port."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "registry.json"
        self.loop_thread = _LoopThread()
        self.rt = proxy.ProxyRuntime(registry=proxy.Registry(self.path))
        self.web = _start_web(self.rt, self.loop_thread)

    def tearDown(self):
        async def close_bus():
            self.rt.event_bus.close()

        self.loop_thread.submit(close_bus())
        self.web.stop()
        self.loop_thread.close()
        shutil.rmtree(self.tmp)

    def _feed(self, frame_bytes):
        """Inject a frame into the runtime *inside* the loop, matching production."""
        async def go():
            proxy._feed_discovery(self.rt, Frame(frame_bytes[1:-1]))
        self.loop_thread.submit(go())


class IrEndpointTests(_WebTestBase):
    """The /api/ir control endpoint. The default runtime has no ``ir_queue`` (Flipper IR disabled), so
    the op returns its disabled result; the happy ok→200 path is the shared ``_dispatch_op`` exercised by
    the /api/name tests."""

    def test_get_is_405(self):
        status, headers, _ = _get(self.web.address, "/api/ir")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_disabled_returns_503(self):
        status, _, body = _post(self.web.address, "/api/ir", {"file": "/ext/infrared/Klima.ir", "button": "Power"})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"ok": False, "error": "flipper IR is disabled"})

    def test_missing_button_is_400(self):
        status, _, body = _post(self.web.address, "/api/ir", {"file": "/k.ir"})
        self.assertEqual(status, 400)
        self.assertIn(b"file and button required", body)

    def test_bad_content_type_is_415(self):
        status, _, _ = _post(self.web.address, "/api/ir", b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_non_object_body_is_400(self):
        status, _, body = _post(self.web.address, "/api/ir", [1, 2, 3])      # valid JSON, but not an object
        self.assertEqual(status, 400)
        self.assertIn(b"must be a JSON object", body)

    def test_successful_ir_transmit_returns_ok(self):
        async def start_worker():
            self.rt.ir_queue = asyncio.Queue(maxsize=4)
            return asyncio.create_task(proxy._ir_worker(self.rt))

        async def stop_worker(task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        worker = self.loop_thread.submit(start_worker())
        try:
            with mock.patch.object(proxy.flipper, "transmit_ir") as transmit:
                status, _, body = _post(self.web.address, "/api/ir",
                                        {"file": "/k.ir", "button": "Power"})
        finally:
            self.loop_thread.submit(stop_worker(worker))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        transmit.assert_called_once_with("/k.ir", "Power", device=proxy.FLIPPER_DEV)


class _FakeDeviceSession:
    def __init__(self):
        self.injected = []

    async def inject_to_device(self, raw):
        self.injected.append(raw)


class ControlEndpointTests(_WebTestBase):
    def test_get_is_405(self):
        status, headers, _ = _get(self.web.address, "/api/control")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_successful_control_op_sends_command(self):
        fake = _FakeDeviceSession()

        async def add_session():
            self.rt.sessions.append(fake)

        self.loop_thread.submit(add_session())
        status, _, body = _post(self.web.address, "/api/control",
                                {"op": "switch", "node": 14, "on": True})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(fake.injected, [bytes.fromhex(payload["sent"])])

    def test_no_device_connected_returns_503(self):
        status, _, body = _post(self.web.address, "/api/control",
                                {"op": "switch", "node": 14, "on": True})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"ok": False, "error": "no device connected"})

    def test_bad_payload_returns_400(self):
        status, _, body = _post(self.web.address, "/api/control", {"op": "switch", "node": 14})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "on must be a boolean")

    def test_bad_content_type_is_415(self):
        status, _, _ = _post(self.web.address, "/api/control", b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


class SceneOpsTests(unittest.TestCase):
    """House-wide scenes expand into the same per-device ops used by the UI control buttons."""

    DEVICES = {
        "1": {"type": "light", "level": None},
        "2": {"type": "light", "level": 42},
        "3": {"type": "blind", "level": None},
        "4": {"type": "plug", "level": None},
    }

    def _ops(self, scene, nodes=None, value=None):
        # The scene reads opt-outs from the REGISTRY (not from discovery — keeps the DeviceInfo wire
        # shape stable for native clients), so give rt a registry.nodes map.
        rt = SimpleNamespace(registry=SimpleNamespace(nodes=nodes or {}))
        with mock.patch("hestia.web._merged_discovery", return_value=self.DEVICES):
            return web._scene_ops(rt, scene, value)

    def test_lights_off_switches_plain_lights_and_dimmable_lights_off(self):
        self.assertEqual(self._ops("lights_off"), [
            {"op": "switch", "node": 1, "on": False},
            {"op": "level", "node": 2, "value": 0},
        ])

    def test_lights_on_switches_plain_lights_and_dimmable_lights_on(self):
        self.assertEqual(self._ops("lights_on"), [
            {"op": "switch", "node": 1, "on": True},
            {"op": "level", "node": 2, "value": 99},
        ])

    def test_blinds_down_and_up_use_cover_values(self):
        self.assertEqual(self._ops("blinds_down"), [{"op": "cover", "node": 3, "value": 0}])
        self.assertEqual(self._ops("blinds_up"), [{"op": "cover", "node": 3, "value": 99}])

    def test_blinds_set_drives_blinds_to_a_wire_position(self):
        # The whole-home slider sends a WIRE cover value 0-99 (the UI applies the perceptual curve); the
        # backend fans it out verbatim (a value of 64 → cover 64, not a linear %→wire conversion).
        self.assertEqual(self._ops("blinds_set", value=64), [{"op": "cover", "node": 3, "value": 64}])
        self.assertEqual(self._ops("blinds_set", value=0), [{"op": "cover", "node": 3, "value": 0}])
        self.assertEqual(self._ops("blinds_set", value=99), [{"op": "cover", "node": 3, "value": 99}])

    def test_blinds_set_respects_the_whole_home_opt_out(self):
        # An opted-out blind is skipped by blinds_set just like the up/down extremes.
        self.assertEqual(self._ops("blinds_set", {"3": {"exclude_from_all": True}}, value=50), [])

    def test_clamp_cover_clamps_out_of_range(self):
        self.assertEqual(web._clamp_cover(-5), 0)
        self.assertEqual(web._clamp_cover(150), 99)

    def test_devices_opted_out_of_all_are_skipped(self):
        # Opt-outs live in the registry, NOT in discovery — node 2 excluded, node 3 explicitly re-included.
        nodes = {"2": {"exclude_from_all": True}, "3": {"exclude_from_all": False}}
        self.assertEqual(self._ops("lights_off", nodes),
                         [{"op": "switch", "node": 1, "on": False}])           # node 2 dropped
        self.assertEqual(self._ops("blinds_up", nodes),
                         [{"op": "cover", "node": 3, "value": 99}])            # exclude=False stays in

    def _gang_ops(self, devices, nodes, scene="lights_on"):
        rt = SimpleNamespace(registry=SimpleNamespace(nodes=nodes))
        with mock.patch("hestia.web._merged_discovery", return_value=devices):
            return web._scene_ops(rt, scene)

    def test_gang_excluded_light_expands_to_per_endpoint_ops(self):
        # 2-gang hall/nightlight: gang 2 opted out → ONE endpoint-addressed op for gang 1 only.
        devices = {"7": {"type": "light", "level": None, "endpoints": {1: True, 2: False}}}
        nodes = {"7": {"endpoint_exclude": {"2": True}}}
        self.assertEqual(self._gang_ops(devices, nodes),
                         [{"op": "switch", "node": 7, "on": True, "endpoint": 1}])

    def test_all_gangs_excluded_emits_nothing(self):
        devices = {"7": {"type": "light", "level": None, "endpoints": {1: True, 2: False}}}
        nodes = {"7": {"endpoint_exclude": {"1": True, "2": True}}}
        self.assertEqual(self._gang_ops(devices, nodes), [])

    def test_all_false_endpoint_exclude_keeps_the_node_level_op(self):
        # Re-included gangs (explicit False) are NOT an exclusion → one node-level frame as before.
        devices = {"7": {"type": "light", "level": None, "endpoints": {1: True, 2: False}}}
        nodes = {"7": {"endpoint_exclude": {"2": False}}}
        self.assertEqual(self._gang_ops(devices, nodes), [{"op": "switch", "node": 7, "on": True}])

    def test_gang_set_falls_back_to_labels_when_live_state_is_missing(self):
        # No live gang map yet → the labelled gangs (endpoint_names) still drive the expansion.
        devices = {"7": {"type": "light", "level": None, "endpoints": None}}
        nodes = {"7": {"endpoint_exclude": {"2": True}, "endpoint_names": {"1": "hol", "2": "kaganek"}}}
        self.assertEqual(self._gang_ops(devices, nodes),
                         [{"op": "switch", "node": 7, "on": True, "endpoint": 1}])

    def test_unenumerable_gang_set_is_skipped_fail_safe(self):
        # The only knowledge of this node's gangs is the opt-out itself (no live state, no labels):
        # the sweep must emit NOTHING — never a node-level frame that would drive the excluded gang.
        devices = {"7": {"type": "light", "level": None, "endpoints": None}}
        nodes = {"7": {"endpoint_exclude": {"2": True}}}
        self.assertEqual(self._gang_ops(devices, nodes), [])

    def test_dimmable_light_ignores_endpoint_exclude(self):
        # A dimmer is driven by a node-level `level` op (no endpoint concept) — per-gang flags don't apply.
        devices = {"7": {"type": "light", "level": 42, "endpoints": None}}
        nodes = {"7": {"endpoint_exclude": {"2": True}}}
        self.assertEqual(self._gang_ops(devices, nodes), [{"op": "level", "node": 7, "value": 99}])

    def test_node_exclusion_beats_per_gang_state(self):
        # A node-level opt-out drops the whole device even if per-gang flags also exist.
        devices = {"7": {"type": "light", "level": None, "endpoints": {1: True, 2: False}}}
        nodes = {"7": {"exclude_from_all": True, "endpoint_exclude": {"2": False}}}
        self.assertEqual(self._gang_ops(devices, nodes), [])


class SceneEndpointTests(_WebTestBase):
    """POST /api/scene — fan-out endpoint for house-wide actions."""

    def test_unknown_or_non_object_op_is_400(self):
        for payload in ({"op": "bogus"}, [1, 2]):
            with self.subTest(payload=payload):
                status, _, body = _post(self.web.address, "/api/scene", payload)
                self.assertEqual(status, 400)
                self.assertIn("op must be one of", json.loads(body)["error"])

    def test_bad_content_type_is_415(self):
        status, _, body = _post(self.web.address, "/api/scene", b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])

    def test_total_zero_returns_ok_and_audits(self):
        with mock.patch("hestia.web._merged_discovery", return_value={}):
            with mock.patch("hestia.web._audit") as audit:
                status, _, body = _post(self.web.address, "/api/scene", {"op": "lights_off"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "sent": 0, "total": 0})
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[:3], (self.rt, "anonymous", "scene"))
        self.assertEqual(audit.call_args.kwargs["target"], "lights_off")
        self.assertEqual(audit.call_args.kwargs["result"], "0/0")

    def test_no_device_is_counted_not_sent(self):
        with mock.patch("hestia.web._merged_discovery", return_value={"5": {"type": "light", "level": None}}):
            with mock.patch("hestia.web._audit") as audit:
                status, _, body = _post(self.web.address, "/api/scene", {"op": "lights_on"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "sent": 0, "total": 1})
        self.assertEqual(audit.call_args.kwargs["result"], "0/1")

    def test_successes_are_summarised_and_audited(self):
        devices = {
            "1": {"type": "light", "level": None},
            "2": {"type": "light", "level": 10},
        }

        async def fake_control(_rt, op):
            return {"ok": op["op"] == "switch"}

        with mock.patch("hestia.web._merged_discovery", return_value=devices):
            with mock.patch("hestia.web.process_control_op", side_effect=fake_control):
                with mock.patch("hestia.web._audit") as audit:
                    status, _, body = _post(self.web.address, "/api/scene", {"op": "lights_off"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "sent": 1, "total": 2})
        self.assertEqual(audit.call_args.kwargs["result"], "1/2")

    def test_blinds_set_drives_blinds_to_a_position(self):
        devices = {"3": {"type": "blind", "level": None}}
        sent = []

        async def fake_control(_rt, op):
            sent.append(op)
            return {"ok": True}

        with mock.patch("hestia.web._merged_discovery", return_value=devices):
            with mock.patch("hestia.web.process_control_op", side_effect=fake_control):
                with mock.patch("hestia.web._audit"):
                    status, _, body = _post(self.web.address, "/api/scene", {"op": "blinds_set", "value": 50})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "sent": 1, "total": 1})
        self.assertEqual(sent, [{"op": "cover", "node": 3, "value": 50}])

    def test_blinds_set_rejects_missing_or_bad_value(self):
        # bool is an int subclass — it must NOT pass as a position; missing / out-of-range / non-int → 400.
        # 100 is now out of range too (value is a wire 0-99, not a 0-100 %).
        for payload in ({"op": "blinds_set"}, {"op": "blinds_set", "value": 100},
                        {"op": "blinds_set", "value": -1}, {"op": "blinds_set", "value": True},
                        {"op": "blinds_set", "value": "50"}):
            with self.subTest(payload=payload):
                status, _, body = _post(self.web.address, "/api/scene", payload)
                self.assertEqual(status, 400)
                self.assertIn("blinds_set requires", json.loads(body)["error"])


class IndexTests(_WebTestBase):
    """The root `/` now serves the built TS app (the legacy inline page is gone); `/ui/` redirects to it."""

    def test_root_404_when_dist_absent(self):
        self.web.stop()
        with mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(self.tmp / "missing-dist")}):
            self.web = _start_web(self.rt, self.loop_thread)
            status, _, body = _get(self.web.address, "/")
        self.assertEqual(status, 404)                # no build present → 404 (never a 500)
        self.assertEqual(body, b"")

    def test_root_serves_built_index_and_assets(self):
        dist = self.tmp / "ui-dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><h1>hestia</h1>", encoding="utf-8")
        (assets / "app.js").write_text("console.log('hestia shell');\n", encoding="utf-8")

        self.web.stop()
        with mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(dist)}):
            self.web = _start_web(self.rt, self.loop_thread)
            status, headers, body = _get(self.web.address, "/")
            asset_status, _, asset_body = _get(self.web.address, "/assets/app.js")
            missing_status, _, _ = _get(self.web.address, "/assets/missing.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<h1>hestia</h1>", body)
        self.assertEqual(asset_status, 200)
        self.assertEqual(asset_body, b"console.log('hestia shell');\n")
        self.assertEqual(missing_status, 404)        # built UI, asset not in the bundle → 404, not 500

    def test_ui_path_redirects_to_root(self):
        # The retired /ui/ alias 302-redirects to "../" (relative → correct at a bare root OR behind /hestia/).
        status, headers, _ = _get(self.web.address, "/ui/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "../")


class AuthTests(_WebTestBase):
    """App-level login (#43). Auth is enabled per-request via the module globals, which the middleware
    reads live — so patching them gates the already-running test server without a restart."""

    def setUp(self):
        super().setUp()
        self.users = {"tata": auth.hash_password("s3cret")}
        patches = [
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", b"test-secret-bytes"),
            mock.patch.object(web, "_COOKIE_SECURE", False),
            mock.patch.object(auth, "load_users", return_value=self.users),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _login(self, user="tata", password="s3cret"):
        return _post(self.web.address, "/api/login", {"user": user, "password": password})

    def test_password_attempt_budget(self):
        with mock.patch.object(auth, "LOGIN_ATTEMPTS", 1):
            self.assertEqual(self._login(password="wrong")[0], 401)
            status, headers, _ = self._login()
            self.assertEqual(status, 429)
            self.assertGreater(int(headers["Retry-After"]), 0)

    @staticmethod
    def _cookie(set_cookie_header):
        return set_cookie_header.split(";", 1)[0]      # "hestia_session=<token>"

    # ---- gating ----
    def test_gated_route_401_without_cookie(self):
        status, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(status, 401)
        self.assertEqual(body, b"")

    def test_gated_post_401_without_cookie(self):
        status, _, _ = _post(self.web.address, "/api/control", {"op": "switch", "node": 5, "on": True})
        self.assertEqual(status, 401)                  # a non-GET, non-login route is gated

    def test_forged_cookie_401(self):
        status, _, _ = _get(self.web.address, "/api/discovery",
                            headers={"Cookie": "hestia_session=forged.token"})
        self.assertEqual(status, 401)

    def test_wrong_method_on_public_path_is_gated(self):
        # only POST /api/login is public; a non-POST hit is gated (401), not a public 405 (exact allowlist)
        conn = _client(self.web.address)
        try:
            conn.request("PUT", "/api/login")
            self.assertEqual(conn.getresponse().status, 401)
        finally:
            conn.close()

    def test_public_ui_redirect_open(self):
        status, headers, _ = _get(self.web.address, "/ui/")     # public GET → redirect, never gated
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "../")

    def test_public_root_and_assets_not_gated(self):
        # public GETs: not 401 (no build present in this test → 404, but crucially NOT gated to 401)
        self.assertNotEqual(_get(self.web.address, "/")[0], 401)
        self.assertNotEqual(_get(self.web.address, "/assets/app.js")[0], 401)

    # ---- login ----
    def test_login_success_sets_cookie(self):
        status, headers, body = self._login()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "user": "tata"})
        sc = headers["Set-Cookie"]
        self.assertIn("hestia_session=", sc)
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)

    def test_login_wrong_password_401(self):
        status, headers, body = self._login(password="nope")
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["ok"])
        self.assertNotIn("Set-Cookie", headers)        # no session minted

    def test_login_unknown_user_401(self):
        self.assertEqual(self._login(user="ghost")[0], 401)

    def test_login_malformed_body_401(self):
        self.assertEqual(_post(self.web.address, "/api/login", [])[0], 401)          # non-dict
        self.assertEqual(_post(self.web.address, "/api/login", {"user": "tata"})[0], 401)  # missing password

    def test_login_when_auth_disabled_401(self):
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            self.assertEqual(self._login()[0], 401)     # can't mint a session without a configured secret

    def test_login_bad_content_type_415(self):
        status, _, _ = _post(self.web.address, "/api/login", "x", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)                   # login still enforces the JSON CSRF guard

    # ---- authed round-trip ----
    def test_authed_request_passes(self):
        _, headers, _ = self._login()
        cookie = self._cookie(headers["Set-Cookie"])
        status, _, _ = _get(self.web.address, "/api/discovery", headers={"Cookie": cookie})
        self.assertEqual(status, 200)

    def test_discovery_response_matches_the_contract(self):
        from hestia import api_contract
        cookie = self._cookie(self._login()[1]["Set-Cookie"])
        status, _, body = _get(self.web.address, "/api/discovery", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        api_contract.Discovery.model_validate(json.loads(body))  # the LIVE envelope matches the DTO

    def test_whoami_returns_logged_in_user(self):
        _, headers, _ = self._login()
        cookie = self._cookie(headers["Set-Cookie"])
        status, _, body = _get(self.web.address, "/api/whoami", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        # role resolves to admin here: JSON-backed users (mocked load_users) are legacy single-tier (#73)
        self.assertEqual(json.loads(body), {"user": "tata", "role": "admin"})

    def test_logout_clears_cookie(self):
        status, headers, body = _post(self.web.address, "/api/logout", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertIn("hestia_session=", headers["Set-Cookie"])
        self.assertIn('Max-Age=0', headers["Set-Cookie"])      # del_cookie expires it immediately

    # ---- bearer token (native clients, e.g. the iOS app) ----
    def _bearer_token(self):
        _, _, body = _post(self.web.address, "/api/login",
                           {"user": "tata", "password": "s3cret", "bearer": True})
        return json.loads(body)["token"]

    def test_login_with_bearer_returns_a_valid_token(self):
        status, _, body = _post(self.web.address, "/api/login",
                                {"user": "tata", "password": "s3cret", "bearer": True})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["user"], "tata")
        # the returned token is a real session token for this user
        self.assertEqual(
            auth.verify_session(payload["token"], now=web._now(), secret=b"test-secret-bytes"), "tata")

    def test_login_without_bearer_omits_token(self):
        self.assertNotIn("token", json.loads(self._login()[2]))  # web UI never gets the token in the body

    def test_bearer_header_authenticates_a_gated_route(self):
        status, _, body = _get(self.web.address, "/api/whoami",
                               headers={"Authorization": f"Bearer {self._bearer_token()}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"user": "tata", "role": "admin"})

    def test_forged_bearer_401(self):
        status, _, _ = _get(self.web.address, "/api/discovery",
                            headers={"Authorization": "Bearer forged.token"})
        self.assertEqual(status, 401)


class SettingsEndpointTests(_WebTestBase):
    """GET/POST /api/settings — per-user server settings, local-first on the client."""

    def setUp(self):
        super().setUp()
        self.db_file = self.tmp / "settings.db"
        self.users = {"tata": auth.hash_password("s3cret")}
        patches = [
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", b"test-secret-bytes"),
            mock.patch.object(web, "_COOKIE_SECURE", False),
            mock.patch.object(auth, "load_users", return_value=self.users),
            mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db_file)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        from hestia import db
        engine, Session = db.init_db(self.db_file)
        with db.session_scope(Session) as s:
            s.add(db.User(username="tata", password_hash=self.users["tata"]))
        engine.dispose()

    def _login_cookie(self):
        status, headers, _ = _post(self.web.address, "/api/login", {"user": "tata", "password": "s3cret"})
        self.assertEqual(status, 200)
        return AuthTests._cookie(headers["Set-Cookie"])

    def _headers(self):
        return {"Cookie": self._login_cookie()}

    def test_settings_routes_are_auth_gated(self):
        self.assertEqual(_get(self.web.address, "/api/settings")[0], 401)
        self.assertEqual(_post(self.web.address, "/api/settings", {"locale": "pl"})[0], 401)

    def test_get_returns_nulls_without_row_and_existing_row(self):
        from hestia import store_sql
        headers = self._headers()
        status, _, body = _get(self.web.address, "/api/settings", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"locale": None, "temp_scale": None, "theme": None,
                                            "preferred_languages": ["en"]})

        store_sql.set_user_settings("tata", locale="pl", temp_scale="F", theme="dark")
        status, _, body = _get(self.web.address, "/api/settings", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"locale": "pl", "temp_scale": "F", "theme": "dark",
                                            "preferred_languages": ["pl"]})

    def test_get_auth_off_returns_nulls(self):
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            status, _, body = _get(self.web.address, "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"locale": None, "temp_scale": None, "theme": None,
                                            "preferred_languages": ["en"]})

    def test_post_requires_json_content_type(self):
        status, _, body = _post(self.web.address, "/api/settings", "x",
                                headers={"Cookie": self._login_cookie(), "Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])

    def test_post_rejects_invalid_payloads(self):
        headers = self._headers()
        bad_payloads = [
            ([1, 2], "object"),
            ({"locale": "x" * 36}, "locale"),
            ({"temp_scale": "X"}, "temp_scale"),
            ({"temp_scale": []}, "temp_scale"),
            ({"theme": 5}, "theme"),
            ({"preferred_languages": ["xx"]}, "preferred_languages"),
            ({"preferred_languages": ["de", "de"]}, "preferred_languages"),
            ({"preferred_languages": "de"}, "preferred_languages"),
        ]
        for payload, fragment in bad_payloads:
            with self.subTest(payload=payload):
                status, _, body = _post(self.web.address, "/api/settings", payload, headers=headers)
                self.assertEqual(status, 400)
                self.assertIn(fragment, json.loads(body)["error"])

    def test_post_json_mode_is_ok_noop_without_audit(self):
        headers = self._headers()
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            with mock.patch("hestia.web._audit") as audit:
                status, _, body = _post(self.web.address, "/api/settings", {"locale": "pl"}, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertFalse(audit.called)

    def test_post_auth_off_is_ok_noop(self):
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            with mock.patch("hestia.store_sql.set_user_settings") as set_settings:
                status, _, body = _post(self.web.address, "/api/settings", {"locale": "pl"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertFalse(set_settings.called)

    def test_post_accepts_theme_and_temp_scale(self):
        from hestia import store_sql
        with mock.patch("hestia.web._audit"):
            status, _, body = _post(self.web.address, "/api/settings",
                                    {"temp_scale": "K", "theme": "dark", "preferred_languages": ["fr", "en"]}, headers=self._headers())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(store_sql.get_user_settings("tata"),
                         {"locale": None, "temp_scale": "K", "theme": "dark", "preferred_languages": ["fr", "en"]})

    def test_post_persists_partial_merge_and_audits(self):
        from hestia import store_sql
        headers = self._headers()
        store_sql.set_user_settings("tata", locale="en", temp_scale="F", theme=None)
        with mock.patch("hestia.web._audit") as audit:
            status, _, body = _post(self.web.address, "/api/settings", {"locale": "pl"}, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(store_sql.get_user_settings("tata"),
                         {"locale": "pl", "temp_scale": "F", "theme": None, "preferred_languages": ["pl"]})
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[:3], (self.rt, "tata", "settings"))
        self.assertIn("locale=pl,scale=None", audit.call_args.kwargs["detail"])


class RoomIconsEndpointTests(_WebTestBase):
    """GET/POST /api/rooms/icons — shared durable emoji choices for room cards."""

    def setUp(self):
        super().setUp()
        self.db_file = self.tmp / "room-icons.db"
        self.users = {"tata": auth.hash_password("s3cret")}
        patches = [
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", b"test-secret-bytes"),
            mock.patch.object(web, "_COOKIE_SECURE", False),
            mock.patch.object(auth, "load_users", return_value=self.users),
            mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db_file)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        from hestia import db
        engine, Session = db.init_db(self.db_file)
        with db.session_scope(Session) as s:
            s.add(db.User(username="tata", password_hash=self.users["tata"]))
        engine.dispose()

    def _login_cookie(self):
        status, headers, _ = _post(self.web.address, "/api/login", {"user": "tata", "password": "s3cret"})
        self.assertEqual(status, 200)
        return AuthTests._cookie(headers["Set-Cookie"])

    def _headers(self):
        return {"Cookie": self._login_cookie()}

    def test_room_icon_routes_are_auth_gated(self):
        self.assertEqual(_get(self.web.address, "/api/rooms/icons")[0], 401)
        self.assertEqual(_post(self.web.address, "/api/rooms/icons", {"room": "Salon", "icon": "🛋️"})[0], 401)

    def test_get_returns_empty_or_existing_map(self):
        from hestia import store_sql
        headers = self._headers()
        status, _, body = _get(self.web.address, "/api/rooms/icons", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})

        store_sql.set_room_icon("Salon", "🛋️")
        status, _, body = _get(self.web.address, "/api/rooms/icons", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"Salon": "🛋️"})

    def test_post_requires_json_content_type(self):
        status, _, body = _post(self.web.address, "/api/rooms/icons", "x",
                                headers={"Cookie": self._login_cookie(), "Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])

    def test_post_rejects_invalid_payloads(self):
        headers = self._headers()
        bad_payloads = [
            ([1, 2], "object"),
            ({"room": 5, "icon": "🚪"}, "room"),
            ({"room": "x" * 65, "icon": "🚪"}, "room"),
            ({"room": "Salon"}, "icon"),
            ({"room": "Salon", "icon": 5}, "icon"),
            ({"room": "Salon", "icon": "x" * 17}, "icon"),
        ]
        for payload, fragment in bad_payloads:
            with self.subTest(payload=payload):
                status, _, body = _post(self.web.address, "/api/rooms/icons", payload, headers=headers)
                self.assertEqual(status, 400)
                self.assertIn(fragment, json.loads(body)["error"])

    def test_post_json_mode_is_ok_noop_without_audit(self):
        from hestia import store_sql
        headers = self._headers()
        with mock.patch.dict(os.environ, {"HESTIA_PERSIST": "json"}):
            with mock.patch("hestia.web._audit") as audit:
                status, _, body = _post(self.web.address, "/api/rooms/icons",
                                        {"room": "Salon", "icon": "🛋️"}, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(store_sql.get_room_icons(), {})
        self.assertFalse(audit.called)

    def test_post_auth_off_is_ok_noop(self):
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            with mock.patch("hestia.store_sql.set_room_icon") as set_icon:
                status, _, body = _post(self.web.address, "/api/rooms/icons", {"room": "Salon", "icon": "🛋️"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertFalse(set_icon.called)

    def test_post_persists_empty_room_clears_and_audits(self):
        from hestia import store_sql
        headers = self._headers()
        with mock.patch("hestia.web._audit") as audit:
            status, _, body = _post(self.web.address, "/api/rooms/icons", {"room": "", "icon": "🚪"},
                                    headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(store_sql.get_room_icons(), {"": "🚪"})
        audit.assert_called_once()
        self.assertEqual(audit.call_args.args[:3], (self.rt, "tata", "room_icon"))
        self.assertEqual(audit.call_args.kwargs["target"], "")

        status, _, body = _post(self.web.address, "/api/rooms/icons", {"room": "", "icon": ""},
                                headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(store_sql.get_room_icons(), {})


class DiscoveryTests(_WebTestBase):
    def test_discovery_serves_json_with_summary(self):
        self._feed(DOOR_EVENT_NODE12)
        status, headers, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        data = json.loads(body)
        self.assertIn("18", data["devices"])
        self.assertEqual(data["devices"]["18"]["type"], "door")
        self.assertEqual(data["summary"], {"total": 1, "confirmed": 0, "unknown": 0})
        self.assertEqual(data["globals"],                   # pollers off → null
                         {"crib_temp": None, "crib_temp_ts": None, "outdoor_temp": None,
                          "outdoor_humidity": None, "outdoor_temp_ts": None, "outdoor_battery_ok": None})

    def test_discovery_reflects_global_fields(self):
        self.rt.state.crib_temp = 22.5
        self.rt.state.crib_temp_ts = 1_699_900_000.0
        self.rt.state.outdoor_temp = -1.0
        self.rt.state.outdoor_humidity = 44.0
        self.rt.state.outdoor_temp_ts = 1_700_000_000.0
        self.rt.state.outdoor_battery_ok = True
        _, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(body)["globals"],
                         {"crib_temp": 22.5, "crib_temp_ts": "2023-11-13T18:26:40Z", "outdoor_temp": -1.0,
                          "outdoor_humidity": 44.0, "outdoor_temp_ts": "2023-11-14T22:13:20Z",
                          "outdoor_battery_ok": True})

    def test_discovery_includes_klima(self):
        sentinel = {"file": "/ext/infrared/klima.ir", "modes": {"cool": [22]},
                    "power_on": {"cool": [22]}, "presets": ["off"]}
        with mock.patch.object(web, "KLIMA", sentinel):
            _, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(body)["klima"], sentinel)

    def test_discovery_klima_absent(self):
        with mock.patch.object(web, "KLIMA", {}):
            _, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(body)["klima"], {})

    def test_discovery_includes_klima_state(self):
        _, _, body = _get(self.web.address, "/api/discovery")
        self.assertIsNone(json.loads(body)["klima_state"])    # never commanded → null
        self.rt.state.klima = {"power": True, "mode": "cool", "temp": 22}
        _, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(body)["klima_state"], {"power": True, "mode": "cool", "temp": 22})

    def test_discovery_includes_rule_vocab(self):
        _, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(body)["rule_vocab"], rule_vocab())

    def test_discovery_includes_mode_fields(self):
        _, _, body = _get(self.web.address, "/api/discovery")
        data = json.loads(body)
        self.assertEqual(data["mode"], "proxy")               # the test rt runs proxy
        self.assertEqual(data["target_mode"], "proxy")        # registry not graduated
        self.assertIn("env_override", data)                   # present (None unless HESTIA_MODE is set)

    def test_graduate_persists_and_reflects_target(self):
        with mock.patch.object(self.rt.registry, "write_payload"):
            status, _, body = _post(self.web.address, "/api/graduate", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        _, _, disco = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(disco)["target_mode"], "standalone")   # persisted; running stays proxy

    def test_graduate_get_is_405(self):
        status, _, _ = _get(self.web.address, "/api/graduate")
        self.assertEqual(status, 405)

    def test_graduate_bad_content_type_is_415(self):                       # CSRF guard (same as /api/ir)
        status, _, _ = _post(self.web.address, "/api/graduate", b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_graduate_persist_failure_returns_503(self):
        with mock.patch.object(self.rt.registry, "write_payload", side_effect=OSError("disk")):
            status, _, body = _post(self.web.address, "/api/graduate", {})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body),
                         {"ok": False, "error": "graduate persist failed: OSError('disk')"})


class NamePersistTests(_WebTestBase):
    def test_name_persists(self):
        status, _, body = _post(self.web.address, "/api/name",
                                {"node": 5, "name": "Room A", "room": "LR"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        # round-trip
        _, _, disco = _get(self.web.address, "/api/discovery")
        entry = json.loads(disco)["devices"]["5"]
        self.assertEqual(entry["name"], "Room A")
        self.assertEqual(entry["room"], "LR")

    def test_endpoint_name_persists(self):
        # an ep-name POST is accepted (200) and round-trips into endpoint_names.
        status, _, body = _post(self.web.address, "/api/name", {"node": 7, "ep": 2, "name": "channel 2"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        _, _, disco = _get(self.web.address, "/api/discovery")
        entry = json.loads(disco)["devices"]["7"]
        self.assertEqual(entry["endpoint_names"], {"2": "channel 2"})

    def test_name_round_trip_persists_across_reload(self):
        _post(self.web.address, "/api/name",
              {"node": 5, "name": "Room A", "room": "LR", "type": "blind"})
        # spin everything down and re-open the registry file from scratch.
        self.web.stop()
        self.loop_thread.close()
        reg = proxy.Registry.load(self.path)
        self.assertEqual(reg.nodes["5"]["name"], "Room A")
        self.assertEqual(reg.nodes["5"]["room"], "LR")
        self.assertEqual(reg.nodes["5"]["type"], "blind")
        self.assertTrue(reg.nodes["5"]["type_confirmed"])
        # restart so tearDown's shutdown calls are no-ops on already-closed state
        self.loop_thread = _LoopThread()
        self.web = _start_web(self.rt, self.loop_thread)

    def test_classifier_does_not_override_confirmed_type(self):
        # confirm type=blind, then inject a door event for node 18 — registry
        # should still report "blind" with confidence "confirmed".
        _post(self.web.address, "/api/name", {"node": 18, "type": "blind"})
        self._feed(DOOR_EVENT_NODE12)
        _, _, body = _get(self.web.address, "/api/discovery")
        entry = json.loads(body)["devices"]["18"]
        self.assertEqual(entry["type"], "blind")
        self.assertEqual(entry["confidence"], "confirmed")

    def test_name_only_does_not_freeze_type(self):
        # POST name only; classifier observes a door event after; type updates.
        _post(self.web.address, "/api/name", {"node": 18, "name": "front"})
        self._feed(DOOR_EVENT_NODE12)
        _, _, body = _get(self.web.address, "/api/discovery")
        entry = json.loads(body)["devices"]["18"]
        self.assertEqual(entry["type"], "door")
        self.assertEqual(entry["confidence"], "inferred")
        self.assertEqual(entry["name"], "front")

    def test_ui_name_edit_payload_does_not_freeze_type(self):
        # Same as test_name_only_does_not_freeze_type but reading exact UI
        # behavior — POST {node, name} only, no type field, so type stays inferable.
        _post(self.web.address, "/api/name", {"node": 18, "name": "x"})
        self._feed(DOOR_EVENT_NODE12)
        _, _, body = _get(self.web.address, "/api/discovery")
        entry = json.loads(body)["devices"]["18"]
        self.assertNotEqual(entry["confidence"], "confirmed")


class WholeHomeEndpointTests(_WebTestBase):
    """The dedicated /api/whole-home read+write surface — the opt-out lives here, NOT in DeviceInfo, so
    /api/discovery stays wire-stable for pinned native clients."""

    def _excluded(self):
        _, _, body = _get(self.web.address, "/api/whole-home")
        return json.loads(body)["excluded_nodes"]

    def test_get_empty_by_default(self):
        self.assertEqual(self._excluded(), [])

    def test_post_sets_and_get_reflects_then_reincludes(self):
        status, _, body = _post(self.web.address, "/api/whole-home", {"node": 5, "exclude": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(self._excluded(), [5])
        # exclude=false re-includes → drops out of the list
        _post(self.web.address, "/api/whole-home", {"node": 5, "exclude": False})
        self.assertEqual(self._excluded(), [])

    def test_opt_out_is_not_surfaced_in_discovery(self):
        _post(self.web.address, "/api/whole-home", {"node": 5, "exclude": True})
        _, _, disco = _get(self.web.address, "/api/discovery")
        devices = json.loads(disco)["devices"]
        if "5" in devices:                                    # registry-only → never on the device snapshot
            self.assertNotIn("exclude_from_all", devices["5"])

    def test_excluded_nodes_are_sorted_numerically(self):
        for n in (10, 2, 7):
            _post(self.web.address, "/api/whole-home", {"node": n, "exclude": True})
        self.assertEqual(self._excluded(), [2, 7, 10])

    def test_per_gang_post_round_trips_via_excluded_endpoints(self):
        status, _, body = _post(self.web.address, "/api/whole-home", {"node": 7, "ep": 2, "exclude": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        _, _, got = _get(self.web.address, "/api/whole-home")
        self.assertEqual(json.loads(got)["excluded_endpoints"], {"7": [2]})
        self.assertEqual(json.loads(got)["excluded_nodes"], [])              # node-level untouched
        # re-include the gang → drops out of the map entirely (no empty lists)
        _post(self.web.address, "/api/whole-home", {"node": 7, "ep": 2, "exclude": False})
        _, _, got = _get(self.web.address, "/api/whole-home")
        self.assertEqual(json.loads(got)["excluded_endpoints"], {})

    def test_explicit_null_ep_is_node_level(self):
        status, _, _ = _post(self.web.address, "/api/whole-home", {"node": 5, "ep": None, "exclude": True})
        self.assertEqual(status, 200)
        self.assertEqual(self._excluded(), [5])

    def test_bad_payloads_are_400(self):
        for body in ({"node": "x", "exclude": True}, {"node": 5, "exclude": "yes"},
                     {"node": 300, "exclude": True}, {"node": 5}, {"node": 5, "exclude": True, "zzz": 1},
                     {"node": 5, "exclude": True, "ep": 0}, {"node": 5, "exclude": True, "ep": 3},
                     {"node": 5, "exclude": True, "ep": "x"}, {"node": 5, "exclude": True, "ep": True},
                     [1, 2]):                                   # a non-object JSON body → 400
            with self.subTest(body=body):
                status, _, _ = _post(self.web.address, "/api/whole-home", body)
                self.assertEqual(status, 400)

    def test_bad_content_type_is_415(self):
        status, _, _ = _post(self.web.address, "/api/whole-home", b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)


class NameValidationTests(_WebTestBase):
    def test_name_empty_payload_returns_400(self):
        status, _, body = _post(self.web.address, "/api/name", {"node": 5})
        self.assertEqual(status, 400)
        self.assertIn("at least one", json.loads(body)["error"])

    def test_name_long_string_rejected(self):
        status, _, body = _post(self.web.address, "/api/name", {"node": 5, "name": "x" * 300})
        self.assertEqual(status, 400)
        self.assertIn("≤ 256", json.loads(body)["error"])

    def test_name_invalid_type_returns_400(self):
        status, _, body = _post(self.web.address, "/api/name", {"node": 5, "type": "bogus"})
        self.assertEqual(status, 400)
        self.assertIn("invalid type", json.loads(body)["error"])

    def test_name_invalid_node_returns_400(self):
        # JSON validates, validator passes (no per-field error), but inside
        # `process_control_op` → `set_user` → `_key` → `int("not-a-number", 0)`
        # raises ValueError, which the handler maps to 400.
        status, _, body = _post(self.web.address, "/api/name",
                                {"node": "not-a-number", "name": "x"})
        self.assertEqual(status, 400)

    def test_name_rejects_non_name_op(self):
        status, _, body = _post(self.web.address, "/api/name",
                                {"op": "switch", "node": 5, "name": "x"})
        self.assertEqual(status, 400)
        self.assertIn("only accepts op=name", json.loads(body)["error"])

    def test_name_rejects_oversized_body(self):
        big = {"node": 5, "name": "x" * 9000}            # well over MAX_BODY
        status, _, body = _post(self.web.address, "/api/name", big)
        self.assertEqual(status, 413)

    def test_name_missing_content_length(self):
        # http.client always sets Content-Length when body is provided. To force
        # absence we use a chunked request via raw socket.
        host, port = self.web.address
        import socket
        with socket.create_connection((host, port), timeout=5.0) as s:
            s.sendall(b"POST /api/name HTTP/1.0\r\nHost: localhost\r\n"
                      b"Content-Type: application/json\r\n\r\n")
            data = s.recv(4096)
        self.assertIn(b"411", data.split(b"\r\n", 1)[0])

    def test_name_malformed_content_length(self):
        # aiohttp's HTTP parser owns malformed Content-Length now.
        response = _raw_http(
            self.web.address,
            b"POST /api/name HTTP/1.0\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: abc\r\n\r\n",
        )
        self.assertEqual(_raw_status(response), 400)

    def test_name_invalid_json(self):
        status, _, body = _post(self.web.address, "/api/name", b"not json",
                                headers={"Content-Length": "8"})
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON", json.loads(body)["error"])

    def test_deeply_nested_json_is_400(self):
        # CPython's C accelerator parses this nesting without recursing. The pure
        # decoder, used when that accelerator is unavailable, raises RecursionError.
        from json.decoder import JSONDecoder
        from json.scanner import py_make_scanner

        decoder = JSONDecoder()
        decoder.scan_once = py_make_scanner(decoder)

        def loads(raw, **_kwargs):
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            return decoder.decode(text)

        depth = 800
        body = b'{"a":' * depth + b"1" + b"}" * depth
        self.assertLess(len(body), web.MAX_BODY)
        with self.assertRaises(RecursionError):
            loads(body)
        with mock.patch("hestia.web.json.loads", loads):
            status, _, payload = _post(self.web.address, "/api/name", body)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(payload)["error"], "invalid JSON")

    def test_name_save_failure_returns_500(self):
        with mock.patch.object(self.rt.registry, "write_payload", side_effect=OSError("disk")):
            status, _, body = _post(self.web.address, "/api/name", {"node": 5, "name": "x"})
        self.assertEqual(status, 500)
        self.assertIn("save failed", json.loads(body)["error"])

    def test_name_wrong_content_type_returns_415(self):     # CSRF guard: reject non-JSON content type
        status, _, body = _post(self.web.address, "/api/name", {"node": 5, "name": "x"},
                                headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])


class RoutingTests(_WebTestBase):
    def test_unknown_path_404(self):
        status, _, _ = _get(self.web.address, "/nope")
        self.assertEqual(status, 404)
        status, _, _ = _post(self.web.address, "/nope", b"{}", headers={"Content-Length": "2"})
        self.assertEqual(status, 404)

    def test_method_not_allowed_405_with_allow_header(self):
        status, headers, _ = _post(self.web.address, "/", b"{}", headers={"Content-Length": "2"})
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        status, headers, _ = _get(self.web.address, "/api/name")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")


class HttpContractTests(_WebTestBase):
    def assertJsonHeaders(self, headers):
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("Content-Length", headers)

    def assertEmptyHeaders(self, headers):
        self.assertEqual(headers["Content-Length"], "0")
        self.assertNotIn("Content-Type", headers)

    def test_response_headers_pin_json_html_and_empty_bodies(self):
        status, headers, _ = _get(self.web.address, "/api/discovery")
        self.assertEqual(status, 200)
        self.assertJsonHeaders(headers)

        status, headers, body = _post(self.web.address, "/api/name", {"node": 5, "name": "x"})
        self.assertEqual(status, 200)
        self.assertJsonHeaders(headers)
        self.assertEqual(json.loads(body), {"ok": True})

        status, headers, body = _post(self.web.address, "/api/name", {"node": 5})
        self.assertEqual(status, 400)
        self.assertJsonHeaders(headers)
        self.assertFalse(json.loads(body)["ok"])

        # The UI root serves the built index.html (a FileResponse) when a build is present.
        dist = self.tmp / "hdr-dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><h1>hestia</h1>", encoding="utf-8")
        self.web.stop()
        with mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(dist)}):
            self.web = _start_web(self.rt, self.loop_thread)
            status, headers, body = _get(self.web.address, "/")
            self.assertEqual(status, 200)
            self.assertTrue(headers["Content-Type"].startswith("text/html"))
            self.assertIn("Content-Length", headers)
            self.assertIn(b"<h1>hestia</h1>", body)

            status, headers, body = _get(self.web.address, "/nope")
            self.assertEqual(status, 404)
            self.assertEmptyHeaders(headers)
            self.assertEqual(body, b"")

            status, headers, body = _post(self.web.address, "/", b"{}", headers={"Content-Length": "2"})
            self.assertEqual(status, 405)
            self.assertEmptyHeaders(headers)
            self.assertEqual(body, b"")

    def test_405_allow_headers_are_exact(self):
        status, headers, _ = _post(self.web.address, "/", b"{}", headers={"Content-Length": "2"})
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")

        for path in ("/api/name", "/api/ir", "/api/control", "/api/automations/delete", "/api/graduate"):
            with self.subTest(path=path):
                status, headers, _ = _get(self.web.address, path)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "POST")

    def test_missing_content_length_is_411_on_representative_posts(self):
        for path in ("/api/control", "/api/automations"):
            with self.subTest(path=path):
                response = _raw_post(self.web.address, path, headers=(("Content-Type", "application/json"),))
                self.assertEqual(_raw_status(response), 411)
                self.assertEqual(json.loads(_raw_body(response)),
                                 {"ok": False, "error": "Content-Length required"})

    def test_oversized_bodies_are_413_for_endpoint_caps(self):
        cases = (
            ("/api/name", 8193, "body must be ≤ 8192 bytes"),
            ("/api/automations", 65537, "body must be ≤ 65536 bytes"),
            ("/api/automations/delete", 65537, "body must be ≤ 65536 bytes"),
        )
        for path, size, error in cases:
            with self.subTest(path=path):
                body = b"x" * size
                response = _raw_post(
                    self.web.address, path, body=body,
                    headers=(("Content-Type", "application/json"),
                             ("Content-Length", str(len(body)))))
                self.assertEqual(_raw_status(response), 413)
                self.assertEqual(json.loads(_raw_body(response)), {"ok": False, "error": error})

    def test_wrong_content_type_is_415_on_all_post_routes(self):
        for path in ("/api/name", "/api/ir", "/api/control", "/api/automations",
                     "/api/automations/delete", "/api/graduate"):
            with self.subTest(path=path):
                status, headers, body = _post(self.web.address, path, b"{}", headers={"Content-Type": "text/plain"})
                self.assertEqual(status, 415)
                self.assertJsonHeaders(headers)
                self.assertEqual(json.loads(body),
                                 {"ok": False, "error": "Content-Type must be application/json"})

    def test_post_to_get_only_routes_is_405_with_empty_body(self):
        for path in ("/api/discovery", "/api/events"):
            with self.subTest(path=path):
                status, headers, body = _post(self.web.address, path, {})
                # aiohttp routing: standard 405 for a known path + wrong method (was a stdlib quirk)
                self.assertEqual(status, 405)
                self.assertEmptyHeaders(headers)
                self.assertEqual(headers["Allow"], "GET")
                self.assertEqual(body, b"")

    def test_head_root_is_405(self):
        status, headers, body = _head(self.web.address, "/")
        # aiohttp routing: standard 405 for a known path + wrong method (was a stdlib quirk)
        self.assertEqual(status, 405)
        self.assertEmptyHeaders(headers)
        self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(body, b"")

    def test_non_content_length_parser_errors_keep_aiohttp_default(self):
        response = _raw_http(self.web.address, b"GET / HTTP/1.0\r\nBad Header\r\n\r\n")
        self.assertEqual(_raw_status(response), 400)
        self.assertIn(b"Content-Type: text/plain", response.split(b"\r\n\r\n", 1)[0])


# --- M3: automations CRUD ----------------------------------------------------

VALID_RULE = {
    "id": "lamp-on-scene",
    "trigger": {"type": "scene", "node": 5, "scene_id": 1},
    "actions": [{"op": "switch", "node": 7, "on": True}],
}


class _AutomationsWebTestBase(_WebTestBase):
    """A web fixture whose runtime has a temp-backed automations store, so rule
    CRUD persists to an isolated file rather than the cwd-default `automations.json`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "registry.json"
        self.store_path = self.tmp / "automations.json"
        self.loop_thread = _LoopThread()
        self.rt = proxy.ProxyRuntime(
            registry=proxy.Registry(self.path),
            engine=AutomationEngine(AutomationStore(self.store_path)))
        self.web = _start_web(self.rt, self.loop_thread)


class AutomationsListTests(_AutomationsWebTestBase):
    def test_list_empty(self):
        status, headers, body = _get(self.web.address, "/api/automations")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(body), {"ok": True, "automations": []})

    def test_list_returns_seeded_rule(self):
        status, _, _ = _post(self.web.address, "/api/automations", VALID_RULE)
        self.assertEqual(status, 200)
        _, _, body = _get(self.web.address, "/api/automations")
        rules = json.loads(body)["automations"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["id"], "lamp-on-scene")
        self.assertEqual(rules[0]["trigger"]["type"], "scene")


class AutomationsSetTests(_AutomationsWebTestBase):
    def test_set_valid_rule_returns_id(self):
        status, _, body = _post(self.web.address, "/api/automations", VALID_RULE)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "id": "lamp-on-scene"})

    def test_set_persists_across_reload(self):
        _post(self.web.address, "/api/automations", VALID_RULE)
        reloaded = AutomationStore.load(self.store_path)        # fresh read from disk
        self.assertIn("lamp-on-scene", reloaded.rules)
        self.assertEqual(reloaded.rules["lamp-on-scene"].trigger["scene_id"], 1)


class AuditFieldsTests(unittest.TestCase):
    """_audit_fields: an automation rule body is never recorded raw (no arbitrary-field capture)."""

    def test_automation_set_records_id_only_not_raw_body(self):
        op = {"op": "automation_set", "rule": {"id": "r1", "password": "hunter2",
                                               "trigger": {}, "actions": []}}
        target, detail = web._audit_fields(op)
        self.assertEqual((target, detail), ("r1", "rule update"))
        self.assertNotIn("hunter2", detail)   # an arbitrary field in the body never reaches the audit

    def test_automation_non_string_id_is_not_serialized(self):
        # a non-string id (object) must never be str()'d into target or dumped into detail
        s_target, s_detail = web._audit_fields({"op": "automation_set", "rule": {"id": {"password": "x"}}})
        d_target, d_detail = web._audit_fields({"op": "automation_delete", "id": {"password": "x"}})
        self.assertEqual((s_target, s_detail), (None, "rule update"))
        self.assertEqual((d_target, d_detail), (None, "rule delete"))
        self.assertNotIn("password", s_detail + d_detail)

    def test_automation_delete_records_string_id(self):
        self.assertEqual(web._audit_fields({"op": "automation_delete", "id": "r1"}), ("r1", "rule delete"))

    def test_control_op_records_known_params(self):
        target, detail = web._audit_fields({"op": "switch", "node": 14, "on": True})
        self.assertEqual(target, "14")
        self.assertIn('"on": true', detail)


class Rf433FeedTests(_WebTestBase):
    """GET /api/rf433 — the 433 MHz device-discovery feed."""

    def test_empty_by_default(self):
        status, _, body = _get(self.web.address, "/api/rf433")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"devices": []})

    def test_returns_recorded_devices(self):
        self.rt.rf433.record({"model": "Acme-Doorbell", "id": 7, "code": "abc"}, now=100.0)
        status, _, body = _get(self.web.address, "/api/rf433")
        self.assertEqual(status, 200)
        devices = json.loads(body)["devices"]
        self.assertEqual(devices[0]["key"], "Acme-Doorbell 7")
        self.assertEqual(devices[0]["fields"]["code"], "abc")


class AuditFeedTests(_WebTestBase):
    """GET /api/audit — the audit-log feed (#56)."""

    def test_empty_without_audit_engine(self):
        status, _, body = _get(self.web.address, "/api/audit")     # base rt has audit_engine=None
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"events": []})

    def test_returns_recent_events(self):
        from hestia import db, store_sql
        engine, _ = db.init_db(self.tmp / "hestia.db")
        store_sql.append_audit(engine, actor="tata", action="ir", target="/k.ir", result="ok", ts=100.0)
        self.rt.audit_engine = engine          # rt is shared with the running web; read at request time
        status, _, body = _get(self.web.address, "/api/audit")
        self.assertEqual(status, 200)
        events = json.loads(body)["events"]
        self.assertEqual((events[0]["actor"], events[0]["action"], events[0]["target"]), ("tata", "ir", "/k.ir"))
        engine.dispose()

    def test_set_replaces_same_id(self):
        _post(self.web.address, "/api/automations", VALID_RULE)
        updated = dict(VALID_RULE, actions=[{"op": "switch", "node": 7, "on": False}])
        status, _, _ = _post(self.web.address, "/api/automations", updated)
        self.assertEqual(status, 200)
        _, _, body = _get(self.web.address, "/api/automations")
        rules = json.loads(body)["automations"]
        self.assertEqual(len(rules), 1)                          # replaced, not appended
        self.assertIs(rules[0]["actions"][0]["on"], False)

    def test_set_invalid_trigger_returns_400(self):
        status, _, body = _post(self.web.address, "/api/automations",
                                {"id": "bad", "trigger": {"type": "nope"},
                                 "actions": [{"op": "switch", "node": 1, "on": True}]})
        self.assertEqual(status, 400)
        self.assertIn("trigger.type", json.loads(body)["error"])

    def test_set_empty_actions_returns_400(self):
        status, _, body = _post(self.web.address, "/api/automations",
                                {"id": "x", "trigger": {"type": "scene", "node": 1, "scene_id": 0},
                                 "actions": []})
        self.assertEqual(status, 400)
        self.assertIn("actions", json.loads(body)["error"])

    def test_set_non_dict_body_returns_400(self):
        status, _, body = _post(self.web.address, "/api/automations", [1, 2, 3])
        self.assertEqual(status, 400)
        self.assertIn("must be an object", json.loads(body)["error"])

    def test_set_oversized_body_returns_413(self):           # err=True branch on the set handler
        big = dict(VALID_RULE, id="x" * 70000)               # body well over MAX_RULE_BODY
        status, _, _ = _post(self.web.address, "/api/automations", big)
        self.assertEqual(status, 413)

    def test_set_save_failure_returns_500(self):
        with mock.patch.object(self.rt.engine.store, "write_payload", side_effect=OSError("disk")):
            status, _, body = _post(self.web.address, "/api/automations", VALID_RULE)
        self.assertEqual(status, 500)
        self.assertIn("save failed", json.loads(body)["error"])

    def test_set_wrong_content_type_returns_415(self):      # CSRF guard
        status, _, body = _post(self.web.address, "/api/automations", VALID_RULE,
                                headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])


class DbStatsTests(_WebTestBase):
    """GET /api/db/stats — operator SQLite growth stats."""

    def test_returns_file_size_and_table_counts(self):
        from hestia import db
        db_file = self.tmp / "hestia.db"
        engine, Session = db.init_db(db_file)
        try:
            with db.session_scope(Session) as s:
                s.add(db.Node(key="7", entry_json="{}"))
                s.add(db.User(username="tata", password_hash="scrypt$abc"))
                s.add(db.UserSetting(username="tata", locale="pl", temp_scale="c", theme=None))

            with mock.patch.dict("os.environ", {"HESTIA_DB": str(db_file)}):
                status, _, body = _get(self.web.address, "/api/db/stats")

            stats = json.loads(body)
            self.assertEqual(status, 200)
            self.assertGreater(stats["file_bytes"], 0)
            self.assertEqual(stats["tables"]["nodes"], 1)
            self.assertEqual(stats["tables"]["users"], 1)
            self.assertEqual(stats["tables"]["user_settings"], 1)
            self.assertEqual(stats["tables"]["audit"], 0)
        finally:
            engine.dispose()


class AutomationsDeleteTests(_AutomationsWebTestBase):
    def _seed(self):
        status, _, _ = _post(self.web.address, "/api/automations", VALID_RULE)
        self.assertEqual(status, 200)

    def test_delete_existing_returns_deleted_true(self):
        self._seed()
        status, _, body = _post(self.web.address, "/api/automations/delete", {"id": "lamp-on-scene"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "deleted": True})
        _, _, listing = _get(self.web.address, "/api/automations")
        self.assertEqual(json.loads(listing)["automations"], [])

    def test_delete_absent_returns_deleted_false(self):
        status, _, body = _post(self.web.address, "/api/automations/delete", {"id": "ghost"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "deleted": False})

    def test_delete_non_string_id_returns_400(self):
        status, _, body = _post(self.web.address, "/api/automations/delete", {"id": 5})
        self.assertEqual(status, 400)
        self.assertIn("must be a string", json.loads(body)["error"])

    def test_delete_missing_id_returns_400(self):            # {} → rid=None → ValueError
        status, _, body = _post(self.web.address, "/api/automations/delete", {})
        self.assertEqual(status, 400)
        self.assertIn("must be a string", json.loads(body)["error"])

    def test_delete_non_dict_body_returns_400(self):         # isinstance guard → rid=None → 400
        status, _, body = _post(self.web.address, "/api/automations/delete", [1, 2, 3])
        self.assertEqual(status, 400)
        self.assertIn("must be a string", json.loads(body)["error"])

    def test_delete_invalid_json_returns_400(self):          # err=True branch on the delete handler
        status, _, body = _post(self.web.address, "/api/automations/delete", b"not json",
                                headers={"Content-Length": "8"})
        self.assertEqual(status, 400)
        self.assertIn("invalid JSON", json.loads(body)["error"])

    def test_delete_empty_body_cl0_returns_400(self):        # CL=0 → {} (err=False) → rid=None → 400
        status, _, body = _post(self.web.address, "/api/automations/delete", b"",
                                headers={"Content-Length": "0"})
        self.assertEqual(status, 400)
        self.assertIn("must be a string", json.loads(body)["error"])

    def test_delete_save_failure_returns_500(self):          # seeded → the write path is reached
        self._seed()
        with mock.patch.object(self.rt.engine.store, "write_payload", side_effect=OSError("disk")):
            status, _, body = _post(self.web.address, "/api/automations/delete", {"id": "lamp-on-scene"})
        self.assertEqual(status, 500)
        self.assertIn("save failed", json.loads(body)["error"])

    def test_delete_wrong_content_type_returns_415(self):   # CSRF guard
        status, _, body = _post(self.web.address, "/api/automations/delete", {"id": "x"},
                                headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(body)["error"])


class AutomationsRoutingTests(_AutomationsWebTestBase):
    def test_get_on_delete_path_405(self):
        status, headers, _ = _get(self.web.address, "/api/automations/delete")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")

class BindGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.loop_thread = _LoopThread()
        self.rt = proxy.ProxyRuntime(registry=proxy.Registry(self.tmp / "registry.json"))

    def tearDown(self):
        self.loop_thread.close()
        shutil.rmtree(self.tmp)

    def test_remote_bind_refused_without_optin(self):
        import os
        os.environ.pop("HESTIA_WEB_ALLOW_REMOTE", None)
        with self.assertRaises(RuntimeError):
            _start_web(self.rt, self.loop_thread, "8.8.8.8", 0)

    def test_remote_bind_allowed_with_optin(self):
        # Bind 0.0.0.0:0 (any free port) with the env opt-in; immediately stop.
        with mock.patch.dict("os.environ", {"HESTIA_WEB_ALLOW_REMOTE": "1"}):
            started = _start_web(self.rt, self.loop_thread, "0.0.0.0", 0)
        try:
            self.assertEqual(started.address[0], "0.0.0.0")
            self.assertGreater(started.address[1], 0)
        finally:
            started.stop()

    def test_start_web_falls_back_to_env_defaults(self):
        """Caller omits host/port → start_web reads `HESTIA_WEB_HOST` and
        `HESTIA_WEB_PORT` (the production wire-up uses ProxyConfig defaults that
        come from the same env vars, so this path needs coverage too)."""
        with mock.patch.dict("os.environ", {"HESTIA_WEB_HOST": "127.0.0.1",
                                            "HESTIA_WEB_PORT": "0"}):
            started = _start_web(self.rt, self.loop_thread, None, None)
        try:
            self.assertEqual(started.address[0], "127.0.0.1")
            self.assertGreater(started.address[1], 0)
        finally:
            started.stop()

# --- SSE endpoint -----------------------------------------------------------

class SSEHandlerTests(_WebTestBase):
    def setUp(self):
        super().setUp()
        self.cleanup_event = threading.Event()
        self._cleanup_event_lock = threading.Lock()
        self._cleanup_event_set = False
        original_close = proxy.Subscription.close

        def close_and_signal(sub):
            was_open = sub.queue is not None
            result = original_close(sub)
            if was_open:
                with self._cleanup_event_lock:
                    if not self._cleanup_event_set:
                        self._cleanup_event_set = True
                        self.cleanup_event.set()
            return result

        patcher = mock.patch.object(proxy.Subscription, "close", new=close_and_signal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cap_reached_returns_429(self):
        with mock.patch.object(self.rt.event_bus, "try_subscribe",
                               new=mock.AsyncMock(return_value=None)):
            status, _, _ = _get(self.web.address, "/api/events")
        self.assertEqual(status, 429)

    def test_head_events_is_405_and_does_not_subscribe(self):
        status, headers, body = _head(self.web.address, "/api/events")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(body, b"")
        self.assertFalse(self.rt.event_bus._subs)

    def test_stream_pushes_event_and_keepalive(self):
        """End-to-end SSE: subscribe, publish from the loop, read framed event,
        verify activity payload + that the connection stays open afterwards."""
        s = self._raw_sse()
        try:
            data = _recv_until(s, b"\r\n\r\n")
            head, _, body = data.partition(b"\r\n\r\n")
            self.assertIn(b"HTTP/1.0 200 OK", head.split(b"\r\n", 1)[0])
            self.assertIn(b"Content-Type: text/event-stream", head)
            self.assertIn(b"Cache-Control: no-cache", head)
            self.assertIn(b"Connection: keep-alive", head)
            self.assertIn(b"X-Accel-Buffering: no", head)
            self.assertNotIn(b"Content-Length:", head)
            self._wait_subscribed()
            self.loop_thread.submit(self._publish_async({"type": "activity", "node": 5, "ts": 1.0}))
            if b"\n\n" not in body:
                body += _recv_until(s, b"\n\n")
            event_block = body.split(b"\n\n", 1)[0]
            self.assertTrue(event_block.startswith(b"data: "))
            payload = json.loads(event_block[len(b"data: "):].decode())
            self.assertEqual(payload["node"], 5)
            self.loop_thread.submit(self._close_bus_async())
            self._wait_unsubscribed()
        finally:
            s.close()

    def test_idle_stream_emits_keepalive(self):
        with mock.patch.object(web, "SSE_IDLE_TIMEOUT", 0.01):
            s = self._raw_sse()
            try:
                data = _recv_until(s, b"\r\n\r\n")
                head, _, body = data.partition(b"\r\n\r\n")
                self.assertIn(b"HTTP/1.0 200 OK", head.split(b"\r\n", 1)[0])
                self._wait_subscribed()
                if b":keepalive\n\n" not in body:
                    body += _recv_until(s, b":keepalive\n\n")
                self.assertIn(b":keepalive\n\n", body)
            finally:
                s.close()
            self._wait_unsubscribed()

    async def _publish_async(self, event):
        self.rt.event_bus.publish(event)

    def _wait_subscribed(self, n=1, timeout=0.5):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.rt.event_bus._subs) >= n: return
            time.sleep(0.005)
        self.fail(f"bus did not see {n} subscribers within {timeout}s")

    def _wait_unsubscribed(self, timeout=5.0):
        if self.cleanup_event.wait(timeout=timeout):
            self.assertFalse(self.rt.event_bus._subs)
            return
        self.fail(f"subscription close was not observed within {timeout}s")

    async def _close_bus_async(self):
        self.rt.event_bus.close()

    def test_stream_breaks_on_closed_sentinel(self):
        """`EventBus.close()` from the loop wakes the handler via sentinel."""
        s = self._raw_sse()
        try:
            _recv_until(s, b"\r\n\r\n")
            self._wait_subscribed()
            self.loop_thread.submit(self._close_bus_async())   # pushes sentinel
            self._wait_unsubscribed()
            self.assertEqual(s.recv(4096), b"")                # clean EOF
        finally:
            s.close()

    def test_lifetime_cap_exits_handler(self):
        """A negative `SSE_MAX_LIFETIME` makes the deadline past at handler
        entry — the while-loop never iterates, cleanup runs immediately."""
        with mock.patch("hestia.web.SSE_MAX_LIFETIME", -1.0):
            host, port = self.web.address
            conn = http.client.HTTPConnection(host, port, timeout=2.0)
            conn.request("GET", "/api/events")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            try: eof = resp.fp.readline()
            except (TimeoutError, OSError): eof = b""
            conn.close()
        self._wait_unsubscribed()

    def _raw_sse(self, path="/api/events"):
        """Plain socket SSE so we control the close ourselves (http.client
        hides the socket once getresponse() runs)."""
        import socket
        host, port = self.web.address
        s = socket.create_connection((host, port), timeout=3.0)
        s.send(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        return s

    def test_oserror_on_write_breaks_loop(self):
        """Client disconnect → server's next write fails → handler exits."""
        s = self._raw_sse()
        self._wait_subscribed()
        s.close()                                           # break the pipe
        self.loop_thread.submit(self._publish_async({"type": "activity", "node": 6}))
        self._wait_unsubscribed()

    def test_stop_with_open_sse_returns_promptly_after_bus_close(self):
        s = self._raw_sse()
        try:
            _recv_until(s, b"\r\n\r\n")
            self._wait_subscribed()
            started = time.monotonic()
            self.loop_thread.submit(self._close_bus_async())
            self.web.stop()
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.5)
            self._wait_unsubscribed()
        finally:
            s.close()


class RoleHelpersTests(unittest.TestCase):
    """Pure RBAC helpers (#73): the route→floor map (incl. fail-closed fallbacks) and the rank compare."""

    def test_required_role_classified_routes(self):
        self.assertEqual(web._required_role("GET", "/api/discovery"), "viewer")
        self.assertEqual(web._required_role("GET", "/api/calendars/instances"), "viewer")
        self.assertEqual(web._required_role("POST", "/api/settings"), "viewer")
        self.assertEqual(web._required_role("POST", "/api/control"), "operator")
        self.assertEqual(web._required_role("POST", "/api/automations"), "admin")
        self.assertEqual(web._required_role("GET", "/api/automations"), "admin")  # leaks presence MACs → admin

    def test_required_role_unclassified_write_is_admin(self):
        self.assertEqual(web._required_role("POST", "/api/brand-new"), "admin")   # fail-closed
        self.assertEqual(web._required_role("DELETE", "/x"), "admin")

    def test_required_role_unclassified_read_is_admin(self):
        self.assertEqual(web._required_role("GET", "/api/brand-new"), "admin")
        self.assertEqual(web._required_role("HEAD", "/x"), "admin")
        self.assertEqual(web._required_role("OPTIONS", "/x"), "admin")

    def test_role_allows_hierarchy(self):
        self.assertTrue(web._role_allows("admin", "viewer"))
        self.assertTrue(web._role_allows("admin", "admin"))
        self.assertTrue(web._role_allows("operator", "viewer"))
        self.assertTrue(web._role_allows("operator", "operator"))
        self.assertFalse(web._role_allows("viewer", "operator"))
        self.assertFalse(web._role_allows("operator", "admin"))

    def test_role_allows_unknown_or_none_role_denied(self):
        self.assertFalse(web._role_allows("superuser", "viewer"))   # unknown role → fail closed
        self.assertFalse(web._role_allows(None, "viewer"))


# Every registered method and path for a viewer with a household grant.
# Owner budget routes stay 403, including the POST that reads transactions by key.
SPOUSE_ROUTE_STATUS = {
    ("GET", "/"): 404,
    ("GET", "/api/auth/features"): 200,
    ("GET", "/api/me/capabilities"): 200,
    ("GET", "/api/audit"): 200,
    ("GET", "/api/automations"): 403,
    ("GET", "/api/budget/accounts"): 403,
    ("GET", "/api/budget/analytics/categories"): 403,
    ("GET", "/api/budget/analytics/forecast"): 403,
    ("GET", "/api/budget/analytics/inbox"): 403,
    ("GET", "/api/budget/analytics/monthly-budget"): 403,
    ("GET", "/api/budget/analytics/monthly-budget/charts"): 403,
    ("GET", "/api/budget/analytics/mortgages"): 403,
    ("GET", "/api/budget/analytics/overview"): 403,
    ("GET", "/api/budget/analytics/quarantine"): 403,
    ("GET", "/api/budget/analytics/recurring"): 403,
    ("GET", "/api/budget/analytics/rules"): 403,
    ("GET", "/api/budget/analytics/spending-limits"): 403,
    ("GET", "/api/budget/analytics/transactions"): 403,
    ("GET", "/api/budget/analytics/transfers"): 403,
    ("GET", "/api/budget/balances"): 403,
    ("GET", "/api/budget/connections"): 403,
    ("GET", "/api/budget/consent/pending"): 403,
    ("GET", "/api/budget/consent/reconcile"): 403,
    ("GET", "/api/budget/consent/renewal-reminders"): 403,
    ("GET", "/api/budget/consent/status"): 403,
    ("GET", "/api/budget/enable-banking/callback"): 303,
    ("GET", "/api/budget/household/access"): 200,
    ("GET", "/api/budget/household/categories"): 200,
    ("GET", "/api/budget/household/inbox"): 200,
    ("GET", "/api/budget/household/monthly-budget"): 200,
    ("GET", "/api/budget/household/monthly-budget-settings"): 200,
    ("GET", "/api/budget/household/mortgages"): 200,
    ("GET", "/api/budget/household/overview"): 200,
    ("GET", "/api/budget/household/quarantine"): 200,
    ("GET", "/api/budget/household/recurring"): 200,
    ("GET", "/api/budget/household/report-accounts"): 200,
    ("GET", "/api/budget/household/rules"): 200,
    ("GET", "/api/budget/household/spending-limits"): 200,
    ("GET", "/api/budget/household/transactions"): 200,
    ("GET", "/api/budget/household/transfers"): 200,
    ("GET", "/api/budget/jobs"): 403,
    ("GET", "/api/budget/limits"): 403,
    ("GET", "/api/budget/status"): 403,
    ("GET", "/api/budget/transactions"): 403,
    ("GET", "/api/calendars"): 200,
    ("GET", "/api/calendars/instances"): 400,
    ("GET", "/api/db/stats"): 403,
    ("GET", "/api/discovery"): 200,
    ("GET", "/api/events"): 200,
    ("GET", "/api/rf433"): 403,
    ("GET", "/api/rooms/icons"): 200,
    ("GET", "/api/settings"): 200,
    ("GET", "/api/users"): 403,
    ("GET", "/api/users/grants"): 403,
    ("GET", "/api/whoami"): 200,
    ("GET", "/api/whole-home"): 403,
    ("GET", "/assets/asset.js"): 404,
    ("GET", "/budget/connect/complete"): 200,
    ("GET", "/budget/connect/complete.js"): 200,
    ("GET", "/privacy-policy.html"): 404,
    ("GET", "/terms-of-service.html"): 404,
    ("GET", "/ui/"): 302,
    ("HEAD", "/privacy-policy.html"): 404,
    ("HEAD", "/terms-of-service.html"): 404,
    ("POST", "/api/automations"): 403,
    ("POST", "/api/automations/delete"): 403,
    ("POST", "/api/budget/account-kind"): 403,
    ("POST", "/api/budget/account-credit-limit"): 403,
    ("POST", "/api/budget/account-selection"): 403,
    ("POST", "/api/budget/analytics/annotation"): 403,
    ("POST", "/api/budget/analytics/category-create"): 403,
    ("POST", "/api/budget/analytics/category-update"): 403,
    ("POST", "/api/budget/analytics/category-colors-spread"): 403,
    ("POST", "/api/budget/analytics/category-translation-set"): 403,
    ("POST", "/api/budget/analytics/category-translation-delete"): 403,
    ("POST", "/api/budget/analytics/label-translation-set"): 403,
    ("POST", "/api/budget/analytics/label-translation-delete"): 403,
    ("POST", "/api/budget/analytics/translation-import"): 403,
    ("POST", "/api/budget/analytics/inbox-mark"): 403,
    ("POST", "/api/budget/analytics/mortgage-payment-set"): 403,
    ("POST", "/api/budget/analytics/mortgage-save"): 403,
    ("POST", "/api/budget/analytics/rule-batch-preview"): 403,
    ("POST", "/api/budget/analytics/rule-batch-save"): 403,
    ("POST", "/api/budget/analytics/rule-preview"): 403,
    ("POST", "/api/budget/analytics/rule-save"): 403,
    ("POST", "/api/budget/analytics/spending-limit-save"): 403,
    ("POST", "/api/budget/analytics/transactions-by-keys"): 403,
    ("POST", "/api/budget/receipts/parse-text"): 403,
    ("POST", "/api/budget/receipts/confirm"): 403,
    ("POST", "/api/budget/receipts/list"): 403,
    ("POST", "/api/budget/receipts/search"): 403,
    ("GET", "/api/budget/receipts/detail"): 403,
    ("GET", "/api/budget/receipts/group/detail"): 403,
    ("POST", "/api/budget/receipts/group/candidates"): 403,
    ("POST", "/api/budget/receipts/group/link"): 403,
    ("POST", "/api/budget/receipts/group/unlink"): 403,
    ("GET", "/api/budget/receipts/image"): 403,
    ("POST", "/api/budget/receipts/delete"): 403,
    ("POST", "/api/budget/receipts/device-token/pair-code"): 403,
    ("POST", "/api/budget/receipts/device-token/pair-code-cancel"): 403,
    ("POST", "/api/budget/receipts/device-token/login"): 400,
    ("POST", "/api/budget/receipts/device-token/redeem"): 403,
    ("POST", "/api/budget/receipts/device-token/list"): 403,
    ("POST", "/api/budget/receipts/device-token/revoke"): 403,

    ("GET", "/api/budget/receipts/gmail-status"): 403,
    ("POST", "/api/budget/receipts/gmail-grant"): 403,
    ("POST", "/api/budget/receipts/gmail-revoke"): 403,
    ("POST", "/api/budget/receipts/gmail-sync"): 403,
    ("POST", "/api/budget/analytics/transfer-decision"): 403,
    ("POST", "/api/budget/background-selection"): 403,
    ("POST", "/api/budget/connection-state"): 403,
    ("POST", "/api/budget/consent/cancel"): 403,
    ("POST", "/api/budget/consent/complete"): 403,
    ("POST", "/api/budget/consent/start"): 403,
    ("POST", "/api/budget/refresh"): 403,
    ("POST", "/api/calendars/events"): 403,
    ("POST", "/api/calendars/events/delete"): 403,
    ("POST", "/api/control"): 403,
    ("POST", "/api/graduate"): 403,
    ("POST", "/api/ir"): 403,
    ("POST", "/api/login"): 401,
    ("POST", "/api/logout"): 200,
    ("POST", "/api/auth/qr-code"): 503,
    ("POST", "/api/auth/qr-redeem"): 400,
    ("POST", "/api/auth/device-token"): 401,
    ("POST", "/api/me/password"): 400,
    ("POST", "/api/name"): 403,
    ("POST", "/api/rooms/icons"): 403,
    ("POST", "/api/scene"): 403,
    ("POST", "/api/settings"): 200,
    ("POST", "/api/users"): 403,
    ("POST", "/api/users/grants"): 403,
    ("POST", "/api/users/disabled"): 403,
    ("POST", "/api/users/reset-password"): 403,
    ("POST", "/api/users/role"): 403,
    ("POST", "/api/whole-home"): 403,
}

OPERATOR_ROUTE_STATUS = SPOUSE_ROUTE_STATUS | {
    ("POST", "/api/calendars/events"): 400,
    ("POST", "/api/calendars/events/delete"): 400,
    ("POST", "/api/control"): 503,
    ("POST", "/api/ir"): 503,
    ("POST", "/api/scene"): 200,
}


class RoutePolicyTests(unittest.TestCase):
    """Completeness guard: every route make_app registers must be either public or have a role floor, so
    the policy map can't silently drift from the route table (a new route fails this until classified)."""

    def test_every_registered_route_is_public_or_classified(self):
        app = web.make_app(SimpleNamespace())
        for route in app.router.routes():
            canonical = route.resource.canonical
            path = canonical.replace("{path}", "asset.js") if "{path}" in canonical else canonical
            classified = (web._is_public_route(route.method, path)
                          or (route.method, path) in web._ROUTE_REQUIREMENTS)
            self.assertTrue(classified, f"unclassified route: {route.method} {path}")


class AuthzTests(_WebTestBase):
    """RBAC floor enforcement (#73): the auth middleware resolves the role per request and gates each
    route by its floor. A removed account 401s immediately; an unclassified route is admin-only."""

    def setUp(self):
        super().setUp()
        self.users = {"u": auth.hash_password("pw")}
        self.role = "admin"   # what current_user_role returns; each request flips it via _get/_post
        patches = [
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", b"test-secret-bytes"),
            mock.patch.object(web, "_COOKIE_SECURE", False),
            mock.patch.object(auth, "load_users", return_value=self.users),
            mock.patch.object(
                store_sql,
                "load_auth_state",
                side_effect=lambda _user: store_sql.AuthState(
                    backend="json",
                    session_phase="legacy",
                    policy_phase="legacy",
                    enabled=self.role is not None,
                    role=self.role,
                    generation=0,
                ),
            ),
            mock.patch.dict(os.environ, {"HESTIA_DB": str(self.tmp / "authz.db")}),  # keep db-stats off /data
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(db.reset_engine_cache)
        _, headers, _ = _post(self.web.address, "/api/login", {"user": "u", "password": "pw"})
        self.cookie = AuthTests._cookie(headers["Set-Cookie"])

    def _get(self, path, role):
        self.role = role
        return _get(self.web.address, path, headers={"Cookie": self.cookie})[0]

    def _post(self, path, role, body=None):
        self.role = role
        return _post(self.web.address, path, body if body is not None else {},
                     headers={"Cookie": self.cookie})[0]

    def test_viewer_reads_but_cannot_control_or_see_admin_surface(self):
        self.assertEqual(self._get("/api/discovery", "viewer"), 200)
        self.assertEqual(self._get("/api/audit", "viewer"), 200)
        self.assertEqual(self._post("/api/settings", "viewer", {"locale": "pl"}), 200)   # own UI prefs
        self.assertEqual(self._post("/api/control", "viewer", {"op": "switch", "node": 5, "on": True}), 403)
        self.assertEqual(self._post("/api/scene", "viewer", {"op": "lights_off"}), 403)
        self.assertEqual(self._get("/api/automations", "viewer"), 403)   # admin-only read (leaks MACs)
        self.assertEqual(self._get("/api/db/stats", "viewer"), 403)
        self.assertEqual(self._get("/api/rf433", "viewer"), 403)
        self.assertEqual(self._get("/api/whole-home", "viewer"), 403)    # whole-home config is admin-only
        self.assertEqual(self._post("/api/whole-home", "viewer", {"node": 5, "exclude": True}), 403)

    def test_operator_controls_but_not_the_admin_surface(self):
        self.assertEqual(self._post("/api/control", "operator", {"op": "switch", "node": 5, "on": True}), 503)
        self.assertEqual(self._post("/api/scene", "operator", {"op": "lights_off"}), 200)
        self.assertEqual(self._post("/api/automations", "operator", {}), 403)   # rule editing is admin-only
        self.assertEqual(self._post("/api/automations/delete", "operator", {"id": "x"}), 403)
        self.assertEqual(self._post("/api/name", "operator", {"node": 5, "name": "x"}), 403)
        self.assertEqual(self._post("/api/whole-home", "operator", {"node": 5, "exclude": True}), 403)
        self.assertEqual(self._post("/api/graduate", "operator"), 403)
        self.assertEqual(self._post("/api/rooms/icons", "operator", {"room": "x", "icon": "🚪"}), 403)
        self.assertEqual(self._get("/api/automations", "operator"), 403)

    def test_admin_clears_every_floor(self):
        self.assertEqual(self._get("/api/automations", "admin"), 200)
        self.assertEqual(self._get("/api/db/stats", "admin"), 200)
        self.assertNotEqual(self._post("/api/name", "admin", {"node": 5, "name": "x"}), 403)
        self.assertEqual(self._get("/api/whole-home", "admin"), 200)
        self.assertEqual(self._post("/api/whole-home", "admin", {"node": 5, "exclude": True}), 200)

    def test_removed_account_is_401_everywhere(self):
        self.assertEqual(self._get("/api/discovery", None), 401)   # current_user_role → None → cookie moot

    def test_unclassified_write_route_is_admin_only(self):
        # POST to a GET-only path → fail-closed to admin: a viewer 403s, an admin passes the floor (405 route)
        self.assertEqual(self._post("/api/discovery", "viewer"), 403)
        self.assertEqual(self._post("/api/discovery", "admin"), 405)

    def test_unclassified_read_route_is_admin_only(self):
        # No route and no role entry. A viewer is denied; an admin passes the floor and routing returns 404.
        self.assertEqual(self._get("/api/does-not-exist", "viewer"), 403)
        self.assertEqual(self._get("/api/does-not-exist", "operator"), 403)
        self.assertEqual(self._get("/api/does-not-exist", "admin"), 404)

    def test_receipt_redeem_rejects_admin_and_removed_account_cookies(self):
        with mock.patch.dict(os.environ, {"HESTIA_BUDGET_HTTP_ENABLED": "1", "HESTIA_BUDGET_RECEIPTS_ENABLED": "1"}):
            for role in ("admin", None):
                self.role = role
                status, _, body = _post(self.web.address, "/api/budget/receipts/device-token/redeem", {},
                                        headers={"Cookie": self.cookie})
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"ok": False, "error": "device_token_rejected"})


class HouseholdRouteTests(_WebTestBase):
    def test_household_member_status_is_classified_for_every_route(self):
        """Both granted roles get the same budget reads and each role's usual house controls."""
        self.addCleanup(db.reset_engine_cache)
        database = str(self.tmp / "household.db")
        roles = {"viewer": SPOUSE_ROUTE_STATUS, "operator": OPERATOR_ROUTE_STATUS}
        flags = {
            "HESTIA_DB": database,
            "HESTIA_PERSIST": "sqlite",
            "HESTIA_BUDGET_HTTP_ENABLED": "1",
            "HESTIA_BUDGET_ANALYTICS_ENABLED": "1",
            "HESTIA_BUDGET_RECEIPTS_ENABLED": "1",
            "HESTIA_BUDGET_MONTH_CONFIG": "",
            "HESTIA_BUDGET_CONSENT_ENABLED": "0",
        }
        secret = b"test-secret-bytes"
        with mock.patch.dict(os.environ, flags), mock.patch.multiple(web, _AUTH_ENABLED=True, _SESSION_SECRET=secret):
            engine, sessions = db.init_db()
            store_sql.cutover_users(engine, {"owner": "unused"})
            with sessions.begin() as session:
                session.add_all(db.User(username=role, role=role, password_hash="unused") for role in roles)
            routes = list(web.make_app(SimpleNamespace()).router.routes())
            for role, expected in roles.items():
                with self.subTest(role=role):
                    grant_id = household.grant_member(role, "owner", path=database)["grant_id"]
                    token = auth.make_session(role, now=web._now(), secret=secret)
                    actual = {}
                    for route in routes:
                        method = route.method
                        path = route.resource.canonical.replace("{path}", "asset.js")
                        self.assertNotIn("{", path, path)
                        public = web._is_public_route(method, path)
                        self.assertTrue(public or (method, path) in web._ROUTE_REQUIREMENTS,
                                        f"unclassified route: {method} {path}")
                        status, body = self._member_status(method, path, token, grant_id)
                        actual[(method, path)] = status
                        if method == "GET" and path.startswith(household_api.PREFIX + "/"):
                            self.assertEqual(status, 200, (path, body))
                            self.assertEqual(json.loads(body)["grant_id"], grant_id, path)
                        elif path.startswith("/api/budget/") and not public:
                            self.assertEqual((status, body), (403, b""), path)
                    self.assertEqual(actual, expected)

    def _member_status(self, method, path, token, grant_id):
        headers = {household_api.GRANT_HEADER: grant_id}
        if path.startswith("/api/budget/"):
            headers["Cookie"] = f"{web._SESSION_COOKIE}={token}"
        else:
            headers["Authorization"] = f"Bearer {token}"
        body = None
        if method in web._MUTATING_METHODS:
            controls = {
                "/api/control": {"op": "switch", "node": 5, "on": True},
                "/api/ir": {"file": "/k.ir", "button": "Power"},
                "/api/scene": {"op": "lights_off"},
            }
            headers["Content-Type"] = "application/json"
            body = json.dumps(controls.get(path, {})).encode()
        if path in {household_api.PREFIX + "/" + view for view in
                    ("overview", "transactions", "transfers", "spending-limits")}:
            path += "?period=2024-01"
        conn = http.client.HTTPConnection(*self.web.address, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            body = b"" if path == "/api/events" else response.read()
            return status, body
        finally:
            conn.close()


class UserMgmtValidatorTests(unittest.TestCase):
    """The pure validators + the store-outcome→HTTP mapper for #PR-D user management (no server)."""

    def test_username_error(self):
        self.assertIsNone(web._username_error("mama"))
        self.assertIsNotNone(web._username_error(123))        # not a string
        self.assertIsNotNone(web._username_error(""))         # empty
        self.assertIsNotNone(web._username_error("x" * 65))   # too long
        self.assertIsNotNone(web._username_error("a|b"))      # '|' would corrupt the session token
        self.assertIsNotNone(web._username_error("a/b"))      # '/'

    def test_password_error(self):
        self.assertIsNone(web._password_error("longenough"))
        self.assertIsNotNone(web._password_error(1234))           # not a string
        self.assertIsNotNone(web._password_error("short"))        # < 8 chars
        self.assertIsNotNone(web._password_error("x" * 1025))     # > max

    def test_user_outcome_mapping(self):
        self.assertIsNone(web._user_outcome("ok"))                # success → no error response
        self.assertEqual(web._user_outcome("not_found").status, 404)
        self.assertEqual(web._user_outcome("exists").status, 409)
        self.assertEqual(web._user_outcome("last_admin").status, 409)
        self.assertEqual(web._user_outcome("???").status, 500)    # unexpected store result → 500


class UserMgmtTests(_WebTestBase):
    """#PR-D user management endpoints, against a real sqlite-authoritative users DB with an
    authenticated ADMIN session (the middleware resolves the real role from the DB)."""

    def setUp(self):
        super().setUp()
        self.db_file = self.tmp / "users.db"
        self.env = mock.patch.dict(os.environ, {"HESTIA_PERSIST": "sqlite", "HESTIA_DB": str(self.db_file)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, _ = db.init_db(self.db_file)
        store_sql.cutover_users(engine, {"admin": auth.hash_password("adminpw")})   # admin → admin, authoritative
        engine.dispose()
        store_sql.set_user_db("op1", auth.hash_password("oppw"), "operator")
        store_sql.set_user_db("viewer1", auth.hash_password("vpw"), "viewer")
        for p in (mock.patch.object(web, "_AUTH_ENABLED", True),
                  mock.patch.object(web, "_SESSION_SECRET", b"test-secret-bytes"),
                  mock.patch.object(web, "_COOKIE_SECURE", False)):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self._login("admin", "adminpw")

    def _login(self, user, password):
        _, headers, _ = _post(self.web.address, "/api/login", {"user": user, "password": password})
        return AuthTests._cookie(headers["Set-Cookie"])

    def _p(self, path, body, cookie=None):
        return _post(self.web.address, path, body, headers={"Cookie": cookie or self.cookie})[0]

    def _g(self, path, cookie=None):
        return _get(self.web.address, path, headers={"Cookie": cookie or self.cookie})

    # ---- GET /api/users ----
    def test_list_users_admin_sees_metadata_without_hashes(self):
        status, _, body = self._g("/api/users")
        self.assertEqual(status, 200)
        users = json.loads(body)["users"]
        self.assertEqual({u["username"] for u in users}, {"admin", "op1", "viewer1"})
        self.assertEqual({u["role"] for u in users}, {"admin", "operator", "viewer"})
        for u in users:
            self.assertNotIn("password_hash", u)
        self.assertNotIn("scrypt", body.decode())        # no hash material anywhere in the payload

    def test_list_users_non_admin_is_403(self):
        self.assertEqual(self._g("/api/users", self._login("viewer1", "vpw"))[0], 403)

    def test_list_users_requires_sqlite_409(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            self.assertEqual(self._g("/api/users")[0], 409)

    # ---- POST /api/me/password ----
    def test_me_password_change_then_login_with_new(self):
        self.assertEqual(self._p("/api/me/password", {"current": "adminpw", "new": "brandnewpw"}), 200)
        self.assertEqual(_post(self.web.address, "/api/login", {"user": "admin", "password": "adminpw"})[0], 401)
        self.assertEqual(_post(self.web.address, "/api/login", {"user": "admin", "password": "brandnewpw"})[0], 200)

    def test_viewer_can_change_own_password(self):
        viewer = self._login("viewer1", "vpw")
        self.assertEqual(self._p("/api/me/password", {"current": "vpw", "new": "viewernewpw"}, viewer), 200)

    def test_me_password_wrong_current_is_403(self):
        self.assertEqual(self._p("/api/me/password", {"current": "nope", "new": "brandnewpw"}), 403)

    def test_me_password_validation(self):
        self.assertEqual(self._p("/api/me/password", {"current": "adminpw", "new": "short"}), 400)  # weak new
        self.assertEqual(self._p("/api/me/password", {"new": "brandnewpw"}), 400)                   # missing current

    def test_me_password_requires_sqlite_409(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            self.assertEqual(self._p("/api/me/password", {"current": "adminpw", "new": "brandnewpw"}), 409)

    def test_me_password_account_vanished_is_409(self):
        with mock.patch.object(store_sql, "set_user_password", return_value=False):
            self.assertEqual(self._p("/api/me/password", {"current": "adminpw", "new": "brandnewpw"}), 409)

    def test_me_password_no_session_is_401(self):
        with mock.patch.object(web, "_AUTH_ENABLED", False):   # auth off → no resolved user
            status, _, _ = _post(self.web.address, "/api/me/password", {"current": "x", "new": "brandnewpw"})
        self.assertEqual(status, 401)

    # ---- POST /api/users (add) ----
    def test_add_user_success_then_login(self):
        self.assertEqual(self._p("/api/users", {"username": "kid", "password": "kidpassword", "role": "viewer"}), 200)
        self.assertEqual(_post(self.web.address, "/api/login", {"user": "kid", "password": "kidpassword"})[0], 200)

    def test_add_user_duplicate_is_409(self):
        self.assertEqual(self._p("/api/users", {"username": "admin", "password": "whatever12", "role": "admin"}), 409)

    def test_add_user_validation(self):
        self.assertEqual(self._p("/api/users", {"username": "a|b", "password": "longenough", "role": "viewer"}), 400)
        self.assertEqual(self._p("/api/users", {"username": "ok", "password": "x", "role": "viewer"}), 400)
        self.assertEqual(self._p("/api/users", {"username": "ok", "password": "longenough", "role": "root"}), 400)

    def test_add_user_requires_sqlite_409(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            self.assertEqual(
                self._p("/api/users", {"username": "kid", "password": "kidpassword", "role": "viewer"}), 409)

    # ---- POST /api/users/role ----
    def test_set_role_success(self):
        self.assertEqual(self._p("/api/users/role", {"username": "viewer1", "role": "operator"}), 200)
        self.assertEqual(store_sql.get_user_db_role("viewer1"), "operator")

    def test_set_role_self_is_forbidden(self):
        self.assertEqual(self._p("/api/users/role", {"username": "admin", "role": "viewer"}), 403)

    def test_set_role_validation_and_missing(self):
        self.assertEqual(self._p("/api/users/role", {"username": "", "role": "viewer"}), 400)
        self.assertEqual(self._p("/api/users/role", {"username": "viewer1", "role": "root"}), 400)
        self.assertEqual(self._p("/api/users/role", {"username": "ghost", "role": "viewer"}), 404)

    def test_set_role_last_admin_is_409(self):
        with mock.patch.object(store_sql, "set_user_role", return_value="last_admin"):
            self.assertEqual(self._p("/api/users/role", {"username": "op1", "role": "admin"}), 409)

    def test_set_role_requires_sqlite_409(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            self.assertEqual(self._p("/api/users/role", {"username": "viewer1", "role": "operator"}), 409)

    # ---- POST /api/users/disabled ----
    def test_disable_then_enable(self):
        self.assertEqual(self._p("/api/users/disabled", {"username": "op1", "disabled": True}), 200)
        self.assertIsNone(store_sql.current_user_role("op1"))          # disabled → role denied at once
        self.assertEqual(self._p("/api/users/disabled", {"username": "op1", "disabled": False}), 200)
        self.assertEqual(store_sql.current_user_role("op1"), "operator")

    def test_disable_self_is_forbidden(self):
        self.assertEqual(self._p("/api/users/disabled", {"username": "admin", "disabled": True}), 403)

    def test_disable_validation_and_missing(self):
        self.assertEqual(self._p("/api/users/disabled", {"username": "", "disabled": True}), 400)
        self.assertEqual(self._p("/api/users/disabled", {"username": "op1", "disabled": "yes"}), 400)  # not a bool
        self.assertEqual(self._p("/api/users/disabled", {"username": "ghost", "disabled": True}), 404)

    def test_disable_requires_sqlite_409(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            self.assertEqual(self._p("/api/users/disabled", {"username": "op1", "disabled": True}), 409)

    # ---- POST /api/users/reset-password ----
    def test_reset_password_success_then_login(self):
        self.assertEqual(self._p("/api/users/reset-password", {"username": "viewer1", "new": "resetpassw"}), 200)
        self.assertEqual(_post(self.web.address, "/api/login", {"user": "viewer1", "password": "resetpassw"})[0], 200)

    def test_reset_password_self_is_forbidden(self):
        self.assertEqual(self._p("/api/users/reset-password", {"username": "admin", "new": "resetpassw"}), 403)

    def test_reset_password_validation_and_missing(self):
        self.assertEqual(self._p("/api/users/reset-password", {"username": "", "new": "resetpassw"}), 400)
        self.assertEqual(self._p("/api/users/reset-password", {"username": "viewer1", "new": "x"}), 400)
        self.assertEqual(self._p("/api/users/reset-password", {"username": "ghost", "new": "resetpassw"}), 404)

    def test_reset_password_requires_sqlite_409(self):
        with mock.patch.object(store_sql, "users_db_authoritative", return_value=False):
            self.assertEqual(self._p("/api/users/reset-password", {"username": "viewer1", "new": "resetpassw"}), 409)

    def test_all_post_endpoints_enforce_json_content_type(self):
        # the shared CSRF guard (415 on a non-JSON Content-Type) fires in each handler's `if err` path
        for path in ("/api/me/password", "/api/users", "/api/users/role",
                     "/api/users/disabled", "/api/users/reset-password"):
            status, _, _ = _post(self.web.address, path, "x",
                                 headers={"Cookie": self.cookie, "Content-Type": "text/plain"})
            self.assertEqual(status, 415, path)


def _declared_post(path, length):
    return (
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\nContent-Length: {length}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")


async def _read_http_response(reader, *, allow_close=False, timeout=2.0):
    """Read one response. Stop at Content-Length; do not wait for the server to close.

    With ``allow_close``, a peer that drops the connection before a status line
    returns ``(None, leftover)`` instead of parsing an empty buffer."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if not chunk:
            if allow_close:
                return None, buf
            break
        buf += chunk
    if allow_close and b"\r\n\r\n" not in buf:
        return None, buf
    head, _, rest = buf.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            length = int(value.strip())
    while len(rest) < length:
        chunk = await asyncio.wait_for(reader.read(length - len(rest)), timeout=timeout)
        if not chunk:
            break
        rest += chunk
    return status, rest[:length]


async def _exchange(port, payload, *, shutdown_write=False, allow_close=False, timeout=2.0):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        if shutdown_write:
            writer.write_eof()
        return await _read_http_response(reader, allow_close=allow_close, timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


class _ProdLikeServer(TestServer):
    """Production ``AppRunner`` does not cancel the handler when the peer drops."""

    async def _make_runner(self, **kwargs):
        kwargs["handler_cancellation"] = False
        return await super()._make_runner(**kwargs)


class JsonBodyFramingTests(unittest.IsolatedAsyncioTestCase):
    """The shared reader bounds a stall, rejects a short body on an open socket,
    and still accepts a full one. A half-close only has to finish cleanly."""

    # Real BODY_READ_TIMEOUT stays 10s. A handler that waits it out misses this.
    _FINISH_BOUND = 2.0

    async def asyncSetUp(self):
        self._auth = mock.patch.object(web, "_AUTH_ENABLED", False)
        self._auth.start()
        self._started = []
        self._finished = []
        app = web.make_app(SimpleNamespace())

        @middleware
        async def _watch(request, handler):
            watched = request.path in ("/api/settings", "/api/login")
            if watched:
                self._started.append(request)
            started = time.monotonic()
            try:
                response = await handler(request)
            except asyncio.CancelledError:
                if watched:
                    self._finished.append((request.path, "cancelled", time.monotonic() - started))
                raise
            except Exception as exc:
                if watched:
                    self._finished.append((request.path, exc, time.monotonic() - started))
                raise
            else:
                if watched:
                    self._finished.append((request.path, response.status, time.monotonic() - started))
                return response

        app.middlewares.append(_watch)

        async def _probe(request):
            body, err = await web._read_json_body(request, 1024 * 1024)
            if err:
                return body
            return web._json(200, {"ok": True, "n": len(body["pad"]), "tail": body["tail"]})

        app.router.add_post("/api/body-chunk-probe", _probe)
        self.client = TestClient(_ProdLikeServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._auth.stop()

    async def _wait_started(self, path, prior):
        deadline = time.monotonic() + self._FINISH_BOUND
        while time.monotonic() < deadline:
            for request in self._started[prior:]:
                if request.path == path:
                    return request
            await asyncio.sleep(0.01)
        self.fail(f"{path} handler did not start within {self._FINISH_BOUND}s")

    async def _wait_finished(self, path, prior):
        deadline = time.monotonic() + self._FINISH_BOUND
        while time.monotonic() < deadline:
            for item in self._finished[prior:]:
                if item[0] == path:
                    return item
            await asyncio.sleep(0.01)
        self.fail(f"{path} handler did not finish within {self._FINISH_BOUND}s")

    def _exact_settings(self):
        prefix, suffix = b'{"locale":null', b"}"
        return prefix + b" " * (web.MAX_BODY - len(prefix) - len(suffix)) + suffix

    async def _stall(self, path):
        with mock.patch.object(web, "BODY_READ_TIMEOUT", 0.2):
            status, body = await _exchange(self.client.port, _declared_post(path, 32) + b"{")
        self.assertEqual(status, 408)
        self.assertEqual(json.loads(body), {"ok": False, "error": "body read timed out"})

    async def _half_close(self, path):
        # The peer write-shuts down after a short body. It is already gone, so a
        # status is not required. The handler must not 500 and must not sit in
        # the 10s body read. BODY_READ_TIMEOUT is left at 10s so a stall fails.
        prior = len(self._finished)
        status, body = None, b""
        try:
            status, body = await _exchange(
                self.client.port, _declared_post(path, 32) + b"{}",
                shutdown_write=True, allow_close=True, timeout=self._FINISH_BOUND)
        except TimeoutError:
            self.fail(f"{path} half-close hung with the socket still open")
        except ConnectionError:
            pass
        _path, outcome, elapsed = await self._wait_finished(path, prior)
        self.assertLess(elapsed, self._FINISH_BOUND)
        self.assertNotIsInstance(outcome, Exception)
        self.assertNotEqual(outcome, 500)
        if status is not None:
            self.assertNotEqual(status, 500)
            self.assertNotIn(b"Internal Server Error", body)

    async def _truncate(self, path):
        # "{}" is valid JSON and shorter than Content-Length. End the payload
        # while the TCP socket is still open: that is a 400, not a parsed body
        # (settings would otherwise 200, login would otherwise 401).
        prior = len(self._started)

        async def _send():
            return await _exchange(self.client.port, _declared_post(path, 32) + b"{}")

        task = asyncio.create_task(_send())
        request = await self._wait_started(path, prior)
        transport = request.transport
        self.assertIsNotNone(transport)
        self.assertFalse(transport.is_closing())
        request.content.feed_eof()
        status, body = await asyncio.wait_for(task, timeout=self._FINISH_BOUND)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"ok": False, "error": "incomplete body"})

    async def test_slow_client_is_408(self):
        await self._stall("/api/settings")

    async def test_truncated_body_is_400(self):
        await self._truncate("/api/settings")

    async def test_half_close_finishes_cleanly(self):
        await self._half_close("/api/settings")

    async def test_login_slow_client_is_408(self):
        await self._stall("/api/login")

    async def test_login_truncated_body_is_400(self):
        await self._truncate("/api/login")

    async def test_login_half_close_finishes_cleanly(self):
        await self._half_close("/api/login")

    async def test_body_exactly_at_the_cap_passes(self):
        body = self._exact_settings()
        self.assertEqual(len(body), web.MAX_BODY)
        response = await self.client.post(
            "/api/settings", data=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), {"ok": True})

    async def test_body_over_the_cap_is_413(self):
        body = self._exact_settings() + b" "
        self.assertEqual(len(body), web.MAX_BODY + 1)
        response = await self.client.post(
            "/api/settings", data=body, headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 413)
        self.assertEqual(await response.json(),
                         {"ok": False, "error": f"body must be ≤ {web.MAX_BODY} bytes"})

    async def test_multi_chunk_large_body_is_read_fully(self):
        pad = "a" * (600 * 1024)
        raw = json.dumps({"pad": pad, "tail": "END"}).encode()
        self.assertGreaterEqual(len(raw), 600 * 1024)
        head = _declared_post("/api/body-chunk-probe", len(raw))
        reader, writer = await asyncio.open_connection("127.0.0.1", self.client.port)
        try:
            writer.write(head)
            await writer.drain()
            for offset in range(0, len(raw), 8192):
                writer.write(raw[offset:offset + 8192])
                await writer.drain()
            status, body = await _read_http_response(reader)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "n": len(pad), "tail": "END"})


class SessionBridgeWebTests(unittest.IsolatedAsyncioTestCase):
    """Tests for active v2 session bridge, core Origin checks, CAS password writes, and phase fail-closed."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(db.reset_engine_cache)
        self.db_path = Path(self._dir.name) / "bridge_web.db"
        self.secret = b"session-bridge-web-secret"
        self.env = mock.patch.dict(
            os.environ,
            {
                "HESTIA_PERSIST": "sqlite",
                "HESTIA_DB": str(self.db_path),
                "HESTIA_AUTH_USERS_FILE": str(Path(self._dir.name) / "missing-users.json"),
                "HESTIA_BUDGET_HTTP_ENABLED": "1",
                "HESTIA_BUDGET_RECEIPTS_ENABLED": "1",
                "HESTIA_BUDGET_CONSENT_ENABLED": "0",
                "HESTIA_BUDGET_ANALYTICS_ENABLED": "1",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        for p in (
            mock.patch.object(web, "_AUTH_ENABLED", True),
            mock.patch.object(web, "_SESSION_SECRET", self.secret),
            mock.patch.object(web, "_COOKIE_SECURE", False),
        ):
            p.start()
            self.addCleanup(p.stop)

        engine, _ = db.init_db(self.db_path)
        store_sql.cutover_users(
            engine,
            {
                "admin": auth.hash_password("adminpw12345"),
                "admin2": auth.hash_password("admin2pw1234"),
            },
        )
        store_sql.add_user("op", auth.hash_password("oppw12345678"), "operator", path=self.db_path)
        store_sql.add_user("viewer", auth.hash_password("viewpw123456"), "viewer", path=self.db_path)
        engine.dispose()

        reg = proxy.Registry(Path(self._dir.name) / "reg.json")
        store = AutomationStore(Path(self._dir.name) / "auto.json")
        self.rt = SimpleNamespace(
            registry=reg,
            engine=SimpleNamespace(store=store),
            event_bus=proxy.EventBus(),
            audit_engine=None,
            protocol=None,
        )
        self.client = TestClient(TestServer(web.make_app(self.rt)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def _origin(self) -> str:
        return f"http://{self.client.host}:{self.client.port}"

    def _set_meta(self, key: str, value: str) -> None:
        engine, _ = db.init_db(self.db_path)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO app_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    async def test_legacy_phase_login_keeps_v1_and_bearer_cookie_parity(self):
        resp = await self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345", "bearer": True})
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertIn("Set-Cookie", resp.headers)
        env = auth.decode_session(body["token"], now=web._now(), secret=self.secret)
        self.assertEqual(env.version, 1)

        # Phase off: password change leaves earlier v1 token valid because generation stays 0.
        reset_ok = await self.client.post(
            "/api/me/password",
            json={"current": "adminpw12345", "new": "newadminpw123"},
            headers={"Cookie": f"hestia_session={body['token']}"},
        )
        self.assertEqual(reset_ok.status, 200)
        after = await self.client.get("/api/whoami", headers={"Authorization": f"Bearer {body['token']}"})
        self.assertEqual(after.status, 200)

    async def test_active_v2_login_cookie_native_separation_generation_and_concurrent_reset(self):
        store_sql.activate_sessions_v2(path=self.db_path)

        # Browser login emits v2 browser cookie and clears stale consent cookies via auth_transition.
        flow_cookie = "__Host-hestia_budget_11111111-2222-4333-8444-555555555555"
        browser_resp = await self.client.post(
            "/api/login",
            json={"user": "admin", "password": "adminpw12345"},
            headers={"Cookie": f"{flow_cookie}={'m' * 43}"},
        )
        self.assertEqual(browser_resp.status, 200)
        browser_cookie = browser_resp.cookies["hestia_session"].value
        self.client.session.cookie_jar.clear()
        self.assertEqual(auth.decode_session(browser_cookie, now=web._now(), secret=self.secret).purpose, "browser")
        self.assertEqual(browser_resp.cookies[flow_cookie]["expires"], "Thu, 01 Jan 1970 00:00:00 GMT")

        # Native login returns v2 native token, no Set-Cookie, and skips auth_transition.
        with mock.patch("hestia.budget.consent_api.auth_transition", side_effect=AssertionError("called transition")):
            native_resp = await self.client.post(
                "/api/login",
                json={"user": "admin", "password": "adminpw12345", "bearer": True},
            )
        self.assertEqual(native_resp.status, 200)
        self.assertNotIn("Set-Cookie", native_resp.headers)
        native_token = (await native_resp.json())["token"]
        self.assertEqual(auth.decode_session(native_token, now=web._now(), secret=self.secret).purpose, "native")

        # Carrier substitution rejected at ingress (401).
        self.assertEqual((await self.client.get("/api/whoami", headers={"Cookie": f"hestia_session={native_token}"})).status, 401)
        self.assertEqual((await self.client.get("/api/whoami", headers={"Authorization": f"Bearer {browser_cookie}"})).status, 401)

        # Concurrent reset during scrypt rejects login without minting.
        real_auth = auth.authenticate

        def reset_during_scrypt(u, pw, users):
            ok = real_auth(u, pw, users)
            store_sql.set_user_password("admin", auth.hash_password("adminpw12345"), path=self.db_path)
            return ok

        with mock.patch("hestia.auth.authenticate", side_effect=reset_during_scrypt):
            raced = await self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345"})
        self.assertEqual(raced.status, 401)

        # Generation bump invalidated earlier v2 browser and native tokens as well as v1 generation=0.
        v1_stale = auth.make_session("admin", now=web._now(), secret=self.secret)
        for hdrs in (
            {"Cookie": f"hestia_session={browser_cookie}"},
            {"Authorization": f"Bearer {native_token}"},
            {"Cookie": f"hestia_session={v1_stale}"},
        ):
            self.assertEqual((await self.client.get("/api/whoami", headers=hdrs)).status, 401)

        # Disable and re-enable keeps bumped generation so pre-disable tokens remain invalid.
        fresh_resp = await self.client.post("/api/login", json={"user": "op", "password": "oppw12345678", "bearer": True})
        op_token = (await fresh_resp.json())["token"]
        store_sql.set_user_disabled("op", True, path=self.db_path)
        store_sql.set_user_disabled("op", False, path=self.db_path)
        self.assertEqual((await self.client.get("/api/whoami", headers={"Authorization": f"Bearer {op_token}"})).status, 401)

    async def test_active_v2_core_browser_route_and_origin_matrix(self):
        store_sql.activate_sessions_v2(path=self.db_path)
        login_b = await self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345"})
        b_cookie = login_b.cookies["hestia_session"].value
        self.client.session.cookie_jar.clear()
        login_n = await self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345", "bearer": True})
        n_token = (await login_n.json())["token"]
        login_op = await self.client.post("/api/login", json={"user": "op", "password": "oppw12345678", "bearer": True})
        op_token = (await login_op.json())["token"]
        v1_cookie = auth.make_session("admin", now=web._now(), secret=self.secret)

        # Role floor retains precedence over browser-only check: operator bearer -> empty 403, admin bearer -> JSON 403.
        op_denied = await self.client.post("/api/users", json={}, headers={"Authorization": f"Bearer {op_token}"})
        self.assertEqual(op_denied.status, 403)
        self.assertEqual(await op_denied.read(), b"")

        # GET /api/users: v2 browser cookie without Origin -> 200; v1 -> 401 upgrade; native bearer -> 403 browser_session_required.
        self.assertEqual((await self.client.get("/api/users", headers={"Cookie": f"hestia_session={b_cookie}"})).status, 200)
        u_v1 = await self.client.get("/api/users", headers={"Cookie": f"hestia_session={v1_cookie}"})
        self.assertEqual(u_v1.status, 401)
        self.assertEqual((await u_v1.json())["error"], "browser_session_upgrade_required")
        u_nat = await self.client.get("/api/users", headers={"Authorization": f"Bearer {n_token}"})
        self.assertEqual(u_nat.status, 403)
        self.assertEqual((await u_nat.json())["error"], "browser_session_required")

        # Every core browser mutation route enforces v2 cookie + direct LAN Origin (ignoring forwarded headers).
        direct_origin = self._origin()
        core_mutations = (
            ("/api/users", {}),
            ("/api/users/grants", {}),
            ("/api/users/role", {}),
            ("/api/users/disabled", {}),
            ("/api/users/reset-password", {}),
            ("/api/me/password", {}),
            ("/api/name", {}),
            ("/api/whole-home", {}),
            ("/api/automations", {}),
            ("/api/automations/delete", {}),
            ("/api/graduate", None),
            ("/api/rooms/icons", {}),
        )
        for path, payload in core_mutations:
            with self.subTest(path=path):
                kwargs = {"json": payload} if payload is not None else {}
                # Missing Origin -> 403 origin_required
                no_org = await self.client.post(path, **kwargs, headers={"Cookie": f"hestia_session={b_cookie}"})
                self.assertEqual(no_org.status, 403)
                self.assertEqual((await no_org.json())["error"], "origin_required")
                # Native bearer -> 403 browser_session_required
                nat = await self.client.post(path, **kwargs, headers={"Authorization": f"Bearer {n_token}"})
                self.assertEqual(nat.status, 403)
                self.assertEqual((await nat.json())["error"], "browser_session_required")
                # v1 cookie -> 401 browser_session_upgrade_required
                v1r = await self.client.post(path, **kwargs, headers={"Cookie": f"hestia_session={v1_cookie}", "Origin": direct_origin})
                self.assertEqual(v1r.status, 401)
                self.assertEqual((await v1r.json())["error"], "browser_session_upgrade_required")
                # Direct matching Origin (even with spoofed X-Forwarded headers) passes ingress & Origin check.
                ok_hdrs = {
                    "Cookie": f"hestia_session={b_cookie}",
                    "Origin": direct_origin,
                    "X-Forwarded-Host": "evil.example",
                    "X-Forwarded-Proto": "https",
                }
                ok = await self.client.post(path, **kwargs, headers=ok_hdrs)
                self.assertNotIn(ok.status, (401, 403))

        # Configured HESTIA_PUBLIC_ORIGIN (DNS, terminal dot, IPv6, IPv4, trailing slash) and malformed origin handling.
        for cfg_origin, hdr_origin, mismatched_origin in (
            ("https://hestia.example:8443", "https://hestia.example:8443", direct_origin),
            ("https://hestia.klatt.ie", "https://hestia.klatt.ie", "https://hestia.klatt.ie/"),
            ("https://hestia.klatt.ie/", "https://hestia.klatt.ie", "https://hestia.klatt.ie."),
            ("https://hestia.example.", "https://hestia.example.", "https://hestia.example"),
            ("https://[::1]:8443", "https://[::1]:8443", direct_origin),
            ("https://[::1]", "https://[::1]", direct_origin),
            ("http://192.168.1.10:8080", "http://192.168.1.10:8080", direct_origin),
        ):
            with mock.patch.dict(os.environ, {"HESTIA_PUBLIC_ORIGIN": cfg_origin}):
                bad_origin = await self.client.post(
                    "/api/name", json={}, headers={"Cookie": f"hestia_session={b_cookie}", "Origin": mismatched_origin}
                )
                self.assertEqual(bad_origin.status, 403, f"expected 403 for {mismatched_origin!r} vs {cfg_origin!r}")
                self.assertEqual((await bad_origin.json())["error"], "origin_required")
                good_cfg = await self.client.post(
                    "/api/name",
                    json={},
                    headers={"Cookie": f"hestia_session={b_cookie}", "Origin": hdr_origin},
                )
                self.assertEqual(good_cfg.status, 400)  # cleared auth & origin, reached body validation

        for bad_cfg, hdr_origin in (
            ("not-a-url", direct_origin),
            ("ftp://hestia.example", direct_origin),
            ("https://[v1.hestia.example]", "https://v1.hestia.example"),
            ("https://[192.168.1.1]", direct_origin),
            ("https://[::1", direct_origin),
            ("https://[::1]extra", direct_origin),
            ("https://[::1]:", direct_origin),
            ("https://[::1%eth0]:8443", direct_origin),
            ("https://foo]bar", direct_origin),
            ("https://::1", direct_origin),
            ("https://-bad.example", direct_origin),
            ("https://bad-.example", direct_origin),
            ("https://bad_host.example", direct_origin),
            ("https://.", direct_origin),
            ("https://..", direct_origin),
            ("https://hestia.example..", direct_origin),
            ("http://192.168.1.10.", direct_origin),
            ("https://héllo.example", direct_origin),
            ("https://" + ("a" * 64) + ".example", direct_origin),
            ("https://" + ("a." * 126) + "bb", direct_origin),
            ("http://192.168.1.999:8080", direct_origin),
            ("https://hestia.example/path", direct_origin),
            ("https://hestia.example//", direct_origin),
            ("https://hestia.example?q=1", direct_origin),
            ("https://hestia.example#frag", direct_origin),
            ("https://hestia.example/?q=1", direct_origin),
            ("https://hestia.example/#frag", direct_origin),
            ("https://hestia.example:0", direct_origin),
            ("https://hestia.example:65536", direct_origin),
            ("https://hestia.example:99999", direct_origin),
            ("https://hestia.example:" + ("9" * 4301), direct_origin),
            ("https://hestia.example:abc", direct_origin),
            ("https://u@hestia.example", direct_origin),
            ("https://bad host", direct_origin),
            ("\x01https://hestia.example", "https://hestia.example"),
            ("\x7fhttps://hestia.example", "https://hestia.example"),
            ("https://\x01hestia.example", "https://hestia.example"),
            ("https://hestia.example:", direct_origin),
            ("https://:8080", direct_origin),
        ):
            with mock.patch.dict(os.environ, {"HESTIA_PUBLIC_ORIGIN": bad_cfg}):
                cfg_err = await self.client.post(
                    "/api/name", json={}, headers={"Cookie": f"hestia_session={b_cookie}", "Origin": hdr_origin}
                )
                self.assertEqual(cfg_err.status, 503, f"expected 503 for {bad_cfg!r}")
                self.assertEqual((await cfg_err.json())["error"], "auth_origin_unavailable")

        # Malformed request Origin (4301-digit port / trailing slash) and fallback Host (4301-digit port / trailing slash) -> 403.
        with mock.patch.dict(os.environ, {"HESTIA_PUBLIC_ORIGIN": "https://hestia.example"}):
            huge_origin_resp = await self.client.post(
                "/api/name",
                json={},
                headers={"Cookie": f"hestia_session={b_cookie}", "Origin": "https://hestia.example:" + ("9" * 4301)},
            )
            self.assertEqual(huge_origin_resp.status, 403)
            self.assertEqual((await huge_origin_resp.json())["error"], "origin_required")
        for bad_fallback_host in ("hestia.example/", "hestia.example:" + ("9" * 4301)):
            fallback_err = web._check_core_origin(
                SimpleNamespace(scheme="https", headers=mock.Mock(getall=lambda k, d=None, h=bad_fallback_host: [] if k == "Origin" else [h]))
            )
            self.assertIsNotNone(fallback_err)
            self.assertEqual(fallback_err.status, 403)

        # Non-browser-only routes allow v2 native, v2 browser without Origin, and live v1 compatibility cookies.
        for path, method, body in (
            ("/api/settings", "GET", None),
            ("/api/settings", "POST", {"locale": "pl"}),
            ("/api/whole-home", "GET", None),
            ("/api/automations", "GET", None),
            ("/api/rooms/icons", "GET", None),
            ("/api/whoami", "GET", None),
            ("/api/calendars", "GET", None),
            ("/api/scene", "POST", {"op": "lights_off"}),
        ):
            for hdrs in (
                {"Authorization": f"Bearer {n_token}"},
                {"Cookie": f"hestia_session={b_cookie}"},
                {"Cookie": f"hestia_session={v1_cookie}"},
            ):
                resp = await self.client.request(method, path, json=body, headers=hdrs)
                self.assertNotIn(resp.status, (401, 403), f"{method} {path} unexpectedly rejected: {resp.status}")

        # Duplicate Host header fails Origin fallback cleanly.
        self.assertIsNotNone(
            web._check_core_origin(SimpleNamespace(scheme="http", headers=mock.Mock(getall=lambda k, d=None: [] if k == "Origin" else ["a", "b"])))
        )
        self.assertIsNotNone(
            web._check_core_origin(SimpleNamespace(scheme="http", headers=mock.Mock(getall=lambda k, d=None: ["http://bad host"])))
        )

    async def test_me_password_cas_rejects_concurrent_reset_and_unknown_phase_blocks_start_and_http(self):
        store_sql.activate_sessions_v2(path=self.db_path)
        login_b = await self.client.post("/api/login", json={"user": "admin2", "password": "admin2pw1234"})
        b_cookie = login_b.cookies["hestia_session"].value
        self.client.session.cookie_jar.clear()

        # Simulate admin resetting admin2 right after _me_password verifies current password.
        real_auth = auth.authenticate

        def race_reset(u, pw, users):
            ok = real_auth(u, pw, users)
            store_sql.set_user_password("admin2", auth.hash_password("concurrentpw12"), path=self.db_path)
            return ok

        with mock.patch("hestia.auth.authenticate", side_effect=race_reset):
            raced = await self.client.post(
                "/api/me/password",
                json={"current": "admin2pw1234", "new": "mynewpassword1"},
                headers={"Cookie": f"hestia_session={b_cookie}", "Origin": self._origin()},
            )
        self.assertEqual(raced.status, 409)

        # Custom policy phase fails start_web before listening and returns 503 on HTTP paths.
        self._set_meta("authz_policy_phase", "future_phase")
        with mock.patch("aiohttp.web.AppRunner.setup", side_effect=AssertionError("runner started on custom phase")):
            with self.assertRaises(authz.PolicyPhaseUnsupported):
                await web.start_web(self.rt, host="127.0.0.1", port=0)
        for req in (
            self.client.get("/api/discovery", headers={"Cookie": f"hestia_session={b_cookie}"}),
            self.client.get("/api/discovery"),
            self.client.post("/api/logout"),
            self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345"}),
        ):
            r = await req
            self.assertEqual(r.status, 503)
            self.assertEqual((await r.json())["error"], "authorization_bridge_incompatible")
        with mock.patch.object(web, "_AUTH_ENABLED", False):
            r_off = await self.client.get("/api/discovery")
            self.assertEqual(r_off.status, 503)
            self.assertEqual((await r_off.json())["error"], "authorization_bridge_incompatible")
        # Also if phase flips between login snapshot & finish_sqlite_login, login maps to 503.
        self._set_meta("authz_policy_phase", "catalog_compat")
        real_auth2 = auth.authenticate

        def flip_custom_during_scrypt(u, pw, users):
            ok = real_auth2(u, pw, users)
            self._set_meta("authz_policy_phase", "future_phase")
            return ok

        with mock.patch("hestia.auth.authenticate", side_effect=flip_custom_during_scrypt):
            r_flip = await self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345"})
            self.assertEqual(r_flip.status, 503)
            self.assertEqual((await r_flip.json())["error"], "authorization_bridge_incompatible")

        # Direct _login invocation without middleware also maps snapshot-phase unsupported to 503.
        from aiohttp.test_utils import make_mocked_request
        login_req = make_mocked_request("POST", "/api/login", headers={"Content-Type": "application/json"}, app=self.client.server.app)
        with mock.patch.object(web, "_read_json_body", return_value=({"user": "admin", "password": "adminpw12345"}, False)), \
             mock.patch("hestia.web.consent_context.login_ip", return_value="127.0.0.1"):
            direct_resp = await web._login(login_req)
        self.assertEqual(direct_resp.status, 503)
        self.assertEqual(json.loads(direct_resp.body)["error"], "authorization_bridge_incompatible")

    async def test_login_surrogate_username_runs_dummy_scrypt_and_returns_401(self):
        verify_calls = []
        real_verify = auth.verify_password

        def track_verify(password, stored):
            verify_calls.append(stored)
            return real_verify(password, stored)

        with mock.patch("hestia.auth.verify_password", side_effect=track_verify):
            resp = await self.client.post(
                "/api/login",
                data='{"user": "\\ud800", "password": "wrong"}',
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(resp.status, 401)
        self.assertEqual((await resp.json())["error"], "invalid credentials")
        self.assertEqual(verify_calls, [auth._DUMMY_STORED])

    async def test_middleware_v1_revocation_race_keeps_single_snapshot(self):
        login_v1 = await self.client.post("/api/login", json={"user": "admin", "password": "adminpw12345"})
        self.assertEqual(login_v1.status, 200)
        v1_cookie = login_v1.cookies["hestia_session"].value
        self.client.session.cookie_jar.clear()

        real_load = store_sql.load_auth_state
        real_req = authz.require_supported_policy_phase
        seen_states = []
        interleaved = {"done": False}

        def interleave(session):
            phase = real_req(session)
            if not interleaved["done"]:
                interleaved["done"] = True
                store_sql.activate_sessions_v2(path=self.db_path)
                store_sql.set_user_password("admin", auth.hash_password("adminpw-reset"), path=self.db_path)
            return phase

        def capture_state(*args, **kwargs):
            state = real_load(*args, **kwargs)
            seen_states.append((state.session_phase, state.generation))
            return state

        with mock.patch("hestia.store_sql.load_auth_state", side_effect=capture_state), \
             mock.patch("hestia.authz.require_supported_policy_phase", side_effect=interleave):
            first = await self.client.get("/api/whoami", headers={"Cookie": f"hestia_session={v1_cookie}"})
            self.assertEqual(first.status, 200)
        self.assertEqual(seen_states, [("legacy", 0)])

        revoked = await self.client.get("/api/whoami", headers={"Cookie": f"hestia_session={v1_cookie}"})
        self.assertEqual(revoked.status, 401)


if __name__ == "__main__":
    unittest.main()
