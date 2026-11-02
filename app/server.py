"""画面のサーバー（SPEC §10）。自分の PC の中だけで動き（127.0.0.1）、起動するとブラウザが開く。

使い方:  start.bat（中で python -m app.server）

画面とやりとりするのは物件の仮名だけ。実ファイルの名前（施主名が入る）は送らない（SPEC §2-2）。
ダウンロードのファイル名は英字にする（日本語を HTTP のヘッダに載せない）。
"""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adapters.walkinhome import build, write_building
from core import overrides as ov
from core.export import write_outputs
from core.paths import load_local_paths, out_dir, property_files
from core.privacy import assert_clean
from tools.plan_preview import render_svg

HOST, PORT = "127.0.0.1", 8765
STATIC = Path(__file__).resolve().parent / "static"
MODEL_FILES = {"model.glb": "model/gltf-binary", "model.3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
               "model_bambu.3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
               "foundation.stl": "model/stl", "floor1.stl": "model/stl", "exterior1.stl": "model/stl",
               "roof.stl": "model/stl", "roof_ext.stl": "model/stl", "checks.json": "application/json"}

app = FastAPI(title="HAKOYA")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
def plan(alias: str):
    _check_alias(alias)
    b, _ = _effective(alias)
    svg = render_svg(b, alias)
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
    except KeyError as e:
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
    summary = write_outputs(alias)
    return _safe_json({"undone": item, "checks": summary["checks"]})


def main() -> int:
    import uvicorn
    sys.stdout.reconfigure(encoding="utf-8")
    url = f"http://{HOST}:{PORT}/"
    print(f"HAKOYA を起動しました → {url}（止めるときはこの画面で Ctrl+C）")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
