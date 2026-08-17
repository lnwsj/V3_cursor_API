"""
core/pipelines/tc05_reframe_only.py — TC05 Direct Reframe Only pipeline.

Pure Python. No tkinter imports.

Contract (mirrors ui/tabs/reframe_only_direct.py::_build_render_target):

    inputs.sources[i]   -> 7 lens x 3 compositions (21 outputs/source)
    max_parallel_ffmpeg = clamp(ffmpeg_workers, 1, 3)

Output naming: `{source_basename}_lens{LL}mm_{comp}.mp4`
                (set inside core.ai_reframe.build_reframe_tasks)

The "3 compositions" toggle from values is honored as well (default = all 3).
If user disables center/left/right, only the enabled ones render.

Runs ffmpeg via core.ai_reframe.render_reframe_plan.
"""
from __future__ import annotations

import os

from ..ai_reframe import (
    ReframeSettings,
    render_reframe_plan,
)
from ..gpu_detector import gpu_summary
from ..contract import (
    clamp_tc05_workers,
    pick_compositions,
    reframe_outputs_per_source,
    reframe_settings_for,
    TC05_VIDEO_DEFAULTS,
)

from ._common import (
    PipelineInputs,
    PipelineCallbacks,
    PipelineResult,
    StageResult,
    combined_stop_check,
    finalize_pipeline_result,
    finalize_stage_result,
    safe_file,
    safe_log,
    safe_progress,
)



def _pick_compositions(values: dict) -> list:
    """TC05 default = all 3 (center/left/right)."""
    return pick_compositions(values, all_three_default=True)


def _build_reframe_settings(values: dict, compositions: list) -> ReframeSettings:
    """Build TC05 settings with the requested worker count clamped to 1..3."""
    return reframe_settings_for(
        values,
        compositions=compositions,
        defaults=TC05_VIDEO_DEFAULTS,
        max_parallel_override=clamp_tc05_workers(values),
    )


def _callback_requested(callback) -> bool:
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
        return False


def _terminal_flags(cb: PipelineCallbacks) -> tuple[bool, bool]:
    """Read pause first so PAUSED wins when pause and cancel coexist."""
    paused = _callback_requested(cb.pause_check)
    cancel_requested = _callback_requested(cb.stop_check)
    return paused, cancel_requested


