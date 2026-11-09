import asyncio
import ctypes
import hashlib
import io
import json
import logging
import threading
import urllib.request

import pytest
from fastapi.testclient import TestClient

from guildboard import netpolicy, selfupdate
from guildboard.api.app import create_app
from guildboard.changes import load_changes
from guildboard.config import AppPaths
from guildboard.core import Core
from guildboard.events import EventBus
from guildboard.selfupdate import (
    Release, Updater, newer_release, parse_notes, parse_version, release_text, start_installer,
)
from support import FakeSleep

ASSET = "https://api.github.com/repos/TUSKFiZZY/TUSK-Login/releases/assets/123"
SIGNATURE_ASSET = "https://api.github.com/repos/TUSKFiZZY/TUSK-Login/releases/assets/124"
NAME = "TUSKFiZZY.Setup.0.2.0.exe"  # GitHub turns the spaces of an uploaded file's name into dots
INSTALLER = b"MZ installer"
SHA = hashlib.sha256(INSTALLER).hexdigest()


class ReleaseKey:
    """A throwaway key on P-256, made by Windows' own CNG, that signs test releases as release.ps1 signs real ones:
    ECDSA over the SHA-256 of the text, as r then s. Its public half is in the form the app pins."""

    def __init__(self) -> None:
        self._bcrypt = ctypes.WinDLL("bcrypt")
        self._algorithm, self._key = ctypes.c_void_p(), ctypes.c_void_p()
        assert self._bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(self._algorithm), "ECDSA_P256", None, 0) == 0
        assert self._bcrypt.BCryptGenerateKeyPair(self._algorithm, ctypes.byref(self._key), 256, 0) == 0
        assert self._bcrypt.BCryptFinalizeKeyPair(self._key, 0) == 0
        blob, size = ctypes.create_string_buffer(72), ctypes.c_ulong()
        assert self._bcrypt.BCryptExportKey(self._key, None, "ECCPUBLICBLOB", blob, 72, ctypes.byref(size), 0) == 0
        self.public = "04" + blob.raw[8:72].hex()  # after the blob's magic and the length of a coordinate: x, then y

    def sign(self, text: bytes) -> bytes:
        digest = hashlib.sha256(text).digest()
        signature, size = ctypes.create_string_buffer(64), ctypes.c_ulong()
        assert self._bcrypt.BCryptSignHash(self._key, None, digest, 32, signature, 64, ctypes.byref(size), 0) == 0
        return signature.raw


KEY, OTHER_KEY = ReleaseKey(), ReleaseKey()
SERVED: dict[str, bytes] = {SHA: INSTALLER}  # what each asset's address gives, by its checksum


