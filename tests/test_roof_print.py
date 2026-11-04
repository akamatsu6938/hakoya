"""屋根の印刷の分け方と向き（core/print_split.py・core/export.py print_pieces）。架空の家で確かめる（実データ不要）。
★11/4 作者「入らない屋根は屋根の線で分けて、のりで付ける」「屋根の形ごとに最適な印刷の仕方に」。"""
from __future__ import annotations

import math

import numpy as np
import pytest
from shapely.geometry import Polygon

from core import print_split as ps
from core.builder import build_model, drop_specks, union_all
from core.export import print_pieces
from core.rules import load_rules
from tests.test_roof_multi import RECT, _house, gable, hip

RULES = load_rules("jisha")
LIMIT = ps.BED_MM - 2 * float(RULES["model"]["print_bed_margin_mm"])


def _pieces(b):
    parts = build_model(b, RULES)
    return parts, print_pieces("roof", parts, RULES)


def _object_volume(parts, key="roof"):
    return union_all([p.solid for p in parts.values() if (p.object_key or p.key) == key]).volume()


@pytest.mark.parametrize("planes,shape,count", [(hip, "hip", 4), (gable, "gable", 2)])
def test_multi_plane_roofs_split_into_faces_and_keep_their_volume(planes, shape, count):
    parts, pcs = _pieces(_house(planes(), RECT, shape))
    assert len(pcs) == count and len({p["name"] for p in pcs}) == count
    total = sum(drop_specks(union_all([s for s in p["solids"].values() if not s.is_empty()])).volume() for p in pcs)
    assert total == pytest.approx(_object_volume(parts), rel=0.005)            # 切っても体積は変わらない
    assert all(p["fits"] for p in pcs)


def test_each_piece_lies_with_its_top_surface_on_the_bed():
    parts, pcs = _pieces(_house(hip(), RECT, "hip"))
    for p in pcs:
        v = np.vstack([m[0] for m in p["meshes"].values()])
        assert v[:, 2].min() == pytest.approx(0.0, abs=1e-6)
        on_bed = 0.0
        for verts, tris in p["meshes"].values():
            t = verts[tris]
            flat = (np.abs(t[:, :, 2]).max(axis=1) < 1e-3)
            n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            on_bed += float((np.linalg.norm(n, axis=1) / 2)[flat].sum())
        assert on_bed > 1000.0                                                    # 上の面が床にぴったり（模型 mm²）


def test_shed_roof_is_one_piece_face_down():
    from tests.test_builder import _tiny_house
    parts, pcs = _pieces(_tiny_house())
    assert len(pcs) == 1 and pcs[0]["fits"]
    R = ps.face_down_rotation({"a": 0.15, "b": 0.0, "c": 0.0})
    n = np.array([-0.15, 0.0, 1.0]) / math.hypot(0.15, 1.0)
    assert np.allclose(R @ n, [0, 0, -1], atol=1e-9)                              # 上の面の向きが真下へ
    assert np.isclose(np.linalg.det(R), 1.0)                                      # 裏返らない（三角形の向きはそのまま）


def test_flat_face_turns_over():
    R = ps.face_down_rotation({"a": 0.0, "b": 0.0, "c": 0.0})
    assert np.allclose(R, np.diag([1.0, -1.0, -1.0]))


def test_best_yaw_fits_a_long_trapezoid_diagonally():
    trap = np.array([[0, 0], [283.9, 0], [158.1, 133.3], [125.8, 133.3]])        # 物件C の台形の面くらい（模型 mm）
    deg, L = ps.best_yaw(trap, LIMIT)
    assert L <= LIMIT and 30 < (deg % 180) < 150
    deg, L = ps.best_yaw(np.array([[0, 0], [240, 0], [240, 100], [0, 100]]), LIMIT)
    assert deg == 0.0                                                              # 入るならそのまま
    deg, L = ps.best_yaw(np.array([[0, 0], [300, 0], [300, 100], [0, 100]]), LIMIT)
    assert L > LIMIT                                                               # どう回しても入らない


def test_too_long_face_is_cut_across_the_eave():
    from tests.test_builder import _tiny_house
    b = _tiny_house()
    long_x = 16000.0                                                               # 16m の家（模型 320mm）
    for w in b["walls"]:
        w["start"] = [min(c, long_x) if c >= 6000 - 81 else c for c in w["start"]]
        w["end"] = [min(c, long_x) if c >= 6000 - 81 else c for c in w["end"]]
    for key in ("start", "end"):
        for w in b["walls"]:
            w[key] = [long_x - 81 if abs(w[key][0] - (6000 - 81)) < 1 else (long_x if w[key][0] == 6000 else w[key][0]), w[key][1]]
    b["walls"] = [w for w in b["walls"] if w["id"] != "W5"]
    b["openings"] = [o for o in b["openings"] if o["wall"] != "W5"]
    fp = [[0, 0], [long_x, 0], [long_x, 4000], [0, 4000]]
    b["roof"]["footprint"] = fp
    b["foundation"]["outline"] = fp
    parts, pcs = _pieces(b)
    assert len(pcs) >= 2 and all(p["fits"] for p in pcs)
    assert all(any("半分に切った" in w for w in p["warnings"]) for p in pcs)


def test_fuzzy_skin_is_removed_near_the_cuts():
    parts = build_model(_house(gable(), RECT, "gable"), RULES)
    members = {k: p.solid for k, p in parts.items() if (p.object_key or p.key) == "roof"}
    if "roof_ext" not in members:
        pytest.skip("この家には屋根の外壁がない")
    pcs = ps.split_by_faces(members, parts["roof"].print_faces)
    band = 75.0
    for pc in pcs:
        ps.smooth_cuts(pc, ["roof_ext"], "roof", band)
        strip = union_all([ps._prism(c.buffer(band * 0.9, cap_style="flat"), -1e4, 1e5) for c in pc.cuts])
        ext = pc.solids.get("roof_ext")
        if ext is not None and not ext.is_empty():
            assert (ext ^ strip).volume() < 1.0                                    # 切り口の近くにざらざらの部品が残らない


def test_two_lean_roofs_are_split_by_roof_first():
    from core.builder_multi import _lean_print_faces
    from tests.test_two_story_roofs import _plane, S_LEAN, LEAN_HI, X0, X1, E
    west = {"top_plane": {"a": 0.0, "b": S_LEAN, "c": LEAN_HI - 3000 * S_LEAN}, "footprint": [[X0, -E], [3000, -E], [3000, 3000], [X0, 3000]]}
    east = {"top_plane": {"a": 0.0, "b": S_LEAN, "c": LEAN_HI - 3000 * S_LEAN}, "footprint": [[5000, -E], [X1, -E], [X1, 3000], [5000, 3000]]}
    faces = _lean_print_faces([west, east])
    assert [f.name for f in faces] == ["下屋1の屋根", "下屋2の屋根"]
    assert faces[0].region.intersection(faces[1].region).area < 1.0                # 受け持ちの範囲は重ならない
    assert faces[0].region.contains(Polygon(west["footprint"]).centroid)
