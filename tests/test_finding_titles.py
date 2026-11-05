"""検品の指摘の見出し（title）・対象の番号（targets）の確かめ・Claude に返す道具の結果の見張り（★11/5）。
実データは使わない（架空の物件T。building.json は数と番号だけ）。"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import pytest

from core import findings as fd
from core.ai.tools import ToolContext
from core.findings import TITLE_MAX, title_of
from core.ids import check_targets, target_ids
from core.rules import load_rules


# ---------------------------------------------------------------- 見出しの作り方

def test_title_is_kept_or_made_from_the_message():
    assert title_of({"title": " 西の窓の間の柱が細い ", "message": "x"}) == "西の窓の間の柱が細い"
    assert title_of({"message": "W3 の厚みが、ほかの内壁とそろっていない。glb では 130"}) == "W3 の厚みが、ほかの内壁とそろっていない"
    assert title_of({"message": "南の窓 O3（大きい窓（掃き出し））の上端が低い"}) == "南の窓 O3の上端が低い"   # かっこの中は外す
    long = title_of({"message": "あ" * 50})
    assert len(long) == TITLE_MAX and long.endswith("…")
    assert title_of({"title": "い" * 40})[-1] == "…" and len(title_of({"title": "い" * 40})) == TITLE_MAX
    assert title_of({}) == "指摘" and title_of({"message": "（かっこだけ）"}) == "（かっこだけ）"


# ---------------------------------------------------------------- コードの検査の指摘（core/findings.code_findings）

@pytest.fixture
def house(tmp_path, monkeypatch):
    from core import overrides
    monkeypatch.setattr(fd, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(overrides, "out_dir", lambda a: tmp_path / a)
    d = tmp_path / "物件T"
    (d / "model").mkdir(parents=True)
    building = {"levels": [{"name": "1F", "fl_mm": 624}],
                "walls": [{"id": "W1", "level": "1F", "kind": "interior", "thickness_mm": 145}], "openings": [],
                "glb_exceptions": [{"kind": "back_to_back_exterior_faces", "wall": "W1", "note": "背中合わせ"}],
                "unresolved": [{"id": "U1", "note": "向かい合う面が見つからない"}],
                "skipped": [{"source": "建具#7", "reason": "この建具が入る壁が見つからない"}]}
    (d / "building.json").write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    checks = {"checks": [{"part": "floor1", "level": "高", "item": "watertight", "message": "1階（床・内壁）：閉じた立体になっていません"},
                         {"part": "roof", "level": "中", "item": "islands", "message": "屋根：離れた塊が 2 個あります"},
                         {"part": "x", "level": "中", "item": "new_kind", "message": "新しい検査：何か。くわしくは別"},
                         {"part": "y", "level": "情報", "item": "volume", "message": "体積"}]}
    (d / "model" / "checks.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    return "物件T"


def test_every_code_finding_has_a_title(house):
    out = fd.code_findings(house)
    assert out and all(isinstance(f["title"], str) and 0 < len(f["title"]) <= TITLE_MAX for f in out)
    titles = [f["title"] for f in out]
    for want in ("glb の例外：背中合わせの外壁の面", "未解決の所 U1", "建具が入る壁が見つからない",
                 "1階が閉じた立体でない", "屋根に離れた塊がある", "新しい検査：何か"):     # 知らない項目は message から
        assert want in titles, (want, titles)


def test_consistency_titles_name_the_parts():
    from core.findings import CONSISTENCY_TITLES, _formatted
    assert _formatted(CONSISTENCY_TITLES["walls_overlap"], ["W1", "W2"]) == "W1 と W2 の壁が重なる"
    assert _formatted(CONSISTENCY_TITLES["walls_overlap"], ["W1"]) is None        # 番号が足りないときは作らない


def test_drawing_findings_have_titles(monkeypatch):
    from core import drawing_check as dc
    fake = {"levels": [{"kind": "plan", "mismatches": [{"opening": "O3", "what": "head", "note_id": "T1", "message": "上端が違う",
                                                         "model_head_fl_mm": 2200, "head_fl_mm": 2400, "code": "16511"},
                                                        {"opening": "O4", "what": "width", "note_id": "T2", "message": "幅が違う"}],
                        "unmatched_notes": [{"note_id": "T9", "code": "07405", "head_fl_mm": 2000}]}],
            "elevations": [{"view": "E", "score": 0.9, "mismatches": [{"opening": "O6", "edges": ["上端"]}]}]}
    monkeypatch.setattr(dc, "summary", lambda alias, b: fake)
    titles = [f["title"] for f in dc.findings("物件T", {})]
    assert titles == ["O3 の上端が図面の注記と違う", "O4 の幅が図面の注記と違う", "図面の注記の窓が模型にない",
                      "O6 が東の立面図と合わない"]


# ---------------------------------------------------------------- Claude の指摘の対象の番号（agent.finalize）

def _ctx() -> ToolContext:
    walls = [{"id": "W1", "kind": "interior", "thickness_mm": 130, "level": "1F"},
             {"id": "W2", "kind": "interior", "thickness_mm": 145, "level": "1F"}]
    ops = [{"id": "O3", "wall": "W1", "kind": "window", "width_mm": 1700, "offset_mm": 100, "sill_fl_mm": 1900,
            "head_fl_mm": 2400, "head_mm": 3024, "sill_mm": 2524}]
    roofs = [{"id": "Y1", "kind": "main", "eave_mm": {"S": 600, "N": 600}}]
    raw = {"source": {"company_rules": "bkoumuten"}, "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
           "ground": {"foundation_top_mm": 480}, "walls": walls, "openings": ops, "roofs": roofs, "roof": {}}
    return ToolContext(alias="物件T", raw=raw, building=json.loads(json.dumps(raw)), rules=load_rules("bkoumuten"))


def test_target_ids_follow_the_pick_shapes():
    assert target_ids(_ctx().building) == {"W1", "W2", "O3", "Y1", "Y1:S", "Y1:N"}
    keep, unknown = check_targets(["o3", "Ｗ１", "W1", "O3（南の窓）", "Y1:E", "Y1:S", "W99", "R1", "南の壁", 7, "O31"],
                                  target_ids(_ctx().building))
    assert keep == ["O3", "W1", "Y1", "Y1:S"]                     # 全角・小文字をそろえ、重ねない。軒の向きがなければ屋根
    assert unknown == ["W99", "R1", "南の壁", "7", "O31"]
    assert check_targets(None, {"W1"}) == ([], [])


def test_finalize_keeps_only_known_targets_and_makes_titles():
    from core.ai.agent import FINDINGS_SCHEMA, finalize
    item = FINDINGS_SCHEMA["properties"]["findings"]["items"]
    assert item["properties"]["title"] == {"type": "string"} and "title" in item["required"]   # 新しい検品では必ず書かせる
    out = finalize(_ctx(), [
        {"view": "S", "kind": "opening", "severity": "中", "title": "南の窓 O3 の上端が低い", "message": "O3 の上端が低い。",
         "targets": ["O3", "O99"], "evidence": [], "confidence": "中"},
        {"view": "plan", "kind": "shape", "severity": "低", "message": "W2 の厚みがそろっていない（130 と 145）。glb を見る",
         "targets": ["w2"], "evidence": [], "confidence": "低"}])
    a, b = out
    assert a["title"] == "南の窓 O3 の上端が低い" and a["targets"] == ["O3"] and a["unknown_targets"] == ["O99"]
    assert b["title"] == "W2 の厚みがそろっていない" and b["targets"] == ["W2"] and "unknown_targets" not in b


# ---------------------------------------------------------------- Claude に返す前の見張り（agent.make_tools）

def test_sdk_tools_check_results_before_giving_them_to_claude(monkeypatch, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    from PIL import Image

    import core.privacy
    from core.ai import agent
    png = tmp_path / "S_model.png"
    Image.new("RGB", (4, 4), "white").save(png)
    secret = "架空の施主の名前"
    monkeypatch.setattr(core.privacy, "ng_words", lambda path=None: [secret])

    def call(ctx, name, args):
        if name == "get_building":
            return {"property": "物件T", "note": f"{secret}様邸"}       # 出してはいけない言葉が混ざった結果
        return {"view": "S", "image_path": str(png)}
    tools = {t.name: t for t in agent.make_tools(_ctx(), call=call)}
    bad = asyncio.run(tools["get_building"].handler({}))
    text = json.dumps(bad, ensure_ascii=False)
    assert bad["is_error"] is True and secret not in text and "出してはいけない言葉" in text
    assert all(c["type"] == "text" for c in bad["content"])                    # 中身（画像も）は渡さない
    ok = asyncio.run(tools["render_view"].handler({"view": "S"}))
    assert ok["is_error"] is False and any(c["type"] == "image" for c in ok["content"])


# ---------------------------------------------------------------- 画面の口：前の検品の結果にも見出しを付けて返す

def test_old_reports_get_titles_on_the_screen(tmp_path, monkeypatch):
    import keyring
    from fastapi.testclient import TestClient

    from app import server
    from core import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "MODE", "own")
    monkeypatch.setattr(keyring, "get_password", lambda s, u: None)
    monkeypatch.setattr(server, "_aliases", lambda: ["物件T"])
    d = tmp_path / "out" / "物件T"
    d.mkdir(parents=True)
    old = {"findings": [{"id": "F1", "message": "南の窓 O3 の上端が低い。くわしくは図を見る", "status": "未確認"}],
           "code_findings": [{"id": "C1", "title": "未解決の所 U1", "message": "x", "status": "未確認"}]}
    (d / "findings.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    rep = TestClient(server.app).get("/api/findings/" + quote("物件T")).json()
    assert rep["findings"][0]["title"] == "南の窓 O3 の上端が低い" and rep["code_findings"][0]["title"] == "未解決の所 U1"
    assert "title" not in json.loads((d / "findings.json").read_text(encoding="utf-8"))["findings"][0]   # ファイルは書き換えない
