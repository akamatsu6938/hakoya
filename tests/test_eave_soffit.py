"""軒天（typeESnok）。★11/4 作者「寄棟の軒天がない（サッシのちょうど上に来るはず）」。
glb の軒天の部材を屋根に結びつけ、軒天の高さから屋根までを外壁の外から軒先まで埋める（箱の軒）。架空の家で確かめる。"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Polygon

from adapters.glb_reader import Part, Primitive
from adapters.walkinhome import _attach_soffits
from core import overrides as ov
from core.builder import build_model, prism
from core.export import to_trimesh
from core.rules import load_rules
from tests.test_roof_multi import RECT, _house, hip

# 外壁の外の面（屋根は 8000×6000、軒の出は約 600）。★隅棟の線が外壁の角をちょうど通る数字（600 そろい）だと、
# 軒天がなくても屋根と1階の立体が閉じない（作りものの家だけで起きる。11/4。デモの家が届いたら確かめる）ので少しずらす
INNER = [(610, 590), (7395, 590), (7395, 5415), (610, 5415)]
Z_SOFFIT = 3150.0                                              # 軒先の屋根板の裏（3195.6）より低い
BANDS = {"S": [(0, 0), (8000, 0), (8000, 590), (0, 590)], "N": [(0, 5415), (8000, 5415), (8000, 6000), (0, 6000)],
         "W": [(0, 0), (610, 0), (610, 6000), (0, 6000)], "E": [(7395, 0), (8000, 0), (8000, 6000), (7395, 6000)]}


def _hip_with_soffits(z=Z_SOFFIT):
    b = _house(hip(), INNER, "hip")
    for r in (b["roof"], b["roofs"][0]):
        r["footprint"] = [list(map(float, p)) for p in RECT]
        r["eave_mm"] = {"W": 610, "E": 605, "S": 590, "N": 585}
        r["soffits"] = [{"side": s, "polygon": [list(map(float, p)) for p in poly], "source": f"glb:typeESnok#{k}"}
                        for k, (s, poly) in enumerate(BANDS.items(), 1)]
        r["soffit_z_mm"] = z
    return b


def _zmin_at(solid, x, y, r=5.0):
    probe = solid ^ prism(Polygon([(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r)]), -1000, 20000)
    return None if probe.is_empty() else probe.bounding_box()[2]


def test_soffit_closes_the_eaves_at_its_height():
    parts = build_model(_hip_with_soffits(), load_rules("jisha"))
    roof = parts["roof"].solid
    for x, y in ((4000, 5700), (4000, 300), (300, 3000), (7700, 3000)):
        assert _zmin_at(roof, x, y) == pytest.approx(Z_SOFFIT, abs=0.5)        # 軒の裏は軒天の高さで水平
    assert _zmin_at(roof, 4000, 3000) > Z_SOFFIT + 100                         # 家の中（外壁の内側）は埋めない
    assert any("軒天" in n and "FL+2526" in n for n in parts["roof"].notes)
    assert all(to_trimesh(p, 0.02).is_watertight for p in parts.values())


def test_no_soffit_in_the_glb_means_no_box():
    b = _hip_with_soffits()
    for r in (b["roof"], b["roofs"][0]):
        r.pop("soffits"), r.pop("soffit_z_mm")
    roof = build_model(b, load_rules("jisha"))["roof"].solid
    assert _zmin_at(roof, 4000, 5700) > Z_SOFFIT + 40                          # 軒の裏は斜めのまま（いままでどおり）


def test_soffit_higher_than_the_eave_is_skipped_with_a_note():
    parts = build_model(_hip_with_soffits(z=3400.0), load_rules("jisha"))
    assert any("軒先より高い" in n for n in parts["roof"].notes)


def test_soffit_follows_a_longer_eave():
    """北の軒の出を 300 伸ばすと、北の軒天も同じだけ伸びる。"""
    b = _hip_with_soffits()
    for r in (b["roof"], b["roofs"][0]):
        r["eave_glb_mm"] = dict(r["eave_mm"])
        r["eave_mm"] = {**r["eave_mm"], "N": 885}
    roof = build_model(b, load_rules("jisha"))["roof"].solid
    # 伸ばした所（y=6100）の屋根板の裏は 3165.6。軒天が伸びていれば、いちばん下は軒天の 3150
    assert _zmin_at(roof, 4000, 6100) == pytest.approx(Z_SOFFIT, abs=0.5)


def test_soffit_height_can_be_edited_and_moves_with_the_roof():
    b = _hip_with_soffits()
    moved, res = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "soffit_z_mm", "from": Z_SOFFIT, "to": 3100}])
    assert res[0]["status"] == "applied" and moved["roofs"][0]["soffit_z_mm"] == 3100
    moved, _ = ov.apply(b, [{"id": "OV1", "target": "Y1", "field": "eave_tip_mm", "from": 3300, "to": 3400}])
    assert moved["roofs"][0]["soffit_z_mm"] == Z_SOFFIT + 100                 # 屋根を上げると軒天も一緒に上がる
    with pytest.raises(ValueError):
        ov.check_value("soffit_z_mm", 30)


def _snok(node, poly, z):
    xy = np.array(poly, float)
    v = np.c_[xy, np.full(len(xy), z)]
    up = np.c_[xy, np.full(len(xy), z + 400)]                 # 板の外の縁の立ち上がり（水平でない面は形に使わない）
    verts = np.vstack([v, up])
    tris = np.array([[0, 1, 2], [0, 2, 3], [0, 1, 5], [0, 5, 4]])
    return Part(node=node, name="typeESnok", type_code="typeESnok",
                primitives=[Primitive(material="Material(BuiSNOK_NURA)", code="SNOK_NURA", vertices=verts, triangles=tris)])


def test_attach_soffits_reads_side_height_and_shape():
    roofs = [{"id": "Y1", "kind": "main", "footprint": [list(map(float, p)) for p in RECT]}]
    parts = [_snok(k, poly, Z_SOFFIT) for k, poly in enumerate(BANDS.values(), 8)]
    assert _attach_soffits(parts, roofs) == []
    r = roofs[0]
    assert r["soffit_z_mm"] == int(Z_SOFFIT) and sorted(s["side"] for s in r["soffits"]) == ["E", "N", "S", "W"]
    north = next(s for s in r["soffits"] if s["side"] == "N")
    assert Polygon(north["polygon"]).area == pytest.approx(8000 * 585, rel=1e-3) and north["source"] == "glb:typeESnok#9"


def test_attach_soffits_notes_different_heights():
    roofs = [{"id": "Y1", "kind": "main", "footprint": [list(map(float, p)) for p in RECT]}]
    parts = [_snok(8, BANDS["N"], 3150), _snok(9, BANDS["S"], 3250), _snok(10, BANDS["E"], 3150)]
    notes = _attach_soffits(parts, roofs)
    assert roofs[0]["soffit_z_mm"] == 3150 and any("100mm 違う" in n for n in notes)
