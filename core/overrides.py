"""人が承認した修正（overrides.json）の記録・当て直し・取り消し（SPEC §6・§9-1）。

- 自動で作った値（building.json）と、人が承認した修正（overrides.json）は分けて持つ。
- 修正は1件ずつ記録し、1件ずつ取り消せる。
- 作り直したあとも、修正を上から当て直す。当て直せない修正（部材がなくなった等）は一覧に出す。
- 数値は人か、コードが出どころ（value_source）から計算した値。Claude は数値を作らない（SPEC §2-4）。

1件の形（SPEC §6）:
{"id": "OV7", "target": "O1", "field": "head_mm", "from": 3024, "to": 2824,
 "value_source": "jww:elevation:dim#40", "reason": "...", "origin": "finding:F3", "approved_by": "human", "at": "..."}
field に "_delete" を指定すると、その部材を取り除く（to は使わない）。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

from core.paths import out_dir

COLLECTIONS = {"W": "walls", "O": "openings", "R": "rooms", "P": "porches", "S": "porch_soffits"}
DELETE = "_delete"
TOL = 0.5  # 「前回の自動の値」と比べるときの許容差（mm）


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
            coll = COLLECTIONS.get(ov["target"][:1])
            b[coll] = [x for x in b[coll] if x.get("id") != ov["target"]]
            if coll == "walls":  # 取り除いた壁にある開口も取り除く
                b["openings"] = [o for o in b.get("openings", []) if o.get("wall") != ov["target"]]
            results.append({"id": ov["id"], "status": "applied"})
            continue
        if ov["field"] not in obj:
            results.append({"id": ov["id"], "status": "failed", "reason": f"{ov['target']} に {ov['field']} という項目がない"})
            continue
        before = obj[ov["field"]]
        obj[ov["field"]] = ov["to"]
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
    if field != DELETE and field not in obj:
        raise KeyError(f"{target} に {field} という項目がありません。")
    n = max([int(x["id"][2:]) for x in items if x["id"][2:].isdigit()] + [0]) + 1
    ov = {"id": f"OV{n}", "target": target, "field": field,
          "from": None if field == DELETE else obj[field], "to": None if field == DELETE else to,
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
