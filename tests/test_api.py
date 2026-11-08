import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from guildboard.api.app import SESSION_COOKIE, create_app, sse_stream
from guildboard.config import AppPaths, load_settings
from guildboard.core import Core
from guildboard.events import EventBus
from guildboard.metrics import Label
from support import AccountSource, FakeSleep

PORT = 4321
BASE = f"http://127.0.0.1:{PORT}"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24  # the first bytes of a picture, which is what the icon cache keeps


def _core(tmp_path, clock, demo: bool = True) -> Core:
    return Core(
        AppPaths(tmp_path / "data"), demo=demo, clock=clock, source=None if demo else AccountSource(),
        sleep=FakeSleep(clock), process_lister=lambda: set(),
    )


@pytest.fixture
def core(tmp_path, clock):
    core = _core(tmp_path, clock)
    yield core
    core.store.close()


@pytest.fixture
def client(core):
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        response = client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert response.status_code == 303
        yield client


def _wait_until_idle(client) -> dict:
    for _ in range(200):
        status = client.get("/api/status").json()
        if status["sync"]["state"] != "syncing":
            return status
        time.sleep(0.01)
    raise AssertionError("the sync did not finish")


def test_api_needs_the_session_cookie(core):
    with TestClient(create_app(core, PORT), base_url=BASE) as anonymous:
        response = anonymous.get("/api/status")
        assert response.status_code == 401
        assert response.json() == {"error": "Open Guild Board from its own window or launch link."}
        assert anonymous.get("/").status_code == 200  # the page itself is not a secret


def test_the_launch_link_can_open_the_timers_category(core):
    # A click on a reminder opens Timers, or the Board for quick timers alone. The link names these places only:
    # anything else opens the page as before.
    expected = {
        "achievements/timers": "/#/achievements/timers", "board": "/#/board", "https://example.com/": "/",
        "settings": "/", "week": "/#/week", "characters/sandrone": "/#/characters/sandrone",
        "characters/lan-yan": "/#/characters/lan-yan", "characters/../x": "/", "characters/Sandrone": "/",
    }
    for place, location in expected.items():
        with TestClient(create_app(core, PORT), base_url=BASE) as fresh:
            response = fresh.get("/launch", params={"k": core.launch_secret, "to": place}, follow_redirects=False)
            assert (response.status_code, response.headers["location"]) == (303, location), place


def test_the_launch_link_can_open_gift_codes(core):
    response = TestClient(create_app(core, PORT), base_url=BASE).get(
        "/launch", params={"k": core.launch_secret, "to": "codes"}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/#/codes")


def test_wrong_launch_secret_is_refused(core):
    with TestClient(create_app(core, PORT), base_url=BASE) as anonymous:
        response = anonymous.get("/launch", params={"k": "guess"}, follow_redirects=False)
        assert response.status_code == 403
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
        assert anonymous.get("/api/status").status_code == 401


def test_other_hosts_are_refused(core):
    app = create_app(core, PORT)
    with TestClient(app, base_url="http://evil.example") as outsider:
        assert outsider.get("/api/status").status_code == 400
        assert outsider.get("/").status_code == 400
        assert outsider.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 400
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT + 1}") as wrong_port:
        assert wrong_port.get("/").status_code == 400
    with TestClient(app, base_url=f"http://localhost:{PORT}") as local:
        assert local.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 303


def test_launch_sets_a_strict_http_only_cookie(core):
    with TestClient(create_app(core, PORT), base_url=BASE) as fresh:
        response = fresh.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        cookie = response.headers["set-cookie"].lower()
        assert response.headers["location"] == "/"
        assert cookie.startswith(f"{SESSION_COOKIE}=")
        assert "httponly" in cookie
        assert "samesite=strict" in cookie


def test_status(client):
    status = client.get("/api/status").json()
    assert status["mode"] == "demo"
    assert status["sync"]["state"] == "ok"
    assert status["account"]["nickname"] == "Traveler"
    assert status["game"]["running"] is False
    assert status["tracking_since"] == "2026-03-02"


def test_backlog_and_its_sorts(client):
    least = client.get("/api/backlog").json()
    assert least["sort"] == "least"
    assert least["regions"][0]["name"] == "Snezhnaya"
    assert client.get("/api/backlog", params={"sort": "closest"}).json()["sort"] == "closest"
    response = client.get("/api/backlog", params={"sort": "alphabetical"})
    assert response.status_code == 422
    assert response.json() == {"error": "Unknown sort. Use least, closest or game."}


def test_stats_and_its_ranges(client):
    for key, days in (("7", 7), ("30", 30), ("90", 90), ("all", 226)):
        model = client.get("/api/stats", params={"range": key}).json()
        assert (model["range"], model["period"]["days"]) == (key, days)
    assert client.get("/api/stats").json()["range"] == "30"
    response = client.get("/api/stats", params={"range": "365"})
    assert response.status_code == 422
    assert response.json() == {"error": "Unknown range. Use 7, 30, 90 or all."}


def test_the_endgame_card_has_its_own_model(client):
    # The Stats design, 3.1: the Overview's Endgame card, a column for each mode.
    client.post("/api/sync")
    _wait_until_idle(client)
    model = client.get("/api/stats/endgame").json()
    assert [column["mode"] for column in model["modes"]] == ["abyss", "theater", "stygian"]
    abyss, theater, stygian = model["modes"]
    assert abyss["played"] and abyss["floor_shown"] == 12 and len(abyss["chambers"]) == 12
    assert theater["played"] and theater["medals"] and stygian["played"] and len(stygian["bosses"]) == 3
    # The demo has no pictures: every header is plain, with no icon (the Stats design, section 7).
    assert all(column["header"] == {"icon": None, "picture": None, "crop": None} for column in model["modes"])


