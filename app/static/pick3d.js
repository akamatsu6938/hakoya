// 3D で囲んで直す（SPEC §9-3）：画面に見えている部材を、当たり判定用の形（core/pick.py）で割り出す。
// 部材ごとに色（番号）を付けた形を、見えている模型の奥行きと一緒に画面の外の絵へ描き、囲みの中の画素を数える。
// どの部材を選ぶかはサーバー（core/lasso.select_hits）が決める。ここは数えるだけ。
import * as THREE from 'three';

// 番号の色：頂点の色で入れる（three.js の色の変換は頂点の色には効かず、番号がそのまま戻る）。
// 少し手前に描き、同じ深さの模型の面より先に当たるようにする
const PICK_MAT = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide,
  polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 });
// 本物の模型は奥行きだけを書く（色は書かない）。少し奥へずらす
const DEPTH_MAT = new THREE.MeshBasicMaterial({ colorWrite: false, side: THREE.DoubleSide,
  polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });

export function buildPickGroup(data) {
  const s = 1 / (data.scale || 50);              // 実寸 mm → 画面（模型の寸法）
  const group = new THREE.Group();
  data.items.forEach((it, i) => {
    const idx = i + 1;                           // 0 は「何もない」
    const rgb = [idx & 255, (idx >> 8) & 255, (idx >> 16) & 255];
    const v = it.verts, t = it.tris;
    const pos = new Float32Array(t.length * 3), col = new Uint8Array(t.length * 3);
    for (let k = 0; k < t.length; k++) {
      const vi = t[k] * 3;
      pos[k * 3] = v[vi] * s; pos[k * 3 + 1] = v[vi + 1] * s; pos[k * 3 + 2] = v[vi + 2] * s;
      col[k * 3] = rgb[0]; col[k * 3 + 1] = rgb[1]; col[k * 3 + 2] = rgb[2];
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3, true));
    const mesh = new THREE.Mesh(geo, PICK_MAT);
    mesh.userData = { id: it.id, parts: it.parts };
    mesh.renderOrder = 1;
    group.add(mesh);
  });
  return group;
}

function shown(o) {
  for (let p = o; p; p = p.parent) if (!p.visible) return false;
  return true;
}

// 囲み（画面の CSS 画素の点の並び）の中に見えている、部材ごとの画素の数。
// 戻り値：{ hits: [{id, inside, total}], maskPx }（total は画面全体で見えている画素）
export function pickHits(renderer, camera, root, group, loop, cssW, cssH) {
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const w = size.x, h = size.y;
  const visible = new Set();
  const pickScene = new THREE.Scene();           // 背景なし（黒＝番号 0）
  root.updateMatrixWorld(true);
  root.traverse((o) => {
    if (!o.isMesh || !shown(o)) return;
    visible.add(o.name);
    const d = new THREE.Mesh(o.geometry, DEPTH_MAT);
    d.matrixAutoUpdate = false; d.matrix.copy(o.matrixWorld); d.renderOrder = 0;
    pickScene.add(d);
  });
  group.children.forEach((m) => { m.visible = m.userData.parts.some((p) => visible.has(p)); });   // 隠した部品の部材は選ばない
  pickScene.add(group);
  const rt = new THREE.WebGLRenderTarget(w, h, { minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, samples: 0 });
  const oldTarget = renderer.getRenderTarget();
  const oldColor = renderer.getClearColor(new THREE.Color()), oldAlpha = renderer.getClearAlpha();
  renderer.setRenderTarget(rt);
  renderer.setClearColor(0x000000, 1);
  renderer.clear();
  renderer.render(pickScene, camera);
  const buf = new Uint8Array(w * h * 4);
  renderer.readRenderTargetPixels(rt, 0, 0, w, h, buf);
  renderer.setRenderTarget(oldTarget);
  renderer.setClearColor(oldColor, oldAlpha);
  pickScene.remove(group);
  rt.dispose();

  // 囲みの形を、画面と同じ大きさの絵に塗る（GL の行は下から、canvas の行は上から数える）
  const mask = document.createElement('canvas'); mask.width = w; mask.height = h;
  const ctx = mask.getContext('2d');
  const sx = w / cssW, sy = h / cssH;
  ctx.beginPath();
  loop.forEach(([x, y], i) => (i ? ctx.lineTo(x * sx, y * sy) : ctx.moveTo(x * sx, y * sy)));
  ctx.closePath(); ctx.fillStyle = '#fff'; ctx.fill();
  const m = ctx.getImageData(0, 0, w, h).data;
  const total = new Map(), inside = new Map();
  let maskPx = 0;
  for (let y = 0; y < h; y++) {
    const gy = h - 1 - y;
    for (let x = 0; x < w; x++) {
      const inMask = m[(y * w + x) * 4 + 3] > 127;
      if (inMask) maskPx++;
      const g = (gy * w + x) * 4;
      const idx = buf[g] | (buf[g + 1] << 8) | (buf[g + 2] << 16);
      if (!idx) continue;
      total.set(idx, (total.get(idx) || 0) + 1);
      if (inMask) inside.set(idx, (inside.get(idx) || 0) + 1);
    }
  }
  const ids = group.children.map((c) => c.userData.id);
  const hits = [...inside.entries()].map(([idx, n]) => ({ id: ids[idx - 1], inside: n, total: total.get(idx) || n }))
    .filter((hh) => hh.id !== undefined);
  return { hits, maskPx };
}
