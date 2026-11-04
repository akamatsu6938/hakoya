"""小上がり（床が FL より高い部屋）と畳（typeETata）。★11/4 物件C・D：小上がりの和室が部屋ごと落ち、畳の所が消えていた。
架空の部材と架空の家で確かめる（実データ不要）。実物は tests/test_walkinhome_real_cd.py。"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon

from adapters.glb_reader import GlbModel, Part, Primitive
from adapters import walkinhome as wh
from core.builder import build_model, prism, raised_floors
from core.rules import load_rules

FL = 624.0


def _rect(code, z, x0, y0, x1, y1):
    """水平な長方形（三角形2つ）。"""
    v = np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], dtype=float)
    return Primitive(material=f"Material(Bui{code})", code=code, vertices=v, triangles=np.array([[0, 1, 2], [0, 2, 3]]))


def _wall(code, x0, y0, x1, y1, z0, z1):
    v = np.array([[x0, y0, z0], [x1, y1, z0], [x1, y1, z1], [x0, y0, z1]], dtype=float)
    return Primitive(material=f"Material(Bui{code})", code=code, vertices=v, triangles=np.array([[0, 1, 2], [0, 2, 3]]))


def _raised_room():
    """2000×2000 の小上がり（FL+300）。部屋の床の面は縁の 100mm だけで、真ん中は畳の部材。巾木は FL+300〜310。"""
    z = FL + 300
    room = Part(node=69, name="typeEHeya", type_code="typeEHeya", primitives=[
        _rect("HEYA_YUKA", z, 0, 0, 2000, 100), _rect("HEYA_HABA", z + 10, 0, 0, 2000, 10),
        _wall("HEYA_KABE", 0, 2000, 2000, 2000, z, FL + 2400), _wall("HEYA_KABE", 0, 0, 0, 2000, z, FL + 2400)])
    tatami = Part(node=10, name="typeETata", type_code="typeETata", primitives=[_rect("TATA_HONT", z, 0, 100, 2000, 2000)])
    return room, tatami


def test_raised_room_is_kept_with_its_tatami():
    room, tatami = _raised_room()
    assert wh._room_floor(room, FL).area > 0                       # 前は「床は FL+100 まで」で空になり、部屋ごと落ちていた
    model = GlbModel(generator="t", version_info="t", parts=[room, tatami])
    assert wh._min_floor_z(model, room.source, FL) == FL + 300
    floors = {room.source: wh._room_floor(room, FL)}
    wh._add_tatami(floors, [room], model, FL)
    assert abs(floors[room.source].area - 2000 * 2000) < 1e3       # 縁の床＋畳 ＝ 部屋全体


def test_tatami_at_other_height_or_elsewhere_is_not_added():
    room, _ = _raised_room()
    far = Part(node=11, name="typeETata", type_code="typeETata", primitives=[_rect("TATA_HONT", FL + 300, 5000, 0, 7000, 2000)])
    low = Part(node=12, name="typeETata", type_code="typeETata", primitives=[_rect("TATA_HONT", FL + 150, 0, 100, 2000, 2000)])
    model = GlbModel(generator="t", version_info="t", parts=[room, far, low])
    floors = {room.source: wh._room_floor(room, FL)}
    before = floors[room.source].area
    wh._add_tatami(floors, [room], model, FL)
    assert floors[room.source].area == before


def test_room_on_the_floor_is_unchanged():
    room = Part(node=70, name="typeEHeya", type_code="typeEHeya", primitives=[_rect("HEYA_YUKA", FL, 0, 0, 1000, 1000)])
    model = GlbModel(generator="t", version_info="t", parts=[room])
    assert wh._min_floor_z(model, room.source, FL) == FL
    assert abs(wh._room_floor(room, FL).area - 1e6) < 10


def test_too_high_floor_is_not_a_raised_room():
    """FL+600 より高い床（ロフトなど）は小上がりにしない。"""
    room = Part(node=71, name="typeEHeya", type_code="typeEHeya", primitives=[_rect("HEYA_YUKA", FL + 1400, 0, 0, 1000, 1000)])
    assert wh._room_floor(room, FL).is_empty


def test_platform_from_fl_to_the_raised_floor():
    rooms = [{"id": "R1", "level": "1F", "floor_mm": FL + 300, "polygon": [[1000, 1000], [2500, 1000], [2500, 2500], [1000, 2500]]},
             {"id": "R2", "level": "1F", "floor_mm": FL, "polygon": [[3200, 200], [5800, 200], [5800, 3800], [3200, 3800]]}]
    solids, labels = raised_floors(rooms, FL, Polygon())
    assert labels == ["R1（FL+300）"] and len(solids) == 1
    x0, y0, z0, x1, y1, z1 = solids[0].bounding_box()
    assert (round(z0), round(z1)) == (FL - 1, FL + 300) and (round(x1 - x0), round(y1 - y0)) == (1500, 1500)


def test_build_model_adds_the_platform_to_the_first_floor():
    from tests.test_builder import _tiny_house
    b = _tiny_house()
    b["rooms"] = [{"id": "R1", "level": "1F", "floor_mm": FL + 300, "polygon": [[500, 500], [2500, 500], [2500, 2500], [500, 2500]]}]
    parts = build_model(b, load_rules("jisha"))
    probe = parts["floor1"].solid ^ prism(Polygon([(1400, 1400), (1600, 1400), (1600, 1600), (1400, 1600)]), 0, 9000)
    assert round(probe.bounding_box()[5]) == FL + 300
    assert any("小上がり" in n and "R1" in n for n in parts["floor1"].notes)


def test_plan_shows_the_raised_room():
    from tests.test_builder import _tiny_house
    from tools.plan_preview import render_svg
    b = _tiny_house()
    b["openings"] = []                        # この試験の家の窓には、平面図の札に要る「床からの上端」がない
    b["roof"]["shape"] = "unknown"            # 屋根の欄（勾配・軒の出）は描かない（この試験の家にはその値がない）
    b["rooms"] = [{"id": "R1", "level": "1F", "floor_mm": FL + 300, "polygon": [[500, 500], [2500, 500], [2500, 2500], [500, 2500]]}]
    assert "小上がり FL+300" in render_svg(b, "試験の家")
