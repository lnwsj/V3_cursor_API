"""
TC01 single chroma-key pipeline.

The GUI and CLI both call this module.  Checkpoint schema v2 binds each output
to a deterministic task so resume never guesses from list position.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..contract import green_settings_for, TC01_VIDEO_DEFAULTS  # noqa: F401
from ..gpu_detector import gpu_summary
from ..green_render import GreenSettings, render_green
from ..media_probe import (
    MediaProbeCancelled,
    invalid_audio_stream_paths,
    invalid_video_stream_paths,
)
from ..render_checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    archive_checkpoint,
    build_job_fingerprint,
    build_task_id,
    clear_checkpoint,
    file_signature,
    load_checkpoint_document,
    normalized_path,
    save_checkpoint_document,
    set_paused,
    stable_json_hash,
    validate_completed_output,
)

from ._common import (
    PipelineCallbacks,
    PipelineInputs,
    PipelineResult,
    apply_seed,
    invalid_video_inputs,
    overlapping_role_paths,
    normalize_run_seed,
    resolve_run_seed,
    safe_file,
    safe_log,
    safe_progress,
    shuffle_pool,
)


TC01_DEFAULTS = TC01_VIDEO_DEFAULTS
_PIPELINE_NAME = "TC01"


def _build_green_settings(
    values: dict,
    has_audio: bool,
    has_cover: bool = False,
) -> GreenSettings:
    return green_settings_for(
        values,
        has_audio=has_audio,
        has_cover=has_cover,
        defaults=TC01_VIDEO_DEFAULTS,
    )


def _is_valid_output(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _safe_requested(callback: Any) -> bool:
    if callback is None:
        return False
    try:
        return bool(callback())
    except Exception:
        return False


def _tracked_stop(cb: PipelineCallbacks) -> Tuple[Any, Dict[str, bool]]:
    """Track interruption only when the active loop/engine observes it."""

    state = {"paused": False, "cancelled": False}

    def stop() -> bool:
        paused = _safe_requested(cb.pause_check)
        cancelled = _safe_requested(cb.stop_check)
        state["paused"] = state["paused"] or paused
        state["cancelled"] = state["cancelled"] or cancelled
        return paused or cancelled

    return stop, state


def _terminal_flags(cb: PipelineCallbacks, state: Mapping[str, bool]) -> Tuple[bool, bool]:
    paused = bool(state["paused"]) or _safe_requested(cb.pause_check)
    return paused, bool(state["cancelled"])


def _input_groups(inputs: PipelineInputs) -> Dict[str, List[str]]:
    return {
        "products": list(inputs.products),
        "backgrounds": list(inputs.backgrounds),
        "audios": list(inputs.audios),
        "covers": list(inputs.covers),
    }


def _input_metadata(groups: Mapping[str, List[str]]) -> Dict[str, List[Dict[str, Any]]]:
    return {
        group: [file_signature(path) for path in paths]
        for group, paths in sorted(groups.items())
    }


def _requested_seed(values: Mapping[str, Any]) -> Optional[int]:
    return normalize_run_seed(values.get("seed", 0))


def _seed_from_values(values: Mapping[str, Any]) -> int:
    return resolve_run_seed(values.get("seed", 0))


def _safe_output_stem(path: str) -> str:
    stem = Path(path).stem.strip()
    return stem or "product"


def _build_tasks(
    *,
    groups: Mapping[str, List[str]],
    settings_hash: str,
    seed: int,
    run_stamp: str,
    out_dir: str,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Return persisted task records plus current runtime path assignments."""

    products = list(groups["products"])
    backgrounds = list(groups["backgrounds"])
    audios = list(groups["audios"])
    covers = list(groups["covers"])

    rng = apply_seed(seed)
    bg_pool = shuffle_pool(backgrounds, rng)
    audio_pool = shuffle_pool(audios, rng)
    cover = covers[0] if covers else None

    records: Dict[str, Dict[str, Any]] = {}
    runtime: Dict[str, Dict[str, Any]] = {}
    for slot, product in enumerate(products, 1):
        source_signature = file_signature(product)
        task_id = build_task_id(
            pipeline=_PIPELINE_NAME,
            slot=slot,
            source_signature=source_signature,
            settings_hash=settings_hash,
        )
        background = bg_pool[(slot - 1) % len(bg_pool)]
        audio = audio_pool[(slot - 1) % len(audio_pool)] if audio_pool else None
        out_name = (
            f"{_safe_output_stem(product)}_single_{run_stamp}_{slot:03d}.mp4"
        )
        out_path = normalized_path(os.path.join(out_dir, out_name))
        assignment = {
            "background": normalized_path(background),
            "audio": normalized_path(audio) if audio else None,
            "cover": normalized_path(cover) if cover else None,
        }
        records[task_id] = {
            "task_id": task_id,
            "slot": slot,
            "source_signature": source_signature,
            "assignment": assignment,
            "output_path": out_path,
            "status": "pending",
            "output_signature": None,
            "error": None,
        }
        runtime[task_id] = {
            "product": product,
            "background": background,
            "audio": audio,
            "cover": cover,
            "output_path": out_path,
            "slot": slot,
        }
    return records, runtime


