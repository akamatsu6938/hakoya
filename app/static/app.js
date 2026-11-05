// HAKOYA の画面。サーバー（app/server.py）とやりとりするのは物件の仮名だけ。
// 11/5 夜：印刷前の仕上げ用ソフトの作りに作り直した（作者の要望「Bambu Studio・Blender・CAD のように」）。
//   上の帯：ファイル（新しく読み込む・過去の物件を開く・最近・保存・印刷・閉じる・環境設定）・作業のタブ・モードの札
//   道具の帯：タブごと。左：部品と部材。真ん中：3D／平面図（どのタブでも同じ）。右：選んだ部材の性質とタブの欄。下：状態の帯
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { buildPickGroup, pickHits, pickAt, pickCopies } from './pick3d.js';

const $ = (s) => document.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const api = (path) => path.replace('{a}', encodeURIComponent(state.alias));
const state = { alias: null, building: null, root: null, exploded: false, pick: null, hl: null,
  ws: 'model', pane: '3d', mode: 'own', settings: null, props: [], focus: null, hidden: new Set(), checks: null,
  overrides: [], refreshing: false, inspecting: false, orbiting: false, savedAt: null, treeOpen: new Set(['parts']) };
const KIND = { window: '窓', door: 'ドア', entrance_door: '玄関ドア', exterior: '外壁', interior: '内壁', standalone: '単独の壁' };
const SIDE = { W: '西', E: '東', S: '南', N: '北' };
const ROOF_SHAPE = { gable: '切妻', hip: '寄棟', multi: '複数の面' };   // 片流れは今までどおり書かない
const roofKind = (r) => (r.kind === 'lean_to' ? '下屋の屋根' : state.multi ? '大屋根' : '屋根')
  + (ROOF_SHAPE[r.shape] ? `（${ROOF_SHAPE[r.shape]}・面${(r.planes || []).length}枚）` : '');
const roofShort = (r) => (r.kind === 'lean_to' ? '下屋の屋根' : state.multi ? '大屋根' : '屋根');
const MODE_LABEL = { own: '自社用', other: '他社用', sample: '見本用' };
const MODE_TITLE = { own: '自社用：自分の会社で使う形（全部の機能）', other: '他社用：ほかの会社で使う形（AI は API キーと AI なし）',
  sample: '見本用：審査・動画用（見本の家だけ・手順の帯）' };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const MODEL_SCALE = 50;            // 1/50：模型の mm＝実寸 mm ÷ 50
const isElementId = (id) => /^[WOY]\d+(:[WESN])?$/.test(String(id || ''));
const baseId = (id) => String(id || '').split(':')[0];

function setStatus(text, cls = '') { const s = $('#status'); s.textContent = text; s.title = text; s.className = 'status ' + cls; }

// お知らせ（右下に数秒だけ出る札）。画面を移らずに「直しました」などを知らせる（★11/5 夜 作者「承認すると画面が移って消える」）。
// kind：ok（済み）・err（失敗）・info（知らせ）・busy（しているところ。自分では消えない → 返り値の close() で消す）
// action：{ label, fn } を渡すと、札の中にボタンを出す（例：取り消す・見る）
function toast(text, { kind = 'ok', action = null, ms = null } = {}) {
  const box = $('#toasts'); if (!box) { setStatus(text, kind === 'busy' ? 'busy' : kind); return { close() {}, update() {} }; }
  const el = document.createElement('div'); el.className = 'toast ' + kind; el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  const msg = document.createElement('span'); msg.className = 'toast-text'; msg.textContent = text; el.appendChild(msg);
  let timer = null;
  const close = () => { clearTimeout(timer); el.classList.add('out'); setTimeout(() => el.remove(), 180); };
  if (action && action.label) {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'toast-act'; b.textContent = action.label;
    b.addEventListener('click', () => { try { action.fn?.(); } finally { close(); } });
    el.appendChild(b);
  }
  const x = document.createElement('button'); x.type = 'button'; x.className = 'toast-x'; x.textContent = '×'; x.title = '閉じる'; x.addEventListener('click', close);
  el.appendChild(x);
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();      // 出しすぎない（古いものから消す）
  const life = ms ?? (kind === 'busy' ? 0 : kind === 'err' ? 9000 : action ? 8000 : 4500);
  if (life > 0) timer = setTimeout(close, life);
  return { close, update(t, k) { msg.textContent = t; if (k) { el.className = 'toast ' + k; if (k !== 'busy') timer = setTimeout(close, k === 'err' ? 9000 : 4500); } } };
}
window.toast = toast;

