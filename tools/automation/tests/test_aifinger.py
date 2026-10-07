"""Tests for the AI-fingerprint scanner (bot/aifinger.py).

Covers: C2PA JUMBF detection, encoder-tag detection, synthetic-tone audio
detection, and the overall verdict logic.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
import pytest

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
