# Changelog

Changes are listed newest first. Earlier entries summarize the published GitHub release notes.

## [1.2.1] - 2026-11-14

### Fixed
- MP4 conversion now tolerates a recording filename ending up to one second before the final FC keyframe timestamp. It warns and infers the final GOP end instead of rejecting the entire recording.
- All video frames are retained for this boundary correction; larger negative timestamp discrepancies are still rejected.

### Added
- Six regression tests covering filename/FC end boundaries and the existing start-timestamp validation.
- This changelog, linked from both READMEs and included in the Windows portable package.

### Verified
- The affected real recording passed full strict decoding: 19,600 video packets, 490 keyframes, 980 seconds, no dropped GOPs, and a 0.224-second audio/video duration difference.

## [1.2.0] - 2026-10-27

### Added
- Windows x64 portable ZIP containing the GUI, converter, Python/Qt runtime, FFmpeg/ffprobe, licenses, build manifest, and SHA-256 checksum.
- Queue context-menu actions to open a local video and show a recording in Explorer.
- English and Bulgarian READMEs and GUI guides.

### Fixed
- Unicode paths in FFmpeg concat files and executable discovery in portable builds.
- Build isolation prevents incompatible ICU DLLs from unrelated developer tools entering the package.

## [1.1.0] - 2026-10-27

### Added
- Desktop search and download queue with dark/light themes, per-recording controls, and pause/resume.
- Session-only GUI password entry, MP4 output with video stream copy and supported audio, and optional fast conversion.
- Clearer search/queue navigation; Download now opens the download queue.

### Fixed
- Recorder-pause timing for GOPs of different sizes, preventing accelerated video and lost audio in the validated pause scenario.
- Damaged-segment recovery, structural repair/retry, cancellation cleanup, and sanitizer resynchronization.

[1.2.1]: https://github.com/NTsvetkov/xmeye-nvr-tools/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/NTsvetkov/xmeye-nvr-tools/releases/tag/v1.2.0
[1.1.0]: https://github.com/NTsvetkov/xmeye-nvr-tools/releases/tag/v1.1.0
