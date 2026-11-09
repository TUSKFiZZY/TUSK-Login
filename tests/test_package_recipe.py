"""The installer's recipes: the Electron shell packed with the frozen core, without Qt."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _text(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text("utf-8")


def test_the_core_is_one_console_program_in_a_folder_named_core_and_carries_no_qt():
    spec = _text("packaging", "guildboard.spec")
    assert 'name="tuskfizzy-core"' in spec and "console=True" in spec
    assert re.search(r'^\s+name="core",$', spec, re.MULTILINE)  # shell/src/paths.ts: resources\core\tuskfizzy-core.exe
    assert "core_entry.py" in spec and "PySide6" in spec.split("excludes=", 1)[1].split("]", 1)[0]
    assert "tuskfizzy-app" not in spec and "tuskfizzy-tray" not in spec
    assert "main()" in _text("packaging", "core_entry.py")


def test_the_shell_is_packed_with_the_six_fuses_and_only_what_it_runs():
    script = _text("shell", "build", "package.mjs")
    for fuse, state in (
        ("RunAsNode", "false"), ("EnableNodeOptionsEnvironmentVariable", "false"), ("EnableNodeCliInspectArguments", "false"),
        ("EnableEmbeddedAsarIntegrityValidation", "true"), ("OnlyLoadAppFromAsar", "true"), ("EnableCookieEncryption", "true"),
    ):
        assert f"[FuseV1Options.{fuse}]: {state}" in script, fuse
    assert "executableName: 'tuskfizzy-app'" in script and "asar: true" in script and "extraResource: [core, join(shellRoot, 'assets', 'icon.ico')]" in script
    assert "'/package.json'" in script
    # The tray's icon is a plain file in the resources: Electron copies an .ico out of an asar into a temporary file.
    assert "'/assets" not in script


def test_the_shell_build_empties_dist_first_so_a_removed_page_is_not_packed():
    build = json.loads(_text("shell", "package.json"))["scripts"]["build"]
    assert build.startswith("node scripts/clean-dist.mjs && tsc -p .")
    assert "rmSync" in _text("shell", "scripts", "clean-dist.mjs")


@pytest.mark.skipif(
    shutil.which("node") is None or not (ROOT / "shell" / "node_modules" / "@electron" / "packager").exists(),
    reason="needs Node.js and the shell's packages",
)
def test_packing_stops_unless_the_core_folder_is_named_core(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "tuskfizzy-core.exe").write_bytes(b"MZ")
    done = subprocess.run(
        ["node", "build/package.mjs", "--core", str(other), "--version", "2.0.0"],
        cwd=ROOT / "shell", capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 2 and "must be named core" in done.stderr
    assert not (ROOT / "shell" / "out" / "TUSKFiZZY-win32-x64" / "resources" / "other").exists()


def test_the_installer_removes_what_the_old_build_left_and_the_build_script_packs_the_shell_last():
    recipe = _text("packaging", "installer.iss")
    assert r'Name: "{app}\_internal"' in recipe and r'Name: "{app}\tuskfizzy-tray.exe"' in recipe
    assert r'Name: "{app}\resources\core"' in recipe and r'Name: "{app}\resources"' not in recipe  # a shared folder's own resources stay
    build = _text("build-installer.ps1")
    # A TUSKFiZZY running from the checkout's Electron program stops the build before npm ci deletes it.
    # It opens the file for writing, shared with all, and only an IOException (the file being in use) stops the build; it is
    # checked again just before npm ci of the shell.
    assert "[IO.FileAccess]::Write, [IO.FileShare]'ReadWrite, Delete'" in build and "catch [System.IO.IOException]" in build
    assert "FileShare]::None" not in build and "catch {" not in build.split("function Assert-ShellNotRunning")[1].split("Assert-ShellNotRunning\n")[0]
    assert build.index("\nAssert-ShellNotRunning") < build.index("npm ci --prefix web")
    assert build.count("Assert-ShellNotRunning") == 3  # the function and two calls
    assert build.rindex("Assert-ShellNotRunning") < build.index("npm ci --prefix shell")
    assert "make_icon" not in build and "QT_QPA_PLATFORM" not in build
    # The shell's unit tests run right after its packages are installed, before anything is built from them.
    steps = ("pyinstaller", "npm ci --prefix shell", "npm test --prefix shell", "npm run build --prefix shell", "node build\\package.mjs", "'Make the installer'")
    order = [build.index(step) for step in steps]
    assert order == sorted(order)


def test_the_build_stops_while_a_guild_board_runs_from_either_program_of_the_checkout():
    # setup.ps1's desktop shortcut starts the copy of Electron's program named for TUSKFiZZY (shell\app-dev), and npm start
    # runs Electron's own. The build replaces what a TUSKFiZZY running from either uses, and npm ci deletes the second.
    build = _text("build-installer.ps1")
    guard = build.split("function Assert-ShellNotRunning")[1].split("\nAssert-ShellNotRunning")[0]
    assert "foreach ($program in 'shell\\app-dev\\electron.exe', 'shell\\node_modules\\electron\\dist\\electron.exe')" in guard
    assert "$path = Join-Path $PSScriptRoot $program" in guard
    assert "[IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]'ReadWrite, Delete')" in guard
    assert "($program is in use)" in guard and "exit 1" in guard
