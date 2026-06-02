"""Unit tests for hestia.tuya — the stdlib-only Tuya v3.3 local client.

AES is pinned to FIPS-197 known-answer vectors; the framing/message codec is round-tripped; and
``TuyaDevice.status()`` is exercised end-to-end against a real loopback TCP server (a thread serving
canned, properly-encrypted frames) so every protocol + error branch is covered without a real device.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
import unittest

from hestia import tuya
from hestia.tuya import TuyaDevice, TuyaError

KEY = b"0123456789abcdef"          # 16-byte test local_key
ID22 = "0123456789abcdefghijkl"    # 22 chars -> device22
ID_NORMAL = "01234567890abcdef"    # not 22 chars -> plain DP_QUERY


def _h(s):
    return bytes.fromhex(s)


# --- AES (FIPS-197) ----------------------------------------------------------

class AesTests(unittest.TestCase):
    def test_fips197_c1_encrypt_decrypt(self):
        key = _h("000102030405060708090a0b0c0d0e0f")
        pt = _h("00112233445566778899aabbccddeeff")
        ct = _h("69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(tuya.aes_ecb_encrypt(pt, key), ct)
        self.assertEqual(tuya.aes_ecb_decrypt(ct, key), pt)

    def test_fips197_b_vector(self):
        key = _h("2b7e151628aed2a6abf7158809cf4f3c")
        pt = _h("3243f6a8885a308d313198a2e0370734")
        ct = _h("3925841d02dc09fbdc118597196a0b32")
        self.assertEqual(tuya.aes_ecb_encrypt(pt, key), ct)
        self.assertEqual(tuya.aes_ecb_decrypt(ct, key), pt)

    def test_frozen_golden_vectors(self):
        # Frozen from the PRE-cryptography pure-Python impl (Phase-1 swap parity guard): the
        # cryptography-backed AES must reproduce the exact ciphertext AND the full Tuya v3.3 frame
        # byte-for-byte. AES-ECB is deterministic, so this pins the swap to the captured output.
        key = _h("000102030405060708090a0b0c0d0e0f")
        padded = _h("6865737469612d747579612d6165732d676f6c64656e2d766563746f72212101")
        ct = _h("ed3a3d208ab3be13523a87f823a6b603a1bb617086e30510d96cd49c5bf7704d")
        self.assertEqual(tuya.aes_ecb_encrypt(padded, key), ct)
        self.assertEqual(tuya.aes_ecb_decrypt(ct, key), padded)
        frame = tuya._pack(1, tuya._CONTROL_NEW,
                           tuya.encode_message(tuya._CONTROL_NEW, key, {"dps": {"1": True}}))
        self.assertEqual(frame, _h(
            "000055aa00000001"                                                 # prefix + seq
            "0000000d00000037"                                                 # cmd CONTROL_NEW + length
            "332e33000000000000000000000000"                                   # 15-byte version header
            "b54a5b52cb671ce0ef2ecb70a48979d461d2bc85618fb330888e51310b27a5b4"  # AES-ECB(pkcs7(json))
            "0b2359ff0000aa55"))                                               # crc32 + suffix

    def test_ecb_multiblock_roundtrip(self):
        data = bytes(range(48))                       # 3 blocks
        self.assertEqual(tuya.aes_ecb_decrypt(tuya.aes_ecb_encrypt(data, KEY), KEY), data)

    def test_pkcs7_roundtrip_and_full_block(self):
        for raw in (b"", b"x", b"0123456789abcde", b"0123456789abcdef"):   # incl. full-block pad
            self.assertEqual(tuya._pkcs7_unpad(tuya._pkcs7_pad(raw)), raw)

    def test_aes_arg_guards(self):
        self.assertRaises(TuyaError, tuya.aes_ecb_encrypt, b"\x00" * 16, b"short")     # bad key len
        self.assertRaises(TuyaError, tuya.aes_ecb_decrypt, b"\x00" * 15, KEY)          # non-block data
        self.assertRaises(TuyaError, tuya.aes_ecb_encrypt, b"", KEY)                   # empty data

    def test_pkcs7_bad(self):
        self.assertRaises(TuyaError, tuya._pkcs7_unpad, b"")                # empty
        self.assertRaises(TuyaError, tuya._pkcs7_unpad, b"x" * 15)          # not a block multiple
        self.assertRaises(TuyaError, tuya._pkcs7_unpad, b"\x00" * 16)       # n=0
        self.assertRaises(TuyaError, tuya._pkcs7_unpad, b"a" * 15 + b"\x05")  # n=5 but bytes wrong


# --- framing -----------------------------------------------------------------

def _response(cmd, *, dps=None, retcode=0, with_header=True, key=KEY, empty=False):
    """Build a device→app frame: region = retcode(4) + [version header] + AES-ECB(json)."""
    if empty:
        region = struct.pack(">I", retcode)            # ACK with no data
    else:
        msg = {} if dps is None else {"dps": dps}
        ct = tuya.aes_ecb_encrypt(tuya._pkcs7_pad(json.dumps(msg, separators=(",", ":")).encode()), key)
        region = struct.pack(">I", retcode) + ((tuya._VER_HEADER + ct) if with_header else ct)
    return tuya._pack(0, cmd, region)


class FrameTests(unittest.TestCase):
    def test_unpack_roundtrip(self):
        frame = _response(tuya._DP_QUERY, dps={"2": 259})
        seq, cmd, retcode, region = tuya._unpack(frame)
        self.assertEqual((seq, cmd, retcode), (0, tuya._DP_QUERY, 0))
        self.assertEqual(tuya.decode_message(KEY, region)["dps"], {"2": 259})

    def test_unpack_rejects(self):
        good = _response(tuya._DP_QUERY, dps={"1": True})
        self.assertRaises(TuyaError, tuya._unpack, b"\x00" * 10)                 # too short
        self.assertRaises(TuyaError, tuya._unpack, b"\xde\xad\xbe\xef" + good[4:])  # bad prefix
        self.assertRaises(TuyaError, tuya._unpack, good[:-1])                     # truncated
        self.assertRaises(TuyaError, tuya._unpack, good[:-8] + b"\x00" * 4 + good[-4:])  # bad crc
        self.assertRaises(TuyaError, tuya._unpack, good[:-4] + b"\x00" * 4)       # bad suffix
        short_len = struct.pack(">IIII", tuya._PREFIX, 0, tuya._DP_QUERY, 5) + b"\x00" * 8  # length<12
        self.assertRaises(TuyaError, tuya._unpack, short_len)


# --- message crypto ----------------------------------------------------------

class MessageTests(unittest.TestCase):
    def test_dp_query_command_has_no_header(self):
        m = tuya.encode_message(tuya._DP_QUERY, KEY, {"a": 1})
        self.assertNotEqual(m[:3], b"3.3")
        self.assertEqual(len(m) % 16, 0)

    def test_control_new_command_has_header(self):
        m = tuya.encode_message(tuya._CONTROL_NEW, KEY, {"a": 1})
        self.assertEqual(m[:15], tuya._VER_HEADER)

    def test_decode_with_and_without_header(self):
        for cmd in (tuya._DP_QUERY, tuya._CONTROL_NEW):
            payload = tuya.encode_message(cmd, KEY, {"dps": {"2": 261}})
            self.assertEqual(tuya.decode_message(KEY, payload), {"dps": {"2": 261}})

    def test_decode_bad_length(self):
        self.assertRaises(TuyaError, tuya.decode_message, KEY, b"")
        self.assertRaises(TuyaError, tuya.decode_message, KEY, b"\x01" * 7)      # not a block multiple

    def test_decode_bad_json(self):
        ct = tuya.aes_ecb_encrypt(tuya._pkcs7_pad(b"not-json"), KEY)
        self.assertRaises(TuyaError, tuya.decode_message, KEY, ct)

    def test_decode_misaligned_33_header_not_stripped(self):
        # starts with b"3.3" but (len-15) % 16 != 0 -> header NOT stripped -> decrypt garbage -> TuyaError
        self.assertRaises(TuyaError, tuya.decode_message, KEY, b"3.3" + b"\x00" * 12 + b"X" * 17)


# --- TuyaDevice / status() ---------------------------------------------------

class _FakeServer:
    """One-shot loopback TCP server: accepts one connection, records the request, sends `chunks`
    (each a bytes blob, with a tiny gap between to exercise the recv-loop), then closes."""

    def __init__(self, chunks, read_request=True):
        self.chunks = chunks
        self.read_request = read_request
        self.request = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        try:
            conn, _ = self._sock.accept()
            with conn:
                if self.read_request:
                    conn.settimeout(2.0)
                    try:
                        self.request = conn.recv(4096)
                    except OSError:
                        pass
                for i, chunk in enumerate(self.chunks):
                    if i:
                        time.sleep(0.02)
                    conn.sendall(chunk)
        except OSError:
            pass
        finally:
            self._sock.close()

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class TuyaDeviceTests(unittest.TestCase):
    def test_bad_key_length(self):
        self.assertRaises(TuyaError, TuyaDevice, "127.0.0.1", ID22, "short")

    def test_device22_detection_and_query(self):
        dev = TuyaDevice("127.0.0.1", ID22, KEY)
        self.assertTrue(dev.device22)
        cmd, frame = dev._query()
        self.assertEqual(cmd, tuya._CONTROL_NEW)
        sent = tuya.decode_message(KEY, _strip_retcodeless(frame))            # our request has no retcode
        self.assertEqual(sent["devId"], ID22)
        self.assertEqual(set(sent["dps"]), {str(i) for i in range(1, 21)})    # null-map 1..20

    def test_normal_device_uses_dp_query(self):
        dev = TuyaDevice("127.0.0.1", ID_NORMAL, KEY)
        self.assertFalse(dev.device22)
        self.assertEqual(dev._query()[0], tuya._DP_QUERY)

    def _status(self, chunks, device_id=ID22):
        srv = _FakeServer(chunks)
        try:
            return TuyaDevice("127.0.0.1", device_id, KEY, port=srv.port, timeout=2.0).status()
        finally:
            srv.close()

    def test_status_success(self):
        self.assertEqual(self._status([_response(tuya._CONTROL_NEW, dps={"2": 259})]), {"2": 259})

    def test_status_via_dp_query_response(self):
        self.assertEqual(self._status([_response(tuya._DP_QUERY, dps={"1": True}, with_header=False)]),
                         {"1": True})

    def test_status_status_cmd_response(self):     # cmd 0x08 also accepted
        self.assertEqual(self._status([_response(tuya._STATUS, dps={"3": 5})]), {"3": 5})

    def test_status_two_chunk_recv(self):          # frame split across two recv()s
        frame = _response(tuya._CONTROL_NEW, dps={"2": 260})
        self.assertEqual(self._status([frame[:10], frame[10:]]), {"2": 260})

    def test_status_skips_empty_ack(self):
        ack = _response(tuya._CONTROL_NEW, empty=True)
        data = _response(tuya._CONTROL_NEW, dps={"2": 262})
        self.assertEqual(self._status([ack + data]), {"2": 262})

    def test_status_no_dps_then_exhausted(self):   # only empty ACKs -> raise after MAX_FRAMES
        ack = _response(tuya._CONTROL_NEW, empty=True)
        self.assertRaises(TuyaError, self._status, [ack * tuya._MAX_FRAMES])

    def test_status_no_dps_key(self):              # valid json but no "dps" -> keep looping -> raise
        nodps = _response(tuya._CONTROL_NEW, dps=None)
        self.assertRaises(TuyaError, self._status, [nodps * tuya._MAX_FRAMES])

    def test_status_nonzero_retcode(self):
        self.assertRaises(TuyaError, self._status, [_response(tuya._CONTROL_NEW, dps={"1": 1}, retcode=1)])

    def test_status_unexpected_cmd(self):
        self.assertRaises(TuyaError, self._status, [_response(0x99, dps={"1": 1})])

    def test_status_bad_prefix(self):
        bad = b"\xde\xad\xbe\xef" + _response(tuya._CONTROL_NEW, dps={"1": 1})[4:]
        self.assertRaises(TuyaError, self._status, [bad])

    def test_status_oversized_length(self):
        head = struct.pack(">IIII", tuya._PREFIX, 0, tuya._CONTROL_NEW, tuya._MAX_FRAME + 1)
        self.assertRaises(TuyaError, self._status, [head + b"\x00" * 16])

    def test_status_closed_midframe(self):         # server closes after a partial header
        self.assertRaises(TuyaError, self._status, [b"\x00\x00U\xaa"])

    def test_status_connection_refused(self):
        # connect to a port with no listener
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        dev = TuyaDevice("127.0.0.1", ID22, KEY, port=free_port, timeout=1.0)
        self.assertRaises(TuyaError, dev.status)


def _strip_retcodeless(frame):
    """Our REQUEST frames carry no retcode; return the encoded-message region directly."""
    _prefix, _seq, _cmd, length = struct.unpack(">IIII", frame[:16])
    return frame[16:16 + length - 8]


if __name__ == "__main__":
    unittest.main()
