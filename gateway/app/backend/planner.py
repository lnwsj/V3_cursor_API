"""Pure V3 output planner used by dry-run and contract tests.

The planner deliberately does not probe media or call FFmpeg.  It estimates
final output counts from the request shape and explicit duration assumptions.
Actual pipelines remain the source of truth for validated files.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


TC_LABELS = {"tc01", "tc02", "tc03", "tc04", "tc05", "tc06"}
LENS_COUNT = 7
COMPOSITION_NAMES = ("center", "left", "right")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return default


def _as_positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def composition_count(values: Mapping[str, Any] | None) -> int:
    values = values or {}
    toggles = [
        _as_bool(values.get("use_center"), True),
        _as_bool(values.get("use_left"), True),
        _as_bool(values.get("use_right"), True),
    ]
    return sum(toggles)


def _count(files: Mapping[str, Any] | None, *keys: str) -> int:
    files = files or {}
    for key in keys:
        value = files.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return len(value)
        if isinstance(value, str) and value:
            return 1
    return 0


def plan_tc(tc: str, files: Mapping[str, Any] | None, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
    label = str(tc).lower()
    if label not in TC_LABELS:
        raise ValueError(f"unsupported tc: {tc}")
    values = dict(values or {})
    products = _count(files, "product", "products")
    backgrounds = _count(files, "bg", "background", "backgrounds")
    sources = _count(files, "source", "sources")
    roots = _count(files, "product_root", "product_roots", "root", "roots")
    compositions = composition_count(values)
    reframe_per_source = LENS_COUNT * compositions
    duration = _as_positive_float(values.get("assume_duration_seconds"), 1.0)
    segment_duration = _as_positive_float(values.get("segment_duration"), 10.0)
    segment_count = max(1, math.ceil(duration / segment_duration))

    if label == "tc01":
        final_count = products
        stage_count = final_count
    elif label == "tc02":
        final_count = products * reframe_per_source
        stage_count = final_count * 2
    elif label == "tc03":
        final_count = products * segment_count
        stage_count = final_count
    elif label == "tc04":
        final_count = products * reframe_per_source * segment_count
        stage_count = products * reframe_per_source + final_count
    elif label == "tc05":
        final_count = sources * reframe_per_source
        stage_count = final_count
    else:
        # TC06 discovers final outputs from audio files inside each root.  An
        # archive's contents are unknown to a pure dry-run, so an explicit
        # audio list is exact and a root count is only a lower-confidence
        # fallback for UI previews.
        final_count = _count(files, "audio", "audios") or roots
        stage_count = final_count

    return {
        "tc": label,
        "products": products,
        "backgrounds": backgrounds,
        "sources": sources,
        "product_roots": roots,
        "composition_count": compositions,
        "reframe_per_source": reframe_per_source,
        "segment_count_assumption": segment_count,
        "assume_duration_seconds": duration,
        "segment_duration": segment_duration,
        "planned_stage_count": stage_count,
        "planned_output_count": final_count,
        "final_count": final_count,
        "values": values,
        "assumptions": [
            "counts are estimates until Worker validates media",
            "duration uses assume_duration_seconds when ffprobe data is unavailable",
        ],
    }
