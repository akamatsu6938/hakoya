"""平面図の札（tools/plan_preview.py）：画面で押して選べる data-id・白いふち取り・吹き出し・凡例・紙の大きさ。
★11/5 夜 作者「W1 や O1 などが見づらい」。架空の細長い家で確かめる（実データ不要）。"""
from __future__ import annotations

import re

from tools.plan_preview import ID_LEGEND, render_svg
from tests.test_body_split import _long_house


def _house() -> dict:
    b = _long_house(window_at=4000.0)
    for o in b["openings"]:
        o.setdefault("style", "sliding")                                        # 平面図の一覧に窓の形を書く
    return b


def test_labels_have_data_id_and_halo():
    b = _house()
    svg = render_svg(b, "物件T")
    for w in b["walls"]:                                                        # 壁の札：番号と厚み。押すと選べる（data-id）
        m = re.search(r'<text data-id="%s"([^>]*)>([^<]*)' % w["id"], svg)
        assert m, w["id"]
        assert m.group(2) == f'{w["id"]} {w["thickness_mm"]:g}'
        assert 'font-size="13"' in m.group(1) and 'paint-order="stroke"' in m.group(1) and 'stroke="white"' in m.group(1)
    o = b["openings"][0]                                                        # 窓・ドアの札：番号と上端
    m = re.search(r'<text data-id="%s"([^>]*)>([^<]*)' % o["id"], svg)
    assert m and m.group(2) == f'{o["id"]} FL+{o["head_fl_mm"]}'
    assert 'font-size="12"' in m.group(1) and 'paint-order="stroke"' in m.group(1)


def test_tooltips_name_the_kind():
    b = _house()
    svg = render_svg(b, "物件T")
    o = b["openings"][0]
    tip = f'<title>{o["id"]} 窓 幅 800 上端 FL+2000</title>'
    assert svg.count(tip) == 2                                                  # 開口の形と札の両方に吹き出し
    ext = next(w for w in b["walls"] if w["kind"] == "exterior")
    assert f'<title>{ext["id"]} 外壁 厚み {ext["thickness_mm"]:g}</title>' in svg
    inner = next(w for w in b["walls"] if w["kind"] == "interior")
    assert f'<title>{inner["id"]} 内壁 厚み {inner["thickness_mm"]:g}</title>' in svg


def test_legend_explains_id_letters_and_opening_colors():
    svg = render_svg(_house(), "物件T")
    assert ID_LEGEND == "W＝壁　O＝窓・ドア　Y＝屋根"
    assert '<text class="id-legend"' in svg and ID_LEGEND in svg
    for label in ("窓", "ドア", "玄関ドア"):                                     # 窓・ドアの色の見本
        assert f'font-size="12">{label}</text>' in svg


def test_paper_has_fixed_size():
    """画面で拡大・移動（viewBox を変える）しても、白い紙が図の所に留まる（100% だと見ている範囲に付いて動く）。"""
    svg = render_svg(_house(), "物件T")
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert m and f'<rect width="{m.group(1)}" height="{m.group(2)}" fill="white"/>' in svg
    assert 'width="100%"' not in svg
