"""★11/5 デモ用（作者「袖壁は屋根ではなく家に」「袖壁全体で 180」「下が塗られていないのはおかしい」「下に基礎がないのもおかしい」）の直し。
架空の形で確かめる（実データ不要）。
- 家の角を越えて袖壁と重なる外壁は、家の角で止める（adapters/walls.py stop_at_house_corner）
- 両側とも外の単独の壁は、会社ルールの単独の壁の厚み（walls.standalone_thickness_mm）
- 家の外の単独の壁（袖壁）と、その上をつなぐ梁は、まるごと1階の部品（core/builder.py・core/builder_multi.py）
- 外壁の厚みを直しても、外の面は動かない（core/overrides.py）
- 形の検査（core/shape_checks.py）と、直す案の確かめ（core/ai/tools.py）
"""
from __future__ import annotations

import json

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from adapters import walls as wl
from core.builder import build_model, drop_specks, keep_below, prism, union_all, wall_rect
from core.export import overlap_checks
from core.rules import load_rules
from tests.test_builder import _tiny_house

HOUSE = Polygon([(-8280, 90), (-820, 90), (-820, -11010), (-8280, -11010)])     # デモ用の家の外形（基礎の外形）


def _wall(x0, y0, x1, y1, t=162.0, kind="exterior", top=None, bottom=480.0):
    return wl.Wall(start=np.array([x0, y0], float), end=np.array([x1, y1], float), thickness=t, kind=kind,
                   bottom=bottom, top=top)


def _demo_corner():
    """デモ用の西の額縁：南北の外壁が家の角を越えて袖壁の先まで伸び、袖壁（180）と重なる。"""
    south = _wall(-9190, -10929, -820, -10929)
    north = _wall(-820, 9, -9190, 9)
    s_out = _wall(-9190, -10920, -8645, -10920, t=180, kind="standalone", top=3481)
    s_in = _wall(-8645, -10920, -8280, -10920, t=180, kind="standalone", top=3024)
    n_sl = _wall(-9190, 0, -8280, 0, t=180, kind="standalone", top=3481)
    beam = _wall(-9100, -10830, -9100, -90, t=180, kind="standalone", top=3481, bottom=3024)
    return south, north, s_out, s_in, n_sl, beam


# ---------------------------------------------------------------- 外壁を家の角で止める（A1）

def test_exterior_overlapping_sleeves_stops_at_the_house_corner():
    south, north, s_out, s_in, n_sl, beam = _demo_corner()
    ops = [{"wall": south, "offset_mm": 3790.0, "width_mm": 1700.0}]            # 南の窓（家の中）
    assert wl.stop_at_house_corner([south, north, s_out, s_in, n_sl, beam], HOUSE, ops) == 2
    assert south.start == pytest.approx([-8280, -10929]) and south.end == pytest.approx([-820, -10929])
    assert north.start == pytest.approx([-820, 9]) and north.end == pytest.approx([-8280, 9])
    assert ops[0]["offset_mm"] == pytest.approx(3790 - 910)                       # 始点を動かした分、窓の位置を直す
    assert any(a.startswith("outside_trim:") for a in south.assumptions + north.assumptions)
    assert s_in.top is None and s_out.top is None and n_sl.top is None           # 外壁の代わりに屋根まで
    assert any("outside_trim" in a for a in s_in.assumptions)
    assert beam.top == 3481                                                      # 梁（直角・途中から）は触らない


