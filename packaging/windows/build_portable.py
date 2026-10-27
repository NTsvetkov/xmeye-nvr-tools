#!/usr/bin/env python3
"""Build a Windows x64 portable ZIP using an existing FFmpeg distribution."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def copy_licenses(destination):
    for name in (
        "PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6",
        "PyInstaller",
    ):
        distribution = importlib.metadata.distribution(name)
        for entry in distribution.files or []:
            parts = entry.parts
            if not any(part.endswith(".dist-info") for part in parts):
                continue
            if not any("license" in part.lower() or "copying" in part.lower()
                       for part in parts):
                continue
            target = destination / name / Path(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(distribution.locate_file(entry), target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Python license not found: {python_license}")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ffmpeg-dir", type=Path, required=True,
        help="Extracted FFmpeg package containing bin/, LICENSE and README.txt",
    )
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        parser.error("Build with 64-bit Python on Windows.")
    vendor = args.ffmpeg_dir.resolve()
    required = [vendor / "bin" / name for name in ("ffmpeg.exe", "ffprobe.exe")]
    required.extend([vendor / "LICENSE", vendor / "README.txt"])
    for path in required:
        if not path.is_file():
            parser.error(f"Required FFmpeg package file missing: {path}")
    # Check both binaries before building; source-side PATH is not used.
    versions = {}
    for tool in required[:2]:
        result = subprocess.run(
            [str(tool), "-version"], check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        versions[tool.name] = result.stdout.strip()

    build_env = os.environ.copy()
    windows = Path(build_env.get("SystemRoot", "C:/Windows"))
    # Keep unrelated tools from supplying same-named, incompatible Windows DLLs.
    build_env["PATH"] = os.pathsep.join(map(str, (
        Path(sys.executable).parent, Path(sys.base_prefix),
        windows / "System32", windows,
    )))
    for name in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH",
                 "QT_QPA_PLATFORM_PLUGIN_PATH"):
        build_env.pop(name, None)
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "windows"),
        str(ROOT / "packaging" / "windows" / "XMEyeNVR.spec"),
    ], check=True, cwd=ROOT, env=build_env)
    bundle = ROOT / "dist" / "XMEyeNVR"
    tools = bundle / "bin"
    tools.mkdir(exist_ok=True)
    for path in required[:2]:
        shutil.copy2(path, tools / path.name)
    # Shared FFmpeg distributions also need their DLLs next to the tools.
    for path in (vendor / "bin").glob("*.dll"):
        shutil.copy2(path, tools / path.name)
    notices = bundle / "licenses"
    notices.mkdir(exist_ok=True)
    shutil.copy2(vendor / "LICENSE", notices / "FFmpeg-LICENSE.txt")
    shutil.copy2(vendor / "README.txt", notices / "FFmpeg-README.txt")
    copy_licenses(notices)
    shutil.copytree(
        ROOT / "packaging" / "windows" / "licenses", notices / "open-source",
        dirs_exist_ok=True,
    )
    for name in ("LICENSE", "README.md", "README.bg.md"):
        shutil.copy2(ROOT / name, bundle / name)
    shutil.copytree(ROOT / "docs", bundle / "docs", dirs_exist_ok=True)
    shutil.copy2(
        ROOT / "packaging" / "windows" / "THIRD_PARTY.md",
        bundle / "THIRD_PARTY.md",
    )
    shutil.copytree(
        ROOT / "packaging" / "windows", bundle / "packaging" / "windows",
        dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"),
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "diff", "HEAD", "--name-only"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip())
    manifest = {
        "git_revision": revision, "tracked_changes": dirty,
        "python": platform.python_version(), "architecture": "Windows x64",
        "packages": {name: importlib.metadata.version(name) for name in (
            "PyInstaller", "PySide6", "PySide6_Essentials", "shiboken6",
        )},
        "tools": versions,
        "sha256": {
            path.relative_to(bundle).as_posix(): sha256(path)
            for path in sorted(bundle.rglob("*.exe"))
        },
    }
    (bundle / "BUILD.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    archive = shutil.make_archive(
        str(ROOT / "dist" / "XMEyeNVR-windows-x64"), "zip",
        root_dir=bundle.parent, base_dir=bundle.name,
    )
    print(f"Portable ZIP: {archive}")


if __name__ == "__main__":
    main()
