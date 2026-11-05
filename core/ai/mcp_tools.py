"""社外の会社の Claude Code から HAKOYA を動かす道具（MCP の中身）。入り口は app/mcp_server.py。

- 他社用の HAKOYA を使う会社の人が、自分の Claude Code（自分の Claude のプラン）をつなぐ。
  **HAKOYA は Claude を呼ばない。道具を渡すだけ**（Claude Code は、その会社の人の認証とプランで動く。SPEC §7-4）。
- 調べる道具は、アプリの Claude と同じ道具（core/ai/tools.py の TOOL_SPECS・call_tool）を包む。呼ぶたびに家を読み直す。
- 守ること：
  - alias（物件の仮名）は、登録してある物件（core.paths.aliases）だけ。ほかの名前（フォルダをたどる名前など）は断る。
  - 返す文はすべて「出してはいけない言葉」（assert_clean）と、ファイルの場所らしい文字の検査を通す。画像は中身で返し、場所は返さない。
  - 思いがけないエラーは、日本語の文とエラーの種類だけを返す（str(e) には場所が入ることがある）。
  - 数値は Claude に作らせない（SPEC §2-4）。value_source="number" は、人の一文（human_text）にその数値があるときだけ。
  - 模型を変える道具（apply_override・undo_override・decide_finding・rebuild）には、Claude Code が毎回人に許可を求める印を付ける
    （_meta の anthropic/requiresUserInteraction。app/mcp_server.py）。

道具は普通の関数（fn(**引数) → dict）で、call(名前, 引数) から呼ぶ（テストもここを呼ぶ）。
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import io
import json
import logging
import os
import re
import socket
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from core import findings as fd
from core import overrides as ov
from core import paths
from core.ai import tools as app_tools
from core.ai.tools import TOOL_SPECS, ToolContext, ToolError, call_tool
from core.privacy import NG_FORMAT_MESSAGE, PrivacyError, assert_clean

log = logging.getLogger("hakoya.mcp")

SERVER_NAME = "hakoya"            # Claude Code では道具が mcp__hakoya__<名前> になる
VERSION = "1.0.0"
CONNECTION = "mcp"                # findings.json の connection（Claude Code から MCP で検品した）
PORTS = {"own": 8765, "other": 8766, "sample": 8767}    # 見え方ごとの HAKOYA の画面の口（HAKOYA_MODE）
DEFAULT_PORT = PORTS["other"]
QUESTIONS_FILE = "mcp_questions.json"    # ask_question の質問（report_findings で findings.json に入れて空にする）
IMAGES = "_images"                # 道具の結果の中で、画像（PNG の中身）を入れる所。call() が取り出す
HUMAN_ONLY = "【模型を変える道具・人の許可が要る】"

INSTRUCTIONS = (
    "HAKOYA（箱家）：Walk in home の家の形（glb）から、1/50 の住宅模型の 3D プリント用データを作るソフトの道具です。\n"
    "- 物件は仮名（alias）で指します。一覧は list_properties。\n"
    "- HAKOYA のデータのフォルダには、お客様の図面・glb が入っています。Read・Bash などで直接開かず、HAKOYA の道具だけを使ってください。\n"
    "- 数値（座標・寸法・高さ）は作らないでください。直す案は value_source で数値の出どころを渡し、数値はコードが計算します。"
    "value_source=number は、人がこの会話で書いた数値のときだけ（human_text にその人の一文をそのまま入れる）。\n"
    "- 検品の手順はプロンプト inspect（/mcp__hakoya__inspect 物件の仮名）にあります。最後に report_findings で送ると、HAKOYA の検品タブに出ます。\n"
    "- 模型を変える道具（apply_override・undo_override・decide_finding・rebuild）は、人が許可したときだけ動きます。")

NUMBER_NEEDS_HUMAN = ('value_source が "number" のときは、その数値を含む人の一文を human_text にそのまま入れてください'
                      "（この会話で人が書いた数値だけ使えます。人が数値を書いていなければ ask_question で聞いてください）")
UNEXPECTED = "思いがけないエラーで止まりました（{}）。HAKOYA の画面で同じ操作を試すか、作者に知らせてください"


@dataclass
class ToolResult:
    """道具の結果：text は JSON の文（出してはいけない言葉と場所の検査済み）、images は PNG の中身（場所は渡さない）。"""
    text: str
    images: list[bytes] = field(default_factory=list)
    is_error: bool = False

    @property
    def data(self) -> dict:
        return json.loads(self.text)


@dataclass(frozen=True)
class ToolDef:
    name: str
    title: str
    description: str
    schema: dict
    fn: Callable[..., dict]
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    ask_human: bool = False        # Claude Code が毎回、人に許可を求める（許可の決まりで「いつも許可」にしていても）


_LOCK = threading.Lock()           # 道具は1つずつ動かす（同じ物件の図を2つの道具が同時に書かないように）


# ---------------------------------------------------------------- 守り（物件の名前・出してよい文・エラー）

def _aliases() -> list[str]:
    """登録してある物件の仮名。見本用で起動しているときは、見本の家だけ（画面の物件の一覧と同じ。★11/5 審査・動画用）。"""
    try:
        names = paths.aliases(paths.LOCAL_PATHS)
    except FileNotFoundError:
        return []
    except ValueError as e:                             # YAML の書き間違い：HAKOYA の決まった文（行の番号だけ。中身は入らない）
        msg = str(e)
        raise ToolError(msg if msg.startswith("data/local_paths.yaml の") and len(msg) < 400
                        else f"data/local_paths.yaml を読めません（{type(e).__name__}）") from None
    try:
        from core import settings_store
        if settings_store.mode() == "sample":            # ★11/5 見え方は起動のモードに変わった
            names = [a for a in names if settings_store.is_sample_alias(a)]
    except Exception as e:  # noqa: BLE001  設定が読めなくても一覧は出す（見本だけに絞れないときは絞らない）
        _log_error("settings", e)
    return names


def _check_alias(alias) -> str:
    if not isinstance(alias, str) or not alias.strip():
        raise ToolError("alias（物件の仮名）を入れてください。list_properties で一覧が分かります")
    if alias in (".", "..") or any(c in alias for c in "\\/:*?\"<>|") or alias not in _aliases():
        raise ToolError("その物件はありません。list_properties で、登録してある物件の仮名を確かめてください")
    return alias


def _ctx(alias: str) -> ToolContext:
    """組み立てた家（building.json）に人の修正を当てた、道具の材料。呼ぶたびに読み直す。"""
    alias = _check_alias(alias)
    if not (paths.out_dir(alias) / "building.json").exists():
        raise ToolError("この物件はまだ組み立てていません。人に確かめてから rebuild を使うか、"
                        "HAKOYA の画面で「読み込んで組み立てる」を押してください")
    return ToolContext.load(alias)


# エラーの文のうち、場所らしい所（ドライブ名・\\ で始まる場所・/ で挟まれた名前・ファイルの拡張子）
# ★ドライブ名の前に英字があるもの（http:// など）は場所ではない。お客様のファイル（glb・jww・PDF）の名前には施主名が入ることがある
_PATHISH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\|[\\/][^\s\\/]+[\\/]|\.(?:glb|jww|jwc|pdf)\b", re.I)
_JAPANESE = re.compile("[%s-%s%s-%s]" % (chr(0x3040), chr(0x30FF), chr(0x4E00), chr(0x9FFF)))   # ひらがな・カタカナ・漢字
# 結果の JSON の文の中のファイルの場所（JSON では \ が \\ になる）：ドライブ名で始まる場所と、\\ で始まる場所（ネットワーク）
_ABS_PATH_IN_JSON = re.compile(r"(?<![A-Za-z])[A-Za-z]:(?:\\\\|/)[^\s\"]|(?:\\\\){2}[^\s\"\\]")


def _explain(e: BaseException, fallback: str, prefix: str = "") -> str:
    """HAKOYA が書いた短い日本語のエラーの文なら「prefix＋その文」、そうでなければ「fallback（エラーの種類）」。
    文に場所の文字・お客様のファイルの名前・出してはいけない言葉があれば使わない（app/server.py の _user_message と同じ考え）。"""
    raw = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
    msg = str(raw).strip().strip("'\"")
    if msg and len(msg) <= 300 and _JAPANESE.search(msg) and not _PATHISH.search(msg):
        try:
            assert_clean(msg, "エラーの文")
            return prefix + msg
        except Exception:  # noqa: BLE001  出してはいけない言葉が入っている・言葉の一覧が読めない → 文は使わない
            pass
    return f"{fallback}（{type(e).__name__}）"


def _where(e: BaseException) -> str:
    """エラーが起きた所：HAKOYA のファイルの名前と行だけ（フォルダの場所は出さない）。"""
    frames = traceback.extract_tb(e.__traceback__)[-4:]
    return " ← ".join(f"{Path(f.filename).name}:{f.lineno}" for f in reversed(frames)) or "?"


def _log_error(what: str, e: BaseException) -> None:
    """記録（stderr）には、どこで・何の種類のエラーかだけを書く（文には場所が入ることがある）。"""
    log.warning("%s でエラー：%s（%s）", what, type(e).__name__, _where(e))


def _privacy_checked(text: str) -> str | None:
    """出してよい文なら None。出してはいけない言葉か、ファイルの場所らしい文字があれば、その説明（説明には中身を入れない）。"""
    try:
        assert_clean(text, "Claude に送るデータ")
    except PrivacyError as e:
        return str(e)                                   # 言葉そのものは文に入らない（番号だけ）
    except Exception as e:  # noqa: BLE001  ng_words の書き方が違うなど：確かめられない文は出さない
        why = "出してはいけない言葉の一覧（data/local_paths.yaml）を読めないため、結果を出しませんでした"
        fixed = str(e) in (NG_FORMAT_MESSAGE,) or str(e).startswith("data/local_paths.yaml の")   # HAKOYA の決まった文
        return f"{why}：{e}" if fixed and len(str(e)) < 400 else f"{why}（{type(e).__name__}）"
    home = json.dumps(str(Path.home()), ensure_ascii=False)[1:-1]
    data_dir = json.dumps(str(paths.DATA_DIR), ensure_ascii=False)[1:-1]
    if _ABS_PATH_IN_JSON.search(text) or (len(home) > 3 and home in text) or (len(data_dir) > 3 and data_dir in text):
        return "ファイルの場所らしい文字が入っていたため、結果を出しませんでした（作者に知らせてください）"
    return None


def _ok(data: dict, images: list[bytes] | None = None) -> ToolResult:
    text = json.dumps(data, ensure_ascii=False, default=str)
    why = _privacy_checked(text)
    if why:
        return _error(why)
    return ToolResult(text=text, images=list(images or []), is_error=False)


def _error(message: str) -> ToolResult:
    text = json.dumps({"error": message}, ensure_ascii=False)
    why = _privacy_checked(text)
    if why:                                             # 説明の文（中身を入れていない決まった文）に替える
        text = json.dumps({"error": why}, ensure_ascii=False)
    return ToolResult(text=text, images=[], is_error=True)


# ---------------------------------------------------------------- 引数の確かめ（日本語で返す。JSON Schema の一部）

_TYPE_JA = {"string": "文字", "number": "数", "integer": "整数", "boolean": "true か false", "array": "並び（[…]）",
            "object": "まとまり（{…}）", "null": "null"}


def _type_ok(v, t: str) -> bool:
    if t == "string":
        return isinstance(v, str)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    if t == "null":
        return v is None
    return True


def _join(where: str, key) -> str:
    return str(key) if not where else f"{where}.{key}"


def validate(value, schema: dict, where: str = "") -> None:
    """道具の引数を、道具の形（type・enum・required・properties・additionalProperties・items・anyOf・maxLength）で確かめる。
    違えば、どこが・どう違うかを日本語の ToolError で返す（Claude が読んで直せるように）。"""
    name = where or "引数"
    if "anyOf" in schema:
        errors = []
        for sub in schema["anyOf"]:
            try:
                validate(value, sub, where)
                return
            except ToolError as e:
                errors.append((sub, str(e)))
        for sub, msg in errors:                         # まとまり（{…}）を渡したなら、まとまりの形の違いを伝える
            if isinstance(value, dict) and sub.get("type") == "object":
                raise ToolError(msg)
        kinds = [_TYPE_JA.get(s.get("type"), str(s.get("type"))) for s in schema["anyOf"]]
        raise ToolError(f"「{name}」の形が違います（{' か '.join(kinds)}にしてください）")
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in ts):
            raise ToolError(f"「{name}」の形が違います（{' か '.join(_TYPE_JA.get(x, x) for x in ts)}にしてください）")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"「{name}」は {'・'.join(map(str, schema['enum']))} のどれかです")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ToolError(f"「{name}」が長すぎます（{schema['maxLength']} 文字まで）")
    if isinstance(value, dict) and ("properties" in schema or schema.get("type") == "object"):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise ToolError(f"「{_join(where, k)}」がありません（必ず入れる項目です）")
        if schema.get("additionalProperties") is False:
            extra = [k for k in value if k not in props]
            if extra:
                raise ToolError(f"「{_join(where, extra[0])}」は使えない項目です（使えるのは {'・'.join(props) or 'なし'}）")
        for k, sub in props.items():
            if k in value:
                validate(value[k], sub, _join(where, k))
    if isinstance(value, list) and "items" in schema:
        for i, x in enumerate(value):
            validate(x, schema["items"], f"{name}[{i}]")


# ---------------------------------------------------------------- 道具の呼び出し

def call(name: str, args: dict | None) -> ToolResult:
    """道具を名前で呼ぶ。どんなエラーも、日本語の結果（is_error）にして返す（例外は外に出さない）。"""
    spec = TOOLS.get(name)
    if spec is None:
        return _error("その名前の道具はありません（道具の一覧を見てください）")
    args = dict(args or {})
    required = set(spec.schema.get("required", []))
    args = {k: v for k, v in args.items() if v is not None or k in required}   # 省いた引数に null を入れてきても省いたことにする
    try:
        validate(args, spec.schema)
        with _LOCK, contextlib.redirect_stdout(io.StringIO()):   # print は MCP のやりとりの道（stdout）に混ぜない
            data = spec.fn(**args)
    except ToolError as e:
        return _error(str(e))
    except PrivacyError as e:
        return _error(str(e))
    except Exception as e:  # noqa: BLE001  エラーの文は返さない（場所が入ることがある）。種類だけ
        _log_error(f"道具 {name}", e)
        return _error(UNEXPECTED.format(type(e).__name__))
    images = data.pop(IMAGES, []) if isinstance(data, dict) else []
    return _ok(data, images)


def _run(ctx: ToolContext, name: str, args: dict) -> dict:
    """アプリの Claude と同じ道具（core/ai/tools.py）を呼ぶ。道具の使い方の誤りは日本語の ToolError にする。"""
    res = call_tool(ctx, name, args)
    if "error" in res:
        raise ToolError(res["error"])
    return res


def _app(alias: str, name: str, args: dict) -> dict:
    """アプリの道具を、読み直した家で呼ぶ。"""
    return _run(_ctx(alias), name, args)


def _with_image(res: dict) -> dict:
    """結果の image_path（場所）を外し、画像の中身を付ける。"""
    out = {k: v for k, v in res.items() if k != "image_path"}
    if res.get("image_path"):
        out[IMAGES] = [Path(res["image_path"]).read_bytes()]
        out["image"] = "画像はこの結果に付けました（ファイルの場所は渡しません）"
    return out


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


def _render_all(alias: str):
    from core import render
    return render.render_all(alias)


def _fresh_images(alias: str) -> str | None:
    """面の図（inspect/）が、いまの模型より古ければ作り直す。作り直せず前の図があれば、そのことを返す。
    ★render_view は図がないときだけ作るので、修正や組み直しのあとに前の模型の図を見せてしまうため。"""
    d = paths.out_dir(alias)
    marker = d / "inspect" / "plan.json"               # 面の図を作ると必ずできる（平面はどの家にもある）
    newest = max(_mtime(d / "building.json"), _mtime(ov.overrides_path(alias)), _mtime(d / "model" / "checks.json"))
    if marker.exists() and _mtime(marker) >= newest:
        return None
    try:
        _render_all(alias)
        return None
    except Exception as e:  # noqa: BLE001  glb が見つからないなど
        _log_error("面の図", e)
        if marker.exists():
            return f"面の図を新しい模型で作り直せませんでした（{_explain(e, 'エラー')}）。前の模型の図のままかもしれません"
        raise ToolError(_explain(e, "面の図を作れませんでした", prefix="面の図を作れませんでした：")) from None


def _write_outputs(alias: str) -> dict:
    """模型を組み立てて書き出す（3MF・STL・GLB・checks.json）。テストでは差し替える。"""
    from core.export import write_outputs
    return write_outputs(alias)


def _check_lines(summary: dict) -> list[str]:
    msgs = [f"[{c['level']}] {c['message']}" for c in summary.get("checks", []) if c.get("level") != "情報"]
    return msgs[:20] or ["コードの検査で、問題は見つかっていません"]


def _rebuild_or_undo(alias: str, override_id: str | None) -> dict:
    """組み直す。組み立てられなければ、足したばかりの修正を取り消して元の模型に戻す
    （app/server.py の _rebuild_or_undo と同じ。壊れた修正が残って画面が開けなくならないように）。"""
    try:
        return _write_outputs(alias)
    except Exception as e:  # noqa: BLE001  形の計算などの失敗。種類だけ伝える
        _log_error("組み直し", e)
        if not override_id:
            raise ToolError(f"模型を組み立て直せませんでした（{type(e).__name__}）") from None
        ov.undo(alias, override_id)
        fd.revert_override(alias, override_id)
        try:
            _write_outputs(alias)
        except Exception as e2:  # noqa: BLE001
            _log_error("元に戻した模型の書き出し", e2)
        raise ToolError(f"この値では模型を組み立てられませんでした。修正 {override_id} は取り消しました（{type(e).__name__}）") from None


def hakoya_port() -> int:
    """HAKOYA の画面の口の番号：HAKOYA_PORT → 起動のモード（HAKOYA_MODE：own・other・sample）→ 他社用の口。"""
    v = os.environ.get("HAKOYA_PORT", "").strip()
    if v.isdigit() and 0 < int(v) < 65536:
        return int(v)
    return PORTS.get(os.environ.get("HAKOYA_MODE", "").strip().lower(), DEFAULT_PORT)


def hakoya_url(alias: str) -> str:
    return f"http://127.0.0.1:{hakoya_port()}/?alias={quote(alias, safe='')}"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, data, what: str) -> None:
    """出してはいけない言葉を調べてから、一時のファイルに書いて置き換える（書きかけのファイルを画面が読まないように）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    assert_clean(text, what)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(5):                                  # 画面のサーバーが読んでいる最中だと、Windows では置き換えが断られる
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.1)


