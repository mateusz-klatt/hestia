"""Unit tests for hestia.rf433 — the in-memory 433 MHz device-discovery roll-up."""
from __future__ import annotations

import unittest

from hestia.rf433 import Rf433Registry, _device_key, _fields


class Rf433RegistryTests(unittest.TestCase):
    def test_record_new_then_repeat_folds_into_one_device(self):
        reg = Rf433Registry()
        reg.record({"model": "Prologue-TH", "id": 204, "temperature_C": 21.1}, now=100.0)
        reg.record({"model": "Prologue-TH", "id": 204, "temperature_C": 21.5}, now=101.0)
        snap = reg.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["key"], "Prologue-TH 204")
        self.assertEqual(snap[0]["count"], 2)
        self.assertEqual(snap[0]["first_seen"], 100.0)
        self.assertEqual(snap[0]["last_seen"], 101.0)
        self.assertEqual(snap[0]["fields"]["temperature_C"], 21.5)   # last packet's fields win

    def test_snapshot_is_newest_seen_first(self):
        reg = Rf433Registry()
        reg.record({"model": "A", "id": 1}, now=100.0)
        reg.record({"model": "B", "id": 2}, now=200.0)
        self.assertEqual([d["key"] for d in reg.snapshot()], ["B 2", "A 1"])

    def test_eviction_drops_the_stalest_past_cap(self):
        reg = Rf433Registry(cap=2)
        reg.record({"model": "A"}, now=1.0)
        reg.record({"model": "B"}, now=2.0)
        reg.record({"model": "C"}, now=3.0)   # over cap → evict the stalest (A)
        self.assertEqual([d["key"] for d in reg.snapshot()], ["C", "B"])

    def test_non_dict_packet_is_ignored(self):
        reg = Rf433Registry()
        reg.record("not a dict", now=1.0)
        reg.record(None, now=1.0)
        self.assertEqual(reg.snapshot(), [])

    def test_device_key_uses_whatever_identifies(self):
        self.assertEqual(_device_key({"model": "X", "id": 5, "channel": 2}), "X 5 2")
        self.assertEqual(_device_key({"model": "X", "id": 0}), "X 0")   # id 0 is valid, not "absent"
        self.assertEqual(_device_key({"model": "X"}), "X")
        self.assertEqual(_device_key({"foo": "bar"}), "unknown")        # nothing identifying

    def test_fields_keeps_scalars_drops_noise_and_nested(self):
        kept = _fields({"model": "X", "time": "2026-06-04 09:00:00", "mic": "CRC",
                        "temperature_C": 1.0, "battery_ok": True,
                        "nested": {"a": 1}, "list": [1, 2]})
        self.assertEqual(kept, {"model": "X", "temperature_C": 1.0, "battery_ok": True})


if __name__ == "__main__":
    unittest.main()
