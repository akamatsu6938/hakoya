"""図面（jww）の読み取り・位置合わせ・注記の照合（core/jww.py・core/drawing_check.py）を確かめる。

前半は架空の図面（ezjww の読み込みを差し替える）。後半は物件A・B の本物の図面（data/ がなければ飛ばす）。
★表題欄（用紙の下の帯）と縮尺 1 の層の文字は読み捨てる。出してはいけない言葉（ng_words）を含む文字も捨てる。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from core import jww
from core.drawing_check import NOTE_RE, match_notes, window_notes


def _base(group=0, layer=0, pen_style=1, pen_color=1):
    return {"layer_group": group, "layer": layer, "pen_style": pen_style, "pen_color": pen_color}


def _line(x0, y0, x1, y1, **kw):
    return {"type": "LINE", "base": _base(**kw), "start_x": x0, "start_y": y0, "end_x": x1, "end_y": y1}


def _text(x, y, content, **kw):
    return {"type": "TEXT", "base": _base(**kw), "start_x": x, "start_y": y, "end_x": x + 10, "end_y": y, "size_y": 2.5,
            "content": content}


class _FakeDoc:
    def __init__(self, entities, groups):
        self.jww_document = {"header": {"paper_size": 3, "layer_groups": groups, "memo": "読まないこと"}, "entities": entities}


def _groups():
    layers = [{"state": 2} for _ in range(16)]
    g = [{"state": 2, "scale": 50.0, "layers": [dict(x) for x in layers]} for _ in range(16)]
    g[15]["scale"] = 1.0                                         # 枠・表題欄の層
    g[3]["state"] = 0                                            # 非表示のグループ
    g[0]["layers"][5]["state"] = 0                               # 非表示のレイヤ
    return g


@pytest.fixture
def fake(monkeypatch):
    import ezjww
    ents = [
        _line(-200, -127.5, 200, -127.5, group=15),              # 表題欄の上の線（用紙の幅の 8割より長い）
        _line(-50, 0, 50, 0), _line(-50, 0, -50, 40),             # 本体の線
        _line(0, -130, 10, -130),                                 # 表題欄の帯の中の線 → 捨てる
        _line(0, 10, 10, 10, pen_style=9),                        # 補助線 → 捨てる
        _line(0, 20, 10, 20, layer=5),                            # 非表示のレイヤ → 捨てる
        _line(0, 30, 10, 30, group=3),                            # 非表示のグループ → 捨てる
        _text(-30, 20, "〇07405(FL+2400)"),                       # 窓の注記
        _text(-30, -135, "施主の名前（架空）"),                     # 表題欄の帯 → 捨てる
        _text(-30, 60, "設計事務所（架空）", group=15),            # 縮尺 1 の層 → 捨てる
        _text(-30, 50, "山田邸（架空の言葉）"),                     # ng_words → 捨てる
        _text(-30, 500, "用紙の外"),                              # 用紙の外 → 捨てる
        {"type": "DIMENSION", "base": _base(), "line": {"start_x": -50, "start_y": 70, "end_x": 50, "end_y": 70},
         "text": {"content": "5,000"}},
    ]
    monkeypatch.setattr(ezjww, "readfile", lambda p: _FakeDoc(ents, _groups()))
    return jww.read_drawing(Path("架空.jww"), "plan", ng_words=["山田邸"])


def test_title_band_and_private_texts_are_dropped(fake):
    assert fake.title_band_top == pytest.approx(-127.0)
    assert [t["text"] for t in fake.texts] == ["〇07405(FL+2400)"]          # 本体の注記だけ残る
    assert fake.dropped["title_band"] == 2 and fake.dropped["paper_scale_group"] == 1
    assert fake.dropped["ng_words"] == 1 and fake.dropped["off_paper"] == 1
    assert fake.dropped["aux"] == 1 and fake.dropped["hidden"] == 2
    assert len(fake.lines) == 2 and fake.scale == 50.0
    assert fake.lines[0].tolist() == [-2500.0, 0.0, 2500.0, 0.0]               # 実寸＝用紙 mm × 縮尺
    assert [(d["value"], d["text"]) for d in fake.dims] == [(5000.0, "5,000")]


def test_title_band_falls_back_when_no_long_line():
    band = jww._title_band_top([_line(0, 0, 10, 0)], (420.0, 297.0))
    assert band == pytest.approx(-148.5 + jww.TITLE_FALLBACK_MM + 0.5)


# ---------------------------------------------------------------- 位置合わせ

def _box_house():
    t = 160
    pts = [(0, 0), (9000, 0), (9000, 6000), (4000, 6000), (4000, 9000), (0, 9000)]   # L字（向きが1つに決まる）
    walls = []
    for i, (p, q) in enumerate(zip(pts, pts[1:] + pts[:1]), 1):
        p, q = np.array(p, float), np.array(q, float)
        d = (q - p) / np.linalg.norm(q - p)
        n = np.array([-d[1], d[0]]) * t / 2
        walls.append({"id": f"W{i}", "level": "1F", "kind": "exterior", "start": list(p + n), "end": list(q + n), "thickness_mm": t})
    walls.append({"id": "W9", "level": "1F", "kind": "interior", "start": [4000, 80], "end": [4000, 6000], "thickness_mm": 130})
    return {"levels": [{"name": "1F", "fl_mm": 624}], "walls": walls,
            "openings": [{"id": "O1", "wall": "W1", "kind": "window", "level": "1F", "center": [2000, 80], "head_fl_mm": 2000},
                         {"id": "O2", "wall": "W1", "kind": "window", "level": "1F", "center": [6500, 80], "head_fl_mm": 2400}]}


def _drawing_of(b, rot, tx, ty):
    """模型の壁の面を、逆の変換で図面の座標にした架空の図面（＋寸法線などの余計な線）。"""
    faces = jww._wall_faces(b, "1F")
    inv = {0: 0, 90: 270, 180: 180, 270: 90}[rot]
    a = jww._rot(inv, faces[:, :2] - [tx, ty])
    c = jww._rot(inv, faces[:, 2:] - [tx, ty])
    extra = np.array([[-3000, -2000, 15000, -2000], [-3000, 12000, 15000, 12000]], float)   # 寸法線など
    return jww.Drawing(kind="plan", paper_mm=(420, 297), title_band_top=-127, main_group=0, scale=50.0,
                       lines=np.vstack([np.hstack([a, c]), extra]))


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_align_finds_rotation_and_offset(rot):
    b = _box_house()
    d = _drawing_of(b, rot, 1234.0, -567.0)
    al = jww.align_plan(d, b, "1F")
    assert al is not None and al["rot"] == rot
    assert (al["tx"], al["ty"]) == pytest.approx((1234.0, -567.0), abs=15.0)
    assert jww.to_model(al, *jww._rot({0: 0, 90: 270, 180: 180, 270: 90}[rot], np.array([[100.0 - 1234, 200.0 + 567]]))[0]) == \
        pytest.approx((100.0, 200.0), abs=15.0)


def test_align_refuses_unrelated_drawing():
    b = _box_house()
    rng = np.random.default_rng(1)
    junk = rng.uniform(-20000, 20000, size=(300, 4))
    d = jww.Drawing(kind="plan", paper_mm=(420, 297), title_band_top=-127, main_group=0, scale=50.0, lines=junk)
    assert jww.align_plan(d, b, "1F") is None                              # 推測で合わせない


# ---------------------------------------------------------------- 注記

def test_note_pattern():
    ok = ["〇07405(FL+2400)", "●16503(FL+2000)", "〇06011(L)(FL+2200)", "〇〇07624(FL+2400)", "〇07603（FL＋2000）"]
    assert [int(NOTE_RE.search(s).group(2)) for s in ok] == [2400, 2000, 2200, 2400, 2000]
    for s in ["枕棚+ﾊﾟｲﾌﾟ FL+1800", "特注(W800 H390)", "TV下地 FL+500～1400"]:
        assert NOTE_RE.search(s) is None                                    # 窓でない FL+ は拾わない


def test_notes_match_one_to_one_and_far_note_stays_unmatched():
    b = _box_house()
    al = {"rot": 0, "tx": 0.0, "ty": 0.0}
    notes = [{"note_id": "T1", "code": "07405", "head_fl_mm": 2000, "x": 2000, "y": -700},
             {"note_id": "T2", "code": "16503", "head_fl_mm": 2200, "x": 6400, "y": -700},
             {"note_id": "T3", "code": "07407", "head_fl_mm": 2400, "x": 2600, "y": -900},     # O1 の近くに2つ目（O1 は使用済み）
             {"note_id": "T4", "code": "07407", "head_fl_mm": 2400, "x": 20000, "y": 20000}]   # 遠い
    matched, unmatched = match_notes(notes, b, al, "1F")
    assert sorted((m["note_id"], m["opening"]) for m in matched) == [("T1", "O1"), ("T2", "O2")]
    assert sorted(n["note_id"] for n in unmatched) == ["T3", "T4"]


def test_stacked_windows_get_the_note_with_the_closer_head():
    """★物件F の O4・O5（11/3）：上下に並ぶ窓（地窓と高窓）は平面の位置がほぼ同じなので、上端の近い注記に結ぶ。"""
    b = _box_house()
    b["openings"] = [{"id": "O4", "wall": "W1", "kind": "window", "level": "1F", "center": [2000, 80], "head_fl_mm": 2780},
                     {"id": "O5", "wall": "W1", "kind": "window", "level": "1F", "center": [2010, 80], "head_fl_mm": 1090},
                     {"id": "O6", "wall": "W1", "kind": "window", "level": "1F", "center": [6500, 80], "head_fl_mm": 2400}]
    notes = [{"note_id": "T1", "code": "06711", "head_fl_mm": 1098, "x": 2000, "y": -600},    # 下の窓の注記が上の窓に近い
             {"note_id": "T2", "code": "06715", "head_fl_mm": 2778, "x": 2010, "y": -700},
             {"note_id": "T3", "code": "16503", "head_fl_mm": 2000, "x": 6500, "y": -600}]   # 離れた窓は上端が違っても入れ替えない
    matched, _ = match_notes(notes, b, {"rot": 0, "tx": 0.0, "ty": 0.0}, "1F")
    got = {m["opening"]: (m["note_id"], m["head_fl_mm"]) for m in matched}
    assert got == {"O4": ("T2", 2778), "O5": ("T1", 1098), "O6": ("T3", 2000)}


# ---------------------------------------------------------------- 本物の図面

def _applied(alias):
    from core import overrides as ov
    from core.paths import out_dir
    b = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    return ov.apply(b, ov.load(alias))[0]


@pytest.mark.realdata
def test_real_a_plan_notes_all_match():
    from core.drawing_check import summary
    s = summary("物件A", _applied("物件A"))
    row = s["levels"][0]
    assert row["aligned"] and row["score"] > 0.5
    assert (row["notes"], row["matched"], row["same"], row["mismatches"]) == (11, 11, 11, [])
    assert {p["opening"] for p in row["pairs"]} == {f"O{i}" for i in range(1, 12)}       # 窓 11 か所すべて


@pytest.mark.realdata
def test_real_texts_have_no_private_words():
    from core.privacy import ng_words
    words = ng_words()
    for alias, kinds in (("物件A", ("plan", "elevation", "section")), ("物件B", ("plan", "plan_2f", "elevation"))):
        for kind in kinds:
            d = jww.read_property(alias, kind)
            assert d.dropped["paper_scale_group"] + d.dropped["title_band"] > 0       # 表題欄は捨てている
            for t in d.texts:
                assert not any(w in t["text"] for w in words)
                assert t["y"] / d.scale >= d.title_band_top


@pytest.mark.realdata
def test_real_b_upper_floor_aligns_with_hint():
    from core.drawing_check import summary
    s = summary("物件B", _applied("物件B"))
    rows = {r["kind"]: r for r in s["levels"]}
    assert rows["plan"]["aligned"] and rows["plan_2f"]["aligned"]
    assert rows["plan_2f"]["matched"] == rows["plan_2f"]["notes"] == 4 and not rows["plan_2f"]["mismatches"]
    assert rows["plan"]["same"] == rows["plan"]["matched"] >= 10


@pytest.mark.realdata
def test_real_a_drawing_region_and_jww_value_source():
    """検品の道具：平面の図面の画像（模型の図と同じ大きさ）と寸法・注記の番号。注記の番号で直す案の数値をコードが読む。"""
    from PIL import Image
    from core.ai.tools import ToolContext, call_tool
    ctx = ToolContext.load("物件A")
    r = call_tool(ctx, "get_drawing_region", {"view": "plan"})
    assert r["available"] and len(r["dims"]) > 20 and len(r["notes"]) == 11
    model_png = ctx.inspect_dir / "plan_model.png"
    assert Image.open(r["image_path"]).size == Image.open(model_png).size          # 模型の図と同じ大きさ・縮尺
    note = next(n for n in r["notes"] if n["head_fl_mm"] == 2000)
    p = call_tool(ctx, "propose_override", {"target": "O6", "field": "head_fl_mm", "value_source": f"jww:plan:{note['id']}",
                                            "reason": "平面図の注記に合わせる（試験）"})
    assert "error" in p and "すでに" in p["error"]                                    # O6 はもう FL+2000（同じ値なので案にならない）
    q = call_tool(ctx, "propose_override", {"target": "O6", "field": "sill_fl_mm", "value_source": "jww:plan:T99999", "reason": "x"})
    assert "見つかりません" in q["error"]
    el = call_tool(ctx, "get_drawing_region", {"view": "S"})                       # 立面：図面1枚の画像（位置は合わせない）
    assert el["available"] and el["kind"] == "elevation"
    sec = call_tool(ctx, "get_drawing_region", {"view": "plan", "drawing": "section"})
    assert sec["available"] and len(sec["dims"]) > 10
    d = next(x for x in sec["dims"] if x["value_mm"] == 624)                        # 断面図の 1FL の寸法
    from core.drawing_check import item_value
    assert item_value("物件A", ctx.building, "section", d["id"]) == 624.0


# ---------------------------------------------------------------- 立面図（1枚に4面）

def test_elevation_groups_split_views_and_keep_isolated_window():
    """地面の長い線でつながった2つの面を分け、壁の真ん中に離れてある窓（線が少ない）は、その面のまとまりに入れる。"""
    from core.drawing_check import _elevation_groups

    def rect(x0, y0, x1, y1):
        return [[x0, y0, x1, y0], [x1, y0, x1, y1], [x1, y1, x0, y1], [x0, y1, x0, y0]]
    lines = [[-20000, 0, 20000, 0]]                                         # 2つの面をつなぐ地面の線（長い）
    for k in range(6):                                                      # 面1：外形と縦の線（線を 20 本以上に）
        lines += rect(-15000 + k * 10, 0, -5000 - k * 10, 6000 - k * 10)
    for k in range(6):                                                      # 面2
        lines += rect(5000 + k * 10, 0, 15000 - k * 10, 6000 - k * 10)
    lines += rect(-11000, 3000, -9000, 3400)                                # 面1 の真ん中の窓（ほかの線から 1m 以上離れている）
    d = jww.Drawing(kind="elevation", paper_mm=(420, 297), title_band_top=-127, main_group=0, scale=100.0,
                    lines=np.array(lines, float))
    groups = _elevation_groups(d)
    assert len(groups) == 2
    sizes = sorted(len(g) for g in groups)
    assert sizes == [24, 28]                                                # 窓の4本は面1に入る（地面の線は使わない）


@pytest.mark.realdata
@pytest.mark.parametrize("alias", ["物件A", "物件B"])
def test_real_elevations_align_each_face(alias):
    from PIL import Image
    from core.ai.tools import ToolContext, call_tool
    ctx = ToolContext.load(alias)
    for v in ("S", "N", "E", "W"):
        r = call_tool(ctx, "get_drawing_region", {"view": v})
        assert r["available"] and r.get("aligned") and r["align_score"] >= 0.4, (alias, v, r.get("note"))
        assert Image.open(r["image_path"]).size == Image.open(ctx.inspect_dir / f"{v}_model.png").size


@pytest.mark.realdata
def test_real_drawing_checks_catch_seeded_errors_without_ai():
    """図面との照合（平面図の注記の上端・幅・高さ、立面図の窓、結べない注記）で、試験と同じ間違いの多くを
    コードだけ（AI なし）で見つける。間違いを入れていない物件A・B では、平面図の照合は全部一致。"""
    from core import overrides as ov
    from core.ai.tools import ToolContext
    from core.drawing_check import _CACHE, elevation_windows, findings, summary
    from tools.seeded_inspection import load_dataset, seeded_overrides
    b_rows = {r["view"]: r for r in elevation_windows("物件B", ToolContext.load("物件B").building)}
    assert all(r["aligned"] and r["same"] == r["windows"] > 0 for r in b_rows.values())
    ctx = ToolContext.load("物件A")
    plain = summary("物件A", ctx.building)
    assert plain["levels"][0]["same"] == 11 and not plain["levels"][0]["mismatches"]
    moved, _ = ov.apply(ctx.raw, seeded_overrides(load_dataset(), ctx.raw))
    _CACHE.clear()
    try:
        fs = findings("物件A", moved)
    finally:
        _CACHE.clear()
    targets = {t for f in fs for t in f["targets"]}
    assert {"O3", "O5", "O6", "O9", "O11"} <= targets                                  # E01〜E05
    assert any("06011" in f["message"] and not f["targets"] for f in fs)               # E06：消した窓 O7 の注記が余る


def test_one_code_can_describe_a_row_or_a_stack_of_openings():
    """★物件F（11/3）：品番 34023 が glb では横に3つ（840＋1790＋840）、07424 が上下に2つ（FL+20〜920 と 1030〜2400）。"""
    from core.drawing_check import opening_units

    def op(i, off, w, sill, head):
        return {"id": f"O{i}", "wall": "W1", "offset_mm": off, "width_mm": w, "sill_fl_mm": sill, "head_fl_mm": head}
    row = [op(1, 0, 840, 0, 2400), op(2, 815, 1790, 0, 2400), op(3, 2580, 840, 0, 2400), op(4, 5000, 840, 0, 2400)]
    units = opening_units(row[1], row)
    assert units[0] == (1790, 0, 2400) and (3420, 0, 2400) in units          # 遠い O4 は入らない
    stack = [op(8, 0, 795, 20, 920), op(9, 0, 795, 1030, 2400)]
    assert (795, 20, 2400) in opening_units(stack[0], stack)
    alone = [op(5, 0, 790, 900, 2000)]
    assert opening_units(alone[0], alone) == [(790, 900, 2000)]
