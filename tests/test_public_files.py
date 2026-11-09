"""The files a visitor to the public repository reads first: they exist, say what they must, and name nothing private."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTES = "super" + "powers"  # the private notes' folder; no public file may name it
FILES = ["README.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "LICENSE", ".github/ISSUE_TEMPLATE/bug.md",
         ".github/ISSUE_TEMPLATE/idea.md"]
FAN_MADE = "Unofficial and fan-made. Not affiliated with or endorsed by HoYoverse or COGNOSPHERE."


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_a_public_file_names_no_private_notes_no_support_link_and_no_address_of_the_developers(name):
    text = _text(name).lower()
    assert NOTES not in text
    for word in ("donat", "sponsor", "ko-fi", "patreon", "paypal", "@gmail", "friend of mine"):
        assert word not in text, word


def test_the_licence_is_mit_for_the_handle():
    text = _text("LICENSE")
    assert text.startswith("MIT License\n\nCopyright (c) 2026 QL1STY\n")
    assert "Permission is hereby granted, free of charge" in text


SHOTS = "https://ql1sty.github.io/guild-board/shots/"


def test_the_readme_says_fan_made_links_the_download_the_site_and_the_demo_and_shows_the_sites_screenshots():
    text = _text("README.md")
    assert text.count(FAN_MADE) == 2  # at the top and at the end
    assert "](https://github.com/TUSKFiZZY/TUSK-Login/releases/latest)" in text
    assert "guild-board-releases" not in text  # 2.0.1 moved the releases into the code's repository
    assert "](https://ql1sty.github.io/guild-board/)" in text and "](https://ql1sty.github.io/guild-board/demo/)" in text
    assert "TODO" not in text
    # The pictures are the site's, taken from the demo's made-up account; none is kept in this repository.
    pictures = re.findall(r'<img src="([^"]+)"', text)
    assert pictures and all(picture.startswith(SHOTS) for picture in pictures), pictures
    assert {"board.png", "pulls.png", "achievements.png", "stats.png", "overlay.png"} <= {p.removeprefix(SHOTS) for p in pictures}
    for name in ("LICENSE", "PRIVACY.md", "THIRD-PARTY-NOTICES.txt", "CONTRIBUTING.md", "docs/ARCHITECTURE.md"):
        assert f"]({name})" in text, name
        assert (ROOT / name).is_file(), name


def test_the_rest_name_the_private_reporting_the_covenant_and_the_hands_off_test():
    assert "private vulnerability reporting" in _text("SECURITY.md")
    assert "src/guildboard/selfupdate.py" in _text("SECURITY.md") and "tests/test_handsoff.py" in _text("SECURITY.md")
    assert "https://www.contributor-covenant.org/version/2/1/code_of_conduct/" in _text("CODE_OF_CONDUCT.md")
    contributing = _text("CONTRIBUTING.md")
    assert "uv run pytest" in contributing and "tests/test_handsoff.py" in contributing and "plain, factual English" in contributing


@pytest.mark.parametrize("name", ["bug", "idea"])
def test_an_issue_template_has_its_header(name):
    text = _text(f".github/ISSUE_TEMPLATE/{name}.md")
    assert text.startswith("---\nname: ") and re.search(r"^about: .+$", text, re.MULTILINE)
