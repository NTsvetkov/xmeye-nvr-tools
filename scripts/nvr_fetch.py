#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Callable, Iterable

from xmeye_repair import XMEyeRecordSanitizer


HEADER_FMT = "<BB2xII2xHI"
HEADER_LEN = struct.calcsize(HEADER_FMT)

MSG_LOGIN_REQ = 1000
MSG_FILE_QUERY = 1440
MSG_PLAYBACK_CLAIM = 1424
MSG_PLAYBACK_CTRL = 1420


@dataclass
class Recording:
    index: int
    begin: datetime
    end: datetime
    filename: str
    file_length_raw: str
    size_bytes: int
    event_type: str

    @property
    def duration(self):
        return self.end - self.begin


def sofia_hash(password: str) -> str:
    md5_digest = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

    return "".join(
        chars[(a + b) % 62]
        for a, b in zip(md5_digest[::2], md5_digest[1::2])
    )


def read_exact(
    sock: socket.socket,
    n: int,
    overall_timeout: float = 15.0,
    cancel_event=None,
) -> bytes:
    data = b""
    sock.settimeout(5.0)

    last_progress = time.time()

    while len(data) < n:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Operation cancelled.")

        try:
            chunk = sock.recv(n - len(data))
        except (socket.timeout, TimeoutError):
            if time.time() - last_progress > overall_timeout:
                raise

            continue

        if not chunk:
            raise ConnectionError("Connection closed by NVR")

        data += chunk
        last_progress = time.time()

    return data


