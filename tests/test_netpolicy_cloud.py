"""netpolicy.fetch_cloud, the one sender of the online features (the online plan, call 2): it takes a path, never an
address, so it can reach nothing but the project."""
import email.message
import http.client
import io
import urllib.error
from datetime import datetime, timezone

import pytest

from guildboard import netpolicy
from guildboard.netpolicy import CLOUD_HOST, HostNotAllowed, RedirectRefused, ResponseTooLarge, fetch_cloud


class _Answer:
    length = None  # as http.client's answer has it when no Content-Length came

    def __init__(self, data: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(data)
        self.status = status
        self.headers = email.message.Message()  # none, as an answer may come

    def read(self, amount: int) -> bytes:
        return self._stream.read(amount)

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _recording(monkeypatch, answer: _Answer | None = None) -> list:
    seen = []

    def fake_open(request, timeout):
        seen.append((request, timeout))
        return answer if answer is not None else _Answer(b"[]")

    monkeypatch.setattr(netpolicy, "_open", fake_open)
    return seen


def test_fetch_cloud_goes_to_the_one_host(monkeypatch):
    seen = _recording(monkeypatch, _Answer(b'[{"name": "Nova"}]', 206))
    assert fetch_cloud("/rest/v1/profiles?select=name") == (206, b'[{"name": "Nova"}]')
    [(request, timeout)] = seen
    assert request.full_url == "https://tspjpcyhpkfetwzolzvi.supabase.co/rest/v1/profiles?select=name"
    assert request.get_method() == "GET"
    assert request.get_header("User-agent") == "TUSKFiZZY"
    assert request.follow_redirects is False
    assert timeout == 20.0


def test_a_post_sends_its_body_and_headers_and_the_callers_user_agent_is_dropped(monkeypatch):
    seen = _recording(monkeypatch)
    fetch_cloud(
        "/auth/v1/token?grant_type=refresh_token", method="POST", body=b"{}",
        headers={"apikey": "key", "user-agent": "something else"}, timeout=5.0,
    )
    [(request, timeout)] = seen
    assert (request.get_method(), request.data, timeout) == ("POST", b"{}", 5.0)
    assert request.get_header("Apikey") == "key"
    assert request.get_header("User-agent") == "TUSKFiZZY"


def test_a_delete_without_a_body_is_still_a_delete(monkeypatch):
    seen = _recording(monkeypatch)
    fetch_cloud("/rest/v1/shared_stats?key=eq.wishes", method="DELETE")
    assert seen[0][0].get_method() == "DELETE"


@pytest.mark.parametrize("path", [
    "/storage/v1/object", "https://evil.example/rest/v1/x", "rest/v1/x", "/rest/v1/../admin", "//evil.example/rest/v1/",
    "/rest/v1/x@evil.example", "/auth/v1\\x", "/auth/v1/\\x", "/rest/v1//x", "/rest/v1", "/functions/v1/invite", "",
])
def test_fetch_cloud_refuses_every_other_path(path, monkeypatch):
    seen = _recording(monkeypatch)
    with pytest.raises(HostNotAllowed):
        fetch_cloud(path)
    assert seen == []


def test_a_refused_path_names_only_the_host():
    # A path can hold a query, and a query a token: the refusal stays out of what a log might show.
    with pytest.raises(HostNotAllowed) as error:
        fetch_cloud("/storage/v1/object?token=SECRET-VALUE")
    assert "SECRET-VALUE" not in str(error.value)


def _refusing(monkeypatch, status: int, body: bytes) -> None:
    def fake_open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "refused", email.message.Message(), io.BytesIO(body))

    monkeypatch.setattr(netpolicy, "_open", fake_open)


def test_an_http_error_is_an_answer(monkeypatch):
    _refusing(monkeypatch, 400, b'{"message": "request_limit"}')
    assert fetch_cloud("/rest/v1/rpc/x", method="POST", body=b"{}") == (400, b'{"message": "request_limit"}')


def test_an_http_errors_body_is_bounded(monkeypatch):
    _refusing(monkeypatch, 500, b"x" * 100)
    assert fetch_cloud("/rest/v1/x", max_bytes=10) == (500, b"x" * 10)


def test_an_answer_above_the_bound_is_refused(monkeypatch):
    _recording(monkeypatch, _Answer(b"x" * 11))
    with pytest.raises(ResponseTooLarge):
        fetch_cloud("/rest/v1/x", max_bytes=10)
    _recording(monkeypatch, _Answer(b"x" * 10))
    assert fetch_cloud("/rest/v1/x", max_bytes=10) == (200, b"x" * 10)


class _CutShort(_Answer):
    def read(self, amount: int) -> bytes:
        raise http.client.IncompleteRead(b"par")


