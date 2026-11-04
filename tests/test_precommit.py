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


def test_ng_words_include_each_property(tmp_path):
    """★11/4 の見直し：物件ごとに足した言葉（画面の「物件の登録」・手で書いたもの）も、コミット前の検査に入る。"""
    from tools.precommit_check import load_ng_words
    p = tmp_path / "local_paths.yaml"
    p.write_text("ng_words: [架空施主]\n物件C:\n  glb: C:/x\n  ng_words: [架空次郎, ほげ町9-9]\n", encoding="utf-8")
    assert load_ng_words(p) == ["架空施主", "架空次郎", "ほげ町9-9"]


def test_empty_lines_are_not_words(tmp_path):
    """★11/4：`- ` だけの空の行は None になり、「None」という言葉としてコードの None に当たっていた。空の行は捨てる。"""
    from tools.precommit_check import load_ng_words
    p = tmp_path / "local_paths.yaml"
    p.write_text("ng_words:\n  - 架空施主\n  - \n物件C:\n  glb: C:/x\n  ng_words:\n    -\n    - 架空次郎\n", encoding="utf-8")
    assert load_ng_words(p) == ["架空施主", "架空次郎"]
    assert find_problems({"a.py": b"x = None\n"}, load_ng_words(p)) == []


def test_ng_word_with_other_width_or_spaces():
    """全角・半角と空白の違いでも見つける（言葉「架空 施主」→ 中身「架空施主様」、言葉「ほげ市1-2-3」→ 中身「ほげ市１－２－３」）。"""
    assert find_problems({"docs/a.md": "架空施主様".encode()}, ["架空 施主"])
    assert find_problems({"docs/a.md": "ほげ市１－２－３".encode()}, NG)


def test_broken_yaml_does_not_show_the_line(tmp_path):
    """YAML の書き間違いのエラーに、その行（施主名の入った場所）を出さない。"""
    import pytest

    from tools.precommit_check import load_ng_words
    p = tmp_path / "local_paths.yaml"
    p.write_text("ng_words: [架空施主]\n物件C:\n" + r'  glb: "C:\Users\架空施主邸\家.glb"' + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        load_ng_words(p)
    assert "3 行目" in str(e.value) and "架空施主" not in str(e.value) and e.value.__cause__ is None
