"""本体を造形範囲に入るように分ける（core/body_split.py。★2026-11-05 作者 2-1「いい所で。内壁の真ん中は避ける。線は動かせる」）。

架空の細長い家（物件G と同じく約 7.5×14.3m・屋根が長い方向に下る）で確かめる（実データ不要）。
"""
from __future__ import annotations

import numpy as np
import pytest

from core import body_split as bs
from core import print_split as ps
from core.builder import build_model, union_all
from core.export import body_pieces, print_pieces
from core.rules import load_rules
from tests.test_complex_shapes import T, _house

RULES = load_rules("jisha")
SCALE = float(RULES["model"]["scale"])
W, L = 7460.0, 14267.0


def _long_house(window_at: float | None = None) -> dict:
    """横に通る内壁が2枚（y=6000・9000）ある細長い家。window_at：東の外壁の、その y を中心に窓（幅 800）を開ける。"""
    b = _house([(0, 0), (W, 0), (W, L), (0, L)], roof_slope=0.05, eave=3300.0)
    for i, y in enumerate((6000.0, 9000.0), 1):
        b["walls"].append({"id": f"W9{i}", "level": "1F", "kind": "interior", "start": [T, y], "end": [W - T, y],
                           "thickness_mm": 130.0, "bottom_mm": 624, "height": 3024})
    if window_at is not None:
        east = next(w for w in b["walls"] if w["kind"] == "exterior" and abs(w["start"][0] - w["end"][0]) < 1 and w["start"][0] > W / 2)
        s = np.array(east["start"], float)
        d = (np.array(east["end"], float) - s) / np.linalg.norm(np.array(east["end"], float) - s)
        off = float((np.array([east["start"][0], window_at]) - s) @ d) - 400
        b["openings"].append({"id": "O90", "wall": east["id"], "kind": "window", "offset_mm": off, "width_mm": 800,
                              "sill_mm": 1524, "head_mm": 2624, "sill_fl_mm": 900, "head_fl_mm": 2000,
                              "center": [east["start"][0], window_at]})
    return b


@pytest.fixture(scope="module")
def built():
    b = _long_house()
    parts = build_model(b, RULES)
    return b, parts, bs.choose(b, parts, RULES)


def test_cut_is_across_the_long_axis_on_a_cross_wall_face(built):
    b, parts, h = built
    assert h is not None and h.source == "auto" and len(h.cuts) == 1
    assert abs(h.u[1]) > 0.999                                                   # 長い方向（y）に直角に切る
    faces = [y + s * 65.0 for y in (6000.0, 9000.0) for s in (-1, 1)]
    assert min(abs(h.cuts[0] - f) for f in faces) <= 1.0                          # 横に通る内壁の面で切る
    assert "内壁" in h.reasons[0] and set(h.objects) >= {"foundation", "floor1"}
    assert h.sides == ["南", "北"]


def test_pieces_fit_keep_volume_and_have_no_fuzzy_skin_at_the_cut(built):
    b, parts, h = built
    limit = ps.BED_MM - 2 * float(RULES["model"]["print_bed_margin_mm"])
    for key in h.objects:
        pcs = body_pieces(key, parts, RULES, h)
        assert len(pcs) == 2 and all(p["fits"] for p in pcs)
        for p in pcs:
            v = np.vstack([m[0] for m in p["meshes"].values()])
            ext = v.max(axis=0) - v.min(axis=0)
            assert ext[0] <= limit and ext[1] <= limit
            assert abs(v[:, 2].min()) < 1e-6                                       # 床を下に置く（屋根のように裏返さない）
            whole = union_all([s for s in p["solids"].values() if not s.is_empty()])
            assert len([x for x in whole.decompose() if x.volume() > 1000.0]) == 1   # 1つの塊
        members = [q.solid for q in parts.values() if (q.object_key or q.key) == key]
        before = sum(s.volume() for s in members)
        after = sum(s.volume() for p in pcs for s in p["solids"].values() if not s.is_empty())
        assert after == pytest.approx(before, rel=0.002)                           # 体積は変わらない
    band = float(RULES["model"]["roof_cut_smooth_mm"]) * SCALE * 0.9
    strip = ps._prism(bs._cut_line(h.u, h.objects["floor1"][0]).buffer(band, cap_style="flat"), -1e4, 1e5)
    for p in body_pieces("floor1", parts, RULES, h):
        ext = p["solids"].get("exterior1")
        if ext is not None and not ext.is_empty():
            assert (ext ^ strip).volume() < 1.0                                    # 切り口の近くはざらざらにしない


