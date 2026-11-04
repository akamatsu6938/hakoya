"""2階建て（＋下屋）の組み立て（段階2、2026-11-03 の計画）。平屋は core/builder.py の build_model の今までの道を通る。

部品（オブジェクトは5つ。括弧は同じオブジェクトに入る、ファジースキンの外壁の部品）：
① foundation               基礎（平屋と同じ作り方）
② floor1（exterior1）      1階：床板・内壁・外壁、ポーチの上の塊、2階を受ける三角
③ roof_lower（roof_lower_ext）下屋の屋根：屋根板・下屋の下の天井の板・ずれ止め ＋ 下屋の下の、1階の天井より上の外壁（三角形の飾り壁）
④ floor2（exterior2）      2階：床板（階段の穴・受けのくぼみ）・内壁・外壁
⑤ roof（roof_ext）         大屋根：平屋の屋根と同じ作り方（2階の値で）

分け方（作者に任された。計画どおり）：
- 区域A（2階の外形の下）：1階の壁は、2階の床板の裏（zf2 = 2FL − 2階の床板）まで。2階の壁は zf2 から。
- 区域B（下屋の下）：1階の天井で分け、上は下屋の屋根の部品へ（平屋の「天井の高さで分ける」と同じ）。
- 下屋の屋根と2階は別の部品（別々に持ち上げられるように）。下屋の屋根板は2階の外形を除き、2階とは隙間を空ける。
"""
from __future__ import annotations

import numpy as np
from manifold3d import Manifold
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from core.roof_geom import surfaces as roof_surfaces

from core.builder import (DOMA_UNDER_WALL_MM, GAP_FILL_MM, UPPER_WALL_ABOVE_FL, ModelPart, _pick_corners, _wall_top,
                          drop_loose_bits, drop_specks, keep_above, keep_below, plane_z, prism, union_all, wall_rect, eave_note,
                          slab_outline, slab_solid, opening_keep_out, openings_by_wall_of, smooth_near_openings,
                          raised_floors, eave_soffit_solid, bridge_edge_contacts, OVERLAP_TRIM_MM3)

Z_LO, Z_HI = -10000.0, 100000.0       # 「下・上の限りなし」の代わりの高さ（実寸 mm）
BIG = box(-1e6, -1e6, 1e6, 1e6)
ZONE_B_MARGIN_MM = 300.0              # 下屋の屋根の範囲を広げて、軒先の壁の厚み全体を区域Bに入れる
CRUMB_MODEL_MM3 = 0.5                 # これより小さい塊（模型寸法の体積）は、切れ端として捨てる


def _real_corners(corners, walls_fp: Polygon, leg: float, tol: float = 3.0):
    """2枚の壁が作る本当の角だけを残す（区域の境目にできる、壁のない偽の角を除く）。"""
    out = []
    for v, e1, e2 in corners:
        if all(walls_fp.distance(Point(*(v + e * leg / 2))) <= tol for e in (e1, e2)):
            out.append((v, e1, e2))
    return out