class DVRIPClient:
    def __init__(
        self,
        host: str,
        port: int = 34567,
        connect_timeout: float = 10.0,
    ):
        self.host = host
        self.port = port

        self.sock = socket.create_connection(
            (host, port),
            timeout=connect_timeout,
        )

        self.session_id = 0
        self.sequence = 0

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def send(self, msg_id: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8") + b"\x0a\x00"

        header = struct.pack(
            HEADER_FMT,
            0xFF,
            0x00,
            self.session_id,
            self.sequence,
            msg_id,
            len(body),
        )

        self.sock.sendall(header + body)
        self.sequence += 1

    def recv_json(self) -> dict:
        header_raw = read_exact(
            self.sock,
            HEADER_LEN,
        )

        _, _, _, _, _, payload_len = struct.unpack(
            HEADER_FMT,
            header_raw,
        )

        if payload_len:
            payload_raw = read_exact(
                self.sock,
                payload_len,
            )
        else:
            payload_raw = b""

        return json.loads(
            payload_raw
            .rstrip(b"\x00\x0a")
            .decode("utf-8", errors="replace")
        )

    def login(
        self,
        username: str,
        password: str,
    ) -> None:
        self.send(
            MSG_LOGIN_REQ,
            {
                "EncryptType": "MD5",
                "LoginType": "DVRIP-Web",
                "PassWord": sofia_hash(password),
                "UserName": username,
            },
        )

        response = self.recv_json()

        if response.get("Ret") not in (100, "100"):
            raise RuntimeError(
                f"NVR login failed: {response}"
            )

        self.session_id = int(
            response["SessionID"],
            16,
        )

    def search_recordings(
        self,
        channel: int,
        begin: datetime,
        end: datetime,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_event=None,
    ) -> list[Recording]:
        """
        Search all recordings in the requested period.

        XMEye OPFileQuery returns at most 64 results per request.
        When exactly 64 items are returned, continue searching from
        the EndTime of the last item.

        Queries are inclusive, so the boundary recording may be
        returned again. Results are therefore deduplicated by
        FileName.
        """
        QUERY_LIMIT = 64

        cursor = begin

        all_items: list[dict] = []
        seen_filenames: set[str] = set()

        page_number = 1

        while cursor < end:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Search cancelled.")

            self.send(
                MSG_FILE_QUERY,
                {
                    "Name": "OPFileQuery",
                    "SessionID": f"0x{self.session_id:08X}",
                    "OPFileQuery": {
                        "BeginTime": format_nvr_datetime(cursor),
                        "Channel": channel,
                        "DriverTypeMask": "0x0000FFFF",
                        "EndTime": format_nvr_datetime(end),
                        "Event": "*",
                        "StreamType": "0x00000000",
                        "Type": "h264",
                    },
                },
            )

            response = self.recv_json()

            if response.get("Ret") not in (100, "100"):
                raise RuntimeError(
                    f"OPFileQuery failed: {response}"
                )

            files = response.get(
                "OPFileQuery",
                [],
            )

            if progress_callback is None:
                print(
                    f"  Search page {page_number}: "
                    f"{len(files)} recording(s)"
                )
            else:
                progress_callback(page_number, len(files))

            if not files:
                break

            for item in files:
                filename = (
                    item.get("FileName")
                    or ""
                )

                if not filename:
                    continue

                if filename in seen_filenames:
                    continue

                seen_filenames.add(
                    filename
                )

                all_items.append(
                    item
                )

            # Fewer than 64 means the NVR has returned the
            # final page.
            if len(files) < QUERY_LIMIT:
                break

            last_item = files[-1]

            last_end_text = (
                last_item.get("EndTime")
            )

            if not last_end_text:
                raise RuntimeError(
                    "Cannot paginate OPFileQuery: "
                    "last result has no EndTime."
                )

            next_cursor = parse_nvr_datetime(
                last_end_text
            )

            # Safety guard against an NVR repeatedly returning
            # the same page forever.
            if next_cursor <= cursor:
                raise RuntimeError(
                    "OPFileQuery pagination made no progress: "
                    f"cursor={cursor}, "
                    f"next={next_cursor}"
                )

            if next_cursor >= end:
                break

            cursor = next_cursor
            page_number += 1

        all_items.sort(
            key=lambda item: parse_nvr_datetime(
                item["BeginTime"]
            )
        )

        result = []

        for index, item in enumerate(
            all_items,
            start=1,
        ):
            filename = (
                item.get("FileName")
                or ""
            )

            if "[M]" in filename:
                event_type = "M"
            elif "[R]" in filename:
                event_type = "R"
            else:
                event_type = "?"

            raw_size = (
                item.get("FileLength")
                or "0"
            )

            try:
                # On this NVR FileLength is hexadecimal KiB.
                size_bytes = (
                    int(raw_size, 16)
                    * 1024
                )
            except (
                TypeError,
                ValueError,
            ):
                size_bytes = 0

            result.append(
                Recording(
                    index=index,
                    begin=parse_nvr_datetime(
                        item["BeginTime"]
                    ),
                    end=parse_nvr_datetime(
                        item["EndTime"]
                    ),
                    filename=filename,
                    file_length_raw=raw_size,
                    size_bytes=size_bytes,
                    event_type=event_type,
                )
            )

        return result

    def download_recording(
        self,
        recording: Recording,
        channel: int,
        output_path: Path,
        timeout: float = 120.0,
        progress_callback: Callable[[int, int, float], None] | None = None,
        cancel_event=None,
        record_log_callback: Callable[[str], None] | None = None,
    ) -> None:
        playback_param = {
            "PlayMode": "ByName",
            "FileName": recording.filename,
            "StreamType": 0,
            "Value": 0,
            "TransMode": "TCP",
        }

        begin_str = format_nvr_datetime(
            recording.begin
        )

        end_str = format_nvr_datetime(
            recording.end
        )

        self.send(
            MSG_PLAYBACK_CLAIM,
            {
                "Name": "OPPlayBack",
                "SessionID": f"0x{self.session_id:08X}",
                "OPPlayBack": {
                    "Action": "Claim",
                    "Parameter": playback_param,
                    "StartTime": begin_str,
                    "EndTime": end_str,
                },
            },
        )

        claim_response = self.recv_json()

        if claim_response.get("Ret") not in (
            100,
            "100",
        ):
            raise RuntimeError(
                f"Playback Claim failed: "
                f"{claim_response}"
            )

        self.send(
            MSG_PLAYBACK_CTRL,
            {
                "Name": "OPPlayBack",
                "SessionID": f"0x{self.session_id:08X}",
                "OPPlayBack": {
                    "Action": "DownloadStart",
                    "Parameter": playback_param,
                    "StartTime": begin_str,
                    "EndTime": end_str,
                },
            },
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        total = 0
        started = time.time()
        last_report = started

        with output_path.open("wb") as output_file:
            sanitizer = XMEyeRecordSanitizer(
                output_file,
                record_log_callback or print,
            )
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Download cancelled.")

                header_raw = read_exact(
                    self.sock,
                    HEADER_LEN,
                    overall_timeout=timeout,
                    cancel_event=cancel_event,
                )

                (
                    _,
                    _version,
                    _session,
                    _sequence,
                    _msg_id,
                    payload_len,
                ) = struct.unpack(
                    HEADER_FMT,
                    header_raw,
                )

                if payload_len == 0:
                    break

                payload = read_exact(
                    self.sock,
                    payload_len,
                    overall_timeout=timeout,
                    cancel_event=cancel_event,
                )

                sanitizer.feed(payload)
                total += payload_len

                now = time.time()

                if now - last_report >= 1.0:
                    elapsed = now - started

                    speed = (
                        total / elapsed
                        if elapsed > 0
                        else 0
                    )

                    expected = recording.size_bytes

                    if progress_callback is not None:
                        progress_callback(total, expected, speed)
                    elif expected > 0:
                        percent = min(
                            total / expected * 100,
                            100.0,
                        )

                        print(
                            "\r"
                            f"  {percent:6.2f}%  "
                            f"{format_size(total)} / "
                            f"{format_size(expected)}  "
                            f"{format_size(speed)}/s",
                            end="",
                            flush=True,
                        )
                    else:
                        print(
                            "\r"
                            f"  {format_size(total)}  "
                            f"{format_size(speed)}/s",
                            end="",
                            flush=True,
                        )

                    last_report = now

            sanitize_stats = sanitizer.finish()

        if sanitize_stats.frame_count <= 0:
            raise RuntimeError(
                "Download contained no complete XMEye frame records."
            )

        if sanitize_stats.skipped_bytes:
            message = (
                f"Sanitized download: kept {sanitize_stats.frame_count:,} records "
                f"({format_size(sanitize_stats.output_bytes)}), skipped "
                f"{format_size(sanitize_stats.skipped_bytes)}."
            )
            if record_log_callback is not None:
                record_log_callback(message)
            else:
                print(f"  {message}")

        elapsed = time.time() - started

        expected = recording.size_bytes

        if progress_callback is not None:
            speed = total / elapsed if elapsed > 0 else 0.0
            progress_callback(total, expected, speed)

        # Reaching a zero-length DVRIP packet is a clean end marker
        # from the NVR. Some recordings (especially the current/latest
        # one) can have a FileLength reported by OPFileQuery that differs
        # slightly from the number of bytes PlayByName actually sends.
        # A transport failure never reaches this point because read_exact()
        # raises first, so accept a clean EOF and merely warn on mismatch.
        if progress_callback is not None:
            return
        elif expected > 0 and total != expected:
            difference = total - expected

            print(
                "\r"
                f"  Clean EOF at {format_size(total)} "
                f"({total} bytes); OPFileQuery reported "
                f"{format_size(expected)} ({expected} bytes)."
                f"{' ' * 10}"
            )

            print(
                "  Warning: accepting clean NVR end marker; "
                f"size difference {difference:+,} bytes."
            )
        else:
            print(
                "\r"
                f"  100.00%  "
                f"{format_size(total)}  "
                f"in {format_duration(elapsed)}"
                f"{' ' * 20}"
            )

def parse_nvr_datetime(
    value: str,
) -> datetime:
    return datetime.strptime(
        value,
        "%Y-%m-%d %H:%M:%S",
    )


def parse_user_datetime(
    value: str,
) -> datetime:
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                value,
                fmt,
            )
        except ValueError:
            pass

    raise argparse.ArgumentTypeError(
        f"Invalid date/time: {value!r}. "
        f"Use YYYY-MM-DD HH:MM[:SS]"
    )


def format_nvr_datetime(
    value: datetime,
) -> str:
    return value.strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def format_size(
    value: float,
) -> str:
    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
    )

    size = float(value)

    for unit in units:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"

        size /= 1024.0

    return f"{size:.1f} PiB"


