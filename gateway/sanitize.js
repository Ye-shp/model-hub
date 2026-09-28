// Validates an OpenAI-style chat request and rebuilds it from an allow-list, so a client can
// only send what the upstream model should see. Images must be inline data URIs: remote URLs
// and file paths are refused because llama-server would otherwise fetch or read them itself.
const ROLES = new Set(['system', 'developer', 'user', 'assistant', 'tool']);
const IMAGE_URI = /^data:image\/(png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=\s]+$/;
const COMMON = ['temperature', 'top_p', 'stop', 'seed', 'presence_penalty', 'frequency_penalty',
  'tools', 'tool_choice', 'parallel_tool_calls', 'response_format', 'stream', 'stream_options', 'user'];
const RESIDENT_EXTRA = ['top_k', 'min_p', 'repeat_penalty', 'reasoning_effort', 'chat_template_kwargs'];
const TEMPLATE_KWARGS = new Set(['enable_thinking', 'reasoning_effort', 'preserve_thinking']);
const EFFORTS = new Set(['none', 'low', 'medium', 'high', 'xhigh']);

export class RequestError extends Error {}
const fail = message => { throw new RequestError(message); };

function cleanPart(part) {
  if (!part || typeof part !== 'object') fail('Each content part must be an object.');
  if (part.type === 'text') {
    if (typeof part.text !== 'string') fail('Text parts need a text string.');
    return {type: 'text', text: part.text};
  }
  if (part.type === 'image_url') {
    const url = typeof part.image_url === 'string' ? part.image_url : part.image_url?.url;
    if (typeof url !== 'string' || !IMAGE_URI.test(url)) fail('Images must be sent inline as data:image/...;base64 URIs.');
    const image = {url};
    if (['low', 'high', 'auto'].includes(part.image_url?.detail)) image.detail = part.image_url.detail;
    return {type: 'image_url', image_url: image};
  }
  fail(`Unsupported content part type: ${String(part.type).slice(0, 40)}`);
}

function cleanMessage(m) {
  if (!m || typeof m !== 'object' || !ROLES.has(m.role)) fail('Each message needs a valid role.');
  const out = {role: m.role};
  if (typeof m.content === 'string') out.content = m.content;
  else if (Array.isArray(m.content)) {
    if (m.content.length > 64) fail('Too many content parts in one message.');
    out.content = m.content.map(cleanPart);
    if (m.role !== 'user' && out.content.some(p => p.type === 'image_url')) fail('Only user messages may contain images.');
  } else if (m.content === null || m.content === undefined) {
    if (!(m.role === 'assistant' && Array.isArray(m.tool_calls))) fail('Message content is required.');
    out.content = null;
  } else fail('Message content must be a string or a list of parts.');
  if (typeof m.name === 'string') out.name = m.name.slice(0, 64);
  if (m.role === 'assistant') {
    if (Array.isArray(m.tool_calls)) out.tool_calls = m.tool_calls.map(c => {
      if (!c || typeof c !== 'object' || typeof c.id !== 'string' || typeof c.function?.name !== 'string') fail('Malformed tool call.');
      return {id: c.id, type: 'function', function: {name: c.function.name, arguments: typeof c.function.arguments === 'string' ? c.function.arguments : JSON.stringify(c.function.arguments ?? {})}};
    });
    if (typeof m.reasoning_content === 'string') out.reasoning_content = m.reasoning_content;
  }
  if (m.role === 'tool') {
    if (typeof m.tool_call_id !== 'string') fail('Tool messages need tool_call_id.');
    out.tool_call_id = m.tool_call_id;
  }
  return out;
}

export function cleanChatRequest(body, model, {maxOutputTokens}) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) fail('Request body must be a JSON object.');
  if (!Array.isArray(body.messages) || body.messages.length === 0 || body.messages.length > 4000) fail('messages must be a non-empty list.');
  if (body.n !== undefined && body.n !== 1) fail('Only n = 1 is supported.');
  const out = {model: model.upstreamModel, messages: body.messages.map(cleanMessage)};
  const allowed = model.frontier ? [...COMMON, ...(model.id.startsWith('openai/') ? ['reasoning_effort'] : [])] : [...COMMON, ...RESIDENT_EXTRA];
  for (const key of allowed) if (body[key] !== undefined) out[key] = body[key];
  if (out.stream !== undefined && typeof out.stream !== 'boolean') fail('stream must be true or false.');
  if (out.reasoning_effort !== undefined && !EFFORTS.has(out.reasoning_effort)) fail('reasoning_effort must be none, low, medium, high or xhigh.');
  if (out.chat_template_kwargs !== undefined) {
    const kw = out.chat_template_kwargs;
    if (!kw || typeof kw !== 'object' || Array.isArray(kw) || Object.keys(kw).some(k => !TEMPLATE_KWARGS.has(k))) fail('chat_template_kwargs may only set enable_thinking, reasoning_effort or preserve_thinking.');
  }
  const requested = body.max_completion_tokens ?? body.max_tokens;
  if (requested !== undefined && (!Number.isInteger(requested) || requested < 1)) fail('max_tokens must be a positive integer.');
  // Frontier providers reject limits above their own model maximum, so default them lower.
  const limit = Math.min(requested ?? (model.frontier ? 8192 : maxOutputTokens), maxOutputTokens);
  if (model.frontier && model.id.startsWith('openai/')) out.max_completion_tokens = limit; else out.max_tokens = limit;
  return out;
}
