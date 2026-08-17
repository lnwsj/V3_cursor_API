"""
core/contract.py — Single source of truth for building settings dataclasses
from raw `SettingsPanel.get_values()` dicts.

Pure Python. No tkinter imports.

Why this file exists:
    TC01, TC03, TC04 all build a `GreenSettings(...)` from a values dict.
    Before this refactor each tab inlined the same 13-line constructor with
    hard-coded defaults and the same `audio_source` heuristic, drifting in
    subtle ways across tabs (e.g. TC03 used `audio_source="background" if
    audios else "none"` while TC01 used the same expression but read it
    from a different field).

    Now every pipeline (and every tab) goes through the factories below.

Factories:
    green_settings_for(values, *, has_audio=False, has_cover=False)
    reframe_settings_for(values, *, compositions, max_parallel=3,
                        encoder="auto", width=1920, height=1080)
    batch_settings_for(*, segment_duration, num_outputs, has_audio=False,
                       match_mode_str="no_repeat")

Hardcoded V3 contract values are kept here so they live in one place:
    TC01 / TC03 / TC04 chroma key: despill_screen=True
    TC01 / TC03 / TC04 cover intro: 2.0s when a cover is supplied
    TC03 segment_duration: configurable 0.5..600s, default 10s
    TC05 max_parallel_ffmpeg: configurable 1..3, default 3
    TC06 active pipeline: TC01 Chroma + Audio master with boolean clip reuse
    Legacy standalone loop utility: target_seconds 0.5..600s, default 30s
"""
from __future__ import annotations
import sys

import math
import re
import os
from typing import Any, Dict, List, Mapping, Sequence

from .green_render import GreenSettings
from .ai_reframe import ReframeSettings
from .batch_pingpong import BatchSettings, MatchMode
from .gpu_detector import ALIAS_MAP, UI_PRESET_PROFILES


# =====================================================================
# Visibility — Advanced tabs
# =====================================================================
#
# V1.0.0.17: TC05 (reframe-only) and TC06 (chroma + audio-master) are
# progressively shown to general users. They are still implemented and
# usable from CLI (`--tc TC05`, `--tc TC06`) for power users / scripted
# pipelines, but the main Tk notebook only renders the four core tabs
# (TC01, TC02, TC03, TC04).
#
# Override:
#   * Set the env var  V3_SHOW_ALL_TABS=1   to show TC05 + TC06 in the notebook.
#   * The startup log announces which mode is in effect.

DEFAULT_VISIBLE_TC_LABELS: List[str] = ["TC01", "TC02", "TC03", "TC04"]
ADVANCED_TC_LABELS: List[str] = ["TC05", "TC06"]


def show_all_tabs_enabled() -> bool:
    """Return True when the user has opted in to see TC05 / TC06.

    Default: hidden (general users). Opt-in via ``V3_SHOW_ALL_TABS=1``.
    """
    return os.environ.get("V3_SHOW_ALL_TABS", "").strip().lower() in (
        "1", "true", "yes", "on", "show", "all",
    )


def visible_tc_labels() -> List[str]:
    """Return the ordered list of TC labels that should be shown in the UI."""
    labels = list(DEFAULT_VISIBLE_TC_LABELS)
    if show_all_tabs_enabled():
        labels = labels + list(ADVANCED_TC_LABELS)
    return labels


# =====================================================================
# Numeric contract boundaries
# =====================================================================

TC03_SEGMENT_DURATION_MIN = 0.5
TC03_SEGMENT_DURATION_MAX = 600.0
TC03_SEGMENT_DURATION_DEFAULT = 10.0

TC05_WORKERS_MIN = 1
TC05_WORKERS_MAX = 3
TC05_WORKERS_DEFAULT = 3

TC06_TARGET_SECONDS_MIN = 0.5
TC06_TARGET_SECONDS_MAX = 600.0
TC06_TARGET_SECONDS_DEFAULT = 30.0

