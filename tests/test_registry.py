"""物件の登録（core/registry.py と画面の入口）。本物の local_paths.yaml には書かない（一時フォルダで試す）。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core import registry as rg
from core.paths import aliases, property_files
from core.privacy import ng_words


@pytest.fixture
def yml(tmp_path):
    p = tmp_path / "local_paths.yaml"
    p.write_text("# 作者のメモ\nng_words: [\"山田\"]\n物件A:\n  glb: C:/x/a.glb\n", encoding="utf-8")
    return p


def _glb(folder: Path, name: str = "鈴木様邸.glb") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"glb")
    return folder / name


def test_register_appends_and_keeps_what_was_there(yml, tmp_path):
    g = _glb(tmp_path / "in")
    _glb(tmp_path / "図面", "1階平面図.jww")
    assert rg.next_alias(yml) == "物件B"
    rg.register("物件B", str(g), str(tmp_path / "図面"), ["鈴木", "", "  "], yml)
    text = yml.read_text(encoding="utf-8")
    assert text.startswith("# 作者のメモ\nng_words: [\"山田\"]\n物件A:")            # 前の中身は書き換えない
    assert aliases(yml) == ["物件A", "物件B"]
    f = property_files("物件B", yml)
    assert f.glb == g and set(f.jww) == {"plan"}
    assert ng_words(yml) == ["山田", "鈴木"]
    assert rg.next_alias(yml) == "物件C"


def test_register_a_folder_with_one_glb(yml, tmp_path):
    g = _glb(tmp_path / "物件C")
    rg.register("物件C", f'"{tmp_path / "物件C"}"', None, [], yml)                   # エクスプローラーの「パスのコピー」の " " 付きでもよい
    assert property_files("物件C", yml).glb == g


@pytest.mark.parametrize("alias,glb,word", [
    ("物件A", "ok", "もう登録"), ("物 件", "ok", "仮名は"), ("a/b", "ok", "仮名は"),
    ("物件D", "two", "2 つ"), ("物件D", "none", "見つかりません"), ("物件D", "relative", "C:\\"),
])
def test_register_refuses_without_naming_files(yml, tmp_path, alias, glb, word):
    _glb(tmp_path / "two", "x.glb")
    _glb(tmp_path / "two", "鈴木様邸.glb")
    good = _glb(tmp_path / "ok")
    paths = {"ok": str(good), "two": str(tmp_path / "two"), "none": str(tmp_path / "nothing.glb"), "relative": "in/a.glb"}
    before = yml.read_text(encoding="utf-8")
    with pytest.raises(ValueError) as e:
        rg.register(alias, paths[glb], None, [], yml)
    assert word in str(e.value) and "鈴木" not in str(e.value) and str(tmp_path) not in str(e.value)
    assert yml.read_text(encoding="utf-8") == before


def test_list_shows_kinds_and_counts_but_no_places(yml, tmp_path):
    rg.register("物件B", str(_glb(tmp_path / "in")), None, ["鈴木"], yml)
    data = rg.list_properties(yml)
    assert data["next_alias"] == "物件C" and data["global_ng_words"] == 1
    assert [r["alias"] for r in data["properties"]] == ["物件A", "物件B"]
    assert data["properties"][1] == {"alias": "物件B", "glb": "あり", "drawings": [], "ng_words": 1}
    assert str(tmp_path) not in str(data) and "鈴木" not in str(data)


def test_screen_registers_with_a_picked_file(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    from core import paths
    yml = tmp_path / "local_paths.yaml"
    monkeypatch.setattr(paths, "LOCAL_PATHS", yml)
    g = _glb(tmp_path / "選んだ所")
    monkeypatch.setattr(rg, "pick", lambda what: str(g))                              # Windows の選ぶ窓の代わり
    c = TestClient(server.app)
    assert c.get("/api/registry").json() == {"properties": [], "next_alias": "物件A", "global_ng_words": 0}
    r = c.post("/api/registry/pick", json={"what": "glb"}).json()
    assert r["ok"] and r["text"] == "glb のファイル" and str(tmp_path) not in str(r)     # 場所は画面に渡さない
    out = c.post("/api/registry", json={"alias": "物件A", "glb_token": r["token"], "ng_words": ["鈴木"]})
    assert out.status_code == 200 and out.json()["properties"][0]["glb"] == "あり"
    assert yaml.safe_load(yml.read_text(encoding="utf-8"))["物件A"] == {"glb": str(g), "ng_words": ["鈴木"]}
    bad = c.post("/api/registry", json={"alias": "物件B", "glb_path": "C:/どこにもない/x.glb"})
    assert bad.status_code == 400 and "見つかりません" in bad.json()["detail"]
    assert c.post("/api/registry", json={"alias": "物件C"}).status_code == 400
    assert c.post("/api/registry/pick", json={"what": "exe"}).status_code == 400
