import json
import os
import sqlite3
import stat
from pathlib import Path
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

import guildboard.store as store_module
from guildboard import logging_setup
from guildboard.metrics import Label, Point
from guildboard.store import (
    MIGRATIONS, BannerRow, LayoutRow, ProgressRow, PullsAccountRow, QuickTimerRow, Store, StoreError, TimerRow,
    WikiItemRow, WishImportRow, WishlistRow, WishRow, iso, parse_iso,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


def test_iso_round_trip_and_naive_rejection():
    assert iso(NOW) == "2026-10-13T08:00:00+00:00"
    assert parse_iso(iso(NOW)) == NOW
    with pytest.raises(ValueError):
        iso(datetime(2026, 10, 13, 8, 0))


def test_fresh_database_gets_the_schema(tmp_path):
    path = tmp_path / "guildboard.db"
    store = Store.open(path, tmp_path / "backups", NOW)
    store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    conn.close()
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").iterdir())


def test_existing_database_is_backed_up_before_upgrade(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE marker (x)")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    backups = list((tmp_path / "backups").glob("guildboard-auto-v0-*.db"))
    assert len(backups) == 1
    assert store.start_run("manual", NOW) == 1
    store.close()


def test_only_five_automatic_backups_are_kept(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for index in range(7):
        file = backups / f"guildboard-auto-import-2026010{index}-000000.db"
        file.write_bytes(b"x")
        os.utime(file, (1_700_000_000 + index, 1_700_000_000 + index))
    manual = backups / "guildboard-manual-20260101-000000.db"
    manual.write_bytes(b"x")
    store = Store.open(tmp_path / "guildboard.db", backups, NOW)
    store._prune_auto_backups()
    store.close()
    remaining = sorted(p.name for p in backups.glob("guildboard-auto-*.db"))
    assert remaining == [f"guildboard-auto-import-2026010{index}-000000.db" for index in range(2, 7)]
    assert manual.exists()


def test_garbage_database_is_refused_and_left_untouched(tmp_path):
    path = tmp_path / "guildboard.db"
    garbage = b"this is not a database" * 50
    path.write_bytes(garbage)
    with pytest.raises(StoreError) as error:
        Store.open(path, tmp_path / "backups", NOW)
    assert str(path) in str(error.value)
    assert "left untouched" in str(error.value)
    assert path.read_bytes() == garbage


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "guildboard.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker (x)")
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError) as error:
        Store.open(path, tmp_path / "backups", NOW)
    assert "newer TUSKFiZZY" in str(error.value)


def test_manual_backup_writes_a_copy(tmp_path):
    store = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)
    store.start_run("manual", NOW)
    target = store.backup("manual", NOW)
    store.close()
    assert target.name == "guildboard-manual-20261013-080000.db"
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 1
    copy.close()


def test_runs_are_recorded(store):
    assert store.last_run_started() is None
    assert store.last_ok_at() is None
    first = store.start_run("startup", NOW)
    store.finish_run(first, NOW + timedelta(seconds=5), "ok")
    store.add_skipped("manual", NOW + timedelta(minutes=1), "A sync ran less than 5 minutes ago.")
    second = store.start_run("manual", NOW + timedelta(minutes=10))
    store.finish_run(second, NOW + timedelta(minutes=10, seconds=3), "failed", "offline", "No connection.")
    assert store.last_run_started() == NOW + timedelta(minutes=10)
    assert store.last_ok_at() == NOW + timedelta(seconds=5)
    assert store.runs_since(NOW) == 2  # skipped triggers are not runs
    assert store.runs_since(NOW + timedelta(minutes=5)) == 1
    rows = store.recent_runs()
    assert [row.outcome for row in rows] == ["failed", "skipped", "ok"]
    assert rows[0].error_kind == "offline"
    assert rows[0].message == "No connection."
    assert rows[1].message == "A sync ran less than 5 minutes ago."
    assert store.recent_runs(limit=1)[0].id == rows[0].id


def test_interrupted_runs_are_closed(store):
    run = store.start_run("startup", NOW)
    store.fail_running_runs(NOW + timedelta(hours=1))
    row = store.recent_runs()[0]
    assert (row.id, row.outcome, row.error_kind) == (run, "failed", "error")
    assert row.finished_at == NOW + timedelta(hours=1)


def test_raw_responses_round_trip(store):
    run = store.start_run("manual", NOW)
    store.add_raw(run, "overview", "index", NOW, {"stats": {"achievement_number": 5}, "name": "须弥"})
    store.add_raw(run, "overview", "index", NOW + timedelta(seconds=1), {"second": True})
    assert store.raw_bodies("overview") == [{"second": True}]
    assert store.raw_bodies("overview", limit=5)[1]["name"] == "须弥"
    assert store.raw_bodies("notes") == []


def test_raw_bodies_of_one_endpoint_come_oldest_first_with_their_times(store):
    run = store.start_run("manual", NOW)
    store.add_raw(run, "endgame", "spiralAbyss", NOW, {"read": 1})
    store.add_raw(run, "endgame", "role_combat", NOW, {"theater": True})
    store.add_raw(run, "endgame", "spiralAbyss", NOW + timedelta(hours=3), {"read": 2})
    store.add_raw(run, "notes", "spiralAbyss", NOW, {"another": "group"})
    assert store.raw_bodies_of("endgame", "spiralAbyss") == [(NOW, {"read": 1}), (NOW + timedelta(hours=3), {"read": 2})]
    assert store.raw_bodies_of("endgame", "spiralAbyss", limit=1) == [(NOW + timedelta(hours=3), {"read": 2})]
    assert store.raw_bodies_of("endgame", "hard_challenge") == []


def test_endgame_cycles_are_kept_by_mode_and_cycle_and_come_in_the_order_they_end(store):
    from guildboard.store import EndgameCycleRow

    october = EndgameCycleRow("theater", "30", "2026-10-01T03:00:00+00:00", "2026-11-01T02:59:59+00:00", {"acts": 10}, NOW)
    abyss = EndgameCycleRow("abyss", "124", "2026-10-16T03:00:00+00:00", "2026-11-16T02:59:59+00:00", {"stars": 36}, NOW)
    november = EndgameCycleRow("theater", "31", "2026-11-01T03:00:00+00:00", "2026-12-01T02:59:59+00:00", {"acts": 8}, NOW)
    for row in (november, abyss, october):
        store.put_endgame_cycle(row)
    assert store.endgame_cycles() == [october, abyss, november]
    assert store.endgame_cycles("theater") == [october, november] and store.endgame_cycles("stygian") == []
    later = replace(november, detail={"acts": 10, "medals": [True, False]}, read_at=NOW + timedelta(days=1))
    store.put_endgame_cycle(later)  # the same mode and cycle: the row is replaced
    assert store.endgame_cycles("theater") == [october, later]
    tables = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "endgame_cycles" in tables


def test_points_latest_and_prefix(store):
    day = date(2026, 10, 13)
    store.add_points(None, NOW, day, [Point("stats.days_active", 1700), Point("stats.daysXactive", 1), Point("stats.chests.common", 5)])
    store.add_points(None, NOW + timedelta(hours=1), day, [Point("stats.days_active", 1701)])
    assert store.latest_point_value("stats.days_active") == 1701.0
    assert store.latest_point_value("nothing") is None
    assert store.latest_values("stats.days_") == {"stats.days_active": 1701.0}  # "_" is not a wildcard
    assert store.latest_values("stats.") == {
        "stats.days_active": 1701.0,
        "stats.daysXactive": 1.0,
        "stats.chests.common": 5.0,
    }
    assert store.latest_values("diary.") == {}


def test_day_values_take_the_last_point_of_each_day(store):
    store.add_points(None, NOW, date(2026, 10, 13), [Point("k", 1)])
    store.add_points(None, NOW + timedelta(hours=2), date(2026, 10, 13), [Point("k", 2)])
    store.add_points(None, NOW + timedelta(days=1), date(2026, 10, 14), [Point("k", 5)])
    values = store.day_values(["k", "other"], date(2026, 10, 13), date(2026, 10, 14))
    assert values == {"k": {date(2026, 10, 13): 2.0, date(2026, 10, 14): 5.0}, "other": {}}
    assert store.day_values(["k"], date(2026, 10, 14), date(2026, 10, 14)) == {"k": {date(2026, 10, 14): 5.0}}


def test_value_lookups_around_a_period(store):
    assert store.first_game_day() is None
    store.add_points(None, NOW, date(2026, 10, 13), [Point("k", 10)])
    store.add_points(None, NOW + timedelta(days=2), date(2026, 10, 15), [Point("k", 30)])
    store.add_points(None, NOW + timedelta(days=2, hours=1), date(2026, 10, 15), [Point("k", 25)])
    assert store.first_game_day() == date(2026, 10, 13)
    assert store.value_before("k", date(2026, 10, 13)) is None
    assert store.value_before("k", date(2026, 10, 14)) == 10.0
    assert store.value_at_or_before("k", date(2026, 10, 14)) == 10.0
    assert store.value_at_or_before("k", date(2026, 10, 15)) == 25.0
    assert store.first_value_in("k", date(2026, 10, 14), date(2026, 10, 16)) == 30.0
    assert store.first_value_in("k", date(2026, 10, 16), date(2026, 10, 17)) is None
    assert store.max_value("k") == 30.0
    assert store.max_value("nothing") is None


