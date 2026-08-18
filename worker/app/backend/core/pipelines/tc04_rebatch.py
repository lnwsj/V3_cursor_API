"""
core/pipelines/tc04_rebatch.py - TC04 Reframe -> Batch -> final chroma.

Pure Python. No tkinter imports.

Required stage order:
    1. Reframe every product to 7 lenses x enabled compositions.
    2. Split only the complete, validated reframe set and chroma-key it.

The batch stage is skipped unless the required reframe stage is complete and
all intermediate files exist and are non-zero.
"""
from __future__ import annotations

import os
import concurrent.futures
from datetime import datetime

# v3.PARALLEL (2026-08-18): TC04 chroma stage parallel ffmpegs.
# Default 1 (sequential) preserved for backward compat; env V3_TC04_PARALLEL overrides.
# WARNING: parallel=3 is SLOWER on RTX 2050 (CPU contention — x264 doesn't scale well
# below ~4 threads). Keep at 1 unless CPU count ≥ 16.
_TC04_PARALLEL = max(1, int(os.environ.get("V3_TC04_PARALLEL", "1") or "1"))
from typing import Any, Callable, Dict, Iterable, List, Tuple

from ..ai_reframe import ReframeSettings, render_reframe_plan
from ..batch_pingpong import (
    BatchSettings,
    estimate_duration_split_count,
    render_batch,
    snapshot_product_durations,
    unrenderable_products,
)
from ..green_render import GreenSettings
from ..gpu_detector import gpu_summary
from ..media_probe import (
    MediaProbeCancelled,
    has_video_stream as _bg_has_stream,
    invalid_audio_stream_paths,
    invalid_video_stream_paths,
)
from ..contract import (
    TC03_SEGMENT_DURATION_DEFAULT,
    TC04_BATCH_DEFAULTS,
    TC04_REFRAME_DEFAULTS,
    TC04_VIDEO_DEFAULTS,
    batch_settings_for,
    green_settings_for,
    pick_compositions,
    reframe_outputs_per_source,
    reframe_settings_for,
    validate_tc03_segment_duration,
)
from ._common import (
    PipelineCallbacks,
    PipelineInputs,
    PipelineResult,
    StageResult,
    finalize_pipeline_result,
    finalize_stage_result,
    invalid_video_inputs,
    overlapping_role_paths,
    resolve_run_seed,
    safe_file,
    safe_log,
    safe_progress,
)


def _pick_compositions(values: dict) -> list:
    """Read the three composition toggles; all enabled by default."""
    return pick_compositions(values, all_three_default=True)


def _build_reframe_settings(values: dict, compositions: list) -> ReframeSettings:
    return reframe_settings_for(
        values,
        compositions=compositions,
        defaults=TC04_VIDEO_DEFAULTS,
        max_parallel_override=values.get(
            "max_parallel", TC04_REFRAME_DEFAULTS["max_parallel"]
        ),
    )


def _build_green_settings(
    values: dict,
    has_audio: bool,
    has_cover: bool = False,
) -> GreenSettings:
    """Build final chroma settings and retain TC04's product-audio policy."""
    settings = green_settings_for(
        values,
        has_audio=has_audio,
        has_cover=has_cover,
        defaults=TC04_VIDEO_DEFAULTS,
    )
    return GreenSettings(
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
        bitrate=settings.bitrate,
        encoder_alias=settings.encoder_alias,
        key_color=settings.key_color,
        similarity=settings.similarity,
        blend=settings.blend,
        despill=settings.despill,
        despill_screen=settings.despill_screen,
        cover_enabled=settings.cover_enabled,
        cover_duration=settings.cover_duration,
        cover_scale=settings.cover_scale,
        audio_source="background" if has_audio else "product",
        preset=settings.preset,
    )


