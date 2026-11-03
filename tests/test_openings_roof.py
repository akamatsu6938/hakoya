"""開口（adapters/openings.py）と屋根（adapters/roof.py）を、架空の部材で確かめる（実データ不要）。"""
from __future__ import annotations

import numpy as np
from shapely.geometry import box

from adapters.glb_reader import Part, Primitive
from adapters.openings import assign_wall, classify, hole, snap_door_heads
from adapters.roof import detect_roof
from adapters.walls import Wall


def _box_prim(code, x0, x1, y0, y1, z0, z1):
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], dtype=float)
    tri = np.array([[0, 1, 2], [1, 3, 2], [4, 6, 5], [5, 6, 7], [0, 4, 1], [1, 4, 5],
                    [2, 3, 6], [3, 7, 6], [0, 2, 4], [2, 6, 4], [1, 5, 3], [3, 5, 7]])
    return Primitive(material=f"Material(Bui{code})", code=code, vertices=v, triangles=tri)


def test_outset_sliding_door_uses_sill_not_rail():
    """壁の外に吊る引き戸：レール（穴の約2倍の長さ、天井より上）ではなく、床の敷居で穴を決める。"""
    p = Part(node=3, name="typeETate", type_code="typeETate", primitives=[
        _box_prim("TATE_KURI", 0, 790, 0, 144, 624, 624.5),
        _box_prim("TATE_SDOR", 0, 790, 144, 172, 624, 3024),
        _box_prim("TATE_HTOT", 60, 85, 140, 174, 1384, 1464),
        _box_prim("TATE_OSTN", 0, 1550, 144, 194, 3024, 3084),
    ])
    fp, sill, head = hole(p)
    x0, y0, x1, y1 = fp.bounds
    assert (round(x0), round(x1), round(y0), round(y1)) == (0, 790, 0, 144)
    assert (round(sill), round(head)) == (624, 3024)
    assert classify(p, sill, 624) == ("door", "outset_sliding")


def test_fixed_window_ignores_top_kuri():
    """FIX窓の上端にある KURI は敷居ではないので、穴の平面の形には使わない。"""
    p = Part(node=45, name="typeETate", type_code="typeETate", primitives=[
        _box_prim("TATE_KURI", 0, 1700, 90, 90, 2804, 2804),
        _box_prim("TATE_MGLS", 0, 1700, 80, 100, 1089, 2759),
        _box_prim("TATE_GMAD", 0, 1700, 0, 180, 1024, 2824),
    ])
    fp, sill, head = hole(p)
    assert fp.area > 1000 and (round(sill), round(head)) == (1024, 2824)
    assert classify(p, sill, 624) == ("window", "fixed")
    wall = Wall(start=np.array([-100.0, 90.0]), end=np.array([2000.0, 90.0]), thickness=180, kind="standalone", bottom=480, top=2824)
    assert assign_wall(fp, sill, head, [wall]) is wall


def test_sliding_window_style():
    p = Part(node=4, name="typeETate", type_code="typeETate", primitives=[
        _box_prim("TATE_MADO", 0, 395, 0, 160, 2274, 2624), _box_prim("TATE_MADO", 395, 790, 0, 160, 2294, 2604),
        _box_prim("TATE_GMAD", 0, 790, 0, 160, 2274, 2624)])
    _, sill, _ = hole(p)
    assert classify(p, sill, 624) == ("window", "sliding")


def _roof_part(slope=0.15, thick=100.0, x0=-11011.0, x1=-819.0, y0=-11011.0, y1=91.0, c=4951.68):
    def plane(cc, code, flip):
        pts = np.array([[x, y, slope * x + cc] for x in (x0, x1) for y in (y0, y1)])
        tri = np.array([[0, 2, 1], [1, 2, 3]]) if not flip else np.array([[0, 1, 2], [1, 3, 2]])
        return Primitive(material=f"Material(Bui{code})", code=code, vertices=pts, triangles=tri)
    vertical = thick * np.sqrt(1 + slope ** 2)
    gutter = _box_prim("YANE_NTOI", x0 - 145, x0, y0, y1, 3199, 3350)
    return Part(node=7, name="typeEYane", type_code="typeEYane",
                primitives=[plane(c, "YANE_MAIN", False), plane(c - vertical, "YANE_NURA", True), gutter])


def test_shed_roof_pitch_direction_thickness_and_eaves():
    ext = box(-11010, -11010, -817, 90)
    r = detect_roof(_roof_part(), ext)
    assert r["shape"] == "shed" and r["pitch"] == [1.5, 10] and r["low_side"] == "W"
    assert abs(r["thickness_mm"] - 100) < 0.5
    assert all(abs(v) <= 2 for v in r["eave_mm"].values())        # 屋根板は外壁とそろう
    assert r["eave_with_gutter_mm"]["W"] == 146                      # 146 は軒樋


def test_non_planar_roof_is_read_as_planes():
    """上の面が1枚の平面でない屋根は、面ごとに分けて読む（2026-11-03 作者「片流れ以外もできるように」。
    以前は「片流れ以外は対象外」で unknown にしていた）。"""
    p = _roof_part()
    p.primitives[0].vertices[0, 2] += 500  # 上の面を折る（片流れでない）
    roof = detect_roof(p, box(-11010, -11010, -817, 90))
    assert roof["shape"] in ("gable", "hip", "multi") and len(roof["planes"]) >= 2


def test_roof_without_usable_planes_is_unknown():
    p = _roof_part()
    top = p.primitives[0]
    top.vertices = top.vertices * np.array([0.001, 0.001, 1.0])      # 面が小さすぎる（0.1m² 未満のくず）
    top.vertices[0, 2] += 500
    assert detect_roof(p, box(-11010, -11010, -817, 90))["shape"] == "unknown"


def test_door_head_snaps_to_rule_only_when_close():
    items = [{"kind": "door", "head_fl_mm": 2370.0, "head_mm": 2994.0},
             {"kind": "door", "head_fl_mm": 2000.0, "head_mm": 2624.0},
             {"kind": "window", "head_fl_mm": 2370.0, "head_mm": 2994.0}]
    snap_door_heads(items, 624.0, 2400.0)
    assert [o["head_fl_mm"] for o in items] == [2400.0, 2000.0, 2370.0]
    assert items[0]["head_mm"] == 3024.0 and "assumptions" in items[0]
