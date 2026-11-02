"""作業の終わりの声かけ（Claude Code の Stop フック）。

docs/devlog.md に今日の見出し（## YYYY-MM-DD）がなければ、一言知らせる。止めはしない。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path


def has_today_entry(devlog: Path, today: dt.date) -> bool:
    if not devlog.exists():
        return False
    return f"## {today.isoformat()}" in devlog.read_text(encoding="utf-8")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {}
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    if not has_today_entry(Path(project_dir) / "docs" / "devlog.md", dt.date.today()):
        print(json.dumps({"systemMessage": "今日の docs/devlog.md がまだです。区切りで書きましょう。"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
