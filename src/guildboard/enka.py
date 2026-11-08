"""Public Enka Network profile lookup for a Genshin UID.

No HoYoLAB credentials are used. The network boundary and UID validation live in
netpolicy; this module only parses and briefly caches the public response.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
from typing import Any

from guildboard.netpolicy import fetch_enka_profile


class EnkaError(Exception):
    """A public profile could not be read or did not contain a valid profile."""


class EnkaUnavailable(EnkaError):
    """Enka Network is temporarily unavailable or rate-limiting requests."""


class EnkaNotFound(EnkaError):
    """Enka has no public profile for this UID."""


class EnkaProfileCache:
    """Small in-memory cache; Enka's server TTL is bounded to avoid stale data for too long."""

    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, uid: str) -> dict[str, Any]:
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() or len(uid) not in (9, 10):
            raise ValueError("Genshin UID must contain 9 or 10 digits")
        now = self._clock()
        with self._lock:
            cached = self._items.get(uid)
            if cached is not None and cached[0] > now:
                return {**cached[1], "cached": True}
        try:
            raw = fetch_enka_profile(uid)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise EnkaNotFound("No public Enka profile was found for this UID.") from None
            if error.code in (429, 424, 500, 502, 503, 504):
                raise EnkaUnavailable("Enka Network is temporarily unavailable. Try again later.") from None
            raise EnkaError("Enka Network could not return this profile.") from None
        except (OSError, TimeoutError, ConnectionError):
            raise EnkaUnavailable("Enka Network could not be reached. Try again later.") from None
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EnkaError("Enka Network returned an unreadable profile.") from None
        if not isinstance(payload, dict):
            raise EnkaError("Enka Network returned an invalid profile.")
        if payload.get("playerInfo") is None and not payload.get("avatarInfoList"):
            raise EnkaNotFound("This UID has no public character showcase on Enka Network.")
        ttl = payload.get("ttl", 300)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            ttl = 300
        ttl = max(60, min(86_400, int(ttl)))
        safe = {
            "uid": uid,
            "playerInfo": payload.get("playerInfo") if isinstance(payload.get("playerInfo"), dict) else {},
            "avatarInfoList": payload.get("avatarInfoList") if isinstance(payload.get("avatarInfoList"), list) else [],
            "ttl": ttl,
            "fetchedAt": int(time.time()),
            "cached": False,
        }
        with self._lock:
            self._items[uid] = (self._clock() + ttl, safe)
        return dict(safe)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_default_cache = EnkaProfileCache()


def get_profile(uid: str) -> dict[str, Any]:
    return _default_cache.get(uid)
