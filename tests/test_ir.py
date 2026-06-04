"""Integration tests for the Flipper IR feature wiring: the HESTIA_IR_BUTTONS config parser, the ``ir``
control op, the single-owner ``_ir_worker``, and the engine's ``ir`` effect action (validation + enqueue
via ``_fire``/``_dispatch_ir``). The serial/RPC client itself (hestia.flipper) is covered in test_flipper;
here ``flipper.transmit_ir`` is mocked so nothing touches a real device.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest import mock

from hestia import flipper, proxy
from hestia.automations import AutomationEngine, AutomationStore, Rule
from hestia.flipper import FlipperError

_SCENE = {"type": "scene", "node": 1, "scene_id": 1}


def _ir_rule(file="/ext/infrared/Klima.ir", button="Power"):
    return Rule.from_dict({"id": "r1", "trigger": dict(_SCENE),
                           "actions": [{"op": "ir", "file": file, "button": button}]})


# --- HESTIA_IR_BUTTONS parser ------------------------------------------------

class IrButtonsConfigTests(unittest.TestCase):
    def test_well_formed(self):
        raw = '[{"label":"Klima ON","file":"/ext/infrared/Klima.ir","button":"Power"}]'
        self.assertEqual(proxy._ir_buttons(raw),
                         [{"label": "Klima ON", "file": "/ext/infrared/Klima.ir", "button": "Power"}])

    def test_empty_or_none(self):
        self.assertEqual(proxy._ir_buttons(None), [])
        self.assertEqual(proxy._ir_buttons(""), [])

    def test_bad_json(self):
        self.assertEqual(proxy._ir_buttons("{not json"), [])

    def test_non_list(self):
        self.assertEqual(proxy._ir_buttons('{"label":"x"}'), [])

    def test_skips_malformed_entries(self):
        raw = ('[{"label":"ok","file":"f","button":"b"},'
               ' {"label":"no file"}, "a string", {"label":"","file":"f","button":"b"}]')
        self.assertEqual(proxy._ir_buttons(raw), [{"label": "ok", "file": "f", "button": "b"}])


# --- klima.ir signal parser (dashboard mode+temp panel) ----------------------

class KlimaSignalsConfigTests(unittest.TestCase):
    def _write_ir(self, names):
        fd, path = tempfile.mkstemp(suffix=".ir")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        lines = ["Filetype: IR signals file", "Version: 1"]
        for n in names:
            lines += ["#", f"name: {n}", "type: raw", "data: 1 2 3"]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    def test_parses_modes_power_on_and_presets(self):
        # "<mode>_<temp>" → modes (dup temp deduped + sorted, modes sorted); "on_<mode>_<temp>" →
        # power_on; bare/odd names → presets (dup "fan" skipped, distinct "on_fan" kept); "_5" (empty
        # mode) and "cool_x" (non-digit) fall to presets; an empty name: line is ignored.
        path = self._write_ir(["off", "cool_18", "cool_22", "cool_18", "heat_30",
                               "on_cool_22", "on_heat_30", "fan", "fan", "on_fan",
                               "_5", "cool_x", ""])
        self.assertEqual(proxy._klima_signals(path, "/ext/infrared/klima.ir"), {
            "file": "/ext/infrared/klima.ir",
            "modes": {"cool": [18, 22], "heat": [30]},
            "power_on": {"cool": [22], "heat": [30]},
            "presets": ["off", "fan", "on_fan", "_5", "cool_x"]})

    def test_on_prefix_edge_names(self):
        # "on_" alone and "on_<bare>" → presets; "on_<mode>_<temp>" → power_on; a name merely CONTAINING
        # but not PREFIXED by "on_" (e.g. cool_on_22) is a normal set-mode name (mode "cool_on").
        out = proxy._klima_signals(self._write_ir(["on_", "on_fan", "on_cool_22", "cool_on_22"]), "/sd/k.ir")
        self.assertEqual(out["power_on"], {"cool": [22]})
        self.assertEqual(out["modes"], {"cool_on": [22]})
        self.assertEqual(out["presets"], ["on_", "on_fan"])

    def test_set_mode_only_has_empty_power_on(self):
        # a legacy / set-mode-only file yields power_on == {} (the dashboard then disables "Włącz")
        out = proxy._klima_signals(self._write_ir(["cool_22", "off"]), "/sd/k.ir")
        self.assertEqual(out["power_on"], {})
        self.assertEqual(out["modes"], {"cool": [22]})

    def test_missing_file(self):
        self.assertEqual(proxy._klima_signals("/no/such/dir/klima.ir", "/sd/k.ir"), {})

    def test_no_signal_lines(self):
        self.assertEqual(proxy._klima_signals(self._write_ir([]), "/sd/k.ir"), {})

    def test_only_empty_names(self):
        self.assertEqual(proxy._klima_signals(self._write_ir(["", ""]), "/sd/k.ir"), {})

    def test_bad_digit_suffix_is_preset_not_crash(self):
        # str.isdigit() is True for "²" but int("²") raises; a 5000-digit run trips int()'s limit.
        # Both must fall to presets — the import-time parse must never crash on a crafted name.
        big = "9" * 5000
        out = proxy._klima_signals(self._write_ir(["cool_²", f"heat_{big}", "cool_22"]), "/sd/k.ir")
        self.assertEqual(out["modes"], {"cool": [22]})
        self.assertEqual(out["presets"], ["cool_²", f"heat_{big}"])


# --- engine: ir as an effect action ------------------------------------------

class IrActionTests(unittest.TestCase):
    def setUp(self):
        self.eng = AutomationEngine(AutomationStore("unused.json"))

    def test_rule_requires_file_and_button(self):
        for actions in ([{"op": "ir", "file": "f"}],                 # no button
                        [{"op": "ir", "button": "b"}],               # no file
                        [{"op": "ir", "file": "", "button": "b"}],   # empty file
                        [{"op": "ir", "file": "f", "button": 5}]):   # non-str button
            with self.assertRaises(ValueError):
                Rule.from_dict({"id": "r", "trigger": dict(_SCENE), "actions": actions})

    def test_rule_accepts_valid_ir(self):
        rule = _ir_rule()
        self.assertEqual(rule.actions[0], {"op": "ir", "file": "/ext/infrared/Klima.ir", "button": "Power"})

    def test_fire_enqueues_and_returns_no_frame(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        frames = self.eng._fire(rt, _ir_rule())
        self.assertEqual(frames, [])                              # ir produces no device frame
        self.assertEqual(rt.ir_queue.get_nowait(), ("/ext/infrared/Klima.ir", "Power", None))

    def test_fire_mixed_ir_and_frame(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        rule = Rule.from_dict({"id": "r", "trigger": dict(_SCENE), "actions": [
            {"op": "switch", "node": 5, "on": True},
            {"op": "ir", "file": "/k.ir", "button": "Power"}]})
        frames = self.eng._fire(rt, rule)
        self.assertEqual(len(frames), 1)                          # the switch frame only
        self.assertEqual(rt.ir_queue.get_nowait(), ("/k.ir", "Power", None))

    def test_dispatch_ir_no_queue_skips(self):
        rt = proxy.ProxyRuntime()                                 # ir_queue defaults None
        with self.assertLogs("hestia.automations", level="WARNING"):
            self.assertEqual(self.eng._fire(rt, _ir_rule()), [])

    def test_dispatch_ir_full_queue_drops(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=1)
        rt.ir_queue.put_nowait(("busy", "x", None))
        with self.assertLogs("hestia.automations", level="WARNING"):
            self.eng._fire(rt, _ir_rule())
        self.assertEqual(rt.ir_queue.get_nowait(), ("busy", "x", None))   # the new one was dropped


# --- control op + worker -----------------------------------------------------

class IrControlOpTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled(self):
        rt = proxy.ProxyRuntime()                                 # no queue → IR off
        resp = await proxy.process_control_op(rt, {"op": "ir", "file": "f", "button": "b"})
        self.assertEqual(resp, {"ok": False, "error": "flipper IR is disabled"})

    async def test_missing_args(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue()
        resp = await proxy.process_control_op(rt, {"op": "ir", "file": "", "button": "b"})
        self.assertFalse(resp["ok"])
        self.assertIn("requires", resp["error"])

    async def test_queue_full(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=1)
        rt.ir_queue.put_nowait(("busy", "x", None))
        resp = await proxy.process_control_op(rt, {"op": "ir", "file": "f", "button": "b"})
        self.assertEqual(resp, {"ok": False, "error": "ir queue full"})

    async def test_happy(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        with mock.patch.object(flipper, "transmit_ir") as m:
            worker = asyncio.create_task(proxy._ir_worker(rt))
            resp = await proxy.process_control_op(rt, {"op": "ir", "file": "/k.ir", "button": "Power"})
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(resp, {"ok": True})
        m.assert_called_once_with("/k.ir", "Power", device=proxy.FLIPPER_DEV)

    async def test_transmit_error(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        with mock.patch.object(flipper, "transmit_ir", side_effect=FlipperError("boom")):
            worker = asyncio.create_task(proxy._ir_worker(rt))
            resp = await proxy.process_control_op(rt, {"op": "ir", "file": "/k.ir", "button": "Power"})
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertFalse(resp["ok"])
        self.assertIn("boom", resp["error"])

    async def test_timeout(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)

        def slow(*_a, **_k):
            import time
            time.sleep(0.4)                                       # outlasts the patched op timeout

        with mock.patch.object(proxy, "IR_OP_TIMEOUT", 0.05), \
             mock.patch.object(flipper, "transmit_ir", side_effect=slow):
            worker = asyncio.create_task(proxy._ir_worker(rt))
            resp = await proxy.process_control_op(rt, {"op": "ir", "file": "/k.ir", "button": "Power"})
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(resp, {"ok": False, "error": "ir transmit timed out"})


class IrWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_returns_immediately(self):
        rt = proxy.ProxyRuntime()                                 # ir_queue None
        await proxy._ir_worker(rt)                                # must return, not hang

    async def test_fire_and_forget_error_is_logged_then_continues(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        done = asyncio.get_running_loop().create_future()
        calls = []

        def fake(file, button, *, device):
            calls.append(file)
            if file == "/bad.ir":
                raise FlipperError("nope")

        rt.ir_queue.put_nowait(("/bad.ir", "X", None))            # rule action (no future) that fails
        rt.ir_queue.put_nowait(("/good.ir", "Z", None))           # rule action (no future) that succeeds
        rt.ir_queue.put_nowait(("/ok.ir", "Y", done))             # FIFO after both → resolves last
        with mock.patch.object(flipper, "transmit_ir", side_effect=fake), \
             self.assertLogs("hestia.proxy", level="WARNING") as cm:
            worker = asyncio.create_task(proxy._ir_worker(rt))
            await asyncio.wait_for(done, timeout=2.0)            # resolves only after the prior items ran
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(calls, ["/bad.ir", "/good.ir", "/ok.ir"])
        self.assertTrue(any("ir transmit failed" in line for line in cm.output))


_KLIMA_FIXTURE = {"file": "/k.ir",
                  "modes": {"cool": [22]},
                  "power_on": {"cool": [18, 22], "heat": [24], "auto": [20]},
                  "presets": ["off"]}


class ParseKlimaCommandTests(unittest.TestCase):
    """The pure signal-name → optimistic A/C state map (validated against the parsed signal map)."""

    def test_power_on_and_off(self):
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            self.assertEqual(proxy.parse_klima_command("on_cool_22"),
                             {"power": True, "mode": "cool", "temp": 22})
            self.assertEqual(proxy.parse_klima_command("on_heat_24"),
                             {"power": True, "mode": "heat", "temp": 24})
            self.assertEqual(proxy.parse_klima_command("off"), {"power": False})

    def test_unsupported_or_unknown_names_return_none(self):
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            for name in ("on_cool_99",       # temp not in the cool power_on group
                         "on_fan_18",        # fan is not a power_on mode
                         "cool_22",          # set-mode ADJUST (no on_ prefix) — not a power state
                         "on_cool_2x",       # non-digit temp
                         "on_cool_",         # empty temp
                         "on_",              # empty mode+temp
                         "garbage"):
                self.assertIsNone(proxy.parse_klima_command(name), name)

    def test_off_rejected_when_not_a_real_preset(self):
        no_off = {**_KLIMA_FIXTURE, "presets": []}
        with mock.patch.object(proxy, "KLIMA", no_off):
            self.assertIsNone(proxy.parse_klima_command("off"))


class RecordKlimaStateTests(unittest.IsolatedAsyncioTestCase):
    """``_record_klima_state``: success-only optimistic update + a live ``klima`` delta."""

    async def test_klima_file_sets_state_and_publishes(self):
        rt = proxy.ProxyRuntime()
        sub = await rt.event_bus.try_subscribe()
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            proxy._record_klima_state(rt, "/k.ir", "on_cool_22")
        self.assertEqual(rt.state.klima, {"power": True, "mode": "cool", "temp": 22})
        self.assertTrue(rt.state.dirty)
        events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        self.assertIn({"type": "klima", "klima": {"power": True, "mode": "cool", "temp": 22}}, events)

    async def test_off_retains_last_mode_and_temp(self):
        rt = proxy.ProxyRuntime()
        rt.state.klima = {"power": True, "mode": "cool", "temp": 22}
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            proxy._record_klima_state(rt, "/k.ir", "off")
        self.assertEqual(rt.state.klima, {"power": False, "mode": "cool", "temp": 22})

    async def test_non_klima_file_is_noop(self):
        rt = proxy.ProxyRuntime()
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            proxy._record_klima_state(rt, "/other.ir", "on_cool_22")
        self.assertIsNone(rt.state.klima)
        self.assertFalse(rt.state.dirty)

    async def test_unrecognised_button_is_noop(self):
        rt = proxy.ProxyRuntime()
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            proxy._record_klima_state(rt, "/k.ir", "cool_22")        # set-mode adjust → not a power state
        self.assertIsNone(rt.state.klima)

    async def test_unchanged_resend_publishes_nothing(self):
        rt = proxy.ProxyRuntime()
        rt.state.klima = {"power": True, "mode": "cool", "temp": 22}
        rt.state.dirty = False
        sub = await rt.event_bus.try_subscribe()
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE):
            proxy._record_klima_state(rt, "/k.ir", "on_cool_22")     # identical → idempotent
        self.assertFalse(rt.state.dirty)
        self.assertEqual(sub.queue.qsize(), 0)


class IrWorkerKlimaTests(unittest.IsolatedAsyncioTestCase):
    """The worker records klima state ONLY after a successful transmit."""

    async def test_success_records_klima(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE), \
             mock.patch.object(flipper, "transmit_ir"):
            worker = asyncio.create_task(proxy._ir_worker(rt))
            resp = await proxy.process_control_op(rt, {"op": "ir", "file": "/k.ir", "button": "on_cool_22"})
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(resp["ok"])
        self.assertEqual(rt.state.klima, {"power": True, "mode": "cool", "temp": 22})

    async def test_failed_transmit_does_not_record(self):
        rt = proxy.ProxyRuntime()
        rt.ir_queue = asyncio.Queue(maxsize=4)
        with mock.patch.object(proxy, "KLIMA", _KLIMA_FIXTURE), \
             mock.patch.object(flipper, "transmit_ir", side_effect=FlipperError("boom")):
            worker = asyncio.create_task(proxy._ir_worker(rt))
            resp = await proxy.process_control_op(rt, {"op": "ir", "file": "/k.ir", "button": "on_cool_22"})
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        self.assertFalse(resp["ok"])
        self.assertIsNone(rt.state.klima)                            # no fake state on a failed send


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
