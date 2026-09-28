// Manage gateway API keys (one per friend, agent or program).
//
// From your laptop (recommended):
//   set HUB_URL=https://api.YOUR-DOMAIN   and   set ADMIN_KEY=<your admin key>
//   node gateway/keys.js add laptop-agents --frontier
//   node gateway/keys.js list
//   node gateway/keys.js remove laptop-agents
// On the rental itself, leave HUB_URL unset and it edits DATA_DIR/clients.json directly.
// A new key is printed once. Revoking takes effect immediately.
import {ClientStore} from './clients.js';

const [command, name, ...flags] = process.argv.slice(2);
const frontier = flags.includes('--frontier');
const {HUB_URL, ADMIN_KEY} = process.env;

async function remote(method, path, body) {
  const res = await fetch(HUB_URL.replace(/\/$/, '') + path, {
    method, headers: {authorization: `Bearer ${ADMIN_KEY}`, 'content-type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error?.message || `HTTP ${res.status}`);
  return data;
}

const api = HUB_URL ? {
  add: async () => (await remote('POST', '/admin/keys', {name, frontier})).key,
  list: async () => (await remote('GET', '/admin/keys')).keys,
  remove: async () => remote('DELETE', `/admin/keys/${encodeURIComponent(name)}`).then(() => true, () => false),
} : (() => {
  const store = new ClientStore(process.env.DATA_DIR || './data');
  return {add: async () => store.add(name, {frontier}), list: async () => store.list(), remove: async () => store.remove(name)};
})();

try {
  if (command === 'add' && name) {
    const key = await api.add();
    console.log(`Key for "${name}"${frontier ? ' (may use paid frontier models)' : ''}:\n\n${key}\n\nShown only once. Store it in your password manager.`);
  } else if (command === 'list') {
    for (const c of await api.list()) console.log(`${c.name}\tfrontier=${c.frontier}\tcreated=${c.createdAt}`);
  } else if (command === 'remove' && name) {
    console.log((await api.remove()) ? `Removed ${name}.` : `No key named ${name}.`);
  } else {
    console.log('Usage: node gateway/keys.js add <name> [--frontier] | list | remove <name>');
    process.exitCode = 1;
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
