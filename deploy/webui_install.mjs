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
  const functions = [
    {id: 'cowork', name: 'Qwen Cowork', file: 'openwebui_cowork.py',
      description: 'Say what you want done; Qwen plans and does it with a shell, files, the web, helpers, images, Claude Code and Codex',
      valves: key => ({CONTROLLER_URL: 'http://127.0.0.1:8787', OWNER_KEY: key, ALLOWED_EMAILS: env.PIPE_ALLOWED_EMAILS || '',
        OWNER_PROFILE: env.COWORK_PROFILE || 'balanced', GUEST_PROFILE: env.COWORK_GUEST_PROFILE || 'balanced',
        ALLOW_IMAGES: imagesOn, OWNER_ESCALATION: env.COWORK_ESCALATION !== 'false'})},
    {id: 'model_hub', name: 'Hub', file: 'openwebui_pipe.py',
      description: 'Model Hub agent team: durable jobs with skills, subagents and project memory',
      valves: key => ({CONTROLLER_URL: 'http://127.0.0.1:8787', OWNER_KEY: key, PROJECT_ID: env.PIPE_PROJECT || 'friends',
        PROFILE: env.PIPE_PROFILE || 'balanced', ALLOWED_EMAILS: env.PIPE_ALLOWED_EMAILS || '',
        ALLOW_IMAGES: imagesOn, ALLOW_FRONTIER: env.PIPE_ALLOW_FRONTIER === 'true'})},
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
        // People added on the site (valves) are kept; the instance setting only adds to them.
        const settings = fn.valves(consoleKey);
        const stored = await call(`/functions/id/${fn.id}/valves`, {headers});
        const before = stored.ok ? (stored.json() || {}) : {};
        const emails = new Set([before.ALLOWED_EMAILS, settings.ALLOWED_EMAILS].join(',').split(',').map(e => e.trim().toLowerCase()).filter(Boolean));
        settings.ALLOWED_EMAILS = [...emails].join(',');
        const v = await call(`/functions/id/${fn.id}/valves/update`, {method: 'POST', headers, body: JSON.stringify(settings)});
        if (!v.ok) throw new Error(`${fn.id} settings: HTTP ${v.status} ${v.text.slice(0, 200)}`);
        const current = (await call(`/functions/id/${fn.id}`, {headers})).json();
        if (!current.is_active) await call(`/functions/id/${fn.id}/toggle`, {method: 'POST', headers});
      }
      // Every signed-in user may pick these models (the Pipes themselves check who is invited).
      const everyone = [{principal_type: 'user', principal_id: '*', permission: 'read'}];
      for (const [id, name] of [['cowork', 'Qwen Cowork'], ['qwen-1', 'qwen-1'], ['qwen-2', 'qwen-2']]) {
        const r = await call('/models/model/access/update', {method: 'POST', headers, body: JSON.stringify({id, name, access_grants: everyone})});
        if (!r.ok) log.error(`[supervisor] could not share model ${id}: HTTP ${r.status} ${r.text.slice(0, 200)}`);
      }
      log.log('[supervisor] Qwen Cowork and the agent teams are installed in the chat site');
      return;
    } catch (error) {
      if (attempt % 6 === 1) log.log(`[supervisor] waiting to install the agents in the chat site (${error.message})`);
      await new Promise(r => setTimeout(r, 10000));
    }
  }
}
