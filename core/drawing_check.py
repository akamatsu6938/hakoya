"""図面（jww）と模型の照合（SPEC §4-2・§12「物件Aは窓13か所の注記とも照合」）。

- 平面図を模型に位置合わせする（core/jww.align_property。合わなければ照合しない＝推測で合わせない）。
- 窓・テラスドアの注記（例「〇07405(FL+2400)」＝サッシの品番と上端の高さ）を、近い外壁の開口に1対1で結ぶ。
- 模型の開口の上端（FL から）と注記の FL+ を比べ、違えば「図面の注記と違う」。直す案の数値は注記の値（コードが読む）。
- 注記の文字は決まった形（品番・FL+）だけを使う。表題欄・縮尺1の層の文字は core/jww.py で読み捨て済み。
"""
from __future__ import annotations

import math
import re

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
