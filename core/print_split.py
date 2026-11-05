"""屋根の印刷の分け方と向き（★2026-11-04 作者「入らない屋根は屋根の線で分けて、のりで付ける。割れ目も見えない」
「屋根の形ごとに、いちばんよい印刷の仕方にすればいい」）。

- 屋根のオブジェクト（屋根板・軒天・天井より上の外壁・ずれ止めの三角・ポーチの天井の板など）を、
  面ごとの受け持ちの範囲（core/roof_geom.py の plane_domains。棟・隅棟・谷の線で分かれる）で切る。
  片流れ（面1つ）は切らない。切り口は2つの面のちょうど真ん中の縦の面になり、のりで付けるとぴったり合う。
- 切った1つずつを、その面の上の面が床にぴったり付く向き（上の面を下）に回し、造形範囲に入る角度を探す。
  上の面（いちばん目立つ面）にサポートが付かない。部品に付いた壁や三角は、屋根の勾配だけ傾いて上に立つ。
- 1つの面が入らないときは、その面を軒と直角に半分に切る（最後の手段。注意を出す）。
- 切り口の近くは、ざらざら（ファジースキン）を外す（のり代がなめらかになるように）。
- 画面の 3D（組み上がり）・部品の番号・building.json は変えない。書き出し（3MF・STL）のときだけ分ける。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString, MultiPoint, Polygon
from shapely.affinity import rotate as srotate

from core.roof_geom import plane_domains, roof_planes

BED_MM = 256.0
SPECK_MM3 = 50.0 ** 3 * 0.001        # 実寸で 0.125 L（模型で約 1mm³）。これより小さいかけらは捨てる
ISLAND_SHARE = 0.02                  # 1つの部品の体積のこれ未満の離れた塊は、隣の部品へ移す
SUPPORT_COS = math.cos(math.radians(30.0))   # 下向きで、水平から 30° より寝ている面はサポートが要る（Bambu Studio の既定）
SIDE_JA = {"S": "南", "N": "北", "E": "東", "W": "西"}


@dataclass
class PrintFace:
    name: str                        # 「南の面」など（3MF のオブジェクトの名前に使う）
    region: Polygon | None           # 平面の受け持ちの範囲（実寸 mm）。None は全部
    top_plane: dict                  # その面の上の面の式 z = a·x + b·y + c


@dataclass
class Piece:
    name: str
    solids: dict                     # 部品の key → 回す前の立体（実寸 mm）
    top_plane: dict
    cuts: list = field(default_factory=list)   # 切り口の線（平面、実寸）。ざらざらを外す帯に使う
    warnings: list = field(default_factory=list)


def low_side(plane: dict) -> str | None:
    """面の低い側（下り坂の向き）。平らなら None。"""
    a, b = float(plane["a"]), float(plane["b"])
    if abs(a) < 1e-9 and abs(b) < 1e-9:
        return None
    dx, dy = -a, -b
    return ("E" if dx > 0 else "W") if abs(dx) > abs(dy) else ("N" if dy > 0 else "S")


def roof_print_faces(roof: dict, clip: Polygon | None = None, prefix: str = "") -> list[PrintFace]:
    """屋根を印刷の面に分ける。片流れ（面1つ）は1つ。clip は、その屋根が受け持つ平面の範囲（2階建ての下屋など）。"""
    planes = roof_planes(roof)
    if not planes:
        return [PrintFace(name=f"{prefix}屋根".strip(), region=clip, top_plane=roof["top_plane"])]
    domains = plane_domains([Polygon(p["footprint"]) for p in planes])
    names: dict[str, int] = {}
    out = []
    for dom, p in zip(domains, planes):
        region = dom if clip is None else dom.intersection(clip)
        if region.is_empty or region.area < 1.0:
            continue
        side = low_side(p["top_plane"])
        base = f"{SIDE_JA[side]}の面" if side else "平らな面"
        names[base] = names.get(base, 0) + 1
        out.append(PrintFace(name=f"{prefix}{base}" + (f"{names[base]}" if names[base] > 1 else ""), region=region,
                             top_plane=p["top_plane"]))
    return out


# ---------------------------------------------------------------- 切る

def _prism(poly, z0, z1):
    from core.builder import prism
    return prism(poly, z0, z1)


def _zrange(solids: dict) -> tuple[float, float]:
    zs = [m.bounding_box() for m in solids.values() if not m.is_empty()]
    return (min(b[2] for b in zs) - 10.0, max(b[5] for b in zs) + 10.0) if zs else (0.0, 1.0)


def split_by_faces(solids: dict, faces: list[PrintFace]) -> list[Piece]:
    """立体（部品の key → 立体）を面ごとに切る。体積の合計は変えない（どの範囲にも入らない所は、いちばん近い面へ）。"""
    from core.builder import drop_specks, union_all
    if len(faces) == 1 and faces[0].region is None:
        return [Piece(name=faces[0].name, solids=dict(solids), top_plane=faces[0].top_plane)]
    z0, z1 = _zrange(solids)
    pieces = []
    for f in faces:
        reg = f.region
        pieces.append(Piece(name=f.name, top_plane=f.top_plane, cuts=[reg.boundary] if reg is not None else [],
                            solids={k: (m ^ _prism(reg, z0, z1)) if not m.is_empty() else m for k, m in solids.items()}))
    for k, m in solids.items():                       # 範囲の外に残った所（ふつうは無い）
        if m.is_empty():
            continue
        left = m - union_all([p.solids[k] for p in pieces if not p.solids[k].is_empty()])
        for body in (left.decompose() if not left.is_empty() else []):
            if body.volume() < SPECK_MM3:
                continue
            x0, y0, _, x1, y1, _ = body.bounding_box()
            c = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]).centroid
            best = min((p for p in pieces if faces), key=lambda p: next(f.region for f in faces if f.name == p.name).distance(c))
            best.solids[k] = union_all([best.solids[k], body])
    for p in pieces:
        p.solids = {k: drop_specks(m) if not m.is_empty() else m for k, m in p.solids.items()}
    pieces = [p for p in pieces if any(not m.is_empty() for m in p.solids.values())]
    _settle_islands(pieces)
    return pieces


def _box_overlap(a, b, pad: float = 5.0) -> float:
    ax0, ay0, az0, ax1, ay1, az1 = a
    bx0, by0, bz0, bx1, by1, bz1 = b
    d = [min(ax1, bx1) - max(ax0, bx0) + pad, min(ay1, by1) - max(ay0, by0) + pad, min(az1, bz1) - max(az0, bz0) + pad]
    return d[0] * d[1] * d[2] if all(v > 0 for v in d) else 0.0


def _settle_islands(pieces: list[Piece]) -> None:
    """切ったせいで部品の中で離れた小さな塊（棟をまたぐ板の端など）は、接していた隣の部品へ移す。"""
    from core.builder import union_all
    if len(pieces) < 2:
        return
    for i, p in enumerate(pieces):
        allv = union_all([m for m in p.solids.values() if not m.is_empty()])
        bodies = allv.decompose() if not allv.is_empty() else []
        if len(bodies) < 2:
            continue
        total = sum(b.volume() for b in bodies)
        for body in bodies:
            if body.volume() >= ISLAND_SHARE * total:
                continue
            bb = body.bounding_box()
            others = [(j, q) for j, q in enumerate(pieces) if j != i]
            j, q = max(others, key=lambda jq: max((_box_overlap(bb, m.bounding_box()) for m in jq[1].solids.values()
                                                   if not m.is_empty()), default=0.0))
            for k, m in list(p.solids.items()):
                part = m ^ body
                if part.is_empty() or part.volume() < 1e-6:
                    continue
                p.solids[k] = m - part
                q.solids[k] = union_all([q.solids.get(k), part]) if q.solids.get(k) is not None and not q.solids[k].is_empty() else part


def smooth_cuts(piece: Piece, fuzzy_keys: list[str], owner_key: str, band: float) -> None:
    """切り口の近く（band、実寸）を、ざらざらの部品からざらざらでない部品（屋根の部品）へ移す。"""
    from core.builder import union_all
    if not piece.cuts or band <= 0:
        return
    z0, z1 = _zrange(piece.solids)
    strip = None
    for c in piece.cuts:
        s = c.buffer(band, cap_style="flat")
        strip = s if strip is None else strip.union(s)
    if strip is None or strip.is_empty:
        return
    keep = _prism(strip, z0, z1)
    for k in fuzzy_keys:
        m = piece.solids.get(k)
        if m is None or m.is_empty():
            continue
        moved = m ^ keep
        if moved.is_empty() or moved.volume() < 1e-6:
            continue
        piece.solids[k] = m - moved
        piece.solids[owner_key] = union_all([piece.solids[owner_key], moved]) if not piece.solids[owner_key].is_empty() else moved


# ---------------------------------------------------------------- 回す・置く

def face_down_rotation(top_plane: dict) -> np.ndarray:
    """面の上向きの法線を真下（-Z）へ向ける回転（3×3、向きは保つ）。平らな面は X 軸まわりに 180°。"""
    n = np.array([-float(top_plane["a"]), -float(top_plane["b"]), 1.0])
    n /= np.linalg.norm(n)
    t = np.array([0.0, 0.0, -1.0])
    v = np.cross(n, t)
    s, c = float(np.linalg.norm(v)), float(n @ t)
    if s < 1e-9:
        return np.diag([1.0, -1.0, -1.0]) if c < 0 else np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / s ** 2)


def best_yaw(xy: np.ndarray, limit: float) -> tuple[float, float]:
    """床の上で回す角度（度）と、そのときの長い辺（mm）。0° か 90° で入れば 0°/90°、入らなければ長い辺がいちばん短い角度。"""
    hull = MultiPoint([tuple(p) for p in xy]).convex_hull

    def longest(deg):
        x0, y0, x1, y1 = srotate(hull, deg, origin=(0, 0)).bounds
        return max(x1 - x0, y1 - y0), (x1 - x0, y1 - y0)

    for deg in (0.0, 90.0):
        L, (w, h) = longest(deg)
        if w <= limit and h <= limit:
            return deg, L
    best = min((longest(d / 2)[0], d / 2) for d in range(0, 360))
    return best[1], best[0]


def orient(meshes: dict, top_plane: dict, limit: float) -> tuple[dict, float, float, bool]:
    """meshes：key → (頂点, 三角形)（模型寸法）。上の面を床に付けて回し、造形範囲に入る角度で回す。
    （回した meshes, 角度, 長い辺, 入ったか）。回し方は transform で同じように当てられる（meshes["__transform__"] には入れない）。"""
    R = face_down_rotation(top_plane)
    allv = np.vstack([v for v, _ in meshes.values() if len(v)])
    rotated = {k: (v @ R.T, f) for k, (v, f) in meshes.items()}
    allr = allv @ R.T
    deg, L = best_yaw(allr[:, :2], limit)
    th = math.radians(deg)
    Rz = np.array([[math.cos(th), -math.sin(th), 0], [math.sin(th), math.cos(th), 0], [0, 0, 1.0]])
    zmin = float((allr @ Rz.T)[:, 2].min())
    out = {k: ((v @ Rz.T) - np.array([0, 0, zmin]), f) for k, (v, f) in rotated.items()}
    allo = np.vstack([v for v, _ in out.values() if len(v)])
    ext = allo.max(axis=0) - allo.min(axis=0)
    orient.last = (R, Rz, zmin)                       # 同じ回し方を、部品を合わせた形（サポートの量の計算）に当てるため
    return out, deg, L, bool(ext[0] <= limit and ext[1] <= limit and ext[2] <= BED_MM)


def apply_last(v: np.ndarray) -> np.ndarray:
    """直前の orient と同じ回し方を頂点（模型寸法）に当てる。"""
    R, Rz, zmin = orient.last
    return (v @ R.T) @ Rz.T - np.array([0, 0, zmin])


def _verts_xy(solids: dict) -> np.ndarray:
    pts = [np.asarray(m.to_mesh().vert_properties, dtype=np.float64)[:, :2] for m in solids.values() if not m.is_empty()]
    return np.vstack(pts) if pts else np.zeros((0, 2))


def split_across_eave(piece: Piece) -> list[Piece]:
    """入らない面を、縦の面で半分にする（最後の手段）。切り口の線も記録する。
    ★11/5：前は必ず「軒と直角」に切っていたので、長い方向に下る屋根（物件G）は長い方向に沿って2回切られ、
    細長い4枚（模型で約 37×287mm）になっていた → 軒の向き・下り坂の向きのうち、長い方を半分にする。
    切り口の線（ざらざらを外す帯）も、本当の切り口に沿うように直した（前は傾いた面で 90° ずれていた）。"""
    a, b = float(piece.top_plane["a"]), float(piece.top_plane["b"])
    xy = _verts_xy(piece.solids)
    if not len(xy):
        return [piece]
    if abs(a) < 1e-9 and abs(b) < 1e-9:
        cands = [np.array([1.0, 0.0]), np.array([0.0, 1.0])]
    else:
        e = np.array([-b, a]) / math.hypot(a, b)        # 軒の向き（水平で、下り坂と直角）
        g = np.array([a, b]) / math.hypot(a, b)         # 下り坂（上り坂）の向き
        cands = [e, g]
    ext = lambda d: float((xy @ d).max() - (xy @ d).min())
    nd = max(cands, key=ext)                            # 切る面の法線＝長い方の向き（そこを半分にする）
    lo_, hi_ = float((xy @ nd).min()), float((xy @ nd).max())
    offset = (lo_ + hi_) / 2
    normal = (float(nd[0]), float(nd[1]), 0.0)
    d = np.array([-nd[1], nd[0]])                       # 切り口の線の向き（法線と直角）
    c = nd * offset
    L = 1e5
    line = LineString([tuple(c - d * L), tuple(c + d * L)])
    along_eave = abs(a) + abs(b) > 1e-9 and abs(float(nd @ np.array([-b, a]) / math.hypot(a, b))) > 0.99
    msg = "造形範囲に入らないので、軒と直角に半分に切った" if along_eave else "造形範囲に入らないので、長い方を半分に切った"
    halves = []
    for sign in (1, -1):
        sub = {}
        for k, m in piece.solids.items():
            if m.is_empty():
                sub[k] = m
                continue
            pos, neg = m.split_by_plane(normal, offset)
            sub[k] = pos if sign == 1 else neg
        halves.append(Piece(name=piece.name + ("（右）" if sign == 1 else "（左）"), solids=sub, top_plane=piece.top_plane,
                            cuts=piece.cuts + [line],
                            warnings=piece.warnings + ([] if msg in piece.warnings else [msg])))   # 2回切っても注意は1回
    return [h for h in halves if any(not m.is_empty() for m in h.solids.values())]


def split_by_planes(solids: dict, u, cuts: list[float], names: list[str], top_plane: dict | None = None,
                    base_cuts: list | None = None, warnings: list[str] | None = None, settle: bool = True) -> list[Piece]:
    """立体（部品の key → 立体）を、平面の向き u に直角な縦の面（u·p = cuts）で分ける（本体の分け目。core/body_split.py）。
    体積は変えない。settle なら、離れた小さな塊を隣へ移す（屋根。本体は、もとから離れている塊（離れた基礎など）を
    動かすと部品が家の端から端まで広がるので移さない。★11/5 物件G）。切り口の線を記録する（ざらざらを外す帯に使う）。"""
    from core.builder import drop_specks
    u = np.asarray(u, float)
    v = np.array([-u[1], u[0]])
    u3, nu3 = (float(u[0]), float(u[1]), 0.0), (-float(u[0]), -float(u[1]), 0.0)
    ts = sorted(float(t) for t in cuts)
    L = 1e5
    lines = [LineString([tuple(u * t - v * L), tuple(u * t + v * L)]) for t in ts]
    edges = [None] + ts + [None]
    pieces = []
    for i in range(len(ts) + 1):
        a, z = edges[i], edges[i + 1]
        sub = {}
        for k, m in solids.items():
            x = m
            if not x.is_empty() and a is not None:
                x = x.trim_by_plane(u3, a)
            if not x.is_empty() and z is not None:
                x = x.trim_by_plane(nu3, -z)
            sub[k] = drop_specks(x) if not x.is_empty() else x
        here = [lines[j] for j in (i - 1, i) if 0 <= j < len(lines)]
        pieces.append(Piece(name=names[i] if i < len(names) else f"{i + 1}", solids=sub, top_plane=top_plane or {},
                            cuts=list(base_cuts or []) + here, warnings=list(warnings or [])))
    pieces = [p for p in pieces if any(not m.is_empty() for m in p.solids.values())]
    if settle:
        _settle_islands(pieces)
    return pieces


def support_area(verts: np.ndarray, tris: np.ndarray, floor_tol: float = 0.2) -> float:
    """サポートが要りそうな面の面積（模型 mm²）：下向きで水平から 30° より寝ていて、床に付いていない面。"""
    if not len(tris):
        return 0.0
    f = verts[tris]
    n = np.cross(f[:, 1] - f[:, 0], f[:, 2] - f[:, 0])
    area = np.linalg.norm(n, axis=1) / 2
    with np.errstate(invalid="ignore", divide="ignore"):
        nz = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-12)
    down = (nz < -SUPPORT_COS) & (f[:, :, 2].min(axis=1) > floor_tol)
    return float(area[down].sum())
