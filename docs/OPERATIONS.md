# Operating the hub

Instance IDs, keys and template hashes are kept privately by the owner, not in this repository.

## Where data lives

| Path | Contents | Survives |
|---|---|---|
| `DATA_DIR` (default `/workspace/data`) | chat site database and uploads, gateway keys, controller database (jobs, memory, collected posts), Claude token, bridge key, staged app code | reboot; a recycle only if `DATA_DIR` is on a Vast volume |
| `COWORK_ROOT` (default `/workspace/cowork`) | every chat's folder, sandbox home folders (pip/npm installs, Codex login) | same as above |
| `MODEL_DIR` (`/workspace/models`) | model weights (~18 GB, re-downloaded in minutes) | reboot |

On a persistent **Vast volume**, set `DATA_DIR=/persist/data` and `COWORK_ROOT=/persist/cowork` with the volume mounted at `/persist`. The data then survives recycles and image changes. Weights stay on the container disk.

## 1. App-code change (no data loss, about 2 minutes)

Covers `agents/`, `integrations/`, `skills/`, `console/` and `deploy/webui_*`.

The chat-memory update adds a conversation scope to saved notes on controller startup. Note IDs, contents and embeddings are retained. Historical notes with an unambiguous job/chat source return to that chat; uncertain notes remain visible in the console and are excluded from ordinary chat recall. Old versions already overwritten by the former shared-title rule cannot be recovered. Standard chat IDs keep their existing workspace folders; unusual IDs use a collision-resistant folder name. Take the normal controller-database backup before deploying a schema update.

Tool-enabled chat needs a saved conversation. Open WebUI's temporary chats use a browser-wide socket ID, so the Pipe asks the user to turn off Temporary Chat or save the conversation instead of sharing its files and memory with other temporary chats.

The Tor reader (`read_onion_page`) also arrives this way: on an image older than the Tor change the controller installs and runs Tor itself (`agents/tor_service.py`, state on the System page), so no recycle is needed for it.

1. Push to `main`.
2. In any Qwen Cowork chat, as the owner: `/update-code <full commit sha>`.
3. Restart the instance (`vastai reboot instance <id>`), not recycle. Running tasks are marked interrupted; reply **continue** in their chats.

## 2. New image (gateway, supervisor, Dockerfile, Python packages)

1. Push to `main`. GitHub Actions runs the tests and publishes `ghcr.io/ye-shp/model-hub:<sha>` (about 20–30 minutes).
2. Point the template at the new tag: `vastai update template <hash> --image ghcr.io/ye-shp/model-hub --image_tag <sha> --env "<env line>" --login "<docker login>" --disk_space <GB>`. It prints a new hash.
3. `vastai update instance <id> --template_hash_id <new hash>`, then `vastai recycle instance <id>`.

A recycle wipes the container disk. It is only safe when `DATA_DIR` and `COWORK_ROOT` are on a volume. Otherwise, move the data first with section 3.

## 3. Moving to a new box (bigger disk, volume, other machine)

The old box streams everything to the new one: databases are copied consistently while running, tarred, and sent in AES-GCM-sealed frames.

1. Pick a random `RESTORE_KEY` (32+ characters).
2. Create the new instance with the new image and the usual env, plus:
   - `RESTORE_KEY=<key>` and `RESTORE_PORT=9000`, and expose `-p 9000:9000`.
   - For a volume: `--create-volume <volume offer> --volume-size <GB> --mount-path /persist`, plus `DATA_DIR=/persist/data COWORK_ROOT=/persist/cowork`.

   The new box waits for the data before creating anything, so its tunnel stays down until the data arrives.
3. Find the new box's public IP and the port mapped to 9000 (`vastai show instance <new id> --raw`: `public_ipaddr`, `ports`).
4. Make sure no task is running. Then, signed in to the console (the Access session is enough), run in the browser console:
   `fetch('/api/admin/migrate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({target:'http://IP:PORT',key:'RESTORE_KEY'})})`
   The System page shows progress.
5. When it says done, stop the old instance (`vastai stop instance <old id>`). The new box finishes booting and its tunnel takes over the same hostnames.
6. Check the chat site, the console and a small Cowork task. Keep the old instance stopped for a day, then destroy it.

## 4. Settings worth knowing (template env)

| Variable | Default | Meaning |
|---|---|---|
| `TUNNEL_PROTOCOL` | `http2` | cloudflared transport. QUIC kept timing out on the V100 host |
| `COWORK_MIN_FREE_GB` | `4` | refuse new work below this much free disk (deleting still works) |
| `COWORK_FRIEND_QUOTA_GB` | `5` | each invited friend's folder allowance |
| `COWORK_MAX_PHASES` / `COWORK_FRIEND_MAX_PHASES` | `6` / `2` | automatic project phases in a row |
| `COWORK_CONTEXT_SOFT_CHARS` / `_HARD_CHARS` | `120000` / `260000` | when old tool output starts being shortened |
| `COWORK_LEAD_MODEL` / `COWORK_HELPER_MODEL` | `auto` | pin the lead or helpers to `qwen-1`/`qwen-2` instead of balancing |
| `COWORK_MAX_PARALLEL_HELPERS` | `4` | helpers one `delegate_many` call may start |
| `TOOLS_DIR` | next to `COWORK_ROOT` (`/workspace/tools`) | research/video tools environment, installed on first start (about 1–2 GB) |
| `BRIDGE_PUBLIC_URL` | derived from `WEBUI_URL` (`hub.` → `api.`) | the address shown in the phone setup command |

