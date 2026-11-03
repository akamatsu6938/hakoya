"""予防：物件を会社ルールと比べて「いつもと違う」所を知らせる（core/rule_check.py）を確かめる。"""
from __future__ import annotations

import json

import pytest

from core.rule_check import check_property, differences
from core.rules import load_rules


def _house(ext=162, fl2=None, door_glb=None, window_head=2400):
    levels = [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}]
    stats = {"1F": {"exterior_thickness_measured_mm": ext, "interior_gap_measured_mm": 145}}
    if fl2:
        levels.append({"name": "2F", "fl_mm": fl2, "ceiling_mm": 2400})
    door = {"id": "O1", "kind": "door", "head_fl_mm": 2400, "sill_fl_mm": 0}
    if door_glb:
        door["head_fl_mm"] = door_glb          # 50mm を超える差は、組み立てでそろえない（glb のまま）
    return {"source": {"company_rules": "jisha"}, "ground": {"gl_mm": 0, "foundation_top_mm": 480}, "levels": levels,
            "wall_stats_by_level": stats, "walls": [],
            "openings": [door, {"id": "O2", "kind": "window", "head_fl_mm": window_head, "sill_fl_mm": 900}],
            "roofs": [{"id": "Y1", "kind": "main", "shape": "shed", "pitch": [1.5, 10], "thickness_mm": 100,
                       "eave_mm": {"W": 1, "E": 1, "S": 1, "N": 1}}]}


R = load_rules("jisha")


def test_usual_house_has_no_differences():
    rows = check_property(_house(), R)
    assert rows and all(r["match"] for r in rows)
    assert {r["stat"] for r in rows} >= {"levels.foundation_top", "walls.exterior_measured", "openings.door_head"}


def test_unusual_values_are_reported():
    d = differences(_house(ext=150, door_glb=2000), R)
    msgs = {x["stat"]: x["message"] for x in d}
    assert "会社ルール（162）と違う：150（1F）" in msgs["walls.exterior_measured"]
    assert "確かめてください" in msgs["walls.exterior_measured"]
    assert "FL+2000（O1）" in msgs["openings.door_head"]


def test_values_that_vary_by_house_are_not_reported():
    assert differences(_house(window_head=1800), R) == []                 # 窓の上端は家ごとに変わる
    assert differences(_house(fl2=3624), R) == []                         # 階高 3000 は会社ルールの選択肢にある
    d = differences(_house(fl2=3774), R)                                  # 階高 3150 は選択肢にない
    assert [x["stat"] for x in d] == ["levels.story_height"] and "選択肢 2850・3000" in d[0]["message"]


def test_code_findings_and_ai_summary_show_differences(tmp_path, monkeypatch):
    from core import findings
    from core.ai.tools import ToolContext, call_tool
    monkeypatch.setattr(findings, "out_dir", lambda a: tmp_path / a)
    (tmp_path / "物件T").mkdir()
    b = _house(ext=150)
    (tmp_path / "物件T" / "building.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    fs = findings.code_findings("物件T")
    assert [f["kind"] for f in fs] == ["rule"] and fs[0]["severity"] == "中"
    ctx = ToolContext(alias="物件T", raw=b, building=b, rules=R)
    summary = call_tool(ctx, "get_building", {})
    assert any("外壁の厚み" in m for m in summary["company_rule_check"])
    ok = call_tool(ToolContext(alias="物件T", raw=_house(), building=_house(), rules=R), "get_building", {})
    assert ok["company_rule_check"][0].startswith("会社ルールと比べて、いつもと違う所はない")


@pytest.mark.realdata
def test_real_properties_have_no_differences():
    from core.paths import out_dir
    for a in ("物件A", "物件B"):
        b = json.loads((out_dir(a) / "building.json").read_text(encoding="utf-8"))
        assert differences(b, R) == [], a
