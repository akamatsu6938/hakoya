"""まとめて検査する道具（tools/batch_check.py）の、データに頼らない部分。"""
from __future__ import annotations

import copy

from tools.batch_check import _safe_error, plan_similarity, report_markdown


def _house(dx=0.0):
    walls = [{"id": f"W{i}", "kind": k, "start": [x0 + dx, y0], "end": [x1 + dx, y1]}
             for i, (k, x0, y0, x1, y1) in enumerate([("exterior", 0, 0, 9000, 0), ("exterior", 9000, 0, 9000, 7000),
                                                      ("exterior", 9000, 7000, 0, 7000), ("exterior", 0, 7000, 0, 0),
                                                      ("interior", 4000, 0, 4000, 7000)], 1)]
    ops = [{"id": "O1", "kind": "window", "center": [2000 + dx, 0], "width_mm": 1690}]
    return {"levels": [{"name": "1F"}], "walls": walls, "openings": ops}


def test_same_plan_even_if_the_origin_moves():
    s = plan_similarity(_house(), _house(dx=-3456.0))
    assert s["same"] and s["walls"] == [5, 5, 5] and s["openings"] == [1, 1, 1]


def test_moved_wall_or_window_is_not_the_same_plan():
    b = _house()
    moved = copy.deepcopy(b)
    moved["walls"][4]["start"][0] = moved["walls"][4]["end"][0] = 4500
    assert not plan_similarity(b, moved)["same"]
    wider = copy.deepcopy(b)
    wider["openings"][0]["width_mm"] = 2560
    assert not plan_similarity(b, wider)["same"]


def test_error_text_with_a_path_is_not_shown():
    try:
        raise FileNotFoundError("C:\\物件\\山田様邸.glb が開けない")
    except FileNotFoundError as e:
        text = _safe_error(e)
    assert text.startswith("FileNotFoundError") and "山田" not in text and "tests/test_batch_check.py" in text


def test_own_message_is_shown():
    try:
        raise ValueError("3階建てはまだ扱えません（2階建てまで）。")
    except ValueError as e:
        assert "3階建て" in _safe_error(e)


def test_report_lists_stopped_and_passed_properties():
    ok = {"alias": "物件A", "ok": True, "glb": True, "drawings": ["plan"], "notes": [], "seconds": {"読み取り": 1.0},
          "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}], "outline_mm": [10190, 11100],
          "walls": {"外壁": 10}, "openings": {"窓": 11},
          "roofs": [{"id": "Y1", "level": "1F", "shape": "寄棟", "planes": 4, "pitch": ["4/10"], "eave_mm": {}}],
          "glb_exceptions": {}, "unresolved": 0, "skipped": 0, "parts": 3, "watertight": True, "in_build_volume": False,
          "checks_high_mid": [], "bambu_3mf": False, "code_findings": {}, "code_messages": []}
    stopped = {"alias": "物件E", "ok": False, "glb": False, "drawings": [], "notes": ["glb のフォルダに、glb がまだありません"],
               "seconds": {}, "stopped": "glb がない"}
    md = report_markdown([ok, stopped], [], [], "まとめて検査")
    assert "| 物件A | 通った | 平屋 | 寄棟（面4、4/10）" in md and "**超える**" in md
    assert "止まった（glb がない）" in md and "203.8×222.0mm" in md


def test_report_shows_human_fixes_that_no_longer_apply_and_stopped_elevations():
    """★11/4 の見直し：作り直しで人の修正が当たらなくなった・番号が変わった・立面図の照合が止まったことを表に出す。"""
    from tools.batch_check import describe
    b = {"levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}], "walls": [], "openings": [],
         "roof": {"shape": "shed", "pitch": [1.5, 10]}, "id_changes": {"new": ["W30"], "removed": ["W12", "O4"]}}
    summary = {"overrides": [{"id": "OV1", "status": "applied"}, {"id": "OV2", "status": "failed"}], "checks": [], "parts": {}}
    drawing = {"available": True, "levels": [], "elevations_note": "立面図を照合できませんでした（ValueError）"}
    d = describe(b, summary, [], drawing)
    assert d["overrides"] == {"applied": 1, "failed": 1} and d["id_changes"] == {"new": 1, "removed": 2}
    assert "立面図を照合できませんでした（ValueError）" in d["drawing"]
    row = {"alias": "物件A", "ok": True, "glb": True, "drawings": ["plan"], "notes": [], "seconds": {}, "outline_mm": None, **d}
    md = report_markdown([row], [], [], "まとめて検査")
    assert "**当てられない 1**" in md and "なくなった部材 2" in md
