"""
V3_cursor_API Worker.

The worker owns media files and executes TC01-TC06 pipelines.  Render work is
submitted to a bounded executor so the FastAPI event loop remains responsive
for health, status, control, and capability requests while FFmpeg is running.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zipfile import ZipFile

from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# Path setup so we can import core/ when uvicorn runs from worker/.
WORKER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKER_ROOT))

from core.ffmpeg_runner import FfmpegResult
from core.gpu_detector import effective_video_encoder, ffmpeg_supports_encoder, gpu_summary
from core.green_render import GreenSettings, render_green
from core.pipelines import (
    render_tc01,
    render_tc02,
    render_tc03,
    render_tc04,
    render_tc05,
    render_tc06,
)
from core.pipelines._common import PipelineCallbacks, PipelineInputs


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKER_PORT = int(os.getenv("WORKER_PORT", "8789"))
WORKER_ID = os.getenv("WORKER_ID", f"worker-{secrets.token_hex(4)}")
WORKER_VERSION = os.getenv("V3_API_VERSION", "1.2.0")
BUILD_COMMIT = os.getenv("V3_BUILD_COMMIT", "unknown")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "")
DEFAULT_DATA_DIR = Path.home() / ".cache" / "v3-cursor-api" / "worker"
DATA_DIR = Path(os.getenv("WORKER_DATA_DIR", str(DEFAULT_DATA_DIR)))
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"
WORKER_MAX_CONCURRENT = max(1, int(os.getenv("WORKER_MAX_CONCURRENT", "2")))
WORKER_MAX_QUEUE = max(
    WORKER_MAX_CONCURRENT,
    int(os.getenv("WORKER_MAX_QUEUE", str(WORKER_MAX_CONCURRENT * 2))),
)
MAX_UPLOAD_BYTES = max(1, int(os.getenv("WORKER_MAX_UPLOAD_BYTES", str(200 * 1024 * 1024))))

DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("v3-worker")


# ---------------------------------------------------------------------------
# Job runtime
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = {
    "succeeded",
    "partial",
    "failed",
    "cancelled",
    "paused",
    "invalid_input",
}
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


@dataclass
class _JobControl:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)


RunnerFactory = Callable[[_JobControl], Dict[str, Any]]

_JOBS_LOCK = threading.RLock()
_STATE_WRITE_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_CONTROLS: Dict[str, _JobControl] = {}
_FACTORIES: Dict[str, RunnerFactory] = {}
_FUTURES: Dict[str, Future] = {}
_EXECUTOR = ThreadPoolExecutor(
    max_workers=WORKER_MAX_CONCURRENT,
    thread_name_prefix="v3-render",
)

_HEALTH_LOCK = threading.Lock()
_HEALTH_CACHE: Optional[Dict[str, Any]] = None
_HEALTH_CACHE_AT = 0.0
HEALTH_CACHE_SECONDS = max(1.0, float(os.getenv("WORKER_HEALTH_CACHE_SECONDS", "30")))


def _canonical_status(value: Any) -> str:
    raw = str(value or "unknown").strip().lower()
    return {
        "success": "succeeded",
        "succeeded": "succeeded",
        "completed": "succeeded",
        "done": "succeeded",
        "partial": "partial",
        "failed": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "paused": "paused",
        "invalid_input": "invalid_input",
        "invalid-input": "invalid_input",
        "running": "running",
        "queued": "queued",
    }.get(raw, raw)


def _validate_job_id(job_id: str) -> str:
    if not SAFE_JOB_ID.fullmatch(job_id or ""):
        raise HTTPException(status_code=400, detail="invalid job id")
    return job_id


def _safe_filename(value: str) -> str:
    name = Path(str(value)).name
    if name != str(value) or name in {"", ".", ".."} or not SAFE_FILENAME.fullmatch(name):
        raise HTTPException(status_code=400, detail="invalid filename")
    return name


def _job_dir(job_id: str) -> Path:
    """Return a validated per-job directory."""
    _validate_job_id(job_id)
    directory = JOBS_DIR / job_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _jobs_list_active_jobs() -> Dict[str, Any]:
    """Return list of jobs currently in-flight. Used by both the /v1/active_jobs
    endpoint and the gateway worker monitor endpoint.
    """
    with _JOBS_LOCK:
        inflight = []
        for jid, state in _JOBS.items():
            status = state.get("status") or "running"
            if status in ("succeeded", "failed", "cancelled", "completed"):
                continue
            inflight.append({
                "job_id": jid,
                "status": status,
                "started_at": state.get("started_at"),
                "tc": state.get("tc"),
                "log_tail": list(state.get("log") or [])[-5:],
            })
    return {
        "worker_id": WORKER_ID,
        "active_jobs": len(inflight),
        "max_concurrent": int(os.getenv("WORKER_MAX_CONCURRENT", "2")),
        "jobs": inflight,
    }


def _state_path(job_id: str) -> Path:
    return _job_dir(job_id) / ".job_state.json"


def _json_state(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_state(v) for k, v in value.items() if k not in {"future", "control"}}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_state(item) for item in value]
    return str(value)


def _persist_state(job_id: str, state: Dict[str, Any]) -> None:
    target = _state_path(job_id)
    temporary = target.with_suffix(".tmp")
    with _STATE_WRITE_LOCK:
        temporary.write_text(json.dumps(_json_state(state), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)


def _load_persisted_state(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        target = _state_path(job_id)
        if not target.is_file():
            return None
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, HTTPException):
        return None


def _recover_orphaned_jobs() -> None:
    """Mark jobs that were active before a worker restart as failed."""
    for state_path in JOBS_DIR.glob("*/.job_state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("status") not in {"queued", "running", "cancelling"}:
                continue
            state.update(
                {
                    "status": "failed",
                    "error": "worker restarted while job was active",
                    "finished_at": time.time(),
                    "current_step": "recovered_after_restart",
                }
            )
            _persist_state(state["job_id"], state)
        except (OSError, ValueError, KeyError, HTTPException) as exc:
            log.warning("could not recover %s: %s", state_path, exc)


_recover_orphaned_jobs()


def _snapshot(job_id: str) -> Optional[Dict[str, Any]]:
    with _JOBS_LOCK:
        state = _JOBS.get(job_id)
        if state is not None:
            return dict(state)
    return _load_persisted_state(job_id)


def _update_job(job_id: str, *, persist: bool = False, **changes: Any) -> Dict[str, Any]:
    with _JOBS_LOCK:
        state = _JOBS.setdefault(
            job_id,
            {
                "job_id": job_id,
                "status": "queued",
                "worker_id": WORKER_ID,
                "log": [],
                "output_files": [],
                "progress": 0.0,
            },
        )
        state.update(changes)
        snapshot = dict(state)
    if persist:
        _persist_state(job_id, snapshot)
    return snapshot


def _active_job_count() -> int:
    with _JOBS_LOCK:
        return sum(
            1
            for state in _JOBS.values()
            if state.get("status") in {"queued", "running", "cancelling"}
        )


def _queued_job_count() -> int:
    with _JOBS_LOCK:
        return sum(1 for state in _JOBS.values() if state.get("status") == "queued")


def _append_log(job_id: str, message: str) -> None:
    with _JOBS_LOCK:
        state = _JOBS.setdefault(job_id, {"job_id": job_id, "worker_id": WORKER_ID})
        logs = list(state.get("log") or [])
        logs.append(str(message))
        state["log"] = logs[-200:]
    log.info("[%s] %s", job_id, message)


def _callbacks(job_id: str, control: _JobControl) -> PipelineCallbacks:
    def log_cb(message: str) -> None:
        _append_log(job_id, message)

    def progress_cb(percent: float, message: str = "") -> None:
        try:
            value = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            value = 0.0
        _update_job(job_id, progress=value, current_step=str(message or ""))

    def file_cb(filename: str) -> None:
        _update_job(job_id, current_file=str(filename))

    def step_cb(name: str, message: str) -> None:
        _update_job(job_id, current_step=f"{name}: {message}")

    return PipelineCallbacks(
        log_fn=log_cb,
        stop_check=control.cancel_event.is_set,
        progress_fn=progress_cb,
        file_fn=file_cb,
        pause_check=control.pause_event.is_set,
        step_fn=step_cb,
    )


def _submit_job(job_id: str, tc_label: str, factory: RunnerFactory) -> Dict[str, Any]:
    _validate_job_id(job_id)
    with _JOBS_LOCK:
        existing = _JOBS.get(job_id) or _load_persisted_state(job_id)
        existing_status = _canonical_status(existing.get("status")) if existing and existing.get("status") else None
        if existing_status and existing_status not in {"paused", "failed", "cancelled", "invalid_input"}:
            raise HTTPException(status_code=409, detail=f"job {job_id} is already active")
        active = sum(
            1
            for state in _JOBS.values()
            if state.get("status") in {"queued", "running", "cancelling"}
        )
        if active >= WORKER_MAX_CONCURRENT + WORKER_MAX_QUEUE:
            raise HTTPException(status_code=429, detail="worker queue is full")
        control = _JobControl()
        state = {
            "job_id": job_id,
            "tc": tc_label,
            "status": "queued",
            "worker_id": WORKER_ID,
            "progress": 0.0,
            "current_step": "queued",
            "log": [],
            "output_files": [],
            "created_at": time.time(),
            "cancel_requested": False,
            "pause_requested": False,
        }
        _JOBS[job_id] = state
        _CONTROLS[job_id] = control
        _FACTORIES[job_id] = factory
        snapshot = dict(state)
        # Persist the queued snapshot before submitting.  A fast executor
        # task must never be able to write terminal state before this initial
        # snapshot and then have it overwritten by stale queued data.
        _persist_state(job_id, snapshot)
        future = _EXECUTOR.submit(_execute_job, job_id, tc_label, control, factory)
        _FUTURES[job_id] = future
    return snapshot


def _execute_job(job_id: str, tc_label: str, control: _JobControl, factory: RunnerFactory) -> None:
    started = time.time()
    _update_job(
        job_id,
        persist=True,
        status="running",
        started_at=started,
        current_step="starting",
    )
    try:
        result = factory(control)
        if control.cancel_event.is_set():
            result = {
                **result,
                "status": "cancelled",
                "output_file": None,
                "output_files": [],
                "output_size": 0,
                "error": "cancelled by request",
            }
        elif control.pause_event.is_set() and _canonical_status(result.get("status")) != "succeeded":
            result = {**result, "status": "paused"}
        result["status"] = _canonical_status(result.get("status"))
        result.setdefault("worker_id", WORKER_ID)
        result.setdefault("finished_at", time.time())
        changes = dict(result)
        changes.pop("job_id", None)
        _update_job(job_id, persist=True, **changes)
    except Exception as exc:
        log.exception("job=%s %s crashed", job_id, tc_label)
        _update_job(
            job_id,
            persist=True,
            status="failed",
            error=str(exc),
            finished_at=time.time(),
            current_step="failed",
        )
    finally:
        with _JOBS_LOCK:
            _FUTURES.pop(job_id, None)
            terminal = _JOBS.get(job_id, {}).get("status") in TERMINAL_STATUSES
            if terminal:
                _CONTROLS.pop(job_id, None)
                if _JOBS.get(job_id, {}).get("status") != "paused":
                    _FACTORIES.pop(job_id, None)
                _JOBS.pop(job_id, None)


def _control_job(job_id: str, action: str) -> Dict[str, Any]:
    _validate_job_id(job_id)
    with _JOBS_LOCK:
        state = _JOBS.get(job_id)
        control = _CONTROLS.get(job_id)
        future = _FUTURES.get(job_id)
        factory = _FACTORIES.get(job_id)
    if state is None:
        state = _load_persisted_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    current = _canonical_status(state.get("status"))
    if action == "cancel":
        if current in TERMINAL_STATUSES:
            return state
        if control:
            control.cancel_event.set()
        if future and future.cancel():
            return _update_job(
                job_id,
                persist=True,
                status="cancelled",
                cancel_requested=True,
                finished_at=time.time(),
                current_step="cancelled",
            )
        return _update_job(job_id, cancel_requested=True, status="cancelling", current_step="cancelling")

    if action == "pause":
        if current in TERMINAL_STATUSES and current != "paused":
            raise HTTPException(status_code=409, detail=f"job is already {current}")
        if control:
            control.pause_event.set()
        return _update_job(job_id, pause_requested=True, current_step="pausing")

    if action == "resume":
        if current != "paused":
            raise HTTPException(status_code=409, detail="only paused jobs can resume")
        if factory is None:
            raise HTTPException(status_code=409, detail="job cannot resume after worker restart")
        return _submit_job(job_id, str(state.get("tc") or "unknown"), factory)

    raise HTTPException(status_code=400, detail="invalid job control action")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RenderRequest(BaseModel):
    product_id: str
    background_id: str
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


class TCRenderRequest(BaseModel):
    product_id: Optional[str] = None
    background_id: Optional[str] = None
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    product_ids: List[str] = Field(default_factory=list)
    background_ids: List[str] = Field(default_factory=list)
    cover_ids: List[str] = Field(default_factory=list)
    audio_ids: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)
    product_root_ids: List[str] = Field(default_factory=list)
    product_roots: List[str] = Field(default_factory=list)
    mode: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    values: Optional[Dict[str, Any]] = None
    extra: Optional[Dict[str, Any]] = None
    run_seed: Optional[int] = None


class StatusResponse(BaseModel):
    job_id: str
    status: str
    worker_id: str
    tc: Optional[str] = None
    progress: float = 0.0
    current_step: Optional[str] = None
    current_file: Optional[str] = None
    encoder: Optional[str] = None
    output_file: Optional[str] = None
    output_files: List[str] = Field(default_factory=list)
    output_size: Optional[int] = None
    duration_sec: Optional[float] = None
    expected: Optional[int] = None
    succeeded: Optional[int] = None
    failed: Optional[int] = None
    cancelled: Optional[int] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    pause_requested: bool = False
    log: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# App and auth
# ---------------------------------------------------------------------------


app = FastAPI(title="V3_cursor_API Worker", version=WORKER_VERSION)


def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)) -> bool:
    if not INTERNAL_TOKEN or not x_cutdee_internal or x_cutdee_internal != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Cutdee-Internal header")
    return True


@app.get("/v1/active_jobs")
async def list_active_jobs_endpoint(_: bool = Depends(_verify_internal)):
    """List jobs currently in-flight on this worker (FIX 2026-08-18).

    Used by the gateway worker monitor endpoint to surface what each worker
    is doing right now. Returns: worker_id, active_jobs count, max_concurrent,
    jobs[{job_id, status, started_at, tc, log_tail}].
    """
    return _jobs_list_active_jobs()


# ---------------------------------------------------------------------------
# Health and capabilities
# ---------------------------------------------------------------------------


def _health_snapshot_sync() -> Dict[str, Any]:
    global _HEALTH_CACHE, _HEALTH_CACHE_AT
    now = time.monotonic()
    with _HEALTH_LOCK:
        if _HEALTH_CACHE is not None and now - _HEALTH_CACHE_AT < HEALTH_CACHE_SECONDS:
            return dict(_HEALTH_CACHE)
    gpu = gpu_summary()
    encoder = effective_video_encoder()
    snapshot = {
        "gpu": gpu,
        "encoder": encoder,
        "supports_chromakey_cuda": ffmpeg_supports_encoder("h264_nvenc"),
        "data_dir": str(DATA_DIR),
    }
    with _HEALTH_LOCK:
        _HEALTH_CACHE = snapshot
        _HEALTH_CACHE_AT = time.monotonic()
    return dict(snapshot)


@app.get("/health", response_class=JSONResponse)
async def health() -> Dict[str, Any]:
    snapshot = await asyncio.to_thread(_health_snapshot_sync)
    return {
        "ok": True,
        "worker_id": WORKER_ID,
        "version": WORKER_VERSION,
        "commit": BUILD_COMMIT,
        "active_jobs": _active_job_count(),
        "queued_jobs": _queued_job_count(),
        "max_concurrent": WORKER_MAX_CONCURRENT,
        "max_queue": WORKER_MAX_QUEUE,
        "system": {
            "disk_free_gb": round(shutil.disk_usage(DATA_DIR).free / (1024 ** 3), 2),
        },
        **snapshot,
    }


@app.get("/v1/capabilities", response_class=JSONResponse)
async def capabilities(_: bool = Depends(_verify_internal)) -> Dict[str, Any]:
    snapshot = await asyncio.to_thread(_health_snapshot_sync)
    return {
        "worker_id": WORKER_ID,
        "version": WORKER_VERSION,
        "commit": BUILD_COMMIT,
        "modes": ["tc01", "tc02", "tc03", "tc04", "tc05", "tc06"],
        "encoders": {
            "preferred": snapshot.get("encoder"),
            "nvenc": ffmpeg_supports_encoder("h264_nvenc"),
            "qsv": ffmpeg_supports_encoder("h264_qsv"),
            "libx264": ffmpeg_supports_encoder("libx264"),
        },
        "gpu": snapshot.get("gpu"),
        "max_concurrent": WORKER_MAX_CONCURRENT,
        "max_queue": WORKER_MAX_QUEUE,
        "data_dir": str(DATA_DIR),
    }


# ---------------------------------------------------------------------------
# Input and output helpers
# ---------------------------------------------------------------------------


def _file_for_id(job_id: str, file_id: str) -> Path:
    jd = _job_dir(job_id)
    candidate = str(file_id)
    for path in jd.iterdir():
        if path.is_file() and (path.stem == candidate or path.name == candidate):
            return path
    raise HTTPException(status_code=404, detail=f"file {file_id} not found in job {job_id}")


def _resolve_settings(settings_dict: Optional[Dict[str, Any]]) -> GreenSettings:
    settings = GreenSettings()
    for key, value in (settings_dict or {}).items():
        if hasattr(settings, key):
            setattr(settings, key, value)
    return settings


def _resolve_ids(job_id: str, ids: List[str], singular: Optional[str], role: str) -> List[str]:
    selected = list(ids) if ids else ([singular] if singular else [])
    return [str(_file_for_id(job_id, file_id)) for file_id in selected]


def _safe_extract_root(job_id: str, archive: Path) -> Path:
    destination = _job_dir(job_id) / "product_roots" / archive.stem
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    with ZipFile(archive) as source:
        for member in source.infolist():
            target = (destination / member.filename).resolve()
            if destination_resolved not in target.parents and target != destination_resolved:
                raise HTTPException(status_code=400, detail="product root archive contains unsafe path")
        source.extractall(destination)
    return destination


def _resolve_product_root(job_id: str, value: str) -> str:
    try:
        path = _file_for_id(job_id, value)
    except HTTPException:
        raw = Path(str(value))
        path = raw if raw.is_absolute() else _job_dir(job_id) / raw
        resolved = path.resolve()
        job_root = _job_dir(job_id).resolve()
        if job_root not in resolved.parents and resolved != job_root:
            raise HTTPException(status_code=400, detail="product root must be inside the job directory")
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"product root {value} not found")
    if path.is_file() and path.suffix.lower() == ".zip":
        return str(_safe_extract_root(job_id, path))
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"product root {value} is not a directory")
    return str(path)


def _build_tc_inputs(job_id: str, req: TCRenderRequest) -> PipelineInputs:
    extra = req.extra or {}
    root_values = [*req.product_root_ids, *req.product_roots]
    extra_roots = extra.get("product_roots", extra.get("roots", extra.get("root", [])))
    if isinstance(extra_roots, str):
        extra_roots = [extra_roots]
    root_values.extend(str(value) for value in (extra_roots or []))
    values = {**(req.settings or {}), **(req.values or {})}
    if req.run_seed is not None:
        values["run_seed"] = req.run_seed
    return PipelineInputs(
        output_dir=str(_job_dir(job_id)),
        values=values,
        products=_resolve_ids(job_id, req.product_ids, req.product_id, "product"),
        backgrounds=_resolve_ids(job_id, req.background_ids, req.background_id, "background"),
        audios=_resolve_ids(job_id, req.audio_ids, req.audio_id, "audio"),
        covers=_resolve_ids(job_id, req.cover_ids, req.cover_id, "cover"),
        sources=_resolve_ids(job_id, req.source_ids, None, "source"),
        product_roots=[_resolve_product_root(job_id, value) for value in root_values],
        run_stamp=str(extra.get("run_stamp") or ""),
    )


def _copy_output_into_job(job_id: str, source: Path, index: int) -> Optional[str]:
    if not source.is_file() or source.stat().st_size <= 0:
        return None
    jd = _job_dir(job_id)
    name = _safe_filename(source.name)
    destination = jd / name
    if source.resolve() != destination.resolve() and destination.exists():
        destination = jd / f"{index:03d}_{name}"
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    return destination.name


def _collect_outputs(job_id: str, result: Any, input_paths: set[str]) -> List[str]:
    jd = _job_dir(job_id)
    raw_outputs = list(getattr(result, "outputs", []) or [])
    names: List[str] = []
    for index, raw in enumerate(raw_outputs, start=1):
        path = Path(str(raw))
        name = _copy_output_into_job(job_id, path, index)
        if name and name not in names:
            names.append(name)

    # Fail-safe fallback for older pipeline results that did not populate
    # PipelineResult.outputs. Uploaded files are tracked and excluded by path,
    # not by filename prefix, so TC01 product_* outputs remain discoverable.
    if not names:
        raw_status = getattr(result, "status", None)
        raw_status = getattr(raw_status, "value", raw_status)
        if raw_status is not None and _canonical_status(raw_status) not in {"succeeded", "partial"}:
            return []
        for path in sorted(jd.rglob("*.mp4")):
            if path.name.startswith(".") or ".partial." in path.name:
                continue
            if str(path.resolve()) in input_paths:
                continue
            name = _copy_output_into_job(job_id, path, len(names) + 1)
            if name and name not in names:
                names.append(name)
    return sorted(names)


def _sanitize_result_paths(value: Any) -> Any:
    """Keep pipeline diagnostics useful without exposing worker filesystem paths."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if key in {"outputs", "invalid_outputs"} and isinstance(item, list):
                sanitized[key] = [Path(str(path)).name for path in item]
            else:
                sanitized[key] = _sanitize_result_paths(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_result_paths(item) for item in value]
    return value