def _new_checkpoint(
    *,
    groups: Mapping[str, List[str]],
    effective_settings: Mapping[str, Any],
    job_fingerprint: str,
    settings_hash: str,
    seed: int,
    run_stamp: str,
    tasks: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "pipeline": _PIPELINE_NAME,
        "job_fingerprint": job_fingerprint,
        "settings_hash": settings_hash,
        "settings": dict(effective_settings),
        "inputs": _input_metadata(groups),
        "seed": seed,
        "run_stamp": run_stamp,
        "tasks": {task_id: dict(task) for task_id, task in tasks.items()},
        "created_at": now,
        "updated_at": now,
    }


def _classify_mismatch(
    stored_task: Mapping[str, Any],
    expected_task: Mapping[str, Any],
) -> str:
    """FIX (B-04, 2026-07-31): แยกประเภท mismatch เพื่อ log ที่ operator อ่านเข้าใจ.

    Returns one of:
        'match'        — every identity field equal.
        'path-only'    — only output_path differs (output folder moved).
        'source-changed' — source_signature differs (input file changed).
        'other'        — assignment / slot / task_id / multiple fields differ.
    """
    identity_fields = ("task_id", "slot", "source_signature",
                       "assignment", "output_path")
    diffs = [
        f for f in identity_fields
        if stored_task.get(f) != expected_task.get(f)
    ]
    if not diffs:
        return "match"
    if diffs == ["output_path"]:
        return "path-only"
    if "source_signature" in diffs:
        return "source-changed"
    return "other"


def _task_map_matches(
    stored: Mapping[str, Any],
    expected: Mapping[str, Mapping[str, Any]],
) -> bool:
    if set(stored) != set(expected):
        return False
    identity_fields = (
        "task_id",
        "slot",
        "source_signature",
        "assignment",
        "output_path",
    )
    for task_id, expected_task in expected.items():
        stored_task = stored.get(task_id)
        if not isinstance(stored_task, Mapping):
            return False
        for field in identity_fields:
            if stored_task.get(field) != expected_task.get(field):
                return False
    return True


def _archive_unusable(out_dir: str, kind: str, reason: str, cb: PipelineCallbacks) -> None:
    label = reason or kind
    try:
        archived = archive_checkpoint(out_dir, kind)
    except Exception as exc:
        safe_log(cb.log_fn, f"checkpoint archive failed kind={kind} error={exc}")
        return
    safe_log(
        cb.log_fn,
        f"checkpoint ignored kind={kind} reason={label} archived={archived or 'none'}",
    )


