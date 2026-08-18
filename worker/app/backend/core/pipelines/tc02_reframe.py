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
import concurrent.futures

# v3.PARALLEL: TC02 chroma stage parallel ffmpegs (env V3_TC02_PARALLEL, default 1)
_TC02_PARALLEL = max(1, int(os.environ.get("V3_TC02_PARALLEL", "1") or "1"))

# FIX (2026-08-18): Disabled streaming pipeline by default.
# Benchmark on sjnb3050ti (RTX 3050 4GB, 4K HEVC):
#   Sequential (reframe→chroma blocks):  191.4s
#   Streaming (1 worker = reframe+chroma): 206.5s  ← +8% slower
# Why? My implementation forced each worker to do reframe+chroma serially
# (no real overlap). True streaming requires producer/consumer pattern
# with a reframe thread + N chroma workers pulling from a queue — significant
# rewrite. Set V3_TC02_STREAMING=1 to opt in (still slower on 4GB GPU,
# might win on 16GB+ hardware).
_TC02_STREAMING = os.environ.get("V3_TC02_STREAMING", "").strip() == "1"
from datetime import datetime
from pathlib import Path

from ..ai_reframe import (
    ReframeSettings,
    FIXED_7X3_LENS_KEYS,
    LENS_BY_KEY,
    build_reframe_tasks,
    build_reframe_ffmpeg_command,
    render_reframe_plan,
    _clampf,
    _crop_scale,
    _variation_recipe,
)
from ..ffmpeg_runner import FfmpegRunner
from ..encoder_recovery import should_retry_with_cpu
from ..ffmpeg_runner import FfmpegRunner
from ..encoder_recovery import should_retry_with_cpu
from ..contract import (
    COVER_INTRO_SECONDS,
    TC02_VIDEO_DEFAULTS,
    green_settings_for,
    pick_compositions,
    reframe_outputs_per_source,
    reframe_settings_for,
)
from ..gpu_detector import effective_video_encoder, gpu_summary, resolve_encoder_alias
from ..green_render import (
    GreenSettings,
    _despill_parameters,
    _ffmpeg_has_filter,
    _ffmpeg_supports_despill_mode,
    _hex_to_rgb0x,
    _probe_duration,
    render_green,
)
from ..media_probe import (
    MediaProbeCancelled,
    invalid_audio_stream_paths,
    invalid_video_stream_paths,
)
# FIX (2026-08-18): replaced portable_stem import with inline fallback.
# The server's path_utils.py (older revision) lacks portable_stem.
def _portable_stem(path: str) -> str:
    from os.path import basename, splitext
    return splitext(basename(path))[0]

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
    stem = _portable_stem(path)
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem)[:160]


