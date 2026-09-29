# Start with Open WebUI and your existing GPUs

This is the local v3 extension of Claude's `model-hub-v2.zip`. Your running rental has not been changed. The original archive and earlier project remain intact.

**Keep Open WebUI as your chat interface.** The added CPU controller runs background team jobs and stores project evidence. Its web panel is optional. Follow [the Open WebUI integration guide](docs/OPEN-WEBUI.md) to add the team models beside your current direct Qwen models.

## 1. Prepare the controller locally

Use Python 3.11 or 3.12. From this folder in PowerShell:

```powershell
python -m venv agents/.venv
agents/.venv/Scripts/python -m pip install -r agents/requirements.txt
Copy-Item agents/.env.example agents/.env
```

Edit `agents/.env` locally:

```dotenv
HUB_URL=https://api.YOUR-DOMAIN/v1
HUB_KEY=your-existing-agent-key
FRONTIER_MODEL=
```

Use a normal agent key, not the gateway admin key. Keep keys out of GitHub and chat. Leave the frontier field empty for local-model-only work.

```powershell
agents/.venv/Scripts/python agents/console.py
```

Open **http://127.0.0.1:8787**. The owner key is generated at `agents/data/console.key`; paste it into the unlock form. It is a separate key for the workspace, not a provider credential. The UI holds it only in the current browser tab.

This panel is useful for importing sources and inspecting work. To run tasks from Open WebUI, install `integrations/openwebui_pipe.py` using [these steps](docs/OPEN-WEBUI.md). The Open WebUI server must be able to reach the controller. A rental-hosted Open WebUI cannot reach your laptop through `127.0.0.1`; use a deliberate private connection or the optional colocated controller in a future v3 deployment.

You can inspect the workspace before connecting models. Tasks stay queued while the gateway settings are absent. Start with `--no-worker` whenever you want to inspect data without executing jobs. After changing `.env`, restart the controller.

**v2 compatibility:** the workspace can call the running v2 gateway, and its task deadlines, retrieval and persistence work locally. The improved queue behavior, request limits and supervisor defaults require a later deployment of the v3 gateway. Local development does not change the running server.

## 2. Give the team a real project

Create a project, add a brief, and import text or source files in the optional panel's **Knowledge** page. Add your audience, budget, preferences and constraints in **Memory**. In Open WebUI, choose the matching **Hub** team in the model selector. Write a concrete request, and start with the **Fast** or **Balanced** profile in the Pipe's settings. The panel's Tasks page provides the same job controls.

Use `/hub tasks` to find work needing attention, `/hub status JOB_ID` to inspect progress, and `/hub handoff JOB_ID` to export a continuation brief. Plans and handoffs are the most useful additions adapted from [AgentMatrix](docs/AGENTMATRIX-REVIEW.md).

Try these:

| Skill | A useful first task |
|---|---|
| Research brief | “Compare the approaches in my uploaded notes. Cite sources, identify gaps, and save a recommendation.” |
| Content studio | “Using our brand brief and collected posts, draft three original 30-second scripts with shot lists and captions.” |
| Trend report | “Analyze the last 48 hours of our collected sample. Give supporting post IDs and three ideas worth testing.” |
| Code review | “Review the imported source files. Prioritize concrete bugs and say what requires a runtime test.” |
| Project planner | “Break this project into deliverables with dependencies and acceptance criteria. Save a handoff for the next run.” |
| Decision brief | “Compare these three options against my budget, speed and maintenance constraints.” |
| Visual production | “Develop three cover concepts and production prompts. Generate one image if the third slot is configured.” |

Agents search imported material; there is no built-in live web search or shell execution. They cite document/chunk IDs and collected post IDs. Expand their evidence before expecting a useful factual answer.

## 3. Measure before increasing concurrency

These commands make real calls to your two resident models when you choose to run them:

```powershell
agents/.venv/Scripts/python agents/benchmark.py --concurrency 1 --tokens 512
agents/.venv/Scripts/python agents/benchmark.py --concurrency 2 --tokens 512
agents/.venv/Scripts/python agents/benchmark.py --concurrency 2 --tokens 512 --thinking
```

Compare time to first answer and total duration. Do not assume six simultaneous slots are faster than two. Each GPU runs three slots sharing the largest context that fits (134K on a 3090, 262K on a V100 32GB), with unbounded thinking by default. The model weights and vision encoders remain Claude's pinned choices. Context fit and useful answer quality need live validation on your rental.

The profiles allow up to 5, 15 or 30 minutes (fast, balanced, deep) for the entire agent task. These are hard task deadlines, not promised completion times. A timeout retains saved progress and can be resumed explicitly. Repeated resumes can extend total work and spend.

## 4. Optional frontier advice and images

For frontier advice, list the exact provider model on the gateway, use an agent key with frontier permission, and set `FRONTIER_MODEL` to its gateway ID. Enable **Allow paid frontier advice** on a task. At most two advisor model calls are reserved per job, including attempts after Resume.

For images, read [the third-model guide](docs/THIRD-MODEL.md). No third GPU is assumed or rented. No model weights are downloaded until you explicitly start an optional service or run transcription.

## 5. Keep the work safe when upgrading

The data folder contains the project database, documents, transcripts, captured screenshots, artifacts and session histories. Back it up together. Do not mix a local and a remote copy as if they synchronize automatically. [Upgrade instructions](docs/UPGRADE.md) explain migration and the optional always-on CPU controller on the rental.

Phone collection, automatic posting and frontier-quality reasoning are different capabilities. This version improves collection and drafting; it does not implement social publishing or guarantee frontier-level results.
