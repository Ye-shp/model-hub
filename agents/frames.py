"""Frame extraction (/frame): turn a video into a character sheet to reuse later.

1. The video probe (tools/video_probe.py --dense) downloads the video (or takes an uploaded file) and grabs frames
   evenly across it plus the start of every shot, so every look, angle and outfit change is caught. It also reads
   on-screen text and transcribes the voice.
2. Each frame's colour palette is measured from its pixels (real hex values, not guessed).
3. GLM's vision model (glm.VISION_MODEL, abliteration.ai) reads the frames a few at a time, in parallel, and writes
   detailed notes per frame. Without GLM connected, the resident Qwen-VL reads them instead.
4. The large GLM model (or Qwen) merges the notes, transcript and measurements into one character sheet: the
   canonical look, every outfit, the setting, light and camera, what changes between shots, what had to be
   extrapolated (clearly marked), and a prompt kit for image and video models.
5. The sheet, a contact sheet of the frames and the frames themselves are saved in the chat's folder
   (character-sheets/), shared as files, and the sheet goes into the knowledge base so later chats can use it.

Telegram's /frame command and the owner's Cowork helpers (frame_sheet tool) both run this.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import glm
import workspace as ws

MAX_FRAMES = int(os.environ.get("FRAME_MAX_FRAMES", "40"))
BATCH = 4         # frames per vision call: enough context to compare neighbours, small enough to stay detailed
PARALLEL = 3      # vision calls at once
SKILL = "frame-extraction"

FRAME_PROMPT = """You are building a character sheet from a video, frame by frame. For EACH frame below, write down
everything visible that someone would need to recreate this character and shot exactly. Be concrete and exhaustive:
exact colours, materials, cuts, lengths, positions. Describe only what you can see. Do not identify the person or
guess their name, and do not guess ethnicity or other sensitive traits; describe visible features instead (skin tone,
features, hair). If the same person appears in several frames, call them by the same label (A, B, ...).

Reply with ONLY a JSON array, one object per frame, in order:
[{"t": <seconds>, "shot": "extreme close-up | close-up | medium close-up | medium | cowboy | full | wide",
  "camera": {"angle": "...", "height": "...", "lens_feel": "...", "movement_hint": "...", "focus": "..."},
  "subjects": [{"label": "A", "apparent_age": "...", "face": "shape, eyes, brows, nose, lips, skin, marks",
                "hair": "colour, length, texture, parting, style", "makeup": "...", "facial_hair": "...",
                "build": "...", "pose": "...", "expression": "...", "gaze": "...", "hands": "...",
                "outfit": [{"item": "...", "colour": "...", "material": "...", "fit": "...", "details": "..."}],
                "accessories": ["..."], "nails": "...", "tattoos_marks": "..."}],
  "setting": "location type and everything in the background", "props": ["..."],
  "lighting": "key direction and quality, fill, colour temperature, practical lights, time of day",
  "grade": "colour grade, contrast, saturation, grain, phone/cinema look", "on_screen_text": "...",
  "action": "what happens in this frame", "changes": "what changed from the previous frame (look, outfit, place)"}]"""

SHEET_PROMPT = """Merge the frame notes below into ONE character sheet in markdown, for recreating this character and
their shots later with image and video models (Higgsfield, Qwen-Image and similar). Use exactly these sections:

# {title}
## At a glance  (6-10 bullets: the canonical look in the most reusable words)
## Face and hair
## Body and posture
## Wardrobe  (one sub-heading per look, with the timestamps it appears; every item with colour, material, fit)
## Accessories and details
## Expressions, gestures and mannerisms
## Voice and speech  (from the transcript: tone, pace, catchphrases; "no voice" if none)
## Setting and props
## Lighting and camera  (shot sizes used, angles, lens feel, movement, the overall look)
## Colour palette  (use the MEASURED hex values given; say where each colour appears)
## Continuity  (what never changes; what changes and when)
## Extrapolated  (details the video doesn't show but a full recreation needs: back of the outfit, shoes, other
   angles... Mark every line "(inferred)" and say what it is inferred from)
## Prompt kit
   - Base prompt: one paragraph describing the character for a text-to-image model
   - One prompt per look/scene
   - Negative prompt
   - Consistency notes: the 5 details that make this character recognisable

Rules: describe only what the notes support (except the Extrapolated section). When notes disagree, say which frames
show what. Don't identify the person or guess names, ethnicity or other sensitive traits. No preamble: start with
the title line."""


def parse_task(task: str) -> tuple[str, str]:
    """(source, name) from a /frame job's task text ("SOURCE: ..." and an optional "NAME: ...")."""
    source = re.search(r"^SOURCE:\s*(\S+)", task or "", re.M)
    name = re.search(r"^NAME:\s*(.+)$", task or "", re.M)
    return (source.group(1).strip() if source else ""), (name.group(1).strip()[:80] if name else "")


