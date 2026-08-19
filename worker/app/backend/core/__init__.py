"""
core/ — V3 active modules.

The V3 Tk UI talks ONLY to these modules. Anything from V2 that still
needs to be referenced (e.g. by the V2 PyInstaller bundle) lives under
`core/_legacy/` and can be imported as `from core._legacy import X`.

Public API surface (re-exported here for convenience):

  Green tab (chroma-key + GPU pipeline)
    - gpu_detector:    effective_video_encoder, is_encoder_ready, gpu_summary,
                       resolve_encoder_alias, DEFAULT_PREFERRED_ORDER
    - ffmpeg_runner:   FfmpegRunner, FfmpegProgress, FfmpegResult
    - green_render:    GreenSettings, render_green, preview_green,
                       build_render_command, build_preview_command
    - batch_pingpong:  BatchSettings, BatchMatch, BatchResult, MatchMode,
                       create_ping_pong_segments, get_ping_pong_pattern,
                       calculate_ping_pong_metrics, build_batch_matches,
                       render_batch, TimeRange, PingPongSegment
    - ai_reframe:      LensPreset, LENS_PRESETS, PLATFORMS, SCENARIOS,
                       Composition, FIXED_7X3_LENS_KEYS, FIXED_7X3_LENSES,
                       ReframeSettings, ReframeTask, ReframeResult,
                       auto_pick_lenses, get_lenses_by_focal_range,
                       build_reframe_tasks, build_reframe_ffmpeg_command,
                       render_reframe_plan, list_video_files

  App infrastructure
    - app_config:      load_app_config, AppConfig, debug_mode_log_enabled,
                       recent-file API (add_recent_file, load_recent_files,
                       clear_recent_files, save_recent_files)
    - preset_store:    save_preset, load_preset, list_presets, delete_preset
    - render_checkpoint: save_checkpoint, load_checkpoint, clear_checkpoint,
                         set_paused, get_paused
"""
from __future__ import annotations

# Green tab (chroma-key + GPU pipeline)
from .gpu_detector import (
    effective_video_encoder,
    is_encoder_ready,
    gpu_summary,
    resolve_encoder_alias,
    DEFAULT_PREFERRED_ORDER,
)
from .ffmpeg_runner import FfmpegRunner, FfmpegProgress, FfmpegResult
from .green_render import (
    GreenSettings,
    render_green,
    preview_green,
    build_render_command,
    build_preview_command,
)
from .batch_pingpong import (
    BatchSettings,
    BatchMatch,
    BatchResult,
    MatchMode,
    create_ping_pong_segments,
    get_ping_pong_pattern,
    calculate_ping_pong_metrics,
    build_batch_matches,
    render_batch,
    TimeRange,
    PingPongSegment,
)
from .ai_reframe import (
    LensPreset,
    LENS_PRESETS,
    PLATFORMS,
    SCENARIOS,
    Composition,
    FIXED_7X3_LENS_KEYS,
    FIXED_7X3_LENSES,
    ReframeSettings,
    ReframeTask,
    ReframeResult,
    auto_pick_lenses,
    get_lenses_by_focal_range,
    build_reframe_tasks,
    build_reframe_ffmpeg_command,
    render_reframe_plan,
    list_video_files,
)

# App infrastructure
from .app_config import (
    load_app_config,
    AppConfig,
    config_path,
    debug_mode_log_enabled,
    add_recent_file,
    load_recent_files,
    save_recent_files,
    clear_recent_files,
    RECENT_FILES_MAX,
)
from .preset_store import (
    save_preset,
    load_preset,
    list_presets,
    delete_preset,
    PresetInfo,
)
from .render_checkpoint import (
    save_checkpoint,
    load_checkpoint,
    clear_checkpoint,
    set_paused,
    is_paused,
    get_checkpoint_info,
)