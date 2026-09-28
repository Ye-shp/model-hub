// Container entrypoint on the GPU rental. Starts and keeps alive:
//   qwen-1 (GPU 0) and qwen-2 (GPU 1): llama-server with vision, large context, parallel slots
//   gateway:    OpenAI-compatible API with per-client keys and queues (port 8080)
//   open-webui: the chat website for you and invited friends (port 3000)
//   cloudflared: the outbound tunnel; nothing listens on a public port
import {spawn, execFileSync} from 'node:child_process';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {join} from 'node:path';
import {prepareModels} from './download.mjs';
import {ClientStore} from '../gateway/clients.js';

const env = process.env;
const DATA = env.DATA_DIR || '/workspace/data';
const MODELS = env.MODEL_DIR || '/workspace/models';
for (const name of ['MODEL_API_KEY', 'TUNNEL_TOKEN', 'ADMIN_KEY']) {
  if (!env[name] || env[name].length < 32) throw new Error(`${name} must be set to a random value of at least 32 characters.`);
}
const gpus = execFileSync('nvidia-smi', ['--query-gpu=index,name,memory.total', '--format=csv,noheader'], {encoding: 'utf8'}).trim().split('\n');
console.log(`GPUs:\n  ${gpus.join('\n  ')}`);
if (gpus.length < 2) throw new Error('Two GPUs are required: one per resident model.');

await mkdir(DATA, {recursive: true});
// Secrets the container creates for itself on first boot and keeps on the volume.
async function persistentSecret(file, make) {
  const path = join(DATA, file);
  try { return (await readFile(path, 'utf8')).trim(); }
  catch { const value = make(); await writeFile(path, value, {mode: 0o600}); return value; }
}
const webuiSecret = env.WEBUI_SECRET_KEY || await persistentSecret('webui-secret', () => randomBytes(32).toString('hex'));
// Open WebUI reaches the gateway with its own key, re-created if you ever revoke it.
const clients = new ClientStore(DATA);
const webuiFrontier = env.WEBUI_FRONTIER === 'true';
let webuiKey = await readFile(join(DATA, 'open-webui.key'), 'utf8').then(s => s.trim(), () => '');
const existing = clients.list().find(c => c.name === 'open-webui');
if (!webuiKey || !clients.authenticate(`Bearer ${webuiKey}`) || existing?.frontier !== webuiFrontier) {
  webuiKey = clients.add('open-webui', {frontier: webuiFrontier});
  await writeFile(join(DATA, 'open-webui.key'), webuiKey, {mode: 0o600});
}

const manifest = JSON.parse(await readFile(new URL('./models.json', import.meta.url), 'utf8'));
// Status shown at https://api.<domain>/health so progress is visible from outside.
const STATUS = join(DATA, 'status.json');
const setStatus = (phase, detail = '') => writeFile(STATUS, JSON.stringify({phase, detail, at: new Date().toISOString()})).catch(() => {});

