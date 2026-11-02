"""実データの場所（data/local_paths.yaml）の読み込みと、作業用コピーの用意。

元のファイルには書き込まない（SPEC §2-1）。glb は data/cache/<物件>/source.glb にコピーしてから読む。
出力の中では元のファイル名（施主名が入っている）を使わず、「<物件>.glb」と書く。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
LOCAL_PATHS = DATA_DIR / "local_paths.yaml"


@dataclass
class PropertyFiles:
    alias: str                      # 仮名（例：物件A）
    glb: Path | None
    jww_folder: Path | None
    jww: dict[str, Path] = field(default_factory=dict)

    @property
    def glb_label(self) -> str:
        """出力に書いてよい glb の名前（元のファイル名は使わない）。"""
        return f"{self.alias}.glb"


def load_local_paths(path: Path = LOCAL_PATHS) -> dict:
    if not path.exists():
        raise FileNotFoundError("data/local_paths.yaml がありません。README の手順5を見てください。")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def property_files(alias: str, path: Path = LOCAL_PATHS) -> PropertyFiles:
    conf = load_local_paths(path).get(alias)
    if not isinstance(conf, dict):
        raise KeyError(f"data/local_paths.yaml に「{alias}」がありません。")
    glb = Path(conf["glb"]) if conf.get("glb") else None
    folder = Path(conf["jww_folder"]) if conf.get("jww_folder") else None
    jww = {k: folder / v for k, v in (conf.get("jww") or {}).items()} if folder else {}
    return PropertyFiles(alias=alias, glb=glb, jww_folder=folder, jww=jww)


def cache_dir(alias: str) -> Path:
    return DATA_DIR / "cache" / alias


def out_dir(alias: str) -> Path:
    return DATA_DIR / "out" / alias


def glb_working_copy(files: PropertyFiles) -> Path:
    """元の glb を data/cache/<物件>/source.glb にコピーして、そのパスを返す。
    元が変わっていなければ（大きさと更新日時が同じなら）コピーし直さない。"""
    if files.glb is None or not files.glb.exists():
        raise FileNotFoundError(f"{files.alias} の glb が、local_paths.yaml に書かれた場所にありません。")
    dst = cache_dir(files.alias) / "source.glb"
    src_stat = files.glb.stat()
    if dst.exists():
        d = dst.stat()
        if d.st_size == src_stat.st_size and int(d.st_mtime) == int(src_stat.st_mtime):
            return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(files.glb, dst)  # 元は読むだけ
    return dst
