"""外壁の仕上げ（ざらざら＝ファジースキンを物件ごとに付けるか。core/print_options.py。★11/6 作者）。
架空の小さな家（tests/test_builder.py の _tiny_house）を一時フォルダに置いて確かめる（実データ不要）。
- 付けると model.3mf の外壁の部品にだけ fuzzy_skin=external が入る。付けないと入らない。形の印（parts の fuzzy_skin）は同じ
- 決めていなければ会社ルール（B工務店（架空）はサイディングなので付けない）
- 画面の窓口 GET・POST・DELETE /api/print_options、保存の名前の _fuzzy
- ざらざらの層の検査・Claude の家の要約・MCP の書き出しの説明が、物件の値に従う
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
import zipfile

import pytest

from core import paths
from core import print_options as po
from core.rules import load_rules
from tests.test_builder import _tiny_house

ALIAS = "物件T"
EXTERIOR_PARTS = ["1階の外壁（表面）", "屋根の外壁"]


@pytest.fixture
def house(tmp_path, monkeypatch):
    """B工務店（架空。外壁はサイディング）で作った小さな家。作る会社を変えるときは house(company)。"""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)

    def make(company: str = "bkoumuten") -> dict:
        b = _tiny_house()
        b["source"] = {"company_rules": company}
        b["ground"] = {"foundation_top_mm": 480}
        for o in b["openings"]:                                                   # 会社ルールとの比べ（get_building・検査）が読む値
            o["sill_fl_mm"], o["head_fl_mm"] = o["sill_mm"] - 624, o["head_mm"] - 624
        d = tmp_path / "out" / ALIAS
        d.mkdir(parents=True, exist_ok=True)
        (d / "building.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        return b
    make()
    return make


def _fuzzy_parts(alias: str = ALIAS) -> list[str]:
    """model.3mf で、ファジースキンの設定が入っている部品の名前。"""
    z = zipfile.ZipFile(paths.out_dir(alias) / "model" / "model.3mf")
    cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
    return [p.find("metadata[@key='name']").get("value") for o in cfg for p in o.findall("part")
            if (m := p.find("metadata[@key='fuzzy_skin']")) is not None and m.get("value") == "external"]


def _export_note(summary: dict) -> str:
    return next(c["message"] for c in summary["checks"] if c["item"] == "bambu_3mf")


# ---------------------------------------------------------------- 保存のファイル

def test_saved_value_round_trip(house):
    rules = load_rules("bkoumuten")
    assert po.saved_fuzzy(ALIAS) is None
    assert po.effective_fuzzy(ALIAS, rules) == (False, "company")               # サイディングの会社は付けない
    po.set_fuzzy(ALIAS, True)
    assert json.loads((paths.out_dir(ALIAS) / po.FILE).read_text(encoding="utf-8")) == {"fuzzy_skin": True}
    assert po.effective_fuzzy(ALIAS, rules) == (True, "property")
    po.set_fuzzy(ALIAS, False)
    assert po.effective_fuzzy(ALIAS, {"model": {"exterior_finish": "plaster"}}) == (False, "property")
    po.clear(ALIAS)
    assert not (paths.out_dir(ALIAS) / po.FILE).exists()
    assert po.effective_fuzzy(ALIAS, {"model": {"exterior_finish": "plaster"}}) == (True, "company")
    (paths.out_dir(ALIAS) / po.FILE).write_text("{こわれた", encoding="utf-8")   # 読めないファイルは「決めていない」
    assert po.saved_fuzzy(ALIAS) is None
    assert po.company_default({"model": {"exterior_finish": "none"}}) is False and po.company_default({}) is False


# ---------------------------------------------------------------- 書き出し（3MF の設定・案内の文・checks.json）

def test_default_follows_the_company_rules_siding_has_no_fuzzy(house):
    from core.export import write_outputs
    s = write_outputs(ALIAS, bambu_enabled=False)
    assert _fuzzy_parts() == []
    assert s["fuzzy_skin"] == {"on": False, "source": "company", "company_default": False}
    assert "付けていません" in _export_note(s) and "外壁の仕上げ" in _export_note(s)
    assert [k for k, p in s["parts"].items() if p["fuzzy_skin"]] == ["exterior1", "roof_ext"]   # 形の印はそのまま


def test_turning_fuzzy_on_writes_it_only_on_the_exterior_parts(house):
    from core.export import write_outputs
    po.set_fuzzy(ALIAS, True)
    s = write_outputs(ALIAS, bambu_enabled=False)
    assert _fuzzy_parts() == EXTERIOR_PARTS
    assert s["fuzzy_skin"] == {"on": True, "source": "property", "company_default": False}
    assert "「その他」→「ファジースキン」" in _export_note(s)                    # Bambu 形式がないときは手で付ける案内
    saved = json.loads((paths.out_dir(ALIAS) / "model" / "checks.json").read_text(encoding="utf-8"))
    assert saved["fuzzy_skin"]["on"] is True
    po.set_fuzzy(ALIAS, False)
    s = write_outputs(ALIAS, bambu_enabled=False)
    assert _fuzzy_parts() == [] and s["fuzzy_skin"]["on"] is False
    assert [k for k, p in s["parts"].items() if p["fuzzy_skin"]] == ["exterior1", "roof_ext"]   # 形は変わらない


def test_plaster_company_can_turn_fuzzy_off_for_one_property(house):
    """塗り壁の会社（自社）では、決めていなければ付く。物件で「付けない」にすると外れ、既定に戻すとまた付く。"""
    from core.export import write_outputs
    house("jisha")
    s = write_outputs(ALIAS, bambu_enabled=False)
    assert _fuzzy_parts() == EXTERIOR_PARTS and s["fuzzy_skin"] == {"on": True, "source": "company", "company_default": True}
    po.set_fuzzy(ALIAS, False)
    assert _fuzzy_parts() == EXTERIOR_PARTS                                       # 書き出し直すまでは前のまま
    s = write_outputs(ALIAS, bambu_enabled=False)
    assert _fuzzy_parts() == [] and s["fuzzy_skin"]["source"] == "property"
    po.clear(ALIAS)
    write_outputs(ALIAS, bambu_enabled=False)
    assert _fuzzy_parts() == EXTERIOR_PARTS


def test_bambu_note_tells_whether_fuzzy_is_set(house, monkeypatch):
    """Bambu 形式を作れたときの案内の文も、付けた／付けないで分かれる（Bambu Studio はなくてよい：作る所を差し替える）。"""
    from core import export
    monkeypatch.setattr(export, "to_bambu_3mf", lambda src, dst: {"ok": True})
    assert "付けていません" in _export_note(export.write_outputs(ALIAS))
    po.set_fuzzy(ALIAS, True)
    assert "外壁だけざらざら（ファジースキン・外側の輪郭）を設定しています" in _export_note(export.write_outputs(ALIAS))


# ---------------------------------------------------------------- 画面の窓口

@pytest.fixture
def client(house, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    from core.export import write_outputs
    monkeypatch.setattr(server, "_aliases", lambda: [ALIAS])
    monkeypatch.setattr(server, "write_outputs", lambda a, *k, **kw: write_outputs(a, bambu_enabled=False))
    write_outputs(ALIAS, bambu_enabled=False)
    return TestClient(server.app)


def _saved_name(r) -> str:
    return r.headers["content-disposition"].split("filename=")[-1].strip('"')


def test_screen_can_turn_fuzzy_on_and_back_to_the_default(client):
    url = "/api/print_options/" + ALIAS
    assert client.get(url).json() == {"fuzzy_skin": {"on": False, "source": "company", "company_default": False}}
    assert _saved_name(client.get(f"/api/model/{ALIAS}/model.3mf")) == "hakoya_model.3mf"

    r = client.post(url, json={"fuzzy_skin": True})
    assert r.status_code == 200
    assert r.json()["fuzzy_skin"] == {"on": True, "source": "property", "company_default": False}
    assert any("「その他」→「ファジースキン」" in c["message"] for c in r.json()["checks"])
    assert _fuzzy_parts() == EXTERIOR_PARTS                                        # 書き出し直した
    assert client.get(url).json()["fuzzy_skin"]["on"] is True
    assert _saved_name(client.get(f"/api/model/{ALIAS}/model.3mf")) == "hakoya_model_fuzzy.3mf"   # 名前でも分かる
    assert _saved_name(client.get(f"/api/model/{ALIAS}/exterior1.stl")) == "hakoya_exterior1.stl"  # STL は形だけなので付けない

    r = client.delete(url)
    assert r.status_code == 200 and r.json()["fuzzy_skin"] == {"on": False, "source": "company", "company_default": False}
    assert _fuzzy_parts() == [] and not (paths.out_dir(ALIAS) / po.FILE).exists()
    assert _saved_name(client.get(f"/api/model/{ALIAS}/model.3mf")) == "hakoya_model.3mf"


def test_screen_guards_like_the_split_line(client, monkeypatch):
    from app import server
    url = "/api/print_options/" + ALIAS
    assert client.get("/api/print_options/物件Z").status_code == 404
    assert client.post("/api/print_options/物件Z", json={"fuzzy_skin": True}).status_code == 404
    assert client.post(url, json={"fuzzy_skin": "あり"}).status_code == 422        # true / false だけ
    monkeypatch.setitem(server.RUNS, ALIAS, {"running": True})                    # 検品中は変えない
    assert client.post(url, json={"fuzzy_skin": True}).status_code == 409
    assert client.delete(url).status_code == 409
    assert po.saved_fuzzy(ALIAS) is None


def test_screen_puts_the_old_value_back_when_the_export_fails(client, monkeypatch):
    from app import server
    url = "/api/print_options/" + ALIAS
    assert client.post(url, json={"fuzzy_skin": False}).status_code == 200

    def boom(*a, **k):
        raise RuntimeError("書き出しの失敗（試験）")
    monkeypatch.setattr(server, "write_outputs", boom)
    assert client.post(url, json={"fuzzy_skin": True}).status_code == 500          # 種類だけを返す（HAKOYA のエラーの扱い）
    assert po.saved_fuzzy(ALIAS) is False                                          # 前の値に戻した
    assert client.delete(url).status_code == 500
    assert po.saved_fuzzy(ALIAS) is False


def test_screen_needs_a_built_house(client):
    (paths.out_dir(ALIAS) / "building.json").unlink()
    assert client.post("/api/print_options/" + ALIAS, json={"fuzzy_skin": True}).status_code == 409
    assert po.saved_fuzzy(ALIAS) is None


# ---------------------------------------------------------------- 検査・Claude・MCP

def test_fuzzy_face_check_follows_the_property_setting():
    """ざらざらの層から外れた外壁の検査は、ざらざらにする物件だけ（None なら今までどおり会社ルール）。"""
    from core.shape_checks import fuzzy_face_findings, shape_findings
    b = _tiny_house()
    b["walls"][0]["thickness_mm"] = 180                                           # 外の面が 10mm 外へ（tests/test_sleeve_walls.py と同じ）
    plain = load_rules("bkoumuten")
    assert fuzzy_face_findings(b, plain) == []                                    # サイディングの会社の既定
    assert [f["targets"] for f in fuzzy_face_findings(b, plain, enabled=True)] == [["W1"]]
    assert fuzzy_face_findings(b, plain, enabled=False) == []
    assert not [f for f in shape_findings(b, plain, False) if "ざらざら" in f["title"]]
    assert [f for f in shape_findings(b, plain, True) if "ざらざら" in f["title"]]


def test_code_findings_use_the_property_setting(house):
    from core.findings import code_findings
    b = house()
    b["walls"][0]["thickness_mm"] = 180
    (paths.out_dir(ALIAS) / "building.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    fuzzy = lambda: [f for f in code_findings(ALIAS) if f["title"] == "外壁 W1 がざらざらにならない"]   # noqa: E731
    assert fuzzy() == []
    po.set_fuzzy(ALIAS, True)
    assert len(fuzzy()) == 1


def test_claude_sees_whether_the_property_is_fuzzy(house):
    from core.ai.tools import ToolContext, get_building
    assert get_building(ToolContext.load(ALIAS))["fuzzy_skin"] == {"on": False, "source": "company"}
    po.set_fuzzy(ALIAS, True)
    assert get_building(ToolContext.load(ALIAS))["fuzzy_skin"] == {"on": True, "source": "property"}


def test_mcp_export_notes_and_staleness_follow_the_setting(house):
    import os

    from core.ai import mcp_tools as mt
    from core.export import write_outputs
    write_outputs(ALIAS, bambu_enabled=False)
    model = paths.out_dir(ALIAS) / "model"
    assert mt._file_notes(model)["model_bambu.3mf"] == "Bambu Studio 形式の 3MF（ざらざらなし）"
    assert not mt._outputs_stale(ALIAS)
    po.set_fuzzy(ALIAS, True)
    checks = model / "checks.json"
    t = checks.stat().st_mtime
    os.utime(paths.out_dir(ALIAS) / po.FILE, (t + 10, t + 10))                     # 選び方が書き出しより新しい
    assert mt._outputs_stale(ALIAS)
    write_outputs(ALIAS, bambu_enabled=False)
    os.utime(checks, (t + 20, t + 20))
    assert not mt._outputs_stale(ALIAS)
    assert mt._file_notes(model)["model_bambu.3mf"] == "Bambu Studio 形式の 3MF（外壁のざらざらの設定つき）"
