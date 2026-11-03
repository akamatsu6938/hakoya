"""アプリの Claude に渡す道具（SPEC §7-3）。SDK に依存しない普通の関数として作り、
Agent SDK（claude_code 接続）にも Messages API（api_key 接続）にも同じものをつなぐ。

道具：get_building / get_elements / render_view / get_drawing_region / run_checks / propose_override / ask_question

大事な決まり（SPEC §2-4）：Claude は数値を作らない。propose_override では「数値の出どころ」（value_source）だけを指定させ、
数値はここでコードが計算する。出どころ：
- "majority:<種類>"   同じ種類の壁（exterior / interior / standalone）の厚みの多数派
- "rule:<項目>"       会社ルールの値（例：rule:openings.door_top_fl_mm）
- "glb"              自動で作った元の値（人の修正を外した値）に戻す
- "number"           人が一言で書いた数値（囲んで直すで「900にしたい」など）。number に入れる。
                     囲んで直すでは、その数値が人の一言（と質問への答え）の中にあるかをコードが確かめる
- "jww:<ID>"         図面の寸法（図面の読み取りはまだ作っていないので、いまは使えない）
"""
from __future__ import annotations

import collections
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from core import overrides as ov
from core.paths import out_dir
from core.rules import load_rules, rule

VIEWS = ("S", "N", "E", "W", "plan", "plan2")    # plan2 は2階建ての2階の平面（平屋にはない）
EDITABLE = {"walls": ("thickness_mm",), "openings": ("width_mm", "offset_mm", "sill_fl_mm", "head_fl_mm"),
            # 屋根の軒の出（外壁の外の面から屋根の先まで、板金・樋を除く）。模型は板金の分をさらに外に足す
            "roofs": tuple(f"eave_mm.{s}" for s in "WESN")}
ROOF_KEYS = ("id", "kind", "level", "shape", "pitch", "low_side", "thickness_mm", "eave_mm", "eave_with_gutter_mm")


class ToolError(ValueError):
    """道具の使い方の誤り。Claude にそのまま日本語で返す。"""


@dataclass
class ToolContext:
    alias: str
    raw: dict                          # 自動で作った値（building.json）
    building: dict                     # 人の修正を当てたもの
    rules: dict
    questions: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    human_text: str | None = None      # 囲んで直すの人の一言（number の数値はこの中にあるものだけ）。検品では None

    @classmethod
    def load(cls, alias: str) -> "ToolContext":
        raw = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
        applied, _ = ov.apply(raw, ov.load(alias))
        return cls(alias=alias, raw=raw, building=applied, rules=load_rules(raw["source"].get("company_rules", "jisha")))

    @property
    def inspect_dir(self) -> Path:
        return out_dir(self.alias) / "inspect"


def _check_view(ctx: ToolContext, view: str) -> None:
    if view not in VIEWS:
        raise ToolError(f"view は {', '.join(VIEWS)} のどれかです")
    if view == "plan2" and len(ctx.building.get("levels", [])) < 2:
        raise ToolError("この家は平屋なので、plan2（2階の平面）はありません。平面は plan です")


def _find(b: dict, target: str) -> tuple[str, dict] | tuple[None, None]:
    for coll in ("walls", "openings", "roofs"):
        for x in b.get(coll, []):
            if x["id"] == target:
                return coll, x
    return None, None


# ---------------------------------------------------------------- 調べる道具

