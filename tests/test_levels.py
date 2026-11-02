"""高さの決め方（adapters/walkinhome.py の detect_ground / detect_levels）を、架空の部材で確かめる（実データ不要）。"""
from __future__ import annotations

import numpy as np

from adapters.glb_reader import GlbModel, Part, Primitive
from adapters.walkinhome import compare_with_rules, detect_ground, detect_levels


def _flat(code, z, x0=0, size=1000):
    v = np.array([[x0, 0, z], [x0 + size, 0, z], [x0, size, z]], dtype=float)
    return Primitive(material=f"Material(Bui{code})", code=code, vertices=v, triangles=np.array([[0, 1, 2]]))


def _wall(code, z0, z1):
    v = np.array([[0, 0, z0], [1000, 0, z0], [0, 0, z1]], dtype=float)
    return Primitive(material=f"Material(Bui{code})", code=code, vertices=v, triangles=np.array([[0, 1, 2]]))


def _two_story_model():
    return GlbModel(generator="WIH", version_info="t", parts=[
        Part(node=5, name="typeESiki", type_code="typeESiki", primitives=[_flat("SIKI_SIKI", 0)]),
        Part(node=6, name="typeEKiso", type_code="typeEKiso",
             primitives=[_wall("KISO_HONT", 0, 480), _flat("KISO_ILOW", 480), _wall("KISO_DMIZ", 480, 510)]),
        Part(node=10, name="typeEHeya", type_code="typeEHeya",
             primitives=[_flat("HEYA_YUKA", 624), _flat("HEYA_TENJ", 3024)]),
        Part(node=11, name="typeEHeya", type_code="typeEHeya",
             primitives=[_flat("HEYA_YUKA", 624, x0=2000), _flat("HEYA_TENJ", 3024, x0=2000)]),
        Part(node=12, name="typeEHeya", type_code="typeEHeya",
             primitives=[_flat("HEYA_YUKA", 3474), _flat("HEYA_TENJ", 5874)]),
    ])


def test_ground_uses_foundation_wall_top_not_flashing():
    g = detect_ground(_two_story_model())
    assert g["gl_mm"] == 0 and g["foundation_top_mm"] == 480  # 水切りの 510 ではない
    assert g["foundation_source"] == "glb:typeEKiso#6"


def test_two_levels_are_split():
    levels = detect_levels(_two_story_model())
    assert [(lv["name"], lv["fl_mm"], lv["ceiling_mm"]) for lv in levels] == [("1F", 624, 2400), ("2F", 3474, 2400)]
    assert levels[0]["source"] == "glb:typeEHeya#10"
    assert levels[1]["source"] == "glb:typeEHeya#12"


def test_rule_mismatch_is_reported():
    m = _two_story_model()
    rules = {"levels": {"gl_to_foundation_top_mm": 500, "gl_to_1fl_mm": 624, "ceiling_height_mm": 2400}}
    checks = compare_with_rules(detect_ground(m), detect_levels(m), rules)
    assert [c["match"] for c in checks] == [False, True, True]
