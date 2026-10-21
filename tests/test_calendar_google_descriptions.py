"""Protect literal legacy Meet separators without decoding arbitrary Google HTML."""
from __future__ import annotations

import hashlib
import json
import unittest

from hestia.calendar_sync import events as codec
from hestia.calendar_sync import providers
from tests import test_calendar_providers as fixtures


SOURCE = 'Zażółć <b>"notes" & details</b> &amp;\n-::~:~ begin\nmeet.google.com/synthetic\n-::~:~ end'
ENCODED = 'Zażółć &lt;b&gt;"notes" &amp; details&lt;/b&gt; &amp;amp;\n&#45;::~:~ begin\nmeet.google.com/synthetic\n&#45;::~:~ end'
MARKER = 'html-v1:' + hashlib.sha256(ENCODED.encode('utf-8')).hexdigest()
KEY = 'hestia_description_encoding'


def remote_description(text, marker=None, *, etag='"v1"', **extra):
    private = {"hestia_uid": "uid", "hestia_end_unspecified": "false", "unrelated": "keep"}
    if marker is not None:
        private[KEY] = marker
    return fixtures.google_event(payload={**fixtures.PAYLOAD, "description": text}, etag=etag,
                                 extendedProperties={"private": private}, **extra)


class DescriptionCodecTests(unittest.TestCase):
    def test_literal_sentinel_description_roundtrip_preserves_html_entities_and_unicode(self):
        self.assertEqual(codec.encode_google_description(SOURCE), (ENCODED, MARKER))
        self.assertNotIn('-::~:~', ENCODED)
        self.assertEqual(codec.decode_google_description(ENCODED, MARKER), SOURCE)
        normalized = codec.from_google_event(remote_description(ENCODED, MARKER))
        self.assertEqual(normalized['description'], SOURCE)

    def test_regular_text_and_html_are_not_encoded_or_unescaped(self):
        for description in ('Plain notes', '<b>Existing HTML</b> &amp; &#45;::~:~', ''):
            with self.subTest(description=description):
                self.assertEqual(codec.encode_google_description(description), (description, None))
                self.assertEqual(codec.from_google_event(remote_description(description))['description'], description)
                self.assertEqual(codec.decode_google_description(description, None), description)

    def test_bad_or_stale_markers_never_rewrite_user_edited_description(self):
        for marker in ('', 'html-v1:wrong', MARKER.replace('html-v1:', 'html-v2:'), None, 123, {'invalid': True}):
            with self.subTest(marker=marker):
                self.assertEqual(codec.decode_google_description(ENCODED, marker), ENCODED)
                self.assertEqual(codec.from_google_event(remote_description(ENCODED, marker))['description'], ENCODED)
        edited = ENCODED + '\nUser added <b>new text</b> &amp;'
        self.assertEqual(codec.from_google_event(remote_description(edited, MARKER))['description'], edited)
        self.assertEqual(codec.decode_google_description(12, MARKER), 12)
        with self.assertRaises(codec.EventValidationError):
            codec.from_google_event(remote_description(12, MARKER))


