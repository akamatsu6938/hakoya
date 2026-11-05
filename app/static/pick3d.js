// 3D で囲んで直す（SPEC §9-3）・3D で押して選ぶ：画面に見えている部材を、当たり判定用の形（core/pick.py）で割り出す。
// 部材ごとに色（番号）を付けた形を、見えている模型の奥行きと一緒に画面の外の絵へ描き、囲みの中（押した所）の画素を読む。
// 囲みでどの部材を選ぶかはサーバー（core/lasso.select_hits）が決める。ここは数えるだけ。
// ★11/5 夜：パーツを離した表示でも当たる（作者「パーツを離した状態でも部品を触れるように」）。
//   部品のメッシュは app.js の applyVisibility() が持ち上げている（position.z。離していないときは 0）。
//   1つの部材が複数の部品にまたがる（外壁は天井の高さで上下に分かれ、上は屋根と一緒に持ち上がる）ので、
//   部材の形を「部品ごとに、その部品に入っている高さの範囲」で切り、その部品の持ち上げ量だけずらした写しを使う（pickCopies）。
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
    mesh.userData = { id: it.id, parts: it.parts, group: it.group, kind: it.kind };
    mesh.renderOrder = 1;
    group.add(mesh);
  });
  return group;
}

function shown(o) {
  for (let p = o; p; p = p.parent) if (!p.visible) return false;
  return true;
}

// ---------------------------------------------------------------- 離した表示：部品ごとの高さの範囲と写し
// 部材（壁・開口）の高さを細かく切り、高さごとに「どの持ち上げ量の部品の組が、その部材をいちばん覆っているか」で決める。
// 覆い方：壁は、その高さの部品の断面が壁の長さのどれだけにあるか。開口は、穴の脇（縁）の断面が壁の厚みのどれだけにあるか。
// となりの部材が少しかかるだけ（壁の端の面・位置合わせの三角・交わる壁）では、その部品の組にしない
const TOL = 0.05;          // 模型の mm。部材の平面の形をこれだけ広げて数える（自分の面は形の縁にちょうど乗るため）
const SLICES = 32;         // 高さを切る数
const TINY = 0.02;         // これより覆っていなければ「ない」
const OPENING_EXTRA = 40 / 50;   // 開口の当たり判定の形は壁より 40mm 厚い（core/pick.py）。模型の mm
const rangeCache = new WeakMap();   // 当たり判定の形の組 → { root, byKey: Map(部品の持ち上げ量の並び → 範囲) }

function hull2d(pts) {     // 平面の点の凸包（反時計回り）
  const p = [...new Map(pts.map((q) => [`${q[0].toFixed(4)},${q[1].toFixed(4)}`, q])).values()]
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) return p;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (list) => {
    const h = [];
    for (const q of list) { while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], q) <= 0) h.pop(); h.push(q); }
    return h;
  };
  const lower = half(p), upper = half(p.slice().reverse());
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

function clipBy(poly, dist) {   // 多角形（[x,y,z] の並び）を dist(p) ≥ 0 の側だけ残す
  const out = [];
  for (let k = 0; k < poly.length; k++) {
    const p = poly[k], q = poly[(k + 1) % poly.length]; const dp = dist(p), dq = dist(q);
    if (dp >= 0) out.push(p);
    if ((dp >= 0) !== (dq >= 0)) {
      const t = dp / (dp - dq);
      out.push([p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t]);
    }
  }
  return out;
}

