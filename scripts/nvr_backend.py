#!/usr/bin/env python3
"""Reusable operations shared by the XMEye CLI and desktop GUI."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from nvr_fetch import (
    DVRIPClient,
    Recording,
    disk_free_info,
    format_size,
    safe_filename,
)
from xmeye_repair import default_output_path, sanitize_file


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, float], None]


@dataclass(frozen=True)
class ConnectionSettings:
    host: str
    port: int
    username: str
    password: str


@dataclass(frozen=True)
class RecordingRef:
    channel: int
    recording: Recording

    @property
    def key(self) -> tuple[int, str]:
        return self.channel, self.recording.filename


@dataclass(frozen=True)
class LocalPaths:
    raw: Path
    mp4: Path
    legacy_mkv: Path


@dataclass(frozen=True)
class DownloadOutcome:
    raw_path: Path
    mp4_path: Path
    downloaded: bool
    conversion_needed: bool


@dataclass(frozen=True)
class ConversionOutcome:
    output_path: Path
    repaired: bool


class CancelledError(RuntimeError):
    pass


class LowDiskSpaceError(RuntimeError):
    pass


class ConversionProcessError(RuntimeError):
    def __init__(self, returncode: int, details: str):
        self.returncode = returncode
        self.details = details
        super().__init__(
            f"Conversion failed with exit code {returncode}"
            + (f": {details}" if details else ".")
        )


class OperationCancellation:
    """Event-compatible cancellation token with immediate interrupt hooks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: set[Callable[[], None]] = set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def clear(self) -> None:
        self._event.clear()

    def set(self) -> None:
        self._event.set()
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def register(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                call_now = True
            else:
                self._callbacks.add(callback)
                call_now = False
        if call_now:
            callback()

        def unregister() -> None:
            with self._lock:
                self._callbacks.discard(callback)

        return unregister


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Operation cancelled.")


def local_paths(item: RecordingRef, output_dir: Path) -> LocalPaths:
    raw = output_dir / safe_filename(item.recording, item.channel)
    return LocalPaths(
        raw=raw,
        mp4=raw.with_suffix(".mp4"),
        legacy_mkv=raw.with_suffix(".mkv"),
    )


def local_status(item: RecordingRef, output_dir: Path) -> str:
    paths = local_paths(item, output_dir)
    raw_present = paths.raw.is_file()
    mp4_present = paths.mp4.is_file()
    legacy_mkv_present = paths.legacy_mkv.is_file()
    if raw_present and mp4_present and legacy_mkv_present:
        return "raw + MP4 + legacy MKV"
    if mp4_present and legacy_mkv_present:
        return "MP4 + legacy MKV"
    if raw_present and mp4_present:
        return "raw + MP4"
    if raw_present and legacy_mkv_present:
        return "raw + legacy MKV"
    if raw_present:
        return "raw present"
    if mp4_present:
        return "MP4 present"
    if legacy_mkv_present:
        return "legacy MKV present"
    return "not downloaded"


def search_recordings(
    settings: ConnectionSettings,
    channels: Iterable[int],
    begin: datetime,
    end: datetime,
    log: LogCallback,
    cancel_event: OperationCancellation | threading.Event | None = None,
) -> list[RecordingRef]:
    channels = list(channels)
    results: list[RecordingRef] = []
    _check_cancel(cancel_event)
    log(f"Logging in to {settings.host}:{settings.port}...")
    with DVRIPClient(settings.host, settings.port) as client:
        unregister = (
            cancel_event.register(client.close)
            if isinstance(cancel_event, OperationCancellation)
            else lambda: None
        )
        try:
            client.login(settings.username, settings.password)
            log("Login successful.")
            for position, channel in enumerate(channels, 1):
                _check_cancel(cancel_event)
                log(
                    f"Searching Camera {channel + 1} "
                    f"({position}/{len(channels)})..."
                )

                def page_progress(page: int, count: int, camera=channel) -> None:
                    _check_cancel(cancel_event)
                    log(
                        f"Camera {camera + 1}: page {page}, "
                        f"{count} recording(s)."
                    )

                found = client.search_recordings(
                    channel,
                    begin,
                    end,
                    progress_callback=page_progress,
                    cancel_event=cancel_event,
                )
                results.extend(RecordingRef(channel, recording) for recording in found)
                log(f"Camera {channel + 1}: {len(found)} recording(s) total.")
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("Search cancelled.") from exc
            raise
        finally:
            unregister()

    results.sort(key=lambda item: (item.recording.begin, item.channel))
    log(f"Search complete: {len(results)} recording(s) across all cameras.")
    return results


def prepare_download(
    item: RecordingRef,
    settings: ConnectionSettings,
    output_dir: Path,
    convert: bool,
    min_free_percent: float,
    cancel_event: OperationCancellation | threading.Event,
    progress: ProgressCallback,
    log: LogCallback,
    timeout: float = 120.0,
    retries: int = 3,
    force: bool = False,
) -> DownloadOutcome:
    """Download one recording with a fresh DVRIP session per attempt."""
    _check_cancel(cancel_event)
    paths = local_paths(item, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not force and convert and paths.mp4.is_file():
        log(f"MP4 already exists; skipping download: {paths.mp4.name}")
        return DownloadOutcome(paths.raw, paths.mp4, False, False)

    if not force and paths.raw.is_file():
        # Downloads are written to .part and atomically published as .xmeye
        # only after the NVR sends its clean end marker.  FileLength is not a
        # reliable completion check on these devices, so the final filename
        # itself is the durable completion state.
        log(f"Raw recording already exists: {paths.raw.name}")
        return DownloadOutcome(paths.raw, paths.mp4, False, convert)

    free, total, percent = disk_free_info(output_dir)
    if min_free_percent > 0 and percent < min_free_percent:
        raise LowDiskSpaceError(
            f"Free disk space is {format_size(free)} / {format_size(total)} "
            f"({percent:.2f}%), below the {min_free_percent:.2f}% minimum."
        )

    part_path = paths.raw.with_name(paths.raw.name + ".part")
    for attempt in range(1, retries + 1):
        _check_cancel(cancel_event)
        log(
            f"Downloading Camera {item.channel + 1} {paths.raw.name} "
            f"(attempt {attempt}/{retries})..."
        )
        try:
            with DVRIPClient(settings.host, settings.port) as client:
                unregister = (
                    cancel_event.register(client.close)
                    if isinstance(cancel_event, OperationCancellation)
                    else lambda: None
                )
                try:
                    client.login(settings.username, settings.password)
                    client.download_recording(
                        item.recording,
                        item.channel,
                        part_path,
                        timeout=timeout,
                        progress_callback=progress,
                        cancel_event=cancel_event,
                        record_log_callback=log,
                    )
                finally:
                    unregister()
            _check_cancel(cancel_event)
            os.replace(part_path, paths.raw)
            log(f"Download complete: {paths.raw.name}")
            return DownloadOutcome(paths.raw, paths.mp4, True, convert)
        except InterruptedError as exc:
            part_path.unlink(missing_ok=True)
            raise CancelledError(str(exc)) from exc
        except Exception:
            if cancel_event.is_set():
                part_path.unlink(missing_ok=True)
                raise CancelledError("Download cancelled.")
            if attempt >= retries:
                raise
            log("Download failed; retrying with a fresh DVRIP session...")
            time.sleep(1.0)

    raise RuntimeError("Download failed.")


def _run_converter_process(
    converter: Path,
    input_path: Path,
    output_path: Path,
    cancel_event: threading.Event,
) -> None:
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    error_output = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(converter), str(input_path), str(output_path)],
        stdout=subprocess.DEVNULL,
        stderr=error_output,
        creationflags=creation_flags,
        start_new_session=sys.platform != "win32",
    )

    def terminate_process_tree() -> None:
        if process.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    try:
        while process.poll() is None:
            if cancel_event.wait(0.05):
                terminate_process_tree()
                raise CancelledError("Conversion cancelled.")
        if process.returncode != 0:
            error_output.seek(0)
            details = error_output.read().strip()
            raise ConversionProcessError(process.returncode, details)
    finally:
        if process.poll() is None:
            terminate_process_tree()
        error_output.close()


