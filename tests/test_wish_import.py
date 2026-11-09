"""The import of the wish history on the player's press (the Pulls plan, Task 7).

Every link is made up, with a made-up key, and HoYoverse's history is a stand-in answering pages in the shape of
tests/pulls_samples.py: no test reads the player's game files or reaches HoYoverse.
"""
import asyncio
import functools
import gc
import json
import logging
import re
import sqlite3
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from guildboard import logging_setup, netpolicy
from guildboard.api.app import create_app
from guildboard.config import AppPaths, Settings, save_settings
from guildboard.core import Core
from guildboard.events import EventBus
from guildboard.logging_setup import setup_logging, shutdown_logging
from guildboard.pulls import gamecache, wishapi
from guildboard.pulls.gamecache import CacheProblem, WishLink
from guildboard.pulls.importer import ImportBusy, ImportNotReady, WishImporter, _Kept, demo_links
from guildboard.store import iso
from pulls_samples import (
    KEY_EXPIRED, KEY_INVALID, TOO_FREQUENT, History, cache_holding, made_up_game, record, wish_link,
)
from support import AccountSource, FakeClock, FakeSleep

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
UID = 700000001
KEY = "MADE-UP-KEY-0123456789"
FIRST = 1760000000000000000  # the ids of the made-up records count up from here, as HoYoverse's grow with time
KEY_SENTENCE = "Open Wish → History in the game, then press Import again."
TOO_FREQUENT_SENTENCE = "HoYoverse says there were too many requests. Wait a minute, then press Import again."
HELD_OPEN = "Close the game, then press Import again. The link from Wish → History stays valid for a day."
UNEXPECTED = "The import failed unexpectedly. See the log file."
EVERY_POOL = ("character", "weapon", "standard", "chronicled", "novice")


def records(gacha_type: str, count: int, first: int, uid: int = UID) -> list[dict[str, str]]:
    """`count` records of a wish type with the ids first + 1 to first + count, a minute apart, every tenth a 4★."""
    start = datetime(2026, 9, 1, 12, 0)
    return [
        record(
            first + n, gacha_type, f"{start + timedelta(minutes=first % 100_000 + n):%Y-%m-%d %H:%M:%S}",
            "Fischl" if n % 10 == 0 else "Cool Steel", "Character" if n % 10 == 0 else "Weapon",
            4 if n % 10 == 0 else 3, str(uid),
        )
        for n in range(1, count + 1)
    ]


def importer(store, history, links=None, bus=None, after=None, clock=None, ready=lambda: True):
    """An importer reading `links` (by default one link with KEY), whose pages come from `history` through
    wishapi.fetch_page, and whose pauses only move the fake clock; for a player who has given the first run's consent
    unless `ready` says otherwise."""
    clock = clock or FakeClock(NOW)
    links = [wish_link(KEY)] if links is None else links
    read_links = links if callable(links) else (lambda: list(links))
    fetch = functools.partial(wishapi.fetch_page, fetch=history)
    sleep = FakeSleep(clock)
    return WishImporter(store, clock, bus or EventBus(), read_links, fetch, sleep, after, ready=ready), sleep


def published(queue) -> list[dict]:
    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    return seen


def progress(events) -> list[tuple]:
    """The pulls_import events, as (state, pool, pages, found, message)."""
    return [
        (event["state"], event["pool"], event["pages"], event["found"], event["message"])
        for event in events if event["type"] == "pulls_import"
    ]


def asked(history) -> list[tuple[str, str]]:
    return [(query["gacha_type"], query["end_id"]) for query in history.asked]


# ---------------------------------------------------------------- the link

async def test_the_first_link_that_answers_is_used(store):
    history = History({"301": records("301", 3, FIRST)}, keys=["KEY-C-0123456789"])
    history.keys["KEY-B-0123456789"] = KEY_INVALID  # and KEY-A, which it does not know, is answered as expired
    links = [wish_link(key) for key in ("KEY-A-0123456789", "KEY-B-0123456789", "KEY-C-0123456789")]
    run, sleep = importer(store, history, links)
    await run.run()
    assert [query["authkey"] for query in history.asked] == ["KEY-A-0123456789", "KEY-B-0123456789"] + [
        "KEY-C-0123456789"
    ] * 6
    # Its first page counts as fetched: one request a type, the character event's first with the link's choice.
    assert asked(history)[2:] == [(gacha_type, "0") for gacha_type in ("301", "400", "302", "200", "500", "100")]
    assert sleep.calls == [0.5] * 7  # two before the third link, then one before each type after the first
    assert run.state()["state"] == "done" and store.wish_count(UID) == 3
    # With twelve links that are all too old, every one is tried, half a second apart, and the run says to open the
    # history again. At the check with the player the five tried first were all too old, and the one that would have
    # answered lay further on: no link is left untried since.
    history = History()
    run, sleep = importer(store, history, [wish_link(f"OLD-KEY-{n}-0123456789") for n in range(12)])
    await run.run()
    assert [query["authkey"] for query in history.asked] == [f"OLD-KEY-{n}-0123456789" for n in range(12)]
    assert sleep.calls == [0.5] * 11
    assert (run.state()["state"], run.state()["message"]) == ("failed", KEY_SENTENCE)
    assert store.wish_imports(1)[0].outcome == "failed"


async def test_a_press_tries_at_most_forty_links(store, caplog):
    # Each link tried is a request with an old key, half a second after the one before, and a cache can hold any number
    # of them. A press tries the forty last in the file at most; when none of those answers, the rest are left untried,
    # the log says so, and the import ends as failed with the sentence of links that were all refused: Open Wish →
    # History. A link left untried may be an old one too, and a sentence that said "unexpectedly" would hide that.
    history = History()
    run, sleep = importer(store, history, [wish_link(f"OLD-KEY-{n}-0123456789") for n in range(100)])
    with caplog.at_level("INFO", logger="guildboard.pulls.importer"):
        await run.run()
    assert [query["authkey"] for query in history.asked] == [f"OLD-KEY-{n}-0123456789" for n in range(40)]
    assert sleep.calls == [0.5] * 39
    assert (run.state()["state"], run.state()["message"]) == ("failed", KEY_SENTENCE)
    assert store.wish_imports(1)[0].outcome == "failed"
    lines = [record.getMessage() for record in caplog.records if record.name == "guildboard.pulls.importer"]
    assert lines[0] == (
        "The game's web cache holds 100 wish-history links. The 40 last in the file are tried, until one answers."
    )
    assert "None of the 40 links tried answered; the other 60 were not tried." in lines


async def test_a_wish_type_ends_at_the_most_pages(store, monkeypatch, caplog):
    # A wish type pages until it ends, and a history that answered full pages without end would page for ever. At 1,000
    # pages a type (20,000 wishes, more than any history holds) the import stops there, failed, and keeps nothing of that
    # pool, as a run cut short does.
    from guildboard.pulls import importer as importer_module

    assert importer_module.MOST_PAGES == 1000
    monkeypatch.setattr(importer_module, "MOST_PAGES", 3)
    history = History({"301": records("301", 100, FIRST)}, keys=[KEY])
    run, _ = importer(store, history)
    with caplog.at_level("WARNING", logger="guildboard.pulls.importer"):
        await run.run()
    assert asked(history) == [("301", "0"), ("301", str(FIRST + 81)), ("301", str(FIRST + 61))]
    assert (run.state()["state"], run.state()["message"]) == ("failed", UNEXPECTED)
    assert store.wish_count(UID) == 0
    assert "HoYoverse's wish history answered 3 full pages of wish type 301" in caplog.text


