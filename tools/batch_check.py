"""登録してある物件を、まとめて「読み込んで組み立てる」と同じ道筋で通し、結果を表にする（最終の確認用。SPEC §12）。

使い方:
  python -m tools.batch_check                  登録してある全部の物件
  python -m tools.batch_check 物件C 物件D        指定した物件だけ
  --no-bambu  Bambu 形式の 3MF を作らない（速い）　--no-images  検品の図を描かない（速い）

道筋：glb を読み直して building.json（番号は前回から引き継ぐ）→ 模型を組み立てて書き出す（3MF・検査）→ 検品の図
→ AI なしの指摘（コードの検査・会社ルールとの違い・図面との照合）を数える。
指摘は保存しない（Claude の指摘と人の判定を上書きしないため）。Claude は使わない。画面で検品中の物件には使わない。
結果：data/out/_batch/<日時>/report.md・report.json（施主名などがないか確かめてから書く）。
出すのは数・種類・番号と、HAKOYA が作った文だけ。glb や図面の中の文字（名前）・元のファイル名は出さない。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SAME_MM = 20.0            # 間取りの比べ：壁の端・開口の中心がこれ以内なら同じ位置
KIND_JA = {"exterior": "外壁", "interior": "内壁", "standalone": "単独の壁",
           "window": "窓", "door": "ドア", "entrance_door": "玄関ドア"}


def _safe_error(e: BaseException) -> str:
    """エラーの種類と、HAKOYA のコードのどこで止まったか。メッセージは場所（パス）の文字がなく、ng_words もないときだけ出す。"""
    from core.privacy import PrivacyError, assert_clean
    ours = [f for f in traceback.extract_tb(e.__traceback__)
            if Path(f.filename).resolve().is_relative_to(REPO_ROOT) and ".venv" not in Path(f.filename).parts]
    where = f"{Path(ours[-1].filename).resolve().relative_to(REPO_ROOT).as_posix()}:{ours[-1].lineno}（{ours[-1].name}）" if ours else "?"
    msg = str(e)
    show = bool(msg) and len(msg) < 300 and not re.search(r"[\\/]|\.(glb|jww|pdf)", msg, re.I)
    if show:
        try:
            assert_clean(msg, "エラーの文")
        except PrivacyError:
            show = False
    return f"{type(e).__name__}：{msg if show else '（場所の文字が入るかもしれないので、文は出しません）'}／{where}"


def _pitch(p) -> str:
    return f"{p[0]:g}/{p[1]:g}" if isinstance(p, (list, tuple)) and len(p) == 2 else "?"


def describe(b: dict, summary: dict, code: list[dict], drawing: dict | None) -> dict:
    """1物件の結果を、数と種類でまとめる。"""
    from adapters.roof import SHAPE_JA
    out: dict = {}
    out["levels"] = [{"name": lv["name"], "fl_mm": lv.get("fl_mm"), "ceiling_mm": lv.get("ceiling_mm")} for lv in b["levels"]]
    outline = (b.get("foundation") or {}).get("outline") or []
    if outline:
        xs, ys = [p[0] for p in outline], [p[1] for p in outline]
        out["outline_mm"] = [round(max(xs) - min(xs)), round(max(ys) - min(ys))]
    multi = len(b["levels"]) > 1
    out["walls"] = dict(collections.Counter(
        (f"{w.get('level')}・" if multi else "") + KIND_JA.get(w["kind"], w["kind"]) for w in b["walls"]))
    out["openings"] = dict(collections.Counter(KIND_JA.get(o["kind"], o["kind"]) for o in b.get("openings", [])))
    roofs = []
    for r in b.get("roofs") or [b["roof"]]:
        planes = r.get("planes") or []
        pitches = sorted({_pitch(p.get("pitch")) for p in planes}) if planes else [_pitch(r.get("pitch"))]
        roofs.append({"id": r.get("id"), "level": r.get("level"), "shape": SHAPE_JA.get(r.get("shape"), r.get("shape")),
                      "planes": len(planes) or 1, "pitch": pitches, "eave_mm": r.get("eave_mm")})
    out["roofs"] = roofs
    out["glb_exceptions"] = dict(collections.Counter(e["kind"] for e in b.get("glb_exceptions", [])))
    out["unresolved"] = len(b.get("unresolved", []))
    out["skipped"] = len(b.get("skipped", []))
    checks = summary.get("checks", [])
    out["parts"] = len(summary.get("parts", {}))
    out["watertight"] = not any(c["item"] == "watertight" and c["level"] == "高" for c in checks)
    out["in_build_volume"] = not any(c["item"] == "build_volume" and c["level"] == "高" for c in checks)
    out["checks_high_mid"] = [f"[{c['level']}] {c['message']}" for c in checks if c["level"] in ("高", "中")]
    out["bambu_3mf"] = bool((summary.get("bambu_3mf") or {}).get("ok"))
    out["code_findings"] = dict(collections.Counter(f"{f['kind']}・{f['severity']}" for f in code))
    out["code_messages"] = [f"[{f['severity']}] {f['message']}" for f in code]
    if drawing:
        rows = []
        for r in drawing.get("levels", []):
            rows.append(f"{r.get('level', '')} 平面図：" + (f"注記 {r.get('notes')} 件・結んだ {r.get('matched')}・一致 {r.get('same')}"
                                                        if r.get("aligned", True) and "notes" in r else (r.get("note") or "位置合わせできず")))
        for r in drawing.get("elevations", []):
            rows.append(f"立面 {r['view']}：" + (f"窓・ドア {r.get('windows')} か所のうち {r.get('same')} か所が合う" if r.get("aligned") and "windows" in r
                                               else (r.get("note") or "位置合わせできず")))
        if not drawing.get("available", True):
            rows.append(drawing.get("note") or "図面なし")
        out["drawing"] = rows
    return out


def _walls_norm(b: dict) -> tuple[list, list]:
    """間取りの比べ用：1階の壁の端と開口の中心（いちばん左下の壁の端を原点にそろえる）。"""
    lv0 = b["levels"][0]["name"]
    walls = [w for w in b["walls"] if w.get("level", lv0) == lv0]
    if not walls:
        return [], []
    x0 = min(min(w["start"][0], w["end"][0]) for w in walls)
    y0 = min(min(w["start"][1], w["end"][1]) for w in walls)
    ws = [(w["kind"], (w["start"][0] - x0, w["start"][1] - y0), (w["end"][0] - x0, w["end"][1] - y0)) for w in walls]
    os_ = [(o["kind"], (o["center"][0] - x0, o["center"][1] - y0), float(o.get("width_mm", 0)))
           for o in b.get("openings", []) if o.get("level", lv0) == lv0 and o.get("center")]
    return ws, os_


def _near(p, q) -> bool:
    return abs(p[0] - q[0]) <= SAME_MM and abs(p[1] - q[1]) <= SAME_MM


def plan_similarity(b1: dict, b2: dict) -> dict:
    """2つの家の1階の間取りがどれだけ同じか（壁は種類と両端、開口は種類・中心・幅で比べる）。"""
    w1, o1 = _walls_norm(b1)
    w2, o2 = _walls_norm(b2)
    wm = sum(any(k == k2 and ((_near(s, s2) and _near(e, e2)) or (_near(s, e2) and _near(e, s2))) for k2, s2, e2 in w2)
             for k, s, e in w1)
    om = sum(any(k == k2 and _near(c, c2) and abs(w - w2_) <= SAME_MM for k2, c2, w2_ in o2) for k, c, w in o1)
    return {"walls": [wm, len(w1), len(w2)], "openings": [om, len(o1), len(o2)],
            "same": bool(w1) and wm == len(w1) == len(w2) and om == len(o1) == len(o2)}


def run_one(alias: str, bambu: bool, images: bool) -> tuple[dict, dict | None]:
    """1物件を通す。（結果の行, 修正を当てた building）を返す。"""
    from adapters.walkinhome import build, write_building
    from core import overrides as ov
    from core.export import write_outputs
    from core.findings import code_findings
    from core.paths import out_dir, property_files
    row: dict = {"alias": alias, "ok": False, "seconds": {}}
    files = property_files(alias)
    row.update(glb=files.glb is not None, drawings=sorted(files.jww), notes=list(files.notes))
    if files.glb is None:
        row["stopped"] = "glb がない"
        return row, None
    step, b = "読み取り", None
    try:
        t = time.time()
        write_building(alias, build(alias))
        row["seconds"][step] = round(time.time() - t, 1)
        b, _ = ov.apply(json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8")), ov.load(alias))
        step, t = "組み立て", time.time()
        summary = write_outputs(alias, bambu_enabled=bambu)
        row["seconds"][step] = round(time.time() - t, 1)
        if images:
            from core.render import render_all
            step, t = "検品の図", time.time()
            render_all(alias)
            row["seconds"][step] = round(time.time() - t, 1)
        step, t = "AI なしの指摘", time.time()
        code = code_findings(alias)
        drawing = None
        if files.jww:
            from core.drawing_check import summary as drawing_summary
            try:
                drawing = drawing_summary(alias, b)
            except Exception as e:  # noqa: BLE001  図面が読めなくても、ほかの結果は出す
                drawing = {"available": False, "note": f"図面を読めませんでした（{_safe_error(e)}）"}
        row["seconds"][step] = round(time.time() - t, 1)
    except Exception as e:  # noqa: BLE001  1件で止まっても、ほかの物件は続ける
        row["stopped"] = step
        row["error"] = _safe_error(e)
        return row, b
    row["ok"] = True
    row.update(describe(b, summary, code, drawing))
    return row, b


def report_markdown(rows: list[dict], pairs: list[dict], rules: list[dict], title: str) -> str:
    lines = [f"# {title}", "", "| 物件 | 結果 | 階 | 屋根 | 壁 | 開口 | 部品 | 閉じた立体 | 造形範囲 | 高・中 | AI なしの指摘 | 図面 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r["ok"]:
            lines.append(f"| {r['alias']} | 止まった（{r.get('stopped')}） | | | | | | | | | | {'・'.join(r['drawings']) or 'なし'} |")
            continue
        roofs = "・".join(f"{x['shape']}（面{x['planes']}、{'/'.join(x['pitch'])}）" for x in r["roofs"])
        walls = "・".join(f"{k}{v}" for k, v in r["walls"].items())
        ops = "・".join(f"{k}{v}" for k, v in r["openings"].items())
        lines.append(f"| {r['alias']} | 通った | {'2階建て' if len(r['levels']) > 1 else '平屋'} | {roofs} | {walls} | {ops} | "
                     f"{r['parts']} | {'はい' if r['watertight'] else '**いいえ**'} | {'はい' if r['in_build_volume'] else '**超える**'} | "
                     f"{len(r['checks_high_mid'])} | {sum(r['code_findings'].values())} | {'・'.join(r['drawings']) or 'なし'} |")
    lines += ["", "## 物件ごと"]
    for r in rows:
        lines += ["", f"### {r['alias']}", ""]
        lines.append(f"- glb：{'あり' if r['glb'] else 'なし'}　図面：{'・'.join(r['drawings']) or 'なし'}　時間（秒）：{r['seconds']}")
        for n in r.get("notes", []):
            lines.append(f"- メモ：{n}")
        if not r["ok"]:
            lines.append(f"- **止まった所：{r.get('stopped')}**　{r.get('error', '')}")
            continue
        lines.append("- 階：" + "・".join(f"{lv['name']}（FL {lv['fl_mm']}・天井 {lv['ceiling_mm']}）" for lv in r["levels"]))
        if r.get("outline_mm"):
            w, d = r["outline_mm"]
            lines.append(f"- 基礎の外形：{w}×{d}mm（模型 {w / 50:.1f}×{d / 50:.1f}mm）")
        for x in r["roofs"]:
            lines.append(f"- 屋根 {x['id']}（{x['level']}）：{x['shape']}・面 {x['planes']}・勾配 {'/'.join(x['pitch'])}・軒の出 {x['eave_mm']}")
        lines.append(f"- glb の例外：{r['glb_exceptions'] or 'なし'}　未解決：{r['unresolved']}　飛ばした部材：{r['skipped']}　Bambu 形式：{'あり' if r['bambu_3mf'] else 'なし'}")
        for c in r["checks_high_mid"]:
            lines.append(f"- 検査 {c}")
        for c in r["code_messages"]:
            lines.append(f"- 指摘 {c}")
        for c in r.get("drawing", []):
            lines.append(f"- 図面 {c}")
    if pairs:
        lines += ["", "## 間取りの比べ（1階の壁と開口）", ""]
        for p in pairs:
            s = p["sim"]
            lines.append(f"- {p['a']} と {p['b']}：壁 {s['walls'][0]}/{s['walls'][1]}（相手 {s['walls'][2]}）・開口 {s['openings'][0]}/{s['openings'][1]}"
                         f"（相手 {s['openings'][2]}）→ {'**同じ間取り**' if s['same'] else '違う'}")
    if rules:
        lines += ["", "## 会社ルールの集計（全物件）", ""]
        for x in rules:
            lines.append(f"- {x['label']}：{x.get('agreement', '')}" + ("" if x.get("rule_match", True) else "（会社ルールと違う）"))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="登録してある物件をまとめて組み立て、結果を表にする")
    ap.add_argument("aliases", nargs="*")
    ap.add_argument("--no-bambu", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ns = ap.parse_args(argv)
    from core.paths import DATA_DIR, aliases
    from core.privacy import assert_clean
    names = ns.aliases or aliases()
    unknown = [a for a in names if a not in aliases()]
    if unknown:
        raise SystemExit(f"登録されていない物件：{'・'.join(unknown)}")
    rows, built = [], {}
    for a in names:
        print(f"… {a}", flush=True)
        row, b = run_one(a, bambu=not ns.no_bambu, images=not ns.no_images)
        rows.append(row)
        if row["ok"]:
            built[a] = b
        print(f"  → {'通った' if row['ok'] else '止まった（' + str(row.get('stopped')) + '）'} {row['seconds']} {row.get('error', '')}", flush=True)
    keys = list(built)
    pairs = [{"a": x, "b": y, "sim": plan_similarity(built[x], built[y])} for i, x in enumerate(keys) for y in keys[i + 1:]]
    pairs = [p for p in pairs if p["sim"]["walls"][0] >= 0.5 * max(p["sim"]["walls"][1], 1)]   # 半分以上同じものだけ出す
    try:
        from core.rule_stats import collect
        rules = collect("jisha")
    except Exception as e:  # noqa: BLE001
        rules = [{"label": "会社ルールの集計", "agreement": f"できませんでした（{_safe_error(e)}）"}]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = DATA_DIR / "out" / "_batch" / stamp
    dst.mkdir(parents=True)
    md = report_markdown(rows, pairs, rules, f"まとめて検査（{stamp}・{len(rows)} 件）")
    js = json.dumps({"rows": rows, "pairs": pairs, "rules": rules}, ensure_ascii=False, indent=1, default=str)
    assert_clean(md, "まとめて検査の結果")
    assert_clean(js, "まとめて検査の結果")
    (dst / "report.md").write_text(md, encoding="utf-8")
    (dst / "report.json").write_text(js, encoding="utf-8")
    ok = sum(r["ok"] for r in rows)
    print(f"【まとめて検査】{len(rows)} 件のうち {ok} 件が最後まで通った → data/out/_batch/{stamp}/report.md")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
