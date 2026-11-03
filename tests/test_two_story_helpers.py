"""2階建て対応で足した小さな関数を、架空の部材で確かめる（実データ不要）。
全体の流れは物件Bの実データ（tests/test_walkinhome_real_b.py）で確かめる。"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

from adapters import walkinhome as wh
from adapters import walls as wl
from adapters.exterior_parts import foundation
from adapters.glb_reader import Part, Primitive
from adapters.openings import openings, snap_hanging_doors


def _quad(a, b, z0, z1, outward):
    a, b = np.asarray(a, float), np.asarray(b, float)
    p = [np.array([*a, z0]), np.array([*b, z0]), np.array([*b, z1]), np.array([*a, z1])]
    tris = np.array([[p[0], p[1], p[2]], [p[0], p[2], p[3]]])
    n = np.cross(tris[0][1] - tris[0][0], tris[0][2] - tris[0][0])[:2]
    return tris[:, ::-1] if np.dot(n, outward) < 0 else tris


def _area(tris):
    return float(sum(0.5 * np.linalg.norm(np.cross(t[1] - t[0], t[2] - t[0])) for t in tris))


def test_clip_faces_z_keeps_area_of_the_band():
    tri = np.array([[[0, 0, 0], [1000, 0, 0], [0, 0, 1000]]], dtype=float)
    assert wl.clip_faces_z(tri, None, None) is tri                       # 平屋は何もしない
    out = wl.clip_faces_z(tri, 200, 600)
    assert _area(out) == pytest.approx(240_000)                          # 高さ 200〜600 の部分の面積
    assert out[:, :, 2].min() >= 200 - 1e-9 and out[:, :, 2].max() <= 600 + 1e-9


def test_merge_exterior_pieces_joins_pieces_of_one_face():
    # 同じ外の面で、下端の違う2つのかけら（下屋の上から始まる部分を床まで下ろした後の形）
    faces = np.concatenate([_quad((0, 0), (3000, 0), 3624, 6000, (0, -1)), _quad((3000, 0), (4000, 0), 4049, 6000, (0, -1))])
    planes = wl.group_planes(wl.vertical_triangles(faces, "glb:typeEGaih#1"))
    ext = wl.exterior_walls(planes, 162.0, 3624.0, [])
    assert len(ext) == 2
    for w in ext:
        w.bottom = 3624.0
    merged = wl.merge_exterior_pieces(ext)
    assert len(merged) == 1 and merged[0].length == pytest.approx(4000)
    assert merged[0]._outer.t1 - merged[0]._outer.t0 == pytest.approx(4000)
    assert merged[0].top_glb == pytest.approx(6000)


def _box_prim(code, x0, x1, y0, y1, z0, z1):
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], dtype=float)
    tri = np.array([[0, 1, 2], [1, 3, 2], [4, 6, 5], [5, 6, 7], [0, 4, 1], [1, 4, 5],
                    [2, 3, 6], [3, 7, 6], [0, 2, 4], [2, 6, 4], [1, 5, 3], [3, 5, 7]])
    return Primitive(material=f"Material(Bui{code})", code=code, vertices=v, triangles=tri)


def _tate(node, prims):
    return Part(node=node, name="typeETate", type_code="typeETate", primitives=prims)


def test_openings_stacked_floor_fix_and_hanging_door():
    wall = wl.Wall(start=np.array([-100.0, 80.0]), end=np.array([9000.0, 80.0]), thickness=162, kind="exterior", bottom=480)
    lower = _tate(1, [_box_prim("TATE_GMAD", 0, 790, 0, 160, 624, 1654), _box_prim("TATE_MGLS", 0, 790, 70, 90, 650, 1630)])
    upper = _tate(2, [_box_prim("TATE_MADO", 0, 395, 0, 160, 1654, 3024), _box_prim("TATE_MADO", 395, 790, 0, 160, 1654, 3024)])
    floor_fix = _tate(3, [_box_prim("TATE_GMAD", 2000, 2790, 0, 160, 624, 3024), _box_prim("TATE_MGLS", 2000, 2790, 70, 90, 650, 3000)])
    hanging = _tate(4, [_box_prim("TATE_KURI", 5000, 5790, 0, 144, 1024, 1024.5), _box_prim("TATE_SDOR", 5000, 5790, 144, 172, 1024, 3024),
                        _box_prim("TATE_OSTN", 5000, 6550, 144, 194, 3024, 3084)])
    ops, orphans = openings([lower, upper, floor_fix, hanging], [wall], 624.0)
    assert orphans == [] and len(ops) == 3
    stacked = next(o for o in ops if o.get("sources"))
    assert (stacked["kind"], round(stacked["sill_mm"]), round(stacked["head_mm"])) == ("window", 624, 3024)
    assert stacked["sources"] == ["glb:typeETate#1", "glb:typeETate#2"]
    fix = next(o for o in ops if o["source"] == "glb:typeETate#3")
    assert fix["kind"] == "window"                                       # 床まで届いても窓
    door = next(o for o in ops if o["source"] == "glb:typeETate#4")
    assert door["kind"] == "door" and round(door["sill_fl_mm"]) == 400  # 床から浮いていてもドア
    exc = snap_hanging_doors(ops, 624.0)
    assert door["sill_fl_mm"] == 0 and door["sill_mm"] == 624 and [e["kind"] for e in exc] == ["door_above_floor"]


def _flat_rect(x0, y0, x1, y1, z):
    v = np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], dtype=float)
    return Primitive(material="Material(BuiKISO_ILOW)", code="KISO_ILOW", vertices=v, triangles=np.array([[0, 1, 2], [0, 2, 3]]))


def test_foundation_closes_a_broken_band_with_the_door():
    t = 15
    strips = [(0, 0, 4000, t), (0, 3000 - t, 4000, 3000), (0, 0, t, 3000),                 # 南・北・西
              (4000 - t, 0, 4000, 1000), (4000 - t, 1900, 4000, 3000)]                       # 東は玄関ドア（1000〜1900）で切れている
    kiso = [Part(node=9, name="typeEKiso", type_code="typeEKiso", primitives=[_flat_rect(*s, 480.0) for s in strips])]
    broken = foundation(kiso, 480.0)
    assert broken["area_m2"] < 1                                         # 直さないと帯のまま
    fixed = foundation(kiso, 480.0, bridges=[box(3900, 1000, 4100, 1900)], clip=box(0, 0, 4000, 3000))
    assert fixed["area_m2"] == pytest.approx(12.0, abs=0.01) and fixed["outline_method"] == "kiso_ilow+door_bridge"


def test_lower_roof_gable_and_wall_on_lower_roof():
    lean_to = {"footprint": [[0, -6000], [8000, -6000], [8000, 0], [0, 0]],
               "top_plane": {"a": 0.0, "b": 0.05, "c": 4100.0}, "bottom_plane": {"a": 0.0, "b": 0.05, "c": 4000.0}}

    def wall(a, b, bottom, top_glb):
        return wl.Wall(start=np.array(a, float), end=np.array(b, float), thickness=162, kind="exterior",
                       bottom=bottom, top_glb=top_glb)
    gable = wall((7919, -6000), (7919, 0), 3624, 3990)       # 1階の妻のかけら（上端は下屋の下の面まで）
    upper = wall((7919, 0), (7919, 4000), 3624, 6600)        # 2階の東の壁
    south = wall((0, 81), (8000, 81), 4104, 6600)            # 下屋の屋根の上から始まる2階の南の壁
    keep, dropped = wh._drop_lower_gable_walls([gable, upper, south], [lean_to])
    assert dropped == [gable] and keep == [upper, south]
    wh._extend_on_lower_roof(keep, [lean_to], 3624.0)
    assert south.bottom == 3624.0 and "on_lower_roof" in south.assumptions[0] and upper.bottom == 3624


def test_level_helpers():
    levels = [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}, {"name": "2F", "fl_mm": 3624, "ceiling_mm": 2400}]
    assert wh._level_bands(levels) == [(None, 3624.0), (3624.0, None)]
    assert wh._level_bands(levels[:1]) == [(None, None)]
    assert [wh._level_index(z, levels) for z in (384, 624, 3024, 3624, 5000)] == [0, 0, 0, 1, 1]
    lean_to = {"footprint": [[0, -6000], [8000, -6000], [8000, 0], [0, 0]], "bottom_plane": {"a": 0.0, "b": 0.05, "c": 4000.0}}
    main = {"footprint": [[0, 0], [8000, 0], [8000, 4000], [0, 4000]], "bottom_plane": {"a": -0.05, "b": 0.0, "c": 7000.0}}
    assert wh._roof_level(lean_to, levels) == 0 and wh._roof_level(main, levels) == 1
