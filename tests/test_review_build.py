"""① 組み立ての見直し（11/6）：家の角で止めた外壁と、番号の引き継ぎ・開口の位置・外壁の厚みの直し・壁の上端の直しを、架空の家で確かめる。
実データ不要。止める前の building.json（前回）と、止めた後（今回）を作って、core/ids.py の stabilize を通す。
"""
from __future__ import annotations

import copy
import math

import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from adapters import walls as wl
from adapters.walkinhome import walls_to_json
from core import overrides as ov
from core.ids import stabilize
from tests.test_builder import _tiny_house

HOUSE = Polygon([(-8280, 90), (-820, 90), (-820, -11010), (-8280, -11010)])     # デモ用の家の外形（tests/test_sleeve_walls.py と同じ）


def _wall(x0, y0, x1, y1, t=162.0, kind="exterior", top=None, bottom=480.0):
    return wl.Wall(start=np.array([x0, y0], float), end=np.array([x1, y1], float), thickness=t, kind=kind,
                   bottom=bottom, top=top)


def _demo_walls():
    """デモ用の西の額縁（南北の外壁が家の角を越えて袖壁の先まで伸びる）と、家の東西の外壁・内壁1枚。南と北に窓。"""
    south = _wall(-9190, -10929, -820, -10929)                 # 始点が家の外（止めると始点が動く）
    north = _wall(-820, 9, -9190, 9)                           # 終点が家の外（止めても始点は動かない）
    walls = [south, north, _wall(-8199, 90, -8199, -11010), _wall(-901, -11010, -901, 90),
             _wall(-9190, -10920, -8645, -10920, t=180, kind="standalone", top=3481),
             _wall(-8645, -10920, -8280, -10920, t=180, kind="standalone", top=3024),
             _wall(-9190, 0, -8280, 0, t=180, kind="standalone", top=3481),
             _wall(-9100, -10830, -9100, -90, t=180, kind="standalone", top=3481, bottom=3024),
             _wall(-5000, -10848, -5000, 9, t=130, kind="interior", top=3024, bottom=624)]
    ops = [{"wall": south, "offset_mm": 3790.0, "width_mm": 1700.0, "center": np.array([-4550.0, -10929.0])},
           {"wall": north, "offset_mm": 1000.0, "width_mm": 800.0, "center": np.array([-2220.0, 9.0])}]
    return walls, ops


def _building(walls, ops) -> dict:
    """walls_to_json で building.json の形にする（番号は位置の順に振り直される）。"""
    wj, assumptions, ids = walls_to_json(walls, "1F")
    oj = [{"id": f"O{k}", "wall": ids[id(o["wall"])], "kind": "window", "offset_mm": round(o["offset_mm"], 1),
           "width_mm": o["width_mm"], "sill_mm": 1500, "head_mm": 3000, "center": [float(c) for c in o["center"]],
           "source": f"glb:typeETate#{k}"} for k, o in enumerate(ops, 1)]
    return {"levels": [{"name": "1F", "fl_mm": 624}], "walls": wj, "openings": oj, "rooms": [], "glb_exceptions": [],
            "assumptions": assumptions, "foundation": {"outline": [list(c) for c in HOUSE.exterior.coords[:-1]]}}


def _opening_xy(b: dict, oid: str) -> np.ndarray:
    """開口の始まりの位置（壁の始点＋向き×offset_mm）。家の中の同じ所にあるかを見る。"""
    o = next(x for x in b["openings"] if x["id"] == oid)
    w = next(x for x in b["walls"] if x["id"] == o["wall"])
    s, e = np.array(w["start"], float), np.array(w["end"], float)
    return s + (e - s) / np.linalg.norm(e - s) * float(o["offset_mm"])


def _before_and_after():
    walls, ops = _demo_walls()
    before = _building(walls, ops)
    walls, ops = _demo_walls()
    assert wl.stop_at_house_corner(walls, HOUSE, ops) == 2
    after = stabilize(_building(walls, ops), copy.deepcopy(before))
    return before, after


