# Model Hub

A self-hosted AI workspace on rented GPUs. Two abliterated Qwen3.8-27B models run around the clock, and **Qwen Cowork** turns them into an agent that does real work: it plans, uses a shell, files, the web, parallel helper agents, image generation, your Android phone, and hands hard parts to Claude Code or Codex.

```text
You (owner only) ──────▶ hub.handydandy.cc ── Cloudflare Access ─┐
Owner only ────────────▶ console.handydandy.cc ─ Access ─────────┤  Cloudflare Tunnel (outbound only)
Programs, phone bridge ─▶ api.handydandy.cc ── API / bridge key ─┤
                                                                 ▼
Chat box (Vast, 2× V100 32 GB)                                   Image box (Vast)
  Open WebUI (chat site) ─▶ Qwen Cowork pipe ─▶ controller :8787   Qwen-Image-2.1 service
  gateway :8080 ─▶ qwen-1 (GPU 0), qwen-2 (GPU 1), flex ───────────▶ img.handydandy.cc
```

## Using it

Pick **Qwen Cowork** in the chat site's model list for projects, or **Qwen (chat)** for conversation: chat has the same tools (web, shell, files, images, helpers, Claude Code) and uses them when they help, but answers directly instead of planning. Both are for the owner only.

- **Just say what you want done.** Each chat has its own folder on the box, so follow-up messages build on earlier files. Attached files land in `uploads/`.
- **Thoughts and memory:** both modes show Qwen's streamed thoughts in Open WebUI's expandable Thinking panel. Saved preferences, decisions and checkpoints stay in their originating chat; collected posts and researched playbooks remain a shared library. A new question does not automatically resume an unrelated stopped task.
- **Questions first:** for a new request that leaves real choices open (audience, platform, format, scope, tone), Cowork asks up to three questions before starting and waits up to 30 minutes for the answer; with no answer it starts on its own assumptions and says which. Say "just do it" to skip the questions.
- **Big projects:** ask for a plan first. Cowork keeps `plan.md` in the chat folder and does one phase per task. When a phase finishes, it can start the next one automatically in the same chat: up to 6 phases in a row. After that, reply **continue**.
- **Claude Code / Codex:** say "use Claude for …" (or "have Claude …", "ask Claude to …") and the request goes straight to Claude Code; Qwen then checks the files, shares them and does any part you kept for it. Qwen also hands substantial coding and long-form writing to Claude by itself. The task clock pauses while Claude works. If a hand-off fails, the task stops right away and reports what happened. `/connections` shows their status. Telegram tasks can use Claude too.
- **Time limits:** a Cowork task runs up to 2 hours (Qwen (chat): 20 minutes). When it hits the limit or runs out of steps, you get a report and the next part starts by itself in the same chat with a summary of what was done, up to 4 times in a row (`COWORK_AUTO_CONTINUE`). After that, or after a failed hand-off, reply **continue**.
- **If the page reloads or loses contact**, send **status** to follow the task that's still running.
- **Research and video tools (free):** Cowork decides when to use these.
  - `analyze_video` breaks down a TikTok, Reel, Short or X video (link or upload): hook, beats, CTA, cut rate, on-screen text, voiceover, sound, AI-tool fingerprint.
  - `trend_research` covers the last 30 days on Reddit, X, YouTube, Hacker News, Polymarket, GitHub and Bluesky (the vendored [last30days](https://github.com/mvanhorn/last30days-skill) engine).
  - `google_trends`, `x_search`, `x_trends`, `x_user`, `instagram_profile` and `tiktok_profile` are targeted lookups.

  The tools install themselves into `/workspace/tools` on first start. TikTok and Instagram often block the rented server, so TikTok research leans on the phone.
- **Accounts:** connect free accounts from any chat as the owner: `/connect x USERNAME auth_token=… ct0=…` (enables X search and posting), `/connect instagram USER_ID TOKEN` (Instagram API posting), `/connect bluesky HANDLE APP_PASSWORD`, `/connect github TOKEN`. `/connections` shows everything. Sign-in messages are never passed to the model.
- **Higgsfield:** `/connect higgsfield` opens a sign-in link for your existing account. Cowork discovers Higgsfield's image, video, audio, and utility tools for your next task; `/connections` shows its status. See [Higgsfield setup](docs/HIGGSFIELD.md).
- **Posting:** Cowork drafts posts with `draft_post`. Nothing is published until you reply `approve post N`.
  - X posts through your connected account.
  - Instagram reels and stories go through the official API. Image posts need the next hub image.
  - TikTok goes through the phone: the media lands in its gallery and Cowork posts it in the app.
- **Phone:** when the phone bridge is connected (see below), Cowork can look at and operate the phone and collect TikTok/Instagram posts. Taps that would post, send, comment, follow, like or buy are blocked unless your message approves them, for example "approved, post it".

Behind the scenes:

- **Long runs:** old tool output is shortened before every model call, so long runs don't overflow the context.
- **Both GPUs:** each task's lead agent goes to the less busy GPU (alternating when both are idle) and its helpers to the other. Independent parts go to background helpers (`start_helpers`) that work on the second GPU while the lead keeps going; `delegate_many` runs several and waits. The kickoff questions are decided on the helper GPU. Routine steps, like reading files or results, use short thinking; planning and fixing errors use full thinking.
- **Disk:** new work is refused when disk space runs low, but deleting files still works.

## Console (console.handydandy.cc)

| Page | What it's for |
|---|---|
| Now | GPU load, model queues, disk space, image box; running tasks (with Stop) and recent tasks with their full step log |
| Chats | Every chat folder with its size: browse and download files, delete old folders to free disk |
| Memory | What Cowork has saved about you and your projects: edit or forget |
| Audience | Compare drafts, record published posts and delayed counts, review reach scores and export preference examples |
| Phone | Bridge status, the setup command, a live screenshot, collected posts |
| People | Who used what (tasks, tokens), Claude Code and Codex status, recent hand-offs |
| System | Code version, disks, and progress when moving to a new box |

See [Audience learning](docs/AUDIENCE-LEARNING.md) for a first TikTok or Instagram
experiment. Delayed checks run independently of chat timeouts. The first score
focuses on reach, and preference exports stay separate from model training.

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
