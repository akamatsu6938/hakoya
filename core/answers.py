"""検品の Claude の質問への、人の答え（物件ごと。SPEC §9-1「同じ質問は二度しない」）。

- 保存先は out/<物件>/answers.json（1件ずつ：question_id・question・options・answer・at・inspected_at）。
  一時ファイルに書いてから入れ替える。書く前に、出してはいけない言葉（ng_words）がないか確かめる。
- 検品のたびに findings.json は丸ごと書き直され、質問の番号は Q1 からやり直しになる。
  どの検品の質問への答えかは inspected_at（findings.json の at）と質問の文で見分ける。
- 画面に返すとき（attach）：この検品の質問への答えは answer、前の検品で同じ文の質問にした答えは previous_answer。
- 次の検品で、Claude に「前の検品で人が答えたこと」を渡す（prompt_section）。同じ質問をもう一度しないように。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import unicodedata
from pathlib import Path

from core import privacy
from core.paths import out_dir, write_text_atomic
from core.privacy import assert_clean

ANSWER_MAX = 400          # 答えの長さの上限（字）。図面の数値などを一言で書ける長さ
PROMPT_MAX = 20           # 次の検品に渡す答えの数の上限（新しいものから。指示を短く保つ）
LOCK = threading.Lock()   # answers.json の「読んで足して書く」は1つずつ（★11/6 見直し：2つの答えが同時に届くと片方が消えた）


def answers_path(alias: str) -> Path:
    return out_dir(alias) / "answers.json"


def _norm(text) -> str:
    """同じ質問かを比べる形（全角・半角と空白の違いをなくす）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def same_question(a, b) -> bool:
    return bool(_norm(a)) and _norm(a) == _norm(b)


def _clean_items(items: list[dict]) -> list[dict]:
    """出してはいけない言葉が入っていない答えだけ（書いたあとで言葉を足したとき、その答えは画面に出さない。
    1件のために指摘の一覧ごと開けなくならないように。Claude に渡すほうは prompt_section が同じように外す）。"""
    words = privacy.ng_words()
    return [a for a in items if privacy.find_ng(json.dumps(a, ensure_ascii=False), words) is None] if words else items


def load(alias: str, path: Path | None = None) -> list[dict]:
    """答えの一覧（古い順）。ない・読めないときは空（書きかけ・壊れたファイルで検品を止めない）。"""
    p = path or answers_path(alias)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def questions_of(report: dict) -> list[tuple[str, dict]]:
    """findings.json の質問：(番号, 質問)。検品担当の質問（questions。Q1…）と、指摘に付いた質問（番号はその指摘の F…）。"""
    out = [(str(q.get("id")), q) for q in report.get("questions") or [] if isinstance(q, dict) and q.get("id")]
    out += [(str(f["id"]), f["question"]) for f in report.get("findings") or []
            if isinstance(f, dict) and f.get("id") and isinstance(f.get("question"), dict)]
    return out


def find_question(report: dict, qid: str) -> dict | None:
    return next((q for i, q in questions_of(report) if i == qid), None)


def add(alias: str, question_id: str, question: str, options, answer: str, inspected_at: str | None) -> dict:
    """答えを1件残す。同じ検品の同じ質問に答え直したら、前の答えと入れ替える。
    出してはいけない言葉が入っていたら PrivacyError（何も書かない。言葉そのものは文に入らない）。"""
    answer = str(answer or "").strip()
    if not answer:
        raise ValueError("答えを書いてください")
    if len(answer) > ANSWER_MAX:
        raise ValueError(f"答えは {ANSWER_MAX} 字までにしてください")
    rec = {"question_id": str(question_id), "question": str(question or ""),
           "options": [str(o) for o in (options or [])], "answer": answer,
           "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "inspected_at": inspected_at}
    assert_clean(json.dumps(rec, ensure_ascii=False), "人の答え")       # 書く前に確かめる
    key = (rec["question_id"], inspected_at, _norm(rec["question"]))
    with LOCK:
        items = [a for a in load(alias) if (a.get("question_id"), a.get("inspected_at"), _norm(a.get("question"))) != key]
        write_text_atomic(answers_path(alias), json.dumps(items + [rec], ensure_ascii=False, indent=2))
    return rec


def attach(alias: str, report: dict) -> dict:
    """質問に人の答えを付ける（画面に返す前。ファイルは書き換えない）。
    answer・answered_at：この検品の、この質問への答え。previous_answer：前の検品で同じ文の質問にした答え（いちばん新しいもの）。"""
    items = _clean_items(load(alias))
    if not items:
        return report
    at = report.get("at")
    for qid, q in questions_of(report):
        key = _norm(q.get("question"))
        if not key:
            continue
        same_text = [a for a in items if _norm(a.get("question")) == key]
        mine = [a for a in same_text if a.get("question_id") == qid and a.get("inspected_at") == at]
        if mine:
            q["answer"], q["answered_at"] = mine[-1].get("answer"), mine[-1].get("at")
        before = [a for a in same_text if a not in mine]
        if before:
            q["previous_answer"] = {"answer": before[-1].get("answer"), "at": before[-1].get("at")}
    return report


def prompt_section(alias: str, path: Path | None = None) -> str:
    """次の検品の指示に足す「前の検品で人が答えたこと」（答えがなければ空）。同じ文の質問は新しい答えだけ。
    あとから出してはいけない言葉を足したときのために、渡す前にも1件ずつ確かめ、入っているものは渡さない。
    path：答えのファイルの場所（試験・書き出し先を変えたとき。省くと out/<物件>/answers.json）。"""
    words = privacy.ng_words()
    rows, seen = [], set()
    for a in reversed(load(alias, path)):
        key = _norm(a.get("question"))
        if not key or key in seen or not str(a.get("answer") or "").strip():
            continue
        seen.add(key)
        if words and privacy.find_ng(json.dumps(a, ensure_ascii=False), words) is not None:
            continue                     # 出してはいけない言葉が入った答えは渡さない（同じ質問の古い答えにも戻らない）
        opts = [str(o) for o in a.get("options") or []]
        rows.append(f"- 質問：{a.get('question')}"
                    + (f"（選択肢：{' ／ '.join(f'{i}. {o}' for i, o in enumerate(opts, 1))}）" if opts else "")
                    + f" → 人の答え：{a.get('answer')}")
        if len(rows) >= PROMPT_MAX:
            break
    if not rows:
        return ""
    return "\n".join(["## 前の検品で人が答えたこと（HAKOYA の検品タブで、人が Claude の質問に答えたもの）",
                      "同じ質問は、もう一度しないでください。人の答えに沿って判断してください。", *reversed(rows)])
