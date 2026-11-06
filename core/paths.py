"""実データの場所（data/local_paths.yaml）の読み込みと、作業用コピーの用意。

元のファイルには書き込まない（SPEC §2-1）。glb は data/cache/<物件>/source.glb にコピーしてから読む。
出力の中では元のファイル名（施主名が入っている）を使わず、「<物件>.glb」と書く。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 同梱物（rules/・skills/・app/static/）の場所。exe のときは PyInstaller が展開した場所（sys._MEIPASS）
REPO_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------- 起動のしかた（★11/5：3つを別々に起動する。画面では切り替えない）
# own＝自社用（start.bat・HAKOYA.exe）：全部の機能。データは data/
# other＝他社用（start_other.bat・HAKOYA_external.exe）：AI は API キーと AI なしだけ。データは data/other/（最初は空）
# sample＝見本用（start_sample.bat・HAKOYA.exe --mode sample）：審査・動画用。見本の家だけ。データは data/sample/
# 決め方：HAKOYA_MODE（own・other・sample）。なければ、社外用の版は other、ほかは own（★11/6 作者：公開リポジトリでも
# start.bat は自社用。会社ルールは見本の B工務店で、会社ルールのタブ（学習）も使える。前は自社の会社ルールがない版は other だった）。
# 社外用の版は、いつも other（HAKOYA_MODE でも変えられない）。データの置き場所は HAKOYA_DATA_DIR で変えられる
MODES = ("own", "other", "sample")
MODE_FOLDERS = {"own": "", "other": "other", "sample": "sample"}     # 自社用の data/ の中のフォルダ（own は data/ そのもの）
EDITION_FILE = "EDITION"        # 社外用の exe に同梱する印（中身は "external"）。packaging/hakoya_external.spec


def edition() -> str:
    """版：self（自分用。AI の接続3つ）／external（社外用。API キーと AI なしだけ）。
    exe の中に印のファイルがあれば、それを使う（環境変数で自分用に戻せないように）。開発中は HAKOYA_EDITION。
    起動のしかたを決めるのに使うので、ここに置く（core/settings_store.edition はこれを呼ぶ）。"""
    mark = REPO_ROOT / EDITION_FILE
    if mark.exists():
        return mark.read_text(encoding="utf-8").strip() or "external"
    return os.environ.get("HAKOYA_EDITION", "self")


def default_mode() -> str:
    """HAKOYA_MODE がないときの起動のしかた。社外用の版は他社用、ほかは自社用（提出用の公開リポジトリも。★11/6）。"""
    return "other" if edition() == "external" else "own"


def launch_mode() -> str:
    """起動のしかた（own・other・sample）。社外用の版はいつも other。知らない値は日本語の ValueError（取り違えて
    自社のデータを開かないように、黙って既定にはしない）。"""
    if edition() == "external":
        return "other"
    m = (os.environ.get("HAKOYA_MODE") or "").strip().lower()
    if not m:
        return default_mode()
    if m not in MODES:
        raise ValueError("HAKOYA_MODE は own（自社用）・other（他社用）・sample（見本用）のどれかにしてください")
    return m


def own_data_dir() -> Path:
    """自社用のデータの場所。exe のときは exe の隣の data/（exe の中だと書き出しが消えるため）、開発中はリポジトリの data/。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return REPO_ROOT / "data"


def mode_data_dir(mode: str) -> Path:
    """起動のしかたごとのデータの場所（local_paths.yaml・書き出し・設定）。HAKOYA_DATA_DIR があれば、それ。"""
    if os.environ.get("HAKOYA_DATA_DIR"):
        return Path(os.environ["HAKOYA_DATA_DIR"])
    sub = MODE_FOLDERS[mode]
    return own_data_dir() / sub if sub else own_data_dir()


MODE = launch_mode()
DATA_DIR = mode_data_dir(MODE)
LOCAL_PATHS = DATA_DIR / "local_paths.yaml"


@dataclass
class PropertyFiles:
    alias: str                      # 仮名（例：物件A）
    glb: Path | None
    jww_folder: Path | None
    jww: dict[str, Path] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)   # 場所を読んで分かったこと（元のファイル名は書かない）

    def __repr__(self) -> str:
        """場所（元のファイル名に施主名が入る）は出さない。★11/5：テストが失敗したとき、pytest がこの値をそのまま表示して、
        物件B の glb のファイル名（施主名入り）が画面に出た。種類と数だけにする。"""
        return (f"PropertyFiles(alias={self.alias!r}, glb={'あり' if self.glb else 'なし'}, "
                f"jww_folder={'あり' if self.jww_folder else 'なし'}, jww={sorted(self.jww)}, notes={len(self.notes)}件)")

    __str__ = __repr__

    @property
    def glb_label(self) -> str:
        """出力に書いてよい glb の名前（元のファイル名は使わない）。"""
        return f"{self.alias}.glb"


