"""検品の進み具合（core/ai/progress.py）と、指摘の承認・取り消し（core/findings.py）を確かめる（実データ不要）。"""
from __future__ import annotations

import json

import pytest

from core.ai.progress import read_progress


def _write_log(path, recs):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")


def test_progress_from_task_messages(tmp_path):
    log = tmp_path / "inspect_log.jsonl"
    recs = [
        {"type": "AssistantMessage", "content": [{"tool_use": "Agent", "id": "t1", "input": {"subagent_type": "inspect-S"}},
                                                 {"tool_use": "Agent", "id": "t2", "input": {"subagent_type": "inspect-N"}}]},
        {"type": "TaskStartedMessage", "task_id": "a1", "tool_use_id": "t1"},
        {"type": "TaskStartedMessage", "task_id": "a2", "tool_use_id": "t2"},
        {"type": "TaskProgressMessage", "task_id": "a1", "last_tool_name": "mcp__hakoya__render_view"},
        {"type": "TaskNotificationMessage", "task_id": "a2", "status": "completed", "summary": "指摘なし"},
    ]
    _write_log(log, recs)
    p = read_progress(log)
    assert p["faces"]["S"] == {"state": "見ている", "doing": "図を見ている", "summary": ""}
    assert p["faces"]["N"]["state"] == "済み" and p["faces"]["N"]["summary"] == "指摘なし"
    assert p["faces"]["E"]["state"] == "待ち" and p["phase"] == "面ごとに検品中"
    # 終わりが TaskUpdatedMessage（patch.status）で届く場合
    recs.append({"type": "TaskUpdatedMessage", "task_id": "a1", "patch": {"status": "completed"}})
    _write_log(log, recs)
    assert read_progress(log)["phase"] == "指摘をまとめている"
    recs.append({"type": "ResultMessage"})
    _write_log(log, recs)
    assert read_progress(log)["phase"] == "終わり"


def test_progress_without_log(tmp_path):
    assert read_progress(tmp_path / "none.jsonl")["phase"] == "準備中"


@pytest.fixture
def alias(tmp_path, monkeypatch):
    from core import findings, overrides
    monkeypatch.setattr(findings, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(overrides, "out_dir", lambda a: tmp_path / a)
    d = tmp_path / "物件T"
    d.mkdir()
    building = {"levels": [{"name": "1F", "fl_mm": 624}], "walls": [{"id": "W1", "level": "1F", "kind": "interior", "thickness_mm": 145}],
                "openings": [], "glb_exceptions": [{"kind": "back_to_back_exterior_faces", "wall": "W1", "note": "背中合わせ"}],
                "unresolved": [], "skipped": []}
    (d / "building.json").write_text(json.dumps(building, ensure_ascii=False), encoding="utf-8")
    report = {"findings": [{"id": "F1", "message": "厚みがそろっていない", "severity": "低", "status": "未確認",
                            "proposal": {"target": "W1", "field": "thickness_mm", "from": 145, "to": 130,
                                         "value_source": "majority:interior", "reason": "そろえる"}},
                           {"id": "F2", "message": "直す案なし", "severity": "中", "status": "未確認", "proposal": None}],
              "code_findings": []}
    (d / "findings.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return "物件T"


def test_approve_records_override_and_undo_reverts(alias):
    from core import findings, overrides
    f = findings.decide(alias, "F1", "approve")
    assert f["status"] == "承認" and f["override"] == "OV1"
    item = overrides.load(alias)[0]
    assert (item["target"], item["to"], item["origin"]) == ("W1", 130, "finding:F1")
    findings.revert_override(alias, "OV1")
    assert findings.load(alias)["findings"][0]["status"] == "未確認"


def test_reject_later_and_refuse(alias):
    from core import findings
    assert findings.decide(alias, "F2", "later")["status"] == "あとで"
    with pytest.raises(ValueError):
        findings.decide(alias, "F2", "approve")          # 直す案がない
    with pytest.raises(KeyError):
        findings.decide(alias, "F9", "reject")


def test_code_findings_include_glb_exceptions(alias):
    from core import findings
    cf = findings.code_findings(alias)
    assert cf[0]["targets"] == ["W1"] and "背中合わせ" in cf[0]["message"]   # SPEC §12：両面外壁の壁を指摘できる
