import asyncio
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from guildboard.__main__ import NO_SHELL_TEXT, default_web_dir, main, run_headless
from guildboard.config import AppPaths
from guildboard.logging_setup import setup_logging, shutdown_logging


async def _http(port: int, path: str, cookie: str | None = None) -> tuple[int, dict[str, str], str]:
    for _ in range(100):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        raise AssertionError("the server did not start listening")
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Connection: close"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    raw = (await reader.read()).decode("utf-8", "replace")
    writer.close()
    head, _, body = raw.partition("\r\n\r\n")
    headers = {}
    for line in head.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(head.split()[1]), headers, body


def test_a_start_with_neither_switch_says_where_the_window_is(tmp_path, monkeypatch, capsys):
    # The window is the Electron shell's. The core starts only for it (--shell) or alone (--no-shell); with neither, nothing
    # starts, not even the log or the data folder.
    monkeypatch.setattr("guildboard.__main__.setup_logging", lambda folder: pytest.fail("the log was opened"))
    monkeypatch.setattr("guildboard.__main__.run_headless", lambda *args, **kwargs: pytest.fail("the core started"))
    assert main(["--demo", "--data-dir", str(tmp_path / "data")]) == 2
    printed = capsys.readouterr().out
    assert printed.strip() == (
        "TUSKFiZZY's window is its shell. Start it with tuskfizzy-app.exe, or with npm start --prefix shell in a checkout."
    )
    assert printed.strip() == NO_SHELL_TEXT
    assert not (tmp_path / "data").exists()
    assert main([]) == 2
    assert main(["guildboard://invite/abcDEF0123456789_-abcdef"]) == 2  # a link alone has no shell to answer it


def test_the_installers_command_hands_the_link_over_to_the_core_alone(tmp_path, monkeypatch):
    seen: dict = {}

    async def run_headless(paths, **options):  # noqa: ANN001, ANN003, ANN202
        seen.update(options)
        return 0

    monkeypatch.setattr("guildboard.__main__.run_headless", run_headless)
    monkeypatch.setattr("guildboard.__main__.setup_logging", lambda folder: None)
    code = "abcDEF0123456789_-abcdef"
    assert main(["--no-shell", "--data-dir", str(tmp_path / "data"), "--", f"guildboard://invite/{code}"]) == 0
    assert seen["invite"] == code


def test_a_link_brings_no_options_of_its_own(tmp_path, monkeypatch):
    # A quotation mark in a link would end the argument Windows quoted for it. What follows it comes after the installer's
    # `--`, where it can't be an option, and Guild Board doesn't start.
    monkeypatch.setattr("guildboard.__main__.run_headless", lambda *args, **kwargs: pytest.fail("TUSKFiZZY started"))
    with pytest.raises(SystemExit):
        main(["--no-shell", "--", "guildboard://invite/abcDEF0123456789_-abcdef", "--data-dir", str(tmp_path / "data")])
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("link", ["guildboard://invite/../x", "https://evil.example/", "guildboard://invite/short"])
def test_anything_else_in_its_place_is_ignored_and_guild_board_starts(tmp_path, monkeypatch, link):
    seen: dict = {}

    async def run_headless(paths, **options):  # noqa: ANN001, ANN003, ANN202
        seen.update(options)
        return 0

    monkeypatch.setattr("guildboard.__main__.run_headless", run_headless)
    monkeypatch.setattr("guildboard.__main__.setup_logging", lambda folder: None)
    assert main(["--no-shell", link, "--data-dir", str(tmp_path / "data")]) == 0
    assert seen["invite"] is None


def test_the_core_alone_takes_a_link_too(tmp_path, monkeypatch):
    seen: dict = {}

    async def run_headless(paths, **options):  # noqa: ANN001, ANN003, ANN202
        seen.update(options)
        return 0

    monkeypatch.setattr("guildboard.__main__.run_headless", run_headless)
    monkeypatch.setattr("guildboard.__main__.setup_logging", lambda folder: None)
    code = "abcDEF0123456789_-abcdef"
    assert main([f"guildboard://invite/{code}", "--no-shell", "--data-dir", str(tmp_path / "data")]) == 0
    assert seen["invite"] == code


