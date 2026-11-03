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
