// Installs Qwen Cowork and the agent teams into the chat site (Open WebUI "Pipe" functions) through its
// Unix socket. Used by supervise.mjs on every start; kept separate so it can be tested on its own.
import {readFile} from 'node:fs/promises';
import {request as httpRequest} from 'node:http';
import {join} from 'node:path';

// Install Qwen Cowork and the agent teams into the chat site (Open WebUI "Pipe" functions) on every
// start, because the site's database lives on the container disk and a redeploy starts it empty. Signs
// in as OWNER_EMAIL through the same trusted header Cloudflare Access sets, so that account is created
// (as the first user, the site's admin) if it doesn't exist yet.
function webui(socket, path, {method = 'GET', headers = {}, body} = {}) {
  return new Promise((resolve, reject) => {
    const target = socket ? {socketPath: socket} : {host: '127.0.0.1', port: 3000};
    const req = httpRequest({...target, path: `/api/v1${path}`, method, headers: {host: 'localhost', ...headers}}, res => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', chunk => { text += chunk; });
      res.on('end', () => resolve({ok: res.statusCode < 400, status: res.statusCode, text, json: () => JSON.parse(text)}));
    });
    req.on('error', reject);
    req.setTimeout(30000, () => req.destroy(new Error('timeout')));
    if (body) req.write(body);
    req.end();
  });
}

export async function installAgents({env, socket, dataDir, integrations = '/opt/hub/integrations', stopping = () => false, attempts = 60, log = console}) {
  const call = (path, options) => webui(socket, path, options);
  if (env.ENABLE_AGENT_CONSOLE !== 'true' || !env.OWNER_EMAIL) return;
  const imagesOn = env.MODEL3_KIND === 'image' && Boolean(env.MODEL3_URL);
  // Both are the same Pipe file: Qwen Cowork for autonomous projects, Qwen (chat) for conversation with the same tools.
  // Owner only: the Pipe refuses anyone who isn't the site's admin.
  const functions = [
    {id: 'cowork', name: 'Qwen Cowork', file: 'openwebui_cowork.py',
      description: 'Say what you want done; Qwen plans and does it with a shell, files, the web, helpers, images, Claude Code and Codex',
      valves: key => ({CONTROLLER_URL: 'http://127.0.0.1:8787', OWNER_KEY: key, MODE: 'cowork',
        OWNER_PROFILE: env.COWORK_PROFILE || 'deep', ALLOW_IMAGES: imagesOn, OWNER_ESCALATION: env.COWORK_ESCALATION !== 'false'})},
    {id: 'qwen_chat', name: 'Qwen (chat)', file: 'openwebui_cowork.py',
      description: 'Chat with Qwen; it uses the web, a shell, files, images, helpers and Claude Code when they help',
      valves: key => ({CONTROLLER_URL: 'http://127.0.0.1:8787', OWNER_KEY: key, MODE: 'chat',
        OWNER_PROFILE: env.CHAT_PROFILE || 'fast', ALLOW_IMAGES: imagesOn, OWNER_ESCALATION: env.COWORK_ESCALATION !== 'false'})},
  ];
  for (let attempt = 1; attempt <= attempts && !stopping(); attempt++) {
    try {
      const consoleKey = env.CONSOLE_KEY || (await readFile(join(dataDir, 'agent-workspace', 'console.key'), 'utf8')).trim();
      const signin = await call('/auths/signin', {method: 'POST', headers: {'content-type': 'application/json',
        'Cf-Access-Authenticated-User-Email': env.OWNER_EMAIL}, body: JSON.stringify({email: env.OWNER_EMAIL, password: 'unused'})});
      if (!signin.ok) throw new Error(`sign-in HTTP ${signin.status}`);
      const {token, role} = signin.json();
      if (role !== 'admin') { log.error(`[supervisor] agents not installed: ${env.OWNER_EMAIL} is not the chat site's admin (role ${role})`); return; }
      const headers = {authorization: `Bearer ${token}`, 'content-type': 'application/json'};
      for (const fn of functions) {
        const content = await readFile(join(integrations, fn.file), 'utf8');
        const form = {id: fn.id, name: fn.name, content, meta: {description: fn.description}};
        const found = (await call(`/functions/id/${fn.id}`, {headers})).ok;
        const saved = await call(`/functions/${found ? `id/${fn.id}/update` : 'create'}`, {method: 'POST', headers, body: JSON.stringify(form)});
        if (!saved.ok) throw new Error(`saving ${fn.id}: HTTP ${saved.status} ${saved.text.slice(0, 200)}`);
        const settings = fn.valves(consoleKey);
        const v = await call(`/functions/id/${fn.id}/valves/update`, {method: 'POST', headers, body: JSON.stringify(settings)});
        if (!v.ok) throw new Error(`${fn.id} settings: HTTP ${v.status} ${v.text.slice(0, 200)}`);
        const current = (await call(`/functions/id/${fn.id}`, {headers})).json();
        if (!current.is_active) await call(`/functions/id/${fn.id}/toggle`, {method: 'POST', headers});
      }
      // The older "Hub · …" team models are retired: Qwen Cowork does all of it.
      for (const retired of ['model_hub']) {
        if ((await call(`/functions/id/${retired}`, {headers})).ok) {
          const r = await call(`/functions/id/${retired}/delete`, {method: 'DELETE', headers});
          log.log(`[supervisor] removed the retired ${retired} function (${r.ok ? 'ok' : `HTTP ${r.status}`})`);
        }
      }
      // The model list shows Qwen Cowork and Qwen (chat), both with tools. The bare models (qwen, qwen-1, qwen-2) stay
      // usable (chat titles use qwen-2) but are hidden from the picker. The Pipes refuse anyone but the owner.
      const everyone = [{principal_type: 'user', principal_id: '*', permission: 'read'}];
      for (const [id, name, hidden] of [['cowork', 'Qwen Cowork', false], ['qwen_chat', 'Qwen (chat)', false],
                                        ['qwen', 'qwen (no tools)', true], ['qwen-1', 'qwen-1', true], ['qwen-2', 'qwen-2', true]]) {
        const r = await call('/models/model/access/update', {method: 'POST', headers, body: JSON.stringify({id, name, access_grants: everyone})});
        if (!r.ok) { log.error(`[supervisor] could not share model ${id}: HTTP ${r.status} ${r.text.slice(0, 200)}`); continue; }
        const model = r.json() || {};
        if (Boolean(model.meta?.hidden) === hidden) continue;
        const u = await call('/models/model/update', {method: 'POST', headers, body: JSON.stringify({
          id, name, meta: {...(model.meta || {}), hidden}, params: model.params || {}})});
        if (!u.ok) log.error(`[supervisor] could not ${hidden ? 'hide' : 'show'} model ${id}: HTTP ${u.status} ${u.text.slice(0, 200)}`);
      }
      log.log('[supervisor] Qwen Cowork is installed in the chat site');
      return;
    } catch (error) {
      if (attempt % 6 === 1) log.log(`[supervisor] waiting to install the agents in the chat site (${error.message})`);
      await new Promise(r => setTimeout(r, 10000));
    }
  }
}
