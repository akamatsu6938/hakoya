"""見直し（★11/6）：② 修正の流れ・④ 検品のサーバー側で、同時に起きたときにファイルを壊さないか・人の直しを勝手に消さないか。

架空の家（tests/test_fix_requests.py の「試験の家F」・tests/test_finding_decisions_and_answers.py の「物件T」）だけを使う。
Claude は呼ばない（_run_agent を偽物にする）。2つの thread の間の取り方は、待ち合わせ（Event）で決めて再現する。
鍵があれば片方が待つので、待ち合わせは時間切れで先へ進む（そのぶん1〜2秒かかる）。
- 窓口の鍵（server.MODEL_LOCK）：表の直しと依頼の承認が同時に overrides.json を書いても、片方が消えない。
  承認の組み直しの直後に、前から走っていた組み直しが古い修正で model.glb を上書きして、本当の直しを「形が変わらない」と取り消さない。
- 修正の番号の使い回し：取り消したあとに同じ番号の修正ができても、前に承認した案を「承認のまま」にしない（fix_requests）。
- 古い model.glb（組み直しに失敗した・別のプロセスが修正を書いた）を「前」の形にして、直しを取り消さない（shape_diff）。
- findings.json：決定が古い一覧を読んだあとに検品が新しい一覧を書いても、新しい検品の結果を消さない（findings.LOCK）。
- answers.json：2つの答えが同時に届いても、両方残る（answers.LOCK）。
"""
from __future__ import annotations

import json
import os
import threading

from core import answers as an
from core import findings as fd
from core import overrides as ov
from core import shape_diff as sd
from tests.test_finding_decisions_and_answers import AT, QUESTION
from tests.test_finding_decisions_and_answers import alias as finding_alias  # noqa: F401  （架空の物件T の用意）
from tests.test_fix_requests import (ALIAS, A, NG, URL, W9_TO_MAJORITY, _fake, _wait, client, data,  # noqa: F401
                                     template)

WAIT_S = 1.5          # 鍵で待たされた側が、待ち合わせをあきらめて先へ進むまでの秒数


def _approve_w9(client, monkeypatch) -> None:
    """偽の Claude が W9 の厚みを内壁の多数派（130）にそろえる案を出し、R1 が終わるまで待つ。"""
    _fake(monkeypatch, plan=[W9_TO_MAJORITY])
    client.post(URL, json={"text": "ほかとそろえて", "connection": "claude_code", "ids": ["W9"]})
    _wait(client, "R1")


def _run(*fns) -> None:
    ts = [threading.Thread(target=f) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)


# ---------------------------------------------------------------- 窓口の鍵（server.MODEL_LOCK）

def test_table_edit_during_a_fix_approval_keeps_both_fixes(client, data, monkeypatch):
    """依頼の承認が overrides.json を読んでから書くまでの間に、表の直しが届いても、両方の修正が残り、番号もぶつからない。
    （鍵がなかったとき：どちらも OV1 になり、表の直しが消えた）"""
    _approve_w9(client, monkeypatch)
    a_saving, t_saving = threading.Event(), threading.Event()
    real_save = ov.save

    def save(alias, items):
        if items and items[-1]["target"] == "W9":      # 依頼の承認が書く直前：表の直しが来るのを待ってみる
            a_saving.set()
            t_saving.wait(WAIT_S)
        elif items and items[-1]["target"] == "O1":
            t_saving.set()
        real_save(alias, items)
    monkeypatch.setattr(ov, "save", save)
    out = {}

    def approve():
        out["fix"] = client.post(f"{URL}/R1/approve", json={"index": 0})

    def table():
        a_saving.wait(30)
        out["table"] = client.post(f"/api/overrides/{A}", json={"target": "O1", "field": "offset_mm", "to": 310.0, "reason": "表で"})
    _run(approve, table)
    assert out["fix"].status_code == 200 and out["table"].status_code == 200, (out["fix"].text, out["table"].text)
    assert out["fix"].json()["changed"] is True
    items = ov.load(ALIAS)
    assert sorted((x["id"], x["target"]) for x in items) == [("OV1", "W9"), ("OV2", "O1")]
    assert out["table"].json()["override"]["id"] == "OV2"