def _result_payload(job_id: str, tc_label: str, result: Any, output_files: List[str], elapsed: float) -> Dict[str, Any]:
    result_dict = _sanitize_result_paths(result.to_dict() if hasattr(result, "to_dict") else {})
    result_dict["outputs"] = list(output_files)
    raw_status = getattr(result, "status", result_dict.get("status"))
    raw_status = getattr(raw_status, "value", raw_status)
    status = _canonical_status(raw_status)
    output_file = output_files[0] if output_files else None
    output_size = sum((_job_dir(job_id) / name).stat().st_size for name in output_files)
    return {
        "job_id": job_id,
        "tc": tc_label,
        "status": status,
        "expected": getattr(result, "expected", result_dict.get("expected")),
        "succeeded": getattr(result, "succeeded", result_dict.get("succeeded")),
        "failed": getattr(result, "failed", result_dict.get("failed")),
        "cancelled": getattr(result, "cancelled", result_dict.get("cancelled")),
        "output_file": output_file,
        "output_files": output_files,
        "output_size": output_size,
        "duration_sec": elapsed,
        "encoder": result_dict.get("metadata", {}).get("encoder") if isinstance(result_dict.get("metadata"), dict) else None,
        "progress": 100.0 if status == "succeeded" else 0.0,
        "result": result_dict,
        "error": "; ".join(result_dict.get("all_errors", result_dict.get("errors", [])) or []),
    }


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------


