"""使う人の設定（秘密でないもの）：DATA_DIR/settings.json。API キーはここに入れない（資格情報マネージャー。core/secret_store.py）。

- auto_inspect：組み立てたあとの検品。claude_code（Claude、自分のプラン）／api_key（Claude、API キー）／
  off（コードの検査だけ、AI なし）／none（しない）。
  既定は off（勝手にプランや API の利用枠を使わない。それでも検品の欄は動く）。SPEC §12「組み立てると自動で検品が始まる」。
- 社外用の exe（HAKOYA_EDITION=external）ではプランの接続（claude_code）を出さない（規約。SPEC §7-4）。
- api_key を選ぶときは、費用の上限を見せて1回だけ確かめる（api_cost_ack）。
- company：「読み込んで組み立てる」で使う会社ルール（rules/<会社>.yaml）。既定は jisha（★11/4 B工務店。SPEC §5）。
  rules/jisha.yaml がない（公開の版）ときは、core.rules.default_company()（B工務店（架空））。
- view：画面の見え方（★11/5）。sample＝見本確認用（審査・動画用。見本の家だけ・手順の帯）／own＝自社利用用（今までどおり全部）／
  other＝他社利用用（AI は API キーと AI なしだけ・会社ルールを選んでいなければ B工務店）。社外用の exe では own は選べない。

ファイルには、使う人が選んだ値をそのまま残す（load() で見え方・版に合わせて直した値は書き戻さない。見え方を行き来しても、選んだ値が消えない）。
"""
from __future__ import annotations

import json
import os
from importlib.util import find_spec

import yaml

from core import paths

DEFAULTS = {"version": 1, "auto_inspect": "off", "api_cost_ack": False, "company": "jisha", "company_chosen": False,
            "view": "own"}
AUTO_CHOICES = ("claude_code", "api_key", "off", "none")
VIEW_CHOICES = ("sample", "own", "other")
OTHER_DEFAULT_COMPANY = "bkoumuten"      # 他社利用用で、会社ルールをまだ選んでいないとき（B工務店（架空））
SAMPLE_ALIASES = ("デモ用",)             # 見本確認用で出す物件：この仮名か、「見本」で始まる仮名


def settings_path():
    return paths.DATA_DIR / "settings.json"


def _stored() -> dict:
    """ファイルに書いてある値（なければ既定）。見え方・版に合わせて直す前のもの。"""
    p = settings_path()
    data = dict(DEFAULTS)
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in DEFAULTS if k in raw})
    except (OSError, ValueError):
        pass                                          # 壊れていたら既定で動く
    return data


def load() -> dict:
    data = _stored()
    if data["view"] not in available_views():
        data["view"] = default_view()
    if data["auto_inspect"] not in _connections(data["view"]):
        data["auto_inspect"] = DEFAULTS["auto_inspect"]
    codes = {c["code"] for c in companies()}
    if data["view"] == "other" and not data["company_chosen"] and OTHER_DEFAULT_COMPANY in codes:
        data["company"] = OTHER_DEFAULT_COMPANY        # 他社利用用：まだ選んでいなければ B工務店（架空）
    if data["company"] not in codes:
        from core.rules import default_company
        data["company"] = default_company()            # 消した会社ルールが書いてあったら既定に戻す（jisha がなければ B工務店（架空））
    return data


def save(data: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: data[k] for k in DEFAULTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


EDITION_FILE = "EDITION"        # 社外用の exe に同梱する印（中身は "external"）。packaging/hakoya_external.spec


def edition() -> str:
    """版：self（自分用。AI の接続3つ）／external（社外用。API キーと AI なしだけ）。
    exe の中に印のファイルがあれば、それを使う（環境変数で自分用に戻せないように）。開発中は HAKOYA_EDITION。"""
    mark = paths.REPO_ROOT / EDITION_FILE
    if mark.exists():
        return mark.read_text(encoding="utf-8").strip() or "external"
    return os.environ.get("HAKOYA_EDITION", "self")


def check_connection(connection: str) -> None:
    """この版・見え方で使えない接続なら、日本語の ValueError（社外用では Claude のプランの接続は使えない。SPEC §7-4）。"""
    if connection == "claude_code" and "claude_code" not in available_connections():
        if edition() == "external":
            raise ValueError("この版では Claude（自分のプラン）の接続は使えません。API キーか、AI なしを選んでください")
        raise ValueError("いまの見え方（他社利用用）では Claude（自分のプラン）の接続は使えません。API キーか、AI なしを選んでください")


def _connections(view: str) -> list[str]:
    out = []
    if edition() != "external" and view != "other" and find_spec("claude_agent_sdk") is not None:
        out.append("claude_code")
    return out + ["api_key", "off", "none"]


def available_connections() -> list[str]:
    """自動の検品で選べるつなぎ方。社外用の版と他社利用用の見え方では、プランの接続を出さない。"""
    view = _stored()["view"]
    return _connections(view if view in available_views() else default_view())


def available_views() -> list[str]:
    """選べる見え方。社外用の exe では自社利用用（own）を出さない。"""
    return ["sample", "other"] if edition() == "external" else list(VIEW_CHOICES)


def default_view() -> str:
    return "other" if edition() == "external" else "own"


def set_view(view: str) -> dict:
    if view not in VIEW_CHOICES:
        raise ValueError("見え方は 見本確認用（sample）・自社利用用（own）・他社利用用（other）のどれかです")
    if view not in available_views():
        raise ValueError("この版では、自社利用用の見え方は選べません")
    data = _stored()
    data["view"] = view
    save(data)
    return load()


def is_sample_alias(alias: str) -> bool:
    """見本確認用で一覧に出す物件か（仮名が「デモ用」か、「見本」で始まる）。"""
    return alias in SAMPLE_ALIASES or alias.startswith("見本")


def companies() -> list[dict]:
    """選べる会社ルール：rules/ の yaml のうち会社の決まりのもの（AI の設定 ai_models.yaml は除く）。[{"code", "name"}]"""
    from core.rules import RULES_DIR
    out = []
    for p in sorted(RULES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and isinstance(data.get("walls"), dict):
            out.append({"code": p.stem, "name": str(data.get("name_ja") or p.stem)})
    return out


def set_company(code: str) -> dict:
    if code not in {c["code"] for c in companies()}:
        raise ValueError("その会社ルールはありません")
    data = _stored()
    data["company"] = code
    data["company_chosen"] = True
    save(data)
    return load()


def set_auto_inspect(value: str, cost_ack: bool = False) -> dict:
    if value not in AUTO_CHOICES:
        raise ValueError("組み立てたあとの検品は claude_code・api_key・off・none のどれかです")
    if value not in available_connections():
        if edition() == "external":
            raise ValueError("この版では、その検品のしかたは選べません（社外用の版では Claude のプランの接続は使えません）")
        raise ValueError("いまの見え方（他社利用用）では、その検品のしかたは選べません（Claude のプランの接続は使えません）")
    data = _stored()
    if value == "api_key" and not (cost_ack or data["api_cost_ack"]):
        raise ValueError("API キーで自動の検品をするには、費用の確認が要ります")
    data["auto_inspect"] = value
    if value == "api_key":
        data["api_cost_ack"] = True
    save(data)
    return load()
