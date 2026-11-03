"""会社ルールの数値（rules/<会社>.yaml）の読み込み（SPEC §5）。

会社ルールの言語化（学習）：人が承認した「学んだルール」（DATA_DIR/rules/<会社>.learned.json）を、決まった項目だけ上に重ねる。
- 家ごとに変わる値は物件ごとに glb から作る。学んだルールは、会社で変わらない値（壁厚など）と細かい決まりを残し、
  困ったときの予防（新しい物件との比べ・glb から分からないときの予備の値・Claude に渡す決まり）に使う（作者 11/3）。
- 元の yaml（★のコメント付き）と skills/ は書き換えない。exe では rules/ に書き込めないため、学んだルールは DATA_DIR に置く。
- HAKOYA_LEARNED_RULES=off のときは重ねない（テストと控えの道具。結果が人の承認で変わらないように）。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import yaml

from core.paths import DATA_DIR

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"
DEFAULT_COMPANY = "jisha"
LEARNED_DIR = DATA_DIR / "rules"
# 学んでよい項目（会社で変わらない値）。模型の作り方（model.*）と内壁の厚み（作者の決め）は学ばない
LEARNABLE = ("levels.gl_to_foundation_top_mm", "levels.gl_to_1fl_mm", "levels.ceiling_height_mm",
             "levels.story_height_choices_mm", "walls.exterior_thickness_mm", "openings.door_top_fl_mm",
             "roof.build_up_mm")


def _base(company: str) -> dict:
    path = RULES_DIR / f"{company}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"会社ルール rules/{company}.yaml がありません。")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def base_text(company: str) -> str:
    """会社ルールの yaml の文（★のコメント込み。Claude に読ませる）。"""
    return (RULES_DIR / f"{company}.yaml").read_text(encoding="utf-8")


def learned_enabled() -> bool:
    return os.environ.get("HAKOYA_LEARNED_RULES", "on").strip().lower() != "off"


def learned_path(company: str) -> Path:
    return LEARNED_DIR / f"{company}.learned.json"


def load_learned(company: str) -> dict:
    """学んだルール。{next_id, values: {項目: {id, value, previous, evidence, ...}}, notes: [...], rejected: [...]}"""
    p = learned_path(company)
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data.setdefault("next_id", 1)
    data.setdefault("values", {})
    data.setdefault("notes", [])
    data.setdefault("rejected", [])
    return data


def save_learned(company: str, data: dict) -> None:
    p = learned_path(company)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def type_ok(old, new) -> bool:
    """学んだ値の型が、元の会社ルールの値の型と合うか（数・数の並び・文字）。"""
    if _is_num(old):
        return _is_num(new)
    if isinstance(old, list):
        return isinstance(new, list) and all(_is_num(x) for x in new)
    if isinstance(old, str):
        return isinstance(new, str)
    return False


def _set_dotted(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value


def apply_learned(rules: dict, learned: dict) -> tuple[dict, list[dict]]:
    """学んだ値を重ねた会社ルールの写しと、重ねられなかった学んだ値（理由つき）。"""
    out = copy.deepcopy(rules)
    skipped = []
    for key, item in learned.get("values", {}).items():
        if key not in LEARNABLE:
            skipped.append({"rule_key": key, "reason": "学んでよい項目ではない"})
            continue
        try:
            old = rule(out, key)
        except KeyError:
            skipped.append({"rule_key": key, "reason": "元の会社ルールにない項目"})
            continue
        if not type_ok(old, item.get("value")):
            skipped.append({"rule_key": key, "reason": "値の型が元の会社ルールと違う"})
            continue
        _set_dotted(out, key, item["value"])
    return out, skipped


def load_rules(company: str = DEFAULT_COMPANY, learned: bool | None = None) -> dict:
    """会社ルール。learned=None のときは HAKOYA_LEARNED_RULES に従って学んだルールを重ねる。"""
    base = _base(company)
    use = learned_enabled() if learned is None else learned
    if not use:
        return base
    merged, _ = apply_learned(base, load_learned(company))
    return merged


def learned_skill_section(company: str) -> str:
    """承認した文章を、会社の Skill の後ろに足す節にする（なければ空。skills/ は書き換えない）。"""
    if not learned_enabled():
        return ""
    notes = load_learned(company).get("notes", [])
    if not notes:
        return ""
    lines = ["## 学んだ会社の決まり（人が承認したもの。HAKOYA の会社ルールの言語化）"]
    lines += [f"- {n['text']}（根拠：{n.get('evidence', '')}）" for n in notes]
    return "\n".join(lines)


def _answer_files() -> list[tuple[str, Path]]:
    """人の答えの記録：開発中に書いた rules/answers.jsonl（番号 A）と、アプリが足す DATA_DIR/rules/answers.jsonl（番号 D）。"""
    return [("A", RULES_DIR / "answers.jsonl"), ("D", LEARNED_DIR / "answers.jsonl")]


def load_answers() -> list[dict]:
    """人の答え（同じ質問を二度しないため・会社ルールの言語化の根拠）。番号は行の順（足すだけなので変わらない）。"""
    out = []
    for prefix, p in _answer_files():
        if not p.exists():
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                out.append({"id": f"{prefix}{i}", **json.loads(line)})
    return out


def add_answer(question: str, answer: str, property: str = "全体", evidence: str = "") -> dict:
    """人の答えを DATA_DIR/rules/answers.jsonl に足す（rules/ は書き換えない）。"""
    import datetime as dt
    p = LEARNED_DIR / "answers.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"property": property, "question": question, "answer": answer, "applied_as": "", "evidence": evidence,
           "at": dt.date.today().isoformat()}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"id": f"D{sum(1 for x in p.read_text(encoding='utf-8').splitlines() if x.strip())}", **rec}


def rule(rules: dict, dotted: str):
    """「walls.exterior_thickness_mm」の形で値を引く。"""
    cur = rules
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(f"会社ルールに {dotted} がありません。")
        cur = cur[key]
    return cur
