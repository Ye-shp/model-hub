"""H.264/AAC QuickTime preparation with configurable filters and metadata finalization."""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MediaPrepCfg
from .mp4finalize import finalize, resolve_reference

log = logging.getLogger("bot.mediaprep")


@dataclass
class PrepResult:
    """Result of a media preparation run."""
    ok: bool
    src: str
    dst: str
    duration_s: float = 0.0
    size_bytes: int = 0
    filters_applied: list[str] = field(default_factory=list)
    audio_normalised: bool = False
    watermark_removed: bool = False
    error: str = ""

    def summary(self) -> str:
        status = "OK" if self.ok else f"ERR ({self.error})"
        parts = [f"{status}", f"{self.src} → {self.dst}"]
        if self.ok:
            mb = self.size_bytes / 1_048_576
            parts.append(f"{mb:.1f} MB")
            if self.watermark_removed:
                parts.append("watermark removed")
            if self.audio_normalised:
                parts.append("audio normalised")
            if self.filters_applied:
                parts.append(f"filters: {', '.join(self.filters_applied)}")
        return " | ".join(parts)


class MediaPrepError(Exception):
    """Raised when the media-prep pipeline fails (missing ffmpeg, encode error, etc.)."""


def _normalise_regions(regions):
    """Accept [x,y,w,h] lists or {x,y,w,h} dicts; return a list of dicts."""
    out: list[dict[str, int]] = []
    for r in regions or []:
        if isinstance(r, dict):
            out.append({"x": int(r.get("x", 0)), "y": int(r.get("y", 0)),
                        "w": int(r.get("w", 100)), "h": int(r.get("h", 50))})
        elif isinstance(r, (list, tuple)) and len(r) == 4:
            x, y, w, h = (int(v) for v in r)
            out.append({"x": x, "y": y, "w": w, "h": h})
    return out


