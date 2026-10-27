"""Two executables sharing one portable Python/Qt runtime directory."""
from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]
scripts = root / "scripts"

gui = Analysis([str(scripts / "nvr_gui.py")], pathex=[str(scripts)])
converter = Analysis([str(scripts / "xmeye_convert.py")], pathex=[str(scripts)])

# This pinned Qt build imports Windows' system ICU API, not a version-suffixed
# third-party ICU. Do not let a same-named DLL from the build host shadow it.
gui.binaries = [
    entry for entry in gui.binaries
    if Path(entry[0]).name.lower() not in {"icuuc.dll", "icudt78.dll"}
]

gui_exe = EXE(
    PYZ(gui.pure), gui.scripts, [("X utf8", None, "OPTION")], exclude_binaries=True,
    name="XMEyeNVR", console=False, upx=False,
)
converter_exe = EXE(
    PYZ(converter.pure), converter.scripts, [("X utf8", None, "OPTION")], exclude_binaries=True,
    name="XMEyeConvert", console=True, upx=False,
)
COLLECT(
    gui_exe, converter_exe,
    gui.binaries, gui.datas, converter.binaries, converter.datas,
    name="XMEyeNVR", upx=False,
)
