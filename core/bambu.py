"""Bambu Studio を使って、Bambu 形式の 3MF（設定ごと読まれる）を作る（入っている PC だけ。なければ何もしない）。

★わかったこと（2026-11-02、Bambu Studio 02.08.02）：
- 画面の Bambu Studio は、Bambu 製と認めない 3MF の設定を捨てて形だけ読む
  （"The 3mf is not from Bambu Lab, load geometry data only."）。部品ごとのファジースキンも消える。
- 印だけ真似た 3MF（Application=BambuStudio-…）は、コマンドで読ませると何も出力されなかった。
- コマンド（bambu-studio.exe --export-3mf）で私たちの 3MF を読ませて書き出させると、部品ごとのファジースキンは残る。
- ただしコマンドは、プリンター設定の「親の設定の引き継ぎ（inherits）」を読み解かない（A1 の造形範囲が 200×200 になった）。
  そこで同梱の設定ファイルの引き継ぎを、ここで全部たどってまとめた完全な設定を渡す。

使う設定（既定）：Bambu Lab A1 0.4 nozzle / 0.20mm Standard @BBL A1 / Bambu PLA Basic @BBL A1。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_EXE = Path(r"C:\Program Files\Bambu Studio\bambu-studio.exe")
PRESETS = {"machine": "Bambu Lab A1 0.4 nozzle", "process": "0.20mm Standard @BBL A1", "filament": "Bambu PLA Basic @BBL A1"}


def find_exe() -> Path | None:
    return DEFAULT_EXE if DEFAULT_EXE.exists() else None


def resolve_preset(folder: Path, name: str) -> dict:
    """設定ファイルの引き継ぎ（inherits）をたどって、親から順に上書きした完全な設定を返す。"""
    chain = []
    seen = set()
    cur = name
    while cur:
        if cur in seen:
            raise ValueError(f"設定の引き継ぎが循環しています：{cur}")
        seen.add(cur)
        path = folder / f"{cur}.json"
        if not path.exists():
            raise FileNotFoundError(f"Bambu Studio の設定ファイルが見つかりません：{path.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        chain.append(data)
        cur = data.get("inherits", "")
    merged: dict = {}
    for data in reversed(chain):
        merged.update({k: v for k, v in data.items() if k != "inherits"})
    merged["name"] = name
    merged["from"] = "system"
    merged.pop("inherits", None)
    return merged


def to_bambu_3mf(src: Path, dst: Path, exe: Path | None = None, presets: dict | None = None, timeout: int = 180) -> dict:
    """src（私たちの 3MF）を Bambu Studio のコマンドで読ませ、Bambu 形式の 3MF を dst に書き出す。"""
    exe = exe or find_exe()
    if exe is None:
        return {"ok": False, "reason": "Bambu Studio が見つかりません（Bambu 形式の 3MF は作りませんでした）"}
    presets = {**PRESETS, **(presets or {})}
    profiles = exe.parent / "resources" / "profiles" / "BBL"
    with tempfile.TemporaryDirectory(prefix="hakoya_bambu_") as tmp:
        t = Path(tmp)
        files = {}
        for kind in ("machine", "process", "filament"):
            data = resolve_preset(profiles / kind, presets[kind])
            files[kind] = t / f"{kind}.json"           # パスは英字だけにする（コマンドに日本語を渡さない）
            files[kind].write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        shutil.copyfile(src, t / "in.3mf")
        out = t / "out"
        out.mkdir()
        cmd = [str(exe), "--load-settings", f"{files['machine']};{files['process']}", "--load-filaments", str(files["filament"]),
               "--outputdir", str(out), "--export-3mf", "model.3mf", str(t / "in.3mf")]
        try:
            subprocess.run(cmd, timeout=timeout, capture_output=True)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "Bambu Studio のコマンドが時間内に終わりませんでした"}
        result = {}
        if (out / "result.json").exists():
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
        if not (out / "model.3mf").exists() or result.get("return_code") != 0:
            return {"ok": False, "reason": f"Bambu Studio のコマンドが失敗しました（{result.get('error_string', '結果なし')}）"}
        shutil.copyfile(out / "model.3mf", dst)
    return {"ok": True, "presets": presets}


def inspect_3mf(path: Path) -> dict:
    """Bambu 形式の 3MF の中身（プリンター設定の主な値と、部品ごとのファジースキン）を読む（確かめ用）。"""
    import re
    import zipfile
    z = zipfile.ZipFile(path)
    ps = json.loads(z.read("Metadata/project_settings.config"))
    ms = z.read("Metadata/model_settings.config").decode("utf-8")
    parts = {}
    for part in re.findall(r'<part id="\d+".*?</part>', ms, re.S):
        name = re.search(r'key="name" value="([^"]*)"', part).group(1)
        parts[name] = dict(re.findall(r'key="(fuzzy_skin[^"]*)" value="([^"]*)"', part))
    return {"printer_model": ps.get("printer_model"), "printer_settings_id": ps.get("printer_settings_id"),
            "print_settings_id": ps.get("print_settings_id"), "printable_area": ps.get("printable_area"),
            "printable_height": ps.get("printable_height"), "parts": parts}
