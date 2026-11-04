"""会社ルールの切り替え（★11/4 B工務店。SPEC §5）：架空の「B工務店」の決まりで、同じ家が違う模型に作り直され、
会社ルールとの比べ（いつもと違う）も変わる。"""
from __future__ import annotations

import keyring
import pytest
import yaml

from core.rules import RULES_DIR, load_rules


def _keys(d: dict, prefix: str = "") -> set[str]:
    out = set()
    for k, v in d.items():
        if isinstance(v, dict):
            out |= _keys(v, f"{prefix}{k}.")
        else:
            out.add(f"{prefix}{k}")
    return out


def test_b_koumuten_has_every_item_of_jisha():
    """項目が足りないと、組み立ての途中でコードが止まる（会社ルールに ○○ がありません）。"""
    a = yaml.safe_load((RULES_DIR / "jisha.yaml").read_text(encoding="utf-8"))
    b = yaml.safe_load((RULES_DIR / "bkoumuten.yaml").read_text(encoding="utf-8"))
    assert _keys(a) - {"company", "name_ja"} == _keys(b) - {"company", "name_ja"}
    diff = {k for k in _keys(a) - {"company", "name_ja"} if _get(a, k) != _get(b, k)}
    assert {"walls.exterior_thickness_mm", "walls.interior_thickness_mm", "model.roof_overhang_mm", "model.exterior_finish",
            "openings.door_top_fl_mm", "levels.gl_to_foundation_top_mm", "levels.gl_to_1fl_mm"} <= diff


def _get(d: dict, dotted: str):
    for k in dotted.split("."):
        d = d[k]
    return d


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from core import paths
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))   # 本物の資格情報マネージャーは使わない
    return tmp_path


def test_company_setting(data_dir):
    from core import settings_store as ss
    assert {"code": "bkoumuten", "name": "B工務店（架空）"} in ss.companies()
    assert "ai_models" not in {c["code"] for c in ss.companies()}                 # AI の設定は会社ルールではない
    assert ss.load()["company"] == "jisha"
    ss.set_company("bkoumuten")
    assert ss.load()["company"] == "bkoumuten"
    with pytest.raises(ValueError):
        ss.set_company("../jisha")
    (data_dir / "settings.json").write_text('{"company": "nakunatta"}', encoding="utf-8")
    assert ss.load()["company"] == "jisha"                                       # 消した会社ルールなら既定に戻す


def test_company_screen(data_dir):
    from fastapi.testclient import TestClient

    from app.server import app
    c = TestClient(app)
    st = c.get("/api/settings").json()
    assert st["company"] == "jisha" and [x["code"] for x in st["companies"]][:2] == ["jisha", "bkoumuten"]
    assert c.post("/api/settings/company", json={"company": "bkoumuten"}).json() == {"company": "bkoumuten"}
    assert c.get("/api/settings").json()["company"] == "bkoumuten"
    assert c.post("/api/settings/company", json={"company": "nai"}).status_code == 400


@pytest.mark.realdata
def test_same_house_is_rebuilt_with_b_koumuten_rules():
    """物件A を B工務店の決まりで作ると、壁の厚み・屋根板の出・外壁の仕上げが変わり、「いつもと違う」も変わる。"""
    from core.paths import aliases
    if "物件A" not in aliases():
        pytest.skip("物件A がありません")
    from adapters.walkinhome import build
    from core.builder import build_model
    from core.rule_check import check_property
    a, b = build("物件A", "jisha"), build("物件A", "bkoumuten")
    assert b["source"]["company_rules"] == "bkoumuten"
    th = lambda bb, kind: {w["thickness_mm"] for w in bb["walls"] if w["kind"] == kind}
    assert th(a, "exterior") == {162} and th(b, "exterior") == {190}
    assert th(a, "interior") == {130} and th(b, "interior") == {150}
    rb = load_rules("bkoumuten")
    parts = build_model(b, rb)
    assert any("2.0mm" in n for n in parts["roof"].notes)                          # 屋根板の出（破風・鼻隠しの分）
    rows_a = {r["rule_key"]: r["match"] for r in check_property(a, load_rules("jisha")) if "rule_key" in r}
    rows_b = {r["rule_key"]: r["match"] for r in check_property(b, rb) if "rule_key" in r}
    assert rows_a != rows_b and any(v is False for v in rows_b.values())
