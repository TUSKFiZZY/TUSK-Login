"""The installed copy's updates: it asks now and then whether a newer release is out, and on the player's press downloads
its installer into the data folder, checks it and starts it. The installer asks this Guild Board to quit, puts the new
one in its place and opens it again. A copy run from a checkout, and the demo, never ask.

A release is offered only when its installer comes with a signature by the player's release key (H4 of the audit of
2026-11-01): GitHub's answer alone proves only that a file is the one GitHub lists, and anyone holding the player's
GitHub login could list another."""
from __future__ import annotations

import _winapi
import asyncio
import ctypes
import functools
import hashlib
import json
import logging
import msvcrt
import os
import re
import struct
import subprocess
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable

from guildboard import __version__, netpolicy
from guildboard.events import EventBus

log = logging.getLogger(__name__)

FIRST_CHECK_AFTER = 60.0  # seconds after the start: the start itself comes first
CHECK_EVERY = 12 * 3600.0  # Guild Board can stay in the tray for days
INSTALL_WAIT = 600.0  # seconds: an installer that has not closed this Guild Board by then was cancelled or failed
MAX_INSTALLER_BYTES = 600_000_000
INSTALLER_ARGUMENTS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
VERSION = re.compile(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)")
SHA256 = re.compile(r"sha256:([0-9a-fA-F]{64})")
NOTE_LINE = re.compile(r"\s*[-*]\s+(?:\[(New|Better|Fixed)\]\s*)?(.+)")  # the rest is stripped after: no backtracking
PUBLISHED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

# The release key's public half: ECDSA on the curve P-256, as its uncompressed point (04, then x and y) in hex. The player
# made the key once with tools/make-release-key.ps1, keeps its private half off GitHub, and tools/sign-release.ps1 signs every
# release with it; tests/test_handsoff.py pins this line. While it is empty, no release is offered.
RELEASE_KEY = "044d6934e90d6bf5628c147472250a93be80b6dc69af5476df335a7b9380acc6b101d92a01f0eb46b3dee516ebcae864ff3c8cab86b3ab56574d2474821fc124ba"
SIGNATURE_BYTES = 64  # IEEE P1363: r, then s, 32 bytes each
SIGNATURE_SUFFIX = ".sig"  # the signature's asset is named for the installer's: "<installer name>.sig"
POINT = re.compile(r"04[0-9a-f]{128}")
# Windows' own CNG checks the signature: no library is added for it.
_ECDSA_P256 = "ECDSA_P256"
_ECC_PUBLIC_BLOB = "ECCPUBLICBLOB"
_ECDSA_PUBLIC_P256_MAGIC = 0x31534345  # "ECS1", which starts a P-256 public key for ECDSA in CNG's blob
_SHARE_READ = 0x1  # FILE_SHARE_READ alone: others may read the installer, and nothing may write, rename or delete it


def parse_version(text: str) -> tuple[int, int, int] | None:
    found = VERSION.fullmatch(text.strip())
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def parse_notes(body: Any) -> tuple[dict[str, Any], ...]:
    """The items of a release's notes, from its body as tools/release-signing.ps1 writes it: one bullet per item, `- [New] Title: text`
    (or Better, Fixed), and `- Title` for an item without a kind. Anything that is not a bullet is skipped, and so is a
    body that is missing, so that a release written by hand still installs."""
    if not isinstance(body, str):
        return ()
    items: list[dict[str, Any]] = []
    for line in body.splitlines():
        found = NOTE_LINE.fullmatch(line)
        if found is None:
            continue
        kind, rest = found[1], found[2].strip()
        if not rest:
            continue
        if kind is None:
            items.append({"kind": None, "title": rest, "text": ""})
            continue
        title, _, text = rest.partition(":")  # a tag with no colon is a title alone
        if title.strip():
            items.append({"kind": kind.lower(), "title": title.strip(), "text": text.strip()})
    return tuple(items)


@dataclass(frozen=True)
class Release:
    version: str
    url: str  # the installer's asset, as GitHub's API names it
    size: int
    sha256: str
    notes: tuple[dict[str, Any], ...] = ()  # what the version changed, for the list the pages open
    released: str | None = None  # the day GitHub published it, as YYYY-MM-DD


