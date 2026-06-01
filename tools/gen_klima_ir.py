#!/usr/bin/env python3
"""Generate a Flipper Zero `.ir` file of LG air-conditioner states by SYNTHESISING them from the
decoded protocol — instead of capturing each state by hand — and (optionally) upload it to the
Flipper SD card over RPC.

Standalone repo tool (NOT part of the `hestia` package, so it is outside the 100%-coverage gate);
it reuses the pure-stdlib protobuf/serial primitives from `hestia.flipper` for the upload.

LG A/C IR frame (the documented LG encoding for this model):
    28 bits = 0x88 (8-bit signature) | data (16 bits) | checksum (4 bits)
    data nibbles = [n3][n2][n1][n0]; checksum = (n3 + n2 + n1 + n0) & 0xF
    temperature C = n1 + 15
    n3 = 0 for a normal state, 0xC for the dedicated power-OFF command
Reference codes (round-tripped by the self-test below):
    OFF        = 0x88C0051  (n3=C, n2=0, n1=0, n0=5)
    ON (24 C)  = 0x880190A  (n3=0, n2=1, n1=9, n0=0)  -- the "Power" / resume-last-state frame

Before relying on the cool/heat table on a given unit, transmit ONE generated state at the A/C to
confirm it actuates as expected; if a mode/fan nibble differs for your model, fix the four
*_MODE / *_FAN constants and regenerate (temperature + checksum follow directly from the encoding).

Usage:
    python tools/gen_klima_ir.py --out tools/klima.ir
    python tools/gen_klima_ir.py --out tools/klima.ir --upload --dest /ext/infrared/klima.ir
"""
from __future__ import annotations

import argparse
import sys

# --- protocol -------------------------------------------------------------------------------------

_SIG = 0x88                       # 8-bit signature (high byte of the 28-bit frame)
_N3_NORMAL = 0x0
_N3_OFF = 0xC                     # dedicated power-off command nibble

# Per-mode mode nibble (n2 = 0x8 | LG mode code, where cool=0 dry=1 fan=2 auto=3 heat=4) and fan
# nibble (n0), per the documented LG encoding. The round-trip self-test pins each constant to its
# known code (Cool18=0x880834F, Heat30=0x880CF4F, Dry24=0x8809902, Auto21=0x880B645, Fan=0x880A341,
# off=0x88C0051) — see _self_test(). Temperature = n1 + 15.
COOL_MODE, COOL_FAN = 0x8, 0x4
DRY_MODE, DRY_FAN = 0x9, 0x0
FAN_MODE, FAN_FAN = 0xA, 0x4
AUTO_MODE, AUTO_FAN = 0xB, 0x4
HEAT_MODE, HEAT_FAN = 0xC, 0x4

# Power-ON-to-mode nibble = the LG mode code WITHOUT the 0x8 "adjust-a-running-unit" bit (n2 bit3 clear).
# Such a frame turns an OFF unit ON directly into that mode+temp in ONE shot — vs the set-mode frames
# above, which only adjust an already-running unit. Pinned to the "Power" frame
# (= power-on to dry@24 = 0x880190A, n2=1; see _self_test). Fan nibble matches the set-mode value.
COOL_ON, DRY_ON, FAN_ON, AUTO_ON, HEAT_ON = 0x0, 0x1, 0x2, 0x3, 0x4

# Timing template (microseconds): the header is non-standard (~3300/9700 for this LG model, rather
# than the canonical LG 9000/4500), so those values are used as-is. 38 kHz, 1/3 duty.
HEADER = (3300, 9727)
MARK = 513
SPACE0 = 514                      # space after a "0" bit
SPACE1 = 1540                     # space after a "1" bit
STOP = 500                        # trailing mark
FREQUENCY = 38000
DUTY = 0.330000


def encode(mode: int, temp_c: int, fan: int, n3: int = _N3_NORMAL) -> int:
    """The 28-bit LG frame for one state. ``temp_c`` is the setpoint in C (encoded as n1 = temp-15)."""
    n1 = temp_c - 15
    if not (0 <= n1 <= 0xF):
        raise ValueError(f"temperature {temp_c}C out of range (15..30)")
    nibbles = (n3, mode & 0xF, n1, fan & 0xF)
    data = (nibbles[0] << 12) | (nibbles[1] << 8) | (nibbles[2] << 4) | nibbles[3]
    cks = sum(nibbles) & 0xF
    return (_SIG << 20) | (data << 4) | cks


def to_timings(code: int) -> "list[int]":
    """Expand a 28-bit code to the raw mark/space sample list (header + 28 bits + stop = 59 samples)."""
    out = [HEADER[0], HEADER[1]]
    for i in range(27, -1, -1):                  # MSB first
        out.append(MARK)
        out.append(SPACE1 if (code >> i) & 1 else SPACE0)
    out.append(STOP)
    return out


