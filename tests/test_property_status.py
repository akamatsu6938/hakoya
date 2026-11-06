"""物件の一覧の進み具合・小さな絵・ファイルが変わったかの口（★11/5）と、一時ファイルに書いてから入れ替える保存。
実データは使わない（架空の物件T・U・V を一時フォルダに作る。中身は数だけ）。"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
from urllib.parse import quote

import keyring
import pytest

from core import findings as fd
from core import overrides as ov
from core import paths

NAMES = ["物件T", "物件U", "物件V"]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "MODE", "own")
    monkeypatch.delenv("HAKOYA_EDITION", raising=False)
    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))
    return tmp_path


@pytest.fixture
def client(data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from app import server
    monkeypatch.setattr(server, "_aliases", lambda: list(NAMES))
    monkeypatch.setattr(server, "property_files", lambda a: paths.PropertyFiles(alias=a, glb=None, jww_folder=None))
    return TestClient(server.app)


def _write(p, data, t: float) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.utime(p, (t, t))


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _png(size=(8, 8)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    return buf.getvalue()


def _url(prefix: str, alias: str) -> str:
    return prefix + quote(alias)


# ---------------------------------------------------------------- GET /api/properties

def test_properties_have_status_and_newest_first(client, data_dir):
    now = 1_790_000_000.0
    t = data_dir / "out" / "物件T"
    _write(t / "building.json", {}, now + 10)
    _write(t / "overrides.json", [{"id": "OV1"}, {"id": "OV2"}], now + 40)
    _write(t / "findings.json", {"findings": [{"id": "F1", "status": "未確認"}, {"id": "F2", "status": "承認"}],
                                 "code_findings": [{"id": "C1", "status": "あとで"}, {"id": "C2", "status": "却下"}]}, now + 20)
    _write(t / "model" / "checks.json", {"checks": [{"level": "高"}, {"level": "高"}, {"level": "中"}, {"level": "情報"}]},
           now + 30)
    u = data_dir / "out" / "物件U"
    _write(u / "building.json", {}, now + 100)           # 検品のあとに組み立て直した（検品が古い）
    _write(u / "findings.json", "{書きかけ", now + 50)     # 読めないファイルで一覧を止めない
    rows = client.get("/api/properties").json()
    assert [r["alias"] for r in rows] == ["物件U", "物件T", "物件V"]           # いちばん新しく触った物件が上
    st = {r["alias"]: r for r in rows}
    assert st["物件T"]["built"] is True and st["物件T"]["thumb"] is False and st["物件T"]["has_glb"] is False
    assert st["物件T"]["status"] == {"built_at": _iso(now + 10), "edited_at": _iso(now + 40), "inspected_at": _iso(now + 20),
                                     "exported_at": _iso(now + 30), "fixes": 2, "open_findings": 2, "high": 2, "stale": False}
    assert st["物件U"]["status"]["stale"] is True and st["物件U"]["status"]["open_findings"] == 0
    assert st["物件U"]["status"]["edited_at"] is None and st["物件U"]["status"]["fixes"] == 0
    assert st["物件V"] == {"alias": "物件V", "has_glb": False, "built": False, "thumb": False,
                           "status": {"built_at": None, "edited_at": None, "inspected_at": None, "exported_at": None,
                                      "fixes": 0, "open_findings": 0, "high": 0, "stale": False}}
    assert dt.datetime.fromisoformat(st["物件T"]["status"]["built_at"]).timestamp() == pytest.approx(now + 10, abs=1)


def test_untouched_houses_keep_the_registered_order(client):
    assert [r["alias"] for r in client.get("/api/properties").json()] == NAMES


# ---------------------------------------------------------------- POST・GET /api/thumb

def test_thumb_is_saved_and_served(client, data_dir):
    url = _url("/api/thumb/", "物件T")
    assert client.get(url).status_code == 404                                 # まだない
    png = _png()
    r = client.post(url, json={"png": "data:image/png;base64," + base64.b64encode(png).decode("ascii")})
    assert r.status_code == 200 and r.json() == {"thumb": True, "bytes": len(png)}
    assert (data_dir / "out" / "物件T" / "thumb.png").read_bytes() == png
    assert [p.name for p in (data_dir / "out" / "物件T").iterdir()] == ["thumb.png"]   # 一時ファイルは残さない
    g = client.get(url)
    assert g.status_code == 200 and g.headers["content-type"] == "image/png" and g.content == png
    assert {r["alias"]: r["thumb"] for r in client.get("/api/properties").json()}["物件T"] is True


@pytest.mark.parametrize("payload, code", [
    ("data:image/jpeg;base64,/9j/4AAQ", 400),                                     # PNG ではない形
    ("data:image/png;base64,これは base64 ではない", 400),
    ("data:image/png;base64," + base64.b64encode(b"GIF89a" + b"\0" * 20).decode("ascii"), 400),   # 中身が PNG ではない
    ("data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (800 * 1024)).decode("ascii"), 413),
])
def test_bad_thumbs_are_refused(client, data_dir, payload, code):
    r = client.post(_url("/api/thumb/", "物件T"), json={"png": payload})
    assert r.status_code == code and r.json()["detail"]
    assert not (data_dir / "out" / "物件T" / "thumb.png").exists()


def test_thumb_needs_a_known_house(client):
    png = "data:image/png;base64," + base64.b64encode(_png()).decode("ascii")
    assert client.post(_url("/api/thumb/", "ない家"), json={"png": png}).status_code == 404
    assert client.get(_url("/api/thumb/", "ない家")).status_code == 404


# ---------------------------------------------------------------- GET /api/state

def test_state_changes_when_any_file_changes(client, data_dir):
    url = _url("/api/state/", "物件T")
    assert client.get(_url("/api/state/", "ない家")).status_code == 404
    assert client.get(url).json() == {"saved_at": None, "version": 0}
    now = 1_790_000_000.0
    d = data_dir / "out" / "物件T"
    _write(d / "building.json", {}, now)
    first = client.get(url).json()
    assert first == {"saved_at": _iso(now), "version": (d / "building.json").stat().st_mtime_ns}
    seen = {first["version"]}
    for i, rel in enumerate(("overrides.json", "findings.json", "print_split.json", "print_options.json", "model/checks.json"), 1):
        _write(d / rel, {}, now + i * 10)                                    # どのファイルが変わっても、数が変わる
        st = client.get(url).json()
        assert st["version"] not in seen and st["saved_at"] == _iso(now + i * 10)
        seen.add(st["version"])
    _write(d / "thumb.png", "x", now + 999)                                   # 小さな絵は数えない（画面が書くもの）
    assert client.get(url).json()["version"] in seen


# ---------------------------------------------------------------- 一時ファイルに書いてから入れ替える保存

def test_saves_go_through_a_temporary_file(data_dir, monkeypatch):
    real = os.replace
    moved = []

    def replace(src, dst):
        moved.append((os.path.basename(src), os.path.basename(dst), os.path.dirname(src) == os.path.dirname(dst)))
        return real(src, dst)
    monkeypatch.setattr(paths.os, "replace", replace)
    ov.save("物件T", [{"id": "OV1"}])
    fd.save("物件T", {"findings": [], "code_findings": []})
    assert [m[1] for m in moved] == ["overrides.json", "findings.json"] and all(m[2] for m in moved)
    assert all(m[0].endswith(".tmp") for m in moved)
    assert ov.load("物件T") == [{"id": "OV1"}] and fd.load("物件T") == {"findings": [], "code_findings": []}
    assert sorted(p.name for p in (data_dir / "out" / "物件T").iterdir()) == ["findings.json", "overrides.json"]


def test_atomic_write_waits_while_another_program_reads(tmp_path, monkeypatch):
    """Windows では、ほかのプロセスが読んでいる最中のファイルは入れ替えられない（PermissionError）。少し待ってやり直す。"""
    real = os.replace
    fails = {"n": 2}

    def busy(src, dst):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError("使用中")
        return real(src, dst)
    monkeypatch.setattr(paths.os, "replace", busy)
    monkeypatch.setattr("time.sleep", lambda s: None)
    target = tmp_path / "x.json"
    target.write_text("old", encoding="utf-8")
    paths.write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new" and [p.name for p in tmp_path.iterdir()] == ["x.json"]
    fails["n"] = 10 ** 6                                                      # ずっと使われている：止めて、元のファイルは残す
    with pytest.raises(PermissionError):
        paths.write_text_atomic(target, "newer")
    assert target.read_text(encoding="utf-8") == "new" and [p.name for p in tmp_path.iterdir()] == ["x.json"]
