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
import socket
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hestia import proxy, web
from hestia.automations import AutomationEngine, AutomationStore, rule_vocab
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

    def submit(self, coro, timeout=2.0):
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
        self._loop_thread.submit(web.stop_web(self._handle), timeout=3.0)


def _start_web(rt, loop_thread, host="127.0.0.1", port=0):
    handle = loop_thread.submit(web.start_web(rt, host, port))
    return _StartedWeb(handle, loop_thread)


def _client(address) -> http.client.HTTPConnection:
    host, port = address
    return http.client.HTTPConnection(host, port, timeout=5.0)


def _get(address, path) -> "tuple[int, dict, bytes]":
    conn = _client(address)
    try:
        conn.request("GET", path)
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

    def test_celsius_must_be_number_between_5_and_30(self):
        bad = [
            {"op": "thermostat", "node": 13},
            {"op": "thermostat", "node": 13, "celsius": True},
            {"op": "thermostat", "node": 13, "celsius": "21"},
            {"op": "thermostat", "node": 13, "celsius": float("nan")},
            {"op": "thermostat", "node": 13, "celsius": float("inf")},
            {"op": "thermostat", "node": 13, "celsius": 4.9},
            {"op": "thermostat", "node": 13, "celsius": 30.1},
            {"op": "thermostat", "node": 13, "celsius": 10**10000},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(self._validate(payload),
                                 "celsius must be a number between 5 and 30")

    def test_valid_cases(self):
        good = [
            {"op": "switch", "node": 0, "on": False},
            {"op": "level", "node": 255, "value": 99},
            {"op": "cover", "node": 8, "value": 0},
            {"op": "thermostat", "node": 13, "celsius": 21},
            {"op": "thermostat", "node": 13, "celsius": 21.5},
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


class IndexTests(_WebTestBase):
    def test_index_serves_html(self):
        status, headers, body = _get(self.web.address, "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<table>", body)
        self.assertIn(b"hestia", body)
        # heatmap: the "last seen" column + the 1 Hz relative-time ticker must ship
        self.assertIn(b"last seen", body)
        self.assertIn(b"tickActivity", body)
        # battery column (replaces "power"): header + the level formatter
        self.assertIn(b"<th>battery</th>", body)
        self.assertIn(b"battFmt", body)
        # live "stan" column: header + the type-aware formatter
        self.assertIn(b"<th>stan</th>", body)
        self.assertIn(b"stateStr", body)
        # dashboard click-to-control: action column + safe DOM renderer + control POST helper
        self.assertIn(b"<th>akcje</th>", body)
        self.assertIn(b"function postControl", body)
        self.assertIn(b"function renderActions", body)
        self.assertIn(b"api/control", body)
        self.assertIn(b"wys", body)   # "wysłano" is UTF-8; this keeps the smoke check ASCII-only
        # SSE state-delta patching (no full refetch on a live value change)
        self.assertIn(b"applyStatePatch", body)
        # globals (crib_temp/outdoor_temp) UI: spans + formatter + applier + SSE branch
        self.assertIn(b'id="g-crib"', body)
        self.assertIn(b'id="g-outdoor"', body)
        self.assertIn(b"function fmtTemp", body)
        self.assertIn(b"function applyGlobals", body)
        self.assertIn(b"function renderGlobals", body)
        self.assertIn(b"pendingGlobals", body)             # globals deltas queue mid-refresh (no stale rollback)
        self.assertIn(b"'globals'", body)
        # 2-gang per-endpoint sub-rows (the JS isn't unit-tested → smoke-check it ships)
        self.assertIn(b"function subRow", body)
        self.assertIn(b"function bindSubRow", body)
        self.assertIn(b"ep-stan", body)
        self.assertIn(b"save-ep-name", body)
        self.assertIn(b"function flashRow", body)   # channel sub-rows flash on state change
        # M3 automations editor: header, list loader, JSON editor, save + delete bindings
        self.assertIn(b">automations</h1>", body)
        self.assertIn(b"loadAutomations", body)
        self.assertIn(b'id="rule-json"', body)
        self.assertIn(b'id="save-rule"', body)
        self.assertIn(b"auto-del", body)
        self.assertIn(b"trigSummary", body)
        # klima panel: container + the data-driven mode+temp renderer + the power-on ("Włącz") wiring
        self.assertIn(b'id="klima"', body)
        self.assertIn(b"function renderKlima", body)
        self.assertIn(b"power_on", body)
        self.assertIn(b"Ustaw", body)        # the single program button
        # guided rule form (M3.1): container + builder + the Build button
        self.assertIn(b'id="rule-form"', body)
        self.assertIn(b"function renderRuleForm", body)
        self.assertIn(b"Zbuduj JSON", body)

    def test_ui_returns_404_when_dist_absent(self):
        self.web.stop()
        with mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(self.tmp / "missing-dist")}):
            self.web = _start_web(self.rt, self.loop_thread)
            status, _, body = _get(self.web.address, "/ui/")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_ui_serves_built_index_and_assets(self):
        dist = self.tmp / "ui-dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><h1>hestia</h1>", encoding="utf-8")
        (assets / "app.js").write_text("console.log('hestia shell');\n", encoding="utf-8")

        self.web.stop()
        with mock.patch.dict("os.environ", {"HESTIA_UI_DIST": str(dist)}):
            self.web = _start_web(self.rt, self.loop_thread)
            status, headers, body = _get(self.web.address, "/ui/")
            asset_status, _, asset_body = _get(self.web.address, "/ui/assets/app.js")
            missing_status, _, _ = _get(self.web.address, "/ui/assets/missing.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<h1>hestia</h1>", body)
        self.assertEqual(asset_status, 200)
        self.assertEqual(asset_body, b"console.log('hestia shell');\n")
        self.assertEqual(missing_status, 404)        # built UI, asset not in the bundle → 404, not 500


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
        self.assertEqual(data["globals"], {"crib_temp": None, "outdoor_temp": None})   # pollers off → null

    def test_discovery_reflects_global_fields(self):
        self.rt.state.crib_temp = 22.5
        self.rt.state.outdoor_temp = -1.0
        _, _, body = _get(self.web.address, "/api/discovery")
        self.assertEqual(json.loads(body)["globals"], {"crib_temp": 22.5, "outdoor_temp": -1.0})

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

        status, headers, body = _get(self.web.address, "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Content-Length", headers)
        self.assertIn(b"<table>", body)

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


if __name__ == "__main__":
    unittest.main()
