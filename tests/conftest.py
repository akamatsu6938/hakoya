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
# 起動のしかた（core/paths.py の MODE）は自社用に決める（見本用の黒い画面から試験を走らせても、data/ の実データの試験が同じに動くように）。
# 起動のしかたの決め方そのものは tests/test_views.py が環境変数を変えて確かめる
os.environ["HAKOYA_MODE"] = "own"

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


@pytest.fixture(autouse=True)
def _plan_connection_like_the_author_pc(monkeypatch):
    """自社の会社ルールがない版（公開リポジトリ）では、アプリはプランの接続を出さない（★11/6）。プランの接続を使う流れの
    テストが公開の版でも同じに動くよう、テストの中では作者の手元と同じ扱いにする。出さないことは test_views.py が
    own_rules_present を差し替えて確かめる。jisha.yaml があるときは何も変えない。"""
    if not JISHA_RULES.exists():
        from core import settings_store
        monkeypatch.setattr(settings_store, "own_rules_present", lambda: True)


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
