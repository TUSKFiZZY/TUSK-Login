"""Schema 12: every change the player makes leaves a line for sync (the online plan, Task C2). SQLite triggers fill the outbox, so no
write site of the store changes its call; the triggers are off while a row that another PC sent is being kept."""
import sqlite3
import time
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from guildboard.store import MIGRATIONS, Store, TimerRow

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
STAMP = "2026-10-13T08:00:00+00:00"
UID = 700000001

# The player-made tables that sync, and no other (the online plan, Task C2). Written out, so that a table added to one place and
# not the other is a failing test: Task C3's KINDS has to name these same eighteen.
KINDS = [
    "achievement_progress", "achievement_parts", "achievement_counters", "commission_log", "timers", "quick_timers", "layouts",
    "widget_data", "pulls_accounts", "wishlist", "gift_codes", "sessions", "char_focus", "char_plans", "char_card_edits",
    "char_ticks", "char_held", "char_theater",
]
# Tables whose rows are numbered by this PC alone, and are named across PCs by a key of their own.
KEYED = ["timers", "quick_timers", "wishlist", "sessions"]

# A row of each kind: the statement that makes it (as it stands in schema 11, which has no sync_key), one that changes a column
# that syncs, one that deletes it, and its key across PCs (None where the key is the row's sync_key, which is drawn).
CASES: dict[str, tuple[str, str, str, str | None]] = {
    "achievement_progress": (
        f"INSERT INTO achievement_progress (stage_id, status, source, updated_at) VALUES (1402, 'in_progress', 'player', '{STAMP}')",
        "UPDATE achievement_progress SET status = 'done' WHERE stage_id = 1402",
        "DELETE FROM achievement_progress WHERE stage_id = 1402",
        "[1402]",
    ),
    "achievement_parts": (
        f"INSERT INTO achievement_parts (stage_id, part, done_at) VALUES (1402, 2, '{STAMP}')",
        f"UPDATE achievement_parts SET done_at = '2026-10-14T08:00:00+00:00' WHERE stage_id = 1402 AND part = 2",
        "DELETE FROM achievement_parts WHERE stage_id = 1402 AND part = 2",
        "[1402,2]",
    ),
    "achievement_counters": (
        f"INSERT INTO achievement_counters (group_id, value, updated_at) VALUES (7, 3, '{STAMP}')",
        "UPDATE achievement_counters SET value = 4 WHERE group_id = 7",
        "DELETE FROM achievement_counters WHERE group_id = 7",
        "[7]",
    ),
    "commission_log": (
        f"INSERT INTO commission_log (stage_id, day, at, source) VALUES (1402, '2026-10-13', '{STAMP}', 'player')",
        "UPDATE commission_log SET source = 'import' WHERE stage_id = 1402",
        "DELETE FROM commission_log WHERE stage_id = 1402",
        '[1402,"2026-10-13"]',
    ),
    "timers": (
        "INSERT INTO timers (preset, name, place, rule, steps, bell, created_at, updated_at) "
        f"VALUES (NULL, 'Teapot', '', '{{\"kind\":\"hours\",\"hours\":24}}', NULL, 1, '{STAMP}', '{STAMP}')",
        "UPDATE timers SET name = 'Teapot, again'",
        "DELETE FROM timers",
        None,
    ),
    "quick_timers": (
        f"INSERT INTO quick_timers (seconds, started_at, ready_at) VALUES (60, '{STAMP}', '2026-10-13T08:01:00+00:00')",
        "UPDATE quick_timers SET seconds = 120",
        "DELETE FROM quick_timers",
        None,
    ),
    "layouts": (
        f"INSERT INTO layouts (place, widgets, revision, updated_at) VALUES ('board', '[]', 1, '{STAMP}')",
        "UPDATE layouts SET revision = 2 WHERE place = 'board'",
        "DELETE FROM layouts WHERE place = 'board'",
        '["board"]',
    ),
    "widget_data": (
        f"INSERT INTO widget_data (widget_id, data, updated_at) VALUES ('resin', '{{}}', '{STAMP}')",
        "UPDATE widget_data SET data = '{\"a\":1}' WHERE widget_id = 'resin'",
        "DELETE FROM widget_data WHERE widget_id = 'resin'",
        '["resin"]',
    ),
    "pulls_accounts": (
        f"INSERT INTO pulls_accounts (uid) VALUES ({UID})",
        f"UPDATE pulls_accounts SET welkin = 1 WHERE uid = {UID}",
        f"DELETE FROM pulls_accounts WHERE uid = {UID}",
        f"[{UID}]",
    ),
    "wishlist": (
        "INSERT INTO wishlist (uid, name, kind, about, copies, position, created_at) "
        f"VALUES ({UID}, 'Skirk', 'character', '2026-10-13', 1, 1, '{STAMP}')",
        "UPDATE wishlist SET copies = 2",
        "DELETE FROM wishlist",
        None,
    ),
    "gift_codes": (
        f"INSERT INTO gift_codes (uid, code, result, primogems, source, at) VALUES ('{UID}', 'ABC', 'ok', 60, 'player', '{STAMP}')",
        f"UPDATE gift_codes SET primogems = 80 WHERE uid = '{UID}' AND code = 'ABC'",
        f"DELETE FROM gift_codes WHERE uid = '{UID}' AND code = 'ABC'",
        f'["{UID}","ABC"]',
    ),
    "sessions": (
        f"INSERT INTO sessions (started_at, ended_at, started_before_tool) VALUES ('{STAMP}', '{STAMP}', 0)",
        "UPDATE sessions SET ended_at = '2026-10-13T09:00:00+00:00'",
        "DELETE FROM sessions",
        None,
    ),
    "char_focus": (
        f"INSERT INTO char_focus (uid, position, character) VALUES ({UID}, 1, 'sandrone')",
        f"UPDATE char_focus SET character = 'odette' WHERE uid = {UID} AND position = 1",
        f"DELETE FROM char_focus WHERE uid = {UID} AND position = 1",
        f"[{UID},1]",
    ),
    "char_plans": (
        f"INSERT INTO char_plans (uid, character, plan, updated_at) VALUES ({UID}, 'sandrone', '{{}}', '{STAMP}')",
        f"UPDATE char_plans SET plan = '{{\"x\":1}}' WHERE uid = {UID} AND character = 'sandrone'",
        f"DELETE FROM char_plans WHERE uid = {UID} AND character = 'sandrone'",
        f'[{UID},"sandrone"]',
    ),
    "char_card_edits": (
        f"INSERT INTO char_card_edits (uid, character, edits, updated_at) VALUES ({UID}, 'sandrone', '{{}}', '{STAMP}')",
        f"UPDATE char_card_edits SET edits = '{{\"x\":1}}' WHERE uid = {UID} AND character = 'sandrone'",
        f"DELETE FROM char_card_edits WHERE uid = {UID} AND character = 'sandrone'",
        f'[{UID},"sandrone"]',
    ),
    "char_ticks": (
        f"INSERT INTO char_ticks (uid, day, task, done_at) VALUES ({UID}, '2026-10-13', 'domain', '{STAMP}')",
        f"UPDATE char_ticks SET done_at = '2026-10-14T08:00:00+00:00' WHERE uid = {UID} AND task = 'domain'",
        f"DELETE FROM char_ticks WHERE uid = {UID} AND task = 'domain'",
        f'[{UID},"2026-10-13","domain"]',
    ),
    "char_held": (
        f"INSERT INTO char_held (uid, material, amount, set_at) VALUES ({UID}, 'mora', 5, '{STAMP}')",
        f"UPDATE char_held SET amount = 6 WHERE uid = {UID} AND material = 'mora'",
        f"DELETE FROM char_held WHERE uid = {UID} AND material = 'mora'",
        f'[{UID},"mora"]',
    ),
    "char_theater": (
        f"INSERT INTO char_theater (uid, month, elements, set_at) VALUES ({UID}, '2026-10', '[]', '{STAMP}')",
        f"UPDATE char_theater SET elements = '[\"pyro\"]' WHERE uid = {UID} AND month = '2026-10'",
        f"DELETE FROM char_theater WHERE uid = {UID} AND month = '2026-10'",
        f'[{UID},"2026-10"]',
    ),
}


