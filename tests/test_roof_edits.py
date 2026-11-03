"""表で直す：屋根の形（勾配・低い側・軒先の高さ）の修正（core/overrides.py）を確かめる。

勾配は軒先（屋根の上の面のいちばん低い所）を固定して回す。面が複数の屋根は、勾配がそろっているときだけ、
軒先からの高さを比例で伸ばす。向きは片流れだけ。軒先の高さは屋根全体を上下させる。
屋根まで届いていた壁（数値の高さの外壁・単独の壁）は、屋根の下の面の動きについていく。
"""
from __future__ import annotations

import json
import math

import pytest

from core import overrides as ov
from core.builder import build_model
from core.rules import load_rules
from tests.test_roof_multi import RECT, S, T_V, _house, _zmax_at, gable, hip

RULES = {"model": {"scale": 50, "min_feature_mm": 1.0, "upper_floor_plate_mm": 3.0},
         "levels": {"ceiling_height_mm": 2400}}


def _shed():
    """8000×6000 の片流れ（南が低い。軒先 3300、1.5/10）。単独の壁 W9 が屋根の下の面まで届いている。"""
    b = _house([{"footprint": [list(map(float, p)) for p in RECT], "top_plane": {"a": 0.0, "b": 0.15, "c": 3300.0},
                 "bottom_plane": {"a": 0.0, "b": 0.15, "c": 3300.0 - 100 * math.sqrt(1 + 0.0225)}}], RECT, "shed")
    for r in (b["roof"], b["roofs"][0]):
        r.pop("planes")
        r.update(shape="shed", pitch=[1.5, 10], slope=0.15, low_side="S", thickness_mm=100.0, top_z_range_mm=[3300, 4200])
    bottom_mid = 3300.0 - 100 * math.sqrt(1 + 0.0225) + 0.15 * 3000
    b["walls"].append({"id": "W9", "level": "1F", "kind": "standalone", "start": [4000, 1000], "end": [4000, 5000],
                       "thickness_mm": 180, "bottom_mm": 480, "height": round(bottom_mid + 10)})
    b["walls"].append({"id": "W10", "level": "1F", "kind": "standalone", "start": [2000, 1000], "end": [2000, 2000],
                       "thickness_mm": 180, "bottom_mm": 480, "height": 2000})          # 屋根に届いていない
    return b


@pytest.fixture
def alias(tmp_path, monkeypatch):
    monkeypatch.setattr(ov, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(ov, "_company_rules", lambda b: RULES)
    return "物件T"


def _w(b, i):
    return next(w for w in b["walls"] if w["id"] == i)


def test_pitch_rotates_about_the_eave_and_keeps_thickness():
    b = _shed()
    moved, res = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "pitch", "from": 1.5, "to": 3.0}])
    assert res[0]["status"] == "applied"
    for r in (moved["roof"], moved["roofs"][0]):                       # roof と roofs の両方
        assert r["pitch"] == [3.0, 10] and r["top_plane"]["b"] == pytest.approx(0.3)
        assert r["top_plane"]["c"] == pytest.approx(3300.0)            # 軒先（南の端 y=0）の高さはそのまま
        assert r["top_z_range_mm"] == [3300, 5100]
        t_v = r["top_plane"]["c"] - r["bottom_plane"]["c"]
        assert t_v / math.sqrt(1 + 0.09) == pytest.approx(100.0, abs=0.1)   # 面に垂直な厚みは 100 のまま
    assert b["roof"]["pitch"] == [1.5, 10]                             # 元の building は変わらない


def test_walls_that_reached_the_roof_follow_it():
    b = _shed()
    old = _w(b, "W9")["height"]
    moved, _ = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "pitch", "from": 1.5, "to": 3.0}])
    assert _w(moved, "W9")["height"] == pytest.approx(old + 0.15 * 3000 - (100 * math.sqrt(1.09) - 100 * math.sqrt(1.0225)), abs=1)
    assert _w(moved, "W10")["height"] == 2000                           # 屋根に届いていない壁はそのまま


