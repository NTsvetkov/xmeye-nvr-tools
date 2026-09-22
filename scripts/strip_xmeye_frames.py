#!/usr/bin/env python3

import argparse
import struct
from pathlib import Path


MAGIC_PREFIX = b"\x00\x00\x01"

TYPE_IFRAME = 0xFC
TYPE_PFRAME = 0xFD
TYPE_SNAPSHOT = 0xFE
TYPE_AUDIO = 0xFA
TYPE_INFO = 0xF9


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract raw H.264/H.265 video from Xiongmai/XMeye frame container."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    data = args.input.read_bytes()
    size = len(data)

    pos = 0
    out = bytearray()

    counts = {
        "iframe": 0,
        "pframe": 0,
        "snapshot": 0,
        "audio": 0,
        "info": 0,
        "unknown": 0,
        "skipped_bytes": 0,
    }

    first_iframe_info = None

    while pos + 8 <= size:
        # All XMeye media frames start with 00 00 01 <type>.
        if data[pos:pos + 3] != MAGIC_PREFIX:
            pos += 1
            counts["skipped_bytes"] += 1
            continue

        frame_type = data[pos + 3]

        # I-frame / snapshot: 16-byte header.
        if frame_type in (TYPE_IFRAME, TYPE_SNAPSHOT):
            if pos + 16 > size:
                break

            t = data[pos + 4]
            fps_byte = data[pos + 5]

            payload_len = struct.unpack_from("<I", data, pos + 12)[0]
            payload_start = pos + 16
            payload_end = payload_start + payload_len

            if payload_end > size:
                print(
                    f"Truncated keyframe at 0x{pos:X}: "
                    f"payload={payload_len:,}, "
                    f"available={size - payload_start:,}"
                )
                break

            if first_iframe_info is None:
                codec_id = t & 0x0F
                encoded_fps = fps_byte & 0x1F

                first_iframe_info = {
                    "codec_id": codec_id,
                    "fps": encoded_fps,
                    "t": t,
                    "f": fps_byte,
                }

            out.extend(data[payload_start:payload_end])

            if frame_type == TYPE_IFRAME:
                counts["iframe"] += 1
            else:
                counts["snapshot"] += 1

            pos = payload_end
            continue

        # P-frame: 8-byte header, LE32 length.
        if frame_type == TYPE_PFRAME:
            payload_len = struct.unpack_from("<I", data, pos + 4)[0]
            payload_start = pos + 8
            payload_end = payload_start + payload_len

            if payload_end > size:
                print(
                    f"Truncated P-frame at 0x{pos:X}: "
                    f"payload={payload_len:,}, "
                    f"available={size - payload_start:,}"
                )
                break

            out.extend(data[payload_start:payload_end])
            counts["pframe"] += 1
            pos = payload_end
            continue

        # Audio and information frames:
        # 8-byte header, payload size is LE16 at offset +6.
        if frame_type in (TYPE_AUDIO, TYPE_INFO):
            payload_len = struct.unpack_from("<H", data, pos + 6)[0]
            payload_start = pos + 8
            payload_end = payload_start + payload_len

            if payload_end > size:
                print(
                    f"Truncated auxiliary frame at 0x{pos:X}: "
                    f"type=0x{frame_type:02X}, "
                    f"payload={payload_len:,}"
                )
                break

            if frame_type == TYPE_AUDIO:
                counts["audio"] += 1
            else:
                counts["info"] += 1

            pos = payload_end
            continue

        # We encountered 00 00 01 xx, but xx is not a known XMeye
        # container frame type. It may simply be an H.264 start code
        # encountered while recovering from an unknown region.
        counts["unknown"] += 1
        pos += 4

    args.output.write_bytes(out)

    print()
    print(f"Input size:       {size:,} bytes")
    print(f"Output size:      {len(out):,} bytes")
    print(f"I-frames:         {counts['iframe']}")
    print(f"P-frames:         {counts['pframe']}")
    print(f"Snapshots:        {counts['snapshot']}")
    print(f"Video frames:     {counts['iframe'] + counts['pframe']}")
    print(f"Audio frames:     {counts['audio']}")
    print(f"Info frames:      {counts['info']}")
    print(f"Unknown markers:  {counts['unknown']}")
    print(f"Skipped bytes:    {counts['skipped_bytes']:,}")

    if first_iframe_info:
        codec_names = {
            0x01: "MPEG-4",
            0x02: "H.264",
            0x03: "H.265",
        }

        codec_id = first_iframe_info["codec_id"]

        print()
        print("First keyframe metadata:")
        print(
            f"  Codec:          "
            f"{codec_names.get(codec_id, f'unknown 0x{codec_id:02X}')}"
        )
        print(f"  Encoded FPS:    {first_iframe_info['fps']}")
        print(
            f"  T/F bytes:      "
            f"0x{first_iframe_info['t']:02X} "
            f"0x{first_iframe_info['f']:02X}"
        )


if __name__ == "__main__":
    main()
