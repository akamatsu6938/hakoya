"""★11/4 物件G〜M・デモ用（14件のまとめて検査）で見つかった形の問題の直し。架空の形で確かめる（実データ不要）。
実物での確かめは tests/test_walkinhome_real_more.py。"""
from __future__ import annotations

import numpy as np
from manifold3d import Manifold
from shapely.geometry import Polygon, box

from adapters import walls as wl
from adapters.walkinhome import _filled
from core.builder import bridge_edge_contacts, union_all
from core.checks import part_checks


def _wall(x0, y0, x1, y1, t=162.0, kind="exterior", top=None, bottom=480.0):
    return wl.Wall(start=np.array([x0, y0], float), end=np.array([x1, y1], float), thickness=t, kind=kind,
                   bottom=bottom, top=top)


# ---------------------------------------------------------------- 外壁の T字のすき間（デモ用の北西・南西の角）

def test_small_gap_to_a_perpendicular_wall_is_closed():
    north = _wall(-9190, 9, -820, 9)                 # 北の外壁（家の外まで袖壁として続く）。内側の面は y=-72
    west = _wall(-8199, -90, -8199, -10830)          # 西の外壁の端が y=-90（すき間 18mm）
    assert wl.close_t_gaps([north, west]) == 1
    assert west.start[1] == 9.0 and any("t_gap" in a for a in west.assumptions)   # 北の外壁の中心線まで伸びる


def test_tiny_and_large_gaps_are_left_alone():
    north = _wall(-9190, 9, -820, 9)
    tiny = _wall(-5000, -75, -5000, -3000)           # 2.8mm（物件A の垂れ壁の端と同じくらい）：触らない
    far = _wall(-3000, -160, -3000, -3000)           # 88mm：別の所（つながない）
    assert wl.close_t_gaps([north, tiny, far]) == 0
    assert tiny.start[1] == -75.0 and far.start[1] == -160.0


# ---------------------------------------------------------------- 背中合わせでも外周の壁は外壁のまま（物件H）

def test_back_to_back_on_the_outline_stays_exterior():
    upper = _wall(-7289, 3730, -7289, -982)          # 西の外壁（窓あり）
    lower = _wall(-7289, -982, -7289, -3568, t=130.0, kind="interior", top=3024.0)
    lower.assumptions.append("back_to_back: glb では背中合わせの外壁の面（間 162mm）")
    inner = _wall(-3000, 0, -3000, -2000, t=130.0, kind="interior", top=3024.0)   # 部屋と部屋の間（物件A の形）
    inner.assumptions.append("back_to_back: glb では背中合わせの外壁の面（間 90mm）")
    assert wl.keep_outline_back_to_back([upper, lower, inner], 162.0) == 1
    assert (lower.kind, lower.thickness, lower.top) == ("exterior", 162.0, None)
    assert inner.kind == "interior"


# ---------------------------------------------------------------- 家の外形：角の1点だけで触れる外壁でも閉じる（物件H）

def test_outline_closes_walls_touching_only_at_corners():
    ring = [box(0, 0, 6000, 162), box(6000, 162, 6162, 4000),            # 右下の角は1点だけで触れる
            box(0, 4000, 6000, 4162), box(-162, 162, 0, 4000)]
    from shapely.ops import unary_union
    fp = unary_union(ring)
    assert _filled(fp).area > 0.9 * 6000 * 4000


# ---------------------------------------------------------------- 基礎の外形：帯から輪が作れないときは外壁の外形（物件I）

def test_foundation_outline_falls_back_to_exterior_walls():
    from adapters.exterior_parts import foundation
    from adapters.glb_reader import Part, Primitive
    # C字の帯（右の辺がない）
    band = Polygon([(0, 0), (8000, 0), (8000, 150), (150, 150), (150, 5850), (8000, 5850), (8000, 6000), (0, 6000)])
    tris, verts = [], []
    import shapely
    for t in shapely.delaunay_triangles(band).geoms:
        if band.contains(t.representative_point()):
            base = len(verts)
            verts += [[x, y, 480.0] for x, y in list(t.exterior.coords)[:3]]
            tris.append([base, base + 1, base + 2])
    kiso = Part(node=1, name="typeEKiso", type_code="typeEKiso",
                primitives=[Primitive(material="Material(BuiKISO_ILOW)", code="KISO_ILOW", vertices=np.array(verts), triangles=np.array(tris))])
    clip = box(0, 0, 8000, 6000).buffer(1, join_style="mitre")
    out = foundation([kiso], 480.0, bridges=[], clip=clip)
    assert out["outline_method"] == "exterior_walls" and abs(out["area_m2"] - 48.0) < 0.1


# ---------------------------------------------------------------- 辺だけで接する所（物件H・K）と、閉じているかの判定

def test_edge_contact_is_bridged_and_judged_on_the_3mf_mesh():
    a = Manifold.cube([100.0, 100.0, 300.0])
    b = Manifold.cube([100.0, 100.0, 300.0]).translate([100.0, 100.0, 0.0])   # 縦の辺だけで接する
    m = union_all([a, b])
    fixed, n = bridge_edge_contacts(m)
    assert n == 1 and len(fixed.decompose()) == 1
    import trimesh
    mesh = fixed.to_mesh()
    t = trimesh.Trimesh(vertices=np.asarray(mesh.vert_properties)[:, :3], faces=np.asarray(mesh.tri_verts), process=True)
    assert t.is_watertight


def test_flat_edge_contact_is_not_bridged():
    a = Manifold.cube([100.0, 100.0, 100.0])
    b = Manifold.cube([100.0, 100.0, 100.0]).translate([100.0, 0.0, 100.0])  # 横の辺だけで接する（屋根の棟などと同じ向き）
    fixed, n = bridge_edge_contacts(union_all([a, b]))
    assert n == 0                                                            # 目立つ面に出っ張らないように、横の辺は触らない


def test_closed_mesh_touching_at_an_edge_is_low_not_high():
    import trimesh
    a = trimesh.creation.box(extents=(1, 1, 1))
    b = trimesh.creation.box(extents=(1, 1, 1))
    b.apply_translation((1, 1, 0))
    merged = trimesh.util.concatenate([a, b])
    merged.merge_vertices()
    checks = part_checks("roof_ext", "屋根の外壁", merged, [1.0, 1.0], raw_watertight=True)
    w = next(c for c in checks if c["item"] == "watertight")
    assert w["level"] == "低" and "接している所" in w["message"]
    checks = part_checks("roof_ext", "屋根の外壁", merged, [1.0], raw_watertight=False)
    assert next(c for c in checks if c["item"] == "watertight")["level"] == "高"
