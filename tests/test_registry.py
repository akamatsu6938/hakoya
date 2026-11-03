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


# ---------------------------------------------------------------- 11/4 の見直し

@pytest.mark.parametrize("alias,words,expect", [
    ("山田邸", ["鈴木"], "仮名に"),              # 全体の言葉（山田）が仮名に入る → 書いたあと画面が止まっていた
    ("鈴木邸", ["鈴木"], "仮名に"),              # 足す言葉が仮名に入る
    ("物件B", ["物件A"], "登録済みの仮名"),       # 言葉が登録済みの仮名（物件A）に当たる
    ("物件B", ["123-4"], "数字だけ"),           # 数字だけの言葉は座標に当たって組み立てが止まる
    ("物件B", ["鈴"], "2文字以上"),
    ("物件B.", ["鈴木"], "最後に"), ("NUL", ["鈴木"], "Windows"),
])
def test_register_refuses_alias_and_words_that_would_stop_the_screen(yml, tmp_path, alias, words, expect):
    g = _glb(tmp_path / "in")
    before = yml.read_text(encoding="utf-8")
    with pytest.raises(ValueError) as e:
        rg.register(alias, str(g), None, words, yml)
    assert expect in str(e.value) and "鈴木" not in str(e.value) and "山田" not in str(e.value)
    assert yml.read_text(encoding="utf-8") == before                                   # 何も書かない


def test_alias_with_data_left_from_another_house_is_skipped(yml, tmp_path):
    """手で消した仮名の data/out に前の家の修正・番号が残っていると、新しい家に当たる → 使わない。"""
    left = tmp_path / "out" / "物件B"
    left.mkdir(parents=True)
    (left / "overrides.json").write_text("[]", encoding="utf-8")
    assert rg.next_alias(yml) == "物件C"
    with pytest.raises(ValueError) as e:
        rg.register("物件B", str(_glb(tmp_path / "in")), None, ["鈴木"], yml)
    assert "前のデータ" in str(e.value)


def test_register_keeps_the_bytes_and_rechecks_just_before_writing(tmp_path, monkeypatch):
    """前の中身（CRLF・最後の改行なし）はバイト列のまま残す。書く直前の中身で、もう一度同じ仮名がないか確かめる。"""
    p = tmp_path / "local_paths.yaml"
    p.write_bytes("# メモ\r\nng_words: [山田]\r\n物件A:\r\n  glb: C:/x/a.glb".encode("utf-8"))
    rg.register("物件B", str(_glb(tmp_path / "in")), None, [], p)
    raw = p.read_bytes()
    assert raw.startswith("# メモ\r\nng_words: [山田]\r\n物件A:\r\n  glb: C:/x/a.glb\r\n".encode("utf-8")) and b"\n\n" not in raw
    assert aliases(p) == ["物件A", "物件B"]
    stale = {"物件A": {"glb": "C:/x/a.glb"}}                    # 確かめる前に読んだ中身には 物件C がない（連打で2つ目が来た）
    monkeypatch.setattr(rg, "_conf", lambda path: stale)
    p.write_bytes(p.read_bytes() + "物件C:\r\n  glb: C:/x/c.glb\r\n".encode("utf-8"))
    with pytest.raises(ValueError) as e:
        rg.register("物件C", str(_glb(tmp_path / "in2")), None, [], p)
    assert "もう登録" in str(e.value)


def test_glb_column_does_not_show_drawing_notes(yml, tmp_path):
    """glb のファイルがないとき、一覧の glb の欄に図面の注意（平面図が2つ…）を出さない。"""
    folder = tmp_path / "図面"
    _glb(folder, "1階平面図.jww")
    _glb(folder, "平面図.jww")
    with open(yml, "a", encoding="utf-8") as f:
        f.write(f"物件B:\n  glb: {tmp_path / 'ない.glb'}\n  jww_folder: {folder}\n")
    row = rg.list_properties(yml)["properties"][1]
    assert row["glb"] == "見つかりません"


class _Done:
    def __init__(self, code, out):
        self.returncode, self.stdout = code, out


def test_pick_reads_base64_and_tells_failure_from_cancel(monkeypatch):
    """選ぶ窓の場所は Base64 で受け取る（日本語のパスが化けない）。PowerShell の失敗は「やめた」と分ける。"""
    import base64
    place = "C:\\テスト用☆【物件】\\図面　フォルダ\\家.glb"
    monkeypatch.setattr(rg.subprocess, "run", lambda *a, **k: _Done(0, base64.b64encode(place.encode("utf-8")) + b"\r\n"))
    assert rg.pick("glb") == place
    monkeypatch.setattr(rg.subprocess, "run", lambda *a, **k: _Done(0, b""))
    assert rg.pick("folder") is None
    monkeypatch.setattr(rg.subprocess, "run", lambda *a, **k: _Done(1, b""))
    with pytest.raises(RuntimeError):
        rg.pick("glb")


def test_screen_prefers_the_pasted_place_and_asks_again_after_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    from core import paths
    yml = tmp_path / "local_paths.yaml"
    monkeypatch.setattr(paths, "LOCAL_PATHS", yml)
    wrong, right = _glb(tmp_path / "前に選んだ所"), _glb(tmp_path / "貼り付けた所")
    monkeypatch.setattr(rg, "pick", lambda what: str(wrong))
    c = TestClient(server.app)
    token = c.post("/api/registry/pick", json={"what": "glb"}).json()["token"]
    assert token.isalpha()                                                                 # 数字の言葉に当たらない
    out = c.post("/api/registry", json={"alias": "物件A", "glb_token": token, "glb_path": str(right), "ng_words": ["鈴木"]})
    assert out.status_code == 200 and property_files("物件A", yml).glb == right
    lost = c.post("/api/registry", json={"alias": "物件B", "glb_token": "zzzzzzzzzzzzzzzz"})
    assert lost.status_code == 400 and "もう一度" in lost.json()["detail"]