def _key(store: Store, kind: str) -> str:
    """The key of the one row of a kind, across PCs."""
    key = CASES[kind][3]
    if key is not None:
        return key
    return store._conn.execute(f"SELECT json_array(sync_key) FROM {kind}").fetchone()[0]


def _lines(store: Store) -> list[tuple[str, str, bool]]:
    return [(line.kind, line.row_key, line.deleted) for line in store.sync_outbox(1000)]


def _forget_all(store: Store) -> None:
    for line in store.sync_outbox(1000):
        store.sync_sent(line)


def _old_database(path, rows: bool) -> None:
    """A database as schema 11 left it, with a row of every kind when asked."""
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:11]))
    if rows:
        for insert, *_ in CASES.values():
            old.execute(insert)
    old.execute("PRAGMA user_version = 11")
    old.commit()
    old.close()


def test_the_eighteen_kinds_are_the_plans():
    assert len(KINDS) == 18 and len(set(KINDS)) == 18
    assert set(CASES) == set(KINDS)


def test_the_triggers_are_for_these_tables_alone(store):
    # A fetched table with a trigger would send HoYoLAB's answers up. Each kind has three, and no other table has any.
    triggers = store._conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'").fetchall()
    assert sorted({table for _, table in triggers}) == sorted(KINDS)
    assert sorted(name for name, _ in triggers) == sorted(
        f"sync_{kind}_{what}" for kind in KINDS for what in ("insert", "update", "delete")
    )


