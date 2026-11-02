"""コミットの前の検査（git の pre-commit から呼ぶ）。

止めるもの（SPEC §2-2・§2-6・§2-7・§11）:
- data/ の中のファイル、glb・jww・3mf・stl・画像・PDF（物件の図面や画像が混ざるのを防ぐ）
- 出してはいけない言葉（data/local_paths.yaml の ng_words：施主名・住所など）を含むファイル（名前と中身）
- API キーらしき文字列

問題を見つけたら一覧を出して終了コード 1（コミットが止まる）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BLOCKED_EXT = (".glb", ".jww", ".jwc", ".3mf", ".stl", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".pdf")
API_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{20,}[\"']"),
]
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_PATHS = REPO_ROOT / "data" / "local_paths.yaml"


def load_ng_words(path: Path = LOCAL_PATHS) -> list[str]:
    import yaml  # .venv に入っている

    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(w) for w in (data.get("ng_words") or []) if str(w).strip()]


def find_problems(files: dict[str, bytes], ng_words: list[str]) -> list[str]:
    """files: {リポジトリ内のパス: 中身}。見つかった問題を日本語で返す（言葉そのものは出さない）。"""
    problems: list[str] = []
    for name, blob in files.items():
        lower = name.replace("\\", "/").lower()
        if lower.startswith("data/"):
            problems.append(f"{name}: data/ の中のファイルはコミットできません")
            continue
        if lower.endswith(BLOCKED_EXT):
            problems.append(f"{name}: 図面・模型・画像のファイルはコミットできません")
            continue
        text = blob.decode("utf-8", errors="ignore")
        for i, w in enumerate(ng_words, 1):
            if w in name or w in text:
                problems.append(f"{name}: 出してはいけない言葉（ng_words の {i} 番目）が入っています")
        for pat in API_KEY_PATTERNS:
            if pat.search(text):
                problems.append(f"{name}: API キーらしき文字列が入っています")
                break
    return problems


def staged_files() -> dict[str, bytes]:
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    files = {}
    for n in filter(None, names):
        blob = subprocess.run(["git", "show", f":{n}"], cwd=REPO_ROOT, capture_output=True, check=True).stdout
        files[n] = blob
    return files


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ng_words = load_ng_words()
    if not ng_words:
        print("[コミット前の検査] 注意：data/local_paths.yaml の ng_words が空か、ファイルがありません。施主名の検査はしていません。")
    problems = find_problems(staged_files(), ng_words)
    if problems:
        print("[コミット前の検査] コミットを止めました：")
        for p in problems:
            print("  - " + p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
