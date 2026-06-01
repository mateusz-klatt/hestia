#!/usr/bin/env python3
"""Decode captured Keemple ``0x7e`` streams and print the frames.

Each input file should be one direction of one TCP conversation (the raw
reassembled bytes). Produce them with tcpflow, e.g.::

    tcpflow -r captures/pcap/8925-*.pcap -o /tmp/flows port 8925
    python3 tools/decode_stream.py /tmp/flows/*

tcpflow names files ``srcIP.sport-dstIP.dport`` — so the filename tells you who
is talking.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hestia.protocol import Frame, iter_frames  # noqa: E402


def main(argv: "list[str]") -> int:
    if not argv:
        print(__doc__)
        return 1
    for arg in argv:
        path = Path(arg)
        if not path.is_file() or path.suffix == ".xml":  # skip tcpflow report.xml
            continue
        data = path.read_bytes()
        print(f"\n===== {path.name}  ({len(data)} B) =====")
        for index, body in enumerate(iter_frames(data)):
            print(f"--- frame {index} ---")
            print(Frame(body))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