def _outputs_stale(alias: str) -> bool:
    d = paths.out_dir(alias)
    checks = d / "model" / "checks.json"
    newest = max(_mtime(d / "building.json"), _mtime(ov.overrides_path(alias)), _mtime(d / "print_split.json"))
    return not checks.exists() or _mtime(checks) < newest


# ---------------------------------------------------------------- 調べる道具

def list_properties() -> dict:
    out = []
    for a in _aliases():
        d = paths.out_dir(a)
        item: dict[str, Any] = {"alias": a, "built": (d / "building.json").exists()}
        try:
            f = paths.property_files(a, paths.LOCAL_PATHS, use_mode=False)
            item["has_glb"] = bool(f.glb and f.glb.exists())
        except Exception:  # noqa: BLE001  登録の書き方が違う物件も一覧には出す
            item["has_glb"] = False
        if item["built"]:
            try:
                b, _ = ov.apply(json.loads((d / "building.json").read_text(encoding="utf-8")), ov.load(a))
                item["counts"] = {"levels": len(b.get("levels", [])), "walls": len(b.get("walls", [])),
                                  "openings": len(b.get("openings", [])), "roofs": len(b.get("roofs", []))}
                item["overrides"] = len(ov.load(a))
            except Exception as e:  # noqa: BLE001
                _log_error("物件の一覧", e)
                item["counts"] = None
        try:
            rep = fd.load(a) if item["built"] else None
        except Exception as e:  # noqa: BLE001  壊れた findings.json でも一覧は出す
            _log_error("物件の一覧", e)
            rep = None
        if rep:
            all_f = rep.get("findings", []) + rep.get("code_findings", [])
            item["findings"] = {"connection": rep.get("connection"), "at": rep.get("at"),
                                "claude": len(rep.get("findings", [])), "code": len(rep.get("code_findings", [])),
                                "open": len([f for f in all_f if f.get("status") not in ("承認", "却下")])}
        out.append(item)
    note = "登録してある物件がありません。HAKOYA の画面の「設定」→「物件の登録」で登録してください" if not out else \
        "built が false の物件は、まだ組み立てていません（rebuild か、HAKOYA の画面の「読み込んで組み立てる」）"
    return {"properties": out, "note": note}