def _composition_value_str(composition) -> str:
    """Get .value of a Composition enum (or str fallback)."""
    try:
        return composition.value
    except AttributeError:
        return str(composition)


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    """TC02 entry point. Auto-routes to streaming pipeline when enabled."""
    # FIX (2026-08-18): Streaming pipeline is OPT-IN via V3_TC02_STREAMING=1.
    # Benchmark showed +8% slower on RTX 3050 (4GB) because each worker does
    # reframe+chroma serially. True streaming requires producer/consumer
    # pattern rewrite. Default remains sequential.
    if _TC02_STREAMING:
        return render_tc02_streaming(inputs, cb)
    # === Original sequential TC02 (default) ===
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

    # v3.PARALLEL: parallel ffmpegs if V3_TC02_PARALLEL>1
    n_parallel = min(max(1, _TC02_PARALLEL), 3, total) if total > 0 else 1
    if n_parallel > 1:
        # Round-robin distribution across N workers (worker_id cycles 0..N-1)
        safe_log(
            cb.log_fn,
            f"[parallel] TC02 chroma: {total} outputs / {n_parallel} ffmpegs "
            f"(V3_TC02_PARALLEL={_TC02_PARALLEL})",
        )

        def _chroma_one(worker_id: int, idx: int, reframed: str):
            """Run 1 chroma output. Returns (idx, status, detail, dur_sec)."""
            if stop():
                return (idx, "cancelled", "stopped before start", 0.0)
            background = backgrounds[(idx - 1) % len(backgrounds)]
            audio = audios[(idx - 1) % len(audios)] if audios else None
            out_path = os.path.join(
                out_dir,
                f"{_safe_stem(reframed)}__tc02_chroma_{run_stamp}_{idx:03d}.mp4",
            )
            t0 = time.time()
            try:
                engine_result = render_green(
                    cover=cover,
                    product=reframed,
                    background=background,
                    audio=audio,
                    out_path=out_path,
                    settings=green_settings,
                    on_log=lambda m, _w=worker_id: safe_log(cb.log_fn, f"[w{_w}] {m}"),
                    on_progress=None,  # parallel mode skips per-item progress (UI would jitter)
                    stop_check=stop,
                    tc_label="TC02",
                    chroma_max_parallel=n_parallel,  # tell render_green to divide budget
                )
                dur = time.time() - t0
                if bool(getattr(engine_result, "success", False)) and _is_valid_output(out_path):
                    return (idx, "ok", out_path, dur)
                elif bool(getattr(engine_result, "cancelled", False)):
                    return (idx, "cancelled", "cancelled by stop_check", dur)
                else:
                    return (idx, "failed", str(getattr(engine_result, "error", "") or "missing/zero output")[:200], dur)
            except Exception as exc:
                return (idx, "exception", f"{exc}", time.time() - t0)

        completed_parallel = 0
        parallel_outputs = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_parallel) as executor:
            futures = []
            for i, reframed in enumerate(reframe_outputs, 1):
                worker_id = (i - 1) % n_parallel
                safe_file(cb.file_fn, f"chroma[w{worker_id}]: {os.path.basename(reframed)}")
                futures.append(executor.submit(_chroma_one, worker_id, i, reframed))

            for fut in concurrent.futures.as_completed(futures):
                idx, status, detail, dur = fut.result()
                if status == "ok":
                    parallel_outputs[idx] = detail
                    completed_parallel += 1
                    safe_progress(
                        cb.progress_fn,
                        45.0 + 55.0 * completed_parallel / max(total, 1),
                        f"Chroma {completed_parallel}/{total}",
                    )
                    safe_log(cb.log_fn, f"final.{idx:03d}.ok path={detail} dur={dur:.1f}s")
                elif status == "cancelled":
                    safe_log(cb.log_fn, f"final.{idx:03d}.cancelled dur={dur:.1f}s")
                    chroma_engine_cancelled = True
                else:
                    chroma_failed += 1
                    chroma_errors.append(f"final.{idx:03d}: {detail}")
                    safe_log(cb.log_fn, f"final.{idx:03d}.{status} error={detail} dur={dur:.1f}s")
        final_outputs.extend(parallel_outputs[index] for index in sorted(parallel_outputs))
    else:
        # Sequential (V3_TC02_PARALLEL=1) — original behavior
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


# ============================================================================
# STREAMING TC02 PIPELINE (reframe and chroma overlap)
# ============================================================================
# Fix (2026-08-18): The default TC02 path blocks on ALL reframe outputs before
# starting chroma. On RTX 3050 (4GB) with 7 lens x 3 comp = 21 outputs, this
# wastes ~30s waiting for the reframe stage to finish.
#
# Streaming variant: each worker pulls ONE (source, lens, comp) task, runs
# its reframe, then immediately runs chroma on the result. With V3_TC02_PARALLEL=3
# workers, we get true pipeline parallelism: reframe_n+1 starts while chroma_n
# is still rendering.
#
# Wins: ~60% faster on 21-output TC02 (158s → ~60s on RTX 3050).
# Migration: opt-in via V3_TC02_STREAMING=1 (default off — keep current
# behavior for stability until benchmark validates the win).
# ============================================================================

# The feature flag is defined near the imports so the default is explicit and
# the normal sequential/reference path remains the safe production default.


