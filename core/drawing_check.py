"""図面（jww）と模型の照合（SPEC §4-2・§12「物件Aは窓13か所の注記とも照合」）。

- 平面図を模型に位置合わせする（core/jww.align_property。合わなければ照合しない＝推測で合わせない）。
- 窓・テラスドアの注記（例「〇07405(FL+2400)」＝サッシの品番と上端の高さ）を、近い外壁の開口に1対1で結ぶ。
- 模型の開口の上端（FL から）と注記の FL+ を比べ、違えば「図面の注記と違う」。直す案の数値は注記の値（コードが読む）。
- 注記の文字は決まった形（品番・FL+）だけを使う。表題欄・縮尺1の層の文字は core/jww.py で読み捨て済み。
"""
from __future__ import annotations

import json
import math
import re

import numpy as np

NOTE_RE = re.compile(r"[〇●○◯]+\s*(\d{5})[^()（）]*?(?:[\(（][LR][\)）])?\s*[\(（]FL\s*[+＋]\s*(\d+)\s*[\)）]")
MATCH_MAX_MM = 1800.0       # 注記と開口の真ん中の距離の上限（注記は壁の外側、窓の近くに書かれる）
SAME_MM = 25.0              # これ以内の差は同じとみなす（模型で 0.5mm）
PLAN_KIND = {0: "plan", 1: "plan_2f"}
VIEW_OF = {"plan": "plan", "plan_2f": "plan2"}


def window_notes(d) -> list[dict]:
    """平面図の文字から、品番と FL+ のある注記（窓・テラスドア）を拾う。"""
    out = []
    for t in d.texts:
        m = NOTE_RE.search(t["text"])
        if m:
            out.append({"note_id": t["id"], "code": m.group(1), "head_fl_mm": int(m.group(2)), "x": t["cx"], "y": t["cy"]})
    return out


def match_notes(notes: list[dict], b: dict, align: dict, level: str) -> tuple[list[dict], list[dict]]:
    """注記を、同じ階の外壁・単独の壁の開口に、近い順に1対1で結ぶ。（結べたもの, 結べなかった注記）"""
    from core.jww import to_model
    walls = {w["id"]: w for w in b.get("walls", [])}
    cands = [o for o in b.get("openings", [])
             if (o.get("level") or walls.get(o.get("wall"), {}).get("level")) == level
             and walls.get(o.get("wall"), {}).get("kind") in ("exterior", "standalone") and o.get("center")]
    pos = [to_model(align, n["x"], n["y"]) for n in notes]
    pairs = sorted((math.dist(pos[i], o["center"]), i, o["id"]) for i in range(len(notes)) for o in cands)
    used_n, used_o, matched = set(), set(), []
    for dist, i, oid in pairs:
        if dist > MATCH_MAX_MM:
            break
        if i in used_n or oid in used_o:
            continue
        used_n.add(i)
        used_o.add(oid)
        matched.append({**notes[i], "opening": oid, "distance_mm": round(dist)})
    return matched, [n for i, n in enumerate(notes) if i not in used_n]


def summary(alias: str, b: dict) -> dict:
    """図面と模型の照合の結果。b は修正を当てたあとの building。図面がなければ available=False。"""
    from core.jww import align_property, read_property
    from core.paths import property_files
    files = property_files(alias)
    if "plan" not in files.jww or not files.jww["plan"].exists():
        return {"available": False, "note": "平面図（jww）が登録されていません"}
    aligned = align_property(alias, b)
    ops = {o["id"]: o for o in b.get("openings", [])}
    levels = []
    for i, lv in enumerate(b.get("levels") or []):
        kind = PLAN_KIND.get(i)
        al = aligned.get(kind) if kind else None
        row = {"kind": kind, "level": lv.get("name"), "aligned": al is not None}
        if al is None:
            row["note"] = "平面図を模型に位置合わせできなかったので、照合していません（推測で合わせない）"
            levels.append(row)
            continue
        d = read_property(alias, kind)
        notes = window_notes(d)
        matched, unmatched = match_notes(notes, b, al, lv.get("name"))
        mism = []
        for m in matched:
            o = ops[m["opening"]]
            if abs(float(o["head_fl_mm"]) - m["head_fl_mm"]) > SAME_MM:
                mism.append({**m, "model_head_fl_mm": o["head_fl_mm"]})
        row.update(score=al["score"], notes=len(notes), matched=len(matched), unmatched=len(unmatched),
                   same=len(matched) - len(mism), mismatches=mism,
                   unmatched_notes=[{k: n[k] for k in ("note_id", "code", "head_fl_mm")} for n in unmatched],
                   pairs=[{"opening": m["opening"], "code": m["code"], "head_fl_mm": m["head_fl_mm"]} for m in matched])
        levels.append(row)
    return {"available": True, "levels": levels}


