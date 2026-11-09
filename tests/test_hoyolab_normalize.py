import copy
from datetime import date, datetime, timezone

import pytest

import endgame_samples
import hoyolab_samples as samples
from guildboard.records import ArchonChapter, Area, CalcTarget, DiarySource, Expedition, GameAccount, Offering
from guildboard.sources import hoyolab_normalize as norm
from guildboard.store import iso

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


def test_overview_counters():
    record = norm.normalize_overview(samples.INDEX)
    assert (record.nickname, record.server, record.adventure_rank, record.abyss_progress) == (
        "Traveler", "os_euro", 60, "12-3",
    )
    assert record.counters["achievements"] == 1318.0
    assert record.counters["days_active"] == 1842.0
    assert record.counters["characters"] == 96.0
    assert record.counters["waypoints"] == 512.0
    assert record.counters["domains"] == 71.0
    assert record.counters["friendship10"] == 58.0
    assert record.counters["chests.remarkable"] == 152.0
    assert sum(value for name, value in record.counters.items() if name.startswith("chests.")) == 4962.0


def test_new_oculus_types_are_found_by_name():
    record = norm.normalize_overview(samples.INDEX)
    assert record.counters["oculi.cryo"] == 42.0
    assert record.counters["oculi.luno"] == 172.0
    assert record.counters["oculi.anemo"] == 66.0
    assert norm.oculus_type("electroculus_number") == "electro"
    assert norm.oculus_type("oculus_number") is None
    assert norm.oculus_type("achievement_number") is None


def test_unknown_numeric_fields_are_kept():
    record = norm.normalize_overview(samples.INDEX)
    assert record.counters["raw.brand_new_counter_number"] == 7.0
    assert not any("spiral_abyss" in name for name in record.counters)  # text is not a counter
    assert not any("some_flag" in name for name in record.counters)  # neither is a true/false value
    assert not any("role_combat" in name or "hard_challenge" in name for name in record.counters)


def test_overview_regions():
    sumeru, mondstadt, natlan = norm.normalize_overview(samples.INDEX).regions
    assert (sumeru.id, sumeru.parent_id, sumeru.name, sumeru.pct) == (15, 0, "Sumeru", 93.4)
    assert sumeru.icon_url.endswith("/sumeru.png")
    assert sumeru.offerings == (Offering("Tree of Dreams", 50, "https://act-webstatic.hoyoverse.com/x/tree.png"),)
    assert sumeru.areas == (Area("Desert of Hadramaveth", 96.0), Area("Rainforest", 100.0))  # 1005 is capped
    assert mondstadt.pct == 100.0
    assert mondstadt.offerings == (Offering("Reputation", 8),)
    assert natlan.offerings == (Offering("Children of Echoes", 4),)


def test_overview_without_stats_is_rejected():
    broken = copy.deepcopy(samples.INDEX)
    del broken["stats"]
    with pytest.raises(ValueError):
        norm.normalize_overview(broken)
    with pytest.raises(ValueError):
        norm.normalize_overview({"stats": "not an object"})


def test_overview_tolerates_missing_optional_parts():
    record = norm.normalize_overview({"stats": {"achievement_number": 5}})
    assert (record.nickname, record.server, record.adventure_rank, record.regions) == ("", "", 0, ())
    assert record.counters == {"achievements": 5.0}
    odd = norm.normalize_overview({"stats": {}, "world_explorations": ["text", {"id": 3, "name": "Liyue"}]})
    assert [(region.id, region.name, region.pct) for region in odd.regions] == [(3, "Liyue", 0.0)]


def test_notes():
    record = norm.normalize_notes(samples.DAILY_NOTE, NOW)
    assert (record.resin, record.resin_max) == (142, 200)
    assert record.resin_full_at == "2026-10-13T15:44:00+00:00"  # 27,840 seconds later
    assert (record.realm_currency, record.realm_currency_max) == (1800, 2400)
    assert (record.commissions_done, record.commissions_max) == (4, 4)
    assert record.extra_reward_claimed is True
    assert (record.boss_discounts_left, record.boss_discounts_max) == (1, 3)
    assert (record.expeditions_done, record.expeditions_total) == (1, 2)
    assert record.archon_open == (
        ArchonChapter(7102, "ongoing", "Chapter VII: Act II", "Sample"),
        ArchonChapter(7103, "not_open", "Chapter VII: Act III", "Sample"),
    )
    assert record.archon_all_finished is False


