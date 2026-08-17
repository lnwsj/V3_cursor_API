"""
GreenRender — Chroma-key + compositing engine (local, independent).

Pattern ported from:
  - green.sj88ai.com/core/media_ffmpeg.py::build_ffmpeg_cmd
  - green.sj88ai.com/services/ffmpeg_commands.py::build_composite_preview_command

Workflow:
  - เปลี่ยนพื้นหลัง "cover" (เช่น green screen) เป็น "background" + ใส่ "product" ทับ
  - ใช้ ffmpeg filter: scale/pad/chromakey/despill/overlay
  - รองรับ GPU (h264_nvenc) + CPU (libx264) auto-detect
  - รองรับ:
      * Cover overlay (intro card)
      * Audio passthrough (product audio หรือ background audio)
      * Preview (1-frame composite)
"""
import json
import math
import os
import re
import subprocess
import time
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .gpu_detector import (
    effective_video_encoder,
    encoder_args_for_preset,
    resolve_encoder_alias,
)
from .ffmpeg_runner import FfmpegRunner, FfmpegResult, FfmpegProgress, NO_WINDOW_FLAGS
from .cpu_limit import effective_ffmpeg_threads
from .media_probe import (
    MediaProbeCancelled,
    MediaStreamState,
    _run_probe as _run_media_probe,
    audio_stream_state,
    has_video_stream,
)
from .encoder_recovery import (
    command_video_encoder,
    remove_partial,
    should_retry_with_cpu,
)


# ==================== Config ====================

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
OUTPUT_DURATION_FRAME_ALLOWANCE = 2.0
OUTPUT_DURATION_MIN_TOLERANCE_SEC = 0.01
OUTPUT_DURATION_MAX_TOLERANCE_SEC = 0.10
OUTPUT_DURATION_FALLBACK_FPS = 30.0

@dataclass
class GreenSettings:
    """ค่าตั้ง chroma-key + render (อ้างอิง defaultSettings จาก green.sj88ai.com/App.jsx)"""
    # Output
    width: int = 1080
    height: int = 1920
    fps: int = 30
    bitrate: str = "6000k"   # green.sj88ai.com config.example.json default
    encoder_alias: str = "nvenc"          # prefer h264_nvenc when preflight passes

    # Chroma key
    key_color: str = "#00FF00"     # hex ของสีที่จะเอาออก
    similarity: float = 0.29       # canonical TC01-TC04 profile
    blend: float = 0.04            # canonical TC01-TC04 profile
    despill: float = 0.32          # canonical TC01-TC04 profile
    despill_screen: bool = True    # True = screen blend, False = average

    # Overlay
    cover_enabled: bool = False    # แสดง intro card
    cover_duration: float = 2.0    # วินาที
    cover_scale: float = 1.0       # scale 0.1-1.0 ของ frame

    # Audio
    audio_source: str = "product"  # "product" | "background" | "none"

    # Misc
    preset: str = "medium"         # CPU fallback aligns with green.sj88ai.com


# ==================== Hex helpers ====================

