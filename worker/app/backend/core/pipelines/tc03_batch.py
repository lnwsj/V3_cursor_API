"""
core/pipelines/tc03_batch.py — TC03 Batch + chroma pipeline.

Pure Python. No tkinter imports.

Contract:
    inputs.products[i] -> split by segment_duration, then chroma-key
    inputs.backgrounds -> required chroma background pool
    inputs.audios      -> optional pool
    inputs.covers      -> optional; first cover is a 2-second intro

segment_duration is configurable from 0.5 to 600 seconds, default 10.
The legacy TC03_SEGMENT_SECONDS constant remains the default alias only.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

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
    invalid_audio_stream_paths,
    invalid_video_stream_paths,
)
from ..contract import (
    TC03_SEGMENT_DURATION_DEFAULT,
    TC03_VIDEO_DEFAULTS,
    batch_settings_for,
    green_settings_for,
    validate_tc03_segment_duration,
)
from ._common import (
    PipelineCallbacks,
    PipelineInputs,
    PipelineResult,
    finalize_pipeline_result,
    safe_file,
    invalid_video_inputs,
    overlapping_role_paths,
    resolve_run_seed,
    safe_log,
    safe_progress,
)


# Backwards-compatible default alias. Runtime reads inputs.values instead.
TC03_SEGMENT_SECONDS: float = TC03_SEGMENT_DURATION_DEFAULT
TC03_DEFAULTS = TC03_VIDEO_DEFAULTS


def _resolve_segment_duration(values: dict) -> float:
    """Legacy tolerant resolver retained for older callers and tests.

    The production render path below deliberately uses the strict
    ``validate_tc03_segment_duration`` contract so invalid user input fails
    closed. This helper preserves main's earlier API behavior without
    weakening the V1.0.0.18 runtime validation.
    """

    raw = values.get("segment_duration") if isinstance(values, dict) else None
    try:
        duration = float(raw) if raw is not None else TC03_SEGMENT_SECONDS
    except (TypeError, ValueError):
        duration = TC03_SEGMENT_SECONDS
    return max(0.5, min(duration, 600.0))


def _build_green_settings(
    values: dict,
    has_audio: bool,
    has_cover: bool = False,
) -> GreenSettings:
    """Build final chroma settings, including the optional cover contract."""
    return green_settings_for(
        values,
        has_audio=has_audio,
        has_cover=has_cover,
        defaults=TC03_VIDEO_DEFAULTS,
    )


def _build_batch_settings(
    values: dict,
    segment_duration: float,
    num_outputs: int,
    has_audio: bool,
) -> BatchSettings:
    """Build deterministic-per-run batch settings from validated values."""
    settings = batch_settings_for(
        segment_duration=segment_duration,
        num_outputs=num_outputs,
        has_audio=has_audio,
        match_mode_str=values.get(
            "match_mode",
            TC03_VIDEO_DEFAULTS["match_mode"],
        ),
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
        seed=resolve_run_seed(values.get("seed", 0)),
    )


def _safe_requested(callback: Any) -> bool:
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
        return False


def _tracked_stop(cb: PipelineCallbacks) -> Tuple[Any, Dict[str, bool]]:
    """Return an engine stop callback plus sticky pause/cancel observations."""
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


def _output_path(result: Any) -> str:
    raw = getattr(result, "output_path", "")
    try:
        return os.fspath(raw)
    except TypeError:
        return ""


def _summarize_batch_results(
    engine_results: Iterable[Any],
    *,
    expected: int,
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
            # FIX (B-05, 2026-07-31): success but no path -> failure, not success.
            resolved = _output_path(item)
            if not resolved:
                failed += 1
                errors.append(
                    f"batch output {index} reported success but path is empty"
                )
                continue
            succeeded += 1
            outputs.append(resolved)
        elif bool(getattr(item, "cancelled", False)):
            cancelled += 1
        else:
            failed += 1
            detail = str(getattr(item, "error", "") or "unknown engine failure")
            # FIX (B-26, 2026-07-31): include product path / segment index from
            # the attached BatchMatch so operators can identify which
            # product + segment failed.
            match = getattr(item, "match", None)
            match_detail = ""
            if match is not None:
                match_detail = (
                    f" product={os.path.basename(match.product_path)} "
                    f"seg={match.segment.segment_index}"
                )
            errors.append(f"batch output {index} failed:{match_detail} {detail}")

    returned = len(results)
    missing = max(expected - returned, 0)
    if missing:
        if paused or cancel_requested:
            cancelled += missing
        else:
            failed += missing
            errors.append(f"batch engine incomplete: returned={returned} expected={expected}")
    if returned == 0:
        errors.append("batch engine returned no results")
    if returned > expected:
        errors.append(f"batch engine returned too many results: {returned}>{expected}")
        # FIX (B-06, 2026-07-31): when the engine emits more than expected,
        # normalise counts so accounted_count == expected. Otherwise the
        # invariant check in _finalize_common trips and the pipeline ends
        # up FAILED with a misleading "invariant error". We decrement
        # failed first (failed entries are usually phantom over-emits with
        # no output path attached), then cancelled, then succeeded last
        # (preserving valid output files). Trim outputs to the expected
        # prefix so downstream consumers see only the canonical list.
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
            pipeline="TC03",
            expected=0,
            errors=[message],
            metadata={"segment_duration_raw": raw_duration},
        ),
        invalid_input=True,
    )


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    """Run TC03 and return a fail-closed truth-bearing result."""
    products = list(inputs.products)
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    covers = list(inputs.covers)
    out_dir = inputs.output_dir
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stop, terminal_state = _tracked_stop(cb)

    if not products:
        result = _invalid_result("TC03 requires at least one product")
        safe_log(cb.log_fn, result.all_errors[0])
        return result
    if not backgrounds:
        result = _invalid_result("TC03 requires at least one background")
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    role_overlap = overlapping_role_paths(products, backgrounds)
    if role_overlap:
        result = _invalid_result(
            "TC03 Product and Background must be different files: "
            + role_overlap[0]
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    invalid_products = invalid_video_inputs(products)
    if invalid_products:
        result = _invalid_result(f"TC03 Product must be a video: {invalid_products[0]}")
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    raw_duration = inputs.values.get(
        "segment_duration",
        TC03_SEGMENT_DURATION_DEFAULT,
    )
    try:
        # Validate output settings before media and duration probes.
        segment_duration = validate_tc03_segment_duration(raw_duration)
        base_settings = _build_green_settings(
            inputs.values,
            has_audio=bool(audios),
            has_cover=bool(covers),
        )
    except (TypeError, ValueError) as exc:
        result = _invalid_result(str(exc), raw_duration)
        safe_log(cb.log_fn, str(exc))
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
                    f"TC03 {label} has no readable video stream: {invalid_streams[0]}"
                )
                safe_log(cb.log_fn, result.all_errors[0])
                return result

        invalid_audio = invalid_audio_stream_paths(
            audios,
            stop_check=stop,
        )
        if invalid_audio:
            result = _invalid_result(
                f"TC03 Audio has no readable audio stream: {invalid_audio[0]}"
            )
            safe_log(cb.log_fn, result.all_errors[0])
            return result
    except MediaProbeCancelled:
        paused, cancel_requested = _terminal_flags(cb, terminal_state)
        safe_log(cb.log_fn, "TC03 media preflight cancelled")
        return finalize_pipeline_result(
            PipelineResult(
                pipeline="TC03",
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
        safe_log(cb.log_fn, "TC03 duration planning cancelled")
        return finalize_pipeline_result(
            PipelineResult(
                pipeline="TC03",
                expected=1,
                cancelled=1,
                metadata={
                    "cancelled_during": "duration_planning",
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
            f"TC03 Product has no readable duration: {unreadable_products[0]}"
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        num_outputs = estimate_duration_split_count(
            products,
            segment_duration,
            duration_snapshot=product_duration_snapshot,
        )
    except Exception as exc:
        result = _invalid_result(
            f"unable to plan TC03 segments: {exc}", raw_duration
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result
    if num_outputs <= 0:
        result = _invalid_result(
            "TC03 products produced no renderable segments", raw_duration
        )
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        batch_settings = _build_batch_settings(
            inputs.values,
            segment_duration,
            num_outputs,
            has_audio=bool(audios),
        )
    except (TypeError, ValueError) as exc:
        result = _invalid_result(str(exc), raw_duration)
        safe_log(cb.log_fn, result.all_errors[0])
        return result

    try:
        gpu = gpu_summary()
    except Exception:
        gpu = {}
    safe_log(
        cb.log_fn,
        f"batch->chroma {len(products)} products -> {num_outputs} final outputs "
        f"(segment={segment_duration:g}s cover={bool(covers)})",
    )
    safe_log(cb.log_fn, f"[gpu] {gpu}")
    safe_log(cb.log_fn, f"[batch] effective_seed={batch_settings.seed}")

    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        outcome = {
            "succeeded": 0,
            "failed": num_outputs,
            "cancelled": 0,
            "outputs": [],
            "errors": [f"unable to create TC03 output directory: {exc}"],
            "returned": 0,
        }
        return finalize_pipeline_result(
            PipelineResult(pipeline="TC03", expected=num_outputs, **{
                key: outcome[key]
                for key in ("succeeded", "failed", "cancelled", "outputs", "errors")
            })
        )

    last_product = [""]

    def log_match(match: Any) -> None:
        if cb.file_fn is not None and match.product_path != last_product[0]:
            last_product[0] = match.product_path
            safe_file(cb.file_fn, match.product_path)

    def on_progress(current: int, total: int, progress: Any) -> None:
        pct = getattr(progress, "pct", 0)
        safe_progress(
            cb.progress_fn,
            ((current - 1) + pct / 100.0) * 100.0 / max(total, 1),
            f"Output {current}/{total}: {pct:.0f}%",
        )

    engine_error = ""
    try:
        engine_results = render_batch(
            products=products,
            backgrounds=backgrounds,
            audios=audios,
            covers=covers,
            out_dir=out_dir,
            base_settings=base_settings,
            duration_snapshot=product_duration_snapshot,
            batch_settings=batch_settings,
            on_log=cb.log_fn,
            on_match=log_match,
            on_progress=on_progress,
            stop_check=stop,
            tc_label="TC03",
            chroma_max_parallel=1,
            run_stamp=run_stamp,
        )
    except Exception as exc:
        engine_results = []
        engine_error = f"batch engine exception: {exc}"
        safe_log(cb.log_fn, engine_error)

    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    outcome = _summarize_batch_results(
        engine_results,
        expected=num_outputs,
        paused=paused,
        cancel_requested=cancel_requested,
        engine_error=engine_error,
    )
    completed_all = (
        outcome["succeeded"] == num_outputs
        and outcome["failed"] == 0
        and outcome["cancelled"] == 0
        and outcome["returned"] == num_outputs
    )
    if completed_all:
        cancel_requested = False
    result = finalize_pipeline_result(
        PipelineResult(
            pipeline="TC03",
            expected=num_outputs,
            succeeded=outcome["succeeded"],
            failed=outcome["failed"],
            cancelled=outcome["cancelled"],
            outputs=outcome["outputs"],
            errors=outcome["errors"],
            metadata={
                "segment_duration": segment_duration,
                "effective_seed": batch_settings.seed,
                "cover_enabled": bool(covers),
                "engine_returned": outcome["returned"],
                "run_stamp": run_stamp,
            },
        ),
        paused=paused,
        cancel_requested=cancel_requested,
    )
    safe_log(
        cb.log_fn,
        f"done: status={result.status.value} ok={result.succeeded}/{result.expected}",
    )
    return result