async def test_the_log_says_how_many_links_there_were_and_which_answered_never_a_key(store, caplog):
    # The log of an import tells how the link was found: how many the cache held, the time and place of each tried, and
    # which answered. The two last in the file are too old here, and the first answers.
    newer, answering, oldest = "NEWER-KEY-0123456789", "ANSWERING-KEY-0123456789", "OLDEST-KEY-0123456789"
    timeless = WishLink(wish_link(oldest).url.replace("&timestamp=1760000000", ""), {}, wish_link().cache_path)
    data = cache_holding(wish_link(answering, timestamp="1760050000"), timeless, wish_link(newer, timestamp="1760090000"))
    history = History({"301": records("301", 3, FIRST)}, keys=[answering])
    try:
        run, _ = importer(store, history, lambda: gamecache.links(data))
        with caplog.at_level("INFO", logger="guildboard.pulls.importer"):
            await run.run()
    finally:
        logging_setup.forget_secrets()  # gamecache registered each made-up key as a secret
    assert run.state()["state"] == "done"
    lines = [record.getMessage() for record in caplog.records if record.name == "guildboard.pulls.importer"]
    start = {key: data.rindex(b"https://", 0, data.index(f"authkey={key}".encode())) for key in (newer, answering, oldest)}
    assert lines[:5] == [
        "The game's web cache holds 3 wish-history links. They are tried the last in the file first, until one answers.",
        f"Trying link 1 of 3: its timestamp says 2025-10-10 09:53:20 UTC, and it lies at byte {start[newer]:,} of the "
        "cache.",
        f"Trying link 2 of 3: it has no timestamp, and it lies at byte {start[oldest]:,} of the cache.",
        f"Trying link 3 of 3: its timestamp says 2025-10-09 22:46:40 UTC, and it lies at byte {start[answering]:,} of the "
        "cache.",
        "Link 3 of 3 answered.",
    ]
    assert not any(key in line for line in lines for key in (newer, answering, oldest))
    # A cache with one link, which has no timestamp.
    caplog.clear()
    data = cache_holding(timeless)
    history = History({"301": records("301", 3, FIRST + 10)}, keys=[oldest])
    try:
        run, _ = importer(store, history, lambda: gamecache.links(data))
        with caplog.at_level("INFO", logger="guildboard.pulls.importer"):
            await run.run()
    finally:
        logging_setup.forget_secrets()
    lines = [record.getMessage() for record in caplog.records if record.name == "guildboard.pulls.importer"]
    assert lines[:3] == [
        "The game's web cache holds 1 wish-history link.",
        f"Trying link 1 of 1: it has no timestamp, and it lies at byte {data.index(b'https://'):,} of the cache.",
        "Link 1 of 1 answered.",
    ]


async def test_the_link_last_in_the_cache_is_tried_first_whatever_its_timestamp(store):
    # As at the check with the player: five links with a later timestamp lie earlier in the file and are too old; the
    # one written last, with an earlier timestamp, is tried first and the import runs on it, with no request spent on
    # the old ones.
    newest = "NEWEST-KEY-0123456789"
    links = [wish_link(f"OLDER-KEY-{n}-0123456789", timestamp="1760090000") for n in range(5)]
    links += [wish_link(newest, timestamp="1760089962")]
    data = cache_holding(*links)
    history = History({"301": records("301", 3, FIRST)}, keys=[newest])
    try:
        run, sleep = importer(store, history, lambda: gamecache.links(data))
        await run.run()
    finally:
        logging_setup.forget_secrets()  # gamecache registered each made-up key as a secret
    assert [query["authkey"] for query in history.asked] == [newest] * 6
    assert sleep.calls == [0.5] * 5  # no pause for a link given up
    assert run.state()["state"] == "done" and store.wish_count(UID) == 3


