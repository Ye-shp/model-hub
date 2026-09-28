# Upgrade and operating notes

The supplied files are a local candidate release. No GitHub push, container publication, server replacement, account change or paid API call was performed while building it.

## Data compatibility

Back up the entire existing data directory before running v3 on real data. Stop the collector/controller while copying a database, or use SQLite's backup mechanism; a live WAL database can span `.db`, `.db-wal` and `.db-shm` files.

v3 uses additive migrations for Claude's posts and drafts tables. Old records remain in the `default` project. It adds project IDs, capture evidence, source fields and job references, plus new project/document/memory/job tables. The FTS5 search index runs on CPU. No vector database or embedding GPU is required.

Duplicate detection now uses a supplied platform ID/URL or an exact screenshot digest. Without reliable identity, it keeps the observation. This favors preserving evidence over aggressive deduplication; frame changes may yield repeat observations. Old caption-based fingerprints are retained for old rows but not reused to discard new captures.

## Local controller

`python agents/console.py` serves the owner dashboard and one job worker. It allows two simultaneous model calls across the team. One controller may own a data directory at a time; an OS lock prevents concurrent recovery processes.

Queued work survives restarts. A stopped in-progress job becomes `interrupted`; the owner explicitly resumes it. A resume starts a fresh bounded attempt with project notes and saved results, not the exact interrupted model computation. SQLite keeps an audit conversation per attempt. Retrieval and checkpoints reduce repeated context, but are not lossless unlimited model memory.

Open WebUI remains the chat interface. Its optional [team Pipe](OPEN-WEBUI.md) exposes selected-project jobs to administrators and explicitly invited users. The owner dashboard is a separate, optional administrative workspace. Invited Open WebUI chat users do not automatically get access to all projects. Project tools are scoped by project; anyone with the console owner key can access all projects in that controller. It is not a multi-tenant account system.

## Optional always-on controller on the existing rental

The v3 Dockerfile includes a separate CPU Python environment for the controller. It remains off unless these settings are chosen for a future deployment:

```dotenv
ENABLE_AGENT_CONSOLE=true
CONSOLE_URL=
AGENT_FRONTIER_MODEL=
CONSOLE_KEY=
```

The supervisor creates a dedicated internal gateway key. Workspace data lives under `/workspace/data/agent-workspace`; the owner key is generated at `/workspace/data/agent-workspace/console.key` unless supplied explicitly. In the Open WebUI Pipe, use `http://127.0.0.1:8787` and that owner key. No additional public hostname is needed for this arrangement.

If you also want to publish the optional owner panel, set `CONSOLE_URL=https://workspace.YOUR-DOMAIN`, add a Cloudflare tunnel hostname pointing to `http://127.0.0.1:8787`, protect it with Access for the owner's email, and retain the panel's owner-key requirement. The controller never receives provider or tunnel secrets.

Transfer the existing controller data directory before starting the remote controller if you want the same projects. Laptop and server SQLite files do not synchronize. Phone collectors can run on the host with adb access; their collected database must be the same controller data set or imported/transferred deliberately. Do not assume a laptop collector automatically fills a remote controller's SQLite database.

## Changed runtime defaults

| Setting | v3 default | Reason |
|---|---|---|
| `MODEL_PARALLEL` | 1 per resident | Start with predictable latency and memory use |
| `MODEL_CONTEXT` | empty (fit to memory) | Measured 134K on a 3090, 262K on a V100 32GB; set 65536 if a model runs out of memory |
| Thinking budget | -1 / -1 (unbounded) | Set `MODEL1_REASONING_BUDGET` / `MODEL2_REASONING_BUDGET` to cap deliberation |
| Maximum output | 4096 tokens | Bound individual responses |
| Queue | 12 waiting, 30 seconds | Avoid long hidden backlogs |
| Streaming call deadline | 180 seconds including queue | Bounded calls with SSE heartbeats |
| Non-streaming deadline | 90 seconds including queue | Return a real status before a proxy idle timeout |
| Agent task deadline | 180 / 360 / 600 seconds | Fast / Balanced / Deep |

Existing deployment environment values override defaults. Copy deliberate changes into the future deployment configuration; replacing code alone will not overwrite your current settings. Never destroy the rental volume as an update shortcut.

Streaming errors use explicit SSE error events after headers have been sent. Non-streaming errors retain their actual HTTP status. The Python team uses streaming internally, including tool calls. Disconnecting a direct gateway request cancels that upstream call. Closing the Open WebUI team chat leaves its durable job running; use `/hub cancel JOB_ID` to cancel the job. Live Cloudflare behavior remains to be smoke-tested.

## Phones

The collector accepts a selected adb serial, project, frame count and time budget. Three consecutive frames are read by default; this is not full video/audio understanding. Screenshot paths include the project, device and a unique capture ID.

```powershell
agents/.venv/Scripts/python agents/collect.py tiktok --serial DEVICE_ID --project default --posts 5 --frames 3
agents/.venv/Scripts/python agents/fleet.py agents/fleet.example.json --dry-run
```

Fill the fleet file with already-connected devices, then run without `--dry-run`. It runs at most two collectors at once and supports a list of up to ten devices. This is a bounded batch runner, not a cloud-phone provisioning, scheduling or account-management system. It does not bypass login screens or implement posting.

## Validation and remaining live checks

Local checks cover the gateway, SSE tool calls, jobs, cancellation, migration, document search, key authentication and image adapter behavior. Run:

```powershell
npm ci
npm test
agents/.venv/Scripts/python -m unittest discover -s tests -p "test_*.py" -v
```

Before replacing the running system, build the container in a test environment, load the two actual GGUFs, verify the vision encoders, benchmark one and two concurrent calls, confirm Cloudflare streaming/cancellation, and run a five-post phone collection. Test the optional image service separately. No local test establishes model intelligence or live GPU throughput.
