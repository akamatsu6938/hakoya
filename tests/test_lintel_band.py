"""窓の上の帯（壁の lintel_band_mm。決定 2-4「気になる所は直す案にして、人が決める」）を、架空の平屋で確かめる（実データ不要）。

天井の高さ（継ぎ目）に上端が届く窓が並ぶと、窓の間の細い柱は1階の部品の中で下だけでつながる（折れやすい）。
- コードの検査が直す案（会社ルール model.lintel_band_mm）つきで指摘する
- 帯（100mm）を付けると、1階の部品の中で柱の上がつながり、屋根の部品とは重ならず、屋根は帯の分だけ減る
- 0 なら今までと同じ。承認で修正になり、取り消すと元に戻る
"""
from __future__ import annotations

import json

import pytest
from shapely.geometry import box

from core.builder import build_model, drop_specks, prism, union_all
from core.checks import part_checks
from core.export import overlap_checks, to_trimesh
from core.lintel import fragile_post_findings
from core.rules import load_rules
from tests.test_builder import _tiny_house
from tests.test_complex_shapes import SHAPES, _house

Z_CUT = 624 + 2400           # 天井の高さ（_tiny_house の FL＋天井）
WINDOWS = (900, 1520, 2140)  # 南の外壁 W1 の窓（幅 500）。間の柱は 120mm（模型 2.4mm）


def _band_house(band=None, offsets=WINDOWS, width=500):
    """_tiny_house の南の窓を、上端が天井の高さの窓3つに替えた家（W1 は x=0〜6000、y=80 の外壁）。"""
    b = _tiny_house()
    b["openings"] = [o for o in b["openings"] if o["id"] != "O1"]
    for i, off in enumerate(offsets, 3):
        b["openings"].append({"id": f"O{i}", "wall": "W1", "kind": "window", "offset_mm": off, "width_mm": width,
                              "sill_mm": 1524, "head_mm": Z_CUT, "sill_fl_mm": 900, "head_fl_mm": 2400,
                              "center": [off + width / 2, 80]})
    for o in b["openings"]:
        o.setdefault("sill_fl_mm", o["sill_mm"] - 624)
        o.setdefault("head_fl_mm", o["head_mm"] - 624)
    if band is not None:
        b["walls"][0]["lintel_band_mm"] = band
    return b


@pytest.fixture(scope="module")
def rules():
    return load_rules("jisha")


@pytest.fixture(scope="module")
def built(rules):
    return {"base": build_model(_band_house(), rules), "band": build_model(_band_house(100), rules),
            "zero": build_model(_band_house(0), rules)}


def _objects(parts) -> dict:
    objs: dict[str, list] = {}
    for p in parts.values():
        objs.setdefault(p.object_key or p.key, []).append(p.solid)
    return {k: union_all(v) for k, v in objs.items()}


def _vol(parts, key) -> float:
    return sum(p.solid.volume() for p in parts.values() if (p.object_key or p.key) == key)


def test_finding_proposes_band_for_posts_between_windows(rules):
    out = fragile_post_findings(_band_house(), rules)
    assert len(out) == 1
    f = out[0]
    assert f["severity"] == "中" and f["view"] == "S" and f["targets"] == ["W1", "O3", "O4", "O5"]
    assert "外壁 W1 の窓 O3・O4・O5 の間の柱（2 本、幅 模型 2.4mm" in f["message"]
    assert "1階の部品の中で上がつながっていません" in f["message"] and "帯（模型 2mm）" in f["message"]
    assert f["proposal"] == {"target": "W1", "field": "lintel_band_mm", "from": 0.0, "to": 100.0,
                             "value_source": "rule:model.lintel_band_mm", "reason": f["proposal"]["reason"]}


def test_no_finding_when_band_exists_or_not_needed(rules):
    assert fragile_post_findings(_band_house(100), rules) == []               # すでに帯がある
    assert fragile_post_findings(_band_house(offsets=(900,)), rules) == []    # 窓が1つ（間の柱がない）
    low = _band_house()
    for o in low["openings"]:
        o["head_mm"] = Z_CUT - 300                                            # 上端が継ぎ目に届かない（上に壁が残る）
    assert fragile_post_findings(low, rules) == []
    two = _band_house()
    two["levels"].append({"name": "2F", "fl_mm": 3500, "ceiling_mm": 2400})
    assert fragile_post_findings(two, rules) == []                            # 2階建てには出さない（平屋だけ）


