"""物件のファイルの場所（core/paths.py）：glb のフォルダ指定・図面の種類の推定・物件ごとの ng_words。"""
from pathlib import Path

import pytest
import yaml

from core.paths import detect_jww, glb_working_copy, jww_kind, property_files
from core.privacy import ng_words


def _yaml(tmp_path: Path, conf: dict) -> Path:
    p = tmp_path / "local_paths.yaml"
    p.write_text(yaml.safe_dump(conf, allow_unicode=True), encoding="utf-8")
    return p


def _touch(folder: Path, *names: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"x")


def test_glb_folder_with_one_glb(tmp_path):
    d = tmp_path / "物件C"
    _touch(d, "施主名入りの家.glb", "図面.pdf")
    (d / "model").mkdir()
    (d / "model" / "model.glb").write_bytes(b"x")          # 書き出し（下のフォルダ）は数えない
    f = property_files("物件C", _yaml(tmp_path, {"物件C": {"glb": str(d)}}))
    assert f.glb == d / "施主名入りの家.glb"
    assert f.notes == []


@pytest.mark.parametrize("names, word", [((), "まだありません"), (("a.glb", "b.GLB"), "2 つあります")])
def test_glb_folder_without_exactly_one(tmp_path, names, word):
    d = tmp_path / "物件D"
    _touch(d, *names)
    f = property_files("物件D", _yaml(tmp_path, {"物件D": {"glb": str(d)}}))
    assert f.glb is None
    assert word in f.notes[0]
    with pytest.raises(FileNotFoundError) as e:
        glb_working_copy(f)
    assert word in str(e.value)
    assert "a.glb" not in str(e.value)                       # 元のファイル名は出さない


def test_glb_file_path_still_works(tmp_path):
    _touch(tmp_path / "x", "家.glb")
    f = property_files("物件A", _yaml(tmp_path, {"物件A": {"glb": str(tmp_path / "x" / "家.glb")}}))
    assert f.glb == tmp_path / "x" / "家.glb"


@pytest.mark.parametrize("stem, kind", [
    ("○○邸 平面図", "plan"), ("1階平面図", "plan"), ("2階平面図", "plan_2f"), ("２Ｆ平面図", "plan_2f"),
    ("1・2階平面図", "plan"), ("立面図", "elevation"), ("断面図(A-A)", "section"),
    ("展開図", None), ("屋根伏図", None), ("平面図・立面図", None), ("メモ", None),
])
def test_jww_kind(stem, kind):
    assert jww_kind(stem) == kind


def test_detect_jww(tmp_path):
    _touch(tmp_path, "邸 1階平面図.jww", "邸 2階平面図.JWW", "立面図.jww", "断面図.jww", "展開図.jww", "平面図.pdf")
    jww, notes = detect_jww(tmp_path)
    assert {k: p.name for k, p in jww.items()} == {"plan": "邸 1階平面図.jww", "plan_2f": "邸 2階平面図.JWW",
                                                   "elevation": "立面図.jww", "section": "断面図.jww"}
    assert notes == []


def test_detect_jww_two_of_a_kind_is_not_used(tmp_path):
    _touch(tmp_path, "平面図.jww", "平面図（古い）.jww", "立面図.jww")
    jww, notes = detect_jww(tmp_path)
    assert set(jww) == {"elevation"}
    assert "2 つ" in notes[0] and "平面図.jww" not in notes[0]


def test_explicit_jww_mapping_wins(tmp_path):
    _touch(tmp_path / "図面", "a.jww", "平面図.jww")
    f = property_files("物件A", _yaml(tmp_path, {"物件A": {"jww_folder": str(tmp_path / "図面"), "jww": {"plan": "a.jww"}}}))
    assert f.jww == {"plan": tmp_path / "図面" / "a.jww"}


def test_jww_detected_when_not_written(tmp_path):
    _touch(tmp_path / "物件D", "家.glb", "立面図.jww")
    d = str(tmp_path / "物件D")
    f = property_files("物件D", _yaml(tmp_path, {"物件D": {"glb": d, "jww_folder": d}}))
    assert set(f.jww) == {"elevation"}


def test_ng_words_include_each_property(tmp_path):
    p = _yaml(tmp_path, {"ng_words": ["山田"], "物件A": {"glb": "x"}, "物件C": {"glb": "y", "ng_words": ["鈴木", " "]}})
    assert ng_words(p) == ["山田", "鈴木"]