async function call(method, path, body) {
  const res = await fetch(api(path), { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch (e) { /* 本文なし */ } const err = new Error(msg); err.status = res.status; throw err; }
  return res.headers.get('content-type')?.includes('json') ? res.json() : res.text();
}
// 窓口がない（合わせる前のサーバー）か：FastAPI の「Not Found」
const missingEndpoint = (e) => e && (e.status === 405 || (e.status === 404 && e.message === 'Not Found'));

function fmtTime(iso) {   // 今日なら 15:42、ほかの日は 11/4 15:42
  const d = iso instanceof Date ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const hm = `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
  const now = new Date();
  return d.toDateString() === now.toDateString() ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

// ---------------------------------------------------------------- 作業のタブ（模型・修正・検品・印刷・会社ルール）
const WS_LIST = ['model', 'fix', 'inspect', 'print', 'rules'];
function setWorkspace(ws) {
  if (!WS_LIST.includes(ws) || (ws === 'rules' && state.mode !== 'own')) ws = 'model';
  state.ws = ws; document.body.dataset.ws = ws;
  $$('.ws-tabs [data-ws]').forEach((b) => { const on = b.dataset.ws === ws; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false'); });
  $$('.strip[data-ws]').forEach((s) => { s.hidden = s.dataset.ws !== ws; });
  $$('.ws-panel[data-ws]').forEach((p) => { p.hidden = p.dataset.ws !== ws; });
  if (lasso.on || lasso.on3d) { setLassoMode(false); setLasso3dMode(false); }
  // ★11/5 夜：囲みは修正タブ以外に移っても消さない（作者「待っている間に他のことをするとどっか行ってしまう」）
  if (ws === 'fix') { loadFixRequests(); if (lasso.selection.length) markLassoSelection(); }   // 依頼の一覧を読み直す
  reapplyFocus();                                          // 指摘や一覧で光らせた部材は、タブを替えても残す
  renderInspector();
  if (ws === 'rules') renderRules();
}
$('.ws-tabs').addEventListener('click', (e) => { const b = e.target.closest('button[data-ws]'); if (b) setWorkspace(b.dataset.ws); });
$('.ws-tabs').addEventListener('keydown', (e) => {   // 横のタブは左右の矢印でも動ける
  if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
  const btns = $$('.ws-tabs button[data-ws]').filter((b) => b.offsetParent); const k = btns.indexOf(document.activeElement); if (k < 0) return;
  e.preventDefault(); const next = btns[(k + (e.key === 'ArrowRight' ? 1 : btns.length - 1)) % btns.length]; next.focus(); next.click();
});
// タブの数（修正：直しの件数・検品：まだ決めていない指摘・印刷：検査の高と中の件数）。tone：bad（高がある）／warn
function setTabCount(ws, n, tone = '') {
  const c = $(`.ws-tabs button[data-ws="${ws}"] .count`); if (!c) return;
  c.hidden = !n; c.textContent = n ? String(n) : ''; c.className = 'count' + (tone ? ' ' + tone : '');
}

// ---------------------------------------------------------------- 3D／平面図の切り替え（真ん中。どのタブでも同じ）
function setPane(pane) {
  state.pane = pane; document.body.dataset.pane = pane;
  $$('#pane-switch [data-pane]').forEach((b) => { const on = b.dataset.pane === pane; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false'); });
  $('#v3d').classList.toggle('on', pane === '3d'); $('#vplan').classList.toggle('on', pane === 'plan');
  clearLassoLines(); setLassoMode(false); setLasso3dMode(false);   // 図を替えたら囲みの線だけ消す（囲んだ部材は残す。★11/5 夜）
  reapplyFocus();
  if (lasso.selection.length) markLassoSelection();
  if (pane === '3d') resize();
}
$('#pane-switch').addEventListener('click', (e) => { const b = e.target.closest('button[data-pane]'); if (b && b.dataset.pane !== state.pane) setPane(b.dataset.pane); });

// ---------------------------------------------------------------- 3D 表示
const holder = $('#canvas');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(window.devicePixelRatio);
holder.appendChild(renderer.domElement);
// 見た目（造形室）：暗い背景に、3D プリンターの金色の造形板（256mm 角）と、樹脂の灰色の模型
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x1f2226);
scene.add(new THREE.HemisphereLight(0xe8eef5, 0x3a3a3a, 1.5));
const sun = new THREE.DirectionalLight(0xffffff, 1.5); sun.position.set(-1, -2, 3); scene.add(sun);
const PART_COLORS = { foundation: 0x7d848b, floor1: 0xc9cdd1, exterior1: 0xe2e5e8, roof: 0x8e959c, roof_ext: 0xdadde1,
  roof_lower: 0x979ea5, roof_lower_ext: 0xdadde1, floor2: 0xc9cdd1, exterior2: 0xe2e5e8 };
const plate = (() => {             // 造形板：ざらついた金色の板（模型の mm で 256 角、角を丸める）。模型の真下に置く
  const cv = document.createElement('canvas'); cv.width = cv.height = 512;
  const cx = cv.getContext('2d'); cx.fillStyle = '#b8924f'; cx.fillRect(0, 0, 512, 512);
  for (let i = 0; i < 26000; i++) {
    cx.fillStyle = Math.random() > 0.5 ? 'rgba(255,230,170,0.10)' : 'rgba(60,40,10,0.10)';
    cx.fillRect(Math.random() * 512, Math.random() * 512, 1.5, 1.5);
  }
  const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace;
  const shape = new THREE.Shape(); const r = 10, s = 128;
  shape.moveTo(-s + r, -s); shape.lineTo(s - r, -s); shape.quadraticCurveTo(s, -s, s, -s + r); shape.lineTo(s, s - r);
  shape.quadraticCurveTo(s, s, s - r, s); shape.lineTo(-s + r, s); shape.quadraticCurveTo(-s, s, -s, s - r); shape.lineTo(-s, -s + r);
  shape.quadraticCurveTo(-s, -s, -s + r, -s);
  const geo = new THREE.ShapeGeometry(shape); const uv = geo.attributes.uv; const pos = geo.attributes.position;
  for (let i = 0; i < uv.count; i++) uv.setXY(i, (pos.getX(i) + s) / (2 * s), (pos.getY(i) + s) / (2 * s));
  const g = new THREE.Group();
  const board = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map: tex })); board.position.z = -0.6; g.add(board);
  const grid = new THREE.GridHelper(256, 8, 0x8a6c37, 0x8a6c37); grid.rotation.x = Math.PI / 2; grid.position.z = -0.5;
  grid.material.transparent = true; grid.material.opacity = 0.35; g.add(grid);
  scene.add(g); return g;           // 物件を開いていないときも、空の造形板を出しておく
})();
const camera = new THREE.PerspectiveCamera(30, 1, 1, 10000); camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, renderer.domElement);
function resize() {
  const w = holder.clientWidth, h = holder.clientHeight;
  if (!w || !h) return;                                   // 平面図を出している間（3D が隠れている）は合わせない
  renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);
new ResizeObserver(resize).observe(holder);   // 右の欄の幅が変わったときも、3D の大きさを合わせ直す

// カメラを動かす（視点の切り替え・部材へ寄る）。人が回し始めたら止める。動きを減らす設定ならすぐに動かす
let tween = null;
const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function flyTo(pos, target, ms = 360) {
  if (!ms || reduceMotion()) { tween = null; camera.position.copy(pos); controls.target.copy(target); controls.update(); return; }
  tween = { p0: camera.position.clone(), t0: controls.target.clone(), p1: pos.clone(), t1: target.clone(), start: performance.now(), ms };
}
function stepTween(now) {
  if (!tween) return;
  const k = Math.min(1, (now - tween.start) / tween.ms); const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
  camera.position.lerpVectors(tween.p0, tween.p1, e); controls.target.lerpVectors(tween.t0, tween.t1, e);
  if (k >= 1) tween = null;
}
controls.addEventListener('start', () => { tween = null; state.orbiting = true; });
controls.addEventListener('end', () => { state.orbiting = false; });
function loop(now) {               // 描く（始めるのはファイルのいちばん下。向きの印などを用意してから）
  requestAnimationFrame(loop);
  if (document.hidden || state.pane !== '3d') return;
  stepTween(now ?? performance.now()); controls.update(); renderer.render(scene, camera);
  updateGizmo(); updateScalebar(); updateTags();
}

// ---- 右上の向きの印：東西南北と上。押すと、その向きから見る（真ん中の点は斜め）
const gizmo = $('#gizmo');
const GZ = [['N', '北', [0, 1, 0], 'back', '北から見る'], ['S', '南', [0, -1, 0], 'front', '南から見る（正面）'],
  ['E', '東', [1, 0, 0], 'east', '東から見る'], ['W', '西', [-1, 0, 0], 'side', '西から見る（横）'], ['U', '上', [0, 0, 1], 'top', '上から見る']];
gizmo.innerHTML = '<circle class="ring" r="45"/>' + GZ.map(([k]) => `<line class="ax" data-k="${k}" x1="0" y1="0" x2="0" y2="0"/>`).join('')
  + '<circle class="home" r="4" data-view="iso"><title>斜めから見る</title></circle>'
  + GZ.map(([k, l, , v, t]) => `<g class="k" data-k="${k}" data-view="${v}" tabindex="0" role="button" aria-label="${t}"><title>${t}</title><circle r="9.5"/><text>${l}</text></g>`).join('');
let gizmoKey = '';
function updateGizmo() {
  const q = camera.quaternion; const key = `${q.x.toFixed(3)},${q.y.toFixed(3)},${q.z.toFixed(3)},${q.w.toFixed(3)}`;
  if (key === gizmoKey) return; gizmoKey = key;
  const inv = q.clone().invert();
  const items = GZ.map(([k, , d]) => { const v = new THREE.Vector3(...d).applyQuaternion(inv); return { k, x: v.x * 33, y: -v.y * 33, z: v.z }; });
  for (const it of items) {
    const ln = gizmo.querySelector(`line[data-k="${it.k}"]`);
    ln.setAttribute('x2', it.x.toFixed(1)); ln.setAttribute('y2', it.y.toFixed(1));
    ln.classList.toggle('back', it.z < -0.05); ln.classList.toggle('up', it.k === 'U');
    const g = gizmo.querySelector(`g[data-k="${it.k}"]`);
    g.setAttribute('transform', `translate(${it.x.toFixed(1)} ${it.y.toFixed(1)})`);
    g.classList.toggle('back', it.z < -0.05); g.classList.toggle('up', it.k === 'U');
  }
  items.sort((a, b) => a.z - b.z).forEach((it) => gizmo.appendChild(gizmo.querySelector(`g[data-k="${it.k}"]`)));   // 手前の字を上に
}
gizmo.addEventListener('click', (e) => { const k = e.target.closest('[data-view]'); if (k) setView(k.dataset.view); });
gizmo.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target.dataset.view) { e.preventDefault(); setView(e.target.dataset.view); } });

// ---- 左下の縮尺の目盛（模型の mm）。カメラが見ている所（回す中心）の大きさで測る
let scaleKey = '';
function updateScalebar() {
  if (!state.root) return;
  const h = holder.clientHeight || 1;
  const mmPerPx = 2 * camera.position.distanceTo(controls.target) * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) / h;
  const nice = [1, 2, 5, 10, 20, 50, 100, 200].filter((n) => n / mmPerPx <= 150).pop() || 1;
  const px = Math.round(nice / mmPerPx);
  const key = `${nice}|${px}`; if (key === scaleKey) return; scaleKey = key;
  const real = nice * MODEL_SCALE;
  $('#scalebar').innerHTML = `<div style="width:${px}px"><div class="bar"><i></i><i></i><i></i><i></i></div>`
    + `<div class="lbl"><span>0</span><span>${nice} mm</span></div></div><div class="real">実寸 ${real >= 1000 ? `${real / 1000} m` : `${real} mm`}</div>`;
}

const LIFT = { foundation: 0, floor1: 1, exterior1: 1, roof: 3, roof_ext: 3 };
// 2階建て（基礎・1階・下屋の屋根・2階・大屋根の5段）
const LIFT_MULTI = { foundation: 0, floor1: 1, exterior1: 1, roof_lower: 2, roof_lower_ext: 2, floor2: 3, exterior2: 3, roof: 4, roof_ext: 4 };
// 部品の表示の切り替え：[パーツの名前（空白区切り）, 一覧の名前, 説明]。1つの切り替えで複数のパーツ（屋根＋屋根の外壁）
const SINGLE_PARTS = [['foundation', '基礎', '基礎'], ['floor1', '1階（床・内壁）', '1階（床・内壁）'], ['exterior1', '外壁', '外壁'],
  ['roof roof_ext', '屋根', '屋根（天井より上の壁・ポーチの天井を含む）']];
const MULTI_PARTS = [['foundation', '基礎', '基礎'], ['floor1 exterior1', '1階', '1階（床・内壁・外壁）'], ['roof_lower roof_lower_ext', '下屋の屋根', '下屋の屋根'],
  ['floor2 exterior2', '2階', '2階（床・内壁・外壁）'], ['roof roof_ext', '大屋根', '大屋根']];
const partGroups = () => (state.multi ? MULTI_PARTS : SINGLE_PARTS);
function applyVisibility() {
  if (!state.root) return;
  const lift = state.multi ? LIFT_MULTI : LIFT;
  state.root.traverse((o) => {
    if (!o.isMesh) return;
    const g = partGroups().find(([k]) => k.split(' ').includes(o.name));
    o.visible = g ? !state.hidden.has(g[0]) : true;
    o.position.z = state.exploded ? (lift[o.name] || 0) * 60 : 0;
  });
}

const VIEW_DIRS = { iso: [-0.9, -1.45, 1.0], front: [0, -2.0, 0.2], side: [-2.0, 0, 0.2], top: [0, -0.001, 2.1], back: [0, 2.0, 0.2], east: [2.0, 0, 0.2] };
function setView(kind, ms = 300) {
  if (!state.root) { framePlate(); return; }
  const box = new THREE.Box3().setFromObject(state.root); const c = box.getCenter(new THREE.Vector3());
  const r = box.getSize(new THREE.Vector3()).length();
  const pos = VIEW_DIRS[kind] || VIEW_DIRS.iso;
  flyTo(new THREE.Vector3(c.x + pos[0] * r, c.y + pos[1] * r, c.z + pos[2] * r), c, ms);
}
function framePlate() {             // 物件を開いていないとき：空の造形板を斜めから
  plate.position.set(0, 0, 0);
  const r = 256 * 1.75; const d = VIEW_DIRS.iso;
  flyTo(new THREE.Vector3(d[0] * r, d[1] * r, d[2] * r), new THREE.Vector3(0, 0, 0), 0);
}
framePlate();
function setExploded(on) {
  state.exploded = on;
  const b = $('#explode'); b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.querySelector('span').textContent = on ? 'パーツを戻す' : 'パーツを離す';
  applyVisibility();
  // ★11/5 夜：離したままでも押して選べる・囲める（当たり判定の形も部品ごとにずらす。pick3d.js）。
  // 選んだもの・囲んだものはそのまま残し、光らせた印を新しい位置で描き直す
  if (state.focus) reapplyFocus();
  else if (state.hl && state.hlRows) highlight3d(state.hlRows);
}
document.querySelectorAll('button[data-view]').forEach((b) => b.addEventListener('click', () => {
  if (b.dataset.view === 'explode') { if (state.root) setExploded(!state.exploded); return; }
  if (state.pane !== '3d') setPane('3d');
  setView(b.dataset.view);
}));

async function loadModel() {
  const buf = await (await fetch(api('/api/model/{a}/model.glb') + '?t=' + Date.now())).arrayBuffer();
  const gltf = await new GLTFLoader().parseAsync(buf, '');
  if (state.root) scene.remove(state.root);
  state.root = gltf.scene;
  state.root.traverse((o) => {
    if (!o.isMesh) return;
    o.geometry.computeVertexNormals(); o.material.flatShading = true;
    if (PART_COLORS[o.name] !== undefined) o.material.color.setHex(PART_COLORS[o.name]);   // 樹脂の灰色（書き出しの GLB の色は変えない）
    o.material.needsUpdate = true;
  });
  const box = new THREE.Box3().setFromObject(state.root); const mid = box.getCenter(new THREE.Vector3());
  state.modelSize = box.getSize(new THREE.Vector3());
  plate.position.set(mid.x, mid.y, box.min.z); plate.visible = true;
  scene.add(state.root); resize(); applyVisibility();
  if (!state.viewSet) { setView('iso', 0); state.viewSet = true; }
  ensurePick().catch(() => {});      // 当たり判定の形を先に読んでおく（押したらすぐ選べるように）
}

// ---------------------------------------------------------------- 表で直す
function num(v) { return Math.round(v * 10) / 10; }

function editableRow(cells, onApprove) {
  // cells: [{text}] または [{field, value}]（入力欄）。値を変えると「承認」の行が出る
  const tr = document.createElement('tr');
  tr.dataset.id = cells[0].text;                   // 部材の番号（囲んで直す・選んだ部材で行に色を付ける）
  const inputs = [];
  for (const c of cells) {
    const td = document.createElement('td');
    if (c.field && c.options) {                     // 選ぶ欄（例：屋根の低い側）
      const sel = document.createElement('select');
      sel.innerHTML = c.options.map(([v, label]) => `<option value="${v}">${label}</option>`).join('');
      sel.value = c.value; sel.dataset.field = c.field; sel.dataset.orig = c.value; inputs.push(sel); td.appendChild(sel);
    } else if (c.field) {
      const inp = document.createElement('input'); inp.type = 'number'; inp.step = c.step || '1'; inp.value = c.step ? c.value : num(c.value);
      inp.dataset.field = c.field; inp.dataset.orig = inp.value; inputs.push(inp); td.appendChild(inp);
    } else td.textContent = c.text;
    tr.appendChild(td);
  }
  if (isElementId(cells[0].text)) {                 // 番号を押すと、模型でその部材を光らせる
    tr.firstChild.title = `${cells[0].text} を模型で光らせる`;
    tr.firstChild.addEventListener('click', () => selectElement(cells[0].text));
  }
  const approve = document.createElement('tr'); approve.className = 'approve'; approve.hidden = true;
  approve.innerHTML = `<td colspan="${cells.length}"><div class="approve-box"><input type="text" placeholder="直す理由（例：立面図の窓の上端に合わせる）"> <button class="primary">承認</button> <button>やめる</button></div></td>`;
  const [reason, ok, cancel] = approve.querySelectorAll('input, button');
  const changed = () => inputs.filter((i) => (i.tagName === 'SELECT' ? i.value !== i.dataset.orig : Number(i.value) !== Number(i.dataset.orig)));
  inputs.forEach((i) => i.addEventListener(i.tagName === 'SELECT' ? 'change' : 'input', () => { const ch = changed().length > 0; tr.classList.toggle('changed', ch); approve.hidden = !ch; }));
  cancel.addEventListener('click', () => { inputs.forEach((i) => { i.value = i.dataset.orig; }); tr.classList.remove('changed'); approve.hidden = true; });
  ok.addEventListener('click', () => onApprove(changed(), reason.value));
  return [tr, approve];
}

async function approveChanges(target, inputs, reason, reselect = null) {
  try {
    setStatus('修正を記録して、模型を組み直しています…', 'busy');
    for (const i of inputs) await call('POST', '/api/overrides/{a}', { target, field: i.dataset.field, to: i.tagName === 'SELECT' ? i.value : Number(i.value), reason });
    await refresh(); setStatus(`${target} を直しました（直しの履歴から取り消せます）`, 'ok');
    if (reselect) await selectElement(reselect, { frame: false, quiet: true });   // 右の欄で直したときは、同じ部材を選び直す
  } catch (e) { setStatus('直せませんでした：' + e.message, 'err'); }
}

async function approveLevels(inputs, reason, fl1) {
  // 階高は「2階の床の高さ − 1階の床の高さ」。記録は 2階の床の高さ（fl_mm）で残す
  try {
    setStatus('階の高さを直して、上のもの（壁・窓・屋根）をずらしています…', 'busy');
    let base = fl1;
    for (const i of inputs) {
      const v = Number(i.value);
      if (i.dataset.field === 'story_mm') {
        await call('POST', '/api/overrides/{a}', { target: 'level:2F', field: 'fl_mm', to: base + v, reason });
      } else {
        await call('POST', '/api/overrides/{a}', { target: i.dataset.target, field: i.dataset.field, to: v, reason });
        if (i.dataset.target === 'level:1F' && i.dataset.field === 'fl_mm') base = v;
      }
    }
    await refresh(); setStatus('階の高さを直しました（直しの履歴から取り消せます）', 'ok');
  } catch (e) { setStatus('直せませんでした：' + e.message, 'err'); }
}

function renderLevels(b) {
  const lt = $('#levels'); if (!lt) return;
  const lv = b.levels || [];
  lt.innerHTML = '<tr><th>階</th><th>床の高さ（GL から）</th><th>階高</th><th>天井の高さ（床から）</th></tr>';
  lv.forEach((l, k) => {
    const next = lv[k + 1];
    const cells = [{ text: LEVEL_JA[l.name] || l.name },
      k === 0 ? { field: 'fl_mm', value: l.fl_mm } : { text: l.fl_mm },
      next ? { field: 'story_mm', value: next.fl_mm - l.fl_mm } : { text: '' },
      { field: 'ceiling_mm', value: l.ceiling_mm }];
    const rows = editableRow(cells, (inputs, reason) => approveLevels(inputs, reason, lv[0].fl_mm));
    rows[0].querySelectorAll('input').forEach((i) => { i.dataset.target = `level:${l.name}`; });
    rows.forEach((r) => lt.appendChild(r));
  });
}

function deleteWall(w) {
  const reason = prompt(`${w.id} を取り除きます。理由を書いてください`); if (reason === null) return;
  (async () => {
    try { setStatus('壁を取り除いて組み直しています…', 'busy'); await call('POST', '/api/overrides/{a}', { target: w.id, field: '_delete', reason }); await refresh(); setStatus(`${w.id} を取り除きました`, 'ok'); }
    catch (e) { setStatus('取り除けませんでした：' + e.message, 'err'); }
  })();
}

// 屋根の表の欄（表と右の欄で同じものを使う）
const roofPitchCell = (r) => (r.top_plane && r.pitch ? { field: 'pitch', value: r.pitch[0], step: '0.1' } : { text: (r.pitch || []).join('/') });
const roofIsShed = (r) => !(r.planes && r.planes.length > 1) && r.low_side && SIDE[r.low_side];

function renderTables(b) {
  renderLevels(b);
  // 2階建てのときだけ「階」の列を足す（平屋の表は今までどおり）
  const lvHead = state.multi ? '<th>階</th>' : '';
  const lvCell = (x) => (state.multi ? [{ text: x.level || '' }] : []);
  const ot = $('#openings'); ot.innerHTML = `<tr><th>番号</th>${lvHead}<th>種類</th><th>壁</th><th>幅</th><th>下端 FL+</th><th>上端 FL+</th></tr>`;
  for (const o of b.openings || []) {
    const rows = editableRow([{ text: o.id }, ...lvCell(o), { text: KIND[o.kind] || o.kind }, { text: o.wall },
      { field: 'width_mm', value: o.width_mm }, { field: 'sill_fl_mm', value: o.sill_fl_mm }, { field: 'head_fl_mm', value: o.head_fl_mm }],
      (inputs, reason) => approveChanges(o.id, inputs, reason));
    rows.forEach((r) => ot.appendChild(r));
  }
  const wt = $('#walls'); wt.innerHTML = `<tr><th>番号</th>${lvHead}<th>種類</th><th>厚み</th><th>下端</th><th>高さ</th><th></th></tr>`;
  for (const w of b.walls || []) {
    const rows = editableRow([{ text: w.id }, ...lvCell(w), { text: KIND[w.kind] || w.kind }, { field: 'thickness_mm', value: w.thickness_mm },
      { text: w.bottom_mm }, { text: w.height === 'to_roof' ? '屋根まで' : w.height }, { text: '' }],
      (inputs, reason) => approveChanges(w.id, inputs, reason));
    const del = document.createElement('button'); del.textContent = '取り除く';
    del.addEventListener('click', () => deleteWall(w));
    rows[0].lastChild.appendChild(del);
    rows.forEach((r) => wt.appendChild(r));
  }
  // 屋根：勾配（10 に対する値）・低い側（片流れだけ）・軒先の高さ・軒の出（西・東・南・北）を直せる。面の式はサーバーが計算し直す
  // 表は2つ：屋根の形（勾配・低い側・軒先の高さ）と軒の出（西・東・南・北）。1つにすると右の欄からはみ出すため
  const rt = $('#roofs'), et = $('#roof-eaves');
  rt.innerHTML = `<tr><th>番号</th>${lvHead}<th>種類</th><th>勾配 /10</th><th>低い側</th><th>軒先の高さ</th></tr>`;
  const soffitCol = (b.roofs || []).some((r) => r.soffit_z_mm != null);   // 軒天がある屋根（glb に軒天の部材）のときだけ列を出す
  et.innerHTML = `<tr><th>番号</th>${[...'WESN'].map((s) => `<th>${SIDE[s]}</th>`).join('')}${soffitCol ? '<th>軒天の高さ</th>' : ''}</tr>`;
  for (const r of b.roofs || []) {
    const low = roofIsShed(r) ? { field: 'low_side', value: r.low_side, options: [...'SNEW'].map((s) => [s, SIDE[s]]) } : { text: '' };
    const tip = r.top_z_range_mm ? { field: 'eave_tip_mm', value: r.top_z_range_mm[0] } : { text: '' };
    editableRow([{ text: r.id }, ...lvCell(r), { text: roofKind(r) }, roofPitchCell(r), low, tip],
      (inputs, reason) => approveChanges(r.id, inputs, reason)).forEach((x) => rt.appendChild(x));
    const eaves = [...'WESN'].map((s) => (r.eave_mm && s in r.eave_mm ? { field: `eave_mm.${s}`, value: r.eave_mm[s] } : { text: '' }));
    const soffit = soffitCol ? [r.soffit_z_mm != null ? { field: 'soffit_z_mm', value: r.soffit_z_mm } : { text: '' }] : [];
    editableRow([{ text: r.id }, ...eaves, ...soffit], (inputs, reason) => approveChanges(r.id, inputs, reason)).forEach((x) => et.appendChild(x));
  }
}

async function renderHistory(results) {
  const items = await call('GET', '/api/overrides/{a}');
  state.overrides = items;
  const ul = $('#history'); ul.innerHTML = items.length ? '' : '<li class="muted">まだ修正はありません</li>';
  const status = Object.fromEntries((results || []).map((r) => [r.id, r]));
  const label = { applied: '当てた', applied_changed: '当てた（自動の値が変わっていた）', failed: '当てられない' };
  for (const it of items.slice().reverse()) {
    const li = document.createElement('li'); const st = status[it.id];
    const what = it.field === '_delete' ? '取り除く' : `${FIELD[it.field] || it.field}：${valText(it.from)} → ${valText(it.to)}`;
    const t = String(it.target);
    const tgt = t.startsWith('level:') ? `<b>${esc(LEVEL_JA[t.slice(6)] || t)}</b> ` : isElementId(t) ? `<button class="idc" data-id="${esc(t)}" title="${esc(t)} を模型で光らせる">${esc(t)}</button>` : `<b>${esc(t)}</b> `;
    li.innerHTML = `<div class="what"><span class="tag">${esc(it.id)}</span>${tgt}${esc(what)}</div>`
      + `<div class="why">${esc(it.reason)}（${esc(sourceText(it.value_source))}・${esc(String(it.at || '').slice(0, 16).replace('T', ' '))}）${st ? '・' + esc(label[st.status] || st.status) : ''}</div>`;
    const undo = document.createElement('button'); undo.textContent = '取り消す'; undo.className = 'quiet';
    undo.addEventListener('click', () => undoOverride(it));
    li.appendChild(undo); ul.appendChild(li);
  }
  $('#hist-count').textContent = items.length ? String(items.length) : '';
  setTabCount('fix', items.length);
  $('#undo-last').disabled = !items.length;
  const last = items.map((x) => x.at).filter(Boolean).sort().pop();
  if (last && !state.savedFromServer) state.savedAt = last;          // 保存の時刻（窓口がないサーバーのときの代わり）
}
async function undoOverride(it) {
  try { setStatus('取り消して組み直しています…', 'busy'); await call('DELETE', `/api/overrides/{a}/${it.id}`); await refresh(); setStatus(`${it.id} を取り消しました`, 'ok'); }
  catch (e) { setStatus('取り消せませんでした：' + e.message, 'err'); }
}
$('#undo-last').addEventListener('click', () => {   // 道具の帯の「取り消す」：いちばん新しい直し
  const it = state.overrides[state.overrides.length - 1]; if (!it) return;
  const what = it.field === '_delete' ? '取り除く' : `${FIELD[it.field] || it.field} ${valText(it.from)} → ${valText(it.to)}`;
  if (!confirm(`いちばん新しい直し（${it.id}：${it.target} の ${what}）を取り消します。よろしいですか？`)) return;
  undoOverride(it);
});
$('#fix-table').addEventListener('click', () => { const s = $('#sec-tables'); s.open = true; s.scrollIntoView({ block: 'start', behavior: 'smooth' }); });
$('#fix-history').addEventListener('click', () => { const s = $('#sec-history'); s.open = true; s.scrollIntoView({ block: 'start', behavior: 'smooth' }); });

// checks.json（書き出しの検査・部品・分け目）。検査・書き出し・下の帯・模型の欄が使う
async function reloadChecks() {   // ファイルなのでブラウザが古いものを使わないように、毎回ちがう URL で読む
  try { state.checks = await call('GET', `/api/model/{a}/checks.json?t=${Date.now()}`); } catch (e) { state.checks = null; }
}

let checksSeq = 0;   // 描き直しが重なったら、最後に始めたものだけを描く（前は2回分が足され、前の物件の結果も混ざった）
async function renderChecks() {
  const my = ++checksSeq;
  const ul = $('#checks'); ul.innerHTML = '';
  const add = (html) => { if (my === checksSeq) ul.insertAdjacentHTML('beforeend', html); };
  try {   // 予防：会社ルールと比べた結果（家ごとに変わる値は比べない）
    const rc = await call('GET', '/api/rulecheck/{a}');
    add('<li class="lv-情報"><b>会社ルールと比べた結果</b>（家ごとに変わる窓の高さ・屋根の勾配などは比べません）</li>');
    for (const r of rc.rows) {
      add(r.match
        ? `<li class="lv-情報"><span class="tag">一致</span>${esc(r.label)}：会社ルール ${esc(r.rule_text)}</li>`
        : `<li class="lv-中"><span class="tag">いつもと違う</span>${esc(r.message)}</li>`);
    }
  } catch (e) { /* 組み立て前は出さない */ }
  try {   // 図面（jww）と比べた結果：平面図の窓・テラスドアの注記（品番と上端）と模型の開口
    const dw = await call('GET', '/api/drawing/{a}');
    if (dw.available) {
      add('<li class="lv-情報"><b>図面と比べた結果</b>（平面図の窓・テラスドアの注記の上端 FL+ と品番の幅・高さ、立面図の窓の位置を、模型の窓と比べる）</li>');
      for (const r of dw.levels) {
        const name = LEVEL_JA[r.level] || r.level;
        if (!r.aligned) { add(`<li class="lv-中"><span class="tag">${esc(name)}</span>${esc(r.note)}</li>`); continue; }
        add(`<li class="lv-情報"><span class="tag">${esc(name)}</span>注記 ${r.notes} 件のうち ${r.matched} 件を開口に結び、${r.same} 件が一致（上端・幅・高さ）`
          + (r.unmatched ? `。近くに開口がない注記 ${r.unmatched} 件` : '') + '</li>');
        for (const m of r.mismatches) add(`<li class="lv-中"><span class="tag">図面と違う</span>${esc(m.message)}</li>`);
        for (const n of r.unmatched_notes) add(`<li class="lv-低"><span class="tag">要確認</span>図面に注記（品番 ${esc(n.code)}、FL+${n.head_fl_mm}）があるが、近くに模型の開口がない</li>`);
      }
      const VJ = { S: '南', N: '北', E: '東', W: '西' };
      for (const r of dw.elevations || []) {   // 立面図：面ごとに位置を合わせ、見えている窓・ドアの4辺に図面の線があるか
        const name = `${VJ[r.view]}の立面`;
        if (!r.aligned) { add(`<li class="lv-情報"><span class="tag">${name}</span>立面図を位置合わせできなかったので、照合していません</li>`); continue; }
        if (r.note) { add(`<li class="lv-情報"><span class="tag">${name}</span>${esc(r.note)}</li>`); continue; }
        add(`<li class="lv-情報"><span class="tag">${name}</span>見えている窓・ドア ${r.windows} か所のうち ${r.same} か所が立面図と合う</li>`);
        for (const m of r.mismatches) add(`<li class="lv-中"><span class="tag">図面と違う</span>${esc(m.opening)} の${esc(m.edges.join('・'))}が立面図の線と合わない</li>`);
      }
    }
  } catch (e) { /* 図面がない・読めないときは出さない */ }
  const s = state.checks;
  if (s) {
    add('<li class="lv-情報"><b>部品ごとのメモ</b>（組み立てで決めたこと）</li>');
    for (const [, p] of Object.entries(s.parts || {})) for (const n of p.notes || []) add(`<li class="lv-情報"><span class="tag">${esc(p.label)}</span>${esc(n)}</li>`);
    add('<li class="lv-情報"><b>検査の結果</b>（閉じた立体か・大きさ・造形範囲に入るか など）</li>');
    for (const c of s.checks || []) add(`<li class="lv-${esc(c.level)}"><span class="tag">${esc(c.level)}</span>${esc(c.message)}</li>`);
  } else if (my === checksSeq) ul.innerHTML = '<li class="muted">まだ検査していません</li>';
  if (my !== checksSeq) return;
  // 確かめる所（高・中）は、画面では一覧のいちばん上に集める（style.css の order）。タブにも数を出す
  const bad = ul.querySelectorAll('li.lv-高').length; const mid = ul.querySelectorAll('li.lv-中').length;
  setTabCount('print', bad + mid, bad ? 'bad' : 'warn');
  const nc = $('#n-checks'); nc.textContent = bad + mid ? String(bad + mid) : ''; nc.className = 'sec-count' + (bad ? ' bad' : '');
  if (bad + mid) ul.insertAdjacentHTML('afterbegin', `<li class="lv-情報 sum"><b>確かめる所 ${bad + mid} 件</b>（黄と赤の印の行を、ここに集めました）</li>`);
}

// 書き出した STL の一覧（checks.json の stl_files）から、ダウンロードの行を作る。分けた部品は print_pieces の名前（例：1階 1/2（南））
function stlRows(s) {
  const pieces = s.print_pieces || []; const parts = s.parts || {}; const stl = new Set(s.stl_files);
  const rows = []; const used = new Set();
  const isExt = (f) => /^exterior|_ext(_p\d+)?\.stl$/.test(f);
  const objects = [...new Set(Object.keys(parts).map((k) => parts[k].object || k))];
  for (const obj of objects) {
    const mine = pieces.filter((pc) => pc.object === obj);
    if (mine.length) {
      for (const pc of mine) for (const f of pc.files) {
        if (!stl.has(f) || used.has(f)) continue;
        rows.push([f, `STL：${pc.name}${pc.files.length > 1 && isExt(f) ? '（外壁）' : ''}`]); used.add(f);
      }
    }
    for (const k of Object.keys(parts).filter((x) => (parts[x].object || x) === obj)) {
      const f = `${k}.stl`;
      if (stl.has(f) && !used.has(f)) { rows.push([f, `STL：${parts[k].label}`]); used.add(f); }
    }
  }
  for (const f of s.stl_files) if (!used.has(f)) rows.push([f, `STL：${f}`]);   // 名前の分からない部品も落とさない
  return rows;
}

function renderDownloads() {
  const files = [['model_bambu.3mf', '3MF（Bambu Studio 形式・外壁のファジースキン設定済み）おすすめ'], ['model.3mf', '3MF（ふつうの形式・設定は手で）'], ['model.glb', 'GLB（3D 表示用）']];
  const s = state.checks;
  // 道具の帯の主のボタン：Bambu Studio 形式を作らない設定のときは、ふつうの 3MF
  const noBambu = s && s.bambu_3mf && s.bambu_3mf.ok === false;
  const main = $('#dl-main');
  main.href = api('/api/model/{a}/' + (noBambu ? 'model.3mf' : 'model_bambu.3mf'));
  $('#dl-main-label').textContent = noBambu ? '3MF を保存' : '3MF を保存（Bambu Studio 形式）';
  main.setAttribute('aria-disabled', s ? 'false' : 'true');
  if (s && Array.isArray(s.stl_files) && s.stl_files.length) {   // 書き出した STL をそのまま並べる（本体を分けたときも合う）
    const row = ([f, label], k) => `<li${k === 0 ? ' class="dl-main"' : ''}><a href="${api('/api/model/{a}/' + f)}" download>${esc(label)}</a></li>`;
    const stls = stlRows(s);   // 1つ目（おすすめの 3MF）を大きく、STL は「部品ごと」のまとまりに分けて並べる
    $('#downloads').innerHTML = files.map(row).join('')
      + `<li class="dl-group">部品ごとの STL（${stls.length} 個）</li>` + stls.map((x) => row(x, -1)).join('');
    return;
  }
  files.push(['foundation.stl', 'STL：基礎'], ['floor1.stl', 'STL：1階（床・内壁）'], ['exterior1.stl', 'STL：1階の外壁']);
  if (state.multi) files.push(['floor2.stl', 'STL：2階（床・内壁）'], ['exterior2.stl', 'STL：2階の外壁']);
  // 屋根は面ごとに分けて寝かせた STL（core/print_split.py）。分けていない書き出しなら前の名前
  const pieces = (s && s.print_pieces) || [];
  if (pieces.length) {
    for (const pc of pieces) for (const f of pc.files) files.push([f, `STL：${pc.name}${f.includes('_ext') ? '（外壁）' : ''}`]);
  } else if (state.multi) {
    files.push(['roof_lower.stl', 'STL：下屋の屋根'], ['roof_lower_ext.stl', 'STL：下屋の外壁'], ['roof.stl', 'STL：大屋根'], ['roof_ext.stl', 'STL：大屋根の外壁']);
  } else {
    files.push(['roof.stl', 'STL：屋根（屋根板・ポーチの天井）'], ['roof_ext.stl', 'STL：屋根の外壁']);
  }
  $('#downloads').innerHTML = files.map(([f, label], k) => `<li${k === 0 ? ' class="dl-main"' : ''}><a href="${api('/api/model/{a}/' + f)}" download>${esc(label)}</a></li>`).join('');
}

// 印刷する部品の数（分けた屋根・本体は分けた数）と、造形範囲に入るか（checks.json の build_volume。高があれば入らない）
function printSummary(s) {
  if (!s) return null;
  const objects = [...new Set(Object.entries(s.parts || {}).map(([k, p]) => p.object || k))];
  const pieces = s.print_pieces || [];
  const count = objects.reduce((n, obj) => n + (pieces.filter((pc) => pc.object === obj).length || 1), 0)
    || (s.stl_files || []).length;
  const bv = (s.checks || []).filter((c) => c.item === 'build_volume');
  const fits = bv.length ? bv.every((c) => c.level !== '高') : null;
  return { count, fits };
}

// ---------------------------------------------------------------- 模型の欄（模型タブ）
function renderModelInfo() {
  const box = $('#model-info');
  if (!state.alias) { box.innerHTML = '<p class="muted">物件を開くと、模型の大きさと部品が出ます。</p>'; return; }
  if (!state.built || !state.root) { box.innerHTML = '<p class="muted">まだ組み立てていません。道具の帯の「読み込んで組み立てる」を押してください。</p>'; return; }
  const sz = state.modelSize || new THREE.Vector3();
  const m = (v) => (v * MODEL_SCALE / 1000).toFixed(2);
  const ps = printSummary(state.checks);
  const rows = [['大きさ', `${sz.x.toFixed(1)} × ${sz.y.toFixed(1)} × ${sz.z.toFixed(1)} mm<span class="sub2">模型</span>`],
    ['実寸', `${m(sz.x)} × ${m(sz.y)} × ${m(sz.z)} m`],
    ['階', state.multi ? '2階建て' : '平屋'],
    ['会社ルール', esc(state.companies?.[companyOf()] || companyOf())]];
  if (ps) rows.push(['部品', `${ps.count} 個`], ['造形範囲', ps.fits == null ? '—' : ps.fits ? '<span class="fit-ok">どの部品も入る（256mm 角）</span>' : '<span class="fit-bad">入らない部品がある</span>']);
  let html = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
  const bv = (state.checks?.checks || []).filter((c) => c.item === 'build_volume');
  if (bv.length) {
    html += '<table class="parts-table"><tr><th>部品</th><th>大きさ（模型 mm）</th><th></th></tr>' + bv.map((c) => {
      const mm = /^(.*?)：大きさ ([\d.]+)×([\d.]+)×([\d.]+)mm/.exec(c.message || '');
      const name = mm ? mm[1] : (state.checks.parts?.[c.part]?.label || c.part);
      const dims = mm ? `${mm[2]} × ${mm[3]} × ${mm[4]}` : '';
      return `<tr><td>${esc(name)}</td><td>${dims}</td><td>${c.level === '高' ? '<span class="fit-bad" title="造形範囲に入らない">入らない</span>' : '<span class="fit-ok" title="造形範囲に入る">入る</span>'}</td></tr>`;
    }).join('') + '</table>';
  }
  box.innerHTML = html;
}

// ---------------------------------------------------------------- 検品（SPEC §9-2・§10）
const VIEW_OF = { S: 'S', N: 'N', E: 'E', W: 'W', plan: 'plan', plan2: 'plan2', print: 'plan' };
const VIEW_JA = { S: '南の面', N: '北の面', E: '東の面', W: '西の面', plan: '平面', plan2: '2階の平面', print: '印刷' };

const FIELD = { thickness_mm: '厚み', width_mm: '幅', offset_mm: '壁の端からの位置', sill_fl_mm: '下端 FL+', head_fl_mm: '上端 FL+',
  fl_mm: '床の高さ', ceiling_mm: '天井の高さ', pitch: '勾配（10 に対して）', low_side: '低い側', eave_tip_mm: '軒先の高さ', soffit_z_mm: '軒天の高さ',
  lintel_band_mm: '窓の上の帯（実寸 mm）', height: '上端の高さ' };
const valText = (v) => (v === 'to_roof' ? '屋根まで' : v);   // 壁の高さ「屋根まで」（★11/6 直す案で壁の height を直せるようにした）
const LEVEL_JA = { '1F': '1階', '2F': '2階' };
for (const s of 'WESN') FIELD[`eave_mm.${s}`] = `軒の出（${SIDE[s]}）`;

function sourceText(vs) {   // 数値の出どころを日本語にする（例：majority:exterior → 外壁の多数派）
  const s = String(vs ?? '');
  if (s.startsWith('majority:')) return `${KIND[s.slice(9)] || s.slice(9)}の多数派`;
  if (s.startsWith('rule:')) return `会社ルール（${s.slice(5)}）`;
  if (s.startsWith('human:number:')) return `人が書いた数値 ${s.slice(13)}`;
  if (s === 'human:table') return '表で直した';
  if (s === 'glb') return '元の形（glb）';
  if (s === 'to_roof') return '屋根まで（屋根の下の面）';
  return s;
}

function proposalText(p) {
  if (!p) return '';
  if (p.field === '_delete') return `${esc(p.target)} を取り除く`;
  return `${esc(p.target)} の ${esc(FIELD[p.field] || p.field)}：${esc(valText(p.from))} → <b>${esc(valText(p.to))}</b>（数値の出どころ：${esc(sourceText(p.value_source))}）`;
}
function proposalLine(p) {     // カードの1行：何を・いくつから・いくつへ
  if (!p) return '';
  if (p.field === '_delete') return `<b>${esc(p.target)}</b> を取り除く`;
  return `${esc(p.target)} の${esc(FIELD[p.field] || p.field)}　${esc(valText(p.from))}<span class="arrow">→</span><b>${esc(valText(p.to))}</b>`;
}
// 見出し：title（20字ほど）。古い指摘は本文の最初の「。」「：」「、」まで、なければ 30 字ほど
function headline(f) {
  if (f.title) return String(f.title);
  const m = String(f.message || '');
  const cut = m.search(/[。：、]/);
  let t = cut > 0 ? m.slice(0, cut) : m;
  if (t.length > 30) t = t.slice(0, 29) + '…';
  return t || String(f.id || '');
}
const evidenceText = (ev) => (ev || []).map((e) => (e && typeof e === 'object' ? (e.ref || e.text || e.type || '') : e)).filter(Boolean).map(esc).join('、');

// ---- 指摘の状態でボタンを変える（★11/5 夜 作者「却下ボタンしかない」「承認したときの反応を」「承認しても画面を移らない」）
// 未確認：承認・却下・あとで（直す案があれば承認＝直す。なければ Claude に直す案を頼む）／承認：「承認済み ✓」と取り消す／
// 却下・あとで・依頼済み：その札と戻す。押した瞬間にボタンを押せなくし、終わったらお知らせ（toast）。タブは替えない
const DONE_ST = ['承認', '却下', '依頼済み'];     // 決めた指摘（タブの数に入れない）。「あとで」はまだ決めていないほうに数える
const ST_LABEL = { 承認: '承認済み ✓', 却下: '却下済み', あとで: 'あとで', 依頼済み: '直す案を頼みました（修正タブの「頼んだこと」）' };
const fixing = { busy: false };                 // 直している（組み直している）間は、ほかの承認・取り消しを受けない（組み直しを重ねない）
const cardSel = (fid) => `.cards .finding[data-fid="${CSS.escape(String(fid))}"]`;
const NO_REQUEST = 'この版では使えません（直す案を Claude に頼む仕組みが、まだこの版に入っていません）';

function lockCard(card, b, text) {   // 押した瞬間：カードのボタンを押せなくし、押したボタンを「…しています」にする
  if (!card) return;
  $$('.f-acts button:not(.where)', card).forEach((x) => { x.disabled = true; });
  if (b) { b.dataset.label = b.textContent; b.textContent = text; b.classList.add('busy'); }
}
function unlockCard(card) {
  if (!card) return;
  $$('.f-acts button', card).forEach((x) => {
    x.disabled = false; x.classList.remove('busy');
    if (x.dataset.label) { x.textContent = x.dataset.label; delete x.dataset.label; }
  });
}
// 読み直しても、押したカード（質問）を同じ高さに見せる（読み直すと右の欄の上の「選んだ部材」が消えたり出たりして、
// カードが動いて見えなくなっていた）。上に「選んだ部材」が重なるときは、その下に出す
async function keepPlace(sel, fn) {
  const insp = $('.inspector'); const top = document.querySelector(sel)?.getBoundingClientRect().top;
  const out = await fn();
  const el = document.querySelector(sel);
  if (insp && el && top != null && el.offsetParent) {
    insp.scrollTop += el.getBoundingClientRect().top - top;
    const panel = $('#sel-panel');
    if (!panel.hidden) { const over = panel.getBoundingClientRect().bottom - el.getBoundingClientRect().top; if (over > 0) insp.scrollTop -= over + 8; }
  }
  return out;
}
function fixedText(p) {   // 直したことの一言（お知らせ）。例：O7（上端 FL+）を 2400 → 2300 に直しました
  if (!p) return '直しました';
  if (p.field === '_delete') return `${p.target} を取り除きました`;
  return `${p.target}（${FIELD[p.field] || p.field}）を ${valText(p.from)} → ${valText(p.to)} に直しました`;
}
async function relight(f, p) {   // 直したあと：直した部材と指摘の部材を光らせ直す（読み直すと印が消えるため。カメラは動かさない）
  const ids = [...new Set([p && p.field !== '_delete' ? String(p.target) : null, ...(f.targets || []).map(String)].filter(Boolean))]
    .filter((id) => findElement(id));
  if (!ids.length) return;
  state.focus = { ids, primary: ids[0], label: f.id, fid: f.id };
  renderInspector(); revealInTree(ids[0]);
  await applyFocus({ frame: false, quiet: true });
}

async function approveFinding(f, b) {   // 直す案のある指摘の承認：修正として記録し、組み直して、直した部材を光らせる
  if (fixing.busy) { toast('いま直しています。終わってから押してください', { kind: 'info' }); return; }
  fixing.busy = true;
  const card = b?.closest('.finding'); lockCard(card, b, '直しています…');
  setStatus(`${f.id} を直しています（修正として記録し、模型を組み直しています）…`, 'busy');
  try {
    const r = await quietly(() => keepPlace(cardSel(f.id), async () => {
      const res = await call('POST', `/api/findings/{a}/${encodeURIComponent(f.id)}`, { action: 'approve' });
      if (res.already) { await renderFindings(); return res; }
      await refresh();                     // 模型・平面図・表・指摘を読み直す（タブは替えない）
      await relight(f, f.proposal);
      return res;
    }));
    const oid = r.finding?.override;
    if (r.already) { toast(`${f.id} はもう承認してあります${oid ? `（直し ${oid}）` : ''}`, { kind: 'info' }); setStatus('', ''); return; }
    toast(fixedText(f.proposal), { kind: 'ok', action: oid ? { label: '取り消す', fn: () => undoFindingFix(f, oid) } : null });
    setStatus(`${f.id}：${fixedText(f.proposal)}（直しの履歴から取り消せます）`, 'ok');
  } catch (e) {
    toast('直せませんでした：' + e.message, { kind: 'err' }); setStatus('直せませんでした：' + e.message, 'err'); unlockCard(card);
  } finally { fixing.busy = false; }
}

async function undoFindingFix(f, oid, b) {   // 承認した直しを取り消す（指摘はサーバーが「未確認」に戻す）
  if (fixing.busy) { toast('いま直しています。終わってから押してください', { kind: 'info' }); return; }
  fixing.busy = true;
  const card = b?.closest('.finding') || document.querySelector(cardSel(f.id)); lockCard(card, b, '取り消しています…');
  setStatus(`${oid} を取り消して、模型を組み直しています…`, 'busy');
  try {
    await quietly(() => keepPlace(cardSel(f.id), async () => {
      await call('DELETE', `/api/overrides/{a}/${encodeURIComponent(oid)}`); await refresh();
    }));
    toast(`${f.id} の直し（${oid}）を取り消しました。指摘は「未確認」に戻りました`, { kind: 'ok' });
    setStatus(`${oid} を取り消しました`, 'ok');
  } catch (e) {
    toast('取り消せませんでした：' + e.message, { kind: 'err' }); setStatus('取り消せませんでした：' + e.message, 'err'); unlockCard(card);
  } finally { fixing.busy = false; }
}

async function decideQuiet(f, action, b) {   // 却下・あとで・戻す（模型は変わらない）
  const card = b?.closest('.finding') || document.querySelector(cardSel(f.id)); lockCard(card, b, '記録しています…');
  try {
    await quietly(() => keepPlace(cardSel(f.id), async () => {
      await call('POST', `/api/findings/{a}/${encodeURIComponent(f.id)}`, { action }); await renderFindings();
    }));
    const msg = { reject: `${f.id} を却下しました`, later: `${f.id} を「あとで」にしました`, reset: `${f.id} を「未確認」に戻しました` }[action];
    if (msg) toast(msg, { kind: 'ok', action: action === 'reset' ? null : { label: '戻す', fn: () => decideQuiet(f, 'reset') } });
  } catch (e) { toast('記録できませんでした：' + e.message, { kind: 'err' }); unlockCard(card); }
}

// 直す案を Claude に頼むときのつなぎ方：修正タブの「囲んで直す」で選んでいるもの（window.createFixRequest も同じものを使う想定）
function fixConnection() {
  const sel = $('#lasso-conn');
  if (sel && sel.value) return sel.value;
  return (state.settings?.connections || []).includes('claude_code') && state.mode !== 'other' ? 'claude_code' : 'api_key';
}
async function confirmFixCost(what) {   // 費用の確かめ（検品・囲んで直すの確かめの窓の文に合わせる）。what：「この指摘のとおりに直す案を」
  if (fixConnection() === 'api_key') {
    let st = state.settings;
    try { st = await call('GET', '/api/settings'); } catch (e) { /* 前に読んだ設定で確かめる */ }
    if (!st?.api_key_set) { toast('API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください', { kind: 'err' }); return false; }
    return confirm(`${what}、API キーで Claude に作ってもらいます。30秒〜数分かかり、キーの持ち主に API の料金がかかります（1回の上限 $1.00）。よろしいですか？`);
  }
  return confirm(`${what} Claude に作ってもらいます。30秒〜数分かかり、Claude のプランの利用枠を使います。よろしいですか？`);
}
const plainEvidence = (ev) => (ev || []).map((e) => (e && typeof e === 'object' ? (e.ref || e.text || e.type || '') : e)).filter(Boolean).join('、');
function requestText(f) {   // 指摘の見出しと本文から作る頼み文（修正タブの「頼んだこと」に出て、Claude に渡る）
  const ev = plainEvidence(f.evidence);
  return [`検品の指摘 ${f.id}「${headline(f)}」のとおりに直す案を作ってください。`,
    f.message && f.message !== headline(f) ? `指摘の内容：${f.message}` : '', ev ? `根拠：${ev}` : ''].filter(Boolean).join('\n');
}
// 頼む：修正担当の window.createFixRequest（修正タブの「頼んだこと」に出し、終わったら知らせる）。まだない版では知らせるだけ。
// 返り値は待たない（Claude の答えまで 30秒〜数分かかる）。頼めたら true
function sendFixRequest(req) {
  if (typeof window.createFixRequest !== 'function') { toast(NO_REQUEST, { kind: 'info' }); return false; }
  let pending;
  try { pending = window.createFixRequest(req); } catch (e) { toast('頼めませんでした：' + e.message, { kind: 'err' }); return false; }
  Promise.resolve(pending).catch((e) => toast(`直す案を頼めませんでした：${e?.message || e}`, { kind: 'err' }));
  return true;
}
async function requestFix(f, b) {   // 直す案のない指摘の承認：この指摘のとおりに直す案を Claude に頼み、指摘を「依頼済み」にする
  if (typeof window.createFixRequest !== 'function') { toast(NO_REQUEST, { kind: 'info' }); return; }
  if (!(await confirmFixCost('この指摘のとおりに直す案を'))) return;
  const card = b?.closest('.finding'); lockCard(card, b, '頼んでいます…');
  if (!sendFixRequest({ text: requestText(f), ids: (f.targets || []).map(String), findingId: f.id })) { unlockCard(card); return; }
  try {
    await quietly(() => keepPlace(cardSel(f.id), async () => {
      await call('POST', `/api/findings/{a}/${encodeURIComponent(f.id)}`, { action: 'request' }); await renderFindings();
    }));
  } catch (e) { toast(`${f.id} に「頼んだ」の印を残せませんでした：${e.message}`, { kind: 'err' }); unlockCard(card); }
}

function findingActions(f, st, acts) {
  const btn = (label, cls, title, fn) => {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.className = cls; b.title = title;
    b.addEventListener('click', () => fn(b)); acts.appendChild(b); return b;
  };
  if (st === '未確認') {
    btn('承認', 'approve', f.proposal ? '直す案のとおりに直します（修正として記録し、模型を組み直します。取り消せます）'
      : 'この指摘のとおりに直す案を Claude に作ってもらいます（修正タブの「頼んだこと」に出ます）', (b) => (f.proposal ? approveFinding(f, b) : requestFix(f, b)));
    btn('却下', '', '指摘を却下します（模型は変わりません。あとで戻せます）', (b) => decideQuiet(f, 'reject', b));
    btn('あとで', 'quiet', 'あとで決めます（模型は変わりません）', (b) => decideQuiet(f, 'later', b));
    return;
  }
  acts.insertAdjacentHTML('beforeend', `<span class="f-done is-${esc(st)}">${esc(ST_LABEL[st] || st)}</span>`);
  if (st === '承認') {
    if (f.override) btn('取り消す', 'quiet', `直し（${f.override}）を取り消して、模型を組み直します。指摘は「未確認」に戻ります`, (b) => undoFindingFix(f, f.override, b));
  } else btn('戻す', 'quiet', '「未確認」に戻します（模型は変わりません）', (b) => decideQuiet(f, 'reset', b));
}

function findingItem(f, withActions) {
  const li = document.createElement('li');
  const sev = ['高', '中', '低', '情報'].includes(f.severity) ? f.severity : 'none';
  const st = f.status || '未確認';
  li.className = `finding sev-${sev} st-${st}`; li.dataset.fid = f.id;
  const ids = (f.targets || []).map(String);
  li.innerHTML = `<div class="f-head" role="button" tabindex="0" title="押すと、模型のその場所を光らせます">`
      + `<span class="sev" title="重さ：${esc(f.severity)}">${esc(f.severity || '')}</span>`
      + `<div class="f-main"><span class="f-title">${esc(headline(f))}</span>`
      + `<span class="f-ids">${ids.map((id) => `<span class="idc" data-id="${esc(id)}" title="${esc(id)} だけを光らせる">${esc(id)}</span>`).join('')}</span></div>`
      + (st === '未確認' ? '<span class="f-st"><span class="st 未確認">未確認</span></span>' : '') + '</div>'   // 決めたものは下の札で示す
    + (f.proposal ? `<div class="f-prop">${proposalLine(f.proposal)}</div>` : '');
  const head = li.querySelector('.f-head');
  head.addEventListener('click', (e) => { const c = e.target.closest('.idc'); if (c) selectElement(c.dataset.id, { label: f.id }); else focusFinding(f); });
  head.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); focusFinding(f); } });
  const acts = document.createElement('div'); acts.className = 'f-acts';
  if (withActions) findingActions(f, st, acts);
  const where = document.createElement('button'); where.className = 'where';
  where.innerHTML = '<svg class="ic"><use href="#i-target"/></svg><span>場所を見る</span>'; where.title = '模型と平面図で、この指摘の部材を光らせます';
  where.addEventListener('click', () => focusFinding(f));
  if (ids.length) acts.appendChild(where);
  li.appendChild(acts);
  // くわしく：本文・根拠・確からしさ・直す案の中身・図
  const more = document.createElement('details'); more.className = 'f-more';
  const ev = evidenceText(f.evidence);
  more.innerHTML = '<summary>くわしく</summary><div class="f-detail">'
    + `<p class="msg">${esc(f.message)}</p><dl>`
    + `<dt>番号</dt><dd>${esc(f.id)}${f.view && VIEW_JA[f.view] ? `（${VIEW_JA[f.view]}で見つけた）` : ''}</dd>`
    + `<dt>対象</dt><dd>${ids.map(esc).join('・') || 'なし'}</dd>`
    + `<dt>根拠</dt><dd>${ev || 'なし'}</dd>`
    + (f.confidence ? `<dt>確からしさ</dt><dd>${esc(f.confidence)}</dd>` : '')
    + (f.proposal ? `<dt>直す案</dt><dd>${proposalText(f.proposal)}</dd>` : '') + '</dl>'
    + (f.proposal_error ? `<p class="err-text">直す案は作れませんでした：${esc(f.proposal_error)}</p>` : '') + '</div>';
  const detail = more.querySelector('.f-detail');
  const view = VIEW_OF[f.view] || 'plan';
  const img = document.createElement('img'); img.hidden = true; img.alt = '検品の図（対象の部材を赤い枠で強調）';
  const imgErr = document.createElement('p'); imgErr.className = 'err-text'; imgErr.hidden = true; imgErr.textContent = '図を作れませんでした（もう一度押すか、組み立て直してから見てください）';
  img.addEventListener('error', () => { img.hidden = true; imgErr.hidden = false; show.querySelector('span').textContent = '図を見る'; });
  const show = document.createElement('button'); show.className = 'quiet small figure';
  show.innerHTML = '<svg class="ic"><use href="#i-image"/></svg><span>図を見る</span>';
  show.addEventListener('click', () => {
    imgErr.hidden = true;
    if (img.hidden) { img.src = api(`/api/inspect/{a}/image/${view}`) + `?hl=${encodeURIComponent(ids.join(','))}&t=${Date.now()}`; }
    img.hidden = !img.hidden; show.querySelector('span').textContent = img.hidden ? '図を見る' : '図を閉じる';
  });
  detail.append(show, img, imgErr);
  li.appendChild(more);
  return li;
}

async function renderFindings() {
  const rep = await call('GET', '/api/findings/{a}');
  state.report = rep; updateSteps();
  const all = [...(rep.findings || []), ...(rep.code_findings || [])];
  const open = all.filter((f) => !DONE_ST.includes(f.status));
  setTabCount('inspect', open.length, open.some((f) => f.severity === '高') ? 'bad' : '');
  // 「くわしく」を開いていたカードは、描き直しても開いたままにする
  const opened = new Set($$('.cards .finding').filter((li) => li.querySelector(':scope > .f-more')?.open).map((li) => li.dataset.fid));
  const fill = (sel, items, withActions, empty, countSel) => {
    const ul = $(sel); ul.innerHTML = '';
    const n = (items || []).filter((f) => !DONE_ST.includes(f.status));
    const c = $(countSel); c.textContent = n.length ? String(n.length) : ''; c.className = 'sec-count' + (n.some((f) => f.severity === '高') ? ' bad' : '');
    if (!items || !items.length) { ul.innerHTML = `<li class="hint">${empty}</li>`; return; }
    for (const f of items) {
      const li = findingItem(f, withActions);
      if (opened.has(String(f.id))) li.querySelector(':scope > .f-more').open = true;
      ul.appendChild(li);
    }
  };
  fill('#findings', rep.findings, true, rep.connection === 'off' ? 'AI なしで検品したので、Claude の指摘はありません' : 'まだありません', '#n-findings');
  fill('#code-findings', rep.code_findings, true, 'ありません', '#n-code');
  renderQuestions(rep);
  const cost = rep.cost_usd ? `　費用の目安 $${Number(rep.cost_usd).toFixed(2)}` : '';
  $('#inspect-summary').textContent = rep.summary ? `${rep.summary}${rep.duration_s ? `（${rep.duration_s} 秒${cost}）` : ''}` : '';
  markLit();
}

// ---- Claude からの質問に答える（★11/5 夜 作者「質問に回答できる窓口がない」）。選択肢は番号付きのボタン、ほかは書いて答える。
// 答えは物件ごとに残り（out/<物件>/answers.json）、次の検品で Claude に伝わる（同じ質問を二度しない）
const qSel = (qid) => `#questions [data-qid="${CSS.escape(String(qid))}"]`;
function renderQuestions(rep) {
  const qs = $('#questions'); qs.innerHTML = '';
  const byId = new Map((rep.findings || []).map((f) => [f.id, f]));
  const questions = (rep.questions || []).concat((rep.findings || []).filter((f) => f.question).map((f) => ({ ...f.question, id: f.id })));
  const open = questions.filter((q) => !q.answer).length;
  $('#n-questions').textContent = open ? String(open) : '';
  if (!questions.length) { qs.innerHTML = '<li class="muted">ありません</li>'; return; }
  for (const q of questions) qs.appendChild(questionItem(q, byId.get(q.id) || null));
}
function questionItem(q, f) {   // f：指摘に付いた質問なら、その指摘（直す案を頼むときに部材の番号を渡す）
  const li = document.createElement('li'); li.className = 'q-item' + (q.answer ? ' answered' : ''); li.dataset.qid = q.id;
  li.innerHTML = `<div class="q-line"><span class="tag">${esc(q.id)}</span><span class="q">${esc(q.question)}</span></div>`
    + (f ? `<div class="meta">指摘 ${esc(f.id)}「${esc(headline(f))}」についての質問</div>` : '')
    + (q.evidence ? `<div class="meta">根拠：${esc(q.evidence)}</div>` : '');
  const body = document.createElement('div'); body.className = 'q-body'; li.appendChild(body);
  if (!q.answer) {
    if (q.previous_answer?.answer) body.insertAdjacentHTML('beforeend', `<div class="q-prev">前の検品で同じ質問に「${esc(q.previous_answer.answer)}」と答えています${q.previous_answer.at ? `（${esc(fmtTime(q.previous_answer.at))}）` : ''}</div>`);
    body.appendChild(answerForm(q, f));
    return li;
  }
  body.insertAdjacentHTML('beforeend', `<div class="q-ans"><span class="lbl">答え</span><b>${esc(q.answer)}</b>${q.answered_at ? `<span class="when">${esc(fmtTime(q.answered_at))}</span>` : ''}</div>`);
  const acts = document.createElement('div'); acts.className = 'q-acts';
  const make = document.createElement('button'); make.type = 'button'; make.className = 'approve'; make.textContent = 'この答えで直す案を作る';
  make.title = 'この答えのとおりに直す案を Claude に作ってもらいます（修正タブの「頼んだこと」に出ます）';
  make.addEventListener('click', () => requestFromAnswer(q, q.answer, f, make));
  const redo = document.createElement('button'); redo.type = 'button'; redo.className = 'quiet'; redo.textContent = '答え直す';
  redo.addEventListener('click', () => { body.innerHTML = ''; body.appendChild(answerForm(q, f)); body.querySelector('.q-opt, input')?.focus(); });
  acts.append(make, redo); body.appendChild(acts);
  return li;
}
function answerForm(q, f) {
  const box = document.createElement('div'); box.className = 'q-form';
  const opts = document.createElement('div'); opts.className = 'q-opts';
  (q.options || []).forEach((o, i) => {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'q-opt'; b.title = `「${o}」と答える`;
    b.innerHTML = `<span class="num">${i + 1}.</span><span>${esc(o)}</span>`;
    b.addEventListener('click', () => answerQuestion(q, String(o), f, box)); opts.appendChild(b);
  });
  const row = document.createElement('div'); row.className = 'q-write';
  const inp = document.createElement('input'); inp.type = 'text'; inp.maxLength = 400; inp.placeholder = '選択肢にないときは、書いて答える';
  const send = document.createElement('button'); send.type = 'button'; send.textContent = '答える';
  const go = () => { const v = inp.value.trim(); if (!v) { inp.focus(); return; } answerQuestion(q, v, f, box); };
  send.addEventListener('click', go);
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); go(); } });   // 変換中の Enter では送らない
  row.append(inp, send);
  if ((q.options || []).length) box.appendChild(opts);
  box.appendChild(row);
  return box;
}
async function answerQuestion(q, answer, f, box) {
  $$('button, input', box).forEach((el) => { el.disabled = true; });
  try {
    await quietly(() => keepPlace(qSel(q.id), async () => {
      await call('POST', `/api/findings/{a}/questions/${encodeURIComponent(q.id)}/answer`, { answer, question: q.question }); await renderFindings();
    }));
    const shown = answer.length > 24 ? answer.slice(0, 23) + '…' : answer;
    toast(`${q.id} に「${shown}」と答えました。次の検品で Claude に伝えます`, { kind: 'ok', action: { label: 'この答えで直す案を作る', fn: () => requestFromAnswer(q, answer, f) } });
  } catch (e) {
    toast('答えを残せませんでした：' + e.message, { kind: 'err' });
    $$('button, input', box).forEach((el) => { el.disabled = false; });
  }
}
async function requestFromAnswer(q, answer, f, b) {   // 答えのとおりに直す案を Claude に頼む（修正タブの「頼んだこと」）
  if (typeof window.createFixRequest !== 'function') { toast(NO_REQUEST, { kind: 'info' }); return; }
  if (!(await confirmFixCost('この答えのとおりに直す案を'))) return;
  const opts = (q.options || []).map((o, i) => `${i + 1}. ${o}`).join(' ／ ');
  const text = [`検品の Claude の質問 ${q.id}「${q.question}」に、人が「${answer}」と答えました。この答えのとおりに直す案を作ってください。`,
    opts ? `選択肢：${opts}` : '', q.evidence ? `質問の根拠：${q.evidence}` : '',
    f ? `もとの指摘 ${f.id}「${headline(f)}」：${f.message || ''}` : ''].filter(Boolean).join('\n');
  if (sendFixRequest({ text, ids: f ? (f.targets || []).map(String) : [], findingId: f ? f.id : null }) && b) {
    b.disabled = true; b.textContent = '頼みました（修正タブの「頼んだこと」）';
  }
}

