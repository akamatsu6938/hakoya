"""公開の前の確認（tools/history_check.py）を、一時的な git リポジトリと架空の言葉で確かめる（本物の履歴は使わない）。"""
from __future__ import annotations

import subprocess

import pytest

from tools import history_check as hc

WORD = "架空太郎邸"           # 架空の言葉（本物の ng_words ではない）
ADDR = "someone" + "@" + "hakoya-test.jp"   # 見つけるべきアドレス。実行時に組み立てる（このファイルにアドレスの文字を残さない）


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=テスト", "-c", f"user.email={ADDR}", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "old.md").write_text(f"昔のメモ：{WORD}", encoding="utf-8")
    (tmp_path / "sjis.txt").write_bytes(WORD.encode("cp932"))                      # Shift_JIS でも見つける
    (tmp_path / "spaced.txt").write_text("架 空 太 郎 邸", encoding="utf-8")           # 空白を入れても見つける
    (tmp_path / "wide.txt").write_text("架空　太郎邸", encoding="utf-8")            # 全角の空白でも見つける（★11/5）
    (tmp_path / "pic.txt").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)              # 拡張子を変えた画像
    (tmp_path / "ok.py").write_text("print('hello')  # noreply@anthropic.com と例示用の someone@example.com は許す", encoding="utf-8")
    (tmp_path / f"{WORD}の図面.txt").write_text("中身は大丈夫", encoding="utf-8")       # 名前に言葉
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "最初")
    (tmp_path / "old.md").unlink()                                                    # 消しても履歴には残る
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", f"連絡先 {ADDR} を書いてしまった")
    return tmp_path


def test_scan_finds_words_in_history_without_printing_them(repo, capsys):
    r = hc.scan(repo, ng_words=[WORD])
    assert not r["ok"] and r["commits"] == 2
    kinds = {(p["kind"], p["where"]) for p in r["problems"]}
    assert ("ng_word", "old.md") in kinds and ("ng_word", "sjis.txt") in kinds and ("ng_word", "spaced.txt") in kinds
    assert ("ng_word", "wide.txt") in kinds
    assert ("binary", "pic.txt") in kinds
    assert any(k == "ng_word_in_path" for k, _ in kinds)
    assert any(k == "email" and w.startswith("コミットの文") for k, w in kinds)
    assert any(k == "author_email" for k, _ in kinds)
    assert not any(w == "ok.py" for _, w in kinds)                                   # noreply は問題にしない
    text = repr(r)
    assert WORD not in text and ADDR not in text                                       # 言葉もアドレスも出さない


def test_scan_fails_closed_without_ng_words(repo):
    r = hc.scan(repo, ng_words=[])
    assert not r["ok"] and r["problems"][0]["kind"] == "no_ng_words"


def _sha(repo, rel) -> str:
    return subprocess.run(["git", "rev-parse", f"HEAD:{rel}"], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_allowed_files_skip_only_the_image_and_binary_checks(tmp_path):
    """★11/6：許すファイル（パスと中身の番号の組）は、図面・画像の拡張子と中身の印の検査だけ外す。
    言葉は許すファイルでも止める。中身の番号が違うもの・data/ と local_paths のパスは許さない。"""
    files = {"samples/sample_house/house.glb": b"glTF\x02\x00\x00\x00" + b"\x00" * 40,
             "samples/sample_house/out/S_model.png": b"\x89PNG\r\n\x1a\n" + b"\x01" * 20,
             "samples/sample_house/out/findings.json": f'{{"note": "{WORD}"}}'.encode("utf-8"),
             "samples/sample_house/out/model.3mf": b"PK\x03\x04" + b"\x02" * 20,       # 中身の番号を違えて渡す
             "data/sample/x.png": b"\x89PNG\r\n\x1a\n" + b"\x03" * 20,
             "samples/sample_house/local_paths.yaml": b"x: 1\n"}
    for rel, data in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(data)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=テスト", "-c", "user.email=1+t@users.noreply.github.com", "-c",
                    "commit.gpgsign=false", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=テスト", "-c", "user.email=1+t@users.noreply.github.com", "-c",
                    "commit.gpgsign=false", "commit", "-q", "-m", "見本"], cwd=tmp_path, check=True, capture_output=True)
    allow = {rel: _sha(tmp_path, rel) for rel in files}
    allow["samples/sample_house/out/model.3mf"] = "0" * 40
    r = hc.scan(tmp_path, ng_words=[WORD], allow=allow)
    found = {(p["where"], p["kind"]) for p in r["problems"]}
    for rel in ("samples/sample_house/house.glb", "samples/sample_house/out/S_model.png"):
        assert not any(w == rel for w, _ in found), rel                                # 許したものは通る
    assert ("samples/sample_house/out/findings.json", "ng_word") in found              # 言葉は許さない
    assert ("samples/sample_house/out/model.3mf", "binary") in found                   # 中身の番号が違う
    assert ("samples/sample_house/out/model.3mf", "drawing_or_image") in found
    assert ("data/sample/x.png", "data") in found and ("data/sample/x.png", "binary") in found
    assert ("samples/sample_house/local_paths.yaml", "data") in found
    assert r["allowed"] == 3 and not r["ok"]                                           # glb・png・findings.json
    plain = hc.scan(tmp_path, ng_words=[WORD])
    assert plain["allowed"] == 0
    assert ("samples/sample_house/house.glb", "binary") in {(p["where"], p["kind"]) for p in plain["problems"]}
    assert hc.scan(tmp_path, ng_words=[WORD], allow=list(allow.items()))["allowed"] == 3   # 組の並びでも渡せる
