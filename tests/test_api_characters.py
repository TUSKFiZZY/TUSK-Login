"""Task V2: the Characters routes: each route's answer and each of its 422, 404 and 409 sentences, the characters_changed that every
change publishes, the refresh's limits, the Theater's pick for the month, and the art route."""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import characters_samples as samples
from characters_service_support import SUNDAY, build_record, calculator, keep_builds, keep_notes
from characters_shape import problem_with
from guildboard.api.app import UNEXPECTED_REQUEST, create_app
from guildboard.config import AppPaths
from guildboard.core import Core
from support import AccountSource, FakeClock, FakeSleep

PORT = 4321
BASE = f"http://127.0.0.1:{PORT}"
NO_SUCH = "There is no such character."
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@contextmanager
def opened(core: Core):
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        assert client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 303
        yield client


def _core(tmp_path, clock, *, demo: bool = False) -> Core:
    return Core(
        AppPaths(tmp_path / "data"), demo=demo, clock=clock, source=None if demo else AccountSource(),
        sleep=FakeSleep(clock), process_lister=lambda: set(),
    )


@pytest.fixture
def clock():
    return FakeClock(SUNDAY)


@pytest.fixture
def core(tmp_path, clock):
    core = _core(tmp_path, clock)
    keep_builds(core.store, clock.now(), [build_record(samples.sandrone_build())], calculator())
    keep_notes(core.store, clock.now())
    yield core
    core.store.close()


@pytest.fixture
def client(core):
    with opened(core) as client:
        yield client


def test_character_sync_says_its_cadence(core):
    assert core.characters.sync_view()["every_minutes"] == 360


def _published(queue) -> list[dict]:
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    return seen


def test_the_roster_the_profile_and_the_planner_answer_in_their_shapes(client):
    roster = client.get("/api/characters").json()
    assert problem_with(roster, "Roster") is None and roster["owned"] == 1
    profile = client.get("/api/characters/sandrone").json()
    assert problem_with(profile, "Profile") is None and profile["readiness"]["total"] > 0
    planner = client.get("/api/characters/sandrone/plan").json()
    assert problem_with(planner, "Planner") is None and planner["plan"]["talents"] == {"normal": 10, "skill": 10, "burst": 10}
    assert client.get("/api/characters/nobody").json() == {"error": NO_SUCH}
    assert client.get("/api/characters/nobody").status_code == 404
    assert client.get("/api/characters/nobody/plan").status_code == 404


def test_the_plan_is_put_kept_and_deleted_with_its_sentences(client):
    body = client.get("/api/characters/sandrone/plan").json()["plan"]
    answer = client.put("/api/characters/sandrone/plan", json={**body, "refinement": 3})
    assert answer.status_code == 200 and problem_with(answer.json(), "Planner") is None
    assert answer.json()["plan"]["refinement"] == 3
    assert client.get("/api/characters/sandrone/plan").json()["plan"]["refinement"] == 3
    for change, sentence in (
        ({"talents": {"normal": 0, "skill": 10, "burst": 10}}, "Choose talent levels from 1 to 10."),
        ({"level": 0}, "Choose a level from 1 to 90."),
        ({"refinement": 0}, "Choose a refinement from 1 to 5."),
        ({"typed": {"slot": "goblet", "main": "dmg", "subs": [["cr", -1]], "use": True}}, "Give each substat as a number from 0 to 100."),
    ):
        refused = client.put("/api/characters/sandrone/plan", json={**body, **change})
        assert (refused.status_code, refused.json()) == (422, {"error": sentence})
    assert client.put("/api/characters/nobody/plan", json=body).status_code == 404
    assert client.put("/api/characters/sandrone/plan", json=[1]).json() == {"error": UNEXPECTED_REQUEST}
    back = client.delete("/api/characters/sandrone/plan")
    assert back.status_code == 200 and back.json()["plan"]["refinement"] == 1
    assert client.delete("/api/characters/nobody/plan").status_code == 404


def test_the_card_is_edited_with_a_partial_card_and_goes_back(client):
    edited = client.put("/api/characters/sandrone/card", json={"roles": ["support"], "keeps_alive": True})
    assert edited.status_code == 200 and problem_with(edited.json(), "Profile") is None
    assert edited.json()["card"]["edited"] == ["keeps_alive", "roles"]
    refused = client.put("/api/characters/sandrone/card", json={"nickname": "x"})
    assert (refused.status_code, refused.json()) == (422, {"error": "That is not a field of a card."})
    assert client.put("/api/characters/sandrone/card", json={}).status_code == 422
    assert client.put("/api/characters/nobody/card", json={"roles": ["damage"]}).status_code == 404
    # Her goals as the card gives them, researched or the role's defaults: the edit changes cr alone.
    goals = client.get("/api/characters/sandrone").json()["card"]["fields"]["goals"]
    both = client.put("/api/characters/sandrone/card", json={"goals": {"cr": 60}}).json()["card"]
    assert both["edited"] == ["goals", "keeps_alive", "roles"] and both["fields"]["goals"] == {**goals, "cr": 60.0}
    one = client.put("/api/characters/sandrone/card", json={"goals": None}).json()["card"]   # null: that field, back to the card
    assert one["edited"] == ["keeps_alive", "roles"] and one["fields"]["goals"] == goals
    back = client.delete("/api/characters/sandrone/card")
    assert back.status_code == 200 and back.json()["card"]["edited"] == []


