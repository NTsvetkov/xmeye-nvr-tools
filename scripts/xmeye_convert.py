#!/usr/bin/env python3
"""Timestamp-aware XMEye to MP4 converter with video stream copy."""

import argparse
import json
import mmap
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

MAGIC = b"\x00\x00\x01"
INFO, AUDIO, IFRAME, PFRAME, SNAPSHOT = 0xF9, 0xFA, 0xFC, 0xFD, 0xFE
KNOWN_TYPES = {INFO, AUDIO, IFRAME, PFRAME, SNAPSHOT}
CODECS = {0x02: ("h264", "h264"), 0x03: ("hevc", "hevc")}
DEFAULT_ANCHOR_INTERVAL = 60.0
DEFAULT_TRANSITION_MIN_SECONDS = 10.0
MAX_TRAILER_BYTES = 4096
MAX_KEYFRAME_GAP = 30.0
MIN_SEGMENT_SECONDS = 5.0


@dataclass
class Keyframe:
    number: int
    frame_index: int
    timestamp: datetime
    mode: int
    codec_id: int


@dataclass
class AudioInfo:
    codec: str
    demuxer: str
    extension: str
    sample_rate: int
    channels: int
    sample_format: str
    packet_count: int
    payload_bytes: int
    total_samples: int
    strip_bytes: int
    header: bytes
    packet_lengths: Counter

    @property
    def duration(self):
        return self.total_samples / self.sample_rate


@dataclass
class StreamInfo:
    codec: str
    demuxer: str
    keyframes: list[Keyframe] = field(default_factory=list)
    iframe_count: int = 0
    pframe_count: int = 0
    audio_count: int = 0
    info_count: int = 0
    snapshot_count: int = 0
    trailer_bytes: int = 0
    info_timestamps: list[datetime] = field(default_factory=list)
    audio: AudioInfo | None = None
    audio_error: str | None = None
    timestamp_repairs: int = 0
    leading_pframes: int = 0
    leading_audio_frames: int = 0

    @property
    def video_frames(self):
        return self.iframe_count + self.pframe_count


def normalize_local_timestamp_regressions(keyframes):
    """Interpolate short, bracketed FC clock regressions without dropping video."""
    repaired = 0
    index = 1
    while index < len(keyframes):
        if keyframes[index].timestamp >= keyframes[index - 1].timestamp:
            index += 1
            continue

        left_index = index - 1
        left = keyframes[left_index]
        right_index = index
        lowest = keyframes[index].timestamp
        while (
            right_index < len(keyframes)
            and keyframes[right_index].timestamp <= left.timestamp
        ):
            lowest = min(lowest, keyframes[right_index].timestamp)
            right_index += 1

        backward = (left.timestamp - lowest).total_seconds()
        if right_index >= len(keyframes) or backward > MAX_KEYFRAME_GAP:
            current = keyframes[index]
            raise RuntimeError(
                f"Non-monotonic FC timestamps: {left.timestamp}, {current.timestamp}"
            )

        right = keyframes[right_index]
        forward = (right.timestamp - left.timestamp).total_seconds()
        frame_span = right.frame_index - left.frame_index
        if forward > MAX_KEYFRAME_GAP or forward <= 0 or frame_span <= 0:
            current = keyframes[index]
            raise RuntimeError(
                f"Non-monotonic FC timestamps: {left.timestamp}, {current.timestamp}"
            )

        for repair_index in range(index, right_index):
            keyframe = keyframes[repair_index]
            fraction = (keyframe.frame_index - left.frame_index) / frame_span
            keyframe.timestamp = left.timestamp + timedelta(seconds=forward * fraction)
            repaired += 1
        index = right_index

    return repaired


@dataclass
class Segment:
    index: int
    start_keyframe: int
    end_keyframe: int
    start: datetime
    end: datetime
    frame_count: int
    mode: int
    raw_path: Path | None = None
    encoded_frames: int | None = None
    encoded_keyframes: int | None = None
    dropped_gops: int = 0
    dropped_frames: int = 0
    fully_dropped: bool = False

    @property
    def duration(self):
        return (self.end - self.start).total_seconds()

    @property
    def fps(self):
        return self.frame_count / self.duration

    @property
    def encoded_fps(self):
        frames = self.frame_count if self.encoded_frames is None else self.encoded_frames
        return frames / self.duration


def decode_datetime(raw):
    return datetime(
        2000 + ((raw >> 26) & 0x3F), (raw >> 22) & 0x0F,
        (raw >> 17) & 0x1F, (raw >> 12) & 0x1F,
        (raw >> 6) & 0x3F, raw & 0x3F,
    )


def recording_times(path):
    match = re.fullmatch(
        r"\d{2}_(\d{4}-\d{2}-\d{2})_(\d{6})_(\d{6})", path.stem
    )
    if not match:
        return None
    day, start_text, end_text = match.groups()
    start = datetime.strptime(f"{day}_{start_text}", "%Y-%m-%d_%H%M%S")
    end = datetime.strptime(f"{day}_{end_text}", "%Y-%m-%d_%H%M%S")
    if end < start:
        end += timedelta(days=1)
    return start, end


