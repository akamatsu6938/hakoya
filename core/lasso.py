"""囲んで直す（SPEC §9-3）の「囲みの中の部材」をコードで決める（Claude は使わない）。

平面図で人が描いた囲み（平面の mm の多角形）と、壁・開口の重なりで決める。
- 壁：中心線のうち囲みの中にある長さが、壁の長さの半分以上か 1,000mm 以上（長い壁の一部だけを囲んでも選べる。
  囲みの縁にかかっただけの、直交する壁は選ばない）。
- 開口（窓・ドア）：中心が囲みの中。
"""
from __future__ import annotations

from shapely.geometry import LineString, Point, Polygon

WALL_MIN_INSIDE_MM = 1000
WALL_MIN_INSIDE_RATIO = 0.5
KINDS = {"exterior": "外壁", "interior": "内壁", "standalone": "単独の壁",
         "window": "窓", "door": "ドア", "entrance_door": "玄関ドア"}


def lasso_polygon(points: list[list[float]]) -> Polygon:
    """囲みの点の並びを多角形にする。自分と交わる囲み（8の字など）は直す。"""
    if len(points) < 3:
        raise ValueError("囲みの点が足りません。ぐるっと囲んでください")
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area < 100 * 100:
        raise ValueError("囲みが小さすぎます。もう少し大きく囲んでください")
    return poly


def select(building: dict, points: list[list[float]]) -> dict:
    """囲みの中の部材の番号。{"walls": [...], "openings": [...]}"""
    poly = lasso_polygon(points)
    walls = []
    for w in building.get("walls", []):
        line = LineString([w["start"], w["end"]])
        if line.length <= 0:
            continue
        inside = line.intersection(poly).length
        if inside >= WALL_MIN_INSIDE_RATIO * line.length or inside >= WALL_MIN_INSIDE_MM:
            walls.append(w["id"])
    openings = [o["id"] for o in building.get("openings", []) if poly.contains(Point(o["center"]))]
    return {"walls": walls, "openings": openings}


def describe(building: dict, selection: dict) -> list[dict]:
    """囲んだ部材の一覧（画面と Claude への説明に使う）。値は building のまま。"""
    rows = []
    walls = {w["id"]: w for w in building.get("walls", [])}
    for wid in selection.get("walls", []):
        w = walls[wid]
        length = LineString([w["start"], w["end"]]).length
        rows.append({"id": wid, "kind": w["kind"], "label": KINDS.get(w["kind"], w["kind"]),
                     "text": f"厚み {w['thickness_mm']:g}mm・長さ {length:.0f}mm・下端 {w['bottom_mm']}"})
    ops = {o["id"]: o for o in building.get("openings", [])}
    for oid in selection.get("openings", []):
        o = ops[oid]
        rows.append({"id": oid, "kind": o["kind"], "label": KINDS.get(o["kind"], o["kind"]),
                     "text": f"壁 {o['wall']}・幅 {o['width_mm']:g}mm・FL+{o['sill_fl_mm']}〜FL+{o['head_fl_mm']}"})
    return rows
