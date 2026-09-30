// Model Hub control room: live tasks, box health, chat folders, memory, phone bridge, people, system.
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
const loaders = {now: loadNow, chats: loadChats, memory: loadMemory, phone: loadPhone, people: loadPeople, system: loadSystem};
let busy = false;
async function refresh() {
  if (busy) return; busy = true;
  try { await loaders[tab](); $('app').hidden = false; $('unlock').hidden = true; }
  catch (e) { if (e.message !== 'Locked') notice(e.message); }
  finally { busy = false; }
}
setInterval(() => { if (!document.hidden && !$('app').hidden && ['now', 'phone'].includes(tab)) refresh(); }, 5000);
refresh();
