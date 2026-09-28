# Model Hub v3 — Open WebUI + a durable agent team

Local extension of Claude's v2 project for your two RTX 3090s. **Open WebUI remains the chat website**, including direct access to the resident models. A new Open WebUI Pipe adds project-aware team tasks. An optional owner panel manages documents, memory, outputs and background jobs.

Nothing in this package has been deployed to your rental or pushed to GitHub. The original v2 archive is unchanged.

## Start here

1. [Start the controller and connect Open WebUI](START-HERE.md).
2. [Install the Open WebUI Pipe](docs/OPEN-WEBUI.md).
3. [Review what was adapted from AgentMatrix](docs/AGENTMATRIX-REVIEW.md).
4. [Choose an optional third model](docs/THIRD-MODEL.md).
5. [Plan the future server upgrade](docs/UPGRADE.md). Fresh-server instructions are in [Deployment](docs/DEPLOYMENT.md).

## What it adds

| Capability | What you can do |
|---|---|
| Open WebUI team models | Choose research, content, trends, planning, code review, decisions or visual production from the model selector |
| Persistent projects | Import text, search evidence locally, retain preferences and decisions, save reports and original drafts |
| Team execution | Lead, researcher, analyst, writer and critic share scoped tools and source references |
| Resumable jobs | Queue, cancel, inspect or resume work; enforce time, turn and model-call allowances |
| Plans and attention | See saved steps, overdue worker heartbeats, interrupted jobs and results awaiting review |
| Handoff exports | Carry the brief, decisions, source IDs, output references and next steps into another session |
| Optional frontier advice | Up to two advisor calls per job when explicitly enabled, including resumed attempts |
| Image generation | Qwen-Image-2.1 with an abliterated text encoder on its own GPU box (non-commercial license); CPU transcription importer for supplied clips |
| Phone preparation | Selected-device collection, multiple frames, evidence storage and bounded batches of up to ten connected devices |
| Gateway improvements | Bounded queues, coherent streaming heartbeats, real error statuses, cancellation and private usage metrics |

The controller is CPU software. It does not load another text model. It sends inference to the two existing resident models through your gateway.

```text
You / invited users → Open WebUI → direct model chat → gateway → two resident GPUs
                             └→ Hub team Pipe → controller → gateway
                                                    ├→ projects, evidence, notes, jobs
                                                    └→ optional frontier / image service
Owner only → optional operations panel ──────────────┘
```

## Limits worth designing around

Two 3090s do not make these models equivalent to frontier models. Retrieval, short delegated tasks, saved plans and checkpoints improve continuity; no mechanism here guarantees lossless unlimited context. Large histories can still exceed the model's context window. Use focused briefs and import source material instead of pasting everything into one prompt.

The third image model requires capacity of its own if both 27B residents must stay loaded. The optional models are not downloaded by starting the normal controller. Frontier and third-slot charges are separate; call caps are not dollar caps.

There is no autonomous social publishing, cloud-phone provisioning, live web search, arbitrary shell execution or video renderer in this version. Phone collection reads a sample of screenshots, not full video and audio. Code-review skills inspect supplied text without executing it.

All users of a Pipe share its configured project. The owner panel key controls all projects. Use separate Pipe configurations and explicit invitations for different groups; this is not a full tenant-per-account system.

## Verify

Use Node 22+ and Python 3.11+ with `agents/requirements.txt` installed:

```sh
npm ci
npm run check
npm test
python -m unittest discover -s tests -p 'test_*.py' -v
```

See [Validation](docs/VALIDATION.md) for actual local results and remaining live checks.