// ---- 検品中の札（★11/5 夜 作者「Claude が考えています、という文章がわかりづらい」）：だれが・何をしていて・どのくらいかかるか
function modelLabel(id) {   // claude-opus-5-5 → Opus 5.5
  const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(String(id || ''));
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : '';
}
function fmtElapsed(sec) {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  return n < 60 ? `${n} 秒` : `${Math.floor(n / 60)} 分 ${String(n % 60).padStart(2, '0')} 秒`;
}
function renderInspectBanner(st) {
  const box = $('#inspect-banner'); if (!box) return;
  const s = st.state || {};
  if (!s.running) { box.hidden = true; box.innerHTML = ''; return; }
  let lead; let note;
  if (s.connection === 'off') { lead = 'コードが模型を検査しています'; note = 'ふつう数秒で終わります'; }
  else {
    const phase = st.progress?.phase || '準備中'; const m = s.models || {};
    const merging = phase === '指摘をまとめている';
    const name = modelLabel(merging ? m.merge : m.face);
    const who = `Claude${name ? `（${name}）` : ''}`;
    const faces = Object.values(st.progress?.faces || {});
    const done = faces.filter((x) => x.state === '済み' || x.state === '失敗').length;
    lead = merging ? `${who}が、6 つの面の指摘をまとめています`
      : phase === '面ごとに検品中' ? `${who}が 6 つの面を見比べています（済み ${done}/${faces.length || 6}）` : `${who}が検品の準備をしています`;
    note = 'ふつう 3〜6 分かかります';
  }
  box.hidden = false;
  box.innerHTML = `<span class="spin" aria-hidden="true"></span><div class="ib-text"><b>${esc(lead)}</b>`
    + `<span>経過 ${esc(fmtElapsed(s.elapsed_s))}・${esc(note)}・裏で進むので、ほかのタブを見ていても大丈夫です</span></div>`;
}

function renderProgress(st) {
  const box = $('#inspect-progress');
  const prog = st.progress; const s = st.state || {};
  renderInspectBanner(st);
  if (!s.connection) { box.innerHTML = ''; return; }
  if (s.connection === 'off') { box.innerHTML = `<div class="face ${s.running ? '見ている' : '済み'}"><b>コードの検査</b>${s.running ? '検査中…' : '済み'}</div>`; return; }
  if (!prog) { box.innerHTML = ''; return; }
  box.innerHTML = Object.entries(prog.faces).map(([k, f]) =>
    `<div class="face ${esc(f.state)}" title="${esc(f.doing || '')}"><b>${esc(st.face_labels[k] || k)}</b>${esc(f.state)}${f.doing ? '：' + esc(f.doing) : ''}</div>`).join('');
}

async function pollInspection() {
  if (!state.alias) { renderInspectBanner({}); return; }       // 物件を閉じた
  try {
    const st = await call('GET', '/api/inspect/{a}/status');
    renderProgress(st);
    const s = st.state || {};
    if (s.running) {
      const phase = st.progress ? st.progress.phase : 'コードの検査中';
      setStatus(`検品中（${phase}・${s.elapsed_s || 0} 秒）…`, 'busy');
      setTimeout(pollInspection, 2000);
    } else if (s.connection) {
      inspectButtons(false);
      const look = state.ws === 'inspect' ? null : { label: '検品を見る', fn: () => setWorkspace('inspect') };   // 押したときだけタブを替える
      if (s.error) { setStatus('検品が終わりませんでした：' + s.error, 'err'); toast('検品が終わりませんでした：' + s.error, { kind: 'err', action: look }); }
      else {
        await quietly(renderFindings); setStatus(`検品が終わりました（${s.elapsed_s || 0} 秒）`, 'ok');
        const rep = state.report || {};
        const nq = (rep.questions || []).length + (rep.findings || []).filter((f) => f.question).length;
        toast(`検品が終わりました：指摘 ${(rep.findings || []).length + (rep.code_findings || []).length} 件${nq ? `・質問 ${nq} 件` : ''}（${fmtElapsed(s.elapsed_s)}）`, { kind: 'ok', action: look });
      }
    }
  } catch (e) { setStatus('進み具合を読めませんでした：' + e.message, 'err'); }
}

function inspectButtons(disabled) {
  state.inspecting = disabled;
  for (const id of ['#inspect-ai', '#inspect-api', '#inspect-off']) $(id).disabled = disabled || !state.built;
}

async function startInspection(connection, opts = {}) {
  // opts.auto：組み立てたあとに自動で始める（設定で自分が選んだので、確かめの窓は出さない）
  if (!state.alias) return;
  if (!opts.auto && connection === 'claude_code' && !confirm('Claude に検品してもらいます。5分ほどかかり、Claude のプランの利用枠を使います。よろしいですか？')) return;
  if (connection === 'api_key') {
    const st = await call('GET', '/api/settings');
    if (!st.api_key_set) { setStatus('API キーが設定されていません。「ファイル」→「環境設定」で API キーを入れてください', 'err'); return; }
    if (!opts.auto && !confirm(`API キーで Claude に検品してもらいます。5分ほどかかり、キーの持ち主に API の料金がかかります（1回の上限 $${Number(st.max_budget_usd || 3).toFixed(2)}）。よろしいですか？`)) return;
  }
  try {
    await call('POST', `/api/inspect/{a}?connection=${connection}`);
    inspectButtons(true);
    pollInspection();
  } catch (e) { setStatus('検品を始められませんでした：' + e.message, 'err'); }
}
$('#inspect-ai').addEventListener('click', () => startInspection('claude_code'));
$('#inspect-api').addEventListener('click', () => startInspection('api_key'));
$('#inspect-off').addEventListener('click', () => startInspection('off'));