def test_the_demo_core_never_reads_the_game(tmp_path, clock):
    demo = Core(AppPaths(tmp_path / "demo"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set())
    live = Core(AppPaths(tmp_path / "live"), demo=False, clock=clock, source=AccountSource(), sleep=FakeSleep(clock),
                process_lister=lambda: set())
    try:
        assert demo.wish_import._read_links().wish[0].query["authkey"] == "DEMO-KEY-0123456789"
        assert demo.wish_import._read_links is not gamecache.read_links
        assert (live.wish_import._read_links, live.wish_import._fetch) == (gamecache.read_history_links, wishapi.fetch_page)
        [link] = demo_links()
        assert link.query["authkey"] == "DEMO-KEY-0123456789"
    finally:
        demo.store.close()
        live.store.close()


# ---------------------------------------------------------------- what an import fetches and keeps

async def test_the_first_import_fetches_every_type_page_by_page_and_keeps_every_wish(store):
    history = History({"301": records("301", 41, FIRST)}, keys=[KEY])
    bus = EventBus()
    heard = bus.subscribe()
    run, sleep = importer(store, history, bus=bus)
    assert run.state() == {
        "state": "idle", "last_at": None, "found": 0, "pages": 0, "pool": None, "message": None, "auto": None,
        "auto_stopped": None, "login": None, "every_minutes": None,
    }
    await run.run()
    # The character event in three pages, 20, 20 and 1, each after the last record of the page before; every other
    # type in one page, empty. Half a second between two requests.
    assert asked(history) == [
        ("301", "0"), ("301", str(FIRST + 22)), ("301", str(FIRST + 2)),
        ("400", "0"), ("302", "0"), ("200", "0"), ("500", "0"), ("100", "0"),
    ]
    assert all((query["size"], query["lang"], query["authkey"]) == ("20", "en-us", KEY) for query in history.asked)
    assert sleep.calls == [0.5] * 7
    events = published(heard)
    # While it runs, each event names the page within the wish type and the new wishes so far; the end, the run's pages.
    assert progress(events) == [
        ("running", None, 0, 0, None),
        ("running", "Character event", 1, 20, None),
        ("running", "Character event", 2, 40, None),
        ("running", "Character event", 3, 41, None),
        ("running", "Character event", 1, 41, None),
        ("running", "Weapon event", 1, 41, None),
        ("running", "Standard", 1, 41, None),
        ("running", "Chronicled", 1, 41, None),
        ("running", "Beginners' wish", 1, 41, None),
        ("done", None, 8, 41, "Imported 41 new wishes."),
    ]
    assert events[-1] == {"type": "pulls_changed"}
    kept = store.wishes(UID)
    [row] = store.wish_imports(1)
    assert len(kept) == 41 and {wish.import_id for wish in kept} == {row.id}
    assert (kept[0].id, kept[-1].id, kept[0].pool, kept[0].gacha_type) == (FIRST + 1, FIRST + 41, "character", "301")
    assert (row.outcome, row.uid, row.found, row.pages, row.message, row.pools) == ("ok", UID, 41, 8, None, EVERY_POOL)
    assert row.finished_at == NOW + timedelta(seconds=3)  # the pauses moved the fake clock 3.5 s; kept to the second
    assert run.state() == {
        "state": "done", "last_at": iso(row.finished_at), "found": 41, "pages": 8, "pool": None,
        "message": "Imported 41 new wishes.", "auto": {"until": iso(NOW + timedelta(hours=24)), "last_at": None},
        "auto_stopped": None, "login": None, "every_minutes": 30,
    }
    assert run.running is False


async def test_a_later_import_fetches_only_what_is_new(store):
    history = History({"301": records("301", 41, FIRST)}, keys=[KEY])
    run, _ = importer(store, history)
    await run.run()
    history.add("301", records("301", 3, FIRST + 41))
    history.add("302", records("302", 1, FIRST + 44))
    history.asked.clear()
    bus = EventBus()
    heard = bus.subscribe()
    run, _ = importer(store, history, bus=bus)
    await run.run()
    # The character event's first page holds a wish kept already: that type ends there. One request a type.
    assert asked(history) == [(gacha_type, "0") for gacha_type in ("301", "400", "302", "200", "500", "100")]
    assert progress(published(heard))[-1] == ("done", None, 6, 4, "Imported 4 new wishes.")
    assert store.wish_count(UID) == 45
    # One new wish, then none.
    history.add("200", records("200", 1, FIRST + 45))
    run, _ = importer(store, history)
    await run.run()
    assert run.state()["message"] == "Imported 1 new wish."
    run, _ = importer(store, history)
    await run.run()
    assert (run.state()["found"], run.state()["message"]) == (0, "Nothing new. The history is up to date.")
    assert store.wish_count(UID) == 46


async def test_a_record_answered_under_two_types_is_kept_once(store):
    both = records("301", 2, FIRST)
    history = History({"301": both, "400": both[:1]}, keys=[KEY])
    run, _ = importer(store, history)
    await run.run()
    assert (run.state()["found"], store.wish_count(UID)) == (2, 2)
    assert {wish.gacha_type for wish in store.wishes(UID)} == {"301"}


async def test_a_type_outside_the_six_is_kept_in_the_pool_of_the_query(store):
    history = History({"500": [record(FIRST + 1, "999", name="Something new", rank=5)]}, keys=[KEY])
    run, _ = importer(store, history)
    await run.run()
    [wish] = store.wishes(UID)
    assert (wish.gacha_type, wish.pool) == ("999", "chronicled")


async def test_an_import_that_cannot_read_the_cache_records_a_failed_run_and_keeps_the_history(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    first, _ = importer(store, history)
    await first.run()
    kept, [done] = store.wishes(UID), store.wish_imports(1)

    def held() -> list:
        raise CacheProblem(HELD_OPEN)

    history.asked.clear()
    bus = EventBus()
    heard = bus.subscribe()
    run, _ = importer(store, history, held, bus=bus)
    await run.run()
    assert history.asked == []
    assert store.wishes(UID) == kept
    events = published(heard)
    assert progress(events) == [("running", None, 0, 0, None), ("failed", None, 0, 0, HELD_OPEN)]
    assert events[-1] == {"type": "pulls_changed"}
    [row] = store.wish_imports(1)
    assert (row.outcome, row.uid, row.found, row.pages, row.message, row.pools) == (
        "failed", None, 0, 0, HELD_OPEN, ()
    )
    # The page still names the last import that ended well.
    assert run.state() == {
        "state": "failed", "last_at": iso(done.finished_at), "found": 0, "pages": 0, "pool": None, "message": HELD_OPEN,
        "auto": None, "auto_stopped": None, "login": None, "every_minutes": None,
    }
    # A failure of another kind while reading the files ends the run the same way, with the sentence for it.

    def broken() -> list:
        raise OSError("the disk is unreadable")

    run, _ = importer(store, history, broken)
    await run.run()
    assert (run.state()["state"], run.state()["message"]) == ("failed", UNEXPECTED)
    assert history.asked == [] and store.wishes(UID) == kept


async def test_too_many_requests_keeps_the_finished_types_and_the_next_import_fills_the_rest(store):
    history = History({"301": records("301", 25, FIRST), "302": records("302", 45, FIRST + 100)}, keys=[KEY])
    refused: list[dict] = []

    def once_too_many(query: dict) -> bytes | None:
        """HoYoverse refuses the weapon event's second page once: too many requests."""
        if query["gacha_type"] == "302" and query["end_id"] != "0" and not refused:
            refused.append(query)
            return TOO_FREQUENT
        return None

    history.refuse = once_too_many
    bus = EventBus()
    heard = bus.subscribe()
    run, _ = importer(store, history, bus=bus)
    await run.run()
    assert asked(history) == [("301", "0"), ("301", str(FIRST + 6)), ("400", "0"), ("302", "0"), ("302", str(FIRST + 126))]
    # The character event, fetched to its end, is kept; the weapon event keeps none of its pages.
    assert len(store.wishes(UID, "character")) == 25 and store.wishes(UID, "weapon") == []
    [row] = store.wish_imports(1)
    assert (row.outcome, row.found, row.pages, row.message, row.pools) == (
        "failed", 25, 4, TOO_FREQUENT_SENTENCE, ("character",)
    )
    assert progress(published(heard))[-1] == ("failed", None, 4, 25, TOO_FREQUENT_SENTENCE)
    assert (run.state()["state"], run.state()["message"]) == ("failed", TOO_FREQUENT_SENTENCE)
    # The next import fetches the weapon event again from its newest wish to its end: no gap is left.
    history.asked.clear()
    run, _ = importer(store, history)
    await run.run()
    assert [query for query in asked(history) if query[0] == "302"] == [
        ("302", "0"), ("302", str(FIRST + 126)), ("302", str(FIRST + 106)),
    ]
    assert [wish.id for wish in store.wishes(UID, "weapon")] == list(range(FIRST + 101, FIRST + 146))
    [row] = store.wish_imports(1)
    assert (row.outcome, row.found, row.pools) == ("ok", 45, EVERY_POOL)


async def test_an_expired_or_invalid_key_says_to_open_the_history_again(store):
    for answer in (None, KEY_INVALID):  # an unknown key is answered as expired (-101)
        history = History({"301": records("301", 3, FIRST)})
        if answer is not None:
            history.keys[KEY] = answer
        run, _ = importer(store, history)
        await run.run()
        assert (run.state()["state"], run.state()["message"]) == ("failed", KEY_SENTENCE)
        assert store.wish_count(UID) == 0
    # A key that grows too old during the run ends it there, keeping the pools finished before: a key is only tried
    # for another link before the first page.
    history = History({"301": records("301", 3, FIRST), "302": records("302", 3, FIRST + 10)}, keys=[KEY])
    history.refuse = lambda query: KEY_EXPIRED if query["gacha_type"] == "302" else None
    run, _ = importer(store, history)
    await run.run()
    assert (run.state()["state"], run.state()["message"]) == ("failed", KEY_SENTENCE)
    assert [wish.pool for wish in store.wishes(UID)] == ["character"] * 3
    assert store.wish_imports(1)[0].pools == ("character",)


async def test_an_import_of_another_uid_is_kept_apart(store):
    first = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, first)
    await run.run()
    other = History({"301": records("301", 2, FIRST + 50, uid=700000002)}, keys=["OTHER-KEY-0123456789"])
    run, _ = importer(store, other, [wish_link("OTHER-KEY-0123456789")])
    await run.run()
    assert store.wish_uids() == [700000002, UID]
    assert (store.wish_count(UID), store.wish_count(700000002)) == (3, 2)
    assert store.wish_imports(1)[0].uid == 700000002  # the run's uid: the first row answered


async def test_after_an_import_that_ended_well_after_is_called_with_its_uid(store, caplog):
    bus = EventBus()
    heard = bus.subscribe()
    calls = []

    async def after(uid):
        calls.append((uid, [event["type"] for event in published(heard)]))
        raise RuntimeError("the banners could not be read")  # logged; the import stays done

    run, _ = importer(store, History({"301": records("301", 3, FIRST)}, keys=[KEY]), bus=bus, after=after)
    with caplog.at_level(logging.ERROR):
        await run.run()
    [(uid, before)] = calls
    assert uid == UID and before[-2:] == ["pulls_import", "pulls_changed"]  # after the end, and the page told
    assert published(heard) == [{"type": "pulls_changed"}]  # and once more after it: the icons it read
    assert run.state()["state"] == "done" and "the banners could not be read" in caplog.text
    # Not after an import that failed.
    calls.clear()
    run, _ = importer(store, History(), bus=bus, after=after)
    await run.run()
    assert calls == []


async def test_the_state_when_idle_names_the_last_import_that_ended_well(store):
    run, _ = importer(store, History({"301": records("301", 3, FIRST)}, keys=[KEY]))
    await run.run()
    [row] = store.wish_imports(1)
    later, _ = importer(store, History())  # as after a start of Guild Board
    assert later.state() == {
        "state": "idle", "last_at": iso(row.finished_at), "found": 3, "pages": 6, "pool": None, "message": None,
        "auto": None, "auto_stopped": None, "login": None, "every_minutes": None,  # after a start nothing is kept: the key lives in memory alone
    }


CLOSED = "TUSKFiZZY closed before this import finished."


async def test_after_a_start_an_import_the_closing_cut_short_shows_how_it_ended(store):
    run, _ = importer(store, History({"301": records("301", 3, FIRST)}, keys=[KEY]))
    await run.run()
    [done] = store.wish_imports(1)
    # A newer import that Guild Board's closing cut short, after it kept the character event: the next start ends it
    # as failed, with the closing's sentence. The page says so, as it would have said how the import ended.
    cut = store.start_wish_import(NOW + timedelta(hours=1))
    store.keep_import_pool(cut, [], ["character"], UID)
    store.fail_running_wish_imports(NOW + timedelta(hours=2))
    later, _ = importer(store, History())  # as after a start of Guild Board
    assert later.state() == {
        "state": "failed", "last_at": iso(done.finished_at), "found": 0, "pages": 0, "pool": None, "message": CLOSED,
        "auto": None, "auto_stopped": None, "login": None, "every_minutes": None,
    }
    # Once a newer import ended well, it is idle again, with that one's figures.
    history = History({"301": records("301", 5, FIRST)}, keys=[KEY])
    await importer(store, history)[0].run()
    [newest] = store.wish_imports(1)
    assert importer(store, History())[0].state() == {
        "state": "idle", "last_at": iso(newest.finished_at), "found": 2, "pages": 6, "pool": None, "message": None,
        "auto": None, "auto_stopped": None, "login": None, "every_minutes": None,
    }
    # A newer import that failed for another reason, in an earlier session, leaves it idle, as before.
    await importer(store, History())[0].run()  # the key is too old
    assert store.wish_imports(1)[0].outcome == "failed"
    assert importer(store, History())[0].state()["state"] == "idle"


async def test_before_the_first_run_s_consent_nothing_is_read_or_recorded(store):
    # Ruling R21: until the player gives the first run's consent, a press is refused, and neither the game's files nor
    # HoYoverse's history is read.
    reads: list[str] = []
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    consented = [False]
    run, _ = importer(
        store, history, lambda: reads.append("links") or [wish_link(KEY)], ready=lambda: consented[0]
    )
    with pytest.raises(ImportNotReady, match="^Guild Board has not started yet. Read its first page, then press Continue.$"):
        await run.run()
    with pytest.raises(ImportNotReady):
        run.start()
    assert (reads, history.asked, store.wish_imports(1), run.running) == ([], [], [], False)
    consented[0] = True
    await run.run()
    assert reads == ["links"] and run.state()["state"] == "done"


async def test_a_second_press_while_one_runs_is_refused(store):
    release = threading.Event()
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])

    def slow(url: str) -> bytes:
        release.wait(10)
        return history(url)

    run, _ = importer(store, slow)
    task = asyncio.create_task(run.run())
    try:
        for _ in range(500):
            if run.running and run.state()["state"] == "running":
                break
            await asyncio.sleep(0.01)
        assert run.running
        with pytest.raises(ImportBusy, match="^An import is running.$"):
            await run.run()
        with pytest.raises(ImportBusy):
            run.start()
    finally:
        release.set()
        await task
    assert run.state()["state"] == "done" and run.running is False


