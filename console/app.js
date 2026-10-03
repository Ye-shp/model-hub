// Model Hub control room: live tasks, health, folders, memory, audience, phone, people, system.
'use strict';
const $ = id => document.getElementById(id);
let token = '';
let tab = 'now';
let phoneKeyShown = false;
const cache = {};

// ---------- helpers ----------
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null) e.textContent = text; if (cls) e.className = cls; return e; }
function btn(text, action, cls = '') { const b = el('button', text, cls); b.type = 'button'; b.addEventListener('click', ev => { ev.stopPropagation(); Promise.resolve().then(action).catch(e => notice(e.message)); }); return b; }
function notice(text = '') { $('notice').textContent = text; $('notice').hidden = !text; if (text) setTimeout(() => notice(''), 8000); }
function ago(value) {
  if (!value) return '';
  const t = typeof value === 'number' ? value * 1000 : Date.parse(value);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return Math.round(s) + 's ago'; if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago'; return Math.round(s / 86400) + 'd ago';
}
function bytes(n) { if (n == null) return ''; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; }
function badge(text, cls) { return el('span', text, 'badge ' + (cls || text || '')); }
function empty(target, text) { $(target).replaceChildren(el('div', text, 'empty')); }
function request(task) { return (task || '').split('CURRENT REQUEST:\n').pop().trim(); }
function projectFor(account) { return account === 'owner' ? 'default' : account === 'guest' ? 'friends' : account; }
function kv(target, pairs) {
  const box = $(target); box.replaceChildren();
  for (const [k, v] of pairs) { box.append(el('div', k, 'k')); const cell = el('div', undefined, 'v'); if (v instanceof Node) cell.append(v); else cell.textContent = v ?? '—'; box.append(cell); }
}
function bar(pct) { const b = el('div', undefined, 'bar' + (pct > 90 ? ' bad' : pct > 75 ? ' warn' : '')); const s = el('span'); s.style.width = Math.max(0, Math.min(100, pct)) + '%'; b.append(s); return b; }

