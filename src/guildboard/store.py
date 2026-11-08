"""SQLite storage: schema, migrations with a backup first, and the read and write functions the other units use."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from guildboard.logging_setup import redact
from guildboard.metrics import Label, Point

log = logging.getLogger(__name__)

AUTO_BACKUPS_KEPT = 5
DAILY_BACKUPS_KEPT = 14
SKIPPED_RUNS_KEPT_DAYS = 90  # a refused trigger leaves a record, and only the recent ones say anything
RAW_FULL_DAYS = 30  # how long every raw body is kept; after that, the last one of each endpoint on each game day
BUSY_SECONDS = 5.0  # how long to wait for another program that is writing to the database
# How an import of the wish history that TUSKFiZZY's closing cut short ended, as the next start records it.
IMPORT_CUT_SHORT = "TUSKFiZZY closed before this import finished."
CHAR_FOCUS_MAX = 4  # the Characters pages' focus (the plan's FOCUS_MAX, which the store may not import)

# What schema 12 watches for sync (the online plan, Task C2): the tables the player makes rows in, each with the columns that
# name a row across PCs, and the columns that make up a change of it (every column but those that stay on this PC: the numbers
# a PC draws for its own rows, and the reminders it sent). A table with `sync_key` in the first place has no key but that one,
# drawn for each row (a pre-set timer's is its pre-set's name). Frozen here: the migration is what it was when it was made.
_SYNCED_AT_12: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("achievement_progress", ("stage_id",), ("status", "done_at", "pinned", "note", "source", "updated_at")),
    ("achievement_parts", ("stage_id", "part"), ("done_at",)),
    ("achievement_counters", ("group_id",), ("value", "updated_at")),
    ("commission_log", ("stage_id", "day"), ("at", "source")),
    ("timers", ("sync_key",), ("preset", "name", "place", "rule", "steps", "bell", "ready_override", "created_at", "updated_at")),
    ("quick_timers", ("sync_key",), ("seconds", "started_at", "ready_at")),
    ("layouts", ("place",), ("widgets", "revision", "updated_at")),
    ("widget_data", ("widget_id",), ("data", "updated_at")),
    (
        "pulls_accounts", ("uid",),
        (
            "charted_weapon", "chronicled_path", "balance_primogems", "balance_fates", "balance_set_at", "corrections", "points",
            "welkin", "gnostic_hymn", "events_typed",
        ),
    ),
    ("wishlist", ("sync_key",), ("uid", "name", "kind", "about", "copies", "position", "created_at")),
    ("gift_codes", ("uid", "code"), ("result", "primogems", "source", "at")),
    ("sessions", ("sync_key",), ("started_at", "ended_at", "started_before_tool")),
    ("char_focus", ("uid", "position"), ("character",)),
    ("char_plans", ("uid", "character"), ("plan", "updated_at")),
    ("char_card_edits", ("uid", "character"), ("edits", "updated_at")),
    ("char_ticks", ("uid", "day", "task"), ("done_at",)),
    ("char_held", ("uid", "material"), ("amount", "set_at")),
    ("char_theater", ("uid", "month"), ("elements", "set_at")),
)
_SYNCED_KEYS = {table: key for table, key, _ in _SYNCED_AT_12}
_SYNC_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_SYNC_OPEN = "(SELECT applying FROM sync_gate) = 0"  # a row that another PC sent is being kept: no line for it
_LINE = chr(10)


def _sync_key_drawn(table: str, row: str = "") -> str:
    """The key of a new row of a table that numbers its rows for itself. A pre-set timer's is its pre-set's name, so that the
    rows two PCs make of one pre-set are one row."""
    if table == "timers":
        return f"CASE WHEN {row}preset IS NOT NULL THEN 'preset:' || {row}preset ELSE lower(hex(randomblob(16))) END"
    return "lower(hex(randomblob(16)))"


def _sync_line(table: str, row: str, deleted: int) -> str:
    """The statement that leaves a line for a row: one for each kind and key, the last thing done and when."""
    return (
        "INSERT OR REPLACE INTO sync_outbox (kind, row_key, edited_at, deleted) "
        f"VALUES ('{table}', json_array({row}), {_SYNC_NOW}, {deleted});"
    )


def _sync_migration() -> str:
    """Schema 12 as one script. The four tables whose rows a PC numbers for itself gain a `sync_key`, which every row that is
    there gets before the triggers exist, so that the migration leaves the outbox empty. Then each table gets three triggers: a
    change the player makes leaves one line in `sync_outbox` for its row. While `sync_gate` is shut nothing is written, so that
    the rows another PC sent are not sent back. A column that stays on this PC does not make a change. (`INSERT OR REPLACE`
    removes a row without a trigger: the store's statements that put a row back under its id keep its key themselves.)"""
    script = [
        "CREATE TABLE sync_outbox (kind TEXT NOT NULL, row_key TEXT NOT NULL, edited_at TEXT NOT NULL,",
        "    deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, row_key)) WITHOUT ROWID;",
        "CREATE TABLE sync_gate (id INTEGER PRIMARY KEY CHECK (id = 1), applying INTEGER NOT NULL DEFAULT 0);",
        "INSERT INTO sync_gate (id, applying) VALUES (1, 0);",
    ]
    for table, key, _ in _SYNCED_AT_12:
        if key == ("sync_key",):
            script += [
                f"ALTER TABLE {table} ADD COLUMN sync_key TEXT;",
                f"UPDATE {table} SET sync_key = {_sync_key_drawn(table)};",
                f"CREATE UNIQUE INDEX {table}_by_sync_key ON {table} (sync_key);",
            ]
    for table, key, changes in _SYNCED_AT_12:
        new, old = (", ".join(f"{side}.{column}" for column in key) for side in ("NEW", "OLD"))
        if key == ("sync_key",):
            # The key is drawn once the row is there, and the line is made from the row as it is then.
            insert = [
                f"UPDATE {table} SET sync_key = {_sync_key_drawn(table, 'NEW.')} WHERE rowid = NEW.rowid AND sync_key IS NULL;",
                "INSERT OR REPLACE INTO sync_outbox (kind, row_key, edited_at, deleted) "
                f"SELECT '{table}', json_array(sync_key), {_SYNC_NOW}, 0 FROM {table} WHERE rowid = NEW.rowid;",
            ]
            update, watched = [_sync_line(table, new, 0)], changes
        else:
            insert = [_sync_line(table, new, 0)]
            # A key that was changed names a row that is gone, and a row that is new.
            update = [
                "INSERT OR REPLACE INTO sync_outbox (kind, row_key, edited_at, deleted) "
                f"SELECT '{table}', json_array({old}), {_SYNC_NOW}, 1 WHERE json_array({old}) IS NOT json_array({new});",
                _sync_line(table, new, 0),
            ]
            watched = key + changes
        for what, when, body in (
            ("insert", "AFTER INSERT", insert),
            ("update", f"AFTER UPDATE OF {', '.join(watched)}", update),
            ("delete", "AFTER DELETE", [_sync_line(table, old, 1)]),
        ):
            script.append(f"CREATE TRIGGER sync_{table}_{what} {when} ON {table} WHEN {_SYNC_OPEN}")
            script += ["BEGIN", *(f"    {statement}" for statement in body), "END;"]
    return _LINE.join(script)


MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        trigger TEXT NOT NULL,
        outcome TEXT NOT NULL,
        error_kind TEXT,
        message TEXT
    );
    CREATE TABLE raw_responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
        grp TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        body BLOB NOT NULL
    );
    CREATE TABLE points (
        key TEXT NOT NULL,
        taken_at TEXT NOT NULL,
        game_day TEXT NOT NULL,
        value REAL NOT NULL,
        sync_run_id INTEGER REFERENCES sync_runs(id),
        PRIMARY KEY (key, taken_at)
    ) WITHOUT ROWID;
    CREATE INDEX points_by_day ON points (game_day);
    CREATE TABLE labels (
        key TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        kind TEXT NOT NULL,
        parent_key TEXT,
        icon_url TEXT NOT NULL DEFAULT '',
        sort INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE latest_docs (
        grp TEXT PRIMARY KEY,
        taken_at TEXT NOT NULL,
        doc TEXT NOT NULL
    );
    CREATE TABLE sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        started_before_tool INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE group_fetches (
        grp TEXT PRIMARY KEY,
        last_ok_at TEXT,
        needs_retry INTEGER NOT NULL DEFAULT 0
    );
    """,
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE achievement_lists (
        slot TEXT PRIMARY KEY CHECK (slot IN ('current', 'previous')),
        installed_at TEXT NOT NULL, source TEXT NOT NULL, sha256 TEXT NOT NULL,
        version TEXT NOT NULL, body BLOB NOT NULL
    );
    CREATE TABLE achievement_progress (
        stage_id INTEGER PRIMARY KEY,
        status TEXT NOT NULL CHECK (status IN ('not_started', 'in_progress', 'done')),
        done_at TEXT, pinned INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE achievement_parts (
        stage_id INTEGER NOT NULL, part INTEGER NOT NULL, done_at TEXT NOT NULL,
        PRIMARY KEY (stage_id, part)
    ) WITHOUT ROWID;
    CREATE TABLE achievement_counters (group_id INTEGER PRIMARY KEY, value INTEGER NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE achievement_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, label TEXT NOT NULL, before TEXT NOT NULL);
    CREATE TABLE achievement_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL,
        file TEXT NOT NULL, fingerprint TEXT NOT NULL, summary TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        preset TEXT UNIQUE,
        name TEXT NOT NULL, place TEXT NOT NULL DEFAULT '', rule TEXT NOT NULL,
        steps INTEGER, bell INTEGER NOT NULL DEFAULT 1,
        ready_override TEXT, notified_ready_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK ((preset IS NULL) = (rule <> ''))
    );
    CREATE TABLE timer_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timer_id INTEGER NOT NULL,
        at TEXT NOT NULL, recorded_at TEXT NOT NULL, source TEXT NOT NULL
    );
    CREATE INDEX timer_events_by_timer ON timer_events (timer_id, at);
    CREATE TABLE commission_log (
        stage_id INTEGER NOT NULL, day TEXT NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY (stage_id, day)
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE layouts (
        place TEXT PRIMARY KEY, widgets TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE widget_data (widget_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE quick_timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, seconds INTEGER NOT NULL, started_at TEXT NOT NULL,
        ready_at TEXT NOT NULL, notified INTEGER NOT NULL DEFAULT 0
    );
    """,
    # The wish history and what the Pulls page keeps (the Pulls design, section 5). wish_imports comes first: a wish
    # names the import that kept it.
    """
    CREATE TABLE wish_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, started_at TEXT NOT NULL, finished_at TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('running', 'ok', 'failed')),
        found INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0, message TEXT,
        pools TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE wishes (
        id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, gacha_type TEXT NOT NULL, pool TEXT NOT NULL,
        name TEXT NOT NULL, item_type TEXT NOT NULL, rank INTEGER NOT NULL, time TEXT NOT NULL, at TEXT NOT NULL,
        import_id INTEGER NOT NULL REFERENCES wish_imports(id)
    );
    CREATE INDEX wishes_by_pool ON wishes (uid, pool, id);
    CREATE TABLE pulls_accounts (
        uid INTEGER PRIMARY KEY, charted_weapon TEXT NOT NULL DEFAULT '', chronicled_path TEXT NOT NULL DEFAULT '',
        balance_primogems INTEGER, balance_fates INTEGER, balance_set_at TEXT, corrections TEXT NOT NULL DEFAULT '{}',
        points TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE wishlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
        about TEXT NOT NULL, copies INTEGER NOT NULL DEFAULT 1, position INTEGER NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE banners (
        ann_id INTEGER PRIMARY KEY, name TEXT NOT NULL, pool TEXT NOT NULL, featured TEXT NOT NULL,
        art_url TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, read_at TEXT NOT NULL
    );
    CREATE TABLE wiki_items (
        kind TEXT NOT NULL, name TEXT NOT NULL, rank INTEGER NOT NULL, icon_url TEXT NOT NULL,
        entry_page_id TEXT NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY (kind, name)
    ) WITHOUT ROWID;
    """,
    # The Pulls second pass (its design, section 6): the two switches of the next version's estimate, and the events
    # count the player typed for a version, per account. events_typed is a JSON object, {"7.2": 2330}.
    """
    ALTER TABLE pulls_accounts ADD COLUMN welkin INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE pulls_accounts ADD COLUMN gnostic_hymn INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE pulls_accounts ADD COLUMN events_typed TEXT NOT NULL DEFAULT '{}';
    """,
    # Gift codes (the gift codes design, section 1): what each code did on each account, and the livestream codes the
    # player was already told about.
    """
    CREATE TABLE gift_codes (
        uid TEXT NOT NULL, code TEXT NOT NULL, result TEXT NOT NULL, primogems INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (uid, code)
    );
    CREATE TABLE gift_code_notices (code TEXT PRIMARY KEY, at TEXT NOT NULL);
    """,
    # The screenshot gallery (the ledger design, section 7): the pictures the player marked, by file name.
    """
    CREATE TABLE gallery_favourites (name TEXT PRIMARY KEY, at TEXT NOT NULL);
    """,
    # The game's own history logs, kept forever once read (the ledger design, sections 0 and 4): each entry once by its
    # account, kind and the game's id; a record of each kind's import; and the HoYoLAB diary's own primogem entries,
    # `n` numbering identical entries of the same second from 0.
    """
    CREATE TABLE ledger_entries (
        uid INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('primogem','crystal','resin','artifact')),
        id INTEGER NOT NULL, time TEXT NOT NULL, at TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '', rarity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (uid, kind, id)
    ) WITHOUT ROWID;
    CREATE INDEX ledger_by_time ON ledger_entries (uid, kind, at);
    CREATE TABLE ledger_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, kind TEXT NOT NULL, at TEXT NOT NULL,
        found INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL CHECK (outcome IN ('ok','failed')), message TEXT
    );
    CREATE TABLE diary_actions (
        uid INTEGER NOT NULL, time TEXT NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL, amount INTEGER NOT NULL,
        n INTEGER NOT NULL, PRIMARY KEY (uid, time, action, amount, n)
    ) WITHOUT ROWID;
    CREATE INDEX diary_actions_by_time ON diary_actions (uid, at);
    """,
    # The Characters pages (their design, section 4.5; the plan's contract, "Tables"): the focus, the plans and the card
    # edits per character, the ticks of the week's tasks, the amounts the player typed as held, the Theater's pick of a
    # month, the notices sent, the wiki's entries as read, and the art cache's index. Every row is of one account.
    """
    CREATE TABLE char_focus (uid INTEGER NOT NULL, position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 4),
        character TEXT NOT NULL, PRIMARY KEY (uid, position)) WITHOUT ROWID;
    CREATE TABLE char_plans (uid INTEGER NOT NULL, character TEXT NOT NULL, plan TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (uid, character)) WITHOUT ROWID;
    CREATE TABLE char_card_edits (uid INTEGER NOT NULL, character TEXT NOT NULL, edits TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (uid, character)) WITHOUT ROWID;
    CREATE TABLE char_ticks (uid INTEGER NOT NULL, day TEXT NOT NULL, task TEXT NOT NULL, done_at TEXT NOT NULL,
        PRIMARY KEY (uid, day, task)) WITHOUT ROWID;
    CREATE TABLE char_held (uid INTEGER NOT NULL, material TEXT NOT NULL, amount INTEGER NOT NULL, set_at TEXT NOT NULL,
        PRIMARY KEY (uid, material)) WITHOUT ROWID;
    CREATE TABLE char_theater (uid INTEGER NOT NULL, month TEXT NOT NULL, elements TEXT NOT NULL, set_at TEXT NOT NULL,
        PRIMARY KEY (uid, month)) WITHOUT ROWID;
    CREATE TABLE char_notices (uid INTEGER NOT NULL, kind TEXT NOT NULL, day TEXT NOT NULL, sent_at TEXT NOT NULL,
        PRIMARY KEY (uid, kind, day)) WITHOUT ROWID;
    CREATE TABLE wiki_entries (entry_page_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('character', 'weapon', 'set', 'material')),
        name TEXT NOT NULL, data TEXT NOT NULL, version TEXT NOT NULL, read_at TEXT NOT NULL) WITHOUT ROWID;
    CREATE TABLE art (key TEXT PRIMARY KEY, url TEXT NOT NULL, file TEXT NOT NULL, bytes INTEGER NOT NULL,
        read_at TEXT NOT NULL) WITHOUT ROWID;
    """,
    # Sync (the online plan, Task C2): the outbox the triggers fill, the gate that keeps rows from another PC out of it, and
    # the keys that name the rows a PC numbers for itself. Every player-made table gets its three triggers.
    _sync_migration(),
    # The outbox's lines get a number that only goes up (the audit's CLD-2): their stamps are the PC's clock to the
    # millisecond, and an edit in the same millisecond as the line being sent was lost, and hid from the quiet seconds. Every
    # INSERT OR REPLACE of a line, which the triggers of schema 12 make as they were, now takes the next number. The lines that
    # wait are kept, numbered in the order of their stamps. The table is made again under its own name, after the old one is
    # gone, so that no trigger that names it is touched.
    """
    CREATE TABLE sync_outbox_waiting AS SELECT kind, row_key, edited_at, deleted FROM sync_outbox;
    DROP TABLE sync_outbox;
    CREATE TABLE sync_outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, row_key TEXT NOT NULL,
        edited_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, UNIQUE (kind, row_key));
    INSERT INTO sync_outbox (kind, row_key, edited_at, deleted)
        SELECT kind, row_key, edited_at, deleted FROM sync_outbox_waiting ORDER BY edited_at, kind, row_key;
    DROP TABLE sync_outbox_waiting;
    """,
    # A point that repeats the value of the key's row before it, on the same game day, says nothing the row before does not
    # (the audit's DB-1): add_points no longer stores one, and this deletes the ones that were stored. The first point of
    # each day and every change stay, so the answers of the history do not move.
    """
    DELETE FROM points WHERE (key, taken_at) IN (
        SELECT key, taken_at FROM (
            SELECT key, taken_at, value, game_day,
                LAG(value) OVER ordered AS before_value, LAG(game_day) OVER ordered AS before_day
            FROM points WINDOW ordered AS (PARTITION BY key ORDER BY taken_at)
        ) WHERE value = before_value AND game_day = before_day
    );
    """,
    # Each cycle of the three endgame modes, as last read (the Stats design, 3.5): one row a mode and a schedule, with its
    # start and end and the details of that cycle as JSON, which the Months and the Year read. It stays on this PC and is
    # no sync kind: it is made of HoYoLAB's responses, and none of those ever goes to the online project (CLAUDE.md).
    """
    CREATE TABLE endgame_cycles (mode TEXT NOT NULL, cycle TEXT NOT NULL, starts TEXT NOT NULL, ends TEXT NOT NULL,
        detail TEXT NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY (mode, cycle));
    """,
    # The wiki's table holds the Tutorial entries of the three endgame modes as well (the Stats design, 3.2): their icons,
    # the headers' fallback pictures and the Stygian difficulties' medals. SQLite cannot change a CHECK, so the table is made
    # again with the new kind and the entries kept are copied into it. It has no trigger: the wiki is read by each PC.
    """
    CREATE TABLE wiki_entries_new (entry_page_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('character', 'weapon', 'set', 'material', 'tutorial')),
        name TEXT NOT NULL, data TEXT NOT NULL, version TEXT NOT NULL, read_at TEXT NOT NULL) WITHOUT ROWID;
    INSERT INTO wiki_entries_new (entry_page_id, kind, name, data, version, read_at)
        SELECT entry_page_id, kind, name, data, version, read_at FROM wiki_entries;
    DROP TABLE wiki_entries;
    ALTER TABLE wiki_entries_new RENAME TO wiki_entries;
    """,
    # TUSKFiZZY's own checklist profiles and imported legacy history. Authentication is deliberately not stored here.
    """
    CREATE TABLE tuskfizzy_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, portrait TEXT NOT NULL DEFAULT '',
        genshin_id TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE tuskfizzy_genshin_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, uid TEXT NOT NULL,
        server TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    );
    CREATE TABLE tuskfizzy_daily (
        day TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES tuskfizzy_accounts(id) ON DELETE CASCADE,
        login INTEGER NOT NULL CHECK (login IN (0,1)),
        commission INTEGER NOT NULL CHECK (commission IN (0,1)),
        resin INTEGER NOT NULL CHECK (resin IN (0,1)),
        resin_last INTEGER,
        PRIMARY KEY (day, account_id)
    ) WITHOUT ROWID;
    CREATE INDEX tuskfizzy_daily_by_account ON tuskfizzy_daily(account_id, day);
    """,
)

