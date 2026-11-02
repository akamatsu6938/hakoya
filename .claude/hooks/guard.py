"""書き込む前の見張り（Claude Code の PreToolUse フック）。

止めるもの（SPEC §2-1・§2-6・§11）:
- 元のファイル（*.glb / *.jww / *.jwc）への書き込み・削除・移動
- data/ の中への書き込み（data/local_paths.yaml だけは例外）
- git add で data/・glb・jww を入れる操作と、git add -f（.gitignore を無視する）

止めるときは終了コード 2 と理由（stderr）を返す。Claude Code はその操作を取りやめる。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PROTECTED_EXT = (".glb", ".jww", ".jwc")
ALLOWED_IN_DATA = ("local_paths.yaml",)

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

# シェルのコマンドで「書き込み・削除・移動」にあたる言葉
WRITE_VERBS = re.compile(
    r"(?i)(\bset-content\b|\badd-content\b|\bout-file\b|\bremove-item\b|\bmove-item\b|"
    r"\brename-item\b|\bcopy-item\b|\bclear-content\b|\bnew-item\b|\brm\b|\bdel\b|\bmv\b|\bcp\b|"
    r"\bren\b|\btee\b|\btruncate\b|\bsed\s+-i\b|\bshred\b)"
)
PROTECTED_IN_SHELL = re.compile(r"(?i)(\.(glb|jww|jwc)\b|(^|[\s\"'=/\\])data[/\\])")
# リダイレクト（> や >>）の書き込み先そのものが守る対象か
REDIRECT_TO_PROTECTED = re.compile(r"(?i)(?<![0-9&])>>?\s*[\"']?[^\s\"']*(\.(glb|jww|jwc)\b|(^|[/\\])?data[/\\])")


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower()


def check_file_path(file_path: str, project_dir: str) -> str | None:
    if not file_path:
        return None
    path = _norm(file_path)
    if path.endswith(PROTECTED_EXT):
        return "元のファイル（glb・jww）には書き込めません（SPEC §2-1）。作業はコピーで行ってください。"
    project = _norm(str(Path(project_dir)))
    if not re.match(r"^[a-z]:/|^/", path):
        path = f"{project}/{path}"
    data_dir = f"{project}/data/"
    if path.startswith(data_dir):
        if path[len(data_dir):] in ALLOWED_IN_DATA:
            return None
        return "data/ の中には書き込めません（実物のデータを守るため。SPEC §2）。出力はコードから行ってください。"
    return None


def check_shell(command: str) -> str | None:
    if not command:
        return None
    cmd = command
    # git add の見張り
    for m in re.finditer(r"(?i)\bgit\s+add\b([^;&|\n]*)", cmd):
        args = m.group(1)
        if re.search(r"(^|\s)(-f|--force)\b", args):
            return "git add -f は使えません（.gitignore を無視して data/ などが入るのを防ぐため。SPEC §2-6）。"
        if re.search(r"(?i)(\.(glb|jww|jwc|3mf)\b|(^|[\s\"'/\\])data([/\\]|\s|$)|local_paths)", args):
            return "data/・glb・jww・3mf は git に入れられません（SPEC §2-6）。"
    if REDIRECT_TO_PROTECTED.search(cmd):
        return "元のファイル（glb・jww）や data/ の中へは書き込めません（SPEC §2-1）。"
    if WRITE_VERBS.search(cmd) and PROTECTED_IN_SHELL.search(cmd):
        if re.search(r"(?i)data[/\\]local_paths\.yaml", cmd) and not re.search(r"(?i)\.(glb|jww|jwc)\b", cmd):
            return None
        return "元のファイル（glb・jww）や data/ の中を、書き換え・削除・移動するコマンドは使えません（SPEC §2-1）。"
    return None


def decide(payload: dict, project_dir: str) -> str | None:
    """止める理由を返す。止めないときは None。"""
    tool = payload.get("tool_name", "")
    tin = payload.get("tool_input") or {}
    if tool in FILE_TOOLS:
        return check_file_path(tin.get("file_path") or tin.get("notebook_path") or "", project_dir)
    if tool in SHELL_TOOLS:
        return check_shell(tin.get("command", ""))
    return None


def main() -> int:
    sys.stderr.reconfigure(encoding="utf-8")
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    reason = decide(payload, project_dir)
    if reason:
        print(f"[見張り] {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
