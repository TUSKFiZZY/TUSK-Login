"""Invite links: opening one shows who it is from and sends nothing, because the inviter accepts their own invite and a click
alone would hand a stranger the player's shared stats (the online plan, Task B4; Review Focus 3)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cloud_fakes import USER_ID, FakeCloud, answer, token_answer
from guildboard import netpolicy
from guildboard.api.app import create_app
from guildboard.cloud import INVITE, invite_code
from guildboard.cloud.models import OnlineRefused
from guildboard.cloud.service import Online
from guildboard.cloud.session import CloudSession, CloudStore
from guildboard.config import AppPaths
from guildboard.core import Core
from guildboard.events import EventBus
from support import AccountSource, FakeSleep

PEEK = ("POST", "/rest/v1/rpc/peek_invite")
REDEEM = ("POST", "/rest/v1/rpc/redeem_invite")
MAKE = ("POST", "/rest/v1/rpc/make_invite")
OVERVIEW = ("POST", "/rest/v1/rpc/friends_overview")
EXCHANGE = ("POST", "/auth/v1/token?grant_type=pkce")
ENSURE = ("POST", "/rest/v1/rpc/ensure_profile")
LOGOUT = ("POST", "/auth/v1/logout")
CODE = "abcDEF0123456789_-abcdef"
CARD = {"name": "Nova", "tag": "5120", "has_picture": False, "ar": 41}
NOBODY = {"friends": [], "incoming": [], "outgoing": []}
GONE = "This invite was used or has expired. Ask for a new one."
ROOT = Path(__file__).resolve().parents[1]
PORT = 4321
BASE = f"http://127.0.0.1:{PORT}"


def _online(tmp_path, clock, cloud, *, signed_in: bool = True, opened=None) -> Online:
    store = CloudStore(tmp_path / "cloud.bin")
    if signed_in:
        store.save(CloudSession("ACCESS-0123456789", "REFRESH-0123456789", clock.now().timestamp() + 3600, USER_ID, "Elena", "0031"))
    return Online(store, EventBus(), clock, fetch=cloud, open_browser=(opened if opened is not None else []).append)


def _core(tmp_path, clock, cloud) -> Core:
    paths = AppPaths(tmp_path / "data")
    paths.ensure()
    CloudStore(paths.cloud_path).save(
        CloudSession("ACCESS-0123456789", "REFRESH-0123456789", clock.now().timestamp() + 3600, USER_ID, "Elena", "0031")
    )
    return Core(paths, demo=False, clock=clock, source=AccountSource(), sleep=FakeSleep(clock), process_lister=lambda: set(),
                cloud_fetch=cloud, open_browser=lambda address: None)


async def test_a_link_is_the_functions_address(tmp_path, clock):
    online = _online(tmp_path, clock, FakeCloud({MAKE: answer(200, CODE)}))
    assert await online.invite_link() == f"https://{netpolicy.CLOUD_HOST}/functions/v1/invite?c={CODE}"


async def test_opening_a_link_sends_nothing(tmp_path, clock):
    cloud = FakeCloud({PEEK: answer(200, CARD)})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    assert cloud.sent() == ["POST /rest/v1/rpc/peek_invite"] and cloud.body() == {"p_code": CODE}
    assert online.view()["invite"] == CARD


async def test_send_redeems_once(tmp_path, clock):
    cloud = FakeCloud({PEEK: answer(200, CARD), REDEEM: answer(200, "sent"), OVERVIEW: answer(200, NOBODY)})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    model = await online.send_invite()
    assert set(model) == {"me", "friends", "incoming", "outgoing", "read_at", "stale"}
    assert cloud.sent().count("POST /rest/v1/rpc/redeem_invite") == 1
    assert online.view()["invite"] is None
    with pytest.raises(OnlineRefused, match="This invite was used"):
        await online.send_invite()  # a second press has nothing to send
    assert cloud.sent().count("POST /rest/v1/rpc/redeem_invite") == 1


async def test_dropping_an_invite_sends_nothing(tmp_path, clock):
    cloud = FakeCloud({PEEK: answer(200, CARD)})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    online.drop_invite()
    assert online.view()["invite"] is None and len(cloud.requests) == 1


async def test_a_used_code_says_ask_for_a_new_one(tmp_path, clock):
    online = _online(tmp_path, clock, FakeCloud({PEEK: answer(400, {"message": "invite_gone"})}))
    await online.offer_invite(CODE)
    view = online.view()
    assert (view["invite"], view["message"]) == (None, GONE)


async def test_what_was_said_of_a_used_code_goes_when_it_is_dropped(tmp_path, clock):
    online = _online(tmp_path, clock, FakeCloud({PEEK: answer(400, {"message": "invite_gone"})}))
    await online.offer_invite(CODE)
    online.drop_invite()
    assert online.view()["message"] is None


async def test_a_code_spent_between_the_look_and_the_press_says_so(tmp_path, clock):
    cloud = FakeCloud({PEEK: answer(200, CARD), REDEEM: answer(400, {"message": "invite_gone"})})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    with pytest.raises(OnlineRefused, match="This invite was used"):
        await online.send_invite()
    assert online.view()["invite"] is None


async def test_a_send_with_the_server_away_keeps_the_invite(tmp_path, clock):
    cloud = FakeCloud({PEEK: answer(200, CARD), REDEEM: [answer(503), answer(200, "sent")], OVERVIEW: answer(200, NOBODY)})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    with pytest.raises(OnlineRefused) as refused:
        await online.send_invite()
    assert refused.value.status == 503 and online.view()["invite"] == CARD
    await online.send_invite()
    assert online.view()["invite"] is None


async def test_a_send_with_the_session_lost_keeps_the_invite(tmp_path, clock):
    # After a lost session the list stays, and its invite with it: the code isn't spent, so it waits for the sign-in.
    cloud = FakeCloud({PEEK: answer(200, CARD), REDEEM: answer(400, {"message": "not_signed_in"})})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    with pytest.raises(OnlineRefused, match="Sign in again first"):
        await online.send_invite()
    assert online.view()["state"] == "sign_in_again" and online.view()["invite"] == CARD


async def test_a_press_before_the_server_said_who_it_is_from_sends_nothing(tmp_path, clock):
    # The page has shown nobody yet, and the inviter accepts their own invite: this press can't be the player's answer.
    cloud = FakeCloud({PEEK: answer(503)})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    with pytest.raises(OnlineRefused, match="This invite was used"):
        await online.send_invite()
    assert "POST /rest/v1/rpc/redeem_invite" not in cloud.sent()


async def test_a_link_with_a_lost_session_waits_for_the_sign_in(tmp_path, clock):
    cloud = FakeCloud({("GET", "/rest/v1/profiles"): answer(200, [])})  # the account was deleted on another device
    online = _online(tmp_path, clock, cloud)
    await online.look()
    sent = len(cloud.requests)
    await online.offer_invite(CODE)
    assert len(cloud.requests) == sent and online.view()["invite"] is None


async def test_a_look_with_the_server_away_comes_again_on_the_next_turn(tmp_path, clock):
    cloud = FakeCloud({
        PEEK: [answer(503), answer(200, CARD)], OVERVIEW: answer(200, NOBODY),
        ("GET", "/rest/v1/profiles"): answer(200, [{"name": "Elena", "discriminator": "0031"}]),
    })
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    assert online.view()["invite"] is None and online.view()["offline"] is True
    clock.advance(minutes=5)
    await online.turn()
    assert online.view()["invite"] == CARD and online.view()["offline"] is False


async def test_a_link_while_signed_out_waits_for_the_sign_in(tmp_path, clock):
    cloud = FakeCloud({
        PEEK: answer(200, CARD), EXCHANGE: token_answer(), ENSURE: answer(200, {"name": "Elena", "discriminator": "0031"}),
    })
    opened: list[str] = []
    online = _online(tmp_path, clock, cloud, signed_in=False, opened=opened)
    await online.offer_invite(CODE)
    assert cloud.requests == [] and online.view()["invite"] is None

    online.begin_sign_in(4321)
    nonce = opened[0].rsplit("%2F", 1)[-1].split("&", 1)[0]
    assert await online.finish_sign_in(nonce, "THE-CODE-0123456789") is True
    assert online.view()["invite"] == CARD
    assert "POST /rest/v1/rpc/redeem_invite" not in cloud.sent()  # nothing is sent before the player presses


async def test_signing_out_forgets_the_invite(tmp_path, clock):
    cloud = FakeCloud({PEEK: answer(200, CARD), LOGOUT: answer(204)})
    online = _online(tmp_path, clock, cloud)
    await online.offer_invite(CODE)
    await online.sign_out()
    assert online.view()["invite"] is None


@pytest.mark.parametrize("link", [
    "guildboard://invite/../x", "guildboard://invite/short", "guildboard://invite/" + "a" * 65,
    "guildboard://evil/abcDEF0123456789", "https://evil.example/invite/abcDEF0123456789",
    "guildboard://invite/abcDEF0123456789/more", "guildboard://invite/abcDEF0123456789?x=1", "guildboard://invite/abc%2F0123456789012",
    "guildboard://invite/", "", "xguildboard://invite/abcDEF0123456789",
])
def test_only_a_well_formed_link_is_taken(link):
    assert invite_code(link) is None


def test_a_well_formed_link_gives_its_code():
    assert invite_code(f"guildboard://invite/{CODE}") == CODE
    assert invite_code("guildboard://invite/" + "a" * 16) == "a" * 16
    assert invite_code("guildboard://invite/" + "a" * 64) == "a" * 64
    assert INVITE.pattern == r"guildboard://invite/([A-Za-z0-9_-]{16,64})"


def test_the_routes_make_send_and_drop(tmp_path, clock):
    cloud = FakeCloud({MAKE: answer(200, CODE), PEEK: answer(200, CARD), REDEEM: answer(200, "sent"), OVERVIEW: answer(200, NOBODY)})
    core = _core(tmp_path, clock, cloud)
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        reply = client.post("/api/friends/invite-link")
        assert reply.json() == {"link": f"https://{netpolicy.CLOUD_HOST}/functions/v1/invite?c={CODE}"}

        client.portal.call(core.online.offer_invite, CODE)
        assert client.get("/api/online").json()["invite"] == CARD
        assert client.delete("/api/friends/invite").json()["invite"] is None  # the view: it works signed out too

        client.portal.call(core.online.offer_invite, CODE)
        assert set(client.post("/api/friends/invite").json()) == {"me", "friends", "incoming", "outgoing", "read_at", "stale"}
        assert cloud.sent().count("POST /rest/v1/rpc/redeem_invite") == 1

        reply = client.post("/api/friends/invite")
        assert (reply.status_code, reply.json()) == (409, {"error": GONE})
    core.store.close()


def test_the_launch_link_can_open_friends(tmp_path, clock):
    core = _core(tmp_path, clock, FakeCloud())
    with TestClient(create_app(core, PORT), base_url=BASE) as client:
        reply = client.get("/launch", params={"k": core.launch_secret, "to": "friends"}, follow_redirects=False)
    core.store.close()
    assert reply.headers["location"] == "/#/friends"


def test_the_edge_function_is_the_one_deployed():
    code = (ROOT / "cloud" / "functions" / "invite" / "index.ts").read_text(encoding="utf-8")
    assert 'const CODE = /^[A-Za-z0-9_-]{16,64}$/;' in code
    assert "Location: `guildboard://invite/${code}`" in code and "status: 302" in code and "status: 404" in code


def test_the_installer_registers_the_link_and_removes_it():
    recipe = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert 'Root: HKCU; Subkey: "Software\\Classes\\guildboard"; ValueType: string; ValueName: ""; ValueData: "URL:TUSKFiZZY"; Flags: uninsdeletekey' in recipe
    assert 'ValueName: "URL Protocol"; ValueData: ""' in recipe
    assert 'Subkey: "Software\\Classes\\guildboard\\shell\\open\\command"; ValueType: string; ValueName: ""; ValueData: """{app}\\tuskfizzy-app.exe"" -- ""%1"""' in recipe
    assert "HKLM" not in recipe  # the current user's own classes, and no administrator rights
