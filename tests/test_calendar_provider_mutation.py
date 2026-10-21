"""A failed read can clear a fresh write intent only before a write attempt."""
from __future__ import annotations

import unittest

from hestia.calendar_sync import events as codec
from hestia.calendar_sync import providers as p
from hestia.calendar_sync.types import ProviderError
from tests.test_calendar_providers import (
    PAYLOAD, RESOURCE, dav, dav_acl, dav_get, google, google_acl, google_event,
    google_page, response,
)


class ProviderMutationCertaintyTests(unittest.TestCase):
    def test_permission_failure_before_any_write_is_confirmed(self):
        for factory, identifier in ((google, "event"), (dav, RESOURCE)):
            for operation in ("create", "update", "delete"):
                provider, transport = factory(response(status=403))
                with self.subTest(provider=factory.__name__, operation=operation), self.assertRaises(ProviderError) as error:
                    if operation == "delete":
                        provider.delete(identifier, '"v1"')
                    else:
                        provider.put(identifier, PAYLOAD, '"v1"', create=operation == "create", uid="family-uid")
                self.assertTrue(error.exception.no_mutation)
                self.assertEqual(error.exception.status, 403)
                self.assertTrue(all(call[0] in {"GET", "PROPFIND"} for call in transport.calls))

    def test_current_read_and_revision_failures_before_write_are_confirmed(self):
        for factory, acl, current, identifier in (
            (google, google_acl, lambda: response(google_event()), "event"),
            (dav, dav_acl, dav_get, RESOURCE),
        ):
            for operation in ("update", "delete"):
                provider, transport = factory(acl(), response(status=403))
                with self.subTest(provider=factory.__name__, operation=operation), self.assertRaises(ProviderError) as error:
                    if operation == "delete":
                        provider.delete(identifier, None, uid="family-uid")
                    else:
                        provider.put(identifier, {**PAYLOAD, "summary": "Changed"}, '"v1"', create=False, uid="family-uid")
                self.assertTrue(error.exception.no_mutation)
                self.assertEqual(len(transport.calls), 2)
            provider, transport = factory(acl(), current())
            with self.assertRaises(ProviderError) as error:
                provider.put(identifier, {**PAYLOAD, "summary": "Changed"}, '"stale"', create=False, uid="family-uid")
            self.assertTrue(error.exception.no_mutation)
            self.assertTrue(error.exception.conflict)
            self.assertEqual(len(transport.calls), 2)

    def test_special_google_restriction_is_checked_before_patch_attempt(self):
        before = google_event(eventType="fromGmail")
        provider, transport = google(google_acl(), response(before))
        with self.assertRaises(ProviderError) as error:
            provider.put("event", {**PAYLOAD, "summary": "Changed"}, '"v1"', create=False, uid="family-uid")
        self.assertTrue(error.exception.no_mutation)
        self.assertEqual(error.exception.code, "google_event_type_restricted")
        self.assertEqual([call[0] for call in transport.calls], ["GET", "GET"])

    def test_google_post_and_dav_put_readback_failures_stay_uncertain(self):
        for factory, acl, identifier, statuses, final_error in (
            (google, google_acl, "event", (201,), response(status=403)),
            (google, google_acl, "event", (409,), response(status=403)),
            (dav, dav_acl, RESOURCE, (201,), response(status=404)),
            (dav, dav_acl, RESOURCE, (412,), response(status=403)),
            # A nested reader's certainty cannot override this call's earlier write.
            (google, google_acl, "event", (201,), ProviderError("Read denied", status=403, no_mutation=True)),
        ):
            provider, transport = factory(acl(), *(response(status=status) for status in statuses), final_error)
            with self.subTest(provider=factory.__name__, statuses=statuses), self.assertRaises(ProviderError) as error:
                provider.put(identifier, PAYLOAD, None, create=True, uid="family-uid")
            self.assertFalse(error.exception.no_mutation)
            self.assertEqual([call[0] for call in transport.calls][1:],
                             ["POST", "GET"] if factory is google else ["PUT", "GET"])

    def test_failed_calendar_mutation_request_is_never_marked_preflight(self):
        for factory, acl, identifier in ((google, google_acl, "event"), (dav, dav_acl, RESOURCE)):
            for operation in ("create", "delete"):
                provider, transport = factory(acl(), response(status=403))
                with self.subTest(provider=factory.__name__, operation=operation), self.assertRaises(ProviderError) as error:
                    if operation == "delete":
                        provider.delete(identifier, '"v1"')
                    else:
                        provider.put(identifier, PAYLOAD, None, create=True, uid="family-uid")
                self.assertFalse(error.exception.no_mutation)
                self.assertEqual(transport.calls[-1][0], "DELETE" if operation == "delete" else "POST" if factory is google else "PUT")

    def test_partial_google_exception_bundle_preserves_uncertainty(self):
        series = codec.validate_payload({**PAYLOAD, "recurrence": ["RRULE:FREQ=DAILY;COUNT=4"]})
        master = google_event(payload=series)
        originals = [{"date": "2026-11-02"}, {"date": "2026-11-03"}]
        overrides = [{"originalStartTime": original, **codec.default_instance(series, original), "summary": f"Override {index}"}
                     for index, original in enumerate(originals)]
        wanted = codec.validate_payload({**series, "exceptions": overrides})
        first = google_event("instance", payload=codec.default_instance(series, originals[0]),
                             originalStartTime=originals[0], recurringEventId="event")
        for partially_written in (False, True):
            replies = [google_acl(), response(master), google_page([master])]
            if partially_written:
                replies += [response({"items": [first]}), response({**first, **overrides[0]})]
            provider, transport = google(*replies, response(status=403))
            with self.subTest(partially_written=partially_written), self.assertRaises(ProviderError) as error:
                provider.put("event", wanted, p._google_etag(master, []), create=False, uid="family-uid")
            self.assertEqual(error.exception.no_mutation, not partially_written)
            self.assertEqual(sum(call[0] == "PATCH" for call in transport.calls), int(partially_written))
