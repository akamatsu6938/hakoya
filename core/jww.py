"""図面（.jww）の読み取り（SPEC §4-2。ライブラリ ezjww 0.7.6）。

★守ること（SPEC §2）：
- 元のファイルは読むだけ（ezjww は読むだけの道具。写しも作らない）。
- **表題欄（用紙の下の帯）と、縮尺 1 の層（枠・表題欄）の文字は読み捨てる**（施主名・住所・設計事務所の情報が入っているため）。
  header の memo も読まない。出してはいけない言葉（ng_words）を含む文字も捨てる。
- 用紙の外にある文字も捨てる（見えない覚え書きのことがあるため）。

読み方（SPEC §4-2 の★）：
- ezjww のネイティブ記録（jww_document）を使う（DXF に変える道は、ペン・線種・円弧の角度を誤る）。
- 実寸 mm ＝ 用紙 mm × その要素のレイヤグループの縮尺。原点は用紙の中心、+Y が上。
- 補助線（pen_style か pen_color が 9）と、非表示のレイヤ（グループかレイヤの状態が 0）は使わない。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PAPER_MM = {0: (1189.0, 841.0), 1: (841.0, 594.0), 2: (594.0, 420.0), 3: (420.0, 297.0), 4: (297.0, 210.0)}   # A0〜A4（横）
AUX = 9                       # 補助線のペン（印刷されない）
TITLE_BAND_SEARCH_MM = 45.0   # 表題欄の上の線を探す範囲（用紙の下端から）
TITLE_LINE_SHARE = 0.8        # 表題欄の上の線：用紙の幅のこの割合より長い水平線
TITLE_FALLBACK_MM = 30.0      # 線が見つからないときの表題欄の高さ
DIM_RE = re.compile(r"^[0-9][0-9,]*(\.[0-9]+)?$")


@dataclass
class Drawing:
    kind: str                                  # plan / plan_2f / elevation / section / site
    paper_mm: tuple[float, float]
    title_band_top: float                      # 表題欄の上端（用紙 mm。これより下は捨てた）
    main_group: int                            # いちばん線の多いレイヤグループ（図面の本体）
    scale: float                               # 本体の縮尺（50 なら 1/50）
    lines: np.ndarray                          # (N, 4) 実寸 x0, y0, x1, y1（本体のグループだけ）
    texts: list[dict] = field(default_factory=list)    # {"id", "text", "x", "y", "h"}（実寸。本体のグループ、表題欄の外）
    dims: list[dict] = field(default_factory=list)     # {"id", "value", "text", "x0", "y0", "x1", "y1"}（実寸）
    dropped: dict = field(default_factory=dict)        # 捨てた数（表題欄・縮尺1・用紙の外・ng_words・非表示・補助線）


def _visible(e: dict, groups: list[dict]) -> bool:
    b = e.get("base") or {}
    g = groups[b.get("layer_group", 0)]
    if g.get("state", 2) == 0:
        return False
    layers = g.get("layers") or []
    li = b.get("layer", 0)
    return not (li < len(layers) and layers[li].get("state", 2) == 0)


def _aux(e: dict) -> bool:
    b = e.get("base") or {}
    return b.get("pen_style") == AUX or b.get("pen_color") == AUX


def _title_band_top(entities: list[dict], paper: tuple[float, float]) -> float:
    """表題欄の上端（用紙 mm）：下端から TITLE_BAND_SEARCH_MM 以内で、用紙の幅の 8割より長い水平線のうち一番上。"""
    w, h = paper
    bottom = -h / 2
    best = None
    for e in entities:
        if e.get("type") != "LINE":
            continue
        y0, y1 = e["start_y"], e["end_y"]
        if abs(y0 - y1) > 0.05 or abs(e["end_x"] - e["start_x"]) < w * TITLE_LINE_SHARE:
            continue
        if bottom < y0 < bottom + TITLE_BAND_SEARCH_MM and (best is None or y0 > best):
            best = y0
    return (best if best is not None else bottom + TITLE_FALLBACK_MM) + 0.5


def read_drawing(path: Path, kind: str, ng_words: list[str] | None = None) -> Drawing:
    """図面を読む。表題欄・縮尺1の層・用紙の外・ng_words の文字は捨てる（数だけ dropped に残す）。"""
    import ezjww
    from core.privacy import find_ng
    if ng_words is None:
        from core.privacy import ng_words as _ng
        ng_words = _ng()
    doc = ezjww.readfile(str(path)).jww_document
    header = doc["header"]
    groups = header["layer_groups"]
    paper = PAPER_MM.get(int(header.get("paper_size", 3)), PAPER_MM[3])
    ents = doc["entities"]
    band = _title_band_top(ents, paper)
    w, h = paper
    dropped = {"title_band": 0, "paper_scale_group": 0, "off_paper": 0, "ng_words": 0, "hidden": 0, "aux": 0}

    def on_paper(x, y):
        return -w / 2 - 1 <= x <= w / 2 + 1 and band <= y <= h / 2 + 1

    # 本体のグループ：見えている線の長さがいちばん長いグループ（縮尺 1 の層は除く）
    length: dict[int, float] = {}
    for e in ents:
        if e.get("type") == "LINE" and _visible(e, groups) and not _aux(e):
            g = e["base"]["layer_group"]
            if float(groups[g].get("scale", 1.0)) != 1.0:
                length[g] = length.get(g, 0.0) + float(np.hypot(e["end_x"] - e["start_x"], e["end_y"] - e["start_y"]))
    main = max(length, key=length.get) if length else 0
    scale = float(groups[main].get("scale", 1.0))

    lines, texts, dims = [], [], []
    for e in ents:
        t = e.get("type")
        g = (e.get("base") or {}).get("layer_group", 0)
        if not _visible(e, groups):
            dropped["hidden"] += 1
            continue
        if t == "TEXT":
            x, y = float(e["start_x"]), float(e["start_y"])
            content = str(e.get("content", ""))
            if float(groups[g].get("scale", 1.0)) == 1.0:
                dropped["paper_scale_group"] += 1          # 枠・表題欄の層：読み捨てる
            elif y < band:
                dropped["title_band"] += 1                 # 表題欄の帯：読み捨てる
            elif not on_paper(x, y):
                dropped["off_paper"] += 1
            elif find_ng(content, ng_words) is not None:     # 全角・半角と空白の違いも同じに見る
                dropped["ng_words"] += 1
            elif g == main:
                ex, ey = float(e.get("end_x", x)), float(e.get("end_y", y))
                texts.append({"id": f"T{len(texts) + 1}", "text": content.strip(), "x": x * scale, "y": y * scale,
                              "cx": (x + ex) / 2 * scale, "cy": (y + ey) / 2 * scale, "h": float(e.get("size_y", 3.0)) * scale})
            continue
        if _aux(e):
            dropped["aux"] += 1
            continue
        if t == "LINE" and g == main:
            ys = (e["start_y"], e["end_y"])
            if min(ys) < band:
                dropped["title_band"] += 1
                continue
            lines.append([e["start_x"] * scale, e["start_y"] * scale, e["end_x"] * scale, e["end_y"] * scale])
        elif t == "DIMENSION" and g == main:
            ln, tx = e.get("line") or {}, e.get("text") or {}
            val = str(tx.get("content", "")).strip()
            if not DIM_RE.match(val) or min(ln.get("start_y", 0), ln.get("end_y", 0)) < band:
                continue
            dims.append({"id": f"D{len(dims) + 1}", "value": float(val.replace(",", "")), "text": val,
                         "x0": ln["start_x"] * scale, "y0": ln["start_y"] * scale,
                         "x1": ln["end_x"] * scale, "y1": ln["end_y"] * scale})
    return Drawing(kind=kind, paper_mm=paper, title_band_top=band, main_group=main, scale=scale,
                   lines=np.array(lines, float).reshape(-1, 4), texts=texts, dims=dims, dropped=dropped)


_PDF_CACHE: dict = {}


def read_property(alias: str, kind: str = "plan") -> Drawing | None:
    """物件の図面を読む（data/local_paths.yaml の jww か PDF。なければ None。高性能読み込みが切でも None）。
    PDF の縮尺は、控え（平面図の位置合わせで決めたもの）→ 寸法の数字 → 平面図と同じ、の順に決める。"""
    from core.paths import property_files
    files = property_files(alias)
    p = files.jww.get(kind)
    if p is None or not p.exists():
        return None
    if p.suffix.lower() != ".pdf":
        return read_drawing(p, kind)
    from core import reading
    from core.pdf_drawing import read_pdf, rescale
    st = p.stat()
    key = (str(p), st.st_size, int(st.st_mtime), kind)
    if key not in _PDF_CACHE:                          # 1枚で数秒かかるので、同じファイルは読み直さない
        _PDF_CACHE[key] = read_pdf(p, kind)
    d = _PDF_CACHE[key]
    saved = reading.saved_scale(alias, kind, p)
    if saved and abs(saved - d.scale) > 1e-6:
        return rescale(d, saved)
    if getattr(d, "scale_source", "") == "assumed" and kind != "plan":
        plan = files.jww.get("plan")
        ps = reading.saved_scale(alias, "plan", plan) if plan is not None and plan.exists() else None
        if ps:
            return rescale(d, ps)                      # 平面図と同じ縮尺とみなす（同じ組の図面）
    return d


# ---------------------------------------------------------------- 平面図と模型の位置合わせ

ALIGN_CELL_MM = 20.0          # 模型の壁の面を描くマス目の大きさ
ALIGN_NEAR_CELLS = 2          # 壁の面からこのマス数以内なら「乗っている」（40mm）
ALIGN_MIN_SCORE = 0.15        # 図面の線の点のうち、壁の面に乗る割合の下限（家具・寸法線などもあるので低め）
ALIGN_MIN_MARGIN = 1.3        # 2番目に良い向きより、この倍以上よいこと


def _rot(theta: int, pts: np.ndarray) -> np.ndarray:
    x, y = pts[:, 0], pts[:, 1]
    return {0: np.column_stack([x, y]), 90: np.column_stack([-y, x]),
            180: np.column_stack([-x, -y]), 270: np.column_stack([y, -x])}[theta]


def _sample(lines: np.ndarray, step: float) -> np.ndarray:
    """線分を step ごとの点にする。"""
    out = []
    for x0, y0, x1, y1 in lines:
        n = max(int(np.hypot(x1 - x0, y1 - y0) / step), 1)
        t = np.linspace(0.0, 1.0, n + 1)
        out.append(np.column_stack([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t]))
    return np.vstack(out) if out else np.zeros((0, 2))


def _wall_faces(b: dict, level: str | None) -> np.ndarray:
    from core.builder import wall_rect
    segs = []
    for w in b.get("walls", []):
        if level and w.get("level") not in (None, level):
            continue
        ring = list(wall_rect(w).exterior.coords)
        segs += [[*p, *q] for p, q in zip(ring[:-1], ring[1:])]
    return np.array(segs, float).reshape(-1, 4)


class _Raster:
    def __init__(self, segs: np.ndarray, margin: float = 3000.0, near_cells: int | None = None):
        pts = _sample(segs, ALIGN_CELL_MM / 2)
        self.x0, self.y0 = pts.min(axis=0) - margin
        x1, y1 = pts.max(axis=0) + margin
        self.nx, self.ny = int((x1 - self.x0) / ALIGN_CELL_MM) + 1, int((y1 - self.y0) / ALIGN_CELL_MM) + 1
        grid = np.zeros((self.ny, self.nx), bool)
        ix, iy = self._idx(pts)
        grid[iy, ix] = True
        k = ALIGN_NEAR_CELLS if near_cells is None else near_cells
        near = grid.copy()
        for dx in range(-k, k + 1):
            for dy in range(-k, k + 1):
                near |= np.roll(np.roll(grid, dy, axis=0), dx, axis=1)
        self.near = near

    def _idx(self, pts: np.ndarray):
        ix = np.clip(((pts[:, 0] - self.x0) / ALIGN_CELL_MM).astype(int), 0, self.nx - 1)
        iy = np.clip(((pts[:, 1] - self.y0) / ALIGN_CELL_MM).astype(int), 0, self.ny - 1)
        return ix, iy

    def score(self, pts: np.ndarray) -> float:
        inside = (pts[:, 0] > self.x0) & (pts[:, 1] > self.y0) & \
                 (pts[:, 0] < self.x0 + self.nx * ALIGN_CELL_MM) & (pts[:, 1] < self.y0 + self.ny * ALIGN_CELL_MM)
        ix, iy = self._idx(pts)
        return float((self.near[iy, ix] & inside).mean()) if len(pts) else 0.0


COARSE_CELL_MM = 100.0        # 最初の見当（相互相関）のマス目


def _grid(pts: np.ndarray, cell: float) -> tuple[np.ndarray, np.ndarray]:
    lo = pts.min(axis=0)
    idx = ((pts - lo) / cell).astype(int)
    g = np.zeros((idx[:, 1].max() + 1, idx[:, 0].max() + 1))
    g[idx[:, 1], idx[:, 0]] = 1.0
    return g, lo


def _coarse_offset(model_pts: np.ndarray, draw_pts: np.ndarray, cell: float = COARSE_CELL_MM) -> np.ndarray:
    """模型の壁の面の点と図面の線の点をマス目にして重ね、いちばん重なる平行移動（相互相関の山。FFT）。
    寸法線・家具などの余計な線があっても、壁の線がそろう位置が山になる。"""
    m, m0 = _grid(model_pts, cell)
    dg, d0 = _grid(draw_pts, cell)
    H, W = m.shape[0] + dg.shape[0], m.shape[1] + dg.shape[1]
    corr = np.fft.irfft2(np.fft.rfft2(m, (H, W)) * np.conj(np.fft.rfft2(dg, (H, W))), (H, W))
    ky, kx = np.unravel_index(int(np.argmax(corr)), corr.shape)
    ky = ky - H if ky > H // 2 else ky
    kx = kx - W if kx > W // 2 else kx
    return m0 - d0 + np.array([kx, ky]) * cell


def align_plan(d: Drawing, b: dict, level: str | None = "1F", hint: dict | None = None) -> dict | None:
    """平面図の座標 → 模型の座標の変換（回転 0/90/180/270°＋平行移動）。合わなければ None（推測で合わせない）。
    図面の線（本体）を点にして、模型の壁の面（40mm 以内）に乗る割合がいちばん大きい向きと位置を探す。
    hint（1階の平面図で決まった変換）があれば、その向きのまま近く（±400mm）だけを探す
    （★物件B の2階は壁が8枚で形が対称に近く、向きを決めきれなかった。図面は同じ並べ方で描かれている）。"""
    faces = _wall_faces(b, level)
    if not len(faces) or not len(d.lines):
        return None
    raster = _Raster(faces)
    src = getattr(d, "align_lines", None)               # PDF：縦横の長い線だけ（core/pdf_drawing.py _strong）
    pts = _sample(src if src is not None and len(src) else d.lines, 50.0)
    results = []
    model_pts = _sample(faces, COARSE_CELL_MM / 2)
    for theta in ((hint["rot"],) if hint else (0, 90, 180, 270)):
        rp = _rot(theta, pts)
        # 最初の見当：相互相関の山（hint があれば 1階の変換）。そのまわりを細かく探す
        t0 = np.array([hint["tx"], hint["ty"]]) if hint else _coarse_offset(model_pts, rp)
        best = (-1.0, t0)
        steps = ((40.0, 400.0), (10.0, 50.0)) if hint else ((50.0, 300.0), (10.0, 60.0))
        for step, reach in steps:
            c = best[1]
            tried = []
            for dx in np.arange(-reach, reach + 1e-6, step):
                for dy in np.arange(-reach, reach + 1e-6, step):
                    t = c + np.array([dx, dy])
                    s = raster.score(rp + t)
                    tried.append((s, t))
                    if s > best[0]:
                        best = (s, t)
            # 「乗っている」とみなす幅（40mm）の中では点数が同じになるので、いちばん良い点数の所の真ん中を取る
            top_ts = [t for s, t in tried if s >= best[0] - 1e-9]
            if top_ts:
                best = (best[0], np.mean(top_ts, axis=0))
        results.append({"rot": theta, "tx": float(best[1][0]), "ty": float(best[1][1]), "score": round(best[0], 3)})
    results.sort(key=lambda r: -r["score"])
    top = results[0]
    if hint:
        top["hint"] = True
        return top if top["score"] >= ALIGN_MIN_SCORE else None
    second = results[1]
    top["second"] = second["score"]
    if top["score"] < ALIGN_MIN_SCORE or top["score"] < second["score"] * ALIGN_MIN_MARGIN:
        return None
    return top


PDF_TRY_SCALES = (100, 50, 200, 150, 300, 30)   # PDF の縮尺が分からないときに試す（1/100 → 1/50 → …）


def _align_pdf_scale(alias: str, d: Drawing, b: dict, level: str) -> tuple[Drawing, dict | None]:
    """縮尺が分からない PDF の平面図：縮尺を変えながら位置合わせして、いちばん合う縮尺に決める（控えに残す）。"""
    from core import reading
    from core.paths import property_files
    from core.pdf_drawing import rescale
    best = None
    for s in PDF_TRY_SCALES:
        ds = rescale(d, s)
        a = align_plan(ds, b, level)
        if a is not None and (best is None or a["score"] > best[2]["score"]):
            best = (s, ds, a)
    if best is None:
        return d, None
    p = property_files(alias).jww.get("plan")
    if p is not None:
        reading.save_scale(alias, "plan", p, best[0], "aligned")
    best[2]["pdf_scale"] = best[0]
    return best[1], best[2]


def align_property(alias: str, b: dict) -> dict:
    """物件の平面図（1階・2階）の位置合わせ。{"plan": 変換か None, "plan_2f": …}。2階は1階の変換を手がかりにする。
    PDF の平面図で縮尺が分からないときは、縮尺も探す（_align_pdf_scale）。"""
    out = {}
    d1 = read_property(alias, "plan")
    lv1 = (b.get("levels") or [{}])[0].get("name", "1F")
    if d1 is not None and getattr(d1, "scale_source", "") == "assumed":
        d1, out["plan"] = _align_pdf_scale(alias, d1, b, lv1)
    else:
        out["plan"] = align_plan(d1, b, lv1) if d1 else None
    if len(b.get("levels") or []) > 1:
        d2 = read_property(alias, "plan_2f")
        out["plan_2f"] = align_plan(d2, b, b["levels"][1]["name"], hint=out["plan"]) if d2 and out["plan"] else None
    return out


def to_model(align: dict, x: float, y: float) -> tuple[float, float]:
    p = _rot(align["rot"], np.array([[x, y]], float))[0]
    return float(p[0] + align["tx"]), float(p[1] + align["ty"])