def test_labels_upsert(store):
    store.upsert_labels([Label("explore.8", "Sumeru", "region", None, "https://x.hoyoverse.com/a.png", 1)], NOW)
    store.upsert_labels(
        [Label("explore.8", "Sumeru!", "region", None, "", 0), Label("explore.8.area.rainforest", "Rainforest", "area", "explore.8")],
        NOW,
    )
    assert store.labels("region") == {"explore.8": Label("explore.8", "Sumeru!", "region", None, "", 0)}
    assert set(store.labels()) == {"explore.8", "explore.8.area.rainforest"}


def test_latest_doc_round_trip(store):
    assert store.latest_doc("overview") is None
    store.set_latest_doc("overview", NOW, {"nickname": "Traveler"})
    store.set_latest_doc("overview", NOW + timedelta(hours=1), {"nickname": "Lumine"})
    assert store.latest_doc("overview") == (NOW + timedelta(hours=1), {"nickname": "Lumine"})


def test_group_bookkeeping(store):
    assert store.group_states() == {}
    store.mark_group_failed("notes")
    assert store.group_states()["notes"].last_ok_at is None
    assert store.group_states()["notes"].needs_retry is True
    store.mark_group_ok("notes", NOW)
    assert store.group_states()["notes"].last_ok_at == NOW
    assert store.group_states()["notes"].needs_retry is False
    store.mark_group_failed("notes")
    assert store.group_states()["notes"].last_ok_at == NOW  # the last success is remembered
    assert store.group_states()["notes"].needs_retry is True


def test_sessions(store):
    assert store.last_session() is None
    first = store.open_session(NOW, False)
    store.touch_session(first, NOW + timedelta(minutes=30))
    second = store.open_session(NOW + timedelta(hours=3), True)
    store.touch_session(second, NOW + timedelta(hours=4))
    overlapping = store.sessions_overlapping(NOW + timedelta(minutes=10), NOW + timedelta(minutes=20))
    assert [row.id for row in overlapping] == [first]
    assert overlapping[0].ended_at == NOW + timedelta(minutes=30)
    assert store.sessions_overlapping(NOW + timedelta(minutes=30), NOW + timedelta(hours=3)) == []
    assert store.last_session().started_before_tool is True
    store.delete_session(second)
    assert store.last_session().id == first


def test_text_with_half_an_emoji_can_be_archived(store):
    run = store.start_run("manual", NOW)
    store.add_raw(run, "overview", "index", NOW, {"name": "Trav\ud83d"})  # a lone surrogate
    assert store.raw_bodies("overview") == [{"name": "Trav\ud83d"}]
    store.set_latest_doc("overview", NOW, {"nickname": "Trav\ud83d"})
    assert store.latest_doc("overview")[1] == {"nickname": "Trav\ud83d"}


def test_a_registered_secret_inside_a_raw_body_is_masked_before_it_is_stored(store):
    logging_setup.register_secret("v2_SECRET-IN-A-RESPONSE-0123")
    try:
        run = store.start_run("manual", NOW)
        store.add_raw(run, "overview", "index", NOW, {"echo": "cookie v2_SECRET-IN-A-RESPONSE-0123 here"})
        assert store.raw_bodies("overview") == [{"echo": "cookie [redacted] here"}]
    finally:
        logging_setup.forget_secrets()


def test_last_finished_run_ignores_refused_and_unfinished_ones(store):
    assert store.last_finished_run() is None
    first = store.start_run("manual", NOW)
    store.finish_run(first, NOW, "failed", "verification_needed", "HoYoLAB asked for a verification check.")
    for minute in range(1, 40):
        store.add_skipped("startup", NOW + timedelta(minutes=minute), "HoYoLAB is waiting for a verification check.")
    store.start_run("manual", NOW + timedelta(hours=1))  # still running
    row = store.last_finished_run()
    assert (row.id, row.error_kind) == (first, "verification_needed")


def test_damaged_pages_are_refused_and_left_untouched(tmp_path):
    path = tmp_path / "guildboard.db"
    store = Store.open(path, tmp_path / "backups", NOW)
    store.add_points(None, NOW, date(2026, 10, 13), [Point(f"key.{number}", number) for number in range(4000)])
    store.close()
    data = bytearray(path.read_bytes())
    assert len(data) > 20 * 4096
    data[4096:] = b"\xff" * (len(data) - 4096)  # every page after the first is damaged; the header stays valid
    path.write_bytes(bytes(data))
    damaged = path.read_bytes()
    with pytest.raises(StoreError) as error:
        Store.open(path, tmp_path / "backups", NOW)
    assert str(path) in str(error.value)
    assert "left untouched" in str(error.value)
    assert path.read_bytes() == damaged


def test_a_folder_in_place_of_the_database_is_refused(tmp_path):
    path = tmp_path / "guildboard.db"
    path.mkdir()
    with pytest.raises(StoreError) as error:
        Store.open(path, tmp_path / "backups", NOW)
    assert str(path) in str(error.value)


def test_a_read_only_database_is_refused(tmp_path):
    path = tmp_path / "guildboard.db"
    Store.open(path, tmp_path / "backups", NOW).close()
    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(StoreError) as error:
            Store.open(path, tmp_path / "backups", NOW)
        assert str(path) in str(error.value)
        assert "cannot be written" in str(error.value)
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_a_database_another_program_is_writing_to_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "guildboard.db"
    Store.open(path, tmp_path / "backups", NOW).close()
    monkeypatch.setattr(store_module, "BUSY_SECONDS", 0.2, raising=False)
    other = sqlite3.connect(path)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StoreError) as error:
            Store.open(path, tmp_path / "backups", NOW)
        assert str(path) in str(error.value)
        assert "in use by another program" in str(error.value)
    finally:
        other.rollback()
        other.close()


def _commits(store) -> list[str]:
    seen: list[str] = []
    store._conn.set_trace_callback(lambda sql: seen.append(sql) if sql.strip().upper().startswith("COMMIT") else None)
    return seen


def test_a_transaction_commits_once(store):
    commits = _commits(store)
    with store.transaction():
        run = store.start_run("manual", NOW)
        store.add_points(run, NOW, date(2026, 10, 13), [Point("k", 1)])
        with store.transaction():  # nesting is allowed; the outermost one commits
            store.finish_run(run, NOW, "ok")
    assert len(commits) == 1
    assert store.latest_point_value("k") == 1.0
    assert store.recent_runs()[0].outcome == "ok"