def test_notes_with_everything_finished():
    raw = copy.deepcopy(samples.DAILY_NOTE)
    raw["archon_quest_progress"] = {"list": [], "is_finish_all_mainline": True}
    raw["current_resin"] = 200
    raw["resin_recovery_time"] = "0"
    record = norm.normalize_notes(raw, NOW)
    assert record.archon_open == ()
    assert record.archon_all_finished is True
    assert record.resin_full_at is None


def test_notes_without_resin_are_rejected():
    with pytest.raises(ValueError):
        norm.normalize_notes({"max_resin": 200}, NOW)


def test_the_notes_keep_each_expedition_the_transformer_and_when_realm_currency_is_full():
    record = norm.normalize_notes(samples.DAILY_NOTE_FULL, NOW)
    assert record.expeditions == (
        Expedition(samples.AMBER_ICON, None), Expedition(samples.NOELLE_ICON, "2026-10-13T10:00:00+00:00"),
    )
    assert (record.expeditions_max, record.expeditions_done, record.expeditions_total) == (5, 1, 2)
    assert (record.transformer_obtained, record.transformer_ready_at) == (True, "2026-10-16T12:00:00+00:00")
    assert record.realm_currency_full_at == "2026-10-13T18:00:00+00:00"


def test_a_transformer_that_reached_its_time_is_ready_when_read():
    reached = {"obtained": True, "recovery_time": {"Day": 0, "Hour": 0, "Minute": 0, "Second": 0, "reached": True}}
    record = norm.normalize_notes({**samples.DAILY_NOTE_FULL, "transformer": reached}, NOW)
    assert record.transformer_ready_at == "2026-10-13T08:00:00+00:00"


def test_notes_without_a_transformer_or_a_realm_currency_time_keep_the_defaults():
    record = norm.normalize_notes({**samples.DAILY_NOTE, "home_coin_recovery_time": "0"}, NOW)
    assert (record.transformer_obtained, record.transformer_ready_at, record.realm_currency_full_at) == (
        False, None, None,
    )


def test_characters():
    record = norm.normalize_characters(samples.CHARACTER_LIST)
    assert (record.owned, record.level90) == (3, 2)


def test_characters_without_a_list_are_rejected():
    with pytest.raises(ValueError):
        norm.normalize_characters({"avatars": []})


def test_endgame():
    abyss, theater, stygian = norm.normalize_endgame(
        samples.SPIRAL_ABYSS, samples.ROLE_COMBAT, samples.HARD_CHALLENGE
    ).modes
    assert (abyss.mode, abyss.label, abyss.has_data, abyss.result_text, abyss.score) == (
        "abyss", "Spiral Abyss", True, "36 of 36 stars", 36.0,
    )
    assert (abyss.period_start, abyss.period_end) == ("2026-09-16T03:00:00+00:00", "2026-10-16T02:59:59+00:00")
    assert (theater.mode, theater.has_data, theater.result_text, theater.score) == (
        "theater", True, "Act 10 reached", 10.0,
    )
    assert (theater.period_start, theater.period_end) == ("2026-10-01T03:00:00+00:00", "2026-11-01T02:59:59+00:00")
    assert (stygian.mode, stygian.has_data, stygian.result_text, stygian.score, stygian.period_label) == (
        "stygian", True, "Difficulty 5 cleared", 5.0, "Sample Season",
    )


def test_endgame_without_attempts():
    modes = norm.normalize_endgame({"is_unlock": False}, {"is_unlock": True, "data": []}, []).modes
    assert [mode.has_data for mode in modes] == [False, False, False]
    assert [mode.score for mode in modes] == [None, None, None]
    assert [mode.result_text for mode in modes] == [
        "No attempt this period", "No attempt this season", "No attempt this season",
    ]
    untouched = dict(samples.SPIRAL_ABYSS, total_battle_times=0, total_star=0)
    assert norm.normalize_endgame(untouched, None, None).modes[0].has_data is False


def test_diary():
    record = norm.normalize_diary([samples.MONTH_INFO_OCT, samples.MONTH_INFO_SEP], date(2026, 10, 13))
    assert [(m.year, m.month, m.primogems) for m in record.months] == [(2026, 9, 7340), (2026, 10, 3180)]
    assert record.months[1].sources == (
        DiarySource("Events", 1240), DiarySource("Quests", 1160), DiarySource("Daily Activity", 780),
    )


