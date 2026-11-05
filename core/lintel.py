"""窓の上の帯（壁ごとの lintel_band_mm）：決定 2-4「気になる所は直す案にして、人が決める」。

外壁は天井の高さ（継ぎ目）で分け、上は屋根の部品と一緒に印刷する（core/builder.py split_wall）。
窓・ドアの上端が継ぎ目に届くと、上の細い帯は抜くので（lintel_slivers）、並んだ窓の間の細い柱は
1階の部品の中で下だけでつながり、印刷・組み立てで折れやすい（★11/5 デモの家の検品）。

- 壁に lintel_band_mm（実寸 mm）があると、その並びの上だけ、1階の部品の外壁を「いちばん高い上端＋帯」まで残し、
  屋根の部品の外壁から同じ所を除く（core/builder.py）。ない・0 のときは今までどおり。
- コードの検査（core/findings.py）が、柱が上でつながらない外壁に直す案（会社ルール model.lintel_band_mm）を出す。
- 平屋だけ。2階建て（core/builder_multi.py）は区域ごとに切るので、まだ作らない（案も出さない）。
数値（柱の幅・帯の範囲と高さ）はコードが計算する。
"""
from __future__ import annotations

import math

BAND_FIELD = "lintel_band_mm"
BAND_RANGE_MM = (0.0, 500.0)    # 帯の高さの範囲（実寸。0 は帯なし）
LINTEL_MARGIN_MM = 100.0        # 帯を、並びの両端の開口からさらに伸ばす長さ（実寸）
SIDE_TITLE = {"S": "南", "N": "北", "E": "東", "W": "西"}     # 指摘の見出し（例「南の窓の間の柱が折れやすい」）


def band_of(w: dict) -> float:
    """壁の帯の高さ（実寸 mm）。項目がなければ 0（今までどおり）。"""
    v = w.get(BAND_FIELD)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0.0


def seam_runs(ops: list[dict], z_cut: float, min_feat: float) -> list[dict]:
    """継ぎ目（天井の高さ z_cut）に上端が届く開口（上端 ≥ 継ぎ目 − 最小の太さ）を壁に沿って並べ、
    間の柱（幅がその高さより小さい壁）でつながる並びに分ける。柱が1本以上ある並びだけを返す。
    並び：{"openings": 開口, "posts": [(t0, t1, 高さ)], "t0": 始め, "t1": 終わり, "head": いちばん高い上端, "head_min": いちばん低い上端}
    （t は壁の始点からの距離、実寸 mm。柱の高さは、隣り合う2つの開口の高い方の下端から継ぎ目まで）"""
    reach = sorted((o for o in ops if float(o["head_mm"]) >= z_cut - min_feat), key=lambda o: float(o["offset_mm"]))
    runs: list[dict] = []
    cur = None
    for o in reach:
        a, c = float(o["offset_mm"]), float(o["offset_mm"]) + float(o["width_mm"])
        sill, head = float(o["sill_mm"]), float(o["head_mm"])
        if cur is not None:
            gap = a - cur["t1"]
            free = z_cut - max(sill, cur["last_sill"])
            if gap < free:                       # 間の壁が高さより細い（柱）か、開口がつながっている → 同じ並び
                if gap >= 1.0:
                    cur["posts"].append((cur["t1"], a, free))
                cur["openings"].append(o)
                cur["t1"] = max(cur["t1"], c)
                cur["last_sill"] = sill
                cur["head"], cur["head_min"] = max(cur["head"], head), min(cur["head_min"], head)
                continue
        cur = {"openings": [o], "posts": [], "t0": a, "t1": c, "last_sill": sill, "head": head, "head_min": head}
        runs.append(cur)
    return [r for r in runs if r["posts"]]


def _side_of_wall(w: dict, center) -> str:
    """壁の外の面が向く側（東西南北）。立面の図を選ぶため。glb の +Y を北とみなす（推定）。"""
    sx, sy = map(float, w["start"])
    ex, ey = map(float, w["end"])
    mx, my = (sx + ex) / 2 - center[0], (sy + ey) / 2 - center[1]
    dx, dy = ex - sx, ey - sy
    nx, ny = -dy, dx
    if nx * mx + ny * my < 0:
        nx, ny = -nx, -ny
    if abs(nx) >= abs(ny):
        return "E" if nx > 0 else "W"
    return "N" if ny > 0 else "S"