def test_manual_sync_runs_and_then_cools_down(client):
    first = client.post("/api/sync").json()
    assert first == {"started": True, "reason": "ok", "wait_seconds": 0}
    status = _wait_until_idle(client)
    assert status["sync"]["state"] == "ok"
    newest = client.get("/api/synclog", params={"limit": 1}).json()[0]
    assert (newest["trigger"], newest["outcome"]) == ("manual", "ok")
    second = client.post("/api/sync").json()
    assert (second["started"], second["reason"]) == (False, "cooldown")
    assert 0 < second["wait_seconds"] <= 300
    assert client.get("/api/status").json()["sync"]["manual_wait_seconds"] == second["wait_seconds"]


def test_sync_log_rows(client):
    rows = client.get("/api/synclog").json()
    assert len(rows) == 50
    assert set(rows[0]) == {"id", "started_at", "finished_at", "trigger", "outcome", "error_kind", "message"}
    assert rows[0]["id"] > rows[1]["id"]
    assert len(client.get("/api/synclog", params={"limit": 3}).json()) == 3
    assert len(client.get("/api/synclog", params={"limit": 100000}).json()) == 211


def test_pause_and_resume(client, core):
    assert client.post("/api/sync/pause").json()["sync"]["state"] == "paused"
    assert load_settings(core.paths.settings_path).sync_paused is True
    assert client.post("/api/sync").json() == {"started": False, "reason": "paused", "wait_seconds": 0}
    assert client.post("/api/sync/resume").json()["sync"]["state"] == "ok"


def test_settings_round_trip(client):
    assert client.get("/api/settings").json()["in_session_interval_min"] == 0
    changed = client.put(
        "/api/settings", json={"in_session_interval_min": 30, "sync_paused": True, "made_up": 1}
    ).json()
    assert changed["in_session_interval_min"] == 30
    assert changed["sync_paused"] is False
    assert "made_up" not in changed
    assert client.get("/api/settings").json() == changed
    assert client.put("/api/settings", json=["not", "an", "object"]).status_code == 422


@pytest.mark.parametrize("method, path, content, content_type", [
    ("PUT", "/api/settings", b'["\\ud83d"]', "application/json"),
    ("PUT", "/api/settings", b"\xff", "text/plain"),
    ("POST", "/api/demo/fault", b'"\\ud83d"', "application/json"),
    ("POST", "/api/demo/clock", b"\xff", "application/json"),
    ("GET", "/api/synclog?limit=abc", None, "application/json"),
], ids=[
    "settings with half of an emoji in a list", "settings that are not UTF-8", "a fault that is half of an emoji",
    "a clock step that is not UTF-8", "a log limit that is not a number",
])
def test_a_request_fastapi_refuses_gets_a_sentence_and_not_what_it_sent(client, method, path, content, content_type):
    # FastAPI's own answer repeats the request, which fails on half of an emoji and on bytes that are not UTF-8.
    response = client.request(method, path, content=content, headers={"Content-Type": content_type})
    assert response.status_code == 422
    assert response.json() == {"error": "This request does not have the expected shape."}
    assert client.get("/api/settings").json()["in_session_interval_min"] == 0


TOO_LARGE = "This request is too large."


