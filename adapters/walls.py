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
        spans = sorted((min(np.dot(w.start, d), np.dot(w.end, d)), max(np.dot(w.start, d), np.dot(w.end, d)), w) for w in g)
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


def extend_to_junctions(walls: list[Wall], targets: list[Wall], zone: Polygon | None = None,
                        short_ext: float = 400.0, max_ext: float = 2000.0, zone_ratio: float = 0.9) -> None:
    """内壁の端を、ぶつかる壁の中心線まで伸ばす（壁どうしをすき間なくつなぐため）。

    short_ext までは無条件に伸ばす（部屋の角から交わる壁の中心線まで）。
    それより遠くても、伸ばす部分が zone（内壁の範囲・ドアの位置・外壁）の中を通るなら max_ext まで伸ばす
    （ドアの手前で途切れた壁を、ドアの向こうの壁までつなぐため）。
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
                ext = t.thickness / 2
                seg = LineString([t.start - td * ext, t.end + td * ext])
                hit = ray.intersection(seg)
                if hit.is_empty or hit.geom_type != "Point":
                    continue
                dist = float(np.linalg.norm(np.array(hit.coords[0]) - p))
                if dist > 1e-6:
                    hits.append((dist, np.array(hit.coords[0])))
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


def footprint(walls: list[Wall]) -> Polygon:
    return unary_union([w.rect() for w in walls])


def bbox_polygon(points: np.ndarray) -> Polygon:
    """点の集まりを囲む最小の長方形（斜めでもよい）。"""
    if len(points) < 3:
        return box(*points.min(axis=0), *points.max(axis=0))
    return MultiPoint([tuple(p) for p in points]).minimum_rotated_rectangle
