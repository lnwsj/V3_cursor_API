"""
core/pipelines/_common.py — Shared dataclasses + helpers for TC01-05 pipelines.

Pure Python, no tkinter. Imported by every `core/pipelines/tc0N_*.py`.

Contents:
    - PipelineInputs:    what the UI passes to a pipeline (files + values)
    - PipelineCallbacks: log/progress/file/stop/pause callbacks (provided by
                         BaseRenderTab via Worker; the pipeline never imports
                         tkinter)
    - StepCallback:      protocol for per-step UI updates (used by TC04 which
                         has a 2-stage reframe -> batch pipeline)
    - combined_stop_check(stop, pause) -> bool
    - safe_log / safe_progress / safe_file: no-op-safe wrappers
    - shuffle_pool(items, rng) -> list
    - apply_seed(seed) -> random.Random
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence


VIDEO_INPUT_EXTS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"})
RUN_SEED_MAX = 999999


# =====================================================================
# Callbacks — what the Worker thread hands to every pipeline.
# =====================================================================
# These match the signature BaseRenderTab already passes through Worker.start:
#   log_fn(msg: str)
#   stop_check() -> bool
#   progress_fn(pct: float, info: str)
#   file_fn(filename: str)
#   pause_check() -> bool
LogFn = Callable[[str], None]
StopFn = Callable[[], bool]
ProgressFn = Callable[[float, str], None]
FileFn = Callable[[str], None]
PauseFn = Callable[[], bool]
StepFn = Callable[[str, str], None]   # (step_name, status_text)


class StepCallback(Protocol):
    """Protocol for per-step UI updates (used by TC04 2-stage pipeline)."""

    def __call__(self, step_name: str, status_text: str) -> None: ...


@dataclass
class PipelineCallbacks:
    """Callbacks the UI Worker passes to a pipeline.

    All fields are optional except log_fn / stop_check / progress_fn.
    file_fn / pause_check may be None if the UI doesn't provide them.
    step_fn is used by TC04 to update its 2-stage pipeline badges.
    """
    log_fn: LogFn
    stop_check: StopFn
    progress_fn: ProgressFn
    file_fn: Optional[FileFn] = None
    pause_check: Optional[PauseFn] = None
    step_fn: Optional[StepFn] = None  # for multi-stage UI badges


@dataclass
class PipelineInputs:
    """Files + values passed from the UI tab to the pipeline.

    `output_dir` is absolute and already created by the caller
    (the UI tab knows its OUTPUT_DIR_NAME).

    `values` is the raw `SettingsPanel.get_values()` dict for the active
    settings panel (each tab has its own set of fields; the pipeline reads
    only the keys it cares about and falls back to the documented defaults
    if a key is missing — same policy as V3's old inline render code).
    """
    output_dir: str
    values: Dict[str, Any] = field(default_factory=dict)
    # Used by TC02/TC03/TC04 to compose products + bg + audio + cover.
    products: List[str] = field(default_factory=list)
    backgrounds: List[str] = field(default_factory=list)
    audios: List[str] = field(default_factory=list)
    covers: List[str] = field(default_factory=list)
    # TC05 only: source files (kept separate from `products` for clarity).
    sources: List[str] = field(default_factory=list)
    # TC06 only: direct product folders and/or parent roots.  TC06 resolves
    # each product independently so product/bg/audio pools never cross.
    product_roots: List[str] = field(default_factory=list)
    # FIX (B-03, 2026-07-31): TC06 outer pipeline mints one microsecond run_stamp
    # per session and passes it down so the inner chroma stage (render_tc01)
    # uses the SAME stamp for its output filenames. Without this, the chroma
    # intermediate and the audio-master final have different timestamps and
    # operators cannot correlate them by name on disk. Empty string means
    # "mint your own" (default behaviour).
    run_stamp: str = ""


# =====================================================================
# Result truth contract
# =====================================================================

class PipelineStatus(str, Enum):
    """Terminal pipeline verdicts.

    ``SUCCEEDED`` is deliberately the only passing value. Every result starts
    as ``FAILED`` and can become successful only after :meth:`finalize` has
    checked both count invariants and the final output files.
    """

    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"
    INVALID_INPUT = "INVALID_INPUT"


def _count_value(value: Any) -> int:
    """Return a count for arithmetic without allowing invalid values to pass."""

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _json_safe(value: Any) -> Any:
    """Recursively convert result payloads to values accepted by ``json``."""

    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Stable ordering keeps evidence hashes and snapshots deterministic.
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _dedupe(items: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        text = str(item)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


class _ResultMixin:
    """Shared fail-closed finalization for stage and pipeline results."""

    expected: int
    succeeded: int
    failed: int
    skipped: int
    cancelled: int
    validated_resumed: int
    outputs: List[str]
    errors: List[str]
    status: PipelineStatus
    invariant_errors: List[str]
    invalid_outputs: List[str]
    valid_output_count: int
    outputs_validated: bool
    _finalized: bool

    @property
    def produced_this_run(self) -> int:
        """Number of newly produced outputs (alias for ``succeeded``)."""

        return _count_value(self.succeeded)

    @property
    def completed_count(self) -> int:
        """New outputs plus validated checkpoint/resume outputs."""

        return _count_value(self.succeeded) + _count_value(self.validated_resumed)

    @property
    def accounted_count(self) -> int:
        """All mutually exclusive terminal task counts."""

        return (
            _count_value(self.succeeded)
            + _count_value(self.failed)
            + _count_value(self.skipped)
            + _count_value(self.cancelled)
            + _count_value(self.validated_resumed)
        )

    @property
    def is_success(self) -> bool:
        """True only after a complete, invariant-checked finalization."""

        return (
            self._finalized
            and self.status is PipelineStatus.SUCCEEDED
            and not self.invariant_errors
        )

    @property
    def all_errors(self) -> List[str]:
        """Reported engine errors followed by generated invariant failures."""

        return _dedupe([*self.errors, *self.invariant_errors])

    def _finalize_common(
        self,
        *,
        paused: bool,
        cancel_requested: bool,
        invalid_input: bool,
        validate_outputs: bool,
        extra_structural_errors: Sequence[str] = (),
        extra_completion_errors: Sequence[str] = (),
    ) -> None:
        structural_errors = list(extra_structural_errors)
        output_errors: List[str] = list(extra_completion_errors)
        if self.errors:
            output_errors.append(f"reported errors: {len(self.errors)}")

        counts = {
            "expected": self.expected,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "validated_resumed": self.validated_resumed,
        }
        for name, value in counts.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                structural_errors.append(f"{name} must be a non-negative integer")

        expected = _count_value(self.expected)
        completed = self.completed_count
        accounted = self.accounted_count

        if expected <= 0:
            structural_errors.append("expected must be greater than zero")
        elif accounted != expected:
            structural_errors.append(
                f"count invariant mismatch: accounted={accounted} expected={expected}"
            )
        if completed > expected:
            structural_errors.append(
                f"completion count exceeds expected: completed={completed} expected={expected}"
            )

        normalized_paths: List[str] = []
        invalid_paths: List[str] = []
        for raw_path in self.outputs:
            try:
                normalized_paths.append(os.fspath(raw_path))
            except TypeError:
                invalid_paths.append(str(raw_path))
        if len(normalized_paths) != completed:
            output_errors.append(
                f"output count mismatch: paths={len(normalized_paths)} completed={completed}"
            )
        if len(set(normalized_paths)) != len(normalized_paths):
            structural_errors.append("output paths must be unique")

        valid_count = 0
        if validate_outputs:
            for path in normalized_paths:
                try:
                    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
                        invalid_paths.append(path)
                        continue
                    valid_count += 1
                except OSError:
                    invalid_paths.append(path)
            if invalid_paths:
                output_errors.append(
                    f"invalid output files: {len(_dedupe(invalid_paths))} missing or zero-byte"
                )
        else:
            valid_count = len(normalized_paths)

        self.invalid_outputs = _dedupe(invalid_paths)
        self.valid_output_count = valid_count
        self.outputs_validated = bool(validate_outputs)
        self.invariant_errors = _dedupe([*structural_errors, *output_errors])

        # Explicit terminal intent wins over derived counts. Pause preserves a
        # resumable checkpoint, so it wins if pause and cancel arrive together.
        if invalid_input:
            status = PipelineStatus.INVALID_INPUT
        elif paused:
            status = PipelineStatus.PAUSED
        elif cancel_requested or _count_value(self.cancelled) > 0:
            status = PipelineStatus.CANCELLED
        elif structural_errors:
            status = PipelineStatus.FAILED
        elif (
            expected > 0
            and completed == expected
            and _count_value(self.failed) == 0
            and _count_value(self.skipped) == 0
            and _count_value(self.cancelled) == 0
            and not output_errors
            and valid_count == expected
        ):
            status = PipelineStatus.SUCCEEDED
        elif valid_count > 0:
            status = PipelineStatus.PARTIAL
        else:
            status = PipelineStatus.FAILED

        self.status = status
        self._finalized = True

    def _counts_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "is_success": self.is_success,
            "finalized": self._finalized,
            "expected": self.expected,
            "succeeded": self.succeeded,
            "produced_this_run": self.produced_this_run,
            "validated_resumed": self.validated_resumed,
            "failed": self.failed,
            "skipped": self.skipped,
            "cancelled": self.cancelled,
            "accounted": self.accounted_count,
            "completed": self.completed_count,
            "outputs": _json_safe(self.outputs),
            "outputs_validated": self.outputs_validated,
            "valid_output_count": self.valid_output_count,
            "invalid_outputs": _json_safe(self.invalid_outputs),
            "errors": _json_safe(self.errors),
            "invariant_errors": _json_safe(self.invariant_errors),
            "all_errors": _json_safe(self.all_errors),
        }


@dataclass
class StageResult(_ResultMixin):
    """Truth-bearing result for one pipeline stage."""

    name: str
    expected: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    validated_resumed: int = 0
    outputs: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    required: bool = True
    status: PipelineStatus = field(default=PipelineStatus.FAILED, init=False)
    invariant_errors: List[str] = field(default_factory=list, init=False)
    invalid_outputs: List[str] = field(default_factory=list, init=False)
    valid_output_count: int = field(default=0, init=False)
    outputs_validated: bool = field(default=False, init=False)
    _finalized: bool = field(default=False, init=False, repr=False)

    def finalize(
        self,
        *,
        paused: bool = False,
        cancel_requested: bool = False,
        invalid_input: bool = False,
        validate_outputs: bool = True,
    ) -> "StageResult":
        structural = [] if str(self.name).strip() else ["stage name must not be empty"]
        self._finalize_common(
            paused=paused,
            cancel_requested=cancel_requested,
            invalid_input=invalid_input,
            validate_outputs=validate_outputs,
            extra_structural_errors=structural,
        )
        return self

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "name": self.name,
            "required": self.required,
            **self._counts_dict(),
            "metadata": _json_safe(self.metadata),
        }
        return _json_safe(payload)


@dataclass
class PipelineResult(_ResultMixin):
    """Final verdict for a TC pipeline.

    Counts describe final outputs only; intermediate work belongs in
    ``stages``. Every required stage must itself be finalized successfully
    before the pipeline can become ``SUCCEEDED``.
    """

    pipeline: str
    expected: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    validated_resumed: int = 0
    outputs: List[str] = field(default_factory=list)
    stages: List[StageResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: PipelineStatus = field(default=PipelineStatus.FAILED, init=False)
    invariant_errors: List[str] = field(default_factory=list, init=False)
    invalid_outputs: List[str] = field(default_factory=list, init=False)
    valid_output_count: int = field(default=0, init=False)
    outputs_validated: bool = field(default=False, init=False)
    _finalized: bool = field(default=False, init=False, repr=False)

    @property
    def tc_label(self) -> str:
        """Compatibility/readability alias used by existing TC call sites."""

        return self.pipeline

    def finalize(
        self,
        *,
        paused: bool = False,
        cancel_requested: bool = False,
        invalid_input: bool = False,
        validate_outputs: bool = True,
    ) -> "PipelineResult":
        structural: List[str] = []
        completion_errors: List[str] = []
        if not str(self.pipeline).strip():
            structural.append("pipeline name must not be empty")

        stage_names = [str(stage.name) for stage in self.stages]
        if len(set(stage_names)) != len(stage_names):
            structural.append("stage names must be unique")
        for stage in self.stages:
            if not stage.required:
                continue
            if not stage._finalized:
                completion_errors.append(
                    f"required stage '{stage.name}' is not finalized"
                )
            elif not stage.is_success:
                completion_errors.append(
                    f"required stage '{stage.name}' status={stage.status.value}"
                )

        self._finalize_common(
            paused=paused,
            cancel_requested=cancel_requested,
            invalid_input=invalid_input,
            validate_outputs=validate_outputs,
            extra_structural_errors=structural,
            extra_completion_errors=completion_errors,
        )
        return self

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "pipeline": self.pipeline,
            **self._counts_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "metadata": _json_safe(self.metadata),
        }
        return _json_safe(payload)


def finalize_stage_result(result: StageResult, **kwargs: Any) -> StageResult:
    """Finalize a stage via a named helper suitable for pipeline call sites."""

    if not isinstance(result, StageResult):
        raise TypeError("result must be a StageResult")
    return result.finalize(**kwargs)


def finalize_pipeline_result(result: PipelineResult, **kwargs: Any) -> PipelineResult:
    """Finalize a pipeline via a named helper suitable for UI/CLI call sites."""

    if not isinstance(result, PipelineResult):
        raise TypeError("result must be a PipelineResult")
    return result.finalize(**kwargs)


# =====================================================================
# Helpers
# =====================================================================

def combined_stop_check(
    stop_check: StopFn,
    pause_check: Optional[PauseFn],
) -> Callable[[], bool]:
    """Return a stop callable that also triggers on pause.

    TC01-05 all use the same idiom: "user pressed Stop" OR "user pressed
    Pause" both mean "finish current output and exit gracefully". Encapsulated
    here so each pipeline can write `if stop(): break` and have pause work
    automatically.
    """
    def _stop() -> bool:
        if stop_check():
            return True
        if pause_check is not None and pause_check():
            return True
        return False
    return _stop


def safe_log(log_fn: LogFn, msg: str) -> None:
    """log_fn wrapper that swallows callback errors."""
    try:
        log_fn(msg)
    except Exception:
        pass


def safe_progress(progress_fn: ProgressFn, pct: float, info: str = "") -> None:
    """progress_fn wrapper that swallows callback errors."""
    try:
        progress_fn(float(pct), str(info))
    except Exception:
        pass


def safe_file(file_fn: Optional[FileFn], filename: str) -> None:
    """file_fn wrapper — only fires if the UI provided one."""
    if file_fn is None:
        return
    try:
        file_fn(str(filename))
    except Exception:
        pass

def invalid_video_inputs(paths: Sequence[str]) -> List[str]:
    """Return Product/Source paths whose extension is not a supported video."""

    return [
        str(path)
        for path in paths
        if Path(str(path)).suffix.lower() not in VIDEO_INPUT_EXTS
    ]


def normalized_role_path(path: str) -> str:
    """Canonicalize a user path for cross-role equality checks."""

    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path))))
    )


def overlapping_role_paths(
    products: Sequence[str],
    backgrounds: Sequence[str],
) -> List[str]:
    """Return Product paths also selected as Background.

    TC01-TC04 require semantically distinct foreground and background roles.
    Relative paths, case variants, and symlink aliases must not bypass this
    fail-closed invariant.
    """

    background_keys = {
        normalized_role_path(path)
        for path in backgrounds
        if str(path).strip()
    }
    return [
        str(path)
        for path in products
        if str(path).strip() and normalized_role_path(path) in background_keys
    ]


def normalize_run_seed(value: Any, *, maximum: int = RUN_SEED_MAX) -> Optional[int]:
    """Validate the UI run-seed contract without generating an auto seed.

    Zero or blank returns ``None`` (auto). Positive integral values are
    reproducible. Booleans, negatives, fractional values, and values above the
    UI limit are rejected instead of silently changing the user's request.
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("run seed must be an integer from 0 to 999999")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("run seed must be an integer from 0 to 999999") from exc
    if not numeric.is_integer():
        raise ValueError("run seed must be an integer from 0 to 999999")
    seed = int(numeric)
    if seed < 0 or seed > maximum:
        raise ValueError(f"run seed must be between 0 and {maximum}")
    return None if seed == 0 else seed


def resolve_run_seed(value: Any, *, maximum: int = RUN_SEED_MAX) -> int:
    """Return the effective seed: generated for auto, unchanged for fixed."""

    requested = normalize_run_seed(value, maximum=maximum)
    return time.time_ns() if requested is None else requested



def shuffle_pool(items: Sequence[Any], rng: random.Random) -> List[Any]:
    """Return a shuffled copy of `items`. Never returns the input list itself."""
    out = list(items)
    rng.shuffle(out)
    return out


def apply_seed(seed: Optional[int] = None) -> random.Random:
    """Build a fresh `random.Random` instance.

    If `seed` is None, use `time.time_ns()` so each run is non-deterministic.
    If `seed` is provided (user-supplied or from a TC run log), the pipeline
    is reproducible.
    """
    if seed is None:
        seed = time.time_ns()
    return random.Random(seed)
