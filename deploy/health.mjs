// Container health: both models, the gateway and the website must answer.
const auth = {authorization: `Bearer ${process.env.MODEL_API_KEY}`};
const checks = [
  ['qwen-1', 'http://127.0.0.1:8001/health', auth],
  ['qwen-2', 'http://127.0.0.1:8002/health', auth],
  ['gateway', 'http://127.0.0.1:8080/health', {}],
  ['open-webui', 'http://127.0.0.1:3000/health', {}],
];
const results = await Promise.all(checks.map(([name, url, headers]) =>
  fetch(url, {headers, signal: AbortSignal.timeout(5000)}).then(r => [name, r.ok], () => [name, false])));
const down = results.filter(([, ok]) => !ok).map(([name]) => name);
if (down.length) console.error(`Unhealthy: ${down.join(', ')}`);
process.exit(down.length ? 1 : 0);
