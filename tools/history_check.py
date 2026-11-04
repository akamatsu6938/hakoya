"""公開の前の確認：git の履歴全体を調べる（SPEC §11「公開の前の確認」1）。読むだけで、何も書き換えない。

調べるもの（すべてのブランチ・タグの、すべてのコミットのファイル。消したファイルも含む）：
- 出してはいけない言葉（data/local_paths.yaml の ng_words：施主名・住所など）。UTF-8・Shift_JIS（cp932）・UTF-16 で探す。
  空白を抜いた形・全角半角をそろえた形でも探す。
- API キーらしい文字
- メールアドレス（Anthropic と GitHub の noreply、例示用の example.com などは除く）
- data/ の中のファイル、図面・模型・画像・PDF などのファイル（名前の拡張子と、中身の先頭の印で見分ける）
- コミットの文（メッセージ）と、作者・コミットした人の名前とアドレス

出すのは「どのコミット・どのパス・何の種類か・ng_words の何番目か」だけ。言葉・アドレス・キーそのものは出さない。
ng_words が空なら、調べられないので失敗にする（調べたつもりにならない）。
使い方：.venv\\Scripts\\python -m tools.history_check     問題があれば終了コード 1
"""
from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from tools.precommit_check import API_KEY_PATTERNS, BLOCKED_EXT, load_ng_words

REPO_ROOT = Path(__file__).resolve().parents[1]
MORE_EXT = (".dxf", ".dwg", ".obj", ".zip", ".jwc", ".bmp", ".tif", ".tiff", ".heic")
SIGNATURES = {b"\x89PNG": "PNG 画像", b"\xff\xd8\xff": "JPEG 画像", b"GIF8": "GIF 画像", b"%PDF": "PDF",
              b"glTF": "glb（3D）", b"PK\x03\x04": "ZIP（3MF など）", b"BM": None}
