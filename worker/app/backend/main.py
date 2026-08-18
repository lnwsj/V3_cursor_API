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
from core.pipelines import (
    render_tc01, render_tc02, render_tc03, render_tc04, render_tc05, render_tc06,
)

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
    product_id: str  # upload id
    background_id: str
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None  # GreenSettings overrides


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
# Health + capabilities
# ============================================================

@app.get("/health", response_class=JSONResponse)
async def health():
    """Public health endpoint — gateway polls this every 2-5s."""
    gpu = gpu_summary()
    encoder = effective_video_encoder()
    return {
        "ok": True,
        "worker_id": WORKER_ID,
        "version": "1.0.0",
        "gpu": gpu,
        "encoder": encoder,
        "supports_chromakey_cuda": ffmpeg_supports_encoder("h264_nvenc"),
        "data_dir": str(DATA_DIR),
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
    fname = f"{role}_{int(time.time())}_{secrets.token_hex(4)}"
    if "filename=" in cd:
        try:
            fname = cd.split("filename=", 1)[1].strip('"')
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

    # Run render
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
            "log": log_lines[-50:],  # keep last 50 lines
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
# TC02-TC06 pipeline render (v3.PARALLEL: parallel chroma for TC02)
# ============================================================

class TCRenderRequest(BaseModel):
    """Multi-file render request used by TC02/TC03/TC05/TC06.
    
    product_ids/background_ids/cover_ids/audio_ids/source_ids are lists
    of uploaded file ids (filename stem). For TC01 the singular
    product_id/background_id/cover_id/audio_id fields are also accepted.
    """
    product_id: Optional[str] = None
    background_id: Optional[str] = None
    cover_id: Optional[str] = None
    audio_id: Optional[str] = None
    product_ids: List[str] = []
    background_ids: List[str] = []
    cover_ids: List[str] = []
    audio_ids: List[str] = []
    source_ids: List[str] = []
    mode: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None
    values: Optional[Dict[str, Any]] = None


def _file_for_id_or_path(job_id: str, file_id: str) -> Path:
    """Resolve file_id (stem) to a path; raise 404 if not found."""
    return _file_for_id(job_id, file_id)


def _build_tc_inputs(job_id: str, req: TCRenderRequest) -> tuple:
    """Convert TCRenderRequest to PipelineInputs lists of file paths."""
    def resolve(ids: List[str]) -> List[str]:
        out = []
        for fid in ids:
            try:
                out.append(str(_file_for_id_or_path(job_id, fid)))
            except HTTPException:
                continue
        return out
    products = resolve(req.product_ids) or ([str(_file_for_id_or_path(job_id, req.product_id))] if req.product_id else [])
    backgrounds = resolve(req.background_ids) or ([str(_file_for_id_or_path(job_id, req.background_id))] if req.background_id else [])
    audios = resolve(req.audio_ids) or ([str(_file_for_id_or_path(job_id, req.audio_id))] if req.audio_id else [])
    covers = resolve(req.cover_ids) or ([str(_file_for_id_or_path(job_id, req.cover_id))] if req.cover_id else [])
    return products, backgrounds, audios, covers


def _run_tc_pipeline(tc_label: str, render_fn, job_id: str, req: TCRenderRequest):
    """Generic TC01-TC06 runner that returns a JSON-serializable result."""
    t0 = time.time()
    jd = _job_dir(job_id)
    out_dir = jd  # TC01 outputs to job dir, TC02/etc. use subfolders
    log_lines: List[str] = []

    def log_cb(msg: str):
        log_lines.append(msg)
        log.info(f"[{job_id}] {msg}")

    try:
        products, backgrounds, audios, covers = _build_tc_inputs(job_id, req)
    except HTTPException:
        raise

    values = req.values or req.settings or {}
    n_parallel = int(os.environ.get(f"V3_{tc_label.upper()}_PARALLEL", os.environ.get("V3_TC02_PARALLEL", "1") or "1"))

    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "running",
            "started_at": t0,
            "worker_id": WORKER_ID,
            "tc": tc_label,
        }

    try:
        from core.pipelines._common import PipelineInputs, PipelineCallbacks
        inputs = PipelineInputs(
            output_dir=str(out_dir),
            values=values,
            products=products,
            backgrounds=backgrounds,
            audios=audios,
            covers=covers,
        )
        cb = PipelineCallbacks(
            log_fn=log_cb,
            stop_check=lambda: False,
            progress_fn=lambda pct, msg: None,
            file_fn=lambda n: None,
            pause_check=lambda: False,
        )
        result = render_fn(inputs, cb)
    except Exception as exc:
        log.error(f"job={job_id} {tc_label} crashed: {exc}")
        with _JOBS_LOCK:
            _JOBS[job_id].update({
                "status": "failed",
                "error": str(exc),
                "finished_at": time.time(),
                "log": log_lines,
            })
        raise HTTPException(status_code=500, detail=f"{tc_label} render crashed: {exc}")

    elapsed = time.time() - t0
    output_files = []
    upload_prefixes = ("background_", "product_", "source_", "cover_", "audio_")
    for f in jd.iterdir():
        if f.suffix != ".mp4":
            continue
        if f.name.endswith(".partial.*"):
            continue
        # Skip raw upload files (no __lens*__tc*__ marker)
        if f.name.startswith(upload_prefixes) and "__lens" not in f.name:
            continue
        output_files.append(f.name)
    output_files.sort()

    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    with _JOBS_LOCK:
        _JOBS[job_id].update({
            "status": "succeeded" if result.is_success else status.lower(),
            "finished_at": time.time(),
            "output_file": output_files[0] if output_files else None,
            "output_size": (jd / output_files[0]).stat().st_size if output_files else 0,
            "duration_sec": elapsed,
            "encoder": values.get("encoder_alias", "libx264"),
            "log": log_lines[-50:],
        })

    return {
        "job_id": job_id,
        "tc": tc_label,
        "status": status,
        "expected": result.expected,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "cancelled": result.cancelled,
        "output_files": output_files,
        "output_file": output_files[0] if output_files else None,
        "output_size": (jd / output_files[0]).stat().st_size if output_files else 0,
        "duration_sec": elapsed,
        "encoder": values.get("encoder_alias", "libx264"),
        "n_parallel": n_parallel,
    }