// Each process gets only the settings it needs, so for example the chat website never sees
// your frontier API keys or the tunnel token.
const BASE = Object.fromEntries(Object.entries(env).filter(([k]) =>
  ['PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'LD_LIBRARY_PATH'].includes(k) || k.startsWith('NVIDIA_') || k.startsWith('CUDA_')));
const GATEWAY_ENV = Object.fromEntries(Object.entries(env).filter(([k]) => !['TUNNEL_TOKEN', 'WEBUI_SECRET_KEY', 'HF_TOKEN', 'CONSOLE_KEY'].includes(k)));

const children = new Set();
let stopping = false;
function keepRunning(name, command, args, childEnv, cwd = '/opt/hub') {
  let failures = 0, startedAt = 0;
  const start = () => {
    if (stopping) return;
    startedAt = Date.now();
    const child = spawn(command, args, {cwd, stdio: ['ignore', 'inherit', 'inherit'], env: childEnv});
    children.add(child);
    console.log(`[supervisor] started ${name}`);
    child.on('error', error => console.error(`[supervisor] ${name} could not start: ${error.message}`));
    child.on('exit', code => {
      children.delete(child);
      if (stopping) return;
      failures = Date.now() - startedAt > 300000 ? 1 : failures + 1;
      const delay = Math.min(300, 5 * 2 ** (failures - 1));
      console.error(`[supervisor] ${name} exited (code ${code}); restart ${failures} in ${delay}s${failures >= 3 ? ' — repeated failures: check the log above (out of GPU memory? try MODEL_CONTEXT=65536, or check nvidia-smi for another process on that GPU)' : ''}`);
      setTimeout(start, delay * 1000);
    });
  };
  start();
}

function startModels(slots) {
  for (const slot of ['1', '2']) {
    const n = Number(slot);
    const args = [
      '--model', slots[slot].model, '--alias', `qwen-${n}`, '--host', '127.0.0.1', '--port', String(8000 + n),
      // All layers stay on the GPU; llama.cpp sizes the context to the memory left (never below
      // 32K) unless MODEL_CONTEXT is set. Measured: 134K per slot on a 3090, 262K on a V100 32GB.
      '--n-gpu-layers', 'all', '--fit', 'on', '--fit-target', env.FIT_MARGIN_MIB || '1536', '--fit-ctx', '32768',
      '--parallel', env.MODEL_PARALLEL || '3', '--kv-unified',
      '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '--flash-attn', 'on',
      '--jinja', '--reasoning-format', 'deepseek',
      '--reasoning-effort', env[`MODEL${n}_REASONING_EFFORT`] || (n === 1 ? 'medium' : 'low'),
      '--reasoning-budget', env[`MODEL${n}_REASONING_BUDGET`] || '-1',
      '--metrics',
    ];
    if (slots[slot].mmproj) args.push('--mmproj', slots[slot].mmproj);
    if (env.MODEL_CONTEXT) args.push('--ctx-size', env.MODEL_CONTEXT);
    keepRunning(`qwen-${n}`, '/app/llama-server', args, {...BASE, CUDA_VISIBLE_DEVICES: String(n - 1), LLAMA_API_KEY: env.MODEL_API_KEY}, '/app');
  }
}

keepRunning('gateway', '/usr/local/bin/node', ['/opt/hub/gateway/index.js'], {...GATEWAY_ENV, DATA_DIR: DATA});

// Optional always-on CPU controller. Uses the resident models through the gateway.
if (env.ENABLE_AGENT_CONSOLE === 'true') {
  const file = join(DATA, 'agent-controller.key');
  let key = await readFile(file, 'utf8').then(s => s.trim(), () => '');
  const frontier = Boolean(env.AGENT_FRONTIER_MODEL);
  const current = clients.list().find(c => c.name === 'agent-controller');
  if (!key || !clients.authenticate(`Bearer ${key}`) || current?.frontier !== frontier) {
    key = clients.add('agent-controller', {frontier});
    await writeFile(file, key, {mode: 0o600});
  }
  keepRunning('agent-console', '/opt/agents/bin/python', ['/opt/hub/agents/console.py'], {
    ...BASE, PYTHONUNBUFFERED: '1', HUB_URL: 'http://127.0.0.1:8080/v1', HUB_KEY: key,
    HUB_DATA_DIR: join(DATA, 'agent-workspace'), FRONTIER_MODEL: env.AGENT_FRONTIER_MODEL || '',
    CONSOLE_URL: env.CONSOLE_URL || '', CONSOLE_KEY: env.CONSOLE_KEY || '',
  });
}

keepRunning('open-webui', '/opt/openwebui/bin/open-webui', ['serve', '--host', '127.0.0.1', '--port', '3000'], {
  ...BASE,
  DATA_DIR: join(DATA, 'open-webui'),
  HF_HOME: join(DATA, 'cache', 'huggingface'),
  WEBUI_SECRET_KEY: webuiSecret,
  WEBUI_URL: env.WEBUI_URL || '',
  CORS_ALLOW_ORIGIN: env.WEBUI_URL || '*',
  // Cloudflare Access signs people in; Open WebUI trusts the email it passes along.
  WEBUI_AUTH_TRUSTED_EMAIL_HEADER: 'Cf-Access-Authenticated-User-Email',
  DEFAULT_USER_ROLE: env.WEBUI_DEFAULT_ROLE || 'pending',
  ENABLE_OLLAMA_API: 'false',
  ENABLE_EVALUATION_ARENA_MODELS: 'false',
  OPENAI_API_BASE_URLS: 'http://127.0.0.1:8080/v1',
  OPENAI_API_KEYS: webuiKey,
  ANONYMIZED_TELEMETRY: 'false', DO_NOT_TRACK: 'true', SCARF_NO_ANALYTICS: 'true',
});

keepRunning('cloudflared', '/usr/bin/cloudflared', ['tunnel', '--no-autoupdate', 'run'], {...BASE, TUNNEL_TOKEN: env.TUNNEL_TOKEN});

// The website, API and tunnel are up now; the models join once their files are ready.
await setStatus('downloading models');
try {
  const slots = await prepareModels(manifest, MODELS, {onProgress: text => setStatus('downloading models', text)});
  await setStatus('models starting', 'loading weights onto both GPUs (about a minute)');
  startModels(slots);
} catch (error) {
  console.error(`[supervisor] model download failed: ${error.message}`);
  await setStatus('download failed', `${error.message} — restart the instance to resume`);
}

for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
  stopping = true;
  for (const child of children) child.kill('SIGTERM');
  setTimeout(() => { for (const child of children) child.kill('SIGKILL'); process.exit(0); }, 10000).unref();
});
