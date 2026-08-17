"""
GPU/Encoder Detector — auto-probe ffmpeg capabilities for hardware encoders.

Pattern ported from:
  - green.sj88ai.com/core/media_ffmpeg.py::_auto_detect_encoder (line 105-118)
  - greenlnw.cutdee.com/core/media_ffmpeg.py::_ffmpeg_nvenc_ready (line 260-284)

รองรับ:
  - h264_nvenc  (NVIDIA)
  - hevc_nvenc  (NVIDIA H.265)
  - h264_qsv    (Intel Quick Sync)
  - av1_nvenc   (NVIDIA AV1)
  - libx264     (CPU fallback, always available)

ใช้ LRU cache + smoke test 1-frame encode เพื่อยืนยันว่า encoder ทำงานได้จริง
(ไม่ใช่แค่ compile flag)
"""
import os
import re
import subprocess
import sys
import time
from functools import lru_cache
from typing import List, Optional, Tuple


# FIX (2026-07-02): ปิดหน้าต่าง console ดำตอน spawn ffmpeg (ป้องกันจอกระพริบบน exe)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# FIX (2026-07-31) — macOS VideoToolbox hardware acceleration:
# Apple Silicon (M1/M2/M3) and macOS Intel iGPUs ship a hardware H.264/H.265
# encoder accessible via ffmpeg's `h264_videotoolbox` and `hevc_videotoolbox`.
# This is 5-10× faster than libx264 with comparable perceptual quality.
# We add VideoToolbox to the cascade on darwin only — Windows/Linux cascades
# remain NVIDIA/Intel/AMD/CPU.
_VT_ENCODERS: List[str] = (
    ["h264_videotoolbox", "hevc_videotoolbox"] if sys.platform == "darwin" else []
)

# Default preference order — fastest encoder first.
DEFAULT_PREFERRED_ORDER: List[str] = [
    *_VT_ENCODERS,            # macOS Apple Silicon/Intel hardware (h264/hevc VT)
    "h264_nvenc",
    "av1_nvenc",
    "hevc_nvenc",
    "h264_qsv",
    "h264_amf",
    "libx264",  # CPU fallback (ต้องอยู่ท้ายสุด)
]

# Default NVENC preset + tune (from greenlnw.cutdee.com).
NVENC_DEFAULT_PRESET = "p6"
NVENC_DEFAULT_TUNE = "hq"

# GreenSettings.preset is a cross-encoder *profile*, not a raw FFmpeg value.
# Translate the UI speed/quality ladder into distinct values whenever the
# encoder exposes enough native levels.  A profile must not silently collapse
# into its neighbour (for example medium == slow == hq on libx264), because the
# UI describes these choices as a real speed/quality trade-off.
UI_PRESET_PROFILES = frozenset({
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "hq",
})

_NVENC_PRESET_BY_PROFILE = {
    "ultrafast": "p1",
    "superfast": "p2",
    "veryfast": "p3",
    "faster": "p4",
    "fast": "p5",
    "medium": NVENC_DEFAULT_PRESET,
    "slow": "p7",
    "hq": "p7",
}

_QSV_PRESET_BY_PROFILE = {
    "ultrafast": "veryfast",
    "superfast": "faster",
    "veryfast": "fast",
    "faster": "medium",
    "fast": "slow",
    "medium": "slower",
    "slow": "veryslow",
    "hq": "veryslow",
}

# FIX (2026-07-31): VideoToolbox doesn't have multi-tier presets.
# We map the UI speed/quality ladder to its native `-q:v` value (0–100; higher
# = better quality). The bitrate (`-b:v`) the user already set still controls
# the cap; `-q:v` is a soft quality cap when bitrate allows better quality.
_VT_QUALITY_BY_PROFILE = {
    "ultrafast": "50",
    "superfast": "55",
    "veryfast": "60",
    "faster":    "65",
    "fast":      "70",
    "medium":    "75",
    "slow":      "80",
    "hq":        "85",
}


