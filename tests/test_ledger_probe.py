"""The ledger probe (the ledger design, section 9): it tells what the game's history logs hold, and never the key."""
import json
from urllib.parse import parse_qs, quote, urlsplit

from guildboard import netpolicy
from guildboard.ledger import LEDGER_URL, probe
from ledger_samples import csc_url
from pulls_samples import made_up_game

KEY = "PROBE-KEY-0123456789-ABCDEFGH"  # made up: the Feedback link's key
WISH_KEY = "PROBE-WISH-KEY-0123456789-ABCDEFGH"  # made up: a wish link's key, which the probe never sends


class _Answer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, amount: int) -> bytes:
        return self._data[:amount]

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _entry(entry_id: int, reason: str, amount: int, **more: object) -> dict:
    return {"id": str(entry_id), "uid": "700000001", "datetime": "2026-10-27 04:01:00", "add_num": str(amount),
            "reason": reason, **more}


ENTRIES = {
    "GetPrimogemLog": [_entry(2, "Blessing of the Welkin Moon", 90), _entry(1, "Daily Commission reward", 60)],
    "GetCrystalLog": [_entry(4, "Top-up", 1960), _entry(3, "Blessing of the Welkin Moon", 300)],
    "GetResinLog": [_entry(6, "Domain reward", -20), _entry(5, "Original Resin recovery", 1)],
    "GetArtifactLog": [_entry(8, "Domain reward", 1, name="Gladiator's Nostalgia", quality="5"),
                       _entry(7, "Artifact destroyed", -1, name="Wanderer's String-Kettle", quality="4")],
}


def _game_with_feedback(home, key: str = KEY):
    """A made-up game whose web cache holds a wish link, and whose log holds the Feedback page's link with `key`."""
    log, cache = made_up_game(home, WISH_KEY)
    with log.open("a", encoding="utf-8") as file:
        file.write(f"OpenURL {csc_url(key)}\n")
    return log, cache