def test_rebuild_that_started_earlier_does_not_undo_a_real_fix(client, data, monkeypatch):
    """承認より前に始まった組み直し（分け目を戻す）が、承認の組み直しの後に古い修正で model.glb を書いても、
    承認は「形が変わらない」と取り違えて直しを取り消さない（鍵がなかったとき：W9 の厚みの直しが no_change で消えた）。"""
    from app import server
    from core import export
    _approve_w9(client, monkeypatch)
    b_read, a_built, b_wrote = threading.Event(), threading.Event(), threading.Event()
    mine = threading.local()
    real_make_model, real_sig, real_write = export.make_model, sd.signature_of, server.write_outputs

    def make_model(alias, company=None):
        out = real_make_model(alias, company)
        if not b_read.is_set() and not ov.load(alias):   # 分け目を戻す側（修正をまだ読んでいない）
            mine.is_b = True
            b_read.set()
            a_built.wait(WAIT_S)                         # 承認が組み直すまで待ってから、古い修正で書く
        return out

    def write_outputs(alias, *k, **kw):
        out = real_write(alias, *k, **kw)
        if getattr(mine, "is_b", False):
            b_wrote.set()
        return out
    calls = []

    def signature_of(alias):
        calls.append(alias)
        if len(calls) == 2:                              # 承認の「後」の名札：分け目を戻す側が書くのを待ってみる
            a_built.set()
            b_wrote.wait(WAIT_S)
        return real_sig(alias)
    monkeypatch.setattr(export, "make_model", make_model)
    monkeypatch.setattr(server, "write_outputs", write_outputs)
    monkeypatch.setattr(sd, "signature_of", signature_of)
    out = {}

    def reset_split():
        out["split"] = client.delete(f"/api/split/{A}")

    def approve():
        b_read.wait(30)
        out["fix"] = client.post(f"{URL}/R1/approve", json={"index": 0})
    _run(reset_split, approve)
    assert out["split"].status_code == 200 and out["fix"].status_code == 200, (out["split"].text, out["fix"].text)
    res = out["fix"].json()
    assert res["changed"] is True and res["request"]["proposals"][0]["state"] == "approved"
    assert [(x["target"], x["to"]) for x in ov.load(ALIAS)] == [("W9", 130.0)]   # 直しは残る


def test_rebuilds_from_the_screen_never_overlap(client, data, monkeypatch):
    """画面の窓口からの組み直しは、同じ HAKOYA の中では1つずつ（2つ重なると STL を消し合って失敗し、直しを取り消していた）。"""
    import time

    from app import server
    real_write = server.write_outputs
    busy, most = [0], [0]
    count = threading.Lock()

    def write_outputs(alias, *k, **kw):
        with count:
            busy[0] += 1
            most[0] = max(most[0], busy[0])
        try:
            time.sleep(0.2)                              # 重なりやすくする
            return real_write(alias, *k, **kw)
        finally:
            with count:
                busy[0] -= 1
    monkeypatch.setattr(server, "write_outputs", write_outputs)
    codes = []
    calls = [lambda: codes.append(client.delete(f"/api/split/{A}").status_code),
             lambda: codes.append(client.delete(f"/api/split/{A}").status_code),
             lambda: codes.append(client.post(f"/api/overrides/{A}", json={"target": "O1", "field": "offset_mm", "to": 310.0,
                                                                           "reason": "表で"}).status_code)]
    _run(*calls)
    assert codes == [200, 200, 200] and most[0] == 1


# ---------------------------------------------------------------- 修正の番号の使い回し（core/fix_requests._still_recorded）

