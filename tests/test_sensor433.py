"""Tests for hestia.sensor433 — the stdlib rtl_433 local-433 outdoor-temperature read.

No real SDR / subprocess: every test injects a fake ``run`` standing in for ``subprocess.run``.
"""
from __future__ import annotations

import json
import subprocess
import types
import unittest

from hestia import sensor433


class _FakeRun:
    """A ``subprocess.run`` stand-in: records each call (cmd, kwargs) and returns an object with a
    ``stdout`` attribute, or raises ``exc`` to simulate a spawn/timeout failure."""
    def __init__(self, stdout="", exc=None, returncode=0):
        self.stdout = stdout
        self.exc = exc
        self.returncode = returncode
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc is not None:
            raise self.exc
        return types.SimpleNamespace(stdout=self.stdout, returncode=self.returncode)


def _line(**fields):
    """One rtl_433 JSON output line."""
    return json.dumps(fields)


class ReadOutdoorTempTests(unittest.TestCase):
    def test_happy_path(self):
        run = _FakeRun(stdout=_line(model="Nexus-TH", id=42, temperature_C=12.3))
        self.assertEqual(sensor433.read_outdoor_temp(run=run), 12.3)

    def test_latest_matching_wins(self):
        run = _FakeRun(stdout="\n".join([_line(temperature_C=1.0), _line(temperature_C=2.5)]))
        self.assertEqual(sensor433.read_outdoor_temp(run=run), 2.5)

    def test_int_coerced_to_float(self):
        run = _FakeRun(stdout=_line(temperature_C=5))
        value = sensor433.read_outdoor_temp(run=run)
        self.assertEqual(value, 5.0)
        self.assertIsInstance(value, float)

    def test_command_and_kwargs(self):
        run = _FakeRun(stdout=_line(temperature_C=1.0))
        sensor433.read_outdoor_temp(device="rtl_tcp:127.0.0.1:5555", window=60.0, run=run)
        cmd, kwargs = run.calls[0]
        self.assertEqual(cmd, ["rtl_433", "-d", "rtl_tcp:127.0.0.1:5555", "-F", "json", "-T", "60"])
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["timeout"], 75.0)        # window + 15 safety net

    def test_protocol_adds_R_flag(self):
        run = _FakeRun(stdout=_line(temperature_C=1.0))
        sensor433.read_outdoor_temp(protocol="73", run=run)
        self.assertIn("-R", run.calls[0][0])
        self.assertIn("73", run.calls[0][0])

    def test_no_protocol_no_R_flag(self):
        run = _FakeRun(stdout=_line(temperature_C=1.0))
        sensor433.read_outdoor_temp(run=run)
        self.assertNotIn("-R", run.calls[0][0])

    def test_model_filter_match(self):
        run = _FakeRun(stdout=_line(model="Nexus-TH", temperature_C=9.0))
        self.assertEqual(sensor433.read_outdoor_temp(model="Nexus-TH", run=run), 9.0)

    def test_model_filter_skips_other(self):
        run = _FakeRun(stdout=_line(model="Other", temperature_C=9.0))
        self.assertIsNone(sensor433.read_outdoor_temp(model="Nexus-TH", run=run))

    def test_no_model_filter_accepts_any(self):
        run = _FakeRun(stdout=_line(model="Whatever", temperature_C=9.0))
        self.assertEqual(sensor433.read_outdoor_temp(model=None, run=run), 9.0)

    def test_id_filter_match(self):
        run = _FakeRun(stdout=_line(id=42, temperature_C=7.0))
        self.assertEqual(sensor433.read_outdoor_temp(sensor_id="42", run=run), 7.0)

    def test_id_filter_skips_other(self):
        run = _FakeRun(stdout=_line(id=99, temperature_C=7.0))
        self.assertIsNone(sensor433.read_outdoor_temp(sensor_id="42", run=run))

    def test_missing_binary_returns_none(self):
        run = _FakeRun(exc=FileNotFoundError("rtl_433 not found"))
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_oserror_returns_none(self):
        run = _FakeRun(exc=OSError("spawn failed"))
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_timeout_returns_none(self):
        run = _FakeRun(exc=subprocess.TimeoutExpired(cmd="rtl_433", timeout=75.0))
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_nonzero_exit_returns_none(self):
        # rtl_433 emitted a valid line but exited non-zero (e.g. rtl_tcp dropped) -> discard, keep last.
        run = _FakeRun(stdout=_line(temperature_C=9.0), returncode=1)
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_empty_stdout_returns_none(self):
        self.assertIsNone(sensor433.read_outdoor_temp(run=_FakeRun(stdout="")))

    def test_stdout_none_returns_none(self):
        self.assertIsNone(sensor433.read_outdoor_temp(run=_FakeRun(stdout=None)))

    def test_blank_lines_skipped(self):
        run = _FakeRun(stdout="\n   \n" + _line(temperature_C=3.3))
        self.assertEqual(sensor433.read_outdoor_temp(run=run), 3.3)

    def test_non_json_line_skipped(self):
        run = _FakeRun(stdout="not json\n" + _line(temperature_C=4.4))
        self.assertEqual(sensor433.read_outdoor_temp(run=run), 4.4)

    def test_non_dict_json_skipped(self):
        run = _FakeRun(stdout="123\n" + _line(temperature_C=5.5))     # bare int is valid JSON, not a dict
        self.assertEqual(sensor433.read_outdoor_temp(run=run), 5.5)

    def test_missing_temperature_returns_none(self):
        run = _FakeRun(stdout=_line(model="Nexus-TH", id=42, humidity=55))
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_non_finite_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):       # json emits NaN/Infinity
            run = _FakeRun(stdout=_line(temperature_C=bad))
            self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_bool_rejected(self):
        run = _FakeRun(stdout=_line(temperature_C=True))
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))

    def test_non_number_rejected(self):
        run = _FakeRun(stdout=_line(temperature_C="warm"))
        self.assertIsNone(sensor433.read_outdoor_temp(run=run))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
