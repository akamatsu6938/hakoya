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
import shapely
from shapely.geometry import MultiPoint, Point, Polygon, box
from shapely.ops import unary_union

from adapters import exterior_parts as xp
from adapters import openings as op
from adapters import walls as wl
from adapters.glb_reader import GlbModel, Part, read_glb
from adapters.roof import detect_roof, merge_roof_parts
from adapters.walkinhome_types import part_type
from core.paths import glb_working_copy, out_dir, property_files
from core.ids import stabilize
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
    # 2階から上：階高（下の階の床 → この階の床）と天井高。
    # ★階高は物件ごとに変わる（物件A 2,850・物件B 3,000）。比べていなかったため、物件B で気づけなかった（2026-11-03）
    def jp(name: str) -> str:            # 「2F」→「2階」
        return f"{name[:-1]}階" if name.endswith("F") and name[:-1].isdigit() else name
    for lower, upper in zip(levels, levels[1:]):
        lo, up = jp(lower["name"]), jp(upper["name"])
        pairs.append(("levels.story_height_mm", upper["fl_mm"] - lower["fl_mm"], f"{lo}の階高（{lo}の床→{up}の床）"))
        if "ceiling_mm" in upper:
            pairs.append(("levels.ceiling_height_mm", upper["ceiling_mm"], f"{up}の天井高"))
    # 階高は物件ごとに変わる（作者 2026-11-03：基礎高・壁厚は同じだが、階高は物件で変わる）。
    # 会社ルールの選択肢にある値なら食い違いにしない。模型はいつも glb の値で作る
    choices = (rules.get("levels") or {}).get("story_height_choices_mm") or []
    for key, measured, label in pairs:
        expected = rule(rules, key)
        row = {"item": label, "glb_mm": measured, "rule": key, "rule_mm": expected, "match": measured == expected}
        if key == "levels.story_height_mm" and measured != expected and measured in choices:
            row["match"] = True
            row["note"] = f"階高は物件ごとに変わる（会社ルールの選択肢：{'・'.join(f'{c:,}' for c in choices)}）。glb の値を使う"
        out.append(row)
    return out


# ---------------------------------------------------------------- 壁と部屋（段階2b）

NOT_FINISH_CODES = ("HABA", "MAWA")   # 巾木・廻り縁：裏面が部屋の内側を向くので使わない（SPEC §4-1）
NOT_FLOOR_CODES = ("HABA", "MAWA", "TENJ")


def _code_endswith(code: str | None, suffixes: tuple[str, ...]) -> bool:
    return bool(code) and code.endswith(suffixes)


RAISED_MAX_MM = 600.0   # 小上がり（床が FL より高い部屋）とみなす床の高さの上限 (推定。物件C は FL+300・物件D は FL+350)


def _floor_triangles(part: Part, max_z: float) -> list[Polygon]:
    """部屋の床の水平な三角形（高さ max_z 以下。巾木・廻り縁・天井は除く）。"""
    polys = []
    for pr in part.primitives:
        if _code_endswith(pr.code, NOT_FLOOR_CODES):
            continue
        f = pr.vertices[pr.triangles]
        if not len(f):
            continue
        flat = (np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM) & (f[:, 0, 2] <= max_z)
        for t in f[flat]:
            p = Polygon(t[:, :2])
            if p.area > 1:
                polys.append(p)
    return polys


def _raised_z(part: Part, fl_mm: float) -> float | None:
    """FL+100 までに床がない部屋の、小上がりの床の高さ（FL+100〜FL+600 の水平な床の面で、いちばん低い所）。
    ★11/4 物件C・D：小上がりの和室が「床は FL+100 まで」の決まりで部屋ごと落ち、畳の所が消えていた。"""
    zs = []
    for pr in part.primitives:
        if _code_endswith(pr.code, NOT_FLOOR_CODES):
            continue
        f = pr.vertices[pr.triangles]
        if not len(f):
            continue
        flat = np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM
        zs += [float(z) for z in f[flat, 0, 2] if fl_mm + 100 < z <= fl_mm + RAISED_MAX_MM]
    return min(zs) if zs else None


def _add_tatami(floors: dict, rooms: list[Part], model: GlbModel, fl: float, band: tuple = (None, None)) -> None:
    """小上がりの部屋の床に、畳の面（typeETata）を足す。
    ★11/4 物件C・D：畳の所は部屋の床ではなく畳の部材で書き出され、部屋の床の面は畳のまわりの細い所だけだった（C 0.81m²・D 0.01m²）。
    足すのは、高さが小上がりの床と同じ（±30mm）で、半分以上がその部屋の範囲に入る畳だけ。"""
    tatami = []
    for p in model.by_type("typeETata"):
        f = p.faces()
        flat = f[np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM] if len(f) else f
        if not len(flat):
            continue
        z = float(np.min(flat[:, 0, 2]))
        if band[0] is not None and not (band[0] <= z < band[1]):
            continue
        poly = unary_union([q for q in (Polygon(t[:, :2]) for t in flat) if q.area > 1])
        if not poly.is_empty:
            tatami.append((poly, z))
    if not tatami:
        return
    for p in rooms:
        rz = None if _floor_triangles(p, fl + 100) else _raised_z(p, fl)
        if rz is None:
            continue
        hull = MultiPoint(p.faces().reshape(-1, 3)[:, :2]).convex_hull     # 部屋の部材の広がり（壁の仕上げ面も含む）
        add = [poly for poly, z in tatami if abs(z - rz) <= 30 and poly.intersection(hull).area > 0.5 * poly.area]
        if add:
            floors[p.source] = (unary_union([floors[p.source]] + add)
                                .buffer(0.5, join_style="mitre").buffer(-0.5, join_style="mitre"))


def _room_floor(part: Part, fl_mm: float) -> Polygon:
    """部屋の床の形（FL＋100mm 以下の水平な面を合わせたもの。玄関の土間も含む）。
    FL＋100mm 以下に床がなければ、小上がりの床（_raised_z の高さの面）。"""
    polys = _floor_triangles(part, fl_mm + 100)
    if not polys:
        rz = _raised_z(part, fl_mm)
        if rz is not None:
            polys = _floor_triangles(part, rz + FLAT_TOL_MM)
    if not polys:
        return Polygon()
    return unary_union(polys).buffer(0.5, join_style="mitre").buffer(-0.5, join_style="mitre")


