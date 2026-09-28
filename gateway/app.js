// OpenAI-compatible gateway in front of the two resident Qwen servers, the optional third
// slot and any paid frontier providers. Anything that speaks the OpenAI API (Open WebUI,
// the agent code in agents/, most agent frameworks) can use it with a per-client key.
import Fastify from 'fastify';
import {once} from 'node:events';
import {timingSafeEqual} from 'node:crypto';
import {hashKey} from './clients.js';
import {Pool, leastLoaded, QueueFullError, QueueTimeoutError} from './queue.js';
import {cleanChatRequest, RequestError} from './sanitize.js';

const IMAGE_SIZES = new Set(['256x256', '512x512', '768x768', '1024x1024', '1024x1536', '1536x1024']);

export function createApp({config, clients, ledger, fetcher = fetch, log = () => {}}) {
  const app = Fastify({logger: false, bodyLimit: config.bodyLimit});
  // Accept an empty body labelled as JSON (common on DELETE); Fastify's default rejects it.
  app.removeContentTypeParser('application/json');
  app.addContentTypeParser('application/json', {parseAs: 'string'}, (_req, text, done) => {
    if (text === '') return done(null, undefined);
    try { done(null, JSON.parse(text)); }
    catch { const error = new Error('Malformed JSON.'); error.statusCode = 400; done(error); }
  });
  const pools = new Map(config.models.map(m => [m.id, new Pool(m.id, m.capacity, config)]));
  const residents = config.models.filter(m => m.resident);

  const errorBody = (message, type) => ({error: {message, type}});
  const fail = (reply, status, message, type = 'invalid_request_error') => reply.code(status).send(errorBody(message, type));

  const adminHash = config.adminKey ? hashKey(config.adminKey) : null;
  app.addHook('onRequest', async (req, reply) => {
    reply.header('cache-control', 'no-store');
    if (req.url === '/health') return;
    if (req.url.startsWith('/admin/')) {
      const header = req.headers.authorization;
      const given = typeof header === 'string' && header.startsWith('Bearer ') ? hashKey(header.slice(7).trim()) : '';
      if (!adminHash || given.length !== adminHash.length || !timingSafeEqual(Buffer.from(given), Buffer.from(adminHash)))
        return fail(reply, 401, 'Admin key required.', 'authentication_error');
      return;
    }
    const client = clients.authenticate(req.headers.authorization);
    if (!client) return fail(reply, 401, 'Missing or invalid API key.', 'authentication_error');
    req.client = client;
  });

  const available = client => config.models.filter(m => !m.frontier || client.frontier);
  function resolve(client, id) {
    if (typeof id !== 'string') return null;
    if (id === 'qwen' && residents.length) {
      const pool = leastLoaded(residents.map(m => pools.get(m.id)));
      return residents.find(m => m.id === pool.name);
    }
    return available(client).find(m => m.id === id) || null;
  }

  app.get('/health', async () => ({ok: true}));

  app.get('/v1/models', async req => ({
    object: 'list',
    data: [
      ...(residents.length ? [{id: 'qwen', object: 'model', owned_by: 'model-hub', description: 'Whichever resident Qwen is least busy'}] : []),
      ...available(req.client).map(m => ({id: m.id, object: 'model', owned_by: m.frontier ? m.id.split('/')[0] : 'model-hub', kind: m.kind})),
    ],
  }));

  app.get('/v1/status', async req => ({
    client: req.client.name,
    models: available(req.client).map(m => pools.get(m.id).status()),
    frontier: {callsThisMonth: ledger.used(), monthlyLimit: config.frontierMonthlyCalls},
  }));

  // Key management for the owner (ADMIN_KEY). Keys are returned once, on creation.
  app.get('/admin/keys', async () => ({keys: clients.list().map(({name, frontier, createdAt}) => ({name, frontier, createdAt}))}));
  app.post('/admin/keys', async (req, reply) => {
    const {name, frontier = false} = req.body || {};
    if (name === 'open-webui') return fail(reply, 400, 'That name is reserved for the website’s own key.');
    try { return {name, frontier: Boolean(frontier), key: clients.add(String(name ?? ''), {frontier: frontier === true})}; }
    catch (error) { return fail(reply, 400, error.message); }
  });
  app.delete('/admin/keys/:name', async (req, reply) => (clients.remove(req.params.name) ? {removed: req.params.name} : fail(reply, 404, 'No key with that name.', 'not_found_error')));

  app.post('/v1/chat/completions', async (req, reply) => {
    const model = resolve(req.client, req.body?.model);
    if (!model) return fail(reply, 404, 'Unknown model, or this key may not use it. See GET /v1/models.', 'not_found_error');
    if (model.kind !== 'chat') return fail(reply, 400, 'That model generates images; use /v1/images/generations.');
    let body;
    try { body = cleanChatRequest(req.body, model, config); }
    catch (error) { if (error instanceof RequestError) return fail(reply, 400, error.message); throw error; }
    return forward(req, reply, model, '/chat/completions', body, body.stream === true);
  });

  app.post('/v1/images/generations', async (req, reply) => {
    const model = resolve(req.client, req.body?.model);
    if (!model || model.kind !== 'image') return fail(reply, 404, 'No image model is connected to the third slot.', 'not_found_error');
    const {prompt, size = '1024x1024'} = req.body;
    if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 8000) return fail(reply, 400, 'prompt must be 1-8000 characters.');
    if (!IMAGE_SIZES.has(size)) return fail(reply, 400, `size must be one of ${[...IMAGE_SIZES].join(', ')}.`);
    return forward(req, reply, model, '/images/generations', {model: model.upstreamModel, prompt, size, n: 1, response_format: 'b64_json'}, false);
  });

  async function forward(req, reply, model, path, body, stream) {
    const started = Date.now();
    const res$ = reply.raw;
    const clientGone = new AbortController();
    res$.on('close', () => { if (!res$.writableFinished) clientGone.abort(new Error('Client disconnected')); });

    let release;
    try { release = await pools.get(model.id).acquire(clientGone.signal); }
    catch (error) {
      if (error instanceof QueueFullError || error instanceof QueueTimeoutError) return fail(reply, 503, `${model.id} is overloaded right now. Retry shortly.`, 'overloaded_error');
      return reply.hijack();
    }
    if (model.frontier) {
      try { await ledger.reserve(); }
      catch { release(); return fail(reply, 429, 'The monthly frontier call allowance is used up.', 'rate_limit_error'); }
    }

    reply.hijack();
    let committed = false, status = 0, keepaliveTimer, keepaliveInterval;
    const finish = () => { clearTimeout(keepaliveTimer); clearInterval(keepaliveInterval); release(); log({client: req.client.name, model: model.id, status, ms: Date.now() - started}); };
    const head = (code, contentType) => {
      if (committed) return;
      committed = true; status = code;
      res$.writeHead(code, {'content-type': contentType, 'cache-control': 'no-store', 'x-accel-buffering': 'no'});
    };
    const sendJson = (code, obj) => {
      if (res$.destroyed) return;
      head(code, 'application/json');
      res$.end(JSON.stringify(obj));
    };

    // Cloudflare closes proxied requests that send nothing for 100 seconds. For long
    // non-streamed answers, send the headers early and keep the connection alive with
    // whitespace, which JSON parsers ignore.
    if (!stream) keepaliveTimer = setTimeout(() => {
      head(200, 'application/json');
      res$.write(' ');
      keepaliveInterval = setInterval(() => res$.write(' '), 15000);
    }, config.keepaliveAfterMs);

    const headers = {'content-type': 'application/json'};
    if (model.key) headers.authorization = `Bearer ${model.key}`;
    const deadline = AbortSignal.timeout(config.timeoutMs);
    try {
      const upstream = await fetcher(model.url + path, {
        method: 'POST', headers, body: JSON.stringify(body), redirect: 'error',
        signal: AbortSignal.any([clientGone.signal, deadline]),
      });
      if (!upstream.ok) {
        const text = (await upstream.text()).slice(0, 4000);
        let message;
        try { const parsed = JSON.parse(text); message = parsed.error?.message || parsed.error || parsed.message; } catch {}
        clearTimeout(keepaliveTimer);
        return sendJson(upstream.status >= 500 ? 502 : upstream.status, errorBody(`${model.id}: ${typeof message === 'string' ? message : `HTTP ${upstream.status}`}`, 'upstream_error'));
      }
      clearTimeout(keepaliveTimer);
      if (stream) {
        head(200, upstream.headers.get('content-type') || 'text/event-stream');
        for await (const chunk of upstream.body) {
          if (res$.destroyed) break;
          if (!res$.write(chunk)) await once(res$, 'drain');
        }
        res$.end();
      } else {
        const text = await upstream.text();
        head(200, 'application/json');
        res$.end(text);
      }
    } catch (error) {
      if (clientGone.signal.aborted) { res$.destroy(); return; }
      if (deadline.aborted) return sendJson(504, errorBody(`${model.id} did not finish within the time limit.`, 'timeout_error'));
      if (committed && stream) { res$.end(); return; }
      sendJson(502, errorBody(`${model.id} is unreachable. It may still be loading.`, 'upstream_error'));
    } finally {
      finish();
    }
  }

  app.setErrorHandler((error, _req, reply) => {
    const status = error.statusCode && error.statusCode < 500 ? error.statusCode : 500;
    reply.code(status).send(errorBody(status === 413 ? 'Request too large.' : status < 500 ? 'Malformed request.' : 'Gateway error.', 'invalid_request_error'));
  });
  return app;
}
