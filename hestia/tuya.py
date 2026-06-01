"""Minimal, stdlib-only Tuya **protocol v3.3** local client.

Reads a Tuya v3.3 device's data points (DPS) over the LAN (TCP 6668) with **no cloud** and **no
third-party dependency** — the AES-128-ECB primitive the protocol needs is implemented here in pure
Python (validated against FIPS-197 test vectors). Built for the Neno baby monitor, which is a Tuya
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


class TuyaError(Exception):
    """Any Tuya protocol / framing / crypto / I-O failure (the caller never sees a raw exception)."""


# --- AES-128 (pure Python; S-box computed at import, pinned by FIPS-197 tests) ----------------

def _gmul(a: int, b: int) -> int:
    """Multiply in GF(2^8) with the AES reduction polynomial 0x11B."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox() -> list:
    inv = [0] * 256
    for i in range(1, 256):
        for j in range(i, 256):
            if _gmul(i, j) == 1:
                inv[i] = j
                inv[j] = i
                break
    return [x ^ _rotl8(x, 1) ^ _rotl8(x, 2) ^ _rotl8(x, 3) ^ _rotl8(x, 4) ^ 0x63
            for x in inv]


_SBOX = _build_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _key_expansion(key: bytes) -> list:
    """16-byte key -> 11 round keys (each a 16-byte list)."""
    w = list(key)
    for i in range(16, 176, 4):
        t = w[i - 4:i]
        if i % 16 == 0:
            t = [_SBOX[b] for b in (t[1], t[2], t[3], t[0])]   # RotWord + SubWord
            t[0] ^= _RCON[i // 16 - 1]
        w.extend(w[i - 16 + j] ^ t[j] for j in range(4))
    return [w[r * 16:(r + 1) * 16] for r in range(11)]


def _shift_rows(s, inv=False):
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            src = (c - r) % 4 if inv else (c + r) % 4
            out[r + 4 * c] = s[r + 4 * src]
    return out


_MIX = (2, 3, 1, 1)
_INV_MIX = (14, 11, 13, 9)


def _mix_columns(s, coeffs):
    out = [0] * 16
    for c in range(4):
        col = s[4 * c:4 * c + 4]
        for r in range(4):
            out[4 * c + r] = (_gmul(col[0], coeffs[(0 - r) % 4]) ^ _gmul(col[1], coeffs[(1 - r) % 4])
                              ^ _gmul(col[2], coeffs[(2 - r) % 4]) ^ _gmul(col[3], coeffs[(3 - r) % 4]))
    return out


def _encrypt_block(s, rk):
    s = [s[i] ^ rk[0][i] for i in range(16)]
    for r in range(1, 10):
        s = [_SBOX[b] for b in s]
        s = _shift_rows(s)
        s = _mix_columns(s, _MIX)
        s = [s[i] ^ rk[r][i] for i in range(16)]
    s = [_SBOX[b] for b in s]
    s = _shift_rows(s)
    return bytes(s[i] ^ rk[10][i] for i in range(16))


def _decrypt_block(s, rk):
    s = [s[i] ^ rk[10][i] for i in range(16)]
    for r in range(9, 0, -1):
        s = _shift_rows(s, inv=True)
        s = [_INV_SBOX[b] for b in s]
        s = [s[i] ^ rk[r][i] for i in range(16)]
        s = _mix_columns(s, _INV_MIX)
    s = _shift_rows(s, inv=True)
    s = [_INV_SBOX[b] for b in s]
    return bytes(s[i] ^ rk[0][i] for i in range(16))


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
    _check_aes_args(data, key)
    rk = _key_expansion(key)
    return b"".join(_encrypt_block(list(data[i:i + 16]), rk) for i in range(0, len(data), 16))


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    _check_aes_args(data, key)
    rk = _key_expansion(key)
    return b"".join(_decrypt_block(list(data[i:i + 16]), rk) for i in range(0, len(data), 16))


# --- Tuya v3.3 framing + message crypto -------------------------------------------------------

def _pack(seq: int, cmd: int, payload: bytes) -> bytes:
    """Build a frame. ``payload`` is the region between the length field and the crc (for a command:
    the encoded message; the 4-byte return code, if any, must already be inside it)."""
    head = struct.pack(">IIII", _PREFIX, seq, cmd, len(payload) + 8)
    body = head + payload
    return body + struct.pack(">II", binascii.crc32(body), _SUFFIX)


def _unpack(buf: bytes):
    """Parse a device→app frame -> ``(seq, cmd, retcode, payload)`` (4-byte retcode stripped)."""
    if len(buf) < 24:
        raise TuyaError("frame too short")
    prefix, seq, cmd, length = struct.unpack(">IIII", buf[:16])
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
    except (ValueError, UnicodeDecodeError) as exc:
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
        cmd, frame = self._query()
        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(frame)
                for _ in range(_MAX_FRAMES):
                    head = _recv_exact(sock, 16)
                    prefix, _seq, _cmd, length = struct.unpack(">IIII", head)
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
