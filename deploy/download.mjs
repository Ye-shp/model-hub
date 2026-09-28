// Downloads each pinned file once, resumes interrupted downloads, verifies size and SHA-256,
// and records the verification so restarts don't re-hash 16 GB every boot.
import {mkdir, rename, stat, writeFile, readFile} from 'node:fs/promises';
import {createWriteStream, createReadStream} from 'node:fs';
import {Readable, Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {createHash} from 'node:crypto';
import {join} from 'node:path';

const valid = f => /^[a-f0-9]{64}$/.test(f.sha256) && /^[a-f0-9]{40}$/.test(f.revision)
  && /^[\w.-]+\/[\w.-]+$/.test(f.repo) && /^[\w.-]+\.gguf$/.test(f.file) && Number.isInteger(f.size);

async function sha256(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest('hex');
}
const size = path => stat(path).then(s => s.size, () => -1);

const HF = (process.env.HF_ENDPOINT || 'https://huggingface.co').replace(/\/$/, '');
const STALL_MS = Number(process.env.DOWNLOAD_STALL_MS || 60000);

async function fetchFile(f, path) {
  const partial = `${path}.partial`;
  for (let attempt = 1; attempt <= 60; attempt++) {
    const have = Math.max(0, await size(partial));
    if (have === f.size) break;
    const headers = have ? {range: `bytes=${have}-`} : {};
    if (process.env.HF_TOKEN) headers.authorization = `Bearer ${process.env.HF_TOKEN}`;
    // Abort if no data arrives for STALL_MS, then resume from where it stopped.
    const controller = new AbortController();
    let received = 0, lastData = Date.now(), lastLog = Date.now(), lastLogged = 0;
    const watchdog = setInterval(() => {
      if (Date.now() - lastData > STALL_MS) controller.abort(new Error(`no data for ${STALL_MS / 1000} s`));
      if (Date.now() - lastLog >= 30000) {
        const done = have + received, rate = (received - lastLogged) / ((Date.now() - lastLog) / 1000);
        console.log(`${f.file}: ${(done / 1e9).toFixed(2)} / ${(f.size / 1e9).toFixed(2)} GB (${Math.round(done / f.size * 100)}%, ${(rate / 1e6).toFixed(0)} MB/s)`);
        lastLog = Date.now(); lastLogged = received;
      }
    }, 5000);
    try {
      const res = await fetch(`${HF}/${f.repo}/resolve/${f.revision}/${f.file}`, {headers, signal: controller.signal});
      if (!(res.ok || res.status === 206) || !res.body) throw new Error(`HTTP ${res.status}`);
      const append = res.status === 206 && have > 0;
      if (!append) received = -have; // server ignored the range: starting over
      console.log(`${f.file}: ${append ? `resuming at ${(have / 1e9).toFixed(2)} GB` : 'downloading'} (attempt ${attempt})`);
      const counter = new Transform({transform(chunk, _enc, cb) { received += chunk.length; lastData = Date.now(); cb(null, chunk); }});
      await pipeline(Readable.fromWeb(res.body), counter, createWriteStream(partial, {flags: append ? 'a' : 'w'}), {signal: controller.signal});
    } catch (error) {
      console.error(`${f.file}: download interrupted (${controller.signal.reason?.message || error.message}); resuming in 10 s`);
      await new Promise(r => setTimeout(r, 10000));
    } finally {
      clearInterval(watchdog);
    }
  }
  if (await size(partial) !== f.size) throw new Error(`${f.file}: size mismatch after download.`);
  console.log(`${f.file}: verifying checksum`);
  if (await sha256(partial) !== f.sha256) { await rename(partial, `${partial}.bad`); throw new Error(`${f.file}: checksum mismatch; moved aside.`); }
  await rename(partial, path);
}

// Parallel ranged download: the file is split into chunks fetched by several connections at
// once. Each chunk has its own stall timeout and retries, and finished chunks are recorded in
// a sidecar file, so a frozen connection or a container restart only costs one chunk.
const CHUNK = Number(process.env.DOWNLOAD_CHUNK_MB || 128) * 1024 * 1024;
const WORKERS = Number(process.env.DOWNLOAD_CONNECTIONS || 8);

async function fetchChunk(url, handle, start, end, onBytes) {
  const controller = new AbortController();
  let lastData = Date.now();
  const watchdog = setInterval(() => { if (Date.now() - lastData > STALL_MS) controller.abort(new Error(`no data for ${STALL_MS / 1000} s`)); }, 2000);
  try {
    const headers = {range: `bytes=${start}-${end}`};
    if (process.env.HF_TOKEN) headers.authorization = `Bearer ${process.env.HF_TOKEN}`;
    const res = await fetch(url, {headers, signal: controller.signal});
    if (res.status !== 206 || !res.body) throw Object.assign(new Error(`HTTP ${res.status} for a range request`), {noRange: res.status === 200});
    let pos = start;
    for await (const chunk of res.body) {
      lastData = Date.now();
      await handle.write(chunk, 0, chunk.length, pos);
      pos += chunk.length;
      onBytes(chunk.length);
    }
    if (pos !== end + 1) throw new Error(`short chunk (${pos - start} of ${end - start + 1} bytes)`);
  } catch (error) {
    throw controller.signal.aborted ? Object.assign(new Error(controller.signal.reason?.message || 'aborted'), {noRange: false}) : error;
  } finally {
    clearInterval(watchdog);
  }
}

async function fetchFileParallel(f, path, onProgress = () => {}) {
  const partial = `${path}.partial`, partsFile = `${path}.parts`;
  const url = `${HF}/${f.repo}/resolve/${f.revision}/${f.file}`;
  const count = Math.ceil(f.size / CHUNK);
  let done = new Set(JSON.parse(await readFile(partsFile, 'utf8').catch(() => '[]')));
  const existing = await size(partial);
  if (!done.size && existing > 0) {
    // A sequential partial from an older downloader: keep every whole chunk it already has.
    for (let i = 0; (i + 1) * CHUNK <= existing; i++) done.add(i);
  }
  const {open} = await import('node:fs/promises');
  const handle = await open(partial, existing >= 0 ? 'r+' : 'w+');
  try {
    await handle.truncate(f.size);
    let bytes = [...done].reduce((sum, i) => sum + Math.min(CHUNK, f.size - i * CHUNK), 0);
    const startBytes = bytes, started = Date.now();
    console.log(`${f.file}: downloading ${((f.size - bytes) / 1e9).toFixed(2)} GB with ${WORKERS} connections${bytes ? ` (${(bytes / 1e9).toFixed(2)} GB already here)` : ''}`);
    const progress = setInterval(() => {
      const rate = (bytes - startBytes) / ((Date.now() - started) / 1000);
      const line = `${f.file}: ${(bytes / 1e9).toFixed(2)} / ${(f.size / 1e9).toFixed(2)} GB (${Math.round(bytes / f.size * 100)}%, ${(rate / 1e6).toFixed(0)} MB/s)`;
      console.log(line);
      onProgress(line);
    }, 15000);
    const queue = [...Array(count).keys()].filter(i => !done.has(i));
    let saving = Promise.resolve();
    const record = i => { done.add(i); saving = saving.then(() => writeFile(partsFile, JSON.stringify([...done]))); return saving; };
    try {
      await Promise.all(Array.from({length: Math.min(WORKERS, queue.length)}, async () => {
        for (let i = queue.shift(); i !== undefined; i = queue.shift()) {
          const start = i * CHUNK, end = Math.min(f.size, start + CHUNK) - 1;
          for (let attempt = 1; ; attempt++) {
            let got = 0;
            try { await fetchChunk(url, handle, start, end, n => { got += n; bytes += n; }); break; }
            catch (error) {
              bytes -= got;
              if (error.noRange) throw error;
              if (attempt >= 12) throw new Error(`chunk ${i} failed ${attempt} times: ${error.message}`);
              console.error(`${f.file}: chunk ${i + 1}/${count} interrupted (${error.message}); retry ${attempt}`);
              await new Promise(r => setTimeout(r, Math.min(30000, 2000 * attempt)));
            }
          }
          await record(i);
        }
      }));
    } finally {
      clearInterval(progress);
      await saving;
    }
  } finally {
    await handle.close();
  }
  console.log(`${f.file}: verifying checksum`);
  onProgress(`${f.file}: verifying checksum`);
  if (await sha256(partial) !== f.sha256) {
    await rename(partial, `${partial}.bad`);
    await writeFile(partsFile, '[]');
    throw new Error(`${f.file}: checksum mismatch; moved aside and will download again on the next start.`);
  }
  await rename(partial, path);
  await writeFile(partsFile, '[]');
}

async function download(f, path, onProgress) {
  try { await fetchFileParallel(f, path, onProgress); }
  catch (error) {
    if (!error.noRange) throw error;
    console.log(`${f.file}: server doesn't support ranged downloads; using a single connection`);
    await fetchFile(f, path);
  }
}

export async function prepareModels(manifest, directory, {onProgress} = {}) {
  await mkdir(directory, {recursive: true});
  const paths = {};
  for (const [name, f] of Object.entries(manifest.files)) {
    if (!valid(f)) throw new Error(`Manifest entry ${name} is invalid. Pin repo, 40-char revision, .gguf file, byte size and SHA-256.`);
    const path = join(directory, `${f.sha256}.gguf`);
    const marker = `${path}.verified`;
    const current = await stat(path).catch(() => null);
    const recorded = await readFile(marker, 'utf8').catch(() => '');
    if (!current || current.size !== f.size || recorded !== `${f.sha256} ${current.mtimeMs}`) {
      if (!current || current.size !== f.size) await download(f, path, onProgress);
      else if (await sha256(path) !== f.sha256) { await rename(path, `${path}.bad`); await download(f, path, onProgress); }
      await writeFile(marker, `${f.sha256} ${(await stat(path)).mtimeMs}`);
    }
    paths[name] = path;
  }
  const slots = {};
  for (const [slot, s] of Object.entries(manifest.slots)) {
    if (!paths[s.model] || (s.mmproj && !paths[s.mmproj])) throw new Error(`Slot ${slot} refers to an unknown file.`);
    slots[slot] = {model: paths[s.model], mmproj: s.mmproj ? paths[s.mmproj] : null};
  }
  return slots;
}