def format_duration(
    seconds: float,
) -> str:
    seconds = int(
        round(seconds)
    )

    hours, remainder = divmod(
        seconds,
        3600,
    )

    minutes, seconds = divmod(
        remainder,
        60,
    )

    if hours:
        return (
            f"{hours}:"
            f"{minutes:02d}:"
            f"{seconds:02d}"
        )

    return (
        f"{minutes}:"
        f"{seconds:02d}"
    )


def parse_selection(
    text: str,
    max_index: int,
) -> list[int]:
    text = text.strip().lower()

    if text in (
        "all",
        "a",
        "*",
    ):
        return list(
            range(
                1,
                max_index + 1,
            )
        )

    selected: set[int] = set()

    for part in text.split(","):
        part = part.strip()

        if not part:
            continue

        if "-" in part:
            match = re.fullmatch(
                r"(\d+)\s*-\s*(\d+)",
                part,
            )

            if not match:
                raise ValueError(
                    f"Invalid range: {part}"
                )

            start = int(
                match.group(1)
            )

            end = int(
                match.group(2)
            )

            if start > end:
                start, end = end, start

            selected.update(
                range(
                    start,
                    end + 1,
                )
            )
        else:
            selected.add(
                int(part)
            )

    invalid = [
        value
        for value in selected
        if (
            value < 1
            or value > max_index
        )
    ]

    if invalid:
        raise ValueError(
            f"Invalid selection: {invalid}"
        )

    return sorted(selected)