def test_the_amounts_held_are_typed_with_their_sentence(client):
    assert client.put("/api/characters/held", json={"amounts": {"mora": 5000, "crown-of-insight": None}}).json() == {"ok": True}
    for bad in ({"mora": -1}, {"mora": 10_000_000}, {"mora": 1.5}, {"mora": "5"}, {}):
        refused = client.put("/api/characters/held", json={"amounts": bad})
        assert (refused.status_code, refused.json()) == (422, {"error": "Give amounts as whole numbers from 0 to 9,999,999."})
    assert client.put("/api/characters/held", json={}).status_code == 422


def test_the_focus_the_week_and_its_ticks(client):
    answer = client.put("/api/focus", json={"characters": ["sandrone"]})
    assert answer.status_code == 200 and problem_with(answer.json(), "Week") is None
    assert [card["key"] for card in answer.json()["focus"]] == ["sandrone"]
    refused = client.put("/api/focus", json={"characters": ["sandrone", "odette", "alyosha", "jahoda", "nefer"]})
    assert (refused.status_code, refused.json()) == (422, {"error": "Four at most: take one out first."})
    assert client.put("/api/focus", json={"characters": ["nobody"]}).json() == {"error": NO_SUCH}
    assert client.put("/api/focus", json={"characters": ["nobody"]}).status_code == 404
    week = client.get("/api/week").json()
    assert problem_with(week, "Week") is None and week["today"]["day"] == "2026-10-25"
    # a task of today is ticked, one of another day or of no day is not
    tasks = week["today"]["tasks"]
    if tasks:
        ticked = client.put("/api/week/ticks", json={"day": "2026-10-25", "task": tasks[0]["key"], "done": True})
        assert ticked.status_code == 200 and ticked.json()["today"]["tasks"][0]["done"] is True
    refused = client.put("/api/week/ticks", json={"day": "2026-10-26", "task": "x", "done": True})
    assert (refused.status_code, refused.json()) == (422, {"error": "Only today's tasks can be ticked."})


def test_the_endgame_answers_now_and_after_and_the_theater_pick_is_kept_for_the_month(client, core, clock):
    assert problem_with(client.get("/api/endgame").json(), "Endgame") is None
    assert client.get("/api/endgame", params={"after": 1}).json()["after"] is True
    assert client.get("/api/endgame", params={"after": 2}).status_code == 422
    first = client.put("/api/endgame/theater", json={"elements": ["Pyro", "Hydro", "Geo"]})
    assert first.status_code == 200 and first.json()["theater"]["elements"] == ["Pyro", "Hydro", "Geo"]
    assert first.json()["theater"]["from_calendar"] is False and first.json()["theater"]["month"] == "2026-10"
    refused = client.put("/api/endgame/theater", json={"elements": ["Pyro", "Pyro", "Geo"]})
    assert (refused.status_code, refused.json()) == (422, {"error": "Choose three different elements."})
    assert client.put("/api/endgame/theater", json={"elements": ["Pyro", "Hydro"]}).status_code == 422
    assert client.get("/api/endgame").json()["theater"]["elements"] == ["Pyro", "Hydro", "Geo"]   # kept for the month
    assert client.get("/api/endgame").json()["theater"]["calendar_elements"] == []   # the demo's calendar here names none
    dropped = client.delete("/api/endgame/theater")
    assert dropped.status_code == 200 and dropped.json()["theater"]["elements"] == [] and problem_with(dropped.json(), "Endgame") is None
    client.put("/api/endgame/theater", json={"elements": ["Pyro", "Hydro", "Geo"]})
    clock.advance(days=10)   # November
    assert client.get("/api/endgame").json()["theater"]["elements"] == []


def test_every_change_publishes_characters_changed(client, core):
    queue = core.bus.subscribe()
    client.get("/api/characters"), client.get("/api/characters/sandrone"), client.get("/api/week"), client.get("/api/endgame")
    assert _published(queue) == []   # reading changes nothing
    plan = client.get("/api/characters/sandrone/plan").json()["plan"]
    for method, path, body in (
        ("PUT", "/api/focus", {"characters": ["sandrone"]}),
        ("PUT", "/api/characters/sandrone/plan", plan),
        ("DELETE", "/api/characters/sandrone/plan", None),
        ("PUT", "/api/characters/sandrone/card", {"roles": ["damage"]}),
        ("DELETE", "/api/characters/sandrone/card", None),
        ("PUT", "/api/characters/held", {"amounts": {"mora": 1}}),
        ("PUT", "/api/endgame/theater", {"elements": ["Pyro", "Hydro", "Geo"]}),
        ("DELETE", "/api/endgame/theater", None),
    ):
        assert client.request(method, path, json=body).status_code == 200, path
        assert _published(queue) == [{"type": "characters_changed"}], path
    assert client.put("/api/focus", json={"characters": ["nobody"]}).status_code == 404
    assert client.put("/api/characters/held", json={"amounts": {"mora": -1}}).status_code == 422
    assert _published(queue) == []   # a refused change publishes nothing