@app.post("/v1/jobs/{job_id}/upload/{role}")
async def upload_file(
    job_id: str,
    role: str,
    request: Request,
    _: bool = Depends(_verify_internal),
) -> Dict[str, Any]:
    _validate_job_id(job_id)
    if role not in ("product", "background", "cover", "audio", "source", "product_root"):
        raise HTTPException(status_code=400, detail=f"invalid role: {role}")
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")

    filename = f"{role}_{int(time.time())}_{secrets.token_hex(4)}.mp4"
    content_disposition = request.headers.get("Content-Disposition", "")
    if "filename=" in content_disposition:
        candidate = content_disposition.split("filename=", 1)[1].strip().strip('"')
        if candidate:
            filename = _safe_filename(candidate)
    if "." not in filename:
        suffix = ".png" if role == "cover" else ".zip" if role == "product_root" else ".mp4"
        filename = f"{filename}{suffix}"

    target = _job_dir(job_id) / filename
    target.write_bytes(body)
    _append_log(job_id, f"uploaded {role} -> {target.name} ({len(body)} bytes)")
    return {
        "job_id": job_id,
        "role": role,
        "file_id": target.stem,
        "filename": target.name,
        "size": len(body),
    }


# ---------------------------------------------------------------------------
# Render factories
# ---------------------------------------------------------------------------


