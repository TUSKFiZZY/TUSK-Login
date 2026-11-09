"""HoYoverse's wish-history pages, read with the link's own parameters (the Pulls plan, Task 6).

Every answer comes from tests/pulls_samples.py, and every key is a made-up one: no test reaches HoYoverse.
"""
import http.client
import io
import json
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import pytest

from guildboard import logging_setup, netpolicy
from guildboard.logging_setup import register_secret, setup_logging, shutdown_logging
from guildboard.pulls import wishapi
from guildboard.pulls.wishapi import (
    HISTORY_URL, KeyExpired, KeyInvalid, Offline, Page, TooFrequent, Unexpected, WishApiError, fetch_page, page_url,
)
from guildboard.store import WishRow
from pulls_samples import (
    KEY_EXPIRED, KEY_INVALID, PAGE, PAGE_OF_TWENTY, TOO_FREQUENT, UID, gacha_page, record, wish_link,
)

KEY_SENTENCE = "Open Wish → History in the game, then press Import again."
TOO_FREQUENT_SENTENCE = "HoYoverse says there were too many requests. Wait a minute, then press Import again."
OFFLINE_SENTENCE = "HoYoverse could not be reached. Check the connection and try again."
UNEXPECTED_SENTENCE = "The import failed unexpectedly. See the log file."


def answering(*answers):
    """A stand-in for netpolicy.fetch_hoyoverse: it gives `answers` in turn, raising the exceptions among them, and
    remembers each address it was asked for."""
    asked: list[str] = []
    queue = list(answers)

    def fetch(url: str) -> bytes:
        asked.append(url)
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    fetch.asked = asked
    return fetch


def test_the_constants_are_the_plans():
    assert HISTORY_URL == "https://public-operation-hk4e-sg.hoyoverse.com/gacha_info/api/getGachaLog"
    assert (wishapi.PAGE_SIZE, wishapi.PAUSE_SECONDS) == (20, 0.5)


def test_the_page_url_keeps_the_links_parameters_and_sets_the_page():
    link = wish_link("MADE+UP/KEY=0123456789")
    url = page_url(link, "301", "0")
    address, _, query = url.partition("?")
    assert address == HISTORY_URL
    sent = parse_qs(query, keep_blank_values=True)
    assert all(len(values) == 1 for values in sent.values())  # each parameter once: the key among them
    sent = {name: values[0] for name, values in sent.items()}
    assert sent["authkey"] == "MADE+UP/KEY=0123456789"
    assert query.count("authkey=") == 1
    assert (sent["lang"], sent["gacha_type"], sent["size"], sent["end_id"]) == ("en-us", "301", "20", "0")
    # Every other parameter of the link goes as the link has it, the key's version and the region among them.
    kept = {name: value for name, value in link.query.items() if name != "lang"}
    assert {name: sent[name] for name in kept} == kept
    assert (sent["authkey_ver"], sent["region"], sent["game_biz"], sent["auth_appid"]) == (
        "1", "os_euro", "hk4e_global", "webview_gacha"
    )
    assert set(sent) == set(link.query) | {"gacha_type", "size", "end_id"}
    # A later page names the last record of the page before it.
    assert parse_qs(urlsplit(page_url(link, "400", "1760000000000000001")).query)["end_id"] == ["1760000000000000001"]


def test_a_page_is_read_newest_first_with_server_time_converted():
    fetch = answering(PAGE)
    page = fetch_page(wish_link(), "301", "0", "character", fetch=fetch)
    assert isinstance(page, Page) and len(page.rows) == 20
    assert urlsplit(fetch.asked[0])._replace(query="").geturl() == HISTORY_URL
    first = page.rows[0]
    assert first == WishRow(
        id=1760000000000000020, uid=int(UID), gacha_type="301", pool="character", name="Fischl", item_type="Character",
        rank=4, time="2026-10-12 21:14:03", at=datetime(2026, 10, 12, 20, 14, 3, tzinfo=timezone.utc), import_id=0,
    )
    assert first.at.isoformat() == "2026-10-12T20:14:03+00:00"  # Europe's server time is an hour ahead of UTC
    assert [row.id for row in page.rows] == sorted((row.id for row in page.rows), reverse=True)
    assert {row.import_id for row in page.rows} == {0}  # the importer fills it
    assert {row.rank for row in page.rows} == {3, 4, 5}
    # The page is newest first however HoYoverse orders it.
    reversed_page = fetch_page(wish_link(), "301", "0", "character", fetch=answering(gacha_page(PAGE_OF_TWENTY[::-1])))
    assert reversed_page == page


