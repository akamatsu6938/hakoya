"""組み直す前と後で、模型の形が本当に変わったかを比べる（★11/5 夜 作者「承認押したけど、治らないな」）。

修正を承認しても模型が変わらない直し（効かない項目・同じ値）は、「直しました」と言わずに取り消すために使う。
形の名札（部品ごと）：体積・表面積・外形の箱（いちばん小さい所と大きい所）・面の重心・体の重心・点と三角形の数。
- 名札は画面の 3D と同じ model.glb（組んだ状態、模型の mm）から作る。組み立てをもう一度しないので速い（小さな家で 0.03 秒）。
- 組み立ては同じ入力なら同じ形になる（★試した：同じ家を2回組むと、名札はぴったり同じ）。窓を 10mm 動かしただけでも、
  体積は同じだが重心が動く（約 0.004mm）ので見分けられる。
- 見分けられないとき（前の glb がない・読めない・building.json や overrides.json より古い）は「変わった」とみなす
  （人の直しを勝手に消さない）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from core.paths import out_dir

REL_TOL = 1e-7          # 値の大きさに対する許し（組み立ての計算の順番で出る、ごく小さな違いは同じとみなす）


def _mesh_tag(vertices, faces) -> dict:
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if not len(v) or not len(f):
        return {"volume": 0.0, "area": 0.0, "min": [0.0] * 3, "max": [0.0] * 3, "area_center": [0.0] * 3,
                "volume_center": [0.0] * 3, "points": int(len(v)), "triangles": int(len(f))}
    tri = v[f]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area_i = np.linalg.norm(cross, axis=1) / 2
    vol_i = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])) / 6     # 原点との四面体（閉じた立体なら和が体積）
    area, vol = float(area_i.sum()), float(vol_i.sum())
    area_c = (area_i[:, None] * tri.mean(axis=1)).sum(axis=0) / area if area > 0 else np.zeros(3)
    vol_c = (vol_i[:, None] * tri.sum(axis=1) / 4).sum(axis=0) / vol if abs(vol) > 1e-12 else np.zeros(3)
    return {"volume": vol, "area": area, "min": v.min(axis=0).tolist(), "max": v.max(axis=0).tolist(),
            "area_center": [float(x) for x in area_c], "volume_center": [float(x) for x in vol_c],
            "points": int(len(v)), "triangles": int(len(f))}


def signature(glb_path: Path) -> dict[str, dict] | None:
    """model.glb の部品ごとの名札。ファイルがない・読めないときは None。"""
    import trimesh
    p = Path(glb_path)
    if not p.is_file():
        return None
    try:
        scene = trimesh.load(str(p), force="scene")
    except Exception:  # noqa: BLE001  書きかけ・壊れたファイル（比べられない → 呼ぶ側が「変わった」とみなす）
        return None
    return {str(name): _mesh_tag(g.vertices, g.faces) for name, g in scene.geometry.items()
            if hasattr(g, "vertices") and hasattr(g, "faces")}


MODEL_INPUTS = ("building.json", "overrides.json")     # 模型の形を決めるファイル（model.glb はこれより後に書かれるはず）


def signature_of(alias: str) -> dict[str, dict] | None:
    """物件の、いま書き出してある模型（out/<物件>/model/model.glb）の名札。
    glb が building.json・overrides.json より古いときは None（★11/6 見直し：修正を取り消したあとの組み直しが失敗した・
    別のプロセスが修正を書いた、などで、glb がいまの修正の形でない。その glb を「前」にすると、取り消した直しをもう一度承認したとき
    「形が変わらない」と判断して、本当の直しを取り消してしまう。比べられない → 呼ぶ側は「変わった」とみなす）。"""
    d = out_dir(alias)
    glb = d / "model" / "model.glb"
    try:
        made = glb.stat().st_mtime_ns
    except OSError:
        return None
    for name in MODEL_INPUTS:
        try:
            if (d / name).stat().st_mtime_ns > made:
                return None
        except OSError:                     # ない（修正がまだない）・読めないファイルは比べない
            continue
    return signature(glb)


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= REL_TOL * max(1.0, abs(float(a)), abs(float(b)))


def changed_parts(before: dict | None, after: dict | None) -> list[str]:
    """形が変わった部品の名前（model.glb の名前：floor1・roof など）。比べられないときは ["?"]。"""
    if before is None or after is None:
        return ["?"]
    out = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a is None or b is None:
            out.append(key)
            continue
        same = (a["points"] == b["points"] and a["triangles"] == b["triangles"]
                and _close(a["volume"], b["volume"]) and _close(a["area"], b["area"])
                and all(_close(x, y) for k in ("min", "max", "area_center", "volume_center") for x, y in zip(a[k], b[k])))
        if not same:
            out.append(key)
    return out


def changed(before: dict | None, after: dict | None) -> bool:
    """模型の形が変わったか（比べられないときは True）。"""
    return bool(changed_parts(before, after))