// ---------------------------------------------------------------- 会社ルールの言語化（SPEC §5-4②）
// 家ごとに変わる値は物件ごとに glb から作る。会社で変わらない値と細かい決まりを会社ルールとして残し、予防に使う（作者 11/3）
const STATUS_JA = { same: '会社で同じ', varies: '物件ごとに違う', few: '件数が足りない', info: '参考' };
const RKIND_JA = { value: '値を直す', glb: '物件ごとに glb の値', skill: '細かい決まり' };
const companyOf = () => state.building?.source?.company_rules || state.defaultCompany || 'jisha';   // 既定は設定の画面の default_company（jisha がない版では B工務店（架空））
const rulesApi = (p = '') => `/api/rules/${encodeURIComponent(companyOf())}${p}`;
let rulesDone = new Set();       // 承認・却下した案の印（最後の案を開き直しても出さない）

async function renderRules() {
  let d;
  try { d = await call('GET', rulesApi()); } catch (e) { $('#rules-stats').innerHTML = `<tr><td>${esc(e.message)}</td></tr>`; return; }
  $('#rules-title').textContent = `集計（コードが計算。物件 ${d.properties.length} 件：${d.properties.join('・')}）`;
  $('#rules-stats').innerHTML = '<tr><th>項目</th><th>物件ごとの値</th><th>判定</th></tr>' + d.stats.map((s) =>
    `<tr><td>${esc(s.label)}</td><td>${esc(s.agreement)}</td><td><span class="st st-${s.status}">${STATUS_JA[s.status] || s.status}</span></td></tr>`).join('');
  const values = Object.entries(d.learned.values).map(([k, v]) => ({ ...v, key: k }));
  rulesDone = new Set([...values, ...d.learned.notes].map((x) => x.signature).concat(d.rejected || []));
  renderRuleProposals(d.last_draft);
  const ul = $('#rules-learned'); ul.innerHTML = '';
  const items = [...values, ...d.learned.notes];
  if (!items.length) ul.innerHTML = '<li class="muted">まだありません</li>';
  for (const it of items) {
    const li = document.createElement('li');
    li.innerHTML = `<span class="tag">${esc(it.id)}</span>${esc(RKIND_JA[it.kind] || it.kind)}：${esc(it.text)}`
      + (it.key ? `（${esc(it.key)}：${esc(it.previous)} → ${esc(it.value)}）` : '') + `<br><span class="hint">根拠：${esc(it.evidence)}</span> `;
    const b = document.createElement('button'); b.textContent = '取り消す'; b.className = 'quiet';
    b.addEventListener('click', async () => {
      try { await call('DELETE', rulesApi(`/learned/${encodeURIComponent(it.id)}`)); setStatus(`${it.id} を取り消しました`, 'ok'); renderRules(); }
      catch (e) { setStatus('取り消せませんでした：' + e.message, 'err'); }
    });
    li.appendChild(b); ul.appendChild(li);
  }
  for (const s of d.learned_skipped || []) ul.insertAdjacentHTML('beforeend', `<li class="lv-中">重ねられなかった学んだ値：${esc(s.rule_key)}（${esc(s.reason)}）</li>`);
}

function renderRuleProposals(draft) {
  const box = $('#rules-proposals'); box.innerHTML = '';
  if (!draft) { box.innerHTML = '<p class="hint">まだ案はありません。道具の帯の「案を書いてもらう」を押してください。</p>'; return; }
  const conn = { claude_code: 'Claude（自分のプラン）', api_key: 'Claude（API キー）', off: 'AI なし' }[draft.connection] || draft.connection;
  box.insertAdjacentHTML('beforeend', `<p class="hint">最後の案：${esc((draft.at || '').slice(0, 16).replace('T', ' '))}・${esc(conn)}`
    + `${draft.cost_usd ? `・費用の目安 $${Number(draft.cost_usd).toFixed(2)}` : ''}</p>`);
  if (draft.message) box.insertAdjacentHTML('beforeend', `<p class="msg">${esc(draft.message)}</p>`);
  for (const p of (draft.proposals || []).filter((x) => !rulesDone.has(x.signature))) {
    const div = document.createElement('div'); div.className = 'prop';
    div.innerHTML = `<span class="tag">${esc(RKIND_JA[p.kind] || p.kind)}</span>${esc(p.wording)}`
      + (p.kind === 'value' ? `<div class="meta">${esc(p.rule_key)}：${esc(p.from)} → ${esc(p.to)}（新しい物件との比べ・予備の値に使われます）</div>` : '')
      + `<div class="meta">根拠：${esc(p.evidence)}</div>`;
    const ok = document.createElement('button'); ok.className = 'primary'; ok.textContent = '承認';
    ok.addEventListener('click', async () => {
      try {
        await call('POST', rulesApi('/approve'), { kind: p.kind, wording: p.wording, stat_ids: p.stat_ids, answer_ids: p.answer_ids,
          rule_key: p.rule_key, value_source: p.value_source, origin: p.origin });
        setStatus('会社ルールに入れました（予防に使われます。取り消せます）', 'ok'); renderRules();
      } catch (e) { setStatus('入れられませんでした：' + e.message, 'err'); }
    });
    const ng = document.createElement('button'); ng.textContent = '却下';
    ng.addEventListener('click', async () => {
      try { await call('POST', rulesApi('/reject'), { signature: p.signature }); setStatus('却下しました（同じ案は二度出しません）', 'ok'); renderRules(); }
      catch (e) { setStatus('却下できませんでした：' + e.message, 'err'); }
    });
    div.append(ok, ng); box.appendChild(div);
  }
  for (const q of draft.questions || []) {   // 答えは記録され、次の案出しで根拠に使われる
    const div = document.createElement('div'); div.className = 'question';
    div.innerHTML = `<b>質問</b>　${esc(q.question)}${q.evidence ? `<div class="meta">根拠：${esc(q.evidence)}</div>` : ''}`;
    const answer = async (a) => {
      try { await call('POST', rulesApi('/answers'), { question: q.question, answer: a }); div.remove();
        setStatus('答えを記録しました。もう一度「案を書いてもらう」を押すと、答えを根拠に使います', 'ok'); }
      catch (e) { setStatus('記録できませんでした：' + e.message, 'err'); }
    };
    for (const opt of q.options || []) { const b = document.createElement('button'); b.textContent = opt; b.addEventListener('click', () => answer(opt)); div.appendChild(b); }
    const row = document.createElement('div'); row.className = 'answer';
    const inp = document.createElement('input'); inp.type = 'text'; inp.placeholder = '答えを書く';
    const send = document.createElement('button'); send.textContent = '答える';
    send.addEventListener('click', () => { if (inp.value.trim()) answer(inp.value.trim()); });
    row.append(inp, send); div.appendChild(row); box.appendChild(div);
  }
}

$('#rules-draft').addEventListener('click', async () => {
  const connection = $('#rules-conn').value;
  if (connection === 'api_key' && !confirm('API キーで Claude に頼みます。キーの持ち主に API の料金がかかります（1回の上限 $1.00）。よろしいですか？')) return;
  $('#rules-draft').disabled = true;
  setStatus(connection === 'off' ? '候補を作っています…' : 'Claude が会社ルールの案を書いています（1〜数分）…', 'busy');
  try {
    const d = await call('POST', rulesApi('/draft'), { connection, text: $('#rules-text').value });
    renderRuleProposals(d);
    setStatus(`案 ${d.proposals.length} 件・質問 ${d.questions.length} 件${d.cost_usd ? `（費用の目安 $${Number(d.cost_usd).toFixed(2)}）` : ''}`, 'ok');
  } catch (e) { setStatus('案を作れませんでした：' + e.message, 'err'); }
  $('#rules-draft').disabled = false;
});

// ---------------------------------------------------------------- 環境設定（前の設定タブ）・モード（自社用・他社用・見本用）
const AUTO_JA = { claude_code: 'Claude に検品してもらう（自分のプラン）', api_key: 'Claude に検品してもらう（API キー）',
  off: 'コードの検査だけ（AI なし）', none: '検品しない' };
async function renderSettings() {
  try {
    const st = await call('GET', '/api/settings');
    state.settings = st;
    $('#apikey-state').textContent = st.api_key_set ? 'API キー：保存してあります' : 'API キー：まだ入っていません';
    $('#apikey-del').disabled = !st.api_key_set;
    const sel = $('#auto-inspect');
    sel.innerHTML = (st.connections || []).map((c) => `<option value="${c}">${AUTO_JA[c] || c}</option>`).join('');
    sel.value = st.auto_inspect; sel.dataset.prev = st.auto_inspect; sel.dataset.budget = st.max_budget_usd; sel.dataset.ack = st.api_cost_ack ? '1' : '';
    state.companies = Object.fromEntries((st.companies || []).map((c) => [c.code, c.name]));
    state.defaultCompany = st.default_company;
    const cs = $('#company');
    cs.innerHTML = (st.companies || []).map((c) => `<option value="${esc(c.code)}">${esc(c.name)}</option>`).join('');
    cs.value = st.company; cs.dataset.prev = st.company;
    $('#budget').textContent = `$${Number(st.max_budget_usd || 3).toFixed(2)}`;
    applyMode(st);
  } catch (e) { $('#apikey-state').textContent = '設定を読めませんでした：' + e.message; }
}

// モードは起動で決まる（画面では切り替えない）。mode がない前のサーバーでは view を使う（名前は同じ）
function applyMode(st) {
  const ok = (m) => ['own', 'other', 'sample'].includes(m);
  const mode = ok(st.mode) ? st.mode : ok(st.view) ? st.view : 'own';
  state.mode = mode; document.body.dataset.mode = mode;
  const badge = $('#mode-badge'); badge.textContent = st.mode_label || MODE_LABEL[mode]; badge.title = MODE_TITLE[mode];
  applyConnections(st.connections || []);
  $('#steps').hidden = mode !== 'sample';
  $('#cmd-new').hidden = mode === 'sample'; $('#empty-new').hidden = mode === 'sample';   // 見本用は読み込まない
  const chip = $('#company-chip');
  chip.hidden = mode !== 'other';
  chip.textContent = `会社ルール：${state.companies?.[st.company] || st.company}`;
  $('#company-callout').hidden = !!st.company_chosen;
  if (mode !== 'own' && state.ws === 'rules') setWorkspace('model');
  // AI のつなぎ方の一覧（環境設定）
  const conns = st.connections || [];
  const rows = [];
  if (conns.includes('claude_code') && mode !== 'other') rows.push(['Claude（自分のプラン）', true]);
  rows.push([`Claude（API キー）${st.api_key_set ? '' : '：キーがまだ入っていません'}`, !!st.api_key_set]);
  rows.push(['AI なし（コードの検査だけ）', true]);
  $('#ai-conns').innerHTML = rows.map(([t, on]) => `<li class="${on ? '' : 'off'}">${esc(t)}</li>`).join('');
  updateSteps(); updateStatusBar(); updateChrome();
}

// 版・モードに合わせて、使えない接続（社外用の版と他社用ではプランの接続）の選択肢とボタンを隠す（戻せるように消さない）
function applyConnections(connections) {
  const plan = connections.includes('claude_code') && state.mode !== 'other';
  for (const sel of [$('#lasso-conn'), $('#rules-conn')]) {
    const opt = sel.querySelector('option[value="claude_code"]');
    if (!opt) continue;
    opt.hidden = !plan; opt.disabled = !plan;
    if (!plan && sel.value === 'claude_code') { sel.value = 'api_key'; sel.dataset.autoApi = '1'; }
    else if (plan && sel.dataset.autoApi) { sel.value = 'claude_code'; delete sel.dataset.autoApi; }   // 隠したときに変えた分だけ戻す
  }
  $('#inspect-ai').hidden = !plan;
}

$('#company-chip').addEventListener('click', () => openPrefs('set-company'));
// 手順の帯（見本用）：組み立てていない→1、検品していない（検品のあとに組み立て直した）→2、承認していない直す案がある→3、ほかは→4
const STEP_HINT = {
  1: '「模型」タブの「読み込んで組み立てる」で、glb から模型を組み立てます',
  2: '「検品」タブで、Claude かコードの検査に模型を見てもらいます',
  3: '指摘の「承認」か、「修正」タブの表で直します。直すと模型を組み直します',
  4: '「印刷」タブから 3MF を保存して、Bambu Studio で開きます',
};
function currentStep() {
  const rep = state.report;
  if (!state.alias || !state.built) return 1;
  if (!rep || !rep.connection || rep.stale) return 2;
  const pending = [...(rep.findings || []), ...(rep.code_findings || [])]
    .filter((f) => f.proposal && !['承認', '却下'].includes(f.status)).length;
  return pending ? 3 : 4;
}
function updateSteps() {
  const cur = currentStep();
  document.querySelectorAll('#steps [data-step]').forEach((b) => {
    const n = Number(b.dataset.step);
    b.classList.toggle('on', n === cur); b.classList.toggle('done', n < cur);
    if (n === cur) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
  });
  $('#step-hint').textContent = STEP_HINT[cur];
}
$('#steps').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-step]'); if (!b) return;
  const n = Number(b.dataset.step);
  if (n === 1) { setWorkspace('model'); $('#build').focus(); return; }
  setWorkspace(n === 2 ? 'inspect' : n === 4 ? 'print' : (currentStep() === 3 ? 'inspect' : 'fix'));
});
$('#company').addEventListener('change', async (e) => {   // 会社ルール：作り直すのは次に「読み込み直す」を押したとき
  const sel = e.target;
  try {
    await call('POST', '/api/settings/company', { company: sel.value });
    sel.dataset.prev = sel.value;
    await renderSettings();                                   // 上の帯の「会社ルール：…」と、選んでくださいの案内を直す
    setStatus(`会社ルールを「${state.companies?.[sel.value] || sel.value}」にしました。「読み込み直す」を押すと、この決まりで作り直します`, 'ok');
  } catch (err) { setStatus('会社ルールを切り替えられませんでした：' + err.message, 'err'); sel.value = sel.dataset.prev; }
});
$('#auto-inspect').addEventListener('change', async (e) => {
  const sel = e.target; const value = sel.value;
  let costAck = false;
  if (value === 'api_key' && !sel.dataset.ack) {   // API キーは1回だけ費用を確かめる
    if (!confirm(`組み立てるたびに API キーで Claude が検品します。キーの持ち主に API の料金がかかります（1回の上限 $${Number(sel.dataset.budget || 3).toFixed(2)}）。よろしいですか？`)) {
      sel.value = sel.dataset.prev; return;
    }
    costAck = true;
  }
  try {
    await call('POST', '/api/settings/auto_inspect', { value, cost_ack: costAck });
    setStatus(`組み立てたあとの検品：${AUTO_JA[value]}`, 'ok'); renderSettings();
  } catch (err) { setStatus('設定を保存できませんでした：' + err.message, 'err'); sel.value = sel.dataset.prev; }
});
$('#apikey-save').addEventListener('click', async () => {
  const key = $('#apikey').value.trim();
  if (!key) { setStatus('API キーを貼り付けてから「保存」を押してください', 'err'); return; }
  try {
    await call('POST', '/api/settings/api_key', { api_key: key });
    $('#apikey').value = '';                       // 画面に残さない
    setStatus('API キーを資格情報マネージャーに保存しました', 'ok');
  } catch (e) { setStatus('保存できませんでした：' + e.message, 'err'); }
  await renderSettings();
});
$('#apikey-del').addEventListener('click', async () => {
  if (!confirm('保存してある API キーを消します。よろしいですか？')) return;
  try {
    await call('DELETE', '/api/settings/api_key');
    setStatus('API キーを消しました', 'ok');
  } catch (e) { setStatus('消せませんでした：' + e.message, 'err'); }
  await renderSettings();
});
const settingsReady = renderSettings();     // 物件の一覧は、モードを読んでから出す（下の始まりの所で待つ）

// ---------------------------------------------------------------- 囲んで直す（平面図、SPEC §9-3）
// 囲みの中の部材はサーバー（コード）が決める。画面は囲んだ点を平面の mm に直して送るだけ。
// source：最後に囲んだ図（plan＝平面図、3d＝3D）。3D は部材ごとの画素の数（hits）を送る
// ★11/5 夜（作者の試し）：複数を囲める。groups は囲みごとの部材（①②…。「囲みを足す」で次の囲み）、selection は全部の囲みの部材。
//   囲みは修正タブ以外に移っても消さない（線は図を替えたときだけ消す）。頼んだら囲みは空にし、答えは下の「依頼」に残る
const CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩';
const lasso = { on: false, pts: [], line: null, polygon: null, selection: [], source: null,
  on3d: false, pts3d: [], line3d: null, hits: null, maskPx: 0, view: null,
  groups: [], lines: [], append: false, seq: 0 };
const planSvg = () => $('#plan svg');
const nextLabel = () => CIRCLED[lasso.seq] || `(${lasso.seq + 1})`;

