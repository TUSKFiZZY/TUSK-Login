import json
import urllib.error

import pytest

from guildboard import enka


def test_enka_profile_supports_nine_and_ten_digit_uids(monkeypatch):
    seen = []
    monkeypatch.setattr(enka, "fetch_enka_profile", lambda uid: seen.append(uid) or json.dumps({
        "playerInfo": {"nickname": "Traveler"},
        "avatarInfoList": [{"avatarId": 10000002}],
        "ttl": 300,
    }).encode())
    cache = enka.EnkaProfileCache()
    assert cache.get("123456789")["uid"] == "123456789"
    assert cache.get("1234567890")["uid"] == "1234567890"
    assert seen == ["123456789", "1234567890"]


@pytest.mark.parametrize("uid", ["", "12345678", "12345678901", "12345678x", "１２３４５６７８９"])
def test_enka_profile_rejects_invalid_uid(uid):
    with pytest.raises(ValueError):
        enka.EnkaProfileCache().get(uid)


def test_enka_profile_is_cached_for_server_ttl(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(enka, "fetch_enka_profile", lambda uid: calls.append(uid) or b'{"playerInfo":{"nickname":"A"},"ttl":120}')
    cache = enka.EnkaProfileCache(clock=lambda: now[0])
    first = cache.get("123456789")
    second = cache.get("123456789")
    assert first["cached"] is False and second["cached"] is True
    assert calls == ["123456789"]
    now[0] += 121
    assert cache.get("123456789")["cached"] is False
    assert calls == ["123456789", "123456789"]


def test_enka_profile_bounds_server_ttl(monkeypatch):
    monkeypatch.setattr(enka, "fetch_enka_profile", lambda uid: b'{"playerInfo":{"nickname":"A"},"ttl":9999999}')
    now = [0.0]
    cache = enka.EnkaProfileCache(clock=lambda: now[0])
    assert cache.get("123456789")["ttl"] == 86400


def test_enka_profile_translates_not_found(monkeypatch):
    def missing(uid):
        raise urllib.error.HTTPError("url", 404, "missing", {}, None)
    monkeypatch.setattr(enka, "fetch_enka_profile", missing)
    with pytest.raises(enka.EnkaNotFound):
        enka.EnkaProfileCache().get("123456789")


def test_enka_profile_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(enka, "fetch_enka_profile", lambda uid: b"not-json")
    with pytest.raises(enka.EnkaError):
        enka.EnkaProfileCache().get("123456789")
