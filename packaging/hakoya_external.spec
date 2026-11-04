# -*- mode: python ; coding: utf-8 -*-
# HAKOYA 社外用ビルド（AI の接続は API キーと AI なしだけ。Claude Agent SDK＝Claude Code 本体は入れない）。SPEC §7-4・§10。
# 作り方:  .venv\Scripts\pyinstaller packaging\hakoya_external.spec --distpath dist\external --workpath build\external --noconfirm
# できるもの: dist\external\HAKOYA_external\HAKOYA_external.exe（フォルダごと配る）。使う人のデータは exe の隣の data\ に置く。
# 作ったら必ず: .venv\Scripts\python -m tools.check_external_build dist\external\HAKOYA_external
# ★API キーや Claude のログイン情報は入れない（SPEC §2-7）。キーは使う人が設定画面で入れ、資格情報マネージャーに保存する。
# ★claude.exe だけ消しても、SDK がその PC の claude を探して使ってしまう → SDK のパッケージごと外す（下調べ 11/3）。
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "app", "static"), os.path.join("app", "static")),
    (os.path.join(ROOT, "rules"), "rules"),
    (os.path.join(ROOT, "skills"), "skills"),
    (os.path.join(ROOT, "packaging", "EDITION_external"), "."),   # 版の印（exe の中で "EDITION" という名前にする）
]
binaries = []
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
# pdfplumber・pdfminer（PDF の日本語の文字の表 cmap を含む）・pypdfium2（pdfplumber が使う部品）：高性能読み込みの PDF の図面（11/4）
for pkg in ("manifold3d", "shapely", "trimesh", "pygltflib", "anthropic", "keyring", "ezjww",
            "pdfplumber", "pdfminer", "pypdfium2", "pypdfium2_raw"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports = [h for h in hiddenimports if not h.startswith(("claude_agent_sdk", "mcp", "anthropic.lib.tools.mcp"))]

a = Analysis(
    [os.path.join(ROOT, "packaging", "launch_self.py")],
    pathex=[ROOT],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "scipy", "pytest", "IPython", "claude_agent_sdk", "mcp"],
    noarchive=False,
)
# 版の印は exe の中で "EDITION" にする（core/settings_store.py が読む）
a.datas = [(("EDITION" if dest.endswith("EDITION_external") else dest), src, kind) for dest, src, kind in a.datas]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HAKOYA_external",
    console=True,
    options=[("X utf8", None, "OPTION")],
)
coll = COLLECT(exe, a.binaries, a.datas, name="HAKOYA_external")
