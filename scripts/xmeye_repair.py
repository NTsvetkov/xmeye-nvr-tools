#!/usr/bin/env python3

import argparse
import mmap
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable


MAGIC_PREFIX = b"\x00\x00\x01"

TYPE_INFO = 0xF9
TYPE_AUDIO = 0xFA
TYPE_IFRAME = 0xFC
TYPE_PFRAME = 0xFD
TYPE_SNAPSHOT = 0xFE

KNOWN_MARKERS = tuple(
    MAGIC_PREFIX + bytes((frame_type,))
    for frame_type in (
        TYPE_INFO,
        TYPE_AUDIO,
        TYPE_IFRAME,
        TYPE_PFRAME,
        TYPE_SNAPSHOT,
    )
)

MAX_RECORD_BYTES = 64 * 1024 * 1024
RESYNC_CONFIRM_RECORDS = 3


@dataclass(frozen=True)
class SanitizeStats:
    input_bytes: int
    output_bytes: int
    frame_count: int
    skipped_bytes: int
    skipped_ranges: int


def find_next_container_frame(
    data: mmap.mmap,
    start: int,
) -> int:
    candidates = []

    for marker in KNOWN_MARKERS:
        pos = data.find(marker, start)

        if pos != -1:
            candidates.append(pos)

    if not candidates:
        return -1

    return min(candidates)


def frame_end(
    data: mmap.mmap,
    pos: int,
    size: int,
) -> tuple[int | None, str | None]:
    """
    Return (end_offset, error_message).

    end_offset is the first byte after the complete frame.
    If the frame is truncated, end_offset is None.
    """
    if pos + 8 > size:
        return None, (
            f"Truncated frame header at 0x{pos:X}"
        )

    if data[pos:pos + 3] != MAGIC_PREFIX:
        return None, (
            f"Invalid frame marker at 0x{pos:X}"
        )

    frame_type = data[pos + 3]

    if frame_type == TYPE_IFRAME:
        if pos + 16 > size:
            return None, (
                f"Truncated I-frame header at 0x{pos:X}"
            )

        payload_len = struct.unpack_from(
            "<I",
            data,
            pos + 12,
        )[0]

        end = pos + 16 + payload_len

        if end > size:
            return None, (
                f"Truncated I-frame at 0x{pos:X}"
            )

        return end, None

    if frame_type == TYPE_PFRAME:
        payload_len = struct.unpack_from(
            "<I",
            data,
            pos + 4,
        )[0]

        end = pos + 8 + payload_len

        if end > size:
            return None, (
                f"Truncated P-frame at 0x{pos:X}"
            )

        return end, None

    if frame_type in (
        TYPE_AUDIO,
        TYPE_INFO,
    ):
        payload_len = struct.unpack_from(
            "<H",
            data,
            pos + 6,
        )[0]

        end = pos + 8 + payload_len

        if end > size:
            label = (
                "audio"
                if frame_type == TYPE_AUDIO
                else "info"
            )

            return None, (
                f"Truncated {label} frame "
                f"at 0x{pos:X}"
            )

        return end, None

    if frame_type == TYPE_SNAPSHOT:
        if pos + 16 > size:
            return None, (
                f"Truncated snapshot header at 0x{pos:X}"
            )

        payload_len = struct.unpack_from(
            "<I",
            data,
            pos + 12,
        )[0]

        end = pos + 16 + payload_len

        if end > size:
            return None, (
                f"Truncated snapshot at 0x{pos:X}"
            )

        return end, None

    return None, (
        f"Unknown XMEye frame type "
        f"0x{frame_type:02X} at 0x{pos:X}"
    )


def record_size(data, pos: int = 0) -> tuple[int | None, str]:
    """Return a complete record size, or why it cannot be returned yet."""
    available = len(data) - pos
    if available < 4:
        return None, "incomplete"
    if data[pos:pos + 3] != MAGIC_PREFIX or data[pos:pos + 4] not in KNOWN_MARKERS:
        return None, "invalid"

    frame_type = data[pos + 3]
    header_size = 16 if frame_type in (TYPE_IFRAME, TYPE_SNAPSHOT) else 8
    if available < header_size:
        return None, "incomplete"

    if frame_type in (TYPE_IFRAME, TYPE_SNAPSHOT):
        payload_len = struct.unpack_from("<I", data, pos + 12)[0]
    elif frame_type == TYPE_PFRAME:
        payload_len = struct.unpack_from("<I", data, pos + 4)[0]
    else:
        payload_len = struct.unpack_from("<H", data, pos + 6)[0]

    total = header_size + payload_len
    if total > MAX_RECORD_BYTES:
        return None, "invalid"
    if available < total:
        return None, "incomplete"
    return total, "complete"


