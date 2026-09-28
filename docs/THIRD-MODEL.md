# A complementary third model

Checked September 28, 2026. Exact repository revisions are recorded in `models/catalog.json`; recommendations are based on publisher documentation, not benchmarks on your rental.

## Image generation: Qwen-Image-2.1 with an abliterated text encoder

[Qwen/Qwen-Image-2.1](https://huggingface.co/Qwen/Qwen-Image-2.1) (released September 14, 2026) generates and edits images natively up to 2K, renders text well and supports transparent (RGBA) output. The hub runs it with [pottokao/Qwen-Image-2.1-Text-Encoder-Heretic](https://huggingface.co/pottokao/Qwen-Image-2.1-Text-Encoder-Heretic), a refusal-ablated copy of its Qwen3-VL-8B text encoder (5/100 refusals vs 100/100, KL 0.022; Apache-2.0). Only the text encoder is modified; the diffusion transformer and VAE are the official weights, so what the model can draw is still bounded by its training.

**License: Qwen-Image-2.1 is under the Qwen Research License, non-commercial use only.** Commercial use (ads, monetized posts) needs a separate license from Qwen (model-business@notice.qwencloud.com). The Apache-2.0 alternative is Qwen/Qwen-Image-2512.

### Where it runs

On its own GPU box, not beside the chat models: an RTX 3090 or newer (Ampere+, because it runs in bf16; V100s cannot). The box image is `deploy/image/Dockerfile`, built by the same workflow as `ghcr.io/<repo>-image`. It downloads about 33 GB of pinned weights on first boot (the stock text encoder is skipped), serves the OpenAI images API on loopback port 8090 and reaches the hub through **its own** Cloudflare tunnel (two machines must never share one tunnel token, or Cloudflare splits traffic between them).

On a 24 GB card the 17.5 GB encoder and 14 GB transformer cannot both stay on the GPU, so each stage moves in only while it runs (needs about 40 GB of system RAM). With 40 GB+ of VRAM everything stays on the GPU.

Vast template for the image box (one GPU, 80 GB disk, 40 GB+ RAM):

```dotenv
IMAGE_API_KEY=a-separate-random-key-at-least-32-characters
TUNNEL_TOKEN=token-of-the-image-box-tunnel
```

In Cloudflare, give that tunnel a public hostname (e.g. `img.YOUR-DOMAIN`) pointing to `http://localhost:8090`. `/health` is public and shows download and loading progress; everything else needs the key.

Then add the slot to the main box:

```dotenv
MODEL3_URL=https://img.YOUR-DOMAIN/v1
MODEL3_ID=qwen-image-2.1
MODEL3_KIND=image
MODEL3_API_KEY=the-same-IMAGE_API_KEY
MODEL3_PARALLEL=1
```

The gateway exposes it as model `flex` on `/v1/images/generations`, and the website's image button uses it automatically.

| Size | Use |
|---|---|
| `1024x1024`, `1024x1536`, `1536x1024`, `1152x2048` (9:16), `2048x1152` (16:9) | Fast drafts |
| `2048x2048`, `1536x2752` (9:16), `2752x1536` (16:9), `1696x2528`, `2528x1696` | Native 2K finals |

Optional request fields: `steps` (8-60, default 40), `seed`, `negative_prompt`. One image renders at a time; the gateway queues the rest and keeps slow (2K) requests alive past Cloudflare's 100-second limit. Image editing is not wired up yet. Speeds have not been measured on the rental yet.

## A practical CPU companion: Faster Whisper Small

[Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small) is a CTranslate2 conversion of Whisper Small, licensed MIT. It adds a missing source of evidence: speech in supplied audio/video files. This is more useful than another text model when screenshots miss most of a video's meaning.

```powershell
agents/.venv/Scripts/python -m pip install -r agents/requirements-audio.txt
agents/.venv/Scripts/python agents/media.py transcribe path/to/clip.mp4 --project default
```

The importer uses CPU int8, writes a timestamped transcript artifact, and indexes it in project knowledge. The first run downloads a pinned checkpoint. CPU speed, host RAM and transcription quality need validation. This does not record audio from the Android apps or download platform videos. The agent and dashboard do not automatically start transcription.

## Later: preset-voice narration

[Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice) is a candidate for narrated drafts, with multilingual preset voices and Apache 2.0 licensing. A revision is recorded in the catalog for evaluation. Speech generation is not integrated in v3, and no spare VRAM is reserved for it.

## Budget rule

The hub's frontier cap counts requests, not dollars. Per-task allowances also count calls, not dollars. Use actual rental invoices and provider billing limits to manage the $300 total; a third rental, image endpoint or frontier token bill can exceed it. Local research and indexed memory do not require a third GPU.