def get_building(ctx: ToolContext) -> dict:
    """家の要約：階・外形・壁と開口の数・屋根・会社ルールの主な値・未解決の所・glb の例外。"""
    b = ctx.building
    walls = collections.Counter(w["kind"] for w in b["walls"])
    ops = collections.Counter(o["kind"] for o in b.get("openings", []))
    r = b.get("roof", {})
    out = {
        "property": ctx.alias,
        "levels": [{k: lv[k] for k in ("name", "fl_mm", "ceiling_mm") if k in lv} for lv in b["levels"]],
        "foundation_top_mm": b["ground"]["foundation_top_mm"],
        "outer_size_mm": b.get("wall_stats", {}).get("outer_size_mm"),
        "walls": dict(walls), "openings": dict(ops),
        "roof": {"id": next((x.get("id") for x in b.get("roofs", []) if x.get("kind") == "main"), None),
                 **{k: r.get(k) for k in ("shape", "pitch", "low_side", "thickness_mm", "eave_mm", "eave_with_gutter_mm")},
                 # ★説明がないと「軒の出が glb（樋込み 146mm）より短い」と誤って指摘された（検品 3回目 F2）
                 "note": "eave_mm は glb の屋根板の出（外壁の外の面から屋根の先まで、板金・樋を除く）、eave_with_gutter_mm は軒樋を含めた出。"
                         "直すときは target=屋根の id・field=eave_mm.S のように辺を付ける。樋・破風・鼻隠しは模型では省く"
                         f"（include.gutter / fascia）。模型の屋根板は板金の分として4辺を model.roof_overhang_mm"
                         f"（{ctx.rules['model'].get('roof_overhang_mm', 0)}mm）出す。横から見ると軒裏が細い帯に見えるのは正しい"},
        "company_rules": {"walls": ctx.rules["walls"], "openings": ctx.rules["openings"],
                          "ceiling_height_mm": ctx.rules["levels"]["ceiling_height_mm"]},
        "unresolved": b.get("unresolved", []),
        "glb_exceptions": b.get("glb_exceptions", []),
        "skipped": b.get("skipped", []),
        "direction_note": "東西南北は glb の +Y を北とみなしている（推定）",
    }
    if len(b["levels"]) > 1:
        out["by_level"] = {lv["name"]: {
            "walls": dict(collections.Counter(w["kind"] for w in b["walls"] if w.get("level") == lv["name"])),
            "openings": dict(collections.Counter(o["kind"] for o in b.get("openings", []) if o.get("level") == lv["name"])),
            "outer_size_mm": b.get("wall_stats_by_level", {}).get(lv["name"], {}).get("outer_size_mm"),
            "floor_openings": [{"id": h.get("id"), "kind": h.get("kind"), "area_m2": h.get("area_m2")}
                               for h in lv.get("floor_openings", [])]} for lv in b["levels"]}
        out["roofs"] = [{k: x.get(k) for k in ("id", "kind", "level", "shape", "pitch", "low_side", "thickness_mm",
                                               "eave_mm", "eave_with_gutter_mm")} for x in b.get("roofs", [])]
        out["multi_note"] = ("2階建て。roof は大屋根（2階の上）、roofs は全部の屋根（下屋 lean_to を含む）。"
                             "部品は5つ（基礎・1階・下屋の屋根・2階・大屋根）。平面の図は1階が plan、2階が plan2")
    return out


def get_elements(ctx: ToolContext, view: str | None = None, kind: str | None = None, ids: list[str] | None = None) -> dict:
    """部材の一覧。view（S/N/E/W/plan/plan2）を渡すと、その面の図に写っている部材だけ。kind・ids でも絞れる。
    軒の番号（Y1:S など）は、その屋根（Y1）として返す。"""
    b = ctx.building
    if ids:
        ids = [i.split(":")[0] for i in ids]
    in_view = None
    if view:
        _check_view(ctx, view)
        p = ctx.inspect_dir / f"{view}.json"
        if p.exists():
            in_view = {e["id"]: e for e in json.loads(p.read_text(encoding="utf-8"))["elements"]}
    out = []
    for coll in ("walls", "openings", "roofs"):
        for x in b.get(coll, []):
            if ids and x["id"] not in ids:
                continue
            if kind and x["kind"] != kind:
                continue
            if in_view is not None and x["id"] not in in_view:
                continue
            if coll == "roofs":            # 屋根は外形などの長い座標を除いて渡す
                out.append({k: x.get(k) for k in ROOF_KEYS})
                continue
            item = {k: x[k] for k in x if k not in ("sources",)}
            if in_view is not None:
                item["bbox_px"] = in_view[x["id"]]["bbox_px"]
            out.append(item)
    return {"count": len(out), "elements": out}


def render_view(ctx: ToolContext, view: str, highlight: list[str] | None = None, source: str = "model") -> dict:
    """面の画像（PNG のパス）。source="model" は模型、"glb" は元の形。highlight の部材を赤い枠で強調する。"""
    _check_view(ctx, view)
    if source not in ("model", "glb"):
        raise ToolError('source は "model" か "glb" です')
    src = ctx.inspect_dir / f"{view}_{source}.png"
    if not src.exists():
        from core.render import render_all
        render_all(ctx.alias)
    if not highlight:
        return {"image_path": str(src), "view": view, "source": source}
    info = json.loads((ctx.inspect_dir / f"{view}.json").read_text(encoding="utf-8"))
    boxes = {e["id"]: e["bbox_px"] for e in info["elements"]}
    missing = [h for h in highlight if h not in boxes]
    img = Image.open(src).convert("RGB")
    d = ImageDraw.Draw(img)
    for h in highlight:
        if h in boxes:
            x0, y0, x1, y1 = boxes[h]
            d.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=(230, 0, 0), width=3)
    dst = ctx.inspect_dir / f"{view}_{source}_hl.png"
    img.save(dst)
    res = {"image_path": str(dst), "view": view, "source": source, "highlighted": [h for h in highlight if h in boxes]}
    if missing:
        res["not_in_view"] = missing
    return res