async def test_a_press_while_the_banners_are_read_starts_the_next_import_and_keeps_it_running(store):
    # The banners and the icons are read after the import ended: a press then starts the next import, and the first
    # run's end does not make the second look finished, so that a third press is refused while the second runs.
    reading, read = asyncio.Event(), asyncio.Event()

    async def after(uid):
        reading.set()
        await read.wait()

    release = threading.Event()
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    calls = []

    def wire(url: str) -> bytes:
        calls.append(url)
        if len(calls) > 6:  # the second import waits
            release.wait(10)
        return history(url)

    run, _ = importer(store, wire, after=after)
    first = run.start()
    await asyncio.wait_for(reading.wait(), 5)
    assert run.running is False and run.state()["state"] == "done"
    second = run.start()
    for _ in range(500):
        if len(calls) > 6:
            break
        await asyncio.sleep(0.01)
    read.set()
    await first  # the first run's banners are read: it ends, the second still runs
    assert run.running is True
    with pytest.raises(ImportBusy):
        run.start()
    release.set()
    await second
    assert run.running is False and run.state()["state"] == "done"


async def test_closing_ends_every_import_task(store):
    reading = asyncio.Event()

    async def after(uid):
        reading.set()
        await asyncio.Event().wait()  # the banners take long to read

    release = threading.Event()
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    calls = []

    def wire(url: str) -> bytes:
        calls.append(url)
        if len(calls) > 6:
            release.wait(10)
        return history(url)

    run, _ = importer(store, wire, after=after)
    first = run.start()
    await asyncio.wait_for(reading.wait(), 5)
    second = run.start()
    try:
        await run.stop()
    finally:
        release.set()
    assert first.cancelled() and second.cancelled() and run.running is False


async def test_a_run_cut_short_still_credits_the_pools_it_kept(store):
    # Each pool's wishes and the pools the run has finished are written together: a run that Guild Board's closing cuts
    # short still credits the pools it kept, so that the history's window counts them.
    release = threading.Event()
    history = History({"301": records("301", 3, FIRST), "302": records("302", 2, FIRST + 10)}, keys=[KEY])
    asked: list[str] = []

    def wire(url: str) -> bytes:
        asked.append(url)
        if "gacha_type=302" in url:  # Guild Board closes while the weapon event is asked for
            release.wait(10)
        return history(url)

    run, _ = importer(store, wire)
    task = run.start()
    try:
        for _ in range(500):
            if any("gacha_type=302" in url for url in asked):
                break
            await asyncio.sleep(0.01)
        [row] = store.wish_imports(1)
        assert (row.outcome, row.pools, row.found, row.uid) == ("running", ("character",), 3, UID)
        await run.stop()
    finally:
        release.set()
    assert task.cancelled() and store.wish_count(UID) == 3
    store.fail_running_wish_imports(NOW + timedelta(minutes=1))  # as the next start does
    [row] = store.wish_imports(1)
    assert (row.outcome, row.pools, row.found) == ("failed", ("character",), 3)


async def test_a_page_that_does_not_move_past_the_last_ends_its_type(store, caplog):
    # An answer that does not go past the record asked for would have the import ask again and again: that type ends.
    history = History({"301": records("301", 25, FIRST)}, keys=[KEY])
    calls: list[str] = []

    def the_same_page(url: str) -> bytes:  # HoYoverse answering the newest page whatever end_id says
        calls.append(url)
        if len(calls) > 12:
            return TOO_FREQUENT  # where it would end without the check
        return history(re.sub(r"end_id=[0-9]+", "end_id=0", url))

    run, _ = importer(store, the_same_page)
    with caplog.at_level(logging.WARNING):
        await run.run()
    assert (run.state()["state"], run.state()["found"]) == ("done", 20)
    assert [url.count("gacha_type=301") for url in calls].count(1) == 2  # the first page, and the one that did not move
    assert "does not go past" in caplog.text


