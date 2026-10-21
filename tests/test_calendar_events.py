"""Calendar content contracts: provider roundtrips must not change event meaning."""
from copy import deepcopy
from unittest.mock import patch

import unittest

from hestia.calendar_sync.events import (
    EventValidationError, default_instance, fingerprint, from_google_bundle,
    from_google_event, from_ical, to_google_event, to_google_exception,
    to_ical, validate_payload, original_start_key,
)


def timed():
    return {"summary": "Rodzina 👪", "description": "First\nSecond", "location": "Dom",
            "start": {"dateTime": "2026-03-22T09:00:00+01:00", "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": "2026-03-22T10:00:00+01:00", "timeZone": "Europe/Warsaw"}}


def all_day():
    return {"summary": "Holiday", "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-26"}}


def ics(lines):
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + lines.replace("\n", "\r\n") + "\r\nEND:VCALENDAR\r\n"


def event(lines="", uid="test"):
    return "BEGIN:VEVENT\nUID:" + uid + "\n" + lines + "\nEND:VEVENT"



def _cases(name, values):
    def decorate(test):
        def run(self):
            for value in values:
                with self.subTest(**{name: value}):
                    test(self, value)
        return run
    return decorate


class CalendarEventTests(unittest.TestCase):
    def test_timed_ical_google_roundtrip_preserves_timezone_and_core_content(self):
        source = timed()
        source.update(status="tentative", transparency="transparent", visibility="private")
        uid, restored = from_ical(to_ical(source, "stable@hestia"))
        assert uid == "stable@hestia"
        assert fingerprint(source) == fingerprint(restored)
        google = to_google_event(restored, uid)
        google.update(id="remote", updated="2026-10-21T09:00:00Z", etag='"42"', sequence=4,
                      attendees=[{"email": "nobody@example.test"}], reminders={"useDefault": True})
        assert fingerprint(from_google_event(google)) == fingerprint(source)
        assert "attendees" not in to_google_event(from_google_event(google))


    def test_all_day_exclusive_end_and_recurrence_roundtrip(self):
        source = all_day()
        source["recurrence"] = ["RRULE:FREQ=YEARLY;COUNT=3", "RDATE;VALUE=DATE:20271120,20271119", "EXDATE;VALUE=DATE:20281024"]
        uid, restored = from_ical(to_ical(source, "all-day"))
        assert uid == "all-day"
        assert fingerprint(restored) == fingerprint(source)
        assert restored["end"] == {"date": "2026-10-26"}


    def test_dst_series_and_moved_cancelled_exceptions_survive_roundtrip(self):
        source = timed()
        source["recurrence"] = ["RRULE:COUNT=5;FREQ=WEEKLY;BYDAY=SU", "RDATE;TZID=Europe/Warsaw:20260406T090000", "EXDATE;TZID=Europe/Warsaw:20260412T090000"]
        source["exceptions"] = [
            {"originalStartTime": {"dateTime": "2026-03-29T07:00:00Z"}, "summary": "Moved",
             "start": {"dateTime": "2026-03-29T11:00:00+02:00", "timeZone": "Europe/Warsaw"},
             "end": {"dateTime": "2026-03-29T12:00:00+02:00", "timeZone": "Europe/Warsaw"}},
            {"originalStartTime": {"dateTime": "2026-04-05T07:00:00Z"}, "status": "cancelled"},
        ]
        encoded = to_ical(source, "series")
        assert b"BEGIN:VTIMEZONE" in encoded
        assert b"RECURRENCE-ID;TZID=Europe/Warsaw:20260329T090000" in encoded
        assert fingerprint(from_ical(encoded)[1]) == fingerprint(source)
        master = to_google_event(source)
        exceptions = [to_google_exception(item) for item in validate_payload(source)["exceptions"]]
        assert "exceptions" not in master
        assert fingerprint(from_google_bundle(master, exceptions)) == fingerprint(source)


    def test_defaults_newlines_recurrence_order_and_server_metadata_do_not_churn(self):
        first = timed()
        first["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=WE,MO;COUNT=5", "EXDATE:20260330T070000Z,20260401T070000Z"]
        second = deepcopy(first)
        second.update(status="confirmed", transparency="opaque", visibility="default")
        second["description"] = "First\r\nSecond"
        second["recurrence"] = ["EXDATE:20260401T070000Z", "RRULE:COUNT=5;BYDAY=MO,WE;FREQ=WEEKLY", "EXDATE;TZID=Europe/Warsaw:20260330T090000"]
        assert fingerprint(first) == fingerprint(second)
        assert first["description"] == "First\nSecond"


    def test_default_instance_reset_and_redundant_override_dedup_across_dst(self):
        master = validate_payload({**timed(), "recurrence": ["RRULE:FREQ=WEEKLY"]})
        original = {"dateTime": "2026-03-29T07:00:00Z"}
        reset = default_instance(master, original)
        assert reset["start"]["dateTime"] == "2026-03-29T09:00:00+02:00"
        assert reset["end"]["dateTime"] == "2026-03-29T10:00:00+02:00"
        normalized = from_google_bundle(to_google_event(master), [{**reset, "originalStartTime": original}])
        assert normalized == master
        days = validate_payload(all_day())
        assert default_instance(days, {"date": "2027-10-24"})["end"] == {"date": "2027-10-26"}


    def test_instance_identity_uses_instant_and_elapsed_duration_across_clock_change(self):
        assert original_start_key({"dateTime": "2026-03-29T09:00:00+02:00", "timeZone": "Europe/Warsaw"}) == original_start_key({"dateTime": "2026-03-29T07:00:00Z"})
        source = timed()
        source["start"]["dateTime"] = "2026-03-29T01:30:00+01:00"
        source["end"]["dateTime"] = "2026-03-29T03:30:00+02:00"
        normalized = validate_payload(source)
        assert default_instance(normalized, {"dateTime": "2026-04-05T01:30:00+02:00"})["end"]["dateTime"] == "2026-04-05T02:30:00+02:00"
        source["start"]["dateTime"] = "2026-10-25T02:30:00+02:00"
        source["end"]["dateTime"] = "2026-10-25T02:00:00+01:00"
        assert validate_payload(source)["end"]["dateTime"] == "2026-10-25T02:00:00+01:00"


    def test_parser_failure_is_translated_into_content_validation_error(self):
        with patch("hestia.calendar_sync.events.Calendar.from_ical", side_effect=ValueError("malformed input")):
            with self.assertRaises(EventValidationError):
                validate_payload({**timed(), "recurrence": ["RRULE:FREQ=DAILY"]})


    def test_offset_only_normalizes_to_utc_and_explicit_naive_zone_is_supported(self):
        source = timed()
        source["start"] = {"dateTime": "2026-03-22T09:00:00+01:00"}
        source["end"] = {"dateTime": "2026-03-22T10:00:00+01:00"}
        result = validate_payload(source)
        assert result["start"] == {"dateTime": "2026-03-22T08:00:00+00:00", "timeZone": "UTC"}
        assert fingerprint(from_ical(to_ical(source, "utc"))[1]) == fingerprint(source)
        source = timed()
        source["start"]["dateTime"] = "2026-03-22T09:00:00"
        assert validate_payload(source)["start"]["dateTime"] == "2026-03-22T09:00:00+01:00"


    @_cases("value", [None, [], {}, {"date": "2026-01-01", "dateTime": "2026-01-01T00:00:00Z"},
        {"date": "20260101"}, {"date": "2026-02-30"}, {"dateTime": 42}, {"dateTime": "2026-01-01"},
        {"dateTime": "brokenTtime"}, {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "Custom/Unknown"},
        {"dateTime": "2026-01-01T09:00:00.123Z"}, {"dateTime": "2026-01-01T09:00:00"},
        {"dateTime": "2026-03-29T02:30:00", "timeZone": "Europe/Warsaw"},
        {"dateTime": "2026-10-25T02:30:00", "timeZone": "Europe/Warsaw"}, {"date": "2026-01-01", "unknown": True}])
    def test_invalid_event_times_are_rejected(self, value):
        with self.assertRaises(EventValidationError):
            validate_payload({**timed(), "start": value})


    @_cases("patch", [None, [], {"unknown": 1}, {"summary": 3}, {"end": {"date": "2026-10-25"}},
        {"end": {"dateTime": "2026-03-22T07:00:00Z"}}, {"status": "invalid"}, {"recurrence": "RRULE:FREQ=DAILY"},
        {"recurrence": [3]}, {"recurrence": ["RRULE\n:FREQ=DAILY"]}, {"recurrence": ["RRULE"]},
        {"recurrence": ["EXRULE:FREQ=DAILY"]}, {"recurrence": ["RRULE:FREQ=INVALID"]}, {"recurrence": ["RRULE:FREQ=DAILY;FOO=BAR"]},
        {"recurrence": ["RRULE:FREQ=DAILY;COUNT=3;UNTIL=20261201T000000Z"]},
        {"recurrence": ["RRULE:FREQ=DAILY;COUNT=0"]}, {"recurrence": ["RRULE:FREQ=DAILY;INTERVAL=-1"]},
        {"recurrence": ["RRULE:FREQ=DAILY;UNTIL=20261201"]}, {"recurrence": ["RRULE:FREQ=DAILY;UNTIL=20261201T000000"]},
        {"recurrence": ["RDATE;VALUE=PERIOD:20261001T090000Z/PT1H"]}, {"recurrence": ["EXDATE;VALUE=DATE:20261001"]},
        {"recurrence": ["EXDATE:20261001T090000"]}, {"recurrence": ["RRULE:garbage"]},
        {"exceptions": {}}, {"exceptions": [{"status": "cancelled"}]}])
    def test_invalid_content_is_rejected(self, patch):
        value = {**timed(), **patch} if isinstance(patch, dict) else patch
        with self.assertRaises(EventValidationError):
            validate_payload(value)


    @_cases("exception", [
        {"originalStartTime": {"date": "2026-03-29"}, "status": "cancelled"},
        {"originalStartTime": {"dateTime": "2026-03-29T07:00:00Z"}, "recurrence": []},
    ])
    def test_invalid_series_exceptions_are_rejected(self, exception):
        with self.assertRaises(EventValidationError):
            validate_payload({**timed(), "recurrence": ["RRULE:FREQ=WEEKLY"], "exceptions": [exception]})


    def test_duplicate_exceptions_rejected_even_if_the_first_is_redundant(self):
        master = validate_payload({**timed(), "recurrence": ["RRULE:FREQ=WEEKLY"]})
        key = {"dateTime": "2026-03-29T07:00:00Z"}
        override = {"originalStartTime": key, **default_instance(master, key)}
        with self.assertRaisesRegex(EventValidationError, "Duplicate"):
            validate_payload({**master, "exceptions": [override, override]})


    @_cases("patch", [{"eventType": "focusTime"}, {"eventType": "outOfOffice"}, {"endTimeUnspecified": "true"}])
    def test_google_special_event_semantics_are_rejected(self, patch):
        with self.assertRaises(EventValidationError):
            from_google_event({**timed(), **patch})


    def test_ical_duration_missing_all_day_end_and_utc_until(self):
        uid, value = from_ical(ics(event("DTSTART:20261021T090000Z\nDURATION:PT1H\nRRULE:FREQ=DAILY;UNTIL=20261101T090000Z")))
        assert uid == "test"
        assert value["end"]["dateTime"] == "2026-10-21T10:00:00+00:00"
        _, value = from_ical(ics(event("DTSTART;VALUE=DATE:20261021\nRRULE:FREQ=YEARLY;UNTIL=20301021")))
        assert value["end"] == {"date": "2026-10-22"}


    @_cases("resource", ["garbage", ics(""),
        ics(event("DTSTART:20261021T090000Z\nDTEND:20261021T100000Z", uid="")),
        ics(event("DTSTART:20261021T090000Z\nDTEND:20261021T100000Z") + "\n" + event("RECURRENCE-ID:20261022T090000Z\nSTATUS:CANCELLED", uid="different")),
        ics(event("SUMMARY:No start")),
        ics(event("DTSTART:20261021T090000Z\nDTEND:20261021T100000Z\nDURATION:PT1H")),
        ics(event("DTSTART:20261021T090000Z\nDTEND:20261021T100000Z\nRRULE:FREQ=DAILY") + "\n" + event("RECURRENCE-ID;RANGE=THISANDFUTURE:20261022T090000Z\nDTSTART:20261022T110000Z\nDTEND:20261022T120000Z")),
        ics(event("DTSTART:not-a-date\nDTEND:20261021T100000Z")),
        ics(event("DTSTART:20261021T090000Z\nDURATION:P1D\nRRULE:FREQ=DAILY")),
        ics(event("DTSTART:20261021T090000Z\nDTSTART:20261021T100000Z\nDTEND:20261021T110000Z")),
    ])
    def test_unsafe_or_malformed_ical_is_rejected(self, resource):
        with self.assertRaises(EventValidationError):
            from_ical(resource)


    @_cases("uid", [None, "", "bad\nuid", "bad\ruid"])
    def test_invalid_uid_is_rejected(self, uid):
        with self.assertRaises(EventValidationError):
            to_ical(timed(), uid)

    def test_timed_point_events_omit_dtend_and_roundtrip_without_inventing_duration(self):
        for zone in ("UTC", "Europe/Rome"):
            source = timed()
            source["start"] = {"dateTime": "2026-03-22T09:00:00Z", "timeZone": zone}
            source["end"] = deepcopy(source["start"])
            source["recurrence"] = ["RRULE:FREQ=WEEKLY;COUNT=3"]
            encoded = to_ical(source, "point-event")
            self.assertNotIn(b"DTEND", encoded)
            self.assertEqual(fingerprint(source), fingerprint(from_ical(encoded)[1]))
        _, implicit = from_ical(ics(event("DTSTART:20261021T090000Z")))
        self.assertEqual(implicit["start"], implicit["end"])
        with self.assertRaises(EventValidationError):
            validate_payload({"start": {"date": "2026-10-21"}, "end": {"date": "2026-10-21"}})

    def test_fixed_offset_legacy_zones_preserve_instant_and_normalize_to_utc(self):
        for zone, stamp, expected in (("GMT+01:00", "2026-01-02T09:00:00+01:00", "2026-01-02T08:00:00+00:00"),
                                      ("GMT+02:00", "2026-01-02T09:00:00", "2026-01-02T07:00:00+00:00"),
                                      ("GMT-08:00", "2026-01-02T09:00:00-08:00", "2026-01-02T17:00:00+00:00"),
                                      ("GMT+01:00", "2026-01-02T08:00:00Z", "2026-01-02T08:00:00+00:00")):
            source = timed()
            source["start"] = {"dateTime": stamp, "timeZone": zone}
            source["end"] = deepcopy(source["start"])
            normalized = from_google_event(source)
            self.assertEqual(normalized["start"], {"dateTime": expected, "timeZone": "UTC"})
            self.assertEqual(fingerprint(normalized), fingerprint(from_ical(to_ical(normalized, "legacy"))[1]))
        for zone in ("GMT+01:99", "GMT+24:00", "GMT+1:00"):
            with self.assertRaises(EventValidationError):
                validate_payload({**timed(), "start": {"dateTime": "2026-01-02T09:00:00", "timeZone": zone}})
        source = {**timed(), "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"],
                  "start": {"dateTime": "2026-01-02T09:00:00+01:00", "timeZone": "GMT+01:00"}}
        with self.assertRaisesRegex(EventValidationError, "recurring fixed-offset"):
            validate_payload(source)
        with self.assertRaisesRegex(EventValidationError, "recurring fixed-offset"):
            from_ical(ics(event('DTSTART;TZID="GMT+01:00":20260102T090000\nDTEND:20260102T090000Z\nRRULE:FREQ=WEEKLY;BYDAY=MO')))

    def test_from_gmail_and_birthday_copy_only_portable_content(self):
        for kind, source in (("fromGmail", timed()), ("fromGmail", all_day()),
                             ("birthday", {**all_day(), "recurrence": ["RRULE:FREQ=YEARLY"]})):
            imported = from_google_event({**source, "eventType": kind,
                "source": {"url": "https://mail.example.test/private"},
                "birthdayProperties": {"contact": "people/private", "type": "birthday"}})
            self.assertEqual(imported, validate_payload(source))
            self.assertNotIn("eventType", to_google_event(imported))
            self.assertNotIn("birthdayProperties", imported)
            self.assertEqual(fingerprint(imported), fingerprint(from_ical(to_ical(imported, "portable"))[1]))
        self.assertEqual(from_google_event({**timed(), "originalStartTime": {"dateTime": "2026-03-22T08:00:00Z"}}), validate_payload(timed()))

    def test_unspecified_end_roundtrips_with_marker_and_without_a_made_up_duration(self):
        for source in (timed(), all_day()):
            source.update(eventType="fromGmail", endTimeUnspecified=True)
            imported = from_google_event(source)
            self.assertTrue(imported["endTimeUnspecified"])
            if "dateTime" in imported["start"]:
                self.assertEqual(imported["end"], imported["start"])
            else:
                self.assertEqual(imported["end"], {"date": "2026-10-25"})
            encoded = to_ical(imported, "unknown")
            self.assertNotIn(b"DTEND", encoded)
            self.assertIn(b"X-HESTIA-END-UNSPECIFIED:TRUE", encoded)
            self.assertEqual(imported, from_ical(encoded)[1])
            exported = to_google_event(imported)
            self.assertNotIn("endTimeUnspecified", exported)
            copied = {**exported, "extendedProperties": {"private": {"hestia_end_unspecified": "true"}}}
            self.assertEqual(imported, from_google_event(copied))
            self.assertNotIn("endTimeUnspecified", to_google_exception(imported))
        with self.assertRaises(EventValidationError):
            validate_payload({**timed(), "endTimeUnspecified": 1})

    def test_google_marker_can_override_native_flag_and_concrete_device_end_clears_marker(self):
        source = {**timed(), "endTimeUnspecified": True,
                  "extendedProperties": {"private": {"hestia_end_unspecified": "false"}}}
        self.assertEqual(from_google_event(source), validate_payload(timed()))
        source["extendedProperties"]["private"]["hestia_end_unspecified"] = "true"
        self.assertTrue(from_google_event(source)["endTimeUnspecified"])
        source["endTimeUnspecified"] = False
        self.assertEqual(from_google_event(source), validate_payload(timed()))
        source["extendedProperties"]["private"]["hestia_end_unspecified"] = "unrecognized"
        self.assertEqual(from_google_event(source), validate_payload(timed()))
        for explicit in ("DTEND:20261021T100000Z", "DURATION:PT1H"):
            _, parsed = from_ical(ics(event("DTSTART:20261021T090000Z\nX-HESTIA-END-UNSPECIFIED:TRUE\n" + explicit)))
            self.assertNotIn("endTimeUnspecified", parsed)

    def test_unknown_end_series_keeps_known_and_unknown_exception_semantics(self):
        source = {**timed(), "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=5"],
                  "endTimeUnspecified": True, "status": "tentative", "exceptions": [
            {**timed(), "originalStartTime": {"dateTime": "2026-03-29T07:00:00Z"},
             "endTimeUnspecified": False, "status": "confirmed", "summary": "Known duration override"},
            {"originalStartTime": {"dateTime": "2026-04-05T07:00:00Z"}, "status": "cancelled"}]}
        normalized = validate_payload(source)
        self.assertFalse(normalized["exceptions"][0]["endTimeUnspecified"])
        self.assertEqual(normalized["exceptions"][0]["status"], "confirmed")
        self.assertEqual(normalized, validate_payload(normalized))
        self.assertEqual(normalized, from_ical(to_ical(normalized, "unknown-series"))[1])
        master = to_google_event(normalized)
        master["endTimeUnspecified"] = True
        override = deepcopy(normalized["exceptions"][0])
        override.pop("endTimeUnspecified")
        cancelled = {**normalized["exceptions"][1], "extendedProperties": {"private": {"hestia_end_unspecified": "true"}}}
        self.assertEqual(normalized, from_google_bundle(master, [override, cancelled]))
