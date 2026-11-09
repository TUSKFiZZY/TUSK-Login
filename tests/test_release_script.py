"""release.ps1 and the release key's tools, as far as they run without GitHub: the signature they make is the one the
installed copies check (H4 of the audit of 2026-11-01), run here in Windows PowerShell as the player runs them.
release.ps1 itself is for a private build and is not in the public tree, where its tests are skipped; the tools they
share, tools/release-signing.ps1, are."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from guildboard import netpolicy, selfupdate
from guildboard.selfupdate import Release, newer_release

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RELEASE = ROOT / "release.ps1"
ASSET = "https://api.github.com/repos/TUSKFiZZY/TUSK-Login/releases/assets/123"
SIGNATURE_ASSET = "https://api.github.com/repos/TUSKFiZZY/TUSK-Login/releases/assets/124"
# Where 2.0.0 and earlier look, and the bridge release 2.0.1 reached them.
OLD_ASSET = "https://api.github.com/repos/QL1STY/guild-board-releases/releases/assets/123"
NAME = "TUSKFiZZY.Setup.0.2.0.exe"  # as GitHub names the uploaded "TUSKFiZZY Setup 0.2.0.exe"

windows_powershell = pytest.mark.skipif(shutil.which("powershell") is None, reason="needs Windows PowerShell")
private_build = pytest.mark.skipif(not RELEASE.exists(), reason="release.ps1 is not in the public tree")

# Signs a draft as release.ps1 does, with the library it loads: GitHub's answer for the draft, the installer that was
# uploaded, the key's file, the key the app trusts and the folder for the signature.
SIGN = r"""
param($Tools, $Release, $Installer, $Key, $Folder, [string]$Trusted = '')
$ErrorActionPreference = 'Stop'
. (Join-Path $Tools 'release-signing.ps1')
New-ReleaseSignatureFile -Json ([IO.File]::ReadAllText($Release)) -Installer $Installer -KeyFile $Key `
    -Trusted $Trusted -Folder $Folder
"""


def _powershell(*arguments: str) -> subprocess.CompletedProcess:
    # Windows PowerShell writes to a pipe in the console's code page, in which Windows words its own messages.
    return subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *arguments],
        capture_output=True, encoding="oem", errors="replace", timeout=120,
    )


def _make_key(path: Path) -> str:
    """A release key made by tools/make-release-key.ps1, and the line it prints for the app."""
    made = _powershell("-File", str(TOOLS / "make-release-key.ps1"), "-Path", str(path))
    assert made.returncode == 0, made.stdout + made.stderr
    [public] = re.findall(r'^RELEASE_KEY = "(04[0-9a-f]{128})"$', made.stdout, flags=re.MULTILINE)
    return public


def _draft(tmp_path: Path, **asset) -> tuple[Path, Path]:
    """An installer, and GitHub's answer for the draft it was uploaded to, before its signature is."""
    installer = tmp_path / "TUSKFiZZY Setup 0.2.0.exe"
    installer.write_bytes(b"MZ the installer")
    facts = {"name": NAME, "url": ASSET, "size": installer.stat().st_size,
             "digest": "sha256:" + hashlib.sha256(installer.read_bytes()).hexdigest()}
    facts.update(asset)
    answer = tmp_path / "draft.json"
    answer.write_text(json.dumps({
        "tag_name": "v0.2.0", "draft": True, "prerelease": False, "body": "- [New] Pins: Ünïcode is fine.",
        "assets": [facts],
    }), encoding="utf-8")
    return installer, answer


def _sign(tmp_path: Path, answer: Path, installer: Path, key: Path, trusted: str) -> subprocess.CompletedProcess:
    """`trusted` empty stands for a selfupdate.py whose key was never made."""
    script = tmp_path / "sign.ps1"
    script.write_text(SIGN, encoding="utf-8")
    (tmp_path / "out").mkdir(exist_ok=True)
    return _powershell(
        "-File", str(script), "-Tools", str(TOOLS), "-Release", str(answer), "-Installer", str(installer),
        "-Key", str(key), "-Folder", str(tmp_path / "out"), *(("-Trusted", trusted) if trusted else ()),
    )


@windows_powershell
def test_the_release_scripts_signature_is_the_one_the_app_checks(tmp_path):
    key = tmp_path / "keys" / "release.pem"
    public = _make_key(key)
    pem = key.read_text(encoding="ascii")
    assert pem.startswith("-----BEGIN PRIVATE KEY-----\n") and pem.endswith("-----END PRIVATE KEY-----\n")
    installer, answer = _draft(tmp_path)
    signed = _sign(tmp_path, answer, installer, key, public)
    assert signed.returncode == 0, signed.stdout + signed.stderr
    signature_file = tmp_path / "out" / f"{NAME}.sig"  # named for the installer as GitHub names it
    assert signed.stdout.strip() == str(signature_file)
    signature = signature_file.read_bytes()
    assert len(signature) == 64
    # Published with its signature beside the installer, the release is one the app offers, under that key alone.
    published = json.loads(answer.read_text(encoding="utf-8"))
    published["draft"] = False
    published["assets"].append({
        "name": f"{NAME}.sig", "url": SIGNATURE_ASSET, "size": 64,
        "digest": "sha256:" + hashlib.sha256(signature).hexdigest(),
    })
    body = json.dumps(published).encode()
    size, sha = installer.stat().st_size, hashlib.sha256(installer.read_bytes()).hexdigest()
    assert newer_release(body, "0.1.0", signature=lambda *asset: signature, key=public) == Release(
        "0.2.0", ASSET, size, sha, ({"kind": "new", "title": "Pins", "text": "Ünïcode is fine."},),
    )
    assert newer_release(body, "0.1.0", signature=lambda *asset: signature) is None  # not under the pinned key


@windows_powershell
def test_the_release_script_signs_only_what_github_shows_and_only_with_the_key_the_app_trusts(tmp_path):
    key, other = tmp_path / "keys" / "release.pem", tmp_path / "keys" / "other.pem"
    public, other_public = _make_key(key), _make_key(other)
    installer, answer = _draft(tmp_path)
    # A key the app does not trust, or no key pinned yet: nothing is signed.
    for trusted in (other_public, ""):
        refused = _sign(tmp_path, answer, installer, key, trusted)
        assert refused.returncode != 0 and "RELEASE_KEY" in refused.stdout + refused.stderr
    # GitHub showing another file than the one uploaded: nothing is signed.
    installer, answer = _draft(tmp_path, digest="sha256:" + "0" * 64)
    refused = _sign(tmp_path, answer, installer, key, public)
    assert refused.returncode != 0 and "is not the one that was uploaded" in refused.stdout + refused.stderr
    # Two installers: the app takes the first, and the script will not guess.
    installer, answer = _draft(tmp_path)
    draft = json.loads(answer.read_text(encoding="utf-8"))
    draft["assets"].append(dict(draft["assets"][0], name="Second.exe"))
    answer.write_text(json.dumps(draft), encoding="utf-8")
    refused = _sign(tmp_path, answer, installer, key, public)
    assert refused.returncode != 0 and "more than one installer" in refused.stdout + refused.stderr
    # Nothing is signed for a draft that is not as GitHub shows an installer: its checksum in capitals (the app would
    # lower-case the hex, but the script asks for the text GitHub gives, with -cne) or under another "sha256:" prefix
    # (which the app does not read), an address outside the releases repository, a size beyond what installed copies
    # take.
    hexdigest = hashlib.sha256(b"MZ the installer").hexdigest()
    for asset, said in (
        ({"digest": "sha256:" + hexdigest.upper()}, "is not the one that was uploaded"),
        ({"digest": "SHA256:" + hexdigest}, "is not the one that was uploaded"),
        ({"url": "https://api.github.com/repos/someone/else/releases/assets/123"}, "installed copies do not download"),
        ({"size": selfupdate.MAX_INSTALLER_BYTES + 1}, "installed copies take at most"),
    ):
        installer, answer = _draft(tmp_path, **asset)
        refused = _sign(tmp_path, answer, installer, key, public)
        assert refused.returncode != 0 and said in refused.stdout + refused.stderr, asset
    assert netpolicy.INSTALLER_ASSET.fullmatch(ASSET)  # the address the drafts give is the app's own pattern
    # The old releases repository's, for the bridge release, is signed too: the copies that look there check it.
    installer, answer = _draft(tmp_path, url=OLD_ASSET)
    bridged = _sign(tmp_path, answer, installer, key, public)
    assert bridged.returncode == 0, bridged.stdout + bridged.stderr
    for made in (tmp_path / "out").iterdir():
        made.unlink()
    assert f"-gt {selfupdate.MAX_INSTALLER_BYTES})" in (TOOLS / "release-signing.ps1").read_text(encoding="utf-8")
    assert list((tmp_path / "out").iterdir()) == []


# Whether a draft shows the installer and its signature as installed copies will read them, as release.ps1 asks before
# it publishes.
CHECK = r"""
param($Tools, $Release, $Installer, $Signature, $Trusted)
$ErrorActionPreference = 'Stop'
. (Join-Path $Tools 'release-signing.ps1')
Write-Output "$(Test-SignedRelease ([IO.File]::ReadAllText($Release)) $Installer $Signature $Trusted)"
"""


@windows_powershell
def test_a_draft_is_published_only_as_installed_copies_will_read_it(tmp_path):
    key = tmp_path / "keys" / "release.pem"
    public = _make_key(key)
    installer, answer = _draft(tmp_path)
    signed = _sign(tmp_path, answer, installer, key, public)
    assert signed.returncode == 0, signed.stdout + signed.stderr
    signature = Path(signed.stdout.strip())
    digest = hashlib.sha256(signature.read_bytes()).hexdigest()
    beside = {"name": f"{NAME}.sig", "url": SIGNATURE_ASSET, "size": 64, "digest": f"sha256:{digest}"}
    script = tmp_path / "check.ps1"
    script.write_text(CHECK, encoding="utf-8")

    def shown(**changes) -> str:
        draft = json.loads(answer.read_text(encoding="utf-8"))
        draft["assets"].append({**beside, **changes})
        path = tmp_path / "shown.json"
        path.write_text(json.dumps(draft), encoding="utf-8")
        checked = _powershell(
            "-File", str(script), "-Tools", str(TOOLS), "-Release", str(path), "-Installer", str(installer),
            "-Signature", str(signature), "-Trusted", public,
        )
        assert checked.returncode == 0, checked.stdout + checked.stderr
        return checked.stdout.strip()

    assert shown() == "True"
    assert shown(digest=f"sha256:{digest.upper()}") == "False"  # the app would read it; the script holds to GitHub's text
    assert shown(digest=f"SHA256:{digest}") == "False"  # the prefix is compared as it is written too
    assert shown(url="https://api.github.com/repos/someone/else/releases/assets/124") == "False"
    assert shown(name="Other.exe.sig") == "False"
    assert shown(size=65) == "False"


@windows_powershell
def test_the_key_is_made_once_and_never_inside_the_repository(tmp_path):
    inside = ROOT / "release-key-made-by-a-test.pem"
    try:
        refused = _powershell("-File", str(TOOLS / "make-release-key.ps1"), "-Path", str(inside))
        assert refused.returncode != 0 and not inside.exists()
        assert "inside this repository" in refused.stdout
    finally:
        inside.unlink(missing_ok=True)  # a refusal that broke never leaves a private key in the repository
    key = tmp_path / "release.pem"
    _make_key(key)
    before = key.read_bytes()
    again = _powershell("-File", str(TOOLS / "make-release-key.ps1"), "-Path", str(key))
    assert again.returncode != 0 and "already" in again.stdout
    assert key.read_bytes() == before  # a key is never replaced: installed copies trust the one they have


PARSE = r"""
$failed = 0
foreach ($path in $args) {
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors)
    foreach ($found in $errors) { Write-Output "$path $($found.Extent.StartLineNumber): $($found.Message)"; $failed = 1 }
}
exit $failed
"""


@windows_powershell
def test_the_release_scripts_parse(tmp_path):
    # release.ps1 publishes to every installed copy and never runs in a test: PowerShell's own parser reads each script.
    scripts = [*([RELEASE] if RELEASE.exists() else []), ROOT / "build-installer.ps1", *sorted(TOOLS.glob("*.ps1"))]
    check = tmp_path / "parse.ps1"
    check.write_text(PARSE, encoding="utf-8")
    parsed = _powershell("-File", str(check), *map(str, scripts))
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr


def _code(path: Path) -> str:
    """A PowerShell script without its comments: what it runs."""
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#"))


def _in_order(text: str, *steps: str) -> list[int]:
    """Where each step first appears in the script, which must be in the order given."""
    places = []
    for step in steps:
        assert step in text, step
        places.append(text.index(step, places[-1] if places else 0))
    return places


def test_a_release_is_published_only_once_its_signature_checks():
    signing = _code(TOOLS / "release-signing.ps1")
    # The installer goes up as a draft, which installed copies do not see, is signed as GitHub shows it, and is published
    # only once GitHub shows the signature beside it.
    _in_order(
        signing, "function Publish-SignedDraft", "Read-Draft", "New-ReleaseSignatureFile", "gh release upload",
        "Test-SignedRelease", "gh release edit",
    )
    [create] = re.findall(r"^\s*gh release create .*$", signing, flags=re.MULTILINE)
    assert " --draft" in create
    [publish] = re.findall(r"^\s*gh release edit .*$", signing, flags=re.MULTILINE)
    assert "--draft=false" in publish
    assert "GUILDBOARD_RELEASE_KEY" in signing and "Read-ReleaseKey" in signing


@private_build
def test_release_prepares_a_version_and_leaves_the_build_and_the_publication_to_the_public_way():
    # Releases are built by GitHub from a tag of the code's repository and signed by tools/sign-release.ps1 (the player's
    # choice, 2026-11-08): an installer built here and put in that public repository could not be tied to its commit.
    script = _code(RELEASE)
    for publishing in ("New-DraftRelease", "Publish-SignedDraft", "build-installer.ps1", "GUILDBOARD_RELEASE_KEY"):
        assert publishing not in script, publishing
    # It runs neither gh nor a tag nor a push, which it only names in the steps it says at its end.
    run = [line for line in script.splitlines() if not line.lstrip().startswith(("Write-Output", "Stop-With", '"', "'"))]
    assert not [line for line in run if re.search(r"(^|[\s;{(&])(gh|git tag|git push)\s", line)], run
    # Its top says which script is for what, and its end says the steps that follow.
    top = RELEASE.read_text(encoding="utf-8").split("param(")[0]
    assert "release.ps1 (this one)" in top and "sign-release.ps1" in top and "release.yml" in top
    _in_order(script, "git commit", "public-tree.ps1", "git tag -a v$Version", "release.yml", "tools\\sign-release.ps1 -Tag v$Version")


@private_build
def test_release_refuses_a_dirty_tree_or_a_version_without_notes_and_runs_the_tests_before_it_commits():
    # L32: what is released is what is committed and tested. The tree is checked before anything is changed; the
    # version must have its notes in changes.json, which the release takes its notes from; the lock file takes the new
    # version; the Python and the interface's tests run before the version is committed, each step stopping when it fails.
    script = _code(RELEASE)
    # The untracked files count whatever git's configuration says, and a tag of this version on another commit stops
    # the run before anything is changed.
    _in_order(
        script, "git status --porcelain --untracked-files=normal", 'Get-TaggedCommit "v$Version"',
        "Stop-With $onAnother", "changes.json", "Set the version", "uv lock", "pytest", "npm ci --prefix web",
        "npm test --prefix web", "git commit",
    )
    # Nothing but the files the script changes itself may differ from the last commit: the version in the Python package's
    # two places and in the shell's package, and the lock files, which hold it too.
    [allowed] = re.findall(r"^\$versionFiles = @\((.*)\)$", script, flags=re.MULTILINE)
    assert sorted(re.findall(r"'([^']+)'", allowed)) == [
        "pyproject.toml", "shell/package-lock.json", "shell/package.json", "src/guildboard/__init__.py", "uv.lock",
    ]
    # The shell's version is set with the others, so that a later release does not ship a shell that says 2.0.0.
    assert "shell\\package.json" in script and "shell\\package-lock.json" in script
    # The tests run as steps that stop the release, in the build's own environment, never in the .venv a running Guild
    # Board may use.
    for step in ("pytest", "npm test --prefix web"):
        [line] = [line for line in script.splitlines() if step in line]
        assert line.startswith("Step '"), line
    assert "$env:UV_PROJECT_ENVIRONMENT = '.venv-package'" in script.split("pytest")[0]
    # The build, on GitHub, installs exactly the locked packages.
    build = _code(ROOT / "build-installer.ps1")
    assert "uv sync --locked --no-dev --group package" in build
    assert all("--locked" in line for line in build.splitlines() if re.search(r"\buv (?:sync|run)\b", line))


TRUSTED = r"""
param($Tools)
. (Join-Path $Tools 'release-signing.ps1')
foreach ($key in $args) { Write-Output "$(Test-TrustedKey $key)" }
"""


@windows_powershell
def test_an_installer_is_built_only_with_a_release_key_pinned(tmp_path):
    # A copy built while no key is pinned could never update itself. build-installer.ps1, the way to give Guild Board to
    # someone else, stops before it builds anything, unless asked on purpose for an installer that never updates.
    build = _code(ROOT / "build-installer.ps1")
    assert build.lstrip().startswith("param([switch]$WithoutReleaseKey)")
    _in_order(build, "Test-TrustedKey (Get-TrustedKey", "if (-not $WithoutReleaseKey)", "exit 1", "Step '")
    # The warning is said again where the player looks last, after the line that says the installer is ready.
    _in_order(build, "will never update itself", "The installer is ready", "if ($keyWarning) { Write-Output $keyWarning }")
    script = tmp_path / "trusted.ps1"
    script.write_text(TRUSTED, encoding="utf-8")
    point = "04" + "ab" * 64
    asked = _powershell("-File", str(script), "-Tools", str(TOOLS), point, point.upper(), "04" + "ab" * 10, "none")
    assert asked.returncode == 0, asked.stdout + asked.stderr
    assert asked.stdout.split() == ["True", "False", "False", "False"]  # only a point, as the app takes it


@windows_powershell
def test_the_release_script_reads_the_key_the_app_pins(tmp_path):
    script = tmp_path / "trusted.ps1"
    script.write_text(
        "param($Tools, $Source)\n. (Join-Path $Tools 'release-signing.ps1')\nWrite-Output \"[$(Get-TrustedKey $Source)]\"\n",
        encoding="utf-8",
    )
    read = _powershell("-File", str(script), "-Tools", str(TOOLS), "-Source", str(ROOT / "src/guildboard/selfupdate.py"))
    assert read.returncode == 0, read.stderr
    assert read.stdout.strip() == f"[{selfupdate.RELEASE_KEY}]"
