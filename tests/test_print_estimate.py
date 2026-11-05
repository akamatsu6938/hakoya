"""印刷の見込みの道具（tools/print_estimate.py）。結果を読む所は、小さな架空の slice_info などで確かめる。
実際にスライスするテストは、Bambu Studio が入っている PC だけ（なければ飛ばす）。"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile

import numpy as np
import pytest

from core.bambu import find_exe
from tools import print_estimate as pe

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value="02.08.02.61"/>
  </header>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="prediction" value="9000"/>
    <metadata key="weight" value="40.50"/>
    <metadata key="outside" value="false"/>
    <metadata key="support_used" value="false"/>
    <object identify_id="16" name="1階" skipped="false" />
    <filament id="1" tray_info_idx="GFA00" type="PLA" color="#00AE42" used_m="13.40" used_g="40.50" />
    <warning msg="some_new_warning" level="1" error_code ="12345678"  />
  </plate>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="4800"/>
    <metadata key="weight" value="18.24"/>
    <metadata key="outside" value="false"/>
    <metadata key="support_used" value="false"/>
    <object identify_id="8" name="基礎" skipped="false" />
    <object identify_id="9" name="外した部品" skipped="true" />
    <filament id="1" tray_info_idx="GFA00" type="PLA" color="#00AE42" used_m="6.05" used_g="18.24" />
    <warning msg="not_support_traditional_timelapse" level="2" error_code ="10018003"  />
  </plate>
</config>
"""

MODEL_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="3">
    <metadata key="name" value="1階"/>
    <metadata key="extruder" value="1"/>
    <metadata face_count="590"/>
    <part id="1" subtype="normal_part">
      <metadata key="name" value="1階（床・内壁）"/>
      <mesh_stat face_count="326"/>
    </part>
    <part id="2" subtype="normal_part">
      <metadata key="name" value="1階の外壁（表面）"/>
      <metadata key="fuzzy_skin" value="external"/>
      <metadata key="fuzzy_skin_point_distance" value="0.5"/>
      <metadata key="fuzzy_skin_thickness" value="0.2"/>
    </part>
  </object>
  <object id="8">
    <metadata key="name" value="基礎"/>
    <part id="7" subtype="normal_part"><metadata key="name" value="基礎"/></part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <model_instance><metadata key="object_id" value="8"/><metadata key="identify_id" value="8"/></model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <model_instance><metadata key="object_id" value="3"/><metadata key="identify_id" value="16"/></model_instance>
  </plate>
</config>
"""

PROJECT_SETTINGS = {"printer_settings_id": "Bambu Lab A1 0.4 nozzle", "print_settings_id": "0.20mm Standard @BBL A1",
                    "filament_settings_id": ["Bambu PLA Basic @BBL A1"], "layer_height": "0.2", "wall_loops": "2",
                    "sparse_infill_density": "15%", "sparse_infill_pattern": "grid", "top_shell_layers": "5",
                    "bottom_shell_layers": "3", "enable_support": "0", "support_type": "tree(auto)",
                    "filament_density": ["1.26"]}


def _gcode(top: float, layers: list[float]) -> str:
    head = f"; HEADER_BLOCK_START\n; model printing time: 1h; total estimated time: 1h\n; max_z_height: {top:.2f}\n; HEADER_BLOCK_END\n"
    return head + "".join(f"; CHANGE_LAYER\n; Z_HEIGHT: {z:g}\n; LAYER_HEIGHT: 0.2\nG1 X1 Y1 E0.1\n" for z in layers)


def _fake_sliced_3mf() -> zipfile.ZipFile:
    """Bambu Studio がスライスして書き出す 3MF の、道具が読む所だけを真似たもの（2つのプレート）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE_INFO)
        z.writestr("Metadata/model_settings.config", MODEL_SETTINGS)
        z.writestr("Metadata/project_settings.config", json.dumps(PROJECT_SETTINGS))
        z.writestr("Metadata/plate_1.gcode", _gcode(11.2, [0.2, 0.4, 11.2]))
        z.writestr("Metadata/plate_2.gcode", _gcode(50.8, [0.2, 0.4, 23.6]))   # 上の方が印刷されない
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


def test_parse_slice_info_reads_each_plate():
    info = pe.parse_slice_info(SLICE_INFO)
    assert info["version"] == "02.08.02.61"
    first, second = info["plates"]                      # プレートの番号の順に並べ直す
    assert (first["plate"], first["objects"], first["seconds"], first["grams"]) == (1, ["基礎"], 4800, 18.24)
    assert first["filament_m"] == 6.05 and first["support_used"] is False and first["outside"] is False
    assert first["warnings"] == [{"msg": "not_support_traditional_timelapse", "level": "2", "error_code": "10018003"}]
    assert (second["plate"], second["objects"], second["seconds"], second["grams"]) == (2, ["1階"], 9000, 40.5)
    used = pe.parse_slice_info(SLICE_INFO.replace('"support_used" value="false"', '"support_used" value="true"', 1))
    assert used["plates"][1]["support_used"] is True