def test_diary_year_across_january():
    months = [{"data_month": number, "month_data": {"current_primogems": 1}} for number in (1, 12, 11)]
    record = norm.normalize_diary(months, date(2027, 1, 5))
    assert [(m.year, m.month) for m in record.months] == [(2026, 11), (2026, 12), (2027, 1)]


def test_malformed_diary_month_is_rejected():
    with pytest.raises(ValueError):
        norm.normalize_diary([{"data_month": 13, "month_data": {}}], date(2026, 10, 13))
    with pytest.raises(ValueError):
        norm.normalize_diary([{"data_month": 10}], date(2026, 10, 13))


def test_calendar_counts():
    record = norm.normalize_calendar(samples.ACT_CALENDAR)
    assert (record.banners, record.events) == (3, 4)
    assert norm.normalize_calendar({}) == norm.normalize_calendar({"act_list": None})


def test_accounts_keep_only_genshin():
    expected = [
        GameAccount(700000001, "Traveler", "os_euro", 60),
        GameAccount(800000002, "Lumine", "os_asia", 45),
    ]
    assert norm.normalize_accounts(samples.ACCOUNTS) == expected
    assert norm.normalize_accounts(samples.ACCOUNTS["list"]) == expected
    assert norm.normalize_accounts(None) == []


def test_numbers_that_are_not_finite_are_ignored():
    record = norm.normalize_overview(
        {
            "stats": {"achievement_number": float("inf"), "way_point_number": float("nan"), "domain_number": 71},
            "role": {"level": float("inf")},
            "world_explorations": [{"id": 3, "name": "Liyue", "exploration_percentage": float("inf")}],
        }
    )
    assert record.counters == {"domains": 71.0}
    assert record.adventure_rank == 0
    assert record.regions[0].pct == 0.0
    assert norm.normalize_notes({"current_resin": "1e999", "resin_recovery_time": "inf"}, NOW).resin == 0


def test_half_an_emoji_in_a_name_becomes_a_replacement_mark():
    raw = copy.deepcopy(samples.INDEX)
    raw["role"]["nickname"] = "Trav\ud83d"  # a lone surrogate, which is what half of an emoji looks like
    raw["world_explorations"][0]["name"] = "Sum\ude00eru"
    raw["world_explorations"][0]["area_exploration_list"][0]["name"] = "Desert\ud83d"
    record = norm.normalize_overview(raw)
    assert record.nickname == "Trav�"
    assert record.regions[0].name == "Sum�eru"
    assert record.regions[0].areas[0].name == "Desert�"
    record.nickname.encode("utf-8")  # valid text can be stored and sent; a lone surrogate cannot
    notes = copy.deepcopy(samples.DAILY_NOTE)
    notes["archon_quest_progress"]["list"][0]["chapter_title"] = "Act\ud83d"
    assert norm.normalize_notes(notes, NOW).archon_open[0].title == "Act�"
    accounts = copy.deepcopy(samples.ACCOUNTS)
    accounts["list"][0]["nickname"] = "Trav\ud83d"
    assert norm.normalize_accounts(accounts)[0].nickname == "Trav�"


def test_a_region_listed_only_through_its_parts_takes_their_average():
    # HoYoLAB lists Chenyu Vale at 0, with its three parts carrying the real percentages; the Chasm lists its own.
    raw = copy.deepcopy(samples.INDEX)
    raw["world_explorations"] = [
        {"id": 13, "parent_id": 10, "name": "Chenyu Vale: Upper Vale", "exploration_percentage": 1000},
        {"id": 12, "parent_id": 10, "name": "Chenyu Vale: Southern Mountain", "exploration_percentage": 1000},
        {"id": 11, "parent_id": 10, "name": "Mt. Laixin", "exploration_percentage": 980},
        {"id": 7, "parent_id": 6, "name": "The Chasm: Underground Mines", "exploration_percentage": 1000},
        {"id": 6, "parent_id": 0, "name": "The Chasm", "exploration_percentage": 950},
        {"id": 10, "parent_id": 0, "name": "Chenyu Vale", "exploration_percentage": 0},
        {"id": 21, "parent_id": 0, "name": "A region just opened", "exploration_percentage": 0},
    ]
    pct = {region.name: region.pct for region in norm.normalize_overview(raw).regions}
    assert pct["Chenyu Vale"] == 99.3  # (100 + 100 + 98) / 3
    assert pct["The Chasm"] == 95.0  # a region with its own percentage keeps it
    assert pct["A region just opened"] == 0.0  # no parts: nothing to average


