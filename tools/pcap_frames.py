#!/usr/bin/env python3
"""Read a pcap, extract ``0x7e`` frames on ``:8925`` WITH timestamps, print
compactly. Lets us correlate decoded frames against the app's Activity log,
which labels every event with a precise time.

    python3 tools/pcap_frames.py captures/pcap/8925-*.pcap [HH:MM:SS-substr ...]

With no time filters it prints every frame; otherwise only frames whose
timestamp contains one of the given substrings (e.g. ``16:27:5``).
"""
from __future__ import annotations

import struct
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hestia.protocol import FRAME_TYPES, Frame, iter_frames  # noqa: E402

NAMES = {"192.0.2.6": "GW.6", "192.0.2.17": "DEV.17", "1.2.3.4": "CLOUD"}


def packets(path: str):
    data = Path(path).read_bytes()
    magic = struct.unpack("<I", data[:4])[0]
    end = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    nano = magic == 0xA1B23C4D
    off, n = 24, len(data)
    while off + 16 <= n:
        ts_s, ts_f, incl, _orig = struct.unpack(end + "IIII", data[off : off + 16])
        off += 16
        yield ts_s + ts_f / (1e9 if nano else 1e6), data[off : off + incl]
        off += incl


def tcp_payload(pkt: bytes):
    if len(pkt) < 14:
        return None
    etype = struct.unpack("!H", pkt[12:14])[0]
    o = 14
    if etype == 0x8100:  # VLAN tag
        etype = struct.unpack("!H", pkt[16:18])[0]
        o = 18
    if etype != 0x0800:  # IPv4 only
        return None
    ip = pkt[o:]
    if len(ip) < 20 or ip[9] != 6:  # TCP only
        return None
    ihl = (ip[0] & 0x0F) * 4
    src = ".".join(map(str, ip[12:16]))
    dst = ".".join(map(str, ip[16:20]))
    tcp = ip[ihl:]
    if len(tcp) < 20:
        return None
    sport, dport = struct.unpack("!HH", tcp[:4])
    doff = (tcp[12] >> 4) * 4
    return src, sport, dst, dport, tcp[doff:]


def main(argv: "list[str]") -> int:
    if not argv:
        print(__doc__)
        return 1
    path, filters = argv[0], argv[1:]
    for ts, pkt in packets(path):
        parsed = tcp_payload(pkt)
        if not parsed:
            continue
        src, sport, dst, dport, payload = parsed
        if 8925 not in (sport, dport) or not payload:
            continue
        tstr = datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
        if filters and not any(f in tstr for f in filters):
            continue
        a, b = NAMES.get(src, src), NAMES.get(dst, dst)
        # NB: iter_frames runs per TCP segment here, so a frame split across two
        # segments is dropped (fine for a quick timeline view). pcap_audit.py keeps a
        # Deframer per flow and reassembles — use it when completeness matters.
        for body in iter_frames(payload):
            if len(body) < 4:
                continue
            fr = Frame(body)
            tn = FRAME_TYPES.get(fr.type, f"0x{fr.type:02x}")
            tags = " ".join(f"0x{t.tag:04x}={t.value.hex()}" for t in fr.tlvs())
            print(f"{tstr} {a:>6}->{b:<6} [{tn} c{fr.cmd:02x}] {tags}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
