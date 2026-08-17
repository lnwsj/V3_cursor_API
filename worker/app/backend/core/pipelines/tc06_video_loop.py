"""TC06: TC01-compatible chroma followed by audio-master assembly.

The filename is retained for import compatibility.  The active TC06 contract
is no longer the historical source-only video loop.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, List, Sequence

from ..audio_master import render_audio_master
from ..contract import (
    TC06_VIDEO_DEFAULTS,
    green_settings_for,
    validate_tc06_allow_clip_reuse,
)
from ..tc06_products import ProductFolderLayout, final_output_path, resolve_product_folders
from ._common import (
    PipelineCallbacks,
    PipelineInputs,
    PipelineResult,
    PipelineStatus,
    StageResult,
    combined_stop_check,
    safe_file,
    safe_log,
    safe_progress,
)
from .tc01_chroma import render as render_tc01


_PIPELINE_NAME = "TC06"


def _valid_output(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def tc06_layout_manifest(
    layouts: Sequence[ProductFolderLayout],
) -> List[dict[str, Any]]:
    """V1.0.0.20: Convert a list of ProductFolderLayout into a JSON-serializable
    manifest. Used by the packaged-evidence controller to compute a SHA256
    fingerprint so it can detect layout drift between captured and current
    state (e.g. a file added to a product folder after the spec was created).

    The shape is intentionally simple: one dict per folder with the resolved
    file paths under each role. Adding a file to a folder changes the SHA256
    so downstream tests can assert that the controller reacted to the drift.
    """
    return [
        {
            "root": layout.root,
            "products": list(layout.products),
            "backgrounds": list(layout.backgrounds),
            "audios": list(layout.audios),
        }
        for layout in layouts
    ]


def _terminal_flags(cb: PipelineCallbacks) -> tuple[bool, bool]:
    paused = bool(cb.pause_check and cb.pause_check())
    cancelled = False if paused else bool(cb.stop_check())
    return paused, cancelled


def _invalid_result(expected: int, errors: List[str]) -> PipelineResult:
    count = max(1, expected)
    chroma = StageResult(
        name="chroma",
        expected=count,
        failed=count,
        errors=list(errors),
    ).finalize(invalid_input=True, validate_outputs=False)
    assemble = StageResult(
        name="audio_master",
        expected=count,
        failed=count,
        errors=list(errors),
    ).finalize(invalid_input=True, validate_outputs=False)
    return PipelineResult(
        pipeline=_PIPELINE_NAME,
        expected=count,
        failed=count,
        stages=[chroma, assemble],
        errors=list(errors),
    ).finalize(invalid_input=True, validate_outputs=False)


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    started = time.time()
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    selected_roots = list(inputs.product_roots)
    layouts, discovery_errors = resolve_product_folders(selected_roots)

    try:
        allow_reuse = validate_tc06_allow_clip_reuse(inputs.values)
        settings = green_settings_for(
            inputs.values,
            has_audio=False,
            has_cover=False,
            defaults=TC06_VIDEO_DEFAULTS,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        discovery_errors.append(f"TC06 settings error: {exc}")

    expected_chroma = sum(len(layout.products) for layout in layouts)
    expected_finals = sum(len(layout.audios) for layout in layouts)
    if discovery_errors or not layouts:
        for error in discovery_errors:
            safe_log(cb.log_fn, error)
        return _invalid_result(expected_finals or expected_chroma, discovery_errors)

    stop = combined_stop_check(cb.stop_check, cb.pause_check)
    safe_log(
        cb.log_fn,
        f"TC06 folders={len(layouts)} chroma={expected_chroma} finals={expected_finals} "
        f"reuse={allow_reuse}",
    )

    chroma_outputs: List[str] = []
    chroma_errors: List[str] = []
    chroma_failed = 0
    final_outputs: List[str] = []
    final_errors: List[str] = []
    final_failed = 0
    stopped = False

    processed_chroma = 0
    processed_finals = 0
    for folder_index, layout in enumerate(layouts, 1):
        if stop():
            stopped = True
            break
        safe_log(
            cb.log_fn,
            f"TC06 product-folder {folder_index}/{len(layouts)}: {layout.root}",
        )

        folder_chroma_base = processed_chroma

        def chroma_progress(pct: float, info: str) -> None:
            units = folder_chroma_base + (float(pct) / 100.0) * len(layout.products)
            overall = units / max(1, expected_chroma) * 50.0
            safe_progress(cb.progress_fn, overall, f"Chroma {folder_index}/{len(layouts)}: {info}")

        chroma_result = render_tc01(
            PipelineInputs(
                output_dir=layout.chroma_dir,
                values=dict(inputs.values),
                products=list(layout.products),
                backgrounds=list(layout.backgrounds),
                audios=[],
                covers=[],
                # FIX (B-03, 2026-07-31): propagate outer run_stamp so chroma
                # intermediates share timestamp with audio-master finals.
                run_stamp=run_stamp,
            ),
            PipelineCallbacks(
                log_fn=cb.log_fn,
                stop_check=cb.stop_check,
                progress_fn=chroma_progress,
                file_fn=cb.file_fn,
                pause_check=cb.pause_check,
            ),
        )
        processed_chroma += len(layout.products)
        valid_chroma = [path for path in chroma_result.outputs if _valid_output(path)]
        chroma_outputs.extend(valid_chroma)
        if not chroma_result.is_success:
            missing = max(0, len(layout.products) - len(valid_chroma))
            chroma_failed += missing
            detail = "; ".join(chroma_result.all_errors[:3]) or chroma_result.status.value
            chroma_errors.append(f"{layout.root}: TC01 chroma failed: {detail}")
            final_failed += len(layout.audios)
            final_errors.append(
                f"{layout.root}: audio-master skipped because required TC01 chroma stage failed"
            )
            if chroma_result.status in {PipelineStatus.CANCELLED, PipelineStatus.PAUSED}:
                stopped = True
                break
            continue

        for audio_index, audio in enumerate(layout.audios, 1):
            if stop():
                stopped = True
                break
            out_path = final_output_path(layout, audio, audio_index, run_stamp)
            safe_file(cb.file_fn, audio)

            # FIX (B-16, 2026-07-31): bump processed_finals BEFORE the render
            # so the progress message and overall-percentage reflect the
            # *current* audio, not the previous one. Previously the message
            # said "Audio N/M" while audio N-1 was still rendering.
            processed_finals += 1
            current_final_index = processed_finals

            def audio_progress(pct: float, info: str) -> None:
                units = (current_final_index - 1) + float(pct) / 100.0
                overall = 50.0 + units / max(1, expected_finals) * 50.0
                safe_progress(
                    cb.progress_fn,
                    overall,
                    f"Audio {current_final_index}/{expected_finals}: {info}",
                )

            try:
                engine_result = render_audio_master(
                    valid_chroma,
                    audio,
                    out_path,
                    settings,
                    allow_reuse=allow_reuse,
                    on_log=cb.log_fn,
                    on_progress=audio_progress,
                    stop_check=stop,
                )
            except Exception as exc:
                engine_result = None
                final_errors.append(f"{audio}: audio-master exception: {exc}")
            if engine_result is not None and engine_result.success and _valid_output(out_path):
                final_outputs.append(out_path)
                safe_log(cb.log_fn, f"TC06 final ok: {out_path}")
            elif engine_result is not None and engine_result.cancelled:
                stopped = True
                break
            else:
                final_failed += 1
                detail = (
                    str(getattr(engine_result, "error", "") or "missing/zero output")
                    if engine_result is not None
                    else "audio-master exception"
                )
                if not any(str(audio) in item for item in final_errors[-1:]):
                    final_errors.append(f"{audio}: {detail}")
                safe_log(cb.log_fn, f"TC06 final fail: {audio}: {detail}")
        if stopped:
            break

    paused, cancel_requested = _terminal_flags(cb)
    cancelled = stopped or paused or cancel_requested
    chroma_accounted = len(chroma_outputs) + chroma_failed
    chroma_cancelled = max(0, expected_chroma - chroma_accounted) if cancelled else 0
    if not cancelled:
        chroma_failed += max(0, expected_chroma - chroma_accounted)

    final_accounted = len(final_outputs) + final_failed
    final_cancelled = max(0, expected_finals - final_accounted) if cancelled else 0
    if not cancelled:
        final_failed += max(0, expected_finals - final_accounted)

    chroma_stage = StageResult(
        name="chroma",
        expected=expected_chroma,
        succeeded=len(chroma_outputs),
        failed=chroma_failed,
        cancelled=chroma_cancelled,
        outputs=chroma_outputs,
        errors=chroma_errors,
        metadata={"engine": "TC01", "product_folders": len(layouts)},
    ).finalize(paused=paused, cancel_requested=cancelled and not paused)
    audio_stage = StageResult(
        name="audio_master",
        expected=expected_finals,
        succeeded=len(final_outputs),
        failed=final_failed,
        cancelled=final_cancelled,
        outputs=final_outputs,
        errors=final_errors,
        metadata={"allow_clip_reuse": allow_reuse},
    ).finalize(paused=paused, cancel_requested=cancelled and not paused)

    result = PipelineResult(
        pipeline=_PIPELINE_NAME,
        expected=expected_finals,
        succeeded=len(final_outputs),
        failed=final_failed,
        cancelled=final_cancelled,
        outputs=final_outputs,
        stages=[chroma_stage, audio_stage],
        errors=[*chroma_errors, *final_errors],
        metadata={
            "elapsed_sec": time.time() - started,
            "product_folders": [layout.root for layout in layouts],
            "allow_clip_reuse": allow_reuse,
            "run_stamp": run_stamp,
            "settings": asdict(settings),
        },
    ).finalize(paused=paused, cancel_requested=cancelled and not paused)
    safe_progress(cb.progress_fn, 100.0 if result.is_success else 0.0, result.status.value)
    safe_log(
        cb.log_fn,
        f"TC06 summary final={result.completed_count}/{expected_finals} status={result.status.value}",
    )
    return result
