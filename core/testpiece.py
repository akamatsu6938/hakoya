"""試験片（SPEC §8）：壁の角と三角だけの数 cm 角の部品。はめあいの隙間・三角の大きさを試し印刷で詰めるため。

使い方:  python -m core.testpiece   → data/out/試験片/test_piece.3mf（と部品ごとの STL）

部品（すべて模型寸法・会社ルールの値で作る）：
1. 壁の角      外壁の厚みの L 字の壁（屋根の角をかぶせる）
2. 屋根の角    板の裏に三角（裏返して印刷。壁の角の内側にはまる）
3. 基礎の角    基礎の帯の L 字＋上向きの三角
4. 床の角      床の板の裏に三角の受けのくぼみ（基礎の角に載せる）
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
from shapely.geometry import Polygon, box

from core.builder import prism, union_all
from core.paths import out_dir
from core.rules import load_rules
from core.threemf import write_3mf

LEG_MM = 30.0     # L 字の長さ（模型寸法）
WALL_H_MM = 12.0  # 壁の角の高さ


def build_pieces(rules: dict) -> dict[str, tuple[str, object, bool]]:
    m, w = rules["model"], rules["walls"]
    s = float(m["scale"])
    t = float(w["exterior_thickness_mm"]) / s
    leg = float(m["corner_block_leg_mm"])
    bh = float(m["corner_block_height_mm"])
    fbh = float(m["floor_block_height_mm"])
    c = float(m["fit_clearance_mm"])
    plate = float(m["plate_thickness_mm"])
    band = float(m["foundation_band_mm"]) / s
    found_h = float(rules["levels"]["gl_to_foundation_top_mm"]) / s
    floor_t = (float(rules["levels"]["gl_to_1fl_mm"]) - float(rules["levels"]["gl_to_foundation_top_mm"])) / s
    L = LEG_MM

    # 1. 壁の角：内側の角が原点。壁は x・y のマイナス側に厚み t
    wall = prism(box(-t, -t, L, 0).union(box(-t, -t, 0, L)), 0.0, WALL_H_MM)
    # 2. 屋根の角：板（壁の外の面まで）＋ 内側の角に三角（隙間 c だけ壁から離す）。組んだ状態で作り、印刷では裏返す
    tri = Polygon([(c, c), (c + leg, c), (c, c + leg)])
    roof = union_all([prism(box(-t, -t, L, L), WALL_H_MM, WALL_H_MM + plate), prism(tri, WALL_H_MM - bh, WALL_H_MM + 0.01)])
    # 3. 基礎の角：帯の L 字。内側の角に地面から立ち上がる三角
    ftri = Polygon([(band, band), (band + leg, band), (band, band + leg)])
    found = union_all([prism(box(0, 0, L, band).union(box(0, 0, band, L)), 0.0, found_h),
                       prism(ftri, 0.0, found_h + fbh)])
    # 4. 床の角：基礎の上に載る板。裏に三角の受けのくぼみ（隙間 c だけ大きく、深さ fbh + c）
    floor = prism(box(0, 0, L, L), found_h, found_h + floor_t) - \
        prism(ftri.buffer(c, join_style="mitre"), found_h - 0.01, found_h + fbh + c)
    return {"wall": ("試験片・壁の角", wall, False), "roof": ("試験片・屋根の角", roof, True),
            "foundation": ("試験片・基礎の角", found, False), "floor": ("試験片・床の角", floor, False)}


def write(rules: dict) -> list[str]:
    dst = out_dir("試験片")
    dst.mkdir(parents=True, exist_ok=True)
    objects = []
    for key, (label, solid, flip) in build_pieces(rules).items():
        mesh = solid.to_mesh()
        v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
        f = np.asarray(mesh.tri_verts, dtype=np.int64)
        if flip:
            v = v * np.array([1.0, -1.0, -1.0])
        objects.append({"name": label, "parts": [{"name": label, "verts": v, "tris": f}]})
        import trimesh
        trimesh.Trimesh(vertices=v - v.min(axis=0) * np.array([0, 0, 1]), faces=f, process=False).export(dst / f"{key}.stl")
    m = rules["model"]
    write_3mf(dst / "test_piece.3mf", objects,
              {"thickness": float(m["fuzzy_skin_thickness_mm"]), "point_distance": float(m["fuzzy_skin_point_distance_mm"])},
              spacing=45.0)
    return [o["name"] for o in objects]


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="はめあいを試す試験片を書き出す")
    ap.add_argument("--company", default="jisha")
    args = ap.parse_args(argv)
    rules = load_rules(args.company)
    names = write(rules)
    m = rules["model"]
    print(f"試験片 → data/out/試験片/test_piece.3mf（{'、'.join(names)}）")
    print(f"  三角：辺 {m['corner_block_leg_mm']}mm・高さ {m['corner_block_height_mm']}mm、床裏のくぼみ {m['floor_block_height_mm']}mm、"
          f"はめあいの隙間 {m['fit_clearance_mm']}mm（rules/{args.company}.yaml の model を変えて作り直せる）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