def release_text(tag: str, name: str, size: int, sha256: str) -> bytes:
    """What the release key signs for a release (tools/sign-release.ps1, through tools/release-signing.ps1): the release's tag and
    its installer's name, size and SHA-256 in lower-case hex, as GitHub's API gives them, one to a line, with no line's
    end after the last."""
    return f"{tag}\n{name}\n{size}\n{sha256}".encode("utf-8")


@functools.cache
def _bcrypt() -> Any:
    """CNG's library, loaded once, when a signature is first checked."""
    bcrypt = ctypes.WinDLL("bcrypt")
    handle, out = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    for name, arguments in (
        ("BCryptOpenAlgorithmProvider", (out, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.ULONG)),
        ("BCryptImportKeyPair", (
            handle, handle, wintypes.LPCWSTR, out, ctypes.c_char_p, wintypes.ULONG, wintypes.ULONG,
        )),
        ("BCryptVerifySignature", (
            handle, ctypes.c_void_p, ctypes.c_char_p, wintypes.ULONG, ctypes.c_char_p, wintypes.ULONG, wintypes.ULONG,
        )),
        ("BCryptDestroyKey", (handle,)),
        ("BCryptCloseAlgorithmProvider", (handle, wintypes.ULONG)),
    ):
        function = getattr(bcrypt, name)
        function.argtypes, function.restype = arguments, wintypes.LONG  # an NTSTATUS: 0 is success
    return bcrypt


def signed(text: bytes, signature: bytes, key: str) -> bool:
    """Whether `signature` is the release key's over `text`: ECDSA on P-256 over its SHA-256, as r then s, checked by
    Windows' own CNG. `key` is the key's public half as RELEASE_KEY holds it. An empty key, or one that is not such a
    point, signs nothing, and so does anything CNG cannot check."""
    if not POINT.fullmatch(key) or len(signature) != SIGNATURE_BYTES:
        return False
    bcrypt = _bcrypt()
    blob = struct.pack("<II", _ECDSA_PUBLIC_P256_MAGIC, 32) + bytes.fromhex(key)[1:]  # x and y, after the point's 04
    digest = hashlib.sha256(text).digest()
    algorithm, public = ctypes.c_void_p(), ctypes.c_void_p()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), _ECDSA_P256, None, 0) != 0:
        return False
    try:
        if bcrypt.BCryptImportKeyPair(algorithm, None, _ECC_PUBLIC_BLOB, ctypes.byref(public), blob, len(blob), 0) != 0:
            return False
        try:
            return bcrypt.BCryptVerifySignature(public, None, digest, len(digest), signature, len(signature), 0) == 0
        finally:
            bcrypt.BCryptDestroyKey(public)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def _asset(asset: dict[str, Any], most: int) -> tuple[str, int, str] | None:
    """An asset's address, size and SHA-256, when its address is one of the releases repository's assets, its size is
    at most `most` bytes and GitHub gives its checksum."""
    url, size, digest = asset.get("url"), asset.get("size"), SHA256.fullmatch(str(asset.get("digest", "")))
    if not isinstance(url, str) or not netpolicy.INSTALLER_ASSET.fullmatch(url) or digest is None:
        return None
    if not isinstance(size, int) or not 0 < size <= most:
        return None
    return url, size, digest[1].lower()


