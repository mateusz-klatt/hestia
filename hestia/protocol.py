"""Keemple device protocol — the cleartext, ``0x7e``-framed binary protocol the
devices speak to their cloud (Alibaba ``:8925``) and, we believe, to a local
gateway (``:8925``) too.

Frame layout (per ``docs/PROTOCOL.md`` §1)::

    0x7e  <type:1> <cmd:1> <length:2 BE> <payload:length> <checksum:1>  0x7e

* ``checksum`` = XOR of every byte from ``type`` through the end of ``payload``
  (per ``docs/PROTOCOL.md`` §1).
* ``payload`` is a sequence of TLVs: ``<tag:2 BE> <length:2 BE> <value:length>``.
* ``0x7e`` is the frame delimiter but is **not** escaped: it also occurs as an
  ordinary data byte (~1 frame in 7) inside the payload, epoch, or checksum. So
  deframing must be driven by ``length`` + ``checksum``, not by naively splitting
  on ``0x7e`` (which shatters such frames into bad-checksum fragments) — see
  `Deframer`. ``0x7d`` is *not* an escape either (no byte-stuffing; per
  ``docs/PROTOCOL.md`` §1). Frame ``type`` is never ``0x7e`` (reserved as the delimiter).

Frame types seen: ``0x64`` 'd', ``0x66`` 'f', ``0x67`` 'g', ``0x1e``. Notable
TLV tags from the handshake: ``0x0002`` device serial, ``0x0004`` firmware,
``0x0064`` session UUID, ``0x0066`` timestamp string, ``0x0068`` epoch,
``0x0065``/``0x004c`` 16-byte tokens.
"""
from __future__ import annotations

from dataclasses import dataclass

FLAG = 0x7E  # frame delimiter

FRAME_TYPES = {0x64: "d", 0x66: "f", 0x67: "g", 0x1E: "1e"}
_WAIT = object()
_RETRY = object()


def xor_checksum(data: bytes) -> int:
    """The frame checksum: XOR of ``type | cmd | len | payload`` (no flags)."""
    x = 0
    for b in data:
        x ^= b
    return x


class Deframer:
    """Stateful, length-aware ``0x7e`` deframer for a streaming connection (one per
    socket). TCP delivers arbitrary chunks; this buffers across reads and yields
    whole frame bodies (``type | cmd | len | payload | checksum``).

    ``0x7e`` is the frame delimiter but is **not** escaped — it also occurs as an
    ordinary data byte inside the payload, epoch, or checksum (~1 frame in 7). So a
    flag is only a boundary *hint*: the 4-byte header is read by slicing (a ``0x7e``
    in ``type``/``cmd``/``length`` is data), the body size comes from the header's
    ``length``, and a frame is accepted only once its XOR checksum validates. On a
    checksum miss — or an implausible length — we re-hunt to the next flag. After
    every accepted frame we drop back to hunting for the next start flag, so a stray
    byte between a frame's checksum and the next flag is skipped cleanly. (Frame
    ``type`` is never ``0x7e`` — reserved as the delimiter; per ``docs/PROTOCOL.md`` §1.)
    """

    # Observed real max declared length = 132; 2048 keeps ~15x margin while also
    # bounding the desync-swallow window — a 1/256 false-accept can consume at most
    # ~2053 bytes before the next re-hunt. Do NOT raise it to 0xffff "to be safe".
    MAX_PAYLOAD = 2048

    def __init__(self) -> None:
        self._buf = bytearray()
        self._synced = False

    def _sync_to_flag(self) -> bool:
        fi = self._buf.find(FLAG)
        if fi < 0:
            self._buf.clear()
            return False
        del self._buf[: fi + 1]
        self._synced = True
        return True

    def _drop_idle_flags(self) -> None:
        i = 0
        while i < len(self._buf) and self._buf[i] == FLAG:  # skip idle / double flags
            i += 1
        if i:
            del self._buf[:i]

    def _next_body(self):
        if not self._synced and not self._sync_to_flag():    # hunt for a start flag
            return _WAIT
        self._drop_idle_flags()
        if len(self._buf) < 4:                               # need the 4-byte header
            return _WAIT
        length = int.from_bytes(self._buf[2:4], "big")       # header read by slicing
        if length > self.MAX_PAYLOAD:                        # implausible -> re-hunt
            self._synced = False
            return _RETRY
        total = 5 + length                                   # type+cmd+len(2)+payload+checksum
        if len(self._buf) < total:                           # wait for the rest
            return _WAIT
        body = bytes(self._buf[:total])
        if not Frame(body).checksum_ok:                      # length + checksum authoritative
            self._synced = False                             # misaligned -> re-hunt the next flag
            return _RETRY
        del self._buf[:total]
        self._synced = False                                 # accept; re-hunt the next start flag
        return body

    def feed(self, data: bytes):
        self._buf += data
        while True:
            body = self._next_body()
            if body is _WAIT:
                return
            if body is _RETRY:
                continue
            yield body


