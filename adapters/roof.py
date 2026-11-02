"""屋根（typeEYane）。屋根板の上の面・下の面の傾きから、形・勾配・向き・厚み・軒の出を出す（SPEC §4-1・§8）。

★物件Aで確かめたこと：
- 屋根板の上の面は YANE_MAIN、下の面は YANE_NURA。どちらも1枚の平面（片流れ）。
- 屋根板の端は4辺とも外壁の外の面とそろっている。低い側に約146mm出ているのは軒樋（YANE_NTOI）。
- 屋根板の平面の形は L 字（北西の角が欠ける）。上の面の三角形を合わせて本当の形を取る。
- 破風（HAFU）・鼻隠し（HANA・HAN2）・軒樋は模型では省く（SPEC §5-1 の include の既定）。
"""
from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from adapters.glb_reader import Part

TOP, BOTTOM, GUTTER = "YANE_MAIN", "YANE_NURA", "YANE_NTOI"


def _fit_plane(pts: np.ndarray) -> tuple[float, float, float, float]:
    """z = a*x + b*y + c に当てはめる。4つ目は当てはまりの悪さ（最大のずれ、mm）。"""
    A = np.column_stack([pts[:, 0], pts[:, 1], np.ones(len(pts))])
    (a, b, c), *_ = np.linalg.lstsq(A, pts[:, 2], rcond=None)
    err = float(np.abs(A @ np.array([a, b, c]) - pts[:, 2]).max())
    return float(a), float(b), float(c), err


def _side_name(dx: float, dy: float) -> str | None:
    """水平の向き → 東西南北（glb の +Y を北とみなす。要検証）。斜めなら None。"""
    ang = math.degrees(math.atan2(dy, dx)) % 360
    for name, center in (("E", 0), ("N", 90), ("W", 180), ("S", 270)):
        if min(abs(ang - center), 360 - abs(ang - center)) <= 10:
            return name
    return None


def detect_roof(part: Part, exterior_fp: Polygon) -> dict:
    tops = [pr for pr in part.primitives if pr.code == TOP]
    bottoms = [pr for pr in part.primitives if pr.code == BOTTOM]
    if not tops:
        return {"shape": "unknown", "source": part.source, "note": "屋根板の上の面（YANE_MAIN）が見つからない"}
    top_pts = np.vstack([pr.vertices for pr in tops])
    a, b, c, err = _fit_plane(top_pts)
    roof = {"source": part.source}
    if err > 5.0:
        roof.update(shape="unknown", note=f"屋根板の上の面が1枚の平面ではない（ずれ {err:.0f}mm）。片流れ以外は対象外（SPEC §1）")
        return roof
    slope = math.hypot(a, b)
    roof["shape"] = "shed" if slope > 1e-3 else "flat"
    roof["pitch"] = [round(slope * 10, 1), 10]
    roof["slope"] = round(slope, 4)
    if slope > 1e-3:
        low = _side_name(-a, -b)  # 下る向き
        roof["low_side"] = low or "slanted"
        roof["low_side_assumption"] = "glb の +Y を北とした（推定：ポーチが西側にあることと一致）"
    roof["top_plane"] = {"a": round(a, 6), "b": round(b, 6), "c": round(c, 2)}
    if bottoms:
        ba, bb, bc, _ = _fit_plane(np.vstack([pr.vertices for pr in bottoms]))
        roof["bottom_plane"] = {"a": round(ba, 6), "b": round(bb, 6), "c": round(bc, 2)}
        vertical = c - bc
        roof["thickness_mm"] = round(vertical / math.sqrt(1 + slope * slope), 1)  # 面に垂直な厚み
    roof["top_z_range_mm"] = [round(float(top_pts[:, 2].min())), round(float(top_pts[:, 2].max()))]
    # 屋根板の平面の形：上の面の三角形を合わせた本当の形（★物件Aは L 字。囲む長方形にすると欠けた角まで覆ってしまう）
    slab = unary_union([Polygon(t[:, :2]) for pr in tops for t in pr.vertices[pr.triangles] if Polygon(t[:, :2]).area > 1])
    slab = max(getattr(slab, "geoms", [slab]), key=lambda g: g.area).simplify(1.0)
    roof["footprint"] = [[round(float(x), 1), round(float(y), 1)] for x, y in list(slab.exterior.coords)[:-1]]
    roof["eave_mm"] = _eaves(top_pts[:, :2], exterior_fp)
    gutters = [pr for pr in part.primitives if pr.code == GUTTER]
    if gutters:
        g = np.vstack([top_pts[:, :2]] + [pr.vertices[:, :2] for pr in gutters])
        roof["eave_with_gutter_mm"] = _eaves(g, exterior_fp)
        roof["gutter_note"] = "軒樋（YANE_NTOI）を含めた出。模型では樋は省く（include.gutter）"
    return roof


def _eaves(pts: np.ndarray, exterior_fp: Polygon) -> dict:
    """外壁の外形から、東西南北それぞれに何 mm 出ているか（縦横の範囲どうしの差。斜めの屋根は要検証）。"""
    sx0, sy0 = pts.min(axis=0)
    sx1, sy1 = pts.max(axis=0)
    ex0, ey0, ex1, ey1 = exterior_fp.bounds
    return {"W": round(ex0 - sx0), "E": round(sx1 - ex1), "S": round(ey0 - sy0), "N": round(sy1 - ey1)}
