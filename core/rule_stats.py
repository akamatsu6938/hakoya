"""会社ルールの言語化の集計（コードだけ。Claude は使わない）。

組み立て済みの物件（building.json）から値を集め、「会社で同じもの」（会社ルールにして、困ったときの予防に使う）と
「物件ごとに違うもの」（ルールにせず、物件ごとに glb から作る）に分ける（作者 11/3）。
- ルールにそろえた後の値は数えない（数えると「いつもルールどおり」に見えてしまう）。
  ドアの上端は glb_original（そろえる前の glb の値）、壁の厚みは測った値を使う。
- 判定：same（会社で同じ）／varies（物件ごとに違う）／few（件数が足りない：2件未満）／info（参考）。
  会社ルールと比べられる項目には rule_value・rule_match を付ける。
- 数値と一致の文（「2件中2件で 480」）はすべてここで作る。Claude は文を読んで言葉にするだけ。
"""
from __future__ import annotations

import collections
import json
import re

from core import overrides as ov
from core.paths import aliases, out_dir
from core.rules import LEARNABLE, load_learned, load_rules, rule

SAME_SHARE = 0.6        # この割合以上の物件で同じ値なら「会社で同じ」
FROM_GLB = "from_glb"   # 会社ルールで「物件ごとに glb の値を使う」と決めてある項目の印
DOOR_TOL_MM = 50.0      # ドアの上端はこの差まで同じとみなす（組み立てでそろえる幅と同じ）
DOOR_TOP_RE = re.compile(r"^door_top: glb の上端 FL\+(-?\d+)")

# 項目：id → (名前, 会社ルールの項目, 比べ方, 書き方)。比べ方：equal / near / choices / None（参考）
STATS = {
    "levels.foundation_top": ("基礎の天端（GL から）", "levels.gl_to_foundation_top_mm", "equal", "mm"),
    "levels.fl1": ("1階の床（GL から）", "levels.gl_to_1fl_mm", "equal", "mm"),
    "levels.ceiling": ("天井高", "levels.ceiling_height_mm", "equal", "mm"),
    "levels.story_height": ("階高（2階建て）", "levels.story_height_choices_mm", "choices", "mm"),
    "walls.exterior_measured": ("外壁の厚み（glb で測った値）", "walls.exterior_thickness_mm", "equal", "mm"),
    "walls.interior_gap": ("内壁の仕上げ面の間（参考。模型は会社ルールの厚みで作り直す）", None, None, "mm"),
    "openings.door_head": ("室内のドアの上端（そろえる前の glb の値）", "openings.door_top_fl_mm", "near", "fl"),
    "openings.window_head": ("窓の上端", "openings.window_top_default_fl_mm", "equal", "fl"),
    "openings.entrance_head": ("玄関ドアの上端（参考）", None, None, "fl"),
    "openings.door_from_floor": ("床から浮いた戸（glb の始まり）", None, None, "fl"),
    "roofs.shape": ("屋根の形", "roof.default_shape", "equal", "text"),
    "roofs.pitch": ("屋根の勾配", "roof.pitch", "equal", "pitch"),
    "roofs.eave": ("軒の出（樋を除く、いちばん出ている辺）", "roof.eave", "equal", "mm"),
    "roofs.thickness": ("屋根の厚み", "roof.build_up_mm", "equal", "mm"),
}
# 物件ごとに違って当然の項目（違っても「いつもと違う」と知らせない。会社ルールの値は予備）
VARIABLE_BY_NATURE = ("openings.window_head", "roofs.pitch", "levels.story_height")
SHAPE_JA = {"shed": "片流れ", "gable": "切妻", "hip": "寄棟", "multi": "複数の面", "flat": "陸屋根", "unknown": "不明"}
EXCEPTION_JA = {
    "agari_kamachi": "上がり框の位置に壁を作らない（框・靴箱などの造作は模型に入れない）",
    "back_to_back_exterior_faces": "背中合わせの外壁の面は1枚の内壁として作る",
    "door_above_floor": "床から浮いて始まる室内の戸は、床からのドアにする",
    "stairs_area": "階段のまわりの壁・手すりは造作なので作らない",
    "upper_floor_overhang": "2階のはね出しは、1階の部品に厚みのある塊として作る",
}


def fmt(value, how: str) -> str:
    if how == "fl":
        return f"FL+{value:g}"
    if how == "pitch":
        return f"{value:g}/10"
    if how == "text":
        return SHAPE_JA.get(value, str(value))
    return f"{value:g}"