def test_parse_model_parts_maps_plates_to_parts_and_fuzzy_skin():
    parts = pe.parse_model_parts(MODEL_SETTINGS)
    floor = parts["by_identify_id"]["16"]
    assert [p["name"] for p in floor] == ["1階（床・内壁）", "1階の外壁（表面）"]
    assert "fuzzy_skin" not in floor[0]
    assert floor[1] == {"name": "1階の外壁（表面）", "fuzzy_skin": "external", "fuzzy_skin_thickness_mm": "0.2",
                        "fuzzy_skin_point_distance_mm": "0.5"}
    assert parts["by_name"]["基礎"] == [{"name": "基礎"}]


def test_gcode_heights_reads_model_top_and_last_printed_layer():
    h = pe.gcode_heights(io.StringIO(_gcode(62.8, [0.2, 0.4, 23.6])))
    assert h == {"model_top_mm": 62.8, "printed_top_mm": 23.6}
    assert pe.gcode_heights(io.StringIO("G1 X0\n")) == {"model_top_mm": None, "printed_top_mm": None}


@pytest.mark.parametrize("seconds, text", [(4800, "1時間20分"), (3600, "1時間"), (3629, "1時間"), (3630, "1時間1分"),
                                           (59, "1分"), (20, "1分未満"), (41405, "11時間30分")])
def test_format_duration(seconds, text):
    assert pe.format_duration(seconds) == text


def test_read_sliced_3mf_summary_and_japanese_report():
    with _fake_sliced_3mf() as z:
        sliced = pe.read_sliced_3mf(z)
    assert sliced["bambu_studio"] == "02.08.02.61"
    assert sliced["settings"]["printer"] == "Bambu Lab A1 0.4 nozzle" and sliced["settings"]["filament"] == "Bambu PLA Basic @BBL A1"
    base, floor = sliced["plates"]
    assert [p["name"] for p in floor["parts"]] == ["1階（床・内壁）", "1階の外壁（表面）"]
    assert (base["model_top_mm"], base["printed_top_mm"]) == (11.2, 11.2)
    assert (floor["model_top_mm"], floor["printed_top_mm"]) == (50.8, 23.6)

    s = pe.summarize(sliced, "model_bambu.3mf", yen_per_kg=(2400, 3400))
    assert s["total"] == {"prints": 2, "seconds": 13800, "time": "3時間50分", "grams": 58.74}
    assert (s["material_cost_yen"]["low"], s["material_cost_yen"]["high"]) == (140, 200)   # 58.74g × 2,400〜3,400円/kg
    assert s["material_cost_yen"]["estimate"] is True and s["material_cost_yen"]["source"]
    assert base["notes"] == []                                           # A1 の縮時撮影の知らせは出さない
    assert any("50.8mm" in n and "23.6mm" in n for n in floor["notes"])  # 上の方が印刷されないことを知らせる
    assert any("some_new_warning" in n for n in floor["notes"])          # 知らない注意はそのまま出す
    assert any("サポートなし" in n for n in s["notes"])
    json.dumps(s, ensure_ascii=False)                                    # そのまま JSON に書ける

    text = pe.report_text(s)
    assert "基礎　1時間20分・18g" in text and "1階　2時間30分・41g" in text   # 40.5g は 41g（四捨五入）
    assert "合計（2回の印刷）：3時間50分・59g" in text
    assert "約 140〜200 円" in text and "推定" in text
    assert "ファジースキン（3MF の部品ごとの設定）：1階の外壁（表面）" in text
    assert "Bambu Lab A1 0.4 nozzle ／ 0.20mm Standard @BBL A1 ／ Bambu PLA Basic @BBL A1" in text


def test_full_preset_adds_included_templates(tmp_path):
    """引き継ぎ（inherits）に加えて、取り込み（include）の G-code も入る。"""
    (tmp_path / "common.json").write_text(json.dumps({"name": "common", "machine_start_gcode": "G28 ;共通",
                                                      "printable_height": "250"}), encoding="utf-8")
    (tmp_path / "printer.json").write_text(json.dumps({"name": "printer", "inherits": "common", "printable_height": "256",
                                                       "include": ["printer template start"]}), encoding="utf-8")
    (tmp_path / "printer template start.json").write_text(json.dumps({"name": "printer template start", "instantiation": "false",
                                                                      "machine_start_gcode": ";===== machine: A1"}), encoding="utf-8")
    data = pe.full_preset(tmp_path, "printer")
    assert data["machine_start_gcode"] == ";===== machine: A1" and data["printable_height"] == "256"
    assert "include" not in data and "inherits" not in data and data["name"] == "printer"


