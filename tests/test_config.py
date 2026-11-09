import json
from pathlib import Path

from guildboard.config import (
    PAGE_SIZES, AppPaths, Settings, default_data_dir, load_settings, save_settings, settings_from_dict,
)


def test_default_data_dir_is_under_local_app_data(monkeypatch):
    monkeypatch.delenv("TUSKFIZZY_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_data_dir() == Path(r"C:\Users\someone\AppData\Local\TUSKFiZZY")
    assert default_data_dir(demo=True) == Path(r"C:\Users\someone\AppData\Local\TUSKFiZZY-demo")


def test_the_new_settings_keep_only_their_choices():
    chosen = settings_from_dict({"commission_region": "sumeru", "fishing_rule": "next_reset", "notifications": False})
    assert (chosen.commission_region, chosen.fishing_rule, chosen.notifications) == ("sumeru", "next_reset", False)
    refused = settings_from_dict({"commission_region": "atlantis", "fishing_rule": "weekly", "notifications": "yes"})
    assert (refused.commission_region, refused.fishing_rule, refused.notifications) == ("liyue", "3_days", True)
    assert settings_from_dict({"commission_region": ["liyue"]}).commission_region == "liyue"


def test_data_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("TUSKFIZZY_DATA_DIR", str(tmp_path))
    assert default_data_dir() == tmp_path
    assert default_data_dir(demo=True) == tmp_path


def test_paths_and_ensure(tmp_path):
    paths = AppPaths(tmp_path / "data")
    assert paths.db_path == tmp_path / "data" / "guildboard.db"
    assert paths.token_path == tmp_path / "data" / "token.bin"
    assert paths.settings_path == tmp_path / "data" / "settings.json"
    paths.ensure()
    assert paths.icons_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.backups_dir.is_dir()


def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(path, Settings(in_session_interval_min=30, sync_paused=True, risk_acknowledged=True))
    loaded = load_settings(path)
    assert loaded.in_session_interval_min == 30
    assert loaded.sync_paused is True
    assert loaded.risk_acknowledged is True
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_settings_give_defaults(tmp_path):
    assert load_settings(tmp_path / "nope.json") == Settings()


def test_corrupt_settings_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert load_settings(path) == Settings()
    path.write_text('["a list, not an object"]', encoding="utf-8")
    assert load_settings(path) == Settings()


def test_wrong_types_and_unknown_keys_are_ignored():
    settings = settings_from_dict(
        {"in_session_interval_min": 45, "sync_paused": "yes", "start_with_windows": True, "made_up": 1}
    )
    assert settings.in_session_interval_min == 0  # 45 is not one of 0, 30, 60
    assert settings.sync_paused is False  # a string is not a bool
    assert settings.start_with_windows is True
    assert "made_up" not in settings.to_dict()


def test_bool_is_not_accepted_as_interval():
    assert settings_from_dict({"in_session_interval_min": True}).in_session_interval_min == 0


def test_saved_file_is_plain_json(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(path, Settings(in_session_interval_min=60))
    assert json.loads(path.read_text(encoding="utf-8"))["in_session_interval_min"] == 60


def test_an_old_settings_file_with_astras_key_still_loads(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"astra_offer_dismissed": true}', encoding="utf-8")
    assert load_settings(path) == Settings()


def test_settings_written_by_the_qt_build_still_load(tmp_path):
    # The Qt build kept "smooth_animations" in settings.json. The Electron build has no such setting: the key is ignored,
    # what the file holds besides it keeps its values, and the next save leaves the key out.
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"smooth_animations": True, "in_session_interval_min": 60, "overlay_opacity": 70, "overlay_lines": False}),
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert (loaded.in_session_interval_min, loaded.overlay_opacity, loaded.overlay_lines) == (60, 70, False)
    assert not hasattr(loaded, "smooth_animations")
    assert "smooth_animations" not in loaded.to_dict()
    save_settings(path, loaded)
    assert "smooth_animations" not in json.loads(path.read_text(encoding="utf-8"))


def test_overlay_settings_have_their_defaults():
    settings = Settings()
    assert settings.overlay_hotkey == "Ctrl+Alt+G"
    assert (settings.overlay_display, settings.overlay_opacity) == ("", 100)
    assert (settings.overlay_pinned, settings.overlay_lines) == (True, True)


def test_overlay_settings_keep_only_what_they_accept():
    assert [settings_from_dict({"overlay_opacity": value}).overlay_opacity for value in (49, 50, 75, 100, 101)] == [
        100, 50, 75, 100, 100,
    ]
    for refused in (True, 75.5, "75", None):
        assert settings_from_dict({"overlay_opacity": refused}).overlay_opacity == 100
    assert settings_from_dict({"overlay_display": "d" * 64}).overlay_display == "d" * 64
    assert settings_from_dict({"overlay_display": "d" * 65}).overlay_display == ""  # at most 64 characters
    for refused in (5, None, ["DISPLAY2"], "\ud83d"):  # half of an emoji cannot be written to the answers or the file
        assert settings_from_dict({"overlay_display": refused}).overlay_display == ""
    assert settings_from_dict({"overlay_hotkey": "alt+ctrl+k"}).overlay_hotkey == "Ctrl+Alt+K"  # kept as its text
    assert settings_from_dict({"overlay_hotkey": "Ctrl+Alt+E"}).overlay_hotkey == "Ctrl+Alt+G"  # AltGr types an E
    for refused in ("G", "Ctrl+", 7, None):
        assert settings_from_dict({"overlay_hotkey": refused}).overlay_hotkey == "Ctrl+Alt+G"
    assert settings_from_dict({"overlay_pinned": False, "overlay_lines": False}).overlay_pinned is False
    shown = settings_from_dict({"overlay_pinned": "no", "overlay_lines": 0})
    assert (shown.overlay_pinned, shown.overlay_lines) == (True, True)