def _door_head(o: dict) -> float:
    if (o.get("glb_original") or {}).get("head_fl_mm") is not None:
        return float(o["glb_original"]["head_fl_mm"])
    for a in o.get("assumption", []):          # 古い building.json：注記の文から読む
        m = DOOR_TOP_RE.match(a)
        if m:
            return float(m.group(1))
    return float(o["head_fl_mm"])


def observe(b: dict) -> dict[str, list[tuple]]:
    """1つの物件の値：項目 → [(値, どこの値か)]。"""
    out: dict[str, list[tuple]] = collections.defaultdict(list)
    ground, levels = b.get("ground", {}), b.get("levels", [])
    gl = float(ground.get("gl_mm", 0))
    if not ground.get("gl_assumption"):            # 敷地がなく GL を仮に決めた物件は、GL からの高さを数えない
        if ground.get("foundation_top_mm") is not None:
            out["levels.foundation_top"].append((round(float(ground["foundation_top_mm"]) - gl), "基礎"))
        if levels:
            out["levels.fl1"].append((round(float(levels[0]["fl_mm"]) - gl), levels[0]["name"]))
    for lv in levels:
        if lv.get("ceiling_mm"):
            out["levels.ceiling"].append((round(float(lv["ceiling_mm"])), lv["name"]))
    for lo, hi in zip(levels, levels[1:]):
        out["levels.story_height"].append((round(float(hi["fl_mm"]) - float(lo["fl_mm"])), f"{lo['name']}→{hi['name']}"))
    stats = b.get("wall_stats_by_level") or ({levels[0]["name"]: b["wall_stats"]} if levels and b.get("wall_stats") else {})
    for name, s in stats.items():
        if s.get("exterior_thickness_measured_mm"):
            out["walls.exterior_measured"].append((round(float(s["exterior_thickness_measured_mm"])), name))
        if s.get("interior_gap_measured_mm"):
            out["walls.interior_gap"].append((round(float(s["interior_gap_measured_mm"])), name))
    for o in b.get("openings", []):
        if o["kind"] == "door":
            out["openings.door_head"].append((round(_door_head(o)), o["id"]))
            if (o.get("glb_original") or {}).get("sill_fl_mm") is not None:
                out["openings.door_from_floor"].append((round(float(o["glb_original"]["sill_fl_mm"])), o["id"]))
            elif any(a.startswith("door_from_floor:") for a in o.get("assumption", [])):
                m = re.search(r"FL\+(\d+)", next(a for a in o["assumption"] if a.startswith("door_from_floor:")))
                if m:
                    out["openings.door_from_floor"].append((int(m.group(1)), o["id"]))
        elif o["kind"] == "window":
            out["openings.window_head"].append((round(float(o["head_fl_mm"])), o["id"]))
        elif o["kind"] == "entrance_door":
            out["openings.entrance_head"].append((round(float(o["head_fl_mm"])), o["id"]))
    for r in b.get("roofs") or ([b["roof"]] if b.get("roof") else []):
        rid = r.get("id", "屋根")
        if r.get("shape"):
            out["roofs.shape"].append((r["shape"], rid))
        if r.get("pitch"):
            out["roofs.pitch"].append((float(r["pitch"][0]), rid))
        if r.get("eave_mm"):
            biggest = max(abs(float(v)) for v in r["eave_mm"].values())
            out["roofs.eave"].append((int(round(biggest / 10.0)) * 10, rid))
        if r.get("thickness_mm"):
            out["roofs.thickness"].append((round(float(r["thickness_mm"])), rid))
    return out


def _mode(values: list, near: float | None = None):
    """いちばん多い値（同じ数なら大きい方）。near を渡すとその差までを同じとみなして数える。"""
    counts = collections.Counter(values)
    if near:
        def support(v):
            return sum(c for u, c in counts.items() if abs(u - v) <= near)
        best = max(counts, key=lambda v: (support(v), counts[v], v))
        return best, support(best)
    best = max(counts, key=lambda v: (counts[v], v if not isinstance(v, str) else 0))
    return best, counts[best]


def _same(a, b, compare: str | None) -> bool:
    if compare == "near":
        return abs(float(a) - float(b)) <= DOOR_TOL_MM
    return a == b


def entries(company: str) -> list[tuple[str, dict]]:
    """その会社の組み立て済みの物件（仮名・building.json）。"""
    out = []
    for a in aliases():
        p = out_dir(a) / "building.json"
        if not p.exists():
            continue
        b = json.loads(p.read_text(encoding="utf-8"))
        if b.get("source", {}).get("company_rules", "jisha") == company:
            out.append((a, b))
    return out