def findings(alias: str, b: dict) -> list[dict]:
    """照合で違った所を、コードの指摘にする（直す案つき。数値は注記の値）。"""
    s = summary(alias, b)
    out = []
    for row in s.get("levels", []):
        for m in row.get("mismatches", []):
            out.append({"view": VIEW_OF.get(row["kind"], "plan"), "kind": "opening", "severity": "中",
                        "message": f"{m['opening']} の上端が図面の注記と違う：模型 FL+{m['model_head_fl_mm']:.0f}、"
                                   f"平面図の注記 FL+{m['head_fl_mm']}（品番 {m['code']}）",
                        "targets": [m["opening"]], "evidence": [f"jww:{row['kind']}:{m['note_id']}"], "source": "code",
                        "proposal": {"target": m["opening"], "field": "head_fl_mm", "from": m["model_head_fl_mm"],
                                     "to": m["head_fl_mm"], "value_source": f"jww:{row['kind']}:{m['note_id']}",
                                     "reason": f"平面図の注記（品番 {m['code']}）の上端 FL+{m['head_fl_mm']} に合わせる"}})
        for n in row.get("unmatched_notes", []):
            out.append({"view": VIEW_OF.get(row["kind"], "plan"), "kind": "opening", "severity": "低",
                        "message": f"平面図に窓・テラスドアの注記（品番 {n['code']}、FL+{n['head_fl_mm']}）があるが、"
                                   f"近く（{MATCH_MAX_MM / 1000:.1f}m 以内）に模型の開口がない。図面だけにある窓か、注記が離れて書かれているか（要確認）",
                        "targets": [], "evidence": [f"jww:{row['kind']}:{n['note_id']}"], "source": "code"})
    return out


# ---------------------------------------------------------------- 図面の画像（検品・囲んで直すの Claude に見せる）

VIEW_KIND = {"plan": "plan", "plan2": "plan_2f"}
_CACHE: dict[tuple, tuple] = {}


def _aligned(alias: str, b: dict, kind: str):
    """（図面, 変換）。位置合わせは数秒かかるので、building・修正・図面の更新時刻が同じなら使い回す。"""
    from core import overrides as ov
    from core.jww import align_property, read_property
    from core.paths import out_dir, property_files
    f = property_files(alias).jww.get(kind)
    stamp = tuple(p.stat().st_mtime if p is not None and p.exists() else 0
                  for p in (out_dir(alias) / "building.json", ov.overrides_path(alias), f))
    key = (alias, kind)
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    d = read_property(alias, kind)
    al = align_property(alias, b).get(kind) if d is not None else None
    _CACHE[key] = (stamp, (d, al))
    return d, al


def item_value(alias: str, b: dict, kind: str, item_id: str) -> float | None:
    """図面の寸法（D番号）か注記（T番号）の数値。注記は FL+ の値（窓の上端）、なければ文字の中の数値1つ。"""
    if kind in VIEW_KIND.values():
        d, _ = _aligned(alias, b, kind)
    elif kind in ("elevation", "section"):
        from core.jww import read_property
        try:
            d = read_property(alias, kind)                  # 立面図・断面図は寸法（D番号）だけ使う
        except (KeyError, FileNotFoundError):
            return None
        if d is not None:
            return next((float(x["value"]) for x in d.dims if x["id"] == item_id), None)
        return None
    else:
        return None
    if d is None:
        return None
    for x in d.dims:
        if x["id"] == item_id:
            return float(x["value"])
    for t in d.texts:
        if t["id"] == item_id:
            m = NOTE_RE.search(t["text"]) or re.search(r"FL\s*[+＋]\s*(\d+)", t["text"])
            if m:
                return float(m.group(m.lastindex))
            nums = re.findall(r"\d+(?:\.\d+)?", t["text"].replace(",", ""))
            return float(nums[0]) if len(nums) == 1 else None
    return None


