// OpenAI-compatible gateway in front of the two resident Qwen servers, the optional third
// slot and any paid frontier providers. Anything that speaks the OpenAI API (Open WebUI,
// the agent code in agents/, most agent frameworks) can use it with a per-client key.
import Fastify from 'fastify';
import {once} from 'node:events';
import {timingSafeEqual} from 'node:crypto';
import {hashKey} from './clients.js';
import {Pool, leastLoaded, QueueFullError, QueueTimeoutError} from './queue.js';
import {cleanChatRequest, RequestError} from './sanitize.js';
import {Metrics} from './metrics.js';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';

// Sizes the Qwen-Image-2.1 service accepts (services/image_server.py): fast 1K sizes, then native 2K.
const IMAGE_SIZES = new Set(['1024x1024', '1024x1536', '1536x1024', '1152x2048', '2048x1152',
  '2048x2048', '1536x2752', '2752x1536', '1696x2528', '2528x1696']);

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
  const metrics = new Metrics();

  const errorBody = (message, type) => ({error: {message, type}});
  const fail = (reply, status, message, type = 'invalid_request_error') => reply.code(status).send(errorBody(message, type));

  const adminHash = config.adminKey ? hashKey(config.adminKey) : null;
  app.addHook('onRequest', async (req, reply) => {
    reply.header('cache-control', 'no-store');
    if (req.url === '/health' || req.url.startsWith('/bridge/')) return;  // the phone bridge has its own key (checked by the controller)
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

  // Public: gateway up + the supervisor's startup phase (no secrets), so a download can be followed from outside.
  app.get('/health', async () => {
    let status = null;
    try { status = JSON.parse(readFileSync(join(config.dataDir, 'status.json'), 'utf8')); } catch {}
    return {ok: true, ...(status ? {startup: status} : {})};
  });

  app.get('/v1/models', async req => ({
    object: 'list',
    data: [
      ...(residents.length ? [{id: 'qwen', object: 'model', owned_by: 'model-hub', description: 'Whichever resident Qwen is least busy'}] : []),
      // The website reaches the image model through its image-generation setting, not as a chat
      // model, so it is left out of the website's chat list.
      ...available(req.client).filter(m => !(req.client.name === 'open-webui' && m.kind === 'image'))
        .map(m => ({id: m.id, object: 'model', owned_by: m.frontier ? m.id.split('/')[0] : 'model-hub', kind: m.kind})),
    ],
  }));

  app.get('/v1/status', async req => ({
    client: req.client.name,
    models: available(req.client).map(m => pools.get(m.id).status()),
    frontier: {callsThisMonth: ledger.used(), monthlyLimit: config.frontierMonthlyCalls},
    metrics: {scope: 'this key; last 500 gateway requests; resets on restart', models: metrics.summary(req.client.name)},
  }));

  // Key management for the owner (ADMIN_KEY). Keys are returned once, on creation.
  app.get('/admin/keys', async () => ({keys: clients.list().map(({name, frontier, createdAt}) => ({name, frontier, createdAt}))}));
  app.post('/admin/keys', async (req, reply) => {
    const {name, frontier = false} = req.body || {};
    if (['open-webui', 'agent-controller'].includes(name)) return fail(reply, 400, 'That name is reserved for a built-in service key.');
    try { return {name, frontier: Boolean(frontier), key: clients.add(String(name ?? ''), {frontier: frontier === true})}; }
    catch (error) { return fail(reply, 400, error.message); }
  });
  app.delete('/admin/keys/:name', async (req, reply) => (clients.remove(req.params.name) ? {removed: req.params.name} : fail(reply, 404, 'No key with that name.', 'not_found_error')));

  // The phone bridge on the owner's PC reaches the agent controller through here (api.<domain> has no
  // Cloudflare Access login). Only these two endpoints are passed through; the controller checks the bridge key.
  for (const path of ['/bridge/poll', '/bridge/result']) {
    app.post(path, async (req, reply) => {
      if (!config.controllerUrl) return fail(reply, 404, 'The agent controller is not enabled on this hub.', 'not_found_error');
      try {
        const upstream = await fetcher(config.controllerUrl + path, {
          method: 'POST', redirect: 'error', signal: AbortSignal.timeout(90000),
          headers: {'content-type': 'application/json', authorization: String(req.headers.authorization || '')},
          body: JSON.stringify(req.body ?? {}),
        });
        const text = await upstream.text();
        return reply.code(upstream.status).header('content-type', 'application/json').send(text);
      } catch {
        return fail(reply, 502, 'The agent controller is not reachable right now.', 'upstream_error');
      }
    });
  }

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
    const {prompt, size = '1024x1024', negative_prompt: negative, steps, seed} = req.body;
    if (typeof prompt !== 'string' || !prompt.trim() || prompt.length > 8000) return fail(reply, 400, 'prompt must be 1-8000 characters.');
    if (!IMAGE_SIZES.has(size)) return fail(reply, 400, `size must be one of ${[...IMAGE_SIZES].join(', ')}.`);
    if (negative !== undefined && (typeof negative !== 'string' || negative.length > 4000)) return fail(reply, 400, 'negative_prompt must be a string up to 4000 characters.');
    if (steps !== undefined && !(Number.isInteger(steps) && steps >= 8 && steps <= 60)) return fail(reply, 400, 'steps must be an integer from 8 to 60.');
    if (seed !== undefined && !(Number.isInteger(seed) && seed >= 0 && seed <= 4294967295)) return fail(reply, 400, 'seed must be an integer from 0 to 4294967295.');
    const body = {model: model.upstreamModel, prompt, size, n: 1, response_format: 'b64_json'};
    if (negative) body.negative_prompt = negative;
    if (steps !== undefined) body.steps = steps;
    if (seed !== undefined) body.seed = seed;
    return forward(req, reply, model, '/images/generations', body, false, {longRunning: true});
  });

  async function forward(req, reply, model, path, body, stream, {longRunning = false} = {}) {
    const started = Date.now();
    const res$ = reply.raw;
    const clientGone = new AbortController();
    res$.on('close', () => { if (!res$.writableFinished) clientGone.abort(new Error('Client disconnected')); });

    reply.hijack();
    let committed = false, status = 0, release, usage, heartbeat;
    const deadline = AbortSignal.timeout(stream ? config.timeoutMs : longRunning ? config.imageTimeoutMs : config.nonStreamTimeoutMs);
    const signal = AbortSignal.any([clientGone.signal, deadline]);
    const finish = () => {
      clearInterval(heartbeat); release?.();
      const entry = {client: req.client.name, model: model.id, status, ms: Date.now() - started, usage};
      metrics.record(entry); log(entry);
    };
    const head = (code, contentType) => {
      if (committed) return;
      committed = true; status = code;
      res$.writeHead(code, {'content-type': contentType, 'cache-control': 'no-store', 'x-accel-buffering': 'no'});
    };
    const sendError = (code, obj) => {
      if (res$.destroyed) return;
      if (committed && stream) {
        status = code;
        res$.end(`data: ${JSON.stringify(obj)}\n\ndata: [DONE]\n\n`);
        return;
      }
      if (!stream) clearInterval(heartbeat);
      if (committed) status = code;  // headers already went out as 200 (long-running keep-alive)
      head(code, 'application/json');
      res$.end(JSON.stringify(obj));
    };
    // SSE comments are legal keep-alives, including while waiting for a GPU slot.
    // Non-streaming responses keep their real HTTP status and must finish inside Cloudflare's
    // 100 s idle limit (NONSTREAM_TIMEOUT_MS). Long answers should stream.
    // Images (especially 2K) can take minutes: commit the headers early and send whitespace, which
    // JSON parsers ignore, so Cloudflare's 100 s idle limit never cuts the response. A late failure
    // then arrives as a JSON error body with status 200.
    if (longRunning) heartbeat = setInterval(() => {
      if (res$.destroyed) return;
      head(200, 'application/json');
      res$.write(' ');
    }, Math.min(config.keepaliveAfterMs, 15000));
    if (stream) {
      head(200, 'text/event-stream');
      res$.write(': connected\n\n');
      heartbeat = setInterval(() => { if (!res$.destroyed) res$.write(': keep-alive\n\n'); }, Math.min(config.keepaliveAfterMs, 15000));
    }
    const headers = {'content-type': 'application/json'};
    if (model.key) headers.authorization = `Bearer ${model.key}`;
    try {
      release = await pools.get(model.id).acquire(signal);
      if (model.frontier) {
        try { await ledger.reserve(); }
        catch { return sendError(429, errorBody('The monthly frontier call allowance is used up.', 'rate_limit_error')); }
      }
      const upstream = await fetcher(model.url + path, {
        method: 'POST', headers, body: JSON.stringify(body), redirect: 'error',
        signal,
      });
      if (!upstream.ok) {
        const text = (await upstream.text()).slice(0, 4000);
        let message;
        try { const parsed = JSON.parse(text); message = parsed.error?.message || parsed.error || parsed.message; } catch {}
        return sendError(upstream.status >= 500 ? 502 : upstream.status, errorBody(`${model.id}: ${typeof message === 'string' ? message : `HTTP ${upstream.status}`}`, 'upstream_error'));
      }
      if (stream) {
        head(200, upstream.headers.get('content-type') || 'text/event-stream');
        const decoder = new TextDecoder();
        let pending = '';
        for await (const chunk of upstream.body) {
          if (res$.destroyed) break;
          pending += decoder.decode(chunk, {stream: true});
          // Only emit whole SSE events: a heartbeat between partial JSON chunks corrupts the stream.
          let boundary;
          while ((boundary = /\r?\n\r?\n/.exec(pending))) {
            const event = pending.slice(0, boundary.index);
            pending = pending.slice(boundary.index + boundary[0].length);
            for (const line of event.split(/\r?\n/)) {
              if (line.startsWith('data:')) try { const item = JSON.parse(line.slice(5)); if (item.usage) usage = item.usage; } catch {}
            }
            if (!res$.write(event + '\n\n')) await once(res$, 'drain', {signal});
          }
          if (pending.length > config.bodyLimit) throw new Error('Upstream event is too large');
        }
        pending += decoder.decode();
        if (pending.trim() && !res$.destroyed) res$.write(pending + '\n\n');
        res$.end();
      } else {
        const text = await upstream.text();
        clearInterval(heartbeat);
        try { usage = JSON.parse(text).usage; } catch {}
        head(200, 'application/json');
        res$.end(text);
      }
    } catch (error) {
      if (clientGone.signal.aborted) { status = 499; res$.destroy(); return; }
      if (deadline.aborted) return sendError(504, errorBody(`${model.id} exceeded the total request deadline, including its queue wait.`, 'timeout_error'));
      if (error instanceof QueueFullError || error instanceof QueueTimeoutError)
        return sendError(503, errorBody(`${model.id} is busy. Retry shortly.`, 'overloaded_error'));
      sendError(502, errorBody(`${model.id} is unreachable. It may still be loading.`, 'upstream_error'));
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