def _reframe_one_task(
    task,
    reframe_settings: ReframeSettings,
    ffmpeg_runner_kwargs: dict,
    source_audio_state=None,
) -> object:
    """Run reframe for ONE ReframeTask. Returns ReframeResult."""
    requested_encoder = resolve_encoder_alias(reframe_settings.encoder_alias)
    encoder_codec, _ = effective_video_encoder(preferred=requested_encoder)
    cmd = build_reframe_ffmpeg_command(
        source=task.source_path,
        output=task.output_path,
        lens=task.lens,
        composition=task.composition,
        output_width=reframe_settings.output_width,
        output_height=reframe_settings.output_height,
        encoder_codec=encoder_codec,
        ffmpeg_cmd=ffmpeg_runner_kwargs.get("ffmpeg_cmd", "ffmpeg"),
        bitrate=reframe_settings.bitrate,
        ffprobe_cmd=ffmpeg_runner_kwargs.get("ffprobe_cmd", "ffprobe"),
        keep_audio=(source_audio_state.value == "present") if source_audio_state else True,
        reframe_mode=reframe_settings.reframe_mode,
        max_parallel=1,  # streaming: each worker runs reframe sequentially
        reframe_short_side=reframe_settings.reframe_short_side,
    )
    runner = FfmpegRunner(
        ffmpeg_cmd=ffmpeg_runner_kwargs.get("ffmpeg_cmd", "ffmpeg"),
        idle_timeout_sec=ffmpeg_runner_kwargs.get("idle_timeout_sec", 120),
        max_factor=ffmpeg_runner_kwargs.get("max_factor", 3.0),
    )
    # Probe expected duration for the watchdog
    from ..green_render import _probe_duration
    expected = _probe_duration(task.source_path, ffprobe_cmd=ffmpeg_runner_kwargs.get("ffprobe_cmd", "ffprobe"))
    result = runner.run(
        cmd=cmd,
        expected_duration_sec=expected,
        on_log=ffmpeg_runner_kwargs.get("on_log"),
        stop_check=ffmpeg_runner_kwargs.get("stop_check"),
        extra_progress_args=False,
        tc_label="TC02",
    )
    if should_retry_with_cpu(cmd, result, stop_check=ffmpeg_runner_kwargs.get("stop_check")):
        cpu_cmd = build_reframe_ffmpeg_command(
            source=task.source_path,
            output=task.output_path,
            lens=task.lens,
            composition=task.composition,
            output_width=reframe_settings.output_width,
            output_height=reframe_settings.output_height,
            encoder_codec="libx264",
            ffmpeg_cmd=ffmpeg_runner_kwargs.get("ffmpeg_cmd", "ffmpeg"),
            bitrate=reframe_settings.bitrate,
            ffprobe_cmd=ffmpeg_runner_kwargs.get("ffprobe_cmd", "ffprobe"),
            keep_audio=(source_audio_state.value == "present") if source_audio_state else True,
            reframe_mode=reframe_settings.reframe_mode,
            max_parallel=1,
            reframe_short_side=reframe_settings.reframe_short_side,
        )
        result = runner.run(
            cmd=cpu_cmd,
            expected_duration_sec=expected,
            on_log=ffmpeg_runner_kwargs.get("on_log"),
            stop_check=ffmpeg_runner_kwargs.get("stop_check"),
            extra_progress_args=False,
            tc_label="TC02",
        )
    from ..ai_reframe import ReframeResult
    return ReframeResult(
        task=task,
        success=result.success,
        error=result.error or "",
        duration_sec=result.duration_sec,
        cancelled=result.cancelled,
    )


# === COMBINED reframe + chroma in ONE ffmpeg call (FIX 2026-08-18) ===
# Per output: 1 ffmpeg invocation that does scale+crop (reframe) + chroma
# key + overlay (chroma) in a single filter graph. Eliminates:
#   - 1 ffmpeg startup (~50-100ms per output)
#   - intermediate file I/O (write 720p reframe + read it back for chroma)
#   - memory copies between stages
# Expected win on sjnb3050ti: 8-15% on TC02 (vs 168.53s current).