def _legacy_factory(job_id: str, req: RenderRequest) -> RunnerFactory:
    def run(control: _JobControl) -> Dict[str, Any]:
        started = time.time()
        product = _file_for_id(job_id, req.product_id)
        background = _file_for_id(job_id, req.background_id)
        cover = _file_for_id(job_id, req.cover_id) if req.cover_id else None
        audio = _file_for_id(job_id, req.audio_id) if req.audio_id else None
        settings = _resolve_settings(req.settings)
        output = _job_dir(job_id) / f"output_{int(started)}.mp4"
        lines: List[str] = []

        def on_log(message: str) -> None:
            lines.append(str(message))
            _append_log(job_id, str(message))

        result: FfmpegResult = render_green(
            cover=str(cover) if cover else None,
            product=str(product),
            background=str(background),
            audio=str(audio) if audio else None,
            out_path=str(output),
            settings=settings,
            on_log=on_log,
            stop_check=lambda: control.cancel_event.is_set() or control.pause_event.is_set(),
        )
        if control.cancel_event.is_set():
            return {"job_id": job_id, "status": "cancelled", "output_files": [], "log": lines}
        if control.pause_event.is_set():
            return {"job_id": job_id, "status": "paused", "output_files": [], "log": lines}
        if not result.success:
            return {
                "job_id": job_id,
                "status": "failed",
                "output_files": [],
                "error": result.error or "render failed",
                "duration_sec": time.time() - started,
                "log": lines,
            }
        name = _copy_output_into_job(job_id, Path(result.output_path or output), 1) or output.name
        size = (_job_dir(job_id) / name).stat().st_size if (_job_dir(job_id) / name).exists() else 0
        return {
            "job_id": job_id,
            "status": "succeeded",
            "output_file": name,
            "output_files": [name],
            "output_size": size,
            "duration_sec": time.time() - started,
            "encoder": getattr(settings, "encoder_alias", None),
            "progress": 100.0,
            "log": lines[-200:],
        }

    return run