def _build_batch_settings(
    values: dict,
    segment_duration: float,
    num_outputs: int,
    has_audio: bool,
    seed: int,
) -> BatchSettings:
    match_mode = values.get("match_mode", TC04_BATCH_DEFAULTS["match_mode"])
    settings = batch_settings_for(
        segment_duration=segment_duration,
        num_outputs=num_outputs,
        has_audio=has_audio,
        match_mode_str=match_mode,
    )
    return BatchSettings(
        segment_duration=settings.segment_duration,
        num_outputs=num_outputs,
        split_by_duration=settings.split_by_duration,
        product_ping_pong=False,
        background_ping_pong=settings.background_ping_pong,
        cover_mode=settings.cover_mode,
        background_mode=settings.background_mode,
        audio_mode=settings.audio_mode,
        use_uploaded_audio=settings.use_uploaded_audio,
        use_product_audio=settings.use_product_audio,
        uploaded_audio_controls_duration=has_audio,
        seed=seed,
    )


def _safe_requested(callback: Any) -> bool:
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
        return False


def _tracked_stop(cb: PipelineCallbacks) -> Tuple[Any, Dict[str, bool]]:
    state = {"paused": False, "cancelled": False}

    def stop() -> bool:
        paused = _safe_requested(cb.pause_check)
        cancelled = _safe_requested(cb.stop_check)
        state["paused"] = state["paused"] or paused
        state["cancelled"] = state["cancelled"] or cancelled
        return paused or cancelled

    return stop, state


def _terminal_flags(
    cb: PipelineCallbacks,
    state: Dict[str, bool],
) -> Tuple[bool, bool]:
    paused = bool(state["paused"]) or _safe_requested(cb.pause_check)
    return paused, bool(state["cancelled"])


def _safe_step(cb: PipelineCallbacks, name: str, text: str) -> None:
    if cb.step_fn is None:
        return
    try:
        cb.step_fn(name, text)
    except Exception:
        pass


def _summarize_engine_results(
    engine_results: Iterable[Any],
    *,
    expected: int,
    stage_name: str,
    path_for: Callable[[Any], Any],
    paused: bool,
    cancel_requested: bool,
    engine_error: str = "",
) -> Dict[str, Any]:
    results = list(engine_results or [])
    outputs: List[str] = []
    errors: List[str] = []
    succeeded = 0
    failed = 0
    cancelled = 0

    for index, item in enumerate(results, 1):
        if bool(getattr(item, "success", False)):
            # FIX (B-05, 2026-07-31): if we cannot resolve a path, treat as
            # failure not success. Otherwise the pipeline records a
            # successful output whose path is "", which then trips the
            # invariant check in PipelineResult._finalize_common (the empty
            # string is not a valid file on disk, so valid_count goes down).
            try:
                resolved_path = os.fspath(path_for(item))
            except (AttributeError, TypeError) as exc:
                failed += 1
                errors.append(
                    f"{stage_name} output {index} reported success but "
                    f"path is not fspath-compatible: {exc}"
                )
                continue
            if not resolved_path:
                failed += 1
                errors.append(
                    f"{stage_name} output {index} reported success but path is empty"
                )
                continue
            succeeded += 1
            outputs.append(resolved_path)
        elif bool(getattr(item, "cancelled", False)):
            cancelled += 1
        else:
            failed += 1
            detail = str(getattr(item, "error", "") or "unknown engine failure")
            # FIX (B-26, 2026-07-31): include product path / segment index
            match = getattr(item, "match", None)
            match_detail = ""
            if match is not None:
                match_detail = (
                    f" product={os.path.basename(match.product_path)} "
                    f"seg={match.segment.segment_index}"
                )
            errors.append(f"{stage_name} output {index} failed:{match_detail} {detail}")

    returned = len(results)
    missing = max(expected - returned, 0)
    if missing:
        if paused or cancel_requested:
            cancelled += missing
        else:
            failed += missing
            errors.append(
                f"{stage_name} engine incomplete: returned={returned} expected={expected}"
            )
    if returned == 0:
        errors.append(f"{stage_name} engine returned no results")
    if returned > expected:
        errors.append(f"{stage_name} engine returned too many results: {returned}>{expected}")
        # FIX (B-06, 2026-07-31): normalise counts when engine emits more
        # than expected. See tc03_batch.py for the rationale — decrement
        # failed first (phantom over-emits), then cancelled, then succeeded
        # last (preserve valid output files).
        surplus = returned - expected
        for _ in range(surplus):
            if failed > 0:
                failed -= 1
            elif cancelled > 0:
                cancelled -= 1
            elif succeeded > 0:
                succeeded -= 1
            else:
                break
        if len(outputs) > expected:
            outputs = outputs[:expected]
    if engine_error:
        errors.append(engine_error)

    return {
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": cancelled,
        "outputs": outputs,
        "errors": errors,
        "returned": returned,
    }


