"""高性能読み込み（図面と突き合わせる読み込み）の入れ切り（★2026-11-04 作者）。

作者：「最終的なゴールは glb ファイルだけでの読み込み。高性能読み込みの場合のみ、jww を付けるっていう形にしようか。
jww を pdf にすれば文字化けすることないから、pdf（OCR 可）みたいにしとこうか」。
- ふだん（切）：glb だけで組み立て・検査・検品する（図面は読まない）。
- 高性能読み込み（入）：登録した図面（jww か、文字を選べる PDF）も読み、平面図の重ね・窓の注記の照合・図面ありの検品に使う。
- 入れ切りは物件ごとに data/out/<物件>/reading.json に置く。ないときは「図面を登録した物件は入」（前と同じ動き）。
- PDF の縮尺が図面から分からなかったときに、平面図の位置合わせで決めた縮尺もここに控える（ファイルの大きさと日時が変われば使わない）。
"""
from __future__ import annotations

import json
from pathlib import Path

FILE = "reading.json"


def _path(alias: str) -> Path:
    from core.paths import out_dir
    return out_dir(alias) / FILE


def _load(alias: str) -> dict:
    p = _path(alias)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _save(alias: str, data: dict) -> None:
    p = _path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)                                     # 書きかけのファイルを残さない


def is_high(alias: str, has_drawings: bool) -> bool:
    """高性能読み込みが入か。決めていなければ、図面を登録した物件は入（前と同じ動き）。"""
    v = _load(alias).get("high")
    return bool(has_drawings) if v is None else bool(v)


def set_high(alias: str, on: bool) -> None:
    data = _load(alias)
    data["high"] = bool(on)
    _save(alias, data)


def _file_key(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"         # 場所や名前（施主名が入ることがある）は控えない


def saved_scale(alias: str, kind: str, p: Path) -> float | None:
    """位置合わせで決めた PDF の縮尺（そのファイルのまま変わっていなければ）。"""
    rec = (_load(alias).get("pdf_scale") or {}).get(kind)
    if not rec:
        return None
    try:
        return float(rec["scale"]) if rec.get("key") == _file_key(p) else None
    except (OSError, KeyError, ValueError):
        return None


def save_scale(alias: str, kind: str, p: Path, scale: float, how: str) -> None:
    data = _load(alias)
    data.setdefault("pdf_scale", {})[kind] = {"scale": float(scale), "key": _file_key(p), "how": how}
    _save(alias, data)