def _pipeline_factory(tc_label: str, render_fn: Callable[..., Any], job_id: str, req: TCRenderRequest) -> RunnerFactory:
    def run(control: _JobControl) -> Dict[str, Any]:
        started = time.time()
        inputs = _build_tc_inputs(job_id, req)
        input_paths = {
            str(Path(path).resolve())
            for path in [*inputs.products, *inputs.backgrounds, *inputs.audios, *inputs.covers, *inputs.sources]
            if Path(path).exists()
        }
        for root in inputs.product_roots:
            root_path = Path(root)
            if root_path.is_dir():
                input_paths.update(str(path.resolve()) for path in root_path.rglob("*") if path.is_file())
        callbacks = _callbacks(job_id, control)
        _update_job(job_id, tc=tc_label, current_step="running")
        result = render_fn(inputs, callbacks)
        if control.cancel_event.is_set() or control.pause_event.is_set():
            return {
                "job_id": job_id,
                "tc": tc_label,
                "status": "cancelled" if control.cancel_event.is_set() else "paused",
                "output_files": [],
                "result": result.to_dict() if hasattr(result, "to_dict") else {},
                "log": _snapshot(job_id).get("log", []) if _snapshot(job_id) else [],
            }
        output_files = _collect_outputs(job_id, result, input_paths)
        payload = _result_payload(job_id, tc_label, result, output_files, time.time() - started)
        payload["log"] = _snapshot(job_id).get("log", []) if _snapshot(job_id) else []
        return payload

    return run


