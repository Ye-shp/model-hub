> Building a hub from nothing. The running hub uses 2× V100 32 GB for chat and a separate box for images (see THIRD-MODEL.md); day-to-day changes are in OPERATIONS.md. Hardware and prices below are from the original setup.

# Model Hub

Two abliterated Qwen3.8-27B models running 24/7 on two rented RTX 3090s, with:

- **Vision**: both models read images, including phone screenshots.
- **Context**: three slots per GPU sharing the largest context that fits (measured 134K on a 3090, 262K on a V100 32GB). Set `MODEL_CONTEXT` only to cap it.
- **One API for everything**: an OpenAI-compatible endpoint with a separate key per friend, agent or program. Requests queue instead of failing when a model is busy.
- **A chat website for you and invited friends**: Open WebUI, behind a Cloudflare login.
- **Frontier models**: Claude, GPT or any OpenAI-compatible provider, behind a monthly call cap.
- **A third slot** for another chat model or an image model.
- **Laptop-side agents**: a lead agent with sub-agents, plus a collector that scrolls TikTok or Instagram on your USB Android phone and stores every post.

```
Friends / you ─▶ hub.YOUR-DOMAIN ─(Cloudflare login)─┐
Laptop agents / phone ─▶ api.YOUR-DOMAIN ─(API key)──┤ Cloudflare Tunnel (outbound only)
                                                     ▼
Vast rental: Open WebUI (Unix socket) ─▶ Gateway :8080 ─▶ qwen-1 (GPU 0)
                                     │         └─▶ qwen-2 (GPU 1)
                                     └─▶ optional third slot / Claude / GPT
```

**Budget:** calculate your actual rental rate × billable hours, plus storage, traffic and optional API services. Earlier quotes are not an all-in guarantee. Open WebUI remains the chat frontend; Vercel is not required by this deployment.

---

## 1. Put the code on GitHub

1. Unzip this folder somewhere, e.g. `Documents\model-hub`.
2. In GitHub Desktop: **File → Add local repository** → pick the folder → **create a repository** → commit → **Publish**, with **Keep this code private** ticked.

## 2. Build the GPU image (on GitHub, no Docker needed)

1. In your repository on github.com: **Actions → Build GPU image → Run workflow**.
2. When it finishes (roughly 10–20 minutes), open the run. Its summary shows an image reference like `ghcr.io/you/model-hub@sha256:…`. Copy it.
3. Let Vast download the private image:
   - github.com → Settings → Developer settings → **Personal access tokens (classic)** → new token.
   - Tick only **read:packages** and set an expiry date.
   - Save the token in your password manager.

## 3. Cloudflare: domain, login and tunnel

1. Add your domain to Cloudflare (free plan) and finish the nameserver change at your registrar.
2. **Zero Trust** → create your team; the free plan covers up to 50 users. Note the team name (`YOURTEAM.cloudflareaccess.com`).
3. **Access → Applications → Add → Self-hosted**:
   - Domain: `hub.YOUR-DOMAIN`, all paths.
   - Policy: **Allow**, *Emails*: your email. Add friends' emails here later.
   - Copy the application's **AUD tag**.
4. **Networks → Tunnels → Create tunnel** (Cloudflared). Copy the **tunnel token**. Add two public hostnames:

   | Hostname | Service | Extra setting |
   |---|---|---|
   | `hub.YOUR-DOMAIN` | `unix:/run/hub/webui.sock` | Additional settings → Access → **Protect with Access** on, with your team name and the AUD tag from step 3 |
   | `api.YOUR-DOMAIN` | `http://127.0.0.1:8080` | none; every request needs an API key |

   Keep the hub origin on the root-only Unix socket. Open WebUI trusts the email header supplied by
   Cloudflare Access; a loopback TCP origin would also be reachable by unprivileged Cowork shells.
   `deploy/webui_serve.py` uses HTTP polling for Socket.IO because cloudflared's HTTP/2 tunnel
   cannot forward WebSocket upgrades to a Unix-socket origin reliably.

## 4. Make your secrets

