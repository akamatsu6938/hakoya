"""出力に「出してはいけない言葉」（data/local_paths.yaml の ng_words）が混ざっていないかの検査（SPEC §2-2）。"""
from __future__ import annotations

from pathlib import Path

from core.paths import LOCAL_PATHS, load_local_paths


def ng_words(path: Path | None = None) -> list[str]:
    """全体の ng_words と、物件ごとの ng_words（画面の「物件の登録」で書く）を合わせたもの。"""
    p = path or LOCAL_PATHS
    if not p.exists():
        return []
    conf = load_local_paths(p)
    words = list(conf.get("ng_words") or [])
    for v in conf.values():
        if isinstance(v, dict):
            words += list(v.get("ng_words") or [])
    return [str(w) for w in words if str(w).strip()]


class PrivacyError(RuntimeError):
    pass


def assert_clean(text: str, what: str, words: list[str] | None = None) -> None:
    """text に ng_words が入っていたら止める。言葉そのものはエラーに出さない。"""
    for i, w in enumerate(ng_words() if words is None else words, 1):
        if w in text:
            raise PrivacyError(f"{what} に出してはいけない言葉（ng_words の {i} 番目）が入っていたため、書き出しを止めました。")
