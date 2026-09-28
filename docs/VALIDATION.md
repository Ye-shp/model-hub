# Local validation — September 28, 2026

This is a locally tested candidate release based on Claude's v2 archive. No live rental, GitHub repository, provider account or Open WebUI installation was changed.

## Passed

- **25 gateway tests:** authentication, per-key model visibility, request validation, queueing, least-loaded routing, streaming, cancellation, frontier call limits, key management, non-streaming error statuses, queue deadlines, coherent SSE heartbeats and private usage metrics.
- **28 Python tests:** additive v2 data migration, capture deduplication, time filtering, project-scoped retrieval and memory, bounded valid JSON, jobs, explicit recovery, worker cancellation, saved artifacts, frontier allowances across resumes, all seven skill loaders, owner authentication and host/origin checks, image HTTP adapter, plans, attention ordering, handoff preservation, and Open WebUI Pipe commands/access checks.
- The actual installed Agents SDK ran a mocked streaming tool call, saved a project note, consumed the final answer and persisted its SQLite session. No inference endpoint was contacted.
- Python source parsing, JavaScript syntax checks, JSON parsing and the Skill Creator validator for all seven skills.
- Browser checks against isolated test data: unlock, source import, memory save, job submission, cancellation, task detail and handoff export. The handoff appeared in Deliverables. The tested browser session reported no warning/error logs; the task dialog layout was inspected visually.

Environment: Windows, Python 3.11.5, Node 24.19.0. Runtime dependencies are listed in `agents/requirements.txt` and `package-lock.json`. Python tests use temporary databases and mock HTTP transports. The local preview used a separate data directory and a test-only owner key, with job execution disabled.

## Still requires live validation

- Container build and startup using Claude's pinned CUDA/llama.cpp/Open WebUI base components. The workflow now includes the Python tests, but it was not run on GitHub.
- Actual resident-model loading, VRAM fit, tool-call reliability, answer quality and throughput on the two RTX 3090s.
- Installation and streaming behavior of the Pipe inside the specific Open WebUI release, including Cloudflare behavior and invited-user visibility.
- Real phone/emulator collection, app-screen transitions and multiple-device latency.
- Actual FLUX diffusion loading/rendering and CPU speech transcription. Adapter contracts were tested; the model weights were not downloaded or benchmarked.

Passing local tests does not establish frontier-level intelligence, lossless context sharing, a monthly spend guarantee or production uptime. The included benchmark is an explicit opt-in tool for measuring your hardware later.