def build_multi(b: dict, rules: dict) -> dict[str, ModelPart]:
    m = rules["model"]
    scale = float(m["scale"])
    min_feat = float(m["min_feature_mm"]) * scale
    clearance = float(m["fit_clearance_mm"]) * scale
    leg = float(m["corner_block_leg_mm"]) * scale
    block_h = float(m["corner_block_height_mm"]) * scale
    floor_block_h = float(m["floor_block_height_mm"]) * scale
    band = float(m["foundation_band_mm"])
    plate_t = float(m["plate_thickness_mm"]) * scale
    upper_plate = float(m.get("upper_floor_plate_mm", 3.0)) * scale
    overhang = float(m.get("roof_overhang_mm", 0.0)) * scale

    lv1, lv2 = b["levels"][0], b["levels"][1]
    fl1, fl2 = float(lv1["fl_mm"]), float(lv2["fl_mm"])
    ceil1 = fl1 + float(lv1.get("ceiling_mm", rules["levels"]["ceiling_height_mm"]))
    ceil2 = fl2 + float(lv2.get("ceiling_mm", rules["levels"]["ceiling_height_mm"]))
    zf2 = fl2 - upper_plate                                          # 2階の床板の裏（1階の部品の上端・2階の部品の下端）
    ftop = float(b["foundation"]["top_mm"])
    outline = Polygon(b["foundation"]["outline"])
    zone_a = Polygon(lv2["outline"])
    roofs = b.get("roofs") or [dict(b["roof"], kind="main")]
    main = next(r for r in roofs if r.get("kind") == "main")
    lean = [r for r in roofs if r.get("kind") == "lean_to" and r.get("bottom_plane") and r.get("footprint")]
    zone_b = [Polygon(r["footprint"]).buffer(ZONE_B_MARGIN_MM, join_style="mitre").difference(zone_a) for r in lean]
    lean_s = [roof_surfaces(r) for r in lean]            # （下の面, 上の面）。片流れは平面の式、面が複数なら MultiPlane
    main_b, main_t = roof_surfaces(main)
    zone_c = BIG.difference(unary_union([zone_a] + zone_b))
    walls1 = [w for w in b["walls"] if w["level"] == lv1["name"]]
    walls2 = [w for w in b["walls"] if w["level"] == lv2["name"]]
    ops_by = openings_by_wall_of(b["walls"], b.get("openings", []))   # 隣の壁にはみ出す開口は、隣の壁でも抜く
    notes = {k: [] for k in ("foundation", "floor1", "roof_lower", "floor2", "roof")}
    notes["floor1"].append(f"2階の下は 2階の床板の裏（{zf2:.0f}）まで、下屋の下は 1階の天井（{ceil1:.0f}）まで。下屋の下の天井より上は下屋の屋根の部品へ")

    def zone_of(x: float, y: float):
        p = Point(x, y)
        if zone_a.contains(p):
            return "A", None
        for k, z in enumerate(zone_b):
            if z.contains(p):
                return "B", k
        return "C", None

    def is_upper(w, fl):
        return float(w["bottom_mm"]) >= fl + UPPER_WALL_ABOVE_FL

    def full_wall(w: dict, z0: float, z1: float, top_plane=None) -> Manifold:
        """壁の立体（z0〜z1、上の屋根の下の面で切る）。開口を抜く。下に残る壁が細すぎれば下まで抜く。"""
        s = prism(wall_rect(w), z0, z1)
        if top_plane is not None:
            s = keep_below(s, top_plane)
        cuts = []
        for o in ops_by.get(w["id"], []):
            sill, head = float(o["sill_mm"]), float(o["head_mm"])
            if sill - z0 < min_feat:
                sill = z0 - 1
            cx, cy = o["center"]
            zone, k = zone_of(cx, cy)
            if w["level"] == lv1["name"] and zone == "B" and top_plane is None:
                top_here = plane_z(lean_s[k][0], cx, cy)
            elif top_plane is not None:
                top_here = plane_z(top_plane, cx, cy)
            else:
                top_here = z1
            if top_here - head < min_feat:                               # 上に残る壁が細すぎる → 上まで抜く（SPEC §8）
                head = Z_HI
            cuts.append(prism(wall_rect(w, extra=40, t0=o["offset_mm"], t1=o["offset_mm"] + o["width_mm"]), sill, head))
        return s - union_all(cuts) if cuts else s

    def slivers(w: dict, cap_of) -> list[Manifold]:
        """継ぎ目のすぐ下に開口の上端があり、細い壁が残る所は、継ぎ目まで抜く（面が重なる膜も残さない）。"""
        out = []
        for o in ops_by.get(w["id"], []):
            head = float(o["head_mm"])
            cap = cap_of(*o["center"])
            if cap is not None and -1.0 < cap - head < min_feat:
                out.append(prism(wall_rect(w, extra=40, t0=o["offset_mm"], t1=o["offset_mm"] + o["width_mm"]), head - 1, cap + 1))
        return out

    # ---------------------------------------------------------------- ② 1階の壁
    def cap1(x, y):
        zone, _ = zone_of(x, y)
        return zf2 if zone == "A" else ceil1

    ext1_lo, int1, std1, lower_ext = [], [], [], []      # ② の外壁・内壁・単独の壁、③ の外壁（区域Bの天井より上）
    upper1 = [w for w in walls1 if w["kind"] in ("exterior", "standalone") and is_upper(w, fl1)]
    hanging1 = [w for w in walls1 if w["kind"] == "interior" and is_upper(w, fl1)
                and _wall_top(w) is not None and abs(_wall_top(w) - ceil1) <= 1.0]
    for w in walls1:
        if w in upper1 or w in hanging1:
            continue
        top = _wall_top(w)
        if w["kind"] == "interior":
            z1 = zf2 if top is not None and abs(top - ceil1) <= 1.0 else (top if top is not None else ceil1)   # 2階の下は床の裏まで
        else:
            z1 = Z_HI if top is None else top
        full = full_wall(w, float(w["bottom_mm"]), z1)
        # 区域ごとの上限より上を、壁全体から引く（区域ごとに切ってから合わせ直すと、合わせ目に閉じない辺ができた）
        part = full - union_all([prism(zone_a, zf2, Z_HI), prism(zone_c, ceil1, Z_HI)] + [prism(zb, ceil1, Z_HI) for zb in zone_b])
        if w["kind"] != "interior":
            for k, zb in enumerate(zone_b):
                lower_ext.append(keep_below(full ^ prism(zb, ceil1, Z_HI), lean_s[k][0]))
        cuts = slivers(w, cap1)
        if cuts:
            part = part - union_all(cuts)
        {"exterior": ext1_lo, "interior": int1, "standalone": std1}[w["kind"]].append(part)
    exterior = union_all(ext1_lo)

    # 床板（土間の部屋は抜く）。平屋と同じ作り方
    doma = [Polygon(r["polygon"]) for r in b.get("rooms", []) if r.get("level") == lv1["name"] and r["floor_mm"] < fl1 - 50]
    walls_fp = unary_union([wall_rect(w) for w in walls1 if float(w["bottom_mm"]) <= fl1 + 1])
    near_walls = walls_fp.buffer(DOMA_UNDER_WALL_MM, join_style="mitre")
    doma_holes = [d.union(d.buffer(DOMA_UNDER_WALL_MM, join_style="mitre").intersection(near_walls)).difference(walls_fp)
                  .intersection(outline) for d in doma]
    doma_holes = [max(getattr(h, "geoms", [h]), key=lambda g: g.area) for h in doma_holes if not h.is_empty]
    floor_poly = outline.difference(unary_union(doma_holes)) if doma_holes else outline
    floor1 = union_all([prism(floor_poly, ftop, fl1)] + int1 + std1)
    raised1, raised1_ids = raised_floors(b.get("rooms", []), fl1, walls_fp, level=lv1["name"])
    if raised1:   # 小上がり（床が FL より高い部屋）の台。平屋と同じ
        floor1 = union_all([floor1] + raised1)
        notes["floor1"].append("小上がり（床が FL より高い部屋）" + "・".join(raised1_ids) + " は、床の上に台を作った（畳の部屋など）")
    floor1 = floor1 - exterior

    # ポーチの上の塊（2階のはね出しの下。作者 11/3：薄い板ではなく、2階の床の裏までの塊。サポート材で印刷）
    soffits = b.get("porch_soffits", [])
    for s in soffits:
        sp, z = Polygon(s["polygon"]), float(s["z_mm"])
        if sp.intersection(zone_a).area < 0.5 * sp.area:
            continue
        beams = [wall_rect(w) for w in upper1 if abs(float(w["bottom_mm"]) - z) <= 1.0]
        caps = [wall_rect(w) for w in walls1 if w["kind"] == "standalone" and _wall_top(w) is not None and abs(_wall_top(w) - z) <= 1.0]
        blk = unary_union([sp] + beams + caps).buffer(GAP_FILL_MM / 2, join_style="mitre").buffer(-GAP_FILL_MM / 2, join_style="mitre")
        floor1 = union_all([floor1, prism(blk.intersection(zone_a), z, zf2)])
        notes["floor1"].append(f"ポーチの上の塊（{s['id']}、高さ {z:.0f}〜{zf2:.0f}）は1階の部品に入れた。下に何もないので、"
                               "Bambu Studio でサポート材（ツリー）を付けて印刷する")
    for w in upper1:   # 区域Aの途中から始まる壁（ポーチの上の梁）は塊と一緒に ②、区域Bは ③
        mx, my = (np.array(w["start"]) + np.array(w["end"])) / 2
        zone, k = zone_of(mx, my)
        if zone == "A":
            floor1 = union_all([floor1, full_wall(w, float(w["bottom_mm"]), zf2) ^ prism(zone_a, Z_LO, zf2)])

    # ---------------------------------------------------------------- ① 基礎（平屋と同じ）＋ 基礎→1階の三角
    ring = outline.difference(outline.buffer(-band, join_style="mitre"))
    inner = [wall_rect(w).intersection(outline) for w in walls1 if w["kind"] == "interior" and float(w["bottom_mm"]) <= fl1 + 1]
    bands = ring.union(unary_union(inner)) if inner else ring
    # 外周の帯につながらない内壁の下の帯は作らない（★物件B：台所の腰壁などの下の帯が、基礎の中でばらばらの小さな塊になった）
    kept = [g for g in getattr(bands, "geoms", [bands]) if g.intersects(ring)]
    if len(kept) < len(getattr(bands, "geoms", [bands])):
        notes["foundation"].append("外周の帯につながらない内壁（台所の腰壁など）の下には帯を作らない（ばらばらの塊になるため）")
    pieces = [prism(unary_union(kept), 0.0, ftop)]
    pieces += [prism(Polygon(p["polygon"]), 0.0, float(p["top_mm"])) for p in b.get("porches", [])]
    pieces += [prism(d.buffer(DOMA_UNDER_WALL_MM, join_style="mitre").intersection(outline), 0.0, float(r["floor_mm"]))
               for d, r in zip(doma, [r for r in b.get("rooms", []) if r.get("level") == lv1["name"] and r["floor_mm"] < fl1 - 50])]
    for w in walls1:   # 基礎の外に立つ壁（袖壁など）の下も基礎にする
        r = wall_rect(w)
        if w["kind"] in ("standalone", "exterior") and not is_upper(w, fl1) and r.intersection(outline).area / r.area < 0.5:
            pieces.append(prism(r, 0.0, float(w["bottom_mm"])))
    foundation = union_all(pieces)
    inner_outline = outline.buffer(-band, join_style="mitre")
    f_blocks, f_recess = [], []
    for v, e1, e2 in _pick_corners(inner_outline):
        tri = Polygon([v, v + e1 * leg, v + e2 * leg])
        f_blocks.append(prism(tri, 0.0, ftop + floor_block_h))
        f_recess.append(prism(tri.buffer(clearance, join_style="mitre"), ftop - 1, ftop + floor_block_h + clearance))
    if f_blocks:
        foundation = union_all([foundation] + f_blocks)
        floor1 = floor1 - union_all(f_recess)
        exterior = exterior - union_all(f_recess)
        notes["foundation"].append(f"位置合わせの三角を {len(f_blocks)} か所（基礎の帯の内側の角）")

    # ---------------------------------------------------------------- ④ 2階
    walls2_fp = unary_union([wall_rect(w) for w in walls2]) if walls2 else Polygon()
    holes = unary_union([Polygon(h["polygon"]) for h in lv2.get("floor_openings", [])])
    plate2 = zone_a.difference(holes.difference(walls2_fp)) if not holes.is_empty else zone_a
    floor2 = prism(plate2, zf2, fl2)
    ext2_lo, ext2_up, int2 = [], [], []
    for w in walls2:
        if w["kind"] in ("exterior", "standalone"):
            full = full_wall(w, zf2, Z_HI, top_plane=main_b)   # 床板の裏から（外の面を続ける）
            lo = full.trim_by_plane((0.0, 0.0, -1.0), -ceil2)
            cuts = slivers(w, lambda x, y: ceil2)
            ext2_lo.append(lo - union_all(cuts) if cuts else lo)
            ext2_up.append(full.trim_by_plane((0.0, 0.0, 1.0), ceil2))
        else:
            top = _wall_top(w)
            full = full_wall(w, float(w["bottom_mm"]), top if top is not None else ceil2)
            int2.append(full.trim_by_plane((0.0, 0.0, -1.0), -ceil2))
    exterior2 = union_all(ext2_lo)
    raised2, raised2_ids = raised_floors(b.get("rooms", []), fl2, walls2_fp, level=lv2["name"])
    floor2 = union_all([floor2] + int2 + raised2) - exterior2
    if raised2:
        notes["floor2"].append("小上がり（床が FL より高い部屋）" + "・".join(raised2_ids) + " は、床の上に台を作った（畳の部屋など）")
    if lv2.get("floor_openings"):
        notes["floor2"].append(f"2階の床板（厚み {m.get('upper_floor_plate_mm', 3.0)}mm）に階段の穴を開けた。階段は造作なので作らない")

    # ②→④ のかかり：区域Aの1階の外壁の内側の本当の角に上向きの三角、2階の床裏にくぼみ
    ext1_walls = [w for w in walls1 if w["kind"] == "exterior" and not is_upper(w, fl1)]
    ext1_fp = unary_union([wall_rect(w) for w in ext1_walls])
    inside_a = zone_a.intersection(Polygon(outline.exterior).difference(ext1_fp))
    inside_a = max(getattr(inside_a, "geoms", [inside_a]), key=lambda g: g.area) if not inside_a.is_empty else inside_a
    int1_fp = unary_union([wall_rect(w) for w in walls1 if w["kind"] == "interior"])
    avoid_a = unary_union([int1_fp, holes]) if not holes.is_empty else int1_fp
    a_corners = _real_corners(_pick_corners(inside_a, count=4, leg=leg + clearance, avoid=avoid_a), ext1_fp, leg)
    for v, e1, e2 in a_corners:
        tri = Polygon([v, v + e1 * leg, v + e2 * leg])
        floor1 = union_all([floor1, prism(tri, zf2 - 300.0, zf2 + floor_block_h)])
        floor2 = floor2 - prism(tri.buffer(clearance, join_style="mitre"), zf2 - 1, zf2 + floor_block_h + clearance)
    notes["floor2"].append(f"1階と2階の位置合わせの三角を {len(a_corners)} か所（1階の外壁の内側の角。2階の床裏にくぼみ）")

    # ---------------------------------------------------------------- ③ 下屋の屋根
    roof_lower_parts, lower_soffit_tops = [], {}
    for k, r in enumerate(lean):
        rb, rt = roof_surfaces(r)
        zmax = float(r["top_z_range_mm"][1]) + 200
        sp = slab_outline(r)                                                       # 軒の出の修正はここだけに効く（区域B は元の形）
        sp = sp.buffer(overhang, join_style="mitre") if overhang else sp
        sp = sp.difference(zone_a.buffer(clearance, join_style="mitre"))          # 2階の外形の所は除く（④ と重ねない）
        roof_lower_parts.append(slab_solid(sp, rb, rt, zmax))
        if eave_note(r, f"下屋の屋根 {r.get('id', '')}"):
            notes["roof_lower"].append(eave_note(r, f"下屋の屋根 {r.get('id', '')}"))
        # 下屋の軒天（箱の軒）：1階の外形と2階の外形を除く。1階の天井より低い軒天は、1階の壁とはめあいの隙間を空ける
        sz = r.get("soffit_z_mm")
        gap = clearance if sz is not None and float(sz) < ceil1 else 0.0
        house1 = Polygon(outline.exterior).union(zone_a.buffer(clearance, join_style="mitre"))
        so, so_note = eave_soffit_solid(r, sp, house1, rt, zmax, fl1, gap)
        if so is not None:
            roof_lower_parts.append(so)
        if so_note:
            notes["roof_lower"].append(f"下屋の屋根 {r.get('id', '')}：" + so_note)
    for s in soffits:   # 下屋の下の天井（南西のへこみなど）：平らな板。区域Bの途中から始まる壁の下まで広げる
        sp, z = Polygon(s["polygon"]), float(s["z_mm"])
        if sp.intersection(zone_a).area >= 0.5 * sp.area:
            continue
        hang = [wall_rect(w) for w in upper1 if abs(float(w["bottom_mm"]) - z) <= 1.0]
        pp = unary_union([sp] + hang).buffer(GAP_FILL_MM / 2, join_style="mitre").buffer(-GAP_FILL_MM / 2, join_style="mitre")
        pp = pp.difference(zone_a.buffer(clearance, join_style="mitre"))
        roof_lower_parts.append(prism(pp, z, z + plate_t))
        lower_soffit_tops[s["id"]] = (pp, z + plate_t)
        notes["roof_lower"].append(f"下屋の下の天井（{s['id']}、高さ {z:.0f}）は下屋の屋根と一緒に印刷する")
    for w in upper1:   # 区域Bの途中から始まる壁（へこみの上の垂れ壁など）：天井の板の上から下屋の屋根まで
        mx, my = (np.array(w["start"]) + np.array(w["end"])) / 2
        zone, k = zone_of(mx, my)
        if zone != "B":
            continue
        bottom = float(w["bottom_mm"])
        for pp, top in lower_soffit_tops.values():
            if pp.intersects(wall_rect(w)) and abs(top - plate_t - bottom) <= 1.0:
                bottom = top
        lower_ext.append(keep_below(full_wall(w, bottom, Z_HI) ^ prism(zone_b[k], Z_LO, Z_HI), lean_s[k][0]))
    if hanging1:   # 部屋の中の下がり壁（区域B は下屋の屋根へ、区域A は 1階に残す）
        standing = unary_union([wall_rect(o) for o in walls1 if float(o["bottom_mm"]) <= fl1 + 1]).buffer(clearance, join_style="mitre")
        for w in hanging1:
            mx, my = (np.array(w["start"]) + np.array(w["end"])) / 2
            zone, k = zone_of(mx, my)
            if zone == "B":
                s = keep_below(prism(wall_rect(w), float(w["bottom_mm"]), Z_HI), lean_s[k][1])
                roof_lower_parts.append(s - prism(standing, float(w["bottom_mm"]) - 1, ceil1))
            else:
                floor1 = union_all([floor1, prism(wall_rect(w), float(w["bottom_mm"]), zf2)])
                notes["floor1"].append(f"2階の下の下がり壁 {w['id']} は1階の部品に入れた（宙に渡る形。サポート材が要る）")
    # ③→② のかかり：区域Bの1階の外壁の内側の本当の角
    b_corners = []
    for k, zb in enumerate(zone_b):
        inside_b = zb.intersection(Polygon(outline.exterior).difference(ext1_fp))
        if inside_b.is_empty:
            continue
        inside_b = max(getattr(inside_b, "geoms", [inside_b]), key=lambda g: g.area)
        for v, e1, e2 in _real_corners(_pick_corners(inside_b, count=4, leg=leg, avoid=int1_fp), ext1_fp, leg):
            tri = Polygon([v, v + e1 * leg, v + e2 * leg])
            small = tri.buffer(-clearance, join_style="mitre")
            roof_lower_parts.append(keep_below(prism(tri, ceil1, float(lean[k]["top_z_range_mm"][1]) + 200), lean_s[k][1]))
            roof_lower_parts.append(prism(small, ceil1 - block_h, ceil1 + 1))
            b_corners.append(v)
    notes["roof_lower"].append(f"ずれ止めの三角を {len(b_corners)} か所（下屋の下の1階の外壁の内側の角）。2階の外壁とは隙間を空けた（別々に外せる）")
    roof_lower_ext = union_all(lower_ext)
    roof_lower = union_all(roof_lower_parts) - roof_lower_ext

    # ---------------------------------------------------------------- ⑤ 大屋根（平屋の屋根と同じ作り方を2階の値で）
    rb, rt = main_b, main_t
    zmax = float(main["top_z_range_mm"][1]) + 200
    sp = slab_outline(main)
    sp = sp.buffer(overhang, join_style="mitre") if overhang else sp
    roof_parts = [slab_solid(sp, rb, rt, zmax)]
    if eave_note(main, f"大屋根 {main.get('id', '')}"):
        notes["roof"].append(eave_note(main, f"大屋根 {main.get('id', '')}"))
    # 大屋根の軒天：2階の外形を除く。2階の天井より低い軒天は、2階の壁とはめあいの隙間を空ける
    sz = main.get("soffit_z_mm")
    so, so_note = eave_soffit_solid(main, sp, zone_a, rt, zmax, fl2,
                                    clearance if sz is not None and float(sz) < ceil2 else 0.0)
    if so is not None:
        roof_parts.append(so)
    if so_note:
        notes["roof"].append(so_note)
    ext2_fp = unary_union([wall_rect(w) for w in walls2 if w["kind"] == "exterior"])
    inside2 = zone_a.difference(ext2_fp)
    inside2 = max(getattr(inside2, "geoms", [inside2]), key=lambda g: g.area)
    int2_fp = unary_union([wall_rect(w) for w in walls2 if w["kind"] == "interior"]) if walls2 else None
    r_corners = _pick_corners(inside2, leg=leg, avoid=int2_fp)
    for v, e1, e2 in r_corners:
        tri = Polygon([v, v + e1 * leg, v + e2 * leg])
        roof_parts.append(keep_below(prism(tri, ceil2, zmax), rt))
        roof_parts.append(prism(tri.buffer(-clearance, join_style="mitre"), ceil2 - block_h, ceil2 + 1))
    notes["roof"].append(f"ずれ止めの三角を {len(r_corners)} か所（2階の外壁の内側の角、2階の天井の高さではまる）")
    roof_ext = union_all(ext2_up)
    roof_solid = union_all(roof_parts) - roof_ext

    # ---------------------------------------------------------------- ファジースキン：外壁の外の面の薄い層だけ（窓・ドアのまわりは外す）
    band_mm = m.get("fuzzy_skin_band_mm")
    if band_mm:
        fb = float(band_mm) * scale
        margin = float(m.get("fuzzy_skin_opening_margin_mm", 0.0)) * scale

        def skin_split(solid, house, z0, z1, ext_walls):
            ring2d = house.difference(house.buffer(-fb, join_style="mitre"))
            skin = prism(ring2d, z0 - 1, z1 + 1)
            keep_out = [prism(wall_rect(w, extra=40, t0=o["offset_mm"] - margin, t1=o["offset_mm"] + o["width_mm"] + margin),
                              float(o["sill_mm"]) - margin, float(o["head_mm"]) + margin)
                        for w in ext_walls for o in ops_by.get(w["id"], [])]
            if keep_out:
                skin = skin - union_all(keep_out)
            return solid ^ skin, solid - skin
        exterior, core1 = skin_split(exterior, Polygon(outline.exterior), ftop, zf2, ext1_walls)
        floor1 = union_all([floor1, core1])
        exterior2, core2 = skin_split(exterior2, zone_a, zf2, ceil2, [w for w in walls2 if w["kind"] == "exterior"])
        floor2 = union_all([floor2, core2])
        # 下屋の外壁・大屋根の外壁も、窓・ドアのまわりはざらざらにしない（天井の高さに上端がある窓・ドアの上の帯）
        for name, ws in (("roof_lower", walls1), ("roof", walls2)):
            keep = opening_keep_out([w for w in ws if w["kind"] in ("exterior", "standalone")], ops_by, margin)
            if name == "roof_lower":
                roof_lower_ext, roof_lower, moved = smooth_near_openings(roof_lower_ext, roof_lower, keep)
            else:
                roof_ext, roof_solid, moved = smooth_near_openings(roof_ext, roof_solid, keep)
            if moved:
                notes[name].append(f"外壁の窓・ドアのまわり {m.get('fuzzy_skin_opening_margin_mm')}mm はざらざらにしない（天井の高さに上端がある窓・ドアの上の帯）")
        notes["floor1"].append(f"ファジースキンは外壁の外の面から {band_mm}mm（模型）の層だけ。窓・ドアのまわり {m.get('fuzzy_skin_opening_margin_mm')}mm は外す")

    # 平屋と同じ後始末（★11/4 の見直し：昨夜の直しが2階建ての道に入っていなかった）
    # 基礎とほかの部品が重なる所は基礎から削る（基礎の天端より高いポーチ）。1階から離れた床の板の切れ端は作らない
    others = union_all([s for s in (floor1, exterior, roof_lower, roof_lower_ext) if not s.is_empty()])
    if (foundation ^ others).volume() > 1.0:
        foundation = foundation - others
        notes["foundation"].append("1階・下屋の部品と重なる所（基礎の天端より高いポーチなど）は、基礎の方を削った（組むときにぶつからないように）")
    (floor1, exterior), loose = drop_loose_bits([floor1, exterior], max_height=fl1 - ftop + 1.0)
    if loose:
        notes["floor1"].append(f"1階の床から離れた小さな床の切れ端 {loose} 個は作らない（印刷するとばらばらになるため）")

    # 小さなかけら（模型で 0.5mm³ 未満。区域の境目や斜めの壁の角の床板の切れ端）は捨てる
    crumb = CRUMB_MODEL_MM3 * scale ** 3
    # ★11/4 物件K・M：下屋の屋根の部品の壁（垂れ壁・1階の天井より上の外壁）が、1階・2階の壁と角で少し重なっていた。
    # 組む順の後ろの部品（下屋の屋根・大屋根）から、先に組む部品（1階・2階）と重なる所を除く
    first_obj = union_all([floor1, exterior])
    second_obj = union_all([floor2, exterior2])
    trimmed = {}
    def _trim(solid, base, key):
        if solid.is_empty() or base.is_empty():
            return solid
        v = (solid ^ base).volume()
        if v <= OVERLAP_TRIM_MM3:
            return solid
        trimmed[key] = trimmed.get(key, 0.0) + v
        return solid - base
    roof_lower = _trim(_trim(roof_lower, first_obj, "roof_lower"), second_obj, "roof_lower")
    roof_lower_ext = _trim(_trim(roof_lower_ext, first_obj, "roof_lower"), second_obj, "roof_lower")
    roof_solid = _trim(roof_solid, second_obj, "roof")
    roof_ext = _trim(roof_ext, second_obj, "roof")
    for key, v in trimmed.items():
        notes[key].append(f"先に組む部品（1階・2階）と重なる所（模型で約 {v / scale ** 3:.1f}mm³）は除いた（組むとぶつかるため）")
    parts_solids = [drop_specks(x, crumb) for x in (foundation, floor1, exterior, roof_lower, roof_lower_ext, floor2, exterior2, roof_solid, roof_ext)]
    # 辺だけで接している所を、ごく細い角柱で面でつなぐ（平屋と同じ。★11/4 物件H・K）
    note_key = ["foundation", "floor1", "floor1", "roof_lower", "roof_lower", "floor2", "floor2", "roof", "roof"]
    for i, x in enumerate(parts_solids):
        fixed, n = bridge_edge_contacts(x)
        if n:
            parts_solids[i] = fixed
            notes[note_key[i]].append(f"辺だけで接していた所 {n} か所に、ごく細い角柱（模型 0.2mm 角）を足して面でつないだ（閉じた立体にするため）")
    foundation, floor1, exterior, roof_lower, roof_lower_ext, floor2, exterior2, roof_solid, roof_ext = parts_solids
    parts = {
        "foundation": ModelPart("foundation", "基礎", foundation, notes=notes["foundation"]),
        "floor1": ModelPart("floor1", "1階（床・内壁）", floor1, notes=notes["floor1"]),
        "exterior1": ModelPart("exterior1", "1階の外壁（表面）", exterior, fuzzy_skin=True, object_key="floor1"),
        "roof_lower": ModelPart("roof_lower", "下屋の屋根", roof_lower, flip_for_print=True, notes=notes["roof_lower"]),
        "roof_lower_ext": ModelPart("roof_lower_ext", "下屋の外壁", roof_lower_ext, flip_for_print=True, fuzzy_skin=True,
                                    object_key="roof_lower"),
        "floor2": ModelPart("floor2", "2階（床・内壁）", floor2, notes=notes["floor2"]),
        "exterior2": ModelPart("exterior2", "2階の外壁（表面）", exterior2, fuzzy_skin=True, object_key="floor2"),
        "roof": ModelPart("roof", "大屋根", roof_solid, flip_for_print=True, notes=notes["roof"]),
        "roof_ext": ModelPart("roof_ext", "大屋根の外壁", roof_ext, flip_for_print=True, fuzzy_skin=True, object_key="roof"),
    }
    return {k: p for k, p in parts.items() if not p.solid.is_empty() or k in ("foundation", "floor1", "floor2", "roof")}
