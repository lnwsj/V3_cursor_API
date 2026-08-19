"""Privacy-safe, fail-open runtime metrics for one accepted render lifecycle.

The collector deliberately keeps high-cardinality and sensitive values local.
Paths are accepted only long enough to sum output sizes; commands, filenames,
stderr, exception text, account names, and machine identifiers never enter a
snapshot.  Network payload validation remains the responsibility of
``core.usage_stats``.

This module has no mandatory dependency on :mod:`psutil`.  When psutil is not
available (or process inspection is denied), rendering continues unchanged and
the resource section reports an explicit unavailable/partial state.
"""
from __future__ import annotations

import copy
import concurrent.futures
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

from .gpu_resource import sample_gpu_resource, sanitize_gpu_sample

try:  # Optional at source-runtime level; release builds may pin psutil.
    import psutil as _DEFAULT_PSUTIL  # type: ignore
except Exception:  # pragma: no cover - exercised through dependency injection.
    _DEFAULT_PSUTIL = None


METRICS_VERSION = 3
MAX_COUNT = 100_000_000
MAX_BYTES = (1 << 63) - 1
MAX_DURATION_MS = 31_536_000_000  # one year
MAX_MEDIA_DURATION_MS = 31_536_000_000_000  # aggregate, one thousand years
MAX_RATE = 10_000.0
MAX_RETURN_CODE = (1 << 31) - 1
MIN_RETURN_CODE = -(1 << 31)
MAX_ENCODER_ROWS = 16
MAX_STAGE_ROWS = 16
MAX_RESOURCE_SAMPLES = 120
MAX_FINISHED_COLLECTORS = 128

COLLECTION_STATUSES = frozenset({"COMPLETE", "PARTIAL", "UNAVAILABLE"})
SAMPLER_STATUSES = frozenset({"OK", "NO_SAMPLE", "UNAVAILABLE", "ERROR"})
WATCHDOG_TYPES = frozenset({"NONE", "IDLE", "WALL"})
CANCEL_REASONS = frozenset(
    {"NONE", "USER_STOP", "USER_PAUSE", "APP_SHUTDOWN"}
)
FAILURE_CODES = frozenset(
    {
        "NONE",
        "USER_CANCELLED",
        "USER_PAUSED",
        "APP_SHUTDOWN",
        "INVALID_INPUT",
        "WATCHDOG_IDLE",
        "WATCHDOG_WALL",
        "ENCODER_PROCESS_FAILED",
        "CPU_FALLBACK_EXHAUSTED",
        "OUTPUT_VALIDATION_FAILED",
        "PIPELINE_INVARIANT",
        "UNKNOWN",
    }
)
FAILURE_STAGES = frozenset(
    {
        "NONE",
        "REFRAME",
        "CHROMA",
        "BATCH_CHROMA",
        "AUDIO_MASTER",
        "PIPELINE",
        "UNKNOWN",
    }
)
ATTEMPT_KINDS = frozenset({"primary", "cpu_fallback", "auxiliary"})

OUTPUT_ENCODERS = frozenset(
    {
        "libx264",
        "h264_nvenc",
        "hevc_nvenc",
        "av1_nvenc",
        "h264_qsv",
        "h264_amf",
        "h264_videotoolbox",
        "hevc_videotoolbox",
    }
)
HARDWARE_ENCODERS = frozenset(OUTPUT_ENCODERS - {"libx264"})
ENCODER_IDS = frozenset({*OUTPUT_ENCODERS, "copy", "png", "unknown"})
STAGE_IDS = frozenset(
    {"reframe", "chroma", "batch_chroma", "audio_master", "unknown"}
)
STAGE_ORDER = ("reframe", "chroma", "batch_chroma", "audio_master", "unknown")
STAGE_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "CANCELLED",
        "PAUSED",
        "INVALID_INPUT",
        "UNKNOWN",
    }
)

_RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SPEED_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)x$", re.IGNORECASE)
_CURRENT_RUN_ID: ContextVar[str] = ContextVar(
    "greenpc_run_metrics_client_run_id", default=""
)
_USE_DEFAULT_PSUTIL = object()
_USE_DEFAULT_GPU_SAMPLER = object()


MEDIA_FIELDS = frozenset(
    {
        "input_bytes",
        "output_bytes",
        "produced_duration_ms",
        "primary_duration_count",
        "primary_duration_ms_total",
        "primary_duration_ms_min",
        "primary_duration_ms_max",
        "audio_duration_count",
        "audio_duration_ms_total",
        "audio_duration_ms_min",
        "audio_duration_ms_max",
        "duration_probe_failure_count",
    }
)
ENCODING_FIELDS = frozenset(
    {
        "attempt_count",
        "success_count",
        "failed_count",
        "cancelled_count",
        "wall_ms_sum",
        "wall_span_ms",
        "encoded_media_ms_sum",
        "peak_parallel",
        "speed_x_avg",
        "speed_x_p95",
        "encode_fps_avg",
        "actual_encoder",
        "encoder_attempts",
        "hardware_attempt_count",
        "cpu_fallback_trigger_count",
        "cpu_fallback_success_count",
        "cpu_fallback_failure_count",
    }
)
RESOURCE_FIELDS = frozenset(
    {
        "sampler_status",
        "sample_count",
        "coverage_ms",
        "cpu_limit_pct",
        "logical_cpu_count",
        "ram_total_bytes",
        "ffmpeg_cpu_avg_pct",
        "ffmpeg_cpu_peak_pct",
        "ffmpeg_rss_avg_bytes",
        "ffmpeg_rss_peak_bytes",
    }
)
FAILURE_FIELDS = frozenset(
    {
        "code",
        "stage",
        "ffmpeg_return_code",
        "watchdog",
        "fallback_exhausted",
        "cancel_reason",
    }
)
SNAPSHOT_FIELDS = frozenset(
    {
        "metrics_version",
        "collection_status",
        "media",
        "encoding",
        "resources",
        "failure",
        "stage_metrics",
        "encoder_metrics",
        "resource_samples",
    }
)
ENCODER_ROW_FIELDS = frozenset(
    {"encoder", "attempts", "successes", "failures"}
)
STAGE_METRIC_FIELDS = frozenset(
    {
        "stage",
        "status",
        "expected",
        "succeeded",
        "failed",
        "skipped",
        "cancelled",
        "duration_ms",
    }
)
ENCODER_METRIC_FIELDS = frozenset(
    {
        "encoder",
        "attempts",
        "successes",
        "failures",
        "wall_ms",
        "encoded_media_ms",
        "speed_x_avg",
    }
)
RESOURCE_SAMPLE_FIELDS = frozenset(
    {
        "offset_ms",
        "cpu_percent",
        "ram_mb",
        "gpu_percent",
        "gpu_memory_mb",
    }
)