def test_band_connects_posts_inside_first_floor(built):
    """帯がないと、窓の間の2本の柱は1階の部品の中で離れた塊。帯があると、上でつながって1つの塊になる。"""
    probe = prism(box(1300, -50, 2240, 210), 1600, Z_CUT + 300)               # 柱2本と窓の上（W1 の厚みの範囲）
    base = drop_specks(_objects(built["base"])["floor1"] ^ probe)
    band = drop_specks(_objects(built["band"])["floor1"] ^ probe)
    assert len(base.decompose()) == 2
    assert len(band.decompose()) == 1
    assert band.bounding_box()[5] == pytest.approx(Z_CUT + 100)               # 窓の上端＋帯
    # 屋根の外壁は、帯の所だけ継ぎ目より上から始まる
    above = prism(box(1300, -50, 2240, 210), Z_CUT, Z_CUT + 100)
    assert (_objects(built["band"])["roof"] ^ above).volume() < 1.0
    assert (_objects(built["base"])["roof"] ^ above).volume() > 1.0


def test_band_moves_exactly_its_volume_without_overlap(built, rules):
    scale = float(rules["model"]["scale"])
    expected = (WINDOWS[-1] + 500 + 100 - (WINDOWS[0] - 100)) * 160 * 100      # 並び＋前後 100mm × 壁の厚み × 帯
    base, band = built["base"], built["band"]
    assert _vol(base, "roof") - _vol(band, "roof") == pytest.approx(expected, rel=1e-6)
    assert _vol(band, "floor1") - _vol(base, "floor1") == pytest.approx(expected, rel=1e-6)
    assert not [c for c in overlap_checks(band, scale) if c["level"] == "高"]
    for k, p in band.items():
        assert str(p.solid.status()).endswith("NoError") and p.solid.volume() > 0, k
        checks = part_checks(k, p.label, to_trimesh(p, scale, for_print=True), [x.volume() for x in p.solid.decompose()])
        assert not [c for c in checks if c["level"] == "高"], (k, checks)
    for k, s in _objects(band).items():
        assert len(drop_specks(s).decompose()) == 1, k
    assert any("帯を残した" in n for n in band["floor1"].notes)
    assert any("窓の上の帯" in n for n in band["roof"].notes)


def test_band_zero_is_same_as_today(built):
    base, zero = built["base"], built["zero"]
    assert list(base) == list(zero)
    for k in base:
        assert zero[k].solid.volume() == base[k].solid.volume(), k
        assert zero[k].notes == base[k].notes, k


def test_band_keeps_away_from_roof_corner_triangle(rules):
    """東の角の近くの窓の並び：帯はずれ止めの三角（辺 400）から、隙間＋最小の太さ（10＋50）だけ離して止める。"""
    scale = float(rules["model"]["scale"])
    offsets = (3500, 4220, 4940)                                              # 幅 600、最後の窓は 5540 まで
    base = build_model(_band_house(offsets=offsets, width=600), rules)
    band = build_model(_band_house(100, offsets=offsets, width=600), rules)
    inner = 6000 - 160                                                        # 東の外壁の内の面（三角の角）
    stop = inner - 400 - 10 - 50
    expected = (stop - (offsets[0] - 100)) * 160 * 100
    assert _vol(base, "roof") - _vol(band, "roof") == pytest.approx(expected, rel=1e-6)
    first = _objects(band)["floor1"]
    assert (first ^ prism(box(stop + 0.5, -50, 6100, 210), Z_CUT + 1, Z_CUT + 300)).volume() < 1.0   # 三角の側の端より先に帯はない
    assert (first ^ prism(box(stop - 10, -50, stop, 210), Z_CUT + 1, Z_CUT + 100)).volume() > 1.0    # 端までは帯がある
    assert not [c for c in overlap_checks(band, scale) if c["level"] == "高"]
    assert "ずれ止めの三角を 4 か所" in " ".join(band["roof"].notes)


