"""屋根の面の集まり（片流れ以外の屋根：切妻・寄棟・L字など）。SPEC §8。
★2026-11-03 作者「片流れ以外の屋根とか複雑な凹凸はできるようにしておいてほしい」。

- 屋根（building.json の roof・roofs の各要素）は、面が2つ以上のとき planes（面の一覧）を持つ。
  面1つ（片流れ）のときは planes を持たず、いままでどおり top_plane / bottom_plane の1つの平面で組み立てる
  （物件A・B の模型は変わらない）。
- 面が複数のときは、上から見た平面を面ごとの「受け持ちの範囲」に分け、範囲ごとにその面の式で切る。
  受け持ちの範囲＝その面の平面の形＋屋根の外周の辺から外へ伸ばした帯。帯は角で留め（マイター）に分ける。
  勾配がそろった屋根では、寄棟の隅棟・L字の谷の線を外へ延ばした所で分かれる（軒を伸ばしても正しい）。
- 組み立ての道具（core/builder.py の keep_below・keep_above・plane_z・slab_solid）は、
  1つの平面（dict）の代わりに MultiPlane を受け取ると、範囲ごとに切って合わせる。
"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

REACH_MM = 6000.0          # 受け持ちの範囲を外へ伸ばす長さ（軒の出の上限 2000mm より十分大きく）
TOL_MM = 1.0


def roof_planes(roof: dict) -> list[dict]:
    """面の一覧（2つ以上のときだけ。片流れは空）。"""
    pl = roof.get("planes") or []
    return pl if len(pl) > 1 else []


def _ccw(coords) -> list[tuple[float, float]]:
    ring = [(float(x), float(y)) for x, y in coords]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if not Polygon(ring).exterior.is_ccw:
        ring = ring[::-1]
    return ring


def _split_at(ring: list[tuple[float, float]], points: list[np.ndarray]) -> list[tuple[float, float]]:
    """辺の途中にあるほかの面の頂点で、辺を分ける（L字などで、外周の辺の一部だけがほかの面と接するとき）。"""
    out = []
    for a, b in zip(ring, ring[1:] + ring[:1]):
        out.append(a)
        pa, pb = np.array(a), np.array(b)
        d = pb - pa
        L2 = float(d @ d)
        if L2 < TOL_MM:
            continue
        mids = []
        for p in points:
            t = float((p - pa) @ d) / L2
            if 1e-6 < t < 1 - 1e-6 and np.linalg.norm(pa + d * t - p) < TOL_MM:
                mids.append((t, (float(p[0]), float(p[1]))))
        out += [q for _, q in sorted(mids)]
    return out


def outer_edges(polys: list[Polygon]) -> list[tuple[np.ndarray, np.ndarray, int]]:
    """面の平面の形の辺のうち、屋根全体の外周にある辺（棟・隅棟・谷は除く）。(始点, 終点, 面の番号)。
    向きは屋根が左側（外側は右）。"""
    union = unary_union(polys)
    verts = [np.array(v, float) for p in polys for v in list(p.exterior.coords)[:-1]]
    out = []
    for i, p in enumerate(polys):
        ring = _split_at(_ccw(p.exterior.coords), verts)
        for a, b in zip(ring, ring[1:] + ring[:1]):
            a, b = np.array(a), np.array(b)
            L = float(np.linalg.norm(b - a))
            if L < TOL_MM:
                continue
            d = (b - a) / L
            n = np.array([d[1], -d[0]])
            if union.contains(Point(*((a + b) / 2 + n * 5.0))):
                continue                                   # 外側にも屋根がある → 棟・隅棟・谷
            out.append((a, b, i))
    return out


def _line_x(p1: np.ndarray, d1: np.ndarray, p2: np.ndarray, d2: np.ndarray) -> np.ndarray | None:
    cross = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(cross) < 1e-9:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / cross
    return p1 + d1 * t


def plane_domains(polys: list[Polygon], reach: float = REACH_MM) -> list[Polygon]:
    """面ごとの受け持ちの範囲（面の平面の形＋外周の辺から外へ reach 伸ばした帯。角は留めで分ける）。"""
    edges = outer_edges(polys)
    key = lambda p: (round(float(p[0]), 1), round(float(p[1]), 1))
    starts = {key(a): k for k, (a, _, _) in enumerate(edges)}
    ends = {key(b): k for k, (_, b, _) in enumerate(edges)}

    def offset(k):
        a, b, _ = edges[k]
        d = (b - a) / np.linalg.norm(b - a)
        n = np.array([d[1], -d[0]])
        return a + n * reach, d, n

    def corner(k_in, k_out, at):
        """辺 k_in の終わり（＝辺 k_out の始め）の、伸ばした帯の角。"""
        p1, d1, n1 = offset(k_in)
        p2, d2, _ = offset(k_out)
        x = _line_x(p1, d1, p2, d2)
        return at + n1 * reach if x is None else x

    domains = [[p] for p in polys]
    for k, (a, b, i) in enumerate(edges):
        k_next, k_prev = starts.get(key(b)), ends.get(key(a))
        _, _, n = offset(k)
        b_off = corner(k, k_next, b) if k_next is not None else b + n * reach
        a_off = corner(k_prev, k, a) if k_prev is not None else a + n * reach
        quad = Polygon([tuple(a), tuple(b), tuple(b_off), tuple(a_off)])
        if not quad.is_valid:
            quad = quad.buffer(0)
        if quad.area > 1.0:
            domains[i].append(quad)
    return [unary_union(d) for d in domains]


class MultiPlane:
    """屋根の上の面か下の面（面ごとの受け持ちの範囲と、その面の式 z = a·x + b·y + c）。"""

    def __init__(self, domains: list[Polygon], planes: list[dict]):
        self.items = list(zip(domains, planes))

    def z(self, x: float, y: float) -> float:
        p = Point(x, y)
        dom, pl = min(self.items, key=lambda it: it[0].distance(p))
        return pl["a"] * x + pl["b"] * y + pl["c"]

    def _cut(self, m, fn, offset: float):
        from core.builder import prism, union_all
        if m.is_empty():
            return m
        x0, y0, z0, x1, y1, z1 = m.bounding_box()
        return union_all([fn(m ^ prism(dom, z0 - 1.0, z1 + 1.0), pl, offset) for dom, pl in self.items])

    def keep_below(self, m, offset: float = 0.0):
        from core.builder import keep_below
        return self._cut(m, keep_below, offset)

    def keep_above(self, m, offset: float = 0.0):
        from core.builder import keep_above
        return self._cut(m, keep_above, offset)

    def domains(self) -> list[Polygon]:
        return [d for d, _ in self.items]


def surfaces(roof: dict) -> tuple[object, object]:
    """（下の面, 上の面）。片流れは building.json の平面の式（dict）のまま、面が複数なら MultiPlane。"""
    planes = roof_planes(roof)
    if not planes:
        return roof["bottom_plane"], roof["top_plane"]
    domains = plane_domains([Polygon(p["footprint"]) for p in planes])
    return MultiPlane(domains, [p["bottom_plane"] for p in planes]), MultiPlane(domains, [p["top_plane"] for p in planes])


def bottom_min(roof: dict) -> float | None:
    """屋根の下の面のいちばん低い所（面ごとの平面の形の頂点で測る）。"""
    planes = roof_planes(roof) or ([{"bottom_plane": roof.get("bottom_plane"), "footprint": roof.get("footprint")}])
    vals = [float(p["bottom_plane"]["a"]) * x + float(p["bottom_plane"]["b"]) * y + float(p["bottom_plane"]["c"])
            for p in planes if isinstance(p.get("bottom_plane"), dict) and p.get("footprint") for x, y in p["footprint"]]
    return min(vals) if vals else None
