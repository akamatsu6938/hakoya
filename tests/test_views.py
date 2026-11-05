"""画面の見え方（★11/5）：見本確認用・自社利用用・他社利用用。DATA_DIR/settings.json の view に残す。
実データは使わない（物件の一覧は差し替える）。"""
from __future__ import annotations

import json

import keyring
import pytest

from core import paths
from core import settings_store as ss
from core.rules import RULES_DIR

# 自社の会社ルール（rules/jisha.yaml）は公開の版に入れない。jisha を選ぶテストは、ないときは飛ばす
needs_jisha = pytest.mark.skipif(not (RULES_DIR / "jisha.yaml").exists(), reason="自社の会社ルール（公開しない）がない環境")


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "REPO_ROOT", paths.REPO_ROOT)       # 印（EDITION）のない場所のまま
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))   # 本物の資格情報マネージャーは使わない
    return tmp_path


@pytest.fixture
def client(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    names = ["物件A", "デモ用", "見本の家", "試験の家"]
    monkeypatch.setattr(server, "_aliases", lambda: list(names))
    monkeypatch.setattr(server, "property_files", lambda a: paths.PropertyFiles(alias=a, glb=None, jww_folder=None))
    return TestClient(server.app)


def test_view_is_saved_and_unknown_is_refused(data_dir):
    assert ss.load()["view"] == "own"                                         # 既定は自社利用用（今までどおり）
    assert ss.set_view("sample")["view"] == "sample"
    assert json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))["view"] == "sample"
    with pytest.raises(ValueError, match="見え方"):
        ss.set_view("everything")
    (data_dir / "settings.json").write_text('{"view": "nai"}', encoding="utf-8")
    assert ss.load()["view"] == "own"                                         # 知らない値なら既定に戻す


@needs_jisha
def test_other_view_hides_plan_and_defaults_to_b_koumuten(data_dir):
    ss.set_auto_inspect("claude_code")
    ss.set_view("other")
    st = ss.load()
    assert st["company"] == "bkoumuten"                                       # 選んでいなければ B工務店（架空）
    assert "claude_code" not in ss.available_connections() and st["auto_inspect"] == "off"
    with pytest.raises(ValueError, match="他社利用用"):
        ss.check_connection("claude_code")
    with pytest.raises(ValueError):
        ss.set_auto_inspect("claude_code")
    ss.check_connection("api_key")
    assert ss.set_company("jisha")["company"] == "jisha"                    # 選んだら、その会社のまま
    assert ss.load()["company"] == "jisha"
    ss.set_view("own")
    st = ss.load()
    assert st["auto_inspect"] == "claude_code"                                # 自社利用用に戻すと、選んでいた検品のしかたも戻る
    assert st["company"] == "jisha"


@needs_jisha
def test_company_not_chosen_follows_the_view(data_dir):
    (data_dir / "settings.json").write_text('{"company": "jisha", "view": "other"}', encoding="utf-8")   # 前の版が書いた設定
    assert ss.load()["company"] == "bkoumuten"
    ss.set_view("own")
    assert ss.load()["company"] == "jisha"


def test_external_edition_cannot_choose_own(data_dir, monkeypatch):
    monkeypatch.setenv("HAKOYA_EDITION", "external")
    assert ss.available_views() == ["sample", "other"]
    assert ss.load()["view"] == "other"                                       # 社外用の既定は他社利用用
    with pytest.raises(ValueError, match="この版では"):
        ss.set_view("own")
    (data_dir / "settings.json").write_text('{"view": "own"}', encoding="utf-8")
    assert ss.load()["view"] == "other"
    assert ss.set_view("sample")["view"] == "sample"
    assert "claude_code" not in ss.available_connections()


def test_sample_aliases():
    assert ss.is_sample_alias("デモ用") and ss.is_sample_alias("見本の家") and ss.is_sample_alias("見本")
    assert not ss.is_sample_alias("物件A") and not ss.is_sample_alias("試験の見本")


def test_view_endpoints(client):
    st = client.get("/api/settings").json()
    assert st["view"] == "own" and st["views"] == ["sample", "own", "other"]
    assert client.post("/api/settings/view", json={"view": "other"}).json()["view"] == "other"
    st = client.get("/api/settings").json()
    assert st["view"] == "other" and "claude_code" not in st["connections"] and st["company"] == "bkoumuten"
    assert st["company_chosen"] is False
    r = client.post("/api/settings/view", json={"view": "nai"})
    assert r.status_code == 400 and "見え方" in r.json()["detail"]
    assert client.get("/api/settings").json()["view"] == "other"              # 断ったら変えない
    r = client.post("/api/inspect/x?connection=claude_code")
    assert r.status_code == 404                                               # 物件がない（先に物件を確かめる）


def test_external_edition_endpoint_refuses_own(client, monkeypatch):
    monkeypatch.setenv("HAKOYA_EDITION", "external")
    st = client.get("/api/settings").json()
    assert st["views"] == ["sample", "other"] and st["view"] == "other" and st["edition"] == "external"
    assert client.post("/api/settings/view", json={"view": "own"}).status_code == 400


def test_sample_view_lists_only_sample_houses(client):
    every = [p["alias"] for p in client.get("/api/properties").json()]
    assert every == ["物件A", "デモ用", "見本の家", "試験の家"]                   # 自社利用用：全部
    client.post("/api/settings/view", json={"view": "sample"})
    assert [p["alias"] for p in client.get("/api/properties").json()] == ["デモ用", "見本の家"]
    client.post("/api/settings/view", json={"view": "other"})
    assert len(client.get("/api/properties").json()) == 4                     # 他社利用用も全部
