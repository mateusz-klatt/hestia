"""Unit tests for hestia.flipper — the stdlib-only Flipper Zero RPC IR-transmit client.

The protobuf wire codec (varint/tag/field encoders + the decode helpers) is round-tripped; ``transmit_ir``
is driven end-to-end against a ``FakeTransport`` (records writes, auto-replies with command-id-matched
RPC frames) so every step and error branch is covered with no real serial device; ``SerialTransport``'s
termios open / read / write / close is covered by patching ``os`` / ``termios`` / ``tty`` / ``select``.
"""
from __future__ import annotations

import itertools
import termios
import unittest
from unittest import mock

from hestia import flipper
from hestia.flipper import FlipperError, transmit_ir


def _resp(cid, status=0):
    """A length-delimited PB.Main response: command_id (+ command_status if nonzero) + Empty content."""
    body = flipper._f_varint(1, cid)
    if status:
        body += flipper._f_varint(2, status)
    body += flipper._f_bytes(4, b"")                         # Empty content (Main field 4)
    return flipper._varint(len(body)) + body


class FakeTransport:
    """Records every write; once ``start_rpc_session`` has been written, auto-replies to each RPC frame
    with a status-matched response (overridable per command_id via ``statuses``; a command_id in ``drop``
    gets no reply → forces a timeout). ``prefix`` seeds the read buffer (to test unsolicited-frame skip)."""

    def __init__(self, *, statuses=None, drop=(), prefix=b""):
        self.writes = []
        self.cids = []
        self._inbox = bytearray(prefix)
        self._rpc = False
        self.statuses = statuses or {}
        self.drop = set(drop)
        self.closed = False

    def write(self, data):
        data = bytes(data)
        self.writes.append(data)
        if b"start_rpc_session" in data:
            self._rpc = True
            return
        if not self._rpc:
            return                                           # pre-RPC CLI text (Ctrl-C / loader close)
        cid = flipper._scalar(flipper._take_frame(bytearray(data)), 1)
        self.cids.append(cid)
        if cid in self.drop:
            return
        self._inbox += _resp(cid, self.statuses.get(cid, 0))

    def read(self, timeout):
        if self._inbox:
            out = bytes(self._inbox)
            self._inbox.clear()
            return out
        return b""

    def close(self):
        self.closed = True


class _FastTimeMixin:
    """Patch flipper.time so wall-clock loops (drains, await deadlines) terminate instantly."""

    def setUp(self):
        counter = itertools.count(0.0, 0.05)
        for patcher in (mock.patch.object(flipper.time, "monotonic", new=lambda: next(counter)),
                        mock.patch.object(flipper.time, "sleep", new=lambda *_a: None)):
            patcher.start()
            self.addCleanup(patcher.stop)


# --- protobuf encode ---------------------------------------------------------

class EncodeTests(unittest.TestCase):
    def test_varint_known(self):
        self.assertEqual(flipper._varint(0), b"\x00")
        self.assertEqual(flipper._varint(127), b"\x7f")
        self.assertEqual(flipper._varint(128), b"\x80\x01")
        self.assertEqual(flipper._varint(300), b"\xac\x02")

    def test_varint_negative(self):
        self.assertRaises(FlipperError, flipper._varint, -1)

    def test_tag_single_and_multibyte(self):
        self.assertEqual(flipper._tag(1, 0), b"\x08")        # field 1, varint
        self.assertEqual(flipper._tag(16, 2), b"\x82\x01")   # field 16, len-delimited
        self.assertEqual(flipper._tag(75, 2), b"\xda\x04")   # field 75

    def test_field_encoders(self):
        self.assertEqual(flipper._f_varint(1, 5), b"\x08\x05")
        self.assertEqual(flipper._f_bytes(4, b""), b"\x22\x00")
        self.assertEqual(flipper._f_str(2, "RPC"), b"\x12\x03RPC")

    def test_main_frame_matches_device_wire(self):
        # AppStart(cid=1) Empty-OK response observed live = 0408012200; build the request side here.
        frame = flipper._main_frame(1, 16, flipper._f_str(1, "Infrared") + flipper._f_str(2, "RPC"))
        # length-prefixed; body starts with command_id field 1 = 0x08 0x01
        length, pos = flipper._read_varint(frame, 0)
        self.assertEqual(length, len(frame) - pos)
        self.assertEqual(frame[pos:pos + 2], b"\x08\x01")
        self.assertIn(b"Infrared", frame)
        self.assertIn(b"RPC", frame)


# --- protobuf decode ---------------------------------------------------------