# ---------------------------------------------------------------- the Characters plan: builds


def test_a_detail_answer_becomes_builds_with_rolls_and_base_and_added_values():
    record = norm.normalize_builds(samples.CHARACTER_DETAIL)
    sandrone = next(build for build in record.builds if build.name == "Sandrone")
    assert (sandrone.id, sandrone.element, sandrone.rarity, sandrone.level, sandrone.constellation) == (
        samples.SANDRONE_ID, "Cryo", 5, 90, 2,
    )
    assert sandrone.ascension == 6  # HoYoLAB gives none: the level says it, and level 90 is the sixth
    assert sandrone.wiki_entry == "10920"  # from avatar_wiki
    assert sandrone.icon == "https://act-webstatic.hoyoverse.com/sandrone-icon.png"
    assert sandrone.side_icon == "https://act-webstatic.hoyoverse.com/sandrone-side.png"
    assert sandrone.image == "https://act-webstatic.hoyoverse.com/sandrone-card.png"
    weapon = sandrone.weapon
    assert (weapon.name, weapon.refinement, weapon.base_atk, weapon.level, weapon.ascension) == (
        "A Teaspoon of Transcendence", 1, "674", 90, 6,
    )
    assert (weapon.sub_prop, weapon.sub_value) == (22, "44.1%")
    flower = next(piece for piece in sandrone.pieces if piece.pos == 1)
    assert (flower.set_name, flower.level, flower.main_prop, flower.main_value) == (
        "Disenchantment in Deep Shadow", 20, 2, "4780",
    )
    assert flower.subs[0].prop == 22 and flower.subs[0].value == "21.8%" and flower.subs[0].times == 2
    assert record.prop_names[22] == "CRIT DMG"  # the no-break space in HoYoLAB's name is a plain space
    assert [skill.level for skill in sandrone.skills] == [9, 10, 10]
    by_prop = {prop.prop: prop for prop in sandrone.props}
    assert (by_prop[2001].base, by_prop[2001].add, by_prop[2001].final) == ("1016", "1948.1", "2964.1")
    assert 46 in by_prop  # the element's bonus comes with the rest
    assert len(by_prop) == len(sandrone.props)  # a property the answer lists twice is kept once


def test_a_character_the_wiki_has_no_page_for_has_no_wiki_entry_and_a_bare_one_is_read():
    record = norm.normalize_builds(samples.CHARACTER_DETAIL)
    ayaka = next(build for build in record.builds if build.id == samples.AYAKA_ID)
    assert ayaka.wiki_entry is None and ayaka.pieces == ()
    assert (ayaka.level, ayaka.ascension) == (80, 5)  # the lowest ascension that level allows
    assert ayaka.weapon.sub_prop is None and ayaka.weapon.sub_value is None


def test_a_detail_answer_without_its_list_is_unreadable():
    broken = {key: value for key, value in samples.CHARACTER_DETAIL.items() if key != "list"}
    with pytest.raises(ValueError):
        norm.normalize_builds(broken)
    assert norm.normalize_builds({"list": []}).builds == ()


def test_a_character_the_answer_cannot_name_is_left_out():
    answer = copy.deepcopy(samples.CHARACTER_DETAIL)
    del answer["list"][1]["base"]["name"]
    assert [build.id for build in norm.normalize_builds(answer).builds] == [samples.SANDRONE_ID]


def test_ascension_is_the_lowest_one_the_level_allows():
    levels = {1: 0, 20: 0, 21: 1, 40: 1, 41: 2, 50: 2, 51: 3, 60: 3, 61: 4, 70: 4, 71: 5, 80: 5, 81: 6, 90: 6}
    assert {level: norm.ascension_of(level) for level in levels} == levels


# ---------------------------------------------------------------- the Characters plan: the calculator


def test_a_calculator_answer_gives_each_characters_materials_with_what_is_missing():
    target = CalcTarget(samples.SANDRONE_ID, (90, 90), (14520, 90, 90), ((10, 9, 10),))
    record = norm.normalize_calculator(samples.BATCH_COMPUTE, (target,))
    assert record.has_user_info is True
    mora, books = record.items[samples.SANDRONE_ID]
    assert (mora.id, mora.name, mora.amount, mora.lacking) == (202, "Mora", 1700000, 200000)
    assert (books.name, books.amount, books.lacking) == ("Philosophies of Vagrancy", 16, 0)
    assert record.targets == {samples.SANDRONE_ID: {"level": 90, "weapon": 90, "talent": 10}}