COVER_INTRO_SECONDS = 2.0
FIXED_REFRAME_LENS_COUNT = 7

# TC01-TC04 output settings are intentionally shared. The desktop product is
# documented as non-4K and its UI contract is 360..3840 pixels at 15..60 FPS.
# YUV420 output additionally requires both dimensions to be even.
OUTPUT_DIMENSION_MIN = 360
OUTPUT_DIMENSION_MAX = 3840
OUTPUT_FPS_MIN = 15
OUTPUT_FPS_MAX = 60
REFRAME_PARALLEL_MIN = 1
REFRAME_PARALLEL_MAX = 10
VALID_REFRAME_MODES = frozenset({"speed", "lite_tilt", "legacy"})
# FIX (2026-07-31): macOS-only h264_videotoolbox option surfaced in UI.
# On non-darwin it's not appended so Windows / Linux users don't see an
# unsupported choice.
TC01_TC04_ENCODER_CHOICES = (
    "auto",
    "h264_nvenc",
    "hevc_nvenc",
    "av1_nvenc",
    "h264_qsv",
    "h264_amf",
    "libx264",
    *(
        ("h264_videotoolbox",)
        if sys.platform == "darwin"
        else ()
    ),
)
TC01_TC04_PRESET_CHOICES = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "hq",
)

_BITRATE_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)?)(?P<suffix>[kKmMgG]?)$"
)
_HEX_COLOR_RE = re.compile(r"^#(?P<hex>[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")


# =====================================================================
# Defaults — mirror the *_FIELDS lists in ui/tabs/*.
# =====================================================================

# Shared final Chroma defaults.  TC01-TC04 expose these through the same
# canonical UI panel; pipeline-specific defaults live in separate dictionaries.
TC01_TC04_CHROMA_DEFAULTS: Dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "bitrate": "6000k",
    "encoder": "h264_nvenc",
    "preset": "medium",
    "key_color": "#00FF00",
    # FIX (V1.0.0.19): tighter similarity default (0.32 -> 0.29) — the
    # previous default 0.32 was too tolerant and allowed green bleed on
    # the edges of the source. 0.29 keeps the chroma tight while still
    # tolerating minor colour variation at the edge.
    "similarity": 0.29,
    "blend": 0.04,
    "despill": 0.32,
}

# TC01 defaults (ui/tabs/tc01_chroma.py)
TC01_VIDEO_DEFAULTS: Dict[str, Any] = {
    **TC01_TC04_CHROMA_DEFAULTS,
    "seed": 0,
}

# TC02 defaults — shared final Chroma + TC02 Reframe controls.
TC02_VIDEO_DEFAULTS: Dict[str, Any] = {
    **TC01_TC04_CHROMA_DEFAULTS,
    "max_parallel": 3,
    "reframe_mode": "speed",
}

# TC03 defaults — shared final Chroma + Batch matching.
TC03_VIDEO_DEFAULTS: Dict[str, Any] = {
    **TC01_TC04_CHROMA_DEFAULTS,
    "match_mode": "no_repeat",
    "seed": 0,
}

# TC04 — three settings panels
TC04_VIDEO_DEFAULTS: Dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "bitrate": "6000k",
    "encoder": "h264_nvenc",
    "preset": "medium",
    "key_color": "#00FF00",
    # FIX (V1.0.0.19): see TC01 — 0.32 -> 0.29 for tighter chroma.
    "similarity": 0.29,
    "blend": 0.04,
    "despill": 0.32,
}
TC04_REFRAME_DEFAULTS: Dict[str, Any] = {
    "max_parallel": 3,
    "reframe_mode": "speed",
}
TC04_BATCH_DEFAULTS: Dict[str, Any] = {
    "segment_duration": 10.0,
    "match_mode": "no_repeat",
}

# TC05 defaults (ui/tabs/reframe_only_direct.py::DIRECT_REFRAME_FIELDS) — vertical short-form default
TC05_VIDEO_DEFAULTS: Dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "encoder": "auto",
    "bitrate": "8000k",
    "ffmpeg_workers": TC05_WORKERS_DEFAULT,
}

