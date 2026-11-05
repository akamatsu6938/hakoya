"""会社ルールの言語化：集計（コード）と人の答えから、Claude が会社ルールの案を根拠つきで書く（SPEC §5-4②・§9-1）。

- 家・会社によって変わる部分は物件ごとに glb から作る。会社で変わらない値（壁厚など）と細かい決まりを会社ルールとして残し、
  困ったときの予防に使う（作者 11/3）。
- 数値はコードが集計から出す。Claude は「どの集計・答えを根拠に、どう言葉にするか」だけを返す。
  文章の中の数値は、根拠にした集計か人の答えにある数値だけ（コードが確かめて、違えば断る）。
- 案の種類：value（会社で同じで、会社ルールの値と違う → 値を直す）／glb（物件ごとに違う → 決め打ちしない、と書く）／
  skill（細かい決まりの文章 → 会社の Skill に足す）。
- 承認した分だけ「学んだルール」（DATA_DIR/rules/<会社>.learned.json）に残す。却下した案は二度出さない。
- つなぎ方：claude_code（Agent SDK、この道具だけ）／api_key（Messages API）／off（コードの候補だけ）。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from functools import partial

from core import rules as rules_mod
from core.ai.agent import SERVER, SKILLS_DIR, build_server, deny_non_app_tools, load_ai_models, prepare_workdir, tool_names
from core.ai.api_runner import Meter, api_tools, japanese_error, make_client, skill_text, tool_loop
from core.ai.tools import ToolContext, ToolError, call_tool, get_building
from core.ai.tools import ask_question as _ask_question
from core.paths import aliases
from core.privacy import assert_clean
from core.rule_stats import code_candidates, collect, entries
from core.rules import LEARNABLE, base_text, default_company, load_answers, load_learned, load_rules, rule, save_learned

ROLE = ("あなたは住宅会社の「会社ルール」を言葉にする担当。日本語で考え、日本語で書く。使える道具は HAKOYA の道具だけ。"
        "家・会社によって変わる部分は物件ごとに glb から作るので、会社ルールで決め打ちしない。"
        "会社で変わらない値（壁厚など）と細かい決まりを会社ルールとして残し、困ったとき（glb が読めない・いつもと違う・新しい形）"
        "の予防に使う。数値は自分で作らず、コードの集計（get_rule_stats）と人の答え（get_answers）を根拠に引く。")
KINDS = ("value", "glb", "skill")


@dataclass
class RuleContext:
    company: str
    rules: dict
    stats: list[dict]
    answers: list[dict]
    learned: dict
    aliases: list[str]
    proposals: list[dict] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, company: str) -> "RuleContext":
        return cls(company=company, rules=load_rules(company), stats=collect(company), answers=load_answers(),
                   learned=load_learned(company), aliases=[a for a, _ in entries(company)])

    def stat(self, sid: str) -> dict | None:
        return next((s for s in self.stats if s["id"] == sid), None)

    def answer(self, aid: str) -> dict | None:
        return next((a for a in self.answers if a["id"] == aid), None)


# ---------------------------------------------------------------- 道具

def get_rule_stats(ctx: RuleContext, ids: list[str] | None = None) -> dict:
    """集計（コードが計算）。判定：same＝会社で同じ／varies＝物件ごとに違う／few＝件数が足りない／info＝参考。"""
    keep = ("id", "label", "status", "agreement", "rule_key", "rule_value", "rule_match", "mode", "n", "n_all", "unit")
    rows = [{k: s.get(k) for k in keep} for s in ctx.stats if not ids or s["id"] in ids]
    return {"properties": ctx.aliases, "stats": rows,
            "note": "数値はこの集計の値だけを使う。value の案は status が same の項目だけ。few の項目は値の案にしない"}


def get_company_rules(ctx: RuleContext) -> dict:
    """いまの会社ルール：学べる項目の値・yaml の文（★のコメント込み）・会社の Skill の文・学んだルール・却下した案。"""
    learnable = {}
    for k in LEARNABLE:
        try:
            learnable[k] = rule(ctx.rules, k)
        except KeyError:
            pass
    p = SKILLS_DIR / f"company-{ctx.company}" / "SKILL.md"
    return {"learnable_values": learnable, "yaml_text": base_text(ctx.company),
            "skill_text": p.read_text(encoding="utf-8") if p.exists() else "",
            "learned": {"values": ctx.learned["values"], "notes": ctx.learned["notes"]},
            "rejected": [r.get("signature") for r in ctx.learned["rejected"]],
            "note": "Skill の文と学んだルールにすでにある決まりは、もう一度出さない"}


def get_answers(ctx: RuleContext) -> dict:
    """人（作者）の答え。id を answer_ids に引ける。"""
    return {"answers": [{k: a.get(k) for k in ("id", "property", "question", "answer", "applied_as")} for a in ctx.answers]}


def get_property(ctx: RuleContext, alias: str) -> dict:
    """1つの物件の要約（検品の get_building と同じ）。"""
    if alias not in ctx.aliases:
        raise ToolError(f"{alias} という物件はありません（{'・'.join(ctx.aliases)}）")
    return get_building(ToolContext.load(alias))


def _numbers(text: str) -> list[float]:
    t = unicodedata.normalize("NFKC", text or "").replace(",", "")
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]


def _stat_numbers(s: dict) -> set[float]:
    out = set()
    for v in (s.get("mode"), s.get("n"), s.get("n_all"), s.get("mode_count")):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.add(float(v))
    rv = s.get("rule_value")
    for v in (rv if isinstance(rv, list) else [rv]):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.add(float(v))
    for p in s.get("per_property", []):
        for k in ("value", "count", "total"):
            if isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool):
                out.add(float(p[k]))
        for e in p.get("elements", []):
            for k in ("value", "count"):
                if isinstance(e.get(k), (int, float)) and not isinstance(e.get(k), bool):
                    out.add(float(e[k]))
    out |= set(_numbers(s.get("agreement", "")))
    return out


def _signature(kind: str, rule_key: str | None, stat_ids: list[str], answer_ids: list[str]) -> str:
    if kind == "value":
        return f"value:{rule_key}"
    if kind == "glb":
        return f"glb:{stat_ids[0] if stat_ids else rule_key}"
    return "skill:" + "+".join(sorted(stat_ids + answer_ids))


def validate_proposal(ctx: RuleContext, kind: str, wording: str, stat_ids: list[str] | None = None,
                      answer_ids: list[str] | None = None, rule_key: str | None = None,
                      value_source: str | None = None, origin: str = "claude") -> dict:
    """案を確かめて、値と根拠の文をコードが作る。おかしければ ToolError（日本語）。道具と承認の両方で使う。"""
    stat_ids, answer_ids = list(stat_ids or []), list(answer_ids or [])
    if kind not in KINDS:
        raise ToolError("kind は value（値を直す）・glb（物件ごとに違う）・skill（細かい決まりの文章）のどれかです")
    wording = (wording or "").strip()
    if not wording:
        raise ToolError("wording（会社ルールの文）を書いてください")
    stats = []
    for sid in stat_ids:
        s = ctx.stat(sid)
        if s is None:
            raise ToolError(f"集計 {sid} はありません。get_rule_stats の id を使ってください")
        stats.append(s)
    answers = []
    for aid in answer_ids:
        a = ctx.answer(aid)
        if a is None:
            raise ToolError(f"答え {aid} はありません。get_answers の id を使ってください")
        answers.append(a)
    if not stats and not answers:
        raise ToolError("根拠がありません。stat_ids（集計）か answer_ids（人の答え）を付けてください")
    frm = to = None
    if kind == "value":
        if rule_key not in LEARNABLE:
            raise ToolError(f"値を直せる会社ルールの項目は {', '.join(LEARNABLE)} です（模型の作り方・内壁の厚みは学ばない）")
        m = re.fullmatch(r"stat:(.+):mode", value_source or "")
        if not m or m.group(1) not in stat_ids:
            raise ToolError("value の案は value_source を「stat:<根拠にした集計の id>:mode」にしてください（数値はコードが出す）")
        s = ctx.stat(m.group(1))
        if s.get("rule_key") != rule_key:
            raise ToolError(f"集計 {s['id']} は {s.get('rule_key')} の項目です（{rule_key} ではない）")
        if s["status"] != "same":
            raise ToolError(f"集計 {s['id']} は「{s['status']}」です。会社で同じ（same）の項目だけ値の案にできます。"
                            "物件ごとに違う項目は kind=glb にしてください")
        frm = rule(ctx.rules, rule_key)
        to = s["mode"]
        if frm == to:
            raise ToolError(f"{rule_key} はすでに {frm} です（直す必要がありません）")
    allowed = {float(i) for i in range(11)} | {float(x) for x in ([to] if isinstance(to, (int, float)) else [])}
    for s in stats:
        allowed |= _stat_numbers(s)
    for a in answers:
        allowed |= set(_numbers(" ".join(str(a.get(k, "")) for k in ("question", "answer", "applied_as"))))
    bad = [x for x in _numbers(wording) if not any(abs(x - y) < 1e-6 for y in allowed)]
    if bad:
        raise ToolError(f"文の中の数値 {'・'.join(f'{x:g}' for x in bad)} は、根拠にした集計・答えにありません。"
                        "数値を消すか、その数値がある集計・答えを根拠に付けてください")
    sig = _signature(kind, rule_key, stat_ids, answer_ids)
    done = {n.get("signature") for n in ctx.learned["notes"]} | {f"value:{k}" for k in ctx.learned["values"]} | \
        {r.get("signature") for r in ctx.learned["rejected"]}
    if sig in done:
        raise ToolError("同じ案はすでに承認か却下されています。別の案にしてください")
    if any(p["signature"] == sig for p in ctx.proposals):
        raise ToolError("同じ案をもう出しています")
    evidence = "／".join([s["agreement"] for s in stats] + [f"{a['id']}「{a['question']}」→「{a['answer']}」" for a in answers])
    return {"kind": kind, "rule_key": rule_key, "from": frm, "to": to, "value_source": value_source if kind == "value" else None,
            "wording": wording, "stat_ids": stat_ids, "answer_ids": answer_ids, "evidence": evidence,
            "signature": sig, "origin": origin}


def propose_rule(ctx: RuleContext, kind: str, wording: str, stat_ids: list[str] | None = None,
                 answer_ids: list[str] | None = None, rule_key: str | None = None, value_source: str | None = None) -> dict:
    """会社ルールの案を1件つくる（保存はしない。人が承認したら学んだルールに入る）。"""
    p = validate_proposal(ctx, kind, wording, stat_ids, answer_ids, rule_key, value_source)
    p["id"] = f"P{len(ctx.proposals) + 1}"
    ctx.proposals.append(p)
    return {"proposal": p, "note": "人が承認すると会社ルール（学んだルール）に入り、予防に使われます"}


def ask_question(ctx: RuleContext, question: str, options: list[str], evidence: str = "") -> dict:
    return _ask_question(ctx, question, options, evidence)


_S = {"type": "array", "items": {"type": "string"}}
RULE_TOOL_SPECS = {
    "get_rule_stats": {"fn": get_rule_stats, "description": "全物件の集計（コードが計算）。判定 same＝会社で同じ・varies＝物件ごとに違う・few＝件数が足りない・info＝参考。agreement は一致の文。",
                       "schema": {"type": "object", "properties": {"ids": _S}, "additionalProperties": False}},
    "get_company_rules": {"fn": get_company_rules, "description": "いまの会社ルール（学べる項目の値・yaml の文・会社の Skill の文・学んだルール・却下した案）。",
                          "schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "get_answers": {"fn": get_answers, "description": "人の答え。id を answer_ids に引ける。",
                    "schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "get_property": {"fn": get_property, "description": "1つの物件の要約（階・壁と開口の数・屋根・glb の例外・会社ルールと比べた結果）。",
                     "schema": {"type": "object", "properties": {"alias": {"type": "string"}}, "required": ["alias"],
                                "additionalProperties": False}},
    "propose_rule": {"fn": propose_rule,
                     "description": ("会社ルールの案を1件つくる。kind：value（会社で同じで会社ルールの値と違う → 値を直す。数値は書かず "
                                     "value_source=stat:<集計 id>:mode）／glb（物件ごとに違う → 会社ルールで決め打ちせず glb の値を使う、と書く）／"
                                     "skill（細かい決まりの文章 → 会社の Skill に足す）。根拠に stat_ids か answer_ids が要る。"
                                     "wording の数値は根拠にある数値だけ。"),
                     "schema": {"type": "object", "properties": {
                         "kind": {"type": "string", "enum": list(KINDS)}, "wording": {"type": "string"},
                         "stat_ids": _S, "answer_ids": _S, "rule_key": {"type": "string"}, "value_source": {"type": "string"}},
                         "required": ["kind", "wording"], "additionalProperties": False}},
    "ask_question": {"fn": ask_question, "description": "決まらないことを人に聞く。選択肢は2〜3個（数値を作らない）、根拠を添える。",
                     "schema": {"type": "object", "properties": {
                         "question": {"type": "string"}, "options": _S, "evidence": {"type": "string"}},
                         "required": ["question", "options"], "additionalProperties": False}},
}
rule_call = partial(call_tool, specs=RULE_TOOL_SPECS)


# ---------------------------------------------------------------- 動かす

def draft_prompt(ctx: RuleContext, text: str = "") -> str:
    digest = "\n".join(f"- {s['id']}（{s['status']}）：{s['label']} … {s['agreement']}" for s in ctx.stats)
    extra = f"\n人からの一言：「{text}」\n" if text.strip() else ""
    return (f"物件 {len(ctx.aliases)} 件（{'・'.join(ctx.aliases)}）から、会社ルールの案を書いてください。{extra}\n"
            f"集計の要約（コードが計算）：\n{digest}\n\n"
            "手順：get_company_rules でいまの会社ルールと Skill を読み、get_answers で人の答えを読む。"
            "そのうえで propose_rule で案を出す（1つの決まりごとに1件）。\n"
            "- value：status が same で、会社ルールの値と違う項目だけ（数値は書かず value_source=stat:<id>:mode）。\n"
            "- glb：status が varies の項目（会社ルールで決め打ちせず、物件ごとに glb の値を使う、と書く）。"
            "会社ルールが from_glb の項目は出さない。\n"
            "- skill：人の答えや glb の例外から、細かい決まりを短い文にする（Skill と学んだルールにすでにあるものは出さない）。\n"
            "- few（件数が足りない）の項目は値の案にしない。文の数値は根拠にある数値だけ。決まらないことは ask_question。\n"
            "最後に、何を出したかを日本語で1〜3文で書いてください。")


def _log(company: str, rec: dict) -> None:
    text = json.dumps(rec, ensure_ascii=False, default=str)
    assert_clean(text, "会社ルールの言語化の記録")
    p = rules_mod.learned_dir() / f"{company}.draft_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


async def _run_agent(ctx: RuleContext, prompt: str, conf: dict) -> tuple[str, float | None]:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("claude_code 接続なのに、環境に ANTHROPIC_API_KEY があります。API の料金になるため止めました。"
                           "API キーで動かすときは api_key 接続を選んでください。")
    m, limits = conf["claude_code"]["rule_draft"], conf["limits"]
    options = ClaudeAgentOptions(
        system_prompt=ROLE, model=m["model"], effort=m["effort"],
        mcp_servers={SERVER: build_server(ctx, RULE_TOOL_SPECS, rule_call)},
        tools=["Skill"], allowed_tools=["Skill"] + tool_names(RULE_TOOL_SPECS),
        permission_mode="dontAsk", hooks=deny_non_app_tools(),
        cwd=str(prepare_workdir(f"rules-{ctx.company}", ctx.company, skills_used=())), setting_sources=["project"],
        skills=[f"company-{ctx.company}"],
        max_turns=int(limits["rule_max_turns"]), max_budget_usd=float(limits["rule_max_budget_usd"]))
    result = None
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = msg
    if result is None or result.is_error:
        raise RuntimeError(f"Claude の答えが最後まで返りませんでした（{getattr(result, 'subtype', '結果なし')}）")
    return (result.result or "").strip(), result.total_cost_usd


async def _run_api(ctx: RuleContext, prompt: str, conf: dict, client=None) -> tuple[str, float]:
    m, limits = conf["api_key"]["rule_draft"], conf["limits"]
    own = client is None
    client = client or make_client()
    meter = Meter(prices=conf["prices_usd_per_mtok"], limit_usd=float(limits["rule_max_budget_usd"]))
    system = ROLE + "\n\n" + skill_text(ctx.company, names=())
    try:
        text, stop, _ = await tool_loop(client, ctx, prompt, model=m["model"], effort=m["effort"], system=system,
                                        tools=api_tools(RULE_TOOL_SPECS), meter=meter,
                                        max_turns=int(limits["rule_max_turns"]), call=rule_call)
    except Exception as e:
        raise RuntimeError(japanese_error(e)) from e
    finally:
        if own:
            await client.close()
    if stop == "refusal":
        raise RuntimeError("Claude が答えを断りました")
    return text, round(meter.cost_usd, 4)


async def run_draft(company: str, connection: str, text: str = "", *, client=None, ctx: RuleContext | None = None) -> dict:
    """会社ルールの案を出す（保存はしない。人が承認したら approve で学んだルールに入れる）。"""
    ctx = ctx or RuleContext.load(company)
    out = {"company": company, "connection": connection, "properties": ctx.aliases, "message": "",
           "proposals": [], "questions": [], "cost_usd": None}
    started = time.time()
    if connection == "off":
        for c in code_candidates(ctx.stats, company):
            try:
                propose_rule(ctx, c["kind"], c["wording"], c["stat_ids"], c["answer_ids"], c.get("rule_key"),
                             c.get("value_source"))
            except ToolError:
                continue
        for p in ctx.proposals:
            p["origin"] = "code"
        out["message"] = "AI なしなので、コードが決まった形で出した候補です"
    else:
        prompt = draft_prompt(ctx, text)
        assert_clean(prompt, "Claude に送るデータ")
        conf = load_ai_models()
        if connection == "claude_code":
            out["message"], out["cost_usd"] = await _run_agent(ctx, prompt, conf)
        elif connection == "api_key":
            out["message"], out["cost_usd"] = await _run_api(ctx, prompt, conf, client)
        else:
            raise ValueError("connection は claude_code・api_key・off のどれかです")
        for p in ctx.proposals:
            p["origin"] = connection
    out.update(proposals=ctx.proposals, questions=ctx.questions, at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    _log(company, {"at": out["at"], "connection": connection, "text": text, "properties": ctx.aliases,
                   "proposals": ctx.proposals, "questions": ctx.questions, "message": out["message"],
                   "cost_usd": out["cost_usd"], "duration_s": round(time.time() - started, 1)})
    p = rules_mod.learned_dir() / f"{company}.draft.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out


def last_draft(company: str) -> dict | None:
    p = rules_mod.learned_dir() / f"{company}.draft.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ---------------------------------------------------------------- 承認・却下・取り消し

def approve(company: str, proposal: dict) -> dict:
    """案を承認して学んだルールに入れる。数値・根拠の文は画面から受け取らず、いまの集計から計算し直す。"""
    ctx = RuleContext.load(company)
    try:
        p = validate_proposal(ctx, proposal.get("kind", ""), proposal.get("wording", ""), proposal.get("stat_ids"),
                              proposal.get("answer_ids"), proposal.get("rule_key"), proposal.get("value_source"),
                              origin=proposal.get("origin", "claude"))
    except ToolError as e:
        raise ValueError(str(e)) from e
    learned = ctx.learned
    item = {"id": f"L{learned['next_id']}", "kind": p["kind"], "text": p["wording"], "evidence": p["evidence"],
            "signature": p["signature"], "stat_ids": p["stat_ids"], "answer_ids": p["answer_ids"], "origin": p["origin"],
            "approved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    learned["next_id"] += 1
    if p["kind"] == "value":
        learned["values"][p["rule_key"]] = {**item, "value": p["to"], "previous": p["from"], "value_source": p["value_source"]}
    else:
        learned["notes"].append(item)
    save_learned(company, learned)
    return item


def reject(company: str, signature: str) -> dict:
    learned = load_learned(company)
    rec = {"signature": signature, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    learned["rejected"].append(rec)
    save_learned(company, learned)
    return rec


def undo(company: str, learned_id: str) -> dict:
    """学んだルールを1件取り消す。"""
    learned = load_learned(company)
    for k, v in list(learned["values"].items()):
        if v.get("id") == learned_id:
            del learned["values"][k]
            save_learned(company, learned)
            return v
    for n in list(learned["notes"]):
        if n.get("id") == learned_id:
            learned["notes"].remove(n)
            save_learned(company, learned)
            return n
    raise KeyError(f"{learned_id} という学んだルールはありません")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="会社ルールの言語化：集計と人の答えから、会社ルールの案を出す")
    ap.add_argument("company", nargs="?", default=default_company())
    ap.add_argument("--connection", default="off", choices=["claude_code", "api_key", "off"])
    ap.add_argument("--text", default="")
    args = ap.parse_args(argv)
    res = asyncio.run(run_draft(args.company, args.connection, args.text))
    print(f"【{args.company}】物件 {len(res['properties'])} 件から 案 {len(res['proposals'])} 件・質問 {len(res['questions'])} 件"
          f"（費用 {res['cost_usd']}）")
    print("返事：", res["message"][:400])
    for p in res["proposals"]:
        print(f"  [{p['kind']}] {p['wording']}" + (f"（{p['rule_key']}：{p['from']} → {p['to']}）" if p["kind"] == "value" else ""))
        print(f"        根拠：{p['evidence'][:200]}")
    for q in res["questions"]:
        print(f"  質問：{q['question']} 選択肢 {q['options']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
