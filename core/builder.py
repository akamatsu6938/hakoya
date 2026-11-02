"""building.json（＋人の修正）から、模型のパーツの立体を作る（SPEC §8）。

座標は実寸 mm・glb の向き（Z 上）のまま作り、書き出すときに 1/50 にする（core/export.py）。
模型寸法で決まっている値（最小の太さ・三角の大きさ・はめあいの隙間）は、ここで実寸に直して使う。

パーツ（平屋）：
- foundation  基礎：外周の帯＋内壁の下の帯（中は空ける）、ポーチ・段、玄関の土間、袖壁などの下の基礎
- floor1      1階：床の板＋内壁（外壁と重なる所は除く）
- exterior1   1階の外壁（ファジースキンを効かせるため別の部品。1階と同じオブジェクトに入れる）
- roof        屋根：屋根板＋ずれ止めの三角＋ポーチの上の垂れ壁・屋根まで届く袖壁（★仮の決め：作者に確認）
- soffit      ポーチの天井：平らな板
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from manifold3d import CrossSection, FillRule, Manifold, OpType
from shapely.geometry import Polygon
from shapely.ops import unary_union

UPPER_WALL_ABOVE_FL = 1500.0   # 下端がこれより上の壁は「途中から始まる壁」（ポーチの上の垂れ壁など）
DOMA_UNDER_WALL_MM = 100.0     # 土間を壁の下まで広げる幅（実寸）
GAP_FILL_MM = 40.0             # ポーチの天井の板で埋める細いすき間の幅（実寸）


@dataclass
class ModelPart:
    key: str
    label: str
    solid: Manifold
    flip_for_print: bool = False          # 印刷のときに裏返す（屋根）
    fuzzy_skin: bool = False              # 外壁だけファジースキン
    object_key: str | None = None         # 同じオブジェクトに入れる相手（外壁 → 1階）
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 形の道具

def _cs(poly: Polygon) -> CrossSection:
    """shapely の多角形（穴あり可）→ manifold の断面。"""
    polys = getattr(poly, "geoms", [poly])
    contours = []
    for p in polys:
        if p.is_empty or p.area < 1:
            continue
        p = p.buffer(0)
        for q in getattr(p, "geoms", [p]):
            ext = list(q.exterior.coords)[:-1]
            if Polygon(ext).exterior.is_ccw is False:
                ext = ext[::-1]
            contours.append(ext)
            for hole in q.interiors:
                h = list(hole.coords)[:-1]
                if Polygon(h).exterior.is_ccw:
                    h = h[::-1]
                contours.append(h)
    return CrossSection([np.asarray(c, dtype=np.float64) for c in contours], FillRule.Positive) if contours else CrossSection()


def prism(poly: Polygon, z0: float, z1: float) -> Manifold:
    if z1 - z0 <= 0 or poly.is_empty:
        return Manifold()
    return _cs(poly).extrude(z1 - z0).translate((0.0, 0.0, z0))


def union_all(solids: list[Manifold]) -> Manifold:
    solids = [s for s in solids if not s.is_empty()]
    return Manifold.batch_boolean(solids, OpType.Add) if len(solids) > 1 else (solids[0] if solids else Manifold())


SPECK_MM3 = 1000.0   # これより小さい塊は、面の重なりでできた厚みゼロのかけらとみなして捨てる（実寸 1cm³ ＝ 模型 0.008mm³）


def drop_specks(m: Manifold, min_volume: float = SPECK_MM3) -> Manifold:
    if m.is_empty():
        return m
    bodies = m.decompose()
    kept = [x for x in bodies if abs(x.volume()) >= min_volume]
    return m if len(kept) == len(bodies) else union_all(kept)


def keep_below(m: Manifold, plane: dict, offset: float = 0.0) -> Manifold:
    """z <= a*x + b*y + c + offset の側を残す。"""
    a, b, c = plane["a"], plane["b"], plane["c"] + offset
    L = math.sqrt(a * a + b * b + 1)
    return m.trim_by_plane((a / L, b / L, -1 / L), -c / L)


def keep_above(m: Manifold, plane: dict, offset: float = 0.0) -> Manifold:
    """z >= a*x + b*y + c + offset の側を残す。"""
    a, b, c = plane["a"], plane["b"], plane["c"] + offset
    L = math.sqrt(a * a + b * b + 1)
    return m.trim_by_plane((-a / L, -b / L, 1 / L), c / L)


def plane_z(plane: dict, x: float, y: float) -> float:
    return plane["a"] * x + plane["b"] * y + plane["c"]


def wall_rect(w: dict, extra: float = 0.0, t0: float | None = None, t1: float | None = None) -> Polygon:
    """壁の長方形（t0〜t1 は始点からの距離。extra は厚みの足し分）。"""
    s, e = np.array(w["start"], float), np.array(w["end"], float)
    L = float(np.linalg.norm(e - s)) or 1.0
    d = (e - s) / L
    n = np.array([-d[1], d[0]]) * (w["thickness_mm"] + extra) / 2
    a = s + d * (0.0 if t0 is None else t0)
    b = s + d * (L if t1 is None else t1)
    return Polygon([a + n, b + n, b - n, a - n])


def _wall_top(w: dict) -> float | None:
    return None if w["height"] == "to_roof" else float(w["height"])


# ---------------------------------------------------------------- 組み立て

def build_model(b: dict, rules: dict) -> dict[str, ModelPart]:
    m = rules["model"]
    scale = float(m["scale"])
    min_feat = float(m["min_feature_mm"]) * scale                 # 実寸での最小の太さ
    clearance = float(m["fit_clearance_mm"]) * scale
    leg = float(m["corner_block_leg_mm"]) * scale
    block_h = float(m["corner_block_height_mm"]) * scale
    floor_block_h = float(m["floor_block_height_mm"]) * scale
    band = float(m["foundation_band_mm"])
    plate_t = float(m["plate_thickness_mm"]) * scale

    lv = b["levels"][0]
    fl = float(lv["fl_mm"])
    ftop = float(b["foundation"]["top_mm"])
    roof = b["roof"]
    rb, rt = roof["bottom_plane"], roof["top_plane"]
    roof_zmax = float(roof["top_z_range_mm"][1]) + 200
    outline = Polygon(b["foundation"]["outline"])
    walls = b["walls"]
    notes: dict[str, list[str]] = {"roof": [], "floor1": [], "foundation": [], "soffit": []}

    # 壁の振り分け
    def is_upper(w):
        return w["bottom_mm"] >= fl + UPPER_WALL_ABOVE_FL

    def outside_house(w):
        r = wall_rect(w)
        return r.intersection(outline).area / r.area < 0.5

    def reaches_roof(w):
        top = _wall_top(w)
        if top is None:
            return True
        mid = (np.array(w["start"]) + np.array(w["end"])) / 2
        return top >= plane_z(rb, *mid) - 50

    ext_walls, int_walls, roof_walls, std_in = [], [], [], []
    for w in walls:
        if w["kind"] in ("exterior", "standalone") and (is_upper(w) or (outside_house(w) and reaches_roof(w))):
            roof_walls.append(w)
        elif w["kind"] == "exterior":
            ext_walls.append(w)
        elif w["kind"] == "standalone":
            std_in.append(w)
        else:
            int_walls.append(w)
    openings_by_wall: dict[str, list[dict]] = {}
    for o in b.get("openings", []):
        openings_by_wall.setdefault(o["wall"], []).append(o)

    def wall_solid(w: dict, bottom: float | None = None) -> Manifold:
        z0 = float(w["bottom_mm"]) if bottom is None else bottom
        top = _wall_top(w)
        z1 = roof_zmax if top is None else top
        s = prism(wall_rect(w), z0, z1)
        if w["kind"] != "interior" or top is None:
            s = keep_below(s, rb)                                   # 外壁・袖壁は屋根の下の面で切る
        cuts = []
        for o in openings_by_wall.get(w["id"], []):
            sill, head = float(o["sill_mm"]), float(o["head_mm"])
            if sill - z0 < min_feat:                                 # 下に残る壁が細すぎる → 下まで抜く
                sill = z0 - 1
            wall_top_here = z1 if top is not None else plane_z(rb, *o["center"])
            if wall_top_here - head < min_feat:                      # 上に残る壁が細すぎる → 上まで抜く（SPEC §8）
                head = roof_zmax
            cuts.append(prism(wall_rect(w, extra=40, t0=o["offset_mm"], t1=o["offset_mm"] + o["width_mm"]), sill, head))
        return s - union_all(cuts) if cuts else s

    def window_frames(w: dict) -> list[Manifold]:
        """窓の簡略化した枠（SPEC §8）：太さは最小の太さ、奥行きは壁の厚みの半分（壁の真ん中）。
        引き違いは中央に縦桟1本、FIX は枠だけ。ガラスは入れない。"""
        out = []
        depth_extra = -w["thickness_mm"] / 2          # wall_rect の厚みの足し分（マイナスで薄くする）
        for o in openings_by_wall.get(w["id"], []):
            if o["kind"] != "window":
                continue
            a, wdt = float(o["offset_mm"]), float(o["width_mm"])
            sill, head = float(o["sill_mm"]), float(o["head_mm"])
            fw = min_feat
            if wdt < 3 * fw or head - sill < 3 * fw:
                continue
            bars = [(a, a + wdt, sill, sill + fw), (a, a + wdt, head - fw, head),          # 下枠・上枠
                    (a, a + fw, sill, head), (a + wdt - fw, a + wdt, sill, head)]          # 縦枠
            if o.get("style") == "sliding":
                mid = a + wdt / 2
                bars.append((mid - fw / 2, mid + fw / 2, sill, head))                     # 中央の縦桟
            out += [prism(wall_rect(w, extra=depth_extra, t0=t0, t1=t1), z0, z1) for t0, t1, z0, z1 in bars]
        return out

    # 天井の高さで分ける（作者指定）：外壁などの天井より上は、屋根と一緒に印刷する
    z_cut = fl + float(lv.get("ceiling_mm", rules["levels"]["ceiling_height_mm"])) if m.get("split_at_ceiling") else None

    def lower_part(s: Manifold) -> Manifold:
        return s if z_cut is None else s.trim_by_plane((0.0, 0.0, -1.0), -z_cut)

    def upper_part(s: Manifold) -> Manifold:
        return Manifold() if z_cut is None else s.trim_by_plane((0.0, 0.0, 1.0), z_cut)

    def lintel_slivers(w: dict) -> list[Manifold]:
        """継ぎ目（天井の高さ）のすぐ下に開口の上端があり、細い壁が残る所は、継ぎ目まで抜く。"""
        if z_cut is None:
            return []
        out = []
        for o in openings_by_wall.get(w["id"], []):
            head = float(o["head_mm"])
            if -1.0 < z_cut - head < min_feat:   # 上端が継ぎ目と同じ高さのときも（面が重なると厚みゼロの膜が残るため）
                out.append(prism(wall_rect(w, extra=40, t0=o["offset_mm"], t1=o["offset_mm"] + o["width_mm"]),
                                 head - 1, z_cut + 1))
        return out

    def split_wall(w: dict) -> tuple[Manifold, Manifold]:
        s = wall_solid(w)
        lo = lower_part(s)
        cuts = lintel_slivers(w)
        if cuts:
            lo = lo - union_all(cuts)
        return lo, upper_part(s)

    # 1階の外壁（ファジースキンの部品）と、天井より上の外壁（屋根のオブジェクトの部品へ）
    ext_lo, ext_up = [], []
    for w in ext_walls:
        lo, up = split_wall(w)
        ext_lo.append(lo)
        ext_up.append(up)
    exterior = union_all(ext_lo)

    # 1階：床の板（土間の部屋は抜く）＋内壁＋家の中の単独の壁。外壁と重なる所は除く
    doma = [Polygon(r["polygon"]) for r in b.get("rooms", []) if r["floor_mm"] < fl - 50]
    # 土間の穴は壁の面まで広げる（★部屋の形は glb の仕上げ面までで、作り直した壁（130）との間に床の細い帯が残った。検品 F7）
    walls_fp = unary_union([wall_rect(w) for w in walls if w["bottom_mm"] <= fl + 1]) if walls else Polygon()
    near_walls = walls_fp.buffer(DOMA_UNDER_WALL_MM, join_style="mitre")   # 壁の近くだけ広げる（框の側は広げない）
    doma_holes = [d.union(d.buffer(DOMA_UNDER_WALL_MM, join_style="mitre").intersection(near_walls)).difference(walls_fp)
                  .intersection(outline) for d in doma]
    doma_holes = [max(getattr(h, "geoms", [h]), key=lambda g: g.area) for h in doma_holes if not h.is_empty]
    floor_poly = outline.difference(unary_union(doma_holes)) if doma_holes else outline
    std_lo, std_up = [], []
    for w in std_in:
        lo, up = split_wall(w)
        std_lo.append(lo)
        std_up.append(up)
    floor1 = union_all([prism(floor_poly, ftop, fl)] + [lower_part(wall_solid(w)) for w in int_walls] + std_lo)
    floor1 = floor1 - exterior
    # 窓の枠は「床・内壁」の部品に入れる（外壁の部品に入れると、ファジースキンで枠までざらざらになるため）
    # 窓の枠は会社ルールで切り替え（★自社は作らない：1/50 では枠が柱と紛らわしく、3連窓で線が増えるため）
    frames = ([lower_part(f) for w in ext_walls + int_walls + std_in for f in window_frames(w)]
              if m.get("window_frames", True) else [])
    if frames:
        floor1 = union_all([floor1] + frames)
        notes["floor1"].append(f"窓の枠を作った（{sum(1 for o in b.get('openings', []) if o['kind'] == 'window')} か所。引き違いは中央に縦桟）")
    if doma:
        notes["floor1"].append(f"土間（床が FL より低い部屋）{len(doma)} か所は床の板を抜き、基礎のパーツに土間を作った")
    if z_cut is not None:
        notes["floor1"].append(f"外壁は天井の高さ（{z_cut:.0f}）で分けた。それより上は屋根と一緒に印刷する")

    # 基礎：外周の帯＋内壁の下の帯＋ポーチ＋土間＋袖壁などの下
    ring = outline.difference(outline.buffer(-band, join_style="mitre"))
    inner = [wall_rect(w).intersection(outline) for w in int_walls if w["bottom_mm"] <= fl + 1]
    pieces = [prism(ring.union(unary_union(inner)) if inner else ring, 0.0, ftop)]
    pieces += [prism(Polygon(p["polygon"]), 0.0, float(p["top_mm"])) for p in b.get("porches", [])]
    # 土間は壁の下まで広げて、まわりの基礎の帯とつなぐ（部屋の形は壁の仕上げ面までなので、そのままだと浮く）
    pieces += [prism(d.buffer(DOMA_UNDER_WALL_MM, join_style="mitre").intersection(outline), 0.0, float(r["floor_mm"]))
               for d, r in zip(doma, [r for r in b.get("rooms", []) if r["floor_mm"] < fl - 50])]
    for w in walls:  # 基礎の外に立つ壁（袖壁など）の下も基礎にする
        if w["kind"] in ("standalone", "exterior") and not is_upper(w) and outside_house(w):
            pieces.append(prism(wall_rect(w), 0.0, float(w["bottom_mm"])))
    foundation = union_all(pieces)

    # 基礎 → 1階の位置合わせ：基礎の帯の内側の角に上向きの三角、1階の床裏に受けのくぼみ（SPEC §8）
    inner_outline = outline.buffer(-band, join_style="mitre")
    f_corners = _pick_corners(inner_outline)
    f_blocks, f_recess = [], []
    for v, e1, e2 in f_corners:
        tri = Polygon([v, v + e1 * leg, v + e2 * leg])
        # 三角は地面から立ち上げて帯と一体にする（帯の内側は空洞なので、天端だけだと浮いてしまう）
        f_blocks.append(prism(tri, 0.0, ftop + floor_block_h))
        f_recess.append(prism(tri.buffer(clearance, join_style="mitre"), ftop - 1, ftop + floor_block_h + clearance))
    if f_blocks:
        foundation = union_all([foundation] + f_blocks)
        recess = union_all(f_recess)
        floor1 = floor1 - recess
        exterior = exterior - recess
        notes["foundation"].append(f"位置合わせの三角を {len(f_blocks)} か所（基礎の帯の内側の角）")

    # ファジースキンをかける所を、外壁の外の面の薄い層だけにする。窓・ドアのまわりは外す。
    # ★窓の高さでは外壁の輪が途切れ、窓と窓の間の細い柱が島になる。島の全周（窓の脇・部屋側の面）が
    #   「外側の輪郭」と見なされてざらざらになり、くちゅくちゅに見えた（2026-11-02 作者）。
    band_mm = m.get("fuzzy_skin_band_mm")
    if band_mm:
        band = float(band_mm) * scale
        margin = float(m.get("fuzzy_skin_opening_margin_mm", 0.0)) * scale
        house = Polygon(outline.exterior)
        skin2d = house.difference(house.buffer(-band, join_style="mitre"))
        skin = prism(skin2d, ftop - 1, (z_cut if z_cut is not None else roof_zmax) + 1)
        keep_out = []
        for w in ext_walls:
            for o in openings_by_wall.get(w["id"], []):
                keep_out.append(prism(wall_rect(w, extra=40, t0=o["offset_mm"] - margin, t1=o["offset_mm"] + o["width_mm"] + margin),
                                      float(o["sill_mm"]) - margin, float(o["head_mm"]) + margin))
        if keep_out:
            skin = skin - union_all(keep_out)
        exterior_skin = exterior ^ skin
        exterior_core = exterior - skin
        floor1 = union_all([floor1, exterior_core])      # 中身はざらざらにしない部品（1階の床・内壁と同じ部品）へ
        exterior = exterior_skin
        notes["floor1"].append(f"ファジースキンは外壁の外の面から {band_mm}mm（模型）の層だけ。窓・ドアのまわり {m.get('fuzzy_skin_opening_margin_mm')}mm は外す")

    # 屋根のオブジェクト：①屋根板（4辺を板金の分だけ出す）＋ポーチの天井＋ずれ止めの三角
    #                     ②屋根の外壁（天井より上の外壁・ポーチの垂れ壁・屋根まで届く袖壁。ファジースキン）
    overhang = float(m.get("roof_overhang_mm", 0.0)) * scale
    slab_poly = Polygon(roof["footprint"]).buffer(overhang, join_style="mitre") if overhang else Polygon(roof["footprint"])
    slab = keep_above(keep_below(prism(slab_poly, plane_z(rb, *slab_poly.centroid.coords[0]) - 2000, roof_zmax), rt), rb)
    if overhang:
        notes["roof"].append(f"屋根板を外壁から4辺とも {m['roof_overhang_mm']}mm（模型）出した（板金の分）")
    roof_parts = [slab]
    soffits = b.get("porch_soffits", [])
    soffit_top = max((float(s["z_mm"]) for s in soffits), default=None)
    if soffits:  # ポーチの天井：平らな板（下の面がポーチの天井の高さ）。垂れ壁の下まで広げ、屋根と一緒に印刷する
        sp = unary_union([Polygon(s["polygon"]) for s in soffits] + [wall_rect(w) for w in roof_walls if is_upper(w)])
        # 垂れ壁の実際の厚み（180）と外壁のルール（162）の差でできる細いすき間を埋める
        sp = sp.buffer(GAP_FILL_MM / 2, join_style="mitre").buffer(-GAP_FILL_MM / 2, join_style="mitre")
        roof_parts.append(prism(sp, soffit_top, soffit_top + plate_t))
        notes["roof"].append("ポーチの天井は屋根と一緒に印刷する（作者指定）")
    roof_ext_parts = ext_up + std_up
    for w in roof_walls:
        bottom = soffit_top + plate_t if soffit_top is not None and is_upper(w) else None   # ポーチの天井の板の上に乗る
        roof_ext_parts.append(wall_solid(w, bottom=bottom))
    if roof_walls:
        notes["roof"].append("ポーチの上の垂れ壁と屋根まで届く袖壁（" + "・".join(w["id"] for w in roof_walls) + "）は屋根と一緒に印刷する")
    if z_cut is not None:  # 天井の高さで上下に分けた壁（★検品が「W13 は屋根に入っていない」と誤解した。F3）
        split_ids = [w["id"] for w in ext_walls + std_in if (_wall_top(w) is None or _wall_top(w) > z_cut + 1)]
        if split_ids:
            notes["roof"].append("天井の高さ（" + f"{z_cut:.0f}" + "）より上の部分を屋根の外壁に入れた壁：" + "・".join(split_ids))

    # ずれ止めの三角：外壁の内側の角（四隅に近いもの）。内壁とぶつかる角は使わない（SPEC §8）
    ext_inner = unary_union([wall_rect(w) for w in ext_walls])
    house_inside = Polygon(outline.exterior).difference(ext_inner)
    house_inside = max(getattr(house_inside, "geoms", [house_inside]), key=lambda g: g.area)
    avoid = unary_union([wall_rect(w) for w in int_walls]) if int_walls else None
    r_corners = _pick_corners(house_inside, leg=leg, avoid=avoid)
    for v, e1, e2 in r_corners:
        tri = Polygon([v, v + e1 * leg, v + e2 * leg])
        small = tri.buffer(-clearance, join_style="mitre")
        if z_cut is not None:
            # 継ぎ目より上は屋根の裏まで立ち上げて屋根と一体に（裏返して印刷すると屋根から生える）、下は隙間の分だけ小さくして1階の角にはめる
            roof_parts.append(keep_below(prism(tri, z_cut, roof_zmax), rt))
            roof_parts.append(prism(small, z_cut - block_h, z_cut + 1))
        else:
            zlo = min(plane_z(rb, *p) for p in small.exterior.coords) - block_h - 10
            roof_parts.append(keep_above(keep_below(prism(small, zlo, roof_zmax), rt), rb, offset=-block_h))
    notes["roof"].append(f"ずれ止めの三角を {len(r_corners)} か所（外壁の内側の角" + ("、天井の高さで1階にはまる）" if z_cut else "）"))
    roof_ext = union_all(roof_ext_parts)
    roof_solid = union_all(roof_parts) - roof_ext          # 同じオブジェクトの部品どうしは重ねない

    # 面がちょうど重なる所に残る、厚みゼロのかけらを取り除く
    foundation, floor1, exterior, roof_solid, roof_ext = (
        drop_specks(x) for x in (foundation, floor1, exterior, roof_solid, roof_ext))

    parts = {}
    parts["foundation"] = ModelPart("foundation", "基礎", foundation, notes=notes["foundation"])
    parts["floor1"] = ModelPart("floor1", "1階（床・内壁）", floor1, notes=notes["floor1"])
    parts["exterior1"] = ModelPart("exterior1", "1階の外壁（表面）" if band_mm else "1階の外壁", exterior,
                                   fuzzy_skin=True, object_key="floor1")
    parts["roof"] = ModelPart("roof", "屋根（屋根板・ポーチの天井）", roof_solid, flip_for_print=True, notes=notes["roof"])
    if not roof_ext.is_empty():
        parts["roof_ext"] = ModelPart("roof_ext", "屋根の外壁", roof_ext, flip_for_print=True, fuzzy_skin=True, object_key="roof")
    return parts


def _pick_corners(poly: Polygon, count: int = 4, leg: float | None = None,
                  avoid: Polygon | None = None) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """多角形の内側から見て出っ張った角（三角が収まる角）のうち、外形の四隅にいちばん近いものを count 個。
    返すのは (角の点, 辺1の向き, 辺2の向き)。向きは角から多角形の辺に沿って離れる向き。
    avoid（内壁など）と三角（辺 leg）が重なる角は使わない（SPEC §8：三角が内壁とぶつかる角は別の角に移す）。"""
    if poly.is_empty:
        return []
    ring = list(poly.exterior.coords)[:-1]
    if not Polygon(ring).exterior.is_ccw:
        ring = ring[::-1]
    pts = np.array(ring, float)
    n = len(pts)
    cands = []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        e_prev, e_next = p0 - p1, p2 - p1
        if np.linalg.norm(e_prev) < 1 or np.linalg.norm(e_next) < 1:
            continue
        cross = (p1[0] - p0[0]) * (p2[1] - p1[1]) - (p1[1] - p0[1]) * (p2[0] - p1[0])
        if cross <= 0:   # 反時計回りで左に曲がる角だけ（内側から見て出っ張った角）
            continue
        e1, e2 = e_prev / np.linalg.norm(e_prev), e_next / np.linalg.norm(e_next)
        if avoid is not None and leg is not None:
            if Polygon([p1, p1 + e1 * leg, p1 + e2 * leg]).intersection(avoid).area > 1.0:
                continue
        cands.append((p1, e1, e2))
    x0, y0, x1, y1 = poly.bounds
    chosen = []
    for target in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))[:count]:
        best = min((c for c in cands if not any(c is d for d in chosen)),
                   key=lambda c: math.dist(c[0], target), default=None)
        if best is not None:
            chosen.append(best)
    return chosen