# TC06 defaults: TC01-compatible Chroma, then audio-master assembly.
TC06_VIDEO_DEFAULTS: Dict[str, Any] = {
    **TC01_VIDEO_DEFAULTS,
    "allow_clip_reuse": True,
}

# Kept only for the standalone legacy loop utility.  These values are no
# longer part of the active TC06 UI/pipeline contract.
TC06_LEGACY_LOOP_DEFAULTS: Dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "bitrate": "6000k",
    "encoder": "nvenc",
    "preset": "medium",
    "target_seconds": TC06_TARGET_SECONDS_DEFAULT,
    "mode": "loop",
    "crossfade_seconds": 0.0,
}


# =====================================================================
# Shared value validators
# =====================================================================

def _finite_float_in_range(
    value: Any,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    """Return ``value`` as a finite float inside an inclusive range.

    Numeric strings are accepted because raw values can originate from UI or
    CLI serialization. Booleans, non-numeric values, NaN, infinities, and
    values outside the contract are rejected with one consistent ValueError.
    """
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a finite number between "
            f"{minimum:g} and {maximum:g}; got {value!r}"
        ) from None

    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(
            f"{field_name} must be a finite number between "
            f"{minimum:g} and {maximum:g}; got {value!r}"
        )
    return number


def _integral_in_range(
    value: Any,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    """Return a strict finite integer inside an inclusive range.

    Numeric strings are supported for CLI/JSON parity. Booleans and fractional
    numbers are rejected rather than being silently converted by ``int()``.
    """
    try:
        if isinstance(value, bool):
            raise TypeError
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"{field_name} must be an integer between {minimum} and {maximum}; "
            f"got {value!r}"
        ) from None
    if (
        not math.isfinite(numeric)
        or not numeric.is_integer()
        or not minimum <= numeric <= maximum
    ):
        raise ValueError(
            f"{field_name} must be an integer between {minimum} and {maximum}; "
            f"got {value!r}"
        )
    return int(numeric)


def validate_output_dimension(value: Any, *, field_name: str) -> int:
    """Validate one TC01-TC04 YUV420 output dimension."""
    dimension = _integral_in_range(
        value,
        field_name=field_name,
        minimum=OUTPUT_DIMENSION_MIN,
        maximum=OUTPUT_DIMENSION_MAX,
    )
    if dimension % 2:
        raise ValueError(
            f"{field_name} must be even for yuv420p output; got {value!r}"
        )
    return dimension


def validate_output_fps(value: Any) -> int:
    """Validate the shared TC01-TC04 output frame-rate contract."""
    return _integral_in_range(
        value,
        field_name="fps",
        minimum=OUTPUT_FPS_MIN,
        maximum=OUTPUT_FPS_MAX,
    )


def validate_video_bitrate(value: Any) -> str:
    """Validate and canonicalize a positive FFmpeg video bitrate.

    Accepted examples: ``6000000``, ``6000k``, ``8M``, ``1.5m``. Whitespace,
    signs, scientific notation, option-like strings, zero and non-finite values
    are rejected so the value always remains one safe FFmpeg argument.
    """
    if isinstance(value, bool) or value is None:
        match = None
        text = ""
    else:
        text = str(value).strip()
        match = _BITRATE_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "bitrate must be a positive number with optional k, M, or G suffix; "
            f"got {value!r}"
        )
    number = float(match.group("number"))
    if not math.isfinite(number) or number <= 0:
        raise ValueError(
            "bitrate must be a positive number with optional k, M, or G suffix; "
            f"got {value!r}"
        )
    return f"{match.group('number')}{match.group('suffix').lower()}"


