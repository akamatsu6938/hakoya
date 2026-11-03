"""画面のサーバー（SPEC §10）。自分の PC の中だけで動き（127.0.0.1）、起動するとブラウザが開く。

使い方:  start.bat（中で python -m app.server）

画面とやりとりするのは物件の仮名だけ。実ファイルの名前（施主名が入る）は送らない（SPEC §2-2）。
ダウンロードのファイル名は英字にする（日本語を HTTP のヘッダに載せない）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adapters.walkinhome import build, write_building
from core import findings as fd
from core import overrides as ov
from core import secret_store
from core.export import write_outputs
from core.paths import out_dir, property_files
from core.privacy import assert_clean
from tools.plan_preview import render_svg

HOST, PORT = "127.0.0.1", 8765
STATIC = Path(__file__).resolve().parent / "static"
MODEL_FILES = {"model.glb": "model/gltf-binary", "model.3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
               "model_bambu.3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
               "foundation.stl": "model/stl", "floor1.stl": "model/stl", "exterior1.stl": "model/stl",
               "roof.stl": "model/stl", "roof_ext.stl": "model/stl", "checks.json": "application/json",
               # 2階建て（core/builder_multi.py）の部品
               "roof_lower.stl": "model/stl", "roof_lower_ext.stl": "model/stl", "floor2.stl": "model/stl", "exterior2.stl": "model/stl"}

app = FastAPI(title="HAKOYA")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """書き換える操作（GET 以外）は、HAKOYA の画面から来たものだけ受け付ける。
    ほかのサイトを開いたブラウザから、勝手に検品（API の料金がかかる）を始められたり、キーを書き換えられたりしないため。"""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != f"http://{request.headers.get('host', '')}":
        return JSONResponse({"detail": "HAKOYA の画面以外からの操作は受け付けません"}, status_code=403)
    return await call_next(request)


def _aliases() -> list[str]:
    from core.paths import aliases
    return aliases()


def _check_alias(alias: str) -> None:
    if alias not in _aliases():
        raise HTTPException(404, "その物件はありません")


def _building_path(alias: str) -> Path:
    return out_dir(alias) / "building.json"


def _effective(alias: str) -> tuple[dict, list[dict]]:
    """自動の値に人の修正を当てたもの。"""
    p = _building_path(alias)
    if not p.exists():
        raise HTTPException(409, "まだ組み立てていません。「読み込んで組み立てる」を押してください")
    return ov.apply(json.loads(p.read_text(encoding="utf-8")), ov.load(alias))


def _safe_json(data) -> Response:
    text = json.dumps(data, ensure_ascii=False)
    assert_clean(text, "画面に送るデータ")
    return Response(text, media_type="application/json; charset=utf-8")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/properties")
def properties():
    out = []
    for a in _aliases():
        f = property_files(a)
        out.append({"alias": a, "has_glb": bool(f.glb and f.glb.exists()), "built": _building_path(a).exists()})
    return _safe_json(out)


@app.post("/api/build/{alias}")
def build_property(alias: str):
    """glb を読み直して building.json を作り（番号は前回から引き継ぐ）、模型を組み立てて書き出す。"""
    _check_alias(alias)
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから組み立ててください")
    write_building(alias, build(alias))
    summary = write_outputs(alias)
    return _safe_json(summary)


@app.get("/api/building/{alias}")
def building(alias: str):
    _check_alias(alias)
    b, results = _effective(alias)
    return _safe_json({"building": b, "override_results": results})


@app.get("/api/rulecheck/{alias}")
def rule_check(alias: str):
    """予防：この物件を会社ルール（学んだルールを含む）と比べた結果。家ごとに変わる値は比べない。"""
    _check_alias(alias)
    from core.rule_check import check_property
    from core.rules import load_rules
    b, _ = _effective(alias)
    return _safe_json({"rows": check_property(b, load_rules(b["source"].get("company_rules", "jisha")))})


DRAWING_CACHE: dict[str, tuple] = {}     # 物件ごとの（building・修正の更新時刻, 照合の結果）。位置合わせに数秒かかるため


@app.get("/api/drawing/{alias}")
def drawing_check(alias: str):
    """図面（jww）と比べた結果：平面図の窓・テラスドアの注記（品番と上端の FL+）と模型の開口の上端。数値はコードが読む。"""
    _check_alias(alias)
    from core.drawing_check import summary
    key = tuple(p.stat().st_mtime if p.exists() else 0 for p in (_building_path(alias), ov.overrides_path(alias)))
    hit = DRAWING_CACHE.get(alias)
    if hit and hit[0] == key:
        return _safe_json(hit[1])
    b, _ = _effective(alias)
    try:
        result = summary(alias, b)
    except Exception as e:                                    # 図面が読めない（壊れている・ライブラリがない）
        result = {"available": False, "note": f"図面を読めませんでした（{type(e).__name__}）"}
    DRAWING_CACHE[alias] = (key, result)
    return _safe_json(result)


@app.get("/api/plan/{alias}")
def plan(alias: str, level: str | None = None):
    """平面図。2階建ては level（1F・2F）で階を選ぶ（省くと1階）。"""
    _check_alias(alias)
    b, _ = _effective(alias)
    if level is not None and level not in [lv["name"] for lv in b.get("levels", [])]:
        raise HTTPException(400, "その階はありません")
    svg = render_svg(b, alias, level)
    assert_clean(svg, "平面図")
    return Response(svg, media_type="image/svg+xml; charset=utf-8")


@app.get("/api/model/{alias}/{name}")
def model_file(alias: str, name: str):
    _check_alias(alias)
    if name not in MODEL_FILES:
        raise HTTPException(404, "そのファイルはありません")
    p = out_dir(alias) / "model" / name
    if not p.exists():
        raise HTTPException(409, "まだ書き出していません")
    return FileResponse(p, media_type=MODEL_FILES[name], filename=f"hakoya_{name}")


@app.get("/api/overrides/{alias}")
def list_overrides(alias: str):
    _check_alias(alias)
    return _safe_json(ov.load(alias))


class OverrideIn(BaseModel):
    target: str
    field: str
    to: float | int | str | list | None = None
    reason: str = ""


@app.post("/api/overrides/{alias}")
def add_override(alias: str, body: OverrideIn):
    """表で直した値を、人が承認した修正として1件記録し、模型を組み立て直す。"""
    _check_alias(alias)
    raw = json.loads(_building_path(alias).read_text(encoding="utf-8"))
    try:
        item = ov.add(alias, raw, body.target, body.field, body.to, "human:table", body.reason or "表で直した")
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    summary = _rebuild_or_undo(alias, item["id"])
    return _safe_json({"override": item, "checks": summary["checks"]})


def _rebuild_or_undo(alias: str, override_id: str | None) -> dict:
    """組み直す。組み立てられなければ、足したばかりの修正を取り消して元の模型に戻し、400 を返す
    （壊れた修正が残って画面が開けなくならないように）。"""
    try:
        return write_outputs(alias)
    except Exception as e:                                   # 形の計算などの失敗。種類だけ伝える
        if not override_id:
            raise
        ov.undo(alias, override_id)
        fd.revert_override(alias, override_id)
        write_outputs(alias)
        raise HTTPException(400, f"この値では模型を組み立てられませんでした。修正は取り消しました（{type(e).__name__}）")


@app.delete("/api/overrides/{alias}/{override_id}")
def undo_override(alias: str, override_id: str):
    _check_alias(alias)
    try:
        item = ov.undo(alias, override_id)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    fd.revert_override(alias, override_id)      # この修正を生んだ指摘は「未確認」に戻す
    summary = write_outputs(alias)
    return _safe_json({"undone": item, "checks": summary["checks"]})


# ---------------------------------------------------------------- 検品（SPEC §9-2・§10）

RUNS: dict[str, dict] = {}           # 物件ごとの検品の実行状態
RUNS_LOCK = threading.Lock()


def _run_inspection_thread(alias: str, connection: str) -> None:
    import asyncio
    state = RUNS[alias]
    try:
        from core.render import render_all
        render_all(alias)                                        # 面ごとの図を最新の模型で作り直す
        if connection == "off":
            fd.run_off(alias)
        elif connection == "api_key":
            from core.ai.api_runner import run_inspection_api
            rep = asyncio.run(run_inspection_api(alias))
            state["cost_usd"] = rep.get("cost_usd")
        else:
            from core.ai.agent import run_inspection
            rep = asyncio.run(run_inspection(alias, connection))
            state["cost_usd"] = rep.get("cost_usd")
    except Exception as e:  # 画面に日本語で出す
        state["error"] = str(e)
    finally:
        state["running"] = False
        state["finished_at"] = time.time()


@app.post("/api/inspect/{alias}")
def start_inspection(alias: str, connection: str = "claude_code"):
    """検品を始める（裏で動く）。connection は claude_code（Claude のプラン）・api_key（API キー）・off（コードの検査だけ）。"""
    _check_alias(alias)
    if connection not in ("claude_code", "api_key", "off"):
        raise HTTPException(400, "connection は claude_code・api_key・off のどれかです")
    if connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「設定」タブで API キーを入れてください")
    with RUNS_LOCK:
        if RUNS.get(alias, {}).get("running"):
            raise HTTPException(409, "この物件はいま検品中です")
        if connection != "off":
            log = out_dir(alias) / "inspect_log.jsonl"
            if log.exists():
                log.unlink()                                     # 前回の記録を消して、進み具合を最初から数える
        RUNS[alias] = {"running": True, "connection": connection, "started_at": time.time(), "error": None}
    threading.Thread(target=_run_inspection_thread, args=(alias, connection), daemon=True).start()
    return _safe_json({"started": True, "connection": connection})


@app.get("/api/inspect/{alias}/status")
def inspection_status(alias: str):
    _check_alias(alias)
    from core.ai.progress import FACE_LABELS, read_progress
    state = dict(RUNS.get(alias, {"running": False}))
    if state.get("started_at"):
        state["elapsed_s"] = round((state.get("finished_at") or time.time()) - state["started_at"])
    prog = read_progress(out_dir(alias) / "inspect_log.jsonl") if state.get("connection") in ("claude_code", "api_key") else None
    return _safe_json({"state": state, "progress": prog, "face_labels": FACE_LABELS})


@app.get("/api/findings/{alias}")
def findings(alias: str):
    _check_alias(alias)
    return _safe_json(fd.load(alias) or {"findings": [], "code_findings": [], "summary": "まだ検品していません"})


class DecisionIn(BaseModel):
    action: str


@app.post("/api/findings/{alias}/{fid}")
def decide_finding(alias: str, fid: str, body: DecisionIn):
    """指摘を承認／却下／あとでにする。承認すると修正として記録し、模型を組み直す。"""
    _check_alias(alias)
    try:
        f = fd.decide(alias, fid, body.action)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\""))
    if body.action == "approve":
        _rebuild_or_undo(alias, f.get("override"))
    return _safe_json({"finding": f})


@app.get("/api/inspect/{alias}/image/{view}")
def inspection_image(alias: str, view: str, hl: str = "", source: str = "model"):
    """面の図（検品の材料）。hl に部材の番号をカンマ区切りで渡すと、赤い枠で強調する。"""
    _check_alias(alias)
    from core.ai.tools import ToolContext, call_tool
    res = call_tool(ToolContext.load(alias), "render_view",
                    {"view": view, "source": source, "highlight": [h for h in hl.split(",") if h]})
    if "error" in res:
        raise HTTPException(400, res["error"])
    return FileResponse(res["image_path"], media_type="image/png", filename=f"hakoya_{view}_{source}.png")


# ---------------------------------------------------------------- 囲んで直す（SPEC §9-3）

class LassoIn(BaseModel):
    polygon: list[list[float]] | None = None   # 平面図の囲み（平面の mm。平面図の画面が座標を直して送る）
    hits: list[dict] | None = None             # 3D の囲み：部材ごとの {id, inside, total}（画面が数えた画素の数）
    mask_px: int = 0                           # 3D の囲みの広さ（画素）
    view: str | None = None                    # 3D の画面をどちらから見ていたか（S/N/E/W/plan）
    text: str = ""
    connection: str = "off"
    level: str | None = None             # 2階建て：表示している階（平面図の囲みで、その階の部材だけを選ぶ）


def _lasso_selection(b: dict, body: LassoIn) -> dict:
    from core import lasso as geo
    if body.hits is not None:
        return geo.select_hits(b, body.hits, body.mask_px)
    if body.polygon:
        return geo.select(b, body.polygon, body.level)
    raise ValueError("囲みがありません。もう一度囲んでください")


@app.get("/api/pick/{alias}")
def pick_proxies(alias: str):
    """3D で囲んで直すの当たり判定用の形（部材ごと、実寸 mm）。修正を当てた後の家で作る。"""
    _check_alias(alias)
    from core.pick import proxies
    from core.rules import load_rules
    b, _ = _effective(alias)
    return _safe_json(proxies(b, load_rules(b["source"].get("company_rules", "jisha"))))


@app.post("/api/lasso/{alias}/select")
def lasso_select(alias: str, body: LassoIn):
    """囲みの中の部材（コードが決める）。"""
    _check_alias(alias)
    from core import lasso as geo
    b, _ = _effective(alias)
    try:
        sel = _lasso_selection(b, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _safe_json({"selection": geo.describe(b, sel)})


@app.post("/api/lasso/{alias}/ask")
def lasso_ask(alias: str, body: LassoIn):
    """囲みと一言を Claude に渡し、直す案か質問を受け取る（保存はしない）。30秒〜数分かかる。"""
    import asyncio
    _check_alias(alias)
    _effective(alias)                                   # 組み立て前なら 409
    if body.connection not in ("claude_code", "api_key", "off"):
        raise HTTPException(400, "connection は claude_code・api_key・off のどれかです")
    if body.connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「設定」タブで API キーを入れてください")
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから頼んでください")
    from core.ai.lasso import run_lasso
    try:
        res = asyncio.run(run_lasso(alias, body.connection, body.polygon, body.text, level=body.level,
                                    hits=body.hits, mask_px=body.mask_px, view=body.view))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return _safe_json(res)


class LassoApproveIn(BaseModel):
    target: str
    field: str
    value_source: str
    reason: str = ""


@app.post("/api/lasso/{alias}/approve")
def lasso_approve(alias: str, body: LassoApproveIn):
    """直す案を承認して修正として記録し、模型を組み直す。数値はサーバーが出どころから計算し直す。"""
    _check_alias(alias)
    from core.ai.lasso import approve
    try:
        item = approve(alias, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    summary = _rebuild_or_undo(alias, item["id"])
    return _safe_json({"override": item, "checks": summary["checks"]})


# ---------------------------------------------------------------- 会社ルールの言語化（SPEC §5-4②・§9-1）
# 家ごとに変わる値は物件ごとに glb から作る。会社で変わらない値と細かい決まりを会社ルールとして残し、予防に使う（作者 11/3）

RULE_DRAFTING = threading.Lock()      # 案出しは同時に1つだけ


def _check_company(company: str) -> None:
    from core.rules import RULES_DIR
    if not company.isascii() or not company.replace("_", "").replace("-", "").isalnum() or \
            not (RULES_DIR / f"{company}.yaml").exists():
        raise HTTPException(404, "その会社ルールはありません")


@app.get("/api/rules/{company}")
def company_rules(company: str):
    """集計（コード）・学んだルール・最後の案・AI なしの候補。"""
    _check_company(company)
    from core.ai.rule_draft import last_draft
    from core.rule_stats import code_candidates, collect, entries
    from core.rules import apply_learned, load_learned, load_rules
    stats = collect(company)
    learned = load_learned(company)
    _, skipped = apply_learned(load_rules(company, learned=False), learned)
    return _safe_json({"company": company, "properties": [a for a, _ in entries(company)], "stats": stats,
                       "learned": {"values": learned["values"], "notes": learned["notes"]}, "learned_skipped": skipped,
                       "rejected": [r.get("signature") for r in learned["rejected"]],
                       "last_draft": last_draft(company), "candidates": code_candidates(stats, company)})


class RuleDraftIn(BaseModel):
    connection: str = "off"
    text: str = ""


@app.post("/api/rules/{company}/draft")
def rule_draft(company: str, body: RuleDraftIn):
    """会社ルールの案を出す（保存はしない）。Claude のときは1〜数分かかる。"""
    import asyncio
    _check_company(company)
    if body.connection not in ("claude_code", "api_key", "off"):
        raise HTTPException(400, "connection は claude_code・api_key・off のどれかです")
    if body.connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「設定」タブで API キーを入れてください")
    if not RULE_DRAFTING.acquire(blocking=False):
        raise HTTPException(409, "いま会社ルールの案を作っています。終わってからもう一度押してください")
    try:
        from core.ai.rule_draft import run_draft
        return _safe_json(asyncio.run(run_draft(company, body.connection, body.text)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    finally:
        RULE_DRAFTING.release()


class RuleProposalIn(BaseModel):
    kind: str
    wording: str
    stat_ids: list[str] = []
    answer_ids: list[str] = []
    rule_key: str | None = None
    value_source: str | None = None
    origin: str = "claude"


@app.post("/api/rules/{company}/approve")
def rule_approve(company: str, body: RuleProposalIn):
    """案を承認して学んだルールに入れる。数値と根拠の文は、いまの集計からサーバーが計算し直す。"""
    _check_company(company)
    from core.ai.rule_draft import approve
    try:
        return _safe_json({"learned": approve(company, body.model_dump())})
    except ValueError as e:
        raise HTTPException(400, str(e))


class RuleRejectIn(BaseModel):
    signature: str


@app.post("/api/rules/{company}/reject")
def rule_reject(company: str, body: RuleRejectIn):
    """案を却下する（同じ案は二度出さない）。"""
    _check_company(company)
    from core.ai.rule_draft import reject
    return _safe_json({"rejected": reject(company, body.signature)})


@app.delete("/api/rules/{company}/learned/{learned_id}")
def rule_undo(company: str, learned_id: str):
    """学んだルールを1件取り消す。"""
    _check_company(company)
    from core.ai.rule_draft import undo
    try:
        return _safe_json({"undone": undo(company, learned_id)})
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))


class RuleAnswerIn(BaseModel):
    question: str
    answer: str


@app.post("/api/rules/{company}/answers")
def rule_answer(company: str, body: RuleAnswerIn):
    """Claude の質問への人の答えを記録する（同じ質問を二度しない・会社ルールの根拠になる）。"""
    _check_company(company)
    from core.rules import add_answer
    from core.privacy import PrivacyError
    if not body.question.strip() or not body.answer.strip():
        raise HTTPException(400, "質問と答えを書いてください")
    try:   # 施主名などが入っていたら記録しない（書く前に確かめる）
        assert_clean(body.question + "\n" + body.answer, "人の答え")
    except PrivacyError as e:
        raise HTTPException(400, str(e))
    return _safe_json({"answer": add_answer(body.question.strip(), body.answer.strip())})


# ---------------------------------------------------------------- 設定（SPEC §10 の 8）

@app.get("/api/settings")
def settings():
    """設定の状態。API キーは「入っているかどうか」だけを返す（キーそのものは返さない）。
    組み立てたあとの検品（auto_inspect）と、選べるつなぎ方・API の1回の上限も返す。"""
    from core import settings_store
    from core.ai.agent import load_ai_models
    try:
        key = secret_store.has_api_key()
    except secret_store.SecretStoreError as e:
        raise HTTPException(500, str(e))
    st = settings_store.load()
    return _safe_json({"api_key_set": key, "auto_inspect": st["auto_inspect"], "api_cost_ack": st["api_cost_ack"],
                       "connections": settings_store.available_connections(),
                       "max_budget_usd": float(load_ai_models()["limits"]["max_budget_usd"])})


class AutoInspectIn(BaseModel):
    value: str
    cost_ack: bool = False


@app.post("/api/settings/auto_inspect")
def save_auto_inspect(body: AutoInspectIn):
    """組み立てたあとの検品のしかたを保存する（DATA_DIR/settings.json。API キーは入れない）。"""
    from core import settings_store
    try:
        st = settings_store.set_auto_inspect(body.value, body.cost_ack)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _safe_json({"auto_inspect": st["auto_inspect"], "api_cost_ack": st["api_cost_ack"]})


class ApiKeyIn(BaseModel):
    api_key: str


@app.post("/api/settings/api_key")
def save_api_key(body: ApiKeyIn):
    """API キーを Windows の資格情報マネージャーに保存する（ファイルや記録には書かない）。"""
    try:
        secret_store.set_api_key(body.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except secret_store.SecretStoreError as e:
        raise HTTPException(500, str(e))
    return _safe_json({"api_key_set": True})


@app.delete("/api/settings/api_key")
def remove_api_key():
    try:
        secret_store.delete_api_key()
    except secret_store.SecretStoreError as e:
        raise HTTPException(500, str(e))
    return _safe_json({"api_key_set": False})


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


def main() -> int:
    import uvicorn
    sys.stdout.reconfigure(encoding="utf-8")
    port = int(os.environ.get("HAKOYA_PORT", PORT))       # 口の番号は HAKOYA_PORT で変えられる
    url = f"http://{HOST}:{port}/"
    if _port_in_use(port):
        # ★古い HAKOYA が開いたままだと、新しい方が起動できない（2026-11-02、試験で起きた）
        print(f"口 {port} 番はもう使われています。HAKOYA がすでに起動しているかもしれません。"
              f"前に開いた黒い画面を閉じてから、もう一度起動してください。")
        return 3
    print(f"HAKOYA を起動しました → {url}（止めるときはこの画面で Ctrl+C）")
    if not os.environ.get("HAKOYA_NO_BROWSER"):      # 試験のときはブラウザを開かない
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
