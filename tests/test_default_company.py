"""既定の会社ルール：rules/jisha.yaml があれば jisha（今までどおり）、ない（公開の版）ときは B工務店（架空）。

本物の rules/ は使わず、一時フォルダに会社ルールを置いて core.rules.RULES_DIR を差し替える（本物の jisha.yaml は消さない）。
"""
from __future__ import annotations

import shutil

import keyring
import pytest

from core import paths, rules, settings_store


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    """B工務店（架空）と AI の設定だけがある rules/（jisha はない）。"""
    d = tmp_path / "rules"
    d.mkdir()
    shutil.copy(rules.RULES_DIR / "bkoumuten.yaml", d / "bkoumuten.yaml")
    shutil.copy(rules.RULES_DIR / "ai_models.yaml", d / "ai_models.yaml")      # 会社の決まりではない（walls がない）
    monkeypatch.setattr(rules, "RULES_DIR", d)
    monkeypatch.setattr(rules, "LEARNED_DIR", tmp_path / "learned")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data")
    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))   # 本物の資格情報マネージャーは使わない
    return d


def _fake_jisha(d):
    """中身は B工務店の写し（本物の jisha の値は読まない）。あるかどうかだけを見る。"""
    (d / "jisha.yaml").write_text((d / "bkoumuten.yaml").read_text(encoding="utf-8"), encoding="utf-8")


def test_without_jisha_the_first_company_is_the_default(rules_dir):
    assert rules.default_company() == "bkoumuten"
    assert rules.load_rules()["name_ja"] == "B工務店（架空）"
    assert settings_store.load()["company"] == "bkoumuten"
    (paths.DATA_DIR).mkdir(parents=True)
    (paths.DATA_DIR / "settings.json").write_text('{"company": "jisha"}', encoding="utf-8")
    assert settings_store.load()["company"] == "bkoumuten"                    # 書いてあっても、ない会社なら既定に戻す


def test_with_jisha_the_default_stays_jisha(rules_dir):
    _fake_jisha(rules_dir)
    assert rules.default_company() == "jisha"
    assert settings_store.load()["company"] == "jisha"


def test_names_sorted_and_non_company_files_skipped(rules_dir):
    (rules_dir / "aaa_notes.yaml").write_text("memo: 会社の決まりではない\n", encoding="utf-8")
    (rules_dir / "broken.yaml").write_text("walls: [\n", encoding="utf-8")       # 壊れた yaml は飛ばす
    (rules_dir / "zz_company.yaml").write_text((rules_dir / "bkoumuten.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    assert rules.default_company() == "bkoumuten"


def test_building_without_company_uses_the_default(rules_dir):
    from core.overrides import _company_rules
    assert _company_rules({"source": {}})["name_ja"] == "B工務店（架空）"
    with pytest.raises(FileNotFoundError):
        rules.load_rules("nai")                                                # 名前を書いたときは、今までどおり止まる


def test_settings_screen_tells_the_default(rules_dir):
    from fastapi.testclient import TestClient

    from app.server import app
    st = TestClient(app).get("/api/settings").json()
    assert st["company"] == "bkoumuten" and st["default_company"] == "bkoumuten"
    assert [c["code"] for c in st["companies"]] == ["bkoumuten"]