def _hex_to_rgb0x(hex_color: str) -> str:
    """#RRGGBB -> 0xRRGGBB (ffmpeg chromakey format)"""
    c = (hex_color or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        c = "00FF00"
    return f"0x{c.upper()}"


# ==================== Builder ====================

# FIX (B-07, 2026-07-31): protect cache from concurrent writes via a lock
# so multiple Worker threads do not spawn duplicate `ffmpeg -filters`
# subprocesses on cold start. The cache key now also includes the binary's
# mtime_ns so an in-session binary swap (e.g. user installs NVENC) is
# detected instead of returning stale "no".
import threading as _threading

_FFMPEG_FILTER_CACHE: Dict[Tuple[Tuple[int, int, int], str], bool] = {}
_FILTER_CACHE_LOCK = _threading.Lock()


def _ffmpeg_binary_token(ffmpeg_cmd: str) -> Tuple[int, int, int]:
    """Return (mtime_ns, size, ino) for the binary. (0, 0, 0) on stat failure."""
    try:
        st = os.stat(ffmpeg_cmd)
        return (int(st.st_mtime_ns), int(st.st_size), int(st.st_ino))
    except OSError:
        return (0, 0, 0)


# FIX (2026-07-31): :mode= option was added to the despill filter in ffmpeg 8.0.
# Older ffmpeg rejects it with "Error applying option 'mode' to filter 'despill'".
# Probe once per ffmpeg command and cache so we only emit :mode= when supported.
_DESPILL_MODE_SUPPORTED: Dict[str, bool] = {}


def _ffmpeg_supports_despill_mode(ffmpeg_cmd: str) -> bool:
    """True when the active ffmpeg binary recognises `despill=:mode=`."""
    cached = _DESPILL_MODE_SUPPORTED.get(ffmpeg_cmd)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [ffmpeg_cmd, "-hide_banner", "-h", "filter=despill"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=NO_WINDOW_FLAGS,
        )
        supported = "mode" in (result.stdout or "")
    except Exception:
        supported = False
    _DESPILL_MODE_SUPPORTED[ffmpeg_cmd] = supported
    return supported


def _ffmpeg_has_filter(ffmpeg_cmd: str, filter_name: str) -> bool:
    """Return True when the active ffmpeg binary exposes a named filter."""
    token = _ffmpeg_binary_token(ffmpeg_cmd)
    key = (token, filter_name)
    with _FILTER_CACHE_LOCK:
        cached = _FFMPEG_FILTER_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [ffmpeg_cmd, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=NO_WINDOW_FLAGS,
        )
        output = f"{result.stdout}\n{result.stderr}"
        found = result.returncode == 0 and bool(
            re.search(rf"(^|\s){re.escape(filter_name)}\s", output, re.MULTILINE)
        )
    except Exception:
        found = False
    with _FILTER_CACHE_LOCK:
        _FFMPEG_FILTER_CACHE[key] = found
    return found


def _pick_despill_filter(enc_name: str, ffmpeg_cmd: str) -> str:
    if enc_name == "h264_nvenc" and _ffmpeg_has_filter(ffmpeg_cmd, "despill_cuda"):
        return "despill_cuda"
    return "despill"


def _probe_video_size(path: str, ffprobe_cmd: str = "ffprobe") -> Tuple[int, int]:
    if not path or not os.path.isfile(path):
        return 0, 0
    try:
        out = subprocess.check_output(
            [
                ffprobe_cmd, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x", path,
            ],
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW_FLAGS,
            timeout=10,
        ).strip()
        match = re.search(r"(\d+)\s*x\s*(\d+)", out)
        if not match:
            return 0, 0
        return int(match.group(1)), int(match.group(2))
    except Exception:
        return 0, 0


def _matches_output_aspect(path: str, out_w: int, out_h: int, ffprobe_cmd: str) -> bool:
    src_w, src_h = _probe_video_size(path, ffprobe_cmd)
    if src_w <= 0 or src_h <= 0 or out_w <= 0 or out_h <= 0:
        return False
    return abs((src_w / src_h) - (out_w / out_h)) < 0.01


def _is_image_media(path: str) -> bool:
    return Path(path or "").suffix.lower() in IMAGE_EXTS


def _despill_parameters(hex_color: str, requested_mix: float) -> Tuple[int, float]:
    """Return a safe ffmpeg despill type/mix for the selected key color.

    ffmpeg's despill filter supports green and blue only. Red, magenta,
    grayscale, and malformed keys still work with chromakey, but despill must
    be disabled instead of silently removing the wrong color channel.
    """
    c = (hex_color or "").strip().lstrip("#").lower()
    try:
        if len(c) != 6:
            raise ValueError
        red = int(c[0:2], 16)
        green = int(c[2:4], 16)
        blue = int(c[4:6], 16)
        mix = max(0.0, min(float(requested_mix), 1.0))
    except (TypeError, ValueError):
        return 0, 0.0
    if green > red and green >= blue:
        return 0, mix
    if blue > red and blue > green:
        return 1, mix
    return 0, 0.0


def _despill_type_int(hex_color: str) -> int:
    """Compatibility helper: 0=green, 1=blue."""
    return _despill_parameters(hex_color, 0.0)[0]


def build_render_command(
    cover: Optional[str],
    product: str,
    background: str,
    audio: Optional[str],
    out_path: str,
    settings: GreenSettings,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    chroma_max_parallel: int = 1,  # V1.0.0.7: divide CPU budget across concurrent chroma
    product_audio_state: Optional[MediaStreamState] = None,
    uploaded_audio_state: Optional[MediaStreamState] = None,
    target_duration_sec: Optional[float] = None,
    ping_pong_product_to_target: bool = False,
    disable_encoder_fallback: bool = False,
) -> Tuple[List[str], float]:
    """
    สร้าง ffmpeg command สำหรับ render
    Returns: (cmd, expected_duration_sec)
    """
    # 1) Resolve encoder
    enc_name, enc_args = effective_video_encoder(
        preferred=resolve_encoder_alias(settings.encoder_alias),
        ffmpeg_cmd=ffmpeg_cmd,
        disable_fallback=disable_encoder_fallback,
    )
    enc_args = encoder_args_for_preset(
        enc_name,
        settings.preset,
        base_args=enc_args,
    )

    # 2) Probe durations. TC01-TC03 stay Product-bound. TC04 opts in to an
    # Audio-derived final duration by passing target_duration_sec explicitly.
    product_dur = _probe_duration(product, ffprobe_cmd)
    if product_dur <= 0:
        raise ValueError("product duration is missing or zero")
    if target_duration_sec is None:
        expected = product_dur
    else:
        try:
            expected = float(target_duration_sec)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("target duration must be a finite positive number") from exc
        if not math.isfinite(expected) or expected <= 0:
            raise ValueError("target duration must be a finite positive number")

    has_cover = bool(cover and os.path.isfile(cover))
    if audio:
        if not os.path.isfile(audio):
            raise ValueError("uploaded audio file not found")
        if uploaded_audio_state is None:
            uploaded_audio_state = audio_stream_state(
                audio,
                ffprobe_cmd=ffprobe_cmd,
                ffmpeg_cmd=ffmpeg_cmd,
            )
        if uploaded_audio_state is MediaStreamState.ERROR:
            raise ValueError("uploaded audio probe failed")
        if uploaded_audio_state is not MediaStreamState.PRESENT:
            raise ValueError("uploaded audio has no readable audio stream")

    # 3) Build the input list.
    #   product  is input [0]
    #   background is input [1] — streamed in a loop so it covers the
    #   selected duration.
    #   cover    is input [2] (optional)
    #   audio    is input [3] (optional)
    inputs: List[str] = ["-i", product]
    product_idx = 0
    bg_idx = 1
    if _is_image_media(background):
        inputs += ["-loop", "1", "-framerate", str(settings.fps), "-i", background]
    else:
        inputs += ["-stream_loop", "-1", "-i", background]
    cover_idx = -1
    if has_cover:
        if _is_image_media(cover):
            inputs += ["-loop", "1", "-framerate", str(settings.fps), "-i", cover]
        else:
            inputs += ["-i", cover]
        cover_idx = 2
    audio_idx = -1
    if audio:
        inputs += ["-i", audio]
        # FIX (B-14, 2026-07-31): count input indices dynamically from the
        # final `inputs` list rather than hardcoding `3 if has_cover else 2`.
        # The previous formula assumed exactly 3 optional inputs after product
        # and background, which broke if a future caller introduced another
        # optional input (e.g. an extra audio track). Counting `-i` flags
        # inside `inputs` gives the audio input's real index.
        audio_idx = sum(1 for tok in inputs if tok == "-i") - 1

    # 4) Filter graph (use colorkey filter — simpler than chromakey+despill chain)
    #    colorkey does the green-screen removal in 1 filter
    key_hex = _hex_to_rgb0x(settings.key_color)
    sim = f"{settings.similarity:.3f}"
    blend = f"{settings.blend:.3f}"
    despill_type_int, effective_despill = _despill_parameters(settings.key_color, settings.despill)
    despill_mix = f"{effective_despill:.3f}"
    despill_filter = _pick_despill_filter(enc_name, ffmpeg_cmd)

    w, h = settings.width, settings.height
    fps = settings.fps

    cuda_requested = os.getenv("V3_GREEN_CUDA_FILTERS", "").strip() == "1"
    product_aspect_matches = _matches_output_aspect(product, w, h, ffprobe_cmd)
    background_aspect_matches = _matches_output_aspect(
        background, w, h, ffprobe_cmd
    )
    use_cuda_filters = (
        cuda_requested
        and enc_name == "h264_nvenc"
        # Stock FFmpeg must never enter the custom all-CUDA graph.  The
        # custom filter is the capability boundary for this path.
        and despill_filter == "despill_cuda"
        and not (settings.cover_enabled and has_cover)
        and not _is_image_media(background)
        and _ffmpeg_has_filter(ffmpeg_cmd, "scale_cuda")
        and _ffmpeg_has_filter(ffmpeg_cmd, "chromakey_cuda")
        # pad_cuda in the candidate FFmpeg n9 build can cross hardware frame
        # contexts.  Aspect-mismatched inputs remain on the proven CPU graph.
        and product_aspect_matches
        and background_aspect_matches
    )

    parts: List[str] = []
    product_input_label = f"[{product_idx}:v]"
    if ping_pong_product_to_target and expected > product_dur:
        # TC04 Audio-master Batch contract: the extracted source segment is a
        # cycle, not the final length. Repeat it forward/reverse until the
        # selected Audio target is covered, then trim exactly to that target.
        units = max(2, int(math.ceil(expected / product_dur)))
        if units > 1:
            units += 1
        split_labels = "".join(f"[fgps{index}]" for index in range(units))
        parts.append(
            f"[{product_idx}:v]trim=duration={product_dur:.3f},"
            f"setpts=PTS-STARTPTS,split={units}{split_labels}"
        )
        concat_inputs: List[str] = []
        for index in range(units):
            branch_label = f"fgp{index}"
            if index % 2:
                parts.append(
                    f"[fgps{index}]reverse,setpts=PTS-STARTPTS[{branch_label}]"
                )
            else:
                parts.append(f"[fgps{index}]setpts=PTS-STARTPTS[{branch_label}]")
            concat_inputs.append(f"[{branch_label}]")
        parts.append(
            f"{''.join(concat_inputs)}concat=n={units}:v=1:a=0,"
            f"trim=duration={expected:.3f},setpts=PTS-STARTPTS[product_looped]"
        )
        product_input_label = "[product_looped]"

    if use_cuda_filters:
        # Custom path: scale, key, and despill stay on the CUDA frame before a
        # single controlled download for the proven CPU overlay.
        parts.append(
            f"{product_input_label}"
            f"fps={fps},hwupload_cuda,"
            f"scale_cuda={w}:{h}:format=nv12,"
            f"chromakey_cuda=color={key_hex}:similarity={sim}:blend={blend},"
            f"despill_cuda=type={despill_type_int}:mix={despill_mix}:alpha=0,"
            f"hwdownload,format=yuva420p[fg]"
        )
        parts.append(
            f"[{bg_idx}:v]"
            f"fps={fps},hwupload_cuda,"
            f"scale_cuda={w}:{h}:format=nv12,"
            f"hwdownload,format=nv12,format=yuv420p[bg]"
        )
    else:
        # CPU path keeps aspect-safe pad behavior for non-16:9 sources and cover overlays.
        # FIX (2026-07-31): honor settings.despill_screen — emit :mode=screen|avg
        # so the field's contract semantics actually reach the ffmpeg despill filter.
        # Only emit :mode= when ffmpeg supports it (added in ffmpeg 8.0).
        despill_mode_kwargs = (
            f":mode={'screen' if settings.despill_screen else 'avg'}"
            if _ffmpeg_supports_despill_mode(ffmpeg_cmd)
            else ""
        )
        parts.append(
            f"{product_input_label}"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
            f"setsar=1,fps={fps},format=yuva420p,"
            f"chromakey={key_hex}:{sim}:{blend},"
            # CPU graph is intentionally self-contained.  The CUDA-only
            # filter is valid only in the guarded hardware-frame chain above.
            f"despill=type={despill_type_int}:mix={despill_mix}{despill_mode_kwargs}[fg]"
        )
        parts.append(
            f"[{bg_idx}:v]"
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black:eval=frame,"
            f"setsar=1,fps={fps},format=yuv420p[bg]"
        )

    # overlay fg on bg (CPU)
    parts.append("[bg][fg]overlay=0:0:eof_action=pass[base]")

    last_v = "[base]"

    # cover intro (optional) — overlay cover in first N seconds
    if settings.cover_enabled and has_cover:
        cover_scale_w = int(w * settings.cover_scale)
        cover_scale_h = -1  # preserve aspect
        parts.append(
            f"[{cover_idx}:v]scale={cover_scale_w}:{cover_scale_h},setsar=1[cv]"
        )
        # time-gated overlay (only enabled during the cover window)
        # A short final clip must retain visible Product content. Cover remains
        # capped at the configured 2s for normal clips, but can occupy at most
        # half of a short Product/segment.
        cover_end = min(settings.cover_duration, expected * 0.5)
        parts.append(
            f"{last_v}[cv]overlay=enable='between(t,0,{cover_end})':x=(W-w)/2:y=(H-h)/2[v]"
        )
        last_v = "[v]"

    # finalize video stream
    parts.append(f"{last_v}format=yuv420p[vout]")

    filter_complex = ";".join(parts)

    # 5) ffmpeg command
    # Audio is mapped outside of -filter_complex (ffmpeg infers the filtergraph
    # type from the first chain, so an audio filter chained after the video
    # chain raises "Stream specifier ':a' matches no streams").
    audio_arg = None
    audio_filter_arg = None
    # Verify the input really has an audio stream — the audio_source setting
    # alone doesn't guarantee the file contains audio.
    if settings.audio_source == "product":
        if product_audio_state is None:
            product_audio_state = audio_stream_state(
                product,
                ffprobe_cmd=ffprobe_cmd,
                ffmpeg_cmd=ffmpeg_cmd,
            )
        if product_audio_state is MediaStreamState.ERROR:
            raise ValueError("product audio probe failed")
        if product_audio_state is MediaStreamState.PRESENT:
            # Use the product's audio stream.
            audio_arg = ["-map", f"{product_idx}:a"]
            audio_filter_arg = ["-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"]
    elif settings.audio_source == "background" and audio and audio_idx >= 0:
        audio_arg = ["-map", f"{audio_idx}:a"]
        audio_filter_arg = [
            "-af",
            "aresample=48000:first_pts=0,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,apad",
        ]
    # audio_source == "none" or no audio stream in the input → do not map audio.

    cmd: List[str] = [
        ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "warning",
    ]
    # Keep CPU filter fallback in software frames. NVENC still uses the GPU for
    # encoding; forcing NVDEC here can hand hardware frames to software filters.
    cmd += [
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
    ]
    if audio_arg:
        cmd += audio_arg

    cmd += [
        "-c:v", enc_name,
        *enc_args,
        "-b:v", settings.bitrate,
        "-pix_fmt", "yuv420p",
        # V1.0.0.7: CPU% limiter now applies to BOTH frame threads and
        # filter graph threads. ffmpeg's `-threads` is a HINT that only
        # controls frame-level parallelism — the filter pipeline
        # (scale + chromakey + overlay + aresample) spawns its own
        # threads equal to the total core count, ignoring `-threads`.
        # `-filter_threads` + `-filter_complex_threads` cap the filter
        # graph to the same budget as the frame threads. The `chroma_max_parallel`
        # argument divides the budget across concurrent chroma processes
        # (sequential chroma = 1, parallel chroma = N).
        "-threads", str(effective_ffmpeg_threads(chroma_max_parallel)),
        "-filter_threads", str(effective_ffmpeg_threads(chroma_max_parallel)),
        "-filter_complex_threads", str(effective_ffmpeg_threads(chroma_max_parallel)),
    ]
    if audio_arg:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        if audio_filter_arg:
            cmd += audio_filter_arg
    cmd += [
        "-t", f"{expected:.3f}",
        "-movflags", "+faststart",
        out_path,
    ]
    return cmd, expected


def build_preview_command(
    cover: str,
    product: str,
    background: str,
    settings: GreenSettings,
    ffmpeg_cmd: str = "ffmpeg",
) -> List[str]:
    """
    สร้าง ffmpeg command สำหรับ preview — extract 1 frame composite ที่เวลา t=0.5s
    Output: PNG ไป stdout
    """
    key_hex = _hex_to_rgb0x(settings.key_color)
    sim = f"{settings.similarity:.3f}"
    blend = f"{settings.blend:.3f}"
    despill_type_int, effective_despill = _despill_parameters(settings.key_color, settings.despill)
    despill_mix = f"{effective_despill:.3f}"
    # FIX (2026-07-31): only emit :mode= when ffmpeg supports it (added in ffmpeg 8.0)
    despill_mode_kwargs = (
        f":mode={'screen' if settings.despill_screen else 'avg'}"
        if _ffmpeg_supports_despill_mode(ffmpeg_cmd)
        else ""
    )
    w, h = settings.width, settings.height
    scale = 0.5  # preview ครึ่งขนาด
    pw, ph = int(w * scale), int(h * scale)

    # FIX (2026-07-02): preview = 1 composite frame (background + chromakeyed
    # product). cover (intro card) เป็น temporal element ไม่ใช่ภาพนิ่งเดียว เลย
    # ไม่รวมใน preview — ก่อนหน้านี้อ่าน cover เป็น input 0 แต่สร้าง [v_lead] แล้วไม่ใช้
    # (orphan label) ทำให้ ffmpeg error เงียบๆ ตอนมี cover. ตอนนี้ลบ cover ออกจาก
    # preview ทั้งหมด → ทนต่อ cover ที่ไม่บังคับได้ทุกกรณี
    fc = (
        f"[0:v]scale={pw}:{ph}:force_original_aspect_ratio=decrease,"
        f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[bg];"
        f"[1:v]scale={pw}:{ph}:force_original_aspect_ratio=decrease,"
        f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,setsar=1,"
        f"format=yuva420p,"
        f"chromakey={key_hex}:{sim}:{blend},"
        f"despill=type={despill_type_int}:mix={despill_mix}{despill_mode_kwargs}[fg];"
        f"[bg][fg]overlay=0:0,format=yuv420p[v]"
    )
    cmd = [
        ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "error",
        "-i", background,           # input 0 = background
        "-i", product,              # input 1 = product (no -ss: keeps timestamps in sync
                                    #   with background so the 1-frame overlay composites)
        "-filter_complex", fc,
        "-map", "[v]",
        "-frames:v", "1",
        "-f", "image2",
        "-c:v", "png",
        "-update", "1",
        "pipe:1",
    ]
    return cmd


# ==================== Probe helpers ====================

def _positive_float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def _positive_ratio(value: object) -> float:
    try:
        numerator_text, denominator_text = str(value).split("/", 1)
        numerator = float(numerator_text)
        denominator = float(denominator_text)
    except (TypeError, ValueError):
        return 0.0
    if not (
        math.isfinite(numerator)
        and math.isfinite(denominator)
        and numerator > 0
        and denominator > 0
    ):
        return 0.0
    return numerator / denominator


def _tag_duration_seconds(value: object) -> float:
    """Parse a video-stream DURATION tag without consulting format duration."""

    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 3:
        return 0.0
    try:
        hours, minutes, seconds = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return 0.0
    if not all(math.isfinite(item) and item >= 0 for item in (hours, minutes, seconds)):
        return 0.0
    return _positive_float(hours * 3600.0 + minutes * 60.0 + seconds)


def _duration_from_video_stream(stream: object) -> float:
    if not isinstance(stream, dict):
        return 0.0

    direct = _positive_float(stream.get("duration"))
    if direct:
        return direct

    duration_ts = _positive_float(stream.get("duration_ts"))
    time_base = _positive_ratio(stream.get("time_base"))
    if duration_ts and time_base:
        timestamp_duration = duration_ts * time_base
        if math.isfinite(timestamp_duration) and timestamp_duration > 0:
            return timestamp_duration

    tags = stream.get("tags")
    if isinstance(tags, dict):
        tagged = _tag_duration_seconds(tags.get("DURATION") or tags.get("duration"))
        if tagged:
            return tagged

    frame_count = _positive_float(
        stream.get("nb_read_frames") or stream.get("nb_frames")
    )
    frame_rate = _positive_ratio(
        stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    )
    if frame_count and frame_rate:
        counted_duration = frame_count / frame_rate
        if math.isfinite(counted_duration) and counted_duration > 0:
            return counted_duration
    return 0.0


def _probe_video_stream_json(
    path: str,
    ffprobe_cmd: str,
    *,
    count_frames: bool,
    stop_check: Optional[Callable[[], bool]] = None,
) -> dict:
    command = [ffprobe_cmd, "-v", "error", "-select_streams", "v:0"]
    if count_frames:
        command.append("-count_frames")
    command += [
        "-show_entries",
        (
            "stream=duration,duration_ts,time_base,nb_frames,nb_read_frames,"
            "avg_frame_rate,r_frame_rate:stream_tags=DURATION"
        ),
        "-of",
        "json",
        path,
    ]
    timeout = 30 if count_frames else 10
    if stop_check is None:
        # Keep the established fast path (and its exact failure semantics) for
        # callers that do not need cooperative cancellation.
        output = subprocess.check_output(
            command,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=NO_WINDOW_FLAGS,
        )
    else:
        completed = _run_media_probe(
            command,
            timeout=timeout,
            stop_check=stop_check,
        )
        completed.check_returncode()
        output = completed.stdout
    payload = json.loads(output)
    return payload if isinstance(payload, dict) else {}


def _duration_from_audio_payload(
    payload: object,
    *,
    allow_format_fallback: bool,
) -> float:
    """Return the selected audio-stream duration with an audio-file fallback."""

    if not isinstance(payload, dict):
        return 0.0
    streams = payload.get("streams")
    stream = streams[0] if isinstance(streams, list) and streams else None
    if isinstance(stream, dict):
        direct = _positive_float(stream.get("duration"))
        if direct:
            return direct
        duration_ts = _positive_float(stream.get("duration_ts"))
        time_base = _positive_ratio(stream.get("time_base"))
        if duration_ts and time_base:
            timestamp_duration = duration_ts * time_base
            if math.isfinite(timestamp_duration) and timestamp_duration > 0:
                return timestamp_duration
        tags = stream.get("tags")
        if isinstance(tags, dict):
            tagged = _tag_duration_seconds(
                tags.get("DURATION") or tags.get("duration")
            )
            if tagged:
                return tagged

    # MP3/WAV/M4A files can omit stream.duration while still exposing the
    # selected audio stream and a reliable container duration.
    if allow_format_fallback:
        container = payload.get("format")
        if isinstance(container, dict):
            return _positive_float(container.get("duration"))
    return 0.0


def _probe_audio_duration(
    path: str,
    ffprobe_cmd: str = "ffprobe",
    stop_check: Optional[Callable[[], bool]] = None,
    *,
    allow_format_fallback: bool = True,
) -> float:
    """Return duration of the first selected audio stream, or zero on failure."""

    if not path or not os.path.isfile(path):
        return 0.0
    command = [
        ffprobe_cmd,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        (
            "stream=duration,duration_ts,time_base:"
            "stream_tags=DURATION:format=duration"
        ),
        "-of",
        "json",
        os.fspath(path),
    ]
    try:
        if stop_check is None:
            output = subprocess.check_output(
                command,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=NO_WINDOW_FLAGS,
            )
        else:
            completed = _run_media_probe(
                command,
                timeout=10,
                stop_check=stop_check,
            )
            completed.check_returncode()
            output = completed.stdout
        return _duration_from_audio_payload(
            json.loads(output),
            allow_format_fallback=allow_format_fallback,
        )
    except MediaProbeCancelled:
        raise
    except Exception:
        return 0.0


def _probe_duration(
    path: str,
    ffprobe_cmd: str = "ffprobe",
    stop_check: Optional[Callable[[], bool]] = None,
) -> float:
    """Return first video-stream duration, never container/audio-tail duration.

    The fast metadata path prefers the selected video stream's own duration,
    timestamps, stream tag, or declared frame count. Some real containers omit
    all of those; only then do we ask ffprobe to count decoded video frames and
    derive duration from the selected stream's frame rate. We deliberately do
    not fall back to format.duration because a longer audio track would make a
    truncated video look valid.
    """

    if not path or not os.path.isfile(path):
        return 0.0
    for count_frames in (False, True):
        try:
            payload = _probe_video_stream_json(
                os.fspath(path),
                ffprobe_cmd,
                count_frames=count_frames,
                stop_check=stop_check,
            )
            streams = payload.get("streams")
            stream = streams[0] if isinstance(streams, list) and streams else None
            duration = _duration_from_video_stream(stream)
            if duration:
                return duration
        except MediaProbeCancelled:
            raise
        except Exception:
            continue
    return 0.0


def _duration_tolerance(
    expected: float,
    fps: float = OUTPUT_DURATION_FALLBACK_FPS,
) -> float:
    """Allow normal timestamp jitter while rejecting materially short output.

    Two output frames cover normal mux/encoder timestamp rounding. The 100 ms
    cap prevents unusually low or invalid FPS values from recreating the former
    500 ms acceptance hole; the 10 ms floor covers container precision.
    """

    if not math.isfinite(expected) or expected <= 0:
        return 0.0
    if not math.isfinite(fps) or fps <= 0:
        fps = OUTPUT_DURATION_FALLBACK_FPS
    frame_window = OUTPUT_DURATION_FRAME_ALLOWANCE / fps
    return min(
        OUTPUT_DURATION_MAX_TOLERANCE_SEC,
        max(OUTPUT_DURATION_MIN_TOLERANCE_SEC, frame_window),
    )


def _duration_matches_expected(
    actual: float,
    expected: float,
    fps: float = OUTPUT_DURATION_FALLBACK_FPS,
) -> bool:
    if not (math.isfinite(actual) and math.isfinite(expected)):
        return False
    if actual <= 0 or expected <= 0:
        return False
    return abs(actual - expected) <= _duration_tolerance(expected, fps)

# ==================== Main API ====================

def _same_media_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    normalize = lambda value: os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(value))))
    )
    return normalize(left) == normalize(right)