def test_the_time_follows_the_links_region_and_europe_without_one():
    one = [record(1760000000000000001, time="2026-10-12 21:14:03")]
    asia = fetch_page(wish_link(region="os_asia"), "301", "0", "character", fetch=answering(gacha_page(one)))
    america = fetch_page(wish_link(region="os_usa"), "301", "0", "character", fetch=answering(gacha_page(one)))
    missing = fetch_page(wish_link(region=None), "301", "0", "character", fetch=answering(gacha_page(one)))
    assert asia.rows[0].at == datetime(2026, 10, 12, 13, 14, 3, tzinfo=timezone.utc)
    assert america.rows[0].at == datetime(2026, 10, 13, 2, 14, 3, tzinfo=timezone.utc)
    assert missing.rows[0].at == datetime(2026, 10, 12, 20, 14, 3, tzinfo=timezone.utc)


def test_an_empty_page_has_no_rows():
    page = fetch_page(wish_link(), "100", "0", "novice", fetch=answering(gacha_page([])))
    assert page == Page(rows=())


@pytest.mark.parametrize(
    ("answer", "error", "sentence"),
    [
        (KEY_INVALID, KeyInvalid, KEY_SENTENCE),
        (KEY_EXPIRED, KeyExpired, KEY_SENTENCE),
        (TOO_FREQUENT, TooFrequent, TOO_FREQUENT_SENTENCE),
        (b'{"data":null,"message":"something else","retcode":1}', Unexpected, UNEXPECTED_SENTENCE),
        (b"<html>Bad gateway</html>", Unexpected, UNEXPECTED_SENTENCE),
        (urllib.error.URLError("no route"), Offline, OFFLINE_SENTENCE),
        (TimeoutError("timed out"), Offline, OFFLINE_SENTENCE),
        (ConnectionResetError("reset"), Offline, OFFLINE_SENTENCE),
        (netpolicy.ResponseTooLarge("the response is larger than allowed"), Unexpected, UNEXPECTED_SENTENCE),
    ],
)
def test_each_answer_code_has_its_error_and_sentence(answer, error, sentence):
    with pytest.raises(error) as raised:
        fetch_page(wish_link(), "301", "0", "character", fetch=answering(answer))
    assert isinstance(raised.value, WishApiError)
    assert str(raised.value) == sentence


@pytest.mark.parametrize(
    "body",
    [
        b'{"retcode":0,"message":"OK","data":null}',
        b'{"retcode":0,"message":"OK","data":{"list":"none"}}',
        b'{"retcode":[-100],"message":"OK","data":null}',
        b'{"retcode":"0","message":"OK","data":{"list":[]}}',
        b'{"retcode":true,"message":"OK","data":{"list":[]}}',
        b"[1, 2]",
        b"\xff\xfe not text",
        gacha_page([{key: value for key, value in record(1).items() if key != "time"}]),
        gacha_page([record(1, rank=6)]),
        gacha_page([record(1, time="2026/10/12 21:14")]),
        gacha_page([{**record(1), "id": "not a number"}]),
        gacha_page([{**record(1), "uid": None}]),
    ],
)
def test_a_page_it_cannot_read_is_unexpected(body):
    with pytest.raises(Unexpected) as raised:
        fetch_page(wish_link(), "301", "0", "character", fetch=answering(body))
    assert str(raised.value) == UNEXPECTED_SENTENCE


def test_a_type_outside_the_six_takes_the_querys_pool():
    rows = [
        record(1760000000000000003, "999", name="Lightrace Wish item"),
        record(1760000000000000002, "400", name="Escoffier", rank=5),
        record(1760000000000000001, "500", name="Shenhe", rank=5),
    ]
    page = fetch_page(wish_link(), "500", "0", "chronicled", fetch=answering(gacha_page(rows)))
    assert [(row.gacha_type, row.pool) for row in page.rows] == [
        ("999", "chronicled"), ("400", "character"), ("500", "chronicled"),
    ]


def test_the_default_fetch_goes_through_netpolicy_to_the_history_address(monkeypatch):
    seen = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    def fake_open(request, timeout):
        seen.append((request.get_method(), urlsplit(request.full_url).hostname, request.get_header("User-agent")))
        return Answer(PAGE)

    monkeypatch.setattr(netpolicy, "_open", fake_open)
    assert len(fetch_page(wish_link(), "301", "0", "character").rows) == 20
    assert seen == [("GET", "public-operation-hk4e-sg.hoyoverse.com", "TUSKFiZZY")]


def _register(key: str) -> None:
    """Register the key as the reads of the game's cache do, in each form a link or an address can hold it."""
    for form in (key, quote(key, safe=""), quote_plus(key)):
        register_secret(form)


MARKER = "MARKER-0123456789-ABCDEFGH"
# A made-up key as long as the game's (over 600 characters), holding "/", "+" and "=", the marker at its start: a log
# line that cut a repeated key short before masking it would show the marker.
LONG_KEY = f"{MARKER}/" + "".join(f"{n:03d}+part={n:03d}/" for n in range(60))