def test_a_failed_transaction_keeps_nothing(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.add_points(None, NOW, date(2026, 10, 13), [Point("k", 1)])
            store.open_session(NOW, False)
            raise RuntimeError("stopped half way")
    assert store.latest_point_value("k") is None
    assert store.last_session() is None
    store.add_points(None, NOW, date(2026, 10, 13), [Point("k", 2)])  # single writes work again afterwards
    assert store.latest_point_value("k") == 2.0


def test_a_version_1_database_gains_the_meta_table_after_a_backup(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript(MIGRATIONS[0])
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    assert store.meta("kind") is None
    store.close()
    assert len(list((tmp_path / "backups").glob("guildboard-auto-v1-*.db"))) == 1


def test_a_meta_value_can_be_replaced(store):
    store.set_meta("account_uid", "700000001")
    store.set_meta("account_uid", "800000002")
    assert store.meta("account_uid") == "800000002"


def test_a_folder_belongs_to_the_demo_or_to_the_real_account(tmp_path):
    store = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)
    store.claim("live")
    store.claim("live")  # the same kind again is fine
    with pytest.raises(StoreError) as error:
        store.claim("demo")
    assert "holds your real history" in str(error.value)
    store.close()


def test_a_version_2_database_gains_the_achievement_tables_after_a_backup(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript(MIGRATIONS[0] + MIGRATIONS[1])
    old.execute("PRAGMA user_version = 2")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    assert store.progress() == {} and store.achievement_list() is None
    store.close()
    assert len(list((tmp_path / "backups").glob("guildboard-auto-v2-*.db"))) == 1


def test_installing_a_list_keeps_the_one_before_and_can_go_back(store):
    store.install_achievement_list(b"one", source="bundled", sha256="a", version="7.0", at=NOW)
    store.install_achievement_list(b"two", source="github", sha256="b", version="7.1", at=NOW)
    assert store.achievement_list().body == b"two" and store.achievement_list().source == "github"
    assert store.achievement_list("previous").body == b"one"
    assert store.restore_previous_achievement_list() is True
    assert store.achievement_list().body == b"one" and store.achievement_list("previous") is None
    assert store.restore_previous_achievement_list() is False


def test_a_third_list_keeps_the_second_as_the_previous_one(store):
    store.install_achievement_list(b"one", source="bundled", sha256="a", version="7.0", at=NOW)
    store.install_achievement_list(b"two", source="github", sha256="b", version="7.1", at=NOW)
    store.install_achievement_list(b"three", source="github", sha256="c", version="7.2", at=NOW)
    assert (store.achievement_list().body, store.achievement_list().version) == (b"three", "7.2")
    assert (store.achievement_list("previous").body, store.achievement_list("previous").version) == (b"two", "7.1")


def test_progress_parts_and_counters_are_kept(store):
    row = ProgressRow(80127, "done", NOW, True, "net in the bag", "player", NOW)
    store.put_progress([row])
    assert store.progress() == {80127: row}
    store.drop_progress([80127])
    assert store.progress() == {}
    store.put_part(84508, 2, NOW)
    store.put_part(84508, 0, NOW)
    store.put_part(84508, 2, None)
    assert store.parts() == {84508: {0: NOW}}
    store.put_counter(82055, 17, NOW)
    store.put_counter(80127, 3, NOW)
    store.put_counter(80127, None, NOW)
    assert store.counters() == {82055: 17}


def test_changes_come_back_newest_first_and_only_twenty_are_kept(store):
    for n in range(25):
        store.push_change(f"change {n}", {"n": n}, NOW)
    assert store.pop_change() == ("change 24", {"n": 24})
    labels = [store.pop_change()[0] for _ in range(19)]
    assert labels[0] == "change 23" and labels[-1] == "change 5"
    assert store.pop_change() is None


def test_imports_are_recorded_newest_first(store):
    store.add_import("astra", "state.json", "f1", {"done": 3}, NOW)
    store.add_import("paimon", "paimon.json", "f2", {"done": 1}, NOW + timedelta(minutes=1))
    rows = store.imports()
    assert [(r.kind, r.file, r.summary) for r in rows] == [("paimon", "paimon.json", {"done": 1}), ("astra", "state.json", {"done": 3})]
    assert len(store.imports(1)) == 1


def test_the_latest_point_comes_with_the_time_it_was_taken(store):
    assert store.latest_point("stats.achievements") is None
    store.add_points(None, NOW, NOW.date(), [Point("stats.achievements", 1762)])
    assert store.latest_point("stats.achievements") == (1762.0, NOW)


def test_clearing_the_changes_leaves_nothing_to_undo(store):
    store.push_change("change 1", {"n": 1}, NOW)
    store.push_change("change 2", {"n": 2}, NOW)
    store.clear_changes()
    assert store.pop_change() is None


def test_the_achievement_writes_join_an_open_transaction(store):
    store.push_change("change 1", {"n": 1}, NOW)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.install_achievement_list(b"one", source="bundled", sha256="a", version="7.0", at=NOW)
            store.put_progress([ProgressRow(80127, "done", NOW, False, "", "astra", NOW)])
            store.put_part(84508, 0, NOW)
            store.put_counter(82055, 17, NOW)
            store.add_import("astra", "state.json", "f1", {"done": 1}, NOW)
            store.clear_changes()
            raise RuntimeError("stopped half way")
    assert store.achievement_list() is None and store.progress() == {} and store.parts() == {}
    assert store.counters() == {} and store.imports() == []
    assert store.pop_change() == ("change 1", {"n": 1})


def test_a_backup_before_a_change_counts_among_the_automatic_ones(tmp_path, store):
    on_disk = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)
    for minute in range(6):
        target = on_disk.backup_before("auto-import", NOW + timedelta(minutes=minute))
    on_disk.close()
    assert target.name == "guildboard-auto-import-20261013-080500.db"
    assert len(list((tmp_path / "backups").glob("guildboard-auto-*.db"))) == 5
    assert store.backup_before("auto-import", NOW) is None  # the in-memory store has no backup folder


def test_an_old_backup_that_another_program_holds_open_stays_and_the_new_one_is_made(tmp_path):
    # Windows refuses to delete a file that another program has open, a SQLite browser for example. Pruning is
    # housekeeping: the new backup counts, and the old one goes another time.
    on_disk = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)
    for minute in range(5):
        on_disk.backup_before("auto-import", NOW + timedelta(minutes=minute))
    oldest = tmp_path / "backups" / "guildboard-auto-import-20261013-080000.db"
    try:
        with open(oldest, "rb"):
            target = on_disk.backup_before("auto-import", NOW + timedelta(minutes=5))
    finally:
        on_disk.close()
    assert target.exists() and oldest.exists()


def test_a_backup_that_fails_leaves_no_file_behind(tmp_path, monkeypatch):
    # A backup cut short is no backup, and would count among the five automatic ones kept.
    on_disk = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)

    class FullDisk:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def backup(self, target: sqlite3.Connection, **options: object) -> None:
            raise sqlite3.OperationalError("database or disk is full")

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

    monkeypatch.setattr(on_disk, "_conn", FullDisk(on_disk._conn))
    try:
        with pytest.raises(sqlite3.OperationalError):
            on_disk.backup_before("auto-import", NOW)
    finally:
        monkeypatch.undo()
        on_disk.close()
    assert list((tmp_path / "backups").iterdir()) == []


def test_half_an_emoji_in_achievement_text_becomes_a_replacement_character(store):
    store.put_progress([ProgressRow(84508, "not_started", None, False, "Little Fei \ud83d", "player", NOW)])
    assert store.progress()[84508].note == "Little Fei \ufffd"
    store.push_change("Changed the note on \ud83d\ude00", {}, NOW)  # both halves of one emoji
    assert store.pop_change() == ("Changed the note on \U0001f600", {})
    store.add_import("astra", "state \U0001f600\ud83d.json", "f1", {}, NOW)
    assert store.imports()[0].file == "state \U0001f600\ufffd.json"


def test_half_an_emoji_in_a_list_version_becomes_a_replacement_character(store):
    store.install_achievement_list(b"one", source="github", sha256="a", version="7.1\ud83d", at=NOW)
    assert store.achievement_list().version == "7.1\ufffd"


def test_a_database_from_plan_2a1_gains_the_timer_tables_after_a_backup(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript(MIGRATIONS[0] + MIGRATIONS[1] + MIGRATIONS[2])
    old.execute("PRAGMA user_version = 3")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    assert store.timers() == [] and store.appearances() == {}
    store.close()
    assert len(list((tmp_path / "backups").glob("guildboard-auto-v3-*.db"))) == 1


def _own_timer(name: str = "Walk the dog") -> TimerRow:
    return TimerRow(0, None, name, "", '{"kind": "hours", "hours": 12}', None, True, None, None, NOW, NOW)


def test_a_timer_keeps_its_events_and_goes_with_them(store):
    timer_id = store.add_timer(_own_timer())
    assert timer_id == 1
    later = store.add_timer_event(timer_id, NOW + timedelta(hours=2), NOW + timedelta(hours=2), "player")
    first = store.add_timer_event(timer_id, NOW, NOW + timedelta(hours=3), "astra")
    events = store.timer_events(timer_id)
    by_time = [(first, NOW, "astra"), (later, NOW + timedelta(hours=2), "player")]
    assert [(e.id, e.at, e.source) for e in events] == by_time
    store.set_notified(timer_id, NOW + timedelta(hours=12))
    assert store.timer(timer_id).notified_ready_at == NOW + timedelta(hours=12)
    store.replace_timer_events(timer_id, events[:1])  # an undo puts the events back as they were, ids and all
    assert store.timer_events(timer_id) == events[:1]
    store.drop_timer(timer_id)
    assert store.timers() == [] and store.timer_events() == []


def test_a_removed_timer_comes_back_under_its_own_id(store):
    store.add_timer(_own_timer("First"))
    timer_id = store.add_timer(_own_timer())
    row = store.timer(timer_id)
    store.drop_timer(timer_id)
    store.put_timer(row)
    assert store.timer(timer_id) == row and [timer.id for timer in store.timers()] == [1, 2]


def test_a_pre_set_is_in_use_once(store):
    hilichurl = TimerRow(0, "hilichurl", "", "", "", None, True, None, None, NOW, NOW)
    store.add_timer(hilichurl)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_timer(hilichurl)
    with pytest.raises(sqlite3.IntegrityError):  # a timer of the player's own has a rule of its own
        store.add_timer(replace(hilichurl, preset=None))


def test_appearances_are_kept_by_game_day(store):
    later = NOW + timedelta(days=1)
    store.put_appearance(84508, date(2026, 10, 15), NOW)
    store.put_appearance(84508, date(2026, 10, 16), later, "astra")
    assert store.appearances() == {84508: {date(2026, 10, 15): NOW, date(2026, 10, 16): later}}
    store.put_appearance(84508, date(2026, 10, 15), None)
    assert store.appearances() == {84508: {date(2026, 10, 16): later}}


def test_a_database_from_plan_2a2_gains_layouts_widget_data_and_quick_timers_after_a_backup(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:4]))
    old.execute("PRAGMA user_version = 4")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    assert (store.layout("board"), store.widget_data("notes-1"), store.quick_timers()) == (None, None, [])
    store.close()
    assert len(list((tmp_path / "backups").glob("guildboard-auto-v4-*.db"))) == 1


def test_a_layout_is_kept_by_place_with_its_revision(store):
    assert store.layout("board") is None
    store.put_layout("board", '[{"id": "a"}]', 3, NOW)
    assert store.layout("board") == LayoutRow("board", '[{"id": "a"}]', 3, NOW)
    store.put_layout("board", "[]", 4, NOW + timedelta(minutes=1))
    assert store.layout("board") == LayoutRow("board", "[]", 4, NOW + timedelta(minutes=1))


