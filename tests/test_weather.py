"""Tests for hestia.weather — the stdlib Open-Meteo outdoor-temperature fetch.

No real network: every test mocks ``urllib.request.urlopen`` (the only outbound call).
"""
from __future__ import annotations

import http.client
import io
import json
import unittest
from unittest import mock

from hestia import weather


class _TruncatedResp:
    """A urlopen() stand-in whose body read fails mid-stream (server dropped the connection) —
    ``json.load(resp)`` calls ``read()``, which raises ``IncompleteRead`` (an HTTPException, NOT an
    OSError), the case that would otherwise escape the fetch's catch."""
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, *_a):
        raise http.client.IncompleteRead(b"partial")


def _resp(payload):
    """A urlopen() stand-in whose body is JSON. ``urlopen`` returns it; ``with ... as resp``
    yields the BytesIO (its __enter__ returns self) and ``json.load(resp)`` reads it."""
    return io.BytesIO(json.dumps(payload).encode())


class FetchOutdoorTempTests(unittest.TestCase):
    def _urlopen(self, **kw):
        return mock.patch("hestia.weather.urllib.request.urlopen", **kw)

    def test_happy_path(self):
        with self._urlopen(return_value=_resp({"current": {"temperature_2m": 5.3}})):
            self.assertEqual(weather.fetch_outdoor_temp(52.2, 21.0), 5.3)

    def test_int_coerced_to_float(self):
        with self._urlopen(return_value=_resp({"current": {"temperature_2m": 5}})):
            value = weather.fetch_outdoor_temp(52.2, 21.0)
        self.assertEqual(value, 5.0)
        self.assertIsInstance(value, float)

    def test_url_carries_coordinates(self):
        with self._urlopen(return_value=_resp({"current": {"temperature_2m": 1.0}})) as m:
            weather.fetch_outdoor_temp(52.25, 21.05, base_url="https://x/y")
        url = m.call_args.args[0]
        self.assertIn("latitude=52.25", url)
        self.assertIn("longitude=21.05", url)
        self.assertIn("current=temperature_2m", url)

    def test_oserror_returns_none(self):
        with self._urlopen(side_effect=OSError("no net")):
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_bad_json_returns_none(self):
        with self._urlopen(return_value=io.BytesIO(b"not json")):      # json.load -> ValueError
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_truncated_body_returns_none(self):
        # IncompleteRead (HTTPException) during body read must NOT escape — keep the "never raises".
        with self._urlopen(return_value=_TruncatedResp()):
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_missing_current_key(self):
        with self._urlopen(return_value=_resp({})):                    # data["current"] -> KeyError
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_missing_temperature_key(self):
        with self._urlopen(return_value=_resp({"current": {}})):       # ["temperature_2m"] -> KeyError
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_shape_typeerror_toplevel_list(self):
        with self._urlopen(return_value=_resp([])):                    # list["current"] -> TypeError
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_shape_typeerror_current_none(self):
        with self._urlopen(return_value=_resp({"current": None})):     # None["temperature_2m"] -> TypeError
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_shape_typeerror_current_list(self):
        with self._urlopen(return_value=_resp({"current": []})):       # list["temperature_2m"] -> TypeError
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_non_finite_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):        # json.dumps emits NaN/Infinity
            with self._urlopen(return_value=_resp({"current": {"temperature_2m": bad}})):
                self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_bool_rejected(self):
        with self._urlopen(return_value=_resp({"current": {"temperature_2m": True}})):
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))

    def test_non_number_rejected(self):
        with self._urlopen(return_value=_resp({"current": {"temperature_2m": "warm"}})):
            self.assertIsNone(weather.fetch_outdoor_temp(52.2, 21.0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