def frame_bounds(data, pos, size):
    if pos + 8 > size or data[pos:pos + 3] != MAGIC:
        raise RuntimeError(f"Invalid XMEye frame header at 0x{pos:X}")
    kind = data[pos + 3]
    if kind not in KNOWN_TYPES:
        raise RuntimeError(f"Unknown XMEye frame type 0x{kind:02X} at 0x{pos:X}")
    if kind in (IFRAME, SNAPSHOT):
        if pos + 16 > size:
            raise RuntimeError(f"Truncated 16-byte frame header at 0x{pos:X}")
        length = struct.unpack_from("<I", data, pos + 12)[0]
        start = pos + 16
    elif kind == PFRAME:
        length = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
    else:
        length = struct.unpack_from("<H", data, pos + 6)[0]
        start = pos + 8
    end = start + length
    if end > size:
        raise RuntimeError(
            f"Truncated XMEye frame 0x{kind:02X} at 0x{pos:X}: "
            f"payload is {length:,} bytes"
        )
    return kind, start, end


def classify_payload(payload):
    h264_types, hevc_types = set(), set()
    pos = 0
    while True:
        pos = payload.find(MAGIC, pos)
        if pos < 0:
            break
        start = pos + 3
        if start < len(payload) and payload[start] == 0:
            start += 1
        if start < len(payload):
            value = payload[start]
            h264_types.add(value & 0x1F)
            hevc_types.add((value >> 1) & 0x3F)
        pos = start + 1
    h264 = {7, 8}.issubset(h264_types) and 5 in h264_types
    hevc = {32, 33, 34}.issubset(hevc_types) and any(
        value in hevc_types for value in range(16, 24)
    )
    if h264 == hevc:
        return None
    return "h264" if h264 else "hevc"


AAC_SAMPLE_RATES = {
    0: 96000, 1: 88200, 2: 64000, 3: 48000, 4: 44100,
    5: 32000, 6: 24000, 7: 22050, 8: 16000, 9: 12000,
    10: 11025, 11: 8000, 12: 7350,
}


def parse_adts_frame(payload):
    """Return (rate, channels, MPEG-4 object type, samples)."""
    if len(payload) < 7 or payload[0] != 0xFF or payload[1] & 0xF6 != 0xF0:
        return None
    sample_rate = AAC_SAMPLE_RATES.get((payload[2] >> 2) & 0x0F)
    channels = ((payload[2] & 0x01) << 2) | (payload[3] >> 6)
    object_type = (payload[2] >> 6) + 1
    frame_length = ((payload[3] & 0x03) << 11) | (payload[4] << 3) | (payload[5] >> 5)
    header_length = 7 if payload[1] & 0x01 else 9
    if (
        sample_rate is None
        or channels <= 0
        or frame_length != len(payload)
        or frame_length < header_length
    ):
        return None
    samples = 1024 * ((payload[6] & 0x03) + 1)
    return sample_rate, channels, object_type, samples