def test_oversize_body_is_refused_with_413(client, core, monkeypatch):
    # Every body is bounded before a route reads any of it: by the size it says it has, else by counting what arrives,
    # so that one sent in pieces with no size is cut off too. Only a backup file to restore and an achievement file to
    # import are large, and each has its own bound and sentence.
    import guildboard.api.app as app_module

    previewed: list[tuple] = []

    async def preview(*named):
        previewed.append(named)
        return {}

    monkeypatch.setattr(core, "preview_import", preview)
    big = json.dumps({"in_session_interval_min": 30, "padding": "y" * app_module.MAX_BODY_BYTES}).encode()
    answer = client.put("/api/settings", content=big, headers={"Content-Type": "application/json"})
    assert (answer.status_code, answer.json()) == (413, {"error": TOO_LARGE})

    def pieces():
        yield b'{"in_session_interval_min": 30, "padding": "'
        for _ in range(app_module.MAX_BODY_BYTES // 100_000 + 1):
            yield b"y" * 100_000
        yield b'"}'

    answer = client.put("/api/settings", content=pieces(), headers={"Content-Type": "application/json"})
    assert "content-length" not in answer.request.headers
    assert (answer.status_code, answer.json()) == (413, {"error": TOO_LARGE})
    assert client.get("/api/settings").json()["in_session_interval_min"] == 0  # neither reached the settings
    # The import's bound is its own: the file's text as JSON carries it.
    monkeypatch.setattr(app_module, "MAX_IMPORT_BODY_BYTES", 10_000)
    text = json.dumps({"achievement": {}})
    small = client.post("/api/import/preview", json={"text": text, "file": "export.json"})
    assert small.status_code == 200 and previewed == [(text, "export.json", None)]
    large = client.post("/api/import/preview", json={"text": text + " " * 10_000, "file": "export.json"})
    assert (large.status_code, large.json()) == (413, {"error": "The file is larger than 30 MB. No achievement save is that big."})
    assert len(previewed) == 1
    # A body within the bound is read as before.
    assert client.put("/api/settings", json={"in_session_interval_min": 30}).json()["in_session_interval_min"] == 30


def test_auth_routes_in_demo_mode(client):
    for path in ("/api/auth/login", "/api/auth/verify", "/api/auth/logout"):
        response = client.post(path)
        assert response.status_code == 409
        assert response.json() == {"error": "Demo mode has no login."}


def test_auth_windows_need_the_shell(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False)
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        response = client.post("/api/auth/login")
        assert response.status_code == 409
        assert response.json() == {"error": "This needs the Guild Board window."}
        opened: list[str] = []
        core.window_requests = lambda kind: opened.append(kind) is None
        assert client.post("/api/auth/login").json() == {"opened": True}
        assert client.post("/api/auth/verify").json() == {"opened": True}
        assert opened == ["login", "verify"]
        assert client.post("/api/auth/logout").json()["sync"]["state"] == "login_needed"
        assert client.post("/api/demo/fault", json={"kind": "offline"}).status_code == 404
    core.store.close()


def test_backup(client, core):
    name = client.post("/api/backup").json()["file"]
    assert name.startswith("guildboard-manual-")
    assert (core.paths.backups_dir / name).is_file()


def test_status_answers_while_a_restore_is_staged(tmp_path, clock, monkeypatch):
    # Staging a restore unpacks and checks a file of up to 200 MB. It is done in a worker thread, so that the page's other
    # requests, the event stream and the core's loops go on meanwhile; here the staging waits until the status answered.
    import threading

    import guildboard.core as core_module

    core = _core(tmp_path, clock, demo=False)
    staging, answered = threading.Event(), threading.Event()

    def slow(source, data_dir):
        staging.set()
        answered.wait(10)

    monkeypatch.setattr(core_module, "stage_restore", slow)
    done: dict[str, object] = {}
    try:
        with TestClient(create_app(core, PORT), base_url=BASE) as client:
            client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
            restore = threading.Thread(target=lambda: done.update(answer=client.post(
                "/api/backup/restore", params={"file_name": "x.gbbackup"}, content=b"PK",
                headers={"Content-Type": "application/octet-stream"},
            )))
            restore.start()
            assert staging.wait(10)
            started = time.monotonic()
            assert client.get("/api/status").status_code == 200
            waited = time.monotonic() - started
            answered.set()
            restore.join(10)
        assert waited < 5  # it did not wait the ten seconds the staging waits
        assert done["answer"].json() == {"restarting": False}
    finally:
        answered.set()
        core.store.close()


def test_two_restores_at_once_are_staged_one_after_the_other(tmp_path, clock, monkeypatch):
    # Off the event loop, two restores sent at once could share the upload's file and the staged restore. They wait for
    # each other instead, as they did when they ran on it.
    import threading

    import guildboard.core as core_module

    core = _core(tmp_path, clock, demo=False)
    running = most = 0
    guard = threading.Lock()

    def slow(source, data_dir):
        nonlocal running, most
        with guard:
            running += 1
            most = max(most, running)
        time.sleep(0.3)
        with guard:
            running -= 1

    monkeypatch.setattr(core_module, "stage_restore", slow)
    answers: list[int] = []
    try:
        with TestClient(create_app(core, PORT), base_url=BASE) as client:
            client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)

            def send() -> None:
                answers.append(client.post(
                    "/api/backup/restore", params={"file_name": "x.gbbackup"}, content=b"PK",
                    headers={"Content-Type": "application/octet-stream"},
                ).status_code)

            both = [threading.Thread(target=send) for _ in range(2)]
            for thread in both:
                thread.start()
            for thread in both:
                thread.join(10)
        assert answers == [200, 200] and most == 1
    finally:
        core.store.close()


def test_the_backup_list_and_the_demo_refusing_export_and_restore(client, core):
    name = client.post("/api/backup").json()["file"]
    listed = client.get("/api/backups").json()
    assert listed[0]["name"] == name and listed[0]["kind"] == "manual"
    refusal = "The demo has no data of yours to back up."
    assert client.post("/api/backup/export").json()["error"] == refusal
    restore = client.post("/api/backup/restore", json={"name": name})
    assert restore.status_code == 422 and restore.json()["error"] == refusal
    assert client.post("/api/backup/restore", json={"nothing": 1}).status_code == 422


def test_icon_endpoint(client, core, clock):
    assert client.get("/api/icon/explore.13").status_code == 404  # demo regions have no icon
    assert client.get("/api/icon/no.such.key").status_code == 404
    core.store.upsert_labels(
        [
            Label("explore.900", "Test", "region", None, "https://act-webstatic.hoyoverse.com/test/icon.png"),
            Label("explore.901", "Elsewhere", "region", None, "https://evil.example/icon.png"),
        ],
        clock.now(),
    )
    core.icons._fetch = lambda url: PNG
    response = client.get("/api/icon/explore.900")
    assert (response.status_code, response.content, response.headers["content-type"]) == (200, PNG, "image/png")
    assert client.get("/api/icon/explore.901").status_code == 404


def test_icon_fetch_failure_is_not_retried_at_once(client, core, clock):
    # Each page that shows an icon asks for it: one that could not be fetched is asked of HoYoverse again a day later, not at
    # each of them (the audit's API-11).
    gone = "https://act-webstatic.hoyoverse.com/gone.png"
    core.store.upsert_labels([Label("explore.902", "Gone", "region", None, gone)], clock.now())
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        raise OSError("not found")

    seconds = {"now": 0.0}
    core.icons._fetch, core.icons._now = fetch, lambda: seconds["now"]
    for _ in range(3):
        assert client.get("/api/icon/explore.902").status_code == 404
    assert asked == [gone]
    seconds["now"] += 24 * 3600
    core.icons._fetch = lambda url: asked.append(url) or PNG
    assert client.get("/api/icon/explore.902").content == PNG and asked == [gone, gone]