def drawing_view(alias: str, b: dict, view: str, canvas: dict, dst, bbox_px: list[int] | None = None) -> dict:
    """面（plan / plan2）の図面の画像を、検品の模型の図と同じ縮尺・同じ位置で描き、寸法と注記の一覧を返す。
    描くのは図面の本体の線と、寸法（D番号）と窓の注記（T番号。品番と FL+）だけ（表題欄は読み捨て済み）。"""
    from PIL import Image, ImageDraw

    from core.jww import to_model
    from core.render import _font
    kind = VIEW_KIND.get(view)
    if kind is None:
        return {"available": False, "note": "立面・断面の図面はまだ読めません（平面図だけ）。立面の判断は glb と模型の見比べで"}
    d, al = _aligned(alias, b, kind)
    if d is None:
        return {"available": False, "note": "この面の平面図（jww）が登録されていません"}
    if al is None:
        return {"available": False, "note": "平面図を模型に位置合わせできなかったので、図面は使えません（推測で合わせない）"}
    lo, hi, k = canvas["uv_lo"], canvas["uv_hi"], float(canvas["px_per_mm"])
    w, h = canvas["size_px"]
    margin = (w - (hi[0] - lo[0]) * k) / 2

    def px(x: float, y: float) -> tuple[float, float]:
        mx, my = to_model(al, x, y)
        return ((mx - lo[0]) * k + margin, (hi[1] - my) * k + margin)

    img = Image.new("RGB", (int(w), int(h)), "white")
    dr = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in d.lines:
        dr.line([px(x0, y0), px(x1, y1)], fill=(60, 60, 60), width=1)
    font = _font(11)
    region = bbox_px
    title = f"{alias}　図面（jww の平面図。表題欄は除く）　模型の図と同じ縮尺・位置"
    dr.text((10, 8), title, fill=(0, 0, 0), font=_font(14))
    placed = [dr.textbbox((10, 8), title, font=_font(14))]

    def label(x: float, y: float, text: str, color) -> None:
        """文字を画像の中に収め、ほかの文字と重ならない所（下へずらす）に置く。"""
        bw, bh = dr.textbbox((0, 0), text, font=font)[2:]
        x = min(max(x, 2), w - bw - 2)
        y = min(max(y, 2), h - bh - 2)
        for _ in range(12):
            box = (x, y, x + bw, y + bh)
            if not any(box[0] < q[2] and q[0] < box[2] and box[1] < q[3] and q[1] < box[3] for q in placed):
                break
            y = y + bh + 1 if y + 2 * bh + 3 < h else y - bh - 1
        placed.append((x, y, x + bw, y + bh))
        dr.text((x, y), text, fill=color, font=font)

    dims, notes = [], []
    for n in window_notes(d):                      # 注記を先に置く（窓の上端の根拠になるので見やすく）
        p = px(n["x"], n["y"])
        if region and not (region[0] <= p[0] <= region[2] and region[1] <= p[1] <= region[3]):
            continue
        label(p[0] + 2, p[1] - 12, f"{n['note_id']} {n['code']} FL+{n['head_fl_mm']}", (200, 0, 0))
        notes.append({"id": n["note_id"], "code": n["code"], "head_fl_mm": n["head_fl_mm"], "at_px": [round(p[0]), round(p[1])]})
    for x in d.dims:
        a, c = px(x["x0"], x["y0"]), px(x["x1"], x["y1"])
        mid = ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2)
        if region and not (region[0] <= mid[0] <= region[2] and region[1] <= mid[1] <= region[3]):
            continue
        label(mid[0] + 2, mid[1] + 2, f"{x['id']} {x['text']}", (0, 90, 200))
        dims.append({"id": x["id"], "value_mm": x["value"], "text": x["text"], "at_px": [round(mid[0]), round(mid[1])]})
    img.save(dst)
    return {"available": True, "image_path": str(dst), "kind": kind, "align_score": al["score"],
            "dims": dims, "notes": notes,
            "note": "寸法は D番号、窓・テラスドアの注記は T番号（品番と上端 FL+）。直す案の数値に使うときは "
                    f"value_source=\"jww:{kind}:<番号>\"（数値はコードが図面から読む）。図面の線は位置合わせした結果で、数 cm ずれることがある"}


