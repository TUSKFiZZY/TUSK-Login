"""tools/sign-release.ps1, the half of a release that runs on the maintainer's PC after the workflow built the installer:
it publishes nothing unless every check before it passed, and what it publishes is signed as the installed copies check.
It runs here in Windows PowerShell against a stand-in for gh that answers from a file and keeps a log of what it was
asked, so that a refusal is shown by the calls that never came."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from guildboard.selfupdate import Release, newer_release, release_text, signed
from test_release_script import _make_key

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
POWERSHELL = shutil.which("powershell")

windows_powershell = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")

TAG = "v1.0.0"
SHA = "a" * 40
INSTALLER = b"MZ the installer built by GitHub"
CHANGES = {"1.0.0": [{"kind": "new", "title": "Pins", "text": "Ünïcode is fine."}, "A plain line"]}
GITHUB_NAME = "TUSKFiZZY.Setup.1.0.0.exe"  # as GitHub names the uploaded "TUSKFiZZY Setup 1.0.0.exe"

# A stand-in for gh: it answers from the file GH_FAKE names, keeps the release it is asked to make in a folder, and logs
# every call. A call it does not know ends the run with 99.
FAKE_GH = r"""
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
$cfg = [IO.File]::ReadAllText($env:GH_FAKE) | ConvertFrom-Json
$state = $cfg.state
$line = $args -join ' '
$at = [array]::IndexOf($args, '--repo')
$args_repo = if ($at -ge 0) { $args[$at + 1] } else { '' }
Add-Content -LiteralPath (Join-Path $state 'calls.log') -Value $line -Encoding UTF8
$releaseFile = Join-Path $state 'release.json'
function Save($release) {
    [IO.File]::WriteAllText($releaseFile, (ConvertTo-Json -InputObject $release -Depth 6), (New-Object Text.UTF8Encoding $false))
}
function Load { [IO.File]::ReadAllText($releaseFile) | ConvertFrom-Json }
function Asset($path, $id) {
    $repo = $args_repo
    [pscustomobject]@{
        name = ((Split-Path -Leaf $path) -replace ' ', '.')
        url = "https://api.github.com/repos/$repo/releases/assets/$id"
        size = (Get-Item -LiteralPath $path).Length
        digest = 'sha256:' + (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
switch -Wildcard ($line) {
    'api user *' { if ($cfg.user) { $cfg.user; exit 0 } else { exit 1 } }
    'release view *' {
        if (Test-Path -LiteralPath $releaseFile) { if ((Load).draft) { 'true' } else { 'false' }; exit 0 }
        exit 1
    }
    'repo view *' { $cfg.private; exit 0 }
    'run list *' { if ($cfg.run_list_exit) { exit 1 }; $cfg.runs; exit 0 }
    'api repos/*/commits/*' { if ($cfg.sha_exit) { exit 1 }; $cfg.tag_sha; exit 0 }
    'run download *' {
        if ($cfg.download_exit) { exit 1 }
        $dir = $args[[array]::IndexOf($args, '--dir') + 1]
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Copy-Item -Path (Join-Path $cfg.fixture '*') -Destination $dir
        exit 0
    }
    'attestation verify *' { exit [int]$cfg.attest_exit }
    'api -H * repos/*/contents/*' { if ($cfg.changes_exit) { exit 1 }; $cfg.changes; exit 0 }
    'release create *' {
        if ($cfg.create_exit) { exit 1 }
        Copy-Item -LiteralPath $args[[array]::IndexOf($args, '--notes-file') + 1] -Destination (Join-Path $state 'notes.md')
        Save ([pscustomobject]@{ id = 7; tag_name = $args[2]; draft = $true; prerelease = $false; body = ''; assets = @(Asset $args[3] 1) })
        "https://github.com/$args_repo/releases/tag/draft"
        exit 0
    }
    'api repos/*/releases?per_page=30' { ConvertTo-Json -InputObject @(Load) -Depth 6; exit 0 }
    'api repos/*/releases/7' { ConvertTo-Json -InputObject (Load) -Depth 6; exit 0 }
    'release upload *' {
        if ($cfg.upload_exit) { exit 1 }
        Copy-Item -LiteralPath $args[3] -Destination (Join-Path $state 'signature.sig')
        $release = Load
        $release.assets = @($release.assets) + (Asset $args[3] 2)
        Save $release
        exit 0
    }
    'release edit *' {
        if ($cfg.edit_exit) { exit 1 }
        $release = Load
        $release.draft = $false
        Save $release
        exit 0
    }
    default { Write-Output "unexpected gh call: $line"; exit 99 }
}
"""


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    folder = tmp_path_factory.mktemp("keys")
    key, other = folder / "release.pem", folder / "other.pem"
    return key, _make_key(key), other, _make_key(other)


class World:
    """A checkout holding the two scripts and the key the app pins, a stand-in for gh, and the artifact the build made."""

    def __init__(self, tmp_path: Path, keys):
        self.tmp = tmp_path
        self.key, self.public, self.other, self.other_public = keys
        self.root = tmp_path / "checkout"
        (self.root / "tools").mkdir(parents=True)
        (self.root / "src" / "guildboard").mkdir(parents=True)
        for name in ("sign-release.ps1", "release-signing.ps1"):
            shutil.copy(TOOLS / name, self.root / "tools" / name)
        (self.root / "src" / "guildboard" / "selfupdate.py").write_text(f'RELEASE_KEY = "{self.public}"\n', encoding="utf-8")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "gh.ps1").write_text(FAKE_GH, encoding="utf-8")
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.artifact = tmp_path / "artifact"
        self.artifact.mkdir()
        self.installer = INSTALLER
        self.temp = tmp_path / "temp"
        self.temp.mkdir()
        self.config = {
            "state": str(self.state), "fixture": str(self.artifact), "user": "TUSKFiZZY", "private": "true",
            "runs": json.dumps([{"databaseId": 555, "headSha": SHA, "event": "push"}]), "tag_sha": SHA,
            "attest_exit": 0, "changes": json.dumps(CHANGES),
        }
        self.write_artifact()

    def write_artifact(self, sums: str | None = None, name: str = "TUSKFiZZY Setup 1.0.0.exe") -> None:
        for each in self.artifact.iterdir():
            each.unlink()
        (self.artifact / name).write_bytes(self.installer)
        digest = sums if sums is not None else hashlib.sha256(self.installer).hexdigest()
        (self.artifact / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8")

    def run(self, *arguments: str, key=None, path: str | None = None, **changes) -> subprocess.CompletedProcess:
        (self.state / "calls.log").unlink(missing_ok=True)  # the log is of the last run alone
        config = self.tmp / "fake.json"
        config.write_text(json.dumps({**self.config, **changes}), encoding="utf-8")  # for this run alone
        env = dict(os.environ, GH_FAKE=str(config), TEMP=str(self.temp), TMP=str(self.temp),
                   PATH=path if path is not None else f"{self.bin};{os.environ['PATH']}")
        env.pop("GUILDBOARD_RELEASE_KEY", None)
        chosen = self.key if key is None else key
        if chosen != "none":
            env["GUILDBOARD_RELEASE_KEY"] = str(chosen)
        return subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(self.root / "tools" / "sign-release.ps1"), *arguments],
            capture_output=True, encoding="utf-8", errors="replace", timeout=300, env=env, cwd=str(self.tmp),
        )

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return [line.strip() for line in log.read_text(encoding="utf-8-sig").splitlines()] if log.exists() else []

    def made(self, verb: str) -> list[str]:
        return [call for call in self.calls() if call.startswith(f"release {verb} ")]


@pytest.fixture
def world(tmp_path, keys):
    return World(tmp_path, keys)


def _said(done: subprocess.CompletedProcess) -> str:
    return done.stdout + done.stderr


@windows_powershell
def test_a_good_release_is_signed_as_the_app_checks_and_published_last(world):
    done = world.run("-Tag", TAG, "-NoAttestation")
    assert done.returncode == 0, _said(done) + "\n".join(world.calls())
    calls = world.calls()
    # The order: who, the key, the release, the run, the commit, the download, the notes, the draft, the signature, the
    # draft read back, and only then the publication.
    positions = {
        name: next(i for i, call in enumerate(calls) if call.startswith(name))
        for name in ("api user", "release view", "run list", "api repos/TUSKFiZZY/TUSK-Login/commits", "run download",
                     "api -H", "release create", "release upload", "release edit")
    }
    assert list(positions.values()) == sorted(positions.values())
    assert not any(call.startswith("attestation") for call in calls)  # -NoAttestation, on a private repository
    assert "--draft" in world.made("create")[0] and "--draft=false" in world.made("edit")[0]
    # It is released in the repository that built it, which installed copies look in, on the tag that is there already:
    # GitHub makes no tag of its own on whatever its main holds.
    for verb in ("create", "upload", "edit"):
        assert " --repo TUSKFiZZY/TUSK-Login " in world.made(verb)[0] + " ", verb
    assert world.made("create")[0].endswith(" --verify-tag")
    assert calls[-1] == world.made("edit")[0]  # the publication is the last call...
    assert calls[-2] == "api repos/TUSKFiZZY/TUSK-Login/releases/7"  # ...after the draft was read back with its signature
    assert calls.index(world.made("upload")[0]) < len(calls) - 2
    release = json.loads((world.state / "release.json").read_text(encoding="utf-8"))
    assert release["draft"] is False and [each["name"] for each in release["assets"]] == [GITHUB_NAME, GITHUB_NAME + ".sig"]
    # The release notes come from changes.json at the tag, bullet by bullet.
    notes = (world.state / "notes.md").read_text(encoding="utf-8")
    assert notes == "- [New] Pins: Ünïcode is fine.\n- A plain line"
    # What was published is a release the app offers, under the pinned key, with the signature that was uploaded.
    signature = (world.state / "signature.sig").read_bytes()
    release["body"] = notes
    sha = hashlib.sha256(INSTALLER).hexdigest()
    offered = newer_release(json.dumps(release).encode(), "0.8.0", signature=lambda *asset: signature, key=world.public)
    assert offered == Release("1.0.0", release["assets"][0]["url"], len(INSTALLER), sha,
                              ({"kind": "new", "title": "Pins", "text": "Ünïcode is fine."}, {"kind": None, "title": "A plain line", "text": ""}))
    assert newer_release(json.dumps(release).encode(), "0.8.0", signature=lambda *asset: signature, key=world.other_public) is None
    # The folder the installer was downloaded to is outside the repository and gone.
    assert list(world.temp.glob("tuskfizzy-release-*")) == []


@windows_powershell
def test_the_bridge_releases_the_same_installer_in_the_old_releases_repository_for_the_copies_that_look_there(world):
    # 2.0.0 and earlier look in QL1STY/guild-board-releases: 2.0.1 goes there as well, signed as they check, so that it
    # brings them to the code's repository. The installer is still the one the code's repository built and attested.
    done = world.run("-Tag", TAG, "-Bridge", private="false")
    assert done.returncode == 0, _said(done) + "\n".join(world.calls())
    calls = world.calls()
    [verify] = [call for call in calls if call.startswith("attestation verify ")]
    assert verify.endswith(" --repo TUSKFiZZY/TUSK-Login")
    assert any(call.startswith("run list --repo TUSKFiZZY/TUSK-Login ") for call in calls)
    assert any(call.startswith("release view v1.0.0 --repo QL1STY/guild-board-releases ") for call in calls)
    for verb in ("create", "upload", "edit"):
        assert " --repo QL1STY/guild-board-releases " in world.made(verb)[0] + " ", verb
    assert "--verify-tag" not in world.made("create")[0]  # the old repository holds none of the code's tags
    release = json.loads((world.state / "release.json").read_text(encoding="utf-8"))
    assert release["draft"] is False
    assert all(each["url"].startswith("https://api.github.com/repos/QL1STY/guild-board-releases/") for each in release["assets"])
    # The signature is the one those copies check: the text names the tag and the installer, not the repository.
    installer = release["assets"][0]
    text = release_text(TAG, installer["name"], installer["size"], installer["digest"].removeprefix("sha256:"))
    assert signed(text, (world.state / "signature.sig").read_bytes(), world.public)
    assert "QL1STY/guild-board-releases" in _said(done)


@windows_powershell
def test_with_attestation_the_installer_is_verified_against_the_repository_before_anything_is_made(world):
    done = world.run("-Tag", TAG, "-Repo", "TUSKFiZZY/TUSK-Login", private="false")
    assert done.returncode == 0, _said(done) + "\n".join(world.calls())
    calls = world.calls()
    [verify] = [call for call in calls if call.startswith("attestation verify ")]
    assert verify.endswith(" --repo TUSKFiZZY/TUSK-Login") and "TUSKFiZZY Setup 1.0.0.exe" in verify
    assert calls.index(verify) < calls.index(world.made("create")[0])
    assert not any(call.startswith("repo view") for call in calls)  # only -NoAttestation asks whether it is private


def _refused(world, said: str, *arguments: str, early: bool = True, **changes) -> None:
    """A run that stops with `said`, and publishes nothing; `early` when it stops before any draft is made."""
    done = world.run(*(arguments or ("-Tag", TAG, "-NoAttestation")), **changes)
    assert done.returncode != 0, _said(done)
    assert said in _said(done), _said(done) + "\n".join(world.calls())
    calls = world.calls()
    assert world.made("edit") == [] and not any(call.startswith("release delete") for call in calls)
    assert "Nothing was published." in _said(done) or "unpublished" in _said(done)
    if early:
        assert world.made("create") == [] and world.made("upload") == []


@windows_powershell
@pytest.mark.parametrize("arguments, said", [
    (("-Tag", "1.0.0"), "a v and three numbers"),
    (("-Tag", "v1.0"), "a v and three numbers"),
    (("-Tag", TAG, "-Repo", "not a repository"), "owner/name"),
])
def test_it_refuses_a_tag_or_repository_that_is_not_written_right(world, arguments, said):
    _refused(world, said, *arguments)
    assert world.calls() == []


@windows_powershell
def test_it_refuses_unless_gh_is_there_and_logged_in_as_the_maintainer(world):
    _refused(world, "logged in as 'someone-else', not TUSKFiZZY", user="someone-else")
    assert world.calls() == ["api user --jq .login"]
    system = Path(os.environ["SystemRoot"]) / "System32"
    _refused(world, "The GitHub CLI, gh, is not installed.", path=f"{system};{system / 'WindowsPowerShell' / 'v1.0'}")


@windows_powershell
def test_it_refuses_without_the_release_key_before_it_asks_github_for_anything_more(world):
    _refused(world, "GUILDBOARD_RELEASE_KEY does not name", key="none")
    _refused(world, "GUILDBOARD_RELEASE_KEY does not name", key=world.tmp / "no" / "such" / "key.pem")
    inside = world.root / "release.pem"
    shutil.copy(world.key, inside)  # a copy of the key inside the repository, where it could be committed
    _refused(world, "inside this folder", key=inside)
    _refused(world, "not the one the app trusts", key=world.other)
    assert world.calls() == ["api user --jq .login"]  # the last run asked who is logged in, and nothing else


@windows_powershell
def test_it_refuses_a_version_that_is_already_released_or_left_as_a_draft(world):
    (world.state / "release.json").write_text(json.dumps({"draft": False, "tag_name": TAG}), encoding="utf-8")
    _refused(world, "already released")
    (world.state / "release.json").write_text(json.dumps({"draft": True, "tag_name": TAG}), encoding="utf-8")
    _refused(world, "A draft of v1.0.0 is left")
    assert not any(call.startswith(("run", "attestation")) for call in world.calls())


@windows_powershell
def test_it_refuses_when_no_run_of_the_workflow_succeeded_for_the_tag(world):
    _refused(world, "No successful run of release.yml was found for v1.0.0", runs="[]")
    _refused(world, "did not list the runs", run_list_exit=1)
    assert not any(call.startswith("run download") for call in world.calls())


@windows_powershell
def test_it_refuses_a_tag_that_was_moved_after_the_build(world):
    _refused(world, "The tag v1.0.0 now names commit bbbbbbb, but the run built aaaaaaa", tag_sha="b" * 40)
    _refused(world, "did not say which commit", sha_exit=1)
    assert not any(call.startswith("run download") for call in world.calls())


@windows_powershell
def test_it_refuses_an_installer_it_could_not_download_or_that_is_not_the_one_built(world):
    _refused(world, "could not be downloaded", download_exit=1)
    # The artifact of another version, or without the SHA-256 file.
    world.write_artifact(name="TUSKFiZZY Setup 0.9.0.exe")
    _refused(world, "does not hold 'TUSKFiZZY Setup 1.0.0.exe'")
    # The installer changed after the build wrote down its SHA-256, or a SHA-256 that is no hash.
    world.write_artifact(sums="0" * 64)
    _refused(world, "was changed after the build")
    world.write_artifact(sums="not a hash")
    _refused(world, "was changed after the build")
    world.write_artifact(sums=hashlib.sha256(INSTALLER).hexdigest().upper())  # GitHub's checksums are lower-case
    _refused(world, "was changed after the build")
    assert not any(call.startswith("attestation") for call in world.calls())


@windows_powershell
def test_it_refuses_an_installer_whose_attestation_does_not_verify(world):
    _refused(world, "could not verify that TUSKFiZZY/TUSK-Login built this installer", "-Tag", TAG, attest_exit=1, private="false")
    assert any(call.startswith("attestation verify ") for call in world.calls())


@windows_powershell
def test_no_attestation_is_for_a_private_repository_alone(world):
    _refused(world, "-NoAttestation is for a trial on a private repository", "-Tag", TAG, "-NoAttestation", private="false")
    assert not any(call.startswith(("run", "attestation")) for call in world.calls())


@windows_powershell
def test_it_publishes_nothing_when_a_step_of_the_release_fails(world):
    _refused(world, "did not give changes.json", changes_exit=1)
    _refused(world, "The draft could not be made.", early=False, create_exit=1)
    assert world.made("upload") == []
    # The draft is there, and stays a draft with a sentence on how to delete it.
    _refused(world, "stays on GitHub, unpublished", early=False, upload_exit=1)
    assert world.made("edit") == [] and (world.state / "release.json").exists()
    # The key is not the one the app pins: that is found before the draft is made, as the first thing.
    (world.state / "release.json").unlink()
    world.config.update(upload_exit=0)
    (world.root / "src" / "guildboard" / "selfupdate.py").write_text(f'RELEASE_KEY = "{world.other_public}"\n', encoding="utf-8")
    _refused(world, "not the one the app trusts")
    # A draft that GitHub shows with another installer than the one uploaded is not signed.
    (world.root / "src" / "guildboard" / "selfupdate.py").write_text(f'RELEASE_KEY = "{world.public}"\n', encoding="utf-8")
    world.config.update(edit_exit=1)
    done = world.run("-Tag", TAG, "-NoAttestation")
    assert done.returncode != 0 and "could not be published. It is still a draft" in _said(done)


def test_the_script_names_the_steps_in_the_order_it_takes_them():
    text = (TOOLS / "sign-release.ps1").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    steps = ["Assert-GhAccount", "Get-ReleaseKeyFile", "Assert-ReleaseKeyTrusted", "Get-Draft", "gh run list",
             "gh api \"repos/$Repo/commits/$Tag\"", "gh run download", "Get-FileHash", "gh attestation verify",
             "Get-ReleaseNotes", "New-DraftRelease", "Publish-SignedDraft"]
    at = 0
    for step in steps:
        assert step in code[at:], step
        at = code.index(step, at)
    # The script itself never publishes or signs: the shared functions do, and only they.
    assert not re.search(r"gh release (create|upload|edit)", code) and "New-ReleaseSignatureFile" not in code
    # Defaults: the public repository, and the maintainer alone.
    assert "[string]$Repo = 'TUSKFiZZY/TUSK-Login'" in code and "$account = 'TUSKFiZZY'" in code
    # No secret and no token of the releases repository is read from anywhere but gh's own login.
    assert "GH_TOKEN" not in code and "GITHUB_TOKEN" not in code
