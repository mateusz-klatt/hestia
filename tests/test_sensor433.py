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


async def _drain_routes(create, *, enable_outdoor=True, **kw):
    """Run ``stream_readings`` with optional outdoor + crib collectors."""
    outdoor = []
    crib = []

    async def on_outdoor(reading):
        outdoor.append(reading)

    async def on_crib(reading):
        crib.append(reading)

    await sensor433.stream_readings(
        on_outdoor if enable_outdoor else None,
        on_crib_reading=on_crib if kw.get("crib_model") else None,
        create=create,
        **kw,
    )
    return outdoor, crib


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

    async def test_battery_ok_flag_normalised_to_0_or_1(self):
        # rtl_433's battery_ok: 0 = low, 1 = ok; any other finite non-zero collapses to 1; bool/str/NaN
        # and an absent field -> None (so a garbled flag never reads as a bogus level / false "low").
        proc = _FakeProc([
            _line(temperature_C=1.0, battery_ok=0),      # low
            _line(temperature_C=2.0, battery_ok=1),      # ok
            _line(temperature_C=3.0, battery_ok=5),      # non-zero -> ok
            _line(temperature_C=4.0, battery_ok=True),   # bool rejected -> None
            _line(temperature_C=5.0, battery_ok="low"),  # str rejected -> None
            _line(temperature_C=6.0),                    # absent -> None
            _line(temperature_C=7.0, battery_ok=255),    # any non-zero -> ok (1), never a bogus level
        ])
        out = await _drain(_make_create(proc))
        self.assertEqual([r.battery_ok for r in out], [0, 1, 1, None, None, None, 1])

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

    async def test_extra_args_empty_keeps_exact_argv(self):
        for empty in (None, "", "   "):
            create = _make_create(_FakeProc([_line(temperature_C=1.0)]))
            await _drain(create, device="rtl_tcp:127.0.0.1:1234", extra_args=empty)
            self.assertEqual(create.calls[0][0], ["rtl_433", "-d", "rtl_tcp:127.0.0.1:1234", "-F", "json"])

    async def test_extra_args_appended_to_argv(self):
        create = _make_create(_FakeProc([_line(temperature_C=1.0)]))
        await _drain(
            create,
            device="rtl_tcp:127.0.0.1:1234",
            extra_args="-f 433.92M -f 868M -s 1024k -Y classic -H 30",
        )
        self.assertEqual(
            create.calls[0][0],
            ["rtl_433", "-d", "rtl_tcp:127.0.0.1:1234", "-F", "json",
             "-f", "433.92M", "-f", "868M", "-s", "1024k", "-Y", "classic", "-H", "30"],
        )

    async def test_extra_args_quoted_and_negative_numeric_values_allowed(self):
        create = _make_create(_FakeProc([_line(temperature_C=1.0)]))
        await _drain(
            create,
            protocol="245",
            extra_args="-X 'n=flex,m=OOK_PWM,s=500,l=1000,r=3000' -R -3 -p -12 -M level -qv",
        )
        self.assertEqual(
            create.calls[0][0],
            ["rtl_433", "-d", "rtl_tcp:127.0.0.1:1234", "-F", "json", "-R", "245",
             "-X", "n=flex,m=OOK_PWM,s=500,l=1000,r=3000", "-R", "-3", "-p", "-12", "-M", "level", "-qv"],
        )

    async def test_extra_args_validation_rejects_disallowed_and_file_writing_flags(self):
        bad_cases = [
            "-d 0",
            "-d0",
            "-F csv",
            "-F json:/tmp/out.json",
            "-Fcsv:/tmp/out.csv",
            "-T 30",
            "-T30",
            "-w /tmp/raw.cu8",
            "-w/tmp/raw.cu8",
            "-w-",
            "-W /tmp/raw.cu8",
            "-W/tmp/raw.cu8",
            "-S all",
            "-S unknown",
            "-Sknown",
            "-c /tmp/rtl_433.conf",
            "-qw /tmp/raw.cu8",
            "-vd0",
            "--output=csv:/tmp/out.csv",
            "--write-file=/tmp/raw.cu8",
            "--",
            "-f 433.92M > /tmp/out",
            "-f 433.92M 868M",
            "-",
            "-f",
            "-f -w",
            "-f 'unclosed",
            "-f 433.92M\x00bad",
            "-Z 1",
        ]
        for bad in bad_cases:
            with self.subTest(extra_args=bad):
                create = _make_create(_FakeProc([]))
                with self.assertRaises(ValueError):
                    await _drain(create, extra_args=bad)
                self.assertEqual(create.calls, [])           # never spawned rtl_433

    async def test_routing_outdoor_vs_crib_vs_neither(self):
        proc = _FakeProc([
            _line(model="Prologue-TH", id=227, channel=1, temperature_C=4.2, humidity=80, battery_ok=1),
            _line(model="Thermopro-TX2C", id=211, channel=3, temperature_C=21.6, humidity=48, battery_ok=1),
            _line(model="Thermopro-TX2C", id=99, channel=2, temperature_C=19.0, humidity=50),  # neighbour ch
            _line(model="Acme-Doorbell", id=7, button=1),                                       # neither
        ])
        packets = []
        outdoor, crib = await _drain_routes(
            _make_create(proc),
            model="Prologue-TH", sensor_id="*", channel="1",
            crib_model="Thermopro-TX2C", crib_id="*", crib_channel="3",
            on_packet=packets.append,
        )
        self.assertEqual(outdoor, [sensor433.Reading(4.2, 80.0, 1)])
        self.assertEqual(crib, [sensor433.Reading(21.6, 48.0, 1)])
        self.assertEqual(len(packets), 4)

    async def test_crib_route_only_when_outdoor_disabled(self):
        proc = _FakeProc([
            _line(model="Prologue-TH", id=227, channel=1, temperature_C=4.2),
            _line(model="Thermopro-TX2C", id=211, channel=3, temperature_C=21.6, humidity=48),
        ])
        outdoor, crib = await _drain_routes(
            _make_create(proc),
            enable_outdoor=False,
            model=None,
            crib_model="Thermopro-TX2C", crib_id="*", crib_channel="3",
        )
        self.assertEqual(outdoor, [])
        self.assertEqual(crib, [sensor433.Reading(21.6, 48.0, None)])

    async def test_ambiguous_packet_matching_both_routes_logs_once_and_skips(self):
        proc = _FakeProc([
            _line(model="Thermopro-TX2C", id=211, channel=3, temperature_C=21.6),  # matches both (outdoor=any)
            _line(model="Thermopro-TX2C", id=212, channel=3, temperature_C=22.0),  # second ambiguous packet
            _line(model="Prologue-TH", id=227, channel=1, temperature_C=5.0),      # matches outdoor only
        ])
        with self.assertLogs("hestia.sensor433", level="ERROR") as logs:
            outdoor, crib = await _drain_routes(
                _make_create(proc),
                model=None, sensor_id=None, channel=None,                          # matches ANY temp packet
                crib_model="Thermopro-TX2C", crib_id="*", crib_channel="3",
            )
        self.assertEqual(len(logs.output), 1)                                      # logged once, not twice
        self.assertIn("both outdoor and crib", logs.output[0])
        self.assertEqual(crib, [])                                                 # ambiguous packets dropped
        self.assertEqual(outdoor, [sensor433.Reading(5.0, None, None)])            # non-ambiguous still routes

    async def test_crib_wildcard_id_and_channel_survives_battery_swap(self):
        proc = _FakeProc([
            _line(model="Thermopro-TX2C", id=211, channel=3, temperature_C=21.5),
            _line(model="Thermopro-TX2C", id=88, channel=3, temperature_C=21.7),   # new random id after battery swap
            _line(model="Thermopro-TX2C", id=211, channel=1, temperature_C=18.0),  # wrong channel
            _line(model="Thermopro-TX2C", id=211, temperature_C=17.0),             # missing channel -> rejected
        ])
        outdoor, crib = await _drain_routes(
            _make_create(proc),
            enable_outdoor=False,
            crib_model="Thermopro-TX2C", crib_id="*", crib_channel="3",
        )
        self.assertEqual(outdoor, [])
        self.assertEqual(crib, [sensor433.Reading(21.5, None, None), sensor433.Reading(21.7, None, None)])

    async def test_no_active_routes_returns_without_spawning(self):
        create = _make_create(_FakeProc([]))
        outdoor, crib = await _drain_routes(create, enable_outdoor=False, crib_model=None)
        self.assertEqual((outdoor, crib), ([], []))
        self.assertEqual(create.calls, [])

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

    async def test_id_wildcard_matches_any(self):
        # '*' = match any id — for a sensor (e.g. Prologue-TH) whose id randomises on each battery change.
        proc = _FakeProc([_line(id=204, temperature_C=1.0),
                          _line(id=85, temperature_C=2.0),
                          _line(id=227, temperature_C=3.0)])
        out = await _drain(_make_create(proc), sensor_id="*")
        self.assertEqual(out, [sensor433.Reading(1.0, None), sensor433.Reading(2.0, None),
                               sensor433.Reading(3.0, None)])

    async def test_channel_filter_matches_int_and_str(self):
        # channel is str()-compared, so int 1 and str "1" both match a filter of "1".
        proc = _FakeProc([_line(channel=1, temperature_C=1.0), _line(channel="1", temperature_C=2.0)])
        out = await _drain(_make_create(proc), channel="1")
        self.assertEqual(out, [sensor433.Reading(1.0, None), sensor433.Reading(2.0, None)])

    async def test_channel_filter_skips_other(self):
        proc = _FakeProc([_line(channel=2, temperature_C=7.0)])
        self.assertEqual(await _drain(_make_create(proc), channel="1"), [])

    async def test_channel_filter_zero_is_a_real_channel(self):
        # channel "0" is truthy as a filter string and str(0)=="0" matches — a 0 channel is not "unset".
        proc = _FakeProc([_line(channel=0, temperature_C=1.0), _line(channel=1, temperature_C=2.0)])
        self.assertEqual(await _drain(_make_create(proc), channel="0"), [sensor433.Reading(1.0, None)])

    async def test_channel_filter_rejects_packet_missing_channel(self):
        # a filter is set but the packet omits channel -> a different device -> rejected
        proc = _FakeProc([_line(temperature_C=7.0)])
        self.assertEqual(await _drain(_make_create(proc), channel="1"), [])

    async def test_wildcard_id_and_channel_combined(self):
        # The robust cross-battery-change config: any id, pinned to a stable DIP-switch channel + model.
        proc = _FakeProc([_line(model="Prologue-TH", id=85, channel=1, temperature_C=1.0),
                          _line(model="Prologue-TH", id=227, channel=1, temperature_C=2.0),
                          _line(model="Prologue-TH", id=227, channel=2, temperature_C=3.0),   # wrong channel
                          _line(model="Other", id=227, channel=1, temperature_C=4.0)])         # wrong model
        out = await _drain(_make_create(proc), model="Prologue-TH", sensor_id="*", channel="1")
        self.assertEqual(out, [sensor433.Reading(1.0, None), sensor433.Reading(2.0, None)])

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