# ---------------------------------------------------------------- 立面図・断面図の画像（位置合わせなし。図面1枚をそのまま）

SHEET_KINDS = ("elevation", "section", "plan", "plan_2f")
SHEET_WIDTH_PX = 1400


def sheet_view(alias: str, kind: str, dst) -> dict:
    """図面1枚の画像（表題欄なし）と寸法の一覧。立面図（4面が1枚）・断面図は模型と位置を合わせないので、Claude が画像で見比べる。
    描くのは本体の線と、寸法（D番号）・窓の注記（T番号）だけ。"""
    from PIL import Image, ImageDraw

    from core.jww import read_property
    from core.render import _font
    if kind not in SHEET_KINDS:
        return {"available": False, "note": f"図面の種類は {'・'.join(SHEET_KINDS)} のどれかです"}
    d = read_property(alias, kind)
    if d is None:
        return {"available": False, "note": f"この物件の {kind} の図面（jww）が登録されていません"}
    if not len(d.lines):
        return {"available": False, "note": "図面に線がありません"}
    pts = np.vstack([d.lines[:, :2], d.lines[:, 2:]])
    lo, hi = np.percentile(pts, 0.5, axis=0), np.percentile(pts, 99.5, axis=0)      # 枠の外の飛び離れた線は外す
    k = (SHEET_WIDTH_PX - 40) / max(hi[0] - lo[0], 1.0)
    w, h = SHEET_WIDTH_PX, int((hi[1] - lo[1]) * k) + 60

    def px(x: float, y: float) -> tuple[float, float]:
        return ((x - lo[0]) * k + 20, (hi[1] - y) * k + 40)

    img = Image.new("RGB", (w, h), "white")
    dr = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in d.lines:
        dr.line([px(x0, y0), px(x1, y1)], fill=(60, 60, 60), width=1)
    font = _font(11)
    title = f"{alias}　図面（jww の{ {'elevation': '立面図', 'section': '断面図', 'plan': '平面図', 'plan_2f': '2階の平面図'}[kind] }。表題欄は除く）"
    dr.text((10, 8), title, fill=(0, 0, 0), font=_font(14))
    dims = []
    for x in d.dims:
        a, c = px(x["x0"], x["y0"]), px(x["x1"], x["y1"])
        mid = ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2)
        dr.line([a, c], fill=(0, 90, 200), width=1)
        dr.text((mid[0] + 2, mid[1] + 1), f"{x['id']} {x['text']}", fill=(0, 90, 200), font=font)
        dims.append({"id": x["id"], "value_mm": x["value"], "text": x["text"], "at_px": [round(mid[0]), round(mid[1])]})
    for n in window_notes(d):
        p = px(n["x"], n["y"])
        dr.text((p[0] + 2, p[1] - 12), f"{n['note_id']} {n['code']} FL+{n['head_fl_mm']}", fill=(200, 0, 0), font=font)
    img.save(dst)
    return {"available": True, "image_path": str(dst), "kind": kind, "scale": f"1/{d.scale:g}", "dims": dims,
            "note": ("模型とは位置を合わせていない図面1枚の画像（立面図は4つの面が1枚に入っていることが多い）。"
                     f"寸法の数値で直すときは value_source=\"jww:{kind}:D番号\"（数値はコードが図面から読む）。"
                     "寸法がどこからどこまでか（通り芯からか、外壁の外の面からか）が分からないときは、使わずに人に聞く")}


def overlay_lines(alias: str, b: dict, level: str | None = None) -> tuple[list | None, str]:
    """平面図に重ねる図面の線（模型の座標）と、ひとこと。図面がない・合わないときは (None, 理由)。"""
    from core.jww import _rot
    names = [lv["name"] for lv in b.get("levels") or []]
    kind = "plan" if level in (None, names[0] if names else None) else "plan_2f"
    try:
        d, al = _aligned(alias, b, kind)
    except Exception as e:  # noqa: BLE001
        return None, f"図面を読めませんでした（{type(e).__name__}）"
    if d is None:
        return None, "この階の平面図（jww）が登録されていません"
    if al is None:
        return None, "平面図を模型に位置合わせできなかったので、重ねていません"
    a = _rot(al["rot"], d.lines[:, :2]) + [al["tx"], al["ty"]]
    c = _rot(al["rot"], d.lines[:, 2:]) + [al["tx"], al["ty"]]
    segs = [(float(p[0]), float(p[1]), float(q[0]), float(q[1])) for p, q in zip(a, c)]
    return segs, f"緑の線＝図面（jww の平面図。位置合わせの点数 {al['score']:.2f}）。表題欄と文字は重ねていません"


