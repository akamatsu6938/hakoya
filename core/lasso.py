"""囲んで直す（SPEC §9-3）の「囲みの中の部材」をコードで決める（Claude は使わない）。

平面図（select）：人が描いた囲み（平面の mm の多角形）と、壁・開口の重なりで決める。
- 壁：中心線のうち囲みの中にある長さが、壁の長さの半分以上か 1,000mm 以上（長い壁の一部だけを囲んでも選べる。
  囲みの縁にかかっただけの、直交する壁は選ばない）。
- 開口（窓・ドア）：中心が囲みの中。
3D（select_hits）：画面が部材ごとの当たり判定用の形（core/pick.py）を色で塗り、「囲みの中に見えている画素の数」を送る。
- 囲みの中に 30 画素以上見えていて、見えている部分の半分以上が囲みの中か、囲みの広さの 2割以上を占める部材を選ぶ。
- 屋根は辺ごとの軒（Y1:S など）と屋根全体（Y1）。軒が選ばれたら、その屋根全体は外す。
"""
from __future__ import annotations

from shapely.geometry import LineString, Point, Polygon

WALL_MIN_INSIDE_MM = 1000
WALL_MIN_INSIDE_RATIO = 0.5
KINDS = {"exterior": "外壁", "interior": "内壁", "standalone": "単独の壁",
         "window": "窓", "door": "ドア", "entrance_door": "玄関ドア", "eave": "軒", "roof": "屋根全体"}
SIDE_JA = {"W": "西", "E": "東", "S": "南", "N": "北"}
HIT_MIN_PX = 30            # 囲みの中に見えている画素がこれ未満の部材は選ばない
HIT_VISIBLE_SHARE = 0.5    # 見えている部分のうち囲みの中の割合
HIT_LASSO_SHARE = 0.2      # 囲みの広さのうち、その部材が占める割合（長い壁の一部だけを囲んだとき）
EDGE_MINOR_SHARE = 0.25    # 同じ屋根でいちばん多く写っている軒に比べて、これ未満しか写っていない軒は外す


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


def select(building: dict, points: list[list[float]], level: str | None = None) -> dict:
    """囲みの中の部材の番号。{"walls": [...], "openings": [...]}
    level を渡すと、その階の部材だけ（2階建てで、同じ位置の1階と2階の壁が両方選ばれないように）。"""
    poly = lasso_polygon(points)
    walls = []
    candidates = [w for w in building.get("walls", []) if level is None or w.get("level") == level]
    for w in candidates:
        line = LineString([w["start"], w["end"]])
        if line.length <= 0:
            continue
        inside = line.intersection(poly).length
        if inside >= WALL_MIN_INSIDE_RATIO * line.length or inside >= WALL_MIN_INSIDE_MM:
            walls.append(w["id"])
    level_walls = {w["id"] for w in candidates}
    openings = [o["id"] for o in building.get("openings", []) if o["wall"] in level_walls and poly.contains(Point(o["center"]))]
    return {"walls": walls, "openings": openings}


def _roof_kind(building: dict, r: dict) -> str:
    if r.get("kind") == "lean_to":
        return "下屋の屋根"
    return "大屋根" if len(building.get("levels", [])) > 1 else "屋根"


def member_groups(building: dict) -> dict[str, str]:
    """3D で選べる部材の番号と、その仲間（walls / openings / roof_edges / roofs）。"""
    out = {w["id"]: "walls" for w in building.get("walls", [])}
    out.update({o["id"]: "openings" for o in building.get("openings", [])})
    for r in building.get("roofs", []):
        if not r.get("id"):
            continue
        out[r["id"]] = "roofs"
        out.update({f"{r['id']}:{s}": "roof_edges" for s in (r.get("eave_mm") or {})})
    return out


def _natural(mid: str):
    head, _, side = mid.partition(":")
    return (head[:1], int(head[1:]) if head[1:].isdigit() else 0, side)


def select_hits(building: dict, hits: list[dict], mask_px: int) -> dict:
    """3D の囲みの中の部材の番号。hits は部材ごとの {id, inside（囲みの中に見えている画素）, total（画面全体で見えている画素）}。"""
    groups = member_groups(building)
    out = {"walls": [], "openings": [], "roof_edges": [], "roofs": []}
    for h in hits:
        mid = str(h.get("id", ""))
        if mid not in groups:
            raise ValueError(f"{mid} という部材はありません。画面を読み込み直してください")
        inside, total = int(h.get("inside", 0)), max(int(h.get("total", 0)), 1)
        if inside < HIT_MIN_PX:
            continue
        if inside / total >= HIT_VISIBLE_SHARE or (mask_px > 0 and inside / mask_px >= HIT_LASSO_SHARE):
            out[groups[mid]].append(mid)
    # 同じ屋根の軒のうち、いちばん多く写っている軒の 1/4 に満たない軒は外す
    # （正面から南の軒を囲むと、東・西の軒の端だけが囲みに入って選ばれてしまうため。斜めから角を囲めば両方残る）
    inside_of = {str(h.get("id")): int(h.get("inside", 0)) for h in hits}
    best = {}
    for e in out["roof_edges"]:
        rid = e.split(":")[0]
        best[rid] = max(best.get(rid, 0), inside_of.get(e, 0))
    out["roof_edges"] = [e for e in out["roof_edges"] if inside_of.get(e, 0) >= EDGE_MINOR_SHARE * best[e.split(":")[0]]]
    with_edges = {e.split(":")[0] for e in out["roof_edges"]}
    out["roofs"] = [r for r in out["roofs"] if r not in with_edges]
    return {k: sorted(set(v), key=_natural) for k, v in out.items()}


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
    roofs = {r["id"]: r for r in building.get("roofs", []) if r.get("id")}
    for eid in selection.get("roof_edges", []):
        rid, _, side = eid.partition(":")
        r = roofs[rid]
        eave, gutter = (r.get("eave_mm") or {}).get(side), (r.get("eave_with_gutter_mm") or {}).get(side)
        g = f"（樋を含めると {gutter:g}mm）" if gutter is not None and eave is not None and abs(gutter - eave) >= 1 else ""
        rows.append({"id": eid, "kind": "eave", "label": f"軒（{SIDE_JA.get(side, side)}）",
                     "text": f"屋根 {rid}（{_roof_kind(building, r)}）の{SIDE_JA.get(side, side)}の軒：軒の出 {eave:g}mm{g}。"
                             f"軒の出は外壁の外の面から屋根の先まで（板金・樋を除く）。模型は板金の分を外に足す。"
                             f"直すときは target={rid}・field=eave_mm.{side}"})
    for rid in selection.get("roofs", []):
        r = roofs[rid]
        e = r.get("eave_mm") or {}
        eaves = "・".join(f"{SIDE_JA[s]} {e[s]:g}" for s in "WESN" if s in e)
        rows.append({"id": rid, "kind": "roof", "label": KINDS["roof"],
                     "text": f"{_roof_kind(building, r)}・勾配 {'/'.join(f'{x:g}' for x in r.get('pitch', []))}・"
                             f"低い側 {SIDE_JA.get(r.get('low_side'), r.get('low_side'))}・軒の出（mm）{eaves}"})
    return rows
