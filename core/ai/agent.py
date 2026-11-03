"""Claude による検品（SPEC §9-2）：claude_code 接続（Claude Agent SDK、作者本人のプラン）。

- 面ごとの検品担当（サブエージェント）6人：S・N・E・W・plan・print。まとめ役が並列に呼び、指摘を JSON にまとめる。
- 使える道具は HAKOYA の MCP（core/ai/tools.py）だけ。組み込みの道具は外し、PreToolUse フックでも止める（二重の守り）。
- Skill（会社ルール・模型ルール・検品）は、リポジトリの外の作業フォルダの .claude/skills に置いて読ませる
  （リポジトリの中だと、開発用の CLAUDE.md やフックまで読み込まれるため）。
- 直す案の数値は、結果を受け取った後にコードが出どころから計算し直す（SPEC §2-4）。
- claude_code 接続では API キーを使わない。環境に ANTHROPIC_API_KEY があれば止める（API の料金になるのを防ぐ）。

使い方:  python -m core.ai.agent 物件A   → data/out/物件A/findings.json
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from core.ai.tools import TOOL_SPECS, ToolContext, call_tool, propose_override, ToolError
from core.findings import code_findings
from core.paths import REPO_ROOT, out_dir

SERVER = "hakoya"
FACES = {"S": "南の立面", "N": "北の立面", "E": "東の立面", "W": "西の立面", "plan": "平面（屋根を外して上から）",
         "print": "印刷（閉じた立体・細すぎる所・橋渡し・三角・造形範囲）"}
SKILLS_DIR = REPO_ROOT / "skills"
# 2階建て：平面の担当が1階（plan）と2階（plan2）の両方を見る。指摘の view には plan2 も使える
PLAN2 = "plan2"
MULTI_PLAN_LABEL = "平面（1階は plan、2階は plan2。屋根・2階を外して上から）"
MULTI_PLAN_HOW = ("2階建てなので、1階の平面（view=plan：2階と屋根を外した図）と2階の平面（view=plan2：大屋根を外した図）の両方で、"
                  "模型の図と glb の図を render_view で見比べ、get_elements（view=plan / plan2）で部材の番号を確かめる。"
                  "2階の指摘の view は plan2 にする。")


def face_label(face: str, multi: bool = False) -> str:
    return MULTI_PLAN_LABEL if multi and face == "plan" else FACES[face]

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": list(FACES) + [PLAN2]},
                "kind": {"type": "string", "enum": ["shape", "opening", "roof", "outline", "rule", "print"]},
                "severity": {"type": "string", "enum": ["高", "中", "低"]},
                "message": {"type": "string"},
                "targets": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "proposal": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {
                    "target": {"type": "string"}, "field": {"type": "string"}, "value_source": {"type": "string"},
                    "number": {"type": ["number", "null"]}, "reason": {"type": "string"}},
                    "required": ["target", "field", "value_source", "reason"]}]},
                "question": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {
                    "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "string"}}, "required": ["question", "options"]}]},
                "confidence": {"type": "string", "enum": ["高", "中", "低"]},
            },
            "required": ["view", "kind", "severity", "message", "targets", "evidence", "confidence"],
        }},
        "summary": {"type": "string"},
    },
    "required": ["findings", "summary"],
}


def load_ai_models() -> dict:
    return yaml.safe_load((REPO_ROOT / "rules" / "ai_models.yaml").read_text(encoding="utf-8"))


def tool_names() -> list[str]:
    return [f"mcp__{SERVER}__{n}" for n in TOOL_SPECS]


def build_server(ctx: ToolContext):
    """HAKOYA の道具を、Agent SDK のアプリ内 MCP サーバーにする。"""
    from claude_agent_sdk import create_sdk_mcp_server
    return create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=make_tools(ctx))


def make_tools(ctx: ToolContext) -> list:
    """道具ごとの Agent SDK の道具（@tool）。画像は Claude に見せ、パスは渡さない。"""
    from claude_agent_sdk import tool

    def make(name: str, spec: dict):
        @tool(name, spec["description"], spec["schema"])
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            res = call_tool(ctx, name, dict(args or {}))
            content: list[dict] = [{"type": "text", "text": json.dumps(
                {k: v for k, v in res.items() if k != "image_path"}, ensure_ascii=False)}]
            if "image_path" in res:  # 画像は Claude に見せる（パスは渡さない）
                data = base64.b64encode(Path(res["image_path"]).read_bytes()).decode("ascii")
                content.append({"type": "image", "data": data, "mimeType": "image/png"})
            return {"content": content, "is_error": "error" in res}
        return handler

    return [make(n, s) for n, s in TOOL_SPECS.items()]


def prepare_workdir(alias: str, company: str) -> Path:
    """Skill を置いた作業フォルダ（リポジトリの外）。"""
    work = Path(tempfile.gettempdir()) / "hakoya_agent" / f"w{abs(hash(alias)) % 10**8}"
    skills = work / ".claude" / "skills"
    if skills.exists():
        shutil.rmtree(skills)
    for name in ("inspection", "model-rules", f"company-{company}"):
        src = SKILLS_DIR / name
        if src.exists():
            shutil.copytree(src, skills / name)
    return work


def face_agents(models: dict, multi: bool = False) -> dict:
    from claude_agent_sdk import AgentDefinition
    m = models["inspect_face"]
    out = {}
    for face in FACES:
        label = face_label(face, multi)
        how = ("run_checks の結果と、各面の模型の図（render_view source=model）を見て、印刷と組み立ての問題を探す。"
               if face == "print" else MULTI_PLAN_HOW if multi and face == "plan" else
               f"面 {face}（{label}）の模型の図と glb の図を render_view で両方見比べ、get_elements（view={face}）で部材の番号を確かめる。")
        out[f"inspect-{face}"] = AgentDefinition(
            description=f"模型の検品担当：{label}",
            prompt=(f"あなたは住宅模型の検品担当で、担当は「{label}」。Skill の inspection・model-rules・会社ルールに従う。{how}"
                    "食い違いを見つけたら、指摘を Skill の書き方（view・kind・severity・message・targets・evidence・proposal・question・confidence）で、"
                    "日本語の箇条書きで返す。直す案は propose_override で試し、数値は自分で書かない。見つからなければ「指摘なし」と返す。"),
            tools=tool_names(), model=m["model"], effort=m["effort"],
        )
    return out


# 組み込みで使ってよい道具：サブエージェントの呼び出し・Skill・構造化出力
# ★StructuredOutput は、決めた形の JSON を最後に返すための Claude Code の道具（output_format を使うと必要）。
#   1回目の試験ではこれをフックで止めてしまい、JSON が返らなかった（2026-11-02）
BUILTIN_ALLOWED = ("Agent", "Skill", "StructuredOutput")


def deny_non_app_tools():
    """HAKOYA の道具と BUILTIN_ALLOWED 以外の道具を止めるフック。"""
    from claude_agent_sdk import HookMatcher
    allowed_prefix = f"mcp__{SERVER}__"

    async def guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name", "")
        if name.startswith(allowed_prefix) or name in BUILTIN_ALLOWED:
            return {}
        return {"hookSpecificOutput": {"hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "HAKOYA の道具以外は使えません（SPEC §7-3）"}}
    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]}


def build_options(ctx: ToolContext, connection: str = "claude_code"):
    from claude_agent_sdk import ClaudeAgentOptions
    if connection == "claude_code" and os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("claude_code 接続なのに、環境に ANTHROPIC_API_KEY があります。API の料金になるため止めました。"
                           "API キーで動かすときは api_key 接続を選んでください。")
    conf = load_ai_models()
    models, limits = conf[connection], conf["limits"]
    company = ctx.raw["source"].get("company_rules", "jisha")
    work = prepare_workdir(ctx.alias, company)
    merge = models["inspect_merge"]
    return ClaudeAgentOptions(
        system_prompt=("あなたは住宅模型（1/50、3D プリント）の検品のまとめ役。日本語で考え、日本語で書く。"
                       "まず get_building で家の要約を読み、6人の検品担当（inspect-S・inspect-N・inspect-E・inspect-W・inspect-plan・inspect-print）を"
                       "同時に呼ぶ。返ってきた指摘の重複を除き、根拠のないものは捨て、重要度（高・中・低）順に並べて、決められた JSON で返す。"
                       "直す案は数値を書かず value_source（majority:種類 / rule:項目 / glb / number）で指定する。決まらないことは question にする。"),
        model=merge["model"], effort=merge["effort"],
        mcp_servers={SERVER: build_server(ctx)},
        tools=list(BUILTIN_ALLOWED),
        allowed_tools=list(BUILTIN_ALLOWED) + tool_names(),
        permission_mode="dontAsk",
        agents=face_agents(models, multi=len(ctx.building.get("levels", [])) > 1),
        hooks=deny_non_app_tools(),
        cwd=str(work), setting_sources=["project"], skills=["inspection", "model-rules", f"company-{company}"],
        output_format={"type": "json_schema", "schema": FINDINGS_SCHEMA},
        max_turns=int(limits["max_turns"]), max_budget_usd=float(limits["max_budget_usd"]),
    )


def finalize(ctx: ToolContext, raw_findings: list[dict]) -> list[dict]:
    """指摘に番号を振り、直す案の数値をコードで計算し直す（出どころが使えない案は質問に回す）。"""
    out = []
    multi = len(ctx.building.get("levels", [])) > 1
    for i, f in enumerate(raw_findings, 1):
        f = dict(f)
        f["id"] = f"F{i}"
        if f.get("view") == PLAN2 and not multi:     # 平屋に2階の平面はない
            f["view"] = "plan"
        f["status"] = "未確認"
        p = f.get("proposal")
        if p:
            try:
                res = propose_override(ctx, p["target"], p["field"], p["value_source"], p.get("reason", f["message"]),
                                       number=p.get("number"), finding=f["id"])
                f["proposal"] = res["proposal"]
            except ToolError as e:
                f["proposal"] = None
                f["proposal_error"] = str(e)
        out.append(f)
    order = {"高": 0, "中": 1, "低": 2}
    return sorted(out, key=lambda x: order.get(x["severity"], 3))


def _log_record(msg) -> dict:
    """やりとりの1件を、記録に残せる形にする（画像は入れない）。"""
    rec = {"type": type(msg).__name__}
    parent = getattr(msg, "parent_tool_use_id", None)
    if parent:
        rec["parent_tool_use_id"] = parent           # サブエージェント（検品担当）の中のやりとり
    # 検品担当（裏で動くサブエージェント）の開始・進み具合・終了の知らせ（画面の進み具合に使う）
    for key in ("task_id", "tool_use_id", "description", "status", "summary", "last_tool_name", "patch", "task_type"):
        val = getattr(msg, key, None)
        if val is not None and not callable(val):
            rec[key] = val if isinstance(val, (str, int, float, bool, dict, list)) else str(val)
    blocks = getattr(msg, "content", None)
    if isinstance(blocks, list):
        out = []
        for b in blocks:
            kind = type(b).__name__
            if kind == "TextBlock":
                out.append({"text": b.text})
            elif kind == "ToolUseBlock":
                out.append({"tool_use": b.name, "id": b.id, "input": b.input})
            elif kind == "ToolResultBlock":
                c = b.content
                if isinstance(c, list):
                    c = [x.get("text", "[画像]") if isinstance(x, dict) else str(x) for x in c]
                out.append({"tool_result": b.tool_use_id, "is_error": getattr(b, "is_error", None), "content": str(c)[:1500]})
            elif kind == "ThinkingBlock":
                out.append({"thinking": True})
        rec["content"] = out
    elif isinstance(blocks, str):
        rec["content"] = blocks[:1500]
    return rec


def extract_findings(result) -> dict:
    """結果から指摘の JSON を取り出す。structured_output → 文章の中の JSON の順に探す。"""
    if isinstance(result.structured_output, dict):
        return result.structured_output
    text = result.result or ""
    import re
    for cand in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) + ([text[text.find("{"):text.rfind("}") + 1]] if "{" in text else []):
        try:
            data = json.loads(cand)
            if isinstance(data, dict) and "findings" in data:
                return data
        except json.JSONDecodeError:
            continue
    raise RuntimeError("検品の結果から、決められた形の JSON が見つかりませんでした（inspect_result_raw.json を見てください）")


async def run_inspection(alias: str, connection: str = "claude_code", on_event=None) -> dict:
    from claude_agent_sdk import AssistantMessage, ResultMessage, query
    ctx = ToolContext.load(alias)
    options = build_options(ctx, connection)
    result = None
    log_path = out_dir(alias) / "inspect_log.jsonl"
    with log_path.open("w", encoding="utf-8") as log:
        async for msg in query(prompt=f"物件「{alias}」の模型を検品してください。", options=options):
            log.write(json.dumps(_log_record(msg), ensure_ascii=False, default=str) + "\n")
            log.flush()
            if on_event and isinstance(msg, AssistantMessage):
                on_event(msg)
            if isinstance(msg, ResultMessage):
                result = msg
    if result is not None:  # 読み取る前に、結果をそのまま残す
        raw = {k: getattr(result, k, None) for k in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd",
                                                     "result", "structured_output", "permission_denials", "errors",
                                                     "stop_reason", "terminal_reason", "model_usage")}
        (out_dir(alias) / "inspect_result_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2, default=str),
                                                                encoding="utf-8")
    if result is None or result.is_error:
        raise RuntimeError(f"検品が最後まで終わりませんでした（{getattr(result, 'subtype', '結果なし')}）")
    data = extract_findings(result)
    findings = finalize(ctx, data.get("findings", []))
    report = {"alias": alias, "connection": connection, "summary": data.get("summary", ""), "findings": findings,
              "cost_usd": result.total_cost_usd, "duration_s": round((result.duration_ms or 0) / 1000, 1),
              "turns": result.num_turns, "permission_denials": result.permission_denials or [],
              "questions": ctx.questions,   # 検品担当が ask_question で出した質問（人の答えを待つもの）
              "code_findings": code_findings(alias)}   # コードの検査からの指摘（glb の例外・未解決の所・検査の警告）
    (out_dir(alias) / "findings.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser(description="Claude による模型の検品")
    ap.add_argument("alias")
    ap.add_argument("--connection", default="claude_code", choices=["claude_code"])
    args = ap.parse_args(argv)
    rep = asyncio.run(run_inspection(args.alias, args.connection))
    print(f"【{args.alias}】検品：指摘 {len(rep['findings'])} 件（{rep['duration_s']} 秒、費用の目安 ${rep['cost_usd']}）")
    for f in rep["findings"]:
        print(f"  [{f['severity']}] {f['id']} {f['message']}" + ("　→ 直す案あり" if f.get("proposal") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