# ---------------------------------------------------------------- 番号の引き継ぎ（止める前の building.json → 止めた後）

def test_walls_stopped_at_the_house_corner_keep_their_numbers():
    """家の角で止めて始点・終点・長さが変わっても、前回の番号を引き継ぐ（人の直しの target=W○ が別の壁に当たらない）。"""
    before, after = _before_and_after()
    assert after["id_changes"]["new"] == [] and after["id_changes"]["removed"] == []
    old = {w["id"]: w for w in before["walls"]}
    for w in after["walls"]:
        o = old[w["id"]]
        assert (o["kind"], o["thickness_mm"], o["bottom_mm"]) == (w["kind"], w["thickness_mm"], w["bottom_mm"]), w["id"]
        d = np.array(o["end"], float) - np.array(o["start"], float)
        n = np.array([-d[1], d[0]]) / np.linalg.norm(d)
        assert abs(float(np.dot(np.array(w["start"], float) - np.array(o["start"], float), n))) < 1.0, w["id"]   # 同じ線の上
    south = next(w for w in after["walls"] if w["kind"] == "exterior" and w["end"] == [-820.0, -10929.0])
    assert south["start"] == [-8280.0, -10929.0] and old[south["id"]]["start"] == [-9190.0, -10929.0]
    assert [o["wall"] for o in after["openings"]] == [o["wall"] for o in before["openings"]]
    assert any(a["field"] == f"walls[{south['id']}]" and a["note"].startswith("outside_trim:") for a in after["assumptions"])


def test_numbers_survive_a_thickness_change_that_keeps_one_face():
    """両側とも外の単独の壁を会社ルールの厚みにすると（片側の面を残す）、中心線が厚みの差の半分ずれる。
    glb 100→180 で梁の中心線が 40mm ずれても、前回の番号を引き継ぐ（★11/6 見直し：前は別の番号になり、人の直しが「見つからない」になった）。"""
    def build(t_std, rule):
        walls, ops = _demo_walls()
        for w in walls:
            if w.kind == "standalone":
                w.thickness = t_std
        wl.stop_at_house_corner(walls, HOUSE, ops)
        if rule is not None:
            assert wl.outdoor_standalone(walls, HOUSE, rule, HOUSE)
        return _building(walls, ops)
    before = build(100.0, None)
    after = stabilize(build(100.0, 180.0), copy.deepcopy(before))
    assert after["id_changes"]["new"] == [] and after["id_changes"]["removed"] == []
    beam = next(w for w in after["walls"] if w["bottom_mm"] == 3024 and w["kind"] == "standalone")
    old = next(w for w in before["walls"] if w["id"] == beam["id"])
    assert old["bottom_mm"] == 3024 and abs(beam["start"][0] - old["start"][0]) == pytest.approx(40.0)


def test_numbers_survive_an_exterior_rule_change_but_not_a_different_wall():
    def ext(wid, y, t):
        return {"id": wid, "level": "1F", "kind": "exterior", "start": [0.0, y], "end": [6000.0, y], "thickness_mm": t,
                "bottom_mm": 480}
    before = {"walls": [ext("W1", 81.0, 162.0)], "openings": [], "rooms": []}
    after = stabilize({"walls": [ext("W7", 125.0, 250.0)], "openings": [], "rooms": []}, copy.deepcopy(before))   # 外の面 y=0 のまま
    assert after["walls"][0]["id"] == "W1" and after["id_changes"]["new"] == []
    other = stabilize({"walls": [ext("W7", 141.0, 162.0)], "openings": [], "rooms": []}, copy.deepcopy(before))  # 同じ厚みで 60mm 奥
    assert other["walls"][0]["id"] == "W2" and other["id_changes"]["removed"] == ["W1"]