class DecodeTests(unittest.TestCase):
    def test_read_varint_roundtrip(self):
        for n in (0, 1, 127, 128, 300, 16384, 2 ** 63 - 1):
            enc = flipper._varint(n)
            self.assertEqual(flipper._read_varint(enc, 0), (n, len(enc)))

    def test_read_varint_incomplete(self):
        value, pos = flipper._read_varint(b"\x80", 0)        # continuation bit, buffer ends
        self.assertIsNone(value)
        self.assertEqual(pos, 1)

    def test_read_varint_too_long(self):
        self.assertRaises(FlipperError, flipper._read_varint, b"\x80" * 12, 0)

    def test_scalar_finds_and_defaults(self):
        body = flipper._f_varint(1, 7) + flipper._f_varint(2, 21) + flipper._f_bytes(4, b"")
        self.assertEqual(flipper._scalar(body, 1), 7)        # first field
        self.assertEqual(flipper._scalar(body, 2), 21)       # after skipping a wire-0 field
        self.assertIsNone(flipper._scalar(body, 9))          # absent -> None (also skips wire-2)

    def test_scalar_skips_wire1_and_wire5(self):
        body = flipper._tag(3, 5) + b"\x00\x00\x00\x00" + flipper._tag(4, 1) + b"\x00" * 8 \
            + flipper._f_varint(2, 3)
        self.assertEqual(flipper._scalar(body, 2), 3)

    def test_scalar_unsupported_wire(self):
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(1, 3), 2)   # wire type 3

    def test_scalar_target_wrong_wire_type(self):
        # the target field arriving as anything but a varint is a malformed reply, not "absent"
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(2, 2) + b"\x00", 2)

    def test_scalar_skipped_field_overruns_frame(self):
        self.assertRaises(FlipperError, flipper._scalar,                       # wire-2 length past end
                          flipper._tag(4, 2) + flipper._varint(5) + b"ab", 2)
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(3, 5) + b"\x00", 2)   # wire-5 past end
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(3, 1) + b"\x00", 2)   # wire-1 past end

    def test_scalar_truncated_skipped_varint(self):
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(3, 0) + b"\x80", 2)

    def test_scalar_truncated_tag(self):
        self.assertRaises(FlipperError, flipper._scalar, b"\x80", 2)              # tag varint cut off

    def test_scalar_truncated_scalar_value(self):
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(2, 0) + b"\x80", 2)

    def test_scalar_truncated_length_delimited(self):
        self.assertRaises(FlipperError, flipper._scalar, flipper._tag(4, 2) + b"\x80", 2)

    def test_take_frame_complete_and_partial(self):
        full = _resp(3, 0)
        buf = bytearray(full)
        body = flipper._take_frame(buf)
        self.assertIsNotNone(body)
        self.assertEqual(buf, b"")                            # fully consumed
        self.assertEqual(flipper._scalar(body, 1), 3)
        self.assertIsNone(flipper._take_frame(bytearray(b"\x80")))        # length prefix incomplete
        self.assertIsNone(flipper._take_frame(bytearray(full[:-1])))      # body not fully arrived


# --- await_status ------------------------------------------------------------

class AwaitStatusTests(_FastTimeMixin, unittest.TestCase):
    def test_skips_unsolicited_then_returns(self):
        # an unsolicited frame (cid 99) precedes the real reply (cid 5, status 0)
        t = FakeTransport(prefix=_resp(99, 0))
        t._rpc = True
        t.write(flipper._main_frame(5, flipper._APP_EXIT, b""))   # queues _resp(5, 0) after the prefix
        self.assertEqual(flipper._await_status(t, 5, 2.5), 0)

    def test_timeout(self):
        t = FakeTransport()
        self.assertRaises(FlipperError, flipper._await_status, t, 1, 0.2)


# --- transmit_ir -------------------------------------------------------------

class TransmitTests(_FastTimeMixin, unittest.TestCase):
    def test_happy_press_release(self):
        t = FakeTransport()
        transmit_ir("/ext/infrared/Klima.ir", "Power", transport_factory=lambda dev: t)
        self.assertEqual(t.writes[0], b"\x03")
        self.assertEqual(t.writes[1], b"loader close\r")
        self.assertEqual(t.writes[2], b"start_rpc_session\r")
        self.assertEqual(t.cids, [1, 2, 3, 4])               # AppStart, LoadFile, PressRelease, Exit
        joined = b"".join(t.writes)
        self.assertIn(b"Infrared", joined)
        self.assertIn(b"/ext/infrared/Klima.ir", joined)
        self.assertIn(b"Power", joined)
        self.assertTrue(t.closed)

    def test_happy_separate_press_release(self):
        t = FakeTransport()
        transmit_ir("/ext/infrared/Klima.ir", "Power", transport_factory=lambda dev: t,
                    press_release=False)
        self.assertEqual(t.cids, [1, 2, 3, 4, 5])            # Press + Release are separate
        self.assertTrue(t.closed)

    def test_device_passed_to_factory(self):
        seen = {}

        def factory(dev):
            seen["dev"] = dev
            return FakeTransport()

        transmit_ir("/ext/infrared/Klima.ir", "Power", device="/dev/ttyACM7", transport_factory=factory)
        self.assertEqual(seen["dev"], "/dev/ttyACM7")

    def test_non_ok_status_raises_and_closes(self):
        t = FakeTransport(statuses={2: 17})                  # AppLoadFile -> ERROR_APP_SYSTEM_LOCKED
        with self.assertRaises(FlipperError) as cm:
            transmit_ir("/ext/infrared/Klima.ir", "Power", transport_factory=lambda dev: t)
        self.assertIn("ERROR_APP_SYSTEM_LOCKED", str(cm.exception))
        self.assertTrue(t.closed)

    def test_unknown_status_code_message(self):
        t = FakeTransport(statuses={1: 99})
        with self.assertRaises(FlipperError) as cm:
            transmit_ir("/ext/infrared/Klima.ir", "Power", transport_factory=lambda dev: t)
        self.assertIn("status 99", str(cm.exception))
        self.assertTrue(t.closed)

    def test_timeout_closes_transport(self):
        t = FakeTransport(drop={3})                          # the button press gets no reply
        self.assertRaises(FlipperError, transmit_ir, "/ext/infrared/Klima.ir", "Power",
                          transport_factory=lambda dev: t, timeout=0.2)
        self.assertTrue(t.closed)


