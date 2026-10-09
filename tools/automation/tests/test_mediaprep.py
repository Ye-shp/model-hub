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


def test_quicktime_recipe_and_config() -> None:
    mp = MediaPrep.from_config(MediaPrepCfg())
    cmd = mp.build_command(Path('source.mp4'), Path('output.mov'))
    for flag, value in {'-profile:v': 'high', '-tag:v': 'avc1', '-g': '28',
                        '-keyint_min': '24', '-preset': 'slow', '-fflags': '+bitexact',
                        '-flags': '+bitexact', '-f': 'mov'}.items():
        assert cmd[cmd.index(flag) + 1] == value
    vf = cmd[cmd.index('-vf') + 1]
    assert 'fps=30' in vf
    assert 'setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv' in vf
    assert 'encoder=H.264' in cmd
    assert 'handler_name=Core Media Video' in cmd
    assert 'handler_name=Core Media Audio' in cmd
    assert 'use_metadata_tags' not in ' '.join(cmd)
    assert 'Lavf' not in ' '.join(cmd)
    assert mp.output_ext == '.mov'


def test_config_overrides() -> None:
    mp = MediaPrep.from_config(MediaPrepCfg(target_fps=60, gop_size=60, keyint_min=30,
                                           sharpen=False, highpass_hz=90, apple_model='test model'))
    assert mp.target_fps == 60 and mp.gop_size == 60 and mp.keyint_min == 30
    assert mp.sharpen is False and mp.apple_model == 'test model'
    assert 'highpass=f=90' in mp._build_af_chain()


def test_disabled_without_dry_run() -> None:
    result = MediaPrep(enabled=False, reference_mov='missing').prepare('missing.mp4')
    assert result.ok and result.dst == 'missing.mp4'


def test_resolution_rule_fail_closed(monkeypatch) -> None:
    from bot import aifinger
    probe = {'streams': [{'codec_type': 'video', 'width': 1080, 'height': 1920,
                          'r_frame_rate': '24/1'}]}
    monkeypatch.setattr(aifinger, '_ffprobe_json', lambda p: probe)
    assert not aifinger.looks_iPhone('missing')
    report = aifinger.AIFingerReport(path='missing')
    aifinger.check_resolution_fps(Path('missing'), report)
    assert report.strong_count == 1
    probe['streams'][0]['r_frame_rate'] = '30/1'
    report = aifinger.AIFingerReport(path='missing')
    aifinger.check_resolution_fps(Path('missing'), report)
    assert report.strong_count == 0


def test_finalizer_and_signature_integration(tmp_path) -> None:
    import shutil
    import subprocess
    import pytest
    from bot import aifinger
    from bot.mp4finalize import resolve_reference, reference_meta, boxes
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('ffmpeg/ffprobe required')
    try:
        reference = resolve_reference(MediaPrepCfg().reference_mov)
    except FileNotFoundError:
        pytest.skip('reference MOV required')
    src = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'testsrc=size=128x192:rate=24', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=48000', '-t', '0.5',
                    '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', str(src)], check=True, capture_output=True)
    result = MediaPrep(humanize=False, normalize_loudness=False,
                       output_dir=tmp_path, reference_mov=reference).prepare(src)
    assert result.ok, result.error
    assert Path(result.dst).suffix == '.mov'
    assert result.size_bytes == Path(result.dst).stat().st_size
    assert aifinger.looks_iPhone(result.dst)
    probe = aifinger._ffprobe_json(Path(result.dst))
    assert 'encoder' not in probe['format']['tags']
    assert probe['streams'][0]['width'] == 128
    # Default metadata is byte-identical; custom values rebuild only metadata boxes.
    buf = reference.read_bytes()
    original = next(buf[o:e] for _, end, kind, body in boxes(buf) if kind == b'moov'
                    for o, e, typ, _ in boxes(buf, body, end) if typ == b'meta')
    values = {'make': 'Apple', 'model': 'iPhone 15 Pro Max', 'software': '26.6',
              'full-frame-rate-playback-intent': '0'}
    assert reference_meta(reference, values) == original
    assert b'Custom Model' in reference_meta(reference, {**values, 'model': 'Custom Model'})
    probe['streams'][0]['tags']['vendor_id'] = 'FFMP'
    from unittest.mock import patch
    with patch.object(aifinger, '_ffprobe_json', return_value=probe):
        assert not aifinger.looks_iPhone(result.dst)


def test_finalizer_rejects_invalid_boxes():
    import pytest
    from bot.mp4finalize import boxes
    with pytest.raises(ValueError):
        list(boxes(b'\x00\x00\x00\x20ftyp'))
