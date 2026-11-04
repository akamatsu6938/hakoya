"""3MF の書き出し（Bambu Studio でそのまま開ける形。SPEC §8）。

- パーツはオブジェクトごと（基礎・1階・屋根・ポーチの天井）。1階は「床・内壁」と「外壁」の2つの部品を持つ。
- 外壁の部品にだけ、Bambu Studio の部品ごとの設定としてファジースキン（外側の輪郭だけ）を書く
  （Metadata/model_settings.config の <part> の metadata。キー名は BambuStudio のソースで確認：fuzzy_skin=external）。
- 色は入れない（SPEC §1）。project_settings.config は入れない（プリンターの設定を上書きしないため）。
- (要検証) Bambu Studio で開いたとき、部品の設定としてファジースキンが表示され、スライスに効くか。

座標は模型寸法（mm）。印刷の向き（屋根は面ごとに上の面を下に寝かせたもの。core/print_split.py）で、オブジェクトを横に並べて置く。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
BED_MM = 256.0
SPACING_MM = 300.0   # オブジェクトどうしの間隔（2つ目からは造形範囲の外に置く。Bambu Studio の「並べる」で配置する）


def _mesh_xml(obj_id: int, name: str, verts: np.ndarray, tris: np.ndarray) -> str:
    vs = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in verts)
    ts = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return (f'<object id="{obj_id}" name="{escape(name)}" type="model"><mesh>'
            f'<vertices>{vs}</vertices><triangles>{ts}</triangles></mesh></object>')


def write_3mf(path: Path, objects: list[dict], fuzzy: dict, spacing: float = SPACING_MM,
              bambu_version: str | None = None) -> None:
    """objects: [{"name": 表示名, "parts": [{"name", "verts"(N,3), "tris"(M,3), "fuzzy_skin": bool}]}]
    頂点は印刷の向き・模型寸法。fuzzy: {"thickness": mm, "point_distance": mm}

    bambu_version を渡すと「Bambu 形式」で書く（試し）：★Bambu Studio は Bambu 製と認めない 3MF の設定を捨てて
    形だけ読む（"The 3mf is not from Bambu Lab, load geometry data only."）。そこで同梱の見本の 3MF と同じく
    Application・BambuStudio:3mfVersion・部品の matrix・プレートの割り当てを書く。プリンターや樹脂の設定
    （project_settings.config）は入れない（使う人の設定を上書きしないため）。"""
    resources, build, settings, plates = [], [], [], []
    next_id = 1
    for k, obj in enumerate(objects):
        allv = np.vstack([p["verts"] for p in obj["parts"]])
        lo, hi = allv.min(axis=0), allv.max(axis=0)
        size = hi - lo
        # 床に置き、造形範囲の真ん中へ。2つ目からは横に並べる
        if spacing < BED_MM:   # 小さい物（試験片）は造形範囲の中に横に並べる
            shift = np.array([10 + k * spacing - lo[0], (BED_MM - size[1]) / 2 - lo[1], -lo[2]])
        else:
            shift = np.array([k * spacing + (BED_MM - size[0]) / 2 - lo[0], (BED_MM - size[1]) / 2 - lo[1], -lo[2]])
        part_ids = []
        part_settings = []
        for p in obj["parts"]:
            pid = next_id
            next_id += 1
            resources.append(_mesh_xml(pid, p["name"], p["verts"] + shift, p["tris"]))
            part_ids.append(pid)
            meta = [("name", p["name"])]
            if bambu_version:
                meta.append(("matrix", "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"))
            if p.get("fuzzy_skin"):
                meta += [("fuzzy_skin", "external"), ("fuzzy_skin_thickness", f"{fuzzy['thickness']:g}"),
                         ("fuzzy_skin_point_distance", f"{fuzzy['point_distance']:g}")]
            part_settings.append((pid, meta))
        oid = next_id
        next_id += 1
        comps = "".join(f'<component objectid="{pid}"/>' for pid in part_ids)
        resources.append(f'<object id="{oid}" name="{escape(obj["name"])}" type="model"><components>{comps}</components></object>')
        build.append(f'<item objectid="{oid}"/>')
        parts_xml = "".join(
            f'<part id="{pid}" subtype="normal_part">' + "".join(
                f'<metadata key="{escape(k2)}" value="{escape(v)}"/>' for k2, v in meta) + "</part>"
            for pid, meta in part_settings)
        extra = '<metadata key="extruder" value="1"/>' if bambu_version else ""
        settings.append(f'<object id="{oid}"><metadata key="name" value="{escape(obj["name"])}"/>{extra}{parts_xml}</object>')
        if bambu_version:  # オブジェクトごとに別のプレート（1つの造形範囲に1パーツ）
            plates.append(f'<plate><metadata key="plater_id" value="{k + 1}"/><metadata key="plater_name" value="{escape(obj["name"])}"/>'
                          f'<metadata key="locked" value="false"/><model_instance><metadata key="object_id" value="{oid}"/>'
                          f'<metadata key="instance_id" value="0"/></model_instance></plate>')

    app = f"BambuStudio-{bambu_version}" if bambu_version else "HAKOYA"
    version_meta = '<metadata name="BambuStudio:3mfVersion">1</metadata>' if bambu_version else ""
    model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="ja-JP" xmlns="{CORE_NS}">'
             f'<metadata name="Application">{app}</metadata>{version_meta}<metadata name="Title">HAKOYA 模型</metadata>'
             f'<resources>{"".join(resources)}</resources><build>{"".join(build)}</build></model>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                     '<Default Extension="config" ContentType="text/xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    config = f'<?xml version="1.0" encoding="UTF-8"?>\n<config>{"".join(settings)}{"".join(plates)}</config>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", config)
    path.write_bytes(buf.getvalue())