def test_nearby_parallel_walls_are_not_swapped():
    """許しを広げても、近くの平行な別の壁とは取り違えない。面のすき間 0〜10mm で並ぶ2枚（中心線の間 95〜150）で、
    片方・両方を厚くしても、前回の番号のまま。両方とも相手の側へ厚くして4通りの組が同じ重なりになるときは、中心線の近い方を選ぶ
    （並び順によらない）。"""
    def sw(wid, y, t):
        return {"id": wid, "level": "1F", "kind": "standalone", "start": [0.0, y], "end": [3000.0, y], "thickness_mm": t,
                "bottom_mm": 480}

    def ids(old, new):
        out = stabilize({"walls": copy.deepcopy(new), "openings": [], "rooms": []},
                        {"walls": copy.deepcopy(old), "openings": [], "rooms": []})
        assert out["id_changes"]["new"] == [] and out["id_changes"]["removed"] == []
        return {w["start"][1]: w["id"] for w in out["walls"]}
    old = [sw("W1", 0.0, 100), sw("W2", 150.0, 180)]               # 面のすき間 10mm。許し 30+40=70 < 中心線の間 150
    assert ids(old, [sw("W8", 40.0, 180), sw("W9", 150.0, 180)]) == {40.0: "W1", 150.0: "W2"}    # 相手へ 40 近づく
    assert ids(old, [sw("W8", -40.0, 180), sw("W9", 150.0, 180)]) == {-40.0: "W1", 150.0: "W2"}  # 相手から離れる
    touching = [sw("W1", 0.0, 100), sw("W2", 95.0, 90)]            # 面が接している（すき間 0）
    both = [sw("W8", 40.0, 180), sw("W9", 50.0, 180)]              # 両方とも相手の側へ 180 に（4通りとも重なりは同じ）
    for o in (touching, touching[::-1]):
        for n in (both, both[::-1]):
            assert ids(o, n) == {40.0: "W1", 50.0: "W2"}


def test_openings_stay_in_place_when_the_start_is_cut():
    """始点を切った壁の開口は、切った長さだけ offset_mm が減り、家の中の同じ所のまま。終点だけ切った壁は変わらない。"""
    before, after = _before_and_after()
    for oid in ("O1", "O2"):
        assert _opening_xy(after, oid) == pytest.approx(_opening_xy(before, oid), abs=0.1), oid
    assert [o["offset_mm"] for o in after["openings"]] == [3790.0 - 910.0, 1000.0]


def test_both_ends_and_reversed_walls_keep_openings_in_place():
    house = Polygon([(0, 0), (6000, 0), (6000, 4000), (0, 4000)])
    for x0, x1 in ((-900, 6600), (6600, -900)):                  # 両側とも家の外へ（向きが逆の壁も）
        ext = _wall(x0, 81, x1, 81)
        sleeves = [_wall(-900, 90, 0, 90, t=180, kind="standalone", top=3000),
                   _wall(6000, 90, 6600, 90, t=180, kind="standalone", top=3000)]
        o = {"wall": ext, "offset_mm": 2000.0, "width_mm": 1000.0}
        on_sleeve = {"wall": sleeves[0], "offset_mm": 100.0, "width_mm": 300.0}   # 袖壁の開口（別の壁）は動かさない
        at = ext.start + ext.direction * o["offset_mm"]
        assert wl.stop_at_house_corner([ext] + sleeves, house, [o, on_sleeve]) == 2
        assert sorted([ext.start[0], ext.end[0]]) == [0.0, 6000.0]
        assert ext.start + ext.direction * o["offset_mm"] == pytest.approx(at)
        assert on_sleeve["offset_mm"] == 100.0


# ---------------------------------------------------------------- 外壁の厚みの直し：外の面を動かさない（斜めの壁・2階・2件・取り消し）

def _outer_face_gap(w: dict, outline: Polygon) -> float:
    """外壁の外の面（家の外側の面）の真ん中と、家の外形の辺との距離。"""
    s, e = np.array(w["start"], float), np.array(w["end"], float)
    d = (e - s) / np.linalg.norm(e - s)
    n = np.array([-d[1], d[0]])
    mid, half = (s + e) / 2, float(w["thickness_mm"]) / 2
    faces = [Point(*(mid + n * half)), Point(*(mid - n * half))]
    outer = min(faces, key=lambda p: (outline.contains(p), outline.exterior.distance(p)))   # 外形の上（か外）にある方
    return outline.exterior.distance(outer)


