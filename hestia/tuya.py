"""Minimal Tuya **protocol v3.3** local client (stdlib + the ``cryptography`` library for AES).

Reads a Tuya v3.3 device's data points (DPS) over the LAN (TCP 6668) with **no cloud**. The
AES-128-ECB primitive comes from ``cryptography``; the Tuya framing, PKCS7 padding, version header,
and device22 quirks are implemented here (validated against FIPS-197 known-answer vectors). Built
for the Neno baby monitor, which is a Tuya
v3.3 **"device22"**: its 22-character id makes the plain ``DP_QUERY`` return ``"data unvalid"``, so the
data must be requested via ``CONTROL_NEW`` (cmd ``0x0d``) with an explicit null-valued ``dps`` map.

Wire format (v3.3, big-endian): ``prefix 0x000055AA | seq(4) | cmd(4) | length(4) | <region> | crc32(4)
| suffix 0x0000AA55``, where ``length`` counts ``<region> + crc + suffix`` and a device→app ``<region>``
begins with a 4-byte return code. Command/response payloads are ``AES-ECB(compact-json)``; every command
EXCEPT ``DP_QUERY (0x0a)`` — and every device→app reply — additionally carries a 15-byte **cleartext**
version header ``b"3.3" + 12·NUL`` in front of the ciphertext.

Scope: read-only ``TuyaDevice.status()``, protocol v3.3 only. See ``docs/TUYA.md``.
"""
from __future__ import annotations

import binascii
import json
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_PREFIX = 0x000055AA
_SUFFIX = 0x0000AA55
_DP_QUERY = 0x0A
_CONTROL_NEW = 0x0D
_STATUS = 0x08
_NO_HEADER_CMDS = frozenset({_DP_QUERY})        # only DP_QUERY skips the version header
_ACCEPT_CMDS = frozenset({_DP_QUERY, _CONTROL_NEW, _STATUS})
_VER_HEADER = b"3.3" + b"\x00" * 12             # 15-byte cleartext prefix before the ciphertext
_MAX_FRAME = 8192                               # cap one response frame (DoS guard)
_MAX_FRAMES = 4                                 # skip at most this many empty ACKs
_FRAME_HEAD = ">IIII"                           # big-endian header struct: prefix|seq|cmd|length


class TuyaError(Exception):
    """Any Tuya protocol / framing / crypto / I-O failure (the caller never sees a raw exception)."""


# --- AES-128-ECB via the `cryptography` library ------------------------------------------------
# Tuya v3.3 mandates raw ECB; `cryptography` supplies the block cipher. PKCS7 padding, the version
# header, and the device22 framing below all stay first-party (the clean-room protocol asset).


def _pkcs7_pad(data: bytes) -> bytes:
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16 != 0:
        raise TuyaError("bad block length")
    n = data[-1]
    if not 1 <= n <= 16 or data[-n:] != bytes([n]) * n:
        raise TuyaError("bad PKCS7 padding")
    return data[:-n]


def _check_aes_args(data: bytes, key: bytes) -> None:
    """Guard the public AES helpers so a bad key/length fails as a clear ``TuyaError`` rather than a
    raw ``IndexError`` (short key) or silent nonstandard output (overlong key / partial block)."""
    if len(key) != 16:
        raise TuyaError("AES-128 key must be 16 bytes")
    if not data or len(data) % 16 != 0:
        raise TuyaError("AES-ECB data must be a non-empty multiple of 16 bytes")


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    _check_aes_args(data, key)                       # guard before the cipher: clear TuyaError, not a raw ValueError
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()  # NOSONAR S5542: Tuya v3.3 mandates AES-128-ECB on the wire (device-dictated, not a choice)
    return enc.update(data) + enc.finalize()


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    _check_aes_args(data, key)
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()  # NOSONAR S5542: Tuya v3.3 mandates AES-128-ECB on the wire (device-dictated, not a choice)
    return dec.update(data) + dec.finalize()


# --- Tuya v3.3 framing + message crypto -------------------------------------------------------

def _pack(seq: int, cmd: int, payload: bytes) -> bytes:
    """Build a frame. ``payload`` is the region between the length field and the crc (for a command:
    the encoded message; the 4-byte return code, if any, must already be inside it)."""
    head = struct.pack(_FRAME_HEAD, _PREFIX, seq, cmd, len(payload) + 8)
    body = head + payload
    return body + struct.pack(">II", binascii.crc32(body), _SUFFIX)