def inspect_stream(path):
    metadata_codecs, payload_codecs, counts = Counter(), Counter(), Counter()
    keyframes, info_timestamps = [], []
    audio_headers, audio_lengths = Counter(), Counter()
    audio_bytes = audio_samples = 0
    adts_config = None
    adts_valid = True
    with path.open("rb") as source, mmap.mmap(
        source.fileno(), 0, access=mmap.ACCESS_READ
    ) as data:
        size, pos, video_index = len(data), 0, 0
        while pos < size:
            if pos + 4 > size or data[pos:pos + 3] != MAGIC:
                trailer = size - pos
                if trailer <= MAX_TRAILER_BYTES:
                    counts["trailer"] = trailer
                    break
                raise RuntimeError(
                    f"Unexpected non-frame data at 0x{pos:X} ({trailer:,} bytes "
                    "remain). Use xmeye_repair.py for a damaged recording."
                )
            kind, payload_start, payload_end = frame_bounds(data, pos, size)
            if kind == IFRAME:
                codec_id = data[pos + 4] & 0x0F
                metadata_codecs[codec_id] += 1
                try:
                    timestamp = decode_datetime(struct.unpack_from("<I", data, pos + 8)[0])
                except ValueError as exc:
                    raise RuntimeError(f"Invalid FC timestamp at 0x{pos:X}") from exc
                keyframes.append(Keyframe(
                    len(keyframes), video_index, timestamp, data[pos + 5], codec_id
                ))
                counts["iframe"] += 1
                video_index += 1
                if counts["iframe"] <= 16:
                    found = classify_payload(bytes(
                        data[payload_start:min(payload_end, payload_start + 65536)]
                    ))
                    if found:
                        payload_codecs[found] += 1
            elif kind == PFRAME:
                if not keyframes:
                    counts["leading_pframe"] += 1
                else:
                    counts["pframe"] += 1
                    video_index += 1
            elif kind == AUDIO:
                if not keyframes:
                    counts["leading_audio"] += 1
                    pos = payload_end
                    continue
                counts["audio"] += 1
                header = bytes(data[pos + 4:pos + 6])
                length = payload_end - payload_start
                audio_headers[header] += 1
                audio_lengths[length] += 1
                audio_bytes += length
                # Header 00 02 recordings carry a four-byte XMEye prefix
                # followed by one self-describing ADTS frame.
                parsed = parse_adts_frame(bytes(data[payload_start + 4:payload_end])) \
                    if length >= 11 and header == b"\x00\x02" else None
                if parsed is None:
                    adts_valid = False
                elif adts_config is None:
                    adts_config = parsed[:3]
                    audio_samples += parsed[3]
                elif parsed[:3] == adts_config:
                    audio_samples += parsed[3]
                else:
                    adts_valid = False
            elif kind == INFO:
                counts["info"] += 1
                if payload_end - payload_start >= 4:
                    try:
                        info_timestamps.append(decode_datetime(
                            struct.unpack_from("<I", data, payload_start)[0]
                        ))
                    except ValueError:
                        pass
            else:
                counts["snapshot"] += 1
            pos = payload_end

    if not keyframes:
        raise RuntimeError("No valid XMEye video keyframes were found.")
    if len(metadata_codecs) != 1:
        raise RuntimeError(f"Codec changes or invalid codec metadata: {dict(metadata_codecs)}")
    codec_id = next(iter(metadata_codecs))
    if codec_id not in CODECS:
        raise RuntimeError(f"Unsupported XMEye codec id 0x{codec_id:02X}")
    codec, demuxer = CODECS[codec_id]
    if set(payload_codecs) != {codec}:
        raise RuntimeError(
            "FC codec metadata is not confirmed by encoded parameter sets: "
            f"metadata={codec}, payload={dict(payload_codecs)}"
        )
    timestamp_repairs = normalize_local_timestamp_regressions(keyframes)
    for previous, current in zip(keyframes, keyframes[1:]):
        gap = (current.timestamp - previous.timestamp).total_seconds()
        if gap < 0:
            raise RuntimeError(
                f"Non-monotonic FC timestamps: {previous.timestamp}, {current.timestamp}"
            )
    if info_timestamps and (
        min(info_timestamps) < keyframes[0].timestamp - timedelta(seconds=1)
        or max(info_timestamps) > keyframes[-1].timestamp + timedelta(seconds=MAX_KEYFRAME_GAP)
    ):
        raise RuntimeError("F9 timestamps disagree with the FC timeline.")

    audio = None
    audio_error = None
    if counts["audio"]:
        if adts_valid and adts_config is not None and set(audio_headers) == {b"\x00\x02"}:
            sample_rate, channels, object_type = adts_config
            profile = {
                1: "AAC Main", 2: "AAC-LC", 3: "AAC SSR", 4: "AAC LTP",
            }.get(object_type, f"AAC object type {object_type}")
            audio = AudioInfo(
                "aac", "aac", ".aac", sample_rate, channels, profile,
                counts["audio"], audio_bytes - 4 * counts["audio"],
                audio_samples, 4, b"\x00\x02", audio_lengths,
            )
        elif set(audio_headers) == {b"\x0e\x02"}:
            # Xiongmai type 0x0e is G.711 A-law. The supplied streams also
            # independently confirm this through their D5/55 silence code and
            # an exact 8000 payload bytes/s cadence. One byte is one sample.
            audio = AudioInfo(
                "pcm_alaw", "alaw", ".alaw", 8000, 1,
                "8-bit G.711 A-law (decoded s16)", counts["audio"],
                audio_bytes, audio_bytes, 0, b"\x0e\x02", audio_lengths,
            )
        else:
            headers = {value.hex(): count for value, count in audio_headers.items()}
            audio_error = (
                "unsupported or malformed FA audio: "
                f"headers={headers}, lengths={dict(audio_lengths)}"
            )
    return StreamInfo(
        codec, demuxer, keyframes, counts["iframe"], counts["pframe"],
        counts["audio"], counts["info"], counts["snapshot"],
        counts["trailer"], info_timestamps, audio, audio_error,
        timestamp_repairs, counts["leading_pframe"], counts["leading_audio"],
    )


def infer_end(info):
    gaps = [
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(info.keyframes, info.keyframes[1:])
        if b.timestamp > a.timestamp
    ]
    gap = statistics.median(gaps[-120:]) if gaps else 1.0
    return info.keyframes[-1].timestamp + timedelta(seconds=gap)


def stable_mode_boundaries(keyframes, minimum):
    if minimum <= 0:
        return {i for i in range(1, len(keyframes)) if keyframes[i].mode != keyframes[i - 1].mode}
    runs, start = [], 0
    for index in range(1, len(keyframes)):
        if keyframes[index].mode != keyframes[start].mode:
            runs.append((start, index, keyframes[start].mode))
            start = index
    runs.append((start, len(keyframes), keyframes[start].mode))
    result, stable = set(), runs[0][2]
    for start, end, mode in runs[1:]:
        end_time = keyframes[end].timestamp if end < len(keyframes) else keyframes[-1].timestamp
        duration = (end_time - keyframes[start].timestamp).total_seconds()
        if mode != stable and duration >= minimum:
            result.add(start)
            stable = mode
    return result


