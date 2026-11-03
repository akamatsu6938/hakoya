"""会社ルールの言語化（core/ai/rule_draft.py・学んだルール）を確かめる。Claude は呼ばない（偽の相手）。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from core import rules as rules_mod
from core.ai import rule_draft as rd
from core.ai.tools import ToolError
from core.rule_stats import compute
from core.rules import load_rules


def _house(ext=162, windows=(2400,), exceptions=()):
    return {"source": {"company_rules": "jisha"}, "ground": {"gl_mm": 0, "foundation_top_mm": 480},
            "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
            "wall_stats_by_level": {"1F": {"exterior_thickness_measured_mm": ext, "interior_gap_measured_mm": 145}},
            "walls": [], "glb_exceptions": [{"kind": k} for k in exceptions],
            "openings": [{"id": f"O{i}", "kind": "window", "head_fl_mm": h, "sill_fl_mm": 900} for i, h in enumerate(windows, 1)],
            "roofs": [{"id": "Y1", "kind": "main", "shape": "shed", "pitch": [1.5, 10], "thickness_mm": 100,
                       "eave_mm": {"W": 1, "E": 1, "S": 1, "N": 1}}]}


ANSWERS = [{"id": "A9", "property": "物件B", "question": "FL+400 から始まる吊り戸4つはどうするか", "answer": "床から",
            "applied_as": "FL+100〜600 で始まるドアは床からにする"}]


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(rules_mod, "LEARNED_DIR", tmp_path)
    items = [("物件A", _house(ext=150, windows=(2000, 2000, 2400), exceptions=("agari_kamachi",))),
             ("物件B", _house(ext=150, windows=(2400, 2400), exceptions=("agari_kamachi",)))]
    rules = load_rules("jisha", learned=False)

    def make():
        return rd.RuleContext(company="jisha", rules=load_rules("jisha"), stats=compute(items, rules, {}),
                              answers=list(ANSWERS), learned=rules_mod.load_learned("jisha"), aliases=["物件A", "物件B"])
    monkeypatch.setattr(rd.RuleContext, "load", classmethod(lambda cls, company: make()))
    return make()


# ---------------------------------------------------------------- 案を確かめる（数値はコード）

def test_value_proposal_takes_the_number_from_the_statistics(ctx):
    p = rd.validate_proposal(ctx, "value", "外壁の厚みは 150mm（2件中2件）", ["walls.exterior_measured"],
                             rule_key="walls.exterior_thickness_mm", value_source="stat:walls.exterior_measured:mode")
    assert (p["from"], p["to"]) == (162, 150) and "2件中2件で 150" in p["evidence"]


def test_bad_proposals_are_refused(ctx):
    with pytest.raises(ToolError, match="根拠にした集計・答えにありません"):          # Claude が作った数値
        rd.validate_proposal(ctx, "value", "外壁の厚みは 175mm", ["walls.exterior_measured"],
                             rule_key="walls.exterior_thickness_mm", value_source="stat:walls.exterior_measured:mode")
    with pytest.raises(ToolError, match="学ばない"):                                 # 窓の上端は家ごとに変わるので学ばない
        rd.validate_proposal(ctx, "value", "窓の上端", ["openings.window_head"],
                             rule_key="openings.window_top_default_fl_mm", value_source="stat:openings.window_head:mode")
    ctx.stat("levels.ceiling")["status"] = "varies"                                    # 物件ごとに違う項目は値にしない
    with pytest.raises(ToolError, match="会社で同じ"):
        rd.validate_proposal(ctx, "value", "天井高", ["levels.ceiling"],
                             rule_key="levels.ceiling_height_mm", value_source="stat:levels.ceiling:mode")
    with pytest.raises(ToolError, match="学ばない"):                                 # 模型の作り方は学ばない
        rd.validate_proposal(ctx, "value", "模型", ["walls.exterior_measured"], rule_key="model.scale",
                             value_source="stat:walls.exterior_measured:mode")
    with pytest.raises(ToolError, match="根拠がありません"):
        rd.validate_proposal(ctx, "skill", "なんとなく")


def test_skill_wording_may_use_numbers_from_the_answer(ctx):
    p = rd.validate_proposal(ctx, "skill", "FL+400 から始まる室内の戸は、床からのドアにする", answer_ids=["A9"])
    assert p["signature"] == "skill:A9" and "床から" in p["evidence"]


# ---------------------------------------------------------------- 動かす（off・偽の API）

class FakeClient:
    """1回目：作った数値の案（断られる）、2回目：正しい案、3回目：まとめの文。"""

    def __init__(self):
        self.calls = []
        self.messages = self
        self.script = [
            ("propose_rule", {"kind": "value", "wording": "外壁は 175mm にする", "stat_ids": ["walls.exterior_measured"],
                              "rule_key": "walls.exterior_thickness_mm", "value_source": "stat:walls.exterior_measured:mode"}),
            ("propose_rule", {"kind": "value", "wording": "外壁の厚みは 150mm（2件中2件）", "stat_ids": ["walls.exterior_measured"],
                              "rule_key": "walls.exterior_thickness_mm", "value_source": "stat:walls.exterior_measured:mode"}),
        ]

    async def create(self, **kw):
        self.calls.append(kw)
        usage = NS(input_tokens=3000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        if self.script:
            name, args = self.script.pop(0)
            return NS(content=[NS(type="tool_use", id=f"tu{len(self.calls)}", name=name, input=args)],
                      stop_reason="tool_use", usage=usage)
        return NS(content=[NS(type="text", text="外壁の厚みの案を1件出しました。")], stop_reason="end_turn", usage=usage)


def test_off_uses_code_candidates_without_claude(ctx):
    res = asyncio.run(rd.run_draft("jisha", "off", ctx=ctx))
    kinds = {p["signature"]: p["kind"] for p in res["proposals"]}
    assert kinds["value:walls.exterior_thickness_mm"] == "value" and kinds["glb:openings.window_head"] == "glb"
    assert all(p["origin"] == "code" for p in res["proposals"])


def test_api_draft_refuses_invented_numbers_and_uses_only_rule_tools(ctx):
    client = FakeClient()
    res = asyncio.run(rd.run_draft("jisha", "api_key", client=client, ctx=ctx))
    assert [p["to"] for p in res["proposals"]] == [150]
    assert {t["name"] for t in client.calls[0]["tools"]} == set(rd.RULE_TOOL_SPECS)
    msgs = client.calls[-1]["messages"]            # やりとりの一覧（0：頼み、1：1回目の道具、2：その答え …）
    refused = msgs[2]["content"][0]
    assert refused["is_error"] and "根拠にした集計・答えにありません" in refused["content"][0]["text"]
    assert not msgs[4]["content"][0]["is_error"]    # 2回目の正しい案は通る
    assert "物件 2 件" in client.calls[0]["messages"][0]["content"]


def test_plan_connection_uses_only_rule_tools(ctx, monkeypatch):
    import claude_agent_sdk as sdk
    seen = {}

    class FakeResult:
        is_error, result, total_cost_usd, subtype = False, "案を出しました", 0.12, "success"

    async def fake_query(prompt, options):
        seen["options"] = options
        yield FakeResult()
    monkeypatch.setattr(sdk, "query", fake_query)
    monkeypatch.setattr(sdk, "ResultMessage", FakeResult)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = asyncio.run(rd.run_draft("jisha", "claude_code", ctx=ctx))
    o = seen["options"]
    assert o.allowed_tools == ["Skill"] + [f"mcp__hakoya__{n}" for n in rd.RULE_TOOL_SPECS]
    assert o.skills == ["company-jisha"] and res["cost_usd"] == 0.12
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x" * 30)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        asyncio.run(rd.run_draft("jisha", "claude_code", ctx=ctx))


# ---------------------------------------------------------------- 承認・却下・取り消し（学んだルール）

def test_approve_reject_undo_and_overlay(ctx, monkeypatch):
    monkeypatch.setenv("HAKOYA_LEARNED_RULES", "on")
    item = rd.approve("jisha", {"kind": "value", "wording": "外壁の厚みは 150mm（2件中2件）", "stat_ids": ["walls.exterior_measured"],
                                 "rule_key": "walls.exterior_thickness_mm", "value_source": "stat:walls.exterior_measured:mode",
                                 "to": 999})                                     # 画面から来た数値は使わない
    learned = rules_mod.load_learned("jisha")
    assert learned["values"]["walls.exterior_thickness_mm"]["value"] == 150 and item["id"] == "L1"
    assert load_rules("jisha")["walls"]["exterior_thickness_mm"] == 150              # 重ねる
    assert load_rules("jisha", learned=False)["walls"]["exterior_thickness_mm"] == 162
    note = rd.approve("jisha", {"kind": "skill", "wording": "FL+400 から始まる室内の戸は、床からのドアにする", "answer_ids": ["A9"]})
    assert "床からのドア" in rules_mod.learned_skill_section("jisha")
    with pytest.raises(ValueError, match="すでに承認か却下"):
        rd.approve("jisha", {"kind": "skill", "wording": "FL+400 から始まる戸は床から", "answer_ids": ["A9"]})
    rd.reject("jisha", "glb:openings.window_head")
    assert "glb:openings.window_head" not in {p["signature"] for p in asyncio.run(rd.run_draft("jisha", "off", ctx=rd.RuleContext.load("jisha")))["proposals"]}
    rd.undo("jisha", item["id"])
    rd.undo("jisha", note["id"])
    assert rules_mod.load_learned("jisha")["values"] == {} and rules_mod.learned_skill_section("jisha") == ""


def test_learned_notes_reach_the_skill_copy_but_not_skills_folder(ctx, monkeypatch):
    from core.ai.agent import SKILLS_DIR, prepare_workdir
    from core.ai.api_runner import skill_text
    monkeypatch.setenv("HAKOYA_LEARNED_RULES", "on")
    rd.approve("jisha", {"kind": "skill", "wording": "FL+400 から始まる室内の戸は、床からのドアにする", "answer_ids": ["A9"]})
    before = (SKILLS_DIR / "company-jisha" / "SKILL.md").read_text(encoding="utf-8")
    work = prepare_workdir("rules-test", "jisha", skills_used=())
    copy = (work / ".claude" / "skills" / "company-jisha" / "SKILL.md").read_text(encoding="utf-8")
    assert "学んだ会社の決まり" in copy and "床からのドア" in copy
    assert "床からのドア" in skill_text("jisha", names=())
    assert (SKILLS_DIR / "company-jisha" / "SKILL.md").read_text(encoding="utf-8") == before   # skills/ は書き換えない


def test_learned_values_are_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(rules_mod, "LEARNED_DIR", tmp_path)
    (tmp_path / "jisha.learned.json").write_text(json.dumps({"values": {
        "model.scale": {"value": 10}, "walls.exterior_thickness_mm": {"value": "厚い"},
        "levels.story_height_choices_mm": {"value": [2850, 3000, 3150]}}}), encoding="utf-8")
    merged, skipped = rules_mod.apply_learned(load_rules("jisha", learned=False), rules_mod.load_learned("jisha"))
    assert merged["model"]["scale"] == 50 and merged["walls"]["exterior_thickness_mm"] == 162      # 学べない項目・型の違いは重ねない
    assert merged["levels"]["story_height_choices_mm"] == [2850, 3000, 3150]
    assert {s["rule_key"] for s in skipped} == {"model.scale", "walls.exterior_thickness_mm"}
    assert load_rules("jisha")["levels"]["story_height_choices_mm"] == [2850, 3000]              # テストでは使わない（off）
