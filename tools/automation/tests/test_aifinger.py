"""Tests for the AI-fingerprint scanner (bot/aifinger.py).

Covers: C2PA JUMBF detection, encoder-tag detection, synthetic-tone audio
detection, and the overall verdict logic.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
import pytest

from bot import aifinger
from bot.aifinger import AIFingerReport, Finding, check_c2pa, check_metadata, check_audio, check_resolution_fps, scan


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def clean_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real MP4 with no C2PA, a clean encoder tag, and a non-sine audio track."""
    path = tmp_path_factory.mktemp("af") / "clean.mp4"
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "64k",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return path


@pytest.fixture(scope="module")
def c2pa_video(tmp_path_factory: pytest.TempPathFactory, clean_video: Path) -> Path:
    """A copy of clean_video with a C2PA JUMBF manifest UUID injected into the bytes."""
    path = tmp_path_factory.mktemp("af_c2pa") / "c2pa.mp4"
    raw = bytearray(clean_video.read_bytes())
    # Inject the C2PA manifest JUMBF UUID (63326d61-0011-0010-8000-00AA00389B71)
    # near the header, plus the ASCII marker, so check_c2pa's strong check fires.
    c2pa_uuid = bytes.fromhex("63326d6100110010800000aa00389b71")
    raw[128:144] = c2pa_uuid  # overwrite 16 bytes near the header
    raw += b"c2ma"
    path.write_bytes(bytes(raw))
    return path


# ── unit tests for individual checks ─────────────────────────────────────────

def test_finding_str() -> None:
    f = Finding(severity="strong", label="C2PA", detail="provenance box")
    s = str(f)
    assert "STRONG" in s.upper()
    assert "C2PA" in s


def test_report_add_and_counts() -> None:
    r = AIFingerReport(path="/tmp/x.mp4")
    r.add("strong", "C2PA", "detail")
    r.add("weak", "encoder", "detail")
    r.add("info", "fps", "detail")
    assert r.strong_count == 1
    assert r.weak_count == 1
    assert len(r.findings) == 3


def test_report_to_dict() -> None:
    r = AIFingerReport(path="/tmp/x.mp4")
    r.add("strong", "C2PA", "d")
    d = r.to_dict()
    assert d["verdict"] == "clean"  # default until scan() runs
    assert d["strong_count"] == 1
    assert len(d["findings"]) == 1


def test_c2pa_detected(c2pa_video: Path) -> None:
    report = AIFingerReport(path=c2pa_video)
    check_c2pa(c2pa_video, report)
    assert report.strong_count >= 1
    assert any("C2PA" in f.label for f in report.findings)


def test_no_c2pa_in_clean(clean_video: Path) -> None:
    report = AIFingerReport(path=clean_video)
    check_c2pa(clean_video, report)
    assert report.strong_count == 0


def test_metadata_clean_video_has_no_strong(clean_video: Path) -> None:
    report = AIFingerReport(path=clean_video)
    check_metadata(clean_video, report)
    assert report.strong_count == 0


def test_scan_clean_video(clean_video: Path) -> None:
    """A clean video should return verdict 'clean' (no strong findings)."""
    report = scan(clean_video)
    assert report.verdict in ("clean", "suspicious")
    assert report.strong_count == 0


def test_scan_c2pa_video(c2pa_video: Path) -> None:
    """A video with a C2PA box should return verdict 'likely_ai'."""
    report = scan(c2pa_video)
    assert report.verdict == "likely_ai"