def get_building(alias: str) -> dict:
    return _app(alias, "get_building", {})


def get_elements(alias: str, **args) -> dict:
    ctx = _ctx(alias)
    note = _fresh_images(ctx.alias) if args.get("view") else None     # 面を渡したときは、その面の図の部材の範囲を使う
    res = _run(ctx, "get_elements", args)
    if note:
        res["note"] = note
    return res


def render_view(alias: str, **args) -> dict:
    ctx = _ctx(alias)
    note = _fresh_images(ctx.alias)
    out = _with_image(_run(ctx, "render_view", args))
    if note:
        out["note"] = note
    return out


def get_drawing_region(alias: str, **args) -> dict:
    ctx = _ctx(alias)
    note = _fresh_images(ctx.alias)
    out = _with_image(_run(ctx, "get_drawing_region", args))
    if note:
        out["images_note"] = note
    return out


def run_checks(alias: str) -> dict:
    res = _app(alias, "run_checks", {})
    if _outputs_stale(alias):
        res["note"] = "模型の書き出しが、いまの家や修正より古いかもしれません（export で書き出し直せます）"
    return res


def get_code_findings(alias: str) -> dict:
    _ctx(alias)
    from core.findings import code_findings
    items = code_findings(alias)
    return {"count": len(items), "code_findings": items,
            "note": "コードの検査から作った指摘（Claude を使わない）。同じ指摘を重ねて出さないでください。"
                    "report_findings を送ると、この一覧も HAKOYA の検品タブに入ります（番号は C1…）"}


