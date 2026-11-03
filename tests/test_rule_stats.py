"""会社ルールの言語化の集計（core/rule_stats.py）を、架空の家と実データで確かめる。"""
from __future__ import annotations

import json

import pytest

from core import rules as rules_mod
from core import rule_stats as rs
from core.rules import load_rules


def _house(fl2=None, ext=162, doors=(2400,), windows=(2400,), gl_assumed=False, exceptions=(), door_text=False):
    levels = [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}]
    stats = {"1F": {"exterior_thickness_measured_mm": ext, "interior_gap_measured_mm": 145}}
    if fl2:
        levels.append({"name": "2F", "fl_mm": fl2, "ceiling_mm": 2400})
        stats["2F"] = {"exterior_thickness_measured_mm": ext, "interior_gap_measured_mm": 145}
    ops = []
    for i, h in enumerate(doors, 1):
        o = {"id": f"O{i}", "kind": "door", "head_fl_mm": 2400 if abs(h - 2400) <= 50 else h, "sill_fl_mm": 0}
        if h != o["head_fl_mm"]:
            if door_text:   # 古い building.json：注記の文だけ
                o["assumption"] = [f"door_top: glb の上端 FL+{h} を、会社ルール openings.door_top_fl_mm（FL+2400）にそろえた"]
            else:
                o["glb_original"] = {"head_fl_mm": h}
        ops.append(o)
    for j, h in enumerate(windows, len(ops) + 1):
        ops.append({"id": f"O{j}", "kind": "window", "head_fl_mm": h, "sill_fl_mm": 900})
    ground = {"gl_mm": 0, "foundation_top_mm": 480}
    if gl_assumed:
        ground["gl_assumption"] = "敷地がないため GL＝Z 0 とした"
    return {"source": {"company_rules": "jisha"}, "ground": ground, "levels": levels, "wall_stats_by_level": stats,
            "openings": ops, "walls": [], "glb_exceptions": [{"kind": k} for k in exceptions],
            "roofs": [{"id": "Y1", "kind": "main", "shape": "shed", "pitch": [1.5, 10], "thickness_mm": 100,
                       "eave_mm": {"W": 1, "E": -2, "S": 1, "N": 1}}]}


@pytest.fixture
def learned_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rules_mod, "LEARNED_DIR", tmp_path)
    return tmp_path


def _by_id(stats):
    return {s["id"]: s for s in stats}


def test_same_values_match_the_rules():
    s = _by_id(rs.compute([("物件A", _house()), ("物件B", _house(fl2=3624))], load_rules("jisha"), {}))
    for sid in ("levels.foundation_top", "levels.fl1", "levels.ceiling", "walls.exterior_measured", "roofs.thickness"):
        assert s[sid]["status"] == "same" and s[sid]["rule_match"] is True, sid
    assert s["levels.foundation_top"]["agreement"] == "2件中2件で 480（会社ルール 480と一致）"
    assert s["walls.interior_gap"]["status"] == "info"                    # 内壁の 130 は 作者の決め。比べない


def test_door_head_counts_glb_values_before_snapping():
    new = _house(doors=(2370, 2400))
    old = _house(doors=(2370,), door_text=True)                           # 注記の文から読む
    s = _by_id(rs.compute([("物件A", new), ("物件B", old)], load_rules("jisha"), {}))["openings.door_head"]
    assert s["status"] == "same" and s["rule_match"] is True
    assert "glb では FL+2370 が2か所" in s["agreement"]


def test_story_height_only_for_two_storeys_and_few():
    s = _by_id(rs.compute([("物件A", _house()), ("物件B", _house(fl2=3624))], load_rules("jisha"), {}))
    st = s["levels.story_height"]
    assert st["n"] == 1 and st["status"] == "few" and st["agreement"].startswith("物件B だけ")


def test_varies_gives_glb_candidate_and_differs_gives_value_candidate(learned_dir):
    a = _house(ext=150, windows=(2000, 2000, 2400))
    b = _house(ext=150, windows=(2400, 2400))
    stats = rs.compute([("物件A", a), ("物件B", b)], load_rules("jisha"), {})
    s = _by_id(stats)
    assert s["openings.window_head"]["status"] == "varies"
    assert "は予備の値" in s["openings.window_head"]["agreement"]
    assert s["walls.exterior_measured"]["status"] == "same" and s["walls.exterior_measured"]["rule_match"] is False
    cands = {c["signature"]: c for c in rs.code_candidates(stats, "jisha")}
    v = cands["value:walls.exterior_thickness_mm"]
    assert v["kind"] == "value" and v["to"] == 150
    assert cands["glb:openings.window_head"]["kind"] == "glb"
    assert not any("interior" in k for k in cands)                       # 内壁は候補にしない


def test_exception_in_most_properties_is_a_skill_candidate_once(learned_dir):
    items = [("物件A", _house(exceptions=("agari_kamachi",))), ("物件B", _house(exceptions=("agari_kamachi", "stairs_area")))]
    stats = rs.compute(items, load_rules("jisha"), {})
    s = _by_id(stats)
    assert s["exceptions.agari_kamachi"]["status"] == "same" and s["exceptions.stairs_area"]["status"] == "info"
    assert "skill:exceptions.agari_kamachi" in {c["signature"] for c in rs.code_candidates(stats, "jisha")}
    (learned_dir / "jisha.learned.json").write_text(json.dumps({"rejected": [{"signature": "skill:exceptions.agari_kamachi"}]}),
                                                     encoding="utf-8")
    assert "skill:exceptions.agari_kamachi" not in {c["signature"] for c in rs.code_candidates(stats, "jisha")}


def test_assumed_gl_is_not_counted():
    s = _by_id(rs.compute([("物件A", _house(gl_assumed=True)), ("物件B", _house())], load_rules("jisha"), {}))
    assert s["levels.foundation_top"]["n"] == 1


def test_repeated_human_corrections_are_counted():
    a, b = _house(), _house()
    a["walls"] = [{"id": "W3", "kind": "standalone"}]
    b["walls"] = [{"id": "W9", "kind": "standalone"}]
    ovs = {"物件A": [{"target": "W3", "field": "thickness_mm"}], "物件B": [{"target": "W9", "field": "thickness_mm"}]}
    s = _by_id(rs.compute([("物件A", a), ("物件B", b)], load_rules("jisha"), ovs))
    assert s["corrections.standalone.thickness_mm"]["status"] == "same"


@pytest.mark.realdata
def test_real_properties_oracle():
    """物件A（平屋）・物件B（2階建て）の答え合わせ（2026-11-03 に確かめた値）。
    ★物件C〜F（11/3 夜に追加）も組み立てると数に入るので、物件A・B だけで集計する。"""
    s = _by_id(rs.compute([e for e in rs.entries("jisha") if e[0] in ("物件A", "物件B")], load_rules("jisha")))
    for sid in ("levels.foundation_top", "levels.fl1", "levels.ceiling", "walls.exterior_measured",
                "openings.door_head", "roofs.shape", "roofs.thickness"):
        assert s[sid]["status"] == "same", sid
    assert s["levels.foundation_top"]["mode"] == 480 and s["walls.exterior_measured"]["mode"] == 162
    assert "FL+2370 が5か所" in s["openings.door_head"]["agreement"]
    assert s["openings.window_head"]["status"] == "varies" and s["roofs.pitch"]["status"] == "varies"
    assert s["levels.story_height"]["status"] == "few"
    assert s["exceptions.agari_kamachi"]["status"] == "same"
    from core.privacy import assert_clean
    assert_clean(json.dumps(s, ensure_ascii=False), "集計")