def test_partly_covered_or_window_or_off_line_is_left_alone():
    south, _, s_out, _, _, _ = _demo_corner()                                    # 家の外の 910mm のうち 545mm だけ覆われる
    assert wl.stop_at_house_corner([south, s_out], HOUSE) == 0
    assert south.start[0] == -9190 and s_out.top == 3481
    south, _, s_out, s_in, _, _ = _demo_corner()
    ops = [{"wall": south, "offset_mm": 100.0, "width_mm": 400.0}]               # 家の外の所に開口がある
    assert wl.stop_at_house_corner([south, s_out, s_in], HOUSE, ops) == 0 and ops[0]["offset_mm"] == 100.0
    south = _wall(-9190, -10929, -820, -10929)
    far = [_wall(-9190, -10800, -8280, -10800, t=180, kind="standalone", top=3481)]   # 中心線の差 129mm（厚みの半分より遠い）
    assert wl.stop_at_house_corner([south] + far, HOUSE) == 0


# ---------------------------------------------------------------- 両側とも外の単独の壁の厚み（A4）

def test_outdoor_standalone_uses_the_company_rule_and_keeps_the_outer_face():
    _, _, s_out, s_in, n_sl, beam = _demo_corner()
    walls = [s_out, s_in, n_sl, beam]
    assert wl.outdoor_standalone(walls, HOUSE, 180.0, HOUSE) == []               # 自社：glb と同じ 180 → 形は変わらない
    assert all(getattr(w, "_thickness_rule", None) == "walls.standalone_thickness_mm" for w in walls)
    changed = wl.outdoor_standalone(walls, HOUSE, 208.0, HOUSE)                  # B工務店（架空）208
    assert len(changed) == 4 and all(w.thickness == 208.0 for w in walls)
    assert s_out.start[1] - 104 == pytest.approx(-11010)                         # 南の袖壁：家の外形の辺の延長の上の面（南の面）はそのまま
    assert n_sl.start[1] + 104 == pytest.approx(90)                              # 北の袖壁：北の面はそのまま
    assert beam.start[0] - 104 == pytest.approx(-9190)                           # 梁：家から遠い方の面（袖壁の先とそろう）はそのまま
    assert any("standalone_rule" in a for a in s_out.assumptions)


def test_half_inside_standalone_is_not_an_outdoor_wall():
    porch_side = _wall(-8199, -5000, -8199, -3000, t=180, kind="standalone", top=3024)   # 片側が家の中（外壁の一部）
    assert wl.outdoor_standalone([porch_side], HOUSE, 208.0, HOUSE) == []
    assert porch_side.thickness == 180 and not hasattr(porch_side, "_thickness_rule")


def test_walls_to_json_writes_the_thickness_rule():
    from adapters.walkinhome import walls_to_json
    _, _, s_out, _, _, _ = _demo_corner()
    wl.outdoor_standalone([s_out], HOUSE, 180.0, HOUSE)
    out, _, _ = walls_to_json([s_out], "1F")
    assert out[0]["thickness_rule"] == "walls.standalone_thickness_mm"


# ---------------------------------------------------------------- 袖壁と梁はまるごと1階の部品（A2）

def _frame_house():
    """_tiny_house（6000×4000、西が低い片流れ）の西に、デモ用と同じ額縁：南北の袖壁（180・屋根まで）と、その先をつなぐ梁（3024〜屋根）、
    額縁の中のポーチの天井（3024）。屋根は額縁の上まで伸びる。"""
    b = _tiny_house()
    b["walls"] += [
        {"id": "W6", "level": "1F", "kind": "standalone", "start": [-900, 90], "end": [0, 90], "thickness_mm": 180,
         "bottom_mm": 480, "height": "to_roof"},
        {"id": "W7", "level": "1F", "kind": "standalone", "start": [-900, 3910], "end": [0, 3910], "thickness_mm": 180,
         "bottom_mm": 480, "height": "to_roof"},
        {"id": "W8", "level": "1F", "kind": "standalone", "start": [-810, 180], "end": [-810, 3820], "thickness_mm": 180,
         "bottom_mm": 3024, "height": 3664}]
    b["roof"]["footprint"] = [[-900, 0], [6000, 0], [6000, 4000], [-900, 4000]]
    b["roof"]["top_z_range_mm"] = [3765, 4800]
    b["porch_soffits"] = [{"id": "S1", "polygon": [[-720, 180], [0, 180], [0, 3820], [-720, 3820]], "z_mm": 3024}]
    b["foundation"]["footings"] = [{"polygon": [[-900, 15], [0, 15], [0, 165], [-900, 165]]},
                                   {"polygon": [[-900, 3835], [0, 3835], [0, 3985], [-900, 3985]]}]
    return b


