from __future__ import annotations

import ctypes
import os
import re
import sys
from datetime import datetime, timezone

import pytest

from guildboard import netpolicy
from support import SOCKET_HOSTS, FakeClock, beyond_this_pc

DEAD_PROXY = "http://127.0.0.1:9"  # nothing listens on port 9 of this PC, so a request sent there fails at once

_refused: list[str] | None = None  # while a test runs: what it tried to reach beyond this PC


def _refuse_what_leaves_this_pc(event: str, args: tuple) -> None:
    refused = _refused
    if refused is None or event not in SOCKET_HOSTS:
        return
    try:
        host = SOCKET_HOSTS[event](args)
    except Exception:  # an event of another shape names no host
        return
    if beyond_this_pc(host):
        refused.append(f"{event} {host}")
        # Raised in the hook, it stops the lookup or the connection before anything is sent.
        raise PermissionError(f"A test tried to reach {host}. The tests contact nothing beyond 127.0.0.1.")


sys.addaudithook(_refuse_what_leaves_this_pc)  # audit hooks cannot be removed; outside a test this one does nothing

# What marks a file or a folder of the game's: the folder of its log, the data folder of either client with its web
# cache, and the install folder by its name. The Pulls design lets Guild Board read two of the files, on Import (section
# 4); a test reads none of the player's.
GAME_FILES = re.compile(r"mihoyo|genshinimpact_data|yuanshen_data|webcaches|genshin impact game", re.IGNORECASE)
# Opening a file reads it, through Python's open or Windows' own. Listing a folder opens nothing, but it looks into the
# game's folders all the same.
WATCHED_EVENTS = {"open": "open", "_winapi.CreateFile": "open", "os.listdir": "list", "os.scandir": "list"}

_own_folder: str | None = None  # while a test runs: its own folder
_game_files_refused: list[str] | None = None  # while a test runs: what of the game's it tried to open or list elsewhere


def _refuse_the_players_game_files(event: str, args: tuple) -> None:
    refused, own = _game_files_refused, _own_folder
    if refused is None or own is None or event not in WATCHED_EVENTS:
        return
    try:
        path = args[0]
        if not isinstance(path, (str, bytes, os.PathLike)):  # a file number, or the working folder, names no path
            return
        where = os.path.normcase(os.path.abspath(os.fsdecode(path)))
    except Exception:  # an event of another shape names no path
        return
    if GAME_FILES.search(where) and not where.startswith(own + os.sep):
        refused.append(where)
        # Raised in the hook, it stops the opening or the listing before anything is read.
        raise PermissionError(
            f"A test tried to {WATCHED_EVENTS[event]} {where}, of the game's. The tests read none of the player's."
        )


sys.addaudithook(_refuse_the_players_game_files)  # like the one above, this does nothing outside a test


@pytest.fixture(autouse=True)
def no_game_files_of_the_player(tmp_path):
    """No test opens or lists a file or folder of the game's outside its own folder: the made-up log and cache of the
    tests live in it, with USERPROFILE set there. An audit hook refuses it before anything is read, and the test fails
    at its end when it was tried, also when the code under test caught the refusal. Gives what was refused, so that a
    test of this can say it meant it. (Asking a file's size or time raises no event, so it is not watched.)"""
    global _own_folder, _game_files_refused
    refused: list[str] = []
    _own_folder = os.path.normcase(os.path.abspath(tmp_path))
    _game_files_refused = refused
    try:
        yield refused
    finally:
        _own_folder = _game_files_refused = None
    if refused:
        pytest.fail(f"A test tried to reach {', '.join(refused)}, of the game's. The tests read none of the player's.")


@pytest.fixture(autouse=True)
def away_from_the_players_files(tmp_path):
    """Every test runs with APPDATA, LOCALAPPDATA and USERPROFILE in its own folder, so that no test finds the player's
    Guild Board folder or the game's log. A start with no --data-dir, here or in a process the test starts, uses a
    folder of the test's own, whatever TUSKFIZZY_DATA_DIR the player has. A test that needs a real value sets it
    itself.

    All of this is held by a patch of this fixture's own, apart from the test's `monkeypatch`: a test that calls
    undo() undoes what it patched, and never gives the player's folders back. A process the test starts inherits the
    variables, so that a core started there finds none of the player's files either."""
    folders = pytest.MonkeyPatch()
    folders.setenv("APPDATA", str(tmp_path / "Roaming"))
    folders.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    folders.setenv("USERPROFILE", str(tmp_path / "Profile"))
    folders.setenv("TUSKFIZZY_DATA_DIR", str(tmp_path / "default-data"))
    try:
        yield
    finally:
        folders.undo()


@pytest.fixture(autouse=True)
def refused_requests(monkeypatch):
    """Every test runs off the internet. netpolicy._open, which sends Guild Board's own requests, refuses each one and
    fails the test; a test that needs it replaces it itself. In this process, an audit hook refuses every name lookup
    and every connection beyond this PC, whatever makes it (aiohttp for HoYoLAB, a socket of its own). urllib reads
    the proxy when it builds an opener, so an opener built while a test runs in a process the test starts (netpolicy's
    own there) sends to a proxy where nothing listens. Gives what was refused, so that a test can check there was
    nothing; the test fails at its end when there was, also when the code under test caught the refusal."""
    global _refused
    monkeypatch.setenv("HTTPS_PROXY", DEAD_PROXY)
    monkeypatch.setenv("HTTP_PROXY", DEAD_PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    refused: list[str] = []

    def refuse(request, timeout=None):
        url = getattr(request, "full_url", str(request))
        refused.append(url)
        pytest.fail(f"A test tried to contact {url}. The tests contact nothing beyond 127.0.0.1.")

    monkeypatch.setattr(netpolicy, "_open", refuse)
    _refused = refused
    try:
        yield refused
    finally:
        _refused = None
    if refused:
        pytest.fail(f"A test tried to contact {', '.join(refused)}. The tests contact nothing beyond 127.0.0.1.")


@pytest.fixture(autouse=True)
def no_global_hotkey(monkeypatch):
    """No test registers a global hotkey with Windows: it would take the player's Ctrl+Alt+G, or another key, from
    whatever they are doing while the tests run. A test that needs one gives the code a stand-in for user32."""

    def refuse(*args: object) -> bool:
        pytest.fail("A test tried to register a global hotkey with Windows. The tests take no key from the player.")

    monkeypatch.setattr(ctypes.windll.user32, "RegisterHotKey", refuse)


@pytest.fixture(autouse=True)
def week_and_endgame_on(monkeypatch):
    """Your week and Endgame are hidden in a shipped build (`characters.WEEK_AND_ENDGAME` is False), and their tests run
    with the switch on, as they will when it is turned back on. A test of the hidden state sets it False itself."""
    from guildboard import characters

    monkeypatch.setattr(characters, "WEEK_AND_ENDGAME", True)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc))


from guildboard.store import Store


@pytest.fixture
def store():
    database = Store.in_memory()
    yield database
    database.close()
