"""API キーの保管（SPEC §2-6・§7-4）。キーは Windows の資格情報マネージャーに入れ、コード・設定ファイル・記録には書かない。

- keyring（Windows では資格情報マネージャーを使う）で、HAKOYA の名前で1件だけ保存する。
- 画面には「設定済みかどうか」だけを返し、キーそのもの（一部も）は返さない。
- 環境変数（ANTHROPIC_API_KEY）は使わない。キーは使う人が設定画面で入れる。
"""
from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "HAKOYA"
USERNAME = "anthropic_api_key"


class SecretStoreError(RuntimeError):
    """資格情報マネージャーを使えなかった（日本語のメッセージ）。"""


def get_api_key() -> str | None:
    try:
        return keyring.get_password(SERVICE, USERNAME) or None
    except KeyringError as e:
        raise SecretStoreError("資格情報マネージャーから API キーを読めませんでした") from e


def has_api_key() -> bool:
    return get_api_key() is not None


def set_api_key(key: str) -> None:
    """キーを確かめてから保存する。形がおかしいときは ValueError（日本語、キーの中身は書かない）。"""
    key = (key or "").strip()
    if not key:
        raise ValueError("API キーが空です")
    if not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("API キーに使えない文字（全角・空白など）が入っています")
    if len(key) < 20:
        raise ValueError("API キーが短すぎます。全部を貼り付けたか確かめてください")
    try:
        keyring.set_password(SERVICE, USERNAME, key)
    except KeyringError as e:
        raise SecretStoreError("資格情報マネージャーに API キーを保存できませんでした") from e


def delete_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, USERNAME)
    except PasswordDeleteError:
        pass                                    # もともと無い
    except KeyringError as e:
        raise SecretStoreError("資格情報マネージャーの API キーを消せませんでした") from e