def fragile_post_findings(b: dict, rules: dict) -> list[dict]:
    """外壁の窓の間の柱が、1階の部品の中で上がつながらない所（窓の上端が継ぎ目に届く）→ 直す案つきの指摘。
    b は人の修正を当てた家。帯がすでにある壁（lintel_band_mm > 0）には出さない。平屋だけ。"""
    from shapely.geometry import Polygon

    from core.builder import UPPER_WALL_ABOVE_FL, _wall_top, openings_by_wall_of, plane_z, wall_rect
    from core.roof_geom import surfaces
    from core.rules import rule

    levels = b.get("levels") or []
    m = rules.get("model") or {}
    walls = [w for w in b.get("walls", []) if w.get("kind") == "exterior" and "start" in w and "end" in w]
    if len(levels) != 1 or not m.get("split_at_ceiling") or not walls:
        return []
    try:
        to = float(rule(rules, "model.lintel_band_mm"))
    except KeyError:
        return []
    if to <= 0:
        return []
    scale = float(m["scale"])
    min_feat = float(m["min_feature_mm"]) * scale
    lv = levels[0]
    fl = float(lv["fl_mm"])
    z_cut = fl + float(lv.get("ceiling_mm", rules["levels"]["ceiling_height_mm"]))
    try:
        rb = surfaces(b["roof"])[0]
    except Exception:  # noqa: BLE001  屋根の形を読めない家は、屋根までの高さを確かめずに出す
        rb = None
    outline = Polygon((b.get("foundation") or {}).get("outline") or [])
    center = outline.centroid.coords[0] if not outline.is_empty else (0.0, 0.0)
    keys = ("wall", "offset_mm", "width_mm", "sill_mm", "head_mm", "center")
    ops_by = openings_by_wall_of([w for w in b["walls"] if "start" in w and "end" in w],
                                 [o for o in b.get("openings", []) if all(k in o for k in keys)])
    out = []
    for w in walls:
        if band_of(w) > 0 or float(w["bottom_mm"]) >= fl + UPPER_WALL_ABOVE_FL:
            continue
        r = wall_rect(w)
        if outline.is_empty or r.area <= 0 or r.intersection(outline).area / r.area < 0.5:
            continue                                   # 家の外に立つ壁（袖壁など）は別の作り方
        runs = []
        for run in seam_runs(ops_by.get(w["id"], []), z_cut, min_feat):
            top = _wall_top(w)
            tops = [top if top is not None else (plane_z(rb, *o["center"]) if rb is not None else math.inf)
                    for o in run["openings"]]
            if all(t - float(o["head_mm"]) >= min_feat for t, o in zip(tops, run["openings"])):
                runs.append(run)                       # 開口の上に壁がない（屋根まで抜いている）並びは、帯では直らない
        if not runs:
            continue
        ops = [o for run in runs for o in run["openings"]]
        posts = [p for run in runs for p in run["posts"]]
        ids = list(dict.fromkeys(o["id"] for o in ops))
        kinds = {o.get("kind") for o in ops}
        what = "窓" if kinds == {"window"} else ("ドア" if kinds == {"door"} else "窓・ドア")
        widths = [(t1 - t0) / scale for t0, t1, _ in posts]
        heights = [h / scale for _, _, h in posts]
        wtxt = f"{min(widths):.1f}" if max(widths) - min(widths) < 0.05 else f"{min(widths):.1f}〜{max(widths):.1f}"
        msg = (f"外壁 {w['id']} の{what} {'・'.join(ids)} の間の柱（{len(posts)} 本、幅 模型 {wtxt}mm・"
               f"高さ 約 {max(heights):.0f}mm）が、1階の部品の中で上がつながっていません"
               f"（{what}の上端が天井の高さの継ぎ目に届き、上は屋根の部品に入るため。印刷・組み立てで折れやすい）。"
               f"直す案：{what}の上に帯（模型 {to / scale:g}mm）を残す")
        reason = (f"{what} {'・'.join(ids)} の間の柱の上を、1階の部品の中でつなぐ（上端＋{to:g}mm まで外壁を残し、"
                  "屋根の部品の外壁から同じ所を除く）")
        side = _side_of_wall(w, center)
        out.append({"view": side, "kind": "print", "severity": "中", "message": msg,
                    "title": f"{SIDE_TITLE.get(side, '')}の{what}の間の柱が折れやすい",
                    "targets": [w["id"]] + ids, "evidence": ["コードの検査（窓の間の柱）"], "source": "code",
                    "proposal": {"target": w["id"], "field": BAND_FIELD, "from": band_of(w), "to": to,
                                 "value_source": "rule:model.lintel_band_mm", "reason": reason}})
    return out
