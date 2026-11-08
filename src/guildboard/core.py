"""Wires the units together. The command line and, later, the window shell build one Core and talk to it."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Mapping

from guildboard import __version__, characters, endgame_cycles, netpolicy
from guildboard.achievements import importers
from guildboard.achievements.catalog import bundled_raw
from guildboard.achievements.commissions import Commissions
from guildboard.achievements.notes import load_notes
from guildboard.achievements.progress import Progress
from guildboard.achievements.updates import ListUpdates, demo_fetch, github_fetch
from guildboard.api.icons import IconCache
from guildboard.auth import Auth, has_required, parse_cookie_text
from guildboard.backups import (
    DEMO as BACKUP_DEMO, SUFFIX as BACKUP_SUFFIX, RESTORE_UNFINISHED, BackupRefused, apply_pending, database_copy,
    export_target, stage_database, stage_restore, write_backup,
)
from guildboard.board.layouts import PLACES, Layouts
from guildboard.changes import load_changes
from guildboard.checkin import CheckIn
from guildboard.codes import (
    KEPT_RESULTS, NO_CODE, RESULT_TEXT, SETTLED, CodeBook, is_code, nothing_sent, skipped_sentence, skipped_words,
    split_codes,
)
from guildboard.clock import Clock, ShiftedClock, SystemClock
from guildboard.cloud.demo import DemoOnline
from guildboard.cloud.service import Local, Online
from guildboard.cloud.session import CloudStore
from guildboard.cloud.stats import collect_stats, profile_facts
from guildboard.cloud.sync import SYNCED_SETTINGS, SyncSeam, forget_the_account
from guildboard.config import AppPaths, Settings, load_settings, save_settings, settings_from_dict
from guildboard.events import EventBus
from guildboard.freshness import history_notice
from guildboard.gallery import DEMO_FOLDER, THUMBS_FOLDER, Gallery, demo_locator, find_folder
from guildboard.gameday import SERVER_NAMES, game_day
from guildboard.gamewatch import GameWatch, SessionTracker, list_process_names
from guildboard.history import History
from guildboard.hotkeys import hotkey_problem, parse_hotkey
from guildboard.logging_setup import register_secret
from guildboard.maptabs import MapTabs
from guildboard.metrics import Label
from guildboard.ledger import api as ledger_api
from guildboard.ledger.demo import demo_history_links, demo_ledger_pages, demo_ledger_user, seed_ledger
from guildboard.characters import demo as characters_demo
from guildboard.characters.art import ArtCache, live_download
from guildboard.characters.service import Characters
from guildboard.characters.wiki import TUTORIAL, CharacterWiki
from guildboard.pulls import gamecache, nextversion, wishapi
from guildboard.pulls.banners import Banners
from guildboard.pulls.importer import (
    AUTO_EVERY, ImportBusy, ImportNotReady, WishImporter, demo_links, demo_pages, demo_pause,
)
from guildboard.pulls.planner import Planner
from guildboard.pulls.wiki import WikiIcons, wanted_names
from guildboard.records import LoginNeeded, SourceError
from guildboard.selfupdate import Release, Updater, parse_version
from guildboard.sources.demo import (
    DEMO_ACCOUNT, DEMO_SERVER, DemoSource, seed_achievements, seed_appearances, seed_banners, seed_board, seed_codes, seed_history,
    seed_timers, seed_wishes,
)
from guildboard.store import Store, StoreError, iso, valid_text
from guildboard.sync.policy import DAILY_CAP, MIN_GAP
from guildboard.sync.runner import SyncRunner
from guildboard.sync.scheduler import SyncScheduler
from guildboard.tokenstore import REDEEM_TOKEN, StoredToken, TokenStore
from guildboard.timers.presets import load_presets
from guildboard.timers.quick import QuickTimers
from guildboard.timers.reminders import Reminders
from guildboard.timers.timers import Timers
from guildboard.views.achievements import Query, build_achievements
from guildboard.views.board import build_board
from guildboard.views.timers import build_timers, count_ready
from guildboard.views.commissions import build_commissions, count_left
from guildboard.views.backlog import build_backlog
from guildboard.views.ledger import build_history, build_ledger
from guildboard.views import months, wrapped
from guildboard.views.stats import build_stats
from guildboard.views.endgame_card import MODE_ART, build_endgame_card

log = logging.getLogger(__name__)

DEFAULT_SERVER = "os_euro"
DEFAULT_ADVENTURE_RANK = 60  # what the announcements are asked for before any overview has been read
EDITABLE_SETTINGS = (
    "in_session_interval_min", "start_with_windows", "risk_acknowledged", "tray_hint_shown",
    "commission_region", "fishing_rule", "notifications",
    "characters_notice_weekly", "characters_notice_domain", "characters_notice_ready",
    "overlay_hotkey", "overlay_display", "overlay_opacity", "overlay_pinned", "overlay_lines", "overlay_size",
    "overlay_full_screen", "gallery_enabled", "gallery_folder", "spending_currency", "spending_prices", "picture_quality",
    "sync_my_data", "page_size", "page_size_maximized",
)
# The shell holds the overlay's hotkey, or another program does, or there is no hotkey to hold.
PAUSE_SECONDS = 5.5  # between two gift codes sent, which HoYoverse asks for
LIVE_EVERY = 3600  # seconds between two looks at the livestream codes
DAILY_CHECK_EVERY = 600  # seconds between two looks at whether the date changed, which the daily copy follows
LIVE_GAP = 60  # a page that asks again within this many seconds is told what the last look found
# The Wrapped poster saved as a picture (the Stats design, 6.4): the PNG the page made is held in memory alone, one at
# most, until the window's Save dialog has a place for it, and for ten minutes at most. Its name is the one the dialog
# offers, and no other is taken.
PICTURE_HELD = timedelta(minutes=10)
PICTURE_NAME = re.compile(r"TUSKFiZZY Wrapped [0-9]{4} (?:Story|Wide)\.png")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NOT_A_PICTURE = "This is not a picture of TUSKFiZZY's Wrapped."
SOURCE_BUILD_FAILED = "TUSKFiZZY could not load its HoYoLAB library. See the log file."
NO_PICTURE = "There is no picture to save. Press Save as picture again."
PICTURE_GAME_FOLDER = "A picture cannot be saved in the game's folders. Choose another place."
PICTURE_REPLACED = "This picture was not saved: a newer one took its place, and it has a Save dialog of its own."
HOTKEY_STATES = ("held", "taken", "none")
HISTORY_TOLD = "history_told_for"  # the last read a notification said had run out (Core.tell_of_old_history)
RESIN_SECONDS = 8 * 60  # the game adds one original resin every 8 minutes, up to the maximum
# The Endgame card's entries and pictures are read ahead at most this often, when the card is asked for (the Stats design,
# 3.2): each is read once and then kept, and one that failed is asked for again no sooner than this.
ENDGAME_ART_EVERY = timedelta(hours=1)
# The icon beside each reward on the Achievements page, served at /api/icon/static.primogem.
PRIMOGEM_ICON = Label(
    "static.primogem", "Primogem", "static", None,
    "https://upload-static.hoyoverse.com/hoyolab-wiki/2023/01/19/194600931/8902272b2dbb45cce8fbd6f596c6904b_3502391192719350131.png", 0,
)
# The game's own pictures of what the interface counts: the primogem, resin, realm currency and the Parametric
# Transformer. Each is served at /api/icon/<key> through the icon cache, which downloads it once from HoYoverse's
# servers. Where a picture does not load, as the demo's do not, the interface draws a sign in its place (the look pass
# spec, 7.1).
STATIC_ICONS = (
    PRIMOGEM_ICON,
    Label(
        "static.resin", "Original Resin", "static", None,
        "https://act-upload.hoyoverse.com/event-ugc-hoyowiki/2025/07/03/237301566/4328202cd795d7c4a7d5cd54e331c43b_1224981524026882289.png", 0,
    ),
    Label(
        "static.realm_currency", "Realm Currency", "static", None,
        "https://upload-static.hoyoverse.com/hoyolab-wiki/2023/03/29/194600931/853777b20dff4c545de10c30b8f4543c_3219915689637833360.png", 0,
    ),
    Label(
        "static.transformer", "Parametric Transformer", "static", None,
        "https://bbs.hoyolab.com/hoyowiki/picture/object/Parametric%20Transformer_icon.png", 0,
    ),
)


def resin_now(notes: Mapping[str, Any], read_at: datetime, now: datetime) -> int:
    """Original resin as it is now: the notes say how much there was when they were read, and when it
    would be full. At or above the maximum it does not grow, and it is never shown lower than it was read."""
    resin, top = int(notes["resin"]), int(notes["resin_max"])
    if resin >= top:
        return resin
    full_at = notes.get("resin_full_at")
    if full_at:
        left = (datetime.fromisoformat(full_at) - now).total_seconds()
        return top if left <= 0 else max(resin, top - math.ceil(left / RESIN_SECONDS))
    return max(resin, min(top, resin + int((now - read_at).total_seconds() // RESIN_SECONDS)))


def _picture_route(link: Any) -> str | None:
    """The icon route that serves the profile picture, or None while HoYoLAB has named no picture. The route carries a
    short digest of the link, so a new picture is a new address and the window fetches it again."""
    if not isinstance(link, str) or not link:
        return None
    return "/api/icon/account.picture?v=" + hashlib.sha256(link.encode()).hexdigest()[:8]


def _hoyolab_source_class(cache_dir: Path) -> Any:
    """Import the HoYoLAB adapter; the library is loaded in live mode only.

    The library picks its cache folder from the temp folder at the moment it is first imported, and
    reads a cache file there if one exists. For that moment the temp folder is one inside the data
    folder, so live mode writes nothing outside it and never reads what another program left behind.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    previous = tempfile.tempdir
    tempfile.tempdir = str(cache_dir)
    try:
        from guildboard.sources.hoyolab import HoyolabSource
    finally:
        tempfile.tempdir = previous
    return HoyolabSource


class SourceBuildFailed(SourceError):
    """The live source could not be made (the HoYoLAB library would not load). It says so every time, and the cause is
    logged once, when it happened."""


class _CoreSource:
    """What the sync and the check-in hold in a live start: the core's source, looked up at each use, because making the
    live source is left to its first use (Core.source) and they are made before it."""

    def __init__(self, core: "Core") -> None:
        self._core = core

    def __getattr__(self, name: str) -> Any:
        return getattr(self._core.source, name)


def _demo_download(*_: Any, **__: Any) -> None:
    """The demo contacts nothing: pressing Update now there fails as an offline player's would."""
    raise OSError("The demo does not download.")


def _no_download(url: str) -> bytes:
    """The demo's icon download, which refuses at once: the demo contacts nothing, so it shows no icon."""
    raise OSError("the demo contacts nothing")


class CodesRefused(ValueError):
    """Gift codes that cannot be sent now, with the reason in a sentence for the player."""


class SettingRefused(ValueError):
    """A setting's value that is refused, with the reason in a sentence for the player. Nothing of the change that
    held it is saved."""


def _as_kept(name: str, value: Any) -> Any:
    """A value asked for as it is kept, to tell a value that was kept from one that was rejected: a hotkey is kept as
    its canonical text."""
    hotkey = parse_hotkey(value) if name == "overlay_hotkey" and isinstance(value, str) else None
    return hotkey.text if hotkey is not None else value


