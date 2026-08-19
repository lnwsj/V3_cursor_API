"""
core/pipelines/ — TC01-TC06 render pipelines (pure Python, no tkinter).

Each `tc0N_<name>.py` exposes a `render(...)` function that takes a small
data-only `PipelineInputs` dataclass and a `PipelineCallbacks` dataclass,
and runs the actual ffmpeg work in the caller thread (the UI Worker
already runs us on a daemon thread).

Why split this out from `ui/tabs/`?
  - The pipeline layer can be unit-tested without spinning up tkinter.
  - The UI layer only needs to wire UI widgets to a `render(...)` call;
    no ffmpeg / shuffle / checkpoint logic stays in the tab file.
  - The 800-lines-per-file rule (V3_kuy spec) is much easier to keep when
    UI code and render code are in separate files.

Public surface (re-exported here for convenience):
    - tc01_chroma.render
    - tc02_reframe.render
    - tc03_batch.render
    - tc04_rebatch.render
    - tc05_reframe_only.render
    - tc06_video_loop.render
    - PipelineInputs, PipelineCallbacks, StepCallback  (from ._common)
    - PipelineStatus, StageResult, PipelineResult + finalize helpers
"""
from __future__ import annotations

from ._common import (
    PipelineInputs,
    PipelineCallbacks,
    StepCallback,
    PipelineStatus,
    StageResult,
    PipelineResult,
    finalize_stage_result,
    finalize_pipeline_result,
    combined_stop_check,
    safe_log,
    safe_progress,
    safe_file,
    shuffle_pool,
    apply_seed,
)
from .tc01_chroma import render as render_tc01
from .tc02_reframe import render as render_tc02
from .tc03_batch import render as render_tc03
from .tc04_rebatch import render as render_tc04
from .tc05_reframe_only import render as render_tc05
from .tc06_video_loop import render as render_tc06

__all__ = [
    "PipelineInputs",
    "PipelineCallbacks",
    "StepCallback",
    "PipelineStatus",
    "StageResult",
    "PipelineResult",
    "finalize_stage_result",
    "finalize_pipeline_result",
    "combined_stop_check",
    "safe_log",
    "safe_progress",
    "safe_file",
    "shuffle_pool",
    "apply_seed",
    "render_tc01",
    "render_tc02",
    "render_tc03",
    "render_tc04",
    "render_tc05",
    "render_tc06",
]