def test_the_probe_never_prints_the_key(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    _game_with_feedback(home)
    asked: list[str] = []

    def wire(request, timeout):
        url = request.full_url
        asked.append(url)
        endpoint = urlsplit(url).path.rsplit("/", 1)[-1]
        query = parse_qs(urlsplit(url).query)
        assert url.startswith(LEDGER_URL) and request.get_header("Cookie") is None
        assert query["authkey"] == [KEY] and query["lang"] == ["en"]
        page = ENTRIES[endpoint] if query["end_id"] == ["0"] else []
        return _Answer(json.dumps({"retcode": 0, "data": {"list": page}}).encode())

    monkeypatch.setattr(netpolicy, "_open", wire)
    monkeypatch.setattr(probe, "PAUSE_SECONDS", 0)
    monkeypatch.setattr("sys.argv", ["probe"])

    assert probe.main() == 0
    out = capsys.readouterr().out
    assert KEY not in out and WISH_KEY not in out and "authkey" not in out and "?" not in out
    assert "Try 1 of 1 (self-service link from the log, whole query): accepted" in out and "primogem: 2 entries" in out
    assert "Wish links found: 1" in out  # counted, never tried
    assert "Blessing of the Welkin Moon: 1 entries, sum 90" in out
    assert "1960: 1 entries" in out  # how a top-up is booked
    assert "Gladiator's Nostalgia / 5 / Domain reward" in out
    assert len(asked) == 4  # one page each: two entries, fewer than a page


def test_a_refused_kind_is_said_with_its_retcode(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    _game_with_feedback(home)
    answer = {"retcode": -1, "message": f"auth appid error for {KEY}"}  # a message that repeats the key
    asked: list[str] = []

    def wire(request, timeout):
        asked.append(request.full_url)
        return _Answer(json.dumps(answer).encode())

    monkeypatch.setattr(netpolicy, "_open", wire)
    monkeypatch.setattr(probe, "PAUSE_SECONDS", 0)
    monkeypatch.setattr("sys.argv", ["probe"])

    assert probe.main() == 1
    out = capsys.readouterr().out
    assert 'Try 1 of 1 (self-service link from the log, whole query): refused, retcode -1, "auth appid error for [secret]"' in out
    assert "primogem: refused, retcode -1" in out and KEY not in out
    assert len(asked) == 1 and WISH_KEY not in asked[0]  # the wish link's key is not tried in its place


def test_without_a_feedback_link_the_probe_says_so_and_asks_nothing(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    made_up_game(home, WISH_KEY)  # a wish link alone: conftest.py fails the test on any request
    monkeypatch.setattr("sys.argv", ["probe"])

    assert probe.main() == 1
    out = capsys.readouterr().out
    assert "No Feedback link holds a key. Open Feedback from the Paimon menu in the game, then run the probe again." in out
    assert "Wish links found: 1" in out and WISH_KEY not in out


def test_the_login_mode_asks_for_a_key_as_guild_board_does_and_sends_no_cookie_elsewhere(tmp_path, monkeypatch, capsys):
    from guildboard.config import AppPaths
    from guildboard.tokenstore import StoredToken, TokenStore

    cookies = {
        "ltoken_v2": "v2_LTOKEN-VALUE-0123456789", "ltuid_v2": "123456", "ltmid_v2": "LTMID-VALUE-0123456789",
        "cookie_token_v2": "v2_COOKIE-VALUE-0123456789", "account_id_v2": "123456",
        "account_mid_v2": "ACCOUNT-MID-VALUE-0123456789",
    }
    minted = "MINTED-PROBE-KEY/0123+456789=="  # made up; HoYoverse's answer writes it URL-encoded
    monkeypatch.setenv("TUSKFIZZY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # no game here: nothing of it may be needed
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    TokenStore(paths.token_path).save(StoredToken(dict(cookies), 700000001, "os_euro", "T"))
    sent: list[tuple[str, str | None, bytes | None]] = []

    def wire(request, timeout):
        url = request.full_url
        sent.append((url, request.get_header("Cookie"), request.data))
        if url == netpolicy.MINT_URL:
            assert json.loads(request.data)["auth_appid"] == "csc"
            return _Answer(json.dumps({"retcode": 0, "data": {"authkey": quote(minted, safe="")}}).encode())
        query = parse_qs(urlsplit(url).query)
        assert url.startswith(LEDGER_URL) and query["authkey"] == [minted]
        endpoint = urlsplit(url).path.rsplit("/", 1)[-1]
        if endpoint == "GetUserInfo":
            return _Answer(json.dumps({"retcode": 0, "data": {"uid": "700000001", "region": "os_euro"}}).encode())
        page = ENTRIES[endpoint] if query["end_id"] == ["0"] else []
        return _Answer(json.dumps({"retcode": 0, "data": {"list": page}}).encode())

    monkeypatch.setattr(netpolicy, "_open", wire)
    monkeypatch.setattr(probe, "PAUSE_SECONDS", 0)
    monkeypatch.setattr("sys.argv", ["probe", "--login"])

    assert probe.main() == 0
    out = capsys.readouterr().out
    assert "GetUserInfo: accepted, fields: region, uid" in out
    assert "primogem: 2 entries" in out and "ltoken_v2" in out  # the cookies' names, never their values
    # The key was asked for at the mint alone, with the four cookies it takes; nothing else carried a cookie.
    [mint] = [(header, body) for url, header, body in sent if url == netpolicy.MINT_URL]
    assert {part.split("=", 1)[0] for part in mint[0].split("; ")} == set(netpolicy.MINT_COOKIES) - {"lrsag"}
    assert all(header is None for url, header, _ in sent if url != netpolicy.MINT_URL)
    for url, header, body in sent:
        for name, value in cookies.items():
            if len(value) < 10:  # the account's number, which the mint's own cookie and body carry
                continue
            if url != netpolicy.MINT_URL or name not in netpolicy.MINT_COOKIES:
                assert value not in f"{url} {header} {body!r}", (name, url)
    assert sent[0][0] == netpolicy.MINT_URL and len(sent) == 6  # the mint, the account and one page of each log
    for value in (*cookies.values(), minted, quote(minted, safe="")):
        if len(value) >= 8:
            assert value not in out


def test_a_self_service_link_in_the_games_log_is_tried_first(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    log, _ = made_up_game(home, "OLD-WISH-KEY-0123456789")
    csc = "CSC-KEY-0123456789-ABCDEFGH"
    with log.open("a", encoding="utf-8") as file:
        file.write("OpenURL https://gs.hoyoverse.com/event/user-game-search/genshin/index.html?auth_appid=csc"
                   f"&game_biz=hk4e_global&authkey={csc}&authkey_ver=1&sign_type=2#/moneyRecord\n")

    def wire(request, timeout):
        query = parse_qs(urlsplit(request.full_url).query)
        if query["authkey"] != [csc]:
            return _Answer(json.dumps({"retcode": -1, "message": "unknown auth appid:webview_gacha"}).encode())
        endpoint = urlsplit(request.full_url).path.rsplit("/", 1)[-1]
        page = ENTRIES[endpoint] if query["end_id"] == ["0"] else []
        return _Answer(json.dumps({"retcode": 0, "data": {"list": page}}).encode())

    monkeypatch.setattr(netpolicy, "_open", wire)
    monkeypatch.setattr(probe, "PAUSE_SECONDS", 0)
    monkeypatch.setattr("sys.argv", ["probe"])

    assert probe.main() == 0
    out = capsys.readouterr().out
    assert "Self-service links with a key: 1 in the game's log, 0 in its web cache" in out
    assert "Try 1 of" in out and "(self-service link from the log, whole query): accepted" in out
    assert csc not in out
