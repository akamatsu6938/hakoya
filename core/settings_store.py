"""使う人の設定（秘密でないもの）：DATA_DIR/settings.json。API キーはここに入れない（資格情報マネージャー。core/secret_store.py）。

- auto_inspect：組み立てたあとの検品。claude_code（Claude、自分のプラン）／api_key（Claude、API キー）／
  off（コードの検査だけ、AI なし）／none（しない）。
  既定は off（勝手にプランや API の利用枠を使わない。それでも検品の欄は動く）。SPEC §12「組み立てると自動で検品が始まる」。
- 社外用の exe（HAKOYA_EDITION=external）ではプランの接続（claude_code）を出さない（規約。SPEC §7-4）。
- api_key を選ぶときは、費用の上限を見せて1回だけ確かめる（api_cost_ack）。
- company：「読み込んで組み立てる」で使う会社ルール（rules/<会社>.yaml）。既定は core.rules.DEFAULT_COMPANY（★11/4 B工務店。SPEC §5）。
  自社の会社ルールがない（公開の版）ときは、core.rules.default_company()（B工務店（架空））。
- 起動のしかた（★11/5。core/paths.py の MODE）：own＝自社用（全部）／other＝他社用（AI は API キーと AI なしだけ・
  会社ルールを選んでいなければ B工務店・会社ルールのタブなし）／sample＝見本用（審査・動画用。見本の家だけ・手順の帯・登録なし）。
  起動したときに決まり、画面では切り替えない（前の「見え方」view と POST /api/settings/view はやめた）。
  起動のしかたごとにデータの置き場所が別なので、settings.json も別。前の版が書いた view の値は読んでも使わない。
- last_alias・recent：最後に開いた物件と、最近開いた物件（新しい順に5つまで）。

ファイルには、使う人が選んだ値をそのまま残す（load() で起動のしかた・版に合わせて直した値は書き戻さない）。
"""
from __future__ import annotations

import json
import os
from importlib.util import find_spec

import yaml

from core import paths

DEFAULTS = {"version": 1, "auto_inspect": "off", "api_cost_ack": False, "company": "jisha", "company_chosen": False,
            "last_alias": None, "recent": []}
AUTO_CHOICES = ("claude_code", "api_key", "off", "none")
MODE_LABELS = {"own": "自社用", "other": "他社用", "sample": "見本用"}
OTHER_DEFAULT_COMPANY = "bkoumuten"      # 他社用で、会社ルールをまだ選んでいないとき（B工務店（架空））
SAMPLE_ALIASES = ("デモ用",)             # 見本用で出す物件：この仮名か、「見本」で始まる仮名
RECENT_MAX = 5
EDITION_FILE = paths.EDITION_FILE        # 社外用の exe に同梱する印（packaging/hakoya_external.spec）


def settings_path():
    return paths.DATA_DIR / "settings.json"


def _stored() -> dict:
    """ファイルに書いてある値（なければ既定）。起動のしかた・版に合わせて直す前のもの。"""
    p = settings_path()
    data = dict(DEFAULTS, recent=[])
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: raw[k] for k in DEFAULTS if k in raw})     # 前の版の view などは読まない
    except (OSError, ValueError):
        pass                                          # 壊れていたら既定で動く
    if not isinstance(data["last_alias"], str) or not data["last_alias"]:
        data["last_alias"] = None
    rec = data["recent"] if isinstance(data["recent"], list) else []
    data["recent"] = list(dict.fromkeys(a for a in rec if isinstance(a, str) and a))[:RECENT_MAX]
    return data


def load() -> dict:
    data = _stored()
    m = mode()
    if data["auto_inspect"] not in _connections(m):
        data["auto_inspect"] = DEFAULTS["auto_inspect"]
    codes = {c["code"] for c in companies()}
    if m == "other" and not data["company_chosen"] and OTHER_DEFAULT_COMPANY in codes:
        data["company"] = OTHER_DEFAULT_COMPANY        # 他社用：まだ選んでいなければ B工務店（架空）
    if data["company"] not in codes:
        from core.rules import default_company
        data["company"] = default_company()            # 消した会社ルールが書いてあったら既定に戻す（自社の会社ルールがなければ B工務店（架空））
    return data


def save(data: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: data[k] for k in DEFAULTS}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def edition() -> str:
    """版：self（自分用。AI の接続3つ）／external（社外用。API キーと AI なしだけ）。中身は core/paths.py の edition()。"""
    return paths.edition()


def mode() -> str:
    """起動のしかた（own・other・sample）。起動したときに決まる（core/paths.py の MODE）。社外用の版は、いつも other。"""
    return "other" if edition() == "external" else paths.MODE


def mode_label(m: str | None = None) -> str:
    return MODE_LABELS[m or mode()]


def features() -> dict:
    """起動のしかたで使える機能（画面が出し分けに使う）。import＝物件の登録、rules_tab＝会社ルールのタブ、
    plan_connection＝Claude（自分のプラン）の接続。"""
    m = mode()
    return {"import": m != "sample", "rules_tab": m != "other", "plan_connection": "claude_code" in available_connections()}


def check_connection(connection: str) -> None:
    """この版・起動のしかたで使えない接続なら、日本語の ValueError（社外用では Claude のプランの接続は使えない。SPEC §7-4）。"""
    if connection == "claude_code" and "claude_code" not in available_connections():
        if _in_other_mode():
            raise ValueError("他社用では Claude（自分のプラン）の接続は使えません。API キーか、AI なしを選んでください")
        raise ValueError("この版では Claude（自分のプラン）の接続は使えません。API キーか、AI なしを選んでください")


def _in_other_mode() -> bool:
    """プランの接続が使えない理由が「他社用で起動した」か。社外用の版や、接続の部品がないときは版の理由にする。"""
    return edition() != "external" and mode() == "other"


def _connections(m: str) -> list[str]:
    out = []
    if edition() != "external" and m != "other" and find_spec("claude_agent_sdk") is not None:
        out.append("claude_code")
    return out + ["api_key", "off", "none"]


def available_connections() -> list[str]:
    """自動の検品で選べるつなぎ方。社外用の版と他社用では、プランの接続を出さない。"""
    return _connections(mode())


def is_sample_alias(alias: str) -> bool:
    """見本用で一覧に出す物件か（仮名が「デモ用」か、「見本」で始まる）。"""
    return alias in SAMPLE_ALIASES or alias.startswith("見本")


def set_last_alias(alias: str, known: list[str] | None = None) -> dict:
    """最後に開いた物件を残し、最近開いた物件の先頭にする（5つまで）。known を渡すと、その中にない物件を最近から外す。"""
    data = _stored()
    rest = [a for a in data["recent"] if a != alias and (known is None or a in known)]
    data["last_alias"] = alias
    data["recent"] = [alias, *rest][:RECENT_MAX]
    save(data)
    return {"last_alias": alias, "recent": data["recent"]}


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
        if _in_other_mode():
            raise ValueError("他社用では、その検品のしかたは選べません（Claude のプランの接続は使えません）")
        raise ValueError("この版では、その検品のしかたは選べません（社外用の版では Claude のプランの接続は使えません）")
    data = _stored()
    if value == "api_key" and not (cost_ack or data["api_cost_ack"]):
        raise ValueError("API キーで自動の検品をするには、費用の確認が要ります")
    data["auto_inspect"] = value
    if value == "api_key":
        data["api_cost_ack"] = True
    save(data)
    return load()