class DescriptionProviderTests(unittest.TestCase):
    def test_create_protects_literal_description_and_keeps_other_identity_metadata(self):
        payload = codec.validate_payload({**fixtures.PAYLOAD, 'description': SOURCE})
        remote = remote_description(ENCODED, MARKER)
        provider, transport = fixtures.google(fixtures.google_acl(), fixtures.response(remote), fixtures.response(remote))
        result = provider.put('event', payload, None, create=True, uid='uid')
        self.assertEqual(result.payload, payload)
        body = json.loads(transport.calls[1][2]['body'])
        self.assertEqual(body['description'], ENCODED)
        self.assertEqual(body['extendedProperties']['private'][KEY], MARKER)
        self.assertEqual(body['extendedProperties']['private']['hestia_uid'], 'uid')
        self.assertEqual(body['extendedProperties']['private']['hestia_end_unspecified'], 'false')
        self.assertEqual(transport.calls[1][0], 'POST')

    def test_fresh_normal_description_has_no_empty_encoding_marker(self):
        for description in ('Plain notes', '<b>Existing HTML</b> &amp;'):
            payload = codec.validate_payload({**fixtures.PAYLOAD, 'description': description})
            remote = remote_description(description)
            provider, transport = fixtures.google(fixtures.google_acl(), fixtures.response(remote), fixtures.response(remote))
            self.assertEqual(provider.put('event', payload, None, create=True, uid='uid').payload, payload)
            body = json.loads(transport.calls[1][2]['body'])
            self.assertEqual(body['description'], description)
            self.assertNotIn(KEY, body['extendedProperties']['private'])

    def test_update_to_normal_text_explicitly_clears_old_marker_and_preserves_private_fields(self):
        before = remote_description(ENCODED, MARKER)
        description = '<b>New HTML</b> &amp; remains literal'
        after = remote_description(description, '', etag='"v2"')
        payload = codec.validate_payload({**fixtures.PAYLOAD, 'description': description})
        provider, transport = fixtures.google(fixtures.google_acl(), fixtures.response(before), fixtures.response(after), fixtures.response(after))
        self.assertEqual(provider.put('event', payload, '"v1"', create=False, uid='uid').payload, payload)
        body = json.loads(transport.calls[2][2]['body'])
        self.assertEqual(body['description'], description)
        self.assertEqual(body['extendedProperties']['private'][KEY], '')
        self.assertEqual(body['extendedProperties']['private']['unrelated'], 'keep')
        self.assertEqual(body['extendedProperties']['private']['hestia_uid'], 'uid')
        self.assertEqual(transport.calls[2][2]['headers']['If-Match'], '"v1"')

    def test_recurring_override_encoding_and_marker_clear_roundtrip(self):
        base = codec.validate_payload({**fixtures.PAYLOAD, 'description': 'Parent notes', 'recurrence': ['RRULE:FREQ=DAILY;COUNT=3']})
        original = {'date': '2026-11-02'}
        default = codec.default_instance(base, original)
        override = {'originalStartTime': original, **default, 'description': SOURCE}
        wanted = codec.validate_payload({**base, 'exceptions': [override]})
        master = fixtures.google_event(payload=base, extendedProperties={'private': {'hestia_uid': 'uid'}})
        instance = fixtures.google_event('instance', default, originalStartTime=original, recurringEventId='event',
                                         extendedProperties={'private': {'unrelated': 'keep', 'hestia_uid': 'uid'}})
        encoded = {**instance, 'description': ENCODED, 'etag': '"ex2"',
                   'extendedProperties': {'private': {'hestia_uid': 'uid', 'unrelated': 'keep', KEY: MARKER, 'hestia_end_unspecified': 'false'}}}
        provider, transport = fixtures.google(fixtures.google_acl(), fixtures.response(master), fixtures.google_page([master]),
            fixtures.response({'items': [instance]}), fixtures.response(encoded), fixtures.response(master), fixtures.google_page([master, encoded]))
        result = provider.put('event', wanted, providers._google_etag(master, []), create=False, uid='uid')
        self.assertEqual(result.payload, wanted)
        body = json.loads(transport.calls[4][2]['body'])
        self.assertEqual(body['description'], ENCODED)
        self.assertEqual(body['extendedProperties']['private'][KEY], MARKER)
        self.assertEqual(body['extendedProperties']['private']['unrelated'], 'keep')

        ordinary_override = {**override, 'description': 'New ordinary override'}
        ordinary_payload = codec.validate_payload({**base, 'exceptions': [ordinary_override]})
        normal = {**encoded, 'description': ordinary_override['description'], 'etag': '"ex3"',
                  'extendedProperties': {'private': {'hestia_uid': 'uid', 'unrelated': 'keep', KEY: '', 'hestia_end_unspecified': 'false'}}}
        provider, transport = fixtures.google(fixtures.google_acl(), fixtures.response(master), fixtures.google_page([master, encoded]),
            fixtures.response({'items': [encoded]}), fixtures.response(normal), fixtures.response(master), fixtures.google_page([master, normal]))
        self.assertEqual(provider.put('event', ordinary_payload, result.etag, create=False, uid='uid').payload, ordinary_payload)
        body = json.loads(transport.calls[4][2]['body'])
        self.assertEqual(body['description'], ordinary_override['description'])
        self.assertEqual(body['extendedProperties']['private'][KEY], '')
        self.assertEqual(body['extendedProperties']['private']['unrelated'], 'keep')
