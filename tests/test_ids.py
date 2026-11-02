"""番号の引き継ぎ（core/ids.py）を、架空の building.json で確かめる（実データ不要）。"""
from __future__ import annotations

import copy

from core.ids import stabilize


def _wall(i, kind, s, e, bottom=624):
    return {"id": i, "level": "1F", "kind": kind, "start": s, "end": e, "thickness_mm": 130, "bottom_mm": bottom}


def _prev():
    return {"walls": [_wall("W1", "exterior", [0, 0], [6000, 0], 480), _wall("W2", "interior", [3000, 0], [3000, 4000]),
                      _wall("W3", "interior", [0, 2000], [3000, 2000])],
            "openings": [{"id": "O1", "wall": "W2", "kind": "door", "center": [3000, 1000], "source": "glb:typeETate#5"}],
            "rooms": [{"id": "R1", "source": "glb:typeEHeya#13"}],
            "assumptions": [{"field": "walls[W2]", "note": "x"}]}


def test_ids_survive_reordering_and_small_moves():
    new = {"walls": [_wall("W1", "interior", [0, 2010], [3000, 2010]),        # 前回の W3（10mm ずれた）
                     _wall("W2", "interior", [3005, 0], [3005, 3990]),        # 前回の W2
                     _wall("W3", "exterior", [0, 0], [6000, 0], 480),          # 前回の W1
                     _wall("W4", "interior", [4500, 0], [4500, 4000])],       # 新しい壁
           "openings": [{"id": "O1", "wall": "W2", "kind": "door", "center": [3005, 1000], "source": "glb:typeETate#5"}],
           "rooms": [{"id": "R1", "source": "glb:typeEHeya#13"}],
           "assumptions": [{"field": "walls[W2]", "note": "x"}, {"field": "walls[W1]", "note": "y"}]}
    out = stabilize(copy.deepcopy(new), _prev())
    ids = {(tuple(w["start"]), w["kind"]): w["id"] for w in out["walls"]}
    assert ids[((0, 2010), "interior")] == "W3"
    assert ids[((3005, 0), "interior")] == "W2"
    assert ids[((0, 0), "exterior")] == "W1"
    assert ids[((4500, 0), "interior")] == "W4"          # 新しい壁は続きの番号
    assert out["openings"][0]["wall"] == "W2"            # 開口の「どの壁か」も付け替わる
    assert {a["field"] for a in out["assumptions"]} == {"walls[W2]", "walls[W3]"}
    assert out["id_changes"]["new"] == ["W4"] and out["id_changes"]["removed"] == []


def test_removed_wall_is_reported():
    prev = _prev()
    new = {"walls": [_wall("W1", "exterior", [0, 0], [6000, 0], 480), _wall("W2", "interior", [3000, 0], [3000, 4000])],
           "openings": [], "rooms": [], "assumptions": []}
    out = stabilize(new, prev)
    assert "W3" in out["id_changes"]["removed"] and "O1" in out["id_changes"]["removed"]


def test_hanging_wall_is_not_matched_to_full_wall():
    prev = {"walls": [_wall("W1", "interior", [0, 0], [3000, 0])], "openings": [], "rooms": []}
    new = {"walls": [_wall("W1", "interior", [0, 0], [3000, 0], bottom=2824)], "openings": [], "rooms": [], "assumptions": []}
    out = stabilize(new, prev)
    assert out["walls"][0]["id"] == "W2" and out["id_changes"]["removed"] == ["W1"]


def test_first_time_keeps_ids():
    new = {"walls": [_wall("W1", "interior", [0, 0], [1, 0])], "openings": [], "rooms": []}
    assert stabilize(new, None)["walls"][0]["id"] == "W1"