class MediaPrep:
    """Builds and runs the ffmpeg media-preparation pipeline."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        # C2PA / metadata
        strip_c2pa: bool = True,
        strip_metadata: bool = True,
        encoder_tag: str = "H.264",
        # Watermark removal
        remove_watermark: bool = False,
        watermark_regions: list[dict[str, int]] | None = None,
        # Humanisation
        humanize: bool = True,
        grain_strength: int = 2,
        vignette: bool = True,
        contrast_shift: float = 0.01,
        brightness_shift: float = 0.005,
        sharpen: bool = True,
        # Audio
        normalize_loudness: bool = True,
        target_loudness: float = -14.0,
        true_peak: float = -1.5,
        lra: float = 11.0,
        highpass_hz: int = 80,
        # Output codec
        video_codec: str = "libx264",
        crf: int = 20,
        preset: str = "slow",
        audio_bitrate: str = "192k",
        target_fps: float = 30.0,
        pix_fmt: str = "yuv420p",
        output_dir: str | Path = "state/media_prepped",
        workdir: str | Path = "tmp",
        output_ext: str = '.mov',
        major_brand: str = 'qt',
        color_space: str = 'bt709',
        color_primaries: str = 'bt709',
        color_trc: str = 'bt709',
        color_range: str = 'tv',
        gop_size: int = 28,
        keyint_min: int = 24,
        reference_mov: str | Path | None = None,
        apple_make: str = 'Apple',
        apple_model: str = 'iPhone 15 Pro Max',
        apple_software: str = '26.6',
        apple_ffrate_intent: str = '0',
        dry_run: bool = False,
    ) -> None:
        self.output_ext = output_ext
        self.major_brand = major_brand
        self.color_space = color_space
        self.color_primaries = color_primaries
        self.color_trc = color_trc
        self.color_range = color_range
        self.gop_size = gop_size
        self.keyint_min = keyint_min
        self.apple_make = apple_make
        self.apple_model = apple_model
        self.apple_software = apple_software
        self.apple_ffrate_intent = apple_ffrate_intent
        self.reference_mov = reference_mov if reference_mov is not None else MediaPrepCfg().reference_mov
        self.enabled = enabled
        self.ffmpeg = ffmpeg_bin
        self.ffprobe = ffprobe_bin
        self.strip_c2pa = strip_c2pa
        self.strip_metadata = strip_metadata
        self.encoder_tag = encoder_tag
        self.remove_watermark = remove_watermark
        # normalise watermark regions: accept both [x,y,w,h] lists and {x,y,w,h} dicts
        self.watermark_regions = _normalise_regions(watermark_regions)
        self.output_dir = output_dir
        self.workdir = workdir
        self.dry_run = dry_run
        self.target_fps = target_fps
        self.humanize = humanize
        self.grain_strength = grain_strength
        self.vignette = vignette
        self.contrast_shift = contrast_shift
        self.brightness_shift = brightness_shift
        self.sharpen = sharpen
        self.normalize_loudness = normalize_loudness
        self.target_loudness = target_loudness
        self.true_peak = true_peak
        self.lra = lra
        self.highpass_hz = highpass_hz
        self.video_codec = video_codec
        self.crf = crf
        self.preset = preset
        self.audio_bitrate = audio_bitrate
        self.pix_fmt = pix_fmt

    # ── filter-chain builders ──────────────────────────────────────────────────

    def _video_filters(self) -> list[str]:
        """Build the ordered list of video filter names (for reporting)."""
        vfilters: list[str] = []
        if self.remove_watermark and self.watermark_regions:
            vfilters.append(f"delogo ({len(self.watermark_regions)} region(s))")
        if self.humanize:
            if self.grain_strength > 0:
                vfilters.append(f"noise (grain={self.grain_strength})")
            if self.vignette:
                vfilters.append("vignette")
            if self.contrast_shift or self.brightness_shift:
                vfilters.append("eq (contrast/brightness micro-shift)")
            if self.sharpen:
                vfilters.append("unsharp")
        return vfilters

    def _frame_size(self, src: Path) -> tuple[int, int] | None:
        """Probe the source frame size (W, H) so watermark regions can be clamped."""
        try:
            r = subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
                return w, h
        except Exception:
            pass
        return None

    def _build_vf_chain(self, src: Path | None = None) -> str:
        """Build the -vf filter chain string."""
        parts: list[str] = []
        size = self._frame_size(src) if (src is not None and self.remove_watermark) else None

        # 1. delogo (watermark removal) — first, before colour adjustments
        if self.remove_watermark:
            regions = self.watermark_regions
            if not regions:
                  # Default: bottom-right 13% × 6% region (where most watermarks are).
                  # delogo needs integer pixels, so compute from the real frame size.
                  if size is not None:
                      W, H = size
                      w = max(2, int(W * 0.13))
                      h = max(2, int(H * 0.06))
                      x = max(0, W - 2 - w)
                      y = max(0, H - 2 - h)
                  else:
                      # Unknown size (e.g. stream input): safe 1080×1920 vertical assumption.
                      x, y, w, h = 918, 1766, 140, 115
                  parts.append(f"delogo=x={x}:y={y}:w={w}:h={h}")
            else:
                for r in regions:
                    x = int(r.get("x", 0)); y = int(r.get("y", 0))
                    w = int(r.get("w", 100)); h = int(r.get("h", 50))
                    # Clamp to the actual frame so delogo never runs off the edge.
                    if size is not None:
                        W, H = size
                        w = max(2, min(w, W - 2))
                        x = max(0, min(x, W - 2 - w))
                        h = max(2, min(h, H - 2))
                        y = max(0, min(y, H - 2 - h))
                    parts.append(f"delogo=x={x}:y={y}:w={w}:h={h}")

        # 2. eq (micro contrast/brightness shift) — subtle, breaks "perfect AI" look
        if self.humanize and (self.contrast_shift or self.brightness_shift):
            contrast = 1.0 + self.contrast_shift
            brightness = self.brightness_shift
            parts.append(f"eq=contrast={contrast:.4f}:brightness={brightness:.4f}")

        # 3. noise (temporal grain) — breaks perfectly smooth AI gradients
        if self.humanize and self.grain_strength > 0:
            # alls: all-signal strength (0-10); allf: temporal mode
            s = max(1, min(10, self.grain_strength))
            parts.append(f"noise=alls={s}:allf=t")

        # 4. unsharp (light sharpening) — cleans up any softness from re-encode
        if self.humanize and self.sharpen:
            parts.append("unsharp=5:5:0.3")

        # 5. vignette — adds depth, breaks flat AI lighting
        if self.humanize and self.vignette:
            parts.append("vignette=angle=PI/7")

        if self.target_fps:
            parts.append(f"fps={self.target_fps:g}")
        parts.append(f"setparams=colorspace={self.color_space}:color_primaries={self.color_primaries}:color_trc={self.color_trc}:range={self.color_range}")
        return ",".join(parts)

    def _build_af_chain(self) -> str:
        """Build the -af filter chain string."""
        parts: list[str] = []
        if self.normalize_loudness:
            parts.append(
                f"loudnorm=I={self.target_loudness:.1f}:TP={self.true_peak:.1f}:LRA={self.lra:.1f}"
            )
            # Remove DC offset and low rumble
            parts.append(f"highpass=f={self.highpass_hz}")
        return ",".join(parts)

    # ── main pipeline ──────────────────────────────────────────────────────────

    def build_command(self, src: Path, dst: Path) -> list[str]:
        """Build the full ffmpeg command (without running it)."""
        cmd: list[str] = [self.ffmpeg, "-y", "-hide_banner", "-i", str(src)]

        # Video filters
        vf = self._build_vf_chain(src)
        if vf:
            cmd += ["-vf", vf]

        # Audio filters
        af = self._build_af_chain()
        if af:
            cmd += ["-af", af]

        cmd += ["-map_metadata", "-1", "-fflags", "+bitexact", "-flags", "+bitexact"]
        cmd += ["-profile:v", "high", "-tag:v", "avc1",
                "-g", str(self.gop_size), "-keyint_min", str(self.keyint_min),
                "-x264-params", "log-level=error"]
        for stream, handler in (("v", "Core Media Video"), ("a", "Core Media Audio")):
            cmd += [f"-metadata:s:{stream}:0", f"handler_name={handler}",
                    f"-metadata:s:{stream}:0", "language=und"]
        cmd += ["-metadata:s:v:0", f"encoder={self.encoder_tag}"]

        # Video codec
        cmd += [
            "-c:v", self.video_codec,
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-pix_fmt", self.pix_fmt,
        ]

        # Audio codec
        cmd += ["-c:a", "aac", "-b:a", self.audio_bitrate]

        # Faststart for web
        cmd += ["-movflags", "+faststart", "-brand", self.major_brand.ljust(4), "-f", "mov"]

        cmd.append(str(dst))
        return cmd

    def prepare(
        self,
        src: str | Path,
        dst: str | Path | None = None,
        *,
        platform: str = "tiktok",
    ) -> PrepResult:
        """Run the full media preparation pipeline."""
        src_p = Path(src)
        if self.dry_run:
            return PrepResult(ok=True, src=str(src_p), dst=str(src_p),
                             size_bytes=src_p.stat().st_size if src_p.exists() else 0,
                             filters_applied=[], audio_normalised=False, watermark_removed=False)
        if not self.enabled:
            return PrepResult(ok=True, src=str(src_p), dst=str(src_p),
                             size_bytes=src_p.stat().st_size if src_p.exists() else 0,
                             filters_applied=[], audio_normalised=False, watermark_removed=False)
        if dst is None:
            base = Path(self.output_dir or "state/media_prepped")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst_p = base / f"{src_p.stem}-prepped-{stamp}{self.output_ext}"
        else:
            dst_p = Path(dst).with_suffix(self.output_ext)
        dst_p.parent.mkdir(parents=True, exist_ok=True)

        if src_p.resolve() == dst_p.resolve():
            raise MediaPrepError("source and destination must differ")

        cmd = self.build_command(src_p, dst_p)

        log.info("mediaprep: %s", " ".join(cmd))

        try:
            reference = resolve_reference(self.reference_mov)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                err = (r.stderr or "").strip()[-500:]
                return PrepResult(ok=False, src=str(src), dst=str(dst_p), error=err)

            finalize(dst_p, reference, {
                "make": self.apple_make, "model": self.apple_model,
                "software": self.apple_software,
                "full-frame-rate-playback-intent": str(self.apple_ffrate_intent),
            })
            size = dst_p.stat().st_size if dst_p.exists() else 0
            vf_list = self._video_filters()
            return PrepResult(
                ok=True,
                src=str(src),
                dst=str(dst_p),
                size_bytes=size,
                filters_applied=vf_list,
                audio_normalised=self.normalize_loudness,
                watermark_removed=self.remove_watermark and bool(self.watermark_regions or True),
            )

        except subprocess.TimeoutExpired:
            return PrepResult(ok=False, src=str(src), dst=str(dst_p), error="ffmpeg timed out (300s)")
        except Exception as e:
            return PrepResult(ok=False, src=str(src), dst=str(dst_p), error=str(e))


    @classmethod
    def from_config(cls, cfg, dry_run: bool = False) -> "MediaPrep":
        """Build a MediaPrep from a config.MediaPrepCfg (names match 1:1)."""
        return cls(
            ffmpeg_bin=cfg.ffmpeg_bin,
            ffprobe_bin=cfg.ffprobe_bin,
            strip_c2pa=cfg.strip_c2pa,
            strip_metadata=cfg.strip_metadata,
            encoder_tag=cfg.encoder_tag,
            remove_watermark=cfg.remove_watermark,
            watermark_regions=cfg.watermark_regions,
            humanize=cfg.humanize,
            grain_strength=cfg.grain_strength,
            vignette=cfg.vignette,
            contrast_shift=cfg.contrast_shift,
            brightness_shift=cfg.brightness_shift,
            target_fps=getattr(cfg, "target_fps", 30.0),
            enabled=cfg.enabled,
            normalize_loudness=cfg.normalize_loudness,
            target_loudness=cfg.loudness_I,
            true_peak=cfg.loudness_TP,
            lra=cfg.loudness_LRA,
            highpass_hz=cfg.highpass_hz,
            crf=cfg.crf,
            preset=cfg.preset,
            audio_bitrate=cfg.audio_bitrate,
            pix_fmt=getattr(cfg, "pix_fmt", "yuv420p"),
            output_dir=cfg.output_dir,
            workdir=cfg.workdir,
            sharpen=cfg.sharpen,
            output_ext=getattr(cfg, "output_ext", MediaPrepCfg().output_ext),
            major_brand=getattr(cfg, "major_brand", MediaPrepCfg().major_brand),
            color_space=getattr(cfg, "color_space", MediaPrepCfg().color_space),
            color_primaries=getattr(cfg, "color_primaries", MediaPrepCfg().color_primaries),
            color_trc=getattr(cfg, "color_trc", MediaPrepCfg().color_trc),
            color_range=getattr(cfg, "color_range", MediaPrepCfg().color_range),
            gop_size=getattr(cfg, "gop_size", MediaPrepCfg().gop_size),
            keyint_min=getattr(cfg, "keyint_min", MediaPrepCfg().keyint_min),
            reference_mov=getattr(cfg, "reference_mov", MediaPrepCfg().reference_mov),
            apple_make=getattr(cfg, "apple_make", MediaPrepCfg().apple_make),
            apple_model=getattr(cfg, "apple_model", MediaPrepCfg().apple_model),
            apple_software=getattr(cfg, "apple_software", MediaPrepCfg().apple_software),
            apple_ffrate_intent=getattr(cfg, "apple_ffrate_intent", MediaPrepCfg().apple_ffrate_intent),
            dry_run=dry_run,
        )