def newer_release(
    body: bytes,
    current: str = __version__,
    *,
    signature: Callable[[str, int, str], bytes],
    key: str = RELEASE_KEY,
) -> Release | None:
    """The release GitHub's API describes, if it is newer than `current`, carries an installer with its checksum, and
    carries that installer's signature by the release key `key`: the asset named for it with ".sig" after, a signature
    over the answer's own tag and the installer's name, size and checksum. `signature(url, size, sha256)` downloads that
    asset; it is asked only for a release that would be offered otherwise."""
    data: Any = json.loads(body)
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    version, mine = parse_version(tag) if isinstance(tag, str) else None, parse_version(current)
    if version is None or mine is None or version <= mine:
        return None
    assets = [asset for asset in data.get("assets") or () if isinstance(asset, dict)]
    for asset in assets:
        name = asset.get("name")
        if not isinstance(name, str) or not name.lower().endswith(".exe"):
            continue
        installer = _asset(asset, MAX_INSTALLER_BYTES)
        if installer is None:
            continue
        url, size, sha256 = installer
        if not POINT.fullmatch(key):
            # Built while no key was pinned: this copy can check no signature, so it downloads none and offers nothing.
            log.warning("This copy has no release key, so it offers no update")
            return None
        found = next((_asset(a, SIGNATURE_BYTES) for a in assets if a.get("name") == name + SIGNATURE_SUFFIX), None)
        number = ".".join(map(str, version))
        if found is None or found[1] != SIGNATURE_BYTES or not signed(
            release_text(tag, name, size, sha256), signature(*found), key
        ):
            log.warning("The release %s is not signed by the release key, so it is not offered", number)
            return None
        day = PUBLISHED.match(str(data.get("published_at") or ""))
        return Release(number, url, size, sha256, parse_notes(data.get("body")), day[0] if day else None)
    return None


def _held(path: Path) -> BinaryIO:
    """The downloaded installer, opened to be read while Windows refuses every other opening that would write, rename or
    delete it, for as long as it stays open. Its opening is announced as Python's open announces one, so that what
    watches the opening of files (the tests' guards) sees it."""
    sys.audit("open", str(path), "rb", os.O_RDONLY | os.O_BINARY)
    handle = _winapi.CreateFile(
        str(path), _winapi.GENERIC_READ, _SHARE_READ, _winapi.NULL, _winapi.OPEN_EXISTING, 0, _winapi.NULL
    )
    try:
        number = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    return open(number, "rb")


def start_installer(path: Path, sha256: str) -> None:
    """Start the installer on its own, so that it outlives this Guild Board, which it asks to quit. It is read once more
    first, held so that nothing can change it, and starts only while it is still the file its signed release names; it
    is let go once Windows has started it. ValueError says that it changed."""
    with _held(path) as file:
        digest = hashlib.sha256()
        while chunk := file.read(1 << 20):
            digest.update(chunk)
        if digest.hexdigest() != sha256.lower():
            raise ValueError("the installer is not the one its release names")
        # SHL-8: once SignPath signs the installer with Authenticode, WinVerifyTrust and a check of the expected publisher
        # come here, on the file while it is held, before it starts.
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # The Guild Board the installer opens again starts afresh, not inside this one's unpacked environment.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen([str(path), *INSTALLER_ARGUMENTS], creationflags=flags, close_fds=True, env=env)