## 5. Rotating keys

- **Console owner key / gateway admin key / llama key / image key:** new 32+ character values in the template, then restart (or recycle when on a volume).
- **Claude Code:** `claude setup-token` on your computer, then `/connect claude <token>` in a Cowork chat.
- **Phone bridge:** New key on the console's Phone page, then restart `bridge.py` with it.
- **Vast API key and the GitHub token for ghcr pulls:** create new ones in their dashboards, update the template's docker login, and delete the old ones.

## 6. Telegram

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token it gives you.
2. In any Qwen Cowork chat, as the owner: `/connect telegram <token>`. The reply shows a pairing code.
3. Open the bot and send `/start <code>`. From then on only that Telegram account can use it; strangers get no answer.

Send it TikToks, Reels, X posts or threads, Reddit threads, YouTube links or a video file (up to 20 MB), or a
creator's profile link (tiktok.com/@name, instagram.com/name, youtube.com/@name, x.com/name) to analyse the whole account
(`study_profile`: recent posts' numbers, outliers, deep dives into the best posts, one saved playbook; 5-15 minutes). It replies at
once with the platform, then Cowork studies the post and its top comments (`study_link`) and saves anything useful about
UGC, go-to-market and growth to the owner project's knowledge base, which every Cowork chat searches. Anything else is a
normal Cowork request. `/new` starts a fresh conversation, `/status` shows what's running, `/connect telegram off`
disconnects. The bot polls Telegram from the box, so no tunnel route is needed. Comments: TikTok, Reddit and X (with
`/connect x`) work without other sign-ins; Instagram only shows comments to signed-in accounts, so Reels are studied
from the video and caption alone.

## 7. Audience experiments

The console's **Audience** page records drafts, publication and delayed analytics.
See [AUDIENCE-LEARNING.md](AUDIENCE-LEARNING.md) for creating a comparable
experiment, entering counts, reading scores and exporting preference examples.

The audience collector runs separately from chat tasks. Its checkpoint queue,
retry state, observations and experiments live in the controller database under
`DATA_DIR`, so normal backups and migration must retain that database. A chat
timeout leaves the collector running. After a container restart, pending work is
recovered from the stored queue; collection pauses while the container is stopped.
Check the experiment's **Collection schedule** for retry errors after reconnecting
analytics credentials. A token that supports posting may still lack insight
permissions. Unsupported metrics are missing values, not zeroes.

TikTok's access tokens expire after 24 hours. An access-token-only connection
needs manual renewal; unattended 72-hour and seven-day checks additionally need
a valid refresh token, client key and client secret. The collector can renew
access when these are connected. Reconnect if refresh access is revoked or
expired, and review the collection schedule for missed checkpoints.

Connect in an owner Cowork chat:

```text
/connect tiktok <OAuth open_id> <access token> refresh_token=<refresh token> client_key=<client key> client_secret=<client secret>
```

The refresh fields must be supplied together. The app authorization needs
`video.list` and `user.info.basic`. Reconnecting with only the account ID and
access token removes the saved refresh configuration and requires manual renewal.

This feature does not train or promote a model. The first reward focuses on reach;
other engagement counts are diagnostics. Keep independent evaluation experiments
out of preference exports before planning any separate training work.

## 7. Questions mid-task, MCP servers and APIs

**Questions.** When a request is ambiguous or a decision is yours, Cowork asks (`ask_user`) and the task pauses. In the
chat site the reply ends with the question (options numbered); in Telegram it arrives with tap-to-answer buttons. Your
next message in that chat is the answer, not a new task. The task's time limit is paused while it waits; after the wait
(30 minutes by default, at most 4 hours) it carries on with its best assumption and says which one it made.

**MCP servers** (owner only, from a Cowork chat on the web; keys never go through Telegram or the model):

```text
/connect mcp notion https://mcp.notion.com/mcp bearer=<token>             remote, streamable HTTP (…/sse URLs use SSE)
/connect mcp memory stdio npx -y @modelcontextprotocol/server-memory     local command, runs as the sandbox user
/connect mcp github stdio npx -y @modelcontextprotocol/server-github env:GITHUB_PERSONAL_ACCESS_TOKEN=<token>
/connect mcp <name> off
```

The box connects once right away and lists the server's tools. Small catalogs are offered as `<name>__<tool>`
(for example `memory__create_entities`). Large catalogs, including Higgsfield's, stay in the controller: Qwen gets
compact tools to discover relevant capabilities, read an exact tool's complete schema in pages, and invoke it by its
original name. This keeps ordinary chats from processing the entire media-tool catalog before their first thought.
The account, available capabilities, and invocation checks are the same. A server that's down is skipped and named
in the task's instructions.

**HTTP APIs:**

```text
/connect api shop https://api.example.com/v1 bearer=<key> methods=GET,POST about="Orders and customers"
/connect api weather https://api.example.com query:appid=<key> about="Forecasts"
/connect api <name> off
```

Cowork calls them with `call_api(api, method, path, query_json, body_json)`. Stored headers and query values are added
on the box and hidden from responses; calls stay under the base URL, redirects aren't followed, and only the listed
methods (default GET, POST) are allowed. `/connections` lists everything connected (names and hosts, no keys).