function svgPoint(svg, e) {
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

function toMm(svg, x, y) {   // 平面図の点 → 平面の mm（tools/plan_preview.py が書いた data-* を使う）
  const d = svg.dataset; const s = Number(d.scale); const m = Number(d.margin);
  return [(x - m) / s + Number(d.x0), Number(d.y1) - (y - m) / s];
}

function syncLassoUi() {   // 道具の帯の「囲んで直す」と、3D／平面図の上の札
  const on = lasso.on || lasso.on3d;
  $('#lasso-btn').setAttribute('aria-pressed', on ? 'true' : 'false');
  $('#lasso-btn-label').textContent = on ? '囲むのをやめる' : '囲んで直す';
  $('#lasso-chip').hidden = !on;
  const what = lasso.on ? '平面図の上で、直したい壁や窓をぐるっと囲んでください' : '直したい部材（壁・窓・軒）をぐるっと囲んでください';
  $('#lasso-chip-text').textContent = lasso.append && lasso.groups.length ? `${nextLabel()} の囲み：${what}` : what;
}
function setLassoMode(on) {
  lasso.on = on;
  $('#plan').classList.toggle('lassoing', on);
  syncLassoUi();
}

function clearLassoLines() {   // 囲みの線だけを消す（図を替えたとき。囲んだ部材は残す）
  for (const l of lasso.lines) l.remove();
  lasso.lines = [];
  if (lasso.line) { lasso.line.remove(); lasso.line = null; }
  if (lasso.line3d) { lasso.line3d.remove(); lasso.line3d = null; }
  lasso.pts = []; lasso.pts3d = [];
}

function clearLasso() {   // 囲みと囲んだ部材を消す（依頼の一覧は消さない）
  clearLassoLines();
  lasso.polygon = null; lasso.selection = []; lasso.source = null; lasso.groups = []; lasso.seq = 0; lasso.append = false;
  lasso.hits = null; lasso.maskPx = 0; lasso.view = null;
  clearHighlight3d();
  markPlan([]);
  document.querySelectorAll('.tables tr.picked').forEach((tr) => tr.classList.remove('picked'));   // 「表で直す（AI なし）」の印も消す
  document.querySelectorAll('.tables input.picked-input').forEach((i) => i.classList.remove('picked-input'));
  $('#lasso-panel').hidden = true; $('#lasso-picked').innerHTML = ''; $('#lasso-idle').hidden = false;
}
window.clearLassoSelection = () => { if (!lasso.on && !lasso.on3d) clearLasso(); };   // 見る担当：何もない所を押したら外す

function startLassoMode() {
  if (state.pane === 'plan') { if (planSvg()) setLassoMode(true); } else if (state.root) setLasso3dMode(true);
}
$('#lasso-btn').addEventListener('click', () => {
  if (lasso.on || lasso.on3d) { setLassoMode(false); setLasso3dMode(false); return; }
  if (!state.built) { setStatus('組み立ててから囲んでください', 'err'); return; }
  clearFocus();                                            // 囲むときは、指摘や一覧で光らせた印を消す
  lasso.append = false;                                    // 道具の帯のボタンは囲み直す（足すときは「囲みを足す」）
  startLassoMode();
});
$('#lasso-add').addEventListener('click', () => {           // もう1か所囲む（①②…）
  if (!state.built || !lasso.groups.length) return;
  lasso.append = true;
  startLassoMode();
  if (!lasso.on && !lasso.on3d) lasso.append = false;      // 囲めなかった（離した表示など）
});
$('#lasso-cancel').addEventListener('click', () => { setLassoMode(false); setLasso3dMode(false); lasso.append = false; });
$('#lasso-clear').addEventListener('click', clearLasso);

$('#plan').addEventListener('pointerdown', (e) => {
  const svg = planSvg(); if (!lasso.on || !svg) return;
  e.preventDefault();
  if (!lasso.append) clearLasso(); else if (lasso.line) { lasso.line.remove(); lasso.line = null; }
  lasso.line = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
  lasso.line.setAttribute('class', 'lasso-line'); svg.appendChild(lasso.line);
  const p = svgPoint(svg, e); lasso.pts = [[p.x, p.y]];
  $('#plan').setPointerCapture(e.pointerId);
});
$('#plan').addEventListener('pointermove', (e) => {
  const svg = planSvg(); if (!lasso.on || !lasso.line || !svg) return;
  const p = svgPoint(svg, e); const last = lasso.pts[lasso.pts.length - 1];
  if (Math.hypot(p.x - last[0], p.y - last[1]) < 3) return;
  lasso.pts.push([p.x, p.y]);
  lasso.line.setAttribute('points', lasso.pts.map((q) => q.join(',')).join(' '));
});
$('#plan').addEventListener('pointerup', async () => {
  const svg = planSvg(); if (!lasso.on || !lasso.line || !svg) return;
  const line = lasso.line;
  if (lasso.pts.length < 3) { line.remove(); lasso.line = null; if (!lasso.groups.length) clearLasso(); return; }
  line.setAttribute('points', lasso.pts.concat([lasso.pts[0]]).map((q) => q.join(',')).join(' '));   // 囲みを閉じる
  setLassoMode(false);
  lasso.line = null; lasso.lines.push(line);                 // 囲みを足しても、前の囲みの線は残す
  lasso.polygon = lasso.pts.map(([x, y]) => toMm(svg, x, y));
  lasso.source = 'plan';
  const level = state.multi ? $('#plan-level').value : null;
  try {
    const res = await call('POST', '/api/lasso/{a}/select', { polygon: lasso.polygon, level });
    addGroup(res.selection, { source: 'plan', view: null, level });
  } catch (e) {
    setStatus('囲みを読めませんでした：' + e.message, 'err');
    line.remove(); lasso.lines = lasso.lines.filter((l) => l !== line); lasso.append = false;
    if (!lasso.groups.length) clearLasso();
  }
});

// ---------------------------------------------------------------- 本体の分け目（平面図でドラッグ。★11/5 作者 2-1）
// 平面図の <g id="split">（tools/plan_preview.py）の data-* を読む。分け目の位置 t は「平面の mm を向き u に投影した値」。
// 動かせる範囲（data-ranges）の中だけ動かし、離すと全部の分け目を /api/split に送る（近くの壁の面に寄せるのはサーバー）
const splitUi = { drag: null, busy: false };   // 両側の長さは模型の mm で見せる（MODEL_SCALE）

function splitGroup() { return $('#plan svg g#split'); }
function splitData(g) {
  const d = g.dataset; const parse = (s, dflt) => { try { return JSON.parse(s); } catch (e) { return dflt; } };
  return { ux: Number(d.ux), uy: Number(d.uy), lo: Number(d.lo), hi: Number(d.hi), source: d.source,
    ranges: parse(d.ranges || '[]', []), sides: parse(d.sides || '[]', []),
    cuts: [...g.querySelectorAll('[data-role="split"]')].sort((a, b) => a.dataset.i - b.dataset.i).map((el) => Number(el.dataset.t)) };
}
function splitLabelText(sd, cuts, i, prefix) {
  const prev = i === 0 ? sd.lo : cuts[i - 1]; const next = i === cuts.length - 1 ? sd.hi : cuts[i + 1];
  const a = sd.sides[i] || ''; const b = sd.sides[i + 1] || '';
  return `${prefix}｜${a} ${Math.round((cuts[i] - prev) / MODEL_SCALE)}mm・${b} ${Math.round((next - cuts[i]) / MODEL_SCALE)}mm（模型）`;
}
function updateSplitControls() {   // 平面図を読み直したあと：分け目のあるときだけ説明を出し、人が決めた分け目なら「自動に戻す」を出す
  const g = splitGroup();
  const hc = state.checks?.house_cut;
  const has = !!g || !!(hc && hc.cuts && hc.cuts.length);
  $('#split-hint').hidden = !has;
  $('#split-reset').hidden = !((g && g.dataset.source === 'human') || (!g && hc && hc.source === 'human'));
  $('#sec-split').hidden = !has;
  if (hc && hc.cuts && hc.cuts.length) {
    const pts = [hc.lo, ...hc.cuts, hc.hi]; const sides = hc.sides || [];
    const lens = pts.slice(1).map((t, i) => `${sides[i] || i + 1} ${Math.round((t - pts[i]) / MODEL_SCALE)}mm`);
    $('#split-info').textContent = `長い家なので、本体を ${hc.cuts.length + 1} つに分けて書き出しています（${lens.join('・')}、模型の長さ）。`
      + `${hc.source === 'human' ? '人が動かした分け目です。' : '自動で決めた分け目です。'}平面図のオレンジの点線をつかむと動かせます。`;
  } else if (g) $('#split-info').textContent = '平面図のオレンジの点線は本体の分け目です。つかんで動かせます。';
}
$('#split-show-plan').addEventListener('click', () => setPane('plan'));

$('#plan').addEventListener('pointerdown', (e) => {
  const hit = e.target.closest?.('[data-role="split"]');
  const svg = planSvg(); const g = splitGroup();
  if (lasso.on || !hit || !svg || !g) return;
  e.preventDefault();
  if (splitUi.busy) { setStatus('書き出しが終わってから動かしてください', 'err'); return; }
  const sd = splitData(g); const i = Number(hit.dataset.i);
  splitUi.drag = { i, sd, cuts: sd.cuts.slice(), start: sd.cuts[i], svg, g, pointerId: e.pointerId };
  g.classList.add('dragging');
  $('#plan').setPointerCapture(e.pointerId);
});
$('#plan').addEventListener('pointermove', (e) => {
  const d = splitUi.drag; if (!d) return;
  const p = svgPoint(d.svg, e); const [mx, my] = toMm(d.svg, p.x, p.y);
  const { sd, i } = d;
  let t = sd.ux * mx + sd.uy * my;
  const r = sd.ranges[i]; if (r) t = Math.min(Math.max(t, r[0]), r[1]);         // どの部品も造形範囲に入る範囲
  if (i > 0) t = Math.max(t, d.cuts[i - 1] + 200);                               // となりの分け目を追い越さない
  if (i < d.cuts.length - 1) t = Math.min(t, d.cuts[i + 1] - 200);
  d.cuts[i] = t;
  const s = Number(d.svg.dataset.scale); const dt = t - d.start;
  const move = `translate(${(dt * sd.ux * s).toFixed(2)} ${(-dt * sd.uy * s).toFixed(2)})`;   // 平面図は Y が下向き
  d.g.querySelectorAll(`[data-i="${i}"]`).forEach((el) => el.setAttribute('transform', move));
  for (const j of [i - 1, i, i + 1]) {                                            // となりの分け目の札も、間の部品の長さが変わる
    const label = d.g.querySelector(`text.split-label[data-i="${j}"]`);
    if (label) label.textContent = splitLabelText(sd, d.cuts, j, j === i ? '動かしている分け目' : label.textContent.split('｜')[0]);
  }
});
async function afterExport() {     // 書き出し直したあと：検査・平面図・書き出し・下の帯
  await reloadChecks(); await loadPlan(); await renderChecks(); renderDownloads(); renderModelInfo(); updateStatusBar();
}
async function endSplitDrag() {
  const d = splitUi.drag; if (!d) return;
  splitUi.drag = null; d.g.classList.remove('dragging');
  if (Math.abs(d.cuts[d.i] - d.start) < 1) {                                      // 動かしていない（押しただけ）
    d.g.querySelectorAll(`[data-i="${d.i}"]`).forEach((el) => el.removeAttribute('transform'));
    return;
  }
  splitUi.busy = true;
  setStatus('書き出し中…（分け目を動かして、部品を作り直しています）', 'busy');
  try {
    const res = await call('POST', '/api/split/{a}', { cuts_mm: d.cuts.map((t) => Math.round(t * 10) / 10) });
    const hc = res.house_cut;
    await afterExport();
    const moved = hc && hc.cuts ? splitLabelText({ ...d.sd, lo: hc.lo, hi: hc.hi, sides: hc.sides || d.sd.sides }, hc.cuts, d.i, '分け目を動かしました') : '分け目を動かしました';
    const snapped = hc && hc.cuts && Math.abs(hc.cuts[d.i] - d.cuts[d.i]) > 1 ? '。近くの壁の面に寄せました' : '';
    setStatus(`${moved}${snapped}。書き出し直しました（「分け目を自動に戻す」で戻せます）`, 'ok');
    await syncVersion();
  } catch (err) {
    setStatus('分け目を動かせませんでした：' + err.message, 'err');               // 例：その分け目では、分けた部品のどれかが造形範囲に入りません
    try { await loadPlan(); } catch (e2) { /* 平面図は次に開いたときに読む */ }
  } finally { splitUi.busy = false; }
}
$('#plan').addEventListener('pointerup', endSplitDrag);
$('#plan').addEventListener('pointercancel', endSplitDrag);
$('#split-reset').addEventListener('click', async () => {
  if (splitUi.busy) return;
  splitUi.busy = true; $('#split-reset').disabled = true;
  setStatus('書き出し中…（分け目を自動に戻しています）', 'busy');
  try {
    await call('DELETE', '/api/split/{a}');
    await afterExport();
    setStatus('分け目を自動に戻して、書き出し直しました', 'ok');
    await syncVersion();
  } catch (err) { setStatus('戻せませんでした：' + err.message, 'err'); }
  finally { splitUi.busy = false; $('#split-reset').disabled = false; }
});

// ---------------------------------------------------------------- 囲んで直す（3D）
// 画面に見えている部材だけを選ぶ。当たり判定用の形（/api/pick）を部材ごとの色で塗って画素を数え（pick3d.js）、
// 数をサーバーに送る。どれを選ぶかはサーバー（コード）が決める
const overlay = $('#lasso3d-overlay');
const SVGNS = 'http://www.w3.org/2000/svg';
const HL_MAT = new THREE.MeshBasicMaterial({ color: 0xe0008c, transparent: true, opacity: 0.45, depthTest: false, side: THREE.DoubleSide });

function setLasso3dMode(on) {   // ★11/5 夜：パーツを離したままでも囲める（pick3d.js が離した位置で数える）
  lasso.on3d = on; controls.enabled = !on;                 // 囲む間は回さない
  holder.classList.toggle('lassoing3d', on);
  syncLassoUi();
  if (on) setStatus('直したい部材（壁・窓・軒）をぐるっと囲んでください');
}

function clearHighlight3d() { if (state.hl) { scene.remove(state.hl); state.hl = null; } state.hlRows = null; state.hlAnchors = null; }
// 選んだ部材に半透明の色を重ねる。パーツを離しているときは、部品ごとに離した位置へずらした写しに重ねる（pick3d.js の pickCopies）
function highlight3d(rows) {
  clearHighlight3d();
  if (!state.pick || !state.root) return;
  const ids = new Set(rows.map((r) => String(r.id)));
  state.hl = new THREE.Group(); state.hlRows = rows;
  // 名前の札を出す所：部材の番号（屋根は番号でも）ごとに、全体の箱の上の真ん中と、写しごとの上の真ん中（高い順）。
  // 画面に入っている最初の所に出す（離したとき、上の部分が画面の外なら下の部分の上に）
  const boxes = new Map();
  const grow = (key, box) => { const e = boxes.get(key) || { all: box.clone(), parts: [] }; e.all.union(box); e.parts.push(box); boxes.set(key, e); };
  const top = (b) => new THREE.Vector3((b.min.x + b.max.x) / 2, (b.min.y + b.max.y) / 2, b.max.z);
  for (const c of pickCopies(state.root, state.pick, { all: true })) {
    const id = String(c.src.userData.id); if (!ids.has(id)) continue;
    const m = new THREE.Mesh(c.geo, HL_MAT); m.position.z = c.dz; m.renderOrder = 10; state.hl.add(m);
    if (!c.geo.boundingBox) c.geo.computeBoundingBox();
    const box = c.geo.boundingBox.clone(); box.min.z += c.dz; box.max.z += c.dz;
    grow(id, box); if (baseId(id) !== id) grow(baseId(id), box);
  }
  state.hlAnchors = new Map([...boxes].map(([k, e]) => [k, [top(e.all), ...e.parts.sort((a, b) => b.max.z - a.max.z).map(top)]]));
  state.hl.renderOrder = 10;
  scene.add(state.hl);
}

function viewHint() {               // カメラの向きから「どちらから見た画面か」（core/render.py の面の名前）
  const d = camera.getWorldDirection(new THREE.Vector3());
  if (d.z < -0.8) return 'plan';
  if (Math.abs(d.x) > Math.abs(d.y)) return d.x > 0 ? 'W' : 'E';   // 東（+x）を向いている＝西から見ている
  return d.y > 0 ? 'S' : 'N';
}

function overlayPoint(e) { const r = overlay.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

overlay.addEventListener('pointerdown', (e) => {
  if (!lasso.on3d) return;
  e.preventDefault();
  if (!lasso.append) clearLasso(); else if (lasso.line3d) { lasso.line3d.remove(); lasso.line3d = null; }
  lasso.line3d = document.createElementNS(SVGNS, 'polyline'); lasso.line3d.setAttribute('class', 'lasso-line');
  overlay.appendChild(lasso.line3d);
  lasso.pts3d = [overlayPoint(e)];
  overlay.setPointerCapture(e.pointerId);
});
overlay.addEventListener('pointermove', (e) => {
  if (!lasso.on3d || !lasso.line3d) return;
  const p = overlayPoint(e); const last = lasso.pts3d[lasso.pts3d.length - 1];
  if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 3) return;
  lasso.pts3d.push(p);
  lasso.line3d.setAttribute('points', lasso.pts3d.map((q) => q.join(',')).join(' '));
});
overlay.addEventListener('pointerup', async () => {
  if (!lasso.on3d || !lasso.line3d) return;
  if (lasso.pts3d.length < 3) { lasso.line3d.remove(); lasso.line3d = null; if (!lasso.groups.length) clearLasso(); return; }
  lasso.line3d.setAttribute('points', lasso.pts3d.concat([lasso.pts3d[0]]).map((q) => q.join(',')).join(' '));
  setLasso3dMode(false);
  try {
    setStatus('囲みの中の部材を調べています…', 'busy');
    await ensurePick();
    const r = overlay.getBoundingClientRect();
    const { hits, maskPx } = pickHits(renderer, camera, state.root, state.pick, lasso.pts3d, r.width, r.height);
    Object.assign(lasso, { source: '3d', hits, maskPx, view: viewHint() });
    const res = await call('POST', '/api/lasso/{a}/select', { hits, mask_px: maskPx, view: lasso.view });
    if (lasso.line3d) { lasso.line3d.remove(); lasso.line3d = null; }   // 囲みの線は消し、選んだ部材に色を付ける
    addGroup(res.selection, { source: '3d', view: lasso.view, level: null });
    setStatus(`囲んだ部材 ${res.selection.length} 件`, 'ok');
  } catch (e) {
    setStatus('囲みを読めませんでした：' + e.message, 'err');
    if (lasso.line3d) { lasso.line3d.remove(); lasso.line3d = null; }
    lasso.append = false;
    if (!lasso.groups.length) clearLasso();
  }
});

// 平面図の部材に印を付ける（平面図の多角形には W#・O# の data-id がある）。印を付けられた番号を返す
function markPlan(ids) {
  document.querySelectorAll('#plan [data-id].picked').forEach((el) => el.classList.remove('picked'));
  const found = new Set();
  for (const id of ids) document.querySelectorAll(`#plan [data-id="${CSS.escape(id)}"]`).forEach((el) => { el.classList.add('picked'); found.add(id); });
  return found;
}

// 囲みに少しでもかかった部材を並べる（★11/4 作者「一部だけ囲んだ部材が選ばれない」）。
// 一部だけかかった部材は薄く出し、要らない部材は × で外せる。Claude に頼むときは残した部材だけを送る
// ★11/5 夜：囲みごとに分けて並べる（①②…）。1つだけのときは今までと同じ見た目
const FROM_JA = { S: '南', N: '北', E: '東', W: '西', plan: '上' };
function groupWhere(g) {
  if (g.source === '3d') return `3D（${FROM_JA[g.view] || '斜め'}から）で囲んだ部材`;
  return `平面図${state.multi && g.level ? `（${LEVEL_JA[g.level] || g.level}）` : ''}で囲んだ部材`;
}
function addGroup(rows, meta) {                // 囲みが1つ終わった（rows はサーバーが決めた部材）
  lasso.append = false; syncLassoUi();
  if (!rows.length) {
    if (lasso.groups.length) { setStatus('囲みの中に部材がありません。この囲みは足しませんでした', 'err'); return; }
    lasso.selection = []; markPlan([]);
    $('#lasso-picked').innerHTML = '<span class="hint">囲みの中に部材がありません。壁や窓、軒をぐるっと囲んでください。</span>';
    showLassoPanel(false);
    return;
  }
  lasso.groups.push({ label: nextLabel(), rows, ...meta });
  lasso.seq += 1;
  renderPicked({ focusText: true });
}
function showLassoPanel(has, focusText = false) {
  if (state.ws !== 'fix') setWorkspace('fix');
  const sec = $('#sec-lasso'); sec.hidden = false; sec.open = true;
  $('#lasso-panel').hidden = false; $('#lasso-idle').hidden = true;
  $('#lasso-ask').disabled = !has; $('#lasso-table').disabled = !has;
  $('#lasso-add').disabled = !has; $('#lasso-add').textContent = `囲みを足す（${nextLabel()}）`;
  sec.scrollIntoView({ block: 'nearest' });
  if (has && focusText) $('#lasso-text').focus();
}
function renderPicked({ focusText = false } = {}) {
  lasso.groups = lasso.groups.filter((g) => g.rows.length);
  const all = []; const seen = new Set();
  for (const g of lasso.groups) for (const r of g.rows) if (!seen.has(r.id)) { seen.add(r.id); all.push(r); }
  if (!all.length) { clearLasso(); return; }                 // × で全部外した
  lasso.selection = all;
  lasso.source = lasso.groups[lasso.groups.length - 1].source;
  const multi = lasso.groups.length > 1;
  const box = $('#lasso-picked'); box.innerHTML = '';
  box.insertAdjacentHTML('beforeend', `<b>囲んだ部材${multi ? `（${lasso.groups.length} か所）` : ''}</b>`
    + (all.some((r) => r.partial) ? '<span class="hint">（薄いのは一部だけかかった部材。要らなければ × で外す）</span>' : ''));
  for (const g of lasso.groups) {
    let host = box;
    if (multi) {
      host = document.createElement('div'); host.className = 'pick-grp';
      host.innerHTML = `<div class="grp-head"><span class="grp-no">${esc(g.label)}</span>${esc(groupWhere(g))}</div>`;
      box.appendChild(host);
    }
    for (const r of g.rows) {
      const div = document.createElement('div'); div.className = 'pick-row' + (r.partial ? ' partial' : '');
      div.innerHTML = `<span class="tag">${esc(r.id)}</span><span class="pick-main">${esc(r.label)}　${esc(r.text)}`
        + (r.part ? `<span class="pick-part">模型の部品：${esc(r.part)}</span>` : '') + '</span>';
      const x = document.createElement('button'); x.className = 'unpick'; x.textContent = '×'; x.title = `${r.id} を外す`;
      x.addEventListener('click', () => { g.rows = g.rows.filter((q) => q.id !== r.id); renderPicked(); });
      div.prepend(x); host.appendChild(div);
    }
  }
  markLassoSelection();
  showLassoPanel(true, focusText);
}
let lassoMarkSeq = 0;
async function markLassoSelection() {          // 囲んだ部材に印（平面図）と色（3D）。図を替えたとき・タブに戻ったときも付け直す
  const my = ++lassoMarkSeq;
  markPlan(lasso.selection.map((r) => r.id));
  if (!lasso.selection.length || !state.root || state.focus) return;
  try { await ensurePick(); } catch (e) { return; }
  if (my !== lassoMarkSeq || !lasso.selection.length || state.focus) return;
  highlight3d(lasso.selection);
}

$('#lasso-table').addEventListener('click', () => {   // AI なし：囲んだ部材の表の行を開く（軒は、その辺の軒の出の欄にも印）
  setWorkspace('fix'); $('#sec-tables').open = true;
  document.querySelectorAll('.tables tr.picked').forEach((tr) => tr.classList.remove('picked'));
  document.querySelectorAll('.tables input.picked-input').forEach((i) => i.classList.remove('picked-input'));
  const ids = new Set(lasso.selection.map((r) => r.id.split(':')[0]));
  const rows = [...document.querySelectorAll('.tables tr[data-id]')].filter((tr) => ids.has(tr.dataset.id));
  rows.forEach((tr) => { tr.classList.add('picked'); const d = tr.closest('details'); if (d) d.open = true; });
  for (const r of lasso.selection.filter((x) => x.id.includes(':'))) {
    const [rid, side] = r.id.split(':');
    const inp = document.querySelector(`.tables tr[data-id="${CSS.escape(rid)}"] input[data-field="eave_mm.${side}"]`);
    if (inp) inp.classList.add('picked-input');
  }
  if (rows[0]) rows[0].scrollIntoView({ block: 'center' });
  setStatus(`囲んだ部材 ${rows.length} 件の行に色を付けました。値を変えて「承認」で直せます`, 'ok');
});

// 頼む：囲みごとの部材（ids と groups）と一言を送る。送ったら囲みは空にし、答えは下の「依頼」に出る（★11/5 夜）
async function askLasso() {
  const text = $('#lasso-text').value.trim();
  if (!text) { setStatus('一言を書いてから「Claude に頼む」を押してください', 'err'); $('#lasso-text').focus(); return; }
  if (!lasso.groups.length) return;
  const groups = lasso.groups.map((g) => ({ label: g.label, ids: g.rows.map((r) => r.id), partial: g.rows.filter((r) => r.partial).map((r) => r.id),
    source: g.source, view: g.source === '3d' ? g.view : null }));
  const btn = $('#lasso-ask'); btn.disabled = true;           // 送っている間は押せない（二重に頼まない）
  try {
    const req = await sendFixRequest({ kind: 'lasso', text, ids: lasso.selection.map((r) => r.id), groups });
    if (req) { clearLasso(); $('#lasso-text').value = ''; showRequest(req.id, false); }
  } catch (e) { /* sendFixRequest が知らせた */ }
  finally { btn.disabled = !lasso.groups.length; }
}
$('#lasso-ask').addEventListener('click', () => askLasso());

// ---------------------------------------------------------------- 文字で直す（囲まない。★11/5 夜 作者「囲んで直す以外に文字で直す機能も」）
const focusIdsForText = () => (state.focus ? state.focus.ids.filter((id) => isElementId(id)) : []);
function updateTextFixHint() {
  const ids = focusIdsForText();
  $('#textfix-focus').textContent = ids.length ? `いま選んでいる部材（${ids.join('・')}）も添えます` : '';
}
$('#textfix-text').addEventListener('focus', updateTextFixHint);
$('#textfix-ask').addEventListener('click', async () => {
  const text = $('#textfix-text').value.trim();
  if (!text) { setStatus('一言を書いてから「Claude に頼む」を押してください', 'err'); $('#textfix-text').focus(); return; }
  const btn = $('#textfix-ask'); btn.disabled = true;
  try {
    const req = await sendFixRequest({ kind: 'text', text, ids: focusIdsForText() });
    if (req) { $('#textfix-text').value = ''; showRequest(req.id, false); }
  } catch (e) { /* sendFixRequest が知らせた */ }
  finally { btn.disabled = false; }
});

// ---------------------------------------------------------------- 修正の依頼と Claude の答え（★11/5 夜。サーバーの /api/fix・core/fix_requests.py）
// 頼んだことと答えは物件ごとにサーバーに残る。タブを替えても、物件を開き直しても、一覧を読み直せば見える。
// 考えている依頼があるあいだは数秒ごとに読み直し、答えが届いたらお知らせ（toast）で知らせる（タブは移らない）
const fixState = { alias: null, items: [], loaded: false, loading: false, again: false, cards: new Map(),
  applying: new Set(), answering: new Set() };
const FIX_KIND = { lasso: '囲んで直す', text: '文字で直す' };
const fixUrl = (alias, p = '') => `/api/fix/${encodeURIComponent(alias)}/requests${p}`;
const fixModel = () => ($('#fix-model').value === 'sonnet' ? 'sonnet' : 'opus');
try { const m = localStorage.getItem('hakoya.fixModel'); if (m === 'opus' || m === 'sonnet') $('#fix-model').value = m; } catch (e) { /* 保存できない窓 */ }
$('#fix-model').addEventListener('change', () => { try { localStorage.setItem('hakoya.fixModel', fixModel()); } catch (e) { /* 保存できない窓 */ } });
const statusIsThinking = () => ($('#status').textContent || '').startsWith('Claude が考えています');
const COST_CONFIRM = 'API キーで Claude に頼みます。キーの持ち主に API の料金がかかります（1回の上限 $1.00）。よろしいですか？';

function resetFix() {
  fixState.alias = null; fixState.items = []; fixState.loaded = false;
  for (const c of fixState.cards.values()) c.el.remove();
  fixState.cards.clear(); fixState.applying.clear(); fixState.answering.clear();
  renderFixRequests();
}

async function loadFixRequests() {
  const alias = state.alias;
  if (!alias) { if (fixState.alias) resetFix(); return; }
  if (fixState.loading) { fixState.again = true; return; }
  fixState.loading = true;
  try {
    const res = await call('GET', fixUrl(alias));
    if (alias !== state.alias) return;
    if (fixState.alias !== alias) { resetFix(); fixState.alias = alias; }
    const items = res.requests || [];
    if (fixState.loaded) notifyFixChanges(items);
    fixState.items = items; fixState.loaded = true;
    renderFixRequests();
  } catch (e) { /* 合わせる前のサーバー・つながらない：次に読み直す */ }
  finally {
    fixState.loading = false;
    if (fixState.again) { fixState.again = false; loadFixRequests(); }
  }
}

function notifyFixChanges(items) {             // 考えていた依頼が終わった：お知らせ（修正タブ以外なら「見る」）
  const before = new Map(fixState.items.map((r) => [r.id, r.status]));
  for (const r of items) {
    if (before.get(r.id) !== 'running' || r.status === 'running') continue;
    const look = state.ws === 'fix' ? null : { label: '見る', fn: () => showRequest(r.id) };
    if (r.status === 'done') {
      const n = (r.proposals || []).length; const q = (r.questions || []).length;
      const what = n ? `直す案 ${n} 件` : q ? `質問 ${q} 件` : '直す案はありませんでした';
      toast(`Claude の答えが届きました（${r.id}：${what}）`, { kind: 'ok', action: look });
      if (statusIsThinking()) setStatus(`Claude の答えが届きました${r.cost_usd ? `（費用の目安 $${Number(r.cost_usd).toFixed(2)}）` : ''}`, 'ok');
    } else {
      toast(`${r.id} は答えが届きませんでした：${r.error || ''}`, { kind: 'err', action: look });
      if (statusIsThinking()) setStatus('頼めませんでした：' + (r.error || ''), 'err');
    }
  }
}

function upsertRequest(req) {
  const i = fixState.items.findIndex((r) => r.id === req.id);
  if (i >= 0) fixState.items[i] = req; else fixState.items.unshift(req);
  renderFixRequests();
}

function showRequest(rid, switchTab = true) {   // その依頼のカードを見せる
  if (switchTab && state.ws !== 'fix') setWorkspace('fix');
  const s = $('#sec-requests'); s.open = true;
  const card = $(`#fix-requests [data-rid="${CSS.escape(rid)}"]`); if (!card) return;
  card.scrollIntoView({ block: 'nearest' });
  card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash');
}

// 頼む（囲んで直す・文字で直す・検品の指摘から）。今の接続とモデルを使う。返すのは依頼（やめたら null）
async function sendFixRequest(body, { toastText = '' } = {}) {
  const alias = state.alias;
  if (!alias || !state.built) { const m = '物件を開いて組み立ててから頼んでください'; setStatus(m, 'err'); throw new Error(m); }
  const connection = body.connection || $('#lasso-conn').value;
  if (connection === 'api_key' && !confirm(COST_CONFIRM)) return null;
  let req;
  try { req = await call('POST', fixUrl(alias), { ...body, connection, model: body.model || fixModel() }); }
  catch (e) { setStatus('頼めませんでした：' + e.message, 'err'); toast('頼めませんでした：' + e.message, { kind: 'err' }); throw e; }
  if (alias === state.alias) {
    if (fixState.alias !== alias) { resetFix(); fixState.alias = alias; }
    upsertRequest(req); loadFixRequests();
  }
  setStatus('Claude が考えています（30秒〜数分）…', 'busy');      // 下の帯は今までどおり（作者「表記はそのままでいい」）
  if (toastText) toast(toastText, { kind: 'info', action: state.ws === 'fix' ? null : { label: '見る', fn: () => showRequest(req.id) } });
  return req;
}
// 検品の担当が「直す案のない指摘の承認」で呼ぶ。タブは移らない
window.createFixRequest = async ({ text, ids = [], groups = null, findingId = null, kind = 'text' } = {}) => {
  const body = { kind, text: String(text || ''), ids: (ids || []).map(String), finding_id: findingId };
  if (groups) body.groups = groups;
  return sendFixRequest(body, { toastText: '直す案を Claude に頼みました（修正タブ）' });
};

function renderFixRequests() {                 // カードは依頼ごとに作り、中身が変わったものだけ作り直す（書きかけの答えを消さない）
  const ul = $('#fix-requests'); const items = fixState.items;
  $('#fix-empty').hidden = items.length > 0;
  $('#n-requests').textContent = items.length ? String(items.length) : '';
  const keep = new Set(items.map((r) => r.id));
  for (const [id, c] of fixState.cards) if (!keep.has(id)) { c.el.remove(); fixState.cards.delete(id); }
  items.forEach((r, i) => {
    const marks = [...fixState.applying, ...fixState.answering].filter((k) => k.startsWith(r.id + ':'));
    const sig = JSON.stringify([r.status, r.message, r.error, r.proposals, r.questions, r.selection, marks]);
    let c = fixState.cards.get(r.id);
    if (!c || c.sig !== sig) {
      const el = fixCard(r);
      if (c) c.el.replaceWith(el);
      c = { el, sig }; fixState.cards.set(r.id, c);
    }
    if (ul.children[i] !== c.el) ul.insertBefore(c.el, ul.children[i] || null);
  });
  updateElapsed();
}

function updateElapsed() {                     // 考えている依頼の経過の秒
  for (const el of $$('#fix-requests .fr-sec[data-at]')) {
    const s = Math.max(0, Math.round((Date.now() - Date.parse(el.dataset.at)) / 1000));
    el.textContent = s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  }
}

function fixCard(req) {
  const li = document.createElement('li'); li.className = `fix-req st-${req.status}`; li.dataset.rid = req.id;
  const head = document.createElement('div'); head.className = 'fr-head';
  head.innerHTML = `<span class="fr-id">${esc(req.id)}</span><span class="fr-kind">${esc(FIX_KIND[req.kind] || req.kind)}</span>`
    + `<span class="fr-model" title="直す案を考えるモデル">${esc(req.model_label || '')}</span>`
    + (req.fake ? '<span class="fr-fake" title="HAKOYA_FAKE_AI=1 の試し。Claude は呼んでいません">試し</span>' : '')
    + `<span class="fr-time">${esc(fmtTime(req.at))}</span>`;
  if (req.status !== 'running') {
    const x = document.createElement('button'); x.className = 'fr-x'; x.textContent = '×'; x.title = 'この依頼を一覧から外す（直した修正はそのまま）';
    x.addEventListener('click', () => deleteRequest(req));
    head.appendChild(x);
  }
  li.appendChild(head);
  li.insertAdjacentHTML('beforeend', `<p class="fr-text">${esc(req.text)}</p>`);
  const sel = req.selection || [];
  if (sel.length) {
    const box = document.createElement('div'); box.className = 'fr-sel';
    const chip = (id) => `<button class="idc" data-id="${esc(id)}" title="${esc(id)} を模型で光らせる">${esc(id)}</button>`;
    const groups = (req.groups || []).filter((g) => (g.ids || []).length);
    if (groups.length > 1) box.innerHTML = groups.map((g) => `<span class="grp"><span class="grp-no">${esc(g.label)}</span>${g.ids.map(chip).join('')}</span>`).join('');
    else box.innerHTML = `<span class="fr-lbl">${req.kind === 'text' ? '選んでいた部材' : '囲んだ部材'}</span>${sel.map((r) => chip(r.id)).join('')}`;
    li.appendChild(box);
  }
  if (req.status === 'running') {               // 待っている間（★11/5 夜 作者「Claude が考えています、が分かりにくい」）
    li.insertAdjacentHTML('beforeend', '<div class="fr-wait" role="status"><span class="fr-spin" aria-hidden="true"></span><div class="fr-wait-main">'
      + `<div class="fr-wait-title">Claude（${esc(req.model_label || 'Opus 5.5')}）が直す案を考えています<span class="fr-sec" data-at="${esc(req.at)}"></span></div>`
      + '<div class="fr-wait-hint">ふつう 10〜60 秒。ほかの操作をしても大丈夫です（答えはここに残ります）</div></div></div>');
    return li;
  }
  if (req.status !== 'done') {
    li.insertAdjacentHTML('beforeend', `<p class="err-text fr-err">${esc(req.error || '答えが届きませんでした')}</p>`);
    return li;
  }
  if (req.message) li.insertAdjacentHTML('beforeend', `<p class="msg fr-msg">${esc(req.message)}</p>`);
  (req.proposals || []).forEach((p, i) => li.appendChild(propEl(req, p, i)));
  for (const q of req.questions || []) li.appendChild(questionEl(req, q));
  if (!(req.proposals || []).length && !(req.questions || []).length)
    li.insertAdjacentHTML('beforeend', '<p class="hint">直す案も質問もありませんでした。一言を変えて、もう一度頼んでください。</p>');
  return li;
}

const proposalElementId = (p) => { const f = String(p.field || ''); return f.startsWith('eave_mm.') ? `${p.target}:${f.slice(8)}` : String(p.target); };
const changeText = (p) => (p.field === '_delete' ? '取り除いた' : `${FIELD[p.field] || p.field} ${p.from} → ${p.to}`);
const doneText = (p) => `${p.target} ${changeText(p)}`;

function propEl(req, p, i) {                   // 直す案のカード：押すと光る。承認・却下・直した印
  const applying = fixState.applying.has(`${req.id}:${i}`);
  const st = applying ? 'applying' : (p.state || 'pending');
  const div = document.createElement('div'); div.className = `prop ps-${st}`; div.dataset.i = String(i);
  const line = document.createElement('div'); line.className = 'prop-line'; line.tabIndex = 0; line.setAttribute('role', 'button');
  line.title = '押すと、模型と平面図でこの部材を光らせます';
  line.innerHTML = `<span class="prop-no">案${i + 1}</span><span class="prop-what">${proposalLine(p)}</span>`;
  line.addEventListener('click', () => focusProposal(req, p, div));
  line.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); focusProposal(req, p, div); } });
  div.appendChild(line);
  div.insertAdjacentHTML('beforeend', `<div class="meta">理由：${esc(p.reason || '')}　数値の出どころ：${esc(sourceText(p.value_source))}</div>`);
  const acts = document.createElement('div'); acts.className = 'prop-acts';
  if (st === 'pending' || st === 'failed') {
    const ok = document.createElement('button'); ok.className = 'primary'; ok.textContent = st === 'failed' ? 'もう一度承認' : '承認';
    ok.addEventListener('click', () => approveProposal(req, i));
    const no = document.createElement('button'); no.className = 'quiet'; no.textContent = '却下';
    no.addEventListener('click', () => rejectProposal(req, i));
    acts.append(ok, no);
    if (p.note) acts.insertAdjacentHTML('beforeend', `<span class="${st === 'failed' ? 'err-text' : 'prop-note'}">${esc(p.note)}</span>`);
  } else if (st === 'applying') {
    acts.innerHTML = '<button class="primary" disabled>直しています…</button>';
  } else if (st === 'approved') {
    acts.innerHTML = `<span class="prop-done">直しました ✓（${esc(doneText(p))}）</span>`;
    const undo = document.createElement('button'); undo.className = 'quiet'; undo.textContent = '取り消す';
    undo.title = 'この直しをやめて、模型を組み直します';
    undo.addEventListener('click', () => undoApproved(p, fixState.alias));
    acts.appendChild(undo);
  } else if (st === 'rejected') {
    acts.innerHTML = '<span class="prop-off">却下しました</span>';
  } else if (st === 'no_change') {
    acts.innerHTML = `<span class="prop-warn">${esc(p.note || 'この直しでは模型が変わりませんでした（取り消しました）')}</span>`;
  }
  div.appendChild(acts);
  return div;
}