def build_timeline(path, info, anchor_interval, transition_min, forced_fps):
    first = info.keyframes[0].timestamp
    if forced_fps is not None:
        end = first + timedelta(seconds=info.video_frames / forced_fps)
        source = f"explicit --fps {forced_fps:g}"
        boundaries = [0]
    else:
        named = recording_times(path)
        if named:
            named_start, end = named
            if abs((first - named_start).total_seconds()) > MAX_KEYFRAME_GAP:
                raise RuntimeError(
                    f"Filename and first FC timestamps disagree: {named_start}, {first}"
                )
            source = "recording filename"
        else:
            end, source = infer_end(info), "FC timestamps (inferred final GOP)"
        final_gap = (end - info.keyframes[-1].timestamp).total_seconds()
        if final_gap == 0:
            end = infer_end(info)
            source = "FC timestamps (inferred final GOP after zero filename gap)"
            final_gap = (end - info.keyframes[-1].timestamp).total_seconds()
        if end <= first or final_gap < 0:
            raise RuntimeError(
                f"Recording end disagrees with FC timeline (final gap {final_gap:g} s)."
            )
        if final_gap > MAX_KEYFRAME_GAP:
            end = infer_end(info)
            source = "FC timestamps (recording ended before filename boundary)"
        discontinuities = {
            index for index in range(1, len(info.keyframes))
            if (
                info.keyframes[index].timestamp
                - info.keyframes[index - 1].timestamp
            ).total_seconds() > MAX_KEYFRAME_GAP
        }
        required = stable_mode_boundaries(info.keyframes, transition_min) | discontinuities
        boundaries, last_time = [0], first
        for index, keyframe in enumerate(info.keyframes[1:], 1):
            elapsed = (keyframe.timestamp - last_time).total_seconds()
            if index in required or elapsed >= anchor_interval:
                if keyframe.timestamp > info.keyframes[boundaries[-1]].timestamp:
                    boundaries.append(index)
                    last_time = keyframe.timestamp
        if len(boundaries) > 1 and (
            end - info.keyframes[boundaries[-1]].timestamp
        ).total_seconds() < MIN_SEGMENT_SECONDS:
            boundaries.pop()

    segments = []
    for number, start_index in enumerate(boundaries):
        end_index = boundaries[number + 1] if number + 1 < len(boundaries) else len(info.keyframes)
        start_frame = info.keyframes[start_index].frame_index
        end_frame = info.keyframes[end_index].frame_index if end_index < len(info.keyframes) else info.video_frames
        segment = Segment(
            number, start_index, end_index, info.keyframes[start_index].timestamp,
            info.keyframes[end_index].timestamp if end_index < len(info.keyframes) else end,
            end_frame - start_frame, info.keyframes[start_index].mode,
        )
        if segment.duration <= 0 or segment.frame_count <= 0 or not 0.1 <= segment.fps <= 120:
            raise RuntimeError(f"Invalid timing in timeline segment {number + 1}.")
        segments.append(segment)
    return segments, end, source


def extract_segments(path, info, segments, temp_dir):
    suffix = ".h264" if info.codec == "h264" else ".h265"
    for segment in segments:
        segment.raw_path = temp_dir / f"segment_{segment.index:04d}{suffix}"
    boundary_map = {segment.start_keyframe: segment for segment in segments}
    output, key_number, written = None, 0, 0
    try:
        with path.open("rb") as source, mmap.mmap(
            source.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            size, pos = len(data), 0
            while pos < size and pos + 4 <= size and data[pos:pos + 3] == MAGIC:
                kind, start, end = frame_bounds(data, pos, size)
                if kind == IFRAME:
                    if key_number in boundary_map:
                        if output:
                            output.close()
                        output = boundary_map[key_number].raw_path.open("wb")
                    if output is None:
                        raise RuntimeError("Internal segment extraction error.")
                    output.write(data[start:end])
                    key_number += 1
                    written += 1
                elif kind == PFRAME:
                    if output is None:
                        pos = end
                        continue
                    output.write(data[start:end])
                    written += 1
                pos = end
    finally:
        if output:
            output.close()
    if written != info.video_frames:
        raise RuntimeError(f"Extracted {written:,} frames; expected {info.video_frames:,}.")
    if any(not segment.raw_path.is_file() or segment.raw_path.stat().st_size == 0 for segment in segments):
        raise RuntimeError("An extracted timeline segment is empty.")


def check_audio_timeline(info, video_duration):
    if info.audio is None:
        return
    difference = info.audio.duration - video_duration
    # A normal recorder boundary may leave one partial packet at either end.
    # A larger disagreement means the inferred parameters are not trustworthy.
    if abs(difference) > 2.0:
        info.audio_error = (
            f"FA audio duration {info.audio.duration:.3f} s disagrees with "
            f"video wall-clock duration {video_duration:.3f} s"
        )
        info.audio = None


def extract_audio(path, info, temp_dir):
    if info.audio is None:
        return None
    output_path = temp_dir / f"audio{info.audio.extension}"
    count = payload_bytes = 0
    with path.open("rb") as source, mmap.mmap(
        source.fileno(), 0, access=mmap.ACCESS_READ
    ) as data, output_path.open("wb") as output:
        size, pos, video_started = len(data), 0, False
        while pos < size and pos + 4 <= size and data[pos:pos + 3] == MAGIC:
            kind, start, end = frame_bounds(data, pos, size)
            if kind == IFRAME:
                video_started = True
            elif kind == AUDIO and video_started:
                payload_start = start + info.audio.strip_bytes
                if payload_start > end:
                    raise RuntimeError(f"Malformed audio frame at 0x{pos:X}")
                output.write(data[payload_start:end])
                payload_bytes += end - payload_start
                count += 1
            pos = end
    if count != info.audio.packet_count or payload_bytes != info.audio.payload_bytes:
        raise RuntimeError(
            f"Extracted audio {count:,}/{payload_bytes:,}; expected "
            f"{info.audio.packet_count:,}/{info.audio.payload_bytes:,}."
        )
    return output_path


def write_concat(path, segments, use_mkv=False):
    with path.open("w", encoding="ascii", newline="\n") as output:
        output.write("ffconcat version 1.0\n")
        entries = []
        for segment in segments:
            if use_mkv and segment.fully_dropped:
                if entries:
                    entries[-1][1] += segment.duration
                continue
            source = segment.raw_path.with_suffix(".mkv") if use_mkv else segment.raw_path
            entries.append([source, segment.duration])

        if not entries:
            raise RuntimeError("No decodable video segments remain.")

        for source, duration in entries:
            quoted = source.resolve().as_posix().replace("'", r"'\''")
            output.write(f"file '{quoted}'\n")
            if use_mkv:
                output.write(f"duration {duration:.9f}\n")
            else:
                segment = next(item for item in segments if item.raw_path == source)
                output.write(f"option framerate {segment.fps:.12f}\n")
                output.write(f"duration {duration:.9f}\n")


def find_executable(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name!r} was not found in PATH.")
    return str(Path(path).resolve())


def run(command):
    print("\n> " + subprocess.list2cmdline(command))
    subprocess.run(command, check=True)


def probe_encoded_frames(ffprobe, path, demuxer):
    result = subprocess.run([
        ffprobe, "-v", "error", "-f", demuxer, "-select_streams", "v:0",
        "-show_packets", "-show_entries", "packet=flags", "-of", "csv=p=0",
        str(path),
    ], check=True, capture_output=True, text=True)
    flags = result.stdout.splitlines()
    if not flags:
        raise RuntimeError(f"Could not count encoded frames in {path.name}.")
    return len(flags), sum("K" in value for value in flags)


def count_encoded_frames(ffprobe, path, demuxer):
    return probe_encoded_frames(ffprobe, path, demuxer)[0]


def timestamp_stream(ffmpeg, info, segment, output_path):
    return subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-r", f"{segment.encoded_fps:.12f}", "-f", info.demuxer,
        "-i", str(segment.raw_path), "-an", "-c:v", "copy",
        str(output_path),
    ], capture_output=True, text=True)