POINTS_TRIMMED_AT = 14  # the schema whose upgrade deletes most of `points`: the upgrade to it gives the room back (Store._compact)

# What a folder holds, as the player is told when the other kind is pointed at it.
KINDS = {
    "demo": "{path} holds the demo's made-up data. Your real history needs a folder of its own.",
    "live": "{path} holds your real history. The demo needs a folder of its own.",
}


class StoreError(Exception):
    """The database could not be opened or upgraded. The message is fit to show to the player."""


def iso(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class RunRow:
    id: int
    started_at: datetime
    finished_at: datetime | None
    trigger: str
    outcome: str
    error_kind: str | None
    message: str | None


@dataclass(frozen=True)
class GroupState:
    last_ok_at: datetime | None
    needs_retry: bool


@dataclass(frozen=True)
class SessionRow:
    id: int
    started_at: datetime
    ended_at: datetime
    started_before_tool: bool


@dataclass(frozen=True)
class StoredList:
    installed_at: datetime
    source: str
    sha256: str
    version: str
    body: bytes


@dataclass(frozen=True)
class ProgressRow:
    stage_id: int
    status: str
    done_at: datetime | None
    pinned: bool
    note: str
    source: str
    updated_at: datetime


@dataclass(frozen=True)
class ImportRow:
    id: int
    at: datetime
    kind: str
    file: str
    fingerprint: str
    summary: dict[str, Any]


@dataclass(frozen=True)
class TimerRow:
    id: int
    preset: str | None                  # a pre-set's key; None for a timer of the player's own
    name: str                           # "" for a pre-set, whose name comes from the pre-sets
    place: str
    rule: str                           # the rule as JSON (timers.rules.rule_json); "" for a pre-set
    steps: int | None                   # the visits it ends after; None for a timer without an end
    bell: bool
    ready_override: datetime | None     # Set ready time, until the next Cleared
    notified_ready_at: datetime | None  # the ready time a reminder was sent for
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TimerEvent:
    id: int
    timer_id: int
    at: datetime           # when it was cleared, which Cleared earlier puts in the past
    recorded_at: datetime
    source: str            # "player", "demo", or the kind of an earlier import


@dataclass(frozen=True)
class LayoutRow:
    place: str        # "board"; the overlay's comes with plan 2A-4
    widgets: str      # the widgets as JSON (board/layouts.py)
    revision: int     # goes up at every save, so that a save over a newer layout is refused
    updated_at: datetime


@dataclass(frozen=True)
class QuickTimerRow:
    id: int
    seconds: int
    started_at: datetime
    ready_at: datetime
    notified: bool    # its reminder was sent


@dataclass(frozen=True)
class WishRow:
    id: int           # HoYoverse's id for the wish: it grows with time, and no two wishes share one
    uid: int
    gacha_type: str   # as the wish was answered: "301", "400", "302", "200", "500" or "100"
    pool: str         # pulls.POOL_OF[gacha_type]; a type outside the six is counted in the pool of the query
    name: str
    item_type: str    # as HoYoverse names it
    rank: int         # 3, 4 or 5
    time: str         # the server's own text for the moment, "2026-10-13 09:00:00"
    at: datetime      # that moment, converted with the server's offset
    import_id: int    # the import that kept it


@dataclass(frozen=True)
class LedgerRow:
    uid: int
    kind: str         # "primogem", "crystal", "resin" or "artifact"
    id: int           # the game's id for the entry, which grows with time
    time: str         # the server's own text for the moment, "2026-10-27 04:01:00"
    at: datetime      # that moment, converted with the server's offset
    amount: int       # signed: in, or out
    reason: str       # the game's English reason
    name: str = ""    # an artifact's name
    rarity: int = 0   # an artifact's stars


@dataclass(frozen=True)
class DiaryActionRow:
    uid: int
    time: str         # HoYoLAB's text for the moment, in the server's time
    at: datetime
    action: str       # HoYoLAB's name for what gave the primogems
    amount: int
    n: int            # numbers identical entries of the same second, from 0


@dataclass(frozen=True)
class WishImportRow:
    id: int
    uid: int | None   # the account the run found; None while it runs, and when it found none
    started_at: datetime
    finished_at: datetime | None
    outcome: str      # "running", "ok" or "failed"
    found: int        # the new wishes it kept
    pages: int
    message: str | None
    pools: tuple[str, ...]   # the pools it fetched to their end


@dataclass(frozen=True)
class PullsAccountRow:
    uid: int
    charted_weapon: str            # the Epitomized Path's weapon; "" while none is charted
    chronicled_path: str           # the chronicled wish's chosen item; "" while none is chosen
    balance_primogems: int | None  # what the player typed, and when; None before they typed any
    balance_fates: int | None
    balance_set_at: datetime | None
    corrections: dict[str, Any]    # {pool: {"pity": n, "guaranteed": bool, "from": the window's iso start or None}}
    points: dict[str, Any]         # {pool: {"points": 0 | 1, "at": datetime}}: the Fate Points the game showed
    welkin: bool = False           # the player has the Blessing of the Welkin Moon
    gnostic_hymn: bool = False     # the player has bought the Gnostic Hymn
    events_typed: dict[str, int] = field(default_factory=dict)  # {version: the events primogems the player typed}


@dataclass(frozen=True)
class WishlistRow:
    id: int
    uid: int
    name: str
    kind: str         # "character" or "weapon"
    about: date       # the day the player wants it by
    copies: int
    position: int     # the order of the list
    created_at: datetime


@dataclass(frozen=True)
class OutboxRow:
    """A line for sync: the last thing the player did to a row, a change or a delete. The row is named by its kind (its table)
    and its key, which is the same on every PC; `edited_at` is the time of this PC, as SQLite wrote it. `seq` is the line's
    number: every new line, and every change of a row's line, takes one higher than any before it."""

    kind: str
    row_key: str
    edited_at: str
    deleted: bool
    seq: int


@dataclass(frozen=True)
class BannerRow:
    ann_id: int       # the announcement's id
    name: str
    pool: str         # the pool of the cards it belongs to ("character", "weapon" or "chronicled"), or "other"
    featured: tuple[str, ...]
    art_url: str
    starts_at: datetime
    ends_at: datetime
    read_at: datetime


@dataclass(frozen=True)
class WikiItemRow:
    kind: str         # "character" or "weapon", and "set" for the artifact sets (the Characters plan: menu 5)
    name: str
    rank: int         # 0 when the wiki does not say
    icon_url: str
    entry_page_id: str
    read_at: datetime


@dataclass(frozen=True)
class CharPlanRow:
    """A character's plan as the player kept it: the JSON of the Characters plan's `Plan`."""

    uid: int
    character: str
    plan: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True)
