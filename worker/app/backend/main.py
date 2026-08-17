"""
V3_cursor_API Worker — receives render jobs from gateway, runs ffmpeg green screen.

Architecture:
  Gateway (1 host) — receives uploads, queues jobs, dispatches to workers
  Worker (N hosts) — this service, runs ffmpeg, returns output

Routes:
  GET  /health         — public health (no auth, gateway polls)
  GET  /v1/capabilities — what this worker can do (encoder, GPU, etc.)
  POST /v1/jobs/{job_id}/files     — receive uploaded files
  POST /v1/jobs/{job_id}/render    — run green screen render (internal token only)
  GET  /v1/jobs/{job_id}/status    — get current status
  GET  /v1/jobs/{job_id}/output    — download rendered file
"""
from __future__ import annotations

import os
import sys
import time
import json
import logging
import shutil
import threading
import hashlib
import secrets
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Header, Depends
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

# Path setup so we can import core/
WORKER_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKER_ROOT))

from core.green_render import GreenSettings, render_green
from core.gpu_detector import (
    effective_video_encoder,
    ffmpeg_supports_encoder,
    gpu_summary,
)
from core.ffmpeg_runner import FfmpegResult
from core.media_probe import has_video_stream, has_audio_stream

# V3 pipelines (TC01-TC06) — all available locally
try:
    from core.pipelines import _common
    from core.pipelines import tc01_chroma
    from core.pipelines import tc02_reframe
    from core.pipelines import tc03_batch
    from core.pipelines import tc04_rebatch
    from core.pipelines import tc05_reframe_only
    from core.pipelines import tc06_video_loop
    PIPELINES_AVAILABLE = True
except Exception as _e:
    log.warning(f"core.pipelines not importable: {_e} — TC01-TC06 disabled")
    PIPELINES_AVAILABLE = False
    _common = None
    tc01_chroma = tc02_reframe = tc03_batch = None
    tc04_rebatch = tc05_reframe_only = tc06_video_loop = None

PIPELINES = {
    "tc01": (lambda i, c: tc01_chroma.render(i, c)) if PIPELINES_AVAILABLE else None,
    "tc02": (lambda i, c: tc02_reframe.render(i, c)) if PIPELINES_AVAILABLE else None,
    "tc03": (lambda i, c: tc03_batch.render(i, c)) if PIPELINES_AVAILABLE else None,
    "tc04": (lambda i, c: tc04_rebatch.render(i, c)) if PIPELINES_AVAILABLE else None,
    "tc05": (lambda i, c: tc05_reframe_only.render(i, c)) if PIPELINES_AVAILABLE else None,
    "tc06": (lambda i, c: tc06_video_loop.render(i, c)) if PIPELINES_AVAILABLE else None,
}

# === Configuration ===
WORKER_PORT = int(os.getenv("WORKER_PORT", "7701"))
WORKER_ID = os.getenv("WORKER_ID", f"worker-{secrets.token_hex(4)}")
INTERNAL_TOKEN = os.getenv("CUTDEE_INTERNAL_TOKEN", "dev-internal-token-change-me")
DATA_DIR = Path(os.getenv("WORKER_DATA_DIR", "/var/lib/v3-cursor-api/worker"))
JOBS_DIR = DATA_DIR / "jobs"
LOGS_DIR = DATA_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("v3-worker")

# === Job state (in-memory; gateway tracks real state) ===
_JOBS_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}  # job_id -> {status, log, started_at, ...}


# ============================================================
# Pydantic models
# ============================================================

class RenderRequest(BaseModel):
    product_id: Optional[str] = None  # upload id (legacy: single)
    background_id: Optional[str] = None
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    mode: str = "tc01"  # tc01..tc06 — which pipeline to use
    # V3 WebApp format: list of file paths per role
    product_ids: List[str] = []
    background_ids: List[str] = []
    cover_ids: List[str] = []
    audio_ids: List[str] = []
    source_ids: List[str] = []  # TC05 reframe-only
    settings: Optional[Dict[str, Any]] = None  # GreenSettings overrides (TC01)
    values: Optional[Dict[str, Any]] = None  # V3 pipeline values (TC01-06)


