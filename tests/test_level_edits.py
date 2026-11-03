"""表で直す：階の高さ（1階の床・天井・階高）の修正（core/overrides.py）を確かめる。

床の高さを変えると、その階と上の階の絶対の高さ（壁・窓・部屋・ポーチの天井・屋根）が同じだけずれ、
基礎・外壁の下端・ポーチはそのまま。家として成り立たない値（天井が屋根に近すぎる等）は、保存の前に断る。
"""
from __future__ import annotations

import json

import pytest

from core import overrides as ov

RULES = {"model": {"scale": 50, "min_feature_mm": 1.0, "upper_floor_plate_mm": 3.0},
         "levels": {"ceiling_height_mm": 2400}}


def _plane(c):
    return {"a": 0.15, "b": 0.0, "c": c}


def _one_story():
    roof = {"shape": "shed", "pitch": [1.5, 10], "top_plane": _plane(4000.0), "bottom_plane": _plane(3899.0),
            "top_z_range_mm": [4000, 4900], "footprint": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
            "eave_mm": {"W": 1, "E": 1, "S": 1, "N": 1}}
    return {"source": {"company_rules": "jisha"},
            "foundation": {"top_mm": 480},
            "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
            "walls": [{"id": "W1", "level": "1F", "kind": "exterior", "bottom_mm": 480, "height": "to_roof", "top_glb_mm": 4000},
                      {"id": "W2", "level": "1F", "kind": "interior", "bottom_mm": 624, "height": 3024},
                      {"id": "W3", "level": "1F", "kind": "standalone", "bottom_mm": 480, "height": 2824},
                      {"id": "W4", "level": "1F", "kind": "interior", "bottom_mm": 2824, "height": 3024}],   # 下がり壁
            "openings": [{"id": "O1", "wall": "W1", "level": "1F", "kind": "window", "sill_mm": 1524, "head_mm": 2624,
                          "sill_fl_mm": 900, "head_fl_mm": 2000}],
            "rooms": [{"id": "R1", "level": "1F", "floor_mm": 624}, {"id": "R2", "level": "1F", "floor_mm": 384}],
            "porches": [{"id": "P1", "bottom_mm": 0, "top_mm": 384}],
            "porch_soffits": [{"id": "S1", "z_mm": 2824}],
            "roof": roof,
            "roofs": [{"id": "Y1", "level": "1F", "kind": "main", **roof}]}     # 面の式は roof と同じ dict（共有）


def _two_story():
    lean = {"id": "Y1", "level": "1F", "kind": "lean_to", "top_plane": _plane(3700.0), "bottom_plane": _plane(3600.0),
            "top_z_range_mm": [3700, 4000], "footprint": [[0, 0], [2000, 0], [2000, 4000], [0, 4000]]}
    main = {"shape": "shed", "top_plane": _plane(6700.0), "bottom_plane": _plane(6600.0),
            "top_z_range_mm": [6700, 7000], "footprint": [[2000, 0], [6000, 0], [6000, 4000], [2000, 4000]]}
    return {"source": {"company_rules": "jisha"}, "foundation": {"top_mm": 480},
            "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400},
                       {"name": "2F", "fl_mm": 3624, "ceiling_mm": 2400, "outline": [[2000, 0], [6000, 0], [6000, 4000], [2000, 4000]]}],
            "walls": [{"id": "W1", "level": "1F", "kind": "interior", "bottom_mm": 624, "height": 3024},
                      {"id": "W2", "level": "2F", "kind": "exterior", "bottom_mm": 3624, "height": "to_roof", "top_glb_mm": 6700},
                      {"id": "W3", "level": "2F", "kind": "interior", "bottom_mm": 3624, "height": 6024}],
            "openings": [{"id": "O1", "wall": "W2", "level": "2F", "kind": "window", "sill_mm": 4524, "head_mm": 5624,
                          "sill_fl_mm": 900, "head_fl_mm": 2000}],
            "rooms": [{"id": "R1", "level": "1F", "floor_mm": 624}, {"id": "R2", "level": "2F", "floor_mm": 3624}],
            "porch_soffits": [{"id": "S1", "level": "1F", "z_mm": 2887}],
            "roof": main,
            "roofs": [lean, {"id": "Y2", "level": "2F", "kind": "main", **main}]}