def test_demo_fault_changes_the_sync_state(client, clock):
    assert client.post("/api/demo/fault", json={"kind": "offline"}).json() == {"fault": "offline"}
    assert client.post("/api/sync").json()["started"] is True
    status = _wait_until_idle(client)
    assert status["sync"]["state"] == "offline"
    assert status["sync"]["message"] == "HoYoLAB could not be reached."
    clock.advance(minutes=6)
    assert client.post("/api/demo/fault", json={"kind": "verification_needed"}).json() == {"fault": "verification_needed"}
    client.post("/api/sync")
    assert _wait_until_idle(client)["sync"]["state"] == "verification_needed"
    clock.advance(minutes=6)
    assert client.post("/api/sync").json()["reason"] == "verification_needed"
    assert client.post("/api/demo/fault", json={"kind": "none"}).json() == {"fault": "none"}
    assert client.post("/api/sync").json()["started"] is True
    assert _wait_until_idle(client)["sync"]["state"] == "ok"
    assert client.post("/api/demo/fault", json={"kind": "gremlins"}).status_code == 422


def test_pages_answer_before_the_first_sync(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False)
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        status = client.get("/api/status").json()
        assert (status["sync"]["state"], status["account"], status["tracking_since"]) == ("login_needed", None, None)
        backlog = client.get("/api/backlog")
        assert backlog.status_code == 200
        assert backlog.json()["regions"] == []
        stats = client.get("/api/stats", params={"range": "all"})
        assert stats.status_code == 200
        assert stats.json()["chests"]["total"] == 0
        assert client.get("/api/synclog").json() == []
        assert client.post("/api/sync").json() == {"started": False, "reason": "login_needed", "wait_seconds": 0}
    core.store.close()


def test_root_serves_a_placeholder_or_the_built_interface(core, tmp_path):
    with TestClient(create_app(core, PORT), base_url=BASE) as plain:
        assert "interface has not been built" in plain.get("/").text
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Guild Board</title><p>built interface</p>", encoding="utf-8")
    (web / "app.js").write_text("console.log('hi')", encoding="utf-8")
    with TestClient(create_app(core, PORT, web), base_url=BASE) as built:
        assert "built interface" in built.get("/").text
        assert built.get("/app.js").status_code == 200
        assert built.get("/api/status").status_code == 401  # the API still comes first


def test_event_route_exists(core):
    app = create_app(core, PORT)
    assert "/api/events" in {route.path for route in app.routes}


async def test_event_stream_formats_events():
    bus = EventBus()
    stream = sse_stream(bus, lambda: {"mode": "demo"})
    assert await anext(stream) == 'event: hello\ndata: {"mode": "demo"}\n\n'
    bus.publish("sync_started", trigger="manual")
    assert await anext(stream) == 'event: sync_started\ndata: {"type": "sync_started", "trigger": "manual"}\n\n'
    await stream.aclose()
    assert bus._subscribers == set()


async def test_event_stream_listens_before_it_reads_its_hello():
    # An event published while the hello is read comes after the hello, so the page does not keep an older status.
    bus = EventBus()

    def hello() -> dict:
        bus.publish("state_changed", state="paused", message="Syncing is paused.")
        return {"mode": "demo"}

    stream = sse_stream(bus, hello)
    assert await anext(stream) == 'event: hello\ndata: {"mode": "demo"}\n\n'
    assert await anext(stream) == (
        'event: state_changed\ndata: {"type": "state_changed", "state": "paused", "message": "Syncing is paused."}\n\n'
    )
    await stream.aclose()
    assert bus._subscribers == set()


async def test_event_stream_ends_when_guild_board_closes():
    bus = EventBus()
    stream = sse_stream(bus, lambda: {"mode": "demo"})
    await anext(stream)
    bus.publish("shutdown")
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert bus._subscribers == set()


async def test_event_route_reads_its_hello_once_its_stream_listens(core):
    # The sync is paused after the page asked for the stream and before the stream listens, so the stream does not
    # hear that change. Its hello is read after it, and says so.
    [route] = [route for route in create_app(core, PORT).routes if route.path == "/api/events"]
    response = await route.endpoint()
    core.set_paused(True)
    stream = response.body_iterator
    hello = await anext(stream)
    assert hello.startswith("event: hello\ndata: ")
    assert json.loads(hello.removeprefix("event: hello\ndata: "))["sync"]["state"] == "paused"
    await stream.aclose()
    assert core.bus._subscribers == set()


