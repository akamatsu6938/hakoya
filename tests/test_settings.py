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
    assert client.get("/api/settings").json()["api_key_set"] is False
    r = client.post("/api/settings/api_key", json={"api_key": FAKE})
    assert r.status_code == 200 and r.json() == {"api_key_set": True} and FAKE not in r.text
    st = client.get("/api/settings")
    assert st.json()["api_key_set"] is True and FAKE not in st.text
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


# ---------------------------------------------------------------- 組み立てたあとの検品（DATA_DIR/settings.json）

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from core import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    return tmp_path


def test_auto_inspect_store(data_dir, monkeypatch):
    from core import settings_store as ss
    assert ss.load()["auto_inspect"] == "off"                                  # 既定は AI なし（勝手に利用枠を使わない）
    assert ss.set_auto_inspect("claude_code")["auto_inspect"] == "claude_code"
    with pytest.raises(ValueError, match="費用の確認"):
        ss.set_auto_inspect("api_key")
    assert ss.set_auto_inspect("api_key", cost_ack=True)["api_cost_ack"] is True
    assert ss.set_auto_inspect("api_key")["auto_inspect"] == "api_key"          # 確かめは1回だけ
    with pytest.raises(ValueError):
        ss.set_auto_inspect("anything")
    (data_dir / "settings.json").write_text("{壊れた", encoding="utf-8")
    assert ss.load()["auto_inspect"] == "off"                                  # 壊れていたら既定
    ss.set_auto_inspect("claude_code")
    monkeypatch.setenv("HAKOYA_EDITION", "external")                           # 社外用：プランの接続は出さない
    assert "claude_code" not in ss.available_connections() and ss.load()["auto_inspect"] == "off"
    with pytest.raises(ValueError, match="社外用"):
        ss.set_auto_inspect("claude_code")


def test_auto_inspect_endpoints(client, vault, data_dir):
    st = client.get("/api/settings").json()
    assert st["auto_inspect"] == "off" and "claude_code" in st["connections"] and st["max_budget_usd"] > 0
    assert client.post("/api/settings/auto_inspect", json={"value": "claude_code"}).json()["auto_inspect"] == "claude_code"
    assert client.post("/api/settings/auto_inspect", json={"value": "api_key"}).status_code == 400
    assert client.post("/api/settings/auto_inspect", json={"value": "api_key", "cost_ack": True}).status_code == 200
    text = (data_dir / "settings.json").read_text(encoding="utf-8")
    assert FAKE not in text and "api_key\"" in text                            # API キーそのものは書かない
