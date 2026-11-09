"""The online account through the API: signing in in the system browser, its one route without a cookie, and everything
that stays whole without an account (the online plan, Task A6)."""
import asyncio
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from cloud_fakes import USER_ID, FakeCloud, answer, token_answer
from guildboard import logging_setup
from guildboard.api.app import create_app
from guildboard.cloud.session import CloudSession, CloudStore
from guildboard.cloud.signin import challenge_for
from guildboard.config import AppPaths
from guildboard.core import Core
from support import AccountSource, FakeSleep

PORT = 4321
BASE = f"http://127.0.0.1:{PORT}"
EXCHANGE = ("POST", "/auth/v1/token?grant_type=pkce")
REFRESH = ("POST", "/auth/v1/token?grant_type=refresh_token")
ENSURE = ("POST", "/rest/v1/rpc/ensure_profile")
LOGOUT = ("POST", "/auth/v1/logout")
DELETE = ("POST", "/rest/v1/rpc/delete_my_account")
PROFILE = ("GET", "/rest/v1/profiles")
SIGNED_IN = "Signed in. You can close this tab."
NOT_FINISHED = "Signing in did not finish. You can close this tab and try again in TUSKFiZZY."
MADE_UP_ACCESS, MADE_UP_REFRESH = "MADE-UP-ACCESS-0123456789", "MADE-UP-REFRESH-0123456789"


def _signing_in(*extra: tuple) -> FakeCloud:
    answers = {
        EXCHANGE: token_answer(MADE_UP_ACCESS, MADE_UP_REFRESH, metadata={"custom_claims": {"global_name": "Sora"}}),
        ENSURE: answer(200, {"user_id": USER_ID, "name": "Sora", "discriminator": "7687"}),
    }
    return FakeCloud({**answers, **dict(extra)})


def _core(tmp_path, clock, cloud: FakeCloud, opened: list[str] | None = None, *, demo: bool = False) -> Core:
    return Core(
        AppPaths(tmp_path / "data"), demo=demo, clock=clock, source=None if demo else AccountSource(), sleep=FakeSleep(clock),
        process_lister=lambda: set(), cloud_fetch=cloud, open_browser=(opened if opened is not None else []).append,
    )


class Page:
    """A core, with a page that has the session cookie, and what the browser was told to open."""

    def __init__(self, tmp_path, clock, cloud: FakeCloud) -> None:
        self.cloud, self.opened, self.tmp_path = cloud, [], tmp_path
        self.core = _core(tmp_path, clock, cloud, self.opened)
        self.client = TestClient(create_app(self.core, PORT), base_url=BASE)

    @property
    def cloud_bin(self):
        return self.tmp_path / "data" / "cloud.bin"

    def __enter__(self) -> "Page":
        self.client.__enter__()
        self.client.get("/launch", params={"k": self.core.launch_secret}, follow_redirects=False)
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.__exit__(*exc)
        self.core.store.close()

    def view(self) -> dict:
        return self.client.get("/api/online").json()

    def nonce(self) -> str:
        redirect = parse_qs(urlsplit(self.opened[-1]).query)["redirect_to"][0]
        return redirect.rsplit("/", 1)[-1]

    def sign_in(self, code: str = "THE-CODE-0123456789") -> None:
        self.client.post("/api/online/sign-in")
        assert self.client.get(f"/auth/callback/{self.nonce()}", params={"code": code}).status_code == 200


@pytest.fixture
def page(tmp_path, clock):
    with Page(tmp_path, clock, _signing_in()) as page:
        yield page


def _no_cookie(page: Page) -> TestClient:
    return TestClient(create_app(page.core, PORT), base_url=BASE)


def _saved(tmp_path, clock, *, left: float = 3600) -> CloudSession:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    session = CloudSession(
        MADE_UP_ACCESS, MADE_UP_REFRESH, clock.now().timestamp() + left, USER_ID, "Sora", "7687",
    )
    CloudStore(paths.cloud_path).save(session)
    return session


def test_signed_out_there_are_no_requests(tmp_path, clock):
    cloud = FakeCloud()
    with Page(tmp_path, clock, cloud) as page:
        page.client.portal.call(page.core.start)
        for url in ("/api/status", "/api/online", "/api/settings", "/api/backlog"):
            assert page.client.get(url).status_code == 200
        page.client.portal.call(page.core.stop)
    assert cloud.requests == []
    assert page.opened == []