def get_findings(alias: str) -> dict:
    alias = _check_alias(alias)
    rep = fd.load(alias)
    if rep is None:
        return {"findings": [], "code_findings": [], "questions": [],
                "note": "まだ検品の結果がありません（検品して report_findings で送るか、HAKOYA の画面で検品してください）"}
    keep = ("alias", "connection", "summary", "at", "findings", "code_findings", "questions")
    out = {k: rep.get(k) for k in keep if k in rep}
    fp, bp = fd.findings_path(alias), paths.out_dir(alias) / "building.json"
    out["stale"] = bool(fp.exists() and bp.exists() and _mtime(bp) > _mtime(fp))   # 検品のあとに組み立て直した
    out["note"] = "status が 未確認・あとで の指摘は decide_finding で決められます（人に確かめてから）"
    return out


def list_overrides(alias: str) -> dict:
    ctx = _ctx(alias)
    items = ov.load(alias)
    _, results = ov.apply(ctx.raw, items)
    st = {r.get("id"): r for r in results}
    keys = ("id", "target", "field", "from", "to", "value_source", "reason", "origin", "at")
    rows = [dict({k: x.get(k) for k in keys}, status=st.get(x.get("id"), {}).get("status"),
                 **({"status_reason": st[x["id"]]["reason"]} if st.get(x.get("id"), {}).get("reason") else {}))
            for x in items]
    return {"count": len(rows), "overrides": rows,
            "note": "status：applied（当たっている）・applied_changed（自動の値が前回から変わっていた）・failed（当てられない）。"
                    "取り消すときは undo_override（人に確かめてから）"}


def open_in_hakoya(alias: str) -> dict:
    alias = _check_alias(alias)
    port = hakoya_port()
    running = _port_open(port)
    return {"url": hakoya_url(alias), "hakoya_running": running,
            "note": ("このアドレスをブラウザで開くと、HAKOYA の画面がこの物件を選んだ状態で開きます。"
                     "開いているときは、画面を読み込み直すと新しい模型と指摘が出ます") if running else
                    "HAKOYA の画面が起動していないようです。HAKOYA を起動してから、このアドレスを開いてください"}


# ---------------------------------------------------------------- 直す案と質問（保存しない／質問だけ残す）

def _number_source(ctx, value_source: str, number, human_text: str | None) -> tuple[str, float | None]:
    """数値の出どころの確かめ。number（人が書いた数値）は、human_text（人の一文）にその数値があるときだけ使う。"""
    if value_source.startswith("human:number:"):          # propose_override が返す形をそのまま渡したとき
        try:
            number = float(value_source.split(":")[-1])
        except ValueError:
            raise ToolError('value_source「human:number:…」の数値が読めません。value_source="number" と number で渡してください') from None
        value_source = "number"
    if value_source == "number":
        if not (human_text or "").strip():
            raise ToolError(NUMBER_NEEDS_HUMAN)
        if number is None:
            raise ToolError('value_source が "number" のときは、人が書いた数値を number に入れてください')
        if not app_tools._number_in_text(float(number), human_text):
            raise ToolError(f"number（{float(number):g}）は human_text（人の一文）にない数値です。"
                            "number には人が書いた数値だけを使い、なければ ask_question で人に聞いてください")
        ctx.human_text = human_text
    return value_source, number


def _proposal_args(target, field, value_source, reason, number, finding) -> dict:
    a = {"target": target, "field": field, "value_source": value_source, "reason": reason}
    if number is not None:
        a["number"] = number
    if finding is not None:
        a["finding"] = finding
    return a


def propose_override(alias: str, target: str, field: str, value_source: str, reason: str, number: float | None = None,
                     human_text: str | None = None, finding: str | None = None) -> dict:
    ctx = _ctx(alias)
    value_source, number = _number_source(ctx, value_source, number, human_text)
    res = call_tool(ctx, "propose_override", _proposal_args(target, field, value_source, reason, number, finding))
    if "error" in res:
        raise ToolError(res["error"])
    res["note"] = ("これは案です（まだ保存していません）。人が承認したら apply_override に同じ引数を渡すと、修正として記録して模型を組み直します。"
                   "検品の指摘に付けるときは、report_findings の proposal に target・field・value_source・reason（number のときは number と human_text）を書きます")
    return res


