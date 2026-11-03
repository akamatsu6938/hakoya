"""検品の指摘（findings.json）の扱い（SPEC §6・§9-2）。

- 指摘は1件ずつ「承認／却下／あとで」。承認すると直す案を人が承認した修正（overrides）として記録する。
- 修正の履歴から取り消したら、その指摘は「未確認」に戻す。
- コードの検査からも指摘を作る（code_findings）：glb の例外（背中合わせの外壁など）・未解決の所・検査の警告。
  AI なし（off）のときは、これだけを出す（SPEC §9-2）。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from core import overrides as ov
from core.paths import out_dir

STATUSES = {"approve": "承認", "reject": "却下", "later": "あとで"}


def findings_path(alias: str) -> Path:
    return out_dir(alias) / "findings.json"


def load(alias: str) -> dict | None:
    p = findings_path(alias)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(alias: str, report: dict) -> None:
    findings_path(alias).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def code_findings(alias: str) -> list[dict]:
    """コードの検査から作る指摘（Claude を使わない）。"""
    b = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    out = []
    labels = {"back_to_back_exterior_faces": "背中合わせの外壁の面", "agari_kamachi": "上がり框の位置",
              "extra_roofs": "模型にしなかった屋根の部材", "upper_floor_overhang": "2階のはね出し"}
    for e in b.get("glb_exceptions", []):
        out.append({"view": "plan", "kind": "shape", "severity": "低",
                    "message": f"glb の例外（{labels.get(e['kind'], e['kind'])}）：{e['note']}",
                    "targets": [e["wall"]] if e.get("wall") else [], "evidence": [e.get("source") or "glb"], "source": "code"})
    try:   # glb にありえない形（見つけるだけ。直し方は 作者と決める：docs/proposal-glb-consistency.md）
        from core.consistency import find_all
        applied = ov.apply(b, ov.load(alias))[0]
        levels = {x["id"]: x.get("level") for x in applied.get("walls", []) + applied.get("openings", [])}
        upper = [lv["name"] for lv in applied.get("levels", [])[1:]]
        for c in find_all(applied):
            out.append({"view": "plan2" if levels.get(c["targets"][0]) in upper else "plan", "kind": "shape", "severity": "低",
                        "message": f"glb の整合性（要確認）：{c['message']}", "targets": c["targets"],
                        "evidence": ["コードの検査（glb の整合性）"], "source": "code"})
    except Exception:  # noqa: BLE001  整合性の検査の失敗で、ほかの指摘を止めない
        pass
    for u in b.get("unresolved", []):
        out.append({"view": "plan", "kind": "shape", "severity": "中", "message": f"未解決の所 {u['id']}：{u['note']}",
                    "targets": [], "evidence": ["壁の作り直し（unresolved）"], "source": "code"})
    # 予防：会社ルールと比べて「いつもと違う」所（会社ルールの言語化。家ごとに変わる値は比べない）
    from core.rule_check import differences
    from core.rules import load_rules
    for d in differences(b, load_rules(b.get("source", {}).get("company_rules", "jisha"))):
        out.append({"view": "plan", "kind": "rule", "severity": "中", "message": d["message"],
                    "targets": [], "evidence": [f"会社ルール {d['rule_key']}"], "source": "code"})
    try:   # 図面（jww）と比べて違う所（修正を当てたあとの家と比べる。図面が読めなくてもほかの指摘は出す）
        from core import overrides as ov
        from core.drawing_check import findings as drawing_findings
        out += drawing_findings(alias, ov.apply(b, ov.load(alias))[0])
    except Exception:  # noqa: BLE001  図面の読み取りの失敗で検品全体を止めない
        pass
    for s in b.get("skipped", []):
        if "見つからない" in s.get("reason", ""):
            out.append({"view": "plan", "kind": "opening", "severity": "中", "message": f"{s['source']}：{s['reason']}",
                        "targets": [], "evidence": [s["source"]], "source": "code"})
    checks = out_dir(alias) / "model" / "checks.json"
    if checks.exists():
        for c in json.loads(checks.read_text(encoding="utf-8"))["checks"]:
            if c["level"] in ("高", "中"):
                out.append({"view": "print", "kind": "print", "severity": c["level"], "message": c["message"],
                            "targets": [], "evidence": ["コードの検査"], "source": "code"})
    for i, f in enumerate(out, 1):
        f.setdefault("id", f"C{i}")
        f.setdefault("status", "未確認")
    return out


def run_off(alias: str) -> dict:
    """AI なし（off）の検品：コードの検査だけを指摘の一覧にする。"""
    report = {"alias": alias, "connection": "off", "summary": "AI を使わず、コードの検査だけを出しています",
              "findings": [], "code_findings": code_findings(alias), "questions": [],
              "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    save(alias, report)
    return report


def _find(report: dict, fid: str) -> dict:
    for f in report.get("findings", []) + report.get("code_findings", []):
        if f["id"] == fid:
            return f
    raise KeyError(f"{fid} という指摘はありません")


def decide(alias: str, fid: str, action: str) -> dict:
    """指摘を承認／却下／あとでにする。承認は直す案を修正として記録する（数値は検品のときにコードが計算した値）。"""
    if action not in STATUSES:
        raise ValueError("action は approve / reject / later のどれかです")
    report = load(alias)
    if report is None:
        raise KeyError("まだ検品していません")
    f = _find(report, fid)
    if action == "approve":
        p = f.get("proposal")
        if not p:
            raise ValueError("直す案のない指摘は承認できません（表で直すか、却下・あとでにしてください）")
        raw = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
        item = ov.add(alias, raw, p["target"], p["field"], p.get("to"), p["value_source"], p.get("reason") or f["message"],
                      origin=f"finding:{fid}")
        f["override"] = item["id"]
    f["status"] = STATUSES[action]
    f["decided_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    save(alias, report)
    return f


def revert_override(alias: str, override_id: str) -> None:
    """修正を取り消したら、それを生んだ指摘を「未確認」に戻す。"""
    report = load(alias)
    if report is None:
        return
    changed = False
    for f in report.get("findings", []) + report.get("code_findings", []):
        if f.get("override") == override_id:
            f["status"], f["override"] = "未確認", None
            changed = True
    if changed:
        save(alias, report)
