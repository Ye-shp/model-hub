"""AI-fingerprint scanner: detect traces that give away AI-generated or AI-tool-processed media.

What we scan (fast, no ML — byte-level + metadata + a few ffmpeg probes):
  1. C2PA / Content Credentials provenance (JUMBF `c2ma` UUID boxes in the MP4 byte stream)
  2. Container metadata: encoder tag, creation timestamp, comments, tags
  3. Known AI-tool signatures in metadata (Runway, Higgsfield, Sora, Kling, HeyGen, CapCut, …)
  4. Audio fingerprint: pure synthetic tone / missing harmonics (ffmpeg astats)
  5. Resolution & frame-rate patterns common to AI generators (e.g. 1080x1920 @ 24 fps is a
     strong Runway/Higgsfield signature; 25 fps is less common in AI tools)

Each check returns a Finding(severity, label, detail). The overall verdict is:
  - `clean`      — no signals, safe to post
  - `suspicious` — one or more weak signals; mediaprep recommended
  - `likely_ai`  — one or more strong signals; mediaprep required
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("bot.aifinger")

# ── C2PA byte signatures ──────────────────────────────────────────────────────
# JUMBF (ISO-BMFF) UUID boxes used by C2PA:
#   c2ma  63326d61-0011-0010-8000-00AA00389B71  — C2PA manifest (hard binding)
#   c2cs  (salt, varies)                          — C2PA salt
#   c2ph  (hash)                                  — C2PA hash
# The manifest UUID is the one that matters: its presence means a C2PA provenance
# claim is embedded. We scan for both the ASCII "c2ma" and the raw UUID bytes.
_C2PA_MANIFEST_UUID = bytes.fromhex("63326d6100110010800000aa00389b71")
_C2PA_MANIFEST_ASCII = b"c2ma"
# Generic C2PA namespace strings that sometimes appear in the manifest JSON
_C2PA_JSON_MARKERS = (
    b'"c2pa"', b'"c2pa.hash"', b'"c2pa.actions"', b'"c2pa.assertions"',
    b'"c2pa.label"', b'"c2pa.thumbnail"',
)

# ── Known AI-tool encoder / metadata signatures ──────────────────────────────
# These are the `encoder`, `comment`, `title`, `artist`, `album` values that AI
# video tools leave in MP4 container metadata. Case-insensitive substring match.
AI_TOOL_SIGNATURES: dict[str, str] = {
    # key: substring to look for  →  value: human-readable tool name
    "runway":     "Runway",
    "higgsfield": "Higgsfield",
    "sora":       "OpenAI Sora",
    "kling":      "Kling (Kuaishou)",
    "veo":        "Google Veo",
    "hailuo":     "Hailuo (MiniMax)",
    "pika":       "Pika",
    "luma":       "Luma Dream Machine",
    "heygen":     "HeyGen",
    "synthesia":  "Synthesia",
    "d-id":       "D-ID",
    "descript":   "Descript",
    "capcut":     "CapCut",
    "canva":      "Canva",
    "adobe premiere": "Adobe Premiere",
    "filmora":    "Filmora",
    "inshot":     "InShot",
    "vn video":   "VN Video",
    "wink":       "Wink",
    "magic edit": "Magic Edit",
    "pixverse":   "Pixverse",
    "haiper":     "Haiper",
    "stability":  "Stability AI",
    "comfyui":    "ComfyUI",
    "stable diffusion": "Stable Diffusion",
    "sdxl":       "Stable Diffusion XL",
    "flux":       "Black Forest FLUX",
    "wan":        "Wan (Alibaba)",
}

# ── Dataclasses ────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    severity: str   # "strong" | "weak" | "info"
    label: str
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.severity.upper():6}] {self.label}: {self.detail}"


@dataclass
class AIFingerReport:
    """Result of scanning one media file."""
    path: str
    findings: list[Finding] = field(default_factory=list)
    verdict: str = "clean"   # clean | suspicious | likely_ai

    def add(self, severity: str, label: str, detail: str = "") -> None:
        self.findings.append(Finding(severity, label, detail))

    @property
    def strong_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "strong")

    @property
    def weak_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "weak")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "verdict": self.verdict,
            "strong_count": self.strong_count,
            "weak_count": self.weak_count,
            "findings": [{"severity": f.severity, "label": f.label, "detail": f.detail} for f in self.findings],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _read_bytes(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _ffprobe_json(path: Path, *, ffprobe_bin: str = "ffprobe") -> dict[str, Any]:
    """Run ffprobe and return the JSON output."""
    try:
        r = subprocess.run(
            [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
    return {}


def _ffmpeg_astats(path: Path) -> dict[str, Any]:
    """Run ffmpeg astats to get audio statistics (DC offset, peak, RMS)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
             "-af", "astats=metadata=1:measure_perchannel=none", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60,
        )
        # astats outputs to stderr; parse the key metrics
        stderr = r.stderr or ""
        stats: dict[str, float] = {}
        for key in ("RMS level", "Peak level", "DC offset", "Dynamic range"):
            m = re.search(rf"{re.escape(key)}:\s*(-?[\d.]+)", stderr)
            if m:
                stats[key] = float(m.group(1))
        return stats
    except Exception:
        return {}


