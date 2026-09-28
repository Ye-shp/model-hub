# Use the team from Open WebUI

Open WebUI is the main interface. Keep your existing direct `qwen`, `qwen-1` and `qwen-2` model connections for ordinary chat and vision. The team Pipe adds durable jobs alongside them.

This integration follows Open WebUI's documented [Pipe Functions interface](https://docs.openwebui.com/features/extensibility/plugin/functions/pipe/). It has been tested against the local controller using mocked HTTP/ASGI calls, but has not been installed in your running Open WebUI instance.

## 1. Make the controller reachable from Open WebUI

The URL is resolved by the **Open WebUI server**, not your browser. `127.0.0.1:8787` works when both run in the same container/host, as supported by the v3 deployment supervisor. Enable that CPU controller on a future v3 deployment with `ENABLE_AGENT_CONSOLE=true`.

For local development you can run both applications locally. If Open WebUI is on Vast and the controller is on your laptop, localhost points to Vast: use a deliberate private connection such as SSH port forwarding, or move the controller and its data onto the rental. Do not expose your laptop's unauthenticated ports or assume the browser bridges them. The supplied controller binds only to loopback.

You do **not** need a public operations-panel hostname when Open WebUI and the controller are colocated. A separately published owner panel is optional.

## 2. Install the Pipe as an administrator

1. Open Open WebUI's administrator Functions area and create a new Pipe Function.
2. Paste the complete contents of `integrations/openwebui_pipe.py`, save it and enable it.
3. Open its Valves/settings and enter the values below. Interface labels may differ between Open WebUI releases.
4. In a new chat, select **Hub · Research team** from the model selector.

| Valve | Initial value |
|---|---|
| `CONTROLLER_URL` | `http://127.0.0.1:8787` when colocated |
| `OWNER_KEY` | The controller's private owner key |
| `PROJECT_ID` | `default`, or the exact ID of a project you created |
| `PROFILE` | `fast` for the first trial, then `balanced` |
| `ALLOWED_EMAILS` | Leave empty for administrators only |
| `ALLOW_FRONTIER` | `false` initially |
| `ALLOW_IMAGES` | `false` until the flex service is configured |
| `WAIT_SECONDS` | `60`; work continues after the chat wait ends |

Local owner key location: `agents/data/console.key` (or your `HUB_DATA_DIR`). Rental controller location: `/workspace/data/agent-workspace/console.key`. If `CONSOLE_KEY` is explicitly set, use that value. This is separate from the gateway key. Store it only in the administrator's Valve configuration, never in a chat or prompt.

## 3. First task

Import a short source in the optional panel's Knowledge page, or use `agents/media.py import` locally with the same data directory. Try:

> Compare the approaches in our project sources. Identify three tradeoffs, cite document IDs and chunks, and save a recommendation. Use a short progress plan.

The Pipe sends your latest user message as the task brief; it does not silently import the whole chat, attachments or Open WebUI's own knowledge collection. Include important constraints in the brief or the controller's persistent project knowledge and memory. Direct model chat retains Open WebUI's normal conversation behavior.

## 4. Control a task

```text
/hub help
/hub tasks
/hub status FULL_TASK_ID
/hub cancel FULL_TASK_ID
/hub resume FULL_TASK_ID
/hub handoff FULL_TASK_ID
/hub review FULL_TASK_ID
```

If a job completes during the chat wait, its answer is returned. Otherwise, the chat gives its ID and leaves it running in the background. Closing or cancelling the chat stream does not cancel the durable job; use `/hub cancel`. A lost submission response is ambiguous: inspect `/hub tasks` before submitting again. Retrying or regenerating a normal task message can create another job.

The controller must remain running. Queued jobs persist across restarts; interrupted jobs require an explicit resume. Profiles limit each attempt to 3, 6 or 10 minutes. Resuming gives another attempt, while the per-job two-call frontier and two-image allowances remain consumed.

## 5. Invite people deliberately

Add only intended users' emails to `ALLOWED_EMAILS`, and use Open WebUI's Function/model visibility settings as appropriate. The Pipe checks server-supplied user role/email and verifies every job command against its configured project. Administrator access is always allowed.

All users allowed into one Pipe share its configured project, tasks, results and memory. A separate Pipe instance can point to a different project for another group. This is shared-project access, not private per-user storage. Keep the controller owner key and optional panel restricted to the owner.

## Compatibility checks on first install

Verify `/hub help`, submit one short task, inspect it with `/hub status`, and cancel a queued trial. Verify a non-invited user cannot use the Pipe. Automatic chat-title/tag/follow-up requests return a static label and never enqueue jobs. The Pipe uses asynchronous HTTP and streams empty keep-alive deltas while waiting; verify the behavior through your actual Open WebUI/Cloudflare version before relying on long waits.