def test_requests_from_another_origin_are_refused(client, core):
    foreign = {"Origin": f"http://127.0.0.1:{PORT + 1}"}  # a page from another local program
    for path in ("/api/sync", "/api/sync/pause", "/api/backup"):
        response = client.post(path, headers=foreign)
        assert response.status_code == 403, path
        assert response.json() == {"error": "This request did not come from Guild Board."}
    assert client.put("/api/settings", json={"in_session_interval_min": 30}, headers=foreign).status_code == 403
    assert client.get("/api/status", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/status").json()["sync"]["state"] == "ok"  # nothing was paused
    assert client.get("/api/settings").json()["in_session_interval_min"] == 0
    assert list(core.paths.backups_dir.glob("guildboard-manual-*.db")) == []
    own = {"Origin": f"http://127.0.0.1:{PORT}", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/api/sync/pause", headers=own).status_code == 200
    assert client.post("/api/sync/resume", headers={"Origin": f"http://localhost:{PORT}"}).status_code == 200
    assert client.get("/api/status", headers={"Sec-Fetch-Site": "none"}).status_code == 200  # typed into the address bar


START = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


def test_demo_clock_moves_past_the_cooldown(tmp_path):
    core = Core(AppPaths(tmp_path / "data"), demo=True, demo_now=START, process_lister=lambda: set())
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert client.get("/api/status").json()["now"].startswith("2026-10-13T08:00")
        assert client.post("/api/sync").json()["started"] is True
        _wait_until_idle(client)
        assert client.post("/api/sync").json()["reason"] == "cooldown"
        assert client.post("/api/demo/clock", json={"advance_seconds": 360}).json()["now"].startswith("2026-10-13T08:06")
        assert client.post("/api/sync").json()["started"] is True
        _wait_until_idle(client)
        for bad in ({"advance_seconds": -1}, {"advance_seconds": "60"}, {"advance_seconds": True}, {"advance_seconds": 1e12}, {}):
            assert client.post("/api/demo/clock", json=bad).status_code == 422, bad
    core.store.close()


def test_the_demo_can_say_the_window_is_maximized_as_the_shell_would(tmp_path):
    core = Core(AppPaths(tmp_path / "data"), demo=True, demo_now=START, process_lister=lambda: set())
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert client.get("/api/status").json()["window_maximized"] is False
        assert client.post("/api/demo/window", json={"maximized": True}).json() == {"maximized": True}
        assert client.get("/api/status").json()["window_maximized"] is True
        for bad in ({"maximized": "yes"}, {"maximized": 1}, {}):
            assert client.post("/api/demo/window", json=bad).status_code == 422, bad
    core.store.close()


def test_demo_tools_do_not_exist_in_live_mode(tmp_path, clock):
    core = _core(tmp_path, clock, demo=False)
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert client.post("/api/demo/clock", json={"advance_seconds": 60}).status_code == 404
        assert client.post("/api/demo/fault", json={"kind": "offline"}).status_code == 404
        assert client.post("/api/demo/window", json={"maximized": True}).status_code == 404
    core.store.close()


def test_slow_demo_fault_keeps_the_sync_running_for_a_while(tmp_path):
    core = Core(AppPaths(tmp_path / "data"), demo=True, demo_now=START, process_lister=lambda: set())
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert client.post("/api/demo/fault", json={"kind": "slow"}).json() == {"fault": "slow"}
        assert client.post("/api/sync").json()["started"] is True
        assert client.get("/api/status").json()["sync"]["state"] == "syncing"
        for _ in range(500):
            if client.get("/api/status").json()["sync"]["state"] != "syncing":
                break
            time.sleep(0.02)
        assert client.get("/api/status").json()["sync"]["state"] == "ok"
        assert client.post("/api/demo/fault", json={"kind": "none"}).json() == {"fault": "none"}
        assert core.source.delay == 0.0
        assert client.post("/api/demo/fault", json={"kind": ["offline"]}).status_code == 422
    core.store.close()


def test_requests_to_the_window_need_the_window(client, core):
    for path in ("/api/data/open", "/api/demo/open"):
        response = client.post(path)
        assert response.status_code == 409, path
        assert response.json() == {"error": "This needs the Guild Board window."}
    asked: list[str] = []
    core.window_requests = lambda kind: asked.append(kind) is None
    assert client.post("/api/data/open").json() == {"opened": True}
    assert client.post("/api/demo/open").json() == {"opened": True}
    assert asked == ["data_folder", "demo"]


def test_the_page_may_load_nothing_from_elsewhere(core, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Guild Board</title>", encoding="utf-8")
    policy = (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )
    with TestClient(create_app(core, PORT, web), base_url=BASE) as built:
        assert built.get("/").headers["Content-Security-Policy"] == policy
    with TestClient(create_app(core, PORT), base_url=BASE) as plain:
        assert plain.get("/").headers["Content-Security-Policy"] == policy


def test_launch_with_non_ascii_key_is_403(core):
    with TestClient(create_app(core, PORT), base_url=BASE) as anonymous:
        response = anonymous.get("/launch", params={"k": "zażółć"}, follow_redirects=False)
        assert response.status_code == 403
        assert response.json() == {"error": "This launch link is not valid."}


def test_a_session_cookie_with_non_ascii_text_is_refused_not_a_server_error(core):
    with TestClient(create_app(core, PORT), base_url=BASE) as anonymous:
        response = anonymous.get("/api/status", headers={"Cookie": "gb_session=zażół".encode("utf-8")})
        assert response.status_code == 401


def test_ledger_history_long_before_is_422(client):
    assert client.get("/api/ledger/history", params={"kind": "primogem", "before": "9" * 5000}).status_code == 422
    assert client.get("/api/ledger/history", params={"kind": "primogem", "before": "1" * 19}).status_code == 200


def test_static_files_get_their_content_type_from_guild_board_not_the_registry(core, tmp_path):
    import mimetypes

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    for name in ("app.js", "app.css", "logo.svg", "pic.webp"):
        (web / name).write_bytes(b"x")
    before = {ext: mimetypes.guess_type("x" + ext)[0] for ext in (".js", ".css", ".svg", ".webp")}
    for ext in before:  # a PC whose registry maps them to something else
        mimetypes.add_type("text/plain", ext)
    try:
        with TestClient(create_app(core, PORT, web), base_url=BASE) as anonymous:
            types = {name: anonymous.get("/" + name) for name in ("app.js", "app.css", "logo.svg", "pic.webp", "")}
    finally:
        for ext, kind in before.items():
            if kind:
                mimetypes.add_type(kind, ext)
    assert {name: r.headers["content-type"].split(";")[0] for name, r in types.items()} == {
        "app.js": "text/javascript", "app.css": "text/css", "logo.svg": "image/svg+xml", "pic.webp": "image/webp",
        "": "text/html",
    }
    assert all(r.headers["x-content-type-options"] == "nosniff" for r in types.values())


def test_api_answers_say_nosniff_too(client):
    assert client.get("/api/status").headers["x-content-type-options"] == "nosniff"
    assert client.get("/api/icon/no.such.key").headers["x-content-type-options"] == "nosniff"


def test_tuskfizzy_legacy_import_is_validated_and_repeatable(client, core):
    payload = {
        "format": "tuskfizzy-local-data",
        "version": 1,
        "accounts": [{"id": "profile-a", "name": "Main", "portrait": "", "genshinId": "genshin-a"}],
        "genshinAccounts": [{"id": "genshin-a", "name": "Asia", "uid": "1234567890", "server": "asia"}],
        "dailyRecords": {"2026-11-08": {"profile-a": [True, False, True], "_resinLast": {"profile-a": 55}}},
        "selectedGenshinId": "genshin-a",
    }
    first = client.post("/api/tuskfizzy/import", json=payload)
    assert first.status_code == 200
    assert first.json() == {"imported": {"accounts": 1, "genshinAccounts": 1, "dailyRecords": 1}}
    second = client.post("/api/tuskfizzy/import", json=payload)
    assert second.status_code == 200
    assert second.json() == first.json()
    row = core.store._conn.execute(
        "SELECT login, commission, resin FROM tuskfizzy_daily WHERE day = ? AND account_id = ?",
        ("2026-11-08", "profile-a"),
    ).fetchone()
    assert row == (1, 0, 1)
    previous_resin = core.store._conn.execute(
        "SELECT resin_last FROM tuskfizzy_daily WHERE day = ? AND account_id = ?",
        ("2026-11-08", "profile-a"),
    ).fetchone()
    assert previous_resin[0] == 55
    assert core.store.meta("tuskfizzy_selected_genshin_id") == "genshin-a"


def test_tuskfizzy_legacy_import_rejects_invalid_uid_without_writing(client, core):
    payload = {
        "format": "tuskfizzy-local-data",
        "version": 1,
        "accounts": [{"id": "profile-a", "name": "Main"}],
        "genshinAccounts": [{"id": "genshin-a", "name": "Asia", "uid": "12345"}],
        "dailyRecords": {},
    }
    response = client.post("/api/tuskfizzy/import", json=payload)
    assert response.status_code == 422
    assert core.store.tuskfizzy_import_counts() == {"accounts": 0, "genshinAccounts": 0, "dailyRecords": 0}


def test_tuskfizzy_tracker_profiles_and_checklist_can_be_managed(client):
    created = client.post("/api/tuskfizzy/accounts", json={"name": "Main", "portrait": "", "genshinId": None})
    assert created.status_code == 200
    account = created.json()["account"]
    ident = account["id"]
    assert account["name"] == "Main"

    changed = client.put(
        "/api/tuskfizzy/daily",
        json={"day": "2026-11-08", "accountId": ident, "task": "resin", "checked": True},
    )
    assert changed.status_code == 200
    assert changed.json()["daily"][ident] == [False, False, True]

    tracker = client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-08"})
    assert tracker.status_code == 200
    assert tracker.json()["accounts"][0]["id"] == ident
    assert tracker.json()["daily"][ident] == [False, False, True]
    assert tracker.json()["dailyNote"] is None  # demo data must never be presented as a live HoYoLAB note
    month = client.get("/api/tuskfizzy/month", params={"month": "2026-11"})
    assert month.status_code == 200
    assert month.json()["records"]["2026-11-08"][ident] == [False, False, True]

    deleted = client.delete(f"/api/tuskfizzy/accounts/{ident}")
    assert deleted.status_code == 200
    assert client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-08"}).json()["accounts"] == []


def test_tuskfizzy_genshin_profiles_can_be_managed_and_unlinked(client):
    created = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    )
    assert created.status_code == 200
    profile = created.json()["account"]
    ident = profile["id"]
    assert profile["uid"] == "1234567890"

    tracker_profile = client.post(
        "/api/tuskfizzy/accounts",
        json={"name": "Main", "portrait": "", "genshinId": ident},
    ).json()["account"]
    assert tracker_profile["genshinId"] == ident

    removed = client.delete(f"/api/tuskfizzy/genshin-accounts/{ident}")
    assert removed.status_code == 200
    tracker = client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-08"}).json()
    assert tracker["genshinAccounts"] == []
    assert tracker["accounts"][0]["genshinId"] is None


def test_tuskfizzy_selected_genshin_profile_can_be_changed(client):
    first = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    ).json()["account"]
    second = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Europe", "uid": "9876543210", "server": "os_eu"},
    ).json()["account"]

    selected = client.put("/api/tuskfizzy/selected-genshin", json={"id": first["id"]})
    assert selected.status_code == 200
    assert selected.json()["selectedGenshinId"] == first["id"]
    assert client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-08"}).json()["selectedGenshinId"] == first["id"]

    changed = client.put("/api/tuskfizzy/selected-genshin", json={"id": second["id"]})
    assert changed.status_code == 200
    assert client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-08"}).json()["selectedGenshinId"] == second["id"]

    cleared = client.put("/api/tuskfizzy/selected-genshin", json={"id": None})
    assert cleared.status_code == 200
    assert client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-08"}).json()["selectedGenshinId"] is None


