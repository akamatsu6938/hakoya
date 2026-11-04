"""開口（窓・ドア）。建具（typeETate）から穴の位置・幅・下端・上端を出し、どの壁の穴かを決める（SPEC §4-1 手順5）。

★物件Aで確かめたこと：
- 窓は、建具の箱がそのまま穴（SPEC どおり）。
- 壁の外に吊る引き戸（アウトセット）は、レール（OSTN）が穴の幅の約2倍あり、天井より上（3,024〜3,084）にも出ている。
  床の敷居（KURI）が穴の幅と壁の厚みを表すので、敷居があれば敷居を平面の形に使う。
- 取っ手（HTOT・DTOT・TOT1〜3）は壁の外に出ているので、穴の形には使わない。
"""
from __future__ import annotations

import numpy as np
from shapely.geometry import Polygon

from adapters.glb_reader import Part
from adapters.walls import Wall, bbox_polygon

HARDWARE = ("HTOT", "DTOT", "TOT1", "TOT2", "TOT3")   # 取っ手・握り
RAIL = ("OSTN",)                                     # 吊り戸のレール
SILL = "KURI"                                        # 敷居（くつずり）
DOOR_CODES = ("SDOR", "DTOT", "OSTN", "SGDR")        # 戸・取っ手・吊り戸のレール
GLASS_CODES = ("MADO", "MGLS", "GMAD")               # 窓の障子・ガラス
POCKET = "HANK"                                      # 引き込み戸の戸袋（戸が引き込まれる所）の板（★物件B）


def _suffix(code: str | None) -> str:
    return (code or "").split("_", 1)[-1]


def hole(part: Part) -> tuple[Polygon, float, float]:
    """建具の穴：平面の形（壁を貫く長方形）と、下端・上端の高さ（mm）。"""
    body = [pr for pr in part.primitives if _suffix(pr.code) not in HARDWARE + RAIL and len(pr.vertices)]
    if not body:
        body = [pr for pr in part.primitives if len(pr.vertices)]
    z = np.concatenate([pr.vertices[:, 2] for pr in body])
    zmin = float(z.min())
    # 敷居は、床の高さ（穴の下端）にあるときだけ使う（★FIX窓では上端に KURI が1本あるだけ）
    sills = [pr for pr in body if _suffix(pr.code) == SILL and abs(float(pr.vertices[:, 2].min()) - zmin) < 5]
    fp = bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in sills])) if sills else None
    if fp is None or fp.geom_type != "Polygon" or fp.area < 1.0:
        fp = bbox_polygon(np.vstack([pr.vertices[:, :2] for pr in body]))
    return fp, zmin, float(z.max())


def classify(part: Part, sill: float, fl: float) -> tuple[str, str]:
    """種類（window / door / entrance_door）と形（sliding / fixed / hinged / outset_sliding など）。"""
    return _classify([_suffix(pr.code) for pr in part.primitives], sill, fl)


def _classify(codes: list[str], sill: float, fl: float) -> tuple[str, str]:
    if any(c.startswith("TOT") or c == "EHN1" for c in codes):
        return "entrance_door", "entrance"
    is_door = any(c in DOOR_CODES for c in codes)
    is_glass = any(c in GLASS_CODES for c in codes)
    # ★床まで届く FIX 窓（物件B）：戸の部位がなく窓ガラスの部位があれば、床から始まっていても窓。
    # ★FL+400 から始まる吊り戸（物件B）：戸の部位があれば、床から浮いていてもドア
    if is_door or (sill <= fl + 100 and not is_glass):
        if POCKET in codes:
            return "door", "pocket_sliding"    # 引き込み戸
        if "OSTN" in codes:
            return "door", "outset_sliding"    # 壁の外に吊る引き戸
        if "DTOT" in codes:
            return "door", "hinged"            # 開き戸
        if codes.count("SDOR") >= 2:
            return "door", "sliding_pair"      # 引き違い戸
        return "door", "unknown"
    mado = codes.count("MADO")
    if mado >= 2:
        return "window", "sliding"             # 引き違い（模型では中央に縦桟1本。SPEC §8）
    return "window", "fixed"                   # FIX（枠だけ）。★障子1枚の窓も FIX（物件Aで 作者確認）