def _serve(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    SERVED[digest] = data
    return digest


def _release(tag="v0.2.0", *, key=None, signs=None, signature=True, **asset) -> bytes:
    """GitHub's answer for a release: its installer and, unless `signature` is False, the installer's signature by
    `key` (KEY) over `signs`, by default the release's own facts, as release.ps1 signs them."""
    installer = {"name": NAME, "url": ASSET, "size": len(INSTALLER), "digest": f"sha256:{SHA}"}
    installer.update(asset)
    assets = [installer]
    if signature:
        text = signs if signs is not None else release_text(tag, installer["name"], installer["size"], SHA)
        signed = (key or KEY).sign(text)
        assets.append({
            "name": f"{installer['name']}.sig", "url": SIGNATURE_ASSET, "size": len(signed),
            "digest": f"sha256:{_serve(signed)}",
        })
    return json.dumps({"tag_name": tag, "draft": False, "prerelease": False, "assets": assets}).encode()


def _signature_asset(url: str, size: int, sha256: str) -> bytes:
    assert (url, size) == (SIGNATURE_ASSET, 64)
    return SERVED[sha256]


def _offered(body: bytes, current: str = "0.1.0", *, key: str | None = None) -> Release | None:
    return newer_release(body, current, signature=_signature_asset, key=KEY.public if key is None else key)


def test_versions_are_read_from_the_tag():
    assert parse_version("v0.2.0") == (0, 2, 0)
    assert parse_version("1.10.3") == (1, 10, 3)
    assert parse_version("v0.2") is None
    assert parse_version("latest") is None


def test_only_a_newer_release_with_a_checked_installer_counts():
    assert _offered(_release()) == Release("0.2.0", ASSET, len(INSTALLER), SHA)
    assert _offered(_release("v0.10.0"), "0.9.0") is not None  # compared as numbers, not as text
    assert _offered(_release(), "0.2.0") is None
    assert _offered(_release(), "0.3.0") is None
    assert _offered(_release(digest="")) is None  # no checksum, no install
    assert _offered(_release(url="https://example.com/setup.exe")) is None
    assert _offered(_release(name="notes.txt")) is None
    assert _offered(_release(size=0)) is None
    draft = json.loads(_release())
    draft["prerelease"] = True
    assert _offered(json.dumps(draft).encode()) is None


def test_a_release_is_offered_only_with_a_signature_by_the_pinned_key():
    # H4: the installer's address, size and checksum all come from GitHub's one answer, so they prove only that the file
    # is the one GitHub lists. The release key's signature over the answer's own facts proves the player published it.
    good = Release("0.2.0", ASSET, len(INSTALLER), SHA)
    assert _offered(_release()) == good
    assert _offered(_release(signature=False)) is None  # no signature
    assert _offered(_release(key=OTHER_KEY)) is None  # another key's
    assert _offered(_release(signs=release_text("v0.2.0", NAME, len(INSTALLER), "0" * 64))) is None  # another file's
    assert _offered(_release(signs=release_text("v0.1.9", NAME, len(INSTALLER), SHA))) is None  # an older version's
    assert _offered(_release(signs=release_text("v0.2.0", "other.exe", len(INSTALLER), SHA))) is None
    assert _offered(_release(signs=release_text("v0.2.0", NAME, len(INSTALLER) + 1, SHA))) is None
    # A signature that is not beside the installer, by its name, or is not one in size, counts as none.
    renamed = json.loads(_release())
    renamed["assets"][1]["name"] = "Other.exe.sig"
    assert _offered(json.dumps(renamed).encode()) is None
    longer = json.loads(_release())
    longer["assets"][1]["size"] = 65
    assert _offered(json.dumps(longer).encode()) is None
    # Only the key given counts: the pinned one unless a test gives another, and none while none was made.
    assert _offered(_release(), key=OTHER_KEY.public) is None
    assert _offered(_release(), key="") is None
    assert newer_release(_release(), "0.1.0", signature=_signature_asset) is None  # the test key is not the release key
    # A release that is not newer is not offered, and its signature is not downloaded.
    asked = []
    assert newer_release(_release(), "0.2.0", signature=lambda *asset: asked.append(asset), key=KEY.public) is None
    assert asked == []


def test_a_copy_without_a_release_key_offers_no_update_and_downloads_nothing(caplog):
    # A copy built while no key was pinned can check no signature. It says so in its own words, not as a release that is
    # not signed, and downloads no signature it could not check.
    asked = []
    for key in ("", "04" + "0" * 10, KEY.public.upper()):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="guildboard.selfupdate"):
            assert newer_release(_release(), "0.1.0", signature=lambda *asset: asked.append(asset), key=key) is None
        assert [record.getMessage() for record in caplog.records] == [
            "This copy has no release key, so it offers no update"
        ]
    assert asked == []
    # A copy with a key, and a release signed by another, is the other case.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="guildboard.selfupdate"):
        assert newer_release(_release(key=OTHER_KEY), "0.1.0", signature=_signature_asset, key=KEY.public) is None
    assert [record.getMessage() for record in caplog.records] == [
        "The release 0.2.0 is not signed by the release key, so it is not offered"
    ]


