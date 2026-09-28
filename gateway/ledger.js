// Monthly ceiling on paid frontier calls, persisted on the GPU volume. It counts requests,
// not dollars: also set a spending limit in each provider's billing console.
import {mkdir, readFile, writeFile, rename} from 'node:fs/promises';
import {join} from 'node:path';

export class UsageLedger {
  constructor(directory, limit) { this.directory = directory; this.limit = limit; this.counts = {}; this.queue = Promise.resolve(); }

  async init() {
    await mkdir(this.directory, {recursive: true});
    try { this.counts = JSON.parse(await readFile(join(this.directory, 'frontier-usage.json'), 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }

  reserve() {
    const pending = this.queue.then(async () => {
      const month = new Date().toISOString().slice(0, 7);
      const used = this.counts[month] || 0;
      if (used >= this.limit) throw new Error('Monthly frontier call allowance reached.');
      this.counts[month] = used + 1;
      try {
        await writeFile(join(this.directory, 'frontier-usage.tmp'), JSON.stringify(this.counts), {mode: 0o600});
        await rename(join(this.directory, 'frontier-usage.tmp'), join(this.directory, 'frontier-usage.json'));
      } catch (error) { this.counts[month] = used; throw error; }
    });
    this.queue = pending.catch(() => {});
    return pending;
  }

  used() { return this.counts[new Date().toISOString().slice(0, 7)] || 0; }
}