def _persist_checkpoint(
    out_dir: str,
    checkpoint: Dict[str, Any],
    cb: PipelineCallbacks,
) -> bool:
    checkpoint["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        save_checkpoint_document(out_dir, checkpoint)
    except Exception as exc:
        safe_log(cb.log_fn, f"checkpoint write warning: {exc}")
        return False
    return True


def _load_or_create_checkpoint(
    *,
    inputs: PipelineInputs,
    effective_settings: Mapping[str, Any],
    cb: PipelineCallbacks,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], bool]:
    groups = _input_groups(inputs)
    settings_hash = stable_json_hash(effective_settings)
    job_fingerprint = build_job_fingerprint(
        pipeline=_PIPELINE_NAME,
        inputs=groups,
        settings=effective_settings,
    )
    loaded = load_checkpoint_document(inputs.output_dir)

    if loaded.kind == "v2" and isinstance(loaded.data, dict):
        data = loaded.data
        compatible = (
            data.get("pipeline") == _PIPELINE_NAME
            and data.get("job_fingerprint") == job_fingerprint
            and data.get("settings_hash") == settings_hash
            and isinstance(data.get("seed"), int)
            and not isinstance(data.get("seed"), bool)
            and isinstance(data.get("run_stamp"), str)
            and bool(data.get("run_stamp"))
            and isinstance(data.get("tasks"), dict)
        )
        if compatible:
            seed = int(data["seed"])
            run_stamp = str(data["run_stamp"])
            expected_tasks, runtime = _build_tasks(
                groups=groups,
                settings_hash=settings_hash,
                seed=seed,
                run_stamp=run_stamp,
                out_dir=inputs.output_dir,
            )
            if _task_map_matches(data["tasks"], expected_tasks):
                return data, runtime, True
            # FIX (B-04, 2026-07-31): classify the mismatch so operators see
            # whether the output folder moved ("path-only"), the source file
            # changed ("source-changed"), or something deeper is wrong ("other").
            # The previous generic "task-map-mismatch" message was misleading.
            reasons: list = []
            for task_id, expected_task in expected_tasks.items():
                stored_task = data["tasks"].get(task_id)
                if not isinstance(stored_task, Mapping):
                    continue
                reasons.append(
                    f"{task_id}: {_classify_mismatch(stored_task, expected_task)}"
                )
            detail = ", ".join(reasons[:5])
            if len(reasons) > 5:
                detail += f" (+{len(reasons) - 5} more)"
            _archive_unusable(
                inputs.output_dir,
                "task-map-mismatch",
                f"stored task mapping does not match current mapping [{detail}]",
                cb,
            )
        else:
            _archive_unusable(
                inputs.output_dir,
                "fingerprint-mismatch",
                "pipeline/input/settings fingerprint changed",
                cb,
            )
    elif loaded.kind not in ("missing",):
        _archive_unusable(inputs.output_dir, loaded.kind, loaded.reason, cb)

    seed = _seed_from_values(inputs.values)
    # FIX (B-03, 2026-07-31): TC06 mints one run_stamp and passes it down
    # so chroma intermediates share the timestamp with the audio-master
    # final. If empty (TC01/03/04 direct callers), mint our own.
    run_stamp = inputs.run_stamp or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tasks, runtime = _build_tasks(
        groups=groups,
        settings_hash=settings_hash,
        seed=seed,
        run_stamp=run_stamp,
        out_dir=inputs.output_dir,
    )
    checkpoint = _new_checkpoint(
        groups=groups,
        effective_settings=effective_settings,
        job_fingerprint=job_fingerprint,
        settings_hash=settings_hash,
        seed=seed,
        run_stamp=run_stamp,
        tasks=tasks,
    )
    return checkpoint, runtime, False