def test_a_calculator_answer_without_user_info_says_sync_is_off():
    target = CalcTarget(samples.SANDRONE_ID, (90, 90), None, ())
    record = norm.normalize_calculator(samples.BATCH_COMPUTE_SYNC_OFF, (target,))
    assert record.has_user_info is False
    mora, books = record.items[samples.SANDRONE_ID]
    assert (mora.amount, mora.lacking) == (1700000, 0)  # the amounts are kept; nothing is said to be missing
    assert record.targets == {samples.SANDRONE_ID: {"level": 90}}


def test_without_a_per_character_total_the_materials_are_added_up_from_the_items():
    answer = copy.deepcopy(samples.BATCH_COMPUTE)
    del answer["single_role_result"]
    target = CalcTarget(samples.SANDRONE_ID, (90, 90), None, ())
    mora = {item.name: item for item in norm.normalize_calculator(answer, (target,)).items[samples.SANDRONE_ID]}["Mora"]
    assert (mora.amount, mora.lacking) == (1700000, 200000)  # the character's 1,000,000 and the talent's 700,000


def test_a_calculator_answer_without_items_or_a_sync_flag_is_unreadable():
    with pytest.raises(ValueError):
        norm.normalize_calculator({"unrelated": 1}, ())


# ---------------------------------------------------------------- the Characters plan: endgame and calendar details


def test_the_abyss_answer_gives_each_half_with_its_stars_characters_and_enemies():
    detail = norm.normalize_endgame_detail(samples.SPIRAL_ABYSS_FLOORS, samples.ROLE_COMBAT, samples.HARD_CHALLENGE)
    assert detail.abyss_stars == 36 and detail.abyss_ends == "2026-10-16T02:59:59+00:00"
    halves = {(half.chamber, half.half): half for half in detail.abyss_halves}
    assert set(halves) == {("12-2", 1), ("12-3", 1), ("12-3", 2)}  # a chamber never entered has none
    second = halves[("12-3", 2)]
    assert second.stars == 2
    assert second.characters == (10000021, 10000114)
    assert second.enemies == ("Primordial Construct", "Hydro Cicin Mage")  # the bottom half's
    assert halves[("12-3", 1)].enemies == ("Ruin Serpent",)  # the top half's
    assert halves[("12-3", 1)].characters == (10000002, 10000089)


def test_the_theater_answer_gives_its_difficulty_acts_and_end():
    detail = norm.normalize_endgame_detail(samples.SPIRAL_ABYSS, samples.ROLE_COMBAT, samples.HARD_CHALLENGE)
    assert (detail.theater_difficulty, detail.theater_acts) == ("Visionary", 10)  # difficulty 4 of the latest season
    assert detail.theater_ends == "2026-11-01T02:59:59+00:00"


def test_the_stygian_answer_gives_each_boss_with_its_element_tags_in_words():
    detail = norm.normalize_endgame_detail(samples.SPIRAL_ABYSS, samples.ROLE_COMBAT, samples.HARD_CHALLENGE_BOSSES)
    assert detail.stygian_difficulty == 5 and detail.stygian_ends == "2026-11-01T02:59:59+00:00"
    one, two = detail.stygian_bosses
    assert one.name == "Boss One" and one.seconds == 90 and one.team == (10000002, 10000089)
    assert one.advantage == ("Cryo", "Dendro")  # {SPRITE_PRESET#11001} and {SPRITE_PRESET#11007}
    assert one.disadvantage == ("Hydro", "Pyro")
    assert (two.advantage, two.disadvantage, two.team) == ((), (), (10000114,))


def test_the_calendar_gives_the_theaters_text_and_the_banners_with_their_featured_names():
    detail = norm.normalize_calendar_detail(samples.ACT_CALENDAR_DETAIL)
    assert detail.theater_text == "Cryo, Pyro and Anemo characters are boosted.\nOpens daily."  # the newline is one
    assert detail.banners == (
        (("Sandrone",), "2026-09-21T14:13:20+00:00", "2026-10-03T04:00:00+00:00"),  # the 5-star only
        (("Raiden Shogun",), "2026-10-03T04:00:00+00:00", "2026-10-14T17:46:40+00:00"),
    )


