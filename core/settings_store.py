"""使う人の設定（秘密でないもの）：DATA_DIR/settings.json。API キーはここに入れない（資格情報マネージャー。core/secret_store.py）。

- auto_inspect：組み立てたあとの検品。claude_code（Claude、自分のプラン）／api_key（Claude、API キー）／
  off（コードの検査だけ、AI なし）／none（しない）。
  既定は off（勝手にプランや API の利用枠を使わない。それでも検品の欄は動く）。SPEC §12「組み立てると自動で検品が始まる」。
- 社外用の exe（HAKOYA_EDITION=external）ではプランの接続（claude_code）を出さない（規約。SPEC §7-4）。
- api_key を選ぶときは、費用の上限を見せて1回だけ確かめる（api_cost_ack）。
- company：「読み込んで組み立てる」で使う会社ルール（rules/<会社>.yaml）。既定は jisha（★11/4 B工務店。SPEC §5）。
  rules/jisha.yaml がない（公開の版）ときは、core.rules.default_company()（B工務店（架空））。
"""
from __future__ import annotations

import json
import os
from importlib.util import find_spec

import yaml

from core import paths

DEFAULTS = {"version": 1, "auto_inspect": "off", "api_cost_ack": False, "company": "jisha"}
AUTO_CHOICES = ("claude_code", "api_key", "off", "none")


def settings_path():
    return paths.DATA_DIR / "settings.json"


def load() -> dict:
    p = settings_path()
    data = dict(DEFAULTS)
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in DEFAULTS if k in raw})
    except (OSError, ValueError):
        pass                                          # 壊れていたら既定で動く
    if data["auto_inspect"] not in available_connections():
        data["auto_inspect"] = DEFAULTS["auto_inspect"]
    if data["company"] not in {c["code"] for c in companies()}:
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
    """この版で使えない接続なら、日本語の ValueError（社外用では Claude のプランの接続は使えない。SPEC §7-4）。"""
    if connection == "claude_code" and "claude_code" not in available_connections():
        raise ValueError("この版では Claude（自分のプラン）の接続は使えません。API キーか、AI なしを選んでください")


def available_connections() -> list[str]:
    """自動の検品で選べるつなぎ方。社外用ではプランの接続を出さない。"""
    out = []
    if edition() != "external" and find_spec("claude_agent_sdk") is not None:
        out.append("claude_code")
    return out + ["api_key", "off", "none"]


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
    data = load()
    data["company"] = code
    save(data)
    return data


def set_auto_inspect(value: str, cost_ack: bool = False) -> dict:
    if value not in AUTO_CHOICES:
        raise ValueError("組み立てたあとの検品は claude_code・api_key・off・none のどれかです")
    if value not in available_connections():
        raise ValueError("この版では、その検品のしかたは選べません（社外用の版では Claude のプランの接続は使えません）")
    data = load()
    if value == "api_key" and not (cost_ack or data["api_cost_ack"]):
        raise ValueError("API キーで自動の検品をするには、費用の確認が要ります")
    data["auto_inspect"] = value
    if value == "api_key":
        data["api_cost_ack"] = True
    save(data)
    return data
