"""形の検査（コード。Claude を使わない）：組み立てる前の家（人の修正を当てた building）から、見た目や CAD の入れ忘れの指摘を作る。
core/findings.py の code_findings が呼ぶ。★11/5 作者の言葉から：
1. 外壁の外の面が、ファジースキン（ざらざら）の層から外れている（「下の部分が塗られていないと明らかにおかしい」）
2. 地面から立つ外壁・単独の壁（袖壁）の下に、glb の基礎がない（「上に壁があるのに、下に基礎がないのもあり得ない」）
3. 家の外に立つ外壁が、1階の部品と屋根の部品に分かれている（袖壁は家の部品にまとめた。念のための見張り）
数値（ずれの mm・長さ）はコードが計算する。指摘の形は core/findings.py の code_findings と同じ。
"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

KIND_JA = {"exterior": "外壁", "standalone": "単独の壁"}
FACE_TOL_MM = 1.0           # 外の面のずれがこれ以下なら、ざらざらの層の中とみなす（座標の丸め）
FOOTING_TOL_MM = 30.0       # glb の基礎の形を、この分だけ広げて「乗っている」を見る（基礎の形の丸め・幅の違い）
FOOTING_MIN_RATIO = 0.5     # 壁の足もとのうち、基礎に乗っている割合がこれ未満なら「基礎がない」
OUTSIDE_MIN_MM = 300.0      # 家の外の所がこれより短い外壁は、分かれ方を見ない（角の重なりなど）


def _side(w: dict, center) -> str:
    from core.lintel import _side_of_wall
    return _side_of_wall(w, center)


def _level_info(b: dict, rules: dict) -> tuple[float, float | None, float]:
    """（1階の FL、天井の高さの継ぎ目（分けないなら None）、基礎の天端）。"""
    lv = (b.get("levels") or [{}])[0]
    fl = float(lv.get("fl_mm", 0.0))
    m = rules.get("model") or {}
    ceiling = float(lv.get("ceiling_mm", (rules.get("levels") or {}).get("ceiling_height_mm", 2400)))
    z_cut = fl + ceiling if m.get("split_at_ceiling") else None
    ftop = float((b.get("foundation") or {}).get("top_mm", (b.get("ground") or {}).get("foundation_top_mm", 0.0)))
    return fl, z_cut, ftop


def fuzzy_face_findings(b: dict, rules: dict, enabled: bool | None = None) -> list[dict]:
    """外壁の外の面が、ざらざらの層（基礎の外形の内側の帯。model.fuzzy_skin_band_mm）の中にない外壁。
    ★11/5 デモ用：外壁の厚みを 162→180 に直したとき、中心線のまわりに厚くしたため外の面が 9mm 外へ出て、南の外壁の1階の面が
    全部ざらざらでなくなった（今は外の面を動かさず内側へ厚くする。core/overrides.py）。ざらざらにする物件だけ見る：
    enabled は物件の外壁の仕上げ（core/print_options.py。★11/6）。None なら会社ルール（塗り壁の会社だけ）。"""
    from core.builder import UPPER_WALL_ABOVE_FL, outside_house
    from core.overrides import _inward
    m = rules.get("model") or {}
    on = m.get("exterior_finish") == "plaster" if enabled is None else bool(enabled)
    if not on or not m.get("fuzzy_skin_band_mm"):
        return []
    band_mm = float(m["fuzzy_skin_band_mm"])
    band = band_mm * float(m.get("scale", 50))
    levels = b.get("levels") or []
    found = Polygon((b.get("foundation") or {}).get("outline") or [])
    if found.is_empty or not levels:
        return []
    center = found.centroid.coords[0]
    out = []
    for w in b.get("walls", []):
        if w.get("kind") != "exterior" or "start" not in w or "end" not in w:
            continue
        idx = next((i for i, lv in enumerate(levels) if lv.get("name") == w.get("level")), 0)
        fl = float(levels[idx]["fl_mm"])
        if float(w.get("bottom_mm", 0)) >= fl + UPPER_WALL_ABOVE_FL:
            continue                                    # 途中から始まる壁（屋根の部品の外壁。まるごとざらざら）
        house = Polygon(levels[idx]["outline"]) if idx > 0 and levels[idx].get("outline") else found
        if idx == 0 and outside_house(w, found):
            continue                                    # 家の外に立つ外壁は 3. で見る
        inward = _inward(b, w)
        if inward is None:
            continue
        s, e = np.array(w["start"], float), np.array(w["end"], float)
        face = -np.array(inward) * float(w["thickness_mm"]) / 2
        depths = []                                     # 外の面の、家の外形からの深さ（＋は内側、−は外）
        for t in (0.25, 0.5, 0.75):
            p = Point(*(s + (e - s) * t + face))
            d = house.exterior.distance(p)
            depths.append(d if house.contains(p) else -d)
        depth = float(np.median(depths))
        if -FACE_TOL_MM <= depth <= band - FACE_TOL_MM:
            continue
        where = f"基礎の外形から {abs(depth):.0f}mm 外" if depth < 0 else f"基礎の外形から {depth:.0f}mm 内側（ざらざらの層より奥）"
        side = _side(w, center)
        out.append({"view": side, "kind": "print", "severity": "中",
                    "title": f"外壁 {w['id']} がざらざらにならない",
                    "message": f"外壁 {w['id']} の外の面が{where}にあり、ざらざらになりません（ざらざらにするのは、家の外形の内側の "
                               f"{band_mm:g}mm（模型）の層だけ）。立面の絵で、この外壁の広い所が塗られていない色になります",
                    "targets": [w["id"]], "evidence": ["コードの検査（ざらざらの層）"], "source": "code"})
    return out


def missing_footing_findings(b: dict, rules: dict) -> list[dict]:
    """地面から立つ外壁・単独の壁（袖壁など）の足もとが、glb の基礎（基礎の外形・袖壁などの下の基礎 footings）にほとんど乗っていない。
    内壁は見ない。模型では、家の外に立つ壁の下に基礎を足している（core/builder.py）。"""
    from core.builder import UPPER_WALL_ABOVE_FL, wall_rect
    found = b.get("foundation") or {}
    levels = b.get("levels") or []
    if not found.get("outline") or not levels:
        return []
    fl, _, _ = _level_info(b, rules)
    support = unary_union([Polygon(found["outline"])] + [Polygon(f["polygon"]) for f in found.get("footings") or []
                                                          if len(f.get("polygon") or []) >= 3])
    support = support.buffer(FOOTING_TOL_MM, join_style="mitre")
    center = Polygon(found["outline"]).centroid.coords[0]
    first = levels[0].get("name")
    out = []
    for w in b.get("walls", []):
        if w.get("kind") not in KIND_JA or "start" not in w or "end" not in w:
            continue
        if w.get("level", first) != first or float(w.get("bottom_mm", 0)) >= fl + UPPER_WALL_ABOVE_FL:
            continue                                    # 2階の壁・途中から始まる壁（梁・垂れ壁）は地面から立たない
        r = wall_rect(w)
        if r.area <= 0 or r.intersection(support).area / r.area >= FOOTING_MIN_RATIO:
            continue
        side = _side(w, center)
        out.append({"view": side, "kind": "shape", "severity": "中",
                    "title": f"{w['id']} の下に glb の基礎がない",
                    "message": f"{KIND_JA[w['kind']]} {w['id']} の下に glb の基礎がありません。CAD で基礎の入れ忘れかもしれません"
                               "（模型では基礎を足しています）",
                    "targets": [w["id"]], "evidence": ["コードの検査（glb の基礎）", found.get("source") or "glb"], "source": "code"})
    return out


def split_outside_findings(b: dict, rules: dict) -> list[dict]:
    """家の外に立つ外壁が、1階の部品と屋根の部品に分かれている（または屋根の部品にまるごと入っている）。平屋だけ。
    ★11/5：袖壁（家の外の単独の壁）は1階の部品にまとめ、家の角を越えて袖壁と重なる外壁は家の角で止めるので、ふつうは出ない。
    袖壁で覆われていない外壁の延び（glb で外壁だけが家の外まで伸びている所）を知らせる。"""
    from core.builder import REACH_ROOF_MM, UPPER_WALL_ABOVE_FL, _wall_top, outside_house, plane_z, wall_rect
    from core.roof_geom import surfaces
    found = b.get("foundation") or {}
    levels = b.get("levels") or []
    if len(levels) != 1 or not found.get("outline"):
        return []
    outline = Polygon(found["outline"])
    fl, z_cut, _ = _level_info(b, rules)
    try:
        rb = surfaces(b["roof"])[0]
    except Exception:  # noqa: BLE001  屋根の形を読めない家は、屋根まで届くかを見ない
        rb = None
    center = outline.centroid.coords[0]
    out = []
    for w in b.get("walls", []):
        if w.get("kind") != "exterior" or "start" not in w or "end" not in w or float(w.get("bottom_mm", 0)) >= fl + UPPER_WALL_ABOVE_FL:
            continue
        r = wall_rect(w)
        rest = r.difference(outline)
        t = float(w["thickness_mm"]) or 1.0
        if rest.area < OUTSIDE_MIN_MM * t:
            continue
        top = _wall_top(w)
        mid = (np.array(w["start"], float) + np.array(w["end"], float)) / 2
        whole = outside_house(w, outline) and (top is None or (rb is not None and top >= plane_z(rb, *mid) - REACH_ROOF_MM))
        split = z_cut is not None and (top is None or top > z_cut + 1.0)
        if not (whole or split):
            continue
        length = rest.area / t
        how = "屋根の部品にまるごと入っています" if whole else "天井の高さで、1階の部品と屋根の部品に分かれています"
        side = _side(w, center)
        out.append({"view": side, "kind": "print", "severity": "中",
                    "title": f"家の外の外壁 {w['id']} が屋根の部品に入る",
                    "message": f"外壁 {w['id']} の家の外の所（約 {length:.0f}mm）が、{how}（家の外に立つ壁の一部が屋根に付く）。"
                               "袖壁なら家の部品に入るはず。glb で外壁だけが家の外まで伸びていないか確かめてください",
                    "targets": [w["id"]], "evidence": ["コードの検査（部品の分かれ方）"], "source": "code"})
    return out


def shape_findings(b: dict, rules: dict, fuzzy: bool | None = None) -> list[dict]:
    """形の検査の指摘をまとめて返す（b は人の修正を当てた家。fuzzy は外壁をざらざらにするか。None なら会社ルール）。"""
    return fuzzy_face_findings(b, rules, fuzzy) + missing_footing_findings(b, rules) + split_outside_findings(b, rules)
