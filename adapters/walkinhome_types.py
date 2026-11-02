"""Walk in home の glb の「部材の種類」（ノード名の先頭 typeE○○）と、模型での扱い（SPEC §4-1）。

Walk in home 固有の決まりなので、会社ルール（rules/*.yaml）ではなくここに置く。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PartType:
    label: str      # 日本語の名前
    role: str       # 模型での扱い（下の ROLE_* のどれか）
    note: str = ""  # 推定などの注記


# 模型での扱い
ROLE_EXTERIOR_WALL = "exterior_wall"     # 外壁の元
ROLE_ROOM = "room"                       # 内壁の元（部屋と部屋の隙間）
ROLE_OPENING = "opening"                 # 開口
ROLE_ROOF = "roof"                       # 屋根
ROLE_FOUNDATION = "foundation"           # 基礎
ROLE_STANDALONE_WALL = "standalone_wall" # 厚みのある単独の壁
ROLE_PORCH = "porch"                     # ポーチ・段
ROLE_PORCH_SOFFIT = "porch_soffit"       # ポーチの天井
ROLE_OPTIONAL = "optional"               # 物件ごとに切り替え（既定は省く）
ROLE_OMIT = "omit"                       # 省く
ROLE_SITE = "site"                       # 敷地（印刷しない。大きさの確認だけ）
ROLE_EXCLUDE = "exclude"                 # 除外（家具・外構など）

PART_TYPES: dict[str, PartType] = {
    "typeEGaih": PartType("外壁", ROLE_EXTERIOR_WALL),
    "typeEHeya": PartType("部屋", ROLE_ROOM),
    "typeEShuu": PartType("収納の内側", ROLE_ROOM, "(推定)"),
    "typeETate": PartType("建具", ROLE_OPENING),
    "typeEYane": PartType("屋根", ROLE_ROOF),
    "typeEKiso": PartType("基礎", ROLE_FOUNDATION),
    "typeEKabe": PartType("単独の壁", ROLE_STANDALONE_WALL),
    "typeEPoch": PartType("ポーチ・段", ROLE_PORCH),
    "typeESten": PartType("外部の天井", ROLE_PORCH_SOFFIT, "(推定)"),
    "typeEMhis": PartType("窓の庇", ROLE_OPTIONAL, "(推定)"),
    "typeEShun": PartType("枕棚・ハンガーパイプ", ROLE_OMIT, "(推定)"),
    "typeEKaun": PartType("カウンター", ROLE_OMIT, "(推定)"),
    "typeEAgar": PartType("上がり框", ROLE_OMIT, "(推定)"),
    "typeEThas": PartType("窓の横の小さな部材", ROLE_OMIT, "中身は不明"),
    "typeESiki": PartType("敷地", ROLE_SITE),
    "typeEPark": PartType("駐車場の床", ROLE_EXCLUDE),
    "typeEInte": PartType("家具・設備", ROLE_EXCLUDE),
    "typeEExte": PartType("外構", ROLE_EXCLUDE),
    "typeETree": PartType("植栽", ROLE_EXCLUDE),
}

UNKNOWN = PartType("不明な部材", ROLE_OMIT, "表にない種類。必要なら質問する")

_TYPE_RE = re.compile(r"^(typeE[A-Za-z]+)")
_MATERIAL_RE = re.compile(r"^Material\(Bui(\w+)\)")


def type_code(node_name: str) -> str | None:
    """ノード名から種類のコード（例：typeETate）を取り出す。"""
    m = _TYPE_RE.match(node_name or "")
    return m.group(1) if m else None


def part_type(code: str | None) -> PartType:
    return PART_TYPES.get(code or "", UNKNOWN)


def material_code(material_name: str | None) -> str | None:
    """マテリアル名から部材と部位のコードを取り出す（例：Material(BuiHEYA_YUKA) → HEYA_YUKA）。"""
    m = _MATERIAL_RE.match(material_name or "")
    return m.group(1) if m else None