def test_a_start_signed_in_takes_its_look(tmp_path, clock):
    _saved(tmp_path, clock)
    cloud = FakeCloud({
        PROFILE: answer(200, [{"name": "Nova Vale", "discriminator": "0007"}]),
        ("POST", "/rest/v1/rpc/friends_overview"): answer(200, {"friends": [], "incoming": [], "outgoing": []}),
    })
    with Page(tmp_path, clock, cloud) as page:
        page.client.portal.call(page.core.start)
        for _ in range(500):
            if page.view()["tag"] == "0007":
                break
            page.client.portal.call(asyncio.sleep, 0.01)
        view = page.view()
        page.client.portal.call(page.core.stop)
    assert (view["name"], view["tag"]) == ("Nova Vale", "0007")


def test_a_stop_ends_the_online_task_and_a_waiting_sign_in(tmp_path, clock):
    ran: list[str] = []

    async def forever() -> None:
        ran.append("running")
        await asyncio.Event().wait()

    with Page(tmp_path, clock, FakeCloud()) as page:
        page.core.online.run = forever
        page.client.post("/api/online/sign-in")
        page.client.portal.call(page.core.start)
        task = page.core._online_task
        page.client.portal.call(asyncio.sleep, 0)
        page.client.portal.call(page.core.stop)
        assert ran == ["running"] and task.cancelled()
        assert page.view()["state"] == "signed_out"


def test_signed_out_the_view_says_so_and_offers_nothing_else(page):
    assert page.view() == {
        "state": "signed_out", "name": None, "tag": None, "offline": False, "message": None, "invite": None, "sync": None,
        "usage": None,
    }


def test_the_status_holds_the_view(page):
    assert page.client.get("/api/status").json()["online"] == page.view()


@pytest.mark.parametrize("method, url", [
    ("GET", "/api/online"), ("POST", "/api/online/sign-in"), ("POST", "/api/online/sign-out"), ("POST", "/api/online/delete"),
    ("POST", "/api/online/sync"), ("POST", "/api/online/first-sync"),
])
def test_every_online_route_but_the_callback_needs_the_cookie(page, method, url):
    with _no_cookie(page) as anonymous:
        assert anonymous.request(method, url, json={"confirm": True}).status_code == 401
    assert page.opened == []


def test_sign_in_opens_the_browser_at_the_authorize_address(page):
    view = page.client.post("/api/online/sign-in").json()
    assert view["state"] == "waiting"
    assert page.view()["state"] == "waiting"
    [address] = page.opened
    parts = urlsplit(address)
    assert (parts.hostname, parts.path) == ("tspjpcyhpkfetwzolzvi.supabase.co", "/auth/v1/authorize")
    query = parse_qs(parts.query)
    assert query["provider"] == ["custom:discord"]
    assert query["redirect_to"][0].startswith(f"http://127.0.0.1:{PORT}/auth/callback/")
    assert page.cloud.requests == []  # the browser asks; TUSKFiZZY has sent nothing yet


def test_a_browser_that_will_not_open_says_so(tmp_path, clock):
    with Page(tmp_path, clock, FakeCloud()) as page:
        page.core.online._open_browser = lambda address: False
        view = page.client.post("/api/online/sign-in").json()
    assert (view["state"], view["message"]) == ("signed_out", "The browser could not be opened. Try again.")


def test_the_callback_needs_no_cookie_but_the_right_nonce(page):
    page.client.post("/api/online/sign-in")
    with _no_cookie(page) as browser:
        wrong = browser.get("/auth/callback/not-the-nonce", params={"code": "THE-CODE-0123456789"})
        assert wrong.status_code == 400
        assert NOT_FINISHED in wrong.text
        assert page.cloud.requests == []
        assert page.view()["state"] == "waiting"  # a stray request does not end the waiting sign-in

        right = browser.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
    assert right.status_code == 200
    assert SIGNED_IN in right.text
    assert page.cloud.sent() == ["POST /auth/v1/token?grant_type=pkce", "POST /rest/v1/rpc/ensure_profile"]


def test_the_exchange_carries_the_code_and_the_waiting_verifier(page):
    page.client.post("/api/online/sign-in")
    challenge = parse_qs(urlsplit(page.opened[-1]).query)["code_challenge"][0]
    page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
    sent = page.cloud.body(0)
    assert sent["auth_code"] == "THE-CODE-0123456789"
    assert challenge_for(sent["code_verifier"]) == challenge


