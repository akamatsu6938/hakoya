"""Walk in home の glb から building.json（家の形）を作る（SPEC §4-1・§6）。

使い方:  python -m adapters.walkinhome 物件A
いまの段階（2b）：部材の仕分け・数の一覧、高さ（GL・基礎天端・各階の FL・天井高）、壁の作り直し、部屋の形。
開口・屋根は次の段階で足す。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import numpy as np

from shapely.geometry import Polygon
from shapely.ops import unary_union

from adapters import walls as wl
from adapters.glb_reader import GlbModel, Part, read_glb
from adapters.walkinhome_types import part_type
from core.paths import glb_working_copy, out_dir, property_files
from core.privacy import assert_clean
from core.rules import load_rules, rule

FLAT_TOL_MM = 1.0          # 水平な面とみなす高さの差
STORY_GAP_MM = 1500.0      # 床の高さがこれ以上離れていたら別の階 (推定。2階建ては要検証)
ROOM_FLOOR_CODES = ("HEYA_YUKA", "SHUU_YUKA")


def _flat_heights(parts: list[Part], codes: tuple[str, ...] | None = None) -> collections.Counter:
    """水平な三角形の高さ（mm、整数に丸め）ごとの数。"""
    cnt: collections.Counter = collections.Counter()
    for p in parts:
        for pr in p.primitives:
            if codes is not None and pr.code not in codes:
                continue
            f = pr.vertices[pr.triangles]
            if not len(f):
                continue
            flat = np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM
            for z in np.round(f[flat, 0, 2]).astype(int):
                cnt[int(z)] += 1
    return cnt


def _first_source(parts: list[Part], codes: tuple[str, ...], z: int) -> str:
    """高さ z の水平面を持つ最初の部材の出どころ。"""
    for p in parts:
        if _flat_heights([p], codes).get(z):
            return p.source
    return parts[0].source if parts else "glb:unknown"


def detect_ground(model: GlbModel) -> dict:
    """GL と基礎天端。"""
    site = model.by_type("typeESiki")
    if site:
        gl = int(round(site[0].bounds()[0][2]))
        gl_src = site[0].source
    else:
        gl, gl_src = 0, None
    kiso = model.by_type("typeEKiso")
    if not kiso:
        raise ValueError("基礎（typeEKiso）が glb にありません。")
    # ★基礎の本体（KISO_HONT）は側面と底だけで、天端の水平面は別の部位（KISO_ILOW）に入っている。
    # 本体の側面が届いている一番上を基礎天端とする（水切り KISO_DMIZ は天端より上に出るので使わない）。
    hont = [k.faces("KISO_HONT") for k in kiso]
    hont = [f for f in hont if len(f)]
    if not hont:
        raise ValueError("基礎の本体（KISO_HONT）が見つかりません。")
    top = int(round(max(f[:, :, 2].max() for f in hont)))
    ground = {"gl_mm": gl, "foundation_top_mm": top, "foundation_source": kiso[0].source}
    if gl_src:
        ground["gl_source"] = gl_src
    else:
        ground["gl_assumption"] = "敷地がないため GL＝Z 0 とした"
    return ground


def detect_levels(model: GlbModel) -> list[dict]:
    """各階の FL と天井高。部屋の床の高さをまとめ、離れていれば別の階とする。"""
    rooms = model.by_role("room")
    floors = _flat_heights(rooms, ROOM_FLOOR_CODES)
    if not floors:
        raise ValueError("部屋の床（HEYA_YUKA）が見つかりません。")
    # 高さ順に並べ、STORY_GAP_MM 以上離れたところで階を分ける
    groups: list[list[int]] = []
    for z in sorted(floors):
        if groups and z - groups[-1][-1] < STORY_GAP_MM:
            groups[-1].append(z)
        else:
            groups.append([z])
    ceilings = _flat_heights(rooms, ("HEYA_TENJ",))
    levels = []
    for i, g in enumerate(groups):
        fl = max(g, key=lambda z: floors[z])  # その階でいちばん多い床の高さ
        upper = groups[i + 1][0] if i + 1 < len(groups) else float("inf")
        cands = {z: n for z, n in ceilings.items() if fl < z < upper}
        level = {"name": f"{i + 1}F", "fl_mm": fl, "source": _first_source(rooms, ROOM_FLOOR_CODES, fl)}
        if cands:
            top = max(cands, key=lambda z: cands[z])
            level["ceiling_mm"] = top - fl
            level["ceiling_source"] = _first_source(rooms, ("HEYA_TENJ",), top)
        if len(g) > 1:
            level["other_floor_heights_mm"] = [z for z in g if z != fl]
        levels.append(level)
    return levels


def inventory(model: GlbModel) -> list[dict]:
    """部材の種類ごとの数と、模型での扱い。"""
    cnt = collections.Counter(p.type_code or "unknown" for p in model.parts)
    rows = []
    for code, n in cnt.most_common():
        t = part_type(code)
        rows.append({"type": code, "label": t.label, "role": t.role, "count": n})
    return rows


def compare_with_rules(ground: dict, levels: list[dict], rules: dict) -> list[dict]:
    """glb で測った値と会社ルールの値の食い違い（検品の材料）。"""
    out = []
    pairs = [("levels.gl_to_foundation_top_mm", ground["foundation_top_mm"] - ground["gl_mm"], "基礎天端（GL から）")]
    if levels:
        pairs.append(("levels.gl_to_1fl_mm", levels[0]["fl_mm"] - ground["gl_mm"], "1FL（GL から）"))
        if "ceiling_mm" in levels[0]:
            pairs.append(("levels.ceiling_height_mm", levels[0]["ceiling_mm"], "1階の天井高"))
    for key, measured, label in pairs:
        expected = rule(rules, key)
        out.append({"item": label, "glb_mm": measured, "rule": key, "rule_mm": expected, "match": measured == expected})
    return out


# ---------------------------------------------------------------- 壁と部屋（段階2b）

NOT_FINISH_CODES = ("HABA", "MAWA")   # 巾木・廻り縁：裏面が部屋の内側を向くので使わない（SPEC §4-1）
NOT_FLOOR_CODES = ("HABA", "MAWA", "TENJ")


def _code_endswith(code: str | None, suffixes: tuple[str, ...]) -> bool:
    return bool(code) and code.endswith(suffixes)


def _room_floor(part: Part, fl_mm: float) -> Polygon:
    """部屋の床の形（FL＋100mm 以下の水平な面を合わせたもの。玄関の土間も含む）。"""
    polys = []
    for pr in part.primitives:
        if _code_endswith(pr.code, NOT_FLOOR_CODES):
            continue
        f = pr.vertices[pr.triangles]
        if not len(f):
            continue
        flat = (np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM) & (f[:, 0, 2] <= fl_mm + 100)
        for t in f[flat]:
            p = Polygon(t[:, :2])
            if p.area > 1:
                polys.append(p)
    if not polys:
        return Polygon()
    return unary_union(polys).buffer(0.5, join_style="mitre").buffer(-0.5, join_style="mitre")


LOW_FACE_MM = 300.0   # FL からこの高さまでしかない面（上がり框の段など）は壁の仕上げ面にしない


def _finish_triangles(parts: list[Part], fl_mm: float) -> list:
    """部屋の壁の仕上げ面（垂直な三角形）。巾木・廻り縁と、床近くの低い面（段・框）は除く。"""
    tris = []
    for p in parts:
        for pr in p.primitives:
            if _code_endswith(pr.code, NOT_FINISH_CODES) or not len(pr.triangles):
                continue
            f = pr.vertices[pr.triangles]
            f = f[f[:, :, 2].max(axis=1) > fl_mm + LOW_FACE_MM]
            tris += wl.vertical_triangles(f, p.source)
    return tris


def _measure_exterior_thickness(ext_planes: list[wl.Plane], finish_planes: list[wl.Plane]) -> float | None:
    """外壁の外の面から、向かい合う室内の仕上げ面までの距離（多い値）。"""
    dists = []
    for e in ext_planes:
        for f in finish_planes:
            if np.dot(e.n, f.n) > -0.999:
                continue
            d = e.offset + f.offset  # f.n = -e.n なので、距離 = e.offset - (-f.offset)
            if 100 <= d <= 300:
                dists.append(round(d))
    return float(collections.Counter(dists).most_common(1)[0][0]) if dists else None


def reconstruct(model: GlbModel, level: dict, rules: dict) -> dict:
    """1つの階の壁と部屋を作り直す。2階建ての分け方は物件Bで要検証。"""
    fl = float(level["fl_mm"])
    t_ext = float(rule(rules, "walls.exterior_thickness_mm"))
    t_int = float(rule(rules, "walls.interior_thickness_mm"))

    # 開口（建具の箱）とドア（床まで届く建具）
    tate = model.by_type("typeETate")
    opening_fps, door_fps = [], []
    for p in tate:
        pts = np.vstack([pr.vertices[:, :2] for pr in p.primitives])
        fp = wl.bbox_polygon(pts)
        opening_fps.append(fp)
        if p.bounds()[0][2] <= fl + 100:
            door_fps.append(fp)

    # 手順1：外壁、手順4：単独の壁
    ext_tris = [t for p in model.by_role("exterior_wall") for t in wl.vertical_triangles(p.faces(), p.source)]
    ext_planes = wl.group_planes(ext_tris)
    ext = wl.exterior_walls(ext_planes, t_ext, fl, opening_fps)
    wl.fill_inside_corners(ext)
    ext = wl.merge_back_to_back(ext, t_ext)
    standalone, skipped = [], []
    for p in model.by_role("standalone_wall"):
        w = wl.standalone_wall(p.faces(), p.source)
        if w is None:
            skipped.append({"source": p.source, "reason": "垂直な面がほとんどないか、高さが300mm未満（上端・下端の板や笠木）のため壁にしない"})
        else:
            standalone.append(w)
    outer = wl.drop_covered(ext + standalone)
    exterior_fp = wl.footprint(outer)

    # 手順2・3・6：部屋の内側 → 内壁
    rooms = model.by_role("room")
    finish_planes = wl.group_planes(_finish_triangles(rooms, fl))
    floors = {p.source: _room_floor(p, fl) for p in rooms}
    rooms_inner, region = wl.interior_region([f for f in floors.values() if not f.is_empty], finish_planes,
                                             door_fps, exterior_fp)
    interior = wl.interior_walls(finish_planes, region, t_int)
    zone = unary_union([region, exterior_fp] + door_fps).buffer(5, join_style="mitre")
    for _ in range(2):  # つないだ後にもう一度：つながった壁の端も、ぶつかる壁まで伸ばす
        wl.extend_to_junctions(interior, outer + interior, zone=zone)
        interior = wl.merge_collinear(interior, region)
    kept = wl.drop_covered(outer + interior, keep_kinds=("exterior", "standalone"), ratio=0.5)
    interior = [w for w in kept if w.kind == "interior"]
    for w in interior:  # 巾木の上から始まっている内壁は、床（FL）から立てる
        if w.bottom <= fl + LOW_FACE_MM:
            w.bottom = fl
    unresolved = wl.unresolved_regions(region, interior, t_int, door_fps)

    all_walls = outer + interior
    # 覆えていない所：壁は実際の隙間の幅で数え、ドアの位置（次の段階でくり抜く）は除く
    covered = unary_union([w.rect(getattr(w, "_gap", w.thickness) + 20) for w in interior] + door_fps)
    lo_x, lo_y, hi_x, hi_y = exterior_fp.bounds
    gaps = [getattr(w, "_gap") for w in interior if getattr(w, "_gap", None)]
    stats = {
        "outer_size_mm": [round(hi_x - lo_x, 1), round(hi_y - lo_y, 1)],
        "exterior_thickness_measured_mm": _measure_exterior_thickness(ext_planes, finish_planes),
        "interior_gap_measured_mm": round(float(np.median(gaps)), 1) if gaps else None,
        "interior_region_m2": round(region.area / 1e6, 3),
        "interior_region_uncovered_m2": round(region.difference(covered).area / 1e6, 3),
        "count": dict(collections.Counter(w.kind for w in all_walls)),
    }
    room_list = []
    for src, fl_poly in floors.items():
        inner = fl_poly.intersection(rooms_inner)
        if inner.is_empty:
            continue
        room_list.append({"source": src, "polygon": inner, "floor_mm": _min_floor_z(model, src, fl)})
    return {"walls": all_walls, "rooms": room_list, "skipped": skipped, "stats": stats,
            "unresolved": unresolved, "region": region, "exterior_fp": exterior_fp}


def _min_floor_z(model: GlbModel, source: str, fl: float) -> int:
    part = next(p for p in model.parts if p.source == source)
    zs = [z for z in _flat_heights([part]) if z <= fl + 100]
    return min(zs) if zs else int(fl)


def _xy(p) -> list[float]:
    return [round(float(p[0]), 1), round(float(p[1]), 1)]


def _poly_coords(poly) -> list:
    geoms = getattr(poly, "geoms", [poly])
    big = max(geoms, key=lambda g: g.area)
    return [_xy(c) for c in big.simplify(1.0).exterior.coords[:-1]]


def walls_to_json(walls: list[wl.Wall], level: str) -> tuple[list[dict], list[dict]]:
    order = {"exterior": 0, "standalone": 1, "interior": 2}
    walls = sorted(walls, key=lambda w: (order[w.kind], round(w.start[1]), round(w.start[0])))
    out, assumptions = [], []
    for i, w in enumerate(walls, 1):
        wid = f"W{i}"
        d = {"id": wid, "level": level, "kind": w.kind, "start": _xy(w.start), "end": _xy(w.end),
             "thickness_mm": round(w.thickness, 1), "bottom_mm": round(w.bottom),
             "height": "to_roof" if w.top is None else round(w.top)}
        if w.top_glb is not None:
            d["top_glb_mm"] = round(w.top_glb)
        if w.sources:
            d["source"] = w.sources[0]
            if len(w.sources) > 1:
                d["sources"] = w.sources
        if w.kind == "exterior":
            d["thickness_rule"] = "walls.exterior_thickness_mm"
        elif w.kind == "interior":
            d["thickness_rule"] = "walls.interior_thickness_mm"
        if w.assumptions:
            d["assumption"] = w.assumptions
            for a in w.assumptions:
                assumptions.append({"field": f"walls[{wid}]", "note": a})
        out.append(d)
    return out, assumptions


def build(alias: str, company: str = "jisha", return_debug: bool = False):
    files = property_files(alias)
    model = read_glb(glb_working_copy(files))
    rules = load_rules(company)
    ground = detect_ground(model)
    levels = detect_levels(model)
    rec = reconstruct(model, levels[0], rules)
    walls_json, assumptions = walls_to_json(rec["walls"], levels[0]["name"])
    rooms_json = [{"id": f"R{i}", "level": levels[0]["name"], "polygon": _poly_coords(r["polygon"]),
                   "floor_mm": r["floor_mm"], "area_m2": round(r["polygon"].area / 1e6, 2), "source": r["source"]}
                  for i, r in enumerate(sorted(rec["rooms"], key=lambda r: r["source"]), 1)]
    building = {
        "source": {"glb": files.glb_label, "generator": model.generator, "wih_version": model.version_info,
                   "company_rules": company},
        "ground": ground,
        "levels": levels,
        "walls": walls_json,
        "rooms": rooms_json,
        "wall_stats": rec["stats"],
        "skipped": rec["skipped"],
        "unresolved": [{"id": f"U{i}", "level": levels[0]["name"], "kind": "interior_region_without_faces",
                        "start": _xy(u.start), "end": _xy(u.end), "width_mm": round(u.thickness),
                        "note": u.assumptions[0]} for i, u in enumerate(rec["unresolved"], 1)],
        "inventory": inventory(model),
        "rule_checks": compare_with_rules(ground, levels, rules),
        "assumptions": assumptions,
    }
    return (building, rec) if return_debug else building


def write_building(alias: str, building: dict) -> Path:
    text = json.dumps(building, ensure_ascii=False, indent=2)
    assert_clean(text, "building.json")
    path = out_dir(alias) / "building.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _print_summary(alias: str, b: dict) -> None:
    print(f"【{alias}】 {b['source']['wih_version']}")
    g = b["ground"]
    print(f"  GL {g['gl_mm']} / 基礎天端 {g['foundation_top_mm']}  （出どころ {g.get('gl_source', '-')}, {g['foundation_source']}）")
    for lv in b["levels"]:
        print(f"  {lv['name']}: FL {lv['fl_mm']} / 天井高 FL+{lv.get('ceiling_mm', '?')}  （出どころ {lv['source']}）")
    print("  会社ルールとの照合:")
    for c in b["rule_checks"]:
        mark = "一致" if c["match"] else "★食い違い"
        print(f"    {c['item']}: glb {c['glb_mm']} / ルール {c['rule_mm']} → {mark}")
    s = b.get("wall_stats")
    if s:
        print(f"  壁: {s['count']}  外形 {s['outer_size_mm'][0]}×{s['outer_size_mm'][1]}mm")
        print(f"      外壁の厚み（測った値）{s['exterior_thickness_measured_mm']}mm / 内壁の仕上げ面の間（測った値）{s['interior_gap_measured_mm']}mm")
        print(f"      内壁の範囲 {s['interior_region_m2']}m2 のうち、壁で覆えていない所 {s['interior_region_uncovered_m2']}m2")
        for sk in b.get("skipped", []):
            print(f"      壁にしなかった部材: {sk['source']}（{sk['reason']}）")
        for u in b.get("unresolved", []):
            print(f"      未解決の所 {u['id']}: {u['start']}→{u['end']} 幅{u['width_mm']}mm（壁があるか要確認）")
    print("  部材の数:")
    for r in b["inventory"]:
        print(f"    {r['label']:<12}（{r['type']}）{r['count']:>3} 個 → {r['role']}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Walk in home の glb から building.json を作る")
    ap.add_argument("alias", help="物件の仮名（data/local_paths.yaml の名前。例：物件A）")
    ap.add_argument("--company", default="jisha", help="会社ルール（rules/<名前>.yaml）")
    args = ap.parse_args(argv)
    building = build(args.alias, args.company)
    path = write_building(args.alias, building)
    _print_summary(args.alias, building)
    print(f"  → 書き出し: data/out/{args.alias}/{path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
