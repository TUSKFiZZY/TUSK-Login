import asyncio
import hashlib
import json
import urllib.error

import pytest

from achievement_samples import SMALL_LIST, list_bytes
from guildboard import netpolicy
from guildboard.achievements.catalog import bundled_raw
from guildboard.achievements.updates import MAX_LIST_BYTES, ListUpdates, UpdateError, demo_fetch, github_fetch
from guildboard.store import ProgressRow

BASE = list_bytes(SMALL_LIST)


def _fetching(*answers):
    queue = list(answers)

    async def fetch() -> bytes:
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return fetch


def _newer() -> bytes:
    newer = json.loads(BASE)
    newer["1"]["achievements"] = [s for s in newer["1"]["achievements"] if s["id"] != 80001]
    newer["0"]["achievements"][1]["reward"] = 10
    newer["1"]["achievements"].append({"id": 99001, "name": "New", "desc": "New.", "reward": 5, "ver": "7.2"})
    return list_bytes(newer)


def _newest() -> bytes:
    """_newer() with one achievement more, added in 7.3."""
    newest = json.loads(_newer())
    newest["1"]["achievements"].append({"id": 99002, "name": "Newer", "desc": "Newer.", "reward": 5, "ver": "7.3"})
    return list_bytes(newest)


def _updates(store, clock, *answers) -> ListUpdates:
    return ListUpdates(store, clock, _fetching(*answers), bundled=lambda: BASE)


async def test_check_shows_what_is_new_and_install_puts_it_in_place(store, clock):
    updates = _updates(store, clock, _newer())
    assert updates.meta() == {"source": "bundled", "version": "7.1", "stages": 11, "installed_at": None,
                              "dated": "2026-09-23", "previous": None}
    preview = await updates.check()
    assert preview["up_to_date"] is False and preview["version"] == "7.2" and preview["stages"] == 11
    assert preview["added"] == [{"id": 99001, "name": "New", "description": "New.", "version": "7.2",
                                 "category": "Mortal Travails: Series I"}]
    assert preview["changed"] == [{"id": 84512, "name": "For the Love of Godwin", "what": ["reward"]}]
    assert preview["removed"] == [{"id": 80001, "name": "The Wind and The Star Traveler"}]
    assert updates.current().stage(99001) is None  # nothing changes before the player installs
    meta = updates.install(preview["sha256"])
    assert meta["source"] == "github" and meta["version"] == "7.2"
    assert meta["previous"]["version"] == "7.1"
    assert meta["dated"] is None and meta["installed_at"] == "2026-10-13T08:00:00+00:00"  # installed_at says when
    assert updates.current().stage(99001) is not None


async def test_the_same_list_is_up_to_date(store, clock):
    updates = _updates(store, clock, BASE)
    preview = await updates.check()
    assert preview["up_to_date"] is True and preview["added"] == [] and preview["removed"] == []
    with pytest.raises(UpdateError) as error:
        updates.install(preview["sha256"])  # nothing waits: the same list in both slots would lose the earlier one
    assert str(error.value) == "This list is no longer waiting. Check for new achievements again."


async def test_the_installed_list_checked_again_is_up_to_date_and_not_kept_waiting(store, clock):
    updates = _updates(store, clock, _newer(), _newer())
    updates.install((await updates.check())["sha256"])
    preview = await updates.check()
    assert preview["up_to_date"] is True and preview["added"] == preview["changed"] == preview["removed"] == []
    with pytest.raises(UpdateError) as error:
        updates.install(preview["sha256"])  # it would fill both slots and push the bundled list out
    assert str(error.value) == "This list is no longer waiting. Check for new achievements again."
    assert store.achievement_list("previous").source == "bundled"


@pytest.mark.parametrize("answer, message", [
    (OSError("no route"), "The achievement list could not be downloaded. Check the connection and try again."),
    (urllib.error.HTTPError(netpolicy.ACHIEVEMENT_LIST_URL, 404, "Not Found", None, None),
     "The achievement list was not found at its address. The installed list stays."),
    (netpolicy.ResponseTooLarge("the response is larger than allowed"),
     "The downloaded list is larger than 2 MB. The installed list stays."),
    (netpolicy.HostNotAllowed("raw.githubusercontent.com"),
     "The achievement list moved to another address, which Guild Board does not follow. The installed list stays."),
    (b"<!DOCTYPE html>", "The downloaded list is not usable. The achievement list is not valid JSON. The installed list stays."),
    (list_bytes({"0": SMALL_LIST["0"]}), "The downloaded list has 6 achievements, far fewer than the 11 installed. The installed list stays."),
])
async def test_a_broken_or_much_smaller_download_is_refused_and_the_list_stays(store, clock, answer, message):
    updates = _updates(store, clock, answer)
    with pytest.raises(UpdateError) as error:
        await updates.check()
    assert str(error.value) == message
    assert updates.meta()["stages"] == 11 and store.achievement_list() is None