def decode_video(ffmpeg, path):
    result = subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror",
        "-i", str(path), "-map", "0:v:0", "-f", "null", os.devnull,
    ], capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def extract_segment_gops(path, info, segment, output_dir):
    suffix = ".h264" if info.codec == "h264" else ".h265"
    gops = []
    output = None
    key_number = -1
    try:
        with path.open("rb") as source, mmap.mmap(
            source.fileno(), 0, access=mmap.ACCESS_READ
        ) as data:
            size, pos = len(data), 0
            while pos < size and pos + 4 <= size and data[pos:pos + 3] == MAGIC:
                kind, start, end = frame_bounds(data, pos, size)
                if kind == IFRAME:
                    if output:
                        output.close()
                        output = None
                    key_number += 1
                    if key_number >= segment.end_keyframe:
                        break
                    if key_number >= segment.start_keyframe:
                        gop_path = output_dir / f"gop_{key_number:06d}{suffix}"
                        output = gop_path.open("wb")
                        gops.append((key_number, gop_path))
                if output and kind in (IFRAME, PFRAME):
                    output.write(data[start:end])
                pos = end
    finally:
        if output:
            output.close()
    return gops


def gop_frame_count(info, key_number):
    start = info.keyframes[key_number].frame_index
    if key_number + 1 < len(info.keyframes):
        end = info.keyframes[key_number + 1].frame_index
    else:
        end = info.video_frames
    return end - start


def recover_segment(path, ffmpeg, ffprobe, info, segment, temp_dir):
    recovery_dir = temp_dir / f"segment_{segment.index:04d}_recovery"
    recovery_dir.mkdir()
    gops = extract_segment_gops(path, info, segment, recovery_dir)
    if not gops:
        raise RuntimeError(f"No GOPs found while repairing segment {segment.index + 1}.")

    suffix = ".h264" if info.codec == "h264" else ".h265"
    repaired_path = temp_dir / f"segment_{segment.index:04d}_repaired{suffix}"
    dropped = []
    with repaired_path.open("wb") as repaired:
        for key_number, gop_path in gops:
            gop_mkv = gop_path.with_suffix(".mkv")
            remuxed = subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-r", "25", "-f", info.demuxer, "-i", str(gop_path),
                "-an", "-c:v", "copy", str(gop_mkv),
            ], capture_output=True, text=True)
            clean, error = (False, remuxed.stderr.strip())
            if remuxed.returncode == 0:
                clean, error = decode_video(ffmpeg, gop_mkv)
            if not clean:
                dropped.append((key_number, gop_frame_count(info, key_number), error))
                continue
            with gop_path.open("rb") as source:
                shutil.copyfileobj(source, repaired, length=1024 * 1024)

    if repaired_path.stat().st_size == 0:
        segment.encoded_frames = 0
        segment.encoded_keyframes = 0
        segment.dropped_gops = len(dropped)
        segment.dropped_frames = sum(frames for _, frames, _ in dropped)
        segment.fully_dropped = True
        repaired_path.unlink(missing_ok=True)
        for key_number, frames, error in dropped:
            timestamp = info.keyframes[key_number].timestamp
            detail = error.splitlines()[0] if error else "invalid encoded GOP"
            print(
                f"\n  Recovery: dropped damaged GOP at {timestamp} "
                f"({frames} XMEye frame(s)): {detail}"
            )
        print(
            f"\n  Recovery: omitted fully damaged interval "
            f"{segment.start} - {segment.end} ({segment.duration:.3f} s)."
        )
        return None

    segment.raw_path = repaired_path
    segment.encoded_frames, segment.encoded_keyframes = probe_encoded_frames(
        ffprobe, repaired_path, info.demuxer
    )
    segment.dropped_gops = len(dropped)
    segment.dropped_frames = sum(frames for _, frames, _ in dropped)
    output_path = repaired_path.with_suffix(".mkv")
    remuxed = timestamp_stream(ffmpeg, info, segment, output_path)
    if remuxed.returncode != 0:
        raise RuntimeError(
            f"Repaired segment {segment.index + 1} could not be timestamped: "
            f"{remuxed.stderr.strip()}"
        )
    clean, error = decode_video(ffmpeg, output_path)
    if not clean:
        raise RuntimeError(
            f"Repaired segment {segment.index + 1} still fails decoding: {error}"
        )

    for key_number, frames, error in dropped:
        timestamp = info.keyframes[key_number].timestamp
        detail = error.splitlines()[0] if error else "invalid encoded GOP"
        print(
            f"\n  Recovery: dropped damaged GOP at {timestamp} "
            f"({frames} XMEye frame(s)): {detail}"
        )
    return output_path


