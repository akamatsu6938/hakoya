"""人が承認した修正（overrides.json）の記録・当て直し・取り消し（SPEC §6・§9-1）。

- 自動で作った値（building.json）と、人が承認した修正（overrides.json）は分けて持つ。
- 修正は1件ずつ記録し、1件ずつ取り消せる。
- 作り直したあとも、修正を上から当て直す。当て直せない修正（部材がなくなった等）は一覧に出す。
- 数値は人か、コードが出どころ（value_source）から計算した値。Claude は数値を作らない（SPEC §2-4）。

1件の形（SPEC §6）:
{"id": "OV7", "target": "O1", "field": "head_mm", "from": 3024, "to": 2824,
 "value_source": "jww:elevation:dim#40", "reason": "...", "origin": "finding:F3", "approved_by": "human", "at": "..."}
field に "_delete" を指定すると、その部材を取り除く（to は使わない）。
屋根（Y1 など）の軒の出は、入れ子の項目 "eave_mm.S" のように1段の点でつないで指定する。
階の高さは target "level:1F"・"level:2F"、field "fl_mm"（床の高さ、GL から）・"ceiling_mm"（天井の高さ、床から）。
床の高さを変えると、その階と上の階の絶対の高さ（壁・窓・部屋・ポーチの天井・屋根）を同じだけずらす。
基礎・外壁の下端・ポーチはそのまま（家が基礎の上で上下する。(推定) 2026-11-03、作者に確かめる）。
屋根の形は target "Y1" など、field "pitch"（勾配。10 に対する値）・"low_side"（低い側。片流れだけ）・
"eave_tip_mm"（軒先の高さ＝屋根の上の面のいちばん低い所）。当てるときにコードが面の式を計算し直す。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

from core.paths import out_dir

COLLECTIONS = {"W": "walls", "O": "openings", "R": "rooms", "P": "porches", "S": "porch_soffits", "Y": "roofs"}
DELETE = "_delete"
DELETABLE = ("W", "O", "R", "P", "S")      # 取り除ける部材（屋根は取り除けない）
TOL = 0.5  # 「前回の自動の値」と比べるときの許容差（mm）
EAVE_PREFIX = "eave_mm."
EAVE_RANGE_MM = (0.0, 2000.0)               # 軒の出（外壁の外の面から屋根の先まで、板金・樋を除く）の範囲
LEVEL_PREFIX = "level:"
LEVEL_FIELDS = ("fl_mm", "ceiling_mm")
CEILING_RANGE_MM = (1800.0, 4000.0)         # 天井の高さ（床から）
FL_MAX_MM = 3000.0                          # 1階の床の高さ（GL から）の上限
LEVEL_JA = {"1F": "1階", "2F": "2階"}
ROOF_GEOM_FIELDS = ("pitch", "low_side", "eave_tip_mm")   # 屋根の形（当てるときに面の式を計算し直す）
PITCH_RANGE = (0.5, 12.0)                                 # 勾配（10 に対する値）
SIDES = ("S", "N", "E", "W")
REACH_ROOF_MM = 150.0                                     # 屋根の下の面からこれ以内に上端がある壁は「屋根まで届いている」


def overrides_path(alias: str) -> Path:
    return out_dir(alias) / "overrides.json"


def load(alias: str) -> list[dict]:
    p = overrides_path(alias)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save(alias: str, items: list[dict]) -> None:
    p = overrides_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _find(building: dict, target: str) -> dict | None:
    """番号（W3・O1 など）か、"roof" のような名前から、修正する相手を探す。"""
    if target in ("roof", "foundation"):
        return building.get(target)
    if target.startswith("level:"):
        return next((lv for lv in building.get("levels", []) if lv["name"] == target.split(":", 1)[1]), None)
    coll = COLLECTIONS.get(target[:1])
    if coll is None:
        return None
    return next((x for x in building.get(coll, []) if x.get("id") == target), None)


def _is_roof(obj: dict) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("top_plane"), dict) and isinstance(obj.get("top_z_range_mm"), list)


def has_field(obj: dict, field: str) -> bool:
    """項目があるか。"eave_mm.S" のような1段の入れ子も見る。屋根の形（勾配・低い側・軒先の高さ）は計算で出す。"""
    if field in ROOF_GEOM_FIELDS and _is_roof(obj):
        return True                     # 向きを変えられない屋根（切妻・寄棟など）は、当てるときに理由つきで断る
    head, _, tail = field.partition(".")
    if not tail:
        return field in obj
    return isinstance(obj.get(head), dict) and tail in obj[head]


def get_field(obj: dict, field: str):
    if field in ROOF_GEOM_FIELDS and _is_roof(obj):
        if field == "pitch":
            return float(obj["pitch"][0]) if isinstance(obj.get("pitch"), list) else obj.get("pitch")
        if field == "eave_tip_mm":
            return float(obj["top_z_range_mm"][0])
        return obj.get("low_side")
    head, _, tail = field.partition(".")
    return obj[head][tail] if tail else obj[field]


def _set_field(obj: dict, field: str, value) -> None:
    """入れ子の項目は dict を作り直して入れる（roof と roofs の中身が同じ dict を共有していることがあり、
    その場で書き換えると、ほかの屋根や glb の値まで変わってしまうため）。"""
    head, _, tail = field.partition(".")
    if tail:
        obj[head] = {**obj[head], tail: value}
    else:
        obj[field] = value


def check_value(field: str, to, target: str = "") -> None:
    """値の範囲を確かめる（その値だけで決まるもの）。外れたら日本語の ValueError。
    家全体を見ないと決まらないもの（天井と屋根の間など）は level_problems で確かめる。"""
    if field.startswith(EAVE_PREFIX):
        lo, hi = EAVE_RANGE_MM
        if not isinstance(to, (int, float)) or not lo <= float(to) <= hi:
            raise ValueError(f"軒の出は {lo:.0f}〜{hi:.0f}mm の数値で入れてください（外壁の外の面から屋根の先まで、板金・樋を除く）")
    if target.startswith(LEVEL_PREFIX) and field in LEVEL_FIELDS:
        if not isinstance(to, (int, float)) or isinstance(to, bool):
            raise ValueError("階の高さは数値（mm）で入れてください")
        if field == "ceiling_mm":
            lo, hi = CEILING_RANGE_MM
            if not lo <= float(to) <= hi:
                raise ValueError(f"天井の高さは {lo:.0f}〜{hi:.0f}mm（床から）で入れてください")
        elif not 0 < float(to) <= FL_MAX_MM * 3:
            raise ValueError("床の高さは GL からの mm で入れてください")


def check_roof_value(field: str, to) -> None:
    """屋根の形（勾配・低い側・軒先の高さ）の値の範囲。面の式を持つ屋根に当てるときだけ使う。"""
    if field == "pitch":
        lo, hi = PITCH_RANGE
        if not isinstance(to, (int, float)) or isinstance(to, bool) or not lo <= float(to) <= hi:
            raise ValueError(f"勾配は 10 に対する値で、{lo}〜{hi} の数値で入れてください（例：1.5 は 1.5/10）")
    if field == "low_side" and to not in SIDES:
        raise ValueError("低い側は 南・北・東・西（S・N・E・W）のどれかです")
    if field == "eave_tip_mm" and (not isinstance(to, (int, float)) or isinstance(to, bool) or not 500 <= float(to) <= 20000):
        raise ValueError("軒先の高さは GL からの mm（500〜20000）で入れてください")


def _num(v: float):
    """整数に近ければ整数で（building.json の高さは整数が多い）。"""
    return int(round(v)) if abs(v - round(v)) < 1e-6 else round(v, 3)


def _level_index(building: dict, name: str | None) -> int | None:
    return next((i for i, lv in enumerate(building.get("levels") or []) if lv.get("name") == name), None)


def _roofs_with_level(building: dict) -> list[tuple[dict, int]]:
    """屋根の写し（上の段の roof と roofs の各要素）と、その屋根がかかる階の番号。同じ dict は1回だけ。"""
    levels = building.get("levels") or []
    out, seen = [], set()
    for r in building.get("roofs") or []:
        if isinstance(r, dict) and id(r) not in seen:
            seen.add(id(r))
            out.append((r, _level_index(building, r.get("level")) or 0))
    top = building.get("roof")
    if isinstance(top, dict) and id(top) not in seen and isinstance(top.get("top_plane"), dict):
        twin = _roof_twin(building, "roof")
        i = _level_index(building, twin.get("level")) if twin else None
        out.append((top, i if i is not None else max(len(levels) - 1, 0)))
    return out


def _shift_planes(obj: dict, dz: float) -> dict:
    """面の式の c を dz ずらした写し（入れ子の dict は作り直す）。"""
    out = dict(obj)
    for k in ("top_plane", "bottom_plane"):
        if isinstance(out.get(k), dict) and "c" in out[k]:
            out[k] = {**out[k], "c": float(out[k]["c"]) + dz}
    return out


def _shift_roof(roof: dict, dz: float) -> None:
    """屋根を dz 上下させる（面の式の c と、上の面の高さの範囲。面が複数の屋根は面ごとに）。入れ子の dict は作り直す。"""
    roof.update(_shift_planes(roof, dz))
    if isinstance(roof.get("planes"), list):
        roof["planes"] = [_shift_planes(p, dz) for p in roof["planes"]]
    if isinstance(roof.get("top_z_range_mm"), list):
        roof["top_z_range_mm"] = [_num(float(v) + dz) for v in roof["top_z_range_mm"]]


def _shift_from_level(building: dict, idx: int, fl_before: float, dz: float) -> None:
    """idx の階の床の高さを dz 変えたとき、その階と上の階の絶対の高さを dz ずらす。
    壁の下端は床より下（基礎の上から立つ外壁など）ならそのまま。ポーチ（地面の上）と基礎もそのまま。"""
    levels = building.get("levels") or []
    names = {lv.get("name") for lv in levels[idx:]}
    for lv in levels[idx + 1:]:
        lv["fl_mm"] = _num(float(lv["fl_mm"]) + dz)
    wall_level = {}
    for w in building.get("walls", []):
        wall_level[w.get("id")] = w.get("level")
        if w.get("level") not in names:
            continue
        if isinstance(w.get("bottom_mm"), (int, float)) and float(w["bottom_mm"]) >= fl_before - 1:
            w["bottom_mm"] = _num(float(w["bottom_mm"]) + dz)
        for k in ("height", "top_glb_mm"):
            if isinstance(w.get(k), (int, float)) and not isinstance(w.get(k), bool):
                w[k] = _num(float(w[k]) + dz)
    for o in building.get("openings", []):
        if (o.get("level") or wall_level.get(o.get("wall"))) in names:
            for k in ("sill_mm", "head_mm"):
                if isinstance(o.get(k), (int, float)):
                    o[k] = _num(float(o[k]) + dz)
    for r in building.get("rooms", []):
        if r.get("level") in names and isinstance(r.get("floor_mm"), (int, float)):
            r["floor_mm"] = _num(float(r["floor_mm"]) + dz)
    for coll in ("porch_soffits", "lowered_ceilings"):
        for s in building.get(coll, []):
            on = s["level"] in names if s.get("level") else float(s.get("z_mm", 0)) >= fl_before - 1
            if on and isinstance(s.get("z_mm"), (int, float)):
                s["z_mm"] = _num(float(s["z_mm"]) + dz)
    for roof, ridx in _roofs_with_level(building):
        if ridx >= idx:
            _shift_roof(roof, dz)


def _retop_ceiling(building: dict, lv: dict, old_ceiling: float, new_ceiling: float) -> None:
    """天井の高さを変えたとき、その階で天井まで立っていた壁（内壁・下がり壁）の上端を新しい天井にそろえる。"""
    fl = float(lv["fl_mm"])
    old_top, new_top = fl + old_ceiling, fl + new_ceiling
    for w in building.get("walls", []):
        h = w.get("height")
        if w.get("level") == lv.get("name") and isinstance(h, (int, float)) and abs(float(h) - old_top) <= 1:
            w["height"] = _num(new_top)


def _level_changed(building: dict, lv: dict, field: str, before, to) -> None:
    if not isinstance(before, (int, float)) or not isinstance(to, (int, float)):
        return
    if field == "fl_mm":
        idx = _level_index(building, lv.get("name"))
        if idx is not None:
            _shift_from_level(building, idx, float(before), float(to) - float(before))
    elif field == "ceiling_mm":
        _retop_ceiling(building, lv, float(before), float(to))


def _roof_bottom_min(roof: dict) -> float | None:
    """屋根の下の面のいちばん低い所（屋根の平面の形の頂点で測る。面が複数の屋根は面ごとに）。"""
    from core.roof_geom import bottom_min
    return bottom_min(roof)


def _plane_at(surface, x: float, y: float) -> float:
    if isinstance(surface, dict):
        return float(surface["a"]) * x + float(surface["b"]) * y + float(surface["c"])
    return surface.z(x, y)


def _all_planes(roof: dict) -> list[dict]:
    """屋根の上の面の式（面が複数なら面ごと）。"""
    pl = roof.get("planes") or []
    return [p["top_plane"] for p in pl] if len(pl) > 1 else [roof["top_plane"]]


def _thickness_perp(roof: dict) -> float:
    t = roof.get("thickness_mm")
    if isinstance(t, (int, float)) and t > 0:
        return float(t)
    tp, bp = roof["top_plane"], roof.get("bottom_plane") or roof["top_plane"]
    s = (tp["a"] ** 2 + tp["b"] ** 2) ** 0.5
    return (float(tp["c"]) - float(bp["c"])) / (1 + s * s) ** 0.5 or 100.0


def _with_bottom(top: dict, t_perp: float) -> tuple[dict, dict]:
    s = (top["a"] ** 2 + top["b"] ** 2) ** 0.5
    top = {"a": round(top["a"], 6), "b": round(top["b"], 6), "c": round(top["c"], 2)}
    return top, {"a": top["a"], "b": top["b"], "c": round(top["c"] - t_perp * (1 + s * s) ** 0.5, 2)}


def _footprint_pts(roof: dict) -> list[tuple[float, float]]:
    pts = [tuple(map(float, q)) for q in roof.get("footprint") or []]
    for p in roof.get("planes") or []:
        pts += [tuple(map(float, q)) for q in p.get("footprint") or []]
    return pts


def _side_of(a: float, b: float) -> str | None:
    """下る向き (-a, -b) → 低い側（東西南北。斜めは None）。"""
    import math
    ang = math.degrees(math.atan2(-b, -a)) % 360
    for name, center in (("E", 0), ("N", 90), ("W", 180), ("S", 270)):
        if min(abs(ang - center), 360 - abs(ang - center)) <= 10:
            return name
    return None


def _reshape_roof(roof: dict, field: str, to) -> None:
    """屋根の形を直す（面の式・高さの範囲・名札を計算し直す。入れ子は作り直す）。外れたら日本語の ValueError。"""
    t_perp = _thickness_perp(roof)
    z_low, z_high = float(roof["top_z_range_mm"][0]), float(roof["top_z_range_mm"][1])
    planes = roof.get("planes") or []
    multi = len(planes) > 1
    if field == "eave_tip_mm":
        _shift_roof(roof, float(to) - z_low)
        return
    if field == "pitch":
        slopes = [(p["a"] ** 2 + p["b"] ** 2) ** 0.5 for p in _all_planes(roof)]
        if min(slopes) < 1e-4:
            raise ValueError("平らな面がある屋根は、表では勾配を変えられません")
        if max(slopes) - min(slopes) > 0.005:
            raise ValueError("面ごとに勾配が違う屋根は、表では勾配を変えられません（囲んで直すか、glb を直してください）")
        k = float(to) / 10.0 / slopes[0]
        def scaled(tp: dict) -> dict:    # 軒先（いちばん低い所）の高さを固定して、高さを比例で伸ばす（棟・隅棟の平面の位置は変わらない）
            return {"a": tp["a"] * k, "b": tp["b"] * k, "c": z_low + k * (float(tp["c"]) - z_low)}
        roof["top_plane"], roof["bottom_plane"] = _with_bottom(scaled(roof["top_plane"]), t_perp)
        if multi:
            new = []
            for pl in planes:
                top, bottom = _with_bottom(scaled(pl["top_plane"]), t_perp)
                new.append({**pl, "top_plane": top, "bottom_plane": bottom, "pitch": [round(float(to), 2), 10]})
            roof["planes"] = new
        roof["top_z_range_mm"] = [_num(z_low), _num(round(z_low + k * (z_high - z_low)))]
        roof["pitch"] = [round(float(to), 2), 10]
        roof["slope"] = round(float(to) / 10.0, 4)
        return
    if field == "low_side":
        if multi:
            raise ValueError("面が複数の屋根（切妻・寄棟など）は、表では向きを変えられません")
        s = (roof["top_plane"]["a"] ** 2 + roof["top_plane"]["b"] ** 2) ** 0.5
        if s < 1e-4:
            raise ValueError("平らな屋根には低い側がありません")
        xs = [x for x, _ in _footprint_pts(roof)]
        ys = [y for _, y in _footprint_pts(roof)]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        top = {"S": {"a": 0.0, "b": s, "c": z_low - s * y0}, "N": {"a": 0.0, "b": -s, "c": z_low + s * y1},
               "W": {"a": s, "b": 0.0, "c": z_low - s * x0}, "E": {"a": -s, "b": 0.0, "c": z_low + s * x1}}[to]
        roof["top_plane"], roof["bottom_plane"] = _with_bottom(top, t_perp)
        zs = [_plane_at(roof["top_plane"], x, y) for x, y in _footprint_pts(roof)]
        roof["top_z_range_mm"] = [_num(round(min(zs))), _num(round(max(zs)))]
        roof["low_side"] = to
        roof.pop("low_side_assumption", None)
        roof.pop("eave_with_gutter_mm", None)              # 樋の位置（どの辺にあったか）は向きを変えると合わなくなる
        roof.pop("gutter_note", None)


def _follow_roof(building: dict, roof: dict, old_bottom, new_bottom) -> None:
    """屋根まで届いていた壁の上端を、屋根の下の面の動きに合わせる（数値の高さの外壁・単独の壁、屋根までの壁の glb の上端）。"""
    from shapely.geometry import Point, Polygon
    fp = roof.get("footprint")
    area = Polygon(fp).buffer(300) if fp else None
    for w in building.get("walls", []):
        if w.get("kind") not in ("exterior", "standalone") or "start" not in w:
            continue
        if roof.get("level") and w.get("level") and w["level"] != roof["level"]:
            continue
        mx, my = (float(w["start"][0]) + float(w["end"][0])) / 2, (float(w["start"][1]) + float(w["end"][1])) / 2
        if area is not None and not area.contains(Point(mx, my)):
            continue
        ob, nb = _plane_at(old_bottom, mx, my), _plane_at(new_bottom, mx, my)
        h = w.get("height")
        if isinstance(h, (int, float)) and not isinstance(h, bool) and float(h) >= ob - REACH_ROOF_MM:
            w["height"] = _num(round(float(h) + nb - ob))
        if h == "to_roof" and isinstance(w.get("top_glb_mm"), (int, float)):
            w["top_glb_mm"] = _num(round(float(w["top_glb_mm"]) + nb - ob))


def _apply_roof_geom(b: dict, obj: dict, ov: dict) -> dict:
    """屋根の形の修正を当てる（屋根とその写しの両方。屋根まで届いていた壁もついてくる）。"""
    from core.roof_geom import surfaces
    check_roof_value(ov["field"], ov["to"])
    before = get_field(obj, ov["field"])
    old_bottom = surfaces(obj)[0]
    twin = _roof_twin(b, ov["target"])
    for r in [obj] + ([twin] if twin is not None and twin is not obj else []):
        _reshape_roof(r, ov["field"], ov["to"])
    _follow_roof(b, obj, old_bottom, surfaces(obj)[0])
    status = "applied" if ("from" not in ov or _same(before, ov["from"])) else "applied_changed"
    r = {"id": ov["id"], "status": status}
    if status == "applied_changed":
        r["reason"] = f"自動の値が前回（{ov['from']}）から {before} に変わっていた"
    return r


def level_problems(building: dict, rules: dict) -> dict[tuple, str]:
    """階の高さが組み立てられる範囲か、家全体を見て調べる。{(階, 種類): 日本語の説明}。
    修正を足すときは、足す前になかった問題だけを断る（もともとある食い違いで直せなくならないように）。"""
    m = rules["model"]
    scale = float(m["scale"])
    min_feat = float(m["min_feature_mm"]) * scale
    plate = float(m.get("upper_floor_plate_mm", 3.0)) * scale
    default_ceiling = float(rules["levels"]["ceiling_height_mm"])
    levels = building.get("levels") or []
    ftop = float((building.get("foundation") or {}).get("top_mm", (building.get("ground") or {}).get("foundation_top_mm", 0)))
    roofs = _roofs_with_level(building)
    out = {}
    for i, lv in enumerate(levels):
        name = lv.get("name")
        ja = LEVEL_JA.get(name, name)
        fl = float(lv["fl_mm"])
        ceil = float(lv.get("ceiling_mm", default_ceiling))
        if i == 0 and not ftop + min_feat <= fl <= FL_MAX_MM:
            out[(name, "fl")] = f"1階の床の高さは、基礎の上（{ftop:.0f}mm）より {min_feat:.0f}mm 以上高く、{FL_MAX_MM:.0f}mm 以下にしてください"
        heads = [float(o["head_fl_mm"]) for o in building.get("openings", [])
                 if o.get("level") == name and isinstance(o.get("head_fl_mm"), (int, float))]
        if heads and ceil < max(heads) - 1:
            out[(name, "heads")] = f"{ja}の天井（FL+{ceil:.0f}）が、窓・ドアの上端（いちばん高い FL+{max(heads):.0f}）より低くなります"
        if i + 1 < len(levels):
            nxt = float(levels[i + 1]["fl_mm"])
            if fl + ceil > nxt - plate + 1:
                out[(name, "upper")] = (f"{ja}の天井（{fl + ceil:.0f}mm）が、上の階の床板（{nxt - plate:.0f}mm から）にぶつかります。"
                                        f"階高（{nxt - fl:.0f}mm）を広げるか、天井を下げてください")
        lows = [v for r, ri in roofs if ri == i for v in [_roof_bottom_min(r)] if v is not None]
        if lows and fl + ceil + min_feat > min(lows) + 1:
            out[(name, "roof")] = (f"{ja}の天井（{fl + ceil:.0f}mm）が屋根の下の面（いちばん低い所 {min(lows):.0f}mm）に近すぎます。"
                                   f"最小の太さ {min_feat:.0f}mm 以上あけてください")
    return out


def _roof_twin(building: dict, target: str) -> dict | None:
    """大屋根の2つの写し（上の段の roof と roofs の main）のうち、もう片方。
    平屋の組み立て・要約・平面図は roof を、2階建ての組み立ては roofs を読むので、両方に同じ修正を当てる。"""
    roofs = building.get("roofs") or []
    if target == "roof":
        main = [r for r in roofs if r.get("kind") == "main"] or (roofs if len(roofs) == 1 else [])
        return main[0] if main else None
    if target.startswith("Y") and isinstance(building.get("roof"), dict):
        r = next((x for x in roofs if x.get("id") == target), None)
        if r is not None and (r.get("kind") == "main" or len(roofs) == 1):
            return building["roof"]
    return None


def _write(obj: dict, field: str, to) -> None:
    if field.startswith(EAVE_PREFIX) and isinstance(obj.get("eave_mm"), dict):
        obj.setdefault("eave_glb_mm", dict(obj["eave_mm"]))   # 直す前の glb の軒の出を控える（組み立てで差を使う）
    _set_field(obj, field, to)


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= TOL
    return a == b


def _fl_of(building: dict, obj: dict) -> float | None:
    """開口の階の FL（head_mm を変えたときに head_fl_mm もそろえるため）。"""
    wall = _find(building, obj.get("wall", "")) if obj.get("wall") else None
    level = (wall or {}).get("level")
    lv = next((lv for lv in building.get("levels", []) if lv["name"] == level), None)
    return float(lv["fl_mm"]) if lv else None


def _sync_derived(building: dict, obj: dict, field: str) -> None:
    """関係する値を一緒に直す（開口の絶対の高さ ⇔ FL からの高さ）。"""
    pairs = {"head_mm": "head_fl_mm", "sill_mm": "sill_fl_mm"}
    rev = {v: k for k, v in pairs.items()}
    fl = _fl_of(building, obj)
    if fl is None:
        return
    if field in pairs and pairs[field] in obj:
        obj[pairs[field]] = round(obj[field] - fl)
    elif field in rev and rev[field] in obj:
        obj[rev[field]] = round(obj[field] + fl)


def apply(building: dict, items: list[dict]) -> tuple[dict, list[dict]]:
    """修正を順に当てた building の写しと、1件ずつの結果を返す。元の building は変えない。

    結果の status：applied（当てた）/ applied_changed（当てたが、自動の値が前回から変わっていた）/
    failed（当てられない。部材がない・項目がない・計算できない）
    1件が当てられなくても、残りは当てる（壊れた修正が残っても画面が開けるように）。
    """
    b = copy.deepcopy(building)
    results = []
    for ov in items:
        if ov.get("target", "").startswith(LEVEL_PREFIX) or ov.get("field") in ROOF_GEOM_FIELDS:
            # 階の高さ・屋根の形はたくさんの値を計算し直すので、途中で失敗しても家が半端に残らないように写しで当てる
            trial = copy.deepcopy(b)
            try:
                r = _apply_one(trial, ov)
            except (KeyError, TypeError, ValueError) as e:
                r = {"id": ov["id"], "status": "failed", "reason": f"当てられなかった（{e}）"}
            if r["status"] != "failed":
                b = trial
            results.append(r)
            continue
        try:
            results.append(_apply_one(b, ov))
        except (KeyError, TypeError, ValueError) as e:
            results.append({"id": ov.get("id"), "status": "failed", "reason": f"当てられなかった（{e}）"})
    b.setdefault("applied_overrides", [r for r in results])
    return b, results


def _apply_one(b: dict, ov: dict) -> dict:
    """修正を1件、b に当てる（b を書き換える）。結果を返す。"""
    obj = _find(b, ov["target"])
    if obj is None:
        return {"id": ov["id"], "status": "failed", "reason": f"{ov['target']} が見つからない（作り直しでなくなった可能性）"}
    if ov["field"] == DELETE:
        coll = COLLECTIONS.get(ov["target"][:1]) if ov["target"][:1] in DELETABLE else None
        if coll is None:
            return {"id": ov["id"], "status": "failed", "reason": f"{ov['target']} は取り除けない部材"}
        b[coll] = [x for x in b[coll] if x.get("id") != ov["target"]]
        if coll == "walls":  # 取り除いた壁にある開口も取り除く
            b["openings"] = [o for o in b.get("openings", []) if o.get("wall") != ov["target"]]
        return {"id": ov["id"], "status": "applied"}
    if not has_field(obj, ov["field"]):
        return {"id": ov["id"], "status": "failed", "reason": f"{ov['target']} に {ov['field']} という項目がない"}
    if ov["field"] in ROOF_GEOM_FIELDS and _is_roof(obj):
        return _apply_roof_geom(b, obj, ov)
    if ov["target"].startswith(LEVEL_PREFIX):
        if ov["field"] not in LEVEL_FIELDS:
            return {"id": ov["id"], "status": "failed", "reason": f"階の {ov['field']} は直せない（直せるのは床の高さと天井の高さ）"}
        check_value(ov["field"], ov["to"], ov["target"])          # 数値でない値は当てない（ValueError → 当てられない）
    before = get_field(obj, ov["field"])
    _write(obj, ov["field"], ov["to"])
    twin = _roof_twin(b, ov["target"])
    if twin is not None and has_field(twin, ov["field"]):
        _write(twin, ov["field"], ov["to"])
    _sync_derived(b, obj, ov["field"])
    if ov["target"].startswith(LEVEL_PREFIX):
        _level_changed(b, obj, ov["field"], before, ov["to"])
    status = "applied" if ("from" not in ov or _same(before, ov["from"])) else "applied_changed"
    r = {"id": ov["id"], "status": status}
    if status == "applied_changed":
        r["reason"] = f"自動の値が前回（{ov['from']}）から {before} に変わっていた"
    return r


def _company_rules(building: dict) -> dict:
    from core.rules import load_rules           # 使うときだけ読む（会社ルールの読み込みは overrides に依存しない）
    return load_rules((building.get("source") or {}).get("company_rules", "jisha"))


def add(alias: str, building: dict, target: str, field: str, to, value_source: str, reason: str,
        origin: str = "human:table", approved_by: str = "human", rules: dict | None = None) -> dict:
    """修正を1件足して保存する。from には、いまの値（修正を当てる前の値）を入れる。
    保存の前に、いままでの修正＋この1件を写しに当てて確かめる（当てられない・家として成り立たない値は断る）。"""
    items = load(alias)
    current, _ = apply(building, items)
    obj = _find(current, target)
    if obj is None:
        raise KeyError(f"{target} が見つかりません。")
    if field == DELETE and target[:1] not in DELETABLE:
        raise KeyError(f"{target} は取り除けません。")
    if field != DELETE and not has_field(obj, field):
        raise KeyError(f"{target} に {field} という項目がありません。")
    if target.startswith(LEVEL_PREFIX) and field not in LEVEL_FIELDS:
        raise KeyError(f"階の {field} は直せません（直せるのは床の高さ fl_mm と天井の高さ ceiling_mm）。")
    if field != DELETE:
        check_value(field, to, target)
    if field in ROOF_GEOM_FIELDS and _is_roof(obj):
        check_roof_value(field, to)
    n = max([int(x["id"][2:]) for x in items if x["id"][2:].isdigit()] + [0]) + 1
    ov = {"id": f"OV{n}", "target": target, "field": field,
          "from": None if field == DELETE else get_field(obj, field), "to": None if field == DELETE else to,
          "value_source": value_source, "reason": reason, "origin": origin, "approved_by": approved_by,
          "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    trial, results = apply(building, items + [ov])
    if results[-1]["status"] == "failed":
        raise ValueError(results[-1].get("reason", "当てられない修正です"))
    if target.startswith(LEVEL_PREFIX) or (field in ROOF_GEOM_FIELDS and _is_roof(obj)):
        rules = rules or _company_rules(building)
        before = level_problems(current, rules)
        new = [msg for key, msg in level_problems(trial, rules).items() if key not in before]
        if new:
            raise ValueError(new[0])
    items.append(ov)
    save(alias, items)
    return ov


def undo(alias: str, override_id: str) -> dict:
    """修正を1件取り消す（記録から外す）。取り消した1件を返す。"""
    items = load(alias)
    hit = next((x for x in items if x["id"] == override_id), None)
    if hit is None:
        raise KeyError(f"{override_id} という修正はありません。")
    save(alias, [x for x in items if x["id"] != override_id])
    return hit
