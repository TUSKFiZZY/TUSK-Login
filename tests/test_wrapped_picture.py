"""The Wrapped poster saved as a picture (the Stats design, 6.4): the page makes the PNG and sends it to the core, which
holds it in memory alone until the window's Save dialog has a place for it, and writes only the file the player chose,
never in the game's folders. Without a window the page downloads the picture itself."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guildboard.api.app import create_app
from guildboard.backups import GAME_FOLDER, BackupRefused
from guildboard.config import AppPaths, Settings, save_settings
from guildboard.core import NO_PICTURE, PICTURE_GAME_FOLDER, PICTURE_REPLACED, Core
from support import AccountSource, FakeSleep

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
NAME = "TUSKFiZZY Wrapped 2026 Story.png"
PICTURE = "/api/stats/year/picture"


def _core(tmp_path: Path, clock, demo: bool = False) -> Core:
    return Core(AppPaths(tmp_path / "data"), demo=demo, clock=clock, source=None if demo else AccountSource(),
                sleep=FakeSleep(clock), process_lister=lambda: set())


@pytest.fixture
def core(tmp_path, clock):
    core = _core(tmp_path, clock)
    yield core
    core.store.close()


@pytest.fixture
def client(core):
    with TestClient(create_app(core, 4321), base_url="http://127.0.0.1:4321") as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        yield client


def _send(client: TestClient, body: bytes, name: str = NAME):
    return client.post(PICTURE, params={"name": name}, content=body, headers={"Content-Type": "image/png"})


def test_a_picture_in_the_game_folder_is_refused(tmp_path, clock):
    # An install folder (it holds the game's program, whatever it is called), a folder inside it, and the ScreenShot
    # folder the gallery has: the backup export's own check, in words of the picture's own (a backup's would say
    # "A backup"). Nothing is written there, not even for a moment, and the refused picture is let go: nothing could use
    # it again.
    install = tmp_path / "Games" / "Genshin Impact game"
    (install / "GenshinImpact_Data").mkdir(parents=True)
    (install / "GenshinImpact.exe").write_bytes(b"made up")
    screenshots = tmp_path / "Games" / "ScreenShot"
    screenshots.mkdir()
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    save_settings(paths.settings_path, Settings(gallery_folder=str(screenshots)))
    core = _core(tmp_path, clock)
    try:
        for target in (install / NAME, install / "GenshinImpact_Data" / NAME, screenshots / NAME):
            core.hold_picture(NAME, PNG)
            with pytest.raises(BackupRefused) as refused:
                core.save_picture(target)
            assert str(refused.value) == PICTURE_GAME_FOLDER
            assert PICTURE_GAME_FOLDER != GAME_FOLDER
            assert core.picture_name() is None
        assert not list(tmp_path.rglob("*.png*"))
        # Beside the install folder is not in it.
        core.hold_picture(NAME, PNG)
        assert core.save_picture(tmp_path / "Games" / NAME) == NAME
        assert (tmp_path / "Games" / NAME).read_bytes() == PNG
    finally:
        core.store.close()


def test_a_picture_that_could_not_be_written_is_let_go(core, tmp_path):
    # A place that is not there: no folder is made for it, nothing is left, and the picture is not kept ten minutes more.
    core.hold_picture(NAME, PNG)
    with pytest.raises(OSError):
        core.save_picture(tmp_path / "Nowhere" / NAME)
    assert not (tmp_path / "Nowhere").exists()
    assert core.picture_name() is None


def test_a_picture_replaced_while_its_dialog_was_open_is_not_saved_under_its_name(core, tmp_path):
    # A second press while the first picture's dialog is open: the newer picture takes its place and gets a dialog of its
    # own. The first dialog's save is refused in plain words, its cancel lets nothing go, and the newer one is saved by
    # its own dialog.
    core.hold_picture(NAME, PNG)
    first, offered = core.picture_offer()
    assert offered == NAME
    wide = "TUSKFiZZY Wrapped 2026 Wide.png"
    core.hold_picture(wide, PNG + b"wide")
    second, offered = core.picture_offer()
    assert offered == wide and second != first
    core.drop_picture(first)  # the first dialog cancelled
    assert core.picture_name() == wide
    with pytest.raises(BackupRefused) as refused:
        core.save_picture(tmp_path / NAME, first)
    assert str(refused.value) == PICTURE_REPLACED
    assert not list(tmp_path.glob("*.png*"))
    assert core.picture_name() == wide  # still waiting for its own dialog
    assert core.save_picture(tmp_path / wide, second) == wide
    assert (tmp_path / wide).read_bytes() == PNG + b"wide"
    assert core.picture_name() is None


def test_a_picture_with_no_window_answers_409(client, core):
    answer = _send(client, PNG)
    assert answer.status_code == 409
    assert answer.json()["error"] == "Open TUSKFiZZY's window to save the picture."
    assert core.picture_name() is None  # nothing is kept that no dialog will ask for


def test_a_picture_that_is_not_a_png_is_refused(client, core):
    asked: list[str] = []
    core.window_requests = lambda kind: asked.append(kind) or True
    for body in (b"GIF89a" + PNG, b"", PNG[:7], b"<svg/>"):
        answer = _send(client, body)
        assert answer.status_code == 422, body[:8]
        assert answer.json()["error"] == "This is not a picture of TUSKFiZZY's Wrapped."
    # And a name that is not the poster's: a place, another kind of file, another name.
    for name in (
        "..\\..\\x.png", "C:/Windows/x.png", "TUSKFiZZY Wrapped 2026 Story.exe", "notes.png", "", NAME + " ",
        "TUSKFiZZY Wrapped \u0662\u0660\u0662\u0666 Story.png",  # digits, but not 0 to 9
    ):
        answer = _send(client, PNG, name)
        assert answer.status_code == 422, name
    assert asked == [] and core.picture_name() is None


def test_a_picture_is_offered_to_the_window_and_written_where_the_player_chose(client, core, tmp_path):
    asked: list[str] = []
    core.window_requests = lambda kind: asked.append(kind) or True
    answer = _send(client, PNG, "TUSKFiZZY Wrapped 2026 Wide.png")
    assert answer.status_code == 200 and answer.json() == {"opened": True}
    assert asked == ["save_picture"]
    assert core.picture_name() == "TUSKFiZZY Wrapped 2026 Wide.png"
    chosen = tmp_path / "Pictures" / "My Wrapped.png"
    chosen.parent.mkdir()
    assert core.save_picture(chosen) == "My Wrapped.png"
    assert chosen.read_bytes() == PNG
    assert not chosen.with_name("My Wrapped.png.tmp").exists()
    # Dropped after a save: a second save has nothing to write.
    assert core.picture_name() is None
    with pytest.raises(BackupRefused) as refused:
        core.save_picture(tmp_path / "Pictures" / "again.png")
    assert str(refused.value) == NO_PICTURE
    assert not (tmp_path / "Pictures" / "again.png").exists()


def test_one_picture_is_held_at_most_and_dropped_after_ten_minutes(core, clock, tmp_path):
    core.hold_picture(NAME, PNG)
    core.hold_picture("TUSKFiZZY Wrapped 2025 Wide.png", PNG + b"x")
    assert core.picture_name() == "TUSKFiZZY Wrapped 2025 Wide.png"
    clock.advance(minutes=9, seconds=59)
    assert core.picture_name() == "TUSKFiZZY Wrapped 2025 Wide.png"
    clock.advance(seconds=2)
    assert core.picture_name() is None
    with pytest.raises(BackupRefused):
        core.save_picture(tmp_path / "late.png")
    assert not (tmp_path / "late.png").exists()
    core.hold_picture(NAME, PNG)
    core.drop_picture()  # the dialog was cancelled
    assert core.picture_name() is None


def test_a_picture_may_be_up_to_16_mb(client, core):
    # Larger than any other request the page sends, the backup's restore aside; one byte over is refused before it is read.
    asked: list[str] = []
    core.window_requests = lambda kind: asked.append(kind) or True
    large = PNG + bytes(16 * 1024 * 1024 - len(PNG))
    assert _send(client, large).status_code == 200
    assert core.picture_name() == NAME
    core.drop_picture()
    refused = _send(client, large + b"x")
    assert refused.status_code == 413 and refused.json()["error"] == "This picture is too large to save."
    assert core.picture_name() is None


def test_the_demo_saves_its_picture_too(tmp_path, clock):
    # The demo's poster is of a made-up player; saving it writes only the chosen file, as for a real one.
    core = _core(tmp_path, clock, demo=True)
    try:
        core.hold_picture(NAME, PNG)
        assert core.save_picture(tmp_path / NAME) == NAME
    finally:
        core.store.close()
    assert (tmp_path / NAME).read_bytes() == PNG


def test_the_held_picture_is_dropped_by_the_loop_after_ten_minutes(core):
    # In memory for ten minutes at most, even when nothing asks for it again.
    import asyncio

    async def hold_and_wait() -> None:
        loop = asyncio.get_running_loop()
        scheduled: list[float] = []
        original = loop.call_later

        def noted(delay, callback, *args):
            scheduled.append(delay)
            return original(0, callback, *args)

        loop.call_later = noted
        try:
            core.hold_picture(NAME, PNG)
        finally:
            loop.call_later = original
        assert scheduled == [timedelta(minutes=10).total_seconds()]
        await asyncio.sleep(0.01)  # the timer, brought forward, runs

    asyncio.run(hold_and_wait())
    assert core.picture_name() is None  # the clock has not moved: the loop dropped it