def _questions_path(alias: str) -> Path:
    return paths.out_dir(alias) / QUESTIONS_FILE


def _load_questions(alias: str) -> list[dict]:
    p = _questions_path(alias)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


class _Questions:
    """app_tools.ask_question に渡す入れ物（質問の並びだけを使う）。"""

    def __init__(self, questions: list[dict]):
        self.questions = questions


def ask_question(alias: str, question: str, options: list[str], evidence: str = "") -> dict:
    alias = _check_alias(alias)
    holder = _Questions(list(_load_questions(alias)))
    res = app_tools.ask_question(holder, question, options, evidence)     # 選択肢は2〜3個（だめなら ToolError）
    q = holder.questions[-1]
    q["at"] = _now()
    _write_json_atomic(_questions_path(alias), holder.questions, "質問")
    return {"queued": res["queued"], "pending_questions": len(holder.questions),
            "note": "この会話で人に聞いて、答えをもらってください。質問は report_findings を送ると、HAKOYA の検品タブにも出ます"}


# ---------------------------------------------------------------- 検品の結果を送る

def _finding_item_schema() -> dict:
    """指摘1件の形：アプリの検品と同じ（core/ai/agent.py の FINDINGS_SCHEMA）。title（見出し）・region（図の範囲）は省いてよい。
    直す案の number を使うときだけ human_text（人の一文）を足す。"""
    from core.ai.agent import FINDINGS_SCHEMA
    item = copy.deepcopy(FINDINGS_SCHEMA["properties"]["findings"]["items"])
    props = item["properties"]
    props.setdefault("title", {"type": "string", "description": "指摘の見出し（20字くらい）"})
    item["required"] = [r for r in item.get("required", []) if r != "title"]     # 省いたら本文から作る（report_findings）
    props.setdefault("region", {"description": "図の上の範囲（get_elements の bbox_px）。省いてよい",
                                "anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "number"}},
                                          {"type": "object"}]})
    for branch in props.get("proposal", {}).get("anyOf", []):
        if branch.get("type") == "object":
            branch.setdefault("properties", {})["human_text"] = {
                "type": "string", "description": "value_source が number のときだけ：その数値を含む、人の一文をそのまま"}
    return item


def _known_ids(b: dict) -> set[str]:
    ids = {x.get("id") for coll in ("walls", "openings", "roofs", "rooms", "porches", "porch_soffits") for x in b.get(coll, [])}
    return {i for i in ids if i}


class _HumanText:
    """_number_source に渡す入れ物（指摘ごとの人の一文を、ほかの指摘の確かめに使わないように）。"""
    human_text: str | None = None


def _check_proposal(ctx: ToolContext, f: dict) -> str | None:
    """指摘の直す案を1件だけ確かめる。思いがけないエラー（図面が読めない等）なら理由を返す（ほかの指摘は送れるように）。
    道具の使い方の誤り（ToolError）は、finalize が proposal_error にするので、ここでは通す。"""
    p = f["proposal"]
    try:
        app_tools.propose_override(ctx, p["target"], p["field"], p["value_source"], p.get("reason") or f.get("message", ""),
                                   number=p.get("number"))
    except ToolError:
        return None
    except Exception as e:  # noqa: BLE001
        _log_error("直す案", e)
        return f"直す案を計算できませんでした（{type(e).__name__}）"
    return None


def report_findings(alias: str, findings: list[dict], summary: str) -> dict:
    from core.ai.agent import finalize
    from core.findings import code_findings
    ctx = _ctx(alias)
    allowed = set(_finding_item_schema()["properties"])
    raw, dropped, ignored = [], [], set()
    for i, f in enumerate(findings):
        ignored |= set(f) - allowed                        # 決まった項目だけ残す（番号・状態はコードが付ける）
        f = {k: copy.deepcopy(v) for k, v in f.items() if k in allowed}
        p = f.get("proposal")
        if p:
            vs = str(p.get("value_source", ""))
            if vs == "number" or vs.startswith("human:number:"):
                try:
                    p["value_source"], p["number"] = _number_source(_HumanText(), vs, p.get("number"), p.get("human_text"))
                except ToolError as e:                     # 人の数値でないものは、直す案にしない（指摘は残す）
                    f["proposal"], f["proposal_error"] = None, str(e)
                    dropped.append(i + 1)
            if f.get("proposal"):
                f["proposal"].pop("human_text", None)
                why = _check_proposal(ctx, f)
                if why:
                    f["proposal"], f["proposal_error"] = None, why
        if not str(f.get("title") or "").strip():          # 見出しを省いたら、本文の最初の文から作る（アプリと同じ）
            from core.findings import title_of
            f["title"] = title_of(f)
        raw.append(f)
    ctx.proposals.clear()
    items = finalize(ctx, raw)                              # 番号（F1…）・未確認・直す案の数値をコードで計算し直す
    known = _known_ids(ctx.building)
    unknown = sorted({t.split(":")[0] for f in items for t in f.get("targets", []) if t.split(":")[0] not in known}
                     | {str(t).split(":")[0] for f in items for t in f.get("unknown_targets", [])})   # ★11/5 finalize が外した番号も
    questions = _load_questions(alias)
    report = {"alias": alias, "connection": CONNECTION, "summary": summary, "findings": items,
              "questions": questions, "code_findings": code_findings(alias), "at": _now(),
              "cost_usd": None, "note": "社外の Claude Code（その会社の人のプラン）から MCP で送られた検品"}
    _write_json_atomic(fd.findings_path(alias), report, "検品の指摘（findings.json）")
    if questions:
        _questions_path(alias).unlink(missing_ok=True)      # 送った質問は findings.json に入った
    out = {"saved": True, "findings": len(items), "with_proposal": len([f for f in items if f.get("proposal")]),
           "proposal_errors": [{"id": f["id"], "reason": f["proposal_error"]} for f in items if f.get("proposal_error")],
           "code_findings": len(report["code_findings"]), "questions": len(questions),
           "url": hakoya_url(alias),
           "note": "HAKOYA の検品タブに出ます（画面を開いていれば読み込み直してください）。前の指摘の一覧は置き換わりました"}
    if unknown:
        out["unknown_targets"] = unknown
        out["note"] += "。targets にない部材の番号があります（get_elements で確かめてください）"
    if ignored:
        out["ignored_keys"] = sorted(ignored)
        out["note"] += "。決まった項目にないもの（ignored_keys）は入れていません"
    if dropped:
        out["note"] += f"。{'・'.join(map(str, dropped))} 件目の直す案は、人の数値の確かめが通らなかったので外しました"
    return out