def get_drawing_region(ctx: ToolContext, view: str, bbox_px: list[int] | None = None) -> dict:
    """図面（jww）の同じ面の画像と寸法・注記。図面の読み取りはまだ作っていない。"""
    return {"available": False, "note": "図面（jww）の読み取りはまだ作っていません。glb と模型の見比べだけで判断し、"
                                        "図面が要る判断は ask_question で人に聞いてください。"}


def run_checks(ctx: ToolContext) -> dict:
    """コードの検査の結果（閉じた立体・離れた塊・造形範囲・glb との数の差など）。"""
    p = out_dir(ctx.alias) / "model" / "checks.json"
    if not p.exists():
        raise ToolError("まだ模型を書き出していません")
    s = json.loads(p.read_text(encoding="utf-8"))
    return {"checks": [c for c in s["checks"] if c["level"] != "情報"] or [{"level": "情報", "message": "問題は見つかっていません"}],
            "part_notes": {k: v["notes"] for k, v in s["parts"].items() if v["notes"]}}


# ---------------------------------------------------------------- 直す案と質問

def _value_from_source(ctx: ToolContext, coll: str, obj: dict, fld: str, value_source: str, number: float | None) -> float:
    """数値の出どころから、コードが数値を計算する（Claude に数値を作らせない）。"""
    if value_source == "number":
        if number is None:
            raise ToolError('value_source が "number" のときは、人が書いた数値を number に入れてください')
        if ctx.human_text is not None and not _number_in_text(float(number), ctx.human_text):
            raise ToolError(f"number（{number:g}）は人の一言にない数値です。number には人が書いた数値だけを使い、"
                            "なければ ask_question で人に聞いてください")
        return float(number)
    if value_source == "glb":
        _, raw = _find(ctx.raw, obj["id"])
        if raw is None or not ov.has_field(raw, fld):
            raise ToolError(f"{obj['id']} の自動の値（{fld}）が見つかりません")
        return float(ov.get_field(raw, fld))
    if value_source.startswith("majority:"):
        kind = value_source.split(":", 1)[1]
        if coll != "walls" or fld != "thickness_mm":
            raise ToolError("majority は壁の厚み（thickness_mm）にだけ使えます")
        vals = [w["thickness_mm"] for w in ctx.building["walls"] if w["kind"] == kind and w["id"] != obj["id"]]
        if not vals:
            raise ToolError(f"種類 {kind} のほかの壁がありません")
        return float(collections.Counter(vals).most_common(1)[0][0])
    if value_source.startswith("rule:"):
        try:
            return float(rule(ctx.rules, value_source.split(":", 1)[1]))
        except KeyError as e:
            raise ToolError(str(e).strip("'\""))
    if value_source.startswith("jww:"):
        raise ToolError("図面（jww）の寸法はまだ使えません。ask_question で人に聞いてください")
    raise ToolError(f"value_source「{value_source}」は使えません（majority:種類 / rule:項目 / glb / number）")


def _number_in_text(number: float, text: str) -> bool:
    """人の文章に、その数値が書いてあるか（全角の数字・カンマも扱う）。"""
    t = unicodedata.normalize("NFKC", text).replace(",", "")
    return any(abs(float(x) - number) < 1e-6 for x in re.findall(r"\d+(?:\.\d+)?", t))


def propose_override(ctx: ToolContext, target: str, field: str, value_source: str, reason: str,
                     number: float | None = None, finding: str | None = None) -> dict:
    """直す案を1件つくる（保存はしない。人が承認したら overrides に入る）。数値はコードが出どころから計算する。"""
    coll, obj = _find(ctx.building, target)
    if obj is None:
        raise ToolError(f"{target} という部材はありません")
    if field == ov.DELETE:
        prop = {"target": target, "field": field, "from": None, "to": None, "value_source": value_source,
                "reason": reason, "finding": finding}
    else:
        if field not in EDITABLE.get(coll, ()):
            raise ToolError(f"{target} で直せる項目は {', '.join(EDITABLE.get(coll, ()))} です")
        to = _value_from_source(ctx, coll, obj, field, value_source, number)
        now = ov.get_field(obj, field)
        if abs(to - float(now)) < 0.5:
            raise ToolError(f"{target} の {field} はすでに {now} です（直す必要がありません）")
        try:
            ov.check_value(field, to)
        except ValueError as e:
            raise ToolError(str(e))
        prop = {"target": target, "field": field, "from": now, "to": round(to, 1),
                "value_source": value_source if value_source != "number" else f"human:number:{number:g}",
                "reason": reason, "finding": finding}
    ctx.proposals.append(prop)
    return {"proposal": prop, "note": "人が承認すると修正として記録され、模型が組み直されます"}