def test_a_refresh_without_a_login_is_refused(client):
    refused = client.post("/api/characters/refresh")
    assert (refused.status_code, refused.json()) == (409, {"error": "Log in to HoYoLAB first."})


def test_a_second_refresh_is_refused_with_the_minutes_to_wait(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        with opened(core) as client:
            assert client.post("/api/characters/refresh").json() == {"started": True}
            for _ in range(200):   # the run on the demo's records ends in a moment
                if core.scheduler.current_state() != "syncing":
                    break
                time.sleep(0.02)
            again = client.post("/api/characters/refresh")
            assert again.status_code == 409
            assert again.json() == {"error": "TUSKFiZZY read HoYoLAB a moment ago. Try again in 5 minutes."}
            clock.advance(minutes=4)
            assert "Try again in 1 minute." in client.post("/api/characters/refresh").json()["error"]
    finally:
        core.store.close()


def test_a_refresh_after_the_days_24_runs_says_so_with_the_minutes_until_the_count_starts_again(tmp_path, clock):
    import math

    from guildboard.sync.scheduler import local_midnight

    core = _core(tmp_path, clock, demo=True)
    try:
        start = local_midnight(clock.now())
        for number in range(24):   # twenty-four runs today, all long past the 5-minute gap
            run = core.store.start_run("interval", start + timedelta(seconds=number))
            core.store.finish_run(run, start + timedelta(seconds=number + 1), "ok")
        minutes = math.ceil((start + timedelta(days=1) - clock.now()).total_seconds() / 60)
        with opened(core) as client:
            refused = client.post("/api/characters/refresh")
            assert (refused.status_code, refused.json()) == (409, {
                "error": f"TUSKFiZZY has read HoYoLAB 24 times today, the most it reads in a day. Try again in {minutes} minutes."
            })
    finally:
        core.store.close()


def test_a_refresh_while_paused_or_waiting_for_a_check_is_refused_with_what_to_do(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    try:
        with opened(core) as client:
            core.settings.sync_paused = True
            assert core.characters.sync_view()["blocked"] == "paused"
            paused = client.post("/api/characters/refresh")
            assert (paused.status_code, paused.json()) == (409, {"error": "Syncing is paused. Resume it in Settings."})
            core.settings.sync_paused = False
            core.scheduler._block = "verification_needed"
            assert core.characters.sync_view()["blocked"] == "verification_needed"
            check = client.post("/api/characters/refresh")
            assert (check.status_code, check.json()) == (
                409, {"error": "HoYoLAB asked for a verification check. Finish it from the sync panel first."}
            )
            assert core.characters.sync_view()["can_refresh"] is False
            core.scheduler._block = "login_needed"
            assert core.characters.sync_view()["blocked"] == "login_needed"
            core.scheduler._block = None
            assert core.characters.sync_view()["blocked"] is None
    finally:
        core.store.close()


def test_the_art_route_serves_a_cached_picture_with_its_type(client, core):
    url = "https://act-webstatic.hoyoverse.com/event-static-hoyowiki-admin/2026/06/29/4ee285b198b7bb8dba2a0a63aa9ffc9f_1.png"
    asked = []
    core.art._fetch_bytes = lambda address, max_bytes, hosts=None: asked.append(address) or PNG
    address = core.art.address(url)
    assert client.get(address).status_code == 404 and asked == []   # nothing is fetched before the player's consent
    core.settings.risk_acknowledged = True
    found = client.get(address)
    assert (found.status_code, found.headers["content-type"], found.content) == (200, "image/png", PNG)
    assert client.get(address).content == PNG and len(asked) == 1   # kept: no second request
    missing = client.get("/api/art/" + "0" * 32)
    assert (missing.status_code, missing.json()) == (404, {"error": "There is no picture for this."})


def test_after_the_days_24_runs_the_page_shows_the_minutes_until_the_count_starts_again(client, core, clock):
    import math

    from guildboard.sync.scheduler import local_midnight

    start = local_midnight(clock.now())
    for number in range(24):   # twenty-four runs today, all long past the 5-minute gap
        run = core.store.start_run("interval", start + timedelta(seconds=number))
        core.store.finish_run(run, start + timedelta(seconds=number + 1), "ok")
    sync = client.get("/api/characters").json()["sync"]
    minutes = math.ceil((start + timedelta(days=1) - clock.now()).total_seconds() / 60)
    assert sync["can_refresh"] is False and sync["wait_minutes"] == minutes
    assert client.get("/api/week").json()["sync"]["wait_minutes"] == minutes
