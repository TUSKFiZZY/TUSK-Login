"""Turn raw HoYoLAB JSON into Guild Board's own records.

Pure functions. Missing optional parts become empty values. A missing essential part raises
ValueError; the caller then keeps the raw response and reports the group as unreadable.
"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping, Sequence

from guildboard.records import (
    AbyssHalfRecord,
    ArchonChapter,
    Area,
    BuildRecord,
    BuildsRecord,
    CalcItemRecord,
    CalcTarget,
    CalculatorRecord,
    CalendarDetailRecord,
    CalendarRecord,
    CharactersRecord,
    DiaryEntry,
    DiaryMonth,
    DiaryRecord,
    DiarySource,
    EndgameDetailRecord,
    EndgameMode,
    EndgameRecord,
    EnemyRecord,
    Expedition,
    GameAccount,
    NotesRecord,
    Offering,
    OverviewRecord,
    PieceRecord,
    PropRecord,
    RankRecord,
    Region,
    SkillRecord,
    StygianBossRecord,
    SubRecord,
    WeaponRecord,
)

_COUNTERS = {
    "achievement_number": "achievements",
    "active_day_number": "days_active",
    "avatar_number": "characters",
    "way_point_number": "waypoints",
    "domain_number": "domains",
    "full_fetter_avatar_num": "friendship10",
    "common_chest_number": "chests.common",
    "exquisite_chest_number": "chests.exquisite",
    "precious_chest_number": "chests.precious",
    "luxurious_chest_number": "chests.luxurious",
    "magic_chest_number": "chests.remarkable",
}
_OCULI_NAMES = {"moonoculus": "luno"}
_ARCHON_STATUS = {"StatusOngoing": "ongoing", "StatusNotOpen": "not_open"}
_OCULUS_SUFFIX = "oculus_number"


def _number(value: Any) -> float | None:
    """A finite JSON number, or text holding one. True/false and everything else give None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any, default: int = 0) -> int:
    number = _number(value)
    return int(number) if number is not None else default


