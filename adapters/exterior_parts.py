"""基礎・ポーチ・ポーチの天井（SPEC §4-1・§8）。glb の形を平面の多角形と高さにして building.json に入れる。

模型にするときの作り方（基礎の帯の幅、中を空けるなど）は組み立て（builder）で決める。ここでは形と高さだけ。

★物件Aで確かめたこと：
- 基礎の天端の外形は KISO_ILOW の高さ 480 の面の外周（玄関のへこみを含む12角形）。
- ポーチの袖壁の下には、基礎の本体（KISO_HONT）の底（高さ0）が別にある。
- 外部の天井（typeESten）のうち、家の外形の内側にあるもの（腰掛けの上の下がり天井 FL+2200）はポーチの天井ではない。
"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from adapters.glb_reader import Part

FLAT_TOL = 1.0
MIN_SOFFIT_MM2 = 50_000.0   # これより小さいポーチの天井のかけらは捨てる（0.05m2）


def flat_union(parts: list[Part], codes: tuple[str, ...] | None = None, z: float | None = None) -> Polygon:
    """水平な三角形（codes の部位だけ、高さ z のものだけ）を合わせた平面の形。"""
    polys = []
    for p in parts:
        for pr in p.primitives:
            if codes is not None and pr.code not in codes:
                continue
            f = pr.vertices[pr.triangles]
            if not len(f):
                continue
            for t in f[np.ptp(f[:, :, 2], axis=1) < FLAT_TOL]:
                if z is not None and abs(t[0, 2] - z) > FLAT_TOL:
                    continue
                pg = Polygon(t[:, :2])
                if pg.area > 1:
                    polys.append(pg)
    if not polys:
        return Polygon()
    return unary_union(polys).buffer(0.5, join_style="mitre").buffer(-0.5, join_style="mitre")


def _coords(poly: Polygon) -> list[list[float]]:
    return [[round(float(x), 1), round(float(y), 1)] for x, y in list(poly.simplify(1.0).exterior.coords)[:-1]]


def _polys(g) -> list[Polygon]:
    return [x for x in getattr(g, "geoms", [g]) if not x.is_empty and x.area > 1]


def foundation(kiso: list[Part], top_mm: float) -> dict:
    """基礎の天端の外形と、袖壁などの下の基礎（底の形）。"""
    top = flat_union(kiso, ("KISO_ILOW",), top_mm)
    if top.is_empty:
        return {"top_mm": top_mm, "note": "基礎の天端の面（KISO_ILOW）が見つからない"}
    outline = max(_polys(top), key=lambda g: g.area)
    outline = Polygon(outline.exterior)  # 穴（内側）は埋めて、外形だけにする
    footings = [g for g in _polys(flat_union(kiso, ("KISO_HONT",), 0.0)) if not outline.buffer(1).contains(g)]
    return {"top_mm": top_mm, "outline": _coords(outline), "area_m2": round(outline.area / 1e6, 2),
            "footings": [{"polygon": _coords(g), "note": "袖壁などの下の基礎（底の形）"} for g in footings],
            "source": kiso[0].source}


def porches(parts: list[Part]) -> list[dict]:
    """ポーチ・段。上の面の形と高さ。"""
    out = []
    for p in sorted(parts, key=lambda q: -q.bounds()[1][2]):
        top = float(p.bounds()[1][2])
        shape = flat_union([p], z=top)
        for g in _polys(shape):
            out.append({"polygon": _coords(g), "top_mm": round(top), "bottom_mm": round(float(p.bounds()[0][2])),
                        "area_m2": round(g.area / 1e6, 2), "source": p.source})
    return [{"id": f"P{i}", **d} for i, d in enumerate(out, 1)]


def soffits(parts: list[Part], indoor: Polygon) -> tuple[list[dict], list[dict]]:
    """ポーチの天井。部屋の中（indoor）にあるものは除き、別に返す。"""
    out, skipped = [], []
    for p in parts:
        z = float(p.bounds()[1][2])
        for g in _polys(flat_union([p])):
            if g.intersection(indoor).area / g.area > 0.5:
                skipped.append({"source": p.source, "reason": f"部屋の中にある天井（高さ {z:.0f}）。ポーチの天井ではない（下がり天井など）"})
                continue
            if g.area < MIN_SOFFIT_MM2:  # 壁の角にできる小さなかけら
                continue
            out.append({"polygon": _coords(g), "z_mm": round(z), "area_m2": round(g.area / 1e6, 2), "source": p.source})
    return [{"id": f"S{i}", **d} for i, d in enumerate(out, 1)], skipped