LOW_FACE_MM = 300.0   # FL からこの高さまでしかない面（上がり框の段など）は壁の仕上げ面にしない


def _finish_triangles(parts: list[Part], fl_mm: float, band: tuple = (None, None)) -> list:
    """部屋の壁の仕上げ面（垂直な三角形）。巾木・廻り縁と、床近くの低い面（段・框）は除く。
    band はその階の高さの帯（2階建てで、ほかの階の面が混ざらないように切る。平屋は切らない）。"""
    tris = []
    for p in parts:
        for pr in p.primitives:
            if _code_endswith(pr.code, NOT_FINISH_CODES) or not len(pr.triangles):
                continue
            f = wl.clip_faces_z(pr.vertices[pr.triangles], *band)
            f = f[f[:, :, 2].max(axis=1) > fl_mm + LOW_FACE_MM]
            tris += wl.vertical_triangles(f, p.source)
    return tris


def _runs_along(u: wl.Wall, fp: Polygon, share: float = 0.5) -> bool:
    """細い物（上がり框など）が隙間の中を通り、隙間の長さの半分以上に沿っているか。
    ★物件B：框（幅 146mm）が隙間（幅 352mm）より細く、面積の割合だけでは框の位置と分からなかった（U1）。"""
    pts = shapely.get_coordinates(u.rect().intersection(fp))
    if not len(pts) or u.length <= 0:
        return False
    t = (pts - u.start[:2]) @ u.direction[:2]
    return float(t.max() - t.min()) / u.length >= share


def _drop_lower_gable_faces(faces: np.ndarray, lower_roofs: list[dict]) -> np.ndarray:
    """上の階の帯に入った外壁の面のうち、下の屋根（下屋）の下にあるもの（1階の妻の三角のかけら）を除く。
    ★物件B：1階の東・西の妻は、下屋の屋根の下の面まで（3,631〜3,958）あり、2階の床（3,624）より上に出ている。"""
    if not lower_roofs or not len(faces):
        return faces
    keep = np.ones(len(faces), dtype=bool)
    for r in lower_roofs:
        fp = Polygon(r["footprint"]).buffer(60, join_style="mitre")
        bp = r["bottom_plane"]
        for i, t in enumerate(faces):
            if not keep[i]:
                continue
            cx, cy = t[:, 0].mean(), t[:, 1].mean()
            if fp.contains(Point(cx, cy)) and t[:, 2].max() <= bp["a"] * cx + bp["b"] * cy + bp["c"] + 30:
                keep[i] = False
    return faces[keep]


def _drop_lower_gable_walls(walls: list, lower_roofs: list[dict]) -> tuple[list, list]:
    """上の階の外壁のうち、下の屋根（下屋）の範囲の中にあって、上端が下屋の屋根の下の面より高くないもの
    （1階の妻の三角のかけら）を除く。三角形ごとに見ると、2階の壁と同じ平面の大きな三角形にまたがって外せないため、
    作り直した壁ごとに見る（★物件B の東・西の妻）。戻り値は（残す壁, 除いた壁）。"""
    keep, dropped = [], []
    for w in walls:
        gable = False
        if w.kind == "exterior" and w.top_glb is not None:
            mx, my = (w.start + w.end) / 2
            for r in lower_roofs:
                bp = r["bottom_plane"]
                if not Polygon(r["footprint"]).buffer(60, join_style="mitre").contains(Point(mx, my)):
                    continue
                zmax = max(bp["a"] * p[0] + bp["b"] * p[1] + bp["c"] for p in (w.start, w.end))
                if w.top_glb <= zmax + 30:
                    gable = True
                    break
        (dropped if gable else keep).append(w)
    return keep, dropped


def _extend_on_lower_roof(walls: list, lower_roofs: list[dict], fl: float) -> None:
    """下の屋根（下屋）の上から始まる上の階の外壁は、その階の床まで下ろす（上の階の部品を閉じた箱にする）。
    ★物件B の2階の南の壁は、glb では下屋の屋根の上（4,049）から始まっていた。下ろした分は下屋の屋根裏に隠れる。"""
    for w in walls:
        if w.kind != "exterior" or w.bottom <= fl + LOW_FACE_MM:
            continue
        mx, my = (w.start + w.end) / 2
        for r in lower_roofs:
            tp = r.get("top_plane")
            if not tp or not Polygon(r["footprint"]).buffer(200, join_style="mitre").contains(Point(mx, my)):
                continue
            if abs(w.bottom - (tp["a"] * mx + tp["b"] * my + tp["c"])) <= 40:
                w.assumptions.append(f"on_lower_roof: glb では下屋の屋根の上（{w.bottom:.0f}）から始まる壁。"
                                     f"この階の床（{fl:.0f}）まで下ろした（下ろした分は下屋の屋根裏に隠れる）")
                w.bottom = fl
                break


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


