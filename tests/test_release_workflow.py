"""The release workflow (.github/workflows/release.yml) builds the installer in the open and holds nothing to publish
with: read as text, as no YAML reader is among the project's packages. A run of it needs GitHub, so the shape of the
file is what is checked here."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _code(text: str | None = None) -> str:
    """The workflow without its comment lines: what it does."""
    return "\n".join(line for line in (text or _text()).splitlines() if not line.lstrip().startswith("#"))


def _jobs() -> dict[str, str]:
    """Each job's lines, by its name: the block under `jobs:` at two spaces' indent."""
    code = _code()
    body = code.split("\njobs:\n", 1)[1]
    jobs: dict[str, list[str]] = {}
    name = None
    for line in body.splitlines():
        found = re.fullmatch(r"  ([a-z][a-z0-9_-]*):\s*", line)
        if found:
            name = found.group(1)
            jobs[name] = []
        elif name:
            jobs[name].append(line)
    return {name: "\n".join(lines) for name, lines in jobs.items()}


def _in_order(text: str, *steps: str) -> None:
    at = 0
    for step in steps:
        assert step in text[at:], step
        at = text.index(step, at) + len(step)


def test_it_runs_for_a_tag_of_a_version_and_by_hand_and_for_nothing_else():
    code = _code()
    on = code.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.search(r"^  push:\n    tags: \['v\*'\]$", on, flags=re.MULTILINE), on
    assert re.search(r"^  workflow_dispatch:$", on, flags=re.MULTILINE), on
    # A pull request or a branch must not run the build, and nothing here is triggered by what a stranger can write.
    assert re.findall(r"^  ([a-z_]+):", on, flags=re.MULTILINE) == ["push", "workflow_dispatch"]
    assert "branches" not in on and "pull_request" not in code


def test_the_workflow_holds_no_secret_and_publishes_nothing():
    text = _text()
    assert "secrets." not in text  # the comments included: no secret, not even the one GitHub makes for a run
    assert "secrets:" not in text and "GH_TOKEN" not in text and "GITHUB_TOKEN" not in text
    code = _code()
    # Nothing in it talks to the releases repository or makes a release; the maintainer's script does, with the key.
    assert not re.search(r"\bgh\s|guild-board-releases|softprops|action-gh-release|release create", code)
    # Nothing keeps the checkout's credentials for the steps after it.
    assert "persist-credentials: false" in code


def test_the_permissions_are_the_least_each_job_needs():
    code = _code()
    top = code.split("\npermissions:\n", 1)[1].split("\njobs:", 1)[0]
    assert [line.strip() for line in top.splitlines() if line.strip()] == ["contents: read"]
    jobs = _jobs()
    assert set(jobs) == {"build", "attest"}
    # The job that installs packages and builds can read the repository and nothing more; the right to attest is held by
    # the job that runs no package's code.
    build_permissions = jobs["build"].split("permissions:\n", 1)[1].split("steps:", 1)[0]
    assert [line.strip() for line in build_permissions.splitlines() if line.strip()] == ["contents: read"]
    assert "write" not in jobs["build"] and "id-token" not in jobs["build"]
    attest_permissions = jobs["attest"].split("permissions:\n", 1)[1].split("steps:", 1)[0]
    assert [line.strip() for line in attest_permissions.splitlines() if line.strip()] == [
        "contents: read", "id-token: write", "attestations: write",
    ]
    assert code.count("id-token: write") == 1 and code.count("attestations: write") == 1
    assert "uses: actions/attest-build-provenance@" in jobs["attest"]
    assert "run:" not in jobs["attest"]  # the job that can attest runs no script of the repository's


def test_every_action_is_pinned_to_a_commit():
    code = _code()
    uses = re.findall(r"^\s*(?:- )?uses: (\S+)(.*)$", code, flags=re.MULTILINE)
    assert len(uses) >= 6, uses
    for action, rest in uses:
        assert not action.startswith(("./", "docker://")), action
        name, _, ref = action.partition("@")
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", name), action
        assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{action} is not pinned to a commit: a tag can be moved"
        assert re.search(r"# v?\d+\.\d+", rest), f"{action}: a pinned commit names its version in a comment"
    assert {action.partition("@")[0] for action, _ in uses} == {
        "actions/checkout", "astral-sh/setup-uv", "actions/setup-node", "actions/upload-artifact",
        "actions/download-artifact", "actions/attest-build-provenance",
    }


def test_the_build_runs_on_windows_and_goes_through_the_steps_in_order():
    build = _jobs()["build"]
    assert "runs-on: windows-latest" in build
    _in_order(
        build,
        "uses: actions/checkout@",
        "ref_type == 'tag'",
        "uses: astral-sh/setup-uv@",
        "uses: actions/setup-node@",
        "Inno Setup",
        "uv sync --frozen",
        "npm ci --prefix web",
        "npm ci --prefix shell",
        "npm test --prefix shell",
        "uv run --frozen python -m pytest -q -p no:cacheprovider",
        "npx vitest run",
        "npm run build --prefix web",
        "npm run e2e --prefix shell",
        "build-installer.ps1",
        "Get-FileHash",
        "uses: actions/upload-artifact@",
    )
    assert "node-version: 22" in build
    # The build-installer script looks for the compiler where it is installed, and the workflow installs it if missing.
    assert "choco install innosetup" in build and "Inno Setup 6\\ISCC.exe" in build
    assert "name: tuskfizzy-installer" in build and "if-no-files-found: error" in build
    # The installer and a text file of its SHA-256 are what the maintainer's script downloads.
    assert "TUSKFiZZY Setup *.exe\n" in build and "TUSKFiZZY Setup *.exe.sha256" in build


def test_a_tag_that_is_not_the_version_fails_with_a_plain_sentence():
    build = _jobs()["build"]
    check = build.split("Check that the tag is the version in pyproject.toml", 1)[1].split("- name:", 1)[0]
    assert "if: github.ref_type == 'tag'" in check
    assert "TAG: ${{ github.ref_name }}" in check  # through the environment, never pasted into the script
    assert "${{" not in check.split("run:", 1)[1]
    assert "pyproject.toml" in check and "__init__.py" in check
    assert re.search(r'\$env:TAG -cne "v\$project"', check)
    assert "The tag is $env:TAG, but the version in pyproject.toml is $project." in check
    assert "exit 1" in check


def test_the_attestation_is_made_for_the_installer_and_skipped_in_a_private_repository():
    attest = _jobs()["attest"]
    assert "if: github.event.repository.private == false" in attest
    assert "needs: build" in attest
    _in_order(attest, "uses: actions/download-artifact@", "name: tuskfizzy-installer", "path: setup",
              "uses: actions/attest-build-provenance@", "subject-path: setup/*.exe")
    # The build is not conditional on it: a private trial still builds.
    assert "if:" not in _jobs()["build"].split("steps:", 1)[0]


def test_the_workflow_matches_the_versions_the_project_asks_for():
    # The Node.js of the build is the one CONTRIBUTING.md asks of a contributor.
    assert "Node.js 22" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "node-version: 22" in _text()
