"""囲んで直す（SPEC §9-3）の「囲みの中の部材」をコードで決める（Claude は使わない）。

平面図（select）：人が描いた囲み（平面の mm の多角形）と、壁・開口の形の重なりで決める。
- ★11/4 作者「一部だけ囲んだ部材が選ばれない。触れるだけじゃダメ？」→ **少しでもかかった部材を選ぶ**
  （壁は厚みのある形、窓・ドアは幅×壁の厚みの形が、囲みと TOUCH_MM2 以上重なれば）。
  全部が囲みに入っていない部材は partial（一部だけ）として返す。要らない部材は画面の札の × で外す。
3D（select_hits）：画面が部材ごとの当たり判定用の形（core/pick.py）を色で塗り、「囲みの中に見えている画素の数」を送る。
- 囲みの中に 30 画素以上見えている部材を選ぶ。見えている部分の半分未満しか囲みに入っていなければ partial。
- 屋根は辺ごとの軒（Y1:S など）と屋根全体（Y1）。軒が選ばれたら、その屋根全体は外す。
番号（select_ids）：★11/5 夜 複数を囲む・文字で直す。画面が囲みごとに選んだ部材の番号を送り、家の部材にあるかを確かめる。
部品（part_hint）：その部材が模型のどの部品に入るかの短い説明（Claude と画面への手がかり。決めるのは組み立て core/builder.py）。
"""
from __future__ import annotations

import re

from shapely.geometry import LineString, Point, Polygon

TOUCH_MM2 = 1000.0          # これ以上重なれば「かかった」とみなす（約 30×30mm。囲みの線がかすっただけは選ばない）
FULL_SHARE = 0.99           # 形のこの割合以上が囲みの中なら「全部入っている」
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


def _wall_shape(w: dict):
    line = LineString([w["start"], w["end"]])
    return line, line.buffer(float(w["thickness_mm"]) / 2, cap_style="flat")


def _opening_shape(o: dict, w: dict):
    """窓・ドアの平面の形：壁の上の幅の区間 × 壁の厚み（平面図の札と同じく、厚みに少し足す）。"""
    line = LineString([w["start"], w["end"]])
    if line.length <= 0:
        return Point(o["center"]).buffer(50)
    a = line.interpolate(float(o["offset_mm"]))
    b = line.interpolate(min(float(o["offset_mm"]) + float(o["width_mm"]), line.length))
    return LineString([a, b]).buffer((float(w["thickness_mm"]) + 40) / 2, cap_style="flat")


def _touch(shape, poly) -> tuple[bool, bool]:
    """（かかっているか, 一部だけか）。"""
    if shape.is_empty or shape.area <= 0:
        return False, False
    inside = shape.intersection(poly).area
    return inside >= min(TOUCH_MM2, 0.5 * shape.area), inside < FULL_SHARE * shape.area


def select(building: dict, points: list[list[float]], level: str | None = None) -> dict:
    """囲みにかかった部材の番号。{"walls": [...], "openings": [...], "partial": [一部だけの番号]}
    level を渡すと、その階の部材だけ（2階建てで、同じ位置の1階と2階の壁が両方選ばれないように）。"""
    poly = lasso_polygon(points)
    walls, openings, partial = [], [], []
    candidates = [w for w in building.get("walls", []) if level is None or w.get("level") == level]
    for w in candidates:
        line, shape = _wall_shape(w)
        if line.length <= 0:
            continue
        hit, part = _touch(shape, poly)
        if hit:
            walls.append(w["id"])
            if part:
                partial.append(w["id"])
    by_id = {w["id"]: w for w in candidates}
    for o in building.get("openings", []):
        w = by_id.get(o["wall"])
        if w is None:
            continue
        hit, part = _touch(_opening_shape(o, w), poly)
        if hit or poly.contains(Point(o["center"])):
            openings.append(o["id"])
            if part:
                partial.append(o["id"])
    return {"walls": walls, "openings": openings, "partial": partial}


def keep_only(selection: dict, keep: list[str] | None) -> dict:
    """画面で札の × を押して外した部材を除く。囲みから選ばれた部材の中にない番号は足さない（決めるのはコード）。"""
    if keep is None:
        return selection
    k = set(keep)
    return {g: ([x for x in v if x in k] if isinstance(v, list) else v) for g, v in selection.items()}


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
    partial = []
    for h in hits:
        mid = str(h.get("id", ""))
        if mid not in groups:
            raise ValueError(f"{mid} という部材はありません。画面を読み込み直してください")
        inside, total = int(h.get("inside", 0)), max(int(h.get("total", 0)), 1)
        if inside < HIT_MIN_PX:
            continue
        out[groups[mid]].append(mid)                       # ★11/4：少しでも見えて囲みにかかれば選ぶ（要らなければ札の × で外す）
        if inside / total < HIT_VISIBLE_SHARE and not (mask_px > 0 and inside / mask_px >= HIT_LASSO_SHARE):
            partial.append(mid)
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
    res = {k: sorted(set(v), key=_natural) for k, v in out.items()}
    chosen = {x for v in res.values() for x in v}
    res["partial"] = sorted({x for x in partial if x in chosen}, key=_natural)
    return res


def select_ids(building: dict, ids: list[str], partial: list[str] | None = None, strict: bool = True) -> dict:
    """画面が送った部材の番号（囲みごとに選んだもの）を、select と同じ形にする。番号は家の部材にあるものだけ。
    strict：知らない番号があれば ValueError（囲んで直す）。False なら黙って外す（文字で直すに添える、選んでいる部材）。"""
    groups = member_groups(building)
    out = {"walls": [], "openings": [], "roof_edges": [], "roofs": []}
    for mid in dict.fromkeys(str(i) for i in (ids or [])):          # 同じ番号は1つに（順番はそのまま）
        if mid not in groups:
            if strict:
                raise ValueError(f"{mid} という部材はありません。画面を読み込み直して、もう一度囲んでください")
            continue
        out[groups[mid]].append(mid)
    chosen = {x for v in out.values() for x in v}
    out["partial"] = [x for x in (partial or []) if x in chosen]
    return out


