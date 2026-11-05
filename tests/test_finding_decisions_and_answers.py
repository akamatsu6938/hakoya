"""検品の「承認・却下・あとで・頼んだ・戻す」（core/findings.decide）と、Claude の質問への人の答え（core/answers.py）。★11/5 夜
- 二度承認しても修正を2件にしない（画面の窓口は組み直しもしない）。直す案のない指摘は「依頼済み」にできる。戻すと「未確認」。
- 答えは物件ごとの answers.json に残る（書く前に出してはいけない言葉を確かめる）。画面には答え・前の答えを付けて返す。
- 次の検品で、Claude への指示（自分のプラン・API キー・MCP）に「前の検品で人が答えたこと」が入る（Claude は偽物）。
実データは使わない（架空の物件T。言葉も架空）。"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import pytest

from core import answers as an
from core import findings as fd
from core import overrides as ov
from core import paths
from core.privacy import PrivacyError

ALIAS = "物件T"
AT = "2026-11-05T21:00:00+09:00"                 # この検品の時刻（findings.json の at）
BEFORE = "2026-11-04T10:00:00+09:00"             # 前の検品の時刻
QUESTION = "南の窓の上端は、図面と glb のどちらに合わせますか？"
NG = "架空山田"                                   # 出してはいけない言葉の試し（架空）


@pytest.fixture
def alias(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    d = tmp_path / "out" / ALIAS
    d.mkdir(parents=True)
    building = {"levels": [{"name": "1F", "fl_mm": 624}], "openings": [], "unresolved": [], "skipped": [], "glb_exceptions": [],
                "walls": [{"id": "W1", "level": "1F", "kind": "interior", "thickness_mm": 145}]}
    (d / "building.json").write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    report = {"at": AT, "connection": "claude_code", "summary": "指摘3件",
              "findings": [{"id": "F1", "title": "W1 の厚みがそろっていない", "message": "W1 だけ厚い", "severity": "低", "status": "未確認",
                            "targets": ["W1"], "proposal": {"target": "W1", "field": "thickness_mm", "from": 145, "to": 130,
                                                            "value_source": "majority:interior", "reason": "そろえる"}},
                           {"id": "F2", "title": "南の窓の上端が違う", "message": "直す案なし", "severity": "中", "status": "未確認",
                            "targets": ["W1"], "proposal": None,
                            "question": {"question": "この窓は開けたままにしますか？", "options": ["開けたまま", "ふさぐ"]}}],
              "code_findings": [{"id": "C1", "title": "未解決の所 U1", "message": "x", "severity": "中", "status": "未確認", "targets": []}],
              "questions": [{"id": "Q1", "question": QUESTION, "options": ["図面", "glb"], "evidence": "立面図の寸法"}]}
    (d / "findings.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return ALIAS


def _status(alias: str, fid: str) -> str:
    rep = fd.load(alias)
    return next(f for f in rep["findings"] + rep["code_findings"] if f["id"] == fid)["status"]


# ---------------------------------------------------------------- 承認・却下・あとで・頼んだ・戻す（core/findings.decide）

def test_second_approve_does_not_add_a_second_fix(alias):
    first = fd.decide(alias, "F1", "approve")
    again = fd.decide(alias, "F1", "approve")
    assert first["status"] == again["status"] == "承認" and first["override"] == again["override"] == "OV1"
    assert len(ov.load(alias)) == 1                              # 修正は1件のまま
    # 修正を取り消すと「未確認」に戻り、もう一度承認できる
    ov.undo(alias, "OV1")
    fd.revert_override(alias, "OV1")
    assert _status(alias, "F1") == "未確認"
    assert fd.decide(alias, "F1", "approve")["status"] == "承認" and len(ov.load(alias)) == 1


def test_request_reset_and_what_cannot_be_undone_here(alias):
    f = fd.decide(alias, "F2", "request")                         # 直す案のない指摘：Claude に直す案を頼んだ
    assert f["status"] == "依頼済み" and _status(alias, "F2") == "依頼済み" and f["decided_at"]
    assert ov.load(alias) == []                                   # 模型は変わらない
    for action in ("reset", "reject", "reset", "later", "reset"):
        assert fd.decide(alias, "F2", action)["status"] == fd.STATUSES[action]
    assert _status(alias, "F2") == "未確認" and "decided_at" not in fd.load(alias)["findings"][1]
    assert fd.decide(alias, "C1", "request")["status"] == "依頼済み"   # コードの指摘も同じ
    fd.decide(alias, "F1", "approve")
    for action in ("reset", "reject", "later", "request"):       # 承認した指摘は、直しの取り消しからでないと戻さない
        with pytest.raises(ValueError, match="取り消"):
            fd.decide(alias, "F1", action)
    assert _status(alias, "F1") == "承認" and len(ov.load(alias)) == 1
    with pytest.raises(ValueError):
        fd.decide(alias, "F2", "approve")                        # 直す案がない（今までどおり）
    with pytest.raises(ValueError):
        fd.decide(alias, "F2", "done")


@pytest.fixture
def client(alias, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    monkeypatch.setattr(server, "_aliases", lambda: [ALIAS])
    monkeypatch.setattr(server, "property_files", lambda a: paths.PropertyFiles(alias=a, glb=None, jww_folder=None))
    built = []
    monkeypatch.setattr(server, "write_outputs", lambda a, *x, **k: built.append(a) or {"checks": []})   # 組み直しの代わり
    c = TestClient(server.app)
    c.built = built
    return c


def _url(rest: str) -> str:
    return f"/api/findings/{quote(ALIAS)}{rest}"


def test_screen_approve_twice_rebuilds_once(client):
    r = client.post(_url("/F1"), json={"action": "approve"})
    assert r.status_code == 200 and r.json()["already"] is False and r.json()["finding"]["override"] == "OV1"
    r = client.post(_url("/F1"), json={"action": "approve"})
    assert r.status_code == 200 and r.json()["already"] is True and r.json()["finding"]["override"] == "OV1"
    assert client.built == [ALIAS] and len(ov.load(ALIAS)) == 1   # 2回目は記録も組み直しもしない
    assert client.post(_url("/F1"), json={"action": "reset"}).status_code == 400
    r = client.post(_url("/F2"), json={"action": "request"})
    assert r.status_code == 200 and r.json()["finding"]["status"] == "依頼済み" and r.json()["already"] is False
    assert client.post(_url("/F2"), json={"action": "reset"}).json()["finding"]["status"] == "未確認"
    assert client.built == [ALIAS]                                 # 頼んだ・戻すでは組み直さない


def test_requested_findings_are_not_counted_as_open(client):
    client.post(_url("/F2"), json={"action": "request"})
    row = next(r for r in client.get("/api/properties").json() if r["alias"] == ALIAS)
    assert row["status"]["open_findings"] == 2                    # F1・C1（依頼済みの F2 は数えない）


# ---------------------------------------------------------------- 人の答え（core/answers.py）

def test_answer_is_saved_per_property_and_replaced(alias):
    rec = an.add(alias, "Q1", QUESTION, ["図面", "glb"], " 図面 ", AT)
    saved = json.loads((paths.DATA_DIR / "out" / ALIAS / "answers.json").read_text(encoding="utf-8"))
    assert saved == [rec] and rec["answer"] == "図面" and rec["inspected_at"] == AT
    assert set(rec) == {"question_id", "question", "options", "answer", "at", "inspected_at"}
    an.add(alias, "Q1", QUESTION, ["図面", "glb"], "glb", AT)                 # 同じ検品の同じ質問に答え直す → 入れ替え
    an.add(alias, "Q1", QUESTION, ["図面", "glb"], "図面", BEFORE)            # 前の検品の答えは別に残る
    assert [(a["answer"], a["inspected_at"]) for a in an.load(alias)] == [("glb", AT), ("図面", BEFORE)]
    with pytest.raises(ValueError):
        an.add(alias, "Q1", QUESTION, [], "   ", AT)
    with pytest.raises(ValueError):
        an.add(alias, "Q1", QUESTION, [], "あ" * (an.ANSWER_MAX + 1), AT)


def test_answer_with_a_forbidden_word_is_not_written(alias, monkeypatch):
    import core.privacy
    monkeypatch.setattr(core.privacy, "ng_words", lambda path=None: [NG])
    with pytest.raises(PrivacyError) as e:
        an.add(alias, "Q1", QUESTION, ["図面", "glb"], f"{NG}様の図面に合わせる", AT)
    assert NG not in str(e.value) and not an.answers_path(alias).exists()


def test_screen_gets_answers_and_previous_answers(client, monkeypatch):
    an.add(ALIAS, "Q4", QUESTION, ["図面", "glb"], "図面", BEFORE)          # 前の検品で、同じ文の質問に答えた
    rep = client.get(_url("")).json()
    q1 = rep["questions"][0]
    assert "answer" not in q1 and q1["previous_answer"]["answer"] == "図面"
    r = client.post(_url("/questions/Q1/answer"), json={"answer": "glb"})
    assert r.status_code == 200 and r.json()["answer"]["inspected_at"] == AT
    rep = client.get(_url("")).json()
    q1 = rep["questions"][0]
    assert q1["answer"] == "glb" and q1["answered_at"] and q1["previous_answer"]["answer"] == "図面"
    r = client.post(_url("/questions/F2/answer"), json={"answer": "開けたまま"})   # 指摘に付いた質問（番号は指摘の番号）
    assert r.status_code == 200
    assert client.get(_url("")).json()["findings"][1]["question"]["answer"] == "開けたまま"
    assert "answer" not in json.loads(fd.findings_path(ALIAS).read_text(encoding="utf-8"))["questions"][0]   # findings.json は書き換えない
    assert client.post(_url("/questions/Q9/answer"), json={"answer": "x"}).status_code == 404
    assert client.post(_url("/questions/Q1/answer"), json={"answer": " "}).status_code == 400
    # 画面が古いまま（検品し直して Q1 の中身が変わった）なら断る。空白・全角半角の違いは同じ質問
    assert client.post(_url("/questions/Q1/answer"), json={"answer": "glb", "question": "別の質問ですか？"}).status_code == 409
    assert client.post(_url("/questions/Q1/answer"), json={"answer": "glb", "question": " " + QUESTION.replace("glb", "ｇｌｂ")}).status_code == 200
    import core.privacy
    monkeypatch.setattr(core.privacy, "ng_words", lambda path=None: [NG])
    r = client.post(_url("/questions/Q1/answer"), json={"answer": f"{NG}さんに聞いた"})
    assert r.status_code == 400 and NG not in r.text
    assert all(NG not in a["answer"] for a in an.load(ALIAS))


def test_answer_with_a_word_added_later_is_hidden_but_the_list_still_opens(client, monkeypatch):
    an.add(ALIAS, "Q1", QUESTION, ["図面", "glb"], f"{NG}さんの図面に合わせる", AT)   # 言葉を足す前に書いた答え
    import core.privacy
    monkeypatch.setattr(core.privacy, "ng_words", lambda path=None: [NG])
    r = client.get(_url(""))
    assert r.status_code == 200 and NG not in r.text and "answer" not in r.json()["questions"][0]


def test_prompt_section_is_short_and_skips_forbidden_words(alias, monkeypatch):
    assert an.prompt_section(alias) == ""                                    # 答えがなければ足さない
    an.add(alias, "Q1", QUESTION, ["図面", "glb"], "glb", BEFORE)
    an.add(alias, "Q1", QUESTION, ["図面", "glb"], "図面", AT)                # 同じ質問は新しい答えだけ
    an.add(alias, "Q2", "玄関の段は作りますか？", [], f"{NG}さんの家と同じ", AT)
    import core.privacy
    monkeypatch.setattr(core.privacy, "ng_words", lambda path=None: [NG])
    text = an.prompt_section(alias)
    assert "前の検品で人が答えたこと" in text and "もう一度しないでください" in text
    assert f"質問：{QUESTION}（選択肢：1. 図面 ／ 2. glb） → 人の答え：図面" in text and "人の答え：glb" not in text
    assert NG not in text and "玄関の段" not in text                        # あとから足した言葉の入った答えは渡さない


# ---------------------------------------------------------------- 次の検品の指示に答えが入る（Claude は偽物）

def _answers_file(path) -> None:
    path.write_text(json.dumps([{"question_id": "Q1", "question": QUESTION, "options": ["図面", "glb"], "answer": "図面",
                                 "at": BEFORE, "inspected_at": BEFORE}], ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def fake_api(tmp_path, monkeypatch):
    """API キーの検品（core/ai/api_runner.py）を偽の相手で動かす用意（tests/test_api_runner.py と同じ差し替え）。"""
    from PIL import Image

    from core.ai import api_runner as ar
    png = tmp_path / "S_model.png"
    Image.new("RGB", (8, 8), "white").save(png)
    real = ar.call_tool
    monkeypatch.setattr(ar, "call_tool", lambda ctx, name, args: {"view": args["view"], "image_path": str(png)}
                        if name == "render_view" else real(ctx, name, args))
    monkeypatch.setattr(ar, "code_findings", lambda a: [])
    return tmp_path


def test_api_key_inspection_tells_claude_the_previous_answers(fake_api):
    from core.ai import api_runner as ar
    from tests.test_api_runner import FakeClient, _ctx

    def run():
        client = FakeClient()
        rep = asyncio.run(ar.run_inspection_api(ALIAS, client=client, ctx=_ctx(), out=fake_api))
        # 面ごとの最初の頼み（やりとりの記録はあとから足されるので、中身で数える）
        faces = sorted({c["messages"][0]["content"] for c in client.calls if "format" not in c["output_config"]})
        merge = next(c["messages"][0]["content"] for c in client.calls if "format" in c["output_config"])
        return rep, faces, merge
    rep, faces, merge = run()
    assert len(faces) == 6 and rep["at"]                                     # 検品の時刻を残す（答えを見分けるため）
    assert not any("前の検品で人が答えたこと" in t for t in faces + [merge])    # 答えがなければ足さない
    _answers_file(fake_api / "answers.json")
    rep, faces, merge = run()
    assert all("前の検品で人が答えたこと" in t and "人の答え：図面" in t for t in faces)   # 面ごとの指示
    assert "人の答え：図面" in merge and QUESTION in merge                   # まとめ役
    assert rep["findings"][0]["id"] == "F1"                                 # 検品そのものは今までどおり


def test_plan_inspection_tells_claude_the_previous_answers(tmp_path, monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    from core.ai import agent
    from tests.test_api_runner import _ctx
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "out" / ALIAS).mkdir(parents=True)
    opt = agent.build_options(_ctx())
    assert "前の検品で人が答えたこと" not in opt.system_prompt
    _answers_file(tmp_path / "out" / ALIAS / "answers.json")
    opt = agent.build_options(_ctx())
    assert "人の答え：図面" in opt.system_prompt                             # まとめ役
    assert len(opt.agents) == 6 and all("人の答え：図面" in a.prompt for a in opt.agents.values())   # 面ごとの担当


def test_mcp_inspect_prompt_tells_claude_the_previous_answers(tmp_path, monkeypatch):
    from core.ai import mcp_tools as mt
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mt, "_aliases", lambda: [ALIAS])
    (tmp_path / "out" / ALIAS).mkdir(parents=True)
    assert "前の検品で人が答えたこと" not in mt.inspect_prompt(ALIAS)
    _answers_file(tmp_path / "out" / ALIAS / "answers.json")
    text = mt.inspect_prompt(ALIAS)
    assert "人の答え：図面" in text and text.index("前の検品で人が答えたこと") < text.index("## 手順")
    assert "前の検品で人が答えたこと" not in mt.inspect_prompt("物件Z")      # 登録していない物件には足さない