def ask_question(ctx: ToolContext, question: str, options: list[str], evidence: str = "") -> dict:
    """ルールで決まらないことを人に聞く（SPEC §2-5）。選択肢は2〜3個。"""
    if not 2 <= len(options) <= 3:
        raise ToolError("選択肢は2〜3個にしてください（SPEC §5-2）")
    q = {"id": f"Q{len(ctx.questions) + 1}", "question": question, "options": options, "evidence": evidence}
    ctx.questions.append(q)
    return {"queued": q["id"], "note": "人の答えを待ちます。答えは rules/answers.jsonl に残ります"}


# ---------------------------------------------------------------- 道具の一覧（説明は Claude が読む）

TOOL_SPECS = {
    "get_building": {"fn": get_building, "description": "家の要約（階・外形・壁と開口の数・屋根・会社ルールの主な値・未解決の所・glb の例外）を返す。",
                     "schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "get_elements": {"fn": get_elements, "description": "部材（壁・開口）の一覧を返す。view を渡すとその面の図に写る部材だけ（画像上の範囲 bbox_px 付き）。",
                     "schema": {"type": "object", "properties": {
                         "view": {"type": "string", "enum": list(VIEWS), "description": "plan2 は2階建ての2階の平面"}, "kind": {"type": "string"},
                         "ids": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}},
    "render_view": {"fn": render_view, "description": "面の画像を返す。source=model は模型、glb は元の形。highlight の部材を赤枠で強調する。",
                    "schema": {"type": "object", "properties": {
                        "view": {"type": "string", "enum": list(VIEWS), "description": "plan2 は2階建ての2階の平面"}, "source": {"type": "string", "enum": ["model", "glb"]},
                        "highlight": {"type": "array", "items": {"type": "string"}}}, "required": ["view"], "additionalProperties": False}},
    "get_drawing_region": {"fn": get_drawing_region, "description": "図面（jww）の同じ面の画像と寸法・注記を返す（いまは未対応）。",
                           "schema": {"type": "object", "properties": {"view": {"type": "string", "enum": list(VIEWS)},
                                      "bbox_px": {"type": "array", "items": {"type": "integer"}}}, "required": ["view"], "additionalProperties": False}},
    "run_checks": {"fn": run_checks, "description": "コードの検査の結果（閉じた立体・離れた塊・造形範囲・glb との数の差）を返す。",
                   "schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    "propose_override": {"fn": propose_override, "description": "直す案を1件つくる。数値は書かず、数値の出どころ value_source（majority:種類 / rule:項目 / glb / number）を指定する。数値はコードが計算する。number は人が書いた数値のときだけ。屋根の軒の出は target=Y1・field=eave_mm.S のように辺を付ける（外壁の外の面から屋根の先まで、板金・樋を除く）。",
                         "schema": {"type": "object", "properties": {
                             "target": {"type": "string"}, "field": {"type": "string"}, "value_source": {"type": "string"},
                             "reason": {"type": "string"}, "number": {"type": "number"}, "finding": {"type": "string"}},
                             "required": ["target", "field", "value_source", "reason"], "additionalProperties": False}},
    "ask_question": {"fn": ask_question, "description": "ルールで決まらないことを人に聞く。選択肢は2〜3個、根拠（どの部材・どの図か）を添える。",
                     "schema": {"type": "object", "properties": {
                         "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}},
                         "evidence": {"type": "string"}}, "required": ["question", "options"], "additionalProperties": False}},
}


def call_tool(ctx, name: str, args: dict, specs: dict | None = None) -> dict:
    """道具を名前で呼ぶ。誤りは {"error": 日本語} で返す（Claude が読んで直せるように）。
    specs を渡すと、その道具の一覧から呼ぶ（会社ルールの言語化など）。"""
    spec = (specs or TOOL_SPECS).get(name)
    if spec is None:
        return {"error": f"{name} という道具はありません"}
    try:
        return spec["fn"](ctx, **args)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:
        return {"error": f"引数が正しくありません：{e}"}
