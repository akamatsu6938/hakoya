r"""3MF を Bambu Studio のコマンドでスライスして、印刷の見込みの時間とフィラメントの重さを部品ごとに出す
（作者が画面で手でスライスする代わり。画面は出ない）。

使い方（リポジトリの一番上のフォルダで。PYTHONUTF8=1）:
  .venv\Scripts\python.exe -m tools.print_estimate <3MF の場所> [--json 出力の場所] [--support]
                                                 [--timeout 秒] [--yen-per-kg 安いとき 高いとき]
  例: .venv\Scripts\python.exe -m tools.print_estimate data\out\物件A\model\model_bambu.3mf --json build\estimate.json
  --json        結果を JSON でも書く（UTF-8）
  --support     サポート（ツリー・自動）を付けてスライスする（既定は付けない＝0.20mm Standard のまま）
  --timeout     Bambu Studio のコマンドを待つ上限（秒。既定 900。超えたら止めて、失敗として返す）
  --yen-per-kg  材料費の目安の 1kg あたりの値段（円。既定 2400 3400）
  終了コード：0＝出せた、1＝出せなかった（理由を出す）。

何をするか:
  1. 3MF を英字だけの作業フォルダ（%TEMP%\hakoya_estimate_*）に写す。元の 3MF には書き込まない。
  2. Bambu Studio（C:\Program Files\Bambu Studio\bambu-studio.exe）を Python の subprocess から動かして、
     すべてのプレートをスライスする（書き方は Bambu Studio の wiki「Command Line Usage」。--slice 0＝全部のプレート）:
       bambu-studio.exe --slice 0 --load-settings "machine.json;process.json" --load-filaments filament.json
                        [--enable-support=1] --outputdir out --export-3mf sliced.3mf in.3mf
     設定は core/bambu.py と同じ既定（Bambu Lab A1 0.4 nozzle / 0.20mm Standard @BBL A1 / Bambu PLA Basic @BBL A1）。
     引き継ぎ（inherits）に加えて取り込み（include。A1 の始めと終わりの G-code など）もたどった完全な設定を渡す（full_preset）。
     部品ごとの設定（外壁のファジースキン）は 3MF に入っているものが効く。
     Bambu Studio は書き出しのあと終わり際で止まることがある（run_bambu の★）。結果ができてから 30 秒たっても
     終わらなければ止めて、書き出し済みの結果を使う。
  3. 書き出された 3MF の Metadata/slice_info.config から、プレートごとの見込みの時間（prediction・秒。G-code の頭の
     「total estimated time」と同じ）と重さ（weight・g）、載っている部品を読む。G-code から印刷される高さも確かめる。
  4. プレートごとと合計を、日本語と JSON で出す。作業フォルダは最後に消す。

部品とプレート：HAKOYA の Bambu 形式の 3MF（core/bambu.py）は、部品（オブジェクト）ごとに別のプレートに載る
（1枚の造形板に全部は載らない）。プレートは別々の印刷（1回ずつ準備から始まる）なので、プレートごとの見込み＝部品ごとの見込みで、
合計はその足し算。1つのプレートに部品が2つ以上載っている 3MF なら、そのプレートの数字はまとめての値（部品の名前を並べて出す）。

数字の確からしさ（推定）:
- 時間は Bambu Studio の見込み（G-code の動きを、プリンターの速さ・加速度の決まりで計算したもの）。始めの手順の動きは入るが、
  温まるのを待つ時間・自動の水平出しにかかる時間はほとんど入らない（1回の印刷につき数分 長くなることがある。要検証）。
- 重さは、押し出すフィラメントの長さ × 断面 × 密度（Bambu PLA Basic は 1.26g/cm³）。失敗や試しの印刷の分は入らない。
- ファジースキンは点をでたらめに揺らすので、同じ 3MF でもスライスのたびに時間が少し（試しで 0.1% ほど）変わる。

材料費の目安（推定）：重さ × 1kg あたりの値段（YEN_PER_KG・PRICE_SOURCE）。値段は変わるので、--yen-per-kg で直せる。
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from ctypes import wintypes
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.bambu import PRESETS, TEMPLATE_SKIP, find_exe, full_preset, resolve_preset  # noqa: E402  ★11/6 full_preset は core へ移した

DEFAULT_TIMEOUT_S = 900
EXIT_GRACE_S = 30           # 結果（result.json）ができてから、Bambu Studio が自分で終わるのを待つ秒数
YEN_PER_KG = (2400, 3400)
PRICE_SOURCE = ("Bambu PLA Basic 1kg・税込：Bambu Lab の日本の公式ストアの表示 2,400〜2,800円、国内の販売店約3,400円（2026-11-06 に Web 検索で確かめた値段。値段は変わる）")
# 「従来の縮時撮影（timelapse）」についての知らせ。A1 の既定のスライスで毎回出る。時間と重さには関係ないので、
# 日本語の結果には出さない（JSON には残す）
QUIET_WARNINGS = {"not_support_traditional_timelapse"}
HEIGHT_GAP_MM = 1.0         # 模型の高さより、印刷される高さがこれ以上低ければ知らせる
SETTING_KEYS = {"printer": "printer_settings_id", "process": "print_settings_id", "filament": "filament_settings_id",
                "layer_height_mm": "layer_height", "wall_loops": "wall_loops", "infill": "sparse_infill_density",
                "infill_pattern": "sparse_infill_pattern", "top_layers": "top_shell_layers",
                "bottom_layers": "bottom_shell_layers", "enable_support": "enable_support", "support_type": "support_type",
                "filament_density": "filament_density"}
NOTES = [
    "時間は Bambu Studio の見込み。始めの手順の動きは入るが、温まるのを待つ時間・自動の水平出しの時間はほとんど入らない"
    "（1回の印刷につき数分 長くなることがある。推定・要検証）",
    "重さは押し出すフィラメントの長さ × 断面 × 密度から。失敗や試しの印刷の分は入らない",
    "ファジースキンは点をでたらめに揺らすので、スライスのたびに時間が少し変わる（試しで 0.1% ほど）",
]


def parse_slice_info(text: str) -> dict:
    """Metadata/slice_info.config（スライスした 3MF の中）から、Bambu Studio の版と、プレートごとの見込みを読む。"""
    root = ET.fromstring(text)
    version = next((h.get("value") for h in root.iter("header_item") if h.get("key") == "X-BBL-Client-Version"), None)
    plates = []
    for plate in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata") if m.get("key")}
        objects = [o for o in plate.findall("object") if o.get("skipped") != "true"]
        plates.append({
            "plate": int(meta.get("index") or 0),
            "objects": [o.get("name", "") for o in objects],
            "identify_ids": [o.get("identify_id", "") for o in objects],
            "seconds": int(float(meta.get("prediction") or 0)),
            "grams": float(meta.get("weight") or 0),
            "filament_m": round(sum(float(f.get("used_m") or 0) for f in plate.findall("filament")), 2),
            "support_used": meta.get("support_used") == "true",
            "outside": meta.get("outside") == "true",
            "warnings": [{"msg": w.get("msg", ""), "level": w.get("level"), "error_code": w.get("error_code")}
                         for w in plate.findall("warning")],
        })
    return {"version": version, "plates": sorted(plates, key=lambda p: p["plate"])}


def parse_model_parts(text: str) -> dict:
    """Metadata/model_settings.config から、部品（オブジェクト）ごとの中の部品とファジースキンの設定を読む。
    返す形：{"by_identify_id": {identify_id: [部品]}, "by_name": {オブジェクトの名前: [部品]}}"""
    def meta(el) -> dict:
        return {m.get("key"): m.get("value") for m in el.findall("metadata") if m.get("key")}

    root = ET.fromstring(text)
    objects, by_name = {}, {}
    for obj in root.findall("object"):
        parts = []
        for part in obj.findall("part"):
            m = meta(part)
            item = {"name": m.get("name", "")}
            if m.get("fuzzy_skin") and m.get("fuzzy_skin") != "none":
                item.update({"fuzzy_skin": m["fuzzy_skin"], "fuzzy_skin_thickness_mm": m.get("fuzzy_skin_thickness"),
                             "fuzzy_skin_point_distance_mm": m.get("fuzzy_skin_point_distance")})
            parts.append(item)
        objects[obj.get("id")] = parts
        by_name.setdefault(meta(obj).get("name", ""), parts)
    by_id = {}
    for plate in root.findall("plate"):
        for inst in plate.findall("model_instance"):
            m = meta(inst)
            if m.get("identify_id") and m.get("object_id") in objects:
                by_id[m["identify_id"]] = objects[m["object_id"]]
    return {"by_identify_id": by_id, "by_name": by_name}


def gcode_heights(lines) -> dict:
    """G-code の頭の max_z_height（模型のいちばん上）と、最後に印刷する層の高さ（"; Z_HEIGHT:"）。"""
    top = printed = None
    for line in lines:
        if line.startswith("; Z_HEIGHT:"):
            printed = float(line.split(":", 1)[1])
        elif top is None and line.startswith("; max_z_height:"):
            top = float(line.split(":", 1)[1])
    return {"model_top_mm": top, "printed_top_mm": printed}


def pick_settings(project: dict) -> dict:
    """project_settings.config（実際にスライスに使った設定）から、結果に書く主な値を選ぶ（並びの値は1つ目）。"""
    out = {}
    for key, src in SETTING_KEYS.items():
        v = project.get(src)
        out[key] = v[0] if isinstance(v, list) and v else v
    return out


def read_sliced_3mf(z: zipfile.ZipFile) -> dict:
    """スライスした 3MF から、プレートごとの見込み・中の部品・印刷される高さと、使った設定を読む。"""
    names = set(z.namelist())
    if "Metadata/slice_info.config" not in names:
        raise ValueError("スライスした 3MF に Metadata/slice_info.config がありません")
    info = parse_slice_info(z.read("Metadata/slice_info.config").decode("utf-8"))
    parts = parse_model_parts(z.read("Metadata/model_settings.config").decode("utf-8")) \
        if "Metadata/model_settings.config" in names else {"by_identify_id": {}, "by_name": {}}
    for p in info["plates"]:
        p["parts"] = [x for iid, name in zip(p["identify_ids"], p["objects"])
                      for x in parts["by_identify_id"].get(iid) or parts["by_name"].get(name) or []]
        gname = f"Metadata/plate_{p['plate']}.gcode"
        if gname in names:
            with z.open(gname) as fh:
                p.update(gcode_heights(io.TextIOWrapper(fh, encoding="utf-8", errors="replace")))
    return {"bambu_studio": info["version"], "settings": pick_settings(json.loads(z.read("Metadata/project_settings.config"))),
            "plates": info["plates"]}


def _work_dir() -> Path:
    """作業フォルダ（%TEMP% の下）。Bambu Studio のコマンドに日本語のパスを渡さないため、英字だけの場所でなければ止める。"""
    base = Path(tempfile.gettempdir())
    if not str(base).isascii():
        raise RuntimeError("作業フォルダ（TEMP）の場所に英字以外の文字があります。英字だけの場所にしてから動かしてください")
    return Path(tempfile.mkdtemp(prefix="hakoya_estimate_", dir=base))


def slice_3mf(src: Path, exe: Path | None = None, support: bool = False, timeout: int = DEFAULT_TIMEOUT_S,
              presets: dict | None = None) -> dict:
    """src（3MF）のすべてのプレートを Bambu Studio のコマンドでスライスし、プレートごとの見込みを返す。"""
    exe = exe or find_exe()
    if exe is None:
        return {"ok": False, "reason": "Bambu Studio が見つかりません（C:\\Program Files\\Bambu Studio に入っている PC で動かす）"}
    presets = {**PRESETS, **(presets or {})}
    try:
        work = _work_dir()
    except RuntimeError as e:
        return {"ok": False, "reason": str(e)}
    try:
        res = _slice_in(work, Path(src), exe, presets, support, timeout)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if work.exists():
        res.setdefault("cleanup", f"作業フォルダを消せませんでした（手で消してください）：{work}")
    return res


def _slice_in(work: Path, src: Path, exe: Path, presets: dict, support: bool, timeout: int) -> dict:
    profiles = exe.parent / "resources" / "profiles" / "BBL"
    files = {}
    for kind in ("machine", "process", "filament"):
        files[kind] = work / f"{kind}.json"
        files[kind].write_text(json.dumps(full_preset(profiles / kind, presets[kind]), ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(src, work / "in.3mf")
    out = work / "out"
    out.mkdir()
    extra = ["--enable-support=1"] if support else []
    cmd = [str(exe), "--slice", "0", "--load-settings", f"{files['machine']};{files['process']}",
           "--load-filaments", str(files["filament"]), *extra, "--outputdir", str(out), "--export-3mf", "sliced.3mf",
           str(work / "in.3mf")]
    status = run_bambu(cmd, work, out, timeout)
    if status == "timeout":
        return {"ok": False, "reason": f"Bambu Studio のスライスが {timeout} 秒で終わりませんでした（--timeout で延ばせます）"}
    result = _read_result(out) or {}
    if result.get("return_code") != 0 or not (out / "sliced.3mf").exists():
        return {"ok": False, "reason": f"Bambu Studio のスライスが失敗しました（{result.get('error_string') or '結果なし'}）"}
    with zipfile.ZipFile(out / "sliced.3mf") as z:
        sliced = read_sliced_3mf(z)
    command = " ".join(["bambu-studio.exe --slice 0 --load-settings machine.json;process.json --load-filaments filament.json",
                        *extra, "--outputdir out --export-3mf sliced.3mf in.3mf"])
    res = {"ok": True, "presets": presets, "bambu_command": command, **sliced}
    if status == "stuck":
        res["bambu_exit"] = ("Bambu Studio が書き出しのあと終わり際で止まったので、こちらで止めました"
                             "（結果は書き出し済みのものを使いました）")
    return res


def _read_result(out: Path) -> dict | None:
    """Bambu Studio の result.json（最後に書かれる。return_code と error_string）。まだない・書きかけなら None。"""
    try:
        data = json.loads((out / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "return_code" in data else None


def _outputs_ready(out: Path) -> bool:
    """結果がそろったか（result.json があり、うまくいったなら sliced.3mf も最後まで書けている）。"""
    result = _read_result(out)
    if result is None:
        return False
    return result.get("return_code") != 0 or zipfile.is_zipfile(out / "sliced.3mf")


def run_bambu(cmd: list[str], cwd: Path, out: Path, timeout: int) -> str:
    """Bambu Studio を動かして、結果ができるまで待つ。返す値："exited"（自分で終わった）・"stuck"（結果を書いたあと
    終わり際で止まったので止めた）・"timeout"（時間内に結果が出なかったので止めた）。

    ★2026-11-06（Bambu Studio 02.08.02）：スライスと書き出しが済んで result.json もできたのに、終わり際で止まることがあった
    （試しの9回のうち1回）。終了コード 0 を出したあと、残った1つのスレッドが待ったままで、TerminateProcess も効かない
    （終わりかけのプロセスのため）。そのとき Intel の OpenGL の部品（igxelpicd64.dll）が読み込まれていた（原因はその
    終わりの処理と思われる。推定）。subprocess.run（capture_output）は、時間の上限を過ぎても管が閉じるのを待ち続けた。
    そこで出力は管を使わずに捨て、result.json を見て待ち、止まっていたら残りのスレッドを止めて片付ける。"""
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start, ready_at = time.monotonic(), None
    while True:
        try:
            proc.wait(timeout=1)
            return "exited"
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if ready_at is None and _outputs_ready(out):
            ready_at = now
        if ready_at is not None and now - ready_at >= EXIT_GRACE_S:
            _force_end(proc)
            return "stuck"
        if now - start >= timeout:
            _force_end(proc)
            return "timeout"


def _force_end(proc: subprocess.Popen) -> None:
    """Bambu Studio を止める。終わりかけで止まっているときは TerminateProcess が効かないので、残りのスレッドを止める。
    （終わりかけのプロセスは終了コード 0 を持つので、kill のあとの Popen.wait は「終わった」とすぐ返る。Windows に直接聞く）"""
    proc.kill()
    if sys.platform != "win32":
        proc.wait()
        return
    if not _wait_exited(proc.pid, 5):    # Popen が手を持っている間は、同じ番号が別のプロセスに使われない
        _terminate_threads(proc.pid)
        _wait_exited(proc.pid, 10)


class _ThreadEntry32(ctypes.Structure):
    """Windows の THREADENTRY32（スレッドの一覧の1行）。"""
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wintypes.DWORD)]


def _kernel32():
    """Windows の kernel32（使う関数の引数の型を決めたもの）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes, k32.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    k32.WaitForSingleObject.argtypes, k32.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    k32.CreateToolhelp32Snapshot.argtypes, k32.CreateToolhelp32Snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    k32.Thread32First.argtypes = k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    k32.OpenThread.argtypes, k32.OpenThread.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    k32.TerminateThread.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _wait_exited(pid: int, seconds: float) -> bool:
    """Windows：プロセス pid が本当に終わったか（seconds 秒まで待つ）。見つからなければ終わったと見る。"""
    k32 = _kernel32()
    handle = k32.OpenProcess(0x00100000, False, pid)                   # SYNCHRONIZE
    if not handle:
        return True
    try:
        return k32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
    finally:
        k32.CloseHandle(handle)


