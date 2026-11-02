"""検品の材料の画像（SPEC §9-2）：面ごと（東・西・南・北の立面、屋根を外した平面）に、
模型の図・同じ向きから見た glb の図・図の中の部材の位置（番号と画像上の範囲）を作る。

使い方:  python -m core.render 物件A   → data/out/物件A/inspect/
- {面}_model.png    模型の図（パーツごとに色、番号の札、縮尺）
- {面}_glb.png      glb の図（元の形。サッシなどもそのまま。家具・植栽などは除く）
- {面}.json         図の中の部材（番号・種類・画像上の範囲 bbox_px）と、画像の縮尺

描き方：正射影（遠近なし）。三角形を奥から順に塗る（Pillow だけで描くので、ブラウザや追加の部品が要らない）。
東西南北は glb の +Y を北とみなす（推定。roof.low_side_assumption と同じ）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Point, Polygon

from adapters.glb_reader import read_glb
from core.paths import glb_working_copy, out_dir, property_files

PX_PER_MM = 0.06          # 1mm（実寸）→ 0.06px（10m が 600px）
MARGIN = 50
VIEWS = ("S", "N", "E", "W", "plan")
OCCLUSION_TOL_MM = 60.0   # 部材の手前の面より、これ以上手前に物があれば「隠れている」（実寸）
VISIBLE_MIN = 0.3         # 見えている割合がこれ未満なら「隠れている」
VIEW_LABELS = {"S": "南の立面", "N": "北の立面", "E": "東の立面", "W": "西の立面", "plan": "平面（屋根を外して上から）"}
PART_COLORS = {"foundation": (160, 160, 160), "floor1": (225, 212, 185), "exterior1": (215, 125, 105),
               "roof": (110, 128, 155), "roof_ext": (200, 118, 98),
               # glb の部材の種類ごとの色（模型と見比べやすいように、外壁・屋根・基礎は模型と同じ色）
               "exterior_wall": (215, 125, 105), "standalone_wall": (215, 125, 105), "room": (225, 212, 185),
               "opening": (150, 190, 230), "porch": (160, 160, 160), "porch_soffit": (130, 175, 130)}
LIGHT = np.array([-0.4, -0.6, 0.7])
LIGHT = LIGHT / np.linalg.norm(LIGHT)


def project(view: str, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(N,3) の点 → 画像の (u, v)（右・上が正）と奥行き（大きいほど手前）。"""
    x, y, z = pts[:, 0], pts[:, 1], pts[:, 2]
    if view == "S":    # 南から北を見る
        return np.column_stack([x, z]), -y
    if view == "N":    # 北から南を見る
        return np.column_stack([-x, z]), y
    if view == "E":    # 東から西を見る
        return np.column_stack([y, z]), x
    if view == "W":    # 西から東を見る
        return np.column_stack([-y, z]), -x
    return np.column_stack([x, y]), z  # 平面：上から


VIEW_DIR = {"S": np.array([0, 1, 0]), "N": np.array([0, -1, 0]), "E": np.array([-1, 0, 0]),
            "W": np.array([1, 0, 0]), "plan": np.array([0, 0, -1])}


class Canvas:
    """世界の (u, v) を画像の画素に変える枠。複数の図で同じ縮尺・同じ位置にそろえる。"""

    def __init__(self, uv_min: np.ndarray, uv_max: np.ndarray, px_per_mm: float = PX_PER_MM):
        self.lo, self.hi, self.k = uv_min, uv_max, px_per_mm
        self.w = int((uv_max[0] - uv_min[0]) * px_per_mm) + MARGIN * 2
        self.h = int((uv_max[1] - uv_min[1]) * px_per_mm) + MARGIN * 2 + 30

    def px(self, uv: np.ndarray) -> np.ndarray:
        return np.column_stack([(uv[:, 0] - self.lo[0]) * self.k + MARGIN,
                                (self.hi[1] - uv[:, 1]) * self.k + MARGIN])