def test_the_callbacks_answer_stays_out_of_caches_and_referrers(page):
    page.client.post("/api/online/sign-in")
    response = page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Content-Type"].startswith("text/html")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "<script" not in response.text and "THE-CODE" not in response.text
    refused = page.client.get("/auth/callback/nonce", params={"code": "OTHER-CODE-0123456789"})
    assert refused.headers["Cache-Control"] == "no-store" and refused.headers["Referrer-Policy"] == "no-referrer"
    assert "OTHER-CODE" not in refused.text


def test_the_code_is_masked_once_its_nonce_matched_and_before_it_is_sent(tmp_path, clock):
    masked_when_sent: list[str] = []

    class Watching(FakeCloud):
        def __call__(self, path, **kwargs):
            masked_when_sent.append(logging_setup.redact("THE-CODE-0123456789"))
            return super().__call__(path, **kwargs)

    logging_setup.forget_secrets()
    try:
        with Page(tmp_path, clock, Watching(_signing_in().answers)) as page:
            page.sign_in()
    finally:
        logging_setup.forget_secrets()
    assert masked_when_sent and masked_when_sent[0] == "[redacted]"  # already at the exchange, the first request


def test_callback_with_wrong_nonce_registers_no_secret(page):
    # The callback is outside /api/, so only the Host check stands before it: whatever a stray request brings is no
    # secret of the log. A code no sign-in gives, longer than 512 characters or with other than URL-safe characters, is
    # refused with the same page, before anything else.
    page.client.post("/api/online/sign-in")
    before = set(logging_setup._secrets)
    for code in ("STRANGER-CODE-0123456789", "A" * 513, "A CODE WITH SPACES 0123", "<b>CODE</b>-0123456789"):
        refused = page.client.get("/auth/callback/not-the-nonce", params={"code": code})
        assert refused.status_code == 400 and NOT_FINISHED in refused.text, code
    assert set(logging_setup._secrets) == before
    assert page.cloud.requests == [] and page.view()["state"] == "waiting"
    # With the right nonce, an oversize code is no code: the sign-in did not finish, and nothing was registered or sent.
    oversize = page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "A" * 513})
    assert oversize.status_code == 400 and NOT_FINISHED in oversize.text
    assert set(logging_setup._secrets) == before and page.cloud.requests == []
    assert page.view()["message"] == "Signing in did not finish. Try again."


def test_the_callback_works_once(page):
    page.sign_in()
    nonce = page.nonce()
    again = page.client.get(f"/auth/callback/{nonce}", params={"code": "THE-CODE-0123456789"})
    assert again.status_code == 400
    assert len(page.cloud.requests) == 2  # no second exchange


@pytest.mark.parametrize("params", [{}, {"code": ""}, {"error": "access_denied", "error_description": "The user denied"}])
def test_the_callback_without_a_code_says_it_did_not_finish(page, params):
    page.client.post("/api/online/sign-in")
    nonce = page.nonce()
    response = page.client.get(f"/auth/callback/{nonce}", params=params)
    assert response.status_code == 400
    assert NOT_FINISHED in response.text
    assert page.cloud.requests == []
    view = page.view()
    assert (view["state"], view["message"]) == ("signed_out", "Signing in did not finish. Try again.")
    # The waiting sign-in is spent.
    assert page.client.get(f"/auth/callback/{nonce}", params={"code": "THE-CODE-0123456789"}).status_code == 400
    assert page.cloud.requests == []


def test_a_signed_in_view_has_the_name_and_tag(page):
    page.sign_in()
    assert page.cloud.body(1) == {"p_name": "Sora"}
    assert page.view() == {
        "state": "signed_in", "name": "Sora", "tag": "7687", "offline": False, "message": None, "invite": None,
        "sync": {"on": True, "last_at": None, "needs_choice": False},  # Sync my data is on once the player is signed in
        "usage": None,  # read after a sync that moved rows
    }
    kept = CloudStore(page.cloud_bin).load()
    assert (kept.user_id, kept.name, kept.tag, kept.access_token) == (USER_ID, "Sora", "7687", MADE_UP_ACCESS)


def test_a_new_sign_in_needs_no_second_start(tmp_path, clock):
    # Signed in once, and closed: the next start is still signed in.
    with Page(tmp_path, clock, _signing_in()) as page:
        page.sign_in()
    cloud = FakeCloud({PROFILE: answer(200, [{"name": "Sora", "discriminator": "7687"}])})
    with Page(tmp_path, clock, cloud) as page:
        assert page.view()["state"] == "signed_in"
        assert (page.view()["name"], page.view()["tag"]) == ("Sora", "7687")