def validate_key_color(value: Any) -> str:
    """Validate ``#RGB``/``#RRGGBB`` and return uppercase ``#RRGGBB``."""
    if not isinstance(value, str):
        match = None
    else:
        match = _HEX_COLOR_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            f"key_color must be #RGB or #RRGGBB hexadecimal; got {value!r}"
        )
    digits = match.group("hex")
    if len(digits) == 3:
        digits = "".join(character * 2 for character in digits)
    return f"#{digits.upper()}"


# Sentinel value used by callers to opt into auto-detect mode.
# Resolved by `_resolve_key_color` below; the value is intentionally
# not a valid hex color so it can never reach `validate_key_color`.
AUTO_DETECT_KEY_COLOR = "auto"


def _resolve_key_color(
    value: Any,
    *,
    auto_detect: bool = False,
    product_path: str | None = None,
) -> str:
    """Resolve a key_color value, optionally auto-detecting from a video.

    Logic:
      1. If `auto_detect=True` and `product_path` is a readable file,
         call `detect_key_color` and return its result.
      2. Otherwise, if value is the sentinel `"auto"`, return the
         hardcoded default `#00FF00` and emit a warning.
      3. Otherwise validate as a normal hex color.

    Note: imports are local to avoid a top-level cycle
    (key_color_detector imports from PIL).
    """
    if auto_detect and product_path:
        try:
            from core.key_color_detector import detect_key_color
            result = detect_key_color(product_path, strategy="auto")
            return result.color
        except Exception as exc:  # pragma: no cover - safety net
            # Fall through to the hardcoded value if detection fails.
            import logging
            logging.getLogger(__name__).warning(
                "auto-detect failed for %s: %s — using #00FF00", product_path, exc
            )
    if isinstance(value, str) and value.strip().lower() == AUTO_DETECT_KEY_COLOR:
        if not (auto_detect and product_path):
            import logging
            logging.getLogger(__name__).warning(
                "key_color='auto' but auto_detect is off or product_path is missing "
                "— falling back to #00FF00"
            )
        return "#00FF00"
    return value


def validate_unit_interval(value: Any, *, field_name: str) -> float:
    """Validate a finite chroma parameter in the inclusive 0..1 interval."""
    return _finite_float_in_range(
        value,
        field_name=field_name,
        minimum=0.0,
        maximum=1.0,
    )


def validate_encoder_alias(value: Any) -> str:
    """Return one known encoder alias; never turn a typo into ``auto``."""
    alias = str(value).strip().lower() if not isinstance(value, bool) else ""
    if alias not in ALIAS_MAP:
        allowed = ", ".join(sorted(ALIAS_MAP))
        raise ValueError(
            f"encoder must be one of: {allowed}; got {value!r}"
        )
    return alias


def validate_encoder_preset(value: Any) -> str:
    """Return one shared cross-encoder preset profile."""
    preset = str(value).strip().lower() if not isinstance(value, bool) else ""
    if preset not in UI_PRESET_PROFILES:
        allowed = ", ".join(sorted(UI_PRESET_PROFILES))
        raise ValueError(
            f"preset must be one of: {allowed}; got {value!r}"
        )
    return preset


def validate_reframe_mode(value: Any) -> str:
    """Return a canonical reframe mode; reject unknown silent fallbacks."""
    mode = str(value).strip().lower() if not isinstance(value, bool) else ""
    if mode not in VALID_REFRAME_MODES:
        allowed = ", ".join(sorted(VALID_REFRAME_MODES))
        raise ValueError(
            f"reframe_mode must be one of: {allowed}; got {value!r}"
        )
    return mode


def validate_reframe_parallel(value: Any) -> int:
    """Validate TC02/TC04 reframe worker count without truncation/clamping."""
    return _integral_in_range(
        value,
        field_name="max_parallel",
        minimum=REFRAME_PARALLEL_MIN,
        maximum=REFRAME_PARALLEL_MAX,
    )


def validate_tc03_segment_duration(
    value: Any = TC03_SEGMENT_DURATION_DEFAULT,
) -> float:
    """Validate TC03 segment duration (0.5..600s, default 10s)."""
    return _finite_float_in_range(
        value,
        field_name="segment_duration",
        minimum=TC03_SEGMENT_DURATION_MIN,
        maximum=TC03_SEGMENT_DURATION_MAX,
    )


