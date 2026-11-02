"""Bambu 形式の 3MF（core/bambu.py）。Bambu Studio が入っている PC だけで確かめる（なければ飛ばす）。"""
from __future__ import annotations

import pytest

from core.bambu import find_exe, resolve_preset

pytestmark = pytest.mark.skipif(find_exe() is None, reason="Bambu Studio が入っていない")


def test_resolve_a1_preset_inherits():
    """A1 の設定は造形範囲を親の設定から引き継ぐ。たどってまとめると 256×256 になる。"""
    prof = find_exe().parent / "resources" / "profiles" / "BBL"
    m = resolve_preset(prof / "machine", "Bambu Lab A1 0.4 nozzle")
    assert m["printable_area"] == ["0x0", "256x0", "256x256", "0x256"] and m["printable_height"] == "256"
    assert m["printer_model"] == "Bambu Lab A1" and "inherits" not in m


@pytest.mark.realdata
@pytest.mark.slow
def test_bambu_3mf_keeps_fuzzy_only_on_exterior():
    from core.bambu import inspect_3mf
    from core.export import write_outputs
    from core.paths import out_dir
    s = write_outputs("物件A")
    assert s["bambu_3mf"]["ok"], s["bambu_3mf"]
    info = inspect_3mf(out_dir("物件A") / "model" / "model_bambu.3mf")
    assert info["printer_model"] == "Bambu Lab A1" and info["printable_area"][2] == "256x256"
    fuzzy = sorted(n for n, p in info["parts"].items() if p.get("fuzzy_skin") == "external")
    assert fuzzy == ["1階の外壁", "屋根の外壁"]
