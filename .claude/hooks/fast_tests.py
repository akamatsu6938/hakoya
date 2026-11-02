"""編集した後の自動テスト（Claude Code の PostToolUse フック）。

core/ か adapters/ のファイルを直したら、速いテスト（slow・realdata の印がないもの）を走らせる。
失敗したら終了コード 2 で結果の末尾を返し、Claude Code に直させる。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WATCHED_DIRS = ("core/", "adapters/")


def should_run(file_path: str, project_dir: str) -> bool:
    if not file_path.endswith(".py"):
        return False
    path = file_path.replace("\\", "/").lower()
    project = str(Path(project_dir)).replace("\\", "/").lower().rstrip("/") + "/"
    rel = path[len(project):] if path.startswith(project) else path
    return rel.startswith(WATCHED_DIRS)


def main() -> int:
    sys.stderr.reconfigure(encoding="utf-8")
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    file_path = (payload.get("tool_input") or {}).get("file_path", "")
    if not should_run(file_path, project_dir):
        return 0
    python = Path(project_dir) / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        python = Path(sys.executable)
    env = dict(os.environ, PYTHONUTF8="1")
    result = subprocess.run(
        [str(python), "-m", "pytest", "-q", "-x", "-m", "not slow and not realdata"],
        cwd=project_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    if result.returncode not in (0, 5):  # 5 = テストが1つもない
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-30:])
        print(f"[自動テスト] 速いテストが失敗しました:\n{tail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