def validate_tc06_target_seconds(
    value: Any = TC06_TARGET_SECONDS_DEFAULT,
) -> float:
    """Validate TC06 target duration (0.5..600s, default 30s)."""
    return _finite_float_in_range(
        value,
        field_name="target_seconds",
        minimum=TC06_TARGET_SECONDS_MIN,
        maximum=TC06_TARGET_SECONDS_MAX,
    )


def clamp_tc05_workers(values: Mapping[str, Any]) -> int:
    """Return TC05's effective worker count, clamped to 1..3.

    ``ffmpeg_workers`` is the canonical UI key; ``max_parallel`` remains a
    compatibility fallback. Missing, non-integral, non-finite, or otherwise
    invalid values use the contract default of 3.
    """
    raw_value = values.get(
        "ffmpeg_workers",
        values.get("max_parallel", TC05_WORKERS_DEFAULT),
    )
    try:
        if isinstance(raw_value, bool):
            raise TypeError
        numeric = float(raw_value)
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError
        requested = int(numeric)
    except (TypeError, ValueError, OverflowError):
        requested = TC05_WORKERS_DEFAULT
    return max(TC05_WORKERS_MIN, min(requested, TC05_WORKERS_MAX))


# =====================================================================
# Factories
# =====================================================================

def green_settings_for(
    values: Dict[str, Any],
    *,
    has_audio: bool = False,
    has_cover: bool = False,
    defaults: Dict[str, Any] = TC01_VIDEO_DEFAULTS,
) -> GreenSettings:
    """Build a GreenSettings from a values dict.

    Args:
        values:    raw SettingsPanel.get_values() output
        has_audio: True if user supplied audio files in the DropZone
        has_cover: True if user supplied a cover in the DropZone
                   (note: TC01/03/04 render loop does the cover wiring
                    outside the settings dataclass, but we mirror the
                    flag for symmetry with `_build_preview_kwargs` in
                    the original tab code)
        defaults:  which TC's default field values to fall back to.
                   Pass TC03_VIDEO_DEFAULTS or TC04_VIDEO_DEFAULTS if
                   you want those defaults instead of TC01.

    Returns:
        A GreenSettings ready to hand to `render_green()`.

    Note:
        `despill_screen=True` is hardcoded — matches ui/tabs/green_single.py.
        `cover_enabled` is set to `has_cover`; if your render loop wires
        covers itself (TC01 does), pass has_cover=False.
    """
    return GreenSettings(
        width=validate_output_dimension(
            values.get("width", defaults["width"]), field_name="width"
        ),
        height=validate_output_dimension(
            values.get("height", defaults["height"]), field_name="height"
        ),
        fps=validate_output_fps(values.get("fps", defaults.get("fps", 30))),
        bitrate=validate_video_bitrate(
            values.get("bitrate", defaults["bitrate"])
        ),
        encoder_alias=validate_encoder_alias(
            values.get("encoder", defaults["encoder"])
        ),
        key_color=validate_key_color(
            _resolve_key_color(
                values.get("key_color", defaults["key_color"]),
                auto_detect=values.get("auto_detect_key_color", False),
                product_path=values.get("_product_path"),
            )
        ),
        similarity=validate_unit_interval(
            values.get("similarity", defaults["similarity"]),
            field_name="similarity",
        ),
        blend=validate_unit_interval(
            values.get("blend", defaults["blend"]), field_name="blend"
        ),
        despill=validate_unit_interval(
            values.get("despill", defaults["despill"]), field_name="despill"
        ),
        despill_screen=True,             # V3 HARDCODE
        cover_enabled=has_cover,
        cover_duration=COVER_INTRO_SECONDS if has_cover else 0.0,
        cover_scale=1.0,
        audio_source="background" if has_audio else "product",
        preset=validate_encoder_preset(
            values.get("preset", defaults.get("preset", "medium"))
        ),
    )


