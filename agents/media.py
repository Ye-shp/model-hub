"""Import supplied text or transcribe a local audio/video file into project knowledge."""
import argparse
import json
from pathlib import Path

import hub
import store
import workspace as ws


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["import", "transcribe"])
    ap.add_argument("file", type=Path)
    ap.add_argument("--project", default="default")
    ap.add_argument("--title")
    args = ap.parse_args()
    if not args.file.is_file():
        ap.error("File not found")
    ws.init()
    if not ws.project_exists(args.project):
        ap.error("Create the project first")
    if args.mode == "import":
        if args.file.stat().st_size > 2_000_000:
            ap.error("Text imports are limited to 2 MB")
        text = args.file.read_text(encoding="utf-8")
    else:
        from huggingface_hub import snapshot_download
        from faster_whisper import WhisperModel
        folder = snapshot_download("Systran/faster-whisper-small", revision="536b0662742c02347bc0e980a01041f333bce120",
                                   cache_dir=str(store.DATA / "model-cache"))
        model = WhisperModel(folder, device="cpu", compute_type="int8", cpu_threads=4)
        segments, info = model.transcribe(str(args.file), beam_size=3, vad_filter=True)
        text = "\n".join(f"[{s.start:.2f}-{s.end:.2f}s] {s.text.strip()}" for s in segments)
        artifact = ws.write_artifact(args.project, None, args.file.stem + "-transcript.txt", text.encode(), "text/plain")
        print(json.dumps({"language": info.language, "transcript_artifact": artifact}))
    print(json.dumps(ws.ingest(args.project, args.title or args.file.name, text, str(args.file.resolve()))))


if __name__ == "__main__":
    main()