# 図面の種類を、ファイル名の言葉から推定する（local_paths.yaml に jww: の指定がないとき）
JWW_KIND_WORDS = {"section": ("断面",), "elevation": ("立面",), "plan": ("平面",)}
JWW_KIND_JA = {"plan": "平面図（1階）", "plan_2f": "平面図（2階）", "elevation": "立面図", "section": "断面図"}
# 名前は全角・半角をそろえ（NFKC）、英字を大文字にしてから見る
UPPER_FLOOR_WORDS = ("2階", "2F", "二階")
LOWER_FLOOR_WORDS = ("1階", "1F", "一階")
BOTH_FLOORS = re.compile(r"[1一]\s*[・,、.\-_~〜ー\s]\s*[2二]\s*[階F]")   # 「1・2階」「1,2F」「1.2階」など
NOT_PLAN_WORDS = ("詳細", "小屋裏", "ロフト", "屋根", "基礎", "配置", "伏", "天井")   # 平面詳細図・基礎平面図などは照合に使わない


def _resolve_glb(p: Path) -> tuple[Path | None, str | None]:
    """glb の場所。フォルダが書いてあれば、すぐ下の glb が1つのときだけそれを使う（取り違えないように）。"""
    if not p.is_dir():
        return p, None
    try:
        found = sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() == ".glb")
    except OSError as e:     # エラーの文にはフォルダの場所（施主名が入ることがある）が入るので、種類だけ出す
        return None, f"glb のフォルダを読めません（{type(e).__name__}。権限やネットワークの切断を確かめてください）"
    if len(found) == 1:
        return found[0], None
    if not found:
        return None, "glb のフォルダに、glb がまだありません"
    return None, f"glb のフォルダに glb が {len(found)} つあります（1つにしてください）"


def jww_kind(stem: str) -> str | None:
    """ファイル名（拡張子なし）から図面の種類。種類の言葉がない・2つ以上あるときは None。
    ★11/4 の見直し：平面詳細図・小屋裏平面図・基礎平面図が「1階の平面図」と重なって、本物の平面図まで使えなくなっていた。
    「兼」の後ろ（2階平面図兼1階屋根伏図 の「1階屋根伏図」）と「2階建」は、種類・階の言葉として見ない。"""
    s = unicodedata.normalize("NFKC", stem).upper().split("兼")[0]
    s = re.sub(r"[0-9一二三]\s*階建", "", s)
    hits = [k for k, words in JWW_KIND_WORDS.items() if any(w in s for w in words)]
    if len(hits) != 1:
        return None
    if hits[0] == "plan" and any(w in s for w in NOT_PLAN_WORDS):
        return None
    both = any(w in s for w in LOWER_FLOOR_WORDS) or BOTH_FLOORS.search(s)
    if hits[0] == "plan" and any(w in s for w in UPPER_FLOOR_WORDS) and not both:
        return "plan_2f"          # 「1・2階平面図」のように両方の階が書いてあるものは、1階の平面図として使う
    return hits[0]


def detect_jww(folder: Path, with_pdf: bool = True) -> tuple[dict[str, Path], list[str]]:
    """図面のフォルダのすぐ下の .jww を、ファイル名から種類に分ける。同じ種類が2つ以上あれば使わない（取り違えないように）。
    with_pdf：jww がない種類は、同じ決まりで .pdf（CAD から書き出した図面）も使う（★11/4 高性能読み込み。jww があれば jww）。"""
    if not folder.is_dir():
        return {}, ["図面のフォルダが見つかりません"]
    try:
        entries = sorted(folder.iterdir())
    except OSError as e:     # 場所はエラーの文に出さない
        return {}, [f"図面のフォルダを読めません（{type(e).__name__}。権限やネットワークの切断を確かめてください）"]
    out, notes = {}, []
    for ext in ((".jww", ".pdf") if with_pdf else (".jww",)):
        found: dict[str, list[Path]] = {}
        for p in entries:
            if p.is_file() and p.suffix.lower() == ext and (kind := jww_kind(p.stem)) and kind not in out:
                found.setdefault(kind, []).append(p)
        for kind, ps in found.items():
            if len(ps) == 1:
                out[kind] = ps[0]
            else:
                notes.append(f"{JWW_KIND_JA[kind]}らしい図面（{ext}）が {len(ps)} つあるので使いません（local_paths.yaml の jww: で指定してください）")
    return out, notes