def test_undone_fix_is_not_mixed_up_with_a_new_fix_of_the_same_number(client, data, monkeypatch):
    """承認した案の修正を直しの履歴から取り消し、一覧を読み直す前に表で直すと、表の直しが同じ OV1 になる。
    それでも案は「承認」に見えず、「まだ」に戻る（前は承認のままで、「取り消す」を押すと表の直しが消えるところだった）。"""
    _approve_w9(client, monkeypatch)
    res = client.post(f"{URL}/R1/approve", json={"index": 0}).json()
    p = res["request"]["proposals"][0]
    assert p["state"] == "approved" and p["override_id"] == "OV1" and p["override_at"] == res["override"]["at"]
    assert client.delete(f"/api/overrides/{A}/OV1").status_code == 200          # 一覧（GET）は読み直さない
    table = client.post(f"/api/overrides/{A}", json={"target": "O1", "field": "offset_mm", "to": 310.0, "reason": "表で"}).json()
    assert table["override"]["id"] == "OV1"                                      # 番号は使い回される（core/overrides.add）
    p = client.get(URL).json()["requests"][0]["proposals"][0]
    assert p["state"] == "pending" and p["override_id"] is None and "取り消しました" in p["note"]
    assert [x["target"] for x in ov.load(ALIAS)] == ["O1"]                      # 表の直しはそのまま


def test_recorded_fix_is_matched_by_time_and_content():
    """承認した案と記録の修正は、番号・時刻・部材・項目・値で同じものか確かめる。
    override_at のない記録（前の版で承認したもの）は、今までどおり番号だけで比べる。"""
    from core import fix_requests as fr
    at = "2026-11-06T10:00:00+09:00"
    current = {"OV1": {"id": "OV1", "at": at, "target": "W9", "field": "thickness_mm", "to": 130.0}}
    mine = {"override_id": "OV1", "override_at": at, "target": "W9", "field": "thickness_mm", "to": 130.0}
    assert fr._still_recorded(mine, current)
    assert fr._still_recorded({"override_id": "OV1"}, current)                               # 前の版の記録
    assert not fr._still_recorded({**mine, "override_at": "2026-11-06T09:59:59+09:00"}, current)
    assert not fr._still_recorded({**mine, "target": "O1", "field": "offset_mm", "to": 310.0}, current)   # 同じ秒の別の直し
    assert not fr._still_recorded({**mine, "override_id": "OV2"}, current)


# ---------------------------------------------------------------- 古い model.glb を「前」の形にしない（core/shape_diff.signature_of）

def test_old_model_is_not_used_to_undo_a_real_fix(client, data, monkeypatch):
    """承認した直しを取り消したあとの組み直しが失敗して、model.glb が直したままの形で残っても、
    同じ案をもう一度承認したときに「形が変わらない」と取り違えて取り消さない（glb が overrides.json より古ければ比べない）。"""
    _approve_w9(client, monkeypatch)
    assert client.post(f"{URL}/R1/approve", json={"index": 0}).json()["changed"] is True
    glb = data / "out" / ALIAS / "model" / "model.glb"
    ov.undo(ALIAS, "OV1")                          # 取り消した（組み直しは失敗した、か、別のプロセスが取り消した）
    made = glb.stat().st_mtime_ns
    os.utime(ov.overrides_path(ALIAS), ns=(made + 10**9, made + 10**9))   # overrides.json は glb より後に書かれた
    assert sd.signature_of(ALIAS) is None and sd.signature(glb) is not None
    assert client.get(URL).json()["requests"][0]["proposals"][0]["state"] == "pending"
    res = client.post(f"{URL}/R1/approve", json={"index": 0}).json()
    assert res["changed"] is True and res["changed_parts"] == ["?"]           # 比べられない → 変わったとみなす
    assert [(x["target"], x["to"]) for x in ov.load(ALIAS)] == [("W9", 130.0)]
    assert sd.signature_of(ALIAS) is not None                                  # 組み直したので、また比べられる