def test_tuskfizzy_daily_note_endpoint_applies_threshold_rules(client):
    account = client.post("/api/tuskfizzy/accounts", json={"name": "Main"}).json()["account"]
    ident = account["id"]
    base = {"day": "2026-11-09", "accountId": ident, "extraRewardClaimed": False}

    first = client.post("/api/tuskfizzy/observe-note", json={**base, "resin": 42})
    assert first.status_code == 200
    assert first.json()["daily"][ident] == [False, False, False]

    client.post("/api/tuskfizzy/observe-note", json={**base, "resin": 55})
    crossed = client.post("/api/tuskfizzy/observe-note", json={**base, "resin": 49})
    assert crossed.json()["daily"][ident] == [False, False, True]

    claimed = client.post("/api/tuskfizzy/observe-note", json={**base, "resin": 120, "extraRewardClaimed": True})
    assert claimed.json()["daily"][ident] == [False, True, True]


def test_tracker_applies_fresh_notes_only_to_the_linked_genshin_profile(client, core):
    genshin = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    ).json()["account"]
    profile = client.post(
        "/api/tuskfizzy/accounts",
        json={"name": "Main", "genshinId": genshin["id"]},
    ).json()["account"]
    core.store.set_meta("account_uid", "1234567890")
    day = core.clock.now().astimezone().date().isoformat()

    core.store.set_latest_doc("notes", core.clock.now(), {"resin": 55, "extra_reward_claimed": False})
    client.get("/api/tuskfizzy/tracker", params={"day": day})
    assert core.store.tuskfizzy_daily_for(day)[profile["id"]] == [False, False, False]

    core.store.set_latest_doc("notes", core.clock.now(), {"resin": 49, "extra_reward_claimed": False})
    client.get("/api/tuskfizzy/tracker", params={"day": day})
    assert core.store.tuskfizzy_daily_for(day)[profile["id"]] == [False, False, True]

    core.store.set_latest_doc("notes", core.clock.now(), {"resin": 49, "extra_reward_claimed": True})
    client.get("/api/tuskfizzy/tracker", params={"day": day})
    assert core.store.tuskfizzy_daily_for(day)[profile["id"]] == [False, True, True]


