"""The import of the wish history, on the player's press of Import (the Pulls design, sections 2.1 and 4; the plan's
calls, items 4 and 10).

A run reads the links the game cached (gamecache's two reads, in a thread) and tries them in turn, the last in the file
first, until one answers: the browser engine reuses freed space in its cache, so the newest link can lie anywhere in the
file. Every link is tried if need be, up to MOST_LINKS_TRIED, half a second apart: at the check with the player the five
tried first were all too old. The log says how many there were, the time and place of each tried, and which answered,
never a key. With the link that answered, it fetches every wish type page by page, newest first, half a second apart,
and keeps only what is new: a type ends at a page of fewer than 20 records, or at a record already kept, and after
MOST_PAGES at the latest.

A pool's new records are kept together once each of its types is fetched to its end (the character event has two, 301
and 400), and the run notes the pool as finished. A run that fails keeps the pools it finished; the pool it was in keeps
none of its pages, so the next import fetches that pool again from its newest record, and a pool's kept history never
has a gap, which would make a pity wrong.

The links hold the key that opens the player's history for about a day (H4). A press holds them in memory only while it
runs, and drops them when it ends, but for the link that answered when the import ended well: that one is kept, in
memory alone, for as long as HoYoverse accepts it and 24 hours from the press at most (the second pass design, section
5). With it `auto_update` imports what is new, as a press does and with the same pauses, reading no file of the game's:
the core asks for it every AUTO_EVERY while the game runs, and once when the game closes. The first answer that the key
is too old or not accepted ends the kept link, and `auto_stopped` says so until the next press; the day's end and Guild
Board's closing end it too (`drop_link`). Nothing here writes a key, or anything that holds one, anywhere. A key is
masked in the log from its link's first request until the link is let go: refused, not kept, or dropped
(gamecache.register_key and forget_key); a link never tried is never registered.

The demo's stand-ins are here too: a made-up link, and pages made from the demo's own wishes, with three new ones until
they are kept, so that the demo's first import finds three and the later ones nothing. The demo reads no file of the
game's and contacts nothing.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

from guildboard.clock import Clock
from guildboard.events import EventBus
from guildboard.gameday import server_tz
from guildboard.logging_setup import register_secret
from guildboard.pulls import GACHA_TYPES, POOL_LABELS, POOL_OF
from guildboard.ledger.api import LEDGER_EXPIRED, NO_LEDGER_LINK
from guildboard.ledger.importer import LedgerOutcome, call as ledger_call, fetch_ledgers, ledger_sentence
from guildboard.pulls.gamecache import (
    KEY, KEY_VERSION, NO_LINK, CacheProblem, HistoryLinks, MintedLink, WishLink, forget_key, made_at, register_key,
)
from guildboard.pulls.wishapi import (
    KEY_SENTENCE, PAGE_SIZE, PAUSE_SECONDS, TIME_FORMAT, KeyExpired, KeyInvalid, Page, Unexpected, WishApiError,
)
from guildboard.store import IMPORT_CUT_SHORT, Store, WishRow, iso

log = logging.getLogger(__name__)

BUSY = "An import is running."
NOT_STARTED = "Guild Board has not started yet. Read its first page, then press Continue."  # ruling R21
UNEXPECTED = "The import failed unexpectedly. See the log file."
NOTHING_NEW = "Nothing new. The history is up to date."
RECORDS_LOOKED_AT = 1000  # how far back the record of the imports is searched for the last one that ended well
# The most links of a kind a press tries, each a request with an old key half a second after the one before; and the most
# pages of one wish type, 20,000 wishes, more than any history holds (the ledger's MOST_PAGES too). Past either, the run
# ends as failed, and the log says why. Links left untried end it as links that were all refused do (Open Wish → History);
# a type with no end is unexpected.
MOST_LINKS_TRIED = 40
MOST_PAGES = 1000
KEEP_LINK = timedelta(hours=24)  # how long the link that answered a press is kept, at most, counted from the press
AUTO_EVERY = timedelta(minutes=30)  # how often the core asks for an update
# How long a key HoYoverse gave the HoYoLAB login is used, at most, counted from when it was given: they last about a day.
MINTED_LIFE = timedelta(hours=20)
MINTED_REFUSED = "HoYoverse did not accept the key it gave your HoYoLAB login. Guild Board asks for a new one at the next update."
# What the log says an update reads with: by whether the key came from the login.
_WHICH_WISH = {False: "the link kept from the last import", True: "a key HoYoverse gave the HoYoLAB login"}
_WHICH_LEDGER = {False: "the Feedback link kept from the last import", True: "a key HoYoverse gave the HoYoLAB login"}

# The types of each pool, in the order the import asks for them: a pool is finished when its last type has ended.
TYPES_OF_POOL: dict[str, tuple[str, ...]] = {
    pool: tuple(gacha_type for gacha_type in GACHA_TYPES if POOL_OF[gacha_type] == pool)
    for pool in dict.fromkeys(POOL_OF[gacha_type] for gacha_type in GACHA_TYPES)
}


def _life(minted: bool) -> float:
    """The seconds a kept link lives, at most: a key from the login's, or a press's link's."""
    return (MINTED_LIFE if minted else KEEP_LINK).total_seconds()