def test_fix_that_does_not_change_the_model_is_still_undone(client, monkeypatch):
    """時刻の確かめを足しても、ふつうの承認（前の名札 → 承認 → 組み直し → 後の名札）は両方の名札が取れ、形が変わらない直しは
    今までどおり取り消す。内壁 W5 の上端を天井（FL 624＋2400＝GL 3024）より上にしても、模型の形は変わらない（★試した：名札が同じ）。"""
    _fake(monkeypatch, plan=[("propose_override", {"target": "W5", "field": "height", "value_source": "number", "number": 3100,
                                                   "reason": "上端を上げる"})])
    client.post(URL, json={"text": "W5 の上端を 3100 にして", "connection": "claude_code", "ids": ["W5"]})
    assert _wait(client, "R1")["proposals"][0]["to"] == 3100.0
    res = client.post(f"{URL}/R1/approve", json={"index": 0}).json()
    assert res["changed"] is False and res["changed_parts"] == []              # 名札が取れなければ ["?"]（変わった）になる
    assert res["request"]["proposals"][0]["state"] == "no_change" and ov.load(ALIAS) == []


def test_same_file_time_counts_as_up_to_date(data):
    """glb と building.json・overrides.json の時刻が同じ（時刻の細かさが粗いファイルの置き場所など）なら、古いとはみなさない。
    glb のほうが古いときだけ比べない。"""
    d = data / "out" / ALIAS
    raw = json.loads((d / "building.json").read_text(encoding="utf-8"))
    ov.add(ALIAS, raw, "O1", "offset_mm", 310.0, "human:table", "表で")        # overrides.json を作る
    t = (d / "model" / "model.glb").stat().st_mtime_ns
    for name in ("building.json", "overrides.json", "model/model.glb"):
        os.utime(d / name, ns=(t, t))
    assert sd.signature_of(ALIAS) is not None
    os.utime(d / "overrides.json", ns=(t + 10**9, t + 10**9))     # 1 秒あと（NTFS の時刻は 100ns ごとなので、1ns では変わらない）
    assert sd.signature_of(ALIAS) is None


# ---------------------------------------------------------------- findings.json・answers.json の「読んで足して書く」

