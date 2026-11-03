"""囲んで直す（SPEC §9-3）：囲んだ部材と人の一言から、Claude が直す案か質問を出す。

- 囲みの中の部材はコードが決める（core/lasso.py）。Claude には、その一覧と一言を渡す。
- Claude が使えるのは HAKOYA の道具だけ。直す案は propose_override（数値はコードが出どころから計算）、
  決まらないときは ask_question（選択肢つきの質問）。検品と同じ道具を使う（SPEC §9-1）。
- つなぎ方：claude_code（Agent SDK、作者のプラン）／api_key（Messages API）／off（Claude を使わず、表の行を開く）。
- 1回ごとに、使った量（費用）を lasso_log.jsonl に残す（SPEC §7-5）。
"""
from __future__ import annotations

import json
import os
import time

from core import lasso as geo
from core import overrides as ov
from core.ai.agent import (SERVER, build_server, deny_non_app_tools, load_ai_models, prepare_workdir,
                           tool_names)
from core.ai.api_runner import Meter, api_tools, japanese_error, make_client, skill_text, tool_loop
from core.ai.tools import ToolContext, ToolError, propose_override
from core.paths import out_dir
from core.privacy import assert_clean

ROLE = ("あなたは住宅模型（1/50、3D プリント）の手直しの担当。日本語で考え、日本語で書く。"
        "人が平面図か 3D の画面で部材を囲み、一言で直したいことを書いた。使える道具は HAKOYA の道具だけ。"
        "数値（座標・寸法・高さ）は自分で作らず、直す案は propose_override に数値の出どころ（value_source）を渡して、"
        "コードに計算させる。人が数値を書いたときだけ value_source=number と number を使う。"
        "どの部材か・どの値か・何に合わせるかが決まらないときは、ask_question で選択肢（2〜3個）つきの質問をする。"
        "Skill（検品の決まり・模型のルール・会社のルール）に従う。")


VIEW_JA = {"S": "南", "N": "北", "E": "東", "W": "西", "plan": "真上"}
ROOF_GUIDE = ("屋根の軒の出は propose_override（target=屋根の番号、field=eave_mm.辺）で直す。"
              "図面（jww）はまだ読めない。一言に数値があればその数値を value_source=number で使い、"
              "なければ ask_question で図面の軒の出（外壁の外の面から屋根の先まで、板金・樋を除く）を人に聞く。"
              "数値を自分で作らない（選択肢にも数値を書かない。例：「図面の数値を答える」「glb のままにする」）。")


def lasso_prompt(rows: list[dict], text: str, view: str | None = None) -> str:
    """view を渡すと 3D の画面で囲んだときの言い方（どちらから見た画面か）。"""
    listing = "\n".join(f"- {r['id']}（{r['label']}）：{r['text']}" for r in rows) or "- （なし）"
    ids = ",".join(r["id"] for r in rows if r["id"][:1] in "WO")
    where = f"{VIEW_JA.get(view, view)}から見た 3D の画面で" if view else "平面図で"
    look = (f"必要なら render_view（view={view if view in VIEW_JA else 'plan'}、highlight={ids}）で図を見て、"
            "get_elements で値を確かめてください（屋根・軒は図に枠を付けられないので get_elements の ids で確かめる）。"
            if view else f"必要なら render_view（view=plan、highlight={ids}）で図を見て、get_elements で値を確かめてください。")
    roof = ROOF_GUIDE if any(r["kind"] in ("eave", "roof") for r in rows) else ""
    return (f"人が{where}部材を囲み、こう書きました：「{text}」\n\n"
            f"囲みの中の部材（コードが決めたもの）：\n{listing}\n\n"
            "この一言に合う直す案を propose_override で作ってください（1つの部材の1つの値ごとに1件）。"
            f"決まらないときは ask_question で質問してください。{roof}{look}"
            "最後に、何をしたかを日本語で1〜2文で書いてください。")