async function focusProposal(req, p, el) {    // 案を押す：その部材を 3D・平面図で光らせ、右上の欄に今の値を出す
  $$('#fix-requests .prop.focus').forEach((x) => x.classList.remove('focus'));
  el.classList.add('focus');
  if (fixState.alias !== state.alias) return;
  const id = proposalElementId(p);
  if (!findElement(id)) { setStatus(`${id} は、この家の部材の中に見つかりません（組み立て直したのかもしれません）`, 'err'); return; }
  await selectElement(id, { label: `${req.id} の案` });
}

async function approveProposal(req, i) {
  const alias = fixState.alias; const key = `${req.id}:${i}`;
  if (!alias || alias !== state.alias || fixState.applying.has(key)) return;
  const p = req.proposals[i];
  fixState.applying.add(key); renderFixRequests();              // 押した瞬間に「直しています…」（押せなくする）
  const busy = toast(`${p.target} を直しています…`, { kind: 'busy' });
  setStatus('修正を記録して、模型を組み直しています…', 'busy');
  let res;
  try {
    res = await call('POST', fixUrl(alias, `/${encodeURIComponent(req.id)}/approve`), { index: i });
  } catch (e) {
    busy.close(); fixState.applying.delete(key);
    toast('直せませんでした：' + e.message, { kind: 'err' }); setStatus('直せませんでした：' + e.message, 'err');
    await loadFixRequests(); renderFixRequests();
    return;
  }
  busy.close(); fixState.applying.delete(key);
  upsertRequest(res.request);                                    // ほかの案はそのまま。タブも移らない
  const np = res.request.proposals[i] || p;
  if (!res.changed) {
    toast('この直しでは模型が変わりませんでした（取り消しました）', { kind: 'info', ms: 7000 });
    setStatus(`${np.target}：この直しでは模型が変わりませんでした（取り消しました）`);
    await syncVersion();                                         // 組み直したファイルで「ほかの所で直された」と読み直さない
    return;
  }
  toast(`${np.target} を直しました（${changeText(np)}）`, { kind: 'ok', action: { label: '取り消す', fn: () => undoApproved(np, alias) } });
  try { await refresh(); } catch (e) { setStatus('読み直せませんでした：' + e.message, 'err'); return; }
  setStatus(`${np.target} を直しました（直しの履歴から取り消せます）`, 'ok');
  if (findElement(proposalElementId(np))) await selectElement(proposalElementId(np), { frame: false, quiet: true, label: '直した部材' });
}

async function rejectProposal(req, i) {
  const alias = fixState.alias; if (!alias || alias !== state.alias) return;
  try { const res = await call('POST', fixUrl(alias, `/${encodeURIComponent(req.id)}/reject`), { index: i }); upsertRequest(res.request); }
  catch (e) { toast('却下できませんでした：' + e.message, { kind: 'err' }); }
}

async function undoApproved(p, alias) {        // 承認した案の「取り消す」（直しの履歴の取り消すと同じ）
  if (!p.override_id) return;
  if (alias !== state.alias) { toast('別の物件を開いているので、取り消せません', { kind: 'err' }); return; }
  try {
    setStatus('取り消して組み直しています…', 'busy');
    await call('DELETE', `/api/overrides/${encodeURIComponent(alias)}/${encodeURIComponent(p.override_id)}`);
    await loadFixRequests();
    await refresh();
    setStatus(`${p.override_id} を取り消しました`, 'ok');
    toast(`${p.target} の直しを取り消しました`, { kind: 'info' });
  } catch (e) { setStatus('取り消せませんでした：' + e.message, 'err'); toast('取り消せませんでした：' + e.message, { kind: 'err' }); }
}

function questionEl(req, q) {                  // 質問：選択肢に番号（1. 2. 3.）。番号だけの答えでも通じる（サーバーが選択肢の文を付ける）
  const key = `${req.id}:${q.id}`; const sending = fixState.answering.has(key);
  const div = document.createElement('div'); div.className = 'question' + (q.answered ? ' answered' : '');
  div.innerHTML = `<b>質問</b>　${esc(q.question)}${q.evidence ? `<div class="meta">根拠：${esc(q.evidence)}</div>` : ''}`;
  if (q.answered) {
    div.insertAdjacentHTML('beforeend', `<div class="q-ans">答えました：${esc(q.answered)}${q.child_id ? `（→ ${esc(q.child_id)}）` : ''}</div>`);
    return div;
  }
  const opts = document.createElement('div'); opts.className = 'opts';
  (q.options || []).forEach((opt, k) => {
    const b = document.createElement('button'); b.textContent = `${k + 1}. ${opt}`; b.disabled = sending;
    b.addEventListener('click', () => answerQuestion(req, q, `${k + 1}. ${opt}`));
    opts.appendChild(b);
  });
  div.appendChild(opts);
  // 選択肢にない答え（図面の数値など）は書いて答える。数値は人が書いたものだけが案に使われる
  const ans = document.createElement('div'); ans.className = 'answer';
  const inp = document.createElement('input'); inp.type = 'text'; inp.placeholder = '答えを書く（番号でも。例：2／図面の軒の出は 450）'; inp.disabled = sending;
  const send = document.createElement('button'); send.textContent = sending ? '送っています…' : '答える'; send.disabled = sending;
  send.addEventListener('click', () => answerQuestion(req, q, inp.value));
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) answerQuestion(req, q, inp.value); });
  ans.append(inp, send); div.appendChild(ans);
  return div;
}

async function answerQuestion(req, q, answer) {
  const text = String(answer || '').trim();
  if (!text) { setStatus('答えを書くか、選択肢を押してください', 'err'); return; }
  const alias = fixState.alias; const key = `${req.id}:${q.id}`;
  if (!alias || alias !== state.alias || fixState.answering.has(key)) return;
  if (req.connection === 'api_key' && !confirm(COST_CONFIRM)) return;
  fixState.answering.add(key); renderFixRequests();             // 送っている間は、選択肢も「答える」も押せない
  try {
    const child = await call('POST', fixUrl(alias, `/${encodeURIComponent(req.id)}/answer`), { question_id: q.id, answer: text });
    upsertRequest(child);
    setStatus('Claude が考えています（30秒〜数分）…', 'busy');
  } catch (e) { toast('答えを送れませんでした：' + e.message, { kind: 'err' }); setStatus('答えを送れませんでした：' + e.message, 'err'); }
  finally { fixState.answering.delete(key); await loadFixRequests(); renderFixRequests(); }
}

async function deleteRequest(req) {
  const alias = fixState.alias; if (!alias || alias !== state.alias) return;
  try {
    await call('DELETE', fixUrl(alias, `/${encodeURIComponent(req.id)}`));
    fixState.items = fixState.items.filter((r) => r.id !== req.id); renderFixRequests();
  } catch (e) { toast('外せませんでした：' + e.message, { kind: 'err' }); }
}

// 物件が替わったら一覧を読み直す。考えている依頼があるあいだは 2.5 秒ごとに読み直す
setInterval(() => {
  if (fixState.alias !== state.alias) { if (state.alias) loadFixRequests(); else if (fixState.alias) resetFix(); return; }
  if (fixState.items.some((r) => r.status === 'running')) loadFixRequests();
  if (state.ws === 'fix') updateTextFixHint();
}, 2500);
setInterval(updateElapsed, 1000);

// ---------------------------------------------------------------- 光らせる：指摘の見出し・左の一覧・番号の札（3D と平面図）
// state.focus = { ids, primary, label }。refresh() で消す（組み直すと形が変わるため）。タブを替えても残す
let focusSeq = 0;
// 当たり判定の形：いま開いている家（state.building）のもの。読んでいる間に組み直したら、新しい家の形を読み直す
async function ensurePick() {
  for (;;) {
    const b = state.building;
    if (state.pick && state.pickFor === b) return state.pick;
    if (!state.pickReq || state.pickReq.b !== b) {
      const req = { b, p: call('GET', '/api/pick/{a}') };
      req.p.catch(() => { if (state.pickReq === req) state.pickReq = null; });   // しくじったら、次はもう一度読む
      state.pickReq = req;
    }
    const data = await state.pickReq.p;
    if (state.building !== b) continue;
    if (!state.pick || state.pickFor !== b) { state.pick = buildPickGroup(data); state.pickFor = b; }
    return state.pick;
  }
}
function expandIds(ids) {          // 屋根 Y# は、その軒 Y#:* も一緒に光らせる
  const out = new Set(ids.map(String));
  if (state.pick) for (const m of state.pick.children) { const id = String(m.userData.id); if (id.includes(':') && out.has(baseId(id))) out.add(id); }
  return [...out];
}
function findElement(id) {
  const b = state.building; if (!b || !id) return null;
  const [rid, side] = String(id).split(':');
  for (const [coll, kind] of [['walls', 'wall'], ['openings', 'opening'], ['roofs', 'roof']]) {
    const el = (b[coll] || []).find((e) => e.id === rid);
    if (el) return { kind, el, side: side || null };
  }
  return null;
}
function levelOfIds(ids) {
  for (const id of ids) { const f = findElement(id); if (f && f.kind !== 'roof' && f.el.level) return f.el.level; }
  return null;
}
function frameOn(box) {             // 部材へ寄る。家の外側の部材は外から、真ん中の部材はいまの向きのまま
  const c = box.getCenter(new THREE.Vector3()); const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 6);
  const house = new THREE.Box3().setFromObject(state.root); const hc = house.getCenter(new THREE.Vector3());
  const hr = house.getSize(new THREE.Vector3()).length() / 2;
  let d = new THREE.Vector3(c.x - hc.x, c.y - hc.y, 0);
  if (d.length() < hr * 0.18) { d = camera.position.clone().sub(controls.target); d.z = 0; }
  if (d.lengthSq() < 1e-6) d.set(-0.5, -1, 0);
  d.normalize(); d.z = 0.8; d.normalize();
  const dist = Math.max(r / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 2.0, hr * 2.4, 140);   // 家のどこか分かるように、家の全体が見える近さ
  flyTo(c.clone().add(d.multiplyScalar(dist)), c, 420);
}
async function applyFocus({ frame = false, quiet = false } = {}) {
  const f = state.focus; if (!f) return;
  const my = ++focusSeq;
  const known = f.ids.filter((id) => isElementId(id));
  let in3d = new Set(); let inPlan = new Set();
  // ★11/5 夜：パーツを離したままでも光らせる（戻さない。作者「パーツを離した状態でも部品を触れるようにし、戻らないようにする」）
  if (state.root && state.built && known.length) {
    try { await ensurePick(); } catch (e) { setStatus('部材の形を読めませんでした：' + e.message, 'err'); return; }
    if (my !== focusSeq || state.focus !== f) return;
    const ids = expandIds(known);
    highlight3d(ids.map((id) => ({ id })));
    const want = new Set(ids);
    in3d = new Set(state.pick.children.filter((m) => want.has(String(m.userData.id))).map((m) => baseId(m.userData.id)));
    if (frame && state.hl && state.hl.children.length) frameOn(new THREE.Box3().setFromObject(state.hl));
  }
  if (state.multi && known.length) {                       // 2階建て：平面図の階を、部材の階に合わせる
    const lv = levelOfIds(known);
    if (lv && LEVEL_JA[lv] && $('#plan-level').value !== lv) {
      $('#plan-level').value = lv;
      try { await loadPlan(); } catch (e) { /* 平面図は次に開いたときに読む */ }
      if (my !== focusSeq || state.focus !== f) return;
    }
  }
  inPlan = markPlan(known);
  markLit();
  const roofsOnly3d = state.pane === 'plan' && f.ids.some((id) => /^Y\d+/.test(id));   // 屋根は平面図に出ない
  if (quiet) { if (roofsOnly3d && !inPlan.size) setStatus('屋根は平面図には出ないので、3D で見てください'); return; }
  const shown = f.ids.filter((id) => in3d.has(baseId(id)) || inPlan.has(id));
  const missing = f.ids.filter((id) => !shown.includes(id));
  let msg = shown.length ? `${f.label ? f.label + '：' : ''}${shown.join('・')} を光らせました` : `${f.label ? f.label + '：' : ''}模型の上で場所を示せる部材がありません`;
  if (shown.length && missing.length) msg += `（${missing.join('・')} は場所を示せません）`;
  if (roofsOnly3d) msg += '。屋根は平面図には出ないので、3D で見てください';
  setStatus(msg, shown.length ? 'ok' : '');
}
function reapplyFocus() { if (state.focus) applyFocus({ frame: false, quiet: true }); else markLit(); }
function clearFocus() {
  if (!state.focus) return;
  state.focus = null; clearHighlight3d(); markPlan([]); renderInspector(); markLit();
}
async function focusFinding(f) {
  const ids = (f.targets || []).map(String).filter(Boolean);
  if (!ids.length) { setStatus(`${f.id} には、模型の上で示せる部材がありません`); return; }
  if (lasso.selection.length || lasso.line || lasso.line3d) clearLasso();
  state.focus = { ids, primary: ids.find((id) => findElement(id)) || null, label: f.id, fid: f.id };
  renderInspector(); revealInTree(state.focus.primary);
  await applyFocus({ frame: true });
}
async function selectElement(id, { frame = true, quiet = false, label = '' } = {}) {
  if (!findElement(id)) { setStatus(`${id} は、この家の部材の中に見つかりません`, 'err'); return; }
  if (lasso.selection.length || lasso.line || lasso.line3d) clearLasso();
  state.focus = { ids: [String(id)], primary: String(id), label: label || '' };
  renderInspector(); revealInTree(id);
  await applyFocus({ frame, quiet });
}
// 光っている部材の印：左の一覧の行・指摘のカード・番号の札・表の行
function markLit() {
  const f = state.focus; const lit = new Set(f ? f.ids.map(baseId) : []);
  $$('#tree .el').forEach((r) => { r.classList.toggle('lit', lit.has(r.dataset.id)); r.classList.toggle('sel', !!f && baseId(f.primary) === r.dataset.id); });
  $$('.idc[data-id]').forEach((c) => c.classList.toggle('lit', lit.has(baseId(c.dataset.id))));
  $$('.cards .finding').forEach((li) => li.classList.toggle('focus', !!f && f.fid === li.dataset.fid));
  $$('.tables tr[data-id]').forEach((tr) => tr.classList.toggle('lit', lit.has(tr.dataset.id)));
}

// ---------------------------------------------------------------- 押して選ぶ・外す（3D と平面図。★11/5 夜 作者の要望）
// 「パーツを離した状態でも部品を触れるように」「修正時に選択すると戻せないから、真ん中の操作場所をクリックすると選択が外れるように」。
// 押して（動かさずに）離すと、そこに見えている部材を選ぶ（左の一覧の行を押したときと同じ選び方）。何もない所なら外す。Esc でも外す
const CLICK_PX = 5;                 // これより動かしたら「回した・動かした」とみなす（押したことにしない）
function tagText(id) {              // 部材の名前の札（例：W2 外壁・O3 窓・Y1 屋根・Y1 南の軒）
  const f = findElement(id); if (!f) return String(id);
  const name = f.kind === 'roof' ? (f.side ? `${SIDE[f.side]}の軒` : roofShort(f.el)) : (KIND[f.el.kind] || f.el.kind);
  return `${f.el.id} ${name}`;
}
function clearSelection() {         // 選んだ部材と囲み（修正担当の clearLassoSelection があれば）を外す
  const had = !!state.focus;
  clearFocus();
  window.clearLassoSelection?.();
  if (had) setStatus('選んだ部材を外しました');
}

// 3D：押した所の部材（pick3d.js の pickAt。パーツを離していても、離した位置で当たる）
const press3d = { down: null, seq: 0 };
renderer.domElement.addEventListener('pointerdown', (e) => {
  press3d.down = e.button === 0 && !lasso.on3d ? { x: e.clientX, y: e.clientY, id: e.pointerId } : null;
});
renderer.domElement.addEventListener('pointerup', (e) => {
  const d = press3d.down; press3d.down = null;
  if (!d || e.button !== 0 || d.id !== e.pointerId || lasso.on3d) return;
  if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > CLICK_PX) return;     // 回した
  pick3dAt(e.clientX, e.clientY);
});
async function pick3dAt(cx, cy) {
  if (!state.root || !state.built) return;
  const my = ++press3d.seq;
  let pick;
  try { pick = await ensurePick(); } catch (e) { setStatus('部材の形を読めませんでした：' + e.message, 'err'); return; }
  if (my !== press3d.seq || !state.root || state.pane !== '3d') return;
  const r = renderer.domElement.getBoundingClientRect();
  const id = pickAt(renderer, camera, state.root, pick, cx - r.left, cy - r.top, r.width, r.height);
  if (id == null || !findElement(id)) { clearSelection(); return; }
  await selectElement(id, { frame: false, quiet: true });
  setStatus(`${tagText(id)} を選びました（何もない所を押すか Esc で外れます）`, 'ok');
}

// 3D の名前の札：選んだ部材の上に「W2 外壁」。部材の箱の上の真ん中を毎回、画面の位置に直す（回しても付いていく）
const tags3d = document.createElement('div'); tags3d.className = 'tags3d'; tags3d.setAttribute('aria-hidden', 'true');
$('#v3d').appendChild(tags3d);
const TAG_MAX = 6;
const tagV = new THREE.Vector3();
function updateTags() {
  const f = state.focus; const jishas = state.hlAnchors;
  const items = [];
  if (f && jishas && state.hl) {
    for (const id of f.ids) {
      const a = jishas.get(String(id)) || jishas.get(baseId(id));
      if (a && items.length < TAG_MAX) items.push([String(id), a]);
    }
  }
  while (tags3d.children.length > items.length) tags3d.lastElementChild.remove();
  while (tags3d.children.length < items.length) { const el = document.createElement('div'); el.className = 'tag3d'; tags3d.appendChild(el); }
  const w = holder.clientWidth, h = holder.clientHeight;
  items.forEach(([id, list], k) => {
    const el = tags3d.children[k];
    const text = tagText(id); if (el.textContent !== text) el.textContent = text;
    let at = null;
    for (const a of list) {            // カメラの後ろ・画面の外の所は使わない
      tagV.copy(a).project(camera);
      const x = (tagV.x + 1) / 2 * w, y = (1 - tagV.y) / 2 * h;
      if (tagV.z <= 1 && x >= 0 && x <= w && y >= 24 && y <= h) { at = [x, y]; break; }
    }
    el.hidden = !at;
    if (at) el.style.transform = `translate(${at[0].toFixed(1)}px, ${at[1].toFixed(1)}px) translate(-50%, calc(-100% - 7px))`;
  });
}

// Esc：選んだ部材と囲みを外す（囲むモード・小窓・メニュー・入力中の Esc は、それぞれのものを先に）
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' || e.ctrlKey || e.altKey || e.metaKey) return;
  if (lasso.on || lasso.on3d || document.querySelector('dialog[open]') || !$('#file-pop').hidden) return;
  if (e.target?.closest?.('input, textarea, select, [contenteditable="true"]')) return;
  clearSelection();
}, true);

