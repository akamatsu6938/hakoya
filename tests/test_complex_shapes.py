"""複雑な凹凸の家（U字・段々・斜めの壁・細い出っ張り・凹凸の多い外形）を、架空の家で組み立てて検査まで通す（実データ不要）。

★2026-11-03 作者「複雑な凹凸はできるようにしておいてほしい」。
読み取り（glb → building.json）は物件A（L字）・物件B（斜めの壁）以外の実物がないので、10件の glb で確かめる（要検証）。
ここでは組み立て（building.json → 部品）が崩れないことを確かめる：閉じた立体・部品ごとに1つの塊・部品どうしが重ならない・
窓が角の近くでも抜ける・三角（位置合わせ）が置ける。
"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import Polygon

from core.builder import build_model, drop_specks, union_all
from core.checks import part_checks
from core.export import overlap_checks, to_trimesh
from core.rules import load_rules

T = 160.0
SHAPES = {
    "u_shape": [(0, 0), (12000, 0), (12000, 9000), (8000, 9000), (8000, 4000), (4000, 4000), (4000, 9000), (0, 9000)],
    "steps": [(0, 0), (10000, 0), (10000, 3000), (8000, 3000), (8000, 6000), (6000, 6000), (6000, 8000), (0, 8000)],
    "diagonal": [(0, 0), (9000, 0), (9000, 5000), (6000, 8000), (0, 8000)],
    "bump": [(0, 0), (8000, 0), (8000, 5000), (4500, 5000), (4500, 7000), (3500, 7000), (3500, 5000), (0, 5000)],
    "zigzag": [(0, 0), (3000, 0), (3000, 910), (6000, 910), (6000, 0), (9000, 0), (9000, 6000), (6000, 6000),
               (6000, 5090), (3000, 5090), (3000, 6000), (0, 6000)],
}


def _house(outline, roof_slope=0.15, eave=3300.0):
    ring = list(outline)
    if not Polygon(ring).exterior.is_ccw:
        ring = ring[::-1]
    walls, openings = [], []
    pts = [np.array(v, float) for v in ring]
    k = len(pts)
    # へこんだ角（反時計回りで右に曲がる角）。物件A と同じく、壁の端を相手の壁の厚みの分まで伸ばして重ねる
    def turn(u, v):
        return u[0] * v[1] - u[1] * v[0]
    reflex = [turn(pts[j] - pts[j - 1], pts[(j + 1) % k] - pts[j]) < 0 for j in range(k)]
    for i in range(k):
        p, q = pts[i], pts[(i + 1) % k]
        L = float(np.linalg.norm(q - p))
        d = (q - p) / L
        n = np.array([-d[1], d[0]]) * T / 2                       # 家の中へ半分
        s0 = p + n - d * (T if reflex[i] else 0.0)
        s1 = q + n + d * (T if reflex[(i + 1) % k] else 0.0)
        i += 1
        walls.append({"id": f"W{i}", "level": "1F", "kind": "exterior", "start": list(s0), "end": list(s1),
                      "thickness_mm": T, "bottom_mm": 480, "height": "to_roof"})
        if L >= 2400:                                              # 長い壁には、角の近く（端から 300）に窓
            off = 300 + (T if reflex[i - 1] else 0.0)                # 壁の始点から測る（伸ばした分を足す）
            c = s0 + d * (off + 400)
            openings.append({"id": f"O{i}", "wall": f"W{i}", "kind": "window", "offset_mm": off, "width_mm": 800,
                             "sill_mm": 1524, "head_mm": 2624, "sill_fl_mm": 900, "head_fl_mm": 2000, "center": list(c)})
    poly = Polygon(ring)
    x0, y0, x1, y1 = poly.bounds
    mid_x = (x0 + x1) / 2
    from shapely.geometry import LineString
    cut = poly.intersection(LineString([(mid_x, y0 - 1), (mid_x, y1 + 1)]))   # 真ん中の縦の線の、家の中の部分
    y_in = min(min(y for _, y in g.coords) for g in getattr(cut, "geoms", [cut]))
    walls.append({"id": f"W{len(ring) + 1}", "level": "1F", "kind": "interior", "start": [mid_x, y_in + T / 2], "end": [mid_x, y_in + 2500],
                  "thickness_mm": 130, "bottom_mm": 624, "height": 3024})
    t_v = 100 * math.sqrt(1 + roof_slope ** 2)
    zs = [eave + roof_slope * (y - y0) for _, y in ring]
    roof = {"shape": "shed", "pitch": [roof_slope * 10, 10], "slope": roof_slope, "low_side": "S", "thickness_mm": 100.0,
            "top_plane": {"a": 0.0, "b": roof_slope, "c": eave - roof_slope * y0},
            "bottom_plane": {"a": 0.0, "b": roof_slope, "c": eave - roof_slope * y0 - t_v},
            "top_z_range_mm": [round(min(zs)), round(max(zs))], "footprint": [list(map(float, p)) for p in ring],
            "eave_mm": {"W": 0, "E": 0, "S": 0, "N": 0}}
    return {"levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}], "walls": walls, "openings": openings,
            "roof": roof, "roofs": [{"id": "Y1", "level": "1F", "kind": "main", **roof}],
            "foundation": {"top_mm": 480, "outline": [list(map(float, p)) for p in ring], "footings": []},
            "porches": [], "porch_soffits": [], "rooms": []}


@pytest.fixture(scope="module")
def built():
    rules = load_rules("jisha")
    return {name: build_model(_house(outline), rules) for name, outline in SHAPES.items()}, rules


@pytest.mark.parametrize("name", list(SHAPES))
def test_complex_outline_builds_closed_parts_without_floating_pieces(built, name):
    parts, rules = built[0][name], built[1]
    scale = float(rules["model"]["scale"])
    objects: dict[str, list] = {}
    for k, p in parts.items():
        assert str(p.solid.status()).endswith("NoError") and p.solid.volume() > 0, (name, k)
        objects.setdefault(p.object_key or p.key, []).append(p.solid)
        checks = part_checks(k, p.label, to_trimesh(p, scale, for_print=True), [x.volume() for x in p.solid.decompose()])
        assert not [c for c in checks if c["level"] == "高"], (name, k, checks)
    for key, solids in objects.items():
        # 部品（オブジェクト）ごとに1つの塊（合体の計算でできる厚みゼロのかけらは、書き出しと同じく数えない）
        assert len(drop_specks(union_all(solids)).decompose()) == 1, (name, key)
    assert not [c for c in overlap_checks(parts, scale) if c["level"] == "高"], name


@pytest.mark.parametrize("name", list(SHAPES))
def test_corner_blocks_are_placed(built, name):
    parts = built[0][name]
    note = " ".join(parts["roof"].notes)
    assert "ずれ止めの三角" in note and "0 か所" not in note
    assert "位置合わせの三角" in " ".join(parts["foundation"].notes)


@pytest.mark.parametrize("name", list(SHAPES))
def test_windows_near_corners_are_cut(built, name):
    """角から 300mm の所の窓も抜けている（窓の真ん中の高さで、壁の中に模型の材料がない）。"""
    parts = built[0][name]
    b = _house(SHAPES[name])
    walls = union_all([parts["exterior1"].solid, parts["floor1"].solid])
    from core.builder import prism
    for o in b["openings"]:
        cx, cy = o["center"]
        z = (o["sill_mm"] + o["head_mm"]) / 2
        probe = prism(Polygon([(cx - 5, cy - 5), (cx + 5, cy - 5), (cx + 5, cy + 5), (cx - 5, cy + 5)]), z - 5, z + 5)
        assert (walls ^ probe).is_empty(), (name, o["id"])
