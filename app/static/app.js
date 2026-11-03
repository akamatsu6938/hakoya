// HAKOYA の画面。サーバー（app/server.py）とやりとりするのは物件の仮名だけ。
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { buildPickGroup, pickHits } from './pick3d.js';

const $ = (s) => document.querySelector(s);
const api = (path) => path.replace('{a}', encodeURIComponent(state.alias));
const state = { alias: null, building: null, root: null, exploded: false, pick: null, hl: null };
const KIND = { window: '窓', door: 'ドア', entrance_door: '玄関ドア', exterior: '外壁', interior: '内壁', standalone: '単独の壁' };
const SIDE = { W: '西', E: '東', S: '南', N: '北' };
const roofKind = (r) => (r.kind === 'lean_to' ? '下屋の屋根' : state.multi ? '大屋根' : '屋根');

function setStatus(text, cls = '') { const s = $('#status'); s.textContent = text; s.className = 'status ' + cls; }

async function call(method, path, body) {
  const res = await fetch(api(path), { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) { let msg = res.statusText; try { msg = (await res.json()).detail || msg; } catch (e) { /* 本文なし */ } throw new Error(msg); }
  return res.headers.get('content-type')?.includes('json') ? res.json() : res.text();
}

// ---------------------------------------------------------------- タブ
document.querySelectorAll('.tabs').forEach((nav) => nav.addEventListener('click', (e) => {
  const tab = e.target.dataset.tab; if (!tab) return;
  nav.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.target));
  nav.parentElement.querySelectorAll(':scope > .pane').forEach((p) => p.classList.toggle('on', p.id === tab));
  if (nav.dataset.group === 'view') { clearLasso(); setLassoMode(false); setLasso3dMode(false); }   // 囲みは表示している図だけのもの
  if (tab === 'srules') renderRules();
  if (tab === 'v3d') resize();
}));

// ---------------------------------------------------------------- 3D 表示
const holder = $('#canvas');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(window.devicePixelRatio);
holder.appendChild(renderer.domElement);
const scene = new THREE.Scene(); scene.background = new THREE.Color(0xffffff);
scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8a8a, 1.3));
const sun = new THREE.DirectionalLight(0xffffff, 1.4); sun.position.set(-1, -2, 3); scene.add(sun);
const camera = new THREE.PerspectiveCamera(30, 1, 1, 10000); camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, renderer.domElement);
function resize() { const w = holder.clientWidth, h = holder.clientHeight || 400; renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix(); }
window.addEventListener('resize', resize);
(function loop() { requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); })();

const LIFT = { foundation: 0, floor1: 1, exterior1: 1, roof: 3, roof_ext: 3 };
// 2階建て（基礎・1階・下屋の屋根・2階・大屋根の5段）
const LIFT_MULTI = { foundation: 0, floor1: 1, exterior1: 1, roof_lower: 2, roof_lower_ext: 2, floor2: 3, exterior2: 3, roof: 4, roof_ext: 4 };
function applyVisibility() {
  if (!state.root) return;
  const lift = state.multi ? LIFT_MULTI : LIFT;
  state.root.traverse((o) => {
    if (!o.isMesh) return;
    const cb = document.querySelector(`input[data-part~="${o.name}"]`);   // 1つの切り替えで複数のパーツ（屋根＋屋根の外壁）
    o.visible = cb ? cb.checked : true;
    o.position.z = state.exploded ? (lift[o.name] || 0) * 60 : 0;
  });
}
const SINGLE_TOGGLES = $('#part-toggles').innerHTML;   // 平屋の切り替え（今までどおり）
const MULTI_TOGGLES = [['foundation', '基礎'], ['floor1 exterior1', '1階'], ['roof_lower roof_lower_ext', '下屋の屋根'],
  ['floor2 exterior2', '2階'], ['roof roof_ext', '大屋根']];
function renderPartToggles() {
  $('#part-toggles').innerHTML = state.multi
    ? MULTI_TOGGLES.map(([k, l]) => `<label><input type="checkbox" data-part="${k}" checked>${l}</label>`).join('') : SINGLE_TOGGLES;
  document.querySelectorAll('#part-toggles input[data-part]').forEach((cb) => cb.addEventListener('change', applyVisibility));
}
renderPartToggles();

function setView(kind) {
  if (!state.root) return;
  const box = new THREE.Box3().setFromObject(state.root); const c = box.getCenter(new THREE.Vector3());
  const r = box.getSize(new THREE.Vector3()).length();
  const pos = { iso: [-0.9, -1.45, 1.0], front: [0, -2.0, 0.2], side: [-2.0, 0, 0.2], top: [0, -0.001, 2.1] }[kind] || [-0.9, -1.45, 1.0];
  camera.position.set(c.x + pos[0] * r, c.y + pos[1] * r, c.z + pos[2] * r);
  controls.target.copy(c); controls.update();
}
document.querySelectorAll('button[data-view]').forEach((b) => b.addEventListener('click', () => {
  if (b.dataset.view === 'explode') {
    state.exploded = !state.exploded; b.textContent = state.exploded ? 'パーツを戻す' : 'パーツを離す'; applyVisibility();
    if (lasso.source === '3d') clearLasso();                    // 離すと色付けの位置がずれるので消す
    if (state.exploded) setLasso3dMode(false);
    return;
  }
  setView(b.dataset.view);
}));

