#!/usr/bin/env python3
"""Completeness audit over a directory of pcaps: inventory every observed frame
``(type, cmd)`` pair, every TLV tag, every ``0x0046`` attribute prefix, and any
short/anomalous frames — so the catalog in ``docs/PROTOCOL.md`` can be kept
honest and reproducible.

    python3 tools/pcap_audit.py [captures/pcap]

Uses a per-flow `Deframer` (keyed by the TCP 5-tuple) so frames split across
segments are reassembled correctly.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hestia.protocol import FRAME_TYPES, Deframer, Frame  # noqa: E402
from tools.pcap_frames import packets, tcp_payload  # noqa: E402


def audit(pcap_dir: str) -> int:
    pcaps = sorted(Path(pcap_dir).glob("*.pcap"))
    if not pcaps:
        print(f"no pcaps in {pcap_dir}")
        return 1

    framecmd: Counter = Counter()
    framecmd_dir = defaultdict(set)
    tags_by_fc = defaultdict(Counter)
    tags: Counter = Counter()
    attr46: Counter = Counter()
    short: Counter = Counter()
    deframers: dict = {}

    for path in pcaps:
        for _ts, pkt in packets(str(path)):
            parsed = tcp_payload(pkt)
            if not parsed:
                continue
            src, sport, dst, dport, payload = parsed
            if 8925 not in (sport, dport) or not payload:
                continue
            df = deframers.setdefault((src, sport, dst, dport), Deframer())
            direction = "toSrv" if dport == 8925 else "frSrv"
            for body in df.feed(payload):
                if len(body) < 5:
                    if body:
                        text = "".join(chr(c) if 32 <= c < 127 else "." for c in body)
                        short[f"{body.hex()} |{text}|"] += 1
                    continue
                fr = Frame(body)
                fc = (fr.type, fr.cmd)
                framecmd[fc] += 1
                framecmd_dir[fc].add(direction)
                for tlv in fr.tlvs():
                    tags[tlv.tag] += 1
                    tags_by_fc[fc][tlv.tag] += 1
                    if tlv.tag == 0x0046 and tlv.value:
                        attr46[tlv.value[:2].hex()] += 1

    print(f"scanned {len(pcaps)} pcaps, {len(deframers)} flows\n")
    print("=== FRAME (type,cmd) ===")
    for (typ, cmd), n in sorted(framecmd.items()):
        name = FRAME_TYPES.get(typ, f"0x{typ:02x}")
        dirs = ",".join(sorted(framecmd_dir[(typ, cmd)]))
        toptags = " ".join(f"0x{t:04x}" for t, _ in tags_by_fc[(typ, cmd)].most_common(14))
        print(f"  [{name:>4} c{cmd:02x}] x{n:<6} {dirs:12} {toptags}")

    print("\n=== TLV tags (tag: count) ===")
    print("  " + "  ".join(f"0x{t:04x}:{n}" for t, n in sorted(tags.items())))

    print("\n=== 0x0046 attribute prefixes (first 2 bytes) ===")
    for prefix, n in attr46.most_common():
        print(f"  {prefix}  x{n}")

    print("\n=== short / anomalous frames (body < 5 bytes) ===")
    for s, n in short.most_common(20):
        print(f"  {s}  x{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(audit(sys.argv[1] if len(sys.argv) > 1 else "captures/pcap"))