def reconstruct(model: GlbModel, level: dict, rules: dict, band: tuple = (None, None),
                parts: dict | None = None, lower_roofs: list[dict] | None = None) -> dict:
    """1つの階の壁と部屋を作り直す。
    band：外壁・仕上げ面を切る高さの帯（平屋は (None, None)＝切らない）。
    parts：その階の部材（tate・rooms・standalone・kaun・agar・stairs）。平屋は None（全部）。
    lower_roofs：この階より下の屋根（下屋）。2階で、1階の妻のかけらを除き、下屋の上から始まる壁を床まで下ろす。"""
    fl = float(level["fl_mm"])
    t_ext = float(rule(rules, "walls.exterior_thickness_mm"))
    t_int = float(rule(rules, "walls.interior_thickness_mm"))
    sel = parts or {}
    lower_roofs = [r for r in (lower_roofs or []) if r.get("bottom_plane") and r.get("footprint")]

    # 開口（建具の穴）とドア（床まで届く建具）。穴の形は openings.hole（吊り戸のレール・取っ手は除く）
    tate = sel.get("tate", model.by_type("typeETate"))
    opening_fps, door_fps = [], []
    for p in tate:
        fp, sill, _ = op.hole(p)
        opening_fps.append(fp)
        if sill <= fl + 100:
            door_fps.append(fp)

    # 手順1：外壁、手順4：単独の壁
    ext_tris = []
    for p in model.by_role("exterior_wall"):
        f = _drop_lower_gable_faces(wl.clip_faces_z(p.faces(), *band), lower_roofs)
        ext_tris += wl.vertical_triangles(f, p.source)
    ext_planes = wl.group_planes(ext_tris)
    # 2階建ては、60mm より短い外壁のかけらを捨てる（★物件B：ドアの見込み 21mm から外壁ができ、外形が 32mm 広がった）。
    # 平屋は今までどおり（物件A のいちばん短い壁は 617mm）
    ext = wl.exterior_walls(ext_planes, t_ext, fl, opening_fps, **({} if band == (None, None) else {"min_len": 60.0}))
    if lower_roofs:
        ext, _ = _drop_lower_gable_walls(ext, lower_roofs)
        _extend_on_lower_roof(ext, lower_roofs, fl)
        ext = wl.merge_exterior_pieces(ext)    # 床まで下ろした壁と、同じ面の続きの壁を1枚に
    wl.fill_inside_corners(ext)
    ceiling_abs = fl + float(level.get("ceiling_mm", rule(rules, "levels.ceiling_height_mm")))
    # ★背中合わせの外壁の面は、実際は内壁（物件Aで 作者確認）。内壁の厚み・天井までで作る
    ext = wl.merge_back_to_back(ext, t_ext, as_kind="interior", new_thickness=t_int, new_top=ceiling_abs)
    back_to_back = [w for w in ext if w.kind == "interior"]
    ext = [w for w in ext if w.kind == "exterior"]
    standalone, skipped = [], []
    stairs = [wl.bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in p.primitives])) for p in sel.get("stairs", [])]
    for p in sel.get("standalone", model.by_role("standalone_wall")):
        w = wl.standalone_wall(p.faces(), p.source)
        if w is None:
            skipped.append({"source": p.source, "reason": "垂直な面がほとんどないか、高さが300mm未満（上端・下端の板や笠木）のため壁にしない"})
        elif any(w.rect().intersection(s).area / w.rect().area >= 0.5 for s in stairs):
            skipped.append({"source": p.source, "reason": "階段まわりの壁・手すり（造作）なので壁にしない"})
        else:
            standalone.append(w)
    # 背中合わせの壁の端の小口の面なども、背中合わせから作った壁に覆われていれば除く
    outer = [w for w in wl.drop_covered(ext + standalone + back_to_back, keep_kinds=("standalone", "interior"))
             if w.kind != "interior"]
    exterior_fp = wl.footprint(outer)

    # 手順2・3・6：部屋の内側 → 内壁
    rooms = sel.get("rooms", model.by_role("room"))
    finish_planes = wl.group_planes(_finish_triangles(rooms, fl, band))
    floors = {p.source: _room_floor(p, fl) for p in rooms}
    _add_tatami(floors, rooms, model, fl, band)
    # 上の階：床の穴（階段・吹き抜け）を見つけ、内壁を探すときは仮の部屋として足す（穴と部屋の間の壁が消えないように）
    voids = _floor_voids(exterior_fp, [f for f in floors.values() if not f.is_empty], stairs) if band[0] is not None else []
    rooms_inner, region = wl.interior_region([f for f in floors.values() if not f.is_empty] + [v["polygon"] for v in voids],
                                             finish_planes, door_fps, exterior_fp)
    interior = wl.interior_walls(finish_planes, region, t_int)
    if voids:  # 穴のまわりの手すり壁（天井まで届かない低い壁）は造作として省く
        holes = unary_union([v["polygon"] for v in voids]).buffer(200, join_style="mitre")
        for w in list(interior):
            if w.top is not None and w.top < fl + 1500 and w.rect().intersects(holes):
                interior.remove(w)
                skipped.append({"source": (w.sources or ["glb"])[0], "reason": "床の穴（階段）のまわりの手すり壁（造作）なので壁にしない"})
    zone = unary_union([region, exterior_fp] + door_fps).buffer(5, join_style="mitre")
    for _ in range(2):  # つないだ後にもう一度：つながった壁の端も、ぶつかる壁まで伸ばす
        wl.extend_to_junctions(interior, outer + interior, zone=zone)
        interior = wl.merge_collinear(interior, region)
    kept = wl.drop_covered(outer + interior, keep_kinds=("exterior", "standalone"), ratio=0.5)
    interior = [w for w in kept if w.kind == "interior"]
    # 背中合わせから作った壁は後回し：向かい合う仕上げ面から作った内壁で覆われていれば、そちらを使う。
    # ★単独の壁（袖壁）で覆われていれば、単独の壁だけを作る（物件E：ポーチの袖壁が、単独の壁と
    #   「背中合わせの外壁の面」の両方で入っていて、同じ所に壁が2枚でき、部品どうしが重なった。11/3）
    inner_fp = unary_union([w.rect() for w in interior]) if interior else Polygon()
    std_fp = unary_union([w.rect() for w in standalone]) if standalone else Polygon()
    for b in back_to_back:
        r = b.rect()
        if r.intersection(inner_fp).area / r.area < 0.5 and r.intersection(std_fp).area / r.area < 0.5:
            interior.append(b)
    for w in interior:  # 巾木の上から始まっている内壁は、床（FL）から立てる
        if w.bottom <= fl + LOW_FACE_MM:
            w.bottom = fl

    # 腰掛け・カウンター（造作）と重なる低い部分は壁にしない（SPEC §1：造作は作らない）
    for p in sel.get("kaun", model.by_type("typeEKaun")):
        lo, hi = p.bounds()
        fp = wl.bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in p.primitives]))
        for w in list(interior):
            r = w.rect()
            if w.top is not None and w.top <= hi[2] + 50 and r.intersection(fp).area / r.area >= 0.5:
                interior.remove(w)
                skipped.append({"source": p.source, "reason": "腰掛け・カウンターの部分（造作）なので壁にしない"})

    # glb の例外の記録（検品の材料）
    exceptions = []
    for b in back_to_back:
        merged_into = b if b in interior else max(interior + standalone, key=lambda w: w.rect().intersection(b.rect()).area)
        how = "単独の壁（袖壁）と同じ所なので、単独の壁だけを作った" if merged_into.kind == "standalone" else "内壁として扱った"
        exceptions.append({"kind": "back_to_back_exterior_faces", "start": b.start, "end": b.end, "wall": merged_into,
                           "note": f"glb では背中合わせの外壁の面（間 {getattr(b, '_back_to_back_gap', 0):.0f}mm）。{how}"})
        if merged_into is not b:
            merged_into.assumptions.append("back_to_back: glb で背中合わせの外壁の面として入っていた壁を、この内壁にまとめた")

    unresolved = wl.unresolved_regions(region, interior, t_int, door_fps)
    # 上がり框の位置の隙間：壁はない（物件Aで 作者確認）。未解決にせず、例外として記録だけ残す
    # 階段の範囲の中の隙間：階段まわりの壁・手すりは造作なので作らない。未解決にせず、例外として記録だけ残す
    for u in list(unresolved):
        r = u.rect()
        if any(r.intersection(s).area / r.area >= 0.5 for s in stairs):
            unresolved.remove(u)
            exceptions.append({"kind": "stairs_area", "start": u.start, "end": u.end, "wall": None,
                               "note": "階段の範囲の中。階段まわりの壁・手すりは造作なので作らない"})
    agar_parts = sel.get("agar", model.by_type("typeEAgar"))
    agari = [wl.bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in p.primitives])).buffer(50, join_style="mitre")
             for p in agar_parts]
    for u in list(unresolved):
        r = u.rect()
        for p, fp in zip(agar_parts, agari):
            if r.intersection(fp).area / r.area >= 0.5 or _runs_along(u, fp):
                unresolved.remove(u)
                exceptions.append({"kind": "agari_kamachi", "start": u.start, "end": u.end, "wall": None,
                                   "source": p.source, "note": "上がり框の位置。壁は作らない（框・靴箱などの造作は模型に入れない）"})
                break

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
    # 手順5：開口（どの壁の穴か）。屋根は build でまとめて読む（階をまたぐため）
    opening_list, orphans = op.openings(tate, all_walls, fl)
    exceptions += op.snap_hanging_doors(opening_list, fl)
    op.snap_door_heads(opening_list, fl, float(rule(rules, "openings.door_top_fl_mm")))
    return {"walls": all_walls, "rooms": room_list, "skipped": skipped + orphans, "stats": stats,
            "unresolved": unresolved, "exceptions": exceptions, "openings": opening_list,
            "region": region, "exterior_fp": exterior_fp, "voids": voids}


