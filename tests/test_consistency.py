"""glb にありえない形を見つける（core/consistency.py）。直さない・要確認として出すだけ。"""
from __future__ import annotations

import json

import pytest

from core.consistency import find_all


def _wall(i, kind, s, e, t=162.0, height="to_roof", bottom=480.0, level="1F"):
    return {"id": f"W{i}", "kind": kind, "start": list(s), "end": list(e), "thickness_mm": t, "height": height,
            "bottom_mm": bottom, "level": level}


def _op(i, wall, kind, off, width, sill=1424.0, head=3024.0):
    return {"id": f"O{i}", "wall": wall, "kind": kind, "offset_mm": off, "width_mm": width, "sill_mm": sill, "head_mm": head,
            "center": [off + width / 2, 0.0], "level": "1F"}


def _house(walls, openings=()):
    return {"levels": [{"name": "1F", "fl_mm": 624}], "walls": walls, "openings": list(openings)}


def test_two_walls_in_the_same_place():
    b = _house([_wall(1, "exterior", (0, 0), (9000, 0)), _wall(2, "standalone", (8000, 10), (9750, 10), t=180)])
    (f,) = find_all(b)
    assert f["kind"] == "walls_overlap" and f["targets"] == ["W1", "W2"] and "約 1000mm" in f["message"]


def test_corner_joints_and_parallel_neighbours_are_not_overlaps():
    b = _house([_wall(1, "exterior", (0, 0), (9000, 0)), _wall(2, "exterior", (9000, -81), (9000, 6000)),   # 角
                _wall(3, "exterior", (8919, 0), (12000, 0)),                                                  # 続きの壁（重なり 81mm）
                _wall(4, "interior", (0, 1000), (9000, 1000), t=130)])                                         # 平行で離れた壁
    assert [f["kind"] for f in find_all(b)] == []


def test_window_running_into_the_next_wall_is_found_but_pocket_door_gap_is_not():
    walls = [_wall(1, "exterior", (0, 0), (4000, 0)), _wall(2, "exterior", (4000, 0), (8000, 0))]
    window = _op(1, "W1", "window", 3500, 530)                     # W2 に 30mm はみ出す
    pocket = _op(2, "W1", "door", -800, 1600, sill=624.0)          # 壁のない隙間へ出る引き込み戸（物件B の O17）
    fs = find_all(_house(walls, [window, pocket]))
    assert [(f["kind"], f["targets"]) for f in fs] == [("opening_protrudes", ["O1", "W1", "W2"])]


def test_openings_overlapping_and_door_above_the_ceiling():
    walls = [_wall(1, "interior", (0, 0), (6000, 0), t=130, height=3024.0, bottom=624.0)]
    ops = [_op(1, "W1", "door", 1000, 800, sill=624.0, head=3126.0),      # 物件E の O10：壁の上端より 102mm 高い
           _op(2, "W1", "door", 3000, 800, sill=624.0, head=3024.0),
           _op(3, "W1", "door", 3500, 800, sill=624.0, head=3024.0),      # O2 と 300mm 重なる
           _op(4, "W1", "door", 5000, 790, sill=624.0, head=3054.0)]      # 30mm 高いだけ（天井までのドア）は拾わない
    kinds = sorted((f["kind"], tuple(f["targets"])) for f in find_all(_house(walls, ops)))
    assert kinds == [("opening_above_top", ("O1", "W1")), ("openings_overlap", ("O2", "O3"))]


def test_very_short_wall():
    b = _house([_wall(1, "interior", (0, 0), (60, 0), t=130)])
    assert [f["kind"] for f in find_all(b)] == ["short_wall"]


@pytest.mark.realdata
@pytest.mark.parametrize("alias,expected", [("物件A", [["W8", "W13"]]), ("物件B", [["W13", "W14"]])])
def test_real_known_overlaps_only(alias, expected):
    """物件A・B（11/3 に下調べ）：見つかるのは知っている壁の重なりだけ（引き込み戸 O17 などは拾わない）。"""
    from core import overrides as ov
    from core.paths import out_dir
    p = out_dir(alias) / "building.json"
    if not p.exists():
        pytest.skip("組み立て前")
    b = ov.apply(json.loads(p.read_text(encoding="utf-8")), ov.load(alias))[0]
    assert [f["targets"] for f in find_all(b)] == expected