def _invalid_result(message: str, raw_duration: Any = None) -> PipelineResult:
    return finalize_pipeline_result(
        PipelineResult(
            pipeline="TC04",
            expected=0,
            errors=[message],
            metadata={"segment_duration_raw": raw_duration},
        ),
        invalid_input=True,
    )


def _skip_batch_result(
    *,
    reframe_stage: StageResult,
    expected_final: int,
    message: str,
    paused: bool,
    cancel_requested: bool,
    metadata: Dict[str, Any],
) -> PipelineResult:
    interrupted = paused or cancel_requested
    downstream_cancelled = expected_final if interrupted else 0
    downstream_skipped = 0 if interrupted else expected_final
    batch_stage = finalize_stage_result(
        StageResult(
            name="batch_chroma",
            expected=expected_final,
            skipped=downstream_skipped,
            cancelled=downstream_cancelled,
            errors=[message],
            metadata={"engine_called": False},
        ),
        paused=paused,
        cancel_requested=cancel_requested,
    )
    reframe_outputs: List[Any] = []
    reframe_succeeded = 0
    if reframe_stage and reframe_stage.valid_output_count > 0:
        reframe_outputs = list(reframe_stage.outputs)[:reframe_stage.valid_output_count]
        reframe_succeeded = len(reframe_outputs)
    return finalize_pipeline_result(
        PipelineResult(
            pipeline="TC04",
            expected=expected_final + reframe_succeeded,
            succeeded=reframe_succeeded,
            skipped=downstream_skipped,
            cancelled=downstream_cancelled,
            outputs=reframe_outputs,
            stages=[reframe_stage, batch_stage],
            errors=[message],
            metadata={**metadata, "reframe_outputs_surfaced": reframe_succeeded},
        ),
        paused=paused,
        cancel_requested=cancel_requested,
    )


def _replace_unreadable_backgrounds(
    backgrounds: List[str],
    ffprobe_cmd: str,
    cb: PipelineCallbacks,
) -> List[str]:
    """FIX (V1.0.2.x): probe each background. If unreadable, replace
    with the first known-good background from the pool. Logs each
    replacement so the operator sees what happened."""
    if not backgrounds:
        return backgrounds

    valid: List[str] = []
    result: List[str] = []
    replaced_count = 0
    for bg in backgrounds:
        try:
            if _bg_has_stream(bg, ffprobe_cmd=ffprobe_cmd):
                result.append(bg)
                if bg not in valid:
                    valid.append(bg)
            else:
                if valid:
                    result.append(valid[0])
                    replaced_count += 1
                    safe_log(
                        cb.log_fn,
                        f"[bg-replace] {os.path.basename(bg)} unreadable; "
                        f"replaced with {os.path.basename(valid[0])}",
                    )
                else:
                    result.append(bg)
        except Exception:
            result.append(bg)
    if replaced_count > 0:
        safe_log(
            cb.log_fn,
            f"[bg-replace] {replaced_count} unreadable background(s) replaced",
        )
    return result