def test_low_side_turns_a_shed_roof():
    b = _shed()
    moved, res = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "low_side", "from": "S", "to": "E"}])
    assert res[0]["status"] == "applied"
    r = moved["roof"]
    assert r["low_side"] == "E" and r["top_plane"]["a"] == pytest.approx(-0.15) and r["top_plane"]["b"] == pytest.approx(0.0)
    assert ov._plane_at(r["top_plane"], 8000, 3000) == pytest.approx(3300.0)            # 東の端が軒先
    assert r["top_z_range_mm"] == [3300, 4500]


def test_eave_tip_moves_the_whole_roof():
    b = _shed()
    moved, _ = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "eave_tip_mm", "from": 3300, "to": 3500}])
    assert moved["roof"]["top_plane"]["c"] == pytest.approx(3500.0) and moved["roof"]["top_z_range_mm"] == [3500, 4400]
    assert _w(moved, "W9")["height"] == _w(b, "W9")["height"] + 200


def test_gable_pitch_scales_heights_and_keeps_ridge_position():
    b = _house(gable(), RECT, "gable")
    moved, res = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "pitch", "from": 3.0, "to": 4.0}])
    assert res[0]["status"] == "applied"
    planes = moved["roof"]["planes"]
    assert [p["top_plane"]["b"] for p in planes] == pytest.approx([0.4, -0.4])
    assert [p["footprint"] for p in planes] == [p["footprint"] for p in b["roof"]["planes"]]   # 棟の位置は同じ
    assert moved["roof"]["top_z_range_mm"] == [3300, 3300 + 1200]
    parts = build_model(moved, load_rules("jisha"))
    assert _zmax_at(parts["roof"].solid, 4000, 3000, r=2.0) == pytest.approx(3300 + 0.4 * 3000, abs=3.0)


def test_refusals(alias):
    with pytest.raises(ValueError, match="向き"):
        ov.add(alias, _house(hip(), RECT, "hip"), "Y1", "low_side", "E", "human:table", "寄棟の向きは変えられない")
    b = _house(gable(), RECT, "gable")
    b["roof"]["planes"][1]["top_plane"] = {"a": 0.0, "b": -0.5, "c": 3300 + 6000 * 0.5}
    with pytest.raises(ValueError, match="勾配が違う"):
        ov.add(alias, b, "Y1", "pitch", 2.0, "human:table", "勾配がそろっていない")
    with pytest.raises(ValueError, match="勾配は"):
        ov.add(alias, _shed(), "Y1", "pitch", 30, "human:table", "急すぎる")
    with pytest.raises(ValueError, match="屋根の下の面"):          # 天井（3024）に近すぎる所まで下げる
        ov.add(alias, _shed(), "Y1", "eave_tip_mm", 3100, "human:table", "下げすぎ")
    item = ov.add(alias, _shed(), "Y1", "pitch", 2.0, "human:table", "図面の勾配 2/10")
    assert (item["from"], item["to"]) == (1.5, 2.0)


@pytest.mark.realdata
def test_real_a_pitch_and_eave_tip_build():
    """物件A：勾配 1.5→2.0、軒先 +200 で、屋根がそのとおり動き、壁が屋根を突き抜けない（屋根の板の上に壁が出ない）。"""
    from core.paths import out_dir
    b = json.loads((out_dir("物件A") / "building.json").read_text(encoding="utf-8"))
    rules = load_rules("jisha")
    base = build_model(ov.apply(b, [])[0], rules)
    moved, res = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "pitch", "to": 2.0},
                              {"id": "OV2", "target": "Y1", "field": "eave_tip_mm", "to": 3500}])
    assert [r["status"] for r in res] == ["applied", "applied"]
    parts = build_model(moved, rules)
    zmax = lambda p: p.solid.bounding_box()[5]
    assert zmax(parts["roof"]) > zmax(base["roof"]) + 200
    for k, p in parts.items():
        assert str(p.solid.status()).endswith("NoError"), k
    assert ov.level_problems(moved, rules) == {}
