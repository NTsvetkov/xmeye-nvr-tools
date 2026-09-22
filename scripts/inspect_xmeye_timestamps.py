#!/usr/bin/env python3

import argparse
import struct
from collections import Counter
from datetime import datetime
from pathlib import Path


MAGIC_IFRAME = b"\x00\x00\x01\xFC"


def decode_datetime(raw: int) -> datetime:
    """Decode Xiongmai packed 32-bit DateTime."""
    second = raw & 0x3F
    minute = (raw >> 6) & 0x3F
    hour = (raw >> 12) & 0x1F
    day = (raw >> 17) & 0x1F
    month = (raw >> 22) & 0x0F
    year = 2000 + ((raw >> 26) & 0x3F)

    return datetime(year, month, day, hour, minute, second)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect XMeye/Xiongmai I-frame timestamps and FPS metadata."
    )
    parser.add_argument("input", type=Path)
    args = parser.parse_args()

    data = args.input.read_bytes()

    pos = 0
    records = []

    while True:
        pos = data.find(MAGIC_IFRAME, pos)
        if pos < 0:
            break

        if pos + 16 > len(data):
            break

        t = data[pos + 4]
        f = data[pos + 5]

        packed_datetime = struct.unpack_from("<I", data, pos + 8)[0]
        payload_len = struct.unpack_from("<I", data, pos + 12)[0]

        try:
            timestamp = decode_datetime(packed_datetime)
        except ValueError:
            pos += 4
            continue

        records.append(
            {
                "offset": pos,
                "timestamp": timestamp,
                "fps": f & 0x1F,
                "t": t,
                "f": f,
                "length": payload_len,
            }
        )

        pos += 16 + payload_len

    if not records:
        print("No valid I-frames found.")
        return

    print(f"I-frames found: {len(records)}")
    print(f"First timestamp: {records[0]['timestamp']}")
    print(f"Last timestamp:  {records[-1]['timestamp']}")
    print(
        "Covered time:   "
        f"{(records[-1]['timestamp'] - records[0]['timestamp']).total_seconds():.0f} s"
    )

    fps_counts = Counter(r["fps"] for r in records)

    print("\nFPS values in I-frame headers:")
    for fps, count in sorted(fps_counts.items()):
        print(f"  {fps:2d} fps: {count} I-frames")

    print("\nTimestamp gaps between consecutive I-frames:")

    gap_counts = Counter()

    for previous, current in zip(records, records[1:]):
        delta = int(
            (current["timestamp"] - previous["timestamp"]).total_seconds()
        )
        gap_counts[delta] += 1

    for gap, count in sorted(gap_counts.items()):
        print(f"  {gap:3d} s: {count}")

    print("\nFirst 20 I-frames:")
    for r in records[:20]:
        print(
            f"  {r['timestamp']}  "
            f"fps={r['fps']:2d}  "
            f"offset=0x{r['offset']:08X}"
        )

    print("\nFPS/timestamp changes:")
    previous = records[0]

    for current in records[1:]:
        gap = int(
            (current["timestamp"] - previous["timestamp"]).total_seconds()
        )

        if current["fps"] != previous["fps"] or gap > 5 or gap < 0:
            print(
                f"  {previous['timestamp']} -> {current['timestamp']} "
                f"gap={gap}s "
                f"fps={previous['fps']}->{current['fps']} "
                f"offset=0x{current['offset']:08X}"
            )

        previous = current


if __name__ == "__main__":
    main()
