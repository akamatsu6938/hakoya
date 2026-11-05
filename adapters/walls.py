"""壁の作り直し（SPEC §4-1 の手順1〜6）。平面（2D）で壁を作り、中心線＋厚みで返す。

glb の壁は「面」だけなので、面の向き（法線）を手がかりに厚みのある壁へ作り直す。
向きだけで決めるので、斜めの壁にも同じ式が使える（物件Aには斜めの壁がない。要検証）。

用語：
- 平面（Plane）：同じ向き・同じ位置にある垂直な三角形の集まり（壁の1つの面）。
- t：平面に沿った位置（mm）。p(t) = n*offset + t*接線。
- 帯（band）：面から裏側（法線の反対向き）へ厚み分ふくらませた長方形。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box
from shapely.ops import unary_union

ANGLE_STEP_DEG = 0.5   # 向きをまとめる細かさ
OFFSET_TOL_MM = 2.0    # 同じ平面とみなす位置の差


# ---------------------------------------------------------------- 基本の形

@dataclass(eq=False)
class Plane:
    n: np.ndarray            # 面の向き（2D の単位ベクトル。面が向いている側）
    offset: float            # n·p（原点からの距離）
    tz: np.ndarray           # (K, 3, 2) 三角形を (t, z) で表したもの
    sources: set[str] = field(default_factory=set)

    @property
    def tangent(self) -> np.ndarray:
        return np.array([-self.n[1], self.n[0]])

    def point(self, t: float) -> np.ndarray:
        return self.n * self.offset + self.tangent * t


@dataclass(eq=False)
class Piece:
    """平面の一部（t0〜t1）。bottom/top はその範囲の壁の下端・上端（mm）。"""
    plane: Plane
    t0: float
    t1: float
    bottom: float
    top: float

    @property
    def length(self) -> float:
        return self.t1 - self.t0

    @property
    def p0(self) -> np.ndarray:
        return self.plane.point(self.t0)

    @property
    def p1(self) -> np.ndarray:
        return self.plane.point(self.t1)

    def band(self, depth: float) -> Polygon:
        """面から裏側へ depth の帯。"""
        n = self.plane.n
        a, b = self.p0, self.p1
        return Polygon([a, b, b - n * depth, a - n * depth])


@dataclass(eq=False)
class Wall:
    start: np.ndarray
    end: np.ndarray
    thickness: float
    kind: str                      # exterior / interior / standalone
    bottom: float
    top: float | None = None       # None のときは「屋根まで」
    sources: list[str] = field(default_factory=list)
    top_glb: float | None = None   # glb の面が実際に届いている上端（外壁。屋根で切るかは組み立てで決める）
    assumptions: list[str] = field(default_factory=list)

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.end - self.start))

    @property
    def direction(self) -> np.ndarray:
        d = self.end - self.start
        return d / (np.linalg.norm(d) or 1.0)

    def rect(self, thickness: float | None = None) -> Polygon:
        t = (self.thickness if thickness is None else thickness) / 2
        d = self.direction
        n = np.array([-d[1], d[0]])
        a, b = self.start, self.end
        return Polygon([a + n * t, b + n * t, b - n * t, a - n * t])


# ---------------------------------------------------------------- 三角形 → 平面

def vertical_triangles(faces: np.ndarray, source: str) -> list[tuple[np.ndarray, np.ndarray, str]]:
    """(K,3,3) の三角形から、垂直なものを (三角形, 2D の向き, 出どころ) で返す。"""
    out = []
    if not len(faces):
        return out
    nrm = np.cross(faces[:, 1] - faces[:, 0], faces[:, 2] - faces[:, 0])
    area = np.linalg.norm(nrm, axis=1)
    for f, n, a in zip(faces, nrm, area):
        if a < 1e-6:
            continue
        n = n / a
        if abs(n[2]) > 0.01:
            continue
        n2 = n[:2] / np.linalg.norm(n[:2])
        out.append((f, n2, source))
    return out


def group_planes(tris: list[tuple[np.ndarray, np.ndarray, str]]) -> list[Plane]:
    """垂直な三角形を、向きと位置が同じもの同士にまとめる。"""
    buckets: dict[float, list] = {}
    for f, n, src in tris:
        ang = math.degrees(math.atan2(n[1], n[0]))
        key = (round(ang / ANGLE_STEP_DEG) * ANGLE_STEP_DEG) % 360.0
        buckets.setdefault(key, []).append((f, n, src))
    planes: list[Plane] = []
    for items in buckets.values():
        n_mean = np.mean([n for _, n, _ in items], axis=0)
        n_mean /= np.linalg.norm(n_mean)
        items.sort(key=lambda it: float(np.dot(it[0][0, :2], n_mean)))
        cluster: list = []
        last = None
        for it in items:
            off = float(np.dot(it[0][0, :2], n_mean))
            if cluster and off - last > OFFSET_TOL_MM:
                planes.append(_make_plane(cluster, n_mean))
                cluster = []
            cluster.append(it)
            last = off
        if cluster:
            planes.append(_make_plane(cluster, n_mean))
    return planes


def _make_plane(items, n: np.ndarray) -> Plane:
    offs = [float(np.mean(f[:, :2] @ n)) for f, _, _ in items]
    tangent = np.array([-n[1], n[0]])
    tz = np.array([np.column_stack([f[:, :2] @ tangent, f[:, 2]]) for f, _, _ in items])
    return Plane(n=n.copy(), offset=float(np.mean(offs)), tz=tz, sources={s for _, _, s in items})


# ---------------------------------------------------------------- 平面 → 区間

def _cross_section(tz: np.ndarray, t: float) -> tuple[np.ndarray, np.ndarray]:
    """位置 t で縦に切ったときの、各三角形の下端・上端。三角形にかからなければ除く。"""
    zmin = np.full(len(tz), np.inf)
    zmax = np.full(len(tz), -np.inf)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        ti, zi, tj, zj = tz[:, i, 0], tz[:, i, 1], tz[:, j, 0], tz[:, j, 1]
        inside = (np.minimum(ti, tj) <= t + 1e-6) & (np.maximum(ti, tj) >= t - 1e-6)
        dt = tj - ti
        steep = np.abs(dt) < 1e-9
        with np.errstate(divide="ignore", invalid="ignore"):
            z = zi + (t - ti) * (zj - zi) / np.where(steep, 1.0, dt)
        lo = np.where(steep, np.minimum(zi, zj), z)
        hi = np.where(steep, np.maximum(zi, zj), z)
        zmin = np.where(inside, np.minimum(zmin, lo), zmin)
        zmax = np.where(inside, np.maximum(zmax, hi), zmax)
    ok = np.isfinite(zmin) & (zmax - zmin > 1.0)
    return zmin[ok], zmax[ok]


def plane_pieces(plane: Plane, classify=None) -> list[Piece]:
    """平面を、面がある区間ごとに分ける。classify(t0, t1, bottom, top) が違う所でも分ける。"""
    ts = np.unique(np.round(plane.tz[:, :, 0].ravel(), 1))
    raw: list[tuple[float, float, float, float]] = []
    for u, v in zip(ts[:-1], ts[1:]):
        if v - u < 1.0:
            continue
        lo, hi = _cross_section(plane.tz, (u + v) / 2)
        if len(lo):
            raw.append((float(u), float(v), float(lo.min()), float(hi.max())))
    pieces: list[Piece] = []
    for u, v, b, t in raw:
        key = classify(u, v, b, t) if classify else None
        last = pieces[-1] if pieces else None
        if last is not None and u - last.t1 < 1.5 and getattr(last, "_key", None) == key:
            last.t1, last.bottom, last.top = v, min(last.bottom, b), max(last.top, t)
            continue
        p = Piece(plane, u, v, b, t)
        p._key = key  # type: ignore[attr-defined]
        pieces.append(p)
    return pieces


# ---------------------------------------------------------------- 手順1：外壁

def exterior_walls(planes: list[Plane], thickness: float, fl_mm: float, opening_fps: list[Polygon],
                   upper_min_above_fl: float = 300.0, min_len: float = 20.0) -> list[Wall]:
    """外壁の面（外側の面だけ）から、厚み thickness の外壁を作る。

    途中の高さから始まる壁（ポーチの上の垂れ壁など）は、床から始まる壁と分けて下端を持たせる。
    ただし開口（建具の箱）の位置で下端が上がっているのは開口なので、分けない。
    """
    openings = unary_union(opening_fps) if opening_fps else None

    def make_classify(plane: Plane):
        def classify(u, v, bottom, top):
            if bottom <= fl_mm + upper_min_above_fl:
                return "base"
            if openings is not None and openings.buffer(50).contains(Point(plane.point((u + v) / 2))):
                return "base"
            return ("upper", round(bottom))
        return classify

    pieces = [pc for pl in planes for pc in plane_pieces(pl, make_classify(pl)) if pc.length >= min_len]
    walls: list[Wall] = []
    for pc in pieces:
        n = pc.plane.n
        start = pc.p0 - n * thickness / 2
        end = pc.p1 - n * thickness / 2
        key = getattr(pc, "_key", "base")
        w = Wall(start=start, end=end, thickness=thickness, kind="exterior",
                 bottom=pc.bottom, top=None, sources=sorted(pc.plane.sources), top_glb=pc.top)
        if key != "base":
            w.assumptions.append("upper_wall: 途中の高さから始まる壁（下端は glb の面の下端）")
        w._outer = pc  # type: ignore[attr-defined]
        walls.append(w)
    return walls


def fill_inside_corners(walls: list[Wall], tol: float = 3.0, min_z_overlap: float = 300.0) -> None:
    """外壁の入隅（へこんだ角）のすき間を埋める。

    帯は面に沿ってしか伸びないので、入隅では角の正方形（厚み×厚み）が抜ける。
    2つの外の面の端が同じ点で出会い、相手の面がこちらの面の外側へ向かって伸びているとき、
    こちらの壁をその端で厚み分だけ伸ばす（1つの端につき1回だけ）。
    高さが重ならない面どうし（ポーチの上の垂れ壁と、その下の壁など）は角にならないので除く。
    """
    outers = [(w, getattr(w, "_outer", None)) for w in walls]
    outers = [(w, pc) for w, pc in outers if pc is not None]
    for w, pc in outers:
        n = pc.plane.n
        for which, e in (("start", pc.p0), ("end", pc.p1)):
            if _find_inside_corner(pc, e, outers, w, tol, min_z_overlap):
                d = w.direction if which == "end" else -w.direction
                setattr(w, which, getattr(w, which) + d * w.thickness)
                w.assumptions.append("inside_corner: 入隅のすき間を埋めるため、端を厚み分伸ばした")


def close_t_gaps(walls: list[Wall], max_gap: float = 50.0, min_z_overlap: float = 300.0, min_gap: float = 5.0) -> int:
    """外壁の端と、直角に交わる外壁の面の間の小さなすき間（max_gap 以下）を、端を相手の中心線まで伸ばしてつなぐ（T字）。
    ★11/4 デモ用：北西の角で、北の外壁が袖壁として家の外へ続き、角に単独の壁（袖壁）があったため、入隅の埋め
    （fill_inside_corners。外壁どうしの入隅だけを見る）が効かず、西の外壁の端と北の外壁の内側の面の間に 18mm のすき間が残った。
    すき間のせいで、ずれ止めの三角が壁の中に置かれて組むとぶつかり、角が辺だけで接して屋根の部品が閉じなかった。
    min_gap 以下のすき間（物件A の垂れ壁の端の 2.8mm など。模型で 0.1mm 未満）は、見た目にも組み立てにも効かないので触らない。
    伸ばした端の数を返す。"""
    n_fixed = 0
    for a in walls:
        if a.length <= 0:
            continue
        for which in ("start", "end"):
            p = getattr(a, which)
            d = a.direction if which == "end" else -a.direction           # 端の外向き
            best = None
            for b in walls:
                if b is a or b.length <= 0 or abs(float(np.dot(b.direction, a.direction))) > 0.1:
                    continue                                              # 直角に近い相手だけ
                if min((a.top if a.top is not None else 1e9), (b.top if b.top is not None else 1e9)) - max(a.bottom, b.bottom) < min_z_overlap:
                    continue
                nb = np.array([-b.direction[1], b.direction[0]])
                dist_c = float(np.dot(p - b.start, nb))                   # 相手の中心線からの符号つき距離
                if abs(float(np.dot(d, nb))) < 0.9:
                    continue
                s = float(np.dot(p - b.start, b.direction))               # 相手の長さの中か（厚みの半分＋α の余裕）
                if s < -a.thickness or s > b.length + a.thickness:
                    continue
                toward = -np.sign(dist_c) if abs(dist_c) > 1e-6 else 0.0  # 端が相手へ向かっているか
                if toward == 0.0 or float(np.dot(d, nb)) * toward < 0.9:
                    continue
                gap = abs(dist_c) - b.thickness / 2                       # 端から相手の近い面まで
                if min_gap < gap <= max_gap and (best is None or gap < best[0]):
                    best = (gap, abs(dist_c))
            if best is not None:
                setattr(a, which, p + d * best[1])                        # 相手の中心線まで伸ばす
                a.assumptions.append(f"t_gap: 直角の外壁との間の {best[0]:.0f}mm のすき間を、端を伸ばしてつないだ")
                n_fixed += 1
    return n_fixed


def keep_outline_back_to_back(walls: list[Wall], thickness: float, line_tol: float = 20.0, end_tol: float = 60.0) -> int:
    """背中合わせの面から作った内壁（merge_back_to_back）のうち、外壁と同じ線の上で端どうしがつながっているもの
    （家の外周の壁の続き）は、外壁に戻す。戻した数を返す。
    ★11/4 物件H：西の外周の壁の一部（間 162mm＝外壁の厚みの背中合わせ）が内壁にされ、外壁の輪が切れて、
    基礎の外形・1階の床の板が作れなかった。物件A の背中合わせ（間 90mm、部屋と部屋の間）は外周ではないので変わらない。"""
    n = 0
    exts = [w for w in walls if w.kind == "exterior"]
    for w in walls:
        if w.kind != "interior" or not any("back_to_back" in a for a in w.assumptions) or w.length <= 0:
            continue
        nrm = np.array([-w.direction[1], w.direction[0]])
        for e in exts:
            if e.length <= 0 or abs(float(np.dot(e.direction, w.direction))) < 0.999:
                continue                                          # 平行な外壁だけ
            if abs(float(np.dot(e.start - w.start, nrm))) > line_tol:
                continue                                          # 同じ線の上
            if min(np.linalg.norm(a - b) for a in (w.start, w.end) for b in (e.start, e.end)) > end_tol:
                continue                                          # 端どうしがつながる
            w.kind, w.thickness, w.top = "exterior", thickness, None
            w.assumptions.append("outline: 外周の外壁と同じ線で端がつながるので、背中合わせでも外壁のままにした")
            n += 1
            break
    return n


def _find_inside_corner(pc: Piece, e: np.ndarray, outers, w: Wall, tol: float, min_z_overlap: float) -> bool:
    n = pc.plane.n
    for w2, pc2 in outers:
        if w2 is w or abs(np.dot(pc2.plane.n, n)) > 0.999:
            continue
        if min(pc.top, pc2.top) - max(pc.bottom, pc2.bottom) < min_z_overlap:
            continue
        for e2, other in ((pc2.p0, pc2.p1), (pc2.p1, pc2.p0)):
            if np.linalg.norm(e2 - e) > tol:
                continue
            v = other - e2
            v = v / (np.linalg.norm(v) or 1.0)
            if np.dot(v, n) > 0.5:  # 相手はこちらの面の外側へ伸びている → 入隅
                return True
    return False


def merge_back_to_back(walls: list[Wall], thickness: float, factor: float = 2.0, min_overlap: float = 300.0,
                       as_kind: str = "interior", new_thickness: float | None = None,
                       new_top: float | None = None) -> list[Wall]:
    """背中合わせの外壁の面（SPEC §4-1 の既知の例外）を1枚の壁にまとめる。

    2つの面が反対を向き、裏同士が向き合い、その間が外壁の厚みの factor 倍より近く、
    min_overlap 以上重なっているとき、重なっている区間を、面の真ん中を中心線にした1枚にする。
    （★窓と窓の間の柱型の脇の面（見込み 162mm）も背中合わせに見えるので、短い重なりは除く）
    ★物件Aでは実際は内壁だった（作者確認、2026-11-02）。そのため既定は内壁（as_kind）として、
    厚み new_thickness（会社ルールの内壁の厚み）・上端 new_top（天井）で作る。
    """
    new_thickness = thickness if new_thickness is None else new_thickness
    out = list(walls)
    i = 0
    while i < len(out):
        a = out[i]
        pa = getattr(a, "_outer", None)
        merged = False
        for j in range(i + 1, len(out)):
            b = out[j]
            pb = getattr(b, "_outer", None)
            if pa is None or pb is None:
                continue
            na, nb = pa.plane.n, pb.plane.n
            if np.dot(na, nb) > -0.999:
                continue
            gap = -float(np.dot(pb.p0 - pa.p0, na))  # a の裏側へ b がどれだけ離れているか
            if not (0 < gap < thickness * factor):
                continue
            tan = pa.plane.tangent
            a0, a1 = sorted((float(np.dot(pa.p0, tan)), float(np.dot(pa.p1, tan))))
            b0, b1 = sorted((float(np.dot(pb.p0, tan)), float(np.dot(pb.p1, tan))))
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo < min_overlap:
                continue
            mid_off = float(np.dot(pa.p0, na)) - gap / 2
            p_lo = na * mid_off + tan * lo
            p_hi = na * mid_off + tan * hi
            w = Wall(start=p_lo, end=p_hi, thickness=new_thickness, kind=as_kind,
                     bottom=min(a.bottom, b.bottom), top=new_top, sources=sorted(set(a.sources) | set(b.sources)),
                     top_glb=max(pa.top, pb.top),
                     assumptions=[f"back_to_back: glb では背中合わせの外壁の面（間 {gap:.0f}mm）として入っていた。"
                                  f"1枚の{'内壁' if as_kind == 'interior' else '外壁'}として作り直した"])
            w._back_to_back_gap = gap  # type: ignore[attr-defined]
            rest = []
            for wall, (s0, s1) in ((a, (a0, a1)), (b, (b0, b1))):
                for r0, r1 in ((s0, lo), (hi, s1)):
                    if r1 - r0 >= 20:
                        rest.append(_cut_wall(wall, tan, r0, r1))
            out = [x for k, x in enumerate(out) if k not in (i, j)] + [w] + rest
            merged = True
            break
        if not merged:
            i += 1
    return out


def _cut_wall(w: Wall, tan: np.ndarray, t0: float, t1: float) -> Wall:
    """壁 w の、接線方向 t0〜t1 の部分。"""
    base = w.start - tan * float(np.dot(w.start, tan))
    nw = Wall(start=base + tan * t0, end=base + tan * t1, thickness=w.thickness, kind=w.kind,
              bottom=w.bottom, top=w.top, sources=list(w.sources), assumptions=list(w.assumptions), top_glb=w.top_glb)
    outer = getattr(w, "_outer", None)
    if outer is not None:
        nw._outer = outer  # type: ignore[attr-defined]
    return nw


def z_range(w: Wall) -> tuple[float, float]:
    """壁の高さの範囲。「屋根まで」の壁は上を無限とみなす。"""
    return w.bottom, (w.top if w.top is not None else math.inf)


def z_overlap_ratio(w: Wall, other: Wall) -> float:
    """w の高さのうち、other と重なっている割合。"""
    b0, t0 = z_range(w)
    b1, t1 = z_range(other)
    h = (t0 - b0) if math.isfinite(t0) else 1.0
    ov = min(t0, t1) - max(b0, b1)
    if not math.isfinite(ov):
        return 1.0
    return max(0.0, ov) / h if math.isfinite(t0) else (1.0 if ov > 0 else 0.0)


def drop_covered(walls: list[Wall], keep_kinds: tuple[str, ...] = ("standalone",), ratio: float = 0.9) -> list[Wall]:
    """ほかの壁にほぼ覆われている壁（窓の脇の見込みの面、単独の壁と重なる面など）を除く。短いものから順に調べる。
    高さが重ならない壁（下がり壁とその下の腰の部分など）は、覆っているとはみなさない。"""
    kept = list(walls)
    for w in sorted(walls, key=lambda x: x.length):
        if w.kind in keep_kinds or w not in kept:
            continue
        others = unary_union([x.rect() for x in kept if x is not w and z_overlap_ratio(w, x) >= 0.5])
        r = w.rect()
        if r.area > 0 and r.intersection(others).area / r.area >= ratio:
            kept.remove(w)
    return kept


# ---------------------------------------------------------------- 手順4：単独の壁

def standalone_wall(faces: np.ndarray, source: str, min_area_mm2: float = 50_000.0,
                    min_height_mm: float = 300.0) -> Wall | None:
    """厚みのある単独の壁（typeEKabe）。垂直な面がほとんどないもの（上端の板だけ）は None。

    ★片面しか入っていない壁がある（物件A：もう片面が外壁の部材にある／屋根裏側の面がない）。
    そのため、平面の形は上端・下端の板も含めた部材全体の広がりから取る。高さは垂直な面から取る。
    """
    tris = vertical_triangles(faces, source)
    if not tris:
        return None
    area = sum(0.5 * np.linalg.norm(np.cross(f[1] - f[0], f[2] - f[0])) for f, _, _ in tris)
    zv = np.concatenate([f[:, 2] for f, _, _ in tris])
    if area < min_area_mm2 or zv.max() - zv.min() < min_height_mm:  # 笠木など、高さのないもの
        return None
    pts = faces[:, :, :2].reshape(-1, 2)
    rect = MultiPoint([tuple(p) for p in pts]).minimum_rotated_rectangle
    if rect.geom_type != "Polygon":  # 厚みがまったくない
        return None
    c = np.array(rect.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    if np.linalg.norm(e1) >= np.linalg.norm(e2):
        long_a, long_b, short = (c[0] + c[3]) / 2, (c[1] + c[2]) / 2, np.linalg.norm(e2)
    else:
        long_a, long_b, short = (c[0] + c[1]) / 2, (c[3] + c[2]) / 2, np.linalg.norm(e1)
    z = np.concatenate([f[:, 2] for f, _, _ in tris])
    return Wall(start=long_a, end=long_b, thickness=float(short), kind="standalone",
                bottom=float(z.min()), top=float(z.max()), sources=[source])


# ---------------------------------------------------------------- 手順2・3・6：内壁

def interior_region(room_floors: list[Polygon], finish_planes: list[Plane], door_fps: list[Polygon],
                    exterior: Polygon, carve_mm: float = 110.0, gap_max_mm: float = 260.0) -> tuple[Polygon, Polygon]:
    """部屋の内側と、内壁の範囲を返す。

    1. 部屋の床から、各壁の仕上げ面の裏側の帯（carve_mm）を削る。ドアの位置も削る。
    2. 部屋の内側をすべて合わせ、幅 gap_max_mm 以下の隙間を埋める（膨らませてから縮める）。
    3. 埋まった部分から、部屋の内側と外壁を除いたものが内壁の範囲。
    """
    bands = [pc.band(carve_mm) for pl in finish_planes for pc in plane_pieces(pl) if pc.length >= 20]
    cut = unary_union(bands + list(door_fps))
    rooms = unary_union(room_floors).difference(cut)
    r = gap_max_mm / 2
    filled = rooms.buffer(r, join_style="mitre").buffer(-r, join_style="mitre")
    region = filled.difference(rooms).difference(exterior.buffer(1.0, join_style="mitre"))
    region = region.buffer(-15, join_style="mitre").buffer(15, join_style="mitre")  # 細いすき間（30mm未満）を除く
    return rooms, region


def interior_walls(finish_planes: list[Plane], region: Polygon, thickness: float,
                   gap_min: float = 80.0, gap_max: float = 270.0, min_overlap: float = 50.0,
                   min_open: float = 300.0) -> list[Wall]:
    """向かい合う仕上げ面の真ん中を中心線にして、内壁を作る（厚みは会社ルールの値で作り直す）。
    壁の全長にわたる縦の抜け（min_open 以上）があれば、壁を上下に分ける（★物件Aの下がり壁と腰掛け）。"""
    faces = [pc for pl in finish_planes for pc in plane_pieces(pl) if pc.length >= 20]
    zone = region.buffer(10)
    cands: list[Wall] = []
    for i, a in enumerate(faces):
        na = a.plane.n
        for b in faces[i + 1:]:
            nb = b.plane.n
            if np.dot(na, nb) > -0.999:
                continue
            gap = -float(np.dot(b.p0 - a.p0, na))
            if not (gap_min <= gap <= gap_max):
                continue
            tan = a.plane.tangent
            a0, a1 = sorted((float(np.dot(a.p0, tan)), float(np.dot(a.p1, tan))))
            b0, b1 = sorted((float(np.dot(b.p0, tan)), float(np.dot(b.p1, tan))))
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo < min_overlap:
                continue
            mid = float(np.dot(a.p0, na)) - gap / 2
            p0, p1 = na * mid + tan * lo, na * mid + tan * hi
            if not zone.contains(Point((p0 + p1) / 2)):
                continue
            bands = solid_bands(a, b, lo, hi, min_gap=min_open)
            for z0, z1 in bands:
                w = Wall(start=p0, end=p1, thickness=thickness, kind="interior", bottom=z0, top=z1,
                         sources=sorted(a.plane.sources | b.plane.sources),
                         assumptions=[f"interior_rebuilt: 仕上げ面の間 {gap:.0f}mm の中心線に、会社ルールの厚みで作り直した"])
                if len(bands) > 1:
                    w.assumptions.append(f"open_band: 壁の全長にわたって縦の抜けがあるため（開口）、"
                                         f"{z0:.0f}〜{z1:.0f} の部分だけを壁にした")
                w._gap = gap  # type: ignore[attr-defined]
                cands.append(w)
    return merge_collinear(cands, region)


def _intervals_at(plane: Plane, t: float, tol: float = 5.0) -> list[tuple[float, float]]:
    """平面を位置 t で縦に切ったときに、面がある高さの範囲（つなげたもの）。"""
    lo, hi = _cross_section(plane.tz, t)
    out: list[list[float]] = []
    for a, b in sorted(zip(lo.tolist(), hi.tolist())):
        if out and a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def solid_bands(a: Piece, b: Piece, lo: float, hi: float, min_gap: float = 300.0, samples: int = 7) -> list[tuple[float, float]]:
    """向かい合う2枚の面（a・b）の、区間 lo〜hi（a の接線の位置）で「壁がある高さ」の範囲。

    何か所かで縦に切り、どこかで面がある高さを合わせる。壁の全長にわたって面のない高さ（min_gap 以上）が
    あれば、そこは開口（下がり壁と腰の部分の間など）なので、壁を上下に分ける。
    ドアの所のように一部だけ抜けている所は、ほかの位置で面があるので分けない。
    """
    covered: list[tuple[float, float]] = []
    for t in np.linspace(lo, hi, samples + 2)[1:-1]:
        p = a.plane.point(float(t))
        covered += _intervals_at(a.plane, float(t))
        covered += _intervals_at(b.plane, float(np.dot(p, b.plane.tangent)))
    if not covered:
        return [(min(a.bottom, b.bottom), max(a.top, b.top))]
    merged: list[list[float]] = []
    for z0, z1 in sorted(covered):
        if merged and z0 <= merged[-1][1] + min_gap:
            merged[-1][1] = max(merged[-1][1], z1)
        else:
            merged.append([z0, z1])
    return [(z0, z1) for z0, z1 in merged]


def _line_key(w: Wall) -> tuple[np.ndarray, float, np.ndarray]:
    d = w.direction
    if d[0] < -1e-9 or (abs(d[0]) <= 1e-9 and d[1] < 0):
        d = -d
    n = np.array([-d[1], d[0]])
    return d, float(np.dot(w.start, n)), n


def _same_height(a: Wall, b: Wall, tol: float = 150.0) -> bool:
    (b0, t0), (b1, t1) = z_range(a), z_range(b)
    same_top = (t0 == t1) or (math.isfinite(t0) and math.isfinite(t1) and abs(t0 - t1) <= tol)
    return abs(b0 - b1) <= tol and same_top


def merge_collinear(walls: list[Wall], region: Polygon | None, max_gap: float = 1200.0, fill_ratio: float = 0.8) -> list[Wall]:
    """同じ線の上にある、高さのそろった壁をつなぐ。間が内壁の範囲で埋まっていれば（ドアの位置など）間もつなぐ。
    高さの違う壁（下がり壁と腰の部分など）はつながない。"""
    groups: list[list[Wall]] = []
    for w in walls:
        d, off, _ = _line_key(w)
        for g in groups:
            gd, goff, _ = _line_key(g[0])
            if abs(np.dot(d, gd)) > 0.9999 and abs(off - goff) < 5.0 and _same_height(w, g[0]):
                g.append(w)
                break
        else:
            groups.append([w])
    out: list[Wall] = []
    for g in groups:
        d, off, n = _line_key(g[0])
        # 範囲（始まり・終わり）だけで並べる。★同じ範囲の壁が2つあると、壁そのものを比べてエラーになった（物件B、2026-11-03）
        spans = sorted(((min(np.dot(w.start, d), np.dot(w.end, d)), max(np.dot(w.start, d), np.dot(w.end, d)), w) for w in g),
                       key=lambda s: (s[0], s[1]))
        cur_lo, cur_hi, members = spans[0][0], spans[0][1], [spans[0][2]]
        for lo, hi, w in spans[1:]:
            join = lo <= cur_hi + 1.0
            if not join and region is not None and lo - cur_hi <= max_gap:
                gap_rect = Polygon([n * off + d * cur_hi + n * 40, n * off + d * lo + n * 40,
                                    n * off + d * lo - n * 40, n * off + d * cur_hi - n * 40])
                join = gap_rect.area > 0 and gap_rect.intersection(region).area / gap_rect.area >= fill_ratio
            if join:
                cur_hi = max(cur_hi, hi)
                members.append(w)
            else:
                out.append(_combine(members, n * off + d * cur_lo, n * off + d * cur_hi))
                cur_lo, cur_hi, members = lo, hi, [w]
        out.append(_combine(members, n * off + d * cur_lo, n * off + d * cur_hi))
    return out


def _combine(members: list[Wall], start: np.ndarray, end: np.ndarray) -> Wall:
    first = members[0]
    srcs = sorted({s for m in members for s in m.sources})
    assumptions = list(dict.fromkeys(a for m in members for a in m.assumptions))
    w = Wall(start=start, end=end, thickness=first.thickness, kind=first.kind,
             bottom=min(m.bottom for m in members), top=max((m.top for m in members if m.top is not None), default=None),
             sources=srcs, assumptions=assumptions)
    gaps = [getattr(m, "_gap", None) for m in members if getattr(m, "_gap", None) is not None]
    if gaps:
        w._gap = float(np.median(gaps))  # type: ignore[attr-defined]
    return w


JOIN_TOL_MM = 40.0     # L 字の角で、相手の壁の端の先をどこまで「ぶつかる」と見るか（厚みの半分＋これ）


def extend_to_junctions(walls: list[Wall], targets: list[Wall], zone: Polygon | None = None,
                        short_ext: float = 400.0, max_ext: float = 2000.0, zone_ratio: float = 0.9) -> None:
    """内壁の端を、ぶつかる壁の中心線まで伸ばす（壁どうしをすき間なくつなぐため）。

    short_ext までは無条件に伸ばす（部屋の角から交わる壁の中心線まで）。
    それより遠くても、伸ばす部分が zone（内壁の範囲・ドアの位置・外壁）の中を通るなら max_ext まで伸ばす
    （ドアの手前で途切れた壁を、ドアの向こうの壁までつなぐため）。
    ★11/5（デモの家の検品・作者「建物としておかしい所は直す」）：L 字の角では、両方の壁の端が相手の仕上げ面
    （中心線から 72.5mm）で止まっていて、相手の線を厚みの半分（65）延ばしても届かず、7.5mm のすき間が残っていた。
    → 相手の線を「厚みの半分＋JOIN_TOL_MM」まで延ばして当たりを探し、相手の端の先に当たったとき（L 字）は、
    相手の外の面まで伸ばす（中心線で止めると、角の外側に厚みの半分の四角い欠けが残るため）。
    """
    for w in walls:
        for which in ("start", "end"):
            p = getattr(w, which)
            d = -w.direction if which == "start" else w.direction
            ray = LineString([p, p + d * max_ext])
            hits = []
            for t in targets:
                if t is w:
                    continue
                td = t.direction
                ext = t.thickness / 2 + JOIN_TOL_MM
                seg = LineString([t.start - td * ext, t.end + td * ext])
                hit = ray.intersection(seg)
                if hit.is_empty or hit.geom_type != "Point":
                    continue
                q = np.array(hit.coords[0])
                dist = float(np.linalg.norm(q - p))
                if dist <= 1e-6:
                    continue
                s = float(np.dot(q - t.start, td))
                if s < -1e-6 or s > t.length + 1e-6:          # 相手の端の先（L 字の角）→ 相手の外の面まで
                    corner_only = s < -t.thickness / 2 or s > t.length + t.thickness / 2
                    q = q + d * (t.thickness / 2)
                    dist = float(np.linalg.norm(q - p))
                    if corner_only and dist > short_ext:
                        continue      # 前は当たらなかった所（L 字の角のためだけの当たり）は、遠くまで伸ばす決まりに使わない
                hits.append((dist, q))
            for dist, q in sorted(hits, key=lambda h: h[0]):
                if dist <= short_ext:
                    setattr(w, which, q)
                    break
                if zone is None:
                    break
                n = np.array([-d[1], d[0]]) * 20
                strip = Polygon([p + n, q + n, q - n, p - n])
                # 伸ばす先に平行な壁がすでにあるなら伸ばさない（少しずれた位置の壁を覆って消してしまうため）
                parallel = [t.rect() for t in targets if t is not w and abs(np.dot(t.direction, d)) > 0.999]
                if parallel and strip.intersection(unary_union(parallel)).area / strip.area > 0.3:
                    break
                if strip.intersection(zone).area / strip.area >= zone_ratio:
                    setattr(w, which, q)
                break


def _union_length(spans: list[tuple[float, float]]) -> float:
    total, cur = 0.0, None
    for lo, hi in sorted(spans):
        if cur is None or lo > cur[1]:
            if cur is not None:
                total += cur[1] - cur[0]
            cur = [lo, hi]
        else:
            cur[1] = max(cur[1], hi)
    return total + (cur[1] - cur[0] if cur is not None else 0.0)


def door_walls(doors: list[tuple[Polygon, float, float, str]], walls: list[Wall], house: Polygon, thickness: float,
               fl: float, ceiling: float, cover_ratio: float = 0.5, head_tol: float = 50.0, edge_mm: float = 250.0) -> list[Wall]:
    """床から天井までの戸の所に、戸の向きの壁がなければ、戸の枠に合わせて壁を足す（★11/4 デモの家・物件C〜M）。

    床から天井までの戸の上には壁の面がない。戸が並んだり、戸の間に T 字の壁がぶつかったりすると、向かい合う面の組が
    できず、壁ごと抜けていた（戸は「壁が見つからない」か、直角の壁に入っていた）。戸は壁の中にしか付かないので、
    戸があることを壁がある証拠にする（足した壁には door_wall の注記を付け、glb の例外として検品に回す）。
    doors：（平面の形, 下端, 上端, 出どころ）。戸の向き＝平面の形の長い方。戸の幅のうち、同じ向きで戸を通る壁に
    覆われている長さが cover_ratio 未満なら足す。線・厚み・高さは、戸を通る同じ向きの内壁のうち、いちばん近い壁に合わせる。
    外壁の戸（掃き出し窓・テラス戸。家の外形の線から edge_mm 以内）には足さない（この時点の外壁は開口の所で切れているため、
    壁がないように見える。★物件B で、足した壁に向かって内壁 W33 が 70mm 伸びた）。"""
    out: list[Wall] = []
    for fp, sill, head, src in doors:
        if sill > fl + 100 or head < ceiling - head_tol or fp.area <= 0 or not house.contains(fp.centroid):
            continue
        if house.exterior.distance(fp.centroid) < edge_mm:
            continue                                   # 外壁の戸
        c = np.array(fp.minimum_rotated_rectangle.exterior.coords)[:4]
        e1, e2 = c[1] - c[0], c[2] - c[1]
        d = e1 if np.linalg.norm(e1) >= np.linalg.norm(e2) else e2
        d = d / np.linalg.norm(d)
        if d[0] < -1e-9 or (abs(d[0]) <= 1e-9 and d[1] < 0):
            d = -d                                     # 向きをそろえる（壁の始まりが左・下になるように）
        n = np.array([-d[1], d[0]])
        pts = np.array(fp.exterior.coords)
        a0, a1 = float((pts @ d).min()), float((pts @ d).max())
        b0, b1 = float((pts @ n).min()), float((pts @ n).max())
        through = [w for w in walls + out if abs(float(np.dot(w.direction, d))) > 0.999
                   and b0 - 1.0 <= float(np.dot(w.start, n)) <= b1 + 1.0]

        def span(w: Wall) -> tuple[float, float]:
            return tuple(sorted((float(np.dot(w.start, d)), float(np.dot(w.end, d)))))  # type: ignore[return-value]
        covered = _union_length([(max(s0, a0), min(s1, a1)) for s0, s1 in map(span, through) if min(s1, a1) > max(s0, a0)])
        if covered >= cover_ratio * (a1 - a0):
            continue
        inner = [w for w in through if w.kind == "interior"]
        ref = min(inner, key=lambda w: max(span(w)[0] - a1, a0 - span(w)[1], 0.0)) if inner else None
        off = float(np.dot(ref.start, n)) if ref is not None else (b0 + b1) / 2
        w = Wall(start=n * off + d * a0, end=n * off + d * a1, thickness=ref.thickness if ref is not None else thickness,
                 kind="interior", bottom=ref.bottom if ref is not None else fl,
                 top=ref.top if ref is not None else ceiling, sources=[src],
                 assumptions=[f"door_wall: 床から天井までの戸（{src}）の所に壁の面がない（glb）。戸の枠に合わせて壁を足した"])
        if ref is not None and getattr(ref, "_gap", None) is not None:
            w._gap = ref._gap  # type: ignore[attr-defined]
        out.append(w)
    return out


def unresolved_regions(region: Polygon, walls: list[Wall], thickness: float, door_fps: list[Polygon] | None = None,
                       min_area_mm2: float = 60_000.0) -> list[Wall]:
    """向かい合う面から作れなかった内壁の範囲を、囲む長方形で返す。

    ★壁にはしない（物件Aの上がり框のように、壁がないのに隙間ができる所があるため。SPEC §2-5：推測で埋めない）。
    呼び出し側は「未解決の所」として一覧に出し、検品・質問に回す。
    ドアの位置は除く（★建具の箱は壁より厚く、内壁の範囲からはみ出すため。開口は次の段階でくり抜く）。
    """
    covered = unary_union([w.rect(getattr(w, "_gap", w.thickness) + 20) for w in walls]) if walls else Polygon()
    rest = region.difference(covered)
    if door_fps:
        rest = rest.difference(unary_union(door_fps).buffer(20, join_style="mitre"))
    geoms = getattr(rest, "geoms", [rest])
    out = []
    for g in geoms:
        if g.is_empty or g.area < min_area_mm2:
            continue
        rect = g.minimum_rotated_rectangle
        c = np.array(rect.exterior.coords)[:4]
        e1, e2 = c[1] - c[0], c[2] - c[1]
        if np.linalg.norm(e1) >= np.linalg.norm(e2):
            a, b, short = (c[0] + c[3]) / 2, (c[1] + c[2]) / 2, np.linalg.norm(e2)
        else:
            a, b, short = (c[0] + c[1]) / 2, (c[3] + c[2]) / 2, np.linalg.norm(e1)
        if short > 400:  # 壁にしては太すぎる（部屋の取りこぼしなど）。壁にはせず検品へ
            continue
        out.append(Wall(start=a, end=b, thickness=float(short), kind="interior", bottom=0.0,
                        assumptions=[f"unresolved: 内壁の範囲（幅 {short:.0f}mm）に向かい合う面が見つからない。壁があるか要確認"]))
    return out


def merge_exterior_pieces(walls: list[Wall], tol: float = 1.0) -> list[Wall]:
    """同じ外の面から作った外壁で、端どうしが接し、下端がそろったものを1枚にする（元の面のかけら _outer も合わせる）。
    上の階で、下屋の上から始まる壁を床まで下ろした後に使う（★物件B の2階：南の壁が4枚に分かれ、
    南西の角のかけらが捨てられて西の壁が角まで届かず、外形の輪が閉じなかった）。"""
    ext = [w for w in walls if w.kind == "exterior" and getattr(w, "_outer", None) is not None]
    others = [w for w in walls if w not in ext]
    groups: list[list[Wall]] = []
    for w in ext:
        for g in groups:
            if g[0]._outer.plane is w._outer.plane and abs(g[0].bottom - w.bottom) < 1.0:
                g.append(w)
                break
        else:
            groups.append([w])
    out = []
    for g in groups:
        g.sort(key=lambda w: w._outer.t0)
        run = [g[0]]
        for w in g[1:]:
            if w._outer.t0 <= max(x._outer.t1 for x in run) + tol:
                run.append(w)
            else:
                out.append(_merge_run(run))
                run = [w]
        out.append(_merge_run(run))
    return out + others


def _merge_run(run: list[Wall]) -> Wall:
    if len(run) == 1:
        return run[0]
    first = run[0]
    plane = first._outer.plane
    t0, t1 = min(w._outer.t0 for w in run), max(w._outer.t1 for w in run)
    d = first.direction
    s = float(np.dot(first.start, d))
    lo = min(min(np.dot(w.start, d), np.dot(w.end, d)) for w in run)
    hi = max(max(np.dot(w.start, d), np.dot(w.end, d)) for w in run)
    w = Wall(start=first.start + d * (lo - s), end=first.start + d * (hi - s), thickness=first.thickness, kind="exterior",
             bottom=min(x.bottom for x in run), top=None, sources=sorted({s_ for x in run for s_ in x.sources}),
             top_glb=max((x.top_glb for x in run if x.top_glb is not None), default=None),
             assumptions=list(dict.fromkeys(a for x in run for a in x.assumptions)))
    w._outer = Piece(plane=plane, t0=t0, t1=t1, bottom=min(x._outer.bottom for x in run),  # type: ignore[attr-defined]
                     top=max(x._outer.top for x in run))
    return w


def clip_faces_z(faces: np.ndarray, z0: float | None, z1: float | None, min_area: float = 1.0) -> np.ndarray:
    """三角形（N,3,3）を高さ z0〜z1 の帯で切り、帯の中の部分を三角形に分け直して返す。
    z0・z1 がどちらも None なら、入力をそのまま返す（平屋は何も変わらない）。
    ★2階建て：外壁の部材が1つで、1階と2階の面が同じ平面につながっている（物件B）ため、階ごとに切って使う。"""
    if z0 is None and z1 is None:
        return faces
    lo = -np.inf if z0 is None else float(z0)
    hi = np.inf if z1 is None else float(z1)
    if len(faces) == 0:
        return faces
    zmin, zmax = faces[:, :, 2].min(axis=1), faces[:, :, 2].max(axis=1)
    inside = (zmin >= lo) & (zmax <= hi)
    out = [faces[inside]]
    for tri in faces[~inside & (zmax > lo) & (zmin < hi)]:
        poly = [p for p in tri]
        for bound, keep in ((lo, lambda z, b: z >= b), (hi, lambda z, b: z <= b)):
            if not np.isfinite(bound) or not poly:
                continue
            nxt = []
            for i, a in enumerate(poly):
                b = poly[(i + 1) % len(poly)]
                ina, inb = keep(a[2], bound), keep(b[2], bound)
                if ina:
                    nxt.append(a)
                if ina != inb:
                    t = (bound - a[2]) / (b[2] - a[2])
                    nxt.append(a + (b - a) * t)
            poly = nxt
        for k in range(1, len(poly) - 1):
            t = np.array([poly[0], poly[k], poly[k + 1]])
            if 0.5 * np.linalg.norm(np.cross(t[1] - t[0], t[2] - t[0])) >= min_area:
                out.append(t[None])
    return np.concatenate(out) if out else faces[:0]


def envelope_standalone(walls: list[Wall], inside: Polygon, thickness: float, gap: float = 150.0,
                        samples: int = 8) -> list[Wall]:
    """単独の壁のうち、片側が家の中・反対側が外のものを、外壁の一部とみなして外壁の厚みにする。
    外の面の位置はそのままにして、内側の面を寄せる（外壁の表面に段差を作らない。中心線は外側へずれる）。
    両側とも外（袖壁・ポーチの壁）や、両側とも家の中のものは、glb の厚みのまま。
    家の中かどうかは物件ごとに glb の形から決める（会社ごとの数値に頼らない）。
    ★物件A：W12（ポーチと窓際の腰掛けの間）・W13 は 180→162、W11・W14（両側が外）は 180 のまま。
      作者の答え（2026-11-03）と4枚とも一致した。"""
    changed = []
    need = int(math.ceil(samples * 0.75))
    for w in walls:
        if w.kind != "standalone" or abs(w.thickness - thickness) < 0.5:
            continue
        d = w.direction
        n = np.array([-d[1], d[0]])
        off = w.thickness / 2 + gap
        pts = [w.start + (w.end - w.start) * t for t in np.linspace(0.15, 0.85, samples)]
        left = sum(inside.contains(Point(*(p + n * off))) for p in pts)
        right = sum(inside.contains(Point(*(p - n * off))) for p in pts)
        if left >= need and right == 0:
            to_inside = n
        elif right >= need and left == 0:
            to_inside = -n
        else:
            continue
        old = w.thickness
        shift = -to_inside * (old - thickness) / 2          # 外の面＝中心線 − 厚みの半分（外向き）を変えない
        w.start, w.end, w.thickness = w.start + shift, w.end + shift, thickness
        w.assumptions.append(f"片側が家の中・反対側が外なので外壁の一部とみなし、外壁の厚み {thickness:g}mm にした"
                             f"（glb の単独の壁は {old:g}mm）。外の面の位置はそのまま")
        changed.append(w)
    return changed


def footprint(walls: list[Wall]) -> Polygon:
    return unary_union([w.rect() for w in walls])


def bbox_polygon(points: np.ndarray) -> Polygon:
    """点の集まりを囲む最小の長方形（斜めでもよい）。"""
    if len(points) < 3:
        return box(*points.min(axis=0), *points.max(axis=0))
    return MultiPoint([tuple(p) for p in points]).minimum_rotated_rectangle
