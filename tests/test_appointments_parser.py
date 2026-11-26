"""Unit tests for the pure CMP /GetScheduler response parser."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest import TestCase

from hestia.appointments import (
    WARSAW_TZ,
    BookedAppointment,
    CmpParseError,
    WatchCriteria,
    diff_slots,
    parse_cmp_scheduler,
    parse_cmp_scheduler_bytes,
)
from hestia.appointments.config import parse_warsaw_datetime

FIXTURES = Path(__file__).parent / "fixtures" / "appointments"


class CmpParserTests(TestCase):
    def test_valid_synthetic_fixture(self) -> None:
        raw = (FIXTURES / "cmp_scheduler_valid.json").read_bytes()
        result = parse_cmp_scheduler_bytes(raw)
        self.assertEqual(result.empty_days, 2)
        self.assertEqual(result.skipped, 1)  # syn-slot-004 has available=false
        self.assertEqual(result.warnings, ())
        self.assertEqual(len(result.slots), 3)

        first, second, third = result.slots
        self.assertEqual(first.slot_id, "syn-slot-001")
        self.assertEqual(first.start, datetime(2026, 12, 10, 9, 30, tzinfo=WARSAW_TZ))
        self.assertEqual(first.end, datetime(2026, 12, 10, 9, 50, tzinfo=WARSAW_TZ))
        self.assertEqual(first.doctor_id, "doc-101")
        self.assertEqual(first.doctor_label, "Dr. Synthetic Alpha")
        self.assertEqual(first.location_id, "loc-01")
        self.assertEqual(first.location_label, "Clinic North")
        self.assertEqual(first.specialty_id, "spec-01")
        self.assertFalse(first.telemedicine)

        # Wall-clock time combined with day date, numeric IDs coerced to strings.
        self.assertEqual(second.start, datetime(2026, 12, 10, 11, 15, tzinfo=WARSAW_TZ))
        self.assertEqual(second.end, datetime(2026, 12, 10, 11, 35, tzinfo=WARSAW_TZ))
        self.assertEqual(second.doctor_id, "102")
        self.assertEqual(second.location_id, "2")
        self.assertEqual(second.specialty_id, "spec-02")
        self.assertTrue(second.telemedicine)

        # UTC Z timestamp converted to winter CET (+01:00).
        self.assertEqual(third.start, datetime(2026, 12, 10, 14, 0, tzinfo=WARSAW_TZ))
        self.assertIsNone(third.end)
        self.assertIsNone(third.doctor_label)

    def test_edge_cases_fixture_and_dst_handling(self) -> None:
        raw = (FIXTURES / "cmp_scheduler_edge_cases.json").read_bytes()
        with self.assertLogs("hestia.appointments.parser", level="WARNING") as logs:
            result = parse_cmp_scheduler_bytes(raw)
        # 2 malformed days + 5 malformed slots in the valid day = 7 skipped; 1 valid slot survives.
        self.assertEqual(result.skipped, 7)
        self.assertEqual(result.empty_days, 0)
        self.assertEqual(result.warnings, ("invalid_day", "invalid_slots_list") + ("invalid_slot_entry",) * 5)
        self.assertEqual(len(logs.output), 7)
        self.assertEqual(len(result.slots), 1)
        slot = result.slots[0]
        self.assertEqual(slot.slot_id, "syn-dst-valid")
        # 2026-03-29 03:15 is CEST (+02:00) right after the spring-forward jump.
        self.assertEqual(slot.start.utcoffset().total_seconds(), 7200)

        # Overflowing offsets and ambiguous naive DST fold are skipped per entry with safe warnings.
        overflow_payload = {
            "specialtyId": "spec-1",
            "days": [
                {
                    "date": "2026-10-25",
                    "slots": [
                        {
                            "slotId": "syn-overflow-low",
                            "startTime": "0001-01-01T00:00:00+14:00",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        },
                        {
                            "slotId": "syn-overflow-high",
                            "startTime": "9999-12-31T23:59:59-14:00",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        },
                        {
                            "slotId": "syn-ambiguous-fold",
                            "startTime": "2026-10-25T02:30:00",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        },
                        {
                            "slotId": "syn-survivor",
                            "startTime": "2026-10-25T04:00:00+01:00",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        },
                    ],
                }
            ],
        }
        with self.assertLogs("hestia.appointments.parser", level="WARNING") as overflow_logs:
            overflow_res = parse_cmp_scheduler(overflow_payload)
        self.assertEqual(overflow_res.skipped, 3)
        self.assertEqual(overflow_res.warnings, ("invalid_slot_entry",) * 3)
        self.assertEqual([s.slot_id for s in overflow_res.slots], ["syn-survivor"])
        self.assertTrue(all("0001-01-01" not in line for line in overflow_logs.output))

        # Summer CEST UTC conversion + fall-back ambiguous DST offset preservation.
        dst_payload = {
            "days": [
                {
                    "date": "2026-07-15",
                    "slots": [
                        {
                            "startTime": "2026-07-15T08:00:00Z",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        }
                    ],
                },
                {
                    "date": "2026-10-25",
                    "slots": [
                        {
                            "startTime": "2026-10-25T02:30:00+02:00",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        },
                        {
                            "startTime": "2026-10-25T02:30:00+01:00",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                        },
                    ],
                },
            ]
        }
        parsed = parse_cmp_scheduler(dst_payload, default_specialty_id="spec-default")
        self.assertEqual(len(parsed.slots), 3)
        summer, fold0, fold1 = parsed.slots
        self.assertEqual(summer.start, datetime(2026, 7, 15, 10, 0, tzinfo=WARSAW_TZ))
        self.assertEqual(summer.specialty_id, "spec-default")
        self.assertEqual(fold0.start.astimezone(timezone.utc), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(fold1.start.astimezone(timezone.utc), datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))

        # Booked at 02:15+01:00 (01:15Z, second post-DST occurrence): 02:30+02:00 (00:30Z) is
        # earlier in absolute time and notifiable; 02:30+01:00 (01:30Z) is later and excluded.
        fold_criteria = WatchCriteria(
            watch_id="dst-fold-watch",
            provider="cmp",
            specialty_id="spec-default",
            date_from=date(2026, 10, 25),
            date_to=date(2026, 10, 25),
            booked_slot=BookedAppointment(
                start=parse_warsaw_datetime("2026-10-25T02:15:00+01:00", require_offset=True),
            ),
            only_better_than_booked=True,
        )
        fold_diff = diff_slots([], [fold0, fold1], fold_criteria)
        self.assertEqual(fold_diff.new_slots, (fold0, fold1))
        self.assertEqual(fold_diff.better_slots, (fold0,))
        self.assertEqual(fold_diff.notifiable_slots, (fold0,))

    def test_missing_fields_malformed_values_and_labels(self) -> None:
        payload = {
            "days": [
                {
                    "date": "not-a-date",
                    "slots": [
                        {"time": "09:00", "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                        {
                            "startTime": "2026-12-10T09:00:00",
                            "endTime": "09:30",
                            "doctorId": "doc-1",
                            "locationId": "loc-1",
                            "specialtyId": "spec-1",
                            "doctorName": "   ",
                            "locationName": "Bad\nControl",
                        },
                    ],
                },
                {
                    "date": "2026-02-30",  # invalid calendar date
                    "slots": [
                        {"time": "10:00", "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                    ],
                },
                {
                    "date": "2026-12-10",
                    "slots": [
                        {"startTime": "", "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                        {"startTime": 12345, "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                        {"startTime": "2026-12-10T10:00:00", "available": "yes", "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                        {"startTime": "2026-12-10T10:00:00", "telemedicine": 1, "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                        {"startTime": "2026-12-10T10:00:00", "endTime": "bad-end", "doctorId": "doc-1", "locationId": "loc-1", "specialtyId": "spec-1"},
                        {"startTime": "2026-12-10T10:00:00", "doctorId": -5, "locationId": "loc-1", "specialtyId": "spec-1"},
                        {"startTime": "2026-12-10T10:00:00", "doctorId": "doc-1", "locationId": "loc-1"},  # missing specialty
                    ],
                },
            ]
        }
        with self.assertLogs("hestia.appointments.parser", level="WARNING") as logs:
            result = parse_cmp_scheduler(payload)
        # Only the ISO-startTime slot in day 1 survives; its labels sanitize to None.
        self.assertEqual(len(result.slots), 1)
        self.assertEqual(result.skipped, 9)
        self.assertEqual(len(result.warnings), 9)
        self.assertEqual(len(logs.output), 9)
        self.assertEqual(result.slots[0].end, datetime(2026, 12, 10, 9, 30, tzinfo=WARSAW_TZ))
        self.assertIsNone(result.slots[0].doctor_label)
        self.assertIsNone(result.slots[0].location_label)

    def test_structural_errors_and_slot_limit(self) -> None:
        for invalid in (b"not-json", b'{"days":[],"days":[]}', b'{"days":[NaN]}', b"\xff", b"[]", b"{}", b'{"days":"bad"}'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CmpParseError):
                    parse_cmp_scheduler_bytes(invalid)

        over_limit = {
            "specialtyId": "spec-1",
            "days": [
                {
                    "date": "2026-12-10",
                    "slots": [
                        {"time": "09:00", "doctorId": "doc-1", "locationId": "loc-1"},
                        {"time": "09:30", "doctorId": "doc-1", "locationId": "loc-1"},
                    ],
                }
            ],
        }
        with self.assertRaises(CmpParseError):
            parse_cmp_scheduler(over_limit, max_slots=1)
