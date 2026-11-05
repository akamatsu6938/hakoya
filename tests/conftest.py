"""テスト共通の設定。実データ（data/local_paths.yaml とその先の glb）がなければ realdata のテストを飛ばす。

自社の会社ルール（rules/jisha.yaml）は公開の版に入れない。ないときは、jisha の決まりを読もうとしたテスト
（load_rules("jisha")・base_text("jisha")）と、印 jisha を付けたテストを飛ばす。jisha.yaml があるときは何も変えない。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 人が承認した「学んだルール」はテストでは使わない（結果が承認で変わらないように）。読み込みより先に決める
os.environ.setdefault("HAKOYA_LEARNED_RULES", "off")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCAL_PATHS = REPO_ROOT / "data" / "local_paths.yaml"
JISHA_RULES = REPO_ROOT / "rules" / "jisha.yaml"
NO_JISHA = "自社の会社ルール（公開しない）がない環境"


def _skip_jisha(fn):
    """会社が jisha なら、そのテストを飛ばす（モジュールの読み込み中でも飛ばせるように allow_module_level）。"""
    def wrapper(company, *args, **kwargs):
        if company == "jisha":
            pytest.skip(NO_JISHA, allow_module_level=True)
        return fn(company, *args, **kwargs)
    wrapper.__wrapped__ = fn
    return wrapper


if not JISHA_RULES.exists():
    # load_rules は中で _base を名前で引くので、_base を包めば load_rules("jisha") も飛ぶ（先に import した所も含めて）。
    # テストのモジュールより先に読まれるので、あとから import する所は包んだ base_text を受け取る
    from core import rules as _rules
    _rules._base = _skip_jisha(_rules._base)
    _rules.base_text = _skip_jisha(_rules.base_text)


def pytest_configure(config):
    config.addinivalue_line("markers", "jisha: 自社の会社ルール（rules/jisha.yaml）が要るテスト（なければ飛ばす）")


def pytest_collection_modifyitems(config, items):
    if not JISHA_RULES.exists():
        skip_jisha = pytest.mark.skip(reason=NO_JISHA)
        for item in items:
            if item.get_closest_marker("jisha"):
                item.add_marker(skip_jisha)
    if LOCAL_PATHS.exists():
        return
    skip = pytest.mark.skip(reason="data/local_paths.yaml がないため実データのテストを飛ばします")
    for item in items:
        if "realdata" in item.keywords:
            item.add_marker(skip)