@pytest.mark.parametrize("name", ["u_shape", "zigzag"])
def test_band_on_complex_outline(rules, name):
    """凹凸のある家：いちばん長い外壁の真ん中に、上端が天井の高さの窓3つ。帯を付けても閉じた立体で、重ならない。"""
    scale = float(rules["model"]["scale"])
    b = _house(SHAPES[name])
    import numpy as np
    w = max((x for x in b["walls"] if x["kind"] == "exterior"),
            key=lambda x: float(np.linalg.norm(np.array(x["end"]) - np.array(x["start"]))))
    s, e = np.array(w["start"]), np.array(w["end"])
    L = float(np.linalg.norm(e - s))
    d = (e - s) / L
    b["openings"] = [o for o in b["openings"] if o["wall"] != w["id"]]
    for i, off in enumerate((L / 2 - 760, L / 2 - 250, L / 2 + 260)):
        b["openings"].append({"id": f"O{90 + i}", "wall": w["id"], "kind": "window", "offset_mm": off, "width_mm": 500,
                              "sill_mm": 1524, "head_mm": Z_CUT, "center": list(s + d * (off + 250))})
    assert fragile_post_findings(b, rules)[0]["proposal"]["target"] == w["id"]
    base = build_model(b, rules)
    w["lintel_band_mm"] = 100
    band = build_model(b, rules)
    assert fragile_post_findings(b, rules) == []
    moved = _vol(base, "roof") - _vol(band, "roof")
    assert moved == pytest.approx(_vol(band, "floor1") - _vol(base, "floor1"), rel=1e-6)
    assert moved == pytest.approx((1520 + 200) * 160 * 100, rel=1e-6)
    assert not [c for c in overlap_checks(band, scale) if c["level"] == "高"], name
    for k, p in band.items():
        checks = part_checks(k, p.label, to_trimesh(p, scale, for_print=True), [x.volume() for x in p.solid.decompose()])
        assert not [c for c in checks if c["level"] == "高"], (name, k, checks)
    for k, sol in _objects(band).items():
        assert len(drop_specks(sol).decompose()) == 1, (name, k)


@pytest.fixture
def alias(tmp_path, monkeypatch):
    from core import findings, overrides, paths
    monkeypatch.setattr(findings, "out_dir", lambda a: tmp_path / a)
    monkeypatch.setattr(overrides, "out_dir", lambda a: tmp_path / a)

    def no_drawings(a):          # 試験用の家は登録していない（図面と比べない。実データを読まない）
        raise KeyError(a)
    monkeypatch.setattr(paths, "property_files", no_drawings)
    d = tmp_path / "物件T"
    d.mkdir()
    (d / "building.json").write_text(json.dumps(_band_house(), ensure_ascii=False), encoding="utf-8")
    return "物件T"


def test_approve_and_revert_round_trip(alias, rules):
    from core import findings, overrides
    report = findings.run_off(alias)
    hit = [f for f in report["code_findings"] if (f.get("proposal") or {}).get("field") == "lintel_band_mm"]
    assert len(hit) == 1
    f = findings.decide(alias, hit[0]["id"], "approve")
    assert f["status"] == "承認" and f["override"] == "OV1"
    item = overrides.load(alias)[0]
    assert (item["target"], item["field"], item["from"], item["to"], item["value_source"]) == \
        ("W1", "lintel_band_mm", 0, 100.0, "rule:model.lintel_band_mm")
    raw = json.loads((findings.out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    assert "lintel_band_mm" not in raw["walls"][0]                           # building.json には書かない
    applied, results = overrides.apply(raw, overrides.load(alias))
    assert results[0]["status"] == "applied" and applied["walls"][0]["lintel_band_mm"] == 100.0
    assert not [x for x in findings.code_findings(alias) if (x.get("proposal") or {}).get("field") == "lintel_band_mm"]
    assert any("帯を残した" in n for n in build_model(applied, rules)["floor1"].notes)
    overrides.undo(alias, "OV1")
    findings.revert_override(alias, "OV1")
    back = findings.load(alias)["code_findings"]
    assert next(x for x in back if x["id"] == hit[0]["id"])["status"] == "未確認"
    applied, _ = overrides.apply(raw, overrides.load(alias))
    assert "lintel_band_mm" not in applied["walls"][0]


def test_override_value_checks(alias):
    from core import overrides
    raw = json.loads((overrides.out_dir(alias) / "building.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        overrides.add(alias, raw, "W1", "lintel_band_mm", 5000, "number", "大きすぎる")
    with pytest.raises(KeyError):
        overrides.add(alias, raw, "W5", "lintel_band_mm", 100, "rule:model.lintel_band_mm", "内壁には付けない")
    two = json.loads(json.dumps(raw))
    two["levels"].append({"name": "2F", "fl_mm": 3500, "ceiling_mm": 2400})
    with pytest.raises(ValueError):
        overrides.add(alias, two, "W1", "lintel_band_mm", 100, "rule:model.lintel_band_mm", "2階建て")
    assert overrides.load(alias) == []


def test_ai_tool_proposes_band_from_rule(rules):
    from core.ai.tools import ToolContext, ToolError, propose_override
    b = _band_house()
    ctx = ToolContext(alias="物件T", raw=b, building=b, rules=rules)
    out = propose_override(ctx, "W1", "lintel_band_mm", "rule:model.lintel_band_mm", "窓の間の柱の上をつなぐ")
    assert (out["proposal"]["from"], out["proposal"]["to"]) == (0, 100.0)
    with pytest.raises(ToolError):
        propose_override(ctx, "W5", "lintel_band_mm", "rule:model.lintel_band_mm", "内壁")