def test_the_signature_check_is_standard_ecdsa_on_p256():
    # RFC 6979, A.2.5: a published key and its signatures with SHA-256, made by no code of Guild Board's or of Windows'.
    # The check takes the key as its uncompressed point and the signature as r then s.
    key = (
        "04" "60fed4ba255a9d31c961eb74c6356d68c049b8923b61fa6ce669622e60f29fb6"
        "7903fe1008b8bc99a41ae9e95628bc64f2f1b20c2d7e9f5177a3c294d4462299"
    )
    sample = bytes.fromhex(
        "efd48b2aacb6a8fd1140dd9cd45e81d69d2c877b56aaf991c34d0ea84eaf3716"
        "f7cb1c942d657c41d436c7a1b6e29f65f3e900dbb9aff4064dc4ab2f843acda8"
    )
    test = bytes.fromhex(
        "f1abb023518351cd71d881567b1ea663ed3efcf6c5132b354f28d3b0b7d38367"
        "019f4113742a2b14bd25926b49c649155f267e60d3814b4c0cc84250e46f0083"
    )
    assert selfupdate.signed(b"sample", sample, key) and selfupdate.signed(b"test", test, key)
    assert not selfupdate.signed(b"sample", test, key) and not selfupdate.signed(b"test", sample, key)
    assert not selfupdate.signed(b"sample", sample[32:] + sample[:32], key)  # s then r is no signature
    assert not selfupdate.signed(b"sample", sample, key.upper()) and not selfupdate.signed(b"sample", sample, key[2:])


def _download(*, installer_fails: bool = False):
    """Stands in for netpolicy.download_installer: each asset's address gives what SERVED holds for its checksum."""

    def download(url, target, *, size, sha256, cancel, progress=None):
        if url == ASSET:
            assert (size, sha256) == (len(INSTALLER), SHA)
            if installer_fails:
                raise ConnectionError("offline")
        else:
            assert (url, size) == (SIGNATURE_ASSET, 64)
        target.write_bytes(SERVED[sha256])
        if progress is not None:
            progress(size)

    return download


def _updater(tmp_path, bus=None, *, fetch=lambda: _release(), download=None, launched=None):
    started = launched if launched is not None else []
    return Updater(
        tmp_path / "updates", bus or EventBus(), enabled=True, fetch=fetch, download=download or _download(),
        launch=lambda path, sha256: started.append((path, sha256)), current="0.1.0", key=KEY.public,
    )


async def test_an_unsigned_release_is_never_offered(tmp_path):
    def download(url, target, **kwargs):
        target.write_bytes(INSTALLER)

    unsigned = _release(signature=False)
    updater = Updater(
        tmp_path / "updates", EventBus(), enabled=True, fetch=lambda: unsigned, download=download, current="0.1.0",
    )
    await updater.check()
    assert updater.state == "none" and updater.release is None
    assert not updater.install()


async def test_the_signature_is_downloaded_as_the_installer_is_and_not_kept(tmp_path):
    asked = []
    download = _download()
    updater = _updater(tmp_path, download=lambda url, target, **kwargs: (asked.append(url), download(url, target, **kwargs)))
    await updater.check()
    assert updater.state == "available"
    assert asked == [SIGNATURE_ASSET]  # through netpolicy's own way for the repository's assets
    assert list((tmp_path / "updates").iterdir()) == []


async def test_a_newer_release_is_offered_and_installed_on_the_press(tmp_path):
    bus = EventBus()
    heard = bus.subscribe()
    launched = []
    updater = _updater(tmp_path, bus, launched=launched)
    assert updater.view() == {
        "state": "none", "version": None, "progress": None, "message": None, "notes": [], "released": None,
    }
    await updater.check()
    assert updater.view() == {
        "state": "available", "version": "0.2.0", "progress": None, "message": None, "notes": [], "released": None,
    }
    assert heard.get_nowait()["type"] == "update_changed"
    assert updater.install()
    assert not updater.install()  # one at a time
    await updater._install_task
    assert updater.state == "installing"
    # The installer starts only with the checksum its signed release names, to be checked once more.
    assert launched == [(tmp_path / "updates" / "TUSKFiZZY Setup 0.2.0.exe", SHA)]


async def test_a_check_that_ends_during_an_install_leaves_the_install_alone(tmp_path):
    entered, go_on = threading.Event(), threading.Event()

    def slow_fetch():
        entered.set()
        go_on.wait(10)
        return _release()

    updater = _updater(tmp_path)
    await updater.check()
    updater._fetch = slow_fetch
    checking = asyncio.create_task(updater.check())
    await asyncio.to_thread(entered.wait, 10)
    assert updater.install()  # the player presses Update while the check is under way
    go_on.set()
    await checking
    assert updater.state in ("downloading", "installing")
    await updater._install_task
    assert updater.state == "installing"