_AMF_QUALITY_BY_PROFILE = {
    "ultrafast": "speed",
    "superfast": "speed",
    "veryfast": "speed",
    "faster": "balanced",
    "fast": "balanced",
    "medium": "balanced",
    "slow": "quality",
    "hq": "quality",
}

_AMF_USAGE_BY_PROFILE = {
    "ultrafast": "ultralowlatency",
    "superfast": "lowlatency",
    "veryfast": "transcoding",
    "faster": "ultralowlatency",
    "fast": "lowlatency",
    "medium": "transcoding",
    "slow": "transcoding",
    "hq": "high_quality",
}

_X264_PRESET_BY_PROFILE = {
    "ultrafast": "ultrafast",
    "superfast": "superfast",
    "veryfast": "veryfast",
    "faster": "faster",
    "fast": "fast",
    "medium": "medium",
    "slow": "slow",
    "hq": "veryslow",
}


@lru_cache(maxsize=1)
def _ffmpeg_list_encoders(ffmpeg_cmd: str = "ffmpeg") -> List[str]:
    """คืน list encoder ที่ ffmpeg build รองรับ (parse จาก `ffmpeg -encoders`)"""
    try:
        out = subprocess.run(
            [ffmpeg_cmd, "-hide_banner", "-encoders"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return []

    encs = set()
    for line in out.splitlines():
        # Encoder lines look like:
        #   " V..... = Video codec ..... encoder_name  description"
        # Example:
        #   " V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (decoders: ..."
        parts = line.split()
        if len(parts) >= 3 and re.match(r"^[VAS]\.", parts[0]):
            encs.add(parts[1])
    return sorted(encs)


def ffmpeg_supports_encoder(encoder: str, ffmpeg_cmd: str = "ffmpeg") -> bool:
    """เช็คว่า encoder มีอยู่ใน build หรือไม่ (compile-time check)"""
    return encoder in _ffmpeg_list_encoders(ffmpeg_cmd)


@lru_cache(maxsize=8)
def _encoder_smoke_test(encoder: str, ffmpeg_cmd: str = "ffmpeg") -> bool:
    """
    ทดสอบ encode จริง 1 frame (320x180 lavfi → null)
    เพื่อยืนยันว่า encoder ไม่ crash + รันได้บนเครื่องนี้
    """
    cmd = [
        ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.04:r=25",
        "-frames:v", "1",
        "-c:v", encoder,
    ]
    # Add the encoder-specific preset args before the output muxer.
    if encoder == "h264_nvenc":
        cmd += ["-preset", NVENC_DEFAULT_PRESET, "-tune", NVENC_DEFAULT_TUNE]
    elif encoder == "hevc_nvenc":
        cmd += ["-preset", NVENC_DEFAULT_PRESET, "-tune", NVENC_DEFAULT_TUNE]
    elif encoder == "av1_nvenc":
        cmd += ["-preset", NVENC_DEFAULT_PRESET]
    elif encoder == "h264_qsv":
        cmd += ["-preset", "slow"]
    elif encoder == "h264_amf":
        cmd += ["-quality", "quality"]
    elif encoder == "libx264":
        cmd += ["-preset", "ultrafast"]
    elif encoder in ("h264_videotoolbox", "hevc_videotoolbox"):
        # VT is bitrate-driven but accepts -q:v as a ceiling hint. We pass a
        # mid-range q+v so the smoke frame is realistic; the encoder_args_for_preset
        # path replaces this with a profile-specific q+v at render time.
        cmd += ["-q:v", "75", "-b:v", "2000k"]

    cmd += ["-f", "null", "-"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_encoder_ready(encoder: str, ffmpeg_cmd: str = "ffmpeg") -> bool:
    """เช็ค encoder ทำงานได้จริง (compile + runtime)"""
    if not ffmpeg_supports_encoder(encoder, ffmpeg_cmd):
        return False
    return _encoder_smoke_test(encoder, ffmpeg_cmd)


def effective_video_encoder(
    preferred: Optional[str] = None,
    ffmpeg_cmd: str = "ffmpeg",
    disable_fallback: bool = False,
) -> Tuple[str, List[str]]:
    """
    คืน (encoder, extra_args) ตามลำดับความสำคัญ

    Args:
        preferred: encoder ที่ user เลือก (None = auto)
        ffmpeg_cmd: path ไป ffmpeg
        disable_fallback: ถ้า True และ preferred ไม่ทำงาน → raise (ใช้สำหรับ reframe GPU-only)

    Returns:
        (encoder_name, extra_args) e.g. ("h264_nvenc", ["-preset", "p4", "-tune", "hq"])
    """
    order: List[str] = []
    if preferred and preferred in DEFAULT_PREFERRED_ORDER:
        order.append(preferred)
    if not (disable_fallback and preferred):
        order.extend(e for e in DEFAULT_PREFERRED_ORDER if e not in order)

    for enc in order:
        if is_encoder_ready(enc, ffmpeg_cmd):
            return enc, _video_encoder_args(enc)

    if disable_fallback:
        raise RuntimeError(
            f"Requested encoder is unavailable (preferred={preferred})."
        )
    # Last-ditch fallback — libx264 should always work (if the build includes it).
    if is_encoder_ready("libx264", ffmpeg_cmd):
        return "libx264", _video_encoder_args("libx264")
    raise RuntimeError("No video encoder available (even libx264 failed)")


def _video_encoder_args(encoder: str) -> List[str]:
    """คืน args เพิ่มเติมสำหรับ encoder (preset, tune, etc.)

    FIX (B-01, 2026-07-31): hevc_nvenc กับ av1_nvenc รับ -tune เป็น int(1..5) เท่านั้น
    ไม่ใช่ string "hq" — ffmpeg จะ reject คำสั่ง. ดังนั้น -tune ต้องอยู่บน h264_nvenc
    เท่านั้น (รับ string hq/ll/ull/lossless). hevc/av1 ปล่อยเงียน ๆ ให้ ffmpeg ใช้ default.
    """
    if encoder == "h264_nvenc":
        args = ["-preset", NVENC_DEFAULT_PRESET, "-tune", NVENC_DEFAULT_TUNE]
        args += ["-rc", "vbr", "-spatial-aq", "1"]
        return args
    if encoder in ("hevc_nvenc", "av1_nvenc"):
        # No -tune: those encoders take -tune <int 1..5>, not a string profile.
        return ["-preset", NVENC_DEFAULT_PRESET]
    if encoder == "h264_qsv":
        return ["-preset", "slow"]
    if encoder == "h264_amf":
        return ["-quality", "quality"]
    if encoder == "libx264":
        return ["-preset", "slow", "-crf", "18"]
    # FIX (2026-07-31): VideoToolbox hardware. Default quality=75 is a sensible
    # mid-point; callers override via encoder_args_for_preset() when the
    # user picked a UI preset profile.
    if encoder in ("h264_videotoolbox", "hevc_videotoolbox"):
        return ["-q:v", "75"]
    return []


def _without_encoder_profile_options(args: List[str]) -> List[str]:
    """Remove profile-controlled option pairs without touching rate control."""
    cleaned: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-preset", "-quality", "-q:v", "-multipass", "-rdo", "-usage", "-preanalysis"):
            # These FFmpeg options always consume the following value. Dropping
            # both prevents conflicting duplicate flags after profile mapping.
            # -q:v is included so VideoToolbox preset mapping replaces the default.
            index += 2
            continue
        cleaned.append(token)
        index += 1
    return cleaned


def encoder_args_for_preset(
    encoder: str,
    preset: str,
    *,
    base_args: Optional[List[str]] = None,
) -> List[str]:
    """Translate a UI preset profile to valid args for the resolved encoder.

    Each visible UI choice produces a distinct encoder command. Native preset
    ladders are used first; the final HQ tier adds a documented quality option
    where a family has fewer native presets (NVENC multipass, QSV RDO, AMF
    high-quality usage/preanalysis). Unknown profiles or encoder families fail
    closed. Profile-controlled pairs are replaced, never duplicated, while
    rate-control, tune, CRF and AQ arguments are retained.
    """
    profile = str(preset or "").strip().lower()
    if profile not in UI_PRESET_PROFILES:
        allowed = ", ".join(sorted(UI_PRESET_PROFILES))
        raise ValueError(f"invalid encoder preset profile {preset!r}; allowed: {allowed}")

    if encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        mapped = ["-preset", _NVENC_PRESET_BY_PROFILE[profile]]
        if profile == "hq":
            mapped += ["-multipass", "fullres"]
    elif encoder == "h264_qsv":
        mapped = ["-preset", _QSV_PRESET_BY_PROFILE[profile]]
        if profile == "hq":
            mapped += ["-rdo", "1"]
    elif encoder == "h264_amf":
        mapped = [
            "-quality", _AMF_QUALITY_BY_PROFILE[profile],
            "-usage", _AMF_USAGE_BY_PROFILE[profile],
        ]
        if profile == "hq":
            mapped += ["-preanalysis", "1"]
    elif encoder == "libx264":
        mapped = ["-preset", _X264_PRESET_BY_PROFILE[profile]]
    elif encoder in ("h264_videotoolbox", "hevc_videotoolbox"):
        # FIX (2026-07-31): VT has no preset ladder; map UI profile to a -q:v cap.
        mapped = ["-q:v", _VT_QUALITY_BY_PROFILE[profile]]
        if profile == "hq":
            # At HQ we lift the soft cap and let the encoder spend more cycles.
            mapped += ["-prio_speed", "0"]
    else:
        raise ValueError(f"preset mapping is unsupported for encoder {encoder!r}")

    args = list(_video_encoder_args(encoder) if base_args is None else base_args)

    # FIX (B-01, 2026-07-31): hevc_nvenc/av1_nvenc รับ -tune เป็น int(1..5) เท่านั้น
    # ห้ามมี -tune hq (string) ในคำสั่ง — ffmpeg จะ reject.
    # h264_nvenc รับ -tune เป็น string profile ได้ จึงเก็บไว้.
    if encoder in ("hevc_nvenc", "av1_nvenc"):
        cleaned = []
        skip_next = False
        for token in args:
            if skip_next:
                skip_next = False
                continue
            if token == "-tune":
                skip_next = True
                continue
            cleaned.append(token)
        args = cleaned

    return [*mapped, *_without_encoder_profile_options(args)]


def gpu_summary(ffmpeg_cmd: str = "ffmpeg") -> dict:
    """คืน summary สำหรับแสดงใน UI (encoder ที่ใช้งานได้)"""
    available = []
    for enc in DEFAULT_PREFERRED_ORDER:
        if is_encoder_ready(enc, ffmpeg_cmd):
            available.append(enc)
    return {
        "available": available,
        "nvenc_ready": "h264_nvenc" in available,
        "qsv_ready": "h264_qsv" in available,
        "vt_ready": "h264_videotoolbox" in available,
        "cpu_ready": "libx264" in available,
        "ffmpeg_cmd": ffmpeg_cmd,
    }


# Aliases the user can pick in the UI → actual encoder name.
ALIAS_MAP = {
    "auto": None,  # auto-detect
    "nvenc": "h264_nvenc",
    "h264_nvenc": "h264_nvenc",
    "hevc_nvenc": "hevc_nvenc",
    "av1_nvenc": "av1_nvenc",
    "qsv": "h264_qsv",
    "h264_qsv": "h264_qsv",
    "amf": "h264_amf",
    "h264_amf": "h264_amf",
    "x264": "libx264",
    "libx264": "libx264",
    "cpu": "libx264",
    # FIX (2026-07-31): macOS VideoToolbox aliases.
    "videotoolbox": "h264_videotoolbox",
    "vt": "h264_videotoolbox",
    "h264_videotoolbox": "h264_videotoolbox",
    "hevc_videotoolbox": "hevc_videotoolbox",
}


def resolve_encoder_alias(alias: str) -> Optional[str]:
    """แปล alias ("auto"/"nvenc"/"cpu") เป็น encoder name จริง"""
    return ALIAS_MAP.get(alias.lower().strip() if alias else "")