def iter_frames(stream: bytes):
    """Yield complete frame bodies from a one-shot byte string — a thin wrapper over
    `Deframer` so the offline tools and the live transports share one length-aware
    codec. Any trailing incomplete frame is discarded."""
    yield from Deframer().feed(stream)


def build_frame(ftype: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a complete, checksummed, flag-wrapped frame ready to send."""
    core = bytes([ftype, cmd]) + len(payload).to_bytes(2, "big") + payload
    return bytes([FLAG]) + core + bytes([xor_checksum(core)]) + bytes([FLAG])


def tlv(tag: int, value: bytes) -> bytes:
    """Encode one TLV: ``tag(2 BE) | len(2 BE) | value``."""
    return tag.to_bytes(2, "big") + len(value).to_bytes(2, "big") + value


@dataclass
class TLV:
    tag: int
    value: bytes

    @property
    def text(self) -> str:
        return "".join(chr(c) if 32 <= c < 127 else "." for c in self.value)

    def __str__(self) -> str:
        return f"0x{self.tag:04x} [{len(self.value):>3}] {self.value.hex()}  |{self.text}|"


def parse_tlvs(payload: bytes) -> "list[TLV]":
    """Parse a payload into TLVs: ``tag(2 BE) | len(2 BE) | value``."""
    out: list[TLV] = []
    i, n = 0, len(payload)
    while i + 4 <= n:
        tag = int.from_bytes(payload[i : i + 2], "big")
        length = int.from_bytes(payload[i + 2 : i + 4], "big")
        if i + 4 + length > n:
            break
        out.append(TLV(tag, payload[i + 4 : i + 4 + length]))
        i += 4 + length
    return out


@dataclass
class Frame:
    body: bytes  # raw bytes between the 0x7e flags

    @property
    def type(self) -> int:
        return self.body[0]

    @property
    def cmd(self) -> int:
        return self.body[1]

    @property
    def length(self) -> int:
        return int.from_bytes(self.body[2:4], "big")

    @property
    def core(self) -> bytes:
        """type | cmd | len | payload (the bytes the checksum covers)."""
        return self.body[: 4 + self.length]

    @property
    def payload(self) -> bytes:
        return self.body[4 : 4 + self.length]

    @property
    def stored_checksum(self) -> int:
        idx = 4 + self.length
        return self.body[idx] if idx < len(self.body) else -1

    @property
    def computed_checksum(self) -> int:
        return xor_checksum(self.core)

    @property
    def checksum_ok(self) -> bool:
        return self.stored_checksum == self.computed_checksum

    def tlvs(self) -> "list[TLV]":
        return parse_tlvs(self.payload)

    def __str__(self) -> str:
        if len(self.body) < 4:
            return f"[short {len(self.body)}B] {self.body.hex()}"
        type_name = FRAME_TYPES.get(self.type, f"0x{self.type:02x}")
        mark = "ok" if self.checksum_ok else f"BAD got {self.computed_checksum:#04x}"
        lines = [
            f"[{type_name} cmd=0x{self.cmd:02x}] len={self.length} "
            f"cksum={self.stored_checksum:#04x} ({mark})"
        ]
        tlvs = self.tlvs()
        for t in tlvs:
            lines.append("    " + str(t))
        consumed = sum(4 + len(t.value) for t in tlvs)
        leftover = self.payload[consumed:]
        if leftover:
            lines.append(f"    (+{len(leftover)}B unparsed) {leftover.hex()}")
        return "\n".join(lines)
