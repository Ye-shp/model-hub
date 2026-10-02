"""Start Open WebUI on a Unix socket instead of a TCP port.

The chat site trusts the sign-in email header that Cloudflare Access adds, so anything that can open a
connection to it could claim to be anyone. A socket in a root-only folder can be reached by the tunnel
(root) but not by the Cowork agent's shell, which runs as an unprivileged user on the same box.
Socket.IO uses HTTP polling because cloudflared's HTTP/2 tunnel cannot forward WebSocket upgrades to
this Unix-socket origin.
"""
import os
import sys

os.environ["FROM_INIT_PY"] = "true"
# This runs before Open WebUI imports its environment settings. Keep the root-only socket: binding
# WebUI to loopback TCP would let a Cowork shell forge Cloudflare's trusted email header.
os.environ["ENABLE_WEBSOCKET_SUPPORT"] = "false"
socket_path = sys.argv[1]
os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
os.chmod(os.path.dirname(socket_path), 0o700)
if os.path.exists(socket_path):
    os.unlink(socket_path)

import uvicorn  # noqa: E402
import open_webui.main  # noqa: E402,F401
from open_webui.env import UVICORN_WS_PER_MESSAGE_DEFLATE  # noqa: E402

uvicorn.run("open_webui.main:app", uds=socket_path, forwarded_allow_ips="*", ws_per_message_deflate=UVICORN_WS_PER_MESSAGE_DEFLATE)