class Core:
    def __init__(
        self,
        paths: AppPaths,
        *,
        demo: bool,
        clock: Clock | None = None,
        source: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        process_lister: Callable[[], set[str]] | None = None,
        demo_now: datetime | None = None,
        cloud_fetch: Callable[..., tuple[int, bytes]] | None = None,
        open_browser: Callable[[str], object] = webbrowser.open,
    ) -> None:
        paths.ensure()
        self.paths = paths
        self.demo = demo
        # Demo mode runs on a clock that tests can start at a chosen instant and move forward.
        self.clock: Clock = clock or (ShiftedClock(demo_now) if demo else SystemClock())
        # First of all: a restore the player staged replaces the database and the settings, before either is read.
        restored = apply_pending(paths, self.clock.now())
        self.settings: Settings = load_settings(paths.settings_path)
        self.bus = EventBus()
        db_existed = paths.db_path.exists()  # before Store.open makes it: a first install has none
        self.store = Store.open(paths.db_path, paths.backups_dir, self.clock.now())
        try:
            self.store.claim("demo" if demo else "live")
        except StoreError:
            self.store.close()
            raise
        if restored:
            forget_the_account(self.store)
        try:
            self.store.prune_skipped_runs(self.clock.now())
        except sqlite3.Error:
            log.warning("The old records of refused syncs could not be removed", exc_info=True)
        self.whats_new_at_start(db_existed)
        if not demo:
            try:
                self.store.daily_backup(self.clock.now())
            except (OSError, sqlite3.Error, StoreError):
                log.warning("The daily backup could not be made", exc_info=True)
        self.tokens = TokenStore(paths.token_path)
        self._tuskfizzy_note_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._tuskfizzy_chronicle_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._source: Any = None
        self._make_source: Callable[[], Any] | None = None  # the live source's maker, until it is made
        self._source_lock = threading.Lock()
        self._source_failed = False
        self._warm_task: asyncio.Task[None] | None = None
        if source is not None:
            self.source = source
        elif demo:
            self.source = DemoSource()
        else:
            # The batch calculator is asked about the focus the Characters service names (late-bound: it is made below).
            # The source, and the library it needs, are made when it is first used, and a start warms it up (see `source`).
            self._make_source = lambda: _hoyolab_source_class(paths.data_dir / "cache" / "library")(
                self.tokens, calculator_targets=lambda: self.characters.calculator_targets()
            )
        token = None if demo else self.tokens.load()
        if token is not None:
            for value in token.cookies.values():
                register_secret(value)
        self.history = History(self.store)
        self._sleep = sleep
        self.runner = SyncRunner(
            self._holder(), self.store, self.clock, self.server, self._retry_pause, after_group=self._group_stored,
            before_fetch=self._source_ready,
        )
        self.scheduler = SyncScheduler(
            self.runner,
            self.store,
            self.clock,
            self.bus,
            settings=lambda: self.settings,
            server=self.server,
            has_token=self.has_token,
            sleep=sleep,
        )
        other = self._login_of_another_account()
        if other is not None:
            self.scheduler.block("login_needed", (
                f"The HoYoLAB login is of another game account (UID {other.uid}) than this history (UID {self._account_uid()}). "
                f"Log out and log in with the account of UID {self._account_uid()}."
            ))
        self.tracker = SessionTracker(
            self.store,
            self.clock,
            self.bus,
            on_started=self._game_started,
            on_stopped=self._game_stopped,
        )
        self.gamewatch = GameWatch(self.tracker, lister=process_lister or list_process_names, sleep=sleep)
        self.auth = None if demo else Auth(
            self.tokens,
            self._list_accounts,
            on_change=self._auth_changed,
            account_uid=self._account_uid,
            remember_account=lambda uid: self.store.set_meta("account_uid", str(uid)),
        )
        self.icons = IconCache(paths.icons_dir, _no_download) if demo else IconCache(paths.icons_dir)
        self.checkin = CheckIn(
            self._holder(), self.clock, lambda: self.bus.publish("checkin_changed"),
            ready=lambda: self.demo or (self.settings.risk_acknowledged and self.has_token()),
            before_use=self._source_ready,
        )
        self.launch_secret = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        # A library that logs request addresses must not log these. They live as long as the core, so the log mask's
        # cap never pushes them out (lasting).
        register_secret(self.launch_secret, lasting=True)
        register_secret(self.session_token, lasting=True)
        self.window_requests: Callable[[str], bool] | None = None
        # The Wrapped picture waiting for the Save dialog (its number, name, PNG and when it came), the timer that lets it
        # go, and the number the last one held was given.
        self._picture: tuple[int, str, bytes, datetime] | None = None
        self._picture_timer: asyncio.TimerHandle | None = None
        self._pictures_held = 0
        # set by the shell when its tray icon keeps TUSKFiZZY running after the window closes
        self.has_tray = False
        # Set by the shell: shows a notification from the tray icon, a reminder or a test, with its text.
        self.notifier: Callable[[str, str], bool] | None = None
        # Called with the new settings after every change, on the core's thread (the shell applies them).
        self.settings_listeners: list[Callable[[Settings], None]] = []
        # Called with True when the game starts and with False when it stops, on the core's thread, once the sync
        # scheduler has heard of it (the shell shows the overlay's window while the game runs).
        self.game_listeners: list[Callable[[bool], None]] = []
        # Called with the Map page's tabs (maptabs.snapshot) after every change to them or to the area the page leaves
        # for the map, on the core's thread (the shell keeps its map views in step).
        self.map_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._map_session = 0  # counts logins and logouts: the shell reads the session again when it changes
        # What the shell reported about the overlay, by overlay_shell: its displays and the state of its hotkey. None
        # while there is no shell.
        self._overlay_report: tuple[list[dict[str, Any]], str] | None = None
        # Whether TUSKFiZZY's window is maximized, as the shell reports it (window_state): the Board then shows its
        # arrangement for a maximized window. False while there is no shell.
        self._window_maximized = False
        self._watch_task: asyncio.Task[None] | None = None
        if demo and self.store.first_game_day() is None:
            seed_history(self.store, self.clock.now())
        # One ListUpdates for the core's whole life: the list a check found waits for install() in its memory. The
        # demo's download contacts nothing, and its list is recorded as the demo's.
        fetch, source = (demo_fetch(bundled_raw()), "demo") if demo else (github_fetch(), "github")
        self.lists = ListUpdates(self.store, self.clock, fetch, source=source)
        self.notes = load_notes()
        self.progress = Progress(self.store, self.lists.current, self.notes, self.clock)  # follows the installed list
        self.commissions = Commissions(
            self.store, self.lists.current, self.notes, self.progress, self.clock, self.server
        )
        self.presets = load_presets()
        # A pre-set's official art is served at /api/icon/timer.<key>, through the icon cache, as the primogem is.
        icons = [
            Label(f"timer.{preset.key}", preset.name, "static", None, preset.icon, 0)
            for preset in self.presets.presets.values() if preset.icon is not None
        ]
        self.store.upsert_labels([*STATIC_ICONS, *icons], self.clock.now())
        if demo and not self.store.progress():
            seed_achievements(self.store, self.lists.current(), self.clock.now())
        self.timers = Timers(
            self.store, self.clock, self.progress, self.presets, self.server, lambda: self.settings.fishing_rule
        )
        self.timers.ensure_defaults(self.lists.current())  # after the demo's progress, which decides the defaults
        self.codebook = CodeBook(self.store)
        if demo:
            seed_codes(self.store, self.clock.now())
        self._live_codes: list[dict[str, Any]] = []
        self._live_checked_at: datetime | None = None
        self._codes_progress: list[dict[str, Any]] = []
        self._codes_send: asyncio.Task[None] | None = None
        self._codes_watch: asyncio.Task[None] | None = None
        self._live_look: asyncio.Task[None] | None = None
        self._last_sent_at: datetime | None = None  # when the last code was sent, across runs
        if demo and not self.store.timer_events():  # once, after the defaults: the demo's timers and commission days
            now = self.clock.now()
            seed_timers(self.store, self.timers, now)
            seed_appearances(self.store, self.lists.current(), self.notes, now)
        self.layouts = Layouts(self.store, self.clock)
        if demo:
            seed_board(self.store, self.layouts)  # once: the demo's note on the starting Board
        self.quick = QuickTimers(self.store, self.clock)
        self.maptabs = MapTabs(self.store)
        # Real waits, not the `sleep` the tests give the sync: the watch would otherwise run the clock on and on.
        self.reminders = Reminders(
            self.timers, self.store, self.clock, self.bus,
            enabled=lambda: self.settings.notifications or self.settings.overlay_lines,
            notify=self.remind, quick=self.quick,
        )
        self._reminders_task: asyncio.Task[None] | None = None
        # Pulls. An import that TUSKFiZZY's closing cut short is ended as failed, as a sync is. The planner reads what
        # is kept. The banners and the icons are read from HoYoverse's own pages, through netpolicy; the demo's stay
        # without any read, as the demo contacts nothing.
        now = self.clock.now()
        self.store.fail_running_wish_imports(now)
        if demo:
            seed_wishes(self.store, now)
            seed_banners(self.store, now)
        self.pulls = Planner(
            self.store, self.clock, self.history, self.lists.current, self.server, self._account_uid, demo,
            login_on=self._login_refresh_on,
        )
        # The import reads the game's two files and HoYoverse's history only on the player's press, and only once the
        # player has given the first run's consent (ruling R21). The demo's runs on stand-ins: a made-up link and pages
        # made from its own wishes; it reads no file of the game's and contacts nothing, so it needs no consent. Real
        # waits between two pages, as the reminders have, not the `sleep` the tests give the sync.
        # The same press reads the game's own history logs with the Feedback link the same two reads find (the ledger
        # design, section 0); the demo's are stand-ins made from its own made-up history.
        if demo:
            seed_ledger(self.store, DEMO_ACCOUNT.uid, DEMO_SERVER, now)
            read_links, pages, pause = (
                demo_history_links(demo_links), demo_pages(self.store, DEMO_ACCOUNT.uid, self.clock), demo_pause,
            )
            ledger_pages = demo_ledger_pages(self.store, DEMO_SERVER, self.clock)
            ledger_user = demo_ledger_user(DEMO_ACCOUNT.uid, DEMO_SERVER)
        else:
            read_links, pages, pause = gamecache.read_history_links, wishapi.fetch_page, asyncio.sleep
            ledger_pages, ledger_user = ledger_api.fetch_page, ledger_api.fetch_user
        # Both histories also update themselves with read-only keys HoYoverse gives the stored HoYoLAB login, reading no
        # file of the game's: at the start, once the first sync has run, and then every AUTO_EVERY while TUSKFiZZY
        # runs. A refusal of the login stops them until the next login; Import stays, reading the game's files. The
        # demo's keys are stand-ins, and its first update waits for AUTO_EVERY or POST /api/demo/refresh.
        self._login_refused = False
        self._login_session = 0  # counts logins and logouts: a key asked for across one is not kept
        self._refresh_task: asyncio.Task[None] | None = None
        self._import_task: asyncio.Task[None] | None = None  # the work of the last press of Import
        self._refresh_wake = asyncio.Event()
        self.wish_import = WishImporter(
            self.store, self.clock, self.bus, read_links=read_links, fetch=pages, sleep=pause,
            after=self._after_import, ready=lambda: demo or self.settings.risk_acknowledged,
            ledger_fetch=ledger_pages, ledger_user=ledger_user, login=self._login_status,
        )
        hoyoverse = None if demo else netpolicy.fetch_hoyoverse  # the banners' and the icons' reads; the demo has none
        # Nothing reaches HoYoverse before the first run's consent (ruling R15): the banners wait for the tick.
        self.banners = Banners(
            self.store, self.clock, self.bus, fetch=hoyoverse, server=self.server, uid=self._account_uid,
            level=self._adventure_rank, ready=lambda: self.settings.risk_acknowledged, named=self.icons_wanted,
        )
        self.wiki = WikiIcons(self.store, self.clock, fetch=hoyoverse)
        # The Characters pages (the Characters plan): the wiki's entries and the art cache, and the service on them. The
        # wiki reads through the same seam as the icons' (none in the demo); the art cache's live download and the
        # service's methods are Part V's.
        self.characters_wiki = CharacterWiki(self.store, self.clock, hoyoverse, self._game_version)
        self.art = ArtCache(self.store, paths, self.clock, None if demo else live_download(), demo)
        self._characters_task: asyncio.Task[None] | None = None
        self._endgame_art_task: asyncio.Task[None] | None = None  # the Endgame card's entries and pictures, read ahead
        self._endgame_art_at: datetime | None = None  # when that read last began
        self._daily_task: asyncio.Task[None] | None = None
        self.characters = Characters(
            self.store, self.clock, self.bus, self.characters_wiki, self.art, self.pulls, self.server,
            self.pulls.account_uid, demo,
            ready=lambda: self.settings.risk_acknowledged, sync=self._characters_sync, notify=self._characters_notify,
            switches=self._characters_switches, cards=characters_demo.demo_card if demo else None,
            day_resin=(lambda: characters_demo.DAY_RESIN) if demo else None,
            game_running=lambda: self.tracker.running,
        )
        if demo:
            characters_demo.seed(self.store, DEMO_ACCOUNT.uid, self.clock.now())
        self._endgame_cycles_at_start()
        self._banners_task: asyncio.Task[None] | None = None
        self._icons_task: asyncio.Task[None] | None = None
        # The importer's updates with the link it kept (the second pass design, 3.5): while the game runs, one task asks
        # for an update every AUTO_EVERY; when it closes, one more is asked for, in a task of its own.
        self._auto_task: asyncio.Task[None] | None = None
        self._last_auto_task: asyncio.Task[None] | None = None
        # TUSKFiZZY's own updates, for the installed copy alone: a checkout updates itself with git, and the demo
        # contacts nothing.
        self.updater = Updater(
            paths.data_dir / "updates", self.bus, enabled=not demo and getattr(sys, "frozen", False),
            **({"download": _demo_download} if demo else {}),
        )
        self._update_task: asyncio.Task[None] | None = None
        # The screenshot gallery (the ledger design, section 7): off by default, and while it is off nothing of the
        # game's is read. Its thumbnails are kept in the data folder. The demo's pictures are its own, made in its data
        # folder the first time its gallery looks: the demo never reads the game's log or folders.
        self.gallery = Gallery(
            lambda: self.settings,
            self.store.gallery_favourites,
            lambda name, on: self.store.set_gallery_favourite(name, on, self.clock.now()),
            paths.data_dir / THUMBS_FOLDER,
            locate=demo_locator(paths.data_dir / DEMO_FOLDER, self.clock.now) if demo else find_folder,
        )
        # The online account (the online design): nothing happens until the player signs in, and the demo has no client.
        self.online: Online | DemoOnline = DemoOnline(self.bus, self.clock) if demo else Online(
            CloudStore(paths.cloud_path), self.bus, self.clock, fetch=cloud_fetch, open_browser=open_browser,
            local=Local(
                stats=lambda: collect_stats(self),
                profile=lambda: profile_facts(self),
                game=lambda: (self.tracker.running, self.tracker.session_started_at),
                server=self.server,
                today=self.today,
                sync=SyncSeam(
                    store=self.store, on=lambda: self.settings.sync_my_data, setting=lambda name: getattr(self.settings, name),
                    apply_setting=self.apply_synced_setting, changed=self._synced_changed, uid=self._account_uid,
                    keep_settings=self._backup_before_the_account,
                    settings_changed=lambda: any(getattr(self.settings, name) != getattr(Settings(), name) for name in SYNCED_SETTINGS),
                ),
            ),
            sleep=sleep,
        )
        self._online_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ the endgame's cycles (the Stats design, 3.5)
    def _endgame_cycles_at_start(self) -> None:
        """A live core fills the table of cycles from the raw archive, once for a database; the demo keeps its own made-up
        cycles, up to date with its clock. Neither ever stops a start."""
        try:
            if self.demo:
                self._demo_cycles(self.clock.now())
            else:
                endgame_cycles.backfill_once(self.store, self.clock.now())
        except Exception:  # a failure of the reading as well as of the database
            log.warning("The endgame's cycles could not be filled", exc_info=True)

    def _demo_cycles(self, now: datetime) -> None:
        with self.store.transaction():
            for row in characters_demo.demo_endgame_cycles(now, self.server()):
                self.store.put_endgame_cycle(row)

    def _group_stored(self, group: str, at: datetime) -> None:
        """After a sync stored a group: the endgame's cycles are kept from what it read. The demo reads none of HoYoLAB's
        answers, and keeps its own cycles."""
        if group != "endgame":
            return
        if self.demo:
            self._demo_cycles(at)
        else:
            endgame_cycles.record_latest(self.store, at)

    # ------------------------------------------------------------------ the Endgame card (the Stats design, 3.1 and 3.2)
    def stats_endgame(self) -> dict[str, Any]:
        """The Overview's Endgame card: the newest endgame detail, the art cache's pictures and the Tutorial entries kept."""
        rows = {mode: self.store.wiki_entry(spec.wiki_entry) for mode, spec in MODE_ART.items()}
        return build_endgame_card(self.store, self.art, rows, self.clock.now(), self.server())

    def endgame_art_wanted(self) -> None:
        """The card was asked for: its entries and pictures are read ahead (read_endgame_art) in a task of its own, at most
        once in ENDGAME_ART_EVERY, as the Characters pages' entries are read when a page asks. Only after the first run's
        consent, never in the demo."""
        if self.demo or not self.settings.risk_acknowledged:
            return
        if self._endgame_art_task is not None and not self._endgame_art_task.done():
            return
        now = self.clock.now()
        last = self._endgame_art_at
        if last is not None and 0 <= (now - last).total_seconds() < ENDGAME_ART_EVERY.total_seconds():
            return
        self._endgame_art_at = now
        self._endgame_art_task = self._spawn(self.read_endgame_art())

    async def read_endgame_art(self) -> None:
        """The three modes' Tutorial entries in the HoYoLAB wiki, each read once for a game version, then their icons and the
        three public header pictures, each fetched once into the art cache, one at a time and never while the game runs; the
        pages are told when something new was kept. Nothing is asked before the first run's consent, and the demo asks for
        nothing. A failure leaves what was kept, and the card shows its fallbacks."""
        if self.demo or not self.settings.risk_acknowledged:
            return
        try:
            before = [self.store.wiki_entry(spec.wiki_entry) for spec in MODE_ART.values()]
            rows = [await self.characters_wiki.entry(spec.wiki_entry, TUTORIAL) for spec in MODE_ART.values()]
            icons = [str(row.data["icon"]) for row in rows if row is not None and row.kind == TUTORIAL and row.data.get("icon")]
            running = lambda: self.tracker.running  # noqa: E731
            kept = await self.art.warm(icons, paused=running)
            kept += await self.art.warm_public([spec.picture_index for spec in MODE_ART.values()], paused=running)
            if kept or rows != before:
                self.bus.publish("stats_changed")
        except asyncio.CancelledError:
            raise
        except Exception:  # pictures are decoration: the card shows its fallbacks
            log.warning("The Endgame card's entries and pictures could not be read ahead.", exc_info=True)

    # ------------------------------------------------------------------ facts
    def server(self) -> str:
        if self.demo:
            return DEMO_SERVER
        token = self.tokens.load()
        return token.server if token is not None else DEFAULT_SERVER

    def _account_uid(self) -> int | None:
        """The game account whose history this folder keeps, once the first login has named it."""
        value = self.store.meta("account_uid")
        return int(value) if value is not None else None

    def _login_of_another_account(self) -> StoredToken | None:
        """The stored login, when it is of another game account than the one whose history this folder keeps: after the
        restore of another account's backup, or a data folder carried to another PC. One history follows one game account,
        and a sync with this login would add the other account's counters to it."""
        known, token = self._account_uid(), None if self.demo else self.tokens.load()
        return token if token is not None and known is not None and token.uid != known else None

    def _adventure_rank(self) -> int:
        """The adventure rank the newest read of the overview named, which the announcements are asked for; 60 when no
        overview was read yet, or it named none."""
        overview = self.store.latest_doc("overview")
        rank = overview[1].get("adventure_rank") if overview is not None else None
        return rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else DEFAULT_ADVENTURE_RANK

    def has_token(self) -> bool:
        return self.demo or self.tokens.load() is not None

    def today(self) -> date:
        return game_day(self.clock.now(), self.server())

    def backlog(self, sort: str = "least") -> dict[str, Any]:
        return build_backlog(self.store, self.history, self.today(), sort)

    def stats(self, range_key: str = "30") -> dict[str, Any]:
        # Play by the hour follows this PC's own clock, summer time included (tz None), not the server's: the question
        # is about the player's day.
        return build_stats(
            self.store, self.history, self.today(), self.server(), range_key, tz=None, uid=self.ledger_uid()
        )

    def stats_month(self, ym: str | None = None) -> dict[str, Any]:
        """The Months tab: the list of months and the month `ym`, the newest when it is not in the list. The pity of each
        5-star is the Pulls page's own, with its corrections and windows."""
        uid = self.ledger_uid()
        states = self.pulls.states(uid) if uid is not None else {}
        return months.stats_month(
            self.store, self.history, uid, self.server(), ym, self.today(), states, self.clock.now()
        )

    def stats_year(self, year: int | None = None) -> dict[str, Any]:
        """The Year tab (the Stats design, section 6): the years listed and the year `year`, the newest when it is not
        listed. The pity of each 5-star is the Pulls page's own; the hours are read on this PC's clock, as the
        Overview's; the poster's player is the name, AR and server alone, never the UID."""
        uid = self.ledger_uid()
        states = self.pulls.states(uid) if uid is not None else {}
        account = self._account() or {}
        server = self.server()
        rank = account.get("adventure_rank")
        profile = wrapped.Profile(
            name=str(account.get("nickname") or ""),
            ar=rank if isinstance(rank, int) and not isinstance(rank, bool) else None,
            server=SERVER_NAMES.get(server, server),
        )
        return wrapped.stats_year(
            self.store, self.history, uid, server, year, self.today(), states, self.clock.now(), profile,
            self.art.address, tz=None,
        )

    def ledger_uid(self) -> int | None:
        """The account whose game history and diary detail the pages read: the login's, else the account the Pulls page
        shows, else the one whose history was imported last; the demo's."""
        if self.demo:
            return DEMO_ACCOUNT.uid
        known = self._account_uid() or self.pulls.uid()
        if known:
            return known
        uids = self.store.ledger_uids()
        return uids[0] if uids else None

    def ledger(self) -> dict[str, Any]:
        """The Ledger page's model (the ledger design, section 6)."""
        return build_ledger(
            self.store, self.ledger_uid(), self.server(), self.today(), self.settings,
            self.wish_import.ledger_import_state(),
        )

    def ledger_history(self, kind: str, before: int | None, limit: int) -> dict[str, Any]:
        return build_history(self.store, self.ledger_uid(), kind, before, limit)

    def _account(self) -> dict[str, Any] | None:
        token = None if self.demo else self.tokens.load()
        overview = self.store.latest_doc("overview")
        if not self.demo and token is None and overview is None:
            return None
        doc = overview[1] if overview is not None else {}
        notes = self.store.latest_doc("notes")
        days_active = doc.get("counters", {}).get("days_active")
        if self.demo:
            nickname, uid = DEMO_ACCOUNT.nickname, DEMO_ACCOUNT.uid
        else:
            nickname = doc.get("nickname") or (token.nickname if token is not None else "")
            uid = token.uid if token is not None else None
        return {
            "nickname": nickname,
            "server": self.server(),
            "uid": uid,
            "adventure_rank": doc.get("adventure_rank"),
            "days_active": int(days_active) if days_active is not None else None,
            "resin": resin_now(notes[1], notes[0], self.clock.now()) if notes is not None else None,
            "resin_max": notes[1]["resin_max"] if notes is not None else None,
            # When the notes that resin counts on from were read: the Board's Resin glints for a new read, not the clock.
            "resin_read_at": iso(notes[0]) if notes is not None else None,
            # When Resin is full again, as the latest notes counted it; the top bar shows it beside the number.
            "resin_full_at": notes[1].get("resin_full_at") if notes is not None else None,
            # The profile picture, through the icon route. The address changes with the link, so that a new picture
            # is fetched again, not kept by the window's cache.
            "picture_url": _picture_route(doc.get("picture_url")),
        }

    def status(self) -> dict[str, Any]:
        last_ok = self.store.last_ok_at()
        next_at = self.scheduler.next_sync_at()
        since = self.history.tracking_since()
        last_session = self.store.last_session()
        started = self.tracker.session_started_at
        return {
            "version": __version__,
            "mode": "demo" if self.demo else "live",
            "has_shell": self.window_requests is not None,
            "window_maximized": self._window_maximized,
            "has_tray": self.has_tray,
            "now": iso(self.clock.now()),
            "data_dir": str(self.paths.data_dir),
            "restore_unfinished": (self.paths.data_dir / RESTORE_UNFINISHED).exists(),
            "update": self.updater.view(),
            "tracking_since": since.isoformat() if since is not None else None,
            "sync": {
                "state": self.scheduler.current_state(),
                "message": self.scheduler.message,
                "last_ok_at": iso(last_ok) if last_ok is not None else None,
                "next_at": iso(next_at) if next_at is not None else None,
                "manual_wait_seconds": self.scheduler.manual_wait_seconds(),
                "runs_today": self.scheduler.runs_today(),
                "daily_cap": DAILY_CAP,
                "manual_gap_seconds": int(MIN_GAP.total_seconds()),
            },
            "game": {
                "running": self.tracker.running,
                "session_started_at": iso(started) if started is not None else None,
                "last_session_minutes": (
                    round((last_session.ended_at - last_session.started_at).total_seconds() / 60)
                    if last_session is not None
                    else None
                ),
            },
            "account": self._account(),
            "savings": self._savings(),
            "codes": self.codebook.totals(self._codes_uid() or ""),
            "whats_new": self._whats_new,
            # Whether Your week, Endgame and the Today widget are shown (`characters.WEEK_AND_ENDGAME`).
            "week_and_endgame": characters.WEEK_AND_ENDGAME,
            "online": self.online.view(),
            "history_notice": self._history_notice(),
        }

    def whats_new_at_start(self, db_existed: bool) -> None:
        """Decide once per start whether the "What's new" card shows. A first install, which has no database and has
        seen no card, only records its version; an update shows the card when the notes have an entry for the new
        version."""
        self._whats_new: dict[str, Any] | None = None
        self._demo_history_notice: dict[str, Any] | None = None
        if self.settings.last_seen_version == "" and not db_existed:
            self.whats_new_seen()
            return
        if self.settings.last_seen_version != __version__:
            items = load_changes().get(__version__)
            if items is not None:
                self._whats_new = {"version": __version__, "items": items}

    def demo_update(self, state: str) -> None:
        """The demo only: show the update notice in a state, with the newest notes, to look at it and test it."""
        if state == "none":
            self.updater.demo_clear()
            return
        version, items = next(iter(load_changes().items()))
        self.updater.demo_show(Release(version, "", 1, "", tuple(items), "2026-10-26"), state)

    def demo_history_stopped(self, how: str) -> None:
        """The demo only: show the notice that the history stopped updating, as two days without a read would."""
        now = self.clock.now()
        self._demo_history_notice = history_notice(now - timedelta(days=2), now, now, how)
        self.bus.publish("state_changed")

    def demo_whats_new(self) -> None:
        """The demo only: show the card of the version just installed, as it shows after an update."""
        version, items = next(iter(load_changes().items()))
        self._whats_new = {"version": version, "items": items}
        self.bus.publish("update_changed")

    def whats_new_seen(self) -> None:
        """The player has seen the card: record the version, so it shows no more."""
        self._whats_new = None
        self.settings.last_seen_version = __version__
        save_settings(self.paths.settings_path, self.settings)

    def whats_new_history(self, count: int = 5) -> list[dict[str, Any]]:
        """The notes of the newest `count` versions this copy has, for Settings: by version number, so the file's order
        is no rule, and without the notes written ahead of a release that this copy is not yet."""
        mine = parse_version(__version__)
        known = [(parse_version(version), version, items) for version, items in load_changes().items()]
        shown = sorted(
            ((number, version, items) for number, version, items in known if number and mine and number <= mine),
            key=lambda entry: entry[0],
            reverse=True,
        )
        return [{"version": version, "items": items} for _, version, items in shown[:count]]

    def _savings(self) -> dict[str, int] | None:
        """What the typed balance has grown to now, for the top bar; None while no balance is typed."""
        try:
            estimate = self.pulls.balance_estimate()
        except Exception:
            # The plate shows "Set on Pulls" then; a planner that fails must not make the whole status fail.
            log.exception("the savings could not be worked out")
            return None
        if estimate is None:
            return None
        return {"primogems": estimate.primogems, "fates": estimate.fates, "pulls": estimate.pulls}

    # ------------------------------------------------------------------ changes
    def set_paused(self, paused: bool) -> None:
        self.settings.sync_paused = paused
        save_settings(self.paths.settings_path, self.settings)
        self.scheduler.publish_state()
        self.pulls_changed()  # Pulls and Ledger say whether their history updates itself: not while the sync is paused
        self._settings_changed()

    def update_settings(self, changes: Mapping[str, Any]) -> Settings:
        hotkey = changes.get("overlay_hotkey")
        problem = hotkey_problem(hotkey) if isinstance(hotkey, str) else None
        if problem is not None:
            raise SettingRefused(problem)  # before anything of the request is saved
        merged = self.settings.to_dict()
        merged.update({name: changes[name] for name in EDITABLE_SETTINGS if name in changes})
        candidate = settings_from_dict(merged)
        # A rejected value falls back to the default; keep the current value instead.
        for name in EDITABLE_SETTINGS:
            if name in changes and getattr(candidate, name) != _as_kept(name, changes[name]):
                setattr(candidate, name, getattr(self.settings, name))
        gallery_before = (self.settings.gallery_enabled, self.settings.gallery_folder)
        consent_before = self.settings.risk_acknowledged
        for name in SYNCED_SETTINGS:  # a setting that is not about this PC leaves a line for sync when the player changes it
            if getattr(candidate, name) != getattr(self.settings, name):
                self.store.sync_queue("settings", name)
        self.settings = candidate
        save_settings(self.paths.settings_path, self.settings)
        if (candidate.gallery_enabled, candidate.gallery_folder) != gallery_before:
            self.gallery.forget()  # turned off, or another folder: the next look starts anew
        if candidate.risk_acknowledged and not consent_before and not self.demo:  # the demo needs no consent
            self.wake_login_refresh()  # the first run's consent: the histories may be read with the login now
        self._settings_changed()
        return self.settings

    def _backup_before_the_account(self) -> None:
        """Use the account's data replaces the settings that sync as well, and a copy of the database holds none: this file has
        both, and Settings can restore it."""
        stamp = self.clock.now().astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        write_backup(self.store, self.paths.settings_path, self.paths.backups_dir / f"before-account-{stamp}{BACKUP_SUFFIX}", self.clock.now())

    def apply_synced_setting(self, name: str, value: Any) -> bool:
        """A setting that another PC changed, kept as a value the player could have set: one the settings refuse stays as it
        was. No line is left for sync, and no page is told yet (`_synced_changed` does, once, for all that came). Whether the
        setting changed."""
        if name not in SYNCED_SETTINGS:
            return False
        candidate = settings_from_dict({**self.settings.to_dict(), name: value})
        if getattr(candidate, name) != _as_kept(name, value) or getattr(candidate, name) == getattr(self.settings, name):
            return False
        self.settings = candidate
        save_settings(self.paths.settings_path, self.settings)
        return True

    def _synced_changed(self, events: set[str]) -> None:
        """Rows came from the online account: tell the pages, as each change the player makes here does."""
        for event in sorted(events):
            if event == "achievements_changed":
                self.achievements_changed()
            elif event == "timers_changed":
                self.timers_changed()
            elif event == "layout_changed":
                for place in PLACES:
                    self.layout_changed(place)
            elif event == "pulls_changed":
                self.pulls_changed()
            elif event == "characters_changed":
                self.characters_changed()
            elif event == "codes_changed":
                self.codes_changed()
            elif event == "state_changed":
                self.scheduler.publish_state()
            elif event == "settings_changed":
                self._settings_changed()

    # ------------------------------------------------------------------ the gallery
    async def gallery_model(self, *, fresh: bool = False) -> dict[str, Any]:
        """GET /api/gallery. While the gallery is on, it looks at the folder when the page opens (fresh), when the
        player asks to look again (fresh), and when there is no listing yet; away from the event loop, as it may read the
        game's log and lists the folder. A folder found through the log is kept in the settings. A look that a change of
        the gallery's settings outlived keeps nothing (Gallery.forget), so the folder the player pasted meanwhile stays
        theirs, and one more look is made for it."""
        for _ in range(2):
            if not (self.gallery.enabled and (fresh or not self.gallery.looked)):
                break
            fresh = False
            await asyncio.to_thread(self.gallery.look)
            folder = self.gallery.folder
            if folder is not None and self.settings.gallery_enabled and str(folder) != self.settings.gallery_folder:
                self.settings.gallery_folder = str(folder)
                save_settings(self.paths.settings_path, self.settings)
                self.bus.publish("settings_changed")
        return self.gallery.model()

    def _settings_changed(self) -> None:
        self.reminders.wake()  # the fishing rule moves fishing spots' ready times
        for listener in list(self.settings_listeners):
            try:
                listener(self.settings)
            except Exception:  # one listener's failure must not stop a setting from being saved
                log.exception("a settings listener failed")
        self.bus.publish("settings_changed")  # after the listeners: the shell hears of a change before any page does

    def notify(self, kind: str, text: str) -> bool:
        """Show a notification from the tray icon: "reminder" or "test". False when there is no tray to show it."""
        if self.notifier is None or not self.has_tray:
            return False
        try:
            return bool(self.notifier(kind, text))
        except Exception:  # a notification that cannot be shown must not stop the reminders or the API
            log.exception("a notification could not be shown")
            return False

    def remind(self, kind: str, text: str) -> bool:
        """A reminder, in each way the player has on: a line over the game while it runs, and a Windows notification.
        Whether either was given."""
        line = self.settings.overlay_lines and self.tracker.running
        if line:
            self.overlay_line(text)
        notified = self.settings.notifications and self.notify(kind, text)
        log.info("A %s reminder: line %s, notification %s.", kind, "yes" if line else "no", "yes" if notified else "no")
        return line or notified

    def _tuskfizzy_session_path(self, account_id: str) -> Path:
        """A profile's own DPAPI-protected HoYoLAB login, separate from the Board's primary session."""
        if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
            raise ValueError("Choose a valid Genshin profile.")
        profile = next((item for item in self.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id), None)
        if profile is None:
            raise ValueError("That Genshin profile no longer exists.")
        # IDs from legacy imports are arbitrary text; only a digest becomes a filename.
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        return self.paths.data_dir / "tuskfizzy_sessions" / f"{digest}.bin"

    async def tuskfizzy_login(self, account_id: str, cookie_text: str) -> dict[str, Any]:
        """Validate and save a dedicated HoYoLAB login for exactly one TUSKFiZZY Genshin profile."""
        if self.demo:
            raise ValueError("Demo mode cannot save HoYoLAB sessions.")
        if not isinstance(cookie_text, str) or len(cookie_text) > 32_000:
            raise ValueError("Paste the complete HoYoLAB Cookie header.")
        profile = next((item for item in self.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id), None)
        if profile is None:
            raise ValueError("That Genshin profile no longer exists.")
        cookies = parse_cookie_text(cookie_text)
        if not has_required(cookies):
            raise ValueError("The pasted text does not contain a complete HoYoLAB login.")
        for value in cookies.values():
            register_secret(value)
        await self._source_ready()
        accounts = await self.source.list_accounts(cookies)
        chosen = next((item for item in accounts if str(item.uid) == profile["uid"]), None)
        if chosen is None:
            raise ValueError("This HoYoLAB login does not contain the Genshin UID saved in this profile.")
        if chosen.server != profile["server"]:
            raise ValueError("The HoYoLAB login's server does not match this Genshin profile.")
        path = self._tuskfizzy_session_path(account_id)
        TokenStore(path).save(StoredToken(dict(cookies), chosen.uid, chosen.server, chosen.nickname))
        self._tuskfizzy_note_cache.pop(account_id, None)
        self._tuskfizzy_chronicle_cache.pop(account_id, None)
        self.bus.publish("tuskfizzy_changed")
        return {"uid": str(chosen.uid), "nickname": chosen.nickname, "server": chosen.server}

    def tuskfizzy_logout(self, account_id: str) -> None:
        TokenStore(self._tuskfizzy_session_path(account_id)).delete()
        self._tuskfizzy_note_cache.pop(account_id, None)
        self._tuskfizzy_chronicle_cache.pop(account_id, None)
        self.bus.publish("tuskfizzy_changed")

    async def tuskfizzy_profile_chronicle(self, account_id: str) -> dict[str, Any] | None:
        """Read Battle Chronicle overview and endgame using this profile's own encrypted HoYoLAB session."""
        if self.demo:
            return None
        profile = next((item for item in self.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id), None)
        if profile is None:
            return None
        now = self.clock.now()
        cached = self._tuskfizzy_chronicle_cache.get(account_id)
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < 900:
            return dict(cached[1])
        tokens = TokenStore(self._tuskfizzy_session_path(account_id))
        token = tokens.load()
        if token is None or str(token.uid) != profile["uid"] or token.server != profile["server"]:
            return None
        await self._source_ready()
        from guildboard.sources.hoyolab import HoyolabSource
        source = HoyolabSource(tokens)
        overview_result = await source.fetch("overview", now=now)
        overview = overview_result.record
        if overview is None:
            return None
        endgame_result = await source.fetch("endgame", now=now)
        endgame = endgame_result.record
        value = {
            "uid": profile["uid"],
            "nickname": overview.nickname,
            "server": overview.server,
            "adventureRank": overview.adventure_rank,
            "abyssProgress": overview.abyss_progress,
            "counters": dict(overview.counters),
            "regions": [
                {"name": region.name, "progress": region.pct,
                 "areas": [{"name": area.name, "progress": area.pct} for area in region.areas]}
                for region in overview.regions
            ],
            "endgame": [
                {"mode": mode.mode, "label": mode.label, "hasData": mode.has_data,
                 "result": mode.result_text, "score": mode.score, "period": mode.period_label}
                for mode in (endgame.modes if endgame is not None else ())
            ],
            "readAt": overview_result.fetched_at.isoformat(),
        }
        self._tuskfizzy_chronicle_cache[account_id] = (now, value)
        return dict(value)

    def tuskfizzy_session_connected(self, account_id: str) -> bool:
        """Whether this Genshin profile has its own saved login, without exposing any cookie."""
        try:
            profile = next((item for item in self.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id), None)
            if profile is None:
                return False
            token = TokenStore(self._tuskfizzy_session_path(account_id)).load()
            return token is not None and str(token.uid) == profile["uid"] and token.server == profile["server"]
        except (OSError, ValueError):
            return False

    async def tuskfizzy_profile_note(self, account_id: str) -> dict[str, Any] | None:
        """Fetch a profile's own Daily Note using its own encrypted cookies, never the primary account's session."""
        if self.demo:
            return None
        profile = next((item for item in self.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id), None)
        if profile is None:
            return None
        now = self.clock.now()
        cached = self._tuskfizzy_note_cache.get(account_id)
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < 300:
            return dict(cached[1])
        tokens = TokenStore(self._tuskfizzy_session_path(account_id))
        token = tokens.load()
        if token is None or str(token.uid) != profile["uid"] or token.server != profile["server"]:
            return None
        await self._source_ready()
        from guildboard.sources.hoyolab import HoyolabSource
        source = HoyolabSource(tokens)
        result = await source.fetch("notes", now=now)
        record = result.record
        if record is None:
            return None
        note = {
            "profileName": profile["name"], "uid": profile["uid"],
            "resin": record.resin, "resinMax": record.resin_max,
            "commissionDone": record.commissions_done, "commissionTotal": record.commissions_max,
            "bonusClaimed": record.extra_reward_claimed,
            "expeditionsDone": record.expeditions_done, "expeditionsTotal": record.expeditions_total,
            "readAt": result.fetched_at.isoformat(),
        }
        self._tuskfizzy_note_cache[account_id] = (now, note)
        return dict(note)

    def logout(self) -> None:
        if self.auth is not None:
            self.auth.logout()

    def _auth_changed(self) -> None:
        # A login, or a logout: the keys of the login before are dropped, and a new login is asked for its own at once.
        self._login_session += 1
        self.forget_login_refusal()
        self.wish_import.drop_minted()
        self.wake_login_refresh()
        self.scheduler.clear_block()
        if self.has_token():
            self.scheduler.request("login")
        if self.has_token() and not self.settings.codes_login_done:
            # The first code of an account is refused (-1071) on a login older than HoYoverse likes; the Codes page asks
            # for one before it, until this has happened once.
            self.settings.codes_login_done = True
            save_settings(self.paths.settings_path, self.settings)
            self.codes_changed()
        self._map_session += 1
        self.map_changed()  # the map views sign in or out with it, and the Map page's chip follows

    async def _retry_pause(self, seconds: float) -> None:
        """The runner's pause before it tries an unreachable HoYoLAB again. A demo fault is there to
        show a state, not to make anyone wait two minutes, so the pause is skipped while one is set."""
        if self.demo and getattr(self.source, "fault", None) is not None:
            await asyncio.sleep(0)
            return
        await self._sleep(seconds)

    def request_window(self, kind: str) -> bool:
        return bool(self.window_requests is not None and self.window_requests(kind))

    def verification_done(self) -> None:
        self.scheduler.clear_block()

    async def backup_now(self) -> str:
        """A copy of the database in the backups folder, on request; its file name. It is made in a worker thread, through a
        connection of its own: a large database takes a while, and the store's one connection is the event loop's."""
        made = await asyncio.to_thread(
            database_copy, self.paths.db_path, self.paths.backups_dir, "manual", self.clock.now()
        )
        return made.name

    def backup_file_name(self) -> str:
        """The name the export offers: the core's date, in the player's own time zone."""
        return f"TUSKFiZZY backup {self.clock.now().astimezone().date().isoformat()}{BACKUP_SUFFIX}"

    async def export_backup(self, target: Path) -> str:
        """Write the backup file the player chose; the file name written. Refused in the game's folders, and no folder is
        made for it. The database's copy and its deflate take a while, so the file is written in a worker thread, the
        copy through a connection of its own."""
        if self.demo:
            raise BackupRefused(BACKUP_DEMO)
        folders, now = self._game_folders(), self.clock.now()

        def write() -> None:
            write_backup(self.paths.db_path, self.paths.settings_path, export_target(target, folders), now)

        await asyncio.to_thread(write)
        return target.name

    # ---- the Wrapped picture (the Stats design, 6.4)

    def hold_picture(self, name: str, data: bytes) -> None:
        """Keep the Wrapped poster the page made until the window's Save dialog has a place for it: in memory alone, one
        at most (a new one takes the place of the last), let go once saved or after ten minutes. ValueError for a name
        that is not the poster's, which the dialog offers, or for bytes that are not a PNG."""
        if PICTURE_NAME.fullmatch(name) is None or not data.startswith(PNG_SIGNATURE):
            raise ValueError(NOT_A_PICTURE)
        self.drop_picture()
        self._pictures_held += 1
        self._picture = (self._pictures_held, name, data, self.clock.now())
        with contextlib.suppress(RuntimeError):  # with no loop running, its age alone lets it go (_held_picture)
            self._picture_timer = asyncio.get_running_loop().call_later(PICTURE_HELD.total_seconds(), self.drop_picture)

    def picture_offer(self) -> tuple[int, str] | None:
        """The picture held as the Save dialog offers it: its number, which the save and a cancel name so that they are
        about this picture and no newer one, and its file name. None when none is held."""
        held = self._held_picture()
        return (held[0], held[1]) if held is not None else None

    def picture_name(self) -> str | None:
        """The name of the picture held, which the Save dialog offers; None when none is."""
        offer = self.picture_offer()
        return offer[1] if offer is not None else None

    def drop_picture(self, number: int | None = None) -> None:
        """Let the picture held go: the dialog was cancelled, the page found no window, or its time is up. With `number`,
        only when it is that picture: a newer one waits for a dialog of its own."""
        if number is not None and (self._picture is None or self._picture[0] != number):
            return
        if self._picture_timer is not None:
            self._picture_timer.cancel()
        self._picture, self._picture_timer = None, None

    def _held_picture(self) -> tuple[int, str, bytes, datetime] | None:
        if self._picture is not None and self.clock.now() - self._picture[3] >= PICTURE_HELD:
            self.drop_picture()
        return self._picture

    def save_picture(self, target: Path, number: int | None = None) -> str:
        """Write the picture held to the file the player chose; the file's name. With `number`, that picture only: one
        that a newer one took the place of while its dialog was open is not saved under its name (PICTURE_REPLACED), and
        the newer one stays for its own dialog. Refused in the game's folders by the backup export's own check
        (backups.export_target), in words of its own, and no folder is made for it. It is written under the name with .tmp
        and renamed when whole, so that an earlier file of that name stays until then. Saved, refused or failed, the
        picture is then let go: nothing could use it again. Nothing else is written."""
        held = self._held_picture()
        if held is None:
            raise BackupRefused(NO_PICTURE)
        if number is not None and held[0] != number:
            raise BackupRefused(PICTURE_REPLACED)
        try:
            try:
                place = export_target(target, self._game_folders())
            except BackupRefused:
                raise BackupRefused(PICTURE_GAME_FOLDER) from None
            partial = place.with_name(place.name + ".tmp")
            try:
                partial.write_bytes(held[2])
                os.replace(partial, place)
            except BaseException:
                partial.unlink(missing_ok=True)  # a picture cut short is no picture
                raise
        finally:
            self.drop_picture(held[0])
        return target.name

    def _game_folders(self) -> list[Path]:
        """The game's folders TUSKFiZZY knows without reading anything of the game's: the folder of its log, and the
        ScreenShot folder the gallery has, found through the log or pasted. Its install folder is not among them: the
        settings keep a folder that was found and one that was pasted alike, and the folder beside a pasted one may be a
        whole drive. An install folder is told by the program it holds (backups.export_target)."""
        folders = [gamecache.log_path().parent]
        for screenshots in (self.settings.gallery_folder, self.gallery.folder):
            if screenshots:
                folders.append(Path(screenshots))
        return folders

    def backup_list(self) -> list[dict[str, str]]:
        """The database copies in the backups folder, newest first: those a restore can use."""
        found = []
        for path in self.paths.backups_dir.glob("guildboard-*.db"):
            match = re.fullmatch(r"guildboard-(.+)-(\d{8})-(\d{6})\.db", path.name)
            if match is None:
                continue
            label, day, hour = match.groups()
            kind = next((k for k in ("daily", "manual", "before") if label.startswith(k)), "auto")
            made = datetime.strptime(day + hour, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            found.append({"name": path.name, "made_at": iso(made), "kind": kind})
        return sorted(found, key=lambda item: (item["made_at"], item["name"]), reverse=True)

    async def restore_from(self, source: Path) -> dict[str, bool]:
        """Stage a restore of a backup file, or of one of the copies in the backups folder, and ask the shell to restart
        TUSKFiZZY, which puts it in place at its start. Without a shell the player restarts it. Staging unpacks and
        checks a file of up to 200 MB, so it runs in a worker thread; it reads no store."""
        if self.demo:
            raise BackupRefused(BACKUP_DEMO)
        if source.suffix == ".db":
            await asyncio.to_thread(stage_database, source, self.paths.settings_path, self.paths.data_dir)
        else:
            await asyncio.to_thread(stage_restore, source, self.paths.data_dir)
        return {"restarting": self.request_window("restart")}

    def advance_demo_clock(self, seconds: float) -> datetime:
        """Demo mode only: move the demo clock forward, to show and test what the passing of time changes."""
        if not self.demo or not isinstance(self.clock, ShiftedClock):
            raise RuntimeError("only the demo clock can be moved")
        self.clock.advance(seconds)
        self.scheduler.publish_state()
        self.reminders.wake()  # timers may be ready now
        return self.clock.now()

    # ------------------------------------------------------------------ achievements
    def achievements(self, query: Query) -> dict[str, Any]:
        """The Achievements page model, with the installed list, HoYoLAB's count. The
        special categories carry their counts: the timers ready, and the commission achievements left in the region
        set in Settings."""
        catalog = self.lists.current()
        counted = self.store.latest_point("stats.achievements")
        return {
            **build_achievements(catalog, self.notes, self.store, query),
            "list": self.lists.meta(),
            "hoyolab": None if counted is None else {"count": int(counted[0]), "at": iso(counted[1])},
            "special": {
                "timers_ready": count_ready(self.timers, self.store, self.clock.now()),
                "commissions_left": count_left(catalog, self.notes, self.store, self.settings.commission_region),
            },
        }

    def commissions_model(self, region: str | None) -> dict[str, Any]:
        """The Daily commissions category's model for a region, or for the region set in Settings when it is None."""
        now, server = self.clock.now(), self.server()
        return build_commissions(
            self.lists.current(), self.notes, self.store,
            self.settings.commission_region if region is None else region, game_day(now, server), server, now,
        )

    def timers_model(self) -> dict[str, Any]:
        """The Timers category's model."""
        return build_timers(
            self.timers, self.lists.current(), self.notes, self.store, self.settings.fishing_rule, self.clock.now(),
            self.layouts.pinned_timers(),
        )

    def board_model(self) -> dict[str, Any]:
        """The Board model (views/board.py), with the game's running state as status() reports it."""
        return build_board(
            self.lists.current(), self.store, self.history, self.server(), self.clock.now(), self.tracker.running,
            self.tracker.session_started_at,
        )

    async def read_import(self, text: str | None, file: str | None, profile: str | None) -> importers.ImportSource:
        """The file to import: the text of a file the player chose. A file of up to 30 MB is decoded and read in a worker
        thread; it reads no store, and the plan that does stays on the event loop's thread."""
        if text is None:
            raise importers.ImportFileError("Choose a file to import.")
        # The page's JSON can carry half of an emoji, which UTF-8 cannot encode and no answer can hold.
        return await asyncio.to_thread(
            lambda: importers.read_file(valid_text(text).encode("utf-8"), valid_text(file or "the chosen file"), profile)
        )

    async def preview_import(self, text: str | None, file: str | None, profile: str | None) -> dict[str, Any]:
        """What importing the file would change. Nothing is saved."""
        source = await self.read_import(text, file, profile)
        return importers.plan_import(source, self.lists.current(), self.store, self.clock.now()).summary()

    async def apply_import(
        self, text: str | None, file: str | None, profile: str | None, fingerprint: str
    ) -> dict[str, Any]:
        """Import the file that was previewed. It is read and planned again: a plan writes whole rows, and the
        preview's would undo any change made since."""
        source = await self.read_import(text, file, profile)
        if source.fingerprint != fingerprint:
            raise importers.ImportChanged("The file changed since the preview. Look at the preview again.")
        now = self.clock.now()
        plan = importers.plan_import(source, self.lists.current(), self.store, now)
        summary = importers.apply_import(plan, self.store, now)
        self.achievements_changed()
        return summary

    def achievements_changed(self) -> None:
        """Tell the pages that the achievements changed. The API calls it after every change it makes."""
        self.bus.publish("achievements_changed")

    def timers_changed(self) -> None:
        """Tell the pages that the timers changed, and the reminders to look again. The API calls it after every
        change it makes to them."""
        self.bus.publish("timers_changed")
        self.reminders.wake()

    def layout_changed(self, place: str) -> None:
        """Tell the pages that the layout of a place changed: the page that shows it reads it again."""
        self.bus.publish("layout_changed", place=place)

    def pulls_changed(self) -> None:
        """Tell the pages that what the Pulls page shows changed (a setting, the balance, the wishlist, the banners or
        the history): the pages read /api/pulls again. The API and the Pulls modules call it after every change."""
        self.bus.publish("pulls_changed")

    # ------------------------------------------------------------------ Characters (the Characters plan, the core's seams)
    def characters_changed(self) -> None:
        """Tell the pages that what the Characters pages show changed (the focus, a plan, a tick, a card, a typed amount,
        the Theater's pick, or what HoYoLAB or the wiki gave): they read /api/characters, /api/week and /api/endgame again."""
        self.bus.publish("characters_changed")

    def refresh_characters(self) -> dict[str, Any]:
        """Ask the scheduler for a `characters` run, within its limits. It answers {"started": True}; or
        {"started": False, "wait_minutes": N} while the gap between runs or the day's 24 are in the way; or
        {"started": False, "login": True} without a HoYoLAB login; or {"started": False, "blocked": state} when the sync
        is paused or waits for the player (the scheduler's state)."""
        if not self.has_token():
            return {"started": False, "login": True}
        wait = self.scheduler.request_group("characters")
        if wait is None:
            return {"started": True}
        if wait == 0:
            return {"started": False, "blocked": self.scheduler.current_state()}
        return {"started": False, "wait_minutes": wait}

    def _characters_sync(self) -> dict[str, Any]:
        """What the Characters pages need of the sync to say whether Refresh works: it is under way, how long until a run
        may start, and what stops it (paused, or waiting for the login or a verification check)."""
        state = self.scheduler.current_state()
        return {
            "reading": state == "syncing", "login": not self.has_token(),
            "wait_seconds": self.scheduler.manual_wait_seconds() or self.scheduler.seconds_until_runs_count_again(),
            "blocked": state if state in ("paused", "login_needed", "verification_needed") else None,
        }

    def _characters_notify(self, kind: str, text: str) -> bool:
        """A notice of the Characters pages, in each way the player has on. With every way off it counts as given, so that
        turning one on brings no burst of old notices."""
        self.remind(kind, text)
        return True

    def _characters_switches(self) -> dict[str, bool]:
        """The Characters notices' switches, by kind (the notices' own names for them)."""
        settings = self.settings
        return {
            "weekly": settings.characters_notice_weekly, "domain": settings.characters_notice_domain,
            "ready": settings.characters_notice_ready,
        }

    def _game_version(self) -> str:
        """The game's version number today, "7.1", from the calendar of reference/versions.json: one known version, the
        day it started and how many days each lasts. The wiki's entries are kept by it."""
        calendar = nextversion.load_reference()["calendar"]
        major, minor = calendar["version"].split(".")
        ahead = max(0, (self.today() - date.fromisoformat(calendar["started"])).days // calendar["cadence_days"])
        return f"{major}.{int(minor) + ahead}"

    # ------------------------------------------------------------------ gift codes (the gift codes design, section 1)
    def _codes_uid(self) -> str | None:
        """The account whose codes these are: the one the core knows, None before a login."""
        account = self._account()
        return str(account["uid"]) if account is not None and account.get("uid") is not None else None

    def codes_model(self) -> dict[str, Any]:
        uid = self._codes_uid()
        history = self.codebook.history(uid) if uid else []
        return {
            "totals": self.codebook.totals(uid) if uid else {"primogems": 0, "redeemed": 0},
            "history": history,
            "live": self.codebook.new_live(uid, self._live_codes) if uid else [],
            "busy": self._codes_send is not None and not self._codes_send.done(),
            "progress": [dict(entry) for entry in self._codes_progress],
            "needs_login": uid is None or not self.has_token(),
            # Until a code has gone through on this account or a login has finished, the page asks for a login first.
            "login_first": not self.settings.codes_login_done and not any(row["result"] in SETTLED for row in history),
        }

    def codes_changed(self) -> None:
        self.bus.publish("codes_changed")

    def set_code_primogems(self, code: str, primogems: int) -> bool:
        """The primogems a code gave, as the player corrected them. False when the account has no such code."""
        uid = self._codes_uid()
        if uid is None or not self.codebook.set_primogems(uid, code.upper(), primogems):
            return False
        self.codes_changed()
        return True

    def remove_code(self, code: str) -> bool:
        """Take a code out of the history. False when the account has no such code."""
        uid = self._codes_uid()
        if uid is None or not self.codebook.remove(uid, code.upper()):
            return False
        self.codes_changed()
        return True

    def redeem_codes(self, text: str, primogems: int | None) -> bool:
        """Start sending the codes in `text`, one by one. The player typed, pasted or clicked them: nothing else
        starts this. Only the words shaped as a gift code are codes, and none of a pasted login: any other is never sent
        or kept, and the run's progress says how many were skipped. False while another run is busy; CodesRefused where
        it cannot be done."""
        codes = [(code, primogems or 0) for code in split_codes(text)]
        return self._start_codes(codes, "typed", skipped=skipped_words(text), nothing=nothing_sent(text))

    def redeem_live(self, shown: list[str]) -> bool:
        """Start sending the new livestream codes the page showed when the player pressed Redeem all, each with the primogems
        the guide gave: a code that came since is not among them, and one shown that is settled since is not sent."""
        uid = self._codes_uid()
        live = self.codebook.new_live(uid, self._live_codes) if uid else []
        wanted = set(shown)
        return self._start_codes([(item["code"], item["primogems"]) for item in live if item["code"] in wanted], "livestream")

    def _start_codes(
        self, codes: list[tuple[str, int]], source: str, skipped: int = 0, nothing: str = NO_CODE,
    ) -> bool:
        """Start the run, `skipped` words having been left out of `codes` as no gift codes: the progress says so last.
        With no code at all, CodesRefused says `nothing`."""
        if self.demo:
            raise CodesRefused("The demo does not redeem codes.")
        uid = self._codes_uid()
        if uid is None or not self.has_token():
            raise CodesRefused(RESULT_TEXT["login"])
        if self._codes_send is not None and not self._codes_send.done():
            return False
        if not codes:
            raise CodesRefused(nothing)
        self._codes_progress = [{"code": code, "state": "waiting", "text": "Waiting"} for code, _ in codes]
        if skipped:
            self._codes_progress.append({"code": "", "state": "skipped", "text": skipped_sentence(skipped)})
        self._codes_send = asyncio.create_task(self._send_codes(uid, codes, source))
        self.codes_changed()
        return True

    def _step(self, index: int, state: str, text: str, **more: Any) -> None:
        self._codes_progress[index] = {"code": self._codes_progress[index]["code"], "state": state, "text": text, **more}
        self.codes_changed()

    async def _send_codes(self, uid: str, codes: list[tuple[str, int]], source: str) -> None:
        """Send the codes one by one, PAUSE_SECONDS apart. A code already settled on this account is not sent again."""
        login_lost = False
        try:
            for index, (code, primogems) in enumerate(codes):
                settled = self.codebook.blocked(uid, code)
                if settled is not None:
                    when = datetime.fromisoformat(settled["at"]).astimezone()
                    self._step(index, settled["result"], f"You redeemed this code on {when.day} {when:%b}.", at=settled["at"])
                    continue
                if login_lost:
                    self._step(index, "login", RESULT_TEXT["login"])
                    continue
                if self._last_sent_at is not None:  # also from an earlier run
                    wait = PAUSE_SECONDS - (self.clock.now() - self._last_sent_at).total_seconds()
                    if wait > 0:
                        await self._sleep(wait)
                self._step(index, "sending", "Sending")
                await self._source_ready()
                result = await self.source.redeem(code)
                self._last_sent_at = self.clock.now()
                if result in KEPT_RESULTS:
                    given = primogems if result in ("redeemed", "already") else 0
                    self.codebook.record(uid, code, result, given, source, self.clock.now())
                login_lost = result == "login"
                self._step(index, result, RESULT_TEXT.get(result, RESULT_TEXT["failed"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sending the gift codes failed")
            for index, entry in enumerate(self._codes_progress):
                if entry["state"] in ("waiting", "sending"):
                    self._step(index, "failed", RESULT_TEXT["failed"])
        finally:
            self.codes_changed()

    def look_for_live(self) -> None:
        """Start a look at the livestream codes in the background, unless one is running."""
        if self._live_look is None or self._live_look.done():
            self._live_look = asyncio.create_task(self._look_safely())

    async def _look_safely(self) -> None:
        try:
            await self.check_live()
        except Exception:
            log.exception("the look at the livestream codes failed")

    async def check_live(self, *, force: bool = False) -> None:
        """Look at the livestream codes. New ones that the player was not told about give one notification."""
        now = self.clock.now()
        if not force and self._live_checked_at is not None and (now - self._live_checked_at).total_seconds() < LIVE_GAP:
            return
        if not (self.demo or self.settings.risk_acknowledged):
            return  # nothing is asked of HoYoverse before the player's first-run consent (rulings R15 and R21)
        try:
            await self._source_ready()
            look = getattr(self.source, "livestream_codes", None)
        except SourceBuildFailed:
            return
        if look is None:
            return
        self._live_checked_at = now
        try:
            live = await look()
        except Exception as error:  # offline, or HoYoLAB answered something else: asked again later
            log.info("Could not look for livestream codes (%s)", netpolicy.what_failed(error))
            return
        # A livestream code is held to the typed codes' shape: anything else is never shown, sent or kept.
        shaped = [item for item in live if isinstance(item, dict) and is_code(item.get("code"))]
        if len(shaped) < len(live):
            log.info("%d of HoYoLAB's livestream codes are left out: they are not shaped as gift codes.",
                     len(live) - len(shaped))
        live = shaped
        changed = live != self._live_codes
        self._live_codes = live
        uid = self._codes_uid()
        if uid is not None:
            fresh = self.codebook.unnoticed(self.codebook.new_live(uid, live))
            if fresh:
                self.codebook.mark_noticed([item["code"] for item in fresh], now)
                if self.settings.notifications:
                    self.notify("codes", (
                        "1 new livestream code. Open TUSKFiZZY to redeem it." if len(fresh) == 1
                        else f"{len(fresh)} new livestream codes. Open TUSKFiZZY to redeem them."
                    ))
        if changed:
            self.codes_changed()

    async def _watch_codes(self) -> None:
        # Real waits, as the banners' are: the injected sleep of the tests would run the clock on and on.
        while True:
            try:
                await self.check_live(force=True)
            except Exception:  # one failed look must not end the hourly ones
                log.exception("the look at the livestream codes failed")
            await asyncio.sleep(LIVE_EVERY)

    async def _daily_backups(self) -> None:
        """The daily copy for the date a TUSKFiZZY left running reaches: the start makes the first of a day, and one kept in
        the tray for a week would make none after it. Real waits, as the banners' are. The copy is cheap while it is made on
        the loop's thread (SQLite's backup API, about 20 ms for 18 MB), and the connection is the loop's alone."""
        while True:
            await asyncio.sleep(DAILY_CHECK_EVERY)
            try:
                self.store.daily_backup(self.clock.now())
            except (OSError, sqlite3.Error, StoreError):
                log.warning("The daily backup could not be made", exc_info=True)

    # ------------------------------------------------------------------ the Map page's tabs (the Map design)
    def map_model(self) -> dict[str, Any]:
        """What the Map page shows: the tabs, and whether the map views have a HoYoLAB session to sign in with."""
        signed_in = self.auth is not None and self.auth.current() is not None
        return {**self.maptabs.model(), "signed_in": signed_in, "has_shell": self.window_requests is not None}

    def map_session(self) -> dict[str, str] | None:
        """The session cookies the shell puts into the map views' own cookie store, or None without a login. Only the
        shell asks, in the same process; no API route returns them."""
        token = self.auth.current() if self.auth is not None else None
        return dict(token.cookies) if token is not None else None

    def map_role(self) -> dict[str, str] | None:
        """The game account and server the stored login chose, which the map would otherwise ask for at every start. Only
        the shell asks, with the session; no API route returns it."""
        token = self.auth.current() if self.auth is not None else None
        return {"uid": str(token.uid), "region": token.server} if token is not None else None

    def map_snapshot(self) -> dict[str, Any]:
        """What the shell follows: the tabs and the area (maptabs.snapshot), and `session`, which changes at every login
        and logout. The cookies themselves are never in it."""
        return {**self.maptabs.snapshot(), "session": self._map_session}

    def map_changed(self, *, page: bool = True) -> None:
        """After a change to the tabs or to the map's area: the shell hears of it, and with `page` the pages too."""
        snapshot = self.map_snapshot()
        for listener in list(self.map_listeners):
            try:
                listener(snapshot)
            except Exception:  # the shell's failure must not fail the player's request
                log.exception("a map listener failed")
        if page:
            self.bus.publish("map_changed")

    def map_seen(self, tab_id: int, url: str, title: str) -> None:
        """The shell reports what a map view shows; only a change the page shows (a title) is announced."""
        if self.maptabs.seen(tab_id, url, title):
            self.bus.publish("map_changed")

    def map_failed(self, tab_id: int, failed: bool) -> None:
        if self.maptabs.set_failed(tab_id, failed):
            self.bus.publish("map_changed")

    # ------------------------------------------------------------------ Pulls: the import's sources (Part B)
    async def _after_import(self, uid: int | None) -> None:
        """After an import that ended well: the banners are read again, and the icons of the names the page shows
        (the account's 4★ and 5★ and wishlist, and the banners' featured names) when the wiki's lists kept lack one."""
        await self.banners.refresh(force=True)
        await self.wiki.refresh(wanted_names(self.store, uid))

    def icons_wanted(self) -> None:
        """After a banners read that kept a new featured name, and after an entry was added to the wishlist: the icons of
        the names the shown account's page shows are read, when one lacks its icon, at most once a day, and the pages are
        told. A task of its own, which neither waits for; one at a time. Nothing is read before the first run's consent
        (rulings R15 and R21), whatever history is kept. The demo's wiki reads nothing."""
        if not self.settings.risk_acknowledged:
            return
        if self._icons_task is None or self._icons_task.done():
            self._icons_task = asyncio.create_task(self._read_icons())

    async def _read_icons(self) -> None:
        try:
            if await self.wiki.refresh(wanted_names(self.store, self.pulls.uid()), daily=True):
                self.pulls_changed()
        except Exception:  # the icons are asked for again at the next import
            log.exception("the icons could not be read")

    # ------------------------------------------------------------------ Pulls: updates with the link the importer kept
    def _start_auto_updates(self) -> None:
        """The game started: one task asks the importer for an update every AUTO_EVERY, for as long as the game runs.
        The importer does something only while it keeps the link of an import that ended well."""
        if self._auto_task is not None:
            self._auto_task.cancel()
        self._auto_task = self._spawn(self._auto_updates())

    def _stop_auto_updates(self) -> None:
        """The game stopped: that task ends, and the importer is asked for one more update."""
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
        self._last_auto_task = self._spawn(self._auto_update())

    async def _auto_updates(self) -> None:
        while True:
            await self._sleep(AUTO_EVERY.total_seconds())
            await self._auto_update()

    async def _auto_update(self) -> None:
        if self._login_refresh_on():
            return  # the updates with the login come every AUTO_EVERY, the game running or not, and use a kept link too
        try:
            await self.wish_import.auto_update()
        except Exception:  # an update that broke must not end the next ones
            log.exception("the update of the wish history with the kept link failed")

    # ------------------------------------------------------------------ the source
    @property
    def source(self) -> Any:
        """The source the sync, the check-in and the codes read from. The live one is made the first time this is read: the
        HoYoLAB library takes about a second to load, which a start must not wait for. A start makes it in a thread
        (`start`), and what uses it awaits `_source_ready` first. SourceBuildFailed when it could not be made."""
        if self._source is None and self._make_source is not None:
            return self._build_source()
        return self._source

    @source.setter
    def source(self, value: Any) -> None:
        self._source, self._make_source = value, None

    def _build_source(self) -> Any:
        with self._source_lock:  # two first uses at once make one source
            if self._source is None:
                if self._source_failed:
                    raise SourceBuildFailed(SOURCE_BUILD_FAILED)
                try:
                    self._source = self._make_source()  # type: ignore[misc]
                except Exception as error:
                    log.exception("The HoYoLAB library could not be loaded")
                    self._source_failed = True
                    raise SourceBuildFailed(SOURCE_BUILD_FAILED) from error
            return self._source

    def _holder(self) -> Any:
        """What is given to the parts made before the live source is: the source itself, or a stand-in that finds it."""
        return _CoreSource(self) if self._make_source is not None else self._source

    async def _source_ready(self) -> None:
        """Wait, off the loop, until the live source is made. What uses the source first awaits this: nothing but the
        warm-up of a start then waits for the library, and no page does. SourceBuildFailed when it could not be made."""
        if self._source is None and self._make_source is not None:
            await asyncio.to_thread(self._build_source)

    async def _warm_source(self) -> None:
        with contextlib.suppress(SourceBuildFailed):  # said in the log, and by each use
            await self._source_ready()

    async def _list_accounts(self, cookies: Mapping[str, str]) -> Any:
        await self._source_ready()
        return await self.source.list_accounts(cookies)

    # ------------------------------------------------------------------ Pulls and Ledger: updates with the login
    def _login_refresh_ready(self) -> bool:
        """Whether the histories can update themselves with keys HoYoverse gives the HoYoLAB login: a source that can ask
        for them, a login that holds the cookie token (the demo's stand-in always does), and the first run's consent. The
        token store keeps the login it read, so this decrypts nothing. Until the live source is made it is answered from
        the stored login, by the rule the source has (HoyolabSource.can_mint), so that no page waits for the library."""
        if not (self.demo or self.settings.risk_acknowledged):
            return False
        if self._source is None and self._make_source is not None:
            token = self.tokens.load()
            return token is not None and REDEEM_TOKEN in token.cookies
        try:
            can_mint = getattr(self.source, "can_mint", None)
        except SourceBuildFailed:
            return False
        return can_mint is not None and bool(can_mint())

    def _login_refresh_on(self) -> bool:
        """Whether they do now: ready, with no refusal of the login since it was made, and the sync not paused."""
        return (
            self._login_refresh_ready() and not self._login_refused and not self.settings.sync_paused
            and self._login_of_another_account() is None
        )

    def _login_status(self) -> str | None:
        """The pages' word on the updates with the login (the importer adds how its own go): None where nothing is read
        with it (no login that can be given keys, the sync paused, or a login of another account), "refused" once HoYoverse
        refused the login, else "on". A refusal is said even while the sync is paused: the player has to log in again."""
        if not self._login_refresh_ready():
            return None
        if self._login_refused:
            return "refused"
        return "on" if self._login_refresh_on() else None

    def forget_login_refusal(self) -> None:
        """A new login, or the demo's fault ended: the updates with the login may ask for keys again."""
        if self._login_refused:
            self._login_refused = False
            self.pulls_changed()

    async def refresh_from_login(self) -> bool:
        """Update the wish history and the game's own history with keys HoYoverse gives the login, reading no file of the
        game's (WishImporter.login_update). Whether it ran. It never raises: a key that cannot be had fails nothing else.
        A refusal of the login stops these updates alone, until a new login, without asking again, and the pages say so;
        the sync goes on with the login as it can. A key asked for while the login changed is not kept."""
        if not self._login_refresh_on():
            return False
        try:
            await self._source_ready()
        except SourceBuildFailed:
            return False
        session = self._login_session
        try:
            return await self.wish_import.login_update(self.source.mint_key, lambda: self._login_session == session)
        except LoginNeeded:
            if self._login_session == session:
                self._login_refused = True
                log.info("HoYoverse refused the saved login for a history key: the automatic updates of the histories "
                         "stop until a new login. Import still works.")
                self.pulls_changed()  # the pages say so
        except Exception:
            log.exception("the update of the histories with the login failed")
        return False

    async def import_pressed(self) -> str:
        """The player pressed Import (the freshness design, section 5). Where the histories can update themselves with
        keys HoYoverse gives the HoYoLAB login, the press is that very run (refresh_from_login), which reads no file of
        the game's; if it did not run, or HoYoverse refused the login on this press, the import from the game's files runs
        in its place. A run that started and failed says so itself, through the importer: it is not run again from the
        game's files. Without the login's updates (no login, a login of another account, the sync paused, a login
        refused before), it is the import from the game's files. The way taken: "login" or "game". The work runs on as
        a task. ImportBusy while an import or an update runs, ImportNotReady before the first run's consent, both before
        any task starts."""
        self.wish_import.ensure_pressable()
        if self._import_task is not None and not self._import_task.done():
            raise ImportBusy()  # the press before this one has not begun to run yet
        self.wish_import.pressed()
        if not self._login_refresh_on():
            self.wish_import.start()
            return "game"
        self._import_task = self._spawn(self._import_with_login())
        await asyncio.sleep(0)  # the task runs as far as its first wait: it holds the importer before the route answers
        return "login"

    async def _import_with_login(self) -> None:
        if await self.refresh_from_login() or self.wish_import.running:
            return
        try:
            self.wish_import.start()  # the login could not be had: the game's files, on this press
        except (ImportBusy, ImportNotReady):
            return  # an import began meanwhile, or the consent was taken back: it says so itself

    def _history_notice(self) -> dict[str, Any] | None:
        """That the game's history stopped updating, and what to do (freshness.py). The demo's stands still; a sync the player
        paused, or a login of another account, says so itself."""
        if self.demo:
            return self._demo_history_notice
        if self.settings.sync_paused or self._login_of_another_account() is not None:
            return None
        login = self._login_status()
        session = self.store.last_session()
        played_at = self.clock.now() if self.tracker.running else (session.ended_at if session is not None else None)
        how = "failing" if login == "on" else "login" if login == "refused" or self.has_token() else "feedback"
        return history_notice(self.store.last_ledger_import(self.pulls.uid()), played_at, self.clock.now(), how)

    def tell_of_old_history(self) -> None:
        """A notification that the history stopped updating, once for each last read: it is not asked again by a restart."""
        notice = self._history_notice()
        if notice is None or not self.settings.notifications:
            return
        read = iso(self.store.last_ledger_import(self.pulls.uid()))
        if self.store.meta(HISTORY_TOLD) != read and self.notify("history", notice["text"]):
            self.store.set_meta(HISTORY_TOLD, read)

    def wake_login_refresh(self) -> None:
        """Update the histories with the login now, not at the next AUTO_EVERY: after a login, the first run's consent,
        or the demo's ask."""
        self._refresh_wake.set()

    async def _syncs_ended(self) -> None:
        """Wait until no sync runs. asyncio.wait never cancels what it waits for, so a cancelled wait leaves the sync be."""
        while self.scheduler.running_syncs():
            await asyncio.wait(self.scheduler.running_syncs())
            await asyncio.sleep(0)  # the ended run leaves the scheduler's set on the loop's next turn

    async def _login_refreshes(self) -> None:
        """At the start, once the first sync has run, and then every AUTO_EVERY or when woken: the histories are updated
        with the login. The demo's history is current at its start, so its first waits."""
        now = not self.demo
        while True:
            if now:
                self._refresh_wake.clear()
                await self._syncs_ended()
                await self.refresh_from_login()
                self.tell_of_old_history()
            now = True
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._refresh_wake.wait(), AUTO_EVERY.total_seconds())

    @staticmethod
    def _spawn(work: Coroutine[Any, Any, None]) -> asyncio.Task[None] | None:
        """Run `work` as a task of the running event loop. Where none runs (a test that polls for the game by hand,
        outside any loop) nothing could run it, and it is not started."""
        try:
            return asyncio.get_running_loop().create_task(work)
        except RuntimeError:
            work.close()
            return None

    # ------------------------------------------------------------------ the overlay
    def _game_started(self) -> None:
        self._start_auto_updates()
        try:
            self.scheduler.on_game_started()
        finally:  # the overlay shows over the game whatever the sync's plans do
            self._tell_game_listeners(True)

    def _game_stopped(self, counted: bool) -> None:
        self._stop_auto_updates()
        try:
            self.scheduler.on_game_stopped(counted)
        finally:
            self._tell_game_listeners(False)

    def _tell_game_listeners(self, running: bool) -> None:
        for listener in list(self.game_listeners):
            try:
                listener(running)
            except Exception:  # one listener's failure must not stop the others, or the watch of the game
                log.exception("a game listener failed")

    def overlay_shell(self, displays: list[dict[str, Any]], hotkey_state: str) -> None:
        """What the shell reports about the overlay: the displays it found, each as {name, label, primary}, and
        whether the hotkey is "held" by the shell, "taken" by another program or "none". The Overlay settings show
        them, so the pages are told."""
        if hotkey_state not in HOTKEY_STATES:
            raise ValueError(f"The hotkey's state is {', '.join(HOTKEY_STATES[:-1])} or {HOTKEY_STATES[-1]}.")
        self._overlay_report = ([dict(display) for display in displays], hotkey_state)
        self.bus.publish("overlay_changed")

    def window_state(self, maximized: bool) -> None:
        """Whether TUSKFiZZY's window is maximized, as the shell reports it each time it changes: the Board shows its
        arrangement for that window, so the pages are told."""
        self._window_maximized = maximized
        self.bus.publish("window_changed")

    def overlay_status(self) -> dict[str, Any]:
        """What the Overlay settings show: whether there is a shell with an overlay, the hotkey and whether the shell
        holds it, the displays the shell found, and the display chosen."""
        displays, hotkey_state = self._overlay_report if self._overlay_report is not None else ([], "none")
        return {
            "available": self._overlay_report is not None,
            "hotkey": self.settings.overlay_hotkey,
            "hotkey_state": hotkey_state,
            "displays": [dict(display) for display in displays],
            "display": self.settings.overlay_display,
        }

    def overlay_line(self, text: str) -> None:
        """Tell the pages to show a line over the game: they draw it on the overlay for a few seconds."""
        self.bus.publish("overlay_line", text=text)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.store.clear_changes()  # an undo reaches only this visit (the top bar design, section 6)
        self._watch_task = asyncio.create_task(self.gamewatch.run())
        self._reminders_task = asyncio.create_task(self.reminders.run())
        self._banners_task = asyncio.create_task(self.banners.run())
        self._update_task = asyncio.create_task(self.updater.run())
        self._codes_watch = asyncio.create_task(self._watch_codes())
        self._characters_task = asyncio.create_task(self.characters.run())
        self._online_task = asyncio.create_task(self.online.run())
        self.scheduler.start()
        self._refresh_task = asyncio.create_task(self._login_refreshes())  # after the start's sync is asked for
        if self._make_source is not None:  # the live source loads in a thread, and its first users wait for that
            self._warm_task = asyncio.create_task(self._warm_source())
        if not self.demo:
            self._daily_task = asyncio.create_task(self._daily_backups())

    async def stop(self) -> None:
        tasks = (
            self._watch_task, self._reminders_task, self._banners_task, self._icons_task, self._auto_task,
            self._last_auto_task, self._update_task, self._codes_watch, self._codes_send,
            self._live_look, self._refresh_task, self._characters_task, self._online_task, self._daily_task,
            self._import_task, self._endgame_art_task, self._warm_task,
        )
        for task in tasks:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._watch_task = self._reminders_task = self._banners_task = self._icons_task = None
        self._auto_task = self._last_auto_task = self._update_task = self._codes_watch = self._codes_send = self._live_look = None
        self._refresh_task = self._characters_task = self._online_task = self._daily_task = self._import_task = None
        self._endgame_art_task = self._warm_task = None
        await self.checkin.stop()
        await self.online.stop()
        await self.characters.stop()
        await self.updater.stop()
        await self.wish_import.stop()  # an import cut short is recorded as failed at the next start
        self.wish_import.drop_link()  # the key of the link it kept goes with TUSKFiZZY
        await self.scheduler.stop()
        self.store.close()
