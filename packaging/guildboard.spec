# PyInstaller recipe for the core of the installed copy of TUSKFiZZY. Run by build-installer.ps1, which first builds the
# interface (web/dist) and writes build/package/version.txt.
#
# One folder, named core, with one program in it: tuskfizzy-core.exe. The Electron shell starts it hidden, with the channel
# on its standard input and output, so it is a console program. shell/build/package.mjs puts the folder into the shell's
# resources, at resources\core\tuskfizzy-core.exe, where the shell looks (shell/src/paths.ts).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent
ICON = str(ROOT / "shell" / "assets" / "icon.ico")
SRC = ROOT / "src" / "guildboard"

datas = [
    (str(ROOT / "web" / "dist"), "web/dist"),
    (str(SRC / "reference"), "guildboard/reference"),
    (str(SRC / "characters" / "demo"), "guildboard/characters/demo"),
    # Settings, About shows the notices from here (notices.py); the licence and the privacy notice go with them.
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD-PARTY-NOTICES.txt"), "."),
    (str(ROOT / "PRIVACY.md"), "."),
]
binaries = []
hiddenimports = []
for package in ("genshin", "guildboard"):
    found = collect_all(package)
    datas += found[0]
    binaries += found[1]
    hiddenimports += found[2]

core = Analysis(
    [str(Path(SPECPATH) / "core_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PySide6", "shiboken6"],
)

COLLECT(
    EXE(
        PYZ(core.pure),
        core.scripts,
        [],
        exclude_binaries=True,
        name="tuskfizzy-core",
        icon=ICON,
        version=str(ROOT / "build" / "package" / "version.txt"),
        console=True,
        upx=False,
    ),
    core.binaries,
    core.datas,
    upx=False,
    name="core",
)
