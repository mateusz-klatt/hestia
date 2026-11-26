"""Unit tests for the pure, DB-free appointment slot-diff engine."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from unittest import TestCase

from hestia.appointments import (
    WARSAW_TZ,
    AppointmentSlot,
    BookedAppointment,
    WatchCriteria,
    dedupe_and_sort,
    diff_slots,
    is_better_than_booked,
    matches_criteria,
    slot_digest,
)


def _slot(
    start: datetime,
    *,
    doctor_id: str = "doc-101",
    location_id: str = "loc-01",
    specialty_id: str = "spec-01",
    slot_id: str = "slot-1",
    provider: str = "cmp",
) -> AppointmentSlot:
    return AppointmentSlot(
        provider=provider,
        start=start,
        doctor_id=doctor_id,
        location_id=location_id,
        specialty_id=specialty_id,
        slot_id=slot_id,
    )


class SlotDiffTests(TestCase):
    def setUp(self) -> None:
        self.criteria = WatchCriteria(
            watch_id="watch-1",
            provider="cmp",
            specialty_id="spec-01",
            doctor_ids=("doc-101", "doc-102"),
            location_ids=("loc-01",),
            date_from=date(2026, 12, 10),
            date_to=date(2026, 12, 20),
            time_from=time(8, 0),
            time_to=time(17, 0),
        )

    def test_new_and_removed_slots(self) -> None:
        kept = _slot(datetime(2026, 12, 12, 10, 0, tzinfo=WARSAW_TZ), slot_id="s-kept")
        removed = _slot(datetime(2026, 12, 11, 9, 0, tzinfo=WARSAW_TZ), slot_id="s-removed")
        added = _slot(datetime(2026, 12, 13, 11, 30, tzinfo=WARSAW_TZ), slot_id="s-added")

        diff = diff_slots([kept, removed], [added, kept], self.criteria)
        self.assertEqual(diff.watch_id, "watch-1")
        self.assertEqual(diff.active_slots, (kept, added))
        self.assertEqual(diff.new_slots, (added,))
        self.assertEqual(diff.removed_slots, (removed,))
        self.assertEqual(diff.better_slots, ())
        self.assertEqual(diff.notifiable_slots, (added,))

    def test_better_than_booked_rules_and_only_better_filter(self) -> None:
        booked = BookedAppointment(
            start=datetime(2026, 12, 15, 14, 0, tzinfo=WARSAW_TZ),
            doctor_id="doc-101",
            location_id="loc-01",
            min_improvement_minutes=60,
        )
        criteria_only_better = WatchCriteria(
            watch_id="watch-better",
            provider="cmp",
            specialty_id="spec-01",
            date_from=date(2026, 12, 10),
            date_to=date(2026, 12, 20),
            booked_slot=booked,
            only_better_than_booked=True,
        )
        much_earlier = _slot(datetime(2026, 12, 15, 13, 0, tzinfo=WARSAW_TZ), slot_id="s-exact-60m")
        barely_earlier = _slot(datetime(2026, 12, 15, 13, 30, tzinfo=WARSAW_TZ), slot_id="s-30m")
        later = _slot(datetime(2026, 12, 16, 9, 0, tzinfo=WARSAW_TZ), slot_id="s-later")
        already_seen_earlier = _slot(datetime(2026, 12, 12, 9, 0, tzinfo=WARSAW_TZ), slot_id="s-old")

        diff = diff_slots(
            [already_seen_earlier],
            [already_seen_earlier, later, barely_earlier, much_earlier],
            criteria_only_better,
        )
        self.assertEqual(diff.new_slots, (much_earlier, barely_earlier, later))
        self.assertEqual(diff.better_slots, (much_earlier,))
        self.assertEqual(diff.notifiable_slots, (much_earlier,))

        # When only_better_than_booked is False, all new slots remain notifiable while better_slots highlights winners.
        criteria_all = WatchCriteria(
            watch_id="watch-all",
            provider="cmp",
            specialty_id="spec-01",
            date_from=date(2026, 12, 10),
            date_to=date(2026, 12, 20),
            booked_slot=BookedAppointment(start=datetime(2026, 12, 15, 14, 0, tzinfo=WARSAW_TZ)),
            only_better_than_booked=False,
        )
        diff_all = diff_slots([], [later, barely_earlier], criteria_all)
        self.assertEqual(diff_all.better_slots, (barely_earlier,))
        self.assertEqual(diff_all.notifiable_slots, (barely_earlier, later))

        # Same-doctor and same-location constraints on BookedAppointment.
        strict_booked = BookedAppointment(
            start=datetime(2026, 12, 15, 14, 0, tzinfo=WARSAW_TZ),
            doctor_id="doc-101",
            location_id="loc-01",
            require_same_doctor=True,
            require_same_location=True,
        )
        self.assertTrue(is_better_than_booked(barely_earlier, strict_booked))
        self.assertFalse(is_better_than_booked(
            _slot(datetime(2026, 12, 15, 10, 0, tzinfo=WARSAW_TZ), doctor_id="doc-102"),
            strict_booked,
        ))
        self.assertFalse(is_better_than_booked(
            _slot(datetime(2026, 12, 15, 10, 0, tzinfo=WARSAW_TZ), location_id="loc-02"),
            strict_booked,
        ))
        # Exact same time is never strictly better.
        self.assertFalse(is_better_than_booked(
            _slot(datetime(2026, 12, 15, 14, 0, tzinfo=WARSAW_TZ)),
            strict_booked,
        ))

    def test_out_of_window_and_filter_mismatches(self) -> None:
        in_window = _slot(datetime(2026, 12, 10, 8, 0, tzinfo=WARSAW_TZ))
        wrong_provider = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ), provider="other")
        wrong_specialty = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ), specialty_id="spec-99")
        wrong_doctor = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ), doctor_id="doc-999")
        wrong_location = _slot(datetime(2026, 12, 10, 9, 0, tzinfo=WARSAW_TZ), location_id="loc-99")
        too_early_date = _slot(datetime(2026, 12, 9, 12, 0, tzinfo=WARSAW_TZ))
        too_late_date = _slot(datetime(2026, 12, 21, 12, 0, tzinfo=WARSAW_TZ))
        too_early_time = _slot(datetime(2026, 12, 10, 7, 59, tzinfo=WARSAW_TZ))
        too_late_time = _slot(datetime(2026, 12, 10, 17, 1, tzinfo=WARSAW_TZ))

        self.assertTrue(matches_criteria(in_window, self.criteria))
        for candidate in (
            wrong_provider,
            wrong_specialty,
            wrong_doctor,
            wrong_location,
            too_early_date,
            too_late_date,
            too_early_time,
            too_late_time,
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(matches_criteria(candidate, self.criteria))

        # Doctor-only watch with no specialty, location, or time filters accepts any matching doctor in date window.
        broad = WatchCriteria(
            watch_id="broad",
            provider="cmp",
            doctor_ids=("doc-101",),
            date_from=date(2026, 12, 10),
            date_to=date(2026, 12, 10),
        )
        self.assertTrue(matches_criteria(too_early_time, broad))

        diff = diff_slots([too_early_date], [in_window, too_late_date, wrong_doctor], self.criteria)
        self.assertEqual(diff.active_slots, (in_window,))
        self.assertEqual(diff.new_slots, (in_window,))
        self.assertEqual(diff.removed_slots, ())

    def test_duplicates_stability_and_digests(self) -> None:
        t1_warsaw = datetime(2026, 12, 12, 10, 0, tzinfo=WARSAW_TZ)
        t1_utc = datetime(2026, 12, 12, 9, 0, tzinfo=timezone.utc)
        t2_warsaw = datetime(2026, 12, 12, 11, 0, tzinfo=WARSAW_TZ)

        dup_a = _slot(t1_warsaw, slot_id="slot-a")
        dup_b = _slot(t1_utc, slot_id="slot-b")
        other = _slot(t2_warsaw, slot_id="slot-c")

        # Input order must never change deduped canonical output.
        first_order = dedupe_and_sort([other, dup_b, dup_a])
        second_order = dedupe_and_sort([dup_a, other, dup_b])
        self.assertEqual(first_order, second_order)
        self.assertEqual(first_order, (dup_a, other))

        # Equivalent UTC/Warsaw representations match across previous and current snapshots.
        diff = diff_slots([dup_b], [dup_a, dup_b, other], self.criteria)
        self.assertEqual(diff.new_slots, (other,))
        self.assertEqual(diff.removed_slots, ())

        # Unkeyed and keyed digests are stable and equal across equivalent timezone representations.
        self.assertEqual(slot_digest("watch-1", dup_a), slot_digest("watch-1", dup_b))
        self.assertNotEqual(slot_digest("watch-1", dup_a), slot_digest("watch-2", dup_a))
        key = b"synthetic-32-byte-hmac-key-01234"
        self.assertEqual(
            slot_digest("watch-1", dup_a, hmac_key=key),
            slot_digest("watch-1", dup_b, hmac_key=key),
        )
        self.assertNotEqual(
            slot_digest("watch-1", dup_a),
            slot_digest("watch-1", dup_a, hmac_key=key),
        )