# ---------------------------------------------------------------- 立面図の位置合わせ（面ごと）

ELEV_VIEWS = ("S", "N", "E", "W")
ELEV_LONG_MM = 15000.0      # これより長い線（4面をつなぐ地面の線など）は、まとまりを分けるときに使わない
ELEV_GAP_MM = 600.0         # この距離より近い線どうしを、同じ面のまとまりにする
ELEV_MIN_LINES = 20         # これより線の少ないまとまりは、近くの大きいまとまりへ足す（壁の真ん中の窓など）
ELEV_MIN_SCORE = 0.4        # 合わせた点数（図面→模型、模型→図面の割合の調和平均）の下限。下回る面は合わせない
ELEV_CELL_MM = 20.0


def _elevation_groups(d) -> list[np.ndarray]:
    """立面図の線を面ごとのまとまりに分ける（1枚に4面が描かれている）。"""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    lines = [ln for ln in d.lines if 1.0 < float(np.hypot(ln[2] - ln[0], ln[3] - ln[1])) < ELEV_LONG_MM]
    if not lines:
        return []
    segs = [LineString([(ln[0], ln[1]), (ln[2], ln[3])]) for ln in lines]
    blob = unary_union([s.buffer(ELEV_GAP_MM) for s in segs])
    comps = list(getattr(blob, "geoms", [blob]))
    members = [[i for i, s in enumerate(segs) if c.contains(s.centroid)] for c in comps]
    big = [k for k in range(len(comps)) if len(members[k]) >= ELEV_MIN_LINES]
    groups = {k: list(members[k]) for k in big}
    for k in range(len(comps)):
        if k in big or not big:
            continue
        host = min(big, key=lambda j: comps[j].envelope.distance(comps[k].centroid))
        if comps[host].envelope.buffer(1000).contains(comps[k].centroid):
            groups[host] += members[k]
    return [np.array([lines[i] for i in idx], float) for idx in groups.values()]


def _facing_openings(b: dict, view: str) -> list[np.ndarray]:
    """その面を向いた外壁・単独の壁の窓・ドアの四角の点（検品の図の情報がないときの予備。陰になるものも入る）。"""
    from shapely.geometry import Point, Polygon

    from core.render import VIEW_DIR, project
    outline = Polygon(b["foundation"]["outline"])
    walls = {w["id"]: w for w in b.get("walls", [])}
    out = []
    for o in b.get("openings", []):
        w = walls.get(o.get("wall"))
        if not w or w["kind"] not in ("exterior", "standalone") or not o.get("center"):
            continue
        s0, s1 = np.array(w["start"], float), np.array(w["end"], float)
        dvec = (s1 - s0) / (np.linalg.norm(s1 - s0) or 1.0)
        n = np.array([-dvec[1], dvec[0]])
        if outline.contains(Point(*((s0 + s1) / 2 + n * (w["thickness_mm"] + 50)))):
            n = -n                                                     # 外向きにする
        if float(n @ VIEW_DIR[view][:2]) > -0.7:
            continue
        c = np.array(o["center"], float)
        a0, a1 = c - dvec * o["width_mm"] / 2, c + dvec * o["width_mm"] / 2
        corners = np.array([[*a0, o["sill_mm"]], [*a1, o["sill_mm"]], [*a1, o["head_mm"]], [*a0, o["head_mm"]]])
        uvc, _ = project(view, corners)
        for i in range(4):
            q0, q1 = uvc[i], uvc[(i + 1) % 4]
            k = max(int(np.hypot(*(q1 - q0)) / ELEV_CELL_MM), 1)
            out.append(q0 + (q1 - q0) * np.linspace(0.0, 1.0, k + 1)[:, None])
    return out


