import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from achievement_samples import paimon_export
from guildboard import __version__, netpolicy
from guildboard.achievements.importers import ImportChanged, ImportFileError
from guildboard.config import AppPaths, load_settings
from guildboard.core import PRIMOGEM_ICON, STATIC_ICONS, Core, resin_now
from guildboard.pulls.banners import Banners
from guildboard.pulls.importer import WishImporter
from guildboard.pulls.planner import Planner
from guildboard.pulls.wiki import WikiIcons
from guildboard.records import GameAccount, Offline
from guildboard.store import StoreError, iso
from guildboard.tokenstore import StoredToken, TokenStore
from guildboard.views.achievements import Query
from support import AccountSource, FakeSleep, ok_result

ACCOUNT = GameAccount(700000001, "Traveler", "os_euro", 60)  # the account AccountSource lists
COOKIES = {"ltoken_v2": "v2_SECRET-VALUE-0123456789", "ltuid_v2": "123456"}


def _core(tmp_path, clock, *, demo: bool, source=None) -> Core:
    return Core(
        AppPaths(tmp_path / "data"), demo=demo, clock=clock, source=source,
        sleep=FakeSleep(clock), process_lister=lambda: set(),
    )


def test_demo_core_seeds_history_and_reports_status(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    status = core.status()
    assert (status["version"], status["mode"], status["has_shell"]) == (__version__, "demo", False)
    assert status["tracking_since"] == "2026-03-02"
    assert status["sync"]["state"] == "ok"
    assert status["sync"]["last_ok_at"] is not None
    assert status["sync"]["next_at"] is None
    assert status["sync"]["manual_wait_seconds"] == 0
    assert status["game"]["running"] is False
    assert status["game"]["session_started_at"] is None
    assert status["game"]["last_session_minutes"] > 0
    account = status["account"]
    assert (account["nickname"], account["server"], account["uid"]) == ("Traveler", "os_euro", 700000001)
    assert (account["adventure_rank"], account["resin_max"]) == (60, 200)
    assert account["days_active"] > 1700
    assert core.backlog()["regions"]
    assert core.stats("7")["period"]["days"] == 7
    assert core.auth is None
    core.store.close()


def test_demo_data_is_not_reseeded(tmp_path, clock):
    first = _core(tmp_path, clock, demo=True)
    runs = len(first.store.recent_runs(limit=500))
    seeded = (first.store.timers(), first.store.timer_events(), first.store.appearances())
    first.store.close()
    second = _core(tmp_path, clock, demo=True)
    assert len(second.store.recent_runs(limit=500)) == runs == 211
    assert (second.store.timers(), second.store.timer_events(), second.store.appearances()) == seeded
    second.store.close()


def test_live_core_without_a_token_needs_login(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert core.has_token() is False
    assert core.server() == "os_euro"
    status = core.status()
    assert status["mode"] == "live"
    assert status["sync"]["state"] == "login_needed"
    assert status["account"] is None
    assert status["tracking_since"] is None
    core.store.close()


def test_live_core_uses_the_stored_account(tmp_path, clock):
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    TokenStore(paths.token_path).save(StoredToken(dict(COOKIES), 800000002, "os_asia", "Lumine"))
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert core.has_token() is True
    assert core.server() == "os_asia"
    account = core.status()["account"]
    assert (account["nickname"], account["server"], account["uid"]) == ("Lumine", "os_asia", 800000002)
    assert (account["adventure_rank"], account["resin"], account["resin_read_at"]) == (None, None, None)
    core.store.close()


def test_settings_changes_are_validated_and_saved(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    saved = core.update_settings(
        {"in_session_interval_min": 30, "risk_acknowledged": True, "sync_paused": True, "made_up": 1}
    )
    assert (saved.in_session_interval_min, saved.risk_acknowledged) == (30, True)
    assert saved.sync_paused is False  # pausing has its own switch
    assert core.update_settings({"in_session_interval_min": 45}).in_session_interval_min == 30
    assert load_settings(core.paths.settings_path) == core.settings
    core.set_paused(True)
    assert load_settings(core.paths.settings_path).sync_paused is True
    assert core.status()["sync"]["state"] == "paused"
    core.set_paused(False)
    assert core.status()["sync"]["state"] == "ok"
    core.store.close()


async def test_login_starts_a_sync_and_logout_removes_the_token(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    outcome = await core.auth.login(COOKIES)
    assert outcome.status == "ok"
    await core.scheduler.idle()
    row = core.store.recent_runs()[0]
    assert (row.trigger, row.outcome) == ("login", "ok")
    assert core.status()["sync"]["state"] == "ok"
    core.logout()
    assert core.has_token() is False
    assert core.status()["sync"]["state"] == "login_needed"
    core.store.close()


def test_window_requests_need_a_shell(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert core.request_window("login") is False
    core.window_requests = lambda kind: kind == "login"
    assert core.status()["has_shell"] is True
    assert core.request_window("login") is True
    assert core.request_window("verify") is False
    core.store.close()


async def _as_the_shell_calls(answer):
    """What a call into the core answers, awaited when it is to be, as the shell's own calls are (corethread.call)."""
    return await answer if inspect.isawaitable(answer) else answer


async def test_backup_now(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    name = await _as_the_shell_calls(core.backup_now())
    assert name == "guildboard-manual-20261013-080000.db"
    assert (core.paths.backups_dir / name).is_file()
    core.store.close()


async def test_a_manual_backup_is_copied_off_the_event_loop(tmp_path, clock, monkeypatch):
    # The copy of a large database takes a while, and every request, the event stream and the core's loops would wait
    # for it. It is made in a worker thread, with a connection of its own: the store's one connection is used on the
    # event loop's thread alone, which keeps its transactions whole.
    import sqlite3
    import threading

    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    copy_made_in: list[int] = []  # the threads that connected to the copy, and to the live database it is read from
    live_read_in: list[int] = []
    real = sqlite3.connect

    def connect(database, *args, **kwargs):
        if Path(database).parent == core.paths.backups_dir:
            copy_made_in.append(threading.get_ident())
        elif Path(database) == core.paths.db_path:
            live_read_in.append(threading.get_ident())
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        name = await _as_the_shell_calls(core.backup_now())
        assert name == "guildboard-manual-20261013-080000.db" and (core.paths.backups_dir / name).is_file()
        assert copy_made_in and live_read_in and threading.get_ident() not in copy_made_in + live_read_in
    finally:
        core.store.close()


async def test_an_export_is_written_off_the_event_loop(tmp_path, clock, monkeypatch):
    # The export copies the database and deflates it into the file: 228 ms for an 18 MB database, seconds for a large one.
    import sqlite3
    import threading
    import zipfile

    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    written: list[int] = []
    live_read_in: list[int] = []  # the threads that connected to the live database, which the copy is read from
    real, real_connect = zipfile.ZipFile.write, sqlite3.connect
    monkeypatch.setattr(zipfile.ZipFile, "write", lambda self, *a, **k: written.append(threading.get_ident()) or real(
        self, *a, **k))

    def connect(database, *args, **kwargs):
        if Path(database) == core.paths.db_path:
            live_read_in.append(threading.get_ident())
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    (tmp_path / "out").mkdir()
    try:
        assert await _as_the_shell_calls(core.export_backup(tmp_path / "out" / "x.gbbackup")) == "x.gbbackup"
        assert written and live_read_in and threading.get_ident() not in written + live_read_in
        assert zipfile.ZipFile(tmp_path / "out" / "x.gbbackup").namelist() == ["guildboard.db", "settings.json", "manifest.json"]
    finally:
        core.store.close()


async def test_start_runs_the_first_sync_and_stop_shuts_down(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    core.start()
    await core.scheduler.idle()
    row = core.store.recent_runs()[0]
    assert (row.trigger, row.outcome) == ("startup", "ok")
    await core.stop()


def test_demo_core_can_start_at_a_chosen_instant_and_move_on(tmp_path):
    start = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    core = Core(AppPaths(tmp_path / "data"), demo=True, demo_now=start, process_lister=lambda: set())
    status = core.status()
    assert status["now"].startswith("2026-10-13T08:00")
    assert status["tracking_since"] == "2026-03-02"
    moved = core.advance_demo_clock(6 * 60)
    assert 355 < (moved - start).total_seconds() < 370
    assert core.status()["now"].startswith("2026-10-13T08:06")
    core.store.close()


def test_only_the_demo_clock_can_be_moved(tmp_path, clock):
    live = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert live.status()["now"] == "2026-10-13T08:00:00+00:00"
    with pytest.raises(RuntimeError):
        live.advance_demo_clock(60)
    live.store.close()


async def test_a_demo_fault_does_not_wait_between_attempts(tmp_path, clock):
    sleep = FakeSleep(clock)
    core = Core(AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=sleep, process_lister=lambda: set())
    core.source.fault = Offline("HoYoLAB could not be reached.")
    await core.scheduler.trigger("manual")
    assert core.status()["sync"]["state"] == "offline"
    assert sleep.calls == []  # a demo fault shows a state; it does not make anyone wait two minutes
    core.store.close()


async def test_a_real_connection_problem_still_waits_between_attempts(tmp_path, clock):
    sleep = FakeSleep(clock)
    source = AccountSource()
    source.will("overview", Offline("No connection."), Offline("No connection."), ok_result("overview", clock.now()))
    core = Core(AppPaths(tmp_path / "data"), demo=False, clock=clock, source=source, sleep=sleep, process_lister=lambda: set())
    core.tokens.save(StoredToken(dict(COOKIES), 700000001, "os_euro", "Traveler"))
    await core.scheduler.trigger("manual")
    assert sleep.calls == [10.0, 30.0]
    assert core.status()["sync"]["state"] == "ok"
    core.store.close()


def test_status_names_the_data_folder(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    assert core.status()["data_dir"] == str(tmp_path / "data")
    core.store.close()


def test_the_status_shows_resin_now_not_at_the_last_sync(tmp_path):
    # Spec 10.1: the top bar shows resin now. Syncs are hours apart; resin grows by one every 8 minutes.
    start = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    core = Core(AppPaths(tmp_path / "data"), demo=True, demo_now=start, process_lister=lambda: set())
    before = core.status()["account"]["resin"]
    core.advance_demo_clock(3 * 3600)
    assert core.status()["account"]["resin"] == min(200, before + 22)
    core.store.close()


async def test_the_status_names_the_read_that_resin_counts_on_from(tmp_path, clock):
    # The Board's Resin glints for a new read, never for the clock alone (look pass spec 14.2): the status says which
    # read its count starts from.
    core = Core(AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set())
    first = core.status()["account"]
    assert first["resin_read_at"] == iso(core.store.latest_doc("notes")[0])
    clock.advance(minutes=8)  # the count goes on with the clock, from the same read
    later = core.status()["account"]
    assert (later["resin"], later["resin_read_at"]) == (first["resin"] + 1, first["resin_read_at"])
    clock.advance(minutes=6)
    await core.scheduler.trigger("manual")  # a new read
    read = core.status()["account"]["resin_read_at"]
    assert read == iso(core.store.latest_doc("notes")[0]) != first["resin_read_at"]
    core.store.close()


def test_resin_counts_on_from_the_last_read():
    read = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    notes = {"resin": 77, "resin_max": 200, "resin_full_at": (read + timedelta(minutes=123 * 8)).isoformat()}
    assert resin_now(notes, read, read) == 77
    assert resin_now(notes, read, read + timedelta(hours=3)) == 99
    assert resin_now(notes, read, read + timedelta(days=1)) == 200  # it stops at the maximum
    assert resin_now({**notes, "resin_full_at": None}, read, read + timedelta(minutes=17)) == 79
    assert resin_now({"resin": 230, "resin_max": 200, "resin_full_at": None}, read, read + timedelta(hours=5)) == 230
    assert resin_now(notes, read, read - timedelta(hours=1)) == 77  # a clock that went back never lowers it


def test_the_real_account_cannot_use_the_demo_folder(tmp_path):
    Core(AppPaths(tmp_path / "data"), demo=True, process_lister=lambda: set()).store.close()
    with pytest.raises(StoreError) as error:
        Core(AppPaths(tmp_path / "data"), demo=False, process_lister=lambda: set())
    assert "holds the demo's made-up data" in str(error.value)


async def test_the_first_login_marks_the_folder_with_the_account(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert (await core.auth.login(COOKIES)).status == "ok"
    assert core.store.meta("account_uid") == str(ACCOUNT.uid)
    await core.scheduler.idle()  # the sync the login started ends before the database closes
    core.store.close()


def test_settings_listeners_hear_every_change(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    heard: list[tuple[bool, bool]] = []
    core.settings_listeners.append(lambda settings: heard.append((settings.start_with_windows, settings.sync_paused)))
    core.update_settings({"start_with_windows": True})
    core.set_paused(True)
    assert heard == [(True, False), (True, True)]
    core.store.close()


def _core_watching(tmp_path, clock, names: set[str]) -> Core:
    """A demo core whose process list is `names`, which the test changes."""
    return Core(
        AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: names
    )


def _polls(core: Core, clock, names: set[str], playing: bool, count: int) -> None:
    names.clear()
    if playing:
        names.add("genshinimpact.exe")
    for _ in range(count):
        core.gamewatch.poll_once()
        clock.advance(seconds=5)


def test_game_listeners_hear_start_and_stop(tmp_path, clock):
    names: set[str] = set()
    core = _core_watching(tmp_path, clock, names)
    heard: list[bool] = []
    core.game_listeners.append(heard.append)
    try:
        _polls(core, clock, names, False, 1)
        assert heard == []
        _polls(core, clock, names, True, 6)  # 25 seconds: a session this short is dropped, and plans no sync after it
        assert heard == [True] and core.status()["game"]["running"] is True
        _polls(core, clock, names, False, 1)
        assert heard == [True]  # one poll without the game is not yet the end
        _polls(core, clock, names, False, 1)
        assert heard == [True, False] and core.status()["game"]["running"] is False
    finally:
        core.store.close()


def test_game_listeners_are_told_after_the_scheduler(tmp_path, clock, monkeypatch):
    names: set[str] = set()
    core = _core_watching(tmp_path, clock, names)
    told: list[str] = []
    monkeypatch.setattr(core.scheduler, "on_game_started", lambda: told.append("scheduler started"))
    monkeypatch.setattr(core.scheduler, "on_game_stopped", lambda counted: told.append("scheduler stopped"))
    core.game_listeners.append(lambda playing: told.append("game started" if playing else "game stopped"))
    try:
        _polls(core, clock, names, True, 6)
        _polls(core, clock, names, False, 2)
        assert told == ["scheduler started", "game started", "scheduler stopped", "game stopped"]
    finally:
        core.store.close()


def test_a_failing_game_listener_is_logged_and_skipped(tmp_path, clock, caplog):
    names: set[str] = set()
    core = _core_watching(tmp_path, clock, names)
    heard: list[bool] = []

    def broken(playing: bool) -> None:
        raise RuntimeError("a listener of the shell failed")

    core.game_listeners.extend([broken, heard.append])
    try:
        _polls(core, clock, names, True, 6)
        _polls(core, clock, names, False, 2)
        assert heard == [True, False]  # the next listener heard both, and the game's watch went on
        assert caplog.text.count("a game listener failed") == 2
    finally:
        core.store.close()


def test_game_listeners_hear_the_start_even_when_the_scheduler_fails(tmp_path, clock, monkeypatch):
    names: set[str] = set()
    core = _core_watching(tmp_path, clock, names)
    heard: list[bool] = []

    def broken() -> None:
        raise RuntimeError("the scheduler failed")

    monkeypatch.setattr(core.scheduler, "on_game_started", broken)
    core.game_listeners.append(heard.append)
    try:
        names.add("genshinimpact.exe")
        with pytest.raises(RuntimeError, match="the scheduler failed"):
            core.gamewatch.poll_once()
        assert heard == [True]  # the overlay still shows over the game
    finally:
        core.store.close()


class _KeptLink:
    """Stands in for the importer where the core asks it for the updates of the link it kept: it counts the asks."""

    def __init__(self) -> None:
        self.asked = 0
        self.dropped = 0

    async def auto_update(self) -> bool:
        self.asked += 1
        return True

    def drop_link(self) -> None:
        self.dropped += 1

    async def stop(self) -> None:
        return None


class _ClockSleep:
    """A sleep that ends once the fake clock has moved as far as it was asked for: the test moves the clock by hand."""

    def __init__(self, clock) -> None:
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        until = self._clock.now() + timedelta(seconds=seconds)
        while self._clock.now() < until:
            await asyncio.sleep(0)


async def _turns() -> None:
    """Give the tasks of the core a few turns."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _let_pass(clock, minutes: int) -> None:
    """Let `minutes` of the fake clock pass, five at a time, the tasks having their turns after each."""
    for _ in range(minutes // 5):
        clock.advance(minutes=5)
        await _turns()


def _core_updating(tmp_path, clock, names: set[str], monkeypatch) -> tuple[Core, _KeptLink]:
    """A demo core whose process list is `names` and whose importer is a stand-in; the sync's own plans for the game's
    start and end are another test's, and so are the updates with the login (as for a player without one)."""
    core = Core(
        AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=_ClockSleep(clock), process_lister=lambda: names
    )
    kept = core.wish_import = _KeptLink()
    monkeypatch.setattr(core.scheduler, "on_game_started", lambda: None)
    monkeypatch.setattr(core.scheduler, "on_game_stopped", lambda counted: None)
    monkeypatch.setattr(core, "_login_refresh_on", lambda: False)
    return core, kept


async def test_with_the_login_the_game_asks_for_no_update_of_its_own(tmp_path, clock, monkeypatch):
    # The updates with the login come every half hour whether the game runs or not: the game's own would be a second.
    names: set[str] = set()
    core, kept = _core_updating(tmp_path, clock, names, monkeypatch)
    monkeypatch.setattr(core, "_login_refresh_on", lambda: True)
    try:
        names.add("genshinimpact.exe")
        core.gamewatch.poll_once()
        await _turns()
        await _let_pass(clock, 65)
        names.clear()
        core.gamewatch.poll_once()
        core.gamewatch.poll_once()
        await _turns()
        assert kept.asked == 0
    finally:
        await core.stop()


async def test_the_core_imports_every_half_hour_while_the_game_runs_and_once_when_it_closes(
    tmp_path, clock, monkeypatch
):
    names: set[str] = set()
    core, kept = _core_updating(tmp_path, clock, names, monkeypatch)
    try:
        await _let_pass(clock, 180)
        assert kept.asked == 0  # no game runs, so none is asked for
        names.add("genshinimpact.exe")
        core.gamewatch.poll_once()  # the game starts
        await _turns()
        await _let_pass(clock, 65)
        assert kept.asked == 2  # at 30 and at 60 minutes
        names.clear()
        core.gamewatch.poll_once()
        core.gamewatch.poll_once()  # gone for two polls: the game closed
        await _turns()
        assert kept.asked == 3  # once more
        await _let_pass(clock, 180)
        assert kept.asked == 3  # and no more
        # The next time the game runs it starts over, half an hour after its start.
        names.add("genshinimpact.exe")
        core.gamewatch.poll_once()
        await _turns()
        await _let_pass(clock, 25)
        assert kept.asked == 3
        await _let_pass(clock, 5)
        assert kept.asked == 4
    finally:
        await core.stop()


async def test_a_failing_update_is_logged_and_the_next_one_still_comes(tmp_path, clock, monkeypatch, caplog):
    names = {"genshinimpact.exe"}
    core, kept = _core_updating(tmp_path, clock, names, monkeypatch)

    async def breaks_once() -> bool:
        kept.asked += 1
        if kept.asked == 1:
            raise RuntimeError("the update broke")
        return True

    kept.auto_update = breaks_once
    try:
        core.gamewatch.poll_once()
        await _turns()
        await _let_pass(clock, 65)
        assert kept.asked == 2
        assert caplog.text.count("the update of the wish history with the kept link failed") == 1
    finally:
        await core.stop()


async def test_closing_guild_board_drops_the_link(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    kept = core.wish_import = _KeptLink()
    await core.stop()
    assert kept.dropped == 1


def test_the_status_says_whether_a_tray_icon_keeps_guild_board_running(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert core.status()["has_tray"] is False  # alone, or in a window that closing ends
    core.has_tray = True
    assert core.status()["has_tray"] is True
    core.store.close()


async def test_the_demo_records_its_made_up_list_as_the_demos_own(tmp_path, clock):
    # It downloads nothing, so its list must not read as one downloaded from Paimon.moe.
    core = Core(AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set())
    try:
        preview = await core.lists.check()
        assert core.lists.install(preview["sha256"])["source"] == "demo"
        assert core.lists.undo()["source"] == "bundled"
    finally:
        core.store.close()


def test_the_demo_core_holds_the_list_and_made_up_progress(tmp_path, clock):
    core = Core(AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set())
    try:
        model = core.achievements(Query())
        assert model["list"]["stages"] == 1854 and model["list"]["source"] == "bundled"
        assert model["query"]["category"] == "0"  # Wonders of the World has something left
        assert model["hoyolab"]["count"] > 1000
        assert "astra" not in model
        hilichurl = core.achievements(Query(search="Unusual Hilichurl"))["groups"][0]
        assert hilichurl["counter"]["value"] == 37
        assert (hilichurl["counter"]["unit"], hilichurl["counter"]["unit_one"]) == ("kills", "kill")  # Guild Board's notes
        assert [s["status"] for s in hilichurl["stages"]] == ["done", "done", "in_progress"]
        assert core.store.labels()["static.primogem"].icon_url.startswith("https://upload-static.hoyoverse.com/")
    finally:
        core.store.close()


def test_the_demo_has_timers_in_each_state(tmp_path, clock):
    # At 2026-10-13 08:00 UTC, where the browser tests and the pictures start the demo.
    core = _core(tmp_path, clock, demo=True)
    try:
        timers = core.timers_model()["timers"]
        assert {timer["name"]: (timer["state"], timer["ready_at"]) for timer in timers} == {
            "Teapot · Jade Field": ("ready", "2026-10-13T03:00:00+00:00"),  # planted 75 h before
            "Fishing spot · Luhua Pool, Liyue": ("ready", "2026-10-12T08:00:00+00:00"),  # emptied 4 days before
            "Unusual Hilichurl": ("running", "2026-10-13T18:00:00+00:00"),  # defeated 2 h before
            "Teapot · Luxuriant Glebe": ("running", "2026-10-15T10:00:00+00:00"),  # planted 20 h before
            "Teapot · Orderly Meadow": ("running", "2026-10-14T18:00:00+00:00"),  # the teapot showed 34 h
            "Fishing spot · Cider Lake (1), Mondstadt": ("running", "2026-10-15T08:00:00+00:00"),  # emptied 24 h before
            "Parametric Transformer": ("running", "2026-10-19T00:00:00+00:00"),  # used 30 h before, back after 166
            "Shrine visits": ("running", "2026-10-14T03:00:00+00:00"),  # visited today: back at the next reset
        }
        own = {timer["name"]: timer for timer in timers if timer["own"]}
        assert set(own) == {"Parametric Transformer", "Shrine visits"}
        assert own["Parametric Transformer"]["rule"] == {"kind": "hours", "hours": 166}
        assert (own["Shrine visits"]["rule"], own["Shrine visits"]["visits"]) == (
            {"kind": "resets", "resets": 1}, {"done": 2, "of": 3},
        )
        hilichurl = next(timer for timer in timers if timer["preset"] == "hilichurl")
        assert hilichurl["cleared_at"] == "2026-10-13T06:00:00+00:00"
        assert hilichurl["achievement"]["counter"]["value"] == 37  # the demo's count holds that kill already
        assert core.store.newest_change_label() is None  # the demo's made-up timers cannot be undone
    finally:
        core.store.close()


def test_the_demo_logs_commission_days(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        assert {stage: sorted(map(str, days)) for stage, days in core.store.appearances().items()} == {
            84508: ["2026-10-10", "2026-10-12"],  # Pirates! Argh!, left, came up twice
            84509: ["2026-10-11"],  # A Nourishing Friendship, done
            84510: ["2026-10-12"],  # Love Is All Around, done
        }
        model = core.commissions_model(None)  # Liyue, where Settings starts
        cards = {card["id"]: card for card in model["cards"] + model["done"]}
        assert (cards[84508]["appeared_today"], cards[84508]["last_appeared"]) == (False, "2026-10-12")
        assert (cards[84509]["last_appeared"], cards[84510]["last_appeared"]) == ("2026-10-11", "2026-10-12")
        # All's Well That Ends Well and Swordseeker, the other two left there, have no day yet.
        assert cards[84524]["last_appeared"] is None and cards[84564]["last_appeared"] is None
        assert core.store.newest_change_label() is None  # the demo's made-up days cannot be undone
    finally:
        core.store.close()


def test_a_live_core_has_only_the_default_timers_and_no_commission_days(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        # The first start adds the default pre-sets, never cleared, and none of the demo's timers or days.
        assert {row.preset for row in core.store.timers()} == {
            "hilichurl", "teapot.jade_field", "teapot.luxuriant_glebe", "teapot.orderly_meadow",
        }
        assert (core.store.timer_events(), core.store.appearances()) == ([], {})
    finally:
        core.store.close()


def test_before_the_first_sync_the_achievements_have_no_count_from_hoyolab(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        model = core.achievements(Query())
        assert model["hoyolab"] is None
        assert model["totals"]["done"] == 0
    finally:
        core.store.close()


async def test_an_import_without_a_file_is_refused(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        with pytest.raises(ImportFileError) as error:
            await _as_the_shell_calls(core.preview_import(None, None, None))
        assert str(error.value) == "Choose a file to import."
    finally:
        core.store.close()


async def test_a_chosen_file_is_imported_once_it_is_the_one_previewed(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        text = paimon_export({"": {"0": {"80127": True}}}).decode("utf-8")
        preview = await _as_the_shell_calls(core.preview_import(text, "export.json", None))
        assert preview["done"] == 1 and preview["file"] == "export.json"
        with pytest.raises(ImportChanged):
            await _as_the_shell_calls(core.apply_import(text, "export.json", None, "not the previewed file"))
        await _as_the_shell_calls(core.apply_import(text, "export.json", None, preview["fingerprint"]))
        assert core.store.progress()[80127].status == "done"
    finally:
        core.store.close()


async def test_an_achievement_file_is_read_off_the_event_loop(tmp_path, clock, monkeypatch):
    # A file of up to 30 MB is decoded and read as JSON before it is planned. That happens in a worker thread; the plan,
    # which reads the store, stays on the event loop's thread.
    import threading

    from guildboard.achievements import importers

    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    read: list[int] = []
    real = importers.read_file
    monkeypatch.setattr(importers, "read_file", lambda *a, **k: read.append(threading.get_ident()) or real(*a, **k))
    try:
        text = paimon_export({"": {"0": {"80127": True}}}).decode("utf-8")
        preview = await _as_the_shell_calls(core.preview_import(text, "export.json", None))
        await _as_the_shell_calls(core.apply_import(text, "export.json", None, preview["fingerprint"]))
        assert core.store.progress()[80127].status == "done"
        assert len(read) == 2 and threading.get_ident() not in read
    finally:
        core.store.close()


async def test_a_chosen_file_is_imported_from_its_text_and_the_pages_are_told_once(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        text = paimon_export({"": {"0": {"81732": True}}}).decode("utf-8")
        preview = await _as_the_shell_calls(core.preview_import(text, None, None))
        assert (preview["file"], preview["done"]) == ("the chosen file", 1)
        events = core.bus.subscribe()
        await _as_the_shell_calls(core.apply_import(text, None, None, preview["fingerprint"]))
        assert core.store.progress()[81732].status == "done"
        assert [events.get_nowait()["type"] for _ in range(events.qsize())] == ["achievements_changed"]
    finally:
        core.store.close()


async def test_only_the_live_core_downloads_icons(tmp_path, clock, monkeypatch):
    opened: list[str] = []

    def recorder(request, timeout):  # stands in for the network, so that this test reaches nothing in any case
        opened.append(request.full_url)
        raise OSError("the tests contact nothing")

    monkeypatch.setattr(netpolicy, "_open", recorder)
    demo = Core(
        AppPaths(tmp_path / "demo"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set()
    )
    try:
        assert await demo.icons.get(PRIMOGEM_ICON.icon_url) is None
        assert opened == []  # the demo contacts nothing
    finally:
        demo.store.close()
    live = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        assert await live.icons.get(PRIMOGEM_ICON.icon_url) is None  # the recorder refused it
        assert opened == [PRIMOGEM_ICON.icon_url]
    finally:
        live.store.close()


def test_the_static_pictures_are_served_from_hoyoverse():
    labels = {label.key: label for label in STATIC_ICONS}
    assert set(labels) == {"static.primogem", "static.resin", "static.realm_currency", "static.transformer"}
    assert labels["static.transformer"].icon_url == (
        "https://bbs.hoyolab.com/hoyowiki/picture/object/Parametric%20Transformer_icon.png"
    )
    assert all(netpolicy.is_hoyoverse(label.icon_url) for label in labels.values())


def test_the_static_pictures_are_the_addresses_the_design_names():
    # Spec 7.1 of the look pass. Each answered 200 with an image when it was checked on 2026-10-22; an address with a
    # typo would only show the sign in its place, so the exact text is kept here.
    assert {label.key: label.icon_url for label in STATIC_ICONS} == {
        "static.primogem": PRIMOGEM_ICON.icon_url,
        "static.resin": (
            "https://act-upload.hoyoverse.com/event-ugc-hoyowiki/2025/07/03/237301566/"
            "4328202cd795d7c4a7d5cd54e331c43b_1224981524026882289.png"
        ),
        "static.realm_currency": (
            "https://upload-static.hoyoverse.com/hoyolab-wiki/2023/03/29/194600931/"
            "853777b20dff4c545de10c30b8f4543c_3219915689637833360.png"
        ),
        "static.transformer": "https://bbs.hoyolab.com/hoyowiki/picture/object/Parametric%20Transformer_icon.png",
    }
    assert PRIMOGEM_ICON in STATIC_ICONS and PRIMOGEM_ICON.key == "static.primogem"


def test_the_core_keeps_a_label_for_every_static_picture_so_that_the_icon_route_serves_it(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        held = core.store.labels("static")
        for label in STATIC_ICONS:
            assert held[label.key].icon_url == label.icon_url
    finally:
        core.store.close()


# ---------------------------------------------------------------- Pulls: the seams (the Pulls plan, Task 0)

def test_the_core_builds_the_pulls_modules_and_tells_the_pages_when_pulls_change(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        assert isinstance(core.pulls, Planner) and isinstance(core.wish_import, WishImporter)
        assert isinstance(core.banners, Banners) and isinstance(core.wiki, WikiIcons)
        events = core.bus.subscribe()
        core.pulls_changed()
        assert [events.get_nowait() for _ in range(events.qsize())] == [{"type": "pulls_changed"}]
    finally:
        core.store.close()


def test_the_importer_is_given_the_links_by_read_links():
    # The cache can hold several links, and the import tries them one by one: its constructor takes the function that
    # gives them, in the place the contract gives it; then, by name, whether the first run's consent is given (R21).
    assert list(inspect.signature(WishImporter).parameters) == [
        "store", "clock", "bus", "read_links", "fetch", "sleep", "after", "ready", "ledger_fetch", "ledger_user", "login",
    ]
    assert inspect.signature(WishImporter).parameters["ready"].kind is inspect.Parameter.KEYWORD_ONLY


def test_only_the_demo_core_seeds_its_wishes_and_banners(tmp_path, clock, monkeypatch):
    seeded: list[tuple[str, datetime]] = []
    monkeypatch.setattr("guildboard.core.seed_wishes", lambda store, now: seeded.append(("wishes", now)))
    monkeypatch.setattr("guildboard.core.seed_banners", lambda store, now: seeded.append(("banners", now)))
    live = _core(tmp_path / "live", clock, demo=False, source=AccountSource())
    live.store.close()
    assert seeded == []
    demo = _core(tmp_path / "demo", clock, demo=True)
    demo.store.close()
    assert seeded == [("wishes", clock.now()), ("banners", clock.now())]


def test_an_import_left_running_when_guild_board_closed_is_failed_at_the_next_start(tmp_path, clock):
    first = _core(tmp_path, clock, demo=True)
    left = first.store.start_wish_import(clock.now())
    first.store.close()
    clock.advance(hours=2)
    second = _core(tmp_path, clock, demo=True)
    try:
        row = next(row for row in second.store.wish_imports() if row.id == left)
        assert (row.outcome, row.message) == ("failed", "TUSKFiZZY closed before this import finished.")
        assert row.finished_at == clock.now()
    finally:
        second.store.close()


async def test_start_runs_the_banners_and_stop_ends_them(tmp_path, clock, monkeypatch):
    core = _core(tmp_path, clock, demo=True)
    running, ended = asyncio.Event(), []

    async def banners_run() -> None:
        running.set()
        try:
            await asyncio.Event().wait()  # until stop() ends it
        except asyncio.CancelledError:
            ended.append("cancelled")
            raise

    monkeypatch.setattr(core.banners, "run", banners_run)
    core.start()
    await core.scheduler.idle()
    await asyncio.wait_for(running.wait(), 5)
    assert ended == []
    await core.stop()
    assert ended == ["cancelled"]


def test_the_banners_are_asked_for_the_rank_the_overview_read_and_for_60_before_one(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        assert core._adventure_rank() == 60
        core.store.set_latest_doc("overview", clock.now(), {"nickname": "Traveler", "adventure_rank": 58})
        assert core._adventure_rank() == 58
        core.store.set_latest_doc("overview", clock.now(), {"nickname": "Traveler", "adventure_rank": None})
        assert core._adventure_rank() == 60
    finally:
        core.store.close()


LINE = {"kind": "new", "title": "A line.", "text": "Why."}


def _notes_for(version: str, monkeypatch) -> None:
    """Make changes.json hold one entry, for this version, so the tests do not depend on the shipped notes."""
    from guildboard import core as core_module
    monkeypatch.setattr(core_module, "load_changes", lambda: {version: [LINE]})


def test_an_update_from_before_the_card_shows_it_and_a_first_install_does_not(tmp_path, clock, monkeypatch):
    _notes_for(__version__, monkeypatch)
    fresh = _core(tmp_path / "new", clock, demo=True)
    assert fresh.status()["whats_new"] is None
    assert fresh.settings.last_seen_version == __version__
    assert load_settings(fresh.paths.settings_path).last_seen_version == __version__
    fresh.store.close()

    old = _core(tmp_path / "old", clock, demo=True)  # a database from before the card
    old.store.close()
    (tmp_path / "old" / "data" / "settings.json").unlink(missing_ok=True)
    again = _core(tmp_path / "old", clock, demo=True)
    assert again.status()["whats_new"] == {"version": __version__, "items": [LINE]}
    again.store.close()


def test_the_card_shows_once(tmp_path, clock, monkeypatch):
    _notes_for(__version__, monkeypatch)
    first = _core(tmp_path, clock, demo=True)
    first.store.close()
    (tmp_path / "data" / "settings.json").unlink(missing_ok=True)
    core = _core(tmp_path, clock, demo=True)
    assert core.status()["whats_new"] is not None
    core.whats_new_seen()
    assert core.status()["whats_new"] is None
    core.store.close()
    assert _core(tmp_path, clock, demo=True).status()["whats_new"] is None


def test_no_card_without_notes_for_this_version(tmp_path, clock, monkeypatch):
    _notes_for("0.0.1", monkeypatch)
    first = _core(tmp_path, clock, demo=True)
    first.store.close()
    (tmp_path / "data" / "settings.json").unlink(missing_ok=True)
    core = _core(tmp_path, clock, demo=True)
    assert core.status()["whats_new"] is None
    assert core.whats_new_history(5) == [{"version": "0.0.1", "items": [LINE]}]
    core.store.close()


def test_the_history_is_newest_first_by_number_and_holds_no_version_not_yet_out(tmp_path, clock, monkeypatch):
    # Settings shows what this copy has, so notes written ahead of a release wait for it; the file's order is no rule.
    from guildboard import core as core_module
    notes = {"0.0.9": [LINE], "0.0.10": [LINE], "9.0.0": [LINE], "0.0.2": [LINE]}
    monkeypatch.setattr(core_module, "load_changes", lambda: notes)
    monkeypatch.setattr(core_module, "__version__", "0.0.10")
    core = _core(tmp_path, clock, demo=True)
    assert [entry["version"] for entry in core.whats_new_history(2)] == ["0.0.10", "0.0.9"]
    core.store.close()


# ---------------------------------------------------------------- the histories updated with the login

class _MintingSource(AccountSource):
    """A live source whose login can be given history keys: each ask is remembered; `refuse` makes HoYoverse refuse the
    login."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = False
        self.minted: list[str] = []

    def can_mint(self) -> bool:
        return True

    async def mint_key(self, auth_appid):
        self.minted.append(auth_appid)
        if self.refuse:
            from guildboard.records import LoginNeeded

            raise LoginNeeded("HoYoLAB no longer accepts the saved login.")
        return None


class _Refreshing:
    """Stands in for the importer where the core asks it for the updates with the login: each ask is remembered with
    whether a sync had ended by then, and the mint is called once for each kind."""

    def __init__(self, core) -> None:
        self.core = core
        self.asks: list[bool] = []
        self.running = False
        self.dropped_minted = 0

    async def login_update(self, mint, still=lambda: True) -> bool:
        self.asks.append(self.core.store.last_ok_at() is not None)
        await mint("webview_gacha")
        await mint("csc")
        return True

    async def auto_update(self) -> bool:
        return False

    def drop_minted(self) -> None:
        self.dropped_minted += 1

    def drop_link(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _live_core_with_login(tmp_path, clock, *, consent=True, cookies=None):
    from guildboard.config import Settings, save_settings

    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    TokenStore(paths.token_path).save(StoredToken(
        cookies or {**COOKIES, "cookie_token_v2": "v2_COOKIE-0123456789", "account_id_v2": "123456"},
        700000001, "os_euro", "Traveler",
    ))
    save_settings(paths.settings_path, Settings(risk_acknowledged=consent))
    source = _MintingSource()
    core = Core(paths, demo=False, clock=clock, source=source, process_lister=lambda: set())  # real waits: it starts
    refreshing = core.wish_import = _Refreshing(core)
    return core, source, refreshing


def _real_importer(core, clock, ledgers=None):
    """The core's own kind of importer, reading the game's history from `ledgers` (ledger_samples.Ledgers)."""
    import functools

    from guildboard.ledger import api as ledger_api

    return WishImporter(
        core.store, clock, core.bus, None, None, FakeSleep(clock), None, ready=lambda: True,
        ledger_fetch=functools.partial(ledger_api.fetch_page, fetch=ledgers) if ledgers is not None else None,
        ledger_user=functools.partial(ledger_api.fetch_user, fetch=ledgers) if ledgers is not None else None,
        login=core._login_status,
    )


async def _until(condition) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it did not happen")


async def test_the_histories_update_with_the_login_after_the_first_sync_and_every_half_hour(
    tmp_path, clock, monkeypatch
):
    from guildboard import core as core_module

    monkeypatch.setattr(core_module, "AUTO_EVERY", timedelta(milliseconds=50))
    core, source, refreshing = _live_core_with_login(tmp_path, clock)
    assert core._login_refresh_on()

    async def no_banners() -> None:  # the announcements' reads are another test's
        return None

    core.banners.run = no_banners
    core.characters.wanted = lambda *keys: None  # the wiki's reads are another test's (the hands-off tests')
    core.characters.warm_art = lambda: None  # so are the pictures kept ahead after a sync
    core.start()
    try:
        await _until(lambda: len(refreshing.asks) >= 3)
        assert refreshing.asks[0] is True  # the first came once the start's sync had ended
        assert source.minted[:2] == ["webview_gacha", "csc"]
    finally:
        await core.stop()
    assert core._refresh_task is None


async def test_a_refused_login_stops_only_the_updates_with_it_and_the_syncs_go_on(tmp_path, clock):
    core, source, refreshing = _live_core_with_login(tmp_path, clock)
    source.refuse = True
    try:
        core.wish_import = _real_importer(core, clock)
        assert await core.refresh_from_login() is False
        # The sync is not blocked: ltoken_v2 may still read everything else.
        assert core.scheduler.current_state() != "login_needed"
        assert (await core.scheduler.trigger("manual")).run
        assert core.store.last_ok_at() is not None
        # The pages say so, and no key is asked for again: no storm of asks.
        assert core.wish_import.state()["login"] == "refused"
        assert await core.refresh_from_login() is False
        assert source.minted == ["webview_gacha"]
        # A new login starts the updates again, without the old login's keys.
        source.refuse = False
        core.wish_import = refreshing
        core._auth_changed()
        assert refreshing.dropped_minted == 1 and core._login_refresh_on()
        assert await core.refresh_from_login() is True
    finally:
        await core.stop()


async def test_a_key_asked_for_across_a_change_of_login_is_not_kept(tmp_path, clock):
    from guildboard.pulls.gamecache import MintedLink
    from ledger_samples import Ledgers, csc_link, entry

    core, source, _ = _live_core_with_login(tmp_path, clock)
    ledgers = Ledgers({"GetPrimogemLog": [entry(5)]}, keys=["CSC-KEY-0123456789-ABCDEFGH"])
    core.wish_import = _real_importer(core, clock, ledgers)

    async def mint_across_a_login(auth_appid):
        core._auth_changed()  # the player logs in to another account while the key is asked for
        return MintedLink(csc_link(), 700000001, "os_euro")

    core.source.mint_key = mint_across_a_login
    try:
        assert await core.refresh_from_login() is False
        assert core.wish_import._kept is None and core.wish_import._kept_ledger is None and ledgers.asked == []
    finally:
        await core.stop()


async def test_the_game_history_is_kept_for_the_account_the_key_was_given_for(tmp_path, clock):
    from guildboard.pulls.gamecache import MintedLink
    from ledger_samples import Ledgers, csc_link, entry

    core, source, _ = _live_core_with_login(tmp_path, clock)  # the stored login's account is 700000001
    ledgers = Ledgers({"GetPrimogemLog": [entry(5)]}, keys=["CSC-KEY-0123456789-ABCDEFGH"])
    core.wish_import = _real_importer(core, clock, ledgers)

    async def mint_for_another(auth_appid):
        return MintedLink(csc_link(), 800000002, "os_asia") if auth_appid == "csc" else None

    core.source.mint_key = mint_for_another
    try:
        assert await core.refresh_from_login() is True
        assert len(core.store.ledger_entries(800000002, "primogem")) == 1
        assert core.store.ledger_entries(700000001, "primogem") == []
    finally:
        await core.stop()


async def test_no_update_with_the_login_without_consent_the_cookie_token_or_while_sync_is_paused(tmp_path, clock):
    core, source, refreshing = _live_core_with_login(tmp_path / "a", clock, consent=False)
    assert not core._login_refresh_on() and await core.refresh_from_login() is False
    assert core._login_status() is None
    await core.stop()

    class NoToken(_MintingSource):
        def can_mint(self) -> bool:
            return False

    core, _, refreshing = _live_core_with_login(tmp_path / "b", clock)
    core.source = NoToken()
    assert not core._login_refresh_on() and await core.refresh_from_login() is False
    await core.stop()

    core, source, refreshing = _live_core_with_login(tmp_path / "c", clock)
    core.set_paused(True)
    assert await core.refresh_from_login() is False and source.minted == []
    core.set_paused(False)
    assert await core.refresh_from_login() is True
    assert refreshing.asks == [False]
    await core.stop()


async def test_a_mint_that_breaks_fails_nothing_else(tmp_path, clock, caplog):
    core, source, refreshing = _live_core_with_login(tmp_path, clock)

    async def broken(mint, still=lambda: True):
        raise RuntimeError("broken")

    refreshing.login_update = broken
    try:
        assert await core.refresh_from_login() is False
        assert "the update of the histories with the login failed" in caplog.text
        assert core.scheduler.current_state() != "login_needed" and core._login_refresh_on()
    finally:
        await core.stop()


async def test_the_demo_updates_with_its_stand_in_login_and_recovers_from_its_login_fault(tmp_path, clock):
    from guildboard.records import LoginNeeded

    core = _core(tmp_path, clock, demo=True)
    try:
        assert core._login_refresh_on() and core.wish_import.state()["login"] == "on"
        core.source.fault = LoginNeeded("HoYoLAB no longer accepts the saved login.")
        assert await core.refresh_from_login() is False
        assert core.wish_import.state()["login"] == "refused"
        # The fault ended, as POST /api/demo/fault with "none" ends it: the stand-in login is accepted again.
        core.source.fault = None
        core.forget_login_refusal()
        assert await core.refresh_from_login() is True
        assert core.wish_import.state()["found"] == 3  # the demo's three new wishes
        assert core.wish_import.ledger_state()["state"] == "done"
        assert core.wish_import.state()["login"] == "on"
    finally:
        await core.stop()


# ---------------------------------------------------------------- Import reads with the login first (the freshness design, section 5)

_PRESS_UID = 700000001
_PRESS_KEY = "MADE-UP-KEY-0123456789"


class _PressedGame:
    """What a press reads of the game's files (the importer's cache reader): each read remembered, and the one link it
    finds is made up."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self):
        from pulls_samples import wish_link

        self.reads += 1
        return [wish_link(_PRESS_KEY)]


def _pressed_core(tmp_path, clock, *, login: bool = True):
    """A live core whose importer reads three wishes from HoYoverse's stand-in, with a key from the login (its mint gives
    the made-up key) or, from the game's files, with the made-up link of `_PressedGame`. `login` False is a core with no
    login."""
    import functools

    from pulls_samples import History, record, wish_link

    from guildboard.pulls import wishapi
    from guildboard.pulls.gamecache import MintedLink

    if login:
        core, source, _ = _live_core_with_login(tmp_path, clock)
    else:
        paths = AppPaths(tmp_path / "data")
        paths.ensure()
        source = AccountSource()
        core = Core(paths, demo=False, clock=clock, source=source, process_lister=lambda: set())
        core.update_settings({"risk_acknowledged": True})
    first = 1_760_000_000_000_000_000
    history = History(
        {"301": [record(first + n, "301", f"2026-10-12 21:1{n}:03", "Fischl", "Character", 4) for n in (1, 2, 3)]},
        keys=[_PRESS_KEY],
    )
    game = _PressedGame()
    core.wish_import = WishImporter(
        core.store, clock, core.bus, game, functools.partial(wishapi.fetch_page, fetch=history), FakeSleep(clock), None,
        ready=lambda: True, login=core._login_status,
    )

    async def mint_key(auth_appid):
        source.minted.append(auth_appid)
        if source.refuse:
            from guildboard.records import LoginNeeded

            raise LoginNeeded("HoYoLAB no longer accepts the saved login.")
        return MintedLink(wish_link(_PRESS_KEY), _PRESS_UID, "os_euro") if auth_appid == "webview_gacha" else None

    if login:
        source.mint_key = mint_key
    return core, source, game, history


async def _press_ended(core) -> None:
    await _until(lambda: core._import_task is None or core._import_task.done())
    await _until(lambda: not core.wish_import.running)
    await asyncio.gather(*core.wish_import._tasks)  # the fallback's own task, when one started


async def test_import_with_the_login_reads_no_game_file(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert source.minted == ["webview_gacha"]  # the mint is asked
        assert game.reads == 0  # the game's files are not
        assert core.store.wish_count(_PRESS_UID) == 3
        assert core.wish_import.state()["state"] == "done" and core.wish_import.state()["login"] == "on"
    finally:
        await core.stop()


async def test_import_falls_back_to_the_game_when_the_login_is_refused(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)
    source.refuse = True
    try:
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert source.minted == ["webview_gacha"]
        assert game.reads == 1  # the game-file Import ran on the same press
        assert core.store.wish_count(_PRESS_UID) == 3 and core.wish_import.state()["state"] == "done"
        assert core.wish_import.state()["login"] == "refused" and core._login_status() == "refused"
        # The next press asks no key of a refused login: it reads the game at once.
        assert await core.import_pressed() == "game"
        await _press_ended(core)
        assert source.minted == ["webview_gacha"] and game.reads == 2
    finally:
        await core.stop()


async def test_import_falls_back_to_the_game_when_the_login_gives_no_key(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)

    async def no_key(auth_appid):
        source.minted.append(auth_appid)
        return None

    source.mint_key = no_key
    try:
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert source.minted == ["webview_gacha"] and game.reads == 1
        assert core.store.wish_count(_PRESS_UID) == 3
        assert core._login_status() == "on"  # no key is no refusal: the automatic updates go on
    finally:
        await core.stop()


async def test_import_without_a_login_reads_the_game(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock, login=False)
    try:
        assert await core.import_pressed() == "game"
        await _press_ended(core)
        assert game.reads == 1 and core.store.wish_count(_PRESS_UID) == 3
        assert core.wish_import.state()["state"] == "done"
    finally:
        await core.stop()


async def test_import_with_the_sync_paused_reads_the_game(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)
    core.set_paused(True)
    try:
        assert await core.import_pressed() == "game"
        await _press_ended(core)
        assert source.minted == [] and game.reads == 1
    finally:
        await core.stop()


async def test_import_while_an_update_runs_is_busy(tmp_path, clock):
    from guildboard.pulls.importer import ImportBusy

    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        core.wish_import.running = True  # an update with the login (or an import) runs
        with pytest.raises(ImportBusy) as busy:
            await core.import_pressed()
        assert str(busy.value) == "An import is running."
        assert core._import_task is None and source.minted == [] and game.reads == 0
        core.wish_import.running = False
        # Two presses in the same instant: the second is refused too.
        assert await core.import_pressed() == "login"
        with pytest.raises(ImportBusy):
            await core.import_pressed()
        await _press_ended(core)
        assert game.reads == 0 and source.minted == ["webview_gacha"]
    finally:
        await core.stop()


async def test_the_login_run_has_reserved_the_importer_when_the_press_returns(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        assert await core.import_pressed() == "login"
        assert core.wish_import.running  # no gap in which another press, or a tick, could start a second run
        await _press_ended(core)
    finally:
        await core.stop()


async def test_import_before_the_first_runs_consent_is_refused_and_asks_nothing(tmp_path, clock):
    from guildboard.pulls.importer import ImportNotReady

    core, source, game, history = _pressed_core(tmp_path, clock)
    core.wish_import._ready = lambda: False
    try:
        with pytest.raises(ImportNotReady):
            await core.import_pressed()
        assert core._import_task is None and source.minted == [] and game.reads == 0
    finally:
        await core.stop()


async def test_a_run_with_the_login_that_fails_on_the_network_does_not_read_the_game(tmp_path, clock):
    from pulls_samples import TOO_FREQUENT

    core, source, game, history = _pressed_core(tmp_path, clock)
    history.refuse = lambda query: TOO_FREQUENT  # the key was given, then the page fetch fails
    try:
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert source.minted == ["webview_gacha"]
        assert game.reads == 0  # the run reports itself through the importer's states: no fallback
        assert core.wish_import.state()["state"] == "failed" and core.wish_import.state()["login"] == "failed"
        assert core.store.wish_count(_PRESS_UID) == 0
    finally:
        await core.stop()


async def test_a_login_run_that_follows_a_refused_press_link_clears_the_word_that_the_updates_stopped(tmp_path, clock):
    from pulls_samples import KEY_EXPIRED

    core, source, game, history = _pressed_core(tmp_path, clock)
    source.refuse = True
    try:
        # 1. The login is refused; 2. the game-file import on the same press keeps a link.
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert core._login_status() == "refused" and game.reads == 1
        assert core.wish_import.state()["auto"] is not None
        # 3. The player logs in again. 4. The login run uses the kept link, which HoYoverse now refuses.
        source.refuse = False
        core.forget_login_refusal()
        history.keys[_PRESS_KEY] = KEY_EXPIRED
        assert await core.refresh_from_login() is True
        assert core.wish_import.state()["auto_stopped"] == "expired" and source.minted == ["webview_gacha"]
        # 5. The next login run mints a key and succeeds: the word that the updates stopped goes.
        history.keys[_PRESS_KEY] = None
        assert await core.refresh_from_login() is True
        assert source.minted == ["webview_gacha", "webview_gacha"]
        assert core.wish_import.state()["state"] == "done" and core.wish_import.state()["auto_stopped"] is None
        assert game.reads == 1
    finally:
        await core.stop()


async def test_a_press_through_the_login_clears_the_word_that_the_updates_stopped(tmp_path, clock):
    from guildboard.pulls.importer import ImportBusy

    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        core.wish_import._auto_stopped = "expired"
        assert await core.import_pressed() == "login"
        assert core.wish_import.state()["auto_stopped"] is None  # at once, whatever the run comes to
        await _press_ended(core)
        assert core.wish_import.state()["auto_stopped"] is None
        # A press that is refused as busy leaves it as it was.
        core.wish_import._auto_stopped = "expired"
        core.wish_import.running = True
        with pytest.raises(ImportBusy):
            await core.import_pressed()
        assert core.wish_import.state()["auto_stopped"] == "expired"
        core.wish_import.running = False
    finally:
        await core.stop()


async def test_the_pages_are_not_told_the_history_updates_itself_while_the_sync_is_paused(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        assert core._login_status() == "on" and core.wish_import.state()["every_minutes"] == 30
        assert core.ledger()["import"]["every_minutes"] == 30
        heard = core.bus.subscribe()
        core.set_paused(True)
        assert core._login_status() is None  # the login is there, but nothing is read with it
        assert core.wish_import.state()["login"] is None and core.wish_import.state()["every_minutes"] is None
        assert core.ledger()["import"]["every_minutes"] is None
        assert "pulls_changed" in [heard.get_nowait()["type"] for _ in range(heard.qsize())]  # the pages read the model again
        core.set_paused(False)
        assert core._login_status() == "on" and core.wish_import.state()["every_minutes"] == 30
    finally:
        await core.stop()


async def test_the_pages_are_not_told_the_history_updates_itself_with_the_login_of_another_account(tmp_path, clock):
    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        core.store.set_meta("account_uid", "700000002")  # the history belongs to another game account than the login
        assert core._login_of_another_account() is not None
        assert core._login_status() is None
        assert core.wish_import.state()["login"] is None and core.wish_import.state()["every_minutes"] is None
        assert core.ledger()["import"]["every_minutes"] is None
        core.store.set_meta("account_uid", "700000001")
        assert core._login_status() == "on" and core.wish_import.state()["every_minutes"] == 30
        # A login the HoYoverse refused is still said, paused or not: the player has to log in again.
        core._login_refused = True
        core.set_paused(True)
        assert core._login_status() == "refused"
    finally:
        await core.stop()


async def test_the_planners_refusal_with_no_history_follows_the_logins_updates(tmp_path, clock):
    from guildboard.pulls.planner import PullsConflict

    core, source, game, history = _pressed_core(tmp_path, clock)
    try:
        assert core.pulls._login_on() is True
        with pytest.raises(PullsConflict, match="reading it now with your HoYoLAB login"):
            core.pulls.set_settings({"welkin": True})
        core.set_paused(True)
        assert core.pulls._login_on() is False
        with pytest.raises(PullsConflict, match="Open Wish"):
            core.pulls.set_settings({"welkin": True})
    finally:
        await core.stop()


async def test_the_demo_import_is_the_run_of_its_stand_in_login(tmp_path, clock):
    from guildboard.records import LoginNeeded

    core = _core(tmp_path, clock, demo=True)
    try:
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert core.wish_import.state()["state"] == "done" and core.wish_import.state()["found"] == 3
        assert core.wish_import.ledger_state()["state"] == "done"
        # With its stand-in login refused (a demo fault), the demo reads its stand-in links, as it always did.
        core.wish_import.drop_minted()  # its key is not kept for ever: the next one is asked for
        core.source.fault = LoginNeeded("HoYoLAB no longer accepts the saved login.")
        assert await core.import_pressed() == "login"
        await _press_ended(core)
        assert core.wish_import.state()["state"] == "done" and core._login_status() == "refused"
    finally:
        await core.stop()


async def test_refresh_characters_asks_the_scheduler_for_the_characters_group_within_its_limits(tmp_path, clock):
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    TokenStore(paths.token_path).save(StoredToken(dict(COOKIES), 700000001, "os_euro", "Traveler"))
    source = AccountSource()
    core = _core(tmp_path, clock, demo=False, source=source)
    assert core.refresh_characters() == {"started": True}
    await core.scheduler.idle()
    assert [group for group, _ in source.calls] == ["characters"]
    clock.advance(minutes=2)
    assert core.refresh_characters() == {"started": False, "wait_minutes": 3}  # the 5-minute gap, minutes left
    await core.scheduler.idle()
    assert [group for group, _ in source.calls] == ["characters"]
    core.settings.sync_paused = True
    clock.advance(minutes=10)
    assert core.refresh_characters() == {"started": False, "blocked": "paused"}
    core.store.close()


def test_refresh_characters_without_a_login_says_to_log_in(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert core.refresh_characters() == {"started": False, "login": True}
    core.store.close()


async def test_a_core_running_past_midnight_makes_the_next_daily(tmp_path, clock, monkeypatch):
    import guildboard.core as core_module

    monkeypatch.setattr(core_module, "DAILY_CHECK_EVERY", 0.01)
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    backups = core.paths.backups_dir
    task = asyncio.create_task(core._daily_backups())
    try:
        assert len(list(backups.glob("guildboard-daily-*.db"))) == 1  # the start's own
        await asyncio.sleep(0.05)
        assert len(list(backups.glob("guildboard-daily-*.db"))) == 1  # the same date: no second one
        clock.advance(days=1)
        for _ in range(100):
            if len(list(backups.glob("guildboard-daily-*.db"))) == 2:
                break
            await asyncio.sleep(0.02)
        assert len(list(backups.glob("guildboard-daily-*.db"))) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        core.store.close()


async def test_a_demo_core_makes_no_daily_copy(tmp_path, clock, monkeypatch):
    import guildboard.core as core_module

    monkeypatch.setattr(core_module, "DAILY_CHECK_EVERY", 0.01)
    core = _core(tmp_path, clock, demo=True)
    core.start()
    clock.advance(days=2)
    await asyncio.sleep(0.1)
    assert core._daily_task is None and not list(core.paths.backups_dir.glob("guildboard-daily-*.db"))
    await core.stop()


def test_the_ledger_account_is_asked_for_only_when_the_login_names_none(tmp_path, clock, monkeypatch):
    core = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        asked = []
        real = core.store.ledger_uids
        monkeypatch.setattr(core.store, "ledger_uids", lambda: asked.append(1) or real())
        core.store.set_meta("account_uid", "700000001")
        assert core.ledger_uid() == 700000001 and asked == []  # the scan of the import records is for the other cases
        core.store.drop_meta("account_uid")
        core.ledger_uid()
        assert asked == [1]
    finally:
        core.store.close()


def test_a_start_prunes_the_skipped_runs_of_over_ninety_days(tmp_path, clock):
    first = _core(tmp_path, clock, demo=False, source=AccountSource())
    first.store.add_skipped("manual", clock.now() - timedelta(days=100), "A sync ran less than 5 minutes ago.")
    first.store.add_skipped("manual", clock.now() - timedelta(days=1), "A sync ran less than 5 minutes ago.")
    first.store.close()
    second = _core(tmp_path, clock, demo=False, source=AccountSource())
    try:
        assert len(second.store.recent_runs()) == 1
    finally:
        second.store.close()


async def test_a_login_of_another_account_than_the_history_never_syncs(tmp_path, clock):
    # A restore of another account's backup (or a folder moved between PCs) leaves a login that is not the history's.
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    first = _core(tmp_path, clock, demo=False, source=AccountSource())
    first.store.set_meta("account_uid", "700000001")
    first.store.close()
    TokenStore(paths.token_path).save(StoredToken(dict(COOKIES), 800000002, "os_asia", "Lumine"))
    source = AccountSource()
    core = _core(tmp_path, clock, demo=False, source=source)
    try:
        status = core.status()["sync"]
        assert status["state"] == "login_needed"
        assert "UID 800000002" in status["message"] and "UID 700000001" in status["message"]
        decision = await core.scheduler.trigger("manual")
        assert decision.run is False and source.calls == []
        assert core._login_refresh_on() is False
        # The login of the history's own account syncs again.
        core.tokens.save(StoredToken(dict(COOKIES), 700000001, "os_euro", "Traveler"))
        core.scheduler.clear_block()
        assert (await core.scheduler.trigger("manual")).run is True
    finally:
        core.store.close()


def test_a_login_of_the_history_account_or_a_history_without_one_is_not_blocked(tmp_path, clock):
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    TokenStore(paths.token_path).save(StoredToken(dict(COOKIES), 800000002, "os_asia", "Lumine"))
    core = _core(tmp_path, clock, demo=False, source=AccountSource())  # no account named yet
    assert (core.status()["sync"]["state"], core.status()["sync"]["message"]) == ("ok", None)
    core.store.set_meta("account_uid", "800000002")
    core.store.close()
    again = _core(tmp_path, clock, demo=False, source=AccountSource())
    assert again.status()["sync"]["message"] is None
    again.store.close()