# ---------------------------------------------------------------------------
# Render routes: enqueue and return immediately
# ---------------------------------------------------------------------------


@app.post("/v1/jobs/{job_id}/render", status_code=202)
async def render_job(job_id: str, req: RenderRequest, _: bool = Depends(_verify_internal)) -> Dict[str, Any]:
    _file_for_id(job_id, req.product_id)
    _file_for_id(job_id, req.background_id)
    state = _submit_job(job_id, "tc01", _legacy_factory(job_id, req))
    return {"job_id": job_id, "status": state["status"], "worker_id": WORKER_ID, "queued": True}


PIPELINES: Dict[str, Callable[..., Any]] = {
    "tc01": render_tc01,
    "tc02": render_tc02,
    "tc03": render_tc03,
    "tc04": render_tc04,
    "tc05": render_tc05,
    "tc06": render_tc06,
}


def _make_pipeline_endpoint(tc_label: str):
    async def endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)) -> Dict[str, Any]:
        render_fn = PIPELINES[tc_label]
        # Resolve inputs before enqueue so invalid IDs are reported as 404 and
        # never consume a queue slot.
        _build_tc_inputs(job_id, req)
        state = _submit_job(job_id, tc_label, _pipeline_factory(tc_label, render_fn, job_id, req))
        return {"job_id": job_id, "tc": tc_label, "status": state["status"], "worker_id": WORKER_ID, "queued": True}

    endpoint.__name__ = f"render_{tc_label}_endpoint"
    return endpoint