async def test_a_failed_download_can_be_tried_again(tmp_path):
    updater = _updater(tmp_path, download=_download(installer_fails=True))
    await updater.check()
    updater.install()
    await updater._install_task
    assert updater.state == "failed"
    assert "try again" in updater.message
    await updater.check()  # the same release found again keeps the failure on screen
    assert updater.state == "failed"
    assert updater.install()
    await updater._install_task


async def test_nothing_is_offered_when_the_check_fails_or_finds_nothing_newer(tmp_path):
    def offline():
        raise OSError("offline")

    for fetch in (offline, lambda: _release("v0.1.0"), lambda: b"not json"):
        updater = _updater(tmp_path, fetch=fetch)
        await updater.check()
        assert updater.state == "none"
        assert not updater.install()


async def test_a_checkout_and_the_demo_never_ask(tmp_path):
    def must_not_ask():
        raise AssertionError("asked")

    updater = Updater(tmp_path / "updates", EventBus(), enabled=False, fetch=must_not_ask)
    await updater.run()
    core = Core(AppPaths(tmp_path / "data"), demo=True, process_lister=lambda: set())
    try:
        assert core.updater.enabled is False
        assert core.status()["update"]["state"] == "none"
    finally:
        core.store.close()


async def test_old_installers_are_removed_at_the_start(tmp_path):
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / "TUSKFiZZY Setup 0.1.0.exe").write_bytes(b"MZ")
    (folder / "release.sig").write_bytes(b"left by a start that ended while it checked")
    sleep = asyncio.Event()

    async def wait(seconds):
        await sleep.wait()

    updater = Updater(folder, EventBus(), enabled=True, fetch=lambda: _release(), sleep=wait)
    task = asyncio.create_task(updater.run())
    await asyncio.sleep(0)
    assert list(folder.iterdir()) == []
    task.cancel()


def test_the_api_starts_the_install_only_when_there_is_one(tmp_path, clock):
    core = Core(AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set())
    launched = []
    core.updater = _updater(tmp_path, core.bus, launched=launched)
    with TestClient(create_app(core, 4321), base_url="http://127.0.0.1:4321") as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        refused = client.post("/api/update/install")
        assert refused.status_code == 409
        assert refused.json() == {"error": "There is no update to install."}
        client.portal.call(core.updater.check)
        answer = client.post("/api/update/install")
        assert answer.status_code == 200
        assert answer.json()["update"]["version"] == "0.2.0"
        for _ in range(200):
            if launched:
                break
            client.portal.call(asyncio.sleep, 0.01)
        assert launched and client.get("/api/status").json()["update"]["state"] == "installing"


# ------------------------------------------------------------------ the network side, in netpolicy


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_the_latest_release_is_asked_of_its_one_address(monkeypatch):
    sent = []
    monkeypatch.setattr(netpolicy, "_open", lambda request, timeout: sent.append(request) or _Answer(b"{}"))
    assert netpolicy.fetch_latest_release() == b"{}"
    assert sent[0].full_url == "https://api.github.com/repos/TUSKFiZZY/TUSK-Login/releases/latest"
    assert sent[0].follow_redirects is False
    assert sent[0].get_header("User-agent") == "TUSKFiZZY"


def test_the_installer_is_kept_only_when_it_is_the_one_its_release_names(monkeypatch, tmp_path):
    monkeypatch.setattr(netpolicy, "_open", lambda request, timeout: _Answer(INSTALLER))
    target = tmp_path / "setup.exe"
    seen = []
    netpolicy.download_installer(ASSET, target, size=len(INSTALLER), sha256=SHA, progress=seen.append)
    assert target.read_bytes() == INSTALLER and seen == [len(INSTALLER)]
    with pytest.raises(ValueError):
        netpolicy.download_installer(ASSET, target, size=len(INSTALLER), sha256="0" * 64)
    assert not target.exists()
    with pytest.raises(netpolicy.ResponseTooLarge):
        netpolicy.download_installer(ASSET, target, size=3, sha256=SHA)
    assert not target.exists()
    with pytest.raises(netpolicy.HostNotAllowed):
        netpolicy.download_installer("https://example.com/setup.exe", target, size=1, sha256=SHA)


