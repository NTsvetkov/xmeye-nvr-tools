# Third-party components

The application source is MIT licensed; see LICENSE. The portable ZIP also
contains separate third-party programs and libraries under their own licenses.
Their license texts are in licenses/ and the exact versions and executable
SHA-256 hashes are in BUILD.json.

- Python: https://www.python.org/ and https://www.python.org/downloads/source/
- PySide6, Shiboken and Qt: https://www.qt.io/qt-for-python and
  https://code.qt.io/pyside/pyside-setup.git/
  Qt sources: https://download.qt.io/official_releases/qt/
- PyInstaller: https://pyinstaller.org/ and
  https://github.com/pyinstaller/pyinstaller (bootloader license exception).
- FFmpeg and ffprobe are independent command-line programs in bin/. Their
  build configuration and version are recorded in BUILD.json. The supplied
  Gyan Essentials build is GPL version 3 or later, as described in
  licenses/FFmpeg-LICENSE.txt and licenses/FFmpeg-README.txt.
  Build provider: https://www.gyan.dev/ffmpeg/builds/
  FFmpeg sources: see the exact source commit in licenses/FFmpeg-README.txt
  (8.1.1: https://github.com/FFmpeg/FFmpeg/commit/239f2c733d).
  The provider README also lists the included external libraries.

PySide6/Qt are used under their open-source LGPLv3 terms. The LGPLv3 and GPLv3
texts are included in licenses/open-source/. The commercial text copied from
the wheel describes an alternative license; no commercial Qt license is needed
to use this open-source package under its applicable LGPL terms.

Qt libraries remain separate DLLs in _internal/ so they can be replaced with
compatible versions. Reverse engineering to debug modifications to LGPL
components is permitted under the applicable LGPL terms. Keep the whole folder
together and retain these notices.
Before redistributing a modified bundle, provide the corresponding source and
build information required by the licenses of the exact third-party binaries
you distribute. This local build does not upload or publish any binaries.