def _rb(x):
    return 0.15 * x + 3799.0                                  # 屋根の下の面（_tiny_house）


def _objects(parts) -> dict:
    objs: dict[str, list] = {}
    for p in parts.values():
        objs.setdefault(p.object_key or p.key, []).append(p.solid)
    return {k: union_all(v) for k, v in objs.items()}


@pytest.fixture(scope="module")
def frame():
    rules = load_rules("jisha")
    return build_model(_frame_house(), rules), rules


def test_sleeves_and_beam_go_whole_into_the_first_floor(frame):
    parts, rules = frame
    objs = _objects(parts)
    b = _frame_house()
    rb = b["roof"]["bottom_plane"]
    for w in b["walls"][-3:]:
        body = keep_below(prism(wall_rect(w), float(w["bottom_mm"]), 6000.0), rb)
        assert (objs["floor1"] ^ body).volume() == pytest.approx(body.volume(), rel=0.02), w["id"]   # まるごと1階
        assert (objs["roof"] ^ body).volume() < 1.0, w["id"]                                          # 屋根にかけらがない
    # 袖壁・梁の上端は屋根の下の面に沿う（梁は glb の上端 3664 から屋根の下の面まで上げる）
    for x in (-880.0, -500.0, -20.0):
        probe = objs["floor1"] ^ prism(box(x - 2, 85, x + 2, 95), 0, 6000)
        assert probe.bounding_box()[5] == pytest.approx(_rb(x), abs=1.0), x
    notes = " ".join(parts["floor1"].notes)
    assert "袖壁）W6・W7" in notes and "梁 W8" in notes and "サポート材" in notes
    assert not any("袖壁" in n and "屋根と一緒" in n for n in parts["roof"].notes)


def test_roof_keeps_clearance_around_the_frame_and_nothing_overlaps(frame):
    parts, rules = frame
    objs = _objects(parts)
    scale = float(rules["model"]["scale"])
    clearance = float(rules["model"]["fit_clearance_mm"]) * scale
    b = _frame_house()
    room = union_all([keep_below(prism(wall_rect(w).buffer(clearance - 1.0, join_style="mitre"), 0, 6000), b["roof"]["bottom_plane"])
                      for w in b["walls"][-3:]])
    assert (objs["roof"] ^ room).volume() < 1.0                    # ポーチの天井の板・屋根の外壁は、袖壁・梁から隙間の分だけ離れる
    assert [c["level"] for c in overlap_checks(parts, scale)] == ["情報"]
    for k, s in objs.items():
        assert len(drop_specks(s).decompose()) == 1, k               # 部品がばらばらにならない（梁は袖壁で、天井の板は家の外壁でつながる）
    assert any("隙間の分だけ空けた" in n for n in parts["roof"].notes)
    # ポーチの天井の板は残る（額縁の内側の、隙間を除いた所）
    plate = objs["roof"] ^ prism(box(-700, 200, -20, 3800), 3024, 3124)
    assert plate.volume() == pytest.approx(680 * 3600 * 100, rel=0.01)