def found_sentence(found: int) -> str:
    """What the player is told at the end of an import that ended well."""
    if found == 0:
        return NOTHING_NEW
    return "Imported 1 new wish." if found == 1 else f"Imported {found:,} new wishes."


class ImportBusy(Exception):
    """Import was pressed while an import runs."""

    def __init__(self) -> None:
        super().__init__(BUSY)


class ImportNotReady(Exception):
    """Import was pressed before the player gave the first run's consent (ruling R21)."""

    def __init__(self) -> None:
        super().__init__(NOT_STARTED)


@dataclass
class _Kept:
    """The link that answered the last press that ended well, kept in memory for the updates of its day. Its repr leaves
    the key out, as the link's does."""

    link: WishLink
    until: datetime  # a day from the press: from then on the link is dropped
    last_at: datetime | None = None  # when the last update that ended well ended
    uid: int | None = None  # a self-service link's account, which GetUserInfo named
    region: str | None = None  # and its server
    minted: bool = False  # a key HoYoverse gave the HoYoLAB login, not a link a press found


@dataclass
class _Run:
    """What a run has done so far. It holds rows, never a link."""

    import_id: int
    started: datetime  # when the run began: the kept link's day counts from here
    uid: int | None = None  # the account of the first record answered
    pages: int = 0  # the pages fetched in the whole run
    kept: int = 0  # the new wishes kept, of the pools finished
    pools: list[str] = field(default_factory=list)  # the pools finished, in the order they were
    held: dict[str, dict[int, WishRow]] = field(default_factory=dict)  # by the pool asked for: new rows not kept yet
    seen: set[int] = field(default_factory=set)  # the ids held, whichever pool holds them

    @property
    def found(self) -> int:
        """The new wishes of the run so far: those kept, and those held."""
        return self.kept + sum(len(rows) for rows in self.held.values())