def _font(size: int):
    for name in ("meiryo.ttc", "YuGothM.ttc", "msgothic.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_meshes(view: str, meshes: list[tuple[str, np.ndarray, np.ndarray]], canvas: Canvas, title: str) -> Image.Image:
    """meshes: [(色の名前, 頂点(N,3), 三角形(M,3))]。画素ごとに奥行きを比べて、いちばん手前の面の色を塗る（Z バッファ）。

    ★最初は「三角形の真ん中の奥行きで並べて奥から塗る」方法だったが、大きな屋根の三角形と壁の三角形の順を間違え、
      屋根の上に壁の色のくさびが描かれた。それを Claude の検品が「壁が屋根から出ている」と読んだ（2026-11-02）。
    """
    W, H = canvas.w, canvas.h
    zbuf = np.full((H, W), -np.inf)
    rgb = np.full((H, W, 3), 255, dtype=np.uint8)
    vdir = VIEW_DIR[view]
    for key, v, f in meshes:
        if not len(f):
            continue
        tri = v[f]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1)
        ok = ln > 1e-9
        tri, n = tri[ok], n[ok] / ln[ok, None]
        facing = n @ -vdir > 1e-6                  # こちらを向いている面だけ
        tri, n = tri[facing], n[facing]
        if not len(tri):
            continue
        uv, depth = project(view, tri.reshape(-1, 3))
        pix = canvas.px(uv).reshape(-1, 3, 2)
        depth = depth.reshape(-1, 3)
        shade = 0.55 + 0.45 * np.clip(n @ LIGHT, 0, 1)
        base = np.array(PART_COLORS.get(key, (200, 200, 200)), dtype=float)
        for p, dz, s in zip(pix, depth, shade):
            _raster(p, dz, (base * s).astype(np.uint8), zbuf, rgb)
    canvas.zbuf = zbuf                              # 部材が隠れているかの判定に使う
    img = Image.fromarray(rgb, "RGB")
    d = ImageDraw.Draw(img)
    d.text((MARGIN, 12), title, fill="black", font=_font(16))
    # 縮尺（1m）
    x0, y0 = canvas.w - MARGIN - 1000 * canvas.k, canvas.h - 22
    d.line([(x0, y0), (x0 + 1000 * canvas.k, y0)], fill="black", width=2)
    d.text((x0, y0 - 16), "1m", fill="black", font=_font(12))
    return img


def _raster(p: np.ndarray, dz: np.ndarray, col: np.ndarray, zbuf: np.ndarray, rgb: np.ndarray) -> None:
    """1つの三角形を、画素ごとに奥行き（大きいほど手前）を比べて塗る。"""
    H, W = zbuf.shape
    x0, y0 = np.floor(p.min(axis=0)).astype(int)
    x1, y1 = np.ceil(p.max(axis=0)).astype(int)
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W - 1), min(y1, H - 1)
    if x1 < x0 or y1 < y0:
        return
    (ax, ay), (bx, by), (cx, cy) = p
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if abs(den) < 1e-12:
        return
    xs, ys = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
    w0 = ((by - cy) * (xs - cx) + (cx - bx) * (ys - cy)) / den
    w1 = ((cy - ay) * (xs - cx) + (ax - cx) * (ys - cy)) / den
    w2 = 1.0 - w0 - w1
    eps = -1e-3                                     # 縁の画素も塗る（三角形のつなぎ目に白い筋が出ないように）
    inside = (w0 >= eps) & (w1 >= eps) & (w2 >= eps)
    if not inside.any():
        return
    z = w0 * dz[0] + w1 * dz[1] + w2 * dz[2]
    sub = zbuf[y0:y1 + 1, x0:x1 + 1]
    upd = inside & (z > sub)
    sub[upd] = z[upd]
    rgb[y0:y1 + 1, x0:x1 + 1][upd] = col


