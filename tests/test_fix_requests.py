"""修正の依頼（★11/5 夜。core/fix_requests.py・core/shape_diff.py・/api/fix の窓口・core/ai/lasso.run_fix）を、架空の家で確かめる。

Claude は呼ばない（core/ai/lasso._run_agent を偽物にする。偽物は決めた道具を ctx に対して呼ぶだけ）。
- 依頼は裏で動き、答えは物件ごとのファイルに残る。HAKOYA を起動し直すと、動いていた依頼は「途中で止まった」になる。
- 同じ物件で同時に動かすのは2つまで。質問に答えると、選択肢の番号と文を付けた新しい依頼になる。
- 承認は数値をサーバーが計算し直し、二度はできない。模型の形が変わらない直しは取り消す（形の名札で比べる）。
- 複数の囲み（ids と groups）・文字で直す（kind=text）・モデルの選択（Opus 5.5／Sonnet 5.5）。
家・物件の名前・出してはいけない言葉は、どれも架空。
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from urllib.parse import quote

import pytest

from core import fix_requests as fr
from core import lasso as geo
from core import paths, privacy
from core import shape_diff as sd
from core.ai import lasso as al
from core.ai.tools import call_tool
from core.export import write_outputs
from core.rules import default_company
from tests.test_complex_shapes import _house

ALIAS = "試験の家F"          # 架空の家（組み立て済み）
A = quote(ALIAS)
NG = "架空鈴木"               # 出してはいけない言葉の試し（架空）
URL = f"/api/fix/{A}/requests"


def _building() -> dict:
    """6m×4m の平屋（窓4つ）。内壁 W9 だけ厚みが 145（もう1枚の内壁 W5 は 130）。"""
    b = _house([(0, 0), (6000, 0), (6000, 4000), (0, 4000)])
    b["source"] = {"company_rules": default_company(), "glb": f"{ALIAS}.glb"}
    b["ground"] = {"gl_mm": 0, "foundation_top_mm": 480}
    b["walls"].append({"id": "W9", "level": "1F", "kind": "interior", "start": [1000, 80], "end": [1000, 2000],
                       "thickness_mm": 145, "bottom_mm": 624, "height": 3024})
    return b


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    root = tmp_path_factory.mktemp("hakoya_fix") / "data"
    d = root / "out" / ALIAS
    d.mkdir(parents=True)
    (d / "building.json").write_text(json.dumps(_building(), ensure_ascii=False), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paths, "DATA_DIR", root)
        write_outputs(ALIAS, bambu_enabled=False)
    (root / "local_paths.yaml").write_text(f"ng_words:\n  - {NG}\n{ALIAS}: {{}}\n", encoding="utf-8")
    return root


class FakeClaude:
    """_run_agent の代わり。plan の道具を順に呼び、受け取った指示を覚える。gate を渡すと、それが開くまで待つ。"""

    def __init__(self, plan=None, message="案を出しました。", gate: threading.Event | None = None):
        self.plan = plan or []
        self.message = message
        self.gate = gate
        self.calls: list[dict] = []

    async def __call__(self, ctx, prompt, conf, m=None, role=al.ROLE):
        import asyncio
        self.calls.append({"prompt": prompt, "model": (m or {}).get("model"), "role": role, "human_text": ctx.human_text})
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.02)
        for name, args in self.plan:
            call_tool(ctx, name, dict(args))
        return self.message, 0.12


@pytest.fixture
def data(template, tmp_path, monkeypatch):
    root = tmp_path / "data"
    shutil.copytree(template, root)
    monkeypatch.setattr(paths, "DATA_DIR", root)
    monkeypatch.setattr(paths, "MODE", "own")
    monkeypatch.setattr(paths, "LOCAL_PATHS", root / "local_paths.yaml")
    monkeypatch.setattr(privacy, "LOCAL_PATHS", root / "local_paths.yaml")
    for k in ("HAKOYA_EDITION", "HAKOYA_FAKE_AI", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield root
    _wait_idle()
    fr.ACTIVE.clear()


@pytest.fixture
def client(data, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    monkeypatch.setattr(server, "_aliases", lambda: [ALIAS])
    monkeypatch.setattr(server, "write_outputs", lambda a, *k, **kw: write_outputs(a, bambu_enabled=False))
    return TestClient(server.app)


def _fake(monkeypatch, **kw) -> FakeClaude:
    f = FakeClaude(**kw)
    monkeypatch.setattr(al, "_run_agent", f)
    return f


def _wait_idle(timeout=15.0):
    end = time.time() + timeout
    while fr.ACTIVE and time.time() < end:
        time.sleep(0.02)


def _wait(client, rid, timeout=15.0) -> dict:
    """依頼が終わるまで、画面と同じく GET で読み直して待つ。"""
    end = time.time() + timeout
    while time.time() < end:
        req = next(r for r in client.get(URL).json()["requests"] if r["id"] == rid)
        if req["status"] != "running":
            return req
        time.sleep(0.03)
    raise AssertionError(f"{rid} が終わりません")


def box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


W9_TO_MAJORITY = ("propose_override", {"target": "W9", "field": "thickness_mm", "value_source": "majority:interior",
                                       "reason": "ほかの内壁にそろえる"})


# ---------------------------------------------------------------- 依頼は裏で動き、物件ごとに残る

def test_request_runs_in_background_and_answer_is_kept(client, data, monkeypatch):
    f = _fake(monkeypatch, plan=[W9_TO_MAJORITY])
    r = client.post(URL, json={"text": "この壁厚だけおかしい。ほかとそろえて", "connection": "claude_code",
                               "polygon": box(800, 500, 1200, 1500)})
    assert r.status_code == 200, r.text
    req = r.json()
    assert req["id"] == "R1" and req["status"] == "running" and req["kind"] == "lasso" and req["model_label"] == "Opus 5.5"
    assert [x["id"] for x in req["selection"]] == ["W9"] and req["selection"][0]["part"] == "家（1階の床・内壁）"
    assert req["groups"] == [{"label": "①", "ids": ["W9"], "partial": ["W9"], "source": "plan", "view": None}]
    done = _wait(client, "R1")
    assert done["status"] == "done" and done["message"] == "案を出しました。" and done["cost_usd"] == 0.12
    p = done["proposals"][0]
    assert (p["target"], p["from"], p["to"], p["state"], p["override_id"]) == ("W9", 145, 130.0, "pending", None)   # 130 はコードの計算
    assert f.calls[0]["model"] == "claude-opus-5-5" and "W9（内壁）" in f.calls[0]["prompt"]
    assert "模型の部品：家（1階の床・内壁）" in f.calls[0]["prompt"]
    saved = json.loads((data / "out" / ALIAS / "fix_requests.json").read_text(encoding="utf-8"))
    assert saved["next_id"] == 2 and saved["requests"][0]["status"] == "done"            # 物件を開き直しても出る
    log = [json.loads(x) for x in (data / "out" / ALIAS / "lasso_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["request"] == "R1" and log[-1]["model"] == "claude-opus-5-5" and log[-1]["selection"] == ["W9"]


def test_restart_marks_running_requests_interrupted(client, data):
    req = fr.create(ALIAS, text="窓を高く", kind="text", model="opus", model_label="Opus 5.5", connection="claude_code",
                    selection=[], groups=[])
    assert client.get(URL).json()["requests"][0]["status"] == "running"
    fr.ACTIVE.clear()                                           # HAKOYA を起動し直した（この HAKOYA は動かしていない）
    got = client.get(URL).json()["requests"][0]
    assert got["id"] == req["id"] and got["status"] == "interrupted" and "途中で止まりました" in got["error"]
    assert json.loads(fr.path(ALIAS).read_text(encoding="utf-8"))["requests"][0]["status"] == "interrupted"


def test_at_most_two_running_per_property(client, monkeypatch):
    gate = threading.Event()
    _fake(monkeypatch, gate=gate)
    body = {"text": "W9 を直して", "connection": "claude_code", "kind": "text"}
    assert client.post(URL, json=body).status_code == 200
    assert client.post(URL, json=body).status_code == 200
    r = client.post(URL, json=body)
    assert r.status_code == 409 and "前の依頼が終わってから" in r.json()["detail"]
    d = client.delete(f"{URL}/R1")
    assert d.status_code == 409 and "考えている途中" in d.json()["detail"]   # 考えている途中は外さない
    gate.set()
    assert _wait(client, "R1")["status"] == "done" and _wait(client, "R2")["status"] == "done"
    assert client.post(URL, json=body).status_code == 200           # 終われば、また頼める


def test_refuses_while_inspecting_and_bad_input(client, monkeypatch):
    from app import server
    _fake(monkeypatch)
    monkeypatch.setitem(server.RUNS, ALIAS, {"running": True})
    r = client.post(URL, json={"text": "直して", "kind": "text"})
    assert r.status_code == 409 and "検品中" in r.json()["detail"]
    monkeypatch.setitem(server.RUNS, ALIAS, {"running": False})
    assert client.post(URL, json={"text": "  ", "kind": "text"}).status_code == 400
    assert client.post(URL, json={"text": "直して", "kind": "text", "connection": "off"}).status_code == 400
    r = client.post(URL, json={"text": "直して", "kind": "text", "model": "haiku"})
    assert r.status_code == 400 and "opus" in r.json()["detail"]
    r = client.post(URL, json={"text": f"{NG}様の家の壁", "kind": "text"})
    assert r.status_code == 400 and NG not in r.json()["detail"]                 # 出してはいけない言葉は文に出さない
    r = client.post(URL, json={"text": "直して", "polygon": box(4000, 500, 5000, 1500)})   # 部屋の中だけ
    assert r.status_code == 400 and "部材がありません" in r.json()["detail"]
    assert not fr.load(ALIAS)


# ---------------------------------------------------------------- 質問に答える（選択肢の番号と文を付けて頼み直す）

def test_answer_adds_numbered_options_and_only_once(client, monkeypatch):
    f = _fake(monkeypatch, plan=[("ask_question", {"question": "どちらの壁ですか？", "options": ["W5", "W9"]})])
    client.post(URL, json={"text": "内壁の厚みをそろえて", "connection": "claude_code", "kind": "lasso",
                           "ids": ["W5", "W9"], "groups": [{"label": "①", "ids": ["W5", "W9"]}]})
    parent = _wait(client, "R1")
    assert parent["questions"][0]["options"] == ["W5", "W9"]
    f.plan = [W9_TO_MAJORITY]
    r = client.post(f"{URL}/R1/answer", json={"question_id": "Q1", "answer": "2で試す"})
    assert r.status_code == 200, r.text
    child = r.json()
    assert child["parent_id"] == "R1" and child["kind"] == "lasso" and [x["id"] for x in child["selection"]] == ["W5", "W9"]
    assert "（質問：どちらの壁ですか？／選択肢：1. W5 2. W9／人の答え：2で試す）" in child["text"]
    done = _wait(client, child["id"])
    assert done["proposals"][0]["target"] == "W9"
    sent = f.calls[-1]
    assert "選択肢：1. W5 2. W9" in sent["prompt"] and "人の答え：2で試す" in sent["prompt"]
    # 人の数値として使えるのは、人が書いた文と、番号で選んだ選択肢の文だけ（「1.」「2.」の番号や選ばなかった選択肢は入れない）
    assert sent["human_text"] == "内壁の厚みをそろえて\n2で試す\nW9"
    got = next(x for x in client.get(URL).json()["requests"] if x["id"] == "R1")
    assert got["questions"][0]["answered"] == "2で試す" and got["questions"][0]["child_id"] == child["id"]
    again = client.post(f"{URL}/R1/answer", json={"question_id": "Q1", "answer": "1"})
    assert again.status_code == 409 and "もう答えました" in again.json()["detail"]
    assert client.post(f"{URL}/R1/answer", json={"question_id": "Q9", "answer": "1"}).status_code == 404


def test_option_numbers():
    assert [fr.option_number(a, 3) for a in ("2", "2で試す", "2. W5", "２番", "3つ目", "1.")] == [2, 2, 2, 2, 3, 1]
    assert [fr.option_number(a, 3) for a in ("2.5mm", "200にしたい", "4", "0", "図面だと 450", "2,000")] == [None] * 6


# ---------------------------------------------------------------- 承認（二度はできない・形が変わらない直しは取り消す）

def test_approve_once_and_undo_returns_to_pending(client, monkeypatch):
    _fake(monkeypatch, plan=[W9_TO_MAJORITY])
    client.post(URL, json={"text": "ほかとそろえて", "connection": "claude_code", "ids": ["W9"]})
    _wait(client, "R1")
    r = client.post(f"{URL}/R1/approve", json={"index": 0})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["changed"] is True and "floor1" in res["changed_parts"] and res["override"]["to"] == 130
    p = res["request"]["proposals"][0]
    assert p["state"] == "approved" and p["override_id"] == res["override"]["id"] and (p["from"], p["to"]) == (145, 130)
    assert res["override"]["origin"] == "lasso"
    w9 = next(w for w in client.get(f"/api/building/{A}").json()["building"]["walls"] if w["id"] == "W9")
    assert w9["thickness_mm"] == 130
    again = client.post(f"{URL}/R1/approve", json={"index": 0})
    assert again.status_code == 409 and "もう承認しました" in again.json()["detail"]
    assert client.post(f"{URL}/R1/reject", json={"index": 0}).status_code == 409
    assert client.post(f"{URL}/R1/approve", json={"index": 5}).status_code == 404
    assert len(client.get(f"/api/overrides/{A}").json()) == 1
    # 直しの履歴から取り消すと、案は「まだ」に戻る（もう一度承認できる）
    assert client.delete(f"/api/overrides/{A}/{res['override']['id']}").status_code == 200
    p = client.get(URL).json()["requests"][0]["proposals"][0]
    assert p["state"] == "pending" and p["override_id"] is None and "取り消しました" in p["note"]


def test_fix_that_does_not_change_the_model_is_undone(client, data, monkeypatch):
    """内壁 W5 の上端を天井（FL 624＋2400＝GL 3024）より上にしても、内壁は天井で切るので形に出ない（★試した：名札がぴったり同じ）
    → 取り消して知らせる。★11/6 見直し：前は「天井に届く窓がない壁の窓の上の帯」で試していたが、① がその案を出さないように
    した（core/ai/tools._band_possible）ので、偽の Claude の案が作られずに落ちていた。"""
    _fake(monkeypatch, plan=[("propose_override", {"target": "W5", "field": "height", "value_source": "number", "number": 3100,
                                                   "reason": "上端を上げる"})])
    client.post(URL, json={"text": "W5 の上端を 3100 にして", "connection": "claude_code", "ids": ["W5"]})
    assert _wait(client, "R1")["proposals"][0]["state"] == "pending"
    res = client.post(f"{URL}/R1/approve", json={"index": 0}).json()
    assert res["changed"] is False and res["changed_parts"] == []
    p = res["request"]["proposals"][0]
    assert p["state"] == "no_change" and p["note"] == "この直しでは模型が変わりませんでした（取り消しました）"
    assert client.get(f"/api/overrides/{A}").json() == []                  # 記録から外した
    assert client.post(f"{URL}/R1/approve", json={"index": 0}).status_code == 409


def test_reject_and_failed_proposal(client, monkeypatch):
    _fake(monkeypatch, plan=[W9_TO_MAJORITY, ("propose_override", {"target": "W5", "field": "thickness_mm",
                                                                    "value_source": "majority:interior", "reason": "そろえる"})])
    client.post(URL, json={"text": "内壁をそろえて", "connection": "claude_code", "ids": ["W5", "W9"]})
    _wait(client, "R1")
    assert client.post(f"{URL}/R1/reject", json={"index": 1}).json()["request"]["proposals"][1]["state"] == "rejected"
    assert client.post(f"{URL}/R1/approve", json={"index": 1}).status_code == 409
    ok = client.post(f"{URL}/R1/approve", json={"index": 0}).json()       # 1つ承認しても、もう1つの案はそのまま
    assert [p["state"] for p in ok["request"]["proposals"]] == ["approved", "rejected"]


def test_delete_request(client, monkeypatch):
    _fake(monkeypatch)
    client.post(URL, json={"text": "見て", "kind": "text"})
    _wait(client, "R1")
    assert client.delete(f"{URL}/R1").json() == {"removed": "R1"}
    assert client.get(URL).json()["requests"] == []
    assert client.delete(f"{URL}/R1").status_code == 404
    client.post(URL, json={"text": "見て", "kind": "text"})
    assert client.post(URL, json={"text": "見て", "kind": "text"}).json()["id"] == "R3"   # 番号は使い回さない


# ---------------------------------------------------------------- 複数の囲み・文字で直す・モデル

def test_ids_and_groups_are_checked_and_explained(client, monkeypatch):
    f = _fake(monkeypatch)
    body = {"text": "①は厚みを内壁にそろえて、②は軒をもう少し出して", "connection": "claude_code",
            "ids": ["W9", "W5", "Y1:S"],
            "groups": [{"label": "①", "ids": ["W9"], "source": "plan"},
                       {"label": "②", "ids": ["W5", "Y1:S"], "partial": ["W5"], "source": "3d", "view": "S"}]}
    r = client.post(URL, json=body)
    assert r.status_code == 200, r.text
    req = r.json()
    assert [x["id"] for x in req["selection"]] == ["W9", "W5", "Y1:S"]
    assert [x["part"] for x in req["selection"]] == ["家（1階の床・内壁）", "家（1階の床・内壁）", "屋根"]
    assert [g["label"] for g in req["groups"]] == ["①", "②"] and req["groups"][1]["view"] == "S"
    _wait(client, "R1")
    prompt = f.calls[0]["prompt"]
    assert "2 か所囲み" in prompt and "①（平面図で囲んだもの）" in prompt and "②（南から見た 3D の画面で囲んだもの）" in prompt
    assert prompt.index("W9（内壁）") < prompt.index("②（") < prompt.index("Y1:S（軒（南））")
    assert "W5（内壁）" in prompt and "※囲みに一部だけかかった部材" in prompt and "eave_mm" in prompt
    r = client.post(URL, json={**body, "ids": ["W99"], "groups": [{"label": "①", "ids": ["W99"]}]})
    assert r.status_code == 400 and "W99 という部材はありません" in r.json()["detail"]


def test_text_request_without_lasso(client, monkeypatch):
    f = _fake(monkeypatch, plan=[W9_TO_MAJORITY])
    r = client.post(URL, json={"text": "厚い内壁をほかとそろえて", "kind": "text", "ids": ["O1", "W99"]})
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["selection"]] == ["O1"] and r.json()["groups"] == []   # 知らない番号は黙って外す
    done = _wait(client, "R1")
    assert done["kind"] == "text" and done["proposals"][0]["target"] == "W9"
    c = f.calls[0]
    assert c["role"] == al.TEXT_ROLE and "囲まずに、文字だけで" in c["prompt"] and "get_building" in c["prompt"]
    assert "いま画面で選んでいる部材" in c["prompt"] and "O1（窓）" in c["prompt"]
    res = client.post(f"{URL}/R1/approve", json={"index": 0}).json()
    assert res["override"]["origin"] == "text"
    client.post(URL, json={"text": "南の軒を 600 出して", "kind": "text"})
    _wait(client, "R2")
    assert "いま画面で選んでいる部材" not in f.calls[1]["prompt"] and "eave_mm" in f.calls[1]["prompt"]   # 軒の話なので軒の決まりも


def test_model_choice(client, monkeypatch):
    f = _fake(monkeypatch)
    r = client.post(URL, json={"text": "直して", "kind": "text", "model": "sonnet"})
    assert r.json()["model"] == "sonnet" and r.json()["model_label"] == "Sonnet 5.5"
    _wait(client, "R1")
    assert f.calls[0]["model"] == "claude-sonnet-5-5"
    conf = al.load_ai_models()
    assert conf["api_key"]["lasso"]["model"] == "claude-opus-5-5"            # API キーの既定も Opus 5.5（★11/5 夜）
    for conn in ("claude_code", "api_key"):
        assert al.model_spec(conf, conn, None)["model"] == "claude-opus-5-5"
        assert al.model_spec(conf, conn, "sonnet") == {"model": "claude-sonnet-5-5", "effort": "medium", "label": "Sonnet 5.5"}


def test_run_fix_over_api_key_uses_the_chosen_model(data):
    """API キーの接続（Messages API の偽の相手）でも、選んだモデル・文字で直すの指示・人の数値の決まりが効く。"""
    import asyncio

    from tests.test_lasso import FakeClient
    client = FakeClient("propose_override", {"target": "O1", "field": "head_fl_mm", "value_source": "number", "number": 2100,
                                             "reason": "人の指定"})
    res = asyncio.run(al.run_fix(ALIAS, "api_key", "窓 O1 の上端を FL+2100 に", kind="text", model="sonnet", client=client))
    first = client.calls[0]
    assert first["model"] == "claude-sonnet-5-5" and first["output_config"] == {"effort": "medium"}
    assert first["system"].startswith(al.TEXT_ROLE) and "囲まずに、文字だけで" in first["messages"][0]["content"]
    assert [(p["target"], p["to"], p["value_source"]) for p in res["proposals"]] == [("O1", 2100.0, "human:number:2100")]
    client = FakeClient("propose_override", {"target": "O1", "field": "head_fl_mm", "value_source": "number", "number": 2300,
                                             "reason": "勝手な数値"})
    res = asyncio.run(al.run_fix(ALIAS, "api_key", "窓 O1 を高く", kind="text", client=client,
                                 human_words=["窓 O1 を高く", "2. 図面の数値を答える"]))
    assert res["proposals"] == [] and client.calls[0]["model"] == "claude-opus-5-5"   # 人の文にない数値は断る（既定は Opus）


# ---------------------------------------------------------------- 画面の試し用の偽物（HAKOYA_FAKE_AI=1）

def test_fake_ai_only_with_env_and_not_in_exe(client, monkeypatch):
    async def must_not_call(*a, **k):
        raise AssertionError("偽物のときに Claude を呼んだ")
    monkeypatch.setattr(al, "_run_agent", must_not_call)
    assert not al.fake_enabled()
    monkeypatch.setenv("HAKOYA_FAKE_AI", "1")
    monkeypatch.setenv("HAKOYA_FAKE_AI_DELAY", "0")
    assert al.fake_enabled()
    monkeypatch.setattr(sys, "frozen", True, raising=False)                  # 配る exe では効かない
    assert not al.fake_enabled()
    monkeypatch.delattr(sys, "frozen")
    client.post(URL, json={"text": "そろえて", "ids": ["W9", "W5"]})
    done = _wait(client, "R1")
    assert done["message"] == al.FAKE_MESSAGE and done.get("fake") is True and done["cost_usd"] == 0.0
    assert [(p["target"], p["to"]) for p in done["proposals"]] == [("W9", 130.0), ("W5", 145.0)]   # 数値はコード
    assert done["questions"][0]["options"] == ["W9", "W5"]


# ---------------------------------------------------------------- 形の名札・部品の説明・今までの窓口

def test_shape_signature_finds_real_changes(data):
    from core import overrides as ov
    before = sd.signature_of(ALIAS)
    assert set(before) >= {"foundation", "floor1", "exterior1", "roof", "roof_ext"}
    write_outputs(ALIAS, bambu_enabled=False)
    assert sd.changed_parts(before, sd.signature_of(ALIAS)) == []             # 同じ家をもう一度組むと、ぴったり同じ
    raw = json.loads((data / "out" / ALIAS / "building.json").read_text(encoding="utf-8"))
    ov.add(ALIAS, raw, "O1", "offset_mm", 310.0, "human:table", "10mm だけずらす")
    write_outputs(ALIAS, bambu_enabled=False)
    moved = sd.signature_of(ALIAS)
    assert "exterior1" in sd.changed_parts(before, moved)                     # 体積は同じでも、重心が動く
    assert sd.changed(before, None) and sd.changed_parts(None, moved) == ["?"]   # 比べられないときは「変わった」
    assert sd.signature(data / "nothing.glb") is None


def test_part_hints_and_roof_ids_from_build_notes():
    checks = {"parts": {"roof_ext": {"notes": ["ポーチの上の垂れ壁と屋根まで届く袖壁（W12・W13）は屋根と一緒に印刷する"]},
                        "roof": {"notes": ["下がり壁（W15）は屋根と一緒に印刷する。屋根の裏まで立ち上げ"]},
                        "floor1": {"notes": ["（W1）は屋根と一緒に印刷する"]}}}
    assert geo.roof_ids_from_checks(checks) == {"W12", "W13", "W15"}           # 屋根の部品の記録だけ見る
    assert geo.roof_ids_from_checks(None) == set()
    b = _building()
    rows = geo.describe(b, {"walls": ["W1", "W9"], "openings": ["O1"], "roof_edges": ["Y1:S"]}, {"W9"})
    assert [r["part"] for r in rows] == ["家（天井の高さで分けるときは、上は屋根）", "屋根（組み立ての記録から）",
                                         "壁 W1 と同じ（家（天井の高さで分けるときは、上は屋根））", "屋根"]
    sel = geo.select_ids(b, ["W9", "Y1:S", "W9", "W99"], ["W9", "O3"], strict=False)
    assert sel == {"walls": ["W9"], "openings": [], "roof_edges": ["Y1:S"], "roofs": [], "partial": ["W9"]}
    with pytest.raises(ValueError, match="W99"):
        geo.select_ids(b, ["W99"])


def test_old_lasso_endpoint_still_works(client):
    sel = client.post(f"/api/lasso/{A}/select", json={"polygon": box(800, 500, 1200, 1500)}).json()["selection"]
    assert [r["id"] for r in sel] == ["W9"] and sel[0]["part"] == "家（1階の床・内壁）"


def test_lintel_band_on_a_wall_without_a_band_is_refused_before_approval(client, monkeypatch):
    """★11/6（11/5 夜の作者の試しで、効かない窓の上の帯が承認された）：帯ができない壁への案は、依頼の答えに出ない。"""
    _fake(monkeypatch, plan=[("propose_override", {"target": "W1", "field": "lintel_band_mm",
                                                   "value_source": "rule:model.lintel_band_mm", "reason": "窓の上をつなぐ"})])
    client.post(URL, json={"text": "窓の上をつなげて", "connection": "claude_code", "ids": ["W1"]})
    assert _wait(client, "R1")["proposals"] == []
