"""検品の指摘（findings.json）の扱い（SPEC §6・§9-2）。

- 指摘は1件ずつ「承認／却下／あとで」。承認すると直す案を人が承認した修正（overrides）として記録する。
  直す案のない指摘を承認すると、画面が Claude に直す案を頼み、指摘は「依頼済み」になる（★11/5 夜）。
  却下・あとで・依頼済みは「未確認」に戻せる（reset）。
- 修正の履歴から取り消したら、その指摘は「未確認」に戻す。
- コードの検査からも指摘を作る（code_findings）：glb の例外（背中合わせの外壁など）・未解決の所・検査の警告・
  窓の間の柱が1階の部品の中で上がつながらない所（直す案つき。core/lintel.py）・形の検査（core/shape_checks.py）。
  AI なし（off）のときは、これだけを出す（SPEC §9-2）。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

from core import overrides as ov
from core.paths import out_dir, write_text_atomic

# findings.json の「読んで直して書く」（decide・revert_override）と、検品の最後の書き込み（save）は、このプロセスの中で1つずつ
# （★11/6 見直し：決定が古い一覧を読んだあとに検品が新しい一覧を書くと、決定の書き込みで新しい検品の結果が丸ごと消えた）
LOCK = threading.RLock()

STATUSES = {"approve": "承認", "reject": "却下", "later": "あとで",
            "request": "依頼済み",     # 直す案のない指摘：この指摘のとおりに直す案を Claude に頼んだ（頼むのは画面の修正タブ。★11/5 夜）
            "reset": "未確認"}         # 却下・あとで・依頼済みを、未確認に戻す
TITLE_MAX = 30          # 見出しの長さの上限（目安は20字くらい。長すぎたら切る）
# glb の整合性の検査（core/consistency.py）の種類ごとの見出し。{0}・{1} は対象の番号
CONSISTENCY_TITLES = {"walls_overlap": "{0} と {1} の壁が重なる", "opening_protrudes": "{0} が壁の端からはみ出す",
                      "opening_above_top": "{0} の上端が高すぎる", "openings_overlap": "{0} と {1} が重なる",
                      "short_wall": "{0} の壁が短すぎる"}
# コードの検査（checks.json）の項目ごとの見出し。{name} は部品の名前
CHECK_TITLES = {"empty": "{name}の形がない", "watertight": "{name}が閉じた立体でない", "islands": "{name}に離れた塊がある",
                "build_volume": "{name}が造形範囲を超える", "opening_count": "建具と開口の数が合わない",
                "unresolved": "未解決の所がある", "overlap": "部品どうしが組むとぶつかる",
                "support": "{name}にサポートが要りそう", "print_split": "{name}の分け方に注意", "body_split": "本体の分け目に注意"}


def _clip(text: str) -> str:
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"


def title_of(f: dict) -> str:
    """指摘の見出し（★11/5：何が・どこで・どうおかしいかを20字くらいで。例「西の窓の間の柱が細い」）。
    title があればそれ（長すぎたら切る）。なければ message の最初の文から作る（かっこの中は外す）。"""
    t = str(f.get("title") or "").strip()
    if not t:
        msg = str(f.get("message") or "").strip()
        t = re.split(r"[。\n]", msg, maxsplit=1)[0]
        while True:                                   # 入れ子のかっこも外す
            shorter = re.sub(r"（[^（）]*）|\([^()]*\)", "", t)
            if shorter == t:
                break
            t = shorter
        t = t.strip(" 　：:、") or msg or "指摘"
    return _clip(t)


def _formatted(template: str, targets: list, **kw) -> str | None:
    try:
        return template.format(*targets, **kw)
    except (IndexError, KeyError):
        return None


def _check_title(c: dict) -> str | None:
    """checks.json の1行の見出し。部品の名前は message の「名前：」から（かっこの中は外す）。"""
    msg = str(c.get("message") or "")
    name = msg.split("：", 1)[0].split("（")[0].strip() if "：" in msg else ""
    template = CHECK_TITLES.get(c.get("item"))
    if not template or ("{name}" in template and not name):
        return None
    return _formatted(template, [], name=name)


def findings_path(alias: str) -> Path:
    return out_dir(alias) / "findings.json"


def load(alias: str) -> dict | None:
    p = findings_path(alias)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(alias: str, report: dict, path: Path | None = None) -> None:
    """一時ファイルに書いてから入れ替える（★11/5：MCP の道具など別のプロセスが同時に読んでも、書きかけを読まない）。
    path を渡すと、そこに書く（試験で書き出し先を変えるとき）。"""
    text = json.dumps(report, ensure_ascii=False, indent=2)
    with LOCK:
        write_text_atomic(path or findings_path(alias), text)


def code_findings(alias: str) -> list[dict]:
    """コードの検査から作る指摘（Claude を使わない）。"""
    b = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    out = []
    labels = {"back_to_back_exterior_faces": "背中合わせの外壁の面", "agari_kamachi": "上がり框の位置",
              "extra_roofs": "模型にしなかった屋根の部材", "upper_floor_overhang": "2階のはね出し",
              "door_above_floor": "床より高い所から始まる戸", "stairs_area": "階段の所", "eave_soffit": "軒天",
              "door_without_wall": "戸のまわりに壁の面がない所"}
    for e in b.get("glb_exceptions", []):
        out.append({"view": "plan", "kind": "shape", "severity": "低", "title": f"glb の例外：{labels.get(e['kind'], e['kind'])}",
                    "message": f"glb の例外（{labels.get(e['kind'], e['kind'])}）：{e['note']}",
                    "targets": [x for x in (e.get("wall"), e.get("opening")) if x],   # 開口の例外は開口を指す（★11/4）
                    "evidence": [e.get("source") or "glb"], "source": "code"})
    try:   # glb にありえない形（見つけるだけ。直し方は 作者と決める：docs/proposal-glb-consistency.md）
        from core.consistency import find_all
        applied = ov.apply(b, ov.load(alias))[0]
        levels = {x["id"]: x.get("level") for x in applied.get("walls", []) + applied.get("openings", [])}
        upper = [lv["name"] for lv in applied.get("levels", [])[1:]]
        for c in find_all(applied):
            out.append({"view": "plan2" if levels.get(c["targets"][0]) in upper else "plan", "kind": "shape", "severity": "低",
                        "title": _formatted(CONSISTENCY_TITLES.get(c.get("kind"), ""), c["targets"]) or "glb の形の食い違い（要確認）",
                        "message": f"glb の整合性（要確認）：{c['message']}", "targets": c["targets"],
                        "evidence": ["コードの検査（glb の整合性）"], "source": "code"})
    except Exception as e:  # noqa: BLE001  整合性の検査の失敗で、ほかの指摘を止めない（★11/4：失敗したことは知らせる）
        out.append({"view": "plan", "kind": "shape", "severity": "低", "targets": [], "source": "code",
                    "title": "glb の整合性の検査が止まった",
                    "message": f"glb の整合性の検査が途中で止まりました（{type(e).__name__}）。ほかの検査の結果は出ています",
                    "evidence": ["コードの検査（glb の整合性）"]})
    for u in b.get("unresolved", []):
        out.append({"view": "plan", "kind": "shape", "severity": "中", "title": f"未解決の所 {u['id']}",
                    "message": f"未解決の所 {u['id']}：{u['note']}",
                    "targets": [], "evidence": ["壁の作り直し（unresolved）"], "source": "code"})
    # 予防：会社ルールと比べて「いつもと違う」所（会社ルールの言語化。家ごとに変わる値は比べない）
    from core.rule_check import differences
    from core.rules import default_company, load_rules
    company = load_rules(b.get("source", {}).get("company_rules", default_company()))
    for d in differences(b, company):
        out.append({"view": "plan", "kind": "rule", "severity": "中", "title": f"{d['label']}が会社ルールと違う",
                    "message": d["message"],
                    "targets": [], "evidence": [f"会社ルール {d['rule_key']}"], "source": "code"})
    try:   # 窓の間の柱が、1階の部品の中で上がつながらない所（決定 2-4：直す案にして人が決める。平屋だけ。core/lintel.py）
        from core.lintel import fragile_post_findings
        out += fragile_post_findings(ov.apply(b, ov.load(alias))[0], company)
    except Exception as e:  # noqa: BLE001  この検査の失敗で、ほかの指摘を止めない（失敗したことは知らせる）
        out.append({"view": "plan", "kind": "print", "severity": "低", "targets": [], "source": "code",
                    "title": "窓の間の柱の検査が止まった",
                    "message": f"窓の間の柱の検査が途中で止まりました（{type(e).__name__}）。ほかの検査の結果は出ています",
                    "evidence": ["コードの検査（窓の間の柱）"]})
    try:   # 形の検査：ざらざらの層から外れた外壁・glb の基礎がない壁・家の外の外壁の分かれ方（★11/5。core/shape_checks.py）
        from core.print_options import effective_fuzzy
        from core.shape_checks import shape_findings
        out += shape_findings(ov.apply(b, ov.load(alias))[0], company, effective_fuzzy(alias, company)[0])   # ざらざらなしの物件は層を見ない
    except Exception as e:  # noqa: BLE001  この検査の失敗で、ほかの指摘を止めない（失敗したことは知らせる）
        out.append({"view": "plan", "kind": "shape", "severity": "低", "targets": [], "source": "code",
                    "title": "形の検査が止まった",
                    "message": f"形の検査（ざらざらの層・glb の基礎）が途中で止まりました（{type(e).__name__}）。ほかの検査の結果は出ています",
                    "evidence": ["コードの検査（形）"]})
    try:
        from core.paths import property_files
        has_drawings = bool(property_files(alias).jww)
    except Exception:  # noqa: BLE001  登録されていない家（試験用の家など）は図面と比べない
        has_drawings = False
    if has_drawings:
        try:   # 図面（jww）と比べて違う所（修正を当てたあとの家と比べる。図面が読めなくてもほかの指摘は出す）
            # ★11/4：ここで overrides を import し直していたため、関数の中で ov が「まだ入っていない変数」になり、
            # 上の整合性の検査が毎回 UnboundLocalError で止まって、握りつぶされていた（ov はファイルの先頭で読んでいる）
            from core.drawing_check import findings as drawing_findings
            out += drawing_findings(alias, ov.apply(b, ov.load(alias))[0])
        except Exception as e:  # noqa: BLE001  図面の読み取りの失敗で検品全体を止めない（★11/4：失敗したことは知らせる）
            out.append({"view": "plan", "kind": "opening", "severity": "低", "targets": [], "source": "code",
                        "title": "図面との照合が止まった",
                        "message": f"図面（jww）との照合が途中で止まりました（{type(e).__name__}）。ほかの検査の結果は出ています",
                        "evidence": ["図面との照合"]})
    for s in b.get("skipped", []):
        if "見つからない" in s.get("reason", ""):
            out.append({"view": "plan", "kind": "opening", "severity": "中", "title": s["reason"].removeprefix("この"),
                        "message": f"{s['source']}：{s['reason']}",
                        "targets": [], "evidence": [s["source"]], "source": "code"})
    checks = out_dir(alias) / "model" / "checks.json"
    if checks.exists():
        for c in json.loads(checks.read_text(encoding="utf-8"))["checks"]:
            if c["level"] in ("高", "中"):
                out.append({"view": "print", "kind": "print", "severity": c["level"], "title": _check_title(c),
                            "message": c["message"],
                            "targets": [], "evidence": ["コードの検査"], "source": "code"})
    for i, f in enumerate(out, 1):
        f.setdefault("id", f"C{i}")
        f.setdefault("status", "未確認")
        f["title"] = title_of(f)                      # 見出しのない指摘（作れなかったもの）は message の最初の文から
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


def is_applied(alias: str, f: dict) -> bool:
    """承認して、その修正がいまも直しの履歴に残っているか。"""
    oid = f.get("override")
    return f.get("status") == STATUSES["approve"] and bool(oid) and any(x.get("id") == oid for x in ov.load(alias))


def decide(alias: str, fid: str, action: str) -> dict:
    """指摘を承認／却下／あとで／依頼済み（直す案を頼んだ）／未確認に戻す にする。
    承認は直す案を修正として記録する（数値は検品のときにコードが計算した値）。
    ★11/5 夜：もう承認して修正が残っている指摘に approve が来たら、何もしないで今の指摘を返す（修正を2件にしない）。
    承認した指摘を却下・あとで・未確認にはしない（修正が残ったままになる。やめるときは直しの取り消しから）。"""
    if action not in STATUSES:
        raise ValueError("action は approve / reject / later / request / reset のどれかです")
    with LOCK:                                        # 読んでから書くまで、検品の書き込みを待たせる
        report = load(alias)
        if report is None:
            raise KeyError("まだ検品していません")
        f = _find(report, fid)
        applied = is_applied(alias, f)
        if action == "approve":
            if applied:
                return f
            p = f.get("proposal")
            if not p:
                raise ValueError("直す案のない指摘は承認できません（表で直すか、却下・あとでにしてください）")
            raw = json.loads((out_dir(alias) / "building.json").read_text(encoding="utf-8"))
            item = ov.add(alias, raw, p["target"], p["field"], p.get("to"), p["value_source"], p.get("reason") or f["message"],
                          origin=f"finding:{fid}")
            f["override"] = item["id"]
        elif applied:
            raise ValueError(f"{fid} は承認して、修正 {f['override']} になっています。やめるときは、直しの取り消し（取り消す）を使ってください")
        f["status"] = STATUSES[action]
        if action == "reset":
            f.pop("decided_at", None)
        else:
            f["decided_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        save(alias, report)
        return f


def revert_override(alias: str, override_id: str) -> None:
    """修正を取り消したら、それを生んだ指摘を「未確認」に戻す。"""
    with LOCK:                                        # 読んでから書くまで、検品の書き込みを待たせる
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