def test_the_window_options_moved_to_the_shell(tmp_path):
    # --hidden, --quit and --acknowledged are the shell's (shell/src/args.ts); the core knows none of them.
    for option in ("--hidden", "--quit", "--acknowledged"):
        with pytest.raises(SystemExit):
            main(["--demo", "--no-shell", option, "--data-dir", str(tmp_path / "data")])
    assert not (tmp_path / "data").exists()


def test_an_error_nothing_expected_at_the_start_reaches_the_log_file(tmp_path, monkeypatch):
    async def broken(paths, **options):  # noqa: ANN001, ANN003, ANN202
        raise RuntimeError("the start failed in a way nobody expected")

    monkeypatch.setattr("guildboard.__main__.run_headless", broken)
    data = tmp_path / "data"
    with pytest.raises(RuntimeError):
        main(["--demo", "--no-shell", "--data-dir", str(data)])
    # The log closes as main() returns, before Python reports the error: so main() writes it first.
    written = (AppPaths(data).logs_dir / "guildboard.log").read_text(encoding="utf-8")
    assert "the start failed in a way nobody expected" in written
    assert "Traceback" in written


def test_default_web_dir_is_in_the_project():
    assert default_web_dir().parts[-2:] == ("web", "dist")
    assert (default_web_dir().parents[1] / "pyproject.toml").is_file()


def test_the_installed_copy_finds_the_interface_in_its_own_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert default_web_dir() == tmp_path / "web" / "dist"


