"""アプリの Claude に渡す道具（core/ai/tools.py）を、架空の家で確かめる（実データ不要）。
特に「Claude は数値を作らず、数値はコードが出どころから計算する」（SPEC §2-4）を確かめる。"""
from __future__ import annotations

import pytest

from core.ai.tools import ToolContext, call_tool
from core.rules import load_rules


def _ctx():
    walls = [{"id": "W1", "kind": "interior", "thickness_mm": 130, "level": "1F"},
             {"id": "W2", "kind": "interior", "thickness_mm": 130, "level": "1F"},
             {"id": "W3", "kind": "interior", "thickness_mm": 145, "level": "1F"},
             {"id": "W4", "kind": "exterior", "thickness_mm": 162, "level": "1F"}]
    ops = [{"id": "O1", "wall": "W4", "kind": "window", "width_mm": 1700, "offset_mm": 100, "sill_fl_mm": 1900,
            "head_fl_mm": 2400, "head_mm": 3024, "sill_mm": 2524},
           {"id": "O2", "wall": "W1", "kind": "door", "width_mm": 790, "offset_mm": 100, "sill_fl_mm": 0,
            "head_fl_mm": 2370, "head_mm": 2994, "sill_mm": 624}]
    raw = {"source": {"company_rules": "jisha"}, "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
           "ground": {"foundation_top_mm": 480}, "walls": walls, "openings": ops, "roof": {}}
    import copy
    b = copy.deepcopy(raw)
    b["openings"][0]["head_fl_mm"] = 2200   # 人が修正した後
    return ToolContext(alias="物件T", raw=raw, building=b, rules=load_rules("jisha"))


def test_majority_thickness_is_computed_by_code():
    ctx = _ctx()
    r = call_tool(ctx, "propose_override", {"target": "W3", "field": "thickness_mm", "value_source": "majority:interior",
                                            "reason": "ほかの内壁とそろえる"})
    assert r["proposal"]["from"] == 145 and r["proposal"]["to"] == 130.0


def test_rule_and_glb_sources():
    ctx = _ctx()
    r = call_tool(ctx, "propose_override", {"target": "O2", "field": "head_fl_mm", "value_source": "rule:openings.door_top_fl_mm",
                                            "reason": "室内ドアは 2400"})
    assert r["proposal"]["to"] == 2400.0
    r = call_tool(ctx, "propose_override", {"target": "O1", "field": "head_fl_mm", "value_source": "glb", "reason": "元に戻す"})
    assert r["proposal"]["from"] == 2200 and r["proposal"]["to"] == 2400.0


def test_number_only_from_human_words():
    ctx = _ctx()
    r = call_tool(ctx, "propose_override", {"target": "O1", "field": "width_mm", "value_source": "number", "number": 900,
                                            "reason": "900 にしたい（人の一言）"})
    assert r["proposal"]["to"] == 900.0 and r["proposal"]["value_source"] == "human:number:900"
    r = call_tool(ctx, "propose_override", {"target": "O1", "field": "width_mm", "value_source": "number", "reason": "x"})
    assert "error" in r


@pytest.mark.parametrize("args, msg", [
    ({"target": "W9", "field": "thickness_mm", "value_source": "majority:interior", "reason": "x"}, "ありません"),
    ({"target": "W1", "field": "color", "value_source": "glb", "reason": "x"}, "直せる項目"),
    ({"target": "W1", "field": "thickness_mm", "value_source": "majority:interior", "reason": "x"}, "すでに"),
    ({"target": "O1", "field": "head_fl_mm", "value_source": "jww:elevation:dim#4", "reason": "x"}, "平面図だけ"),   # 立面の図面はまだ読めない
    ({"target": "O1", "field": "head_fl_mm", "value_source": "2300", "reason": "x"}, "使えません"),
])
def test_bad_proposals_are_refused_in_japanese(args, msg):
    r = call_tool(_ctx(), "propose_override", args)
    assert msg in r["error"]


def test_question_needs_2_or_3_options():
    ctx = _ctx()
    assert "error" in call_tool(ctx, "ask_question", {"question": "?", "options": ["a"]})
    r = call_tool(ctx, "ask_question", {"question": "窓の上端は？", "options": ["FL+2200", "FL+2400"], "evidence": "O1"})
    assert r["queued"] == "Q1" and ctx.questions[0]["options"] == ["FL+2200", "FL+2400"]


