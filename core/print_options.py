"""印刷の選び方（物件ごと）：外壁をざらざら（Bambu Studio のファジースキン）にするか（★2026-11-06 作者）。

作者：「ファジースキンを適用するかしないかを決めれるようにして、印刷も区別するようにしよう」。
- ざらざらの部品の形（外壁の表面の層）は、いつも作る（core/builder.py）。ここで決めるのは、3MF の外壁の部品に
  ファジースキンの設定を書くかどうかと、案内の文・検査だけ（形の計算は変えない）。
- 物件ごとに data/out/<物件>/print_options.json（{"fuzzy_skin": true|false}）。印刷タブの「外壁の仕上げ」で決める。
- 決めていなければ会社ルール（model.exterior_finish が plaster＝塗り壁ならざらざら。サイディングなら付けない）。
- 分け目の print_split.json とは別のファイル（あちらは消して自動に戻すので、相乗りしない）。
"""
from __future__ import annotations

import json
from pathlib import Path

FILE = "print_options.json"


def _path(alias: str) -> Path:
    from core.paths import out_dir
    return out_dir(alias) / FILE


def _load(alias: str) -> dict:
    p = _path(alias)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(alias: str, data: dict) -> None:
    from core.paths import write_text_atomic
    p = _path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(p, json.dumps(data, ensure_ascii=False, indent=1))   # 書きかけのファイルを残さない


def company_default(rules: dict) -> bool:
    """会社ルールの既定：外壁が塗り壁（model.exterior_finish が plaster）ならざらざらにする。"""
    return (rules.get("model") or {}).get("exterior_finish") == "plaster"


def saved_fuzzy(alias: str) -> bool | None:
    """物件で決めた値（決めていなければ None）。"""
    v = _load(alias).get("fuzzy_skin")
    return v if isinstance(v, bool) else None


def effective_fuzzy(alias: str, rules: dict) -> tuple[bool, str]:
    """外壁をざらざらにするか、と、その出どころ（"property"：物件で決めた／"company"：会社ルールの既定）。"""
    v = saved_fuzzy(alias)
    if v is not None:
        return v, "property"
    return company_default(rules), "company"


def set_fuzzy(alias: str, on: bool) -> None:
    data = _load(alias)
    data["fuzzy_skin"] = bool(on)
    _save(alias, data)


def clear(alias: str) -> None:
    """物件で決めた値をやめて、会社ルールの既定に戻す。"""
    data = _load(alias)
    data.pop("fuzzy_skin", None)
    p = _path(alias)
    if data:
        _save(alias, data)
    elif p.exists():
        p.unlink()