def test_a_refused_name_falls_back(tmp_path, clock):
    refused = answer(400, {"code": "P0001", "details": None, "hint": None, "message": "invalid_name"})
    cloud = _signing_in((ENSURE, [refused, answer(200, {"name": "Traveler", "discriminator": "0031"})]))
    with Page(tmp_path, clock, cloud) as page:
        page.sign_in()
        view = page.view()
    assert (view["state"], view["name"], view["tag"]) == ("signed_in", "Traveler", "0031")
    assert [json.loads(body) for _, path, _, body in cloud.requests if "ensure_profile" in path] == [
        {"p_name": "Sora"}, {"p_name": "Traveler"},
    ]


def test_a_name_with_nothing_that_fits_starts_as_the_fallback(tmp_path, clock):
    cloud = _signing_in((EXCHANGE, token_answer(metadata={"custom_claims": {"global_name": "😀😀"}})))
    with Page(tmp_path, clock, cloud) as page:
        page.sign_in()
    assert cloud.body(1) == {"p_name": "Traveler"}


@pytest.mark.parametrize("failure", [
    answer(503), answer(400, {"message": "something_else"}), urllib.error.URLError("no route"),
])
def test_a_profile_that_could_not_be_made_leaves_no_half_sign_in(tmp_path, clock, failure):
    with Page(tmp_path, clock, _signing_in((ENSURE, failure))) as page:
        page.client.post("/api/online/sign-in")
        response = page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
        view = page.view()
        assert response.status_code == 400
        assert NOT_FINISHED in response.text
        assert (view["state"], view["message"]) == ("signed_out", "Signing in did not finish. Try again.")
        assert not page.cloud_bin.exists()


@pytest.mark.parametrize("failure", [
    answer(503), answer(403, {"error_code": "flow_state_expired"}), urllib.error.URLError("no route"),
    answer(200, {"no": "tokens"}), answer(200, {"access_token": "a", "refresh_token": "b", "expires_in": 60}),
])
def test_an_exchange_that_failed_keeps_nothing(tmp_path, clock, failure):
    with Page(tmp_path, clock, _signing_in((EXCHANGE, failure))) as page:
        page.client.post("/api/online/sign-in")
        response = page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
        assert response.status_code == 400
        assert page.view()["state"] == "signed_out"
        assert not page.cloud_bin.exists()


def test_the_view_holds_no_token(page):
    page.sign_in()
    for text in (page.client.get("/api/online").text, page.client.get("/api/status").text):
        assert MADE_UP_ACCESS not in text and MADE_UP_REFRESH not in text


def test_while_signed_in_a_new_sign_in_does_nothing(page):
    page.sign_in()
    page.opened.clear()
    assert page.client.post("/api/online/sign-in").json()["state"] == "signed_in"
    assert page.opened == []


def test_sign_out_asks_the_server_and_removes_the_file(tmp_path, clock):
    with Page(tmp_path, clock, _signing_in((LOGOUT, answer(204)))) as page:
        page.sign_in()
        assert page.cloud_bin.exists()
        view = page.client.post("/api/online/sign-out").json()
        assert view["state"] == "signed_out" and view["name"] is None
        assert page.cloud.sent()[-1] == "POST /auth/v1/logout?scope=local"
        assert page.cloud.requests[-1][2]["Authorization"] == f"Bearer {MADE_UP_ACCESS}"
        assert not page.cloud_bin.exists()
        sent = len(page.cloud.requests)
        assert page.view()["state"] == "signed_out"
        assert len(page.cloud.requests) == sent


@pytest.mark.parametrize("failure", [
    answer(503), urllib.error.URLError("no route"), answer(403, {"error_code": "session_not_found"}),
])
def test_sign_out_offline_still_removes_the_file(tmp_path, clock, failure):
    with Page(tmp_path, clock, _signing_in((LOGOUT, failure))) as page:
        page.sign_in()
        view = page.client.post("/api/online/sign-out").json()
        assert view["state"] == "signed_out"
        assert view["message"] is None and view["offline"] is False
        assert not page.cloud_bin.exists()