def test_summary_and_unknown_tool():
    ctx = _ctx()
    s = call_tool(ctx, "get_building", {})
    assert s["walls"] == {"interior": 3, "exterior": 1} and s["company_rules"]["walls"]["interior_thickness_mm"] == 130
    assert "error" in call_tool(ctx, "make_numbers", {})
    assert "error" in call_tool(ctx, "get_elements", {"view": "top"})


def _multi_ctx():
    ctx = _ctx()
    b = ctx.building
    b["levels"].append({"name": "2F", "fl_mm": 3624, "ceiling_mm": 2400,
                        "floor_openings": [{"id": "H1", "kind": "stairwell", "area_m2": 3.44, "polygon": [[0, 0], [1, 0], [1, 1]]}]})
    b["walls"].append({"id": "W5", "kind": "exterior", "thickness_mm": 162, "level": "2F"})
    b["roofs"] = [{"id": "Y1", "kind": "lean_to", "level": "1F", "low_side": "S"},
                  {"id": "Y2", "kind": "main", "level": "2F", "low_side": "E"}]
    return ctx


def test_building_summary_for_two_storeys():
    single = call_tool(_ctx(), "get_building", {})
    assert "by_level" not in single and "roofs" not in single          # 平屋は今までどおり
    s = call_tool(_multi_ctx(), "get_building", {})
    assert s["by_level"]["1F"]["walls"] == {"interior": 3, "exterior": 1} and s["by_level"]["2F"]["walls"] == {"exterior": 1}
    assert s["by_level"]["2F"]["floor_openings"] == [{"id": "H1", "kind": "stairwell", "area_m2": 3.44}]
    assert [(r["id"], r["kind"], r["level"]) for r in s["roofs"]] == [("Y1", "lean_to", "1F"), ("Y2", "main", "2F")]


def test_plan2_only_for_two_storeys():
    assert "平屋" in call_tool(_ctx(), "render_view", {"view": "plan2"})["error"]
    assert "平屋" in call_tool(_ctx(), "get_elements", {"view": "plan2"})["error"]


def _roof_ctx():
    ctx = _ctx()
    eave = {"W": 1, "E": 1, "S": 1, "N": 1}
    roof = {"id": "Y1", "kind": "main", "level": "1F", "pitch": [1.5, 10], "low_side": "W", "eave_mm": dict(eave),
            "footprint": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]}
    ctx.raw["roofs"] = [dict(roof)]
    ctx.building["roofs"] = [dict(roof, eave_mm=dict(eave, S=300))]        # 人が南を 300 に直した後
    return ctx


def test_roof_eave_proposals_use_only_human_numbers():
    ctx = _roof_ctx()
    ctx.human_text = "図面だと軒が４５０ｍｍ出ている"                        # 全角でも読める
    r = call_tool(ctx, "propose_override", {"target": "Y1", "field": "eave_mm.S", "value_source": "number",
                                            "number": 450, "reason": "図面の値"})
    assert (r["proposal"]["from"], r["proposal"]["to"]) == (300, 450.0)
    r = call_tool(ctx, "propose_override", {"target": "Y1", "field": "eave_mm.N", "value_source": "number",
                                            "number": 600, "reason": "推測"})
    assert "人の一言にない" in r["error"]                                    # Claude が作った数値は断る
    r = call_tool(ctx, "propose_override", {"target": "Y1", "field": "eave_mm.S", "value_source": "glb", "reason": "元に戻す"})
    assert r["proposal"]["to"] == 1.0
    assert "eave_mm" in call_tool(ctx, "propose_override", {"target": "Y1", "field": "pitch", "value_source": "glb",
                                                             "reason": "x"})["error"]
    items = call_tool(ctx, "get_elements", {"ids": ["Y1"]})["elements"]
    assert items[0]["eave_mm"]["S"] == 300 and "footprint" not in items[0]
    assert call_tool(ctx, "get_elements", {"ids": ["Y1:S"]})["elements"][0]["id"] == "Y1"   # 軒の番号でも屋根が返る
