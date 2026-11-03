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
from core.paths import load_local_paths, out_dir, property_files
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
    return [k for k, v in load_local_paths().items() if isinstance(v, dict)]


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
    write_building(alias, build(alias))
    summary = write_outputs(alias)
    return _safe_json(summary)


@app.get("/api/building/{alias}")
def building(alias: str):
    _check_alias(alias)
    b, results = _effective(alias)
    return _safe_json({"building": b, "override_results": results})


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
    summary = write_outputs(alias)
    return _safe_json({"override": item, "checks": summary["checks"]})


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
        write_outputs(alias)
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
    summary = write_outputs(alias)
    return _safe_json({"override": item, "checks": summary["checks"]})


# ---------------------------------------------------------------- 設定（SPEC §10 の 8）

@app.get("/api/settings")
def settings():
    """設定の状態。API キーは「入っているかどうか」だけを返す（キーそのものは返さない）。"""
    try:
        return _safe_json({"api_key_set": secret_store.has_api_key()})
    except secret_store.SecretStoreError as e:
        raise HTTPException(500, str(e))


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