def _floor_voids(exterior_fp, floors: list, stairs: list) -> list[dict]:
    """上の階の床の穴（外壁の内側で、部屋の床がない所）。階段に重なれば「階段」、そうでなければ「吹き抜け」。
    ★物件B の2階は、階段の穴（L字、約3.4m²）だけ。"""
    if exterior_fp.is_empty:
        return []
    big = max(getattr(exterior_fp, "geoms", [exterior_fp]), key=lambda g: g.area)
    inside = Polygon(big.exterior).difference(exterior_fp)
    rest = inside.difference(unary_union(floors)) if floors else inside
    rest = rest.buffer(-50, join_style="mitre").buffer(50, join_style="mitre")
    out = []
    for g in getattr(rest, "geoms", [rest]):
        if g.is_empty or g.area < 0.3e6:
            continue
        kind = "stairwell" if any(g.intersects(s) for s in stairs) else "void"
        out.append({"kind": kind, "polygon": g})
    return out


def _min_floor_z(model: GlbModel, source: str, fl: float) -> int:
    part = next(p for p in model.parts if p.source == source)
    zs = [z for z in _flat_heights([part]) if z <= fl + 100]
    if zs:
        return min(zs)
    rz = _raised_z(part, fl)          # 小上がり：その床の高さ（組み立てで FL から台を作る）
    return int(round(rz)) if rz is not None else int(fl)


def _xy(p) -> list[float]:
    return [round(float(p[0]), 1), round(float(p[1]), 1)]


def _poly_coords(poly) -> list:
    geoms = getattr(poly, "geoms", [poly])
    big = max(geoms, key=lambda g: g.area)
    return [_xy(c) for c in big.simplify(1.0).exterior.coords[:-1]]