def test_sleeves_get_the_same_fuzzy_skin_as_the_walls(frame):
    parts, rules = frame
    band = float(rules["model"]["fuzzy_skin_band_mm"]) * float(rules["model"]["scale"])
    ext = parts["exterior1"].solid
    outer = prism(box(-880, 0, -20, band - 1), 600, 3500)                       # 南の袖壁の外（南）の面の層
    inner = prism(box(-880, 180 - band + 1, -20, 180), 600, 3000)              # 北（ポーチ側）の面の層
    core = prism(box(-900 + band + 10, band + 1, -band - 10, 180 - band - 1), 600, 3500)   # 真ん中はざらざらにしない
    assert (ext ^ outer).volume() == pytest.approx(outer.volume(), rel=0.01)
    assert (ext ^ inner).volume() == pytest.approx(inner.volume(), rel=0.01)
    assert (ext ^ core).volume() < 1.0
    assert any("袖壁）・梁も" in n for n in parts["floor1"].notes)


def test_without_sleeves_the_house_is_unchanged():
    rules = load_rules("jisha")
    base = build_model(_tiny_house(), rules)
    b = _tiny_house()
    b["walls"].append({"id": "W9", "level": "1F", "kind": "standalone", "start": [1000, 1000], "end": [2000, 1000],
                       "thickness_mm": 162, "bottom_mm": 480, "height": 2824})   # 家の中の単独の壁は今までどおり
    parts = build_model(b, rules)
    assert not any("袖壁" in n for p in parts.values() for n in p.notes)
    assert parts["roof"].solid.volume() == pytest.approx(base["roof"].solid.volume(), rel=1e-6)


# ---------------------------------------------------------------- 2階建て：下屋の下の袖壁（同じ考え）

def test_two_storey_sleeve_under_the_lean_to_goes_whole_into_the_first_floor():
    from tests.test_two_story_roofs import CASES, house
    rules = load_rules("jisha")
    b = house(CASES["gable"])
    sleeve = {"id": "W20", "level": "1F", "kind": "standalone", "start": [4000, -400], "end": [4000, 0], "thickness_mm": 180,
              "bottom_mm": 480, "height": "to_roof"}                         # 南の外壁から下屋の軒の下へ出る袖壁
    b["walls"].append(sleeve)
    parts = build_model(b, rules)
    objs = _objects(parts)
    lean = next(r for r in b["roofs"] if r["kind"] == "lean_to")
    body = keep_below(prism(wall_rect(sleeve), 480, 9000), lean["bottom_plane"])
    assert (objs["floor1"] ^ body).volume() == pytest.approx(body.volume(), rel=0.02)
    assert (objs["roof_lower"] ^ body).volume() < 1.0                        # 前は天井より上が下屋の屋根の部品に入っていた
    assert [c["level"] for c in overlap_checks(parts, float(rules["model"]["scale"]))] == ["情報"]
    assert any("W20" in n for n in parts["floor1"].notes)


# ---------------------------------------------------------------- 外壁の厚みを直しても、外の面は動かない（A3）

def test_exterior_thickness_override_keeps_the_outer_face():
    from core import overrides as ov
    b = _tiny_house()
    applied, res = ov.apply(b, [{"id": "OV1", "target": "W1", "field": "thickness_mm", "to": 200},     # 南（外の面 y=0）
                                {"id": "OV2", "target": "W4", "field": "thickness_mm", "to": 120},     # 西（外の面 x=0）薄く
                                {"id": "OV3", "target": "W5", "field": "thickness_mm", "to": 150}])    # 内壁は中心のまわり
    assert [r["status"] for r in res] == ["applied"] * 3
    w = {x["id"]: x for x in applied["walls"]}
    assert w["W1"]["start"][1] - 100 == pytest.approx(0) and w["W1"]["end"][1] - 100 == pytest.approx(0)
    assert w["W4"]["start"][0] - 60 == pytest.approx(0)
    assert w["W5"]["start"] == [3000, 80]
    parts = build_model(applied, load_rules("jisha"))
    skin = parts["exterior1"].solid ^ prism(box(300, 0, 900, 60), 600, 1400)  # 南の外の面の層は、ざらざらの部品のまま
    assert skin.volume() == pytest.approx(600 * 60 * 800, rel=0.01)


# ---------------------------------------------------------------- 形の検査（A5）