function frameOf(m) {        // 部材の平面の形（凸包）・長さの向き u・幅の向き v・高さの範囲
  const pos = m.geometry.attributes.position;
  const pts = []; let z0 = Infinity, z1 = -Infinity;
  for (let i = 0; i < pos.count; i++) { pts.push([pos.getX(i), pos.getY(i)]); z0 = Math.min(z0, pos.getZ(i)); z1 = Math.max(z1, pos.getZ(i)); }
  const hull = hull2d(pts);
  if (hull.length < 3 || !(z1 > z0)) return null;
  let best = 0, ux = 1, uy = 0;
  hull.forEach((a, i) => {
    const b = hull[(i + 1) % hull.length]; const l = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (l > best) { best = l; ux = (b[0] - a[0]) / l; uy = (b[1] - a[1]) / l; }
  });
  const us = hull.map((p) => p[0] * ux + p[1] * uy), vs = hull.map((p) => -p[0] * uy + p[1] * ux);
  const xs = hull.map((p) => p[0]), ys = hull.map((p) => p[1]);
  const edges = hull.map((a, i) => {   // 縁ごとの内向きの距離（TOL だけ外へ広げる）
    const b = hull[(i + 1) % hull.length]; const ex = b[0] - a[0], ey = b[1] - a[1]; const len = Math.hypot(ex, ey) || 1;
    return (x, y) => (ex * (y - a[1]) - ey * (x - a[0])) / len + TOL;
  });
  return { edges, ux, uy, umin: Math.min(...us), umax: Math.max(...us), vmin: Math.min(...vs), vmax: Math.max(...vs), z0, z1,
    x0: Math.min(...xs) - TOL, x1: Math.max(...xs) + TOL, y0: Math.min(...ys) - TOL, y1: Math.max(...ys) + TOL };
}

function trianglesNear(tri, f) {   // 部材の箱にかかる三角形だけ（9 個の数で1つ）
  const out = [];
  for (let t = 0; t + 8 < tri.length; t += 9) {
    if (Math.max(tri[t], tri[t + 3], tri[t + 6]) < f.x0 || Math.min(tri[t], tri[t + 3], tri[t + 6]) > f.x1) continue;
    if (Math.max(tri[t + 1], tri[t + 4], tri[t + 7]) < f.y0 || Math.min(tri[t + 1], tri[t + 4], tri[t + 7]) > f.y1) continue;
    if (Math.max(tri[t + 2], tri[t + 5], tri[t + 8]) < f.z0 || Math.min(tri[t + 2], tri[t + 5], tri[t + 8]) > f.z1) continue;
    for (let k = 0; k < 9; k++) out.push(tri[t + k]);
  }
  return out;
}

function sliceIn(tris, f, z) {   // 高さ z の断面のうち、部材の平面の形に入る線分 [[x,y],[x,y]]
  const segs = [];
  for (let t = 0; t + 8 < tris.length; t += 9) {
    const pts = [];
    for (let k = 0; k < 3; k++) {
      const a = t + k * 3, b = t + ((k + 1) % 3) * 3;
      const da = tris[a + 2] - z, db = tris[b + 2] - z;
      if ((da >= 0) !== (db >= 0)) { const s = da / (da - db); pts.push([tris[a] + (tris[b] - tris[a]) * s, tris[a + 1] + (tris[b + 1] - tris[a + 1]) * s]); }
    }
    if (pts.length !== 2) continue;
    let [p, q] = pts; let t0 = 0, t1 = 1;
    for (const d of f.edges) {
      const dp = d(p[0], p[1]), dq = d(q[0], q[1]);
      if (dp < 0 && dq < 0) { t0 = 2; break; }
      if (dp < 0) t0 = Math.max(t0, dp / (dp - dq)); else if (dq < 0) t1 = Math.min(t1, dp / (dp - dq));
    }
    if (t0 > t1) continue;
    const at = (s) => [p[0] + (q[0] - p[0]) * s, p[1] + (q[1] - p[1]) * s];
    segs.push([at(t0), at(t1)]);
  }
  return segs;
}

function unionLength(iv) {     // 区間の並びの、重ならない長さの合計
  iv.sort((a, b) => a[0] - b[0]);
  let sum = 0, s = -Infinity, e = -Infinity;
  for (const [a, b] of iv) { if (a > e) { sum += e - s > 0 ? e - s : 0; s = a; e = b; } else e = Math.max(e, b); }
  return sum + (e - s > 0 ? e - s : 0);
}