class Updater:
    def __init__(
        self,
        folder: Path,
        bus: EventBus,
        *,
        enabled: bool,
        fetch: Callable[[], bytes] = netpolicy.fetch_latest_release,
        download: Callable[..., None] = netpolicy.download_installer,
        launch: Callable[[Path, str], None] = start_installer,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        current: str = __version__,
        key: str = RELEASE_KEY,
    ) -> None:
        self.folder = folder  # inside the data folder: the installers it downloads, and a release's signature for a moment
        self.enabled = enabled
        self._bus = bus
        self._fetch, self._download, self._launch, self._sleep = fetch, download, launch, sleep
        self._current = current
        self._key = key
        self.release: Release | None = None
        self.state = "none"  # none, available, downloading, installing, failed
        self.progress: float | None = None
        self.message: str | None = None
        self._install_task: asyncio.Task[None] | None = None
        self._cancel = threading.Event()  # tells a download in its thread to stop, when Guild Board closes

    def view(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "version": self.release.version if self.release is not None else None,
            "progress": self.progress,
            "message": self.message,
            "notes": list(self.release.notes) if self.release is not None else [],
            "released": self.release.released if self.release is not None else None,
        }

    def demo_show(self, release: Release, state: str) -> None:
        """The demo only: put the updater in a state, to show the notice. Nothing is fetched."""
        self.release = release
        self._set(
            state,
            progress=0.62 if state == "downloading" else None,
            message="The update could not be downloaded. Check the connection and try again." if state == "failed" else None,
        )

    def demo_clear(self) -> None:
        self.release = None
        self._set("none")

    def _set(self, state: str, *, progress: float | None = None, message: str | None = None) -> None:
        self.state, self.progress, self.message = state, progress, message
        self._bus.publish("update_changed")

    async def run(self) -> None:
        """Ask now and then, for as long as Guild Board runs. The installers an earlier start downloaded go first, and a
        signature a start that ended while it checked left behind."""
        if not self.enabled:
            return
        if self.folder.is_dir():
            for old in (*self.folder.glob("*.exe"), *self.folder.glob("*" + SIGNATURE_SUFFIX)):
                try:
                    old.unlink(missing_ok=True)
                except OSError:  # still held by its setup or a virus scanner: removed at a later start
                    pass
        await self._sleep(FIRST_CHECK_AFTER)
        while True:
            await self.check()
            await self._sleep(CHECK_EVERY)

    async def check(self) -> None:
        if self.state in ("downloading", "installing"):
            return
        try:
            release = await asyncio.to_thread(self._newest)
        except Exception as error:  # offline, or GitHub answered something else: asked again later
            log.info("Could not look for an update (%s)", netpolicy.what_failed(error))
            return
        # The player may have pressed Update while this check was under way: the install goes on as it is.
        if release is None or self.state in ("downloading", "installing"):
            return
        if release != self.release or self.state != "failed":
            self.release = release
            self._set("available")

    def _newest(self) -> Release | None:
        """On a thread: the newest release, when it is newer and signed by the release key."""
        return newer_release(self._fetch(), self._current, signature=self._signature, key=self._key)

    def _signature(self, url: str, size: int, sha256: str) -> bytes:
        """A release's signature, downloaded into the updates folder the way its installer is, read, and removed."""
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"release{SIGNATURE_SUFFIX}"
        try:
            self._download(url, target, size=size, sha256=sha256, cancel=self._cancel)
            return target.read_bytes()
        finally:
            target.unlink(missing_ok=True)

    def install(self) -> bool:
        """Start the download and the installer, on the player's press. False when there is nothing to install."""
        if self.release is None or self.state not in ("available", "failed"):
            return False
        self._set("downloading", progress=0.0)  # at once, so that a second press finds it busy
        self._install_task = asyncio.get_running_loop().create_task(self._install(self.release))
        return True

    async def _install(self, release: Release) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"TUSKFiZZY Setup {release.version}.exe"
        loop = asyncio.get_running_loop()
        shown = [0.0]

        def moved(done: int) -> None:
            part = done / release.size
            if part - shown[0] >= 0.05:  # a step of 5 %, so the pages hear of it now and then, not on every chunk
                shown[0] = part
                loop.call_soon_threadsafe(lambda: self._set("downloading", progress=round(part, 2)))

        try:
            await asyncio.to_thread(
                self._download, release.url, target, size=release.size, sha256=release.sha256, progress=moved,
                cancel=self._cancel,
            )
        except Exception as error:
            log.warning("The update to %s did not download (%s)", release.version, netpolicy.what_failed(error))
            self._set("failed", message="The update could not be downloaded. Check the connection and try again.")
            return
        self._set("installing")
        try:
            await asyncio.to_thread(self._launch, target, release.sha256)
        except Exception as error:
            log.warning("The installer of %s did not start (%s)", release.version, netpolicy.what_failed(error))
            self._set("failed", message="The installer could not be started. Try again.")
            return
        # The installer closes this Guild Board. Still here after INSTALL_WAIT, it was cancelled or could not finish.
        loop.call_later(INSTALL_WAIT, self._not_installed)

    def _not_installed(self) -> None:
        if self.state == "installing":
            self._set("failed", message="The update was not installed. Try again.")

    async def stop(self) -> None:
        self._cancel.set()
        if self._install_task is not None:
            self._install_task.cancel()
            await asyncio.gather(self._install_task, return_exceptions=True)
            self._install_task = None
