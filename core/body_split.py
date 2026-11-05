"""本体（基礎・1階・2階）を、造形範囲に入るように縦の面で分ける（★2026-11-05 作者 2-1）。

作者：「いい所で、なんなら分割ライン変えれる方がいい。内壁の半分だと接着の手間と壁が薄くなるから、それを考えた上で」。

- 書き出し（3MF・STL）のときだけ分ける。画面の 3D・番号・building.json は変えない（屋根と同じ。core/print_split.py）。
- 家全体で1組の縦の切る面（家の長い方向 u に直角）。造形範囲に入らない本体のオブジェクトだけを切る
  （入る2階・屋根は切らない。継ぎ目をまたいで支えてくれる）。屋根の面で入らないものも、同じ線で切る（core/export.py）。
- 線の決め方（自動）：切ったどれもが造形範囲に入る範囲の中で、
  - だめ：窓・ドアの幅の中（端から余白）、単独の壁（柱）を切る、斜めに交わる壁、壁を縦に割る、位置合わせの三角の所。
  - いちばん良い：横に通る壁の面。壁は厚いまま片方に残り、床の継ぎ目が壁の根元に隠れる
    （のり代は床と、切り口をまたぐ壁の切り口。壁の面にのりは付かない）。長い壁 → 真ん中に近い所、の順。
  - オブジェクトごとに切り口の断面を調べ、薄いかけら（基礎の外周の帯と外壁の面の差・土間・ポーチなど）ができるときは、
    近くの平行な面に少し寄せる。新しく離れた塊ができる所は選ばない。
- 人が平面図で決めた線は data/out/<物件>/print_split.json（向きと位置の数字だけ。名前・場所は書かない）。
  人の線は、入らないときだけ断る（窓や壁の途中を通るときは注意を出して、人の判断を優先する）。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from core.paths import out_dir

FILE_NAME = "print_split.json"
ROOF_OBJECTS = ("roof", "roof_lower")      # 屋根の道（面ごとに分けて寝かせる）で分けるオブジェクト
BED_MM = 256.0
SCAN_STEP_MM = 100.0                       # 動かせる範囲を探す刻み（実寸）
OPENING_MARGIN_MODEL_MM = 3.0              # 窓・ドアの端から、これだけ離して切る（模型 mm）
SNAP_MODEL_MM = 3.0                        # 人の線を、これより近い壁の面に寄せる（模型 mm）
SLIVER_AREA_MODEL_MM2 = 1.0                # 切り口の近くの薄いかけらの断面の許し（模型 mm²）
SLIVER_IGNORE_MODEL_MM = 0.3               # これより細い形は、かけらに数えない（印刷で形にならない。模型 mm）
FACE_SHIFT_MM = 0.5                        # 壁の面ちょうどで切ると厚みゼロの膜が残ることがあるので、部屋の側へ少しずらす（実寸）
NEAR_SLICE_MM = 2.0                        # 薄いかけらを調べる「すぐ横」の断面の位置（面ちょうどだと厚みゼロの形を見誤るので、少し離す）
PARALLEL_SIN = math.sin(math.radians(3))   # 壁が切る線と平行（u と直角）とみなす角度
OBLIQUE_SIN = math.sin(math.radians(30))   # 切る線と 30° より浅く交わる壁は、くさびのかけらになる
STANDALONE_STUB_MM = 900.0                 # 単独の壁を切ってよいのは、切った両側にこれだけ残るとき（短い単独の壁・柱は切らない。実寸）
ISLAND_SHARE = 0.02


@dataclass
class HouseCut:
    u: np.ndarray                          # 家の長い方向（切る面の法線。平面の単位ベクトル）
    cuts: list[float]                      # 家の分け目（u·p。実寸 mm）
    lo: float                              # 家の u の範囲
    hi: float
    ranges: list[tuple[float, float]]      # それぞれの分け目を動かせる範囲（どの部品も入る範囲）
    objects: dict[str, list[float]]        # 切るオブジェクト → そのオブジェクトの分け目（薄いかけらを避けて少し寄せたもの）
    sides: list[str]                       # 分けた部品の呼び名（南・北 など）
    source: str = "auto"                   # auto（自動）/ human（人が平面図で決めた）
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        r = lambda x: round(float(x), 1)
        return {"u": [round(float(self.u[0]), 6), round(float(self.u[1]), 6)], "cuts": [r(t) for t in self.cuts],
                "lo": r(self.lo), "hi": r(self.hi), "ranges": [[r(a), r(b)] for a, b in self.ranges],
                "objects": {k: [r(t) for t in v] for k, v in self.objects.items()}, "sides": list(self.sides),
                "source": self.source, "reasons": list(self.reasons), "warnings": list(self.warnings)}


# ---------------------------------------------------------------- 向き・名前・形

def house_axis(b: dict) -> np.ndarray:
    """家の長い方向：外壁の向きを長さで重みづけ（90° ごとに同じに見る）→ 外形が長い方の向き。
    最小の外接長方形だと、でこぼこの家で数度傾き、どの壁にも少し斜めになるため使わない。"""
    walls = [w for w in b.get("walls", []) if w.get("kind") == "exterior"] or list(b.get("walls", []))
    c = s = 0.0
    for w in walls:
        d = np.array(w["end"], float) - np.array(w["start"], float)
        L = float(np.hypot(*d))
        if L < 1.0:
            continue
        th = math.atan2(d[1], d[0])
        c += L * math.cos(4 * th)
        s += L * math.sin(4 * th)
    th0 = math.atan2(s, c) / 4 if (abs(c) + abs(s)) > 1e-9 else 0.0
    d1 = np.array([math.cos(th0), math.sin(th0)])
    d2 = np.array([-d1[1], d1[0]])
    pts = np.array([p for w in walls for p in (w["start"], w["end"])], float) if walls else np.zeros((2, 2))
    ext = lambda d: float((pts @ d).max() - (pts @ d).min())
    u = d1 if ext(d1) >= ext(d2) else d2
    if u[0] < -1e-9 or (abs(u[0]) <= 1e-9 and u[1] < 0):
        u = -u
    return u


def side_names(u: np.ndarray, n: int) -> list[str]:
    """分けた部品の呼び名（u の小さい方から）。2つなら「西・東」か「南・北」、3つなら真ん中は「中」。"""
    if abs(u[0]) >= abs(u[1]):
        a, z = ("西", "東") if u[0] > 0 else ("東", "西")
    else:
        a, z = ("南", "北") if u[1] > 0 else ("北", "南")
    if n == 1:
        return [""]
    if n == 2:
        return [a, z]
    if n == 3:
        return [a, "中", z]
    return [f"{i}" for i in range(1, n + 1)]


def _objects(parts: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for k, p in parts.items():
        out.setdefault(p.object_key or p.key, []).append(p)
    return out


def body_objects(parts: dict) -> dict[str, list]:
    """本体のオブジェクト（基礎・1階・2階）→ 部品の並び。屋根（面ごとに分ける道）は除く。"""
    return {k: v for k, v in _objects(parts).items() if k not in ROOF_OBJECTS and not parts[k].print_faces}


def _cut_line(u: np.ndarray, t: float, big: float = 1e6) -> LineString:
    v = np.array([-u[1], u[0]])
    return LineString([tuple(u * t - v * big), tuple(u * t + v * big)])


def _slab(u: np.ndarray, a: float, b: float, big: float = 1e6) -> Polygon:
    v = np.array([-u[1], u[0]])
    return Polygon([tuple(u * a + v * big), tuple(u * b + v * big), tuple(u * b - v * big), tuple(u * a - v * big)])


def fits(poly, limit: float) -> bool:
    """平面の形が、床の上で回して造形範囲（limit。形と同じ単位）に入るか。"""
    from core.print_split import best_yaw
    if poly is None or poly.is_empty:
        return True
    hull = poly.convex_hull
    if hull.geom_type != "Polygon":
        return True
    _, L = best_yaw(np.array(hull.exterior.coords), limit)
    return L <= limit + 1e-6


def _pieces_fit(polys: list, u: np.ndarray, cuts: list[float], limit: float) -> bool:
    edges = [-1e9] + sorted(cuts) + [1e9]
    return all(fits(p.intersection(_slab(u, a, b)), limit) for p in polys for a, b in zip(edges[:-1], edges[1:]))


def _u_range(polys: list, u: np.ndarray) -> tuple[float, float]:
    pts = np.array([c for p in polys for g in getattr(p, "geoms", [p]) for c in g.exterior.coords], float)
    proj = pts @ u
    return float(proj.min()), float(proj.max())


# ---------------------------------------------------------------- 線の良し悪し

def _walls_of(b: dict, levels: set[str] | None) -> list[dict]:
    names = [lv["name"] for lv in b.get("levels", [])]
    out = []
    for w in b.get("walls", []):
        lv = w.get("level") or (names[0] if names else None)
        if levels is None or lv in levels:
            out.append(w)
    return out


def hard_problems(b: dict, u: np.ndarray, t: float, walls: list[dict], keep_out, margin: float) -> list[str]:
    """分け目 t で困ること（日本語の短い文）。空なら問題なし。"""
    probs: list[str] = []
    ops_by: dict[str, list[dict]] = {}
    for o in b.get("openings", []):
        ops_by.setdefault(o["wall"], []).append(o)
    for w in walls:
        s, e = np.array(w["start"], float), np.array(w["end"], float)
        d = e - s
        L = float(np.hypot(*d))
        if L < 1.0:
            continue
        d /= L
        du = float(d @ u)
        th = float(w.get("thickness_mm", 0.0))
        su, eu = float(s @ u), float(e @ u)
        if abs(du) <= PARALLEL_SIN:                         # 切る線と平行な壁：壁の中を通るなら「縦に割る」
            c = (su + eu) / 2
            if c - th / 2 + 1.0 < t < c + th / 2 - 1.0:
                probs.append(f"壁 {w['id']} を縦に割る")
            continue
        if not (min(su, eu) <= t <= max(su, eu)):
            continue
        k = (t - su) / du                                   # 壁の始まりから、分け目までの長さ
        # 単独の壁：短いもの（柱など）や端の近くは切らない。家の長い方向に通る長い壁（デモの家の西の上の壁など）を
        # 直角に切るのはよい（両側とも長く残り、それぞれの床に載る。★11/5 これを断っていて、どの候補も落ちていた）
        if w.get("kind") == "standalone" and min(k, L - k) < STANDALONE_STUB_MM:
            probs.append(f"単独の壁 {w['id']} を切る")
            continue
        if abs(du) < OBLIQUE_SIN:
            probs.append(f"壁 {w['id']} と斜めに交わる")
        for o in ops_by.get(w["id"], []):
            a = float(o["offset_mm"])
            if a - margin <= k <= a + float(o["width_mm"]) + margin:
                probs.append(f"{'窓' if o.get('kind') == 'window' else 'ドア'} {o['id']} を通る")
    if keep_out is not None and not keep_out.is_empty and _cut_line(u, t).intersects(keep_out):
        probs.append("位置合わせの三角の所を通る")
    return probs


def _to_axis(solid, u: np.ndarray):
    """u が上（z）になるように回した立体（u·p で輪切りにするため）。"""
    v = np.array([-u[1], u[0]])
    T = np.array([[v[0], v[1], 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [u[0], u[1], 0.0, 0.0]])
    return solid.transform(T)


def sliver_area(axis_solid, t: float, s: float, ignore: float = 0.0) -> float:
    """分け目 t のそばの薄いかけらの断面積（実寸 mm²）：分け目のすぐ横にあって、s 離れた所にはない材料。
    ignore（実寸）より細い形は数えない（印刷しても形にならない細さ。★11/5 物件L：戸の穴に 5mm はみ出した壁の端）。"""
    out = 0.0
    for sign in (1.0, -1.0):
        d = axis_solid.slice(t + sign * NEAR_SLICE_MM) - axis_solid.slice(t + sign * s)
        if ignore > 0:
            d = d.offset(-ignore / 2)
        out += float(d.area())
    return out


def _face_positions(solid, u: np.ndarray, t: float, reach: float) -> list[float]:
    """立体の、分け目と平行な面の位置（t の ±reach の中）。"""
    m = solid.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3]
    f = np.asarray(m.tri_verts, dtype=np.int64)
    if not len(f):
        return []
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1)
    ok = ln > 1e-9
    nu = np.zeros(len(n))
    nu[ok] = (n[ok, 0] * u[0] + n[ok, 1] * u[1]) / ln[ok]
    pos = tri[:, :, :2].mean(axis=1) @ u
    sel = pos[(np.abs(nu) > 0.999) & (np.abs(pos - t) <= reach)]
    return sorted(set(round(float(x), 1) for x in sel), key=lambda x: abs(x - t))


def _island_ok(solid, u: np.ndarray, cuts: list[float]) -> bool:
    """切ったせいで、どの部品にも新しく大きな離れた塊ができないか。"""
    before = len([x for x in solid.decompose() if x.volume() > 1000.0])
    u3, nu3 = (float(u[0]), float(u[1]), 0.0), (-float(u[0]), -float(u[1]), 0.0)
    edges = [None] + sorted(cuts) + [None]
    for a, z in zip(edges[:-1], edges[1:]):
        x = solid
        if a is not None:
            x = x.trim_by_plane(u3, a)
        if z is not None:
            x = x.trim_by_plane(nu3, -z)
        if x.is_empty():
            continue
        bodies = [y for y in x.decompose() if y.volume() > 1000.0]
        total = sum(y.volume() for y in bodies) or 1.0
        big = [y for y in bodies if y.volume() >= ISLAND_SHARE * total]
        if len(big) > max(1, before):
            return False
    return True


# ---------------------------------------------------------------- 保存（人が平面図で決めた分け目）

def saved_path(alias: str) -> Path:
    return out_dir(alias) / FILE_NAME


def load_saved(alias: str) -> dict | None:
    p = saved_path(alias)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("cuts"), list) and isinstance(d.get("u"), list) else None


def save_human(alias: str, u: np.ndarray, cuts: list[float]) -> None:
    p = saved_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"u": [float(u[0]), float(u[1])], "cuts": [round(float(t), 1) for t in cuts], "source": "human",
            "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".print_split.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def clear_saved(alias: str) -> None:
    p = saved_path(alias)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------- 分け目を決める

def _union(members):
    from core.builder import union_all
    solids = [p.solid for p in members if not p.solid.is_empty()]
    return solids[0] if len(solids) == 1 else union_all(solids)


def choose(b: dict, parts: dict, rules: dict, saved: dict | None = None) -> HouseCut | None:
    """分け目を決める。どの本体も入るなら None。"""
    from core.builder import footprint_of
    m = rules["model"]
    scale = float(m["scale"])
    limit = (BED_MM - 2 * float(m.get("print_bed_margin_mm", 5.0))) * scale          # 実寸
    min_feat = float(m.get("min_feature_mm", 1.0)) * scale
    objs = body_objects(parts)
    solids = {k: _union(v) for k, v in objs.items()}
    fps = {k: footprint_of(s) for k, s in solids.items() if not s.is_empty()}
    need = [k for k, fp in fps.items() if not fits(fp, limit)]
    if not need:
        return None
    u = house_axis(b)
    polys = [fps[k] for k in need]
    lo, hi = _u_range(polys, u)
    levels = {lv["name"] for lv in b.get("levels", [])[: 1 + int("floor2" in need)]} or None
    walls = _walls_of(b, levels)
    keep = [z for k in objs for p in objs[k] for z in getattr(p, "keep_out", [])]
    keep_out = unary_union(keep) if keep else None
    margin = OPENING_MARGIN_MODEL_MM * scale
    s = 2 * min_feat
    ignore = SLIVER_IGNORE_MODEL_MM * scale
    axis_solids = {k: _to_axis(solids[k], u) for k in need}
    warnings: list[str] = []

    # 何本で分けるか・それぞれを動かせる範囲（1本のときは細かく探す。2本以上は等分から）
    n = max(1, math.ceil((hi - lo) / (limit * 0.98)) - 1)
    ranges: list[tuple[float, float]] = []
    if n == 1:
        ts = np.arange(lo + SCAN_STEP_MM, hi, SCAN_STEP_MM)
        ok = [float(t) for t in ts if _pieces_fit(polys, u, [float(t)], limit)]
        if not ok:
            n = 2
        else:
            ranges = [(min(ok), max(ok))]
    if n >= 2:
        even = [lo + (hi - lo) * (i + 1) / (n + 1) for i in range(n)]
        if not _pieces_fit(polys, u, even, limit):
            return HouseCut(u=u, cuts=[], lo=lo, hi=hi, ranges=[], objects={}, sides=[],
                            warnings=["長すぎて、縦の分け目では造形範囲に入る大きさに分けられませんでした"])
        step = (hi - lo) / (n + 1)
        ranges = [(t - step / 3, t + step / 3) for t in even]

    def per_object(cuts: list[float], human: bool) -> tuple[dict[str, list[float]], list[str]] | None:
        """オブジェクトごとに、薄いかけらを避けて分け目を寄せる。寄せても残るときは None（人の線なら注意だけ）。"""
        out, notes = {}, []
        for k in need:
            mine = []
            for t in cuts:
                if sliver_area(axis_solids[k], t, s, ignore) <= SLIVER_AREA_MODEL_MM2 * scale ** 2:
                    mine.append(t)
                    continue
                moved = None
                for f in _face_positions(solids[k], u, t, 2 * s):
                    for g in (f - FACE_SHIFT_MM, f + FACE_SHIFT_MM):
                        if sliver_area(axis_solids[k], g, s, ignore) <= SLIVER_AREA_MODEL_MM2 * scale ** 2 and \
                                _pieces_fit([fps[k]], u, [g if x == t else x for x in cuts], limit):
                            moved = g
                            break
                    if moved is not None:
                        break
                if moved is None:
                    if not human:
                        return None
                    notes.append(f"{objs[k][0].label.split('（')[0]}：分け目のそばに薄いかけらができます")
                    mine.append(t)
                else:
                    mine.append(moved)
                    if abs(moved - t) > 1.0:
                        notes.append(f"{objs[k][0].label.split('（')[0]}は、薄いかけらを作らないように分け目を {abs(moved - t):.0f}mm 寄せた")
            if not human and not _island_ok(solids[k], u, mine):
                return None
            out[k] = mine
        return out, notes

    def make(cuts, objects, source, reasons, notes):
        return HouseCut(u=u, cuts=list(cuts), lo=lo, hi=hi, ranges=ranges, objects=objects, sides=side_names(u, len(cuts) + 1),
                        source=source, reasons=reasons, warnings=warnings + notes)

    # 人が決めた分け目（向きが同じで、本数が同じで、どれも入るときだけ使う）
    if saved and saved.get("source") == "human":
        su = np.array(saved["u"], float)
        cuts = [float(t) for t in saved["cuts"]]
        if len(cuts) == n and abs(float(su @ u)) > 0.999 and _pieces_fit(polys, u, cuts, limit):
            probs = [p for t in cuts for p in hard_problems(b, u, t, walls, keep_out, margin)]
            got = per_object(cuts, human=True)
            objects, notes = got if got else ({k: cuts for k in need}, [])
            return make(cuts, objects, "human", ["平面図で決めた分け目"], [f"分け目が{p}" for p in dict.fromkeys(probs)] + notes)
        warnings.append("保存した分け目では造形範囲に入らない（または家の形が変わった）ので、自動の分け目に戻した")

    # 自動：壁の面 → 部屋の中、の順に探す
    if n == 1:
        a, z = ranges[0]
        mid = (lo + hi) / 2
        cands = []
        for w in walls:
            sv, ev = np.array(w["start"], float), np.array(w["end"], float)
            d = ev - sv
            L = float(np.hypot(*d))
            if L < 1.0 or abs(float(d @ u)) / L > PARALLEL_SIN:
                continue
            c = float(((sv + ev) / 2) @ u)
            th = float(w.get("thickness_mm", 0.0))
            for face, shift in ((c - th / 2, -FACE_SHIFT_MM), (c + th / 2, FACE_SHIFT_MM)):
                t = face + shift
                if not (a <= t <= z):
                    continue
                inside = unary_union(polys).intersection(_cut_line(u, t)).length or 1.0
                cover = min(1.0, L / inside)
                cands.append((-round(cover, 1), abs(t - mid), t, w))
        for _, _, t, w in sorted(cands, key=lambda c: (c[0], c[1])):
            if hard_problems(b, u, t, walls, keep_out, margin):
                continue
            got = per_object([t], human=False)
            if got:
                kind = {"interior": "内壁", "exterior": "外壁", "standalone": "単独の壁"}.get(w.get("kind"), "壁")
                return make([t], got[0], "auto", [f"{kind} {w['id']} の面（壁は厚いまま片方に残り、床の継ぎ目が壁の根元に隠れる）"], got[1])
        for t in sorted((float(x) for x in np.arange(a, z + 1e-6, 50.0)), key=lambda x: abs(x - mid)):
            if hard_problems(b, u, t, walls, keep_out, margin):
                continue
            got = per_object([t], human=False)
            if got:
                return make([t], got[0], "auto", ["壁のない所（部屋の中）。のり代は床と外壁の切り口だけ"],
                            got[1] + ["横に通る壁の面で切れなかったので、部屋の中で切った"])
        t = (a + z) / 2
        return make([t], {k: [t] for k in need}, "auto", ["真ん中"],
                    ["窓・ドア・壁を避けられる分け目が見つからなかったので、動かせる範囲の真ん中で切った（平面図で動かしてください）"])
    cuts = [(r0 + r1) / 2 for r0, r1 in ranges]
    got = per_object(cuts, human=True)
    objects, notes = got if got else ({k: cuts for k in need}, [])
    return make(cuts, objects, "auto", ["等分"], notes + ["3つ以上に分けるので、等分の位置で切った（平面図で動かしてください）"])


def snap_to_face(b: dict, u: np.ndarray, t: float, scale: float) -> float:
    """人の線を、近く（SNAP_MODEL_MM）の、切る線と平行な壁の面に寄せる（部屋の側へ少しずらす）。なければそのまま。"""
    best = None
    for w in b.get("walls", []):
        sv, ev = np.array(w["start"], float), np.array(w["end"], float)
        d = ev - sv
        L = float(np.hypot(*d))
        if L < 1.0 or abs(float(d @ u)) / L > PARALLEL_SIN:
            continue
        c = float(((sv + ev) / 2) @ u)
        th = float(w.get("thickness_mm", 0.0))
        for face, shift in ((c - th / 2, -FACE_SHIFT_MM), (c + th / 2, FACE_SHIFT_MM)):
            dist = abs(face - t)
            if dist <= SNAP_MODEL_MM * scale and (best is None or dist < best[0]):
                best = (dist, face + shift)
    return best[1] if best else t
