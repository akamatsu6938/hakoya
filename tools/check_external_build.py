"""社外用の exe（フォルダ）に、入れてはいけないものが入っていないかを調べる（SPEC §12「社外用の exe に claude_code の接続と API キーが入っていない」）。

使い方：.venv\\Scripts\\python -m tools.check_external_build dist\\external\\HAKOYA_external
調べること：
- Claude Agent SDK（claude_agent_sdk）と Claude Code 本体（claude.exe・claude）が入っていない
- 版の印（EDITION）が external
- data\\（実物の図面・glb・設定）が入っていない
- API キーらしい文字（sk-ant-…）が、文字のファイルにも、バイナリの中にもない
- PDF の日本語の文字の表（pdfminer の cmap）が入っている（ないと、高性能読み込みで PDF の日本語が読めない。★11/4）
見つけた内容は出さない（場所と種類だけ）。問題があれば終了コード 1。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

KEY_RE = re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")
FORBIDDEN_DIRS = ("claude_agent_sdk", "mcp")
FORBIDDEN_FILES = ("claude.exe", "claude")
JP_CMAP = "UniJIS-UCS2-H.json.gz"     # 日本語の PDF の文字の表の1つ（ほかの表と一緒に pdfminer/cmap に入る）
TEXT_EXT = {".py", ".json", ".yaml", ".yml", ".md", ".txt", ".js", ".css", ".html", ".toml", ".cfg", ".ini"}


def check(root: Path) -> list[str]:
    problems = []
    if not root.is_dir():
        return [f"フォルダがありません：{root}"]
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        parts = {x.lower() for x in rel.parts}
        if p.is_dir() and p.name.lower() in FORBIDDEN_DIRS:
            problems.append(f"入れてはいけないパッケージ：{rel}")
        if p.is_file() and p.name.lower() in FORBIDDEN_FILES:
            problems.append(f"Claude Code 本体らしいファイル：{rel}")
        if "data" in parts and rel.parts[0].lower() == "data":
            problems.append(f"data フォルダが入っている：{rel}")
        if p.is_file() and p.stat().st_size < 200_000_000:
            try:
                blob = p.read_bytes()
            except OSError:
                continue
            if KEY_RE.search(blob):
                problems.append(f"API キーらしい文字：{rel}")
    if not any(p.parent.name == "cmap" for p in root.rglob(JP_CMAP)):
        problems.append("PDF の日本語の文字の表（pdfminer の cmap）がない（高性能読み込みで PDF の文字が読めない）")
    marks = list(root.rglob("EDITION"))
    if not marks:
        problems.append("版の印（EDITION）がない")
    elif any(m.read_text(encoding="utf-8").strip() != "external" for m in marks):
        problems.append("版の印（EDITION）が external でない")
    return problems


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0]) if args else Path("dist/external/HAKOYA_external")
    problems = check(root)
    if problems:
        print("社外用の exe に問題があります：")
        for x in problems:
            print("  - " + x)
        return 1
    print(f"社外用の exe を調べました：問題なし（{root}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