def task_text(source: str, name: str = "") -> str:
    return f"Frame extraction: build a character sheet from this video.\nSOURCE: {source}" + (f"\nNAME: {name}" if name else "")


def palette(path: str, colours: int = 6) -> list[dict]:
    """The frame's main colours, measured: [{"hex": "#aabbcc", "share": 0.31}, ...]."""
    from PIL import Image
    image = Image.open(path).convert("RGB")
    image.thumbnail((160, 160))
    quantised = image.quantize(colors=colours, method=Image.Quantize.MEDIANCUT)
    counts = sorted(quantised.getcolors() or [], reverse=True)
    pal = quantised.getpalette() or []
    total = sum(c for c, _ in counts) or 1
    return [{"hex": "#{:02x}{:02x}{:02x}".format(*pal[i * 3:i * 3 + 3]), "share": round(c / total, 3)} for c, i in counts]


def jpeg_uri(path: str, width: int = 768) -> str:
    from PIL import Image
    image = Image.open(path).convert("RGB")
    image.thumbnail((width, width * 2))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def contact_sheet(frames: list[dict], columns: int = 5, width: int = 300) -> bytes:
    """All frames in a grid with their timestamps, as a JPEG."""
    from PIL import Image, ImageDraw
    tiles = []
    for frame in frames:
        image = Image.open(frame["path"]).convert("RGB")
        image.thumbnail((width, width * 2))
        tiles.append((image, f"{frame['at']:.1f}s"))
    height = max(t[0].height for t in tiles) + 24
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * (width + 8) + 8, rows * (height + 8) + 8), "white")
    draw = ImageDraw.Draw(sheet)
    for n, (image, label) in enumerate(tiles):
        x, y = 8 + (n % columns) * (width + 8), 8 + (n // columns) * (height + 8)
        sheet.paste(image, (x, y))
        draw.text((x + 4, y + image.height + 4), label, fill="black")
    out = io.BytesIO()
    sheet.save(out, format="JPEG", quality=85)
    return out.getvalue()


def parse_notes(text: str, batch: list[dict]) -> list[dict]:
    """The vision model's JSON array, leniently; raw text per frame when it isn't valid JSON."""
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            notes = json.loads(text[start:end + 1])
            if isinstance(notes, list) and notes:
                out = []
                for frame, note in zip(batch, notes):
                    note = note if isinstance(note, dict) else {"notes": str(note)}
                    note["t"] = frame["at"]
                    out.append(note)
                return out
        except ValueError:
            pass
    return [{"t": frame["at"], "notes": text.strip()[:6000]} for frame in batch[:1]] + \
           [{"t": frame["at"], "notes": "(see the previous frame's notes)"} for frame in batch[1:]]


async def _ask(messages: list, vision: bool, hub_client, gate, max_tokens: int) -> tuple[str, float, str]:
    """(reply, cost, model): GLM (vision or large) while it's usable, otherwise the resident Qwen through the gateway."""
    if glm.available() is None:
        try:
            text, spent = await glm.complete(messages, glm.VISION_MODEL if vision else glm.MODEL, max_tokens=max_tokens,
                                             temperature=0.2)
            return text, spent, glm.VISION_MODEL if vision else glm.MODEL
        except Exception as error:  # fall back to Qwen rather than lose the work
            print(f"[frames] GLM call failed, using Qwen: {type(error).__name__}", flush=True)
    async with gate:
        response = await hub_client.chat.completions.create(model="qwen", messages=messages, max_tokens=max_tokens,
                                                            temperature=0.2, extra_body={"reasoning_effort": "low"})
    return (response.choices[0].message.content or "").strip(), 0.0, "qwen"


async def read_frames(frames: list[dict], hub_client, gate, log) -> tuple[list[dict], float, set]:
    """Notes for every frame, BATCH frames per call, PARALLEL calls at once."""
    batches = [frames[i:i + BATCH] for i in range(0, len(frames), BATCH)]
    limiter, done = asyncio.Semaphore(PARALLEL), {"n": 0}

    async def one(batch):
        content = [{"type": "text", "text": FRAME_PROMPT}]
        for frame in batch:
            content.append({"type": "text", "text": f"Frame at {frame['at']:.2f} s:"})
            content.append({"type": "image_url", "image_url": {"url": jpeg_uri(frame["path"])}})
        async with limiter:
            text, spent, model = await _ask([{"role": "user", "content": content}], True, hub_client, gate, 6000)
        done["n"] += len(batch)
        log("tool", f"Read {done['n']}/{len(frames)} frames")
        return parse_notes(text, batch), spent, model

    results = await asyncio.gather(*(one(b) for b in batches))
    notes = [note for batch_notes, _, _ in results for note in batch_notes]
    return notes, sum(spent for _, spent, _ in results), {model for _, _, model in results}


def frame_table(notes: list[dict], frames: list[dict], folder: str) -> str:
    rows = ["| Time | Frame | Shot | What's visible |", "|---|---|---|---|"]
    for note, frame in zip(notes, frames):
        subjects = note.get("subjects") or []
        looks = "; ".join(f"{s.get('label', '?')}: " + ", ".join(
            f"{o.get('colour', '')} {o.get('item', '')}".strip() for o in (s.get("outfit") or []) if isinstance(o, dict))
                          for s in subjects if isinstance(s, dict))
        seen = " · ".join(x for x in (note.get("action"), looks, note.get("setting")) if x) or str(note.get("notes", ""))[:200]
        rows.append(f"| {frame['at']:.2f}s | {folder}/frames/{Path(frame['path']).name} | {note.get('shot', '')} | "
                    f"{seen.replace('|', '/').replace(chr(10), ' ')[:400]} |")
    return "\n".join(rows)


async def probe(space, source: str, out_folder: str):
    """Run the video probe densely in the sandbox: (report, output folder, error or None)."""
    import research_tools
    import toolbox
    target = source if source.startswith(("http://", "https://")) else str(space.resolve(source))
    out = space.resolve(out_folder)
    await asyncio.to_thread(toolbox.wait_ready)
    command = [str(toolbox.PYTHON), str(research_tools.script("video_probe.py")), target, str(out), "--dense",
               "--frames", str(MAX_FRAMES), "--whisper", str(toolbox.WHISPER_DIR)]
    result = await space.run(command, timeout=1200)
    report_path = out / "analysis.json"
    if not report_path.is_file():
        return None, out, "the video couldn't be processed: " + result["output"][-1500:]
    report = json.loads(report_path.read_text())
    if not report.get("frames"):
        return report, out, "couldn't get the video: " + json.dumps(report.get("errors", {}))[:1000]
    return report, out, None


async def extract(source: str, space, project: str, job_id: str, hub_client, gate, log, name: str = "",
                  share: bool = True) -> dict:
    """The whole pipeline. Returns {"ok", "summary", "folder", "sheet", "cost", ...} or {"ok": False, "error"}."""
    import research_tools
    started = time.monotonic()
    title = name or "Character"
    folder = f"character-sheets/{research_tools.slug(name or source.rsplit('/', 1)[-1] or source)}-{time.strftime('%Y%m%d-%H%M%S')}"
    log("tool", f"Extracting frames from {source[:120]}")
    report, out, error = await probe(space, source, folder)
    if error:
        return {"ok": False, "error": error}
    frames = report["frames"]
    log("tool", f"Got {len(frames)} frames; measuring colours")
    colours = [{"t": f["at"], "palette": palette(f["path"])} for f in frames]
    if glm.available() is not None:
        log("tool", "GLM isn't available (" + glm.available() + "); Qwen reads the frames instead")
    notes, vision_cost, readers = await read_frames(frames, hub_client, gate, log)
    notes_json = json.dumps({"notes": notes, "colours": colours}, ensure_ascii=False, indent=1)
    space.write_text(f"{folder}/frames.json", notes_json)

    overall: dict[str, float] = {}
    for item in colours:
        for c in item["palette"]:
            overall[c["hex"]] = overall.get(c["hex"], 0) + c["share"] / len(colours)
    main = sorted(overall.items(), key=lambda kv: -kv[1])[:12]
    transcript = (report.get("transcript") or {}).get("text") or ""
    on_screen = "; ".join(f"{t['at']}s: {t['text']}" for t in report.get("on_screen_text") or [])[:4000]
    caption = ((report.get("post") or {}).get("description") or "")[:1500]
    video = report.get("video") or {}
    material = (f"SOURCE: {source}\nDURATION: {video.get('duration', 0):.1f} s, {video.get('width')}x{video.get('height')}\n"
                f"SHOTS: {json.dumps((report.get('shots') or {}).get('list') or [])[:2000]}\n"
                f"MEASURED PALETTE (whole video, share of pixels): {', '.join(f'{h} {s:.0%}' for h, s in main)}\n"
                f"CAPTION: {caption or '(none)'}\nON-SCREEN TEXT: {on_screen or '(none)'}\n"
                f"TRANSCRIPT: {transcript[:8000] or '(no speech)'}\n\nFRAME NOTES (JSON):\n"
                + json.dumps(notes, ensure_ascii=False)[:150000])
    log("tool", "Writing the character sheet")
    sheet_text, sheet_cost, writer = await _ask(
        [{"role": "user", "content": SHEET_PROMPT.format(title=f"{title} — character sheet") + "\n\n" + material}],
        False, hub_client, gate, 12000)
    sheet_text = re.sub(r"^\s*```(?:markdown)?\s*|\s*```\s*$", "", sheet_text).strip()
    if not sheet_text.startswith("# "):
        sheet_text = f"# {title} — character sheet\n\n" + sheet_text
    footer = (f"\n\n---\n\n## Frame by frame\n\n{frame_table(notes, frames, folder)}\n\n"
              f"_Made from {len(frames)} frames of {source} on {time.strftime('%Y-%m-%d')}. Frames were read by "
              f"{', '.join(sorted(readers))}; the sheet was written by {writer}. Contact sheet: {folder}/contact-sheet.jpg._\n")
    sheet = sheet_text + footer
    space.write_text(f"{folder}/sheet.md", sheet)
    contact = contact_sheet(frames)
    space.write_bytes(f"{folder}/contact-sheet.jpg", contact)
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
        for frame in frames:
            archive.write(frame["path"], f"frames/{Path(frame['path']).name}")
        archive.writestr("frames.json", notes_json)
        archive.writestr("sheet.md", sheet)
    space.write_bytes(f"{folder}/frames.zip", packed.getvalue())
    if share:
        name_base = research_tools.slug(title)
        ws.write_artifact(project, job_id, f"{name_base}-character-sheet.md", sheet.encode(), "text/markdown")
        ws.write_artifact(project, job_id, f"{name_base}-contact-sheet.jpg", contact, "image/jpeg")
        ws.write_artifact(project, job_id, f"{name_base}-frames.zip", packed.getvalue(), "application/zip")
    try:
        ws.ingest(project, f"Character sheet: {title} ({time.strftime('%Y-%m-%d')})"[:200], sheet, source[:2000])
        saved = True
    except ValueError:
        saved = False
    cost = vision_cost + sheet_cost
    first = next((line for line in sheet_text.splitlines() if line.startswith("## At a glance")), None)
    glance = ""
    if first:
        block = sheet_text.split(first, 1)[1].split("\n## ", 1)[0].strip()
        glance = block[:1500]
    return {"ok": True, "folder": folder, "sheet": f"{folder}/sheet.md", "frames": len(frames), "cost": round(cost, 4),
            "readers": sorted(readers), "writer": writer, "saved_to_knowledge": saved, "glance": glance,
            "seconds": round(time.monotonic() - started)}


def summary(result: dict, title: str) -> str:
    if not result.get("ok"):
        return f"⚠️ Frame extraction failed: {result.get('error', 'unknown error')}"
    cost = f"${result['cost']:.3f}" if result["cost"] else "free (Qwen)"
    return (f"🎞 **{title} — character sheet** ({result['frames']} frames, {result['seconds']} s, {cost})\n\n"
            + (result["glance"] + "\n\n" if result["glance"] else "")
            + f"Files: the sheet, a contact sheet and all frames (zip). Saved in `{result['folder']}/`"
            + (" and to the knowledge base." if result["saved_to_knowledge"] else "."))


async def run_job(job: dict, gate: asyncio.Semaphore | None = None) -> str:
    """A /frame job (skill "frame-extraction"): runs the pipeline without an agent, so the only paid calls are the
    frame reading and the sheet writing."""
    import hub
    import sandbox
    from cowork import tier_for
    source, name = parse_task(job["task"])
    if not source:
        return "Send /frame with a video link, or attach a video with /frame as its caption."
    space = sandbox.Workspace(tier_for(job), job.get("thread") or "console").prepare()
    if not space.is_owner:
        return "Frame extraction is for the owner only."
    client = hub.async_client()

    def log(kind: str, detail: str):
        ws.event(job["id"], kind, detail)
    try:
        async with asyncio.timeout(1800):
            result = await extract(source, space, job["project"], job["id"], client, gate or asyncio.Semaphore(4), log, name)
    finally:
        await client.close()
    if result.get("ok"):
        log("tool", f"Character sheet done: {result['frames']} frames, ${result['cost']:.4f}")
    return summary(result, name or "Character")
