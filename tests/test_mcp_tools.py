"""社外の Claude Code 向けの MCP の道具（core/ai/mcp_tools.py・app/mcp_server.py）を、架空の家で確かめる（実データ不要）。

- 物件の名前は登録したものだけ・画像は中身で返して場所は返さない・検品の結果は findings.json（connection は mcp）に入る・
  直すと記録して組み直し、取り消せる・人の数値は人の一文にあるものだけ・思いがけないエラーの文（場所が入る）は返さない。
- 最後に、本当に別のプロセスで `python -m app.mcp_server` を起動し、MCP の相手（mcp.client）からつないで確かめる。
家・物件の名前・出してはいけない言葉は、どれも架空。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image

from core import findings as fd
from core import overrides as ov
from core import paths, privacy
from core.ai import mcp_tools as mt
from core.export import write_outputs
from core.rules import default_company
from tests.test_complex_shapes import _house

REPO_ROOT = Path(__file__).resolve().parents[1]
ALIAS = "試験の家M"          # 架空の家（組み立て済み）
OTHER = "試験の家N"          # 架空の家（登録だけ。まだ組み立てていない）
SAMPLE = "見本の家M"         # 見本確認用で出す名前の架空の家
NG = "架空山田"              # 出してはいけない言葉の試し（架空）
VIEWS = ("S", "N", "E", "W", "plan")
CHANGE_TOOLS = {"apply_override", "undo_override", "decide_finding", "rebuild"}
READ_TOOLS = {"list_properties", "get_building", "get_elements", "render_view", "get_drawing_region", "run_checks",
              "get_code_findings", "get_findings", "list_overrides", "propose_override", "open_in_hakoya"}


def _building() -> dict:
    """6m×4m の平屋（窓4つ）。内壁 W9 だけ厚みが 145（ほかの内壁は 130）。"""
    b = _house([(0, 0), (6000, 0), (6000, 4000), (0, 4000)])
    b["source"] = {"company_rules": default_company(), "glb": f"{ALIAS}.glb"}
    b["ground"] = {"gl_mm": 0, "foundation_top_mm": 480}
    b["walls"].append({"id": "W9", "level": "1F", "kind": "interior", "start": [1000, 80], "end": [1000, 2000],
                       "thickness_mm": 145, "bottom_mm": 624, "height": 3024})
    return b


def _fake_render_all(alias: str) -> dict:
    """glb のない架空の家の、面の図の代わり（小さな PNG と、部材の図の上の範囲）。"""
    d = paths.out_dir(alias) / "inspect"
    d.mkdir(parents=True, exist_ok=True)
    b = json.loads((paths.out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    elems = [{"id": w["id"], "type": "wall", "bbox_px": [10, 10, 40, 40]} for w in b["walls"]]
    for view in VIEWS:
        for src in ("model", "glb"):
            Image.new("RGB", (64, 48), (255, 255, 255)).save(d / f"{view}_{src}.png")
        (d / f"{view}.json").write_text(json.dumps({"view": view, "elements": elems, "uv_lo": [0, 0], "uv_hi": [1, 1]},
                                                   ensure_ascii=False), encoding="utf-8")
    return {v: {"elements": len(elems)} for v in VIEWS}


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """組み立てて書き出した架空の家と、面の図の代わりを入れたデータのフォルダ（テストごとに写して使う）。"""
    root = tmp_path_factory.mktemp("hakoya_mcp") / "data"
    d = root / "out" / ALIAS
    d.mkdir(parents=True)
    (d / "building.json").write_text(json.dumps(_building(), ensure_ascii=False), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paths, "DATA_DIR", root)
        write_outputs(ALIAS, bambu_enabled=False)
        _fake_render_all(ALIAS)                          # 書き出しより新しい図（作り直さなくてよい）
    (root / "local_paths.yaml").write_text(f"ng_words:\n  - {NG}\n{ALIAS}: {{}}\n{OTHER}: {{}}\n", encoding="utf-8")
    return root


@pytest.fixture
def data(template, tmp_path, monkeypatch):
    root = tmp_path / "data"
    shutil.copytree(template, root)
    monkeypatch.setattr(paths, "DATA_DIR", root)
    monkeypatch.setattr(paths, "LOCAL_PATHS", root / "local_paths.yaml")
    monkeypatch.setattr(privacy, "LOCAL_PATHS", root / "local_paths.yaml")      # 出してはいけない言葉も架空の一覧から
    monkeypatch.setattr(mt, "_render_all", _fake_render_all)
    from core import render
    monkeypatch.setattr(render, "render_all", _fake_render_all)            # アプリの道具の作り直し（core/ai/tools.py）も本物を呼ばない
    monkeypatch.setattr(mt, "_write_outputs", lambda a: write_outputs(a, bambu_enabled=False))
    for k in ("HAKOYA_PORT", "HAKOYA_MODE"):
        monkeypatch.delenv(k, raising=False)
    return root


def _finding(**kw) -> dict:
    f = {"view": "plan", "kind": "shape", "severity": "中", "message": "内壁 W9 だけ厚みがほかの内壁と違う", "targets": ["W9"],
         "evidence": ["get_elements の W9", "会社ルール walls.interior_thickness_mm"], "confidence": "高",
         "proposal": {"target": "W9", "field": "thickness_mm", "value_source": "majority:interior", "reason": "ほかの内壁とそろえる"},
         "question": None}
    f.update(kw)
    return f


def _no_paths(text: str, root: Path) -> None:
    assert str(root) not in text and json.dumps(str(root))[1:-1] not in text
    assert not re.search(r"(?<![A-Za-z])[A-Za-z]:(\\\\|/)", text)          # ドライブ名で始まる場所（http:// は除く）


# ---------------------------------------------------------------- 物件の名前

def test_alias_must_be_a_registered_property(data):
    for bad in ("物件Z", f"../{ALIAS}", "..", f"{ALIAS}/x", " "):
        r = mt.call("get_building", {"alias": bad})
        assert r.is_error and "list_properties" in r.data["error"], bad
    r = mt.call("get_building", {"alias": OTHER})
    assert r.is_error and "組み立てていません" in r.data["error"]
    r = mt.call("get_building", {})
    assert r.is_error and "「alias」がありません" in r.data["error"]
    (data / "local_paths.yaml").unlink()                       # 登録がひとつもない PC
    assert mt.call("list_properties", {}).data["properties"] == []
    assert mt.call("get_building", {"alias": ALIAS}).is_error


def test_sample_view_shows_only_the_sample_houses(data, monkeypatch):
    from core import settings_store
    (data / "local_paths.yaml").write_text(f"{ALIAS}: {{}}\n{SAMPLE}: {{}}\n", encoding="utf-8")
    monkeypatch.setattr(settings_store, "mode", lambda: "sample")          # ★11/5 見本用は起動のモードで決まる
    assert [p["alias"] for p in mt.call("list_properties", {}).data["properties"]] == [SAMPLE]
    assert mt.call("get_building", {"alias": ALIAS}).is_error


# ---------------------------------------------------------------- 調べる道具

def test_list_and_read_tools(data):
    props = {p["alias"]: p for p in mt.call("list_properties", {}).data["properties"]}
    assert props[ALIAS]["built"] and props[ALIAS]["counts"]["walls"] == 6 and props[ALIAS]["has_glb"] is False
    assert props[OTHER] == {"alias": OTHER, "built": False, "has_glb": False}
    s = mt.call("get_building", {"alias": ALIAS}).data
    assert s["property"] == ALIAS and s["walls"] == {"exterior": 4, "interior": 2}
    e = mt.call("get_elements", {"alias": ALIAS, "ids": ["W9"]}).data
    assert e["count"] == 1 and e["elements"][0]["thickness_mm"] == 145
    e = mt.call("get_elements", {"alias": ALIAS, "view": "S"}).data
    assert e["count"] == 6 and e["elements"][0]["bbox_px"] == [10, 10, 40, 40]   # 面の図に写る部材（図の上の範囲つき）
    assert mt.call("run_checks", {"alias": ALIAS}).data["checks"]
    cf = mt.call("get_code_findings", {"alias": ALIAS}).data
    assert cf["count"] == len(cf["code_findings"]) and all(c["id"].startswith("C") for c in cf["code_findings"])
    assert "まだ検品の結果がありません" in mt.call("get_findings", {"alias": ALIAS}).data["note"]
    assert mt.call("list_overrides", {"alias": ALIAS}).data["count"] == 0
    r = mt.call("get_drawing_region", {"alias": ALIAS, "view": "plan"})       # 図面のない家：図面なしで判断するよう伝える
    assert not r.is_error and r.data["available"] is False and not r.images


def test_wrong_arguments_are_explained_in_japanese(data):
    r = mt.call("get_elements", {"alias": ALIAS, "view": "top"})
    assert r.is_error and "「view」は S・N・E・W・plan・plan2 のどれかです" == r.data["error"]
    r = mt.call("get_building", {"alias": ALIAS, "color": "red"})
    assert r.is_error and "「color」は使えない項目です" in r.data["error"]
    r = mt.call("render_view", {"alias": ALIAS, "view": "S", "highlight": "W1"})
    assert r.is_error and "「highlight」の形が違います" in r.data["error"]
    r = mt.call("render_view", {"alias": ALIAS, "view": "plan2"})               # 平屋に2階の平面はない（アプリの道具の文）
    assert r.is_error and "平屋" in r.data["error"]
    assert mt.call("make_numbers", {}).is_error
    assert not mt.call("get_elements", {"alias": ALIAS, "view": None, "ids": ["W1"]}).is_error   # null は省いたことにする


def test_render_view_returns_the_image_but_never_its_path(data):
    r = mt.call("render_view", {"alias": ALIAS, "view": "S", "highlight": ["W1", "W99"]})
    assert not r.is_error, r.text
    assert len(r.images) == 1 and r.images[0][:8] == b"\x89PNG\r\n\x1a\n"
    assert "image_path" not in r.text and r.data["highlighted"] == ["W1"] and r.data["not_in_view"] == ["W99"]
    _no_paths(r.text, data)


def test_images_are_made_again_when_the_model_changed(data, monkeypatch):
    calls = []
    monkeypatch.setattr(mt, "_render_all", lambda a: (calls.append(a), _fake_render_all(a))[1])
    mt.call("render_view", {"alias": ALIAS, "view": "plan"})
    assert calls == []                                          # 図が書き出しより新しければ作り直さない
    checks = paths.out_dir(ALIAS) / "model" / "checks.json"
    t = (paths.out_dir(ALIAS) / "inspect" / "plan.json").stat().st_mtime + 10
    os.utime(checks, (t, t))                                    # 模型を書き出し直した（修正や組み直しのあと）
    mt.call("render_view", {"alias": ALIAS, "view": "plan"})
    assert calls == [ALIAS]

    def no_glb(a):
        raise FileNotFoundError(f"{a} の glb：local_paths.yaml に書かれた場所にありません。")
    monkeypatch.setattr(mt, "_render_all", no_glb)
    os.utime(checks, (t + 20, t + 20))
    r = mt.call("render_view", {"alias": ALIAS, "view": "plan"})   # 作り直せなくても、前の図を「古いかも」と添えて返す
    assert not r.is_error and r.images and "前の模型の図" in r.data["note"]


# ---------------------------------------------------------------- 人の数値の守り・直す案

def test_number_is_only_taken_from_the_humans_own_words(data):
    base = {"alias": ALIAS, "target": "O1", "field": "width_mm", "value_source": "number", "number": 900,
            "reason": "人が書いた幅"}
    r = mt.call("propose_override", base)
    assert r.is_error and "human_text" in r.data["error"]
    r = mt.call("propose_override", {**base, "human_text": "この窓の幅は８００にしたい"})
    assert r.is_error and "にない数値" in r.data["error"]
    r = mt.call("propose_override", {**base, "human_text": "この窓の幅は９００にしたい"})       # 全角の数字でも読める
    assert not r.is_error and r.data["proposal"]["to"] == 900.0 and r.data["proposal"]["value_source"] == "human:number:900"
    assert ov.load(ALIAS) == []                                     # 試すだけ（保存しない）
    r = mt.call("apply_override", {**base, "value_source": "human:number:900"})   # 案の形で渡しても、人の一文が要る
    assert r.is_error and "human_text" in r.data["error"] and ov.load(ALIAS) == []


def test_code_computes_the_number_for_other_sources(data):
    r = mt.call("propose_override", {"alias": ALIAS, "target": "W9", "field": "thickness_mm",
                                     "value_source": "majority:interior", "reason": "ほかの内壁とそろえる"})
    assert (r.data["proposal"]["from"], r.data["proposal"]["to"]) == (145, 130.0)
    r = mt.call("propose_override", {"alias": ALIAS, "target": "W9", "field": "thickness_mm", "value_source": "1300",
                                     "reason": "x"})
    assert r.is_error and "使えません" in r.data["error"]


# ---------------------------------------------------------------- 検品の結果を送る

def test_report_findings_writes_findings_json_for_the_inspection_tab(data):
    q = mt.call("ask_question", {"alias": ALIAS, "question": "南の窓の上端は図面ではどちらですか", "options": ["FL+2000", "FL+2200"],
                                 "evidence": "S の O1"})
    assert q.data["queued"] == "Q1"
    findings = [
        _finding(title="内壁 W9 だけ厚い", id="F9", status="承認"),        # 番号・状態はコードが付け直す（入れてきても使わない）
        _finding(view="S", kind="opening", severity="低", message="南の窓 O1 の幅を人の数値にする", targets=["O1"],
                 proposal={"target": "O1", "field": "width_mm", "value_source": "number", "number": 900, "reason": "人の数値"}),
        _finding(severity="高", message="W99 の壁が足りない", targets=["W99"], proposal=None),
    ]
    r = mt.call("report_findings", {"alias": ALIAS, "findings": findings, "summary": "架空の家の試しの検品"})
    assert not r.is_error, r.text
    assert r.data["findings"] == 3 and r.data["unknown_targets"] == ["W99"] and r.data["ignored_keys"] == ["id", "status"]
    rep = json.loads(fd.findings_path(ALIAS).read_text(encoding="utf-8"))
    assert rep["connection"] == "mcp" and rep["summary"] == "架空の家の試しの検品" and rep["alias"] == ALIAS
    assert [f["severity"] for f in rep["findings"]] == ["高", "中", "低"]          # 重要度の順
    w9 = next(f for f in rep["findings"] if f["targets"] == ["W9"])
    assert w9["id"] == "F1" and w9["status"] == "未確認" and w9["title"] == "内壁 W9 だけ厚い"
    assert (w9["proposal"]["from"], w9["proposal"]["to"]) == (145, 130.0)          # 数値はコードが計算し直す
    o1 = next(f for f in rep["findings"] if f["targets"] == ["O1"])
    assert o1["proposal"] is None and "human_text" in o1["proposal_error"]         # 人の一文のない数値は案にしない
    assert rep["questions"][0]["id"] == "Q1" and rep["questions"][0]["options"] == ["FL+2000", "FL+2200"]
    assert rep["code_findings"] and all(c["id"].startswith("C") for c in rep["code_findings"])
    assert not (paths.out_dir(ALIAS) / mt.QUESTIONS_FILE).exists()                 # 送った質問は findings.json に入った
    got = mt.call("get_findings", {"alias": ALIAS}).data
    assert got["connection"] == "mcp" and len(got["findings"]) == 3 and got["stale"] is False


def test_bad_report_is_refused_and_nothing_is_written(data):
    r = mt.call("report_findings", {"alias": ALIAS, "findings": [_finding(severity="最高")], "summary": "x"})
    assert r.is_error and r.data["error"] == "「findings[0].severity」は 高・中・低 のどれかです"
    r = mt.call("report_findings", {"alias": ALIAS, "findings": [_finding(proposal={"target": "W9"})], "summary": "x"})
    assert r.is_error and "「findings[0].proposal.field」がありません" in r.data["error"]
    assert not fd.findings_path(ALIAS).exists()


def test_number_proposal_in_a_report_needs_the_humans_words(data):
    p = {"target": "O1", "field": "width_mm", "value_source": "number", "number": 900, "reason": "人の数値",
         "human_text": "南の窓は幅900にして"}
    r = mt.call("report_findings", {"alias": ALIAS, "findings": [_finding(targets=["O1"], proposal=p)], "summary": "x"})
    assert not r.is_error, r.text
    f = json.loads(fd.findings_path(ALIAS).read_text(encoding="utf-8"))["findings"][0]
    assert f["proposal"]["to"] == 900.0 and f["proposal"]["value_source"] == "human:number:900"
    assert "human_text" not in f["proposal"]


# ---------------------------------------------------------------- 直す・取り消す・指摘を決める

def test_apply_override_and_undo_round_trip(data):
    checks = paths.out_dir(ALIAS) / "model" / "checks.json"
    before = checks.stat().st_mtime_ns
    r = mt.call("apply_override", {"alias": ALIAS, "target": "W9", "field": "thickness_mm",
                                   "value_source": "majority:interior", "reason": "ほかの内壁とそろえる"})
    assert not r.is_error, r.text
    item = r.data["override"]
    assert (item["id"], item["to"], item["origin"], item["from"]) == ("OV1", 130.0, "mcp", 145)
    assert [x["id"] for x in ov.load(ALIAS)] == ["OV1"] and checks.stat().st_mtime_ns != before     # 記録して組み直した
    assert mt.call("get_elements", {"alias": ALIAS, "ids": ["W9"]}).data["elements"][0]["thickness_mm"] == 130.0
    assert mt.call("list_overrides", {"alias": ALIAS}).data["overrides"][0]["status"] == "applied"
    u = mt.call("undo_override", {"alias": ALIAS, "override_id": "OV1"})
    assert not u.is_error and u.data["undone"]["id"] == "OV1" and ov.load(ALIAS) == []
    assert mt.call("get_elements", {"alias": ALIAS, "ids": ["W9"]}).data["elements"][0]["thickness_mm"] == 145
    r = mt.call("undo_override", {"alias": ALIAS, "override_id": "OV1"})
    assert r.is_error and r.data["error"] == "OV1 という修正はありません。"      # HAKOYA が書いた日本語の文はそのまま
    assert mt.call("undo_override", {"alias": ALIAS, "override_id": "../x"}).is_error


def test_apply_is_taken_back_when_the_model_cannot_be_built(data, monkeypatch):
    calls = []

    def broken_once(a):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError(r"C:\Users\someone\お客様\図面.jww で止まった")
        return write_outputs(a, bambu_enabled=False)
    monkeypatch.setattr(mt, "_write_outputs", broken_once)
    r = mt.call("apply_override", {"alias": ALIAS, "target": "W9", "field": "thickness_mm",
                                   "value_source": "majority:interior", "reason": "そろえる"})
    assert r.is_error and "取り消しました" in r.data["error"] and "RuntimeError" in r.data["error"]
    assert "someone" not in r.text and "図面" not in r.text
    assert ov.load(ALIAS) == [] and len(calls) == 2                              # 取り消して、元の模型で書き出し直した


def test_decide_finding_approve_reject_and_undo(data):
    findings = [_finding(), _finding(severity="低", message="O2 の位置を確かめたい", targets=["O2"], proposal=None)]
    assert not mt.call("report_findings", {"alias": ALIAS, "findings": findings, "summary": "x"}).is_error
    r = mt.call("decide_finding", {"alias": ALIAS, "finding_id": "F1", "action": "approve"})
    assert not r.is_error, r.text
    assert r.data["finding"]["status"] == "承認" and r.data["finding"]["override"] == "OV1"
    assert ov.load(ALIAS)[0]["origin"] == "finding:F1" and ov.load(ALIAS)[0]["to"] == 130.0
    again = mt.call("decide_finding", {"alias": ALIAS, "finding_id": "F1", "action": "承認"})
    assert again.is_error and "もう承認" in again.data["error"] and len(ov.load(ALIAS)) == 1   # 2回目の承認で修正を重ねない
    r = mt.call("decide_finding", {"alias": ALIAS, "finding_id": "F2", "action": "approve"})
    assert r.is_error and "直す案のない指摘" in r.data["error"]
    r = mt.call("decide_finding", {"alias": ALIAS, "finding_id": "F2", "action": "reject"})
    assert not r.is_error and r.data["finding"]["status"] == "却下"
    assert mt.call("decide_finding", {"alias": ALIAS, "finding_id": "F7", "action": "reject"}).is_error
    assert not mt.call("undo_override", {"alias": ALIAS, "override_id": "OV1"}).is_error
    f1 = next(f for f in fd.load(ALIAS)["findings"] if f["id"] == "F1")
    assert f1["status"] == "未確認" and f1["override"] is None                # 取り消すと指摘は未確認に戻る


def test_rebuild_reads_the_glb_again_and_keeps_the_human_fixes(data, monkeypatch):
    import adapters.walkinhome as wih
    monkeypatch.setattr(wih, "build", lambda alias, company=None: _building())      # glb の読み取りの代わり（架空の家）
    assert not mt.call("apply_override", {"alias": ALIAS, "target": "W9", "field": "thickness_mm",
                                          "value_source": "majority:interior", "reason": "そろえる"}).is_error
    r = mt.call("rebuild", {"alias": ALIAS})
    assert not r.is_error, r.text
    assert r.data["rebuilt"] and r.data["overrides"] == [{"id": "OV1", "status": "applied"}]   # 人の修正は当て直す
    assert (paths.out_dir(ALIAS) / "building.prev.json").exists()                              # 前の形は残す

    def no_glb(alias, company=None):
        raise FileNotFoundError(f"{alias} の glb：glb のフォルダに、glb がまだありません。")
    monkeypatch.setattr(wih, "build", no_glb)
    r = mt.call("rebuild", {"alias": ALIAS})
    assert r.is_error and r.data["error"] == f"組み立てられませんでした：{ALIAS} の glb：glb のフォルダに、glb がまだありません。"


def test_export_lists_files_by_name_only(data):
    d = paths.out_dir(ALIAS)
    t = (d / "building.json").stat().st_mtime - 100
    os.utime(d / "model" / "checks.json", (t, t))               # 書き出しが家より古い → 書き出し直す
    r = mt.call("export", {"alias": ALIAS})
    assert not r.is_error, r.text
    names = [f["name"] for f in r.data["files"]]
    assert r.data["rewritten"] is True and "model/model.3mf" in names and all(n.startswith("model/") for n in names)
    _no_paths(r.text, data)
    assert mt.call("export", {"alias": ALIAS}).data["rewritten"] is False


def test_open_in_hakoya_url_follows_the_mode_and_port(data, monkeypatch):
    assert mt.hakoya_port() == 8766                              # 既定は他社利用用の口
    monkeypatch.setenv("HAKOYA_MODE", "sample")
    assert mt.hakoya_port() == 8767
    monkeypatch.setenv("HAKOYA_MODE", "own")
    assert mt.hakoya_port() == 8765
    monkeypatch.setenv("HAKOYA_PORT", "9")                       # 口の番号が決まっていれば、それを使う
    r = mt.call("open_in_hakoya", {"alias": ALIAS}).data
    assert r["url"] == f"http://127.0.0.1:9/?alias={quote(ALIAS, safe='')}" and r["hakoya_running"] is False
    assert r["url"].isascii()


# ---------------------------------------------------------------- 守り（エラーの文・出してはいけない言葉）

def test_unexpected_errors_return_only_the_kind(data, monkeypatch):
    secret = r"C:\Users\someone\お客様の家\秘密の図面.jww"

    def boom(*a, **k):
        raise OSError(secret)
    monkeypatch.setattr(fd, "load", boom)
    r = mt.call("get_findings", {"alias": ALIAS})
    assert r.is_error and "OSError" in r.data["error"] and "思いがけないエラー" in r.data["error"]
    assert "秘密" not in r.text and "someone" not in r.text
    assert mt._explain(KeyError("W3 が見つかりません。"), "x") == "W3 が見つかりません。"     # HAKOYA が書いた文はそのまま
    assert mt._explain(ValueError(secret), "記録できませんでした") == "記録できませんでした（ValueError）"
    assert mt._explain(ValueError("架空邸の平面図.jww が読めません"), "x") == "x（ValueError）"   # お客様のファイルの名前
    assert mt._explain(ValueError("glb を読めません"), "x", prefix="組み立て：") == "組み立て：glb を読めません"
    assert mt._explain(RuntimeError("could not read"), "x") == "x（RuntimeError）"             # 部品の英語の文は出さない


def test_privacy_words_are_never_sent_or_written(data):
    r = mt.call("report_findings", {"alias": ALIAS, "findings": [_finding(message=f"{NG} 様の家の内壁が厚い")], "summary": "x"})
    assert r.is_error and "出してはいけない言葉" in r.data["error"] and NG not in r.text
    assert not fd.findings_path(ALIAS).exists()
    r = mt.call("ask_question", {"alias": ALIAS, "question": f"{NG} さんの窓は？", "options": ["a", "b"]})
    assert r.is_error and NG not in r.text and not (paths.out_dir(ALIAS) / mt.QUESTIONS_FILE).exists()
    leak = mt._ok({"where": str(data / "out" / ALIAS)})          # 場所が結果に混ざったら出さない
    assert leak.is_error and str(data) not in leak.text
    (data / "local_paths.yaml").write_text(f"ng_words: {NG}\n{ALIAS}: {{}}\n", encoding="utf-8")   # 並びになっていない
    r = mt.call("get_building", {"alias": ALIAS})                # 確かめられないときは出さない（直し方だけ伝える）
    assert r.is_error and privacy.NG_FORMAT_MESSAGE in r.data["error"] and NG not in r.text


# ---------------------------------------------------------------- 道具の一覧・プロンプト

def test_tool_list_marks_the_changing_tools_for_human_approval():
    import jsonschema

    from core.ai.agent import FINDINGS_SCHEMA
    assert {n for n, d in mt.TOOLS.items() if d.ask_human} == CHANGE_TOOLS
    assert {n for n, d in mt.TOOLS.items() if d.read_only} == READ_TOOLS
    for d in mt.TOOLS.values():
        jsonschema.Draft202012Validator.check_schema(d.schema)                  # Claude Code が受け付ける形
        assert all(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", k) for k in d.schema["properties"])
        assert re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", d.description)       # 説明は日本語
        assert d.name == "list_properties" or d.schema["required"][0] == "alias"
    assert all(mt.TOOLS[n].description.startswith(mt.HUMAN_ONLY) for n in CHANGE_TOOLS)
    assert "human_text" not in json.dumps(FINDINGS_SCHEMA)                     # アプリの検品の形は変えていない


def test_inspect_prompt_explains_the_six_faces_and_the_report(data):
    text = mt.get_prompt("inspect", {"alias": ALIAS})
    for word in ("report_findings", "source=glb", "南の立面", "印刷", "human_text", f'alias="{ALIAS}"',
                 '<skill name="inspection">', "get_code_findings"):
        assert word in text, word
    assert "list_properties" in mt.get_prompt("inspect", {}) and "登録されていません" in mt.get_prompt("inspect", {"alias": "物件Z"})
    assert mt.get_prompt("nothing", {}) is None


# ---------------------------------------------------------------- 本物の stdio でつなぐ

def test_stdio_server_end_to_end(template, tmp_path):
    """別のプロセスで python -m app.mcp_server を起動し、MCP の相手からつなぐ（Claude Code と同じつなぎ方）。
    docs/mcp.md の Python 版と同じく、HAKOYA の外の作業用フォルダから、PYTHONPATH で HAKOYA を指して起動する。"""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    root = tmp_path / "data"
    shutil.copytree(template, root)
    work = tmp_path / "work"                                     # 会社の人の作業用フォルダ（HAKOYA の外）
    work.mkdir()
    env = {"HAKOYA_DATA_DIR": str(root), "PYTHONUTF8": "1", "HAKOYA_LEARNED_RULES": "off", "HAKOYA_MODE": "other",
           "PYTHONPATH": str(REPO_ROOT)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], env=env, cwd=str(work))
    errlog = tmp_path / "server_stderr.txt"

    async def session():
        with errlog.open("w", encoding="utf-8") as err:
            async with stdio_client(params, errlog=err) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=120) as s:
                    init = await s.initialize()
                    tools = await s.list_tools()
                    props = await s.call_tool("list_properties", {})
                    image = await s.call_tool("render_view", {"alias": ALIAS, "view": "S"})
                    bad = await s.call_tool("get_building", {"alias": "../data"})
                    prompts = await s.list_prompts()
                    prompt = await s.get_prompt("inspect", {"alias": ALIAS})
                    return init, tools, props, image, bad, prompts, prompt

    init, tools, props, image, bad, prompts, prompt = asyncio.run(asyncio.wait_for(session(), 240))
    assert init.server_info.name == "hakoya"
    assert {t.name for t in tools.tools} == set(mt.TOOLS)
    ask = {t.name for t in tools.tools if (t.meta or {}).get("anthropic/requiresUserInteraction") is True}
    assert ask == CHANGE_TOOLS
    assert all(t.annotations.read_only_hint for t in tools.tools if t.name in READ_TOOLS)
    data = json.loads(props.content[0].text)
    assert not props.is_error and [p["alias"] for p in data["properties"]] == [ALIAS, OTHER]
    assert not image.is_error and [c.type for c in image.content] == ["text", "image"]
    assert image.content[1].mime_type == "image/png" and "image_path" not in image.content[0].text
    _no_paths(image.content[0].text, root)
    assert bad.is_error and "list_properties" in json.loads(bad.content[0].text)["error"]
    assert [p.name for p in prompts.prompts] == ["inspect"]
    assert "report_findings" in prompt.messages[0].content.text
    log = errlog.read_text(encoding="utf-8")
    assert "MCP サーバーを起動しました" in log and str(root) not in log and str(Path.home()) not in log
