"""
AI Reframe — Auto reframe pipeline (70 lens presets + 7×3 fixed mode).

Standalone, framework-agnostic port from:
  - greenlnw.cutdee.com/core/auto_reframe/config.py (70 lens presets, scenarios, encoders, platforms)
  - greenlnw.cutdee.com/WebAppCodex/backend/services/reframe_service.py::_build_fixed_ffmpeg_command

ความสามารถ:
  1. 70 lens presets (16mm → 85mm, step 1mm) — ครบเหมือน greenlnw
  2. 5 platforms (Custom 16:9 / TikTok 9:16 / YouTube Shorts / Reels / Facebook)
  3. 6 scenarios (Easy Mode): presenter / interview / review / group / wide / portrait
  4. 7×3 fixed mode = 7 lenses (16/35/40/45/50/55/60) × 3 compositions (center/left/right) = 21 outputs/source
  5. ffmpeg crop + scale filter (lens scale × composition offset)
  6. GPU (h264_nvenc) + CPU (libx264) auto-detect (override greenlnw GPU-only policy)
  7. Multi-source reframe — render ทุก source × lens × composition

ใช้ได้กับ Flet desktop app ของ AutoMv_A (ไม่ผูกกับ server)
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .ffmpeg_runner import FfmpegRunner, FfmpegResult, FfmpegProgress, NO_WINDOW_FLAGS
from .cpu_limit import effective_ffmpeg_threads
from .gpu_detector import effective_video_encoder, resolve_encoder_alias
from .green_render import (
    _duration_matches_expected,
    _duration_tolerance,
    _probe_duration,
)
from .media_probe import (
    MediaProbeCancelled,
    MediaStreamState,
    audio_stream_state,
    has_video_stream,
)
from .encoder_recovery import remove_partial, should_retry_with_cpu
from .path_utils import portable_stem


VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")

# FIX (B-07, 2026-07-31): guard cache with a lock; key includes the binary's
# (mtime_ns, size, ino) tuple so a binary swap mid-session invalidates.
_FFMPEG_FILTER_CACHE: Dict[Tuple[Tuple[int, int, int], str], bool] = {}
_FFMPEG_FILTER_CACHE_LOCK = threading.Lock()

# FIX (V1.0.2.x, 2026-08-08): reframe retry on transient ffmpeg failure.
# Mirrors the reference implementation in green.sj88ai.com
# (MAX_RETRIES=3, delays=[1,3,10]s). Cancellation aborts the loop.
MAX_RETRIES = 3
RETRY_DELAYS = [1, 3, 10]


def _publish_reframe_output(partial_path: str, output_path: str) -> None:
    """Atomically publish a validated reframe file with a short lock retry.

    Windows can briefly deny a rename while an indexer or scanner has the
    newly-written MP4 open.  Retrying only that transient sharing violation
    preserves the existing atomic ``os.replace`` publication contract.
    """

    for attempt in range(3):
        try:
            os.replace(partial_path, output_path)
            return
        except PermissionError:
            if attempt >= 2:
                raise
            time.sleep(0.05 * (attempt + 1))


def _ffmpeg_binary_token(ffmpeg_cmd: str) -> Tuple[int, int, int]:
    try:
        st = os.stat(ffmpeg_cmd)
        return (int(st.st_mtime_ns), int(st.st_size), int(st.st_ino))
    except OSError:
        return (0, 0, 0)


def _ffmpeg_has_filter(ffmpeg_cmd: str, filter_name: str) -> bool:
    token = _ffmpeg_binary_token(ffmpeg_cmd)
    key = (token, filter_name)
    with _FFMPEG_FILTER_CACHE_LOCK:
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
    with _FFMPEG_FILTER_CACHE_LOCK:
        _FFMPEG_FILTER_CACHE[key] = found
    return found


def _source_audio_state(
    source: str,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
    stop_check: Optional[Callable[[], bool]] = None,
) -> MediaStreamState:
    """Return the source audio state without collapsing probe errors to absent."""

    return audio_stream_state(
        source,
        ffprobe_cmd=ffprobe_cmd,
        ffmpeg_cmd=ffmpeg_cmd,
        stop_check=stop_check,
    )


def _source_has_audio(
    source: str,
    ffprobe_cmd: str = "ffprobe",
    ffmpeg_cmd: str = "ffmpeg",
) -> bool:
    """Backward-compatible bool view of _source_audio_state.

    Probe errors remain distinguishable through the tri-state API while this
    legacy helper continues to return False for them.
    """

    return (
        _source_audio_state(source, ffprobe_cmd, ffmpeg_cmd)
        is MediaStreamState.PRESENT
    )

# ====================================================================
# FIX (2026-07-02): composition crop — ported from greenlnw
# WebAppCodex/backend/services/reframe_service.py (_fixed_*).
# ก่อนหน้านี้ของเราใช้ crop_x = "0" / "iw-ow" / "(iw-ow)/2" (ตรึงขอบ รุนแรง)
# greenlnw ใช้ anchor-based crop (rule-of-thirds) + jitter + static-zoom ทำให้
# ซ้าย/ขวา/กลาง ดูนุ่มนวลเป็นธรรมชาติเหมือนกัน
# ====================================================================
# ค่า default ตรงกับ DEFAULT_REFRAME_* ของ greenlnw (product_center mode)
_REF_X_SPAN = 0.24
_REF_Y_MIN, _REF_Y_MAX = 0.50, 0.56
_REF_STATIC_ZOOM_MIN, _REF_STATIC_ZOOM_MAX = 1.0, 1.08
_REF_JITTER_MAX = 0.012
_REF_TILT_MIN_DEG, _REF_TILT_MAX_DEG = 5.0, 10.0
_REF_ROTATE_PAD_PX = 512

# Reframe profile modes — port of greenlnw REFRAME_MODE_* (controls ONLY tilt_deg).
#  - SPEED (default): ไม่เอียง
#  - LITE_TILT: เอียงนิด 2-3° ที่ซ้าย/ขวา เฉพาะ lens 2/4/6
#  - LEGACY: เอียงเต็ม 5-10° จาก recipe (ซ้าย/ขวา + สลับที่กลางตาม lens_index)
REFRAME_MODE_SPEED = "speed"
REFRAME_MODE_LITE_TILT = "lite_tilt"
REFRAME_MODE_LEGACY = "legacy"
DEFAULT_REFRAME_MODE = REFRAME_MODE_SPEED


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _composition_anchor(composition) -> Tuple[float, float]:
    """Default (x_anchor, y_anchor) ตาม composition — port _fixed_default_anchor."""
    c = composition.value if isinstance(composition, Composition) else str(composition)
    if c == "left":
        return 0.28, 0.53
    if c == "right":
        return 0.72, 0.53
    return 0.50, 0.51


def _crop_scale(view_scale: float, static_zoom: float = 1.0) -> float:
    """ลด scale ตาม static-zoom — port _fixed_crop_scale."""
    s = _clampf(view_scale, 0.05, 1.0)
    z = _clampf(static_zoom, 1.0, 1.20)
    return _clampf(s / z, 0.05, 1.0)


def _composition_value(composition) -> str:
    return composition.value if isinstance(composition, Composition) else str(composition)


def normalize_reframe_mode(mode) -> str:
    m = str(mode or DEFAULT_REFRAME_MODE).strip().lower().replace("-", "_")
    if m in {"lite", "lite_tilt", "litetilt"}:
        return REFRAME_MODE_LITE_TILT
    if m in {"legacy", "full"}:
        return REFRAME_MODE_LEGACY
    return REFRAME_MODE_SPEED  # speed/off/none/unknown → speed


def _visible_tilt_sign(lens_index: int, comp: str) -> float:
    """port _fixed_visible_tilt_sign: left=-1, right=+1, center=-1 if odd else +1."""
    if comp == "left":
        return -1.0
    if comp == "right":
        return 1.0
    return -1.0 if int(lens_index) % 2 else 1.0


def _apply_reframe_mode(tilt_deg: float, mode, lens_index: int, comp: str) -> float:
    """ปรับ tilt_deg ตาม profile mode — port _apply_reframe_profile_recipe."""
    profile = normalize_reframe_mode(mode)
    if profile == REFRAME_MODE_LEGACY:
        return tilt_deg  # เก็บ tilt เต็มจาก recipe
    if profile == REFRAME_MODE_LITE_TILT:
        if comp in ("left", "right") and int(lens_index) in {2, 4, 6}:
            sign = _visible_tilt_sign(lens_index, comp)
            mag = min(3.0, 2.0 + ((int(lens_index) % 3) * 0.5))
            return sign * mag
        return 0.0
    # SPEED (default/fallback): ไม่เอียง
    return 0.0


def _variation_recipe(
    source: str,
    lens: "LensPreset",
    composition,
    lens_index: int,
) -> Tuple[float, float, float, float, float, float]:
    """
    สร้าง variation recipe แบบ deterministic สำหรับ (source, lens, composition)
    → (x_anchor, y_anchor, static_zoom, jitter_x, jitter_y, tilt_deg)

    Port จาก greenlnw _fixed_product_variation_recipe (product_center mode) แต่
    ใช้ seeded RNG (crc32 ของ key) เพื่อให้ re-render = ผลเดิม (reproducible)
    ต่างจาก greenlnw ที่ใช้ random สุ่มทุกครั้ง.
    """
    import random
    import zlib
    comp = _composition_value(composition)
    key = f"{os.path.basename(source)}|{lens.key}|{comp}"
    rng = random.Random(zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF)

    x_span = _REF_X_SPAN
    shift_dir = 1.0 if comp == "left" else (-1.0 if comp == "right" else 0.0)
    base_x_shift = shift_dir * x_span
    zoom_bias = min(1.0, max(0.0, (lens_index - 1) / max(1, len(FIXED_7X3_LENS_KEYS) - 1)))

    if comp == "center":
        local_x_shift = rng.uniform(-min(x_span, 0.08) * 0.50, min(x_span, 0.08) * 0.50)
        x_shift = _clampf(local_x_shift, -0.04, 0.04)
    else:
        local_x_shift = rng.uniform(-x_span * 0.16, x_span * 0.16) if x_span > 0 else 0.0
        x_shift = _clampf(base_x_shift + local_x_shift, -0.26, 0.26)

    x_anchor = _clampf(0.50 - x_shift, 0.24, 0.76)
    y_anchor = rng.uniform(_REF_Y_MIN, _REF_Y_MAX)
    zoom_floor = _REF_STATIC_ZOOM_MIN + (_REF_STATIC_ZOOM_MAX - _REF_STATIC_ZOOM_MIN) * 0.25 * zoom_bias
    static_zoom = rng.uniform(min(zoom_floor, _REF_STATIC_ZOOM_MAX), _REF_STATIC_ZOOM_MAX)
    # tilt (full LEGACY value) — draw order matches greenlnw (tilt before jitter).
    # The profile MODE later selects how much tilt to keep.
    tilt_sign = _visible_tilt_sign(lens_index, comp)
    tilt_deg = tilt_sign * rng.uniform(_REF_TILT_MIN_DEG, _REF_TILT_MAX_DEG) if _REF_TILT_MAX_DEG > 0 else 0.0
    jitter_x = rng.uniform(-_REF_JITTER_MAX, _REF_JITTER_MAX) if _REF_JITTER_MAX > 0 else 0.0
    jitter_y = rng.uniform(-_REF_JITTER_MAX, _REF_JITTER_MAX) if _REF_JITTER_MAX > 0 else 0.0
    return x_anchor, y_anchor, static_zoom, jitter_x, jitter_y, tilt_deg


# ==================== Presets ====================

@dataclass(frozen=True)
class LensPreset:
    """1 lens preset (port from greenlnw.core.auto_reframe.config.ReframePreset)"""
    key: str
    display_name: str
    focal_mm: int                # 16, 17, ... 85
    view_scale: float            # 1.0 → 0.3 (16mm = เต็ม frame, 85mm = ซูม 70%)
    smooth_factor: float         # 0.985 (fixed in greenlnw)

    @property
    def category(self) -> str:
        """Ultra Wide / Wide / Normal / Tele / Tele Portrait (port จาก display_name)"""
        if "Ultra Wide" in self.display_name:
            return "Ultra Wide"
        if self.focal_mm <= 28:
            return "Wide"
        if self.focal_mm <= 50:
            return "Normal"
        if self.focal_mm <= 70:
            return "Tele"
        return "Tele Portrait"


# 70 lens presets — 16mm → 85mm (step 1mm) — ported directly from greenlnw.
LENS_PRESETS: List[LensPreset] = [
    # 16-20: Ultra Wide
    LensPreset("lens16mm", "Lens 16mm (Ultra Wide)", 16, 1.000, 0.985),
    LensPreset("lens17mm", "Lens 17mm (Ultra Wide)", 17, 0.990, 0.985),
    LensPreset("lens18mm", "Lens 18mm (Ultra Wide)", 18, 0.980, 0.985),
    LensPreset("lens19mm", "Lens 19mm (Ultra Wide)", 19, 0.970, 0.985),
    LensPreset("lens20mm", "Lens 20mm (Ultra Wide)", 20, 0.959, 0.985),
    # 21-28: Wide
    LensPreset("lens21mm", "Lens 21mm (Wide)", 21, 0.949, 0.985),
    LensPreset("lens22mm", "Lens 22mm (Wide)", 22, 0.939, 0.985),
    LensPreset("lens23mm", "Lens 23mm (Wide)", 23, 0.929, 0.985),
    LensPreset("lens24mm", "Lens 24mm (Wide)", 24, 0.919, 0.985),
    LensPreset("lens25mm", "Lens 25mm (Wide)", 25, 0.909, 0.985),
    LensPreset("lens26mm", "Lens 26mm (Wide)", 26, 0.899, 0.985),
    LensPreset("lens27mm", "Lens 27mm (Wide)", 27, 0.888, 0.985),
    LensPreset("lens28mm", "Lens 28mm (Wide)", 28, 0.878, 0.985),
    # 29-35: Normal-Wide
    LensPreset("lens29mm", "Lens 29mm (Normal-Wide)", 29, 0.868, 0.985),
    LensPreset("lens30mm", "Lens 30mm (Normal-Wide)", 30, 0.858, 0.985),
    LensPreset("lens31mm", "Lens 31mm (Normal-Wide)", 31, 0.848, 0.985),
    LensPreset("lens32mm", "Lens 32mm (Normal-Wide)", 32, 0.838, 0.985),
    LensPreset("lens33mm", "Lens 33mm (Normal-Wide)", 33, 0.828, 0.985),
    LensPreset("lens34mm", "Lens 34mm (Normal-Wide)", 34, 0.817, 0.985),
    LensPreset("lens35mm", "Lens 35mm (Normal-Wide)", 35, 0.807, 0.985),
    # 36-50: Normal/Portrait
    LensPreset("lens36mm", "Lens 36mm (Normal/Portrait)", 36, 0.797, 0.985),
    LensPreset("lens37mm", "Lens 37mm (Normal/Portrait)", 37, 0.787, 0.985),
    LensPreset("lens38mm", "Lens 38mm (Normal/Portrait)", 38, 0.777, 0.985),
    LensPreset("lens39mm", "Lens 39mm (Normal/Portrait)", 39, 0.767, 0.985),
    LensPreset("lens40mm", "Lens 40mm (Normal/Portrait)", 40, 0.757, 0.985),
    LensPreset("lens41mm", "Lens 41mm (Normal/Portrait)", 41, 0.746, 0.985),
    LensPreset("lens42mm", "Lens 42mm (Normal/Portrait)", 42, 0.736, 0.985),
    LensPreset("lens43mm", "Lens 43mm (Normal/Portrait)", 43, 0.726, 0.985),
    LensPreset("lens44mm", "Lens 44mm (Normal/Portrait)", 44, 0.716, 0.985),
    LensPreset("lens45mm", "Lens 45mm (Normal/Portrait)", 45, 0.706, 0.985),
    LensPreset("lens46mm", "Lens 46mm (Normal/Portrait)", 46, 0.696, 0.985),
    LensPreset("lens47mm", "Lens 47mm (Normal/Portrait)", 47, 0.686, 0.985),
    LensPreset("lens48mm", "Lens 48mm (Normal/Portrait)", 48, 0.675, 0.985),
    LensPreset("lens49mm", "Lens 49mm (Normal/Portrait)", 49, 0.665, 0.985),
    LensPreset("lens50mm", "Lens 50mm (Normal/Portrait)", 50, 0.655, 0.985),
    # 51-70: Tele
    LensPreset("lens51mm", "Lens 51mm (Tele)", 51, 0.645, 0.985),
    LensPreset("lens52mm", "Lens 52mm (Tele)", 52, 0.635, 0.985),
    LensPreset("lens53mm", "Lens 53mm (Tele)", 53, 0.625, 0.985),
    LensPreset("lens54mm", "Lens 54mm (Tele)", 54, 0.614, 0.985),
    LensPreset("lens55mm", "Lens 55mm (Tele)", 55, 0.604, 0.985),
    LensPreset("lens56mm", "Lens 56mm (Tele)", 56, 0.594, 0.985),
    LensPreset("lens57mm", "Lens 57mm (Tele)", 57, 0.584, 0.985),
    LensPreset("lens58mm", "Lens 58mm (Tele)", 58, 0.574, 0.985),
    LensPreset("lens59mm", "Lens 59mm (Tele)", 59, 0.564, 0.985),
    LensPreset("lens60mm", "Lens 60mm (Tele)", 60, 0.554, 0.985),
    LensPreset("lens61mm", "Lens 61mm (Tele)", 61, 0.543, 0.985),
    LensPreset("lens62mm", "Lens 62mm (Tele)", 62, 0.533, 0.985),
    LensPreset("lens63mm", "Lens 63mm (Tele)", 63, 0.523, 0.985),
    LensPreset("lens64mm", "Lens 64mm (Tele)", 64, 0.513, 0.985),
    LensPreset("lens65mm", "Lens 65mm (Tele)", 65, 0.503, 0.985),
    LensPreset("lens66mm", "Lens 66mm (Tele)", 66, 0.493, 0.985),
    LensPreset("lens67mm", "Lens 67mm (Tele)", 67, 0.483, 0.985),
    LensPreset("lens68mm", "Lens 68mm (Tele)", 68, 0.472, 0.985),
    LensPreset("lens69mm", "Lens 69mm (Tele)", 69, 0.462, 0.985),
    LensPreset("lens70mm", "Lens 70mm (Tele)", 70, 0.452, 0.985),
    # 71-85: Tele Portrait
    LensPreset("lens71mm", "Lens 71mm (Tele Portrait)", 71, 0.442, 0.985),
    LensPreset("lens72mm", "Lens 72mm (Tele Portrait)", 72, 0.432, 0.985),
    LensPreset("lens73mm", "Lens 73mm (Tele Portrait)", 73, 0.422, 0.985),
    LensPreset("lens74mm", "Lens 74mm (Tele Portrait)", 74, 0.412, 0.985),
    LensPreset("lens75mm", "Lens 75mm (Tele Portrait)", 75, 0.401, 0.985),
    LensPreset("lens76mm", "Lens 76mm (Tele Portrait)", 76, 0.391, 0.985),
    LensPreset("lens77mm", "Lens 77mm (Tele Portrait)", 77, 0.381, 0.985),
    LensPreset("lens78mm", "Lens 78mm (Tele Portrait)", 78, 0.371, 0.985),
    LensPreset("lens79mm", "Lens 79mm (Tele Portrait)", 79, 0.361, 0.985),
    LensPreset("lens80mm", "Lens 80mm (Tele Portrait)", 80, 0.351, 0.985),
    LensPreset("lens81mm", "Lens 81mm (Tele Portrait)", 81, 0.341, 0.985),
    LensPreset("lens82mm", "Lens 82mm (Tele Portrait)", 82, 0.330, 0.985),
    LensPreset("lens83mm", "Lens 83mm (Tele Portrait)", 83, 0.320, 0.985),
    LensPreset("lens84mm", "Lens 84mm (Tele Portrait)", 84, 0.310, 0.985),
    LensPreset("lens85mm", "Lens 85mm (Tele Portrait)", 85, 0.300, 0.985),
]
assert len(LENS_PRESETS) == 70, f"expected 70 lens presets, got {len(LENS_PRESETS)}"

LENS_BY_KEY: Dict[str, LensPreset] = {p.key: p for p in LENS_PRESETS}


# ==================== Platforms ====================

@dataclass(frozen=True)
class PlatformPreset:
    key: str
    label: str
    width: int
    height: int


PLATFORMS: List[PlatformPreset] = [
    PlatformPreset("custom",   "Custom 16:9 (1920x1080)", 1920, 1080),
    PlatformPreset("tiktok",   "TikTok 9:16 (1080x1920)", 1080, 1920),
    PlatformPreset("shorts",   "YouTube Shorts 9:16",     1080, 1920),
    PlatformPreset("reels",    "Instagram Reels 9:16",    1080, 1920),
    PlatformPreset("facebook", "Facebook 16:9 (1920x1080)", 1920, 1080),
]
PLATFORM_BY_KEY: Dict[str, PlatformPreset] = {p.key: p for p in PLATFORMS}


# ==================== Scenarios (Easy Mode) ====================

@dataclass(frozen=True)
class ScenarioPreset:
    key: str
    label: str
    focus_mode: str     # face / product / center
    lens_min: int       # mm
    lens_max: int       # mm
    default_comps: List[str] = field(default_factory=lambda: ["center"])


SCENARIOS: List[ScenarioPreset] = [
    ScenarioPreset("presenter", "🎤 พิธีกร / พูดคนเดียว", "face",    28, 50, ["center"]),
    ScenarioPreset("interview", "🎬 สัมภาษณ์ 2 คน",       "face",    28, 50, ["center", "left", "right"]),
    ScenarioPreset("review",    "📦 รีวิวสินค้า",          "product", 20, 35, ["center"]),
    ScenarioPreset("group",     "👥 กลุ่มคน",             "face",    16, 35, ["center"]),
    ScenarioPreset("wide",      "🎵 ถ่ายกว้าง / คอนเสิร์ต", "center", 16, 24, ["center"]),
    ScenarioPreset("portrait",  "🎯 Portrait ซูม",         "face",    50, 85, ["center"]),
]


# ==================== Compositions ====================

class Composition(str, Enum):
    CENTER = "center"  # crop ตรงกลาง (default)
    LEFT = "left"      # crop ชิดซ้าย
    RIGHT = "right"    # crop ชิดขวา


ALL_COMPOSITIONS: List[Composition] = [Composition.CENTER, Composition.LEFT, Composition.RIGHT]


# ==================== 7×3 Fixed Mode ====================

# The 7 fixed lenses used by the fixed-7x3 mode (ported from greenlnw).
# FIX (2026-07-02): align fixed lens set to greenlnw FIXED_GPU_PRESET_KEYS
# (config.py) — was (16,28,39,50,62,74,85), only 16 & 50 overlapped → 21 outputs
# were totally different crops. greenlnw uses (16,35,40,45,50,55,60). view_scale
# values already match (16=1.0 35=0.807 40=0.757 45=0.706 50=0.655 55=0.604 60=0.554).
FIXED_7X3_LENS_KEYS: Tuple[str, ...] = (
    "lens16mm", "lens35mm", "lens40mm", "lens45mm",
    "lens50mm", "lens55mm", "lens60mm",
)
FIXED_7X3_LENSES: List[LensPreset] = [LENS_BY_KEY[k] for k in FIXED_7X3_LENS_KEYS]
assert len(FIXED_7X3_LENSES) == 7


# ==================== Auto Lens Picker ====================

def auto_pick_lenses(count: int) -> List[LensPreset]:
    """
    เลือก *count* lens จาก 70 presets กระจายสม่ำเสมอ (linspace)
    Port จาก greenlnw.cutdee.com/core/auto_reframe/config.py::auto_pick_lenses

    ตัวอย่าง:
      count=5  → 16, 33, 50, 68, 85mm
      count=10 → 16, 24, 31, 39, 47, 54, 62, 70, 77, 85mm
    """
    pool = LENS_PRESETS
    if not pool:
        return []
    count = max(1, min(count, len(pool)))
    if count >= len(pool):
        return list(pool)
    if count == 1:
        return [pool[len(pool) // 2]]
    step = (len(pool) - 1) / (count - 1)
    indices = [round(i * step) for i in range(count)]
    seen = set()
    unique = []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            unique.append(idx)
    return [pool[i] for i in unique]


def get_lenses_by_focal_range(min_mm: int, max_mm: int) -> List[LensPreset]:
    """คืน lens ทั้งหมดที่ focal อยู่ในช่วง [min_mm, max_mm]"""
    return [p for p in LENS_PRESETS if min_mm <= p.focal_mm <= max_mm]


# ==================== Settings ====================

@dataclass
class ReframeSettings:
    """ตั้งค่า AI Reframe"""
    # Mode
    use_fixed_7x3: bool = True            # ถ้า True → 7 lens คงที่; ถ้า False → ใช้ lenses_override
    custom_lens_count: int = 5            # จำนวน lens ถ้าไม่ใช้ fixed (auto_pick_lenses)
    scenario_key: Optional[str] = None     # ถ้าใช้ Easy Mode (override custom_lens_count)
    # Reframe profile (tilt policy): speed / lite_tilt / legacy — port greenlnw
    reframe_mode: str = DEFAULT_REFRAME_MODE
    lens_count: int = 7  # Phase 7: 1-15 (default 7 = fixed set)

    # Platform
    platform_key: str = "tiktok"          # default = TikTok 9:16 (1080×1920) for short-form
    output_width: int = 0                 # ถ้า 0 → ใช้จาก platform
    output_height: int = 0

    # Compositions
    compositions: List[str] = field(default_factory=lambda: ["center", "left", "right"])

    # Output
    encoder_alias: str = "nvenc"          # force h264_nvenc (GTX supported)
    bitrate: str = "8000k"

    # Multi-source
    max_sources: int = 0                  # 0 = ไม่จำกัด; otherwise cap

    # Parallelism
    # v3.PERF: auto-scale from cpu_count when 0.
    # 0 (default) → derive from os.cpu_count(): <8 → 2, <16 → 3, ≥16 → 4
    # Explicit values still respected (UI can override).
    max_parallel_ffmpeg: int = 0          # 0=auto-scale from cpu_count

    # 0=full target output resolution, N=work at N-p short side then upscale
    # later in the chroma stage. The reference contract defaults to full res.
    # Set to 0 for full-res reframe (best for 1080p or lower source content).
    reframe_short_side: int = 0


# ==================== Build Reframe Plan ====================

@dataclass
class ReframeTask:
    """1 task = (source × lens × composition) → 1 output"""
    source_path: str
    source_index: int
    lens: LensPreset
    composition: Composition
    output_path: str

    @property
    def task_id(self) -> str:
        return Path(self.output_path).stem


def build_reframe_tasks(
    sources: List[str],
    out_dir: str,
    settings: ReframeSettings,
) -> List[ReframeTask]:
    """
    สร้าง list ของ ReframeTask (ทุก combination ของ source × lens × composition)

    Returns:
        List[ReframeTask] เรียงตาม (source, lens, composition)
    """
    # 1) Resolve lenses
    if settings.scenario_key:
        # Easy Mode
        scenario = next((s for s in SCENARIOS if s.key == settings.scenario_key), None)
        if scenario:
            lenses = get_lenses_by_focal_range(scenario.lens_min, scenario.lens_max)
            # Use the scenario's compositions when the caller did not override them.
            if not settings.compositions:
                settings.compositions = scenario.default_comps
        else:
            lenses = FIXED_7X3_LENSES
    elif settings.use_fixed_7x3:
        lenses = FIXED_7X3_LENSES
    else:
        lenses = auto_pick_lenses(settings.custom_lens_count)

    # 2) Resolve compositions
    comps = []
    for c in settings.compositions:
        try:
            comps.append(Composition(c))
        except ValueError:
            comps.append(Composition.CENTER)
    if not comps:
        comps = [Composition.CENTER]

    # 3) Resolve output dimensions
    if settings.output_width <= 0 or settings.output_height <= 0:
        plat = PLATFORM_BY_KEY.get(settings.platform_key)
        if plat:
            settings.output_width = plat.width
            settings.output_height = plat.height
        else:
            settings.output_width = 1920
            settings.output_height = 1080

    # 4) Cap sources
    srcs = list(sources)
    if settings.max_sources > 0:
        srcs = srcs[:settings.max_sources]
    stem_counts: Dict[str, int] = {}
    for source in srcs:
        stem_key = portable_stem(source).casefold()
        stem_counts[stem_key] = stem_counts.get(stem_key, 0) + 1


    # 5) Build tasks
    os.makedirs(out_dir, exist_ok=True)
    tasks: List[ReframeTask] = []
    for src_idx, src in enumerate(srcs, 1):
        src_base = portable_stem(src)
        output_base = (
            src_base if stem_counts[src_base.casefold()] == 1
            else f"{src_base}__src{src_idx:03d}"
        )
        for lens in lenses:
            for comp in comps:
                out_name = f"{output_base}__{lens.key}__{comp.value}.mp4"
                out_path = os.path.join(out_dir, out_name)
                tasks.append(ReframeTask(
                    source_path=src,
                    source_index=src_idx,
                    lens=lens,
                    composition=comp,
                    output_path=out_path,
                ))
    return tasks


# ==================== FFmpeg Command ====================

def _reframe_with_retry(
    runner: FfmpegRunner,
    cmd: List[str],
    expected_duration_sec: float,
    on_log,
    stop_check,
    extra_progress_args: bool,
    tc_label: str,
    deadline_monotonic: Optional[float] = None,
) -> FfmpegResult:
    """FIX (V1.0.2.x, 2026-08-08): retry ffmpeg.run() up to MAX_RETRIES times.

    Returns the LAST FfmpegResult if all attempts failed.
    Aborts immediately on cancellation.
    """
    last: Optional[FfmpegResult] = None

    def _budget_exhausted() -> FfmpegResult:
        return FfmpegResult(
            success=False,
            returncode=124,
            error="reframe task timeout budget exhausted",
        )

    def _remaining_budget() -> Optional[float]:
        if deadline_monotonic is None:
            return None
        return deadline_monotonic - time.monotonic()

    for attempt in range(MAX_RETRIES):
        remaining = _remaining_budget()
        if remaining is not None:
            if remaining <= 0:
                return _budget_exhausted()
            # FfmpegRunner's wall cap is duration × factor with an idle floor.
            # Recompute both from the remaining total task budget so retries do
            # not multiply main's absolute per-task watchdog.
            runner.idle_timeout_sec = min(120.0, remaining)
            factor = remaining / max(expected_duration_sec, 0.001)
            runner.max_factor = factor
            if tc_label:
                runner.tc_factor_overrides[tc_label] = factor
        if stop_check is not None and stop_check():
            if last is None:
                last = runner.run(
                    cmd,
                    expected_duration_sec=expected_duration_sec,
                    on_log=on_log,
                    stop_check=stop_check,
                    extra_progress_args=extra_progress_args,
                    tc_label=tc_label,
                )
                last.cancelled = True
            return last
        try:
            result = runner.run(
                cmd,
                expected_duration_sec=expected_duration_sec,
                on_log=on_log,
                stop_check=stop_check,
                extra_progress_args=extra_progress_args,
                tc_label=tc_label,
            )
        except Exception:
            if attempt >= MAX_RETRIES - 1:
                raise
            delay = RETRY_DELAYS[attempt]
            remaining = _remaining_budget()
            if remaining is not None and remaining <= delay:
                return _budget_exhausted()
            on_log(
                f"[reframe] attempt {attempt + 1}/{MAX_RETRIES} raised; "
                f"sleeping {delay}s before retry"
            )
            time.sleep(delay)
            continue
        last = result
        if result.cancelled:
            return result
        if result.success:
            return result
        # A real hardware-process failure must rebuild the command with CPU
        # filters/encoder immediately. Retrying the same GPU command here would
        # hide the failure from the outer bounded fallback.
        if should_retry_with_cpu(cmd, result, stop_check=stop_check):
            return result
        if attempt >= MAX_RETRIES - 1:
            return result
        delay = RETRY_DELAYS[attempt]
        remaining = _remaining_budget()
        if remaining is not None and remaining <= delay:
            return _budget_exhausted()
        on_log(
            f"[reframe] attempt {attempt + 1}/{MAX_RETRIES} failed: "
            f"{result.error or 'unknown error'}; "
            f"sleeping {delay}s before retry"
        )
        time.sleep(delay)
    return last


def build_reframe_ffmpeg_command(
    source: str,
    output: str,
    lens: LensPreset,
    composition: Composition,
    output_width: int,
    output_height: int,
    encoder_codec: str,
    ffmpeg_cmd: str = "ffmpeg",
    bitrate: str = "8000k",
    ffprobe_cmd: str = "ffprobe",
    keep_audio: bool = True,
    reframe_mode: str = DEFAULT_REFRAME_MODE,
    max_parallel: int = 1,
    source_audio_state: Optional[MediaStreamState] = None,
    reframe_short_side: int = 0,
) -> List[str]:
    """
    สร้าง ffmpeg command สำหรับ 1 task
    Port ตรงจาก greenlnw._build_fixed_ffmpeg_command

    FIX (2026-07-02): audio ทำ explicit + normalize (aresample/aformat) เหมือน
    TC01 green_render เลย — ไม่พึ่ง `-map 0:a?` แบบ optional อีกต่อไป
    - keep_audio=True (video output): probe source; ถ้ามีเสียง → map 0:a:0 +
      normalize เป็น AAC stereo 48k; ถ้าไม่มี → -an สะอาด
    - keep_audio=False (preview PNG): -an เสมอ
    """
    target_aspect = output_width / output_height

    # FIX (2026-07-02): anchor-based composition (ported from greenlnw).
    # ก่อนหน้านี้ crop_x = "0"/"iw-ow"/"(iw-ow)/2" (ตรึงขอบ รุนแรง) — ตอนนี้ใช้
    # anchor + jitter + static-zoom วางผู้พูด/สินค้าตาม rule-of-thirds แทน
    comp_str = _composition_value(composition)
    try:
        lens_index = FIXED_7X3_LENS_KEYS.index(lens.key) + 1
    except ValueError:
        # FIX (2026-07-02): non-fixed lens → map focal (16..85mm) to 1..7 instead
        # of forcing 7 (which maxed zoom_bias for every non-fixed lens).
        lens_index = int(_clampf(round((lens.focal_mm - 16) / (85 - 16) * 6) + 1, 1, 7))
    x_anchor, y_anchor, static_zoom, jitter_x, jitter_y, legacy_tilt = _variation_recipe(
        source, lens, composition, lens_index,
    )
    # FIX (2026-07-02): profile mode selects how much tilt to keep
    # (speed=0, lite_tilt=subtle on left/right@lens2/4/6, legacy=full 5-10°).
    tilt_deg = _apply_reframe_mode(legacy_tilt, reframe_mode, lens_index, comp_str)
    scale = _crop_scale(lens.view_scale, static_zoom)

    crop_w = f"trunc((if(gte(iw/ih,{target_aspect:.8f}),ih*{target_aspect:.8f},iw)*{scale:.6f})/2)*2"
    crop_h = f"trunc((if(gte(iw/ih,{target_aspect:.8f}),ih,iw/{target_aspect:.8f})*{scale:.6f})/2)*2"
    x_anchor = _clampf(x_anchor, 0.10, 0.90)
    y_anchor = _clampf(y_anchor, 0.10, 0.90)
    jitter_x = _clampf(jitter_x, -0.08, 0.08)
    jitter_y = _clampf(jitter_y, -0.08, 0.08)
    crop_x = f"min(max(0,(iw/2+({jitter_x:.8f}*iw))-(ow*{x_anchor:.6f})),iw-ow)"
    crop_y = f"min(max(0,(ih/2+({jitter_y:.8f}*ih))-(oh*{y_anchor:.6f})),ih-oh)"

    use_cuda_scale = encoder_codec == "h264_nvenc" and _ffmpeg_has_filter(ffmpeg_cmd, "scale_cuda")
    # v3.REFRAME_720P: if reframe_short_side > 0, work at that short-side resolution
    # and let the next stage (chroma) upscale. Otherwise use full target output.
    if reframe_short_side and reframe_short_side > 0:
        # Round to even for yuv420p
        ref_w = int(reframe_short_side * (output_width / output_height))
        ref_w = ref_w & ~1
        ref_h = reframe_short_side & ~1
    else:
        ref_w, ref_h = output_width, output_height
    # FIX (2026-07-02): optional rotate (mirror-pad) สำหรับ tilt; ปิดไว้ default
    filters: List[str] = []
    if abs(tilt_deg) > 0.01:
        pad = _REF_ROTATE_PAD_PX
        if pad > 0:
            pad2 = pad * 2
            filters += [
                f"pad=w=iw+{pad2}:h=ih+{pad2}:x={pad}:y={pad}:color=black",
                f"fillborders=left={pad}:right={pad}:top={pad}:bottom={pad}:mode=mirror",
                f"rotate='{tilt_deg:.6f}*PI/180':ow=iw:oh=ih:c=black",
                f"crop=w=iw-{pad2}:h=ih-{pad2}:x={pad}:y={pad}",
            ]
        else:
            filters.append(f"rotate='{tilt_deg:.6f}*PI/180':ow=iw:oh=ih:c=black")
    filters.append(f"crop=w='{crop_w}':h='{crop_h}':x='{crop_x}':y='{crop_y}'")
    if use_cuda_scale:
        filters += [
            "setsar=1", "format=nv12", "hwupload_cuda",
            f"scale_cuda=w={ref_w}:h={ref_h}:interp_algo=lanczos:format=nv12",
        ]
    else:
        filters += [
            f"scale={ref_w}:{ref_h}:flags=lanczos",
            "setsar=1", "format=yuv420p",
        ]
    vf = ",".join(filters)

    cmd: List[str] = [
        ffmpeg_cmd, "-hide_banner", "-y",
        "-i", source,
        "-vf", vf,
        "-map", "0:v:0",
    ]
    # FIX (2026-07-02): explicit audio mapping. Normalize every source audio
    # format (mono/5.1/unusual sample fmt) into clean stereo 48k AAC so the
    # muxer never silently drops sound. `-an` only when there's genuinely no
    # audio (or for the 1-frame PNG preview, which can't hold audio anyway).
    if keep_audio and source_audio_state is None:
        source_audio_state = _source_audio_state(
            source,
            ffprobe_cmd,
            ffmpeg_cmd,
        )
    if keep_audio and source_audio_state is MediaStreamState.ERROR:
        raise ValueError("source audio probe failed")
    if keep_audio and source_audio_state is MediaStreamState.PRESENT:
        cmd += [
            "-map", "0:a:0",
            "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        ]
    else:
        cmd += ["-an"]
    cmd += ["-c:v", encoder_codec]
    # Encoder-specific args.
    # FIX (2026-07-02): h264_nvenc args now match greenlnw _reframe_nvenc_args()
    # defaults (p5/hq/vbr/cq19/b:v0/multipass disabled/spatial-aq 1/temporal-aq 1)
    # — was p6/cq17 only, which produced different quality/size than greenlnw.
    # FIX (2026-07-31): VideoToolbox hardware encoder support for macOS.
    # h264_videotoolbox uses -q:v as quality cap (mapped from UI preset)
    # and inherits the user-supplied -b:v bitrate from the chain.
    # FIX (V1.0.3, 2026-08-18): env-overridable NVENC preset/tune for batch throughput.
    # V3_REFRAME_NVENC_PRESET=p4 + V3_REFRAME_NVENC_TUNE=ll trades ~30% throughput for
    # negligible quality loss on TC02/TC04 batch (21+ outputs per source).
    # Default unchanged (p5/hq) when env unset — UI quality stays identical.
    if encoder_codec == "h264_nvenc":
        _nvenc_preset = os.environ.get("V3_REFRAME_NVENC_PRESET", "p5")
        _nvenc_tune = os.environ.get("V3_REFRAME_NVENC_TUNE", "hq")
        cmd += ["-preset", _nvenc_preset, "-tune", _nvenc_tune, "-rc", "vbr",
                "-cq", "19", "-b:v", "0", "-multipass", "disabled",
                "-spatial-aq", "1", "-temporal-aq", "1"]
    elif encoder_codec in {"hevc_nvenc", "av1_nvenc"}:
        cmd += ["-preset", "p6", "-cq", "19"]
    elif encoder_codec == "h264_qsv":
        cmd += ["-preset", "slow", "-b:v", bitrate]
    elif encoder_codec == "h264_amf":
        cmd += ["-quality", "quality", "-b:v", bitrate]
    elif encoder_codec == "libx264":
        # v3.PERF (2026-08-18): env-overridable preset for batch throughput.
        # Default "medium" — 2-3× faster than "slow" with minor quality loss.
        _libx264_preset = os.environ.get("V3_LIBX264_PRESET", "medium").strip() or "medium"
        cmd += ["-preset", _libx264_preset, "-crf", "18"]
    elif encoder_codec == "h264_videotoolbox":
        # macOS Apple Silicon/Intel H.264 hardware encoder.
        # VideoToolbox is bitrate-driven via -b:v; -prio_speed=1 trades async
        # latency for throughput (good for batch reframe).
        cmd += ["-b:v", bitrate, "-prio_speed", "1"]
    elif encoder_codec == "hevc_videotoolbox":
        cmd += ["-b:v", bitrate, "-prio_speed", "1"]
    else:
        raise ValueError(f"unsupported reframe encoder: {encoder_codec}")
    if not use_cuda_scale:
        cmd += ["-pix_fmt", "yuv420p"]
    # FIX (2026-07-02): CPU% limiter → -threads N, หารตามจำนวน parallel worker
    # (reframe รันขนาน max_parallel ตัว) เพื่อไม่ให้ผลรวม thread เกิน budget
    cmd += ["-threads", str(effective_ffmpeg_threads(max_parallel))]
    cmd += ["-movflags", "+faststart", output]
    return cmd


def preview_reframe(
    source: str,
    output_png: str,
    lens: LensPreset,
    composition: Composition,
    output_width: int,
    output_height: int,
    encoder_codec: str = "libx264",
    ffmpeg_cmd: str = "ffmpeg",
    reframe_mode: str = DEFAULT_REFRAME_MODE,
    on_log: Optional[Callable[[str], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    FIX (2026-06-22): สร้าง preview 1 เฟรมจาก reframe pipeline → PNG
    ใช้ build_reframe_ffmpeg_command + เพิ่ม -frames:v 1 -ss 0 + output เป็น PNG
    Returns: True ถ้าสำเร็จ
    """
    if not source or not os.path.isfile(source):
        return False
    cmd_for_filter = build_reframe_ffmpeg_command(
        source=source, output=output_png,
        lens=lens, composition=composition,
        output_width=output_width, output_height=output_height,
        encoder_codec="libx264", ffmpeg_cmd=ffmpeg_cmd,
        keep_audio=False,  # 1-frame PNG preview — never map audio
        reframe_mode=reframe_mode,
    )
    # Reuse the reframe filter graph, then write a real PNG image frame.
    try:
        vf = cmd_for_filter[cmd_for_filter.index("-vf") + 1].replace(
            "format=yuv420p", "format=rgb24"
        )
    except Exception:
        return False
    new_cmd: List[str] = [
        ffmpeg_cmd, "-hide_banner", "-y",
        "-ss", "0",
        "-i", source,
        "-vf", vf,
        "-frames:v", "1",
        "-map", "0:v:0",
        "-an",
        "-vcodec", "png",
        "-f", "image2",
        "-update", "1",
        output_png,
    ]
    try:
        if stop_check is not None:
            runner = FfmpegRunner(
                ffmpeg_cmd=ffmpeg_cmd,
                idle_timeout_sec=30.0,
                max_factor=1.0,
            )
            ffmpeg_result = runner.run(
                new_cmd,
                expected_duration_sec=0.0,
                on_log=on_log,
                stop_check=stop_check,
            )
            if ffmpeg_result.success and os.path.isfile(output_png):
                return True
            try:
                if os.path.isfile(output_png):
                    os.remove(output_png)
            except OSError:
                pass
            return False

        result = subprocess.run(
            new_cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=30, creationflags=NO_WINDOW_FLAGS,
        )
        if result.returncode == 0 and os.path.isfile(output_png):
            return True
        return False
    except Exception:
        return False