def render_green(
    cover: Optional[str],
    product: str,
    background: str,
    audio: Optional[str],
    out_path: str,
    settings: GreenSettings,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[FfmpegProgress], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    tc_label: str = "",
    chroma_max_parallel: int = 1,
    target_duration_sec: Optional[float] = None,
    ping_pong_product_to_target: bool = False,
) -> FfmpegResult:
    """
    Render composited video 1 งาน

    V1.0.0.7: `chroma_max_parallel` lets the caller divide the CPU budget
    across multiple concurrent chroma processes (e.g. when the user runs
    two TC01 windows in parallel, or when TC04 batch stage spawns segments
    concurrently in a future). Defaults to 1 (sequential) — the budget
    is then `budget // 1` = the full per-process budget. With
    `chroma_max_parallel=3` each chroma process gets `budget // 3` threads.

    Returns: FfmpegResult
    """
    log = on_log or (lambda m: None)
    log(f"[green] cover={os.path.basename(cover) if cover else '(none)'}")
    log(f"[green] product={os.path.basename(product)}")
    log(f"[green] background={os.path.basename(background)}")
    if audio:
        log(f"[green] audio={os.path.basename(audio)}")
    if target_duration_sec is not None:
        log(
            f"[green] duration target={target_duration_sec!r}s "
            f"product_ping_pong={bool(ping_pong_product_to_target)}"
        )
    log(f"[green] settings: key={settings.key_color} sim={settings.similarity:.2f} "
        f"blend={settings.blend:.2f} despill={settings.despill:.2f} encoder={settings.encoder_alias}")

    if settings.cover_enabled and not (cover and os.path.isfile(cover)):
        return FfmpegResult(success=False, error="cover file not found (cover_enabled=True)")
    if not (product and os.path.isfile(product)):
        return FfmpegResult(success=False, error="product file not found")
    if not (background and os.path.isfile(background)):
        return FfmpegResult(success=False, error="background file not found")
    if _same_media_path(product, background):
        return FfmpegResult(
            success=False,
            error="product and background must be different files",
        )
    if not has_video_stream(
        product,
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
    ):
        return FfmpegResult(success=False, error="product has no readable video stream")
    if not has_video_stream(
        background,
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
    ):
        return FfmpegResult(success=False, error="background has no readable video stream")
    if settings.cover_enabled and not has_video_stream(
        cover or "",
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
    ):
        return FfmpegResult(success=False, error="cover has no readable video stream")
    uploaded_audio_state = MediaStreamState.ABSENT
    product_audio_state = MediaStreamState.ABSENT
    try:
        if audio:
            if not os.path.isfile(audio):
                return FfmpegResult(success=False, error="uploaded audio file not found")
            uploaded_audio_state = audio_stream_state(
                audio,
                ffprobe_cmd=ffprobe_cmd,
                ffmpeg_cmd=ffmpeg_cmd,
                stop_check=stop_check,
            )
            if uploaded_audio_state is MediaStreamState.ERROR:
                return FfmpegResult(success=False, error="uploaded audio probe failed")
            if uploaded_audio_state is not MediaStreamState.PRESENT:
                return FfmpegResult(
                    success=False,
                    error="uploaded audio has no readable audio stream",
                )
        if settings.audio_source == "product":
            product_audio_state = audio_stream_state(
                product,
                ffprobe_cmd=ffprobe_cmd,
                ffmpeg_cmd=ffmpeg_cmd,
                stop_check=stop_check,
            )
            if product_audio_state is MediaStreamState.ERROR:
                return FfmpegResult(success=False, error="product audio probe failed")
    except MediaProbeCancelled as exc:
        return FfmpegResult(success=False, error=str(exc), cancelled=True)

    requires_output_audio = (
        uploaded_audio_state is MediaStreamState.PRESENT
        or product_audio_state is MediaStreamState.PRESENT
    )

    partial_path = (
        f"{out_path}.partial.{os.getpid()}.{threading.get_ident()}."
        f"{time.time_ns()}.mp4"
    )

    try:
        cmd, expected = build_render_command(
            cover=cover, product=product, background=background,
            audio=audio, out_path=partial_path,
            settings=settings, ffmpeg_cmd=ffmpeg_cmd, ffprobe_cmd=ffprobe_cmd,
            chroma_max_parallel=chroma_max_parallel,
            product_audio_state=product_audio_state,
            uploaded_audio_state=uploaded_audio_state,
            target_duration_sec=target_duration_sec,
            ping_pong_product_to_target=ping_pong_product_to_target,
        )
    except Exception as exc:
        return FfmpegResult(success=False, error=f"invalid render input: {exc}")
    effective_encoder = "(unknown)"
    if "-c:v" in cmd:
        try:
            effective_encoder = cmd[cmd.index("-c:v") + 1]
        except Exception:
            effective_encoder = "(unknown)"
    gpu_label = "GPU/NVENC" if effective_encoder in {"h264_nvenc", "hevc_nvenc", "av1_nvenc"} else "CPU/software"
    log(f"[green] effective video encoder: {effective_encoder} ({gpu_label})")
    filter_complex = cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""
    log(
        "[green] filter path: "
        f"scale={'scale_cuda' if 'scale_cuda=' in filter_complex else 'scale'} "
        f"key={'chromakey_cuda' if 'chromakey_cuda=' in filter_complex else 'chromakey'} "
        f"overlay={'overlay_cuda' if 'overlay_cuda=' in filter_complex else 'overlay'}"
    )
    log(f"[green] despill filter: {'despill_cuda' if 'despill_cuda=' in filter_complex else 'despill'}")
    log(
        "[green] duration master: "
        f"{'explicit_target' if target_duration_sec is not None else 'product'}"
    )
    log(f"[green] expected duration: {expected:.1f}s")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    try:
        runner = FfmpegRunner(
            ffmpeg_cmd=ffmpeg_cmd,
            idle_timeout_sec=120,
            max_factor=3.0,
        )
        result = runner.run(
            cmd=cmd,
            expected_duration_sec=expected,
            on_log=on_log,
            on_progress=on_progress,
            stop_check=stop_check,
            extra_progress_args=True,
            tc_label=tc_label,
        )
        if not isinstance(result, FfmpegResult):
            raise TypeError("runner returned an invalid result")
    except Exception as exc:
        result = FfmpegResult(
            success=False,
            error=f"ffmpeg runner exception: {type(exc).__name__}: {exc}",
        )

    if should_retry_with_cpu(cmd, result, stop_check=stop_check):
        gpu_error = result.error or f"hardware encoder exited {result.returncode}"
        remove_partial(partial_path)
        cpu_partial_path = (
            f"{out_path}.partial.cpu.{os.getpid()}.{threading.get_ident()}."
            f"{time.time_ns()}.mp4"
        )
        log(
            "[green] hardware encoder failed during render; "
            "rebuilding the full filter graph for one libx264 retry"
        )
        try:
            cpu_settings = replace(settings, encoder_alias="libx264")
            cpu_cmd, cpu_expected = build_render_command(
                cover=cover,
                product=product,
                background=background,
                audio=audio,
                out_path=cpu_partial_path,
                settings=cpu_settings,
                ffmpeg_cmd=ffmpeg_cmd,
                ffprobe_cmd=ffprobe_cmd,
                chroma_max_parallel=chroma_max_parallel,
                product_audio_state=product_audio_state,
                uploaded_audio_state=uploaded_audio_state,
                target_duration_sec=target_duration_sec,
                ping_pong_product_to_target=ping_pong_product_to_target,
                disable_encoder_fallback=True,
            )
            if command_video_encoder(cpu_cmd) != "libx264":
                raise RuntimeError("exact libx264 CPU fallback is unavailable")
            cpu_runner = FfmpegRunner(
                ffmpeg_cmd=ffmpeg_cmd,
                idle_timeout_sec=120,
                max_factor=3.0,
            )
            cpu_result = cpu_runner.run(
                cmd=cpu_cmd,
                expected_duration_sec=cpu_expected,
                on_log=log,
                on_progress=on_progress,
                stop_check=stop_check,
                extra_progress_args=True,
                tc_label=tc_label,
            )
            if not isinstance(cpu_result, FfmpegResult):
                raise TypeError("CPU fallback runner returned an invalid result")
            if not cpu_result.success:
                cpu_result.error = (
                    f"hardware encoder failed: {gpu_error}; "
                    f"CPU fallback failed: {cpu_result.error or cpu_result.returncode}"
                )
            result = cpu_result
            cmd = cpu_cmd
            expected = cpu_expected
            partial_path = cpu_partial_path
        except Exception as exc:
            remove_partial(cpu_partial_path)
            result = FfmpegResult(
                success=False,
                error=(
                    f"hardware encoder failed: {gpu_error}; "
                    f"CPU fallback exception: {type(exc).__name__}: {exc}"
                ),
            )

    result.output_path = out_path
    try:
        if result.success:
            validation_error = ""
            if not os.path.isfile(partial_path) or os.path.getsize(partial_path) <= 0:
                validation_error = "output validation failed: missing or empty file"
            elif not has_video_stream(
                partial_path,
                ffprobe_cmd=ffprobe_cmd,
                ffmpeg_cmd=ffmpeg_cmd,
            ):
                validation_error = (
                    "output validation failed: missing readable video stream"
                )
            else:
                partial_duration = _probe_duration(partial_path, ffprobe_cmd)
                tolerance = _duration_tolerance(expected, settings.fps)
                if not _duration_matches_expected(
                    partial_duration,
                    expected,
                    settings.fps,
                ):
                    validation_error = (
                        "output validation failed: duration "
                        f"{partial_duration:.3f}s differs from expected "
                        f"{expected:.3f}s (tolerance {tolerance:.3f}s)"
                    )
                elif requires_output_audio:
                    output_audio_state = audio_stream_state(
                        partial_path,
                        ffprobe_cmd=ffprobe_cmd,
                        ffmpeg_cmd=ffmpeg_cmd,
                    )
                    if output_audio_state is MediaStreamState.ERROR:
                        validation_error = (
                            "output validation failed: required audio probe failed"
                        )
                    elif output_audio_state is not MediaStreamState.PRESENT:
                        validation_error = (
                            "output validation failed: required audio stream is missing"
                        )
            if validation_error:
                result.success = False
                result.error = validation_error
            else:
                os.replace(partial_path, out_path)
    except Exception as exc:
        result.success = False
        result.error = f"output publish failed: {exc}"
    finally:
        try:
            if os.path.isfile(partial_path):
                os.remove(partial_path)
        except OSError:
            pass
    if result.success and os.path.isfile(out_path):
        log(f"[green] ✅ saved: {out_path}")
    else:
        log(f"[green] ❌ failed: {result.error[:200]}")
    return result


