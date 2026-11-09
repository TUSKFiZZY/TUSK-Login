"""Folders and non-secret settings."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from guildboard.hotkeys import DEFAULT as DEFAULT_HOTKEY, hotkey_problem, parse_hotkey
from guildboard.ledger.reasons import CURRENCIES, PACK_IDS

log = logging.getLogger(__name__)

APP_DIR_NAME = "TUSKFiZZY"
ALLOWED_INTERVALS = (0, 30, 60)
OVERLAY_OPACITY = (50, 100)  # percent: the least and the most of a pinned widget that shows
OVERLAY_SIZES = (100, 125, 150, 175, 200)  # percent; the Board's size, 100, unless the player chooses larger
# Percent of the size the window's page grows to with the window: 100 fills it as before the setting. The shell steps through
# these with Ctrl and + or -, and with Ctrl and the mouse wheel (PAGE_SIZES in shell/src/dashboard.ts).
PAGE_SIZES = (70, 80, 90, 100, 110, 120, 130)
MAX_DISPLAY_CHARS = 64  # the longest name a display is kept under
REGIONS = ("mondstadt", "liyue", "inazuma", "sumeru", "fontaine", "natlan")  # the daily commissions' regions
REGION_NAMES = {
    "mondstadt": "Mondstadt", "liyue": "Liyue", "inazuma": "Inazuma", "sumeru": "Sumeru", "fontaine": "Fontaine",
    "natlan": "Natlan",
}
# The fishing spots' regions: the commissions' six and the two the commissions do not list yet
FISHING_REGIONS = (*REGIONS, "nod_krai", "snezhnaya")
FISHING_REGION_NAMES = {**REGION_NAMES, "nod_krai": "Nod-Krai", "snezhnaya": "Snezhnaya"}
FISHING_RULES = ("3_days", "next_reset")  # when a fishing spot comes back: guides disagree (spec 9.3)
PICTURE_QUALITIES = ("high", "medium", "low")  # the Characters pages' big pictures: the originals, at most 1536 pixels, at most 1024


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "guildboard.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.bin"

    @property
    def cloud_path(self) -> Path:
        return self.data_dir / "cloud.bin"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def icons_dir(self) -> Path:
        return self.data_dir / "cache" / "icons"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def ensure(self) -> None:
        for folder in (self.data_dir, self.icons_dir, self.logs_dir, self.backups_dir):
            folder.mkdir(parents=True, exist_ok=True)


def standard_data_dir(demo: bool = False) -> Path:
    """The folder a start with no --data-dir and no TUSKFIZZY_DATA_DIR uses."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / (APP_DIR_NAME + ("-demo" if demo else ""))


def default_data_dir(demo: bool = False) -> Path:
    override = os.environ.get("TUSKFIZZY_DATA_DIR")
    if override:
        return Path(override)
    return standard_data_dir(demo)


@dataclass
class Settings:
    in_session_interval_min: int = 0
    sync_paused: bool = False
    start_with_windows: bool = False
    risk_acknowledged: bool = False
    tray_hint_shown: bool = False
    commission_region: str = "liyue"  # the region the player chose in the game, which HoYoLAB does not report
    fishing_rule: str = "3_days"
    notifications: bool = True  # Windows notifications when a timer is ready
    # The Characters notices (the Characters design, 6.6), each on by default: the weekly bosses, an open domain, a goal ready.
    characters_notice_weekly: bool = True
    characters_notice_domain: bool = True
    characters_notice_ready: bool = True
    overlay_hotkey: str = DEFAULT_HOTKEY  # opens and closes the overlay, kept as Hotkey.text
    overlay_display: str = ""  # the display the overlay covers, by the name the shell gives it; "" is the main display
    overlay_opacity: int = 100  # percent of a pinned widget that shows over the game
    overlay_pinned: bool = True  # pinned widgets over the game
    overlay_lines: bool = True  # a line over the game when a timer is ready
    overlay_size: int = 100  # how large the overlay draws, in percent: one of OVERLAY_SIZES, any other value 100
    # The game runs in full screen, which minimizes it whenever it loses the focus: the open overlay then takes the
    # clicks but never the focus. Off, it takes both, as a game in a window or borderless allows.
    overlay_full_screen: bool = False
    # How large the window's page draws, in percent of its size for the window: one of PAGE_SIZES. One size for a window and
    # one for a maximized window, so that a full screen can draw smaller than the usual window.
    page_size: int = 100
    page_size_maximized: int = 100
    last_seen_version: str = ""  # the version whose "What's new" card the player has seen; "" before any
    codes_login_done: bool = False  # a login has finished: HoYoverse's first code of an account needs a fresh one
    gallery_enabled: bool = False  # the screenshot gallery (the ledger design, section 7): off, nothing of it is read
    gallery_folder: str = ""  # the game's ScreenShot folder, found or pasted; "" before either
    picture_quality: str = "high"  # how large the Characters pages' big pictures are fetched: one of PICTURE_QUALITIES (the Characters design, 11.7)
    spending_currency: str = "USD"  # the Ledger's prices: one of CURRENCIES
    # The player's corrections to the price list, by currency and then pack id (the ledger design, section 6).
    spending_prices: dict[str, dict[str, float]] = field(default_factory=dict)
    # Sync my data (the online design, section 5): the player's own data is copied to their online account and back. On once
    # they are signed in; nothing happens before that, whatever this says.
    sync_my_data: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MAX_FOLDER_CHARS = 1000  # a pasted folder's path, at most
