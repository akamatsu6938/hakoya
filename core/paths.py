"""実データの場所（data/local_paths.yaml）の読み込みと、作業用コピーの用意。

元のファイルには書き込まない（SPEC §2-1）。glb は data/cache/<物件>/source.glb にコピーしてから読む。
出力の中では元のファイル名（施主名が入っている）を使わず、「<物件>.glb」と書く。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 同梱物（rules/・skills/・app/static/）の場所。exe のときは PyInstaller が展開した場所（sys._MEIPASS）
REPO_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
# 使う人のデータ（local_paths.yaml・書き出し）の場所。exe のときは exe の隣の data/（exe の中だと書き出しが消えるため）。
# HAKOYA_DATA_DIR で変えられる
if os.environ.get("HAKOYA_DATA_DIR"):
    DATA_DIR = Path(os.environ["HAKOYA_DATA_DIR"])
elif getattr(sys, "frozen", False):
    DATA_DIR = Path(sys.executable).resolve().parent / "data"
else:
    DATA_DIR = REPO_ROOT / "data"
LOCAL_PATHS = DATA_DIR / "local_paths.yaml"


@dataclass
class PropertyFiles:
    alias: str                      # 仮名（例：物件A）
    glb: Path | None
    jww_folder: Path | None
    jww: dict[str, Path] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)   # 場所を読んで分かったこと（元のファイル名は書かない）

    @property
    def glb_label(self) -> str:
        """出力に書いてよい glb の名前（元のファイル名は使わない）。"""
        return f"{self.alias}.glb"


# 図面の種類を、ファイル名の言葉から推定する（local_paths.yaml に jww: の指定がないとき）
JWW_KIND_WORDS = {"section": ("断面",), "elevation": ("立面",), "plan": ("平面",)}
JWW_KIND_JA = {"plan": "平面図（1階）", "plan_2f": "平面図（2階）", "elevation": "立面図", "section": "断面図"}
UPPER_FLOOR_WORDS = ("2階", "２階", "2F", "２F", "2Ｆ", "２Ｆ")
LOWER_FLOOR_WORDS = ("1階", "１階", "1F", "１F", "1Ｆ", "１Ｆ")
BOTH_FLOORS = re.compile(r"[1１]\s*[・,、，\-－~〜～]\s*[2２]\s*[階FＦ]")   # 「1・2階」「1,2F」など


def _resolve_glb(p: Path) -> tuple[Path | None, str | None]:
    """glb の場所。フォルダが書いてあれば、すぐ下の glb が1つのときだけそれを使う（取り違えないように）。"""
    if not p.is_dir():
        return p, None
    found = sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() == ".glb")
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, "glb のフォルダに、glb がまだありません"
    return None, f"glb のフォルダに glb が {len(found)} つあります（1つにしてください）"


def jww_kind(stem: str) -> str | None:
    """ファイル名（拡張子なし）から図面の種類。種類の言葉がない・2つ以上あるときは None。"""
    hits = [k for k, words in JWW_KIND_WORDS.items() if any(w in stem for w in words)]
    if len(hits) != 1:
        return None
    both = any(w in stem for w in LOWER_FLOOR_WORDS) or BOTH_FLOORS.search(stem)
    if hits[0] == "plan" and any(w in stem for w in UPPER_FLOOR_WORDS) and not both:
        return "plan_2f"          # 「1・2階平面図」のように両方の階が書いてあるものは、1階の平面図として使う
    return hits[0]


def detect_jww(folder: Path) -> tuple[dict[str, Path], list[str]]:
    """図面のフォルダのすぐ下の .jww を、ファイル名から種類に分ける。同じ種類が2つ以上あれば使わない（取り違えないように）。"""
    if not folder.is_dir():
        return {}, ["図面のフォルダが見つかりません"]
    found: dict[str, list[Path]] = {}
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() == ".jww" and (kind := jww_kind(p.stem)):
            found.setdefault(kind, []).append(p)
    out, notes = {}, []
    for kind, ps in found.items():
        if len(ps) == 1:
            out[kind] = ps[0]
        else:
            notes.append(f"{JWW_KIND_JA[kind]}らしい図面が {len(ps)} つあるので使いません（local_paths.yaml の jww: で指定してください）")
    return out, notes


def load_local_paths(path: Path = LOCAL_PATHS) -> dict:
    if not path.exists():
        raise FileNotFoundError("data/local_paths.yaml がありません。README の手順5を見てください。")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def aliases(path: Path = LOCAL_PATHS) -> list[str]:
    """登録してある物件の仮名（ng_words などの設定は除く）。"""
    return [k for k, v in load_local_paths(path).items() if isinstance(v, dict)]


def property_files(alias: str, path: Path = LOCAL_PATHS) -> PropertyFiles:
    """物件のファイルの場所。glb にはファイルかフォルダを書ける。図面は jww: の指定がなければ、フォルダの中から名前で探す。"""
    conf = load_local_paths(path).get(alias)
    if not isinstance(conf, dict):
        raise KeyError(f"data/local_paths.yaml に「{alias}」がありません。")
    notes: list[str] = []
    glb = None
    if conf.get("glb"):
        glb, why = _resolve_glb(Path(conf["glb"]))
        if why:
            notes.append(why)
    folder = Path(conf["jww_folder"]) if conf.get("jww_folder") else None
    if folder and conf.get("jww"):
        jww = {k: folder / v for k, v in conf["jww"].items()}
    elif folder:
        jww, more = detect_jww(folder)
        notes += more
    else:
        jww = {}
    return PropertyFiles(alias=alias, glb=glb, jww_folder=folder, jww=jww, notes=notes)


def cache_dir(alias: str) -> Path:
    return DATA_DIR / "cache" / alias


def out_dir(alias: str) -> Path:
    return DATA_DIR / "out" / alias


def glb_working_copy(files: PropertyFiles) -> Path:
    """元の glb を data/cache/<物件>/source.glb にコピーして、そのパスを返す。
    元が変わっていなければ（大きさと更新日時が同じなら）コピーし直さない。"""
    if files.glb is None or not files.glb.exists():
        why = files.notes[0] if files.glb is None and files.notes else "local_paths.yaml に書かれた場所にありません"
        raise FileNotFoundError(f"{files.alias} の glb：{why}。")
    dst = cache_dir(files.alias) / "source.glb"
    src_stat = files.glb.stat()
    if dst.exists():
        d = dst.stat()
        if d.st_size == src_stat.st_size and int(d.st_mtime) == int(src_stat.st_mtime):
            return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(files.glb, dst)  # 元は読むだけ
    return dst