EMAIL_RE = re.compile(rb"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 許すアドレス：Anthropic と GitHub の noreply、例示用に予約されたドメイン（example.com など。誰のものでもない）
ALLOWED_EMAIL = (rb"noreply@anthropic.com", rb"users.noreply.github.com",
                 rb"@example.com", rb"@example.org", rb"@example.net")


def _git(args: list[str], repo: Path, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, input=data, capture_output=True, check=True).stdout


def _variants(words: list[str]) -> list[tuple[int, list[bytes]]]:
    """言葉ごとの、探す形（UTF-8・cp932・UTF-16LE。全角半角をそろえた形・空白を抜いた形も）。"""
    out = []
    for i, w in enumerate(words, 1):
        forms = {w, unicodedata.normalize("NFKC", w), re.sub(r"\s+", "", w), re.sub(r"\s+", "", unicodedata.normalize("NFKC", w))}
        bs = []
        for f in forms:
            if not f:
                continue
            for enc in ("utf-8", "cp932", "utf-16-le"):
                try:
                    bs.append(f.encode(enc))
                except UnicodeEncodeError:
                    pass
        out.append((i, sorted(set(bs))))
    return out


def _bad_email(m: bytes) -> bool:
    return not any(m.lower().endswith(a) for a in ALLOWED_EMAIL)


def scan_blob(path: str, blob: bytes, words: list[tuple[int, list[bytes]]]) -> list[tuple[str, str]]:
    """1つのファイルの中身を調べる。[(種類, 詳しく)]（言葉・アドレスそのものは入れない）。"""
    found = []
    low = path.lower()
    if low.startswith("data/") or "local_paths" in low:
        found.append(("data", "data/ か local_paths のファイル"))
    if low.endswith(BLOCKED_EXT + MORE_EXT):
        found.append(("drawing_or_image", "図面・模型・画像などの拡張子"))
    for sig, label in SIGNATURES.items():
        if label and blob.startswith(sig):
            found.append(("binary", f"中身が {label}"))
    flat = re.sub(rb"\s+", b"", blob)
    norm = unicodedata.normalize("NFKC", blob.decode("utf-8", errors="ignore")).encode("utf-8")
    for i, forms in words:
        if any(f in blob or f in flat or f in norm for f in forms):
            found.append(("ng_word", f"ng_words の {i} 番目"))
    text = blob.decode("utf-8", errors="ignore")
    if any(p.search(text) for p in API_KEY_PATTERNS):
        found.append(("api_key", "API キーらしい文字"))
    if any(_bad_email(m) for m in EMAIL_RE.findall(blob)):
        found.append(("email", "メールアドレス"))
    return found


def scan(repo: Path = REPO_ROOT, ng_words: list[str] | None = None) -> dict:
    """履歴全体を調べた結果。{"ok": bool, "commits": n, "blobs": n, "problems": [...]}（言葉・アドレスは入れない）。"""
    words_list = load_ng_words() if ng_words is None else ng_words
    if not words_list:
        return {"ok": False, "commits": 0, "blobs": 0,
                "problems": [{"kind": "no_ng_words", "where": "-", "detail": "ng_words が空なので、施主名などを調べられません"}]}
    words = _variants(words_list)
    problems = []
    # すべてのコミットのファイル（パスつき。同じ中身は1回だけ読む）
    objs = _git(["rev-list", "--all", "--objects"], repo).decode("utf-8", errors="replace").splitlines()
    paths: dict[str, str] = {}
    for line in objs:
        sha, _, path = line.partition(" ")
        if path:
            paths.setdefault(sha, path)
    kinds = _git(["cat-file", "--batch-check=%(objectname) %(objecttype)"], repo, "\n".join(paths).encode()).decode().split("\n")
    blobs = [s.split()[0] for s in kinds if s.endswith(" blob")]
    for sha in blobs:
        blob = _git(["cat-file", "blob", sha], repo)
        where = paths[sha]
        if any(w in where or unicodedata.normalize("NFKC", w) in unicodedata.normalize("NFKC", where) for w in words_list):
            problems.append({"kind": "ng_word_in_path", "where": "（言葉を含むパス。パスは出さない）", "object": sha[:10],
                             "detail": "ファイルの名前かフォルダの名前に、出してはいけない言葉"})
            where = "（言葉を含むパス）"
        for kind, detail in scan_blob(paths[sha], blob, words):
            problems.append({"kind": kind, "where": where, "object": sha[:10], "detail": detail})
    # コミットの文・作者とコミットした人
    log = _git(["log", "--all", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x01"], repo)
    commits = [c for c in log.split(b"\x01") if c.strip()]
    author_mail = 0
    for c in commits:
        h, an, ae, cn, ce, body = (c.strip(b"\n").split(b"\x00") + [b""] * 6)[:6]
        h = h.decode()[:10]
        if _bad_email(ae) or _bad_email(ce):
            author_mail += 1
        for kind, detail in scan_blob("(commit message)", an + b"\n" + cn + b"\n" + body, words):
            if kind in ("ng_word", "api_key", "email"):
                problems.append({"kind": kind, "where": f"コミットの文・名前 {h}", "object": h, "detail": detail})
    if author_mail:
        problems.append({"kind": "author_email", "where": f"{author_mail} 件のコミット", "object": "-",
                         "detail": "作者かコミットした人のアドレスが noreply ではない（公開すると見える。変えるには履歴の書き換えが要る）"})
    return {"ok": not problems, "commits": len(commits), "blobs": len(blobs), "problems": problems}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    r = scan()
    print(f"履歴を調べました：コミット {r['commits']} 件・ファイルの中身 {r['blobs']} 個")
    if r["ok"]:
        print("問題は見つかりませんでした")
        return 0
    groups: dict[str, list[dict]] = {}
    for p in r["problems"]:
        groups.setdefault(p["kind"], []).append(p)
    for kind, items in groups.items():
        print(f"[{kind}] {len(items)} 件")
        for p in items[:30]:
            print(f"  - {p['where']}（{p['object']}）：{p['detail']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