def test_unreadable_database_exits_with_a_message(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    garbage = b"this is not a database" * 50
    (data / "guildboard.db").write_bytes(garbage)
    assert main(["--demo", "--no-shell", "--data-dir", str(data)]) == 1
    printed = capsys.readouterr().out
    assert "cannot be read" in printed
    assert "left untouched" in printed
    assert (data / "guildboard.db").read_bytes() == garbage


async def test_headless_demo_serves_the_api(tmp_path):
    urls: list[str] = []
    stop = asyncio.Event()
    task = asyncio.create_task(run_headless(AppPaths(tmp_path / "data"), demo=True, ready=urls.append, stop=stop))
    for _ in range(500):
        if urls or task.done():
            break
        await asyncio.sleep(0.02)
    assert urls, "the core did not report its launch link"
    link = urlsplit(urls[0])
    assert link.hostname == "127.0.0.1"
    status, headers, _ = await _http(link.port, f"{link.path}?{link.query}")
    assert status == 303
    cookie = headers["set-cookie"].split(";", 1)[0]
    status, _, body = await _http(link.port, "/api/status", cookie)
    assert status == 200
    assert json.loads(body)["mode"] == "demo"
    status, _, _ = await _http(link.port, "/api/status")
    assert status == 401
    stop.set()
    assert await asyncio.wait_for(task, timeout=30) == 0


async def _start(paths: AppPaths, stop: asyncio.Event):
    """Start the core alone, in live mode without a login: it starts at once and contacts nothing."""
    urls: list[str] = []
    task = asyncio.create_task(run_headless(paths, demo=False, ready=urls.append, stop=stop))
    for _ in range(500):
        if urls or task.done():
            break
        await asyncio.sleep(0.02)
    assert urls, "the core did not report its launch link"
    return task, urlsplit(urls[0])


async def test_stop_works_while_an_event_stream_is_open(tmp_path):
    stop = asyncio.Event()
    task, link = await _start(AppPaths(tmp_path / "data"), stop)
    _, headers, _ = await _http(link.port, f"{link.path}?{link.query}")
    cookie = headers["set-cookie"].split(";", 1)[0]
    reader, writer = await asyncio.open_connection("127.0.0.1", link.port)
    writer.write(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{link.port}\r\nCookie: {cookie}\r\n\r\n".encode())
    await writer.drain()
    opening = await asyncio.wait_for(reader.readuntil(b"event: hello"), timeout=10)
    assert b" 200 " in opening
    stop.set()
    try:
        # A page that is still open must not keep Guild Board from closing.
        assert await asyncio.wait_for(asyncio.shield(task), timeout=10) == 0
    finally:
        writer.close()
        await asyncio.wait_for(task, timeout=30)


async def test_server_errors_reach_the_log_file(tmp_path):
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    setup_logging(paths.logs_dir)
    stop = asyncio.Event()
    try:
        task, _ = await _start(paths, stop)
        logging.getLogger("uvicorn.error").error("something failed inside the server")
        stop.set()
        assert await asyncio.wait_for(task, timeout=30) == 0
    finally:
        shutdown_logging()
    assert "something failed inside the server" in (paths.logs_dir / "guildboard.log").read_text(encoding="utf-8")


def test_the_launch_link_reaches_a_pipe_at_once(tmp_path):
    env = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    process = subprocess.Popen(
        [sys.executable, "-m", "guildboard", "--demo", "--no-shell", "--data-dir", str(tmp_path / "data")],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
    )
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        try:
            line = lines.get(timeout=30)
        except queue.Empty:
            line = ""
        # A program that starts Guild Board and reads its output must get the link without waiting for the exit.
        assert line.startswith("http://127.0.0.1:")
    finally:
        process.kill()
        process.wait(timeout=20)
        reader.join(timeout=20)  # it has read its line, or the pipe has ended with the process
        process.stdout.close()


def test_demo_now_is_checked(capsys):
    assert main(["--no-shell", "--demo-now", "2026-10-13T08:00:00+00:00"]) == 2
    assert "--demo-now needs --demo" in capsys.readouterr().out
    assert main(["--demo", "--no-shell", "--demo-now", "yesterday"]) == 2
    assert "time zone" in capsys.readouterr().out
    assert main(["--demo", "--no-shell", "--demo-now", "2026-10-13T08:00:00"]) == 2
    assert "time zone" in capsys.readouterr().out


def test_week_and_endgame_can_be_turned_on_for_a_demo_only(capsys, monkeypatch):
    from guildboard import characters

    monkeypatch.setattr(characters, "WEEK_AND_ENDGAME", False)
    assert main(["--no-shell", "--week-and-endgame"]) == 2
    assert "--week-and-endgame needs --demo" in capsys.readouterr().out
    assert characters.WEEK_AND_ENDGAME is False
    monkeypatch.setattr("guildboard.__main__.run_headless", lambda *args, **kwargs: asyncio.sleep(0, 0))
    assert main(["--demo", "--no-shell", "--week-and-endgame"]) == 0
    assert characters.WEEK_AND_ENDGAME is True


async def test_headless_demo_starts_at_the_given_instant(tmp_path):
    urls: list[str] = []
    stop = asyncio.Event()
    start = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    task = asyncio.create_task(
        run_headless(AppPaths(tmp_path / "data"), demo=True, demo_now=start, ready=urls.append, stop=stop)
    )
    for _ in range(500):
        if urls or task.done():
            break
        await asyncio.sleep(0.02)
    assert urls, "the core did not report its launch link"
    link = urlsplit(urls[0])
    _, headers, _ = await _http(link.port, f"{link.path}?{link.query}")
    _, _, body = await _http(link.port, "/api/status", headers["set-cookie"].split(";", 1)[0])
    assert json.loads(body)["now"].startswith("2026-10-13T08:00")
    stop.set()
    assert await asyncio.wait_for(task, timeout=30) == 0


async def test_the_core_accepts_connections_when_it_announces_its_link(tmp_path):
    connected: list[int] = []

    def ready(link: str) -> None:
        port = urlsplit(link).port
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            connected.append(port)

    stop = asyncio.Event()
    task = asyncio.create_task(run_headless(AppPaths(tmp_path / "data"), demo=False, ready=ready, stop=stop))
    for _ in range(500):
        if connected or task.done():
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)
    assert connected, "a program that reads the link must be able to connect at once"


async def test_the_real_account_is_kept_out_of_the_demo_folder(tmp_path):
    from guildboard.core import Core
    from guildboard.store import StoreError

    Core(AppPaths(tmp_path / "data"), demo=True, process_lister=lambda: set()).store.close()
    stop = asyncio.Event()
    stop.set()  # without the check, the core would start and stop at once
    with pytest.raises(StoreError) as error:
        await run_headless(AppPaths(tmp_path / "data"), demo=False, ready=lambda link: None, stop=stop)
    assert "holds the demo's made-up data" in str(error.value)