@pytest.mark.parametrize("kind", KINDS)
def test_an_insert_an_update_and_a_delete_each_leave_one_outbox_row(store, kind):
    insert, update, delete, _ = CASES[kind]
    assert store.sync_outbox(10) == []

    store._conn.execute(insert)
    key = _key(store, kind)
    assert _lines(store) == [(kind, key, False)]
    _forget_all(store)

    store._conn.execute(update)
    assert _lines(store) == [(kind, key, False)]
    _forget_all(store)

    store._conn.execute(delete)
    assert _lines(store) == [(kind, key, True)]


def test_the_line_says_when_in_the_time_of_this_pc(store):
    store._conn.execute(CASES["layouts"][0])
    [line] = store.sync_outbox(10)
    assert line.edited_at.endswith("Z") and len(line.edited_at) == len("2026-10-13T08:00:00.123Z")
    moment = datetime.fromisoformat(line.edited_at)
    assert abs((datetime.now(timezone.utc) - moment).total_seconds()) < 5  # SQLite's clock, which the test cannot move


def test_several_edits_of_a_row_leave_one_row_with_the_last_time(store):
    store._conn.execute(CASES["layouts"][0])
    [first] = store.sync_outbox(10)
    time.sleep(0.01)
    store._conn.execute("UPDATE layouts SET revision = 2 WHERE place = 'board'")
    time.sleep(0.01)
    store._conn.execute("UPDATE layouts SET revision = 3 WHERE place = 'board'")
    [last] = store.sync_outbox(10)
    assert (last.kind, last.row_key, last.deleted) == ("layouts", '["board"]', False)
    assert last.edited_at > first.edited_at


def test_an_edit_after_a_delete_and_a_delete_after_an_edit_leave_the_last_thing_done(store):
    store._conn.execute(CASES["layouts"][0])
    store._conn.execute(CASES["layouts"][2])
    store._conn.execute(CASES["layouts"][0])
    assert _lines(store) == [("layouts", '["board"]', False)]
    store._conn.execute(CASES["layouts"][2])
    assert _lines(store) == [("layouts", '["board"]', True)]


def test_a_replace_is_one_edit_of_the_row_that_was(store):
    store._conn.execute(CASES["char_focus"][0])
    _forget_all(store)
    store._conn.execute(f"INSERT OR REPLACE INTO char_focus (uid, position, character) VALUES ({UID}, 1, 'odette')")
    assert _lines(store) == [("char_focus", f"[{UID},1]", False)]


