"""公開の前の確認（tools/history_check.py）を、一時的な git リポジトリと架空の言葉で確かめる（本物の履歴は使わない）。"""
from __future__ import annotations

import subprocess

import pytest

from tools import history_check as hc

WORD = "架空太郎邸"           # 架空の言葉（本物の ng_words ではない）


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=テスト", "-c", "user.email=someone@example.com", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "old.md").write_text(f"昔のメモ：{WORD}", encoding="utf-8")
    (tmp_path / "sjis.txt").write_bytes(WORD.encode("cp932"))                      # Shift_JIS でも見つける
    (tmp_path / "spaced.txt").write_text("架 空 太 郎 邸", encoding="utf-8")           # 空白を入れても見つける
    (tmp_path / "pic.txt").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)              # 拡張子を変えた画像
    (tmp_path / "ok.py").write_text("print('hello')  # noreply@anthropic.com は許す", encoding="utf-8")
    (tmp_path / f"{WORD}の図面.txt").write_text("中身は大丈夫", encoding="utf-8")       # 名前に言葉
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "最初")
    (tmp_path / "old.md").unlink()                                                    # 消しても履歴には残る
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "連絡先 someone@example.com を書いてしまった")
    return tmp_path


def test_scan_finds_words_in_history_without_printing_them(repo, capsys):
    r = hc.scan(repo, ng_words=[WORD])
    assert not r["ok"] and r["commits"] == 2
    kinds = {(p["kind"], p["where"]) for p in r["problems"]}
    assert ("ng_word", "old.md") in kinds and ("ng_word", "sjis.txt") in kinds and ("ng_word", "spaced.txt") in kinds
    assert ("binary", "pic.txt") in kinds
    assert any(k == "ng_word_in_path" for k, _ in kinds)
    assert any(k == "email" and w.startswith("コミットの文") for k, w in kinds)
    assert any(k == "author_email" for k, _ in kinds)
    assert not any(w == "ok.py" for _, w in kinds)                                   # noreply は問題にしない
    text = repr(r)
    assert WORD not in text and "someone@example.com" not in text                      # 言葉もアドレスも出さない


def test_scan_fails_closed_without_ng_words(repo):
    r = hc.scan(repo, ng_words=[])
    assert not r["ok"] and r["problems"][0]["kind"] == "no_ng_words"
