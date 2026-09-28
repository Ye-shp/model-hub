# Changes from Claude's v2 archive

Baseline: `model-hub-v2.zip`, SHA-256 `911a157df2b05bd29a2b30bd8cd25b3f31f8abbd302705cc27c5231b5e4c6e5e`.

The source is based directly on that archive. Resident model weights and encoder pins in `deploy/models.json` are unchanged. Existing authentication, Cloudflare/Open WebUI integration and gateway model routes are retained.

## Modified files

- `.dockerignore`
- `.env.example`
- `.github/workflows/image.yml`
- `.gitignore`
- `README.md`
- `agents/.env.example`
- `agents/collect.py`
- `agents/crew.py`
- `agents/hub.py`
- `agents/phone.py`
- `agents/requirements.txt`
- `agents/store.py`
- `deploy/Dockerfile`
- `deploy/download.mjs`
- `deploy/supervise.mjs`
- `gateway/app.js`
- `gateway/config.js`
- `package-lock.json`
- `package.json`
- `tests/gateway.test.js`

## Added files

- `START-HERE.md`
- `agents/benchmark.py`
- `agents/console.py`
- `agents/coordination.py`
- `agents/fleet.example.json`
- `agents/fleet.py`
- `agents/media.py`
- `agents/requirements-audio.txt`
- `agents/skills.py`
- `agents/worker.py`
- `agents/workspace.py`
- `console/app.js`
- `console/index.html`
- `console/style.css`
- `docs/AGENTMATRIX-REVIEW.md`
- `docs/DEPLOYMENT.md`
- `docs/OPEN-WEBUI.md`
- `docs/THIRD-MODEL.md`
- `docs/UPGRADE.md`
- `docs/VALIDATION.md`
- `gateway/metrics.js`
- `integrations/openwebui_pipe.py`
- `models/catalog.json`
- `services/image_server.py`
- `services/requirements-image.txt`
- `skills/code-review/SKILL.md`
- `skills/content-studio/SKILL.md`
- `skills/decision-brief/SKILL.md`
- `skills/project-planner/SKILL.md`
- `skills/research-brief/SKILL.md`
- `skills/trend-report/SKILL.md`
- `skills/visual-production/SKILL.md`
- `tests/test_coordination.py`
- `tests/test_workspace.py`
- `docs/CHANGES-FROM-V2.md` (this inventory)

## Removed baseline files

None.

## Packaging

The ZIP contains source, dependency manifests, examples, tests and documentation. It excludes installed dependencies, virtual environments, databases, model weights, credentials and test data. AgentMatrix is not vendored.