def test_decision_does_not_overwrite_a_new_inspection(finding_alias, monkeypatch):
    """指摘の決定が古い一覧を読んだあとに、検品が新しい一覧を書いても、新しい検品の結果は消えない
    （鍵がなかったとき：決定が古い一覧＋決定を書き、新しい検品の結果が丸ごと消えた）。"""
    old = fd.load(finding_alias)
    new = {**old, "at": "2026-11-06T09:00:00+09:00", "summary": "新しい検品", "findings": [], "code_findings": []}
    loaded, saved = threading.Event(), threading.Event()
    real_load = fd.load

    def load(alias):
        rep = real_load(alias)
        if threading.current_thread().name == "decide":
            loaded.set()
            saved.wait(WAIT_S)                    # 検品が書くのを待ってみる
        return rep
    monkeypatch.setattr(fd, "load", load)

    def decide():
        fd.decide(finding_alias, "F2", "reject")

    def inspection():
        loaded.wait(30)
        fd.save(finding_alias, new)
        saved.set()
    ts = [threading.Thread(target=decide, name="decide"), threading.Thread(target=inspection)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert json.loads(fd.findings_path(finding_alias).read_text(encoding="utf-8"))["summary"] == "新しい検品"


def test_two_answers_at_once_are_both_kept(finding_alias, monkeypatch):
    """2つの質問への答えが同時に届いても、answers.json に両方残る（鍵がなかったとき：片方が消えた）。"""
    both = threading.Barrier(2, timeout=WAIT_S)
    real_load = an.load

    def load(alias, path=None):
        items = real_load(alias, path)
        try:
            both.wait()                           # 2つとも書く前に読んだ所で待ち合わせてみる
        except threading.BrokenBarrierError:
            pass
        return items
    monkeypatch.setattr(an, "load", load)
    _run(lambda: an.add(finding_alias, "Q1", QUESTION, ["図面", "glb"], "図面", AT),
         lambda: an.add(finding_alias, "F2", "この窓は開けたままにしますか？", ["開けたまま", "ふさぐ"], "ふさぐ", AT))
    monkeypatch.setattr(an, "load", real_load)
    assert sorted(a["question_id"] for a in an.load(finding_alias)) == ["F2", "Q1"]


# ---------------------------------------------------------------- 質問への答え：混んでいて頼めなかったときは「答えた」にしない

def test_busy_answer_leaves_the_question_unanswered(client, monkeypatch):
    """同じ物件で2つ考えている間に質問に答えると 409。質問は「答えた」にならず、空いてからもう一度答えられる。"""
    _fake(monkeypatch, plan=[("ask_question", {"question": "どちらの壁ですか？", "options": ["W5", "W9"]})])
    client.post(URL, json={"text": "内壁をそろえて", "connection": "claude_code", "ids": ["W5", "W9"]})
    _wait(client, "R1")
    gate = threading.Event()
    _fake(monkeypatch, gate=gate)
    for _ in range(2):
        assert client.post(URL, json={"text": "見て", "kind": "text"}).status_code == 200      # R2・R3 が考えている
    r = client.post(f"{URL}/R1/answer", json={"question_id": "Q1", "answer": "2"})
    assert r.status_code == 409 and "前の依頼が終わってから" in r.json()["detail"]
    q = next(x for x in client.get(URL).json()["requests"] if x["id"] == "R1")["questions"][0]
    assert "answered" not in q and "child_id" not in q
    gate.set()
    _wait(client, "R2")
    _wait(client, "R3")
    r = client.post(f"{URL}/R1/answer", json={"question_id": "Q1", "answer": "2"})
    assert r.status_code == 200 and r.json()["parent_id"] == "R1"


# ---------------------------------------------------------------- 個人情報：失敗の文に、場所や出してはいけない言葉を残さない

def test_failure_text_never_keeps_paths_or_forbidden_words(client, data, monkeypatch, capsys):
    """Claude の呼び出しが失敗したとき：思いがけないエラーは種類だけ。出してはいけない言葉が入った文は記録に書かず、
    依頼は次に読んだとき「途中で止まった」になる（言葉は記録・画面・黒い画面のどこにも残らない）。"""
    from core.ai import lasso as al

    async def broken(*a, **k):
        raise OSError(r"C:\Users\someone\架空の家\model.glb が開けません")
    monkeypatch.setattr(al, "_run_agent", broken)
    client.post(URL, json={"text": "見て", "kind": "text"})
    got = _wait(client, "R1")
    assert got["status"] == "error" and got["error"] == "思いがけないエラーです（OSError）"

    async def leaks(*a, **k):
        raise ValueError(f"{NG}様の家の壁が見つかりません")
    monkeypatch.setattr(al, "_run_agent", leaks)
    client.post(URL, json={"text": "見て", "kind": "text"})
    got = _wait(client, "R2")
    assert got["status"] == "interrupted"
    saved = (data / "out" / ALIAS / "fix_requests.json").read_text(encoding="utf-8")
    assert NG not in saved and NG not in client.get(URL).text
    shown = capsys.readouterr()
    assert NG not in shown.out + shown.err and "someone" not in shown.out + shown.err
    assert "修正の依頼 R2 を記録できませんでした（PrivacyError）" in shown.err


def test_group_label_and_parent_id_are_checked_before_writing(client, data):
    """囲みの名前（groups[].label）と parent_id に出してはいけない言葉があれば、書く前に 400 で断る
    （★11/6 見直し：前は一言だけを確かめていて、記録を書くときに PrivacyError の 500 になった）。何も記録しない。"""
    for body in ({"text": "そろえて", "connection": "claude_code", "groups": [{"label": NG, "ids": ["W9"]}]},
                 {"text": "そろえて", "connection": "claude_code", "ids": ["W9"], "parent_id": NG}):
        r = client.post(URL, json=body)
        assert r.status_code == 400 and NG not in r.text, r.text
    assert client.get(URL).json()["requests"] == []
    saved = data / "out" / ALIAS / "fix_requests.json"
    assert not saved.exists() or NG not in saved.read_text(encoding="utf-8")