def _bounded_int(value: Any, low: int, high: int, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _bounded_float_or_none(
    value: Any,
    low: float = 0.0,
    high: float = MAX_RATE,
) -> Optional[float]:
    try:
        if isinstance(value, bool) or value is None:
            return None
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return max(low, min(high, number))


def _rounded(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 3)


def _normalize_run_id(value: Any) -> str:
    run_id = str(value or "").strip().casefold()
    return run_id if _RUN_ID_RE.fullmatch(run_id) else ""


def _normalize_encoder(value: Any) -> str:
    encoder = str(value or "").strip().casefold()
    if encoder == "auto" or encoder not in ENCODER_IDS:
        return "unknown"
    return encoder


def _normalize_attempt_kind(value: Any) -> str:
    kind = str(value or "").strip().casefold()
    return kind if kind in ATTEMPT_KINDS else "primary"


def _parse_speed(value: Any) -> Optional[float]:
    if isinstance(value, str):
        match = _SPEED_RE.fullmatch(value.strip())
        if match is None:
            return None
        value = match.group(1)
    return _bounded_float_or_none(value)


def _enum_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().upper()


def _safe_duration_pair(
    summary: Mapping[str, Any], prefix: str
) -> Dict[str, Optional[int]]:
    count = _bounded_int(summary.get(f"{prefix}_duration_count"), 0, MAX_COUNT)
    total = _bounded_int(
        summary.get(f"{prefix}_duration_ms_total"), 0, MAX_DURATION_MS
    )
    minimum_raw = summary.get(f"{prefix}_duration_ms_min")
    maximum_raw = summary.get(f"{prefix}_duration_ms_max")
    minimum = (
        _bounded_int(minimum_raw, 0, MAX_DURATION_MS)
        if count > 0 and minimum_raw is not None
        else None
    )
    maximum = (
        _bounded_int(maximum_raw, 0, MAX_DURATION_MS)
        if count > 0 and maximum_raw is not None
        else None
    )
    if minimum is not None and maximum is not None and minimum > maximum:
        minimum, maximum = maximum, minimum
    return {
        f"{prefix}_duration_count": count,
        f"{prefix}_duration_ms_total": total,
        f"{prefix}_duration_ms_min": minimum,
        f"{prefix}_duration_ms_max": maximum,
    }


def sanitize_media_summary(value: Any) -> Dict[str, Any]:
    """Return the exact numeric input-media aggregate accepted by snapshots."""

    source = value if isinstance(value, Mapping) else {}
    media: Dict[str, Any] = {
        "input_bytes": _bounded_int(source.get("input_bytes"), 0, MAX_BYTES),
        "output_bytes": 0,
        "produced_duration_ms": None,
        **_safe_duration_pair(source, "primary"),
        **_safe_duration_pair(source, "audio"),
        "duration_probe_failure_count": _bounded_int(
            source.get("duration_probe_failure_count"), 0, MAX_COUNT
        ),
    }
    return media


def probe_produced_duration_ms(
    paths: Any,
    *,
    ffprobe_cmd: str = "ffprobe",
    timeout_sec: float = 8.0,
    max_workers: int = 6,
) -> Optional[int]:
    """Return the summed final-output duration without retaining media paths.

    Coverage is fail-closed: if any distinct final output is missing, cannot be
    probed, or returns a non-positive/non-finite duration, the aggregate is
    ``None`` rather than an understated total. Probes run on the render worker
    and use bounded concurrency/timeouts, so Tk and telemetry delivery remain
    non-blocking.
    """

    if isinstance(paths, (str, bytes, Mapping)) or paths is None:
        return None
    unique: List[str] = []
    seen: set[str] = set()
    try:
        candidates = list(paths)
    except (TypeError, ValueError):
        return None
    for raw in candidates[:10_000]:
        try:
            path = os.fspath(raw)
            if not isinstance(path, str) or not path or not os.path.isfile(path):
                return None
            key = os.path.normcase(os.path.abspath(path))
        except (OSError, TypeError, ValueError):
            return None
        if key not in seen:
            seen.add(key)
            unique.append(path)
    if not unique:
        return None

    timeout = max(0.25, min(30.0, float(timeout_sec or 8.0)))

    def one_duration(path: str) -> Optional[int]:
        try:
            completed = subprocess.run(
                [
                    str(ffprobe_cmd or "ffprobe"),
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
            seconds = float((completed.stdout or "").strip())
            if completed.returncode != 0 or not math.isfinite(seconds) or seconds <= 0:
                return None
            return _bounded_int(round(seconds * 1000.0), 1, MAX_DURATION_MS)
        except Exception:
            return None

    workers = max(1, min(8, int(max_workers or 1), len(unique)))
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="metrics-probe-"
        ) as executor:
            durations = list(executor.map(one_duration, unique))
    except Exception:
        return None
    if any(value is None for value in durations):
        return None
    return _bounded_int(
        sum(int(value or 0) for value in durations), 0, MAX_MEDIA_DURATION_MS
    )


@contextmanager
def bind_run_metrics(client_run_id: Any) -> Iterator[str]:
    """Bind one valid run id to this execution context and restore on exit."""

    run_id = _normalize_run_id(client_run_id)
    token = _CURRENT_RUN_ID.set(run_id)
    try:
        yield run_id
    finally:
        _CURRENT_RUN_ID.reset(token)


def current_run_id() -> str:
    """Return the currently bound privacy-safe run id, or an empty string."""

    return _CURRENT_RUN_ID.get()


@dataclass
class _Attempt:
    attempt_id: str
    encoder: str
    kind: str
    started_monotonic: float
    expected_duration_ms: int
    pid: Optional[int] = None
    finished_monotonic: Optional[float] = None
    success: Optional[bool] = None
    returncode: Optional[int] = None
    cancelled: bool = False
    watchdog: str = "NONE"
    out_time_us: int = 0
    speed_x: Optional[float] = None
    encode_fps: Optional[float] = None
    total_size: int = 0

    @property
    def terminal(self) -> bool:
        return self.finished_monotonic is not None

    def wall_ms(self, fallback_end: float) -> int:
        end = self.finished_monotonic
        if end is None:
            end = fallback_end
        return _bounded_int(
            round(max(0.0, end - self.started_monotonic) * 1000.0),
            0,
            MAX_DURATION_MS,
        )


class RunMetricsCollector:
    """Thread-safe aggregate state for exactly one top-level render run."""

    def __init__(
        self,
        client_run_id: str,
        media_summary: Any = None,
        *,
        sample_interval_sec: float = 0.5,
        psutil_module: Any = _DEFAULT_PSUTIL,
        gpu_sampler: Any = sample_gpu_resource,
        clock: Any = time.monotonic,
        cpu_limit_pct: int = 0,
    ) -> None:
        run_id = _normalize_run_id(client_run_id)
        if not run_id:
            raise ValueError("client_run_id must be 32 lowercase hex characters")
        self.client_run_id = run_id
        self._lock = threading.RLock()
        self._clock = clock if callable(clock) else time.monotonic
        self._media = sanitize_media_summary(media_summary)
        self._sample_interval_sec = max(
            0.05,
            min(60.0, float(sample_interval_sec or 0.5)),
        )
        self._psutil = psutil_module
        self._gpu_sampler = gpu_sampler if callable(gpu_sampler) else None
        self._cpu_limit_pct = _bounded_int(cpu_limit_pct, 0, 100)
        self._accepted = False
        self._accepted_monotonic = 0.0
        self._finished_snapshot: Optional[Dict[str, Any]] = None

        self._attempts: "OrderedDict[str, _Attempt]" = OrderedDict()
        self._active_attempts = 0
        self._peak_parallel = 0
        self._fallback_trigger_count = 0

        self._root_pids: set[int] = set()
        self._process_cache: Dict[int, Any] = {}
        self._stop_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None
        self._sampler_had_error = False
        self._sample_count = 0
        self._sample_cpu_sum = 0.0
        self._sample_cpu_peak = 0.0
        self._sample_rss_sum = 0
        self._sample_rss_peak = 0
        self._first_sample_monotonic: Optional[float] = None
        self._last_sample_monotonic: Optional[float] = None
        self._resource_samples: List[Dict[str, Any]] = []
        self._logical_cpu_count = self._read_logical_cpu_count()
        self._ram_total_bytes = self._read_ram_total()

    def _read_logical_cpu_count(self) -> Optional[int]:
        if self._psutil is None:
            return None
        try:
            value = self._psutil.cpu_count(logical=True)
        except Exception:
            return None
        if value is None:
            return None
        bounded = _bounded_int(value, 1, 4096)
        return bounded or None

    def _read_ram_total(self) -> Optional[int]:
        if self._psutil is None:
            return None
        try:
            value = self._psutil.virtual_memory().total
        except Exception:
            return None
        bounded = _bounded_int(value, 1, MAX_BYTES)
        return bounded or None

    @property
    def is_finished(self) -> bool:
        with self._lock:
            return self._finished_snapshot is not None

    @property
    def sampler_alive(self) -> bool:
        thread = self._sampler_thread
        return bool(thread is not None and thread.is_alive())

    def accept(self, accepted_monotonic: Optional[float] = None) -> bool:
        with self._lock:
            if self._finished_snapshot is not None:
                return False
            if self._accepted:
                return True
            now = _bounded_float_or_none(
                accepted_monotonic,
                0.0,
                float("1e100"),
            )
            self._accepted_monotonic = now if now is not None else self._clock()
            self._accepted = True
            if self._psutil is None:
                return True
            self._sampler_thread = threading.Thread(
                target=self._sample_loop,
                name=f"run-metrics-{self.client_run_id[:8]}",
                daemon=True,
            )
            self._sampler_thread.start()
            return True

    def _sample_loop(self) -> None:
        while not self._stop_event.wait(self._sample_interval_sec):
            try:
                self.sample_now()
            except Exception:
                with self._lock:
                    self._sampler_had_error = True

    def begin_attempt(
        self,
        encoder: Any,
        *,
        expected_duration_sec: Any = 0.0,
        attempt_kind: Any = "primary",
        started_monotonic: Optional[float] = None,
    ) -> str:
        with self._lock:
            if self._finished_snapshot is not None:
                return ""
            attempt_id = uuid.uuid4().hex
            started = _bounded_float_or_none(
                started_monotonic,
                0.0,
                float("1e100"),
            )
            expected = _bounded_float_or_none(
                expected_duration_sec,
                0.0,
                MAX_DURATION_MS / 1000.0,
            )
            self._attempts[attempt_id] = _Attempt(
                attempt_id=attempt_id,
                encoder=_normalize_encoder(encoder),
                kind=_normalize_attempt_kind(attempt_kind),
                started_monotonic=started if started is not None else self._clock(),
                expected_duration_ms=_bounded_int(
                    round((expected or 0.0) * 1000.0),
                    0,
                    MAX_DURATION_MS,
                ),
            )
            self._active_attempts += 1
            self._peak_parallel = max(self._peak_parallel, self._active_attempts)
            return attempt_id

    def register_process(self, attempt_id: str, pid: Any) -> bool:
        pid_value = _bounded_int(pid, 1, MAX_RETURN_CODE)
        if not pid_value:
            return False
        with self._lock:
            attempt = self._attempts.get(str(attempt_id or ""))
            if attempt is None or attempt.terminal:
                return False
            attempt.pid = pid_value
            self._root_pids.add(pid_value)
            return True

    def unregister_process(self, attempt_id: str, pid: Any = None) -> None:
        with self._lock:
            attempt = self._attempts.get(str(attempt_id or ""))
            pid_value = _bounded_int(
                pid if pid is not None else getattr(attempt, "pid", None),
                1,
                MAX_RETURN_CODE,
            )
            if pid_value:
                self._root_pids.discard(pid_value)

    def progress(
        self,
        attempt_id: str,
        *,
        out_time_us: Any = None,
        speed: Any = None,
        fps: Any = None,
        total_size: Any = None,
    ) -> bool:
        with self._lock:
            attempt = self._attempts.get(str(attempt_id or ""))
            if attempt is None or attempt.terminal:
                return False
            if out_time_us is not None:
                attempt.out_time_us = _bounded_int(
                    out_time_us, 0, MAX_DURATION_MS * 1000
                )
            parsed_speed = _parse_speed(speed)
            if parsed_speed is not None:
                attempt.speed_x = parsed_speed
            parsed_fps = _bounded_float_or_none(fps)
            if parsed_fps is not None:
                attempt.encode_fps = parsed_fps
            if total_size is not None:
                attempt.total_size = _bounded_int(total_size, 0, MAX_BYTES)
            return True

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        success: Any,
        returncode: Any = None,
        cancelled: Any = False,
        watchdog: Any = "NONE",
        finished_monotonic: Optional[float] = None,
    ) -> bool:
        with self._lock:
            attempt = self._attempts.get(str(attempt_id or ""))
            if attempt is None or attempt.terminal:
                return False
            finished = _bounded_float_or_none(
                finished_monotonic,
                0.0,
                float("1e100"),
            )
            attempt.finished_monotonic = (
                finished if finished is not None else self._clock()
            )
            attempt.success = bool(success)
            if returncode is not None and not isinstance(returncode, bool):
                attempt.returncode = _bounded_int(
                    returncode,
                    MIN_RETURN_CODE,
                    MAX_RETURN_CODE,
                    default=-1,
                )
            attempt.cancelled = bool(cancelled)
            watchdog_value = _enum_text(watchdog)
            attempt.watchdog = (
                watchdog_value if watchdog_value in WATCHDOG_TYPES else "NONE"
            )
            if attempt.pid is not None:
                self._root_pids.discard(attempt.pid)
            self._active_attempts = max(0, self._active_attempts - 1)
            return True

    def record_fallback_trigger(self) -> None:
        with self._lock:
            if self._finished_snapshot is None:
                self._fallback_trigger_count = min(
                    MAX_COUNT, self._fallback_trigger_count + 1
                )

    def _process_for_pid(self, pid: int, candidate: Any = None) -> Any:
        process = self._process_cache.get(pid)
        if process is not None:
            return process
        process = candidate if candidate is not None else self._psutil.Process(pid)
        self._process_cache[pid] = process
        return process

    def _sample_gpu_values(self) -> Dict[str, Optional[float]]:
        """Return only bounded aggregate GPU values; provider errors are null."""

        if self._gpu_sampler is None:
            return {"gpu_percent": None, "gpu_memory_mb": None}
        try:
            return sanitize_gpu_sample(self._gpu_sampler())
        except Exception:
            return {"gpu_percent": None, "gpu_memory_mb": None}

    @staticmethod
    def _compact_resource_samples(
        samples: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Adaptively thin bounded samples while retaining first and latest."""

        compacted = list(samples)
        while len(compacted) > MAX_RESOURCE_SAMPLES:
            compacted = [
                compacted[0],
                *compacted[1:-1:2],
                compacted[-1],
            ]
        return compacted

    def _append_resource_sample(
        self,
        *,
        sampled_monotonic: float,
        cpu_percent: Any,
        ram_bytes: Any,
        gpu_values: Mapping[str, Any],
    ) -> None:
        if not self._accepted or self._finished_snapshot is not None:
            return
        offset_ms = _bounded_int(
            round(
                max(0.0, sampled_monotonic - self._accepted_monotonic)
                * 1000.0
            ),
            0,
            MAX_DURATION_MS,
        )
        sample = {
            "offset_ms": offset_ms,
            "cpu_percent": _rounded(
                _bounded_float_or_none(cpu_percent, 0.0, 100.0)
            ),
            "ram_mb": _rounded(
                _bounded_float_or_none(
                    float(ram_bytes) / (1024.0 * 1024.0),
                    0.0,
                    4_194_304.0,
                )
            ) if ram_bytes is not None else None,
            "gpu_percent": _rounded(
                _bounded_float_or_none(
                    gpu_values.get("gpu_percent"), 0.0, 100.0
                )
            ),
            "gpu_memory_mb": _rounded(
                _bounded_float_or_none(
                    gpu_values.get("gpu_memory_mb"),
                    0.0,
                    1_048_576.0,
                )
            ),
        }
        if all(
            sample[key] is None
            for key in (
                "cpu_percent",
                "ram_mb",
                "gpu_percent",
                "gpu_memory_mb",
            )
        ):
            return
        if self._resource_samples:
            latest_offset = int(self._resource_samples[-1]["offset_ms"])
            if offset_ms < latest_offset:
                return
            if offset_ms == latest_offset:
                self._resource_samples[-1] = sample
                return
        self._resource_samples.append(sample)
        self._resource_samples = self._compact_resource_samples(
            self._resource_samples
        )

    def sample_now(self) -> bool:
        """Sample registered FFmpeg process trees once without blocking."""

        if self._psutil is None:
            return False
        with self._lock:
            root_pids = tuple(self._root_pids)
        if not root_pids:
            return False

        processes: Dict[int, Any] = {}
        error_seen = False
        for root_pid in root_pids:
            try:
                root = self._process_for_pid(root_pid)
                processes[root_pid] = root
                children = root.children(recursive=True)
            except Exception:
                error_seen = True
                continue
            for child in children or ():
                try:
                    child_pid = _bounded_int(getattr(child, "pid", 0), 1, MAX_RETURN_CODE)
                    if child_pid:
                        processes[child_pid] = self._process_for_pid(
                            child_pid, candidate=child
                        )
                except Exception:
                    error_seen = True

        raw_cpu = 0.0
        rss = 0
        readable = 0
        cpu_readable = 0
        rss_readable = 0
        for process in processes.values():
            process_readable = False
            try:
                cpu = _bounded_float_or_none(process.cpu_percent(interval=None))
                if cpu is not None:
                    raw_cpu += cpu
                    cpu_readable += 1
                    process_readable = True
            except Exception:
                error_seen = True
            try:
                process_rss = _bounded_int(
                    process.memory_info().rss, 0, MAX_BYTES
                )
                rss = min(MAX_BYTES, rss + process_rss)
                rss_readable += 1
                process_readable = True
            except Exception:
                error_seen = True
            if process_readable:
                readable += 1

        if readable <= 0:
            if error_seen:
                with self._lock:
                    self._sampler_had_error = True
            return False

        logical = self._logical_cpu_count or 1
        normalized_cpu = (
            max(0.0, min(100.0, raw_cpu / logical))
            if cpu_readable > 0
            else None
        )
        gpu_values = self._sample_gpu_values()
        now = self._clock()
        with self._lock:
            self._sample_count = min(MAX_COUNT, self._sample_count + 1)
            aggregate_cpu = normalized_cpu or 0.0
            self._sample_cpu_sum += aggregate_cpu
            self._sample_cpu_peak = max(self._sample_cpu_peak, aggregate_cpu)
            self._sample_rss_sum = min(MAX_BYTES * MAX_COUNT, self._sample_rss_sum + rss)
            self._sample_rss_peak = max(self._sample_rss_peak, rss)
            if self._first_sample_monotonic is None:
                self._first_sample_monotonic = now
            self._last_sample_monotonic = now
            self._sampler_had_error = self._sampler_had_error or error_seen
            self._append_resource_sample(
                sampled_monotonic=now,
                cpu_percent=normalized_cpu,
                ram_bytes=rss if rss_readable > 0 else None,
                gpu_values=gpu_values,
            )
        return True

    def stop_sampler(self, timeout: float = 0.25) -> None:
        self._stop_event.set()
        thread = self._sampler_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, min(1.0, float(timeout))))

    def _sampler_status(self) -> str:
        if self._psutil is None:
            return "UNAVAILABLE"
        if self._sample_count > 0:
            return "OK"
        if self._sampler_had_error:
            return "ERROR"
        return "NO_SAMPLE"

    def _resource_snapshot(self) -> Dict[str, Any]:
        count = self._sample_count
        coverage_ms = 0
        if (
            self._first_sample_monotonic is not None
            and self._last_sample_monotonic is not None
        ):
            coverage_ms = _bounded_int(
                round(
                    max(
                        0.0,
                        self._last_sample_monotonic
                        - self._first_sample_monotonic,
                    )
                    * 1000.0
                ),
                0,
                MAX_DURATION_MS,
            )
        return {
            "sampler_status": self._sampler_status(),
            "sample_count": count,
            "coverage_ms": coverage_ms,
            "cpu_limit_pct": self._cpu_limit_pct,
            "logical_cpu_count": self._logical_cpu_count,
            "ram_total_bytes": self._ram_total_bytes,
            "ffmpeg_cpu_avg_pct": (
                _rounded(self._sample_cpu_sum / count) if count else None
            ),
            "ffmpeg_cpu_peak_pct": (
                _rounded(self._sample_cpu_peak) if count else None
            ),
            "ffmpeg_rss_avg_bytes": (
                _bounded_int(round(self._sample_rss_sum / count), 0, MAX_BYTES)
                if count
                else None
            ),
            "ffmpeg_rss_peak_bytes": (
                _bounded_int(self._sample_rss_peak, 0, MAX_BYTES)
                if count
                else None
            ),
        }

    def _resource_samples_snapshot(self) -> List[Dict[str, Any]]:
        return [
            {
                "offset_ms": item["offset_ms"],
                "cpu_percent": item["cpu_percent"],
                "ram_mb": item["ram_mb"],
                "gpu_percent": item["gpu_percent"],
                "gpu_memory_mb": item["gpu_memory_mb"],
            }
            for item in self._resource_samples
        ]

    def _encoding_snapshot(self, finished_monotonic: float) -> Dict[str, Any]:
        attempts = list(self._attempts.values())
        success_count = sum(attempt.success is True for attempt in attempts)
        cancelled_count = sum(attempt.cancelled for attempt in attempts)
        failed_count = len(attempts) - success_count - cancelled_count
        wall_values = [
            attempt.wall_ms(finished_monotonic) for attempt in attempts
        ]
        if attempts:
            first = min(item.started_monotonic for item in attempts)
            last = max(
                item.finished_monotonic or finished_monotonic for item in attempts
            )
            wall_span_ms = _bounded_int(
                round(max(0.0, last - first) * 1000.0),
                0,
                MAX_DURATION_MS,
            )
        else:
            wall_span_ms = 0

        speed_values = [
            item.speed_x for item in attempts if item.speed_x is not None
        ]
        fps_values = [
            item.encode_fps for item in attempts if item.encode_fps is not None
        ]
        speed_sorted = sorted(float(item) for item in speed_values)
        speed_p95 = None
        if speed_sorted:
            index = max(0, math.ceil(len(speed_sorted) * 0.95) - 1)
            speed_p95 = speed_sorted[index]

        encoder_rows: List[Dict[str, Any]] = []
        for encoder in sorted({attempt.encoder for attempt in attempts}):
            matching = [item for item in attempts if item.encoder == encoder]
            successes = sum(item.success is True for item in matching)
            encoder_rows.append(
                {
                    "encoder": encoder,
                    "attempts": len(matching),
                    "successes": successes,
                    "failures": len(matching) - successes,
                }
            )
        encoder_rows = encoder_rows[:MAX_ENCODER_ROWS]

        successful_output_encoders = {
            item.encoder
            for item in attempts
            if item.success is True and item.encoder in OUTPUT_ENCODERS
        }
        if len(successful_output_encoders) == 1:
            actual_encoder: Optional[str] = next(iter(successful_output_encoders))
        elif len(successful_output_encoders) > 1:
            actual_encoder = "MIXED"
        else:
            actual_encoder = None

        fallback_attempts = [
            item for item in attempts if item.kind == "cpu_fallback"
        ]
        fallback_successes = sum(
            item.success is True for item in fallback_attempts
        )
        fallback_failures = len(fallback_attempts) - fallback_successes
        return {
            "attempt_count": len(attempts),
            "success_count": success_count,
            "failed_count": max(0, failed_count),
            "cancelled_count": cancelled_count,
            "wall_ms_sum": _bounded_int(
                sum(wall_values), 0, MAX_DURATION_MS
            ),
            "wall_span_ms": wall_span_ms,
            "encoded_media_ms_sum": _bounded_int(
                sum(item.out_time_us // 1000 for item in attempts),
                0,
                MAX_DURATION_MS,
            ),
            "peak_parallel": _bounded_int(
                self._peak_parallel, 0, MAX_COUNT
            ),
            "speed_x_avg": (
                _rounded(sum(speed_values) / len(speed_values))
                if speed_values
                else None
            ),
            "speed_x_p95": _rounded(speed_p95),
            "encode_fps_avg": (
                _rounded(sum(fps_values) / len(fps_values))
                if fps_values
                else None
            ),
            "actual_encoder": actual_encoder,
            "encoder_attempts": encoder_rows,
            "hardware_attempt_count": sum(
                item.encoder in HARDWARE_ENCODERS for item in attempts
            ),
            "cpu_fallback_trigger_count": self._fallback_trigger_count,
            "cpu_fallback_success_count": fallback_successes,
            "cpu_fallback_failure_count": fallback_failures,
        }

    def _encoder_metrics_snapshot(
        self, finished_monotonic: float
    ) -> List[Dict[str, Any]]:
        """Return bounded per-output-encoder aggregates without commands."""

        result: List[Dict[str, Any]] = []
        attempts = list(self._attempts.values())
        for encoder in sorted(
            {item.encoder for item in attempts if item.encoder in OUTPUT_ENCODERS}
        ):
            matching = [item for item in attempts if item.encoder == encoder]
            speeds = [
                float(item.speed_x)
                for item in matching
                if item.speed_x is not None
            ]
            successes = sum(item.success is True for item in matching)
            result.append(
                {
                    "encoder": encoder,
                    "attempts": _bounded_int(len(matching), 0, MAX_COUNT),
                    "successes": _bounded_int(successes, 0, MAX_COUNT),
                    "failures": _bounded_int(
                        len(matching) - successes, 0, MAX_COUNT
                    ),
                    "wall_ms": _bounded_int(
                        sum(item.wall_ms(finished_monotonic) for item in matching),
                        0,
                        MAX_DURATION_MS,
                    ),
                    "encoded_media_ms": _bounded_int(
                        sum(item.out_time_us // 1000 for item in matching),
                        0,
                        MAX_DURATION_MS,
                    ),
                    "speed_x_avg": (
                        _rounded(sum(speeds) / len(speeds)) if speeds else None
                    ),
                }
            )
        return result[:MAX_ENCODER_ROWS]

    @staticmethod
    def _pipeline_value(pipeline_result: Any, name: str, default: Any = None) -> Any:
        if isinstance(pipeline_result, Mapping):
            return pipeline_result.get(name, default)
        return getattr(pipeline_result, name, default)

    @classmethod
    def _failure_stage(cls, status: str, pipeline_result: Any) -> str:
        if status == "SUCCEEDED":
            return "NONE"
        stages = cls._pipeline_value(pipeline_result, "stages", ())
        if isinstance(stages, Iterable) and not isinstance(stages, (str, bytes, Mapping)):
            for stage in stages:
                stage_status = _enum_text(cls._pipeline_value(stage, "status", ""))
                if stage_status == "SUCCEEDED":
                    continue
                name = _enum_text(cls._pipeline_value(stage, "name", ""))
                return name if name in FAILURE_STAGES else "UNKNOWN"
        return "PIPELINE"

    @classmethod
    def _pipeline_has_items(cls, pipeline_result: Any, name: str) -> bool:
        value = cls._pipeline_value(pipeline_result, name, ())
        if isinstance(value, Mapping):
            return bool(value)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            try:
                return any(True for _ in value)
            except Exception:
                return False
        return bool(value)

    @classmethod
    def _stage_duration_ms(cls, stage: Any) -> Optional[int]:
        metadata = cls._pipeline_value(stage, "metadata", {})
        if not isinstance(metadata, Mapping):
            return None
        for key in ("duration_ms", "elapsed_ms"):
            value = metadata.get(key)
            if value is not None and not isinstance(value, bool):
                try:
                    number = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if 0 <= number <= MAX_DURATION_MS:
                    return number
        value = metadata.get("elapsed_sec")
        seconds = _bounded_float_or_none(
            value, 0.0, MAX_DURATION_MS / 1000.0
        )
        return (
            _bounded_int(round(seconds * 1000.0), 0, MAX_DURATION_MS)
            if seconds is not None
            else None
        )

    @classmethod
    def _stage_metrics_snapshot(cls, pipeline_result: Any) -> List[Dict[str, Any]]:
        """Reduce PipelineResult stages to low-cardinality scalar facts."""

        stages = cls._pipeline_value(pipeline_result, "stages", ())
        if isinstance(stages, (str, bytes, Mapping)) or not isinstance(
            stages, Iterable
        ):
            return []
        result: List[Dict[str, Any]] = []
        seen_names: set[str] = set()
        try:
            candidates = list(stages)
        except (TypeError, ValueError):
            return []
        for stage in candidates[:MAX_STAGE_ROWS]:
            name = str(cls._pipeline_value(stage, "name", "") or "").strip().casefold()
            if name not in STAGE_IDS:
                name = "unknown"
            if name in seen_names:
                continue
            seen_names.add(name)
            status = _enum_text(cls._pipeline_value(stage, "status", ""))
            if status not in STAGE_STATUSES:
                status = "UNKNOWN"
            result.append(
                {
                    "stage": name,
                    "status": status,
                    "expected": _bounded_int(
                        cls._pipeline_value(stage, "expected", 0), 0, MAX_COUNT
                    ),
                    "succeeded": _bounded_int(
                        cls._pipeline_value(stage, "succeeded", 0), 0, MAX_COUNT
                    ),
                    "failed": _bounded_int(
                        cls._pipeline_value(stage, "failed", 0), 0, MAX_COUNT
                    ),
                    "skipped": _bounded_int(
                        cls._pipeline_value(stage, "skipped", 0), 0, MAX_COUNT
                    ),
                    "cancelled": _bounded_int(
                        cls._pipeline_value(stage, "cancelled", 0), 0, MAX_COUNT
                    ),
                    "duration_ms": cls._stage_duration_ms(stage),
                }
            )
        return sorted(result, key=lambda item: STAGE_ORDER.index(item["stage"]))

    def _failure_snapshot(
        self,
        *,
        status: Any,
        pipeline_result: Any,
        cancel_reason: Any,
        encoding: Mapping[str, Any],
    ) -> Dict[str, Any]:
        terminal_status = _enum_text(status)
        reason = _enum_text(cancel_reason)
        if reason not in CANCEL_REASONS:
            reason = "NONE"
        watchdog = "NONE"
        failing_returncode: Optional[int] = None
        for attempt in self._attempts.values():
            if attempt.watchdog in {"IDLE", "WALL"}:
                watchdog = attempt.watchdog
            if attempt.returncode not in (None, 0):
                failing_returncode = attempt.returncode

        fallback_exhausted = bool(
            terminal_status != "SUCCEEDED"
            and encoding["cpu_fallback_trigger_count"] > 0
            and encoding["cpu_fallback_success_count"]
            < encoding["cpu_fallback_trigger_count"]
        )
        if terminal_status == "SUCCEEDED":
            code = "NONE"
        elif terminal_status == "PAUSED":
            code = "USER_PAUSED"
            reason = "USER_PAUSE"
        elif terminal_status == "CANCELLED":
            code = "APP_SHUTDOWN" if reason == "APP_SHUTDOWN" else "USER_CANCELLED"
            if reason == "NONE":
                reason = "USER_STOP"
        elif terminal_status == "INVALID_INPUT":
            code = "INVALID_INPUT"
        elif watchdog == "IDLE":
            code = "WATCHDOG_IDLE"
        elif watchdog == "WALL":
            code = "WATCHDOG_WALL"
        elif fallback_exhausted:
            code = "CPU_FALLBACK_EXHAUSTED"
        elif encoding["failed_count"] > 0 or failing_returncode is not None:
            code = "ENCODER_PROCESS_FAILED"
        elif self._pipeline_has_items(pipeline_result, "invalid_outputs"):
            code = "OUTPUT_VALIDATION_FAILED"
        elif self._pipeline_has_items(pipeline_result, "invariant_errors"):
            code = "PIPELINE_INVARIANT"
        else:
            code = "UNKNOWN"

        return {
            "code": code,
            "stage": self._failure_stage(terminal_status, pipeline_result),
            "ffmpeg_return_code": failing_returncode,
            "watchdog": watchdog,
            "fallback_exhausted": fallback_exhausted,
            "cancel_reason": reason,
        }

    @classmethod
    def _output_paths(cls, explicit: Any, pipeline_result: Any) -> Iterable[Any]:
        if explicit is not None and not isinstance(explicit, (str, bytes)):
            try:
                for value in explicit:
                    yield value
            except TypeError:
                pass
        pipeline_outputs = cls._pipeline_value(pipeline_result, "outputs", ())
        if not isinstance(pipeline_outputs, (str, bytes, Mapping)):
            try:
                for value in pipeline_outputs:
                    yield value
            except TypeError:
                pass

    @classmethod
    def _sum_output_bytes(cls, explicit: Any, pipeline_result: Any) -> int:
        total = 0
        seen: set[str] = set()
        for raw in cls._output_paths(explicit, pipeline_result):
            try:
                path = os.fspath(raw)
                if not isinstance(path, str) or not path:
                    continue
                key = os.path.normcase(os.path.abspath(path))
                if key in seen or not os.path.isfile(path):
                    continue
                seen.add(key)
                total = min(MAX_BYTES, total + max(0, os.path.getsize(path)))
            except (OSError, TypeError, ValueError):
                continue
        return total

    def finish(
        self,
        *,
        status: Any,
        pipeline_result: Any = None,
        output_paths: Any = None,
        produced_duration_ms: Any = None,
        cancel_reason: Any = "NONE",
    ) -> Dict[str, Any]:
        self.stop_sampler()
        with self._lock:
            if self._finished_snapshot is not None:
                return copy.deepcopy(self._finished_snapshot)
            finished_monotonic = self._clock()
            for attempt in self._attempts.values():
                if attempt.terminal:
                    continue
                attempt.finished_monotonic = finished_monotonic
                attempt.success = False
                attempt.cancelled = True
                if attempt.returncode is None:
                    attempt.returncode = -1
            self._active_attempts = 0
            self._root_pids.clear()

            media = dict(self._media)
            media["output_bytes"] = self._sum_output_bytes(
                output_paths, pipeline_result
            )
            media["produced_duration_ms"] = (
                _bounded_int(produced_duration_ms, 0, MAX_MEDIA_DURATION_MS)
                if produced_duration_ms is not None
                else None
            )
            encoding = self._encoding_snapshot(finished_monotonic)
            resources = self._resource_snapshot()
            attempt_count = encoding["attempt_count"]
            partial = bool(
                media["duration_probe_failure_count"] > 0
                or (
                    attempt_count > 0
                    and resources["sampler_status"] != "OK"
                )
                or self._sampler_had_error
            )
            snapshot = {
                "metrics_version": METRICS_VERSION,
                "collection_status": "PARTIAL" if partial else "COMPLETE",
                "media": media,
                "encoding": encoding,
                "resources": resources,
                "failure": self._failure_snapshot(
                    status=status,
                    pipeline_result=pipeline_result,
                    cancel_reason=cancel_reason,
                    encoding=encoding,
                ),
                "stage_metrics": self._stage_metrics_snapshot(pipeline_result),
                "encoder_metrics": self._encoder_metrics_snapshot(
                    finished_monotonic
                ),
                "resource_samples": self._resource_samples_snapshot(),
            }
            if not is_valid_metrics_snapshot(snapshot):
                snapshot = unavailable_metrics_snapshot(
                    status=status, cancel_reason=cancel_reason
                )
            self._finished_snapshot = copy.deepcopy(snapshot)
            return copy.deepcopy(snapshot)


_registry_lock = threading.RLock()
_collectors: "OrderedDict[str, RunMetricsCollector]" = OrderedDict()
_attempt_owners: Dict[str, str] = {}


def _collector_for_attempt(attempt_id: Any) -> Optional[RunMetricsCollector]:
    attempt_key = str(attempt_id or "")
    with _registry_lock:
        owner = _attempt_owners.get(attempt_key, "")
        return _collectors.get(owner)


def _prune_finished_locked() -> None:
    finished = [
        run_id for run_id, collector in _collectors.items() if collector.is_finished
    ]
    excess = max(0, len(finished) - MAX_FINISHED_COLLECTORS)
    for run_id in finished[:excess]:
        _collectors.pop(run_id, None)
        for attempt_id, owner in list(_attempt_owners.items()):
            if owner == run_id:
                _attempt_owners.pop(attempt_id, None)


def prepare_run(
    client_run_id: Any,
    media_summary: Any = None,
    *,
    sample_interval_sec: float = 0.5,
    psutil_module: Any = _USE_DEFAULT_PSUTIL,
    gpu_sampler: Any = _USE_DEFAULT_GPU_SAMPLER,
    clock: Any = time.monotonic,
    cpu_limit_pct: int = 0,
) -> bool:
    """Register an inert collector before Worker acceptance.

    The sampler starts only after :func:`accept_run`. Duplicate or malformed
    run ids fail closed for metrics while leaving rendering untouched.
    """

    run_id = _normalize_run_id(client_run_id)
    if not run_id:
        return False
    module = _DEFAULT_PSUTIL if psutil_module is _USE_DEFAULT_PSUTIL else psutil_module
    resolved_gpu_sampler = (
        sample_gpu_resource
        if gpu_sampler is _USE_DEFAULT_GPU_SAMPLER
        else gpu_sampler
    )
    try:
        collector = RunMetricsCollector(
            run_id,
            media_summary,
            sample_interval_sec=sample_interval_sec,
            psutil_module=module,
            gpu_sampler=resolved_gpu_sampler,
            clock=clock,
            cpu_limit_pct=cpu_limit_pct,
        )
    except Exception:
        return False
    with _registry_lock:
        if run_id in _collectors:
            return False
        _collectors[run_id] = collector
        _prune_finished_locked()
    return True


def accept_run(client_run_id: Any, accepted_monotonic: Optional[float] = None) -> bool:
    run_id = _normalize_run_id(client_run_id)
    with _registry_lock:
        collector = _collectors.get(run_id)
    if collector is None:
        return False
    try:
        return collector.accept(accepted_monotonic)
    except Exception:
        return False


def discard_run(client_run_id: Any) -> None:
    run_id = _normalize_run_id(client_run_id)
    with _registry_lock:
        collector = _collectors.pop(run_id, None)
        for attempt_id, owner in list(_attempt_owners.items()):
            if owner == run_id:
                _attempt_owners.pop(attempt_id, None)
    if collector is not None:
        collector.stop_sampler()


def begin_ffmpeg_attempt(
    encoder: Any,
    *,
    expected_duration_sec: Any = 0.0,
    attempt_kind: Any = "primary",
    started_monotonic: Optional[float] = None,
) -> str:
    run_id = current_run_id()
    with _registry_lock:
        collector = _collectors.get(run_id)
    if collector is None:
        return ""
    try:
        attempt_id = collector.begin_attempt(
            encoder,
            expected_duration_sec=expected_duration_sec,
            attempt_kind=attempt_kind,
            started_monotonic=started_monotonic,
        )
    except Exception:
        return ""
    if attempt_id:
        with _registry_lock:
            _attempt_owners[attempt_id] = run_id
    return attempt_id


def register_ffmpeg_process(attempt_id: Any, pid: Any) -> bool:
    collector = _collector_for_attempt(attempt_id)
    if collector is None:
        return False
    try:
        return collector.register_process(str(attempt_id), pid)
    except Exception:
        return False


def unregister_ffmpeg_process(attempt_id: Any, pid: Any = None) -> None:
    collector = _collector_for_attempt(attempt_id)
    if collector is not None:
        try:
            collector.unregister_process(str(attempt_id), pid)
        except Exception:
            pass


def record_ffmpeg_progress(
    attempt_id: Any,
    *,
    out_time_us: Any = None,
    speed: Any = None,
    fps: Any = None,
    total_size: Any = None,
) -> bool:
    collector = _collector_for_attempt(attempt_id)
    if collector is None:
        return False
    try:
        return collector.progress(
            str(attempt_id),
            out_time_us=out_time_us,
            speed=speed,
            fps=fps,
            total_size=total_size,
        )
    except Exception:
        return False


def finish_ffmpeg_attempt(
    attempt_id: Any,
    *,
    success: Any,
    returncode: Any = None,
    cancelled: Any = False,
    watchdog: Any = "NONE",
    finished_monotonic: Optional[float] = None,
) -> bool:
    attempt_key = str(attempt_id or "")
    collector = _collector_for_attempt(attempt_key)
    if collector is None:
        return False
    try:
        result = collector.finish_attempt(
            attempt_key,
            success=success,
            returncode=returncode,
            cancelled=cancelled,
            watchdog=watchdog,
            finished_monotonic=finished_monotonic,
        )
    except Exception:
        result = False
    return result


def record_cpu_fallback_trigger() -> None:
    run_id = current_run_id()
    with _registry_lock:
        collector = _collectors.get(run_id)
    if collector is not None:
        try:
            collector.record_fallback_trigger()
        except Exception:
            pass


def sample_run_now(client_run_id: Any) -> bool:
    run_id = _normalize_run_id(client_run_id)
    with _registry_lock:
        collector = _collectors.get(run_id)
    if collector is None:
        return False
    try:
        return collector.sample_now()
    except Exception:
        return False


def finish_run(
    client_run_id: Any,
    *,
    status: Any,
    pipeline_result: Any = None,
    output_paths: Any = None,
    produced_duration_ms: Any = None,
    cancel_reason: Any = "NONE",
) -> Dict[str, Any]:
    run_id = _normalize_run_id(client_run_id)
    with _registry_lock:
        collector = _collectors.get(run_id)
    if collector is None:
        return unavailable_metrics_snapshot(
            status=status, cancel_reason=cancel_reason
        )
    try:
        snapshot = collector.finish(
            status=status,
            pipeline_result=pipeline_result,
            output_paths=output_paths,
            produced_duration_ms=produced_duration_ms,
            cancel_reason=cancel_reason,
        )
    except Exception:
        snapshot = unavailable_metrics_snapshot(
            status=status, cancel_reason=cancel_reason
        )
    with _registry_lock:
        for attempt_id, owner in list(_attempt_owners.items()):
            if owner == run_id:
                _attempt_owners.pop(attempt_id, None)
        _collectors.move_to_end(run_id)
        _prune_finished_locked()
    return snapshot


def stop_all(timeout: float = 0.25) -> None:
    """Boundedly stop every sampler; safe for shutdown and repeated calls."""

    with _registry_lock:
        collectors = list(_collectors.values())
        _collectors.clear()
        _attempt_owners.clear()
    deadline = time.monotonic() + max(0.0, min(2.0, float(timeout)))
    for collector in collectors:
        collector.stop_sampler(timeout=max(0.0, deadline - time.monotonic()))


def active_run_count() -> int:
    with _registry_lock:
        return sum(not collector.is_finished for collector in _collectors.values())


def unavailable_metrics_snapshot(
    *, status: Any = "FAILED", cancel_reason: Any = "NONE"
) -> Dict[str, Any]:
    terminal_status = _enum_text(status)
    reason = _enum_text(cancel_reason)
    if reason not in CANCEL_REASONS:
        reason = "NONE"
    if terminal_status == "SUCCEEDED":
        code = "NONE"
        stage = "NONE"
    elif terminal_status == "PAUSED":
        code = "USER_PAUSED"
        stage = "PIPELINE"
        reason = "USER_PAUSE"
    elif terminal_status == "CANCELLED":
        code = "APP_SHUTDOWN" if reason == "APP_SHUTDOWN" else "USER_CANCELLED"
        stage = "PIPELINE"
        if reason == "NONE":
            reason = "USER_STOP"
    elif terminal_status == "INVALID_INPUT":
        code = "INVALID_INPUT"
        stage = "PIPELINE"
    else:
        code = "UNKNOWN"
        stage = "PIPELINE"
    snapshot = {
        "metrics_version": METRICS_VERSION,
        "collection_status": "UNAVAILABLE",
        "media": sanitize_media_summary({}),
        "encoding": {
            "attempt_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "cancelled_count": 0,
            "wall_ms_sum": 0,
            "wall_span_ms": 0,
            "encoded_media_ms_sum": 0,
            "peak_parallel": 0,
            "speed_x_avg": None,
            "speed_x_p95": None,
            "encode_fps_avg": None,
            "actual_encoder": None,
            "encoder_attempts": [],
            "hardware_attempt_count": 0,
            "cpu_fallback_trigger_count": 0,
            "cpu_fallback_success_count": 0,
            "cpu_fallback_failure_count": 0,
        },
        "resources": {
            "sampler_status": "UNAVAILABLE",
            "sample_count": 0,
            "coverage_ms": 0,
            "cpu_limit_pct": 0,
            "logical_cpu_count": None,
            "ram_total_bytes": None,
            "ffmpeg_cpu_avg_pct": None,
            "ffmpeg_cpu_peak_pct": None,
            "ffmpeg_rss_avg_bytes": None,
            "ffmpeg_rss_peak_bytes": None,
        },
        "failure": {
            "code": code,
            "stage": stage,
            "ffmpeg_return_code": None,
            "watchdog": "NONE",
            "fallback_exhausted": False,
            "cancel_reason": reason,
        },
        "stage_metrics": [],
        "encoder_metrics": [],
        "resource_samples": [],
    }
    return snapshot


def _is_int_in(value: Any, low: int, high: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and low <= value <= high
    )


def _is_float_or_none(value: Any, low: float, high: float) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and low <= float(value) <= high
    )


def is_valid_metrics_snapshot(value: Any) -> bool:
    """Strictly validate the bounded, path-free metrics snapshot contract."""

    if not isinstance(value, Mapping) or set(value) != SNAPSHOT_FIELDS:
        return False
    if value.get("metrics_version") != METRICS_VERSION:
        return False
    if value.get("collection_status") not in COLLECTION_STATUSES:
        return False

    media = value.get("media")
    if not isinstance(media, Mapping) or set(media) != MEDIA_FIELDS:
        return False
    for key in (
        "input_bytes",
        "output_bytes",
    ):
        if not _is_int_in(media.get(key), 0, MAX_BYTES):
            return False
    produced_duration_ms = media.get("produced_duration_ms")
    if produced_duration_ms is not None and not _is_int_in(
        produced_duration_ms, 0, MAX_MEDIA_DURATION_MS
    ):
        return False
    for key in (
        "primary_duration_count",
        "audio_duration_count",
        "duration_probe_failure_count",
    ):
        if not _is_int_in(media.get(key), 0, MAX_COUNT):
            return False
    for key in (
        "primary_duration_ms_total",
        "audio_duration_ms_total",
    ):
        if not _is_int_in(media.get(key), 0, MAX_DURATION_MS):
            return False
    for prefix in ("primary", "audio"):
        count = media.get(f"{prefix}_duration_count")
        minimum = media.get(f"{prefix}_duration_ms_min")
        maximum = media.get(f"{prefix}_duration_ms_max")
        if count == 0:
            if minimum is not None or maximum is not None:
                return False
        else:
            if not _is_int_in(minimum, 0, MAX_DURATION_MS):
                return False
            if not _is_int_in(maximum, 0, MAX_DURATION_MS):
                return False
            if minimum > maximum:
                return False

    encoding = value.get("encoding")
    if not isinstance(encoding, Mapping) or set(encoding) != ENCODING_FIELDS:
        return False
    for key in (
        "attempt_count",
        "success_count",
        "failed_count",
        "cancelled_count",
        "peak_parallel",
        "hardware_attempt_count",
        "cpu_fallback_trigger_count",
        "cpu_fallback_success_count",
        "cpu_fallback_failure_count",
    ):
        if not _is_int_in(encoding.get(key), 0, MAX_COUNT):
            return False
    for key in ("wall_ms_sum", "wall_span_ms", "encoded_media_ms_sum"):
        if not _is_int_in(encoding.get(key), 0, MAX_DURATION_MS):
            return False
    if encoding["success_count"] + encoding["failed_count"] + encoding["cancelled_count"] != encoding["attempt_count"]:
        return False
    for key in ("speed_x_avg", "speed_x_p95", "encode_fps_avg"):
        if not _is_float_or_none(encoding.get(key), 0.0, MAX_RATE):
            return False
    actual_encoder = encoding.get("actual_encoder")
    if actual_encoder is not None and actual_encoder not in OUTPUT_ENCODERS | {"MIXED"}:
        return False
    rows = encoding.get("encoder_attempts")
    if not isinstance(rows, list) or len(rows) > MAX_ENCODER_ROWS:
        return False
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != ENCODER_ROW_FIELDS:
            return False
        if row.get("encoder") not in ENCODER_IDS:
            return False
        for key in ("attempts", "successes", "failures"):
            if not _is_int_in(row.get(key), 0, MAX_COUNT):
                return False
        if row["successes"] + row["failures"] != row["attempts"]:
            return False

    resources = value.get("resources")
    if not isinstance(resources, Mapping) or set(resources) != RESOURCE_FIELDS:
        return False
    if resources.get("sampler_status") not in SAMPLER_STATUSES:
        return False
    if not _is_int_in(resources.get("sample_count"), 0, MAX_COUNT):
        return False
    if not _is_int_in(resources.get("coverage_ms"), 0, MAX_DURATION_MS):
        return False
    if not _is_int_in(resources.get("cpu_limit_pct"), 0, 100):
        return False
    logical = resources.get("logical_cpu_count")
    if logical is not None and not _is_int_in(logical, 1, 4096):
        return False
    ram_total = resources.get("ram_total_bytes")
    if ram_total is not None and not _is_int_in(ram_total, 1, MAX_BYTES):
        return False
    for key in ("ffmpeg_cpu_avg_pct", "ffmpeg_cpu_peak_pct"):
        if not _is_float_or_none(resources.get(key), 0.0, 100.0):
            return False
    for key in ("ffmpeg_rss_avg_bytes", "ffmpeg_rss_peak_bytes"):
        item = resources.get(key)
        if item is not None and not _is_int_in(item, 0, MAX_BYTES):
            return False

    failure = value.get("failure")
    if not isinstance(failure, Mapping) or set(failure) != FAILURE_FIELDS:
        return False
    if failure.get("code") not in FAILURE_CODES:
        return False
    if failure.get("stage") not in FAILURE_STAGES:
        return False
    if failure.get("watchdog") not in WATCHDOG_TYPES:
        return False
    if failure.get("cancel_reason") not in CANCEL_REASONS:
        return False
    if not isinstance(failure.get("fallback_exhausted"), bool):
        return False
    returncode = failure.get("ffmpeg_return_code")
    if returncode is not None and not _is_int_in(
        returncode, MIN_RETURN_CODE, MAX_RETURN_CODE
    ):
        return False

    stage_metrics = value.get("stage_metrics")
    if not isinstance(stage_metrics, list) or len(stage_metrics) > MAX_STAGE_ROWS:
        return False
    stage_names: List[str] = []
    for row in stage_metrics:
        if not isinstance(row, Mapping) or set(row) != STAGE_METRIC_FIELDS:
            return False
        if row.get("stage") not in STAGE_IDS:
            return False
        if row.get("status") not in STAGE_STATUSES:
            return False
        stage_names.append(row["stage"])
        for key in (
            "expected",
            "succeeded",
            "failed",
            "skipped",
            "cancelled",
        ):
            if not _is_int_in(row.get(key), 0, MAX_COUNT):
                return False
        if (
            row["succeeded"]
            + row["failed"]
            + row["skipped"]
            + row["cancelled"]
            > row["expected"]
        ):
            return False
        duration_ms = row.get("duration_ms")
        if duration_ms is not None and not _is_int_in(
            duration_ms, 0, MAX_DURATION_MS
        ):
            return False
    if stage_names != sorted(
        set(stage_names), key=STAGE_ORDER.index
    ):
        return False

    encoder_metrics = value.get("encoder_metrics")
    if not isinstance(encoder_metrics, list) or len(encoder_metrics) > MAX_ENCODER_ROWS:
        return False
    encoder_names: List[str] = []
    for row in encoder_metrics:
        if not isinstance(row, Mapping) or set(row) != ENCODER_METRIC_FIELDS:
            return False
        encoder = row.get("encoder")
        if encoder not in OUTPUT_ENCODERS:
            return False
        encoder_names.append(encoder)
        for key in ("attempts", "successes", "failures"):
            if not _is_int_in(row.get(key), 0, MAX_COUNT):
                return False
        if row["successes"] + row["failures"] != row["attempts"]:
            return False
        for key in ("wall_ms", "encoded_media_ms"):
            if not _is_int_in(row.get(key), 0, MAX_DURATION_MS):
                return False
        if not _is_float_or_none(row.get("speed_x_avg"), 0.0, MAX_RATE):
            return False
    if encoder_names != sorted(set(encoder_names)):
        return False

    resource_samples = value.get("resource_samples")
    if not isinstance(resource_samples, list) or len(
        resource_samples
    ) > MAX_RESOURCE_SAMPLES:
        return False
    offsets: List[int] = []
    for sample in resource_samples:
        if not isinstance(sample, Mapping) or set(sample) != RESOURCE_SAMPLE_FIELDS:
            return False
        offset_ms = sample.get("offset_ms")
        if not _is_int_in(offset_ms, 0, MAX_DURATION_MS):
            return False
        offsets.append(offset_ms)
        values = (
            sample.get("cpu_percent"),
            sample.get("ram_mb"),
            sample.get("gpu_percent"),
            sample.get("gpu_memory_mb"),
        )
        if all(item is None for item in values):
            return False
        if not _is_float_or_none(values[0], 0.0, 100.0):
            return False
        if not _is_float_or_none(values[1], 0.0, 4_194_304.0):
            return False
        if not _is_float_or_none(values[2], 0.0, 100.0):
            return False
        if not _is_float_or_none(values[3], 0.0, 1_048_576.0):
            return False
    if offsets != sorted(set(offsets)):
        return False
    return True


def _reset_for_tests() -> None:
    """Clear module state. Private on purpose; production uses :func:`stop_all`."""

    stop_all(timeout=1.0)
    _CURRENT_RUN_ID.set("")


__all__ = [
    "METRICS_VERSION",
    "OUTPUT_ENCODERS",
    "RunMetricsCollector",
    "accept_run",
    "active_run_count",
    "begin_ffmpeg_attempt",
    "bind_run_metrics",
    "current_run_id",
    "discard_run",
    "finish_ffmpeg_attempt",
    "finish_run",
    "is_valid_metrics_snapshot",
    "prepare_run",
    "probe_produced_duration_ms",
    "record_cpu_fallback_trigger",
    "record_ffmpeg_progress",
    "register_ffmpeg_process",
    "sample_run_now",
    "sanitize_media_summary",
    "stop_all",
    "unavailable_metrics_snapshot",
    "unregister_ffmpeg_process",
]
