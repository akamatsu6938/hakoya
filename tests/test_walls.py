"""壁の作り直し（adapters/walls.py）を、架空の小さな家で確かめる（実データ不要）。

架空の家：外形 6000×4000（外の面）、外壁の厚み 160、真ん中に内壁（仕上げ面の間 140）で部屋が2つ。
同じ家を30度回して、斜めの壁でも同じ結果になるかも確かめる。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.affinity import rotate
from shapely.geometry import Polygon, box

from adapters import walls as wl

T_EXT, FIN, H0, H1 = 160.0, 70.0, 0.0, 2400.0


def _quad(a, b, z0, z1, outward):
    """a→b の垂直な長方形を2つの三角形で。outward の向きに法線が向くように並べる。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    p = [np.array([*a, z0]), np.array([*b, z0]), np.array([*b, z1]), np.array([*a, z1])]
    tris = np.array([[p[0], p[1], p[2]], [p[0], p[2], p[3]]])
    n = np.cross(tris[0][1] - tris[0][0], tris[0][2] - tris[0][0])[:2]
    if np.dot(n, outward) < 0:
        tris = tris[:, ::-1]
    return tris


def _rot(p, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return np.array([c * p[0] - s * p[1], s * p[0] + c * p[1]])


def _house(deg=0.0):
    R = lambda p: _rot(p, deg)  # noqa: E731
    Rv = lambda v: _rot(v, deg)  # noqa: E731
    W, D = 6000.0, 4000.0
    outer = [((0, 0), (W, 0), (0, -1)), ((W, 0), (W, D), (1, 0)), ((W, D), (0, D), (0, 1)), ((0, D), (0, 0), (-1, 0))]
    ext = np.concatenate([_quad(R(a), R(b), H0, H1, Rv(n)) for a, b, n in outer])
    t = T_EXT
    mid = 3000.0
    # 部屋1（x: t〜mid-FIN）と部屋2（x: mid+FIN〜W-t）の仕上げ面（法線は部屋の内側向き）
    r1 = [((t, t), (mid - FIN, t), (0, 1)), ((mid - FIN, t), (mid - FIN, D - t), (-1, 0)),
          ((mid - FIN, D - t), (t, D - t), (0, -1)), ((t, D - t), (t, t), (1, 0))]
    r2 = [((mid + FIN, t), (W - t, t), (0, 1)), ((W - t, t), (W - t, D - t), (-1, 0)),
          ((W - t, D - t), (mid + FIN, D - t), (0, -1)), ((mid + FIN, D - t), (mid + FIN, t), (1, 0))]
    fin = np.concatenate([_quad(R(a), R(b), H0, H1, Rv(n)) for a, b, n in r1 + r2])
    # 床は通り芯（外壁の外の面から 80 内側、内壁の芯）まで
    floors = [rotate(box(80, 80, mid, D - 80), deg, origin=(0, 0)), rotate(box(mid, 80, W - 80, D - 80), deg, origin=(0, 0))]
    return ext, fin, floors


def _reconstruct(deg=0.0):
    ext_f, fin_f, floors = _house(deg)
    ext = wl.exterior_walls(wl.group_planes(wl.vertical_triangles(ext_f, "glb:typeEGaih#1")), T_EXT, 0.0, [])
    wl.fill_inside_corners(ext)
    ext = wl.drop_covered(ext)
    fin_planes = wl.group_planes(wl.vertical_triangles(fin_f, "glb:typeEHeya#2"))
    ext_fp = wl.footprint(ext)
    rooms, region = wl.interior_region(floors, fin_planes, [], ext_fp)
    interior = wl.interior_walls(fin_planes, region, 130.0)
    wl.extend_to_junctions(interior, ext + interior)
    return ext, interior, ext_fp


@pytest.mark.parametrize("deg", [0.0, 30.0])
def test_exterior_walls(deg):
    ext, _, fp = _reconstruct(deg)
    assert len(ext) == 4
    assert all(abs(w.thickness - T_EXT) < 1e-6 for w in ext)
    expected = rotate(box(0, 0, 6000, 4000), deg, origin=(0, 0))
    assert abs(fp.convex_hull.area - expected.area) < 1.0          # 外形が外の面と一致
    assert abs(fp.area - (expected.area - expected.buffer(-T_EXT, join_style="mitre").area)) < 5.0  # 帯の面積


@pytest.mark.parametrize("deg", [0.0, 30.0])
def test_interior_wall_centerline_and_ends(deg):
    _, interior, _ = _reconstruct(deg)
    assert len(interior) == 1
    w = interior[0]
    assert abs(w.thickness - 130.0) < 1e-6
    assert abs(getattr(w, "_gap") - 2 * FIN) < 1e-6
    # 中心線は x=3000 の線（回したものを戻して確かめる）、両端は外壁の芯（80 と 3920）まで伸びている
    s, e = sorted([_rot(w.start, -deg), _rot(w.end, -deg)], key=lambda p: p[1])
    assert abs(s[0] - 3000) < 0.5 and abs(e[0] - 3000) < 0.5
    assert abs(s[1] - 80) < 0.5 and abs(e[1] - 3920) < 0.5


def test_back_to_back_faces_become_one_interior_wall():
    a = _quad((0, 0), (2000, 0), 0, 2400, (0, -1))      # 南向きの面（y=0）
    b = _quad((0, 90), (2000, 90), 0, 2400, (0, 1))     # 北向きの面（y=90）。間は 90
    ext = wl.exterior_walls(wl.group_planes(wl.vertical_triangles(np.concatenate([a, b]), "glb:typeEGaih#1")),
                            162.0, 0.0, [])
    merged = wl.merge_back_to_back(ext, 162.0, new_thickness=130.0, new_top=2400.0)
    assert len(merged) == 1
    w = merged[0]
    assert w.kind == "interior" and w.top == 2400.0
    assert abs(w.start[1] - 45) < 1e-6 and abs(w.thickness - 130) < 1e-6
    assert any(s.startswith("back_to_back") for s in w.assumptions)


def test_window_reveals_are_not_back_to_back():
    """窓と窓の間の柱型の脇の面（見込み 162mm、間 120mm）は背中合わせとみなさない。"""
    a = _quad((0, 0), (0, 162), 0, 2400, (1, 0))
    b = _quad((120, 0), (120, 162), 0, 2400, (-1, 0))
    ext = wl.exterior_walls(wl.group_planes(wl.vertical_triangles(np.concatenate([a, b]), "glb:typeEGaih#1")),
                            162.0, 0.0, [])
    assert all(w.kind == "exterior" for w in wl.merge_back_to_back(ext, 162.0))


def test_full_length_vertical_gap_splits_wall():
    """向かい合う面に、全長にわたって縦の抜けがあれば（下がり壁と腰の部分）、壁を上下に分ける。"""
    faces = np.concatenate([
        _quad((0, 0), (3000, 0), 624, 1004, (0, -1)), _quad((0, 0), (3000, 0), 2824, 3024, (0, -1)),
        _quad((0, 145), (3000, 145), 624, 1004, (0, 1)), _quad((0, 145), (3000, 145), 2824, 3024, (0, 1)),
    ])
    planes = wl.group_planes(wl.vertical_triangles(faces, "glb:typeEHeya#1"))
    region = box(-10, 0, 3010, 145)
    walls = wl.interior_walls(planes, region, 130.0)
    assert sorted((round(w.bottom), round(w.top)) for w in walls) == [(624, 1004), (2824, 3024)]
    # 高さが違うので、片方がもう片方に「覆われている」とはみなさない
    assert len(wl.drop_covered(walls, keep_kinds=(), ratio=0.5)) == 2


def test_door_gap_does_not_split_wall():
    """ドアのように一部だけ抜けている所では、壁を上下に分けない。"""
    def side(y, n):
        return [_quad((0, y), (1000, y), 624, 3024, n), _quad((1000, y), (1800, y), 2994, 3024, n),
                _quad((1800, y), (3000, y), 624, 3024, n)]
    faces = np.concatenate(side(0, (0, -1)) + side(145, (0, 1)))
    planes = wl.group_planes(wl.vertical_triangles(faces, "glb:typeEHeya#1"))
    walls = wl.interior_walls(planes, box(-10, 0, 3010, 145), 130.0)
    assert len(walls) == 1 and round(walls[0].bottom) == 624 and round(walls[0].top) == 3024


def test_inside_corner_is_filled():
    """L字の家の入隅（へこんだ角）に、すき間が残らない。"""
    pts = [(0, 0), (4000, 0), (4000, 2000), (2000, 2000), (2000, 4000), (0, 4000)]
    faces = []
    for a, b in zip(pts, pts[1:] + pts[:1]):
        d = np.subtract(b, a)
        outward = (d[1], -d[0])  # 反時計回りなので右手側が外
        faces.append(_quad(a, b, 0, 2400, outward))
    ext = wl.exterior_walls(wl.group_planes(wl.vertical_triangles(np.concatenate(faces), "glb:typeEGaih#1")), 160.0, 0.0, [])
    wl.fill_inside_corners(ext)
    fp = wl.footprint(ext)
    outline = Polygon(pts)
    expected = outline.difference(outline.buffer(-160, join_style="mitre"))
    assert abs(fp.area - expected.area) < 5.0
    assert fp.symmetric_difference(expected).area < 5.0


def test_upper_wall_keeps_its_bottom():
    """途中の高さから始まる壁（ポーチの上の垂れ壁）は、床からの壁と分けて下端を持つ。"""
    base = _quad((0, 0), (2000, 0), 480, 3000, (0, -1))
    upper = _quad((2000, 0), (4000, 0), 2824, 3000, (0, -1))
    ext = wl.exterior_walls(wl.group_planes(wl.vertical_triangles(np.concatenate([base, upper]), "glb:typeEGaih#1")),
                            162.0, 624.0, [])
    assert sorted(round(w.bottom) for w in ext) == [480, 2824]


def test_thin_cap_is_not_a_standalone_wall():
    cap = np.concatenate([_quad((0, 0), (2000, 0), 1004, 1024, (0, -1)), _quad((0, 145), (2000, 145), 1004, 1024, (0, 1))])
    assert wl.standalone_wall(cap, "glb:typeEKabe#61") is None
    wall = np.concatenate([_quad((0, 0), (2000, 0), 480, 2824, (0, -1)), _quad((0, 180), (2000, 180), 480, 2824, (0, 1))])
    w = wl.standalone_wall(wall, "glb:typeEKabe#51")
    assert w is not None and abs(w.thickness - 180) < 1e-6 and abs(w.length - 2000) < 1e-6