async function loadModel() {
  const buf = await (await fetch(api('/api/model/{a}/model.glb') + '?t=' + Date.now())).arrayBuffer();
  const gltf = await new GLTFLoader().parseAsync(buf, '');
  if (state.root) scene.remove(state.root);
  state.root = gltf.scene;
  state.root.traverse((o) => { if (o.isMesh) { o.geometry.computeVertexNormals(); o.material.flatShading = true; o.material.needsUpdate = true; } });
  scene.add(state.root); resize(); applyVisibility();
  if (!state.viewSet) { setView('iso'); state.viewSet = true; }
}

// ---------------------------------------------------------------- 表で直す
function num(v) { return Math.round(v * 10) / 10; }

function editableRow(cells, onApprove) {
  // cells: [{text}] または [{field, value}]（入力欄）。値を変えると「承認」の行が出る
  const tr = document.createElement('tr');
  tr.dataset.id = cells[0].text;                   // 部材の番号（囲んで直すで行に色を付ける）
  const inputs = [];
  for (const c of cells) {
    const td = document.createElement('td');
    if (c.field) {
      const inp = document.createElement('input'); inp.type = 'number'; inp.step = '1'; inp.value = num(c.value);
      inp.dataset.field = c.field; inp.dataset.orig = num(c.value); inputs.push(inp); td.appendChild(inp);
    } else td.textContent = c.text;
    tr.appendChild(td);
  }
  const approve = document.createElement('tr'); approve.className = 'approve'; approve.hidden = true;
  approve.innerHTML = `<td colspan="${cells.length}"><input type="text" placeholder="直す理由（例：立面図の窓の上端に合わせる）"> <button class="primary">承認</button> <button>やめる</button></td>`;
  const [reason, ok, cancel] = approve.querySelectorAll('input, button');
  const changed = () => inputs.filter((i) => Number(i.value) !== Number(i.dataset.orig));
  inputs.forEach((i) => i.addEventListener('input', () => { const ch = changed().length > 0; tr.classList.toggle('changed', ch); approve.hidden = !ch; }));
  cancel.addEventListener('click', () => { inputs.forEach((i) => { i.value = i.dataset.orig; }); tr.classList.remove('changed'); approve.hidden = true; });
  ok.addEventListener('click', () => onApprove(changed(), reason.value));
  return [tr, approve];
}

async function approveChanges(target, inputs, reason) {
  try {
    setStatus('修正を記録して、模型を組み直しています…', 'busy');
    for (const i of inputs) await call('POST', '/api/overrides/{a}', { target, field: i.dataset.field, to: Number(i.value), reason });
    await refresh(); setStatus(`${target} を直しました（修正の履歴から取り消せます）`, 'ok');
  } catch (e) { setStatus('直せませんでした：' + e.message, 'err'); }
}

function renderTables(b) {
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
    del.addEventListener('click', async () => {
      const reason = prompt(`${w.id} を取り除きます。理由を書いてください`); if (reason === null) return;
      try { setStatus('壁を取り除いて組み直しています…', 'busy'); await call('POST', '/api/overrides/{a}', { target: w.id, field: '_delete', reason }); await refresh(); setStatus(`${w.id} を取り除きました`, 'ok'); }
      catch (e) { setStatus('取り除けませんでした：' + e.message, 'err'); }
    });
    rows[0].lastChild.appendChild(del);
    rows.forEach((r) => wt.appendChild(r));
  }
  // 屋根：番号・種類・勾配・低い側は表示だけ。軒の出（西・東・南・北）を直せる
  const rt = $('#roofs');
  const r2 = (h) => h.replace(/<th>/g, '<th rowspan="2">');   // 見出しは2段（軒の出の下に西・東・南・北）
  rt.innerHTML = `<tr>${r2(`<th>番号</th>${lvHead}<th>種類</th><th>勾配</th><th>低い側</th>`)}<th colspan="4">軒の出（mm）</th></tr>`
    + `<tr>${[...'WESN'].map((s) => `<th>${SIDE[s]}</th>`).join('')}</tr>`;
  for (const r of b.roofs || []) {
    const eaves = [...'WESN'].map((s) => (r.eave_mm && s in r.eave_mm ? { field: `eave_mm.${s}`, value: r.eave_mm[s] } : { text: '' }));
    const rows = editableRow([{ text: r.id }, ...lvCell(r), { text: roofKind(r) }, { text: (r.pitch || []).join('/') },
      { text: SIDE[r.low_side] || r.low_side || '' }, ...eaves], (inputs, reason) => approveChanges(r.id, inputs, reason));
    rows.forEach((x) => rt.appendChild(x));
  }
}

