"""コミット前の検査（tools/precommit_check.py）が、施主名・APIキー・図面の混入を見つけるか。
施主名の代わりに架空の言葉を使う（本物の名前はテストにも書かない）。"""
from __future__ import annotations

from tools.precommit_check import find_problems

NG = ["架空施主", "ほげ市1-2-3"]


def test_clean_file_passes():
    assert find_problems({"core/a.py": "x = 1\n".encode()}, NG) == []


def test_ng_word_in_content():
    probs = find_problems({"docs/devlog.md": "今日は架空施主様の家を試した".encode()}, NG)
    assert len(probs) == 1
    assert "架空施主" not in probs[0]  # 見つけた言葉そのものは表示しない


def test_ng_word_in_filename():
    assert find_problems({"tests/ほげ市1-2-3.json": b"{}"}, NG)


def test_api_key():
    fake = "sk-ant-" + "a1" * 20
    assert find_problems({"core/x.py": f"key = '{fake}'".encode()}, NG)


def test_blocked_files():
    for name in ["data/local_paths.yaml", "x/物件A.glb", "out/model.3mf", "docs/plan.png", "a/平面図.jww"]:
        assert find_problems({name: b""}, NG), name