function coverage(tris, f, z, isOpening) {   // 高さ z で、部品の組がこの部材をどれだけ覆っているか（0〜1）
  const segs = sliceIn(tris, f, z);
  if (!segs.length) return 0;
  const U = (p) => p[0] * f.ux + p[1] * f.uy, V = (p) => -p[0] * f.uy + p[1] * f.ux;
  if (!isOpening) {            // 壁：断面が長さの向きにどれだけあるか
    const L = f.umax - f.umin; if (L <= 0) return 0;
    return unionLength(segs.map(([p, q]) => [Math.max(Math.min(U(p), U(q)), f.umin), Math.min(Math.max(U(p), U(q)), f.umax)])) / L;
  }
  // 開口：穴の脇（両端の縁の線の上）の断面が、壁の厚みの向きにどれだけあるか（両端のうち多い方）
  const T = Math.max(f.vmax - f.vmin - OPENING_EXTRA, 0.5); const near = 2 * TOL;
  let best = 0;
  for (const edge of [f.umin, f.umax]) {
    const iv = segs.filter(([p, q]) => Math.abs(U(p) - edge) <= near && Math.abs(U(q) - edge) <= near)
      .map(([p, q]) => [Math.min(V(p), V(q)), Math.max(V(p), V(q))]);
    if (iv.length) best = Math.max(best, unionLength(iv) / T);
  }
  return Math.min(best, 1);
}

// 部材（壁・開口）ごとに、どの持ち上げ量の部品の組のどの高さに入っているか：Map(部材の順番 → [{ dz, lo, hi }])（離していない座標）
function elementRanges(root, group) {
  const meshes = []; root.traverse((o) => { if (o.isMesh && o.geometry?.attributes?.position) meshes.push(o); });
  const key = meshes.map((o) => `${o.name}:${o.position.z}`).join(',');
  let c = rangeCache.get(group);
  if (!c || c.root !== root) { c = { root, byKey: new Map() }; rangeCache.set(group, c); }
  if (c.byKey.has(key)) return c.byKey.get(key);
  root.updateMatrixWorld(true);
  const lifts = new Map();     // 持ち上げる量 → その部品の三角形（離していない座標。9 個の数で1つ）
  const v = new THREE.Vector3();
  for (const o of meshes) {
    const pos = o.geometry.attributes.position; const index = o.geometry.index; const dz = o.position.z;
    const n = index ? index.count : pos.count; const list = lifts.get(dz) || [];
    for (let i = 0; i < n; i++) { v.fromBufferAttribute(pos, index ? index.getX(i) : i).applyMatrix4(o.matrixWorld); list.push(v.x, v.y, v.z - dz); }
    lifts.set(dz, list);
  }
  const ranges = new Map();
  group.children.forEach((m, idx) => {
    const ud = m.userData;
    if (ud.group !== 'walls' && ud.group !== 'openings') return;   // 屋根・軒は1つの部品だけ（切らない）
    const f = frameOf(m); if (!f) return;
    const isOpening = ud.group === 'openings';
    const groups = [...lifts].map(([dz, tri]) => ({ dz, tris: trianglesNear(tri, f) })).filter((g) => g.tris.length);
    if (!groups.length) { ranges.set(idx, []); return; }
    const step = (f.z1 - f.z0) / SLICES;
    const zAt = (k) => f.z0 + (k + 0.5) * step + step * 0.0137;   // 面のちょうどの高さを避ける
    const winner = (z) => {    // その高さでいちばん覆っている組（なければ -1）
      let w = -1, best = TINY;
      groups.forEach((g, i) => { const cv = coverage(g.tris, f, z, isOpening); if (cv > best) { best = cv; w = i; } });
      return w;
    };
    const who = Array.from({ length: SLICES }, (_, k) => winner(zAt(k)));
    const spans = new Map();   // 組 → [下, 上]
    for (let k = 0; k < SLICES; k++) {
      const w = who[k]; if (w < 0) continue;
      let lo = f.z0 + k * step, hi = f.z0 + (k + 1) * step;
      if (k > 0 && who[k - 1] >= 0 && who[k - 1] !== w) {          // 下の組との境目：細かく探す
        let a = zAt(k - 1), b = zAt(k);
        for (let it = 0; it < 8; it++) { const mid = (a + b) / 2; if (winner(mid) === w) b = mid; else a = mid; }
        lo = (a + b) / 2;
      }
      if (k < SLICES - 1 && who[k + 1] >= 0 && who[k + 1] !== w) {  // 上の組との境目
        let a = zAt(k), b = zAt(k + 1);
        for (let it = 0; it < 8; it++) { const mid = (a + b) / 2; if (winner(mid) === w) a = mid; else b = mid; }
        hi = (a + b) / 2;
      }
      const s = spans.get(w); spans.set(w, s ? [Math.min(s[0], lo), Math.max(s[1], hi)] : [lo, hi]);
    }
    const ks = who.filter((w) => w >= 0);
    if (ks.length) {           // いちばん下・上の組は、部材の下端・上端まで（屋根の下の三角の先なども）
      const first = spans.get(ks[0]); first[0] = f.z0;
      const last = spans.get(ks[ks.length - 1]); last[1] = f.z1;
    }
    const list = [...spans].map(([i, [lo, hi]]) => ({ dz: groups[i].dz, lo, hi }));
    list.z0 = f.z0; list.z1 = f.z1;   // 切らずに済むか（1つの組だけで下端から上端まで）を見るため
    ranges.set(idx, list);
  });
  c.byKey.set(key, ranges);
  return ranges;
}