def test_outputs_ready_waits_for_complete_result(tmp_path):
    assert not pe._outputs_ready(tmp_path)                                   # まだない
    (tmp_path / "result.json").write_text('{"return_code": 0, "err', encoding="utf-8")
    assert not pe._outputs_ready(tmp_path)                                   # 書きかけ
    (tmp_path / "result.json").write_text('{"return_code": 0}', encoding="utf-8")
    assert not pe._outputs_ready(tmp_path)                                   # 3MF がまだ
    (tmp_path / "sliced.3mf").write_bytes(b"PK\x03\x04 half")
    assert not pe._outputs_ready(tmp_path)                                   # 3MF が書きかけ
    with zipfile.ZipFile(tmp_path / "sliced.3mf", "w") as z:
        z.writestr("Metadata/slice_info.config", SLICE_INFO)
    assert pe._outputs_ready(tmp_path)
    (tmp_path / "result.json").write_text('{"return_code": -5, "error_string": "失敗"}', encoding="utf-8")
    (tmp_path / "sliced.3mf").unlink()
    assert pe._outputs_ready(tmp_path)                                       # 失敗の結果も「出た」


# Bambu Studio の代わり：結果を書いて終わる（exit）・結果を書いたあと終わらない（hang）・何も書かない（silent）
FAKE_BAMBU = r'''
import json, sys, time, zipfile
from pathlib import Path
out, mode = Path(sys.argv[1]), sys.argv[2]
if mode != "silent":
    with zipfile.ZipFile(out / "sliced.3mf", "w") as z:
        z.writestr("Metadata/slice_info.config", "<config/>")
    (out / "result.json").write_text(json.dumps({"return_code": 0, "error_string": "Success."}))
if mode != "exit":
    time.sleep(10)
'''


@pytest.mark.parametrize("mode, timeout, expected", [("exit", 8, "exited"), ("hang", 8, "stuck"), ("silent", 3, "timeout")])
def test_run_bambu_does_not_wait_forever(tmp_path, monkeypatch, mode, timeout, expected):
    """★結果を書いたあと終わらない Bambu Studio があったので、結果を見て待ち、止まっていれば止める。"""
    monkeypatch.setattr(pe, "EXIT_GRACE_S", 1)
    script = tmp_path / "fake_bambu.py"
    script.write_text(FAKE_BAMBU, encoding="utf-8")
    python = getattr(sys, "_base_executable", sys.executable)   # 仮想環境の起動役を通さない（止めたときに子が残らない）
    t0 = time.monotonic()
    assert pe.run_bambu([python, str(script), str(tmp_path), mode], tmp_path, tmp_path, timeout) == expected
    assert time.monotonic() - t0 < 9.5                          # 10 秒の居眠りを待たない


needs_bambu = pytest.mark.skipif(find_exe() is None, reason="Bambu Studio が入っていない")


@needs_bambu
def test_real_a1_preset_takes_start_gcode_from_template():
    """★Bambu Studio 02.08 の A1 は、始めの G-code を include のファイルに持つ（引き継ぎだけだと共通の短いもの）。"""
    prof = find_exe().parent / "resources" / "profiles" / "BBL" / "machine"
    data = pe.full_preset(prof, "Bambu Lab A1 0.4 nozzle")
    assert data["machine_start_gcode"].startswith(";===== machine: A1")
    assert data["printable_area"] == ["0x0", "256x0", "256x256", "0x256"]


def _box(w: float, d: float, h: float, x0: float = 0.0):
    v = np.array([[0, 0, 0], [w, 0, 0], [w, d, 0], [0, d, 0], [0, 0, h], [w, 0, h], [w, d, h], [0, d, h]], float)
    v[:, 0] += x0
    t = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                  [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, t


@needs_bambu
@pytest.mark.slow
def test_real_slice_of_two_small_boxes(tmp_path):
    """HAKOYA の 3MF（core/threemf.py）と同じ形の小さな箱2つを、本物の Bambu Studio でスライスする。"""
    from core.threemf import write_3mf
    va, ta = _box(20, 20, 3)
    vb, tb = _box(20, 20, 3)
    objects = [{"name": "箱A", "parts": [{"name": "箱A", "verts": va, "tris": ta, "fuzzy_skin": False}]},
               {"name": "箱B", "parts": [{"name": "箱Bの外壁", "verts": vb, "tris": tb, "fuzzy_skin": True}]}]
    src = tmp_path / "boxes.3mf"
    write_3mf(src, objects, {"thickness": 0.2, "point_distance": 0.5})
    res = pe.slice_3mf(src, timeout=300)
    assert res["ok"], res
    assert "cleanup" not in res                                              # 作業フォルダは消えた
    names = sorted(n for p in res["plates"] for n in p["objects"])
    assert names == ["箱A", "箱B"]
    assert all(p["seconds"] > 0 and p["grams"] > 0 for p in res["plates"])
    fuzzy = [x["name"] for p in res["plates"] for x in p["parts"] if x.get("fuzzy_skin") == "external"]
    assert fuzzy == ["箱Bの外壁"]
    assert all(abs(p["model_top_mm"] - p["printed_top_mm"]) < 0.3 for p in res["plates"])
    assert res["settings"]["printer"] == "Bambu Lab A1 0.4 nozzle"
    s = pe.summarize(res, src.name)
    assert s["total"]["seconds"] == sum(p["seconds"] for p in res["plates"])
    assert s["total"]["grams"] == pytest.approx(sum(p["grams"] for p in res["plates"]))