def test_a_list_that_is_written_again_leaves_a_delete_only_for_what_went(store):
    # The focus is written as a whole: the old rows are deleted and the new ones inserted. A place that is filled again is
    # a row that changed; a place that stays empty is a row that went.
    for position, name in ((1, "a"), (2, "b"), (3, "c")):
        store._conn.execute(f"INSERT INTO char_focus (uid, position, character) VALUES ({UID}, {position}, '{name}')")
    _forget_all(store)
    store._conn.execute(f"DELETE FROM char_focus WHERE uid = {UID}")
    store._conn.execute(f"INSERT INTO char_focus (uid, position, character) VALUES ({UID}, 1, 'z')")
    assert sorted(_lines(store)) == [
        ("char_focus", f"[{UID},1]", False), ("char_focus", f"[{UID},2]", True), ("char_focus", f"[{UID},3]", True),
    ]


def test_a_column_that_stays_on_this_pc_leaves_nothing(store):
    # The reminder that was sent is about this PC. The statement that sets it is the store's, as it is written.
    timer = store.add_timer(TimerRow(0, None, "Teapot", "", '{"kind":"hours","n":24}', None, True, None, None, NOW, NOW))
    quick = store.add_quick_timer(60, NOW, NOW)
    _forget_all(store)
    store.set_notified(timer, NOW)
    store.set_quick_notified([quick])
    assert store.sync_outbox(10) == []
    store.put_timer(replace(store.timer(timer), name="Teapot, again", notified_ready_at=None))  # a change that syncs does
    assert [(line.kind, line.deleted) for line in store.sync_outbox(10)] == [("timers", False)]


def test_the_outbox_is_given_oldest_first_and_at_most_a_limit(store):
    for place in ("a", "b", "c"):
        store._conn.execute(f"INSERT INTO layouts (place, widgets, revision, updated_at) VALUES ('{place}', '[]', 1, '{STAMP}')")
        time.sleep(0.01)
    assert [line.row_key for line in store.sync_outbox(2)] == ['["a"]', '["b"]']
    assert [line.row_key for line in store.sync_outbox(1000)] == ['["a"]', '["b"]', '["c"]']


def test_what_was_sent_is_forgotten_unless_the_row_was_edited_since(store):
    store._conn.execute(CASES["layouts"][0])
    [sent] = store.sync_outbox(10)
    time.sleep(0.01)
    store._conn.execute("UPDATE layouts SET revision = 2")  # an edit made while the push was on its way
    store.sync_sent(sent)
    [kept] = store.sync_outbox(10)
    assert kept.edited_at > sent.edited_at and kept.seq > sent.seq
    store.sync_sent(kept)
    assert store.sync_outbox(10) == []
    store.sync_sent(kept)  # a line that is gone is no trouble


def test_an_edit_with_the_sent_stamp_keeps_its_line(store):
    # The lines are stamped to the millisecond by the PC's clock, so an edit made in the same millisecond as the line that was
    # on its way has the same stamp. It is a change all the same, and its line stays for the next push.
    store._conn.execute(CASES["layouts"][0])
    [sent] = store.sync_outbox(10)
    store._conn.execute("UPDATE layouts SET revision = 2")  # an edit made while the push was on its way
    store._conn.execute("UPDATE sync_outbox SET edited_at = ?", (sent.edited_at,))  # in the same millisecond
    store.sync_sent(sent)
    [kept] = store.sync_outbox(10)
    assert kept.edited_at == sent.edited_at and kept.seq > sent.seq


@pytest.mark.parametrize("kind", KINDS)
def test_applying_writes_no_outbox_row(store, kind):
    insert, update, delete, _ = CASES[kind]
    with store.applying_sync():
        store._conn.execute(insert)
        store._conn.execute(update)
        store._conn.execute(delete)
        store._conn.execute(insert)
    assert store.sync_outbox(10) == []
    assert store._conn.execute(f"SELECT COUNT(*) FROM {kind}").fetchone()[0] == 1
    # And the gate is open again.
    store._conn.execute(update)
    assert len(store.sync_outbox(10)) == 1


def test_the_gate_opens_again_when_applying_fails(store):
    with pytest.raises(RuntimeError):
        with store.applying_sync():
            raise RuntimeError("a row that could not be kept")
    store._conn.execute(CASES["layouts"][0])
    assert len(store.sync_outbox(10)) == 1
    assert store._conn.execute("SELECT applying FROM sync_gate").fetchone()[0] == 0


