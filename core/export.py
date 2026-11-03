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


def write_outputs(alias: str, company: str | None = None, bambu_enabled: bool = True) -> dict:
    b, parts, ov_results, rules = make_model(alias, company)
    scale = float(rules["model"]["scale"])
    dst = out_dir(alias) / "model"
    dst.mkdir(parents=True, exist_ok=True)
    checks = building_checks(b) + overlap_checks(parts, scale)
    scene = trimesh.Scene()
    for key, part in parts.items():
        assembled = to_trimesh(part, scale)                 # 組んだ状態（画面の 3D 表示用）
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
        checks += part_checks(key, part.label, printable, [x.volume() for x in obj.decompose()])
    scene.export(dst / "model.glb", include_normals=False)  # 法線は表示側で計算（trimesh は法線に scipy が要るため）

    # 3MF：同じオブジェクトの部品（1階の床・内壁と外壁）は、同じ向き・同じ位置のまま入れる
    m = rules["model"]
    objects = []
    for key in _object_keys(parts):
        members = [p for p in parts.values() if (p.object_key or p.key) == key]
        objects.append({"name": parts[key].label.split("（")[0],
                        "parts": [{"name": p.label, "fuzzy_skin": p.fuzzy_skin and m.get("exterior_finish") == "plaster",
                                   **_print_mesh(p, scale)} for p in members]})
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
                             "flip_for_print": p.flip_for_print, "notes": p.notes} for k, p in parts.items()},
               "overrides": ov_results, "checks": checks, "bambu_3mf": bambu}
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