def safe_filename(
    recording: Recording,
    channel: int,
) -> str:
    """
    Build a filename similar to the naming convention used by
    the original XMEye interface.

    Example:
        01_2026-09-14_200000_210002.xmeye
    """
    channel_number = channel + 1

    date_part = recording.begin.strftime(
        "%Y-%m-%d"
    )

    begin_part = recording.begin.strftime(
        "%H%M%S"
    )

    end_part = recording.end.strftime(
        "%H%M%S"
    )

    return (
        f"{channel_number:02d}_"
        f"{date_part}_"
        f"{begin_part}_"
        f"{end_part}.xmeye"
    )


def print_recordings(
    recordings: Iterable[Recording],
) -> None:
    recordings = list(recordings)

    if not recordings:
        print(
            "No recordings found."
        )
        return

    print()

    print(
        " #   Start                "
        "End                  "
        "Type      Size"
    )

    print(
        "---  -------------------  "
        "-------------------  "
        "----  ---------"
    )

    for recording in recordings:
        print(
            f"{recording.index:>3}  "
            f"{recording.begin:%Y-%m-%d %H:%M:%S}  "
            f"{recording.end:%Y-%m-%d %H:%M:%S}  "
            f"{recording.event_type:^4}  "
            f"{format_size(recording.size_bytes):>9}"
        )

    total_size = sum(
        recording.size_bytes
        for recording in recordings
    )

    print()

    print(
        f"{len(recordings)} recording(s), "
        f"total {format_size(total_size)}"
    )



def disk_free_info(
    path: Path,
) -> tuple[int, int, float]:
    """
    Return (free_bytes, total_bytes, free_percent) for the filesystem
    containing path. The output directory is created first so that
    shutil.disk_usage() always receives an existing path.
    """
    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    usage = shutil.disk_usage(
        path
    )

    free_percent = (
        usage.free / usage.total * 100.0
        if usage.total > 0
        else 0.0
    )

    return (
        usage.free,
        usage.total,
        free_percent,
    )


def has_min_free_space(
    path: Path,
    min_percent: float,
) -> bool:
    if min_percent <= 0:
        return True

    free_bytes, _total_bytes, free_percent = disk_free_info(
        path
    )

    if free_percent >= min_percent:
        return True

    print()
    print(
        "Free disk space is below the configured limit:"
    )
    print(
        f"  Free:    {format_size(free_bytes)} "
        f"({free_percent:.2f}%)"
    )
    print(
        f"  Minimum: {min_percent:.2f}%"
    )
    print(
        "Stopping before the next download."
    )

    return False

def resolve_period(
    args,
) -> tuple[datetime, datetime]:
    now = datetime.now()

    if args.date:
        day = datetime.strptime(
            args.date,
            "%Y-%m-%d",
        ).date()

        begin = datetime.combine(
            day,
            dt_time.min,
        )

        end = datetime.combine(
            day,
            dt_time.max,
        ).replace(
            microsecond=0
        )

        if day == now.date():
            end = now

        return begin, end

    if not args.from_time:
        raise ValueError(
            "Use either --from or --date."
        )

    begin = args.from_time
    end = args.to_time or now

    if end <= begin:
        raise ValueError(
            "--to must be later than --from."
        )

    return begin, end


