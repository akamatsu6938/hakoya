"""社外用の版（SPEC §7-4・§10）：Claude のプランの接続を使えないこと、exe の中の印が環境変数より優先されること、
作った exe を調べる道具が入れてはいけないものを見つけること。"""
from __future__ import annotations

from urllib.parse import quote

import pytest

from core import paths, settings_store
from tools.check_external_build import check


def test_edition_mark_in_bundle_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("HAKOYA_EDITION", "self")
    assert settings_store.edition() == "self"                                  # 印がなければ環境変数（開発中）
    (tmp_path / settings_store.EDITION_FILE).write_text("external\n", encoding="utf-8")
    assert settings_store.edition() == "external"                              # exe の中の印が優先
    assert "claude_code" not in settings_store.available_connections()
    with pytest.raises(ValueError, match="この版では"):
        settings_store.check_connection("claude_code")
    settings_store.check_connection("api_key")                                 # API キーと AI なしは使える
    settings_store.check_connection("off")


@pytest.mark.realdata
def test_server_refuses_plan_connection_in_external_edition(monkeypatch):
    from fastapi.testclient import TestClient
    from app.server import app
    monkeypatch.setattr(settings_store, "available_connections", lambda: ["api_key", "off", "none"])
    c = TestClient(app)
    a = quote("物件A")
    r = c.post(f"/api/inspect/{a}?connection=claude_code")
    assert r.status_code == 400 and "この版では" in r.json()["detail"]
    r = c.post(f"/api/lasso/{a}/ask", json={"polygon": [[0, 0], [1, 0], [1, 1]], "text": "x", "connection": "claude_code"})
    assert r.status_code == 400 and "この版では" in r.json()["detail"]
    r = c.post("/api/rules/jisha/draft", json={"connection": "claude_code", "text": ""})
    assert r.status_code == 400 and "この版では" in r.json()["detail"]


def _bundle(tmp_path, edition="external"):
    root = tmp_path / "HAKOYA_external"
    (root / "_internal" / "anthropic").mkdir(parents=True)
    (root / "_internal" / "anthropic" / "x.py").write_text("print('ok')", encoding="utf-8")
    (root / "_internal" / "EDITION").write_text(edition, encoding="utf-8")
    (root / "HAKOYA_external.exe").write_bytes(b"MZ" + b"\0" * 100)
    (root / "_internal" / "pdfminer" / "cmap").mkdir(parents=True)                     # PDF の日本語の文字の表
    (root / "_internal" / "pdfminer" / "cmap" / "UniJIS-UCS2-H.json.gz").write_bytes(b"x")
    return root


def test_build_check_passes_clean_bundle(tmp_path):
    assert check(_bundle(tmp_path)) == []


def test_build_check_finds_forbidden_things(tmp_path):
    root = _bundle(tmp_path, edition="self")
    (root / "_internal" / "claude_agent_sdk" / "_bundled").mkdir(parents=True)
    (root / "_internal" / "claude_agent_sdk" / "_bundled" / "claude.exe").write_bytes(b"MZ")
    (root / "data").mkdir()
    (root / "data" / "local_paths.yaml").write_text("x", encoding="utf-8")
    fake_key = "sk-" + "ant-" + "a1" * 20                                     # 架空のキー（リポジトリにキーの形の文字を置かない）
    (root / "_internal" / "anthropic" / "y.json").write_text(fake_key, encoding="utf-8")
    (root / "_internal" / "pdfminer" / "cmap" / "UniJIS-UCS2-H.json.gz").unlink()
    problems = " / ".join(check(root))
    for word in ("claude_agent_sdk", "Claude Code 本体", "data フォルダ", "API キーらしい文字", "external でない", "日本語の文字の表"):
        assert word in problems, word
    assert fake_key not in problems                                           # 見つけた中身は出さない


def test_properties_empty_when_no_local_paths(monkeypatch):
    """exe を初めて起動した PC（data/local_paths.yaml がない）でも、物件の一覧はエラーにならず空になる。"""
    from fastapi.testclient import TestClient
    import core.paths
    from app.server import app

    def missing():
        raise FileNotFoundError("data/local_paths.yaml がありません")
    monkeypatch.setattr(core.paths, "aliases", missing)
    c = TestClient(app)
    assert c.get("/api/properties").json() == []
    assert c.post("/api/inspect/x?connection=off").status_code == 404