class WishImporter:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        bus: EventBus,
        read_links: Callable[[], list[Any]] | None,
        fetch: Callable[..., Any] | None,
        sleep: Callable[[float], Awaitable[None]],
        after: Callable[[int | None], Awaitable[None]] | None,
        *,
        ready: Callable[[], bool],
        ledger_fetch: Callable[..., Any] | None = None,
        ledger_user: Callable[..., Any] | None = None,
        login: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._store = store
        # Whether the histories update themselves with keys HoYoverse gives the HoYoLAB login (the core's refresh): None
        # when they do not, "refused" once HoYoverse refused the login, else "on". While it is "on", the importer says how
        # its own updates with the login go: "minting" while the keys are asked for, "failed" after one that did not end
        # well, "on" again after one that did. The pages say so beside Import.
        self._login = login
        self._login_run = "on"
        # The game's own history logs (the ledger design, section 0): one page of a kind (ledger.api.fetch_page) and
        # the account a self-service link belongs to (ledger.api.fetch_user); the demo's stand-ins are in
        # ledger.demo. Without them, an import reads the wishes alone.
        self._ledger_fetch = ledger_fetch
        self._ledger_user = ledger_user
        self._ledger_shown: dict[str, Any] | None = None
        # The self-service link that answered the last press, kept as the wish link is, apart from it: either may be
        # refused, run out or be missing without the other.
        self._kept_ledger: _Kept | None = None
        self._ledger_day_end: asyncio.TimerHandle | None = None
        self._clock = clock
        self._bus = bus
        # Whether the player has given the first run's consent (ruling R21): until then an import is refused, and nothing
        # of the game's is read. The demo's, which reads nothing of the game's and contacts nothing, needs none.
        self._ready = ready
        # The two reads of the game's files (gamecache.read_links): the links in the web cache, the last in the file
        # first, which the import tries one by one until one answers. The demo's stand-in is demo_links.
        self._read_links = read_links
        self._fetch = fetch  # one page of the history (wishapi.fetch_page); the demo's stand-in is demo_pages
        self._sleep = sleep  # the pause between two requests
        self._after = after  # called with the account's uid once an import ended well: the banners and the icons
        self.running = False
        self._shown: dict[str, Any] | None = None  # the last state shown of this session's run, while it runs and after
        # The runs started by a press, until they end: one importing at most, and one more still reading the banners.
        self._tasks: set[asyncio.Task[None]] = set()
        # The link that answered the last press that ended well, for the updates of its day (None before one, and after
        # its day, a refusal or the closing); and, until the next press, that HoYoverse refused it ("expired").
        self._kept: _Kept | None = None
        self._auto_stopped: str | None = None
        # Drops the kept link at its day's end, should no update and no look at the page come to drop it then.
        self._day_end: asyncio.TimerHandle | None = None
        # The links the run in hand tried, each masked in the log from its first request on (gamecache.register_key).
        # The run holds them until it has ended and logged whatever it logs, then lets them go, but for the ones kept.
        self._tried: list[WishLink] = []

    # ------------------------------------------------------------------ what the page shows
    def state(self) -> dict[str, Any]:
        """The Pulls model's import block: this session's run, running or how it ended; else, after a start, the newest
        import when Guild Board's closing cut it short, failed with the sentence recorded for it; else idle. With when the
        last import that ended well finished, None before any; and the updates of the link kept for the day: `auto`
        while one lives (until when, and when it last updated, None before an update), `auto_stopped` once HoYoverse
        refused it, until the next press. A key from the login is no link of a press's: `auto` leaves it out, and `login`
        says whether the history updates itself with the login, with `every_minutes` how often the history updates itself (None when nothing does)."""
        runs = self._store.wish_imports(RECORDS_LOOKED_AT)
        last = next((row for row in runs if row.outcome == "ok" and row.finished_at), None)
        last_at = iso(last.finished_at) if last is not None and last.finished_at is not None else None
        kept = self._live()
        login = self.login_state()
        auto = {
            "auto": None if kept is None or kept.minted else {
                "until": iso(kept.until), "last_at": iso(kept.last_at) if kept.last_at is not None else None,
            },
            "auto_stopped": self._auto_stopped,
            "login": login,
        }
        # The history reads itself every AUTO_EVERY with the login's keys or while a press's link lives (the core asks
        # for an update then), and not otherwise.
        reads_itself = login in ("on", "minting", "failed") or auto["auto"] is not None
        auto["every_minutes"] = int(AUTO_EVERY.total_seconds() // 60) if reads_itself else None
        if self._shown is not None:
            return {**self._shown, "last_at": last_at, **auto}
        newest = runs[0] if runs else None
        if newest is not None and newest.outcome == "failed" and newest.message == IMPORT_CUT_SHORT:
            return {
                "state": "failed", "last_at": last_at, "found": newest.found, "pages": newest.pages, "pool": None,
                "message": newest.message, **auto,
            }
        return {
            "state": "idle", "last_at": last_at, "found": last.found if last is not None else 0,
            "pages": last.pages if last is not None else 0, "pool": None, "message": None, **auto,
        }

    def ledger_state(self) -> dict[str, Any]:
        """The game's own history logs in this session: running (the kind being read and the new entries of it so far)
        or how the last run ended, else idle; when the last import of a kind that ended well ended; and while a
        self-service link is kept, until when, and when its last update ended."""
        last = self._store.last_ledger_import()
        kept = self._live_ledger()
        shown = self._ledger_shown or {"state": "idle", "kind": None, "found": 0, "message": None}
        return {
            **shown, "last_at": iso(last) if last is not None else None,
            "auto": None if kept is None or kept.minted else {
                "until": iso(kept.until), "last_at": iso(kept.last_at) if kept.last_at is not None else None,
            },
        }

    def ledger_import_state(self) -> dict[str, Any]:
        """The Ledger page's import block: state() with the ledger block, whose `every_minutes` also holds while the
        Feedback link kept from a press lives (auto_update reads it on the same AUTO_EVERY)."""
        state = {**self.state(), "ledger": self.ledger_state()}
        if state["every_minutes"] is None and state["ledger"]["auto"] is not None:
            state["every_minutes"] = int(AUTO_EVERY.total_seconds() // 60)
        return state

    def _show_ledger(self, state: str, kind: str | None, found: int, message: str | None) -> None:
        self._ledger_shown = {"state": state, "kind": kind, "found": found, "message": message}
        # The block whole, under "ledger": the bus's own `kind` is the event's type.
        self._bus.publish("ledger_import", ledger=dict(self._ledger_shown))

    def _show(self, state: str, pool: str | None, pages: int, found: int, message: str | None) -> None:
        """Tell the pages how the import goes: at its start, after every page, and at its end. While it runs, `pages` is
        the page within the wish type, and `pool` that type's pool by its label; at the end, the run's pages."""
        self._shown = {"state": state, "pool": pool, "pages": pages, "found": found, "message": message}
        self._bus.publish("pulls_import", state=state, pool=pool, pages=pages, found=found, message=message)

    # ------------------------------------------------------------------ the press
    def start(self) -> asyncio.Task[None]:
        """Start an import as a task, on the player's press. It counts as running at once, so that a second press, even
        in the same instant, is refused with ImportBusy. Before the first run's consent, ImportNotReady."""
        self._reserve()
        task = asyncio.create_task(self._run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run(self) -> None:
        """An import, from the reads of the game's files to its end. ImportBusy while one runs, ImportNotReady before the
        first run's consent."""
        self._reserve()
        await self._run()

    async def stop(self) -> None:
        """At Guild Board's closing: the runs still going end there, and the next start records an import cut short as
        failed."""
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.running = False

    def ensure_pressable(self) -> None:
        """Raise what a press would: ImportNotReady before the first run's consent, ImportBusy while a run goes."""
        if not self._ready():
            raise ImportNotReady()
        if self.running:
            raise ImportBusy()

    def pressed(self) -> None:
        """An accepted press starts over, whichever way it is read: whatever stopped the updates before is said no more."""
        self._auto_stopped = None

    def _reserve(self) -> None:
        self.ensure_pressable()
        self.running = True
        self.pressed()

    def start_with_link(self, link: WishLink, expected_uid: int) -> asyncio.Task[None]:
        """Import a supplied, already validated wish-history link for one named game account."""
        if isinstance(expected_uid, bool) or not isinstance(expected_uid, int) or not 0 < expected_uid < 2**63:
            raise ValueError("Choose a valid Genshin UID.")
        self._reserve()
        self._try_link(link)
        kept = _Kept(link, self._clock.now() + KEEP_LINK, uid=expected_uid)
        self._keep_link(link, kept.until)
        task = asyncio.create_task(self._run(kept=kept))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ the updates of the kept link
    async def auto_update(self) -> bool:
        """Import what is new with the link kept from the last press, as a press does but reading no file of the game's:
        its first request is the first wish type's first page. True when it ran, a run that failed included. False, and
        nothing done, when no link is kept, when the link's day is over (it is dropped), when an import is running, or
        before the first run's consent. The core calls it every AUTO_EVERY while the game runs, and once when it closes."""
        kept, ledger = self._live(), self._live_ledger()
        if (kept is None and ledger is None) or self.running or not self._ready():
            return False
        self.running = True
        if kept is not None:
            log.info("Updating the wish history with %s.", _WHICH_WISH[kept.minted])
        if ledger is not None:
            log.info("Updating the game's history with %s.", _WHICH_LEDGER[ledger.minted])
        await self._run(kept, auto=True)
        return True

    def login_state(self) -> str | None:
        """The pages' word on the updates with the login: None without them, "refused", or how the importer's own go
        ("on", "minting" or "failed")."""
        login = self._login()
        return self._login_run if login == "on" else login

    def _set_login_run(self, state: str) -> None:
        if state != self._login_run:
            self._login_run = state
            self._bus.publish("pulls_changed")  # the pages read the model again, and say it

    async def login_update(
        self, mint: Callable[[str], Awaitable[MintedLink | None]], still: Callable[[], bool] = lambda: True
    ) -> bool:
        """Update both histories with keys HoYoverse gives the HoYoLAB login (`mint`, the source's mint_key), reading no
        file of the game's. A link kept from a press is used while it lives; else a key from the login, asked for when
        none is held and reused until MINTED_LIFE has passed or HoYoverse refuses it. The game history's entries are kept
        for the account the key was given for. Then the run is auto_update's. False, and nothing done, while an import
        runs, before the first run's consent, and when no key could be had. When `still` says the login changed while a
        key was asked for, nothing is kept and nothing runs. A LoginNeeded from `mint` is raised, with no import left
        running."""
        if self.running or not self._ready():
            return False
        self.running = True  # reserved while the keys are asked for, so that a press waits for this run
        self._set_login_run("minting")
        try:
            now = self._clock.now()
            if self._live() is None:
                minted = await mint("webview_gacha")
                if not still():
                    return self._login_changed(minted)
                if minted is not None:
                    self._keep_link(minted.link, now + MINTED_LIFE, minted=True)
            if self._ledger_fetch is not None and self._live_ledger() is None:
                minted = await mint("csc")
                if not still():
                    return self._login_changed(minted)
                if minted is not None:
                    self._keep_ledger_link(minted.link, minted.uid, minted.region, now + MINTED_LIFE, minted=True)
        except BaseException:
            self._set_login_run("on")  # the core says what came of it
            raise
        finally:
            self.running = False
        wishes, ledgers = self._live() is not None, self._live_ledger() is not None
        self._login_run = "on"  # the keys are had: the run's own progress is said from here
        ran = await self.auto_update()
        failed = not ran or (
            (wishes and (self._shown or {}).get("state") == "failed")
            or (ledgers and (self._ledger_shown or {}).get("state") == "failed")
        )
        if ran and not failed:
            self.pressed()  # the updates go well again, with the login's key: the word that a link was refused is stale
        self._set_login_run("failed" if failed else "on")
        return ran

    def _login_changed(self, unkept: MintedLink | None) -> bool:
        """The login changed while a key was asked for: no key of this ask is kept, and nothing runs."""
        log.info("The HoYoLAB login changed while a history key was asked for: nothing of that ask is kept.")
        if unkept is not None:
            forget_key(unkept.link)  # never kept, so masked no more
        self.drop_minted()
        self._set_login_run("on")
        return False

    def drop_minted(self) -> None:
        """Forget the keys HoYoverse gave the login, not a press's links: at a login or a logout."""
        if self._kept is not None and self._kept.minted:
            self.drop_wish_link()
        if self._kept_ledger is not None and self._kept_ledger.minted:
            self.drop_ledger_link()

    def drop_link(self) -> None:
        """Forget both kept links, and their keys: at Guild Board's closing."""
        self.drop_wish_link()
        self.drop_ledger_link()

    def drop_wish_link(self) -> None:
        """Forget the kept wish link, and its key, which the log masks no more. A day's end and a refusal drop it
        too."""
        kept, self._kept = self._kept, None
        if kept is not None:
            forget_key(kept.link)
        if self._day_end is not None:
            self._day_end.cancel()
            self._day_end = None

    def drop_ledger_link(self) -> None:
        """Forget the kept self-service link, and its key, which the log masks no more. A day's end and a refusal drop
        it too."""
        kept, self._kept_ledger = self._kept_ledger, None
        if kept is not None:
            forget_key(kept.link)
        if self._ledger_day_end is not None:
            self._ledger_day_end.cancel()
            self._ledger_day_end = None

    def _keep_ledger_link(
        self, link: WishLink, uid: int, region: str | None, until: datetime, minted: bool = False
    ) -> None:
        self.drop_ledger_link()
        self._kept_ledger = _Kept(link, until, uid=uid, region=region, minted=minted)
        self._hand_over(link)  # a run's hold passes to it; a key from the login is held from its mint on
        self._ledger_day_end = asyncio.get_running_loop().call_later(_life(minted), self.drop_ledger_link)

    def _live_ledger(self) -> _Kept | None:
        """The kept self-service link while its day lasts."""
        kept = self._kept_ledger
        if kept is not None and self._clock.now() >= kept.until:
            log.info("The Feedback link kept from the last import is dropped: its day is over.")
            self.drop_ledger_link()
            kept = None
        return kept

    def _keep_link(self, link: WishLink, until: datetime, minted: bool = False) -> None:
        """Keep the link that answered a press, or a key from the login, in place of any kept before, until `until`. Then
        it is dropped even when nothing asks for it, the game closed and no page open: the key is not kept past its day."""
        self.drop_wish_link()
        self._kept = _Kept(link, until, minted=minted)
        self._hand_over(link)  # a run's hold passes to it; a key from the login is held from its mint on
        self._day_end = asyncio.get_running_loop().call_later(_life(minted), self._day_over)

    def _day_over(self) -> None:
        self._day_end = None
        if self._kept is not None:
            log.info("The link kept from the last import is dropped: its day is over.")
        self.drop_wish_link()

    def _live(self) -> _Kept | None:
        """The kept link while its day lasts; from the day's end it is dropped, so that the key is not kept past it."""
        kept = self._kept
        if kept is not None and self._clock.now() >= kept.until:
            log.info("The link kept from the last import is dropped: its day is over.")
            self.drop_wish_link()
            kept = None
        return kept

    # ------------------------------------------------------------------ the run
    async def _run(self, kept: _Kept | None = None, auto: bool = False) -> None:
        """The import, then, once it ended well, the banners and the icons read again by `after`. A press counts as an
        import running until the import ends, not while those are read. An update with the kept link is the same run, but
        for the game's files, which it reads none of, and `after`, which it calls only when it kept new wishes.

        The wishes come first, then the game's own history logs, each on its own: a failure, or a missing link, of one
        never fails the other, and each says its own sentence (the ledger design, section 0)."""
        run: _Run | None = None
        failure: str | None = UNEXPECTED
        ledger_links: list[WishLink] = []
        started = self._clock.now()
        try:
            if not auto or kept is not None:
                try:
                    run = _Run(self._store.start_wish_import(started), started)
                    self._show("running", None, 0, 0, None)
                    if kept is None:
                        failure, ledger_links = await self._fetch_all(run)
                    else:
                        failure = await self._fetch_kept(run, kept)
                except Exception:  # anything else: the log says what, the player that it failed
                    log.exception("the import of the wish history stopped unexpectedly")
                    failure = UNEXPECTED
                if kept is not None and failure is None:
                    kept.last_at = self._clock.now()
                self._end(run, failure)
            await self._ledgers(ledger_links, auto, started)
        finally:
            self._let_go_of_tried()
            self.running = False
        if failure is None and run is not None and self._after is not None and (kept is None or run.kept):
            try:
                await self._after(run.uid)
            except Exception:  # the import itself ended well
                log.exception("the banners or the icons could not be read after the import")
            self._bus.publish("pulls_changed")

    def _try_link(self, link: WishLink) -> None:
        """A link about to be tried: its key is masked in the log from its first request on, and the run holds it."""
        register_key(link)
        self._tried.append(link)

    def _hand_over(self, link: WishLink) -> None:
        """The run's hold on a link it tried passes to the kept link: its key stays masked until that is dropped."""
        for index, tried in enumerate(self._tried):
            if tried is link:
                del self._tried[index]
                return

    def _let_go_of_tried(self) -> None:
        """The run has ended: it lets go of the links it tried and did not keep, and their keys are masked no more but
        where another holder has the same key (a link kept from an earlier press)."""
        tried, self._tried = self._tried, []
        for link in tried:
            forget_key(link)

    async def _fetch_all(self, run: _Run) -> tuple[str | None, list[WishLink]]:
        """Every wish type, page by page: None when every pool was finished, else the sentence of what stopped it; and
        the self-service links the same two reads found, for the ledgers after. When every pool was, the wish link that
        answered is kept for the day."""
        try:
            found = await asyncio.to_thread(self._read_links)
        except CacheProblem as problem:
            return str(problem), []
        if not isinstance(found, HistoryLinks):  # a reader of the wish links alone
            found = HistoryLinks(list(found), [])
        ledger = list(found.ledger)
        if not found.wish:
            return found.wish_problem or NO_LINK, ledger
        try:
            chosen = await self._choose(list(found.wish))
            del found  # only the link that answered is held from here on
            if chosen is None:
                return KEY_SENTENCE, ledger
            link = chosen[0]
            await self._fetch_types(run, *chosen)
        except WishApiError as error:
            return str(error), ledger
        self._keep_link(link, run.started + KEEP_LINK)
        log.info("The link that answered is kept in memory, to update with it for up to a day.")
        return None, ledger

    # ------------------------------------------------------------------ the game's own history logs
    async def _ledgers(self, links: list[WishLink], auto: bool, started: datetime) -> None:
        """The ledgers' part of a run: with the kept self-service link on an update, else with the first of `links`
        that answers. Never raises: whatever goes wrong is the ledgers' own sentence."""
        if self._ledger_fetch is None or self._ledger_user is None:
            return
        try:
            if auto:
                kept = self._live_ledger()
                if kept is None or kept.uid is None:
                    return
                outcome = await self._read_ledgers(kept.link, kept.uid, kept.region)
                if outcome is not None and outcome.refused:
                    log.info("HoYoverse no longer accepts %s: it is dropped.", _WHICH_LEDGER[kept.minted])
                    self.drop_ledger_link()
                    if kept.minted:
                        self._show_ledger("failed", None, 0, MINTED_REFUSED)
                elif outcome is not None and not outcome.failed:
                    kept.last_at = self._clock.now()
                return
            if not links:
                self._show_ledger("failed", None, 0, NO_LEDGER_LINK)
                return
            self._show_ledger("running", None, 0, None)
            try:
                chosen = await self._choose_ledger(links)
            except WishApiError as error:
                self._show_ledger("failed", None, 0, str(error))
                return
            del links
            if chosen is None:
                self._show_ledger("failed", None, 0, LEDGER_EXPIRED)
                return
            link, uid, region = chosen
            outcome = await self._read_ledgers(link, uid, region)
            if outcome is not None and not outcome.refused:
                self._keep_ledger_link(link, uid, region, started + KEEP_LINK)
                log.info("The Feedback link that answered is kept in memory, to update with it for up to a day.")
        except Exception:
            log.exception("the import of the game's history stopped unexpectedly")
            self._show_ledger("failed", None, 0, UNEXPECTED)

    async def _choose_ledger(self, links: list[WishLink]) -> tuple[WishLink, int, str] | None:
        """The first self-service link whose key HoYoverse accepts, with its account and server; None when none is. A
        refused key gives way to the next, half a second later; any other failure is raised. No more than
        MOST_LINKS_TRIED are tried: when none of those answers and more are left, the log says that they were not tried,
        and the answer is None, as when every link was refused."""
        if len(links) <= MOST_LINKS_TRIED:
            log.info("The game's log and web cache hold %d Feedback link(s). They are tried in turn until one answers.",
                     len(links))
        else:
            log.info("The game's log and web cache hold %d Feedback links. The first %d are tried, until one answers.",
                     len(links), MOST_LINKS_TRIED)
        for index, link in enumerate(links[:MOST_LINKS_TRIED]):
            if index:
                await self._sleep(PAUSE_SECONDS)
            self._try_link(link)
            try:
                uid, region = await ledger_call(self._ledger_user, link)
            except (KeyExpired, KeyInvalid):
                continue
            log.info("Feedback link %d of %d answered.", index + 1, len(links))
            return link, uid, region
        if len(links) > MOST_LINKS_TRIED:
            log.warning("None of the %d Feedback links tried answered; the other %d were not tried.", MOST_LINKS_TRIED,
                        len(links) - MOST_LINKS_TRIED)
        return None

    async def _read_ledgers(self, link: WishLink, uid: int, region: str | None) -> LedgerOutcome | None:
        """Every kind with the link, showing each kind's progress, and the run's sentence at its end."""
        self._show_ledger("running", None, 0, None)
        try:
            outcome = await fetch_ledgers(
                self._store, link, uid, self._ledger_fetch, self._sleep,
                lambda kind, found: self._show_ledger("running", kind, found, None), self._clock.now, region=region,
            )
        except Exception:
            log.exception("the import of the game's history stopped unexpectedly")
            self._show_ledger("failed", None, 0, UNEXPECTED)
            self._bus.publish("ledger_changed")
            return None
        found = sum(outcome.found.values())
        self._show_ledger("failed" if outcome.failed else "done", None, found, ledger_sentence(outcome))
        self._bus.publish("ledger_changed")
        return outcome

    async def _fetch_kept(self, run: _Run, kept: _Kept) -> str | None:
        """Every wish type with the kept link, page by page, as a press fetches them: None when every pool was finished,
        else the sentence of what stopped it. Its first request is the first wish type's first page. The first answer
        that the key is too old or not accepted drops the link, and `auto_stopped` says so until the next press; any
        other failure leaves the link for the next update."""
        gacha_type = GACHA_TYPES[0]
        try:
            first = await self._page(kept.link, gacha_type, "0", POOL_OF[gacha_type])
            if kept.uid is not None and first.rows and first.rows[0].uid != kept.uid:
                raise WishApiError("The wish-history link belongs to a different Genshin account.")
            await self._fetch_types(run, kept.link, first)
        except (KeyExpired, KeyInvalid) as error:
            self.drop_wish_link()
            if kept.minted:
                log.info("HoYoverse no longer accepts the key it gave the HoYoLAB login: it is dropped, and a new one is "
                         "asked for at the next update.")
                return MINTED_REFUSED
            log.info("HoYoverse no longer accepts the link kept from the last import: it is dropped, and the updates "
                     "stop until Import is pressed again.")
            self._auto_stopped = "expired"
            return str(error)
        except WishApiError as error:
            return str(error)
        return None

    async def _choose(self, links: list[WishLink]) -> tuple[WishLink, Page] | None:
        """The first link that answers, with its first page of the first wish type; None when none does. A link whose
        key HoYoverse finds too old or does not accept gives way to the next, half a second later. No more than
        MOST_LINKS_TRIED are tried: when none of those answers and more are left, the log says that they were not tried,
        and the answer is None, as when every link was refused. The log says how many links there are, the time and the
        place in the cache of each tried, and which answered: never a key."""
        gacha_type = GACHA_TYPES[0]
        count = len(links)
        if count == 1:
            log.info("The game's web cache holds 1 wish-history link.")
        elif count <= MOST_LINKS_TRIED:
            log.info("The game's web cache holds %d wish-history links. They are tried the last in the file first, until "
                     "one answers.", count)
        else:
            log.info("The game's web cache holds %d wish-history links. The %d last in the file are tried, until one "
                     "answers.", count, MOST_LINKS_TRIED)
        for index, link in enumerate(links[:MOST_LINKS_TRIED]):
            if index:
                await self._sleep(PAUSE_SECONDS)
            made = made_at(link)
            when = (
                "it has no timestamp" if made is None
                else f"its timestamp says {datetime.fromtimestamp(made, timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
            )
            log.info("Trying link %d of %d: %s, and it lies at byte %s of the cache.", index + 1, count, when,
                     f"{link.at:,}")
            self._try_link(link)
            try:
                page = await self._page(link, gacha_type, "0", POOL_OF[gacha_type])
            except (KeyExpired, KeyInvalid):
                continue
            log.info("Link %d of %d answered.", index + 1, count)
            return link, page
        if count > MOST_LINKS_TRIED:
            log.warning("None of the %d links tried answered; the other %d were not tried.", MOST_LINKS_TRIED,
                        count - MOST_LINKS_TRIED)
        return None

    async def _page(self, link: WishLink, gacha_type: str, end_id: str, pool: str) -> Page:
        """One page. HoYoverse's is fetched in a thread, so that the core keeps answering meanwhile; the demo's, made
        from the store, is made on the core's own thread, as every read of the store is."""
        if inspect.iscoroutinefunction(self._fetch):
            return await self._fetch(link, gacha_type, end_id, pool)
        return await asyncio.to_thread(self._fetch, link, gacha_type, end_id, pool)

    async def _fetch_types(self, run: _Run, link: WishLink, first: Page) -> None:
        """Every wish type from its newest record, page by page; the first type's first page came with the link. A type
        that has not ended after MOST_PAGES is Unexpected: the run ends there, and the pool it was in keeps nothing."""
        page: Page | None = first
        for gacha_type in GACHA_TYPES:
            pool = POOL_OF[gacha_type]
            end_id, number = "0", 0
            while True:
                if page is None:
                    if number >= MOST_PAGES:
                        log.warning(
                            "HoYoverse's wish history answered %d full pages of wish type %s, more than any history "
                            "holds; the import stops there.", number, gacha_type,
                        )
                        raise Unexpected()
                    await self._sleep(PAUSE_SECONDS)
                    page = await self._page(link, gacha_type, end_id, pool)
                number += 1
                run.pages += 1
                reached_the_kept = self._hold(run, page, pool)
                self._show("running", POOL_LABELS[pool], number, run.found, None)
                rows, page = page.rows, None
                if reached_the_kept or len(rows) < PAGE_SIZE:
                    break
                if end_id != "0" and rows[-1].id >= int(end_id):  # no older record: asking again would bring no other
                    log.warning(
                        "HoYoverse's wish history answered a page of wish type %s that does not go past the one "
                        "before; that type ends there.", gacha_type,
                    )
                    break
                end_id = str(rows[-1].id)
            if gacha_type == TYPES_OF_POOL[pool][-1]:
                self._keep(run, pool)

    def _hold(self, run: _Run, page: Page, pool: str) -> bool:
        """Hold the page's records that are not kept yet, for the pool of the type asked for, each once whichever type
        answered it. Whether the page reached a record already kept, where the type ends."""
        if run.uid is None and page.rows:
            run.uid = page.rows[0].uid
        held = run.held.setdefault(pool, {})
        reached = False
        for row in page.rows:
            if self._store.has_wish(row.id):
                reached = True
            elif row.id not in run.seen:
                run.seen.add(row.id)
                held[row.id] = replace(row, import_id=run.import_id)
        return reached

    def _keep(self, run: _Run, pool: str) -> None:
        """A pool fetched to its end: its new records are kept together, and the pool is noted as finished in the same
        write, so that a run cut short later still credits it."""
        pools = [*run.pools, pool]
        run.kept += self._store.keep_import_pool(run.import_id, run.held.pop(pool, {}).values(), pools, run.uid)
        run.pools = pools

    def _end(self, run: _Run | None, failure: str | None) -> None:
        """Record how the import ended, with the run's pages and the new wishes it kept, and tell the pages."""
        pages, kept = (run.pages, run.kept) if run is not None else (0, 0)
        if run is not None:
            self._record(run, self._clock.now(), "ok" if failure is None else "failed", failure)
        if failure is None:
            self._show("done", None, pages, kept, found_sentence(kept))
        else:
            self._show("failed", None, pages, kept, failure)
        self._bus.publish("pulls_changed")

    def _record(self, run: _Run, now: datetime, outcome: str, message: str | None) -> None:
        try:
            self._store.finish_wish_import(
                run.import_id, now, outcome, run.uid, run.kept, run.pages, message, run.pools
            )
        except Exception:  # the next start ends a run left running as failed
            log.exception("the end of the import could not be recorded")


# ---------------------------------------------------------------- the demo's stand-ins

DEMO_KEY = "DEMO-KEY-0123456789"  # made up: the demo's link opens nothing
DEMO_PAUSE_SECONDS = 0.05
DEMO_REGION = "os_euro"
DEMO_NEW_TYPE = "301"  # the character event
DEMO_NEW_BEFORE_NOW = timedelta(hours=2)  # the demo's three new wishes were made this long before its now
DEMO_NEW_WISHES = (("Slingshot", "Weapon", 3), ("Cool Steel", "Weapon", 3), ("Fischl", "Character", 4))  # oldest first
DEMO_FIRST_ID = 1_760_000_000_000_000_000  # the ids start above this when the demo keeps no wish


def demo_links() -> list[WishLink]:
    """The demo's stand-in for the two reads of the game's files: one made-up link, with a made-up key, treated as a key
    is. The demo reads no file of the game's."""
    query = {
        "win_mode": "fullscreen", KEY_VERSION: "1", "auth_appid": "webview_gacha", "lang": "en",
        "region": DEMO_REGION, KEY: DEMO_KEY, "game_biz": "hk4e_global",
    }
    register_secret(DEMO_KEY)
    return [WishLink(f"https://gs.hoyoverse.com/genshin/event/e20190909gacha-v3/index.html?{urlencode(query)}#/log",
                     query, Path())]


def _demo_new_kept(store: Store, uid: int) -> bool:
    """Whether the store holds the demo's three new wishes: they are the account's newest three, a ten-pull's share of
    one time, as they were made. So they are made once for a demo folder, whichever start of Guild Board imports them."""
    newest = store.wishes_before(uid, None, len(DEMO_NEW_WISHES))
    made = [(name, DEMO_NEW_TYPE) for name, _, _ in reversed(DEMO_NEW_WISHES)]
    return [(wish.name, wish.gacha_type) for wish in newest] == made and len({wish.time for wish in newest}) == 1


def _demo_new(store: Store, uid: int, now: datetime) -> list[WishRow]:
    """The demo's three new wishes on the character event, made two hours before the demo's now, with ids above every
    id kept; none once they are kept."""
    if _demo_new_kept(store, uid):
        return []
    top = max((store.wishes_before(other, None, 1)[0].id for other in store.wish_uids()), default=DEMO_FIRST_ID)
    at = (now - DEMO_NEW_BEFORE_NOW).astimezone(server_tz(DEMO_REGION))
    time = at.strftime(TIME_FORMAT)
    at = datetime.strptime(time, TIME_FORMAT).replace(tzinfo=at.tzinfo)  # to the second, as HoYoverse writes it
    return [
        WishRow(top + n, uid, DEMO_NEW_TYPE, POOL_OF[DEMO_NEW_TYPE], name, item_type, rank, time, at, 0)
        for n, (name, item_type, rank) in enumerate(DEMO_NEW_WISHES, start=1)
    ]


def demo_pages(store: Store, uid: int, clock: Clock) -> Callable[[WishLink, str, str, str], Awaitable[Page]]:
    """The demo's stand-in for HoYoverse's history: each wish type's pages from whatever wishes the store holds for
    `uid`, newest first, 20 a page, and on the character event three made-up new ones, two hours before the demo's now,
    until they are kept. So the demo's first import finds three and the later ones nothing, with the demo's history in
    the store or without it. It reads the store, so it runs on the core's own thread: the importer awaits it there."""

    async def fetch(link: WishLink, gacha_type: str, end_id: str, pool: str) -> Page:
        rows = [wish for wish in store.wishes(uid) if wish.gacha_type == gacha_type]
        if gacha_type == DEMO_NEW_TYPE:
            rows.extend(_demo_new(store, uid, clock.now()))
        older = sorted((row for row in rows if end_id == "0" or row.id < int(end_id)), key=lambda row: -row.id)
        return Page(tuple(older[:PAGE_SIZE]))

    return fetch


async def demo_pause(seconds: float) -> None:
    """The demo's pause between two requests: short, as its pages are made at once, yet long enough for the page to
    show the import running."""
    await asyncio.sleep(DEMO_PAUSE_SECONDS)
