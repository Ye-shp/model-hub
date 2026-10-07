# GPU rental image: two abliterated Qwen3.8 servers, the gateway, Open WebUI, the agent controller
# (with Qwen Cowork and its sandbox, Claude Code and Codex) and the tunnel.
# Built by GitHub Actions (.github/workflows/image.yml); no local Docker needed.
# Every base is pinned to an exact digest so rebuilds are repeatable.

# Node runtime and the gateway's one dependency (fastify, pure JavaScript).
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
# Claude Code and Codex, for Cowork's hand-offs. They run on this box as the sandbox user.
RUN npm install -g --prefix /opt/cli --no-audit --no-fund @anthropic-ai/claude-code@2.1.284 @openai/codex@0.158.0

# Official prebuilt llama.cpp server with CUDA 12.8 (release b11206, Ubuntu 24.04).
# Nothing is compiled here, so the build takes minutes instead of an hour.
FROM ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:3e7673cce183a55f97a1bc3c80817f3c61452483c13bc088a6766388af4775fe

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates curl \
      python3-pip git jq ffmpeg imagemagick zip unzip pandoc procps less file libseccomp2 adb \
 && rm -rf /var/lib/apt/lists/*

# Cowork's workspace: what the agent can use from its shell (as the unprivileged users below).
RUN pip3 install --no-cache-dir --break-system-packages pandas numpy matplotlib openpyxl python-docx python-pptx \
      reportlab pypdf pillow requests beautifulsoup4 lxml yt-dlp \
 && useradd --uid 1500 --no-create-home --shell /bin/bash cowork \
 && useradd --uid 1501 --no-create-home --shell /bin/bash guest

# cloudflared 2026.9.3, checksum-verified.
RUN curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/download/2026.9.3/cloudflared-linux-amd64.deb \
 && echo "bc073ef293d504cf5ac533bd0aa1c824ef6b4f358765ccaa6628a8a95cacb4b7  /tmp/cloudflared.deb" | sha256sum -c - \
 && dpkg -i /tmp/cloudflared.deb && rm /tmp/cloudflared.deb

# Open WebUI 0.11.4 with CPU-only PyTorch (the GPUs are reserved for Qwen).
RUN python3 -m venv /opt/openwebui \
 && /opt/openwebui/bin/pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.14.0 \
 && /opt/openwebui/bin/pip install --no-cache-dir open-webui==0.11.4

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=node /opt/cli /opt/cli
WORKDIR /opt/hub
COPY --from=node /build/node_modules ./node_modules
COPY package.json ./
COPY gateway/ ./gateway/
COPY deploy/ ./deploy/
COPY agents/requirements.txt /tmp/agent-requirements.txt
RUN python3 -m venv /opt/agents \
 && /opt/agents/bin/pip install --no-cache-dir -r /tmp/agent-requirements.txt
COPY agents/ ./agents/
COPY console/ ./console/
COPY skills/ ./skills/
COPY integrations/ ./integrations/
COPY tools/tor/ ./tools/tor/
COPY tools/automation/ ./tools/automation/

# llama-server finds its own libraries in /app only via this path (or when started from /app).
ENV DATA_DIR=/workspace/data MODEL_DIR=/workspace/models LD_LIBRARY_PATH=/app:/usr/local/cuda/lib64
# First boot downloads ~17.5 GB of weights, so allow a long start period.
HEALTHCHECK --interval=60s --timeout=15s --start-period=60m CMD ["/usr/local/bin/node", "/opt/hub/deploy/health.mjs"]
ENTRYPOINT ["/usr/local/bin/node", "/opt/hub/deploy/supervise.mjs"]
