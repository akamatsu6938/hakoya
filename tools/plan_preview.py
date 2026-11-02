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
SCALE = 0.08     # 1mm → 0.08px（10m が 800px）
MARGIN = 60


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
    W = (x1 - x0) * SCALE + MARGIN * 2
    H = (y1 - y0) * SCALE + MARGIN * 2 + 70

    def P(x, y):  # 平面図の向き（Y が上）で描く
        return f"{(x - x0) * SCALE + MARGIN:.1f},{(y1 - y) * SCALE + MARGIN:.1f}"

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
           f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="sans-serif">',
           f'<rect width="100%" height="100%" fill="white"/>',
           f'<text x="{MARGIN}" y="30" font-size="16">{html.escape(title)}（平面図・壁の作り直し）</text>']
    for r in b.get("rooms", []):
        poly = " ".join(P(*p) for p in r["polygon"])
        out.append(f'<polygon points="{poly}" fill="#eef2fa" stroke="#b8c2d8" stroke-width="0.5"/>')
    for w in b["walls"]:
        poly = " ".join(P(*c) for c in wall_corners(w))
        dash = ' stroke-dasharray="4,2"' if w["bottom_mm"] > 1500 else ""  # 途中の高さから始まる壁は破線
        c = COLORS[w["kind"]]
        out.append(f'<polygon points="{poly}" fill="{c}" fill-opacity="0.35" stroke="{c}" stroke-width="1"{dash}>'
                   f'<title>{w["id"]} {LABELS[w["kind"]]} 厚み{w["thickness_mm"]}mm 下端{w["bottom_mm"]} '
                   f'{w.get("source", "")}</title></polygon>')
        mx, my = (w["start"][0] + w["end"][0]) / 2, (w["start"][1] + w["end"][1]) / 2
        out.append(f'<text x="{P(mx, my).split(",")[0]}" y="{P(mx, my).split(",")[1]}" font-size="10" '
                   f'fill="{c}" text-anchor="middle" dy="-3">{w["id"]}</text>')
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
    sx = W - MARGIN - 1000 * SCALE
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
