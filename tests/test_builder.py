"""模型の組み立て（core/builder.py）と 3MF（core/threemf.py）を確かめる。前半は架空の家（実データ不要）。"""
from __future__ import annotations

import json
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from core.builder import build_model
from core.rules import load_rules


def _tiny_house():
    """外形 6000×4000、外壁 160、真ん中に内壁（ドア1つ）、南に窓1つ、片流れ（西が低い）の平屋。"""
    t = 160
    ext = [((0, t / 2), (6000, t / 2)), ((6000 - t / 2, 0), (6000 - t / 2, 4000)),
           ((6000, 4000 - t / 2), (0, 4000 - t / 2)), ((t / 2, 4000), (t / 2, 0))]
    walls = [{"id": f"W{i}", "level": "1F", "kind": "exterior", "start": list(s), "end": list(e), "thickness_mm": t,
              "bottom_mm": 480, "height": "to_roof"} for i, (s, e) in enumerate(ext, 1)]
    walls.append({"id": "W5", "level": "1F", "kind": "interior", "start": [3000, 80], "end": [3000, 3920],
                  "thickness_mm": 130, "bottom_mm": 624, "height": 3024})
    return {
        "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
        "walls": walls,
        "openings": [
            {"id": "O1", "wall": "W1", "kind": "window", "offset_mm": 1000, "width_mm": 1700, "sill_mm": 1524,
             "head_mm": 3024, "center": [1850, 80]},
            {"id": "O2", "wall": "W5", "kind": "door", "offset_mm": 1000, "width_mm": 790, "sill_mm": 624,
             "head_mm": 3024, "center": [3000, 1475]},
        ],
        "roof": {"shape": "shed", "top_plane": {"a": 0.15, "b": 0.0, "c": 3900.0},
                 "bottom_plane": {"a": 0.15, "b": 0.0, "c": 3799.0}, "top_z_range_mm": [3900, 4800],
                 "footprint": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]},
        "foundation": {"top_mm": 480, "outline": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]], "footings": []},
        "porches": [], "porch_soffits": [],
        "rooms": [],
    }


@pytest.fixture(scope="module")
def parts():
    return build_model(_tiny_house(), load_rules("jisha"))


def test_all_parts_are_closed_solids(parts):
    assert set(parts) == {"foundation", "floor1", "exterior1", "roof"}
    for p in parts.values():
        assert not p.solid.is_empty() and str(p.solid.status()).endswith("NoError")
        assert p.solid.volume() > 0


def test_window_is_cut_and_walls_follow_roof(parts):
    ext = parts["exterior1"].solid
    # 窓の高さ（2000）で切った断面は、窓のない高さ（1000）より「窓の幅 1700 × 壁の厚み 160」だけ小さい
    diff = ext.slice(1000.0).area() - ext.slice(2000.0).area()
    assert abs(diff - 1700 * 160) < 1.0
    # 西（x=0）は低く、東（x=6000）は高い：屋根の下の面 z = 0.15x + 3799 で切れている
    v = np.asarray(ext.to_mesh().vert_properties)[:, :3]
    west_top = v[v[:, 0] < 1, 2].max()
    east_top = v[v[:, 0] > 5999, 2].max()
    assert abs(west_top - 3799) < 1 and abs(east_top - (0.15 * 6000 + 3799)) < 1


def test_door_cut_to_top_when_wall_above_is_too_thin(parts):
    """ドアの上端（3024）と内壁の上端（3024）の差が最小の太さ未満なので、上まで抜ける（SPEC §8）。"""
    floor1 = parts["floor1"].solid
    sl = floor1.slice(3000.0)  # ドアの高さで切る：内壁がドアの所で途切れている
    polys = sl.to_polygons()
    assert len(polys) >= 2


def test_roof_has_corner_blocks_and_flip(parts):
    roof = parts["roof"]
    assert roof.flip_for_print
    slab = 6000 * 4000 * 101  # 屋根板（鉛直の厚み 101）
    assert slab < roof.solid.volume() < slab * 1.05   # 屋根板＋小さな三角ブロック
    assert any("4 か所" in n for n in roof.notes)


def test_fuzzy_skin_only_on_exterior(parts):
    assert [k for k, p in parts.items() if p.fuzzy_skin] == ["exterior1"]
    assert parts["exterior1"].object_key == "floor1"


# ---------------------------------------------------------------- 物件A（実データ）

@pytest.mark.realdata
@pytest.mark.slow
def test_real_model_export():
    from core.export import write_outputs
    from core.paths import out_dir
    s = write_outputs("物件A")
    checks = s["checks"]
    assert not [c for c in checks if c["level"] == "高"], checks
    assert not [c for c in checks if c["item"] == "islands"], checks
    # 基礎の外形 ≒ 203.9 × 222.0mm（SPEC §12 の受け入れ基準、±0.5mm）。基礎は母屋の外形
    f = next(c for c in checks if c["part"] == "foundation" and c["item"] == "build_volume")["message"]
    w, d = [float(x) for x in f.split("大きさ ")[1].split("mm")[0].split("×")[:2]]
    assert abs(w - 203.9) <= 0.5 and abs(d - 222.0) <= 0.5
    z = zipfile.ZipFile(out_dir("物件A") / "model" / "model.3mf")
    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    fuzzy = [p.find("metadata[@key='name']").get("value") for o in cfg for p in o.findall("part")
             if p.find("metadata[@key='fuzzy_skin']") is not None]
    assert fuzzy == ["1階の外壁"]
