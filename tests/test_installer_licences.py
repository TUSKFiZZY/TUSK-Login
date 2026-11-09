"""The installed copy carries Guild Board's licence, the notices of the software inside it and the privacy notice."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_installed_copy_carries_the_licence_the_notices_and_the_privacy_notice():
    spec = (ROOT / "packaging" / "guildboard.spec").read_text("utf-8")
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.txt", "PRIVACY.md"):
        assert f'(str(ROOT / "{name}"), ".")' in spec, name
    recipe = (ROOT / "packaging" / "installer.iss").read_text("utf-8")
    assert re.search(r"^LicenseFile=\{#Root\}\\LICENSE$", recipe, re.MULTILINE)  # a page of its own
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.txt", "PRIVACY.md"):
        assert re.search(rf'^Source: "\{{#Root\}}\\{re.escape(name)}"; DestDir: "\{{app\}}"', recipe, re.MULTILINE), name


def test_electrons_own_licence_files_stay_beside_the_program_and_guild_boards_licence_is_named_apart():
    # The packed shell folder carries Electron's LICENSE and LICENSES.chromium.html, which the notices say lie beside the
    # program; Guild Board's own licence must not be written over the first.
    recipe = (ROOT / "packaging" / "installer.iss").read_text("utf-8")
    assert re.search(r'^Source: "\{#Root\}\\LICENSE"; DestDir: "\{app\}"; DestName: "LICENSE-TUSKFiZZY.txt"', recipe, re.MULTILINE)
    assert r'#define Built Root + "\shell\out\TUSKFiZZY-win32-x64"' in recipe