def preview_green(
    cover: str,
    product: str,
    background: str,
    settings: GreenSettings,
    out_png_path: str,
    on_log: Optional[Callable[[str], None]] = None,
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    สร้าง preview 1 frame composite → PNG
    Returns: True ถ้าสำเร็จ
    """
    log = on_log or (lambda m: None)
    # FIX (2026-07-02): cover เป็น optional — บังคับเฉพาะตอน cover_enabled=True
    # (เหมือน render_green) ไม่ใช่บังคับเสมอ ไม่งั้น preview TC04 ที่ไม่ใส่ cover จะ error.
    # FIX (2026-07-02): preview แสดง composite (background + chromakeyed product)
    # เท่านั้น — cover (intro card) ไม่ใช่ภาพนิ่งเดียว เลยไม่บังคับ/ไม่ใช้ใน preview.
    # Product และ Background เป็น required; Cover เป็น optional.
    if not (product and os.path.isfile(product)):
        log("[green] ❌ product not found")
        return False
    if not (background and os.path.isfile(background)):
        log("[green] ❌ background not found")
        return False
    if _same_media_path(product, background):
        log("[green] ❌ product and background must be different files")
        return False

    cmd = build_preview_command(cover, product, background, settings, ffmpeg_cmd)
    # Redirect output from pipe:1 to the on-disk file.
    cmd = [c if c != "pipe:1" else out_png_path for c in cmd]

    log(f"[green] preview → {os.path.basename(out_png_path)}")
    try:
        if stop_check is not None:
            runner = FfmpegRunner(
                ffmpeg_cmd=ffmpeg_cmd,
                idle_timeout_sec=30.0,
                max_factor=1.0,
            )
            ffmpeg_result = runner.run(
                cmd,
                expected_duration_sec=0.0,
                on_log=log,
                stop_check=stop_check,
            )
            if ffmpeg_result.success and os.path.isfile(out_png_path):
                log(f"[green] ✅ preview saved: {out_png_path}")
                return True
            if ffmpeg_result.cancelled:
                log("[green] preview cancelled")
            else:
                log(f"[green] ❌ preview failed: {ffmpeg_result.error[:200]}")
            try:
                if os.path.isfile(out_png_path):
                    os.remove(out_png_path)
            except OSError:
                pass
            return False

        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=NO_WINDOW_FLAGS,
        )
        if result.returncode == 0 and os.path.isfile(out_png_path):
            log(f"[green] ✅ preview saved: {out_png_path}")
            return True
        log(f"[green] ❌ preview failed: {result.stderr[:200]}")
        return False
    except Exception as e:
        log(f"[green] ❌ preview exception: {e}")
        return False