class StatusResponse(BaseModel):
    job_id: str
    status: str  # queued | running | succeeded | failed
    worker_id: str
    encoder: Optional[str] = None
    output_file: Optional[str] = None
    output_size: Optional[int] = None
    duration_sec: Optional[float] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    log: List[str] = []


# ============================================================
# App
# ============================================================

app = FastAPI(title="V3_cursor_API Worker", version="1.0.0")


def _verify_internal(x_cutdee_internal: Optional[str] = Header(None)):
    if x_cutdee_internal != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Cutdee-Internal header")
    return True


def _job_dir(job_id: str) -> Path:
    """Per-job directory for files + output."""
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_settings(settings_dict: Optional[Dict[str, Any]]) -> GreenSettings:
    """Build GreenSettings from request dict, falling back to defaults."""
    if not settings_dict:
        return GreenSettings()
    s = GreenSettings()
    for k, v in settings_dict.items():
        if hasattr(s, k):
            setattr(s, k, v)
    return s


def _file_for_id(job_id: str, file_id: str) -> Path:
    """Look up uploaded file by id within a job dir."""
    jd = _job_dir(job_id)
    for f in jd.iterdir():
        if f.stem == file_id or f.name.startswith(f"{file_id}."):
            return f
    raise HTTPException(status_code=404, detail=f"file {file_id} not found in job {job_id}")



# ============================================================
# System stats helpers (CPU / RAM / Disk / GPU)
# ============================================================
_WORKER_START_TIME = time.time()