def _terminate_threads(pid: int) -> None:
    """Windows：プロセス pid に残っているスレッドを止める（終わりかけで止まったプロセスを片付けるため）。"""
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(0x4, 0)                       # TH32CS_SNAPTHREAD
    if not snap or snap == ctypes.c_void_p(-1).value:
        return
    entry = _ThreadEntry32(dwSize=ctypes.sizeof(_ThreadEntry32))
    tids = []
    try:
        ok = k32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                tids.append(entry.th32ThreadID)
            ok = k32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    for tid in tids:
        handle = k32.OpenThread(0x1, False, tid)                       # THREAD_TERMINATE
        if handle:
            k32.TerminateThread(handle, 0)
            k32.CloseHandle(handle)


def format_duration(seconds: float) -> str:
    """秒を「1時間20分」の形に（分で四捨五入）。"""
    minutes = int(seconds / 60 + 0.5)
    if minutes < 1:
        return "1分未満"
    h, m = divmod(minutes, 60)
    if h == 0:
        return f"{m}分"
    return f"{h}時間{m}分" if m else f"{h}時間"


def format_grams(grams: float) -> str:
    """重さを「18g」の形に（1g で四捨五入）。"""
    return f"{int(grams + 0.5)}g"


def plate_notes(p: dict) -> list[str]:
    """プレートごとの知らせ（造形範囲の外・印刷されない上の部分・Bambu Studio の注意）。"""
    label = "・".join(p["objects"]) or f"プレート{p['plate']}"
    notes = []
    if p.get("outside"):
        notes.append(f"{label}：造形範囲の外にはみ出しています")
    top, printed = p.get("model_top_mm"), p.get("printed_top_mm")
    if top is not None and printed is not None and top - printed > HEIGHT_GAP_MM:
        notes.append(f"{label}：模型の高さは {top:.1f}mm ですが、印刷されるのは {printed:.1f}mm までです"
                     "（それより上は細すぎるなどで印刷されない形。3MF の形を確かめる）")
    for w in p.get("warnings", []):
        if w["msg"] not in QUIET_WARNINGS:
            notes.append(f"{label}：Bambu Studio の注意「{w['msg']}」（コード {w['error_code']}）")
    return notes


