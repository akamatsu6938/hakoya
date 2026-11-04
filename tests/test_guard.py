"""書き込む前の見張り（.claude/hooks/guard.py）が、止めるべきものを止め、普通の作業は通すか。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("guard", ROOT / ".claude" / "hooks" / "guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

PROJECT = "C:/work/hakoya"


def _file(tool, path):
    return {"tool_name": tool, "tool_input": {"file_path": path}}


def _sh(cmd, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": cmd}}


@pytest.mark.parametrize("payload", [
    _file("Write", "C:/Users/someone/Desktop/物件A.glb"),
    _file("Edit", r"C:\図面\【物件】\平面図.JWW"),
    _file("Write", "C:/work/hakoya/data/out/building.json"),
    _file("Write", "data/cache/source.glb"),
    _sh("rm -rf data/cache"),
    _sh("Remove-Item C:/Desktop/物件A.glb", tool="PowerShell"),
    _sh("echo x > data/out/a.txt"),
    _sh("git add -f data/local_paths.yaml"),
    _sh("git add data/"),
    _sh("git add model.3mf"),
    _sh("git add --force ."),
    _file("Write", r"C:\図面\物件C\1階平面図.pdf"),                   # ★11/4 PDF の図面も元のファイル
    _sh("Remove-Item C:/図面/1階平面図.PDF", tool="PowerShell"),
    _sh("git add docs/plan.pdf"),
])
def test_blocks(payload):
    assert guard.decide(payload, PROJECT) is not None


@pytest.mark.parametrize("payload", [
    _file("Write", "C:/work/hakoya/core/builder.py"),
    _file("Edit", "C:/work/hakoya/data/local_paths.yaml"),
    _file("Write", "C:/work/hakoya/docs/devlog.md"),
    _sh("git add -A"),
    _sh("git add ."),
    _sh("git status --short 2>/dev/null"),
    _sh("cat data/out/building.json"),
    _sh(".venv/Scripts/python -m adapters.walkinhome 物件A"),
    _sh(".venv/Scripts/python -m pytest tests/test_pdf_drawing.py"),
    _sh("Get-Content data/local_paths.yaml", tool="PowerShell"),
    {"tool_name": "Read", "tool_input": {"file_path": "C:/x/物件A.glb"}},
])
def test_allows(payload):
    assert guard.decide(payload, PROJECT) is None