# ---------------------------------------------------------------- 模型を変える道具（人の許可が要る）

def apply_override(alias: str, target: str, field: str, value_source: str, reason: str, number: float | None = None,
                   human_text: str | None = None, finding: str | None = None) -> dict:
    ctx = _ctx(alias)
    value_source, number = _number_source(ctx, value_source, number, human_text)
    res = call_tool(ctx, "propose_override", _proposal_args(target, field, value_source, reason, number, finding))
    if "error" in res:
        raise ToolError(res["error"])
    p = res["proposal"]                                    # 数値は、いまの家でコードが出どころから計算し直した値
    try:
        item = ov.add(ctx.alias, ctx.raw, p["target"], p["field"], p.get("to"), p["value_source"],
                      p.get("reason") or "Claude Code（MCP）で直した", origin=CONNECTION)
    except (KeyError, ValueError) as e:
        raise ToolError(_explain(e, "この修正は記録できませんでした")) from None
    summary = _rebuild_or_undo(ctx.alias, item["id"])
    return {"override": item, "checks": _check_lines(summary), "url": hakoya_url(ctx.alias),
            "note": f"修正 {item['id']} を記録して、模型を組み直しました。取り消すときは undo_override（{item['id']}）。"
                    "HAKOYA の画面を開いていれば、読み込み直すと新しい模型が出ます"}


def undo_override(alias: str, override_id: str) -> dict:
    ctx = _ctx(alias)
    if not re.fullmatch(r"OV\d{1,6}", override_id or ""):
        raise ToolError("override_id は OV1 のような修正の番号です（list_overrides で分かります）")
    try:
        item = ov.undo(ctx.alias, override_id)
    except KeyError as e:
        raise ToolError(_explain(e, "その修正はありません")) from None
    fd.revert_override(ctx.alias, override_id)             # この修正を生んだ指摘は「未確認」に戻す
    try:
        summary = _write_outputs(ctx.alias)
    except Exception as e:  # noqa: BLE001
        _log_error("取り消しのあとの組み直し", e)
        raise ToolError(f"修正 {override_id} は取り消しましたが、模型を組み立て直せませんでした（{type(e).__name__}）。"
                        "HAKOYA の画面で「読み込んで組み立てる」を押してください") from None
    return {"undone": item, "checks": _check_lines(summary), "url": hakoya_url(ctx.alias),
            "note": f"修正 {override_id} を取り消して、模型を組み直しました"}


ACTIONS = {"approve": "approve", "reject": "reject", "later": "later", "承認": "approve", "却下": "reject", "あとで": "later"}


def decide_finding(alias: str, finding_id: str, action: str) -> dict:
    ctx = _ctx(alias)
    act = ACTIONS.get(action)
    if act is None:
        raise ToolError("action は approve（承認）・reject（却下）・later（あとで）のどれかです")
    report = fd.load(ctx.alias)
    if report is None:
        raise ToolError("まだ検品の結果がありません（検品して report_findings で送るか、HAKOYA の画面で検品してください）")
    f = next((x for x in report.get("findings", []) + report.get("code_findings", []) if x.get("id") == finding_id), None)
    if f is None:
        raise ToolError("その番号の指摘はありません（get_findings で番号を確かめてください）")
    if act == "approve" and f.get("status") == "承認" and f.get("override") and \
            any(x.get("id") == f["override"] for x in ov.load(ctx.alias)):
        raise ToolError(f"{finding_id} はもう承認して、修正 {f['override']} になっています（取り消すときは undo_override）")
    try:
        f = fd.decide(ctx.alias, finding_id, act)
    except (KeyError, ValueError) as e:
        raise ToolError(_explain(e, "決められませんでした")) from None
    out = {"finding": {k: f.get(k) for k in ("id", "title", "status", "message", "override", "proposal") if k in f}}
    if act == "approve":
        summary = _rebuild_or_undo(ctx.alias, f.get("override"))
        out["checks"] = _check_lines(summary)
        out["note"] = f"{finding_id} を承認して、修正 {f.get('override')} として記録し、模型を組み直しました"
    else:
        out["note"] = f"{finding_id} を「{f.get('status')}」にしました（模型は変わりません）"
    out["url"] = hakoya_url(ctx.alias)
    return out


def rebuild(alias: str) -> dict:
    alias = _check_alias(alias)
    from adapters.walkinhome import build, write_building
    from core import settings_store
    company = settings_store.load()["company"]             # 画面の「読み込んで組み立てる」と同じ会社ルール
    try:
        write_building(alias, build(alias, company))
    except (ValueError, OSError, KeyError) as e:           # HAKOYA が書いた説明（「3階建てはまだ扱えません」など）は伝える
        _log_error("組み立て", e)
        raise ToolError(_explain(e, "組み立てられませんでした", prefix="組み立てられませんでした：")) from None
    summary = _rebuild_or_undo(alias, None)
    note = "面の図も新しい模型で作り直しました"
    try:
        _render_all(alias)
    except Exception as e:  # noqa: BLE001  図は render_view のときにもう一度作る
        _log_error("面の図", e)
        note = f"面の図は作り直せませんでした（{type(e).__name__}）。render_view のときにもう一度試します"
    return {"rebuilt": True, "company_rules": company,
            "parts": [p.get("label") for p in summary.get("parts", {}).values()],
            "overrides": [{"id": r.get("id"), "status": r.get("status")} for r in summary.get("overrides", [])],
            "checks": _check_lines(summary), "images": note, "url": hakoya_url(alias),
            "note": "glb から作り直し、人の修正を当て直しました。当てられなかった修正は overrides の status が failed です"}


FILE_NOTES = {"model.3mf": "3MF（Bambu Studio などで開く）", "model_bambu.3mf": "Bambu Studio 形式の 3MF（外壁のざらざらの設定つき）",
              "model.glb": "画面の 3D 表示用の GLB", "checks.json": "コードの検査の結果"}


def export(alias: str) -> dict:
    ctx = _ctx(alias)
    rewritten = _outputs_stale(ctx.alias)
    if rewritten:
        try:
            _write_outputs(ctx.alias)
        except Exception as e:  # noqa: BLE001
            _log_error("書き出し", e)
            raise ToolError(f"書き出せませんでした（{type(e).__name__}）") from None
    model = paths.out_dir(ctx.alias) / "model"
    names = sorted(p.name for p in model.iterdir() if p.is_file()) if model.is_dir() else []
    files = [{"name": f"model/{n}", "what": FILE_NOTES.get(n, "部品ごとの STL" if n.endswith(".stl") else "")} for n in names]
    return {"rewritten": rewritten, "files": files, "url": hakoya_url(ctx.alias),
            "where": "HAKOYA のデータのフォルダの中の、この物件の model フォルダ",
            "note": "ファイルは HAKOYA の画面の「書き出し」タブから保存できます（場所はここでは渡しません）。"
                    "3MF をそのまま Bambu Studio で開けば印刷の準備ができます"}