def summarize(sliced: dict, source_name: str, yen_per_kg=YEN_PER_KG) -> dict:
    """スライスの結果に、プレートごとの時間の文・知らせ、合計、材料費の目安を足す（JSON に書く形）。"""
    plates = sliced["plates"]
    for p in plates:
        p["time"] = format_duration(p["seconds"])
        p["notes"] = plate_notes(p)
    seconds = sum(p["seconds"] for p in plates)
    grams = round(sum(p["grams"] for p in plates), 2)
    low, high = (int(round(grams / 1000 * y, -1)) for y in yen_per_kg)
    notes = list(NOTES)
    if sliced.get("bambu_exit"):
        notes.append(sliced["bambu_exit"])
    if not any(p["support_used"] for p in plates):
        notes.append("サポートなしで計算（下に何もない所は橋渡しで印刷する見込み）。サポートを付けるなら --support で測り直す")
    return {"source": source_name, "bambu_studio": sliced.get("bambu_studio"), "bambu_command": sliced.get("bambu_command"),
            "settings": sliced["settings"], "plates": plates,
            "total": {"prints": len(plates), "seconds": seconds, "time": format_duration(seconds), "grams": grams},
            "material_cost_yen": {"low": low, "high": high, "yen_per_kg": list(yen_per_kg), "source": PRICE_SOURCE,
                                  "estimate": True},
            "notes": notes}


