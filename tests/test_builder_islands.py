"""組み立ての後始末：離れた基礎の帯をつなぐ・1階から離れた小さなかけらを除く（物件C〜F、2026-11-03 夜）。"""
from __future__ import annotations

from manifold3d import Manifold
from shapely.geometry import box

from core.builder import bridge_islands, drop_loose_bits, footprint_of, union_all


def _block(x0, y0, x1, y1, z1=480.0):
    return Manifold.cube((x1 - x0, y1 - y0, z1)).translate((x0, y0, 0.0))


def test_island_strip_is_bridged_to_the_nearest_strip():
    ring = _block(0, 0, 9000, 150) + _block(0, 0, 150, 7000)
    island = _block(3000, 2000, 3130, 3000)                       # ほかの壁とつながらない内壁の下の帯
    out, n = bridge_islands(ring + island, 150.0, 480.0, box(0, 0, 9000, 7000))
    assert n == 1 and len(out.decompose()) == 1
    assert out.volume() > (ring + island).volume()
    assert out.bounding_box()[5] == 480.0                          # 基礎の天端より上には出ない


def test_bridge_does_not_cross_a_low_floor():
    ring = _block(0, 0, 9000, 150)
    island = _block(3000, 2000, 3130, 3000)
    allowed = box(0, 0, 9000, 7000).difference(box(2000, 100, 4000, 1990))   # 間に土間（低い床）
    out, n = bridge_islands(ring + island, 150.0, 480.0, allowed)
    assert n == 0 and len(out.decompose()) == 2                    # つなげないときは、そのまま（注意書きが残る）


def test_footprint_keeps_holes():
    frame = _block(0, 0, 1000, 1000) - _block(100, 100, 900, 900)
    assert abs(footprint_of(frame).area - (1000 * 1000 - 800 * 800)) < 1.0


def test_small_loose_bit_is_dropped_but_big_parts_stay():
    floor = _block(0, 0, 9000, 7000, 144.0)
    bit = _block(9500, 0, 10073, 144, 144.0)                       # 床の切れ端（実寸 573×144）
    annex = _block(0, 8000, 5000, 9000, 144.0)                     # 大きい離れ（1% より大きい）は残す
    (f, e), n = drop_loose_bits([union_all([floor, bit, annex]), Manifold()])
    assert n == 1 and len(f.decompose()) == 2 and e.is_empty()