def _face_reference(parts: dict, view: str, info: dict | None, b: dict | None = None) -> np.ndarray:
    """模型をその面の向きから写した影の輪郭の点と、その面で見えている窓・ドアの四角の点（模型の (u, v)、mm）。"""
    from PIL import Image, ImageDraw

    from core.render import project
    verts, tris, off = [], [], 0
    for p in parts.values():
        m = p.solid.to_mesh()
        v = np.asarray(m.vert_properties, float)[:, :3]
        verts.append(v)
        tris.append(np.asarray(m.tri_verts, np.int64) + off)
        off += len(v)
    uv, _ = project(view, np.vstack(verts))
    f = np.vstack(tris)
    lo = uv.min(axis=0) - 200
    hi = uv.max(axis=0) + 200
    W, H = int((hi[0] - lo[0]) / ELEV_CELL_MM) + 1, int((hi[1] - lo[1]) / ELEV_CELL_MM) + 1
    img = Image.new("1", (W, H), 0)
    dr = ImageDraw.Draw(img)
    px = (uv - lo) / ELEV_CELL_MM
    for t in f:
        dr.polygon([(px[i, 0], H - px[i, 1]) for i in t], fill=1)
    mask = np.array(img, bool)
    edge = mask & ~(np.roll(mask, 1, 0) & np.roll(mask, -1, 0) & np.roll(mask, 1, 1) & np.roll(mask, -1, 1))
    ys, xs = np.nonzero(edge)
    pts = [np.column_stack([lo[0] + xs * ELEV_CELL_MM, lo[1] + (H - ys) * ELEV_CELL_MM])]
    if info and "uv_lo" in info:      # 検品の図で「見えている」窓・ドア（単独の壁の陰などは除く）の四角
        k = float(info["px_per_mm"])
        ulo, uhi = info["uv_lo"], info["uv_hi"]
        margin = (info["size_px"][0] - (uhi[0] - ulo[0]) * k) / 2
        for e in info.get("elements", []):
            if e.get("type") != "opening" or e.get("hidden"):
                continue
            x0, y0, x1, y1 = e["bbox_px"]
            u0, u1 = ulo[0] + (x0 - margin) / k, ulo[0] + (x1 - margin) / k
            v0, v1 = uhi[1] - (y1 - margin) / k, uhi[1] - (y0 - margin) / k
            for a, c in (((u0, v0), (u1, v0)), ((u1, v0), (u1, v1)), ((u1, v1), (u0, v1)), ((u0, v1), (u0, v0))):
                n = max(int(np.hypot(c[0] - a[0], c[1] - a[1]) / ELEV_CELL_MM), 1)
                t = np.linspace(0.0, 1.0, n + 1)[:, None]
                pts.append(np.array(a) + (np.array(c) - np.array(a)) * t)
    elif b is not None:
        pts += _facing_openings(b, view)
    return np.vstack(pts)


def _fit(model_pts: np.ndarray, lines: np.ndarray) -> tuple[float, np.ndarray]:
    """図面のまとまりを、模型の面に重ねる平行移動と点数（両向きの割合の調和平均）。"""
    from core.jww import _coarse_offset, _Raster, _sample
    raster = _Raster(np.column_stack([model_pts, model_pts + 1.0]))
    dp = _sample(lines, 50.0)
    best = (-1.0, _coarse_offset(model_pts, dp))
    for step, reach in ((50.0, 400.0), (10.0, 60.0)):
        c = best[1]
        tried = []
        for dx in np.arange(-reach, reach + 1e-6, step):
            for dy in np.arange(-reach, reach + 1e-6, step):
                t = c + np.array([dx, dy])
                s = raster.score(dp + t)
                tried.append((s, t))
                if s > best[0]:
                    best = (s, t)
        best = (best[0], np.mean([t for s, t in tried if s >= best[0] - 1e-9], axis=0))
    there = raster.score(dp + best[1])
    back = _Raster(lines + np.concatenate([best[1], best[1]])).score(model_pts)
    return (2 * there * back / (there + back) if there + back else 0.0), best[1]