for _tc in PIPELINES:
    app.post(f"/v1/{_tc}/render/{{job_id}}", status_code=202)(_make_pipeline_endpoint(_tc))


# ---------------------------------------------------------------------------
# Status, control, and output
# ---------------------------------------------------------------------------


@app.get("/v1/jobs/{job_id}/status", response_model=StatusResponse)
async def get_status(job_id: str, _: bool = Depends(_verify_internal)) -> StatusResponse:
    state = _snapshot(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    state["status"] = _canonical_status(state.get("status"))
    state.setdefault("worker_id", WORKER_ID)
    state.setdefault("log", [])
    state.setdefault("output_files", [])
    state.pop("job_id", None)
    return StatusResponse(job_id=job_id, **state)


@app.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: bool = Depends(_verify_internal)) -> Dict[str, Any]:
    state = _control_job(job_id, "cancel")
    return {"job_id": job_id, "status": _canonical_status(state.get("status")), "cancel_requested": True}


@app.post("/v1/jobs/{job_id}/pause")
async def pause_job(job_id: str, _: bool = Depends(_verify_internal)) -> Dict[str, Any]:
    state = _control_job(job_id, "pause")
    return {"job_id": job_id, "status": _canonical_status(state.get("status")), "pause_requested": True}


@app.post("/v1/jobs/{job_id}/resume")
async def resume_job(job_id: str, _: bool = Depends(_verify_internal)) -> Dict[str, Any]:
    state = _control_job(job_id, "resume")
    return {"job_id": job_id, "status": _canonical_status(state.get("status")), "queued": True}