def test_applying_inside_a_transaction_and_a_transaction_inside_applying(store):
    with store.transaction():
        with store.applying_sync():
            store._conn.execute(CASES["layouts"][0])
        store._conn.execute(CASES["widget_data"][0])
    assert _lines(store) == [("widget_data", '["resin"]', False)]
    with store.applying_sync():
        with store.transaction():
            store._conn.execute(CASES["char_held"][0])
    assert _lines(store) == [("widget_data", '["resin"]', False)]


def test_a_block_inside_applying_leaves_the_gate_shut_for_the_one_around_it(store):
    # The first sync that uses the account's data applies its rows inside a block of its own: the rows it wipes and the rows it
    # keeps are all of one block, and no line is left for any of them.
    with store.applying_sync():
        with store.applying_sync():
            store._conn.execute(CASES["layouts"][0])
        store._conn.execute(CASES["widget_data"][0])
        assert store._conn.execute("SELECT applying FROM sync_gate").fetchone()[0] == 1
    assert store.sync_outbox(10) == []
    assert store._conn.execute("SELECT applying FROM sync_gate").fetchone()[0] == 0
    store._conn.execute(CASES["char_held"][0])
    assert len(store.sync_outbox(10)) == 1


def test_a_crash_while_applying_leaves_the_gate_open(tmp_path):
    # The gate is shut inside the transaction that keeps the rows, so a PC that goes down in the middle comes back with the
    # gate as it was: otherwise every change the player made from then on would be left out for good.
    path = tmp_path / "guildboard.db"
    store = Store.open(path, tmp_path / "backups", NOW)
    applying = store.applying_sync()
    applying.__enter__()
    store._conn.execute(CASES["layouts"][0])
    other = sqlite3.connect(path)
    try:
        assert other.execute("SELECT applying FROM sync_gate").fetchone()[0] == 0
        assert other.execute("SELECT COUNT(*) FROM layouts").fetchone()[0] == 0  # nothing of it was kept
    finally:
        other.close()
    applying.__exit__(None, None, None)
    store.close()


# ---------------------------------------------------------------------------------------------------- the keys
def test_a_new_row_gets_a_sync_key(store):
    store.add_timer(TimerRow(0, None, "Teapot", "", '{"kind":"hours","n":24}', None, True, None, None, NOW, NOW))
    store.add_timer(TimerRow(0, None, "Fish", "", '{"kind":"hours","n":24}', None, True, None, None, NOW, NOW))
    store.add_quick_timer(60, NOW, NOW)
    store.add_wishlist(UID, "Skirk", "character", date(2026, 11, 1), 1, NOW)
    store.open_session(NOW, False)
    for table in KEYED:
        keys = [key for (key,) in store._conn.execute(f"SELECT sync_key FROM {table}")]
        assert all(len(key) == 32 and set(key) <= set("0123456789abcdef") for key in keys), table
        assert len(set(keys)) == len(keys), table
    assert store._conn.execute("SELECT COUNT(DISTINCT sync_key) FROM timers").fetchone()[0] == 2


def test_a_tick_taken_back_and_given_again_leaves_a_line_for_the_row_and_not_for_a_delete(store):
    # The statement's own conflict rule goes into its triggers: with OR IGNORE, the line for the delete would stay, and the tick
    # that is on this PC would be deleted from the account.
    store.put_tick(UID, date(2026, 10, 13), "domain", True, NOW)
    store.put_tick(UID, date(2026, 10, 13), "domain", False, NOW)
    assert [line.deleted for line in store.sync_outbox(10)] == [True]
    time.sleep(0.002)  # the lines are named by the millisecond
    store.put_tick(UID, date(2026, 10, 13), "domain", True, NOW)
    assert [line.deleted for line in store.sync_outbox(10)] == [False]


