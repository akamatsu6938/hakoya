"""glb の読み取り（adapters/glb_reader.py）を、テストの中で作った小さな glb で確かめる（実データ不要）。"""
from __future__ import annotations

import numpy as np
import pytest
from pygltflib import GLTF2, Accessor, Asset, Buffer, BufferView, Material, Mesh, Node, Primitive, Scene, Attributes

from adapters.glb_reader import read_glb


def _make_glb(path):
    """root(Y上・m) → pobj(mm・Z上) → 部材2つ（1つは行列で X+1000 移動）の小さな glb。"""
    tri = np.array([[0, 0, 0], [100, 0, 0], [0, 100, 0]], dtype=np.float32)  # 床の三角形（Z=0）
    idx = np.array([0, 1, 2], dtype=np.uint16)
    blob = tri.tobytes() + idx.tobytes() + b"\x00\x00"  # 4バイト境界に合わせる
    g = GLTF2(
        asset=Asset(version="2.0", generator="WIH", extras={"WIH_version_info": "テスト版"}),
        scene=0, scenes=[Scene(nodes=[0])],
        nodes=[
            Node(name="root", children=[1], matrix=[1, 0, 0, 0, 0, 0, -0.001, 0, 0, 0.001, 0, 0, 0, 0, 0, 1]),
            Node(name="pobj", children=[2, 3]),
            Node(name="typeEHeya:部屋1", mesh=0),
            Node(name="typeEInte:家具\\いす.sym", mesh=1,
                 matrix=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1000, 0, 624, 1]),
        ],
        meshes=[Mesh(primitives=[Primitive(attributes=Attributes(POSITION=0), indices=1, material=0)]),
                Mesh(primitives=[Primitive(attributes=Attributes(POSITION=0), indices=1, material=1)])],
        materials=[Material(name="Material(BuiHEYA_YUKA)"), Material(name="木目")],
        accessors=[
            Accessor(bufferView=0, componentType=5126, count=3, type="VEC3", min=[0, 0, 0], max=[100, 100, 0]),
            Accessor(bufferView=1, componentType=5123, count=3, type="SCALAR"),
        ],
        bufferViews=[BufferView(buffer=0, byteOffset=0, byteLength=36), BufferView(buffer=0, byteOffset=36, byteLength=6)],
        buffers=[Buffer(byteLength=len(blob))],
    )
    g.set_binary_blob(blob)
    g.save_binary(str(path))


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "小さな家.glb"
    _make_glb(p)
    return read_glb(p)


def test_parts_and_types(model):
    assert model.generator == "WIH"
    assert model.version_info == "テスト版"
    assert [p.type_code for p in model.parts] == ["typeEHeya", "typeEInte"]
    assert [p.role for p in model.parts] == ["room", "exclude"]


def test_material_code_and_source(model):
    room = model.parts[0]
    assert room.primitives[0].code == "HEYA_YUKA"
    assert room.source == "glb:typeEHeya#2"
    assert model.parts[1].primitives[0].code is None  # Bui… でないマテリアル


def test_coordinates_in_pobj_frame(model):
    """root の回転・×0.001 はかけず、部材の行列（移動）だけかける。"""
    lo, hi = model.parts[0].bounds()
    assert np.allclose(lo, [0, 0, 0]) and np.allclose(hi, [100, 100, 0])
    lo, hi = model.parts[1].bounds()
    assert np.allclose(lo, [1000, 0, 624]) and np.allclose(hi, [1100, 100, 624])
