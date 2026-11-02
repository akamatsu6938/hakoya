"""囲んで直す（core/lasso.py・core/ai/lasso.py・画面の口）を確かめる。
Claude は呼ばない（偽の API の相手と、偽の Agent SDK）。囲みの判定と承認は架空の家で確かめる。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from urllib.parse import quote

import pytest

from core import lasso as geo
from core.ai import lasso as al
from core.ai.tools import TOOL_SPECS, ToolContext
from core.rules import load_rules

TEXT = "この壁厚だけおかしい。ほかとそろえて"


def _building() -> dict:
    def wall(i, kind, t, a, b, bottom=624):
        return {"id": i, "kind": kind, "thickness_mm": t, "bottom_mm": bottom, "start": a, "end": b, "level": "1F"}
    walls = [wall("W1", "exterior", 162, [0, 0], [6000, 0], 480), wall("W2", "exterior", 162, [6000, 0], [6000, 4000], 480),
             wall("W3", "interior", 145, [3000, 0], [3000, 4000]), wall("W4", "interior", 130, [0, 2000], [3000, 2000]),
             wall("W5", "interior", 130, [3000, 2000], [6000, 2000])]
    ops = [{"id": "O1", "wall": "W1", "kind": "window", "width_mm": 1690, "offset_mm": 500, "sill_fl_mm": 900,
            "head_fl_mm": 2000, "center": [1345, 0], "head_mm": 2624, "sill_mm": 1524}]
    return {"source": {"company_rules": "jisha"}, "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
            "ground": {"foundation_top_mm": 480}, "walls": walls, "openings": ops, "roof": {}}


def _ctx() -> ToolContext:
    b = _building()
    return ToolContext(alias="物件T", raw=b, building=json.loads(json.dumps(b)), rules=load_rules("jisha"))


def box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


# ---------------------------------------------------------------- 囲みの中の部材（コードが決める）

def test_part_of_a_long_wall_is_picked_but_touching_walls_are_not():
    # W3 の 3,000mm 分を囲む。直交する W4・W5 は囲みの縁に 300mm かかるだけなので選ばない
    assert geo.select(_building(), box(2700, 500, 3300, 3500)) == {"walls": ["W3"], "openings": []}


def test_opening_by_its_center():
    # 窓 O1 だけを囲む（その壁 W1 は 700mm しか入らないので選ばない）
    assert geo.select(_building(), box(1000, -300, 1700, 300)) == {"walls": [], "openings": ["O1"]}


def test_corner_only_picks_nothing():
    assert geo.select(_building(), box(5800, -200, 6200, 200)) == {"walls": [], "openings": []}


def test_bad_lassos():
    # 8の字の囲みも直して使う（2つの三角が W1 の 2,000mm 分を囲んでいる）
    assert geo.select(_building(), [[0, -500], [2000, 500], [2000, -500], [0, 500]])["walls"] == ["W1"]
    with pytest.raises(ValueError, match="点が足りません"):
        geo.select(_building(), [[0, 0], [10, 10]])
    with pytest.raises(ValueError, match="小さすぎ"):
        geo.select(_building(), box(0, 0, 50, 50))


def test_describe_rows():
    rows = geo.describe(_building(), {"walls": ["W3"], "openings": ["O1"]})
    assert rows[0]["id"] == "W3" and rows[0]["label"] == "内壁" and "厚み 145mm" in rows[0]["text"]
    assert rows[1]["label"] == "窓" and "FL+900〜FL+2000" in rows[1]["text"]


# ---------------------------------------------------------------- Claude に頼む（偽の相手）

def _usage(i, o):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class FakeClient:
    """1回目に道具を1つ使い、2回目に短い説明を返す。"""

    def __init__(self, tool_name, tool_input):
        self.calls: list[dict] = []
        self.messages = self
        self.tool = (tool_name, tool_input)

    async def create(self, **kw):
        self.calls.append(kw)
        if len(kw["messages"]) == 1:
            return NS(content=[NS(type="tool_use", id="tu1", name=self.tool[0], input=self.tool[1])],
                      stop_reason="tool_use", usage=_usage(4000, 200))
        return NS(content=[NS(type="text", text="W3 の厚みを、ほかの内壁にそろえる案を出しました。")],
                  stop_reason="end_turn", usage=_usage(1000, 100))


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "out_dir", lambda a: tmp_path)
    return tmp_path


def _ask(client, connection="api_key", points=None, text=TEXT):
    return asyncio.run(al.run_lasso("物件T", connection, points or box(2700, 500, 3300, 3500), text,
                                    client=client, ctx=_ctx()))


def test_proposal_numbers_come_from_code(out):
    client = FakeClient("propose_override", {"target": "W3", "field": "thickness_mm", "value_source": "majority:interior",
                                             "reason": "ほかの内壁にそろえる"})
    res = _ask(client)
    assert [r["id"] for r in res["selection"]] == ["W3"]
    p = res["proposals"][0]
    assert (p["from"], p["to"], p["value_source"]) == (145, 130, "majority:interior")   # 130 は内壁の多数派（コードが計算）
    first = client.calls[0]
    assert {t["name"] for t in first["tools"]} == set(TOOL_SPECS)                          # HAKOYA の道具だけ
    assert first["model"] == "claude-sonnet-5-5" and TEXT in first["messages"][0]["content"] and "W3" in first["messages"][0]["content"]
    assert res["cost_usd"] == pytest.approx((5000 * 2 + 300 * 10) / 1e6)
    log = [json.loads(x) for x in (out / "lasso_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[0]["selection"] == ["W3"] and log[0]["proposals"][0]["to"] == 130


def test_question_when_unclear(out):
    client = FakeClient("ask_question", {"question": "壁が2枚囲まれています。どちらですか？", "options": ["W4", "W5"]})
    res = _ask(client, points=box(500, 1700, 5500, 2300))         # 縦の W3 は 600mm しか入らないので選ばない
    assert {r["id"] for r in res["selection"]} == {"W4", "W5"}
    assert res["questions"][0]["options"] == ["W4", "W5"] and res["proposals"] == []


def test_off_and_empty_do_not_call_claude(out):
    client = FakeClient("get_building", {})
    res = _ask(client, connection="off")
    assert res["message"].startswith("AI なし") and res["proposals"] == [] and client.calls == []
    res = _ask(client, points=box(5800, -200, 6200, 200))
    assert res["selection"] == [] and "部材がありません" in res["message"] and client.calls == []
    with pytest.raises(ValueError, match="一言"):
        _ask(client, text="  ")


def test_plan_connection_uses_only_app_tools(out, monkeypatch):
    import claude_agent_sdk
    seen = {}

    class FakeResult:
        is_error, subtype, result, total_cost_usd = False, "success", "W3 の案を出しました。", 0.12

    async def fake_query(prompt, options):
        seen["options"] = options
        yield FakeResult()
    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(claude_agent_sdk, "ResultMessage", FakeResult)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = _ask(None, connection="claude_code")
    o = seen["options"]
    assert o.tools == ["Skill"] and set(o.allowed_tools) == {"Skill"} | {f"mcp__hakoya__{n}" for n in TOOL_SPECS}
    assert o.model == "claude-opus-5-5" and o.permission_mode == "dontAsk" and o.max_budget_usd == 1.0
    assert res["message"] == "W3 の案を出しました。" and res["cost_usd"] == 0.12
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        _ask(None, connection="claude_code")


# ---------------------------------------------------------------- 承認（数値はサーバーが計算し直す）

@pytest.fixture
def alias(tmp_path, monkeypatch):
    from core import overrides
    from core.ai import tools
    monkeypatch.setattr(tools, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(overrides, "out_dir", lambda a: tmp_path / a)
    (tmp_path / "物件T").mkdir()
    (tmp_path / "物件T" / "building.json").write_text(json.dumps(_building(), ensure_ascii=False), encoding="utf-8")
    return "物件T"


def test_approve_recomputes_and_records(alias):
    item = al.approve(alias, {"target": "W3", "field": "thickness_mm", "value_source": "majority:interior", "reason": "そろえる"})
    assert item["to"] == 130 and item["origin"] == "lasso"
    with pytest.raises(ValueError, match="すでに"):                      # 2回目は直す必要がない
        al.approve(alias, {"target": "W3", "field": "thickness_mm", "value_source": "majority:interior"})
    item = al.approve(alias, {"target": "O1", "field": "head_fl_mm", "value_source": "human:number:2200", "reason": "人の指定"})
    assert item["to"] == 2200
    with pytest.raises(ValueError, match="部材はありません"):
        al.approve(alias, {"target": "W99", "field": "thickness_mm", "value_source": "majority:interior"})


# ---------------------------------------------------------------- 画面の口（物件Aの実データ）

@pytest.mark.realdata
def test_endpoints_with_real_building():
    from fastapi.testclient import TestClient
    from app.server import app
    c = TestClient(app)
    A = quote("物件A")
    b = c.get(f"/api/building/{A}").json()["building"]
    w = max(b["walls"], key=lambda x: abs(x["end"][0] - x["start"][0]) + abs(x["end"][1] - x["start"][1]))   # いちばん長い壁
    mx, my = (w["start"][0] + w["end"][0]) / 2, (w["start"][1] + w["end"][1]) / 2
    poly = box(mx - 600, my - 600, mx + 600, my + 600)
    sel = c.post(f"/api/lasso/{A}/select", json={"polygon": poly}).json()["selection"]
    assert w["id"] in [r["id"] for r in sel]
    res = c.post(f"/api/lasso/{A}/ask", json={"polygon": poly, "text": "", "connection": "off"}).json()
    assert res["message"].startswith("AI なし") and res["proposals"] == []
    assert c.post(f"/api/lasso/{A}/select", json={"polygon": [[0, 0], [1, 1]]}).status_code == 400
    r = c.post(f"/api/lasso/{A}/approve", json={"target": "W99", "field": "thickness_mm", "value_source": "majority:interior"})
    assert r.status_code == 400 and "部材はありません" in r.json()["detail"]
