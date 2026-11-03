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


def has_field(obj: dict, field: str) -> bool:
    """項目があるか。"eave_mm.S" のような1段の入れ子も見る。"""
    head, _, tail = field.partition(".")
    if not tail:
        return field in obj
    return isinstance(obj.get(head), dict) and tail in obj[head]


def get_field(obj: dict, field: str):
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


def check_value(field: str, to) -> None:
    """値の範囲を確かめる（いまは軒の出だけ）。外れたら日本語の ValueError。"""
    if field.startswith(EAVE_PREFIX):
        lo, hi = EAVE_RANGE_MM
        if not isinstance(to, (int, float)) or not lo <= float(to) <= hi:
            raise ValueError(f"軒の出は {lo:.0f}〜{hi:.0f}mm の数値で入れてください（外壁の外の面から屋根の先まで、板金・樋を除く）")


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
    failed（当てられない。部材がない・項目がない）
    """
    b = copy.deepcopy(building)
    results = []
    for ov in items:
        obj = _find(b, ov["target"])
        if obj is None:
            results.append({"id": ov["id"], "status": "failed", "reason": f"{ov['target']} が見つからない（作り直しでなくなった可能性）"})
            continue
        if ov["field"] == DELETE:
            coll = COLLECTIONS.get(ov["target"][:1]) if ov["target"][:1] in DELETABLE else None
            if coll is None:
                results.append({"id": ov["id"], "status": "failed", "reason": f"{ov['target']} は取り除けない部材"})
                continue
            b[coll] = [x for x in b[coll] if x.get("id") != ov["target"]]
            if coll == "walls":  # 取り除いた壁にある開口も取り除く
                b["openings"] = [o for o in b.get("openings", []) if o.get("wall") != ov["target"]]
            results.append({"id": ov["id"], "status": "applied"})
            continue
        if not has_field(obj, ov["field"]):
            results.append({"id": ov["id"], "status": "failed", "reason": f"{ov['target']} に {ov['field']} という項目がない"})
            continue
        before = get_field(obj, ov["field"])
        _write(obj, ov["field"], ov["to"])
        twin = _roof_twin(b, ov["target"])
        if twin is not None and has_field(twin, ov["field"]):
            _write(twin, ov["field"], ov["to"])
        _sync_derived(b, obj, ov["field"])
        status = "applied" if ("from" not in ov or _same(before, ov["from"])) else "applied_changed"
        r = {"id": ov["id"], "status": status}
        if status == "applied_changed":
            r["reason"] = f"自動の値が前回（{ov['from']}）から {before} に変わっていた"
        results.append(r)
    b.setdefault("applied_overrides", [r for r in results])
    return b, results


def add(alias: str, building: dict, target: str, field: str, to, value_source: str, reason: str,
        origin: str = "human:table", approved_by: str = "human") -> dict:
    """修正を1件足して保存する。from には、いまの値（修正を当てる前の値）を入れる。"""
    items = load(alias)
    current, _ = apply(building, items)
    obj = _find(current, target)
    if obj is None:
        raise KeyError(f"{target} が見つかりません。")
    if field == DELETE and target[:1] not in DELETABLE:
        raise KeyError(f"{target} は取り除けません。")
    if field != DELETE and not has_field(obj, field):
        raise KeyError(f"{target} に {field} という項目がありません。")
    if field != DELETE:
        check_value(field, to)
    n = max([int(x["id"][2:]) for x in items if x["id"][2:].isdigit()] + [0]) + 1
    ov = {"id": f"OV{n}", "target": target, "field": field,
          "from": None if field == DELETE else get_field(obj, field), "to": None if field == DELETE else to,
          "value_source": value_source, "reason": reason, "origin": origin, "approved_by": approved_by,
          "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
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