def test_widget_data_is_kept_and_dropped_by_widget(store):
    store.put_widget_data("notes-1", '{"text": "Buy fish"}', NOW)
    store.put_widget_data("notes-2", '{"text": "Mint"}', NOW)
    store.put_widget_data("notes-2", '{"text": "Mint and Sweet Flowers"}', NOW)
    store.drop_widget_data(["notes-1", "notes-9"])
    assert (store.widget_data("notes-1"), store.widget_data("notes-2")) == (None, '{"text": "Mint and Sweet Flowers"}')


def test_quick_timers_are_kept_until_dropped_and_marked_once_reminded(store):
    later = store.add_quick_timer(600, NOW, NOW + timedelta(minutes=10))
    sooner = store.add_quick_timer(60, NOW, NOW + timedelta(minutes=1))
    assert store.quick_timers() == [
        QuickTimerRow(sooner, 60, NOW, NOW + timedelta(minutes=1), False),
        QuickTimerRow(later, 600, NOW, NOW + timedelta(minutes=10), False),
    ]
    store.set_quick_notified([sooner])
    assert [row.notified for row in store.quick_timers()] == [True, False]
    assert store.drop_quick_timer(sooner) is True and store.drop_quick_timer(sooner) is False
    assert [row.id for row in store.quick_timers()] == [later]


# ---------------------------------------------------------------- Pulls: schema 6 (the Pulls design, section 5)

UID = 700000001
OTHER_UID = 700000002


def _wish(
    wish_id: int, import_id: int, *, uid: int = UID, gacha_type: str = "301", pool: str = "character",
    name: str = "Bennett", item_type: str = "Character", rank: int = 4,
) -> WishRow:
    """A wish as the import keeps it: the server's own time text, and the moment that names (Europe is UTC+1)."""
    return WishRow(wish_id, uid, gacha_type, pool, name, item_type, rank, "2026-10-13 09:00:00", NOW, import_id)


def _banner(ann_id: int, starts_at: datetime = NOW, name: str = "Void Star's Advent") -> BannerRow:
    return BannerRow(
        ann_id, name, "character", ("Skirk",), f"https://sdk.hoyoverse.com/upload/ann/{ann_id}.png",
        starts_at, starts_at + timedelta(days=21), NOW,
    )


def _wiki_item(kind: str, name: str, rank: int) -> WikiItemRow:
    return WikiItemRow(kind, name, rank, f"https://act-webstatic.hoyoverse.com/{name}.png", "4321", NOW)


def test_a_version_5_database_gains_the_pulls_tables_after_a_backup(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:5]))
    old.execute("PRAGMA user_version = 5")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    assert store.wishes(UID) == [] and store.wish_imports() == [] and store.banners() == []
    store.close()
    backups = list((tmp_path / "backups").glob("guildboard-auto-v5-*.db"))
    assert len(backups) == 1
    tables = {"wishes", "wish_imports", "pulls_accounts", "wishlist", "banners", "wiki_items"}

    def tables_and_version(file) -> tuple[set[str], int]:
        conn = sqlite3.connect(file)
        try:
            found = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return found & tables, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    assert tables_and_version(path) == (tables, len(MIGRATIONS))
    assert tables_and_version(backups[0]) == (set(), 5)  # the backup is the database as it was


def test_wishes_are_kept_once_by_id_and_read_by_pool(store):
    run = store.start_wish_import(NOW)
    first = _wish(1700000000000000001, run, gacha_type="400", name="Cool Steel", item_type="Weapon", rank=3)
    second = _wish(1700000000000000002, run)
    assert store.add_wishes([first, second]) == 2
    assert store.add_wishes([first, second]) == 0  # each is kept once, by its id
    assert store.wishes(UID, "character") == [first, second]  # oldest first
    assert store.wishes(UID) == [first, second] and store.wishes(UID, "weapon") == []
    assert store.wishes_before(UID, None, 1) == [second]  # newest first
    assert store.wishes_before(UID, second.id, 10) == [first]
    assert store.wishes_before(UID, first.id, 10) == []
    assert store.wish_count(UID) == 2
    assert store.has_wish(first.id) and not store.has_wish(1700000000000000003)
    store.finish_wish_import(run, NOW, "ok", UID, 2, 1, None, ("character",))
    assert store.wish_uids() == [UID]
    assert store.wish_imports()[0].found == 2
    assert store.wish_imports()[0].pools == ("character",)


def test_a_wish_answered_twice_is_kept_as_the_first_import_found_it(store):
    first_run = store.start_wish_import(NOW)
    store.add_wishes([_wish(10, first_run)])
    second_run = store.start_wish_import(NOW + timedelta(days=1))
    assert store.add_wishes([_wish(10, second_run, name="Someone else"), _wish(11, second_run)]) == 1
    assert [(row.id, row.name, row.import_id) for row in store.wishes(UID)] == [
        (10, "Bennett", first_run), (11, "Bennett", second_run),
    ]


def test_the_wishes_of_another_uid_are_kept_apart(store):
    run = store.start_wish_import(NOW)
    store.add_wishes([_wish(1, run), _wish(2, run, uid=OTHER_UID, pool="weapon", gacha_type="302")])
    assert [row.id for row in store.wishes(UID)] == [1]
    assert [row.id for row in store.wishes(OTHER_UID, "weapon")] == [2] and store.wishes(OTHER_UID, "character") == []
    assert [row.id for row in store.wishes_before(OTHER_UID, None, 10)] == [2]
    assert (store.wish_count(UID), store.wish_count(OTHER_UID)) == (1, 1)


def test_wishes_are_read_in_the_order_of_their_ids_whatever_the_order_they_were_kept_in(store):
    run = store.start_wish_import(NOW)
    store.add_wishes([_wish(30, run), _wish(10, run), _wish(20, run)])
    assert [row.id for row in store.wishes(UID)] == [10, 20, 30]
    assert [row.id for row in store.wishes_before(UID, None, 2)] == [30, 20]
    assert [row.id for row in store.wishes_before(UID, 30, 2)] == [20, 10]


def test_wish_uids_come_the_account_the_newest_import_found_first(store):
    def one_import(day: int, found_uid: int, outcome: str, kept: list[tuple[int, int]]) -> None:
        """An import on `day` that found `found_uid` and kept the wishes given as (id, uid)."""
        at = NOW + timedelta(days=day)
        run = store.start_wish_import(at)
        store.add_wishes([_wish(wish_id, run, uid=uid) for wish_id, uid in kept])
        store.finish_wish_import(run, at, outcome, found_uid, len(kept), 1, None, ())

    assert store.wish_uids() == []
    one_import(0, UID, "ok", [(1, UID)])
    one_import(1, OTHER_UID, "ok", [(2, OTHER_UID)])
    assert store.wish_uids() == [OTHER_UID, UID]
    one_import(2, UID, "ok", [])  # the first account again, with nothing new: the newest import found it
    assert store.wish_uids() == [UID, OTHER_UID]
    one_import(3, 700000003, "failed", [])  # a uid of which no wish is kept has no history to list
    assert store.wish_uids() == [UID, OTHER_UID]
    # Guild Board closed during an import that had kept a pool: its uid was never recorded, but its wishes are kept.
    closed = store.start_wish_import(NOW + timedelta(days=4))
    store.add_wishes([_wish(3, closed, uid=700000003)])
    store.fail_running_wish_imports(NOW + timedelta(days=5))
    assert store.wish_uids() == [700000003, UID, OTHER_UID]


def test_an_import_is_recorded_from_its_start_to_its_end_with_the_pools_it_finished(store):
    assert store.wish_imports() == []
    first = store.start_wish_import(NOW)
    assert store.wish_imports() == [WishImportRow(first, None, NOW, None, "running", 0, 0, None, ())]
    store.finish_wish_import(first, NOW + timedelta(seconds=9), "ok", UID, 41, 5, None, ("character", "weapon"))
    sentence = "HoYoverse says there were too many requests. Wait a minute, then press Import again."
    second = store.start_wish_import(NOW + timedelta(days=1))
    store.finish_wish_import(second, NOW + timedelta(days=1, seconds=3), "failed", None, 0, 1, sentence)
    rows = store.wish_imports()
    assert [row.id for row in rows] == [second, first]  # newest first
    assert rows[1] == WishImportRow(
        first, UID, NOW, NOW + timedelta(seconds=9), "ok", 41, 5, None, ("character", "weapon")
    )
    assert (rows[0].outcome, rows[0].message, rows[0].pools) == ("failed", sentence, ())
    assert [row.id for row in store.wish_imports(limit=1)] == [second]
    with pytest.raises(sqlite3.IntegrityError):  # an outcome the table does not know
        store.finish_wish_import(second, NOW, "half", None, 0, 0, None)


def test_an_import_left_running_is_failed_at_the_next_start(store):
    left = store.start_wish_import(NOW)
    done = store.start_wish_import(NOW + timedelta(minutes=5))
    store.finish_wish_import(done, NOW + timedelta(minutes=6), "ok", UID, 3, 1, None, ("character",))
    store.fail_running_wish_imports(NOW + timedelta(hours=1))
    rows = {row.id: row for row in store.wish_imports()}
    assert (rows[left].outcome, rows[left].message) == ("failed", "TUSKFiZZY closed before this import finished.")
    assert rows[left].finished_at == NOW + timedelta(hours=1)
    assert (rows[done].outcome, rows[done].message, rows[done].found) == ("ok", None, 3)  # a finished one stays


