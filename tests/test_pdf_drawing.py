"""高性能読み込み（★11/4）：文字を選べる PDF の図面を読む（core/pdf_drawing.py）と、入れ切り（core/reading.py）。
テストの PDF は中身を直接書いて、その場で作る（tests/pdf_maker.py。PDF のファイルはコミットしない）。"""
from __future__ import annotations

import numpy as np
import pytest
import yaml

from core import paths, reading
from core.pdf_drawing import DrawingUnreadable, read_pdf, rescale
from tests.pdf_maker import jp, line, pdf_bytes, text

W, H = 420.0, 297.0          # A3 横（pdf_bytes の既定の用紙）


def _pdf(tmp_path, content: bytes, name: str = "1階平面図.pdf", **kw):
    p = tmp_path / name
    p.write_bytes(pdf_bytes(content, **kw))
    return p


def test_paper_to_real_mm_with_up_as_plus_y(tmp_path):
    """用紙の左下からの mm → 用紙の真ん中が原点・上が +Y・縮尺を掛けた実寸（jww と同じ形）。"""
    p = _pdf(tmp_path, line(100, 148.5, 200, 148.5) + line(100, 148.5, 100, 198.5))
    d = read_pdf(p, "plan", ng_words=[], scale=100)
    assert d.scale == 100 and d.scale_source == "given" and d.source == "pdf"
    got = sorted(map(tuple, np.round(d.lines).astype(int).tolist()))
    assert got == sorted([(-11000, 0, -1000, 0), (-11000, 0, -11000, 5000)])         # 上へ 50mm の線は +Y へ 5,000mm


def test_title_band_is_dropped(tmp_path):
    """用紙の下の帯（幅の 8割より長い横線より下）の線と文字は読まない（表題欄に施主名・場所が書いてある）。"""
    c = (line(21, 20, 399, 20) + line(30, 10, 390, 10)                   # 表題欄の上の線・表題欄の中の線
         + text(40, 12, b"(TITLE) Tj") + text(40, 100, b"(KEEP) Tj") + line(50, 150, 150, 150))
    d = read_pdf(_pdf(tmp_path, c), "plan", ng_words=[], scale=100)
    words = [t["text"] for t in d.texts]
    assert words == ["KEEP"] and d.dropped["title_band"] == 2                          # 帯の中の線1本と文字1つ
    assert len(d.lines) == 2


def test_japanese_text_is_read_and_ng_words_are_dropped(tmp_path):
    c = text(40, 100, jp("洋室"), b"F2", 10) + text(40, 150, jp("鈴木様邸"), b"F2", 10) + line(50, 200, 150, 200)
    d = read_pdf(_pdf(tmp_path, c), "plan", ng_words=["鈴木"], scale=100)
    words = [t["text"] for t in d.texts]
    assert words == ["洋室"]                                   # 日本語が化けずに読める（exe には pdfminer の文字の表が要る）
    assert d.dropped["ng_words"] == 1 and "鈴木" not in str(d.texts)


def test_text_without_a_character_table_is_dropped(tmp_path):
    """文字の表のない字は (cid:65) のように読める → 意味のない文字なので捨てて、数だけ残す。"""
    c = text(40, 100, b"<0041> Tj", b"F3", 10) + text(40, 150, b"(OK) Tj") + line(50, 200, 150, 200)
    d = read_pdf(_pdf(tmp_path, c), "plan", ng_words=[], scale=100)
    assert [t["text"] for t in d.texts] == ["OK"] and d.dropped["unreadable_text"] == 1


def test_scanned_pdf_is_refused_with_how_to_fix(tmp_path):
    c = b"q %.2f 0 0 %.2f 0 0 cm /Im1 Do Q\n" % (1190.55, 841.89) + line(10, 10, 20, 10)
    with pytest.raises(DrawingUnreadable) as e:
        read_pdf(_pdf(tmp_path, c, image=True), "plan", ng_words=[])
    assert "スキャン" in str(e.value) and "文字を選べる PDF" in str(e.value)
    assert str(tmp_path) not in str(e.value) and "平面図" not in str(e.value)          # 場所・ファイル名は書かない


def test_scale_from_dimension_numbers(tmp_path):
    """寸法の数字 ÷ すぐ横の寸法線の長さ（用紙 mm）の多数決で縮尺を決める（ここでは 1/50）。"""
    c = b""
    for i, (v, y) in enumerate(((3640, 200), (1820, 170), (2730, 140))):
        c += line(60, y, 60 + v / 50, y) + text(62, y + 0.5, b"(%d) Tj" % v)
    d = read_pdf(_pdf(tmp_path, c), "plan", ng_words=[])
    assert d.scale == 50 and d.scale_source == "dims"
    assert sorted(x["value"] for x in d.dims) == [1820, 2730, 3640]
    x = next(x for x in d.dims if x["value"] == 3640)
    assert abs(abs(x["x1"] - x["x0"]) - 3640) < 2                                  # 実寸の寸法線の長さ＝数字