def _wall_frame(w: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    s, e = np.array(w["start"], float), np.array(w["end"], float)
    L = float(np.linalg.norm(e - s)) or 1.0
    d = (e - s) / L
    return s, d, np.array([-d[1], d[0]])


def elements(b: dict, view: str, canvas: Canvas, roof_top: float) -> list[dict]:
    """図の中の部材（壁・開口）の番号と画像上の範囲。立面では、その面から見える向きの外壁・開口だけ。"""
    out = []
    vdir = VIEW_DIR[view][:2]
    walls = {w["id"]: w for w in b["walls"]}

    def bbox(pts3):
        uv, _ = project(view, np.asarray(pts3, float))
        p = canvas.px(uv)
        return [int(p[:, 0].min()), int(p[:, 1].min()), int(p[:, 0].max()), int(p[:, 1].max())]

    def visible_ratio(pts3, box) -> float:
        """手前の物に隠れていない割合（描いたときの奥行きの記録と比べる）。★ポーチの袖壁の奥の窓まで塗ってしまったため。"""
        zbuf = getattr(canvas, "zbuf", None)
        if zbuf is None or view == "plan":
            return 1.0
        _, depth = project(view, np.asarray(pts3, float))
        front = float(np.max(depth))                       # 部材のいちばん手前の奥行き
        x0, y0, x1, y1 = box
        h, w_ = zbuf.shape
        sub = zbuf[max(y0 + 1, 0):min(y1, h), max(x0 + 1, 0):min(x1, w_)]
        if sub.size == 0:
            return 1.0
        return float(np.mean(sub <= front + OCCLUSION_TOL_MM))

    outline = Polygon(b["foundation"]["outline"]) if b.get("foundation", {}).get("outline") else None
    center = np.array(outline.centroid.coords[0]) if outline is not None else np.zeros(2)

    def outward(w):
        """壁の外を向いている側（家の外へ向かう向き）。"""
        s, d, n = _wall_frame(w)
        mid = s + d * float(np.linalg.norm(np.array(w["end"]) - s)) / 2
        if w["kind"] == "exterior" and outline is not None:
            probe_in = mid + n * (w["thickness_mm"] / 2 + 30)
            probe_out = mid - n * (w["thickness_mm"] / 2 + 30)
            a, b2 = outline.contains(Point(probe_in)), outline.contains(Point(probe_out))
            if a != b2:  # 片側だけ家の中 → 反対側が外
                return -n if a else n
            # 両側とも家の外（ポーチの上の垂れ壁など）は、家の真ん中から離れる向き
        return n if float(n @ (mid - center)) >= 0 else -n   # 家の外に立つ壁：家の真ん中から離れる向き

    def faces_view(w):
        if view == "plan":
            return True
        if w["kind"] not in ("exterior", "standalone"):
            return False
        return float(outward(w) @ -vdir) > 0.7

    for w in b["walls"]:
        if not faces_view(w):
            continue
        s, d, n = _wall_frame(w)
        L = float(np.linalg.norm(np.array(w["end"]) - np.array(w["start"])))
        top = roof_top if w["height"] == "to_roof" else float(w["height"])
        t = w["thickness_mm"] / 2
        pts = [[*(s + n * sgn * t + d * a), z] for sgn in (-1, 1) for a in (0, L) for z in (w["bottom_mm"], top)]
        bx = bbox(pts)
        item = {"id": w["id"], "type": "wall", "kind": w["kind"], "bbox_px": bx}
        if visible_ratio(pts, bx) < VISIBLE_MIN:
            item["hidden"] = True                        # 手前の物（ポーチの袖壁など）にほとんど隠れている
        out.append(item)
    for o in b.get("openings", []):
        w = walls.get(o["wall"])
        if w is None or not faces_view(w):
            continue
        s, d, n = _wall_frame(w)
        t = w["thickness_mm"] / 2
        a0, a1 = o["offset_mm"], o["offset_mm"] + o["width_mm"]
        pts = [[*(s + n * sgn * t + d * a), z] for sgn in (-1, 1) for a in (a0, a1) for z in (o["sill_mm"], o["head_mm"])]
        bx = bbox(pts)
        item = {"id": o["id"], "type": "opening", "kind": o["kind"], "wall": o["wall"], "bbox_px": bx,
                "head_fl_mm": o["head_fl_mm"], "width_mm": o["width_mm"]}
        if visible_ratio(pts, bx) < VISIBLE_MIN:
            item["hidden"] = True
        out.append(item)
    return out


WALL_PLAN_COLORS = {"exterior": (200, 105, 85), "interior": (150, 125, 95), "standalone": (95, 112, 140)}


def draw_plan_walls(img: Image.Image, b: dict, canvas: Canvas) -> None:
    """平面図に壁の形を上から描き足す（上から見ると壁の上面と床が同じ明るさになり、壁が見えないため）。
    途中の高さから始まる壁（垂れ壁・下がり壁）は線だけ。"""
    d = ImageDraw.Draw(img)
    for w in b["walls"]:
        s, dd, n = _wall_frame(w)
        L = float(np.linalg.norm(np.array(w["end"]) - s))
        t = w["thickness_mm"] / 2
        pts = np.array([s + n * t, s + dd * L + n * t, s + dd * L - n * t, s - n * t])
        px = canvas.px(project("plan", np.column_stack([pts, np.zeros(4)]))[0])
        col = WALL_PLAN_COLORS.get(w["kind"], (120, 120, 120))
        if w["bottom_mm"] >= float(b["levels"][0]["fl_mm"]) + 1500:
            d.polygon([tuple(p) for p in px], outline=col)
        else:
            d.polygon([tuple(p) for p in px], fill=col, outline=col)
    for o in b.get("openings", []):  # 開口は白く抜く
        w = next((x for x in b["walls"] if x["id"] == o["wall"]), None)
        if w is None:
            continue
        s, dd, n = _wall_frame(w)
        t = w["thickness_mm"] / 2 + 5
        a0, a1 = o["offset_mm"], o["offset_mm"] + o["width_mm"]
        pts = np.array([s + dd * a0 + n * t, s + dd * a1 + n * t, s + dd * a1 - n * t, s + dd * a0 - n * t])
        px = canvas.px(project("plan", np.column_stack([pts, np.zeros(4)]))[0])
        d.polygon([tuple(p) for p in px], fill=(255, 255, 255) if o["kind"] != "window" else (150, 190, 230))


def draw_plan_edges(img: Image.Image, glb, canvas: Canvas, fl: float) -> None:
    """glb の平面図に、壁の面（部屋の仕上げ面・外壁・単独の壁）を線で描き足す（真上からは線になって見えないため）。"""
    from adapters.walls import vertical_triangles
    d = ImageDraw.Draw(img)
    cols = {"room": (110, 90, 60), "exterior_wall": (180, 70, 50), "standalone_wall": (70, 85, 120)}
    for part in glb.parts:
        if part.role not in cols:
            continue
        for pr in part.primitives:
            if pr.code and pr.code.endswith(("HABA", "MAWA")) or not len(pr.triangles):
                continue
            f = pr.vertices[pr.triangles]
            f = f[f[:, :, 2].max(axis=1) > fl + 300]   # 床近くの低い面（段など）は描かない
            for tri, _, _ in vertical_triangles(f, part.source):
                xy = tri[:, :2]
                i, j = np.argmin(xy @ [1, 1e-3]), np.argmax(xy @ [1, 1e-3])
                seg = np.array([[*xy[i], 0], [*xy[j], 0]])
                if np.linalg.norm(seg[1] - seg[0]) < 1:
                    seg = np.array([[*xy.min(axis=0), 0], [*xy.max(axis=0), 0]])
                p = canvas.px(project("plan", seg)[0])
                d.line([tuple(p[0]), tuple(p[1])], fill=cols[part.role], width=1)


def draw_labels(img: Image.Image, elems: list[dict]) -> None:
    d = ImageDraw.Draw(img)
    f = _font(11)
    for e in elems:
        if e.get("hidden"):
            continue
        x0, y0, x1, y1 = e["bbox_px"]
        if e["type"] == "opening":
            d.rectangle([x0, y0, x1, y1], outline=(30, 90, 200), width=1)
            d.text((x0 + 2, y1 + 1), e["id"], fill=(30, 90, 200), font=f)
        else:
            d.text(((x0 + x1) / 2 - 8, y0 + 3), e["id"], fill=(140, 30, 20), font=f)


def render_all(alias: str) -> dict:
    """模型（書き出し済みの組み立て）と glb から、面ごとの画像と部材の一覧を作る。"""
    from core.export import make_model
    b, parts, _, rules = make_model(alias)
    dst = out_dir(alias) / "inspect"
    dst.mkdir(parents=True, exist_ok=True)
    model_meshes = []
    for key, p in parts.items():
        m = p.solid.to_mesh()
        model_meshes.append((key, np.asarray(m.vert_properties, float)[:, :3], np.asarray(m.tri_verts, np.int64)))
    glb = read_glb(glb_working_copy(property_files(alias)))
    keep_roles = {"exterior_wall", "room", "opening", "roof", "foundation", "standalone_wall", "porch", "porch_soffit"}
    glb_meshes = []
    for part in glb.parts:
        if part.role not in keep_roles:
            continue
        for pr in part.primitives:
            if len(pr.triangles):
                glb_meshes.append((part.role, pr.vertices, pr.triangles))
    roof_top = float(b["roof"]["top_z_range_mm"][1])
    index = {}
    for view in VIEWS:
        hide_roof = view == "plan"
        mm = [m for m in model_meshes if not (hide_roof and m[0] in ("roof", "roof_ext"))]
        gm = [m for m in glb_meshes if not (hide_roof and m[0] in ("roof", "porch_soffit"))]
        if hide_roof:  # 平面は天井より上を見せない（glb の天井で部屋が隠れるため）
            gm = [(k, v, f[(v[f][:, :, 2].min(axis=1) < float(b["levels"][0]["fl_mm"]) + 2000)]) for k, v, f in gm]
        allpts = np.vstack([v for _, v, _ in mm + gm])
        uv, _ = project(view, allpts)
        canvas = Canvas(uv.min(axis=0), uv.max(axis=0))
        img_m = draw_meshes(view, mm, canvas, f"{alias}　模型　{VIEW_LABELS[view]}")
        if view == "plan":
            draw_plan_walls(img_m, b, canvas)
        elems = elements(b, view, canvas, roof_top)
        if view != "plan":  # 立面の開口は glb と同じくガラスの色で塗る（★素通しだと家の奥の壁が見え、「ふさがっている」と読まれた）
            dg = ImageDraw.Draw(img_m)
            for e in elems:
                if e["type"] == "opening" and not e.get("hidden"):
                    dg.rectangle(e["bbox_px"], fill=PART_COLORS["opening"])
        draw_labels(img_m, elems)
        img_m.save(dst / f"{view}_model.png")
        img_g = draw_meshes(view, gm, canvas, f"{alias}　glb（元の形）　{VIEW_LABELS[view]}")
        if view == "plan":
            draw_plan_edges(img_g, glb, canvas, float(b["levels"][0]["fl_mm"]))
        img_g.save(dst / f"{view}_glb.png")
        info = {"view": view, "label": VIEW_LABELS[view], "px_per_mm": canvas.k, "size_px": [canvas.w, canvas.h],
                "elements": elems, "direction_assumption": "glb の +Y を北とした（推定）"}
        (dst / f"{view}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        index[view] = {"elements": len(elems), "size_px": [canvas.w, canvas.h]}
    return index


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="検品の材料の画像（面ごと）を作る")
    ap.add_argument("alias")
    args = ap.parse_args(argv)
    idx = render_all(args.alias)
    for v, i in idx.items():
        print(f"  {VIEW_LABELS[v]}：部材 {i['elements']} 個、{i['size_px'][0]}×{i['size_px'][1]}px")
    print(f"→ data/out/{args.alias}/inspect/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