def test_pulls_account_wishlist_banners_and_wiki_items_round_trip(store):
    assert store.pulls_account(UID) == PullsAccountRow(UID, "", "", None, None, None, {}, {})
    kept = PullsAccountRow(
        UID, "Skyward Blade", "Skirk", 12_000, 14, NOW,
        {"character": {"pity": 21, "guaranteed": True}}, {"weapon": {"points": 1, "at": NOW}},
    )
    store.put_pulls_account(kept)
    assert store.pulls_account(UID) == kept
    assert store.pulls_account(UID).points["weapon"]["at"] == NOW  # a datetime, as every time in a row is
    assert store.pulls_account(OTHER_UID) == PullsAccountRow(OTHER_UID, "", "", None, None, None, {}, {})
    cleared = PullsAccountRow(UID, "", "Skirk", None, None, None, {}, {})
    store.put_pulls_account(cleared)  # a row is replaced whole, not merged with the one before
    assert store.pulls_account(UID) == cleared

    entry = store.add_wishlist(UID, "Citlali", "character", date(2026, 11, 11), 1, NOW)
    assert [row.name for row in store.wishlist(UID)] == ["Citlali"]
    row = store.wishlist(UID)[0]
    assert row == WishlistRow(entry, UID, "Citlali", "character", date(2026, 11, 11), 1, row.position, NOW)
    assert store.drop_wishlist(entry) is True and store.drop_wishlist(entry) is False
    assert store.wishlist(UID) == []

    store.replace_banners([_banner(7001)])
    second = [_banner(7003, NOW + timedelta(days=21), "Epitome Invocation"), _banner(7002, NOW, "Another")]
    store.replace_banners(second)  # the kept set becomes the second one
    assert store.banners() == sorted(second, key=lambda banner: banner.starts_at)
    assert store.banners()[0].featured == ("Skirk",)

    store.replace_wiki_items("character", [_wiki_item("character", "Skirk", 5)])
    assert store.wiki_items()[("character", "Skirk")].rank == 5


def test_the_time_of_a_fate_point_is_kept_as_store_iso_text_and_comes_back_a_datetime(store):
    later = NOW + timedelta(days=3, hours=2)
    zone = timezone(timedelta(hours=1))  # a time in the server's own offset is kept as the same moment
    row = PullsAccountRow(
        UID, "", "", None, None, None, {},
        {"weapon": {"points": 1, "at": NOW}, "chronicled": {"points": 0, "at": later.astimezone(zone)}},
    )
    store.put_pulls_account(row)
    kept = store.pulls_account(UID).points
    assert kept == {"weapon": {"points": 1, "at": NOW}, "chronicled": {"points": 0, "at": later}}
    assert all(isinstance(entry["at"], datetime) for entry in kept.values())
    assert kept["chronicled"]["at"].utcoffset() == timedelta(0)  # as iso() keeps it: UTC
    # The table holds what the contract says, JSON with the time as store.iso() text.
    [text] = store._conn.execute("SELECT points FROM pulls_accounts WHERE uid = ?", (UID,)).fetchone()
    assert json.loads(text) == {
        "weapon": {"points": 1, "at": "2026-10-13T08:00:00+00:00"},
        "chronicled": {"points": 0, "at": "2026-10-16T10:00:00+00:00"},
    }
    # A point with no time, and the corrections (which hold no time), are kept as they are given.
    plain = replace(row, corrections={"weapon": {"pity": 3, "guaranteed": False}}, points={"weapon": {"points": 0}})
    store.put_pulls_account(plain)
    assert store.pulls_account(UID) == plain
    with pytest.raises(ValueError):  # a time with no zone is refused, as everywhere in the store
        store.put_pulls_account(replace(row, points={"weapon": {"points": 1, "at": datetime(2026, 10, 13, 8, 0)}}))


def test_the_wishlist_is_kept_in_order_and_a_put_replaces_an_entry(store):
    one = store.add_wishlist(UID, "Citlali", "character", date(2026, 11, 11), 1, NOW)
    two = store.add_wishlist(UID, "Skyward Blade", "weapon", date(2026, 12, 1), 2, NOW)
    other = store.add_wishlist(OTHER_UID, "Mavuika", "character", date(2026, 11, 11), 1, NOW)
    assert [row.id for row in store.wishlist(UID)] == [one, two]
    assert [row.id for row in store.wishlist(OTHER_UID)] == [other]
    first, second = store.wishlist(UID)
    moved = replace(first, name="Skirk", copies=3, position=second.position + 1)  # changed, and behind the other
    store.put_wishlist(moved)
    assert store.wishlist(UID) == [second, moved]
    store.put_wishlist(first)
    assert store.wishlist(UID) == [first, second]
    store.drop_wishlist(one)
    assert store.wishlist(UID) == [second]
    store.put_wishlist(first)  # an entry that was dropped comes back under its own id
    assert store.wishlist(UID) == [first, second]


def test_half_an_emoji_in_a_wishlist_name_becomes_a_replacement_character(store):
    store.add_wishlist(UID, "Citlali\ud83d", "character", date(2026, 11, 11), 1, NOW)
    assert store.wishlist(UID)[0].name == "Citlali�"


def test_replacing_the_banners_is_one_transaction(store):
    kept = [_banner(7001)]
    store.replace_banners(kept)
    with pytest.raises(sqlite3.IntegrityError):  # the second row cannot be kept
        store.replace_banners([_banner(7002), replace(_banner(7003), art_url=None)])
    assert store.banners() == kept  # neither the delete nor the first row stayed
    store.replace_banners([_banner(7004, name="First"), _banner(7004, name="Second")])  # an announcement given twice
    assert [(banner.ann_id, banner.name) for banner in store.banners()] == [(7004, "Second")]
    store.replace_banners([])
    assert store.banners() == []


def test_replacing_the_wiki_items_of_one_kind_leaves_the_other(store):
    store.replace_wiki_items("character", [_wiki_item("character", "Skirk", 5), _wiki_item("character", "Amber", 4)])
    store.replace_wiki_items("weapon", [_wiki_item("weapon", "Skyward Blade", 4)])
    assert set(store.wiki_items()) == {("character", "Skirk"), ("character", "Amber"), ("weapon", "Skyward Blade")}
    store.replace_wiki_items("character", [_wiki_item("character", "Skirk", 5)])
    assert set(store.wiki_items()) == {("character", "Skirk"), ("weapon", "Skyward Blade")}
    with pytest.raises(ValueError):  # a row of the other kind is a mistake of the caller, and nothing is replaced
        store.replace_wiki_items("weapon", [_wiki_item("character", "Skirk", 5)])
    assert set(store.wiki_items()) == {("character", "Skirk"), ("weapon", "Skyward Blade")}
    # A name the wiki lists twice (an entry that moved between two pages being read) is kept once, the last.
    store.replace_wiki_items("weapon", [_wiki_item("weapon", "Amos' Bow", 4), _wiki_item("weapon", "Amos' Bow", 5)])
    assert store.wiki_items()[("weapon", "Amos' Bow")].rank == 5
    assert ("weapon", "Skyward Blade") not in store.wiki_items()


# ---------------------------------------------------------------- Pulls second pass: schema 7 (the switches, typed events)


def test_schema_7_adds_the_switches_and_typed_events(tmp_path):
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:6]))  # the database of the Pulls build: schema 6
    old.execute(
        "INSERT INTO pulls_accounts (uid, charted_weapon, chronicled_path, balance_primogems, balance_fates, "
        "balance_set_at, corrections, points) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (UID, "Skyward Blade", "Skirk", 12_000, 14, iso(NOW), '{"character": {"pity": 21, "guaranteed": true}}', "{}"),
    )
    old.execute("PRAGMA user_version = 6")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    row = store.pulls_account(UID)
    assert row == PullsAccountRow(
        UID, "Skyward Blade", "Skirk", 12_000, 14, NOW, {"character": {"pity": 21, "guaranteed": True}}, {}
    )  # unchanged
    assert row.welkin is False and row.gnostic_hymn is False and row.events_typed == {}
    # The table holds what the migration says: two switches off and an empty JSON object.
    kept = store._conn.execute("SELECT welkin, gnostic_hymn, events_typed FROM pulls_accounts").fetchall()
    assert kept == [(0, 0, "{}")]
    store.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    conn.close()
    backups = list((tmp_path / "backups").glob("guildboard-auto-v6-*.db"))
    assert len(backups) == 1  # the database as it was is copied before the schema moves


def test_the_switches_and_typed_events_round_trip_by_uid(store):
    kept = PullsAccountRow(UID, "", "", None, None, None, {}, {}, True, False, {"7.2": 2330, "7.3": 0})
    store.put_pulls_account(kept)
    assert store.pulls_account(UID) == kept
    assert store.pulls_account(OTHER_UID) == PullsAccountRow(OTHER_UID, "", "", None, None, None, {}, {})
    [(welkin, hymn, typed)] = store._conn.execute("SELECT welkin, gnostic_hymn, events_typed FROM pulls_accounts")
    assert (welkin, hymn, json.loads(typed)) == (1, 0, {"7.2": 2330, "7.3": 0})
    store.put_pulls_account(replace(kept, welkin=False, gnostic_hymn=True, events_typed={}))  # replaced whole
    assert store.pulls_account(UID) == replace(kept, welkin=False, gnostic_hymn=True, events_typed={})


