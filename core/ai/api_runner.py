"""Claude による検品（SPEC §9-2）：api_key 接続（社外用。使う人の API キーで Messages API を呼ぶ）。

claude_code 接続（core/ai/agent.py、Agent SDK）と同じ動きを、Client SDK（anthropic）で作る（SPEC §7-4）。
- 面ごとの検品担当6人（S・N・E・W・plan・print）を同時に回す。それぞれが「道具を使うやりとり」を自分で回す。
- 渡す道具は HAKOYA の道具（core/ai/tools.py）だけ。ほかの道具は渡さない（SPEC §2-5）。
- Skill（skills/ の SKILL.md）の文章を、そのまま指示（system）として渡す（Agent SDK の Skill の代わり）。
- 最後にまとめ役が、構造化出力（output_config.format）で、決めた形の JSON を返す。
- 使ったトークンから費用を計算する。上限（limits.max_budget_usd）に達したら、次の呼び出しの前に止める。
- 直す案の数値は、結果を受け取った後にコードが出どころから計算し直す（agent.finalize、SPEC §2-4）。
- API キーは資格情報マネージャーから読む（core/secret_store.py）。環境変数は使わない。

使い方:  python -m core.ai.api_runner 物件A   → data/out/物件A/findings.json（★API の料金がかかる）
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.ai.agent import FACES, FINDINGS_SCHEMA, MULTI_PLAN_HOW, SKILLS_DIR, face_label, finalize, load_ai_models
from core.ai.tools import TOOL_SPECS, ToolContext, call_tool
from core.findings import code_findings
from core.findings import save as save_findings
from core.paths import out_dir
from core.privacy import PrivacyError, assert_clean
from core.rules import default_company
from core.secret_store import SecretStoreError

# 1回の返事の上限（考える分を含む）。この大きさなら、流しっぱなし（streaming）にしなくても時間切れにならない
MAX_TOKENS = 16000
SKILL_NAMES = ("inspection", "model-rules")
FACE_ROLE = ("あなたは住宅模型（1/50、3D プリント）の検品担当。日本語で考え、日本語で書く。"
             "使える道具は HAKOYA の道具だけ。数値（座標・寸法・高さ）は自分で作らず、直す案は propose_override に"
             "数値の出どころ（value_source）を渡して、コードに計算させる。次の Skill（検品の決まり・模型のルール・会社のルール）に従う。")
MERGE_ROLE = ("あなたは住宅模型（1/50、3D プリント）の検品のまとめ役。日本語で考え、日本語で書く。"
              "6人の検品担当（南・北・東・西・平面・印刷）の報告を読み、重複を除き、根拠のないものと Skill で「指摘しない」と"
              "決まっているものは捨て、重要度（高・中・低）順に並べて、決められた JSON で返す。"
              "指摘ごとに title（何が・どこで・どうおかしいかを20字くらいの見出し。例「西の窓の間の柱が細い」）と、"
              "message（1〜2文の説明）を書く。targets は部材の番号（W○・O○・Y○・Y○:S など）だけ。直す案は数値を書かず "
              "value_source（majority:種類 / rule:項目 / glb / number）で指定する。決まらないことは question にする。"
              "次の Skill（検品の決まり・模型のルール・会社のルール）に従う。")
NUDGE = "道具を使えるのは次が最後です。ここまでに分かったことで、指摘をまとめて返してください。"


class BudgetExceeded(RuntimeError):
    """費用が上限に達した。"""


@dataclass
class Meter:
    """使ったトークンと費用を数える（6人の検品担当とまとめ役で共有する）。"""
    prices: dict
    limit_usd: float
    cost_usd: float = 0.0
    calls: int = 0
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0})

    def check(self) -> None:
        """呼び出しの前に確かめる。上限に達していたら止める。"""
        if self.cost_usd >= self.limit_usd:
            raise BudgetExceeded(f"費用が上限（${self.limit_usd:.2f}）に達したので、検品を止めました（ここまでの費用 ${self.cost_usd:.2f}）。"
                                 "上限は rules/ai_models.yaml の limits.max_budget_usd で変えられます。")

    def add(self, model: str, usage) -> float:
        """返事1回分のトークンを足し、その回の費用（ドル）を返す。"""
        if model not in self.prices:
            raise RuntimeError(f"モデル {model} の料金が、rules/ai_models.yaml の prices_usd_per_mtok にありません")
        p = self.prices[model]
        used = {"input": getattr(usage, "input_tokens", 0) or 0, "output": getattr(usage, "output_tokens", 0) or 0,
                "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0}
        cost = sum(used[k] * p[k] for k in used) / 1_000_000
        for k, v in used.items():
            self.tokens[k] += v
        self.cost_usd += cost
        self.calls += 1
        return cost


def japanese_error(e: BaseException) -> str:
    """エラーを、画面に出せる日本語にする（API の英語のメッセージは記録にだけ残す）。"""
    if isinstance(e, (BudgetExceeded, SecretStoreError, PrivacyError)) or type(e) is RuntimeError:
        return str(e)
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "API キーが正しくありません。「ファイル」→「環境設定」で入れ直してください。"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "この API キーでは、決めたモデルを使えません。キーの権限を確かめてください。"
    if isinstance(e, anthropic.RateLimitError):
        return "API の利用の上限に達しました。少し待ってから、もう一度試してください。"
    if isinstance(e, anthropic.APIConnectionError):
        return "Claude の API につながりませんでした。インターネットの接続を確かめてください。"
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude の API がエラーを返しました（番号 {e.status_code}）。少し待ってから、もう一度試してください。"
    return f"検品の途中でエラーが起きました（{type(e).__name__}）。"


def make_client():
    """使う人の API キー（資格情報マネージャー）で、Claude の API の相手を作る。"""
    from core.secret_store import get_api_key
    key = get_api_key()
    if not key:
        raise RuntimeError("API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください。")
    import anthropic
    return anthropic.AsyncAnthropic(api_key=key)


def skill_text(company: str, names: tuple[str, ...] | None = None) -> str:
    """Skill の文章（SKILL.md の本文）をつなげる。Agent SDK の Skill の代わりに、指示（system）として渡す。
    会社の Skill には、承認した「学んだルール」の文章を足す。"""
    from core.rules import learned_skill_section
    parts = []
    for name in (*(SKILL_NAMES if names is None else names), f"company-{company}"):
        p = SKILLS_DIR / name / "SKILL.md"
        if not p.exists():
            continue
        body = p.read_text(encoding="utf-8")
        if body.startswith("---"):                 # 先頭の名札（name・description）は外す
            end = body.find("\n---", 3)
            if end != -1:
                body = body[end + 4:]
        if name == f"company-{company}" and learned_skill_section(company):
            body = body.rstrip() + "\n\n" + learned_skill_section(company)
        parts.append(f'<skill name="{name}">\n{body.strip()}\n</skill>')
    return "\n\n".join(parts)


def api_schema(schema):
    """構造化出力に渡せる形にする：すべての object に additionalProperties: false を付け、
    型の並び（["number", "null"]）を anyOf に直す。"""
    if isinstance(schema, list):
        return [api_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "properties":
            out[k] = {name: api_schema(s) for name, s in v.items()}
        else:
            out[k] = api_schema(v)
    t = out.get("type")
    if isinstance(t, list):
        rest = {k: v for k, v in out.items() if k != "type"}
        return {"anyOf": [{"type": "null"} if x == "null" else {"type": x, **rest} for x in t]}
    if t == "object":
        out["additionalProperties"] = False
    return out


def api_tools(specs: dict | None = None) -> list[dict]:
    """HAKOYA の道具を、Messages API の道具の形にする（ほかの道具は渡さない）。"""
    return [{"name": n, "description": s["description"], "input_schema": s["schema"]} for n, s in (specs or TOOL_SPECS).items()]


def face_prompt(face: str, multi: bool = False) -> str:
    label = face_label(face, multi)
    if face == "print":
        how = "run_checks の結果と、各面の模型の図（render_view source=model）を見て、印刷と組み立ての問題を探してください。"
    elif multi and face == "plan":
        how = MULTI_PLAN_HOW
    else:
        how = (f"render_view で面 {face}（{label}）の模型の図（source=model）と glb の図（source=glb）を両方見比べ、"
               f"get_elements（view={face}）で部材の番号を確かめてください。")
    return (f"物件の模型を検品してください。あなたの担当は「{label}」です。{how}"
            "食い違いを見つけたら、1件ずつ、面（view）・種類（kind）・重要度（severity）・見出し（title）・内容（message）・"
            "対象の番号（targets）・根拠（evidence）・直す案（proposal）・質問（question）・確からしさ（confidence）が分かるように、"
            "日本語の箇条書きで返してください。見出しは「何が・どこで・どうおかしいか」を20字くらいで（例「西の窓の間の柱が細い」）、"
            "内容は1〜2文の説明にしてください。対象の番号は get_elements が返した番号（W○・O○・Y○・Y○:S など）だけにしてください。"
            "直す案は propose_override で試し、数値は自分で書かないでください。見つからなければ「指摘なし」と返してください。")


def tool_result(ctx, block, call=None) -> dict:
    """道具を1回動かし、結果を tool_result にする。画像は Claude に見せ、パスは渡さない。
    call を渡すと、その呼び方で道具を動かす（会社ルールの道具など。省くと検品・囲んで直すの道具）。"""
    res = (call or call_tool)(ctx, block.name, dict(block.input or {}))
    text = json.dumps({k: v for k, v in res.items() if k != "image_path"}, ensure_ascii=False)
    assert_clean(text, "Claude に送るデータ")
    content: list[dict] = [{"type": "text", "text": text}]
    if "image_path" in res:
        data = base64.b64encode(Path(res["image_path"]).read_bytes()).decode("ascii")
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
    return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": "error" in res}


def _blocks(content) -> list[dict]:
    """返事の中身を、記録に残せる形にする（考えた中身と画像は入れない）。"""
    out = []
    for b in content:
        if b.type == "text":
            out.append({"text": b.text[:3000]})
        elif b.type == "tool_use":
            out.append({"tool_use": b.name, "id": b.id, "input": b.input})
        elif b.type in ("thinking", "redacted_thinking"):
            out.append({"thinking": True})
    return out


async def tool_loop(client, ctx, prompt: str, *, model: str, effort: str, system, tools: list[dict],
                    meter: Meter, max_turns: int, on_turn=None, on_tool=None, call=None) -> tuple[str, str, int]:
    """道具を使うやりとりを、終わるまで（または上限まで）回す。返り値：(最後の文章, 止まった理由, 回数)。
    検品の面ごとの担当と、囲んで直すの両方で使う。"""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    for turn in range(1, max_turns + 1):
        meter.check()
        # 指示（system）の終わりと、やりとりの最後に「覚えておく印」を付け、2回目からは安い料金で読み直させる
        resp = await client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system, tools=tools,
                                            messages=messages, output_config={"effort": effort},
                                            cache_control={"type": "ephemeral"})
        cost = meter.add(model, resp.usage)
        if on_turn:
            on_turn(turn, resp, cost)
        uses = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            return "\n".join(b.text for b in resp.content if b.type == "text").strip(), resp.stop_reason, turn
        messages.append({"role": "assistant", "content": resp.content})     # 考えた中身も含め、そのまま返す
        results = []
        for b in uses:
            if on_tool:
                on_tool(b.name)
            results.append(tool_result(ctx, b, call))
        if turn == max_turns - 1:
            results.append({"type": "text", "text": NUDGE})
        messages.append({"role": "user", "content": results})
    raise RuntimeError(f"やりとりの上限（{max_turns} 回）までに終わりませんでした")


async def run_face(client, ctx: ToolContext, face: str, *, model: str, effort: str, system, tools: list[dict],
                   meter: Meter, log, max_turns: int) -> dict:
    """1つの面の検品担当。道具を使うやりとりを回し、報告の文章を返す。"""
    log({"type": "FaceStarted", "face": face})
    try:
        text, stop, turns = await tool_loop(
            client, ctx, face_prompt(face, len(ctx.building.get("levels", [])) > 1), model=model, effort=effort, system=system, tools=tools, meter=meter,
            max_turns=max_turns,
            on_turn=lambda turn, resp, cost: log({"type": "FaceTurn", "face": face, "turn": turn, "stop_reason": resp.stop_reason,
                                                  "cost_usd": round(cost, 4), "content": _blocks(resp.content)}),
            on_tool=lambda name: log({"type": "FaceTool", "face": face, "tool": name}))
    except Exception as e:
        log({"type": "FaceDone", "face": face, "status": "失敗", "error": japanese_error(e), "detail": repr(e)[:500]})
        raise
    status, note = "済み", ""
    if stop == "refusal":
        status, note = "失敗", "Claude が答えを断りました"
    elif stop == "max_tokens":
        note = "返事が長すぎて、途中で切れました"
    log({"type": "FaceDone", "face": face, "status": status, "summary": (note or text)[:200]})
    return {"face": face, "status": status, "report": text, "note": note, "turns": turns}


async def merge(client, ctx: ToolContext, reports: list[dict], *, model: str, effort: str, system: str,
                meter: Meter, log) -> tuple[dict, str]:
    """まとめ役：6人の報告から、重複を除いて重要度順に並べ、決めた形の JSON にする（道具は使わない1回の呼び出し）。"""
    summary = call_tool(ctx, "get_building", {})
    parts = [f"## 家の要約（get_building）\n{json.dumps(summary, ensure_ascii=False)}"]
    for r in reports:
        body = r["report"] or f"（報告なし：{r.get('note') or r['status']}）"
        parts.append(f"## 検品担当「{FACES[r['face']]}」（view={r['face']}）の報告\n{body}")
    text = "次の報告をまとめて、指摘の JSON を返してください。\n\n" + "\n\n".join(parts)
    assert_clean(text, "Claude に送るデータ")
    log({"type": "MergeStarted"})
    meter.check()
    resp = await client.messages.create(
        model=model, max_tokens=MAX_TOKENS, system=system, messages=[{"role": "user", "content": text}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": api_schema(FINDINGS_SCHEMA)}})
    cost = meter.add(model, resp.usage)
    out_text = next((b.text for b in resp.content if b.type == "text"), "")
    log({"type": "MergeDone", "stop_reason": resp.stop_reason, "cost_usd": round(cost, 4), "content": _blocks(resp.content)})
    if resp.stop_reason == "refusal":
        raise RuntimeError("まとめ役の Claude が答えを断りました")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("まとめ役の返事が長すぎて、途中で切れました")
    try:
        return json.loads(out_text), out_text
    except json.JSONDecodeError as e:
        raise RuntimeError("まとめ役の返事が、決めた形の JSON になっていませんでした") from e


async def run_inspection_api(alias: str, *, client=None, ctx: ToolContext | None = None, out: Path | None = None) -> dict:
    """api_key 接続で検品し、findings.json を書く。client・ctx・out は試験のときに差し替える。"""
    conf = load_ai_models()
    models, limits = conf["api_key"], conf["limits"]
    own_client = client is None
    client = client or make_client()
    ctx = ctx or ToolContext.load(alias)
    out = out or out_dir(alias)
    company = ctx.raw["source"].get("company_rules", default_company())
    skills = skill_text(company)
    face_system = [{"type": "text", "text": FACE_ROLE + "\n\n" + skills, "cache_control": {"type": "ephemeral"}}]
    fm, mm = models["inspect_face"], models["inspect_merge"]
    meter = Meter(prices=conf["prices_usd_per_mtok"], limit_usd=float(limits["max_budget_usd"]))
    started = time.time()
    try:
        with (out / "inspect_log.jsonl").open("w", encoding="utf-8") as f:
            def log(rec: dict) -> None:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                f.flush()
            try:
                results = await asyncio.gather(*(run_face(
                    client, ctx, face, model=fm["model"], effort=fm["effort"], system=face_system, tools=api_tools(),
                    meter=meter, log=log, max_turns=int(limits["max_turns_per_face"])) for face in FACES),
                    return_exceptions=True)
                errors = [r for r in results if isinstance(r, BaseException)]
                for e in errors:
                    if isinstance(e, BudgetExceeded):
                        raise e
                reports = [r for r in results if isinstance(r, dict)]
                if not any(r["status"] == "済み" for r in reports):
                    raise errors[0] if errors else RuntimeError("どの面の検品も終わりませんでした")
                data, raw_text = await merge(client, ctx, reports, model=mm["model"], effort=mm["effort"],
                                             system=MERGE_ROLE + "\n\n" + skills, meter=meter, log=log)
            except Exception as e:
                log({"type": "ApiResult", "is_error": True, "error": japanese_error(e), "detail": repr(e)[:500],
                     "cost_usd": round(meter.cost_usd, 4), "calls": meter.calls, "tokens": meter.tokens})
                raise RuntimeError(japanese_error(e)) from e
            log({"type": "ApiResult", "is_error": False, "cost_usd": round(meter.cost_usd, 4), "calls": meter.calls,
                 "tokens": meter.tokens})
    finally:
        if own_client:
            await client.close()
    faces = {face: (r["status"] if isinstance(r, dict) else "失敗") for face, r in zip(FACES, results)}
    (out / "inspect_result_raw.json").write_text(json.dumps({"merge_text": raw_text, "face_reports": reports},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"alias": alias, "connection": "api_key", "summary": data.get("summary", ""),
              "findings": finalize(ctx, data.get("findings", [])),
              "cost_usd": round(meter.cost_usd, 4), "duration_s": round(time.time() - started, 1), "turns": meter.calls,
              "tokens": meter.tokens, "models": {"inspect_face": fm["model"], "inspect_merge": mm["model"]}, "faces": faces,
              "questions": ctx.questions,      # 検品担当が ask_question で出した質問（人の答えを待つもの）
              "code_findings": code_findings(alias)}
    save_findings(alias, report, out / "findings.json")     # 一時ファイルに書いてから入れ替える
    return report


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser(description="API キーで、Claude に模型を検品させる（API の料金がかかる）")
    ap.add_argument("alias")
    args = ap.parse_args(argv)
    rep = asyncio.run(run_inspection_api(args.alias))
    print(f"【{args.alias}】検品（API キー）：指摘 {len(rep['findings'])} 件（{rep['duration_s']} 秒、"
          f"費用 ${rep['cost_usd']:.2f}、呼び出し {rep['turns']} 回）")
    for f in rep["findings"]:
        print(f"  [{f['severity']}] {f['id']} {f['message']}" + ("　→ 直す案あり" if f.get("proposal") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