def convert_recording(
    raw_path: Path,
) -> Path:
    """
    Convert a downloaded XMEye recording to MKV by invoking
    xmeye_convert.py with the current Python interpreter.
    """
    converter = Path(__file__).with_name(
        "xmeye_convert.py"
    )

    if not converter.is_file():
        raise RuntimeError(
            f"Converter not found: {converter}"
        )

    output_path = raw_path.with_suffix(
        ".mkv"
    )

    command = [
        sys.executable,
        str(converter),
        str(raw_path),
        str(output_path),
    ]

    print()

    print(
        f"Converting to MKV: "
        f"{output_path.name}"
    )

    subprocess.run(
        command,
        check=True,
    )

    if not output_path.is_file():
        raise RuntimeError(
            "Conversion finished but output file "
            f"was not created: {output_path}"
        )

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Interactive XMEye DVRIP "
            "recording browser/downloader"
        )
    )

    parser.add_argument(
        "--host",
        default="192.168.66.153",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=34567,
    )

    parser.add_argument(
        "--user",
        default="koko",
    )

    parser.add_argument(
        "--password",
        default=os.environ.get(
            "XMEYE_PASSWORD"
        ),
        help=(
            "NVR password. Defaults to "
            "environment variable XMEYE_PASSWORD."
        ),
    )

    parser.add_argument(
        "--channel",
        type=int,
        default=0,
        help=(
            "0-based channel number. "
            "0 = D01, 1 = D02, ..."
        ),
    )

    parser.add_argument(
        "--from",
        dest="from_time",
        type=parse_user_datetime,
        help=(
            'Start time, e.g. '
            '"2026-09-14 18:00"'
        ),
    )

    parser.add_argument(
        "--to",
        dest="to_time",
        type=parse_user_datetime,
        help=(
            "End time. "
            "If omitted, current time is used."
        ),
    )

    parser.add_argument(
        "--date",
        help=(
            "Search a whole day, "
            "e.g. 2026-09-14"
        ),
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Download all search results "
            "without interactive selection."
        ),
    )

    parser.add_argument(
        "--list-only",
        action="store_true",
        help=(
            "List matching recordings "
            "without downloading anything."
        ),
    )

    parser.add_argument(
        "--convert",
        action="store_true",
        help=(
            "Convert downloaded XMEye "
            "recordings to MKV."
        ),
    )

    parser.add_argument(
        "--delete-raw",
        action="store_true",
        help=(
            "Delete the raw .xmeye file "
            "after successful conversion. "
            "Requires --convert."
        ),
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("downloads"),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help=(
            "Maximum download attempts per recording. "
            "Each attempt uses a fresh DVRIP session. "
            "Default: 3."
        ),
    )

    parser.add_argument(
        "--min-free-percent",
        type=float,
        default=5.0,
        help=(
            "Do not start another download when free space "
            "on the output filesystem is below this percentage. "
            "Use 0 to disable. Default: 5."
        ),
    )

    args = parser.parse_args()

    if args.delete_raw and not args.convert:
        parser.error(
            "--delete-raw requires --convert."
        )

    if args.retries < 1:
        parser.error(
            "--retries must be at least 1."
        )

    if (
        args.min_free_percent < 0
        or args.min_free_percent > 100
    ):
        parser.error(
            "--min-free-percent must be between 0 and 100."
        )

    if not args.password:
        parser.error(
            "No password supplied. "
            "Set $env:XMEYE_PASSWORD "
            "or use --password."
        )

    try:
        begin, end = resolve_period(
            args
        )
    except ValueError as exc:
        parser.error(
            str(exc)
        )

    print(
        f"NVR:     "
        f"{args.host}:{args.port}"
    )

    print(
        f"Channel: "
        f"{args.channel} "
        f"(D{args.channel + 1:02d})"
    )

    print(
        "Period:  "
        f"{format_nvr_datetime(begin)} "
        "-> "
        f"{format_nvr_datetime(end)}"
    )

    with DVRIPClient(
        args.host,
        args.port,
    ) as client:
        print(
            "\nLogging in..."
        )

        client.login(
            args.user,
            args.password,
        )

        print(
            "Searching recordings..."
        )

        recordings = client.search_recordings(
            args.channel,
            begin,
            end,
        )

        print_recordings(
            recordings
        )

        if not recordings:
            return

        if args.list_only:
            return

        if args.all:
            selected_indexes = [
                recording.index
                for recording
                in recordings
            ]
        else:
            print()
            print(
                "Select recordings, e.g.:"
            )
            print(
                "  1,3,5-7"
            )
            print(
                "  all"
            )
            print(
                "  q"
            )

            while True:
                selection = input(
                    "\nSelection: "
                ).strip()

                if selection.lower() in (
                    "q",
                    "quit",
                    "exit",
                ):
                    return

                try:
                    selected_indexes = (
                        parse_selection(
                            selection,
                            len(recordings),
                        )
                    )

                    break

                except (
                    ValueError,
                    TypeError,
                ) as exc:
                    print(
                        f"Invalid selection: "
                        f"{exc}"
                    )

        selected = [
            recording
            for recording in recordings
            if recording.index
            in selected_indexes
        ]

        selected_total = sum(
            recording.size_bytes
            for recording in selected
        )

        print()

        print(
            f"Selected "
            f"{len(selected)} file(s), "
            f"{format_size(selected_total)}"
        )

        for position, recording in enumerate(
            selected,
            start=1,
        ):
            filename = safe_filename(
                recording,
                args.channel,
            )

            output_path = (
                args.output
                / filename
            )

            print()

            print(
                f"[{position}/{len(selected)}] "
                f"{recording.begin:%Y-%m-%d %H:%M:%S}"
                " -> "
                f"{recording.end:%H:%M:%S}"
            )

            print(
                f"Output: {output_path}"
            )

            mkv_path = (
                output_path
                .with_suffix(".mkv")
            )

            # When converting to MKV, an existing MKV means this
            # recording has already been completed successfully.
            # Skip the raw download as well, which makes repeated
            # batch runs safe after --delete-raw removed the .xmeye.
            if args.convert and mkv_path.is_file():
                print(
                    "MKV already exists, "
                    "skipping download and conversion: "
                    f"{mkv_path}"
                )
                continue

            download_required = True

            if output_path.is_file():
                existing_size = (
                    output_path
                    .stat()
                    .st_size
                )

                if (
                    recording.size_bytes > 0
                    and existing_size
                    == recording.size_bytes
                ):
                    print(
                        "Already downloaded."
                    )

                    download_required = False

                else:
                    print(
                        "Existing incomplete/different "
                        "file will be overwritten."
                    )

            if download_required:
                # Check immediately before starting a new recording.
                # This naturally happens after the previous recording
                # has been downloaded/converted/deleted.
                if not has_min_free_space(
                    args.output,
                    args.min_free_percent,
                ):
                    break

                download_succeeded = False

                for attempt in range(1, args.retries + 1):
                    if args.retries > 1:
                        print(
                            f"Download attempt "
                            f"{attempt}/{args.retries}..."
                        )

                    try:
                        # Always use a fresh DVRIP session. Some XMEye
                        # NVRs leave the previous playback resource busy,
                        # and a broken transfer should never reuse its
                        # connection. output_path is opened with "wb",
                        # so every retry starts the recording from scratch.
                        with DVRIPClient(
                            args.host,
                            args.port,
                        ) as download_client:
                            download_client.login(
                                args.user,
                                args.password,
                            )

                            download_client.download_recording(
                                recording,
                                args.channel,
                                output_path,
                                timeout=args.timeout,
                            )

                        download_succeeded = True
                        break

                    except Exception as exc:
                        print(
                            f"Download attempt {attempt} failed: "
                            f"{exc}",
                            file=sys.stderr,
                        )

                        if attempt < args.retries:
                            print(
                                "Retrying with a fresh DVRIP session..."
                            )
                            time.sleep(1.0)

                if not download_succeeded:
                    print(
                        "Download failed after "
                        f"{args.retries} attempt(s); "
                        "leaving the partial .xmeye file and "
                        "continuing with the next recording.",
                        file=sys.stderr,
                    )
                    continue

            if args.convert:
                if mkv_path.is_file():
                    print(
                        "MKV already exists, "
                        "skipping conversion: "
                        f"{mkv_path}"
                    )

                else:
                    try:
                        mkv_path = convert_recording(
                            output_path
                        )

                        print(
                            f"Converted: "
                            f"{mkv_path}"
                        )

                        if args.delete_raw:
                            output_path.unlink()

                            print(
                                "Deleted raw file: "
                                f"{output_path}"
                            )

                    except subprocess.CalledProcessError as exc:
                        print(
                            "Conversion failed with "
                            f"exit code "
                            f"{exc.returncode}.",
                            file=sys.stderr,
                        )

                        print(
                            "Raw file kept: "
                            f"{output_path}",
                            file=sys.stderr,
                        )

                    except Exception as exc:
                        print(
                            "Conversion failed: "
                            f"{exc}",
                            file=sys.stderr,
                        )

                        print(
                            "Raw file kept: "
                            f"{output_path}",
                            file=sys.stderr,
                        )

    print()
    print(
        "Done."
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "\nInterrupted."
        )

        sys.exit(130)
