"""Unit tests for the 0x7e codec in hestia.protocol."""
from __future__ import annotations

import unittest

from hestia.protocol import (
    FLAG,
    Deframer,
    Frame,
    TLV,
    build_frame,
    iter_frames,
    parse_tlvs,
    tlv,
    xor_checksum,
)


class XorChecksumTests(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(xor_checksum(b""), 0)

    def test_xor_reduce(self):
        self.assertEqual(xor_checksum(b"\x01\x02"), 0x03)
        self.assertEqual(xor_checksum(b"\xff\x0f"), 0xF0)


# Deframing is length-driven (0x7e is unescaped data too), so tests use real
# `build_frame()` frames; `_body` is the bit a deframer yields (between the flags).
def _wire(ftype=0x1E, cmd=0x09, payload=b"\x00"):
    return build_frame(ftype, cmd, payload)


def _body(ftype=0x1E, cmd=0x09, payload=b"\x00"):
    return build_frame(ftype, cmd, payload)[1:-1]


class IterFramesTests(unittest.TestCase):
    def test_two_frames(self):
        stream = _wire(0x1E, 0x09, tlv(0x0047, b"\x12")) + _wire(0x66, 0x01, b"")
        self.assertEqual(
            list(iter_frames(stream)),
            [_body(0x1E, 0x09, tlv(0x0047, b"\x12")), _body(0x66, 0x01, b"")],
        )

    def test_empty_stream(self):
        self.assertEqual(list(iter_frames(b"")), [])

    def test_no_flag_yields_nothing(self):
        self.assertEqual(list(iter_frames(b"junk-without-a-delimiter")), [])

    def test_trailing_incomplete_frame_discarded(self):
        # a complete frame followed by a dangling partial header (no length/body)
        stream = _wire(0x66, 0x01, b"") + b"\x7e\x1e\x09"
        self.assertEqual(list(iter_frames(stream)), [_body(0x66, 0x01, b"")])


class DeframerTests(unittest.TestCase):
    def test_single_frame(self):
        self.assertEqual(list(Deframer().feed(_wire())), [_body()])

    def test_multiple_frames_one_chunk(self):
        stream = _wire(0x1E, 0x09, b"\x00") + _wire(0x66, 0x01, b"")
        self.assertEqual(
            list(Deframer().feed(stream)), [_body(0x1E, 0x09, b"\x00"), _body(0x66, 0x01, b"")]
        )

    def test_buffers_partial_body_across_chunks(self):
        wire = _wire(0x1E, 0x09, tlv(0x0047, b"\x12"))
        d = Deframer()
        self.assertEqual(list(d.feed(wire[:6])), [])        # flag + part of the body
        self.assertEqual(list(d.feed(wire[6:])), [wire[1:-1]])

    def test_partial_header_waits(self):
        wire = _wire(0x1E, 0x09, b"\x00")
        d = Deframer()
        self.assertEqual(list(d.feed(wire[:3])), [])        # flag + 2 header bytes (< 4)
        self.assertEqual(list(d.feed(wire[3:])), [wire[1:-1]])

    def test_double_flag_between_frames_skipped(self):
        # an extra delimiter between frames exercises the idle-flag skip (i > 0)
        stream = _wire(0x1E, 0x09, b"\x00") + b"\x7e" + _wire(0x66, 0x01, b"")
        self.assertEqual(
            list(Deframer().feed(stream)), [_body(0x1E, 0x09, b"\x00"), _body(0x66, 0x01, b"")]
        )

    def test_double_flag_only_yields_nothing(self):
        self.assertEqual(list(Deframer().feed(b"\x7e\x7e")), [])

    def test_leading_preamble_skipped(self):
        # connection-preamble junk before the first flag is dropped (HDLC hunt)
        self.assertEqual(list(Deframer().feed(b"\x20\x20\x00\x00" + _wire())), [_body()])

    def test_no_flag_clears_and_yields_nothing(self):
        self.assertEqual(list(Deframer().feed(b"no flags here")), [])

    def test_oversized_length_resyncs(self):
        junk = b"\x7e\x1e\x09\xff\xff"                       # claimed length 0xffff > MAX_PAYLOAD
        self.assertEqual(list(Deframer().feed(junk + _wire(0x66, 0x01, b""))), [_body(0x66, 0x01, b"")])

    def test_bad_checksum_resyncs_to_next_frame(self):
        bad = bytearray(_wire(0x1E, 0x09, b"\x00"))
        bad[-2] ^= 0xFF                                      # corrupt the checksum byte
        self.assertEqual(
            list(Deframer().feed(bytes(bad) + _wire(0x66, 0x01, b""))), [_body(0x66, 0x01, b"")]
        )

    def test_0x7e_inside_payload_recovered(self):
        # a payload byte equal to the delimiter must NOT split the frame
        wire = _wire(0x1E, 0x09, b"\xaa\x7e\xbb")
        self.assertEqual(list(Deframer().feed(wire)), [wire[1:-1]])

    def test_0x7e_in_length_byte_recovered(self):
        # payload length 0x7e (126) -> the low length byte is 0x7e, read as data
        wire = _wire(0x1E, 0x09, b"\x00" * 0x7E)
        self.assertEqual(list(Deframer().feed(wire)), [wire[1:-1]])

    def test_early_break_then_resume_keeps_no_stale_state(self):
        d = Deframer()
        gen = d.feed(_wire(0x1E, 0x09, b"\x00") + _wire(0x66, 0x01, b""))
        self.assertEqual(next(gen), _body(0x1E, 0x09, b"\x00"))   # take one, abandon generator
        self.assertEqual(list(d.feed(b"")), [_body(0x66, 0x01, b"")])  # 2nd frame still surfaces

    def test_stray_byte_after_checksum_not_dropped(self):
        # Observed on the wire: a valid, checksum-complete frame is followed by a
        # stray non-flag byte BEFORE the next delimiter (no flag right after the
        # checksum). Acceptance is on checksum alone — NOT a trailing 0x7e — so the
        # frame (and the next one) still surface. Guards against a future "require
        # trailing flag" change silently dropping these (10 such frames in the
        # reference frames). See PROTOCOL.md §1.
        a_body = _body(0x1E, 0x0A, b"\x00")
        stream = b"\x7e" + a_body + b"\x01" + b"\x7e" + _wire(0x66, 0x01, b"")
        self.assertEqual(list(Deframer().feed(stream)), [a_body, _body(0x66, 0x01, b"")])


class BuildFrameAndTlvTests(unittest.TestCase):
    def test_tlv_layout(self):
        self.assertEqual(tlv(0x0047, b"\x12"), b"\x00\x47\x00\x01\x12")

    def test_build_frame_roundtrips(self):
        raw = build_frame(0x1E, 0x09, b"\x00")
        self.assertEqual(raw[0], FLAG)
        self.assertEqual(raw[-1], FLAG)
        frame = Frame(raw[1:-1])
        self.assertEqual(frame.type, 0x1E)
        self.assertEqual(frame.cmd, 0x09)
        self.assertEqual(frame.payload, b"\x00")
        self.assertTrue(frame.checksum_ok)


class TLVTests(unittest.TestCase):
    def test_text_renders_printable_and_dots(self):
        self.assertEqual(TLV(0x1, b"AB\x00").text, "AB.")

    def test_str_has_tag_and_text(self):
        s = str(TLV(0x0047, b"\x12"))
        self.assertIn("0x0047", s)
        self.assertIn("12", s)


class ParseTlvsTests(unittest.TestCase):
    def test_single_tlv(self):
        out = parse_tlvs(tlv(0x0047, b"\x12"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].tag, 0x0047)
        self.assertEqual(out[0].value, b"\x12")

    def test_truncated_header_stops(self):
        self.assertEqual(parse_tlvs(b"\x00\x47\x00"), [])      # i+4 > n

    def test_value_longer_than_remaining_stops(self):
        self.assertEqual(parse_tlvs(b"\x00\x47\x00\x05AB"), [])  # i+4+len > n


class FrameTests(unittest.TestCase):
    def _good_body(self, payload=b""):
        return build_frame(0x1E, 0x09, payload)[1:-1]

    def test_properties(self):
        body = self._good_body(tlv(0x0047, b"\x12"))
        frame = Frame(body)
        self.assertEqual(frame.type, 0x1E)
        self.assertEqual(frame.cmd, 0x09)
        self.assertEqual(frame.length, len(tlv(0x0047, b"\x12")))
        self.assertEqual(frame.core, body[:-1])
        self.assertEqual(frame.payload, tlv(0x0047, b"\x12"))
        self.assertTrue(frame.checksum_ok)
        self.assertEqual(frame.computed_checksum, frame.stored_checksum)
        self.assertEqual([t.tag for t in frame.tlvs()], [0x0047])

    def test_stored_checksum_absent_returns_minus_one(self):
        # length claims 5 payload bytes but body is too short -> checksum index past end
        frame = Frame(b"\x1e\x09\x00\x05AB")
        self.assertEqual(frame.stored_checksum, -1)

    def test_str_short_frame(self):
        self.assertIn("short", str(Frame(b"\x1e")))

    def test_str_ok_frame(self):
        s = str(Frame(self._good_body(tlv(0x0047, b"\x12"))))
        self.assertIn("(ok)", s)

    def test_str_bad_checksum(self):
        body = bytearray(self._good_body(tlv(0x0047, b"\x12")))
        body[-1] ^= 0xFF                       # corrupt the checksum byte
        self.assertIn("BAD", str(Frame(bytes(body))))

    def test_str_reports_unparsed_leftover(self):
        body = self._good_body(tlv(0x0047, b"\x12") + b"\xaa\xbb")
        self.assertIn("unparsed", str(Frame(body)))


if __name__ == "__main__":
    unittest.main()