def test_fuzzy_face_finding_when_the_outer_face_leaves_the_skin():
    from core.shape_checks import fuzzy_face_findings
    rules = load_rules("jisha")
    assert fuzzy_face_findings(_tiny_house(), rules) == []
    b = _tiny_house()
    b["walls"][0]["thickness_mm"] = 180                                      # 前の直し方（中心のまわり）：外の面が 10mm 外へ
    out = fuzzy_face_findings(b, rules)
    assert len(out) == 1 and out[0]["targets"] == ["W1"] and out[0]["view"] == "S" and out[0]["severity"] == "中"
    assert "外の面が基礎の外形から 10mm 外" in out[0]["message"] and "ざらざらになりません" in out[0]["message"]
    plain = load_rules("bkoumuten")                                          # なめらかな外壁の会社は見ない
    assert fuzzy_face_findings(b, plain) == []


def test_missing_footing_finding_for_walls_outside_the_foundation():
    from core.shape_checks import missing_footing_findings
    rules = load_rules("jisha")
    b = _frame_house()
    assert missing_footing_findings(b, rules) == []                          # 袖壁の下に glb の基礎（footings）がある
    b["foundation"]["footings"] = b["foundation"]["footings"][:1]            # 北の袖壁の下の基礎を入れ忘れた
    out = missing_footing_findings(b, rules)
    assert [f["targets"] for f in out] == [["W7"]]                           # 梁（途中から）・内壁・家の中の外壁は見ない
    assert out[0]["message"] == "単独の壁 W7 の下に glb の基礎がありません。CAD で基礎の入れ忘れかもしれません（模型では基礎を足しています）"


def test_split_outside_finding_for_an_exterior_wall_reaching_out_of_the_house():
    from core.shape_checks import split_outside_findings
    rules = load_rules("jisha")
    assert split_outside_findings(_frame_house(), rules) == []
    b = _tiny_house()
    b["walls"][0]["start"] = [-900, 80]                                      # 南の外壁が家の角を越えて 900mm 外へ（袖壁なし）
    out = split_outside_findings(b, rules)
    assert len(out) == 1 and out[0]["targets"] == ["W1"]
    assert "約 900mm" in out[0]["message"] and "分かれています" in out[0]["message"]