class _CutShortStream(io.BytesIO):
    def read(self, amount: int = -1) -> bytes:
        raise http.client.IncompleteRead(b"par")


def test_an_answer_cut_short_is_a_connection_error(monkeypatch):
    _recording(monkeypatch, _CutShort(b""))
    with pytest.raises(ConnectionError):
        fetch_cloud("/rest/v1/x")


class _Wire:
    """What http.client reads an answer from, so that the real parser runs and no socket is opened."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO(self._raw)


def _from_the_wire(raw: bytes) -> http.client.HTTPResponse:
    response = http.client.HTTPResponse(_Wire(raw))
    response.begin()
    return response


@pytest.mark.parametrize("raw", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nonly this",
    b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n",  # nothing came at all
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n64\r\nonly this",
])
def test_an_answer_that_stops_before_its_end_is_a_connection_error(raw, monkeypatch):
    # http.client notices this in a read of everything, and not in a read of at most so much, which is what a bound asks
    # for: an answer that stopped short of its Content-Length would pass for a whole one.
    _recording(monkeypatch, _from_the_wire(raw))
    with pytest.raises(ConnectionError):
        fetch_cloud("/rest/v1/x")


@pytest.mark.parametrize("raw, status, body", [
    (b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nall of it", 200, b"all of it"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n9\r\nall of it\r\n0\r\n\r\n", 200, b"all of it"),
    (b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nall of it", 200, b"all of it"),  # no length: the end is the end
    (b"HTTP/1.1 204 No Content\r\n\r\n", 204, b""),
])
def test_a_whole_answer_comes_back_whole(raw, status, body, monkeypatch):
    _recording(monkeypatch, _from_the_wire(raw))
    assert fetch_cloud("/rest/v1/x") == (status, body)


def test_the_servers_clock_comes_with_its_answer(monkeypatch):
    # The Date of an answer is the server's clock, which sync sets this PC's against (the audit's CLD-2). The answer is still
    # its status and body, as it unpacks and compares.
    noon = datetime(2026, 11, 1, 12, 0, 5, tzinfo=timezone.utc).timestamp()
    _recording(monkeypatch, _from_the_wire(b"HTTP/1.1 200 OK\r\nDate: Sun, 01 Nov 2026 12:00:05 GMT\r\nContent-Length: 2\r\n\r\n[]"))
    answer = fetch_cloud("/rest/v1/x")
    assert answer == (200, b"[]") and answer.date == noon
    status, body = answer
    assert (status, body) == (200, b"[]")

    def refused(request, timeout):
        headers = email.message.Message()
        headers["Date"] = "Sun, 01 Nov 2026 12:00:05 GMT"
        raise urllib.error.HTTPError(request.full_url, 400, "refused", headers, io.BytesIO(b"{}"))

    monkeypatch.setattr(netpolicy, "_open", refused)
    assert fetch_cloud("/rest/v1/x").date == noon  # a refusal tells the time too


@pytest.mark.parametrize("header", [b"", b"Date: soon\r\n", b"Date: \r\n"])
def test_an_answer_with_no_date_that_can_be_read_has_none(header, monkeypatch):
    _recording(monkeypatch, _from_the_wire(b"HTTP/1.1 200 OK\r\n" + header + b"Content-Length: 2\r\n\r\n[]"))
    answer = fetch_cloud("/rest/v1/x")
    assert answer == (200, b"[]") and answer.date is None


def test_an_answer_longer_than_the_bound_stays_too_large(monkeypatch):
    # Its Content-Length is not used up by a bounded read, and that is no answer cut short.
    _recording(monkeypatch, _from_the_wire(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 100))
    with pytest.raises(ResponseTooLarge):
        fetch_cloud("/rest/v1/x", max_bytes=10)


def test_an_error_answer_cut_short_is_a_connection_error(monkeypatch):
    def fake_open(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "failed", email.message.Message(), _CutShortStream())

    monkeypatch.setattr(netpolicy, "_open", fake_open)
    with pytest.raises(ConnectionError):
        fetch_cloud("/rest/v1/x")


def test_an_answer_that_is_no_http_is_a_connection_error(monkeypatch):
    def fake_open(request, timeout):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(netpolicy, "_open", fake_open)
    with pytest.raises(ConnectionError):
        fetch_cloud("/rest/v1/x")


def test_no_redirect_is_followed_not_even_on_the_project(monkeypatch):
    seen = _recording(monkeypatch)
    fetch_cloud("/rest/v1/x")
    request = seen[0][0]
    with pytest.raises(RedirectRefused):
        netpolicy._CheckedRedirects().redirect_request(
            request, None, 302, "Found", {}, f"https://{CLOUD_HOST}/rest/v1/y"
        )
