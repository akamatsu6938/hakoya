"""Walk in home の glb から building.json（家の形）を作る（SPEC §4-1・§6）。

使い方:  python -m adapters.walkinhome 物件A
いまの段階（2a）：部材の仕分け・数の一覧と、高さ（GL・基礎天端・各階の FL・天井高）まで。
壁・開口・屋根は次の段階で足す。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import numpy as np

from adapters.glb_reader import GlbModel, Part, read_glb
from adapters.walkinhome_types import part_type
from core.paths import glb_working_copy, out_dir, property_files
from core.privacy import assert_clean
from core.rules import load_rules, rule

FLAT_TOL_MM = 1.0          # 水平な面とみなす高さの差
STORY_GAP_MM = 1500.0      # 床の高さがこれ以上離れていたら別の階 (推定。2階建ては要検証)
ROOM_FLOOR_CODES = ("HEYA_YUKA", "SHUU_YUKA")


def _flat_heights(parts: list[Part], codes: tuple[str, ...] | None = None) -> collections.Counter:
    """水平な三角形の高さ（mm、整数に丸め）ごとの数。"""
    cnt: collections.Counter = collections.Counter()
    for p in parts:
        for pr in p.primitives:
            if codes is not None and pr.code not in codes:
                continue
            f = pr.vertices[pr.triangles]
            if not len(f):
                continue
            flat = np.ptp(f[:, :, 2], axis=1) < FLAT_TOL_MM
            for z in np.round(f[flat, 0, 2]).astype(int):
                cnt[int(z)] += 1
    return cnt


def _first_source(parts: list[Part], codes: tuple[str, ...], z: int) -> str:
    """高さ z の水平面を持つ最初の部材の出どころ。"""
    for p in parts:
        if _flat_heights([p], codes).get(z):
            return p.source
    return parts[0].source if parts else "glb:unknown"


def detect_ground(model: GlbModel) -> dict:
    """GL と基礎天端。"""
    site = model.by_type("typeESiki")
    if site:
        gl = int(round(site[0].bounds()[0][2]))
        gl_src = site[0].source
    else:
        gl, gl_src = 0, None
    kiso = model.by_type("typeEKiso")
    if not kiso:
        raise ValueError("基礎（typeEKiso）が glb にありません。")
    # ★基礎の本体（KISO_HONT）は側面と底だけで、天端の水平面は別の部位（KISO_ILOW）に入っている。
    # 本体の側面が届いている一番上を基礎天端とする（水切り KISO_DMIZ は天端より上に出るので使わない）。
    hont = [k.faces("KISO_HONT") for k in kiso]
    hont = [f for f in hont if len(f)]
    if not hont:
        raise ValueError("基礎の本体（KISO_HONT）が見つかりません。")
    top = int(round(max(f[:, :, 2].max() for f in hont)))
    ground = {"gl_mm": gl, "foundation_top_mm": top, "foundation_source": kiso[0].source}
    if gl_src:
        ground["gl_source"] = gl_src
    else:
        ground["gl_assumption"] = "敷地がないため GL＝Z 0 とした"
    return ground


def detect_levels(model: GlbModel) -> list[dict]:
    """各階の FL と天井高。部屋の床の高さをまとめ、離れていれば別の階とする。"""
    rooms = model.by_role("room")
    floors = _flat_heights(rooms, ROOM_FLOOR_CODES)
    if not floors:
        raise ValueError("部屋の床（HEYA_YUKA）が見つかりません。")
    # 高さ順に並べ、STORY_GAP_MM 以上離れたところで階を分ける
    groups: list[list[int]] = []
    for z in sorted(floors):
        if groups and z - groups[-1][-1] < STORY_GAP_MM:
            groups[-1].append(z)
        else:
            groups.append([z])
    ceilings = _flat_heights(rooms, ("HEYA_TENJ",))
    levels = []
    for i, g in enumerate(groups):
        fl = max(g, key=lambda z: floors[z])  # その階でいちばん多い床の高さ
        upper = groups[i + 1][0] if i + 1 < len(groups) else float("inf")
        cands = {z: n for z, n in ceilings.items() if fl < z < upper}
        level = {"name": f"{i + 1}F", "fl_mm": fl, "source": _first_source(rooms, ROOM_FLOOR_CODES, fl)}
        if cands:
            top = max(cands, key=lambda z: cands[z])
            level["ceiling_mm"] = top - fl
            level["ceiling_source"] = _first_source(rooms, ("HEYA_TENJ",), top)
        if len(g) > 1:
            level["other_floor_heights_mm"] = [z for z in g if z != fl]
        levels.append(level)
    return levels


def inventory(model: GlbModel) -> list[dict]:
    """部材の種類ごとの数と、模型での扱い。"""
    cnt = collections.Counter(p.type_code or "unknown" for p in model.parts)
    rows = []
    for code, n in cnt.most_common():
        t = part_type(code)
        rows.append({"type": code, "label": t.label, "role": t.role, "count": n})
    return rows


def compare_with_rules(ground: dict, levels: list[dict], rules: dict) -> list[dict]:
    """glb で測った値と会社ルールの値の食い違い（検品の材料）。"""
    out = []
    pairs = [("levels.gl_to_foundation_top_mm", ground["foundation_top_mm"] - ground["gl_mm"], "基礎天端（GL から）")]
    if levels:
        pairs.append(("levels.gl_to_1fl_mm", levels[0]["fl_mm"] - ground["gl_mm"], "1FL（GL から）"))
        if "ceiling_mm" in levels[0]:
            pairs.append(("levels.ceiling_height_mm", levels[0]["ceiling_mm"], "1階の天井高"))
    for key, measured, label in pairs:
        expected = rule(rules, key)
        out.append({"item": label, "glb_mm": measured, "rule": key, "rule_mm": expected, "match": measured == expected})
    return out


def build(alias: str, company: str = "jisha") -> dict:
    files = property_files(alias)
    model = read_glb(glb_working_copy(files))
    rules = load_rules(company)
    ground = detect_ground(model)
    levels = detect_levels(model)
    return {
        "source": {"glb": files.glb_label, "generator": model.generator, "wih_version": model.version_info,
                   "company_rules": company},
        "ground": ground,
        "levels": levels,
        "inventory": inventory(model),
        "rule_checks": compare_with_rules(ground, levels, rules),
        "assumptions": [],
    }


def write_building(alias: str, building: dict) -> Path:
    text = json.dumps(building, ensure_ascii=False, indent=2)
    assert_clean(text, "building.json")
    path = out_dir(alias) / "building.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _print_summary(alias: str, b: dict) -> None:
    print(f"【{alias}】 {b['source']['wih_version']}")
    g = b["ground"]
    print(f"  GL {g['gl_mm']} / 基礎天端 {g['foundation_top_mm']}  （出どころ {g.get('gl_source', '-')}, {g['foundation_source']}）")
    for lv in b["levels"]:
        print(f"  {lv['name']}: FL {lv['fl_mm']} / 天井高 FL+{lv.get('ceiling_mm', '?')}  （出どころ {lv['source']}）")
    print("  会社ルールとの照合:")
    for c in b["rule_checks"]:
        mark = "一致" if c["match"] else "★食い違い"
        print(f"    {c['item']}: glb {c['glb_mm']} / ルール {c['rule_mm']} → {mark}")
    print("  部材の数:")
    for r in b["inventory"]:
        print(f"    {r['label']:<12}（{r['type']}）{r['count']:>3} 個 → {r['role']}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Walk in home の glb から building.json を作る")
    ap.add_argument("alias", help="物件の仮名（data/local_paths.yaml の名前。例：物件A）")
    ap.add_argument("--company", default="jisha", help="会社ルール（rules/<名前>.yaml）")
    args = ap.parse_args(argv)
    building = build(args.alias, args.company)
    path = write_building(args.alias, building)
    _print_summary(args.alias, building)
    print(f"  → 書き出し: data/out/{args.alias}/{path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
