# Higgsfield in Cowork

Connect the owner's existing Higgsfield account through the official MCP server,
`https://mcp.higgsfield.ai/mcp`. This uses the account's Higgsfield plan credits;
it is separate from the developer API and does not need an API key.

## Connect

1. In an owner Qwen Cowork chat, send `/connect higgsfield`.
2. Open the returned sign-in link and authorize **Model Hub Cowork** in Higgsfield.
3. Return to Cowork and send `/connections`. Once connected, a new task gets
   compact tools to discover and invoke the relevant Higgsfield tools. Full
   input schemas are read when needed, rather than included in every chat.

Ask Cowork to list available models or check your balance for a first read-only
check. For generation, specify the requested image, video, or audio and any
preferred model. Cowork inspects the provider's model/preset and price, saves
returned job IDs, waits for completion, and reports the result URLs. A remote
generation can continue after a local chat task stops; follow-ups should check
the saved job ID rather than submit the same request again.

`/connect higgsfield off` removes the Hub's saved connection and credentials.
Re-run the connection command if Higgsfield requires sign-in again. An unfinished
sign-in expires after ten minutes and must be restarted after a Hub restart.

## Deployment and credentials

Deploy the connector code through the Hub's existing code-update and restart
process. `CONSOLE_URL` must be the configured public HTTPS console address;
local development can use a loopback HTTP address. This address supplies the
fixed OAuth callback, `/api/connections/higgsfield/callback`; it is never built
from a caller's Host header.

Only an authenticated owner can start or disconnect a connection. The callback
accepts only a single-use, unexpired state from that owner-started flow. The MCP
SDK performs OAuth discovery, client registration, PKCE, issuer validation, and
token refresh. Tokens and client registration stay in the private persistent
Hub data directory, separate from the Cowork workspace and model context.
Guest tasks do not open the connection.

The connector exposes the remote tools it discovers rather than embedding a
fixed model catalog. It preserves structured job IDs and media URLs for Cowork,
which does not render Higgsfield's interactive widgets. Local workspace paths
are not files on Higgsfield: reference files need a supported upload and
confirmation flow before being used. Connecting does not generate or publish
anything.

Official references: [MCP connection](https://higgsfield.ai/creator-hub/help-center/integrations/how-do-i-connect-higgsfield-to-ai-agent),
[self-hosted OAuth/DCR integration](https://higgsfield.ai/blog/automate-ai-video-n8n-make).
