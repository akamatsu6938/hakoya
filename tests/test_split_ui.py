"""平面図の分け目をドラッグで動かす画面（app.js）が読む値：<g id="split"> の data-*（tools/plan_preview.py）。
架空の細長い家で確かめる（実データ不要）。"""
from __future__ import annotations

import html
import json
import re

from tools.plan_preview import render_svg
from tests.test_body_split import _long_house

SPLIT = {"u": [0.0, 1.0], "cuts": [6065.5], "lo": 0.0, "hi": 14267.0, "ranges": [[2000.0, 12300.0]],
         "sides": ["南", "北"], "source": "human"}


def _house() -> dict:
    b = _long_house()
    for o in b["openings"]:
        o.setdefault("style", "sliding")                                        # 平面図の一覧に窓の形を書く
    return b


def _attr(svg: str, name: str) -> str:
    m = re.search(r'<g id="split"[^>]*\s' + name + r'''=(["'])(.*?)\1''', svg)
    assert m, name
    return html.unescape(m.group(2))


def test_split_group_has_what_the_screen_needs():
    svg = render_svg(_house(), "物件T", split=SPLIT)
    assert json.loads(_attr(svg, "data-sides")) == ["南", "北"]                 # ドラッグ中の札の名前
    assert json.loads(_attr(svg, "data-ranges")) == [[2000.0, 12300.0]]         # 動かせる範囲
    assert _attr(svg, "data-source") == "human"                                 # 「分け目を自動に戻す」を出す
    assert float(_attr(svg, "data-lo")) == 0.0 and float(_attr(svg, "data-hi")) == 14267.0
    assert 'data-role="split" data-i="0" data-t="6065.5"' in svg
    assert '<text class="split-label" data-i="0"' in svg and "南 121mm・北 164mm（模型）" in svg


def test_no_split_group_without_cuts():
    assert 'id="split"' not in render_svg(_house(), "物件T", split={**SPLIT, "cuts": []})
    assert 'id="split"' not in render_svg(_house(), "物件T")
