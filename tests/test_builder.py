"""模型の組み立て（core/builder.py）と 3MF（core/threemf.py）を確かめる。前半は架空の家（実データ不要）。"""
from __future__ import annotations

import json
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from core.builder import build_model, union_all
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
    assert set(parts) == {"foundation", "floor1", "exterior1", "roof", "roof_ext"}
    for p in parts.values():
        assert not p.solid.is_empty() and str(p.solid.status()).endswith("NoError")
        assert p.solid.volume() > 0


def test_window_is_cut_and_walls_follow_roof(parts):
    ext = parts["exterior1"].solid
    obj = union_all([ext, parts["floor1"].solid])
    # オブジェクト全体（外壁の表面＋中身＋内壁）を窓の高さ（2000）で切ると、窓のない高さ（1000）より
    # 「窓の幅 1700 × 壁の厚み 160」だけ小さい（自社は窓の枠を作らない：作者指定）
    diff = obj.slice(1000.0).area() - obj.slice(2000.0).area()
    assert abs(diff - 1700 * 160) < 1.0
    # ざらざらにする表面の層（外の面から 60mm）は、窓とそのまわり 75mm ずつの分だけ減る
    skin_diff = ext.slice(1000.0).area() - ext.slice(2000.0).area()
    assert abs(skin_diff - (1700 + 2 * 75) * 60) < 1.0
    # 1階の外壁は天井の高さ（FL 624 + 2400 = 3024）で切れている
    v = np.asarray(ext.to_mesh().vert_properties)[:, :3]
    assert abs(v[:, 2].max() - 3024) < 1
    # 天井より上は屋根の外壁：西（x=0）は低く、東（x=6000）は高い。屋根の下の面 z = 0.15x + 3799 で切れている
    u = np.asarray(parts["roof_ext"].solid.to_mesh().vert_properties)[:, :3]
    assert abs(u[:, 2].min() - 3024) < 1
    assert abs(u[u[:, 0] < 1, 2].max() - 3799) < 1 and abs(u[u[:, 0] > 5999, 2].max() - (0.15 * 6000 + 3799)) < 1


def test_door_cut_to_top_when_wall_above_is_too_thin(parts):
    """ドアの上端（3024）と内壁の上端（3024）の差が最小の太さ未満なので、上まで抜ける（SPEC §8）。"""
    from shapely.geometry import Point, Polygon as SPolygon
    from shapely.ops import unary_union
    floor1 = parts["floor1"].solid
    sl = unary_union([SPolygon(p) for p in floor1.slice(3010.0).to_polygons()])  # ドアの上端より上で切る
    assert not sl.contains(Point(3000, 1475))       # ドアの真ん中（W5 の始点から 1000〜1790）は、天井まで抜けている
    assert sl.contains(Point(3000, 3000))           # ドアのない所は壁がある


def test_roof_has_corner_blocks_and_flip(parts):
    roof = parts["roof"]
    assert roof.flip_for_print
    slab = 6100 * 4100 * 101  # 屋根板（4辺を 50mm ずつ出す。鉛直の厚み 101）
    assert slab < roof.solid.volume() < slab * 1.25   # 屋根板＋三角（天井の高さから屋根の裏までの三角柱）
    assert any("4 か所" in n for n in roof.notes)


def test_fuzzy_skin_only_on_exterior(parts):
    assert [k for k, p in parts.items() if p.fuzzy_skin] == ["exterior1", "roof_ext"]
    assert parts["exterior1"].object_key == "floor1" and parts["roof_ext"].object_key == "roof"


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
    assert fuzzy == ["1階の外壁（表面）", "屋根の外壁"]


def test_test_pieces_fit_without_overlap():
    """試験片：組んだときに重ならない（はめあいの隙間がある）。すべて1つの閉じた立体。"""
    from core.testpiece import build_pieces
    p = build_pieces(load_rules("jisha"))
    for label, solid, _ in p.values():
        assert str(solid.status()).endswith("NoError") and len(solid.decompose()) == 1, label
    assert abs((p["wall"][1] ^ p["roof"][1]).volume()) < 1e-6
    assert abs((p["foundation"][1] ^ p["floor"][1]).volume()) < 1e-6


@pytest.mark.realdata
@pytest.mark.slow
def test_inspection_images():
    """検品の材料：面ごとに模型の図・glb の図・部材の一覧ができ、立面にはその面の外壁と開口だけが入る。"""
    from core.paths import out_dir
    from core.render import VIEWS, render_all
    render_all("物件A")
    d = out_dir("物件A") / "inspect"
    for v in VIEWS:
        assert (d / f"{v}_model.png").exists() and (d / f"{v}_glb.png").exists()
    s = json.loads((d / "S.json").read_text(encoding="utf-8"))
    ops = [e["id"] for e in s["elements"] if e["type"] == "opening"]
    walls = {e["id"] for e in json.loads((d / "plan.json").read_text(encoding="utf-8"))["elements"] if e["type"] == "wall"}
    assert len(ops) == 3                                  # 南の窓は3つ（北の窓は入らない）
    assert len(walls) == 28                               # 平面にはすべての壁
    for e in s["elements"]:
        x0, y0, x1, y1 = e["bbox_px"]
        assert 0 <= x0 <= x1 <= s["size_px"][0] and 0 <= y0 <= y1 <= s["size_px"][1]



def test_window_frames_can_be_switched_on():
    """会社ルールで窓の枠を作る設定にすると、枠（縦枠2本 50×80）が入る。"""
    rules = load_rules("jisha")
    rules["model"]["window_frames"] = True
    p = build_model(_tiny_house(), rules)
    obj = union_all([p["exterior1"].solid, p["floor1"].solid])
    diff = obj.slice(1000.0).area() - obj.slice(2000.0).area()
    assert abs(diff - (1700 * 160 - 2 * 50 * 80)) < 1.0
