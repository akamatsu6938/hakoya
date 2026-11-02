"""壁・開口・部屋の番号を、作り直しても変わらないようにする（SPEC §9-1：人の修正を当て直すため）。

前回の building.json と照らし合わせ、同じものには前回の番号を引き継ぐ。
- 壁：種類が同じ・向きがそろう・中心線のずれが POS_TOL 以内・長さの半分以上が重なる。
- 開口：glb の出どころが同じ。なければ、種類が同じで中心が OPENING_TOL 以内。
- 部屋：glb の出どころが同じ。
新しいものには続きの番号を振る。前回あって今回ないものの番号は id_changes.removed に残す。
"""
from __future__ import annotations

import math
import re

POS_TOL = 30.0        # 壁の中心線のずれ（mm）
OVERLAP_MIN = 0.5     # 短いほうの長さに対する重なりの割合
OPENING_TOL = 150.0   # 開口の中心のずれ（mm）


def _num(i: str) -> int:
    m = re.search(r"(\d+)$", i)
    return int(m.group(1)) if m else 0


def _wall_match_score(a: dict, b: dict) -> float:
    """同じ壁らしさ（重なりの長さ）。同じ壁でなければ 0。"""
    if a["kind"] != b["kind"] or a.get("level") != b.get("level"):
        return 0.0
    (ax0, ay0), (ax1, ay1) = a["start"], a["end"]
    (bx0, by0), (bx1, by1) = b["start"], b["end"]
    la, lb = math.hypot(ax1 - ax0, ay1 - ay0), math.hypot(bx1 - bx0, by1 - by0)
    if la < 1 or lb < 1:
        return 0.0
    dx, dy = (ax1 - ax0) / la, (ay1 - ay0) / la
    ex, ey = (bx1 - bx0) / lb, (by1 - by0) / lb
    if abs(dx * ex + dy * ey) < 0.999:
        return 0.0
    nx, ny = -dy, dx
    if max(abs((bx0 - ax0) * nx + (by0 - ay0) * ny), abs((bx1 - ax0) * nx + (by1 - ay0) * ny)) > POS_TOL:
        return 0.0
    t0, t1 = sorted(((bx0 - ax0) * dx + (by0 - ay0) * dy, (bx1 - ax0) * dx + (by1 - ay0) * dy))
    ov = min(la, t1) - max(0.0, t0)
    if ov <= 0 or ov / min(la, lb) < OVERLAP_MIN:
        return 0.0
    # 下端・上端が大きく違う（下がり壁と腰の部分など）は別の壁
    if abs(a.get("bottom_mm", 0) - b.get("bottom_mm", 0)) > 150:
        return 0.0
    return ov


def _assign(new: list[dict], old: list[dict], score, prefix: str) -> tuple[dict[str, str], list[str]]:
    """新しい番号 → 引き継ぐ番号 の対応と、消えた番号の一覧。"""
    pairs = sorted(((score(n, o), i, j) for i, n in enumerate(new) for j, o in enumerate(old)), reverse=True)
    used_new, used_old, mapping = set(), set(), {}
    for s, i, j in pairs:
        if s <= 0 or i in used_new or j in used_old:
            continue
        mapping[new[i]["id"]] = old[j]["id"]
        used_new.add(i)
        used_old.add(j)
    next_n = max([_num(o["id"]) for o in old] + [0]) + 1
    for i, n in enumerate(new):
        if i not in used_new:
            mapping[n["id"]] = f"{prefix}{next_n}"
            next_n += 1
    removed = [o["id"] for j, o in enumerate(old) if j not in used_old]
    return mapping, removed


def _opening_score(a: dict, b: dict) -> float:
    if a.get("source") and a.get("source") == b.get("source"):
        return 1e9
    if a["kind"] != b["kind"]:
        return 0.0
    d = math.hypot(a["center"][0] - b["center"][0], a["center"][1] - b["center"][1])
    return (OPENING_TOL - d) if d <= OPENING_TOL else 0.0


def _room_score(a: dict, b: dict) -> float:
    return 1.0 if a.get("source") and a.get("source") == b.get("source") else 0.0


def stabilize(building: dict, previous: dict | None) -> dict:
    """building の番号を、previous（前回の building.json）に合わせて付け直す。"""
    if not previous:
        building["id_changes"] = {"base": "初回のため、番号はすべて新しく振った"}
        return building
    wmap, wrem = _assign(building.get("walls", []), previous.get("walls", []), _wall_match_score, "W")
    omap, orem = _assign(building.get("openings", []), previous.get("openings", []), _opening_score, "O")
    rmap, rrem = _assign(building.get("rooms", []), previous.get("rooms", []), _room_score, "R")
    for w in building.get("walls", []):
        w["id"] = wmap[w["id"]]
    for o in building.get("openings", []):
        o["id"] = omap[o["id"]]
        o["wall"] = wmap.get(o["wall"], o["wall"])
    for r in building.get("rooms", []):
        r["id"] = rmap[r["id"]]
    for e in building.get("glb_exceptions", []):
        if e.get("wall"):
            e["wall"] = wmap.get(e["wall"], e["wall"])
    all_map = {**wmap, **omap}
    for a in building.get("assumptions", []):
        a["field"] = re.sub(r"\[(\w+)\]", lambda m: f"[{all_map.get(m.group(1), m.group(1))}]", a["field"])
    building["walls"].sort(key=lambda w: _num(w["id"]))
    building.get("openings", []).sort(key=lambda o: _num(o["id"]))
    building.get("rooms", []).sort(key=lambda r: _num(r["id"]))
    prev_ids = {x["id"] for k in ("walls", "openings", "rooms") for x in previous.get(k, [])}
    new_ids = [x["id"] for k in ("walls", "openings", "rooms") for x in building.get(k, []) if x["id"] not in prev_ids]
    building["id_changes"] = {"base": "前回の building.json に合わせて番号を引き継いだ",
                              "new": new_ids, "removed": wrem + orem + rrem}
    return building