async function api(path, body) {
  const headers = {'Content-Type': 'application/json'};
  if (token) headers.Authorization = 'Bearer ' + token;
  const r = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body)});
  if (r.status === 401) { showUnlock(); throw Error('Locked'); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : data.error || ('HTTP ' + r.status));
  return data;
}
async function download(path, name) {
  const headers = token ? {Authorization: 'Bearer ' + token} : {};
  const r = await fetch('/api/' + path, {headers});
  if (!r.ok) throw Error('Download failed (HTTP ' + r.status + ')');
  const url = URL.createObjectURL(await r.blob());
  const a = el('a'); a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
function imageFromBase64(b64, type = 'image/jpeg') {
  const raw = atob(b64); const buf = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
  const img = el('img'); img.alt = 'Phone screen'; img.src = URL.createObjectURL(new Blob([buf], {type})); return img;
}

// ---------- unlock ----------
function showUnlock() { $('app').hidden = true; $('unlock').hidden = false; }
$('unlock-form').addEventListener('submit', async ev => {
  ev.preventDefault(); token = $('key').value.trim();
  try { await api('health'); $('unlock').hidden = true; $('app').hidden = false; $('unlock-error').hidden = true; refresh(); }
  catch (e) { $('unlock-error').textContent = 'That key did not work.'; $('unlock-error').hidden = false; }
});

// ---------- tabs ----------
document.querySelectorAll('.tab-btn').forEach(b => b.addEventListener('click', () => { tab = b.dataset.tab; document.querySelectorAll('.tab-btn').forEach(x => x.classList.toggle('active', x === b)); document.querySelectorAll('.tab').forEach(s => s.hidden = s.id !== 'tab-' + tab); refresh(); }));
$('drawer-close').addEventListener('click', () => { $('drawer').hidden = true; });
$('files-close').addEventListener('click', () => { $('files-panel').hidden = true; });
$('who-filter').addEventListener('change', () => renderRecent());
let memoryTimer; $('memory-search').addEventListener('input', () => { clearTimeout(memoryTimer); memoryTimer = setTimeout(loadMemory, 300); });

// ---------- NOW ----------
async function loadNow() {
  const [health, activity] = await Promise.all([api('health'), api('activity?limit=80')]);
  cache.health = health; cache.activity = activity.jobs;
  const tiles = [];
  for (const g of health.gpus || []) {
    const pct = g.mem_total ? Math.round(100 * g.mem_used / g.mem_total) : 0;
    const t = el('div', undefined, 'tile'); t.append(el('div', `GPU ${g.index} · ${g.name}`, 'label'), el('div', `${g.util}%`, 'value'), el('div', `memory ${bytes(g.mem_used * 1048576)} of ${bytes(g.mem_total * 1048576)} · ${g.temp}°C`, 'sub'), bar(Number(g.util))); tiles.push(t);
  }
  if (Array.isArray(health.models)) {
    const chat = health.models.filter(m => m.model.startsWith('qwen'));
    const t = el('div', undefined, 'tile'); const run = chat.reduce((a, m) => a + m.running, 0); const wait = chat.reduce((a, m) => a + m.waiting, 0);
    t.append(el('div', 'Model requests', 'label'), el('div', `${run} running`, 'value'), el('div', `${wait} waiting · ${chat.map(m => `${m.model} ${m.running}/${m.capacity}`).join(' · ')}`, 'sub')); tiles.push(t);
  }
  const d = health.disks && (health.disks.chats || health.disks.data);
  if (d) { const t = el('div', undefined, 'tile'); t.append(el('div', 'Disk free', 'label'), el('div', `${d.free_gb} GB`, 'value'), el('div', `of ${d.total_gb} GB (${d.used_pct}% used)`, 'sub'), bar(d.used_pct)); tiles.push(t); }
  const jobs = Object.fromEntries((health.jobs || []).map(j => [j.status, j.n]));
  { const t = el('div', undefined, 'tile'); t.append(el('div', 'Tasks', 'label'), el('div', `${jobs.running || 0} running`, 'value'), el('div', `${jobs.queued || 0} waiting`, 'sub')); tiles.push(t); }
  if (health.image_box) { const t = el('div', undefined, 'tile'); t.append(el('div', 'Image box', 'label'), el('div', health.image_box.ok ? 'Up' : 'Down', 'value'), el('div', health.image_box.error || 'Qwen-Image', 'sub')); tiles.push(t); }
  $('tiles').replaceChildren(...tiles);

  const low = d && d.free_gb < 8, down = Array.isArray(health.models) ? false : true;
  $('pill').querySelector('.dot').className = 'dot ' + (down ? 'bad' : low ? 'warn' : 'ok');
  $('pill-text').textContent = down ? 'Models unreachable' : low ? 'Disk nearly full' : 'All systems normal';

  const active = activity.jobs.filter(j => j.status === 'running' || j.status === 'queued');
  if (!active.length) empty('active', 'Nothing running right now.');
  else $('active').replaceChildren(...active.map(j => jobItem(j, true)));
  const people = [...new Set(activity.jobs.map(j => j.requested_by || 'console'))];
  const sel = $('who-filter'); const keep = sel.value;
  sel.replaceChildren(el('option', 'Everyone'), ...people.map(p => { const o = el('option', p); o.value = p; return o; })); sel.options[0].value = ''; sel.value = people.includes(keep) ? keep : '';
  renderRecent();
}
function renderRecent() {
  const who = $('who-filter').value;
  const jobs = (cache.activity || []).filter(j => j.status !== 'running' && j.status !== 'queued' && (!who || (j.requested_by || 'console') === who)).slice(0, 30);
  if (!jobs.length) return empty('recent', 'No finished tasks yet.');
  $('recent').replaceChildren(...jobs.map(j => jobItem(j, false)));
}
function jobItem(j, live) {
  const item = el('div', undefined, 'item click');
  const line = el('div', undefined, 'line1'); line.append(el('div', request(j.task).slice(0, 160) || '(no text)', 'title'), badge(j.status));
  const meta = el('div', undefined, 'meta');
  meta.append(el('span', j.requested_by || 'console'), el('span', ago(j.started_at || j.created_at)), el('span', `${j.actions} actions`), el('span', `${j.files} files`));
  if (j.error) meta.append(el('span', j.error.slice(0, 120)));
  item.append(line, meta);
  if (live) { const row = el('div', undefined, 'actions'); row.append(btn('Stop', async () => { await api(`jobs/${j.id}/cancel`, {}); refresh(); }, 'small ghost danger')); item.append(row); }
  item.addEventListener('click', () => openJob(j.id));
  return item;
}
async function openJob(id) {
  const t = await api(`jobs/${id}/timeline`);
  $('drawer-title').textContent = request(t.task).slice(0, 80) || 'Task';
  const body = $('drawer-body'); body.replaceChildren();
  const info = el('div', undefined, 'kv');
  for (const [k, v] of [['Status', t.status], ['Who', t.requested_by || 'console'], ['Profile', t.profile], ['Started', t.started_at ? new Date(t.started_at).toLocaleString() : '—'], ['Finished', t.finished_at ? new Date(t.finished_at).toLocaleString() : '—'], ['Model calls', t.model_calls], ['Chat folder', t.thread || '—']]) { info.append(el('div', k, 'k'), el('div', String(v ?? '—'), 'v')); }
  body.append(info);
  const row = el('div', undefined, 'row');
  if (t.status === 'running' || t.status === 'queued') row.append(btn('Stop task', async () => { await api(`jobs/${id}/cancel`, {}); openJob(id); refresh(); }, 'ghost danger'));
  if (['failed', 'interrupted', 'cancelled'].includes(t.status)) row.append(btn('Run again from where it stopped', async () => { await api(`jobs/${id}/resume`, {}); openJob(id); refresh(); }));
  body.append(row);
  if (t.plan && t.plan.length) { body.append(el('h3', 'Plan')); const ul = el('ul', undefined, 'plan'); for (const s of t.plan) ul.append(el('li', s.title, s.status)); body.append(ul); }
  if (t.artifacts && t.artifacts.length) {
    body.append(el('h3', 'Files shared'));
    const list = el('div', undefined, 'list');
    for (const a of t.artifacts) { const it = el('div', undefined, 'item'); const l = el('div', undefined, 'line1'); l.append(el('span', a.name, 'title'), btn('Download', () => download(`artifacts/${a.id}`, a.name), 'small ghost')); it.append(l); list.append(it); }
    body.append(list);
  }
  if (t.result || t.error) { body.append(el('h3', t.result ? 'Reply' : 'Error')); body.append(el('div', t.result || t.error, 'result')); }
  body.append(el('h3', 'What it did'));
  const tl = el('div', undefined, 'timeline');
  for (const e of t.events) { if (['usage', 'model'].includes(e.kind)) continue; const r = el('div', undefined, 'ev'); r.append(el('span', new Date(e.created_at).toLocaleTimeString(), 't'), el('span', e.kind === 'artifact' ? 'Shared ' + (safeJSON(e.detail).name || 'a file') : e.detail, 'd')); tl.append(r); }
  body.append(tl);
  body.append(el('h3', 'Request (with the chat so far)'));
  body.append(el('div', t.task, 'result'));
  $('drawer').hidden = false;
}
function safeJSON(text) { try { return JSON.parse(text); } catch { return {}; } }

// ---------- CHATS ----------
async function loadChats() {
  const data = await api('chats');
  $('chats-free').textContent = `${data.free_gb} GB free on disk`;
  if (!data.chats.length) return empty('chats', 'No chat folders yet.');
  const head = el('div', undefined, 'trow head');
  for (const h of ['Who', 'Last request', 'Tasks', 'Size', 'Changed', '']) head.append(el('div', h, 'cell'));
  const rows = data.chats.map(c => {
    const r = el('div', undefined, 'trow');
    r.append(el('div', c.account === 'owner' ? 'You' : (c.who || c.owner), 'cell'), el('div', c.last_request || c.thread, 'cell'), el('div', String(c.tasks), 'cell'), el('div', bytes(c.bytes), 'cell'), el('div', ago(c.modified), 'cell'));
    const a = el('div', undefined, 'actions');
    a.append(btn('Files', () => openFiles(c), 'small ghost'));
    a.append(btn('Delete', async () => { if (!confirm(`Delete this chat's folder (${bytes(c.bytes)})? Files in it are gone for good.`)) return; await api('chats/delete', {account: c.account, thread: c.thread}); loadChats(); }, 'small ghost danger'));
    r.append(a); return r;
  });
  $('chats').replaceChildren(head, ...rows);
}
async function openFiles(c) {
  const project = projectFor(c.account);
  const data = await api(`workspace/files?project=${encodeURIComponent(project)}&thread=${encodeURIComponent(c.thread)}`);
  $('files-title').textContent = `Files · ${c.last_request ? c.last_request.slice(0, 60) : c.thread}`;
  $('files-panel').hidden = false;
  if (!data.files.length) return empty('files', 'This folder is empty.');
  $('files').replaceChildren(...data.files.map(f => {
    const it = el('div', undefined, 'item'); const l = el('div', undefined, 'line1');
    l.append(el('span', f.path, 'title'), el('span', bytes(f.bytes), 'muted small'));
    l.append(btn('Download', () => download(`workspace/file?project=${encodeURIComponent(project)}&thread=${encodeURIComponent(c.thread)}&path=${encodeURIComponent(f.path)}`, f.path.split('/').pop()), 'small ghost'));
    it.append(l); return it;
  }));
  $('files-panel').scrollIntoView({behavior: 'smooth'});
}

// ---------- MEMORY ----------
async function loadMemory() {
  const data = await api('memories?search=' + encodeURIComponent($('memory-search').value));
  if (!data.memories.length) return empty('memories', 'Nothing saved yet. Cowork saves preferences, decisions and facts as it works.');
  $('memories').replaceChildren(...data.memories.map(m => {
    const it = el('div', undefined, 'item');
    const l = el('div', undefined, 'line1'); l.append(el('span', m.title, 'title'), badge(m.kind));
    const meta = el('div', undefined, 'meta'); meta.append(el('span', m.project_name), el('span', ago(m.updated_at)));
    const text = el('div', m.content, 'text');
    const a = el('div', undefined, 'actions');
    a.append(btn('Edit', () => {
      const area = el('textarea'); area.value = m.content;
      const save = btn('Save', async () => { await api(`memories/${m.id}`, {title: m.title, content: area.value, kind: m.kind}); loadMemory(); }, 'small');
      text.replaceWith(area); a.replaceChildren(save, btn('Cancel', loadMemory, 'small ghost'));
    }, 'small ghost'));
    a.append(btn('Forget', async () => { if (!confirm('Forget this memory?')) return; await api(`memories/${m.id}/delete`, {}); loadMemory(); }, 'small ghost danger'));
    it.append(l, meta, text, a); return it;
  }));
}

// ---------- AUDIENCE ----------
const audience = {selected: null, project: 'default', request: 0, exportURL: null, exportJSONLURL: null};
const audienceMetricLabels = {
  views: 'Views', likes: 'Likes', comments: 'Comments', shares: 'Shares', saves: 'Saves',
  reach: 'People reached', average_watch_seconds: 'Average watch (seconds)',
  completion_rate: 'Completed viewing (%)', followers: 'Followers', conversions: 'Conversions',
};
function audienceProject() { return $('audience-project').value.trim() || 'default'; }
function audienceQuery(project = audienceProject()) { return 'project=' + encodeURIComponent(project); }
function clearAudienceExport() {
  if (audience.exportURL) URL.revokeObjectURL(audience.exportURL);
  if (audience.exportJSONLURL) URL.revokeObjectURL(audience.exportJSONLURL);
  audience.exportURL = null;
  audience.exportJSONLURL = null;
  $('audience-export-report').replaceChildren();
}
function audienceText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') return value.label || value.level || JSON.stringify(value);
  return String(value);
}
function audienceTime(value) {
  if (!value) return '—';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? audienceText(value) : date.toLocaleString();
}
function audienceLocalTime() {
  const date = new Date();
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}
function audienceISO(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) throw Error('Enter a valid date and time.');
  return date.toISOString();
}
function audienceLink(value) {
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol)) return el('span', audienceText(value));
    const link = el('a', 'Open post'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
    return link;
  } catch { return el('span', audienceText(value)); }
}
function audienceInfo(pairs) {
  const box = el('div', undefined, 'kv audience-info');
  for (const [key, value] of pairs) {
    box.append(el('div', key, 'k'));
    const cell = el('div', undefined, 'v');
    if (value instanceof Node) cell.append(value); else cell.textContent = audienceText(value);
    box.append(cell);
  }
  return box;
}
function audienceField(form, name, label, type = 'text', value = '', options = {}) {
  const wrap = el('label', label, options.wide ? 'form-wide' : '');
  const field = el(type === 'textarea' ? 'textarea' : type === 'select' ? 'select' : 'input');
  field.name = name;
  if (type === 'select') for (const [val, title] of options.choices || []) {
    const option = el('option', title); option.value = val; field.append(option);
  }
  else if (type !== 'textarea') field.type = type;
  field.value = value ?? '';
  for (const key of ['required', 'min', 'max', 'step', 'maxLength', 'placeholder', 'pattern']) {
    if (options[key] !== undefined) field[key] = options[key];
  }
  wrap.append(field); form.append(wrap); return field;
}
function audienceForm(title, fields, action, buttonLabel) {
  const details = el('details'); details.append(el('summary', title));
  const form = el('form', undefined, 'audience-form');
  const grid = el('div', undefined, 'form-grid'); fields(grid); form.append(grid);
  const submit = el('button', buttonLabel); submit.type = 'submit';
  form.append(submit, el('p', '', 'form-message'));
  form.querySelector('.form-message').setAttribute('role', 'status');
  form.querySelector('.form-message').setAttribute('aria-live', 'polite');
  form.addEventListener('submit', ev => audienceSubmit(ev, action));
  details.append(form); return details;
}
async function audienceSubmit(ev, action) {
  ev.preventDefault();
  const form = ev.currentTarget, submit = form.querySelector('button[type=submit]');
  const message = form.querySelector('.form-message');
  submit.disabled = true; message.textContent = 'Saving…'; message.classList.remove('error');
  try { await action(new FormData(form)); if (message.textContent === 'Saving…') message.textContent = 'Saved.'; }
  catch (error) { message.textContent = error.message; message.classList.add('error'); }
  finally { submit.disabled = false; }
}
async function loadAudience() {
  const project = audienceProject(), requestId = ++audience.request;
  if (project !== audience.project) { audience.project = project; audience.selected = null; clearAudienceExport(); }
  const [list, performance] = await Promise.all([
    api('audience/experiments?' + audienceQuery(project)),
    api('audience/performance?' + audienceQuery(project)),
  ]);
  if (requestId !== audience.request || project !== audienceProject()) return;
  const experiments = list.experiments || [];
  $('audience-count').textContent = String(experiments.length);
  if (!experiments.some(item => String(item.id) === String(audience.selected))) audience.selected = null;
  if (!experiments.length) empty('audience-experiments', 'Create an experiment, then attach two drafts to compare.');
  else $('audience-experiments').replaceChildren(...experiments.map(experiment => {
    const item = el('div', undefined, 'item' + (String(experiment.id) === String(audience.selected) ? ' selected' : ''));
    const line = el('div', undefined, 'line1');
    line.append(el('span', experiment.name, 'title'), badge(experiment.split === 'eval' ? 'Evaluation' : 'Learning'));
    item.append(line, el('div', [experiment.platform, experiment.account, experiment.kind].filter(Boolean).join(' · '), 'meta'),
      btn('View drafts and results', async () => {
        audience.selected = experiment.id; await loadAudience();
      }, 'small ghost'));
    return item;
  }));
  renderAudienceResults(performance);
  if (audience.selected !== null) await loadAudienceDetail(project, audience.selected, requestId);
  else { $('audience-detail-title').textContent = 'Experiment details'; empty('audience-detail', 'Choose an experiment to see its drafts and collection schedule.'); }
}
async function loadAudienceDetail(project, id, requestId = audience.request) {
  const detail = await api(`audience/experiments/${encodeURIComponent(id)}?${audienceQuery(project)}`);
  if (requestId !== audience.request || project !== audienceProject() || String(id) !== String(audience.selected)) return;
  const experiment = detail.experiment || {}, box = $('audience-detail');
  $('audience-detail-title').textContent = experiment.name || 'Experiment details';
  box.replaceChildren(audienceInfo([
    ['Use', experiment.split === 'eval' ? 'Evaluation only' : 'Learning'],
    ['Platform / account', [experiment.platform, experiment.account].filter(Boolean).join(' · ')],
    ['Format', experiment.kind], ['Posting context', experiment.context],
    ['Brief', experiment.brief], ['Hypothesis', experiment.hypothesis],
  ]));
  box.append(audienceForm('Attach a draft', grid => {
    audienceField(grid, 'label', 'Draft label', 'text', '', {required: true, maxLength: 160, placeholder: 'A — opening question'});
    audienceField(grid, 'post_id', 'Saved post draft ID (optional)', 'number', '', {min: 1, step: 1});
    audienceField(grid, 'model', 'Declared model (optional)', 'text', '', {maxLength: 300});
    audienceField(grid, 'strategy', 'Declared strategy (optional)', 'text', '', {maxLength: 2000, placeholder: 'What you changed in this draft'});
    audienceField(grid, 'response', 'Draft text or script', 'textarea', '', {wide: true, required: true, maxLength: 50000});
  }, async data => {
    await api('audience/variants', {
      project, experiment_id: id, label: data.get('label'), response: data.get('response'),
      post_id: data.get('post_id') === '' ? null : Number(data.get('post_id')),
      model: data.get('model'), strategy: data.get('strategy'),
    });
    await loadAudience();
  }, 'Attach draft'));
  box.append(el('p', 'Model and strategy describe the origin you declare; this record does not verify which model wrote the draft.', 'muted small'));
  const variants = detail.variants || [];
  if (!variants.length) box.append(el('div', 'No drafts attached yet.', 'empty'));
  else for (const variant of variants) box.append(audienceVariant(variant, experiment, project));
  box.append(el('h3', 'Collection schedule'));
  const checkpoints = detail.checkpoints || [];
  if (!checkpoints.length) box.append(el('div', 'Record a published post to schedule its delayed checks.', 'empty'));
  for (const checkpoint of checkpoints) {
    const item = el('div', undefined, 'item');
    const variant = variants.find(value => String(value.id) === String(checkpoint.variant_id));
    const line = el('div', undefined, 'line1');
    line.append(el('span', `${variant?.label || 'Draft ' + checkpoint.variant_id} · ${checkpoint.horizon_hours} hours`, 'title'), badge(checkpoint.status));
    item.append(line, el('div', `Due ${audienceTime(checkpoint.due_at)} · ${checkpoint.attempts || 0} attempts`, 'meta'));
    if (checkpoint.next_attempt_at) item.append(el('div', 'Next attempt ' + audienceTime(checkpoint.next_attempt_at), 'muted small'));
    if (checkpoint.last_error) item.append(el('div', checkpoint.last_error, 'error small text'));
    box.append(item);
  }
}
function audienceVariant(variant, experiment, project) {
  const item = el('div', undefined, 'item audience-variant'), line = el('div', undefined, 'line1');
  line.append(el('span', variant.label || 'Draft ' + variant.id, 'title'), badge(variant.published_at ? 'Published' : 'Draft'));
  item.append(line, el('div', variant.response, 'text audience-draft'));
  item.append(audienceInfo([
    ['Saved post draft', variant.post_id], ['Declared model', variant.model], ['Declared strategy', variant.strategy],
    ['Published', audienceTime(variant.published_at)], ['Exposure', variant.exposure],
    ['Post ID', variant.remote_id], ['Post', variant.url ? audienceLink(variant.url) : null],
  ]));
  if (!variant.published_at) item.append(audienceForm('Record a published post', grid => {
    audienceField(grid, 'remote_id', 'Published post ID', 'text', '', {required: true, maxLength: 40, pattern: '[0-9]{1,40}', placeholder: 'Numeric video or media ID'});
    audienceField(grid, 'url', 'Published post link', 'url', '', {required: true, maxLength: 2000});
    audienceField(grid, 'published_at', 'Published at (your local time)', 'datetime-local', audienceLocalTime(), {required: true});
    audienceField(grid, 'account', 'Publishing account', 'text', experiment.account || '', {required: true, maxLength: 200});
    audienceField(grid, 'exposure', 'Exposure', 'select', 'organic', {choices: [['organic', 'Organic only'], ['paid', 'Paid promotion'], ['mixed', 'Mixed organic and paid'], ['unknown', 'Unknown']]});
  }, async data => {
    const url = new URL(data.get('url'));
    if (url.protocol !== 'https:') throw Error('Use the HTTPS link to the published post.');
    await api(`audience/variants/${encodeURIComponent(variant.id)}/publication`, {
      project, remote_id: data.get('remote_id'), url: url.href,
      published_at: audienceISO(data.get('published_at')), account: data.get('account'), exposure: data.get('exposure'),
    });
    await loadAudience();
  }, 'Save publication record'));
  if (variant.published_at) {
    if (variant.exposure === 'organic') {
      item.append(audienceForm('Exclude from organic comparisons', grid => {
        audienceField(grid, 'exposure', 'Exposure after publication', 'select', 'paid', {wide: true, choices: [['paid', 'Paid promotion'], ['mixed', 'Mixed organic and paid'], ['unknown', 'Unknown']]});
        grid.append(el('p', 'Use this if a post was boosted later or its exposure is uncertain. The publication identity and observations stay recorded; this draft is excluded from organic comparisons. The exclusion cannot be undone.', 'muted small form-wide'));
      }, async data => {
        await api(`audience/variants/${encodeURIComponent(variant.id)}/publication`, {
          project, remote_id: variant.remote_id, url: variant.url, published_at: variant.published_at,
          account: variant.account, exposure: data.get('exposure'),
        });
        await loadAudience();
      }, 'Exclude draft'));
    }
    item.append(audienceForm('Record counts manually', grid => {
      audienceField(grid, 'observed_at', 'Observed at (your local time)', 'datetime-local', audienceLocalTime(), {required: true, wide: true});
      for (const [key, label] of Object.entries(audienceMetricLabels)) {
        audienceField(grid, key, label, 'number', '', {min: 0, max: key === 'completion_rate' ? 100 : undefined, step: key === 'average_watch_seconds' || key === 'completion_rate' ? 'any' : 1});
      }
    }, async data => {
      const metrics = {};
      for (const key of Object.keys(audienceMetricLabels)) {
        metrics[key] = data.get(key) === '' ? null : Number(data.get(key));
        if (key === 'completion_rate' && metrics[key] !== null) metrics[key] /= 100;
      }
      if (!Object.values(metrics).some(value => value !== null)) throw Error('Enter at least one observed count. Leave unavailable counts blank.');
      await api(`audience/variants/${encodeURIComponent(variant.id)}/metrics`, {
        project, metrics, observed_at: audienceISO(data.get('observed_at')),
      });
      await loadAudience();
    }, 'Save counts'));
    item.append(el('p', 'Blank means unavailable. Enter zero only when the platform reports zero. Counts are stored with the observation time.', 'muted small'));
  }
  const snapshots = variant.snapshots || [];
  if (snapshots.length) {
    const details = el('details'); details.append(el('summary', `Observations (${snapshots.length})`));
    for (const snapshot of snapshots) {
      const observation = el('div', undefined, 'item');
      observation.append(el('div', `${audienceTime(snapshot.observed_at)} · ${audienceText(snapshot.source)}`, 'meta'));
      const values = Object.entries(audienceMetricLabels).map(([key, label]) => {
        const value = snapshot.metrics?.[key];
        return [label, key === 'completion_rate' && value != null ? `${(Number(value) * 100).toFixed(1)}%` : value];
      });
      observation.append(audienceInfo(values));
      for (const warning of Array.isArray(snapshot.warnings) ? snapshot.warnings : snapshot.warnings ? [snapshot.warnings] : []) observation.append(el('div', audienceText(warning), 'muted small text'));
      details.append(observation);
    }
    item.append(details);
  }
  if (variant.reward) item.append(audienceReward(variant.reward, variant.selected_snapshot));
  return item;
}
function audienceReward(reward, snapshot) {
  const baseline = reward.baseline_kind === 'historical' ? `Historical posts (${reward.baseline_count || 0})` : reward.baseline_kind === 'experiment_peers' ? 'Experiment median' : null;
  const completion = snapshot?.metrics?.completion_rate;
  return audienceInfo([
    ['Reach score', reward.score == null ? 'Not scored yet' : Number(reward.score).toFixed(3)],
    ['Views', reward.views], ['Sample support', typeof reward.confidence === 'number' ? `${(reward.confidence * 100).toFixed(0)}%` : reward.confidence],
    ['Baseline', baseline], ['Reach relative to baseline', reward.reach_lift == null ? null : `${Number(reward.reach_lift).toFixed(2)}×`],
    ['Share rate', reward.share_rate == null ? null : `${(reward.share_rate * 100).toFixed(2)}%`],
    ['Completed viewing', completion == null ? null : `${(completion * 100).toFixed(1)}%`],
    ['Observed', snapshot ? `${audienceTime(snapshot.observed_at)} · ${Number(snapshot.age_hours).toFixed(1)} hours after publication` : null],
    ['Observation window', reward.horizon_hours == null ? null : `${reward.horizon_hours} hours`],
    ['Comparison status', reward.eligible ? 'Eligible' : reward.reason || 'Waiting for comparable results'],
  ]);
}
function renderAudienceResults(performance) {
  $('audience-score-version').textContent = performance.score_version || '';
  const results = performance.results || [];
  if (!results.length) return empty('audience-results', 'Results appear once a published draft has observations.');
  $('audience-results').replaceChildren(...results.map(result => {
    const item = el('div', undefined, 'item'), reward = result.reward || result;
    item.append(el('div', result.label || result.variant?.label || `Draft ${result.variant_id || result.id || ''}`, 'title'));
    if (result.experiment_name || result.experiment?.name) item.append(el('div', result.experiment_name || result.experiment.name, 'meta'));
    item.append(audienceReward(reward, result.selected_snapshot)); return item;
  }));
}
$('audience-project').addEventListener('change', () => loadAudience().catch(error => notice(error.message)));
$('audience-refresh').addEventListener('click', () => loadAudience().catch(error => notice(error.message)));
$('audience-experiment-form').addEventListener('submit', ev => audienceSubmit(ev, async data => {
  const body = {project: audienceProject()};
  for (const key of ['name', 'brief', 'hypothesis', 'platform', 'account', 'kind', 'context', 'split']) body[key] = data.get(key);
  const created = await api('audience/experiments', body);
  audience.selected = created.id; audience.project = body.project;
  $('audience-experiment-form').reset(); $('audience-create').open = false; await loadAudience();
}));
$('audience-export-form').addEventListener('submit', ev => audienceSubmit(ev, async data => {
  const project = audienceProject();
  const query = new URLSearchParams({project, horizon_hours: data.get('horizon_hours'), min_views: data.get('min_views'), min_margin: data.get('min_margin')});
  const exported = await api('audience/preferences?' + query.toString());
  if (project !== audienceProject()) throw Error('The project changed. Export again for the selected project.');
  clearAudienceExport();
  const report = $('audience-export-report');
  audience.exportURL = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2) + '\n'], {type: 'application/json'}));
  const audit = el('a', 'Download comparison audit'); audit.href = audience.exportURL; audit.download = 'audience-comparison-audit.json';
  const downloads = el('div', undefined, 'row');
  report.append(downloads);
  if (exported.dataset_digest) report.append(el('div', `Exported ${audienceTime(exported.exported_at)} · dataset digest ${exported.dataset_digest}`, 'muted small text'));
  const records = exported.records || [];
  for (const comparison of exported.comparisons || []) report.append(el('div', `Experiment ${comparison.experiment_id}: preferred draft ${comparison.chosen_id}, other draft ${comparison.rejected_id} · margin ${Number(comparison.margin).toFixed(3)} · sample support ${typeof comparison.confidence === 'number' ? (comparison.confidence * 100).toFixed(0) + '%' : audienceText(comparison.confidence)}`, 'item small'));
  for (const skipped of exported.skipped || []) report.append(el('div', `Experiment ${skipped.experiment_id}: ${skipped.reason}`, 'item muted small'));
  if (records.length) {
    audience.exportJSONLURL = URL.createObjectURL(new Blob([records.map(record => JSON.stringify({prompt: record.prompt, chosen: record.chosen, rejected: record.rejected})).join('\n') + '\n'], {type: 'application/x-ndjson'}));
    const link = el('a', 'Download preference examples'); link.href = audience.exportJSONLURL; link.download = 'audience-preferences.jsonl';
    downloads.append(link);
  }
  downloads.append(audit);
  $('audience-export-form').querySelector('.form-message').textContent = records.length ? `Prepared ${records.length} preference example${records.length === 1 ? '' : 's'}. Use the download links below.` : 'No eligible comparisons yet. Review the reasons below.';
}));