def test_code_findings_include_the_shape_checks(tmp_path, monkeypatch):
    from core import findings, overrides, paths
    monkeypatch.setattr(findings, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(overrides, "out_dir", lambda a: tmp_path / a)

    def no_drawings(a):          # 試験用の家は登録していない（図面と比べない。実データを読まない）
        raise KeyError(a)
    monkeypatch.setattr(paths, "property_files", no_drawings)
    b = _frame_house()
    b["foundation"]["footings"] = []
    b["source"] = {"company_rules": "jisha"}
    for o in b["openings"]:                  # 会社ルールとの比べが読む値（_band_house と同じ）
        o.setdefault("sill_fl_mm", o["sill_mm"] - 624)
        o.setdefault("head_fl_mm", o["head_mm"] - 624)
    (tmp_path / "物件T").mkdir()
    (tmp_path / "物件T" / "building.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    titles = [f["title"] for f in findings.code_findings("物件T")]
    assert "W6 の下に glb の基礎がない" in titles and "W7 の下に glb の基礎がない" in titles
    assert not any("形の検査が止まった" in t for t in titles)


# ---------------------------------------------------------------- 直す案の確かめ（A7）

def _ctx(b):
    from core.ai.tools import ToolContext
    return ToolContext(alias="物件T", raw=json.loads(json.dumps(b)), building=b, rules=load_rules("jisha"))


def test_lintel_band_is_refused_where_no_band_can_be_made():
    from core.ai.tools import call_tool
    from tests.test_lintel_band import _band_house
    b = _band_house()
    b["walls"] += _frame_house()["walls"][-3:]
    ctx = _ctx(b)
    ok = call_tool(ctx, "propose_override", {"target": "W1", "field": "lintel_band_mm",
                                             "value_source": "rule:model.lintel_band_mm", "reason": "窓の間の柱をつなぐ"})
    assert ok["proposal"]["to"] == 100.0
    for target in ("W6", "W2"):            # 家の外の袖壁・窓の並びのない外壁
        r = call_tool(ctx, "propose_override", {"target": target, "field": "lintel_band_mm",
                                                "value_source": "rule:model.lintel_band_mm", "reason": "x"})
        assert "この壁には窓の上の帯ができません" in r["error"], target


def test_wall_height_can_be_proposed_and_applied():
    from core import overrides as ov
    from core.ai.tools import call_tool
    b = _frame_house()
    ctx = _ctx(b)
    ctx.human_text = "この梁の上端を 3200 にしたい"
    r = call_tool(ctx, "propose_override", {"target": "W8", "field": "height", "value_source": "number", "number": 3200,
                                            "reason": "人の一言"})
    assert (r["proposal"]["from"], r["proposal"]["to"]) == (3664, 3200.0)
    r = call_tool(ctx, "propose_override", {"target": "W8", "field": "height", "value_source": "to_roof", "reason": "屋根まで"})
    assert r["proposal"]["to"] == "to_roof" and r["proposal"]["value_source"] == "to_roof"
    assert "すでに" in call_tool(ctx, "propose_override", {"target": "W6", "field": "height", "value_source": "to_roof",
                                                         "reason": "x"})["error"]
    assert "会社ルール" in call_tool(ctx, "propose_override", {"target": "W8", "field": "height",
                                                            "value_source": "rule:levels.ceiling_height_mm", "reason": "x"})["error"]
    assert "height" in call_tool(ctx, "propose_override", {"target": "W8", "field": "thickness_mm", "value_source": "to_roof",
                                                          "reason": "x"})["error"]
    ctx.human_text = "上端を 300 に"
    assert "下端" in call_tool(ctx, "propose_override", {"target": "W8", "field": "height", "value_source": "number", "number": 300,
                                                        "reason": "x"})["error"]
    applied, res = ov.apply(b, [{"id": "OV1", "target": "W7", "field": "height", "to": "to_roof"},
                                {"id": "OV2", "target": "W8", "field": "height", "to": 3500},
                                {"id": "OV3", "target": "W8", "field": "height", "to": 2000},       # 下端 3024 より下
                                {"id": "OV4", "target": "W8", "field": "height", "to": "屋根"}])
    assert [r["status"] for r in res] == ["applied", "applied", "failed", "failed"]
    w = {x["id"]: x for x in applied["walls"]}
    assert w["W7"]["height"] == "to_roof" and w["W8"]["height"] == 3500


def test_free_standing_wall_away_from_the_house_is_not_a_sleeve():
    """★11/6 物件A：家から約 2m 離れて立つポーチの壁（屋根まで届く）は、家の部品に入れない（家とつながらない離れた塊になるため）。
    家に接する袖壁と、それにつながる袖壁・梁だけが家（1階）の部品。"""
    from core.builder import sleeve_walls
    house = Polygon([(0, 0), (6000, 0), (6000, 4000), (0, 4000)])
    touching = {"id": "W6", "kind": "standalone", "start": [-900, 90], "end": [0, 90], "thickness_mm": 180, "bottom_mm": 480}
    chained = {"id": "W7", "kind": "standalone", "start": [-1800, 90], "end": [-900, 90], "thickness_mm": 180, "bottom_mm": 480}
    far = {"id": "W9", "kind": "standalone", "start": [-2100, 500], "end": [-2100, 3500], "thickness_mm": 180, "bottom_mm": 480}
    sleeves, beams = sleeve_walls([touching, chained, far], house, 624.0)
    assert [w["id"] for w in sleeves] == ["W6", "W7"] and beams == []
