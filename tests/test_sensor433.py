"""Tests for hestia.sensor433 — the stdlib rtl_433 local-433 streaming PUSH reader.

No real SDR / subprocess: every test injects a fake ``create`` standing in for
``asyncio.create_subprocess_exec`` and a fake process whose stdout yields pre-set byte lines.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

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
    """A ``create_subprocess_exec`` result: records terminate/kill/wait so reaping can be asserted."""
    def __init__(self, lines=(), stdout=True, returncode=None, exit_code=0):
        self.stdout = _FakeStdout(lines) if stdout else None
        self.returncode = returncode
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.waited = False

    def terminate(self) -> None:
        self.terminated = True                         # signal sent; the process reaps on wait()

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


class _WedgedProc(_FakeProc):
    """A child that IGNORES SIGTERM: ``wait()`` only resolves once ``kill()`` (SIGKILL) is delivered."""
    def __init__(self, lines=()):
        super().__init__(lines)
        self._dead = asyncio.Event()

    async def wait(self) -> int:
        self.waited = True
        await self._dead.wait()                        # SIGTERM did nothing — stays alive until killed
        self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._dead.set()


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

    async def test_on_packet_fires_for_every_decoded_packet(self):
        packets = []
        proc = _FakeProc([_line(model="A", id=1, temperature_C=1.0),
                          _line(model="B", id=2, button=1)])   # 2nd has no temp → not a Reading
        out = await _drain(_make_create(proc), on_packet=packets.append)
        self.assertEqual([p["model"] for p in packets], ["A", "B"])    # BOTH packets, pre-filter
        self.assertEqual(out, [sensor433.Reading(1.0, None)])          # only the matching one is a reading

    async def test_on_packet_skips_blank_and_garbage_lines(self):
        packets = []
        proc = _FakeProc([b"\n", b"junk\n", _line(temperature_C=3.3)])
        await _drain(_make_create(proc), on_packet=packets.append)
        self.assertEqual(len(packets), 1)                              # only the one decoded JSON object

    async def test_spawn_failure_returns_no_readings_and_warns(self):
        for exc in (FileNotFoundError("rtl_433 not found"), OSError("spawn failed"), ValueError("bad argv")):
            with self.assertLogs("hestia.sensor433", level="WARNING") as logs:
                self.assertEqual(await _drain(_make_create(exc=exc)), [])   # config error -> no readings
            self.assertTrue(any("rtl_433 spawn failed" in m for m in logs.output))   # but NOT silent

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

    async def test_wedged_child_is_sigkilled_after_grace(self):
        proc = _WedgedProc([_line(temperature_C=1.0)])      # ignores SIGTERM -> wait() blocks until killed
        with mock.patch.object(sensor433, "_TERMINATE_GRACE", 0.02):
            out = await _drain(_make_create(proc))          # must NOT hang: TERM -> grace -> KILL -> reap
        self.assertEqual(out, [sensor433.Reading(1.0, None)])
        self.assertTrue(proc.terminated)                    # tried graceful first
        self.assertTrue(proc.killed)                         # escalated to SIGKILL
        self.assertEqual(proc.returncode, -9)                # reaped

    async def test_cancel_during_reap_kills_reaps_then_propagates(self):
        proc = _WedgedProc()                                 # wait() never resolves until SIGKILL
        with mock.patch.object(sensor433, "_TERMINATE_GRACE", 100):   # don't time out — cancel instead
            task = asyncio.create_task(sensor433._terminate(proc))
            for _ in range(3):                               # let _terminate reach the await on proc.wait()
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(proc.terminated and proc.killed)     # SIGKILL'd so it can't keep holding the SDR
        self.assertEqual(proc.returncode, -9)                # AND reaped before the cancellation propagated

    async def test_reap_defers_cancellation_until_child_reaped(self):
        class _ReapProc(_FakeProc):
            """wait() is cancelled on its 1st await, succeeds on the 2nd — a cancel landing during reap."""
            def __init__(self):
                super().__init__()
                self.waits = 0

            async def wait(self):
                self.waits += 1
                if self.waits == 1:
                    raise asyncio.CancelledError
                self.returncode = -9
                return -9
        proc = _ReapProc()
        with self.assertRaises(asyncio.CancelledError):
            await sensor433._reap(proc)
        self.assertEqual(proc.returncode, -9)                # reaped despite the cancel
        self.assertEqual(proc.waits, 2)                      # retried the wait after deferring the cancel


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