async function renderHistory(results) {
  const items = await call('GET', '/api/overrides/{a}');
  const ul = $('#history'); ul.innerHTML = items.length ? '' : '<li>まだ修正はありません</li>';
  const status = Object.fromEntries((results || []).map((r) => [r.id, r]));
  const label = { applied: '当てた', applied_changed: '当てた（自動の値が変わっていた）', failed: '当てられない' };
  for (const it of items.slice().reverse()) {
    const li = document.createElement('li'); const st = status[it.id];
    const what = it.field === '_delete' ? '取り除く' : `${FIELD[it.field] || it.field}：${it.from} → ${it.to}`;
    li.innerHTML = `<span class="tag">${it.id}</span><b>${it.target}</b> ${what}<br><span class="hint">${it.reason}（${it.value_source}・${it.at.slice(0, 16).replace('T', ' ')}）${st ? '・' + label[st.status] : ''}</span> `;
    const undo = document.createElement('button'); undo.textContent = '取り消す';
    undo.addEventListener('click', async () => {
      try { setStatus('取り消して組み直しています…', 'busy'); await call('DELETE', `/api/overrides/{a}/${it.id}`); await refresh(); setStatus(`${it.id} を取り消しました`, 'ok'); }
      catch (e) { setStatus('取り消せませんでした：' + e.message, 'err'); }
    });
    li.appendChild(undo); ul.appendChild(li);
  }
}

async function renderChecks() {
  const ul = $('#checks'); ul.innerHTML = '';
  try {   // 予防：会社ルールと比べた結果（家ごとに変わる値は比べない）
    const rc = await call('GET', '/api/rulecheck/{a}');
    ul.insertAdjacentHTML('beforeend', '<li class="lv-情報"><b>会社ルールと比べた結果</b>（家ごとに変わる窓の高さ・屋根の勾配などは比べません）</li>');
    for (const r of rc.rows) {
      ul.insertAdjacentHTML('beforeend', r.match
        ? `<li class="lv-情報"><span class="tag">一致</span>${esc(r.label)}：会社ルール ${esc(r.rule_text)}</li>`
        : `<li class="lv-中"><span class="tag">いつもと違う</span>${esc(r.message)}</li>`);
    }
  } catch (e) { /* 組み立て前は出さない */ }
  try {
    const s = await call('GET', '/api/model/{a}/checks.json');
    for (const [k, p] of Object.entries(s.parts)) for (const n of p.notes) ul.insertAdjacentHTML('beforeend', `<li class="lv-情報"><span class="tag">${p.label}</span>${n}</li>`);
    for (const c of s.checks) ul.insertAdjacentHTML('beforeend', `<li class="lv-${c.level}"><span class="tag">${c.level}</span>${c.message}</li>`);
  } catch (e) { ul.innerHTML = '<li>まだ検査していません</li>'; }
}

function renderDownloads() {
  const files = [['model_bambu.3mf', '3MF（Bambu Studio 形式・外壁のファジースキン設定済み）おすすめ'], ['model.3mf', '3MF（ふつうの形式・設定は手で）'], ['model.glb', 'GLB（3D 表示用）'], ['foundation.stl', 'STL：基礎'],
    ['floor1.stl', 'STL：1階（床・内壁）'], ['exterior1.stl', 'STL：1階の外壁']];
  if (state.multi) {   // 2階建て
    files.push(['roof_lower.stl', 'STL：下屋の屋根'], ['roof_lower_ext.stl', 'STL：下屋の外壁'], ['floor2.stl', 'STL：2階（床・内壁）'],
      ['exterior2.stl', 'STL：2階の外壁'], ['roof.stl', 'STL：大屋根'], ['roof_ext.stl', 'STL：大屋根の外壁']);
  } else {
    files.push(['roof.stl', 'STL：屋根（屋根板・ポーチの天井）'], ['roof_ext.stl', 'STL：屋根の外壁']);
  }
  $('#downloads').innerHTML = files.map(([f, label]) => `<li><a href="${api('/api/model/{a}/' + f)}" download>${label}</a></li>`).join('');
}

// ---------------------------------------------------------------- 検品（SPEC §9-2・§10）
const VIEW_OF = { S: 'S', N: 'N', E: 'E', W: 'W', plan: 'plan', plan2: 'plan2', print: 'plan' };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

const FIELD = { thickness_mm: '厚み', width_mm: '幅', offset_mm: '壁の端からの位置', sill_fl_mm: '下端 FL+', head_fl_mm: '上端 FL+' };
for (const s of 'WESN') FIELD[`eave_mm.${s}`] = `軒の出（${SIDE[s]}）`;