def reframe_settings_for(
    values: Dict[str, Any],
    *,
    compositions: Sequence[str],
    defaults: Dict[str, Any] = TC02_VIDEO_DEFAULTS,
    encoder_override: str = "",
    width_override: int = 0,
    height_override: int = 0,
    max_parallel_override: int = 0,
) -> ReframeSettings:
    """Build a ReframeSettings from a values dict.

    Args:
        values:        raw SettingsPanel.get_values() output
        compositions:  list of "center" / "left" / "right" strings.
                       Empty list → caller should supply a default upstream.
        defaults:      TC02 / TC05 VIDEO_DEFAULTS
        encoder_override:  if non-empty, override encoder_alias (e.g. TC04
                           uses values from a different panel).
        width_override / height_override:  if > 0, override (TC04 reframe
                           shares dimensions with the video panel).
        max_parallel_override: if > 0, override (TC04 reframe uses its own
                              panel's max_parallel).

    Returns:
        A ReframeSettings ready to hand to `render_reframe_plan()`.
    """
    raw_width = width_override or values.get("width", defaults["width"])
    raw_height = height_override or values.get("height", defaults["height"])
    raw_encoder = encoder_override or values.get("encoder", defaults["encoder"])
    raw_max_parallel = max_parallel_override or values.get(
        "max_parallel",
        values.get("ffmpeg_workers", defaults.get("max_parallel", 3)),
    )
    return ReframeSettings(
        use_fixed_7x3=True,
        platform_key="tiktok",   # default = vertical short-form
        output_width=validate_output_dimension(raw_width, field_name="width"),
        output_height=validate_output_dimension(raw_height, field_name="height"),
        compositions=list(compositions),
        encoder_alias=validate_encoder_alias(raw_encoder),
        bitrate=validate_video_bitrate(
            values.get("bitrate", defaults["bitrate"])
        ),
        max_parallel_ffmpeg=validate_reframe_parallel(raw_max_parallel),
        reframe_mode=validate_reframe_mode(
            values.get("reframe_mode", "speed")
        ),
    )

def batch_settings_for(
    *,
    segment_duration: float,
    num_outputs: int,
    has_audio: bool = False,
    match_mode_str: str = "no_repeat",
    product_ping_pong: bool = True,
    background_ping_pong: bool = False,
    use_split_by_duration: bool = True,
) -> BatchSettings:
    """Build a BatchSettings.

    Centralizes the match_mode string -> enum translation that TC03 and TC04
    each had inline.

    Args:
        segment_duration:       seconds per segment (TC03/TC04 user setting,
                                0.5..600 seconds; default 10)
        num_outputs:            expected number of outputs (computed upstream
                                via `estimate_duration_split_count`)
        has_audio:              True if user supplied audio files
        match_mode_str:         "random" / "sequential" / "no_repeat" /
                                "shuffle_once"
        product_ping_pong:      TC03/04 default = True
        background_ping_pong:   TC04 default = False (TC03 also False)
        use_split_by_duration:  TC03/04 default = True

    Returns:
        A BatchSettings ready to hand to `render_batch()`.
    """
    match_modes = {
        "random": MatchMode.RANDOM,
        "sequential": MatchMode.SEQUENTIAL,
        "shuffle_once": MatchMode.SHUFFLE_ONCE,
        "no_repeat": MatchMode.NO_REPEAT,
    }
    if not isinstance(match_mode_str, str) or match_mode_str not in match_modes:
        allowed = ", ".join(match_modes)
        raise ValueError(
            f"match_mode must be one of: {allowed}; got {match_mode_str!r}"
        )
    mode = match_modes[match_mode_str]
    validated_segment_duration = validate_tc03_segment_duration(segment_duration)
    return BatchSettings(
        segment_duration=validated_segment_duration,
        num_outputs=num_outputs,
        split_by_duration=use_split_by_duration,
        product_ping_pong=product_ping_pong,
        background_ping_pong=background_ping_pong,
        cover_mode=mode,
        background_mode=mode,
        audio_mode=mode,
        use_uploaded_audio=has_audio,
        use_product_audio=not has_audio,
        # seed is set inside the pipeline (time.time()) because the user
        # may want reproducibility and we don't want to fix it here.
    )


