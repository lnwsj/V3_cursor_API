"""Assemble TC01 chroma outputs to the exact duration of an external audio."""
from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .cpu_limit import effective_ffmpeg_threads
from .ffmpeg_runner import FfmpegResult, FfmpegRunner, NO_WINDOW_FLAGS
from .gpu_detector import (
    effective_video_encoder,
    encoder_args_for_preset,
    resolve_encoder_alias,
)
from .encoder_recovery import remove_partial, should_retry_with_cpu
from .green_render import GreenSettings


@dataclass(frozen=True)
class ClipUse:
    path: str
    duration: float


def _probe_duration(path: str, selector: str, ffprobe_cmd: str = "ffprobe") -> Optional[float]:
    try:
        proc = subprocess.run(
            [
                ffprobe_cmd,
                "-v", "error",
                "-select_streams", selector,
                "-show_entries", "stream=duration:format=duration",
                "-of", "json",
                path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=NO_WINDOW_FLAGS,
        )
        if proc.returncode != 0:
            return None
        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams") or []
        candidates = [stream.get("duration") for stream in streams]
        candidates.append((payload.get("format") or {}).get("duration"))
        for raw in candidates:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                return value
    except Exception:
        return None
    return None


def probe_video_duration(path: str, ffprobe_cmd: str = "ffprobe") -> Optional[float]:
    return _probe_duration(path, "v:0", ffprobe_cmd)


def probe_audio_duration(path: str, ffprobe_cmd: str = "ffprobe") -> Optional[float]:
    return _probe_duration(path, "a:0", ffprobe_cmd)


def plan_clip_sequence(
    clips: Sequence[str],
    target_seconds: float,
    *,
    allow_reuse: bool,
    duration_probe: Callable[[str], Optional[float]] = probe_video_duration,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[List[ClipUse], float]:
    if isinstance(allow_reuse, bool) is False:
        raise TypeError("allow_clip_reuse must be a boolean")
    try:
        target = float(target_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("audio master duration must be a finite positive number") from exc
    if not math.isfinite(target) or target <= 0:
        raise ValueError("audio master duration must be a finite positive number")
    if not clips:
        raise ValueError("TC06 has no TC01 chroma outputs to assemble")

    pool: List[ClipUse] = []
    for path in clips:
        # FIX (B-21, 2026-07-31): honour stop_check during the duration-probe
        # loop so the user can cancel between chroma outputs instead of
        # waiting for the entire planning phase to finish.
        if stop_check is not None and stop_check():
            raise RuntimeError("audio-master planning cancelled by user")
        duration = duration_probe(str(path))
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"cannot probe TC01 chroma output duration: {path}")
        pool.append(ClipUse(str(path), float(duration)))

    unique_total = sum(item.duration for item in pool)
    if not allow_reuse and unique_total + 1e-6 < target:
        raise ValueError(
            "clip reuse is disabled and unique TC01 outputs are too short: "
            f"available={unique_total:.3f}s audio={target:.3f}s"
        )

    sequence: List[ClipUse] = []
    covered = 0.0
    index = 0
    while covered + 1e-6 < target:
        if not allow_reuse and index >= len(pool):
            break
        item = pool[index % len(pool)]
        sequence.append(item)
        covered += item.duration
        index += 1
    if covered + 1e-6 < target:
        raise ValueError(
            f"TC06 video coverage is too short: covered={covered:.3f}s audio={target:.3f}s"
        )
    return sequence, covered


def _ffconcat_line(path: str) -> str:
    normalized = str(Path(path).resolve()).replace("\\", "/")
    escaped = normalized.replace("'", "'\\''")
    return f"file '{escaped}'"


def render_audio_master(
    clips: Sequence[str],
    audio: str,
    out_path: str,
    settings: GreenSettings,
    *,
    allow_reuse: bool,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> FfmpegResult:
    log = on_log or (lambda _message: None)
    audio_duration = probe_audio_duration(audio, ffprobe_cmd)
    if audio_duration is None:
        return FfmpegResult(success=False, error=f"cannot probe audio duration: {audio}")
    try:
        sequence, covered = plan_clip_sequence(
            clips,
            audio_duration,
            allow_reuse=allow_reuse,
            duration_probe=lambda path: probe_video_duration(path, ffprobe_cmd),
            # FIX (B-21, 2026-07-31): forward stop_check so the planning
            # phase can be cancelled between clips.
            stop_check=stop_check,
        )
    except RuntimeError as exc:
        # Planning-phase cancellation: surface to caller as cancelled.
        log(f"⏹ audio-master planning cancelled: {exc}")
        return FfmpegResult(success=False, error=str(exc), cancelled=True)
    except (TypeError, ValueError) as exc:
        return FfmpegResult(success=False, error=str(exc))

    output = Path(out_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    manifest = output.parent / f".{output.stem}.{token}.ffconcat"
    partial = output.parent / f".{output.stem}.{token}.partial.mp4"
    manifest.write_text(
        "ffconcat version 1.0\n"
        + "\n".join(_ffconcat_line(item.path) for item in sequence)
        + "\n",
        encoding="utf-8",
    )

    encoder, encoder_args = effective_video_encoder(
        resolve_encoder_alias(settings.encoder_alias),
        ffmpeg_cmd=ffmpeg_cmd,
    )
    encoder_args = encoder_args_for_preset(
        encoder,
        settings.preset,
        base_args=encoder_args,
    )
    threads = effective_ffmpeg_threads()
    def build_command(
        selected_encoder: str,
        selected_args: Sequence[str],
        destination: Path,
    ) -> list[str]:
        return [
            ffmpeg_cmd,
            "-y",
            "-nostdin",
            "-hide_banner",
            "-f", "concat",
            "-safe", "0",
            "-i", str(manifest),
            "-i", str(Path(audio).resolve()),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-vf", f"fps={int(settings.fps)},format=yuv420p",
            "-t", f"{audio_duration:.6f}",
            "-c:v", selected_encoder,
            *selected_args,
            "-b:v", str(settings.bitrate),
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",
            "-threads", str(threads),
            "-filter_threads", str(threads),
            "-filter_complex_threads", str(threads),
            "-shortest",
            "-movflags", "+faststart",
            str(destination),
        ]

    cmd = build_command(encoder, encoder_args, partial)
    log(
        f"[TC06] audio-master clips={len(sequence)} unique={len(set(item.path for item in sequence))} "
        f"covered={covered:.3f}s audio={audio_duration:.3f}s reuse={allow_reuse}"
    )
    runner = FfmpegRunner(ffmpeg_cmd=ffmpeg_cmd, max_factor=4.0, idle_timeout_sec=90)

    def progress_adapter(progress) -> None:
        if on_progress:
            on_progress(float(getattr(progress, "pct", 0.0)), f"{getattr(progress, 'elapsed_sec', 0.0):.1f}s")

    temporaries = [manifest, partial]
    try:
        result = runner.run(
            cmd,
            expected_duration_sec=audio_duration,
            on_log=log,
            on_progress=progress_adapter,
            stop_check=stop_check,
            tc_label="TC06",
        )
        if should_retry_with_cpu(cmd, result, stop_check=stop_check):
            gpu_error = result.error or f"hardware encoder exited {result.returncode}"
            remove_partial(partial)
            cpu_partial = output.parent / f".{output.stem}.{token}.cpu.partial.mp4"
            temporaries.append(cpu_partial)
            cpu_encoder, cpu_args = effective_video_encoder(
                resolve_encoder_alias("libx264"),
                ffmpeg_cmd=ffmpeg_cmd,
                disable_fallback=True,
            )
            if cpu_encoder != "libx264":
                raise RuntimeError("exact libx264 CPU fallback is unavailable")
            cpu_args = encoder_args_for_preset(
                cpu_encoder,
                settings.preset,
                base_args=cpu_args,
            )
            cmd = build_command(cpu_encoder, cpu_args, cpu_partial)
            log(
                "[TC06] hardware encoder failed during audio-master render; "
                "retrying once with libx264"
            )
            result = FfmpegRunner(
                ffmpeg_cmd=ffmpeg_cmd,
                max_factor=4.0,
                idle_timeout_sec=90,
            ).run(
                cmd,
                expected_duration_sec=audio_duration,
                on_log=log,
                on_progress=progress_adapter,
                stop_check=stop_check,
                tc_label="TC06",
            )
            if not result.success:
                result.error = (
                    f"hardware encoder failed: {gpu_error}; "
                    f"CPU fallback failed: {result.error or result.returncode}"
                )
            partial = cpu_partial
        if not result.success:
            return result
        actual = probe_video_duration(str(partial), ffprobe_cmd)
        tolerance = max(0.15, 2.0 / max(1, int(settings.fps)))
        if actual is None or abs(actual - audio_duration) > tolerance:
            return FfmpegResult(
                success=False,
                returncode=result.returncode,
                error=(
                    "audio-master output duration mismatch: "
                    f"actual={actual!r} expected={audio_duration:.6f} tolerance={tolerance:.3f}"
                ),
            )
        os.replace(partial, output)
        result.output_path = str(output)
        result.duration_sec = actual
        return result
    finally:
        for temporary in temporaries:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