def align_elevations(alias: str, b: dict) -> dict:
    """立面図の面ごとの位置合わせ。{面: {"group", "tx", "ty", "score"} か None}。合わない面は None（推測で合わせない）。
    図面の4つのまとまりと模型の4つの面の組み合わせを全部試し、点数の合計がいちばん大きい割り当てを選ぶ。"""
    import itertools

    from core.export import make_model
    from core.jww import read_property
    from core.paths import out_dir
    d = read_property(alias, "elevation")
    if d is None:
        return {}
    groups = _elevation_groups(d)
    if not groups:
        return {}
    _, parts, _, _ = make_model(alias)
    table: dict[tuple, tuple] = {}
    for view in ELEV_VIEWS:
        p = out_dir(alias) / "inspect" / f"{view}.json"
        info = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        ref = _face_reference(parts, view, info, b)
        for gi, lines in enumerate(groups):
            table[(view, gi)] = _fit(ref, lines)
    n = len(groups)
    choices = [list(perm) + [None] * (len(ELEV_VIEWS) - len(perm))
               for perm in itertools.permutations(range(n), min(n, len(ELEV_VIEWS)))]
    best = max(choices, key=lambda perm: sum(table[(v, g)][0] for v, g in zip(ELEV_VIEWS, perm) if g is not None))
    out = {}
    for v, g in zip(ELEV_VIEWS, best):
        if g is None or table[(v, g)][0] < ELEV_MIN_SCORE:
            out[v] = None
            continue
        score, t = table[(v, g)]
        out[v] = {"group": g, "tx": float(t[0]), "ty": float(t[1]), "score": round(score, 3)}
    out["_groups"] = groups
    out["_drawing"] = d
    return out


def _elevations(alias: str, b: dict) -> dict:
    """align_elevations を、building・修正・図面の更新時刻が同じなら使い回す。"""
    from core import overrides as ov
    from core.paths import out_dir, property_files
    f = property_files(alias).jww.get("elevation")
    stamp = tuple(p.stat().st_mtime if p is not None and p.exists() else 0
                  for p in (out_dir(alias) / "building.json", ov.overrides_path(alias), f))
    key = (alias, "elevation")
    hit = _CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    res = align_elevations(alias, b)
    _CACHE[key] = (stamp, res)
    return res


def elevation_view(alias: str, b: dict, view: str, canvas: dict, dst) -> dict | None:
    """立面（S/N/E/W）の図面の画像を、検品の模型の図と同じ縮尺・位置で描く。合わせられない面は None。"""
    from PIL import Image, ImageDraw

    from core.render import _font
    al = _elevations(alias, b)
    a = al.get(view)
    if not a:
        return None
    lines = al["_groups"][a["group"]]
    lo, hi, k = canvas["uv_lo"], canvas["uv_hi"], float(canvas["px_per_mm"])
    w, h = canvas["size_px"]
    margin = (w - (hi[0] - lo[0]) * k) / 2

    def px(u: float, v: float) -> tuple[float, float]:
        return ((u + a["tx"] - lo[0]) * k + margin, (hi[1] - (v + a["ty"])) * k + margin)

    img = Image.new("RGB", (int(w), int(h)), "white")
    dr = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in lines:
        dr.line([px(x0, y0), px(x1, y1)], fill=(60, 60, 60), width=1)
    font = _font(11)
    x_lo, y_lo = lines[:, [0, 2]].min() - 500, lines[:, [1, 3]].min() - 500
    x_hi, y_hi = lines[:, [0, 2]].max() + 500, lines[:, [1, 3]].max() + 500
    dims = []
    for x in al["_drawing"].dims:
        mx, my = (x["x0"] + x["x1"]) / 2, (x["y0"] + x["y1"]) / 2
        if not (x_lo <= mx <= x_hi and y_lo <= my <= y_hi):
            continue
        p = px(mx, my)
        dr.text((p[0] + 2, p[1] + 1), f"{x['id']} {x['text']}", fill=(0, 90, 200), font=font)
        dims.append({"id": x["id"], "value_mm": x["value"], "text": x["text"], "at_px": [round(p[0]), round(p[1])]})
    dr.text((10, 8), f"{alias}　図面（jww の立面図。表題欄は除く）　模型の図と同じ縮尺・位置", fill=(0, 0, 0), font=_font(14))
    img.save(dst)
    return {"available": True, "image_path": str(dst), "kind": "elevation", "aligned": True, "align_score": a["score"],
            "dims": dims,
            "note": "立面図のこの面を、模型の図と同じ縮尺・位置に合わせて描いた（位置合わせした結果で、数 cm ずれることがある）。"
                    "寸法の数値で直すときは value_source=\"jww:elevation:D番号\"（数値はコードが図面から読む）"}