function sourceText(vs) {   // 数値の出どころを日本語にする（例：majority:exterior → 外壁の多数派）
  const s = String(vs ?? '');
  if (s.startsWith('majority:')) return `${KIND[s.slice(9)] || s.slice(9)}の多数派`;
  if (s.startsWith('rule:')) return `会社ルール（${s.slice(5)}）`;
  if (s.startsWith('human:number:')) return `人が書いた数値 ${s.slice(13)}`;
  if (s === 'glb') return '元の形（glb）';
  return s;
}

function proposalText(p) {
  if (!p) return '';
  if (p.field === '_delete') return `${esc(p.target)} を取り除く`;
  return `${esc(p.target)} の ${esc(FIELD[p.field] || p.field)}：${esc(p.from)} → <b>${esc(p.to)}</b>（数値の出どころ：${esc(sourceText(p.value_source))}）`;
}

function findingItem(f, withActions) {
  const li = document.createElement('li'); li.className = 'finding';
  const ev = (f.evidence || []).map(esc).join('、');
  li.innerHTML = `<span class="tag lv-${esc(f.severity)}">${esc(f.severity)}</span><span class="tag">${esc(f.id)}</span>`
    + `<span class="st ${esc(f.status)}">${esc(f.status || '未確認')}</span>`
    + `<div class="msg">${esc(f.message)}</div>`
    + `<div class="meta">対象：${(f.targets || []).map(esc).join('・') || 'なし'}　根拠：${ev || 'なし'}${f.confidence ? '　確からしさ：' + esc(f.confidence) : ''}</div>`
    + (f.proposal ? `<div class="prop">直す案：${proposalText(f.proposal)}</div>` : '')
    + (f.proposal_error ? `<div class="meta">直す案は作れませんでした：${esc(f.proposal_error)}</div>` : '');
  const acts = document.createElement('div'); acts.className = 'acts';
  const view = VIEW_OF[f.view] || 'plan';
  const img = document.createElement('img'); img.hidden = true;
  const show = document.createElement('button'); show.textContent = '図を見る';
  show.addEventListener('click', () => {
    if (img.hidden) { img.src = api(`/api/inspect/{a}/image/${view}`) + `?hl=${encodeURIComponent((f.targets || []).join(','))}&t=${Date.now()}`; }
    img.hidden = !img.hidden; show.textContent = img.hidden ? '図を見る' : '図を閉じる';
  });
  acts.appendChild(show);
  if (withActions) {
    for (const [action, label] of [['approve', '承認'], ['reject', '却下'], ['later', 'あとで']]) {
      if (action === 'approve' && !f.proposal) continue;
      const b = document.createElement('button'); b.textContent = label; if (action === 'approve') b.className = 'primary';
      b.addEventListener('click', async () => {
        try {
          setStatus(action === 'approve' ? '直す案を修正として記録し、模型を組み直しています…' : '記録しています…', 'busy');
          await call('POST', `/api/findings/{a}/${encodeURIComponent(f.id)}`, { action });
          if (action === 'approve') await refresh(); else await renderFindings();
          setStatus(`${f.id} を「${label}」にしました${action === 'approve' ? '（修正の履歴から取り消せます）' : ''}`, 'ok');
        } catch (e) { setStatus('できませんでした：' + e.message, 'err'); }
      });
      acts.appendChild(b);
    }
  }
  li.appendChild(acts); li.appendChild(img);
  return li;
}

async function renderFindings() {
  const rep = await call('GET', '/api/findings/{a}');
  const fill = (sel, items, withActions, empty) => {
    const ul = $(sel); ul.innerHTML = '';
    if (!items || !items.length) { ul.innerHTML = `<li class="hint">${empty}</li>`; return; }
    for (const f of items) ul.appendChild(findingItem(f, withActions));
  };
  fill('#findings', rep.findings, true, rep.connection === 'off' ? 'AI なしで検品したので、Claude の指摘はありません' : 'まだありません');
  fill('#code-findings', rep.code_findings, true, 'ありません');
  const qs = $('#questions'); qs.innerHTML = '';
  const questions = (rep.questions || []).concat((rep.findings || []).filter((f) => f.question).map((f) => ({ id: f.id, ...f.question })));
  if (!questions.length) qs.innerHTML = '<li class="hint">ありません</li>';
  for (const q of questions) qs.insertAdjacentHTML('beforeend', `<li><span class="tag">${esc(q.id)}</span>${esc(q.question)}<div class="meta">選択肢：${(q.options || []).map(esc).join(' ／ ')}${q.evidence ? '　根拠：' + esc(q.evidence) : ''}</div></li>`);
  const cost = rep.cost_usd ? `　費用の目安 $${Number(rep.cost_usd).toFixed(2)}` : '';
  $('#inspect-summary').textContent = rep.summary ? `${rep.summary}${rep.duration_s ? `（${rep.duration_s} 秒${cost}）` : ''}` : '';
}