def test_a_tick_given_twice_keeps_the_first_time_and_is_no_change(store):
    store.put_tick(UID, date(2026, 10, 13), "domain", True, NOW)
    _forget_all(store)
    store.put_tick(UID, date(2026, 10, 13), "domain", True, datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert store.sync_outbox(10) == []
    assert store._conn.execute("SELECT done_at FROM char_ticks").fetchone()[0] == STAMP


def test_a_key_that_is_given_stays(store):
    store._conn.execute(
        "INSERT INTO quick_timers (seconds, started_at, ready_at, sync_key) "
        f"VALUES (60, '{STAMP}', '{STAMP}', 'chosen-by-the-other-pc')"
    )
    assert store._conn.execute("SELECT sync_key FROM quick_timers").fetchone()[0] == "chosen-by-the-other-pc"


def test_a_preset_timer_has_the_same_key_on_two_devices(store):
    # Both PCs make the same pre-set rows on their own, and a pre-set is unique: it is named by the pre-set.
    other = Store.in_memory()
    try:
        for device in (store, other):
            device.add_timer(TimerRow(0, "parametric", "", "", "", None, True, None, None, NOW, NOW))
            device.add_timer(TimerRow(0, None, "Teapot", "", '{"kind":"hours","n":24}', None, True, None, None, NOW, NOW))
        keys = [
            dict(device._conn.execute("SELECT preset, sync_key FROM timers").fetchall()) for device in (store, other)
        ]
        assert keys[0]["parametric"] == keys[1]["parametric"] == "preset:parametric"
        assert keys[0][None] != keys[1][None]  # a timer of the player's own is theirs, and drawn
    finally:
        other.close()


def test_a_row_put_back_keeps_its_key(store):
    # put_timer and put_wishlist replace a row under its id; the row is the same row, and so is its key across PCs.
    timer = store.add_timer(TimerRow(0, None, "Teapot", "", '{"kind":"hours","n":24}', None, True, None, None, NOW, NOW))
    entry = store.add_wishlist(UID, "Skirk", "character", date(2026, 11, 1), 1, NOW)
    keys = [store._conn.execute(f"SELECT sync_key FROM {table}").fetchone()[0] for table in ("timers", "wishlist")]
    _forget_all(store)
    store.put_timer(replace(store.timer(timer), name="Teapot, again"))
    store.put_wishlist(replace(store.wishlist(UID)[0], copies=2))
    assert [store._conn.execute(f"SELECT sync_key FROM {table}").fetchone()[0] for table in ("timers", "wishlist")] == keys
    assert sorted(_lines(store)) == sorted([("timers", f'["{keys[0]}"]', False), ("wishlist", f'["{keys[1]}"]', False)])
    assert entry == store.wishlist(UID)[0].id  # and under the same id


def test_a_pre_set_timer_put_back_after_a_removal_has_its_key_again(store):
    row = TimerRow(0, "parametric", "", "", "", None, True, None, None, NOW, NOW)
    timer = store.add_timer(row)
    store.drop_timer(timer)
    store.put_timer(replace(row, id=timer))  # an undo
    assert store._conn.execute("SELECT sync_key FROM timers").fetchone()[0] == "preset:parametric"
    assert _lines(store)[-1] == ("timers", '["preset:parametric"]', False)  # the removal is undone in the outbox too


# ---------------------------------------------------------------------------------------------------- the migration
def test_every_existing_row_gets_its_sync_key_in_the_migration(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:11]))
    old.executemany(
        "INSERT INTO timers (preset, name, place, rule, steps, bell, created_at, updated_at) VALUES (?, ?, '', ?, NULL, 1, ?, ?)",
        [("parametric", "", "", STAMP, STAMP), (None, "Teapot", '{"kind":"hours","n":24}', STAMP, STAMP),
         (None, "Fish", '{"kind":"hours","n":24}', STAMP, STAMP)],
    )
    old.executemany(
        "INSERT INTO quick_timers (seconds, started_at, ready_at) VALUES (?, ?, ?)", [(60, STAMP, STAMP), (120, STAMP, STAMP)]
    )
    old.executemany(
        "INSERT INTO wishlist (uid, name, kind, about, copies, position, created_at) VALUES (?, ?, 'character', '2026-11-01', 1, ?, ?)",
        [(UID, "Skirk", 1, STAMP), (UID, "Furina", 2, STAMP)],
    )
    old.executemany(
        "INSERT INTO sessions (started_at, ended_at, started_before_tool) VALUES (?, ?, 0)",
        [(STAMP, STAMP), ("2026-10-12T08:00:00+00:00", "2026-10-12T09:00:00+00:00"), ("2026-10-11T08:00:00+00:00", STAMP)],
    )
    old.execute("PRAGMA user_version = 11")
    old.commit()
    old.close()

    store = Store.open(path, tmp_path / "backups", NOW)
    try:
        for table, count in (("timers", 3), ("quick_timers", 2), ("wishlist", 2), ("sessions", 3)):
            keys = [key for (key,) in store._conn.execute(f"SELECT sync_key FROM {table}")]
            assert len(keys) == count and all(keys), table
            assert len(set(keys)) == count, table
        assert store._conn.execute("SELECT sync_key FROM timers WHERE preset = 'parametric'").fetchone()[0] == "preset:parametric"
        own = [key for (key,) in store._conn.execute("SELECT sync_key FROM timers WHERE preset IS NULL")]
        assert len(own) == 2 and all(len(key) == 32 and set(key) <= set("0123456789abcdef") for key in own)
        # The key is unique: the same key twice would be one row on every other PC.
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "UPDATE quick_timers SET sync_key = (SELECT sync_key FROM quick_timers WHERE seconds = 60) WHERE seconds = 120"
            )
    finally:
        store.close()
    assert len(list((tmp_path / "backups").glob("guildboard-auto-v11-*.db"))) == 1  # the usual backup before a migration