class XMEyeRecordSanitizer:
    """Write only complete XMEye records from an arbitrarily chunked stream."""

    def __init__(
        self,
        output: BinaryIO,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.output = output
        self.log = log or (lambda _message: None)
        self.buffer = bytearray()
        self.buffer_offset = 0
        self.input_bytes = 0
        self.output_bytes = 0
        self.frame_count = 0
        self.skipped_bytes = 0
        self.skipped_ranges = 0
        self._gap_offset: int | None = None
        self._gap_size = 0
        self._gap_preview = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.buffer.extend(chunk)
        self.input_bytes += len(chunk)
        self._process(eof=False)

    def finish(self) -> SanitizeStats:
        self._process(eof=True)
        if self.buffer:
            self._discard_bad(len(self.buffer))
        self._close_gap()
        return SanitizeStats(
            self.input_bytes,
            self.output_bytes,
            self.frame_count,
            self.skipped_bytes,
            self.skipped_ranges,
        )

    def _process(self, eof: bool) -> None:
        while self.buffer:
            size, state = record_size(self.buffer)
            if state == "complete":
                self._close_gap()
                self.output.write(self.buffer[:size])
                del self.buffer[:size]
                self.buffer_offset += size
                self.output_bytes += size
                self.frame_count += 1
                continue
            if state == "incomplete" and not eof:
                return
            if state == "incomplete" and eof:
                self._discard_bad(len(self.buffer))
                return

            candidate, pending = self._find_resync(eof)
            if candidate is not None:
                self._discard_bad(candidate)
                continue
            if pending is not None:
                # Keep the invalid prefix in front of an unconfirmed marker.
                # Otherwise the next feed would see that marker at offset zero
                # and accept it as a normal record without the resync lookahead.
                return
            keep = 0 if eof else min(3, len(self.buffer))
            self._discard_bad(len(self.buffer) - keep)
            if eof:
                return

    def _find_resync(self, eof: bool) -> tuple[int | None, int | None]:
        search = 1
        pending: int | None = None
        while search < len(self.buffer):
            hits = [
                self.buffer.find(marker, search)
                for marker in KNOWN_MARKERS
            ]
            hits = [hit for hit in hits if hit >= 0]
            if not hits:
                break
            candidate = min(hits)
            verdict = self._probe_candidate(candidate, eof)
            if verdict == "valid":
                return candidate, None
            if verdict == "pending":
                pending = candidate
                break
            search = candidate + 1
        return None, pending

    def _probe_candidate(self, pos: int, eof: bool) -> str:
        records = 0
        cursor = pos
        while records < RESYNC_CONFIRM_RECORDS:
            size, state = record_size(self.buffer, cursor)
            if state == "complete":
                records += 1
                cursor += size
                continue
            if state == "invalid":
                return "invalid"
            if eof:
                return "valid" if records else "invalid"
            return "pending"
        return "valid"

    def _discard_bad(self, count: int) -> None:
        if count <= 0:
            return
        if self._gap_offset is None:
            self._gap_offset = self.buffer_offset
        take = min(16 - len(self._gap_preview), count)
        if take > 0:
            self._gap_preview.extend(self.buffer[:take])
        del self.buffer[:count]
        self.buffer_offset += count
        self._gap_size += count
        self.skipped_bytes += count

    def _close_gap(self) -> None:
        if self._gap_offset is None:
            return
        self.skipped_ranges += 1
        preview = bytes(self._gap_preview).hex(" ")
        self.log(
            f"Skipped invalid data at 0x{self._gap_offset:X}: "
            f"{self._gap_size:,} byte(s); first bytes: {preview}"
        )
        self._gap_offset = None
        self._gap_size = 0
        self._gap_preview.clear()


def sanitize_stream(
    source: BinaryIO,
    target: BinaryIO,
    log: Callable[[str], None] | None = None,
    cancel_event=None,
) -> SanitizeStats:
    sanitizer = XMEyeRecordSanitizer(target, log)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Repair cancelled.")
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        sanitizer.feed(chunk)
    return sanitizer.finish()


def sanitize_file(
    input_path: Path,
    output_path: Path,
    log: Callable[[str], None] | None = print,
    cancel_event=None,
) -> SanitizeStats:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with input_path.open("rb") as source, output_path.open("wb") as target:
            return sanitize_stream(source, target, log, cancel_event)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def inspect_valid_prefix(
    input_path: Path,
) -> tuple[int, int, int, str | None]:
    """
    Return:
        valid_end
        valid_frame_count
        skipped_bytes
        problem_message

    valid_end is the first byte after the last complete frame.
    """
    valid_end = 0
    valid_frames = 0
    skipped_bytes = 0
    problem_message = None

    with input_path.open("rb") as source:
        with mmap.mmap(
            source.fileno(),
            length=0,
            access=mmap.ACCESS_READ,
        ) as data:
            size = len(data)
            pos = 0

            while pos < size:
                if pos + 4 > size:
                    problem_message = (
                        f"Trailing {size - pos} byte(s) "
                        f"after last complete frame "
                        f"at 0x{pos:X}"
                    )
                    break

                if data[pos:pos + 3] != MAGIC_PREFIX:
                    next_pos = find_next_container_frame(
                        data,
                        pos + 1,
                    )

                    if next_pos == -1:
                        problem_message = (
                            f"No further XMEye frame marker "
                            f"after 0x{pos:X}"
                        )
                        break

                    skipped_bytes += (
                        next_pos - pos
                    )

                    pos = next_pos
                    continue

                end, error = frame_end(
                    data,
                    pos,
                    size,
                )

                if error is not None:
                    problem_message = error
                    break

                valid_end = end
                valid_frames += 1
                pos = end

            if (
                problem_message is None
                and valid_end < size
            ):
                problem_message = (
                    f"Trailing {size - valid_end} byte(s) "
                    "after last complete frame."
                )

    return (
        valid_end,
        valid_frames,
        skipped_bytes,
        problem_message,
    )


def copy_prefix(
    input_path: Path,
    output_path: Path,
    length: int,
) -> None:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    remaining = length

    with input_path.open("rb") as source:
        with output_path.open("wb") as target:
            while remaining > 0:
                chunk = source.read(
                    min(
                        1024 * 1024,
                        remaining,
                    )
                )

                if not chunk:
                    raise RuntimeError(
                        "Unexpected EOF while copying."
                    )

                target.write(chunk)
                remaining -= len(chunk)


def default_output_path(
    input_path: Path,
) -> Path:
    return input_path.with_name(
        f"{input_path.stem}_repaired"
        f"{input_path.suffix}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Repair an XMEye recording by keeping complete frame records, "
            "skipping invalid data between records, and trimming a partial tail."
        )
    )

    parser.add_argument(
        "input",
        type=Path,
        help="Input .xmeye or .h265x file.",
    )

    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help=(
            "Output repaired recording. "
            "Default: *_repaired with the original suffix."
        ),
    )

    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Replace the input file after successful repair. "
            "The original is first renamed to *.broken with the same suffix."
        ),
    )

    args = parser.parse_args()

    input_path = args.input.resolve()

    if not input_path.is_file():
        parser.error(
            f"Input file does not exist: {input_path}"
        )

    if args.output is not None and args.replace:
        parser.error(
            "Do not use an explicit output together with --replace."
        )

    if args.output is not None:
        output_path = args.output.resolve()
    else:
        output_path = default_output_path(
            input_path
        )

    if output_path == input_path:
        parser.error(
            "Output must differ from input; use --replace for a guarded replacement."
        )

    size = input_path.stat().st_size

    print(
        f"Input:        {input_path}"
    )

    print(
        f"Input size:   {size:,} bytes"
    )

    temp_output = (
        input_path.with_name(
            input_path.name + ".repairing"
        )
        if args.replace
        else output_path
    )

    stats = sanitize_file(
        input_path,
        temp_output,
        print,
    )

    print(f"Valid frames: {stats.frame_count:,}")
    print(f"Valid bytes:  {stats.output_bytes:,}")
    print(f"Skipped:      {stats.skipped_bytes:,} byte(s)")

    if stats.frame_count <= 0:
        temp_output.unlink(missing_ok=True)
        raise RuntimeError("No complete XMEye frames were found.")

    if temp_output.stat().st_size != stats.output_bytes:
        raise RuntimeError(
            "Repaired file size verification failed."
        )

    if stats.skipped_bytes == 0:
        temp_output.unlink(missing_ok=True)
        print()
        print("The file already consists entirely of complete XMEye frames.")
        print("No repaired copy was created.")
        return

    if args.replace:
        backup_path = input_path.with_name(
            f"{input_path.stem}.broken"
            f"{input_path.suffix}"
        )

        if backup_path.exists():
            raise RuntimeError(
                f"Backup already exists: {backup_path}"
            )

        input_path.rename(
            backup_path
        )

        temp_output.rename(
            input_path
        )

        print()
        print(
            f"Repaired:     {input_path}"
        )

        print(
            f"Original:     {backup_path}"
        )

    else:
        print()
        print(
            f"Repaired:     {output_path}"
        )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\nInterrupted.",
            file=sys.stderr,
        )

        sys.exit(130)

    except Exception as exc:
        print(
            f"\nError: {exc}",
            file=sys.stderr,
        )

        sys.exit(1)
