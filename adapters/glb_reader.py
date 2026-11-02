"""Walk in home の glb を読み、部材（pobj の子ノード）ごとの三角形を取り出す。

座標はノード pobj の座標系（mm・Z上向き・GL＝Z 0）で返す（SPEC §4-1）。
上の root / gltf ノードは「Z上→Y上」の回転と ×0.001 をかけているだけなので使わない。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pygltflib import GLTF2

from adapters.walkinhome_types import material_code, part_type, type_code

_COMPONENT_DTYPE = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_TYPE_SIZE = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
POBJ = "pobj"


@dataclass
class Primitive:
    material: str          # マテリアル名そのまま
    code: str | None       # 部材と部位のコード（例：HEYA_YUKA）
    vertices: np.ndarray   # (N, 3) mm、pobj の座標系
    triangles: np.ndarray  # (M, 3) 頂点番号


@dataclass
class Part:
    node: int              # glTF のノード番号（出どころの「#番号」に使う）
    name: str
    type_code: str | None
    primitives: list[Primitive] = field(default_factory=list)

    @property
    def source(self) -> str:
        return f"glb:{self.type_code or 'unknown'}#{self.node}"

    @property
    def role(self) -> str:
        return part_type(self.type_code).role

    def bounds(self) -> tuple[np.ndarray, np.ndarray] | None:
        pts = [p.vertices for p in self.primitives if len(p.vertices)]
        if not pts:
            return None
        allv = np.vstack(pts)
        return allv.min(axis=0), allv.max(axis=0)

    def faces(self, code: str | None = None) -> np.ndarray:
        """三角形の頂点座標 (K, 3, 3)。code を渡すとその部位だけ。"""
        tris = [p.vertices[p.triangles] for p in self.primitives
                if (code is None or p.code == code) and len(p.triangles)]
        return np.concatenate(tris) if tris else np.zeros((0, 3, 3))


@dataclass
class GlbModel:
    generator: str | None
    version_info: str | None
    parts: list[Part]

    def by_role(self, role: str) -> list[Part]:
        return [p for p in self.parts if p.role == role]

    def by_type(self, code: str) -> list[Part]:
        return [p for p in self.parts if p.type_code == code]


def _node_matrix(node) -> np.ndarray:
    """ノードの変換（4x4）。matrix か、移動・回転・拡大から作る。"""
    if node.matrix:
        return np.array(node.matrix, dtype=np.float64).reshape(4, 4).T  # glTF は列優先
    m = np.eye(4)
    if node.scale:
        m = np.diag([*node.scale, 1.0]) @ m
    if node.rotation:
        x, y, z, w = node.rotation
        r = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ])
        rm = np.eye(4)
        rm[:3, :3] = r
        m = rm @ m
    if node.translation:
        t = np.eye(4)
        t[:3, 3] = node.translation
        m = t @ m
    return m


def _read_accessor(gltf: GLTF2, blob: bytes, index: int) -> np.ndarray:
    acc = gltf.accessors[index]
    view = gltf.bufferViews[acc.bufferView]
    dtype = np.dtype(_COMPONENT_DTYPE[acc.componentType])
    ncomp = _TYPE_SIZE[acc.type]
    start = (view.byteOffset or 0) + (acc.byteOffset or 0)
    stride = view.byteStride or dtype.itemsize * ncomp
    if stride == dtype.itemsize * ncomp:
        arr = np.frombuffer(blob, dtype=dtype, count=acc.count * ncomp, offset=start)
    else:
        rows = [np.frombuffer(blob, dtype=dtype, count=ncomp, offset=start + i * stride) for i in range(acc.count)]
        arr = np.concatenate(rows) if rows else np.zeros(0, dtype=dtype)
    return arr.reshape(acc.count, ncomp) if ncomp > 1 else arr


def _collect(gltf: GLTF2, blob: bytes, index: int, m: np.ndarray, part: Part) -> None:
    """ノードの形（三角形）を部材に足す。子のノードがあれば、行列をかけ合わせて同じ部材に足す。
    ★物件A は入れ子なし。物件B は照明（typeELight）の下に子のノードが1つあった（2026-11-03）。"""
    node = gltf.nodes[index]
    if node.mesh is not None:
        for prim in gltf.meshes[node.mesh].primitives:
            if prim.mode not in (None, 4):  # 三角形だけ
                continue
            pos = _read_accessor(gltf, blob, prim.attributes.POSITION).astype(np.float64)
            pos = pos @ m[:3, :3].T + m[:3, 3]
            if prim.indices is not None:
                tri = _read_accessor(gltf, blob, prim.indices).astype(np.int64).reshape(-1, 3)
            else:
                tri = np.arange(len(pos)).reshape(-1, 3)
            mat = gltf.materials[prim.material].name if prim.material is not None else ""
            part.primitives.append(Primitive(material=mat or "", code=material_code(mat), vertices=pos, triangles=tri))
    for k in node.children or []:
        _collect(gltf, blob, k, m @ _node_matrix(gltf.nodes[k]), part)


def read_glb(path: Path) -> GlbModel:
    gltf = GLTF2.load_binary(str(path))
    blob = gltf.binary_blob()
    names = [n.name or "" for n in gltf.nodes]
    if POBJ not in names:
        raise ValueError("glb の中にノード pobj が見つかりません（Walk in home の glb ではない可能性があります）。")
    pobj_index = names.index(POBJ)
    pobj = gltf.nodes[pobj_index]
    pobj_m = np.eye(4)  # pobj の座標系で返すので、pobj 自身の変換はかけない

    parts: list[Part] = []
    for child in pobj.children or []:
        node = gltf.nodes[child]
        part = Part(node=child, name=node.name or "", type_code=type_code(node.name or ""))
        _collect(gltf, blob, child, pobj_m @ _node_matrix(node), part)
        parts.append(part)

    extras = (gltf.asset.extras or {}) if gltf.asset else {}
    if not extras and gltf.extras:
        extras = gltf.extras
    return GlbModel(generator=gltf.asset.generator if gltf.asset else None,
                    version_info=(extras or {}).get("WIH_version_info"), parts=parts)