def test_roof_is_cut_on_the_same_line_into_two_not_four_strips(built):
    b, parts, h = built
    pcs = print_pieces("roof", parts, RULES, h)
    assert len(pcs) == 2 and all(p["fits"] for p in pcs)
    assert all(any("本体と同じ分け目" in w for w in p["warnings"]) for p in pcs)


def test_window_at_the_best_face_is_avoided():
    b = _long_house(window_at=6000.0)                                              # y=6000 の内壁の両方の面の所に窓
    parts = build_model(b, RULES)
    h = bs.choose(b, parts, RULES)
    assert all(abs(t - 6000.0) > 400 + 150 - 1 for t in h.cuts)                   # 窓（±400）と余白（150）を避ける
    assert min(abs(h.cuts[0] - (9000.0 + s * 65.0)) for s in (-1, 1)) <= 1.0       # 次の内壁の面で切る
    assert bs.hard_problems(b, h.u, 6000.0 - 65.5, b["walls"], None, 150.0)        # 窓の所は「窓を通る」になる


def test_long_standalone_wall_along_the_house_does_not_block_the_cut():
    """家の長い方向に通る単独の壁（デモの家の西の上の壁など）は、直角に切ってよい。短い単独の壁・柱と、端の近くは切らない。
    ★11/5：単独の壁を通る分け目をすべて断っていたので、長い単独の壁がある家は、どの候補も落ちて「真ん中」になっていた。"""
    b = _long_house()
    b["walls"].append({"id": "W95", "level": "1F", "kind": "standalone", "start": [3000.0, 200.0], "end": [3000.0, L - 200.0],
                       "thickness_mm": 180.0, "bottom_mm": 624, "height": 3024})
    post = {"id": "W96", "level": "1F", "kind": "standalone", "start": [5000.0, 5800.0], "end": [5000.0, 6400.0], "thickness_mm": 180.0}
    u = np.array([0.0, 1.0])
    walls = b["walls"] + [post]
    assert not [p for p in bs.hard_problems(b, u, 7500.0, walls, None, 150.0) if "W95" in p]   # 長い単独の壁は、真ん中なら切ってよい
    assert any("W95" in p for p in bs.hard_problems(b, u, 600.0, walls, None, 150.0))          # 端の近く（短い切れ端が残る）は切らない
    assert any("W96" in p for p in bs.hard_problems(b, u, 6100.0, walls, None, 150.0))         # 短い単独の壁は切らない
    h = bs.choose(b, build_model(b, RULES), RULES)
    assert h is not None and "内壁" in h.reasons[0]                                           # 横に通る内壁の面で切れる（真ん中に逃げない）
    assert not any("見つからなかった" in x for x in h.warnings)


def test_human_cut_is_used_and_a_cut_that_does_not_fit_falls_back(built):
    b, parts, h = built
    mine = bs.choose(b, parts, RULES, {"u": [0.0, 1.0], "cuts": [7000.0], "source": "human"})
    assert mine.source == "human" and mine.cuts == [7000.0]
    bad = bs.choose(b, parts, RULES, {"u": [0.0, 1.0], "cuts": [500.0], "source": "human"})   # 北の部品が入らない
    assert bad.source == "auto" and any("自動の分け目に戻した" in w for w in bad.warnings)


def test_human_cut_through_a_window_is_allowed_with_a_warning():
    b = _long_house(window_at=7000.0)
    parts = build_model(b, RULES)
    h = bs.choose(b, parts, RULES, {"u": [0.0, 1.0], "cuts": [7000.0], "source": "human"})
    assert h.source == "human" and any("窓" in w for w in h.warnings)