// ---------- PHONE ----------
async function loadPhone() {
  const p = await api('phone');
  cache.phone = p;
  $('phone-state').textContent = p.connected ? 'connected' : 'offline'; $('phone-state').className = 'badge ' + (p.connected ? 'connected' : 'offline');
  const i = p.info || {};
  kv('phone-info', [['Phone', i.device ? `${i.model || ''} (${i.device})` : 'none'], ['Android', i.android], ['Screen', i.size ? i.size.join(' × ') : null], ['PC', i.host], ['Last contact', p.last_seen ? ago(p.last_seen) : 'never']]);
  const url = p.bridge_url || location.origin.replace('://console.', '://api.');
  const key = phoneKeyShown ? p.key : p.key.slice(0, 8) + '…';
  $('phone-command').textContent = `python bridge.py --url ${url} --key ${key}`;
  cache.phoneCommand = `python bridge.py --url ${url} --key ${p.key}`;
  $('phone-reveal').textContent = phoneKeyShown ? 'Hide key' : 'Show key';
  if (p.screen && p.screen.jpeg && cache.phoneShot !== p.screen.at) { cache.phoneShot = p.screen.at; $('phone-screen').replaceChildren(imageFromBase64(p.screen.jpeg)); }
  kv('phone-posts', (p.posts || []).length ? p.posts.map(r => [r.platform, `${r.n} posts · last ${ago(r.last)}`]) : [['Posts', 'none yet']]);
  $('phone-log').replaceChildren(...(p.log || []).slice().reverse().map(l => el('div', `${new Date(l.at).toLocaleTimeString()}  ${l.text}`)));
  if (!(p.log || []).length) $('phone-log').replaceChildren(el('div', 'No activity yet.', 'muted'));
  $('phone-shot').disabled = !p.connected;
}
$('phone-copy').addEventListener('click', async () => { try { await navigator.clipboard.writeText(cache.phoneCommand || ''); notice(''); $('phone-copy').textContent = 'Copied'; setTimeout(() => $('phone-copy').textContent = 'Copy command', 1500); } catch { notice('Copy failed; select the text instead.'); } });
$('phone-reveal').addEventListener('click', () => { phoneKeyShown = !phoneKeyShown; loadPhone().catch(e => notice(e.message)); });
$('phone-rotate').addEventListener('click', async () => { if (!confirm('Make a new bridge key? The bridge on the PC stops working until you restart it with the new key.')) return; await api('phone/key', {}); phoneKeyShown = true; loadPhone(); });
$('phone-shot').addEventListener('click', async () => { $('phone-shot').disabled = true; try { await api('phone/screen', {}); await loadPhone(); } catch (e) { notice(e.message); } finally { $('phone-shot').disabled = false; } });