def compute(items: list[tuple[str, dict]], rules: dict, overrides_of: dict[str, list] | None = None) -> list[dict]:
    """集計。items：[(仮名, building.json)]。"""
    per: dict[str, list[dict]] = collections.defaultdict(list)
    exc: dict[str, list[str]] = collections.defaultdict(list)
    for alias, b in items:
        for sid, vals in observe(b).items():
            name, rkey, compare, how = STATS[sid]
            near = DOOR_TOL_MM if compare == "near" else None
            value, count = _mode([v for v, _ in vals], near)
            elements = [{"value": v, "count": c, "where": [w for u, w in vals if u == v][:12]}
                        for v, c in collections.Counter(v for v, _ in vals).most_common()]
            per[sid].append({"alias": alias, "value": value, "count": count, "total": len(vals), "elements": elements})
        for kind in sorted({e["kind"] for e in b.get("glb_exceptions", [])}):
            exc[kind].append(alias)
    out = []
    for sid, (name, rkey, compare, how) in STATS.items():
        props = per.get(sid, [])
        if not props:
            continue
        near = DOOR_TOL_MM if compare == "near" else None
        mode, mode_count = _mode([p["value"] for p in props], near)
        n = len(props)
        dist = collections.defaultdict(list)
        for p in props:
            dist[p["value"]].append(p["alias"])
        item = {"id": sid, "label": name, "rule_key": rkey, "compare": compare, "unit": how,
                "per_property": props, "distribution": [{"value": v, "properties": ps} for v, ps in
                                                        sorted(dist.items(), key=lambda kv: -len(kv[1]))],
                "n": n, "n_all": len(items), "mode": mode, "mode_count": mode_count}
        if rkey is None:
            item["status"] = "info"
        elif n < 2:
            item["status"] = "few"
        elif mode_count / n >= SAME_SHARE and (mode_count == n or sid not in VARIABLE_BY_NATURE):
            item["status"] = "same"
        else:
            item["status"] = "varies"
        if rkey:
            try:
                rv = rule(rules, rkey)
            except KeyError:
                rv = None
            item["rule_value"] = rv
            if rv == FROM_GLB:          # 会社ルールが「glb の値を使う」の項目は比べない
                pass
            elif rv is not None:
                if compare == "choices":
                    item["rule_match"] = all(p["value"] in rv for p in props)
                else:
                    item["rule_match"] = _same(mode, rv, compare)
        item["agreement"] = agreement(item)
        out.append(item)
    for kind, props in sorted(exc.items()):
        n = len(items)
        out.append({"id": f"exceptions.{kind}", "label": f"glb の例外：{EXCEPTION_JA.get(kind, kind)}", "rule_key": None,
                    "compare": None, "unit": "count", "per_property": [{"alias": a} for a in props],
                    "distribution": [], "n": n, "mode": None, "mode_count": len(props),
                    "status": "same" if n >= 2 and len(props) / n >= SAME_SHARE else ("few" if n < 2 else "info"),
                    "agreement": f"{n}件中{len(props)}件（{'・'.join(props)}）"})
    for key, props in sorted(_corrections(items, overrides_of).items()):
        n = len(items)
        out.append({"id": f"corrections.{key}", "label": f"人の修正：{key}", "rule_key": None, "compare": None,
                    "unit": "count", "per_property": [{"alias": a} for a in props], "distribution": [], "n": n,
                    "mode": None, "mode_count": len(props),
                    "status": "same" if len(props) >= 2 else "info",
                    "agreement": f"{n}件中{len(props)}件で同じ種類の修正（{'・'.join(props)}）"})
    return out


def _corrections(items: list[tuple[str, dict]], overrides_of: dict[str, list] | None) -> dict[str, list[str]]:
    """人の修正（overrides）を「部材の種類・項目」でまとめる。同じ修正が2物件以上 → 会社ルールの候補（SPEC §9-1）。"""
    out: dict[str, list[str]] = collections.defaultdict(list)
    for alias, b in items:
        ovs = (overrides_of or {}).get(alias)
        if ovs is None:
            try:
                ovs = ov.load(alias)
            except Exception:
                ovs = []
        kinds = {x["id"]: x.get("kind", "") for coll in ("walls", "openings", "roofs") for x in b.get(coll, [])}
        for o in ovs:
            kind = "level" if str(o["target"]).startswith("level:") else kinds.get(o["target"], o["target"][:1])
            key = f"{kind}.{o['field']}"
            if alias not in out[key]:
                out[key].append(alias)
    return out


