// A small fair queue per model: at most `capacity` requests run at once, the rest wait
// in arrival order instead of being rejected.
export class QueueFullError extends Error {}
export class QueueTimeoutError extends Error {}

export class Pool {
  constructor(name, capacity, {maxQueue = 64, maxWaitMs = 600000} = {}) {
    this.name = name; this.capacity = capacity; this.maxQueue = maxQueue; this.maxWaitMs = maxWaitMs;
    this.active = 0; this.waiting = [];
  }

  get load() { return (this.active + this.waiting.length) / this.capacity; }

  acquire(signal) {
    if (signal?.aborted) return Promise.reject(signal.reason ?? new Error('Aborted'));
    if (this.active < this.capacity && this.waiting.length === 0) {
      this.active++;
      return Promise.resolve(this.#releaser());
    }
    if (this.waiting.length >= this.maxQueue) return Promise.reject(new QueueFullError(`${this.name} queue is full`));
    return new Promise((resolve, reject) => {
      const entry = {resolve, reject};
      const cleanup = () => { clearTimeout(entry.timer); signal?.removeEventListener('abort', entry.onAbort); };
      entry.grant = () => { cleanup(); this.active++; resolve(this.#releaser()); };
      entry.onAbort = () => { this.#remove(entry); cleanup(); reject(signal.reason ?? new Error('Aborted')); };
      entry.timer = setTimeout(() => { this.#remove(entry); cleanup(); reject(new QueueTimeoutError(`Waited too long for ${this.name}`)); }, this.maxWaitMs);
      entry.timer.unref?.();
      signal?.addEventListener('abort', entry.onAbort, {once: true});
      this.waiting.push(entry);
    });
  }

  #remove(entry) {
    const i = this.waiting.indexOf(entry);
    if (i >= 0) this.waiting.splice(i, 1);
  }

  #releaser() {
    let done = false;
    return () => {
      if (done) return;
      done = true;
      this.active--;
      const next = this.waiting.shift();
      if (next) next.grant();
    };
  }

  status() { return {model: this.name, running: this.active, waiting: this.waiting.length, capacity: this.capacity}; }
}

// Picks the least-loaded pool; ties go to the first listed.
export function leastLoaded(pools) {
  return pools.reduce((best, p) => (p.load < best.load ? p : best));
}