On your laptop, with [Node.js 22+](https://nodejs.org) installed, run this twice. Save both values in your password manager:

```
node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"
```

One value is your `ADMIN_KEY`, the other is `MODEL_API_KEY`.

## 5. Rent the GPUs on Vast.ai

1. **Templates → New**:
   - Image: the reference from step 2.
   - Docker login: server `ghcr.io`, your GitHub username, the read:packages token.
   - Launch mode: **Docker ENTRYPOINT**.
   - Disk: **60 GB**.
   - Environment: every line of `.env.example`. At minimum fill `ADMIN_KEY`, `MODEL_API_KEY`, `TUNNEL_TOKEN` and `WEBUI_URL=https://hub.YOUR-DOMAIN`.
   - Do not open any ports; the tunnel connects outward.
2. Search offers:
   - **2× RTX 3090**, **verified**, **on-demand**.
   - At least 48 GB system RAM.
   - A driver that supports **CUDA 12.8** (driver 570 or newer).
   - Aim for about **$0.32/hour or less**, including storage.
3. Rent it and open **Logs**. First boot downloads about 17.5 GB (model plus vision encoder), checks it, then starts everything. Look for:
   - Two `llama-server` lines reporting the context size (`n_ctx`) they chose.
   - `Gateway listening`.
   - Open WebUI starting.
   - The tunnel connecting.
4. Stopping the instance keeps the disk, so the next start is fast. Destroying it deletes everything, including accounts and chats.

If a model reports out-of-memory errors, reduce context to 16384 and inspect the logs. Back up persistent data before changing an instance; do not destroy it as a troubleshooting shortcut.

## 6. Sign in and invite friends

1. **You first:** open `https://hub.YOUR-DOMAIN`, enter the email code Cloudflare sends you, and you land in Open WebUI as **admin**. Do this before inviting anyone: the first account created becomes admin.
2. **Friends:**
   - Add their email to the Access policy (step 3.3). They sign in the same way.
   - They wait as *pending* until you approve them under **Admin Panel → Users**.
   - To remove someone, delete their email from the policy, revoke their session under **Zero Trust → My Team → Users**, and deactivate them in Open WebUI.
3. **Models on the website:** `qwen` (whichever resident is free), `qwen-1`, `qwen-2`, plus the third slot if configured. Frontier models appear only if you set `WEBUI_FRONTIER=true`. You can hide models per user under **Admin Panel → Models**.

## 7. API keys for agents, apps and friends' tools

From the project folder on your laptop (Windows Command Prompt):

```
set HUB_URL=https://api.YOUR-DOMAIN
set ADMIN_KEY=your-admin-key
node gateway\keys.js add laptop-agents --frontier
node gateway\keys.js list
node gateway\keys.js remove laptop-agents
```

Each key is printed once. Leave out `--frontier` for keys that must not spend money on Claude or GPT.

Any OpenAI-compatible tool works with base URL `https://api.YOUR-DOMAIN/v1`, a key, and one of these model IDs:

| Model ID | What it is |
|---|---|
| `qwen` | Whichever resident Qwen is least busy |
| `qwen-1`, `qwen-2` | A specific resident Qwen |
| `flex` | The third slot, if configured |
| `anthropic/<model>`, `openai/<model>`, `other/<model>` | Frontier models you listed in the rental settings |

Options you can add to a request:
- **Thinking depth:** `"reasoning_effort": "low" | "medium" | "xhigh"`. To turn thinking off, send `"chat_template_kwargs": {"enable_thinking": false}`.
- **Images:** send them inline as `data:image/...;base64,...`. Web links to images are refused for security.
- **Queue status:** `GET /v1/status` shows how busy each model is.

## 8. Phone and agents on your laptop

**One-time setup:**
1. Install [Python 3.12](https://www.python.org/downloads/) (tick "Add to PATH").
2. Install Android [platform-tools](https://developer.android.com/tools/releases/platform-tools) and unzip it, e.g. to `C:\platform-tools`.
3. On the phone:
   - Settings → About phone → tap **Build number** 7 times.
   - Developer options → **USB debugging** on.
   - Plug the phone in and accept the prompt.
   - Sign in to TikTok and Instagram. Start with a spare account: automating these apps can get an account restricted.
4. Set up the agents folder:

```
cd agents
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

5. Edit `agents\.env`:
   - `HUB_URL=https://api.YOUR-DOMAIN/v1`
   - `HUB_KEY=` the `laptop-agents` key from step 7.
   - `ADB=C:\platform-tools\adb.exe`
   - Optionally `FRONTIER_MODEL=anthropic/<model>`.

**Collect posts** (the phone scrolls; Qwen reads each screen through its vision encoder):
```
.venv\Scripts\python collect.py tiktok --posts 30
.venv\Scripts\python collect.py instagram --posts 30
```
Posts go into `agents\data\hub.db` (creator, caption, on-screen text, topic, hashtags, sound, likes/comments/shares), with a screenshot of each.

**Queue work for the v3 team** (start `python console.py` to process tasks):
```
.venv\Scripts\python crew.py "What's trending in today's TikTok posts? Draft 3 original scripts."
.venv\Scripts\python crew.py --show-drafts
```
`phone.py` also has `push_to_gallery()`, which copies a finished video onto the phone so you can post it from the app.

## 9. Check it before relying on it

- `https://api.YOUR-DOMAIN/v1/models` without a key returns **401**.
- `https://hub.YOUR-DOMAIN` asks for the Cloudflare email code when you are signed out.
- **Admin Panel → Users** shows a friend as *pending* until you approve them.
- Send a screenshot in the website chat and ask what it shows (tests vision).
- Run `collect.py` for five posts and compare the stored numbers against the phone.
- Check the Vast bill after the first hour and after the first day.

## Not built yet

- **Automatic posting:** it needs a video renderer (scripts → finished videos) and a posting flow per app.
- Full video understanding and platform publishing remain outside this version. v3 adds project memory and retrieval; see the main README.
- **Automatic provisioning of the third slot.**

## Project layout

| Path | What it is |
|---|---|
| `gateway/` | OpenAI-compatible gateway (Node, Fastify). Tests: `npm test` |
| `deploy/` | Dockerfile, pinned model manifest, downloader, process supervisor, health check |
| `agents/` | Laptop-side Python: `collect.py` (phone), `crew.py` (agent team), `phone.py` (adb), `store.py` (SQLite) |
| `.github/workflows/image.yml` | Builds and publishes the GPU image |
