"""Gift codes in the core (the gift codes design, section 1): cleaning, the history per account, sending one by one with
the pause, the livestream codes and their one notification, and the routes."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from guildboard.api.app import create_app
from guildboard.codes import PASTED_LOGIN, CodeBook, holds_cookies, is_code, nothing_sent, skipped_words, split_codes
from guildboard.config import AppPaths, load_settings
from guildboard.core import PAUSE_SECONDS, CodesRefused, Core
from guildboard.store import Store
from guildboard.tokenstore import StoredToken
from support import AccountSource, FakeSleep

UID = "700000001"
COOKIES = {"ltoken_v2": "v2_SECRET-VALUE-0123456789", "cookie_token_v2": "v2_OTHER-0123456789", "account_id_v2": "1"}
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
PORT = 4321


class CodeSource(AccountSource):
    """A source that answers each code from a script, and the livestream codes from a list."""

    def __init__(self, answers: dict[str, str] | None = None, live: list[dict] | None = None) -> None:
        super().__init__()
        self.answers = answers or {}
        self.live = live or []
        self.sent: list[str] = []
        self.delay = 0.0

    async def redeem(self, code: str) -> str:
        self.sent.append(code)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answers.get(code, "redeemed")

    async def livestream_codes(self) -> list[dict]:
        return list(self.live)


def _core(tmp_path, clock, source=None, *, demo: bool = False) -> Core:
    sleep = FakeSleep(clock)
    core = Core(
        AppPaths(tmp_path / "data"), demo=demo, clock=clock, source=None if demo else (source or CodeSource()),
        sleep=sleep, process_lister=lambda: set(),
    )
    core.fake_sleep = sleep
    core.settings.risk_acknowledged = True  # the first run's consent, before which nothing is asked of HoYoverse
    if not demo:
        core.tokens.save(StoredToken(dict(COOKIES), int(UID), "os_euro", "Traveler"))
    return core


async def _finish(core: Core) -> None:
    await core._codes_send


def test_codes_are_split_cleaned_and_deduplicated():
    assert split_codes(" genshingift, GENSHINGIFT\n7q2b8r4kz3mh  \n\n") == ["GENSHINGIFT", "7Q2B8R4KZ3MH"]
    assert split_codes("") == []


def test_split_codes_keeps_only_code_shaped_tokens():
    # A gift code is 6 to 20 of the letters A to Z and the digits, a letter among them, once in capitals; the marks
    # around it in a sentence or a list ("GENSHINGIFT.", "(GENSHINGIFT)") are no part of it. Any other word is left out
    # and only counted: a UID or an account's number, a word too short or too long. An ordinary word of 6 to 20 letters
    # is shaped as a code too, and is sent: the shape cannot tell words from codes.
    text = (
        "Codes: GENSHINGIFT, abc; (7q2b8r4kz3mh) AAAAAA \"BBBBBBBBBBBBBBBBBBBB\" CCCCCCCCCCCCCCCCCCCCC ﬁveﬁve "
        "123456789 MADE-UP-CODE genshingift. today:TODAYCODE1 'LIVECODE1'"
    )
    assert split_codes(text) == [
        "GENSHINGIFT", "7Q2B8R4KZ3MH", "AAAAAA", "BBBBBBBBBBBBBBBBBBBB", "TODAYCODE1", "LIVECODE1",
    ]
    # "Codes", "abc", the 21 letters, the ligatures (no ASCII however they are written in capitals), the UID, the code
    # with dashes and "today".
    assert skipped_words(text) == 7
    assert split_codes("GENSHINGIFT = 60 primogems") == ["GENSHINGIFT", "PRIMOGEMS"]  # a word is no code, yet it is sent
    assert skipped_words(" GENSHINGIFT \n") == 0 and skipped_words("") == 0
    assert [is_code(text) for text in ("GENSHINGIFT", "ABC123", "genshingift", "ABCDE", "ABC-DEF", "123456789", None,
                                       12345678)] == [True, True, False, False, False, False, False, False]


def test_a_text_that_holds_a_logins_cookies_is_no_list_of_codes():
    # A cookie, as the login's Cookie line writes each (a name, then "="), means a pasted login: none of the text is a
    # code, whatever its words, so that no part of a cookie, cut at the dots inside its value, is ever sent.
    for pasted in (
        "Cookie: ltoken_v2=v2_MADE-UP.ABCDEFGHIJ; ltuid_v2=123456789 GENSHINGIFT", "account_id_v2=123456789",
        "'mi18nLang=en-us'",
    ):
        assert holds_cookies(pasted) and split_codes(pasted) == [], pasted
    assert not holds_cookies("GENSHINGIFT = 60 primogems") and not holds_cookies("Codes: GENSHINGIFT")


def test_a_redeem_link_gives_its_code_and_no_junk_words():
    # The marks a code may stand between also cut a link at its dots and its colon, which made "HOYOVERSE" a code and
    # dropped the real one. A word holding "/" or "?" is no code; the code= parameter of a HoYoverse link is read.
    link = "https://genshin.hoyoverse.com/en/gift?code=GENSHINGIFT&lang=en-us"
    assert split_codes(link) == ["GENSHINGIFT"] and skipped_words(link) == 0
    assert split_codes("Use (https://genshin.hoyoverse.com/gift?code=7q2b8r4kz3mh). Also LIVECODE1") == [
        "7Q2B8R4KZ3MH", "LIVECODE1",
    ]
    assert skipped_words("Use (https://genshin.hoyoverse.com/gift?code=7q2b8r4kz3mh). Also LIVECODE1") == 2  # Use, Also
    for elsewhere in (
        "https://evil.example/redeem?code=GENSHINGIFT", "http://genshin.hoyoverse.com/gift?code=GENSHINGIFT",
        "genshin.hoyoverse.com/gift?code=GENSHINGIFT", "docs/GENSHINGIFT", "GENSHINGIFT?", "https://hoyoverse.com/",
    ):
        assert split_codes(elsewhere) == [] and skipped_words(elsewhere) == 1, elsewhere


def test_a_code_parameter_is_no_pasted_login():
    # "code=" looks like the name of a cookie, but a text of it was told to be a login's cookies, which it is not.
    assert not holds_cookies("code=GENSHINGIFT") and not holds_cookies("CODE=abc")
    assert split_codes("code=GENSHINGIFT") == ["GENSHINGIFT"] and skipped_words("code=GENSHINGIFT") == 0
    assert nothing_sent("code=abc") == (
        "Nothing was sent: this is not a gift code. A gift code is 6 to 20 letters and digits, with at least one letter."
    )


def test_a_text_that_names_a_login_cookie_anywhere_is_a_pasted_login():
    # A JSON or cookies.txt export of the browser's cookies names the cookie without "name=value", and its values may be
    # short: none of it is a list of codes.
    for pasted in (
        '[{"domain": ".hoyolab.com", "name": "ltoken_v2", "value": "v2_X"}]',
        ".hoyolab.com	TRUE	/	TRUE	0	ltuid_v2	123456789",
        '{"cookie_token_v2": "abc", "other": "GENSHINGIFT"}', "ltmid_v2 GENSHINGIFT", "lrsag: x",
    ):
        assert holds_cookies(pasted) and split_codes(pasted) == [], pasted
        assert nothing_sent(pasted) == PASTED_LOGIN, pasted
    assert not holds_cookies("ltoken_v2x GENSHINGIFT") and not holds_cookies("my ltuid_v3")


async def test_text_that_is_not_a_code_is_never_sent_or_kept(tmp_path, clock):
    # The login's paste steps tell the player to copy the Cookie line. Pasted into the codes box, nothing of it is sent
    # to HoYoverse or kept in the history (which goes up to the account), and the player is told so in a sentence that
    # repeats none of it.
    source = CodeSource()
    core = _core(tmp_path, clock, source)
    pasted = "Cookie: ltoken_v2=v2_MADE-UP-LTOKEN-0123456789; ltuid_v2=123456789; account_id_v2=123456789; mi18nLang=en-us"
    with pytest.raises(CodesRefused) as refused:
        core.redeem_codes(pasted, None)
    assert str(refused.value) == "Nothing was sent: this holds a login's cookies, not gift codes."
    # Words that are no codes, a UID among them.
    with pytest.raises(CodesRefused) as refused:
        core.redeem_codes("hello there 700000001", None)
    assert str(refused.value) == (
        "Nothing was sent: none of these 3 words is a gift code. A gift code is 6 to 20 letters and digits, with at "
        "least one letter."
    )
    with pytest.raises(CodesRefused, match="Nothing was sent: this is not a gift code."):
        core.redeem_codes("700000001", None)
    assert source.sent == [] and core.codes_model()["history"] == [] and core.codes_model()["progress"] == []
    assert core.store.gift_code_rows(UID) == [] and core._codes_send is None
    core.store.close()


async def test_a_text_with_codes_and_other_words_sends_the_codes_and_says_what_was_skipped(tmp_path, clock):
    source = CodeSource()
    core = _core(tmp_path, clock, source)
    assert core.redeem_codes("Codes for today: GENSHINGIFT and 7q2b8r4kz3mh", 60)
    await _finish(core)
    assert source.sent == ["GENSHINGIFT", "7Q2B8R4KZ3MH"]
    progress = core.codes_model()["progress"]
    assert [(entry["code"], entry["state"]) for entry in progress] == [
        ("GENSHINGIFT", "redeemed"), ("7Q2B8R4KZ3MH", "redeemed"), ("", "skipped"),
    ]
    assert progress[-1]["text"] == "4 words were skipped: they are not gift codes."
    assert sorted(row["code"] for row in core.codes_model()["history"]) == ["7Q2B8R4KZ3MH", "GENSHINGIFT"]
    core.store.close()


async def test_a_livestream_code_that_is_not_shaped_as_a_code_is_left_out(tmp_path, clock):
    live = [
        {"code": "LIVECODE1", "primogems": 60}, {"code": "NOT A CODE", "primogems": 60},
        {"code": "LTUID_V2=123456789", "primogems": 0}, {"code": "ABC", "primogems": 0},
        {"code": "123456789", "primogems": 0},
    ]
    source = CodeSource(live=live)
    core = _core(tmp_path, clock, source)
    await core.check_live(force=True)
    assert [item["code"] for item in core.codes_model()["live"]] == ["LIVECODE1"]
    assert core.redeem_live([item["code"] for item in live])  # a list that names them all sends only what is a code
    await _finish(core)
    assert source.sent == ["LIVECODE1"]
    assert [row["code"] for row in core.codes_model()["history"]] == ["LIVECODE1"]
    core.store.close()


def test_only_redeemed_codes_count_toward_the_totals():
    book = CodeBook(Store.in_memory())
    book.record(UID, "A", "redeemed", 50, "typed", NOW)
    book.record(UID, "B", "redeemed", 100, "livestream", NOW)
    book.record(UID, "C", "already", 60, "typed", NOW)
    book.record(UID, "D", "expired", 60, "typed", NOW)
    book.record("2", "E", "redeemed", 999, "typed", NOW)
    assert book.totals(UID) == {"primogems": 150, "redeemed": 2}
    assert book.totals("none") == {"primogems": 0, "redeemed": 0}
    assert book.set_primogems(UID, "A", 80) and book.totals(UID)["primogems"] == 180
    assert not book.set_primogems(UID, "Z", 5)
    assert {row["code"] for row in book.history(UID)} == {"A", "B", "C", "D"}
    assert book.blocked(UID, "C")["result"] == "already"
    assert book.blocked(UID, "D") is None


def test_a_removed_code_leaves_the_totals_and_may_be_sent_again():
    book = CodeBook(Store.in_memory())
    book.record(UID, "A", "redeemed", 50, "typed", NOW)
    book.record("2", "A", "redeemed", 70, "typed", NOW)
    assert book.remove(UID, "A") is True
    assert book.remove(UID, "A") is False
    assert book.totals(UID) == {"primogems": 0, "redeemed": 0}
    assert book.blocked(UID, "A") is None
    assert book.totals("2")["primogems"] == 70


async def test_a_redeemed_code_is_not_sent_again_and_others_may_be(tmp_path, clock):
    source = CodeSource()
    core = _core(tmp_path, clock, source)
    core.codebook.record(UID, "DONECODE1", "redeemed", 50, "typed", NOW)
    core.codebook.record(UID, "OLDCODE01", "expired", 0, "typed", NOW)
    assert core.redeem_codes("donecode1 oldcode01", 40)
    await _finish(core)
    assert source.sent == ["OLDCODE01"]
    first, second = core.codes_model()["progress"]
    assert (first["code"], first["state"], first["text"]) == ("DONECODE1", "redeemed", "You redeemed this code on 26 Oct.")
    assert (second["code"], second["state"]) == ("OLDCODE01", "redeemed")
    assert core.codebook.totals(UID) == {"primogems": 90, "redeemed": 2}
    core.store.close()


async def test_codes_go_one_by_one_with_the_pause(tmp_path, clock):
    core = _core(tmp_path, clock, CodeSource({"BBBBBB2": "expired"}))
    queue = core.bus.subscribe()
    assert core.redeem_codes("aaaaaa1 bbbbbb2 cccccc3", None)
    await _finish(core)
    assert core.fake_sleep.calls == [PAUSE_SECONDS, PAUSE_SECONDS] == [5.5, 5.5]
    assert [e["state"] for e in core.codes_model()["progress"]] == ["redeemed", "expired", "redeemed"]
    assert not queue.empty() and queue.get_nowait()["type"] == "codes_changed"
    assert core.codes_model()["busy"] is False
    core.store.close()


async def test_a_login_that_is_lost_stops_the_rest_without_keeping_them(tmp_path, clock):
    source = CodeSource({"AAAAAA1": "login"})
    core = _core(tmp_path, clock, source)
    core.redeem_codes("aaaaaa1 bbbbbb2", None)
    await _finish(core)
    assert source.sent == ["AAAAAA1"]
    assert [e["state"] for e in core.codes_model()["progress"]] == ["login", "login"]
    assert core.codes_model()["history"] == []
    core.store.close()


async def test_a_second_redeem_while_one_runs_is_refused(tmp_path, clock):
    core = _core(tmp_path, clock)
    assert core.redeem_codes("aaaaaa1 bbbbbb2", None) is True
    assert core.codes_model()["busy"] is True
    assert core.redeem_codes("cccccc3", None) is False
    await _finish(core)
    core.store.close()


async def test_livestream_codes_are_new_until_redeemed_or_already_and_notified_once(tmp_path, clock):
    live = [{"code": "LIVECODE1", "primogems": 60}, {"code": "LIVECODE2", "primogems": 60}]
    source = CodeSource(live=live)
    core = _core(tmp_path, clock, source)
    shown: list[tuple[str, str]] = []
    core.notifier = lambda kind, text: shown.append((kind, text)) or True
    core.has_tray = True
    await core.check_live(force=True)
    assert shown == [("codes", "2 new livestream codes. Open TUSKFiZZY to redeem them.")]
    await core.check_live(force=True)
    assert len(shown) == 1  # told once
    assert [item["code"] for item in core.codes_model()["live"]] == ["LIVECODE1", "LIVECODE2"]
    assert core.redeem_live(["LIVECODE1", "LIVECODE2"])
    await _finish(core)
    assert core.codes_model()["live"] == []
    assert core.codebook.totals(UID) == {"primogems": 120, "redeemed": 2}
    source.live = [*live, {"code": "LIVECODE3", "primogems": 0}]
    await core.check_live(force=True)
    assert len(shown) == 2 and shown[1][1] == "1 new livestream code. Open TUSKFiZZY to redeem it."
    core.store.close()


async def test_redeem_live_takes_only_codes_shown(tmp_path, clock):
    # The page showed two new livestream codes, and a third came while it was open: Redeem all sends the two the player saw,
    # and only those of them that are still new (the audit's WEB-3; the rule: a code the player clicked to redeem).
    live = [{"code": "LIVECODE1", "primogems": 60}, {"code": "LIVECODE2", "primogems": 60}]
    source = CodeSource(live=live)
    core = _core(tmp_path, clock, source)
    await core.check_live(force=True)
    shown = [item["code"] for item in core.codes_model()["live"]]
    source.live = [*live, {"code": "LIVECODE3", "primogems": 0}]
    await core.check_live(force=True)
    assert core.redeem_live([*shown, "NOT-A-LIVE-CODE"])
    await _finish(core)
    assert source.sent == ["LIVECODE1", "LIVECODE2"]
    assert [item["code"] for item in core.codes_model()["live"]] == ["LIVECODE3"]
    with pytest.raises(CodesRefused, match="There is no code to redeem."):
        core.redeem_live(shown)  # both settled since: nothing of what the page showed is new
    core.store.close()


def test_the_live_route_sends_the_codes_the_page_shows(tmp_path, clock):
    source = CodeSource(live=[{"code": "LIVECODE1", "primogems": 60}, {"code": "LIVECODE2", "primogems": 60}])
    core = _core(tmp_path, clock, source)
    with TestClient(create_app(core, PORT), base_url=f"http://127.0.0.1:{PORT}") as client:
        assert client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 303
        client.portal.call(lambda: core.check_live(force=True))
        for body in (None, {}, {"codes": "LIVECODE1"}, {"codes": [1]}, {"codes": ["x" * 200]}, {"codes": ["LIVECODE1"], "more": 1}):
            answer = client.post("/api/codes/redeem-live", json=body)
            assert answer.status_code == 422, body  # without the list it shows, nothing is sent
        assert source.sent == []
        assert client.post("/api/codes/redeem-live", json={"codes": ["LIVECODE2"]}).json() == {"started": True}
        client.portal.call(_finish, core)
        assert source.sent == ["LIVECODE2"]
    core.store.close()


async def test_a_look_within_a_minute_is_not_repeated(tmp_path, clock):
    source = CodeSource(live=[{"code": "LIVECODE1", "primogems": 1}])
    core = _core(tmp_path, clock, source)
    await core.check_live()
    source.live = []
    await core.check_live()
    assert [item["code"] for item in core.codes_model()["live"]] == ["LIVECODE1"]
    clock.advance(seconds=61)
    await core.check_live()
    assert core.codes_model()["live"] == []
    core.store.close()


async def test_the_demo_does_not_redeem(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    with pytest.raises(CodesRefused, match="The demo does not redeem codes."):
        core.redeem_codes("ABC", None)
    with pytest.raises(CodesRefused):
        core.redeem_live(["DEMOLIVE1"])
    model = core.codes_model()
    assert sorted((r["code"], r["result"], r["primogems"]) for r in model["history"]) == [
        ("DEMOGIFT1", "redeemed", 50), ("DEMOGIFT2", "redeemed", 100), ("DEMOOLD", "expired", 0),
    ]
    assert sorted(r["result"] for r in model["history"]) == ["expired", "redeemed", "redeemed"]
    assert model["totals"] == {"primogems": 150, "redeemed": 2}
    assert core.status()["codes"] == {"primogems": 150, "redeemed": 2}
    await core.check_live(force=True)
    assert [item["code"] for item in core.codes_model()["live"]] == ["DEMOLIVE1", "DEMOLIVE2"]
    core.store.close()


def test_without_an_account_the_history_is_empty_and_a_login_is_needed(tmp_path, clock):
    core = Core(
        AppPaths(tmp_path / "data"), demo=False, clock=clock, source=CodeSource(), sleep=FakeSleep(clock),
        process_lister=lambda: set(),
    )
    model = core.codes_model()
    assert (model["history"], model["needs_login"], model["totals"]) == ([], True, {"primogems": 0, "redeemed": 0})
    with pytest.raises(CodesRefused, match="Log in again"):
        core.redeem_codes("A", None)
    core.store.close()


def test_the_routes(tmp_path, clock):
    source = CodeSource(live=[{"code": "LIVECODE1", "primogems": 60}])
    source.delay = 0.3
    core = _core(tmp_path, clock, source)
    with TestClient(create_app(core, PORT), base_url=f"http://127.0.0.1:{PORT}") as client:
        assert client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 303
        for _ in range(50):  # the look runs in the background: the page hears of its result
            live = client.get("/api/codes").json()["live"]
            if live:
                break
            time.sleep(0.05)
        assert [item["code"] for item in live] == ["LIVECODE1"]
        assert client.post("/api/codes/redeem", json={"text": "abcdef1", "primogems": 5000000}).status_code == 422
        assert client.post("/api/codes/redeem", json={"text": 3, "primogems": None}).status_code == 422
        assert client.post("/api/codes/redeem", json={"text": "  ", "primogems": None}).status_code == 409
        refused = client.post("/api/codes/redeem", json={"text": "123456789", "primogems": None})
        assert (refused.status_code, refused.json()["error"]) == (
            409, "Nothing was sent: this is not a gift code. A gift code is 6 to 20 letters and digits, with at least "
            "one letter.",
        )
        login = client.post("/api/codes/redeem", json={"text": "ltuid_v2=123456789", "primogems": None})
        assert login.json()["error"] == "Nothing was sent: this holds a login's cookies, not gift codes."
        assert client.post("/api/codes/redeem", json={"text": "abcdef1", "primogems": 70}).json() == {"started": True}
        busy = client.post("/api/codes/redeem", json={"text": "def", "primogems": None})
        assert (busy.status_code, busy.json()["error"]) == (409, "Codes are being redeemed.")
        assert client.put("/api/codes/ABC", json={"primogems": -1}).status_code == 422
        assert client.put("/api/codes/NOPE", json={"primogems": 10}).status_code == 404
        missing = client.delete("/api/codes/NOPE")
        assert (missing.status_code, missing.json()["error"]) == (404, "This code is not in your history.")
    core.store.close()


def test_the_demo_routes_refuse(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    with TestClient(create_app(core, PORT), base_url=f"http://127.0.0.1:{PORT}") as client:
        assert client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 303
        answer = client.post("/api/codes/redeem", json={"text": "ABC", "primogems": None})
        assert (answer.status_code, answer.json()["error"]) == (409, "The demo does not redeem codes.")
        assert client.post("/api/codes/redeem-live", json={"codes": ["DEMOLIVE1"]}).status_code == 409
        assert client.put("/api/codes/DEMOGIFT1", json={"primogems": 55}).json()["totals"]["primogems"] == 155
    core.store.close()


def test_schema_8_keeps_codes_per_account_and_the_notices():
    store = Store.in_memory()
    tables = {name for (name,) in store._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"gift_codes", "gift_code_notices"} <= tables
    store.put_gift_code("1", "A", "redeemed", 5, "typed", NOW)
    store.put_gift_code("2", "A", "expired", 0, "typed", NOW)  # the same code on another account is its own row
    assert [row[1] for row in store.gift_code_rows("1")] == ["redeemed"]
    store.put_gift_code("1", "A", "already", 0, "typed", NOW)  # a later answer replaces the earlier one
    assert [row[1] for row in store.gift_code_rows("1")] == ["already"]


def test_the_codes_route_answers_without_waiting_for_a_slow_look(tmp_path, clock):
    import time

    class Slow(CodeSource):
        async def livestream_codes(self):
            await asyncio.sleep(1.5)
            return [{"code": "LIVECODE1", "primogems": 1}]

    core = _core(tmp_path, clock, Slow())
    with TestClient(create_app(core, PORT), base_url=f"http://127.0.0.1:{PORT}") as client:
        assert client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False).status_code == 303
        started = time.monotonic()
        assert client.get("/api/codes").json()["live"] == []
        assert time.monotonic() - started < 1.0
        client.get("/api/codes")  # a look is running: no second one starts
        assert core._live_look is not None
    core.store.close()


async def test_the_pause_is_kept_across_runs(tmp_path, clock):
    core = _core(tmp_path, clock)
    core.redeem_codes("aaaaaa1", None)
    await _finish(core)
    assert core.fake_sleep.calls == []
    clock.advance(seconds=2)
    core.redeem_codes("bbbbbb2", None)
    await _finish(core)
    assert core.fake_sleep.calls == [3.5]
    clock.advance(seconds=30)
    core.redeem_codes("cccccc3", None)
    await _finish(core)
    assert core.fake_sleep.calls == [3.5]
    core.store.close()


async def test_an_already_redeemed_code_keeps_the_primogems_typed_and_the_totals_stay(tmp_path, clock):
    core = _core(tmp_path, clock, CodeSource({"AAAAAA1": "already"}))
    core.redeem_codes("aaaaaa1", 70)
    await _finish(core)
    assert [(r["result"], r["primogems"]) for r in core.codes_model()["history"]] == [("already", 70)]
    assert core.codes_model()["totals"] == {"primogems": 0, "redeemed": 0}
    core.store.close()


async def test_nothing_is_asked_before_the_first_run_consent(tmp_path, clock):
    source = CodeSource(live=[{"code": "LIVECODE1", "primogems": 1}])
    asked = []
    original = source.livestream_codes

    async def counting():
        asked.append(1)
        return await original()

    source.livestream_codes = counting
    core = _core(tmp_path, clock, source)
    core.settings.risk_acknowledged = False
    await core.check_live(force=True)
    assert asked == [] and core.codes_model()["live"] == []
    core.settings.risk_acknowledged = True
    await core.check_live(force=True)
    assert asked == [1]
    core.store.close()


async def test_one_failed_look_does_not_end_the_hourly_loop(tmp_path, clock, monkeypatch):
    core = _core(tmp_path, clock)
    calls = []

    async def failing(*, force=False):
        calls.append(force)
        raise RuntimeError("boom")

    async def stop(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(core, "check_live", failing)
    monkeypatch.setattr("guildboard.core.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await core._watch_codes()
    assert calls == [True]  # it got as far as the wait
    core.store.close()


# ------------------------------------------------------------------ the login before the first code


async def test_the_page_asks_for_a_login_until_a_code_was_settled_or_a_login_finished(tmp_path, clock):
    core = _core(tmp_path, clock)
    assert core.codes_model()["login_first"] is True
    core._auth_changed()  # what Auth calls after a login
    assert core.settings.codes_login_done is True
    assert core.codes_model()["login_first"] is False
    assert load_settings(core.paths.settings_path).codes_login_done is True  # it stays across starts
    core.store.close()


async def test_a_logout_is_not_a_login(tmp_path, clock):
    core = _core(tmp_path, clock)
    core.tokens.delete()
    core._auth_changed()
    assert core.settings.codes_login_done is False
    core.store.close()


async def test_a_settled_code_makes_the_login_unneeded_but_a_refused_one_does_not(tmp_path, clock):
    core = _core(tmp_path, clock)
    core.codebook.record(UID, "OLD", "expired", 0, "typed", clock.now())
    assert core.codes_model()["login_first"] is True
    core.codebook.record(UID, "ALREADY", "already", 0, "typed", clock.now())
    assert core.codes_model()["login_first"] is False
    core.codebook.remove(UID, "ALREADY")
    core.codebook.record(UID, "NEW", "redeemed", 60, "typed", clock.now())
    assert core.codes_model()["login_first"] is False
    core.store.close()


async def test_the_demo_does_not_ask_for_a_login_first(tmp_path, clock):
    core = _core(tmp_path, clock, demo=True)
    assert core.codes_model()["login_first"] is False
    core.store.close()
