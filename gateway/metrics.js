// Bounded operational telemetry. Never keep prompts, completions, API keys or image payloads.
export class Metrics {
  constructor(limit = 500) { this.limit = limit; this.entries = []; }
  record(entry) { this.entries.push({...entry}); if (this.entries.length > this.limit) this.entries.shift(); }
  summary(client) {
    const entries = this.entries.filter(e => e.client === client);
    return [...new Set(entries.map(e => e.model))].map(model => {
      const calls = entries.filter(e => e.model === model);
      const times = calls.map(e => e.ms).sort((a, b) => a - b);
      const percentile = p => times[Math.max(0, Math.ceil(times.length * p) - 1)] ?? 0;
      return {model, requests: calls.length, errors: calls.filter(e => e.status >= 400).length,
        p50Ms: percentile(.5), p95Ms: percentile(.95),
        promptTokens: calls.reduce((n, e) => n + (e.usage?.prompt_tokens || 0), 0),
        completionTokens: calls.reduce((n, e) => n + (e.usage?.completion_tokens || 0), 0)};
    });
  }
}
