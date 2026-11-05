"""模型の書き出し：パーツ別 STL、画面用 GLB、検査結果（SPEC §8）。3MF は core/threemf.py。

使い方:  python -m core.export 物件A   → data/out/物件A/model/（STL・GLB・checks.json）
building.json に人の修正（overrides.json）を当ててから組み立てる。
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import trimesh

from core import overrides as ov
from core.builder import ModelPart, build_model, drop_specks, union_all
from core.checks import building_checks, part_checks
from core.paths import out_dir
from core.rules import load_rules
from core.bambu import to_bambu_3mf
from core.threemf import write_3mf
from core import print_split as ps

COLORS = {"foundation": (150, 150, 150, 255), "floor1": (235, 220, 190, 255), "exterior1": (230, 120, 100, 255),
          "roof": (90, 110, 140, 255), "roof_ext": (200, 115, 95, 255),
          # 2階建て（core/builder_multi.py）
          "roof_lower": (110, 130, 160, 255), "roof_lower_ext": (210, 125, 105, 255),
          "floor2": (240, 228, 205, 255), "exterior2": (225, 130, 110, 255)}


def to_trimesh(part: ModelPart, scale: float, for_print: bool = False) -> trimesh.Trimesh:
    """立体を 1/scale の模型寸法のメッシュにする。for_print なら印刷の向き（屋根は裏返す）にして床に置く。"""
    m = part.solid.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3] / scale
    f = np.asarray(m.tri_verts, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=v, faces=f, process=True)
    if for_print:
        if part.flip_for_print:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
        mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return mesh


def _raw_watertight(part: ModelPart) -> bool:
    """点をまとめない形（3MF に書く形）で閉じているか。"""
    m = part.solid.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3]
    return bool(trimesh.Trimesh(vertices=v, faces=np.asarray(m.tri_verts, dtype=np.int64), process=False).is_watertight)


def _object_keys(parts: dict[str, ModelPart]) -> list[str]:
    """3MF のオブジェクトの並び：ほかの部品にまとめられない部品（基礎・1階・屋根…）を、組み立ての順に。
    外壁の部品は object_key のオブジェクトに入る。★前は「基礎・1階・屋根」の決め打ちで、2階建てで足した部品が落ちた。"""
    return [k for k, p in parts.items() if p.object_key is None]


def _print_mesh(part: ModelPart, scale: float) -> dict:
    """3MF 用：模型寸法・印刷の向き（裏返すものは裏返す）の頂点と三角形。床への置き方は threemf が決める。"""
    m = part.solid.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3] / scale
    f = np.asarray(m.tri_verts, dtype=np.int64)
    if part.flip_for_print:  # X 軸まわりに 180 度（y→-y, z→-z）。向きが裏返らないので三角形の順はそのまま
        v = v * np.array([1.0, -1.0, -1.0])
    return {"verts": v, "tris": f}


OVERLAP_MM3 = 0.5   # 組んだときにパーツどうしがこれ以上重なっていたら「ぶつかる」とする（模型の体積）


def overlap_checks(parts: dict[str, ModelPart], scale: float) -> list[dict]:
    """組んだ状態で、別々のオブジェクト（基礎・1階・屋根）どうしが重なっていないか（ぶつかって組めない所がないか）。"""
    objects: dict[str, list] = {}
    for p in parts.values():
        objects.setdefault(p.object_key or p.key, []).append(p.solid)
    solids = {k: union_all(v) if len(v) > 1 else v[0] for k, v in objects.items()}
    out = []
    keys = list(solids)
    for i, a in enumerate(keys):
        for b2 in keys[i + 1:]:
            vol = abs((solids[a] ^ solids[b2]).volume()) / scale ** 3
            if vol > OVERLAP_MM3:
                out.append({"part": f"{a}+{b2}", "level": "高", "item": "overlap",
                            "message": f"{parts[a].label.split('（')[0]}と{parts[b2].label.split('（')[0]}が、組んだときに {vol:.1f}mm³ 重なっています（ぶつかって組めない可能性）"})
    if not out:
        out.append({"part": "all", "level": "情報", "item": "overlap", "message": "組んだときに、パーツどうしは重なっていません"})
    return out


def make_model(alias: str, company: str | None = None) -> tuple[dict, dict[str, ModelPart], list[dict], dict]:
    """building.json＋修正 → 組み立て。（修正を当てた building、パーツ、修正の結果、会社ルール）を返す。"""
    b = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    rules = load_rules(company or b["source"].get("company_rules", "jisha"))
    applied, results = ov.apply(b, ov.load(alias))
    return applied, build_model(applied, rules), results, rules


def _split_keys(parts: dict[str, ModelPart], rules: dict) -> set[str]:
    """印刷のときに面ごとに分けて寝かせるオブジェクト（屋根）。会社ルールの model.roof_print が flip なら前の裏返し。"""
    if rules["model"].get("roof_print", "face_down") != "face_down":
        return set()
    return {k for k, p in parts.items() if p.object_key is None and p.print_faces}


def _mesh_of(solid, scale: float) -> tuple[np.ndarray, np.ndarray]:
    m = solid.to_mesh()
    return np.asarray(m.vert_properties, dtype=np.float64)[:, :3] / scale, np.asarray(m.tri_verts, dtype=np.int64)


def _house_cuts_in(pc, house) -> list[float]:
    """家の分け目のうち、その部品をまたぐもの。"""
    if house is None or not house.cuts:
        return []
    xy = ps._verts_xy(pc.solids)
    if not len(xy):
        return []
    proj = xy @ house.u
    lo, hi = float(proj.min()), float(proj.max())
    return [t for t in house.cuts if lo + 1.0 < t < hi - 1.0]


def print_pieces(owner_key: str, parts: dict[str, ModelPart], rules: dict, house=None) -> list[dict]:
    """屋根のオブジェクトを面ごとに分け、上の面を床に付けて、造形範囲に入る角度で置いた部品の一覧。
    入らない面は、家の分け目（house。core/body_split.py）があればそれで切り（本体と継ぎ目がそろう）、なければ長い方を半分にする。
    [{"name", "face", "meshes": {部品の key: (頂点, 三角形)}, "yaw", "fits", "support_mm2", "warnings"}]（模型寸法）"""
    m = rules["model"]
    scale = float(m["scale"])
    limit = ps.BED_MM - 2 * float(m.get("print_bed_margin_mm", 5.0))
    members = {k: p for k, p in parts.items() if (p.object_key or p.key) == owner_key}
    fuzzy = [k for k, p in members.items() if p.fuzzy_skin]
    pieces = ps.split_by_faces({k: p.solid for k, p in members.items()}, parts[owner_key].print_faces)
    out = []
    queue = [(pc, 0) for pc in pieces]
    while queue:
        pc, depth = queue.pop(0)
        ps.smooth_cuts(pc, fuzzy, owner_key, float(m.get("roof_cut_smooth_mm", 1.5)) * scale)
        meshes = {k: _mesh_of(s, scale) for k, s in pc.solids.items() if not s.is_empty()}
        if not meshes:
            continue
        oriented, yaw, longest, fits = ps.orient(meshes, pc.top_plane, limit)
        if not fits and depth < 2:
            cuts = _house_cuts_in(pc, house)
            if cuts:
                from core.body_split import side_names
                msg = "造形範囲に入らないので、本体と同じ分け目で切った（継ぎ目がそろう）"
                names = [f"{pc.name}・{s}" for s in side_names(house.u, len(cuts) + 1)]
                subs = ps.split_by_planes(pc.solids, house.u, cuts, names, top_plane=pc.top_plane, base_cuts=pc.cuts,
                                          warnings=pc.warnings + ([] if msg in pc.warnings else [msg]))
                queue[0:0] = [(h, depth + 1) for h in subs]
            else:
                queue[0:0] = [(h, depth + 1) for h in ps.split_across_eave(pc)]
            continue
        # サポートの量は、部品を合わせた形で数える（同じオブジェクトの部品どうしの境目の面は、印刷では中に埋もれるため）
        union = drop_specks(union_all([x for x in pc.solids.values() if not x.is_empty()]))
        uv, uf = _mesh_of(union, scale)
        support = ps.support_area(ps.apply_last(uv), uf)
        warns = list(pc.warnings) + ([] if fits else [f"造形範囲 {limit:.0f}mm に入らない（長い辺 {longest:.1f}mm）"])
        out.append({"name": pc.name, "meshes": oriented, "yaw": yaw, "fits": fits, "support_mm2": support,
                    "warnings": warns, "solids": pc.solids})
    return out


def body_pieces(owner_key: str, parts: dict[str, ModelPart], rules: dict, house) -> list[dict]:
    """本体のオブジェクト（基礎・1階・2階）を家の分け目で分け、床を下のまま、造形範囲に入る角度に回して置いた部品の一覧
    （★11/5 作者 2-1。print_pieces と同じ形。本体のサポートの量は数えない：窓の上の壁などで多めに出て、紛らわしいため）。"""
    import math
    m = rules["model"]
    scale = float(m["scale"])
    limit = ps.BED_MM - 2 * float(m.get("print_bed_margin_mm", 5.0))
    members = {k: p for k, p in parts.items() if (p.object_key or p.key) == owner_key}
    fuzzy = [k for k, p in members.items() if p.fuzzy_skin]
    pieces = ps.split_by_planes({k: p.solid for k, p in members.items()}, house.u, house.objects[owner_key], house.sides,
                                settle=False)
    out = []
    for pc in pieces:
        ps.smooth_cuts(pc, fuzzy, owner_key, float(m.get("roof_cut_smooth_mm", 1.5)) * scale)
        meshes = {k: _mesh_of(s, scale) for k, s in pc.solids.items() if not s.is_empty()}
        if not meshes:
            continue
        allv = np.vstack([v for v, _ in meshes.values()])
        deg, _ = ps.best_yaw(allv[:, :2], limit)
        th = math.radians(deg)
        Rz = np.array([[math.cos(th), -math.sin(th), 0.0], [math.sin(th), math.cos(th), 0.0], [0.0, 0.0, 1.0]])
        zmin = float(allv[:, 2].min())
        placed = {k: ((v @ Rz.T) - np.array([0.0, 0.0, zmin]), f) for k, (v, f) in meshes.items()}
        allp = np.vstack([v for v, _ in placed.values()])
        ext = allp.max(axis=0) - allp.min(axis=0)
        fits = bool(ext[0] <= limit and ext[1] <= limit and ext[2] <= ps.BED_MM)
        warns = list(pc.warnings) + ([] if fits else [f"造形範囲 {limit:.0f}mm に入らない（{ext[0]:.1f}×{ext[1]:.1f}mm）"])
        out.append({"name": pc.name, "meshes": placed, "yaw": deg, "fits": fits, "support_mm2": None,
                    "warnings": warns, "solids": pc.solids})
    return out


def write_outputs(alias: str, company: str | None = None, bambu_enabled: bool = True) -> dict:
    from core import body_split as bs
    b, parts, ov_results, rules = make_model(alias, company)
    scale = float(rules["model"]["scale"])
    dst = out_dir(alias) / "model"
    dst.mkdir(parents=True, exist_ok=True)
    for old in dst.glob("*.stl"):                       # 前の書き出しの STL（分け方が変わると名前が変わる）を消す
        old.unlink()
    checks = building_checks(b) + overlap_checks(parts, scale)
    # 本体が造形範囲に入らなければ、家の分け目を決める（人が平面図で決めた分け目があれば、それを使う）
    house = bs.choose(b, parts, rules, bs.load_saved(alias))
    split = _split_keys(parts, rules) | (set(house.objects) if house else set())
    split_members = {k for k, p in parts.items() if (p.object_key or p.key) in split}
    scene = trimesh.Scene()
    for key, part in parts.items():
        assembled = to_trimesh(part, scale)                 # 組んだ状態（画面の 3D 表示用）
        if key in split_members:                           # 屋根は面ごとに分けて、下で STL と検査を作る
            assembled.visual = trimesh.visual.TextureVisuals(
                material=trimesh.visual.material.PBRMaterial(baseColorFactor=COLORS.get(key, (200, 200, 200, 255)),
                                                             metallicFactor=0.0, roughnessFactor=0.9))
            scene.add_geometry(assembled, node_name=key, geom_name=key)
            continue
        printable = to_trimesh(part, scale, for_print=True)  # 印刷の向き（STL・検査用）
        printable.export(dst / f"{key}.stl")
        # 色は材質の色として付ける（面ごとの色は、書き出しのときに trimesh が scipy を使うため）
        assembled.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(baseColorFactor=COLORS.get(key, (200, 200, 200, 255)),
                                                         metallicFactor=0.0, roughnessFactor=0.9))
        scene.add_geometry(assembled, node_name=key, geom_name=key)
        # 離れた塊は、同じオブジェクトに入る部品（1階の床・内壁と外壁）を合わせて数える（印刷では一体になるため）
        together = [q.solid for q in parts.values() if (q.object_key or q.key) == (part.object_key or key)]
        obj = drop_specks(together[0] if len(together) == 1 else union_all(together))  # 合体の計算でできる厚みゼロのかけらは数えない
        checks += part_checks(key, part.label, printable, [x.volume() for x in obj.decompose()], raw_watertight=_raw_watertight(part))
    scene.export(dst / "model.glb", include_normals=False)  # 法線は表示側で計算（trimesh は法線に scipy が要るため）

    # 3MF：同じオブジェクトの部品（1階の床・内壁と外壁）は、同じ向き・同じ位置のまま入れる
    m = rules["model"]
    objects = []
    piece_info = []
    for key in _object_keys(parts):
        members = [p for p in parts.values() if (p.object_key or p.key) == key]
        if key not in split:
            objects.append({"name": parts[key].label.split("（")[0],
                            "parts": [{"name": p.label, "fuzzy_skin": p.fuzzy_skin and m.get("exterior_finish") == "plaster",
                                       **_print_mesh(p, scale)} for p in members]})
            continue
        # 屋根：面ごとに分けて、上の面を床に付けて置く（core/print_split.py）。本体：家の分け目で分け、床を下のまま（core/body_split.py）
        is_body = house is not None and key in house.objects
        pieces = body_pieces(key, parts, rules, house) if is_body else print_pieces(key, parts, rules, house)
        n = len(pieces)
        base = parts[key].label.split("（")[0]
        for i, pc in enumerate(pieces, 1):
            name = base if n == 1 else f"{base} {i}/{n}（{pc['name']}）"
            files, prt = [], []
            for p in members:
                if p.key not in pc["meshes"]:
                    continue
                v, f = pc["meshes"][p.key]
                fname = f"{p.key}.stl" if n == 1 else f"{p.key}_p{i}.stl"
                trimesh.Trimesh(vertices=v, faces=f, process=True).export(dst / fname)
                files.append(fname)
                prt.append({"name": p.label if n == 1 else f"{p.label} {i}/{n}", "verts": v, "tris": f,
                            "fuzzy_skin": p.fuzzy_skin and m.get("exterior_finish") == "plaster"})
            objects.append({"name": name, "parts": prt})
            allv = np.vstack([q["verts"] for q in prt])
            combined = trimesh.Trimesh(vertices=allv, faces=np.vstack([q["tris"] + sum(len(r["verts"]) for r in prt[:j])
                                                                       for j, q in enumerate(prt)]), process=True)
            union = drop_specks(union_all([s for s in pc["solids"].values() if not s.is_empty()]))   # 体積ゼロのかけらは数えない
            raw_ok = all(trimesh.Trimesh(vertices=q["verts"], faces=q["tris"], process=False).is_watertight for q in prt)
            checks += part_checks(f"{key}_p{i}" if n > 1 else key, name, combined, [x.volume() for x in union.decompose()],
                                  raw_watertight=raw_ok)
            if pc["support_mm2"] is not None and pc["support_mm2"] > 300:
                checks.append({"part": f"{key}_p{i}" if n > 1 else key, "level": "中", "item": "support",
                               "message": f"{name}：サポートが要りそうな面が約 {pc['support_mm2']:.0f}mm²（ポーチの天井の板・下がり天井の板の下など。見えない側）"})
            for w in pc["warnings"]:
                checks.append({"part": f"{key}_p{i}" if n > 1 else key, "level": "中", "item": "print_split", "message": f"{name}：{w}"})
            ext = allv.max(axis=0) - allv.min(axis=0)
            piece_info.append({"object": key, "name": name, "face": pc["name"], "files": files, "yaw_deg": round(pc["yaw"], 1),
                               "size_mm": [round(float(x), 1) for x in ext],
                               "support_mm2": round(pc["support_mm2"]) if pc["support_mm2"] is not None else None,
                               "fits": pc["fits"], "warnings": pc["warnings"]})
        if is_body:
            checks.append({"part": key, "level": "情報", "item": "body_split",
                           "message": f"{base}：長いので {n} 個に分けた（{house.reasons[0] if house.reasons else '分け目'}）。"
                                      "分け目は平面図で動かせる。のり代は床と、切り口をまたぐ壁の切り口（定規に当てて平らに付ける）"})
            continue
        checks.append({"part": key, "level": "情報", "item": "print_split",
                       "message": (f"{base}：上の面を床に付けて置いた（上の面にサポートが付かない）" if n == 1 else
                                   f"{base}：棟・隅棟・谷の線で面ごとに {n} 個に分け、それぞれ上の面を床に付けて置いた。"
                                   "のりで付けるのは棟・隅棟（切り口は2つの面のちょうど真ん中）")})
    if house is not None:
        for w in house.warnings:
            checks.append({"part": "house_cut", "level": "中", "item": "body_split", "message": f"本体の分け目：{w}"})
    write_3mf(dst / "model.3mf", objects,
              {"thickness": float(m["fuzzy_skin_thickness_mm"]), "point_distance": float(m["fuzzy_skin_point_distance_mm"])})
    # Bambu Studio が入っていれば、Bambu 形式の 3MF（画面で開いても部品ごとの設定が残る）も作る（core/bambu.py）
    bambu = to_bambu_3mf(dst / "model.3mf", dst / "model_bambu.3mf") if bambu_enabled else {"ok": False, "reason": "作らない設定"}
    if bambu["ok"]:
        checks.append({"part": "export", "level": "情報", "item": "bambu_3mf",
                       "message": "Bambu 形式の 3MF（model_bambu.3mf）を作りました。外壁だけファジースキン（外側の輪郭）が設定済みです"})
    else:
        checks.append({"part": "export", "level": "情報", "item": "bambu_3mf",
                       "message": f"{bambu['reason']}。model.3mf を開いたら、外壁の部品を選んで「その他」→「ファジースキン」→「外側の輪郭」にしてください"})
    summary = {"parts": {k: {"label": p.label, "fuzzy_skin": p.fuzzy_skin, "object": p.object_key or k,
                             "flip_for_print": p.flip_for_print and k not in split_members, "notes": p.notes} for k, p in parts.items()},
               "overrides": ov_results, "checks": checks, "bambu_3mf": bambu, "print_pieces": piece_info,
               "house_cut": house.to_json() if house is not None else None,
               "stl_files": sorted(f.name for f in dst.glob("*.stl"))}
    (dst / "checks.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="模型のパーツを組み立てて書き出す")
    ap.add_argument("alias", help="物件の仮名（例：物件A）")
    args = ap.parse_args(argv)
    s = write_outputs(args.alias)
    print(f"【{args.alias}】模型の書き出し → data/out/{args.alias}/model/")
    for k, p in s["parts"].items():
        print(f"  {p['label']}（{k}）" + ("　ファジースキン" if p["fuzzy_skin"] else "") + ("　裏返して印刷" if p["flip_for_print"] else ""))
        for n in p["notes"]:
            print(f"      ・{n}")
    print("  検査:")
    for c in s["checks"]:
        print(f"    [{c['level']}] {c['message']}")
    if s["overrides"]:
        print("  人の修正: " + "、".join(f"{r['id']} {r['status']}" for r in s["overrides"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