// ---------------------------------------------------------------- 左：部品と部材
const TREE_KINDS = [['walls', '壁', 'i-wall'], ['openings', '窓・ドア', 'i-opening'], ['roofs', '屋根', 'i-roof']];
function elRow(kind, x, depth) {
  let name = '', val = '', title = '';
  if (kind === 'walls') { name = KIND[x.kind] || x.kind; val = x.thickness_mm != null ? String(num(x.thickness_mm)) : ''; title = `${x.id}：${name}・厚み ${val}mm`; }
  if (kind === 'openings') { name = KIND[x.kind] || x.kind; val = x.width_mm != null ? String(num(x.width_mm)) : ''; title = `${x.id}：${name}（${x.wall || ''}）・幅 ${val}mm`; }
  if (kind === 'roofs') {
    name = roofShort(x);
    const p = x.pitch || []; val = p.length ? (Number(p[1]) === 10 ? `${p[0]}寸` : `${p[0]}/${p[1]}`) : '';
    title = `${x.id}：${roofKind(x)}${val ? `・勾配 ${val}` : ''}`;
  }
  const fixed = state.overrides.some((o) => baseId(o.target) === x.id);
  return `<button class="trow el" role="treeitem" data-id="${esc(x.id)}" style="--d:${depth}" title="${esc(title)}">`
    + `<span class="tid">${esc(x.id)}</span><span class="tname">${esc(name)}</span>${fixed ? '<span class="mark-fix" title="人が直した部材"></span>' : ''}<span class="tval">${esc(val)}</span></button>`;
}
function renderTree() {
  const t = $('#tree');
  if (!state.alias || !state.building) { t.innerHTML = ''; return; }
  const b = state.building; const q = $('#tree-filter').value.trim().toLowerCase();
  const open = (k) => (q ? true : state.treeOpen.has(k));
  const head = (key, label, icon, count, depth = 0) => `<button class="trow thead" data-group="${key}" aria-expanded="${open(key)}" style="--d:${depth}">`
    + `<svg class="ic chev"><use href="#i-chev"/></svg>${icon ? `<svg class="ic ic-s"><use href="#${icon}"/></svg>` : ''}<span>${label}</span>${count != null ? `<span class="tcount">${count}</span>` : ''}</button>`;
  let html = '';
  if (!q) {   // 部品（目のボタンで隠す・出す）
    html += head('parts', '部品', 'i-parts', null) + `<div class="tkids"${open('parts') ? '' : ' hidden'}>`
      + partGroups().map(([k, label, long]) => {
        const off = state.hidden.has(k);
        return `<div class="trow part${off ? ' off' : ''}" style="--d:1" title="${esc(long)}"><button class="eye" data-part="${esc(k)}" aria-pressed="${!off}" title="${off ? `${esc(label)}を出す` : `${esc(label)}を隠す`}">`
          + `<svg class="ic"><use href="#${off ? 'i-eye-off' : 'i-eye'}"/></svg></button><span class="tname">${esc(label)}</span></div>`;
      }).join('') + '</div>';
  }
  let hits = 0;
  for (const [kind, label, icon] of TREE_KINDS) {
    const all = b[kind] || [];
    const items = q ? all.filter((x) => `${x.id} ${KIND[x.kind] || ''} ${kind === 'roofs' ? roofShort(x) : ''}`.toLowerCase().includes(q)) : all;
    if (!items.length) continue;
    hits += items.length;
    html += head(kind, label, icon, items.length) + `<div class="tkids"${open(kind) ? '' : ' hidden'}>`;
    if (state.multi && kind !== 'roofs') {
      for (const lv of ['1F', '2F']) {
        const mine = items.filter((x) => x.level === lv); if (!mine.length) continue;
        const key = `${kind}:${lv}`;
        html += head(key, LEVEL_JA[lv], '', mine.length, 1) + `<div class="tkids"${open(key) ? '' : ' hidden'}>` + mine.map((x) => elRow(kind, x, 2)).join('') + '</div>';
      }
      const rest = items.filter((x) => !['1F', '2F'].includes(x.level));
      html += rest.map((x) => elRow(kind, x, 1)).join('');
    } else html += items.map((x) => elRow(kind, x, 1)).join('');
    html += '</div>';
  }
  if (q && !hits) html += `<div class="nohit">「${esc(q)}」に当たる部材はありません</div>`;
  t.innerHTML = html;
  markLit();
}
$('#tree').addEventListener('click', (e) => {
  const eye = e.target.closest('.eye');
  if (eye) {
    const k = eye.dataset.part; if (state.hidden.has(k)) state.hidden.delete(k); else state.hidden.add(k);
    applyVisibility(); renderTree(); return;
  }
  const h = e.target.closest('.thead');
  if (h) { const k = h.dataset.group; if (state.treeOpen.has(k)) state.treeOpen.delete(k); else state.treeOpen.add(k); renderTree(); return; }
  const r = e.target.closest('.el');
  if (r) selectElement(r.dataset.id);
});
$('#tree').addEventListener('keydown', (e) => {   // 上下の矢印で行を動く
  if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
  const rows = $$('#tree .trow').map((r) => (r.classList.contains('part') ? r.querySelector('.eye') : r)).filter((r) => r && r.offsetParent);
  const k = rows.indexOf(document.activeElement); if (k < 0) return;
  e.preventDefault(); const next = rows[Math.max(0, Math.min(rows.length - 1, k + (e.key === 'ArrowDown' ? 1 : -1)))]; next.focus();
});
$('#tree-filter').addEventListener('input', renderTree);
function revealInTree(id) {        // 選んだ部材の行が見えるように、まとまりを開いてそこへ動く
  const f = findElement(id); if (!f) return;
  const kind = { wall: 'walls', opening: 'openings', roof: 'roofs' }[f.kind];
  let changed = !state.treeOpen.has(kind); state.treeOpen.add(kind);
  if (state.multi && kind !== 'roofs' && f.el.level) { const k = `${kind}:${f.el.level}`; changed = changed || !state.treeOpen.has(k); state.treeOpen.add(k); }
  if (changed) renderTree(); else markLit();
  const row = $(`#tree .el[data-id="${CSS.escape(f.el.id)}"]`); if (row) row.scrollIntoView({ block: 'nearest' });
}

// ---------------------------------------------------------------- 右：選んだ部材の性質（修正タブでは直せる。表と同じ窓口）
function renderInspector() {
  const box = $('#sel-panel');
  const f = state.focus; const found = f && f.primary ? findElement(f.primary) : null;
  if (!f || !state.building || (!found && !(f.ids || []).length)) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  const editable = state.ws === 'fix';
  const others = f.ids.length > 1 ? `<div class="sel-ids"><span class="lbl">${esc(f.label || '')} の対象</span>${f.ids.map((id) => `<button class="idc lit${id === f.primary ? ' cur' : ''}" data-pick="${esc(id)}" title="${esc(id)} の性質を見る">${esc(id)}</button>`).join('')}</div>` : '';
  if (!found) {
    box.innerHTML = `<div class="sel-head"><span class="what">${esc(f.label || '選んだ部材')}</span><div class="acts"><button class="icon-btn" data-act="clear" title="印を消す"><svg class="ic"><use href="#i-close"/></svg></button></div></div>${others}`
      + '<p class="sel-note">この番号の部材は、家の中に見つかりません。</p>';
    return;
  }
  const { kind, el, side } = found;
  const rows = [];   // [ラベル, 値の HTML か {field, value, step?, options?}, 単位, 強調]
  const lv = el.level ? (LEVEL_JA[el.level] || el.level) : '';
  let what = '';
  if (kind === 'wall') {
    what = KIND[el.kind] || el.kind;
    const len = el.start && el.end ? Math.round(Math.hypot(el.end[0] - el.start[0], el.end[1] - el.start[1])) : null;
    rows.push(['厚み', { field: 'thickness_mm', value: el.thickness_mm }, 'mm']);
    if (len) rows.push(['長さ', esc(len), 'mm']);
    rows.push(['下端', esc(el.bottom_mm ?? ''), el.bottom_mm != null ? 'mm' : '']);
    rows.push(['高さ', el.height === 'to_roof' ? '屋根まで' : esc(el.height ?? ''), el.height === 'to_roof' || el.height == null ? '' : 'mm']);
  } else if (kind === 'opening') {
    what = KIND[el.kind] || el.kind;
    rows.push(['壁', el.wall ? `<button class="idc" data-id="${esc(el.wall)}">${esc(el.wall)}</button>` : '', '']);
    rows.push(['幅', { field: 'width_mm', value: el.width_mm }, 'mm']);
    rows.push(['下端 FL+', { field: 'sill_fl_mm', value: el.sill_fl_mm }, 'mm']);
    rows.push(['上端 FL+', { field: 'head_fl_mm', value: el.head_fl_mm }, 'mm']);
    if (el.offset_mm != null) rows.push(['壁の端から', esc(num(el.offset_mm)), 'mm']);
  } else {
    what = roofKind(el);
    const pc = roofPitchCell(el); rows.push(['勾配', pc.field ? pc : esc(pc.text), '/10']);
    if (roofIsShed(el)) rows.push(['低い側', { field: 'low_side', value: el.low_side, options: [...'SNEW'].map((s) => [s, SIDE[s]]) }, '']);
    if (el.top_z_range_mm) rows.push(['軒先の高さ', { field: 'eave_tip_mm', value: el.top_z_range_mm[0] }, 'mm']);
    for (const s of 'WESN') if (el.eave_mm && s in el.eave_mm) rows.push([`軒の出（${SIDE[s]}）`, { field: `eave_mm.${s}`, value: el.eave_mm[s] }, 'mm', side === s]);
    if (el.soffit_z_mm != null) rows.push(['軒天の高さ', { field: 'soffit_z_mm', value: el.soffit_z_mm }, 'mm']);
  }
  const cell = (v) => {
    if (v && typeof v === 'object') {
      if (v.value == null || v.value === '') return '';     // 値のない欄（glb にない）は空のまま
      if (!editable) return esc(v.options ? (SIDE[v.value] || v.value) : (v.step ? v.value : num(v.value)));
      if (v.options) return `<select data-field="${v.field}" data-orig="${esc(v.value)}">${v.options.map(([o, l]) => `<option value="${o}"${o === v.value ? ' selected' : ''}>${l}</option>`).join('')}</select>`;
      const val = v.step ? v.value : num(v.value);
      return `<input type="number" step="${v.step || '1'}" value="${esc(val)}" data-field="${v.field}" data-orig="${esc(val)}">`;
    }
    return v;
  };
  box.innerHTML = `<div class="sel-head"><span class="idc static lit">${esc(el.id)}</span><span class="what">${esc(what)}</span><span class="where">${esc(lv)}${side ? `・${SIDE[side]}の軒` : ''}</span>`
    + `<div class="acts"><button class="icon-btn" data-act="frame" title="模型でこの部材へ寄る"><svg class="ic"><use href="#i-target"/></svg></button>`
    + `<button class="icon-btn" data-act="clear" title="選ぶのをやめる（印を消す）"><svg class="ic"><use href="#i-close"/></svg></button></div></div>${others}`
    + `<dl class="props">${rows.map(([k, v, unit, hl]) => { const c = cell(v); return `<dt${hl ? ' class="hl"' : ''}>${k}</dt><dd${hl ? ' class="hl"' : ''}>${c}${unit && c !== '' ? `<span class="unit">${unit}</span>` : ''}</dd>`; }).join('')}</dl>`
    + (editable ? `<div class="sel-approve" hidden><input type="text" placeholder="直す理由（例：立面図の窓の上端に合わせる）"><button class="primary">承認</button><button>やめる</button></div>`
      + (kind === 'wall' ? '<button class="quiet danger small sel-del">この壁を取り除く</button>' : '')
      : '<p class="sel-note">数値は mm（実寸）。直すときは<button data-act="fix">「修正」タブ</button>で</p>');
  if (!editable) return;
  const inputs = $$('input[data-field], select[data-field]', box);
  const ap = box.querySelector('.sel-approve');
  const changed = () => inputs.filter((i) => (i.tagName === 'SELECT' ? i.value !== i.dataset.orig : Number(i.value) !== Number(i.dataset.orig)));
  inputs.forEach((i) => i.addEventListener(i.tagName === 'SELECT' ? 'change' : 'input', () => {
    i.closest('dd').classList.toggle('changed', i.tagName === 'SELECT' ? i.value !== i.dataset.orig : Number(i.value) !== Number(i.dataset.orig));
    ap.hidden = !changed().length;
  }));
  const [reason, ok, cancel] = ap.querySelectorAll('input, button');
  cancel.addEventListener('click', () => { inputs.forEach((i) => { i.value = i.dataset.orig; i.closest('dd').classList.remove('changed'); }); ap.hidden = true; });
  ok.addEventListener('click', () => approveChanges(el.id, changed(), reason.value, f.primary));
  box.querySelector('.sel-del')?.addEventListener('click', () => deleteWall(el));
}
$('#sel-panel').addEventListener('click', (e) => {
  const a = e.target.closest('[data-act]');
  if (a?.dataset.act === 'clear') { clearFocus(); return; }
  if (a?.dataset.act === 'fix') { setWorkspace('fix'); return; }
  if (a?.dataset.act === 'frame' && state.hl && state.hl.children.length) { frameOn(new THREE.Box3().setFromObject(state.hl)); return; }
  const p = e.target.closest('[data-pick]');
  if (p && state.focus) { state.focus.primary = p.dataset.pick; renderInspector(); revealInTree(p.dataset.pick); markLit(); }
});
// 番号の札（直しの履歴・右の欄）：押すと、その部材を光らせる
document.addEventListener('click', (e) => {
  const c = e.target.closest('.idc[data-id]');
  if (!c || c.classList.contains('static') || c.closest('.f-head')) return;
  selectElement(c.dataset.id);
});

// ---------------------------------------------------------------- 平面図
async function loadPlan() {   // 平面図（2階建ては選んだ階）。「図面を重ねる」なら図面（jww）の線も
  const params = [];
  if (state.multi) params.push(`level=${encodeURIComponent($('#plan-level').value)}`);
  if ($('#plan-drawing').checked) params.push('drawing=1');
  const svgText = await call('GET', '/api/plan/{a}' + (params.length ? '?' + params.join('&') : ''));
  const fresh = !planSvg();                                // 閉じたあとに開き直した
  $('#plan').innerHTML = svgText;
  const key = `${state.alias}|${state.multi ? $('#plan-level').value : ''}`;   // 物件や階を替えたら「全体」に戻す
  if (key !== planView.key || fresh) { planView.key = key; planView.fit = true; }
  applyPlanView();
  updateSplitControls();
  if (state.focus) markPlan(state.focus.ids);             // 読み直すと印が消えるので、付け直す
}

