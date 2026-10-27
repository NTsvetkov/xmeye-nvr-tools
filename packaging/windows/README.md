# Building the Windows portable ZIP

Build on Windows 10 (1809 or later) / Windows 11 x64 with Python 3.12.
The application remains usable from
source; freezing changes only how the GUI finds the converter and its tools.

Create a dedicated environment and install pinned build dependencies:

```powershell
python -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install -r packaging/windows/requirements-build.txt
.venv-build\Scripts\python.exe packaging/windows/build_portable.py --ffmpeg-dir C:\path\to\ffmpeg-essentials_build
```

Use the extracted FFmpeg package folder, with bin/ffmpeg.exe, bin/ffprobe.exe,
LICENSE and README.txt. Obtain a Gyan Essentials build through the providers
linked from https://ffmpeg.org/download.html. The build script uses these exact
binaries and copies their license and build information into the package.

Output: dist/XMEyeNVR-windows-x64.zip. Extract the whole archive and launch
XMEyeNVR/XMEyeNVR.exe. Do not move the executable out of its folder.
XMEyeConvert.exe is a console helper launched hidden by the GUI. Both programs
share _internal/, while FFmpeg and ffprobe are in bin/. Python and PATH changes
are not required on the destination PC.

The script replaces its own previous dist/XMEyeNVR build. It does not package
downloads, credentials, QSettings, the virtual environment, or repository files
outside the explicit application documentation/license files.

Before publication, validate the extracted ZIP on a clean Windows x64 machine
without Python or FFmpeg: launch, search, download, full/fast conversion with
audio, cancellation/cleanup, and graceful shutdown. Review third-party source
redistribution requirements in THIRD_PARTY.md. A build alone is not evidence
that live NVR operations work on a clean PC.