def states(cool, heat, auto) -> "list[tuple[str, int]]":
    """The (name, code) list: the OFF frame plus one ``on_<mode>_<temp>`` program per requested
    temp/mode. The power-on (n2 bit3-clear) frame is IDEMPOTENT — validated on hardware: it turns an
    OFF unit on AND just re-programs a running one — so it is the single "set program" command the
    dashboard needs. (The redundant set-mode frame class and the unused dry/fan modes are no longer
    generated; their nibbles stay below as encoder self-test anchors.)"""
    out = [("off", encode(0, 15, 5, n3=_N3_OFF))]         # == 0x88C0051 (confirmed)
    # (label, power-on nibble, fan nibble, temps)
    table = [("cool", COOL_ON, COOL_FAN, cool),
             ("heat", HEAT_ON, HEAT_FAN, heat),
             ("auto", AUTO_ON, AUTO_FAN, auto)]
    for label, on_nib, fan, temps in table:
        for t in temps:
            out.append((f"on_{label}_{t}", encode(on_nib, t, fan)))
    return out


def render_ir(named: "list[tuple[str, int]]") -> str:
    """Render the Flipper `.ir` file text (raw signals)."""
    blocks = ["Filetype: IR signals file", "Version: 1"]
    for name, code in named:
        data = " ".join(str(x) for x in to_timings(code))
        blocks.append("#")
        blocks.append(f"name: {name}")
        blocks.append("type: raw")
        blocks.append(f"frequency: {FREQUENCY}")
        blocks.append(f"duty_cycle: {DUTY:.6f}")
        blocks.append(f"data: {data}")
    return "\n".join(blocks) + "\n"


# --- self-test (the encoder must reproduce the known LG reference codes) ---------------------------

def _self_test() -> None:
    # each (mode, fan) constant is pinned to its documented LG code, so the generated frames match the
    # known encoding (not guesses).
    assert encode(0, 15, 5, n3=_N3_OFF) == 0x88C0051, "OFF"
    assert encode(COOL_MODE, 18, COOL_FAN) == 0x880834F, "Cool18"
    assert encode(HEAT_MODE, 30, HEAT_FAN) == 0x880CF4F, "Heat30"
    assert encode(DRY_MODE, 24, DRY_FAN) == 0x8809902, "Dry24"
    assert encode(AUTO_MODE, 21, AUTO_FAN) == 0x880B645, "Auto21"
    assert encode(FAN_MODE, 18, FAN_FAN) == 0x880A341, "Fan"
    # power-on class pinned to the "Power" frame = power-on to dry@24 (n2 bit3 clear)
    assert encode(DRY_ON, 24, DRY_FAN) == 0x880190A, "on_dry_24 (power-on frame)"
    # the raw expansion must be the canonical 59-sample LG raw shape
    assert len(to_timings(0x880834F)) == 59


# --- upload (RPC StorageWrite, chunked; reuses hestia.flipper primitives) --------------------------

def upload(data: bytes, dest: str, device: str, chunk: int = 512) -> None:
    sys.path.insert(0, ".")
    from hestia import flipper as fl

    t = fl.SerialTransport(device)
    try:
        t.write(b"\x03"); fl._drain(t, 0.3)
        t.write(b"loader close\r"); fl._drain(t, 1.0)
        t.write(b"start_rpc_session\r"); fl._drain(t, 0.8)
        parts = [data[i:i + chunk] for i in range(0, len(data), chunk)] or [b""]
        for k, part in enumerate(parts):
            last = k == len(parts) - 1
            file_msg = fl._f_bytes(4, part)                       # File.data = field 4
            wreq = fl._f_str(1, dest) + fl._f_bytes(2, file_msg)  # WriteRequest{path=1, file=2}
            main = fl._f_varint(1, 1)                             # command_id (same for every chunk)
            if not last:
                main += fl._f_varint(3, 1)                        # has_next
            main += fl._f_bytes(11, wreq)                         # storage_write_request = field 11
            t.write(fl._varint(len(main)) + main)
        status = fl._await_status(t, 1, 8.0)                      # one response after the last chunk
        if status != 0:
            raise fl.FlipperError(f"storage write failed: status {status}")
    finally:
        t.close()


def _temp_range(spec: str) -> "list[int]":
    if not spec:
        return []
    lo, hi = (int(x) for x in spec.split("-")) if "-" in spec else (int(spec), int(spec))
    return list(range(lo, hi + 1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate (and optionally upload) an LG-A/C Flipper .ir file.")
    ap.add_argument("--out", default="tools/klima.ir", help="output .ir path")
    ap.add_argument("--cool", default="18-30", help="cool temp range, e.g. 18-30 (empty to skip)")
    ap.add_argument("--heat", default="16-30", help="heat temp range")
    ap.add_argument("--auto", default="18-30", help="auto temp range (power-on nibble inferred from the protocol)")
    ap.add_argument("--upload", action="store_true", help="also upload via Flipper RPC StorageWrite")
    ap.add_argument("--device", default="/dev/ttyACM0", help="Flipper serial device")
    ap.add_argument("--dest", default="/ext/infrared/klima.ir", help="destination path on the Flipper SD")
    args = ap.parse_args(argv)

    _self_test()
    named = states(_temp_range(args.cool), _temp_range(args.heat), _temp_range(args.auto))
    text = render_ir(named)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {len(named)} signals -> {args.out} ({len(text)} bytes)")
    print("  signals:", ", ".join(name for name, _ in named))
    if args.upload:
        upload(text.encode("utf-8"), args.dest, args.device)
        print(f"uploaded -> {args.device}:{args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
