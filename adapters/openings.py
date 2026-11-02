"""開口（窓・ドア）。建具（typeETate）から穴の位置・幅・下端・上端を出し、どの壁の穴かを決める（SPEC §4-1 手順5）。

★物件Aで確かめたこと：
- 窓は、建具の箱がそのまま穴（SPEC どおり）。
- 壁の外に吊る引き戸（アウトセット）は、レール（OSTN）が穴の幅の約2倍あり、天井より上（3,024〜3,084）にも出ている。
  床の敷居（KURI）が穴の幅と壁の厚みを表すので、敷居があれば敷居を平面の形に使う。
- 取っ手（HTOT・DTOT・TOT1〜3）は壁の外に出ているので、穴の形には使わない。
"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon

from adapters.glb_reader import Part
from adapters.walls import Wall, bbox_polygon

HARDWARE = ("HTOT", "DTOT", "TOT1", "TOT2", "TOT3")   # 取っ手・握り
RAIL = ("OSTN",)                                     # 吊り戸のレール
SILL = "KURI"                                        # 敷居（くつずり）


def _suffix(code: str | None) -> str:
    return (code or "").split("_", 1)[-1]


def hole(part: Part) -> tuple[Polygon, float, float]:
    """建具の穴：平面の形（壁を貫く長方形）と、下端・上端の高さ（mm）。"""
    body = [pr for pr in part.primitives if _suffix(pr.code) not in HARDWARE + RAIL and len(pr.vertices)]
    if not body:
        body = [pr for pr in part.primitives if len(pr.vertices)]
    z = np.concatenate([pr.vertices[:, 2] for pr in body])
    zmin = float(z.min())
    # 敷居は、床の高さ（穴の下端）にあるときだけ使う（★FIX窓では上端に KURI が1本あるだけ）
    sills = [pr for pr in body if _suffix(pr.code) == SILL and abs(float(pr.vertices[:, 2].min()) - zmin) < 5]
    fp = bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in sills])) if sills else None
    if fp is None or fp.geom_type != "Polygon" or fp.area < 1.0:
        fp = bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in body]))
    return fp, zmin, float(z.max())


def classify(part: Part, sill: float, fl: float) -> tuple[str, str]:
    """種類（window / door / entrance_door）と形（sliding / fixed / hinged / outset_sliding など）。"""
    codes = [_suffix(pr.code) for pr in part.primitives]
    if any(c.startswith("TOT") or c == "EHN1" for c in codes):
        return "entrance_door", "entrance"
    if sill <= fl + 100:
        if "OSTN" in codes:
            return "door", "outset_sliding"    # 壁の外に吊る引き戸
        if "DTOT" in codes:
            return "door", "hinged"            # 開き戸
        if codes.count("SDOR") >= 2:
            return "door", "sliding_pair"      # 引き違い戸
        return "door", "unknown"
    mado = codes.count("MADO")
    if mado >= 2:
        return "window", "sliding"             # 引き違い（模型では中央に縦桟1本。SPEC §8）
    if mado == 0:
        return "window", "fixed"               # FIX（枠だけ）
    return "window", "unknown"


def assign_wall(fp: Polygon, sill: float, head: float, walls: list[Wall]) -> Wall | None:
    """穴の平面の形といちばん重なり、高さも重なる壁。"""
    best, best_area = None, 0.0
    for w in walls:
        top = w.top if w.top is not None else float("inf")
        if min(head, top) - max(sill, w.bottom) <= 0:
            continue
        a = fp.intersection(w.rect()).area
        if a > best_area:
            best, best_area = w, a
    return best


def openings(tate: list[Part], walls: list[Wall], fl: float) -> tuple[list[dict], list[dict]]:
    """開口の一覧と、壁が見つからなかった建具の一覧。壁は Wall の物で返す（番号は後で振る）。"""
    out, orphans = [], []
    for p in tate:
        fp, sill, head = hole(p)
        kind, style = classify(p, sill, fl)
        w = assign_wall(fp, sill, head, walls)
        if w is None:
            orphans.append({"source": p.source, "reason": "この建具が入る壁が見つからない"})
            continue
        d = w.direction
        pts = np.array(fp.exterior.coords)[:-1]
        proj = (pts - w.start) @ d
        out.append({"wall": w, "kind": kind, "style": style,
                    "offset_mm": float(proj.min()), "width_mm": float(proj.max() - proj.min()),
                    "sill_mm": sill, "head_mm": head, "sill_fl_mm": sill - fl, "head_fl_mm": head - fl,
                    "center": np.asarray(fp.centroid.coords[0]), "source": p.source, "footprint": fp})
    return out, orphans
