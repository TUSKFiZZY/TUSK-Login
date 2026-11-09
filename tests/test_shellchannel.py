import json
import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import timedelta

import pytest

from guildboard.__main__ import main
from guildboard.config import AppPaths
from guildboard.core import NO_PICTURE, Core
from guildboard.records import GameAccount, Offline
from guildboard.shellchannel import CALLS, ShellChannel
from support import AccountSource, FakeSleep


@pytest.fixture
def core(tmp_path, clock):
    # A core with a login (a demo has none), whose source lists one made-up account.
    core = Core(
        AppPaths(tmp_path / "data"), demo=False, clock=clock, source=AccountSource(), sleep=FakeSleep(clock),
        process_lister=lambda: set(),
    )
    yield core
    core.store.close()


@pytest.fixture
def lines():
    return []


@pytest.fixture
def channel(core, lines):
    channel = ShellChannel(core, lines.append)
    channel.attach()
    return channel


def _start(tmp_path):
    return subprocess.Popen(
        [sys.executable, "-m", "guildboard", "--shell", "--demo", "--data-dir", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def _next_line(core, deadline: float) -> str:
    """The core's next line, or a failed test at `deadline` (a time.monotonic() value): a core that says nothing must not
    hang the suite. The thread of a line that never came ends with the process, which the test's `finally` kills."""
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: lines.put(core.stdout.readline()), daemon=True).start()
    try:
        return lines.get(timeout=max(0.1, deadline - time.monotonic()))
    except queue.Empty:
        raise AssertionError("the core said nothing in time") from None


def _read_until(core, wanted, timeout: float = 60) -> dict:
    """Read the core's lines until one satisfies `wanted`. Every line skipped is an event: a demo core reads the real
    process list, and a `game` event may come first. An answer is never skipped."""
    return _events_until(core, wanted, timeout)[-1]


def _events_until(core, wanted, timeout: float = 60) -> list[dict]:
    """Every message the core wrote up to and including the first that satisfies `wanted`."""
    deadline = time.monotonic() + timeout
    seen: list[dict] = []
    while line := _next_line(core, deadline):
        message = json.loads(line)
        seen.append(message)
        if wanted(message):
            return seen
        assert "event" in message and "id" not in message, message
    raise AssertionError("the core's output ended")


def _event(name):
    return lambda message: message.get("event") == name


def _answer(ident):
    return lambda message: message.get("id") == ident


def _data_log(tmp_path) -> str:
    return (tmp_path / "logs" / "guildboard.log").read_text(encoding="utf-8")


def test_ready_sends_the_settings_then_the_link(channel, lines):
    channel.ready("http://127.0.0.1:5/launch?k=x")
    assert [json.loads(line)["event"] for line in lines] == ["settings", "ready"]
    assert "overlay_hotkey" in json.loads(lines[0])["settings"]
    assert json.loads(lines[1])["link"] == "http://127.0.0.1:5/launch?k=x"


def test_a_window_request_is_an_event_and_makes_a_shell(channel, core, lines):
    assert core.request_window("login") is True
    assert json.loads(lines[-1]) == {"event": "window", "kind": "login"}
    assert core.status()["has_shell"] is True


async def test_a_notification_needs_a_tray_and_carries_its_place(channel, core, lines):
    assert core.notify("codes", "A code") is False  # no hello yet
    await channel.answer('{"id": 1, "call": "hello", "args": {"has_tray": true}}')
    assert json.loads(lines[-1]) == {"id": 1, "result": None}
    assert core.notify("codes", "A code") is True
    assert json.loads(lines[-1]) == {"event": "notify", "kind": "codes", "text": "A code", "place": "codes"}
    assert core.notify("test", "Hello") is True
    assert json.loads(lines[-1])["place"] is None
    assert core.notify("characters/hu-tao", "Ready") is True
    assert json.loads(lines[-1])["place"] == "characters/hu-tao"
    await channel.answer('{"id": 2, "call": "hello", "args": {"has_tray": false}}')
    assert core.notify("codes", "A code") is False


async def test_a_settings_change_is_an_event_and_the_call_answers_with_the_settings(channel, lines):
    await channel.answer('{"id": 3, "call": "update_settings", "args": {"changes": {"overlay_pinned": true}}}')
    event, answer = (json.loads(line) for line in lines[-2:])
    assert event["event"] == "settings" and event["settings"]["overlay_pinned"] is True
    assert answer["id"] == 3 and answer["result"]["overlay_pinned"] is True


async def test_the_game_and_the_map_are_events(channel, core, lines):
    core.game_listeners[0](True)
    assert json.loads(lines[-1]) == {"event": "game", "running": True}
    core.map_changed()
    event = json.loads(lines[-1])
    assert event["event"] == "map" and set(event["snapshot"]) == {"tabs", "active", "viewport", "session"}
    await channel.answer('{"id": 4, "call": "game_running", "args": {}}')
    assert json.loads(lines[-1]) == {"id": 4, "result": False}


async def test_calls_answer_by_id_and_unknown_calls_say_so(channel, lines):
    await channel.answer('{"id": 7, "call": "map_snapshot", "args": {}}')
    answer = json.loads(lines[-1])
    assert answer["id"] == 7 and set(answer["result"]) == {"tabs", "active", "viewport", "session"}
    await channel.answer('{"id": 8, "call": "nope", "args": {}}')
    assert json.loads(lines[-1]) == {"id": 8, "error": "unknown call nope"}


async def test_a_call_that_fails_answers_with_its_text(channel, lines):
    await channel.answer('{"id": 5, "call": "set_paused", "args": {}}')
    assert json.loads(lines[-1]) == {"id": 5, "error": "The call needs paused."}
    await channel.answer('{"id": 6, "call": "map_snapshot", "args": 3}')
    assert json.loads(lines[-1]) == {"id": 6, "error": "The call's arguments are not an object."}


async def test_a_line_with_no_id_is_ignored_and_its_content_is_not_logged(channel, lines, caplog):
    caplog.set_level(logging.WARNING)
    await channel.answer('{"call": "hello", "args": {"has_tray": true}, "extra": "made-up-content"}')
    await channel.answer("not json made-up-content")
    assert lines == []
    assert len(caplog.records) == 2 and "made-up-content" not in caplog.text


async def test_a_login_is_answered_and_its_cookies_are_logged_nowhere(channel, lines, caplog):
    caplog.set_level(logging.INFO)
    await channel.answer('{"id": 1, "call": "login", "args": {"cookies": {"ltoken_v2": "made-up-ltoken"}, "uid": null}}')
    assert json.loads(lines[-1])["result"] == {
        "status": "missing_cookies", "account": None, "accounts": [], "known_uid": None,
    }
    assert "made-up-ltoken" not in caplog.text
    assert sum("made-up-ltoken" in line for line in lines) == 0


async def test_a_login_text_is_read_as_cookies(channel, lines, caplog):
    await channel.answer('{"id": 1, "call": "login_text", "args": {"text": "Cookie: ltoken_v2=made-up-ltoken", "uid": null}}')
    assert json.loads(lines[-1])["result"]["status"] == "missing_cookies"
    assert "made-up-ltoken" not in caplog.text


async def test_a_good_login_answers_with_the_account(channel, lines):
    cookies = {"ltoken_v2": "made-up-ltoken", "ltuid_v2": "1234567"}
    await channel.answer(json.dumps({"id": 1, "call": "login", "args": {"cookies": cookies, "uid": None}}))
    result = json.loads(lines[-1])["result"]
    assert result["status"] == "ok"
    assert result["account"] == {"uid": 700000001, "nickname": "Traveler", "server": "os_euro", "level": 60}
    await channel.answer('{"id": 2, "call": "verify_cookies", "args": {}}')
    assert json.loads(lines[-1])["result"] == cookies  # the stored login, for the verify window alone


class _Spy:
    """Stands in for the core's account lister: remembers the cookies it was given."""

    def __init__(self, error=None):
        self.cookies, self.error = None, error

    async def __call__(self, cookies):
        self.cookies = dict(cookies)
        if self.error is not None:
            raise self.error
        return [GameAccount(700000001, "Traveler", "os_euro", 60)]


_EXTRA = {"_ga": "made-up-analytics", "mi18nLang": "en-us", "HYV_LOGIN_PLATFORM_TOKEN": "made-up-platform"}


async def test_a_login_keeps_only_the_cookies_a_login_is_made_of(core, channel, lines, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(core.auth, "_list_accounts", spy)
    cookies = {"ltoken_v2": "made-up-ltoken", "ltuid_v2": "1234567", "ltoken": "made-up-old", **_EXTRA}
    await channel.answer(json.dumps({"id": 1, "call": "login", "args": {"cookies": cookies, "uid": None}}))
    assert json.loads(lines[-1])["result"]["status"] == "ok"
    assert spy.cookies == {"ltoken_v2": "made-up-ltoken", "ltuid_v2": "1234567", "ltoken": "made-up-old"}
    await channel.answer('{"id": 2, "call": "verify_cookies", "args": {}}')
    assert json.loads(lines[-1])["result"] == spy.cookies  # what token.bin holds


async def test_a_pasted_text_keeps_only_the_cookies_a_login_is_made_of(core, channel, lines, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(core.auth, "_list_accounts", spy)
    text = "Cookie: ltoken_v2=made-up-ltoken; _ga=made-up-analytics; ltuid_v2=1234567; mi18nLang=en-us; cookie_token_v2=made-up-ct"
    await channel.answer(json.dumps({"id": 1, "call": "login_text", "args": {"text": text, "uid": None}}))
    assert json.loads(lines[-1])["result"]["status"] == "ok"
    assert spy.cookies == {"ltoken_v2": "made-up-ltoken", "ltuid_v2": "1234567", "cookie_token_v2": "made-up-ct"}


@pytest.mark.parametrize("call, args", [
    ("login", {"cookies": {"ltoken_v2": "made-up-ltoken", "ltuid_v2": "1234567"}, "uid": None}),
    ("login_text", {"text": "ltoken_v2=made-up-ltoken; ltuid_v2=1234567", "uid": None}),
])
async def test_an_unreachable_hoyolab_is_an_offline_outcome(core, channel, lines, monkeypatch, call, args):
    monkeypatch.setattr(core.auth, "_list_accounts", _Spy(Offline("HoYoLAB could not be reached.")))
    await channel.answer(json.dumps({"id": 1, "call": call, "args": args}))
    assert json.loads(lines[-1]) == {
        "id": 1, "result": {"status": "offline", "account": None, "accounts": [], "known_uid": None},
    }


async def test_a_backup_and_a_picture_answer_with_their_texts(channel, lines, tmp_path):
    await channel.answer('{"id": 1, "call": "backup_file_name", "args": {}}')
    assert json.loads(lines[-1])["result"].startswith("TUSKFiZZY backup 2026-10-")
    await channel.answer('{"id": 2, "call": "picture_offer", "args": {}}')
    assert json.loads(lines[-1]) == {"id": 2, "result": None}
    await channel.answer(json.dumps({"id": 3, "call": "save_picture", "args": {"path": str(tmp_path / "a.png"), "number": 1}}))
    assert json.loads(lines[-1]) == {"id": 3, "result": {"refused": NO_PICTURE}}
    await channel.answer('{"id": 4, "call": "drop_picture", "args": {"number": null}}')
    assert json.loads(lines[-1]) == {"id": 4, "result": None}
    await channel.answer(json.dumps({"id": 5, "call": "export_backup", "args": {"path": str(tmp_path / "b.zip")}}))
    assert json.loads(lines[-1])["result"] == {"written": "b.zip"}


async def test_a_refused_save_answers_with_its_sentence_and_any_other_failure_is_an_error(channel, lines, tmp_path, caplog):
    # BackupRefused is what the player reads (ruling R28); any other exception stays an error answer.
    caplog.set_level(logging.INFO, logger="guildboard.shellchannel")
    await channel.answer(json.dumps({"id": 1, "call": "save_picture", "args": {"path": str(tmp_path / "a.png"), "number": 1}}))
    assert json.loads(lines[-1]) == {"id": 1, "result": {"refused": NO_PICTURE}}
    # The refusal is in the log, as Qt logged it (app.py:292).
    assert any(r.name == "guildboard.shellchannel" and NO_PICTURE in r.getMessage() for r in caplog.records)
    await channel.answer(json.dumps({"id": 2, "call": "export_backup", "args": {"path": 5}}))
    assert json.loads(lines[-1]) == {"id": 2, "error": "The call needs path."}
    # The file cannot be made: an OS error, with a path in its text, is an error and not a sentence.
    blocker = tmp_path / "a file"
    blocker.write_text("x", encoding="utf-8")
    await channel.answer(json.dumps({"id": 3, "call": "export_backup", "args": {"path": str(blocker / "b.zip")}}))
    answer = json.loads(lines[-1])
    assert "error" in answer and "result" not in answer


async def test_the_overlay_report_is_taken(channel, core, lines):
    displays = [{"name": "A", "label": "Display 1", "primary": True}]
    await channel.answer(json.dumps({"id": 1, "call": "overlay_shell", "args": {"displays": displays, "hotkey_state": "held"}}))
    assert json.loads(lines[-1]) == {"id": 1, "result": None}
    assert core.overlay_status()["hotkey_state"] == "held"
    await channel.answer(json.dumps({"id": 2, "call": "overlay_shell", "args": {"displays": [], "hotkey_state": "bad"}}))
    assert "error" in json.loads(lines[-1])


async def test_whether_the_window_is_maximized_is_taken_and_the_pages_hear_of_it(channel, core, lines):
    assert core.status()["window_maximized"] is False  # until the shell says
    heard = core.bus.subscribe()
    await channel.answer(json.dumps({"id": 1, "call": "window_state", "args": {"maximized": True}}))
    assert json.loads(lines[-1]) == {"id": 1, "result": None}
    assert core.status()["window_maximized"] is True
    assert [heard.get_nowait()["type"] for _ in range(heard.qsize())] == ["window_changed"]
    await channel.answer(json.dumps({"id": 2, "call": "window_state", "args": {"maximized": "yes"}}))
    assert "error" in json.loads(lines[-1])
    assert core.status()["window_maximized"] is True


async def test_a_shell_log_line_goes_to_the_core_log(channel, lines, caplog):
    await channel.answer('{"id": 2, "call": "log", "args": {"level": "warning", "text": "A refused switch."}}')
    assert json.loads(lines[-1]) == {"id": 2, "result": None}
    assert any(r.name == "guildboard.shell" and r.getMessage() == "A refused switch." for r in caplog.records)


def test_the_calls_are_the_channels_table():
    assert set(CALLS) == {
        "hello", "login", "login_text", "sync_now", "set_paused", "update_settings", "backup_file_name",
        "export_backup", "picture_offer", "save_picture", "drop_picture", "verify_cookies", "verification_done",
        "map_snapshot", "map_session", "map_role", "map_seen", "map_failed", "game_running", "overlay_shell",
        "offer_invite", "log", "shutdown", "window_state",
    }


def test_shell_and_no_shell_do_not_go_together(capsys):
    assert main(["--shell", "--no-shell"]) == 2
    assert "cannot be used together" in capsys.readouterr().out


def _seed_a_due_quick_timer(data, *, demo: bool) -> None:
    """A data folder that holds a quick timer ready an hour ago and not yet reminded about, as a closed Guild Board leaves it."""
    seeded = Core(AppPaths(data), demo=demo, process_lister=lambda: set())
    try:
        now = seeded.clock.now().replace(microsecond=0)
        seeded.store.add_quick_timer(60, now - timedelta(hours=2), now - timedelta(hours=1))
    finally:
        seeded.store.close()


def _start_in(data, *extra):
    return subprocess.Popen(
        [sys.executable, "-m", "guildboard", "--shell", "--data-dir", str(data), *extra],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )


def test_a_timer_due_at_start_is_announced_and_a_demo_announces_none(tmp_path):  # real processes
    # `hello` comes after the start, so the core must already count on a tray when its first look at the timers is made:
    # the Qt shell had its tray first. Without it the reminder was marked as sent and nothing was shown.
    _seed_a_due_quick_timer(tmp_path / "real", demo=False)
    _seed_a_due_quick_timer(tmp_path / "demo", demo=True)
    real = _start_in(tmp_path / "real")
    demo = _start_in(tmp_path / "demo", "--demo")
    try:
        # The first look at the timers is made just after `ready`, as the core's tasks start: read on to the notification.
        _events_until(real, _event("ready"))
        notice = _read_until(real, _event("notify"))
        assert notice["kind"] == "quick"
        assert notice["text"].endswith("1 min is ready.")
        # The demo's look comes before an answer to a call made after `ready`: a notification would be among these lines.
        _events_until(demo, _event("ready"))
        demo.stdin.write('{"id": 1, "call": "backup_file_name", "args": {}}\n')
        demo.stdin.flush()
        seen = _events_until(demo, _answer(1))
        assert [event for event in seen if event.get("event") == "notify"] == []  # a demo has no tray
    finally:
        for core in (real, demo):
            core.kill()
            core.wait(timeout=20)
            core.stdout.close()
            core.stdin.close()


def test_the_core_stops_when_its_input_ends(tmp_path):  # a real process
    core = _start(tmp_path)
    try:
        _read_until(core, _event("settings"))
        _read_until(core, _event("ready"))
        core.stdin.close()
        assert core.wait(timeout=20) == 0
    finally:
        core.kill()


def test_shutdown_answers_then_finishes(tmp_path):  # a real process
    core = _start(tmp_path)
    try:
        _read_until(core, _event("settings"))
        _read_until(core, _event("ready"))
        core.stdin.write('{"id": 1, "call": "shutdown", "args": {}}\n')
        core.stdin.flush()
        assert _read_until(core, _answer(1)) == {"id": 1, "result": None}
        _read_until(core, _event("finished"))
        assert core.wait(timeout=20) == 0
        assert core.stdout.read() == ""  # nothing was written after `finished`
    finally:
        core.kill()


def test_a_shell_that_is_gone_is_a_clean_stop(tmp_path):  # a real process
    core = _start(tmp_path)
    try:
        _read_until(core, _event("settings"))
        _read_until(core, _event("ready"))
        core.stdout.close()  # the reader of the core's output goes away, and so does its input
        core.stdin.close()
        assert core.wait(timeout=20) == 0
    finally:
        core.kill()
    assert "stopped on an error" not in _data_log(tmp_path)
    assert "Traceback" not in _data_log(tmp_path)


async def test_a_call_with_a_null_where_a_string_is_needed_is_refused(channel, lines):
    await channel.answer('{"id": 1, "call": "offer_invite", "args": {"code": null}}')
    assert json.loads(lines[-1]) == {"id": 1, "error": "The call needs code."}
    await channel.answer('{"id": 2, "call": "log", "args": {"level": "info", "text": null}}')
    assert json.loads(lines[-1]) == {"id": 2, "error": "The call needs text."}
