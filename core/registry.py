"""物件の登録（画面の「設定」→「物件の登録」）。data/local_paths.yaml の最後に、仮名・glb の場所・図面のフォルダ・
出してはいけない言葉を書き足す（README の手順5を、YAML を手で書かずにできるようにする）。

- 元のファイルは読むだけ（コピーもしない。組み立てのときに data/cache へ写す：core/paths.py）。
- 画面に返すのは、仮名と、見つかったものの種類・数だけ。場所（パス）と元のファイル名（施主名が入ることがある）は返さない。
- 書き足すだけで、いまの中身（作者のコメントや並び）は書き換えない。書く前に、足したあとの全体が読めるか確かめる。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from core import paths as _paths
from core.paths import JWW_KIND_JA, _resolve_glb, detect_jww, load_local_paths, property_files

ALIAS_RE = re.compile(r"^[^\s\\/:*?\"<>|#\[\]{},&!%@`'=]{1,20}$")   # フォルダ名と YAML にそのまま使える短い名前
KIND_ORDER = ("plan", "plan_2f", "elevation", "section")


def _where(path: Path | None) -> Path:
    return path or _paths.LOCAL_PATHS          # 呼ばれたときに決める（テストで置き場所を変えられるように）


def _conf(path: Path) -> dict:
    return load_local_paths(path) if path.exists() else {}


def next_alias(path: Path | None = None) -> str:
    """まだ使っていない仮名（物件A〜物件Z、その先は 物件27〜）。"""
    used = set(_conf(_where(path)))
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if f"物件{c}" not in used:
            return f"物件{c}"
    n = 27
    while f"物件{n}" in used:
        n += 1
    return f"物件{n}"


def describe_glb(p: Path) -> tuple[bool, str]:
    """glb の場所（ファイルかフォルダ）を見て、使えるかと説明（ファイル名は書かない）。"""
    if not p.is_absolute():
        return False, "場所は C:\\ などから始まる形で入れてください"
    if not p.exists():
        return False, "見つかりません"
    if p.is_file():
        return (True, "glb のファイル") if p.suffix.lower() == ".glb" else (False, "glb のファイルではありません")
    glb, why = _resolve_glb(p)
    return (True, "フォルダの中の glb（1つ）") if glb else (False, why or "glb がありません")


def describe_drawings(p: Path) -> tuple[bool, str]:
    """図面のフォルダを見て、使えるかと、名前から分かった図面の種類（ファイル名は書かない）。"""
    if not p.is_absolute() or not p.is_dir():
        return False, "フォルダが見つかりません"
    jww, notes = detect_jww(p)
    kinds = [JWW_KIND_JA[k] for k in KIND_ORDER if k in jww]
    text = "図面：" + ("・".join(kinds) if kinds else "名前から種類が分かる jww がありません（なくても登録できます）")
    return True, text + ("。" + "／".join(notes) if notes else "")


def list_properties(path: Path | None = None) -> dict:
    path = _where(path)
    conf = _conf(path)
    rows = []
    for alias, entry in conf.items():
        if not isinstance(entry, dict):
            continue
        f = property_files(alias, path)
        rows.append({"alias": alias,
                     "glb": "あり" if f.glb is not None and f.glb.exists() else (f.notes[0] if f.notes else "見つかりません"),
                     "drawings": [JWW_KIND_JA[k] for k in KIND_ORDER if k in f.jww],
                     "ng_words": len(entry.get("ng_words") or [])})
    return {"properties": rows, "next_alias": next_alias(path), "global_ng_words": len(conf.get("ng_words") or [])}


def register(alias: str, glb: str, drawings: str | None, ng_words: list[str], path: Path | None = None) -> str:
    """物件を1つ書き足す。だめなときは ValueError（場所やファイル名は文に入れない）。"""
    path = _where(path)
    alias = (alias or "").strip()
    if not ALIAS_RE.match(alias):
        raise ValueError("仮名は 20 文字以内で、空白や記号（\\ / : * ? \" < > | # など）を入れないでください")
    conf = _conf(path)
    if alias in conf:
        raise ValueError(f"「{alias}」はもう登録されています")
    gp = Path(glb.strip().strip('"'))
    ok, why = describe_glb(gp)
    if not ok:
        raise ValueError(f"glb：{why}")
    entry: dict = {"glb": str(gp)}
    if drawings and drawings.strip():
        dp = Path(drawings.strip().strip('"'))
        ok, why = describe_drawings(dp)
        if not ok:
            raise ValueError(f"図面：{why}")
        entry["jww_folder"] = str(dp)
    words = [w.strip() for w in ng_words if w and w.strip()]
    if words:
        entry["ng_words"] = words
    raw = path.read_bytes() if path.exists() else b""
    nl = "\r\n" if b"\r\n" in raw else "\n"
    block = "# 画面の「物件の登録」で足した" + nl + yaml.safe_dump({alias: entry}, allow_unicode=True, sort_keys=False).replace("\n", nl)
    sep = nl if raw and not raw.endswith(b"\n") else ""
    after = yaml.safe_load(raw.decode("utf-8") + sep + block) or {}
    if not isinstance(after.get(alias), dict) or Path(after[alias]["glb"]) != gp:
        raise ValueError("登録の書き込みを確かめられませんでした（local_paths.yaml の書き方を確かめてください）")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(sep + block)
    return alias


PICK_DIALOGS = {
    "glb": "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Filter = 'glb (*.glb)|*.glb'; $d.Title = 'Walk in home の glb を選ぶ'",
    "folder": "$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = '図面（jww）のフォルダを選ぶ'",
}


def pick(what: str) -> str | None:
    """Windows の「選ぶ」窓を出して、選んだ場所を返す（やめたら None）。PowerShell を使う（exe に部品を足さない）。"""
    script = ("Add-Type -AssemblyName System.Windows.Forms; [Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "$o = New-Object System.Windows.Forms.Form; $o.TopMost = $true; " + PICK_DIALOGS[what] + "; "
              "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { "
              "if ($d -is [System.Windows.Forms.OpenFileDialog]) { $d.FileName } else { $d.SelectedPath } }")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, timeout=900, creationflags=flags)
    out = r.stdout.decode("utf-8", errors="replace").strip()
    return out or None
