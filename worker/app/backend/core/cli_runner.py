"""Command-line runner for SJ88 Green Screen TC01-TC06.

The CLI intentionally reuses the same pure pipeline layer as the Tk GUI.
When ffmpeg_mode is "dry-run", no ffmpeg work is started; the runner only
builds the input contract and planned output filenames.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from core.ai_reframe import Composition, FIXED_7X3_LENSES, ReframeSettings
from core.auto_dragdrop import (
    AUTO_CONTEXT_MIXED,
    AUTO_CONTEXT_SOURCE_ONLY,
    AutoDragDropResult,
    classify_auto_dragdrop,
    probe_media_duration,
)
from core.batch_pingpong import (
    BatchSettings,
    PingPongSegment,
    build_asset_pickers,
    build_forward_segment_ranges,
)
from core.contract import (
    TC02_VIDEO_DEFAULTS,
    TC03_VIDEO_DEFAULTS,
    TC04_BATCH_DEFAULTS,
    TC04_VIDEO_DEFAULTS,
    TC05_VIDEO_DEFAULTS,
    TC06_VIDEO_DEFAULTS,
    batch_settings_for,
    clamp_tc05_workers,
    validate_tc03_segment_duration,
    green_settings_for,
    pick_compositions,
    reframe_outputs_per_source,
    reframe_settings_for,
    validate_tc06_allow_clip_reuse,
)
from core.pipelines import (
    PipelineCallbacks,
    PipelineInputs,
    PipelineResult,
    PipelineStatus,
    render_tc01,
    render_tc02,
    render_tc03,
    render_tc04,
    render_tc05,
    render_tc06,
)
from core.pipelines._common import (
    apply_seed,
    invalid_video_inputs,
    overlapping_role_paths,
    normalize_run_seed,
    resolve_run_seed,
    shuffle_pool,
)
from core.tc06_products import final_output_path, resolve_product_folders


TC_KEYS = ("TC01", "TC02", "TC03", "TC04", "TC05", "TC06")
OUTPUT_DIR_NAMES = {
    "TC01": "tc01_single_output",
    "TC02": "tc02_reframe_output",
    "TC03": "tc03_batch_output",
    "TC04": "tc04_rebatch_output",
    "TC05": "tc05_reframe_only_output",
    "TC06": "tc06_output",
}


@dataclass
class PlannedOutput:
    kind: str
    path: str
    source: str = ""
    product: str = ""
    background: str = ""
    audio: str = ""
    cover: str = ""
    lens: str = ""
    composition: str = ""
    segment_index: int = -1
    output_index: int = 0
    note: str = ""


@dataclass
class _ReframePlanTask:
    source_path: str
    lens_key: str
    composition: str
    output_path: str


@dataclass
class CliCaseResult:
    tc: str
    ffmpeg_mode: str
    output_dir: str
    inputs: dict[str, list[str]]
    values: dict[str, Any]
    planned_outputs: list[PlannedOutput] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    progress: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, str]] = field(default_factory=list)
    auto_dragdrop: Optional[dict[str, Any]] = None
    pipeline_result: Optional[dict[str, Any]] = None
    status: str = "PENDING"
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tc": self.tc,
            "ffmpeg_mode": self.ffmpeg_mode,
            "output_dir": self.output_dir,
            "inputs": self.inputs,
            "values": self.values,
            "planned_outputs": [asdict(item) for item in self.planned_outputs],
            "planned_output_count": len(self.planned_outputs),
            "logs": self.logs,
            "progress": self.progress,
            "steps": self.steps,
            "auto_dragdrop": self.auto_dragdrop,
            "pipeline_result": self.pipeline_result,
            "status": self.status,
            "error": self.error,
        }


def _flatten(items: Optional[Sequence[Sequence[str]]]) -> list[str]:
    out: list[str] = []
    for group in items or []:
        out.extend(str(item) for item in group if item)
    return out


def _safe_stem(path: str) -> str:
    stem = Path(path).stem
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem)[:160]


def _plan_reframe_tasks(
    sources: Sequence[str],
    out_dir: str,
    settings: ReframeSettings,
    *,
    expected_outputs_per_source: int,
) -> list[_ReframePlanTask]:
    """Build reframe output names without touching the filesystem."""
    comps: list[Composition] = []
    for raw in settings.compositions:
        try:
            comps.append(Composition(raw))
        except ValueError:
            comps.append(Composition.CENTER)
    if not comps:
        comps = [Composition.CENTER]
    srcs = list(sources)
    if settings.max_sources > 0:
        srcs = srcs[:settings.max_sources]
    stem_counts: dict[str, int] = {}
    for source in srcs:
        stem_key = Path(source).stem.casefold()
        stem_counts[stem_key] = stem_counts.get(stem_key, 0) + 1

    tasks: list[_ReframePlanTask] = []
    for src_index, src in enumerate(srcs, 1):
        src_base = Path(src).stem
        output_base = (
            src_base if stem_counts[src_base.casefold()] == 1
            else f"{src_base}__src{src_index:03d}"
        )
        for lens in FIXED_7X3_LENSES:
            for comp in comps:
                out_name = f"{output_base}__{lens.key}__{comp.value}.mp4"
                tasks.append(_ReframePlanTask(
                    source_path=src,
                    lens_key=lens.key,
                    composition=comp.value,
                    output_path=os.path.join(out_dir, out_name),
                ))
    expected_tasks = len(srcs) * expected_outputs_per_source
    if len(tasks) != expected_tasks:
        raise RuntimeError(
            "reframe planner count mismatch: "
            f"planned={len(tasks)} expected={expected_tasks}"
        )
    return tasks


def _load_values(raw: str = "", path: str = "") -> dict[str, Any]:
    if raw:
        return json.loads(raw)
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return {}


def _duration(path: str, assume_seconds: float) -> float:
    dur = probe_media_duration(path)
    if dur is not None and dur > 0:
        return dur
    if assume_seconds > 0:
        return assume_seconds
    return 0.0


def _split_count_for_file(path: str, segment_duration: float, assume_seconds: float) -> int:
    dur = _duration(path, assume_seconds)
    return len(build_forward_segment_ranges(dur, segment_duration))


def _segments_for_file(path: str, segment_duration: float, assume_seconds: float) -> list[PingPongSegment]:
    dur = _duration(path, assume_seconds)
    return [
        PingPongSegment(time_range=time_range, direction=1, segment_index=index)
        for index, time_range in enumerate(
            build_forward_segment_ranges(dur, segment_duration)
        )
    ]


def _auto_inputs(tc: str, paths: Sequence[str]) -> tuple[dict[str, list[str]], Optional[AutoDragDropResult]]:
    if tc == "TC06":
        return {
            "products": [],
            "backgrounds": [],
            "audios": [],
            "covers": [],
            "sources": [],
            "product_roots": [str(path) for path in paths if os.path.isdir(path)],
        }, None
    source_only = tc == "TC05"
    context = (
        AUTO_CONTEXT_SOURCE_ONLY
        if source_only
        else AUTO_CONTEXT_MIXED
    )
    result = classify_auto_dragdrop(paths, context=context)
    if source_only:
        return {
            "products": [],
            "backgrounds": [],
            "audios": [],
            "covers": [],
            "sources": list(result.source),
            "product_roots": [],
        }, result
    if tc == "TC06":
        # V1.0.0.10: TC06 also takes the auto-classified product set as sources.
        return {
            "products": [],
            "backgrounds": [],
            "audios": [],
            "covers": [],
            "sources": list(result.product),
        }, result
    return {
        "products": list(result.product),
        "backgrounds": list(result.background),
        "audios": list(result.audio),
        "covers": [],
        "sources": [],
        "product_roots": [],
    }, result


def build_pipeline_inputs(
    tc: str,
    *,
    output_dir: str,
    values: dict[str, Any],
    products: Sequence[str] = (),
    backgrounds: Sequence[str] = (),
    audios: Sequence[str] = (),
    covers: Sequence[str] = (),
    sources: Sequence[str] = (),
    product_roots: Sequence[str] = (),
    auto_paths: Sequence[str] = (),
) -> tuple[PipelineInputs, Optional[AutoDragDropResult]]:
    """Build the same data contract that the GUI hands to core pipelines."""
    tc = tc.upper()
    auto_result: Optional[AutoDragDropResult] = None
    if auto_paths:
        auto_data, auto_result = _auto_inputs(tc, auto_paths)
        products = [*products, *auto_data["products"]]
        backgrounds = [*backgrounds, *auto_data["backgrounds"]]
        audios = [*audios, *auto_data["audios"]]
        covers = [*covers, *auto_data["covers"]]
        sources = [*sources, *auto_data["sources"]]
        product_roots = [*product_roots, *auto_data["product_roots"]]
    return PipelineInputs(
        output_dir=output_dir,
        values=dict(values),
        products=list(products),
        backgrounds=list(backgrounds),
        audios=list(audios),
        covers=list(covers),
        sources=list(sources),
        product_roots=list(product_roots),
    ), auto_result


def _plan_tc01(inputs: PipelineInputs, run_stamp: str) -> list[PlannedOutput]:
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    raw_seed = inputs.values.get("seed", 0)
    requested_seed = normalize_run_seed(raw_seed)
    seed = resolve_run_seed(raw_seed)
    seed_mode = "auto" if requested_seed is None else "fixed"
    rng = apply_seed(seed)
    backgrounds = shuffle_pool(backgrounds, rng)
    audios = shuffle_pool(audios, rng)
    cover = inputs.covers[0] if inputs.covers else ""
    outputs: list[PlannedOutput] = []
    for idx, product in enumerate(inputs.products, 1):
        bg = backgrounds[(idx - 1) % len(backgrounds)]
        audio = audios[(idx - 1) % len(audios)] if audios else ""
        outputs.append(PlannedOutput(
            kind="final",
            path=os.path.join(inputs.output_dir, f"{_safe_stem(product)}_single_{run_stamp}_{idx:03d}.mp4"),
            product=product,
            background=bg,
            audio=audio,
            cover=cover,
            output_index=idx,
            note=f"seed_mode={seed_mode} effective_seed={seed}",
        ))
    return outputs


def _tc02_reframe_settings(values: dict[str, Any]):
    compositions = pick_compositions(values, all_three_default=True)
    return reframe_settings_for(values, compositions=compositions, defaults=TC02_VIDEO_DEFAULTS)


def _plan_tc02(inputs: PipelineInputs, run_stamp: str) -> list[PlannedOutput]:
    reframe_dir = os.path.join(inputs.output_dir, "reframe")
    tasks = _plan_reframe_tasks(
        list(inputs.products),
        reframe_dir,
        _tc02_reframe_settings(inputs.values),
        expected_outputs_per_source=reframe_outputs_per_source(inputs.values),
    )
    outputs: list[PlannedOutput] = []
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    cover = inputs.covers[0] if inputs.covers else ""
    for task in tasks:
        outputs.append(PlannedOutput(
            kind="intermediate_reframe",
            path=task.output_path,
            source=task.source_path,
            product=task.source_path,
            lens=task.lens_key,
            composition=task.composition,
        ))
    for idx, task in enumerate(tasks, 1):
        bg = backgrounds[(idx - 1) % len(backgrounds)] if backgrounds else ""
        audio = audios[(idx - 1) % len(audios)] if audios else ""
        outputs.append(PlannedOutput(
            kind="final",
            path=os.path.join(
                inputs.output_dir,
                f"{_safe_stem(task.output_path)}__tc02_chroma_{run_stamp}_{idx:03d}.mp4",
            ),
            source=task.output_path,
            product=task.source_path,
            background=bg,
            audio=audio,
            cover=cover,
            lens=task.lens_key,
            composition=task.composition,
            output_index=idx,
        ))
    return outputs


def _tc03_batch_settings(inputs: PipelineInputs, assume_seconds: float) -> BatchSettings:
    segment_duration = validate_tc03_segment_duration(inputs.values.get("segment_duration", 10.0))
    num_outputs = sum(_split_count_for_file(p, segment_duration, assume_seconds) for p in inputs.products)
    settings = batch_settings_for(
        segment_duration=segment_duration,
        num_outputs=num_outputs,
        has_audio=bool(inputs.audios),
        match_mode_str="no_repeat",
    )
    settings.product_ping_pong = False
    settings.seed = resolve_run_seed(inputs.values.get("seed", 0))
    return settings


def _plan_tc03(inputs: PipelineInputs, assume_seconds: float, run_stamp: str) -> list[PlannedOutput]:
    backgrounds = list(inputs.backgrounds)
    base_settings = green_settings_for(inputs.values, has_audio=bool(inputs.audios), has_cover=bool(inputs.covers), defaults=TC03_VIDEO_DEFAULTS)
    batch_settings = _tc03_batch_settings(inputs, assume_seconds)
    if batch_settings.num_outputs <= 0:
        return []
    outputs: list[PlannedOutput] = []
    output_index = 0
    audios = list(inputs.audios)
    _, pick_bg, pick_audio = build_asset_pickers(backgrounds, audios, batch_settings)
    cover = inputs.covers[0] if inputs.covers else ""
    for product in inputs.products:
        for segment in _segments_for_file(product, batch_settings.segment_duration, assume_seconds):
            output_index += 1
            bg = pick_bg()
            audio = pick_audio() if audios else ""
            out_name = f"batch_{run_stamp}_{output_index:03d}_{Path(product).stem}.mp4"
            outputs.append(PlannedOutput(
                kind="final",
                path=os.path.join(inputs.output_dir, out_name),
                product=product,
                background=bg,
                audio=audio,
                cover=cover,
                segment_index=segment.segment_index,
                output_index=output_index,
                note=f"segment={segment.time_range.start:.3f}-{segment.time_range.end:.3f}s encoder={base_settings.encoder_alias}",
            ))
    return outputs


def _tc04_reframe_settings(values: dict[str, Any]):
    compositions = pick_compositions(values, all_three_default=True)
    return reframe_settings_for(values, compositions=compositions, defaults=TC04_VIDEO_DEFAULTS)


def _plan_tc04(inputs: PipelineInputs, assume_seconds: float, run_stamp: str) -> list[PlannedOutput]:
    segment_duration = validate_tc03_segment_duration(inputs.values.get("segment_duration", TC04_BATCH_DEFAULTS["segment_duration"]))
    batch_settings = batch_settings_for(
        segment_duration=segment_duration,
        num_outputs=0,
        has_audio=bool(inputs.audios),
        match_mode_str=inputs.values.get("match_mode", TC04_BATCH_DEFAULTS["match_mode"]),
    )
    batch_settings.product_ping_pong = False
    batch_settings.uploaded_audio_controls_duration = bool(inputs.audios)
    batch_settings.seed = resolve_run_seed(inputs.values.get("seed", 0))

    reframe_dir = os.path.join(inputs.output_dir, "reframe")
    tasks = _plan_reframe_tasks(
        list(inputs.products),
        reframe_dir,
        _tc04_reframe_settings(inputs.values),
        expected_outputs_per_source=reframe_outputs_per_source(inputs.values),
    )
    outputs: list[PlannedOutput] = []
    for task in tasks:
        outputs.append(PlannedOutput(
            kind="intermediate_reframe",
            path=task.output_path,
            source=task.source_path,
            product=task.source_path,
            lens=task.lens_key,
            composition=task.composition,
        ))
    backgrounds = list(inputs.backgrounds)
    audios = list(inputs.audios)
    _, pick_bg, pick_audio = build_asset_pickers(backgrounds, audios, batch_settings)
    cover = inputs.covers[0] if inputs.covers else ""
    final_index = 0
    for task in tasks:
        for segment in _segments_for_file(task.source_path, segment_duration, assume_seconds):
            final_index += 1
            bg = pick_bg()
            audio = pick_audio() if audios else ""
            outputs.append(PlannedOutput(
                kind="final",
                path=os.path.join(inputs.output_dir, f"batch_{run_stamp}_{final_index:03d}_{Path(task.output_path).stem}.mp4"),
                source=task.output_path,
                product=task.source_path,
                background=bg,
                audio=audio,
                cover=cover,
                lens=task.lens_key,
                composition=task.composition,
                segment_index=segment.segment_index,
                output_index=final_index,
                note=(
                    f"segment={segment.time_range.start:.3f}-{segment.time_range.end:.3f}s "
                    + ("final_duration=selected_audio" if audio else "final_duration=source_segment")
                ),
            ))
    return outputs


def _tc05_reframe_settings(values: dict[str, Any]):
    compositions = pick_compositions(values, all_three_default=True)
    return reframe_settings_for(
        values,
        compositions=compositions,
        defaults=TC05_VIDEO_DEFAULTS,
        max_parallel_override=clamp_tc05_workers(values),
    )


def _plan_tc05(inputs: PipelineInputs) -> list[PlannedOutput]:
    tasks = _plan_reframe_tasks(
        list(inputs.sources),
        inputs.output_dir,
        _tc05_reframe_settings(inputs.values),
        expected_outputs_per_source=reframe_outputs_per_source(inputs.values),
    )
    return [
        PlannedOutput(
            kind="final",
            path=task.output_path,
            source=task.source_path,
            product=task.source_path,
            lens=task.lens_key,
            composition=task.composition,
            output_index=idx,
        )
        for idx, task in enumerate(tasks, 1)
    ]


def _plan_tc06(inputs: PipelineInputs, run_stamp: str) -> list[PlannedOutput]:
    layouts, errors = resolve_product_folders(inputs.product_roots)
    if errors:
        raise ValueError("; ".join(errors))
    allow_reuse = validate_tc06_allow_clip_reuse(inputs.values)
    outputs: list[PlannedOutput] = []
    output_index = 0
    for layout in layouts:
        for audio_index, audio in enumerate(layout.audios, 1):
            output_index += 1
            outputs.append(
                PlannedOutput(
                    kind="final",
                    path=final_output_path(layout, audio, audio_index, run_stamp),
                    source=layout.root,
                    product=layout.root,
                    audio=audio,
                    output_index=output_index,
                    note=(
                        f"pipeline=TC01_chroma_then_audio_master "
                        f"allow_clip_reuse={str(allow_reuse).lower()}"
                    ),
                )
            )
    return outputs


def plan_outputs(
    tc: str,
    inputs: PipelineInputs,
    *,
    run_stamp: str,
    assume_duration_seconds: float = 0.0,
) -> list[PlannedOutput]:
    tc = tc.upper()
    if tc == "TC01":
        return _plan_tc01(inputs, run_stamp)
    if tc == "TC02":
        return _plan_tc02(inputs, run_stamp)
    if tc == "TC03":
        return _plan_tc03(inputs, assume_duration_seconds, run_stamp)
    if tc == "TC04":
        return _plan_tc04(inputs, assume_duration_seconds, run_stamp)
    if tc == "TC05":
        return _plan_tc05(inputs)
    if tc == "TC06":
        return _plan_tc06(inputs, run_stamp)
    raise ValueError(f"unknown TC: {tc}")


def _render_func(tc: str):
    return {
        "TC01": render_tc01,
        "TC02": render_tc02,
        "TC03": render_tc03,
        "TC04": render_tc04,
        "TC05": render_tc05,
        "TC06": render_tc06,
    }[tc]


def _inputs_dict(inputs: PipelineInputs) -> dict[str, list[str]]:
    return {
        "products": list(inputs.products),
        "backgrounds": list(inputs.backgrounds),
        "audios": list(inputs.audios),
        "covers": list(inputs.covers),
        "sources": list(inputs.sources),
        "product_roots": list(inputs.product_roots),
    }


def _validate_tc01_tc04_settings(tc: str, inputs: PipelineInputs) -> None:
    """Fail closed on the same settings used by real GUI pipeline runs."""
    values = inputs.values
    has_audio = bool(inputs.audios)
    has_cover = bool(inputs.covers)

    if tc == "TC01":
        green_settings_for(
            values,
            has_audio=has_audio,
            has_cover=has_cover,
        )
        normalize_run_seed(values.get("seed", 0))
    elif tc == "TC02":
        compositions = pick_compositions(values, all_three_default=True)
        reframe_settings_for(
            values,
            compositions=compositions,
            defaults=TC02_VIDEO_DEFAULTS,
        )
        green_settings_for(
            values,
            has_audio=has_audio,
            has_cover=has_cover,
            defaults=TC02_VIDEO_DEFAULTS,
        )
    elif tc == "TC03":
        validate_tc03_segment_duration(values.get("segment_duration", 10.0))
        green_settings_for(
            values,
            has_audio=has_audio,
            has_cover=has_cover,
            defaults=TC03_VIDEO_DEFAULTS,
        )
        normalize_run_seed(values.get("seed", 0))
    elif tc == "TC04":
        batch_settings_for(
            segment_duration=values.get(
                "segment_duration", TC04_BATCH_DEFAULTS["segment_duration"]
            ),
            num_outputs=0,
            has_audio=has_audio,
            match_mode_str=values.get(
                "match_mode", TC04_BATCH_DEFAULTS["match_mode"]
            ),
        )
        compositions = pick_compositions(values, all_three_default=True)
        reframe_settings_for(
            values,
            compositions=compositions,
            defaults=TC04_VIDEO_DEFAULTS,
        )
        green_settings_for(
            values,
            has_audio=has_audio,
            has_cover=has_cover,
            defaults=TC04_VIDEO_DEFAULTS,
        )
        normalize_run_seed(values.get("seed", 0))


def _validate_inputs(tc: str, inputs: PipelineInputs) -> list[str]:
    errors: list[str] = []
    if tc in {"TC01", "TC02", "TC03", "TC04"} and not inputs.products:
        errors.append("missing products")
    if tc in {"TC01", "TC02", "TC03", "TC04"} and not inputs.backgrounds:
        errors.append("missing backgrounds")
    if tc in {"TC01", "TC02", "TC03", "TC04"}:
        invalid_products = invalid_video_inputs(inputs.products)
        if invalid_products:
            errors.append(f"Product must be a video: {invalid_products[0]}")
        role_overlap = overlapping_role_paths(
            inputs.products,
            inputs.backgrounds,
        )
        if role_overlap:
            errors.append(
                "Product and Background must be different files: "
                f"{role_overlap[0]}"
            )
    if tc == "TC05" and not inputs.sources:
        errors.append("missing sources")
    if tc == "TC06":
        layouts, layout_errors = resolve_product_folders(inputs.product_roots)
        if not layouts and not layout_errors:
            layout_errors.append("missing product roots")
        errors.extend(layout_errors)
        try:
            validate_tc06_allow_clip_reuse(inputs.values)
            green_settings_for(
                inputs.values,
                has_audio=False,
                has_cover=False,
                defaults=TC06_VIDEO_DEFAULTS,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            errors.append(f"TC06 settings error: {exc}")
    if tc in {"TC02", "TC04", "TC05"}:
        try:
            compositions = pick_compositions(
                inputs.values, all_three_default=True
            )
        except (TypeError, ValueError) as exc:
            message = f"{tc} settings error: {exc}"
            if message not in errors:
                errors.append(message)
        else:
            if not compositions:
                errors.append(f"{tc} requires at least one enabled composition")
    if tc in {"TC01", "TC02", "TC03", "TC04"}:
        try:
            _validate_tc01_tc04_settings(tc, inputs)
        except (TypeError, ValueError, OverflowError) as exc:
            message = f"{tc} settings error: {exc}"
            if message not in errors:
                errors.append(message)
    return errors


def run_case(
    tc: str,
    *,
    output_dir: str,
    values: dict[str, Any],
    products: Sequence[str] = (),
    backgrounds: Sequence[str] = (),
    audios: Sequence[str] = (),
    covers: Sequence[str] = (),
    sources: Sequence[str] = (),
    product_roots: Sequence[str] = (),
    auto_paths: Sequence[str] = (),
    ffmpeg_mode: str = "real",
    run_stamp: str = "",
    assume_duration_seconds: float = 0.0,
    allow_invalid: bool = False,
) -> CliCaseResult:
    tc = tc.upper()
    run_stamp = run_stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    inputs, auto_result = build_pipeline_inputs(
        tc,
        output_dir=output_dir,
        values=values,
        products=products,
        backgrounds=backgrounds,
        audios=audios,
        covers=covers,
        sources=sources,
        product_roots=product_roots,
        auto_paths=auto_paths,
    )
    result = CliCaseResult(
        tc=tc,
        ffmpeg_mode=ffmpeg_mode,
        output_dir=output_dir,
        inputs=_inputs_dict(inputs),
        values=dict(values),
        auto_dragdrop=auto_result.as_dict() if auto_result else None,
    )
    errors = _validate_inputs(tc, inputs)
    if errors and not allow_invalid:
        result.status = "FAIL"
        result.error = "; ".join(errors)
        return result

    if ffmpeg_mode == "dry-run":
        try:
            result.planned_outputs = plan_outputs(
                tc,
                inputs,
                run_stamp=run_stamp,
                assume_duration_seconds=assume_duration_seconds,
            )
            result.status = "PASS"
        except Exception as exc:
            result.status = "FAIL"
            result.error = str(exc)
        return result

    logs: list[str] = []
    progress: list[dict[str, Any]] = []
    steps: list[dict[str, str]] = []

    def log_fn(msg: str) -> None:
        logs.append(str(msg))
        _safe_console_log(msg)

    def progress_fn(pct: float, info: str) -> None:
        progress.append({"pct": float(pct), "info": str(info)})

    def step_fn(name: str, text: str) -> None:
        steps.append({"name": str(name), "text": str(text)})

    os.makedirs(output_dir, exist_ok=True)
    try:
        pipeline_result = _render_func(tc)(
            inputs,
            PipelineCallbacks(
                log_fn=log_fn,
                stop_check=lambda: False,
                progress_fn=progress_fn,
                file_fn=lambda name: logs.append(f"file: {name}"),
                pause_check=lambda: False,
                step_fn=step_fn,
            ),
        )
        if not isinstance(pipeline_result, PipelineResult):
            result.status = "FAIL"
            result.error = "pipeline did not return PipelineResult"
        else:
            result.pipeline_result = pipeline_result.to_dict()
            if (
                pipeline_result.is_success
                and pipeline_result.status is PipelineStatus.SUCCEEDED
            ):
                result.status = "PASS"
            else:
                result.status = "FAIL"
                errors = pipeline_result.all_errors
                result.error = (
                    "; ".join(errors[:5])
                    if errors
                    else f"pipeline status={pipeline_result.status.value}"
                )
    except Exception as exc:
        result.status = "FAIL"
        result.error = str(exc)
    result.logs = logs
    result.progress = progress
    result.steps = steps
    return result


def _default_output_dir(root: Path, tc: str) -> str:
    return str(root / OUTPUT_DIR_NAMES[tc])


def _print_text(result: dict[str, Any]) -> None:
    for case in result["cases"]:
        print(f"{case['tc']} {case['status']} mode={case['ffmpeg_mode']} out={case['output_dir']}")
        if case.get("error"):
            print(f"  error: {case['error']}")
        print(f"  planned outputs: {case.get('planned_output_count', 0)}")
        for item in case.get("planned_outputs", [])[:50]:
            print(f"  - {item['kind']}: {item['path']}")
        if len(case.get("planned_outputs", [])) > 50:
            print(f"  ... {len(case['planned_outputs']) - 50} more")


def _safe_console_log(message: Any) -> None:
    """Write a CLI log line without allowing the active codepage to fail a render."""
    text = str(message)
    try:
        print(text, flush=True)
        return
    except UnicodeEncodeError:
        pass
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="backslashreplace").decode(
        encoding, errors="replace"
    )
    try:
        sys.stdout.write(safe + "\n")
        sys.stdout.flush()
    except Exception:
        # Logs remain available in CliCaseResult.logs; console I/O must never
        # mutate a successful FFmpeg result into a pipeline failure.
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SJ88 Green Screen CLI: GUI-equivalent TC01-TC06 runner with ffmpeg dry-run planning.",
    )
    parser.add_argument("--tc", choices=[*TC_KEYS, "ALL"], required=True, help="Test case/pipeline to run.")
    parser.add_argument("--ffmpeg-mode", choices=["real", "dry-run"], default="real")
    parser.add_argument("--dry-run", action="store_true", help="Alias for --ffmpeg-mode dry-run.")
    parser.add_argument("--auto", nargs="+", action="append", help="Files/folders to classify with Auto DragDrop rules.")
    parser.add_argument("--product", nargs="+", action="append", help="Product/green-screen video files.")
    parser.add_argument("--bg", nargs="+", action="append", help="Background image/video files (required for TC01-TC04).")
    parser.add_argument("--audio", nargs="+", action="append", help="Audio files.")
    parser.add_argument("--cover", nargs="+", action="append", help="Cover files.")
    parser.add_argument("--source", nargs="+", action="append", help="TC05 source videos.")
    parser.add_argument("--product-root", nargs="+", action="append", help="TC06 parent root or direct product folders.")
    parser.add_argument("--output-root", default="", help="Base output root. Defaults to repo root.")
    parser.add_argument("--output-dir", default="", help="Exact output dir for a single TC.")
    parser.add_argument("--values-json", default="", help="Raw JSON settings dict.")
    parser.add_argument("--values-file", default="", help="Path to JSON settings dict.")
    parser.add_argument("--run-stamp", default="", help="Deterministic timestamp for planned names, e.g. 20260704_120000.")
    parser.add_argument("--assume-duration-seconds", type=float, default=0.0, help="Fallback duration for batch dry-run when ffprobe is unavailable.")
    parser.add_argument("--allow-invalid", action="store_true", help="Return a plan/status even when required inputs are missing.")
    parser.add_argument("--json-out", default="", help="Write full result JSON to this path.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ffmpeg_mode = "dry-run" if args.dry_run else args.ffmpeg_mode
    root = Path(__file__).resolve().parent.parent
    output_root = Path(args.output_root).resolve() if args.output_root else root
    values = _load_values(args.values_json, args.values_file)
    auto_paths = _flatten(args.auto)
    products = _flatten(args.product)
    backgrounds = _flatten(args.bg)
    audios = _flatten(args.audio)
    covers = _flatten(args.cover)
    sources = _flatten(args.source)
    product_roots = _flatten(args.product_root)

    tcs = list(TC_KEYS) if args.tc == "ALL" else [args.tc]
    cases: list[CliCaseResult] = []
    for tc in tcs:
        output_dir = args.output_dir if args.output_dir and len(tcs) == 1 else _default_output_dir(output_root, tc)
        cases.append(
            run_case(
                tc,
                output_dir=output_dir,
                values=values,
                products=products,
                backgrounds=backgrounds,
                audios=audios,
                covers=covers,
                sources=sources,
                product_roots=product_roots,
                auto_paths=auto_paths,
                ffmpeg_mode=ffmpeg_mode,
                run_stamp=args.run_stamp,
                assume_duration_seconds=args.assume_duration_seconds,
                allow_invalid=args.allow_invalid,
            )
        )

    payload = {
        "tool": "green_cli",
        "ffmpeg_mode": ffmpeg_mode,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "cases": [case.as_dict() for case in cases],
        "status": "PASS" if all(case.status == "PASS" for case in cases) else "FAIL",
    }
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_text(payload)
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
