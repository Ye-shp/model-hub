# AgentMatrix: what is useful here

Reviewed September 28, 2026: [Ye-shp/AgentMatrix](https://github.com/Ye-shp/AgentMatrix), commit `344ea59a005d1c76cb236a0205aa295013237b98`. The repository was cloned for inspection; its setup scripts, global hooks and dependency installation were not run.

## Assessment

AgentMatrix is an Electron desktop environment for managing Claude Code and Copilot CLI sessions. Its strongest fit for Model Hub is its coordination design: surface the work that needs attention, show structured progress, and make session handoffs explicit. It is not a drop-in inference engine or a replacement for Open WebUI.

The README's “No API keys needed” refers to driving installed CLI programs using their existing authentication. It does not provide a free frontier API for the Qwen team. The autonomous organization document is a design proposal; it should not be treated as proof that a complete autonomous company is implemented.

## Adapted in this version

| AgentMatrix reference | Independent implementation in Model Hub |
|---|---|
| `lib/dashboard/attentionQueue.ts` | `agents/coordination.py`: one attention item per task, prioritizing overdue heartbeats, failed/interrupted work and completed results awaiting review |
| Task and structured-progress concepts in `lib/state/appTaskStore.ts` and the coordination design | SQLite task plans, exposed in task detail and `/hub status`; the lead can update steps without storing its private deliberation |
| `electron/services/HandoffService.ts` | Deterministic Markdown handoffs from existing brief, plan, notes, evidence index and output references; no additional model call or terminal scraping |
| Separation of a coordination surface from actual agent execution | An Open WebUI Pipe starts and inspects durable jobs; the CPU worker owns execution and survives browser disconnects |
| `lib/state/contextUsage.ts` distinction between known and unknown usage | No invented context-fill percentage. Gateway metrics report observed request counts, token usage when supplied, errors and latency instead |

These are fresh implementations tailored to the existing Fastify/Python/SQLite stack. No AgentMatrix source was copied. No license file was present in the inspected checkout, so its code was not treated as a licensed library dependency.

## Components left out

- **Electron, terminal emulation and node-pty:** useful for a desktop coding product, unnecessary for the GPU server and Open WebUI.
- **Global Claude/Copilot hooks and provider-specific terminal parsing:** introduce a second execution/authentication path and machine-wide changes.
- **Flat JSON task persistence:** the existing SQLite transactions are a better fit for multiple workers and collectors.
- **A full company hierarchy:** increases latency and calls before it demonstrates better results on these GPUs. The current team delegates only focused tasks.
- **Automatic repository updates and package proxy policy:** specific to that application, not needed for this project.

## How to use the adapted features

In Open WebUI, select a Hub team and send a concrete task. Use `/hub tasks` to see work needing attention, `/hub status JOB_ID` to inspect its plan and result, `/hub handoff JOB_ID` to export context, and `/hub review JOB_ID` after reviewing a completed result.

Handoffs include up to 30 notes, 100 document references and 100 artifact references, with inclusion counts. They preserve the originals and explicitly omit full source texts. They are useful snapshots, not a promise of zero context degradation. The chat display abbreviates very large handoffs; the full file is saved as a deliverable.
