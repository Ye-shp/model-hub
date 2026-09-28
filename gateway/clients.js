// API keys for people and programs. Only SHA-256 hashes are stored; the key itself is shown
// once when created (see keys.js). The file is re-read automatically when it changes.
import {createHash, randomBytes} from 'node:crypto';
import {readFileSync, statSync, writeFileSync, renameSync, mkdirSync} from 'node:fs';
import {join} from 'node:path';

export const hashKey = key => createHash('sha256').update(key).digest('hex');
export const newKey = () => `mh_${randomBytes(32).toString('base64url')}`;
const NAME = /^[a-z0-9][a-z0-9._-]{0,47}$/;

export class ClientStore {
  constructor(dataDir) { this.path = join(dataDir, 'clients.json'); this.mtime = -1; this.byHash = new Map(); this.dataDir = dataDir; }

  #load() {
    let mtime;
    try { mtime = statSync(this.path).mtimeMs; } catch { this.byHash = new Map(); this.mtime = -1; return; }
    if (mtime === this.mtime) return;
    const {clients = []} = JSON.parse(readFileSync(this.path, 'utf8'));
    this.byHash = new Map(clients.map(c => [c.hash, c]));
    this.mtime = mtime;
  }

  list() { this.#load(); return [...this.byHash.values()]; }

  authenticate(header) {
    if (typeof header !== 'string' || !header.startsWith('Bearer ')) return null;
    const key = header.slice(7).trim();
    if (key.length < 20 || key.length > 200) return null;
    this.#load();
    return this.byHash.get(hashKey(key)) || null;
  }

  add(name, {frontier = false} = {}) {
    if (!NAME.test(name)) throw new Error('Name: lowercase letters, digits, dot, dash or underscore (max 48).');
    const clients = this.list().filter(c => c.name !== name);
    const key = newKey();
    clients.push({name, hash: hashKey(key), frontier: Boolean(frontier), createdAt: new Date().toISOString()});
    this.#write(clients);
    return key;
  }

  remove(name) {
    const clients = this.list();
    const kept = clients.filter(c => c.name !== name);
    this.#write(kept);
    return kept.length !== clients.length;
  }

  #write(clients) {
    mkdirSync(this.dataDir, {recursive: true});
    writeFileSync(`${this.path}.tmp`, JSON.stringify({clients}, null, 2), {mode: 0o600});
    renameSync(`${this.path}.tmp`, this.path);
    this.mtime = -1;
  }
}