def timestamp_segments(path, ffmpeg, ffprobe, info, segments, temp_dir):
    # Raw H.26x has no packet timestamps. Remux each anchor interval first;
    # then the concat demuxer sees timestamped inputs. This is the optimistic
    # stream-copy path; decoding is performed once on the complete result.
    for number, segment in enumerate(segments, 1):
        print(f"  Timestamping part {number}/{len(segments)}", end="\r", flush=True)
        segment.encoded_frames, segment.encoded_keyframes = probe_encoded_frames(
            ffprobe, segment.raw_path, info.demuxer
        )
        output_segment = segment.raw_path.with_suffix(".mkv")
        remuxed = timestamp_stream(ffmpeg, info, segment, output_segment)
        if remuxed.returncode != 0:
            output_segment.unlink(missing_ok=True)
            output_segment = recover_segment(
                path, ffmpeg, ffprobe, info, segment, temp_dir
            )
            if output_segment is None:
                segment.raw_path = None
    print(" " * 60, end="\r")


def recover_decoding_segments(path, ffmpeg, ffprobe, info, segments, temp_dir):
    recovered = False
    for number, segment in enumerate(segments, 1):
        if segment.fully_dropped:
            continue
        output_segment = segment.raw_path.with_suffix(".mkv")
        clean, error = decode_video(ffmpeg, output_segment)
        if clean:
            continue
        recovered = True
        detail = error.splitlines()[0] if error else "invalid encoded segment"
        print(
            f"  Strict decode failed in part {number}/{len(segments)}: {detail}\n"
            "  Falling back to GOP-level recovery."
        )
        output_segment.unlink(missing_ok=True)
        output_segment = recover_segment(
            path, ffmpeg, ffprobe, info, segment, temp_dir
        )
        if output_segment is None:
            segment.raw_path = None
    return recovered


def mux_segments(ffmpeg, info, segments, concat_path, output_path, audio_path):
    write_concat(concat_path, segments, use_mkv=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
    ]
    if audio_path is not None:
        if info.audio.codec == "aac":
            command.extend(["-f", "aac"])
        elif info.audio.codec == "pcm_alaw":
            command.extend([
                "-f", "alaw", "-sample_rate", str(info.audio.sample_rate),
                "-ch_layout", "mono",
            ])
        command.extend(["-i", str(audio_path)])
    command.extend(["-map", "0:v:0", "-c:v", "copy"])
    if info.codec == "hevc":
        command.extend(["-tag:v", "hvc1"])
    if audio_path is None:
        command.append("-an")
    elif info.audio.codec == "pcm_alaw":
        command.extend(["-map", "1:a:0", "-c:a", "aac", "-b:a", "32k"])
    else:
        command.extend(["-map", "1:a:0", "-c:a", "copy"])
    command.extend(["-movflags", "+faststart"])
    command.append(str(output_path))
    run(command)


def probe_summary(ffprobe, path):
    result = subprocess.run([
        ffprobe, "-v", "error", "-count_packets", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,nb_read_packets:format=duration",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def validate_packets(ffprobe, path):
    result = subprocess.run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_packets",
        "-show_entries", "packet=pts_time,dts_time,duration_time,flags",
        "-of", "csv=p=0",
        str(path),
    ], check=True, capture_output=True, text=True)
    count = keys = 0
    previous_pts = previous_dts = first_pts = last_pts = None
    for line in result.stdout.splitlines():
        fields = line.strip().split(",")
        if len(fields) < 4 or "N/A" in fields[:3]:
            raise RuntimeError("FFprobe found a packet without PTS/DTS.")
        pts, dts, packet_duration = map(float, fields[:3])
        if previous_pts is not None and pts <= previous_pts:
            raise RuntimeError("Output PTS values are not strictly monotonic.")
        if previous_dts is not None and dts <= previous_dts:
            raise RuntimeError("Output DTS values are not strictly monotonic.")
        keys += "K" in fields[3]
        first_pts = pts if first_pts is None else first_pts
        last_pts, previous_pts, previous_dts = pts, pts, dts
        count += 1
    if first_pts is None:
        raise RuntimeError("FFprobe found no output packets.")
    return count, keys, first_pts, last_pts + packet_duration