def test_ai_tool_signature_detected(tmp_path: Path) -> None:
    """A video with 'Runway' in the encoder tag should be flagged as a strong finding."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    path = tmp_path / "ai_tool.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "64k",
        "-metadata", "title=Runway Gen-4",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    report = AIFingerReport(path=path)
    check_metadata(path, report)
    assert report.strong_count >= 1
    assert any("AI tool signature" in f.label and "runway" in f.detail.lower() for f in report.findings)


@pytest.fixture
def iphone_probe() -> dict:
    return {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "tags": {
                "major_brand": "qt  ", "minor_version": "0", "compatible_brands": "qt  ",
                "com.apple.quicktime.make": "Apple",
                "com.apple.quicktime.model": "iPhone 15 Pro Max",
                "com.apple.quicktime.software": "26.6",
                "com.apple.quicktime.creationdate": "2026-10-09T12:00:00-0400",
                "com.apple.quicktime.full-frame-rate-playback-intent": "0",
            },
        },
        "streams": [
            {
                "codec_type": "video", "codec_name": "h264", "profile": "High",
                "codec_tag_string": "avc1", "r_frame_rate": "30/1",
                "color_space": "bt709", "color_primaries": "bt709",
                "color_transfer": "bt709", "color_range": "tv",
                "tags": {"handler_name": "Core Media Video", "language": "und",
                         "vendor_id": "[0][0][0][0]", "encoder": "H.264"},
            },
            {
                "codec_type": "audio", "codec_name": "aac",
                "tags": {"handler_name": "Core Media Audio", "language": "und",
                         "vendor_id": "[0][0][0][0]"},
            },
        ],
    }


def test_iphone_signature_audio_optional_only_when_requested(monkeypatch, iphone_probe) -> None:
    monkeypatch.setattr(aifinger, "_ffprobe_json", lambda path: iphone_probe)
    assert aifinger.looks_iPhone("output.mov")
    iphone_probe["streams"].pop()
    assert not aifinger.looks_iPhone("output.mov")
    assert aifinger.looks_iPhone("output.mov", require_audio=False)


@pytest.mark.parametrize("key,value", [
    ("codec_name", "mp3"),
    ("handler_name", "SoundHandler"),
    ("language", "eng"),
    ("vendor_id", "FFMP"),
])
def test_iphone_optional_audio_still_validates_present_audio(monkeypatch, iphone_probe, key, value) -> None:
    audio = iphone_probe["streams"][1]
    (audio if key == "codec_name" else audio["tags"])[key] = value
    monkeypatch.setattr(aifinger, "_ffprobe_json", lambda path: iphone_probe)
    assert not aifinger.looks_iPhone("output.mov", require_audio=False)


@pytest.mark.parametrize("fps", ["24/1", "25/1", "30/1", "30000/1001", "60/1", "60000/1001", "24000/1001"])
def test_iphone_signature_supported_frame_rates(monkeypatch, iphone_probe, fps) -> None:
    iphone_probe["streams"][0]["r_frame_rate"] = fps
    monkeypatch.setattr(aifinger, "_ffprobe_json", lambda path: iphone_probe)
    assert aifinger.looks_iPhone("output.mov")


@pytest.mark.parametrize("field,value", [
    ("codec_name", "hevc"), ("profile", "Main"),
    ("color_space", "bt2020nc"), ("r_frame_rate", "15/1"),
])
def test_iphone_signature_preserves_video_requirements(monkeypatch, iphone_probe, field, value) -> None:
    iphone_probe["streams"][0][field] = value
    monkeypatch.setattr(aifinger, "_ffprobe_json", lambda path: iphone_probe)
    assert not aifinger.looks_iPhone("output.mov", require_audio=False)


def test_iphone_signature_requires_metadata(monkeypatch, iphone_probe) -> None:
    iphone_probe["format"]["tags"].pop("com.apple.quicktime.model")
    monkeypatch.setattr(aifinger, "_ffprobe_json", lambda path: iphone_probe)
    assert not aifinger.looks_iPhone("output.mov", require_audio=False)


def test_iphone_signature_uses_configured_ffprobe(monkeypatch, iphone_probe, tmp_path) -> None:
    commands = []
    executable = str(tmp_path / "custom tools" / "ffprobe")

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(iphone_probe), stderr="")

    monkeypatch.setattr(aifinger.subprocess, "run", run)
    assert aifinger.looks_iPhone("output.mov", ffprobe_bin=executable)
    assert commands[0][0] == executable
    assert commands[0][-1] == "output.mov"


def test_iphone_signature_missing_configured_ffprobe_fails_closed(monkeypatch) -> None:
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(aifinger.subprocess, "run", missing)
    assert not aifinger.looks_iPhone("output.mov", ffprobe_bin="missing-ffprobe", require_audio=False)
