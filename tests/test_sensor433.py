"""Tests for hestia.sensor433 — the stdlib rtl_433 local-433 streaming PUSH reader.

No real SDR / subprocess: every test injects a fake ``create`` standing in for
``asyncio.create_subprocess_exec`` and a fake process whose stdout yields pre-set byte lines.
"""
from __future__ import annotations

import asyncio
import json
import unittest

from hestia import sensor433


def _line(**fields) -> bytes:
    """One rtl_433 JSON output line, as the bytes a stdout StreamReader would yield."""
    return (json.dumps(fields) + "\n").encode("utf-8")


class _FakeStdout:
    """An ``asyncio`` StreamReader stand-in: async-iterates the given byte lines, then EOF."""
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    """A ``create_subprocess_exec`` result: records terminate/wait so reaping can be asserted."""
    def __init__(self, lines=(), stdout=True, returncode=None, exit_code=0):
        self.stdout = _FakeStdout(lines) if stdout else None
        self.returncode = returncode
        self._exit_code = exit_code
        self.terminated = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True                         # signal sent; the process reaps on wait()

    async def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


def _make_create(proc=None, exc=None):
    """A fake ``create`` (async). Records the argv/kwargs of each call, or raises ``exc`` to simulate a spawn failure."""
    calls = []

    async def create(*cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if exc is not None:
            raise exc
        return proc

    create.calls = calls
    return create


async def _drain(create, **kw):
    """Run ``stream_readings`` to EOF, collecting every pushed Reading."""
    out = []

    async def on_reading(reading):
        out.append(reading)

    await sensor433.stream_readings(on_reading, create=create, **kw)
    return out


class StreamReadingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_temp_and_humidity(self):
        proc = _FakeProc([_line(model="Prologue-TH", id=204, temperature_C=21.1, humidity=44)])
        out = await _drain(_make_create(proc))
        self.assertEqual(out, [sensor433.Reading(21.1, 44.0)])

    async def test_humidity_absent_is_none(self):
        proc = _FakeProc([_line(model="Nexus-TH", id=42, temperature_C=12.3)])
        out = await _drain(_make_create(proc))
        self.assertEqual(out, [sensor433.Reading(12.3, None)])

    async def test_each_matching_line_pushed(self):
        proc = _FakeProc([_line(temperature_C=1.0), _line(temperature_C=2.5, humidity=50)])
        out = await _drain(_make_create(proc))
        self.assertEqual(out, [sensor433.Reading(1.0, None), sensor433.Reading(2.5, 50.0)])

    async def test_int_coerced_to_float(self):
        proc = _FakeProc([_line(temperature_C=5, humidity=40)])
        out = await _drain(_make_create(proc))
        self.assertEqual(out, [sensor433.Reading(5.0, 40.0)])
        self.assertIsInstance(out[0].temperature_C, float)
        self.assertIsInstance(out[0].humidity, float)

    async def test_command_no_T_and_pipes(self):
        create = _make_create(_FakeProc([_line(temperature_C=1.0)]))
        await _drain(create, device="rtl_tcp:127.0.0.1:5555")
        cmd, kwargs = create.calls[0]
        self.assertEqual(cmd, ["rtl_433", "-d", "rtl_tcp:127.0.0.1:5555", "-F", "json"])
        self.assertNotIn("-T", cmd)                    # streaming: rtl_433 runs until terminated
        self.assertEqual(kwargs["stdout"], asyncio.subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], asyncio.subprocess.DEVNULL)

    async def test_protocol_adds_R_flag(self):
        create = _make_create(_FakeProc([_line(temperature_C=1.0)]))
        await _drain(create, protocol="73")
        self.assertIn("-R", create.calls[0][0])
        self.assertIn("73", create.calls[0][0])

    async def test_no_protocol_no_R_flag(self):
        create = _make_create(_FakeProc([_line(temperature_C=1.0)]))
        await _drain(create)
        self.assertNotIn("-R", create.calls[0][0])

    async def test_model_filter_match(self):
        proc = _FakeProc([_line(model="Nexus-TH", temperature_C=9.0)])
        out = await _drain(_make_create(proc), model="Nexus-TH")
        self.assertEqual(out, [sensor433.Reading(9.0, None)])

    async def test_model_filter_skips_other(self):
        proc = _FakeProc([_line(model="Other", temperature_C=9.0)])
        self.assertEqual(await _drain(_make_create(proc), model="Nexus-TH"), [])

    async def test_no_model_filter_accepts_any(self):
        proc = _FakeProc([_line(model="Whatever", temperature_C=9.0)])
        self.assertEqual(await _drain(_make_create(proc), model=None), [sensor433.Reading(9.0, None)])

    async def test_id_filter_match(self):
        proc = _FakeProc([_line(id=42, temperature_C=7.0)])
        self.assertEqual(await _drain(_make_create(proc), sensor_id="42"), [sensor433.Reading(7.0, None)])

    async def test_id_filter_skips_other(self):
        proc = _FakeProc([_line(id=99, temperature_C=7.0)])
        self.assertEqual(await _drain(_make_create(proc), sensor_id="42"), [])

    async def test_same_id_without_temperature_skipped(self):
        # a keyfob press on the same id has no temperature_C -> ignored (not a weather reading)
        proc = _FakeProc([_line(model="Microchip-HCS200", id=42, button=1)])
        self.assertEqual(await _drain(_make_create(proc), sensor_id="42"), [])

    async def test_temp_non_finite_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            proc = _FakeProc([_line(temperature_C=bad)])
            self.assertEqual(await _drain(_make_create(proc)), [])

    async def test_temp_bool_rejected(self):
        proc = _FakeProc([_line(temperature_C=True)])
        self.assertEqual(await _drain(_make_create(proc)), [])

    async def test_temp_non_number_rejected(self):
        proc = _FakeProc([_line(temperature_C="warm")])
        self.assertEqual(await _drain(_make_create(proc)), [])

    async def test_humidity_garbage_becomes_none_temp_kept(self):
        for bad in (float("nan"), True, "wet"):
            proc = _FakeProc([_line(temperature_C=10.0, humidity=bad)])
            self.assertEqual(await _drain(_make_create(proc)), [sensor433.Reading(10.0, None)])

    async def test_blank_and_garbage_lines_skipped(self):
        proc = _FakeProc([b"\n", b"   \n", b"not json\n", b"123\n", _line(temperature_C=3.3)])
        self.assertEqual(await _drain(_make_create(proc)), [sensor433.Reading(3.3, None)])

    async def test_spawn_failure_returns_no_readings(self):
        for exc in (FileNotFoundError("rtl_433 not found"), OSError("spawn failed"), ValueError("bad argv")):
            self.assertEqual(await _drain(_make_create(exc=exc)), [])

    async def test_stdout_none_returns_and_reaps(self):
        proc = _FakeProc(stdout=False)
        self.assertEqual(await _drain(_make_create(proc)), [])
        self.assertTrue(proc.terminated and proc.waited)   # still reaped

    async def test_eof_terminates_and_reaps(self):
        proc = _FakeProc([_line(temperature_C=1.0)])       # returncode None -> still "running" at EOF
        await _drain(_make_create(proc))
        self.assertTrue(proc.terminated)                   # signalled
        self.assertTrue(proc.waited)                        # and reaped

    async def test_already_exited_not_terminated_but_reaped(self):
        proc = _FakeProc([], returncode=0)                  # rtl_433 already gone (e.g. rtl_tcp dropped)
        await _drain(_make_create(proc))
        self.assertFalse(proc.terminated)                   # no signal to a dead process
        self.assertTrue(proc.waited)                        # reaped (returns the existing code)

    async def test_cancel_mid_stream_reaps_and_propagates(self):
        proc = _FakeProc([_line(temperature_C=1.0)])

        async def on_reading(_r):
            raise asyncio.CancelledError                    # simulate shutdown cancelling us mid-reading

        create = _make_create(proc)
        with self.assertRaises(asyncio.CancelledError):
            await sensor433.stream_readings(on_reading, create=create)
        self.assertTrue(proc.terminated and proc.waited)    # child reaped before cancellation propagated

    async def test_terminate_tolerates_already_reaped_child(self):
        class _RacyProc(_FakeProc):
            def terminate(self):
                raise ProcessLookupError                    # child exited between the None check and the signal
        proc = _RacyProc([_line(temperature_C=1.0)])
        out = await _drain(_make_create(proc))              # must not raise
        self.assertEqual(out, [sensor433.Reading(1.0, None)])
        self.assertTrue(proc.waited)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