def test_an_installer_is_redirected_only_to_githubs_download_host():
    handler = netpolicy._CheckedRedirects()

    def redirect(source, to):
        request = urllib.request.Request(source)
        request.installer = True
        return handler.redirect_request(request, None, 302, "Found", {}, to)

    sent = redirect(ASSET, "https://release-assets.githubusercontent.com/github-production-release-asset/1?sig=x")
    assert sent.full_url.startswith("https://release-assets.githubusercontent.com/")
    assert sent.follow_redirects is False  # and no further
    for to in ("https://example.com/setup.exe", "http://release-assets.githubusercontent.com/1"):
        with pytest.raises(netpolicy.RedirectRefused):
            redirect(ASSET, to)
    with pytest.raises(netpolicy.RedirectRefused):
        redirect("https://api.github.com/repos/someone/else/releases/assets/1", "https://release-assets.githubusercontent.com/1")


async def test_an_install_that_never_closes_guild_board_can_be_tried_again(tmp_path, monkeypatch):
    monkeypatch.setattr("guildboard.selfupdate.INSTALL_WAIT", 0.01)
    updater = _updater(tmp_path)
    await updater.check()
    updater.install()
    await updater._install_task
    assert updater.state == "installing"
    await asyncio.sleep(0.05)
    assert updater.state == "failed" and updater.install()
    await updater._install_task


async def test_a_failed_launch_says_so(tmp_path):
    def refused(path, sha256):
        raise OSError("blocked")

    updater = Updater(
        tmp_path / "updates", EventBus(), enabled=True, fetch=lambda: _release(), current="0.1.0", launch=refused,
        download=_download(), key=KEY.public,
    )
    await updater.check()
    updater.install()
    await updater._install_task
    assert updater.view()["message"] == "The installer could not be started. Try again."


def test_the_installer_is_read_again_and_held_while_it_starts(tmp_path, monkeypatch):
    # The file is hashed once more right before Windows starts it, held open so that nothing can write, rename or
    # delete it in between: what was checked is what starts.
    path = tmp_path / "TUSKFiZZY Setup 0.2.0.exe"
    path.write_bytes(INSTALLER)
    started = []

    def popen(arguments, **options):
        with pytest.raises(PermissionError):
            path.open("r+b")
        with pytest.raises(PermissionError):
            path.unlink()
        with pytest.raises(PermissionError):
            path.rename(tmp_path / "other.exe")
        assert path.read_bytes() == INSTALLER  # reading stays open to all: Windows reads the file to start it
        started.append((arguments, options["creationflags"]))

    monkeypatch.setattr(selfupdate.subprocess, "Popen", popen)
    start_installer(path, SHA.upper())  # a checksum in capitals is the same checksum
    detached = selfupdate.subprocess.DETACHED_PROCESS | selfupdate.subprocess.CREATE_NEW_PROCESS_GROUP
    assert started == [([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], detached)]
    path.write_bytes(b"MZ something else")  # the file is free again once Windows has started it
    with pytest.raises(ValueError):
        start_installer(path, SHA)
    assert len(started) == 1  # a file that changed after its download never starts


def test_a_download_stops_when_guild_board_closes(monkeypatch, tmp_path):
    monkeypatch.setattr(netpolicy, "_open", lambda request, timeout: _Answer(INSTALLER))
    cancel = threading.Event()
    cancel.set()
    target = tmp_path / "setup.exe"
    with pytest.raises(InterruptedError):
        netpolicy.download_installer(ASSET, target, size=len(INSTALLER), sha256=SHA, cancel=cancel)
    assert not target.exists()


# ------------------------------------------------------------------ the release notes, from the release body


def test_release_notes_are_read_from_the_bullets_release_ps1_writes():
    body = (
        "- [New] Remove a code: The x takes it off.\r\n"
        "- [Better] Gift codes in a new look: One field.\n"
        "- [Fixed] \"Log in again\": HoYoverse refused.\n"
        "- [New] A title with no colon\n"
        "- A line from before kinds\n"
        "Some prose that is not a bullet.\n"
        "- [Huge] Not a kind: skipped?\n"
        "\n"
    )
    assert parse_notes(body) == (
        {"kind": "new", "title": "Remove a code", "text": "The x takes it off."},
        {"kind": "better", "title": "Gift codes in a new look", "text": "One field."},
        {"kind": "fixed", "title": '"Log in again"', "text": "HoYoverse refused."},
        {"kind": "new", "title": "A title with no colon", "text": ""},
        {"kind": None, "title": "A line from before kinds", "text": ""},
        {"kind": None, "title": "[Huge] Not a kind: skipped?", "text": ""},
    )