# 組み立ての記録（model/checks.json の部品の notes）に「（W12・W13）は屋根と一緒に印刷する」と書かれた部材は、屋根の部品
_ROOF_NOTE = re.compile(r"（([A-Z]\d+(?:・[A-Z]\d+)*)）は(?:下屋の)?屋根と一緒に印刷する")


def roof_ids_from_checks(checks: dict | None) -> set[str]:
    """組み立ての結果から分かる「屋根の部品に入った壁」の番号（袖壁・垂れ壁・下がり壁など）。読めなければ空。"""
    out: set[str] = set()
    for key, info in ((checks or {}).get("parts") or {}).items():
        if not str(key).startswith("roof") or not isinstance(info, dict):
            continue
        for note in info.get("notes") or []:
            for m in _ROOF_NOTE.finditer(str(note)):
                out.update(m.group(1).split("・"))
    return out


def part_hint(building: dict, kind: str, el: dict, roof_ids: set[str] | frozenset = frozenset()) -> str:
    """部材が入る模型の部品の短い説明。決め打ちを避けるため、組み立ての記録で屋根の部品と分かる壁だけ「屋根」と言い切り、
    ほかは会社ルールで変わる所（天井の高さで分けるか）をぼかして書く（★11/5 夜：袖壁の振り分けは組み立ての担当が変える）。"""
    multi = len(building.get("levels", [])) > 1
    if kind in ("eave", "roof"):
        return "下屋の屋根" if el.get("kind") == "lean_to" else ("大屋根" if multi else "屋根")
    if el.get("id") in roof_ids:
        return "屋根（組み立ての記録から）"
    lv = el.get("level") or "1F"
    if el.get("kind") == "interior":
        return ("2階" if lv == "2F" else "1階") if multi else "家（1階の床・内壁）"
    if not multi:
        return "家（天井の高さで分けるときは、上は屋根）"
    return "2階（天井の高さより上は大屋根）" if lv == "2F" else "1階（下屋の下は天井の高さで分け、上は下屋の屋根）"


def describe(building: dict, selection: dict, roof_ids: set[str] | frozenset = frozenset()) -> list[dict]:
    """囲んだ部材の一覧（画面と Claude への説明に使う）。値は building のまま。
    part：その部材が入る模型の部品（roof_ids は組み立ての記録から分かる屋根の部品の壁。roof_ids_from_checks）。"""
    rows = []
    part = set(selection.get("partial") or [])
    walls = {w["id"]: w for w in building.get("walls", [])}
    for wid in selection.get("walls", []):
        w = walls[wid]
        length = LineString([w["start"], w["end"]]).length
        rows.append({"id": wid, "kind": w["kind"], "label": KINDS.get(w["kind"], w["kind"]),
                     "text": f"厚み {w['thickness_mm']:g}mm・長さ {length:.0f}mm・下端 {w['bottom_mm']}",
                     "part": part_hint(building, "wall", w, roof_ids)})
    ops = {o["id"]: o for o in building.get("openings", [])}
    for oid in selection.get("openings", []):
        o = ops[oid]
        w = walls.get(o["wall"])
        rows.append({"id": oid, "kind": o["kind"], "label": KINDS.get(o["kind"], o["kind"]),
                     "text": f"壁 {o['wall']}・幅 {o['width_mm']:g}mm・FL+{o['sill_fl_mm']}〜FL+{o['head_fl_mm']}",
                     "part": f"壁 {o['wall']} と同じ" + (f"（{part_hint(building, 'wall', w, roof_ids)}）" if w else "")})
    roofs = {r["id"]: r for r in building.get("roofs", []) if r.get("id")}
    for eid in selection.get("roof_edges", []):
        rid, _, side = eid.partition(":")
        r = roofs[rid]
        eave, gutter = (r.get("eave_mm") or {}).get(side), (r.get("eave_with_gutter_mm") or {}).get(side)
        g = f"（樋を含めると {gutter:g}mm）" if gutter is not None and eave is not None and abs(gutter - eave) >= 1 else ""
        rows.append({"id": eid, "kind": "eave", "label": f"軒（{SIDE_JA.get(side, side)}）",
                     "text": f"屋根 {rid}（{_roof_kind(building, r)}）の{SIDE_JA.get(side, side)}の軒：軒の出 {eave:g}mm{g}。"
                             f"軒の出は外壁の外の面から屋根の先まで（板金・樋を除く）。模型は板金の分を外に足す。"
                             f"直すときは target={rid}・field=eave_mm.{side}",
                     "part": part_hint(building, "eave", r, roof_ids)})
    for rid in selection.get("roofs", []):
        r = roofs[rid]
        e = r.get("eave_mm") or {}
        eaves = "・".join(f"{SIDE_JA[s]} {e[s]:g}" for s in "WESN" if s in e)
        rows.append({"id": rid, "kind": "roof", "label": KINDS["roof"],
                     "text": f"{_roof_kind(building, r)}・勾配 {'/'.join(f'{x:g}' for x in r.get('pitch', []))}・"
                             f"低い側 {SIDE_JA.get(r.get('low_side'), r.get('low_side'))}・軒の出（mm）{eaves}",
                     "part": part_hint(building, "roof", r, roof_ids)})
    for r in rows:
        r["partial"] = r["id"] in part                     # 囲みに一部だけかかった部材（画面では薄く、Claude には印を付ける）
    return rows