def _probe_outputs_valid(
    output_paths,
    ffprobe_cmd,
    on_log,
) -> set:
    """FIX (V1.0.2.14, G3): probe each output path. If it exists and has
    a readable video stream with size > 0, add to the pre-validated
    set. Returns the set of absolute paths that are already valid.
    Used by TC04 to skip-existing-output when only a few outputs are
    missing from a previous interrupted run.
    """
    try:
        from core.media_probe import has_video_stream
    except Exception:
        return set()
    valid = set()
    for raw in output_paths:
        path = os.fspath(raw)
        try:
            if os.path.getsize(path) <= 0:
                continue
            if not has_video_stream(path, ffprobe_cmd=ffprobe_cmd):
                continue
            valid.add(path)
        except Exception:
            continue
    if on_log and valid:
        on_log(f"[bg-probe] {len(valid)} existing output(s) are valid; skip-existing-output will reuse them")
    return valid


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    """Run TC04's two required stages and return a fail-closed result."""
    products = list(inputs.products)
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    covers = list(inputs.covers)
    out_dir = inputs.output_dir
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stop, terminal_state = _tracked_stop(cb)

    if not products:
        result = _invalid_result("TC04 requires at least one product")
        safe_log(cb.log_fn, result.all_errors[0])
        return result
    if not backgrounds:
        result = _invalid_result("TC04 requires at least one background")
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    role_overlap = overlapping_role_paths(products, backgrounds)
    if role_overlap:
        result = _invalid_result(
            "TC04 Product and Background must be different files: "
            + role_overlap[0]
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        compositions = _pick_compositions(inputs.values)
    except (TypeError, ValueError) as exc:
        result = _invalid_result(f"TC04 settings error: {exc}")
        safe_log(cb.log_fn, result.all_errors[0])
        return result
    if not compositions:
        result = _invalid_result("TC04 requires at least one enabled composition")
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    invalid_products = invalid_video_inputs(products)
    if invalid_products:
        result = _invalid_result(f"TC04 Product must be a video: {invalid_products[0]}")
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    raw_duration = inputs.values.get(
        "segment_duration",
        TC04_BATCH_DEFAULTS.get(
            "segment_duration",
            TC03_SEGMENT_DURATION_DEFAULT,
        ),
    )
    outputs_per_source = reframe_outputs_per_source(inputs.values)
    expected_reframe = len(products) * outputs_per_source

    try:
        # Reject output settings before stream/duration probes or stage-1 work.
        effective_seed = resolve_run_seed(inputs.values.get("seed", 0))
        segment_duration = validate_tc03_segment_duration(raw_duration)
        reframe_settings = _build_reframe_settings(inputs.values, compositions)
        green_settings = _build_green_settings(
            inputs.values,
            has_audio=bool(audios),
            has_cover=bool(covers),
        )
    except Exception as exc:
        result = _invalid_result(f"TC04 settings error: {exc}", raw_duration)
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        for label, paths in (
            ("Product", products),
            ("Background", backgrounds),
            ("Cover", covers[:1]),
        ):
            invalid_streams = invalid_video_stream_paths(
                paths,
                stop_check=stop,
            )
            if invalid_streams:
                result = _invalid_result(
                    f"TC04 {label} has no readable video stream: {invalid_streams[0]}"
                )
                safe_log(cb.log_fn, result.all_errors[0])
                return result

        invalid_audio = invalid_audio_stream_paths(
            audios,
            stop_check=stop,
        )
        if invalid_audio:
            result = _invalid_result(
                f"TC04 Audio has no readable audio stream: {invalid_audio[0]}"
            )
            safe_log(cb.log_fn, result.all_errors[0])
            return result
    except MediaProbeCancelled:
        paused, cancel_requested = _terminal_flags(cb, terminal_state)
        safe_log(cb.log_fn, "TC04 media preflight interrupted")
        return finalize_pipeline_result(
            PipelineResult(
                pipeline="TC04",
                expected=1,
                cancelled=1,
                metadata={
                    "cancelled_during": "media_preflight",
                    "expected_count_basis": "preflight_operation",
                },
            ),
            paused=paused,
            cancel_requested=cancel_requested or not paused,
        )

    try:
        product_duration_snapshot = snapshot_product_durations(
            products,
            stop_check=stop,
        )
    except MediaProbeCancelled:
        paused, cancel_requested = _terminal_flags(cb, terminal_state)
        safe_log(cb.log_fn, "TC04 source-duration planning cancelled")
        return finalize_pipeline_result(
            PipelineResult(
                pipeline="TC04",
                expected=1,
                cancelled=1,
                metadata={
                    "cancelled_during": "source_duration_planning",
                    "expected_count_basis": "planning_operation",
                },
            ),
            paused=paused,
            cancel_requested=cancel_requested or not paused,
        )

    unreadable_products = unrenderable_products(
        products,
        duration_snapshot=product_duration_snapshot,
    )
    if unreadable_products:
        result = _invalid_result(
            f"TC04 Product has no readable duration: {unreadable_products[0]}"
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        source_segments = estimate_duration_split_count(
            products,
            segment_duration,
            duration_snapshot=product_duration_snapshot,
        )
    except Exception as exc:
        result = _invalid_result(
            f"unable to plan TC04 segments: {exc}", raw_duration
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result
    expected_final_before_reframe = source_segments * outputs_per_source
    if source_segments <= 0 or expected_final_before_reframe <= 0:
        result = _invalid_result(
            "TC04 products produced no renderable segments", raw_duration
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        batch_settings = _build_batch_settings(
            inputs.values,
            segment_duration,
            expected_final_before_reframe,
            has_audio=bool(audios),
            seed=effective_seed,
        )
    except Exception as exc:
        result = _invalid_result(f"TC04 settings error: {exc}", raw_duration)
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    reframe_dir = os.path.join(out_dir, "reframe")
    try:
        os.makedirs(reframe_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        message = f"TC04 setup failed: {exc}"
        reframe_stage = finalize_stage_result(
            StageResult(
                name="reframe",
                expected=expected_reframe,
                failed=expected_reframe,
                errors=[message],
            )
        )
        paused, cancel_requested = _terminal_flags(
            cb,
            {"paused": False, "cancelled": False},
        )
        return _skip_batch_result(
            reframe_stage=reframe_stage,
            expected_final=expected_final_before_reframe,
            message="batch_chroma skipped because TC04 setup failed",
            paused=paused,
            cancel_requested=cancel_requested,
            metadata={"segment_duration": segment_duration},
        )

    try:
        gpu = gpu_summary()
    except Exception:
        gpu = {}
    safe_log(
        cb.log_fn,
        f"rebatch->chroma {len(products)} products -> {expected_reframe} reframe -> "
        f"{expected_final_before_reframe} planned final "
        f"(segment={segment_duration:g}s cover={bool(covers)})",
    )
    safe_log(cb.log_fn, f"[gpu] {gpu}")
    safe_log(cb.log_fn, f"[rebatch] effective_seed={effective_seed}")

    # Stage 1: reframe.
    safe_log(cb.log_fn, f"[step 1] reframe: expected={expected_reframe}")
    _safe_step(cb, "step1", "Reframe inputs")
    last_source = [""]

    def on_reframe_progress(current: int, total: int, task: Any) -> None:
        source = task.source_path
        if source != last_source[0]:
            last_source[0] = source
            safe_file(cb.file_fn, f"step1: {os.path.basename(source)}")
        safe_progress(
            cb.progress_fn,
            45 * current / max(total, 1),
            f"Step 1: {current}/{total} ({task.lens.key}/{task.composition.value})",
        )

    # FIX (V1.0.2.14, G3): probe existing outputs so we can skip render
    # for files that are already valid on disk. This recovers a 30-min
    # interrupted run without re-encoding finished outputs.
    reframe_pre_validated = set()
    batch_pre_validated = set()
    try:
        ref = getattr(inputs, "ffprobe_cmd", "ffprobe")
        # reframe outputs land in reframe_dir; batch outputs in out_dir.
        # Probe every .mp4 in both dirs and validate it.
        if reframe_dir and os.path.isdir(reframe_dir):
            for raw in os.listdir(reframe_dir):
                if raw.endswith(".mp4"):
                    reframe_pre_validated.add(
                        os.fspath(os.path.join(reframe_dir, raw))
                    )
        if out_dir and os.path.isdir(out_dir):
            for raw in os.listdir(out_dir):
                if raw.startswith("batch_") and raw.endswith(".mp4"):
                    batch_pre_validated.add(
                        os.fspath(os.path.join(out_dir, raw))
                    )
        reframe_pre_validated = _probe_outputs_valid(
            reframe_pre_validated, ref, cb.log_fn
        )
        batch_pre_validated = _probe_outputs_valid(
            batch_pre_validated, ref, cb.log_fn
        )
    except Exception as exc:
        safe_log(cb.log_fn, f"[bg-probe] failed: {exc}; falling back to no skip-existing-output")
        reframe_pre_validated = set()
        batch_pre_validated = set()

    reframe_error = ""
    try:
        reframe_results = render_reframe_plan(
            sources=products,
            out_dir=reframe_dir,
            settings=reframe_settings,
            on_log=cb.log_fn,
            on_progress=on_reframe_progress,
            stop_check=stop,
            tc_label="TC04",
            pre_validated_outputs=reframe_pre_validated,
        )
    except Exception as exc:
        reframe_results = []
        reframe_error = f"reframe engine exception: {exc}"
        safe_log(cb.log_fn, reframe_error)

    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    reframe_outcome = _summarize_engine_results(
        reframe_results,
        expected=expected_reframe,
        stage_name="reframe",
        path_for=lambda item: item.task.output_path,
        paused=paused,
        cancel_requested=cancel_requested,
        engine_error=reframe_error,
    )
    reframe_stage = finalize_stage_result(
        StageResult(
            name="reframe",
            expected=expected_reframe,
            succeeded=reframe_outcome["succeeded"],
            failed=reframe_outcome["failed"],
            cancelled=reframe_outcome["cancelled"],
            outputs=reframe_outcome["outputs"],
            errors=reframe_outcome["errors"],
            metadata={"engine_returned": reframe_outcome["returned"]},
        ),
        paused=paused,
        cancel_requested=cancel_requested,
    )
    safe_log(
        cb.log_fn,
        f"[step 1] status={reframe_stage.status.value} "
        f"valid={reframe_stage.valid_output_count}/{expected_reframe}",
    )

    if not reframe_stage.is_success:
        message = "batch_chroma skipped because reframe stage was incomplete"
        safe_log(cb.log_fn, message)
        _safe_step(cb, "step1", "Reframe incomplete")
        _safe_step(cb, "step2", "Batch plan skipped")
        _safe_step(cb, "step3", "Chroma skipped")
        return _skip_batch_result(
            reframe_stage=reframe_stage,
            expected_final=expected_final_before_reframe,
            message=message,
            paused=paused,
            cancel_requested=cancel_requested,
            metadata={
                "segment_duration": segment_duration,
                "cover_enabled": bool(covers),
                "reframe_expected": expected_reframe,
                "reframe_returned": reframe_outcome["returned"],
            },
        )

    reframe_sources = list(reframe_outcome["outputs"])
    _safe_step(cb, "step1_count", f"{len(reframe_sources)} reframe clips")
    _safe_step(cb, "step1", "Reframe complete")

    # Logical UI Stage 2: snapshot and validate the complete batch split plan.
    _safe_step(cb, "step2", "Planning batch split")
    try:
        reframe_duration_snapshot = snapshot_product_durations(
            reframe_sources,
            stop_check=stop,
        )
    except MediaProbeCancelled:
        paused, cancel_requested = _terminal_flags(cb, terminal_state)
        safe_log(cb.log_fn, "TC04 reframe-duration planning cancelled")
        _safe_step(cb, "step2", "Batch plan cancelled")
        _safe_step(cb, "step3", "Chroma skipped")
        downstream_cancelled = expected_final_before_reframe
        batch_stage = finalize_stage_result(
            StageResult(
                name="batch_chroma",
                expected=downstream_cancelled,
                cancelled=downstream_cancelled,
                metadata={
                    "engine_called": False,
                    "cancelled_during": "reframe_duration_planning",
                },
            ),
            paused=paused,
            cancel_requested=cancel_requested or not paused,
        )
        return finalize_pipeline_result(
            PipelineResult(
                pipeline="TC04",
                expected=downstream_cancelled,
                cancelled=downstream_cancelled,
                stages=[reframe_stage, batch_stage],
                metadata={
                    "segment_duration": segment_duration,
                    "cover_enabled": bool(covers),
                    "cancelled_during": "reframe_duration_planning",
                },
            ),
            paused=paused,
            cancel_requested=cancel_requested or not paused,
        )

    try:
        batch_expected = estimate_duration_split_count(
            reframe_sources,
            segment_duration,
            duration_snapshot=reframe_duration_snapshot,
        )
    except Exception as exc:
        batch_expected = 0
        batch_plan_error = f"unable to plan batch_chroma stage: {exc}"
    else:
        batch_plan_error = ""
    if batch_expected <= 0:
        expected_failure_count = expected_final_before_reframe
        message = batch_plan_error or "batch_chroma produced no renderable plan"
        batch_stage = finalize_stage_result(
            StageResult(
                name="batch_chroma",
                expected=expected_failure_count,
                failed=expected_failure_count,
                errors=[message],
                metadata={"engine_called": False},
            )
        )
        result = finalize_pipeline_result(
            PipelineResult(
                pipeline="TC04",
                expected=expected_failure_count,
                failed=expected_failure_count,
                stages=[reframe_stage, batch_stage],
                errors=[message],
                metadata={
                    "segment_duration": segment_duration,
                    "cover_enabled": bool(covers),
                },
            )
        )
        safe_log(cb.log_fn, message)
        _safe_step(cb, "step2", "Batch plan failed")
        _safe_step(cb, "step3", "Chroma skipped")
        return result

    if batch_expected != expected_final_before_reframe:
        message = (
            "TC04 batch plan drift: "
            f"planned={expected_final_before_reframe} recomputed={batch_expected}; "
            "batch_chroma skipped"
        )
        safe_log(cb.log_fn, message)
        _safe_step(cb, "step2", "Batch plan drift")
        _safe_step(cb, "step3", "Chroma skipped")
        return _skip_batch_result(
            reframe_stage=reframe_stage,
            expected_final=expected_final_before_reframe,
            message=message,
            paused=paused,
            cancel_requested=cancel_requested,
            metadata={
                "segment_duration": segment_duration,
                "cover_enabled": bool(covers),
                "reframe_expected": expected_reframe,
                "reframe_returned": reframe_outcome["returned"],
                "planned_final_outputs": expected_final_before_reframe,
                "recomputed_final_outputs": batch_expected,
                "plan_consistent": False,
            },
        )

    # The pre-reframe count is the TC04 contract. The post-reframe probe is
    # only an integrity check; it must never silently redefine the workload.
    # FIX (B-22, 2026-07-31): batch_settings.num_outputs is already set to
    # expected_final_before_reframe by _build_batch_settings() at line 487;
    # do not mutate the dataclass field after construction. The previous
    # assignment was a no-op (same value) but a code smell.
    planned_final_outputs = expected_final_before_reframe
    safe_log(
        cb.log_fn,
        f"[step 3] batch split -> chroma render: expected={planned_final_outputs} "
        f"segment={segment_duration:g}s",
    )
    _safe_step(cb, "step3", "Rendering final chroma")

    def on_batch_match(match: Any) -> None:
        if match.product_path != last_source[0]:
            last_source[0] = match.product_path
            safe_file(cb.file_fn, f"step3: {os.path.basename(match.product_path)}")

    def on_batch_progress(current: int, total: int, progress: Any) -> None:
        pct = getattr(progress, "pct", 0)
        safe_progress(
            cb.progress_fn,
            45 + 55 * (((current - 1) + pct / 100.0) / max(total, 1)),
            f"Step 3: {current}/{total} ({pct:.0f}%)",
        )

    batch_error = ""
    try:
        batch_results = render_batch(
            products=reframe_sources,
            backgrounds=backgrounds,
            audios=audios,
            covers=covers,
            out_dir=out_dir,
            base_settings=green_settings,
            duration_snapshot=reframe_duration_snapshot,
            batch_settings=batch_settings,
            on_log=cb.log_fn,
            on_match=on_batch_match,
            on_progress=on_batch_progress,
            stop_check=stop,
            tc_label="TC04",
            # render_batch is currently sequential; keep the budget at one
            # until a real concurrent batch executor is implemented.
            chroma_max_parallel=1,
            run_stamp=run_stamp,
            pre_validated_outputs=batch_pre_validated,
        )
    except Exception as exc:
        batch_results = []
        batch_error = f"batch_chroma engine exception: {exc}"
        safe_log(cb.log_fn, batch_error)

    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    batch_outcome = _summarize_engine_results(
        batch_results,
        expected=planned_final_outputs,
        stage_name="batch_chroma",
        path_for=lambda item: item.output_path,
        paused=paused,
        cancel_requested=cancel_requested,
        engine_error=batch_error,
    )
    completed_all = (
        batch_outcome["succeeded"] == planned_final_outputs
        and batch_outcome["failed"] == 0
        and batch_outcome["cancelled"] == 0
        and batch_outcome["returned"] == planned_final_outputs
    )
    if completed_all:
        cancel_requested = False
    batch_stage = finalize_stage_result(
        StageResult(
            name="batch_chroma",
            expected=planned_final_outputs,
            succeeded=batch_outcome["succeeded"],
            failed=batch_outcome["failed"],
            cancelled=batch_outcome["cancelled"],
            outputs=batch_outcome["outputs"],
            errors=batch_outcome["errors"],
            metadata={"engine_called": True, "engine_returned": batch_outcome["returned"]},
        ),
        paused=paused,
        cancel_requested=cancel_requested,
    )
    _safe_step(
        cb,
        "step3_count",
        f"{batch_stage.valid_output_count}/{planned_final_outputs} final MP4",
    )
    _safe_step(
        cb,
        "step3",
        "Final chroma complete" if batch_stage.is_success else "Final chroma incomplete",
    )
    result = finalize_pipeline_result(
        PipelineResult(
            pipeline="TC04",
            expected=planned_final_outputs,
            succeeded=batch_outcome["succeeded"],
            failed=batch_outcome["failed"],
            cancelled=batch_outcome["cancelled"],
            outputs=batch_outcome["outputs"],
            stages=[reframe_stage, batch_stage],
            errors=batch_outcome["errors"],
            metadata={
                "segment_duration": segment_duration,
                "effective_seed": effective_seed,
                "cover_enabled": bool(covers),
                "reframe_expected": expected_reframe,
                "reframe_returned": reframe_outcome["returned"],
                "batch_returned": batch_outcome["returned"],
                "planned_final_outputs": planned_final_outputs,
                "recomputed_final_outputs": batch_expected,
                "plan_consistent": True,
                "run_stamp": run_stamp,
            },
        ),
        paused=paused,
        cancel_requested=cancel_requested,
    )
    _safe_step(
        cb,
        "step2_count",
        f"{result.valid_output_count}/{result.expected} valid final outputs",
    )
    safe_log(
        cb.log_fn,
        f"done: status={result.status.value} reframe={reframe_stage.valid_output_count}/"
        f"{expected_reframe} batch={result.valid_output_count}/{result.expected}",
    )
    return result