function clipGeometryZ(m, lo, hi) {   // 当たり判定の形を高さ lo〜hi で切った形（色＝番号はそのまま）。部材ごとに覚えておく
  const key = `${lo.toFixed(3)}|${hi.toFixed(3)}`;
  const cache = m.userData.clipCache || (m.userData.clipCache = new Map());
  if (cache.has(key)) return cache.get(key);
  const pos = m.geometry.attributes.position; const rgb = m.geometry.attributes.color.array.slice(0, 3);
  const out = [];
  for (let t = 0; t + 2 < pos.count; t += 3) {
    let poly = [0, 1, 2].map((k) => [pos.getX(t + k), pos.getY(t + k), pos.getZ(t + k)]);
    poly = clipBy(clipBy(poly, (p) => p[2] - lo), (p) => hi - p[2]);
    for (let k = 1; k + 1 < poly.length; k++) out.push(poly[0], poly[k], poly[k + 1]);
  }
  const p = new Float32Array(out.length * 3), col = new Uint8Array(out.length * 3);
  out.forEach((q, i) => { p.set(q, i * 3); col.set(rgb, i * 3); });
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(p, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(col, 3, true));
  cache.set(key, geo);
  return geo;
}

// 部材の写し：[{ src（元の形）, idx, dz（持ち上げる量）, geo }]。
// 離していないときは元の形のまま（ずらさない・切らない。今までと同じ）。all：隠した部品の部材も含める（光らせる印用）
export function pickCopies(root, group, { all = false } = {}) {
  const meshes = []; root.traverse((o) => { if (o.isMesh) meshes.push(o); });
  const dzOf = new Map(meshes.map((o) => [o.name, o.position.z]));
  const visible = new Set(meshes.filter((o) => all || shown(o)).map((o) => o.name));
  const out = [];
  if (!meshes.some((o) => Math.abs(o.position.z) > 1e-6)) {
    group.children.forEach((m, idx) => { if (all || m.userData.parts.some((p) => visible.has(p))) out.push({ src: m, idx, dz: 0, geo: m.geometry }); });
    return out;
  }
  const ranges = elementRanges(root, group);
  const shownDz = new Set(meshes.filter((o) => visible.has(o.name)).map((o) => o.position.z));   // 見えている部品の持ち上げ量
  group.children.forEach((m, idx) => {
    const found = ranges.get(idx);
    if (!found || !found.length) {   // 屋根・軒と、範囲を決められなかった部材：見えている最初の部品の量でずらす（切らない）
      const p = m.userData.parts.find((q) => visible.has(q));
      if (p !== undefined) out.push({ src: m, idx, dz: dzOf.get(p) || 0, geo: m.geometry });
      return;
    }
    for (const { dz, lo, hi } of found) {
      // 隠した部品の所は選ばない：その量の部品のうち、部材が写る部品（parts）が見えているか。parts にない部品
      // （屋根の部品に入る下がり壁など）は、その量の部品のどれかが見えているか
      const listed = m.userData.parts.filter((p) => dzOf.get(p) === dz);
      if (listed.length ? !listed.some((p) => visible.has(p)) : !shownDz.has(dz)) continue;
      const whole = lo <= found.z0 && hi >= found.z1;
      out.push({ src: m, idx, dz, geo: whole ? m.geometry : clipGeometryZ(m, lo, hi) });
    }
  });
  return out;
}