def loop_settings_for(values: Dict[str, Any]) -> "LoopSettings":
    """Build a LoopSettings (TC06) from a values dict.

    Reads the *_FIELDS list in ui/tabs/tc06_video_loop.py and produces a
    validated LoopSettings ready to hand to `core.loop_video.render_loop()`.

    Target duration must be in the inclusive 0.5..600s range. Invalid target
    values are rejected rather than silently clamped. Crossfade must be >= 0
    and is capped at one quarter of the validated target.
    """
    # Local import to avoid a circular reference at module load time.
    from .loop_video import LoopSettings

    target = validate_tc06_target_seconds(
        values.get("target_seconds", TC06_LEGACY_LOOP_DEFAULTS["target_seconds"])
    )
    xfade = float(values.get("crossfade_seconds", TC06_LEGACY_LOOP_DEFAULTS["crossfade_seconds"]))
    xfade = max(0.0, min(xfade, target / 4))  # never let xfade eat more than 1/4

    mode = str(values.get("mode", TC06_LEGACY_LOOP_DEFAULTS["mode"]))
    if mode not in ("loop", "ping_pong"):
        mode = "loop"

    return LoopSettings(
        width=int(values.get("width", TC06_LEGACY_LOOP_DEFAULTS["width"])),
        height=int(values.get("height", TC06_LEGACY_LOOP_DEFAULTS["height"])),
        fps=int(values.get("fps", TC06_LEGACY_LOOP_DEFAULTS["fps"])),
        bitrate=str(values.get("bitrate", TC06_LEGACY_LOOP_DEFAULTS["bitrate"])),
        encoder_alias=str(values.get("encoder", TC06_LEGACY_LOOP_DEFAULTS["encoder"])),
        preset=str(values.get("preset", TC06_LEGACY_LOOP_DEFAULTS["preset"])),
        target_seconds=target,
        mode=mode,
        crossfade_seconds=xfade,
    )


def validate_tc06_allow_clip_reuse(values: Mapping[str, Any]) -> bool:
    value = values.get("allow_clip_reuse", TC06_VIDEO_DEFAULTS["allow_clip_reuse"])
    if not isinstance(value, bool):
        raise TypeError("allow_clip_reuse must be a boolean")
    return value


def pick_compositions(values: Mapping[str, Any], *, all_three_default: bool = True) -> List[str]:
    """Read use_center / use_left / use_right toggles from a values dict.

    Used by TC02, TC04, TC05. Missing toggle keys use ``all_three_default``;
    each explicitly supplied value must be a real ``bool``. Explicit ``False``
    for all three returns an empty list so callers can reject the configuration
    instead of silently rendering center.
    """
    on_default = True if all_three_default else False
    comps: List[str] = []
    for key, composition in (
        ("use_center", "center"),
        ("use_left", "left"),
        ("use_right", "right"),
    ):
        enabled = values[key] if key in values else on_default
        if not isinstance(enabled, bool):
            raise ValueError(f"{key} must be a boolean; got {enabled!r}")
        if enabled:
            comps.append(composition)
    return comps


def reframe_outputs_per_source(
    values: Mapping[str, Any],
    *,
    all_three_default: bool = True,
) -> int:
    """Return the fixed-reframe output count for one source.

    TC02, TC04, and TC05 each apply seven fixed lenses to every selected
    composition. Missing toggle keys retain the three-composition default;
    explicitly disabling all three produces zero so callers can fail input.
    """
    return FIXED_REFRAME_LENS_COUNT * len(
        pick_compositions(values, all_three_default=all_three_default)
    )