def test_the_overlay_size_is_one_of_five_sizes_and_100_percent_unless_another_is_chosen():
    assert Settings().overlay_size == 100  # the Board's size: larger only when the player chooses it
    for size in (100, 125, 150, 175, 200):
        assert settings_from_dict({"overlay_size": size}).overlay_size == size
    # Anything else is taken as 100, the 0 that "Automatic" saved before included.
    for refused in (0, 50, 110, 225, True, 150.0, "150", None):
        assert settings_from_dict({"overlay_size": refused}).overlay_size == 100, refused


def test_the_page_sizes_are_one_of_seven_steps_and_100_percent_unless_another_is_chosen():
    assert PAGE_SIZES == (70, 80, 90, 100, 110, 120, 130)  # the window's steps too (PAGE_SIZES in shell/src/dashboard.ts)
    # One for a window and one for a maximized window; the page grows with the window, as it did before the settings.
    for name in ("page_size", "page_size_maximized"):
        assert getattr(Settings(), name) == 100
        for size in PAGE_SIZES:
            assert getattr(settings_from_dict({name: size}), name) == size
        for refused in (0, 60, 75, 140, True, 90.0, "90", None):
            assert getattr(settings_from_dict({name: refused}), name) == 100, (name, refused)


def test_the_page_sizes_are_kept_in_the_file(tmp_path):
    path = tmp_path / "settings.json"
    save_settings(path, Settings(page_size=110, page_size_maximized=80))
    kept = load_settings(path)
    assert (kept.page_size, kept.page_size_maximized) == (110, 80)


def test_overlay_settings_are_kept_in_the_file(tmp_path):
    path = tmp_path / "settings.json"
    chosen = Settings(
        overlay_hotkey="Shift+Win+F9", overlay_display=r"\\.\DISPLAY2", overlay_opacity=60, overlay_pinned=False,
        overlay_lines=False, overlay_size=150,
    )
    save_settings(path, chosen)
    assert load_settings(path) == chosen


def test_the_gallery_settings_are_off_by_default_and_keep_only_a_short_path():
    assert (Settings().gallery_enabled, Settings().gallery_folder) == (False, "")
    chosen = settings_from_dict({"gallery_enabled": True, "gallery_folder": r"D:\Games\Genshin\ScreenShot"})
    assert (chosen.gallery_enabled, chosen.gallery_folder) == (True, r"D:\Games\Genshin\ScreenShot")
    refused = settings_from_dict({"gallery_enabled": "yes", "gallery_folder": "x" * 1001})
    assert (refused.gallery_enabled, refused.gallery_folder) == (False, "")


def test_the_picture_quality_is_high_by_default_and_one_of_three_words():
    assert Settings().picture_quality == "high"
    for quality in ("high", "medium", "low"):
        assert settings_from_dict({"picture_quality": quality}).picture_quality == quality
    for refused in ("ultra", "", "LOW", 1, None):
        assert settings_from_dict({"picture_quality": refused}).picture_quality == "high"


def test_a_network_path_is_no_gallery_folder():
    # Looking at it would reach out to another computer.
    for pasted in (r"\\server\share\ScreenShot", "//server/share/ScreenShot", r"\\?\UNC\server\share\ScreenShot"):
        assert settings_from_dict({"gallery_folder": pasted}).gallery_folder == ""


def test_settings_reject_an_unknown_currency():
    # US dollars by default, for a new player and the demo (the player's choice of 2026-11-03); a saved choice is kept.
    assert (Settings().spending_currency, Settings().spending_prices) == ("USD", {})
    assert settings_from_dict({"spending_currency": "EUR"}).spending_currency == "EUR"
    assert settings_from_dict({"spending_currency": "PLN"}).spending_currency == "PLN"
    assert settings_from_dict({"spending_currency": "GBP"}).spending_currency == "USD"
    assert settings_from_dict({"spending_currency": ["EUR"]}).spending_currency == "USD"


def test_the_corrected_prices_keep_only_known_packs_and_plain_numbers():
    chosen = settings_from_dict({"spending_prices": {"PLN": {"c60": 5, "welkin": 21.99}, "EUR": {}}})
    assert chosen.spending_prices == {"PLN": {"c60": 5.0, "welkin": 21.99}, "EUR": {}}
    for refused in (
        {"GBP": {"c60": 1.0}}, {"PLN": {"c61": 1.0}}, {"PLN": {"c60": -1}}, {"PLN": {"c60": True}},
        {"PLN": {"c60": "5"}}, {"PLN": {"c60": 100_001}}, ["PLN"], {"PLN": ["c60"]},
    ):
        assert settings_from_dict({"spending_prices": refused}).spending_prices == {}, refused


def test_sync_my_data_is_on_by_default_and_is_a_plain_true_or_false():
    assert Settings().sync_my_data is True
    assert settings_from_dict({"sync_my_data": False}).sync_my_data is False
    for refused in ("no", 0, None, [], {}):
        assert settings_from_dict({"sync_my_data": refused}).sync_my_data is True
