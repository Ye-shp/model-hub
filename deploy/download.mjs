// Downloads each pinned file once, resumes interrupted downloads, verifies size and SHA-256,
// and records the verification so restarts don't re-hash 16 GB every boot.
import {mkdir, rename, stat, writeFile, readFile} from 'node:fs/promises';
import {createWriteStream, createReadStream} from 'node:fs';
import {Readable} from 'node:stream';
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

async function fetchFile(f, path) {
  const partial = `${path}.partial`;
  for (let attempt = 1; attempt <= 5; attempt++) {
    const have = Math.max(0, await size(partial));
    if (have === f.size) break;
    const headers = have ? {range: `bytes=${have}-`} : {};
    if (process.env.HF_TOKEN) headers.authorization = `Bearer ${process.env.HF_TOKEN}`;
    try {
      const res = await fetch(`https://huggingface.co/${f.repo}/resolve/${f.revision}/${f.file}`, {headers});
      if (!(res.ok || res.status === 206) || !res.body) throw new Error(`HTTP ${res.status}`);
      const append = res.status === 206 && have > 0;
      console.log(`${f.file}: ${append ? `resuming at ${(have / 1e9).toFixed(2)} GB` : 'downloading'} (attempt ${attempt})`);
      await pipeline(Readable.fromWeb(res.body), createWriteStream(partial, {flags: append ? 'a' : 'w'}));
    } catch (error) {
      console.error(`${f.file}: download interrupted (${error.message}); retrying in 15 s`);
      await new Promise(r => setTimeout(r, 15000));
    }
  }
  if (await size(partial) !== f.size) throw new Error(`${f.file}: size mismatch after download.`);
  console.log(`${f.file}: verifying checksum`);
  if (await sha256(partial) !== f.sha256) { await rename(partial, `${partial}.bad`); throw new Error(`${f.file}: checksum mismatch; moved aside.`); }
  await rename(partial, path);
}

export async function prepareModels(manifest, directory) {
  await mkdir(directory, {recursive: true});
  const paths = {};
  for (const [name, f] of Object.entries(manifest.files)) {
    if (!valid(f)) throw new Error(`Manifest entry ${name} is invalid. Pin repo, 40-char revision, .gguf file, byte size and SHA-256.`);
    const path = join(directory, `${f.sha256}.gguf`);
    const marker = `${path}.verified`;
    const current = await stat(path).catch(() => null);
    const recorded = await readFile(marker, 'utf8').catch(() => '');
    if (!current || current.size !== f.size || recorded !== `${f.sha256} ${current.mtimeMs}`) {
      if (!current || current.size !== f.size) await fetchFile(f, path);
      else if (await sha256(path) !== f.sha256) { await rename(path, `${path}.bad`); await fetchFile(f, path); }
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