def _safe_run(cmd, timeout=2):
    """Run a shell command, return stdout or None on error."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None

def _cpu_stats():
    """Return dict with cpu info. Cross-platform (Linux/Mac)."""
    out = {"percent": None, "count": None, "load_avg": None}
    try:
        import psutil  # type: ignore
        # Prime the cpu_percent so first call returns a real value
        psutil.cpu_percent(interval=None)
        out["percent"] = psutil.cpu_percent(interval=0.1)
        out["count"] = psutil.cpu_count(logical=True) or 0
        la = psutil.getloadavg() if hasattr(psutil, "getloadavg") else None
        out["load_avg"] = list(la) if la else None
    except ImportError:
        # Fallback: top
        text = _safe_run(["sh", "-c", "top -bn1 | grep '^%Cpu' | head -1"])
        if text:
            try:
                idle = float(text.split("id,")[0].split()[-1])
                out["percent"] = round(100.0 - idle, 1)
            except Exception: pass
        out["count"] = os.cpu_count()
        la_text = _safe_run(["sh", "-c", "cat /proc/loadavg 2>/dev/null || sysctl -n vm.loadavg"])
        if la_text:
            try:
                out["load_avg"] = [float(x) for x in la_text.split()[:3]]
            except Exception: pass
    return out

def _ram_stats():
    """Return dict with RAM info in MB."""
    out = {"total_mb": None, "used_mb": None, "percent": None}
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        out["total_mb"] = round(vm.total / 1024 / 1024)
        out["used_mb"] = round(vm.used / 1024 / 1024)
        out["percent"] = round(vm.percent, 1)
    except ImportError:
        # Linux: /proc/meminfo
        text = _safe_run(["sh", "-c", "cat /proc/meminfo 2>/dev/null"])
        if text:
            try:
                d = {}
                for line in text.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        d[k.strip()] = v.strip()
                total_kb = int(d.get("MemTotal", "0").split()[0])
                avail_kb = int(d.get("MemAvailable", d.get("MemFree", "0")).split()[0])
                used_kb = total_kb - avail_kb
                out["total_mb"] = round(total_kb / 1024)
                out["used_mb"] = round(used_kb / 1024)
                out["percent"] = round(100.0 * used_kb / max(total_kb, 1), 1)
            except Exception: pass
        # Mac fallback
        if out["total_mb"] is None:
            text = _safe_run(["sh", "-c", "sysctl -n hw.memsize"])
            if text:
                try:
                    out["total_mb"] = int(int(text) / 1024 / 1024)
                except Exception: pass
            vm_text = _safe_run(["sh", "-c", "vm_stat | awk '/free/ {f=$3} /inactive/ {i=$3} END {print (f+i)*4096/1024/1024}'"])
            if vm_text and out["total_mb"]:
                try:
                    free_mb = float(vm_text)
                    out["used_mb"] = round(out["total_mb"] - free_mb)
                    out["percent"] = round(100.0 * out["used_mb"] / out["total_mb"], 1)
                except Exception: pass
    return out

def _disk_stats(path):
    """Return disk usage for given path in GB."""
    out = {"total_gb": None, "used_gb": None, "free_gb": None, "percent": None}
    try:
        import psutil  # type: ignore
        u = psutil.disk_usage(path)
        out["total_gb"] = round(u.total / 1024 / 1024 / 1024, 1)
        out["used_gb"] = round(u.used / 1024 / 1024 / 1024, 1)
        out["free_gb"] = round(u.free / 1024 / 1024 / 1024, 1)
        out["percent"] = round(u.percent, 1)
    except ImportError:
        text = _safe_run(["sh", "-c", f"df -BG {path} 2>/dev/null | tail -1"])
        if text:
            try:
                parts = text.split()
                # /dev/xxx  100G  20G  80G  20%  /path
                total = float(parts[1].rstrip("G"))
                used = float(parts[2].rstrip("G"))
                free = float(parts[3].rstrip("G"))
                pct = float(parts[4].rstrip("%"))
                out["total_gb"], out["used_gb"], out["free_gb"], out["percent"] = total, used, free, pct
            except Exception: pass
    return out

def _gpu_nvidia_stats():
    """Try nvidia-smi (NVIDIA GPUs)."""
    text = _safe_run(["sh", "-c", "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null"])
    if not text: return None
    gpus = []
    for line in text.splitlines():
        if not line.strip(): continue
        try:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 6: continue
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "util_pct": float(parts[2]) if parts[2] else None,
                "vram_used_mb": int(parts[3]) if parts[3] else None,
                "vram_total_mb": int(parts[4]) if parts[4] else None,
                "temp_c": int(parts[5]) if parts[5] else None,
            })
        except Exception: pass
    return gpus if gpus else None

def _gpu_apple_stats():
    """Apple Silicon (M1/M2/M3/M4) - try system_profiler + mlx info."""
    text = _safe_run(["sh", "-c", "system_profiler SPDisplaysDataType 2>/dev/null | grep -E 'Chipset Model|VRAM|VRAM (Total)'"])
    # Mac unified memory is shared with CPU; we approximate via total RAM and "GPU utilization" via mlx
    out = _safe_run(["sh", "-c", "ioreg -l | grep -i 'gpu.*utilization' 2>/dev/null"])
    return {
        "platform": "apple_silicon",
        "chip": _safe_run(["sh", "-c", "sysctl -n machdep.cpu.brand_string"]),
        "unified_memory_mb": None,  # set by caller
        "gpu_util_pct": None,  # not easily accessible
    }

def _system_stats():
    """Bundle all system stats. Best-effort, all fields optional."""
    ram = _ram_stats()
    disk = _disk_stats(str(DATA_DIR))
    cpu = _cpu_stats()
    nvidia = _gpu_nvidia_stats()
    is_apple = sys.platform == "darwin"
    gpu_block = None
    if nvidia:
        # Aggregate: sum across all GPUs
        vram_used = sum((g.get("vram_used_mb") or 0) for g in nvidia)
        vram_total = sum((g.get("vram_total_mb") or 0) for g in nvidia)
        util = max((g.get("util_pct") or 0) for g in nvidia)
        temp = max((g.get("temp_c") or 0) for g in nvidia)
        gpu_block = {
            "platform": "nvidia",
            "count": len(nvidia),
            "gpus": nvidia,
            "vram_used_mb": vram_used,
            "vram_total_mb": vram_total,
            "vram_percent": round(100.0 * vram_used / max(vram_total, 1), 1),
            "util_pct": util,
            "temp_c": temp,
        }
    elif is_apple:
        ap = _gpu_apple_stats()
        if ap: ap["unified_memory_mb"] = ram.get("total_mb")
        gpu_block = ap
    else:
        gpu_block = {"platform": "none", "count": 0, "gpus": [],
                     "vram_used_mb": 0, "vram_total_mb": 0, "vram_percent": 0,
                     "util_pct": None, "temp_c": None}
    return {
        "cpu": cpu,
        "ram": ram,
        "disk": disk,
        "gpu": gpu_block,
        "uptime_sec": int(time.time() - _WORKER_START_TIME),
    }

# ============================================================
# Health + capabilities
# ============================================================

@app.get("/health", response_class=JSONResponse)
async def health():
    """Public health endpoint — gateway polls this every 2-5s."""
    gpu = gpu_summary()
    encoder = effective_video_encoder()
    sys_stats = _system_stats()
    # Active jobs = count of jobs in PG with status in (running, queued)
    active_jobs = 0
    try:
        from app.backend.core.db_optional import _pg_conn  # type: ignore
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM v3_jobs WHERE status IN ('running','queued')")
                active_jobs = int(cur.fetchone()[0] or 0)
    except Exception:
        pass  # PG may not be available; gateway tracks active via jobs PG too
    return {
        "ok": True,
        "worker_id": WORKER_ID,
        "version": "1.0.0",
        "gpu": gpu,
        "encoder": encoder,
        "supports_chromakey_cuda": ffmpeg_supports_encoder("h264_nvenc"),
        "data_dir": str(DATA_DIR),
        "active_jobs": active_jobs,
        "system": sys_stats,
    }


@app.get("/v1/capabilities", response_class=JSONResponse)
async def capabilities(_: bool = Depends(_verify_internal)):
    """What this worker can do (for gateway's smart dispatch)."""
    gpu = gpu_summary()
    return {
        "worker_id": WORKER_ID,
        "version": "1.0.0",
        "modes": ["chroma_key"],  # TC01 only for v1.0
        "encoders": {
            "preferred": effective_video_encoder(),
            "nvenc": ffmpeg_supports_encoder("h264_nvenc"),
            "qsv": ffmpeg_supports_encoder("h264_qsv"),
            "libx264": ffmpeg_supports_encoder("libx264"),
        },
        "gpu": gpu,
        "max_concurrent": int(os.getenv("WORKER_MAX_CONCURRENT", "2")),
        "data_dir": str(DATA_DIR),
    }


# ============================================================
# File upload (internal)
# ============================================================

@app.post("/v1/jobs/{job_id}/upload/{role}")
async def upload_file(
    job_id: str,
    role: str,  # product | background | cover | audio
    request: Request,
    _: bool = Depends(_verify_internal),
):
    """Receive a file for a specific role (product/background/cover/audio)."""
    if role not in ("product", "background", "cover", "audio"):
        raise HTTPException(status_code=400, detail=f"invalid role: {role}")
    jd = _job_dir(job_id)
    body = await request.body()
    # Extract filename from Content-Disposition if present
    cd = request.headers.get("Content-Disposition", "")
    fname = f"{role}_{int(time.time())}_{secrets.token_hex(4)}.mp4"  # default to .mp4 (V3 pipelines check ext)
    if "filename=" in cd:
        try:
            raw = cd.split("filename=", 1)[1].strip('"')
            # If no extension, add .mp4 (pipelines require it)
            if "." not in raw:
                raw = raw + ".mp4"
            fname = raw
        except Exception:
            pass
    target = jd / fname
    target.write_bytes(body)
    log.info(f"job={job_id} uploaded {role} -> {target.name} ({len(body)} bytes)")
    return {
        "job_id": job_id,
        "role": role,
        "file_id": target.stem,
        "filename": target.name,
        "size": len(body),
    }


# ============================================================
# Render (internal)
# ============================================================


def _build_pipeline_inputs(
    mode: str,
    jd: Path,
    product_path: Optional[Path],
    background_path: Optional[Path],
    cover_path: Optional[Path],
    audio_path: Optional[Path],
    settings: Dict[str, Any],
    product_paths: List[Path] = None,
    background_paths: List[Path] = None,
    cover_paths: List[Path] = None,
    audio_paths: List[Path] = None,
    source_paths: List[Path] = None,
) -> "_common.PipelineInputs":
    """Build a PipelineInputs from worker request fields. The shape varies per TC:
    - tc01/tc02/tc03/tc04: product + bg (lists OK)
    - tc05: source only (reframe, no chroma)
    - tc06: product + bg + audio (audio master)
    """
    if product_paths is None: product_paths = []
    if background_paths is None: background_paths = []
    if cover_paths is None: cover_paths = []
    if audio_paths is None: audio_paths = []
    if source_paths is None: source_paths = []
    if product_path and not product_paths: product_paths = [product_path]
    if background_path and not background_paths: background_paths = [background_path]
    if cover_path and not cover_paths: cover_paths = [cover_path]
    if audio_path and not audio_paths: audio_paths = [audio_path]
    return _common.PipelineInputs(
        output_dir=str(jd),
        values=settings or {},
        products=[str(p) for p in product_paths],
        backgrounds=[str(p) for p in background_paths],
        audios=[str(p) for p in audio_paths],
        covers=[str(p) for p in cover_paths],
        sources=[str(p) for p in source_paths],
    )


def _run_v3_pipeline(
    mode: str,
    job_id: str,
    jd: Path,
    product_path: Optional[Path],
    background_path: Optional[Path],
    cover_path: Optional[Path],
    audio_path: Optional[Path],
    settings: Dict[str, Any],
    log_cb,
    progress_cb=None,
    product_paths: List[Path] = None,
    background_paths: List[Path] = None,
    cover_paths: List[Path] = None,
    audio_paths: List[Path] = None,
    source_paths: List[Path] = None,
) -> Dict[str, Any]:
    """Run a V3 TC01-TC06 pipeline. Returns dict with status, output_files, etc."""
    pipeline_fn = PIPELINES.get(mode)
    if not pipeline_fn:
        raise ValueError(f"Unknown mode: {mode}")
    inputs = _build_pipeline_inputs(
        mode, jd, product_path, background_path, cover_path, audio_path,
        settings, product_paths, background_paths, cover_paths, audio_paths, source_paths,
    )
    callbacks = _common.PipelineCallbacks(
        log_fn=log_cb,
        stop_check=lambda: False,
        progress_fn=(progress_cb or (lambda pct, info: None)),
        file_fn=lambda fn: log_cb(f"[file] {fn}"),
        pause_check=lambda: False,
        step_fn=lambda step, state: log_cb(f"[step] {step}: {state}"),
    )
    result = pipeline_fn(inputs, callbacks)
    payload = result.to_dict() if hasattr(result, "to_dict") else {"status": "unknown"}
    return payload


@app.post("/v1/jobs/{job_id}/render")
async def render_job(job_id: str, req: RenderRequest, _: bool = Depends(_verify_internal)):
    """Run green screen render for the given job."""
    t0 = time.time()
    jd = _job_dir(job_id)
    log_lines: List[str] = []

    def log_cb(msg: str):
        log_lines.append(msg)
        log.info(f"[{job_id}] {msg}")

    # Resolve files
    try:
        product = _file_for_id(job_id, req.product_id)
        background = _file_for_id(job_id, req.background_id)
        cover = _file_for_id(job_id, req.cover_id) if req.cover_id else None
        audio = _file_for_id(job_id, req.audio_id) if req.audio_id else None
    except HTTPException:
        raise

    # Build settings
    settings = _resolve_settings(req.settings)

    # Output path
    out_path = jd / f"output_{int(time.time())}.mp4"

    # Mark running
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "running",
            "started_at": t0,
            "worker_id": WORKER_ID,
        }

    # Dispatch by mode
    mode = (req.mode or "tc01").lower()
    log.info(f"job={job_id} mode={mode}")

    # V3 pipeline (TC01-TC06) — uses core.pipelines
    if mode in PIPELINES and PIPELINES.get(mode) is not None:
        def progress_cb(pct, info):
            log_cb(f"[progress] {pct}% {info}")
            with _JOBS_LOCK:
                _JOBS[job_id].setdefault("progress_pct", int(pct))
                _JOBS[job_id].setdefault("progress_info", str(info))
        try:
            payload = _run_v3_pipeline(
                mode=mode,
                job_id=job_id,
                jd=jd,
                product_path=product,
                background_path=background,
                cover_path=cover,
                audio_path=audio,
                settings=req.values or req.settings or {},
                log_cb=log_cb,
                progress_cb=progress_cb,
                product_paths=[_file_for_id(job_id, fid) for fid in (req.product_ids or [])] or ([product] if product else []),
                background_paths=[_file_for_id(job_id, fid) for fid in (req.background_ids or [])] or ([background] if background else []),
                cover_paths=[_file_for_id(job_id, fid) for fid in (req.cover_ids or [])] or ([cover] if cover else []),
                audio_paths=[_file_for_id(job_id, fid) for fid in (req.audio_ids or [])] or ([audio] if audio else []),
                source_paths=[_file_for_id(job_id, fid) for fid in (req.source_ids or [])],
            )
        except Exception as exc:
            log.error(f"job={job_id} {mode} crashed: {exc}")
            with _JOBS_LOCK:
                _JOBS[job_id].update({
                    "status": "failed", "error": str(exc),
                    "finished_at": time.time(), "log": log_lines,
                })
            raise HTTPException(status_code=500, detail=f"{mode} crashed: {exc}")
        # payload has status, outputs (list of files), errors, etc.
        elapsed = time.time() - t0
        status = payload.get("status", "unknown")
        output_files = payload.get("outputs", [])
        # Pick first output as the main one
        output_file = output_files[0] if output_files else None
        if not output_file:
            # Pipeline didn't produce output_path key — try a fallback
            output_file = payload.get("output_path")
        if not output_file:
            # scan the job dir for the newest mp4
            mp4s = sorted(jd.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if mp4s:
                output_file = mp4s[0].name
        if not output_file:
            with _JOBS_LOCK:
                _JOBS[job_id].update({
                    "status": "failed", "error": payload.get("errors", ["no output"])[0] if payload.get("errors") else "no output produced",
                    "finished_at": time.time(), "log": log_lines, "result": payload,
                })
            raise HTTPException(status_code=500, detail=f"{mode} produced no output: {payload.get('errors', ['unknown'])}")
        out_path = jd / output_file
        output_size = out_path.stat().st_size if out_path.exists() else 0
        log.info(f"job={job_id} {mode} done in {elapsed:.1f}s, {output_size} bytes, {len(output_files)} outputs")
        # Normalize status to lowercase
        status_norm = str(status).lower() if status else "succeeded"
        if "fail" in status_norm: status_norm = "failed"
        elif "cancel" in status_norm: status_norm = "cancelled"
        elif "success" in status_norm or status_norm == "succeeded": status_norm = "succeeded"
        else: status_norm = "succeeded" if not payload.get("errors") else "failed"
        with _JOBS_LOCK:
            _JOBS[job_id].update({
                "status": status_norm,
                "finished_at": time.time(),
                "output_file": output_file,
                "output_size": output_size,
                "output_files": output_files,
                "duration_sec": elapsed,
                "encoder": settings.encoder_alias,
                "log": log_lines[-50:],
                "result": payload,
                "progress_pct": 100 if status_norm == "succeeded" else 0,
            })
        return {
            "job_id": job_id,
            "status": status_norm,
            "output_file": output_file,
            "output_files": output_files,
            "output_size": output_size,
            "duration_sec": elapsed,
            "encoder": settings.encoder_alias,
            "log_lines": log_lines[-20:],
        }

    # Fallback: simple render_green (legacy single-product chroma)
    try:
        result: FfmpegResult = render_green(
            cover=str(cover) if cover else None,
            product=str(product),
            background=str(background),
            audio=str(audio) if audio else None,
            out_path=str(out_path),
            settings=settings,
            on_log=log_cb,
        )
    except Exception as exc:
        log.error(f"job={job_id} render crashed: {exc}")
        with _JOBS_LOCK:
            _JOBS[job_id].update({
                "status": "failed",
                "error": str(exc),
                "finished_at": time.time(),
                "log": log_lines,
            })
        raise HTTPException(status_code=500, detail=f"render crashed: {exc}")

    elapsed = time.time() - t0
    if not result.success:
        log.error(f"job={job_id} render failed: {result.error}")
        with _JOBS_LOCK:
            _JOBS[job_id].update({
                "status": "failed",
                "error": result.error,
                "finished_at": time.time(),
                "log": log_lines,
            })
        raise HTTPException(status_code=500, detail=result.error)

    output_size = out_path.stat().st_size if out_path.exists() else 0
    log.info(f"job={job_id} render succeeded in {elapsed:.1f}s, {output_size} bytes")
    with _JOBS_LOCK:
        _JOBS[job_id].update({
            "status": "succeeded",
            "finished_at": time.time(),
            "output_file": out_path.name,
            "output_size": output_size,
            "duration_sec": elapsed,
            "encoder": settings.encoder_alias,
            "log": log_lines[-50:],
        })

    return {
        "job_id": job_id,
        "status": "succeeded",
        "output_file": out_path.name,
        "output_size": output_size,
        "duration_sec": elapsed,
        "encoder": settings.encoder_alias,
    }


# ============================================================
# Status + download
# ============================================================

@app.get("/v1/jobs/{job_id}/status", response_model=StatusResponse)
async def get_status(job_id: str, _: bool = Depends(_verify_internal)):
    with _JOBS_LOCK:
        info = _JOBS.get(job_id)
    if not info:
        # Check if output file exists even without in-memory state
        out = list(_job_dir(job_id).glob("output_*.mp4"))
        if out:
            return StatusResponse(
                job_id=job_id,
                status="succeeded",
                worker_id=WORKER_ID,
                output_file=out[0].name,
                output_size=out[0].stat().st_size,
            )
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return StatusResponse(job_id=job_id, **info)


@app.get("/v1/jobs/{job_id}/output")
async def get_output(job_id: str, filename: str, _: bool = Depends(_verify_internal)):
    jd = _job_dir(job_id)
    target = jd / filename
    if not target.is_file() or not target.name.startswith("output_"):
        raise HTTPException(status_code=404, detail="output not found")
    return FileResponse(
        target,
        media_type="video/mp4",
        filename=filename,
    )


# Cleanup old jobs (keep last 7 days)
@app.post("/v1/admin/cleanup")
async def cleanup(days: int = 7, _: bool = Depends(_verify_internal)):
    cutoff = time.time() - days * 86400
    removed = 0
    freed_bytes = 0
    for jd in JOBS_DIR.iterdir():
        if not jd.is_dir():
            continue
        try:
            mtime = jd.stat().st_mtime
            if mtime < cutoff:
                for f in jd.rglob("*"):
                    if f.is_file():
                        freed_bytes += f.stat().st_size
                shutil.rmtree(jd)
                removed += 1
        except Exception as e:
            log.warning(f"cleanup {jd}: {e}")
    return {"removed_jobs": removed, "freed_mb": round(freed_bytes / 1024 / 1024, 2)}



# === V3 WebApp-compatible TC aliases (POST /v1/tc0X/render) ===
def _make_tc_handler(tc_name: str):
    async def _handler(job_id: str, req: RenderRequest, _: bool = Depends(_verify_internal)):
        """Alias for /v1/jobs/{job_id}/render with the right mode set."""
        req.mode = tc_name
        return await render_job(job_id, req, _)
    _handler.__name__ = f"render_{tc_name}"
    return _handler

for _tc in ("tc01", "tc02", "tc03", "tc04", "tc05", "tc06"):
    app.post(f"/v1/{_tc}/render/" + "{job_id}")(_make_tc_handler(_tc))

if __name__ == "__main__":
    import uvicorn
    log.info(f"starting V3_cursor_API worker on 0.0.0.0:{WORKER_PORT}, id={WORKER_ID}")
    log.info(f"data_dir={DATA_DIR}, internal_token={'set' if INTERNAL_TOKEN != 'dev-internal-token-change-me' else 'DEFAULT (change me!)'}")
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")