def _text(value: Any) -> str:
    """Text from HoYoLAB as valid Unicode. Half of an emoji (a lone surrogate) becomes a replacement mark,
    because such text can neither be stored nor sent to the interface."""
    return str(value).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _timestamp(value: Any) -> str | None:
    """Seconds since 1970 (a number, or digits as text) as UTC ISO text."""
    number = _number(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _percent(tenths: Any) -> float:
    number = _number(tenths) or 0.0
    return round(min(100.0, max(0.0, number / 10.0)), 1)


def oculus_type(field: str) -> str | None:
    if not field.endswith(_OCULUS_SUFFIX) or len(field) == len(_OCULUS_SUFFIX):
        return None
    name = field[: -len("_number")]  # for example "cryoculus"
    return _OCULI_NAMES.get(name, name[: -len("culus")])


def _region(item: Mapping[str, Any]) -> Region:
    offerings = [
        Offering(_text(entry["name"]), _int(entry.get("level")), _text(entry.get("icon", "")))
        for entry in _items(item.get("offerings"))
        if entry.get("name")
    ]
    if item.get("type") == "Reputation" and not any(offering.name == "Reputation" for offering in offerings):
        offerings.append(Offering("Reputation", _int(item.get("level"))))
    for tribe in _items(_map(item.get("natan_reputation")).get("tribal_list")):
        if tribe.get("name"):
            offerings.append(Offering(_text(tribe["name"]), _int(tribe.get("level")), _text(tribe.get("icon", ""))))
    areas = tuple(
        Area(_text(entry["name"]), _percent(entry.get("exploration_percentage")))
        for entry in _items(item.get("area_exploration_list"))
        if entry.get("name")
    )
    return Region(
        id=_int(item.get("id")),
        parent_id=_int(item.get("parent_id")),
        name=_text(item.get("name", "")),
        pct=_percent(item.get("exploration_percentage")),
        icon_url=_text(item.get("icon", "")),
        offerings=tuple(offerings),
        areas=areas,
    )


def _with_parts(regions: tuple[Region, ...]) -> tuple[Region, ...]:
    """HoYoLAB lists some regions (Chenyu Vale) at 0, with the real percentages on their parts. Such a region
    takes the average of its parts. A region that reports a percentage of its own (the Chasm) keeps it."""
    parts: dict[int, list[float]] = {}
    for region in regions:
        if region.parent_id:
            parts.setdefault(region.parent_id, []).append(region.pct)
    return tuple(
        replace(region, pct=round(sum(parts[region.id]) / len(parts[region.id]), 1))
        if region.pct == 0.0 and parts.get(region.id)
        else region
        for region in regions
    )


def normalize_overview(raw: Mapping[str, Any]) -> OverviewRecord:
    stats = raw.get("stats")
    if not isinstance(stats, Mapping):
        raise ValueError("the overview has no stats")
    counters: dict[str, float] = {}
    for field, value in stats.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # nested objects, text such as "12-3", and true/false values are not counters
        number = _number(value)
        if number is None:
            continue  # neither is a number that is not finite
        if field in _COUNTERS:
            counters[_COUNTERS[field]] = number
        elif (kind := oculus_type(field)) is not None:
            counters[f"oculi.{kind}"] = number
        else:
            counters[f"raw.{field}"] = number
    role = _map(raw.get("role"))
    abyss = stats.get("spiral_abyss")
    return OverviewRecord(
        nickname=_text(role.get("nickname", "")),
        server=_text(role.get("region", "")),
        adventure_rank=_int(role.get("level")),
        counters=counters,
        regions=_with_parts(tuple(_region(item) for item in _items(raw.get("world_explorations")))),
        abyss_progress=_text(abyss) if isinstance(abyss, str) else "",
        picture_url=_text(role.get("game_head_icon", "")),
    )


def normalize_notes(raw: Mapping[str, Any], now: datetime) -> NotesRecord:
    if "current_resin" not in raw:
        raise ValueError("the notes have no resin")
    resin_seconds = _number(raw.get("resin_recovery_time")) or 0.0
    expeditions = _items(raw.get("expeditions"))
    progress = _map(raw.get("archon_quest_progress"))
    chapters = tuple(
        ArchonChapter(
            _int(chapter.get("id")),
            _ARCHON_STATUS.get(str(chapter.get("status")), "ongoing"),
            _text(chapter.get("chapter_num", "")),
            _text(chapter.get("chapter_title", "")),
        )
        for chapter in _items(progress.get("list"))
    )
    full_at = (now + timedelta(seconds=resin_seconds)).isoformat(timespec="seconds") if resin_seconds > 0 else None
    home_coin_seconds = _number(raw.get("home_coin_recovery_time")) or 0.0
    transformer = _map(raw.get("transformer"))
    return NotesRecord(
        resin=_int(raw.get("current_resin")),
        resin_max=_int(raw.get("max_resin"), 200),
        resin_full_at=full_at,
        realm_currency=_int(raw.get("current_home_coin")),
        realm_currency_max=_int(raw.get("max_home_coin")),
        commissions_done=_int(raw.get("finished_task_num")),
        commissions_max=_int(raw.get("total_task_num")),
        boss_discounts_left=_int(raw.get("remain_resin_discount_num")),
        boss_discounts_max=_int(raw.get("resin_discount_num_limit")),
        expeditions_done=sum(1 for expedition in expeditions if expedition.get("status") == "Finished"),
        expeditions_total=len(expeditions),
        archon_open=chapters,
        archon_all_finished=bool(progress.get("is_finish_all_mainline")) and not chapters,
        expeditions=tuple(_expedition(expedition, now) for expedition in expeditions),
        expeditions_max=_int(raw.get("max_expedition_num")),
        transformer_obtained=bool(transformer.get("obtained")),
        transformer_ready_at=_transformer_ready_at(transformer, now),
        realm_currency_full_at=_later(now, home_coin_seconds),
        extra_reward_claimed=raw.get("is_extra_task_reward_received") is True,
    )


def _later(now: datetime, seconds: float) -> str | None:
    """The time `seconds` after `now`, as the notes keep a time; None when no time is left."""
    return (now + timedelta(seconds=seconds)).isoformat(timespec="seconds") if seconds > 0 else None


def _expedition(raw: Mapping[str, Any], now: datetime) -> Expedition:
    """An expedition's picture, and when it is back: never once it is finished."""
    left = 0.0 if raw.get("status") == "Finished" else _number(raw.get("remained_time")) or 0.0
    return Expedition(_text(raw.get("avatar_side_icon", "")), _later(now, left))


def _transformer_ready_at(transformer: Mapping[str, Any], now: datetime) -> str | None:
    """When the Parametric Transformer can be used again: the time read once it can be, None until it is obtained."""
    if not transformer.get("obtained"):
        return None
    wait = _map(transformer.get("recovery_time"))
    if wait.get("reached"):
        return now.isoformat(timespec="seconds")
    units = (("Day", 86_400), ("Hour", 3_600), ("Minute", 60), ("Second", 1))
    seconds = sum((_number(wait.get(unit)) or 0.0) * size for unit, size in units)
    return _later(now, seconds) or now.isoformat(timespec="seconds")


def normalize_characters(raw: Mapping[str, Any]) -> CharactersRecord:
    if not isinstance(raw.get("list"), (list, tuple)):
        raise ValueError("the character list is missing")
    characters = _items(raw["list"])
    return CharactersRecord(
        owned=len(characters),
        level90=sum(1 for character in characters if _int(character.get("level")) >= 90),
    )


# The levels a character cannot pass without ascending, and the ascension each range of levels follows.
_ASCENSION_LEVELS = (20, 40, 50, 60, 70, 80)


def ascension_of(level: int) -> int:
    """The lowest ascension a character of this level can have. HoYoLAB gives none for a character; a level that
    stands on a limit (20, 40, ... 80) may be ascended or not, and the lower is taken."""
    return sum(1 for limit in _ASCENSION_LEVELS if level > limit)


def _optional_int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _wiki_entry(url: Any) -> str | None:
    """The wiki's entry id at the end of the link HoYoLAB gives for a character: ".../entry/10920" is "10920"."""
    tail = str(url or "").rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else None


def _plain(name: Any) -> str:
    """A name as HoYoLAB writes it, with its no-break spaces as plain ones."""
    return " ".join(_text(name).replace(" ", " ").split())


def _weapon(raw: Mapping[str, Any]) -> WeaponRecord:
    main = _map(raw.get("main_property"))
    sub = raw.get("sub_property")
    sub = sub if isinstance(sub, Mapping) else None
    return WeaponRecord(
        id=_int(raw.get("id")),
        name=_text(raw.get("name", "")),
        icon=_text(raw.get("icon", "")),
        type=_int(raw.get("type")),
        rarity=_int(raw.get("rarity")),
        level=_int(raw.get("level")),
        refinement=_int(raw.get("affix_level"), 1),
        ascension=_int(raw.get("promote_level")),
        base_atk=_text(main.get("final") or main.get("base") or ""),
        sub_prop=_optional_int(sub.get("property_type")) if sub else None,
        sub_value=(_text(sub.get("final") or sub.get("base") or "") if sub else None),
    )


def _piece(raw: Mapping[str, Any]) -> PieceRecord:
    owner = _map(raw.get("set"))
    main = _map(raw.get("main_property"))
    return PieceRecord(
        pos=_int(raw.get("pos")),
        name=_text(raw.get("name", "")),
        icon=_text(raw.get("icon", "")),
        set_id=_int(owner.get("id")),
        set_name=_text(owner.get("name", "")),
        rarity=_int(raw.get("rarity")),
        level=_int(raw.get("level")),
        main_prop=_int(main.get("property_type")),
        main_value=_text(main.get("value", "")),
        subs=tuple(
            SubRecord(_int(sub.get("property_type")), _text(sub.get("value", "")), _int(sub.get("times")))
            for sub in _items(raw.get("sub_property_list"))
        ),
    )


def _properties(raw: Mapping[str, Any]) -> tuple[PropRecord, ...]:
    """The base, extra, element and selected properties as one list, each property once (the first that names it)."""
    seen: dict[int, PropRecord] = {}
    for name in ("base_properties", "extra_properties", "element_properties", "selected_properties"):
        for item in _items(raw.get(name)):
            prop = _optional_int(item.get("property_type"))
            if prop is not None and prop not in seen:
                seen[prop] = PropRecord(
                    prop, _text(item.get("base", "")), _text(item.get("add", "")), _text(item.get("final", ""))
                )
    return tuple(seen.values())


def _build(raw: Mapping[str, Any], wiki: Mapping[str, Any]) -> BuildRecord | None:
    base = _map(raw.get("base"))
    ident = _optional_int(base.get("id"))
    name = _text(base.get("name", "")).strip()
    if ident is None or not name or not isinstance(raw.get("weapon"), Mapping):
        return None
    level = _int(base.get("level"))
    return BuildRecord(
        id=ident,
        name=name,
        element=_text(base.get("element", "")),
        rarity=_int(base.get("rarity")),
        level=level,
        ascension=ascension_of(level),
        constellation=_int(base.get("actived_constellation_num")),
        icon=_text(base.get("icon", "")),
        side_icon=_text(base.get("side_icon", "")),
        image=_text(base.get("image", "")),
        wiki_entry=_wiki_entry(wiki.get(str(ident))),
        weapon=_weapon(_map(raw.get("weapon"))),
        pieces=tuple(_piece(piece) for piece in _items(raw.get("relics"))),
        skills=tuple(
            SkillRecord(
                _int(skill.get("skill_id")), _int(skill.get("skill_type")), _text(skill.get("name", "")),
                _text(skill.get("icon", "")), _int(skill.get("level")),
            )
            for skill in _items(raw.get("skills"))
        ),
        props=_properties(raw),
    )


def character_id(item: Mapping[str, Any]) -> int | None:
    """The id of a character of the list."""
    return _optional_int(item.get("id"))


def merge_details(answers: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The answers of several detail requests as one: their characters one after another, their maps together. With
    none (no owned character) it is an answer with an empty list."""
    merged: dict[str, Any] = {"list": []}
    for answer in answers:
        if not isinstance(answer, Mapping) or not isinstance(answer.get("list"), (list, tuple)):
            raise ValueError("the detailed characters are missing")
        merged["list"].extend(answer["list"])
        for key, value in answer.items():
            if isinstance(value, Mapping):
                merged.setdefault(key, {}).update(value)
    return merged


def normalize_builds(raw: Mapping[str, Any]) -> BuildsRecord:
    """Every character of a `character/detail` answer as a build: the weapon, the pieces with their rolls, the skills and
    the properties as base, added and final values. A character the answer cannot name is left out; an answer with no
    list at all raises ValueError."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("list"), (list, tuple)):
        raise ValueError("the detailed characters are missing")
    wiki = _map(raw.get("avatar_wiki"))
    builds = tuple(build for build in (_build(item, wiki) for item in _items(raw["list"])) if build is not None)
    if raw["list"] and not builds:
        raise ValueError("no detailed character could be read")
    names: dict[int, str] = {}
    for key, info in _map(raw.get("property_map")).items():
        prop = _optional_int(key)
        if prop is not None and isinstance(info, Mapping):
            names[prop] = _plain(info.get("name", ""))
    return BuildsRecord(builds, names)


def _material(raw: Mapping[str, Any]) -> CalcItemRecord | None:
    ident = _optional_int(raw.get("id"))
    if ident is None:
        return None
    return CalcItemRecord(
        ident, _text(raw.get("name", "")), _text(raw.get("icon", "")), _int(raw.get("num")), _int(raw.get("lack_num"))
    )


def _item_materials(item: Mapping[str, Any]) -> list[CalcItemRecord]:
    """Every material of one character's entry in the calculator's answer: the character's, the weapon's and each
    talent's."""
    lists = [_items(item.get("avatar_consume")), _items(item.get("weapon_consume"))]
    lists += [_items(talent.get("consume_list")) for talent in _items(item.get("skills_consume"))]
    found = (_material(entry) for entries in lists for entry in entries)
    return [material for material in found if material is not None]


def _added_up(materials: Sequence[CalcItemRecord]) -> tuple[CalcItemRecord, ...]:
    """One entry for each material: the amounts added, and the most that any entry says is missing (an entry's missing
    count is of the material as a whole)."""
    total: dict[int, CalcItemRecord] = {}
    for material in materials:
        seen = total.get(material.id)
        total[material.id] = (
            material if seen is None
            else replace(seen, amount=seen.amount + material.amount, lacking=max(seen.lacking, material.lacking))
        )
    return tuple(total.values())


def normalize_calculator(raw: Mapping[str, Any], targets: Sequence[CalcTarget]) -> CalculatorRecord:
    """What the batch calculator says about the characters it was asked for (in the order it was asked): each one's
    materials with their amounts and, when HoYoLAB's calculator sync is on, how many are missing. `has_user_info` False
    means sync is off: the amounts are kept and nothing is said to be missing. An answer with neither its items nor its
    sync flag raises ValueError."""
    if not isinstance(raw, Mapping) or ("items" not in raw and "has_user_info" not in raw):
        raise ValueError("the calculator's answer has no items")
    entries = _items(raw.get("items"))
    singles = _items(raw.get("single_role_result"))
    items: dict[int, tuple[CalcItemRecord, ...]] = {}
    for index, target in enumerate(targets):
        own = singles[index] if index < len(singles) else {}
        totals = [_material(entry) for entry in _items(own.get("overall_consume"))]
        materials = [material for material in totals if material is not None]
        if not materials and index < len(entries):
            materials = _item_materials(entries[index])
        items[target.character_id] = _added_up(materials)
    asked: dict[int, dict[str, int]] = {}
    for target in targets:
        goals = {"level": target.level[1]}
        if target.weapon is not None:
            goals["weapon"] = target.weapon[2]
        if target.talents:
            goals["talent"] = max(talent[2] for talent in target.talents)
        asked[target.character_id] = goals
    return CalculatorRecord(has_user_info=bool(raw.get("has_user_info")), items=items, targets=asked)


def _abyss(raw: Mapping[str, Any]) -> EndgameMode:
    stars = _int(raw.get("total_star"))
    has_data = bool(raw.get("is_unlock")) and _int(raw.get("total_battle_times")) > 0
    return EndgameMode(
        mode="abyss",
        label="Spiral Abyss",
        has_data=has_data,
        result_text=f"{stars} of 36 stars" if has_data else "No attempt this period",
        score=float(stars) if has_data else None,
        period_start=_timestamp(raw.get("start_time")),
        period_end=_timestamp(raw.get("end_time")),
        period_label="",
    )


def _season_start(season: Mapping[str, Any]) -> float:
    return _number(_map(season.get("schedule")).get("start_time")) or 0.0


def _theater(raw: Mapping[str, Any]) -> EndgameMode:
    latest = _latest_theater(raw)
    if latest is None:
        return EndgameMode(
            "theater", "Imaginarium Theater", False, "No attempt this season", None, None, None, ""
        )
    act = _int(_map(latest.get("stat")).get("max_round_id"))
    schedule = _map(latest.get("schedule"))
    return EndgameMode(
        mode="theater",
        label="Imaginarium Theater",
        has_data=True,
        result_text=f"Act {act} reached",
        score=float(act),
        period_start=_timestamp(schedule.get("start_time")),
        period_end=_timestamp(schedule.get("end_time")),
        period_label="",
    )


def _stygian_seasons(raw: Any) -> list[Mapping[str, Any]]:
    """The Stygian Onslaught's seasons: the library hands over their list, and the raw archive keeps the whole answer,
    with the list under "data"."""
    return _items(raw.get("data") if isinstance(raw, Mapping) else raw)


def _latest_stygian(seasons: Any) -> Mapping[str, Any] | None:
    """The newest season of the Stygian Onslaught in which the player has a record."""
    played = [
        season
        for season in _stygian_seasons(seasons)
        if _map(season.get("schedule")).get("is_valid", True)
        and _map(season.get("single")).get("has_data")
        and _map(_map(season.get("single")).get("best"))
    ]
    return max(played, key=_season_start, default=None)


def _latest_theater(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    played = [season for season in _items(raw.get("data")) if season.get("has_data")]
    return max(played, key=_season_start, default=None)


def _stygian(seasons: Any) -> EndgameMode:
    latest = _latest_stygian(seasons)
    if latest is None:
        return EndgameMode("stygian", "Stygian Onslaught", False, "No attempt this season", None, None, None, "")
    schedule = _map(latest.get("schedule"))
    difficulty = _int(_map(_map(latest.get("single")).get("best")).get("difficulty"))
    return EndgameMode(
        mode="stygian",
        label="Stygian Onslaught",
        has_data=True,
        result_text=f"Difficulty {difficulty} cleared",
        score=float(difficulty),
        period_start=_timestamp(schedule.get("start_time")),
        period_end=_timestamp(schedule.get("end_time")),
        period_label=_text(schedule.get("name", "")),
    )


def normalize_endgame(abyss: Any, theater: Any, stygian: Any) -> EndgameRecord:
    return EndgameRecord((_abyss(_map(abyss)), _theater(_map(theater)), _stygian(stygian)))


_THEATER_DIFFICULTIES = {1: "Easy", 2: "Normal", 3: "Hard", 4: "Visionary", 5: "Arcana Challenge"}
# How HoYoLAB writes an element inside a Stygian Onslaught tag: a sprite (the library's HardChallengeTagElement).
_SPRITE_ELEMENTS = {
    "{SPRITE_PRESET#11001}": "Cryo",
    "{SPRITE_PRESET#11002}": "Hydro",
    "{SPRITE_PRESET#11003}": "Pyro",
    "{SPRITE_PRESET#11007}": "Dendro",
}
_ADVANTAGE, _DISADVANTAGE = 1, 0  # the tag types (HardChallengeTagType)
# The Abyss's highlights, each a list whose first item is the character it is of: HoYoLAB's key, and the kind kept.
_ABYSS_RANKS = (
    ("damage_rank", "strike"),
    ("defeat_rank", "defeats"),
    ("take_damage_rank", "taken"),
    ("energy_skill_rank", "bursts"),
    ("normal_skill_rank", "skills"),
    ("reveal_rank", "played"),
)
# The Theater's battle statistics, each one character.
_THEATER_RANKS = (("max_damage_avatar", "damage"), ("max_defeat_avatar", "defeats"), ("max_take_damage_avatar", "taken"))
_SPIRE_FIRST_FLOOR = 9  # the Abyssal Moon Spire's first floor: HoYoLAB's answer holds floors 9 to 12
_CHAMBER = re.compile(r"(\d+)-(\d+)")


def _enemy(raw: Mapping[str, Any]) -> EnemyRecord:
    return EnemyRecord(name=_text(raw.get("name", "")), level=_int(raw.get("level")), icon=_text(raw.get("icon") or ""))


def _rank(kind: str, raw: Mapping[str, Any]) -> RankRecord | None:
    """A highlight, or None when HoYoLAB names no character for it."""
    avatar = _int(raw.get("avatar_id"))
    if avatar <= 0:
        return None
    return RankRecord(kind=kind, value=_int(raw.get("value")), avatar_id=avatar, icon=_text(raw.get("avatar_icon") or ""))


def _first(value: Any) -> Mapping[str, Any]:
    items = _items(value)
    return items[0] if items else {}


def _ranks(pairs: Sequence[tuple[str, Mapping[str, Any]]]) -> tuple[RankRecord, ...]:
    found = (_rank(kind, raw) for kind, raw in pairs)
    return tuple(rank for rank in found if rank is not None)


def _abyss_halves(raw: Mapping[str, Any]) -> tuple[AbyssHalfRecord, ...]:
    halves = []
    for floor in _items(raw.get("floors")):
        for chamber in _items(floor.get("levels")):
            name = f"{_int(floor.get('index'))}-{_int(chamber.get('index'))}"
            for battle in _items(chamber.get("battles")):
                half = _int(battle.get("index"))
                if half not in (1, 2):
                    continue
                enemies = _items(chamber.get("top_half_floor_monster" if half == 1 else "bottom_half_floor_monster"))
                foes = tuple(_enemy(enemy) for enemy in enemies)
                halves.append(
                    AbyssHalfRecord(
                        chamber=name,
                        half=half,
                        stars=_int(chamber.get("star")),
                        characters=tuple(_int(avatar.get("id")) for avatar in _items(battle.get("avatars"))),
                        enemies=tuple(foe.name for foe in foes),
                        foes=foes,
                    )
                )
    return tuple(halves)


def _abyss_chambers(raw: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    """Each chamber's stars, floor by floor. A chamber the player skipped to ("skipped_floor", such as "10-3") is not in
    the answer's floors, and the answer's total counts its three stars: it is given those."""
    stars: dict[tuple[int, int], int] = {}
    for floor in _items(raw.get("floors")):
        for chamber in _items(floor.get("levels")):
            stars[(_int(floor.get("index")), _int(chamber.get("index")))] = _int(chamber.get("star"))
    skipped = _CHAMBER.fullmatch(str(raw.get("skipped_floor") or "").strip())
    if skipped:
        last = (int(skipped.group(1)), int(skipped.group(2)))
        for floor in range(_SPIRE_FIRST_FLOOR, last[0] + 1):
            for chamber in (1, 2, 3):
                if (floor, chamber) <= last:
                    stars.setdefault((floor, chamber), 3)
    return tuple((f"{floor}-{chamber}", count) for (floor, chamber), count in sorted(stars.items()))


def _tag_elements(tags: Sequence[Mapping[str, Any]], kind: int) -> tuple[str, ...]:
    """The elements the tags of one type name, in the order the text gives them, each once."""
    found: list[tuple[int, str]] = []
    for tag in tags:
        if _int(tag.get("type"), -1) != kind:
            continue
        text = str(tag.get("desc", ""))
        found += [(text.find(sprite), element) for sprite, element in _SPRITE_ELEMENTS.items() if sprite in text]
    ordered: list[str] = []
    for _, element in sorted(found):
        if element not in ordered:
            ordered.append(element)
    return tuple(ordered)


def _stygian_boss(challenge: Mapping[str, Any]) -> StygianBossRecord:
    monster = _map(challenge.get("monster"))
    tags = _items(monster.get("tags"))
    seconds = _optional_int(challenge.get("second"))
    return StygianBossRecord(
        name=_text(challenge.get("name") or monster.get("name") or ""),
        advantage=_tag_elements(tags, _ADVANTAGE),
        disadvantage=_tag_elements(tags, _DISADVANTAGE),
        team=tuple(_int(member.get("avatar_id")) for member in _items(challenge.get("teams"))),
        seconds=seconds if seconds and seconds > 0 else None,
        icon=_text(monster.get("icon") or ""),
        level=_int(monster.get("level")),
    )


def _next_stygian(seasons: Any, played: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """The newest season after the one played (or any, when none was) in which the player has no record yet, as (name,
    starts): HoYoLAB gives a season's bosses only after the first try. None when there is none."""
    after = _season_start(played) if played is not None else -math.inf
    waiting = [
        season
        for season in _stygian_seasons(seasons)
        if _map(season.get("schedule")).get("is_valid", True)
        and not _map(season.get("single")).get("has_data")
        and _season_start(season) > after
    ]
    newest = max(waiting, key=_season_start, default=None)
    schedule = _map(newest.get("schedule")) if newest is not None else {}
    name, starts = _text(schedule.get("name", "")), _timestamp(schedule.get("start_time"))
    return (name, starts) if name and starts else None


def normalize_endgame_detail(abyss_raw: Any, theater_raw: Any, stygian_raw: Any) -> EndgameDetailRecord:
    """What the pages need of the three endgame modes beyond their summaries. For the Characters pages: the Abyss's
    halves with their stars, teams and enemies; the Theater's difficulty, acts and end; the Stygian bosses with the
    elements they are weak and strong to. For the Stats page (its design, 3.4): the Abyss's start, battles, wins,
    chambers, enemies' levels and pictures and its highlights; the Theater's start, medals, Fantasia Flowers, time in
    battle and highlights; the Stygian season's name, start, best time and medal, its bosses' pictures and levels, and
    the next season when it has no record yet. A mode with nothing played gives empty details. The Stygian Onslaught's
    answer may be the library's list of seasons or the whole answer the archive keeps."""
    abyss = _map(abyss_raw)
    abyss_played = bool(abyss.get("is_unlock")) and _int(abyss.get("total_battle_times")) > 0
    theater = _latest_theater(_map(theater_raw))
    stat = _map(theater.get("stat")) if theater else {}
    fights = _map(_map(theater.get("detail")).get("fight_statisic")) if theater else {}
    medals = stat.get("get_medal_round_list")
    stygian = _latest_stygian(stygian_raw)
    single = _map(stygian.get("single")) if stygian else {}
    best = _map(single.get("best"))
    schedule = _map(stygian.get("schedule")) if stygian else {}
    best_seconds = _optional_int(best.get("second"))
    difficulty = _int(stat.get("difficulty_id")) if theater else 0
    return EndgameDetailRecord(
        abyss_stars=_int(abyss.get("total_star")) if abyss_played else None,
        abyss_halves=_abyss_halves(abyss) if abyss_played else (),
        abyss_ends=_timestamp(abyss.get("end_time")) if abyss_played else None,
        theater_difficulty=_THEATER_DIFFICULTIES.get(difficulty) if theater else None,
        theater_acts=_int(stat.get("max_round_id")) if theater else None,
        theater_ends=_timestamp(_map(theater.get("schedule")).get("end_time")) if theater else None,
        stygian_difficulty=_optional_int(best.get("difficulty")) if stygian else None,
        stygian_bosses=tuple(_stygian_boss(challenge) for challenge in _items(single.get("challenge"))),
        stygian_ends=_timestamp(schedule.get("end_time")) if stygian else None,
        abyss_starts=_timestamp(abyss.get("start_time")) if abyss_played else None,
        abyss_battles=_int(abyss.get("total_battle_times")) if abyss_played else 0,
        abyss_wins=_int(abyss.get("total_win_times")) if abyss_played else 0,
        abyss_chambers=_abyss_chambers(abyss) if abyss_played else (),
        abyss_ranks=_ranks([(kind, _first(abyss.get(key))) for key, kind in _ABYSS_RANKS]) if abyss_played else (),
        theater_starts=_timestamp(_map(theater.get("schedule")).get("start_time")) if theater else None,
        theater_medals=tuple(bool(_int(medal)) for medal in medals) if isinstance(medals, list) else (),
        theater_medal_count=_int(stat.get("medal_num")),
        theater_flowers=_int(stat.get("coin_num")),
        theater_seconds=_int(fights.get("total_use_time")),
        theater_ranks=_ranks([(kind, _map(fights.get(key))) for key, kind in _THEATER_RANKS]),
        stygian_season=_text(schedule.get("name", "")),
        stygian_starts=_timestamp(schedule.get("start_time")) if stygian else None,
        stygian_seconds=best_seconds if best_seconds and best_seconds > 0 else None,
        stygian_medal=_text(best.get("icon") or ""),
        stygian_next=_next_stygian(stygian_raw, stygian),
    )


def _cycle_id(value: Any) -> str | None:
    number = _optional_int(value)
    return str(number) if number is not None and number > 0 else None


def normalize_endgame_cycles(
    abyss_raw: Any, theater_raw: Any, stygian_raw: Any,
) -> list[tuple[str, str, EndgameDetailRecord]]:
    """Every cycle the three answers hold in which the player has a record, each as (mode, the schedule's id, the detail
    of that cycle alone): the Abyss's, and each Theater and Stygian season, the older ones too. A season with no record
    is no cycle, nor is one without an id or an end."""
    cycles: list[tuple[str, str, EndgameDetailRecord]] = []
    abyss = _map(abyss_raw)
    detail = normalize_endgame_detail(abyss, None, None)
    ident = _cycle_id(abyss.get("schedule_id"))
    if ident is not None and detail.abyss_ends is not None:
        cycles.append(("abyss", ident, detail))
    for season in _items(_map(theater_raw).get("data")):
        detail = normalize_endgame_detail(None, {"data": [season]}, None)
        ident = _cycle_id(_map(season.get("schedule")).get("schedule_id"))
        if ident is not None and detail.theater_ends is not None:
            cycles.append(("theater", ident, detail))
    for season in _stygian_seasons(stygian_raw):
        detail = normalize_endgame_detail(None, None, [season])
        ident = _cycle_id(_map(season.get("schedule")).get("schedule_id"))
        if ident is not None and detail.stygian_ends is not None:
            cycles.append(("stygian", ident, detail))
    return cycles


DIARY_TIME = "%Y-%m-%d %H:%M:%S"


def normalize_diary_entries(pages: Sequence[Any], server: tzinfo) -> tuple[DiaryEntry, ...]:
    """The detail log's pages as entries, their times read in the server's time. An entry that cannot be read is left
    out: the detail is a help to the month totals, never a reason to lose them."""
    entries = []
    for page in pages:
        for item in _items(_map(page).get("list")):
            time, action, amount = item.get("time"), item.get("action"), item.get("num")
            if not isinstance(time, str) or not isinstance(action, str):
                continue
            try:
                moment = datetime.strptime(time, DIARY_TIME).replace(tzinfo=server).astimezone(timezone.utc)
            except ValueError:
                continue
            entries.append(DiaryEntry(time, moment, action, _int(amount)))
    return tuple(entries)


def normalize_diary(
    months: Sequence[Mapping[str, Any]], today: date, entries: tuple[DiaryEntry, ...] = (), uid: int = 0,
) -> DiaryRecord:
    out = []
    for raw in months:
        month = _int(raw.get("data_month"))
        data = raw.get("month_data")
        if not 1 <= month <= 12 or not isinstance(data, Mapping):
            raise ValueError("a diary month is malformed")
        year = today.year - 1 if month > today.month else today.year
        sources = [DiarySource(_text(group.get("action", "")), _int(group.get("num"))) for group in _items(data.get("group_by"))]
        sources = sorted((source for source in sources if source.amount > 0), key=lambda source: -source.amount)
        out.append(DiaryMonth(year, month, _int(data.get("current_primogems")), tuple(sources)))
    out.sort(key=lambda entry: (entry.year, entry.month))
    return DiaryRecord(tuple(out), entries, uid)


def normalize_calendar(raw: Mapping[str, Any]) -> CalendarRecord:
    banners = sum(
        len(_items(raw.get(name)))
        for name in ("avatar_card_pool_list", "weapon_card_pool_list", "mixed_card_pool_list")
    )
    events = len(_items(raw.get("act_list"))) + len(_items(raw.get("fixed_act_list")))
    return CalendarRecord(banners=banners, events=events)


def _is_theater(entry: Mapping[str, Any]) -> bool:
    return isinstance(entry.get("role_combat_detail"), Mapping) or "theater" in str(entry.get("name", "")).lower()


def normalize_calendar_detail(raw: Mapping[str, Any]) -> CalendarDetailRecord:
    """The Theater's text from the calendar's challenge entries (whose words say this month's elements, when they do),
    and the character banners with the 5-star characters each features, as (names, starts, ends)."""
    raw = _map(raw)
    entry = next((entry for entry in _items(raw.get("fixed_act_list")) if _is_theater(entry)), None)
    text = _text(entry.get("desc", "")).replace("\\n", "\n").strip() if entry else ""
    banners = []
    for name in ("avatar_card_pool_list", "mixed_card_pool_list"):
        for pool in _items(raw.get(name)):
            featured = tuple(
                _text(character.get("name", ""))
                for character in _items(pool.get("avatars"))
                if _int(character.get("rarity"), 5) >= 5 and character.get("name")
            )
            if featured:
                banners.append(
                    (featured, _timestamp(pool.get("start_timestamp")) or "", _timestamp(pool.get("end_timestamp")) or "")
                )
    return CalendarDetailRecord(theater_text=text or None, banners=tuple(banners))


def normalize_accounts(raw: Any) -> list[GameAccount]:
    entries = _items(raw.get("list")) if isinstance(raw, Mapping) else _items(raw)
    return [
        GameAccount(_int(entry.get("game_uid")), _text(entry.get("nickname", "")), _text(entry.get("region", "")), _int(entry.get("level")))
        for entry in entries
        if str(entry.get("game_biz", "")).startswith("hk4e")
    ]