# ---------------------------------------------------------------- 道具の一覧（説明は Claude Code が読む）

ALIAS = {"type": "string", "description": "物件の仮名（list_properties で分かる。例：物件A）"}
HUMAN_TEXT = {"type": "string", "description": "value_source が number のときだけ必須：その数値を含む、人がこの会話で書いた一文をそのまま"
                                               "（例：「袖壁を900にしたい」）。コードが、数値がこの文にあるか確かめる"}


def _with_alias(schema: dict, extra: dict | None = None) -> dict:
    s = copy.deepcopy(schema)
    s["properties"] = {"alias": ALIAS, **s.get("properties", {}), **(extra or {})}
    s["required"] = ["alias", *[r for r in s.get("required", []) if r != "alias"]]
    s["additionalProperties"] = False
    return s


def _spec(name: str) -> dict:
    return TOOL_SPECS[name]


PROPOSAL_EXTRA = {"human_text": HUMAN_TEXT}
EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}


def _tools() -> dict[str, ToolDef]:
    items = [
        ToolDef("list_properties", "物件の一覧",
                "登録してある物件（仮名）の一覧。組み立て済みか（built）・glb があるか・部材の数・検品の指摘の数・修正の数を返す。"
                "ファイルの場所は返さない。", EMPTY, list_properties, read_only=True),
        ToolDef("get_building", "家の要約", _spec("get_building")["description"],
                _with_alias(_spec("get_building")["schema"]), get_building, read_only=True),
        ToolDef("get_elements", "部材の一覧", _spec("get_elements")["description"],
                _with_alias(_spec("get_elements")["schema"]), get_elements, read_only=True),
        ToolDef("render_view", "面の図", _spec("render_view")["description"] +
                "画像はこの結果に付けて返す（ファイルの場所は返さない）。模型が変わっていれば、図を作り直してから返す。",
                _with_alias(_spec("render_view")["schema"]), render_view, read_only=True),
        ToolDef("get_drawing_region", "図面の範囲", _spec("get_drawing_region")["description"] +
                "図面は高性能読み込みが入の物件だけ（ないときは available=false）。画像はこの結果に付けて返す。",
                _with_alias(_spec("get_drawing_region")["schema"]), get_drawing_region, read_only=True),
        ToolDef("run_checks", "コードの検査", _spec("run_checks")["description"],
                _with_alias(_spec("run_checks")["schema"]), run_checks, read_only=True),
        ToolDef("get_code_findings", "コードの指摘",
                "コードの検査から作った指摘（glb の例外・未解決の所・会社ルールといつもと違う所・印刷の警告など。Claude を使わない）。"
                "検品では、これと同じ指摘を重ねて出さない。", _with_alias(EMPTY), get_code_findings, read_only=True),
        ToolDef("get_findings", "いまの指摘",
                "HAKOYA の検品タブに出ている指摘（findings.json）：Claude の指摘（F1…）・コードの指摘（C1…）・質問と、"
                "それぞれの状態（未確認・承認・却下・あとで）と直す案。decide_finding の番号はここで確かめる。",
                _with_alias(EMPTY), get_findings, read_only=True),
        ToolDef("list_overrides", "修正の一覧",
                "人が承認した修正（overrides.json）の一覧と、いまの模型に当たっているか。undo_override の番号（OV1…）はここで確かめる。",
                _with_alias(EMPTY), list_overrides, read_only=True),
        ToolDef("propose_override", "直す案を試す", _spec("propose_override")["description"] +
                "試すだけで保存しない。value_source=number は、人がこの会話で書いた数値のときだけ使い、"
                "human_text にその数値を含む人の一文をそのまま入れる（ないと断る）。",
                _with_alias(_spec("propose_override")["schema"], PROPOSAL_EXTRA), propose_override, read_only=True),
        ToolDef("ask_question", "人に聞く", _spec("ask_question")["description"] +
                "この会話で人に聞いて答えをもらう。質問は report_findings を送ると HAKOYA の検品タブにも出る。",
                _with_alias(_spec("ask_question")["schema"]), ask_question),
        ToolDef("report_findings", "検品の結果を送る",
                "検品の結果を HAKOYA に送る（findings.json に書く。HAKOYA の検品タブに出る）。findings は1件ずつ "
                "view・kind・severity・message・targets・evidence・confidence（必ず）と、title（20字くらいの見出し）・proposal・question。"
                "proposal は数値を書かず value_source で（数値はコードが計算し直す。number のときは human_text も）。"
                "コードの指摘（get_code_findings）と ask_question の質問は自動で足す。前の指摘の一覧は置き換わる。",
                {"type": "object", "properties": {
                    "alias": ALIAS,
                    "findings": {"type": "array", "items": _finding_item_schema()},
                    "summary": {"type": "string", "description": "検品のまとめ（日本語で1〜3文）"}},
                 "required": ["alias", "findings", "summary"], "additionalProperties": False},
                report_findings, destructive=True),
        ToolDef("apply_override", "修正する", HUMAN_ONLY +
                "直す案を修正として記録し、模型を組み直す（HAKOYA の画面の承認と同じ）。引数は propose_override と同じで、"
                "数値はコードが value_source から計算し直す。組み立てられない値なら、自動で取り消して元に戻す。取り消しは undo_override。",
                _with_alias(_spec("propose_override")["schema"], PROPOSAL_EXTRA), apply_override,
                destructive=True, ask_human=True),
        ToolDef("undo_override", "修正を取り消す", HUMAN_ONLY +
                "修正を1件取り消して、模型を組み直す。その修正を生んだ指摘は「未確認」に戻る。番号は list_overrides で。",
                _with_alias({"type": "object", "properties": {
                    "override_id": {"type": "string", "description": "修正の番号（OV1 など）"}}, "required": ["override_id"]}),
                undo_override, destructive=True, ask_human=True),
        ToolDef("decide_finding", "指摘を決める", HUMAN_ONLY +
                "指摘を承認（approve）・却下（reject）・あとで（later）にする。承認すると直す案を修正として記録し、模型を組み直す"
                "（直す案のない指摘は承認できない）。番号は get_findings で。",
                _with_alias({"type": "object", "properties": {
                    "finding_id": {"type": "string", "description": "指摘の番号（F1・C2 など）"},
                    "action": {"type": "string", "enum": list(ACTIONS), "description": "approve＝承認・reject＝却下・later＝あとで"}},
                    "required": ["finding_id", "action"]}),
                decide_finding, destructive=True, ask_human=True),
        ToolDef("rebuild", "組み立て直す", HUMAN_ONLY +
                "glb を読み直して家の形を作り直し、人の修正を当て直して、模型と面の図を作り直す（HAKOYA の「読み込んで組み立てる」と同じ。"
                "会社ルールは HAKOYA の設定のもの）。1分ほどかかることがある。",
                _with_alias(EMPTY), rebuild, destructive=True, idempotent=True, ask_human=True),
        ToolDef("export", "書き出し",
                "書き出したファイル（3MF・STL・GLB）の一覧。模型が家や修正より古ければ書き出し直す。返すのは物件のフォルダからの名前だけ"
                "（ファイルの場所は返さない）。ファイルは HAKOYA の画面の「書き出し」タブから保存する。",
                _with_alias(EMPTY), export, idempotent=True),
        ToolDef("open_in_hakoya", "HAKOYA で開く",
                "HAKOYA の画面で、この物件を開くアドレス（http://127.0.0.1:…/?alias=…）と、HAKOYA が起動しているかを返す。",
                _with_alias(EMPTY), open_in_hakoya, read_only=True),
    ]
    return {t.name: t for t in items}