# ── Individual checks ──────────────────────────────────────────────────────────

def check_c2pa(path: Path, report: AIFingerReport) -> None:
    """Byte-scan for C2PA JUMBF manifest boxes."""
    raw = _read_bytes(path)
    # Check for the manifest UUID (strong signal)
    if _C2PA_MANIFEST_UUID in raw:
        report.add("strong", "C2PA provenance",
                   "C2PA manifest (c2ma) JUMBF box found — file carries a content-credentials claim")
    # Check for ASCII "c2ma" (weaker — could be a coincidence in a large file)
    elif _C2PA_MANIFEST_ASCII in raw:
        report.add("weak", "C2PA marker (ASCII)",
                   '"c2ma" string found in byte stream — likely C2PA manifest')
    # Check for C2PA JSON markers
    elif any(m in raw for m in _C2PA_JSON_MARKERS):
        report.add("weak", "C2PA JSON marker",
                   "C2PA assertion strings found in byte stream")


def check_metadata(path: Path, report: AIFingerReport) -> None:
    """Scan ffprobe output for AI-tool encoder / metadata signatures."""
    probe = _ffprobe_json(path)
    if not probe:
        return

    # Gather all string metadata fields from format + streams.
    # ffprobe JSON nests these under a "tags" sub-dict at both levels.
    text_fields: list[str] = []
    fmt = probe.get("format", {})
    fmt_tags = fmt.get("tags", {}) or {}
    for key in ("encoder", "comment", "title", "artist", "album", "tag"):
        v = fmt_tags.get(key)
        if v:
            text_fields.append(str(v))
    for stream in probe.get("streams", []):
        stags = stream.get("tags", {}) or {}
        for key in ("encoder", "comment", "title", "artist", "album"):
            v = stags.get(key)
            if v:
                text_fields.append(str(v))

    joined = " | ".join(text_fields).lower()
    for sig, tool_name in AI_TOOL_SIGNATURES.items():
        if sig in joined:
            severity = "strong"  # encoder tag is a strong signal
            report.add(severity, f"AI tool signature",
                       f"Encoder/metadata contains '{sig}' → likely {tool_name}")

    # Also check the creation_time for an unusually recent timestamp (AI tools
    # generate in seconds; a file created < 1 hour ago is suspicious if it's a
    # "finished" video)
    ct = fmt_tags.get("creation_time", "")
    if ct:
        try:
            from datetime import datetime, timezone
            created = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if age_hours < 1:
                report.add("info", "Recent creation time",
                           f"File created {age_hours:.1f}h ago — normal for AI-generated, "
                           f"unusual for filmed content")
        except Exception:
            pass


def check_audio(path: Path, report: AIFingerReport) -> None:
    """Check audio for synthetic-tone signatures (pure sine wave = strong AI signal)."""
    stats = _ffmpeg_astats(path)
    if not stats:
        return  # no audio stream

    # A pure sine wave has RMS ≈ Peak / √2 ≈ 0.707 × Peak
    rms = stats.get("RMS level", None)
    peak = stats.get("Peak level", None)
    if rms is not None and peak is not None and peak > 0:
        ratio = abs(rms) / abs(peak)
        # Pure sine: ratio ≈ 0.707; real audio: ratio typically 0.1–0.5
        if 0.68 < ratio < 0.73:
            report.add("weak", "Synthetic-tone audio",
                       f"RMS/Peak ratio {ratio:.3f} is very close to a pure sine wave (0.707) — "
                       f"AI tools often use synthetic tones for silent/placeholder audio")

    # DC offset: AI tools sometimes produce a non-zero DC offset
    dc = stats.get("DC offset", None)
    if dc is not None and abs(dc) > 0.01:
        report.add("info", "Audio DC offset",
                   f"DC offset {dc:.4f} — unusual for real audio, common in synthetic audio")


# Legit iPhone capture frame rates. ffprobe reports these as rational "num/den"
# strings; the validator must accept all of them, not just 30 fps (24/25/29.97
# are all real iPhone capture modes).
IPHONE_FPS_SET = {"30/1", "24/1", "25/1", "30000/1001", "60/1", "60000/1001", "24000/1001"}