def _unpack(buf: bytes):
    """Parse a device→app frame -> ``(seq, cmd, retcode, payload)`` (4-byte retcode stripped)."""
    if len(buf) < 24:
        raise TuyaError("frame too short")
    prefix, seq, cmd, length = struct.unpack(_FRAME_HEAD, buf[:16])
    if prefix != _PREFIX:
        raise TuyaError("bad prefix")
    total = 16 + length
    if length < 12 or len(buf) < total:
        raise TuyaError("truncated frame")
    crc, suffix = struct.unpack(">II", buf[total - 8:total])
    if suffix != _SUFFIX:
        raise TuyaError("bad suffix")
    if binascii.crc32(buf[:total - 8]) != crc:
        raise TuyaError("bad crc")
    region = buf[16:total - 8]                       # retcode(4) + data
    retcode = struct.unpack(">I", region[:4])[0]
    return seq, cmd, retcode, region[4:]


def encode_message(cmd: int, key: bytes, obj: dict) -> bytes:
    """Compact-JSON -> AES-ECB; prepend the 15-byte cleartext version header for every command
    except ``DP_QUERY (0x0a)``."""
    ct = aes_ecb_encrypt(_pkcs7_pad(json.dumps(obj, separators=(",", ":")).encode("utf-8")), key)
    return ct if cmd in _NO_HEADER_CMDS else _VER_HEADER + ct


def decode_message(key: bytes, payload: bytes) -> dict:
    """Strip the optional 15-byte cleartext version header (BEFORE decrypting), AES-ECB decrypt,
    PKCS7-unpad, JSON-decode."""
    if len(payload) >= 15 and payload[:3] == b"3.3" and (len(payload) - 15) % 16 == 0:
        payload = payload[15:]
    if not payload or len(payload) % 16 != 0:
        raise TuyaError("bad payload length")
    plain = _pkcs7_unpad(aes_ecb_decrypt(payload, key))
    try:
        return json.loads(plain)
    except ValueError as exc:                        # UnicodeDecodeError ⊂ ValueError
        raise TuyaError(f"bad json ({exc})") from None


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise TuyaError("connection closed mid-frame")
        buf += chunk
    return buf


class TuyaDevice:
    """Read-only Tuya v3.3 local device. ``status()`` returns the DPS dict (e.g. ``{"1": true,
    "2": 259}``). A 22-character ``device_id`` selects the ``device22`` query path automatically."""

    def __init__(self, ip: str, device_id: str, local_key, *, port: int = 6668, timeout: float = 3.0):
        key = local_key.encode("utf-8") if isinstance(local_key, str) else bytes(local_key)
        if len(key) != 16:
            raise TuyaError("local_key must be 16 bytes")
        self.ip = ip
        self.device_id = device_id
        self.key = key
        self.port = port
        self.timeout = timeout
        self.device22 = len(device_id) == 22
        self.dps_to_request = list(range(1, 21))     # device22 null-map (indices 1..20)

    def _query(self) -> tuple:
        payload = {"gwId": self.device_id, "devId": self.device_id,
                   "uid": self.device_id, "t": str(int(time.time()))}
        cmd = _CONTROL_NEW if self.device22 else _DP_QUERY
        if self.device22:
            payload["dps"] = {str(i): None for i in self.dps_to_request}
        return cmd, _pack(0, cmd, encode_message(cmd, self.key, payload))

    def status(self) -> dict:
        _, frame = self._query()
        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(frame)
                for _ in range(_MAX_FRAMES):
                    head = _recv_exact(sock, 16)
                    prefix, _seq, _cmd, length = struct.unpack(_FRAME_HEAD, head)
                    if prefix != _PREFIX:
                        raise TuyaError("bad prefix")
                    if not 12 <= length <= _MAX_FRAME:
                        raise TuyaError(f"bad frame length {length}")
                    _seq, rcmd, retcode, region = _unpack(head + _recv_exact(sock, length))
                    if retcode != 0:
                        raise TuyaError(f"device retcode {retcode}")
                    if rcmd not in _ACCEPT_CMDS:
                        raise TuyaError(f"unexpected cmd {rcmd:#x}")
                    if not region:
                        continue                          # empty ACK — read the next frame
                    msg = decode_message(self.key, region)
                    if isinstance(msg, dict) and isinstance(msg.get("dps"), dict):
                        return msg["dps"]
                raise TuyaError("no dps-bearing response")
        except OSError as exc:
            raise TuyaError(f"io error ({exc})") from None