def test_gallery_favourites_round_trip(store):
    at = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    assert store.gallery_favourites() == set()
    store.set_gallery_favourite("20261026211400.png", True, at)
    store.set_gallery_favourite("20261026211400.png", True, at)  # twice is once
    store.set_gallery_favourite("20261020185500.png", True, at)
    store.set_gallery_favourite("20261020185500.png", False, at)
    assert store.gallery_favourites() == {"20261026211400.png"}


# ---------------------------------------------------------------------------------------------- the Characters tables


CHARACTERS_TABLES = {
    "char_focus", "char_plans", "char_card_edits", "char_ticks", "char_held", "char_theater", "char_notices",
    "wiki_entries", "art",
}


def test_a_version_10_database_gains_the_characters_tables_after_a_backup(tmp_path):
    # The Characters tables are the 11th migration: the plan called them schema 8, before the gift codes, the gallery
    # and the ledger took 8 to 10. Sync is the 12th (the online plan, Task C2), its outbox's numbered lines the 13th, and the
    # database moves on to them. The 14th only deletes rows, the 15th keeps the endgame's cycles (the Stats design, 3.5),
    # and the 16th lets the wiki's table hold the Tutorial entries (the Stats design, 3.2).
    assert len(MIGRATIONS) == 17
    path = tmp_path / "guildboard.db"
    old = sqlite3.connect(path)
    old.executescript("".join(MIGRATIONS[:10]))
    old.execute("PRAGMA user_version = 10")
    old.commit()
    old.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    assert store.focus(UID) == [] and store.char_plans(UID) == {} and store.art_rows() == []
    store.close()
    backups = list((tmp_path / "backups").glob("guildboard-auto-v10-*.db"))
    assert len(backups) == 1

    def tables_and_version(file) -> tuple[set[str], int]:
        conn = sqlite3.connect(file)
        try:
            found = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return found & CHARACTERS_TABLES, conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    assert len(CHARACTERS_TABLES) == 9
    assert tables_and_version(path) == (CHARACTERS_TABLES, len(MIGRATIONS))
    assert tables_and_version(backups[0]) == (set(), 10)  # the backup is the database as it was


def test_the_focus_keeps_four_in_order_and_refuses_a_fifth(store):
    four = ["sandrone", "odette", "alyosha", "jahoda"]
    store.put_focus(UID, four)
    assert store.focus(UID) == four
    store.put_focus(OTHER_UID, ["jahoda"])
    store.put_focus(UID, ["jahoda", "sandrone"])  # replaces, in the order given
    assert store.focus(UID) == ["jahoda", "sandrone"]
    assert store.focus(OTHER_UID) == ["jahoda"]
    with pytest.raises(ValueError):
        store.put_focus(UID, [*four, "flins"])
    assert store.focus(UID) == ["jahoda", "sandrone"]  # a refused change leaves the focus as it was
    store.put_focus(UID, [])
    assert store.focus(UID) == []


def test_plans_card_edits_ticks_held_theater_and_notices_round_trip(store):
    later = NOW + timedelta(hours=1)
    plan = {"level": 90, "talents": {"normal": 10, "skill": 10, "burst": 10}, "weapon": None, "typed": None}
    store.put_char_plan(UID, "sandrone", plan, NOW)
    store.put_char_plan(OTHER_UID, "sandrone", {"level": 80}, NOW)
    assert store.char_plans(UID) == {"sandrone": store_module.CharPlanRow(UID, "sandrone", plan, NOW)}
    store.put_char_plan(UID, "sandrone", {**plan, "level": 80}, later)  # replaced whole
    assert store.char_plans(UID)["sandrone"].plan["level"] == 80 and store.char_plans(UID)["sandrone"].updated_at == later
    assert store.drop_char_plan(UID, "sandrone") is True and store.drop_char_plan(UID, "sandrone") is False
    assert store.char_plans(UID) == {} and set(store.char_plans(OTHER_UID)) == {"sandrone"}

    store.put_card_edits(UID, "sandrone", {"notes": "Mine.", "goals": {"cr": 80}}, NOW)
    store.put_card_edits(UID, "odette", {"early": True}, NOW)
    assert store.card_edits(UID) == {"sandrone": {"notes": "Mine.", "goals": {"cr": 80}}, "odette": {"early": True}}
    store.put_card_edits(UID, "sandrone", {}, later)  # no edits left: the row goes
    assert store.card_edits(UID) == {"odette": {"early": True}}
    assert store._conn.execute("SELECT COUNT(*) FROM char_card_edits WHERE character = 'sandrone'").fetchone()[0] == 0

    days = [date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)]
    store.put_tick(UID, days[0], "books:sandrone", True, NOW)
    store.put_tick(UID, days[1], "books:sandrone", True, NOW)
    store.put_tick(UID, days[1], "weekly:the-doctor", True, NOW)
    store.put_tick(UID, days[2], "boss:alyosha", True, NOW)
    store.put_tick(UID, days[2], "boss:alyosha", True, later)  # twice is once
    store.put_tick(OTHER_UID, days[1], "books:odette", True, NOW)
    assert store.ticks(UID, days[1]) == {
        (days[1], "books:sandrone"), (days[1], "weekly:the-doctor"), (days[2], "boss:alyosha"),
    }  # earlier days are left out
    assert store.ticks(UID, date(2026, 10, 27)) == set()
    store.put_tick(UID, days[1], "weekly:the-doctor", False, later)  # a tick taken back
    assert (days[1], "weekly:the-doctor") not in store.ticks(UID, days[0])
    assert len(store.ticks(UID, days[0])) == 3

    assert store.held(UID) == {}
    store.put_held(UID, {"mora": 2410000, "philosophies-of-vagrancy": 9, "crown-of-insight": 2}, NOW)
    assert store.held(UID) == {"mora": 2410000, "philosophies-of-vagrancy": 9, "crown-of-insight": 2}
    store.put_held(UID, {"mora": None, "philosophies-of-vagrancy": 12}, later)  # None drops that key alone
    assert store.held(UID) == {"philosophies-of-vagrancy": 12, "crown-of-insight": 2}
    assert store.held(OTHER_UID) == {}

    assert store.theater_elements(UID, "2026-11") is None  # none before a put
    store.put_theater_elements(UID, "2026-10", ["Cryo", "Electro", "Anemo"], NOW)
    assert store.theater_elements(UID, "2026-10") == ("Cryo", "Electro", "Anemo")
    assert store.theater_elements(UID, "2026-11") is None and store.theater_elements(OTHER_UID, "2026-10") is None
    store.put_theater_elements(UID, "2026-10", ("Pyro", "Hydro", "Geo"), later)  # replaced
    assert store.theater_elements(UID, "2026-10") == ("Pyro", "Hydro", "Geo")

    store.add_notice_sent(UID, "weekly", date(2026, 10, 25), NOW)
    store.add_notice_sent(UID, "weekly", date(2026, 10, 25), later)  # twice is once
    store.add_notice_sent(UID, "domain", date(2026, 10, 26), NOW)
    store.add_notice_sent(UID, "ready", date(2026, 10, 20), NOW)
    assert store.notices_sent(UID, date(2026, 10, 25)) == {("weekly", date(2026, 10, 25)), ("domain", date(2026, 10, 26))}
    assert store.notices_sent(OTHER_UID, date(2026, 10, 1)) == set()


def test_wiki_entries_and_art_rows_round_trip(store):
    first = store_module.WikiEntryRow("10920", "character", "Sandrone", {"element": "Cryo", "talents": [1, 2]}, "7.1", NOW)
    weapon = store_module.WikiEntryRow("10949", "weapon", "A Teaspoon of Transcendence", {"atk": 674}, "7.1", NOW)
    store.put_wiki_entry(first)
    store.put_wiki_entry(weapon)
    assert store.wiki_entry("10920") == first and store.wiki_entry("0") is None
    assert store.wiki_entries("character") == {"10920": first}
    assert store.wiki_entries("set") == {} and set(store.wiki_entries("weapon")) == {"10949"}
    newer = replace(first, version="7.2", read_at=NOW + timedelta(days=1))
    store.put_wiki_entry(newer)  # replaced
    assert store.wiki_entries("character") == {"10920": newer}
    with pytest.raises(sqlite3.IntegrityError):
        store.put_wiki_entry(replace(first, entry_page_id="1", kind="monster"))  # a kind the table does not hold
    # The artifact sets of menu 5 are kept in wiki_items, which takes the kind "set" as well.
    store.replace_wiki_items("set", [WikiItemRow("set", "Blizzard Strayer", 5, "https://bbs.hoyolab.com/x.png", "10760", NOW)])
    assert ("set", "Blizzard Strayer") in store.wiki_items()

    assert store.art_rows() == [] and store.art("a") is None
    rows = [
        store_module.ArtRow("c", "https://act-webstatic.hoyoverse.com/c.png", "c.png", 300, NOW + timedelta(hours=2)),
        store_module.ArtRow("a", "https://act-webstatic.hoyoverse.com/a.png", "a.png", 100, NOW),
        store_module.ArtRow("b", "https://act-webstatic.hoyoverse.com/b.png", "b.png", 200, NOW + timedelta(hours=1)),
    ]
    for row in rows:
        store.put_art(row)
    assert store.art("b") == rows[2]
    assert [row.key for row in store.art_rows()] == ["a", "b", "c"]  # the oldest read first
    store.put_art(replace(rows[1], read_at=NOW + timedelta(hours=5)))  # "a" read again: it is the newest now
    assert [row.key for row in store.art_rows()] == ["b", "c", "a"]
    assert store.drop_art("a") is True and store.drop_art("a") is False
    assert [row.key for row in store.art_rows()] == ["b", "c"]


