"""出力に「出してはいけない言葉」（data/local_paths.yaml の ng_words）が混ざっていないかの検査（SPEC §2-2）。"""
from __future__ import annotations

from core.paths import LOCAL_PATHS, load_local_paths


def ng_words() -> list[str]:
    if not LOCAL_PATHS.exists():
        return []
    return [str(w) for w in (load_local_paths().get("ng_words") or []) if str(w).strip()]


class PrivacyError(RuntimeError):
    pass


def assert_clean(text: str, what: str, words: list[str] | None = None) -> None:
    """text に ng_words が入っていたら止める。言葉そのものはエラーに出さない。"""
    for i, w in enumerate(ng_words() if words is None else words, 1):
        if w in text:
            raise PrivacyError(f"{what} に出してはいけない言葉（ng_words の {i} 番目）が入っていたため、書き出しを止めました。")
