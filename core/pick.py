"""3D で囲んで直す（SPEC §9-3）の当たり判定用の形（部材ごとの見えない形）。

画面はこの形を部材ごとの色で塗り、本物の模型の奥行きと比べて「囲みの中に見えている画素の数」を数え、
サーバーに送る。どの部材を選ぶかは core/lasso.select_hits（コード）が決める。
形は組み立てと同じ道具（prism・wall_rect・keep_below）で作る。座標は実寸 mm（画面では 1/50 にして重ねる）。
- 壁：中心線±厚みの箱。下端から上端まで（屋根までの壁は glb の上端か屋根の下の面まで）
- 開口：組み立てで穴を開ける箱と同じ（壁より 40mm 厚い。壁の面より手前に出るので、壁より先に当たる）
- 軒（Y1:S など）：屋根板の、その向きの縁の帯（屋根板より少し厚く、屋根全体より先に当たる）
- 屋根全体（Y1）：屋根板の残りの部分
parts は、その部材が写る模型の部品（画面で部品を隠しているときは、その部材を選ばない）。
"""
from __future__ import annotations

import numpy as np
from manifold3d import Manifold
from shapely.affinity import translate
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from core.builder import SIDE_DIRS, keep_above, keep_below, plane_z, prism, slab_outline, wall_rect
from core.roof_geom import surfaces as roof_surfaces

EAVE_BAND_MM = 600.0      # 軒の帯の幅（屋根板の縁から内側へ。軒の出と板金の分はさらに足す）
PROUD_MM = 20.0           # 軒の帯を屋根板より厚く・広くする分（屋根全体より先に当たるように）


def _mesh(m: Manifold) -> tuple[list[float], list[int]]:
    mesh = m.to_mesh()
    v = np.asarray(mesh.vert_properties, float)[:, :3]
    t = np.asarray(mesh.tri_verts, np.int64)
    return np.round(v, 1).ravel().tolist(), t.ravel().tolist()


def _wall_parts(w: dict, multi: bool) -> list[str]:
    if multi:
        if w.get("level") == "2F":
            return ["floor2"] if w["kind"] == "interior" else ["floor2", "exterior2", "roof_ext"]
        return ["floor1"] if w["kind"] == "interior" else ["floor1", "exterior1", "roof_lower_ext"]
    return ["floor1"] if w["kind"] == "interior" else ["floor1", "exterior1", "roof_ext"]


def _covering_roof(roofs: list[dict], w: dict) -> dict | None:
    """壁の真ん中の上にある、同じ階の屋根（屋根までの壁の上端を決める）。"""
    mid = Point((np.array(w["start"], float) + np.array(w["end"], float)) / 2)
    same = [r for r in roofs if r.get("level", w.get("level")) == w.get("level") and r.get("footprint")]
    for r in same:
        if Polygon(r["footprint"]).buffer(w["thickness_mm"]).contains(mid):
            return r
    return same[0] if len(same) == 1 else None


def _wall_solid(w: dict, roofs: list[dict], roof_top: float) -> Manifold:
    z0 = float(w["bottom_mm"])
    if w["height"] != "to_roof":
        return prism(wall_rect(w), z0, float(w["height"]))
    z1 = float(w.get("top_glb_mm") or roof_top)
    s = prism(wall_rect(w), z0, z1)
    r = _covering_roof(roofs, w)
    return keep_below(s, roof_surfaces(r)[0]) if r is not None and r.get("bottom_plane") else s


def _roof_items(r: dict, overhang: float, cut: Polygon | None) -> list[tuple[str, str, str, Manifold]]:
    """1つの屋根の、辺ごとの軒の帯と屋根全体。"""
    rb, rt = roof_surfaces(r)                      # 面が複数の屋根も（MultiPlane）
    slab = slab_outline(r)
    slab = slab.buffer(overhang, join_style="mitre") if overhang else slab
    if cut is not None:
        slab = slab.difference(cut)
    if slab.is_empty:
        return []
    x0, y0, x1, y1 = slab.bounds
    corners = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
    z_lo = min(plane_z(rb, x, y) for x, y in corners) - 200
    z_hi = max(plane_z(rt, x, y) for x, y in corners) + 200
    eave = r.get("eave_mm") or {}
    bands = {}
    for side, (dx, dy) in SIDE_DIRS.items():
        if side not in eave:
            continue
        width = EAVE_BAND_MM + overhang + max(float(eave[side]), 0.0)
        band = slab.difference(translate(slab, -dx * width, -dy * width))   # その向きの縁から width までの帯
        if not band.is_empty and band.area > 1.0:
            bands[side] = band
    for side in ("W", "E"):   # 角は南・北の帯に入れる
        if side in bands:
            bands[side] = bands[side].difference(unary_union([bands[s] for s in ("S", "N") if s in bands]))
    out = []
    for side, band in bands.items():
        if band.is_empty or band.area <= 1.0:
            continue
        solid = keep_above(keep_below(prism(band.buffer(PROUD_MM, join_style="mitre"), z_lo, z_hi), rt, PROUD_MM), rb, -PROUD_MM)
        out.append(("roof_edges", f"{r['id']}:{side}", "eave", solid))
    body = slab.difference(unary_union(list(bands.values()))) if bands else slab
    if not body.is_empty and body.area > 1.0:
        out.append(("roofs", r["id"], "roof", keep_above(keep_below(prism(body, z_lo, z_hi), rt), rb)))
    return out


def proxies(b: dict, rules: dict) -> dict:
    """部材ごとの当たり判定用の形。{"scale": 50, "items": [{id, group, kind, level, parts, verts, tris}]}"""
    m = rules["model"]
    scale = float(m["scale"])
    overhang = float(m.get("roof_overhang_mm", 0.0)) * scale
    clearance = float(m.get("fit_clearance_mm", 0.0)) * scale
    levels = b.get("levels", [])
    multi = len(levels) > 1
    roofs = [r for r in b.get("roofs", []) if r.get("id") and r.get("top_plane") and r.get("bottom_plane") and r.get("footprint")]
    roof = b.get("roof") or {}
    roof_top = float((roof.get("top_z_range_mm") or [0, 10000])[1]) + 200
    walls = {w["id"]: w for w in b.get("walls", [])}
    items = []

    def add(group: str, mid: str, kind: str, level, parts: list[str], solid: Manifold) -> None:
        if solid.is_empty():
            return
        verts, tris = _mesh(solid)
        items.append({"id": mid, "group": group, "kind": kind, "level": level, "parts": parts, "verts": verts, "tris": tris})

    for w in walls.values():
        add("walls", w["id"], w["kind"], w.get("level"), _wall_parts(w, multi), _wall_solid(w, roofs, roof_top))
    for o in b.get("openings", []):
        w = walls.get(o["wall"])
        if w is None:
            continue
        rect = wall_rect(w, extra=40, t0=o["offset_mm"], t1=o["offset_mm"] + o["width_mm"])
        add("openings", o["id"], o["kind"], o.get("level", w.get("level")), _wall_parts(w, multi),
            prism(rect, float(o["sill_mm"]), float(o["head_mm"])))
    upper = Polygon(levels[1]["outline"]) if multi and levels[1].get("outline") else None
    for r in roofs:
        lean = r.get("kind") == "lean_to"
        cut = upper.buffer(clearance, join_style="mitre") if lean and upper is not None else None   # 組み立てと同じく2階の所は除く
        parts = ["roof_lower"] if lean else ["roof"]
        for group, mid, kind, solid in _roof_items(r, overhang, cut):
            add(group, mid, kind, r.get("level"), parts, solid)
    return {"scale": scale, "items": items}
