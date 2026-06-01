"""Unit tests for the per-install device registry (hestia.registry)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hestia.registry import Registry, _key


class KeyTests(unittest.TestCase):
    def test_normalises_int_decimal_hex(self):
        self.assertEqual(_key(5), "5")
        self.assertEqual(_key("5"), "5")
        self.assertEqual(_key("0x05"), "5")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "registry.json"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_load_missing_is_empty(self):
        self.assertEqual(Registry.load(self.path).nodes, {})

    def test_load_corrupt_starts_empty(self):
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(Registry.load(self.path).nodes, {})

    def test_roundtrip_with_unicode(self):
        reg = Registry(self.path)
        reg.set_user(5, name="Room C", room="Room D")
        reg.observe(5, "blind", "inferred", power="mains")
        reg.save()
        again = Registry.load(self.path)
        self.assertEqual(again.nodes["5"]["name"], "Room C")
        self.assertEqual(again.nodes["5"]["power"], "mains")

    def test_save_creates_parent_dir(self):
        nested = self.dir / "a" / "b" / "registry.json"
        Registry(nested).save()
        self.assertTrue(nested.exists())

    def test_save_cleans_tmp_on_error(self):
        reg = Registry(self.path)
        reg.observe(5, "blind", "inferred")
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                reg.save()
        self.assertEqual(list(self.dir.glob("*.tmp")), [])   # temp file removed

    def test_observe_new_then_update(self):
        reg = Registry(self.path)
        reg.observe(5, "blind", "inferred")                  # power None branch
        self.assertEqual(reg.nodes["5"]["type"], "blind")
        self.assertNotIn("power", reg.nodes["5"])
        reg.observe(5, "blind", "probable", power="mains")   # existing node + power
        self.assertEqual(reg.nodes["5"]["power"], "mains")
        self.assertEqual(reg.nodes["5"]["confidence"], "probable")

    def test_observe_returns_true_only_on_real_change(self):
        reg = Registry(self.path)
        self.assertTrue(reg.observe(5, "blind", "inferred", power="mains"))   # new node → changed
        self.assertFalse(reg.observe(5, "blind", "inferred", power="mains"))  # identical → no change
        self.assertTrue(reg.observe(5, "blind", "inferred", battery=80))      # battery appeared
        self.assertFalse(reg.observe(5, "blind", "inferred", battery=80))     # same battery

    def test_observe_battery_persists_dirty_on_change(self):
        reg = Registry(self.path)
        reg.observe(12, "thermostat", "inferred", battery=74)
        self.assertEqual(reg.nodes["12"]["battery"], 74)
        self.assertTrue(reg.dirty)
        reg.dirty = False
        reg.observe(12, "thermostat", "inferred", battery=74)   # unchanged → no churn
        self.assertFalse(reg.dirty)
        reg.observe(12, "thermostat", "inferred", battery=70)   # drained → persist
        self.assertTrue(reg.dirty)
        self.assertEqual(reg.nodes["12"]["battery"], 70)

    def test_name_does_not_freeze_type_but_dtype_does(self):
        reg = Registry(self.path)
        reg.set_user(5, name="Room A")                        # name only → NOT type_confirmed
        reg.observe(5, "blind", "inferred")                  # auto-type still applies
        self.assertEqual(reg.nodes["5"]["type"], "blind")
        reg.set_user(5, dtype="cover")                       # confirm the type
        reg.observe(5, "light", "inferred")                  # must NOT overwrite now
        self.assertEqual(reg.nodes["5"]["type"], "cover")
        self.assertEqual(reg.nodes["5"]["confidence"], "confirmed")

    def test_set_user_all_fields(self):
        reg = Registry(self.path)
        reg.set_user(7, name="n", room="r", dtype="light")
        entry = reg.nodes["7"]
        self.assertEqual(
            (entry["name"], entry["room"], entry["type"], entry["confidence"]),
            ("n", "r", "light", "confirmed"),
        )
        self.assertTrue(entry["type_confirmed"])

    def test_set_user_room_only(self):
        reg = Registry(self.path)
        reg.set_user(8, room="Room B")                      # name None, dtype None branches
        entry = reg.nodes["8"]
        self.assertEqual(entry["room"], "Room B")
        self.assertNotIn("name", entry)
        self.assertNotIn("type_confirmed", entry)

    def test_set_user_endpoint_name(self):
        reg = Registry(self.path)
        reg.set_user(7, name="node-name")                    # node-level name first
        reg.set_user(7, ep=2, name="Room E")               # endpoint label → endpoint_names
        entry = reg.nodes["7"]
        self.assertEqual(entry["name"], "node-name")          # node name untouched
        self.assertEqual(entry["endpoint_names"], {"2": "Room E"})

    def test_set_user_endpoint_without_name_noop(self):
        reg = Registry(self.path)
        reg.set_user(7, ep=1)                                # ep but no name → no-op, no ghost entry
        self.assertNotIn("7", reg.nodes)
        self.assertFalse(reg.dirty)

    def test_set_user_all_none_noop(self):
        reg = Registry(self.path)
        reg.set_user(9)                                      # nothing to write → no entry, not dirty
        self.assertNotIn("9", reg.nodes)
        self.assertFalse(reg.dirty)

    def test_snapshot_is_a_copy(self):
        reg = Registry(self.path)
        reg.observe(5, "blind", "inferred")
        snap = reg.snapshot()
        snap["nodes"].pop("5")                               # mutate the snapshot
        self.assertIn("5", reg.nodes)                        # registry untouched
        self.assertEqual(snap["schema"], 2)
        self.assertEqual(snap["mode"], "proxy")              # default mode persisted in the snapshot

    def test_dirty_flag_tracking(self):
        reg = Registry(self.path)
        self.assertFalse(reg.dirty)
        reg.observe(5, "blind", "inferred")
        self.assertTrue(reg.dirty)
        reg.save()
        self.assertFalse(reg.dirty)                          # cleared after save
        reg.set_user(5, name="x")
        self.assertTrue(reg.dirty)                           # set_user also dirties

    def test_unknown_does_not_overwrite_real_type(self):
        reg = Registry(self.path)
        reg.observe(5, "blind", "inferred")
        reg.observe(5, "unknown", "unknown")                 # must NOT clobber the real type
        self.assertEqual(reg.nodes["5"]["type"], "blind")

    def test_unknown_sets_when_no_prior(self):
        reg = Registry(self.path)
        reg.observe(5, "unknown", "unknown")                 # nothing better → write it
        self.assertEqual(reg.nodes["5"]["type"], "unknown")

    def test_observe_dirty_only_on_change(self):
        reg = Registry(self.path)
        reg.observe(5, "blind", "inferred")
        reg.save()
        self.assertFalse(reg.dirty)
        reg.observe(5, "blind", "inferred")                  # identical → no dirty
        self.assertFalse(reg.dirty)
        reg.observe(5, "blind", "probable")                  # confidence changed → dirty
        self.assertTrue(reg.dirty)

    def test_record_scene_new_dirties_and_creates_entry(self):
        reg = Registry(self.path)
        self.assertTrue(reg.record_scene(2, 3, "deadbeef"))  # unseen node → entry created
        self.assertEqual(reg.nodes["2"]["scenes"]["3"], "deadbeef")
        self.assertTrue(reg.dirty)

    def test_record_scene_identical_is_noop(self):
        reg = Registry(self.path)
        reg.record_scene(2, 3, "deadbeef")
        reg.save()
        self.assertFalse(reg.dirty)
        self.assertFalse(reg.record_scene(2, 3, "deadbeef"))  # identical → no change, no dirty
        self.assertFalse(reg.dirty)

    def test_record_scene_overwrite(self):
        reg = Registry(self.path)
        reg.record_scene(2, 3, "deadbeef")
        self.assertTrue(reg.record_scene(2, 3, "cafe"))       # different batch → overwrite
        self.assertEqual(reg.nodes["2"]["scenes"]["3"], "cafe")

    def test_record_scene_persists(self):
        reg = Registry(self.path)
        reg.record_scene("0x02", 3, "deadbeef")               # hex node key normalised
        reg.save()
        again = Registry.load(self.path)
        self.assertEqual(again.scene_batch(2, 3), "deadbeef")

    def test_scene_batch_misses(self):
        reg = Registry(self.path)
        self.assertIsNone(reg.scene_batch(2, 3))              # unknown node
        reg.record_scene(2, 3, "deadbeef")
        self.assertIsNone(reg.scene_batch(2, 9))              # known node, unknown scene
        self.assertEqual(reg.scene_batch(2, 3), "deadbeef")


class RegistryModeTests(unittest.TestCase):
    """Phase-3 graduation: the persisted runtime `mode` field + __main__.resolve_mode."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "registry.json"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_default_mode_proxy(self):
        self.assertEqual(Registry(self.path).mode, "proxy")

    def test_bad_mode_coerced_to_proxy(self):
        self.assertEqual(Registry(self.path, mode="bogus").mode, "proxy")

    def test_set_mode_dirties_and_round_trips(self):
        reg = Registry(self.path)
        reg.set_mode("standalone")
        self.assertEqual(reg.mode, "standalone")
        self.assertTrue(reg.dirty)
        reg.save()
        self.assertEqual(Registry.load(self.path).mode, "standalone")

    def test_set_mode_rejects_unknown(self):
        with self.assertRaises(ValueError):
            Registry(self.path).set_mode("nope")

    def test_payload_for_mode_rejects_unknown(self):
        with self.assertRaises(ValueError):
            Registry(self.path).payload_for_mode("nope")

    def test_load_schema1_defaults_proxy(self):
        self.path.write_text('{"schema": 1, "nodes": {}}', encoding="utf-8")   # legacy, no mode key
        self.assertEqual(Registry.load(self.path).mode, "proxy")

    def test_load_bad_mode_coerced(self):
        self.path.write_text('{"schema": 2, "mode": "weird", "nodes": {}}', encoding="utf-8")
        self.assertEqual(Registry.load(self.path).mode, "proxy")

    def test_resolve_mode_env_override_lowercased(self):
        from hestia.__main__ import resolve_mode
        self.assertEqual(resolve_mode("Standalone", self.path), "standalone")   # env wins (no registry read)

    def test_resolve_mode_uses_persisted(self):
        from hestia.__main__ import resolve_mode
        Registry(self.path, mode="standalone").save()
        self.assertEqual(resolve_mode(None, self.path), "standalone")

    def test_resolve_mode_default_proxy(self):
        from hestia.__main__ import resolve_mode
        self.assertEqual(resolve_mode(None, self.path), "proxy")               # missing file → proxy


if __name__ == "__main__":
    unittest.main()