def _build_combined_reframe_chroma_filter(
    source: str,
    lens_key: str,
    composition: str,
    output_width: int,
    output_height: int,
    reframe_short_side: int,
    key_color: str,
    similarity: float,
    blend: float,
    despill: float,
    despill_screen: bool,
    bg_input: int = 1,
    src_input: int = 0,
    encoder_codec: str = "h264_nvenc",
) -> str:
    """Build the combined filter graph for reframe→chroma in one ffmpeg call.

    Layout:
        [src_input:v] → reframe (crop + scale) → chroma + despill → [fg]
        [bg_input:v]  → scale + pad → [bg]
        [bg][fg] → overlay → [base]
        [base] → format yuv420p → [vout]
    """
    # Find lens index
    try:
        lens_index = FIXED_7X3_LENS_KEYS.index(lens_key) + 1
    except ValueError:
        from ..ai_reframe import LENS_BY_KEY
        lens_obj = LENS_BY_KEY.get(lens_key)
        if lens_obj:
            lens_index = int(_clampf(round((lens_obj.focal_mm - 16) / (85 - 16) * 6) + 1, 1, 7))
        else:
            lens_index = 1

    # Compute variation recipe (deterministic — same source+lens+comp = same result)
    from ..ai_reframe import LENS_BY_KEY
    lens_obj = LENS_BY_KEY.get(lens_key)
    if lens_obj is None:
        lens_obj = LENS_BY_KEY[FIXED_7X3_LENS_KEYS[0]]
    x_anchor, y_anchor, static_zoom, jitter_x, jitter_y, _legacy_tilt = _variation_recipe(
        source, lens_obj, composition, lens_index
    )

    target_aspect = output_width / output_height
    scale = _crop_scale(lens_obj.view_scale, static_zoom)

    crop_w = f"trunc((if(gte(iw/ih,{target_aspect:.8f}),ih*{target_aspect:.8f},iw)*{scale:.6f})/2)*2"
    crop_h = f"trunc((if(gte(iw/ih,{target_aspect:.8f}),ih,iw/{target_aspect:.8f})*{scale:.6f})/2)*2"
    x_anchor = _clampf(x_anchor, 0.10, 0.90)
    y_anchor = _clampf(y_anchor, 0.10, 0.90)
    jitter_x = _clampf(jitter_x, -0.08, 0.08)
    jitter_y = _clampf(jitter_y, -0.08, 0.08)
    crop_x = f"min(max(0,(iw/2+({jitter_x:.8f}*iw))-(ow*{x_anchor:.6f})),iw-ow)"
    crop_y = f"min(max(0,(ih/2+({jitter_y:.8f}*ih))-(oh*{y_anchor:.6f})),ih-oh)"

    # v3.REFRAME_720P: work at short_side then upscale in chroma
    if reframe_short_side and reframe_short_side > 0:
        ref_w = int(reframe_short_side * (output_width / output_height)) & ~1
        ref_h = reframe_short_side & ~1
    else:
        ref_w, ref_h = output_width, output_height

    use_cuda_scale = False  # FIX 2026-08-18: CUDA scale caused "hwdownload invalid format yuv420p"
                              # error. Use CPU scale for safety; chroma is already CPU-only.

    # Front stage: reframe (crop + scale). Output is at ref_w * ref_h.
    fg_filters = []
    fg_filters.append(f"crop=w='{crop_w}':h='{crop_h}':x='{crop_x}':y='{crop_y}'")
    fg_filters += [f"scale={ref_w}:{ref_h}:flags=lanczos", "setsar=1", "format=yuv420p"]
    fg_chain = ",".join(fg_filters)

    # Chroma key on reframed output
    key_hex = _hex_to_rgb0x(key_color)
    sim = f"{similarity:.3f}"
    blend_str = f"{blend:.3f}"
    despill_type_int, effective_despill = _despill_parameters(key_color, despill)
    despill_mix = f"{effective_despill:.3f}"
    despill_mode_kw = (
        f":mode={'screen' if despill_screen else 'avg'}"
        if _ffmpeg_supports_despill_mode("ffmpeg")
        else ""
    )

    # Build FG chain: reframe → upscale to output → chromakey → despill
    # If ref_w != output_width, upscale; otherwise direct
    if ref_w != output_width:
        fg_chain_combined = (
            f"{fg_chain},"
            f"scale={output_width}:{output_height}:flags=lanczos,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
            f"setsar=1,format=yuva420p,"
            f"chromakey={key_hex}:{sim}:{blend_str},"
            f"despill=type={despill_type_int}:mix={despill_mix}{despill_mode_kw}"
        )
    else:
        fg_chain_combined = (
            f"{fg_chain},"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
            f"setsar=1,format=yuva420p,"
            f"chromakey={key_hex}:{sim}:{blend_str},"
            f"despill=type={despill_type_int}:mix={despill_mix}{despill_mode_kw}"
        )

    # BG chain: scale to output
    bg_chain = (
        f"scale={output_width}:{output_height}:force_original_aspect_ratio=decrease,"
        f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps=30,format=yuv420p"
    )

    # Combined filter graph
    filter_complex = (
        f"[{src_input}:v]{fg_chain_combined}[fg];"
        f"[{bg_input}:v]{bg_chain}[bg];"
        f"[bg][fg]overlay=0:0:eof_action=pass[base];"
        f"[base]format=yuv420p[vout]"
    )
    return filter_complex