# --- SerialTransport (termios mocked) ----------------------------------------

def _attrs():
    return [0, 0, 0, 0, 0, 0, [0] * 32]


class SerialTransportTests(unittest.TestCase):
    def _open(self, **kw):
        """Construct a SerialTransport with os/termios/tty open+config mocked; return (transport, mocks)."""
        patchers = {
            "open": mock.patch.object(flipper.os, "open", return_value=7),
            "setraw": mock.patch.object(flipper.tty, "setraw"),
            "tcgetattr": mock.patch.object(flipper.termios, "tcgetattr", return_value=_attrs()),
            "tcsetattr": mock.patch.object(flipper.termios, "tcsetattr"),
            "tcflush": mock.patch.object(flipper.termios, "tcflush"),
        }
        started = {k: p.start() for k, p in patchers.items()}
        for p in patchers.values():
            self.addCleanup(p.stop)
        return flipper.SerialTransport("/dev/ttyACM9"), started

    def test_open_ok(self):
        t, m = self._open()
        self.assertEqual(t.fd, 7)
        m["open"].assert_called_once()
        m["setraw"].assert_called_once_with(7)

    def test_open_failure(self):
        with mock.patch.object(flipper.os, "open", side_effect=OSError("nope")):
            self.assertRaises(FlipperError, flipper.SerialTransport, "/dev/ttyACM9")

    def test_configure_failure_closes_fd(self):
        with mock.patch.object(flipper.os, "open", return_value=7), \
             mock.patch.object(flipper.tty, "setraw", side_effect=termios.error("bad")), \
             mock.patch.object(flipper.os, "close") as m_close:
            self.assertRaises(FlipperError, flipper.SerialTransport, "/dev/ttyACM9")
            m_close.assert_called_once_with(7)

    def test_write_partial_blocking_then_done(self):
        t, _ = self._open()
        # 1st call partial (3 bytes), 2nd raises EAGAIN → wait for writable, 3rd writes the rest.
        with mock.patch.object(flipper.os, "write", side_effect=[3, BlockingIOError(), 100]), \
             mock.patch.object(flipper.select, "select", return_value=([], [t.fd], [])):
            t.write(b"x" * 10)

    def test_write_timeout(self):
        t, _ = self._open()
        clock = itertools.count(0.0, 3.0)                # advances past the 5 s deadline within 2 ticks
        with mock.patch.object(flipper.os, "write", side_effect=BlockingIOError()), \
             mock.patch.object(flipper.select, "select", return_value=([], [], [])), \
             mock.patch.object(flipper.time, "monotonic", new=lambda: next(clock)):
            self.assertRaises(FlipperError, t.write, b"hello")

    def test_write_oserror(self):
        t, _ = self._open()
        with mock.patch.object(flipper.os, "write", side_effect=OSError("io")):
            self.assertRaises(FlipperError, t.write, b"hello")

    def test_write_select_oserror(self):
        t, _ = self._open()
        with mock.patch.object(flipper.os, "write", side_effect=BlockingIOError()), \
             mock.patch.object(flipper.select, "select", side_effect=OSError("poll fail")):
            self.assertRaises(FlipperError, t.write, b"hello")

    def test_read_ready(self):
        t, _ = self._open()
        with mock.patch.object(flipper.select, "select", return_value=([7], [], [])), \
             mock.patch.object(flipper.os, "read", return_value=b"abc"):
            self.assertEqual(t.read(0.1), b"abc")

    def test_read_not_ready(self):
        t, _ = self._open()
        with mock.patch.object(flipper.select, "select", return_value=([], [], [])):
            self.assertEqual(t.read(0.1), b"")

    def test_read_would_block(self):
        t, _ = self._open()
        with mock.patch.object(flipper.select, "select", return_value=([7], [], [])), \
             mock.patch.object(flipper.os, "read", side_effect=BlockingIOError()):
            self.assertEqual(t.read(0.1), b"")

    def test_read_oserror(self):
        t, _ = self._open()
        with mock.patch.object(flipper.select, "select", side_effect=OSError("io")):
            self.assertRaises(FlipperError, t.read, 0.1)

    def test_close_ok_and_swallows_error(self):
        t, _ = self._open()
        with mock.patch.object(flipper.os, "close") as m_close:
            t.close()
            m_close.assert_called_once_with(7)
        with mock.patch.object(flipper.os, "close", side_effect=OSError("already")):
            t.close()                                        # must not raise


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