def _valid_output(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except (OSError, TypeError, ValueError):
        return False


def _finalize_result(
    *,
    expected: int,
    succeeded: int = 0,
    failed: int = 0,
    cancelled: int = 0,
    outputs: list[str] | None = None,
    errors: list[str] | None = None,
    paused: bool = False,
    cancel_requested: bool = False,
    invalid_input: bool = False,
    metadata: dict | None = None,
) -> PipelineResult:
    output_paths = list(outputs or [])
    error_messages = list(errors or [])
    stage = finalize_stage_result(
        StageResult(
            name="reframe",
            expected=expected,
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            outputs=output_paths,
            errors=error_messages,
            metadata=dict(metadata or {}),
            required=True,
        ),
        paused=paused,
        cancel_requested=cancel_requested,
        invalid_input=invalid_input,
    )
    return finalize_pipeline_result(
        PipelineResult(
            pipeline="TC05",
            expected=expected,
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            outputs=output_paths,
            errors=error_messages,
            stages=[stage],
            metadata=dict(metadata or {}),
        ),
        paused=paused,
        cancel_requested=cancel_requested,
        invalid_input=invalid_input,
    )


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    """Run TC05 direct reframe and return a truth-bearing final verdict."""
    sources = list(inputs.sources)
    out_dir = inputs.output_dir
    compositions = _pick_compositions(inputs.values)
    expected = len(sources) * reframe_outputs_per_source(inputs.values)
    requested_workers = inputs.values.get(
        "ffmpeg_workers",
        inputs.values.get("max_parallel", 3),
    )
    workers = clamp_tc05_workers(inputs.values)
    metadata = {
        "workers": workers,  # compatibility alias
        "workers_requested": requested_workers,
        "workers_effective": workers,
        "compositions": list(compositions),
    }

    paused, cancel_requested = _terminal_flags(cb)
    if not sources:
        error = "TC05 requires at least one source"
        safe_log(cb.log_fn, f"[direct-reframe] invalid_input={error}")
        return _finalize_result(
            expected=0,
            errors=[error],
            paused=paused,
            cancel_requested=cancel_requested,
            invalid_input=True,
            metadata=metadata,
        )
    if not compositions:
        error = "TC05 requires at least one enabled composition"
        safe_log(cb.log_fn, f"[direct-reframe] invalid_input={error}")
        return _finalize_result(
            expected=0,
            errors=[error],
            paused=paused,
            cancel_requested=cancel_requested,
            invalid_input=True,
            metadata=metadata,
        )

    try:
        settings = _build_reframe_settings(inputs.values, compositions)
    except Exception as exc:
        error = f"TC05 settings validation failed: {exc}"
        safe_log(cb.log_fn, f"[direct-reframe] invalid_input={error}")
        return _finalize_result(
            expected=expected,
            failed=expected,
            errors=[error],
            paused=paused,
            cancel_requested=cancel_requested,
            invalid_input=True,
            metadata=metadata,
        )

    try:
        gpu = gpu_summary()
    except Exception:
        gpu = {}

    safe_log(
        cb.log_fn,
        f"[direct-reframe] sources={len(sources)} expected_outputs={expected} "
        f"workers.requested={requested_workers!r} "
        f"workers.effective={settings.max_parallel_ffmpeg} "
        f"encoder={settings.encoder_alias} "
        f"nvenc_ready={gpu.get('nvenc_ready')}",
    )

    if paused or cancel_requested:
        return _finalize_result(
            expected=expected,
            cancelled=expected,
            paused=paused,
            cancel_requested=cancel_requested,
            metadata=metadata,
        )

    stop = combined_stop_check(cb.stop_check, cb.pause_check)
    last_source = [""]

    def _on_progress(c: int, t: int, task_obj) -> None:
        src = task_obj.source_path
        if src != last_source[0]:
            last_source[0] = src
            safe_file(cb.file_fn, src)
        safe_progress(
            cb.progress_fn,
            c * 100.0 / max(t, 1),
            f"Task {c}/{t}: {task_obj.lens.key}/{task_obj.composition.value}",
        )

    errors: list[str] = []
    try:
        os.makedirs(out_dir, exist_ok=True)
        raw_results = render_reframe_plan(
            sources=sources,
            out_dir=out_dir,
            settings=settings,
            on_log=cb.log_fn,
            on_progress=_on_progress,
            stop_check=stop,
            tc_label="TC05",
        )
        results = list(raw_results or [])
    except Exception as exc:
        results = []
        errors.append(f"render_reframe_plan exception: {exc}")

    succeeded = 0
    failed = 0
    outputs: list[str] = []

    for index, item in enumerate(results[:expected], start=1):
        success = bool(getattr(item, "success", False))
        task = getattr(item, "task", None)
        raw_path = getattr(task, "output_path", "")
        try:
            output_path = os.fspath(raw_path) if raw_path else ""
        except TypeError:
            output_path = ""

        if success and output_path and _valid_output(output_path):
            succeeded += 1
            outputs.append(output_path)
            continue

        failed += 1
        engine_error = str(getattr(item, "error", "") or "failed")
        if success:
            engine_error = "engine reported success but output is missing or zero-byte"
        errors.append(f"reframe task {index} failed: {engine_error}")

    paused_now, cancel_now = _terminal_flags(cb)
    paused = paused or paused_now
    cancel_requested = cancel_requested or cancel_now
    remaining = max(0, expected - min(len(results), expected))
    cancelled = remaining if (paused or cancel_requested) else 0
    if not (paused or cancel_requested):
        failed += remaining
        if remaining:
            errors.append(
                f"render_reframe_plan returned {len(results)} of {expected} results"
            )

    if not results and not (paused or cancel_requested) and not errors:
        errors.append("render_reframe_plan returned no results")

    if len(results) > expected:
        errors.append(
            f"render_reframe_plan returned too many results: {len(results)} > {expected}"
        )
        if succeeded > 0:
            succeeded -= 1
            failed += 1
            outputs.pop()
        elif failed < expected:
            failed += 1

    safe_log(
        cb.log_fn,
        f"[direct-reframe] done status_counts ok={succeeded} failed={failed} "
        f"cancelled={cancelled} expected={expected}",
    )
    return _finalize_result(
        expected=expected,
        succeeded=succeeded,
        failed=failed,
        cancelled=cancelled,
        outputs=outputs,
        errors=errors,
        paused=paused,
        cancel_requested=cancel_requested,
        metadata=metadata,
    )