def validate_audio(ffprobe, path, audio):
    result = subprocess.run([
        ffprobe, "-v", "error", "-select_streams", "a:0", "-show_streams",
        "-show_packets", "-show_entries",
        "stream=codec_name,sample_rate,channels:packet=pts_time,duration_time",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True)
    parsed = json.loads(result.stdout)
    streams = parsed.get("streams", [])
    if len(streams) != 1:
        raise RuntimeError(f"Expected one audio stream, found {len(streams)}.")
    stream = streams[0]
    actual = (
        stream.get("codec_name"), int(stream.get("sample_rate", 0)),
        int(stream.get("channels", 0)),
    )
    output_codec = "aac" if audio.codec == "pcm_alaw" else audio.codec
    expected = (output_codec, audio.sample_rate, audio.channels)
    if actual != expected:
        raise RuntimeError(f"Output audio parameters {actual}; expected {expected}.")
    previous = None
    end = 0.0
    for packet in parsed.get("packets", []):
        pts = float(packet["pts_time"])
        duration = float(packet["duration_time"])
        if previous is not None and pts <= previous:
            raise RuntimeError("Output audio PTS values are not strictly monotonic.")
        previous = pts
        end = pts + duration
    if previous is None:
        raise RuntimeError("FFprobe found no output audio packets.")
    if abs(end - audio.duration) > 0.100:
        raise RuntimeError(
            f"Output audio duration {end:.3f} s; expected {audio.duration:.3f} s."
        )
    return end


def validate_output(ffprobe, path, info, segments, expected_duration):
    summary = probe_summary(ffprobe, path)
    streams = summary.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_name") != info.codec:
        raise RuntimeError("Output video stream/codec validation failed.")
    count, keys, first_pts, video_end = validate_packets(ffprobe, path)
    expected_packets = sum(
        segment.frame_count if segment.encoded_frames is None else segment.encoded_frames
        for segment in segments
    )
    expected_keys = sum(
        segment.encoded_keyframes
        if segment.encoded_keyframes is not None
        else segment.end_keyframe - segment.start_keyframe
        for segment in segments
    )
    if count != expected_packets or keys != expected_keys:
        raise RuntimeError(
            f"Output packets/keyframes {count}/{keys}; expected "
            f"{expected_packets}/{expected_keys}."
        )
    frame_tolerance = max(
        (
            1.0 / segment.encoded_fps
            for segment in segments
            if not segment.fully_dropped and segment.encoded_fps > 0
        ),
        default=0.0,
    ) + 0.005
    if abs(video_end - expected_duration) > max(
        0.100, expected_duration * 0.0001, frame_tolerance
    ):
        raise RuntimeError(
            f"Output video duration {video_end:.3f} s; expected {expected_duration:.3f} s."
        )
    audio_end = validate_audio(ffprobe, path, info.audio) if info.audio else None
    format_duration = float(summary["format"]["duration"])
    return format_duration, video_end, audio_end, count, keys, first_pts


def print_analysis(info, segments, end, end_source):
    first = info.keyframes[0].timestamp
    duration = (end - first).total_seconds()
    modes = Counter(item.mode for item in info.keyframes)
    gaps = [
        (b.timestamp - a.timestamp).total_seconds()
        for a, b in zip(info.keyframes, info.keyframes[1:]) if b.timestamp > a.timestamp
    ]
    print(f"Codec:          {info.codec}")
    print(f"I-frames:       {info.iframe_count:,}")
    print(f"P-frames:       {info.pframe_count:,}")
    print(f"Video frames:   {info.video_frames:,}")
    print(f"Audio frames:   {info.audio_count:,}")
    if info.leading_pframes or info.leading_audio_frames:
        print(
            f"Leading drop:   {info.leading_pframes:,} P-frame(s), "
            f"{info.leading_audio_frames:,} audio frame(s) before first keyframe"
        )
    if info.audio:
        common_lengths = info.audio.packet_lengths.most_common(5)
        lengths = ", ".join(
            f"{length} B x {count:,}"
            for length, count in common_lengths
        )
        if len(info.audio.packet_lengths) > len(common_lengths):
            lengths += f" ({len(info.audio.packet_lengths)} sizes total)"
        print(f"Audio codec:    {info.audio.codec}")
        print(f"Audio format:   {info.audio.sample_rate} Hz, {info.audio.channels} ch, "
              f"{info.audio.sample_format}")
        print(f"Audio duration: {info.audio.duration:.3f} s")
        print(f"FA header:      {info.audio.header.hex(' ')}")
        print(f"FA payloads:    {lengths}")
    elif info.audio_error:
        print(f"Audio omitted:  {info.audio_error}")
    else:
        print("Audio:          none")
    print(f"Info frames:    {info.info_count:,}")
    print(f"Snapshots:      {info.snapshot_count:,}")
    print(f"FC first:       {first}")
    print(f"FC last:        {info.keyframes[-1].timestamp}")
    if info.timestamp_repairs:
        print(f"FC repairs:     {info.timestamp_repairs:,} locally regressed timestamp(s) interpolated")
    print(f"Recording end:  {end} ({end_source})")
    print(f"Duration:       {duration:.3f} s ({duration / 60:.2f} min)")
    print(f"FC modes/FPS:   {dict(sorted(modes.items()))}")
    if gaps:
        print(f"Median GOP gap: {statistics.median(gaps):.3f} s")
    print(f"Timeline parts: {len(segments)}")
    print(f"Trailer bytes:  {info.trailer_bytes:,} (ignored NVR padding)")
    print("\n #   Start                Frames  Duration    FPS       F")
    print("---  -------------------  ------  ----------  --------  --")
    for segment in segments:
        print(
            f"{segment.index + 1:>3}  {segment.start:%Y-%m-%d %H:%M:%S}  "
            f"{segment.frame_count:>6}  {segment.duration:>9.3f}s  "
            f"{segment.fps:>8.4f}  {segment.mode}"
        )


def convert(path, output_path, keep_temp, anchor_interval, transition_min, forced_fps):
    ffmpeg, ffprobe = find_executable("ffmpeg"), find_executable("ffprobe")
    print(f"Input:  {path}\nOutput: {output_path}\n\nInspecting XMEye stream...")
    info = inspect_stream(path)
    segments, end, end_source = build_timeline(
        path, info, anchor_interval, transition_min, forced_fps
    )
    expected = (end - info.keyframes[0].timestamp).total_seconds()
    check_audio_timeline(info, expected)
    print()
    print_analysis(info, segments, end, end_source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if keep_temp:
        temp_dir = output_path.parent / f"{output_path.stem}_work"
        temp_dir.mkdir(parents=True, exist_ok=True)
        context = None
    else:
        context = tempfile.TemporaryDirectory(prefix="xmeye_convert_", dir=output_path.parent)
        temp_dir = Path(context.name)
    temporary_output = temp_dir / f"{output_path.stem}.converting.mp4"
    try:
        print("\nExtracting timestamp segments...")
        extract_segments(path, info, segments, temp_dir)
        audio_path = extract_audio(path, info, temp_dir)
        concat_path = temp_dir / "timeline.ffconcat"
        if info.audio and info.audio.codec == "pcm_alaw":
            print("\nMuxing reconstructed video (copy) and converting A-law audio to AAC...")
        else:
            print("\nMuxing reconstructed video and audio (stream copy)...")
        timestamp_segments(path, ffmpeg, ffprobe, info, segments, temp_dir)
        mux_segments(
            ffmpeg, info, segments, concat_path, temporary_output, audio_path
        )
        print("\nStrict-decoding the complete fast-path result...")
        clean, error = decode_video(ffmpeg, temporary_output)
        if not clean:
            print("Fast-path decode failed; locating damaged timeline parts...")
            temporary_output.unlink(missing_ok=True)
            recover_decoding_segments(
                path, ffmpeg, ffprobe, info, segments, temp_dir
            )
            mux_segments(
                ffmpeg, info, segments, concat_path, temporary_output, audio_path
            )
            clean, error = decode_video(ffmpeg, temporary_output)
            if not clean:
                detail = error.splitlines()[0] if error else "unknown decode error"
                raise RuntimeError(
                    f"Recovered output still fails strict decoding: {detail}"
                )
        print("\nValidating streams, durations, and packet timestamps...")
        duration, video_duration, audio_duration, packets, keys, _ = validate_output(
            ffprobe, temporary_output, info, segments, expected
        )
        dropped_gops = sum(segment.dropped_gops for segment in segments)
        dropped_frames = sum(segment.dropped_frames for segment in segments)
        os.replace(temporary_output, output_path)
        print(
            f"Validated: {packets:,} packets, {keys:,} keyframes, "
            f"video {video_duration:.3f} s, monotonic PTS/DTS"
        )
        if audio_duration is not None:
            print(
                f"Audio validated: "
                f"{'aac' if info.audio.codec == 'pcm_alaw' else info.audio.codec}, "
                f"{info.audio.sample_rate} Hz, "
                f"{info.audio.channels} ch, {audio_duration:.3f} s, "
                f"A/V difference {audio_duration - video_duration:+.3f} s"
            )
        if dropped_gops:
            print(
                f"Recovery validated: dropped {dropped_gops} damaged GOP(s), "
                f"{dropped_frames:,} XMEye frame record(s)."
            )
        print(f"Container duration: {duration:.3f} s")
        print(f"\nDone: {output_path}")
        if keep_temp:
            print(f"Temporary files kept in: {temp_dir}")
    finally:
        if context:
            context.cleanup()


def main():
    parser = argparse.ArgumentParser(description=(
        "Convert XMEye DVRIP recording to MP4 without re-encoding video, "
        "using FC/GOP timestamps for the timeline."
    ))
    parser.add_argument("input", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--anchor-interval", type=float, default=DEFAULT_ANCHOR_INTERVAL,
                        help="Maximum seconds between FC interpolation anchors (default: 60).")
    parser.add_argument("--transition-min-seconds", type=float,
                        default=DEFAULT_TRANSITION_MIN_SECONDS,
                        help="Minimum persistent FC mode/FPS change (default: 10).")
    parser.add_argument("--fps", type=float, default=None,
                        help="Explicit constant-FPS override for diagnostics/legacy use.")
    parser.add_argument("--real-time", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()
    if args.anchor_interval < MIN_SEGMENT_SECONDS:
        parser.error(f"--anchor-interval must be at least {MIN_SEGMENT_SECONDS:g} seconds.")
    if args.transition_min_seconds < 0:
        parser.error("--transition-min-seconds cannot be negative.")
    if args.fps is not None and args.fps <= 0:
        parser.error("--fps must be greater than zero.")
    path = args.input.resolve()
    if not path.is_file():
        parser.error(f"Input file does not exist: {path}")
    output = args.output.resolve() if args.output else path.with_suffix(".mp4")
    if path == output:
        parser.error("Input and output paths must differ.")
    if output.suffix.lower() != ".mp4":
        parser.error("Output must use the .mp4 extension.")
    try:
        convert(path, output, args.keep_temp, args.anchor_interval,
                args.transition_min_seconds, args.fps)
    except subprocess.CalledProcessError as exc:
        print(f"\nFFmpeg/ffprobe failed with exit code {exc.returncode}.", file=sys.stderr)
        sys.exit(exc.returncode or 1)
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
