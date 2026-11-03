"""屋根（typeEYane）。屋根板の上の面・下の面の傾きから、形・勾配・向き・厚み・軒の出を出す（SPEC §4-1・§8）。

★物件Aで確かめたこと：
- 屋根板の上の面は YANE_MAIN、下の面は YANE_NURA。どちらも1枚の平面（片流れ）。
- 屋根板の端は4辺とも外壁の外の面とそろっている。低い側に約146mm出ているのは軒樋（YANE_NTOI）。
- 屋根板の平面の形は L 字（北西の角が欠ける）。上の面の三角形を合わせて本当の形を取る。
- 破風（HAFU）・鼻隠し（HANA・HAN2）・軒樋は模型では省く（SPEC §5-1 の include の既定）。

片流れ以外（2026-11-03 作者「できるようにしておいてほしい」）：上の面が1枚の平面でないときは、
上の面の三角形を向き（法線）と高さで面ごとに分け、面の一覧（planes）にする。面の数と向きで
切妻（gable）・寄棟（hip）・そのほか（multi）に分ける。★実物の glb（切妻・寄棟）はまだないので、
Walk in home の書き出し方は要検証（10件の glb で確かめる）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from adapters.glb_reader import Part

TOP, BOTTOM, GUTTER = "YANE_MAIN", "YANE_NURA", "YANE_NTOI"
ANGLE_TOL_DEG = 2.0          # 同じ面とみなす向きの差
OFFSET_TOL_MM = 20.0         # 同じ面とみなす高さの差（面に垂直に測る）
MIN_PLANE_MM2 = 100_000.0    # これより小さい面（0.1m²）は屋根の面とみなさない
DEFAULT_THICKNESS_MM = 100.0  # 下の面が見つからないときの屋根板の厚み（面に垂直。★物件A は約100）
SHAPE_JA = {"shed": "片流れ", "flat": "陸屋根", "gable": "切妻", "hip": "寄棟", "multi": "複数の面", "unknown": "不明"}


ROOF_JOIN_MM = 50.0          # 屋根の部材どうしが、平面でこれ以内に接していれば同じ屋根


@dataclass
class MergedRoofPart(Part):
    """面ごとに分かれて書き出された屋根の部材を、1つの屋根にまとめたもの。出どころは全部の番号。"""
    nodes: list[int] = field(default_factory=list)

    @property
    def source(self) -> str:
        return f"glb:{self.type_code or 'unknown'}#" + "+".join(str(n) for n in self.nodes)


def _top_extent(p: Part) -> tuple[Polygon | None, tuple[float, float] | None]:
    tris = p.faces(TOP)
    if not len(tris):
        return None, None
    poly = unary_union([Polygon(t[:, :2]) for t in tris if Polygon(t[:, :2]).area > 1])
    return poly, (float(tris[:, :, 2].min()), float(tris[:, :, 2].max()))


def merge_roof_parts(parts: list[Part]) -> list[Part]:
    """★Walk in home は切妻・寄棟の屋根を、面ごとに別の部材（typeEYane）で書き出す（物件C は4つ、物件D は2つ。11/3）。
    平面で接していて、高さの範囲が重なる部材を1つの屋根にまとめる（面に分けるのは detect_roof）。
    下屋と大屋根は高さの範囲が重ならないので、まとめない。片流れ（部材1つ）は今までどおり。"""
    info = [_top_extent(p) for p in parts]
    parent = list(range(len(parts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            (pi, zi), (pj, zj) = info[i], info[j]
            if pi is None or pj is None:
                continue
            if pi.distance(pj) <= ROOF_JOIN_MM and max(zi[0], zj[0]) <= min(zi[1], zj[1]) + ROOF_JOIN_MM:
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(parts)):
        groups.setdefault(find(i), []).append(i)
    out: list[Part] = []
    for g in sorted(groups.values(), key=lambda g: g[0]):
        if len(g) == 1:
            out.append(parts[g[0]])
            continue
        first = parts[g[0]]
        out.append(MergedRoofPart(node=first.node, name=first.name, type_code=first.type_code,
                                  primitives=[pr for k in g for pr in parts[k].primitives], nodes=[parts[k].node for k in g]))
    return out


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
        multi = _detect_planes(part, tops, bottoms, exterior_fp)
        if multi is None:
            roof.update(shape="unknown", note=f"屋根板の上の面が平面に分けられない（ずれ {err:.0f}mm）")
            return roof
        return multi
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


# ---------------------------------------------------------------- 片流れ以外（面が複数の屋根）

def _triangles(prims) -> list[tuple[np.ndarray, float, float, np.ndarray]]:
    """三角形ごとの（上向きの法線, 面の高さ n·p, 面積, 頂点）。"""
    out = []
    for pr in prims:
        for t in pr.vertices[pr.triangles]:
            n = np.cross(t[1] - t[0], t[2] - t[0])
            L = float(np.linalg.norm(n))
            if L < 2.0:                       # 面積 1mm² 未満は捨てる
                continue
            n = n / L
            if n[2] < 0:
                n = -n
            out.append((n, float(n @ t.mean(axis=0)), L / 2, t))
    return out


def _cluster(tris) -> list[dict]:
    """向きと高さが同じ三角形をまとめる（大きい三角形から順に）。"""
    cos_tol = math.cos(math.radians(ANGLE_TOL_DEG))
    clusters: list[dict] = []
    for n, _, area, t in sorted(tris, key=lambda x: -x[2]):
        c_pt = t.mean(axis=0)
        for c in clusters:
            nc = c["n"] / np.linalg.norm(c["n"])
            if float(nc @ n) >= cos_tol and abs(float(nc @ c_pt) - c["d"]) <= OFFSET_TOL_MM:
                c["n"] = c["n"] + n * area
                c["tris"].append(t)
                c["area"] += area
                break
        else:
            clusters.append({"n": n * area, "d": float(n @ c_pt), "tris": [t], "area": area})
    return clusters


def _flat_poly(tris) -> list[Polygon]:
    shape = unary_union([Polygon(t[:, :2]) for t in tris if Polygon(t[:, :2]).area > 1])
    return [g.simplify(1.0) for g in getattr(shape, "geoms", [shape]) if g.area >= MIN_PLANE_MM2]


def _plane_dict(a: float, b: float, c: float) -> dict:
    return {"a": round(a, 6), "b": round(b, 6), "c": round(c, 2)}


def _classify(planes: list[dict]) -> str:
    """面の数と向きで、切妻・寄棟・そのほか。"""
    dirs = []
    for p in planes:
        a, b = p["top_plane"]["a"], p["top_plane"]["b"]
        L = math.hypot(a, b)
        dirs.append(np.array([-a, -b]) / L if L > 1e-3 else None)
    if any(d is None for d in dirs):
        return "multi"
    opposite = lambda i, j: float(dirs[i] @ dirs[j]) < -0.95
    if len(planes) == 2 and opposite(0, 1):
        return "gable"
    if len(planes) == 4 and all(any(opposite(i, j) for j in range(4) if j != i) for i in range(4)):
        return "hip"
    return "multi"


def _detect_planes(part: Part, tops, bottoms, exterior_fp: Polygon) -> dict | None:
    """上の面を面ごとに分けて、面の一覧つきの屋根にする。分けられなければ None。"""
    top_cl = [c for c in _cluster(_triangles(tops)) if c["area"] >= MIN_PLANE_MM2]
    if len(top_cl) < 2:
        return None
    bot_cl = [c for c in _cluster(_triangles(bottoms)) if c["area"] >= MIN_PLANE_MM2]
    planes, thick = [], []
    for c in sorted(top_cl, key=lambda c: -c["area"]):
        pts = np.vstack(c["tris"])
        a, b, cc, err = _fit_plane(pts)
        if err > 5.0:
            return None
        polys = _flat_poly(c["tris"])
        slope = math.hypot(a, b)
        bottom = None
        nc = c["n"] / np.linalg.norm(c["n"])
        for bc in bot_cl:                       # 同じ向きで、平面で重なる下の面
            nb = bc["n"] / np.linalg.norm(bc["n"])
            if float(nb @ nc) >= math.cos(math.radians(3.0)) and polys and                     unary_union(_flat_poly(bc["tris"]) or [Polygon()]).intersection(polys[0]).area > 0.3 * polys[0].area:
                ba, bb, bcc, _ = _fit_plane(np.vstack(bc["tris"]))
                bottom = (ba, bb, bcc)
                thick.append((cc - bcc) / math.sqrt(1 + slope * slope))
                break
        for poly in polys:
            planes.append({"footprint": [[round(float(x), 1), round(float(y), 1)] for x, y in list(poly.exterior.coords)[:-1]],
                           "top_plane": _plane_dict(a, b, cc), "_bottom": bottom, "_slope": slope})
    if len(planes) < 2:
        return None
    t_perp = float(np.median(thick)) if thick else DEFAULT_THICKNESS_MM
    for k, p in enumerate(planes, 1):
        a, b, cc = p["top_plane"]["a"], p["top_plane"]["b"], p["top_plane"]["c"]
        s = p.pop("_slope")
        bt = p.pop("_bottom") or (a, b, cc - t_perp * math.sqrt(1 + s * s))
        p["id"] = str(k)
        p["bottom_plane"] = _plane_dict(*bt)
        p["pitch"] = [round(s * 10, 1), 10]
        low = _side_name(-a, -b) if s > 1e-3 else None
        if low:
            p["low_side"] = low
    top_pts = np.vstack([pr.vertices for pr in tops])
    slopes = sorted(p["pitch"][0] for p in planes)
    union = unary_union([Polygon(p["footprint"]) for p in planes])
    outer = max(getattr(union, "geoms", [union]), key=lambda g: g.area).simplify(1.0)
    roof = {"source": part.source, "shape": _classify(planes),
            "pitch": [slopes[-1], 10], "slope": round(slopes[-1] / 10, 4),
            "top_plane": planes[0]["top_plane"], "bottom_plane": planes[0]["bottom_plane"],
            "thickness_mm": round(t_perp, 1),
            "top_z_range_mm": [round(float(top_pts[:, 2].min())), round(float(top_pts[:, 2].max()))],
            "footprint": [[round(float(x), 1), round(float(y), 1)] for x, y in list(outer.exterior.coords)[:-1]],
            "eave_mm": _eaves(top_pts[:, :2], exterior_fp),
            "planes": planes,
            "planes_note": f"屋根の面 {len(planes)} 枚（{SHAPE_JA.get(_classify(planes))}）。top_plane・bottom_plane はいちばん大きい面"}
    if slopes[-1] - slopes[0] > 0.2:
        roof["pitch_note"] = f"面ごとに勾配が違う（{slopes[0]}〜{slopes[-1]}/10）。pitch はいちばん急な面"
    gutters = [pr for pr in part.primitives if pr.code == GUTTER]
    if gutters:
        g = np.vstack([top_pts[:, :2]] + [pr.vertices[:, :2] for pr in gutters])
        roof["eave_with_gutter_mm"] = _eaves(g, exterior_fp)
        roof["gutter_note"] = "軒樋（YANE_NTOI）を含めた出。模型では樋は省く（include.gutter）"
    return roof