def test_a_missing_or_odd_body_gives_no_notes():
    assert parse_notes(None) == ()
    assert parse_notes(5) == ()
    assert parse_notes("") == ()
    assert parse_notes("No bullets here.") == ()


def test_the_release_carries_its_notes_and_its_date():
    data = json.loads(_release())
    data["body"] = "- [New] One: Text."
    data["published_at"] = "2026-10-26T10:11:12Z"
    found = _offered(json.dumps(data).encode())
    assert found is not None
    assert found.notes == ({"kind": "new", "title": "One", "text": "Text."},)
    assert found.released == "2026-10-26"
    assert _offered(_release()).notes == ()


async def test_the_view_gives_the_notes_to_the_pages(tmp_path):
    data = json.loads(_release())
    data["body"] = "- [Fixed] A fix: It works."
    updater = _updater(tmp_path, fetch=lambda: json.dumps(data).encode())
    await updater.check()
    assert updater.view()["notes"] == [{"kind": "fixed", "title": "A fix", "text": "It works."}]
    assert updater.view()["released"] is None


# ------------------------------------------------------------------ the demo's way to show an update


def test_the_demo_can_show_each_state_of_an_update_and_the_card_after_one(tmp_path, clock):
    core = Core(AppPaths(tmp_path / "data"), demo=True, clock=clock, sleep=FakeSleep(clock), process_lister=lambda: set())
    with TestClient(create_app(core, 4321), base_url="http://127.0.0.1:4321") as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert client.get("/api/status").json()["update"]["state"] == "none"
        newest, items = next(iter(load_changes().items()))  # the demo shows the file's newest entry
        for state in ("available", "downloading", "installing", "failed"):
            answer = client.post("/api/demo/update", json={"state": state})
            assert answer.status_code == 200
            shown = client.get("/api/status").json()["update"]
            assert shown["state"] == state and shown["version"] == newest
            assert [item["title"] for item in shown["notes"]] == [item["title"] for item in items]
        assert client.get("/api/status").json()["update"]["progress"] is None
        client.post("/api/demo/update", json={"state": "downloading"})
        assert client.get("/api/status").json()["update"]["progress"] == 0.62
        assert client.post("/api/demo/update", json={"state": "none"}).status_code == 200
        assert client.get("/api/status").json()["update"]["state"] == "none"
        assert client.post("/api/demo/update", json={"state": "sideways"}).status_code == 422
        # Pressing Update now in the demo downloads nothing: it fails, as an offline player's would.
        client.post("/api/demo/update", json={"state": "available"})
        assert client.post("/api/update/install").status_code == 200
        for _ in range(200):
            if client.get("/api/status").json()["update"]["state"] == "failed":
                break
            client.portal.call(asyncio.sleep, 0.01)
        assert client.get("/api/status").json()["update"]["state"] == "failed"

        assert client.get("/api/status").json()["whats_new"] is None
        assert client.post("/api/demo/whats-new").status_code == 200
        shown = client.get("/api/status").json()["whats_new"]
        assert shown["version"] == newest and len(shown["items"]) == len(items)
        client.post("/api/whats-new/seen")
        assert client.get("/api/status").json()["whats_new"] is None
    core.store.close()


def test_the_demo_switches_do_not_exist_in_live_mode(tmp_path, clock):
    from support import AccountSource

    core = Core(
        AppPaths(tmp_path / "data"), demo=False, clock=clock, source=AccountSource(), sleep=FakeSleep(clock),
        process_lister=lambda: set(),
    )
    with TestClient(create_app(core, 4321), base_url="http://127.0.0.1:4321") as client:
        client.get("/launch", params={"k": core.launch_secret}, follow_redirects=False)
        assert client.post("/api/demo/update", json={"state": "available"}).status_code == 404
        assert client.post("/api/demo/whats-new").status_code == 404
    core.store.close()