def test_tuskfizzy_daily_note_only_marks_resin_on_observed_crossing_and_keeps_it_checked(core):
    store = core.store
    store.tuskfizzy_put_account("profile-a", "Main", "", None, "2026-11-09T00:00:00")

    # The first observation is already below 50: it must not auto-complete Resin.
    assert store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 30, False)
    assert store.tuskfizzy_daily_for("2026-11-09")["profile-a"] == [False, False, False]

    # A later observation above the threshold followed by one below it is a real crossing.
    assert store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 55, False)
    assert store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 49, False)
    assert store.tuskfizzy_daily_for("2026-11-09")["profile-a"] == [False, False, True]

    # Refilling must not clear the completed Resin task; claiming the bonus completes Commission.
    assert store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 160, True)
    assert store.tuskfizzy_daily_for("2026-11-09")["profile-a"] == [False, True, True]


def test_tuskfizzy_daily_note_does_not_update_unknown_profile(core):
    assert not core.store.tuskfizzy_observe_daily_note("2026-11-09", "missing", 20, True)


def test_tuskfizzy_selected_genshin_profile_can_be_changed_and_removed(client):
    created = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    )
    assert created.status_code == 200
    ident = created.json()["account"]["id"]

    selected = client.put("/api/tuskfizzy/selected-genshin", json={"id": ident})
    assert selected.status_code == 200
    assert selected.json() == {"selectedGenshinId": ident}
    assert client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-09"}).json()["selectedGenshinId"] == ident

    assert client.delete(f"/api/tuskfizzy/genshin-accounts/{ident}").status_code == 200
    assert client.get("/api/tuskfizzy/tracker", params={"day": "2026-11-09"}).json()["selectedGenshinId"] is None


def test_tuskfizzy_import_without_a_selected_profile_clears_old_selection(client, core):
    core.store.tuskfizzy_put_genshin_account("genshin-a", "Asia", "1234567890", "os_asia", "2026-11-09T00:00:00")
    assert core.store.tuskfizzy_select_genshin_account("genshin-a")
    response = client.post("/api/tuskfizzy/import", json={
        "format": "tuskfizzy-local-data",
        "version": 1,
        "accounts": [],
        "genshinAccounts": [],
        "dailyRecords": {},
        "selectedGenshinId": "",
    })
    assert response.status_code == 200
    assert core.store.tuskfizzy_selected_genshin_id() is None