class WikiEntryRow:
    """A wiki entry as read (its modules, as JSON), kept by the game version it was read at."""

    entry_page_id: str
    kind: str         # "character", "weapon", "set", "material" or "tutorial" (an endgame mode's, for the Stats page)
    name: str
    data: dict[str, Any]
    version: str
    read_at: datetime


@dataclass(frozen=True)
class ArtRow:
    """A picture of the art cache: its key, the address it was read from, its file in the cache's folder, its size."""

    key: str
    url: str
    file: str
    bytes: int
    read_at: datetime


@dataclass(frozen=True)
class EndgameCycleRow:
    """One cycle of an endgame mode as last read (the Stats design, 3.5), kept by its mode ("abyss", "theater" or
    "stygian") and its cycle, the schedule's id: its start and end as UTC ISO text, the mode's details of that cycle as
    JSON (the endgame detail's fields of the mode, without the mode's prefix), and when it was read."""

    mode: str
    cycle: str
    starts: str
    ends: str
    detail: dict[str, Any]
    read_at: datetime


def _open_problem(path: Path, backups_dir: Path, error: sqlite3.Error) -> str:
    """Say in plain words why the database could not be opened. Nothing is changed on this path."""
    reason = str(error).lower()
    if "locked" in reason or "busy" in reason:
        return (
            f"The history database at {path} is in use by another program (or by a second TUSKFiZZY). "
            "Close it and start again. The file has been left untouched."
        )
    if "readonly" in reason or "read-only" in reason:
        return f"The history database at {path} cannot be written: the file is read-only. It has been left untouched."
    return (
        f"The history database at {path} cannot be read ({error}). It has been left untouched. "
        f"Backups are in {backups_dir}."
    )


def _full_check(conn: sqlite3.Connection) -> list[str]:
    """SQLite's integrity check of the whole file: ["ok"] when it is sound. Reads every page, so it takes seconds on a
    database of years (about 24 ms per 18 MB)."""
    return [str(row[0]) for row in conn.execute("PRAGMA quick_check")]