def walls_to_json(walls: list[wl.Wall], level, level_of: dict[int, int] | None = None) -> tuple[list[dict], list[dict], dict[int, str]]:
    """壁を building.json の形にする。3つ目の戻り値は「壁の物 → 番号」の対応（例外の記録に使う）。
    level は階の名前（全部同じ階のとき）か、名前の一覧（level_of で壁ごとの階の番号を引く。2階建て）。
    番号は階の順（1階が先）→ 種類 → 位置の順で、全部の階を通して振る。"""
    order = {"exterior": 0, "standalone": 1, "interior": 2}
    li = (lambda w: level_of[id(w)]) if level_of is not None else (lambda w: 0)
    walls = sorted(walls, key=lambda w: (li(w), order[w.kind], round(w.start[1]), round(w.start[0])))
    out, assumptions, ids = [], [], {}
    for i, w in enumerate(walls, 1):
        wid = f"W{i}"
        ids[id(w)] = wid
        name = level if isinstance(level, str) else level[li(w)]
        d = {"id": wid, "level": name, "kind": w.kind, "start": _xy(w.start), "end": _xy(w.end),
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
    return out, assumptions, ids


def build(alias: str, company: str = "jisha", return_debug: bool = False):
    files = property_files(alias)
    model = read_glb(glb_working_copy(files))
    rules = load_rules(company)
    ground = detect_ground(model)
    levels = detect_levels(model)
    if len(levels) > 2:
        raise ValueError(f"{len(levels)}階建てはまだ扱えません（2階建てまで）。")
    multi = len(levels) > 1
    names = [lv["name"] for lv in levels]
    t_ext = float(rule(rules, "walls.exterior_thickness_mm"))

    # 階ごとに作り直す。2階建ては、屋根の面と形を先に読み（軒の出は後で）、2階に「下の屋根（下屋）」を渡す
    roof_parts = merge_roof_parts(model.by_role("roof"))     # 面ごとに分かれた切妻・寄棟の部材を1つの屋根に
    pre_roofs =[detect_roof(p, box(0, 0, 1, 1)) for p in roof_parts] if multi else []
    for r in pre_roofs:
        r["_li"] = _roof_level(r, levels)
    bands = _level_bands(levels)
    recs = []
    for i, lv in enumerate(levels):
        if multi:
            recs.append(reconstruct(model, lv, rules, band=bands[i], parts=_level_parts(model, levels, i),
                                    lower_roofs=[r for r in pre_roofs if r["_li"] < i]))
        else:
            recs.append(reconstruct(model, lv, rules))
    rec = recs[0]

    # 基礎の外形。★物件B は天端の帯が玄関ドアの所で切れていたので、玄関ドアの穴で輪を閉じる
    fl0 = float(levels[0]["fl_mm"])
    bridges = []
    for p in model.by_type("typeETate"):
        fp, sill, _ = op.hole(p)
        if op.classify(p, sill, fl0)[0] == "entrance_door":
            bridges.append(fp)
    found = xp.foundation(model.by_role("foundation"), ground["foundation_top_mm"], bridges=bridges,
                          clip=_filled(rec["exterior_fp"]).buffer(1, join_style="mitre"))
    porch_list = xp.porches(model.by_role("porch"))
    porch_union = unary_union([Polygon(p["polygon"]) for p in porch_list])
    # 単独の壁で、片側が家の中・反対側が外のものは外壁の一部 → 外壁の厚み（作者 2026-11-03）。
    # 「家の中」＝基礎の外形の内側からポーチを除いた範囲（部屋の床だけだと、窓際の腰掛けのくぼみが外に見える）。上の階はその階の外形
    if found.get("outline"):
        inside = Polygon(found["outline"]).difference(porch_union)
        wl.envelope_standalone(rec["walls"], inside, t_ext)
    for r in recs[1:]:
        wl.envelope_standalone(r["walls"], _filled(r["exterior_fp"]), t_ext)

    # ポーチの天井・下がり天井：「家の中」は、天井と同じ階の部屋の床の範囲で判断する
    # （基礎の外形はポーチ北のへこみも含むため。★物件B のポーチの天井は2階の部屋の真下にある）
    soffit_list, lowered_list = [], []
    for i, r in enumerate(recs):
        sparts = [p for p in model.by_role("porch_soffit") if not multi or _level_index(p.bounds()[1][2], levels) == i]
        indoor = unary_union([rm["polygon"] for rm in r["rooms"]])
        ceiling_z = float(levels[i]["fl_mm"]) + float(levels[i].get("ceiling_mm", rule(rules, "levels.ceiling_height_mm")))
        s, skipped_s, low = xp.soffits(sparts, indoor, ceiling_z)
        r["skipped"] += skipped_s
        soffit_list += [{**d, "level": names[i]} if multi else d for d in s]
        lowered_list += [{**d, "level": names[i]} if multi else d for d in low]
    if multi:  # 階をまたいで番号を振り直す
        soffit_list = [{**d, "id": f"S{k}"} for k, d in enumerate(soffit_list, 1)]
        lowered_list = [{**d, "id": f"C{k}"} for k, d in enumerate(lowered_list, 1)]

    # 壁の番号は、全部の階を通して1回で振る
    if multi:
        level_of = {id(w): i for i, r in enumerate(recs) for w in r["walls"]}
        walls_json, assumptions, wall_ids = walls_to_json([w for r in recs for w in r["walls"]], names, level_of)
    else:
        walls_json, assumptions, wall_ids = walls_to_json(rec["walls"], levels[0]["name"])
    order = {"window": 0, "entrance_door": 1, "door": 2}
    tagged = [(o, i) for i, r in enumerate(recs) for o in r["openings"]]
    tagged.sort(key=lambda t: (order[t[0]["kind"]], wall_ids[id(t[0]["wall"])], t[0]["offset_mm"]))
    openings_json, op_ids = [], {}
    for k, (o, i) in enumerate(tagged, 1):
        op_ids[id(o)] = f"O{k}"
        openings_json.append({"id": f"O{k}", "wall": wall_ids[id(o["wall"])], "kind": o["kind"], "style": o["style"],
                              "offset_mm": round(o["offset_mm"], 1), "width_mm": round(o["width_mm"], 1),
                              "sill_mm": round(o["sill_mm"]), "head_mm": round(o["head_mm"]),
                              "sill_fl_mm": round(o["sill_fl_mm"]), "head_fl_mm": round(o["head_fl_mm"]),
                              "center": _xy(o["center"]), "source": o["source"], "level": names[i],
                              **({"sources": o["sources"]} if o.get("sources") else {}),
                              **({"glb_original": o["glb_original"]} if o.get("glb_original") else {}),
                              **({"assumption": o["assumptions"]} if o.get("assumptions") else {})})
    exceptions_json = []
    for i, r in enumerate(recs):
        for e in r["exceptions"]:
            if "opening" in e:   # 開口の例外（FL+400 から始まる戸など）
                d = {"kind": e["kind"], "level": names[i], "opening": op_ids.get(id(e["opening"])), "note": e["note"]}
            else:
                d = {"kind": e["kind"], "level": names[i], "start": _xy(e["start"]), "end": _xy(e["end"]), "note": e["note"]}
                if e.get("wall") is not None:
                    d["wall"] = wall_ids.get(id(e["wall"]))
            if e.get("source"):
                d["source"] = e["source"]
            exceptions_json.append(d)
    for oj in openings_json:
        for a in oj.get("assumption", []):
            assumptions.append({"field": f"openings[{oj['id']}]", "note": a})
    rooms_sorted = [(rm, i) for i, r in enumerate(recs) for rm in sorted(r["rooms"], key=lambda rm: rm["source"])]
    rooms_json = [{"id": f"R{k}", "level": names[i], "polygon": _poly_coords(rm["polygon"]),
                   "floor_mm": rm["floor_mm"], "area_m2": round(rm["polygon"].area / 1e6, 2), "source": rm["source"]}
                  for k, (rm, i) in enumerate(rooms_sorted, 1)]
    unresolved = [(u, i) for i, r in enumerate(recs) for u in r["unresolved"]]

    # 屋根。平屋は今までどおり1つ（roof）。2階建ては屋根ごとに読み、roofs に並べる（roof は大屋根）
    if not roof_parts:
        roof, roofs = {"shape": "unknown", "note": "屋根（typeEYane）がない"}, []
    elif not multi:
        main_part = main_roof_part(roof_parts)
        roof = detect_roof(main_part, rec["exterior_fp"])
        if len(roof_parts) > 1:
            roof["note"] = f"屋根の部材が {len(roof_parts)} 個ある。いちばん大きい屋根だけを模型にした"
            exceptions_json.append({"kind": "extra_roofs", "source": main_part.source,
                                    "note": f"屋根の部材がほかに {len(roof_parts) - 1} 個あるが、模型には入れていない（庇・離れた屋根など (推定)）"})
        roofs = [{"id": "Y1", "level": names[0], "kind": "main", **roof}]
    else:
        roof, roofs = _multi_roofs(roof_parts, pre_roofs, recs, levels)
    for n in _attach_soffits(model.by_role("eave_soffit"), roofs):
        exceptions_json.append({"kind": "eave_soffit", "source": "glb:typeESnok", "note": n})
    main = next((r for r in roofs if r.get("kind") == "main"), None)
    if main is not None:
        roof.update({k: main[k] for k in ("soffits", "soffit_z_mm") if k in main})

    building = {
        "source": {"glb": files.glb_label, "generator": model.generator, "wih_version": model.version_info,
                   "company_rules": company},
        "ground": ground,
        "levels": levels,
        "walls": walls_json,
        "openings": openings_json,
        "roof": roof,
        "roofs": roofs,                        # 全部の屋根（2階建ては大屋根と下屋）。roof は大屋根
        "foundation": found,
        "porches": porch_list,
        "porch_soffits": soffit_list,
        "lowered_ceilings": lowered_list,      # 部屋の中の下がり天井（その階の天井より低い天井）
        "rooms": rooms_json,
        "wall_stats": rec["stats"],
        "skipped": [s for r in recs for s in r["skipped"]],
        "unresolved": [{"id": f"U{k}", "level": names[i], "kind": "interior_region_without_faces",
                        "start": _xy(u.start), "end": _xy(u.end), "width_mm": round(u.thickness),
                        "note": u.assumptions[0]} for k, (u, i) in enumerate(unresolved, 1)],
        "glb_exceptions": exceptions_json,
        "inventory": inventory(model),
        "rule_checks": compare_with_rules(ground, levels, rules),
        "assumptions": assumptions,
    }
    if multi:
        _add_upper_level_info(building, recs, levels, found)
    return (building, rec) if return_debug else building


def _filled(fp) -> Polygon:
    """壁の形（壁の四角を合わせたもの）の外側の輪で囲んだ面（その階の外形）。"""
    if fp.is_empty:
        return Polygon()
    big = max(getattr(fp, "geoms", [fp]), key=lambda g: g.area)
    return Polygon(big.exterior)


def _level_bands(levels: list[dict]) -> list[tuple]:
    """階ごとの高さの帯（外壁・仕上げ面を切る）。1階は (なし, 2FL)、2階は (2FL, なし)。平屋は (なし, なし)。"""
    out = []
    for i in range(len(levels)):
        lo = None if i == 0 else float(levels[i]["fl_mm"])
        hi = None if i == len(levels) - 1 else float(levels[i + 1]["fl_mm"])
        out.append((lo, hi))
    return out


def _level_index(z: float, levels: list[dict], below: float = 400.0) -> int:
    """高さ z が属する階：FL−below 以上で、いちばん上の階（玄関の土間 384 は 1F）。"""
    idx = 0
    for i, lv in enumerate(levels):
        if z >= float(lv["fl_mm"]) - below:
            idx = i
    return idx


def _room_floor_z(p: Part) -> float:
    """部屋の部材の床の高さ（天井・巾木・廻り縁を除く水平な面のいちばん低い所）。"""
    zs = []
    for pr in p.primitives:
        if _code_endswith(pr.code, NOT_FLOOR_CODES) or not len(pr.triangles):
            continue
        f = pr.vertices[pr.triangles]
        zs += list(f[np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM, 0, 2])
    return float(min(zs)) if zs else float(p.bounds()[0][2])


def _level_parts(model: GlbModel, levels: list[dict], i: int) -> dict:
    """その階の部材（建具・部屋・単独の壁・カウンター・上がり框）と、階段（全部の階で使う）。"""
    def mid(p):
        _, s, h = op.hole(p)
        return (s + h) / 2
    return {
        "tate": [p for p in model.by_type("typeETate") if _level_index(mid(p), levels, 0.0) == i],
        "rooms": [p for p in model.by_role("room") if _level_index(_room_floor_z(p), levels) == i],
        "standalone": [p for p in model.by_role("standalone_wall") if _level_index(p.bounds()[0][2], levels, 100.0) == i],
        "kaun": [p for p in model.by_type("typeEKaun") if _level_index(p.bounds()[0][2], levels) == i],
        "agar": [p for p in model.by_type("typeEAgar") if _level_index(p.bounds()[0][2], levels) == i],
        "stairs": model.by_type("typeEKais"),
    }


def main_roof_part(roof_parts: list[Part]) -> Part:
    """平屋の家の屋根：上の面がいちばん広い部材（★11/4 の見直し：前は1つ目を使い、玄関の庇が先に並ぶ glb だと
    庇を家の屋根にするおそれがあった）。"""
    from adapters.roof import _top_extent
    return max(roof_parts, key=lambda p: (_top_extent(p)[0].area if _top_extent(p)[0] is not None else 0.0))


def _roof_level(roof: dict, levels: list[dict]) -> int:
    """屋根が載る階：屋根の平面の中ほどでの下の面の高さの、すぐ下に天井がある階（下屋は 1F、大屋根は 2F）。
    ★11/4 の見直し：前は軒先を含む「いちばん低い所」で測っていた。軒の出が深い・勾配が急な2階建ての大屋根は、軒先が
    2階の天井より下に来るので1階の屋根と取り違え、2階の外壁を「1階の妻のかけら」として消すおそれがあった。"""
    from core.builder import plane_z
    from core.roof_geom import bottom_surface
    if not roof.get("footprint") or not (roof.get("bottom_plane") or roof.get("planes")):
        return len(levels) - 1
    p = Polygon(roof["footprint"]).representative_point()      # 平面の中の点（L字でも外に出ない）
    z = plane_z(bottom_surface(roof), p.x, p.y)                # 面が複数の屋根は、その点を受け持つ面で測る
    idx = 0
    for i, lv in enumerate(levels):
        if float(lv["fl_mm"]) + float(lv.get("ceiling_mm", 2400)) <= z + 1:
            idx = i
    return idx


SOFFIT_SIDE_TOL_MM = 30.0   # 軒天の高さが辺ごとにこれ以上違えば、注記に残す


def _attach_soffits(parts: list[Part], roofs: list[dict]) -> list[str]:
    """軒天の部材（typeESnok）を、重なりのいちばん大きい屋根に結びつける。屋根には soffits（辺・水平な面の形・出どころ）と
    soffit_z_mm（軒天の高さ、GL から）を書く。★11/4 物件C：軒天を知らない種類として捨てていた（作者「軒天がない」）。"""
    notes = []
    by_roof: dict[int, list[tuple[dict, float]]] = {}
    for p in parts:
        f = p.faces()
        flat = f[np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM] if len(f) else f
        if not len(flat):
            continue
        z = float(np.min(flat[:, 0, 2]))
        low = flat[np.abs(flat[:, 0, 2] - z) < FLAT_TOL_MM]          # いちばん下の水平な面＝軒天の板の下の面
        poly = unary_union([q for q in (Polygon(t[:, :2]) for t in low) if q.area > 1])
        if poly.is_empty:
            continue
        best, k_best = 0.0, None
        for k, r in enumerate(roofs):
            if not r.get("footprint"):
                continue
            a = poly.intersection(Polygon(r["footprint"])).area
            if a > best:
                best, k_best = a, k
        if k_best is None:
            notes.append(f"軒天 {p.source} は、どの屋根にも重ならないので使わなかった")
            continue
        c, rc = poly.centroid, Polygon(roofs[k_best]["footprint"]).centroid
        dx, dy = c.x - rc.x, c.y - rc.y
        side = ("E" if dx > 0 else "W") if abs(dx) > abs(dy) else ("N" if dy > 0 else "S")
        geom = max(getattr(poly, "geoms", [poly]), key=lambda g: g.area)
        by_roof.setdefault(k_best, []).append(({"side": side, "polygon": [[round(float(x), 1), round(float(y), 1)]
                                                for x, y in list(geom.exterior.coords)[:-1]], "source": p.source}, z))
    for k, items in by_roof.items():
        zs = sorted(z for _, z in items)
        roofs[k]["soffits"] = [it for it, _ in items]
        roofs[k]["soffit_z_mm"] = int(round(zs[len(zs) // 2]))
        if zs[-1] - zs[0] > SOFFIT_SIDE_TOL_MM:
            notes.append(f"{roofs[k].get('id')} の軒天の高さが辺ごとに {zs[-1] - zs[0]:.0f}mm 違う。真ん中の値 {roofs[k]['soffit_z_mm']} にそろえた")
    return notes


def _multi_roofs(roof_parts: list[Part], pre_roofs: list[dict], recs: list[dict], levels: list[dict]) -> tuple[dict, list[dict]]:
    """2階建ての屋根。屋根ごとに、その屋根が載る階の外壁（屋根の下にある分）と比べて軒の出を測る。
    いちばん上の階の屋根が大屋根（main）、ほかは下屋（lean_to）。"""
    found = []
    for p, pre in zip(roof_parts, pre_roofs):
        li = pre["_li"]
        fp_level = recs[li]["exterior_fp"]
        under = fp_level.intersection(Polygon(pre["footprint"])) if pre.get("footprint") else fp_level
        r = detect_roof(p, under if not under.is_empty else fp_level)
        found.append((li, r))
    found.sort(key=lambda t: (t[0], (t[1].get("top_z_range_mm") or [0, 0])[1]))
    top_li = max(li for li, _ in found)
    roofs = []
    for k, (li, r) in enumerate(found, 1):
        kind = "main" if li == top_li and k == len(found) else "lean_to"
        roofs.append({"id": f"Y{k}", "level": levels[li]["name"], "kind": kind, **r})
    main = next(r for r in roofs if r["kind"] == "main")
    return {k: v for k, v in main.items() if k not in ("id", "level", "kind")}, roofs


def _add_upper_level_info(b: dict, recs: list[dict], levels: list[dict], found: dict) -> None:
    """上の階の外形・床の穴・はね出しを levels に書き、はね出しは glb の例外にする。階ごとの壁の数値も足す。"""
    base = Polygon(found["outline"]) if found.get("outline") else _filled(recs[0]["exterior_fp"])
    b["wall_stats_by_level"] = {lv["name"]: r["stats"] for lv, r in zip(levels, recs)}
    for i in range(1, len(levels)):
        lv, r = levels[i], recs[i]
        outline = _filled(r["exterior_fp"])
        lv["outline"] = _poly_coords(outline)
        lv["floor_openings"] = [{"id": f"H{k}", "kind": v["kind"], "polygon": _poly_coords(v["polygon"]),
                                 "area_m2": round(v["polygon"].area / 1e6, 2)} for k, v in enumerate(r["voids"], 1)]
        over = outline.difference(base.buffer(1, join_style="mitre"))
        if over.area >= 0.5e6:
            b["glb_exceptions"].append({"kind": "upper_floor_overhang", "level": lv["name"],
                                        "area_m2": round(over.area / 1e6, 2), "polygon": _poly_coords(over),
                                        "note": f"{lv['name']}の一部（{over.area / 1e6:.2f}m²）が、1階の外（ポーチなど）の上に出ている（はね出し）。"
                                                "1階の部品に、厚みのある塊として作る（作者 11/3）"})


def write_building(alias: str, building: dict, fresh: bool = False) -> Path:
    """building.json を書き出す。前回のものがあれば番号を引き継ぎ（core/ids.py）、前回の分は .prev に残す。
    fresh=True なら前回を使わず、番号を振り直す（物件B：平屋として作った前回の番号と混ざらないように）。"""
    path = out_dir(alias) / "building.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() and not fresh else None
    stabilize(building, previous)
    text = json.dumps(building, ensure_ascii=False, indent=2)
    assert_clean(text, "building.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_suffix(".prev.json"))
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
        print(f"    {c['item']}: glb {c['glb_mm']} / ルール {c['rule_mm']} → {mark}" + (f"（{c['note']}）" if c.get("note") else ""))
    from core.rule_check import check_property
    rows = check_property(b, load_rules(b["source"].get("company_rules", "jisha")))
    print("  会社ルールと比べた結果（壁の厚み・ドアの上端なども。家ごとに変わる値は比べない）:")
    for r in rows:
        print(f"    {r['label']}: " + ("一致" if r["match"] else f"★{r['message']}"))
    s = b.get("wall_stats")
    if s:
        print(f"  壁: {s['count']}  外形 {s['outer_size_mm'][0]}×{s['outer_size_mm'][1]}mm")
        print(f"      外壁の厚み（測った値）{s['exterior_thickness_measured_mm']}mm / 内壁の仕上げ面の間（測った値）{s['interior_gap_measured_mm']}mm")
        print(f"      内壁の範囲 {s['interior_region_m2']}m2 のうち、壁で覆えていない所 {s['interior_region_uncovered_m2']}m2")
        for sk in b.get("skipped", []):
            print(f"      壁にしなかった部材: {sk['source']}（{sk['reason']}）")
        for u in b.get("unresolved", []):
            print(f"      未解決の所 {u['id']}: {u['start']}→{u['end']} 幅{u['width_mm']}mm（壁があるか要確認）")
    ops = b.get("openings", [])
    if ops:
        kinds = collections.Counter(o["kind"] for o in ops)
        print(f"  開口: 窓 {kinds.get('window', 0)} / 玄関ドア {kinds.get('entrance_door', 0)} / 室内のドア・引き戸 {kinds.get('door', 0)}")
        heads = collections.Counter(o["head_fl_mm"] for o in ops if o["kind"] == "window")
        print(f"      窓の上端（FL から）: " + "、".join(f"FL+{h} が {n} か所" for h, n in sorted(heads.items())))
        dheads = collections.Counter(o["head_fl_mm"] for o in ops if o["kind"] == "door")
        print(f"      室内のドアの上端（FL から）: " + "、".join(f"FL+{h} が {n} か所" for h, n in sorted(dheads.items())))
    for r in (b.get("roofs") or [b.get("roof") or {}]):
        if r.get("shape") == "shed":
            name = {"main": "大屋根", "lean_to": "下屋"}.get(r.get("kind"), "屋根")
            print(f"  {name}（{r.get('id', '')} {r.get('level', '')}）: 片流れ {r['pitch'][0]}/10、低い側 {r.get('low_side')}、"
                  f"厚み {r.get('thickness_mm')}mm、軒の出 {r['eave_mm']}（樋を含めると {r.get('eave_with_gutter_mm')}）")
    if len(b["levels"]) > 1:   # 2階建て：階ごとの数
        print("  階ごと:")
        for lv in b["levels"]:
            n = lv["name"]
            wc = collections.Counter(w["kind"] for w in b["walls"] if w["level"] == n)
            oc = collections.Counter(o["kind"] for o in b["openings"] if o.get("level") == n)
            rc = sum(1 for rm in b["rooms"] if rm["level"] == n)
            st = (b.get("wall_stats_by_level") or {}).get(n, {})
            size = st.get("outer_size_mm")
            print(f"    {n}: 壁 {dict(wc)}・開口 {dict(oc)}・部屋 {rc}" + (f"・外形 {size[0]}×{size[1]}mm" if size else ""))
            for h in lv.get("floor_openings", []):
                print(f"      床の穴 {h['id']}（{'階段' if h['kind'] == 'stairwell' else '吹き抜け'}）{h['area_m2']}m²")
        for e in b["glb_exceptions"]:
            if e["kind"] in ("upper_floor_overhang", "door_above_floor"):
                print(f"    glb の例外（{e['kind']}）: {e['note']}")
    print("  部材の数:")
    for r in b["inventory"]:
        print(f"    {r['label']:<12}（{r['type']}）{r['count']:>3} 個 → {r['role']}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Walk in home の glb から building.json を作る")
    ap.add_argument("alias", help="物件の仮名（data/local_paths.yaml の名前。例：物件A）")
    ap.add_argument("--company", default="jisha", help="会社ルール（rules/<名前>.yaml）")
    ap.add_argument("--fresh-ids", action="store_true", help="前回の番号を引き継がず、振り直す")
    args = ap.parse_args(argv)
    building = build(args.alias, args.company)
    path = write_building(args.alias, building, fresh=args.fresh_ids)
    _print_summary(args.alias, building)
    ch = building.get("id_changes", {})
    if "new" in ch:
        print(f"  番号: 前回から引き継ぎ。新しい番号 {ch['new'] or 'なし'} / なくなった番号 {ch['removed'] or 'なし'}")
    print(f"  → 書き出し: data/out/{args.alias}/{path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