def looks_iPhone(path: str | Path, *, ffprobe_bin: str = "ffprobe", require_audio: bool = True) -> bool:
    """Validate the reference container signature; this does not establish provenance."""
    try:
        probe = (_ffprobe_json(Path(path)) if ffprobe_bin == "ffprobe"
                 else _ffprobe_json(Path(path), ffprobe_bin=ffprobe_bin))
        fmt = probe.get("format", {})
        tags = fmt.get("tags", {})
        if fmt.get("format_name") != "mov,mp4,m4a,3gp,3g2,mj2":
            return False
        if any(str(tags.get(k, "")).strip() != v for k, v in {
            "major_brand": "qt", "minor_version": "0", "compatible_brands": "qt",
        }.items()):
            return False
        # Accept either com.apple.quicktime.* keys (meta-box path) or
        # unprefixed format tags (no-reference / use_metadata_tags path).
        if not all(
            tags.get("com.apple.quicktime." + key) or tags.get(key)
            for key in ("make", "model", "software", "creationdate", "full-frame-rate-playback-intent")
        ):
            return False
        video = next(s for s in probe["streams"] if s.get("codec_type") == "video")
        audio = next((s for s in probe["streams"] if s.get("codec_type") == "audio"), None)
        if audio is None and require_audio:
            return False
        expected = {"codec_name": "h264", "profile": "High", "codec_tag_string": "avc1",
                    "color_space": "bt709", "color_primaries": "bt709",
                    "color_transfer": "bt709", "color_range": "tv"}
        if (any(video.get(k) != v for k, v in expected.items())
                or video.get("r_frame_rate") not in IPHONE_FPS_SET
                or (audio is not None and audio.get("codec_name") != "aac")):
            return False
        for stream, handler in ((video, "Core Media Video"), (audio, "Core Media Audio")):
            if stream is None:
                continue
            st = stream.get("tags", {})
            if any(st.get(k) != v for k, v in {
                "handler_name": handler, "language": "und", "vendor_id": "[0][0][0][0]",
            }.items()):
                return False
        return video.get("tags", {}).get("encoder") == "H.264"
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration):
        return False


def check_resolution_fps(path: Path, report: AIFingerReport) -> None:
    """Flag resolution/fps combos common to AI generators."""
    probe = _ffprobe_json(path)
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        w = int(stream.get("width", 0))
        h = int(stream.get("height", 0))
        # Frame rate: "30/1", "24/1", "30000/1001" → 29.97, etc.
        fps_str = stream.get("r_frame_rate", "")
        fps = 0.0
        if "/" in fps_str:
            num, den = fps_str.split("/")
            try:
                fps = int(num) / int(den)
            except (ValueError, ZeroDivisionError):
                pass

        # 1080x1920 @ 24 fps = Runway / Higgsfield / Sora signature
        if w == 1080 and h == 1920 and abs(fps - 24) < 1:
            report.add("info" if looks_iPhone(path) else "strong", "AI-typical resolution+fps",
                       f"1080×1920 @ {fps:.0f} fps is a common AI-generator output (Runway, Higgsfield, Sora)")
        # 1024x1792 or 1792x1024 @ any fps = some AI tools
        elif (w, h) in ((1024, 1792), (1792, 1024)):
            report.add("weak", "AI-typical resolution",
                       f"{w}×{h} is a common AI-generator resolution")
        # 25 fps in a 1080x1920 video = less common than 24 or 30 in AI tools
        elif w == 1080 and h == 1920 and abs(fps - 25) < 1:
            report.add("info", "Unusual fps for AI output",
                       f"25 fps is less common in AI tools (most use 24 or 30) — may be human")


# ── Public API ─────────────────────────────────────────────────────────────────

def scan(path: str | Path) -> AIFingerReport:
    """Run all AI-fingerprint checks on a media file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"file not found: {p}")
    if p.stat().st_size < 1024:
        raise ValueError(f"file too small to be a video: {p} ({p.stat().st_size} bytes)")

    report = AIFingerReport(path=str(p))
    check_c2pa(p, report)
    check_metadata(p, report)
    check_audio(p, report)
    check_resolution_fps(p, report)

    # Determine verdict
    if report.strong_count >= 2:
        report.verdict = "likely_ai"
    elif report.strong_count == 1:
        report.verdict = "likely_ai"
    elif report.weak_count >= 2:
        report.verdict = "suspicious"
    elif report.weak_count == 1:
        report.verdict = "suspicious"
    # else: clean

    log.info("aifinger %s → %s (%d strong, %d weak)", p.name, report.verdict,
             report.strong_count, report.weak_count)
    return report
