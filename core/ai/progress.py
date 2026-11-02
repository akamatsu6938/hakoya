"""検品の進み具合（SPEC §10：面ごとのサブエージェントの状態を並べて見せる）。

検品のやりとりの記録（data/out/<物件>/inspect_log.jsonl）を読み、6人の検品担当それぞれの状態を出す。
- まとめ役が Agent の道具で検品担当を呼ぶ（input の subagent_type が "inspect-S" など）。
- 検品担当は裏で動き、開始・進み具合・終了は Task… の知らせで届く（tool_use_id で呼び出しと結びつく）。
  終了は TaskNotificationMessage か、TaskUpdatedMessage の patch.status が終わりの値のどちらかで届く（SDK の説明）。
"""
from __future__ import annotations

import json
from pathlib import Path

FACES = ("S", "N", "E", "W", "plan", "print")
FACE_LABELS = {"S": "南", "N": "北", "E": "東", "W": "西", "plan": "平面", "print": "印刷"}
TERMINAL = {"completed", "failed", "killed", "stopped", "error", "cancelled"}
TOOL_LABELS = {"render_view": "図を見ている", "get_elements": "部材を確かめている", "get_building": "家の要約を読んでいる",
               "run_checks": "検査の結果を見ている", "propose_override": "直す案を考えている", "ask_question": "質問を考えている",
               "get_drawing_region": "図面を見ている", "Skill": "ルールを読んでいる", "StructuredOutput": "まとめを書いている"}


def _tool_label(name: str | None) -> str:
    if not name:
        return "見ている"
    short = name.split("__")[-1]
    return TOOL_LABELS.get(short, "見ている")


def read_progress(log_path: Path) -> dict:
    """記録から進み具合を出す。faces: {面: {state: 待ち／見ている／済み／失敗, doing, summary}}、phase: 全体の段階。"""
    faces = {f: {"state": "待ち", "doing": "", "summary": ""} for f in FACES}
    out = {"faces": faces, "phase": "準備中", "events": 0, "finished": False}
    if not log_path.exists():
        return out
    call_face: dict[str, str] = {}     # Agent の呼び出しの id → 面
    task_face: dict[str, str] = {}     # 検品担当の task_id → 面
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue                    # 書きかけの行
        out["events"] += 1
        t = r.get("type", "")
        for c in r.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("tool_use") == "Agent":
                sub = (c.get("input") or {}).get("subagent_type", "")
                face = sub.replace("inspect-", "")
                if face in faces:
                    call_face[c["id"]] = face
                    faces[face]["state"] = "見ている"
                    out["phase"] = "面ごとに検品中"
            elif "tool_use" in c and r.get("parent_tool_use_id") in call_face:
                face = call_face[r["parent_tool_use_id"]]       # 検品担当の中で道具を使った
                if faces[face]["state"] != "済み":
                    faces[face]["doing"] = _tool_label(c["tool_use"])
        if t == "TaskStartedMessage" and r.get("tool_use_id") in call_face:
            task_face[r["task_id"]] = call_face[r["tool_use_id"]]
        face = task_face.get(r.get("task_id", "")) or call_face.get(r.get("tool_use_id", ""))
        if face:
            if t == "TaskProgressMessage" and faces[face]["state"] != "済み":
                faces[face]["doing"] = _tool_label(r.get("last_tool_name"))
            status = r.get("status") or (r.get("patch") or {}).get("status")
            if t in ("TaskNotificationMessage", "TaskUpdatedMessage") and status in TERMINAL:
                faces[face]["state"] = "済み" if status == "completed" else "失敗"
                faces[face]["doing"] = ""
                if r.get("summary"):
                    faces[face]["summary"] = r["summary"]
        if t == "ResultMessage":
            out["finished"] = True
    called = [f for f in FACES if faces[f]["state"] != "待ち"]
    if called and all(faces[f]["state"] in ("済み", "失敗") for f in called) and not out["finished"]:
        out["phase"] = "指摘をまとめている"
    if out["finished"]:
        out["phase"] = "終わり"
        for f in called:                # 終わりの知らせがなくても、検品が最後まで終わっていれば済み
            if faces[f]["state"] == "見ている":
                faces[f]["state"], faces[f]["doing"] = "済み", ""
    return out
