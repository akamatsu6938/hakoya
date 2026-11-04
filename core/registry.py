"""物件の登録（画面の「設定」→「物件の登録」）。data/local_paths.yaml の最後に、仮名・glb の場所・図面のフォルダ・
出してはいけない言葉を書き足す（README の手順5を、YAML を手で書かずにできるようにする）。

- 元のファイルは読むだけ（コピーもしない。組み立てのときに data/cache へ写す：core/paths.py）。
- 画面に返すのは、仮名と、見つかったものの種類・数だけ。場所（パス）と元のファイル名（施主名が入ることがある）は返さない。
- 書き足すだけで、いまの中身（作者のコメントや並び）は書き換えない。書く前に、足したあとの全体が読めるか確かめる。
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

from core import paths as _paths
from core.paths import JWW_KIND_JA, _resolve_glb, detect_jww, load_local_paths, parse_local_paths, property_files
from core.privacy import clean_words

ALIAS_RE = re.compile(r"^[^\s\\/:*?\"<>|#\[\]{},&!%@`'=]{1,20}$")   # フォルダ名と YAML にそのまま使える短い名前
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LEFTOVER_FILES = ("building.json", "overrides.json", "findings.json")   # 前の家のデータ（作った形・人の修正・指摘）
NUMBERS_ONLY = re.compile(r"[0-9０-９\s\-－ー.,、・/／()（）]+")
KIND_ORDER = ("plan", "plan_2f", "elevation", "section")
_WRITE_LOCK = threading.Lock()              # 同じ仮名を2回同時に書かないように（「登録する」の連打など）


def _where(path: Path | None) -> Path:
    return path or _paths.LOCAL_PATHS          # 呼ばれたときに決める（テストで置き場所を変えられるように）


def _conf(path: Path) -> dict:
    return load_local_paths(path) if path.exists() else {}


def _leftover(alias: str, path: Path) -> bool:
    """data/out/<仮名> に前の家のデータが残っているか（手で消した仮名を別の家に使うと、修正や番号が新しい家に当たる）。"""
    d = path.parent / "out" / alias
    return any((d / n).exists() for n in LEFTOVER_FILES)


def next_alias(path: Path | None = None) -> str:
    """まだ使っていない仮名（物件A〜物件Z、その先は 物件27〜）。前の家のデータが残っている仮名も飛ばす。"""
    path = _where(path)
    used = set(_conf(path))
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if f"物件{c}" not in used and not _leftover(f"物件{c}", path):
            return f"物件{c}"
    n = 27
    while f"物件{n}" in used or _leftover(f"物件{n}", path):
        n += 1
    return f"物件{n}"


def _check_alias_and_words(alias: str, words: list[str], conf: dict, path: Path) -> None:
    """仮名と出してはいけない言葉を、書く前に確かめる。★11/4 の見直し：仮名に言葉が入ると、画面に送るたびに見張りが止めて
    画面がまったく動かなくなる（書いたあとなので画面からは戻せない）。数字だけの言葉は模型の座標に当たって組み立てが止まる。"""
    from core.privacy import find_ng
    if alias in (".", "..") or alias.endswith(".") or alias.upper() in WIN_RESERVED:
        raise ValueError("仮名の最後に「.」を付けたり、Windows で使えない名前（CON・NUL など）にしたりしないでください")
    for w in words:
        if len(w) < 2:
            raise ValueError("出してはいけない言葉は2文字以上にしてください（1文字だと、ほかの文字にも当たって画面が止まります）")
        if NUMBERS_ONLY.fullmatch(w):
            raise ValueError("数字だけの言葉（番地だけ・郵便番号だけなど）は入れないでください（模型の数値に当たって組み立てが止まります）。"
                             "「○○町1-2-3」のように地名と一緒に入れてください")
    existing = clean_words(conf.get("ng_words"))
    for v in conf.values():
        if isinstance(v, dict):
            existing += clean_words(v.get("ng_words"))
    if find_ng(alias, words + existing) is not None:
        raise ValueError("仮名に、出してはいけない言葉が入っています（仮名は「物件G」のような名前にしてください）")
    if any(find_ng(a, words) is not None for a, v in conf.items() if isinstance(v, dict)):
        raise ValueError("出してはいけない言葉が、登録済みの仮名に当たります（言葉を長くしてください）")
    if _leftover(alias, path):
        raise ValueError(f"「{alias}」の前のデータ（data/out/{alias}）が残っています。別の仮名にしてください")


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
        if entry.get("glb"):        # glb の欄には glb のことだけ（前は図面の注意が出ることがあった）
            gp, why = _resolve_glb(Path(str(entry["glb"])))
            glb_state = "あり" if gp is not None and gp.exists() else (why or "見つかりません")
        else:
            glb_state = "書かれていません"
        rows.append({"alias": alias, "glb": glb_state,
                     "drawings": [JWW_KIND_JA[k] for k in KIND_ORDER if k in f.jww],
                     "ng_words": len(clean_words(entry.get("ng_words")))})   # 空の行は数えない（数で打ち間違いに気づけるように）
    return {"properties": rows, "next_alias": next_alias(path), "global_ng_words": len(clean_words(conf.get("ng_words")))}


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
    _check_alias_and_words(alias, words, conf, path)
    if words:
        entry["ng_words"] = words
    with _WRITE_LOCK:
        raw = path.read_bytes() if path.exists() else b""
        if alias in (parse_local_paths(raw.decode("utf-8")) if raw.strip() else {}):   # 書く直前の中身でもう一度
            raise ValueError(f"「{alias}」はもう登録されています")
        nl = "\r\n" if b"\r\n" in raw else "\n"
        block = "# 画面の「物件の登録」で足した" + nl + yaml.safe_dump({alias: entry}, allow_unicode=True, sort_keys=False).replace("\n", nl)
        sep = nl if raw and not raw.endswith(b"\n") else ""
        after = parse_local_paths(raw.decode("utf-8") + sep + block)
        if not isinstance(after.get(alias), dict) or Path(after[alias]["glb"]) != gp:
            raise ValueError("登録の書き込みを確かめられませんでした（local_paths.yaml の書き方を確かめてください）")
        # いまの中身はそのまま（バイト列で）残し、後ろに足したものを別のファイルに書いてから入れ替える（途中で落ちても壊れない）
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".local_paths.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw + (sep + block).encode("utf-8"))
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise ValueError(f"local_paths.yaml に書けませんでした（{type(e).__name__}。ほかのソフトで開いていたら閉じてください）") from None
    return alias


PICK_DIALOGS = {
    "glb": "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Filter = 'glb (*.glb)|*.glb'; $d.Title = 'Walk in home の glb を選ぶ'",
    "folder": "$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = '図面（jww）のフォルダを選ぶ'",
}


PICK_SCRIPT = ("Add-Type -AssemblyName System.Windows.Forms; "
               "$o = New-Object System.Windows.Forms.Form; $o.TopMost = $true; {dialog}; "
               "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) {{ "
               "$p = if ($d -is [System.Windows.Forms.OpenFileDialog]) {{ $d.FileName }} else {{ $d.SelectedPath }}; "
               "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($p)) }}")


def pick(what: str) -> str | None:
    """Windows の「選ぶ」窓を出して、選んだ場所を返す（やめたら None、窓を出せなければ RuntimeError）。
    PowerShell を使う（exe に部品を足さない）。★11/4 の見直し：場所は Base64（英数字だけ）で受け取る（日本語のパスが
    文字コードの違いで化けない）。PowerShell の失敗を「やめた」と取り違えない。"""
    script = PICK_SCRIPT.format(dialog=PICK_DIALOGS[what])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                       capture_output=True, timeout=900, creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError(f"PowerShell が止まりました（終了コード {r.returncode}）")
    out = r.stdout.decode("ascii", errors="ignore").strip()
    if not out:
        return None
    try:
        return base64.b64decode(out, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("選んだ場所を受け取れませんでした") from None