def test_unknown_scale_is_assumed_and_rescaled(tmp_path):
    c = line(100, 100, 200, 100) + line(100, 100, 100, 103) + line(100, 100, 150, 150)
    d = read_pdf(_pdf(tmp_path, c), "plan", ng_words=[])
    assert d.scale == 100 and d.scale_source == "assumed"
    assert len(d.lines) == 3 and len(d.align_lines) == 1          # 位置合わせには縦横の 5mm 以上の線だけ（短い線・斜めの線は使わない）
    d2 = rescale(d, 50)
    assert d2.scale == 50 and d2.scale_source == "aligned"
    assert np.allclose(d2.lines, d.lines / 2) and np.allclose(d2.align_lines, d.align_lines / 2)


def test_document_info_is_not_used(tmp_path):
    """PDF の文書の情報（作成者・題名。施主名や場所が入りやすい）は使わない：言葉に入れていなくても、結果に出てこない。"""
    p = tmp_path / "x.pdf"
    p.write_bytes(pdf_bytes(line(100, 100, 200, 100) + text(40, 100, b"(KEEP) Tj"),
                            info={"Author": "鈴木一郎", "Title": "C:/施主/鈴木様邸"}))
    import pdfplumber
    with pdfplumber.open(str(p)) as pdf:
        assert "鈴木" in str(pdf.metadata)                                    # 試験の PDF に、確かに入っている
    d = read_pdf(p, "plan", ng_words=[], scale=100)
    assert [t["text"] for t in d.texts] == ["KEEP"]
    assert "鈴木" not in str(vars(d)) and "施主" not in str(vars(d))


# ---------------------------------------------------------------- 図面の見つけ方と、高性能読み込みの入れ切り

def test_pdf_is_used_only_when_there_is_no_jww_of_that_kind(tmp_path):
    for n in ("1階平面図.pdf", "立面図.jww", "立面図.pdf", "メモ.pdf"):
        (tmp_path / n).write_bytes(b"x")
    found, notes = paths.detect_jww(tmp_path)
    assert {k: v.name for k, v in found.items()} == {"plan": "1階平面図.pdf", "elevation": "立面図.jww"}
    assert set(paths.detect_jww(tmp_path, with_pdf=False)[0]) == {"elevation"}


@pytest.fixture
def house(tmp_path, monkeypatch):
    """図面のフォルダを登録した物件A（data/ の代わりに一時フォルダ）。"""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    folder = tmp_path / "図面"
    folder.mkdir()
    (folder / "1階平面図.pdf").write_bytes(pdf_bytes(line(100, 100, 300, 100) + line(100, 100, 100, 200)))
    yml = tmp_path / "local_paths.yaml"
    yml.write_text(yaml.safe_dump({"物件A": {"glb": str(tmp_path / "a.glb"), "jww_folder": str(folder)}}, allow_unicode=True),
                   encoding="utf-8")
    real = paths.property_files
    monkeypatch.setattr(paths, "property_files", lambda alias, path=None, use_mode=True: real(alias, path or yml, use_mode))
    return yml


def test_switch_off_means_glb_only(house):
    from core.drawing_check import summary
    from core.jww import read_property
    assert reading.is_high("物件A", has_drawings=True)                        # 決めていなければ、図面を登録した物件は入（前と同じ）
    d = read_property("物件A", "plan")
    assert d is not None and d.source == "pdf" and len(d.lines) == 2
    reading.set_high("物件A", False)
    f = paths.property_files("物件A")
    assert f.jww == {} and any("高性能読み込みが切" in n for n in f.notes)
    assert paths.property_files("物件A", use_mode=False).jww                       # 一覧には登録した図面を見せる
    assert read_property("物件A", "plan") is None                                   # 図面を使う所はすべて「図面なし」
    assert summary("物件A", {"walls": [], "openings": [], "levels": [{"name": "1F"}]})["available"] is False
    reading.set_high("物件A", True)
    assert read_property("物件A", "plan") is not None


def test_saved_scale_is_forgotten_when_the_file_changes(house, tmp_path):
    p = tmp_path / "図面" / "1階平面図.pdf"
    reading.save_scale("物件A", "plan", p, 50, "aligned")
    assert reading.saved_scale("物件A", "plan", p) == 50
    from core.jww import read_property
    assert read_property("物件A", "plan").scale == 50                               # 控えた縮尺で読む
    p.write_bytes(p.read_bytes() + b"\n%")                                          # 図面を書き出し直した
    assert reading.saved_scale("物件A", "plan", p) is None
    saved = (paths.DATA_DIR / "out" / "物件A" / "reading.json").read_text(encoding="utf-8")
    assert "平面図" not in saved and str(tmp_path) not in saved                      # 控えに場所・ファイル名を書かない


def test_screen_switch(house, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    monkeypatch.setattr(paths, "LOCAL_PATHS", house)
    monkeypatch.setattr(server, "_aliases", lambda: ["物件A"])
    monkeypatch.setattr(server, "property_files", paths.property_files)
    c = TestClient(server.app)
    r = c.post("/api/reading/物件A", json={"high": False})
    assert r.status_code == 200 and r.json()["properties"][0]["high"] is False
    assert c.post("/api/reading/物件A", json={"high": True}).json()["properties"][0]["high"] is True
    assert c.post("/api/reading/物件Z", json={"high": True}).status_code == 404