function renderProgress(st) {
  const box = $('#inspect-progress');
  const prog = st.progress; const s = st.state || {};
  if (!s.connection) { box.innerHTML = ''; return; }
  if (s.connection === 'off') { box.innerHTML = `<div class="face ${s.running ? '見ている' : '済み'}"><b>コードの検査</b>${s.running ? '検査中…' : '済み'}</div>`; return; }
  if (!prog) { box.innerHTML = ''; return; }
  box.innerHTML = Object.entries(prog.faces).map(([k, f]) =>
    `<div class="face ${esc(f.state)}"><b>${esc(st.face_labels[k] || k)}</b>${esc(f.state)}${f.doing ? '：' + esc(f.doing) : ''}</div>`).join('');
}

async function pollInspection() {
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
      if (s.error) setStatus('検品が終わりませんでした：' + s.error, 'err');
      else { await renderFindings(); setStatus(`検品が終わりました（${s.elapsed_s || 0} 秒）`, 'ok'); }
    }
  } catch (e) { setStatus('進み具合を読めませんでした：' + e.message, 'err'); }
}

function inspectButtons(disabled) {
  for (const id of ['#inspect-ai', '#inspect-api', '#inspect-off']) $(id).disabled = disabled;
}

async function startInspection(connection, opts = {}) {
  // opts.auto：組み立てたあとに自動で始める（設定で自分が選んだので、確かめの窓は出さない）
  if (!state.alias) return;
  if (!opts.auto && connection === 'claude_code' && !confirm('Claude に検品してもらいます。5分ほどかかり、Claude のプランの利用枠を使います。よろしいですか？')) return;
  if (connection === 'api_key') {
    const st = await call('GET', '/api/settings');
    if (!st.api_key_set) { setStatus('API キーが設定されていません。「設定」タブで API キーを入れてください', 'err'); return; }
    if (!opts.auto && !confirm('API キーで Claude に検品してもらいます。5分ほどかかり、キーの持ち主に API の料金がかかります（1回の上限 $3.00）。よろしいですか？')) return;
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

// ---------------------------------------------------------------- 設定（API キー）
// ---------------------------------------------------------------- 会社ルールの言語化（SPEC §5-4②）
// 家ごとに変わる値は物件ごとに glb から作る。会社で変わらない値と細かい決まりを会社ルールとして残し、予防に使う（作者 11/3）
const STATUS_JA = { same: '会社で同じ', varies: '物件ごとに違う', few: '件数が足りない', info: '参考' };
const RKIND_JA = { value: '値を直す', glb: '物件ごとに glb の値', skill: '細かい決まり' };
const companyOf = () => state.building?.source?.company_rules || 'jisha';
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
  if (!items.length) ul.innerHTML = '<li>まだありません</li>';
  for (const it of items) {
    const li = document.createElement('li');
    li.innerHTML = `<span class="tag">${esc(it.id)}</span>${esc(RKIND_JA[it.kind] || it.kind)}：${esc(it.text)}`
      + (it.key ? `（${esc(it.key)}：${esc(it.previous)} → ${esc(it.value)}）` : '') + `<br><span class="hint">根拠：${esc(it.evidence)}</span> `;
    const b = document.createElement('button'); b.textContent = '取り消す';
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
  if (!draft) { box.innerHTML = '<p class="hint">まだ案はありません。「案を書いてもらう」を押してください。</p>'; return; }
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

const AUTO_JA = { claude_code: 'Claude に検品してもらう（自分のプラン）', api_key: 'Claude に検品してもらう（API キー）',
  off: 'コードの検査だけ（AI なし）', none: '検品しない' };
async function renderSettings() {
  try {
    const st = await call('GET', '/api/settings');
    $('#apikey-state').textContent = st.api_key_set ? 'API キー：保存してあります' : 'API キー：まだ入っていません';
    $('#apikey-del').disabled = !st.api_key_set;
    const sel = $('#auto-inspect');
    sel.innerHTML = (st.connections || []).map((c) => `<option value="${c}">${AUTO_JA[c] || c}</option>`).join('');
    sel.value = st.auto_inspect; sel.dataset.prev = st.auto_inspect; sel.dataset.budget = st.max_budget_usd; sel.dataset.ack = st.api_cost_ack ? '1' : '';
  } catch (e) { $('#apikey-state').textContent = '設定を読めませんでした：' + e.message; }
}
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
renderSettings();

// ---------------------------------------------------------------- 囲んで直す（平面図、SPEC §9-3）
// 囲みの中の部材はサーバー（コード）が決める。画面は囲んだ点を平面の mm に直して送るだけ。
// source：最後に囲んだ図（plan＝平面図、3d＝3D）。3D は部材ごとの画素の数（hits）を送る
const lasso = { on: false, pts: [], line: null, polygon: null, selection: [], source: null,
  on3d: false, pts3d: [], line3d: null, hits: null, maskPx: 0, view: null };
const planSvg = () => $('#plan svg');

function svgPoint(svg, e) {
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

function toMm(svg, x, y) {   // 平面図の点 → 平面の mm（tools/plan_preview.py が書いた data-* を使う）
  const d = svg.dataset; const s = Number(d.scale); const m = Number(d.margin);
  return [(x - m) / s + Number(d.x0), Number(d.y1) - (y - m) / s];
}

function setLassoMode(on) {
  lasso.on = on;
  $('#lasso-toggle').classList.toggle('on', on);
  $('#lasso-toggle').textContent = on ? '囲むのをやめる' : '囲む';
  $('#plan').classList.toggle('lassoing', on);
}

function clearLasso() {
  lasso.pts = []; lasso.polygon = null; lasso.selection = []; lasso.source = null;
  if (lasso.line) { lasso.line.remove(); lasso.line = null; }
  lasso.pts3d = []; lasso.hits = null; lasso.maskPx = 0; lasso.view = null;
  if (lasso.line3d) { lasso.line3d.remove(); lasso.line3d = null; }
  clearHighlight3d();
  document.querySelectorAll('#plan [data-id].picked').forEach((el) => el.classList.remove('picked'));
  const wasOpen = !$('#lasso-panel').hidden;
  $('#lasso-panel').hidden = true; $('#lasso-result').innerHTML = '';
  if (wasOpen) resize();                                   // 欄を閉じると 3D の画面が広がる
}

$('#lasso-toggle').addEventListener('click', () => { if (planSvg()) setLassoMode(!lasso.on); });
$('#lasso-clear').addEventListener('click', clearLasso);

$('#plan').addEventListener('pointerdown', (e) => {
  const svg = planSvg(); if (!lasso.on || !svg) return;
  e.preventDefault(); clearLasso();
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
  if (lasso.pts.length < 3) { clearLasso(); return; }
  lasso.line.setAttribute('points', lasso.pts.concat([lasso.pts[0]]).map((q) => q.join(',')).join(' '));   // 囲みを閉じる
  setLassoMode(false);
  lasso.polygon = lasso.pts.map(([x, y]) => toMm(svg, x, y));
  lasso.source = 'plan';
  try {
    const res = await call('POST', '/api/lasso/{a}/select', { polygon: lasso.polygon, level: state.multi ? $('#plan-level').value : null });
    showPicked(res.selection);
  } catch (e) { setStatus('囲みを読めませんでした：' + e.message, 'err'); clearLasso(); }
});

// ---------------------------------------------------------------- 囲んで直す（3D）
// 画面に見えている部材だけを選ぶ。当たり判定用の形（/api/pick）を部材ごとの色で塗って画素を数え（pick3d.js）、
// 数をサーバーに送る。どれを選ぶかはサーバー（コード）が決める
const overlay = $('#lasso3d-overlay');
const SVGNS = 'http://www.w3.org/2000/svg';
const HL_MAT = new THREE.MeshBasicMaterial({ color: 0xe0008c, transparent: true, opacity: 0.45, depthTest: false, side: THREE.DoubleSide });

function setLasso3dMode(on) {
  if (on && state.exploded) { setStatus('パーツを戻してから囲んでください（離した表示では囲めません）', 'err'); return; }
  lasso.on3d = on; controls.enabled = !on;                 // 囲む間は回さない
  $('#lasso3d-toggle').classList.toggle('on', on);
  $('#lasso3d-toggle').textContent = on ? '囲むのをやめる' : '囲む';
  holder.classList.toggle('lassoing3d', on);
  if (on) setStatus('直したい部材（壁・窓・軒）をぐるっと囲んでください');   // 長いと上の帯が2行になり、画面がずれる
}
$('#lasso3d-toggle').addEventListener('click', () => { if (state.root) setLasso3dMode(!lasso.on3d); });

function clearHighlight3d() { if (state.hl) { scene.remove(state.hl); state.hl = null; } }
function highlight3d(rows) {        // 選んだ部材に半透明の色を重ねる
  clearHighlight3d();
  if (!state.pick) return;
  const ids = new Set(rows.map((r) => r.id));
  state.hl = new THREE.Group();
  state.pick.children.forEach((m) => { if (ids.has(m.userData.id)) state.hl.add(new THREE.Mesh(m.geometry, HL_MAT)); });
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
  e.preventDefault(); clearLasso();
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
  if (lasso.pts3d.length < 3) { clearLasso(); return; }
  lasso.line3d.setAttribute('points', lasso.pts3d.concat([lasso.pts3d[0]]).map((q) => q.join(',')).join(' '));
  setLasso3dMode(false);
  try {
    setStatus('囲みの中の部材を調べています…', 'busy');
    if (!state.pick) state.pick = buildPickGroup(await call('GET', '/api/pick/{a}'));
    const r = overlay.getBoundingClientRect();
    const { hits, maskPx } = pickHits(renderer, camera, state.root, state.pick, lasso.pts3d, r.width, r.height);
    Object.assign(lasso, { source: '3d', hits, maskPx, view: viewHint() });
    const res = await call('POST', '/api/lasso/{a}/select', { hits, mask_px: maskPx, view: lasso.view });
    if (lasso.line3d) { lasso.line3d.remove(); lasso.line3d = null; }   // 囲みの線は消し、選んだ部材に色を付ける
    showPicked(res.selection);
    highlight3d(res.selection);
    setStatus(`囲んだ部材 ${res.selection.length} 件`, 'ok');
  } catch (e) { setStatus('囲みを読めませんでした：' + e.message, 'err'); clearLasso(); }
});

function showPicked(rows) {
  lasso.selection = rows;
  for (const r of rows) document.querySelectorAll(`#plan [data-id="${CSS.escape(r.id)}"]`).forEach((el) => el.classList.add('picked'));
  $('#lasso-picked').innerHTML = rows.length
    ? '<b>囲んだ部材</b>' + rows.map((r) => `<div><span class="tag">${esc(r.id)}</span>${esc(r.label)}　${esc(r.text)}</div>`).join('')
    : '<span class="hint">囲みの中に部材がありません。壁や窓、軒をぐるっと囲んでください。</span>';
  $('#lasso-panel').hidden = false; $('#lasso-result').innerHTML = '';
  $('#lasso-ask').disabled = !rows.length; $('#lasso-table').disabled = !rows.length;
  resize();                                                // 欄が開いた分、3D の画面の大きさを合わせ直す
  if (rows.length) $('#lasso-text').focus();
}

$('#lasso-table').addEventListener('click', () => {   // AI なし：囲んだ部材の表の行を開く（軒は、その辺の軒の出の欄にも印）
  document.querySelector('.tabs button[data-tab="sedit"]').click();
  document.querySelectorAll('#sedit tr.picked').forEach((tr) => tr.classList.remove('picked'));
  document.querySelectorAll('#sedit input.picked-input').forEach((i) => i.classList.remove('picked-input'));
  const ids = new Set(lasso.selection.map((r) => r.id.split(':')[0]));
  const rows = [...document.querySelectorAll('#sedit tr[data-id]')].filter((tr) => ids.has(tr.dataset.id));
  rows.forEach((tr) => tr.classList.add('picked'));
  for (const r of lasso.selection.filter((x) => x.id.includes(':'))) {
    const [rid, side] = r.id.split(':');
    const inp = document.querySelector(`#sedit tr[data-id="${CSS.escape(rid)}"] input[data-field="eave_mm.${side}"]`);
    if (inp) inp.classList.add('picked-input');
  }
  if (rows[0]) rows[0].scrollIntoView({ block: 'center' });
  setStatus(`囲んだ部材 ${rows.length} 件の行に色を付けました。値を変えて「承認」で直せます`, 'ok');
});

async function askLasso(extra = '') {
  const text = $('#lasso-text').value.trim();
  if (!text) { setStatus('一言を書いてから「Claude に頼む」を押してください', 'err'); return; }
  const connection = $('#lasso-conn').value;
  if (connection === 'api_key' && !confirm('API キーで Claude に頼みます。キーの持ち主に API の料金がかかります（1回の上限 $1.00）。よろしいですか？')) return;
  $('#lasso-ask').disabled = true;
  setStatus('Claude が考えています（30秒〜数分）…', 'busy');
  try {
    const where = lasso.source === '3d' ? { hits: lasso.hits, mask_px: lasso.maskPx, view: lasso.view }
      : { polygon: lasso.polygon, level: state.multi ? $('#plan-level').value : null };
    const res = await call('POST', '/api/lasso/{a}/ask', { ...where, text: text + extra, connection });
    renderLassoResult(res);
    setStatus(`Claude の答えが届きました${res.cost_usd ? `（費用の目安 $${Number(res.cost_usd).toFixed(2)}）` : ''}`, 'ok');
  } catch (e) { setStatus('頼めませんでした：' + e.message, 'err'); }
  $('#lasso-ask').disabled = false;
}
$('#lasso-ask').addEventListener('click', () => askLasso());

function renderLassoResult(res) {
  const box = $('#lasso-result'); box.innerHTML = '';
  if (res.message) box.insertAdjacentHTML('beforeend', `<p class="msg">${esc(res.message)}</p>`);
  for (const p of res.proposals || []) {
    const div = document.createElement('div'); div.className = 'prop';
    div.innerHTML = `直す案：${proposalText(p)}<div class="meta">理由：${esc(p.reason)}</div>`;
    const ok = document.createElement('button'); ok.className = 'primary'; ok.textContent = '承認';
    ok.addEventListener('click', async () => {
      try {
        setStatus('修正を記録して、模型を組み直しています…', 'busy');
        await call('POST', '/api/lasso/{a}/approve', { target: p.target, field: p.field, value_source: p.value_source, reason: p.reason });
        clearLasso(); await refresh();
        setStatus(`${p.target} を直しました（修正の履歴から取り消せます）`, 'ok');
      } catch (e) { setStatus('直せませんでした：' + e.message, 'err'); }
    });
    div.appendChild(ok); box.appendChild(div);
  }
  for (const q of res.questions || []) {   // 選択肢を押すと、答えを添えてもう一度頼む
    const div = document.createElement('div'); div.className = 'question';
    div.innerHTML = `<b>質問</b>　${esc(q.question)}${q.evidence ? `<div class="meta">根拠：${esc(q.evidence)}</div>` : ''}`;
    for (const opt of q.options || []) {
      const b = document.createElement('button'); b.textContent = opt;
      b.addEventListener('click', () => askLasso(`\n（質問「${q.question}」への答え：${opt}）`));
      div.appendChild(b);
    }
    // 選択肢にない答え（図面の数値など）は書いて答える。数値は人が書いたものだけが案に使われる
    const ans = document.createElement('div'); ans.className = 'answer';
    const inp = document.createElement('input'); inp.type = 'text'; inp.placeholder = '答えを書く（例：図面の軒の出は 450）';
    const send = document.createElement('button'); send.textContent = '答える';
    send.addEventListener('click', () => { const v = inp.value.trim(); if (v) askLasso(`\n（質問「${q.question}」への答え：${v}）`); });
    ans.append(inp, send); div.appendChild(ans);
    box.appendChild(div);
  }
  if (res.connection !== 'off' && !(res.proposals || []).length && !(res.questions || []).length)
    box.insertAdjacentHTML('beforeend', '<p class="hint">直す案も質問もありませんでした。一言を変えて、もう一度頼んでください。</p>');
}

// ---------------------------------------------------------------- 全体
async function loadPlan() {   // 平面図（2階建ては選んだ階）
  const q = state.multi ? `?level=${encodeURIComponent($('#plan-level').value)}` : '';
  $('#plan').innerHTML = await call('GET', '/api/plan/{a}' + q);
}
$('#plan-level').addEventListener('change', async () => {
  clearLasso();
  try { await loadPlan(); } catch (e) { setStatus('平面図を読めませんでした：' + e.message, 'err'); }
});

async function refresh() {
  const data = await call('GET', '/api/building/{a}');
  state.building = data.building;
  state.pick = null; clearHighlight3d();                   // 組み直したら当たり判定用の形も読み直す（次に囲むとき）
  state.multi = (data.building.levels || []).length > 1;          // 2階建てか
  renderPartToggles();
  $('#plan-level').hidden = !state.multi;
  if (!state.multi) $('#plan-level').value = '1F';
  renderTables(data.building);
  await renderHistory(data.override_results);
  await loadPlan();
  await renderChecks(); renderDownloads(); await renderFindings(); await loadModel();
}

async function selectAlias(alias, built) {
  state.alias = alias; state.viewSet = false;
  if (!built) { setStatus(`${alias}：まだ組み立てていません。「読み込んで組み立てる」を押してください`); return; }
  try { setStatus('読み込んでいます…', 'busy'); await refresh(); setStatus(`${alias} を表示しています`, 'ok'); }
  catch (e) { setStatus(e.message, 'err'); }
}

$('#build').addEventListener('click', async () => {
  if (!state.alias) return;
  const btn = $('#build'); btn.disabled = true;
  let built = false;
  try { setStatus('glb を読んで、壁を作り直し、模型を組み立てています（数秒かかります）…', 'busy'); await call('POST', '/api/build/{a}'); await refresh(); setStatus(`${state.alias} を組み立てました`, 'ok'); built = true; }
  catch (e) { setStatus('組み立てられませんでした：' + e.message, 'err'); }
  finally { btn.disabled = false; }
  if (built) await autoInspect();
});

// 組み立てたあと、設定どおりに自動で検品を始める（SPEC §12）。検品タブを開いて、面ごとの担当が動く様子を見せる
async function autoInspect() {
  let st;
  try { st = await call('GET', '/api/settings'); } catch (e) { return; }
  const conn = st.auto_inspect;
  if (!conn || conn === 'none') return;
  if (conn === 'api_key' && !st.api_key_set) { setStatus('組み立てました。自動の検品は API キーが入っていないため始めませんでした（「設定」タブ）', 'err'); return; }
  document.querySelector('.tabs button[data-tab="sinsp"]').click();
  await startInspection(conn, { auto: true });
  setStatus(`${state.alias} を組み立てました。自動で検品を始めました（設定で変えられます）`, 'ok');
}

// URL の末尾でタブを開く（例：#sinsp で検品、#vplan で平面図）。撮影や説明のとき用
for (const id of (location.hash || '').slice(1).split(',').filter(Boolean)) {
  const btn = document.querySelector(`.tabs button[data-tab="${id}"]`);
  if (btn) btn.click();
}

(async () => {
  const props = await call('GET', '/api/properties');
  const sel = $('#alias');
  sel.innerHTML = props.map((p) => `<option value="${p.alias}" data-built="${p.built}">${p.alias}${p.has_glb ? '' : '（glb なし）'}</option>`).join('');
  sel.addEventListener('change', () => selectAlias(sel.value, sel.selectedOptions[0].dataset.built === 'true'));
  if (props.length) selectAlias(props[0].alias, props[0].built);
})();