def test_snap_to_a_nearby_wall_face():
    b = _long_house()
    assert bs.snap_to_face(b, np.array([0.0, 1.0]), 6100.0, SCALE) == pytest.approx(6065.5)
    assert bs.snap_to_face(b, np.array([0.0, 1.0]), 7500.0, SCALE) == 7500.0      # 近くに面がなければそのまま


def test_house_that_fits_is_not_cut():
    b = _house([(0, 0), (8000, 0), (8000, 6000), (0, 6000)])
    assert bs.choose(b, build_model(b, RULES), RULES) is None


def test_saved_cut_round_trip(tmp_path, monkeypatch):
    from core import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    assert bs.load_saved("物件T") is None
    bs.save_human("物件T", np.array([0.0, 1.0]), [7000.04])
    d = bs.load_saved("物件T")
    assert d["cuts"] == [7000.0] and d["source"] == "human" and d["u"] == [0.0, 1.0]
    text = (tmp_path / "out" / "物件T" / "print_split.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in text                                               # 場所は書かない
    bs.clear_saved("物件T")
    assert bs.load_saved("物件T") is None


def test_split_across_eave_cuts_the_longer_side_and_records_the_real_cut_line():
    """★11/5：半分切りは長い方を半分にし、ざらざらを外す帯の線は本当の切り口に沿う（前は傾いた面で 90° ずれていた）。"""
    import manifold3d as m3
    box = m3.Manifold.cube((16000.0, 4000.0, 300.0))
    pc = ps.Piece(name="屋根", solids={"roof": box}, top_plane={"a": 0.15, "b": 0.0, "c": 3000.0})   # x の向きに下る
    halves = ps.split_across_eave(pc)
    assert len(halves) == 2
    xs = sorted(h.solids["roof"].bounding_box()[3] for h in halves)
    assert xs[0] == pytest.approx(8000.0, abs=1.0)                                 # 長い方（x）を半分
    line = halves[0].cuts[-1]
    assert abs(line.coords[0][0] - 8000.0) < 1.0 and abs(line.coords[1][0] - 8000.0) < 1.0   # 線は x=8000 の縦の線


def test_write_outputs_splits_the_long_house(tmp_path, monkeypatch):
    """書き出し：本体の STL が分け目ごとに出る・造形範囲の「高」が出ない・分け目が checks.json に残る。"""
    import json

    from core import paths
    from core.export import write_outputs
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    b = _long_house()
    b["source"] = {"company_rules": "jisha"}
    d = tmp_path / "out" / "物件T"
    d.mkdir(parents=True)
    (d / "building.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    s = write_outputs("物件T", bambu_enabled=False)
    assert s["house_cut"] and len(s["house_cut"]["cuts"]) == 1 and s["house_cut"]["source"] == "auto"
    assert {"floor1_p1.stl", "floor1_p2.stl", "foundation_p1.stl", "foundation_p2.stl"} <= set(s["stl_files"])
    assert not [c for c in s["checks"] if c["item"] == "build_volume" and c["level"] == "高"]
    assert any(c["item"] == "body_split" and "2 個に分けた" in c["message"] for c in s["checks"])
    names = [p["name"] for p in s["print_pieces"]]
    assert any(n.startswith("1階 1/2（南）") for n in names) and any(n.startswith("基礎 2/2（北）") for n in names)


def test_screen_can_move_and_reset_the_split_line(tmp_path, monkeypatch):
    """平面図で分け目を動かす（POST）・自動に戻す（DELETE）。近くの壁の面に寄せる。入らない位置は断る。"""
    import json

    from fastapi.testclient import TestClient

    from app import server
    from core import paths
    from core.export import write_outputs
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "_aliases", lambda: ["物件T"])
    b = _long_house()
    b["source"] = {"company_rules": "jisha"}
    for o in b["openings"]:
        o.setdefault("style", "sliding")
    d = tmp_path / "out" / "物件T"
    d.mkdir(parents=True)
    (d / "building.json").write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(server, "write_outputs", lambda a, *k, **kw: write_outputs(a, bambu_enabled=False))
    write_outputs("物件T", bambu_enabled=False)
    c = TestClient(server.app)
    assert 'data-role="split"' in c.get("/api/plan/物件T").text
    r = c.post("/api/split/物件T", json={"cuts_mm": [9100.0]})
    assert r.status_code == 200
    hc = r.json()["house_cut"]
    assert hc["source"] == "human" and hc["cuts"] == [9065.5]                   # 内壁の面に寄せた
    assert c.post("/api/split/物件T", json={"cuts_mm": [500.0]}).status_code == 400   # 北の部品が入らない
    assert c.post("/api/split/物件Z", json={"cuts_mm": [9000.0]}).status_code == 404
    assert c.delete("/api/split/物件T").json()["house_cut"]["source"] == "auto"
    assert server.PIECE_STL.match("floor1_p2.stl") and not server.PIECE_STL.match("../floor1_p2.stl")


def test_new_island_is_found_even_when_the_part_already_has_a_detached_body():
    """★11/5 見直し：もとから離れた塊（離れて立つ塀など）がある部品では、切ってできた新しい離れた塊を見逃していた
    （部品の塊の数を「全体の塊の数」と比べていたため）。塊ごとに調べるので、塀があってもなくても同じ所で切る。"""
    def house(detached):
        b = _long_house()
        b["walls"].append({"id": "W95", "level": "1F", "kind": "interior", "start": [3000.0, 6000.0], "end": [3000.0, 7000.0],
                           "thickness_mm": 130.0, "bottom_mm": 624, "height": 3024})     # W91 に T 字でつく短い間仕切り
        if detached:
            b["walls"].append({"id": "W96", "level": "1F", "kind": "standalone", "start": [W + 1500.0, 1000.0],
                               "end": [W + 1500.0, 3000.0], "thickness_mm": 120.0, "bottom_mm": 480, "height": 2000})
        return b
    plain = bs.choose(house(False), build_model(house(False), RULES), RULES)
    b = house(True)
    parts = build_model(b, RULES)
    with_wall = bs.choose(b, parts, RULES)
    assert with_wall.objects["foundation"] == plain.objects["foundation"]                # 塀があっても、離れた塊を作る所では切らない
    for key in with_wall.objects:
        whole = bs._union(bs.body_objects(parts)[key])
        assert bs._island_ok(whole, with_wall.u, with_wall.objects[key])


def test_axis_sign_does_not_flip_with_a_tiny_tilt_and_saved_cut_keeps_its_place():
    """★11/5 見直し：南北に長い家で、外壁のごく小さな傾き（0.1mm）で向きの符号が反転し、保存した分け目が鏡の位置になった。"""
    def shifted(dx_end):
        b = _long_house()
        ox, oy = -W / 2, -L / 2                                                           # Walk in home のように原点をまたぐ

        def mv(q):
            return [q[0] + ox, q[1] + oy]
        for w in b["walls"]:
            w["start"], w["end"] = mv(w["start"]), mv(w["end"])
        for o in b["openings"]:
            o["center"] = mv(o["center"])
        b["foundation"]["outline"] = [mv(q) for q in b["foundation"]["outline"]]
        for r in [b["roof"]] + b["roofs"]:
            r["footprint"] = [mv(q) for q in r["footprint"]]
            for k in ("top_plane", "bottom_plane"):
                r[k]["c"] = r[k]["c"] - r[k]["a"] * ox - r[k]["b"] * oy
        east = next(w for w in b["walls"] if w["kind"] == "exterior" and abs(w["start"][0] - w["end"][0]) < 1 and w["start"][0] > 0)
        east["end"] = [east["end"][0] + dx_end, east["end"][1]]
        return b
    ua, ub = bs.house_axis(shifted(+0.1)), bs.house_axis(shifted(-0.1))
    assert float(ua @ ub) > 0.999                                                        # 傾きの向きで反転しない
    b = shifted(-0.1)
    saved = {"u": [float(-ub[0]), float(-ub[1])], "cuts": [-1500.0], "source": "human"}     # 逆向きで保存した、y=+1500 の分け目
    h = bs.choose(b, build_model(b, RULES), RULES, saved)
    assert h.source == "human" and abs(h.cuts[0] * float(h.u[1]) - 1500.0) < 1.0          # 同じ所（y=+1500）を指す
