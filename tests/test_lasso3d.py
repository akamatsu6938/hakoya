"""3D で囲んで直す（core/pick.py・core/lasso.select_hits・屋根の行・軒の出の案）を確かめる。
Claude は呼ばない（偽の API の相手）。家は架空の平屋（片流れ、西が低い）。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from core import lasso as geo
from core.ai import lasso as al
from core.ai.tools import ToolContext
from core.pick import proxies
from core.rules import load_rules


def _house() -> dict:
    t = 162
    ext = [((0, t / 2), (6000, t / 2)), ((6000 - t / 2, 0), (6000 - t / 2, 4000)),
           ((6000, 4000 - t / 2), (0, 4000 - t / 2)), ((t / 2, 4000), (t / 2, 0))]
    walls = [{"id": f"W{i}", "level": "1F", "kind": "exterior", "start": list(s), "end": list(e), "thickness_mm": t,
              "bottom_mm": 480, "height": "to_roof", "top_glb_mm": 4800} for i, (s, e) in enumerate(ext, 1)]
    walls.append({"id": "W5", "level": "1F", "kind": "interior", "start": [3000, 81], "end": [3000, 3919],
                  "thickness_mm": 130, "bottom_mm": 624, "height": 3024})
    roof = {"shape": "shed", "pitch": [1.5, 10], "low_side": "W", "thickness_mm": 100,
            "top_plane": {"a": 0.15, "b": 0.0, "c": 3900.0}, "bottom_plane": {"a": 0.15, "b": 0.0, "c": 3799.0},
            "top_z_range_mm": [3900, 4800], "footprint": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
            "eave_mm": {"W": 1, "E": 1, "S": 1, "N": 1}, "eave_with_gutter_mm": {"W": 146, "E": 1, "S": 1, "N": 1}}
    return {"source": {"company_rules": "jisha"}, "levels": [{"name": "1F", "fl_mm": 624, "ceiling_mm": 2400}],
            "ground": {"foundation_top_mm": 480}, "walls": walls,
            "openings": [{"id": "O1", "wall": "W1", "kind": "window", "offset_mm": 1000, "width_mm": 1700, "sill_mm": 1524,
                          "head_mm": 3024, "sill_fl_mm": 900, "head_fl_mm": 2400, "center": [1850, 81], "level": "1F"}],
            "roof": roof, "roofs": [{"id": "Y1", "kind": "main", "level": "1F", **roof}]}


def _range(item, axis):
    v = np.asarray(item["verts"], float).reshape(-1, 3)[:, axis]
    return float(v.min()), float(v.max())


# ---------------------------------------------------------------- 当たり判定用の形

def test_proxies_cover_walls_openings_and_eaves():
    data = proxies(_house(), load_rules("jisha"))
    items = {it["id"]: it for it in data["items"]}
    assert data["scale"] == 50
    assert {"W1", "W2", "W3", "W4", "W5", "O1", "Y1", "Y1:W", "Y1:E", "Y1:S", "Y1:N"} <= set(items)
    for it in items.values():                                     # 閉じた三角形の並び
        assert len(it["verts"]) % 3 == 0 and len(it["tris"]) % 3 == 0 and max(it["tris"]) < len(it["verts"]) // 3
    assert items["W5"]["parts"] == ["floor1"] and "exterior1" in items["W1"]["parts"] and items["Y1"]["parts"] == ["roof"]
    assert _range(items["O1"], 1) == pytest.approx((-20, 182), abs=0.2)     # 開口は壁（0〜162）より 20mm ずつ厚い
    assert _range(items["W1"], 2)[1] <= 4700                                  # 屋根までの壁は屋根の下の面で切る
    ys = _range(items["Y1:S"], 1)                                             # 南の軒の帯：板金の分 50 ＋ 少し厚く 20
    assert ys[0] == pytest.approx(-70, abs=0.5) and ys[1] == pytest.approx(-50 + 651 + 20, abs=0.5)


def test_proxies_follow_eave_override():
    b = _house()
    for r in (b["roof"], b["roofs"][0]):
        r["eave_glb_mm"] = dict(r["eave_mm"])
        r["eave_mm"] = {**r["eave_mm"], "S": 451}
    items = {it["id"]: it for it in proxies(b, load_rules("jisha"))["items"]}
    assert _range(items["Y1:S"], 1)[0] == pytest.approx(-70 - 450, abs=0.5)      # 直した軒の先まで帯が伸びる


# ---------------------------------------------------------------- 3D の囲みの中の部材（コードが決める）

def test_select_hits_rules():
    hits = [{"id": "W1", "inside": 400, "total": 500},       # 見えている所の 8割が囲みの中 → 選ぶ
            {"id": "W2", "inside": 100, "total": 1000},      # 1割だけ・囲みの 2%：★11/4 からは選ぶ（一部だけの印）
            {"id": "W3", "inside": 1200, "total": 5000},     # 長い壁の一部：囲みの 3割を占める → 選ぶ（一部だけではない扱い）
            {"id": "O1", "inside": 20, "total": 20},         # 30 画素未満 → 選ばない
            {"id": "Y1", "inside": 500, "total": 600},       # 軒が選ばれたので屋根全体は外す
            {"id": "Y1:S", "inside": 300, "total": 400}]
    assert geo.select_hits(_house(), hits, mask_px=4000) == {"walls": ["W1", "W2", "W3"], "openings": [],
                                                             "roof_edges": ["Y1:S"], "roofs": [], "partial": ["W2"]}
    with pytest.raises(ValueError, match="読み込み直して"):
        geo.select_hits(_house(), [{"id": "W99", "inside": 100, "total": 100}], 4000)


def test_minor_side_eaves_are_dropped():
    """正面から南の軒を囲むと、東・西の軒の端も少し入る（物件A の撮影で S 2795・E 468・W 446 画素）。横の軒は外す。"""
    hits = [{"id": "Y1:S", "inside": 2795, "total": 4262}, {"id": "Y1:E", "inside": 468, "total": 603},
            {"id": "Y1:W", "inside": 446, "total": 576}, {"id": "Y1", "inside": 5565, "total": 6579}]
    assert geo.select_hits(_house(), hits, 10068)["roof_edges"] == ["Y1:S"]
    corner = [{"id": "Y1:S", "inside": 1500, "total": 2500}, {"id": "Y1:E", "inside": 1200, "total": 1800}]   # 斜めから角を囲む
    assert geo.select_hits(_house(), corner, 6000)["roof_edges"] == ["Y1:E", "Y1:S"]


def test_describe_roof_rows():
    rows = geo.describe(_house(), {"roof_edges": ["Y1:S"], "roofs": ["Y1"]})
    assert rows[0]["label"] == "軒（南）" and "軒の出 1mm" in rows[0]["text"] and "target=Y1・field=eave_mm.S" in rows[0]["text"]
    assert rows[1]["label"] == "屋根全体" and "勾配 1.5/10" in rows[1]["text"] and "低い側 西" in rows[1]["text"]
    west = geo.describe(_house(), {"roof_edges": ["Y1:W"]})[0]["text"]
    assert "樋を含めると 146mm" in west


# ---------------------------------------------------------------- Claude に頼む（偽の相手）

def _usage(i, o):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


class FakeClient:
    """1回目に道具を1つ使い、2回目に短い説明を返す。"""

    def __init__(self, tool_name, tool_input):
        self.calls: list[dict] = []
        self.messages = self
        self.tool = (tool_name, tool_input)

    async def create(self, **kw):
        self.calls.append(kw)
        if len(kw["messages"]) == 1:
            return NS(content=[NS(type="tool_use", id="tu1", name=self.tool[0], input=self.tool[1])],
                      stop_reason="tool_use", usage=_usage(4000, 200))
        return NS(content=[NS(type="text", text="南の軒の出を直す案を出しました。")], stop_reason="end_turn", usage=_usage(1000, 100))


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "out_dir", lambda a: tmp_path)
    return tmp_path


def _ask3d(client, text):
    b = _house()
    ctx = ToolContext(alias="物件T", raw=b, building=json.loads(json.dumps(b)), rules=load_rules("jisha"))
    return asyncio.run(al.run_lasso("物件T", "api_key", None, text, client=client, ctx=ctx,
                                    hits=[{"id": "Y1:S", "inside": 300, "total": 400}], mask_px=2000, view="S"))


EAVE = {"target": "Y1", "field": "eave_mm.S", "value_source": "number", "number": 450, "reason": "図面の軒の出"}


def test_3d_eave_proposal_uses_the_human_number(out):
    client = FakeClient("propose_override", EAVE)
    res = _ask3d(client, "図面だと 450mm 出ている")
    assert [r["id"] for r in res["selection"]] == ["Y1:S"]
    p = res["proposals"][0]
    assert (p["target"], p["field"], p["from"], p["to"], p["value_source"]) == ("Y1", "eave_mm.S", 1, 450.0, "human:number:450")
    prompt = client.calls[0]["messages"][0]["content"]
    assert "南から見た 3D の画面" in prompt and "jww:section:D番号" in prompt and "通り芯から" in prompt   # 断面図の寸法の使い方
    log = [json.loads(x) for x in (out / "lasso_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[0]["where"] == "3d:S" and log[0]["selection"] == ["Y1:S"]


def test_3d_eave_number_not_in_words_is_refused(out):
    client = FakeClient("propose_override", EAVE)                  # Claude が勝手に 450 と書いた
    res = _ask3d(client, "図面だともう少し出ている")
    assert res["proposals"] == []
    second = client.calls[1]["messages"][-1]["content"][0]          # 道具の答え（断った理由）が Claude に返る
    assert second["is_error"] and "人の一言にない" in second["content"][0]["text"]


@pytest.fixture
def alias(tmp_path, monkeypatch):
    from core import overrides
    from core.ai import tools
    monkeypatch.setattr(tools, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(overrides, "out_dir", lambda a: tmp_path / a)
    (tmp_path / "物件T").mkdir()
    (tmp_path / "物件T" / "building.json").write_text(json.dumps(_house(), ensure_ascii=False), encoding="utf-8")
    return "物件T"


def test_approve_eave_proposal(alias):
    item = al.approve(alias, {"target": "Y1", "field": "eave_mm.S", "value_source": "human:number:450", "reason": "図面の値"})
    assert (item["target"], item["field"], item["from"], item["to"], item["origin"]) == ("Y1", "eave_mm.S", 1, 450.0, "lasso")
    ctx = ToolContext.load(alias)
    assert ctx.building["roof"]["eave_mm"]["S"] == 450 and ctx.building["roofs"][0]["eave_glb_mm"]["S"] == 1
