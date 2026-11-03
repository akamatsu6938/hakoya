"""人が承認した修正（core/overrides.py）の当て直し・取り消しを確かめる（実データ不要）。"""
from __future__ import annotations

import pytest

from core import overrides as ov


def _building():
    return {"levels": [{"name": "1F", "fl_mm": 624}],
            "walls": [{"id": "W1", "level": "1F", "kind": "interior", "thickness_mm": 130},
                      {"id": "W2", "level": "1F", "kind": "interior", "thickness_mm": 145}],
            "openings": [{"id": "O1", "wall": "W1", "kind": "window", "head_mm": 3024, "head_fl_mm": 2400,
                          "sill_mm": 2524, "sill_fl_mm": 1900},
                         {"id": "O2", "wall": "W2", "kind": "door", "head_mm": 3024, "head_fl_mm": 2400}],
            "roof": {"pitch": [1.5, 10]}}


@pytest.fixture
def alias(tmp_path, monkeypatch):
    monkeypatch.setattr(ov, "out_dir", lambda a: tmp_path / a)
    return "物件T"


def test_add_apply_and_undo(alias):
    b = _building()
    o1 = ov.add(alias, b, "O1", "head_mm", 2824, "jww:elevation:dim#40", "立面図の窓の上端に合わせる")
    o2 = ov.add(alias, b, "W2", "thickness_mm", 130, "majority:interior", "ほかの内壁とそろえる")
    assert (o1["id"], o1["from"]) == ("OV1", 3024) and o2["id"] == "OV2"
    applied, results = ov.apply(b, ov.load(alias))
    assert [r["status"] for r in results] == ["applied", "applied"]
    o = next(x for x in applied["openings"] if x["id"] == "O1")
    assert o["head_mm"] == 2824 and o["head_fl_mm"] == 2200      # FL からの高さも一緒に直る
    assert b["openings"][0]["head_mm"] == 3024                    # 元の building は変わらない
    ov.undo(alias, "OV1")                                         # 1件だけ取り消せる
    applied, _ = ov.apply(b, ov.load(alias))
    assert next(x for x in applied["openings"] if x["id"] == "O1")["head_mm"] == 3024
    assert next(x for x in applied["walls"] if x["id"] == "W2")["thickness_mm"] == 130


def test_reapply_after_rebuild_reports_problems(alias):
    b = _building()
    ov.add(alias, b, "O1", "head_mm", 2824, "jww:elevation:dim#40", "立面図に合わせる")
    ov.add(alias, b, "W2", "thickness_mm", 130, "majority:interior", "そろえる")
    rebuilt = _building()
    rebuilt["openings"][0]["head_mm"] = 2994                      # 入れ直しで自動の値が変わった
    rebuilt["walls"] = [w for w in rebuilt["walls"] if w["id"] != "W2"]   # W2 がなくなった
    _, results = ov.apply(rebuilt, ov.load(alias))
    assert [r["status"] for r in results] == ["applied_changed", "failed"]


def test_delete_wall_also_removes_its_openings(alias):
    b = _building()
    ov.add(alias, b, "W2", ov.DELETE, None, "human:table", "余分な壁")
    applied, results = ov.apply(b, ov.load(alias))
    assert results[0]["status"] == "applied"
    assert [w["id"] for w in applied["walls"]] == ["W1"]
    assert [o["id"] for o in applied["openings"]] == ["O1"]


def test_unknown_target_or_field_is_refused(alias):
    b = _building()
    with pytest.raises(KeyError):
        ov.add(alias, b, "W9", "thickness_mm", 130, "human:table", "x")
    with pytest.raises(KeyError):
        ov.add(alias, b, "W1", "color", "red", "human:table", "x")


def test_roof_field(alias):
    b = _building()
    ov.add(alias, b, "roof", "pitch", [2.0, 10], "jww:section:dim#3", "断面図の勾配")
    applied, _ = ov.apply(b, ov.load(alias))
    assert applied["roof"]["pitch"] == [2.0, 10]


def _roofed():
    b = _building()
    main = {"shape": "shed", "pitch": [1.5, 10], "eave_mm": {"W": 1, "E": 1, "S": 1, "N": 1},
            "footprint": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]}
    b["roof"] = main
    b["roofs"] = [{"id": "Y1", "kind": "main", "level": "1F", **main}]   # 中身（eave_mm）は roof と同じ dict（メモリ上の building と同じ）
    return b


def test_roof_eave_override_mirrors_main_roof_and_keeps_glb(alias):
    b = _roofed()
    item = ov.add(alias, b, "Y1", "eave_mm.S", 450, "human:number:450", "図面だと 450")
    assert (item["from"], item["to"]) == (1, 450)
    applied, results = ov.apply(b, ov.load(alias))
    assert results[0]["status"] == "applied"
    y1 = applied["roofs"][0]
    assert y1["eave_mm"]["S"] == 450 and y1["eave_glb_mm"]["S"] == 1
    assert applied["roof"]["eave_mm"]["S"] == 450 and applied["roof"]["eave_glb_mm"]["S"] == 1   # 平屋の組み立てが読む roof にも
    assert b["roof"]["eave_mm"]["S"] == 1 and b["roofs"][0]["eave_mm"]["S"] == 1                # 元の building は変わらない
    assert "eave_glb_mm" not in b["roofs"][0]
    ov.undo(alias, item["id"])
    applied, _ = ov.apply(b, ov.load(alias))
    assert applied["roofs"][0]["eave_mm"]["S"] == 1 and "eave_glb_mm" not in applied["roofs"][0]


def test_roof_eave_refusals(alias):
    b = _roofed()
    with pytest.raises(ValueError):
        ov.add(alias, b, "Y1", "eave_mm.S", 5000, "human:number:5000", "大きすぎる")
    with pytest.raises(KeyError):
        ov.add(alias, b, "Y1", "eave_mm.X", 100, "human:number:100", "そんな辺はない")
    with pytest.raises(KeyError):
        ov.add(alias, b, "Y1", "_delete", None, "human", "屋根は取り除けない")
    _, res = ov.apply(b, [{"id": "OV9", "target": "Y1", "field": "_delete"}])
    assert res[0]["status"] == "failed"
