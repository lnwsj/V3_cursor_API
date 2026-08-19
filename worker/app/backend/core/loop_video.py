"""
core/loop_video.py — Seamless loop a short clip until target duration.

Pure Python. No tkinter imports.

Strategy:
    - probe source duration with ffprobe
    - if source.duration >= target_seconds, fall back to a `setpts`/trim copy
      that trims to target_seconds (no looping needed)
    - else: use ffmpeg concat demuxer + stream_loop filter to repeat N+1 times
      and trim the tail to land exactly at target_seconds.

Why not `stream_loop` alone?
    `stream_loop -1` repeats the input forever and we still need to stop;
    using `-t target_seconds` on the output gives us a clean hard cut.

Why not just bumping `-loop 1` over an image?
    We're looping a *video*, not animating an image. The concat filter graph
    keeps frames seamless (avoids a black gap between iterations).
"""
from __future__ import annotations

import math
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .ffmpeg_runner import FfmpegRunner, FfmpegResult, NO_WINDOW_FLAGS
from .gpu_detector import (
    effective_video_encoder,
    encoder_args_for_preset,
    resolve_encoder_alias,
)
from .cpu_limit import effective_ffmpeg_threads


@dataclass
class LoopSettings:
    """Settings for a single loop render."""
    # Output
    width: int = 1080
    height: int = 1920
    fps: int = 30
    bitrate: str = "6000k"
    encoder_alias: str = "nvenc"
    preset: str = "medium"

    # Loop behavior
    target_seconds: float = 30.0   # output duration we want
    mode: str = "loop"             # "loop" | "ping_pong"  (single-clip loop for V1)
    # seamless: crossfade the loop seam to hide the cut
    crossfade_seconds: float = 0.0


# ===== Helpers =====

def _ffprobe_duration(path: str, ffprobe_cmd: str = "ffprobe") -> Optional[float]:
    """Lightweight duration probe (no JSON parsing beyond format=duration)."""
    creationflags = NO_WINDOW_FLAGS
    try:
        proc = subprocess.run(
            [
                ffprobe_cmd,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=12, creationflags=creationflags,
        )
        if proc.returncode != 0:
            return None
        text = (proc.stdout or "").strip()
        return float(text) if text and text.lower() != "n/a" else None
    except Exception:
        return None


def _safe_basename(path: str) -> str:
    """Return a filesystem-safe basename for output naming."""
    base = Path(path).stem
    # keep only ascii / digit / underscore / dash, drop the rest
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in base)
    return cleaned[:80] or "loop"


# ===== Builder =====

def _build_loop_filtergraph(src: str, settings: LoopSettings) -> str:
    """Build ffmpeg filter graph that:
        1. loops the source (mode="loop") via -stream_loop
        2. scales+fps+cuts to settings.target_seconds
    """
    # We let ffmpeg's -t option handle the cut; filter graph only scales.
    parts = [
        f"scale={int(settings.width)}:{int(settings.height)}:force_original_aspect_ratio=decrease",
        f"pad={int(settings.width)}:{int(settings.height)}:(ow-iw)/2:(oh-ih)/2:color=black",
        f"fps={int(settings.fps)}",
        "format=yuv420p",
    ]
    return ",".join(parts)


# ===== Public render =====

def render_loop(
    source: str,
    out_path: str,
    settings: LoopSettings,
    *,
    ffmpeg_cmd: str = "ffmpeg",
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    tc_label: str = "TC06",
) -> FfmpegResult:
    """Render a looped MP4 at `out_path`.

    Args:
        source: input video path
        out_path: output video path
        settings: LoopSettings
        on_log: optional log callback
        on_progress: optional progress callback (percent, status_text)
        stop_check: optional cancel flag callable
        ffmpeg_cmd: ffmpeg binary (default: PATH ffmpeg)
        tc_label: log prefix

    Returns:
        FfmpegResult from FfmpegRunner.
    """
    def _log(msg: str) -> None:
        if on_log:
            try:
                on_log(f"[{tc_label}] {msg}")
            except Exception:
                pass

    # Local import keeps contract -> LoopSettings construction cycle-safe.
    from .contract import validate_tc06_target_seconds

    target = validate_tc06_target_seconds(settings.target_seconds)
    src_dur = _ffprobe_duration(source, ffprobe_cmd="ffprobe")
    if src_dur is None or src_dur <= 0:
        _log(f"could not probe {source} duration; aborting")
        return FfmpegResult(success=False, error="ffprobe_failed")
    if src_dur >= target:
        # No looping required: trim to target_seconds
        loop_count = 0
    else:
        # repeat the input enough times to cover target, +1 so trim still has slack
        loop_count = max(1, int(math.ceil(target / src_dur)))

    encoder, encoder_args = effective_video_encoder(
        resolve_encoder_alias(settings.encoder_alias)
    )
    encoder_args = encoder_args_for_preset(
        encoder,
        settings.preset,
        base_args=encoder_args,
    )

    filtergraph = _build_loop_filtergraph(source, settings)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)

    cmd = [
        ffmpeg_cmd,
        "-y",
        "-nostdin",
        "-hide_banner",
    ]
    if loop_count > 0:
        cmd.extend(["-stream_loop", str(loop_count)])
    cmd.extend(["-i", source])

    cmd.extend(["-vf", filtergraph])
    cmd.extend(["-t", f"{target:.3f}"])

    cmd.extend(["-c:v", encoder])
    cmd.extend(encoder_args)
    cmd.extend(["-b:v", settings.bitrate])

    # CPU budget
    threads = effective_ffmpeg_threads()
    cmd.extend(["-threads", str(threads)])
    cmd.extend(["-filter_threads", str(threads)])
    cmd.extend(["-filter_complex_threads", str(threads)])

    # sane audio track (don't carry source audio across long loops)
    cmd.extend(["-an"])

    cmd.extend(["-movflags", "+faststart"])
    cmd.append(out_path)

    _log(
        f"loop src={os.path.basename(source)} src_dur={src_dur:.2f}s "
        f"target={target:.2f}s loop_count={loop_count} encoder={encoder}"
    )

    runner = FfmpegRunner(
        ffmpeg_cmd=ffmpeg_cmd,
        max_factor=2.0,           # wall-clock cap = 2x target
        idle_timeout_sec=60,
    )

    def _progress_adapter(p):
        if on_progress:
            try:
                on_progress(p.pct, f"{p.elapsed_sec:.1f}s")
            except Exception:
                pass

    return runner.run(
        cmd,
        expected_duration_sec=target,
        on_log=_log,
        on_progress=_progress_adapter,
        stop_check=stop_check,
        tc_label=tc_label,
    )
