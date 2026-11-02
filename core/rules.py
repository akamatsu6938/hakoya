"""会社ルールの数値（rules/<会社>.yaml）の読み込み（SPEC §5）。"""
from __future__ import annotations

from pathlib import Path

import yaml

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"
DEFAULT_COMPANY = "jisha"


def load_rules(company: str = DEFAULT_COMPANY) -> dict:
    path = RULES_DIR / f"{company}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"会社ルール rules/{company}.yaml がありません。")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def rule(rules: dict, dotted: str):
    """「walls.exterior_thickness_mm」の形で値を引く。"""
    cur = rules
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(f"会社ルールに {dotted} がありません。")
        cur = cur[key]
    return cur