def test_exterior_thickness_override_keeps_the_outer_face_on_a_slanted_house():
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))

    def rot(p):
        return [p[0] * c - p[1] * s, p[0] * s + p[1] * c]
    b = _tiny_house()
    for w in b["walls"]:
        w["start"], w["end"] = rot(w["start"]), rot(w["end"])
    b["foundation"]["outline"] = [rot(p) for p in b["foundation"]["outline"]]
    outline = Polygon(b["foundation"]["outline"])
    for tid, to in (("W1", 200), ("W3", 100)):                    # 厚く・薄く
        applied, res = ov.apply(b, [{"id": "OV1", "target": tid, "field": "thickness_mm", "to": to}])
        w = next(x for x in applied["walls"] if x["id"] == tid)
        assert res[0]["status"] == "applied" and w["thickness_mm"] == to
        assert _outer_face_gap(w, outline) < 0.2, tid              # 座標の丸め（0.1mm）の分だけ


def test_upper_level_exterior_uses_its_own_outline_and_two_overrides_add_up():
    b = _tiny_house()
    b["levels"].append({"name": "2F", "fl_mm": 3500, "ceiling_mm": 2400, "outline": [[0, 0], [3000, 0], [3000, 4000], [0, 4000]]})
    b["walls"].append({"id": "W9", "level": "2F", "kind": "exterior", "start": [2920, 0], "end": [2920, 4000],
                       "thickness_mm": 160, "bottom_mm": 3500, "height": "to_roof"})
    applied, _ = ov.apply(b, [{"id": "OV1", "target": "W9", "field": "thickness_mm", "to": 200}])
    w9 = next(x for x in applied["walls"] if x["id"] == "W9")
    assert w9["start"] == [2900.0, 0.0]                           # 2階の外形の東の辺（x=3000）が外の面のまま
    items = [{"id": "OV1", "target": "W1", "field": "thickness_mm", "from": 160, "to": 180},
             {"id": "OV2", "target": "W1", "field": "thickness_mm", "from": 180, "to": 200}]
    applied, res = ov.apply(b, items)
    w1 = next(x for x in applied["walls"] if x["id"] == "W1")
    assert [r["status"] for r in res] == ["applied", "applied"] and w1["start"] == [0.0, 100.0]   # 外の面 y=0 のまま
    applied, res = ov.apply(b, items[1:])                         # 1件目を取り消しても、外の面は動かない
    w1 = next(x for x in applied["walls"] if x["id"] == "W1")
    assert res[0]["status"] == "applied_changed" and w1["start"] == [0.0, 100.0]
    applied, _ = ov.apply(b, [])                                  # 全部取り消すと元のまま
    assert next(x for x in applied["walls"] if x["id"] == "W1")["start"] == [0, 80.0]


# ---------------------------------------------------------------- 壁の上端の直し：屋根まで ⇔ 数値

def test_wall_height_from_to_roof_to_a_number_and_back():
    from core.ai.tools import call_tool
    from tests.test_sleeve_walls import _ctx, _frame_house
    b = _frame_house()
    ctx = _ctx(b)
    ctx.human_text = "南の袖壁の上端は 3500 にして"
    r = call_tool(ctx, "propose_override", {"target": "W6", "field": "height", "value_source": "number", "number": 3500,
                                            "reason": "人の一言"})
    assert (r["proposal"]["from"], r["proposal"]["to"]) == ("to_roof", 3500.0)
    applied, res = ov.apply(b, [{"id": "OV1", "target": "W6", "field": "height", "from": "to_roof", "to": 3500.0}])
    assert res[0]["status"] == "applied"
    ctx = _ctx(applied)
    ctx.raw = copy.deepcopy(b)                                    # 自動の値（glb）は屋根まで
    r = call_tool(ctx, "propose_override", {"target": "W6", "field": "height", "value_source": "glb", "reason": "glb に戻す"})
    assert (r["proposal"]["from"], r["proposal"]["to"]) == (3500.0, "to_roof")
