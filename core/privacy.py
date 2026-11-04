"""出力に「出してはいけない言葉」（data/local_paths.yaml の ng_words）が混ざっていないかの検査（SPEC §2-2）。"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from core.paths import LOCAL_PATHS, load_local_paths


def clean_words(items) -> list[str]:
    """YAML の言葉の並びを文字の並びにする。空の行（`- ` だけ）は捨てる。
    ★11/4：空の行は None になり、str(None) で「None」という言葉になって、コードの None に当たっていた。"""
    return [str(w) for w in (items or []) if w is not None and str(w).strip()]


def ng_words(path: Path | None = None) -> list[str]:
    """全体の ng_words と、物件ごとの ng_words（画面の「物件の登録」で書く）を合わせたもの。"""
    p = path or LOCAL_PATHS
    if not p.exists():
        return []
    conf = load_local_paths(p)
    words = clean_words(conf.get("ng_words"))
    for v in conf.values():
        if isinstance(v, dict):
            words += clean_words(v.get("ng_words"))
    return words


class PrivacyError(RuntimeError):
    pass


def _norm(s: str) -> str:
    """全角・半角と空白の違いをなくした形（「山田 太郎」と「山田太郎」、全角の数字と半角の数字を同じに見る）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def find_ng(text: str, words: list[str]) -> int | None:
    """text に入っている ng_words の番号（1 から）。なければ None。そのままの形と、全角・半角と空白をそろえた形の両方で探す。
    ★11/4 の見直し：前はそのままの形だけで、「山田 太郎」と書いた言葉が「山田太郎様」に当たらなかった。"""
    normed = None
    for i, w in enumerate(words, 1):
        if w in text:
            return i
        nw = _norm(w)
        if nw:
            normed = _norm(text) if normed is None else normed
            if nw in normed:
                return i
    return None


def assert_clean(text: str, what: str, words: list[str] | None = None) -> None:
    """text に ng_words が入っていたら止める。言葉そのものはエラーに出さない。"""
    i = find_ng(text, ng_words() if words is None else words)
    if i is not None:
        raise PrivacyError(f"{what} に出してはいけない言葉（ng_words の {i} 番目）が入っていたため、書き出しを止めました。")