def render(inputs: PipelineInputs, cb: PipelineCallbacks) -> PipelineResult:
    products = list(inputs.products)
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    covers = list(inputs.covers)
    out_dir = inputs.output_dir
    total = len(products)
    stop, terminal_state = _tracked_stop(cb)

    input_errors: List[str] = []
    if not products:
        input_errors.append("TC01 input error: no products")
    if not backgrounds:
        input_errors.append("TC01 input error: no backgrounds")
    role_overlap = overlapping_role_paths(products, backgrounds)
    if role_overlap:
        input_errors.append(
            f"TC01 input error: Product and Background must be different files: {role_overlap[0]}"
        )
    invalid_products = invalid_video_inputs(products)
    if invalid_products:
        input_errors.append(f"TC01 input error: Product must be a video: {invalid_products[0]}")
    settings = None
    effective_settings: Dict[str, Any] = {}

    # Settings must fail before stream probing/checkpoint I/O/FFmpeg work.
    if not input_errors:
        try:
            settings = _build_green_settings(
                inputs.values,
                has_audio=bool(audios),
                has_cover=bool(covers),
            )
            effective_settings = asdict(settings)
            # A fixed seed changes deterministic pool assignment.
            effective_settings["requested_seed"] = _requested_seed(inputs.values)
        except Exception as exc:
            input_errors.append(f"TC01 settings error: {exc}")

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
                        f"TC01 input error: {label} has no readable video stream: "
                        f"{invalid_streams[0]}"
                    )
                    break
            invalid_audio = invalid_audio_stream_paths(
                audios,
                stop_check=stop,
            )
            if not input_errors and invalid_audio:
                input_errors.append(
                    f"TC01 input error: Audio has no readable audio stream: {invalid_audio[0]}"
                )
        except MediaProbeCancelled:
            paused, cancel_requested = _terminal_flags(cb, terminal_state)
            expected = max(1, total)
            safe_log(cb.log_fn, "TC01 media preflight interrupted")
            return PipelineResult(
                pipeline=_PIPELINE_NAME,
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
            pipeline=_PIPELINE_NAME,
            expected=total,
            skipped=total,
            errors=input_errors,
        ).finalize(invalid_input=True)

    if settings is None:
        # settings must be initialised by green_settings_for() above; if it is
        # None here the pipeline is in an inconsistent state. Fail closed with a
        # structured INVALID_INPUT result instead of relying on `assert` (which
        # is stripped under `python -O` / PyInstaller optimisation).
        message = "TC01 green settings resolved to None (should not happen)"
        safe_log(cb.log_fn, message)
        return PipelineResult(
            pipeline=_PIPELINE_NAME,
            expected=total,
            skipped=total,
            errors=[message],
        ).finalize(invalid_input=True)

    try:
        os.makedirs(out_dir, exist_ok=True)
        checkpoint, runtime_tasks, resumed_document = _load_or_create_checkpoint(
            inputs=inputs,
            effective_settings=effective_settings,
            cb=cb,
        )
    except Exception as exc:
        message = f"TC01 checkpoint initialization error: {exc}"
        safe_log(cb.log_fn, message)
        return PipelineResult(
            pipeline=_PIPELINE_NAME,
            expected=total,
            failed=total,
            errors=[message],
        ).finalize()

    checkpoint_tasks = checkpoint["tasks"]
    ordered_ids = sorted(
        runtime_tasks,
        key=lambda task_id: int(runtime_tasks[task_id]["slot"]),
    )
    resumed_outputs: List[str] = []
    pending_ids: List[str] = []
    checkpoint_changed = not resumed_document
    for task_id in ordered_ids:
        task = checkpoint_tasks[task_id]
        output_path = str(task["output_path"])
        if (
            task.get("status") == "completed"
            and validate_completed_output(
                output_path,
                task.get("output_signature") or {},
            )
        ):
            resumed_outputs.append(output_path)
            continue
        if task.get("status") == "completed":
            safe_log(
                cb.log_fn,
                f"resume invalid task={task_id[:12]} output={output_path}; rerendering",
            )
        task["status"] = "pending"
        task["output_signature"] = None
        task["error"] = None
        pending_ids.append(task_id)
        checkpoint_changed = True

    validated_resumed = len(resumed_outputs)
    if validated_resumed:
        safe_log(
            cb.log_fn,
            f"resume.validated={validated_resumed} pending={len(pending_ids)} "
            f"seed={checkpoint['seed']} run_stamp={checkpoint['run_stamp']}",
        )
        safe_progress(
            cb.progress_fn,
            validated_resumed / total * 100.0,
            f"Resumed {validated_resumed}/{total}",
        )
    if checkpoint_changed:
        _persist_checkpoint(out_dir, checkpoint, cb)

    try:
        gpu = gpu_summary()
    except Exception:
        gpu = {}
    safe_log(
        cb.log_fn,
        f"render {total} products -> {out_dir} "
        f"({settings.width}x{settings.height} @ {settings.fps}fps, {settings.bitrate})",
    )
    safe_log(cb.log_fn, f"[gpu] {gpu}")

    started = time.time()
    produced_outputs: List[str] = []
    errors: List[str] = []
    failed = 0
    engine_cancelled = False

    for task_id in pending_ids:
        runtime = runtime_tasks[task_id]
        task = checkpoint_tasks[task_id]
        slot = int(runtime["slot"])
        if stop():
            safe_log(cb.log_fn, f"stopped before product {slot}/{total}")
            break

        product = str(runtime["product"])
        out_path = str(runtime["output_path"])
        safe_log(cb.log_fn, f"[{slot}/{total}] render -> {os.path.basename(out_path)}")
        safe_file(cb.file_fn, product)

        def _on_core_progress(progress, _slot=slot, _total=total):
            pct = getattr(
                progress,
                "pct",
                float(progress) if isinstance(progress, (int, float)) else 0.0,
            )
            overall = ((_slot - 1) + pct / 100.0) / _total * 100.0
            safe_progress(cb.progress_fn, overall, f"Task {_slot}/{_total} ({pct:.0f}%)")

        try:
            engine_result = render_green(
                cover=runtime["cover"],
                product=product,
                background=str(runtime["background"]),
                audio=runtime["audio"],
                out_path=out_path,
                settings=settings,
                on_log=cb.log_fn,
                on_progress=_on_core_progress,
                stop_check=stop,
                tc_label=_PIPELINE_NAME,
            )
            if bool(getattr(engine_result, "success", False)) and _is_valid_output(out_path):
                signature = file_signature(out_path, include_sha256=True)
                task["status"] = "completed"
                task["output_signature"] = signature
                task["error"] = None
                produced_outputs.append(out_path)
                safe_log(
                    cb.log_fn,
                    f"output.{slot:03d}.ok path={out_path} bytes={signature['size']}",
                )
            elif bool(getattr(engine_result, "cancelled", False)):
                safe_log(cb.log_fn, f"output.{slot:03d}.cancelled")
                engine_cancelled = True
                break
            else:
                failed += 1
                detail = str(
                    getattr(engine_result, "error", "") or "missing/zero output"
                )
                task["status"] = "failed"
                task["output_signature"] = None
                task["error"] = detail
                errors.append(f"output.{slot:03d}: {detail}")
                safe_log(cb.log_fn, f"output.{slot:03d}.fail error={detail[:200]}")
        except Exception as exc:
            failed += 1
            task["status"] = "failed"
            task["output_signature"] = None
            task["error"] = str(exc)
            errors.append(f"output.{slot:03d}: {exc}")
            safe_log(cb.log_fn, f"output.{slot:03d}.exception error={exc}")
        finally:
            _persist_checkpoint(out_dir, checkpoint, cb)

    accounted = validated_resumed + len(produced_outputs) + failed
    remaining = max(0, total - accounted)
    completed_all = remaining == 0 and failed == 0 and not engine_cancelled
    paused, cancel_requested = _terminal_flags(cb, terminal_state)
    cancel_requested = cancel_requested or engine_cancelled
    if completed_all:
        # A pause requested after the last valid output is already complete is
        # terminally equivalent to a late Stop: there is no remaining work to
        # preserve, so the completed run must stay SUCCEEDED.
        paused = False
        cancel_requested = False
    cancelled = remaining if paused or cancel_requested else 0
    if not paused and not cancel_requested:
        failed += remaining

    all_outputs = [
        str(checkpoint_tasks[task_id]["output_path"])
        for task_id in ordered_ids
        if checkpoint_tasks[task_id].get("status") == "completed"
        and _is_valid_output(str(checkpoint_tasks[task_id]["output_path"]))
    ]
    result = PipelineResult(
        pipeline=_PIPELINE_NAME,
        expected=total,
        succeeded=len(produced_outputs),
        failed=failed,
        cancelled=cancelled,
        validated_resumed=validated_resumed,
        outputs=all_outputs,
        errors=errors,
        metadata={
            "elapsed_sec": time.time() - started,
            "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
            "job_fingerprint": checkpoint["job_fingerprint"],
            "seed": checkpoint["seed"],
            "run_stamp": checkpoint["run_stamp"],
        },
    ).finalize(paused=paused, cancel_requested=cancel_requested)

    safe_log(
        cb.log_fn,
        f"summary input.products={total} output.ok={result.completed_count} "
        f"output.fail={failed} status={result.status.value} "
        f"elapsed={time.time() - started:.1f}s",
    )

    if result.is_success:
        clear_checkpoint(out_dir)
        set_paused(out_dir, False)
    else:
        _persist_checkpoint(out_dir, checkpoint, cb)
        if paused:
            set_paused(out_dir, True)

    return result