@pytest.mark.parametrize("failure", [OSError("no route"), b"<!DOCTYPE html>", list_bytes({"0": SMALL_LIST["0"]})],
                         ids=["not-downloaded", "not-usable", "far-smaller"])
async def test_a_check_that_fails_or_is_refused_leaves_nothing_waiting(store, clock, failure):
    updates = _updates(store, clock, _newer(), failure)
    newer = (await updates.check())["sha256"]
    with pytest.raises(UpdateError):
        await updates.check()
    with pytest.raises(UpdateError) as error:
        updates.install(newer)  # the preview from before the failed check can no longer be installed
    assert str(error.value) == "This list is no longer waiting. Check for new achievements again."


async def test_of_two_checks_at_once_the_one_that_ends_last_decides_what_waits(store, clock):
    first, second = asyncio.Event(), asyncio.Event()
    downloads = [(first, _newer()), (second, OSError("no route"))]

    async def fetch() -> bytes:
        gate, answer = downloads.pop(0)
        await gate.wait()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    updates = ListUpdates(store, clock, fetch, bundled=lambda: BASE)
    earlier, later = asyncio.create_task(updates.check()), asyncio.create_task(updates.check())
    await asyncio.sleep(0)
    assert downloads == []  # both checks are downloading
    first.set()
    newer = (await earlier)["sha256"]
    second.set()
    with pytest.raises(UpdateError):
        await later
    with pytest.raises(UpdateError) as error:
        updates.install(newer)  # the check that ended last failed
    assert str(error.value) == "This list is no longer waiting. Check for new achievements again."


async def test_a_list_a_little_smaller_may_be_installed(store, clock):
    smaller = json.loads(BASE)
    smaller["1"]["achievements"].pop()
    preview = await _updates(store, clock, list_bytes(smaller)).check()
    assert [r["id"] for r in preview["removed"]] == [81222]


async def test_install_needs_the_list_that_was_checked(store, clock):
    updates = _updates(store, clock, _newer())
    with pytest.raises(UpdateError) as error:
        updates.install("abc")
    assert str(error.value) == "This list is no longer waiting. Check for new achievements again."
    await updates.check()
    with pytest.raises(UpdateError):
        updates.install(hashlib.sha256(b"something else").hexdigest())


async def test_a_checked_list_is_installed_once_and_not_after_going_back(store, clock):
    updates = _updates(store, clock, _newer(), BASE)
    newer = (await updates.check())["sha256"]
    updates.install(newer)
    with pytest.raises(UpdateError):
        updates.install(newer)  # a second time would push the bundled list out of the previous slot
    assert store.achievement_list("previous").source == "bundled"
    bundled = (await updates.check())["sha256"]
    updates.undo()
    with pytest.raises(UpdateError):
        updates.install(bundled)  # it was compared with the list that going back replaced


async def test_undo_goes_back_to_the_list_before_and_then_says_there_is_none(store, clock):
    updates = _updates(store, clock, _newer())
    updates.install((await updates.check())["sha256"])
    assert updates.undo()["version"] == "7.1"
    with pytest.raises(UpdateError) as error:
        updates.undo()
    assert str(error.value) == "There is no earlier list to go back to."


async def test_a_second_update_keeps_the_first_as_the_list_to_go_back_to(store, clock):
    updates = _updates(store, clock, _newer(), _newest())
    updates.install((await updates.check())["sha256"])  # list A, 7.2, at 08:00
    clock.advance(days=1)
    meta = updates.install((await updates.check())["sha256"])  # list B, 7.3, a day later
    assert (meta["version"], meta["stages"], meta["installed_at"]) == ("7.3", 12, "2026-10-14T08:00:00+00:00")
    assert updates.meta()["previous"] == {"version": "7.2", "stages": 11, "installed_at": "2026-10-13T08:00:00+00:00"}
    back = updates.undo()
    assert (back["version"], back["stages"], back["previous"]) == ("7.2", 11, None)
    assert updates.current().stage(99001) is not None and updates.current().stage(99002) is None
    with pytest.raises(UpdateError) as error:
        updates.undo()
    assert str(error.value) == "There is no earlier list to go back to."