MAX_PRICE = 100_000  # a pack's corrected price, at most

# The settings that hold one of a few words.
CHOICES: dict[str, tuple[str, ...]] = {
    "commission_region": REGIONS, "fishing_rule": FISHING_RULES, "spending_currency": CURRENCIES,
    "picture_quality": PICTURE_QUALITIES,
}


def _prices(value: Any) -> dict[str, dict[str, float]] | None:
    """The corrected prices when each is a known currency's known pack at a plain number; None otherwise."""
    if not isinstance(value, dict):
        return None
    kept: dict[str, dict[str, float]] = {}
    for currency, prices in value.items():
        if currency not in CURRENCIES or not isinstance(prices, dict):
            return None
        kept[currency] = {}
        for pack, price in prices.items():
            plain = isinstance(price, (int, float)) and not isinstance(price, bool)
            if pack not in PACK_IDS or not plain or not 0 <= price <= MAX_PRICE or price != price:
                return None
            kept[currency][pack] = float(price)
    return kept


def _writable(text: str) -> bool:
    """Whether the text can be written as UTF-8. Half of an emoji, which JSON can carry, cannot, and an answer that
    held it would fail."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def is_network_path(text: str) -> bool:
    """Whether a path starts with two slashes, as \\\\server\\share and \\\\?\\UNC\\ do: looking at it could reach out to
    another computer. (A device path such as \\\\?\\C:\\ is refused with them; a drive letter does the same.)"""
    return len(text) >= 2 and text[0] in "\\/" and text[1] in "\\/"


def _coerce(name: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if name in CHOICES:
        return value if isinstance(value, str) and value in CHOICES[name] else default
    if name == "overlay_hotkey":
        return parse_hotkey(value).text if isinstance(value, str) and hotkey_problem(value) is None else default
    if name == "last_seen_version":
        return value if isinstance(value, str) and len(value) <= 40 and _writable(value) else default
    if name == "gallery_folder":
        fits = isinstance(value, str) and len(value) <= MAX_FOLDER_CHARS and _writable(value)
        return value if fits and not is_network_path(value) else default
    if name == "spending_prices":
        prices = _prices(value)
        return prices if prices is not None else default
    if name == "overlay_display":
        return value if isinstance(value, str) and len(value) <= MAX_DISPLAY_CHARS and _writable(value) else default
    is_int = isinstance(value, int) and not isinstance(value, bool)
    if name == "in_session_interval_min":
        return value if is_int and value in ALLOWED_INTERVALS else default
    if name == "overlay_opacity":
        return value if is_int and OVERLAY_OPACITY[0] <= value <= OVERLAY_OPACITY[1] else default
    if name == "overlay_size":
        return value if is_int and value in OVERLAY_SIZES else default
    if name in ("page_size", "page_size_maximized"):
        return value if is_int and value in PAGE_SIZES else default
    return value if is_int else default


def settings_from_dict(data: Any) -> Settings:
    settings = Settings()
    if not isinstance(data, dict):
        return settings
    for field in fields(Settings):
        if field.name in data:
            setattr(settings, field.name, _coerce(field.name, data[field.name], getattr(settings, field.name)))
    return settings


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError):
        log.warning("the settings file is unreadable; using defaults")
        return Settings()
    return settings_from_dict(raw)


def save_settings(path: Path, settings: Settings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
