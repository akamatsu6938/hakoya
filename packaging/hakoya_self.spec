# -*- mode: python ; coding: utf-8 -*-
# HAKOYA 自分用ビルド（AI の接続 3通りすべて。Claude Agent SDK＝Claude Code 本体を同梱）。
# 作り方:  .venv\Scripts\pyinstaller packaging\hakoya_self.spec --distpath dist --workpath build --noconfirm
# できるもの: dist\HAKOYA\HAKOYA.exe（フォルダごと配る）。使う人のデータは exe の隣の data\ に置く。
# ★API キーや Claude のログイン情報は入れない（SPEC §2-7）。Claude のプランは、使う人の PC の Claude Code のログインを使う。
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "app", "static"), os.path.join("app", "static")),
    (os.path.join(ROOT, "rules"), "rules"),
    (os.path.join(ROOT, "skills"), "skills"),
]
binaries = []
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
for pkg in ("manifold3d", "shapely", "trimesh", "pygltflib", "claude_agent_sdk"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "packaging", "launch_self.py")],
    pathex=[ROOT],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "scipy", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HAKOYA",
    console=True,                 # 試しのあいだは黒い画面に記録を出す
    options=[("X utf8", None, "OPTION")],   # Python の UTF-8 モード（日本語のパス・文字のため）
)
coll = COLLECT(exe, a.binaries, a.datas, name="HAKOYA")