def test_an_endgame_without_data_gives_empty_details():
    detail = norm.normalize_endgame_detail(None, {}, [])
    assert detail.abyss_stars is None and detail.abyss_halves == () and detail.abyss_ends is None
    assert (detail.theater_difficulty, detail.theater_acts, detail.theater_ends) == (None, None, None)
    assert (detail.stygian_difficulty, detail.stygian_bosses, detail.stygian_ends) == (None, (), None)
    calendar = norm.normalize_calendar_detail({})
    assert calendar.theater_text is None and calendar.banners == ()
    unplayed = norm.normalize_endgame_detail(
        {"is_unlock": True, "total_battle_times": 0, "total_star": 0, "floors": []}, {"data": []}, []
    )
    assert unplayed.abyss_stars is None and unplayed.abyss_halves == ()  # nothing was attempted this period


def test_the_calendar_has_no_theater_text_when_its_entry_has_none():
    calendar = norm.normalize_calendar_detail({"fixed_act_list": [{"name": "Imaginarium Theater", "desc": ""}]})
    assert calendar.theater_text is None
    elsewhere = norm.normalize_calendar_detail({"fixed_act_list": [{"id": 1, "name": "Spiral Abyss", "desc": "Cryo"}]})
    assert elsewhere.theater_text is None  # another entry's words are never the Theater's


def test_the_summaries_are_unchanged_by_the_details():
    assert norm.normalize_endgame(samples.SPIRAL_ABYSS_FLOORS, samples.ROLE_COMBAT, samples.HARD_CHALLENGE_BOSSES).modes[0].score == 36.0
    assert norm.normalize_calendar(samples.ACT_CALENDAR_DETAIL).banners == 3  # the pools of every kind, as before


# ---------------------------------------------------------------- the Stats design, 3.4: what the Endgame card needs

def _stamp(seconds: int) -> str:
    return iso(datetime.fromtimestamp(seconds, timezone.utc))


def _real_detail():
    return norm.normalize_endgame_detail(endgame_samples.abyss(), endgame_samples.theater(), endgame_samples.stygian())


def test_the_abyss_keeps_each_enemys_icon_and_level():
    detail = _real_detail()
    half = next(h for h in detail.abyss_halves if h.chamber == "12-1" and h.half == 2)
    assert [f.name for f in half.foes][:1] == ["Iniquitous Baptist - Caller of Flood, Flame, and Fulmination"]
    assert half.foes[0].level == 95 and half.foes[0].icon.startswith("https://act-webstatic.hoyoverse.com/")
    assert half.enemies == tuple(f.name for f in half.foes)          # the Characters pages' field is unchanged
    assert (detail.abyss_battles, detail.abyss_wins) == (6, 6)
    assert next(r for r in detail.abyss_ranks if r.kind == "strike").value == 1352944


def test_the_abyss_keeps_its_start_its_chambers_and_each_rank_with_its_face():
    detail = _real_detail()
    assert (detail.abyss_starts, detail.abyss_ends) == (_stamp(1792119600), _stamp(1794797999))
    # Floors 9 and 10 were skipped up to 10-3: the answer has no floor for them, and the total counts their stars in full.
    assert detail.abyss_chambers == tuple(
        (f"{floor}-{chamber}", 3) for floor in (9, 10, 11, 12) for chamber in (1, 2, 3)
    )
    ranks = {rank.kind: rank for rank in detail.abyss_ranks}
    assert list(ranks) == ["strike", "defeats", "taken", "bursts", "skills", "played"]
    assert (ranks["strike"].avatar_id, ranks["defeats"].value, ranks["taken"].value) == (10000133, 22, 14583)
    assert (ranks["bursts"].value, ranks["skills"].value, ranks["played"].value) == (13, 21, 6)
    assert ranks["played"].avatar_id == 10000058  # the first of the most played
    assert all(rank.icon.startswith("https://act-webstatic.hoyoverse.com/") for rank in ranks.values())


def test_the_theater_keeps_medals_flowers_and_ranks():
    detail = _real_detail()
    assert detail.theater_medals[:4] == (True, False, True, True) and detail.theater_flowers == 1285
    assert detail.theater_seconds == 1002
    assert {r.kind for r in detail.theater_ranks} == {"damage", "defeats", "taken"}


