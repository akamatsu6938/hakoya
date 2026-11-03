"""予防：物件を会社ルール（承認した学んだルールを含む）と比べ、「いつもと違う」所を知らせる（会社ルールの言語化 段階C）。

- 家ごとに変わる値（窓の上端・屋根の勾配・軒の出など）は比べない（知らせない）。
- 会社で変わらない値（基礎・1FL・天井高・外壁の厚み（測った値）・室内のドアの上端（そろえる前）・屋根の厚み）と、
  階高が会社ルールの選択肢にあるかを見る。
- building.json の rule_checks は組み立てたときの1回きりなので、こちらで毎回いまの会社ルールと比べ直す。
  ★11/3 物件B の階高（3,000）を見落とした（docs/mistakes/2026-11-03-story-height-not-checked.md）。その再発防止。
"""
from __future__ import annotations

from core.rule_stats import FROM_GLB, STATS, _same, fmt, observe
from core.rules import rule

CHECKED = ("levels.foundation_top", "levels.fl1", "levels.ceiling", "levels.story_height", "walls.exterior_measured",
           "openings.door_head", "roofs.thickness")
ASK = "glb の読み違いか、特別な物件か確かめてください"


def check_property(b: dict, rules: dict) -> list[dict]:
    """項目ごとの比べ：[{stat, label, rule_key, values, rule_value, match, message?}]。"""
    out = []
    obs = observe(b)
    for sid in CHECKED:
        vals = obs.get(sid)
        if not vals:
            continue
        name, rkey, compare, how = STATS[sid]
        try:
            rv = rule(rules, rkey)
        except KeyError:
            continue
        if rv is None or rv == FROM_GLB:
            continue
        bad = [(v, w) for v, w in vals if not ((v in rv) if compare == "choices" else _same(v, rv, compare))]
        rule_txt = ("選択肢 " + "・".join(fmt(x, how) for x in rv)) if compare == "choices" else fmt(rv, how)
        row = {"stat": sid, "label": name, "rule_key": rkey, "values": sorted({v for v, _ in vals}),
               "rule_value": rv, "rule_text": rule_txt, "match": not bad}
        if bad:
            where = "・".join(f"{fmt(v, how)}（{w}）" for v, w in bad[:6]) + (f" ほか{len(bad) - 6}か所" if len(bad) > 6 else "")
            row["message"] = f"{name}が会社ルール（{rule_txt}）と違う：{where}。{ASK}"
        out.append(row)
    return out


def differences(b: dict, rules: dict) -> list[dict]:
    """「いつもと違う」所だけ。"""
    return [r for r in check_property(b, rules) if not r["match"]]