@app.get("/v1/jobs/{job_id}/output")
async def get_output(job_id: str, filename: str, _: bool = Depends(_verify_internal)) -> FileResponse:
    filename = _safe_filename(filename)
    state = _snapshot(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="job not found")
    output_files = list(state.get("output_files") or [])
    if filename not in output_files:
        raise HTTPException(status_code=404, detail="output not found")
    target = _job_dir(job_id) / filename
    if not target.is_file() or target.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="output not found")
    media_type = "video/mp4" if target.suffix.lower() == ".mp4" else "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=filename)


@app.post("/v1/admin/cleanup")
async def cleanup(days: int = 7, _: bool = Depends(_verify_internal)) -> Dict[str, Any]:
    days = max(0, int(days))
    cutoff = time.time() - days * 86400
    removed = 0
    freed_bytes = 0
    for directory in JOBS_DIR.iterdir():
        if not directory.is_dir():
            continue
        with _JOBS_LOCK:
            active = directory.name in _JOBS and _JOBS[directory.name].get("status") not in TERMINAL_STATUSES
        if active:
            continue
        try:
            if directory.stat().st_mtime < cutoff:
                for path in directory.rglob("*"):
                    if path.is_file():
                        freed_bytes += path.stat().st_size
                shutil.rmtree(directory)
                removed += 1
        except OSError as exc:
            log.warning("cleanup %s failed: %s", directory, exc)
    return {"removed_jobs": removed, "freed_mb": round(freed_bytes / 1024 / 1024, 2)}


if __name__ == "__main__":
    import uvicorn

    log.info("starting V3_cursor_API worker on 0.0.0.0:%s, id=%s", WORKER_PORT, WORKER_ID)
    log.info("data_dir=%s, version=%s, commit=%s", DATA_DIR, WORKER_VERSION, BUILD_COMMIT)
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")
