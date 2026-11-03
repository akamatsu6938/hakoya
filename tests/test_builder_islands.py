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


def _w(i, s, e, level="1F", t=162.0):
    return {"id": f"W{i}", "start": list(s), "end": list(e), "thickness_mm": t, "level": level}


def test_opening_running_into_the_next_collinear_wall_is_cut_there_too():
    """★物件E の窓 O1（11/3 深夜）：一直線に続く隣の外壁に 30mm かかり、窓の中に細い壁が残った。"""
    from core.builder import openings_by_wall_of
    walls = [_w(1, (0, 0), (4000, 0)), _w(2, (8000, 8), (4000, 8)),          # 続きの壁（向きが逆・8mm ずれ）
             _w(3, (4000, 0), (4000, 3000)), _w(4, (4000, 0), (9000, 0), level="2F")]
    ops = [{"id": "O1", "wall": "W1", "kind": "window", "offset_mm": 3500.0, "width_mm": 530.0},
           {"id": "O2", "wall": "W1", "kind": "window", "offset_mm": 1000.0, "width_mm": 800.0}]
    by = openings_by_wall_of(walls, ops)
    assert [o["id"] for o in by["W1"]] == ["O1", "O2"]
    (v,) = by["W2"]
    assert v["_from"] == "W1" and abs(v["offset_mm"] - 3970.0) < 1e-6 and abs(v["width_mm"] - 30.0) < 1e-6
    assert "W3" not in by and "W4" not in by                                   # 直交する壁・別の階は抜かない


def test_loose_bits_keep_tall_parts_and_drop_floor_scraps():
    """★11/4 の見直し：離れた小さなかけらのうち、床の板の切れ端（高さが床の板以下）だけを除く。離れて立つ柱は残す（検査で知らせる）。"""
    from shapely.geometry import box as sbox

    from core.builder import drop_loose_bits, prism
    big = prism(sbox(0, 0, 10000, 10000), 480, 624)                 # 床の板（高さ 144）
    post = prism(sbox(12000, 0, 12120, 120), 480, 2880)             # 離れて立つ柱
    scrap = prism(sbox(0, 11000, 500, 11140), 480, 624)             # 床の板の切れ端
    (kept,), n = drop_loose_bits([big + post + scrap], max_height=624 - 480 + 1)
    assert n == 1 and len(kept.decompose()) == 2
    assert any(abs(p.bounding_box()[0] - 12000) < 1 for p in kept.decompose())  # 柱は残る
    (kept,), n = drop_loose_bits([big + post + scrap])                # 高さを見ないと、柱も消える（前の動き）
    assert n == 2