async def test_only_the_bundled_list_byte_for_byte_carries_its_date(store, clock):
    updates = _updates(store, clock, _newer())
    updates.install((await updates.check())["sha256"])
    assert updates.meta()["dated"] is None
    assert updates.undo()["dated"] == "2026-09-23"  # the copy of the bundled list that install stored
    updated = ListUpdates(store, clock, _fetching(), bundled=_newer)  # a later Guild Board, with another snapshot
    assert updated.meta() == {"source": "bundled", "version": "7.1", "stages": 11,
                              "installed_at": "2026-10-13T08:00:00+00:00", "dated": None, "previous": None}


async def test_progress_survives_a_list_that_drops_its_achievement(store, clock):
    store.put_progress([ProgressRow(80001, "done", clock.now(), False, "", "player", clock.now())])
    updates = _updates(store, clock, _newer())
    updates.install((await updates.check())["sha256"])
    assert updates.current().stage(80001) is None and 80001 in store.progress()
    updates.undo()
    assert updates.current().stage(80001) is not None and store.progress()[80001].status == "done"


async def test_the_demo_offers_three_made_up_achievements():
    newer = await demo_fetch(bundled_raw())()
    added = [s for s in json.loads(newer)["0"]["achievements"] if isinstance(s, dict) and s["id"] >= 99901]
    assert [(s["id"], s["name"], s["ver"]) for s in added] == [
        (99901, "Demo achievement 1", "7.2"), (99902, "Demo achievement 2", "7.2"), (99903, "Demo achievement 3", "7.2"),
    ]
    assert {(s["desc"], s["reward"]) for s in added} == {("Made up for the demo.", 5)}


async def test_the_demo_list_is_checked_and_installed(store, clock):
    updates = ListUpdates(store, clock, demo_fetch(bundled_raw()))
    preview = await updates.check()
    assert preview["added"] == [
        {"id": 99900 + n, "name": f"Demo achievement {n}", "description": "Made up for the demo.", "version": "7.2",
         "category": "Wonders of the World"}
        for n in (1, 2, 3)
    ]
    assert preview["changed"] == preview["removed"] == []
    meta = updates.install(preview["sha256"])
    assert meta["stages"] == 1_857 and meta["version"] == "7.2"
    assert (await updates.check())["up_to_date"] is True  # the demo's download is the same every time


async def test_the_download_asks_netpolicy_for_the_one_address_and_at_most_2_mb(monkeypatch):
    calls, opened = [], []

    def refuse(*args, **options):  # no request can leave, however updates.py comes to call fetch_bytes
        opened.append(args)
        raise AssertionError("a test tried to send a request")

    def recorder(url, **options):
        calls.append((url, options))
        return BASE

    monkeypatch.setattr(netpolicy, "_open", refuse)
    monkeypatch.setattr(netpolicy, "fetch_bytes", recorder)
    assert await github_fetch()() == BASE
    assert calls == [(netpolicy.ACHIEVEMENT_LIST_URL, {"timeout": 30.0, "max_bytes": 2_000_000})]
    assert opened == []


class _Answer:
    """An answer from netpolicy._open, holding a body."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *exc) -> None:
        return None


# The live download from end to end, through netpolicy.fetch_bytes. Only netpolicy._open, which would send the
# request, is replaced, so these tests contact nothing.
async def test_the_live_download_is_one_plain_get_of_the_one_address(monkeypatch):
    sent = []

    def recorder(request, timeout):
        sent.append((request, timeout))
        return _Answer(BASE)

    monkeypatch.setattr(netpolicy, "_open", recorder)
    assert await github_fetch()() == BASE
    [(request, timeout)] = sent
    assert (request.full_url, request.get_method(), request.data, timeout) == (
        netpolicy.ACHIEVEMENT_LIST_URL, "GET", None, 30.0,
    )
    assert request.header_items() == [("User-agent", "TUSKFiZZY")]  # Guild Board's user agent and nothing else
    assert not request.has_header("Cookie")


async def test_a_live_download_over_2_mb_is_refused_with_its_sentence(store, clock, monkeypatch):
    monkeypatch.setattr(netpolicy, "_open", lambda request, timeout: _Answer(b" " * MAX_LIST_BYTES))
    assert len(await github_fetch()()) == MAX_LIST_BYTES  # 2 MB itself is let through
    monkeypatch.setattr(netpolicy, "_open", lambda request, timeout: _Answer(b" " * (MAX_LIST_BYTES + 1)))
    with pytest.raises(netpolicy.ResponseTooLarge):
        await github_fetch()()
    with pytest.raises(UpdateError) as error:
        await ListUpdates(store, clock, github_fetch(), bundled=lambda: BASE).check()
    assert str(error.value) == "The downloaded list is larger than 2 MB. The installed list stays."
