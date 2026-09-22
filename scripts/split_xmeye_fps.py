#!/usr/bin/env python3

import argparse
import struct
from pathlib import Path


PREFIX = b"\x00\x00\x01"

TYPE_IFRAME = 0xFC
TYPE_PFRAME = 0xFD
TYPE_AUDIO = 0xFA


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output_prefix", type=Path)
    args = parser.parse_args()

    data = args.input.read_bytes()

    pos = 0
    current_f = None
    current_out = bytearray()
    segment_no = 0

    iframe_count = 0
    pframe_count = 0

    def flush_segment():
        nonlocal current_out, segment_no

        if not current_out:
            return

        filename = Path(
            f"{args.output_prefix}-seg{segment_no:02d}-f{current_f}.h264"
        )

        filename.write_bytes(current_out)

        print(
            f"Written {filename}: "
            f"{len(current_out):,} bytes"
        )

        current_out = bytearray()
        segment_no += 1

    while pos + 8 <= len(data):
        if data[pos:pos + 3] != PREFIX:
            pos += 1
            continue

        frame_type = data[pos + 3]

        if frame_type == TYPE_IFRAME:
            if pos + 16 > len(data):
                break

            f = data[pos + 5]

            payload_len = struct.unpack_from("<I", data, pos + 12)[0]
            payload_start = pos + 16
            payload_end = payload_start + payload_len

            if payload_end > len(data):
                break

            # Start a new segment when the stream FPS/mode marker changes.
            if current_f is None:
                current_f = f

            elif f != current_f:
                print(
                    f"FPS/mode change: {current_f} -> {f} "
                    f"at input offset 0x{pos:X}"
                )

                flush_segment()
                current_f = f

            current_out.extend(data[payload_start:payload_end])
            iframe_count += 1
            pos = payload_end
            continue

        if frame_type == TYPE_PFRAME:
            payload_len = struct.unpack_from("<I", data, pos + 4)[0]
            payload_start = pos + 8
            payload_end = payload_start + payload_len

            if payload_end > len(data):
                break

            current_out.extend(data[payload_start:payload_end])
            pframe_count += 1
            pos = payload_end
            continue

        if frame_type == TYPE_AUDIO:
            payload_len = struct.unpack_from("<H", data, pos + 6)[0]
            pos += 8 + payload_len
            continue

        pos += 4

    flush_segment()

    print()
    print(f"I-frames: {iframe_count}")
    print(f"P-frames: {pframe_count}")


if __name__ == "__main__":
    main()
