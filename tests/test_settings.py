"""API キーの保管（core/secret_store.py）と設定の画面の口（app/server.py）を確かめる。
本物の資格情報マネージャーは使わず、入れ物を差し替える。キーは試験用の偽物。"""
from __future__ import annotations

from urllib.parse import quote

import keyring
import pytest

from core import secret_store

FAKE = "test-" + "x" * 30           # 試験用の偽のキー（本物の形にしない）


@pytest.fixture
def vault(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))

    def delete(s, u):
        if (s, u) not in store:
            raise keyring.errors.PasswordDeleteError("none")
        del store[(s, u)]
    monkeypatch.setattr(keyring, "delete_password", delete)
    return store


def test_save_read_delete(vault):
    assert not secret_store.has_api_key()
    secret_store.set_api_key("  " + FAKE + "\n")                 # 前後の空白は取る
    assert secret_store.get_api_key() == FAKE and vault == {("HAKOYA", "anthropic_api_key"): FAKE}
    secret_store.delete_api_key()
    secret_store.delete_api_key()                                # 2回消してもエラーにしない
    assert not secret_store.has_api_key()


@pytest.mark.parametrize("bad, msg", [("", "空"), ("ｔｅｓｔ" + "x" * 30, "使えない文字"), ("test x" + "x" * 30, "使えない文字"),
                                      ("short", "短すぎ")])
def test_bad_keys_are_refused_in_japanese(vault, bad, msg):
    with pytest.raises(ValueError, match=msg) as e:
        secret_store.set_api_key(bad)
    assert bad.strip() not in str(e.value) or not bad.strip()   # キーの中身はメッセージに出さない
    assert vault == {}


@pytest.fixture
def client(vault):
    from fastapi.testclient import TestClient
    from app.server import app
    return TestClient(app)


def test_settings_endpoints_never_return_the_key(client, vault):
    assert client.get("/api/settings").json() == {"api_key_set": False}
    r = client.post("/api/settings/api_key", json={"api_key": FAKE})
    assert r.status_code == 200 and r.json() == {"api_key_set": True} and FAKE not in r.text
    assert client.get("/api/settings").json() == {"api_key_set": True}
    assert client.post("/api/settings/api_key", json={"api_key": "short"}).status_code == 400
    assert client.delete("/api/settings/api_key").json() == {"api_key_set": False} and vault == {}


def test_other_sites_cannot_change_settings(client, vault):
    r = client.post("/api/settings/api_key", json={"api_key": FAKE}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and vault == {}
    ok = client.post("/api/settings/api_key", json={"api_key": FAKE}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200                                   # HAKOYA の画面からは通る


@pytest.mark.realdata
def test_api_inspection_needs_a_key(client, vault):
    r = client.post(f"/api/inspect/{quote('物件A')}?connection=api_key")
    assert r.status_code == 409 and "API キー" in r.json()["detail"]
