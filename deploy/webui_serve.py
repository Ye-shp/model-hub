"""Start Open WebUI on a Unix socket instead of a TCP port.

The chat site trusts the sign-in email header that Cloudflare Access adds, so anything that can open a
connection to it could claim to be anyone. A socket in a root-only folder can be reached by the tunnel
(root) but not by the Cowork agent's shell, which runs as an unprivileged user on the same box.
Same settings as `open-webui serve` otherwise.
"""
import os
import sys

os.environ["FROM_INIT_PY"] = "true"
socket_path = sys.argv[1]
os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
os.chmod(os.path.dirname(socket_path), 0o700)
if os.path.exists(socket_path):
    os.unlink(socket_path)

import uvicorn  # noqa: E402
import open_webui.main  # noqa: E402,F401
from open_webui.env import UVICORN_WS_PER_MESSAGE_DEFLATE  # noqa: E402

uvicorn.run("open_webui.main:app", uds=socket_path, forwarded_allow_ips="*", ws_per_message_deflate=UVICORN_WS_PER_MESSAGE_DEFLATE)
