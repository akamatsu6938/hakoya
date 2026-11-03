"""2階建て＋切妻・寄棟（試験用の家）を組み立てる（core/builder_multi.py）。data/ がなくても動く。
★2026-11-04：実物の glb で確かめた切妻・寄棟は平屋だけ（物件C・D）。残りの glb に2階建ての切妻・寄棟が来る見込みなので、
building.json の形で、大屋根（切妻・寄棟）と下屋（片流れ・両端を寄棟にした下屋・L字に回る下屋）の2階建てを通す。

家の形（実寸 mm、外壁の外の面）：1階 8000×9000。2階は北の 8000×6000（L字の下屋では北西の 5000×6000）。軒の出は 450。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from adapters.glb_reader import Part, Primitive
from core.builder import build_model, drop_specks, prism, union_all
from core.export import overlap_checks
from core.rules import load_rules

T_EXT, T_INT = 162.0, 130.0
FL1, FL2 = 624.0, 3624.0
CEIL1, CEIL2 = FL1 + 2400, FL2 + 2400
S, S_LEAN = 0.4, 0.2          # 大屋根 4/10・下屋 2/10
E = 450.0                     # 軒の出（四方とも）
H2 = 6500.0                   # 大屋根の上の面の高さ（2階の外壁の外の面で）
LEAN_HI = 3900.0              # 下屋の上の面の高さ（2階の外壁の外の面で）
X0, X1 = -E, 8000 + E
FOOT = [(0, 0), (8000, 0), (8000, 9000), (0, 9000)]
UPPER = [(0, 3000), (8000, 3000), (8000, 9000), (0, 9000)]
UPPER_L = [(0, 3000), (5000, 3000), (5000, 9000), (0, 9000)]
PARTS = ["foundation", "floor1", "exterior1", "roof_lower", "roof_lower_ext", "floor2", "exterior2", "roof", "roof_ext"]


def tv(s: float) -> float:
    """屋根板 100mm（面に垂直）の、鉛直の厚み。"""
    return 100.0 * math.sqrt(1 + s * s)


def _plane(fp, a, b, c, s):
    return {"footprint": [[float(x), float(y)] for x, y in fp],
            "top_plane": {"a": a, "b": b, "c": c}, "bottom_plane": {"a": a, "b": b, "c": c - tv(s)}}


def main_gable(east=8000.0):
    """棟は東西（y=6000）。2階の東西の外壁が妻壁。"""
    x1, y0, y1 = east + E, 3000 - E, 9000 + E
    return [_plane([(X0, y0), (x1, y0), (x1, 6000), (X0, 6000)], 0.0, S, H2 - 3000 * S, S),
            _plane([(X0, 6000), (x1, 6000), (x1, y1), (X0, y1)], 0.0, -S, H2 + 9000 * S, S)]


def main_hip():
    """棟は (3000,6000)〜(5000,6000)。隅棟は軒先の角から 45°。"""
    y0, y1 = 3000 - E, 9000 + E
    return [_plane([(X0, y0), (X1, y0), (5000, 6000), (3000, 6000)], 0.0, S, H2 - 3000 * S, S),
            _plane([(X1, y1), (X0, y1), (3000, 6000), (5000, 6000)], 0.0, -S, H2 + 9000 * S, S),
            _plane([(X0, y1), (X0, y0), (3000, 6000)], S, 0.0, H2, S),
            _plane([(X1, y0), (X1, y1), (5000, 6000)], -S, 0.0, H2 + 8000 * S, S)]


def lean_shed():
    """南へ下がる片流れ。2階の南の外壁に取り付く。"""
    return [_plane([(X0, -E), (X1, -E), (X1, 3000), (X0, 3000)], 0.0, S_LEAN, LEAN_HI - 3000 * S_LEAN, S_LEAN)]


def lean_hipped():
    """両端を寄棟にした下屋（南の面＋東西の三角の面）。"""
    return [_plane([(X0, -E), (X1, -E), (5000, 3000), (3000, 3000)], 0.0, S_LEAN, LEAN_HI - 3000 * S_LEAN, S_LEAN),
            _plane([(X0, 3000), (X0, -E), (3000, 3000)], S_LEAN, 0.0, LEAN_HI - 3000 * S_LEAN, S_LEAN),
            _plane([(X1, -E), (X1, 3000), (5000, 3000)], -S_LEAN, 0.0, LEAN_HI + 5000 * S_LEAN, S_LEAN)]


def lean_l():
    """南と東に L字に回る下屋。南の面と東の面は、2階の南東の角から軒先の角への隅棟で分かれる。東の北の端はけらば。"""
    return [_plane([(X0, -E), (X1, -E), (5000, 3000), (X0, 3000)], 0.0, S_LEAN, LEAN_HI - 3000 * S_LEAN, S_LEAN),
            _plane([(X1, -E), (X1, 9000 + E), (5000, 9000 + E), (5000, 3000)], -S_LEAN, 0.0, LEAN_HI + 5000 * S_LEAN, S_LEAN)]


_FULL = dict(upper=UPPER,
             main_pts=[(4000, 4500), (4000, 7500), (4000, 6000), (1000, 6000), (7000, 4000), (7500, 8500), (-300, 9300)],
             lean_pts=[(4000, 1000), (500, 1500), (7600, 500), (4000, -300), (-300, 2800)],
             upper_wall_pts=[(81, 4000), (81, 7000), (7919, 5000), (7919, 6000), (2000, 3081), (6000, 8919), (4000, 8919)],
             lower_wall_pts=[(81, 1000), (81, 2500), (7919, 1500), (2000, 81), (6000, 81)],
             end_wall=(7919, 6000, 8000))
CASES = {
    "gable": dict(_FULL, main=main_gable, main_shape="gable", lean=lean_shed, lean_shape="shed"),
    "hip": dict(_FULL, main=main_hip, main_shape="hip", lean=lean_shed, lean_shape="shed"),
    "hipped_lean": dict(_FULL, main=main_gable, main_shape="gable", lean=lean_hipped, lean_shape="multi"),
    "l_lean": dict(upper=UPPER_L, main=lambda: main_gable(5000.0), main_shape="gable", lean=lean_l, lean_shape="multi",
                   main_pts=[(2500, 4500), (2500, 7500), (2500, 6000), (500, 6000), (5300, 8000), (-300, 9300)],
                   lean_pts=[(2500, 1000), (7000, 6000), (6000, 500), (7600, 8000), (8300, 9300), (4000, -300), (-300, 2800)],
                   upper_wall_pts=[(81, 4000), (81, 7000), (4919, 5000), (4919, 6000), (2000, 3081), (2500, 8919)],
                   lower_wall_pts=[(81, 1000), (81, 2500), (7919, 1500), (7919, 6000), (2000, 81), (6000, 81), (6500, 8919)],
                   end_wall=(4919, 6000, 5000)),
}


def _top(pl, x, y):
    p = pl["top_plane"]
    return p["a"] * x + p["b"] * y + p["c"]


def _surface(planes, x, y):
    """凸の屋根（切妻・寄棟・片流れ・隅棟でつながる下屋）の上の面：面の式のいちばん低いもの。"""
    return min(_top(pl, x, y) for pl in planes)


def _roof(rid, level, kind, shape, planes, s):
    tops = [_top(pl, x, y) for pl in planes for x, y in pl["footprint"]]
    outline = unary_union([Polygon(pl["footprint"]) for pl in planes])
    r = {"id": rid, "level": level, "kind": kind, "shape": shape, "pitch": [s * 10, 10], "slope": s,
         "top_plane": planes[0]["top_plane"], "bottom_plane": planes[0]["bottom_plane"], "thickness_mm": 100.0,
         "top_z_range_mm": [round(min(tops)), round(max(tops))],
         "footprint": [list(map(float, p)) for p in list(outline.exterior.coords)[:-1]],
         "eave_mm": {"W": E, "E": E, "S": E, "N": E}}
    if len(planes) > 1:
        r["planes"] = [dict(pl, id=str(k)) for k, pl in enumerate(planes, 1)]
    return r


def _ring_walls(ring, level, bottom, first):
    walls = []
    for i, (p, q) in enumerate(zip(ring, ring[1:] + ring[:1])):
        p, q = np.array(p, float), np.array(q, float)
        d = (q - p) / np.linalg.norm(q - p)
        n = np.array([-d[1], d[0]]) * T_EXT / 2                  # 左（家の中）へ半分
        walls.append({"id": f"W{first + i}", "level": level, "kind": "exterior", "start": list(p + n), "end": list(q + n),
                      "thickness_mm": T_EXT, "bottom_mm": bottom, "height": "to_roof"})
    return walls


def _interior(wid, level, start, end, bottom, top):
    return {"id": wid, "level": level, "kind": "interior", "start": list(start), "end": list(end),
            "thickness_mm": T_INT, "bottom_mm": bottom, "height": top}


def _opening(oid, w, offset, width, sill, head):
    s, e = np.array(w["start"]), np.array(w["end"])
    c = s + (e - s) / np.linalg.norm(e - s) * (offset + width / 2)
    return {"id": oid, "wall": w["id"], "kind": "window", "style": "sliding", "offset_mm": offset, "width_mm": width,
            "sill_mm": sill, "head_mm": head, "center": [float(c[0]), float(c[1])], "level": w["level"]}


def house(c):
    upper = c["upper"]
    east = float(upper[1][0])                                 # 2階の東の外壁の外の面
    walls = _ring_walls(FOOT, "1F", 480, 1) + _ring_walls(upper, "2F", FL2, 5)
    # 内壁は外壁の中心線まで入れる（物件B の glb と同じ。外壁の内側の面で止めると、内壁の下の基礎の帯が外周の帯から離れる）
    walls.append(_interior("W9", "1F", (81.0, 3081.0), (east - 81.0, 3081.0), FL1, CEIL1))        # 2階の南の外壁の下
    walls.append(_interior("W10", "2F", (east / 2, 3081.0), (east / 2, 8919.0), FL2, CEIL2))
    if east < 8000:
        walls.append(_interior("W11", "1F", (east - 81.0, 3081.0), (east - 81.0, 8919.0), FL1, CEIL1))   # 2階の東の外壁の下
    by = {w["id"]: w for w in walls}
    openings = [_opening("O1", by["W1"], 2000, 1690, 1424, 2624),                      # 1階の南（下屋の下）
                _opening("O2", by["W5"], (east - 1690) / 2, 1690, 4524, 5624),          # 2階の南
                _opening("O3", by["W6"], 2000, 790, 4524, 5624),                        # 2階の東（切妻なら妻壁）
                _opening("O4", by["W2"], 4000, 1690, 1424, 2624)]                       # 1階の東
    main = _roof("Y2", "2F", "main", c["main_shape"], c["main"](), S)
    lean = _roof("Y1", "1F", "lean_to", c["lean_shape"], c["lean"](), S_LEAN)
    stair = [[500.0, 3400.0], [1400.0, 3400.0], [1400.0, 5200.0], [500.0, 5200.0]]
    return {"levels": [{"name": "1F", "fl_mm": FL1, "ceiling_mm": 2400},
                       {"name": "2F", "fl_mm": FL2, "ceiling_mm": 2400, "outline": [list(map(float, p)) for p in upper],
                        "floor_openings": [{"id": "H1", "kind": "stairwell", "polygon": stair, "area_m2": 1.62}]}],
            "walls": walls, "openings": openings,
            "roof": {k: v for k, v in main.items() if k not in ("id", "level", "kind")}, "roofs": [lean, main],
            "foundation": {"top_mm": 480.0, "outline": [list(map(float, p)) for p in FOOT], "footings": []},
            "porches": [], "porch_soffits": [], "rooms": []}


def _zmax_at(solid, x, y, r=2.0):
    """(x, y) の上で立体のいちばん高い所（細い柱と交わらせて測る）。なければ None。"""
    probe = solid ^ prism(Polygon([(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r)]), -1000, 20000)
    return None if probe.is_empty() else probe.bounding_box()[5]


@pytest.fixture(scope="module")
def built():
    rules = load_rules("jisha")
    return rules, {name: build_model(house(c), rules) for name, c in CASES.items()}


@pytest.mark.parametrize("name", list(CASES))
def test_closed_single_bodies_and_no_overlap(built, name):
    rules, models = built
    parts = models[name]
    assert list(parts) == PARTS
    for k, p in parts.items():
        assert str(p.solid.status()).endswith("NoError") and p.solid.volume() > 0, (name, k)
    for key in ("foundation", "floor1", "roof_lower", "floor2", "roof"):     # オブジェクトごとに1つの塊
        together = union_all([p.solid for p in parts.values() if (p.object_key or p.key) == key])
        assert len(drop_specks(together).decompose()) == 1, (name, key)   # 合体の計算でできる厚みゼロのかけらは数えない（export と同じ）
    assert [c["level"] for c in overlap_checks(parts, float(rules["model"]["scale"]))] == ["情報"], name   # 組んでも重ならない
    assert not any("つながらない" in n for n in parts["foundation"].notes)      # 2階の外壁の下の内壁にも基礎の帯がある


@pytest.mark.parametrize("name", list(CASES))
def test_roof_tops_follow_their_planes(built, name):
    c, parts = CASES[name], built[1][name]
    for key, make, pts in (("roof", c["main"], c["main_pts"]), ("roof_lower", c["lean"], c["lean_pts"])):
        planes = make()
        for x, y in pts:
            assert _zmax_at(parts[key].solid, x, y) == pytest.approx(_surface(planes, x, y), abs=3.0), (name, key, x, y)


@pytest.mark.parametrize("name", list(CASES))
def test_walls_reach_the_roof_and_never_poke_through(built, name):
    """2階の外壁は大屋根の下の面まで、下屋の下の1階の外壁は下屋の下の面まで（すき間も突き抜けもない）。"""
    c, parts = CASES[name], built[1][name]
    main_planes, lean_planes = c["main"](), c["lean"]()
    upper = union_all([parts[k].solid for k in ("exterior2", "roof_ext", "floor2")])
    lower = union_all([parts[k].solid for k in ("exterior1", "floor1", "roof_lower_ext")])
    for x, y in c["upper_wall_pts"]:
        assert _zmax_at(upper, x, y) == pytest.approx(_surface(main_planes, x, y) - tv(S), abs=2.0), (name, x, y)
    for x, y in c["lower_wall_pts"]:
        assert _zmax_at(lower, x, y) == pytest.approx(_surface(lean_planes, x, y) - tv(S_LEAN), abs=2.0), (name, x, y)


@pytest.mark.parametrize("name", list(CASES))
def test_upper_end_wall_is_a_gable_only_under_a_gable_roof(built, name):
    """切妻は2階の東の外壁（妻壁）が棟の下まで三角に立つ。寄棟は軒の高さのまま（天井より上の帯）。"""
    c = CASES[name]
    x, y, face = c["end_wall"]
    z = _zmax_at(built[1][name]["roof_ext"].solid, x, y)
    expected = H2 + 3000 * S - tv(S) if c["main_shape"] == "gable" else H2 + S * (face - (x - 2)) - tv(S)
    assert z == pytest.approx(expected, abs=2.0)


@pytest.mark.parametrize("name", list(CASES))
def test_lifting_the_upper_parts_shows_the_first_floor(built, name):
    """2階と下屋の屋根を外すと、1階の部屋の上を覆う板がない（壁の切り口だけ）。"""
    assert built[1][name]["floor1"].solid.slice(CEIL1 + 100).area() < 0.2 * 8000 * 9000


def _glb_part(planes, node, s):
    """glb の屋根の部材（上の面 YANE_MAIN と下の面 YANE_NURA）。"""
    def prim(code, dz):
        verts, tris = [], []
        for pl in planes:
            poly = Polygon(pl["footprint"])
            base = len(verts)
            pts = list(poly.exterior.coords)[:-1]
            for x, y in pts:
                verts.append([x, y, _top(pl, x, y) - dz])
            for k in range(1, len(pts) - 1):
                tri = Polygon([pts[0], pts[k], pts[k + 1]])
                if tri.area > 1 and poly.buffer(1).contains(tri):
                    tris.append([base, base + k, base + k + 1])
        return Primitive(material=f"Material(Bui{code})", code=code, vertices=np.array(verts, float), triangles=np.array(tris))
    return Part(node=node, name="typeEYane", type_code="typeEYane", primitives=[prim("YANE_MAIN", 0.0), prim("YANE_NURA", tv(s))])


@pytest.mark.parametrize("name", list(CASES))
def test_per_plane_glb_parts_merge_into_main_roof_and_lean_to(name):
    """★Walk in home は面ごとに別の部材で書き出す。大屋根の面どうし・下屋の面どうしはまとめ、大屋根と下屋はまとめない
    （平面では大屋根の軒が下屋に重なるが、高さの範囲が重ならない）。"""
    from adapters.roof import detect_roof, merge_roof_parts
    c = CASES[name]
    main_planes, lean_planes = c["main"](), c["lean"]()
    parts = [_glb_part([pl], 10 + k, S) for k, pl in enumerate(main_planes)]
    parts += [_glb_part([pl], 20 + k, S_LEAN) for k, pl in enumerate(lean_planes)]
    merged = merge_roof_parts(parts)
    assert [p.source for p in merged] == ["glb:typeEYane#" + "+".join(str(10 + k) for k in range(len(main_planes))),
                                          "glb:typeEYane#" + "+".join(str(20 + k) for k in range(len(lean_planes)))]
    main = detect_roof(merged[0], Polygon(c["upper"]))
    assert main["shape"] == c["main_shape"] and len(main["planes"]) == len(main_planes)
    lean = detect_roof(merged[1], Polygon(FOOT).difference(Polygon(c["upper"])))
    assert lean["shape"] == c["lean_shape"] and len(lean.get("planes") or [0]) == len(lean_planes)


def test_deep_eaves_main_roof_still_sits_on_the_upper_floor():
    """★11/4 の見直し：軒の出が深い大屋根（軒先の下の面が2階の天井より下）を、1階の屋根（下屋）と取り違えない。
    取り違えると、2階の外壁が「1階の妻のかけら」として消える。"""
    from adapters.walkinhome import _roof_level
    from core.roof_geom import bottom_min
    deep, h = 1000.0, CEIL2 + 250                                    # 2階の外壁の外の面で、天井の 250mm 上
    planes = [_plane([(X0 - 550, 3000 - deep), (X1 + 550, 3000 - deep), (X1 + 550, 6000), (X0 - 550, 6000)], 0.0, S, h - 3000 * S, S),
              _plane([(X0 - 550, 6000), (X1 + 550, 6000), (X1 + 550, 9000 + deep), (X0 - 550, 9000 + deep)], 0.0, -S, h + 9000 * S, S)]
    main = _roof("Y2", "2F", "main", "gable", planes, S)
    levels = [{"name": "1F", "fl_mm": FL1, "ceiling_mm": 2400}, {"name": "2F", "fl_mm": FL2, "ceiling_mm": 2400}]
    assert bottom_min(main) < CEIL2                                  # 前の測り方（いちばん低い所）なら1階の屋根になっていた
    assert _roof_level(main, levels) == 1
    assert _roof_level(_roof("Y1", "1F", "lean_to", "shed", lean_shed(), S_LEAN), levels) == 0
    assert _roof_level(_roof("Y1", "1F", "lean_to", "multi", lean_l(), S_LEAN), levels) == 0
