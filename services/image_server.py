"""Image service for the third slot: Qwen-Image-2.1 with an abliterated ("Heretic") text encoder.

Runs on its own GPU box (an RTX 3090 or better; bf16 is required, so not a V100) and speaks the
OpenAI images API, so the hub gateway can use it as MODEL3_KIND=image. The weights are downloaded
once at pinned revisions; /health is public and shows download/loading progress, everything else
needs IMAGE_API_KEY.

License note: Qwen-Image-2.1 is released under the Qwen Research License (non-commercial use only).
The Heretic text encoder is Apache-2.0.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

MODEL_ID = "qwen-image-2.1"
PIPELINE = {"repo": "Qwen/Qwen-Image-2.1", "revision": "790c92633540aa0cb11d9abf19eb46d861714758",
            # Everything except the stock text encoder weights, which the Heretic encoder replaces.
            "allow": ["model_index.json", "processor/*", "scheduler/*", "transformer/*", "vae/*", "LICENSE"]}
TEXT_ENCODER = {"repo": "pottokao/Qwen-Image-2.1-Text-Encoder-Heretic", "revision": "047e54342fc4bfcd2addd54049db9c90bb74731e",
                "allow": ["config.json", "generation_config.json", "model-0000*-of-00004.safetensors",
                          "model.safetensors.index.json", "LICENSE", "NOTICE"]}
# width x height. The model is trained up to 2K; 1K sizes are the fast defaults.
SIZES = {
    "1024x1024", "1024x1536", "1536x1024", "1152x2048", "2048x1152",  # fast: square, 2:3, 3:2, 9:16, 16:9
    "2048x2048", "1536x2752", "2752x1536", "1696x2528", "2528x1696",  # native 2K
}
DEFAULT_STEPS = 40


class ImageRequest(BaseModel):
    model: str = MODEL_ID
    prompt: str = Field(min_length=1, max_length=8000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    size: str = "1024x1024"
    n: int = 1
    response_format: str = "b64_json"
    steps: int = Field(default=DEFAULT_STEPS, ge=8, le=60)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)


class Status:
    def __init__(self):
        self.phase, self.detail, self.error = "starting", "", None

    def set(self, phase, detail=""):
        self.phase, self.detail = phase, detail
        print(f"[image] {phase} {detail}".rstrip(), flush=True)


def download(models_dir: Path, status: Status) -> tuple[Path, Path]:
    from huggingface_hub import snapshot_download
    paths = []
    for name, spec in (("pipeline", PIPELINE), ("text encoder", TEXT_ENCODER)):
        status.set("downloading", f"{spec['repo']} ({name})")
        target = models_dir / spec["repo"].replace("/", "--") / spec["revision"]
        snapshot_download(spec["repo"], revision=spec["revision"], allow_patterns=spec["allow"], local_dir=target,
                          max_workers=8)
        paths.append(target)
    return paths[0], paths[1]


class QwenImageRenderer:
    def __init__(self, pipeline_dir: Path, encoder_dir: Path, status: Status):
        import torch
        from diffusers import QwenImage21Pipeline
        from transformers import Qwen3VLForConditionalGeneration
        if not torch.cuda.is_available():
            raise RuntimeError("A CUDA GPU is required for this image service")
        major, _ = torch.cuda.get_device_capability()
        if major < 8:
            raise RuntimeError("Qwen-Image-2.1 runs in bf16; use an Ampere or newer GPU (e.g. RTX 3090), not a V100")
        self.torch = torch
        status.set("loading", "text encoder (Heretic, abliterated)")
        encoder = Qwen3VLForConditionalGeneration.from_pretrained(encoder_dir, dtype=torch.bfloat16)
        status.set("loading", "diffusion transformer and VAE")
        self.pipeline = QwenImage21Pipeline.from_pretrained(pipeline_dir, text_encoder=encoder, dtype=torch.bfloat16)
        total = torch.cuda.get_device_properties(0).total_memory
        if total >= 40 * 1024**3:
            self.pipeline.to("cuda")
        else:
            # 24 GB cards cannot hold the 17.5 GB encoder and the 14 GB transformer at once; each stage
            # moves to the GPU only while it runs.
            self.pipeline.enable_model_cpu_offload()
        self.offload = total < 40 * 1024**3
        # Decode large (2K) images in tiles: a full-frame 2K decode needs ~5 GB more than a 24 GB card has left.
        self.pipeline.vae.enable_tiling()

    def recover(self):
        """After a failure (e.g. out of memory) the offload hooks can leave parts on the wrong device,
        which breaks every later image. Put everything back where it belongs."""
        torch = self.torch
        if self.offload:
            self.pipeline.remove_all_hooks()
            for component in self.pipeline.components.values():
                if isinstance(component, torch.nn.Module):
                    component.to("cpu")
            torch.cuda.empty_cache()
            self.pipeline.enable_model_cpu_offload()
        else:
            torch.cuda.empty_cache()

    def __call__(self, prompt, width, height, steps=DEFAULT_STEPS, seed=None, negative_prompt=None):
        generator = self.torch.Generator("cpu").manual_seed(seed) if seed is not None else None
        extra = {"negative_prompt": negative_prompt, "true_cfg_scale": 4.0} if negative_prompt else {}
        try:
            return self.pipeline(prompt=prompt, width=width, height=height, num_inference_steps=steps,
                                 generator=generator, **extra).images[0]
        except Exception:
            try:
                self.recover()
            except Exception as error:  # cannot trust the GPU state any more: restart (start.sh relaunches)
                print(f"[image] recovery failed ({type(error).__name__}: {error}); restarting", flush=True)
                os._exit(1)
            raise


def create_app(renderer, key: str, status: Status | None = None) -> FastAPI:
    """renderer: a callable, or None while the model is still downloading/loading (set app.state.renderer later)."""
    if len(key) < 32:
        raise ValueError("IMAGE_API_KEY must contain at least 32 characters")
    status = status or Status()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.renderer = renderer
    if renderer is not None:
        status.set("ready")
    lock = threading.Lock()

    def auth(value):
        if not hmac.compare_digest((value or "").encode(), ("Bearer " + key).encode()):
            raise HTTPException(401, "Invalid image service key")

    @app.get("/health")
    def health():
        return {"ok": status.error is None, "model": MODEL_ID, "phase": status.phase, "detail": status.detail,
                "error": status.error, "busy": lock.locked()}

    @app.get("/v1/models")
    def models(authorization: str = Header(default="")):
        auth(authorization)
        return {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}

    @app.post("/v1/images/generations")
    def generate(body: ImageRequest, authorization: str = Header(default="")):
        auth(authorization)
        if body.model not in {MODEL_ID, "flex"} or body.size not in SIZES or body.n != 1 or body.response_format != "b64_json":
            raise HTTPException(400, f"Use model {MODEL_ID}, n=1, b64_json and a size from: {', '.join(sorted(SIZES))}")
        if app.state.renderer is None:
            raise HTTPException(503, f"Image model is not ready yet ({status.phase}{': ' + status.detail if status.detail else ''})")
        # One image at a time; the gateway queues the rest.
        if not lock.acquire(blocking=False):
            raise HTTPException(503, "Image worker is busy")
        width, height = map(int, body.size.split("x"))
        result: dict = {}
        finished = threading.Event()

        def render():
            started = time.time()
            try:
                image = app.state.renderer(body.prompt, width, height, steps=body.steps, seed=body.seed,
                                           negative_prompt=body.negative_prompt)
                output = io.BytesIO()
                image.save(output, format="PNG")
                result["body"] = {"created": int(time.time()), "data": [{"b64_json": base64.b64encode(output.getvalue()).decode()}]}
                print(f"[image] {body.size} {body.steps} steps in {time.time() - started:.1f}s", flush=True)
            except Exception as error:
                print(f"[image] failed: {type(error).__name__}: {error}", flush=True)
                result["body"] = {"error": {"message": f"Image generation failed: {type(error).__name__}", "type": "server_error"}}
            finally:
                lock.release()  # only once the GPU is actually free, even if the caller went away
                finished.set()

        threading.Thread(target=render, daemon=True).start()
        # Quick results (and quick failures) get a normal response with a real status code; only
        # renders that take longer switch to the keep-alive stream below.
        if finished.wait(20):
            code = 200 if "data" in result["body"] else 500
            return JSONResponse(result["body"], status_code=code)

        def respond():
            # Large images take minutes. Whitespace before the JSON keeps proxies (Cloudflare drops
            # connections idle for 100 s) from cutting the response; JSON parsers ignore it.
            while not finished.wait(10):
                yield b" "
            yield json.dumps(result["body"]).encode()

        return StreamingResponse(respond(), media_type="application/json")
    return app


def main():
    import uvicorn
    key = os.environ.get("IMAGE_API_KEY", "")
    if len(key) < 32:
        raise SystemExit("Set IMAGE_API_KEY (at least 32 characters) before starting this service")
    status = Status()
    app = create_app(None, key, status)

    def prepare():
        try:
            pipeline_dir, encoder_dir = download(Path(os.environ.get("IMAGE_MODEL_DIR", "/workspace/image-models")), status)
            app.state.renderer = QwenImageRenderer(pipeline_dir, encoder_dir, status)
            status.set("ready")
        except Exception as error:  # surfaced on /health; restart the container to retry
            status.error = f"{type(error).__name__}: {error}"
            status.set("failed", status.error)

    threading.Thread(target=prepare, daemon=True).start()
    uvicorn.run(app, host=os.environ.get("IMAGE_HOST", "127.0.0.1"), port=int(os.environ.get("IMAGE_PORT", "8090")))


if __name__ == "__main__":
    main()