def convert_recording(
    raw_path: Path,
    cancel_event: threading.Event,
    log: LogCallback,
    force: bool = False,
) -> ConversionOutcome:
    """Convert once, then sanitize structural damage and retry exactly once."""
    _check_cancel(cancel_event)
    converter = Path(__file__).with_name("xmeye_convert.py")
    output_path = raw_path.with_suffix(".mp4")
    if output_path.is_file() and not force:
        log(f"MP4 already exists; skipping conversion: {output_path.name}")
        return ConversionOutcome(output_path, False)

    log(f"Converting to MP4: {output_path.name}")
    repaired = False
    try:
        _run_converter_process(converter, raw_path, output_path, cancel_event)
    except ConversionProcessError as first_error:
        repairable = (
            "Unexpected non-frame data" in first_error.details
            or "Truncated XMEye frame" in first_error.details
        )
        if not repairable:
            raise
        log("Structural XMEye damage detected; repairing a temporary copy...")
        try:
            with tempfile.TemporaryDirectory(
                prefix=".xmeye_repair_", dir=raw_path.parent
            ) as repair_dir:
                repaired_path = Path(repair_dir) / raw_path.name
                _, summary = repair_recording(
                    raw_path, repaired_path, cancel_event=cancel_event
                )
                log(summary)
                _check_cancel(cancel_event)
                log("Retrying conversion from the repaired copy...")
                _run_converter_process(
                    converter, repaired_path, output_path, cancel_event
                )
                repaired = True
        except (CancelledError, InterruptedError):
            raise
        except Exception as retry_error:
            raise RuntimeError(
                f"{first_error} Automatic repair/retry also failed: {retry_error}"
            ) from retry_error

    if not output_path.is_file():
        raise RuntimeError("Conversion finished without creating the MP4 file.")
    log(f"Conversion complete: {output_path.name}")
    return ConversionOutcome(output_path, repaired)


def repair_recording(
    input_path: Path,
    output_path: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[Path, str]:
    input_path = input_path.resolve()
    output_path = (output_path or default_output_path(input_path)).resolve()
    if output_path == input_path:
        raise ValueError("Repair output must be different from the input file.")
    messages = []
    stats = sanitize_file(
        input_path, output_path, messages.append, cancel_event=cancel_event
    )
    if stats.frame_count <= 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("No complete XMEye frames were found.")
    summary = (
        f"Repaired {stats.frame_count} complete frame(s), "
        f"wrote {format_size(stats.output_bytes)}; "
        f"skipped {format_size(stats.skipped_bytes)}."
    )
    if messages:
        summary += " " + " ".join(messages)
    return output_path, summary