def test_the_migration_leaves_the_outbox_empty(tmp_path):
    # The first sync of a PC decides what goes up (Task C5); a line for every row there ever was would only be a second list.
    path = tmp_path / "guildboard.db"
    _old_database(path, rows=True)
    store = Store.open(path, tmp_path / "backups", NOW)
    try:
        assert store.sync_outbox(1000) == []
        assert store._conn.execute("SELECT applying FROM sync_gate").fetchone()[0] == 0
        for kind in KINDS:
            assert store._conn.execute(f"SELECT COUNT(*) FROM {kind}").fetchone()[0] == 1, kind
        store._conn.execute(CASES["layouts"][1])  # and the triggers are on
        assert len(store.sync_outbox(10)) == 1
    finally:
        store.close()


def test_schema_13_numbers_the_lines_that_wait_in_their_order(tmp_path):
    # The lines a PC had waiting when it updated keep waiting, numbered in the order of their stamps, and the triggers of
    # schema 12 write to the outbox of schema 13 as they did to the one before.
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:12]))
    old.executemany("INSERT INTO sync_outbox (kind, row_key, edited_at, deleted) VALUES (?, ?, ?, ?)", [
        ("layouts", '["b"]', "2026-10-13T08:00:00.002Z", 0), ("layouts", '["a"]', "2026-10-13T08:00:00.001Z", 1),
        ("settings", "fishing_rule", "2026-10-13T08:00:00.002Z", 0),
    ])
    old.execute("PRAGMA user_version = 12")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    try:
        lines = store.sync_outbox(10)
        assert [(line.kind, line.row_key, line.deleted) for line in lines] == [
            ("layouts", '["a"]', True), ("layouts", '["b"]', False), ("settings", "fishing_rule", False),
        ]
        assert [line.seq for line in lines] == sorted({line.seq for line in lines})
        store._conn.execute(CASES["layouts"][0])
        assert store.sync_outbox(10)[-1].row_key == '["board"]' and store.sync_outbox(10)[-1].seq > lines[-1].seq
        assert store.sync_outbox_state() == (4, store.sync_outbox(10)[-1].seq)
    finally:
        store.close()
    assert len(list((tmp_path / "backups").glob("guildboard-auto-v12-*.db"))) == 1  # the usual backup before a migration


def test_a_fresh_database_has_the_sync_tables(store):
    tables = {name for (name,) in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"sync_outbox", "sync_gate"} <= tables
    assert store._conn.execute("SELECT id, applying FROM sync_gate").fetchall() == [(1, 0)]
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 17
