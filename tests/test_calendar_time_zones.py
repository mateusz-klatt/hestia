"""Declared timezone aliases: follow the same data-source priority as ZoneInfo."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hestia.calendar_sync import time_zones
from hestia.calendar_sync.events import fingerprint, from_google_event, from_ical, to_ical


class CalendarTimeZoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clear_caches()
        self.addCleanup(self.clear_caches)

    @staticmethod
    def clear_caches():
        time_zones._file_links.cache_clear()
        time_zones._package_links.cache_clear()

    def source(self, name, zones, manifest=None):
        root = self.root / name
        root.mkdir()
        for zone in zones:
            path = root / zone
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        if manifest is not None:
            (root / "tzdata.zi").write_text(manifest)
        return root

    def test_link_short_and_long_forms_follow_chains_and_ignore_other_lines(self):
        root = self.source("system", ["US/Eastern", "America/New_York", "Europe/Skopje", "Europe/Belgrade", "Legacy/East"],
                           "# version test\nL America/New_York US/Eastern\nLink Europe/Belgrade Europe/Skopje # declared link\nL US/Eastern Legacy/East\nZ America/New_York -4:56 - LMT\nL incomplete\nBogus X Y\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "America/New_York")
            self.assertEqual(time_zones.canonical_zone("Europe/Skopje"), "Europe/Belgrade")
            self.assertEqual(time_zones.canonical_zone("Legacy/East"), "America/New_York")
            self.assertEqual(time_zones.canonical_zone("America/New_York"), "America/New_York")

    def test_system_zone_uses_its_own_manifest_not_another_path_or_package(self):
        first = self.source("first", ["Europe/Skopje"], "Z Europe/Skopje 1:25:44 - LMT\n")
        second = self.source("second", ["Europe/Skopje", "Europe/Belgrade"], "L Europe/Belgrade Europe/Skopje\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(first), str(second))), mock.patch.object(time_zones.resources, "files") as package:
            self.assertEqual(time_zones.canonical_zone("Europe/Skopje"), "Europe/Skopje")
            package.assert_not_called()
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(first), str(second))):
            self.assertEqual(time_zones.canonical_zone("Europe/Belgrade"), "Europe/Belgrade")

    def test_link_target_cannot_switch_to_a_different_database_with_higher_priority(self):
        first = self.source("first-target", ["America/New_York"])
        second = self.source("second-alias", ["US/Eastern", "America/New_York"], "L America/New_York US/Eastern\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(first), str(second))):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "US/Eastern")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(first),)), mock.patch.object(time_zones.resources, "files", return_value=second):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "US/Eastern")

    def test_missing_unreadable_or_badly_encoded_system_manifest_keeps_source_key(self):
        root = self.source("no-manifest", ["US/Eastern"])
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)), mock.patch.object(time_zones.resources, "files") as package:
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "US/Eastern")
            package.assert_not_called()
        self.clear_caches()
        (root / "tzdata.zi").write_bytes(b"\xff")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "US/Eastern")

    def test_cycles_and_missing_link_targets_preserve_original_names(self):
        root = self.source("broken", ["Cycle/A", "Cycle/B", "Partial/A"],
                           "L Cycle/B Cycle/A\nL Cycle/A Cycle/B\nL Missing/Target Partial/A\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)):
            self.assertEqual(time_zones.canonical_zone("Cycle/A"), "Cycle/A")
            self.assertEqual(time_zones.canonical_zone("Cycle/B"), "Cycle/B")
            self.assertEqual(time_zones.canonical_zone("Partial/A"), "Partial/A")

    def test_package_fallback_only_when_package_supplies_the_zone(self):
        package = self.source("package", ["US/Eastern", "America/New_York"], "L America/New_York US/Eastern\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", ()), mock.patch.object(time_zones.resources, "files", return_value=package):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "America/New_York")
            self.assertEqual(time_zones.canonical_zone("Absent/Zone"), "Absent/Zone")

    def test_unavailable_or_incomplete_package_preserves_names(self):
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", ()), mock.patch.object(time_zones.resources, "files", side_effect=ModuleNotFoundError):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "US/Eastern")
            self.assertEqual(time_zones._package_links(), {})
        self.clear_caches()
        package = self.source("no-package-manifest", ["US/Eastern"])
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", ()), mock.patch.object(time_zones.resources, "files", return_value=package):
            self.assertEqual(time_zones.canonical_zone("US/Eastern"), "US/Eastern")

    def test_same_offsets_without_a_declared_link_never_collapse(self):
        root = self.source("distinct", ["Europe/Paris", "Europe/Berlin"], "Z Europe/Paris 0:9:21 - LMT\nZ Europe/Berlin 0:53:28 - LMT\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)):
            self.assertEqual(time_zones.canonical_zone("Europe/Paris"), "Europe/Paris")
            self.assertEqual(time_zones.canonical_zone("Europe/Berlin"), "Europe/Berlin")

    def test_intrinsic_utc_keeps_established_codec_spelling(self):
        root = self.source("utc", ["UTC", "Etc/UTC", "UCT"], "L Etc/UTC UTC\nL Etc/UTC UCT\n")
        with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)):
            self.assertEqual(time_zones.canonical_zone("UTC"), "UTC")
            self.assertEqual(time_zones.canonical_zone("Etc/UTC"), "UTC")
            self.assertEqual(time_zones.canonical_zone("UCT"), "UTC")

    def test_event_hashes_and_ical_roundtrip_use_declared_canonical_name(self):
        root = self.source("canonical", ["US/Eastern", "America/New_York", "Europe/Skopje", "Europe/Belgrade"],
                           "L America/New_York US/Eastern\nL Europe/Belgrade Europe/Skopje\n")
        # Temporal parsing still uses real ZoneInfo objects. Only the alias source
        # resolver is pointed at these synthetic manifests and placeholder files.
        original = time_zones.canonical_zone
        def synthetic(key):
            with mock.patch.object(time_zones.zoneinfo, "TZPATH", (str(root),)):
                return original(key)
        with mock.patch("hestia.calendar_sync.events.canonical_zone", side_effect=synthetic):
            for alias, target, offset in (("US/Eastern", "America/New_York", "-04:00"), ("Europe/Skopje", "Europe/Belgrade", "+02:00")):
                source = {"summary": "Series", "start": {"dateTime": "2026-10-21T09:00:00" + offset, "timeZone": alias},
                          "end": {"dateTime": "2026-10-21T10:00:00" + offset, "timeZone": alias},
                          "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"]}
                canonical = from_google_event(source)
                self.assertEqual(canonical["start"]["timeZone"], target)
                self.assertEqual(canonical["end"]["timeZone"], target)
                self.assertEqual(fingerprint(source), fingerprint(canonical))
                self.assertEqual(fingerprint(canonical), fingerprint(from_ical(to_ical(canonical, "canonical"))[1]))
