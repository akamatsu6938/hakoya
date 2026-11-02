"""building.json から平面図の絵（SVG）を作る。図面と見比べるための確認用。

使い方:  python -m tools.plan_preview 物件A   → data/out/物件A/plan.svg（ブラウザで開く）
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys

from core.paths import out_dir
from core.privacy import assert_clean

COLORS = {"exterior": "#d64545", "standalone": "#3355dd", "interior": "#1f9d55"}
LABELS = {"exterior": "外壁", "standalone": "単独の壁", "interior": "内壁"}
OPENING_COLORS = {"window": "#1a73c9", "door": "#c26a00", "entrance_door": "#8a3fc0"}
OPENING_LABELS = {"window": "窓", "door": "ドア", "entrance_door": "玄関ドア"}
STYLE_LABELS = {"sliding": "引き違い", "fixed": "FIX", "unknown": "形不明", "outset_sliding": "吊り引き戸",
                "hinged": "開き戸", "sliding_pair": "引き違い戸", "entrance": "玄関"}
SCALE = 0.08     # 1mm → 0.08px（10m が 800px）
MARGIN = 60
LIST_WIDTH = 400  # 右側の壁の一覧の幅


def wall_corners(w: dict) -> list[tuple[float, float]]:
    (x0, y0), (x1, y1) = w["start"], w["end"]
    L = math.hypot(x1 - x0, y1 - y0) or 1.0
    dx, dy = (x1 - x0) / L, (y1 - y0) / L
    nx, ny = -dy * w["thickness_mm"] / 2, dx * w["thickness_mm"] / 2
    return [(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]


def render_svg(b: dict, title: str) -> str:
    pts = [c for w in b["walls"] for c in wall_corners(w)]
    pts += [tuple(p) for r in b.get("rooms", []) for p in r["polygon"]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    plan_w = (x1 - x0) * SCALE + MARGIN * 2
    W = plan_w + LIST_WIDTH
    n_lines = len(b["walls"]) + len(b.get("unresolved", [])) + len(b.get("openings", [])) + 10
    H = max((y1 - y0) * SCALE + MARGIN * 2 + 70, MARGIN + 60 + 16 * n_lines)

    def P(x, y):  # 平面図の向き（Y が上）で描く
        return f"{(x - x0) * SCALE + MARGIN:.1f},{(y1 - y) * SCALE + MARGIN:.1f}"

    # data-*：画面で囲んだ点を平面の mm に直すための値（mm = (画面の点 − 余白) ÷ 縮尺 ＋ 原点。Y は上下が逆）
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
           f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="sans-serif" '
           f'data-x0="{x0:.1f}" data-y1="{y1:.1f}" data-scale="{SCALE}" data-margin="{MARGIN}">',
           f'<rect width="100%" height="100%" fill="white"/>',
           f'<text x="{MARGIN}" y="30" font-size="16">{html.escape(title)}（平面図・壁の作り直し）</text>']
    for r in b.get("rooms", []):
        poly = " ".join(P(*p) for p in r["polygon"])
        out.append(f'<polygon points="{poly}" fill="#eef2fa" stroke="#b8c2d8" stroke-width="0.5"/>')
    for w in b["walls"]:
        poly = " ".join(P(*c) for c in wall_corners(w))
        dash = ' stroke-dasharray="4,2"' if w["bottom_mm"] > 1500 else ""  # 途中の高さから始まる壁は破線
        c = COLORS[w["kind"]]
        out.append(f'<polygon data-id="{w["id"]}" points="{poly}" fill="{c}" fill-opacity="0.35" stroke="{c}" stroke-width="1"{dash}>'
                   f'<title>{w["id"]} {LABELS[w["kind"]]} 厚み{w["thickness_mm"]}mm 下端{w["bottom_mm"]} '
                   f'{w.get("source", "")}</title></polygon>')
        mx, my = (w["start"][0] + w["end"][0]) / 2, (w["start"][1] + w["end"][1]) / 2
        out.append(f'<text x="{P(mx, my).split(",")[0]}" y="{P(mx, my).split(",")[1]}" font-size="10" '
                   f'fill="{c}" text-anchor="middle" dy="-3">{w["id"]} {w["thickness_mm"]:g}</text>')
    walls_by_id = {w["id"]: w for w in b["walls"]}
    for o in b.get("openings", []):
        w = walls_by_id[o["wall"]]
        (x0w, y0w), (x1w, y1w) = w["start"], w["end"]
        L = math.hypot(x1w - x0w, y1w - y0w) or 1.0
        dx, dy = (x1w - x0w) / L, (y1w - y0w) / L
        a = (x0w + dx * o["offset_mm"], y0w + dy * o["offset_mm"])
        e = (a[0] + dx * o["width_mm"], a[1] + dy * o["width_mm"])
        piece = {"start": a, "end": e, "thickness_mm": w["thickness_mm"] + 40}
        poly = " ".join(P(*c) for c in wall_corners(piece))
        col = OPENING_COLORS[o["kind"]]
        out.append(f'<polygon data-id="{o["id"]}" points="{poly}" fill="white" stroke="{col}" stroke-width="1.5"/>')
        cx, cy = P(*o["center"]).split(",")
        out.append(f'<text x="{cx}" y="{cy}" font-size="9" fill="{col}" text-anchor="middle" dy="12">'
                   f'{o["id"]} FL+{o["head_fl_mm"]}</text>')
    for u in b.get("unresolved", []):
        a, e = P(*u["start"]), P(*u["end"])
        out.append(f'<line x1="{a.split(",")[0]}" y1="{a.split(",")[1]}" x2="{e.split(",")[0]}" y2="{e.split(",")[1]}" '
                   f'stroke="#ff8800" stroke-width="5" stroke-dasharray="6,3"><title>{u["id"]} 未解決（壁があるか要確認）</title></line>')
    # 凡例と縮尺
    ly = H - 40
    lx = MARGIN
    for kind in ("exterior", "standalone", "interior"):
        out.append(f'<rect x="{lx}" y="{ly}" width="18" height="10" fill="{COLORS[kind]}" fill-opacity="0.5"/>'
                   f'<text x="{lx + 24}" y="{ly + 10}" font-size="12">{LABELS[kind]}</text>')
        lx += 100
    out.append(f'<line x1="{lx}" y1="{ly + 5}" x2="{lx + 30}" y2="{ly + 5}" stroke="#ff8800" stroke-width="5" stroke-dasharray="6,3"/>'
               f'<text x="{lx + 36}" y="{ly + 10}" font-size="12">未解決の所</text>')
    lx += 130
    out.append(f'<text x="{lx}" y="{ly + 10}" font-size="12">破線＝途中の高さから始まる壁</text>')
    # 右側：壁の一覧（マウスを使わなくても読めるように）
    tx, ty = plan_w + 10, MARGIN
    out.append(f'<text x="{tx}" y="{ty}" font-size="13" font-weight="bold">壁の一覧（番号・種類・厚み・下端）</text>')
    for w in b["walls"]:
        ty += 16
        extra = "　途中から始まる壁" if w["bottom_mm"] > 1500 else ""
        out.append(f'<text x="{tx}" y="{ty}" font-size="12" fill="{COLORS[w["kind"]]}">'
                   f'{w["id"]}　{LABELS[w["kind"]]}　厚み {w["thickness_mm"]:g}mm　下端 {w["bottom_mm"]}{extra}</text>')
    for u in b.get("unresolved", []):
        ty += 16
        out.append(f'<text x="{tx}" y="{ty}" font-size="12" fill="#ff8800">{u["id"]}　未解決（壁があるか要確認）　幅 {u["width_mm"]}mm</text>')
    if b.get("openings"):
        ty += 26
        out.append(f'<text x="{tx}" y="{ty}" font-size="13" font-weight="bold">開口の一覧（番号・壁・幅・下端〜上端）</text>')
        for o in b["openings"]:
            ty += 16
            out.append(f'<text x="{tx}" y="{ty}" font-size="12" fill="{OPENING_COLORS[o["kind"]]}">'
                       f'{o["id"]}　{OPENING_LABELS[o["kind"]]}（{STYLE_LABELS.get(o["style"], o["style"])}）　{o["wall"]}　'
                       f'幅 {o["width_mm"]:g}　FL+{o["sill_fl_mm"]}〜FL+{o["head_fl_mm"]}</text>')
    r = b.get("roof") or {}
    if r.get("shape") == "shed":
        ty += 26
        out.append(f'<text x="{tx}" y="{ty}" font-size="13" font-weight="bold">屋根</text>')
        for line in (f"片流れ {r['pitch'][0]}/10　低い側 {r.get('low_side')}（+Y を北とした）　厚み {r.get('thickness_mm')}mm",
                     f"軒の出 西{r['eave_mm']['W']} 東{r['eave_mm']['E']} 南{r['eave_mm']['S']} 北{r['eave_mm']['N']}mm（樋は除く）"):
            ty += 16
            out.append(f'<text x="{tx}" y="{ty}" font-size="12">{html.escape(line)}</text>')
    sx = plan_w - MARGIN - 1000 * SCALE
    out.append(f'<line x1="{sx}" y1="{ly + 25}" x2="{sx + 1000 * SCALE}" y2="{ly + 25}" stroke="black" stroke-width="2"/>'
               f'<text x="{sx}" y="{ly + 20}" font-size="11">1m</text>')
    out.append("</svg>")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="building.json から平面図の絵（SVG）を作る")
    ap.add_argument("alias", help="物件の仮名（例：物件A）")
    args = ap.parse_args(argv)
    src = out_dir(args.alias) / "building.json"
    if not src.exists():
        print(f"先に python -m adapters.walkinhome {args.alias} を実行してください。")
        return 1
    svg = render_svg(json.loads(src.read_text(encoding="utf-8")), args.alias)
    assert_clean(svg, "plan.svg")
    dst = out_dir(args.alias) / "plan.svg"
    dst.write_text(svg, encoding="utf-8")
    print(f"→ 書き出し: data/out/{args.alias}/plan.svg（ブラウザで開いてください）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
