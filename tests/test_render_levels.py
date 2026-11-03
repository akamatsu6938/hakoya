"""検品の図（core/render.py）の2階建ての分け方を、架空の家で確かめる（実データ不要）。"""
from __future__ import annotations

import numpy as np

from core.render import Canvas, elements, view_label, views_of


def _house(levels=("1F", "2F")):
    lv = [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}, {"name": "2F", "fl_mm": 3624, "ceiling_mm": 2400}][:len(levels)]
    walls = [{"id": "W1", "level": "1F", "kind": "exterior", "start": [0, 0], "end": [8000, 0], "thickness_mm": 162,
              "bottom_mm": 480, "height": "to_roof", "top_glb_mm": 3624},
             {"id": "W2", "level": "2F", "kind": "exterior", "start": [0, 0], "end": [5000, 0], "thickness_mm": 162,
              "bottom_mm": 3624, "height": "to_roof", "top_glb_mm": 6600}]
    ops = [{"id": "O1", "level": "1F", "wall": "W1", "kind": "window", "offset_mm": 500, "width_mm": 800,
            "sill_mm": 1524, "head_mm": 3024, "head_fl_mm": 2400},
           {"id": "O2", "level": "2F", "wall": "W2", "kind": "window", "offset_mm": 500, "width_mm": 800,
            "sill_mm": 4524, "head_mm": 5424, "head_fl_mm": 1800}]
    keep = set(levels)
    return {"levels": lv, "walls": [w for w in walls if w["level"] in keep], "openings": [o for o in ops if o["level"] in keep],
            "foundation": {"outline": [[0, -6000], [8000, -6000], [8000, 0], [0, 0]]}}


def test_views_and_labels():
    single, multi = _house(("1F",)), _house()
    assert views_of(single) == ("S", "N", "E", "W", "plan") and views_of(multi)[-1] == "plan2"
    assert view_label(single, "plan") == "平面（屋根を外して上から）"
    assert view_label(multi, "plan").startswith("1階の平面") and view_label(multi, "plan2").startswith("2階の平面")


def test_plan_elements_are_per_level():
    b = _house()
    canvas = Canvas(np.array([-500.0, -6500.0]), np.array([8500.0, 500.0]))
    ids = lambda es: sorted(e["id"] for e in es)
    assert ids(elements(b, "plan", canvas, 7000.0, "1F")) == ["O1", "W1"]
    assert ids(elements(b, "plan2", canvas, 7000.0, "2F")) == ["O2", "W2"]
    assert ids(elements(b, "plan", canvas, 7000.0)) == ["O1", "O2", "W1", "W2"]      # 階を渡さなければ全部（平屋の道）


def test_elevation_label_height_uses_wall_top_on_two_storeys():
    """2階建ての立面：1階の壁の札が2階の高さに出ないように、壁の上端は glb の上端を使う。"""
    b = _house()
    canvas = Canvas(np.array([-8500.0, 0.0]), np.array([500.0, 7000.0]))
    w1 = next(e for e in elements(b, "N", canvas, 7000.0) if e["id"] == "W1")       # W1 は家の北の壁
    y_top = canvas.px(np.array([[0.0, 3624.0]]))[0, 1]
    assert abs(w1["bbox_px"][1] - y_top) <= 1
