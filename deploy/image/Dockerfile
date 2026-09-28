# Image-generation box (third slot): Qwen-Image-2.1 + the abliterated Heretic text encoder.
# Runs on its own Ampere-or-newer GPU (RTX 3090 or better) with its own Cloudflare tunnel.
# Built by GitHub Actions alongside the main image; weights download on first boot (~33 GB).
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/*

# cloudflared 2026.9.3, checksum-verified (same release as the main box).
RUN curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/download/2026.9.3/cloudflared-linux-amd64.deb \
 && echo "bc073ef293d504cf5ac533bd0aa1c824ef6b4f358765ccaa6628a8a95cacb4b7  /tmp/cloudflared.deb" | sha256sum -c - \
 && dpkg -i /tmp/cloudflared.deb && rm /tmp/cloudflared.deb

# PyTorch 2.14 + torchvision (the Qwen3-VL processor needs it) with its own CUDA 13.0 runtime (needs a host driver >= 580; Ampere or newer), then the pinned stack.
COPY services/requirements-image.txt /tmp/requirements-image.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu130 torch==2.14.0 torchvision==0.29.0 \
 && pip install --no-cache-dir -r /tmp/requirements-image.txt

WORKDIR /opt/image
COPY services/image_server.py ./
COPY deploy/image/start.sh ./
ENV IMAGE_MODEL_DIR=/workspace/image-models HF_HOME=/workspace/hf-cache PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=10s --start-period=60m CMD ["curl", "-fsS", "http://127.0.0.1:8090/health"]
ENTRYPOINT ["/bin/sh", "/opt/image/start.sh"]