// 番号の色で描く場面：見えている模型は奥行きだけ、部材の写しは番号の色
function pickScene(root, group) {
  const scene = new THREE.Scene();               // 背景なし（黒＝番号 0）
  root.updateMatrixWorld(true);
  root.traverse((o) => {
    if (!o.isMesh || !shown(o)) return;
    const d = new THREE.Mesh(o.geometry, DEPTH_MAT);
    d.matrixAutoUpdate = false; d.matrix.copy(o.matrixWorld); d.renderOrder = 0;
    scene.add(d);
  });
  for (const c of pickCopies(root, group)) {     // 隠した部品の部材は選ばない
    const m = new THREE.Mesh(c.geo, PICK_MAT); m.position.z = c.dz; m.renderOrder = 1;
    scene.add(m);
  }
  return scene;
}

function renderIds(renderer, scene, camera, w, h) {
  const rt = new THREE.WebGLRenderTarget(w, h, { minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, samples: 0 });
  const oldTarget = renderer.getRenderTarget();
  const oldColor = renderer.getClearColor(new THREE.Color()), oldAlpha = renderer.getClearAlpha();
  renderer.setRenderTarget(rt);
  renderer.setClearColor(0x000000, 1);
  renderer.clear();
  renderer.render(scene, camera);
  const buf = new Uint8Array(w * h * 4);
  renderer.readRenderTargetPixels(rt, 0, 0, w, h, buf);
  renderer.setRenderTarget(oldTarget);
  renderer.setClearColor(oldColor, oldAlpha);
  rt.dispose();
  return buf;
}

// 囲み（画面の CSS 画素の点の並び）の中に見えている、部材ごとの画素の数。
// 戻り値：{ hits: [{id, inside, total}], maskPx }（total は画面全体で見えている画素）
export function pickHits(renderer, camera, root, group, loop, cssW, cssH) {
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const w = size.x, h = size.y;
  const buf = renderIds(renderer, pickScene(root, group), camera, w, h);

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

// 押した所（画面の CSS 画素）に見えている部材の番号。なければ null。
// 押した所のまわり radius 画素だけを描き、真ん中にいちばん近い部材を選ぶ（細い壁を押しやすく）
export function pickAt(renderer, camera, root, group, x, y, cssW, cssH, radius = 3) {
  const size = renderer.getDrawingBufferSize(new THREE.Vector2());
  const sx = size.x / cssW, sy = size.y / cssH;
  const r = Math.max(1, Math.round(radius * sx)); const n = 2 * r + 1;
  const px = Math.round(x * sx) - r, py = Math.round(y * sy) - r;
  const scene = pickScene(root, group);
  camera.setViewOffset(size.x, size.y, px, py, n, n);   // 画面のうち、押した所のまわりだけを写す
  let buf;
  try { buf = renderIds(renderer, scene, camera, n, n); } finally { camera.clearViewOffset(); }
  let best = 0, bestD = Infinity;
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const g = (j * n + i) * 4;
      const idx = buf[g] | (buf[g + 1] << 8) | (buf[g + 2] << 16);
      if (!idx) continue;
      const d = (i - r) ** 2 + (j - r) ** 2;
      if (d < bestD) { bestD = d; best = idx; }
    }
  }
  return best ? group.children[best - 1]?.userData.id ?? null : null;
}