def _log(alias: str, rec: dict) -> None:
    text = json.dumps(rec, ensure_ascii=False, default=str)
    assert_clean(text, "囲んで直すの記録")
    with (out_dir(alias) / "lasso_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(text + "\n")


async def _run_agent(ctx: ToolContext, prompt: str, conf: dict) -> tuple[str, float | None]:
    """claude_code 接続：Agent SDK で1人の担当を動かす（サブエージェントは使わない）。"""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("claude_code 接続なのに、環境に ANTHROPIC_API_KEY があります。API の料金になるため止めました。"
                           "API キーで動かすときは api_key 接続を選んでください。")
    m, limits = conf["claude_code"]["lasso"], conf["limits"]
    company = ctx.raw["source"].get("company_rules", "jisha")
    options = ClaudeAgentOptions(
        system_prompt=ROLE, model=m["model"], effort=m["effort"],
        mcp_servers={SERVER: build_server(ctx)}, tools=["Skill"], allowed_tools=["Skill"] + tool_names(),
        permission_mode="dontAsk", hooks=deny_non_app_tools(),
        cwd=str(prepare_workdir(ctx.alias, company)), setting_sources=["project"],
        skills=["inspection", "model-rules", f"company-{company}"],
        max_turns=int(limits["lasso_max_turns"]), max_budget_usd=float(limits["lasso_max_budget_usd"]))
    result = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg
    if result is None or result.is_error:
        raise RuntimeError(f"Claude の答えが最後まで返りませんでした（{getattr(result, 'subtype', '結果なし')}）")
    return (result.result or "").strip(), result.total_cost_usd


async def _run_api(ctx: ToolContext, prompt: str, conf: dict, client=None) -> tuple[str, float]:
    """api_key 接続：Messages API で道具を使うやりとりを回す。"""
    m, limits = conf["api_key"]["lasso"], conf["limits"]
    own = client is None
    client = client or make_client()
    meter = Meter(prices=conf["prices_usd_per_mtok"], limit_usd=float(limits["lasso_max_budget_usd"]))
    system = ROLE + "\n\n" + skill_text(ctx.raw["source"].get("company_rules", "jisha"))
    try:
        text, stop, _ = await tool_loop(client, ctx, prompt, model=m["model"], effort=m["effort"], system=system,
                                        tools=api_tools(), meter=meter, max_turns=int(limits["lasso_max_turns"]))
    except Exception as e:
        raise RuntimeError(japanese_error(e)) from e
    finally:
        if own:
            await client.close()
    if stop == "refusal":
        raise RuntimeError("Claude が答えを断りました")
    return text, round(meter.cost_usd, 4)


async def run_lasso(alias: str, connection: str, points: list[list[float]] | None, text: str, *,
                    client=None, ctx: ToolContext | None = None, level: str | None = None,
                    hits: list[dict] | None = None, mask_px: int = 0, view: str | None = None) -> dict:
    """囲み（平面の mm）か 3D の画素の数（hits）と一言から、直す案と質問を返す。保存はしない（人が承認したら approve で記録する）。
    level：2階建てで表示している階（平面図の囲みで、その階の部材だけを選ぶ）。view：3D の画面をどちらから見ていたか。"""
    ctx = ctx or ToolContext.load(alias)
    if hits is not None:
        selection = geo.select_hits(ctx.building, hits, mask_px)
    elif points:
        selection = geo.select(ctx.building, points, level)
    else:
        raise ValueError("囲みがありません。もう一度囲んでください")
    rows = geo.describe(ctx.building, selection)
    out = {"connection": connection, "selection": rows, "message": "", "proposals": [], "questions": [], "cost_usd": None}
    if connection == "off" or not rows:
        out["message"] = ("囲みの中に部材がありません。壁や窓をぐるっと囲んでください" if not rows
                          else "AI なしなので、囲んだ部材の表の行を開きます")
        return out
    text = (text or "").strip()
    if not text:
        raise ValueError("一言を書いてください（例：この壁厚だけおかしい。ほかとそろえて）")
    ctx.human_text = text                                # number の数値は、この一言（と質問への答え）にあるものだけ
    prompt = lasso_prompt(rows, text, view if hits is not None else None)
    assert_clean(prompt, "Claude に送るデータ")
    conf = load_ai_models()
    started = time.time()
    if connection == "claude_code":
        message, cost = await _run_agent(ctx, prompt, conf)
    elif connection == "api_key":
        message, cost = await _run_api(ctx, prompt, conf, client)
    else:
        raise ValueError("connection は claude_code・api_key・off のどれかです")
    out.update(message=message, proposals=ctx.proposals, questions=ctx.questions, cost_usd=cost)
    _log(alias, {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "connection": connection, "text": text,
                 "where": ("3d:" + str(view)) if hits is not None else "plan",
                 "selection": [r["id"] for r in rows], "proposals": ctx.proposals, "questions": ctx.questions,
                 "message": message, "cost_usd": cost, "duration_s": round(time.time() - started, 1)})
    return out


def approve(alias: str, proposal: dict) -> dict:
    """囲んで直すの直す案を承認して、修正として1件記録する。数値は画面から受け取らず、出どころから計算し直す。"""
    ctx = ToolContext.load(alias)
    vs = proposal.get("value_source", "")
    number = None
    if vs.startswith("human:number:"):                  # 人が書いた数値（propose_override がこの形にする）
        vs, number = "number", float(vs.split(":")[-1])
    try:
        p = propose_override(ctx, proposal["target"], proposal["field"], vs, proposal.get("reason", ""),
                             number=number)["proposal"]
    except (KeyError, ToolError) as e:
        raise ValueError(str(e).strip("'\"")) from e
    return ov.add(alias, ctx.raw, p["target"], p["field"], p.get("to"), p["value_source"],
                  p.get("reason") or "囲んで直した", origin="lasso")
