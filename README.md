# Model Hub

A self-hosted AI workspace on rented GPUs. Two abliterated Qwen3.8-27B models run around the clock, and **Qwen Cowork** turns them into an agent that does real work: it plans, uses a shell, files, the web, parallel helper agents, image generation, your Android phone, and hands hard parts to Claude Code or Codex.

```text
You / invited friends ─▶ hub.handydandy.cc ── Cloudflare Access ─┐
Owner only ────────────▶ console.handydandy.cc ─ Access ─────────┤  Cloudflare Tunnel (outbound only)
Programs, phone bridge ─▶ api.handydandy.cc ── API / bridge key ─┤
                                                                 ▼
Chat box (Vast, 2× V100 32 GB)                                   Image box (Vast)
  Open WebUI (chat site) ─▶ Qwen Cowork pipe ─▶ controller :8787   Qwen-Image-2.1 service
  gateway :8080 ─▶ qwen-1 (GPU 0), qwen-2 (GPU 1), flex ───────────▶ img.handydandy.cc
```

## Using it

Pick **Qwen Cowork** in the chat site's model list. (**Qwen (chat)** is the plain model with no tools.)

- **Just say what you want done.** Each chat has its own folder on the box, so follow-up messages build on earlier files. Attached files land in `uploads/`.
- **Big projects:** ask for a plan first. Cowork keeps `plan.md` in the chat folder and does one phase per task. When a phase finishes, it can start the next one automatically in the same chat: up to 6 phases in a row for you, 2 for friends. After that, reply **continue**.
- **Claude Code / Codex:** say "use Claude Code for …" to hand work over. If a hand-off fails, the task stops right away and reports what happened. `/connections` shows their status.
- **If a task stops early** (time limit or failed hand-off), you still get a report. Reply **continue** and the next task gets a summary of what was done, so finished steps aren't redone.
- **If the page reloads or loses contact**, send **status** to follow the task that's still running.
- **Phone:** when the phone bridge is connected (see below), Cowork can look at and operate the phone and collect TikTok/Instagram posts. Taps that would post, send, comment, follow, like or buy are blocked unless your message approves them, for example "approved, post it".

Behind the scenes:

- **Long runs:** old tool output is shortened before every model call, so long runs don't overflow the context.
- **Both GPUs:** each task's lead agent goes to the less busy GPU and its helpers to the other. Multi-part work is handed to several helpers at once (`delegate_many`), spread over both GPUs. Routine steps, like reading files or results, use short thinking; planning and fixing errors use full thinking.
- **Disk:** new work is refused when disk space runs low, but deleting files still works.
- **Friends:** each invited friend has their own sandbox user, folders and memory, with a 5 GB allowance.

## Console (console.handydandy.cc)

| Page | What it's for |
|---|---|
| Now | GPU load, model queues, disk space, image box; running tasks (with Stop) and recent tasks with their full step log |
| Chats | Every chat folder with its size: browse and download files, delete old folders to free disk |
| Memory | What Cowork has saved about you and your projects: edit or forget |
| Phone | Bridge status, the setup command, a live screenshot, collected posts |
| People | Who used what (tasks, tokens), Claude Code and Codex status, recent hand-offs |
| System | Code version, disks, and progress when moving to a new box |

## Phone bridge

On the PC the phone is plugged into:

1. Install Android platform-tools (adb) and Python 3.
2. Turn on USB debugging on the phone and allow the prompt.
3. Download `bridge.py` from the console's Phone page and run the command shown there (`python bridge.py --url https://api.handydandy.cc --key phb_…`).

The bridge only makes outgoing HTTPS requests. The key only works for the bridge endpoints and can be replaced on the Phone page.

## Operating it

See [docs/OPERATIONS.md](docs/OPERATIONS.md): updating app code without losing data, shipping a new image, moving to a new box or a persistent volume, and rotating keys. [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) covers building everything from scratch, and [docs/THIRD-MODEL.md](docs/THIRD-MODEL.md) the image box.

## Tests

```sh
npm ci && npm run check && npm test                              # gateway
python -m unittest discover -s tests -p 'test_*.py'              # controller, Cowork, pipe, console, migration
```
