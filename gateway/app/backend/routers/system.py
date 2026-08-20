"""System router (Phase 3.2)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import _verify_user, _verify_internal
GATEWAY_PORT = 8788  # gateway listen port (FIX Phase 3.2)


router = APIRouter()


@router.get("/api/v1/dashboard")
async def api_v1_dashboard(user: str = Depends(_verify_user), limit: int = 20):
    """Lightweight user dashboard (FIX 2026-08-18)."""
    from app.backend.services.jobs import list_user_jobs
    from app.backend.services.users import get_user_full
    stats = get_user_full(user)
    jobs = list_user_jobs(user, limit=min(max(limit, 1), 100))
    return {"user": stats, "recent_jobs": jobs}


@router.get("/healthz")
async def healthz():
    """Simple alive endpoint."""
    return {"ok": True, "service": "gateway", "port": GATEWAY_PORT}


@router.get("/api/health")
async def api_health():
    return {"ok": True, "service": "gateway", "version": "1.2.0", "port": GATEWAY_PORT}


@router.get("/api/version")
async def api_version():
    return {"ok": True, "version": "1.2.0"}


@router.get("/api/ffmpeg")
async def api_ffmpeg():
    return {"ok": True, "ffmpeg": "8.0.1"}


@router.get("/api/encoders")
async def api_encoders():
    return {"ok": True, "encoders": ["libx264", "h264_nvenc", "hevc_nvenc", "h264_videotoolbox"]}


@router.get("/api/lens")
async def api_lens():
    return {"ok": True, "lenses": ["lens16mm", "lens35mm", "lens40mm", "lens45mm", "lens50mm", "lens55mm", "lens60mm"]}


@router.get("/api/config")
async def api_config():
    return {"ok": True, "config": {"output_format": "mp4", "max_resolution": "1080x1920"}}


@router.get("/api/outputs")
async def api_outputs():
    from pathlib import Path
    import os
    from app.backend.deps import OUTPUTS_DIR
    if not OUTPUTS_DIR.exists():
        return {"ok": True, "files": []}
    files = sorted(OUTPUTS_DIR.glob("**/*.mp4"), key=lambda p: -p.stat().st_mtime)[:50]
    return {"ok": True, "files": [{"name": f.name, "size": f.stat().st_size, "modified": f.stat().st_mtime} for f in files]}


@router.get("/api/download/{file_path:path}")
async def api_download(file_path: str):
    """Legacy output file proxy."""
    from app.backend.deps import DATA_DIR
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(400, "invalid path")
    target = (DATA_DIR / file_path).resolve()
    if not target.is_file() or DATA_DIR.resolve() not in target.parents:
        raise HTTPException(404, "not found")
    from fastapi.responses import FileResponse
    return FileResponse(str(target))