// ---------------------------------------------------------------- 平面図の拡大・縮小・移動（★11/5 夜 作者「W1 や O1 などが見づらい」）
// SVG の viewBox を変える（囲みの座標の変換 svgPoint・toMm はそのまま合う）。見ている所は「真ん中の点（SVG の座標）と
// 倍率 k（画面の px ÷ SVG の 1）」で持ち、窓の大きさが変わっても保つ。ホイール：カーソルの所を中心に。ドラッグ：動かす
const planView = { cx: 0, cy: 0, k: 1, fit: true, key: '', pan: null };
const PLAN_PAD = { top: 52, right: 16, bottom: 16, left: 16 };     // 左上の切り替え・右上の道具に紙がかからないように
function planPaper(svg) {          // 紙（SVG の全体）の大きさ。最初の viewBox を覚えておく
  if (!svg.dataset.vw) { const vb = svg.viewBox.baseVal; svg.dataset.vw = vb.width || svg.getAttribute('width'); svg.dataset.vh = vb.height || svg.getAttribute('height'); }
  return [Number(svg.dataset.vw), Number(svg.dataset.vh)];
}
function planFitK(svg, w, h) {
  const [W, H] = planPaper(svg);
  return Math.max(0.01, Math.min((w - PLAN_PAD.left - PLAN_PAD.right) / W, (h - PLAN_PAD.top - PLAN_PAD.bottom) / H));
}
function applyPlanView() {
  const svg = planSvg(); const box = $('#plan'); if (!svg) return;
  const w = box.clientWidth, h = box.clientHeight; if (!w || !h) return;   // 3D を出している間（隠れている）は後で
  const [W, H] = planPaper(svg);
  if (planView.fit) {
    planView.k = planFitK(svg, w, h);
    planView.cx = W / 2 - (PLAN_PAD.left - PLAN_PAD.right) / 2 / planView.k;
    planView.cy = H / 2 - (PLAN_PAD.top - PLAN_PAD.bottom) / 2 / planView.k;
  }
  const { cx, cy, k } = planView;
  svg.setAttribute('viewBox', `${(cx - w / 2 / k).toFixed(2)} ${(cy - h / 2 / k).toFixed(2)} ${(w / k).toFixed(2)} ${(h / k).toFixed(2)}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
}
function zoomPlan(f, sx, sy) {     // f 倍にする。(sx, sy)＝その点を動かさない（平面図の欄の中の px。省くと真ん中）
  const svg = planSvg(); const box = $('#plan'); if (!svg) return;
  const w = box.clientWidth, h = box.clientHeight; if (!w || !h) return;
  const fitK = planFitK(svg, w, h);
  const k = Math.min(Math.max(planView.k * f, fitK * 0.5), fitK * 14);
  const px = (sx ?? w / 2) - w / 2, py = (sy ?? h / 2) - h / 2;
  const ux = planView.cx + px / planView.k, uy = planView.cy + py / planView.k;
  Object.assign(planView, { k, cx: ux - px / k, cy: uy - py / k, fit: false });
  applyPlanView();
}
function fitPlan() { planView.fit = true; applyPlanView(); }
new ResizeObserver(() => applyPlanView()).observe($('#plan'));
$('#plan').addEventListener('wheel', (e) => {
  if (!planSvg()) return;
  e.preventDefault();
  const r = $('#plan').getBoundingClientRect();
  const dy = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1);
  zoomPlan(Math.exp(-dy * 0.0015), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });
$('#plan-zoom-in').addEventListener('click', () => zoomPlan(1.4));
$('#plan-zoom-out').addEventListener('click', () => zoomPlan(1 / 1.4));
$('#plan-fit').addEventListener('click', fitPlan);

// 平面図：ドラッグで動かす・押して選ぶ（囲むとき・分け目をつかむときは、それぞれの処理）。札（W2 など）を押しても選べる
$('#plan').addEventListener('pointerdown', (e) => {
  if (e.button !== 0 || lasso.on || splitUi.drag || !planSvg() || e.target.closest?.('[data-role="split"]')) return;
  planView.pan = { x: e.clientX, y: e.clientY, cx: planView.cx, cy: planView.cy, target: e.target, id: e.pointerId, moved: false };
  $('#plan').setPointerCapture(e.pointerId);
});
$('#plan').addEventListener('pointermove', (e) => {
  const p = planView.pan; if (!p || p.id !== e.pointerId) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  if (!p.moved && Math.hypot(dx, dy) <= CLICK_PX) return;
  if (!p.moved) { p.moved = true; $('#plan').classList.add('panning'); }
  Object.assign(planView, { cx: p.cx - dx / planView.k, cy: p.cy - dy / planView.k, fit: false });
  applyPlanView();
});
function endPlanPan(e, cancel) {
  const p = planView.pan; if (!p || p.id !== e.pointerId) return;
  planView.pan = null; $('#plan').classList.remove('panning');
  if (cancel || p.moved || lasso.on) return;
  const id = p.target.closest?.('[data-id]')?.dataset.id;
  if (id && findElement(id)) {
    selectElement(id, { frame: false, quiet: true }).then(() => setStatus(`${tagText(id)} を選びました（何もない所を押すか Esc で外れます）`, 'ok'));
  } else clearSelection();
}
$('#plan').addEventListener('pointerup', (e) => endPlanPan(e, false));
$('#plan').addEventListener('pointercancel', (e) => endPlanPan(e, true));
$('#plan-drawing').addEventListener('change', async () => {
  try { setStatus($('#plan-drawing').checked ? '図面を重ねています（初回は数秒かかります）…' : '図面を外しています…', 'busy'); await loadPlan(); setStatus('', ''); }
  catch (e) { setStatus('平面図を読めませんでした：' + e.message, 'err'); }
});
$('#plan-level').addEventListener('change', async () => {
  clearLasso();
  try { await loadPlan(); } catch (e) { setStatus('平面図を読めませんでした：' + e.message, 'err'); }
});

// ---------------------------------------------------------------- 物件を開く・読み直す
async function refresh() {
  state.refreshing = true;
  try {
    const data = await call('GET', '/api/building/{a}');
    state.building = data.building; state.built = true;
    state.pick = null; clearHighlight3d(); state.focus = null;   // 組み直したら当たり判定用の形も読み直す・光らせた印は消す
    state.multi = (data.building.levels || []).length > 1;          // 2階建てか
    $('#plan-level').hidden = !state.multi;
    if (!state.multi) $('#plan-level').value = '1F';
    renderTables(data.building);
    await renderHistory(data.override_results);
    renderTree(); renderInspector();
    await reloadChecks();
    await loadPlan();
    await renderChecks(); renderDownloads(); await renderFindings(); await loadModel();
    renderModelInfo(); updateStatusBar(); updateChrome();
    inspectButtons(state.inspecting);
    await syncVersion();                                   // 自分の直しで、もう一度読み直さないように
  } finally { state.refreshing = false; }
  scheduleThumb();
}

// 自社の会社ルール以外で作った模型は、状態の文に会社の名前を出す（どの決まりで作った模型か分かるように）
function companyNote() {
  const c = companyOf();
  return c === 'jisha' ? '' : `（会社ルール：${state.companies?.[c] || c}）`;
}

async function selectAlias(alias, built) {
  if (!built) {
    clearProperty();          // 前の物件の模型・表・書き出しを残さない（残ると、書き出しが前の物件のまま・承認が新しい物件に飛ぶ）
    state.alias = alias; state.viewSet = false; updateChrome();
    setStatus(`${alias}：まだ組み立てていません。「読み込んで組み立てる」を押してください`); return;
  }
  if (state.alias !== alias) { state.hidden = new Set(); state.treeOpen = new Set(['parts']); }
  state.alias = alias; state.viewSet = false; state.built = built; state.report = null; state.savedAt = null; state.savedFromServer = false; updateSteps();
  updateChrome();
  try { setStatus('読み込んでいます…', 'busy'); await refresh(); setStatus(`${alias}を表示しています${companyNote()}`, 'ok'); }
  catch (e) { setStatus(e.message, 'err'); }
}

$('#build').addEventListener('click', () => buildCurrent());
$('#empty-build').addEventListener('click', () => buildCurrent());
async function buildCurrent() {
  if (!state.alias) return;
  const btns = [$('#build'), $('#empty-build')]; btns.forEach((b) => { b.disabled = true; });
  let built = false;
  try { setStatus('glb を読んで、壁を作り直し、模型を組み立てています（数秒かかります）…', 'busy'); await call('POST', '/api/build/{a}'); state.built = true; await refresh(); setStatus(`${state.alias} を組み立てました${companyNote()}`, 'ok'); built = true; }
  catch (e) { setStatus('組み立てられませんでした：' + e.message, 'err'); }
  finally { btns.forEach((b) => { b.disabled = false; }); updateChrome(); }
  if (built) { loadProps().catch(() => {}); await autoInspect(); }
  return built;
}

// 組み立てたあと、設定どおりに自動で検品を始める（SPEC §12）。検品タブを開いて、面ごとの担当が動く様子を見せる
async function autoInspect() {
  let st;
  try { st = await call('GET', '/api/settings'); } catch (e) { return; }
  const conn = st.auto_inspect;
  if (!conn || conn === 'none') return;
  if (conn === 'api_key' && !st.api_key_set) { setStatus('組み立てました。自動の検品は API キーが入っていないため始めませんでした（環境設定）', 'err'); return; }
  setWorkspace('inspect');
  await startInspection(conn, { auto: true });
  setStatus(`${state.alias} を組み立てました。自動で検品を始めました（環境設定で変えられます）`, 'ok');
}

function clearProperty() {
  state.alias = null; state.building = null; state.built = false; state.report = null; state.focus = null; state.checks = null;
  state.overrides = []; state.savedAt = null; state.savedFromServer = false; state.pick = null;
  if (state.root) { scene.remove(state.root); state.root = null; }
  if (state.exploded) setExploded(false);
  clearLasso(); setLassoMode(false); setLasso3dMode(false); framePlate();
  $('#plan').innerHTML = ''; updateSplitControls();
  for (const id of ['#levels', '#openings', '#walls', '#roofs', '#roof-eaves', '#history', '#checks', '#downloads',
    '#findings', '#questions', '#code-findings', '#inspect-progress', '#tree']) $(id).innerHTML = '';
  $('#inspect-summary').textContent = '';
  for (const id of ['#hist-count', '#n-findings', '#n-questions', '#n-code', '#n-checks']) $(id).textContent = '';
  for (const t of ['inspect', 'fix', 'print']) setTabCount(t, 0);
  $('#undo-last').disabled = true;
  renderInspector(); renderModelInfo(); updateSteps(); updateStatusBar();
  stopPolling();
}

// ---------------------------------------------------------------- 上の帯・空の画面・下の帯
function updateChrome() {
  const open = !!state.alias;
  document.body.dataset.open = open && state.built ? '1' : '0';
  $('#doc-name').textContent = state.alias || '';
  document.title = state.alias ? `${state.alias} - HAKOYA（箱家）` : 'HAKOYA（箱家）';
  $$('#file-pop [data-need="alias"]').forEach((b) => { b.disabled = !open; });
  $('#build').disabled = !open;
  $('#build-label').textContent = state.built ? '読み込み直す' : '読み込んで組み立てる';
  for (const id of ['#inspect-ai', '#inspect-api', '#inspect-off']) $(id).disabled = !state.built || state.inspecting;
  $('#lasso-btn').disabled = !state.built; $('#fix-table').disabled = !state.built; $('#fix-history').disabled = !state.built;
  $('#dl-main').setAttribute('aria-disabled', state.built && state.checks ? 'false' : 'true');
  // 空の作業画面：物件がない → ファイルの案内／組み立てていない → 組み立てるボタン
  const empty = $('#empty');
  empty.hidden = open && state.built;
  if (!open) {
    $('#empty-lead').textContent = state.mode === 'sample' ? 'ファイル → 過去の物件を開く' : 'ファイル → 新しく読み込む／過去の物件を開く';
    $('#empty-new').hidden = state.mode === 'sample'; $('#empty-open').hidden = false; $('#empty-build').hidden = true;
    renderRecent();
  } else if (!state.built) {
    $('#empty-lead').textContent = `${state.alias}：まだ組み立てていません`;
    $('#empty-new').hidden = true; $('#empty-open').hidden = true; $('#empty-build').hidden = false; $('#empty-recent').hidden = true;
  }
}

// 下の帯：左は動きの文（#status）、右は保存済みの時刻だけ（★11/5 夜 作者「縮尺とか造形範囲とか部品とか AI はいらないかな。
// 保存済みだけでいい」。造形範囲・部品の数は模型の欄、AI のつなぎ方は環境設定で見られる）
function updateStatusBar() {
  const saved = $('#sb-saved');
  saved.hidden = !state.alias;
  saved.textContent = state.savedAt ? `保存済み ${fmtTime(state.savedAt)}` : '自動で保存';
}
$('#empty-new').addEventListener('click', () => runCmd('new'));
$('#empty-open').addEventListener('click', () => runCmd('open'));

// ---------------------------------------------------------------- 物件の一覧・最近の物件
async function loadProps() {
  const rows = await call('GET', '/api/properties');
  state.props = (rows || []).map((p) => ({ ...p, status: p.status || null }));
  if (state.props.some((p) => p.status)) {   // 新しい順（サーバーが並べるが、念のため）
    const t = (p) => (p.status && (p.status.edited_at || p.status.built_at)) || '';
    state.props.sort((a, b) => (t(b) > t(a) ? 1 : t(b) < t(a) ? -1 : 0));
  }
  return state.props;
}
const recentKey = () => `hakoya.recent.${state.mode}`;
function localRecent() { try { return JSON.parse(localStorage.getItem(recentKey()) || '[]'); } catch (e) { return []; } }
function recentList() {
  const rec = Array.isArray(state.settings?.recent) ? state.settings.recent : localRecent();
  const known = new Set(state.props.map((p) => p.alias));              // このモードの物件だけ（ほかのモードの名前を出さない）
  return rec.filter((a) => known.has(a)).slice(0, 5);
}
async function rememberRecent(alias) {
  try { const r = [alias, ...localRecent().filter((a) => a !== alias)].slice(0, 8); localStorage.setItem(recentKey(), JSON.stringify(r)); } catch (e) { /* 保存できない窓 */ }
  try {
    const r = await call('POST', '/api/settings/last_alias', { alias });
    if (r && state.settings) { if (Array.isArray(r.recent)) state.settings.recent = r.recent; state.settings.last_alias = r.last_alias; }
  } catch (e) { /* 合わせる前のサーバー */ }
  renderRecent();
}
function recentButtons(list) {
  const prop = (a) => state.props.find((p) => p.alias === a);
  return list.map((a) => {
    const p = prop(a); const t = p?.status ? (p.status.edited_at || p.status.built_at) : null;
    return `<button role="menuitem" class="recent${a === state.alias ? ' cur' : ''}" data-cmd="recent" data-alias="${esc(a)}"><span class="dot"></span><span class="rname">${esc(a)}</span><span class="rtime">${t ? esc(fmtTime(t)) : a === state.alias ? '開いています' : ''}</span></button>`;
  }).join('');
}
function renderRecent() {
  const list = recentList();
  $('#recent-list').innerHTML = list.length ? recentButtons(list) : '<div class="recent-empty">まだありません</div>';
  const er = $('#empty-recent'); er.hidden = !list.length || !!state.alias;
  $('#empty-recent-list').innerHTML = recentButtons(list);
}
$('#empty-recent-list').addEventListener('click', (e) => { const b = e.target.closest('[data-alias]'); if (b) openProperty(b.dataset.alias); });

async function openProperty(alias) {
  if (!state.props.length) { try { await loadProps(); } catch (e) { /* 下で案内 */ } }
  let p = state.props.find((x) => x.alias === alias);
  if (!p) { try { await loadProps(); p = state.props.find((x) => x.alias === alias); } catch (e) { /* 下で案内 */ } }
  if (!p) { setStatus(`「${alias}」という物件はありません`, 'err'); return false; }
  if (state.alias && state.alias !== alias) clearProperty();
  const url = new URL(location.href); url.searchParams.set('alias', alias); history.replaceState(null, '', url);
  await selectAlias(p.alias, p.built);
  rememberRecent(alias);
  startPolling();
  return true;
}
function closeProperty() {
  clearProperty(); setPane('3d'); updateChrome();            // 空の造形板を見せる
  const url = new URL(location.href); url.searchParams.delete('alias'); history.replaceState(null, '', url);
  setStatus('物件を閉じました');
}

// ---------------------------------------------------------------- 開いている物件が外（MCP など）で変わったら読み直す（4 秒ごと）
const poll = { timer: null, version: undefined, off: false };
function startPolling() { stopPolling(); if (poll.off || !state.alias) return; poll.version = undefined; poll.timer = setInterval(pollState, 4000); pollState(); }
function stopPolling() { if (poll.timer) clearInterval(poll.timer); poll.timer = null; }
function canAutoRefresh() {   // 人が何かをしている途中（ドラッグ・囲み・小窓・直しかけの値）は読み直さない
  if (document.querySelector('dialog[open]') || !$('#file-pop').hidden) return false;
  if (lasso.on || lasso.on3d || lasso.line || lasso.line3d || lasso.selection.length) return false;
  if (splitUi.drag || splitUi.busy || state.orbiting || state.refreshing || state.selfWriting || $('#status').classList.contains('busy')) return false;
  if (document.querySelector('tr.approve:not([hidden]), .sel-approve:not([hidden])')) return false;
  return true;
}
async function pollState() {
  if (!state.alias || poll.off || document.hidden) return;
  const alias = state.alias;
  let st;
  try { st = await call('GET', '/api/state/{a}'); } catch (e) {
    if (missingEndpoint(e)) { poll.off = true; stopPolling(); }   // 合わせる前のサーバー：読み直しはしない
    return;
  }
  if (alias !== state.alias) return;
  if (st.saved_at) { state.savedAt = st.saved_at; state.savedFromServer = true; updateStatusBar(); }
  if (poll.version === undefined) { poll.version = st.version; return; }
  if (st.version === poll.version || !canAutoRefresh()) return;   // 途中なら、次の 4 秒でもう一度見る
  poll.version = st.version;
  try {
    if (!state.built) { await selectAlias(alias, true); } else await refresh();
    setStatus('ほかの所で直されたので、読み直しました', 'ok');
  } catch (e) { setStatus('読み直せませんでした：' + e.message, 'err'); }
}
async function quietly(fn) {       // 自分で書く間は読み直さない（書いたあと、番号を覚え直す）
  state.selfWriting = (state.selfWriting || 0) + 1;
  try { return await fn(); } finally { await syncVersion(); state.selfWriting -= 1; }
}
async function syncVersion() {     // 自分で直したあと：その番号を覚えて、自分の直しで読み直さないようにする
  if (!state.alias || poll.off) return;
  try { const st = await call('GET', '/api/state/{a}'); poll.version = st.version; if (st.saved_at) { state.savedAt = st.saved_at; state.savedFromServer = true; updateStatusBar(); } }
  catch (e) { if (missingEndpoint(e)) { poll.off = true; stopPolling(); } }
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) pollState(); });

// ---------------------------------------------------------------- 小さな絵（過去の物件の一覧に出す）：斜めから1回描いて送る
const thumb = { timer: null, off: false };
function scheduleThumb() { clearTimeout(thumb.timer); if (!thumb.off) thumb.timer = setTimeout(captureThumb, 700); }
async function captureThumb() {
  if (!state.root || !state.alias || thumb.off) return;
  if (tween || state.orbiting) { scheduleThumb(); return; }
  const alias = state.alias;
  const keep = { pos: camera.position.clone(), tgt: controls.target.clone(), aspect: camera.aspect, pr: renderer.getPixelRatio(), size: renderer.getSize(new THREE.Vector2()) };
  const meshes = []; state.root.traverse((o) => { if (o.isMesh) { meshes.push([o, o.visible, o.position.z]); o.visible = true; o.position.z = 0; } });
  const hlVisible = state.hl ? state.hl.visible : null; if (state.hl) state.hl.visible = false;
  let png = null;
  try {
    const W = 480, H = 300;
    renderer.setPixelRatio(1); renderer.setSize(W, H, false);
    camera.aspect = W / H; camera.updateProjectionMatrix();
    const box = new THREE.Box3().setFromObject(state.root); const c = box.getCenter(new THREE.Vector3()); const r = box.getSize(new THREE.Vector3()).length() * 0.78;
    const d = VIEW_DIRS.iso; camera.position.set(c.x + d[0] * r, c.y + d[1] * r, c.z + d[2] * r); camera.lookAt(c);
    renderer.render(scene, camera);
    png = renderer.domElement.toDataURL('image/png');
  } finally {
    renderer.setPixelRatio(keep.pr); renderer.setSize(keep.size.x, keep.size.y, false);
    camera.aspect = keep.aspect; camera.updateProjectionMatrix(); camera.position.copy(keep.pos); controls.target.copy(keep.tgt); controls.update();
    for (const [o, v, z] of meshes) { o.visible = v; o.position.z = z; }
    if (state.hl && hlVisible !== null) state.hl.visible = hlVisible;
    renderer.render(scene, camera);
  }
  if (!png || alias !== state.alias) return;
  await quietly(async () => {
    try {
      await call('POST', '/api/thumb/{a}', { png });
      const p = state.props.find((x) => x.alias === alias); if (p) { p.thumb = true; p.thumbAt = Date.now(); }
    } catch (e) { if (missingEndpoint(e)) thumb.off = true; }
  });
}

// ---------------------------------------------------------------- ファイルのメニュー
function toggleMenu(open) {
  const pop = $('#file-pop'); const btn = $('#file-btn');
  if (open === pop.hidden) { pop.hidden = !open; btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
  if (open) { renderRecent(); updateChrome(); pop.querySelector('button:not([disabled]):not([hidden])')?.focus(); }
}
$('#file-btn').addEventListener('click', () => toggleMenu($('#file-pop').hidden));
document.addEventListener('pointerdown', (e) => { if (!e.target.closest('#file-menu')) toggleMenu(false); });
$('#file-pop').addEventListener('click', (e) => {
  const b = e.target.closest('[data-cmd]'); if (!b || b.disabled) return;
  toggleMenu(false); runCmd(b.dataset.cmd, b.dataset.alias);
});
$('#file-pop').addEventListener('keydown', (e) => {
  const items = $$('#file-pop [data-cmd]').filter((b) => !b.disabled && b.offsetParent);
  const k = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); items[(k + (e.key === 'ArrowDown' ? 1 : items.length - 1) + items.length) % items.length]?.focus(); }
  if (e.key === 'Escape') { e.preventDefault(); toggleMenu(false); $('#file-btn').focus(); }
  if (e.key === 'Tab') toggleMenu(false);
});
async function runCmd(cmd, arg) {
  if (cmd === 'new') { if (state.mode !== 'sample') openNew(); return; }
  if (cmd === 'open') { openOpen(); return; }
  if (cmd === 'recent') { openProperty(arg); return; }
  if (cmd === 'prefs') { openPrefs(); return; }
  if (!state.alias) { setStatus('物件を開いていません。「ファイル」→「過去の物件を開く」から開いてください', 'err'); return; }
  if (cmd === 'save') {    // 直すたびに自動で保存している。押したら、保存したことを見せる
    await syncVersion();
    const now = new Date(); state.savedAt = now.toISOString(); state.savedFromServer = false; updateStatusBar();
    setStatus(`保存しました ${fmtTime(now)}（直すたびに自動で保存しています）`, 'ok');
    return;
  }
  if (cmd === 'print') { setWorkspace('print'); return; }
  if (cmd === 'close') closeProperty();
}
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey && !e.altKey && !e.metaKey) {
    const k = e.key.toLowerCase();
    if (k === 's') { e.preventDefault(); runCmd('save'); }
    else if (k === 'o') { e.preventDefault(); runCmd('open'); }
    else if (k === 'p') { e.preventDefault(); runCmd('print'); }
    else if (e.key === ',') { e.preventDefault(); runCmd('prefs'); }
    return;
  }
  if (e.key === 'Escape' && (lasso.on || lasso.on3d) && !document.querySelector('dialog[open]')) { setLassoMode(false); setLasso3dMode(false); }
});

// ---------------------------------------------------------------- 小窓（過去の物件を開く・新しく読み込む・環境設定）
function openDialog(id) { toggleMenu(false); const d = $(id); if (!d.open) d.showModal(); return d; }
$$('dialog [data-close]').forEach((b) => b.addEventListener('click', () => b.closest('dialog').close()));   // 外側を押しても閉じない（ソフトの小窓と同じ）

// ---- 過去の物件を開く
let openSel = null;
function propChips(p) {
  const s = p.status || {};
  const chips = [];
  if (!p.has_glb) chips.push(['glb なし', 'bad']);
  if (p.built || s.built_at) chips.push(['組み立て済み', '']); else chips.push(['まだ組み立てていません', 'dim']);
  if (s.inspected_at) chips.push([s.stale ? '検品済み（そのあと組み立て直した）' : '検品済み', s.stale ? 'dim' : 'ok']);
  if (s.fixes) chips.push([`直し ${s.fixes} 件`, '']);
  if (s.open_findings) chips.push([`未確認の指摘 ${s.open_findings} 件`, s.high ? 'bad' : 'warn']);
  if (s.exported_at) chips.push(['書き出し済み', 'ok']);
  return chips;
}
function renderOpenList() {
  const q = $('#open-filter').value.trim().toLowerCase();
  const rows = state.props.filter((p) => !q || p.alias.toLowerCase().includes(q));
  $('#open-count').textContent = `${rows.length} 件${q ? `（全 ${state.props.length} 件）` : ''}`;
  if (!rows.some((p) => p.alias === openSel)) openSel = rows[0]?.alias || null;
  const ul = $('#open-list');
  if (!rows.length) {
    ul.innerHTML = `<li class="none">${state.props.length ? `「${esc(q)}」に当たる物件はありません` : state.mode === 'sample' ? '見本の家がありません' : 'まだ物件がありません。「ファイル」→「新しく読み込む」から読み込んでください'}</li>`;
    $('#open-go').disabled = true; return;
  }
  ul.innerHTML = rows.map((p) => {
    const s = p.status || {}; const t = s.edited_at || s.built_at;
    const src = p.thumb === false ? '' : `/api/thumb/${encodeURIComponent(p.alias)}?t=${p.thumbAt || ''}`;
    return `<li role="option" data-alias="${esc(p.alias)}" aria-selected="${p.alias === openSel}" tabindex="-1">`
      + `<div class="thumb">${src ? `<img src="${src}" alt="" loading="lazy">` : ''}<span class="ph"><svg class="ic"><use href="#i-house"/></svg></span></div>`
      + `<div><div class="pname">${esc(p.alias)}</div><div class="pchips">${propChips(p).map(([l, c]) => `<span class="pc ${c}">${esc(l)}</span>`).join('')}</div></div>`
      + `<div class="ptime">${t ? `<b>${esc(fmtTime(t))}</b>最後に直した日時` : ''}${p.alias === state.alias ? '<div class="pcur">開いています</div>' : ''}</div></li>`;
  }).join('');
  ul.querySelectorAll('img').forEach((img) => {
    img.addEventListener('load', () => { img.nextElementSibling?.remove(); });   // 絵がある：家の印を消す
    img.addEventListener('error', () => img.remove());                          // 絵がない：家の印のまま
  });
  $('#open-go').disabled = !openSel;
}
async function openOpen() {
  openDialog('#dlg-open');
  $('#open-filter').value = '';
  try { await loadProps(); } catch (e) { setStatus('物件の一覧を読めませんでした：' + e.message, 'err'); }
  openSel = state.alias && state.props.some((p) => p.alias === state.alias) ? state.alias : state.props[0]?.alias || null;
  renderOpenList();
  $('#open-filter').focus();
}
$('#open-filter').addEventListener('input', renderOpenList);
$('#open-filter').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const items = $$('#open-list li[data-alias]').map((li) => li.dataset.alias); if (!items.length) return;
    const k = items.indexOf(openSel); openSel = items[Math.max(0, Math.min(items.length - 1, k + (e.key === 'ArrowDown' ? 1 : -1)))];
    renderOpenList(); $(`#open-list li[data-alias="${CSS.escape(openSel)}"]`)?.scrollIntoView({ block: 'nearest' });
  }
  if (e.key === 'Enter' && openSel) { e.preventDefault(); goOpen(); }
});
$('#open-list').addEventListener('click', (e) => {
  const li = e.target.closest('li[data-alias]'); if (!li) return;
  openSel = li.dataset.alias; $$('#open-list li[data-alias]').forEach((x) => x.setAttribute('aria-selected', x === li ? 'true' : 'false')); $('#open-go').disabled = false;
});
$('#open-list').addEventListener('dblclick', (e) => { const li = e.target.closest('li[data-alias]'); if (li) { openSel = li.dataset.alias; goOpen(); } });
$('#open-go').addEventListener('click', goOpen);
function goOpen() { if (!openSel) return; $('#dlg-open').close(); openProperty(openSel); }

// ---- 新しく読み込む（core/registry.py）：登録 → 組み立て → 開く
const reg = { glb: null, dwg: null };
function openNew() {
  openDialog('#dlg-new');
  $('#reg-msg').textContent = ''; $('#reg-msg').className = 'msg';
  renderRegistry();
  setTimeout(() => $('#reg-pick-glb').focus(), 0);
}
async function renderRegistry() {
  let r;
  try { r = await call('GET', '/api/registry'); } catch (e) { $('#registry-msg').textContent = '登録の一覧を読めませんでした：' + e.message; return; }
  if (!$('#reg-alias').value) $('#reg-alias').value = r.next_alias;
  const shown = new Set(state.props.map((p) => p.alias));    // このモードで見える物件だけ（見本用でお客様の家の名前を出さない）
  const rows = r.properties.filter((p) => !state.props.length || shown.has(p.alias));
  $('#registry-list').innerHTML = '<tr><th>仮名</th><th>glb</th><th>図面</th><th>高性能読み込み</th><th>出してはいけない言葉</th></tr>' + (rows.length ? rows.map((p) =>
    `<tr><td>${esc(p.alias)}</td><td>${esc(p.glb)}</td><td>${esc(p.drawings.join('・') || 'なし')}</td>` +
    `<td>${p.drawings.length ? `<label class="high"><input type="checkbox" data-high="${esc(p.alias)}"${p.high ? ' checked' : ''}>${p.high ? '入' : '切'}</label>` : '<span class="hint">glb だけ</span>'}</td>` +
    `<td>${p.ng_words ? `${p.ng_words} 件` : `<span class="warn">この物件の言葉なし</span>${r.global_ng_words ? `（全体の ${r.global_ng_words} 件だけ）` : ''}`}</td></tr>`).join('')
    : '<tr><td colspan="5" class="muted">まだ物件がありません</td></tr>');
}
// 高性能読み込み（図面と突き合わせる）の入れ切り。切ると、図面の照合・重ね・図面ありの検品を使わない（模型は変わらない）
$('#registry-list').addEventListener('change', async (e) => {
  const box = e.target.closest('input[data-high]');
  if (!box) return;
  const alias = box.dataset.high, on = box.checked;
  const msg = $('#registry-msg');
  box.disabled = true;
  try {
    await call('POST', `/api/reading/${encodeURIComponent(alias)}`, { high: on });
    msg.textContent = on ? `${alias}：高性能読み込みを入にしました。図面と突き合わせて検査・検品します` : `${alias}：高性能読み込みを切にしました。glb だけで検査・検品します`;
    msg.className = 'fine msg ok';
    if (alias === state.alias) { if ($('#plan-drawing').checked) await loadPlan(); await renderChecks(); }
  } catch (err) { box.checked = !on; msg.textContent = '切り替えられませんでした：' + err.message; msg.className = 'fine msg err'; }
  renderRegistry();
});
async function pickFor(what, key, el) {
  el.textContent = '選ぶ窓を開いています…（ブラウザの後ろに出ることがあります）'; el.className = 'hint';
  try {
    const r = await call('POST', '/api/registry/pick', { what });
    // やめた・失敗したときは、前に選んだものも外す（前は前の選択が残ったまま登録された）
    if (r.cancelled) { reg[key] = null; el.textContent = '選ぶのをやめました（選んだものはありません）'; return; }
    reg[key] = r.token; el.textContent = r.text; el.className = 'hint ' + (r.ok ? 'ok' : 'err');
  } catch (e) { reg[key] = null; el.textContent = e.message; el.className = 'hint err'; }
}
$('#reg-pick-glb').addEventListener('click', () => pickFor('glb', 'glb', $('#reg-glb-status')));
$('#reg-pick-dwg').addEventListener('click', () => pickFor('folder', 'dwg', $('#reg-dwg-status')));
function regMsg(text, cls) { const m = $('#reg-msg'); m.textContent = text; m.className = 'msg ' + (cls || ''); m.title = text; }
$('#reg-submit').addEventListener('click', async () => {
  const body = { alias: $('#reg-alias').value.trim(), glb_token: reg.glb, glb_path: $('#reg-glb-path').value.trim() || null,
    drawings_token: reg.dwg, drawings_path: $('#reg-dwg-path').value.trim() || null, ng_words: $('#reg-ng').value.split('\n') };
  if (!body.ng_words.some((w) => w.trim()) && !confirm('出してはいけない言葉（施主名・住所など）が空です。このまま読み込みますか？')) return;
  $('#reg-submit').disabled = true;     // 連打で同じ仮名を2回書かないように
  try {
    regMsg('登録しています…', 'busy');
    const r = await call('POST', '/api/registry', body);
    reg.glb = reg.dwg = null;
    for (const id of ['#reg-alias', '#reg-glb-path', '#reg-dwg-path', '#reg-ng']) $(id).value = '';
    $('#reg-glb-status').textContent = 'まだ選んでいません'; $('#reg-glb-status').className = 'hint';
    $('#reg-dwg-status').textContent = 'なくても読み込めます'; $('#reg-dwg-status').className = 'hint';
    await loadProps();
    regMsg(`${r.alias} を登録しました。組み立てています（数秒かかります）…`, 'busy');
    await openProperty(r.alias);                             // まだ組み立てていない物件として開く
    const built = await buildCurrent();                      // 組み立てる（設定どおりに自動の検品も）
    if (built) { $('#dlg-new').close(); setStatus(`${r.alias} を読み込んで、組み立てました${companyNote()}`, 'ok'); }
    else regMsg(`${r.alias} は登録しましたが、組み立てられませんでした（下の帯を見てください）。「読み込んで組み立てる」でやり直せます`, 'err');
    renderRegistry();
  } catch (e) { regMsg('読み込めませんでした：' + e.message, 'err'); }
  finally { $('#reg-submit').disabled = false; }
});

// ---- 環境設定
function openPrefs(section) {
  openDialog('#dlg-prefs');
  renderSettings(); renderRegistry();
  const target = section || (state.mode === 'other' ? 'set-company' : null);
  $$('.prefs-nav [data-jump]').forEach((b) => b.classList.toggle('on', b.dataset.jump === (target || $('.prefs-nav [data-jump]').dataset.jump)));
  if (target) setTimeout(() => document.getElementById(target)?.scrollIntoView({ block: 'start' }), 0);
  else $('#prefs-body').scrollTop = 0;
}
$('.prefs-nav').addEventListener('click', (e) => {
  const b = e.target.closest('[data-jump]'); if (!b) return;
  $$('.prefs-nav [data-jump]').forEach((x) => x.classList.toggle('on', x === b));
  document.getElementById(b.dataset.jump)?.scrollIntoView({ block: 'start', behavior: 'smooth' });
});

// ---------------------------------------------------------------- 説明（？）：押すと、説明の文を開く（吹き出しでも読める）
document.addEventListener('click', (e) => {
  const h = e.target.closest('.help'); if (!h) return;
  e.preventDefault(); e.stopPropagation();
  const host = h.closest('summary');
  const after = host || h;
  const next = after.nextElementSibling;
  if (next && next.classList.contains('help-text')) { next.remove(); return; }
  const p = document.createElement('p'); p.className = 'help-text'; p.textContent = h.title;
  after.after(p);
  if (host && host.parentElement.tagName === 'DETAILS') host.parentElement.open = true;
}, true);
document.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target.classList?.contains('help')) { e.preventDefault(); e.target.click(); } });

// ---------------------------------------------------------------- URL の末尾（撮影や説明のとき用）：#inspect・#fix・#print・#rules・#plan・#prefs・#open
// 前の名前（#sinsp・#sedit・#shist・#schk・#sout・#srules・#sset・#vplan・#v3d）も使える
function applyHash() {
  const WS = { sinsp: 'inspect', sedit: 'fix', shist: 'fix', schk: 'print', sout: 'print', srules: 'rules',
    model: 'model', fix: 'fix', inspect: 'inspect', print: 'print', rules: 'rules' };
  for (const id of (location.hash || '').slice(1).split(',').filter(Boolean)) {
    if (WS[id]) setWorkspace(WS[id]);
    if (id === 'vplan' || id === 'plan') setPane('plan');
    if (id === 'v3d' || id === '3d') setPane('3d');
    if (id === 'shist') $('#sec-history').scrollIntoView({ block: 'start' });
    if (id === 'schk') $('#checks').closest('.sec').scrollIntoView({ block: 'start' });
    if (id === 'sset' || id === 'prefs') runCmd('prefs');
    if (id === 'open') runCmd('open');
    if (id === 'new') runCmd('new');
  }
}

// ---------------------------------------------------------------- 始まり
requestAnimationFrame(loop);
(async () => {
  await settingsReady.catch(() => {});      // モード（state.mode）が分かってから
  try { await loadProps(); } catch (e) { setStatus('物件の一覧を読めませんでした：' + e.message, 'err'); }
  renderRecent(); updateChrome(); updateStatusBar();
  const want = new URLSearchParams(location.search).get('alias');
  if (want) await openProperty(want);
  else if (state.mode === 'sample') {       // 見本用：デモの家をそのまま開く
    const p = state.props.find((x) => x.alias === 'デモ用') || state.props[0];
    if (p) await openProperty(p.alias);
    else setStatus('見本の家がありません。仮名が「デモ用」か「見本」で始まる物件だけが出ます', 'err');
  } else if (!state.props.length) setStatus('物件がまだありません。「ファイル」→「新しく読み込む」で glb を選んで読み込んでください');
  applyHash();
})();