def load_local_paths(path: Path = LOCAL_PATHS) -> dict:
    if not path.exists():
        raise FileNotFoundError("data/local_paths.yaml がありません。「ファイル」→「新しく読み込む」で物件を登録してください。")
    return parse_local_paths(path.read_text(encoding="utf-8"))


def parse_local_paths(text: str) -> dict:
    """local_paths.yaml の中身を読む。★11/4 の見直し：YAML の書き間違いのエラー文には、その行がそのまま入る
    （施主名の入った場所が黒い画面に出る）ので、行の番号だけを出す。"""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        where = f"{mark.line + 1} 行目あたり" if mark else "どこか"
        raise ValueError(f"data/local_paths.yaml の {where}が YAML として読めません。よくある原因：①「- 言葉」の「-」のあとの空白が全角になっている"
                         "（半角の空白にする）②場所を \"…\" で囲んでいる（\\ がそのまま書けないので、'…' で囲むか、囲まずに書く）") from None
    if not isinstance(data, dict):
        raise ValueError("data/local_paths.yaml の形が違います（「物件A:」のように仮名で始まる書き方にしてください）")
    return data


def aliases(path: Path = LOCAL_PATHS) -> list[str]:
    """登録してある物件の仮名（ng_words などの設定は除く）。"""
    return [k for k, v in load_local_paths(path).items() if isinstance(v, dict)]


def property_files(alias: str, path: Path = LOCAL_PATHS, use_mode: bool = True) -> PropertyFiles:
    """物件のファイルの場所。glb にはファイルかフォルダを書ける。図面は jww: の指定がなければ、フォルダの中から名前で探す。
    use_mode：高性能読み込み（core/reading.py）が切の物件は、図面を空にする（図面を使う所はすべて「図面なし」の動きになる）。"""
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
    if use_mode and jww:
        from core.reading import is_high
        if not is_high(alias, has_drawings=True):
            jww = {}
            notes.append("高性能読み込みが切なので、図面は使わない（glb だけ）")
    return PropertyFiles(alias=alias, glb=glb, jww_folder=folder, jww=jww, notes=notes)


def cache_dir(alias: str) -> Path:
    return DATA_DIR / "cache" / alias


def out_dir(alias: str) -> Path:
    return DATA_DIR / "out" / alias


REPLACE_TRIES = 20          # 入れ替えのやり直しの回数（Windows では、ほかのプロセスが読んでいる最中のファイルは入れ替えられない）


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """同じフォルダの一時ファイルに書いてから入れ替える（途中で止まっても、読む側が書きかけのファイルを読まない）。
    ★11/5：MCP の道具（別のプロセス）と画面のサーバーが同じ物件のファイルを読み書きするため。
    Windows では読んでいる最中のファイルを入れ替えられない（PermissionError）ので、少し待って何回かやり直す。"""
    import tempfile
    import time
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        for i in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_text_atomic(path: Path, text: str) -> None:
    write_bytes_atomic(path, text.encode("utf-8"))


# ---------------------------------------------------------------- 見本用のデータ（★11/5）
DEMO_ALIAS = "デモ用"        # 見本用に写す、組み立て済みの見本の家（自社用のデータから。ほかの物件は写さない）
# ★11/6 作者：公開リポジトリでも、見本用は最初から見本の家が入っている（組み立て＋検品の結果まで）。
# 同梱の見本（house.glb と、組み立て済みの out/ の中身）。名前は ASCII（日本語のファイル名を避ける）。公開用の書き出しが足す
SAMPLE_BUNDLE = REPO_ROOT / "samples" / "sample_house"
SAMPLE_ALIAS = "見本の家"     # 同梱の見本を写すときの仮名（動画に出てくる家）


