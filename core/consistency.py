"""glb にありえない形を見つける（直さない。「要確認」として出すだけ）。docs/proposal-glb-consistency.md の「見つける」。

作者（2026-11-03）：「glb 上でありえないようなものが人的ミスやソフトの仕様であった場合に、整合性をとる仕組み」。
直し方（自動で直すか、直す案にするか）は 作者と決める。ここでは見つけるだけ。
★見つけ方は物件A〜F（11/3）で確かめた。物件A の W8・W13、物件B の W13・W14 の重なりを拾い、
  物件B の引き込み戸 O17（戸袋の壁の外の、もともと壁のない隙間を通る）や、連窓の枠の小さな重なり（物件F）は拾わない。
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Point, Polygon

PARALLEL_DEG = 2.0           # 平行とみなす向きの差
WALL_OVERLAP_MM = 300.0      # 壁どうしが、厚みの中でこれより長く重なれば「同じ所に壁が2枚」（角のつなぎの重なりは拾わない）
PROTRUDE_MM = 20.0           # 開口が壁の端からこれより出れば「はみ出す」
OPENING_OVERLAP_MM = 50.0    # 開口どうしが、横・縦ともこれより重なれば「重なる」（連窓の枠の重なり 25mm は拾わない）
ABOVE_MM = 50.0              # 開口の上端が、壁の上端・屋根の下の面をこれより超えれば「突き抜ける」（★物件C・F：天井までのドアと、天井の 30mm 下で終わる内壁の差は拾わない）
SHORT_WALL_MM = 100.0        # これより短い壁は「ごく短い壁」

KIND_JA = {"exterior": "外壁", "interior": "内壁", "standalone": "単独の壁",
           "window": "窓", "door": "ドア", "entrance_door": "玄関ドア"}


def _line(w: dict) -> tuple[np.ndarray, np.ndarray, float]:
    s, e = np.array(w["start"], float), np.array(w["end"], float)
    L = float(np.linalg.norm(e - s))
    return s, (e - s) / L if L > 0 else np.array([1.0, 0.0]), L


def _rect(w: dict, t0: float | None = None, t1: float | None = None) -> Polygon:
    s, d, L = _line(w)
    n = np.array([-d[1], d[0]]) * float(w["thickness_mm"]) / 2
    a, b = s + d * (0.0 if t0 is None else t0), s + d * (L if t1 is None else t1)
    return Polygon([a + n, b + n, b - n, a - n])


def overlapping_walls(b: dict) -> list[dict]:
    """同じ階で平行な2枚の壁が、厚みの中で長く重なっている（同じ所に壁が2枚）。"""
    out, walls = [], b.get("walls", [])
    cos_tol = math.cos(math.radians(PARALLEL_DEG))
    for i, w1 in enumerate(walls):
        s1, d1, L1 = _line(w1)
        n1 = np.array([-d1[1], d1[0]])
        for w2 in walls[i + 1:]:
            if w1.get("level") != w2.get("level"):
                continue
            s2, d2, L2 = _line(w2)
            if abs(float(d1 @ d2)) < cos_tol:
                continue
            mid2 = s2 + d2 * L2 / 2
            if abs(float((mid2 - s1) @ n1)) >= (float(w1["thickness_mm"]) + float(w2["thickness_mm"])) / 2 - 1:
                continue
            t = sorted([float((s2 - s1) @ d1), float((s2 + d2 * L2 - s1) @ d1)])
            length = min(L1, t[1]) - max(0.0, t[0])
            if length > WALL_OVERLAP_MM:
                out.append({"kind": "walls_overlap", "targets": [w1["id"], w2["id"]],
                            "message": f"{KIND_JA.get(w1['kind'], w1['kind'])} {w1['id']} と{KIND_JA.get(w2['kind'], w2['kind'])} {w2['id']} が、"
                                       f"約 {length:.0f}mm 重なっている（同じ所に壁が2枚。glb の入力の重ねか (推定)）"})
    return out


def opening_problems(b: dict) -> list[dict]:
    """開口が壁の端からはみ出す・開口どうしが重なる・開口の上端が壁の上や屋根を突き抜ける。"""
    from core.builder import plane_z
    from core.roof_geom import surfaces
    walls = {w["id"]: w for w in b.get("walls", [])}
    ops = [o for o in b.get("openings", []) if o.get("wall") in walls and o.get("offset_mm") is not None]
    roofs = []
    for r in b.get("roofs") or ([b["roof"]] if b.get("roof") else []):
        if r.get("footprint") and (r.get("bottom_plane") or r.get("planes")):
            roofs.append((Polygon(r["footprint"]).buffer(10), surfaces(r)[0]))
    out = []
    for o in ops:
        w = walls[o["wall"]]
        _, _, L = _line(w)
        a, c = float(o["offset_mm"]), float(o["offset_mm"]) + float(o["width_mm"])
        name = f"{KIND_JA.get(o['kind'], o['kind'])} {o['id']}"
        if a < -PROTRUDE_MM or c > L + PROTRUDE_MM:
            outside = []
            if a < -PROTRUDE_MM:
                outside.append(_rect(w, a, 0.0))
            if c > L + PROTRUDE_MM:
                outside.append(_rect(w, L, c))
            hits = [x["id"] for x in walls.values() if x["id"] != w["id"] and x.get("level") == w.get("level")
                    and any(_rect(x).intersection(p).area > 0.3 * p.area for p in outside)]
            if o["kind"] == "window" or hits:      # 戸が壁のない隙間へ出るのは引き込み戸（物件B の O17）なので拾わない
                over = max(-a, c - L)
                out.append({"kind": "opening_protrudes", "targets": [o["id"], w["id"]] + hits,
                            "message": f"{name} が{KIND_JA.get(w['kind'], w['kind'])} {w['id']} の端から約 {over:.0f}mm はみ出している"
                                       + (f"（はみ出した所に {'・'.join(hits)} がある）" if hits else "")})
        top = None if w.get("height") == "to_roof" else float(w["height"])
        head = float(o["head_mm"])
        if top is not None and head > top + ABOVE_MM:
            out.append({"kind": "opening_above_top", "targets": [o["id"], w["id"]],
                        "message": f"{name} の上端（{head:.0f}）が、{w['id']} の上端（{top:.0f}）より {head - top:.0f}mm 高い"})
        elif top is None and roofs:
            p = Point(o["center"])
            zs = [plane_z(rb, p.x, p.y) for fp, rb in roofs if fp.contains(p)]
            zs = [z for z in zs if z > float(w["bottom_mm"])]
            if zs and head > min(zs) + ABOVE_MM:
                out.append({"kind": "opening_above_top", "targets": [o["id"], w["id"]],
                            "message": f"{name} の上端（{head:.0f}）が、屋根の下の面（{min(zs):.0f}）より {head - min(zs):.0f}mm 高い"})
    for i, o1 in enumerate(ops):
        for o2 in ops[i + 1:]:
            if o1["wall"] != o2["wall"]:
                continue
            h = min(o1["offset_mm"] + o1["width_mm"], o2["offset_mm"] + o2["width_mm"]) - max(o1["offset_mm"], o2["offset_mm"])
            v = min(o1["head_mm"], o2["head_mm"]) - max(o1["sill_mm"], o2["sill_mm"])
            if h > OPENING_OVERLAP_MM and v > OPENING_OVERLAP_MM:
                out.append({"kind": "openings_overlap", "targets": [o1["id"], o2["id"]],
                            "message": f"{o1['id']} と {o2['id']} が、同じ壁 {o1['wall']} の上で重なっている（横 {h:.0f}mm・縦 {v:.0f}mm）"})
    return out


def short_walls(b: dict) -> list[dict]:
    return [{"kind": "short_wall", "targets": [w["id"]],
             "message": f"{KIND_JA.get(w['kind'], w['kind'])} {w['id']} の長さが {_line(w)[2]:.0f}mm しかない（入力のかけらか (推定)）"}
            for w in b.get("walls", []) if _line(w)[2] < SHORT_WALL_MM]


def find_all(b: dict) -> list[dict]:
    """b は修正を当てたあとの building。見つけたもの（kind・targets・message）の一覧。"""
    return overlapping_walls(b) + opening_problems(b) + short_walls(b)
