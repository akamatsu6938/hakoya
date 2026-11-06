"""修正の依頼（囲んで直す・文字で直す）を物件ごとに保存し、裏で Claude を動かす（★11/5 夜 作者の試しから）。

作者の言葉：「承認すると修正画面が別のタブに移動して消える」「答えを待っている間に、他のことをするとどっか行ってしまう」。
→ 頼んだことと Claude の答えを、物件ごとのファイルに残す。画面はタブを替えても・物件を開き直しても、一覧を読み直せば見える。

- 保存先：out/<物件>/fix_requests.json（{"next_id": 次の番号, "requests": [古い順]}）。一時ファイルに書いてから入れ替える。
  書く前に、出してはいけない言葉が入っていないか確かめる（入っていれば答えは残さず、失敗にする）。
- 依頼の形（画面にもこのまま返す）：{id:"R1", at, status, text, kind, model, model_label, connection, selection, groups,
  message, proposals:[{…直す案…, state, override_id, override_at, note}], questions:[{id, question, options, evidence, answered?}],
  cost_usd, duration_s, error, finding_id, parent_id, human_words}
  - status：running（Claude が考えている）・done・error・interrupted（HAKOYA を止めたので途中で止まった）
  - 直す案の state：pending（まだ）・approved（直した。override_id と、その修正の時刻 override_at）・rejected・
    no_change（模型が変わらないので取り消した）・failed
- 裏の thread で動かす。同じ物件で同時に動かすのは MAX_RUNNING（2）まで。
- この HAKOYA の中で動いていない running（前に起動した HAKOYA が動かしていたもの）は、読むときに interrupted にする。
- 直す案を承認したあと、その修正が取り消されていたら（直しの履歴から取り消したなど）、読むときに pending に戻す。
- 質問への答えは、元の一言に「質問：…／選択肢：1. … 2. …／人の答え：…」を付けた新しい依頼にする（answered_text）。
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path

from core.paths import out_dir, write_text_atomic
from core.privacy import PrivacyError, assert_clean

FILE = "fix_requests.json"
MAX_RUNNING = 2                  # 同じ物件で同時に Claude に考えてもらう依頼の数
LOCK = threading.RLock()        # ファイルの読み書き（このプロセスの中で1つずつ）
ACTIVE: set[tuple[str, str]] = set()   # この HAKOYA の中で動いている依頼（物件, 番号）

INTERRUPTED = "HAKOYA を止めたため、途中で止まりました。もう一度頼んでください"
UNDONE_NOTE = "取り消しました（もう一度承認できます）"


class BusyError(RuntimeError):
    """同じ物件で動いている依頼が多すぎる。"""


def path(alias: str) -> Path:
    return out_dir(alias) / FILE


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _read(alias: str) -> dict:
    p = path(alias)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"next_id": 1, "requests": []}
    if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
        return {"next_id": 1, "requests": []}
    return data


def _write(alias: str, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=1)
    assert_clean(text, "修正の依頼の記録")
    write_text_atomic(path(alias), text)


def _current_overrides(alias: str) -> dict[str, dict] | None:
    """いま記録にある修正（番号 → 修正。読めなければ None。そのときは承認の印を直さない）。"""
    from core import overrides as ov
    try:
        return {x.get("id"): x for x in ov.load(alias)}
    except (OSError, ValueError):
        return None


def _still_recorded(p: dict, current: dict[str, dict]) -> bool:
    """承認した案の修正が、いまも記録にあるか。修正の番号は取り消すと使い回される（core/overrides.add は「いちばん大きい番号＋1」）
    ので、承認したときに控えた修正の時刻（override_at）と、部材・項目・値も比べる（★11/6 見直し：承認した案の修正を直しの履歴から
    取り消したあと、一覧を読み直す前に表で直すと同じ OV 番号になり、案が承認のままに見えて「取り消す」で表の直しを消すところだった）。
    override_at のない記録（前の版で承認したもの）は、番号だけで比べる。"""
    x = current.get(p["override_id"])
    if x is None:
        return False
    if not p.get("override_at"):
        return True
    return (x.get("at"), x.get("target"), x.get("field"), x.get("to")) == (p["override_at"], p.get("target"), p.get("field"),
                                                                            p.get("to"))


def _settle(alias: str, data: dict) -> bool:
    """読むときの手直し：前の HAKOYA の running → interrupted、取り消された修正の承認 → pending。直したら True。"""
    changed = False
    current = None
    for r in data["requests"]:
        if r.get("status") == "running" and (alias, r.get("id")) not in ACTIVE:
            r.update(status="interrupted", error=INTERRUPTED)
            changed = True
        for p in r.get("proposals") or []:
            if p.get("state") == "approved" and p.get("override_id"):
                current = _current_overrides(alias) if current is None else current
                if current is not None and not _still_recorded(p, current):
                    p.update(state="pending", override_id=None, override_at=None, note=UNDONE_NOTE)
                    changed = True
    return changed


def load(alias: str) -> list[dict]:
    """依頼の一覧（新しい順）。"""
    with LOCK:
        data = _read(alias)
        if _settle(alias, data):
            _write(alias, data)
        return [copy.deepcopy(r) for r in reversed(data["requests"])]


def get(alias: str, rid: str) -> dict:
    for r in load(alias):
        if r.get("id") == rid:
            return r
    raise KeyError(f"{rid} という依頼はありません")


def running_count(alias: str) -> int:
    return sum(1 for a, _ in list(ACTIVE) if a == alias)      # 写しで数える（裏の thread が同時に外すため）


def create(alias: str, *, text: str, kind: str, model: str, model_label: str, connection: str,
           selection: list[dict], groups: list[dict], finding_id: str | None = None, parent_id: str | None = None,
           human_words: list[str] | None = None) -> dict:
    """依頼を running で記録する（Claude を動かすのは start）。同じ物件で MAX_RUNNING 個が動いていれば BusyError。"""
    with LOCK:
        if running_count(alias) >= MAX_RUNNING:
            raise BusyError(f"いま {MAX_RUNNING} つの依頼を Claude が考えています。前の依頼が終わってから頼んでください")
        data = _read(alias)
        _settle(alias, data)
        n = max([int(data.get("next_id") or 1)] + [int(str(r.get("id", "R0"))[1:] or 0) + 1 for r in data["requests"]
                                                   if str(r.get("id", ""))[1:].isdigit()])
        req = {"id": f"R{n}", "at": _now(), "status": "running", "text": text, "kind": kind, "model": model,
               "model_label": model_label, "connection": connection, "selection": selection, "groups": groups,
               "message": "", "proposals": [], "questions": [], "cost_usd": None, "duration_s": None, "error": None,
               "finding_id": finding_id, "parent_id": parent_id, "human_words": human_words or [text]}
        data["next_id"] = n + 1
        data["requests"].append(req)
        ACTIVE.add((alias, req["id"]))
        try:
            _write(alias, data)
        except Exception:
            ACTIVE.discard((alias, req["id"]))
            raise
        return copy.deepcopy(req)


def _update(alias: str, rid: str, fn) -> dict | None:
    """依頼を1つ書き換えて保存する。消されていれば None。"""
    with LOCK:
        data = _read(alias)
        _settle(alias, data)
        for r in data["requests"]:
            if r.get("id") == rid:
                fn(r)
                _write(alias, data)
                return copy.deepcopy(r)
        return None


def finish(alias: str, rid: str, result: dict) -> dict | None:
    """Claude の答えを記録する（直す案は state=pending から）。"""
    def put(r):
        r.update(status="done", message=result.get("message") or "", cost_usd=result.get("cost_usd"),
                 duration_s=result.get("duration_s"), error=None,
                 proposals=[{**p, "state": "pending", "override_id": None, "note": ""} for p in result.get("proposals") or []],
                 questions=[dict(q) for q in result.get("questions") or []])
        if result.get("selection") is not None:
            r["selection"] = result["selection"]           # いまの家の値で説明し直した一覧
        if result.get("fake"):
            r["fake"] = True
    try:
        return _update(alias, rid, put)
    except PrivacyError:                                   # 答えに出してはいけない言葉が入っていた：答えは残さない
        return fail(alias, rid, "Claude の答えに出してはいけない言葉が入っていたため、答えを残しませんでした")


def fail(alias: str, rid: str, error: str) -> dict | None:
    def put(r):
        r.update(status="error", error=error)
    return _update(alias, rid, put)


def start(alias: str, rid: str, job) -> threading.Thread:
    """裏の thread で job()（Claude に頼んで答えの dict を返す）を動かし、終わったら記録する。"""
    def run():
        started = time.time()
        try:
            try:
                result = job()
            except (ValueError, RuntimeError) as e:        # HAKOYA が書いた日本語の説明
                fail(alias, rid, str(e) or type(e).__name__)
            except Exception as e:  # noqa: BLE001  思いがけないエラー（文には場所が入りうるので種類だけ）
                fail(alias, rid, f"思いがけないエラーです（{type(e).__name__}）")
            else:
                if result.get("duration_s") is None:
                    result["duration_s"] = round(time.time() - started, 1)
                finish(alias, rid, result)
        except Exception as e:  # noqa: BLE001  記録に書けなかった（黒い画面には種類だけ。依頼は次に読むとき「途中で止まった」になる）
            print(f"[HAKOYA] 修正の依頼 {rid} を記録できませんでした（{type(e).__name__}）", file=sys.stderr, flush=True)
        finally:
            ACTIVE.discard((alias, rid))
    t = threading.Thread(target=run, daemon=True, name=f"fix-{rid}")
    t.start()
    return t


def remove(alias: str, rid: str) -> dict:
    """一覧から外す（lasso_log.jsonl の記録は残る）。Claude が考えている途中の依頼は外さない。"""
    with LOCK:
        data = _read(alias)
        _settle(alias, data)
        hit = next((r for r in data["requests"] if r.get("id") == rid), None)
        if hit is None:
            raise KeyError(f"{rid} という依頼はありません")
        if hit.get("status") == "running":
            raise BusyError("Claude が考えている途中なので外せません。答えが届いてから外してください")
        data["requests"] = [r for r in data["requests"] if r.get("id") != rid]
        _write(alias, data)
        return copy.deepcopy(hit)


def set_proposal(alias: str, rid: str, index: int, **changes) -> dict:
    """直す案の印（state・override_id・note・from・to）を書き換える。"""
    def put(r):
        props = r.get("proposals") or []
        if not 0 <= index < len(props):
            raise IndexError(index)
        props[index].update(changes)
    try:
        out = _update(alias, rid, put)
    except IndexError:
        raise KeyError(f"{rid} に {index + 1} 番目の直す案はありません") from None
    if out is None:
        raise KeyError(f"{rid} という依頼はありません")
    return out


def mark_answered(alias: str, rid: str, question_id: str, answer: str, child_id: str) -> dict | None:
    def put(r):
        for q in r.get("questions") or []:
            if q.get("id") == question_id:
                q.update(answered=answer, child_id=child_id)
    return _update(alias, rid, put)


# ---------------------------------------------------------------- 質問への答え（★11/5 夜：選択肢の文が Claude に届かず、「2で試す」が取り違えられた）

_OPTION_NO = re.compile(r"^(\d+)(?:[.)、:]\s|[.)]?$|(?=[^\d.,]))")


def option_number(answer: str, n: int) -> int | None:
    """「2」「2で試す」「2. W5」「２番」のように選択肢の番号で始まる答えの番号（1〜n）。「2.5」「200にしたい」は番号ではない。"""
    m = _OPTION_NO.match(unicodedata.normalize("NFKC", answer or "").strip())
    if m and 1 <= int(m.group(1)) <= n:
        return int(m.group(1))
    return None


def answered_text(parent: dict, question: dict, answer: str) -> tuple[str, list[str]]:
    """答えを添えて頼み直すときの一言と、人が書いた・選んだ文（number の数値はこの中のものだけ）。
    選択肢は番号と文を必ず付ける（番号だけの答えでも通じるように）。"""
    options = [str(o) for o in question.get("options") or []]
    listed = " ".join(f"{i}. {o}" for i, o in enumerate(options, 1))
    text = f"{parent.get('text', '')}\n（質問：{question.get('question', '')}／選択肢：{listed}／人の答え：{answer}）"
    words = list(parent.get("human_words") or [parent.get("text", "")]) + [answer]
    k = option_number(answer, len(options))
    if k:
        words.append(options[k - 1])                      # 人が番号で選んだ選択肢の文
    return text, words