async def test_the_demos_new_wishes_are_two_hours_before_its_now(tmp_path):
    # The demo's history follows its now: so do the three wishes its import finds, two hours before it, in the server's
    # time; they are the newest by time as by id. A later start on the same folder finds none: they are made once.
    start = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    paths = AppPaths(tmp_path / "data")
    core = Core(paths, demo=True, demo_now=start, process_lister=lambda: set())
    try:
        await core.wish_import.run()
        assert core.wish_import.state()["found"] == 3
        newest = core.store.wishes_before(UID, None, 3)
        assert [wish.name for wish in newest] == ["Fischl", "Cool Steel", "Slingshot"]
        assert len({wish.time for wish in newest}) == 1 and newest[0].time.startswith("2026-11-20 11:00:")
        assert timedelta(0) <= newest[0].at - (start - timedelta(hours=2)) < timedelta(minutes=1)
        assert max(wish.at for wish in core.store.wishes(UID)) == newest[0].at
        await core.wish_import.run()
        assert core.wish_import.state()["found"] == 0
    finally:
        core.store.close()
    later = Core(paths, demo=True, demo_now=start + timedelta(days=3), process_lister=lambda: set())
    try:
        await later.wish_import.run()
        assert later.wish_import.state()["found"] == 0
    finally:
        later.store.close()


# ---------------------------------------------------------------- the link kept for a day (the second pass design, 3.5)

DAY = timedelta(hours=24)


