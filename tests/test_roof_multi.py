"""片流れ以外の屋根（切妻・寄棟・L字の谷のある屋根）の読み取りと組み立てを、架空の家で確かめる（実データ不要）。

★2026-11-03 作者「片流れ以外の屋根とか複雑な凹凸はできるようにしておいてほしい」。
実物の glb（切妻・寄棟）はまだないので、Walk in home の書き出し方そのものは 10件の glb で確かめる（要検証）。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from adapters.glb_reader import Part, Primitive
from adapters.roof import detect_roof
from core.builder import build_model, prism, union_all
from core.roof_geom import outer_edges, plane_domains, surfaces
from core.rules import load_rules

H = 3300.0          # 軒の高さ（外壁の外の面での屋根の上の面）
S = 0.3             # 勾配 3/10
T_V = 100.0 * math.sqrt(1 + S * S)   # 屋根板の厚み 100（面に垂直）の、鉛直の厚み


def _pl(a, b, c):
    return {"a": a, "b": b, "c": c}


def _plane(fp, a, b, c):
    return {"footprint": [list(map(float, p)) for p in fp], "top_plane": _pl(a, b, c), "bottom_plane": _pl(a, b, c - T_V)}


def gable():
    """8000×6000、棟は東西（y=3000）。南の面と北の面。"""
    return [_plane([(0, 0), (8000, 0), (8000, 3000), (0, 3000)], 0.0, S, H),
            _plane([(0, 3000), (8000, 3000), (8000, 6000), (0, 6000)], 0.0, -S, H + 6000 * S)]


def hip():
    """8000×6000 の寄棟。棟は (3000,3000)〜(5000,3000)。"""
    return [_plane([(0, 0), (8000, 0), (5000, 3000), (3000, 3000)], 0.0, S, H),
            _plane([(8000, 6000), (0, 6000), (3000, 3000), (5000, 3000)], 0.0, -S, H + 6000 * S),
            _plane([(0, 6000), (0, 0), (3000, 3000)], S, 0.0, H),
            _plane([(8000, 0), (8000, 6000), (5000, 3000)], -S, 0.0, H + 8000 * S)]


L_OUTLINE = [(0, 0), (10000, 0), (10000, 10000), (6000, 10000), (6000, 6000), (0, 6000)]


def l_roof():
    """L字の家：東西の切妻（棟 y=3000）に、北へ出る切妻（棟 x=8000）がぶつかる。谷が2本。"""
    return [_plane([(0, 0), (10000, 0), (10000, 3000), (0, 3000)], 0.0, S, H),
            _plane([(0, 3000), (10000, 3000), (10000, 6000), (8000, 4000), (6000, 6000), (0, 6000)], 0.0, -S, H + 6000 * S),
            _plane([(6000, 6000), (8000, 4000), (8000, 10000), (6000, 10000)], S, 0.0, H - 6000 * S),
            _plane([(8000, 4000), (10000, 6000), (10000, 10000), (8000, 10000)], -S, 0.0, H + 10000 * S)]


def _top_z(plane, x, y):
    p = plane["top_plane"]
    return p["a"] * x + p["b"] * y + p["c"]


def _house(planes, outline, shape):
    t = 160.0
    ring = list(outline)
    walls = []
    for i, (p, q) in enumerate(zip(ring, ring[1:] + ring[:1]), 1):
        p, q = np.array(p, float), np.array(q, float)
        d = (q - p) / np.linalg.norm(q - p)
        n = np.array([-d[1], d[0]]) * t / 2                      # 左（家の中）へ半分
        walls.append({"id": f"W{i}", "level": "1F", "kind": "exterior", "start": list(p + n), "end": list(q + n),
                      "thickness_mm": t, "bottom_mm": 480, "height": "to_roof"})
    tops = [_top_z(pl, x, y) for pl in planes for x, y in pl["footprint"]]
    roof = {"shape": shape, "pitch": [S * 10, 10], "top_plane": planes[0]["top_plane"], "bottom_plane": planes[0]["bottom_plane"],
            "top_z_range_mm": [round(min(tops)), round(max(tops))], "footprint": [list(map(float, p)) for p in outline],
            "eave_mm": {"W": 0, "E": 0, "S": 0, "N": 0}, "planes": [dict(pl, id=str(k)) for k, pl in enumerate(planes, 1)]}
    return {"levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}], "walls": walls, "openings": [],
            "roof": roof, "roofs": [{"id": "Y1", "level": "1F", "kind": "main", **roof}],
            "foundation": {"top_mm": 480, "outline": [list(map(float, p)) for p in outline], "footings": []},
            "porches": [], "porch_soffits": [], "rooms": []}


def _zmax_at(solid, x, y, r=5.0):
    """(x, y) の上で立体のいちばん高い所（細い柱と交わらせて測る）。なければ None。"""
    probe = solid ^ prism(Polygon([(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r)]), -1000, 20000)
    return None if probe.is_empty() else probe.bounding_box()[5]


RECT = [(0, 0), (8000, 0), (8000, 6000), (0, 6000)]
CASES = {"gable": (gable, RECT), "hip": (hip, RECT), "multi": (l_roof, L_OUTLINE)}


# ---------------------------------------------------------------- 受け持ちの範囲

@pytest.mark.parametrize("name", list(CASES))
def test_domains_tile_the_roof_and_the_band_around_it(name):
    make, outline = CASES[name]
    polys = [Polygon(p["footprint"]) for p in make()]
    doms = plane_domains(polys, reach=3000)
    around = Polygon(outline).buffer(2500, join_style="mitre")
    assert unary_union(doms).intersection(around).area == pytest.approx(around.area, rel=1e-6)   # すき間がない
    for i in range(len(doms)):
        for j in range(i + 1, len(doms)):
            assert doms[i].intersection(doms[j]).area < 1.0                                      # 重ならない


def test_ridge_and_valleys_are_not_outer_edges():
    edges = outer_edges([Polygon(p["footprint"]) for p in l_roof()])
    length = sum(float(np.linalg.norm(b - a)) for a, b, _ in edges)
    assert length == pytest.approx(Polygon(L_OUTLINE).length)       # 外周の辺だけ（棟・谷は入らない）


# ---------------------------------------------------------------- 組み立て

@pytest.fixture(scope="module")
def built():
    rules = load_rules("jisha")
    return {name: build_model(_house(make(), outline, name), rules) for name, (make, outline) in CASES.items()}


@pytest.mark.parametrize("name", list(CASES))
def test_parts_are_closed_and_roof_is_one_piece(built, name):
    parts = built[name]
    for k, p in parts.items():
        assert str(p.solid.status()).endswith("NoError") and p.solid.volume() > 0, (name, k)
    assert len(parts["roof"].solid.decompose()) == 1                 # 面ごとの板が棟・隅棟・谷でつながる


@pytest.mark.parametrize("name,x,y", [("gable", 4000, 1500), ("gable", 4000, 4500), ("gable", 4000, 3000),
                                      ("hip", 4000, 3000), ("hip", 1000, 3000), ("hip", 7000, 2500),
                                      ("multi", 9000, 8000), ("multi", 7000, 8000), ("multi", 8000, 8000),
                                      ("multi", 7000, 5000), ("multi", 5000, 5000), ("multi", 2000, 1000)])
def test_roof_top_follows_each_plane(built, name, x, y):
    make, _ = CASES[name]
    expected = max(_top_z(pl, x, y) for pl in make() if Polygon(pl["footprint"]).buffer(1).contains(Polygon([(x, y), (x + 1, y), (x, y + 1)]).centroid))
    assert _zmax_at(built[name]["roof"].solid, x, y, r=2.0) == pytest.approx(expected, abs=3.0)


def test_gable_end_wall_rises_to_the_ridge(built):
    """妻壁（東西の外壁）は棟の下で三角に立ち上がる（屋根の下の面まで）。"""
    parts = built["gable"]
    walls_up = parts["roof_ext"].solid                                # 天井より上の外壁（屋根と一緒に印刷する）
    assert _zmax_at(walls_up, 7920, 3000, r=2.0) == pytest.approx(H + 3000 * S - T_V, abs=3.0)
    assert _zmax_at(walls_up, 7920, 1000, r=2.0) == pytest.approx(H + 1000 * S - T_V, abs=3.0)


@pytest.mark.parametrize("name", list(CASES))
def test_walls_never_poke_through_the_roof(built, name):
    make, outline = CASES[name]
    planes = make()
    parts = built[name]
    walls = union_all([p.solid for k, p in parts.items() if k != "roof"])
    ring = list(outline)
    for p, q in zip(ring, ring[1:] + ring[:1]):
        for t in np.linspace(0.1, 0.9, 5):
            x, y = (1 - t) * np.array(p, float) + t * np.array(q, float)
            cx, cy = np.array([x, y]) + (np.array(Polygon(outline).centroid.coords[0]) - np.array([x, y])) * 0.02   # 少し家の中へ
            z = _zmax_at(walls, cx, cy, r=2.0)
            roof_bottom = min(_top_z(pl, cx, cy) for pl in planes) - T_V if name != "multi" else None
            if z is not None and roof_bottom is not None:
                assert z <= roof_bottom + 2.0, (name, cx, cy, z, roof_bottom)


def test_eave_override_moves_only_that_side_of_a_gable():
    rules = load_rules("jisha")
    b = _house(gable(), RECT, "gable")
    b["roof"]["eave_glb_mm"] = dict(b["roof"]["eave_mm"])
    b["roof"]["eave_mm"] = {**b["roof"]["eave_mm"], "S": 450}
    base = build_model(_house(gable(), RECT, "gable"), rules)["roof"].solid.bounding_box()
    moved = build_model(b, rules)["roof"].solid.bounding_box()
    assert moved[1] == pytest.approx(base[1] - 450, abs=1.0)                 # 南だけ 450 伸びる
    assert (moved[0], moved[3], moved[4]) == pytest.approx((base[0], base[3], base[4]), abs=1.0)


def test_surfaces_single_plane_stays_a_plain_dict():
    roof = {"top_plane": _pl(0.15, 0, 4000), "bottom_plane": _pl(0.15, 0, 3899), "footprint": RECT}
    assert surfaces(roof) == (roof["bottom_plane"], roof["top_plane"])       # 片流れはいままでどおり


# ---------------------------------------------------------------- 読み取り（glb の屋根板の三角形から面を分ける）

def _part(planes):
    def prim(code, dz):
        verts, tris = [], []
        for pl in planes:
            poly = Polygon(pl["footprint"])
            base = len(verts)
            pts = list(poly.exterior.coords)[:-1]
            for x, y in pts:
                verts.append([x, y, _top_z(pl, x, y) - dz])
            for k in range(1, len(pts) - 1):                 # 扇形に三角形にする（凸でない面は三角形を作り直す）
                tri = Polygon([pts[0], pts[k], pts[k + 1]])
                if tri.area > 1 and poly.buffer(1).contains(tri):
                    tris.append([base, base + k, base + k + 1])
        return Primitive(material=f"Material(Bui{code})", code=code, vertices=np.array(verts, float), triangles=np.array(tris))
    return Part(node=99, name="typeEYane", type_code="typeEYane", primitives=[prim("YANE_MAIN", 0.0), prim("YANE_NURA", T_V)])


@pytest.mark.parametrize("name,expected_shape,count", [("gable", "gable", 2), ("hip", "hip", 4)])
def test_detect_planes_from_glb_triangles(name, expected_shape, count):
    make, outline = CASES[name]
    roof = detect_roof(_part(make()), Polygon(outline))
    assert roof["shape"] == expected_shape and len(roof["planes"]) == count
    assert roof["pitch"] == [3.0, 10] and roof["thickness_mm"] == pytest.approx(100.0, abs=0.5)
    assert sum(Polygon(p["footprint"]).area for p in roof["planes"]) == pytest.approx(Polygon(outline).area, rel=1e-3)
    assert Polygon(roof["footprint"]).area == pytest.approx(Polygon(outline).area, rel=1e-3)
    assert roof["top_z_range_mm"] == [round(H), round(H + 3000 * S)]
    for p in roof["planes"]:
        assert p["top_plane"]["c"] - p["bottom_plane"]["c"] == pytest.approx(T_V, abs=0.5)


def test_detected_gable_builds():
    roof = detect_roof(_part(gable()), Polygon(RECT))
    b = _house([{k: p[k] for k in ("footprint", "top_plane", "bottom_plane")} for p in roof["planes"]], RECT, roof["shape"])
    parts = build_model(b, load_rules("jisha"))
    assert _zmax_at(parts["roof"].solid, 4000, 3000, r=2.0) == pytest.approx(H + 3000 * S, abs=3.0)


def test_level_change_moves_every_roof_plane():
    from core import overrides as ov
    b = _house(gable(), RECT, "gable")
    moved, res = ov.apply(b, [{"id": "OV1", "target": "level:1F", "field": "fl_mm", "from": 624, "to": 724}])
    assert res[0]["status"] == "applied"
    for r in (moved["roof"], moved["roofs"][0]):
        assert [p["top_plane"]["c"] for p in r["planes"]] == pytest.approx([H + 100, H + 6000 * S + 100])
        assert [p["bottom_plane"]["c"] for p in r["planes"]] == pytest.approx([H + 100 - T_V, H + 6000 * S + 100 - T_V])
    assert b["roof"]["planes"][0]["top_plane"]["c"] == H                     # 元の building は変わらない
    parts = build_model(moved, load_rules("jisha"))
    assert _zmax_at(parts["roof"].solid, 4000, 3000, r=2.0) == pytest.approx(H + 3000 * S + 100, abs=3.0)


@pytest.mark.parametrize("name", list(CASES))
def test_3d_pick_shapes_for_multi_plane_roofs(name):
    """3D で囲んで直すの当たり判定の形（屋根全体と辺ごとの軒）も、面が複数の屋根で作れる。"""
    from core.pick import proxies
    make, outline = CASES[name]
    items = proxies(_house(make(), outline, name), load_rules("jisha"))["items"]
    ids = {it["id"] for it in items if it["group"] in ("roofs", "roof_edges")}
    assert "Y1" in ids and {"Y1:S", "Y1:N"} <= ids
