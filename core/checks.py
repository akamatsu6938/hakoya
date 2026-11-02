"""コードによる検査（SPEC §3：閉じた立体・浮いた部材・造形範囲・glb との数の差）。結果は日本語の一覧で返す。"""
from __future__ import annotations

import numpy as np
import trimesh

BUILD_VOLUME_MM = 256.0   # Bambu Lab A1


def part_checks(key: str, label: str, mesh: trimesh.Trimesh, body_volumes: list[float]) -> list[dict]:
    """body_volumes：離れた塊ごとの体積（manifold の decompose で数える。trimesh の split は追加の部品が要るため）。"""
    out = []

    def add(level, item, message):
        out.append({"part": key, "level": level, "item": item, "message": f"{label}：{message}"})

    if mesh.is_empty:
        add("高", "empty", "形がありません")
        return out
    if not mesh.is_watertight:
        add("高", "watertight", "閉じた立体になっていません（印刷できない可能性）")
    else:
        add("情報", "watertight", "閉じた立体です")
    if len(body_volumes) > 1:
        vols = sorted(body_volumes, reverse=True)
        small = [v for v in vols[1:] if v < vols[0] * 0.01]
        add("中", "islands", f"離れた塊が {len(vols)} 個あります（小さいもの {len(small)} 個。浮いた部材の可能性）")
    size = mesh.extents
    if (size > BUILD_VOLUME_MM).any():
        add("高", "build_volume", f"大きさ {size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f}mm が造形範囲 {BUILD_VOLUME_MM:.0f}mm を超えています。分け方を考える必要があります")
    else:
        add("情報", "build_volume", f"大きさ {size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f}mm（造形範囲に収まる）")
    add("情報", "volume", f"体積 {mesh.volume / 1000:.1f}cm³（樹脂の目安）")
    return out


def building_checks(b: dict) -> list[dict]:
    """glb との数の差など、building.json の段階で分かること。"""
    out = []
    tate = next((r["count"] for r in b.get("inventory", []) if r["type"] == "typeETate"), None)
    if tate is not None and tate != len(b.get("openings", [])):
        out.append({"part": "building", "level": "中", "item": "opening_count",
                    "message": f"建具 {tate} 個のうち、開口になったのは {len(b.get('openings', []))} 個"})
    for u in b.get("unresolved", []):
        out.append({"part": "building", "level": "中", "item": "unresolved", "message": f"未解決の所 {u['id']}：{u['note']}"})
    return out