def _point_rows(store):
    return store._conn.execute("SELECT key, taken_at, game_day, value FROM points ORDER BY key, taken_at").fetchall()


def test_a_value_repeated_on_its_game_day_is_kept_once(store):
    day = date(2026, 10, 13)
    store.add_points(None, NOW, day, [Point("k", 5), Point("other", 1)])
    store.add_points(None, NOW + timedelta(hours=1), day, [Point("k", 5), Point("other", 2)])  # k repeats, other changes
    store.add_points(None, NOW + timedelta(hours=2), day, [Point("k", 5)])
    store.add_points(None, NOW + timedelta(hours=3), day, [Point("k", 6)])
    store.add_points(None, NOW + timedelta(hours=4), day, [Point("k", 5)])  # back to an earlier value: a change
    store.add_points(None, NOW + timedelta(days=1), date(2026, 10, 14), [Point("k", 5)])  # a new day starts afresh
    rows = [(r[0], r[1][11:16], r[3]) for r in _point_rows(store)]
    assert rows == [
        ("k", "08:00", 5.0), ("k", "11:00", 6.0), ("k", "12:00", 5.0), ("k", "08:00", 5.0),
        ("other", "08:00", 1.0), ("other", "09:00", 2.0),
    ]
    assert store.latest_point_value("k") == 5.0


def test_a_point_older_than_the_newest_row_is_kept_though_its_value_is_the_same(store):
    # The PC's clock was moved back past the last sync: the row is no repeat of what comes later.
    day = date(2026, 10, 13)
    store.add_points(None, NOW + timedelta(hours=1), day, [Point("k", 5)])
    store.add_points(None, NOW, day, [Point("k", 5)])
    assert [r[1][11:16] for r in _point_rows(store)] == ["08:00", "09:00"]


def test_the_upgrade_to_schema_16_keeps_the_wikis_entries_and_takes_its_tutorial_entries(tmp_path):
    # The Endgame card (the Stats design, 3.2) reads three of the wiki's Tutorial entries, a kind the table did not hold.
    path = tmp_path / "guildboard.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:15]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 15")
    conn.execute(
        "INSERT INTO wiki_entries (entry_page_id, kind, name, data, version, read_at) VALUES ('10920', 'character', "
        "'Sandrone', '{\"element\": \"Cryo\"}', '7.1', ?)", (NOW.isoformat(),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO wiki_entries (entry_page_id, kind, name, data, version, read_at) VALUES ('7786', 'tutorial', "
            "'Stygian Onslaught', '{}', '7.1', ?)", (NOW.isoformat(),),
        )
    conn.commit()
    conn.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    try:
        kept = store_module.WikiEntryRow("10920", "character", "Sandrone", {"element": "Cryo"}, "7.1", NOW)
        assert store.wiki_entries("character") == {"10920": kept}
        tutorial = store_module.WikiEntryRow("7786", "tutorial", "Stygian Onslaught", {"pictures": []}, "7.1", NOW)
        store.put_wiki_entry(tutorial)
        assert store.wiki_entry("7786") == tutorial and store.wiki_entries("tutorial") == {"7786": tutorial}
        with pytest.raises(sqlite3.IntegrityError):
            store.put_wiki_entry(replace(tutorial, entry_page_id="1", kind="monster"))
    finally:
        store.close()


def test_the_upgrade_to_schema_14_gives_the_freed_room_back(tmp_path):
    path = tmp_path / "guildboard.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:13]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 13")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    conn.executemany(
        "INSERT INTO points (key, taken_at, game_day, value) VALUES (?, ?, '2026-01-01', 5.0)",
        [(f"key-{index % 40}", (start + timedelta(seconds=index)).isoformat()) for index in range(20_000)],
    )
    conn.commit()
    conn.close()
    before = path.stat().st_size
    store = Store.open(path, tmp_path / "backups", NOW)
    store.close()
    assert path.stat().st_size < before / 4


def test_a_later_start_gives_nothing_back_again(tmp_path, monkeypatch):
    store = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)
    store.close()
    compacted = []
    monkeypatch.setattr(Store, "_compact", lambda self: compacted.append(1))
    Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW).close()
    assert compacted == []