def test_the_key_is_never_in_an_error(tmp_path):
    marker = MARKER
    key = LONG_KEY
    assert len(key) > 600 and {"/", "+", "="} <= set(key)
    link = wish_link(key)
    logging_setup.forget_secrets()
    _register(key)
    setup_logging(tmp_path / "logs")
    errors: list[WishApiError] = []
    url = page_url(link, "301", "0")
    try:
        for answer in (
            KEY_INVALID, KEY_EXPIRED, TOO_FREQUENT,
            # HoYoverse repeats the key in its message: as the link has it, encoded as the address has it, and cut.
            json.dumps({"data": None, "message": f"bad key {key}", "retcode": 1}).encode(),
            json.dumps({"data": None, "message": f"bad key {quote_plus(key)}", "retcode": 1}).encode(),
            json.dumps({"data": None, "message": f"bad key {key[:300]}", "retcode": -1}).encode(),
            json.dumps({"data": {"list": [{"id": key}]}, "message": "OK", "retcode": 0}).encode(),
            f"<html>{url}</html>".encode(),
            urllib.error.URLError(f"could not open {url}"),
            urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None),
        ):
            with pytest.raises(WishApiError) as raised:
                fetch_page(link, "301", "0", "character", fetch=answering(answer))
            errors.append(raised.value)
    finally:
        shutdown_logging()
        logging_setup.forget_secrets()
    assert len(errors) == 10
    for error in errors:
        assert marker not in str(error) and marker not in repr(error) and error.args == (str(error),)
        # Nothing of the failure stays with the error: not the answer, nor an exception holding the address.
        assert error.__cause__ is None and error.__context__ is None
    log_text = (tmp_path / "logs" / "guildboard.log").read_text(encoding="utf-8")
    assert "retcode 1" in log_text and "retcode -1" in log_text and "503" in log_text  # the log says what happened
    # No part of the key, whole or cut, masked or not.
    assert marker not in log_text and "+part=" not in log_text and quote_plus("+part=") not in log_text


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _redirected(request, timeout):
    """A stand-in for the network whose answer is a redirect elsewhere on HoYoverse's domains, handled as urllib handles
    one: through netpolicy's own redirect check. Followed, it leads to a page."""
    netpolicy._CheckedRedirects().redirect_request(
        request, None, 302, "Found", {}, "https://public-operation-hk4e-sg.hoyoverse.com/elsewhere?authkey=x"
    )
    return _Answer(PAGE)


def test_the_history_request_never_follows_a_redirect(tmp_path, monkeypatch):
    # The key goes only to the history address: a redirect, even within HoYoverse's domains, is refused there.
    monkeypatch.setattr(netpolicy, "_open", _redirected)
    logging_setup.forget_secrets()
    _register(LONG_KEY)
    setup_logging(tmp_path / "logs")
    try:
        with pytest.raises(Unexpected) as raised:
            fetch_page(wish_link(LONG_KEY), "301", "0", "character")
    finally:
        shutdown_logging()
        logging_setup.forget_secrets()
    assert str(raised.value) == UNEXPECTED_SENTENCE
    log_text = (tmp_path / "logs" / "guildboard.log").read_text(encoding="utf-8")
    assert "wish type 301" in log_text
    assert "?" not in log_text.split("wish type 301", 1)[1].splitlines()[0]  # no address's query is logged
    assert MARKER not in log_text and "authkey" not in log_text


@pytest.mark.parametrize("cut", [0, 1, 40, 200, 777, -2, -1])
def test_an_answer_cut_short_is_offline(cut):
    # A connection that dropped mid-answer leaves the JSON short: HoYoverse could not be reached, as for a drop before it.
    with pytest.raises(Offline) as raised:
        fetch_page(wish_link(), "301", "0", "character", fetch=answering(PAGE[:cut]))
    assert str(raised.value) == OFFLINE_SENTENCE


def test_an_answer_cut_short_inside_a_character_is_offline():
    body = json.dumps({"retcode": 0, "message": "OK", "data": {"list": [record(1, name="Thoma é")]}}, ensure_ascii=False)
    whole = body.encode("utf-8")
    with pytest.raises(Offline):
        fetch_page(wish_link(), "301", "0", "character", fetch=answering(whole[: whole.index("é".encode()) + 1]))


def test_a_connection_dropped_mid_answer_is_offline(monkeypatch):
    class Dropped(io.BytesIO):
        def read(self, amount=-1):
            raise http.client.IncompleteRead(PAGE[:100], len(PAGE) - 100)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(netpolicy, "_open", lambda request, timeout: Dropped())
    with pytest.raises(Offline) as raised:
        fetch_page(wish_link(), "301", "0", "character")
    assert str(raised.value) == OFFLINE_SENTENCE