@pytest.fixture
def alias(tmp_path, monkeypatch):
    monkeypatch.setattr(ov, "out_dir", lambda a: tmp_path / a)
    return "物件T"


def _get(b, coll, id_):
    return next(x for x in b[coll] if x["id"] == id_)


def test_raise_first_floor_moves_everything_above_but_not_foundation():
    b = _one_story()
    applied, res = ov.apply(b, [{"id": "OV1", "target": "level:1F", "field": "fl_mm", "from": 624, "to": 724}])
    assert res[0]["status"] == "applied"
    assert applied["levels"][0]["fl_mm"] == 724
    assert _get(applied, "walls", "W1")["bottom_mm"] == 480                        # 基礎の上から立つ外壁の下端はそのまま
    assert _get(applied, "walls", "W1")["top_glb_mm"] == 4100
    assert _get(applied, "walls", "W2")["bottom_mm"] == 724 and _get(applied, "walls", "W2")["height"] == 3124
    assert _get(applied, "walls", "W3")["bottom_mm"] == 480 and _get(applied, "walls", "W3")["height"] == 2924
    assert _get(applied, "walls", "W4")["bottom_mm"] == 2924                       # 下がり壁も一緒に
    o = _get(applied, "openings", "O1")
    assert (o["sill_mm"], o["head_mm"], o["sill_fl_mm"], o["head_fl_mm"]) == (1624, 2724, 900, 2000)
    assert [r["floor_mm"] for r in applied["rooms"]] == [724, 484]                  # 土間も同じ階なので一緒に
    assert applied["porches"][0]["top_mm"] == 384                                  # ポーチ（地面の上）はそのまま
    assert applied["porch_soffits"][0]["z_mm"] == 2924
    for r in (applied["roof"], applied["roofs"][0]):                                # roof と roofs の両方。二重にずれない
        assert r["top_plane"]["c"] == 4100.0 and r["bottom_plane"]["c"] == 3999.0
        assert r["top_z_range_mm"] == [4100, 5000]
    assert b["roof"]["top_plane"]["c"] == 4000.0 and b["walls"][1]["height"] == 3024   # 元の building は変わらない


def test_ceiling_change_retops_walls_up_to_the_ceiling_only():
    b = _one_story()
    applied, _ = ov.apply(b, [{"id": "OV1", "target": "level:1F", "field": "ceiling_mm", "from": 2400, "to": 2300}])
    assert _get(applied, "walls", "W2")["height"] == 2924 and _get(applied, "walls", "W4")["height"] == 2924
    assert _get(applied, "walls", "W3")["height"] == 2824                          # 天井まででない壁はそのまま
    assert applied["roof"]["top_plane"]["c"] == 4000.0                              # 屋根は動かない


def test_story_height_moves_upper_floor_and_main_roof_only():
    b = _two_story()
    applied, res = ov.apply(b, [{"id": "OV1", "target": "level:2F", "field": "fl_mm", "from": 3624, "to": 3724}])
    assert res[0]["status"] == "applied"
    assert _get(applied, "walls", "W1")["height"] == 3024                          # 1階はそのまま
    assert _get(applied, "walls", "W2")["bottom_mm"] == 3724 and _get(applied, "walls", "W3")["height"] == 6124
    assert _get(applied, "openings", "O1")["head_mm"] == 5724
    assert [r["floor_mm"] for r in applied["rooms"]] == [624, 3724]
    assert applied["porch_soffits"][0]["z_mm"] == 2887                             # 1階のポーチの天井はそのまま
    assert applied["roofs"][0]["top_plane"]["c"] == 3700.0                         # 下屋はそのまま
    assert applied["roofs"][1]["top_plane"]["c"] == 6800.0 and applied["roof"]["top_plane"]["c"] == 6800.0


def test_first_floor_change_in_two_story_moves_second_floor_too():
    applied, _ = ov.apply(_two_story(), [{"id": "OV1", "target": "level:1F", "field": "fl_mm", "from": 624, "to": 674}])
    assert [lv["fl_mm"] for lv in applied["levels"]] == [674, 3674]
    assert applied["roofs"][0]["top_plane"]["c"] == 3750.0 and applied["roofs"][1]["top_plane"]["c"] == 6750.0


