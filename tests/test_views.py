"""起動のしかた（★11/5）：自社用（own）・他社用（other）・見本用（sample）。起動したときに決まり、画面では切り替えない。
起動のしかたごとにデータの置き場所が別（data/・data/other/・data/sample/）。実データは使わない（物件の一覧は差し替える）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import keyring
import pytest

from core import paths
from core import settings_store as ss
from core.rules import DEFAULT_COMPANY, RULES_DIR

# 自社の会社ルールは公開の版に入れない。それを選ぶテストは、ないときは飛ばす
needs_own_rules = pytest.mark.skipif(not (RULES_DIR / f"{DEFAULT_COMPANY}.yaml").exists(),
                                     reason="自社の会社ルール（公開しない）がない環境")
NAMES = ["物件A", "デモ用", "見本の家", "試験の家"]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "MODE", "own")
    monkeypatch.setattr(paths, "REPO_ROOT", paths.REPO_ROOT)       # 印（EDITION）のない場所のまま
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))   # 本物の資格情報マネージャーは使わない
    return tmp_path


@pytest.fixture
def client(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    monkeypatch.setattr(server, "_aliases", lambda: list(NAMES))
    monkeypatch.setattr(server, "property_files", lambda a: paths.PropertyFiles(alias=a, glb=None, jww_folder=None))
    return TestClient(server.app)


# ---------------------------------------------------------------- 起動のしかたとデータの置き場所（core/paths.py）

def test_mode_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    for value, want in (("own", "own"), ("other", "other"), ("sample", "sample"), (" Sample ", "sample")):
        monkeypatch.setenv("HAKOYA_MODE", value)
        assert paths.launch_mode() == want
    monkeypatch.delenv("HAKOYA_MODE")
    assert paths.launch_mode() == paths.default_mode()                       # 書いていなければ既定
    monkeypatch.setenv("HAKOYA_MODE", "everything")
    with pytest.raises(ValueError, match="HAKOYA_MODE"):                       # 知らない値は黙って自社用にしない
        paths.launch_mode()


def test_external_edition_is_always_other(tmp_path, monkeypatch):
    """社外用の版（exe の中の印か HAKOYA_EDITION）は、HAKOYA_MODE に何を書いても他社用。"""
    monkeypatch.setenv("HAKOYA_EDITION", "external")
    for value in ("own", "sample", "", "everything"):
        monkeypatch.setenv("HAKOYA_MODE", value)
        assert paths.launch_mode() == "other" and paths.default_mode() == "other"
    monkeypatch.delenv("HAKOYA_EDITION")
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    (tmp_path / paths.EDITION_FILE).write_text("external\n", encoding="utf-8")   # exe の中の印
    monkeypatch.setenv("HAKOYA_MODE", "own")
    assert paths.launch_mode() == "other"
    monkeypatch.setattr(paths, "MODE", "own")                                 # 起動のあとで版が分かっても他社用
    assert ss.mode() == "other"


def test_public_edition_without_own_rules_starts_as_own(tmp_path, monkeypatch):
    """提出用の公開リポジトリ（自社の会社ルールがない）も、HAKOYA_MODE がなければ自社用で始まる（★11/6 作者。前は他社用）。
    会社ルールは B工務店（架空）で、会社ルールのタブ（学習）も使える。ただしプランの接続は、どの起動でも出さない。"""
    import core.rules
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "bkoumuten.yaml").write_text((RULES_DIR / "bkoumuten.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(core.rules, "RULES_DIR", rules)
    monkeypatch.setattr(ss, "own_rules_present", lambda: False)               # テスト共通の差し替えを外して、公開の版の扱い
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    monkeypatch.delenv("HAKOYA_MODE", raising=False)
    assert paths.default_mode() == "own" and paths.launch_mode() == "own"
    for m in ("own", "sample", "other"):
        assert "claude_code" not in ss._connections(m)                         # プランの接続はどの起動でも出さない
        assert ss._connections(m)[-3:] == ["api_key", "off", "none"]            # API キー・AI なしは使える
    monkeypatch.setattr(paths, "MODE", "own")
    with pytest.raises(ValueError, match="この版では"):
        ss.check_connection("claude_code")
    assert ss.features()["rules_tab"] is True and ss.features()["plan_connection"] is False


def test_each_mode_has_its_own_data_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("HAKOYA_DATA_DIR", raising=False)
    own = paths.REPO_ROOT / "data"
    assert paths.own_data_dir() == own
    assert paths.mode_data_dir("own") == own
    assert paths.mode_data_dir("other") == own / "other" and paths.mode_data_dir("sample") == own / "sample"
    monkeypatch.setattr(sys, "frozen", True, raising=False)                    # exe のときは exe の隣の data/
    monkeypatch.setattr(sys, "executable", str(tmp_path / "HAKOYA" / "HAKOYA.exe"))
    assert paths.mode_data_dir("own") == tmp_path / "HAKOYA" / "data"
    assert paths.mode_data_dir("sample") == tmp_path / "HAKOYA" / "data" / "sample"
    monkeypatch.setenv("HAKOYA_DATA_DIR", str(tmp_path / "elsewhere"))         # HAKOYA_DATA_DIR がいちばん強い
    assert paths.mode_data_dir("other") == tmp_path / "elsewhere"


@pytest.mark.parametrize("first", ["core.paths", "core.rules", "core.settings_store"])
def test_mode_is_decided_when_the_program_starts(first):
    """起動のしかたとデータの置き場所は、読み込んだときに決まる（どの部品から読み込んでも、互いを呼び合って止まらない）。"""
    env = dict(os.environ, HAKOYA_MODE="sample", PYTHONUTF8="1")
    env.pop("HAKOYA_DATA_DIR", None)
    env.pop("HAKOYA_EDITION", None)
    code = (f"import {first}\nimport core.paths as p, core.rules as r, core.settings_store as s\n"
            "print(p.MODE, p.DATA_DIR.relative_to(p.REPO_ROOT).as_posix(), r.learned_dir().relative_to(p.REPO_ROOT).as_posix(), "
            "s.mode())")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(paths.REPO_ROOT), env=env, capture_output=True,
                         text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.split() == ["sample", "data/sample", "data/sample/rules", "sample"]


# ---------------------------------------------------------------- 設定（core/settings_store.py）

def test_old_view_in_settings_is_ignored(data_dir):
    """前の版の「見え方」（view）が settings.json に残っていても、読んで困らない（使わない）。"""
    (data_dir / "settings.json").write_text('{"view": "sample", "auto_inspect": "off", "company": "bkoumuten"}',
                                            encoding="utf-8")
    st = ss.load()
    assert "view" not in st and st["company"] == "bkoumuten" and ss.mode() == "own"
    ss.set_company("bkoumuten")
    assert "view" not in json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))


@needs_own_rules
def test_other_mode_hides_plan_and_defaults_to_b_koumuten(data_dir, monkeypatch):
    ss.set_auto_inspect("claude_code")
    monkeypatch.setattr(paths, "MODE", "other")
    st = ss.load()
    assert st["company"] == "bkoumuten"                                       # 選んでいなければ B工務店（架空）
    assert "claude_code" not in ss.available_connections() and st["auto_inspect"] == "off"
    with pytest.raises(ValueError, match="他社用"):
        ss.check_connection("claude_code")
    with pytest.raises(ValueError, match="他社用"):
        ss.set_auto_inspect("claude_code")
    ss.check_connection("api_key")
    assert ss.set_company(DEFAULT_COMPANY)["company"] == DEFAULT_COMPANY      # 選んだら、その会社のまま
    monkeypatch.setattr(paths, "MODE", "own")
    st = ss.load()
    assert st["auto_inspect"] == "claude_code"                                # 自社用なら、選んでいた検品のしかたが使える
    assert st["company"] == DEFAULT_COMPANY


def test_features_follow_the_mode(data_dir, monkeypatch):
    plan = "claude_code" in ss._connections("own")                            # Agent SDK が入っているか
    want = {"own": {"import": True, "rules_tab": True, "plan_connection": plan},
            "other": {"import": True, "rules_tab": False, "plan_connection": False},
            "sample": {"import": False, "rules_tab": True, "plan_connection": plan}}
    for m, f in want.items():
        monkeypatch.setattr(paths, "MODE", m)
        assert ss.mode() == m and ss.features() == f
    assert ss.mode_label("own") == "自社用" and ss.mode_label("other") == "他社用" and ss.mode_label("sample") == "見本用"


def test_sample_aliases():
    assert ss.is_sample_alias("デモ用") and ss.is_sample_alias("見本の家") and ss.is_sample_alias("見本")
    assert not ss.is_sample_alias("物件A") and not ss.is_sample_alias("試験の見本")


def test_last_alias_and_recent(data_dir):
    assert ss.load()["last_alias"] is None and ss.load()["recent"] == []
    for a in ("物件A", "物件B", "物件C", "物件D", "物件E", "物件F", "物件B"):
        res = ss.set_last_alias(a)
    assert res == {"last_alias": "物件B", "recent": ["物件B", "物件F", "物件E", "物件D", "物件C"]}   # 新しい順に5つまで
    assert ss.set_last_alias("物件C", known=["物件C", "物件E"])["recent"] == ["物件C", "物件E"]    # なくなった物件は外す
    (data_dir / "settings.json").write_text('{"last_alias": 3, "recent": "x"}', encoding="utf-8")
    assert ss.load()["last_alias"] is None and ss.load()["recent"] == []     # 変な値は無いものとして読む


# ---------------------------------------------------------------- 画面の口（app/server.py）

def test_settings_endpoint_reports_the_mode(client, monkeypatch):
    for m, label in (("own", "自社用"), ("other", "他社用"), ("sample", "見本用")):
        monkeypatch.setattr(paths, "MODE", m)
        st = client.get("/api/settings").json()
        assert st["mode"] == m and st["mode_label"] == label and st["view"] == m and st["views"] == [m]
        assert st["features"] == ss.features()
    monkeypatch.setattr(paths, "MODE", "other")
    st = client.get("/api/settings").json()
    assert "claude_code" not in st["connections"] and st["company"] == "bkoumuten" and st["company_chosen"] is False
    assert client.post("/api/inspect/x?connection=claude_code").status_code == 404   # 物件がない（先に物件を確かめる）


def test_view_cannot_be_switched_from_the_screen(client):
    r = client.post("/api/settings/view", json={"view": "other"})
    assert r.status_code == 400 and "起動" in r.json()["detail"]
    assert client.get("/api/settings").json()["mode"] == "own"                 # 変わらない


def test_external_edition_endpoint(client, monkeypatch):
    monkeypatch.setenv("HAKOYA_EDITION", "external")
    st = client.get("/api/settings").json()
    assert st["mode"] == "other" and st["views"] == ["other"] and st["edition"] == "external"
    assert st["features"]["plan_connection"] is False and "claude_code" not in st["connections"]


def test_sample_mode_lists_only_sample_houses(client, monkeypatch):
    assert [p["alias"] for p in client.get("/api/properties").json()] == NAMES        # 自社用：全部
    monkeypatch.setattr(paths, "MODE", "sample")
    assert [p["alias"] for p in client.get("/api/properties").json()] == ["デモ用", "見本の家"]
    monkeypatch.setattr(paths, "MODE", "other")
    assert len(client.get("/api/properties").json()) == 4                     # 他社用も全部


def test_sample_mode_refuses_registration(client, data_dir, monkeypatch):
    """見本用では物件を登録しない（画面で隠すだけにしない。data/sample/ は見本の家だけ）。"""
    monkeypatch.setattr(paths, "MODE", "sample")
    for path, body in (("/api/registry/pick", {"what": "glb"}), ("/api/registry", {"alias": "物件Z", "glb_path": "C:\\x.glb"})):
        r = client.post(path, json=body)
        assert r.status_code == 400 and "見本用" in r.json()["detail"]
    assert not (data_dir / "local_paths.yaml").exists()
    monkeypatch.setattr(paths, "MODE", "other")
    r = client.post("/api/registry", json={"alias": "物件Z", "glb_path": ""})
    assert r.status_code == 400 and "見本用" not in r.json()["detail"]          # 他社用は登録できる（ここでは glb がないので断る）


def test_last_alias_endpoint(client, data_dir, monkeypatch):
    r = client.post("/api/settings/last_alias", json={"alias": "試験の家"})
    assert r.status_code == 200 and r.json() == {"last_alias": "試験の家", "recent": ["試験の家"]}
    assert client.post("/api/settings/last_alias", json={"alias": "物件A"}).json()["recent"] == ["物件A", "試験の家"]
    st = client.get("/api/settings").json()
    assert st["last_alias"] == "物件A" and st["recent"] == ["物件A", "試験の家"]
    r = client.post("/api/settings/last_alias", json={"alias": "ない家"})
    assert r.status_code == 404 and "物件" in r.json()["detail"]
    monkeypatch.setattr(paths, "MODE", "sample")                              # 見本用：見本の家だけ（一覧にないものは出さない）
    st = client.get("/api/settings").json()
    assert st["last_alias"] is None and st["recent"] == []
    assert client.post("/api/settings/last_alias", json={"alias": "物件A"}).status_code == 404
    assert client.post("/api/settings/last_alias", json={"alias": "デモ用"}).json()["last_alias"] == "デモ用"


def test_findings_older_than_the_last_build_are_marked_stale(client, data_dir):
    """検品のあとに「読み込んで組み立てる」で作り直したら、見本用の手順の帯は「2 検品する」に戻る（stale）。
    承認は building.json を書かないので、承認のあとは stale にならない。"""
    import time
    from urllib.parse import quote
    d = data_dir / "out" / "デモ用"
    d.mkdir(parents=True)
    url = "/api/findings/" + quote("デモ用")
    assert client.get(url).json()["stale"] is False                            # まだ検品していない
    (d / "findings.json").write_text('{"findings": [], "code_findings": [], "connection": "off"}', encoding="utf-8")
    (d / "building.json").write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(d / "findings.json", (now - 100, now - 100))
    os.utime(d / "building.json", (now, now))
    assert client.get(url).json()["stale"] is True                             # 検品のあとに組み立て直した
    os.utime(d / "findings.json", (now + 5, now + 5))
    assert client.get(url).json()["stale"] is False                            # 検品し直した（承認で findings.json だけ書いても同じ）


def test_repo_launchers_set_the_mode():
    """起動のファイル：start.bat は今までどおり（自社用）、start_other.bat・start_sample.bat は HAKOYA_MODE を書く。"""
    root = Path(paths.REPO_ROOT)
    for name, mode in (("start_other.bat", "other"), ("start_sample.bat", "sample")):
        text = (root / name).read_text(encoding="utf-8")
        assert f"set HAKOYA_MODE={mode}" in text and "chcp 65001" in text and "PYTHONUTF8=1" in text
        assert ".venv\\Scripts\\python.exe\" -m app.server" in text
    assert "HAKOYA_MODE" not in (root / "start.bat").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 見本用のデータ（core/paths.py の ensure_sample_data）

def _own_data(root: Path) -> Path:
    """自社用のデータ（架空）：見本の家「デモ用」と、写してはいけない物件A・見本の家。glb は中身のない架空のファイル。"""
    import yaml
    own, glb = root / "own", root / "glb"
    glb.mkdir(parents=True)
    for name in ("demo.glb", "a.glb", "mihon.glb"):
        (glb / name).write_bytes(b"glTF")
    (own / "out").mkdir(parents=True)
    conf = {"ng_words": ["架空の施主の名前"], "物件A": {"glb": str(glb / "a.glb")},
            "デモ用": {"glb": str(glb / "demo.glb"), "ng_words": ["架空の言葉"]}, "見本の家": {"glb": str(glb / "mihon.glb")}}
    (own / "local_paths.yaml").write_text(yaml.safe_dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for alias in ("デモ用", "物件A", "見本の家"):
        d = own / "out" / alias
        (d / "model").mkdir(parents=True)
        (d / "building.json").write_text(json.dumps({"alias": alias}, ensure_ascii=False), encoding="utf-8")
        (d / "model" / "checks.json").write_text('{"checks": []}', encoding="utf-8")
    (own / "out" / "デモ用" / "overrides.json").write_text("[]", encoding="utf-8")
    (own / "cache" / "デモ用").mkdir(parents=True)
    (own / "cache" / "デモ用" / "source.glb").write_bytes(b"glTF")
    return own


def _bundle(root: Path) -> Path:
    """同梱の見本（samples/sample_house）の形の、架空の中身。"""
    b = root / "samples" / "sample_house"
    (b / "out" / "model").mkdir(parents=True)
    (b / "house.glb").write_bytes(b"glTF-sample")
    (b / "out" / "building.json").write_text(json.dumps({"alias": "見本の家"}, ensure_ascii=False), encoding="utf-8")
    (b / "out" / "findings.json").write_text('{"findings": [{"id": "F1"}]}', encoding="utf-8")
    (b / "out" / "model" / "checks.json").write_text('{"checks": []}', encoding="utf-8")
    return b


def test_sample_data_copies_the_bundled_sample_house(tmp_path, capsys):
    """公開リポジトリ：自社用のデータがなくても、同梱の見本の家（組み立て＋検品の結果まで）を写す（★11/6 作者）。"""
    bundle, sample = _bundle(tmp_path), tmp_path / "sample"
    assert paths.ensure_sample_data(tmp_path / "nothing", sample, bundle) == "copied"
    conf = paths.load_local_paths(sample / "local_paths.yaml")
    assert paths.aliases(sample / "local_paths.yaml") == ["見本の家"]
    assert Path(conf["見本の家"]["glb"]) == (sample / "glb" / "house.glb").resolve()       # glb も写し、写した方を指す
    assert (sample / "glb" / "house.glb").read_bytes() == b"glTF-sample"
    for rel in ("building.json", "findings.json", "model/checks.json"):
        assert (sample / "out" / "見本の家" / rel).read_bytes() == (bundle / "out" / rel).read_bytes()
    assert ss.is_sample_alias("見本の家")                                       # 見本用の一覧に出る
    assert capsys.readouterr().out == ""                                       # 何も表示しない（場所を出さない）
    (sample / "out" / "見本の家" / "findings.json").write_text("{}", encoding="utf-8")
    assert paths.ensure_sample_data(tmp_path / "nothing", sample, bundle) == "ready"   # 2回目からは何もしない
    assert (sample / "out" / "見本の家" / "findings.json").read_text(encoding="utf-8") == "{}"
    own = _own_data(tmp_path / "b")
    assert paths.ensure_sample_data(own, tmp_path / "s2", bundle) == "copied"        # 同梱があれば、自社用のデモ用より先
    assert paths.aliases(tmp_path / "s2" / "local_paths.yaml") == ["見本の家"]
    (bundle / "out" / "building.json").unlink()                                       # 組み立て済みがない同梱は使わない
    assert paths.ensure_sample_data(own, tmp_path / "s3", bundle) == "copied"
    assert paths.aliases(tmp_path / "s3" / "local_paths.yaml") == ["デモ用"]


def test_sample_data_redoes_a_broken_bundle_copy(tmp_path):
    bundle, sample = _bundle(tmp_path), tmp_path / "sample"
    (sample / "out" / "見本の家").mkdir(parents=True)
    (sample / "out" / "見本の家" / "half.json").write_text("{", encoding="utf-8")
    assert paths.ensure_sample_data(tmp_path / "nothing", sample, bundle) == "copied"
    assert not (sample / "out" / "見本の家" / "half.json").exists()


NOB = Path("__no_bundle__")              # 同梱の見本がない場合（作者の手元の samples/ に左右されないように）


def test_sample_data_copies_only_the_demo_house(tmp_path, capsys):
    own, sample = _own_data(tmp_path), tmp_path / "sample"
    assert paths.ensure_sample_data(own, sample, tmp_path / NOB) == "copied"
    conf = paths.load_local_paths(sample / "local_paths.yaml")
    assert paths.aliases(sample / "local_paths.yaml") == ["デモ用"]                 # デモ用だけ（見本の家・物件A は写さない）
    assert conf["デモ用"]["glb"] == str(tmp_path / "glb" / "demo.glb")               # glb の場所は同じ（glb は写さない）
    assert conf["ng_words"] == ["架空の施主の名前"] and conf["デモ用"]["ng_words"] == ["架空の言葉"]   # 見張りの言葉は写す
    assert sorted(p.name for p in (sample / "out").iterdir()) == ["デモ用"]
    for rel in ("building.json", "overrides.json", "model/checks.json"):
        assert (sample / "out" / "デモ用" / rel).read_bytes() == (own / "out" / "デモ用" / rel).read_bytes()
    assert not (sample / "cache").exists()
    assert sorted(p.name for p in sample.iterdir()) == ["local_paths.yaml", "out"]   # 書きかけの一時ファイルも残さない
    assert capsys.readouterr().out == ""                                       # 何も表示しない（場所を出さない）
    (sample / "out" / "デモ用" / "overrides.json").write_text('[{"id": "OV1"}]', encoding="utf-8")
    assert paths.ensure_sample_data(own, sample) == "ready"                     # 2回目からは何もしない（見本用で直した分を消さない）
    assert (sample / "out" / "デモ用" / "overrides.json").read_text(encoding="utf-8") == '[{"id": "OV1"}]'


def test_sample_data_needs_a_built_demo_house(tmp_path):
    import yaml
    own, sample = _own_data(tmp_path), tmp_path / "sample"
    (own / "out" / "デモ用" / "building.json").unlink()                           # 組み立てていない
    nob = tmp_path / NOB
    assert paths.ensure_sample_data(own, sample, nob) == "missing" and not sample.exists()
    assert paths.ensure_sample_data(tmp_path / "nothing", sample, nob) == "missing"   # 自社用のデータも同梱の見本もない
    assert paths.ensure_sample_data(own, own, nob) == "ready"                   # 自社用のデータそのものには書かない
    assert paths.ensure_sample_data(own, own, _bundle(tmp_path / "c")) == "ready"   # 同梱があっても、自社用のデータには書かない
    other = _own_data(tmp_path / "b")
    conf = paths.load_local_paths(other / "local_paths.yaml")
    del conf["デモ用"]
    (other / "local_paths.yaml").write_text(yaml.safe_dump(conf, allow_unicode=True), encoding="utf-8")
    assert paths.ensure_sample_data(other, sample, nob) == "missing" and not sample.exists()   # 登録していない


def test_sample_data_redoes_a_broken_copy(tmp_path):
    """前に途中で止まった写し（local_paths.yaml がまだない）は、写し直す。"""
    own, sample = _own_data(tmp_path), tmp_path / "sample"
    (sample / "out" / "デモ用").mkdir(parents=True)
    (sample / "out" / "デモ用" / "half.json").write_text("{", encoding="utf-8")
    assert paths.ensure_sample_data(own, sample, tmp_path / NOB) == "copied"
    assert not (sample / "out" / "デモ用" / "half.json").exists() and (sample / "out" / "デモ用" / "building.json").exists()


def test_sample_start_messages_have_no_paths(monkeypatch, capsys):
    from app import server
    secret = "C:\\架空\\施主の名前のフォルダ"

    def boom(*a, **k):
        raise PermissionError(f"[WinError 5] アクセスが拒否されました: '{secret}'")
    for result, word in ((lambda: "missing", "start.bat"), (lambda: "copied", "写しました"), (boom, "PermissionError")):
        monkeypatch.setattr(paths, "ensure_sample_data", result)
        server._prepare_sample_data()
        out = capsys.readouterr().out
        assert word in out and secret not in out and "架空" not in out


# ---------------------------------------------------------------- 口の番号・exe の --mode

def test_each_mode_starts_on_its_own_port(monkeypatch, capsys):
    import uvicorn

    from app import server
    used = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, log_level: used.append(port))
    monkeypatch.setattr(server, "_port_in_use", lambda port: False)
    monkeypatch.setattr(server, "_prepare_sample_data", lambda: used.append("sample-data"))
    monkeypatch.setenv("HAKOYA_NO_BROWSER", "1")
    monkeypatch.delenv("HAKOYA_PORT", raising=False)
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    for m in ("own", "other", "sample"):
        monkeypatch.setattr(paths, "MODE", m)
        assert server.main() == 0
    assert used == [8765, 8766, "sample-data", 8767]                          # 見本用だけ、起動の前にデータを用意する
    assert "HAKOYA（見本用）を起動しました" in capsys.readouterr().out
    monkeypatch.setenv("HAKOYA_PORT", "9123")
    server.main()
    assert used[-1] == 9123                                                   # HAKOYA_PORT がいちばん強い
    monkeypatch.setattr(server, "_port_in_use", lambda port: True)
    assert server.main() == 3 and "見本用" in capsys.readouterr().out


def _launcher():
    import importlib.util
    spec = importlib.util.spec_from_file_location("launch_self", Path(paths.REPO_ROOT) / "packaging" / "launch_self.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exe_launcher_takes_mode(monkeypatch, capsys):
    from app import server
    ls = _launcher()
    assert ls.MODES == paths.MODES
    assert ls.mode_from_args([]) is None and ls.mode_from_args(["--mode", "sample"]) == "sample"
    assert ls.mode_from_args(["--mode=own"]) == "own" and ls.mode_from_args(["--mode"]) == ""
    monkeypatch.setenv("HAKOYA_MODE", "own")
    monkeypatch.setattr(server, "main", lambda: 7)
    assert ls.main(["--mode", "Sample"]) == 7 and os.environ["HAKOYA_MODE"] == "sample"
    assert ls.main(["--mode", "everything"]) == 2 and os.environ["HAKOYA_MODE"] == "sample"
    assert "--mode は" in capsys.readouterr().out
    monkeypatch.setenv("HAKOYA_MODE", "other")
    assert ls.main([]) == 7 and os.environ["HAKOYA_MODE"] == "other"           # 書かなければ、そのまま