# TC01 (multi-file capable)
@app.post("/v1/tc01/render/{job_id}")
async def render_tc01_endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)):
    return _run_tc_pipeline("TC01", render_tc01, job_id, req)


# TC02 (reframe + chroma with optional parallel chroma)
@app.post("/v1/tc02/render/{job_id}")
async def render_tc02_endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)):
    return _run_tc_pipeline("TC02", render_tc02, job_id, req)


# TC03
@app.post("/v1/tc03/render/{job_id}")
async def render_tc03_endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)):
    return _run_tc_pipeline("TC03", render_tc03, job_id, req)


# TC04
@app.post("/v1/tc04/render/{job_id}")
async def render_tc04_endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)):
    return _run_tc_pipeline("TC04", render_tc04, job_id, req)


# TC05
@app.post("/v1/tc05/render/{job_id}")
async def render_tc05_endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)):
    return _run_tc_pipeline("TC05", render_tc05, job_id, req)


# TC06
@app.post("/v1/tc06/render/{job_id}")
async def render_tc06_endpoint(job_id: str, req: TCRenderRequest, _: bool = Depends(_verify_internal)):
    return _run_tc_pipeline("TC06", render_tc06, job_id, req)


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


if __name__ == "__main__":
    import uvicorn
    log.info(f"starting V3_cursor_API worker on 0.0.0.0:{WORKER_PORT}, id={WORKER_ID}")
    log.info(f"data_dir={DATA_DIR}, internal_token={'set' if INTERNAL_TOKEN != 'dev-internal-token-change-me' else 'DEFAULT (change me!)'}")
    uvicorn.run(app, host="0.0.0.0", port=WORKER_PORT, log_level="info")