def test_tuskfizzy_enka_endpoint_validates_uid_and_returns_public_profile(client, monkeypatch):
    from guildboard.api import app as api_app

    seen = []
    monkeypatch.setattr(api_app.enka, "get_profile", lambda uid: seen.append(uid) or {
        "uid": uid, "playerInfo": {"nickname": "Traveler"}, "avatarInfoList": [], "ttl": 300,
        "fetchedAt": 1, "cached": False,
    })
    invalid = client.get("/api/tuskfizzy/enka/12345678")
    assert invalid.status_code == 422
    response = client.get("/api/tuskfizzy/enka/1234567890")
    assert response.status_code == 200
    assert response.json()["playerInfo"]["nickname"] == "Traveler"
    assert seen == ["1234567890"]


def test_tuskfizzy_enka_endpoint_maps_missing_profile(client, monkeypatch):
    from guildboard.api import app as api_app

    def missing(uid):
        raise api_app.enka.EnkaNotFound("No public profile.")
    monkeypatch.setattr(api_app.enka, "get_profile", missing)
    response = client.get("/api/tuskfizzy/enka/123456789")
    assert response.status_code == 404
    assert response.json() == {"error": "No public profile."}



def test_tuskfizzy_profile_login_is_scoped_to_selected_profile(client, monkeypatch):
    from guildboard.api import app as api_app

    seen = []
    async def login(profile_id, cookie_text):
        seen.append((profile_id, cookie_text))
        return {"uid": "1234567890", "nickname": "Traveler", "server": "os_asia"}
    monkeypatch.setattr(api_app.core, "tuskfizzy_login", login, raising=False)
    response = client.post(
        "/api/tuskfizzy/genshin-accounts/profile-a/login",
        json={"cookieText": "ltoken_v2=secret; ltuid_v2=1234567890"},
    )
    assert response.status_code == 200
    assert response.json() == {"connected": True, "account": {
        "uid": "1234567890", "nickname": "Traveler", "server": "os_asia",
    }}
    assert seen == [("profile-a", "ltoken_v2=secret; ltuid_v2=1234567890")]


def test_tuskfizzy_profile_login_requires_cookie_text(client):
    response = client.post("/api/tuskfizzy/genshin-accounts/profile-a/login", json={"uid": "1234567890"})
    assert response.status_code == 422


def test_tuskfizzy_profile_logout_is_scoped_to_profile(client, monkeypatch):
    from guildboard.api import app as api_app

    seen = []
    monkeypatch.setattr(api_app.core, "tuskfizzy_logout", lambda profile_id: seen.append(profile_id), raising=False)
    response = client.delete("/api/tuskfizzy/genshin-accounts/profile-a/login")
    assert response.status_code == 200
    assert response.json() == {"connected": False}
    assert seen == ["profile-a"]


def test_tuskfizzy_tracker_reads_the_selected_profile_session_not_primary_login(client, core, monkeypatch):
    profile = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    ).json()["account"]
    checklist = client.post(
        "/api/tuskfizzy/accounts",
        json={"name": "Main", "genshinId": profile["id"]},
    ).json()["account"]
    client.put("/api/tuskfizzy/selected-genshin", json={"id": profile["id"]})
    day = core.clock.now().astimezone().date().isoformat()
    calls = []
    async def note(profile_id):
        calls.append(profile_id)
        return {
            "profileName": "Asia", "uid": "1234567890", "resin": 49, "resinMax": 200,
            "commissionDone": 4, "commissionTotal": 4, "bonusClaimed": True,
            "expeditionsDone": 5, "expeditionsTotal": 5, "readAt": core.clock.now().isoformat(),
        }
    monkeypatch.setattr(core, "tuskfizzy_profile_note", note)
    response = client.get("/api/tuskfizzy/tracker", params={"day": day})
    assert response.status_code == 200
    assert response.json()["dailyNote"]["profileName"] == "Asia"
    assert response.json()["daily"][checklist["id"]] == [False, True, True]
    assert calls == [profile["id"]]


def test_tuskfizzy_tracker_does_not_use_primary_login_for_unconnected_profile(client, core, monkeypatch):
    profile = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    ).json()["account"]
    client.put("/api/tuskfizzy/selected-genshin", json={"id": profile["id"]})
    day = core.clock.now().astimezone().date().isoformat()
    monkeypatch.setattr(core, "tuskfizzy_profile_note", lambda profile_id: None)
    response = client.get("/api/tuskfizzy/tracker", params={"day": day})
    assert response.status_code == 200
    assert response.json()["dailyNote"] is None



def test_tuskfizzy_tracker_includes_selected_profile_battle_chronicle(client, core, monkeypatch):
    profile = client.post(
        "/api/tuskfizzy/genshin-accounts",
        json={"name": "Asia", "uid": "1234567890", "server": "os_asia"},
    ).json()["account"]
    client.put("/api/tuskfizzy/selected-genshin", json={"id": profile["id"]})
    day = core.clock.now().astimezone().date().isoformat()
    calls = []
    async def chronicle(profile_id):
        calls.append(profile_id)
        return {
            "uid": "1234567890", "nickname": "Traveler", "server": "os_asia",
            "adventureRank": 60, "abyssProgress": "12-3", "counters": {"spiral_abyss": 36},
            "regions": [], "endgame": [], "readAt": core.clock.now().isoformat(),
        }
    async def note(profile_id):
        return None
    monkeypatch.setattr(core, "tuskfizzy_profile_chronicle", chronicle)
    monkeypatch.setattr(core, "tuskfizzy_profile_note", note)
    response = client.get("/api/tuskfizzy/tracker", params={"day": day})
    assert response.status_code == 200
    assert response.json()["battleChronicle"]["abyssProgress"] == "12-3"
    assert calls == [profile["id"]]
