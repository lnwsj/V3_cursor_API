"""
core/pipelines/tc02_reframe.py - TC02 reframe -> chroma pipeline.

Pure Python. No tkinter imports.

Contract:
    Stage 1:
        inputs.products[i] -> 7 lens x selected composition reframe outputs
        written to {out_dir}/reframe/

    Stage 2:
        each reframe output -> TC01-style chroma render with backgrounds
        written to {out_dir}/

TC02 is intentionally not source-only. Source-only reframe belongs to TC05.
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime
from pathlib import Path

from ..ai_reframe import ReframeSettings, render_reframe_plan
from ..contract import (
    COVER_INTRO_SECONDS,
    TC02_VIDEO_DEFAULTS,
    green_settings_for,
    pick_compositions,
    reframe_outputs_per_source,
    reframe_settings_for,
)
from ..gpu_detector import gpu_summary
from ..green_render import GreenSettings, render_green
from ..media_probe import (
    MediaProbeCancelled,
    invalid_audio_stream_paths,
    invalid_video_stream_paths,
)

from ._common import (
    PipelineCallbacks,
    PipelineInputs,
    PipelineResult,
    StageResult,
    invalid_video_inputs,
    overlapping_role_paths,
    safe_file,
    safe_log,
    safe_progress,
)


def _pick_compositions(values: dict) -> list:
    return pick_compositions(values, all_three_default=True)


def _build_reframe_settings(values: dict, compositions: list) -> ReframeSettings:
    return reframe_settings_for(values, compositions=compositions, defaults=TC02_VIDEO_DEFAULTS)


def _build_green_settings(
    values: dict,
    has_uploaded_audio: bool,
    has_cover: bool = False,
) -> GreenSettings:
    return green_settings_for(
        values,
        has_audio=has_uploaded_audio,
        has_cover=has_cover,
        defaults=TC02_VIDEO_DEFAULTS,
    )

def _is_valid_output(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _safe_requested(callback) -> bool:
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
        return False


def _tracked_stop(cb: PipelineCallbacks):
    """Track interruption only when the active loop/engine observes it."""

    state = {"paused": False, "cancelled": False}

    def stop() -> bool:
        paused = _safe_requested(cb.pause_check)
        cancelled = _safe_requested(cb.stop_check)
        state["paused"] = state["paused"] or paused
        state["cancelled"] = state["cancelled"] or cancelled
        return paused or cancelled

    return stop, state


def _terminal_flags(cb: PipelineCallbacks, state) -> tuple[bool, bool]:
    paused = bool(state["paused"]) or _safe_requested(cb.pause_check)
    return paused, bool(state["cancelled"])


def _finish_counts(
    expected: int,
    succeeded: int,
    failed: int,
    *,
    paused: bool,
    cancel_requested: bool,
) -> tuple[int, int]:
    """Return final failed/cancelled counts with every task accounted for."""

    remaining = max(0, expected - succeeded - failed)
    if paused or cancel_requested:
        return failed, remaining
    return failed + remaining, 0


def _safe_stem(path: str) -> str:
    stem = Path(path).stem
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem)[:160]


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    """Run TC02 reframe then chroma, returning a staged fail-closed result."""

    products = list(inputs.products)
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    covers = list(inputs.covers)
    out_dir = inputs.output_dir
    composition_error = ""
    try:
        compositions = _pick_compositions(inputs.values)
        outputs_per_product = reframe_outputs_per_source(inputs.values)
    except (TypeError, ValueError) as exc:
        compositions = []
        outputs_per_product = 0
        composition_error = f"TC02 settings error: {exc}"
    stop, terminal_state = _tracked_stop(cb)

    input_errors = []
    if not products:
        input_errors.append("TC02 input error: no products")
    if not backgrounds:
        input_errors.append("TC02 input error: no backgrounds")
    role_overlap = overlapping_role_paths(products, backgrounds)
    if role_overlap:
        input_errors.append(
            f"TC02 input error: Product and Background must be different files: {role_overlap[0]}"
        )
    if composition_error:
        input_errors.append(composition_error)
    elif not compositions:
        input_errors.append("TC02 requires at least one enabled composition")
    invalid_products = invalid_video_inputs(products)
    if invalid_products:
        input_errors.append(f"TC02 input error: Product must be a video: {invalid_products[0]}")
    expected_reframe = len(products) * outputs_per_product
    expected_final = expected_reframe
    reframe_settings = None
    green_settings = None

    # Validate all output settings before any media probe or expensive reframe.
    if not input_errors:
        try:
            reframe_settings = _build_reframe_settings(
                inputs.values, compositions
            )
            green_settings = _build_green_settings(
                inputs.values,
                has_uploaded_audio=bool(audios),
                has_cover=bool(covers),
            )
        except Exception as exc:
            input_errors.append(f"TC02 settings error: {exc}")

    if not input_errors:
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
                    input_errors.append(
                        f"TC02 input error: {label} has no readable video stream: "
                        f"{invalid_streams[0]}"
                    )
                    break
            invalid_audio = invalid_audio_stream_paths(
                audios,
                stop_check=stop,
            )
            if not input_errors and invalid_audio:
                input_errors.append(
                    f"TC02 input error: Audio has no readable audio stream: {invalid_audio[0]}"
                )
        except MediaProbeCancelled:
            paused, cancel_requested = _terminal_flags(cb, terminal_state)
            expected = max(1, expected_final)
            safe_log(cb.log_fn, "TC02 media preflight cancelled")
            return PipelineResult(
                pipeline="TC02",
                expected=expected,
                cancelled=expected,
                metadata={"cancelled_during": "media_preflight"},
            ).finalize(
                paused=paused,
                cancel_requested=cancel_requested or not paused,
            )

    if input_errors:
        for message in input_errors:
            safe_log(cb.log_fn, message)
        return PipelineResult(
            pipeline="TC02",
            expected=expected_final,
            skipped=expected_final,
            errors=input_errors,
        ).finalize(invalid_input=True)

    if reframe_settings is None or green_settings is None:
        # Settings must be initialised by reframe_settings_for() / green_settings_for()
        # above; if either is None here the pipeline is in an inconsistent state.
        # Fail closed with a structured INVALID_INPUT result instead of relying on
        # `assert` (which is stripped under `python -O` / PyInstaller optimisation).
        message = (
            "TC02 settings resolved to None "
            f"(reframe_settings={reframe_settings}, green_settings={green_settings})"
        )
        safe_log(cb.log_fn, message)
        return PipelineResult(
            pipeline="TC02",
            expected=expected_final,
            skipped=expected_final,
            errors=[message],
        ).finalize(invalid_input=True)

    reframe_dir = os.path.join(out_dir, "reframe")
    try:
        os.makedirs(reframe_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)
    except Exception as exc:
        message = f"TC02 output directory error: {exc}"
        safe_log(cb.log_fn, message)
        return PipelineResult(
            pipeline="TC02",
            expected=expected_final,
            failed=expected_final,
            errors=[message],
        ).finalize()

    try:
        gpu = gpu_summary()
    except Exception:
        gpu = {}

    safe_log(
        cb.log_fn,
        f"TC02 reframe->chroma {len(products)} products -> "
        f"{expected_reframe} reframe -> {expected_final} final",
    )
    safe_log(cb.log_fn, f"[gpu] {gpu}")

    started = time.time()
    last_source = [""]

    def _on_reframe_progress(c: int, t: int, task_obj) -> None:
        src = task_obj.source_path
        if src != last_source[0]:
            last_source[0] = src
            safe_file(cb.file_fn, f"reframe: {os.path.basename(src)}")
        safe_progress(
            cb.progress_fn,
            45.0 * (c / max(t, 1)),
            f"Reframe {c}/{t}: {task_obj.lens.key}/{task_obj.composition.value}",
        )

    reframe_results = []
    reframe_errors = []
    if not stop():
        try:
            reframe_results = list(
                render_reframe_plan(
                    sources=products,
                    out_dir=reframe_dir,
                    settings=reframe_settings,
                    on_log=cb.log_fn,
                    on_progress=_on_reframe_progress,
                    stop_check=stop,
                    tc_label="TC02",
                )
            )
        except Exception as exc:
            reframe_errors.append(f"reframe engine: {exc}")
            safe_log(cb.log_fn, f"reframe error: {exc}")
            safe_log(cb.log_fn, traceback.format_exc())

    reframe_outputs = []
    reframe_failed = 0
    reframe_engine_cancelled = False
    for index, item in enumerate(reframe_results, 1):
        try:
            path = os.fspath(item.task.output_path)
            if bool(getattr(item, "success", False)) and _is_valid_output(path):
                reframe_outputs.append(path)
            elif bool(getattr(item, "cancelled", False)):
                reframe_engine_cancelled = True
            else:
                reframe_failed += 1
                detail = str(getattr(item, "error", "") or "missing/zero output")
                reframe_errors.append(f"reframe.{index:03d}: {detail}")
        except Exception as exc:
            reframe_failed += 1
            reframe_errors.append(f"reframe.{index:03d}: {exc}")

    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    cancel_requested = cancel_requested or reframe_engine_cancelled
    reframe_failed, reframe_cancelled = _finish_counts(
        expected_reframe,
        len(reframe_outputs),
        reframe_failed,
        paused=paused,
        cancel_requested=cancel_requested,
    )
    reframe_stage = StageResult(
        name="reframe",
        expected=expected_reframe,
        succeeded=len(reframe_outputs),
        failed=reframe_failed,
        cancelled=reframe_cancelled,
        outputs=reframe_outputs,
        errors=reframe_errors,
        required=True,
    ).finalize(
        paused=paused,
        cancel_requested=cancel_requested,
    )

    safe_log(
        cb.log_fn,
        f"reframe.actual.count={len(reframe_outputs)} "
        f"status={reframe_stage.status.value}",
    )

    # Chroma is forbidden unless every required reframe artifact is valid.
    if not reframe_stage.is_success:
        pipeline_failed, pipeline_cancelled = _finish_counts(
            expected_final,
            0,
            0,
            paused=paused,
            cancel_requested=cancel_requested,
        )
        chroma_stage = StageResult(
            name="chroma",
            expected=expected_final,
            skipped=0 if paused or cancel_requested else expected_final,
            cancelled=expected_final if paused or cancel_requested else 0,
            errors=[] if paused or cancel_requested else ["blocked by incomplete reframe"],
            required=True,
        ).finalize(
            paused=paused,
            cancel_requested=cancel_requested,
        )
        result = PipelineResult(
            pipeline="TC02",
            expected=expected_final,
            failed=pipeline_failed,
            cancelled=pipeline_cancelled,
            stages=[reframe_stage, chroma_stage],
            errors=reframe_stage.all_errors or ["reframe stage incomplete"],
            metadata={"elapsed_sec": time.time() - started},
        ).finalize(
            paused=paused,
            cancel_requested=cancel_requested,
        )
        safe_log(
            cb.log_fn,
            f"reframe incomplete: {len(reframe_outputs)}/{expected_reframe} "
            f"status={result.status.value}",
        )
        return result

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cover = covers[0] if covers else None
    final_outputs = []
    chroma_errors = []
    chroma_failed = 0
    total = len(reframe_outputs)
    chroma_engine_cancelled = False

    for idx, reframed in enumerate(reframe_outputs, 1):
        if stop():
            safe_log(cb.log_fn, f"stopped before chroma {idx}/{total}")
            break
        background = backgrounds[(idx - 1) % len(backgrounds)]
        audio = audios[(idx - 1) % len(audios)] if audios else None
        out_path = os.path.join(
            out_dir,
            f"{_safe_stem(reframed)}__tc02_chroma_{run_stamp}_{idx:03d}.mp4",
        )
        safe_file(cb.file_fn, f"chroma: {os.path.basename(reframed)}")

        def _on_green_progress(p, _idx=idx, _total=total) -> None:
            pct = getattr(p, "pct", float(p) if isinstance(p, (int, float)) else 0.0)
            overall = 45.0 + 55.0 * (((_idx - 1) + pct / 100.0) / max(_total, 1))
            safe_progress(cb.progress_fn, overall, f"Chroma {_idx}/{_total} ({pct:.0f}%)")

        try:
            engine_result = render_green(
                cover=cover,
                product=reframed,
                background=background,
                audio=audio,
                out_path=out_path,
                settings=green_settings,
                on_log=cb.log_fn,
                on_progress=_on_green_progress,
                stop_check=stop,
                tc_label="TC02",
            )
            if bool(getattr(engine_result, "success", False)) and _is_valid_output(out_path):
                final_outputs.append(out_path)
                safe_log(cb.log_fn, f"final.{idx:03d}.ok path={out_path}")
            elif bool(getattr(engine_result, "cancelled", False)):
                safe_log(cb.log_fn, f"final.{idx:03d}.cancelled")
                chroma_engine_cancelled = True
                break
            else:
                chroma_failed += 1
                detail = str(getattr(engine_result, "error", "") or "missing/zero output")
                chroma_errors.append(f"final.{idx:03d}: {detail}")
                safe_log(cb.log_fn, f"final.{idx:03d}.fail error={detail[:200]}")
        except Exception as exc:
            chroma_failed += 1
            chroma_errors.append(f"final.{idx:03d}: {exc}")
            safe_log(cb.log_fn, f"final.{idx:03d}.exception error={exc}")
            safe_log(cb.log_fn, traceback.format_exc())

    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    cancel_requested = cancel_requested or chroma_engine_cancelled
    completed_all = len(final_outputs) == expected_final and chroma_failed == 0
    if completed_all and not chroma_engine_cancelled:
        cancel_requested = False
    chroma_failed, chroma_cancelled = _finish_counts(
        expected_final,
        len(final_outputs),
        chroma_failed,
        paused=paused,
        cancel_requested=cancel_requested,
    )
    chroma_stage = StageResult(
        name="chroma",
        expected=expected_final,
        succeeded=len(final_outputs),
        failed=chroma_failed,
        cancelled=chroma_cancelled,
        outputs=final_outputs,
        errors=chroma_errors,
        required=True,
    ).finalize(
        paused=paused,
        cancel_requested=cancel_requested,
    )

    result = PipelineResult(
        pipeline="TC02",
        expected=expected_final,
        succeeded=len(final_outputs),
        failed=chroma_failed,
        cancelled=chroma_cancelled,
        outputs=final_outputs,
        stages=[reframe_stage, chroma_stage],
        errors=chroma_errors,
        metadata={"elapsed_sec": time.time() - started},
    ).finalize(
        paused=paused,
        cancel_requested=cancel_requested,
    )

    safe_log(
        cb.log_fn,
        f"done: reframe={len(reframe_outputs)}/{expected_reframe} "
        f"final={len(final_outputs)}/{expected_final} "
        f"status={result.status.value} elapsed={time.time() - started:.1f}s",
    )
    if result.is_success:
        safe_progress(cb.progress_fn, 100.0, "done")
    return result