def _copy_sample_bundle(bundle: Path, dst: Path) -> str:
    """同梱の見本を data/sample/ に写す。glb も写し（exe の展開先は起動のたびに変わることがあるため）、local_paths.yaml には
    写した glb の場所を書く（local_paths.yaml を最後に書く：途中で止まったら次の起動で写し直す）。"""
    glb = dst / "glb" / "house.glb"
    out = dst / "out" / SAMPLE_ALIAS
    if out.exists():                            # 前に途中で止まった写し
        shutil.rmtree(out)
    glb.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundle / "house.glb", glb)
    shutil.copytree(bundle / "out", out)
    entry = {"glb": str(glb.resolve())}
    text = ("# 見本用（start_sample.bat）の物件。HAKOYA が同梱の見本の家（samples/sample_house）を写した\n"
            + yaml.safe_dump({SAMPLE_ALIAS: entry}, allow_unicode=True, sort_keys=False))
    if parse_local_paths(text).get(SAMPLE_ALIAS) != entry:      # 書く前に、読み直して同じになるか確かめる
        raise ValueError("見本用の local_paths.yaml を作れませんでした")
    write_text_atomic(dst / "local_paths.yaml", text)
    return "copied"


def ensure_sample_data(src: Path | None = None, dst: Path | None = None, bundle: Path | None = None) -> str:
    """見本用のデータ（data/sample/）を用意する。dst に local_paths.yaml がなければ写す。
    - 同梱の見本（bundle。既定は samples/sample_house）があれば、それを「見本の家」として写す（公開リポジトリ・★11/6）。
    - なければ、自社用のデータ（src）の組み立て済みの「デモ用」の out/デモ用/ を丸ごと写し、local_paths.yaml には「デモ用」だけを
      書く（glb・図面の場所はそのまま同じ。出してはいけない言葉も写す：見本用でも、画面に送る前の見張りが同じ言葉で止めるように）。
    返り値："copied"（写した）・"ready"（もう用意してある）・"missing"（写すものがない。何もしない。画面が案内を出す）。
    場所は表示しない・記録に書かない。"""
    src = Path(src) if src is not None else own_data_dir()
    dst = Path(dst) if dst is not None else DATA_DIR
    bundle = Path(bundle) if bundle is not None else SAMPLE_BUNDLE
    target = dst / "local_paths.yaml"
    if target.exists():
        return "ready"
    if src.resolve() == dst.resolve():          # 自社用のデータそのものには書かない
        return "missing"
    if (bundle / "house.glb").is_file() and (bundle / "out" / "building.json").is_file():
        return _copy_sample_bundle(bundle, dst)
    conf_path = src / "local_paths.yaml"
    if not conf_path.exists():
        return "missing"
    conf = load_local_paths(conf_path)
    entry = conf.get(DEMO_ALIAS)
    built = src / "out" / DEMO_ALIAS
    if not isinstance(entry, dict) or not entry.get("glb") or not (built / "building.json").is_file():
        return "missing"
    keep = {"ng_words": conf["ng_words"]} if conf.get("ng_words") is not None else {}
    text = ("# 見本用（start_sample.bat）の物件。HAKOYA が自社用の local_paths.yaml から「デモ用」だけを写した\n"
            + yaml.safe_dump({**keep, DEMO_ALIAS: entry}, allow_unicode=True, sort_keys=False))
    back = parse_local_paths(text)              # 書く前に、読み直して同じになるか確かめる
    if [k for k, v in back.items() if isinstance(v, dict)] != [DEMO_ALIAS] or back[DEMO_ALIAS] != entry:
        raise ValueError("見本用の local_paths.yaml を作れませんでした（自社用の local_paths.yaml の「デモ用」の書き方を確かめてください）")
    out = dst / "out" / DEMO_ALIAS
    if out.exists():                            # 前に途中で止まった写し（local_paths.yaml は最後に書くので、ないときは写し直す）
        shutil.rmtree(out)
    shutil.copytree(built, out)
    write_text_atomic(target, text)
    return "copied"


def glb_working_copy(files: PropertyFiles) -> Path:
    """元の glb を data/cache/<物件>/source.glb にコピーして、そのパスを返す。
    元が変わっていなければ（大きさと更新日時が同じなら）コピーし直さない。"""
    if files.glb is None or not files.glb.exists():
        why = files.notes[0] if files.glb is None and files.notes else "local_paths.yaml に書かれた場所にありません"
        raise FileNotFoundError(f"{files.alias} の glb：{why}。")
    dst = cache_dir(files.alias) / "source.glb"
    try:
        src_stat = files.glb.stat()
        if dst.exists():
            d = dst.stat()
            if d.st_size == src_stat.st_size and int(d.st_mtime) == int(src_stat.st_mtime):
                return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files.glb, dst)  # 元は読むだけ
    except OSError as e:     # エラーの文には元の場所（施主名が入ることがある）が入るので、種類だけ出す
        raise OSError(f"{files.alias} の glb を作業用に写せませんでした（{type(e).__name__}。場所・権限・空き容量を確かめてください）") from None
    return dst