def test_add_refuses_values_that_do_not_fit_the_house(alias, monkeypatch):
    monkeypatch.setattr(ov, "_company_rules", lambda b: RULES)
    b = _one_story()     # 屋根の下の面のいちばん低い所は 3899（x=0）
    with pytest.raises(ValueError, match="屋根の下の面"):
        ov.add(alias, b, "level:1F", "ceiling_mm", 3300, "human:table", "天井を上げすぎ")
    with pytest.raises(ValueError, match="窓・ドアの上端"):
        ov.add(alias, b, "level:1F", "ceiling_mm", 1900, "human:table", "窓より低い天井")
    with pytest.raises(ValueError, match="基礎の上"):
        ov.add(alias, b, "level:1F", "fl_mm", 500, "human:table", "基礎に近すぎる")
    with pytest.raises(ValueError, match="天井の高さは"):
        ov.add(alias, b, "level:1F", "ceiling_mm", 9000, "human:table", "範囲の外")
    item = ov.add(alias, b, "level:1F", "fl_mm", 700, "human:table", "床を上げる")      # 屋根も一緒に上がるので通る
    assert (item["from"], item["to"]) == (624, 700)
    assert ov.load(alias)[0]["target"] == "level:1F"


def test_add_refuses_story_height_that_hits_lower_ceiling(alias, monkeypatch):
    monkeypatch.setattr(ov, "_company_rules", lambda b: RULES)
    with pytest.raises(ValueError, match="上の階の床板"):
        ov.add(alias, _two_story(), "level:2F", "fl_mm", 3100, "human:table", "階高を縮めすぎ")


def test_existing_problem_does_not_block_other_level_edits(alias, monkeypatch):
    monkeypatch.setattr(ov, "_company_rules", lambda b: RULES)
    b = _one_story()
    b["openings"][0]["head_fl_mm"] = 2463                                         # もともと天井より高い窓（glb の例外）
    ov.add(alias, b, "level:1F", "fl_mm", 650, "human:table", "床だけ直す")       # 新しい問題ではないので通る


def test_broken_level_override_is_skipped_and_others_still_apply():
    b = _one_story()
    items = [{"id": "OV1", "target": "level:1F", "field": "fl_mm", "from": 624, "to": "高い"},
             {"id": "OV2", "target": "W2", "field": "thickness_mm", "to": 130}]
    b["walls"][1]["thickness_mm"] = 145
    applied, res = ov.apply(b, items)
    assert res[0]["status"] == "failed" and applied["levels"][0]["fl_mm"] == 624   # 半端に当たらない
    assert res[1]["status"] == "applied" and _get(applied, "walls", "W2")["thickness_mm"] == 130
    applied2, res2 = ov.apply(b, [{"id": "OV1", "target": "level:9F", "field": "fl_mm", "to": 700}])
    assert res2[0]["status"] == "failed"


@pytest.mark.realdata
def test_real_a_raise_floor_rebuilds_model_shifted():
    """物件A：1階の床を 100 上げると、基礎はそのままで、屋根と内壁が 100 上がった模型になる。"""
    from core.builder import build_model
    from core.paths import out_dir
    from core.rules import load_rules
    b = json.loads((out_dir("物件A") / "building.json").read_text(encoding="utf-8"))
    rules = load_rules("jisha")
    base = build_model(ov.apply(b, [])[0], rules)
    moved, res = ov.apply(b, [{"id": "OV1", "target": "level:1F", "field": "fl_mm", "from": 624, "to": 724}])
    assert res[0]["status"] == "applied"
    parts = build_model(moved, rules)
    zmax = lambda p: p.solid.bounding_box()[5]
    assert abs(zmax(parts["roof"]) - zmax(base["roof"]) - 100) < 1
    assert abs(zmax(parts["floor1"]) - zmax(base["floor1"]) - 100) < 1
    assert moved["foundation"] == b["foundation"]                    # 基礎の帯の高さはそのまま（土間は同じ階なので一緒に上がる）
    assert abs(zmax(parts["foundation"]) - zmax(base["foundation"])) < 1
    assert ov.level_problems(moved, rules) == {}