def report_text(s: dict) -> str:
    """人が読む日本語の結果（例「基礎　1時間20分・18g」）。"""
    st = s["settings"]
    support = f"サポートあり（{st.get('support_type')}）" if str(st.get("enable_support")) in ("1", "true") else "サポートなし"
    fuzzy = [x["name"] for p in s["plates"] for x in p.get("parts", []) if x.get("fuzzy_skin")]
    lines = [f"印刷の見込み（Bambu Studio {s.get('bambu_studio') or '?'} でスライスした見込み。推定）",
             f"3MF：{s['source']}",
             f"設定：{st.get('printer')} ／ {st.get('process')} ／ {st.get('filament')}",
             f"　　　層 {st.get('layer_height_mm')}mm・壁 {st.get('wall_loops')}周・中の詰め物 {st.get('infill')}"
             f"（{st.get('infill_pattern')}）・上 {st.get('top_layers')}層／下 {st.get('bottom_layers')}層・{support}"]
    if fuzzy:
        lines.append(f"　　　ファジースキン（3MF の部品ごとの設定）：{'・'.join(fuzzy)}")
    lines.append("部品ごと（1プレート＝1回の印刷）：")
    for p in s["plates"]:
        inside = "＋".join(x["name"] for x in p.get("parts", []))
        lines.append(f"　{'・'.join(p['objects']) or '（名前なし）'}　{p['time']}・{format_grams(p['grams'])}"
                     f"　（プレート{p['plate']}{'：' + inside if inside else ''}）")
    t, c = s["total"], s["material_cost_yen"]
    lines.append(f"合計（{t['prints']}回の印刷）：{t['time']}・{format_grams(t['grams'])}")
    lines.append(f"材料費の目安：約 {c['low']:,}〜{c['high']:,} 円（1kg あたり {c['yen_per_kg'][0]:,}〜{c['yen_per_kg'][1]:,} 円で計算。推定）")
    lines.append(f"　　　値段の出どころ：{c['source']}")
    plate_notes_all = [n for p in s["plates"] for n in p["notes"]]
    if plate_notes_all:
        lines.append("知らせ：")
        lines += [f"　- {n}" for n in plate_notes_all]
    lines.append("数字の確からしさ：")
    lines += [f"　- {n}" for n in s["notes"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="3MF を Bambu Studio のコマンドでスライスして、印刷の見込みの時間と重さを出す")
    ap.add_argument("path", type=Path, help="3MF の場所（Bambu 形式の model_bambu.3mf など）")
    ap.add_argument("--json", type=Path, help="結果を JSON でも書く場所")
    ap.add_argument("--support", action="store_true", help="サポート（ツリー・自動）を付けてスライスする")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Bambu Studio を待つ上限（秒）")
    ap.add_argument("--yen-per-kg", type=int, nargs=2, default=list(YEN_PER_KG), metavar=("LOW", "HIGH"),
                    help="材料費の目安の 1kg あたりの値段（円）")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # UTF-8 でない画面でも、出せない文字で止まらない
    if not args.path.is_file():
        print(f"3MF が見つかりません：{args.path.name}")
        return 1
    res = slice_3mf(args.path, support=args.support, timeout=args.timeout)
    if res.get("cleanup"):
        print(res["cleanup"])
    if not res["ok"]:
        print(res["reason"])
        return 1
    s = summarize(res, args.path.name, tuple(args.yen_per_kg))
    print(report_text(s))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON を書きました：{args.json.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
