"""api_key 接続の検品（core/ai/api_runner.py）を、偽の API の相手で確かめる。
Claude は呼ばない（料金はかからない）。実データも要らない（架空の家を使う）。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from core.ai import api_runner as ar
from core.ai.agent import FACES, FINDINGS_SCHEMA
from core.ai.progress import read_progress
from core.ai.tools import TOOL_SPECS, ToolContext
from core.rules import load_rules

MERGED = {"summary": "指摘1件", "findings": [{
    "view": "plan", "kind": "rule", "severity": "低", "message": "W3 の厚みが、ほかの内壁とそろっていない",
    "targets": ["W3"], "evidence": ["get_elements"], "confidence": "中", "question": None,
    "proposal": {"target": "W3", "field": "thickness_mm", "value_source": "majority:interior", "number": None,
                 "reason": "内壁の厚みをそろえる"}}]}


def _ctx() -> ToolContext:
    walls = [{"id": "W1", "kind": "interior", "thickness_mm": 130, "level": "1F"},
             {"id": "W2", "kind": "interior", "thickness_mm": 130, "level": "1F"},
             {"id": "W3", "kind": "interior", "thickness_mm": 145, "level": "1F"},
             {"id": "W4", "kind": "exterior", "thickness_mm": 162, "level": "1F"}]
    raw = {"source": {"company_rules": "jisha"}, "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
           "ground": {"foundation_top_mm": 480}, "walls": walls, "openings": [], "roof": {}}
    return ToolContext(alias="物件T", raw=raw, building=json.loads(json.dumps(raw)), rules=load_rules("jisha"))


def _usage(i, o, cw=0, cr=0):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


class FakeClient:
    """Messages API の偽物。面の担当には「道具を1回使ってから報告」、まとめ役には決めた JSON を返す。"""

    def __init__(self, refuse=None, error=None):
        self.calls: list[dict] = []
        self.messages = self            # client.messages.create(...) の形にする
        self.refuse = refuse            # この面の担当は答えを断る
        self.error = error              # 呼ばれたらこのエラーを出す

    async def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        if "format" in (kw.get("output_config") or {}):
            return NS(content=[NS(type="text", text=json.dumps(MERGED, ensure_ascii=False))], stop_reason="end_turn",
                      usage=_usage(5000, 800))
        first = kw["messages"][0]["content"]
        if self.refuse and self.refuse in first:
            return NS(content=[], stop_reason="refusal", usage=_usage(100, 0))
        if len(kw["messages"]) == 1:
            name, inp = ("render_view", {"view": "S", "source": "model"}) if "南の立面" in first else ("get_building", {})
            return NS(content=[NS(type="thinking", thinking="", signature="sig"),
                               NS(type="tool_use", id=f"tu{len(self.calls)}", name=name, input=inp)],
                      stop_reason="tool_use", usage=_usage(3000, 100, cw=2500))
        return NS(content=[NS(type="text", text="指摘なし")], stop_reason="end_turn", usage=_usage(500, 50, cr=2500))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """図を描く道具とコードの検査は、架空の家用に差し替える（図は小さな PNG）。"""
    from PIL import Image
    png = tmp_path / "S_model.png"
    Image.new("RGB", (8, 8), "white").save(png)
    real = ar.call_tool

    def call_tool(ctx, name, args):
        if name == "render_view":
            return {"view": args["view"], "image_path": str(png)}
        return real(ctx, name, args)
    monkeypatch.setattr(ar, "call_tool", call_tool)
    monkeypatch.setattr(ar, "code_findings", lambda alias: [])
    return tmp_path


def _run(client, out):
    return asyncio.run(ar.run_inspection_api("物件T", client=client, ctx=_ctx(), out=out))


def test_inspection_with_fake_api(env):
    client = FakeClient()
    rep = _run(client, env)
    assert rep["connection"] == "api_key" and rep["turns"] == 13          # 6面×2回 ＋ まとめ1回
    assert rep["faces"] == {f: "済み" for f in FACES}
    # 直す案の数値は Claude ではなくコードが計算する（内壁の多数派 130）
    assert rep["findings"][0]["proposal"]["to"] == 130 and rep["findings"][0]["id"] == "F1"
    # 費用：面（Sonnet 5.5）は 1回目 3000入力＋100出力＋2500書き込み、2回目 500入力＋50出力＋2500読み直し。まとめ（Opus 5.5）5000＋800
    # 読み直しは Sonnet 5.5 で $0.10／100万トークン（11/4 公式の料金表で確かめて直した。前は 0.2）
    face = (3000 * 2 + 100 * 10 + 2500 * 2.5) + (500 * 2 + 50 * 10 + 2500 * 0.1)
    assert rep["cost_usd"] == pytest.approx(round((6 * face + 5000 * 4 + 800 * 20) / 1e6, 4))
    assert json.loads((env / "findings.json").read_text(encoding="utf-8"))["findings"][0]["id"] == "F1"


def test_requests_use_only_app_tools_and_cache(env):
    client = FakeClient()
    _run(client, env)
    faces = [c for c in client.calls if "format" not in c["output_config"]]
    assert len(faces) == 12
    for c in faces:
        assert {t["name"] for t in c["tools"]} == set(TOOL_SPECS)           # HAKOYA の道具だけ
        assert c["model"] == "claude-sonnet-5-5" and c["output_config"] == {"effort": "medium"}
        assert c["cache_control"] == {"type": "ephemeral"}
        assert c["system"] == faces[0]["system"] and c["tools"] == faces[0]["tools"]   # 覚えさせる部分は全員同じ
    system = faces[0]["system"][0]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert '<skill name="inspection">' in system["text"] and '<skill name="company-jisha">' in system["text"]
    # 2回目：考えた中身（thinking）をそのまま返し、道具の結果に画像を付ける
    second = next(c for c in faces if len(c["messages"]) == 3 and "南の立面" in c["messages"][0]["content"])
    assert second["messages"][1]["content"][0].type == "thinking"
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["is_error"] is False
    assert result["content"][1]["source"]["media_type"] == "image/png" and "image_path" not in result["content"][0]["text"]
    merge = next(c for c in client.calls if "format" in c["output_config"])
    assert merge["model"] == "claude-opus-5-5" and merge["output_config"]["effort"] == "high"
    assert merge["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_progress_reads_api_log(env):
    _run(FakeClient(), env)
    p = read_progress(env / "inspect_log.jsonl")
    assert p["finished"] and p["phase"] == "終わり"
    assert all(f["state"] == "済み" for f in p["faces"].values())


def test_one_refusal_does_not_stop_the_rest(env):
    rep = _run(FakeClient(refuse="北の立面"), env)
    assert rep["faces"]["N"] == "失敗" and rep["faces"]["S"] == "済み"
    assert read_progress(env / "inspect_log.jsonl")["faces"]["N"]["state"] == "失敗"


def test_budget_limit_stops_before_next_call(env, monkeypatch):
    conf = ar.load_ai_models()
    conf["limits"]["max_budget_usd"] = 0.01
    monkeypatch.setattr(ar, "load_ai_models", lambda: conf)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="上限"):
        _run(client, env)
    assert len(client.calls) == 1                     # 1回目で上限を超えたので、次は呼ばない
    assert not (env / "findings.json").exists()
    last = json.loads((env / "inspect_log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["type"] == "ApiResult" and last["is_error"] and last["cost_usd"] > 0.01


def test_wrong_key_message_is_japanese(env):
    import anthropic
    import httpx
    err = anthropic.AuthenticationError("invalid x-api-key", response=httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
    with pytest.raises(RuntimeError, match="API キーが正しくありません"):
        _run(FakeClient(error=err), env)


def test_no_key_is_refused_before_calling(env, monkeypatch):
    from core import secret_store
    monkeypatch.setattr(secret_store, "get_api_key", lambda: None)
    with pytest.raises(RuntimeError, match="API キーが設定されていません"):
        asyncio.run(ar.run_inspection_api("物件T", ctx=_ctx(), out=env))


def test_schema_for_structured_output():
    s = ar.api_schema(FINDINGS_SCHEMA)

    def walk(x):
        if isinstance(x, dict):
            assert not isinstance(x.get("type"), list)                       # 型の並びは anyOf に直す
            if x.get("type") == "object":
                assert x["additionalProperties"] is False
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(s)
    number = s["properties"]["findings"]["items"]["properties"]["proposal"]["anyOf"][1]["properties"]["number"]
    assert number == {"anyOf": [{"type": "number"}, {"type": "null"}]}


def test_face_prompt_and_findings_for_two_storeys():
    """2階建てでは、平面の担当が1階（plan）と2階（plan2）の両方を見る。平屋の指摘に plan2 が来たら plan に直す。"""
    assert "plan2" not in ar.face_prompt("plan") and "plan2" in ar.face_prompt("plan", multi=True)
    assert "plan2" not in ar.face_prompt("S", multi=True)
    assert "plan2" in FINDINGS_SCHEMA["properties"]["findings"]["items"]["properties"]["view"]["enum"]
    from core.ai.agent import finalize
    f = finalize(_ctx(), [{"view": "plan2", "kind": "shape", "severity": "低", "message": "x", "targets": [], "evidence": [],
                           "confidence": "低"}])
    assert f[0]["view"] == "plan"
