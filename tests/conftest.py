"""テスト共通の設定。実データ（data/local_paths.yaml とその先の glb）がなければ realdata のテストを飛ばす。"""
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


def pytest_collection_modifyitems(config, items):
    if LOCAL_PATHS.exists():
        return
    skip = pytest.mark.skip(reason="data/local_paths.yaml がないため実データのテストを飛ばします")
    for item in items:
        if "realdata" in item.keywords:
            item.add_marker(skip)
