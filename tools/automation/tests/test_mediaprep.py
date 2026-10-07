"""Tests for the media-prep pipeline (bot/mediaprep.py) and its config wiring.

These are unit-level: they build ffmpeg command lines and check the pipeline
logic without running ffmpeg.
"""
from __future__ import annotations

from pathlib import Path

from bot.config import MediaPrepCfg
from bot.mediaprep import MediaPrep, MediaPrepError, PrepResult


def _mp(**kw) -> MediaPrep:
    base = dict(dry_run=True)
    base.update(kw)
    return MediaPrep(**base)


def test_from_config_defaults() -> None:
    cfg = MediaPrepCfg()
    mp = MediaPrep.from_config(cfg)
    assert mp.enabled is True
    assert mp.strip_c2pa is True
    assert mp.normalize_loudness is True
    assert mp.humanize is True


def test_from_config_disabled() -> None:
    cfg = MediaPrepCfg(enabled=False)
    mp = MediaPrep.from_config(cfg)
    assert mp.enabled is False


def test_build_command_has_metadata_strip() -> None:
    mp = _mp()
    cmd = mp.build_command(Path("/x/in.mp4"), Path("/x/out.mp4"))
    joined = " ".join(cmd)
    assert "-map_metadata -1" in joined
    assert "-movflags +faststart" in joined


def test_build_command_has_loudnorm() -> None:
    mp = _mp()
    cmd = " ".join(mp.build_command(Path("/x/in.mp4"), Path("/x/out.mp4")))
    assert "loudnorm=" in cmd


def test_build_command_has_humanize_filters() -> None:
    mp = _mp(humanize=True)
    cmd = " ".join(mp.build_command(Path("/x/in.mp4"), Path("/x/out.mp4")))
    assert "noise=" in cmd        # grain
    assert "vignette=" in cmd    # vignette
    assert "unsharp=" in cmd     # sharpen


def test_build_command_no_humanize_when_disabled() -> None:
    mp = _mp(humanize=False)
    cmd = " ".join(mp.build_command(Path("/x/in.mp4"), Path("/x/out.mp4")))
    assert "noise=" not in cmd
    assert "vignette=" not in cmd


def test_build_command_has_delogo_when_regions() -> None:
    mp = _mp(remove_watermark=True, watermark_regions=[{"x": 100, "y": 1700, "w": 300, "h": 150}])
    cmd = " ".join(mp.build_command(Path("/x/in.mp4"), Path("/x/out.mp4")))
    assert "delogo=" in cmd


def test_build_command_no_delogo_when_empty_regions() -> None:
    mp = _mp(watermark_regions=[])
    cmd = " ".join(mp.build_command(Path("/x/in.mp4"), Path("/x/out.mp4")))
    assert "delogo=" not in cmd


def test_normalise_regions_accepts_lists() -> None:
    mp = MediaPrep.from_config(
        MediaPrepCfg(
            enabled=True,
            watermark_regions=[[100, 1700, 300, 150]],
        )
    )
    assert len(mp.watermark_regions) == 1
    r = mp.watermark_regions[0]
    assert r["x"] == 100 and r["y"] == 1700 and r["w"] == 300 and r["h"] == 150


def test_prepare_dry_run_noop() -> None:
    """dry_run short-circuits: ok=True, dst == src, no filters applied."""
    mp = _mp()
    src = Path("/tmp/nonexistent_input.mp4")
    r = mp.prepare(src, platform="ig")
    assert r.ok
    assert Path(r.dst) == src
    assert r.duration_s == 0.0
    assert r.filters_applied == []


def test_prepare_disabled_noop() -> None:
    mp = _mp(enabled=False)
    src = Path("/tmp/nonexistent_input.mp4")
    r = mp.prepare(src, platform="ig")
    assert r.ok and Path(r.dst) == src


def test_prepresult_summary() -> None:
    r = PrepResult(ok=True, src="/x/in.mp4", dst="/x/out.mp4",
                   duration_s=30.0, size_bytes=1024)
    s = r.summary()
    assert "OK" in s
    assert "/x/in.mp4" in s


def test_prepresult_error_message() -> None:
    r = PrepResult(ok=False, src="/x/in.mp4", dst="/x/out.mp4", error="boom")
    assert "ERR" in r.summary()
    assert "boom" in r.summary()