async def _until(condition, what: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


async def test_a_good_import_keeps_its_link_for_a_day(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history)
    assert (run.state()["auto"], run.state()["auto_stopped"]) == (None, None)
    await run.run()
    # A day from the press, not from its end: the pauses moved the fake clock 2.5 s meanwhile.
    assert run.state()["state"] == "done"
    assert (run.state()["auto"], run.state()["auto_stopped"]) == ({"until": iso(NOW + DAY), "last_at": None}, None)
    # An import that failed keeps no link: none that no link answered, and none that failed after a link answered.
    for failing in (History(), History({"301": records("301", 3, FIRST)}, keys=[KEY])):
        failing.refuse = lambda query: TOO_FREQUENT if query["gacha_type"] == "302" else None
        run, _ = importer(store, failing)
        await run.run()
        assert run.state()["state"] == "failed"
        assert run.state()["auto"] is None


async def test_the_kept_link_imports_only_whats_new_and_reads_no_file(store):
    reads: list[str] = []

    def read_once() -> list:
        reads.append("the game's files")
        if len(reads) > 1:
            raise AssertionError("an update with the kept link read the game's files")
        return [wish_link(KEY)]

    history = History({"301": records("301", 41, FIRST)}, keys=[KEY])
    clock, bus = FakeClock(NOW), EventBus()
    heard = bus.subscribe()
    run, sleep = importer(store, history, read_once, bus=bus, clock=clock)
    await run.run()
    history.add("301", records("301", 3, FIRST + 41))
    history.asked.clear()
    sleep.calls.clear()
    published(heard)
    assert await run.auto_update() is True
    assert reads == ["the game's files"]  # the press read them; the update did not
    # One request a type, the first being the first type's first page: its page holds a wish kept already, so that
    # type ends there. All with the kept key, and the same pauses as a press.
    assert asked(history) == [(gacha_type, "0") for gacha_type in ("301", "400", "302", "200", "500", "100")]
    assert {query["authkey"] for query in history.asked} == {KEY}
    assert sleep.calls == [0.5] * 5
    assert store.wish_count(UID) == 44
    # The run is recorded like any other, and the page is told.
    [row] = store.wish_imports(1)
    assert (row.outcome, row.uid, row.found, row.pages, row.message, row.pools) == ("ok", UID, 3, 6, None, EVERY_POOL)
    events = published(heard)
    assert progress(events)[-1] == ("done", None, 6, 3, "Imported 3 new wishes.")
    assert events[-1] == {"type": "pulls_changed"}
    state = run.state()
    assert (state["state"], state["found"], state["message"]) == ("done", 3, "Imported 3 new wishes.")
    # The link lives on, to its first day's end; it records when it last updated.
    assert (state["auto"], state["auto_stopped"]) == ({"until": iso(NOW + DAY), "last_at": iso(clock.now())}, None)
    # Nothing new: still a run, and it still says when it updated.
    clock.advance(minutes=30)
    history.asked.clear()
    published(heard)
    assert await run.auto_update() is True
    assert asked(history) == [(gacha_type, "0") for gacha_type in ("301", "400", "302", "200", "500", "100")]
    assert (run.state()["found"], run.state()["message"]) == (0, "Nothing new. The history is up to date.")
    assert run.state()["auto"] == {"until": iso(NOW + DAY), "last_at": iso(clock.now())}
    assert {"type": "pulls_changed"} in published(heard)  # the page reads the time of the update again
    assert reads == ["the game's files"]


class _Watching(EventBus):
    """A bus that notes what the importer's block says at each pulls_changed: the pages read the model again then, and the
    top bar's line is made from that block."""

    def __init__(self) -> None:
        super().__init__()
        self.watched: WishImporter | None = None
        self.block_at_changes: list[tuple] = []

    def publish(self, kind: str, **data) -> None:
        super().publish(kind, **data)
        if kind == "pulls_changed" and self.watched is not None:
            state = self.watched.state()
            self.block_at_changes.append((state["state"], state["auto"], state["auto_stopped"]))


async def test_every_update_publishes_pulls_changed_with_its_block_final_whatever_it_found(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    clock, bus = FakeClock(NOW), _Watching()
    run, _ = importer(store, history, bus=bus, clock=clock)
    bus.watched = run
    await run.run()
    # An update that finds nothing new: the page is told, and what it reads then says when the link last updated.
    bus.block_at_changes.clear()
    clock.advance(minutes=30)
    assert await run.auto_update() is True
    assert (run.state()["found"], run.state()["message"]) == (0, "Nothing new. The history is up to date.")
    assert bus.block_at_changes == [("done", {"until": iso(NOW + DAY), "last_at": iso(clock.now())}, None)]
    # An update that new wishes ended: told too, with the same block.
    history.add("301", records("301", 2, FIRST + 3))
    clock.advance(minutes=30)
    assert await run.auto_update() is True
    assert run.state()["found"] == 2
    assert bus.block_at_changes[-1] == ("done", {"until": iso(NOW + DAY), "last_at": iso(clock.now())}, None)
    # An update that the link's refusal ended: told, with the link gone and the word that says why.
    history.keys[KEY] = KEY_EXPIRED
    assert await run.auto_update() is True
    assert bus.block_at_changes[-1] == ("failed", None, "expired")
    assert len(bus.block_at_changes) == 3  # one each: no run goes untold


async def test_a_refused_link_is_dropped_and_said_once(store):
    clock = FakeClock(NOW)
    for refusal in (KEY_EXPIRED, KEY_INVALID):  # -101 and -100
        history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
        run, _ = importer(store, history, clock=clock)
        await run.run()
        history.keys[KEY] = refusal
        history.asked.clear()
        assert await run.auto_update() is True
        assert [query["authkey"] for query in history.asked] == [KEY]  # the first page, and no more
        state = run.state()
        assert (state["auto"], state["auto_stopped"]) == (None, "expired")
        assert (state["state"], state["message"]) == ("failed", KEY_SENTENCE)
        assert store.wish_imports(1)[0].outcome == "failed"
        # Said once: later ticks do nothing, and ask nothing, until a new press.
        history.asked.clear()
        assert await run.auto_update() is False
        assert await run.auto_update() is False
        assert history.asked == []
        assert run.state()["auto_stopped"] == "expired"
    # A press starts over: it clears the word at once, even when it fails, and its good end keeps its own link.
    history.keys[KEY] = KEY_EXPIRED
    await run.run()
    assert (run.state()["state"], run.state()["auto_stopped"], run.state()["auto"]) == ("failed", None, None)
    history.keys[KEY] = None
    pressed = clock.now()
    await run.run()
    assert run.state()["state"] == "done" and run.state()["auto_stopped"] is None
    assert run.state()["auto"] == {"until": iso(pressed + DAY), "last_at": None}
    history.asked.clear()
    assert await run.auto_update() is True and {query["authkey"] for query in history.asked} == {KEY}


async def test_a_refusal_in_the_middle_of_an_update_drops_the_link_and_keeps_what_was_finished(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history)
    await run.run()
    history.add("301", records("301", 2, FIRST + 3))
    history.add("302", records("302", 2, FIRST + 10))
    history.refuse = lambda query: KEY_EXPIRED if query["gacha_type"] == "302" else None  # the key grows old meanwhile
    assert await run.auto_update() is True
    assert (run.state()["auto"], run.state()["auto_stopped"]) == (None, "expired")
    assert store.wish_count(UID) == 5 and store.wishes(UID, "weapon") == []  # the character event, finished, is kept
    assert store.wish_imports(1)[0].pools == ("character",)
    assert await run.auto_update() is False


async def test_an_update_that_fails_for_another_reason_keeps_the_link_for_the_next(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history)
    await run.run()
    history.add("301", records("301", 2, FIRST + 3))
    history.refuse = lambda query: TOO_FREQUENT
    assert await run.auto_update() is True
    assert (run.state()["state"], run.state()["message"]) == ("failed", TOO_FREQUENT_SENTENCE)
    assert (run.state()["auto"], run.state()["auto_stopped"]) == ({"until": iso(NOW + DAY), "last_at": None}, None)
    history.refuse = None
    assert await run.auto_update() is True
    assert (run.state()["state"], run.state()["found"]) == ("done", 2)
    assert run.state()["auto"]["last_at"] is not None


async def test_the_link_is_dropped_after_a_day(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    clock = FakeClock(NOW)
    run, _ = importer(store, history, clock=clock)
    await run.run()
    clock.advance(hours=24)
    history.asked.clear()
    # From the day's end it does not update: it drops the link, says nothing of an expiry, and asks nothing.
    assert await run.auto_update() is False
    assert (run.state()["auto"], run.state()["auto_stopped"]) == (None, None)
    assert await run.auto_update() is False
    assert history.asked == []


async def test_the_link_updates_up_to_the_last_second_of_its_day(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    clock = FakeClock(NOW)
    run, _ = importer(store, history, clock=clock)
    await run.run()
    clock.set(NOW + DAY - timedelta(seconds=1))
    history.asked.clear()
    assert await run.auto_update() is True
    assert {query["authkey"] for query in history.asked} == {KEY}


async def test_a_look_at_the_page_after_the_day_drops_the_link_too(store):
    # The page's own read drops the expired link: its key is not left in memory for a tick that may never come, as when
    # the game is not running. Nothing brings it back.
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    clock = FakeClock(NOW)
    run, _ = importer(store, history, clock=clock)
    await run.run()
    clock.advance(hours=24)
    assert (run.state()["auto"], run.state()["auto_stopped"]) == (None, None)
    clock.set(NOW)
    history.asked.clear()
    assert await run.auto_update() is False
    assert history.asked == []


async def test_a_press_and_a_tick_never_run_together(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    release, slow, calls = threading.Event(), [False], []

    def wire(url: str) -> bytes:
        calls.append(url)
        if slow[0]:
            release.wait(10)
        return history(url)

    run, _ = importer(store, wire)
    await run.run()  # the link is kept
    # A tick during a running press returns False, and asks HoYoverse for nothing.
    slow[0], before = True, len(calls)
    press = run.start()
    try:
        await _until(lambda: len(calls) > before, "the press did not ask HoYoverse")
        asked_so_far = len(calls)
        assert run.running and await run.auto_update() is False
        assert len(calls) == asked_so_far
    finally:
        release.set()
    await press
    # A press during a running tick is refused, as one during a press is: 409 "An import is running."
    release.clear()
    before = len(calls)
    tick = asyncio.create_task(run.auto_update())
    try:
        await _until(lambda: len(calls) > before, "the tick did not ask HoYoverse")
        assert run.running
        with pytest.raises(ImportBusy, match="^An import is running.$"):
            await run.run()
        with pytest.raises(ImportBusy, match="^An import is running.$"):
            run.start()
    finally:
        release.set()
    assert await tick is True
    assert run.running is False and run.state()["state"] == "done"


async def test_a_tick_does_nothing_without_a_link_or_the_consent(store):
    reads, consented = [], [True]
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history, lambda: reads.append("links") or [wish_link(KEY)], ready=lambda: consented[0])
    # No link is kept before a press, nothing is read, and no run is recorded.
    assert await run.auto_update() is False
    assert (reads, history.asked, store.wish_imports(1), run.running) == ([], [], [], False)
    await run.run()
    # The consent is not given: nothing is asked.
    consented[0] = False
    history.asked.clear()
    assert await run.auto_update() is False
    assert history.asked == [] and run.running is False
    consented[0] = True
    assert await run.auto_update() is True


async def test_the_banners_and_icons_are_read_again_after_an_update_only_when_it_found_wishes(store):
    seen: list[int | None] = []

    async def after(uid):
        seen.append(uid)

    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history, after=after)
    await run.run()
    assert seen == [UID]
    assert await run.auto_update() is True  # nothing new: no read of the announcements every half hour for nothing
    assert seen == [UID]
    history.add("301", records("301", 2, FIRST + 3))
    assert await run.auto_update() is True
    assert seen == [UID, UID]


async def test_drop_link_forgets_it(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history)
    await run.run()
    assert run.state()["auto"] is not None
    run.drop_link()
    assert (run.state()["auto"], run.state()["auto_stopped"]) == (None, None)
    history.asked.clear()
    assert await run.auto_update() is False
    assert history.asked == []
    run.drop_link()  # and again: nothing to forget


# ---------------------------------------------------------------- the key (H4)

async def _links_alive(kept: int = 0) -> list[WishLink]:
    """This test's links still in memory once the run is over: the event loop's step that ended a run holds the last page
    it awaited, and the thread that raised a refused page's error holds that error, until each of them goes on. A link
    something keeps stays after that. `kept` links are expected to stay (the one a good import keeps)."""
    for _ in range(100):
        await asyncio.sleep(0.01)
        gc.collect()
        alive = [link for link in gc.get_objects() if isinstance(link, WishLink) and link.query.get("authkey") in OWN_KEYS]
        if len(alive) <= kept:
            break
    return alive


# The keys of this test's own links: another test's run may still be ending in a thread of its own.
OWN_KEY, OTHER_OWN_KEY = "DROPPED-KEY-0123456789", "DROPPED-OTHER-KEY-0123456789"
OWN_KEYS = (OWN_KEY, OTHER_OWN_KEY)


async def test_the_runs_links_are_dropped_when_it_ends_but_the_one_a_good_import_keeps(store):
    # The links are read afresh for each press, as the game's files are; once the run ended, no link is left anywhere
    # but the one that answered, and that one only after an import that ended well: it is kept in memory for the day of
    # updates (the second pass design, section 5). drop_link() forgets it, and with it the key.
    history = History({"301": records("301", 1, FIRST)}, keys=[OTHER_OWN_KEY])
    run, _ = importer(store, history, lambda: [wish_link(OWN_KEY), wish_link(OTHER_OWN_KEY)])
    await run.run()
    assert run.state()["state"] == "done"
    assert [link.query["authkey"] for link in await _links_alive(kept=1)] == [OTHER_OWN_KEY]
    run.drop_link()
    assert await _links_alive() == []
    # None is kept after a run that no link answered, and none after one that failed later.
    for history in (History(), History({"302": records("302", 1, FIRST + 5)}, keys=[OWN_KEY])):
        history.refuse = lambda query: TOO_FREQUENT if query["gacha_type"] == "302" else None
        run, _ = importer(store, history, lambda: [wish_link(OWN_KEY), wish_link(OTHER_OWN_KEY)])
        await run.run()
        assert run.state()["state"] == "failed"
        assert await _links_alive() == []


async def test_the_kept_link_is_dropped_at_its_days_end_though_nothing_asks_for_it(store, monkeypatch):
    # The key is kept a day at most (the second pass design, section 5), also when no update comes to drop it and no page
    # looks at the import: the game was closed after the press, and Guild Board's window too. Here the day is short.
    monkeypatch.setattr("guildboard.pulls.importer.KEEP_LINK", timedelta(milliseconds=200))
    history = History({"301": records("301", 1, FIRST)}, keys=[OWN_KEY])
    run, _ = importer(store, history, lambda: [wish_link(OWN_KEY)])
    await run.run()
    assert [link.query["authkey"] for link in await _links_alive(kept=1)] == [OWN_KEY]
    await asyncio.sleep(0.3)  # the day is over, and nothing has asked for the link
    assert await _links_alive() == []
    assert (run.state()["auto"], run.state()["auto_stopped"]) == (None, None)


def test_the_key_never_reaches_the_log_the_database_or_an_answer(tmp_path, clock, monkeypatch):
    marker = "MARKER-KEY-0123456789-ABCDEFGH"
    # A made-up key as long as the game's, holding "/", "+" and "=", the marker at its start.
    key = f"{marker}/" + "".join(f"{n:03d}+part={n:03d}/" for n in range(60))
    assert len(key) > 600
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    made_up_game(home, key)  # the game's log and its cache, with a link whose key holds the marker
    history = History({"301": records("301", 25, FIRST), "302": records("302", 2, FIRST + 100)}, keys=[key])
    # The first press: HoYoverse answers the weapon event with an error that repeats the key.
    history.refuse = lambda query: json.dumps(
        {"retcode": -1, "message": f"bad request for {query['authkey']}", "data": None}
    ).encode() if query["gacha_type"] == "302" and not getattr(history, "repeated", False) else None

    def wire(request, timeout):  # netpolicy's requests: the history from the stand-in, nothing else
        if not request.full_url.startswith(wishapi.HISTORY_URL):
            raise urllib.error.URLError("the tests contact nothing else")  # the banners and the wiki, after
        return _Answer(history(request.full_url))

    monkeypatch.setattr(netpolicy, "_open", wire)
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    save_settings(paths.settings_path, Settings(risk_acknowledged=True))  # as the player who can reach Import has
    logging_setup.forget_secrets()
    setup_logging(paths.logs_dir)
    bodies: list[str] = []
    try:
        core = Core(paths, demo=False, clock=clock, source=AccountSource(), sleep=FakeSleep(clock),
                    process_lister=lambda: set())
        core.wish_import._sleep = FakeSleep(clock)
        app = create_app(core, 4321)
        heard = core.bus.subscribe()  # every event the runs publish, to the page as to anything else that listens
        with TestClient(app, base_url="http://127.0.0.1:4321") as client:
            client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
            for attempt in ("echoed", "ok"):
                started = client.post("/api/pulls/import")
                bodies.append(started.text)
                assert started.json() == {"started": True}
                _wait_for_the_import(core)
                history.repeated = True
            assert core.wish_import.state()["state"] == "done"
            # Every page's answer: the status, and the Pulls routes, the planner's model and history among them.
            pulls_pages = sorted(
                route.path for route in app.routes
                if route.path.startswith("/api/pulls") and "GET" in getattr(route, "methods", ())
            )
            assert {"/api/pulls", "/api/pulls/history"} <= set(pulls_pages)
            for url in ("/api/status", *pulls_pages):
                response = client.get(url)
                assert response.status_code == 200, url
                bodies.append(response.text)
            bodies.append(client.portal.call(_hello, app))
            bodies.append(json.dumps(core.wish_import.state()))
        events = published(heard)
        core.store.close()
    finally:
        shutdown_logging()
        logging_setup.forget_secrets()
    assert store_count(paths) == 27  # the wishes were kept
    assert {"pulls_import", "pulls_changed"} <= {event["type"] for event in events}
    bodies.extend(json.dumps(event) for event in events)
    log_text = (paths.logs_dir / "guildboard.log").read_text(encoding="utf-8")
    assert "retcode -1" in log_text  # HoYoverse's answer that repeated the key was logged, without its text
    assert marker not in log_text and "+part=" not in log_text
    assert all(marker not in body for body in bodies)
    for file in paths.data_dir.rglob("*"):
        if file.is_file():
            assert marker.encode() not in file.read_bytes(), file.name


class _Answer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, amount: int) -> bytes:
        return self._data[:amount]

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _wait_for_the_import(core) -> None:
    for _ in range(1000):
        if not core.wish_import.running:
            return
        threading.Event().wait(0.01)
    raise AssertionError("the import did not end")


async def _hello(app) -> str:
    """The hello of the event stream, read as the page reads it first."""
    [route] = [route for route in app.routes if route.path == "/api/events"]
    response = await route.endpoint()
    stream = response.body_iterator
    try:
        return await anext(stream)
    finally:
        await stream.aclose()


def store_count(paths: AppPaths) -> int:
    database = sqlite3.connect(paths.db_path)
    try:
        return int(database.execute("SELECT COUNT(*) FROM wishes").fetchone()[0])
    finally:
        database.close()


# ---------------------------------------------------------------- the history keys from the login

class _Mint:
    """Stands in for the source's mint_key: a link with KEY for the wish history, none for the game's history, each
    asked for remembered."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, auth_appid: str):
        self.asked.append(auth_appid)
        return gamecache.MintedLink(wish_link(KEY), UID, "os_euro") if auth_appid == "webview_gacha" else None


def _no_game_files() -> list:
    raise AssertionError("an update with a key from the login read the game's files")


async def test_a_key_from_the_login_updates_the_history_and_reads_no_game_file(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    clock = FakeClock(NOW)
    mint = _Mint()
    run, _ = importer(store, history, _no_game_files, clock=clock)
    assert await run.login_update(mint) is True
    assert mint.asked == ["webview_gacha"]  # without the ledgers' reads, no key for them is asked for
    assert store.wish_count(UID) == 3 and run.state()["state"] == "done"
    # The key is held in memory, apart from a press's link: the page says nothing of a link kept for a day.
    state = run.state()
    assert (state["auto"], state["auto_stopped"]) == (None, None)
    # The next tick reuses it, until it is 20 hours old.
    history.add("301", records("301", 2, FIRST + 3))
    clock.advance(minutes=30)
    assert await run.login_update(mint) is True
    assert mint.asked == ["webview_gacha"] and store.wish_count(UID) == 5
    clock.advance(hours=20)
    assert await run.login_update(mint) is True
    assert mint.asked == ["webview_gacha", "webview_gacha"]
    # Refused: it is dropped without a word to the player, and the next tick asks for a new one.
    history.keys.clear()
    assert await run.login_update(mint) is True
    assert run.state()["auto_stopped"] is None and run.state()["state"] == "failed"
    history.keys[KEY] = None
    assert await run.login_update(mint) is True
    assert mint.asked == ["webview_gacha"] * 3 and run.state()["state"] == "done"


async def test_a_press_link_is_used_before_a_key_from_the_login_and_nothing_runs_twice(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    mint = _Mint()
    run, _ = importer(store, history)
    await run.run()
    assert await run.login_update(mint) is True
    assert mint.asked == [] and run.state()["auto"] is not None  # the press's link, kept for its day
    # While an import runs, nothing is minted and nothing more runs.
    run.running = True
    assert await run.login_update(mint) is False
    run.running = False
    # Before the first run's consent, nothing either.
    unready, _ = importer(store, history, ready=lambda: False)
    assert await unready.login_update(mint) is False and mint.asked == []
    # A mint that fails, or gives no key, runs nothing; one that breaks leaves no import running.
    run.drop_link()

    async def none(auth_appid):
        return None

    assert await run.login_update(none) is False

    async def broken(auth_appid):
        raise RuntimeError("broken")

    with pytest.raises(RuntimeError):
        await run.login_update(broken)
    assert not run.running


async def test_the_page_is_told_whether_the_history_updates_itself_with_the_login_and_how_it_goes(store):
    login: list[str | None] = ["on"]
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history, _no_game_files)
    run._login = lambda: login[0]
    assert run.state()["login"] == "on"
    seen: list[str | None] = []

    async def watching_mint(auth_appid):
        seen.append(run.state()["login"])  # while the key is asked for, the page says so
        return await _Mint()(auth_appid)

    assert await run.login_update(watching_mint) is True
    assert seen == ["minting"] and run.state()["login"] == "on"
    # An update that did not end well is said, until one does.
    history.keys.clear()
    run.drop_link()
    assert await run.login_update(watching_mint) is True
    assert run.state()["login"] == "failed"
    history.keys[KEY] = None
    run.drop_link()
    assert await run.login_update(watching_mint) is True
    assert run.state()["login"] == "on"
    # No key at all is a failed update too; a refused login and no updates are the core's word.

    async def none(auth_appid):
        return None

    run.drop_link()
    assert await run.login_update(none) is False and run.state()["login"] == "failed"
    login[0] = "refused"
    assert run.state()["login"] == "refused"
    login[0] = None
    assert run.state()["login"] is None


def test_import_model_cadence_follows_the_login(store):
    run, _ = importer(store, History({}, keys=[KEY]), _no_game_files)
    for login, run_state, minutes in (
        ("on", "on", 30), ("on", "minting", 30), ("on", "failed", 30), ("refused", "on", None), (None, "on", None),
    ):
        run._login = lambda login=login: login
        run._login_run = run_state
        assert run.state()["every_minutes"] == minutes, (login, run_state)


def test_import_model_cadence_holds_while_a_link_from_a_press_is_kept(store):
    # The history updates itself every AUTO_EVERY while the game runs and a press's link lives, with no login at all.
    run, _ = importer(store, History({}, keys=[KEY]), _no_game_files)
    run._login = lambda: None
    assert run.state()["every_minutes"] is None
    run._kept = _Kept(wish_link(KEY), run._clock.now() + timedelta(hours=1))
    assert run.state()["every_minutes"] == 30
    run._clock.advance(hours=1)  # the link's day is over: it is dropped and nothing reads by itself
    assert run.state()["every_minutes"] is None and run.state()["auto"] is None


async def test_a_key_asked_for_while_the_login_changed_is_not_kept(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history, _no_game_files)
    assert await run.login_update(_Mint(), still=lambda: False) is False
    assert run._kept is None and history.asked == [] and not run.running


async def test_a_new_login_drops_the_keys_from_the_old_one_but_not_a_press_link(store):
    history = History({"301": records("301", 3, FIRST)}, keys=[KEY])
    run, _ = importer(store, history, _no_game_files)
    await run.login_update(_Mint())
    assert run._kept is not None
    run.drop_minted()
    assert run._kept is None
    pressed, _ = importer(store, history)
    await pressed.run()
    pressed.drop_minted()
    assert pressed._kept is not None


# ---------------------------------------------------------------- the log's mask: a key is a secret while a link holds it

def _masked(key: str) -> bool:
    """Whether the log masks `key`, which logging_setup holds as a secret."""
    return logging_setup.redact(key) != key


async def test_a_key_is_a_secret_from_its_first_request_until_its_link_is_let_go(store):
    # Only the keys that are tried are registered, each before its first request: a refused one is forgotten once the
    # run has ended, one never tried is never registered, and the one kept from the press is forgotten when it is
    # dropped. So the log's mask holds the keys in use and no more, however long Guild Board runs.
    refused, kept, untried = "REFUSED-KEY-0123456789", "KEPT-KEY-0123456789", "UNTRIED-KEY-0123456789"
    history = History({"301": records("301", 3, FIRST)}, keys=[kept])
    masked_when_sent: list[bool] = []

    def watching(url: str) -> bytes:
        masked_when_sent.append(_masked(parse_qs(urlsplit(url).query)["authkey"][0]))
        return history(url)

    logging_setup.forget_secrets()
    try:
        run, _ = importer(store, watching, [wish_link(key) for key in (refused, kept, untried)])
        await run.run()
        assert run.state()["state"] == "done"
        assert masked_when_sent and all(masked_when_sent)
        assert [_masked(key) for key in (refused, kept, untried)] == [False, True, False]
        run.drop_link()
        assert not _masked(kept)
        # A link that answered, in a run that failed later, is not kept: its key is no secret any more either.
        history.refuse = lambda query: TOO_FREQUENT if query["gacha_type"] == "302" else None
        run, _ = importer(store, watching, [wish_link(kept)])
        await run.run()
        assert run.state()["state"] == "failed" and not _masked(kept)
    finally:
        logging_setup.forget_secrets()


async def test_a_run_that_fails_unexpectedly_is_logged_while_the_keys_it_tried_are_still_masked(store, tmp_path):
    # The run holds the links it tried until it has ended: its own log of a failure it did not expect, whose text may
    # quote what it asked with (wishapi's errors never do, but a fetch is any callable), comes before it lets them go.
    key = "FAILING-KEY-0123456789"

    def failing(link, gacha_type, end_id, pool):
        raise RuntimeError(f"something unexpected with {link.query['authkey']}")

    logging_setup.forget_secrets()
    setup_logging(tmp_path / "logs")
    try:
        run, _ = importer(store, History(), [wish_link(key)])
        run._fetch = failing
        await run.run()
        assert run.state()["state"] == "failed" and not _masked(key)
    finally:
        shutdown_logging()
        logging_setup.forget_secrets()
    text = (tmp_path / "logs" / "guildboard.log").read_text(encoding="utf-8")
    assert "something unexpected with [redacted]" in text and key not in text


async def test_a_key_kept_from_a_press_stays_masked_while_a_later_press_lets_the_same_key_go(store):
    # The link kept from one press and a later press that finds the same key again each hold it: the later press's
    # refusal of it does not unmask the kept link's key, and keeping the same key again does not lose it.
    key = "SAME-KEY-0123456789"
    history = History({"301": records("301", 3, FIRST)}, keys=[key])
    logging_setup.forget_secrets()
    try:
        run, _ = importer(store, history, [wish_link(key)])
        await run.run()
        await run.run()  # the same key kept again in place of the first
        assert run.state()["auto"] is not None and _masked(key)
        history.keys[key] = KEY_EXPIRED
        await run.run()  # refused now: this press lets it go, the kept link still holds it
        assert run.state()["auto"] is not None and _masked(key)
        run.drop_link()
        assert not _masked(key)
    finally:
        logging_setup.forget_secrets()


async def test_a_key_from_the_login_is_masked_while_it_is_held_and_one_never_kept_is_forgotten(store):
    key = "MINTED-HELD-KEY-0123456789"

    async def mint(auth_appid):  # the source's mint_key, which registers the key as minted_link does
        return gamecache.minted_link(key, auth_appid, UID, "os_euro")

    history = History({"301": records("301", 3, FIRST)}, keys=[key])
    logging_setup.forget_secrets()
    try:
        run, _ = importer(store, history, _no_game_files)
        assert await run.login_update(mint) is True
        assert _masked(key)
        run.drop_minted()
        assert not _masked(key)
        assert await run.login_update(mint, still=lambda: False) is False  # asked for while the login changed
        assert run._kept is None and not _masked(key)
    finally:
        logging_setup.forget_secrets()