TOOLS = _tools()


# ---------------------------------------------------------------- プロンプト（Claude Code では /mcp__hakoya__inspect）

@dataclass(frozen=True)
class PromptArg:
    name: str
    description: str
    required: bool = False


@dataclass(frozen=True)
class PromptDef:
    name: str
    title: str
    description: str
    arguments: tuple[PromptArg, ...]
    build: Callable[[str | None], str]


def inspect_prompt(alias: str | None = None) -> str:
    """検品の手順（面ごとの6人の検品担当と同じ見方。Skill の文章はアプリの Claude と同じもの）。"""
    from core.ai.agent import FACES, MULTI_PLAN_HOW, face_label
    from core.ai.api_runner import FACE_ROLE, skill_text
    from core.rules import default_company
    known = bool(alias) and alias in _aliases()
    multi, company = False, None
    if known and (paths.out_dir(alias) / "building.json").exists():
        try:
            b = json.loads((paths.out_dir(alias) / "building.json").read_text(encoding="utf-8"))
            multi = len(b.get("levels", [])) > 1
            company = (b.get("source") or {}).get("company_rules")
        except (OSError, ValueError):
            pass
    if not company:
        try:
            from core import settings_store
            company = settings_store.load()["company"]
        except Exception:  # noqa: BLE001
            company = default_company()
    if known:
        head = f"物件「{alias}」を検品してください。道具を呼ぶときは、いつも alias=\"{alias}\" を付けます。"
    else:
        head = ("まず list_properties で物件の一覧を見て、どの物件を検品するか人に確かめてください。"
                + ("（指定された物件は登録されていませんでした）" if alias else "")
                + "道具を呼ぶときは、いつもその仮名を alias に付けます。")
    faces = "\n".join(f"   - {f}：{face_label(f, multi)}" for f in FACES)
    plan_how = f"\n   2階建てなので、plan の担当は：{MULTI_PLAN_HOW}" if multi else ""
    # 前の検品で人が答えたこと（HAKOYA の検品タブで答えたもの。同じ質問をもう一度しないため。★11/5 夜）
    from core.answers import prompt_section
    answers = prompt_section(alias) if known else ""
    answers = f"\n{answers}\n" if answers else ""
    return f"""{FACE_ROLE}
ここでは、検品担当とまとめ役の両方をあなたが一人で行います。HAKOYA のデータのフォルダ（お客様の図面・glb）を Read・Bash などで直接開かず、HAKOYA の道具（mcp__hakoya__…）だけを使ってください。

## 物件
{head}
{answers}
## 手順
1. get_building で家の要約を読む。run_checks（コードの検査）と get_code_findings（コードの指摘）も読む。built でない物件は、人に確かめてから rebuild。
2. 6つの面を順に見る：
{faces}
   面ごとに render_view（source=model）と render_view（source=glb）を両方見比べ、get_elements（view=その面）で部材の番号と図の上の範囲を確かめる。気になる部材は render_view の highlight で赤い枠を付けて確かめる。図面が要る判断は get_drawing_region（なければ glb と模型の見比べで判断し、図面が要ることは ask_question で人に聞く）。{plan_how}
   print は、run_checks の結果と各面の模型の図（source=model）で、印刷と組み立ての問題を探す。
   （面ごとに Claude Code のサブエージェントへ分けてもよい。そのときも HAKOYA の道具だけを使わせ、alias を伝える。）
3. 指摘にするのは、根拠（どの面の図・どの部材の番号・どの会社ルールの項目か）がある食い違いだけ。同じ食い違いを何件も出さない。コードの指摘と同じものは出さない。Skill に「指摘しない」とあるもの（サッシ・樋・家具など、模型で省くもの）は出さない。
4. 直す案は propose_override で試す。数値は書かず、数値の出どころ value_source（majority:種類 / rule:項目 / glb / jww:図面:番号）を渡して、コードに計算させる。value_source=number は、人がこの会話で書いた数値のときだけ使い、human_text にその数値を含む人の一文をそのまま入れる。決まらないことは ask_question（選択肢2〜3個・根拠つき）で人に聞く。
5. 最後に report_findings を1回呼ぶ：findings（1件ずつ view・kind・severity・message・targets・evidence・confidence と、title＝20字くらいの見出し・proposal・question）と summary（日本語で1〜3文）。重要度（高・中・低）の高いものから。HAKOYA の検品タブに出ます（前の指摘の一覧は置き換わります）。
6. 直すかどうかは人が決めます。人に頼まれたときだけ decide_finding（承認・却下・あとで）か apply_override を使います（どちらも Claude Code が人に許可を求めます）。直したら open_in_hakoya のアドレスを人に伝えます。

## 決まり（Skill：検品の決まり・模型のルール・会社のルール）
{skill_text(company)}
"""


PROMPTS = {"inspect": PromptDef(
    "inspect", "物件を検品する",
    "HAKOYA の道具で、物件の模型を6つの面（南・北・東・西・平面・印刷）で見比べて検品し、指摘を HAKOYA に送る手順。",
    (PromptArg("alias", "物件の仮名（list_properties で分かる。例：物件A）"),), inspect_prompt)}


def get_prompt(name: str, args: dict | None) -> str | None:
    """プロンプトの文（なければ None）。出してはいけない言葉が入っていれば PrivacyError（言葉は文に入らない）。"""
    p = PROMPTS.get(name)
    if p is None:
        return None
    alias = (args or {}).get("alias")
    with _LOCK, contextlib.redirect_stdout(io.StringIO()):
        text = p.build(alias.strip() if isinstance(alias, str) and alias.strip() else None)
    assert_clean(text, "プロンプト")
    return text
