"""画面のサーバー（SPEC §10）。自分の PC の中だけで動き（127.0.0.1）、起動するとブラウザが開く。

使い方:  start.bat（中で python -m app.server）

画面とやりとりするのは物件の仮名だけ。実ファイルの名前（施主名が入る）は送らない（SPEC §2-2）。
ダウンロードのファイル名は英字にする（日本語を HTTP のヘッダに載せない）。
"""
from __future__ import annotations

import json
import os
import re
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
MODE_PORTS = {"own": PORT, "other": 8766, "sample": 8767}    # 起動のしかたごとの口の番号（★11/5。3つを同時に開ける）
STATIC = Path(__file__).resolve().parent / "static"
MODEL_FILES = {"model.glb": "model/gltf-binary", "model.3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
               "model_bambu.3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
               "foundation.stl": "model/stl", "floor1.stl": "model/stl", "exterior1.stl": "model/stl",
               "roof.stl": "model/stl", "roof_ext.stl": "model/stl", "checks.json": "application/json",
               # 2階建て（core/builder_multi.py）の部品
               "roof_lower.stl": "model/stl", "roof_lower_ext.stl": "model/stl", "floor2.stl": "model/stl", "exterior2.stl": "model/stl"}

app = FastAPI(title="HAKOYA")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class HideErrorText:
    """思いがけないエラーの文を、黒い画面にも画面にも出さない（★11/4 の見直し：YAML の書き間違いやフォルダの読めないエラーの文には、
    施主名の入った場所がそのまま入る）。出すのはエラーの種類と、HAKOYA のどのファイルの何行目かだけ。"""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await self.inner(scope, receive, send_wrapper)
        except Exception as e:  # noqa: BLE001  ここで止めて、uvicorn にエラーの文を出させない
            import traceback
            frames = traceback.extract_tb(e.__traceback__)[-4:]          # ファイルの名前と行だけ（中身・場所は出さない）
            where = " ← ".join(f"{Path(f.filename).name}:{f.lineno}" for f in reversed(frames)) or "?"
            print(f"[HAKOYA] {scope.get('path', '')} でエラー：{type(e).__name__}（{where}）", file=sys.stderr, flush=True)
            if started:
                return
            body = json.dumps({"detail": f"思いがけないエラーです（{type(e).__name__}）。黒い画面の [HAKOYA] の行を見せてください"},
                              ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 500,
                        "headers": [(b"content-type", b"application/json; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})


app.add_middleware(HideErrorText)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """書き換える操作（GET 以外）は、HAKOYA の画面から来たものだけ受け付ける。
    ほかのサイトを開いたブラウザから、勝手に検品（API の料金がかかる）を始められたり、キーを書き換えられたりしないため。"""
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != f"http://{request.headers.get('host', '')}":
        return JSONResponse({"detail": "HAKOYA の画面以外からの操作は受け付けません"}, status_code=403)
    return await call_next(request)


def _aliases() -> list[str]:
    """登録してある物件の仮名。data/local_paths.yaml がまだなければ空（★exe を初めて起動した PC で一覧がエラーになった）。"""
    from core.paths import aliases
    try:
        return aliases()
    except FileNotFoundError:
        return []


def _check_alias(alias: str) -> None:
    if alias not in _aliases():
        raise HTTPException(404, "その物件はありません")


def _visible_aliases() -> list[str]:
    """画面の一覧に出す物件。見本用で起動したときは、見本の家（仮名が「デモ用」か「見本」で始まる）だけ（★11/5 審査・動画用）。"""
    from core import settings_store
    names = _aliases()
    if settings_store.mode() == "sample":
        names = [a for a in names if settings_store.is_sample_alias(a)]
    return names


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


def _mtime_ns(p: Path) -> int | None:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def _iso(ns: int | None) -> str | None:
    """ファイルの更新時刻（ns）を、この PC の時刻の ISO の文字にする（例 2026-11-05T14:19:00+09:00）。"""
    if ns is None:
        return None
    import datetime as dt
    return dt.datetime.fromtimestamp(ns / 1e9).astimezone().isoformat(timespec="seconds")


def _read_json(p: Path):
    """読めなければ None（書きかけ・壊れたファイルで一覧を止めない）。"""
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _findings_stale(alias: str) -> bool:
    """検品のあとに「読み込んで組み立てる」で作り直したか（building.json が findings.json より新しい）。
    building.json を書くのは組み立てだけで、承認は書かない。"""
    b, f = _mtime_ns(_building_path(alias)), _mtime_ns(fd.findings_path(alias))
    return b is not None and f is not None and b > f


# 物件の進み具合に使うファイル（out/<物件>/ の中）：組み立て・人の修正・検品・書き出し
STATUS_FILES = {"built_at": "building.json", "edited_at": "overrides.json", "inspected_at": "findings.json",
                "exported_at": "model/checks.json"}
DECIDED = ("承認", "却下", "依頼済み")     # 依頼済み：直す案を Claude に頼んだ（続きは修正タブの「頼んだこと」。★11/5 夜）


def _status(alias: str) -> tuple[dict, int]:
    """物件の進み具合（★11/5）。ファイルの更新時刻と数だけで、形の計算はしない（一覧を軽く保つ）。
    返り値：(status, いちばん新しい時刻 ns。何もなければ 0)。"""
    d = out_dir(alias)
    ns = {k: _mtime_ns(d / rel) for k, rel in STATUS_FILES.items()}
    ovs = _read_json(d / "overrides.json") if ns["edited_at"] else None
    rep = _read_json(d / "findings.json") if ns["inspected_at"] else None
    chk = _read_json(d / "model" / "checks.json") if ns["exported_at"] else None
    items = [f for k in ("findings", "code_findings") for f in (rep.get(k) or [])] if isinstance(rep, dict) else []
    checks = (chk.get("checks") or []) if isinstance(chk, dict) else []
    status = {k: _iso(v) for k, v in ns.items()}
    status.update(fixes=len(ovs) if isinstance(ovs, list) else 0,
                  open_findings=sum(1 for f in items if isinstance(f, dict) and f.get("status") not in DECIDED),
                  high=sum(1 for c in checks if isinstance(c, dict) and c.get("level") == "高"),
                  stale=_findings_stale(alias))
    return status, max((v for v in ns.values() if v is not None), default=0)


@app.get("/api/properties")
def properties():
    """物件の一覧。見本用で起動したときは、見本の家（仮名が「デモ用」か「見本」で始まる）だけ（★11/5 審査・動画用）。
    行ごとに進み具合（status：組み立て・修正・検品・書き出しの時刻、修正の数、まだ決めていない指摘の数、検査の「高」の数、
    検品が古いか）と、小さな絵（thumb）があるか。いちばん新しく触った物件が上（触っていない物件は登録の順で下）。"""
    rows = []
    for a in _visible_aliases():
        f = property_files(a)
        status, newest = _status(a)
        rows.append((newest, {"alias": a, "has_glb": bool(f.glb and f.glb.exists()), "built": _building_path(a).exists(),
                              "status": status, "thumb": _thumb_path(a).is_file()}))
    rows.sort(key=lambda r: r[0], reverse=True)          # 並べ替えは安定なので、同じ時刻（触っていない物件）は登録の順のまま
    return _safe_json([r for _, r in rows])


# ---------------------------------------------------------------- 物件の小さな絵（★11/5：一覧に出す。画面の 3D から作って送る）
THUMB_MAX_BYTES = 800 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DATA_URL_PNG = "data:image/png;base64,"


def _thumb_path(alias: str) -> Path:
    return out_dir(alias) / "thumb.png"


class ThumbIn(BaseModel):
    png: str          # "data:image/png;base64,..."


@app.post("/api/thumb/{alias}")
def thumb_save(alias: str, body: ThumbIn):
    """物件の小さな絵（PNG）を out/<物件>/thumb.png に保存する（一時ファイルに書いてから入れ替える）。"""
    import base64
    import binascii
    from core.paths import write_bytes_atomic
    _check_alias(alias)
    text = body.png.strip()
    if not text.startswith(DATA_URL_PNG):
        raise HTTPException(400, "絵は PNG（data:image/png;base64,…）で送ってください")
    if len(text) > len(DATA_URL_PNG) + (THUMB_MAX_BYTES + 2) // 3 * 4 + 8:        # 読む前に大きさを確かめる
        raise HTTPException(413, f"絵が大きすぎます（{THUMB_MAX_BYTES // 1024}KB まで）。小さくして送ってください")
    try:
        data = base64.b64decode(text[len(DATA_URL_PNG):], validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "絵の中身が読めません（base64 の形が違います）") from None
    if not data.startswith(PNG_SIGNATURE):
        raise HTTPException(400, "絵が PNG ではありません")
    if len(data) > THUMB_MAX_BYTES:
        raise HTTPException(413, f"絵が大きすぎます（{THUMB_MAX_BYTES // 1024}KB まで）。小さくして送ってください")
    write_bytes_atomic(_thumb_path(alias), data)
    return _safe_json({"thumb": True, "bytes": len(data)})


@app.get("/api/thumb/{alias}")
def thumb_get(alias: str):
    """物件の小さな絵（image/png）。まだなければ 404。"""
    _check_alias(alias)
    try:
        data = _thumb_path(alias).read_bytes()
    except OSError:
        raise HTTPException(404, "この物件の絵はまだありません") from None
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- 物件のファイルが変わったか（★11/5：画面が数秒ごとに見て、変わったら読み直す）
STATE_FILES = ("building.json", "overrides.json", "findings.json", "print_split.json", "print_options.json", "model/checks.json")


@app.get("/api/state/{alias}")
def state(alias: str):
    """組み立て・修正・検品・分け目・書き出しのファイルの、いちばん新しい更新時刻。MCP の道具（別のプロセス）が
    書き換えたときも、画面が気づいて読み直せるように。version は ns の数（比べるだけに使う）、saved_at はその時刻（なければ null）。"""
    _check_alias(alias)
    d = out_dir(alias)
    newest = max((ns for ns in (_mtime_ns(d / rel) for rel in STATE_FILES) if ns is not None), default=None)
    return _safe_json({"saved_at": _iso(newest), "version": newest or 0})


# 模型を変える窓口（修正の記録 overrides.json・指摘の決定 findings.json の「読んで足して書く」と、組み直し write_outputs）は、
# この HAKOYA の中では1つずつ（★11/6 見直し：鍵が別々だと、表の直しと依頼の承認が同時に overrides.json を書いて片方の修正が消え、
# 組み直しが2つ重なると STL を消し合って失敗し（足したばかりの直しを取り消す）、古い修正で組んだ model.glb が承認の直後に
# 上書きされて、本当の直しを「形が変わらない」と取り消した）。MCP の道具（別のプロセス）とは、この鍵では分けられない。
MODEL_LOCK = threading.RLock()


@app.post("/api/build/{alias}")
def build_property(alias: str):
    """glb を読み直して building.json を作り（番号は前回から引き継ぐ）、模型を組み立てて書き出す。"""
    _check_alias(alias)
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから組み立ててください")
    from core import settings_store
    try:
        with MODEL_LOCK:
            write_building(alias, build(alias, settings_store.load()["company"]))   # 設定の会社ルールで作る（★11/4 B工務店）
            summary = write_outputs(alias)
    except (ValueError, OSError) as e:      # HAKOYA が書いた説明（「3階建てはまだ扱えません」など）は画面に出す
        msg = _user_message(e)
        if msg is None:
            raise                            # 場所の文字などが入るかもしれない文は出さない（HideErrorText が種類だけ出す）
        raise HTTPException(422, f"組み立てられませんでした：{msg}")
    return _safe_json(summary)


def _user_message(e: Exception) -> str | None:
    """画面に見せてよいエラーの文（場所の文字・出してはいけない言葉がない、短い文）。なければ None。"""
    import re as _re
    from core.privacy import PrivacyError
    msg = str(e)
    if not msg or len(msg) > 300 or _re.search(r"[\\/]|\.(glb|jww|pdf)\b", msg, _re.I):
        return None
    try:
        assert_clean(msg, "エラーの文")
    except PrivacyError:
        return None
    return msg


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
    from core.rules import default_company, load_rules
    b, _ = _effective(alias)
    return _safe_json({"rows": check_property(b, load_rules(b["source"].get("company_rules", default_company())))})


DRAWING_CACHE: dict[str, tuple] = {}     # 物件ごとの（building・修正の更新時刻, 照合の結果）。位置合わせに数秒かかるため


@app.get("/api/drawing/{alias}")
def drawing_check(alias: str):
    """図面（jww）と比べた結果：平面図の窓・テラスドアの注記（品番と上端の FL+）と模型の開口の上端。数値はコードが読む。"""
    _check_alias(alias)
    from core.drawing_check import summary
    key = tuple(p.stat().st_mtime if p.exists() else 0
                for p in (_building_path(alias), ov.overrides_path(alias), out_dir(alias) / "reading.json"))
    try:   # 図面を書き出し直したら（PDF・jww の時刻が変われば）作り直す
        key += tuple(sorted((k, p.stat().st_mtime if p.exists() else 0) for k, p in property_files(alias).jww.items()))
    except (KeyError, OSError, ValueError):
        pass
    hit = DRAWING_CACHE.get(alias)
    if hit and hit[0] == key:
        return _safe_json(hit[1])
    b, _ = _effective(alias)
    try:
        result = summary(alias, b)
    except Exception as e:                                    # 図面が読めない（壊れている・ライブラリがない）
        from core.pdf_drawing import DrawingUnreadable
        why = _user_message(e) if isinstance(e, DrawingUnreadable) else None    # スキャンの PDF など：直し方を出す
        result = {"available": False, "note": f"図面を読めませんでした（{why or type(e).__name__}）"}
    DRAWING_CACHE[alias] = (key, result)
    return _safe_json(result)


@app.get("/api/plan/{alias}")
def plan(alias: str, level: str | None = None, drawing: bool = False):
    """平面図。2階建ては level（1F・2F）で階を選ぶ（省くと1階）。drawing=1 で図面（jww）の線を重ねる。"""
    _check_alias(alias)
    b, _ = _effective(alias)
    if level is not None and level not in [lv["name"] for lv in b.get("levels", [])]:
        raise HTTPException(400, "その階はありません")
    overlay, note = None, None
    if drawing:
        from core.drawing_check import overlay_lines
        overlay, note = overlay_lines(alias, b, level)
    svg = render_svg(b, alias, level, overlay=overlay, overlay_note=note, split=_house_cut(alias))
    assert_clean(svg, "平面図")
    return Response(svg, media_type="image/svg+xml; charset=utf-8")


def _house_cut(alias: str) -> dict | None:
    """書き出したときの本体の分け目（checks.json）。画面の線とファイルの分け方は、必ず同じものにする。"""
    p = out_dir(alias) / "model" / "checks.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("house_cut")
    except (OSError, ValueError):
        return None


class SplitIn(BaseModel):
    cuts_mm: list[float]


@app.post("/api/split/{alias}")
def split_set(alias: str, body: SplitIn):
    """本体の分け目を、平面図で人が決めた位置にする（★11/5 作者 2-1「分割ラインを変えられる方がいい」）。
    近くの壁の面に寄せ、どれも造形範囲に入るか確かめてから保存し、書き出し直す。入らなければ 400（理由つき）。"""
    import numpy as np

    from core import body_split as bs
    from core.export import make_model
    _check_alias(alias)
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから分け目を動かしてください")
    with MODEL_LOCK:
        hc = _house_cut(alias)
        if not hc or not hc.get("cuts"):
            raise HTTPException(409, "この家は分けずに入るので、分け目はありません（先に「読み込んで組み立てる」を押してください）")
        if len(body.cuts_mm) != len(hc["cuts"]) or not all(np.isfinite(body.cuts_mm)):
            raise HTTPException(400, "分け目の数が違います")
        b, parts, _, rules = make_model(alias)
        u = np.array(hc["u"], float)
        cuts = sorted(bs.snap_to_face(b, u, float(t), float(rules["model"]["scale"])) for t in body.cuts_mm)
        trial = bs.choose(b, parts, rules, {"u": list(u), "cuts": cuts, "source": "human"})
        if trial is None or trial.source != "human":
            raise HTTPException(400, "その分け目では、分けた部品のどれかが造形範囲に入りません。もう少し真ん中寄りにしてください")
        before = bs.load_saved(alias)
        bs.save_human(alias, u, cuts)
        try:
            summary = write_outputs(alias)
        except Exception:
            if before:
                bs.save_human(alias, np.array(before["u"], float), before["cuts"])
            else:
                bs.clear_saved(alias)
            raise
    return _safe_json({"house_cut": summary.get("house_cut"), "checks": summary.get("checks", [])})


@app.delete("/api/split/{alias}")
def split_reset(alias: str):
    """本体の分け目を自動に戻して、書き出し直す。"""
    from core import body_split as bs
    _check_alias(alias)
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから戻してください")
    with MODEL_LOCK:
        bs.clear_saved(alias)
        summary = write_outputs(alias)
    return _safe_json({"house_cut": summary.get("house_cut"), "checks": summary.get("checks", [])})


# ---------------------------------------------------------------- 外壁の仕上げ（ざらざらにするか。★11/6 作者「適用するかしないかを決めれるように」）

def _print_rules(alias: str) -> dict:
    """この物件の会社ルール（組み立てた会社。まだ組み立てていなければ設定の会社）。書き出し（make_model）と同じ決め方。"""
    from core import settings_store
    from core.rules import default_company, load_rules
    p = _building_path(alias)
    if p.exists():
        try:
            return load_rules(json.loads(p.read_text(encoding="utf-8"))["source"].get("company_rules", default_company()))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return load_rules(settings_store.load()["company"])


def _fuzzy_state(alias: str) -> dict:
    from core import print_options as po
    rules = _print_rules(alias)
    on, source = po.effective_fuzzy(alias, rules)
    return {"on": on, "source": source, "company_default": po.company_default(rules)}


@app.get("/api/print_options/{alias}")
def print_options_get(alias: str):
    """外壁をざらざらにするか（on）・物件で決めたか会社ルールの既定か（source）・会社ルールの既定（company_default）。"""
    _check_alias(alias)
    return _safe_json({"fuzzy_skin": _fuzzy_state(alias)})


class PrintOptionsIn(BaseModel):
    fuzzy_skin: bool


def _set_print_options(alias: str, change) -> dict:
    """物件の選び方を変えて書き出し直す（分け目と同じ守り）。書き出せなければ、前の選び方に戻す。"""
    from core import print_options as po
    _check_alias(alias)
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから変えてください")
    if not _building_path(alias).exists():
        raise HTTPException(409, "まだ組み立てていません。「読み込んで組み立てる」を押してください")
    with MODEL_LOCK:
        before = po.saved_fuzzy(alias)
        change()
        try:
            summary = write_outputs(alias)
        except Exception:
            if before is None:
                po.clear(alias)
            else:
                po.set_fuzzy(alias, before)
            raise
    return {"fuzzy_skin": _fuzzy_state(alias), "checks": summary.get("checks", [])}


@app.post("/api/print_options/{alias}")
def print_options_set(alias: str, body: PrintOptionsIn):
    """外壁をざらざら（ファジースキン）にするかを、この物件で決めて、書き出し直す。形は変わらない（3MF の設定だけ）。"""
    from core import print_options as po
    return _safe_json(_set_print_options(alias, lambda: po.set_fuzzy(alias, body.fuzzy_skin)))


@app.delete("/api/print_options/{alias}")
def print_options_reset(alias: str):
    """物件で決めた値をやめて、会社ルールの既定に戻し、書き出し直す。"""
    from core import print_options as po
    return _safe_json(_set_print_options(alias, lambda: po.clear(alias)))


def _exported_fuzzy(alias: str) -> bool:
    """書き出した 3MF に、ざらざらの設定を入れたか（checks.json。前の版の書き出しには印がないので「入れていない」）。"""
    p = out_dir(alias) / "model" / "checks.json"
    try:
        return bool((json.loads(p.read_text(encoding="utf-8")).get("fuzzy_skin") or {}).get("on"))
    except (OSError, ValueError, AttributeError):
        return False


PIECE_STL = re.compile(r"^(roof|roof_ext|roof_lower|roof_lower_ext|foundation|floor1|exterior1|floor2|exterior2)_p\d{1,2}\.stl$")   # 分けた STL（屋根の面・本体の分け目）


@app.get("/api/model/{alias}/{name}")
def model_file(alias: str, name: str):
    _check_alias(alias)
    if name not in MODEL_FILES and not PIECE_STL.match(name):
        raise HTTPException(404, "そのファイルはありません")
    p = out_dir(alias) / "model" / name
    if not p.exists():
        raise HTTPException(409, "まだ書き出していません")
    # 保存する名前：ざらざらの設定を入れた 3MF は、名前でも分かるように _fuzzy を付ける（英字だけ。★11/6）
    fname = f"hakoya_{name[:-4]}_fuzzy.3mf" if name.endswith(".3mf") and _exported_fuzzy(alias) else f"hakoya_{name}"
    return FileResponse(p, media_type=MODEL_FILES.get(name, "model/stl"), filename=fname)


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
    with MODEL_LOCK:
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
    with MODEL_LOCK:
        try:
            item = ov.undo(alias, override_id)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        fd.revert_override(alias, override_id)      # この修正を生んだ指摘は「未確認」に戻す
        summary = write_outputs(alias)
    return _safe_json({"undone": item, "checks": summary["checks"]})


# ---------------------------------------------------------------- 検品（SPEC §9-2・§10）

def _check_edition(connection: str) -> None:
    """社外用の版では、Claude のプランの接続（claude_code）を断る（SPEC §7-4。画面で隠すだけにしない）。"""
    from core import settings_store
    try:
        settings_store.check_connection(connection)
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    _check_edition(connection)
    if connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください")
    with RUNS_LOCK:
        if RUNS.get(alias, {}).get("running"):
            raise HTTPException(409, "この物件はいま検品中です")
        if connection != "off":
            log = out_dir(alias) / "inspect_log.jsonl"
            if log.exists():
                log.unlink()                                     # 前回の記録を消して、進み具合を最初から数える
        RUNS[alias] = {"running": True, "connection": connection, "started_at": time.time(), "error": None,
                       "models": _inspect_models(connection)}
    threading.Thread(target=_run_inspection_thread, args=(alias, connection), daemon=True).start()
    return _safe_json({"started": True, "connection": connection})


def _inspect_models(connection: str) -> dict | None:
    """検品に使うモデル（面ごとの担当・まとめ役）。画面の「Claude（Opus 5.5）が…」の札に出す（★11/5 夜）。AI なしは None。"""
    if connection not in ("claude_code", "api_key"):
        return None
    from core.ai.agent import load_ai_models
    try:
        m = load_ai_models()[connection]
        return {"face": m["inspect_face"]["model"], "merge": m["inspect_merge"]["model"]}
    except (OSError, KeyError, TypeError):           # 設定が読めなくても検品は始める（札にモデル名を出さないだけ）
        return None


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
    from core import answers
    _check_alias(alias)
    rep = fd.load(alias) or {"findings": [], "code_findings": [], "summary": "まだ検品していません"}
    for f in [*(rep.get("findings") or []), *(rep.get("code_findings") or [])]:
        if isinstance(f, dict):
            f["title"] = fd.title_of(f)          # ★11/5 より前の検品には見出しがないので、message から作る（ファイルは書き換えない）
    # 質問に人の答えを付ける（この検品の答えは answer、前の検品で同じ質問にした答えは previous_answer。★11/5 夜）
    answers.attach(alias, rep)
    # 検品のあとに「読み込んで組み立てる」で作り直したか（見本用の手順の帯を「検品する」に戻す）
    rep["stale"] = _findings_stale(alias)
    return _safe_json(rep)


class DecisionIn(BaseModel):
    action: str


@app.post("/api/findings/{alias}/{fid}")
def decide_finding(alias: str, fid: str, body: DecisionIn):
    """指摘を承認／却下／あとで／依頼済み（request：直す案を Claude に頼んだ）／未確認に戻す（reset）にする。
    承認すると修正として記録し、模型を組み直す。もう承認してある指摘は、何もしないで今の指摘を返す（already: true。組み直さない）。
    指摘を決めるのは1つずつ（同じ指摘の承認が同時に2つ届いても、修正を2件にしない。組み直しも MODEL_LOCK の中）。"""
    _check_alias(alias)
    with MODEL_LOCK:
        before = {x.get("id") for x in ov.load(alias)}
        try:
            f = fd.decide(alias, fid, body.action)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        added = f.get("override") if body.action == "approve" and f.get("override") not in before else None
        if added:
            _rebuild_or_undo(alias, added)
    return _safe_json({"finding": f, "already": body.action == "approve" and not added})


class AnswerIn(BaseModel):
    answer: str
    question: str | None = None      # 画面に出ていた質問の文（検品し直して番号の中身が変わっていたら断るため）


@app.post("/api/findings/{alias}/questions/{qid}/answer")
def answer_question(alias: str, qid: str, body: AnswerIn):
    """検品の Claude の質問に、人が答える（★11/5 夜）。物件ごとに out/<物件>/answers.json に残し、次の検品で Claude に伝える
    （同じ質問を二度しないため）。書く前に、出してはいけない言葉がないか確かめる。"""
    from core import answers
    from core.privacy import PrivacyError
    _check_alias(alias)
    rep = fd.load(alias)
    if rep is None:
        raise HTTPException(409, "まだ検品していません")
    q = answers.find_question(rep, qid)
    if q is None:
        raise HTTPException(404, f"{qid} という質問はありません（検品し直したときは、画面を読み直してください）")
    if body.question is not None and not answers.same_question(body.question, q.get("question")):
        raise HTTPException(409, f"{qid} の質問が変わっています（検品し直したため）。画面を読み直してから答えてください")
    try:
        rec = answers.add(alias, qid, q.get("question"), q.get("options"), body.answer, rep.get("at"))
    except (PrivacyError, ValueError) as e:
        raise HTTPException(400, str(e))
    return _safe_json({"answer": rec})


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
    keep: list[str] | None = None        # 画面で札の × を押さずに残した部材（囲みから選ばれた部材の中だけが効く）


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
    from core.rules import default_company, load_rules
    b, _ = _effective(alias)
    return _safe_json(proxies(b, load_rules(b["source"].get("company_rules", default_company()))))


@app.post("/api/lasso/{alias}/select")
def lasso_select(alias: str, body: LassoIn):
    """囲みの中の部材（コードが決める）。part は、その部材が入る模型の部品の短い説明。"""
    _check_alias(alias)
    from core import lasso as geo
    from core.ai.lasso import read_checks
    b, _ = _effective(alias)
    try:
        sel = _lasso_selection(b, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _safe_json({"selection": geo.describe(b, sel, geo.roof_ids_from_checks(read_checks(alias)))})


@app.post("/api/lasso/{alias}/ask")
def lasso_ask(alias: str, body: LassoIn):
    """囲みと一言を Claude に渡し、直す案か質問を受け取る（保存はしない）。30秒〜数分かかる。"""
    import asyncio
    _check_alias(alias)
    _effective(alias)                                   # 組み立て前なら 409
    if body.connection not in ("claude_code", "api_key", "off"):
        raise HTTPException(400, "connection は claude_code・api_key・off のどれかです")
    _check_edition(body.connection)
    if body.connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください")
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから頼んでください")
    from core.ai.lasso import run_lasso
    try:
        res = asyncio.run(run_lasso(alias, body.connection, body.polygon, body.text, level=body.level,
                                    hits=body.hits, mask_px=body.mask_px, view=body.view, keep=body.keep))
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
    with MODEL_LOCK:
        try:
            item = approve(alias, body.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))
        summary = _rebuild_or_undo(alias, item["id"])
    return _safe_json({"override": item, "checks": summary["checks"]})


# ---------------------------------------------------------------- 修正の依頼（★11/5 夜 作者の試しから。core/fix_requests.py）
# 囲んで直す・文字で直すを裏で動かし、頼んだことと答えを物件ごとに残す。画面はタブを替えても・物件を開き直しても、
# 一覧（GET）を読み直せば答えが見える。承認したら模型の形を前後で比べ、変わらない直しは取り消す（core/shape_diff.py）。
# 今までの /api/lasso/*（テストや録画が使う）は、そのまま残す。

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
# 承認・却下は1つずつ（二度押しで同じ案を2回直さない）：MODEL_LOCK を使う（表の直し・指摘の承認・分け目と同じ鍵。★11/6 見直し）
NO_CHANGE_NOTE = "この直しでは模型が変わりませんでした（取り消しました）"
FIX_STATE_JA = {"approved": "もう承認しました（やめるときは「取り消す」）", "rejected": "却下しました",
                "no_change": "模型が変わらなかったので取り消しました"}


class FixGroupIn(BaseModel):
    label: str = ""
    ids: list[str] = []
    partial: list[str] = []          # 囲みに一部だけかかった部材
    source: str = "plan"             # plan（平面図で囲んだ）・3d（3D で囲んだ）
    view: str | None = None          # 3D をどちらから見ていたか（S/N/E/W/plan）


class FixRequestIn(BaseModel):
    text: str = ""
    connection: str = "claude_code"
    model: str = "opus"              # opus（Opus 5.5・既定）・sonnet（Sonnet 5.5）
    kind: str = "lasso"              # lasso（囲んで直す）・text（文字で直す）
    ids: list[str] | None = None     # 囲んだ部材（lasso）・いま選んでいる部材（text）
    groups: list[FixGroupIn] | None = None   # 囲みごとの部材（①②…）
    polygon: list[list[float]] | None = None  # 1つの囲みを /api/lasso と同じ形で送るとき（サーバーが部材を決める）
    level: str | None = None
    hits: list[dict] | None = None
    mask_px: int = 0
    view: str | None = None
    keep: list[str] | None = None
    finding_id: str | None = None    # 検品の指摘から頼んだとき
    parent_id: str | None = None


def _fix_ready(alias: str, connection: str) -> dict:
    """依頼を受ける前の確かめ（組み立て済み・接続・検品中でない）。修正を当てた家を返す。"""
    _check_alias(alias)
    b, _ = _effective(alias)                                   # 組み立て前なら 409
    if connection not in ("claude_code", "api_key"):
        raise HTTPException(400, "connection は claude_code（自分のプラン）か api_key です（AI なしは「表で直す」）")
    _check_edition(connection)
    if connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください")
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから頼んでください")
    return b


def _fix_selection(alias: str, b: dict, body: FixRequestIn) -> tuple[list[dict], list[dict]]:
    """依頼の部材の一覧（説明つき）と、囲みごとの番号 [{label, ids, partial, source, view}]。"""
    from core import lasso as geo
    from core.ai.lasso import read_checks
    roof_ids = geo.roof_ids_from_checks(read_checks(alias))
    if body.kind == "text":                                     # いま選んでいる部材（なくてもよい。知らない番号は外す）
        return geo.describe(b, geo.select_ids(b, body.ids or [], strict=False), roof_ids), []
    if body.hits is not None or body.polygon:                   # 1つの囲み（/api/lasso と同じ。部材はサーバーが決める）
        lasso_in = LassoIn(polygon=body.polygon, hits=body.hits, mask_px=body.mask_px, view=body.view, level=body.level)
        rows = geo.describe(b, geo.keep_only(_lasso_selection(b, lasso_in), body.keep), roof_ids)
        src = "3d" if body.hits is not None else "plan"
        return rows, [{"label": CIRCLED[0], "ids": [r["id"] for r in rows], "partial": [r["id"] for r in rows if r["partial"]],
                       "source": src, "view": body.view if src == "3d" else None}] if rows else []
    groups_in = body.groups or ([FixGroupIn(label=CIRCLED[0], ids=body.ids)] if body.ids else [])
    ids = list(dict.fromkeys([*(body.ids or []), *(i for g in groups_in for i in g.ids)]))
    rows = geo.describe(b, geo.select_ids(b, ids, [i for g in groups_in for i in g.partial]), roof_ids)   # 知らない番号は 400
    known = [r["id"] for r in rows]
    groups, used = [], set()
    for k, g in enumerate(groups_in):
        gid = [i for i in dict.fromkeys(g.ids) if i in known]
        if gid:
            groups.append({"label": (g.label.strip() or CIRCLED[k % len(CIRCLED)])[:4], "ids": gid,
                           "partial": [i for i in g.partial if i in gid], "source": "3d" if g.source == "3d" else "plan",
                           "view": g.view if g.source == "3d" and g.view in ("S", "N", "E", "W", "plan") else None})
            used.update(gid)
    if groups and len(used) < len(known):                       # どの囲みにも入っていない番号（送り方の誤り）は「ほか」に
        groups.append({"label": "ほか", "ids": [i for i in known if i not in used], "partial": [], "source": "plan", "view": None})
    return rows, groups


def _start_fix(alias: str, req: dict) -> None:
    """依頼を裏の thread で Claude に渡す（終わったら fix_requests が記録する）。"""
    import asyncio
    from core import fix_requests as fr
    from core.ai.lasso import run_fix
    ids = [r["id"] for r in req["selection"]]
    partial = [r["id"] for r in req["selection"] if r.get("partial")]

    def job():
        return asyncio.run(run_fix(alias, req["connection"], req["text"], kind=req["kind"], model=req["model"],
                                   ids=ids, partial=partial, groups=req["groups"], human_words=req.get("human_words"),
                                   request_id=req["id"]))
    fr.start(alias, req["id"], job)


@app.post("/api/fix/{alias}/requests")
def fix_create(alias: str, body: FixRequestIn):
    """修正の依頼を作り、裏で Claude に渡す（すぐ返る。答えは GET で読む）。返すのは依頼（status=running）。"""
    from core import fix_requests as fr
    from core.ai.agent import load_ai_models
    from core.ai.lasso import model_spec
    from core.privacy import PrivacyError
    b = _fix_ready(alias, body.connection)
    if body.kind not in ("lasso", "text"):
        raise HTTPException(400, "kind は lasso（囲んで直す）か text（文字で直す）です")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "一言を書いてください（例：この壁厚だけおかしい。ほかとそろえて）")
    try:   # 記録に残る文は全部、書く前に確かめる（★11/6 見直し：囲みの名前・parent_id は確かめておらず、記録を書くときに 500 になった）
        assert_clean("\n".join([text, body.finding_id or "", body.parent_id or "", *(g.label for g in body.groups or [])]), "一言")
    except PrivacyError as e:
        raise HTTPException(400, str(e))
    try:
        m = model_spec(load_ai_models(), body.connection, body.model)
        rows, groups = _fix_selection(alias, b, body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.kind == "lasso" and not rows:
        raise HTTPException(400, "囲みの中に部材がありません。壁や窓、軒をぐるっと囲んでください")
    try:
        req = fr.create(alias, text=text, kind=body.kind, model=body.model, model_label=m["label"], connection=body.connection,
                        selection=rows, groups=groups, finding_id=body.finding_id, parent_id=body.parent_id)
    except fr.BusyError as e:
        raise HTTPException(409, str(e))
    _start_fix(alias, req)
    return _safe_json(req)


@app.get("/api/fix/{alias}/requests")
def fix_list(alias: str):
    """修正の依頼の一覧（新しい順）。running は、この物件でいま Claude が考えている数。"""
    from core import fix_requests as fr
    _check_alias(alias)
    return _safe_json({"requests": fr.load(alias), "running": fr.running_count(alias), "max_running": fr.MAX_RUNNING})


class FixIndexIn(BaseModel):
    index: int


def _fix_proposal(alias: str, rid: str, index: int) -> tuple[dict, dict]:
    from core import fix_requests as fr
    try:
        req = fr.get(alias, rid)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    props = req.get("proposals") or []
    if not 0 <= index < len(props):
        raise HTTPException(404, f"{rid} に {index + 1} 番目の直す案はありません")
    return req, props[index]


@app.post("/api/fix/{alias}/requests/{rid}/approve")
def fix_approve(alias: str, rid: str, body: FixIndexIn):
    """直す案を承認して修正として記録し、模型を組み直す。数値はサーバーが出どころから計算し直す。
    組み直す前と後で模型の形を比べ、変わらなければ取り消して state=no_change（changed=false）。同じ案は二度承認できない。"""
    from core import fix_requests as fr
    from core import shape_diff as sd
    from core.ai.lasso import approve
    _check_alias(alias)
    _effective(alias)
    if RUNS.get(alias, {}).get("running"):
        raise HTTPException(409, "この物件はいま検品中です。検品が終わってから承認してください")
    with MODEL_LOCK:
        req, p = _fix_proposal(alias, rid, body.index)
        if p.get("state") in FIX_STATE_JA:
            raise HTTPException(409, f"この案は{FIX_STATE_JA[p['state']]}")
        before = sd.signature_of(alias)
        try:
            item = approve(alias, p, origin="text" if req.get("kind") == "text" else "lasso")
        except ValueError as e:
            fr.set_proposal(alias, rid, body.index, state="failed", note=str(e))
            raise HTTPException(400, str(e))
        try:
            summary = _rebuild_or_undo(alias, item["id"])
        except HTTPException as e:
            fr.set_proposal(alias, rid, body.index, state="failed", note=str(e.detail))
            raise
        parts = sd.changed_parts(before, sd.signature_of(alias))
        values = {"from": item.get("from"), "to": item.get("to")}         # 承認のときに計算し直した値
        if not parts:                                                     # 模型が変わらない直し：記録から外して組み直す
            ov.undo(alias, item["id"])
            fd.revert_override(alias, item["id"])
            summary = write_outputs(alias)
            req = fr.set_proposal(alias, rid, body.index, state="no_change", override_id=None, note=NO_CHANGE_NOTE, **values)
            return _safe_json({"request": req, "override": item, "changed": False, "changed_parts": [],
                               "checks": summary["checks"]})
        # override_at：修正の番号は取り消すと使い回されるので、承認したときの修正の時刻も控える（fix_requests._still_recorded）
        req = fr.set_proposal(alias, rid, body.index, state="approved", override_id=item["id"], override_at=item.get("at"),
                              note="", **values)
        return _safe_json({"request": req, "override": item, "changed": True, "changed_parts": parts,
                           "checks": summary["checks"]})


@app.post("/api/fix/{alias}/requests/{rid}/reject")
def fix_reject(alias: str, rid: str, body: FixIndexIn):
    """直す案を却下する（模型は変えない）。承認した案は「取り消す」でやめる。"""
    from core import fix_requests as fr
    _check_alias(alias)
    with MODEL_LOCK:
        _, p = _fix_proposal(alias, rid, body.index)
        if p.get("state") == "approved":
            raise HTTPException(409, "承認した案です。やめるときは「取り消す」を押してください")
        req = fr.set_proposal(alias, rid, body.index, state="rejected", note="")
    return _safe_json({"request": req})


class FixAnswerIn(BaseModel):
    question_id: str
    answer: str


@app.post("/api/fix/{alias}/requests/{rid}/answer")
def fix_answer(alias: str, rid: str, body: FixAnswerIn):
    """Claude の質問に答える：元の一言に「質問：…／選択肢：1. … 2. …／人の答え：…」を付けた新しい依頼（parent_id 付き）を作り、
    裏で Claude に渡す。返すのは新しい依頼。同じ質問には一度だけ答えられる。"""
    from core import fix_requests as fr
    from core import lasso as geo
    from core.ai.agent import load_ai_models
    from core.ai.lasso import model_spec, read_checks
    from core.privacy import PrivacyError
    _check_alias(alias)
    answer = (body.answer or "").strip()
    if not answer:
        raise HTTPException(400, "答えを書くか、選択肢を押してください")
    try:
        assert_clean(answer, "人の答え")
    except PrivacyError as e:
        raise HTTPException(400, str(e))
    with fr.LOCK:                                             # 二度押しで、同じ答えの依頼を2つ作らない
        try:
            parent = fr.get(alias, rid)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        q = next((x for x in parent.get("questions") or [] if x.get("id") == body.question_id), None)
        if q is None:
            raise HTTPException(404, f"{rid} に {body.question_id} という質問はありません")
        if q.get("answered"):
            raise HTTPException(409, "この質問にはもう答えました（答えを添えた依頼が一覧にあります）")
        b = _fix_ready(alias, parent["connection"])
        text, words = fr.answered_text(parent, q, answer)
        ids = [r["id"] for r in parent.get("selection") or []]
        partial = [r["id"] for r in parent.get("selection") or [] if r.get("partial")]
        rows = geo.describe(b, geo.select_ids(b, ids, partial, strict=False), geo.roof_ids_from_checks(read_checks(alias)))
        try:
            m = model_spec(load_ai_models(), parent["connection"], parent.get("model"))
            req = fr.create(alias, text=text, kind=parent.get("kind", "lasso"), model=parent.get("model") or "opus",
                            model_label=m["label"], connection=parent["connection"], selection=rows,
                            groups=parent.get("groups") or [], finding_id=parent.get("finding_id"), parent_id=rid,
                            human_words=words)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except fr.BusyError as e:
            raise HTTPException(409, str(e))
        fr.mark_answered(alias, rid, q["id"], answer, req["id"])
    _start_fix(alias, req)
    return _safe_json(req)


@app.delete("/api/fix/{alias}/requests/{rid}")
def fix_remove(alias: str, rid: str):
    """依頼を一覧から外す（記録 lasso_log.jsonl には残る。承認した修正はそのまま）。考えている途中の依頼は外さない。"""
    from core import fix_requests as fr
    _check_alias(alias)
    try:
        fr.remove(alias, rid)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    except fr.BusyError as e:
        raise HTTPException(409, str(e))
    return _safe_json({"removed": rid})


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
    _check_edition(body.connection)
    if body.connection == "api_key" and not secret_store.has_api_key():
        raise HTTPException(409, "API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください")
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

# ---------------------------------------------------------------- 物件の登録（core/registry.py）
PICKS: dict[str, str] = {}   # 画面の「選ぶ」で選んだ場所。サーバーの中だけで持ち、画面には番号だけ渡す（場所に施主名が入ることがある）


class PickIn(BaseModel):
    what: str


class RegisterIn(BaseModel):
    alias: str
    glb_token: str | None = None
    glb_path: str | None = None
    drawings_token: str | None = None
    drawings_path: str | None = None
    ng_words: list[str] = []


@app.get("/api/registry")
def registry_list():
    """登録してある物件（仮名・glb があるか・図面の種類・出してはいけない言葉の数）と、次の仮名。場所は返さない。"""
    from core import registry as rg
    return _safe_json(rg.list_properties())


def _check_import_allowed() -> None:
    """見本用では物件を登録しない（data/sample/ は見本の家だけ。画面で隠すだけにしない）。"""
    from core import settings_store
    if not settings_store.features()["import"]:
        raise HTTPException(400, "見本用では物件を登録できません。登録は自社用（start.bat）か他社用（start_other.bat）で起動してください")


@app.post("/api/registry/pick")
def registry_pick(body: PickIn):
    """Windows の「選ぶ」窓を出す（glb＝ファイル、folder＝図面のフォルダ）。返すのは番号と、見つかったものの種類だけ。"""
    import secrets as _secrets
    from core import registry as rg
    _check_import_allowed()
    if body.what not in rg.PICK_DIALOGS:
        raise HTTPException(400, "選ぶものが分かりません")
    try:
        chosen = rg.pick(body.what)
        if chosen:
            ok, text = (rg.describe_glb if body.what == "glb" else rg.describe_drawings)(Path(chosen))
    except Exception as e:  # noqa: BLE001  PowerShell が使えない PC・フォルダが読めないなど（文には場所が入るので種類だけ）
        raise HTTPException(500, f"選ぶ窓を出せませんでした（{type(e).__name__}）。場所を貼り付けてください")
    if not chosen:
        return _safe_json({"cancelled": True})
    token = "".join(_secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(16))   # 数字の言葉に当たらないよう英字だけ
    PICKS[token] = chosen
    return _safe_json({"token": token, "ok": ok, "text": text})


def _chosen(token: str | None, pasted: str | None) -> str | None:
    """登録に使う場所：貼り付けた場所があればそれ（★11/4：前は前に選んだものが優先された）、なければ「選ぶ」で選んだもの。"""
    if pasted and pasted.strip():
        return pasted
    if token:
        if token not in PICKS:
            raise HTTPException(400, "選んだ場所の記録が消えています（アプリを起動し直したため）。もう一度「選ぶ」を押してください")
        return PICKS[token]
    return None


@app.post("/api/registry")
def registry_add(body: RegisterIn):
    """物件を登録する（local_paths.yaml の最後に書き足す。元のファイルは読むだけ）。"""
    from core import registry as rg
    _check_import_allowed()
    glb = _chosen(body.glb_token, body.glb_path) or ""
    drawings = _chosen(body.drawings_token, body.drawings_path)
    if not glb.strip():
        raise HTTPException(400, "glb を選んでください")
    try:
        alias = rg.register(body.alias, glb, drawings, body.ng_words)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _safe_json({"alias": alias, **rg.list_properties()})


class ReadingIn(BaseModel):
    high: bool


@app.post("/api/reading/{alias}")
def reading_set(alias: str, body: ReadingIn):
    """高性能読み込み（図面と突き合わせる）の入れ切り（★11/4 作者：ふだんは glb だけ、高性能読み込みのときだけ図面）。
    切にすると、図面の照合・重ね・図面ありの検品は「図面なし」と同じになる。模型（glb から組み立てた形）は変わらない。"""
    _check_alias(alias)
    from core import drawing_check as dc
    from core import reading
    from core import registry as rg
    if body.high and not property_files(alias, use_mode=False).jww:
        raise HTTPException(400, "この物件には図面が登録されていません（図面のフォルダを登録すると入にできます）")
    reading.set_high(alias, body.high)
    DRAWING_CACHE.pop(alias, None)                            # 照合・重ね・図面の画像を、次に開いたときに作り直す
    for k in [k for k in dc._CACHE if k[0] == alias]:
        dc._CACHE.pop(k, None)
    return _safe_json(rg.list_properties())


@app.get("/api/settings")
def settings():
    """設定の状態。API キーは「入っているかどうか」だけを返す（キーそのものは返さない）。
    組み立てたあとの検品（auto_inspect）と、選べるつなぎ方・API の1回の上限も返す。"""
    from core import settings_store
    from core.ai.agent import load_ai_models
    from core.rules import default_company
    try:
        key = secret_store.has_api_key()
    except secret_store.SecretStoreError as e:
        raise HTTPException(500, str(e))
    st = settings_store.load()
    mode = settings_store.mode()
    try:
        visible = _visible_aliases()
    except ValueError:              # local_paths.yaml の書き間違い（一覧のほうで日本語の説明が出る）。設定の画面は開けるようにする
        visible = []
    return _safe_json({"api_key_set": key, "auto_inspect": st["auto_inspect"], "api_cost_ack": st["api_cost_ack"],
                       "connections": settings_store.available_connections(),
                       "max_budget_usd": float(load_ai_models()["limits"]["max_budget_usd"]),
                       "company": st["company"], "companies": settings_store.companies(),
                       "default_company": default_company(),        # 会社ルールの書いていない模型に使う会社（自社の会社ルール。なければ B工務店（架空））
                       "company_chosen": st["company_chosen"],
                       # 起動のしかた（★11/5：own・other・sample。起動したときに決まり、画面では切り替えない）。view・views は前の画面のため
                       "mode": mode, "mode_label": settings_store.mode_label(mode), "view": mode, "views": [mode],
                       "features": settings_store.features(),
                       # 最後に開いた物件・最近開いた物件（いまの一覧にないものは出さない）
                       "last_alias": st["last_alias"] if st["last_alias"] in visible else None,
                       "recent": [a for a in st["recent"] if a in visible],
                       "edition": settings_store.edition()})


class LastAliasIn(BaseModel):
    alias: str


@app.post("/api/settings/last_alias")
def save_last_alias(body: LastAliasIn):
    """最後に開いた物件を残す（次に起動したとき、その物件を開く）。最近開いた物件（5つまで）の先頭にもする。"""
    from core import settings_store
    visible = _visible_aliases()
    if body.alias not in visible:
        raise HTTPException(404, "その物件はありません")
    return _safe_json(settings_store.set_last_alias(body.alias, known=visible))


class CompanyIn(BaseModel):
    company: str


@app.post("/api/settings/company")
def save_company(body: CompanyIn):
    """「読み込んで組み立てる」で使う会社ルールを保存する。作り直すのは、次に組み立てるとき（★11/4 B工務店。SPEC §5）。"""
    from core import settings_store
    try:
        st = settings_store.set_company(body.company)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _safe_json({"company": st["company"]})


class ViewIn(BaseModel):
    view: str


@app.post("/api/settings/view")
def save_view(body: ViewIn):
    """前の「見え方の切り替え」。★11/5 から起動のしかた（自社用・他社用・見本用）は起動したときに決まるので、いつも 400。"""
    raise HTTPException(400, "使い方（自社用・他社用・見本用）は起動のしかたで決まるので、画面では切り替えられません。"
                             "別の使い方にするときは、その起動のファイル（自社用は start.bat、他社用は start_other.bat、"
                             "見本用は start_sample.bat）から起動し直してください")


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


def _prepare_sample_data() -> None:
    """見本用：初めて起動したときに、同梱の見本の家（公開リポジトリ）か、自社用のデータの組み立て済みの「デモ用」だけを写す（場所は出さない）。"""
    from core.paths import ensure_sample_data
    try:
        state = ensure_sample_data()
    except ValueError as e:                  # local_paths.yaml の書き間違い（HAKOYA が書いた文だけを出す）
        print(f"見本用のデータを用意できませんでした：{_user_message(e) or type(e).__name__}")
        return
    except Exception as e:  # noqa: BLE001  エラーの文には場所が入ることがあるので、種類だけ出す（見本用は起動は続ける）
        print(f"見本用のデータを用意できませんでした（{type(e).__name__}。空き容量や権限を確かめてください）")
        return
    if state == "copied":
        print("見本用のデータを用意しました（組み立て済みの見本の家を写しました）")
    elif state == "missing":
        print("見本用の家がまだありません。同梱の見本の家（samples\\sample_house）があるか確かめるか、"
              "自社用（start.bat）で「デモ用」を組み立ててから、もう一度起動してください")


def main() -> int:
    import uvicorn
    sys.stdout.reconfigure(encoding="utf-8")
    from core import settings_store
    mode = settings_store.mode()
    label = settings_store.mode_label(mode)
    # 口の番号は起動のしかたごとに別（3つを同時に開け、ブラウザの保存も別々になる）。HAKOYA_PORT で変えられる
    port = int(os.environ.get("HAKOYA_PORT") or MODE_PORTS[mode])
    url = f"http://{HOST}:{port}/"
    if _port_in_use(port):
        # ★古い HAKOYA が開いたままだと、新しい方が起動できない（2026-11-02、試験で起きた）
        print(f"口 {port} 番はもう使われています。HAKOYA（{label}）がすでに起動しているかもしれません。"
              f"前に開いた黒い画面を閉じてから、もう一度起動してください。")
        return 3
    if mode == "sample":
        _prepare_sample_data()
    print(f"HAKOYA（{label}）を起動しました → {url}（止めるときはこの画面で Ctrl+C）")
    if not os.environ.get("HAKOYA_NO_BROWSER"):      # 試験のときはブラウザを開かない
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