# ==================== Render Plan ====================

@dataclass
class ReframeResult:
    task: ReframeTask
    success: bool
    error: str = ""
    duration_sec: float = 0.0
    cancelled: bool = False


def render_reframe_plan(
    sources: List[str],
    out_dir: str,
    settings: ReframeSettings,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, ReframeTask], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    tc_label: str = "",  # TC tag passed to FfmpegRunner watchdog policy
    per_task_timeout_sec: float = 600.0,
    pre_validated_outputs: Optional[set] = None,
    # v3.PIPELINE (2026-08-18): per-task callback fired when each reframe output is saved.
    # Enables per-pipeline (producer-consumer) architecture in TC04: reframe→chroma overlap.
    on_reframe_ready: Optional[Callable[[ReframeResult], None]] = None,
) -> List[ReframeResult]:
    """
    Render plan: ทุก source × lens × composition

    Args:
        sources: list ของ source video paths
        out_dir: โฟลเดอร์ output
        settings: ReframeSettings
        on_log: log callback
        on_progress: (current, total, task) callback
        stop_check: cancel callback
        ffmpeg_cmd / ffprobe_cmd: paths
        tc_label: TC tag used by FfmpegRunner for watchdog policy and logs.

    Returns:
        List[ReframeResult] เรียงตาม task order
    """
    log = on_log or (lambda m: None)
    tasks = build_reframe_tasks(sources, out_dir, settings)
    log(f"[reframe] sources={len(sources)} → tasks={len(tasks)}")
    log(f"[reframe] platform={settings.platform_key} ({settings.output_width}x{settings.output_height})")
    log(f"[reframe] lens count={len(set(t.lens.key for t in tasks))}, comps={settings.compositions}")
    log(f"[reframe] encoder={settings.encoder_alias}, max_parallel={settings.max_parallel_ffmpeg}")

    if not tasks:
        log("[reframe] no tasks to render")
        return []

    def _preflight_results(
        error: str,
        *,
        cancelled: bool = False,
    ) -> List[ReframeResult]:
        log(f"[reframe] preflight failed: {error}")
        return [
            ReframeResult(
                task=task,
                success=False,
                error=error,
                cancelled=cancelled,
            )
            for task in tasks
        ]

    # Probe each unique source once before scheduling any encodes. An audio
    # probe ERROR is not equivalent to a confirmed silent source: fail closed
    # instead of silently discarding Product audio from every derivative.
    source_audio_states: Dict[str, MediaStreamState] = {}
    source_durations: Dict[str, float] = {}
    for src in sorted(set(t.source_path for t in tasks)):
        try:
            audio_state = _source_audio_state(
                src,
                ffprobe_cmd,
                ffmpeg_cmd,
                stop_check=stop_check,
            )
        except MediaProbeCancelled as exc:
            return _preflight_results(str(exc), cancelled=True)

        source_audio_states[src] = audio_state
        if audio_state is MediaStreamState.ERROR:
            return _preflight_results(
                f"source audio probe failed: {os.path.basename(src)}"
            )
        if audio_state is MediaStreamState.PRESENT:
            log(
                f"[reframe] audio: {os.path.basename(src)} "
                "-> kept (AAC stereo 48k)"
            )
        else:
            log(
                f"[reframe] audio: {os.path.basename(src)} "
                "-> NONE (source has no audio stream)"
            )

        source_duration = _probe_duration(src, ffprobe_cmd)
        if source_duration <= 0:
            return _preflight_results(
                f"source video duration probe failed: {os.path.basename(src)}"
            )
        source_durations[src] = source_duration

    # Resolve encoder
    try:
        enc_name, enc_args = effective_video_encoder(
            preferred=resolve_encoder_alias(settings.encoder_alias),
            ffmpeg_cmd=ffmpeg_cmd,
        )
        log(f"[reframe] using encoder: {enc_name}")
        log(f"[reframe] scale filter: {'scale_cuda' if enc_name == 'h264_nvenc' and _ffmpeg_has_filter(ffmpeg_cmd, 'scale_cuda') else 'scale'}")
    except Exception as e:
        log(f"[reframe] ❌ encoder error: {e}")
        return []

    # Add encoder-specific args to bitrate handling
    # (build_reframe_ffmpeg_command dispatches on the resolved encoder_name)

    results: List[ReframeResult] = []
    total = len(tasks)
    # v3.PERF (2026-08-18): auto-scale max_parallel_ffmpeg from cpu_count when 0.
    # 0 → derive: <8 cores → 2, <16 → 3, ≥16 → 4 (capped at 6 to avoid GPU contention).
    _requested = settings.max_parallel_ffmpeg
    if _requested <= 0:
        try:
            import os as _os_auto
            _ncpu = _os_auto.cpu_count() or 4
        except Exception:
            _ncpu = 4
        if _ncpu < 8:
            _requested = 2
        elif _ncpu < 16:
            _requested = 3
        elif _ncpu < 32:
            _requested = 4
        else:
            _requested = 6
        log(f"[reframe] auto max_parallel={_requested} (cpu_count={_ncpu})")
    max_parallel = max(1, min(_requested, 3))

    task_order = {id(task): index for index, task in enumerate(tasks)}
    # Use a ThreadPoolExecutor (ported from _run_fixed_ffmpeg_reframe).
    completed = 0

    def _encode_one(task: ReframeTask) -> ReframeResult:
        if stop_check and stop_check():
            return ReframeResult(
                task=task,
                success=False,
                error="cancelled before start",
                cancelled=True,
            )

        if pre_validated_outputs and task.output_path in pre_validated_outputs:
            log(
                f"[reframe] skipping {os.path.basename(task.output_path)} "
                "(pre-validated)"
            )
            return ReframeResult(
                task=task,
                success=True,
                duration_sec=0.0,
            )

        # Encode to a sibling temporary MP4, then atomically publish only a
        # validated non-empty file. Cancel/failure can never leave a partial
        # file at the final output path.
        partial_path = (
            f"{task.output_path}.partial.{threading.get_ident()}.mp4"
        )
        partial_paths = [partial_path]
        cmd = build_reframe_ffmpeg_command(
            source=task.source_path,
            output=partial_path,
            lens=task.lens,
            composition=task.composition,
            output_width=settings.output_width,
            output_height=settings.output_height,
            encoder_codec=enc_name,
            ffmpeg_cmd=ffmpeg_cmd,
            bitrate=settings.bitrate,
            ffprobe_cmd=ffprobe_cmd,
            reframe_mode=settings.reframe_mode,
            max_parallel=max_parallel,
            source_audio_state=source_audio_states[task.source_path],
            reframe_short_side=settings.reframe_short_side,
        )
        start = time.time()
        expected_duration = source_durations[task.source_path]
        hard_timeout = max(1.0, float(per_task_timeout_sec))
        task_deadline = time.monotonic() + hard_timeout
        timeout_factor = hard_timeout / max(expected_duration, 0.001)
        runner = FfmpegRunner(
            ffmpeg_cmd=ffmpeg_cmd,
            idle_timeout_sec=min(120.0, hard_timeout),
            max_factor=timeout_factor,
            tc_factor_overrides={tc_label: timeout_factor}
            if tc_label
            else None,
        )
        # FIX (V1.0.2.x, 2026-08-08): retry transient ffmpeg failures via helper.
        try:
            ffmpeg_result = _reframe_with_retry(
                runner,
                cmd,
                expected_duration,
                log,
                stop_check,
                True,
                tc_label,
                deadline_monotonic=task_deadline,
            )
            if should_retry_with_cpu(cmd, ffmpeg_result, stop_check=stop_check):
                gpu_error = (
                    ffmpeg_result.error
                    or f"hardware encoder exited {ffmpeg_result.returncode}"
                )
                remove_partial(partial_path)
                cpu_partial_path = (
                    f"{task.output_path}.partial.cpu.{threading.get_ident()}."
                    f"{time.time_ns()}.mp4"
                )
                partial_paths.append(cpu_partial_path)
                log(
                    "[reframe] hardware encoder failed during render; "
                    "rebuilding task with CPU scale + libx264"
                )
                cpu_cmd = build_reframe_ffmpeg_command(
                    source=task.source_path,
                    output=cpu_partial_path,
                    lens=task.lens,
                    composition=task.composition,
                    output_width=settings.output_width,
                    output_height=settings.output_height,
                    encoder_codec="libx264",
                    ffmpeg_cmd=ffmpeg_cmd,
                    bitrate=settings.bitrate,
                    ffprobe_cmd=ffprobe_cmd,
                    reframe_mode=settings.reframe_mode,
                    max_parallel=max_parallel,
                    source_audio_state=source_audio_states[task.source_path],
                    reframe_short_side=settings.reframe_short_side,
                )
                remaining = task_deadline - time.monotonic()
                if remaining <= 0:
                    cpu_result = FfmpegResult(
                        success=False,
                        returncode=124,
                        error="reframe task timeout budget exhausted before CPU fallback",
                    )
                else:
                    cpu_factor = remaining / max(expected_duration, 0.001)
                    cpu_runner = FfmpegRunner(
                        ffmpeg_cmd=ffmpeg_cmd,
                        idle_timeout_sec=min(120.0, remaining),
                        max_factor=cpu_factor,
                        tc_factor_overrides={tc_label: cpu_factor}
                        if tc_label
                        else None,
                    )
                    cpu_result = cpu_runner.run(
                        cpu_cmd,
                        expected_duration_sec=expected_duration,
                        on_log=log,
                        stop_check=stop_check,
                        extra_progress_args=True,
                        tc_label=tc_label,
                    )
                if not cpu_result.success:
                    cpu_result.error = (
                        f"hardware encoder failed: {gpu_error}; "
                        f"CPU fallback failed: "
                        f"{cpu_result.error or cpu_result.returncode}"
                    )
                ffmpeg_result = cpu_result
                cmd = cpu_cmd
                partial_path = cpu_partial_path
            duration = time.time() - start
            validation_error = ""
            if ffmpeg_result.success:
                if (
                    not os.path.isfile(partial_path)
                    or os.path.getsize(partial_path) <= 0
                ):
                    validation_error = "reframe output missing or empty"
                elif not has_video_stream(
                    partial_path,
                    ffprobe_cmd=ffprobe_cmd,
                    ffmpeg_cmd=ffmpeg_cmd,
                ):
                    validation_error = "reframe output has no readable video stream"
                else:
                    expected_duration = source_durations[task.source_path]
                    partial_duration = _probe_duration(partial_path, ffprobe_cmd)
                    if not _duration_matches_expected(
                        partial_duration,
                        expected_duration,
                    ):
                        tolerance = _duration_tolerance(expected_duration)
                        validation_error = (
                            "reframe output duration "
                            f"{partial_duration:.3f}s differs from source "
                            f"{expected_duration:.3f}s "
                            f"(tolerance {tolerance:.3f}s)"
                        )
                    elif (
                        source_audio_states[task.source_path]
                        is MediaStreamState.PRESENT
                    ):
                        output_audio_state = audio_stream_state(
                            partial_path,
                            ffprobe_cmd=ffprobe_cmd,
                            ffmpeg_cmd=ffmpeg_cmd,
                        )
                        if output_audio_state is MediaStreamState.ERROR:
                            validation_error = (
                                "reframe output required audio probe failed"
                            )
                        elif output_audio_state is not MediaStreamState.PRESENT:
                            validation_error = (
                                "reframe output lost required Product audio stream"
                            )

                    if not validation_error:
                        _publish_reframe_output(partial_path, task.output_path)
                        return ReframeResult(
                            task=task,
                            success=True,
                            duration_sec=duration,
                        )
            error = validation_error or ffmpeg_result.error or (
                "cancelled" if ffmpeg_result.cancelled else "ffmpeg failed"
            )
            return ReframeResult(
                task=task,
                success=False,
                error=error[-300:],
                duration_sec=duration,
                cancelled=bool(ffmpeg_result.cancelled),
            )
        except Exception as exc:
            return ReframeResult(
                task=task,
                success=False,
                error=str(exc),
                duration_sec=time.time() - start,
            )
        finally:
            for candidate in partial_paths:
                try:
                    if os.path.isfile(candidate):
                        os.remove(candidate)
                except OSError:
                    pass

    with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="reframe-") as ex:
        future_to_task = {ex.submit(_encode_one, t): t for t in tasks}
        cancel_observed = False
        for fut in as_completed(future_to_task):
            # A queued future cancelled below never ran and therefore has no
            # output/result to report. Futures that had already started cannot
            # be cancelled here; drain them so every atomically published file
            # has a matching ReframeResult.
            if fut.cancelled():
                continue
            result = fut.result()
            results.append(result)
            completed += 1

            # Read the completed result before observing late Stop. The worker
            # may have atomically published its output immediately before the
            # UI set Stop; dropping this future would leave an orphan MP4 that
            # the returned result list falsely omitted. Cancel only work that
            # has not started, then keep draining already-running futures.
            if not cancel_observed and stop_check and stop_check():
                cancel_observed = True
                log("[reframe] cancelled")
                for pending in future_to_task:
                    if pending is not fut and not pending.done():
                        pending.cancel()
            if on_progress:
                try:
                    on_progress(completed, total, result.task)
                except Exception:
                    pass
            status = "✅" if result.success else f"❌ {result.error[:100]}"
            log(f"[reframe] [{completed}/{total}] {result.task.lens.key}/{result.task.composition.value}: {status}")

            # v3.PIPELINE (2026-08-18): per-pipeline callback for TC04 producer-consumer.
            if on_reframe_ready and result.success:
                try:
                    on_reframe_ready(result)
                except Exception as _exc:
                    log(f"[reframe] on_reframe_ready exception: {_exc}")

    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count
    log(f"[reframe] 🏁 done: {success_count} ok, {fail_count} fail (of {total})")
    return sorted(results, key=lambda result: task_order[id(result.task)])


# ==================== Convenience: list source files ====================

def list_video_files(folder: str, exts: Tuple[str, ...] = VIDEO_EXTS) -> List[str]:
    """คืน list ของไฟล์วิดีโอในโฟลเดอร์ (เรียงตามชื่อ)"""
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for name in os.listdir(folder):
        if name.lower().endswith(exts):
            out.append(os.path.join(folder, name))
    return sorted(out)
