"""囲んで直す（SPEC §9-3）：囲んだ部材と人の一言から、Claude が直す案か質問を出す。

- 囲みの中の部材はコードが決める（core/lasso.py）。Claude には、その一覧と一言を渡す。
- Claude が使えるのは HAKOYA の道具だけ。直す案は propose_override（数値はコードが出どころから計算）、
  決まらないときは ask_question（選択肢つきの質問）。検品と同じ道具を使う（SPEC §9-1）。
- つなぎ方：claude_code（Agent SDK、作者のプラン）／api_key（Messages API）／off（Claude を使わず、表の行を開く）。
- 1回ごとに、使った量（費用）を lasso_log.jsonl に残す（SPEC §7-5）。
- ★11/5 夜（作者の試し）：修正の依頼（core/fix_requests.py）から呼ぶ run_fix を足した。複数の囲み（①②…）・
  文字で直す（囲まない）・モデルの選択（Opus 5.5／Sonnet 5.5。rules/ai_models.yaml の lasso_models）。
  今までの run_lasso（/api/lasso/ask。テストや録画が使う）は、そのまま動く。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time

from core import lasso as geo
from core import overrides as ov
from core.ai.agent import (SERVER, build_server, deny_non_app_tools, load_ai_models, prepare_workdir,
                           tool_names)
from core.ai.api_runner import Meter, api_tools, japanese_error, make_client, skill_text, tool_loop
from core.ai.tools import ToolContext, ToolError, call_tool, propose_override
from core.paths import out_dir
from core.privacy import assert_clean
from core.rules import default_company

_RULES = ("使える道具は HAKOYA の道具だけ。"
          "数値（座標・寸法・高さ）は自分で作らず、直す案は propose_override に数値の出どころ（value_source）を渡して、"
          "コードに計算させる。人が数値を書いたときだけ value_source=number と number を使う。"
          "どの部材か・どの値か・何に合わせるかが決まらないときは、ask_question で選択肢（2〜3個）つきの質問をする。"
          "Skill（検品の決まり・模型のルール・会社のルール）に従う。")
ROLE = ("あなたは住宅模型（1/50、3D プリント）の手直しの担当。日本語で考え、日本語で書く。"
        "人が平面図か 3D の画面で部材を囲み、一言で直したいことを書いた。" + _RULES)
TEXT_ROLE = ("あなたは住宅模型（1/50、3D プリント）の手直しの担当。日本語で考え、日本語で書く。"
             "人が修正の画面で、部材を囲まずに、直したいことを文字で書いた。" + _RULES)


VIEW_JA = {"S": "南", "N": "北", "E": "東", "W": "西", "plan": "真上"}
ROOF_GUIDE = ("屋根の軒の出は propose_override（target=屋根の番号、field=eave_mm.辺）で直す。"
              "図面の寸法は get_drawing_region（drawing=section は断面図、立面は view=S など）の画像と寸法の番号で確かめ、"
              "軒の出そのものの寸法（外壁の外の面から屋根の先まで）があれば value_source=jww:section:D番号 で使う。"
              "測り方が違う寸法（通り芯から等）しかなければ使わない。一言に数値があればその数値を value_source=number で使い、"
              "なければ ask_question で図面の軒の出（外壁の外の面から屋根の先まで、板金・樋を除く）を人に聞く。"
              "数値を自分で作らない（選択肢にも数値を書かない。例：「図面の数値を答える」「glb のままにする」）。")
PARTIAL_NOTE = "「一部だけかかった部材」は、囲みの縁がかすっただけのこともあります。一言から外れていれば直さないでください。\n\n"
FINISH = "最後に、何をしたかを日本語で1〜2文で書いてください。"


def _line(r: dict) -> str:
    """部材の1行：番号（種類）：値（部品）。"""
    return (f"- {r['id']}（{r['label']}）：{r['text']}" + (f"（模型の部品：{r['part']}）" if r.get("part") else "")
            + ("　※囲みに一部だけかかった部材" if r.get("partial") else ""))


def lasso_prompt(rows: list[dict], text: str, view: str | None = None) -> str:
    """view を渡すと 3D の画面で囲んだときの言い方（どちらから見た画面か）。"""
    listing = "\n".join(_line(r) for r in rows) or "- （なし）"
    ids = ",".join(r["id"] for r in rows if r["id"][:1] in "WO")
    where = f"{VIEW_JA.get(view, view)}から見た 3D の画面で" if view else "平面図で"
    look = (f"必要なら render_view（view={view if view in VIEW_JA else 'plan'}、highlight={ids}）で図を見て、"
            "get_elements で値を確かめてください（屋根・軒は図に枠を付けられないので get_elements の ids で確かめる）。"
            if view else f"必要なら render_view（view=plan、highlight={ids}）で図を見て、get_elements で値を確かめてください。")
    roof = ROOF_GUIDE if any(r["kind"] in ("eave", "roof") for r in rows) else ""
    return (f"人が{where}部材を囲み、こう書きました：「{text}」\n\n"
            f"囲みの中の部材（コードが決めたもの）：\n{listing}\n\n"
            + (PARTIAL_NOTE if any(r.get("partial") for r in rows) else "") +
            "この一言に合う直す案を propose_override で作ってください（1つの部材の1つの値ごとに1件）。"
            f"決まらないときは ask_question で質問してください。{roof}{look}" + FINISH)


def _group_where(g: dict) -> str:
    if g.get("source") == "3d":
        v = g.get("view")
        return f"{VIEW_JA.get(v, v or '斜め')}から見た 3D の画面で"
    return "平面図で"


def groups_prompt(rows: list[dict], text: str, groups: list[dict]) -> str:
    """複数の囲み（①②…）。一言は1つで、人は「①は…、②は…」と書ける（★11/5 夜 作者「複数を囲んで修正する機能も」）。"""
    by_id = {r["id"]: r for r in rows}
    blocks = []
    for g in groups:
        items = [by_id[i] for i in g.get("ids", []) if i in by_id]
        blocks.append(f"{g['label']}（{_group_where(g)}囲んだもの）\n" + ("\n".join(_line(r) for r in items) or "- （なし）"))
    ids = ",".join(r["id"] for r in rows if r["id"][:1] in "WO")
    views = sorted({(g.get("view") if g.get("source") == "3d" and g.get("view") in VIEW_JA else "plan") for g in groups})
    roof = ROOF_GUIDE if any(r["kind"] in ("eave", "roof") for r in rows) else ""
    return (f"人が部材を {len(groups)} か所囲み、こう書きました：「{text}」\n"
            "（①②…は人が囲んだ順の番号です。一言の「①は…」は、その囲みの部材のことです）\n\n"
            "囲みの中の部材（コードが決めたもの。囲みごと）：\n" + "\n\n".join(blocks) + "\n\n"
            + (PARTIAL_NOTE if any(r.get("partial") for r in rows) else "") +
            "この一言に合う直す案を propose_override で作ってください（1つの部材の1つの値ごとに1件。囲みごとに違う直しでもよい）。"
            f"決まらないときは ask_question で質問してください。{roof}"
            f"必要なら render_view（view={'・'.join(views)} のどれか、highlight={ids}）で図を見て、get_elements で値を確かめてください"
            "（屋根・軒は get_elements の ids で確かめる）。" + FINISH)


def text_prompt(rows: list[dict], text: str) -> str:
    """文字で直す（囲まない）。いま選んでいる部材（画面の光っている部材）があれば参考に添える（★11/5 夜 作者）。"""
    sel = ("人がいま画面で選んでいる部材（参考。一言の部材と違うこともあります）：\n" + "\n".join(_line(r) for r in rows) + "\n\n"
           if rows else "")
    roof = ROOF_GUIDE if any(r["kind"] in ("eave", "roof") for r in rows) or re.search(r"軒|屋根|のき", text) else ""
    return (f"人が修正の画面で、部材を囲まずに、文字だけでこう書きました：「{text}」\n\n{sel}"
            "まず get_building（家の要約）で家を読み、get_elements（kind・view・ids で絞れる）で一言に合う部材を探してください。"
            "部材の番号は W○（壁）・O○（窓・ドア）・Y○（屋根。軒は Y○:S のように辺を付ける）です。"
            "見つけた部材に、一言に合う直す案を propose_override で作ってください（1つの部材の1つの値ごとに1件）。"
            "どの部材か決まらないとき（候補が2つ以上ある・見つからない）は、推測で選ばず ask_question で質問してください"
            "（選択肢には部材の番号と場所を書く）。"
            f"必要なら render_view（view=plan など、highlight=部材の番号）で図を見て確かめてください。{roof}" + FINISH)


def _log(alias: str, rec: dict) -> None:
    text = json.dumps(rec, ensure_ascii=False, default=str)
    assert_clean(text, "囲んで直すの記録")
    with (out_dir(alias) / "lasso_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(text + "\n")


MODEL_KEYS = ("opus", "sonnet")


def model_spec(conf: dict, connection: str, model: str | None) -> dict:
    """修正で選んだモデル（opus・sonnet）の {model, effort, label}。lasso_models がなければ今までの lasso。"""
    choices = (conf.get("lasso_models") or {}).get(connection) or {}
    if model and model not in MODEL_KEYS:
        raise ValueError("モデルは opus（Opus 5.5）か sonnet（Sonnet 5.5）です")
    m = choices.get(model or "opus") or conf[connection]["lasso"]
    return {"model": m["model"], "effort": m["effort"], "label": m.get("label") or m["model"]}


async def _run_agent(ctx: ToolContext, prompt: str, conf: dict, m: dict | None = None, role: str = ROLE) -> tuple[str, float | None]:
    """claude_code 接続：Agent SDK で1人の担当を動かす（サブエージェントは使わない）。"""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("claude_code 接続なのに、環境に ANTHROPIC_API_KEY があります。API の料金になるため止めました。"
                           "API キーで動かすときは api_key 接続を選んでください。")
    m, limits = m or conf["claude_code"]["lasso"], conf["limits"]
    company = ctx.raw["source"].get("company_rules", default_company())
    options = ClaudeAgentOptions(
        system_prompt=role, model=m["model"], effort=m["effort"],
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


async def _run_api(ctx: ToolContext, prompt: str, conf: dict, client=None, m: dict | None = None,
                   role: str = ROLE) -> tuple[str, float]:
    """api_key 接続：Messages API で道具を使うやりとりを回す。"""
    m, limits = m or conf["api_key"]["lasso"], conf["limits"]
    own = client is None
    client = client or make_client()
    meter = Meter(prices=conf["prices_usd_per_mtok"], limit_usd=float(limits["lasso_max_budget_usd"]))
    system = role + "\n\n" + skill_text(ctx.raw["source"].get("company_rules", default_company()))
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


# ---------------------------------------------------------------- 画面の試し用の偽物（Claude を呼ばない）

def fake_enabled() -> bool:
    """画面の確かめ用：環境変数 HAKOYA_FAKE_AI=1 のときだけ、Claude を呼ばずに決まった答えを返す。
    配る exe（PyInstaller でまとめたもの）では、環境変数があっても効かない。"""
    return os.environ.get("HAKOYA_FAKE_AI") == "1" and not getattr(sys, "frozen", False)


FAKE_MESSAGE = "（試しの偽物の答えです。Claude は呼んでいません）同じ種類の壁の多数派の厚みにそろえる案を出しました。"


async def _run_fake(ctx: ToolContext, rows: list[dict]) -> tuple[str, float]:
    """決まった直す案（囲んだ壁を、同じ種類の壁の多数派の厚みにそろえる。最大2件）と、選択肢つきの質問を1つ。
    数値はいつもどおりコード（propose_override）が出す。待ち時間は HAKOYA_FAKE_AI_DELAY 秒（既定 8）。"""
    await asyncio.sleep(max(0.0, float(os.environ.get("HAKOYA_FAKE_AI_DELAY") or 8)))
    walls = [r for r in rows if r["id"][:1] == "W"] or \
        [{"id": w["id"], "kind": w["kind"]} for w in ctx.building.get("walls", [])]
    made = 0
    for r in walls:
        if made >= 2:
            break
        res = call_tool(ctx, "propose_override", {"target": r["id"], "field": "thickness_mm",
                                                  "value_source": f"majority:{r['kind']}",
                                                  "reason": "（試し）同じ種類の壁の多数派の厚みにそろえる"})
        made += "error" not in res
    opts = [r["id"] for r in rows[:3]] if len(rows) >= 2 else ["この案のまま", "直さない"]
    call_tool(ctx, "ask_question", {"question": "（試し）どの部材を直しますか？", "options": opts, "evidence": "試しの偽物の答え"})
    return FAKE_MESSAGE, 0.0


# ---------------------------------------------------------------- 今までの窓口（/api/lasso/ask）

async def run_lasso(alias: str, connection: str, points: list[list[float]] | None, text: str, *,
                    client=None, ctx: ToolContext | None = None, level: str | None = None,
                    hits: list[dict] | None = None, mask_px: int = 0, view: str | None = None,
                    keep: list[str] | None = None) -> dict:
    """囲み（平面の mm）か 3D の画素の数（hits）と一言から、直す案と質問を返す。保存はしない（人が承認したら approve で記録する）。
    level：2階建てで表示している階（平面図の囲みで、その階の部材だけを選ぶ）。view：3D の画面をどちらから見ていたか。"""
    ctx = ctx or ToolContext.load(alias)
    if hits is not None:
        selection = geo.select_hits(ctx.building, hits, mask_px)
    elif points:
        selection = geo.select(ctx.building, points, level)
    else:
        raise ValueError("囲みがありません。もう一度囲んでください")
    selection = geo.keep_only(selection, keep)            # 画面で札の × を押して外した部材を除く
    rows = geo.describe(ctx.building, selection, geo.roof_ids_from_checks(read_checks(alias)))
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


def read_checks(alias: str) -> dict | None:
    """書き出したときの記録（model/checks.json）。部品の振り分けの手がかりに使う。読めなければ None。"""
    try:
        return json.loads((out_dir(alias) / "model" / "checks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 修正の依頼（core/fix_requests.py が裏の thread で呼ぶ）

def fix_rows(ctx: ToolContext, alias: str, ids: list[str] | None, partial: list[str] | None = None) -> list[dict]:
    """依頼の部材の一覧（いまの家の値で）。組み立て直して番号がなくなった部材は外す。"""
    sel = geo.select_ids(ctx.building, ids or [], partial, strict=False)
    return geo.describe(ctx.building, sel, geo.roof_ids_from_checks(read_checks(alias)))


async def run_fix(alias: str, connection: str, text: str, *, kind: str = "lasso", model: str = "opus",
                  ids: list[str] | None = None, partial: list[str] | None = None, groups: list[dict] | None = None,
                  human_words: list[str] | None = None, request_id: str | None = None,
                  ctx: ToolContext | None = None, client=None) -> dict:
    """修正の依頼を1つ Claude に渡し、直す案と質問を返す（保存は呼ぶ側。承認は approve）。
    kind：lasso（囲んだ部材。groups は囲みごとの {label, ids, source, view}）／text（囲まない。ids はいま選んでいる部材）。
    human_words：人が書いた・選んだ文（number の数値は、この中にあるものだけ。なければ text）。"""
    if kind not in ("lasso", "text"):
        raise ValueError("kind は lasso（囲んで直す）か text（文字で直す）です")
    ctx = ctx or ToolContext.load(alias)
    rows = fix_rows(ctx, alias, ids, partial)
    if kind == "lasso" and not rows:
        raise ValueError("囲んだ部材が家の中に見つかりません（組み立て直したのかもしれません）。もう一度囲んでください")
    text = (text or "").strip()
    if not text:
        raise ValueError("一言を書いてください（例：この壁厚だけおかしい。ほかとそろえて）")
    ctx.human_text = "\n".join(w for w in (human_words or [text]) if w)
    groups = [g for g in (groups or []) if g.get("ids")]
    if kind == "text":
        prompt, role = text_prompt(rows, text), TEXT_ROLE
    elif len(groups) >= 2:
        prompt, role = groups_prompt(rows, text, groups), ROLE
    else:
        g = groups[0] if groups else {}
        prompt, role = lasso_prompt(rows, text, g.get("view") if g.get("source") == "3d" else None), ROLE
    assert_clean(prompt, "Claude に送るデータ")
    conf = load_ai_models()
    m = model_spec(conf, connection, model)
    fake = fake_enabled()
    started = time.time()
    if fake:
        message, cost = await _run_fake(ctx, rows)
    elif connection == "claude_code":
        message, cost = await _run_agent(ctx, prompt, conf, m, role)
    elif connection == "api_key":
        message, cost = await _run_api(ctx, prompt, conf, client, m, role)
    else:
        raise ValueError("connection は claude_code か api_key です（AI なしは「表で直す」）")
    duration = round(time.time() - started, 1)
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "request": request_id, "kind": kind, "connection": connection,
           "model": m["model"], "effort": m["effort"], "text": text,
           "where": "text" if kind == "text" else ",".join(f"{g.get('label', '')}{g.get('source') or 'plan'}"
                                                           f"{':' + str(g.get('view')) if g.get('source') == '3d' else ''}"
                                                           for g in groups) or "plan",
           "selection": [r["id"] for r in rows], "proposals": ctx.proposals, "questions": ctx.questions,
           "message": message, "cost_usd": cost, "duration_s": duration}
    if fake:
        rec["fake"] = True
    _log(alias, rec)
    return {"message": message, "proposals": ctx.proposals, "questions": ctx.questions, "cost_usd": cost,
            "duration_s": duration, "selection": rows, "model": m["model"], "fake": fake}


def approve(alias: str, proposal: dict, origin: str = "lasso") -> dict:
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
                  p.get("reason") or "囲んで直した", origin=origin)