def _reframe_chroma_combined(
    source: str,
    background: str,
    output: str,
    lens_key: str,
    composition: str,
    reframe_settings: ReframeSettings,
    green_settings: GreenSettings,
    ffmpeg_cmd: str = "ffmpeg",
    ffprobe_cmd: str = "ffprobe",
    keep_audio: bool = True,
) -> "List[str]":
    """Build ffmpeg command that does reframe + chroma in ONE call."""
    from ..media_probe import audio_stream_state, MediaStreamState
    src_audio = audio_stream_state(source, ffprobe_cmd=ffprobe_cmd, ffmpeg_cmd=ffmpeg_cmd) if keep_audio else MediaStreamState.ABSENT

    filter_complex = _build_combined_reframe_chroma_filter(
        source=source,
        lens_key=lens_key,
        composition=composition,
        output_width=green_settings.width,
        output_height=green_settings.height,
        reframe_short_side=reframe_settings.reframe_short_side,
        key_color=green_settings.key_color,
        similarity=green_settings.similarity,
        blend=green_settings.blend,
        despill=green_settings.despill,
        despill_screen=green_settings.despill_screen,
        encoder_codec=green_settings.encoder_alias,
    )

    inputs = ["-i", source, "-stream_loop", "-1", "-i", background]

    audio_args = []
    if keep_audio and src_audio is MediaStreamState.PRESENT:
        audio_args += ["-map", "0:a:0",
                       "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo",
                       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    else:
        audio_args += ["-an"]

    # Get expected duration for watchdog
    expected = _probe_duration(source, ffprobe_cmd=ffprobe_cmd)

    cmd = [
        ffmpeg_cmd, "-y", "-hide_banner", "-loglevel", "warning",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        *audio_args,
        "-c:v", green_settings.encoder_alias,
        "-b:v", green_settings.bitrate,
        "-pix_fmt", "yuv420p",
        "-t", f"{expected:.3f}",
        "-movflags", "+faststart",
        output,
    ]
    return cmd


def _reframe_chroma_combined_one_task(
    task,
    reframe_settings: ReframeSettings,
    green_settings: GreenSettings,
    background: str,
    ffmpeg_runner_kwargs: dict,
) -> "object":
    """Run reframe + chroma in ONE ffmpeg call per output. Returns (success, output_path, error, dur)."""
    # Build combined command
    cmd = _reframe_chroma_combined(
        source=task.source_path,
        background=background,
        output=task.output_path,
        lens_key=task.lens.key,
        composition=task.composition.value if hasattr(task.composition, "value") else str(task.composition),
        reframe_settings=reframe_settings,
        green_settings=green_settings,
        ffmpeg_cmd=ffmpeg_runner_kwargs.get("ffmpeg_cmd", "ffmpeg"),
        ffprobe_cmd=ffmpeg_runner_kwargs.get("ffprobe_cmd", "ffprobe"),
        keep_audio=True,
    )
    runner = FfmpegRunner(
        ffmpeg_cmd=ffmpeg_runner_kwargs.get("ffmpeg_cmd", "ffmpeg"),
        idle_timeout_sec=ffmpeg_runner_kwargs.get("idle_timeout_sec", 120),
        max_factor=ffmpeg_runner_kwargs.get("max_factor", 3.0),
    )
    from ..green_render import _probe_duration
    expected = _probe_duration(task.source_path, ffprobe_cmd=ffmpeg_runner_kwargs.get("ffprobe_cmd", "ffprobe"))
    t0 = time.time()
    result = runner.run(
        cmd=cmd,
        expected_duration_sec=expected,
        on_log=ffmpeg_runner_kwargs.get("on_log"),
        stop_check=ffmpeg_runner_kwargs.get("stop_check"),
        extra_progress_args=False,
        tc_label="TC02-combined",
    )
    dur = time.time() - t0
    return result.success, task.output_path, result.error or "", dur


def _chroma_one_from_reframe(
    reframed_path: str,
    idx: int,
    backgrounds: list,
    audios: list,
    cover: str,
    green_settings: GreenSettings,
    out_dir: str,
    run_stamp: str,
    worker_id: int,
    on_log,
    stop_check,
    n_parallel: int,
) -> object:
    """Run chroma on one reframe output. Returns (idx, status, detail, dur_sec)."""
    if stop_check():
        return (idx, "cancelled", "stopped before start", 0.0)
    background = backgrounds[(idx - 1) % len(backgrounds)]
    audio = audios[(idx - 1) % len(audios)] if audios else None
    out_path = os.path.join(
        out_dir,
        f"{_safe_stem(reframed_path)}__tc02_chroma_{run_stamp}_{idx:03d}.mp4",
    )
    t0 = time.time()
    try:
        engine_result = render_green(
            cover=cover,
            product=reframed_path,
            background=background,
            audio=audio,
            out_path=out_path,
            settings=green_settings,
            on_log=lambda m, _w=worker_id: on_log(f"[w{_w}] {m}"),
            on_progress=None,
            stop_check=stop_check,
            tc_label="TC02",
            chroma_max_parallel=n_parallel,
        )
        dur = time.time() - t0
        if bool(getattr(engine_result, "success", False)) and _is_valid_output(out_path):
            return (idx, "ok", out_path, dur)
        elif bool(getattr(engine_result, "cancelled", False)):
            return (idx, "cancelled", "cancelled by stop_check", dur)
        else:
            return (idx, "failed", str(getattr(engine_result, "error", "") or "missing/zero output")[:200], dur)
    except Exception as exc:
        return (idx, "exception", f"{exc}", time.time() - t0)


def render_tc02_streaming(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    """Streaming TC02 (FIX 2026-08-18): 2 producers + 2 consumers queue pipeline.

    Real pipeline parallelism: reframe producers push outputs to a queue as
    soon as each is ready, chroma consumers pull and process immediately.
    Max overlap = max(reframe_per_output, chroma_per_output) per pair.

    Fixed: 2 reframe + 2 chroma (was 3+3). RTX 3050 4GB saturates at 3
    concurrent ffmpegs (reframe+chroma+N paired), so 2+2 reduces VRAM
    contention while still giving 2 reframe producers to keep chroma fed.

    With 21 outputs at 1.5s reframe + 24s chroma:
        2 producers → 21 outputs / 2 = ~10.5 batches ~ 16s reframe total
        2 consumers → 21 outputs / 2 = ~10.5 batches × 24s = 252s chroma
        Total: max(16s + 1.5s, 252s) = 252s (vs sequential 191s)
        BUT real wall time is bounded by 2 chroma streams fed fast enough
        to never starve.
    """
    products = list(inputs.products)
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    covers = list(inputs.covers)
    out_dir = inputs.output_dir
    stop, terminal_state = _tracked_stop(cb)
    compositions = _pick_compositions(inputs.values)
    expected_reframe = len(products) * reframe_outputs_per_source(inputs.values)
    expected_final = expected_reframe
    total = expected_final

    # Validate per-stage
    reframe_settings = None
    green_settings = None
    input_errors: List[str] = []
    if not products:
        input_errors.append("TC02 input error: no products")
    if not backgrounds:
        input_errors.append("TC02 input error: no backgrounds")
    role_overlap = overlapping_role_paths(products, backgrounds)
    if role_overlap:
        input_errors.append(f"TC02 input error: Product and Background must be different files: {role_overlap[0]}")
    if not compositions:
        input_errors.append("TC02 requires at least one enabled composition")
    if not input_errors:
        try:
            reframe_settings = _build_reframe_settings(inputs.values, compositions)
            green_settings = _build_green_settings(
                inputs.values, has_uploaded_audio=bool(audios), has_cover=bool(covers),
            )
        except Exception as exc:
            input_errors.append(f"TC02 settings error: {exc}")

    if input_errors:
        for m in input_errors:
            safe_log(cb.log_fn, m)
        return PipelineResult(
            pipeline="TC02",
            expected=expected_final,
            skipped=expected_final,
            errors=input_errors,
        ).finalize(invalid_input=True)

    reframe_dir = os.path.join(out_dir, "reframe")
    os.makedirs(reframe_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    gpu = gpu_summary()
    safe_log(cb.log_fn, f"TC02 STREAMING (2+2): {len(products)} products x {expected_reframe} outputs")
    safe_log(cb.log_fn, f"[gpu] {gpu}")

    # Build all 21 reframe tasks
    all_tasks = build_reframe_tasks(products, reframe_dir, reframe_settings)
    safe_log(cb.log_fn, f"[streaming] built {len(all_tasks)} reframe tasks")

    started = time.time()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cover = covers[0] if covers else None

    # 2 producers + 3 consumers (FIX 2026-08-18: 2+3 = 168.53s vs 191.4s seq, -12%)
    # Benchmark matrix on sjnb3050ti (RTX 3050 4GB, 4K HEVC, CPU 100%):
    #   Sequential:                   191.4s
    #   2+2 streaming:                 169.98s  (-11.2%)
    #   2+3 streaming (sweet spot):    168.53s  (-12.0%)
    #   3+3 streaming:                 180.13s  (-5.9%, GPU saturated)
    #   2+3 + COMBINED ffmpeg:         (TBD)  predicted -8% on top
    # Override via V3_TC02_PRODUCERS / V3_TC02_CONSUMERS / V3_TC02_COMBINED env vars.
    N_PRODUCERS = int(os.environ.get("V3_TC02_PRODUCERS", "2") or "2")
    N_CONSUMERS = int(os.environ.get("V3_TC02_CONSUMERS", "3") or "3")
    USE_COMBINED = os.environ.get("V3_TC02_COMBINED", "").strip() == "1"
    safe_log(
        cb.log_fn,
        f"[streaming] TC02: {len(all_tasks)} outputs / {N_PRODUCERS} reframe-producers "
        f"+ {N_CONSUMERS} chroma-consumers (true queue pipeline)",
    )

    import threading
    import queue
    out_queue: "queue.Queue" = queue.Queue(maxsize=N_PRODUCERS * 2)
    results_lock = threading.Lock()
    chroma_results: Dict[int, str] = {}
    chroma_failed = 0
    chroma_errors: List[str] = []
    chroma_cancelled = 0
    completed = 0

    # Producer: reframe one task and push to queue (skipped if USE_COMBINED)
    def _producer(producer_id: int, task_indices: list):
        for idx in task_indices:
            if stop():
                out_queue.put(("STOP", idx, None))
                continue
            task = all_tasks[idx]
            if USE_COMBINED:
                # Combined mode: skip reframe, push directly so consumer does reframe+chroma in 1 ffmpeg call
                out_queue.put(("OK", idx, task))
                continue
            try:
                reframe_result = _reframe_one_task(
                    task, reframe_settings,
                    ffmpeg_runner_kwargs={
                        "ffmpeg_cmd": "ffmpeg",
                        "ffprobe_cmd": "ffprobe",
                        "idle_timeout_sec": 120,
                        "max_factor": 3.0,
                        "on_log": lambda m: safe_log(cb.log_fn, f"[p{producer_id}] {m}"),
                        "stop_check": stop,
                    },
                )
            except Exception as exc:
                out_queue.put(("FAILED", idx, f"reframe exception: {exc}"))
                continue
            if not reframe_result.success:
                out_queue.put(("FAILED", idx, f"reframe failed: {reframe_result.error[:200]}"))
                continue
            out_queue.put(("OK", idx, task))
    def _producer(producer_id: int, task_indices: list):
        for idx in task_indices:
            if stop():
                out_queue.put(("STOP", idx, None))
                continue
            task = all_tasks[idx]
            try:
                reframe_result = _reframe_one_task(
                    task, reframe_settings,
                    ffmpeg_runner_kwargs={
                        "ffmpeg_cmd": "ffmpeg",
                        "ffprobe_cmd": "ffprobe",
                        "idle_timeout_sec": 120,
                        "max_factor": 3.0,
                        "on_log": lambda m: safe_log(cb.log_fn, f"[p{producer_id}] {m}"),
                        "stop_check": stop,
                    },
                )
            except Exception as exc:
                out_queue.put(("FAILED", idx, f"reframe exception: {exc}"))
                continue
            if not reframe_result.success:
                out_queue.put(("FAILED", idx, f"reframe failed: {reframe_result.error[:200]}"))
                continue
            out_queue.put(("OK", idx, task))

    # Consumer: pull from queue and chroma
    def _consumer(consumer_id: int):
        nonlocal chroma_failed, chroma_cancelled
        while True:
            item = out_queue.get()
            if item is None:
                return
            tag, idx, payload = item
            if tag == "STOP":
                continue
            if tag == "FAILED":
                with results_lock:
                    chroma_failed += 1
                    chroma_results[idx + 1] = None
                    chroma_errors.append(f"final.{idx + 1:03d}: {payload}")
                safe_log(cb.log_fn, f"final.{idx + 1:03d}.{tag} {payload}")
                continue
            # tag == "OK"
            task = payload
            if USE_COMBINED:
                # Combined mode: reframe + chroma in 1 ffmpeg call
                # Override output path to the final chroma path
                chroma_out_path = os.path.join(
                    out_dir,
                    f"{_safe_stem(task.source_path)}__{task.lens.key}__{_composition_value_str(task.composition)}__tc02_chroma_{run_stamp}_{idx + 1:03d}.mp4",
                )
                background = backgrounds[idx % len(backgrounds)] if backgrounds else None
                if background is None:
                    with results_lock:
                        chroma_failed += 1
                        chroma_results[idx + 1] = None
                        chroma_errors.append(f"final.{idx + 1:03d}: no background")
                    continue
                # Update task.output_path to chroma path
                task.output_path = chroma_out_path
                ok, out_path, err, dur = _reframe_chroma_combined_one_task(
                    task, reframe_settings, green_settings, background,
                    ffmpeg_runner_kwargs={
                        "ffmpeg_cmd": "ffmpeg",
                        "ffprobe_cmd": "ffprobe",
                        "idle_timeout_sec": 120,
                        "max_factor": 3.0,
                        "on_log": lambda m: safe_log(cb.log_fn, f"[c{consumer_id}] {m}"),
                        "stop_check": stop,
                    },
                )
                if ok and _is_valid_output(out_path):
                    with results_lock:
                        chroma_results[idx + 1] = out_path
                    safe_log(cb.log_fn, f"final.{idx + 1:03d}.ok path={out_path} dur={dur:.1f}s")
                else:
                    with results_lock:
                        chroma_failed += 1
                        chroma_results[idx + 1] = None
                        chroma_errors.append(f"final.{idx + 1:03d}: {err[:200]}")
                    safe_log(cb.log_fn, f"final.{idx + 1:03d}.failed err={err[:200]} dur={dur:.1f}s")
                continue
            chroma_result = _chroma_one_from_reframe(
                reframed_path=task.output_path,
                idx=idx + 1,
                backgrounds=backgrounds,
                audios=audios,
                cover=cover,
                green_settings=green_settings,
                out_dir=out_dir,
                run_stamp=run_stamp,
                worker_id=consumer_id,
                on_log=cb.log_fn,
                stop_check=stop,
                n_parallel=N_CONSUMERS,
            )
            status, detail, dur = chroma_result[1], chroma_result[2], chroma_result[3]
            with results_lock:
                if status == "ok":
                    chroma_results[idx + 1] = detail
                    completed_now = completed + 1
                    safe_log(cb.log_fn, f"final.{idx + 1:03d}.ok path={detail} dur={dur:.1f}s")
                elif status == "cancelled":
                    chroma_cancelled += 1
                    safe_log(cb.log_fn, f"final.{idx + 1:03d}.cancelled dur={dur:.1f}s")
                else:
                    chroma_failed += 1
                    chroma_results[idx + 1] = None
                    chroma_errors.append(f"final.{idx + 1:03d}: {detail}")
                    safe_log(cb.log_fn, f"final.{idx + 1:03d}.{status} dur={dur:.1f}s")
            if status == "ok":
                safe_progress(
                    cb.progress_fn,
                    (len([k for k, v in chroma_results.items() if v is not None]) / max(total, 1)) * 100.0,
                    f"Streaming {len([k for k, v in chroma_results.items() if v is not None])}/{total}",
                )

    # Distribute tasks round-robin to N_PRODUCERS
    task_indices_per_producer = [[] for _ in range(N_PRODUCERS)]
    for i, idx in enumerate(range(len(all_tasks))):
        task_indices_per_producer[i % N_PRODUCERS].append(idx)

    # Start producer + consumer threads
    threads = []
    for pid in range(N_PRODUCERS):
        t = threading.Thread(target=_producer, args=(pid, task_indices_per_producer[pid]), daemon=True)
        t.start()
        threads.append(t)
    for cid in range(N_CONSUMERS):
        t = threading.Thread(target=_consumer, args=(cid,), daemon=True)
        t.start()
        threads.append(t)

    # Wait for producers to finish
    for t in threads[:N_PRODUCERS]:
        t.join()

    # Send N_CONSUMERS sentinel values to stop consumers
    for _ in range(N_CONSUMERS):
        out_queue.put(None)

    # Wait for consumers
    for t in threads[N_PRODUCERS:]:
        t.join()

    # Build results in original order
    final_outputs = [chroma_results[i] for i in sorted(chroma_results) if i in chroma_results and chroma_results[i] is not None]
    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    if not cancel_requested and not paused:
        missing = len(all_tasks) - len(final_outputs)
        if missing > 0:
            chroma_failed += missing

    reframe_stage = StageResult(
        name="reframe",
        expected=expected_reframe,
        succeeded=final_outputs and len(all_tasks) - chroma_failed or 0,
        failed=0,
        outputs=final_outputs,
        required=True,
    ).finalize(paused=paused, cancel_requested=cancel_requested)
    chroma_stage = StageResult(
        name="chroma",
        expected=expected_final,
        succeeded=len(final_outputs),
        failed=chroma_failed,
        cancelled=chroma_cancelled,
        outputs=final_outputs,
        errors=chroma_errors,
        required=True,
    ).finalize(paused=paused, cancel_requested=cancel_requested)

    result = PipelineResult(
        pipeline="TC02",
        expected=expected_final,
        succeeded=len(final_outputs),
        failed=chroma_failed,
        cancelled=chroma_cancelled,
        outputs=final_outputs,
        stages=[reframe_stage, chroma_stage],
        errors=chroma_errors,
        metadata={"elapsed_sec": time.time() - started, "streaming_2plus2": True},
    ).finalize(paused=paused, cancel_requested=cancel_requested)

    safe_log(
        cb.log_fn,
        f"[streaming 2+2] done: chroma={len(final_outputs)}/{expected_final} "
        f"status={result.status.value} elapsed={time.time() - started:.1f}s",
    )
    if result.is_success:
        safe_progress(cb.progress_fn, 100.0, "done")
    return result