def test_the_upgrade_to_schema_14_deletes_the_repeats_of_a_game_day(tmp_path):
    path = tmp_path / "guildboard.db"
    conn = sqlite3.connect(path)
    for script in MIGRATIONS[:13]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 13")
    rows = [
        ("k", "2026-10-13T08:00:00+00:00", "2026-10-13", 5.0), ("k", "2026-10-13T09:00:00+00:00", "2026-10-13", 5.0),
        ("k", "2026-10-13T10:00:00+00:00", "2026-10-13", 6.0), ("k", "2026-10-13T11:00:00+00:00", "2026-10-13", 6.0),
        ("k", "2026-10-13T12:00:00+00:00", "2026-10-13", 5.0), ("k", "2026-10-14T08:00:00+00:00", "2026-10-14", 5.0),
        ("k", "2026-10-14T09:00:00+00:00", "2026-10-14", 5.0), ("j", "2026-10-13T08:00:00+00:00", "2026-10-13", 1.0),
        ("j", "2026-10-13T09:00:00+00:00", "2026-10-13", 1.0),
    ]
    conn.executemany("INSERT INTO points (key, taken_at, game_day, value) VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    store = Store.open(path, tmp_path / "backups", NOW)
    kept = [(r[0], r[1][5:16], r[3]) for r in _point_rows(store)]
    store.close()
    assert kept == [
        ("j", "10-13T08:00", 1.0),
        ("k", "10-13T08:00", 5.0), ("k", "10-13T10:00", 6.0), ("k", "10-13T12:00", 5.0), ("k", "10-14T08:00", 5.0),
    ]
    assert list((tmp_path / "backups").glob("guildboard-auto-v13-*.db"))  # backed up first, as every upgrade is


def _count_full_checks(monkeypatch):
    calls = []
    real = store_module._full_check

    def counting(conn):
        calls.append(1)
        return real(conn)

    monkeypatch.setattr(store_module, "_full_check", counting)
    return calls


def test_the_full_check_runs_once_a_day(tmp_path, monkeypatch):
    path, backups = tmp_path / "guildboard.db", tmp_path / "backups"
    Store.open(path, backups, NOW).close()  # a new database has nothing to check
    calls = _count_full_checks(monkeypatch)
    first = Store.open(path, backups, NOW)  # the day's first start: the daily copy is not made yet
    assert len(calls) == 1
    first.daily_backup(NOW)
    first.close()
    for hour in (1, 2):  # the day's later starts follow a clean close, and today's copy is there
        again = Store.open(path, backups, NOW + timedelta(hours=hour))
        again.close()
    assert len(calls) == 1
    Store.open(path, backups, NOW + timedelta(days=1)).close()  # the next day has no copy yet
    assert len(calls) == 2


def test_the_full_check_runs_after_a_close_that_never_came(tmp_path, monkeypatch):
    path, backups = tmp_path / "guildboard.db", tmp_path / "backups"
    store = Store.open(path, backups, NOW)
    store.daily_backup(NOW)
    store.close()
    calls = _count_full_checks(monkeypatch)
    Store.open(path, backups, NOW + timedelta(hours=1)).close()
    assert calls == []
    killed = Store.open(path, backups, NOW + timedelta(hours=2))  # never closed: the process was killed
    killed._conn.close()
    Store.open(path, backups, NOW + timedelta(hours=3)).close()
    assert len(calls) == 1


def test_a_damaged_database_is_refused_by_the_check_that_runs(tmp_path):
    path, backups = tmp_path / "guildboard.db", tmp_path / "backups"
    store = Store.open(path, backups, NOW)
    store.add_points(None, NOW, NOW.date(), [Point(f"key.{number}", number) for number in range(400)])
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store._conn.close()  # not a clean close
    data = bytearray(path.read_bytes())
    for offset in range(8192, len(data) - 4096, 4096):  # damage the middle of every page past the first two
        data[offset + 2000: offset + 2060] = b"\xff" * 60
    path.write_bytes(bytes(data))
    with pytest.raises(StoreError, match="left untouched"):
        Store.open(path, backups, NOW + timedelta(hours=1))


def test_an_open_that_fails_in_the_upgrade_lets_go_of_the_file(tmp_path, monkeypatch):
    path = tmp_path / "guildboard.db"
    Store.open(path, tmp_path / "backups", NOW).close()
    monkeypatch.setattr(store_module, "MIGRATIONS", (*MIGRATIONS, "ALTER TABLE no_such_table ADD COLUMN x;"))
    with pytest.raises(sqlite3.Error):
        Store.open(path, tmp_path / "backups", NOW)
    path.unlink()  # Windows refuses while any connection holds the file
    for side in ("-wal", "-shm"):
        (tmp_path / f"guildboard.db{side}").unlink(missing_ok=True)


def test_a_copy_left_unfinished_is_not_todays_daily_nor_listed(tmp_path, monkeypatch):
    # A Guild Board killed while it copies leaves what it had written. The copy is written under another name and takes its
    # own only when whole, so that what is left is no daily copy, counts toward nothing and is never offered for restore.
    backups = tmp_path / "backups"
    on_disk = Store.open(tmp_path / "guildboard.db", backups, NOW)
    seen: dict[str, list[str]] = {}
    real = sqlite3.Connection

    class Interrupted:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def backup(self, target: sqlite3.Connection, **options: object) -> None:
            self._connection.backup(target, **options)
            seen["during"] = sorted(p.name for p in backups.iterdir())
            raise KeyboardInterrupt  # the process dies after the pages are written, before the copy is whole

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

    monkeypatch.setattr(on_disk, "_conn", Interrupted(on_disk._conn))
    with pytest.raises(KeyboardInterrupt):
        on_disk.daily_backup(NOW)
    monkeypatch.undo()
    assert seen["during"] == ["guildboard-daily-20261013-080000.db.tmp"]
    assert list(backups.iterdir()) == []  # an interrupt that Python sees removes what it began
    # And a file the dead process could not remove: it is not a daily copy, so the day's copy is made.
    leftover = backups / "guildboard-daily-20261013-070000.db.tmp"
    leftover.write_bytes(b"half")
    made = on_disk.daily_backup(NOW)
    on_disk.close()
    assert made is not None and made.name == "guildboard-daily-20261013-080000.db"
    assert sqlite3.connect(made).execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_an_old_unfinished_copy_is_swept_with_the_pruning(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    old, fresh = backups / "guildboard-daily-20261001-000000.db.tmp", backups / "guildboard-manual-20261013-070000.db.tmp"
    old.write_bytes(b"half")
    fresh.write_bytes(b"half")  # a worker thread may be writing it this moment
    stamp = datetime.now().timestamp()
    os.utime(old, (stamp - 7200, stamp - 7200))
    on_disk = Store.open(tmp_path / "guildboard.db", backups, NOW)
    on_disk.backup_before("auto-import", NOW)
    on_disk.close()
    assert not old.exists() and fresh.exists()


def test_pruning_removes_a_copys_side_files(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    for index in range(7):
        file = backups / f"guildboard-auto-import-2026010{index}-000000.db"
        file.write_bytes(b"x")
        for side in ("-wal", "-shm"):
            Path(str(file) + side).write_bytes(b"y")
        os.utime(file, (1_700_000_000 + index, 1_700_000_000 + index))
    on_disk = Store.open(tmp_path / "guildboard.db", backups, NOW)
    on_disk._prune_auto_backups()
    on_disk.close()
    assert sorted(p.name for p in backups.iterdir()) == sorted(
        name for index in range(2, 7) for name in (
            f"guildboard-auto-import-2026010{index}-000000.db",
            f"guildboard-auto-import-2026010{index}-000000.db-shm",
            f"guildboard-auto-import-2026010{index}-000000.db-wal",
        )
    )

    # Daily copies the same.
    on_disk = Store.open(tmp_path / "guildboard.db", backups, NOW)
    for day in range(1, 17):
        file = backups / f"guildboard-daily-202609{day:02d}-000000.db"
        file.write_bytes(b"x")
        Path(str(file) + "-wal").write_bytes(b"y")
    on_disk.daily_backup(NOW)
    on_disk.close()
    left = sorted(p.name for p in backups.glob("guildboard-daily-*"))
    assert len([n for n in left if n.endswith(".db")]) == 14
    assert len(left) == 14 + 13  # the three oldest of the seventeen went, and with them their side files
    assert not [n for n in left if n.startswith(("guildboard-daily-20260901", "guildboard-daily-20260902", "guildboard-daily-20260903"))]


def test_the_copy_before_a_migration_survives_five_imports(tmp_path):
    # An older Guild Board can open only the copy made before the upgrade. Imports make five copies of their own, and the
    # five kept were the newest of all of them.
    path, backups = tmp_path / "guildboard.db", tmp_path / "backups"
    old = sqlite3.connect(path)
    for script in MIGRATIONS[:12]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 12")
    old.commit()
    old.close()
    on_disk = Store.open(path, backups, NOW)
    before_upgrade = backups / "guildboard-auto-v12-20261013-080000.db"
    assert before_upgrade.exists()
    for minute in range(1, 8):
        on_disk.backup_before("auto-import", NOW + timedelta(minutes=minute))
    on_disk.close()
    assert before_upgrade.exists()
    assert len(list(backups.glob("guildboard-auto-import-*.db"))) == 5
    # Only the newest of the copies made before an upgrade is kept apart.
    path2, backups2 = tmp_path / "other.db", tmp_path / "backups2"
    backups2.mkdir()
    older = backups2 / "guildboard-auto-v5-20260101-000000.db"
    older.write_bytes(b"x")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    newer = backups2 / "guildboard-auto-v9-20260201-000000.db"
    newer.write_bytes(b"x")
    os.utime(newer, (1_700_100_000, 1_700_100_000))
    other = Store.open(path2, backups2, NOW)
    for minute in range(1, 7):
        other.backup_before("auto-sync", NOW + timedelta(minutes=minute))
    other.close()
    assert newer.exists() and not older.exists()


def test_skipped_runs_older_than_ninety_days_are_pruned_and_the_points_keep_their_values(store):
    old, recent = NOW - timedelta(days=100), NOW - timedelta(days=10)
    store.add_skipped("manual", old, "A sync ran less than 5 minutes ago.")
    store.add_skipped("manual", recent, "A sync ran less than 5 minutes ago.")
    finished = store.start_run("startup", old)  # a finished run is a record of a sync: it stays however old it is
    store.finish_run(finished, old, "ok")
    skipped_id = store._conn.execute("SELECT id FROM sync_runs WHERE outcome = 'skipped' ORDER BY id").fetchone()[0]
    store._conn.execute(
        "INSERT INTO points (key, taken_at, game_day, value, sync_run_id) VALUES ('k', ?, '2026-07-05', 1, ?)",
        (iso(old), skipped_id),
    )
    store.prune_skipped_runs(NOW)
    assert [(r.outcome, r.started_at) for r in store.recent_runs()] == [("ok", old), ("skipped", recent)]
    assert store.latest_point_value("k") == 1.0


def test_writes_are_synced_at_the_normal_level_in_the_log(tmp_path):
    # Safe with a write-ahead log (a power cut can lose the last commit, never corrupt the file), and a commit no longer
    # waits for a flush of the disk.
    on_disk = Store.open(tmp_path / "guildboard.db", tmp_path / "backups", NOW)
    assert on_disk._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    on_disk.close()


def test_tuskfizzy_legacy_import_keeps_profiles_and_daily_history_idempotently(store):
    accounts = [
        {"id": "profile-a", "name": "Main", "portrait": "", "genshinId": "genshin-a", "createdAt": ""},
        {"id": "profile-b", "name": "Alt", "portrait": "", "genshinId": None, "createdAt": ""},
    ]
    genshin = [{"id": "genshin-a", "name": "Asia", "uid": "1234567890", "server": "asia", "createdAt": ""}]
    daily = {
        "2026-11-08": {
            "profile-a": [True, False, True],
            "profile-b": [False, True, False],
            "_resinLast": {"profile-a": 55},
        }
    }
    expected = {"accounts": 2, "genshinAccounts": 1, "dailyRecords": 2}
    assert store.import_tuskfizzy_data(accounts, daily, genshin, "genshin-a") == expected
    assert store.import_tuskfizzy_data(accounts, daily, genshin, "genshin-a") == expected
    rows = store._conn.execute(
        "SELECT day, account_id, login, commission, resin, resin_last FROM tuskfizzy_daily ORDER BY account_id"
    ).fetchall()
    assert rows == [
        ("2026-11-08", "profile-a", 1, 0, 1, 55),
        ("2026-11-08", "profile-b", 0, 1, 0, None),
    ]
    assert store.meta("tuskfizzy_selected_genshin_id") == "genshin-a"


def test_tuskfizzy_daily_note_uses_same_day_resin_transition_and_claimed_commission(store):
    accounts = [{"id": "profile-a", "name": "Main", "portrait": "", "genshinId": None, "createdAt": ""}]
    store.import_tuskfizzy_data(accounts, {}, [], "")

    # Starting the day below 50 must not auto-complete Resin.
    assert store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 40, False)
    assert store.tuskfizzy_daily_for("2026-11-09")["profile-a"] == [False, False, False]

    # A later observed crossing from 50+ to below 50 completes it.
    store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 55, False)
    store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 49, False)
    assert store.tuskfizzy_daily_for("2026-11-09")["profile-a"] == [False, False, True]

    # Resin remains checked after refill; Commission only checks after the bonus is claimed.
    store.tuskfizzy_observe_daily_note("2026-11-09", "profile-a", 120, True)
    assert store.tuskfizzy_daily_for("2026-11-09")["profile-a"] == [False, True, True]