def test_the_theater_keeps_the_seasons_start_its_medal_count_and_each_face():
    detail = _real_detail()
    assert (detail.theater_starts, detail.theater_ends) == (_stamp(1793502000), _stamp(1796093999))
    assert len(detail.theater_medals) == 12 and detail.theater_medal_count == 6  # the Arcana acts' medals are not counted
    damage, defeats, taken = detail.theater_ranks
    assert (damage.kind, damage.value, damage.avatar_id) == ("damage", 943229, 10000120)  # HoYoLAB sends these as text
    assert (defeats.value, taken.value) == (11, 68942)
    assert damage.icon.startswith("https://act-webstatic.hoyoverse.com/")
    assert (detail.theater_difficulty, detail.theater_acts) == ("Arcana Challenge", 10)  # as the Characters pages read them


def test_the_stygian_keeps_icons_and_the_next_season():
    detail = _real_detail()
    assert detail.stygian_season == "Luminous Battlefront" and detail.stygian_seconds == 280
    assert detail.stygian_bosses[0].icon.endswith(".png")
    assert detail.stygian_next == ("Battle Amid the Great Waves", iso(datetime.fromtimestamp(1794387600, timezone.utc)))


def test_the_stygian_keeps_the_seasons_dates_its_medal_and_each_bosss_level():
    detail = _real_detail()
    assert (detail.stygian_starts, detail.stygian_ends) == (_stamp(1790758800), _stamp(1793674799))
    assert detail.stygian_medal == "UI_LeyLineChallenge_Medal_5" and detail.stygian_difficulty == 5
    assert [(boss.level, boss.seconds) for boss in detail.stygian_bosses] == [(105, 119), (105, 42), (105, 119)]


def test_the_stygian_answer_reads_the_same_as_the_library_gives_it_and_as_the_archive_keeps_it():
    # The library hands over the list of seasons; the raw archive keeps the whole answer, with the list under "data".
    kept = endgame_samples.stygian()
    assert norm.normalize_endgame_detail(None, {}, kept) == norm.normalize_endgame_detail(None, {}, kept["data"])


def test_only_a_season_newer_than_the_one_played_is_the_next():
    seasons = endgame_samples.stygian()["data"]
    older = copy.deepcopy(seasons[0])
    older["schedule"].update(schedule_id="5269011", name="An Older Season", start_time="1780000000", end_time="1782000000")
    assert norm.normalize_endgame_detail(None, {}, [seasons[1], older]).stygian_next is None  # skipped long ago, not next
    assert norm.normalize_endgame_detail(None, {}, [older, *seasons]).stygian_next[0] == "Battle Amid the Great Waves"
    assert norm.normalize_endgame_detail(None, {}, [seasons[0]]).stygian_next[0] == "Battle Amid the Great Waves"


def test_an_endgame_without_data_gives_the_new_fields_their_defaults():
    detail = norm.normalize_endgame_detail(None, {}, [])
    assert (detail.abyss_starts, detail.abyss_battles, detail.abyss_wins, detail.abyss_chambers, detail.abyss_ranks) == (
        None, 0, 0, (), ()
    )
    assert (detail.theater_starts, detail.theater_medals, detail.theater_medal_count) == (None, (), 0)
    assert (detail.theater_flowers, detail.theater_seconds, detail.theater_ranks) == (0, 0, ())
    assert (detail.stygian_season, detail.stygian_starts, detail.stygian_seconds, detail.stygian_medal) == ("", None, None, "")
    assert detail.stygian_next is None
    # A boss or an enemy given by its name alone keeps an empty icon and a level of 0.
    season = copy.deepcopy(samples.HARD_CHALLENGE_BOSSES[0])
    season["single"]["challenge"] = [{"name": "Boss Three"}]
    boss = norm.normalize_endgame_detail(None, {}, [season]).stygian_bosses[0]
    assert (boss.name, boss.icon, boss.level) == ("Boss Three", "", 0)
    abyss = copy.deepcopy(samples.SPIRAL_ABYSS_FLOORS)
    abyss["floors"][0]["levels"][0]["top_half_floor_monster"] = [{"name": "Thunder Manifestation"}]
    foe = norm.normalize_endgame_detail(abyss, {}, []).abyss_halves[0].foes[0]
    assert (foe.name, foe.level, foe.icon) == ("Thunder Manifestation", 0, "")