def test_sign_out_with_a_lost_session_removes_the_file(tmp_path, clock):
    _saved(tmp_path, clock, left=-1)
    with Page(tmp_path, clock, FakeCloud({REFRESH: answer(400, {"error_code": "refresh_token_not_found"})})) as page:
        page.client.portal.call(page.core.online.look)
        assert page.view()["state"] == "sign_in_again"
        assert page.client.post("/api/online/sign-out").json()["state"] == "signed_out"
        assert not page.cloud_bin.exists()


def test_sign_out_ends_a_sign_in_that_was_waiting(tmp_path, clock):
    _saved(tmp_path, clock, left=-1)
    cloud = _signing_in((REFRESH, answer(400, {"error_code": "refresh_token_not_found"})))
    with Page(tmp_path, clock, cloud) as page:
        page.client.portal.call(page.core.online.look)
        page.client.post("/api/online/sign-in")
        nonce = page.nonce()
        assert page.client.post("/api/online/sign-out").json()["state"] == "signed_out"
        assert page.client.get(f"/auth/callback/{nonce}", params={"code": "THE-CODE-0123456789"}).status_code == 400
    assert not [line for line in cloud.sent() if "grant_type=pkce" in line]


def test_sign_out_clears_what_a_failed_delete_said(tmp_path, clock):
    with Page(tmp_path, clock, _signing_in((DELETE, answer(503)), (LOGOUT, answer(204)))) as page:
        page.sign_in()
        assert page.client.post("/api/online/delete", json={"confirm": True}).json()["offline"] is True
        view = page.client.post("/api/online/sign-out").json()
        assert (view["state"], view["message"], view["offline"]) == ("signed_out", None, False)


def test_sign_out_while_signed_out_sends_nothing(tmp_path, clock):
    cloud = FakeCloud()
    with Page(tmp_path, clock, cloud) as page:
        assert page.client.post("/api/online/sign-out").json()["state"] == "signed_out"
    assert cloud.requests == []


def test_a_lost_session_says_sign_in_again_and_keeps_local_data(tmp_path, clock):
    session = _saved(tmp_path, clock, left=-1)
    with Page(tmp_path, clock, FakeCloud({REFRESH: answer(400, {"error_code": "refresh_token_not_found"})})) as page:
        page.client.portal.call(page.core.online.look)
        view = page.view()
        assert view["state"] == "sign_in_again"
        assert (view["name"], view["tag"]) == ("Sora", "7687")
        assert CloudStore(page.cloud_bin).load() == session  # the file is left until the player signs in again
        for url in ("/api/status", "/api/backlog", "/api/stats", "/api/settings"):
            assert page.client.get(url).status_code == 200


def test_signing_in_again_replaces_a_lost_session(tmp_path, clock):
    _saved(tmp_path, clock, left=-1)
    cloud = _signing_in((REFRESH, answer(400, {"error_code": "refresh_token_not_found"})))
    with Page(tmp_path, clock, cloud) as page:
        page.client.portal.call(page.core.online.look)
        assert page.client.post("/api/online/sign-in").json()["state"] == "waiting"
        page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
        assert page.view()["state"] == "signed_in"


def test_a_failed_sign_in_again_stays_as_it_was(tmp_path, clock):
    session = _saved(tmp_path, clock, left=-1)
    cloud = _signing_in(
        (REFRESH, answer(400, {"error_code": "refresh_token_not_found"})), (EXCHANGE, urllib.error.URLError("no route")),
    )
    with Page(tmp_path, clock, cloud) as page:
        page.client.portal.call(page.core.online.look)
        page.client.post("/api/online/sign-in")
        page.client.get(f"/auth/callback/{page.nonce()}", params={"code": "THE-CODE-0123456789"})
        view = page.view()
        assert (view["state"], view["message"]) == ("sign_in_again", "Signing in did not finish. Try again.")
        assert CloudStore(page.cloud_bin).load() == session


@pytest.mark.parametrize("body", [{}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {"other": True}])
def test_delete_needs_confirm(tmp_path, clock, body):
    with Page(tmp_path, clock, _signing_in((DELETE, answer(204)))) as page:
        page.sign_in()
        assert page.client.post("/api/online/delete", json=body).status_code == 422
        assert page.cloud_bin.exists()
        assert page.view()["state"] == "signed_in"
    assert not [line for line in page.cloud.sent() if "delete_my_account" in line]


