"""Measure a short-form video so a model can analyse it: download (if a link), shots and cut rate, keyframes,
on-screen text, voiceover transcript, the song/sound used, and the post's stats.

    python video_probe.py <url or file> <output folder> [--frames 12] [--whisper DIR]

Runs inside the chat's sandbox with the tools virtualenv. Writes <output>/analysis.json and <output>/frames/*.jpg.
Every step is best-effort: a step that fails is recorded under "errors" and the rest still runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

META_KEYS = ("title", "description", "uploader", "uploader_id", "channel", "upload_date", "timestamp", "duration",
             "view_count", "like_count", "comment_count", "repost_count", "save_count", "track", "artist", "album",
             "tags", "webpage_url", "extractor", "width", "height", "fps")


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, timeout=60)
    data = json.loads(out.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    rate = video.get("avg_frame_rate") or "0/1"
    num, _, den = rate.partition("/")
    return {"duration": float(data.get("format", {}).get("duration") or 0), "width": video.get("width"),
            "height": video.get("height"), "fps": round(float(num) / float(den or 1), 2) if float(den or 1) else None,
            "has_audio": audio is not None, "video_codec": video.get("codec_name")}


def download(url: str, folder: Path) -> tuple[Path, dict]:
    import yt_dlp
    options = {"outtmpl": str(folder / "video.%(ext)s"), "format": "mp4/bestvideo[height<=1080]+bestaudio/best",
               "merge_output_format": "mp4", "quiet": True, "no_warnings": True, "noplaylist": True,
               "socket_timeout": 30, "retries": 2}
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]
        path = Path(ydl.prepare_filename(info))
    if not path.exists():
        found = sorted(folder.glob("video.*"))
        if not found:
            raise RuntimeError("download produced no file (a photo slideshow? try the phone or upload the file)")
        path = found[0]
    meta = {k: info.get(k) for k in META_KEYS if info.get(k) not in (None, "", [])}
    return path, meta


def shots(path: Path) -> list[tuple[float, float]]:
    from scenedetect import AdaptiveDetector, detect
    scenes = detect(str(path), AdaptiveDetector(), show_progress=False)
    seconds = lambda t: t.seconds if isinstance(getattr(type(t), "seconds", None), property) else t.get_seconds()
    return [(round(seconds(a), 2), round(seconds(b), 2)) for a, b in scenes]


def frame_times(duration: float, scenes: list[tuple[float, float]], limit: int) -> list[float]:
    """The hook (0 s, 1 s, 2 s), then the middle of each shot, thinned evenly to the limit."""
    hook = [t for t in (0.0, 1.0, 2.0) if t < max(duration - 0.05, 0.1)]
    middles = [round((a + b) / 2, 2) for a, b in scenes if (a + b) / 2 > 2.2] or \
              [round(duration * k / 6, 2) for k in range(1, 6) if duration * k / 6 > 2.2]
    room = max(limit - len(hook), 1)
    if len(middles) > room:
        step = len(middles) / room
        middles = [middles[int(i * step)] for i in range(room)]
    if duration > 4 and (not middles or duration - middles[-1] > 2):
        middles[-1:] = middles[-1:] + [round(max(duration - 0.5, 0), 2)]  # the ending usually holds the CTA
    return sorted(set(hook + middles))[:limit + 1]


def grab(path: Path, at: float, target: Path) -> bool:
    out = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale='min(720,iw)':-2", "-q:v", "3", str(target)], capture_output=True, timeout=60)
    return out.returncode == 0 and target.exists()


def read_text(frames: list[dict]) -> list[dict]:
    from rapidocr import RapidOCR
    engine = RapidOCR()
    seen, found = set(), []
    for frame in frames:
        result = engine(frame["path"])
        texts = [t.strip() for t in (getattr(result, "txts", None) or ()) if t and t.strip()]
        new = [t for t in texts if t.lower() not in seen]
        seen.update(t.lower() for t in texts)
        if texts:
            found.append({"at": frame["at"], "text": " | ".join(texts), "new": bool(new)})
    return found


def transcribe(path: Path, model_dir: str) -> dict:
    audio = path.with_name("audio.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(audio)],
                   check=True, capture_output=True, timeout=180)
    import wave
    import numpy as np
    from faster_whisper import WhisperModel
    with wave.open(str(audio), "rb") as handle:  # decode ourselves: faster-whisper's own decoder breaks with newer PyAV
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=min(8, os.cpu_count() or 4))
    segments, info = model.transcribe(samples, vad_filter=True, beam_size=1)
    lines = [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()} for s in segments]
    words = sum(len(line["text"].split()) for line in lines)
    return {"language": info.language, "speech_seconds": round(sum(l["end"] - l["start"] for l in lines), 1),
            "words": words, "segments": lines}


def identify_sound(path: Path) -> dict | None:
    from shazamio import Shazam
    clip = path.with_name("clip.mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-t", "20", "-vn", "-ac", "1", "-ar", "44100",
                    str(clip)], check=True, capture_output=True, timeout=120)

    async def run():
        return await Shazam().recognize(str(clip))
    found = asyncio.run(run())
    track = (found or {}).get("track")
    if not track:
        return None
    return {"title": track.get("title"), "artist": track.get("subtitle"), "url": track.get("url")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("out")
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--whisper", default="")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "frames").mkdir(parents=True, exist_ok=True)
    report: dict = {"source": args.source, "errors": {}}

    if args.source.startswith(("http://", "https://")):
        try:
            path, report["post"] = download(args.source, out)
        except Exception as error:
            report["errors"]["download"] = f"{type(error).__name__}: {str(error)[:400]}"
            (out / "analysis.json").write_text(json.dumps(report, indent=1))
            print(json.dumps({"ok": False, "error": report["errors"]["download"]}))
            return
    else:
        path = Path(args.source)
        if not path.is_file():
            print(json.dumps({"ok": False, "error": f"{args.source} is not a file"}))
            return
    report["file"] = str(path)
    report["video"] = ffprobe(path)
    duration = report["video"]["duration"]

    try:
        scenes = shots(path)
    except Exception as error:
        scenes, report["errors"]["shots"] = [], f"{type(error).__name__}: {str(error)[:300]}"
    if not scenes and duration:
        scenes = [(0.0, round(duration, 2))]
    lengths = [b - a for a, b in scenes]
    report["shots"] = {"count": len(scenes), "cuts_per_second": round(max(len(scenes) - 1, 0) / duration, 2) if duration else None,
                       "average_shot_seconds": round(sum(lengths) / len(lengths), 2) if lengths else None,
                       "first_cut_at": scenes[1][0] if len(scenes) > 1 else None, "list": scenes[:80]}

    frames = []
    for i, at in enumerate(frame_times(duration, scenes, args.frames)):
        target = out / "frames" / f"{i:02d}_{at:06.2f}s.jpg"
        if grab(path, at, target):
            frames.append({"at": at, "path": str(target)})
    report["frames"] = frames

    try:
        report["on_screen_text"] = read_text(frames)
    except Exception as error:
        report["on_screen_text"], report["errors"]["ocr"] = [], f"{type(error).__name__}: {str(error)[:300]}"
    if report["video"]["has_audio"]:
        if args.whisper:
            try:
                report["transcript"] = transcribe(path, args.whisper)
            except Exception as error:
                report["errors"]["transcript"] = f"{type(error).__name__}: {str(error)[:300]}"
        try:
            report["sound"] = identify_sound(path)
        except Exception as error:
            report["errors"]["sound"] = f"{type(error).__name__}: {str(error)[:300]}"
    (out / "analysis.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps({"ok": True, "analysis": str(out / "analysis.json")}))


if __name__ == "__main__":
    sys.exit(main())
