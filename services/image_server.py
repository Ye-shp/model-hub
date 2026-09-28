"""Optional FLUX.2 Klein endpoint. Run on a separate GPU/service, never started by the hub."""
from __future__ import annotations

import base64
import hmac
import io
import os
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

MODEL = "black-forest-labs/FLUX.2-klein-4B"
REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
SIZES = {"512x512", "768x768", "1024x1024", "1024x1536", "1536x1024"}


class ImageRequest(BaseModel):
    model: str = MODEL
    prompt: str = Field(min_length=1, max_length=8000)
    size: str = "1024x1024"
    n: int = 1
    response_format: str = "b64_json"


class FluxRenderer:
    def __init__(self):
        import torch
        from diffusers import Flux2KleinPipeline
        if not torch.cuda.is_available():
            raise RuntimeError("A CUDA GPU is required for this image service")
        free, _ = torch.cuda.mem_get_info()
        if free < 14 * 1024**3:
            raise RuntimeError("Less than 14 GiB free VRAM. Use a separate GPU; do not displace the two resident models.")
        self.torch = torch
        self.pipeline = Flux2KleinPipeline.from_pretrained(MODEL, revision=REVISION, torch_dtype=torch.bfloat16)
        self.pipeline.enable_model_cpu_offload()

    def __call__(self, prompt, width, height):
        return self.pipeline(prompt=prompt, width=width, height=height, guidance_scale=1.0,
                             num_inference_steps=4).images[0]


def create_app(renderer, key: str) -> FastAPI:
    if len(key) < 32:
        raise ValueError("IMAGE_API_KEY must contain at least 32 characters")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()

    def auth(value):
        if not hmac.compare_digest((value or "").encode(), ("Bearer " + key).encode()):
            raise HTTPException(401, "Invalid image service key")

    @app.get("/health")
    def health():
        return {"ok": True, "model": MODEL, "busy": lock.locked()}

    @app.get("/v1/models")
    def models(authorization: str = Header(default="")):
        auth(authorization)
        return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "local"}]}

    @app.post("/v1/images/generations")
    def generate(body: ImageRequest, authorization: str = Header(default="")):
        auth(authorization)
        if body.model not in {MODEL, "flex"} or body.size not in SIZES or body.n != 1 or body.response_format != "b64_json":
            raise HTTPException(400, "Use the configured FLUX model, a supported size, n=1 and b64_json")
        if not lock.acquire(blocking=False):
            raise HTTPException(503, "Image worker is busy")
        try:
            width, height = map(int, body.size.split("x"))
            output = io.BytesIO()
            renderer(body.prompt, width, height).save(output, format="PNG")
            return {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}]}
        finally:
            lock.release()
    return app


if __name__ == "__main__":
    import uvicorn
    key = os.environ.get("IMAGE_API_KEY", "")
    if len(key) < 32:
        raise SystemExit("Set IMAGE_API_KEY before starting this service")
    # Load before the endpoint becomes healthy; model downloads never occur inside a request.
    app = create_app(FluxRenderer(), key)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("IMAGE_PORT", "8090")))
