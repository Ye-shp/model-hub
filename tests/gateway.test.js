import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createApp} from '../gateway/app.js';
import {readConfig} from '../gateway/config.js';
import {ClientStore} from '../gateway/clients.js';
import {UsageLedger} from '../gateway/ledger.js';
import {Pool, QueueFullError} from '../gateway/queue.js';

// A fake OpenAI-compatible model server whose behaviour each test can replace.
async function fakeUpstream() {
  const state = {handler: null, requests: []};
  const server = http.createServer(async (req, res) => {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    const entry = {url: req.url, headers: req.headers, body: raw ? JSON.parse(raw) : null, aborted: false};
    req.on('close', () => { if (!res.writableFinished) entry.aborted = true; });
    res.on('close', () => { if (!res.writableFinished) entry.aborted = true; });
    state.requests.push(entry);
    if (state.handler) return state.handler(entry, res);
    res.writeHead(200, {'content-type': 'application/json'});
    res.end(JSON.stringify({choices: [{message: {role: 'assistant', content: `hi from ${entry.body.model}`}}]}));
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  return {state, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(r => { server.closeAllConnections(); server.close(r); })};
}

async function setup(t, env = {}) {
  const up = await fakeUpstream();
  const dir = await mkdtemp(join(tmpdir(), 'hub-'));
  const config = readConfig({
    DATA_DIR: dir, MODEL_API_KEY: 'internal', MODEL_PARALLEL: '1', KEEPALIVE_AFTER_MS: '60000',
    MODEL1_URL: `${up.url}/r1/v1`, MODEL2_URL: `${up.url}/r2/v1`,
    OPENAI_API_KEY: 'sk-test', OPENAI_MODELS: 'gpt-test', ...env,
  });
  // Point the frontier and image entries at the fake server too.
  for (const m of config.models) if (m.frontier) m.url = `${up.url}/openai/v1`;
  for (const m of config.models) if (m.kind === 'image') m.url = `${up.url}/img/v1`;
  const clients = new ClientStore(dir);
  const friend = clients.add('friend');
  const owner = clients.add('owner', {frontier: true});
  const ledger = new UsageLedger(dir, Number(env.FRONTIER_MONTHLY_CALLS ?? 5));
  await ledger.init();
  const app = createApp({config, clients, ledger});
  await app.listen({port: 0, host: '127.0.0.1'});
  const base = `http://127.0.0.1:${app.server.address().port}`;
  t.after(async () => { await app.close(); await up.close(); await rm(dir, {recursive: true, force: true}); });
  const call = (key, path, body, extra = {}) => fetch(base + path, {
    method: body ? 'POST' : 'GET', headers: {authorization: `Bearer ${key}`, 'content-type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined, ...extra,
  });
  return {up, base, call, friend, owner, clients, config};
}
const chat = (model, content = 'hello', extra = {}) => ({model, messages: [{role: 'user', content}], ...extra});

test('health is public; everything else needs a valid key', async t => {
  const {base, call} = await setup(t);
  assert.equal((await fetch(`${base}/health`)).status, 200);
  assert.equal((await fetch(`${base}/v1/models`)).status, 401);
  assert.equal((await call('mh_not-a-real-key-000000000', '/v1/models')).status, 401);
});

test('model list hides paid frontier models from keys without frontier access', async t => {
  const {call, friend, owner} = await setup(t);
  const ids = async key => (await (await call(key, '/v1/models')).json()).data.map(m => m.id);
  assert.deepEqual(await ids(friend), ['qwen', 'qwen-1', 'qwen-2']);
  assert.deepEqual(await ids(owner), ['qwen', 'qwen-1', 'qwen-2', 'openai/gpt-test']);
  assert.equal((await call(friend, '/v1/chat/completions', chat('openai/gpt-test'))).status, 404);
});

test('requests are rebuilt from an allow-list and sent with the internal model key', async t => {
  const {call, owner, up} = await setup(t);
  const res = await call(owner, '/v1/chat/completions', chat('qwen-2', 'hi', {evil_param: 1, top_k: 20, reasoning_effort: 'low', max_tokens: 999999}));
  assert.equal(res.status, 200);
  assert.equal((await res.json()).choices[0].message.content, 'hi from qwen-2');
  const sent = up.state.requests[0];
  assert.equal(sent.url, '/r2/v1/chat/completions');
  assert.equal(sent.headers.authorization, 'Bearer internal');
  assert.equal(sent.body.evil_param, undefined);
  assert.equal(sent.body.top_k, 20);
  assert.equal(sent.body.reasoning_effort, 'low');
  assert.equal(sent.body.max_tokens, 32768);
});

test('images must be inline data URIs; URLs and file paths are refused', async t => {
  const {call, friend, up} = await setup(t);
  const withImage = url => ({model: 'qwen-1', messages: [{role: 'user', content: [{type: 'text', text: 'what is this'}, {type: 'image_url', image_url: {url}}]}]});
  for (const bad of ['http://169.254.169.254/latest', 'https://example.com/a.png', '/workspace/data/clients.json', 'file:///etc/passwd'])
    assert.equal((await call(friend, '/v1/chat/completions', withImage(bad))).status, 400, bad);
  assert.equal(up.state.requests.length, 0);
  assert.equal((await call(friend, '/v1/chat/completions', withImage('data:image/png;base64,iVBORw0KGgo='))).status, 200);
});

test('template options are limited to the thinking controls', async t => {
  const {call, friend} = await setup(t);
  assert.equal((await call(friend, '/v1/chat/completions', chat('qwen-1', 'x', {chat_template_kwargs: {enable_thinking: false}}))).status, 200);
  assert.equal((await call(friend, '/v1/chat/completions', chat('qwen-1', 'x', {chat_template_kwargs: {tools_in_system: 'x'}}))).status, 400);
});

test('a busy model queues the next request instead of rejecting it', async t => {
  const {call, friend, up} = await setup(t);
  const order = [];
  up.state.handler = (entry, res) => setTimeout(() => {
    order.push(entry.body.messages[0].content);
    res.writeHead(200, {'content-type': 'application/json'});
    res.end(JSON.stringify({choices: [{message: {content: 'ok'}}]}));
  }, 60);
  const results = await Promise.all(['first', 'second', 'third'].map(c => call(friend, '/v1/chat/completions', chat('qwen-1', c))));
  assert.deepEqual(results.map(r => r.status), [200, 200, 200]);
  assert.deepEqual(order, ['first', 'second', 'third']);
});

test('the "qwen" alias spreads concurrent work across both residents', async t => {
  const {call, friend, up} = await setup(t);
  up.state.handler = (entry, res) => setTimeout(() => { res.writeHead(200, {'content-type': 'application/json'}); res.end('{"choices":[]}'); }, 50);
  await Promise.all([1, 2].map(() => call(friend, '/v1/chat/completions', chat('qwen'))));
  assert.deepEqual(up.state.requests.map(r => r.body.model).sort(), ['qwen-1', 'qwen-2']);
});

test('streaming responses pass through as they are produced', async t => {
  const {call, friend, up} = await setup(t);
  up.state.handler = (entry, res) => {
    res.writeHead(200, {'content-type': 'text/event-stream'});
    res.write('data: {"choices":[{"delta":{"content":"a"}}]}\n\n');
    setTimeout(() => { res.write('data: [DONE]\n\n'); res.end(); }, 30);
  };
  const res = await call(friend, '/v1/chat/completions', chat('qwen-1', 'x', {stream: true}));
  assert.equal(res.headers.get('content-type'), 'text/event-stream');
  const text = await res.text();
  assert.match(text, /"content":"a"/);
  assert.match(text, /\[DONE\]/);
});

test('non-streamed answers keep headers uncommitted until their real result', async t => {
  const {call, friend, up} = await setup(t, {KEEPALIVE_AFTER_MS: '1000'});
  up.state.handler = (entry, res) => setTimeout(() => { res.writeHead(200, {'content-type': 'application/json'}); res.end('{"choices":[{"message":{"content":"late"}}]}'); }, 1300);
  const res = await call(friend, '/v1/chat/completions', chat('qwen-1'));
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /^\{/);
  assert.equal(JSON.parse(text).choices[0].message.content, 'late');
});

test('a disconnected client cancels the model call and frees the slot', async t => {
  const {base, call, friend, up} = await setup(t);
  up.state.handler = () => {}; // never answers
  const controller = new AbortController();
  const pending = fetch(`${base}/v1/chat/completions`, {method: 'POST', signal: controller.signal, headers: {authorization: `Bearer ${friend}`, 'content-type': 'application/json'}, body: JSON.stringify(chat('qwen-1'))}).catch(() => 'aborted');
  await new Promise(r => setTimeout(r, 80));
  controller.abort();
  assert.equal(await pending, 'aborted');
  await new Promise(r => setTimeout(r, 80));
  assert.equal(up.state.requests[0].aborted, true);
  up.state.handler = null;
  const status = await (await call(friend, '/v1/status')).json();
  assert.equal(status.models.find(m => m.model === 'qwen-1').running, 0);
  assert.equal((await call(friend, '/v1/chat/completions', chat('qwen-1'))).status, 200);
});

test('frontier calls stop at the monthly allowance', async t => {
  const {call, owner} = await setup(t, {FRONTIER_MONTHLY_CALLS: '1'});
  assert.equal((await call(owner, '/v1/chat/completions', chat('openai/gpt-test'))).status, 200);
  assert.equal((await call(owner, '/v1/chat/completions', chat('openai/gpt-test'))).status, 429);
});

test('frontier requests drop llama-only options and use max_completion_tokens', async t => {
  const {call, owner, up} = await setup(t);
  await call(owner, '/v1/chat/completions', chat('openai/gpt-test', 'x', {top_k: 5, chat_template_kwargs: {enable_thinking: true}}));
  const sent = up.state.requests[0];
  assert.equal(sent.url, '/openai/v1/chat/completions');
  assert.equal(sent.headers.authorization, 'Bearer sk-test');
  assert.equal(sent.body.model, 'gpt-test');
  assert.equal(sent.body.top_k, undefined);
  assert.equal(sent.body.chat_template_kwargs, undefined);
  assert.equal(sent.body.max_completion_tokens, 8192);
});

test('upstream errors are reported with the model name, e.g. context overflow', async t => {
  const {call, friend, up} = await setup(t);
  up.state.handler = (entry, res) => { res.writeHead(400, {'content-type': 'application/json'}); res.end('{"error":{"message":"the request exceeds the available context size"}}'); };
  const res = await call(friend, '/v1/chat/completions', chat('qwen-1'));
  assert.equal(res.status, 400);
  assert.match((await res.json()).error.message, /qwen-1: the request exceeds the available context size/);
});

test('an unreachable model returns 502 and releases its slot', async t => {
  const {call, friend, config} = await setup(t);
  config.models[0].url = 'http://127.0.0.1:1/v1';
  assert.equal((await call(friend, '/v1/chat/completions', chat('qwen-1'))).status, 502);
  const status = await (await call(friend, '/v1/status')).json();
  assert.equal(status.models.find(m => m.model === 'qwen-1').running, 0);
});

test('keys can be added and revoked without restarting', async t => {
  const {call, clients} = await setup(t);
  const key = clients.add('temp');
  assert.equal((await call(key, '/v1/models')).status, 200);
  clients.remove('temp');
  assert.equal((await call(key, '/v1/models')).status, 401);
});

test('queue refuses work beyond its limit', async () => {
  const pool = new Pool('m', 1, {maxQueue: 1, maxWaitMs: 5000});
  const release = await pool.acquire();
  const waiting = pool.acquire();
  await assert.rejects(pool.acquire(), QueueFullError);
  release();
  (await waiting)();
  assert.deepEqual(pool.status(), {model: 'm', running: 0, waiting: 0, capacity: 1});
});

test('admin key creates, lists and revokes client keys over the API', async t => {
  const admin = 'admin-key-for-tests-with-at-least-32-chars';
  const {base, call} = await setup(t, {ADMIN_KEY: admin});
  const adminCall = (method, path, body) => fetch(base + path, {method, headers: {authorization: `Bearer ${admin}`, 'content-type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  assert.equal((await fetch(`${base}/admin/keys`, {headers: {authorization: 'Bearer wrong-wrong-wrong-wrong-wrong'}})).status, 401);
  const created = await (await adminCall('POST', '/admin/keys', {name: 'phone-agent', frontier: false})).json();
  assert.match(created.key, /^mh_/);
  assert.equal((await call(created.key, '/v1/models')).status, 200);
  const listed = await (await adminCall('GET', '/admin/keys')).json();
  assert.ok(listed.keys.some(k => k.name === 'phone-agent'));
  assert.ok(!JSON.stringify(listed).includes(created.key));
  assert.equal((await adminCall('DELETE', '/admin/keys/phone-agent')).status, 200);
  assert.equal((await call(created.key, '/v1/models')).status, 401);
  assert.equal((await adminCall('POST', '/admin/keys', {name: 'Bad Name!'})).status, 400);
});

test('client keys cannot use admin routes, and admin routes are off without ADMIN_KEY', async t => {
  const {base, owner} = await setup(t);
  assert.equal((await fetch(`${base}/admin/keys`, {headers: {authorization: `Bearer ${owner}`}})).status, 401);
});

test('the website key name is reserved', async t => {
  const admin = 'admin-key-for-tests-with-at-least-32-chars';
  const {base} = await setup(t, {ADMIN_KEY: admin});
  const res = await fetch(`${base}/admin/keys`, {method: 'POST', headers: {authorization: `Bearer ${admin}`, 'content-type': 'application/json'}, body: JSON.stringify({name: 'open-webui'})});
  assert.equal(res.status, 400);
});

test('late non-streaming provider errors preserve their HTTP status', async t => {
  const {call, friend, up} = await setup(t, {KEEPALIVE_AFTER_MS: '1000'});
  up.state.handler = (_entry, res) => setTimeout(() => {
    res.writeHead(400, {'content-type':'application/json'}); res.end('{"error":{"message":"context overflow"}}');
  }, 1200);
  const res = await call(friend, '/v1/chat/completions', chat('qwen-1'));
  assert.equal(res.status, 400);
  assert.match((await res.json()).error.message, /context overflow/);
});

test('streaming queued requests send headers and keep-alives before a GPU becomes free', async t => {
  const {call, friend, up} = await setup(t, {KEEPALIVE_AFTER_MS:'1000'});
  up.state.handler = (_entry, res) => setTimeout(() => {
    res.writeHead(200, {'content-type':'text/event-stream'}); res.end('data: [DONE]\n\n');
  }, 1400);
  const first = await call(friend, '/v1/chat/completions', chat('qwen-1', 'one', {stream:true}));
  const second = await call(friend, '/v1/chat/completions', chat('qwen-1', 'two', {stream:true}));
  const reader = second.body.getReader();
  const early = new TextDecoder().decode((await reader.read()).value);
  assert.match(early, /: connected/);
  let rest=''; for (;;) { const chunk=await reader.read(); if(chunk.done)break; rest+=new TextDecoder().decode(chunk.value); }
  assert.match(rest, /: keep-alive/);
  assert.match(rest, /\[DONE\]/);
  await first.text();
});

test('heartbeat does not corrupt an upstream SSE event split across chunks', async t => {
  const {call, friend, up} = await setup(t, {KEEPALIVE_AFTER_MS:'1000'});
  up.state.handler = (_entry, res) => {
    res.writeHead(200, {'content-type':'text/event-stream'});
    res.write('data: {"choices":[{"delta":{"content":"');
    setTimeout(() => res.end('hello"}}]}\n\ndata: [DONE]\n\n'), 1200);
  };
  const res=await call(friend, '/v1/chat/completions', chat('qwen-1','x',{stream:true}));
  const events=(await res.text()).split('\n\n').filter(e=>e.startsWith('data: ')&&!e.includes('[DONE]'));
  assert.equal(JSON.parse(events[0].slice(6)).choices[0].delta.content,'hello');
});

test('non-streaming deadline includes queue wait and frees queued entries', async t => {
  const {call, friend, up} = await setup(t, {NONSTREAM_TIMEOUT_MS:'1000'});
  up.state.handler = () => {};
  const results=await Promise.all([call(friend,'/v1/chat/completions',chat('qwen-1')),call(friend,'/v1/chat/completions',chat('qwen-1'))]);
  assert.deepEqual(results.map(r=>r.status),[504,504]);
  await Promise.all(results.map(r=>r.text()));
  const status=await (await call(friend,'/v1/status')).json();
  assert.equal(status.models[0].running,0); assert.equal(status.models[0].waiting,0);
});

test('stream errors are explicit error events, not silently successful empty answers', async t => {
  const {call, friend, up} = await setup(t);
  up.state.handler=(_entry,res)=>{res.writeHead(400);res.end('{"error":{"message":"bad context"}}');};
  const res=await call(friend,'/v1/chat/completions',chat('qwen-1','x',{stream:true}));
  const body=await res.text(); assert.match(body,/"error"/); assert.match(body,/bad context/); assert.match(body,/\[DONE\]/);
});

test('usage metrics are scoped to the current API key and exclude prompt text', async t => {
  const {call, friend, owner, up} = await setup(t);
  up.state.handler=(_entry,res)=>{res.writeHead(200,{'content-type':'application/json'});res.end('{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}');};
  await (await call(owner,'/v1/chat/completions',chat('qwen-1','PRIVATE-PROMPT'))).text();
  const ownerStatus=await (await call(owner,'/v1/status')).json();
  assert.equal(ownerStatus.metrics.models[0].completionTokens,5);
  assert.ok(!JSON.stringify(ownerStatus).includes('PRIVATE-PROMPT'));
  assert.deepEqual((await (await call(friend,'/v1/status')).json()).metrics.models,[]);
});

test('image requests reach the image slot with validated options only', async t => {
  const {call, friend, up} = await setup(t, {MODEL3_URL: 'PLACEHOLDER', MODEL3_ID: 'qwen-image-2.1', MODEL3_KIND: 'image', MODEL3_API_KEY: 'img-key'});
  up.state.handler = (_entry, res) => { res.writeHead(200, {'content-type': 'application/json'}); res.end('{"created":1,"data":[{"b64_json":"aGk="}]}'); };
  const ok = await call(friend, '/v1/images/generations', {model: 'flex', prompt: 'reel cover', size: '1152x2048', steps: 20, seed: 7, negative_prompt: 'blurry', user: 'x', url: 'file:///etc/passwd'});
  assert.equal(ok.status, 200);
  assert.equal(JSON.parse(await ok.text()).data[0].b64_json, 'aGk=');
  const sent = up.state.requests.at(-1);
  assert.equal(sent.url, '/img/v1/images/generations');
  assert.equal(sent.headers.authorization, 'Bearer img-key');
  assert.deepEqual(sent.body, {model: 'qwen-image-2.1', prompt: 'reel cover', size: '1152x2048', n: 1, response_format: 'b64_json', negative_prompt: 'blurry', steps: 20, seed: 7});
  assert.equal((await call(friend, '/v1/images/generations', {model: 'flex', prompt: 'x', size: '999x999'})).status, 400);
  assert.equal((await call(friend, '/v1/images/generations', {model: 'flex', prompt: 'x', steps: 500})).status, 400);
  assert.equal((await call(friend, '/v1/chat/completions', chat('flex'))).status, 400);
});

test('slow images keep the connection alive with whitespace and still parse as JSON', async t => {
  const {call, friend, up} = await setup(t, {MODEL3_URL: 'PLACEHOLDER', MODEL3_ID: 'qwen-image-2.1', MODEL3_KIND: 'image', KEEPALIVE_AFTER_MS: '1000', NONSTREAM_TIMEOUT_MS: '1000'});
  up.state.handler = (_entry, res) => setTimeout(() => { res.writeHead(200, {'content-type': 'application/json'}); res.end('{"data":[{"b64_json":"aGk="}]}'); }, 2500);
  const res = await call(friend, '/v1/images/generations', {model: 'flex', prompt: 'slow'});
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /^ +\{/);  // keep-alive spaces arrived before the body, beyond the 1 s chat limit
  assert.equal(JSON.parse(text).data[0].b64_json, 'aGk=');
});

test('the phone bridge reaches the controller through /bridge/* only, with its own key', async t => {
  const {up, base, config} = await setup(t, {ENABLE_AGENT_CONSOLE: 'true'});
  assert.equal(config.controllerUrl, 'http://127.0.0.1:8787');
  config.controllerUrl = `${up.url}/ctl`;  // the fake server plays the controller
  up.state.handler = (entry, res) => {
    res.writeHead(entry.headers.authorization === 'Bearer phb_good' ? 200 : 401, {'content-type': 'application/json'});
    res.end(JSON.stringify({path: entry.url, got: entry.body}));
  };
  const post = (path, key) => fetch(base + path, {method: 'POST', headers: {authorization: `Bearer ${key}`, 'content-type': 'application/json'},
    body: JSON.stringify({device: 'abc'})});
  let r = await post('/bridge/poll', 'phb_good');
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), {path: '/ctl/bridge/poll', got: {device: 'abc'}});
  assert.equal((await post('/bridge/result', 'phb_bad')).status, 401);
  assert.equal((await post('/bridge/other', 'phb_good')).status, 404);
  config.controllerUrl = '';
  assert.equal((await post('/bridge/poll', 'phb_good')).status, 404);
  const plain = await setup(t);
  assert.equal(plain.config.controllerUrl, '');
});