def agreement(item: dict) -> str:
    """一致の文（コードが書く。Claude はこの文を根拠に引く）。"""
    how, n = item["unit"], item["n"]
    props = item["per_property"]

    def detail(p):
        if p["total"] > 1:
            return f"{p['alias']} {fmt(p['value'], how)}（{p['total']}か所中{p['count']}か所）"
        return f"{p['alias']} {fmt(p['value'], how)}"

    rv = item.get("rule_value")
    if rv == FROM_GLB:
        rule_txt = "会社ルール：物件ごとに glb の値を使う"
    elif item["compare"] == "choices" and rv is not None:
        rule_txt = f"会社ルールの選択肢 {'・'.join(fmt(v, how) for v in rv)}"
    elif rv is not None:
        rule_txt = f"会社ルール {fmt(rv, how)}"
    else:
        rule_txt = ""
    match = item.get("rule_match")
    if item["status"] == "varies" and rv is not None and rv != FROM_GLB:
        tail = f"（{rule_txt} は予備の値）"           # 物件ごとに違う項目では、ルールとの一致は意味がない
    else:
        tail = f"（{rule_txt}{'と一致' if match else 'と違う' if match is not None else ''}）" if rule_txt else ""
    only = "・".join(p["alias"] for p in props)
    if n < item.get("n_all", n):                    # 一部の物件にしかない項目
        return f"{only} だけ：" + "、".join(detail(p) for p in props) + tail
    if item["status"] == "few":
        return f"{n}件だけ：" + "、".join(detail(p) for p in props) + tail
    if item["status"] in ("same", "info") and item["mode_count"] == n:
        near = "（50mm 以内）" if item["compare"] == "near" else ""
        text = f"{n}件中{n}件で {fmt(item['mode'], how)}{near}{tail}"
        others = sorted({e["value"] for p in props for e in p["elements"] if e["value"] != item["mode"]})
        if others and item["compare"] == "near":
            k = sum(e["count"] for p in props for e in p["elements"] if e["value"] != item["mode"])
            text += f"。glb では {'・'.join(fmt(v, how) for v in others)} が{k}か所"
        return text
    if item["status"] == "same":
        return f"{n}件中{item['mode_count']}件で {fmt(item['mode'], how)}{tail}。ほか：" + "、".join(
            detail(p) for p in props if not _same(p["value"], item["mode"], item["compare"]))
    return "物件ごとに違う：" + "、".join(detail(p) for p in props) + tail


def collect(company: str) -> list[dict]:
    """その会社の全物件の集計（いまの会社ルール、学んだルールを含む、と比べる）。"""
    return compute(entries(company), load_rules(company))


def code_candidates(stats: list[dict], company: str) -> list[dict]:
    """AI なしの候補（コードが決まった形で出す）。承認・却下した案は出さない。"""
    learned = load_learned(company)
    done = {n.get("signature") for n in learned["notes"]} | {f"value:{k}" for k in learned["values"]} | \
        {r.get("signature") for r in learned["rejected"]}
    out = []
    for s in stats:
        if s["status"] == "same" and s.get("rule_key") in LEARNABLE and s.get("rule_match") is False \
                and s["compare"] != "choices":
            sig = f"value:{s['rule_key']}"
            if sig not in done:
                out.append({"kind": "value", "rule_key": s["rule_key"], "stat_ids": [s["id"]], "answer_ids": [],
                            "value_source": f"stat:{s['id']}:mode", "to": s["mode"],
                            "wording": f"{s['label']}は、会社ルールを {fmt(s['mode'], s['unit'])} にする（{s['agreement']}）",
                            "signature": sig, "origin": "code"})
        elif s["status"] == "varies" and s.get("rule_key") and s.get("rule_value") != FROM_GLB:
            sig = f"glb:{s['id']}"
            if sig not in done:
                out.append({"kind": "glb", "rule_key": s["rule_key"], "stat_ids": [s["id"]], "answer_ids": [],
                            "wording": f"{s['label']}は物件ごとに違うので、会社ルールで決め打ちせず glb の値を使う"
                                       f"（{s['agreement']}）",
                            "signature": sig, "origin": "code"})
        elif s["id"].startswith("exceptions.") and s["status"] == "same":
            kind = s["id"].split(".", 1)[1]
            sig = f"skill:{s['id']}"
            if kind in EXCEPTION_JA and sig not in done:
                out.append({"kind": "skill", "rule_key": None, "stat_ids": [s["id"]], "answer_ids": [],
                            "wording": f"{EXCEPTION_JA[kind]}（glb の例外。{s['agreement']}）",
                            "signature": sig, "origin": "code"})
    return out