def _closed_cleanly(conn: sqlite3.Connection) -> bool:
    """Whether the last TUSKFiZZY that had the database closed it itself. A database from before `meta` was made, or one
    whose flag is not there, was not."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'closed_cleanly'").fetchone()
    except sqlite3.Error:
        return False
    return row is not None and row[0] == "1"


def _daily_made_on(backups_dir: Path, today: date) -> bool:
    """Whether the folder holds a daily copy made on the player's local date `today`."""
    for existing in backups_dir.glob("guildboard-daily-*.db"):
        try:
            made = datetime.strptime(existing.stem.removeprefix("guildboard-daily-"), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if made.replace(tzinfo=timezone.utc).astimezone().date() == today:
            return True
    return False


_BEFORE_UPGRADE = re.compile(r"^guildboard-auto-v\d+-")  # what Store.open names the copy it makes before an upgrade
UNFINISHED_COPY_HOURS = 1  # a copy being written under its working name is left alone this long: a worker thread may be at it


def _forget_copy(copy: Path) -> None:
    """Remove a backup copy with the side files SQLite may have left beside it. Housekeeping: one that cannot be removed is
    logged and stays until the next time."""
    for file in (copy, Path(str(copy) + "-wal"), Path(str(copy) + "-shm")):
        try:
            file.unlink(missing_ok=True)
        except OSError:
            log.warning("The old backup %s could not be removed", file.name, exc_info=True)


def _sweep_unfinished_copies(backups_dir: Path) -> None:
    """Remove the copies a process that was killed left under their working names (`<copy>.db.tmp`). They are no backup."""
    try:
        for partial in backups_dir.glob("guildboard-*.db.tmp"):
            if time.time() - partial.stat().st_mtime > UNFINISHED_COPY_HOURS * 3600:
                _forget_copy(partial)
    except OSError:
        log.warning("The unfinished backups could not be listed, so none was removed", exc_info=True)


def _prefix_bounds(prefix: str) -> tuple[str, str]:
    """The half-open key range that holds every key starting with `prefix`."""
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def valid_text(value: str) -> str:
    """Text that can be written as UTF-8, which SQLite and the answers to the page need. Half of an emoji (a lone
    surrogate, which JSON can carry) becomes U+FFFD; both halves together make the whole emoji."""
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _points_to_json(points: Mapping[str, Any]) -> str:
    """The Fate Points of a row as JSON. The time of each is a datetime in the row, as every time in a row is, and
    iso() text in the table (a time with no zone is refused, as everywhere in the store)."""
    return json.dumps({
        pool: {**entry, "at": iso(entry["at"])}
        if isinstance(entry, Mapping) and isinstance(entry.get("at"), datetime) else entry
        for pool, entry in points.items()
    })


def _points_from_json(text: str) -> dict[str, Any]:
    """The Fate Points of a row from the table's JSON: the time of each comes back a datetime."""
    points = json.loads(text)
    for entry in points.values():
        if isinstance(entry, dict) and isinstance(entry.get("at"), str):
            entry["at"] = parse_iso(entry["at"])
    return points


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path | None, backups_dir: Path | None) -> None:
        self._conn = conn
        self._path = path
        self._backups_dir = backups_dir
        self._in_transaction = False
        self._applying = 0  # how many applying_sync() blocks are open: the gate opens when the last one ends

    @contextmanager
    def _commit(self) -> Iterator[None]:
        """One write, committed at once. Inside transaction() the outer block commits instead."""
        if self._in_transaction:
            yield
            return
        with self._conn:
            yield

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Several writes as one: all of them are kept, or none. May be nested; the outermost one commits."""
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        try:
            with self._conn:
                yield
        finally:
            self._in_transaction = False

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, path: Path, backups_dir: Path, now: datetime) -> "Store":
        existed = path.is_file() and path.stat().st_size > 0
        path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            # One connection, used from the event-loop thread only; tests reach it from a helper thread.
            conn = sqlite3.connect(path, timeout=BUSY_SECONDS, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")  # safe with the log: a power cut can lose the last commit, never the file
            conn.execute("PRAGMA foreign_keys=ON")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            # The whole file is checked on the day's first start (the one that makes the daily copy) and after a close that
            # never came: a check that reads every page is seconds on a database of years, and the start waits for it.
            if existed and (not _closed_cleanly(conn) or not _daily_made_on(backups_dir, now.astimezone().date())):
                findings = _full_check(conn)
                if findings != ["ok"]:
                    raise sqlite3.DatabaseError(findings[0] if findings else "the integrity check gave no answer")
            # Prove now that the file can be written, so that a read-only or busy database is refused with
            # a clear reason. BEGIN alone succeeds on a read-only file; only a real write fails. This one
            # writes the version it just read and is undone at once.
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(f"PRAGMA user_version = {version}")
            finally:
                conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise StoreError(_open_problem(path, backups_dir, exc)) from exc
        if version > len(MIGRATIONS):
            conn.close()
            raise StoreError(
                f"The history database at {path} was written by a newer TUSKFiZZY (schema {version})."
            )
        store = cls(conn, path, backups_dir)
        try:
            if version < len(MIGRATIONS):
                if existed:
                    store.backup(f"auto-v{version}", now)
                    store._prune_auto_backups()
                store._migrate(version)
                if existed and version < POINTS_TRIMMED_AT:
                    store._compact()
            store.set_meta("closed_cleanly", "0")  # until close() says otherwise
        except BaseException:
            conn.close()  # a connection left open keeps the file open, and Windows will not remove or replace it
            raise
        return store

    @classmethod
    def in_memory(cls) -> "Store":
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn, None, None)
        store._migrate(0)
        return store

    # ------------------------------------------------------------------ what the folder belongs to
    def meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def drop_meta(self, key: str) -> None:
        with self._commit():
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    # ------------------------------------------------------------------ TUSKFiZZY legacy checklist data
    def tuskfizzy_import_counts(self) -> dict[str, int]:
        return {
            "accounts": int(self._conn.execute("SELECT COUNT(*) FROM tuskfizzy_accounts").fetchone()[0]),
            "genshinAccounts": int(self._conn.execute("SELECT COUNT(*) FROM tuskfizzy_genshin_accounts").fetchone()[0]),
            "dailyRecords": int(self._conn.execute("SELECT COUNT(*) FROM tuskfizzy_daily").fetchone()[0]),
        }

    def import_tuskfizzy_data(self, accounts: list[dict[str, Any]], daily: dict[str, dict[str, list[Any]]],
                              genshin_accounts: list[dict[str, Any]], selected_genshin_id: str) -> dict[str, int]:
        """Idempotently import validated, non-secret legacy profiles and checklist history in one transaction."""
        with self.transaction():
            for item in genshin_accounts:
                self._conn.execute(
                    "INSERT INTO tuskfizzy_genshin_accounts(id,name,uid,server,created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, uid=excluded.uid, server=excluded.server",
                    (item["id"], item["name"], item["uid"], item.get("server", ""), item.get("createdAt", "")),
                )
            for item in accounts:
                self._conn.execute(
                    "INSERT INTO tuskfizzy_accounts(id,name,portrait,genshin_id,created_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, portrait=excluded.portrait, genshin_id=excluded.genshin_id",
                    (item["id"], item["name"], item.get("portrait", ""), item.get("genshinId"), item.get("createdAt", "")),
                )
            for day, profiles in daily.items():
                for account_id, flags in profiles.items():
                    if account_id == "_resinLast":
                        continue
                    resin_last = profiles.get("_resinLast", {}).get(account_id) if isinstance(profiles.get("_resinLast"), dict) else None
                    self._conn.execute(
                        "INSERT INTO tuskfizzy_daily(day,account_id,login,commission,resin,resin_last) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(day,account_id) DO UPDATE SET login=excluded.login, commission=excluded.commission, "
                        "resin=excluded.resin, resin_last=excluded.resin_last",
                        (day, account_id, int(flags[0]), int(flags[1]), int(flags[2]),
                         int(resin_last) if isinstance(resin_last, bool) else None),
                    )
            if selected_genshin_id:
                self.set_meta("tuskfizzy_selected_genshin_id", selected_genshin_id)
            return self.tuskfizzy_import_counts()

    def claim(self, kind: str) -> None:
        """Mark the folder for the demo or for the real account, and refuse the other kind."""
        held = self.meta("kind")
        if held is None:
            self.set_meta("kind", kind)
        elif held != kind:
            folder = self._path.parent if self._path is not None else "This database"
            raise StoreError(KINDS[held].format(path=folder))

    def _compact(self) -> None:
        """Give the room deleted rows left back to the file, and so to every copy made of it. Only once: SQLite reuses the room
        by itself. It needs as much free disk as the file, and without it the database simply stays as large as it was."""
        try:
            self._conn.execute("VACUUM")
        except sqlite3.Error:
            log.warning("The database could not be made smaller", exc_info=True)

    def _migrate(self, from_version: int) -> None:
        for number, script in enumerate(MIGRATIONS[from_version:], start=from_version + 1):
            self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")

    def close(self) -> None:
        try:
            self.set_meta("closed_cleanly", "1")
        except sqlite3.Error:  # a connection that is shut already, or a database that cannot be written: closing goes on
            log.warning("The database could not be marked as closed", exc_info=True)
        self._conn.close()

    # ------------------------------------------------------------------ backups
    def backup(self, label: str, now: datetime) -> Path:
        if self._backups_dir is None:
            raise StoreError("This database has no backup folder.")
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        target = self._backups_dir / f"guildboard-{label}-{stamp}.db"
        # Written under another name and renamed when whole: a TUSKFiZZY that is killed while it copies leaves no file
        # that would count as the day's copy, and be offered for restore.
        partial = target.with_name(target.name + ".tmp")
        partial.unlink(missing_ok=True)
        destination = sqlite3.connect(partial)
        try:
            self._conn.backup(destination)
            destination.close()
            os.replace(partial, target)
        except BaseException:
            destination.close()
            _forget_copy(partial)  # a backup cut short is no backup
            raise
        return target

    def copy_to(self, target: Path) -> None:
        """A copy of the database at `target`, made with SQLite's backup API, which is safe while the database is in
        use. For the backup file; `backup` keeps the copies in the backups folder."""
        destination = sqlite3.connect(target)
        try:
            self._conn.backup(destination)
        finally:
            destination.close()

    def daily_backup(self, now: datetime) -> Path | None:
        """The day's copy, made once: None when one exists for this date. The newest DAILY_BACKUPS_KEPT are kept; the
        automatic, manual and before-restore copies are not touched."""
        if self._backups_dir is None:
            return None
        if _daily_made_on(self._backups_dir, now.astimezone().date()):  # the player's own date, as the export's file name has
            return None
        made = self.backup("daily", now)
        try:
            dailies = sorted(self._backups_dir.glob("guildboard-daily-*.db"), key=lambda p: p.name)
        except OSError:
            log.warning("The daily backups could not be listed, so none was removed", exc_info=True)
            return made
        for old in dailies[:-DAILY_BACKUPS_KEPT]:
            _forget_copy(old)
        _sweep_unfinished_copies(self._backups_dir)
        return made

    def _prune_auto_backups(self) -> None:
        """Only the newest automatic backups are kept, and the newest copy made before an upgrade (`auto-v<schema>`) besides
        them: five imports after an update must not take away the one copy an older TUSKFiZZY can still open. This is
        housekeeping: an old one that another program holds open stays until the next time, and nothing waits for it."""
        if self._backups_dir is None:
            return
        try:
            autos = sorted(self._backups_dir.glob("guildboard-auto-*.db"), key=lambda p: p.stat().st_mtime)
        except OSError:
            log.warning("The automatic backups could not be listed, so none was removed", exc_info=True)
            return
        before_upgrade = [p for p in autos if _BEFORE_UPGRADE.match(p.name)]
        pool = [p for p in autos if not before_upgrade or p != before_upgrade[-1]]
        for old in pool[:-AUTO_BACKUPS_KEPT]:
            _forget_copy(old)
        _sweep_unfinished_copies(self._backups_dir)

    def backup_before(self, label: str, now: datetime) -> Path | None:
        """An automatic backup before a change such as an import: the label starts "auto-" and old ones are pruned."""
        if self._backups_dir is None:  # the in-memory store
            return None
        target = self.backup(label, now)
        self._prune_auto_backups()
        return target

    # ------------------------------------------------------------------ sync of the player's own data
    def sync_outbox(self, limit: int) -> list[OutboxRow]:
        """The oldest lines, at most `limit`: what the player changed, and has not been sent."""
        rows = self._conn.execute(
            "SELECT kind, row_key, edited_at, deleted, seq FROM sync_outbox ORDER BY seq LIMIT ?", (limit,)
        ).fetchall()
        return [OutboxRow(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]

    def sync_sent(self, line: OutboxRow) -> None:
        """A line was sent and kept. It goes unless the row was changed again meanwhile: its line then has a higher number, and
        stays for the next push, also when the change came in the same millisecond."""
        with self._commit():
            self._conn.execute("DELETE FROM sync_outbox WHERE seq = ?", (line.seq,))

    def sync_outbox_state(self) -> tuple[int, int | None]:
        """How many lines wait, and the number of the newest: it changes whenever the player changes a row, and says nothing
        else."""
        r = self._conn.execute("SELECT COUNT(*), MAX(seq) FROM sync_outbox").fetchone()
        return int(r[0]), r[1]

    def sync_queue(self, kind: str, row_key: str, deleted: bool = False) -> None:
        """A line made by hand, for a thing that is no row of a table: a setting the player changed (the online plan, C4)."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_outbox (kind, row_key, edited_at, deleted) "
                f"VALUES (?, ?, {_SYNC_NOW}, ?)",
                (kind, row_key, int(deleted)),
            )

    def sync_has_data(self) -> bool:
        """Whether this PC holds data of the player's own, which an account that holds some too has to be asked about. The app's
        default pre-set timers, as they were made, are not the player's."""
        for table, _, _ in _SYNCED_AT_12:
            where = ""
            if table == "timers":
                where = " WHERE NOT (preset IS NOT NULL AND created_at = updated_at AND bell = 1 AND ready_override IS NULL AND steps IS NULL)"
            if self._conn.execute(f"SELECT 1 FROM {table}{where} LIMIT 1").fetchone() is not None:
                return True
        return False

    def sync_stamp_all(self) -> int:
        """A line for every row of every synced table, with the time of now: the first sync that sends this PC's data, which
        wins over what the account holds because it is the newest edit. How many rows."""
        total = 0
        with self._commit():
            for table, key, _ in _SYNCED_AT_12:
                row = ", ".join(key)
                cursor = self._conn.execute(
                    "INSERT OR REPLACE INTO sync_outbox (kind, row_key, edited_at, deleted) "
                    f"SELECT '{table}', json_array({row}), {_SYNC_NOW}, 0 FROM {table}"
                )
                total += max(cursor.rowcount, 0)
        return total

    def sync_wipe(self) -> None:
        """Every row of the synced tables goes, with the lines for them and the Cleared of the timers, which belong to timers
        that are gone. For use inside applying_sync(), when the account's rows are about to take their place."""
        with self._commit():
            for table, _, _ in _SYNCED_AT_12:
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM timer_events")
            self._conn.execute("DELETE FROM sync_outbox")
            self._conn.execute("DELETE FROM achievement_changes")  # an undo would put back rows that are gone, over what took their place

    def sync_drop_lines(self) -> None:
        """No line waits any more: what this PC has to say to the account is decided again, by a first sync."""
        with self._commit():
            self._conn.execute("DELETE FROM sync_outbox")

    def sync_pending(self, kind: str, row_key: str) -> OutboxRow | None:
        """The line for a row, when the player changed it and it has not been sent."""
        r = self._conn.execute(
            "SELECT kind, row_key, edited_at, deleted, seq FROM sync_outbox WHERE kind = ? AND row_key = ?", (kind, row_key)
        ).fetchone()
        return OutboxRow(r[0], r[1], r[2], bool(r[3]), r[4]) if r is not None else None

    def _synced(self, table: str, key_columns: Sequence[str] | None = None) -> None:
        """Sync reaches the tables of schema 12's list and no others, by the key that list names: the names go into the
        statements below as they are."""
        key = _SYNCED_KEYS.get(table)
        if key is None or (key_columns is not None and tuple(key_columns) != key):
            raise ValueError(f"{table} is not a table that sync keeps rows of, by this key.")

    def sync_columns(self, table: str) -> tuple[str, ...]:
        """The columns of a synced table, in its order."""
        return tuple(self.sync_types(table))

    def sync_types(self, table: str) -> dict[str, str]:
        """The columns of a synced table, in its order, each with the type it was made with: INTEGER or TEXT."""
        self._synced(table)
        return {str(row[1]): str(row[2]).upper() for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def sync_row(self, table: str, key_columns: Sequence[str], key: Sequence[Any]) -> dict[str, Any] | None:
        """A row of a synced table by its key, with every column it has; None when there is none."""
        columns = self.sync_columns(table)
        self._synced(table, key_columns)
        where = " AND ".join(f"{column} = ?" for column in key_columns)
        row = self._conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", tuple(key)).fetchone()
        return dict(zip(columns, row)) if row is not None else None

    def sync_put(self, table: str, key_columns: Sequence[str], values: Mapping[str, Any]) -> None:
        """Keep a row another PC sent: the row with its key takes the columns given, and a new one is made when there is none.
        A column the table does not have is left out, and one the row does not give keeps what it holds. The key must be in
        `values`. For use inside applying_sync()."""
        columns = self.sync_columns(table)
        self._synced(table, key_columns)
        chosen = [column for column in columns if column in values]
        if any(column not in chosen for column in key_columns):
            raise ValueError("A row needs its key.")
        others = [column for column in chosen if column not in key_columns]
        action = "DO UPDATE SET " + ", ".join(f"{column} = excluded.{column}" for column in others) if others else "DO NOTHING"
        with self._commit():
            self._conn.execute(
                f"INSERT INTO {table} ({', '.join(chosen)}) VALUES ({', '.join('?' for _ in chosen)}) "
                f"ON CONFLICT({', '.join(key_columns)}) {action}",
                [values[column] for column in chosen],
            )

    def sync_drop(self, table: str, key_columns: Sequence[str], key: Sequence[Any]) -> bool:
        """Remove a row another PC deleted, and a timer's Cleared with it; False when there was none. For use inside
        applying_sync()."""
        self._synced(table, key_columns)
        where = " AND ".join(f"{column} = ?" for column in key_columns)
        with self._commit():
            if table == "timers":
                self._conn.execute(f"DELETE FROM timer_events WHERE timer_id IN (SELECT id FROM timers WHERE {where})", tuple(key))
            return self._conn.execute(f"DELETE FROM {table} WHERE {where}", tuple(key)).rowcount > 0

    def sync_put_cleared(self, timer_id: int, cleared: Sequence[Mapping[str, str]]) -> None:
        """A timer's Cleared as another PC sent them, in the place of those it has. For use inside applying_sync()."""
        with self._commit():
            self._conn.execute("DELETE FROM timer_events WHERE timer_id = ?", (timer_id,))
            self._conn.executemany(
                "INSERT INTO timer_events (timer_id, at, recorded_at, source) VALUES (?, ?, ?, ?)",
                [(timer_id, event["at"], event["recorded_at"], event["source"]) for event in cleared],
            )

    @contextmanager
    def applying_sync(self) -> Iterator[None]:
        """What is written inside is a row another PC sent, and not the player's own change: no line is left for it. The gate is
        shut inside the transaction that keeps the rows, so that a crash in the middle leaves it as it was: open."""
        with self.transaction():
            self._applying += 1
            self._conn.execute("UPDATE sync_gate SET applying = 1")
            try:
                yield
            finally:
                self._applying -= 1
                if self._applying == 0:  # a block inside another leaves the gate shut for the one around it
                    self._conn.execute("UPDATE sync_gate SET applying = 0")

    # ------------------------------------------------------------------ sync runs
    def start_run(self, trigger: str, started_at: datetime) -> int:
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO sync_runs (started_at, trigger, outcome) VALUES (?, ?, 'running')",
                (iso(started_at), trigger),
            )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        finished_at: datetime,
        outcome: str,
        error_kind: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._commit():
            self._conn.execute(
                "UPDATE sync_runs SET finished_at = ?, outcome = ?, error_kind = ?, message = ? WHERE id = ?",
                (iso(finished_at), outcome, error_kind, message, run_id),
            )

    def add_skipped(self, trigger: str, at: datetime, message: str) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT INTO sync_runs (started_at, finished_at, trigger, outcome, message) "
                "VALUES (?, ?, ?, 'skipped', ?)",
                (iso(at), iso(at), trigger, message),
            )

    def fail_running_runs(self, at: datetime) -> None:
        with self._commit():
            self._conn.execute(
                "UPDATE sync_runs SET finished_at = ?, outcome = 'failed', error_kind = 'error', "
                "message = 'TUSKFiZZY closed before this sync finished.' WHERE outcome = 'running'",
                (iso(at),),
            )

    def prune_skipped_runs(self, now: datetime) -> None:
        """Remove the records of refused triggers (a manual sync asked for too soon, and the like) that are older than
        SKIPPED_RUNS_KEPT_DAYS. A refused trigger fetched nothing, so no raw body names it; a point may, and keeps its
        value without the name of a run that is gone."""
        cutoff = iso(now - timedelta(days=SKIPPED_RUNS_KEPT_DAYS))
        old = [row[0] for row in self._conn.execute(
            "SELECT id FROM sync_runs WHERE outcome = 'skipped' AND started_at < ?", (cutoff,)
        )]
        if not old:
            return
        with self.transaction():
            self._conn.executemany("UPDATE points SET sync_run_id = NULL WHERE sync_run_id = ?", [(i,) for i in old])
            self._conn.executemany(
                "DELETE FROM sync_runs WHERE id = ? AND id NOT IN (SELECT sync_run_id FROM raw_responses)", [(i,) for i in old]
            )

    def recent_runs(self, limit: int = 50) -> list[RunRow]:
        rows = self._conn.execute(
            "SELECT id, started_at, finished_at, trigger, outcome, error_kind, message "
            "FROM sync_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            RunRow(r[0], parse_iso(r[1]), parse_iso(r[2]) if r[2] else None, r[3], r[4], r[5], r[6]) for r in rows
        ]

    def last_finished_run(self) -> RunRow | None:
        """The newest run that ran to an end. Refused triggers and a run still under way do not count."""
        r = self._conn.execute(
            "SELECT id, started_at, finished_at, trigger, outcome, error_kind, message FROM sync_runs "
            "WHERE outcome NOT IN ('skipped', 'running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return RunRow(r[0], parse_iso(r[1]), parse_iso(r[2]) if r[2] else None, r[3], r[4], r[5], r[6]) if r else None

    def last_run_started(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT started_at FROM sync_runs WHERE outcome != 'skipped' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return parse_iso(row[0]) if row else None

    def last_ok_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT finished_at FROM sync_runs WHERE outcome IN ('ok', 'partial') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return parse_iso(row[0]) if row and row[0] else None

    def runs_since(self, since: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sync_runs WHERE outcome != 'skipped' AND started_at >= ?", (iso(since),)
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ raw archive
    def add_raw(self, run_id: int, grp: str, endpoint: str, fetched_at: datetime, body: Any) -> None:
        # ASCII-only JSON can hold any text HoYoLAB sends, including half of an emoji, which UTF-8 cannot.
        # A session cookie that a response echoes back is masked like in the log (H4).
        text = redact(json.dumps(body, separators=(",", ":"), default=str))
        with self._commit():
            self._conn.execute(
                "INSERT INTO raw_responses (sync_run_id, grp, endpoint, fetched_at, body) VALUES (?, ?, ?, ?, ?)",
                (run_id, grp, endpoint, iso(fetched_at), zlib.compress(text.encode("utf-8"))),
            )

    def trim_raw(self, grp: str, keep: int) -> None:
        """Keep only the `keep` newest raw bodies of a group."""
        with self._commit():
            self._conn.execute(
                "DELETE FROM raw_responses WHERE grp = ? AND id NOT IN "
                "(SELECT id FROM raw_responses WHERE grp = ? ORDER BY id DESC LIMIT ?)",
                (grp, grp, keep),
            )

    def trim_old_raw(self, now: datetime, day_of: Callable[[datetime], date]) -> None:
        """Keep every raw body for RAW_FULL_DAYS, and after that only the last one of each endpoint on each game day
        (`day_of` gives the game day of a time). The statistics are per game day, so what the archive is for survives, and
        nothing reads it back but a person looking into what HoYoLAB sent. Looked at once a game day: the old bodies do not
        change between two syncs."""
        today = day_of(now).isoformat()
        if self.meta("raw_trimmed_on") == today:
            return
        old = self._conn.execute(
            "SELECT id, grp, endpoint, fetched_at FROM raw_responses WHERE fetched_at < ? ORDER BY fetched_at, id",
            (iso(now - timedelta(days=RAW_FULL_DAYS)),),
        ).fetchall()
        last: dict[tuple[str, str, date], int] = {}
        for body_id, grp, endpoint, fetched_at in old:
            last[(grp, endpoint, day_of(parse_iso(fetched_at)))] = body_id
        kept = set(last.values())
        with self.transaction():
            self._conn.executemany("DELETE FROM raw_responses WHERE id = ?", [(row[0],) for row in old if row[0] not in kept])
            self.set_meta("raw_trimmed_on", today)

    def raw_bodies(self, grp: str, limit: int = 1) -> list[Any]:
        rows = self._conn.execute(
            "SELECT body FROM raw_responses WHERE grp = ? ORDER BY id DESC LIMIT ?", (grp, limit)
        ).fetchall()
        return [json.loads(zlib.decompress(row[0]).decode("utf-8")) for row in rows]

    def raw_bodies_of(self, grp: str, endpoint: str, limit: int | None = None) -> list[tuple[datetime, Any]]:
        """One endpoint's raw bodies with the times they were read, oldest first: every one the archive keeps, or only
        the newest `limit`."""
        rows = self._conn.execute(
            "SELECT fetched_at, body FROM raw_responses WHERE grp = ? AND endpoint = ? ORDER BY fetched_at DESC, id DESC "
            "LIMIT ?",
            (grp, endpoint, -1 if limit is None else limit),
        ).fetchall()
        return [(parse_iso(row[0]), json.loads(zlib.decompress(row[1]).decode("utf-8"))) for row in reversed(rows)]

    # ------------------------------------------------------------------ endgame cycles
    def put_endgame_cycle(self, row: EndgameCycleRow) -> None:
        """Keep a cycle under its mode and cycle, replacing the one kept."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO endgame_cycles (mode, cycle, starts, ends, detail, read_at) VALUES (?, ?, ?, ?, ?, ?)",
                (row.mode, row.cycle, row.starts, row.ends, json.dumps(row.detail), iso(row.read_at)),
            )

    def endgame_cycles(self, mode: str | None = None) -> list[EndgameCycleRow]:
        """The cycles kept, of one mode or of all, in the order they end."""
        where, params = ("WHERE mode = ? ", (mode,)) if mode is not None else ("", ())
        rows = self._conn.execute(
            f"SELECT mode, cycle, starts, ends, detail, read_at FROM endgame_cycles {where}ORDER BY ends, mode, cycle", params
        ).fetchall()
        return [EndgameCycleRow(r[0], r[1], r[2], r[3], json.loads(r[4]), parse_iso(r[5])) for r in rows]

    # ------------------------------------------------------------------ points
    def add_points(self, run_id: int | None, taken_at: datetime, game_day: date, points: Iterable[Point]) -> None:
        """Keep the points that are news: a key's value that is the one its newest row holds, on the same game day, is
        dropped. Every first point of a day and every change stays, so the days' last values, the value before a day and
        the first value in a period all answer as they did when every sync stored every key."""
        day = game_day.isoformat()
        with self._commit():
            for p in points:
                value = float(p.value)
                newest = self._conn.execute(
                    "SELECT value, game_day, taken_at FROM points WHERE key = ? ORDER BY taken_at DESC LIMIT 1", (p.key,)
                ).fetchone()
                # A clock moved back gives a row older than the newest: that is no repeat of what comes after it.
                if newest is not None and newest[0] == value and newest[1] == day and newest[2] < iso(taken_at):
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO points (key, taken_at, game_day, value, sync_run_id) VALUES (?, ?, ?, ?, ?)",
                    (p.key, iso(taken_at), day, value, run_id),
                )

    def latest_point_value(self, key: str) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM points WHERE key = ? ORDER BY taken_at DESC LIMIT 1", (key,)
        ).fetchone()
        return float(row[0]) if row else None

    def latest_point(self, key: str) -> tuple[float, datetime] | None:
        """The newest value of `key`, with the time it was taken."""
        row = self._conn.execute(
            "SELECT value, taken_at FROM points WHERE key = ? ORDER BY taken_at DESC LIMIT 1", (key,)
        ).fetchone()
        return (float(row[0]), parse_iso(row[1])) if row else None

    def latest_values(self, prefix: str) -> dict[str, float]:
        low, high = _prefix_bounds(prefix)
        rows = self._conn.execute(
            "SELECT p.key, p.value FROM points p JOIN ("
            "  SELECT key, MAX(taken_at) AS latest FROM points WHERE key >= ? AND key < ? GROUP BY key"
            ") m ON m.key = p.key AND m.latest = p.taken_at",
            (low, high),
        ).fetchall()
        return {row[0]: float(row[1]) for row in rows}

    def day_values(self, keys: Sequence[str], first: date, last: date) -> dict[str, dict[date, float]]:
        """For each key, the last value recorded on each game day in [first, last]."""
        out: dict[str, dict[date, float]] = {key: {} for key in keys}
        for key in keys:
            rows = self._conn.execute(
                "SELECT game_day, value FROM points WHERE key = ? AND game_day >= ? AND game_day <= ? "
                "ORDER BY taken_at",
                (key, first.isoformat(), last.isoformat()),
            )
            for day_text, value in rows:
                out[key][date.fromisoformat(day_text)] = float(value)
        return out

    def value_before(self, key: str, day: date) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM points WHERE key = ? AND game_day < ? ORDER BY taken_at DESC LIMIT 1",
            (key, day.isoformat()),
        ).fetchone()
        return float(row[0]) if row else None

    def value_at_or_before(self, key: str, day: date) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM points WHERE key = ? AND game_day <= ? ORDER BY taken_at DESC LIMIT 1",
            (key, day.isoformat()),
        ).fetchone()
        return float(row[0]) if row else None

    def first_value_in(self, key: str, first: date, last: date) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM points WHERE key = ? AND game_day >= ? AND game_day <= ? ORDER BY taken_at LIMIT 1",
            (key, first.isoformat(), last.isoformat()),
        ).fetchone()
        return float(row[0]) if row else None

    def first_game_day(self) -> date | None:
        row = self._conn.execute("SELECT MIN(game_day) FROM points").fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def max_value(self, key: str) -> float | None:
        row = self._conn.execute("SELECT MAX(value) FROM points WHERE key = ?", (key,)).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------ labels and documents
    def upsert_labels(self, labels: Iterable[Label], at: datetime) -> None:
        rows = [(l.key, l.label, l.kind, l.parent_key, l.icon_url, l.sort, iso(at)) for l in labels]
        with self._commit():
            self._conn.executemany(
                "INSERT INTO labels (key, label, kind, parent_key, icon_url, sort, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET label = excluded.label, kind = excluded.kind, "
                "parent_key = excluded.parent_key, icon_url = excluded.icon_url, sort = excluded.sort, "
                "updated_at = excluded.updated_at",
                rows,
            )

    def labels(self, kind: str | None = None) -> dict[str, Label]:
        sql = "SELECT key, label, kind, parent_key, icon_url, sort FROM labels"
        args: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        rows = self._conn.execute(sql + " ORDER BY sort, key", args).fetchall()
        return {r[0]: Label(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows}

    def label(self, key: str) -> Label | None:
        """One label, by key, without reading the others."""
        row = self._conn.execute(
            "SELECT key, label, kind, parent_key, icon_url, sort FROM labels WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else Label(row[0], row[1], row[2], row[3], row[4], row[5])

    def set_latest_doc(self, grp: str, taken_at: datetime, doc: Mapping[str, Any]) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT INTO latest_docs (grp, taken_at, doc) VALUES (?, ?, ?) "
                "ON CONFLICT(grp) DO UPDATE SET taken_at = excluded.taken_at, doc = excluded.doc",
                (grp, iso(taken_at), json.dumps(doc, default=str)),
            )

    def latest_doc(self, grp: str) -> tuple[datetime, dict[str, Any]] | None:
        row = self._conn.execute("SELECT taken_at, doc FROM latest_docs WHERE grp = ?", (grp,)).fetchone()
        return (parse_iso(row[0]), json.loads(row[1])) if row else None

    # ------------------------------------------------------------------ group bookkeeping
    def group_states(self) -> dict[str, GroupState]:
        rows = self._conn.execute("SELECT grp, last_ok_at, needs_retry FROM group_fetches").fetchall()
        return {r[0]: GroupState(parse_iso(r[1]) if r[1] else None, bool(r[2])) for r in rows}

    def mark_group_ok(self, grp: str, at: datetime) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT INTO group_fetches (grp, last_ok_at, needs_retry) VALUES (?, ?, 0) "
                "ON CONFLICT(grp) DO UPDATE SET last_ok_at = excluded.last_ok_at, needs_retry = 0",
                (grp, iso(at)),
            )

    def mark_group_failed(self, grp: str) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT INTO group_fetches (grp, last_ok_at, needs_retry) VALUES (?, NULL, 1) "
                "ON CONFLICT(grp) DO UPDATE SET needs_retry = 1",
                (grp,),
            )

    # ------------------------------------------------------------------ sessions
    def open_session(self, started_at: datetime, started_before_tool: bool) -> int:
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO sessions (started_at, ended_at, started_before_tool) VALUES (?, ?, ?)",
                (iso(started_at), iso(started_at), int(started_before_tool)),
            )
        return int(cursor.lastrowid)

    def touch_session(self, session_id: int, at: datetime) -> None:
        with self._commit():
            self._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (iso(at), session_id))

    def delete_session(self, session_id: int) -> None:
        with self._commit():
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def sessions_overlapping(self, start: datetime, end: datetime) -> list[SessionRow]:
        rows = self._conn.execute(
            "SELECT id, started_at, ended_at, started_before_tool FROM sessions "
            "WHERE ended_at > ? AND started_at < ? ORDER BY started_at",
            (iso(start), iso(end)),
        ).fetchall()
        return [SessionRow(r[0], parse_iso(r[1]), parse_iso(r[2]), bool(r[3])) for r in rows]

    def first_session(self) -> SessionRow | None:
        row = self._conn.execute(
            "SELECT id, started_at, ended_at, started_before_tool FROM sessions ORDER BY started_at, id LIMIT 1"
        ).fetchone()
        return SessionRow(row[0], parse_iso(row[1]), parse_iso(row[2]), bool(row[3])) if row else None

    def last_session(self) -> SessionRow | None:
        row = self._conn.execute(
            "SELECT id, started_at, ended_at, started_before_tool FROM sessions ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return SessionRow(row[0], parse_iso(row[1]), parse_iso(row[2]), bool(row[3])) if row else None

    # ------------------------------------------------------------------ achievements
    def achievement_list(self, slot: str = "current") -> StoredList | None:
        """The list in a slot, "current" or "previous", with its body as it was installed."""
        row = self._conn.execute(
            "SELECT installed_at, source, sha256, version, body FROM achievement_lists WHERE slot = ?", (slot,)
        ).fetchone()
        return StoredList(parse_iso(row[0]), row[1], row[2], row[3], zlib.decompress(row[4])) if row else None

    def install_achievement_list(self, body: bytes, *, source: str, sha256: str, version: str, at: datetime) -> None:
        """In one transaction, the current list becomes the previous one and this one becomes the current one."""
        row = (iso(at), source, sha256, valid_text(version), zlib.compress(body))
        with self._commit():
            self._conn.execute("UPDATE OR REPLACE achievement_lists SET slot = 'previous' WHERE slot = 'current'")
            self._conn.execute(
                "INSERT INTO achievement_lists (slot, installed_at, source, sha256, version, body) "
                "VALUES ('current', ?, ?, ?, ?, ?)",
                row,
            )

    def restore_previous_achievement_list(self) -> bool:
        """One step back: the previous list replaces the current one. False when there is no previous list."""
        with self._commit():
            cursor = self._conn.execute(
                "UPDATE OR REPLACE achievement_lists SET slot = 'current' WHERE slot = 'previous'"
            )
        return cursor.rowcount == 1

    def progress(self) -> dict[int, ProgressRow]:
        rows = self._conn.execute(
            "SELECT stage_id, status, done_at, pinned, note, source, updated_at FROM achievement_progress "
            "ORDER BY stage_id"
        ).fetchall()
        return {
            r[0]: ProgressRow(r[0], r[1], parse_iso(r[2]) if r[2] else None, bool(r[3]), r[4], r[5], parse_iso(r[6]))
            for r in rows
        }

    def put_progress(self, rows: Iterable[ProgressRow]) -> None:
        values = [
            (r.stage_id, r.status, iso(r.done_at) if r.done_at is not None else None, int(r.pinned), valid_text(r.note),
             r.source, iso(r.updated_at))
            for r in rows
        ]
        with self._commit():
            self._conn.executemany(
                "INSERT OR REPLACE INTO achievement_progress "
                "(stage_id, status, done_at, pinned, note, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def drop_progress(self, stage_ids: Iterable[int]) -> None:
        with self._commit():
            self._conn.executemany("DELETE FROM achievement_progress WHERE stage_id = ?", [(i,) for i in stage_ids])

    def parts(self) -> dict[int, dict[int, datetime]]:
        """For each stage, the parts done and when."""
        out: dict[int, dict[int, datetime]] = {}
        rows = self._conn.execute("SELECT stage_id, part, done_at FROM achievement_parts ORDER BY stage_id, part")
        for stage_id, part, done_at in rows:
            out.setdefault(stage_id, {})[part] = parse_iso(done_at)
        return out

    def put_part(self, stage_id: int, part: int, done_at: datetime | None) -> None:
        """Record when a part was done; None removes the part."""
        with self._commit():
            if done_at is None:
                self._conn.execute("DELETE FROM achievement_parts WHERE stage_id = ? AND part = ?", (stage_id, part))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO achievement_parts (stage_id, part, done_at) VALUES (?, ?, ?)",
                    (stage_id, part, iso(done_at)),
                )

    def counters(self) -> dict[int, int]:
        rows = self._conn.execute("SELECT group_id, value FROM achievement_counters ORDER BY group_id").fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def put_counter(self, group_id: int, value: int | None, at: datetime) -> None:
        """Record the count of a tiered achievement; None removes it."""
        with self._commit():
            if value is None:
                self._conn.execute("DELETE FROM achievement_counters WHERE group_id = ?", (group_id,))
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO achievement_counters (group_id, value, updated_at) VALUES (?, ?, ?)",
                    (group_id, value, iso(at)),
                )

    def push_change(self, label: str, before: Mapping[str, Any], at: datetime, keep: int = 20) -> None:
        """Record a change that can be undone, with what it replaced. Only the newest `keep` changes stay."""
        with self._commit():
            self._conn.execute(
                "INSERT INTO achievement_changes (at, label, before) VALUES (?, ?, ?)",
                (iso(at), valid_text(label), json.dumps(before)),
            )
            self._conn.execute(
                "DELETE FROM achievement_changes WHERE id NOT IN "
                "(SELECT id FROM achievement_changes ORDER BY id DESC LIMIT ?)",
                (keep,),
            )

    def pop_change(self) -> tuple[str, dict[str, Any]] | None:
        """The newest change and what it replaced, taken out of the history. None when there is none."""
        row = self._conn.execute(
            "SELECT id, label, before FROM achievement_changes ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        with self._commit():
            self._conn.execute("DELETE FROM achievement_changes WHERE id = ?", (row[0],))
        return row[1], json.loads(row[2])

    def newest_change_label(self) -> str | None:
        """The label of the newest change, the one an undo would put back, left in the history. None when there is
        none."""
        row = self._conn.execute("SELECT label FROM achievement_changes ORDER BY id DESC LIMIT 1").fetchone()
        return str(row[0]) if row else None

    def clear_changes(self) -> None:
        """Forget every recorded change, so that undo cannot reach back past a write it did not record."""
        with self._commit():
            self._conn.execute("DELETE FROM achievement_changes")

    def add_import(self, kind: str, file: str, fingerprint: str, summary: Mapping[str, Any], at: datetime) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT INTO achievement_imports (at, kind, file, fingerprint, summary) VALUES (?, ?, ?, ?, ?)",
                (iso(at), kind, valid_text(file), fingerprint, json.dumps(summary)),
            )

    def imports(self, limit: int = 20) -> list[ImportRow]:
        """The recorded imports, newest first."""
        rows = self._conn.execute(
            "SELECT id, at, kind, file, fingerprint, summary FROM achievement_imports ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ImportRow(r[0], parse_iso(r[1]), r[2], r[3], r[4], json.loads(r[5])) for r in rows]

    # ------------------------------------------------------------------ timers and the commission log
    _TIMER_COLUMNS = (
        "id, preset, name, place, rule, steps, bell, ready_override, notified_ready_at, created_at, updated_at"
    )

    @staticmethod
    def _timer(row: Sequence[Any]) -> TimerRow:
        return TimerRow(
            row[0], row[1], row[2], row[3], row[4], row[5], bool(row[6]),
            parse_iso(row[7]) if row[7] else None, parse_iso(row[8]) if row[8] else None,
            parse_iso(row[9]), parse_iso(row[10]),
        )

    @staticmethod
    def _timer_values(row: TimerRow) -> tuple[Any, ...]:
        return (
            row.preset, valid_text(row.name), valid_text(row.place), row.rule, row.steps, int(row.bell),
            iso(row.ready_override) if row.ready_override is not None else None,
            iso(row.notified_ready_at) if row.notified_ready_at is not None else None,
            iso(row.created_at), iso(row.updated_at),
        )

    def timers(self) -> list[TimerRow]:
        rows = self._conn.execute(f"SELECT {self._TIMER_COLUMNS} FROM timers ORDER BY id").fetchall()
        return [self._timer(row) for row in rows]

    def timer_keys(self) -> dict[int, str]:
        """Each timer's key across PCs, by its number on this one."""
        return {row[0]: row[1] for row in self._conn.execute("SELECT id, sync_key FROM timers WHERE sync_key IS NOT NULL")}

    def timer(self, timer_id: int) -> TimerRow | None:
        row = self._conn.execute(f"SELECT {self._TIMER_COLUMNS} FROM timers WHERE id = ?", (timer_id,)).fetchone()
        return self._timer(row) if row is not None else None

    def add_timer(self, row: TimerRow) -> int:
        """Keep a new timer; its id is the store's, whatever `row.id` holds. A pre-set's key is its name, here as the default
        pre-sets are made with the triggers off; a timer of the player's own has its key drawn by its trigger."""
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO timers (sync_key, preset, name, place, rule, steps, bell, ready_override, notified_ready_at, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (f"preset:{row.preset}" if row.preset is not None else None, *self._timer_values(row)),
            )
        return int(cursor.lastrowid)

    def put_timer(self, row: TimerRow) -> None:
        """Keep a timer under its own id, replacing it: an undo puts a removed timer back this way."""
        with self._commit():
            # The row that is replaced keeps its sync_key: it is the same timer on every PC.
            self._conn.execute(
                "INSERT OR REPLACE INTO timers (id, sync_key, preset, name, place, rule, steps, bell, ready_override, "
                "notified_ready_at, created_at, updated_at) "
                "VALUES (?, (SELECT sync_key FROM timers WHERE id = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row.id, row.id, *self._timer_values(row)),
            )

    def drop_timer(self, timer_id: int) -> None:
        """Remove a timer and every Cleared of it."""
        with self._commit():
            self._conn.execute("DELETE FROM timer_events WHERE timer_id = ?", (timer_id,))
            self._conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))

    def timer_events(self, timer_id: int | None = None) -> list[TimerEvent]:
        """Every Cleared, of one timer or of all, by the time it was cleared."""
        where, args = ("WHERE timer_id = ?", (timer_id,)) if timer_id is not None else ("", ())
        rows = self._conn.execute(
            f"SELECT id, timer_id, at, recorded_at, source FROM timer_events {where} ORDER BY at, id", args
        ).fetchall()
        return [TimerEvent(r[0], r[1], parse_iso(r[2]), parse_iso(r[3]), r[4]) for r in rows]

    def add_timer_event(self, timer_id: int, at: datetime, recorded_at: datetime, source: str) -> int:
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO timer_events (timer_id, at, recorded_at, source) VALUES (?, ?, ?, ?)",
                (timer_id, iso(at), iso(recorded_at), source),
            )
        return int(cursor.lastrowid)

    def replace_timer_events(self, timer_id: int, events: Sequence[TimerEvent]) -> None:
        """A timer's events as `events` holds them, their ids kept: an undo puts them back this way."""
        with self._commit():
            self._conn.execute("DELETE FROM timer_events WHERE timer_id = ?", (timer_id,))
            self._conn.executemany(
                "INSERT INTO timer_events (id, timer_id, at, recorded_at, source) VALUES (?, ?, ?, ?, ?)",
                [(e.id, timer_id, iso(e.at), iso(e.recorded_at), e.source) for e in events],
            )

    def set_notified(self, timer_id: int, ready_at: datetime | None) -> None:
        """Remember the ready time a reminder was sent for."""
        with self._commit():
            self._conn.execute(
                "UPDATE timers SET notified_ready_at = ? WHERE id = ?",
                (iso(ready_at) if ready_at is not None else None, timer_id),
            )

    # ------------------------------------------------------------------ layouts, widget data and quick timers
    # Not part of the undo history: their writes neither record a change nor end it.
    def layout(self, place: str) -> LayoutRow | None:
        row = self._conn.execute(
            "SELECT place, widgets, revision, updated_at FROM layouts WHERE place = ?", (place,)
        ).fetchone()
        return LayoutRow(row[0], row[1], row[2], parse_iso(row[3])) if row else None

    def put_layout(self, place: str, widgets: str, revision: int, at: datetime) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO layouts (place, widgets, revision, updated_at) VALUES (?, ?, ?, ?)",
                (place, widgets, revision, iso(at)),
            )

    def widget_data(self, widget_id: str) -> str | None:
        row = self._conn.execute("SELECT data FROM widget_data WHERE widget_id = ?", (widget_id,)).fetchone()
        return str(row[0]) if row else None

    def put_widget_data(self, widget_id: str, data: str, at: datetime) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO widget_data (widget_id, data, updated_at) VALUES (?, ?, ?)",
                (widget_id, data, iso(at)),
            )

    def drop_widget_data(self, widget_ids: Iterable[str]) -> None:
        with self._commit():
            self._conn.executemany("DELETE FROM widget_data WHERE widget_id = ?", [(i,) for i in widget_ids])

    def quick_timers(self) -> list[QuickTimerRow]:
        """Every quick timer, the soonest ready first."""
        rows = self._conn.execute(
            "SELECT id, seconds, started_at, ready_at, notified FROM quick_timers ORDER BY ready_at, id"
        ).fetchall()
        return [QuickTimerRow(r[0], r[1], parse_iso(r[2]), parse_iso(r[3]), bool(r[4])) for r in rows]

    def add_quick_timer(self, seconds: int, started_at: datetime, ready_at: datetime) -> int:
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO quick_timers (seconds, started_at, ready_at) VALUES (?, ?, ?)",
                (seconds, iso(started_at), iso(ready_at)),
            )
        return int(cursor.lastrowid)

    def drop_quick_timer(self, quick_id: int) -> bool:
        """Remove a quick timer; False when there was none."""
        with self._commit():
            cursor = self._conn.execute("DELETE FROM quick_timers WHERE id = ?", (quick_id,))
        return cursor.rowcount > 0

    def set_quick_notified(self, quick_ids: Iterable[int]) -> None:
        """Remember that these quick timers' reminders were sent."""
        with self._commit():
            self._conn.executemany("UPDATE quick_timers SET notified = 1 WHERE id = ?", [(i,) for i in quick_ids])

    def appearances(self) -> dict[int, dict[date, datetime]]:
        """For each commission achievement, the game days it appeared and when each was marked."""
        out: dict[int, dict[date, datetime]] = {}
        rows = self._conn.execute("SELECT stage_id, day, at FROM commission_log ORDER BY stage_id, day")
        for stage_id, day, at in rows:
            out.setdefault(stage_id, {})[date.fromisoformat(day)] = parse_iso(at)
        return out

    def put_appearance(self, stage_id: int, day: date, at: datetime | None, source: str = "player") -> None:
        """Mark the game day a commission achievement appeared; None removes the mark."""
        with self._commit():
            if at is None:
                self._conn.execute(
                    "DELETE FROM commission_log WHERE stage_id = ? AND day = ?", (stage_id, day.isoformat())
                )
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO commission_log (stage_id, day, at, source) VALUES (?, ?, ?, ?)",
                    (stage_id, day.isoformat(), iso(at), source),
                )

    # ------------------------------------------------------------------ Pulls: the wish history
    # Not part of the undo history, as the layouts are not: nothing here records a change or ends the history.
    _WISH_COLUMNS = "id, uid, gacha_type, pool, name, item_type, rank, time, at, import_id"

    @staticmethod
    def _wish(row: Sequence[Any]) -> WishRow:
        return WishRow(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], parse_iso(row[8]), row[9])

    def add_wishes(self, rows: Iterable[WishRow]) -> int:
        """Keep wishes, each once by its id: a wish already kept stays as it was. How many were new."""
        values = [
            (r.id, r.uid, r.gacha_type, r.pool, valid_text(r.name), valid_text(r.item_type), r.rank, r.time,
             iso(r.at), r.import_id)
            for r in rows
        ]
        with self._commit():
            before = self._conn.total_changes
            self._conn.executemany(
                f"INSERT OR IGNORE INTO wishes ({self._WISH_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values
            )
            return self._conn.total_changes - before

    def wishes(self, uid: int, pool: str | None = None) -> list[WishRow]:
        """An account's wishes, of one pool or of all, oldest first: HoYoverse's ids grow with time."""
        where, args = ("WHERE uid = ? AND pool = ?", (uid, pool)) if pool is not None else ("WHERE uid = ?", (uid,))
        rows = self._conn.execute(f"SELECT {self._WISH_COLUMNS} FROM wishes {where} ORDER BY id", args).fetchall()
        return [self._wish(row) for row in rows]

    def wishes_before(self, uid: int, before_id: int | None, limit: int) -> list[WishRow]:
        """Up to `limit` of an account's wishes, newest first, older than the wish `before_id` when it is given."""
        where, args = ("WHERE uid = ?", (uid,)) if before_id is None else ("WHERE uid = ? AND id < ?", (uid, before_id))
        rows = self._conn.execute(
            f"SELECT {self._WISH_COLUMNS} FROM wishes {where} ORDER BY id DESC LIMIT ?", (*args, limit)
        ).fetchall()
        return [self._wish(row) for row in rows]

    def wish_count(self, uid: int) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM wishes WHERE uid = ?", (uid,)).fetchone()[0])

    def has_wish(self, wish_id: int) -> bool:
        return self._conn.execute("SELECT 1 FROM wishes WHERE id = ?", (wish_id,)).fetchone() is not None

    def wish_uids(self) -> list[int]:
        """The accounts a wish is kept of, the one the newest import found first. An import found an account when it
        named it or kept a wish of it: an import that TUSKFiZZY's closing cut short kept wishes and named none."""
        rows = self._conn.execute(
            "SELECT uid FROM ("
            "  SELECT uid, MAX(id) AS newest FROM wish_imports WHERE uid IS NOT NULL GROUP BY uid"
            "  UNION ALL"
            "  SELECT uid, MAX(import_id) AS newest FROM wishes GROUP BY uid"
            ") WHERE uid IN (SELECT uid FROM wishes) GROUP BY uid ORDER BY MAX(newest) DESC, uid"
        ).fetchall()
        return [int(row[0]) for row in rows]

    def start_wish_import(self, started_at: datetime) -> int:
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO wish_imports (started_at, outcome) VALUES (?, 'running')", (iso(started_at),)
            )
        return int(cursor.lastrowid)

    def finish_wish_import(
        self,
        import_id: int,
        finished_at: datetime,
        outcome: str,
        uid: int | None,
        found: int,
        pages: int,
        message: str | None,
        pools: Sequence[str] = (),
    ) -> None:
        """End an import as "ok" or "failed", with the account it found, the new wishes it kept, the pages it read and
        the pools it fetched to their end."""
        with self._commit():
            self._conn.execute(
                "UPDATE wish_imports SET finished_at = ?, outcome = ?, uid = ?, found = ?, pages = ?, message = ?, "
                "pools = ? WHERE id = ?",
                (
                    iso(finished_at), outcome, uid, found, pages, valid_text(message) if message is not None else None,
                    ",".join(pools), import_id,
                ),
            )

    def keep_import_pool(
        self, import_id: int, rows: Iterable[WishRow], pools: Sequence[str], uid: int | None
    ) -> int:
        """Keep the new wishes of a pool an import fetched to its end, and note the pools it has finished so far, its
        account and the wishes it kept, as one write: an import that TUSKFiZZY's closing cuts short still credits the
        pools it kept. How many wishes were new."""
        with self.transaction():
            kept = self.add_wishes(rows)
            self._conn.execute(
                "UPDATE wish_imports SET pools = ?, found = found + ?, uid = COALESCE(uid, ?) WHERE id = ?",
                (",".join(pools), kept, uid, import_id),
            )
        return kept

    def fail_running_wish_imports(self, at: datetime) -> None:
        """At a start: an import still marked as running was cut short by TUSKFiZZY closing (IMPORT_CUT_SHORT)."""
        with self._commit():
            self._conn.execute(
                "UPDATE wish_imports SET finished_at = ?, outcome = 'failed', message = ? WHERE outcome = 'running'",
                (iso(at), IMPORT_CUT_SHORT),
            )

    def wish_imports(self, limit: int = 20) -> list[WishImportRow]:
        """The recorded imports, newest first."""
        rows = self._conn.execute(
            "SELECT id, uid, started_at, finished_at, outcome, found, pages, message, pools FROM wish_imports "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            WishImportRow(
                r[0], r[1], parse_iso(r[2]), parse_iso(r[3]) if r[3] else None, r[4], r[5], r[6], r[7],
                tuple(pool for pool in r[8].split(",") if pool),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ Pulls: what the page keeps
    def pulls_account(self, uid: int) -> PullsAccountRow:
        """What the player set for an account's Pulls page; the defaults when nothing is kept."""
        r = self._conn.execute(
            "SELECT uid, charted_weapon, chronicled_path, balance_primogems, balance_fates, balance_set_at, "
            "corrections, points, welkin, gnostic_hymn, events_typed FROM pulls_accounts WHERE uid = ?",
            (uid,),
        ).fetchone()
        if r is None:
            return PullsAccountRow(uid, "", "", None, None, None, {}, {})
        return PullsAccountRow(
            r[0], r[1], r[2], r[3], r[4], parse_iso(r[5]) if r[5] else None, json.loads(r[6]),
            _points_from_json(r[7]), bool(r[8]), bool(r[9]), json.loads(r[10]),
        )

    def put_pulls_account(self, row: PullsAccountRow) -> None:
        """Keep an account's settings, the row replacing the one before."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO pulls_accounts (uid, charted_weapon, chronicled_path, balance_primogems, "
                "balance_fates, balance_set_at, corrections, points, welkin, gnostic_hymn, events_typed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.uid, valid_text(row.charted_weapon), valid_text(row.chronicled_path), row.balance_primogems,
                    row.balance_fates, iso(row.balance_set_at) if row.balance_set_at is not None else None,
                    json.dumps(row.corrections), _points_to_json(row.points), int(row.welkin), int(row.gnostic_hymn),
                    json.dumps(row.events_typed),
                ),
            )

    @staticmethod
    def _wishlist_entry(row: Sequence[Any]) -> WishlistRow:
        return WishlistRow(
            row[0], row[1], row[2], row[3], date.fromisoformat(row[4]), row[5], row[6], parse_iso(row[7])
        )

    def wishlist(self, uid: int) -> list[WishlistRow]:
        """An account's wishlist in the order the player keeps it."""
        rows = self._conn.execute(
            "SELECT id, uid, name, kind, about, copies, position, created_at FROM wishlist WHERE uid = ? "
            "ORDER BY position, id",
            (uid,),
        ).fetchall()
        return [self._wishlist_entry(row) for row in rows]

    def add_wishlist(self, uid: int, name: str, kind: str, about: date, copies: int, at: datetime) -> int:
        """Add an entry at the end of the account's list; its id is the store's."""
        with self._commit():
            cursor = self._conn.execute(
                "INSERT INTO wishlist (uid, name, kind, about, copies, position, created_at) VALUES "
                "(?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM wishlist WHERE uid = ?), ?)",
                (uid, valid_text(name), kind, about.isoformat(), copies, uid, iso(at)),
            )
        return int(cursor.lastrowid)

    def put_wishlist(self, row: WishlistRow) -> None:
        """Keep an entry under its own id, replacing it: a change, and an undo, put an entry back this way."""
        with self._commit():
            # The row that is replaced keeps its sync_key: it is the same entry on every PC.
            self._conn.execute(
                "INSERT OR REPLACE INTO wishlist (id, sync_key, uid, name, kind, about, copies, position, created_at) "
                "VALUES (?, (SELECT sync_key FROM wishlist WHERE id = ?), ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.id, row.id, row.uid, valid_text(row.name), row.kind, row.about.isoformat(), row.copies, row.position,
                    iso(row.created_at),
                ),
            )

    def drop_wishlist(self, entry_id: int) -> bool:
        """Remove an entry; False when there was none."""
        with self._commit():
            cursor = self._conn.execute("DELETE FROM wishlist WHERE id = ?", (entry_id,))
        return cursor.rowcount > 0

    def banners(self) -> list[BannerRow]:
        """The banners last read, the earliest start first."""
        rows = self._conn.execute(
            "SELECT ann_id, name, pool, featured, art_url, starts_at, ends_at, read_at FROM banners "
            "ORDER BY starts_at, ann_id"
        ).fetchall()
        return [
            BannerRow(
                r[0], r[1], r[2], tuple(json.loads(r[3])), r[4], parse_iso(r[5]), parse_iso(r[6]), parse_iso(r[7])
            )
            for r in rows
        ]

    def replace_banners(self, rows: Iterable[BannerRow]) -> None:
        """The kept banners become `rows`, all at once. A banner given twice is kept once, the last."""
        values = [
            (
                r.ann_id, valid_text(r.name), r.pool, json.dumps([valid_text(name) for name in r.featured]),
                r.art_url, iso(r.starts_at), iso(r.ends_at), iso(r.read_at),
            )
            for r in rows
        ]
        with self._commit():
            self._conn.execute("DELETE FROM banners")
            self._conn.executemany(
                "INSERT OR REPLACE INTO banners (ann_id, name, pool, featured, art_url, starts_at, ends_at, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )

    def wiki_items(self) -> dict[tuple[str, str], WikiItemRow]:
        """The items the wiki lists, by (kind, name)."""
        rows = self._conn.execute(
            "SELECT kind, name, rank, icon_url, entry_page_id, read_at FROM wiki_items ORDER BY kind, name"
        ).fetchall()
        return {(r[0], r[1]): WikiItemRow(r[0], r[1], r[2], r[3], r[4], parse_iso(r[5])) for r in rows}

    def replace_wiki_items(self, kind: str, rows: Iterable[WikiItemRow]) -> None:
        """The kept items of `kind` become `rows`, all at once; the items of the other kind stay. An item given twice
        is kept once, the last."""
        kept = list(rows)
        if any(row.kind != kind for row in kept):
            raise ValueError(f"Only {kind} items can replace the {kind} items.")
        with self._commit():
            self._conn.execute("DELETE FROM wiki_items WHERE kind = ?", (kind,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO wiki_items (kind, name, rank, icon_url, entry_page_id, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (r.kind, valid_text(r.name), r.rank, r.icon_url, r.entry_page_id, iso(r.read_at))
                    for r in kept
                ],
            )

    # ------------------------------------------------------------------ gift codes
    def gift_code_rows(self, uid: str) -> list[tuple[str, str, int, str, str]]:
        """(code, result, primogems, source, at) of an account's codes, newest first."""
        return [
            (str(c), str(r), int(p), str(s), str(a))
            for c, r, p, s, a in self._conn.execute(
                "SELECT code, result, primogems, source, at FROM gift_codes WHERE uid = ? ORDER BY at DESC, code",
                (uid,),
            )
        ]

    def put_gift_code(self, uid: str, code: str, result: str, primogems: int, source: str, at: datetime) -> None:
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO gift_codes (uid, code, result, primogems, source, at) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, code, result, primogems, source, iso(at)),
            )

    def set_gift_code_primogems(self, uid: str, code: str, primogems: int) -> bool:
        with self._commit():
            return self._conn.execute(
                "UPDATE gift_codes SET primogems = ? WHERE uid = ? AND code = ?", (primogems, uid, code)
            ).rowcount > 0

    def delete_gift_code(self, uid: str, code: str) -> bool:
        with self._commit():
            return self._conn.execute("DELETE FROM gift_codes WHERE uid = ? AND code = ?", (uid, code)).rowcount > 0

    def gift_code_noticed(self) -> set[str]:
        return {str(code) for (code,) in self._conn.execute("SELECT code FROM gift_code_notices")}

    def add_gift_code_notices(self, codes: Iterable[str], at: datetime) -> None:
        with self._commit():
            self._conn.executemany(
                "INSERT OR IGNORE INTO gift_code_notices (code, at) VALUES (?, ?)", [(code, iso(at)) for code in codes]
            )

    # ------------------------------------------------------------------ the game's own history logs
    _LEDGER_COLUMNS = "uid, kind, id, time, at, amount, reason, name, rarity"

    @staticmethod
    def _ledger(row: Sequence[Any]) -> LedgerRow:
        return LedgerRow(row[0], row[1], row[2], row[3], parse_iso(row[4]), row[5], row[6], row[7], row[8])

    def keep_ledger_entries(self, rows: Iterable[LedgerRow]) -> int:
        """Keep entries, each once by its account, kind and id: one already kept stays as it was. How many were new."""
        values = [
            (r.uid, r.kind, r.id, r.time, iso(r.at), r.amount, valid_text(r.reason), valid_text(r.name), r.rarity)
            for r in rows
        ]
        with self._commit():
            before = self._conn.total_changes
            self._conn.executemany(
                f"INSERT OR IGNORE INTO ledger_entries ({self._LEDGER_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            return self._conn.total_changes - before

    def has_ledger_entry(self, uid: int, kind: str, entry_id: int) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM ledger_entries WHERE uid = ? AND kind = ? AND id = ?", (uid, kind, entry_id)
        ).fetchone() is not None

    def ledger_entries(
        self, uid: int, kind: str, *, since: datetime | None = None, before_id: int | None = None,
        limit: int | None = None,
    ) -> list[LedgerRow]:
        """An account's entries of a kind, newest first: those after `since`, older than the entry `before_id`."""
        where, args = ["uid = ?", "kind = ?"], [uid, kind]
        if since is not None:
            where.append("at > ?")
            args.append(iso(since))
        if before_id is not None:
            where.append("id < ?")
            args.append(before_id)
        tail = ""
        if limit is not None:
            tail = " LIMIT ?"
            args.append(limit)
        rows = self._conn.execute(
            f"SELECT {self._LEDGER_COLUMNS} FROM ledger_entries WHERE {' AND '.join(where)} ORDER BY id DESC{tail}",
            args,
        ).fetchall()
        return [self._ledger(row) for row in rows]

    def _end_entry(self, uid: int, kind: str, order: str) -> LedgerRow | None:
        row = self._conn.execute(
            f"SELECT {self._LEDGER_COLUMNS} FROM ledger_entries WHERE uid = ? AND kind = ? ORDER BY id {order} LIMIT 1",
            (uid, kind),
        ).fetchone()
        return self._ledger(row) if row is not None else None

    def oldest_ledger_entry(self, uid: int, kind: str) -> LedgerRow | None:
        return self._end_entry(uid, kind, "ASC")

    def newest_ledger_entry(self, uid: int, kind: str) -> LedgerRow | None:
        return self._end_entry(uid, kind, "DESC")

    def record_ledger_import(
        self, uid: int | None, kind: str, at: datetime, found: int, outcome: str, message: str | None
    ) -> None:
        """How one kind's import ended: "ok" with the new entries it kept, or "failed" with the player's sentence. A refresh
        that found nothing and follows one that found nothing too only moves that record's time: the histories are
        refreshed every hour, and a record of each would be 25 rows a day to say the same."""
        with self._commit():
            if outcome == "ok" and found == 0:
                newest = self._conn.execute(
                    "SELECT id, found, outcome FROM ledger_imports WHERE kind = ? AND uid IS ? ORDER BY id DESC LIMIT 1",
                    (kind, uid),
                ).fetchone()
                if newest is not None and newest[1] == 0 and newest[2] == "ok":
                    self._conn.execute("UPDATE ledger_imports SET at = ? WHERE id = ?", (iso(at), newest[0]))
                    return
            self._conn.execute(
                "INSERT INTO ledger_imports (uid, kind, at, found, outcome, message) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, kind, iso(at), found, outcome, valid_text(message) if message is not None else None),
            )

    def last_ledger_import(self, uid: int | None = None) -> datetime | None:
        """When the newest import of a kind that ended well ended, of the account or of any; None before one."""
        where, args = ("AND uid = ?", (uid,)) if uid is not None else ("", ())
        row = self._conn.execute(
            f"SELECT at FROM ledger_imports WHERE outcome = 'ok' {where} ORDER BY id DESC LIMIT 1", args
        ).fetchone()
        return parse_iso(row[0]) if row is not None else None

    def ledger_uids(self) -> list[int]:
        """The accounts whose history was imported, the newest import's first."""
        rows = self._conn.execute(
            "SELECT uid FROM ledger_imports WHERE uid IS NOT NULL GROUP BY uid ORDER BY MAX(id) DESC"
        ).fetchall()
        return [int(row[0]) for row in rows]

    def keep_diary_actions(self, uid: int, rows: Iterable[DiaryActionRow]) -> int:
        """Keep the diary's entries of an account, each once. How many were new."""
        values = [(uid, r.time, iso(r.at), valid_text(r.action), r.amount, r.n) for r in rows]
        with self._commit():
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO diary_actions (uid, time, at, action, amount, n) VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            return self._conn.total_changes - before

    def diary_actions(self, uid: int, since: datetime, until: datetime) -> list[DiaryActionRow]:
        """The diary's entries of an account from `since` up to before `until`, oldest first."""
        rows = self._conn.execute(
            "SELECT uid, time, at, action, amount, n FROM diary_actions WHERE uid = ? AND at >= ? AND at < ? "
            "ORDER BY at, n",
            (uid, iso(since), iso(until)),
        ).fetchall()
        return [DiaryActionRow(r[0], r[1], parse_iso(r[2]), r[3], r[4], r[5]) for r in rows]

    def gallery_favourites(self) -> set[str]:
        """The file names of the pictures marked as favourites."""
        return {str(name) for (name,) in self._conn.execute("SELECT name FROM gallery_favourites")}

    def set_gallery_favourite(self, name: str, on: bool, at: datetime) -> None:
        """Mark a picture as a favourite, or no longer; marking one twice keeps the first time."""
        with self._commit():
            if on:
                self._conn.execute("INSERT OR IGNORE INTO gallery_favourites (name, at) VALUES (?, ?)", (name, iso(at)))
            else:
                self._conn.execute("DELETE FROM gallery_favourites WHERE name = ?", (name,))

    # ------------------------------------------------------------------ the Characters pages
    def focus(self, uid: int) -> list[str]:
        """The characters of the account's focus, by position."""
        rows = self._conn.execute("SELECT character FROM char_focus WHERE uid = ? ORDER BY position", (uid,))
        return [str(character) for (character,) in rows]

    def put_focus(self, uid: int, characters: Sequence[str]) -> None:
        """The focus becomes `characters`, in that order. More than four is refused, and the focus stays as it was."""
        kept = [valid_text(character) for character in characters]
        if len(kept) > CHAR_FOCUS_MAX:
            raise ValueError(f"The focus holds {CHAR_FOCUS_MAX} characters at most.")
        with self._commit():
            self._conn.execute("DELETE FROM char_focus WHERE uid = ?", (uid,))
            self._conn.executemany(
                "INSERT INTO char_focus (uid, position, character) VALUES (?, ?, ?)",
                [(uid, position, character) for position, character in enumerate(kept, start=1)],
            )

    def char_plans(self, uid: int) -> dict[str, CharPlanRow]:
        """The plans the account kept, by character."""
        rows = self._conn.execute(
            "SELECT character, plan, updated_at FROM char_plans WHERE uid = ? ORDER BY character", (uid,)
        ).fetchall()
        return {r[0]: CharPlanRow(uid, r[0], json.loads(r[1]), parse_iso(r[2])) for r in rows}

    def put_char_plan(self, uid: int, character: str, plan: Mapping[str, Any], at: datetime) -> None:
        """Keep a character's plan, replacing the one kept."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO char_plans (uid, character, plan, updated_at) VALUES (?, ?, ?, ?)",
                (uid, character, json.dumps(plan), iso(at)),
            )

    def drop_char_plan(self, uid: int, character: str) -> bool:
        """Take a character's plan away, so that her default plan shows; False when none was kept."""
        with self._commit():
            cursor = self._conn.execute("DELETE FROM char_plans WHERE uid = ? AND character = ?", (uid, character))
        return cursor.rowcount > 0

    def card_edits(self, uid: int) -> dict[str, dict[str, Any]]:
        """The fields of each card the player edited, by character: the card's own names, the player's values."""
        rows = self._conn.execute(
            "SELECT character, edits FROM char_card_edits WHERE uid = ? ORDER BY character", (uid,)
        ).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def put_card_edits(self, uid: int, character: str, edits: Mapping[str, Any], at: datetime) -> None:
        """Keep a card's edits, replacing those kept. No edits at all take the row away: back to the card."""
        with self._commit():
            if not edits:
                self._conn.execute("DELETE FROM char_card_edits WHERE uid = ? AND character = ?", (uid, character))
                return
            self._conn.execute(
                "INSERT OR REPLACE INTO char_card_edits (uid, character, edits, updated_at) VALUES (?, ?, ?, ?)",
                (uid, character, json.dumps(edits), iso(at)),
            )

    def ticks(self, uid: int, since: date) -> set[tuple[date, str]]:
        """The tasks ticked on `since` and the days after it, as (day, task)."""
        rows = self._conn.execute("SELECT day, task FROM char_ticks WHERE uid = ? AND day >= ?", (uid, since.isoformat()))
        return {(date.fromisoformat(day), task) for day, task in rows}

    def put_tick(self, uid: int, day: date, task: str, done: bool, at: datetime) -> None:
        """Tick a task of a day, or take its tick back; ticking one twice keeps the first time. Not INSERT OR IGNORE: SQLite gives
        that rule to the statements of the triggers too, and the line of an earlier delete would stay and win."""
        with self._commit():
            if done:
                self._conn.execute(
                    "INSERT INTO char_ticks (uid, day, task, done_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (uid, day.isoformat(), task, iso(at)),
                )
            else:
                self._conn.execute(
                    "DELETE FROM char_ticks WHERE uid = ? AND day = ? AND task = ?", (uid, day.isoformat(), task)
                )

    def held(self, uid: int) -> dict[str, int]:
        """The amounts the player typed as held, by material key."""
        rows = self._conn.execute("SELECT material, amount FROM char_held WHERE uid = ?", (uid,))
        return {str(material): int(amount) for material, amount in rows}

    def put_held(self, uid: int, amounts: Mapping[str, int | None], at: datetime) -> None:
        """Keep the amounts given by material key; None takes that material's amount away, and the rest stay."""
        with self._commit():
            for material, amount in amounts.items():
                if amount is None:
                    self._conn.execute("DELETE FROM char_held WHERE uid = ? AND material = ?", (uid, material))
                else:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO char_held (uid, material, amount, set_at) VALUES (?, ?, ?, ?)",
                        (uid, material, amount, iso(at)),
                    )

    def theater_elements(self, uid: int, month: str) -> tuple[str, ...] | None:
        """The three elements the player picked for a month ("2026-10"); None before a pick."""
        row = self._conn.execute("SELECT elements FROM char_theater WHERE uid = ? AND month = ?", (uid, month)).fetchone()
        return None if row is None else tuple(json.loads(row[0]))

    def put_theater_elements(self, uid: int, month: str, elements: Sequence[str], at: datetime) -> None:
        """Keep the player's pick of a month's elements, replacing the one kept."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO char_theater (uid, month, elements, set_at) VALUES (?, ?, ?, ?)",
                (uid, month, json.dumps(list(elements)), iso(at)),
            )

    def drop_theater_elements(self, uid: int, month: str) -> None:
        """Take the player's pick of a month away, so that the calendar's elements show again."""
        with self._commit():
            self._conn.execute("DELETE FROM char_theater WHERE uid = ? AND month = ?", (uid, month))

    def notices_sent(self, uid: int, since: date) -> set[tuple[str, date]]:
        """The notices sent on `since` and the days after it, as (kind, day)."""
        rows = self._conn.execute("SELECT kind, day FROM char_notices WHERE uid = ? AND day >= ?", (uid, since.isoformat()))
        return {(kind, date.fromisoformat(day)) for kind, day in rows}

    def add_notice_sent(self, uid: int, kind: str, day: date, at: datetime) -> None:
        """Record that a kind of notice was sent for a day; a second record keeps the first time."""
        with self._commit():
            self._conn.execute(
                "INSERT OR IGNORE INTO char_notices (uid, kind, day, sent_at) VALUES (?, ?, ?, ?)",
                (uid, kind, day.isoformat(), iso(at)),
            )

    @staticmethod
    def _wiki_entry(row: Sequence[Any]) -> WikiEntryRow:
        return WikiEntryRow(row[0], row[1], row[2], json.loads(row[3]), row[4], parse_iso(row[5]))

    def wiki_entry(self, entry_page_id: str) -> WikiEntryRow | None:
        row = self._conn.execute(
            "SELECT entry_page_id, kind, name, data, version, read_at FROM wiki_entries WHERE entry_page_id = ?",
            (entry_page_id,),
        ).fetchone()
        return None if row is None else self._wiki_entry(row)

    def wiki_entry_count(self) -> int:
        """How many entries were read, of all kinds, counted without decoding any."""
        return int(self._conn.execute("SELECT COUNT(*) FROM wiki_entries").fetchone()[0])

    def wiki_entry_stamp(self, entry_page_id: str) -> tuple[str, str, datetime] | None:
        """An entry's kind, game version and read time, without its data: what tells whether a page parsed from it is still
        current."""
        row = self._conn.execute(
            "SELECT kind, version, read_at FROM wiki_entries WHERE entry_page_id = ?", (entry_page_id,)
        ).fetchone()
        return None if row is None else (row[0], row[1], parse_iso(row[2]))

    def wiki_entries(self, kind: str) -> dict[str, WikiEntryRow]:
        """The entries of a kind that were read, by entry page id."""
        rows = self._conn.execute(
            "SELECT entry_page_id, kind, name, data, version, read_at FROM wiki_entries WHERE kind = ? "
            "ORDER BY entry_page_id",
            (kind,),
        ).fetchall()
        return {row[0]: self._wiki_entry(row) for row in rows}

    def put_wiki_entry(self, row: WikiEntryRow) -> None:
        """Keep an entry under its page id, replacing the one kept."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO wiki_entries (entry_page_id, kind, name, data, version, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row.entry_page_id, row.kind, valid_text(row.name), json.dumps(row.data), row.version, iso(row.read_at)),
            )

    @staticmethod
    def _art(row: Sequence[Any]) -> ArtRow:
        return ArtRow(row[0], row[1], row[2], row[3], parse_iso(row[4]))

    def art(self, key: str) -> ArtRow | None:
        row = self._conn.execute("SELECT key, url, file, bytes, read_at FROM art WHERE key = ?", (key,)).fetchone()
        return None if row is None else self._art(row)

    def put_art(self, row: ArtRow) -> None:
        """Keep a picture's index row under its key, replacing the one kept."""
        with self._commit():
            self._conn.execute(
                "INSERT OR REPLACE INTO art (key, url, file, bytes, read_at) VALUES (?, ?, ?, ?, ?)",
                (row.key, row.url, row.file, row.bytes, iso(row.read_at)),
            )

    def drop_art(self, key: str) -> bool:
        """Take a picture's row away; False when there was none."""
        with self._commit():
            cursor = self._conn.execute("DELETE FROM art WHERE key = ?", (key,))
        return cursor.rowcount > 0

    def art_rows(self) -> list[ArtRow]:
        """Every picture of the cache, the one read longest ago first."""
        rows = self._conn.execute("SELECT key, url, file, bytes, read_at FROM art ORDER BY read_at, key").fetchall()
        return [self._art(row) for row in rows]