def group_stacked(holes: list[tuple]) -> list[list[tuple]]:
    """上下に重なる建具（下が FIX・上が引き違いの2段の窓など）を1組にする。holes は (建具, 平面の形, 下端, 上端)。
    平面の重なりが小さい方の8割以上で、上下の端が 5mm 以内で接するものを同じ組にする（★物件B に4組）。"""
    parent = list(range(len(holes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(holes)):
        for j in range(i + 1, len(holes)):
            (_, fi, si, hi), (_, fj, sj, hj) = holes[i], holes[j]
            if fi.intersection(fj).area < 0.8 * min(fi.area, fj.area):
                continue
            if abs(hi - sj) <= 5 or abs(hj - si) <= 5:
                parent[find(j)] = find(i)
    groups: dict[int, list[tuple]] = {}
    for i, h in enumerate(holes):
        groups.setdefault(find(i), []).append(h)
    return [sorted(g, key=lambda h: h[2]) for g in groups.values()]


def snap_hanging_doors(items: list[dict], fl: float, low: float = 100.0, high: float = 600.0) -> list[dict]:
    """FL+100〜600 から始まるドアは、床からのドアにする。glb の例外（普通の家ではありえない形）として返す。
    ★物件B の吊り戸4つは FL+400 から始まり、下の 400mm が壁の面で埋まっていた（作者 11/3「床から」）。"""
    exceptions = []
    for o in items:
        if o["kind"] != "door" or not (low < o["sill_fl_mm"] <= high):
            continue
        old = o["sill_fl_mm"]
        o.setdefault("assumptions", []).append(f"door_from_floor: glb では FL+{old:.0f} から始まる戸。床からのドアにした")
        o.setdefault("glb_original", {})["sill_fl_mm"] = round(old)   # そろえる前の glb の値（会社ルールの集計に使う）
        o["sill_fl_mm"], o["sill_mm"] = 0.0, fl
        exceptions.append({"kind": "door_above_floor", "opening": o, "source": o["source"],
                           "note": f"glb では室内の戸が FL+{old:.0f} から始まっていた（下は壁の面）。床からのドアにした"})
    return exceptions


def snap_door_heads(items: list[dict], fl: float, door_top_fl: float, tol: float = 50.0) -> None:
    """室内のドアの上端が会社ルールの値（door_top_fl）と tol 以内の差なら、ルールの値にそろえる。

    ★物件Aの開き戸は glb で FL+2370 だったが、作者は「室内の建具はすべて 2400 のつもり」。
    差が 50mm（模型で 1mm）以内なら入力のぶれとみなす。それより大きい差は glb のまま（検品で確かめる）。
    """
    for o in items:
        if o["kind"] != "door":
            continue
        diff = door_top_fl - o["head_fl_mm"]
        if 0 < abs(diff) <= tol:
            o.setdefault("assumptions", []).append(
                f"door_top: glb の上端 FL+{o['head_fl_mm']:.0f} を、会社ルール openings.door_top_fl_mm（FL+{door_top_fl:.0f}）にそろえた")
            o.setdefault("glb_original", {})["head_fl_mm"] = round(o["head_fl_mm"])   # そろえる前の glb の値
            o["head_fl_mm"] = door_top_fl
            o["head_mm"] = fl + door_top_fl


TOUCH_MM = 5.0          # 開口の上端と壁の下端の差がこれ以内なら「真上の壁」（下がり壁の下の開口）


def _extents(fp: Polygon, d: np.ndarray) -> tuple[float, float]:
    """平面の形の、向き d に沿った長さと、それに直角な長さ。"""
    pts = np.array(fp.exterior.coords)
    n = np.array([-d[1], d[0]])
    return float(np.ptp(pts @ d)), float(np.ptp(pts @ n))


def assign_wall(fp: Polygon, sill: float, head: float, walls: list[Wall]) -> Wall | None:
    """穴の平面の形といちばん重なり、高さも重なる壁。"""
    return _assign(fp, sill, head, walls)[0]


def _assign(fp: Polygon, sill: float, head: float, walls: list[Wall]) -> tuple[Wall | None, bool]:
    """（壁, 下がり壁の下か）。★11/4（デモの家・物件C〜M）：開口の向きと直角な壁は選ばない（戸の枠の奥行き 145〜216mm を
    幅として拾い、交わる壁に床から戸の高さまでの切り欠きができていた）。高さの重なる壁がなければ、真上の下がり壁
    （下端が開口の上端と同じ）を選ぶ（開口の所には壁がないので、穴は開けない）。"""
    best, best_area, above, above_area = None, 0.0, None, 0.0
    for w in walls:
        along, across = _extents(fp, w.direction)
        if along < 0.5 * max(along, across):
            continue                                   # 開口の向きと直角な壁
        a = fp.intersection(w.rect()).area
        if a <= 0:
            continue
        top = w.top if w.top is not None else float("inf")
        if min(head, top) - max(sill, w.bottom) > 0:
            if a > best_area:
                best, best_area = w, a
        elif abs(w.bottom - head) <= TOUCH_MM and a > above_area:
            above, above_area = w, a
    return (best, False) if best is not None else (above, above is not None)


def openings(tate: list[Part], walls: list[Wall], fl: float) -> tuple[list[dict], list[dict]]:
    """開口の一覧と、壁が見つからなかった建具の一覧。壁は Wall の物で返す（番号は後で振る）。"""
    out, orphans = [], []
    for g in group_stacked([(p, *hole(p)) for p in tate]):
        parts = [h[0] for h in g]
        sill, head = min(h[2] for h in g), max(h[3] for h in g)
        if len(g) == 1:
            fp = g[0][1]
            kind, style = classify(parts[0], sill, fl)
        else:   # 上下2段の窓などは1つの穴にまとめる
            fp = bbox_polygon(np.vstack([np.array(h[1].exterior.coords) for h in g]))
            kind, style = _classify([_suffix(pr.code) for p in parts for pr in p.primitives], sill, fl)
        w, under = _assign(fp, sill, head, walls)
        if w is None:
            orphans += [{"source": p.source, "reason": "この建具が入る壁が見つからない"} for p in parts]
            continue
        d = w.direction
        pts = np.array(fp.exterior.coords)[:-1]
        proj = (pts - w.start) @ d
        a0, a1 = float(proj.min()), float(proj.max())
        center = np.asarray(fp.centroid.coords[0])
        notes = [f"stacked: 上下に重なる建具 {len(g)} 個を1つの穴にまとめた"] if len(g) > 1 else []
        if under:
            notes.append("under_hanging_wall: 下がり壁の下の開口（壁は開口の上端より上だけ）。穴は開けない")
        # ★引き込み戸（物件B の O17）：建具の箱は戸袋まで含む。戸袋は戸を隠す壁なので、穴は通れる所だけにする
        pocket = [pr.vertices[:, :2] for p in parts for pr in p.primitives if _suffix(pr.code) == POCKET and len(pr.vertices)]
        if pocket:
            hp = (np.vstack(pocket) - w.start) @ d
            h0, h1 = float(hp.min()), float(hp.max())
            n0, n1 = max((a0, min(a1, h0)), (max(a0, h1), a1), key=lambda s: s[1] - s[0])
            if n1 > n0:
                center = center + d * ((n0 + n1) - (a0 + a1)) / 2
                notes.append(f"pocket: 引き込み戸。戸が引き込まれる所（{h1 - h0:.0f}mm）は穴にせず、戸を隠す壁として残した")
                a0, a1 = n0, n1
        rec = {"wall": w, "kind": kind, "style": style,
               "offset_mm": a0, "width_mm": a1 - a0,
               "sill_mm": sill, "head_mm": head, "sill_fl_mm": sill - fl, "head_fl_mm": head - fl,
               "center": center, "source": parts[0].source, "footprint": fp}
        if len(g) > 1:
            rec["sources"] = [p.source for p in parts]
        if notes:
            rec["assumptions"] = notes
        out.append(rec)
    return out, orphans