// ---------- PEOPLE ----------
async function loadPeople() {
  const o = await api('overview');
  const head = el('div', undefined, 'trow people head');
  for (const h of ['Person', 'Tasks', 'Running', 'Done', 'Failed', 'Tokens in / out', 'Last task']) head.append(el('div', h, 'cell'));
  const rows = o.people.map(p => { const r = el('div', undefined, 'trow people'); for (const v of [p.who, p.tasks, p.active, p.completed, p.failed, `${(p.input_tokens / 1000).toFixed(0)}K / ${(p.output_tokens / 1000).toFixed(0)}K`, ago(p.last_task)]) r.append(el('div', String(v ?? 0), 'cell')); return r; });
  $('people').replaceChildren(head, ...rows);
  const c = o.connections || {};
  if (c.error) kv('connections', [['Status', c.error]]);
  else kv('connections', Object.entries(c).map(([k, v]) => [k === 'claude' ? 'Claude Code' : 'Codex', `${v.signed_in ? 'connected' : 'not connected'}${v.installed ? '' : ' (not installed)'} · ${v.used_today}/${v.daily_limit} today`]));
  try {
    const [social, posts] = await Promise.all([api('connections/social'), api('posts')]);
    const names = {x: 'X', instagram: 'Instagram posting', tiktok: 'TikTok analytics', bluesky: 'Bluesky', github: 'GitHub', scrapecreators: 'ScrapeCreators'};
    kv('social', [['Research tools', social.tools.state + (social.tools.detail ? ' — ' + social.tools.detail.slice(0, 160) : '')],
      ...Object.entries(social.accounts).map(([k, v]) => [names[k] || k, v.connected ? 'connected' + (v.username ? ' as @' + v.username : '') : 'not connected'])]);
    if (!posts.posts.length) empty('posts', 'No drafts yet.');
    else $('posts').replaceChildren(...posts.posts.map(p => { const it = el('div', undefined, 'item'); const l = el('div', undefined, 'line1');
      l.append(el('span', `#${p.id} · ${p.platform}${p.kind ? ' ' + p.kind : ''}`, 'title'), badge(p.status, {published: 'completed', draft: 'queued', on_phone: 'running'}[p.status] || p.status));
      it.append(l, el('div', p.caption.slice(0, 280), 'text'), el('div', ago(p.updated_at), 'meta')); return it; }));
  } catch (e) { kv('social', [['Status', e.message]]); }
  if (!o.escalations.length) empty('handoffs', 'No hand-offs yet.');
  else $('handoffs').replaceChildren(...o.escalations.map(e => { const it = el('div', undefined, 'item'); it.append(el('div', e.detail, 'text'), el('div', `${e.requested_by || ''} · ${ago(e.created_at)}`, 'meta')); return it; }));
}

