// Reads all gateway settings from environment variables. Nothing here is secret-free:
// keep the real .env on the GPU server only.
const list = value => (value || '').split(',').map(s => s.trim()).filter(Boolean);
const int = (value, fallback, min, max) => {
  const n = value === undefined || value === '' ? fallback : Number(value);
  if (!Number.isInteger(n) || n < min || n > max) throw new Error(`Invalid number: ${value}`);
  return n;
};

export function readConfig(env = process.env) {
  const parallel = int(env.MODEL_PARALLEL, 3, 1, 16);
  const residents = [1, 2].map(n => ({
    id: `qwen-${n}`,
    kind: 'chat',
    resident: true,
    url: (env[`MODEL${n}_URL`] || `http://127.0.0.1:${8000 + n}/v1`).replace(/\/$/, ''),
    upstreamModel: `qwen-${n}`,
    key: env.MODEL_API_KEY || '',
    capacity: parallel,
  }));

  const models = [...residents];
  if (env.MODEL3_URL && env.MODEL3_ID) {
    const kind = env.MODEL3_KIND === 'image' ? 'image' : 'chat';
    models.push({
      id: 'flex', kind, resident: false, url: env.MODEL3_URL.replace(/\/$/, ''),
      upstreamModel: env.MODEL3_ID, key: env.MODEL3_API_KEY || '', capacity: int(env.MODEL3_PARALLEL, 2, 1, 16),
    });
  }
  const frontier = (prefix, url, key, names) => {
    if (!key) return;
    for (const name of list(names)) models.push({
      id: `${prefix}/${name}`, kind: 'chat', frontier: true, url, upstreamModel: name, key,
      capacity: int(env.FRONTIER_PARALLEL, 4, 1, 32),
    });
  };
  frontier('openai', 'https://api.openai.com/v1', env.OPENAI_API_KEY, env.OPENAI_MODELS);
  // Anthropic's OpenAI-compatible Chat Completions endpoint.
  frontier('anthropic', 'https://api.anthropic.com/v1', env.ANTHROPIC_API_KEY, env.ANTHROPIC_MODELS);
  if (env.OTHER_BASE_URL) frontier('other', env.OTHER_BASE_URL.replace(/\/$/, ''), env.OTHER_API_KEY, env.OTHER_MODELS);

  const adminKey = env.ADMIN_KEY || '';
  if (adminKey && adminKey.length < 32) throw new Error('ADMIN_KEY must be at least 32 characters.');
  return {
    models,
    adminKey,
    dataDir: env.DATA_DIR || './data',
    port: int(env.GATEWAY_PORT, 8080, 1, 65535),
    host: env.GATEWAY_HOST || '127.0.0.1',
    maxQueue: int(env.QUEUE_MAX, 64, 1, 10000),
    maxWaitMs: int(env.QUEUE_MAX_WAIT_MS, 600000, 1000, 86400000),
    timeoutMs: int(env.MODEL_TIMEOUT_MS, 1800000, 10000, 86400000),
    keepaliveAfterMs: int(env.KEEPALIVE_AFTER_MS, 25000, 1000, 90000),
    maxOutputTokens: int(env.MAX_OUTPUT_TOKENS, 32768, 256, 262144),
    frontierMonthlyCalls: int(env.FRONTIER_MONTHLY_CALLS, 300, 0, 10000000),
    bodyLimit: int(env.BODY_LIMIT_BYTES, 33554432, 65536, 209715200),
  };
}