def test_delete_needs_confirm_and_removes_the_file(tmp_path, clock):
    with Page(tmp_path, clock, _signing_in((DELETE, answer(204)))) as page:
        page.sign_in()
        view = page.client.post("/api/online/delete", json={"confirm": True}).json()
        assert view["state"] == "signed_out" and view["name"] is None
        assert page.cloud.sent()[-1] == "POST /rest/v1/rpc/delete_my_account"
        assert not page.cloud_bin.exists()


@pytest.mark.parametrize("failure, says, offline", [
    (answer(503), "Your online data was not deleted, because the server could not be reached. Try again later.", True),
    (urllib.error.URLError("no route"), "Your online data was not deleted, because the server could not be reached. Try again later.", True),
    (answer(403, {"message": "permission denied"}), "Your online data was not deleted. Try again later.", False),
])
def test_a_delete_that_fails_keeps_the_session_and_says_so(tmp_path, clock, failure, says, offline):
    with Page(tmp_path, clock, _signing_in((DELETE, failure))) as page:
        page.sign_in()
        view = page.client.post("/api/online/delete", json={"confirm": True}).json()
        assert (view["state"], view["message"], view["offline"]) == ("signed_in", says, offline)
        assert page.cloud_bin.exists()


def test_a_delete_with_a_lost_session_asks_to_sign_in_again(tmp_path, clock):
    _saved(tmp_path, clock, left=-1)
    with Page(tmp_path, clock, FakeCloud({REFRESH: answer(400, {"error_code": "refresh_token_not_found"})})) as page:
        view = page.client.post("/api/online/delete", json={"confirm": True}).json()
        assert (view["state"], view["message"]) == ("sign_in_again", "Your online data was not deleted. Sign in again first.")
        assert page.cloud_bin.exists()


def test_sync_now_reads_the_account_and_sends_what_waits(tmp_path, clock):
    # Settings' Sync now (the online plan, Task C4): at once, whatever the time since the last, and the answer is the view.
    cloud = FakeCloud({
        PROFILE: answer(200, [{"name": "Sora", "discriminator": "7687", "uid": None, "pins": [], "sharing": []}]),
        ("GET", "/rest/v1/sync_rows"): answer(200, []), ("POST", "/rest/v1/rpc/sync_push"): answer(200, 1),
    })
    _saved(tmp_path, clock)
    with Page(tmp_path, clock, cloud) as page:
        page.core.store.put_layout("board", "[]", 1, clock.now())
        before = page.view()["sync"]
        assert before == {"on": True, "last_at": None, "needs_choice": False}
        reply = page.client.post("/api/online/sync")
        assert reply.status_code == 200
        assert reply.json()["sync"] == {"on": True, "last_at": clock.now().isoformat(), "needs_choice": False}
        assert page.core.store.sync_outbox(10) == []
    assert [line.split("?")[0] for line in cloud.sent() if "sync_" in line] == [
        "GET /rest/v1/sync_rows", "GET /rest/v1/sync_rows", "POST /rest/v1/rpc/sync_push",  # the first look, the read, the send
    ]
    pushed = [row["kind"] for row in cloud.body(-1)["p_rows"]]
    assert "layouts" in pushed and pushed.count("settings") == 8  # an empty account takes this PC's data without asking


def test_sync_now_signed_out_sends_nothing(page):
    assert page.client.post("/api/online/sync").json()["sync"] is None
    assert page.cloud.requests == []


def test_the_demo_is_signed_in_and_never_sends_a_request(tmp_path, clock):
    cloud = FakeCloud()
    opened: list[str] = []
    core = _core(tmp_path, clock, cloud, opened, demo=True)
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.portal.call(core.start)
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        view = client.get("/api/online").json()
        assert view["state"] == "signed_in" and view["name"] and view["tag"]
        assert client.get("/api/status").json()["online"] == view
        # The buttons work on the made-up account, so that each state can be looked at, and nothing leaves the PC.
        assert client.post("/api/online/sign-out").json()["state"] == "signed_out"
        assert client.post("/api/online/sign-in").json()["state"] == "signed_in"
        assert client.post("/api/online/delete", json={"confirm": True}).json()["state"] == "signed_out"
        assert client.post("/api/online/delete", json={}).status_code == 422
        assert client.post("/api/online/sync").json()["sync"] is None  # there is nothing to sync with
        assert client.get("/auth/callback/any", params={"code": "x"}).status_code == 400
        client.portal.call(core.stop)
    assert cloud.requests == [] and opened == []
    assert not (tmp_path / "data" / "cloud.bin").exists()
