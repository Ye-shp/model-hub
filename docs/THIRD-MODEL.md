# A complementary third model

Checked September 28, 2026. Exact repository revisions are recorded in `models/catalog.json`; recommendations are based on publisher documentation, not benchmarks on your rental.

## First choice: FLUX.2 Klein 4B

[black-forest-labs/FLUX.2-klein-4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) adds image generation for original covers, illustrations and visual concepts. It uses four inference steps in the publisher's example, carries Apache 2.0 licensing, and the publisher reports approximately 13 GB VRAM with its recommended setup. It is not an abliterated checkpoint.

Two 24 GB cards do not form a freely interchangeable 48 GB pool. Each card already hosts a 27B model and its runtime state. **Do not automatically load this beside a resident model.** Keep it as a separate on-demand image service if both residents must remain loaded. Its rental/API charge is additional; no current price is assumed here. The publisher's advertised latency is not a measured RTX 3090 latency for this project.

The optional adapter is `services/image_server.py`. On a separate CUDA worker, install an appropriate CUDA PyTorch build, then:

```bash
python -m pip install -r services/requirements-image.txt
export IMAGE_API_KEY='a-separate-random-key-at-least-32-characters'
python services/image_server.py
```

The service binds to loopback port 8090, loads the pinned model before becoming healthy, allows one image at a time, and refuses to load when less than 14 GiB VRAM is free. That check is a conservative startup prerequisite, not an OOM guarantee. CPU offloading also needs sufficient system RAM. Expose it through your own authenticated tunnel/reverse proxy when it runs on another host; do not open a public unauthenticated model port.

Configure the hub only when that service is available:

```dotenv
MODEL3_URL=https://YOUR-IMAGE-SERVICE/v1
MODEL3_ID=black-forest-labs/FLUX.2-klein-4B
MODEL3_KIND=image
MODEL3_API_KEY=the-image-service-key
MODEL3_PARALLEL=1
```

Use the Visual production skill with images enabled. The team saves PNGs and prompt recipes. Its two-image allowance persists across resumes. This adapter exposes text-to-image generation; the model's editing capabilities are not implemented here. The HTTP adapter is tested with a mock renderer; the actual diffusion pipeline still needs a GPU smoke test.

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
