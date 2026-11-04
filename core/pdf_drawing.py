"""PDF の図面を、jww と同じ形（core/jww.py の Drawing：線・文字・寸法、実寸 mm）にする（高性能読み込み。★2026-11-04）。

- 読むのは、CAD（Jw_cad・Walk in home）から書き出した「文字を選べる PDF」。スキャン（画像だけ）の PDF は読めないと伝える
  （作者：スキャンで画質が悪い所は、文字化けまではいかなくても違う感じになっていた）。
- 用紙の座標（左上が原点・pt）を、jww と同じ「用紙の真ん中が原点・上が +Y・mm」に直してから、縮尺を掛けて実寸にする。
- 線は描かれた線だけ（塗りつぶしの面・文字の形は捨てる）。表題欄（用紙の下の帯）の線と文字は捨てる。出してはいけない言葉の文字も捨てる。
- **PDF の文書の情報（作成者・題名など）と注釈は使わない**（施主名やファイルの場所が入りやすいため）。pdfplumber は開くときに
  文書の情報を読み込むが、HAKOYA はそれに触らず、どこにも出さない（線と文字だけを取り出す）。
- 縮尺：寸法の数字と、その横の線の長さの比で決める（多数決）。決まらなければ 1/100 と仮に置き、平面図の位置合わせで決め直す
  （core/jww.py align_property）。決めた縮尺は reading.json に控える。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import numpy as np

from core.jww import DIM_RE, Drawing

import logging
logging.getLogger("pdfminer").setLevel(logging.ERROR)      # 「字の大きさの情報がない」などの害のない警告を出さない

PT_MM = 25.4 / 72.0
TITLE_BAND_SEARCH_MM = 45.0      # 表題欄の上の線を探す範囲（用紙の下端から）
TITLE_LINE_SHARE = 0.8           # 表題欄の上の線：用紙の幅のこの割合より長い水平線
TITLE_FALLBACK_MM = 30.0         # 線が見つからないときの表題欄の高さ
SCALES = (10, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500)
MIN_VECTOR_LINES = 50            # これより線が少なく、大きな画像がある PDF はスキャンとみなす


class DrawingUnreadable(ValueError):
    """読めない図面（スキャンの PDF など）。文は画面にそのまま出してよい（場所・ファイル名は入れない）。"""


def _segments(page, k: float, W: float, H: float) -> list[list[float]]:
    """描かれた線（線・四角・折れ線）を、用紙 mm（真ん中が原点、上が +Y）の線分にする。塗りだけの面は捨てる。"""
    segs = []
    for kind in ("line", "rect", "curve"):
        for o in page.objects.get(kind, []):
            if not o.get("stroke", True) and o.get("fill"):
                continue                               # 塗りつぶしの面（文字の形・ハッチング）
            pts = o.get("pts") or []
            if kind == "rect" and len(pts) < 2:
                x0, x1, t, bt = o["x0"], o["x1"], o["top"], o["bottom"]
                pts = [(x0, t), (x1, t), (x1, bt), (x0, bt), (x0, t)]
            elif kind == "rect":
                pts = list(pts) + [pts[0]]
            pp = [(x * k - W / 2, H / 2 - top * k) for x, top in pts]
            for (ax, ay), (bx, by) in zip(pp, pp[1:]):
                if abs(ax - bx) + abs(ay - by) > 0.05:
                    segs.append([ax, ay, bx, by])
    return segs


def _title_band(segs: list[list[float]], W: float, H: float) -> float:
    """表題欄の上端（用紙 mm）：下端から TITLE_BAND_SEARCH_MM 以内で、用紙の幅の 8割より長い水平線のうち一番上。"""
    bottom = -H / 2
    cands = [max(a[1], a[3]) for a in segs
             if abs(a[1] - a[3]) < 0.3 and abs(a[2] - a[0]) >= TITLE_LINE_SHARE * W and a[1] - bottom <= TITLE_BAND_SEARCH_MM]
    return max(cands) if cands else bottom + TITLE_FALLBACK_MM


def _words(page, k: float, W: float, H: float) -> list[dict]:
    try:
        page = page.dedupe_chars()                     # 1文字ずつ重ねて書かれた PDF のため
    except Exception:                                  # noqa: BLE001 （古い版にはない）
        pass
    out = []
    for w in page.extract_words(x_tolerance=1.5, y_tolerance=1.5, keep_blank_chars=False, extra_attrs=["size", "upright"]):
        x0, x1 = w["x0"] * k - W / 2, w["x1"] * k - W / 2
        yt, yb = H / 2 - w["top"] * k, H / 2 - w["bottom"] * k
        out.append({"text": str(w["text"]), "x": x0, "y": yb, "cx": (x0 + x1) / 2, "cy": (yt + yb) / 2,
                    "h": abs(yt - yb), "w": abs(x1 - x0), "upright": bool(w.get("upright", True))})
    return out


def _nearest_dim_line(t: dict, segs: np.ndarray) -> np.ndarray | None:
    """寸法の数字のすぐ横（3mm 以内）にある、数字と同じ向きの線（寸法線）。"""
    if not len(segs):
        return None
    horiz = t["upright"]
    d = segs
    dx, dy = np.abs(d[:, 2] - d[:, 0]), np.abs(d[:, 3] - d[:, 1])
    par = (dy < 0.2 * dx) if horiz else (dx < 0.2 * dy)
    if horiz:
        mid_ok = (np.minimum(d[:, 0], d[:, 2]) <= t["cx"]) & (np.maximum(d[:, 0], d[:, 2]) >= t["cx"])
        gap = np.abs((d[:, 1] + d[:, 3]) / 2 - t["cy"])
        length = dx
    else:
        mid_ok = (np.minimum(d[:, 1], d[:, 3]) <= t["cy"]) & (np.maximum(d[:, 1], d[:, 3]) >= t["cy"])
        gap = np.abs((d[:, 0] + d[:, 2]) / 2 - t["cx"])
        length = dy
    ok = par & mid_ok & (gap <= 3.0) & (length >= 2.0)
    if not ok.any():
        return None
    i = int(np.argmin(np.where(ok, gap, np.inf)))
    return d[i]


def guess_scale(texts: list[dict], segs: np.ndarray) -> tuple[float | None, int]:
    """寸法の数字 ÷ 寸法線の長さ（用紙 mm）の多数決で縮尺を決める。（縮尺か None, 票の数）"""
    votes = Counter()
    for t in texts:
        if not DIM_RE.match(t["text"]):
            continue
        line = _nearest_dim_line(t, segs)
        if line is None:
            continue
        L = float(np.hypot(line[2] - line[0], line[3] - line[1]))
        r = float(t["text"].replace(",", "")) / L if L > 0 else 0
        near = [s for s in SCALES if abs(r - s) <= 0.03 * s]
        if near:
            votes[near[0]] += 1
    if not votes:
        return None, 0
    s, n = votes.most_common(1)[0]
    return (float(s), n) if n >= 3 else (None, n)


def read_pdf(path: Path, kind: str, ng_words: list[str] | None = None, scale: float | None = None, page: int = 0) -> Drawing:
    """PDF の1ページを Drawing にする。scale を渡さなければ寸法の数字から決め、決まらなければ 1/100 と仮に置く
    （d.scale_source が "dims"・"given"・"assumed" のどれか）。スキャンの PDF は DrawingUnreadable。"""
    import pdfplumber                                  # 使うときだけ読み込む（jww だけの物件では要らない）
    from core.privacy import find_ng
    if ng_words is None:
        from core.privacy import ng_words as _ng
        ng_words = _ng()
    with pdfplumber.open(str(path)) as pdf:            # ★文書の情報（pdf.metadata）と注釈（annots）には触らない
        if page >= len(pdf.pages):
            raise DrawingUnreadable("PDF にそのページがありません")
        pg = pdf.pages[page]
        W, H = float(pg.width) * PT_MM, float(pg.height) * PT_MM
        k = PT_MM
        segs = _segments(pg, k, W, H)
        img_area = sum(max(0.0, (i["x1"] - i["x0"]) * (i["bottom"] - i["top"])) for i in pg.images) * k * k
        if len(segs) < MIN_VECTOR_LINES and img_area > 0.5 * W * H:
            raise DrawingUnreadable("スキャンの PDF（画像）なので読めません。CAD から書き出した、文字を選べる PDF にしてください")
        words = _words(pg, k, W, H)
    band = _title_band(segs, W, H)
    dropped = {"title_band": 0, "ng_words": 0, "unreadable_text": 0, "off_paper": 0}
    kept_segs = []
    for s in segs:
        if min(s[1], s[3]) < band:
            dropped["title_band"] += 1
        else:
            kept_segs.append(s)
    paper_texts = []
    for t in words:
        if t["cy"] < band:
            dropped["title_band"] += 1                 # 表題欄の帯：読み捨てる
        elif "(cid:" in t["text"]:
            dropped["unreadable_text"] += 1            # 文字の表がなく読めない文字
        elif find_ng(t["text"], ng_words) is not None:
            dropped["ng_words"] += 1
        else:
            paper_texts.append(t)
    seg_arr = np.array(kept_segs, float).reshape(-1, 4)
    how = "given"
    if scale is None:
        s, _ = guess_scale(paper_texts, seg_arr)
        scale, how = (s, "dims") if s else (100.0, "assumed")
    texts = [{"id": f"T{i}", "text": t["text"].strip(), "x": t["x"] * scale, "y": t["y"] * scale,
              "cx": t["cx"] * scale, "cy": t["cy"] * scale, "h": t["h"] * scale} for i, t in enumerate(paper_texts, 1)]
    dims = []
    for t in paper_texts:
        if not DIM_RE.match(t["text"]):
            continue
        line = _nearest_dim_line(t, seg_arr)
        if line is None:
            continue
        dims.append({"id": f"D{len(dims) + 1}", "value": float(t["text"].replace(",", "")), "text": t["text"],
                     "x0": float(line[0]) * scale, "y0": float(line[1]) * scale, "x1": float(line[2]) * scale, "y1": float(line[3]) * scale})
    d = Drawing(kind=kind, paper_mm=(W, H), title_band_top=band, main_group=0, scale=float(scale),
                lines=seg_arr * scale, texts=texts, dims=dims, dropped=dropped)
    d.source, d.scale_source = "pdf", how
    d.align_lines = _strong(seg_arr) * scale
    return d


ALIGN_MIN_PAPER_MM = 5.0         # 位置合わせに使う線：縦か横で、用紙の上でこれ以上の長さ（壁の線。家具・扉の弧・網かけは除く）


def _strong(seg: np.ndarray) -> np.ndarray:
    """位置合わせに使う線（縦横の長い線）。★11/4 Walk in home の平面図の PDF（線 1万本以上）は、細かい線が多くて
    位置合わせが外れた縮尺を選んだ。縦横の 5mm 以上の線だけにすると、物件C・E・F とも 1/100・0° にはっきり決まった。"""
    if not len(seg):
        return seg
    dx, dy = np.abs(seg[:, 2] - seg[:, 0]), np.abs(seg[:, 3] - seg[:, 1])
    keep = ((dy < 0.02 * dx) | (dx < 0.02 * dy)) & (np.hypot(dx, dy) >= ALIGN_MIN_PAPER_MM)
    return seg[keep]


def rescale(d: Drawing, scale: float) -> Drawing:
    """同じ PDF の Drawing を、別の縮尺に掛け直す（平面図の位置合わせで縮尺を探すとき）。"""
    f = float(scale) / float(d.scale)
    out = Drawing(kind=d.kind, paper_mm=d.paper_mm, title_band_top=d.title_band_top, main_group=d.main_group, scale=float(scale),
                  lines=d.lines * f, texts=[{**t, "x": t["x"] * f, "y": t["y"] * f, "cx": t["cx"] * f, "cy": t["cy"] * f, "h": t["h"] * f}
                                           for t in d.texts],
                  dims=[{**x, "x0": x["x0"] * f, "y0": x["y0"] * f, "x1": x["x1"] * f, "y1": x["y1"] * f} for x in d.dims],
                  dropped=dict(d.dropped))
    out.source, out.scale_source = "pdf", "aligned"
    if getattr(d, "align_lines", None) is not None:
        out.align_lines = d.align_lines * f
    return out