// ---------- SYSTEM ----------
async function loadSystem() {
  const [h, m] = await Promise.all([api('health'), api('admin/migrate')]);
  kv('system', [['App code', h.code ? h.code.slice(0, 12) : 'built into the image'], ['Startup', h.startup ? `${h.startup.phase}${h.startup.detail ? ' — ' + h.startup.detail : ''}` : '—'],
    ['GPUs', (h.gpus || []).map(g => `${g.index}: ${g.name}`).join(', ') || '—'],
    ['Models', Array.isArray(h.models) ? h.models.map(x => `${x.model} ${x.running}/${x.capacity}`).join(' · ') : 'unreachable'],
    ['Image box', h.image_box ? (h.image_box.ok ? 'up' : 'down') : 'not configured'], ['Checked', new Date(h.at).toLocaleTimeString()]]);
  $('disks').replaceChildren(...Object.entries(h.disks || {}).map(([k, d]) => { const it = el('div', undefined, 'item'); const l = el('div', undefined, 'line1'); l.append(el('span', `${k} (${d.path})`, 'title'), el('span', `${d.free_gb} GB free of ${d.total_gb} GB`, 'muted small')); it.append(l, bar(d.used_pct)); return it; }));
  $('migrate-state').textContent = m.state; $('migrate-state').className = 'badge ' + m.state;
  kv('migrate', m.state === 'idle' ? [['Status', 'Not in progress']] : [['Target', m.target], ['Sent', bytes(m.bytes)], ['Files', m.files], ['Error', m.error], ['Receiver', m.receiver ? JSON.stringify(m.receiver) : null]]);
}

// ---------- loop ----------
const loaders = {now: loadNow, chats: loadChats, memory: loadMemory, audience: loadAudience, phone: loadPhone, people: loadPeople, system: loadSystem};
let busy = false;
async function refresh() {
  if (busy) return; busy = true;
  try { await loaders[tab](); $('app').hidden = false; $('unlock').hidden = true; }
  catch (e) { if (e.message !== 'Locked') notice(e.message); }
  finally { busy = false; }
}
setInterval(() => { if (!document.hidden && !$('app').hidden && ['now', 'phone'].includes(tab)) refresh(); }, 5000);
setInterval(() => {
  if (!document.hidden && !$('app').hidden && tab === 'audience' && !document.querySelector('#tab-audience details[open]')) refresh();
}, 30000);
refresh();
